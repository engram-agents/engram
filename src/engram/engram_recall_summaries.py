"""engram_recall_summaries — recall-summary write (family I) impl for the ENGRAM MCP server.

Extracted from server.py in #872 wave 2.

HOUSE RULES (mirror engram_core.py § HOUSE RULES):
- Access shared state ONLY via ``import engram_core as core; core.NAME`` — never
  via ``from engram_core import NAME``.
- This module must not import server.py (acyclic: server → family → core).
- No module-level mutable assignments — all state lives in engram_core.
"""

import json

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

    if not isinstance(payload, dict) or not isinstance(payload.get("summaries"), list):
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
                    conn.execute(
                        "UPDATE nodes SET recall_summary = ?, recall_keywords = ? WHERE id = ?",
                        (recall_summary, json.dumps(recall_keywords), node_id),
                    )
                    ok.append({"node_id": node_id})
                except Exception as exc:  # noqa: BLE001
                    errors.append({"node_id": node_id, "error": str(exc)})
    finally:
        conn.close()

    return json.dumps({
        "ok": ok,
        "errors": errors,
        "applied": len(ok),
        "failed": len(errors),
        "pre_flight_failures": pre_flight_failures,
    })
