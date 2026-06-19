#!/usr/bin/env python3
"""cohort_dispatch.py — Batch-summary cohort orchestration for the sleep cycle.

Five subcommands drive the mechanical pipeline for batch-summary cohort
orchestration:

  prepare         Sample/chunk a cohort into per-chunk payload files.
  verify-in       Pre-flight integrity checks on a cohort before dispatch.
  validate        Validate per-chunk agent output; split clean vs failures.
                  On exit 0 (no failures): auto-writes final_payload.json.
                  On exit 1 (failures): auto-writes retry_payload.json with
                  previous_error fields for the retry fairy.
  incorporate     Merge retry fairy output → final_payload.json.
                  When still_failing is non-empty: writes retry_payload.json
                  (same schema as validate's, for the next retry round).
                  When still_failing is empty: DELETES retry_payload.json so
                  the retry loop exits on [ -f retry_payload.json ].
                  Also writes unfixable.json + increments attempt_count.
  incorporate-retry  Alias for incorporate (backward compat).

Architecture rationale (Lei 2026-05-27):
  - Validation failures are retried by re-dispatching a fairy, NOT by
    mechanical script fixes. Quality of recall_summary / recall_keywords
    is critical because they surface in recall_surface and many node-rendering
    tools. One extra batch round is a reasonable price for quality.
  - This script does NOT modify LLM output. It validates, packages retry
    context, and packages the final payload. All content modification is
    done by the LLM (initial fairy or retry fairy).
  - Fairies receive a short dispatch prompt naming input/output file paths;
    they Read the payload file and Write the output file. No inline payload
    embedding in the prompt.

Empirical basis: N=15 worst-case batch at 37K tokens / 1 sub-agent turn
produces summaries with cosine similarity within in-sample variance of the
serial-pattern ground truth (mean Δ cosine = +0.043, pooled stdev = 0.118).
Token reduction vs serial: ~95% for a 50-node / 4-chunk cohort.

Usage:
    python -m tools.cohort_dispatch prepare --ids <comma-sep-ids> --out <dir>
    python -m tools.cohort_dispatch prepare --ids-file <path> --out <dir> [--chunk-size 15]
    python -m tools.cohort_dispatch verify-in --out <dir>
    python -m tools.cohort_dispatch validate --out <dir>
    python -m tools.cohort_dispatch incorporate --retry-output <path> --out <dir>
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
# (recall_summary_prompts TYPE_GUIDANCE/KEYWORD_GUIDANCE no longer needed in
# dispatch prompts — guidance now lives exclusively in the fairy's agent spec)

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


def _build_initial_prompt(payload_path: str, output_path: str) -> str:
    """Build the short dispatch prompt for a fresh batch-summary-fairy.

    The prompt names the input payload file to Read and the output file to
    Write. Per-type guidance, summary rules, keyword rules, and output schema
    live in the fairy's agent spec (engram-batch-summary-fairy.md) — this
    prompt does NOT duplicate them.

    Size: ~200 tokens regardless of cohort/payload size (no inline payload).
    """
    return textwrap.dedent(f"""\
        You are dispatched as `engram-batch-summary-fairy`.

        1. Read your input payload from: {payload_path}
        2. Follow all rules in your agent spec (engram-batch-summary-fairy.md) to generate
           recall_summary + recall_keywords for each node in the payload.
        3. Write your output JSON to: {output_path}

        The output must be a single JSON object: {{"items": [...]}}
        Write it to the output path above. Return a brief confirmation naming
        both paths when done.
    """)


def _build_retry_prompt(
    retry_payload_path: str,
    output_path: str,
    failures: list[dict],
) -> str:
    """Build the short dispatch prompt for a retry batch-summary-fairy.

    The prompt names the retry payload file to Read (contains only the failed
    nodes) and the output file to Write. Per-type guidance + rules live in the
    fairy's agent spec. The failure context (node IDs + errors) is included
    inline so the fairy knows what to avoid.

    Each entry in `failures` has: node_id, error (str), previous_error (str, optional).
    Size: short stub + failure list, still much smaller than the old inline-payload prompt.
    """
    n = len(failures)
    failure_lines = []
    for f in failures:
        error_text = f.get("previous_error") or f.get("error", "unknown error")
        failure_lines.append(
            f"- node_id: {f['node_id']}\n"
            f"  previous_error: {error_text}"
        )
    failure_block = "\n\n".join(failure_lines)

    return textwrap.dedent(f"""\
        You are dispatched as `engram-batch-summary-fairy` on a RETRY pass.

        The previous attempt produced output that failed validation for {n} node(s).
        Regenerate ONLY those nodes, avoiding the cited errors.

        ## Previous attempt failures

        {failure_block}

        ## Instructions

        1. Read the retry payload from: {retry_payload_path}
           (Contains only the failed nodes, each with a `previous_error` field showing what to avoid.)
        2. Follow all rules in your agent spec (engram-batch-summary-fairy.md) to regenerate
           recall_summary + recall_keywords for each node, avoiding the previous errors.
        3. Write your output JSON to: {output_path}

        The output must be a single JSON object: {{"items": [...]}} with only the {n} retried entries.
        Write it to the output path above. Return a brief confirmation naming both paths when done.
    """)


# ---------------------------------------------------------------------------
# Subcommand: prepare
# ---------------------------------------------------------------------------


def cmd_prepare(args: argparse.Namespace) -> int:
    """Sample/chunk a cohort; write per-chunk payload files.

    Outputs to stdout a JSON manifest listing chunk directories.
    No prompt.md is written — the dispatcher constructs the short prompt inline
    at dispatch time using paths from the manifest.
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

        (chunk_dir / "payload.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False)
        )

        manifest_chunks.append({
            "chunk_index": idx,
            "node_count": len(chunk),
            "node_ids": [n["id"] for n in chunk],
            "chunk_dir": str(chunk_dir),
            "payload_path": str(chunk_dir / "payload.json"),
        })

    manifest = {
        "total_nodes": len(nodes),
        "total_chunks": len(chunks),
        "chunk_size": chunk_size,
        "out_dir": str(out_dir),
        "attempt_count": 0,
        "chunks": manifest_chunks,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Print manifest to stdout so parent agent can read it
    print(json.dumps(manifest, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Subcommand: verify-in
# ---------------------------------------------------------------------------

_VERIFY_REQUIRED_FIELDS = ("id", "type", "claim")
_VERIFY_MAX_CHUNK_SIZE = 15


def cmd_verify_in(args: argparse.Namespace) -> int:
    """Pre-flight integrity checks on a cohort before dispatch.

    For each chunk's payload.json:
      1. Valid JSON.
      2. Every entry has required fields: id (str), type (str), claim (str).
      3. Every id resolves to a current DB row whose claim matches the payload
         (race-window check — guards against node retraction between cohort
         selection and dispatch).
      4. No duplicate IDs within a chunk OR across chunks.
      5. Chunk size <= 15.

    Exit codes:
      0 — all checks pass
      1 — one or more checks failed (structured JSON error to stdout)
      2 — missing cohort dir or manifest

    Known limitations:
        - Whitespace normalization: only strips leading/trailing whitespace.
          A node's claim re-saved with internal whitespace normalization
          (collapsed newlines/double-spaces) fires a false-positive
          integrity-check failure. Operator regenerates the cohort to recover.
        - Race window: integrity check is at verify-in time; fairy dispatch
          fires later. A retraction in the verify-in → dispatch window is
          still possible. Window is narrower than pre-verify-in but non-zero.
    """
    out_dir = Path(args.out)
    if not out_dir.exists():
        print(f"error: output directory not found: {out_dir}", file=sys.stderr)
        return 2

    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"error: manifest.json not found at {manifest_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text())
    db_path = Path(args.db)

    errors: list[dict] = []
    all_ids_seen: dict[str, str] = {}  # id -> "chunk-N" where first seen

    for chunk_meta in manifest["chunks"]:
        chunk_idx = chunk_meta["chunk_index"]
        chunk_label = f"chunk-{chunk_idx}"
        payload_path = Path(chunk_meta["payload_path"])

        # Check 1: valid JSON
        if not payload_path.exists():
            errors.append({
                "chunk": chunk_label,
                "check": "json_valid",
                "error": f"payload.json not found at {payload_path}",
            })
            continue

        try:
            entries = json.loads(payload_path.read_text())
        except json.JSONDecodeError as exc:
            errors.append({
                "chunk": chunk_label,
                "check": "json_valid",
                "error": f"payload.json is not valid JSON: {exc}",
            })
            continue

        if not isinstance(entries, list):
            errors.append({
                "chunk": chunk_label,
                "check": "json_valid",
                "error": f"payload.json must be a JSON array, got {type(entries).__name__}",
            })
            continue

        # Check 5: chunk size
        if len(entries) > _VERIFY_MAX_CHUNK_SIZE:
            errors.append({
                "chunk": chunk_label,
                "check": "chunk_size",
                "error": (
                    f"chunk has {len(entries)} entries, exceeds max "
                    f"{_VERIFY_MAX_CHUNK_SIZE}"
                ),
                "got": len(entries),
                "max": _VERIFY_MAX_CHUNK_SIZE,
            })

        for entry_idx, entry in enumerate(entries):
            entry_label = f"{chunk_label}/entry[{entry_idx}]"

            # Check 2: required fields
            for field in _VERIFY_REQUIRED_FIELDS:
                if field not in entry or not isinstance(entry[field], str) or not entry[field].strip():
                    errors.append({
                        "chunk": chunk_label,
                        "check": "required_fields",
                        "error": (
                            f"{entry_label} missing or empty required field: '{field}'"
                        ),
                        "field": field,
                        "entry_index": entry_idx,
                    })
                    break

            else:
                node_id = entry["id"]

                # Check 4: no duplicate IDs across chunks
                if node_id in all_ids_seen:
                    errors.append({
                        "chunk": chunk_label,
                        "check": "no_duplicates",
                        "error": (
                            f"duplicate id '{node_id}' in {chunk_label} also seen in "
                            f"{all_ids_seen[node_id]}"
                        ),
                        "node_id": node_id,
                        "first_seen_in": all_ids_seen[node_id],
                    })
                else:
                    all_ids_seen[node_id] = chunk_label

    # Check 4b: intra-chunk duplicates — targeted per-chunk pass to complement the
    # cross-chunk pass above (which catches the first occurrence in a different chunk
    # but cannot flag duplicates that appear twice within the same chunk).
    for chunk_meta in manifest["chunks"]:
        chunk_idx = chunk_meta["chunk_index"]
        chunk_label = f"chunk-{chunk_idx}"
        payload_path = Path(chunk_meta["payload_path"])
        if not payload_path.exists():
            continue
        try:
            entries = json.loads(payload_path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(entries, list):
            continue
        seen_in_chunk: set[str] = set()
        for entry_idx, entry in enumerate(entries):
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            nid = entry["id"]
            if nid in seen_in_chunk:
                # Only add if not already reported from the cross-chunk pass
                already_reported = any(
                    e.get("check") == "no_duplicates" and e.get("node_id") == nid
                    for e in errors
                )
                if not already_reported:
                    errors.append({
                        "chunk": chunk_label,
                        "check": "no_duplicates",
                        "error": (
                            f"duplicate id '{nid}' appears more than once within {chunk_label}"
                        ),
                        "node_id": nid,
                    })
            else:
                seen_in_chunk.add(nid)

    # Check 3: content integrity — every id resolves to a DB row with matching claim
    if all_ids_seen and not db_path.exists():
        print(
            f"warning: DB path {db_path} not found — content-integrity check skipped",
            file=sys.stderr,
        )
    if all_ids_seen and db_path.exists():
        id_list = list(all_ids_seen.keys())
        try:
            db_nodes = _fetch_nodes(db_path, id_list)
        except FileNotFoundError as exc:
            errors.append({
                "chunk": "all",
                "check": "db_access",
                "error": str(exc),
            })
            db_nodes = []

        db_by_id = {n["id"]: n for n in db_nodes}

        # Build payload claim lookup: node_id -> (chunk, claim)
        payload_claims: dict[str, tuple[str, str]] = {}
        for chunk_meta in manifest["chunks"]:
            chunk_idx = chunk_meta["chunk_index"]
            chunk_label = f"chunk-{chunk_idx}"
            payload_path = Path(chunk_meta["payload_path"])
            if not payload_path.exists():
                continue
            try:
                entries = json.loads(payload_path.read_text())
            except json.JSONDecodeError:
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and "id" in entry and "claim" in entry:
                    payload_claims[entry["id"]] = (chunk_label, entry.get("claim", ""))

        for node_id, (chunk_label, payload_claim) in payload_claims.items():
            if node_id not in db_by_id:
                errors.append({
                    "chunk": chunk_label,
                    "check": "content_integrity",
                    "error": f"id '{node_id}' not found in DB — may have been retracted",
                    "node_id": node_id,
                })
            else:
                db_claim = db_by_id[node_id].get("claim") or ""
                if db_claim.strip() != payload_claim.strip():
                    errors.append({
                        "chunk": chunk_label,
                        "check": "content_integrity",
                        "error": (
                            f"claim mismatch for '{node_id}': payload claim does not "
                            f"match DB claim (race-window: node may have been updated)"
                        ),
                        "node_id": node_id,
                        "payload_claim_prefix": payload_claim[:80],
                        "db_claim_prefix": db_claim[:80],
                    })

    if errors:
        result = {"passed": False, "error_count": len(errors), "errors": errors}
        print(json.dumps(result, indent=2))
        return 1

    result = {"passed": True, "error_count": 0, "errors": []}
    print(json.dumps(result, indent=2))
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

    # Write retry_payload.json if there are failures (retryable items)
    # retry_payload.json is in the same schema as input payload.json so it can
    # be fed straight to a re-dispatched fairy. Each item gets a previous_error
    # field so the retry fairy knows what to avoid.
    if failures:
        retry_payload = []
        for f in failures:
            payload_entry = dict(f.get("payload", {"id": f["node_id"]}))
            payload_entry["previous_error"] = f["error"]
            retry_payload.append(payload_entry)
        retry_payload_path = out_dir / "retry_payload.json"
        retry_payload_path.write_text(
            json.dumps(retry_payload, indent=2, ensure_ascii=False)
        )
        print(f"\nRetry needed: {len(failures)} nodes. retry_payload.json: {retry_payload_path}",
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
    """Merge retry output + prior failures into final artifacts.

    Reads:
      --retry-output  path to the retry fairy's agent_output.json (or raw JSON)
      --out           directory containing failures.json + clean_items.json

    Writes:
      <out-dir>/final_payload.json   — engram_set_recall_summaries-ready payload
      <out-dir>/retry_payload.json   — items still failing (structurally retryable);
                                       same schema as input payload.json + previous_error
      <out-dir>/unfixable.json       — items that failed in structurally non-retryable ways
                                       (fairy invented IDs, malformed output beyond recovery)
    Updates:
      <out-dir>/clean_items.json     — CUMULATIVE clean set: prior_clean ∪ this round's
                                       newly-passing items (monotonic; len only ever grows)
      <out-dir>/failures.json        — SHRUNKEN failure pool: only this round's still-failing
                                       items, in cmd_validate's failures schema. The next
                                       retry round reads this as its prior_failures.
      <out-dir>/manifest.json        — increments attempt_count

    Cumulative-retry invariant (#1215): `cmd_validate` writes the round-0 baseline
    (clean_items.json + failures.json); `incorporate` OWNS the per-round accumulation.
    Each round persists the grown clean set and the shrunken failure pool back to those
    same files, so a multi-round retry reads the *updated* clean set + *shrunken* failures
    — never the round-0 snapshot. Without this, round N re-read round-0's clean/failures
    and silently dropped every intermediate round's fixes (clean count could regress).

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

    # Build a set of known node IDs from the prior failures so we can detect
    # invented IDs in the retry output.
    known_ids: set[str] = {f["node_id"] for f in prior_failures}

    try:
        retry_items = _parse_agent_output(raw)
        parse_failed = False
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"error: could not parse retry output: {exc}", file=sys.stderr)
        parse_failed = True
        retry_items = []

    # Index retry items by node_id; detect invented IDs (unfixable)
    retry_by_id: dict[str, dict] = {}
    unfixable_items: list[dict] = []

    if parse_failed:
        # Entire output is malformed — all prior failures go to unfixable
        for f in prior_failures:
            unfixable_items.append({
                "node_id": f["node_id"],
                "reason": "retry output JSON malformed beyond per-item recovery",
            })
    else:
        for item in retry_items:
            nid = item.get("node_id", "")
            if not nid or nid not in known_ids:
                # Fairy invented an ID or returned an empty node_id — unfixable
                unfixable_items.append({
                    "node_id": nid or "<empty>",
                    "reason": (
                        f"fairy produced output for unknown node_id {nid!r} "
                        "(not in retry payload — invented ID)"
                    ),
                })
            else:
                retry_by_id[nid] = item

    builder = PayloadBuilder()
    still_failing: list[dict] = []  # retryable (validator error, not structural) — retry_payload schema
    newly_clean: list[dict] = []    # items this round fixed — clean_items schema (cumulative append)
    next_round_failures: list[dict] = []  # shrunken failure pool — failures.json schema for next round

    def _record_still_failing(failure: dict, node_id: str, err_text: str) -> None:
        """Append an item that is still validator-fixable to BOTH the retry_payload
        list (still_failing, payload-schema + previous_error) and the next-round
        failures pool (next_round_failures, cmd_validate's {node_id, payload, error}
        schema). Keeping the two in lockstep is what makes the failure pool shrink
        monotonically across rounds."""
        payload = failure.get("payload", {"id": node_id})
        still_failing.append({**payload, "previous_error": err_text})
        next_round_failures.append({
            "node_id": node_id,
            "payload": payload,
            "error": err_text,
        })

    # Carry forward all previously clean items
    prior_clean_revalidation_failed: set[str] = set()
    for item in prior_clean:
        err = builder.add_summary(
            item["node_id"], item["recall_summary"], item["recall_keywords"]
        )
        if err is not None:
            # Shouldn't happen — these were already validated — but record if so.
            # Exclude from the cumulative-clean write below: re-asserting an item
            # the validator now rejects would re-add it every round (it can never
            # resolve), so it must NOT count as "clean" going forward. Also surface
            # it in unfixable.json — a re-validation rejection is terminal (it lands
            # in neither the active pool nor a retry), so without this it would be
            # operator-invisible (only a builder failure in final_payload). Recording
            # it keeps the diagnostic trail complete.
            builder.add_failure(item["node_id"], f"re-validation rejected: {err['error']}")
            prior_clean_revalidation_failed.add(item["node_id"])
            unfixable_items.append({
                "node_id": item["node_id"],
                "reason": f"re-validation rejected: {err['error']}",
            })

    # For each prior failure, check if the retry fixed it
    for failure in prior_failures:
        node_id = failure["node_id"]

        # Skip if already classified as unfixable (structural parse failure)
        if any(u["node_id"] == node_id for u in unfixable_items):
            builder.add_failure(node_id, "retry output malformed beyond recovery")
            continue

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
                    # Still validator-fixable — goes to retry_payload for next round
                    _record_still_failing(failure, node_id, add_err["error"])
                else:
                    # Fixed this round — join the cumulative clean set
                    newly_clean.append({
                        "node_id": retry_item["node_id"],
                        "recall_summary": retry_item["recall_summary"],
                        "recall_keywords": retry_item["recall_keywords"],
                    })
            else:
                builder.add_failure(
                    node_id,
                    f"validation failed after one retry: {err['error']}",
                )
                # Still validator-fixable — goes to retry_payload for next round
                _record_still_failing(failure, node_id, err["error"])
        else:
            # Retry fairy did not produce output for this node
            builder.add_failure(
                node_id,
                "validation failed after one retry: no output produced by retry fairy",
            )
            # No output = potentially recoverable (fairy may have skipped it) — retryable
            _record_still_failing(failure, node_id, "no output produced by retry fairy")

    final_payload = builder.to_json()
    final_payload_path = out_dir / "final_payload.json"
    final_payload_path.write_text(final_payload)

    # Persist the CUMULATIVE clean set (#1215). prior_clean already holds every
    # round's accumulated fixes; append this round's newly-passing items. Dedup by
    # node_id (last-wins) defensively — prior_clean and newly_clean are disjoint by
    # construction, but a dup would otherwise double-apply downstream. len(cumulative)
    # is monotonic non-decreasing: it only ever gains newly_clean, never loses prior.
    cumulative_clean: list[dict] = []
    _seen_clean: set[str] = set()
    for item in (*prior_clean, *newly_clean):
        nid = item.get("node_id")
        # Drop any prior-clean item the validator just rejected on re-check — it is
        # no longer clean (recorded as a builder failure above), so it must not be
        # re-asserted into the cumulative clean set (would loop forever otherwise).
        if nid in prior_clean_revalidation_failed:
            continue
        if nid in _seen_clean:
            # Replace the earlier entry with the later (newly_clean) one.
            cumulative_clean = [c for c in cumulative_clean if c.get("node_id") != nid]
        else:
            _seen_clean.add(nid)
        cumulative_clean.append(item)
    (out_dir / "clean_items.json").write_text(
        json.dumps(cumulative_clean, indent=2, ensure_ascii=False)
    )

    # Persist the SHRUNKEN failure pool (#1215) — only this round's still-failing
    # items, in cmd_validate's failures schema. The next retry round reads THIS as
    # its prior_failures, so it never re-processes already-fixed items.
    (out_dir / "failures.json").write_text(
        json.dumps(next_round_failures, indent=2, ensure_ascii=False)
    )

    # Write or delete retry_payload.json.
    # File-presence semantic: retry_payload.json exists ⟺ items left to retry.
    # The retry loop (engram-sleep skill) uses [ -f retry_payload.json ] as its exit condition;
    # writing an empty list would cause the loop to run to MAX_RETRIES unnecessarily.
    retry_payload_path = out_dir / "retry_payload.json"
    if still_failing:
        retry_payload_path.write_text(json.dumps(still_failing, indent=2, ensure_ascii=False))
    else:
        retry_payload_path.unlink(missing_ok=True)

    # Write unfixable.json CUMULATIVELY (#1215). Unfixable items are terminal — once
    # an item is deemed unfixable it drops out of the active failure pool, so if this
    # file were overwritten per-round, an earlier round's unfixable items would be lost
    # (no longer in failures.json to re-surface). Merge with any prior unfixable,
    # dedup by node_id (first-wins keeps the original diagnosis).
    unfixable_path = out_dir / "unfixable.json"
    prior_unfixable: list[dict] = []
    if unfixable_path.exists():
        try:
            prior_unfixable = json.loads(unfixable_path.read_text())
        except (json.JSONDecodeError, ValueError):
            prior_unfixable = []
    unfixable_this_round = len(unfixable_items)  # before merge with prior rounds
    cumulative_unfixable: list[dict] = []
    _seen_unfixable: set[str] = set()
    for item in (*prior_unfixable, *unfixable_items):
        nid = item.get("node_id")
        if nid in _seen_unfixable:
            continue
        _seen_unfixable.add(nid)
        cumulative_unfixable.append(item)
    unfixable_items = cumulative_unfixable
    unfixable_path.write_text(json.dumps(unfixable_items, indent=2, ensure_ascii=False))

    # Increment attempt_count in manifest.json
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            manifest["attempt_count"] = manifest.get("attempt_count", 0) + 1
            manifest_path.write_text(json.dumps(manifest, indent=2))
        except (json.JSONDecodeError, KeyError):
            pass  # manifest update is best-effort

    payload_data = json.loads(final_payload)
    print(json.dumps({
        "summaries": len(payload_data.get("summaries", [])),
        "failures": len(payload_data.get("failures", [])),
        "still_retryable": len(still_failing),
        "unfixable": len(unfixable_items),  # cumulative across all rounds so far
        "unfixable_this_round": unfixable_this_round,  # newly detected this round only
        "final_payload_path": str(final_payload_path),
        "retry_payload_path": str(retry_payload_path) if retry_payload_path.exists() else None,
        "unfixable_path": str(unfixable_path),
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
    p_prepare = sub.add_parser("prepare", help="Chunk a cohort into payload files")
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

    # --- verify-in ---
    p_verify = sub.add_parser(
        "verify-in",
        help="Pre-flight integrity checks on a cohort before dispatch",
    )
    p_verify.add_argument(
        "--out", required=True, help="Directory containing manifest.json + chunk-N/payload.json"
    )
    p_verify.add_argument(
        "--db", default=str(DEFAULT_DB), help="Path to ENGRAM SQLite database"
    )

    # --- validate ---
    p_validate = sub.add_parser(
        "validate", help="Validate chunk agent outputs; write clean_items + failures"
    )
    p_validate.add_argument(
        "--out", required=True, help="Directory containing chunk-N/agent_output.json files"
    )

    # --- incorporate / incorporate-retry (alias) ---
    for cmd_name, cmd_help in [
        ("incorporate", "Merge retry fairy output; auto-build retry_payload + unfixable artifacts"),
        ("incorporate-retry", "Alias for incorporate (backward compat)"),
    ]:
        p_retry = sub.add_parser(cmd_name, help=cmd_help)
        p_retry.add_argument(
            "--retry-output",
            required=True,
            help="Path to retry fairy's agent_output.json",
        )
        p_retry.add_argument(
            "--out", required=True,
            help="Directory containing failures.json + clean_items.json",
        )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "prepare":
        return cmd_prepare(args)
    elif args.command == "verify-in":
        return cmd_verify_in(args)
    elif args.command == "validate":
        return cmd_validate(args)
    elif args.command in ("incorporate", "incorporate-retry"):
        return cmd_incorporate_retry(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
