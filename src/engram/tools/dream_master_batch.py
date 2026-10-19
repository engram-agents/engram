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
  coverage_gaps       — Category 9 cornerstone auto-load surface gaps (human-action items)

Routing (alpha#2987): a finding's bucket is decided in three tiers, most
authoritative first —

  1. An explicit per-finding ``bucket`` field, when present and a valid
     bucket name (future-proofing for a structured fairy/dream-master
     contract; no fairy emits this yet).
  2. The finding's REPORT-level ``category`` (1-12), via CATEGORY_TO_BUCKET,
     for categories whose dream-fairy definition is inherently single-purpose
     (7/8/9 today — a Category-7 finding is *always* an edge-wiring
     suggestion by definition of the category, independent of how the
     dream-master happens to phrase it in ``suggestion`` text).
  3. SUGGESTION_ROUTING below — regex over ``finding["suggestion"]`` prose.
     Kept as the fallback for heterogeneous categories (1/2/3/4/5/6, which
     can each produce more than one action type) and for any finding whose
     report omits ``category``.

Add new SUGGESTION_ROUTING entries when the dream-fairy spec adds new
suggestion types; add a CATEGORY_TO_BUCKET entry only when a category is
architecturally 1:1 with a single bucket, never as a probabilistic default.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
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
BUCKET_COVERAGE_GAPS = "coverage_gaps"
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
    BUCKET_COVERAGE_GAPS,
    BUCKET_UNKNOWN,
)

# ---------------------------------------------------------------------------
# Category → bucket map (alpha#2987) — tier 2 of the three-tier routing above.
# ---------------------------------------------------------------------------
# Only categories that are ARCHITECTURALLY 1:1 with a single bucket belong
# here — i.e. every finding the category's dream-fairy definition can
# produce maps to the same action, regardless of suggestion phrasing.
# Categories 1/2/3/4/5/6 are heterogeneous (a Category-1 open-question
# finding may resolve, or may recommend no action at all) and are
# deliberately NOT listed — they stay on the SUGGESTION_ROUTING fallback.
# Categories 11/12 are ALSO deliberately excluded, for a different reason:
# both require per-finding multi-way adjudication (Category 11: rewire /
# leave-unanchored / retract; Category 12: no bulk path by design — the
# generalization direction is a judgment call the bucket-loop can't express)
# and are handled entirely outside bucket_findings()/ALL_BUCKET_NAMES — see
# engram-dream-master.md's "What's in your domain" section, not this map.

CATEGORY_TO_BUCKET: dict[int, str] = {
    7: BUCKET_EDGE_WIRING,       # missing principle-edges — always engram_add_edge
    8: BUCKET_TASK_CLOSURES,     # stale external task refs — always engram_update_task
    9: BUCKET_COVERAGE_GAPS,     # coverage-gap flags — always a human-action item
}

# ---------------------------------------------------------------------------
# Suggestion-string → bucket routing table (tier 3, fallback)
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
    # Coverage gap patterns (Category 9) — MUST precede generic cornerstone patterns
    # because coverage-gap suggestions contain "cornerstone" but route to the
    # human-action bucket, not cornerstone_moves.
    (r"\bauto.load surface\b", BUCKET_COVERAGE_GAPS),
    (r"\bwarm.briefing anchor\b", BUCKET_COVERAGE_GAPS),
    # Cornerstone patterns
    (r"\bcornerstone\b", BUCKET_CORNERSTONE_MOVES),
    (r"\banchors?\b", BUCKET_CORNERSTONE_MOVES),
    (r"\bengram_add_cornerstone\b", BUCKET_CORNERSTONE_MOVES),
    (r"\bengram_outgrow_cornerstone\b", BUCKET_CORNERSTONE_MOVES),
    # Task-closure patterns (Category 8 stale-task-ref suggestions).
    # Canonical form: "close task and mark done — external reference owner/repo#N <state>".
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
    """Tier-3 fallback: map a suggestion STRING to a bucket by regex.

    Only reached when tiers 1 (explicit ``finding["bucket"]``) and 2
    (``CATEGORY_TO_BUCKET`` on the report's ``category``) didn't resolve it —
    see the module docstring and ``_route_finding`` below. Matches
    case-insensitively against the literal ``suggestion`` text, so it is only
    as reliable as that text happens to be (alpha#2987: real dream-master
    findings have so far carried the fairy's fixed-format prose line rather
    than a bare verb, which is what makes this tier work in practice today —
    but nothing enforces that, which is why tier 2 exists for the categories
    where it doesn't have to be relied on at all).

    Returns BUCKET_UNKNOWN if no pattern matches.  Dream-master must log
    unknown-bucket findings in the dream record so Lei can decide.
    """
    if not isinstance(suggestion, str):
        return BUCKET_UNKNOWN
    for pattern, bucket in _COMPILED_ROUTING:
        if pattern.search(suggestion):
            return bucket
    return BUCKET_UNKNOWN


def _route_finding(finding: dict, category: Any) -> tuple[str, str]:
    """Route one finding to a bucket, three tiers, most authoritative first.

    Returns ``(bucket, routed_via)`` where ``routed_via`` is one of
    ``"explicit"`` (tier 1), ``"category"`` (tier 2), ``"regex"`` (tier 3
    matched something), or ``"unknown"`` (nothing matched — BUCKET_UNKNOWN).
    """
    explicit = finding.get("bucket")
    if isinstance(explicit, str) and explicit in ALL_BUCKET_NAMES:
        return explicit, "explicit"
    # [reviewer-fairy suggestion, PR-2988] An explicit `bucket` that is a
    # string but NOT a valid name (e.g. a typo once a fairy/dream-master
    # starts emitting this field for real) falls through silently to tier
    # 2/3 here — indistinguishable from the field being absent. Fine today
    # (no producer exists yet), but worth a real validation error once one
    # does.

    if isinstance(category, int) and category in CATEGORY_TO_BUCKET:
        return CATEGORY_TO_BUCKET[category], "category"

    bucket = _route_suggestion(finding.get("suggestion", ""))
    return bucket, ("regex" if bucket != BUCKET_UNKNOWN else "unknown")


def bucket_findings(fairy_reports: list[dict]) -> dict[str, list[dict]]:
    """Partition findings from all fairy reports into action-type buckets.

    Args:
        fairy_reports: list of report dicts.  Each report has shape::

            {
              "category": int,        # 1–12, optional but drives tier-2 routing
              "fairy_id": str,        # optional label
              "findings": [           # the core list
                {
                  "node_id": str,
                  "node_snapshot": dict,        # pre-packed inspection state
                  "bucket": str,                 # optional — see routing tier 1
                  "suggestion": str,            # human-readable suggestion text
                                                 # (alpha#2987: NOT guaranteed to
                                                 # be a bare verb — real findings
                                                 # so far carry the fairy's full
                                                 # report-line prose; see the
                                                 # module docstring's tier 3)
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
        an added ``_bucket`` key for traceability, and an added
        ``_routed_via`` key (``"explicit"|"category"|"regex"|"unknown"`` —
        see ``_route_finding``) so the dream-master can tell a genuinely
        unclassifiable finding (regex fallback found nothing, on a
        heterogeneous category) apart from a finding that landed in
        BUCKET_UNKNOWN despite belonging to a category CATEGORY_TO_BUCKET
        already covers — the latter means the report's own ``category``
        field was missing or malformed, a contract violation worth
        surfacing loudly rather than silently logging as "I don't know
        what this is." Original finding dicts are not mutated; copies are
        used.
    """
    buckets: dict[str, list[dict]] = {name: [] for name in ALL_BUCKET_NAMES}

    for report in fairy_reports:
        fairy_label = report.get("fairy_id") or report.get("category") or "unknown"
        category = report.get("category")
        findings = report.get("findings", [])
        if not isinstance(findings, list):
            continue

        for finding in findings:
            if not isinstance(finding, dict):
                continue

            bucket, routed_via = _route_finding(finding, category)

            enriched = dict(finding)
            enriched["_source_fairy"] = fairy_label
            enriched["_bucket"] = bucket
            enriched["_routed_via"] = routed_via
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

# The one statement `fetch_safety_row` sends, registered byte for byte as a read template under the site
# `tools/dream_master_batch.py:fetch_safety_row` (U17, #3880). `_SAFETY_FIELDS` is its column order: the owner
# returns rows as positional lists with no `description`, so the mapping to names must come from here.
_SAFETY_FIELDS = ("is_current", "status", "superseded_by")
_SAFETY_ROW_SQL = "SELECT is_current, status, superseded_by FROM nodes WHERE id = ?"
if _SAFETY_ROW_SQL != f"SELECT {', '.join(_SAFETY_FIELDS)} FROM nodes WHERE id = ?":
    raise RuntimeError("dream_master_batch: _SAFETY_ROW_SQL and _SAFETY_FIELDS disagree")


class DreamReadError(RuntimeError):
    """A store read the dream master asked for could not be answered (U17, #3880).

    Raised for every owner failure (flag ON) and for a malformed answer from either path. The CLI turns it
    into a non-zero exit with EMPTY stdout: a failed read must never print `null` (a missing node), `0`, or
    a coverage fraction."""


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
                 Works regardless of the connection's ``row_factory`` — a
                 default connection (rows as tuples) and a
                 ``sqlite3.Row``-factory connection both return an equivalent
                 dict.  The dict is built locally from the cursor's own
                 ``description`` rather than relying on ``dict(row)``, which
                 only works when the connection already uses
                 ``sqlite3.Row`` and raises ``TypeError`` on a plain tuple.
        node_id: The node ID to look up.

    Returns:
        dict with keys ``is_current``, ``status``, ``superseded_by`` if the
        node exists, or None if the node is not found. A missing node is a
        distinct outcome from a fetch failure: any error from the query
        itself (bad connection, missing table, …) propagates as an
        exception rather than being folded into ``None`` — collapsing the
        two would let a broken connection masquerade as a normal "node not
        found" result, which is exactly the silent-on-the-dangerous-path
        failure this helper exists to avoid feeding into
        ``check_snapshot_divergence``.
    """
    cursor = conn.execute(_SAFETY_ROW_SQL, (node_id,))
    # A SELECT by the unique node id answers zero or one row. The owner's read cursor has only
    # fetchone()/fetchall() (no fetchmany), so read them all: more than one is malformed and must never
    # become a plausible pre-write safety result, whether the extra rows agree or not.
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise DreamReadError(
            f"fetch_safety_row: expected zero or one row for node id {node_id!r}, got {len(rows)}"
        )
    if not rows:
        return None
    row = rows[0]
    if isinstance(row, dict):
        # An exotic dict-returning row_factory: tuple(row) would yield the
        # KEYS, silently returning column names as pseudo-values — the exact
        # silent-wrong-answer class this helper exists to avoid. Take the
        # dict as-is instead. (No in-repo caller uses such a factory today;
        # reviewer-constructed edge case, PR #2091.)
        return dict(row)
    # The owner's read cursor has no `description` (U17, #3880): its columns are the SELECT list, in order.
    description = getattr(cursor, "description", None)
    columns = [col[0] for col in description] if description else list(_SAFETY_FIELDS)
    cells = tuple(row)
    if len(cells) != len(columns):
        # zip() would silently truncate a short or wide row into a dict missing a key.
        raise DreamReadError(
            f"fetch_safety_row: the row has {len(cells)} cells, expected {len(columns)} ({', '.join(columns)})"
        )
    return dict(zip(columns, cells))


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


# ---------------------------------------------------------------------------
# CLI: the dream master's three store reads, so no agent ever opens knowledge.db (U17, #3880 of #2652)
# ---------------------------------------------------------------------------
#
#   safety-row <node_id>               one JSON object {"is_current","status","superseded_by"}, or `null`
#                                      for a MISSING node (exit 0)
#   coverage {recall_summary|recall_keywords}
#                                      the fraction health.metrics.recall_*_coverage(conn) computes, for
#                                      every value engram_core can write (see the note above the SQL)
#                                      (the string "UNAVAILABLE" when no claim-bearing node is current,
#                                      as health reports it)
#   null-embeddings --population P     the count health.engine._count_null_embeddings(conn, P) computes
#
# ONE stdout/exit contract for all three: a successful read prints exactly one JSON line and exits 0; ANY
# failure prints NOTHING on stdout, one `<ClassName>: <message>` line on stderr, and exits non-zero. A
# failed read is never `null`, `0` or a fraction.
#
# Behind `engramd.dream_safety_reads_via_owner` (default OFF) every statement goes to the engramd owner as
# one registered read template; flag ON with the owner down, refusing or answering malformed FAILS, with no
# direct fallback. Flag OFF opens the store `mode=ro`. The two aggregates are computed IN SQL (counts, not
# every summary and keyword list over the socket); a cell in tests/test_dream_safety_owner_read_3880.py holds
# them equal to the health functions for every value engram_core can write (NULL, or the json.dumps of a list).
# Five hand-written recall_keywords shapes engram_core cannot write DIVERGE and are pinned, not fixed, in
# test_round1_keywords_shapes_engram_core_cannot_write_diverge_in_exactly_the_pinned_way: [NaN], [Infinity],
# [-Infinity] and nesting past SQLite's depth limit (python: present, SQL: missing), and a NUL byte after the
# list (python: missing, SQL: present). All five are unreachable through a normal write.

OWNER_FLAG_KEY = "dream_safety_reads_via_owner"

# engram_core.CLAIM_BEARING_TYPES, sorted, as a literal: this CLI must not import engram_core (on a host without
# sqlite-vec the import prints a notice on stderr, and a successful call must leave stderr empty). A test pins
# the literal equal to sorted(engram_core.CLAIM_BEARING_TYPES), so there is still no second list to drift.
CLAIM_BEARING_TYPES_SORTED = (
    "axiom",
    "conjecture",
    "derivation",
    "observation_factual",
    "observation_predictive",
    "theory",
)

_COVERAGE_UNAVAILABLE = "UNAVAILABLE"
_POPULATION_CLAIM_BEARING = "claim_bearing_nodes"

# The three aggregate statements, each registered byte for byte under its own function's site. The claim-bearing
# type list travels as ONE json parameter (the registry's admitted json_each form): the same set the health
# functions read from engram_core.CLAIM_BEARING_TYPES (held here as CLAIM_BEARING_TYPES_SORTED, pinned by a test). The summary test
# trims exactly the characters Python's str.strip() trims (health.metrics.recall_summary_coverage uses `.strip()`):
# those with str.isspace() true, the list the session-start hook's registered statements use too.
_RECALL_SUMMARY_COUNTS_SQL = (
    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN trim(coalesce(recall_summary, ''), '\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000') = '' "
    "THEN 1 ELSE 0 END), 0) FROM nodes WHERE is_current = 1 AND type IN (SELECT value FROM json_each(?))"
)
_RECALL_KEYWORDS_COUNTS_SQL = (
    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN json_valid(recall_keywords) THEN CASE WHEN "
    "json_type(recall_keywords) = 'array' AND json_array_length(recall_keywords) > 0 THEN 0 ELSE 1 END "
    "ELSE 1 END), 0) FROM nodes WHERE is_current = 1 AND type IN (SELECT value FROM json_each(?))"
)
_NULL_EMBEDDINGS_SQL = (
    "SELECT COUNT(*) FROM nodes WHERE is_current = 1 AND type IN (SELECT value FROM json_each(?)) "
    "AND embedding IS NULL"
)


def _owner_read():
    """The shared owner-read helper (tools/_owner_read.py), imported lazily so the library functions above
    keep their import-time purity. Resolves the package root through the symlink, so it works run as a script,
    loaded by path, or from the flattened plugin layout."""
    pkg_root = str(Path(__file__).resolve().parent.parent)
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    from tools import _owner_read as owner_read

    return owner_read


def _owner_spec(owner_read: Any) -> Any:
    return owner_read.OwnerReadSpec(
        flag_key=OWNER_FLAG_KEY,
        error_cls=DreamReadError,
        home=owner_read.engram_home(),
        never_empty="Not answering from a default: a failed read must never read as a missing node, "
                    "zero coverage or zero findings.",
    )


def _claim_bearing_types_json() -> str:
    """The claim-bearing types, sorted, as the json_each parameter (the order health.metrics uses)."""
    return json.dumps(list(CLAIM_BEARING_TYPES_SORTED))


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _one_row(rows: list, width: int, what: str) -> tuple:
    """An aggregate statement returns exactly one row of `width` cells; anything else is malformed, never a
    value (an empty answer would otherwise read as zero coverage)."""
    if len(rows) != 1 or not isinstance(rows[0], (list, tuple)) or len(rows[0]) != width:
        raise DreamReadError(f"{what}: expected exactly one row of {width} cells, got {str(rows)[:120]}")
    return tuple(rows[0])


def _checked_counts(rows: list, what: str) -> tuple[int, int]:
    total, missing = _one_row(rows, 2, what)
    if not (_is_count(total) and _is_count(missing) and missing <= total):
        raise DreamReadError(f"{what}: counts are not 0 <= missing <= total integers: {str((total, missing))[:120]}")
    return total, missing


def recall_summary_counts(conn: Any) -> tuple[int, int]:
    """(current claim-bearing nodes, of those with no recall_summary) -- the denominator and numerator
    health.metrics.recall_summary_coverage works from, computed in SQL."""
    rows = conn.execute(_RECALL_SUMMARY_COUNTS_SQL, (_claim_bearing_types_json(),)).fetchall()
    return _checked_counts(rows, "recall_summary coverage")


def recall_keywords_counts(conn: Any) -> tuple[int, int]:
    """(current claim-bearing nodes, of those with no non-empty recall_keywords list)."""
    rows = conn.execute(_RECALL_KEYWORDS_COUNTS_SQL, (_claim_bearing_types_json(),)).fetchall()
    return _checked_counts(rows, "recall_keywords coverage")


def coverage_value(total: int, missing: int) -> "float | str":
    """health.metrics's own arithmetic: 1.0 - missing/total, or its UNAVAILABLE sentinel (named, never a
    number) when there is no claim-bearing node to cover."""
    return _COVERAGE_UNAVAILABLE if total == 0 else 1.0 - (missing / total)


def _check_population(population: str) -> None:
    if population != _POPULATION_CLAIM_BEARING:
        raise ValueError(
            f"null-embeddings only implements the '{_POPULATION_CLAIM_BEARING}' population, got {population!r}"
        )


def count_null_embeddings(conn: Any, population: str) -> int:
    """Count of CURRENT claim-bearing nodes with a NULL embedding -- health.engine._count_null_embeddings."""
    _check_population(population)
    (count,) = _one_row(
        conn.execute(_NULL_EMBEDDINGS_SQL, (_claim_bearing_types_json(),)).fetchall(), 1, "null-embeddings"
    )
    if not _is_count(count):
        raise DreamReadError(f"null-embeddings: the count is not a non-negative integer: {str(count)[:120]}")
    return count


def _checked_safety_row(row: "dict | None") -> "dict | None":
    """Types of a safety row, on BOTH paths: is_current an int or null, status and superseded_by text or null."""
    if row is None:
        return None
    cur = row.get("is_current")
    ok = (
        list(row) == list(_SAFETY_FIELDS)
        and (cur is None or (isinstance(cur, int) and not isinstance(cur, bool)))
        and all(row[k] is None or isinstance(row[k], str) for k in ("status", "superseded_by"))
    )
    if not ok:
        raise DreamReadError(f"safety-row: the row does not match the node schema: {str(row)[:200]}")
    return row


def _with_reader(read: Any) -> Any:
    """Open the read connection the flag selects, call read(conn), close. Flag ON: the owner's answer replaces
    the db_path.exists() precondition and nothing is opened directly; flag OFF: a `mode=ro` connection."""
    owner_read = _owner_read()
    spec = _owner_spec(owner_read)
    db_path = owner_read.default_db_path()
    use_owner = owner_read.flag_applies(spec, db_path)
    if use_owner:
        owner_read.ping(spec)
    elif not db_path.exists():
        raise FileNotFoundError(f"ENGRAM database not found: {db_path}")
    conn = owner_read.open_reader(spec, db_path, use_owner)
    try:
        return read(conn)
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="dream_master_batch.py",
        description="The dream master's store reads. One JSON line on stdout and exit 0, or NOTHING on stdout "
                    "and a non-zero exit with the error class on stderr. Never open knowledge.db yourself.",
    )
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("safety-row", help="is_current, status, superseded_by of one node (null if missing)")
    p.add_argument("node_id")
    p = sub.add_parser("coverage", help="fraction of current claim-bearing nodes with a summary / keywords")
    p.add_argument("which", choices=["recall_summary", "recall_keywords"])
    p = sub.add_parser("null-embeddings", help="count of current claim-bearing nodes with a NULL embedding")
    p.add_argument("--population", required=True)
    return ap


def main(argv: "list[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "safety-row":
            result = _with_reader(lambda conn: _checked_safety_row(fetch_safety_row(conn, args.node_id)))
        elif args.command == "coverage":
            counts = recall_summary_counts if args.which == "recall_summary" else recall_keywords_counts
            result = coverage_value(*_with_reader(counts))
        else:
            _check_population(args.population)
            result = _with_reader(lambda conn: count_null_embeddings(conn, args.population))
        line = json.dumps(result)
    except Exception as exc:  # every failure is loud and empty on stdout; KeyboardInterrupt propagates
        sys.stderr.write(f"{type(exc).__name__}: {' '.join(str(exc).split())}\n")
        return 1
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
