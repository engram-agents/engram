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
from datetime import datetime, timezone

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
# Idempotent at the curated-field level: re-writing the same node leaves the
# same summary/keywords, but still performs a strict embedding refresh so a
# same-input replay can repair stale vector data.
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

_EMBEDDING_REFRESH_PENDING = "embedding_refresh_pending"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _vec_store_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vec_nodes'"
    ).fetchone() is not None


def _set_embedding_refresh_marker(conn, node_id: str, metadata_raw, *, reason: str) -> str | None:
    """Persist strict-refresh debt while preserving unrelated metadata keys."""
    metadata, warning = core._metadata_dict_for_write(
        metadata_raw, node_id, "engram_set_recall_summaries"
    )
    metadata[_EMBEDDING_REFRESH_PENDING] = {
        "reason": reason, "stage": "strict_refresh", "at": _utc_now(),
    }
    if conn.execute(
        "UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(metadata), node_id)
    ).rowcount != 1:
        raise RuntimeError("could not persist embedding refresh debt marker")
    return warning


def _clear_embedding_refresh_marker(conn, node_id: str, metadata_raw) -> str | None:
    """The sole debt-clearer: a verified strict refresh of this one node."""
    metadata, warning = core._metadata_dict_for_write(
        metadata_raw, node_id, "engram_set_recall_summaries"
    )
    metadata.pop(_EMBEDDING_REFRESH_PENDING, None)
    if conn.execute(
        "UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(metadata), node_id)
    ).rowcount != 1:
        raise RuntimeError("could not clear embedding refresh debt marker")
    return warning


def _pending_reason(outcome) -> str:
    if outcome.status == "skipped":
        return "unavailable"
    if outcome.stage in {"text", "embed", "store"}:
        return outcome.stage
    return "embedding"


def _strict_refresh_embedding(conn, node_id: str, metadata_raw) -> tuple[dict, str | None]:
    """Refresh and verify one full recipe; restore both vector stores on failure.

    This is intentionally the single per-node marker-clearing path which a
    later explicit repair operation must reuse.
    """
    conn.execute("SAVEPOINT recall_summary_vector_refresh")
    reason = None
    try:
        outcome = core._compute_and_store_embedding(conn, node_id)
        if outcome.status != "embedded":
            reason = _pending_reason(outcome)
        elif not outcome.vec_mirror_ok:
            reason = "mirror"
        else:
            row = conn.execute("SELECT embedding FROM nodes WHERE id = ?", (node_id,)).fetchone()
            try:
                vector = json.loads(row[0]) if row else None
            except (TypeError, json.JSONDecodeError):
                vector = None
            if not isinstance(vector, list) or not vector:
                reason = "canonical"
            elif _vec_store_exists(conn):
                # Do not accept the core helper's normal-write tolerance or
                # its wrong-dimension mirror bypass in this strict path.
                if (not core._VEC_BACKEND_AVAILABLE or core._sqlite_vec is None
                        or len(vector) != 384):
                    reason = "mirror"
                else:
                    mirror = conn.execute(
                        "SELECT embedding FROM vec_nodes WHERE node_id = ?", (node_id,)
                    ).fetchone()
                    if mirror is None or mirror[0] != core._sqlite_vec.serialize_float32(vector):
                        reason = "mirror"
    except Exception:  # savepoint makes unexpected compute/verify failure non-destructive
        reason = "embedding"

    if reason is not None:
        conn.execute("ROLLBACK TO recall_summary_vector_refresh")
        conn.execute("RELEASE recall_summary_vector_refresh")
        warning = _set_embedding_refresh_marker(conn, node_id, metadata_raw, reason=reason)
        return {"embedding_refresh": "pending", "embedding_refresh_reason": reason}, warning

    conn.execute("RELEASE recall_summary_vector_refresh")
    warning = _clear_embedding_refresh_marker(conn, node_id, metadata_raw)
    return {"embedding_refresh": "refreshed"}, warning


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

    # -- Apply valid entries with independent per-entry transactions --
    ok: list[dict] = []
    # Per-entry `ok` retains every attempt.  The response-level debt is instead
    # one stable-order state per target, overwritten only by a successfully
    # applied entry, so a later strict refresh can resolve earlier batch debt.
    response_debt_by_node: dict[str, bool] = {}
    warnings: list[str] = []
    conn = core._get_db()
    try:
        # Each outermost entry savepoint commits independently on RELEASE.
        # This is the best-effort peer-isolation transaction boundary.
        with conn:
            for entry in valid_entries:
                node_id = entry["node_id"].strip()
                recall_summary = entry["recall_summary"]
                recall_keywords = entry["recall_keywords"]
                conn.execute("SAVEPOINT recall_summary_entry")
                try:
                    row = conn.execute(
                        "SELECT id, is_current, metadata FROM nodes WHERE id = ?",
                        (node_id,),
                    ).fetchone()
                    if row is None:
                        errors.append({
                            "node_id": node_id,
                            "error": "node not found",
                        })
                        conn.execute("RELEASE recall_summary_entry")
                        continue
                    if row["is_current"] != 1:
                        errors.append({
                            "node_id": node_id,
                            "error": "node is not current (superseded)",
                        })
                        conn.execute("RELEASE recall_summary_entry")
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
                    refresh, warning = _strict_refresh_embedding(conn, node_id, row["metadata"])
                    if warning is not None:
                        warnings.append(warning)
                    conn.execute("RELEASE recall_summary_entry")
                    item = {"node_id": node_id, **refresh}
                    ok.append(item)
                    response_debt_by_node[node_id] = (
                        refresh["embedding_refresh"] == "pending"
                    )
                except Exception as exc:  # noqa: BLE001
                    conn.execute("ROLLBACK TO recall_summary_entry")
                    conn.execute("RELEASE recall_summary_entry")
                    errors.append({"node_id": node_id, "error": str(exc)})
    finally:
        conn.close()

    needs_reembed = [
        node_id for node_id, is_pending in response_debt_by_node.items() if is_pending
    ]
    result = {
        "ok": ok,
        "errors": errors,
        "applied": len(ok),
        "failed": len(errors),
        "pre_flight_failures": pre_flight_failures,
        "needs_reembed": needs_reembed,
    }
    if warnings:
        result["warnings"] = warnings
    # File-based responses always echo the resolved path, even when applied=0 —
    # so a genuinely-empty final_payload.json ("nothing to summarize tonight")
    # is distinguishable in the response from a misread/empty file, and never
    # silently indistinguishable from an inline no-op call (which carries no
    # such field, unchanged from before this feature).
    if summaries_file_path is not None:
        result["summaries_file"] = summaries_file_path
    return json.dumps(result)
