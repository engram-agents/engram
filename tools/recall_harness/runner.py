"""Drive the system-under-test (current `_search_nodes`) against the Phase 3
snapshot for each golden query. Produces test RRUs that can be compared
to golden RRUs via the metric registry.

For v1: single-branch (current dev HEAD), in-process call into server.py's
`_search_nodes`. No worktree, no MCP. Snapshot DB is opened read-only and
the embedder global is reused.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import Optional


def _looks_like_engram_server(server_py_path: str) -> bool:
    """Cheap textual check that `server_py_path` is actually ENGRAM's
    server.py, not just a same-named file from an unrelated ancestor
    directory picked up by the walk-parents search below.

    Checks for a `_search_nodes` definition in the file's own text. A text
    check (not an import) is deliberate: importing an arbitrary stranger's
    server.py during path resolution -- before we even know it's ours -- is
    worse than the bug this guards against.
    """
    try:
        with open(server_py_path, encoding="utf-8", errors="replace") as f:
            contents = f.read()
    except OSError:
        return False
    return "def _search_nodes" in contents


def _resolve_engram_alpha_dir() -> str:
    """Resolve the sys.path entry that makes `import server` succeed.

    Priority: `$ENGRAM_ALPHA_DIR` (explicit override — the directory
    CONTAINING server.py, not necessarily the repo root) > `$CLAUDE_PLUGIN_ROOT`
    (set when running as an installed plugin, where server.py sits flat at
    the plugin root) > a walk-parents search from this file's own location.
    `ENGRAM_ALPHA_DIR` returns when set to a non-empty value; `CLAUDE_PLUGIN_ROOT`
    returns only if `server.py` exists directly under it. Neither override
    applies a content check -- only the walk-parents search (below) is
    hardened against a decoy.

    The walk-parents search checks, at each ancestor of this file, for
    server.py directly (installed-plugin / flat layout) OR at
    `<ancestor>/src/engram/server.py` (this repo's dev-checkout layout,
    mirroring tests/conftest.py's SRC_ENGRAM convention) — server.py is NOT
    tracked at the repo root itself (only under src/engram/), so a plain
    same-directory check would never find it walking up from tools/. A
    found server.py is only accepted if it looks like ENGRAM's own
    (`_looks_like_engram_server`) -- otherwise an unrelated ancestor
    directory that happens to contain its own server.py would be picked as
    the repo root.

    No default points at any home directory: this harness ships inside the
    repo it measures (tools/recall_harness/), so the right sys.path entry is
    always derivable from `__file__` unless deliberately overridden. Fails
    loudly (naming the env var to set) rather than falling back to a guessed
    path — per project convention (CLAUDE.md "Plugin build restructures code
    paths"), a fixed `parents[N]` silently overshoots if this package is ever
    copied to a different nesting depth, so we search instead of hardcoding
    a depth.
    """
    override = os.environ.get("ENGRAM_ALPHA_DIR")
    if override:
        return override

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root and os.path.exists(os.path.join(plugin_root, "server.py")):
        return plugin_root

    here = os.path.dirname(os.path.abspath(__file__))
    candidate = here
    for _ in range(10):
        # Installed-plugin / flat layout: server.py sits directly here.
        flat_server = os.path.join(candidate, "server.py")
        if os.path.exists(flat_server) and _looks_like_engram_server(flat_server):
            return candidate
        # Dev-checkout layout: real sources live under src/engram/.
        src_engram = os.path.join(candidate, "src", "engram")
        nested_server = os.path.join(src_engram, "server.py")
        if os.path.exists(nested_server) and _looks_like_engram_server(nested_server):
            return src_engram
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent

    raise RuntimeError(
        f"Could not locate engram-alpha's server.py by walking up from "
        f"{here!r} (checked both '<dir>/server.py' and "
        "'<dir>/src/engram/server.py' at each level, requiring a "
        "_search_nodes definition to accept a match), and neither "
        "ENGRAM_ALPHA_DIR nor CLAUDE_PLUGIN_ROOT is set. Set "
        "ENGRAM_ALPHA_DIR to the directory containing server.py."
    )


ENGRAM_ALPHA_DIR = _resolve_engram_alpha_dir()
if ENGRAM_ALPHA_DIR not in sys.path:
    # append, never insert(0): src/engram/ contains its OWN `tools/`
    # package (a different package than this repo's root-level `tools/`,
    # which is where THIS file lives). Putting src/engram ahead of the repo
    # root on sys.path makes `import tools` resolve to the wrong package and
    # caches that resolution for the rest of the process — the exact
    # sys.path-collision class guarded against in tests/conftest.py (#2565).
    # Appending keeps whatever already-present repo-root entry wins.
    sys.path.append(ENGRAM_ALPHA_DIR)

import server as engram_server  # noqa: E402

from .rru import RankingResultUnit, RankedNode
from .golden_loader import load_golden_set


def _default_snapshot_path() -> str | None:
    """Resolve the snapshot DB path from the environment, or None if unset.

    No default points at any home directory or /tmp path — a missing
    snapshot must fail loudly (via `open_snapshot`'s existence check), never
    silently create/measure an empty database. Set `ENGRAM_RECALL_SNAPSHOT`
    to the snapshot `.db` file to measure against.
    """
    return os.environ.get("ENGRAM_RECALL_SNAPSHOT")


def resolve_snapshot_path(snapshot_path: str | None = None) -> str:
    """Resolve the snapshot path: prefer an explicit argument, else fall back
    to `$ENGRAM_RECALL_SNAPSHOT`. Raises RuntimeError (naming the env var) if
    neither is available — never guesses a path.

    Deliberately does NOT check existence — `open_snapshot` owns that check
    (kept separate so a resolved-but-nonexistent path still hits the
    load-bearing FileNotFoundError guard rather than a resolution-time one).
    """
    if snapshot_path is None:
        snapshot_path = _default_snapshot_path()
    if not snapshot_path:
        raise RuntimeError(
            "No snapshot path given and ENGRAM_RECALL_SNAPSHOT is not set. "
            "Set ENGRAM_RECALL_SNAPSHOT to the snapshot .db file to measure "
            "against, or pass snapshot_path explicitly."
        )
    return snapshot_path


def open_snapshot(snapshot_path: str | None = None) -> sqlite3.Connection:
    """Open the snapshot DB. sqlite-vec extension needs loading.

    `snapshot_path` defaults to `$ENGRAM_RECALL_SNAPSHOT` when omitted (via
    `resolve_snapshot_path`, which fails loudly naming the env var if neither
    is available — never falls back to a guessed path). Also guards against
    sqlite3.connect silently *creating* an empty database at a missing path
    (empirically confirmed: subsequent queries then fail with "no such
    table: nodes", measuring an empty graph instead of erroring) — this
    existence check is load-bearing, keep it.
    """
    snapshot_path = resolve_snapshot_path(snapshot_path)
    if not os.path.exists(snapshot_path):
        raise FileNotFoundError(f"snapshot not found: {snapshot_path}")
    # Use read-write for vocab-enabled snapshot so IDF queries work.
    conn = sqlite3.connect(snapshot_path)
    conn.row_factory = sqlite3.Row

    # Load sqlite-vec extension (same way server.py does for live DB)
    if engram_server._sqlite_vec is not None:
        try:
            conn.enable_load_extension(True)
            engram_server._sqlite_vec.load(conn)
        except Exception:
            pass  # fall through to slow-path; _search_nodes handles missing vec
    return conn


def run_one(
    conn: sqlite3.Connection,
    golden: RankingResultUnit,
    top_k: int = 20,
    include_superseded: bool = False,
    use_semantic: bool = True,
) -> RankingResultUnit:
    """Run the system-under-test once for one golden query. Returns a test
    RRU whose ranked_nodes preserve the system's output order and capture
    per-candidate signals (similarity, match_type, composite if present).

    Mirrors the production hook (engram-surface-hook.py:500-514, PR #186):
    when context.prev_response_tail is populated, prepend it to the embedding
    query (semantic side only; FTS still uses the raw prompt). Short-prompt
    threshold is NOT enforced here — the test-set already pre-filtered which
    queries should fire prepending (the prepending_fire category)."""
    ctx = golden.metadata.get("context") or {}
    prev_tail = ctx.get("prev_response_tail") if isinstance(ctx, dict) else None

    embed_query: Optional[str] = None
    if prev_tail:
        embed_query = f"{prev_tail}\n\n{golden.query_text}"

    search = engram_server._search_nodes(
        conn,
        query=golden.query_text,
        top_k=top_k,
        include_superseded=include_superseded,
        use_semantic=use_semantic,
        embed_query=embed_query,
        return_debug=True,  # capture per-query + per-result signals (PR #206)
    )
    results = search.get("results", [])
    debug = search.get("debug") or {}
    debug_per_result = {e["id"]: e for e in (debug.get("per_result") or [])}

    ranked: list[RankedNode] = []
    for i, n in enumerate(results, start=1):
        signals: dict = {}
        if "similarity" in n:
            signals["similarity"] = n["similarity"]
        if "match_type" in n:
            signals["match_type"] = n["match_type"]
        if "_composite" in n:
            signals["composite"] = n["_composite"]
        if "utility_score" in n and n["utility_score"] is not None:
            signals["utility_score"] = n["utility_score"]
        if "importance_score" in n and n["importance_score"] is not None:
            signals["importance_score"] = n["importance_score"]
        if "confidence" in n and n["confidence"] is not None:
            signals["confidence"] = n["confidence"]
        # Merge in the debug-payload per-result signals (bm25_raw,
        # relevance_normalized, importance_base, surprise_score, recall_turn,
        # etc.). Existing keys above take precedence — they came from the
        # primary result-node and reflect the post-rerank values.
        dbg = debug_per_result.get(n["id"]) or {}
        for k in ("bm25_raw", "relevance_normalized", "importance_base",
                  "surprise_score", "recall_turn"):
            if k in dbg and dbg[k] is not None and k not in signals:
                signals[k] = dbg[k]
        ranked.append(RankedNode(
            id=n["id"],
            rank=i,
            signals=signals or None,
        ))

    return RankingResultUnit(
        query_id=golden.query_id,
        query_text=golden.query_text,
        ranked_nodes=ranked,
        metadata={
            "algo": "engram_query_dev_head",
            "top_k": top_k,
            "include_superseded": include_superseded,
            "use_semantic": use_semantic,
            "keyword_matches": search.get("keyword_matches", 0),
            "semantic_matches": search.get("semantic_matches", 0),
            "semantic_available": search.get("semantic_available", False),
            # Per-query debug signals (extracted_keywords, fts_query_string,
            # candidate counts, etc.) — surface in eval JSON so the SxS UI
            # can show "WHY this candidate pool".
            "query_debug": {
                "extracted_keywords": debug.get("extracted_keywords"),
                "fts_query_string": debug.get("fts_query_string"),
                "fts_candidate_count": debug.get("fts_candidate_count"),
                "semantic_candidate_count": debug.get("semantic_candidate_count"),
                "fts_cap_applied": debug.get("fts_cap_applied"),
                "embed_query_used": debug.get("embed_query_used"),
                "embed_query_overridden": debug.get("embed_query_overridden"),
                "snapshot_turn": debug.get("snapshot_turn"),
            } if debug else None,
        },
    )


def run_all(
    snapshot_path: str | None = None,
    top_k: int = 20,
    include_superseded: bool = False,
) -> tuple[dict[str, RankingResultUnit], str]:
    """Run the system against every golden in the canonical ground-truth set.

    Returns `({query_id: test_rru}, resolved_snapshot_path)` — the second
    element is the ACTUAL path opened (via `resolve_snapshot_path`), so
    callers can record true snapshot provenance instead of a literal (see
    cli.py's `--snapshot` argument and its JSON report's `snapshot` field).
    """
    resolved_path = resolve_snapshot_path(snapshot_path)
    conn = open_snapshot(resolved_path)
    goldens = load_golden_set()
    out: dict[str, RankingResultUnit] = {}
    for qid in sorted(goldens):
        out[qid] = run_one(conn, goldens[qid], top_k=top_k,
                            include_superseded=include_superseded)
    conn.close()
    return out, resolved_path


if __name__ == "__main__":
    runs, _snapshot_used = run_all()
    print(f"Ran {len(runs)} queries against snapshot")
    for qid in sorted(runs)[:3]:
        r = runs[qid]
        print(f"  {qid}: returned {len(r.ranked_nodes)} candidates "
              f"(kw={r.metadata['keyword_matches']}, sem={r.metadata['semantic_matches']})")
        print(f"    top-3: {[n.id for n in r.ranked_nodes[:3]]}")
