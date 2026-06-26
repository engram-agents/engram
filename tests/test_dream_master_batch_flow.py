"""Tests for dream-master batch-by-type bucketing + snapshot-divergence safety.

PR-B: self-sufficient fairy reports + dream-master batch-by-type orchestration.

Coverage:
  1. bucket_findings() — routing correctness for all nine action buckets.
  2. bucket_findings() — unknown suggestions land in BUCKET_UNKNOWN.
  3. bucket_findings() — multi-fairy input is merged; _source_fairy is set.
  4. bucket_findings() — empty / malformed input is handled safely.
  5. bucket_findings() — _bucket traceability key is added to every finding.
  6. check_snapshot_divergence() — clean snapshot passes silently.
  7. check_snapshot_divergence() — is_current divergence raises.
  8. check_snapshot_divergence() — status divergence raises.
  9. check_snapshot_divergence() — superseded_by divergence raises.
  10. check_snapshot_divergence() — partial snapshot (missing fields) passes.
  11. SnapshotDivergence — attributes are accessible for dream-record logging.
  12. BatchResult — accumulates executed / skipped entries.
  13. Resolution bucket execution sequence: findings are processed in order
      and check_snapshot_divergence fires before each write.
  14. Goal-tension findings route to goal_tension_resolutions, not resolutions.
  19. check_snapshot_divergence() — missing node (fetch_safety_row → None)
      raises SnapshotDivergence(field="existence"), not TypeError.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.dream_master_batch import (
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
    ALL_BUCKET_NAMES,
    BatchResult,
    SnapshotDivergence,
    bucket_findings,
    check_snapshot_divergence,
    fetch_safety_row,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_finding(
    node_id: str,
    suggestion: str,
    *,
    status: str = "open",
    is_current: bool = True,
    superseded_by: str | None = None,
    confidence: float = 0.75,
    extra_snapshot: dict | None = None,
) -> dict:
    """Build a finding dict with a well-formed node_snapshot."""
    snapshot = {
        "claim": f"Claim for {node_id}",
        "status": status,
        "confidence": confidence,
        "is_current": is_current,
        "supersedes": None,
        "superseded_by": superseded_by,
        "key_neighbors": [],
        "recall_count": 3,
        "memory_status": "active",
    }
    if extra_snapshot:
        snapshot.update(extra_snapshot)
    return {
        "node_id": node_id,
        "node_snapshot": snapshot,
        "suggestion": suggestion,
        "rationale": f"Rationale for {node_id}.",
        "verification_state": "snapshot taken at 2026-05-27T12:00:00Z; dream_mode=true",
    }


def _make_report(
    findings: list[dict],
    *,
    category: int = 1,
    fairy_id: str = "fairy-1",
) -> dict:
    return {
        "category": category,
        "fairy_id": fairy_id,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# 1. Routing correctness — each suggestion maps to the expected bucket
# ---------------------------------------------------------------------------


class TestBucketFindingsRouting:
    """Each suggestion string routes to the correct bucket."""

    def _single(self, suggestion: str) -> str:
        """Run bucket_findings with one finding, return its bucket."""
        finding = _make_finding("ob_0001", suggestion)
        report = _make_report([finding])
        buckets = bucket_findings([report])
        for name, items in buckets.items():
            for item in items:
                if item["node_id"] == "ob_0001":
                    return name
        return BUCKET_UNKNOWN

    def test_resolution_suggestion(self):
        assert self._single("close as resolved") == BUCKET_RESOLUTIONS

    def test_resolution_wire_suggestion(self):
        assert self._single("wire via engram_resolve to close this question") == BUCKET_RESOLUTIONS

    def test_supersede_suggestion(self):
        assert self._single("supersede with fresher derivation") == BUCKET_SUPERSEDES

    def test_retraction_suggestion(self):
        assert self._single("retract — tainted by upstream removal") == BUCKET_RETRACTIONS

    def test_derive_suggestion(self):
        assert self._single("derive synthesis from these three observations") == BUCKET_NEW_DERIVATIONS

    def test_compose_derivation_suggestion(self):
        assert self._single("compose a derivation citing ob_0012 and ob_0013") == BUCKET_NEW_DERIVATIONS

    def test_lesson_suggestion(self):
        assert self._single("register incident for lesson ls_0014") == BUCKET_LESSONS

    def test_coverage_gap_auto_load_surface(self):
        assert self._single(
            "Add warm-briefing anchor entry for cs_0042 — no auto-load surface coverage found"
        ) == BUCKET_COVERAGE_GAPS

    def test_coverage_gap_warm_briefing(self):
        assert self._single(
            "Add warm-briefing anchor for cs_0011 (handle: cheap-verify) — no auto-load surface anchor found"
        ) == BUCKET_COVERAGE_GAPS

    def test_coverage_gap_routes_before_cornerstone(self):
        # coverage-gap suggestions contain "cornerstone" but must NOT fall through to BUCKET_CORNERSTONE_MOVES
        assert self._single(
            "cs_0050 cornerstone has no auto-load surface anchor — add warm-briefing anchor entry"
        ) == BUCKET_COVERAGE_GAPS

    def test_cornerstone_suggestion(self):
        assert self._single("anchor as cornerstone via engram_add_cornerstone") == BUCKET_CORNERSTONE_MOVES

    def test_outgrow_cornerstone_suggestion(self):
        assert self._single("call engram_outgrow_cornerstone to retire") == BUCKET_CORNERSTONE_MOVES

    def test_goal_tension_suggestion(self):
        assert self._single("resolve goal-tension gt_0003 via synthesis") == BUCKET_GOAL_TENSION_RESOLUTIONS

    def test_gt_id_in_suggestion(self):
        assert self._single("gt_0005 needs a resolution derivation") == BUCKET_GOAL_TENSION_RESOLUTIONS

    def test_unknown_suggestion(self):
        assert self._single("a completely novel action type") == BUCKET_UNKNOWN


# ---------------------------------------------------------------------------
# 2. Unknown suggestions land in BUCKET_UNKNOWN
# ---------------------------------------------------------------------------


def test_unknown_suggestion_lands_in_unknown_bucket():
    finding = _make_finding("ob_0099", "do something never before described")
    report = _make_report([finding])
    buckets = bucket_findings([report])
    assert len(buckets[BUCKET_UNKNOWN]) == 1
    assert buckets[BUCKET_UNKNOWN][0]["node_id"] == "ob_0099"


# ---------------------------------------------------------------------------
# 3. Multi-fairy input merges; _source_fairy is set
# ---------------------------------------------------------------------------


def test_multi_fairy_input_merged():
    """Six fairy reports, mixed suggestion types — all findings appear in buckets."""
    reports = [
        _make_report([_make_finding("qu_0001", "close as resolved")], fairy_id="fairy-1"),
        _make_report([_make_finding("ct_0002", "resolve contradiction stale-by-premise")], fairy_id="fairy-2"),
        _make_report([_make_finding("ob_0003", "supersede with updated framing")], fairy_id="fairy-3"),
        _make_report([_make_finding("dv_0004", "retract due to tainted premise")], fairy_id="fairy-4"),
        _make_report([_make_finding("dv_0005", "anchor as cornerstone")], fairy_id="fairy-5"),
        _make_report([_make_finding("qu_0006", "derive synthesis from ob_0001 and ob_0002")], fairy_id="fairy-6"),
    ]
    buckets = bucket_findings(reports)

    all_findings = [f for b in buckets.values() for f in b]
    assert len(all_findings) == 6

    # Check _source_fairy is set on each finding
    source_fairies = {f["_source_fairy"] for f in all_findings}
    assert source_fairies == {"fairy-1", "fairy-2", "fairy-3", "fairy-4", "fairy-5", "fairy-6"}


def test_source_fairy_label_from_fairy_id():
    """_source_fairy uses fairy_id when present."""
    finding = _make_finding("ob_0001", "close as resolved")
    report = {"fairy_id": "dream-fairy-3", "findings": [finding]}
    buckets = bucket_findings([report])
    items = [f for b in buckets.values() for f in b]
    assert items[0]["_source_fairy"] == "dream-fairy-3"


def test_source_fairy_label_falls_back_to_category():
    """_source_fairy falls back to category when fairy_id absent."""
    finding = _make_finding("ob_0001", "close as resolved")
    report = {"category": 5, "findings": [finding]}
    buckets = bucket_findings([report])
    items = [f for b in buckets.values() for f in b]
    assert items[0]["_source_fairy"] == 5


# ---------------------------------------------------------------------------
# 4. bucket_findings — empty / malformed input handled safely
# ---------------------------------------------------------------------------


def test_empty_fairy_reports():
    """Empty fairy_reports list returns empty buckets."""
    buckets = bucket_findings([])
    for name in ALL_BUCKET_NAMES:
        assert buckets[name] == [], f"Bucket {name} should be empty"


def test_report_with_empty_findings():
    """Report with findings=[] produces no items in any bucket."""
    report = {"fairy_id": "fairy-1", "findings": []}
    buckets = bucket_findings([report])
    total = sum(len(v) for v in buckets.values())
    assert total == 0


def test_report_with_missing_findings_key():
    """Report without 'findings' key produces no items."""
    report = {"fairy_id": "fairy-1"}  # no 'findings' key
    buckets = bucket_findings([report])
    total = sum(len(v) for v in buckets.values())
    assert total == 0


def test_report_with_non_list_findings():
    """Report with findings=None / string is skipped safely."""
    for bad_findings in (None, "not a list", 42):
        report = {"fairy_id": "fairy-1", "findings": bad_findings}
        buckets = bucket_findings([report])
        total = sum(len(v) for v in buckets.values())
        assert total == 0, f"Expected 0 items for findings={bad_findings!r}"


def test_non_dict_finding_skipped():
    """Non-dict items in findings list are skipped without error."""
    report = {"fairy_id": "fairy-1", "findings": [None, "bad", {"node_id": "ob_0001", "suggestion": "retract"}]}
    buckets = bucket_findings([report])
    # Only the valid dict finding should appear
    total = sum(len(v) for v in buckets.values())
    assert total == 1


# ---------------------------------------------------------------------------
# 5. _bucket traceability key on every finding
# ---------------------------------------------------------------------------


def test_bucket_key_added_to_every_finding():
    """Every finding in buckets has a '_bucket' key matching the bucket it's in."""
    reports = [
        _make_report([
            _make_finding("qu_0001", "close as resolved"),
            _make_finding("ob_0002", "supersede with updated claim"),
            _make_finding("ob_0003", "something novel"),
        ], fairy_id="fairy-1"),
    ]
    buckets = bucket_findings(reports)
    for bucket_name, items in buckets.items():
        for item in items:
            assert item["_bucket"] == bucket_name, (
                f"finding in bucket {bucket_name} has _bucket={item['_bucket']!r}"
            )


def test_original_finding_not_mutated():
    """bucket_findings must not mutate the original finding dict."""
    finding = _make_finding("ob_0001", "close as resolved")
    original_keys = set(finding.keys())
    report = _make_report([finding])
    bucket_findings([report])
    assert set(finding.keys()) == original_keys, "Original finding was mutated"


# ---------------------------------------------------------------------------
# 6. check_snapshot_divergence — clean snapshot passes silently
# ---------------------------------------------------------------------------


def test_clean_snapshot_passes():
    """Snapshot matching DB state raises nothing."""
    snapshot = {
        "status": "open",
        "is_current": True,
        "superseded_by": None,
        "confidence": 0.75,
    }
    current = {
        "status": "open",
        "is_current": 1,          # DB stores as int
        "superseded_by": None,
        "confidence": 0.75,
    }
    check_snapshot_divergence("qu_0001", snapshot, current)  # must not raise


# ---------------------------------------------------------------------------
# 7. check_snapshot_divergence — is_current divergence raises
# ---------------------------------------------------------------------------


def test_is_current_divergence_raises():
    """If snapshot says current=True but DB shows 0, raise SnapshotDivergence."""
    snapshot = {"status": "open", "is_current": True, "superseded_by": None}
    current = {"status": "open", "is_current": 0, "superseded_by": "ob_0099"}
    try:
        check_snapshot_divergence("ob_0001", snapshot, current)
        assert False, "Expected SnapshotDivergence to be raised"
    except SnapshotDivergence as e:
        assert e.node_id == "ob_0001"
        assert e.field == "is_current"
        assert e.snapshot_value is True
        assert e.current_value == 0


def test_is_current_stays_false_no_divergence():
    """snapshot is_current=False, DB also 0 — not a divergence (consistent)."""
    snapshot = {"is_current": False}
    current = {"is_current": 0}
    check_snapshot_divergence("ob_0001", snapshot, current)  # no raise


# ---------------------------------------------------------------------------
# 8. check_snapshot_divergence — status divergence raises
# ---------------------------------------------------------------------------


def test_status_divergence_raises():
    """If status changed since dispatch, raise SnapshotDivergence."""
    snapshot = {"status": "open", "is_current": True}
    current = {"status": "resolved", "is_current": 1}
    try:
        check_snapshot_divergence("qu_0002", snapshot, current)
        assert False, "Expected SnapshotDivergence"
    except SnapshotDivergence as e:
        assert e.node_id == "qu_0002"
        assert e.field == "status"
        assert e.snapshot_value == "open"
        assert e.current_value == "resolved"


def test_status_matches_no_divergence():
    snapshot = {"status": "active"}
    current = {"status": "active"}
    check_snapshot_divergence("ob_0003", snapshot, current)


# ---------------------------------------------------------------------------
# 9. check_snapshot_divergence — superseded_by divergence raises
# ---------------------------------------------------------------------------


def test_superseded_by_divergence_raises():
    """If snapshot had no superseded_by but DB now has one, raise."""
    snapshot = {"status": "active", "superseded_by": None, "is_current": True}
    current = {"status": "active", "superseded_by": "ob_0100", "is_current": 0}
    try:
        check_snapshot_divergence("ob_0004", snapshot, current)
        assert False, "Expected SnapshotDivergence"
    except SnapshotDivergence as e:
        assert e.field in ("is_current", "superseded_by"), (
            f"Expected divergence on is_current or superseded_by, got {e.field}"
        )


def test_superseded_by_both_null_no_divergence():
    snapshot = {"superseded_by": None}
    current = {"superseded_by": None}
    check_snapshot_divergence("ob_0005", snapshot, current)


# ---------------------------------------------------------------------------
# 10. check_snapshot_divergence — partial snapshot (missing fields) passes
# ---------------------------------------------------------------------------


def test_partial_snapshot_no_divergence():
    """If snapshot omits a field, that field is not checked."""
    # snapshot omits is_current and superseded_by
    snapshot = {"status": "active"}
    current = {"status": "active", "is_current": 0, "superseded_by": "ob_0099"}
    check_snapshot_divergence("ob_0006", snapshot, current)  # must not raise


def test_empty_snapshot_always_passes():
    """Empty snapshot performs no checks — always passes."""
    snapshot = {}
    current = {"status": "retracted", "is_current": 0, "superseded_by": "ob_0099"}
    check_snapshot_divergence("ob_0007", snapshot, current)  # no raise


# ---------------------------------------------------------------------------
# 11. SnapshotDivergence — attributes accessible for dream-record logging
# ---------------------------------------------------------------------------


def test_snapshot_divergence_attributes():
    e = SnapshotDivergence("qu_0001", "status", "open", "resolved")
    assert e.node_id == "qu_0001"
    assert e.field == "status"
    assert e.snapshot_value == "open"
    assert e.current_value == "resolved"
    assert "qu_0001" in e.message
    assert "status" in e.message
    assert "Skip this finding" in e.message
    assert str(e) == e.message


# ---------------------------------------------------------------------------
# 12. BatchResult — accumulates executed / skipped entries
# ---------------------------------------------------------------------------


def test_batch_result_accumulates():
    result = BatchResult(BUCKET_RESOLUTIONS)
    f1 = _make_finding("qu_0001", "close as resolved")
    f2 = _make_finding("qu_0002", "close as resolved")
    f3 = _make_finding("qu_0003", "close as resolved")

    result.record_executed(f1, mcp_result={"status": "resolution_created"})
    result.record_executed(f2, mcp_result={"status": "resolution_created"})

    divergence = SnapshotDivergence("qu_0003", "status", "open", "resolved")
    result.record_skip_divergence(f3, divergence)

    summary = result.summary()
    assert summary["bucket"] == BUCKET_RESOLUTIONS
    assert summary["executed"] == 2
    assert summary["skipped_diverged"] == 1
    assert summary["skipped_other"] == 0
    assert summary["total"] == 3


def test_batch_result_record_skip_other():
    result = BatchResult(BUCKET_SUPERSEDES)
    finding = _make_finding("ob_0001", "supersede with newer")
    result.record_skip_other(finding, reason="node_not_found")
    summary = result.summary()
    assert summary["skipped_other"] == 1
    assert result.skipped_other[0]["reason"] == "node_not_found"


# ---------------------------------------------------------------------------
# 13. Resolution bucket execution sequence — divergence fires before write
# ---------------------------------------------------------------------------


def test_resolution_bucket_divergence_fires_before_write():
    """Dream-master pattern: check divergence BEFORE calling engram_resolve.

    Simulates the dream-master loop for a resolution bucket:
      - finding-A: snapshot matches DB → write fires
      - finding-B: snapshot diverges (status changed) → write is skipped
      - finding-C: snapshot matches DB → write fires
    """
    # Synthetic "DB" rows keyed by node_id
    db_state = {
        "qu_0001": {"status": "open", "is_current": 1, "superseded_by": None},
        "qu_0002": {"status": "resolved", "is_current": 1, "superseded_by": None},  # already resolved
        "qu_0003": {"status": "open", "is_current": 1, "superseded_by": None},
    }

    findings = [
        _make_finding("qu_0001", "close as resolved", status="open"),
        _make_finding("qu_0002", "close as resolved", status="open"),  # snapshot says open; DB says resolved
        _make_finding("qu_0003", "close as resolved", status="open"),
    ]
    report = _make_report(findings)
    buckets = bucket_findings([report])
    resolution_findings = buckets[BUCKET_RESOLUTIONS]
    assert len(resolution_findings) == 3

    result = BatchResult(BUCKET_RESOLUTIONS)
    written_node_ids: list[str] = []

    for finding in resolution_findings:
        nid = finding["node_id"]
        snapshot = finding.get("node_snapshot", {})
        current = db_state[nid]
        try:
            check_snapshot_divergence(nid, snapshot, current)
            # Simulated MCP write
            written_node_ids.append(nid)
            result.record_executed(finding, mcp_result={"status": "resolution_created"})
        except SnapshotDivergence as div:
            result.record_skip_divergence(finding, div)

    assert written_node_ids == ["qu_0001", "qu_0003"], (
        f"Only non-diverged findings should be written; got {written_node_ids}"
    )
    summary = result.summary()
    assert summary["executed"] == 2
    assert summary["skipped_diverged"] == 1


# ---------------------------------------------------------------------------
# 14. Goal-tension findings route to goal_tension_resolutions, not resolutions
# ---------------------------------------------------------------------------


def test_goal_tension_routes_separately():
    """gt_* findings must not land in BUCKET_RESOLUTIONS."""
    gt_finding = {
        "node_id": "gt_0001",
        "node_snapshot": {
            "claim": "Tension between exploration and rigor",
            "status": "open",
            "is_current": True,
            "superseded_by": None,
        },
        "suggestion": "resolve goal-tension gt_0001 via prioritization derivation",
        "rationale": "Rigor wins in high-stakes graph operations.",
        "verification_state": "snapshot taken at 2026-05-27T12:00:00Z",
    }
    report = _make_report([gt_finding], fairy_id="fairy-2")
    buckets = bucket_findings([report])

    assert len(buckets[BUCKET_GOAL_TENSION_RESOLUTIONS]) == 1, (
        f"Goal-tension finding should be in goal_tension_resolutions bucket, "
        f"got resolutions={buckets[BUCKET_RESOLUTIONS]}"
    )
    assert len(buckets[BUCKET_RESOLUTIONS]) == 0, (
        f"resolutions bucket should be empty; got {buckets[BUCKET_RESOLUTIONS]}"
    )


# ---------------------------------------------------------------------------
# 15. Goal-tension routing handles reverse word-order ("resolve tension")
# ---------------------------------------------------------------------------


def test_resolve_first_tension_second_routes_to_goal_tension():
    """'resolve tension' (resolve-first) must route to goal_tension_resolutions.

    Regression guard for SUGGESTION_ROUTING pattern \bresolv.*\btension\b.
    A fairy writing "resolve the tension between X and Y" must not slip through
    to BUCKET_RESOLUTIONS just because "resolve" precedes "tension".
    """
    suggestions = [
        "resolve tension between exploration and rigor",
        "resolve the tension between gl_0001 and gl_0002",
        "resolving tension here — consider a synthesis derivation",
    ]
    for suggestion in suggestions:
        finding = _make_finding("gt_0010", suggestion)
        report = _make_report([finding])
        buckets = bucket_findings([report])
        assert buckets[BUCKET_GOAL_TENSION_RESOLUTIONS], (
            f"Expected goal_tension_resolutions for {suggestion!r}, "
            f"but got resolutions={buckets[BUCKET_RESOLUTIONS]}"
        )
        assert not buckets[BUCKET_RESOLUTIONS], (
            f"BUCKET_RESOLUTIONS must be empty for {suggestion!r}"
        )


# ---------------------------------------------------------------------------
# 16. gt_-prefixed node_id without goal-tension keywords in suggestion text
# ---------------------------------------------------------------------------


def test_gt_id_prefix_without_keywords_routes_by_suggestion_text():
    """Routing is purely suggestion-text based; node_id prefix is NOT consulted.

    Design assumption: if a fairy files a gt_* finding but the suggestion text
    contains no "goal-tension" / "gt_" / "tension" keywords, the routing falls
    through to whatever the suggestion text says.  This test documents that
    assumption and guards against silent behaviour changes.

    Example: node_id="gt_0007", suggestion="close as resolved" (no tension
    keyword) → BUCKET_RESOLUTIONS (the generic resolv pattern wins), NOT
    BUCKET_GOAL_TENSION_RESOLUTIONS.

    If this routing behaviour is intentionally changed (e.g., to consult the
    ID prefix), this test should be updated to reflect the new design.
    """
    finding = _make_finding("gt_0007", "close as resolved")
    report = _make_report([finding])
    buckets = bucket_findings([report])
    # The suggestion "close as resolved" has no tension/goal-tension keywords,
    # so it falls to the generic \bresolv pattern → BUCKET_RESOLUTIONS.
    assert len(buckets[BUCKET_RESOLUTIONS]) == 1, (
        f"gt_ node with plain 'close as resolved' suggestion should route to "
        f"BUCKET_RESOLUTIONS (routing is suggestion-text-only); "
        f"got goal_tension_resolutions={buckets[BUCKET_GOAL_TENSION_RESOLUTIONS]}"
    )
    assert len(buckets[BUCKET_GOAL_TENSION_RESOLUTIONS]) == 0, (
        "BUCKET_GOAL_TENSION_RESOLUTIONS should be empty when suggestion has "
        "no goal-tension keywords, even if node_id starts with gt_"
    )


# ---------------------------------------------------------------------------
# 17. fetch_safety_row + check_snapshot_divergence: full stale-detection path
#
# Exercises the BLOCKER fix: fetch_safety_row supplies a fresh current_db_row
# so check_snapshot_divergence has real DB state to compare against.
# ---------------------------------------------------------------------------


def test_fetch_safety_row_detects_stale_snapshot():
    """fetch_safety_row + check_snapshot_divergence catches a superseded node.

    Scenario: fairy snapshot shows node as current (is_current=True, status="open").
    Between fairy dispatch and dream-master execution, the node was superseded
    (is_current=0 in DB).  fetch_safety_row returns the fresh DB state; the
    guard detects divergence and raises SnapshotDivergence.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, is_current INTEGER, "
        "status TEXT, superseded_by TEXT)"
    )
    # DB state: node was superseded after fairy dispatch
    conn.execute(
        "INSERT INTO nodes VALUES (?, ?, ?, ?)",
        ("qu_0010", 0, "open", "qu_0099"),
    )
    conn.commit()

    # Fairy snapshot captured the node as still current
    snapshot = {"is_current": True, "status": "open", "superseded_by": None}

    current = fetch_safety_row(conn, "qu_0010")
    assert current is not None, "fetch_safety_row should find the row"
    assert current["is_current"] == 0

    try:
        check_snapshot_divergence("qu_0010", snapshot, current)
        assert False, "Expected SnapshotDivergence to be raised"
    except SnapshotDivergence as e:
        assert e.node_id == "qu_0010"
        assert e.field == "is_current"


def test_fetch_safety_row_returns_none_for_missing_node():
    """fetch_safety_row returns None when the node does not exist in DB."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, is_current INTEGER, "
        "status TEXT, superseded_by TEXT)"
    )
    conn.commit()

    result = fetch_safety_row(conn, "qu_9999")
    assert result is None


def test_fetch_safety_row_clean_node_passes_guard():
    """fetch_safety_row + check_snapshot_divergence: no divergence for a clean node."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, is_current INTEGER, "
        "status TEXT, superseded_by TEXT)"
    )
    conn.execute(
        "INSERT INTO nodes VALUES (?, ?, ?, ?)",
        ("ob_0020", 1, "active", None),
    )
    conn.commit()

    snapshot = {"is_current": True, "status": "active", "superseded_by": None}
    current = fetch_safety_row(conn, "ob_0020")
    check_snapshot_divergence("ob_0020", snapshot, current)  # must not raise


# ---------------------------------------------------------------------------
# 18. check_snapshot_divergence — fail-closed when DB row is missing a field
#
# Exercises the SUGGESTION fix: if the snapshot has a field but the DB row
# does not (e.g. fetch_safety_row returned a row from a schema missing the
# column), the guard must raise — "cannot verify → treat as divergent."
# ---------------------------------------------------------------------------


def test_fail_closed_when_snapshot_has_is_current_but_db_row_missing_field():
    """Guard raises when snapshot has is_current but DB row omits it entirely.

    If the DB row came back without is_current (e.g. partial schema, migration
    gap), we cannot verify the field is still consistent — fail-closed to
    prevent executing on unverifiable state.
    """
    snapshot = {"is_current": True, "status": "open"}
    current = {"status": "open"}  # is_current key absent from DB row

    try:
        check_snapshot_divergence("ob_0050", snapshot, current)
        assert False, "Expected SnapshotDivergence (fail-closed on missing DB field)"
    except SnapshotDivergence as e:
        assert e.node_id == "ob_0050"
        assert e.field == "is_current"


def test_fail_closed_when_snapshot_has_status_but_db_row_missing_field():
    """Guard raises when snapshot has status but DB row omits it entirely."""
    snapshot = {"status": "open"}
    current = {}  # status key absent from DB row

    try:
        check_snapshot_divergence("ob_0051", snapshot, current)
        assert False, "Expected SnapshotDivergence (fail-closed on missing DB field)"
    except SnapshotDivergence as e:
        assert e.node_id == "ob_0051"
        assert e.field == "status"


def test_empty_snapshot_still_passes_with_missing_db_fields():
    """Empty snapshot performs no checks — still passes even if DB row is empty.

    Fail-closed only applies when the SNAPSHOT has the field (we have
    something to compare).  If the snapshot omits a field entirely,
    there's nothing to compare — skip that check.
    """
    snapshot = {}
    current = {}  # both empty — nothing to compare
    check_snapshot_divergence("ob_0052", snapshot, current)  # must not raise


# ---------------------------------------------------------------------------
# 19. check_snapshot_divergence — missing node (fetch_safety_row → None)
#     raises SnapshotDivergence with field="existence", not TypeError
# ---------------------------------------------------------------------------


def test_missing_node_raises_snapshot_divergence_not_type_error():
    """fetch_safety_row returning None must produce SnapshotDivergence(existence).

    Scenario: a node referenced in the fairy report was deleted (or never
    committed) between fairy dispatch and dream-master invocation.
    fetch_safety_row returns None; passing that None to check_snapshot_divergence
    must raise SnapshotDivergence(field="existence") — not TypeError — so the
    caller's existing except SnapshotDivergence handler catches + logs it cleanly.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, is_current INTEGER, "
        "status TEXT, superseded_by TEXT)"
    )
    conn.commit()
    # Table is empty — node does not exist

    missing_row = fetch_safety_row(conn, "ob_9999")
    assert missing_row is None, "fetch_safety_row must return None for absent node"

    snapshot = {"is_current": True, "status": "open", "superseded_by": None}

    try:
        check_snapshot_divergence("ob_9999", snapshot, missing_row)
        assert False, "Expected SnapshotDivergence to be raised for missing node"
    except SnapshotDivergence as e:
        assert e.node_id == "ob_9999"
        assert e.field == "existence", (
            f"Expected field='existence', got {e.field!r}"
        )
        assert e.current_value is None
    except TypeError:
        assert False, (
            "check_snapshot_divergence raised TypeError instead of "
            "SnapshotDivergence for a missing node (None current_db_row)"
        )


# ---------------------------------------------------------------------------
# 20. edge_wiring bucket — Category 7 suggestion strings route correctly
# ---------------------------------------------------------------------------


class TestEdgeWiringBucket:
    """Category 7 missing-principle-edge suggestions route to BUCKET_EDGE_WIRING."""

    def _single(self, suggestion: str) -> str:
        """Run bucket_findings with one finding, return its bucket."""
        finding = _make_finding("ob_0001", suggestion)
        report = _make_report([finding])
        buckets = bucket_findings([report])
        for name, items in buckets.items():
            for item in items:
                if item["node_id"] == "ob_0001":
                    return name
        return BUCKET_UNKNOWN

    def test_real_category7_format_routes_to_edge_wiring(self):
        """The exact Category 7 output format from engram-dream-fairy.md routes correctly.

        Verbatim category-7 format:
          [source_id] → [target_id] (pattern-match) similarity <score> —
          suggested_relation: <instantiates|serves> — evidence: <snippet> —
          suggested action: dream-master wires via
          `engram_add_edge(source_id=..., target_id=..., relation=...)`
        """
        suggestion = (
            "[ob_0042] → [gl_0001] (pattern-match) similarity 0.82 — "
            "suggested_relation: serves — evidence: task work product toward goal — "
            "suggested action: dream-master wires via "
            "`engram_add_edge(source_id=ob_0042, target_id=gl_0001, relation=serves)`"
        )
        assert self._single(suggestion) == BUCKET_EDGE_WIRING

    def test_instantiates_category7_routes_to_edge_wiring(self):
        """Category 7 suggestion with instantiates relation routes to BUCKET_EDGE_WIRING."""
        suggestion = (
            "[dv_0015] → [cs_0001] (pattern-match) similarity 0.79 — "
            "suggested_relation: instantiates — evidence: derived conclusion realizes axiom — "
            "suggested action: dream-master wires via "
            "`engram_add_edge(source_id=dv_0015, target_id=cs_0001, relation=instantiates)`"
        )
        assert self._single(suggestion) == BUCKET_EDGE_WIRING

    def test_engram_add_edge_alone_routes_to_edge_wiring(self):
        """Any suggestion containing 'engram_add_edge' routes to BUCKET_EDGE_WIRING."""
        assert self._single("wire via engram_add_edge to connect source to target") == BUCKET_EDGE_WIRING

    def test_suggested_relation_alone_routes_to_edge_wiring(self):
        """Any suggestion containing 'suggested_relation' routes to BUCKET_EDGE_WIRING."""
        assert self._single("suggested_relation: serves — add edge between nodes") == BUCKET_EDGE_WIRING

    def test_edge_wiring_does_not_shadow_cornerstone(self):
        """engram_add_cornerstone still routes to BUCKET_CORNERSTONE_MOVES."""
        assert self._single("anchor as cornerstone via engram_add_cornerstone") == BUCKET_CORNERSTONE_MOVES

    def test_edge_wiring_does_not_shadow_resolution(self):
        """Plain resolution suggestions still route to BUCKET_RESOLUTIONS."""
        assert self._single("close as resolved") == BUCKET_RESOLUTIONS

    def test_edge_wiring_does_not_shadow_goal_tension(self):
        """Goal-tension suggestions still route to BUCKET_GOAL_TENSION_RESOLUTIONS."""
        assert self._single("resolve goal-tension gt_0003 via synthesis") == BUCKET_GOAL_TENSION_RESOLUTIONS

    def test_edge_wiring_bucket_in_all_bucket_names(self):
        """BUCKET_EDGE_WIRING is registered in ALL_BUCKET_NAMES."""
        assert BUCKET_EDGE_WIRING in ALL_BUCKET_NAMES

    def test_edge_wiring_before_unknown_in_all_bucket_names(self):
        """BUCKET_EDGE_WIRING appears before BUCKET_UNKNOWN in ALL_BUCKET_NAMES."""
        names = list(ALL_BUCKET_NAMES)
        assert names.index(BUCKET_EDGE_WIRING) < names.index(BUCKET_UNKNOWN)


# ---------------------------------------------------------------------------
# 22. Task-closures bucket routing (Category 8)
# ---------------------------------------------------------------------------


class TestTaskClosuresBucket:
    """Routing correctness and containment for the task_closures bucket."""

    def _route(self, suggestion: str) -> str:
        finding = _make_finding("tk_0001", suggestion)
        report = _make_report([finding])
        buckets = bucket_findings([report])
        for name, items in buckets.items():
            for item in items:
                if item["node_id"] == "tk_0001":
                    return name
        return BUCKET_UNKNOWN

    def test_canonical_suggestion_routes_to_task_closures(self):
        assert (
            self._route("close task and mark done — external reference #1251 MERGED")
            == BUCKET_TASK_CLOSURES
        )

    def test_mark_done_variant_routes_to_task_closures(self):
        assert self._route("mark task done — external reference closed PR") == BUCKET_TASK_CLOSURES

    def test_stale_task_variant_routes_to_task_closures(self):
        assert self._route("stale task — reference #999 CLOSED") == BUCKET_TASK_CLOSURES

    def test_does_not_shadow_resolutions(self):
        assert self._route("resolve question via engram_resolve") == BUCKET_RESOLUTIONS

    def test_close_task_not_bare_close(self):
        """Bare 'close' without 'task' should not route to task_closures."""
        result = self._route("close as resolved — no longer relevant")
        assert result != BUCKET_TASK_CLOSURES

    def test_mark_done_without_task_does_not_route_to_task_closures(self):
        """'mark contradiction done' must not shadow resolutions bucket."""
        result = self._route("mark contradiction done after resolution")
        assert result != BUCKET_TASK_CLOSURES

    def test_task_closures_in_all_bucket_names(self):
        assert BUCKET_TASK_CLOSURES in ALL_BUCKET_NAMES

    def test_task_closures_before_unknown_in_all_bucket_names(self):
        names = list(ALL_BUCKET_NAMES)
        assert names.index(BUCKET_TASK_CLOSURES) < names.index(BUCKET_UNKNOWN)

    def test_task_closures_after_edge_wiring_in_all_bucket_names(self):
        names = list(ALL_BUCKET_NAMES)
        assert names.index(BUCKET_EDGE_WIRING) < names.index(BUCKET_TASK_CLOSURES)


# ---------------------------------------------------------------------------
# 21. Regression table — all existing buckets still route unchanged
# ---------------------------------------------------------------------------


class TestRoutingRegressionTable:
    """Representative string for every pre-existing bucket still routes correctly.

    Guards against the edge_wiring entries shadowing earlier semantics.
    """

    def _single(self, suggestion: str) -> str:
        finding = _make_finding("ob_0001", suggestion)
        report = _make_report([finding])
        buckets = bucket_findings([report])
        for name, items in buckets.items():
            for item in items:
                if item["node_id"] == "ob_0001":
                    return name
        return BUCKET_UNKNOWN

    def test_resolutions_still_routes(self):
        assert self._single("close as resolved") == BUCKET_RESOLUTIONS

    def test_supersedes_still_routes(self):
        assert self._single("supersede with fresher derivation") == BUCKET_SUPERSEDES

    def test_retractions_still_routes(self):
        assert self._single("retract — tainted by upstream removal") == BUCKET_RETRACTIONS

    def test_new_derivations_still_routes(self):
        assert self._single("derive synthesis from these three observations") == BUCKET_NEW_DERIVATIONS

    def test_lessons_still_routes(self):
        assert self._single("register incident for lesson ls_0014") == BUCKET_LESSONS

    def test_cornerstone_moves_still_routes(self):
        assert self._single("anchor as cornerstone via engram_add_cornerstone") == BUCKET_CORNERSTONE_MOVES

    def test_goal_tension_resolutions_still_routes(self):
        assert self._single("resolve goal-tension gt_0003 via synthesis") == BUCKET_GOAL_TENSION_RESOLUTIONS

    def test_task_closures_still_routes(self):
        assert self._single("close task and mark done — external reference #1251 MERGED") == BUCKET_TASK_CLOSURES

    def test_coverage_gaps_still_routes(self):
        assert self._single("Add warm-briefing anchor entry for cs_0042 (handle: soma-scope) — no auto-load surface coverage found.") == BUCKET_COVERAGE_GAPS

    def test_unknown_still_falls_through(self):
        assert self._single("a completely novel action type no pattern knows") == BUCKET_UNKNOWN


if __name__ == "__main__":
    import traceback

    tests: list = []
    for name, val in list(globals().items()):
        if name.startswith("test_") and callable(val):
            tests.append((name, val))

    # Also collect class-level tests
    for name, val in list(globals().items()):
        if isinstance(val, type) and name.startswith("Test"):
            for method_name, method in vars(val).items():
                if method_name.startswith("test_") and callable(method):
                    tests.append((f"{name}.{method_name}", lambda m=method, v=val: m(v())))

    passed = 0
    failed = []
    for test_name, test_fn in tests:
        try:
            test_fn()
            passed += 1
            print(f"PASS  {test_name}")
        except Exception:
            failed.append(test_name)
            print(f"FAIL  {test_name}")
            traceback.print_exc()

    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
