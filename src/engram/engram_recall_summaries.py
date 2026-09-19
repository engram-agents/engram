"""engram_recall_summaries — recall-summary write (family I) impl for the ENGRAM MCP server.

Extracted from server.py in #872 wave 2.

HOUSE RULES (mirror engram_core.py § HOUSE RULES):
- Access shared state ONLY via ``import engram_core as core; core.NAME`` — never
  via ``from engram_core import NAME``.
- This module must not import server.py (acyclic: server → family → core).
- No module-level mutable assignments — all state lives in engram_core.
"""

import json
import os

import engram_core as core
from tools.recall_summary_validator import validate_summary_entry


# ---------------------------------------------------------------------------
# Recall summary write tool (batch)
# ---------------------------------------------------------------------------
# Constants and per-entry validation live in tools/recall_summary_validator.py
# (single source of truth).


# DESIGN INTENT — engram_set_recall_summaries
# -------------------------------------------
# Batch-write recall_summary + recall_keywords across multiple nodes. These
# are the agent-curated short-form representations that drive the recall
# surfaces (auto-surface lossy hints + tier-1/tier-2 query result formatting).
# Quality of recall_summary directly determines whether the right nodes
# surface when the agent needs them.
#
# Sleep-cycle workflow: the engram-sleep skill's batch-summary-fairy dispatches
# generate recall_summary/keywords for the day's cohort, then the dream-master
# applies via this batch tool. Bulk-backfill workflow: when an agent first
# enables recall_summary curation, this tool seeds the whole graph at once.
#
# Best-effort semantics (no all-or-nothing): per-item validation; valid
# entries apply, invalid entries return per-item errors. Doesn't roll back
# the whole batch on one bad row.
#
# Idempotent: re-writing the same node with the same summary is a no-op.
# Constants + per-entry validation live in tools/recall_summary_validator.py
# (single source of truth — used both here and by the batch-summary-fairy).
#
# File-based input (payload_json["summaries_file"]): an alternative to inline
# "summaries" for large batches (the sleep cycle's cohort-dispatch workflow
# writes its output to final_payload.json on disk — with the cohort cap
# removed this can run 40KB+, and re-typing that verbatim through the model
# to pass it inline is pure transcription waste with a silent failure mode).
# When "summaries_file" is given, this reads that path, parses it as JSON,
# and expects the SAME {"summaries": [...], "failures": [...]} shape that
# tools/recall_summary_payload.PayloadBuilder.to_json() emits (also what
# cohort_dispatch's "validate" subcommand writes to final_payload.json) —
# then proceeds down the identical apply path as the inline case. The path
# MUST be absolute: the MCP server is a long-lived process whose working
# directory is fixed at launch and is not guaranteed to track the calling
# agent's own (mutable, per-call) cwd — a relative path could silently
# resolve to an unrelated file. "summaries" and "summaries_file" together is
# a loud, ambiguous-input error; neither present is the existing
# missing-"summaries" error.
def _set_recall_summaries_impl(payload_json: str) -> str:
    """Impl for engram_set_recall_summaries — takes raw payload_json string."""
    # -- Parse payload --
    try:
        payload = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return json.dumps({
            "error": "invalid JSON in payload_json",
            "got": str(payload_json)[:100],
        })

    if not isinstance(payload, dict):
        return json.dumps({"error": "payload_json must have 'summaries' as a list"})

    has_inline = "summaries" in payload
    has_file = "summaries_file" in payload

    if has_inline and has_file:
        return json.dumps({
            "error": (
                "payload_json must not include both 'summaries' and "
                "'summaries_file' — ambiguous input, applying nothing"
            ),
        })

    summaries_file_path = None

    if has_file:
        raw_path = payload["summaries_file"]
        if not isinstance(raw_path, str) or not raw_path:
            return json.dumps({
                "error": "'summaries_file' must be a non-empty string path",
                "got": raw_path,
            })
        if not os.path.isabs(raw_path):
            return json.dumps({
                "error": (
                    "'summaries_file' must be an absolute path — the MCP server "
                    "runs as its own long-lived process whose working directory "
                    "is not guaranteed to match the calling agent's cwd, so a "
                    "relative path could silently resolve to the wrong file"
                ),
                "got": raw_path,
            })
        if not os.path.exists(raw_path):
            return json.dumps({"error": f"summaries_file not found: {raw_path}"})
        if not os.path.isfile(raw_path):
            return json.dumps({"error": f"summaries_file is not a regular file: {raw_path}"})
        try:
            with open(raw_path, "r", encoding="utf-8") as fh:
                file_text = fh.read()
        except PermissionError as exc:
            return json.dumps({"error": f"summaries_file is not readable: {raw_path} ({exc})"})
        except OSError as exc:
            return json.dumps({"error": f"could not read summaries_file {raw_path}: {exc}"})
        try:
            file_payload = json.loads(file_text)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"summaries_file is not valid JSON: {raw_path} ({exc})"})
        if not isinstance(file_payload, dict) or not isinstance(file_payload.get("summaries"), list):
            return json.dumps({
                "error": (
                    f"summaries_file must be a JSON object with 'summaries' as a "
                    f"list (the shape cohort_dispatch validate / PayloadBuilder "
                    f"emit): {raw_path}"
                ),
            })
        payload = file_payload
        summaries_file_path = raw_path
    elif not has_inline:
        return json.dumps({"error": "payload_json must have 'summaries' as a list"})

    if not isinstance(payload.get("summaries"), list):
        return json.dumps({"error": "payload_json must have 'summaries' as a list"})

    summaries = payload["summaries"]
    pre_flight_failures = payload.get("failures") or []

    # -- Validate all entries, partition into valid / error --
    valid_entries: list[dict] = []
    errors: list[dict] = []

    for entry in summaries:
        err = validate_summary_entry(entry)
        if err is not None:
            errors.append({"node_id": entry.get("node_id") if isinstance(entry, dict) else None,
                            **err})
        else:
            valid_entries.append(entry)

    # -- Apply valid entries in a single transaction --
    ok: list[dict] = []
    conn = core._get_db()
    try:
        with conn:
            for entry in valid_entries:
                node_id = entry["node_id"].strip()
                recall_summary = entry["recall_summary"]
                recall_keywords = entry["recall_keywords"]
                try:
                    row = conn.execute(
                        "SELECT id, is_current FROM nodes WHERE id = ?",
                        (node_id,),
                    ).fetchone()
                    if row is None:
                        errors.append({
                            "node_id": node_id,
                            "error": "node not found",
                        })
                        continue
                    if row["is_current"] != 1:
                        errors.append({
                            "node_id": node_id,
                            "error": "node is not current (superseded)",
                        })
                        continue
                    # Clear needs_tightening/summary_missing on successful apply
                    # (#1952 PR3): both flags exist to queue a node into the
                    # batch-summary fairy's reviewer-of-outliers cohort
                    # (engram-sleep SKILL.md Step 5). Before PR3 the cohort
                    # query never consulted needs_tightening, so a stuck flag
                    # was inert; now that it does, an un-cleared flag would
                    # re-select an already-fixed node forever, defeating the
                    # "near-zero most nights" design goal. Treat a
                    # successfully-applied summary as fully reviewed
                    # regardless of its exact length (this tool's own
                    # 200-char hard cap already bounds it) — the 140-char
                    # write-time target/280 hard-reject gate is a distinct,
                    # deliberately-not-unified regime (see engram_core.py's
                    # RECALL_SUMMARY_TARGET comment) that this path does not
                    # re-derive.
                    conn.execute(
                        "UPDATE nodes SET recall_summary = ?, recall_keywords = ?, "
                        "needs_tightening = 0, summary_missing = 0 WHERE id = ?",
                        (recall_summary, json.dumps(recall_keywords), node_id),
                    )
                    ok.append({"node_id": node_id})
                except Exception as exc:  # noqa: BLE001
                    errors.append({"node_id": node_id, "error": str(exc)})
    finally:
        conn.close()

    result = {
        "ok": ok,
        "errors": errors,
        "applied": len(ok),
        "failed": len(errors),
        "pre_flight_failures": pre_flight_failures,
    }
    # File-based responses always echo the resolved path, even when applied=0 —
    # so a genuinely-empty final_payload.json ("nothing to summarize tonight")
    # is distinguishable in the response from a misread/empty file, and never
    # silently indistinguishable from an inline no-op call (which carries no
    # such field, unchanged from before this feature).
    if summaries_file_path is not None:
        result["summaries_file"] = summaries_file_path
    return json.dumps(result)
