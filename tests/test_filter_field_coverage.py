"""Every real `nodes` column must be a DECIDED column.

This is the drift guard for #1955. `ALLOWED_NODE_COLUMNS` in engram_filter is a
hand-maintained allowlist, and a hand-maintained allowlist fails OPEN and
SILENTLY: a column added to the schema is simply never offered by engram_list,
and nobody finds out until an agent needs it.

That already happened. The standpoint-v3 columns landed and were never added
here, so the entire standpoint axis — the one designed to be reasoned about in
aggregate — was reachable only by opening the database directly. Meanwhile the
trust-tier columns, added around the same time, WERE remembered. Same file,
same discipline, opposite outcomes.

The test deliberately does NOT demand that every column be filterable. It
demands that every column be *decided*: either exposed, or listed in
DELIBERATELY_NOT_FILTERABLE with a comment saying why. Without that, "internal
on purpose" and "somebody forgot" are indistinguishable, and that
indistinguishability is the actual defect.

The schema is read from a freshly-initialised database rather than parsed out
of the source, because the columns arrive by two different routes (the base
CREATE TABLE and the idempotent ALTER TABLE migration list). Scraping either
one alone undercounts — which is exactly how the first hand audit of this gap
reported 16 stranded columns when the real number was 18.
"""

import sqlite3

import pytest

from engram_core import _run_db_one_time_setup
from engram_filter import ALLOWED_NODE_COLUMNS, DELIBERATELY_NOT_FILTERABLE


@pytest.fixture
def real_node_columns(tmp_path):
    """Actual `nodes` columns, from a fresh DB — the authoritative source."""
    conn = sqlite3.connect(tmp_path / "schema_probe.db")
    conn.row_factory = sqlite3.Row
    try:
        _run_db_one_time_setup(conn)
        return {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    finally:
        conn.close()


def test_every_column_is_either_filterable_or_explicitly_excluded(real_node_columns):
    undecided = real_node_columns - ALLOWED_NODE_COLUMNS - DELIBERATELY_NOT_FILTERABLE
    assert not undecided, (
        f"New `nodes` column(s) {sorted(undecided)} are not reachable via engram_list "
        f"and are not declared as deliberately internal.\n\n"
        f"Decide which they are:\n"
        f"  - agents should be able to query it  -> add to ALLOWED_NODE_COLUMNS\n"
        f"  - internal/blob/instrumentation      -> add to DELIBERATELY_NOT_FILTERABLE "
        f"with a comment saying why\n\n"
        f"Silence is the failure mode this test exists to prevent: an undeclared "
        f"column is invisible to every agent who would not think to open the DB."
    )


def test_no_filterable_field_is_a_phantom(real_node_columns):
    """A name in the allowlist that is not a real column fails at query time."""
    phantom = ALLOWED_NODE_COLUMNS - real_node_columns
    assert not phantom, (
        f"ALLOWED_NODE_COLUMNS names {sorted(phantom)}, which are not real `nodes` "
        f"columns. Filtering on them would produce a SQL error rather than the "
        f"friendly 'unknown field' message the allowlist exists to give."
    )


def test_the_two_sets_do_not_overlap(real_node_columns):
    """A column cannot be both exposed and declared internal."""
    both = ALLOWED_NODE_COLUMNS & DELIBERATELY_NOT_FILTERABLE
    assert not both, (
        f"{sorted(both)} appear in BOTH ALLOWED_NODE_COLUMNS and "
        f"DELIBERATELY_NOT_FILTERABLE. The exclusion list is documentation of a "
        f"decision; a column in both means the decision is unreadable."
    )


def test_standpoint_axis_is_queryable(real_node_columns):
    """Regression guard for the specific gap that motivated #1955.

    Pinned by name rather than left to the generic test above: these five are
    the reason the drift went unnoticed for months, and a future refactor that
    quietly drops them back into the exclusion set should fail loudly rather
    than pass by satisfying 'every column is decided'.
    """
    standpoint = {c for c in real_node_columns if c.startswith("standpoint_")}
    assert standpoint, "no standpoint_* columns found — schema probe is wrong"
    missing = standpoint - ALLOWED_NODE_COLUMNS
    assert not missing, (
        f"standpoint column(s) {sorted(missing)} are not filterable. The standpoint "
        f"axis exists to answer population questions about evidence diversity; "
        f"making it unqueryable defeats its purpose."
    )
