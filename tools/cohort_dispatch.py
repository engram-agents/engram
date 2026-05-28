#!/usr/bin/env python3
"""cohort_dispatch.py — Batch-summary cohort orchestration for the sleep cycle.

Three subcommands drive the validate/retry loop that replaces the serial
engram-summary-fairy pattern:

  prepare         Sample/chunk a cohort into per-chunk prompt + payload files.
  validate        Validate per-chunk agent output; split clean vs failures.
  incorporate-retry  Merge a retry fairy's output into the final payload.

Architecture rationale (Lei 2026-05-27):
  - Validation failures are retried by re-dispatching a fairy, NOT by
    mechanical script fixes. Quality of recall_summary / recall_keywords
    is critical because they surface in recall_surface and many node-rendering
    tools. One extra batch round is a reasonable price for quality.
  - This script does NOT modify LLM output. It validates, packages retry
    context, and packages the final payload. All content modification is
    done by the LLM (initial fairy or retry fairy).

Empirical basis: N=15 worst-case batch at 37K tokens / 1 sub-agent turn
produces summaries with cosine similarity within in-sample variance of the
serial-pattern ground truth (mean Δ cosine = +0.043, pooled stdev = 0.118).
Token reduction vs serial: ~95% for a 50-node / 4-chunk cohort.

Usage:
    python -m tools.cohort_dispatch prepare --ids <comma-sep-ids> --out <dir>
    python -m tools.cohort_dispatch prepare --ids-file <path> --out <dir> [--chunk-size 15]
    python -m tools.cohort_dispatch validate --out <dir>
    python -m tools.cohort_dispatch incorporate-retry --retry-output <path> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Locate SSoT modules — support both "run as module" and direct invocation
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.recall_summary_validator import validate_summary_entry  # noqa: E402
from tools.recall_summary_payload import PayloadBuilder  # noqa: E402
from tools.recall_summary_prompts import TYPE_GUIDANCE, DEFAULT_GUIDANCE, KEYWORD_GUIDANCE  # noqa: E402

# ---------------------------------------------------------------------------
# Default ENGRAM DB path (overridable via --db)
# ---------------------------------------------------------------------------

DEFAULT_DB = Path.home() / ".engram" / "knowledge.db"

# ---------------------------------------------------------------------------
# Node fields fetched from SQLite (read-only)
# ---------------------------------------------------------------------------

_NODE_COLUMNS = "id, type, claim, quoted_text, interpretation"


def _fetch_nodes(db_path: Path, node_ids: list[str]) -> list[dict]:
    """Read node content from ENGRAM SQLite. Read-only; no MCP access."""
    if not db_path.exists():
        raise FileNotFoundError(f"ENGRAM database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(node_ids))
        rows = conn.execute(
            f"SELECT {_NODE_COLUMNS} FROM nodes WHERE id IN ({placeholders})",
            node_ids,
        ).fetchall()
    finally:
        conn.close()
    by_id = {r["id"]: dict(r) for r in rows}
    # Return in the same order as input IDs; skip silently if not found
    return [by_id[nid] for nid in node_ids if nid in by_id]


def _build_payload_entry(node: dict) -> dict:
    """Strip fields the fairy must not see (recall_summary, recall_keywords)."""
    entry: dict = {
        "id": node["id"],
        "type": node["type"],
        "claim": node["claim"] or "",
    }
    if node.get("quoted_text"):
        entry["quoted_text"] = node["quoted_text"]
    if node.get("interpretation"):
        entry["interpretation"] = node["interpretation"]
    return entry


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_TYPE_GUIDANCE_BLOCK = "\n".join(
    f"- **{t}**: {g}" for t, g in TYPE_GUIDANCE.items()
)

_DEFAULT_GUIDANCE_LINE = DEFAULT_GUIDANCE


def _build_initial_prompt(payload: list[dict]) -> str:
    """Build the Markdown prompt for a fresh batch-summary-fairy dispatch.

    The prompt is self-contained: all type guidance, summary rules, keyword
    rules, output format, and the payload are embedded. The fairy needs no
    tool access.
    """
    n = len(payload)
    payload_block = json.dumps(payload, indent=2, ensure_ascii=False)

    return textwrap.dedent(f"""\
        # Task: Batch-generate recall_summary + recall_keywords for {n} ENGRAM nodes

        You are a one-shot batch summary generator dispatched as `engram-batch-summary-fairy`.
        Receive {n} node payloads below. For each node, produce ONE recall_summary
        (≤120 char target, ≤200 char hard cap) plus 3–5 recall_keywords. Emit ALL
        {n} entries in ONE JSON object, no tool calls, no intermediate reasoning
        text in the output — just the final JSON.

        ## Per-type guidance

        For each node, look up the guidance by the `type` field:

        {_TYPE_GUIDANCE_BLOCK}

        If the type is not listed, fall back to: {_DEFAULT_GUIDANCE_LINE}

        ## Summary rules

        - Single line, ≤120 characters (target). Hard cap ≤200.
        - Active voice. No hedging. No meta-language ("this node says...").
        - Keep technical terms verbatim if load-bearing.
        - No quotation marks unless part of the term.
        - Don't pad to fill the budget — shorter is fine for simple claims.

        ## Keyword rules

        {KEYWORD_GUIDANCE}

        - Output 3 to 5 keywords per node (NOT exactly 3, NOT exactly 5 — as many as carry distinct signal)
        - Each keyword ≤ 30 characters
        - DO NOT use ENGRAM node IDs (e.g. ob_1234, dv_0500) as keywords
        - DO NOT use generic words: node, claim, observation, derivation, agent, system

        ## Output format

        Emit EXACTLY this JSON shape (no other text, no markdown fences):

        {{"items": [{{"node_id": "<id>", "recall_summary": "<summary>", "recall_keywords": ["kw1", "kw2", "kw3"]}}, ...]}}

        Self-check before output:
        - items array length must equal {n}
        - each recall_summary ≤200 chars
        - each recall_keywords array between 3 and 5 entries, no duplicates, each ≤30 chars
        - no ENGRAM node IDs in any keyword list
        - IDs in items must come from the payload below — do not invent IDs

        ## Payload ({n} nodes)

        ```json
        {payload_block}
        ```

        Emit the JSON now. One turn only. Do NOT call any tools.
    """)


def _build_retry_prompt(
    failures: list[dict],
) -> str:
    """Build the Markdown prompt for a retry dispatch targeting failed nodes.

    Each entry in `failures` has: node_id, payload (dict), error (str).
    The retry fairy receives the same type guidance + rules PLUS a
    "Previous attempt failed" section with verbatim validator errors.
    """
    n = len(failures)
    payload = [f["payload"] for f in failures]
    payload_block = json.dumps(payload, indent=2, ensure_ascii=False)

    failure_lines = []
    for f in failures:
        failure_lines.append(
            f"- node_id: {f['node_id']}\n"
            f"  error: {f['error']}\n"
            f"  original_payload: {json.dumps(f['payload'], ensure_ascii=False)}"
        )
    failure_block = "\n\n".join(failure_lines)

    return textwrap.dedent(f"""\
        # Task: Retry — batch-generate recall_summary + recall_keywords for {n} ENGRAM nodes

        You are a one-shot batch summary generator dispatched as `engram-batch-summary-fairy`
        on a RETRY pass. The previous attempt produced output that failed validation for the
        nodes listed below. Regenerate ONLY those nodes, avoiding the cited errors.

        ## Previous attempt failed for these nodes

        {failure_block}

        ## Per-type guidance

        For each node, look up the guidance by the `type` field:

        {_TYPE_GUIDANCE_BLOCK}

        If the type is not listed, fall back to: {_DEFAULT_GUIDANCE_LINE}

        ## Summary rules

        - Single line, ≤120 characters (target). Hard cap ≤200.
        - Active voice. No hedging. No meta-language ("this node says...").
        - Keep technical terms verbatim if load-bearing.
        - No quotation marks unless part of the term.
        - Don't pad to fill the budget — shorter is fine for simple claims.

        ## Keyword rules

        {KEYWORD_GUIDANCE}

        - Output 3 to 5 keywords per node (NOT exactly 3, NOT exactly 5 — as many as carry distinct signal)
        - Each keyword ≤ 30 characters
        - DO NOT use ENGRAM node IDs (e.g. ob_1234, dv_0500) as keywords
        - DO NOT use generic words: node, claim, observation, derivation, agent, system

        ## Output format

        Emit EXACTLY this JSON shape (no other text, no markdown fences) for the {n} retried nodes:

        {{"items": [{{"node_id": "<id>", "recall_summary": "<summary>", "recall_keywords": ["kw1", "kw2", "kw3"]}}, ...]}}

        Self-check before output:
        - items array length must equal {n} (ONLY the failed nodes above — do not include others)
        - each recall_summary ≤200 chars
        - each recall_keywords array between 3 and 5 entries, no duplicates, each ≤30 chars
        - no ENGRAM node IDs in any keyword list

        ## Payload ({n} nodes — same content as original_payload above)

        ```json
        {payload_block}
        ```

        Emit the JSON now. One turn only. Do NOT call any tools.
    """)


# ---------------------------------------------------------------------------
# Subcommand: prepare
# ---------------------------------------------------------------------------


def cmd_prepare(args: argparse.Namespace) -> int:
    """Sample/chunk a cohort; write per-chunk payload + prompt files.

    Outputs to stdout a JSON manifest listing chunk directories.
    Exit code: 0.
    """
    # Resolve node IDs
    node_ids: list[str]
    if args.ids:
        node_ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    elif args.ids_file:
        ids_path = Path(args.ids_file)
        node_ids = [ln.strip() for ln in ids_path.read_text().splitlines() if ln.strip()]
    else:
        # Read from stdin
        node_ids = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]

    if not node_ids:
        print("error: no node IDs provided (use --ids, --ids-file, or stdin)", file=sys.stderr)
        return 1

    # Dedup while preserving order; warn so the caller knows input was noisy.
    deduped = list(dict.fromkeys(node_ids))
    if len(deduped) < len(node_ids):
        dupes = len(node_ids) - len(deduped)
        print(
            f"warning: {dupes} duplicate ID(s) removed from input list "
            f"({len(deduped)} unique IDs will be processed)",
            file=sys.stderr,
        )
    node_ids = deduped

    db_path = Path(args.db)
    nodes = _fetch_nodes(db_path, node_ids)

    if not nodes:
        print(f"error: none of the requested nodes found in {db_path}", file=sys.stderr)
        return 1

    missing = [nid for nid in node_ids if not any(n["id"] == nid for n in nodes)]
    if missing:
        print(f"warning: {len(missing)} node(s) not found in DB: {missing[:5]}", file=sys.stderr)

    chunk_size: int = args.chunk_size
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[list[dict]] = []
    for i in range(0, len(nodes), chunk_size):
        chunks.append(nodes[i : i + chunk_size])

    manifest_chunks = []
    for idx, chunk in enumerate(chunks):
        chunk_dir = out_dir / f"chunk-{idx}"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        payload = [_build_payload_entry(n) for n in chunk]
        prompt = _build_initial_prompt(payload)

        (chunk_dir / "payload.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )
        (chunk_dir / "prompt.md").write_text(prompt)

        manifest_chunks.append({
            "chunk_index": idx,
            "node_count": len(chunk),
            "node_ids": [n["id"] for n in chunk],
            "chunk_dir": str(chunk_dir),
            "payload_path": str(chunk_dir / "payload.json"),
            "prompt_path": str(chunk_dir / "prompt.md"),
        })

    manifest = {
        "total_nodes": len(nodes),
        "total_chunks": len(chunks),
        "chunk_size": chunk_size,
        "out_dir": str(out_dir),
        "chunks": manifest_chunks,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Print manifest to stdout so parent agent can read it
    print(json.dumps(manifest, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Subcommand: validate
# ---------------------------------------------------------------------------


def _extract_first_json_object(text: str) -> str:
    """Extract the first balanced {...} substring from text.

    Scans forward, respecting string literals (double-quoted strings with
    backslash escapes). Returns the substring from the first '{' through the
    matching '}'. Raises ValueError if no balanced object is found.

    This makes parsing robust to agents that emit a sentence before the JSON,
    e.g.: "Sure, here's the result:\n\n{...valid JSON...}\n\nLet me know..."
    """
    i = 0
    n = len(text)
    # Skip to the first '{'
    while i < n and text[i] != "{":
        i += 1
    if i >= n:
        raise ValueError("No '{' found in agent output")
    start = i
    depth = 0
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2  # skip escaped character
                continue
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    raise ValueError(f"Unbalanced braces in agent output (depth={depth} at end)")


def _parse_agent_output(raw: str) -> list[dict]:
    """Parse the fairy's raw output string into a list of item dicts.

    The fairy should emit {"items": [...]} as a raw JSON string.
    Handles bare JSON, JSON wrapped in a markdown code fence, and responses
    where the agent emits a preamble sentence before the JSON block.

    Uses a balanced-brace scanner to extract the first {...} object, which
    makes parsing robust to arbitrary preamble or postamble text.
    """
    text = raw.strip()
    json_str = _extract_first_json_object(text)
    data = json.loads(json_str)
    if isinstance(data, dict) and "items" in data:
        return data["items"]
    # Fallback: if it's already a list
    if isinstance(data, list):
        return data
    raise ValueError(f"Unexpected agent output shape: {list(data.keys()) if isinstance(data, dict) else type(data)}")


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate per-chunk agent output; write clean_items.json + failures.json.

    Reads chunk-N/agent_output.json files under --out.
    Exit code: 0 if no failures, 1 if failures require retry.
    """
    out_dir = Path(args.out)
    if not out_dir.exists():
        print(f"error: output directory not found: {out_dir}", file=sys.stderr)
        return 2

    clean_items: list[dict] = []
    failures: list[dict] = []

    # Discover chunks by reading manifest if present, else scanning dirs
    manifest_path = out_dir / "manifest.json"
    chunk_dirs: list[Path]
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        chunk_dirs = [Path(c["chunk_dir"]) for c in manifest["chunks"]]
        # Build a lookup: node_id → original payload entry
        _payload_lookup: dict[str, dict] = {}
        for c in manifest["chunks"]:
            payload_path = Path(c["payload_path"])
            if payload_path.exists():
                entries = json.loads(payload_path.read_text())
                for entry in entries:
                    _payload_lookup[entry["id"]] = entry
    else:
        # No manifest.json: ad-hoc invocation only. _payload_lookup will be
        # empty, so retry-prompt failures carry only {"id": node_id} with no
        # claim/type context. This is acceptable for ad-hoc use but means
        # retry prompts will lack original payload detail.
        print(
            "warning: manifest.json not found — _payload_lookup is empty; "
            "retry-prompt failure entries will lack claim/type context. "
            "This path is intended for ad-hoc invocations only.",
            file=sys.stderr,
        )
        chunk_dirs = sorted(out_dir.glob("chunk-*"), key=lambda p: int(p.name.split("-")[1]))
        _payload_lookup = {}

    if not chunk_dirs:
        print(f"error: no chunk directories found under {out_dir}", file=sys.stderr)
        return 2

    for chunk_dir in chunk_dirs:
        output_file = chunk_dir / "agent_output.json"
        if not output_file.exists():
            print(
                f"warning: agent_output.json missing for {chunk_dir.name} — "
                "treating all payload IDs as missing-output failures",
                file=sys.stderr,
            )
            # Collect IDs from this chunk's payload and record as failures so
            # the dream master sees them in the dream record (not silently dropped).
            payload_path = chunk_dir / "payload.json"
            if payload_path.exists():
                try:
                    missing_entries = json.loads(payload_path.read_text())
                    for entry in missing_entries:
                        failures.append({
                            "node_id": entry["id"],
                            "payload": entry,
                            "error": (
                                f"missing output: agent_output.json absent for "
                                f"{chunk_dir.name}"
                            ),
                        })
                except (json.JSONDecodeError, KeyError) as exc:
                    print(
                        f"warning: could not read payload for {chunk_dir.name}: {exc}",
                        file=sys.stderr,
                    )
            continue

        raw = output_file.read_text()
        try:
            items = _parse_agent_output(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"error: could not parse {output_file}: {exc}", file=sys.stderr)
            # All nodes in this chunk go to failures with a parse error
            payload_path = chunk_dir / "payload.json"
            if payload_path.exists():
                payload_entries = json.loads(payload_path.read_text())
                for entry in payload_entries:
                    failures.append({
                        "node_id": entry["id"],
                        "payload": entry,
                        "error": f"agent_output.json parse error: {exc}",
                    })
            continue

        # Validate each item
        for item in items:
            err = validate_summary_entry(item)
            if err is None:
                clean_items.append({
                    "node_id": item["node_id"],
                    "recall_summary": item["recall_summary"],
                    "recall_keywords": item["recall_keywords"],
                })
            else:
                node_id = item.get("node_id", "<unknown>")
                failures.append({
                    "node_id": node_id,
                    "payload": _payload_lookup.get(node_id, {"id": node_id}),
                    "error": err["error"],
                })

    # Write artifacts
    (out_dir / "clean_items.json").write_text(
        json.dumps(clean_items, indent=2, ensure_ascii=False)
    )
    (out_dir / "failures.json").write_text(
        json.dumps(failures, indent=2, ensure_ascii=False)
    )

    print(json.dumps({
        "clean": len(clean_items),
        "failures": len(failures),
        "clean_items_path": str(out_dir / "clean_items.json"),
        "failures_path": str(out_dir / "failures.json"),
    }, indent=2))

    # Write retry prompt if there are failures
    if failures:
        retry_prompt = _build_retry_prompt(failures)
        retry_prompt_path = out_dir / "retry_prompt.md"
        retry_prompt_path.write_text(retry_prompt)
        print(f"\nRetry needed: {len(failures)} nodes. Retry prompt: {retry_prompt_path}",
              file=sys.stderr)
        return 1

    # All items clean — write final_payload.json directly from clean_items.
    # The dream master unconditionally reads final_payload.json; produce it
    # here so the validate-exit-0 path (most common case) doesn't leave the
    # file absent.
    builder = PayloadBuilder()
    for item in clean_items:
        err = builder.add_summary(
            item["node_id"], item["recall_summary"], item["recall_keywords"]
        )
        if err is not None:
            # Shouldn't happen — items were already validated — but record if so.
            builder.add_failure(item["node_id"], f"re-validation rejected: {err['error']}")

    final_payload_path = out_dir / "final_payload.json"
    final_payload_path.write_text(builder.to_json())
    return 0


# ---------------------------------------------------------------------------
# Subcommand: incorporate-retry
# ---------------------------------------------------------------------------


def cmd_incorporate_retry(args: argparse.Namespace) -> int:
    """Merge retry output + prior failures into final_payload.json.

    Reads:
      --retry-output  path to the retry fairy's agent_output.json (or raw JSON)
      --out           directory containing failures.json + clean_items.json

    Writes:
      <out-dir>/final_payload.json  — engram_set_recall_summaries-ready payload

    Exit code: 0.
    """
    out_dir = Path(args.out)
    failures_path = out_dir / "failures.json"
    clean_items_path = out_dir / "clean_items.json"

    if not failures_path.exists():
        print(f"error: failures.json not found at {failures_path}", file=sys.stderr)
        return 2
    if not clean_items_path.exists():
        print(f"error: clean_items.json not found at {clean_items_path}", file=sys.stderr)
        return 2

    prior_failures: list[dict] = json.loads(failures_path.read_text())
    prior_clean: list[dict] = json.loads(clean_items_path.read_text())

    retry_output_path = Path(args.retry_output)
    if not retry_output_path.exists():
        print(f"error: retry output not found: {retry_output_path}", file=sys.stderr)
        return 2

    raw = retry_output_path.read_text()
    try:
        retry_items = _parse_agent_output(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: could not parse retry output: {exc}", file=sys.stderr)
        return 2

    # Index retry items by node_id
    retry_by_id: dict[str, dict] = {item.get("node_id", ""): item for item in retry_items}

    builder = PayloadBuilder()

    # Carry forward all previously clean items
    for item in prior_clean:
        err = builder.add_summary(
            item["node_id"], item["recall_summary"], item["recall_keywords"]
        )
        if err is not None:
            # Shouldn't happen — these were already validated — but record if so
            builder.add_failure(item["node_id"], f"re-validation rejected: {err['error']}")

    # For each prior failure, check if the retry fixed it
    for failure in prior_failures:
        node_id = failure["node_id"]
        if node_id in retry_by_id:
            retry_item = retry_by_id[node_id]
            err = validate_summary_entry(retry_item)
            if err is None:
                add_err = builder.add_summary(
                    retry_item["node_id"],
                    retry_item["recall_summary"],
                    retry_item["recall_keywords"],
                )
                if add_err is not None:
                    builder.add_failure(
                        node_id,
                        f"validation failed after one retry: {add_err['error']}",
                    )
            else:
                builder.add_failure(
                    node_id,
                    f"validation failed after one retry: {err['error']}",
                )
        else:
            # Retry fairy did not produce output for this node
            builder.add_failure(
                node_id,
                f"validation failed after one retry: no output produced by retry fairy",
            )

    final_payload = builder.to_json()
    final_payload_path = out_dir / "final_payload.json"
    final_payload_path.write_text(final_payload)

    payload_data = json.loads(final_payload)
    print(json.dumps({
        "summaries": len(payload_data.get("summaries", [])),
        "failures": len(payload_data.get("failures", [])),
        "final_payload_path": str(final_payload_path),
    }, indent=2))
    return 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- prepare ---
    p_prepare = sub.add_parser("prepare", help="Chunk a cohort into prompt + payload files")
    p_prepare.add_argument("--ids", help="Comma-separated node IDs")
    p_prepare.add_argument("--ids-file", help="Path to a file with one node ID per line")
    p_prepare.add_argument(
        "--out", required=True, help="Output directory for chunk-N/ subdirs"
    )
    p_prepare.add_argument(
        "--chunk-size", type=int, default=15, help="Max nodes per chunk (default 15)"
    )
    p_prepare.add_argument(
        "--db", default=str(DEFAULT_DB), help="Path to ENGRAM SQLite database"
    )

    # --- validate ---
    p_validate = sub.add_parser(
        "validate", help="Validate chunk agent outputs; write clean_items + failures"
    )
    p_validate.add_argument(
        "--out", required=True, help="Directory containing chunk-N/agent_output.json files"
    )

    # --- incorporate-retry ---
    p_retry = sub.add_parser(
        "incorporate-retry",
        help="Merge retry fairy output into final_payload.json",
    )
    p_retry.add_argument(
        "--retry-output",
        required=True,
        help="Path to retry fairy's agent_output.json",
    )
    p_retry.add_argument(
        "--out", required=True, help="Directory containing failures.json + clean_items.json"
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "prepare":
        return cmd_prepare(args)
    elif args.command == "validate":
        return cmd_validate(args)
    elif args.command == "incorporate-retry":
        return cmd_incorporate_retry(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
