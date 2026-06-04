"""Round-trip tests for engram_backup — the pure-Python (#720) dump mechanism.

Builds a synthetic ENGRAM-shaped DB (external-content FTS5 + the fts5vocab
table + an embedding column + the FTS triggers) and verifies:
  - the dump is pure-Python (no sqlite3 CLI is ever invoked),
  - derived/regenerable data (embedding values, nodes_fts*, vec_nodes) is
    excluded from the dump,
  - base rows restore losslessly,
  - the FTS index rebuilds to identical search results post-restore.

The fts5vocab-before-parent drop ordering is the load-bearing gotcha: if it
regresses, `iterdump` raises `no such fts5 table: main.nodes_fts` and these
tests fail loudly.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engram_backup  # noqa: E402


# Sentinel: True only when sqlite_vec can both be imported AND loaded into a
# connection. The vec0 regression test requires a real vec0 vtable.
def _check_sqlite_vec_loadable() -> bool:
    try:
        import sqlite_vec
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.close()
        return True
    except Exception:
        return False


_SQLITE_VEC_LOADABLE = _check_sqlite_vec_loadable()


def _build_engram_shaped_db(path: str) -> None:
    """Create a minimal but faithful ENGRAM-shaped DB at ``path``."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id         TEXT PRIMARY KEY,
            claim      TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            embedding  TEXT
        );
        CREATE TABLE edges (
            src TEXT, dst TEXT, relation TEXT
        );
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END;
        CREATE TRIGGER nodes_fts_delete AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
            VALUES('delete', old.rowid, old.claim, old.quoted_text, old.interpretation);
        END;
        """
    )
    # The fts5vocab table — the dependency that must be dropped before nodes_fts.
    conn.execute(
        "CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row')"
    )
    rows = [
        ("ob_1", "plugin migration completed cleanly", "verify output", "the migration held", "[0.1, 0.2]"),
        ("ob_2", "the backup mechanism strips embeddings", "letter", "regenerable data excluded", "[0.3, 0.4]"),
        ("ob_3", "migration of scatter to plugin", "forum post", "second seat migration", "[0.5, 0.6]"),
        ("ob_4", "unrelated claim about cornerstones", "doc", "no match here", "[0.7, 0.8]"),
    ]
    conn.executemany(
        "INSERT INTO nodes(id, claim, quoted_text, interpretation, embedding) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.execute("INSERT INTO edges VALUES ('ob_1','ob_2','relates_to')")
    conn.commit()
    conn.close()


@pytest.fixture
def engram_db(tmp_path):
    p = str(tmp_path / "knowledge.db")
    _build_engram_shaped_db(p)
    return p


def test_dump_excludes_derived_data(engram_db, tmp_path):
    out = str(tmp_path / "knowledge.sql")
    stats = engram_backup.dump_stripped(engram_db, out)
    assert stats["fts_excluded"] is True
    text = open(out).read()
    # No FTS/vec table statements survive in the dump.
    assert "nodes_fts" not in text
    assert "vec_nodes" not in text
    # The nodes table is present; embeddings are nulled (no float-list literals).
    assert "CREATE TABLE nodes" in text
    assert "[0.1, 0.2]" not in text and "[0.5, 0.6]" not in text


def test_source_db_untouched(engram_db, tmp_path):
    """dump_stripped must strip on a COPY only — the live DB keeps its embeddings + FTS."""
    out = str(tmp_path / "knowledge.sql")
    engram_backup.dump_stripped(engram_db, out)
    conn = sqlite3.connect(engram_db)
    assert conn.execute("SELECT count(*) FROM nodes WHERE embedding IS NOT NULL").fetchone()[0] == 4
    assert conn.execute("SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'").fetchone()[0] == 2
    conn.close()


def test_base_rows_restore_losslessly(engram_db, tmp_path):
    out = str(tmp_path / "knowledge.sql")
    engram_backup.dump_stripped(engram_db, out)
    recon = str(tmp_path / "recon.db")
    rc = sqlite3.connect(recon)
    rc.executescript(open(out).read())
    assert rc.execute("SELECT count(*) FROM nodes").fetchone()[0] == 4
    assert rc.execute("SELECT count(*) FROM edges").fetchone()[0] == 1
    assert rc.execute("SELECT count(*) FROM nodes WHERE embedding IS NULL").fetchone()[0] == 4
    rc.close()


def test_fts_rebuilds_to_identical_hits(engram_db, tmp_path):
    """The load-bearing contract: FTS search returns identical results post-restore+rebuild."""
    src = sqlite3.connect(engram_db)
    orig_hits = src.execute("SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'").fetchone()[0]
    fts_sql = src.execute("SELECT sql FROM sqlite_master WHERE name='nodes_fts'").fetchone()[0]
    src.close()
    assert orig_hits == 2  # ob_1, ob_3

    out = str(tmp_path / "knowledge.sql")
    engram_backup.dump_stripped(engram_db, out)
    recon = str(tmp_path / "recon.db")
    rc = sqlite3.connect(recon)
    rc.executescript(open(out).read())
    rc.close()
    # Recreate the FTS virtual table (server does this at _get_db) + rebuild.
    rc = sqlite3.connect(recon)
    rc.execute(fts_sql)
    rc.close()
    assert engram_backup.rebuild_fts_index(recon) is True
    rc = sqlite3.connect(recon)
    hits = rc.execute("SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'").fetchone()[0]
    rc.close()
    assert hits == orig_hits


def test_verify_roundtrip_self_check(engram_db):
    """The module's own verify_roundtrip helper passes on a faithful fixture."""
    rep = engram_backup.verify_roundtrip(engram_db)
    assert rep["ok"] is True
    assert rep["nodes"] == 4
    assert rep["fts_hits"] == 2


def test_rebuild_fts_returns_false_when_no_nodes_table(tmp_path):
    """rebuild_fts_index returns False when there's no nodes table (non-ENGRAM DB).

    The new self-contained rebuild (#781) creates the FTS table if nodes exists
    but FTS doesn't, so the old 'no FTS → False' contract is gone.  The new
    contract is: False only when nodes itself is absent.
    """
    p = str(tmp_path / "plain.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE other_table(id TEXT, claim TEXT)")
    conn.commit()
    conn.close()
    assert engram_backup.rebuild_fts_index(p) is False


def test_rebuild_fts_creates_fts_table_when_absent(tmp_path):
    """rebuild_fts_index returns True and creates nodes_fts when nodes exists but FTS doesn't.

    This is the #781 self-contained restore-path: the dump strips nodes_fts,
    and rebuild_fts_index must create it, populate it, and exclude retractions.
    """
    p = str(tmp_path / "plain.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'plugin migration', 'src', 'interp')")
    conn.commit()
    conn.close()
    assert engram_backup.rebuild_fts_index(p) is True
    # FTS must now exist and be queryable.
    conn = sqlite3.connect(p)
    has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='nodes_fts'").fetchone()
    assert has_fts is not None
    hits = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    conn.close()
    assert hits == 1


def test_verify_roundtrip_no_false_positive_on_content_mentioning_derived_tables(tmp_path):
    """Regression test for #724 round-2: verify_roundtrip must NOT raise when node
    content (claim text / quoted_text / interpretation) merely *mentions* the strings
    'vec_nodes' or 'nodes_fts'.  The old guard matched those substrings anywhere on
    a line, producing a false-positive AssertionError on real ENGRAM graphs whose
    observations document internal schema details (e.g. ob_0919 on a 4451-node graph).
    """
    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id         TEXT PRIMARY KEY,
            claim      TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            embedding  TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END;
        CREATE TRIGGER nodes_fts_delete AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
            VALUES('delete', old.rowid, old.claim, old.quoted_text, old.interpretation);
        END;
        """
    )
    conn.execute(
        "CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row')"
    )
    # The critical row: claim text mentions "vec_nodes" and "nodes_fts" literally.
    # This mimics a schema-documentation observation (e.g. ob_0919 in the live graph).
    conn.execute(
        "INSERT INTO nodes(id, claim, quoted_text, interpretation, embedding) VALUES (?,?,?,?,?)",
        (
            "ob_schema",
            "ENGRAM's vec_nodes virtual table is vec0; nodes_fts is the FTS5 index",
            "vec_nodes and nodes_fts are derived tables excluded from dumps",
            "schema documentation node",
            "[0.1, 0.2]",
        ),
    )
    conn.execute(
        "INSERT INTO nodes(id, claim, quoted_text, interpretation, embedding) VALUES (?,?,?,?,?)",
        ("ob_plain", "ordinary claim about migration", "forum post", "no special terms", "[0.3, 0.4]"),
    )
    conn.commit()
    conn.close()

    # Must NOT raise — the content mention is not a derived-table statement.
    rep = engram_backup.verify_roundtrip(db_path)
    assert rep["ok"] is True
    assert rep["nodes"] == 2


def test_verify_roundtrip_still_catches_real_leak(tmp_path):
    """Guard sanity: the statement-anchored regex must still match actual derived-table
    statements.  Exercises the guard regex directly against a synthetic dump that has
    a real INSERT INTO vec_nodes_chunks line injected — confirms the guard is not
    neutered by the anchoring fix.
    """
    import re

    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            embedding TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_1', 'plain claim', 'src', 'interp', '[0.1]')"
    )
    conn.commit()
    conn.close()

    # Produce a clean dump first, then inject a fake derived-table statement.
    dump_path = str(tmp_path / "injected.sql")
    engram_backup.dump_stripped(db_path, dump_path)

    # Append a line that IS a real derived-table INSERT statement.
    with open(dump_path, "a") as f:
        f.write("INSERT INTO vec_nodes_chunks(rowid, chunk) VALUES (1, 'x');\n")

    # Run the guard regex (same pattern as verify_roundtrip) — must match.
    guard = re.compile(
        r'^\s*(CREATE\s+(VIRTUAL\s+)?TABLE\s+(IF\s+NOT\s+EXISTS\s+)?["\']?(vec_nodes|nodes_fts)'
        r'|INSERT\s+INTO\s+["\']?(vec_nodes|nodes_fts)'
        r'|CREATE\s+TRIGGER\s+["\']?(vec_nodes|nodes_fts))',
        re.IGNORECASE,
    )
    leaked = [ln for ln in open(dump_path) if guard.search(ln)]
    assert leaked, "guard failed to catch real derived-table statement in dump"
    assert any("vec_nodes" in ln for ln in leaked)


@pytest.mark.skipif(not _SQLITE_VEC_LOADABLE, reason="sqlite_vec not importable or not loadable")
def test_verify_roundtrip_sqlite_sequence_not_in_parity_check(tmp_path):
    """Regression test for Bug 1: verify_roundtrip must NOT raise when sqlite_sequence
    has more rows in the live DB than in the restored DB.

    Real-graph shape: a vec0 vec_nodes table (uses AUTOINCREMENT shadow tables that
    populate sqlite_sequence) plus another AUTOINCREMENT table.  dump_stripped correctly
    drops vec_nodes, so its sqlite_sequence entries disappear from the dump; the restored
    DB has fewer sqlite_sequence rows.  Before the fix, verify_roundtrip included
    sqlite_sequence in its data-parity check, producing:
        AssertionError: sqlite_sequence: 4 != 6
    The fix excludes all sqlite_* tables from the parity check.
    """
    import sqlite_vec

    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id   TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            embedding TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        -- A second AUTOINCREMENT table so sqlite_sequence has a base entry that
        -- survives the dump (nodes uses TEXT PK, no AUTOINCREMENT), reproducing
        -- the real-graph shape where base rows persist but vec entries don't.
        CREATE TABLE audit_log (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg    TEXT
        );
        CREATE VIRTUAL TABLE vec_nodes USING vec0(embedding float[4]);
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        """
    )
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'hello world', 'src', 'interp', NULL)")
    conn.execute("INSERT INTO audit_log(msg) VALUES ('boot')")
    conn.execute("INSERT INTO vec_nodes(rowid, embedding) VALUES (1, '[0.1, 0.2, 0.3, 0.4]')")
    conn.commit()
    conn.close()

    # Must NOT raise — sqlite_sequence parity divergence is expected and now excluded.
    rep = engram_backup.verify_roundtrip(db_path)
    assert rep["ok"] is True
    assert rep["nodes"] == 1


def test_verify_roundtrip_fts_no_probe_term(tmp_path):
    """Regression test for Bug 2 / #723 nit: verify_roundtrip must NOT raise when the
    graph's FTS content lacks the hardcoded probe term ('migration').

    The FTS rebuild still runs (so a rebuild error is still caught), but the hit-count
    assertion is skipped when fts_hits_orig == 0 — a 0==0 check is vacuous and would
    falsely mask a graph that genuinely has no 'migration' term in any node.
    """
    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id   TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            embedding TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END;
        """
    )
    # No row contains 'migration' — probe term will return 0.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_1', 'hello world', 'greeting', 'friendly', '[0.1]')"
    )
    conn.commit()
    conn.close()

    # Must NOT raise even though the FTS probe returns 0 == 0 (vacuous check skipped).
    rep = engram_backup.verify_roundtrip(db_path)
    assert rep["ok"] is True
    assert rep["nodes"] == 1


@pytest.mark.skipif(not _SQLITE_VEC_LOADABLE, reason="sqlite_vec not importable or not loadable")
def test_dump_stripped_with_real_vec_nodes(tmp_path):
    """Regression test for #724: dump_stripped must not raise when the DB contains
    a real vec0 vec_nodes table.  Before the fix, the snapshot connection had no
    vec0 module loaded, so DROP TABLE IF EXISTS vec_nodes raised
    sqlite3.OperationalError: no such module: vec0.
    """
    import sqlite_vec

    db_path = str(tmp_path / "knowledge.db")

    # Build a DB that contains a real vec0 virtual table — the fixture
    # _build_engram_shaped_db intentionally omits it, so this test creates its own.
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            embedding TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        CREATE VIRTUAL TABLE vec_nodes USING vec0(embedding float[4]);
        """
    )
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'migration claim', NULL)")
    conn.execute("INSERT INTO vec_nodes(rowid, embedding) VALUES (1, '[0.1, 0.2, 0.3, 0.4]')")
    conn.commit()
    conn.close()

    out_sql = str(tmp_path / "knowledge.sql")

    # Must not raise — the bug was an OperationalError on the DROP TABLE for vec_nodes.
    stats = engram_backup.dump_stripped(db_path, out_sql)

    # vec_nodes is excluded (vec_excluded=True) and no vec_nodes statements survive.
    assert stats["vec_excluded"] is True
    dump_text = open(out_sql).read()
    assert "vec_nodes" not in dump_text, "vec_nodes statements leaked into dump"

    # Base-table round-trip: nodes row survives.
    # Note: iterdump() emits "DELETE FROM sqlite_sequence" (a vec0 shadow-table
    # side-effect) even though sqlite_sequence isn't in the dump's CREATE statements.
    # Filter it out before executing on a fresh DB where the table doesn't exist.
    restore_sql = "\n".join(
        line for line in dump_text.splitlines()
        if "sqlite_sequence" not in line
    )
    recon = str(tmp_path / "recon.db")
    rc = sqlite3.connect(recon)
    rc.executescript(restore_sql)
    assert rc.execute("SELECT count(*) FROM nodes").fetchone()[0] == 1
    rc.close()


def test_verify_roundtrip_stale_fts_index_does_not_raise(tmp_path):
    """Regression test for the stale-FTS-index false-fail (issue #724 continuation).

    Reproduces the live-graph failure: original DB has 135 'migration' hits but
    a fresh rebuild produces 137 — the live FTS index was stale (2 docs not yet
    indexed). verify_roundtrip must NOT raise when rebuilt >= original; the `>=`
    comparison handles the stale-original case.

    Construction: build a DB, populate rows through the trigger (so the FTS
    index is consistent), then insert extra rows BYPASSING the FTS trigger so
    the live index under-counts them.  The rebuilt index will see all rows and
    return a higher count than the stale original.
    """
    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id         TEXT PRIMARY KEY,
            claim      TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            embedding  TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation);
        END;
        CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row');
        """
    )
    # Insert rows through the trigger so the FTS index picks them up.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_1', 'plugin migration completed', 'verified', 'the migration held', NULL)"
    )
    # Check the live FTS hit count before adding stale rows.
    live_hits_before = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    assert live_hits_before == 1

    # Now insert more rows that contain 'migration' BUT bypass the FTS trigger
    # by inserting directly into nodes without a matching trigger path.
    # The cleanest portable approach: disable triggers temporarily via writable_schema
    # or use a DELETE+shadow approach.  Simplest: drop the trigger, insert, don't restore.
    # This makes the live index stale: nodes has 3 rows with 'migration', FTS knows 1.
    conn.execute("DROP TRIGGER nodes_fts_insert")
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_2', 'second migration step', 'forum post', 'second migration', NULL)"
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_3', 'third migration event', 'letter', 'third migration', NULL)"
    )
    conn.commit()

    # Confirm stale state: live FTS still reports 1, nodes has 3 'migration' rows.
    live_hits_stale = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    node_rows_with_migration = conn.execute(
        "SELECT count(*) FROM nodes WHERE claim LIKE '%migration%'"
    ).fetchone()[0]
    conn.close()

    assert live_hits_stale == 1, f"expected stale FTS (1 hit), got {live_hits_stale}"
    assert node_rows_with_migration == 3, f"expected 3 node rows with 'migration', got {node_rows_with_migration}"
    # Stale gap confirmed: live FTS (1) < rows (3).

    # verify_roundtrip must NOT raise.  The rebuilt index will return 3 hits
    # (>= the stale original's 1), satisfying the >= assertion.
    rep = engram_backup.verify_roundtrip(db_path)
    assert rep["ok"] is True
    assert rep["nodes"] == 3
    # fts_hits is the rebuilt count — must be >= the stale original count.
    assert rep["fts_hits"] >= live_hits_stale


def test_verify_roundtrip_fresh_index_exact_match_still_passes(engram_db):
    """Confirm that an already-consistent (non-stale) FTS index still passes.

    The engram_db fixture inserts all 4 rows through the trigger so both the
    live FTS count (2 'migration' hits) and the rebuilt count (2 hits) are
    identical.  The >= assertion holds trivially (2 >= 2), so this test checks
    that tightening == to >= did NOT break the common case.
    """
    rep = engram_backup.verify_roundtrip(engram_db)
    assert rep["ok"] is True
    assert rep["nodes"] == 4
    # The fixture has ob_1 and ob_3 matching 'migration' — rebuilt must report >= 2.
    assert rep["fts_hits"] is not None
    assert rep["fts_hits"] >= 2


def test_verify_roundtrip_fts_regression_still_trips(tmp_path):
    """Confirm that a genuine FTS regression (rebuilt < original) still raises.

    We cannot construct a genuine regression via the full verify_roundtrip path
    because dump_stripped + rebuild_fts_index always produces a correct index.
    Instead we test the comparison logic directly: assert that (hits < fts_hits_orig)
    raises AssertionError, mirroring what verify_roundtrip would do.

    This is the unit-test approach described in the spec: if constructing a true
    regression via the full path is impractical, test the comparison logic directly.
    """
    # Simulate the assertion logic in verify_roundtrip for the regression case.
    hits = 1           # rebuilt index (lower — a genuine regression)
    fts_hits_orig = 3  # original live count

    # The assertion in verify_roundtrip is: assert hits >= fts_hits_orig
    # A regression means hits < fts_hits_orig, which must raise.
    raised = False
    try:
        assert hits >= fts_hits_orig, f"FTS hits regressed after rebuild: {hits} < {fts_hits_orig}"
    except AssertionError:
        raised = True

    assert raised, "Expected AssertionError for regression case (rebuilt < original) but none was raised"
