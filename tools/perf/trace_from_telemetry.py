#!/usr/bin/env python3
"""Generate a synthetic-but-realistic MCP replay trace from a target ENGRAM graph.

Part of the replay-bench harness (blueprint §6, docs/perf/optimization-blueprint.md,
umbrella #1668). Produces a JSONL trace of `{"tool_name": ..., "params": {...},
"ts": ...}` calls that `replay_bench.py` replays against a sandboxed copy of the
same graph.

Call-mix source: the `tool_timing` SQLite table (schema: id, timestamp, tool_name,
duration_ms, status, turn — src/engram/engram_core.py ~line 1770). This table has
NO stored call parameters — it is used ONLY to derive a realistic call-mix /
frequency / rough-ordering distribution across tools.

Param synthesis is a SEPARATE step from tool_timing (since tool_timing carries no
params): real node IDs are sampled from the `nodes` table (id, type columns only)
for ID-taking tools. Free-text query strings are synthesized from a small fixed
set of generic templates.

CONTENT-ANONYMIZATION REQUIREMENT: this module never reads or copies `claim`,
`quoted_text`, `interpretation`, or any other free-text content column into the
trace. Real node IDs are structural identifiers (safe to reuse); real claim/quote
text is never touched. Only `id` and `type` are selected from the `nodes` table.

v1 tool scope (READ_TOOLS below) is deliberately read-only — see
docs/perf/optimization-blueprint.md §1: reads are ~10x the cost of writes in the
current telemetry, so that's where today's perf story lives. Write-tool payload
synthesis (engram_add_observation, engram_derive, ...) is out of scope for v1 —
fabricating a realistic claim/quote is a different, harder problem.
"""

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

# The read-heavy tools that are both the actual perf story (per the blueprint's
# telemetry table) and have an honest, content-anonymized synthesis path.
READ_TOOLS = (
    "engram_inspect",
    "engram_query",
    "engram_surface",
    "engram_list",
    "engram_get_subgraph",
)

# Tools whose payload needs a real node_id sampled from the target graph.
NODE_ID_TOOLS = frozenset({"engram_inspect", "engram_get_subgraph"})

# A handful of varied synthetic query strings for query/surface payloads. Plain
# generic phrases — never derived from any real node's claim/quote text.
SYNTHETIC_QUERY_TEMPLATES = (
    "recent observations about testing",
    "derivation confidence calibration",
    "quote verification provenance",
    "memory tier decay behavior",
    "contradiction resolution history",
    "trust pool advisory checks",
    "focus mode node lifecycle",
    "semantic surface daemon latency",
    "recall summary generation",
    "axiom and cornerstone review",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open db_path strictly read-only (mode=ro URI) — this module only ever
    issues SELECTs, but a plain sqlite3.connect() still opens read-write by
    default, which is worth avoiding on principle if --data-dir is ever
    pointed at a live directory rather than the read-only copy this harness
    recommends (see replay_bench.py's _copy_data_dir docstring for the
    parallel caveat on the copy side)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _load_call_mix(db_path: Path, tools=READ_TOOLS) -> list[str]:
    """Return tool_name values from tool_timing, restricted to `tools`, ordered
    by (timestamp, id) to approximate the real call sequence.

    Returns [] if knowledge.db, the tool_timing table, or any matching rows
    are missing — the caller falls back to a uniform distribution.
    """
    if not db_path.exists():
        return []
    conn = _connect_ro(db_path)
    try:
        if not _table_exists(conn, "tool_timing"):
            return []
        placeholders = ",".join("?" * len(tools))
        cur = conn.execute(
            f"SELECT tool_name FROM tool_timing WHERE tool_name IN ({placeholders}) "
            f"ORDER BY timestamp, id",
            list(tools),
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _sample_node_ids(db_path: Path, limit: int = 2000) -> tuple[list[str], list[str]]:
    """Return (all real node ids, distinct node types) from the nodes table.

    Only `id` and `type` columns are read — never claim/quoted_text/interpretation
    (content-anonymization requirement). Returns ([], []) if knowledge.db or the
    nodes table is missing.
    """
    if not db_path.exists():
        return [], []
    conn = _connect_ro(db_path)
    try:
        if not _table_exists(conn, "nodes"):
            return [], []
        cur = conn.execute("SELECT id, type FROM nodes LIMIT ?", (int(limit),))
        rows = cur.fetchall()
        ids = [row[0] for row in rows]
        types = sorted({row[1] for row in rows if row[1]})
        return ids, types
    finally:
        conn.close()


def _expand_to_n(seq: list[str], n: int) -> list[str]:
    """Expand/contract a real call-mix sequence to exactly n entries.

    len(seq) >= n: systematic (evenly-spaced) sample, preserving relative order.
    len(seq) <  n: tile the sequence (repeat in order) until length n.

    Deterministic — both branches are pure index arithmetic, no randomness
    involved (an earlier `rng` parameter here was unused and has been removed).
    """
    if not seq:
        return []
    if len(seq) >= n:
        return [seq[int(i * len(seq) / n)] for i in range(n)]
    reps = -(-n // len(seq))  # ceil division
    return (seq * reps)[:n]


def _synthesize_params(tool_name: str, node_ids: list[str], node_types: list[str],
                        rng: random.Random) -> dict:
    if tool_name in NODE_ID_TOOLS:
        return {"node_id": rng.choice(node_ids)}
    if tool_name in ("engram_query", "engram_surface"):
        return {"query": rng.choice(SYNTHETIC_QUERY_TEMPLATES)}
    if tool_name == "engram_list":
        if node_types and rng.random() < 0.5:
            return {"node_type": rng.choice(node_types)}
        return {}
    raise ValueError(f"No param-synthesis rule for tool {tool_name!r}")


def generate_trace(data_dir, out_path, n_calls: int = 300, seed: int | None = None) -> int:
    """Generate a replay trace JSONL at out_path, sampled from data_dir's graph.

    Args:
        data_dir: path to an ENGRAM data dir (contains knowledge.db).
        out_path: destination JSONL path (parent dirs created if needed).
        n_calls: number of calls to write.
        seed: optional RNG seed for reproducible synthesis.

    Returns:
        Number of calls written (== n_calls, unless n_calls <= 0).

    Raises:
        RuntimeError: if the graph has no nodes at all (nothing to sample —
            every in-scope tool needs at least a query template or a node id,
            and node id tools can't run with zero real nodes to draw from; if
            node_ids is empty every remaining tool still works off synthetic
            query templates, so this only fires when node sampling AND the
            remaining non-node-id tool set are both unusable).
    """
    data_dir = Path(data_dir)
    db_path = data_dir / "knowledge.db"
    rng = random.Random(seed)

    node_ids, node_types = _sample_node_ids(db_path)
    call_mix = _load_call_mix(db_path)

    usable_tools = list(READ_TOOLS)
    if not node_ids:
        usable_tools = [t for t in usable_tools if t not in NODE_ID_TOOLS]
        print(
            f"[trace_from_telemetry] WARNING: no real node ids found in {db_path} "
            f"— dropping {sorted(NODE_ID_TOOLS)} from the call mix.",
            file=sys.stderr,
        )
    # Currently unreachable given today's READ_TOOLS/NODE_ID_TOOLS constants —
    # there are always non-node-id tools (engram_query/engram_surface/
    # engram_list) left in usable_tools even when node_ids is empty and
    # NODE_ID_TOOLS is dropped above. This is a defensive guard against a
    # FUTURE edit to those constants that makes READ_TOOLS all node-id tools,
    # not dead code to delete.
    if not usable_tools:
        raise RuntimeError(
            f"No usable tools remain for {db_path} — graph has no nodes at all."
        )

    call_mix = [t for t in call_mix if t in usable_tools]

    if call_mix:
        sequence = _expand_to_n(call_mix, n_calls)
    else:
        print(
            f"[trace_from_telemetry] WARNING: no tool_timing rows found for "
            f"{sorted(READ_TOOLS)} in {db_path} — falling back to a uniform "
            f"call mix over {usable_tools}.",
            file=sys.stderr,
        )
        sequence = [rng.choice(usable_tools) for _ in range(max(n_calls, 0))]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for i, tool_name in enumerate(sequence):
            params = _synthesize_params(tool_name, node_ids, node_types, rng)
            f.write(json.dumps({"tool_name": tool_name, "params": params, "ts": i}) + "\n")
    return len(sequence)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate a synthetic replay trace from an ENGRAM graph's telemetry."
    )
    parser.add_argument("--data-dir", required=True,
                         help="Path to an ENGRAM data dir to sample from (contains knowledge.db).")
    parser.add_argument("--out", required=True, help="Output trace.jsonl path.")
    parser.add_argument("--n-calls", type=int, default=300,
                         help="Number of calls to synthesize (default 300).")
    parser.add_argument("--seed", type=int, default=None,
                         help="Optional RNG seed for reproducible synthesis.")
    args = parser.parse_args(argv)

    n = generate_trace(args.data_dir, args.out, n_calls=args.n_calls, seed=args.seed)
    print(f"[trace_from_telemetry] Wrote {n} calls to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
