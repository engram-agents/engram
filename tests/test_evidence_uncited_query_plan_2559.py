"""#2559 -- the dangling-evidence NOT EXISTS query must use the composite
index (evidence_id, is_current), not idx_nodes_current, on a FRESH database
with no query-planner statistics.

Background: this correlated subquery (health/measurements.py's
m_evidence_uncited, and health/metrics.py's dangling_evidence_rate --
the SSoT verification oracle) has two single-column candidate indexes
(idx_nodes_evidence_id, idx_nodes_current). Without ANALYZE/PRAGMA optimize
ever having run (no shipped code path ran either before this issue),
sqlite_stat1 does not exist and the planner has no cardinality information
to prefer the selective index -- it picked idx_nodes_current, effectively
the whole table, making the subquery O(N) per outer row (O(N^2) overall).
Measured: 1187.8ms -> 1.6ms after ANALYZE alone (758x, Ariadne's 4,597-node
graph, forum#390); this issue's fix (a composite index) makes the correct
plan deterministic WITHOUT depending on ANALYZE ever running.

A timing test is the wrong shape here (flaky, environment-dependent -- see
this repo's own precedent of avoiding timing assertions in tests). The
PLAN is the invariant: this test reads EXPLAIN QUERY PLAN directly and
asserts the correlated subquery step names the new composite index, on a
database built via the REAL production schema path (engram_core._get_db(),
not a hand-rolled test-local DDL) with zero rows and zero ANALYZE runs --
exactly the "any fresh seat, no stats" condition the issue is about.

Checked to fail (mutation-tested, not merely asserted): dropping the new
index from a copy of the connection reproduces the original bad plan
(SEARCH o USING INDEX idx_nodes_current), confirming this test would have
failed before the fix and is not vacuous.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
if str(SRC_ENGRAM) not in sys.path:
    sys.path.append(str(SRC_ENGRAM))

_DANGLING_EVIDENCE_QUERY = """
    SELECT COUNT(*) as c FROM nodes n
    WHERE n.type = 'evidence' AND n.is_current = 1
      AND NOT EXISTS (
          SELECT 1 FROM nodes o
          WHERE o.evidence_id = n.id AND o.is_current = 1
      )
"""


def _plan_rows(conn) -> list[str]:
    return [
        row["detail"] if isinstance(row, dict) or hasattr(row, "keys") else row[3]
        for row in conn.execute(
            f"EXPLAIN QUERY PLAN{_DANGLING_EVIDENCE_QUERY}"
        ).fetchall()
    ]


def _subquery_plan_line(plan: list[str]) -> str:
    """The plan line for the correlated subquery's own table scan (aliased
    `o` in the query) -- distinct from the OUTER query's own `SEARCH n ...`
    line, which legitimately uses idx_nodes_current regardless of this
    issue's fix (filtering the outer evidence-type/current rows is a
    different concern from how the correlated subquery resolves its own
    evidence_id/is_current lookup). Asserting against the WHOLE plan text
    would false-positive on the outer line's own correct, unrelated use of
    the same index name."""
    matches = [line for line in plan if line.strip().startswith("SEARCH o ")]
    assert len(matches) == 1, (
        f"expected exactly one 'SEARCH o ...' plan line (the correlated "
        f"subquery's own scan) -- got {len(matches)} in plan: {plan}"
    )
    return matches[0]


class TestDanglingEvidenceQueryPlan:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_2559_"))
        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        import engram_core as core
        self.core = core
        core._configure_paths(self.tmp)
        core._ensure_data_dir()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_stats_plan_uses_composite_index_not_idx_nodes_current(self):
        """The real, load-bearing check: on a fresh DB built via the actual
        production schema path, with zero ANALYZE runs, the correlated
        subquery must use the new composite index -- not the old
        idx_nodes_current full-table-ish scan."""
        conn = self.core._get_db()

        # Positive control: this repo has never run ANALYZE anywhere in a
        # shipped code path, so sqlite_stat1 must not exist here either --
        # if it does, this test isn't exercising the "no stats" condition
        # the issue is about, and its result would say nothing about the
        # bug this fix addresses.
        stat_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_stat%'"
        ).fetchall()
        assert not stat_tables, (
            f"expected no sqlite_stat* tables on a fresh, never-ANALYZE'd "
            f"DB -- found {stat_tables}, which would invalidate this test's "
            f"no-statistics premise"
        )

        plan = _plan_rows(conn)
        subquery_line = _subquery_plan_line(plan)
        assert "idx_nodes_evidence_current" in subquery_line, (
            f"expected the correlated subquery's own scan to use the new "
            f"composite index idx_nodes_evidence_current -- got:\n"
            f"{subquery_line}\n(full plan: {plan})"
        )
        assert "idx_nodes_current" not in subquery_line.replace(
            "idx_nodes_evidence_current", ""
        ), (
            f"the subquery must NOT fall back to the old, effectively-"
            f"full-table idx_nodes_current scan -- got:\n{subquery_line}\n"
            f"(full plan: {plan})"
        )

    def test_dropping_the_new_index_reproduces_the_old_bad_plan(self):
        """Checked to fail, per #2559's own spec: with the new index
        dropped (simulating pre-fix code, on the SAME real schema
        otherwise), the planner reverts to the old bad plan -- confirming
        this test file's positive assertion above is a genuine
        discriminator, not vacuous."""
        conn = self.core._get_db()
        conn.execute("DROP INDEX IF EXISTS idx_nodes_evidence_current")

        plan = _plan_rows(conn)
        subquery_line = _subquery_plan_line(plan)
        assert "idx_nodes_current" in subquery_line, (
            f"expected the pre-fix bad plan (idx_nodes_current) on the "
            f"subquery's own scan once the new composite index is removed "
            f"-- got:\n{subquery_line}\n(full plan: {plan}). If this "
            f"doesn't reproduce, the positive test above may not actually "
            f"be checking what it claims to."
        )
        assert "idx_nodes_evidence_current" not in subquery_line
