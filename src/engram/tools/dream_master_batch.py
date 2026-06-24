"""dream_master_batch.py — Dream-master batch-by-type bucketing helpers.

Implements the bucket_findings() + check_snapshot_divergence() logic the
dream-master uses to process pre-packed fairy reports without re-inspecting
every node. This module is pure Python (no ENGRAM server dependency) so it
is easily unit-tested.

Architecture context (PR-B):
  Each dream-fairy now includes a node_snapshot in every finding — the
  inspection state the fairy already gathered while doing its analysis.
  The dream-master calls bucket_findings() once to partition all findings
  by action type, then executes one bucket at a time (all resolutions, then
  all supersedes, …), calling check_snapshot_divergence() before each MCP
  write to guard against stale state.

Bucket names match the dream-master's action categories:
  resolutions         — engram_resolve calls
  supersedes          — engram_supersede calls
  retractions         — engram_retract calls
  new_derivations     — engram_derive calls
  lessons             — engram_lesson_register_incident calls
  cornerstone_moves   — engram_add_cornerstone / engram_outgrow_cornerstone
  goal_tension_resolutions — engram_resolve on gt_* nodes
  edge_wiring         — engram_add_edge calls (Category 7 missing-edge suggestions)
  task_closures       — engram_update_task calls (Category 8 stale-task-ref closures)

Suggestion-string → bucket routing uses the SUGGESTION_ROUTING table below;
add new entries when the dream-fairy spec adds new suggestion types.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Bucket name constants
# ---------------------------------------------------------------------------

BUCKET_RESOLUTIONS = "resolutions"
BUCKET_SUPERSEDES = "supersedes"
BUCKET_RETRACTIONS = "retractions"
BUCKET_NEW_DERIVATIONS = "new_derivations"
BUCKET_LESSONS = "lessons"
BUCKET_CORNERSTONE_MOVES = "cornerstone_moves"
BUCKET_GOAL_TENSION_RESOLUTIONS = "goal_tension_resolutions"
BUCKET_EDGE_WIRING = "edge_wiring"
BUCKET_TASK_CLOSURES = "task_closures"
BUCKET_UNKNOWN = "unknown"

ALL_BUCKET_NAMES = (
    BUCKET_RESOLUTIONS,
    BUCKET_SUPERSEDES,
    BUCKET_RETRACTIONS,
    BUCKET_NEW_DERIVATIONS,
    BUCKET_LESSONS,
    BUCKET_CORNERSTONE_MOVES,
    BUCKET_GOAL_TENSION_RESOLUTIONS,
    BUCKET_EDGE_WIRING,
    BUCKET_TASK_CLOSURES,
    BUCKET_UNKNOWN,
)

# ---------------------------------------------------------------------------
# Suggestion-string → bucket routing table
# ---------------------------------------------------------------------------
# Each entry: (regex_pattern, bucket_name).
# Patterns are matched case-insensitively against finding["suggestion"].
# First match wins.

SUGGESTION_ROUTING: list[tuple[str, str]] = [
    # Edge-wiring (Category 7 missing-principle-edge suggestions) — MUST precede
    # all other patterns.  Category 7 suggestion lines contain "engram_add_edge",
    # "suggested_relation", "instantiates", and "serves" — words that could
    # collide with cornerstone (\banchors?\b), lesson, or other buckets.
    # Anchoring on engram_add_edge and suggested_relation is unambiguous: no
    # other bucket's suggestion format uses those strings.
    (r"\bengram_add_edge\b", BUCKET_EDGE_WIRING),
    (r"\bsuggested_relation\b", BUCKET_EDGE_WIRING),
    # Goal-tension resolution — MUST precede generic 'resolv' patterns because
    # goal-tension suggestions contain words like "resolve tension" that would
    # otherwise match the generic resolution pattern first.
    (r"\bgoal.tension\b", BUCKET_GOAL_TENSION_RESOLUTIONS),
    (r"\btension\b.*\bresol", BUCKET_GOAL_TENSION_RESOLUTIONS),
    (r"\bresolv.*\btension\b", BUCKET_GOAL_TENSION_RESOLUTIONS),
    (r"\bgt_\w+", BUCKET_GOAL_TENSION_RESOLUTIONS),
    # Resolution patterns (after goal-tension so no false positives)
    (r"\bresolv", BUCKET_RESOLUTIONS),
    (r"\bclose\b.*\bquestion\b", BUCKET_RESOLUTIONS),
    (r"\bwire\b.*\bengram_resolve\b", BUCKET_RESOLUTIONS),
    # Supersede patterns
    (r"\bsupersede", BUCKET_SUPERSEDES),
    (r"\breplace\b.*\bwith\b", BUCKET_SUPERSEDES),
    # Retraction patterns
    (r"\bretract", BUCKET_RETRACTIONS),
    (r"\bremove\b.*\bnode\b", BUCKET_RETRACTIONS),
    # New derivation patterns
    (r"\bderive\b", BUCKET_NEW_DERIVATIONS),
    (r"\bdraft.*derivation\b", BUCKET_NEW_DERIVATIONS),
    (r"\bcompose.*derivation\b", BUCKET_NEW_DERIVATIONS),
    # Lesson patterns
    (r"\blesson\b", BUCKET_LESSONS),
    (r"\bregister.*incident\b", BUCKET_LESSONS),
    (r"\bincident.*lesson\b", BUCKET_LESSONS),
    # Cornerstone patterns
    (r"\bcornerstone\b", BUCKET_CORNERSTONE_MOVES),
    (r"\banchors?\b", BUCKET_CORNERSTONE_MOVES),
    (r"\bengram_add_cornerstone\b", BUCKET_CORNERSTONE_MOVES),
    (r"\bengram_outgrow_cornerstone\b", BUCKET_CORNERSTONE_MOVES),
    # Task-closure patterns (Category 8 stale-task-ref suggestions).
    # Canonical form: "close task and mark done — external reference #N <state>".
    # "close task" anchors on task; "mark.*task.*done" requires both words to
    # prevent false-positives like "mark contradiction done after resolution".
    (r"\b(close task|mark.*task.*done|update.*task|task.*done|stale.*task)\b", BUCKET_TASK_CLOSURES),
]

# Pre-compiled patterns for performance.
_COMPILED_ROUTING: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pat, re.IGNORECASE), bucket)
    for pat, bucket in SUGGESTION_ROUTING
]


def _route_suggestion(suggestion: str) -> str:
    """Map a suggestion string to a bucket name.

    Returns BUCKET_UNKNOWN if no pattern matches.  Dream-master must log
    unknown-bucket findings in the dream record so Lei can decide.
    """
    if not isinstance(suggestion, str):
        return BUCKET_UNKNOWN
    for pattern, bucket in _COMPILED_ROUTING:
        if pattern.search(suggestion):
            return bucket
    return BUCKET_UNKNOWN


def bucket_findings(fairy_reports: list[dict]) -> dict[str, list[dict]]:
    """Partition findings from all fairy reports into action-type buckets.

    Args:
        fairy_reports: list of report dicts.  Each report has shape::

            {
              "category": int,        # 1–6, optional
              "fairy_id": str,        # optional label
              "findings": [           # the core list
                {
                  "node_id": str,
                  "node_snapshot": dict,        # pre-packed inspection state
                  "suggestion": str,            # human-readable action verb
                  "rationale": str,             # human-readable audit trail
                  "verification_state": str,    # optional ISO timestamp + notes
                  ...                           # category-specific extra fields
                },
                ...
              ]
            }

    Returns:
        dict mapping each bucket name to a list of finding dicts.
        Every finding carries an added ``_source_fairy`` key (value =
        ``fairy_report.get("fairy_id")`` or ``fairy_report.get("category")``),
        and an added ``_bucket`` key for traceability.  Original finding dicts
        are not mutated; copies are used.
    """
    buckets: dict[str, list[dict]] = {name: [] for name in ALL_BUCKET_NAMES}

    for report in fairy_reports:
        fairy_label = report.get("fairy_id") or report.get("category") or "unknown"
        findings = report.get("findings", [])
        if not isinstance(findings, list):
            continue

        for finding in findings:
            if not isinstance(finding, dict):
                continue

            suggestion = finding.get("suggestion", "")
            bucket = _route_suggestion(suggestion)

            enriched = dict(finding)
            enriched["_source_fairy"] = fairy_label
            enriched["_bucket"] = bucket
            buckets[bucket].append(enriched)

    return buckets


# ---------------------------------------------------------------------------
# Snapshot divergence check
# ---------------------------------------------------------------------------

class SnapshotDivergence(Exception):
    """Raised by check_snapshot_divergence when a divergence is detected.

    Attributes:
        node_id:    The node whose state diverged.
        field:      The field that diverged.
        snapshot_value: What the fairy's snapshot said.
        current_value:  What the DB shows now.
        message:    Human-readable summary.
    """

    def __init__(
        self,
        node_id: str,
        field: str,
        snapshot_value: Any,
        current_value: Any,
    ):
        self.node_id = node_id
        self.field = field
        self.snapshot_value = snapshot_value
        self.current_value = current_value
        self.message = (
            f"Snapshot divergence for {node_id}: field '{field}' "
            f"snapshot={snapshot_value!r}, current={current_value!r}. "
            f"Skip this finding and log in dream record."
        )
        super().__init__(self.message)


def check_snapshot_divergence(
    node_id: str,
    snapshot: dict,
    current_db_row: "dict | None",
) -> None:
    """Guard: raise SnapshotDivergence if snapshot conflicts with current DB state.

    The dream-master calls this before each MCP write.  If the node's state
    changed between fairy dispatch and dream-master invocation (e.g., the
    parent resolved or superseded the node while fairies were running), the
    MCP write would act on stale state — silently incorrect.

    Checks performed (in order; first divergence raises immediately):

    0. ``existence``: if ``current_db_row`` is ``None``, the node no longer
       exists in the DB (deleted or never committed).  Treated as divergent —
       raise SnapshotDivergence with ``field="existence"`` so the caller's
       existing ``except SnapshotDivergence`` handler skips + logs the action.

    1. ``is_current``: if the snapshot says the node was current (``True`` /
       ``1``) but the DB now shows ``0`` or ``False``, the node was superseded
       after fairy dispatch.  All write operations on a non-current node are
       likely wrong.

    2. ``status``: if the snapshot status differs from the DB status, the
       node's lifecycle state changed.  E.g., the node was ``open`` in the
       snapshot but is now ``resolved`` — no need to re-resolve.

    3. ``superseded_by``: if the snapshot had ``None`` but the DB now has a
       non-null ``superseded_by``, the node was superseded after dispatch.

    Args:
        node_id:       The node ID being checked.
        snapshot:      The ``node_snapshot`` dict from the fairy finding.
                       May omit fields; only present fields are checked.
        current_db_row: A dict representing the current DB state for the node,
                       or ``None`` if ``fetch_safety_row`` found no row (node
                       missing / deleted).  Must include at least the fields
                       present in snapshot when not ``None``.

    Raises:
        SnapshotDivergence: if any checked field diverges, or if the node is
                       missing from the DB (``current_db_row is None``).
        TypeError: if ``snapshot`` is not a dict.
    """
    if not isinstance(snapshot, dict):
        raise TypeError(f"snapshot must be a dict, got {type(snapshot)!r}")
    if current_db_row is None:
        raise SnapshotDivergence(
            node_id,
            "existence",
            snapshot.get("is_current") if isinstance(snapshot, dict) else True,
            None,
        )
    if not isinstance(current_db_row, dict):
        raise TypeError(f"current_db_row must be a dict, got {type(current_db_row)!r}")

    # 1. is_current divergence (node superseded after fairy dispatch)
    snap_current = snapshot.get("is_current")
    db_current = current_db_row.get("is_current")
    if snap_current is not None:
        if db_current is None:
            # Snapshot has the field but DB row does not — cannot verify; fail-closed.
            raise SnapshotDivergence(
                node_id, "is_current", snap_current, db_current
            )
        # Normalize to bool for comparison (DB stores 0/1 int; snapshot may use bool)
        snap_current_bool = bool(snap_current)
        db_current_bool = bool(db_current)
        if snap_current_bool and not db_current_bool:
            raise SnapshotDivergence(
                node_id, "is_current", snap_current, db_current
            )

    # 2. status divergence
    snap_status = snapshot.get("status")
    db_status = current_db_row.get("status")
    if snap_status is not None:
        if db_status is None:
            # Snapshot has the field but DB row does not — cannot verify; fail-closed.
            raise SnapshotDivergence(
                node_id, "status", snap_status, db_status
            )
        if snap_status != db_status:
            raise SnapshotDivergence(
                node_id, "status", snap_status, db_status
            )

    # 3. superseded_by divergence (node was superseded after fairy dispatch)
    # Use a sentinel to distinguish "key absent from snapshot" (don't check)
    # from "key present with value None" (snapshot explicitly says no superseder).
    _MISSING = object()
    snap_sup_by = snapshot.get("superseded_by", _MISSING)
    if snap_sup_by is not _MISSING:
        # The snapshot explicitly included superseded_by.
        # Only flag if snapshot said None (no superseder) but DB now shows one.
        db_sup_by = current_db_row.get("superseded_by")
        if snap_sup_by is None and db_sup_by is not None:
            raise SnapshotDivergence(
                node_id, "superseded_by", snap_sup_by, db_sup_by
            )


# ---------------------------------------------------------------------------
# Safety-row fetch helper
# ---------------------------------------------------------------------------


def fetch_safety_row(conn: Any, node_id: str) -> "dict | None":
    """Cheap targeted read of just the fields check_snapshot_divergence compares.

    The dream-master calls this immediately before each MCP write to obtain a
    fresh current_db_row for check_snapshot_divergence.  Only the three fields
    the guard inspects are fetched — this is intentionally NOT a full
    engram_inspect, so it does not restore the 50-turn re-inspection cost the
    snapshot architecture eliminated.

    Usage pattern (per dream-master spec):

        current = fetch_safety_row(conn, target_id)
        check_snapshot_divergence(target_id, finding["node_snapshot"], current)
        # … proceed with MCP write only if no exception raised …

    Args:
        conn:    A sqlite3.Connection (or compatible) to the ENGRAM knowledge.db.
        node_id: The node ID to look up.

    Returns:
        dict with keys ``is_current``, ``status``, ``superseded_by`` if the
        node exists, or None if the node is not found.
    """
    row = conn.execute(
        "SELECT is_current, status, superseded_by FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Batch execution result helpers
# ---------------------------------------------------------------------------

class BatchResult:
    """Accumulates the outcome of executing one bucket's findings.

    The dream-master iterates over a bucket's findings, calls
    ``check_snapshot_divergence``, and either executes the MCP write or
    records a skip.  BatchResult collects that state for the dream record.
    """

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self.executed: list[dict] = []       # findings that ran successfully
        self.skipped_diverged: list[dict] = []   # findings skipped for divergence
        self.skipped_other: list[dict] = []  # findings skipped for other reasons

    def record_executed(self, finding: dict, mcp_result: Any = None) -> None:
        entry = {"node_id": finding.get("node_id"), "mcp_result": mcp_result}
        self.executed.append(entry)

    def record_skip_divergence(
        self, finding: dict, divergence: SnapshotDivergence
    ) -> None:
        entry = {
            "node_id": finding.get("node_id"),
            "reason": "snapshot_divergence",
            "detail": divergence.message,
        }
        self.skipped_diverged.append(entry)

    def record_skip_other(self, finding: dict, reason: str) -> None:
        entry = {"node_id": finding.get("node_id"), "reason": reason}
        self.skipped_other.append(entry)

    def summary(self) -> dict:
        return {
            "bucket": self.bucket,
            "executed": len(self.executed),
            "skipped_diverged": len(self.skipped_diverged),
            "skipped_other": len(self.skipped_other),
            "total": len(self.executed) + len(self.skipped_diverged) + len(self.skipped_other),
        }
