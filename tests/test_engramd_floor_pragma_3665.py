"""Contract cells C1 for U6b (#3665): the client-read floor allows the READ form
of `PRAGMA data_version` and nothing else new.

Why: fts5vocab runs `PRAGMA data_version` internally, so the vocab read the typed
`df` op performs is denied by the floor today (measured: `authorization denied`).
Design seat ruling (#3665 comment 6124428822): allowlist `data_version`, READ form
only.

Written BEFORE the implementation (contract-first). Cells run
`engramd_owner._client_read_authorizer` through a REAL sqlite3 connection with the
authorizer set -- no stubbed authorizer arguments.

DISCRIMINATING MUTANT (design seat requirement; run in stage 2, not here).
  Mutant M_ALL_READS: in `_client_read_authorizer`, replace the allowlist check
  for a read-form PRAGMA with a blanket allow, i.e. in the SQLITE_PRAGMA branch,
  after the `if arg2 is not None: return SQLITE_DENY` guard, change
      if arg1 in CLIENT_READ_PRAGMA_ALLOWLIST: return SQLITE_OK
      return SQLITE_DENY
  to
      return SQLITE_OK
  Expected: test_read_form_of_a_pragma_outside_the_allowlist_is_denied (cell 4,
  read-form rows) goes RED; test_data_version_read_form_succeeds (cell 1) stays
  GREEN; the assignment-form cells (2, and 4's `=` rows) stay GREEN because the
  arg2 guard precedes the mutated line. The cells are split exactly so a pragma
  allowlist that is merely widened to "everything" cannot hide behind cell 1.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SRC_ENGRAM = str(Path(__file__).resolve().parent.parent / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_owner as eo  # noqa: E402


@pytest.fixture
def floor_conn(tmp_path):
    """A temp-file store opened with the client-read floor set as the owner sets it."""
    db = tmp_path / "knowledge.db"
    seed = sqlite3.connect(str(db))
    seed.execute("CREATE TABLE nodes (x INTEGER)")
    seed.execute("INSERT INTO nodes VALUES (1)")
    seed.commit()
    seed.close()
    conn = sqlite3.connect(str(db))
    conn.set_authorizer(eo._client_read_authorizer)
    yield conn
    conn.close()


# Pre-change deny set, 16 names (security seat's table, #3665). Each is denied by
# the floor before this change and must stay denied after it. Both forms are
# probed per name: the NAME path (`PRAGMA q`, arg2 None -> decided by the
# allowlist) and the ASSIGNMENT path (`PRAGMA q=v`, arg2 set -> denied by the
# arg2 guard). Mutant M_ALL_READS can only turn the NAME-path rows red.
# `table_info(nodes)` carries its argument in arg2, so it is denied by the arg2
# guard on its own; it is kept because the seat listed it.
# Assignment probes SKIPPED (these pragmas take no assignment form):
#   integrity_check, table_info, database_list, compile_options, freelist_count
# Totals: 16 name probes + 11 assignment probes.
DENIED_READ_FORMS = [
    "journal_mode",
    "writable_schema",
    "foreign_keys",
    "query_only",
    "secure_delete",
    "synchronous",
    "locking_mode",
    "auto_vacuum",
    "page_size",
    "cache_size",
    "temp_store",
    "integrity_check",
    "table_info(nodes)",
    "database_list",
    "compile_options",
    "freelist_count",
]
DENIED_ASSIGN_FORMS = [
    "journal_mode=DELETE",
    "writable_schema=1",
    "foreign_keys=0",
    "query_only=0",
    "secure_delete=1",
    "synchronous=1",
    "locking_mode=EXCLUSIVE",
    "auto_vacuum=1",
    "page_size=8192",
    "cache_size=1",
    "temp_store=2",
]


def test_data_version_read_form_succeeds(floor_conn):
    """C1.1: `PRAGMA data_version` (read form) is allowed and returns one integer row."""
    rows = floor_conn.execute("PRAGMA data_version").fetchall()
    assert len(rows) == 1 and len(rows[0]) == 1
    assert isinstance(rows[0][0], int)


def test_data_version_is_constant_across_two_reads_on_one_connection(floor_conn):
    """C1.1b: `PRAGMA data_version` is inert -- two reads on one connection agree
    (pins that it is harmless, not merely allowed)."""
    first = floor_conn.execute("PRAGMA data_version").fetchone()[0]
    second = floor_conn.execute("PRAGMA data_version").fetchone()[0]
    assert isinstance(first, int) and first == second


def test_data_version_assignment_form_is_denied(floor_conn):
    """C1.2: `PRAGMA data_version=5` (arg2 not None) stays denied."""
    with pytest.raises(sqlite3.DatabaseError):
        floor_conn.execute("PRAGMA data_version=5")


def test_user_version_read_form_still_works(floor_conn):
    """C1.3: the existing allowlisted `PRAGMA user_version` read is unchanged."""
    rows = floor_conn.execute("PRAGMA user_version").fetchall()
    assert len(rows) == 1 and isinstance(rows[0][0], int)


def test_user_version_assignment_form_still_denied(floor_conn):
    """C1.3b: the assignment form of the already-allowlisted `user_version` stays denied."""
    with pytest.raises(sqlite3.DatabaseError):
        floor_conn.execute("PRAGMA user_version=3")


@pytest.mark.parametrize("pragma", DENIED_READ_FORMS)
def test_read_form_of_a_pragma_outside_the_allowlist_is_denied(floor_conn, pragma):
    """C1.4 (read forms): every pre-change denied PRAGMA read is still denied
    (mutant M_ALL_READS turns these red)."""
    with pytest.raises(sqlite3.DatabaseError):
        floor_conn.execute("PRAGMA " + pragma).fetchall()


@pytest.mark.parametrize("pragma", DENIED_ASSIGN_FORMS)
def test_assignment_form_of_a_denied_pragma_is_denied(floor_conn, pragma):
    """C1.4 (assignment forms): every pre-change denied PRAGMA assignment is still denied."""
    with pytest.raises(sqlite3.DatabaseError):
        floor_conn.execute("PRAGMA " + pragma)


def test_allowlist_membership_is_exactly_the_two_read_pragmas():
    """C1.5 support: the allowlist grew by `data_version` only (a wider set is a
    separate decision), and still holds `user_version`."""
    assert eo.CLIENT_READ_PRAGMA_ALLOWLIST == frozenset({"user_version", "data_version"})


def test_stale_pragma_allowlist_comment_is_rewritten():
    """C1.6: the pre-change comment above CLIENT_READ_PRAGMA_ALLOWLIST ("No real
    caller is wired to a pragma yet", "builder's-call design choice, not a
    measured set") is false once `data_version` is allowlisted for a real caller
    (the df op / fts5vocab); the build stage must rewrite it."""
    text = Path(eo.__file__).read_text(encoding="utf-8")
    assert "No real caller is wired to a pragma yet" not in text
    assert "builder's-call design choice, not a measured set" not in text


def test_table_valued_pragma_forms_are_allowed_as_a_pre_existing_fact(floor_conn):
    """F6. DOCUMENTS, does not endorse, current behaviour: the table-valued
    spellings `SELECT * FROM pragma_data_version` and `pragma_user_version` are
    allowed on the floor, while e.g. `pragma_page_size` is denied. This is the
    same class that existed for `pragma_user_version` BEFORE this PR;
    `pragma_data_version` joins it only because the name is allowlisted. Both
    are inert integer reads. The authorizer is deliberately unchanged here; if
    the table-valued path is ever tightened, this is the cell to revisit."""
    for name in ("pragma_data_version", "pragma_user_version"):
        rows = floor_conn.execute("SELECT * FROM " + name).fetchall()
        assert len(rows) == 1 and isinstance(rows[0][0], int), name
    with pytest.raises(sqlite3.DatabaseError):
        floor_conn.execute("SELECT * FROM pragma_page_size").fetchall()
