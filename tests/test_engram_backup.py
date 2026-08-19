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


def test_verify_roundtrip_zero_fts_hits(tmp_path):
    """Zero-hit FTS path: verify_roundtrip returns ok=True and fts_hits==0 when no
    node in the DB contains the probe term ('migration').

    This confirms the guard introduced for the #723 nit is load-bearing: a graph
    that genuinely has no 'migration' content passes cleanly, and the returned
    fts_hits value is 0 (not None, not missing).
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
    # Nodes exist but none contain 'migration'.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_1', 'epistemic humility cornerstone', 'axiom', 'introspective', '[0.1]')"
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_2', 'provenance chain for observations', 'evidence', 'sourced', '[0.2]')"
    )
    conn.commit()
    conn.close()

    rep = engram_backup.verify_roundtrip(db_path)
    assert rep["ok"] is True
    assert rep["nodes"] == 2
    assert rep["fts_hits"] == 0


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

    Reproduces the live-graph failure: original DB has a stale FTS index that
    under-counts docs.  verify_roundtrip must NOT raise on this fixture.

    #727: the old `>=` hit-count assertion handled this case (rebuilt >= stale
    original).  The new structural-parity assertion is unconditional and also
    handles it — the recon parity check compares nodes_fts row count against
    the non-retracted nodes count, independent of the stale source FTS state.
    The probe-hit assertions in this test still hold numerically (rebuilt=3 >=
    stale=1) and are preserved as diagnostic checks.

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

    # verify_roundtrip must NOT raise.  The rebuilt index will return 3 hits.
    # #727: pass is now determined by structural parity, not by hit count comparison.
    rep = engram_backup.verify_roundtrip(db_path)
    assert rep["ok"] is True
    assert rep["nodes"] == 3
    # Informational probe checks: rebuilt clean count >= stale source count.
    assert rep["fts_hits"] >= live_hits_stale


def test_verify_roundtrip_fresh_index_exact_match_still_passes(engram_db):
    """Confirm that an already-consistent (non-stale, non-polluted) FTS index still passes.

    The engram_db fixture inserts all 4 rows through the trigger so both the
    live FTS count (2 'migration' hits) and the rebuilt count (2 hits) are
    identical.  #727: pass is now determined by structural parity (nodes_fts row
    count == non-retracted node count); probe-hit count is informational.
    """
    rep = engram_backup.verify_roundtrip(engram_db)
    assert rep["ok"] is True
    assert rep["nodes"] == 4
    # The fixture has ob_1 and ob_3 matching 'migration' — recon probe returns 2.
    assert rep["fts_hits"] is not None
    assert rep["fts_hits"] == 2


def test_verify_roundtrip_null_status_node_passes(tmp_path):
    """#811 BLOCKER regression: verify_roundtrip must pass when a node has explicit NULL status.

    SQL three-valued logic: `status != 'retracted'` evaluates to NULL (not TRUE)
    for a NULL-status row, so the row is NOT counted — but rebuild_fts_index only
    removes rows with status='retracted', so the NULL-status row IS in the rebuilt
    index.  Result: parity mismatch (expected < actual), raising AssertionError on
    a healthy roundtrip.

    Fix: the parity predicate uses `status IS NOT 'retracted'` (SQLite null-safe),
    which correctly counts NULL-status rows as non-retracted.

    Fixture: one active node + one node with explicit NULL status (not relying on
    DEFAULT — INSERT supplies NULL directly).  verify_roundtrip must PASS and
    report rep["nodes"] == 2.
    """
    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id          TEXT PRIMARY KEY,
            claim       TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            status      TEXT,
            embedding   TEXT
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
    # Active node — status explicitly 'active'.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_active', 'migration completed', 'forum post', 'active node', 'active', NULL)"
    )
    # NULL-status node — explicit NULL, not relying on DEFAULT.
    conn.execute(
        "INSERT INTO nodes(id, claim, quoted_text, interpretation, status, embedding) "
        "VALUES ('ob_null', 'second claim present', 'src doc', 'null status node', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    # Must NOT raise — both nodes are non-retracted and must be counted by parity.
    rep = engram_backup.verify_roundtrip(db_path)
    assert rep["ok"] is True
    assert rep["nodes"] == 2


def test_verify_roundtrip_passes_with_polluted_source_and_retraction(tmp_path):
    """#727 regression: verify_roundtrip must pass when the SOURCE index is polluted
    by a bare rebuild (includes retracted rows) while the RECON index is clean.

    This is the exact failure mode #727 documents: live=137 FTS
    hits (with 2 retracted matches) vs. clean=135 hits post-rebuild.  The old
    `hits >= fts_hits_orig` assertion would FALSE-FAIL here (135 >= 137 is false)
    even though the recon index is CORRECT and the source index is POLLUTED.

    Construction:
    - One node containing the probe term 'migration' (non-retracted).
    - One node containing 'migration' that is RETRACTED — correctly absent from
      a healthy index, but present in a bare-rebuild-polluted index.
    - Pollute the source FTS by running a bare INSERT INTO nodes_fts VALUES('rebuild')
      after the retraction trigger has excluded the retracted row — the bare rebuild
      re-adds it, so fts_hits_orig = 2.
    - verify_roundtrip must pass: the recon structural parity holds (1 non-retracted
      node → 1 FTS row), and fts_hits_clean = 1 < fts_hits_orig = 2.

    The old `>=` assertion would have raised here (1 >= 2 is false).
    """
    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id          TEXT PRIMARY KEY,
            claim       TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            status      TEXT,
            embedding   TEXT
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
        CREATE VIRTUAL TABLE nodes_fts_vocab USING fts5vocab('nodes_fts', 'row');
        """
    )
    # Non-retracted node — probe term present.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_live', 'plugin migration succeeded', 'forum post', 'migration complete', 'active', NULL)"
    )
    # Retracted node — probe term present.  The retract trigger will exclude it
    # from the live FTS index via 'delete'-insert.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_retracted', 'migration plan deprecated', 'old doc', 'retracted migration ref', 'retracted', NULL)"
    )
    # Simulate the retract trigger exclusion: 'delete'-insert the retracted row
    # so the healthy live index correctly has 1 hit (ob_live only).
    retracted_row = conn.execute(
        "SELECT rowid, claim, quoted_text, interpretation FROM nodes WHERE id='ob_retracted'"
    ).fetchone()
    conn.execute(
        "INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation) "
        "VALUES ('delete', ?, ?, ?, ?)",
        retracted_row,
    )
    conn.commit()

    # Confirm healthy live index has 1 hit.
    clean_hits = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    assert clean_hits == 1, f"expected 1 clean hit, got {clean_hits}"

    # NOW pollute: bare rebuild re-adds the retracted row to the index.
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
    conn.commit()

    polluted_hits = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    assert polluted_hits == 2, f"expected 2 polluted hits, got {polluted_hits}"
    conn.close()

    # verify_roundtrip must pass.
    # The recon structural parity: 1 non-retracted node → 1 FTS row (correct).
    # The old `>=` would have raised: recon_hits(1) >= fts_hits_orig(2) → False.
    rep = engram_backup.verify_roundtrip(db_path)
    assert rep["ok"] is True
    assert rep["nodes"] == 2  # total nodes (retracted + active)
    # fts_hits is the clean recon probe count (1 — retracted row excluded).
    assert rep["fts_hits"] == 1
    # fts_hits_orig is the polluted source probe count (2 — retracted row included).
    assert rep["fts_hits_orig"] == 2


def test_verify_roundtrip_retracted_term_absent_from_recon_fts(tmp_path):
    """#727: after verify_roundtrip, the recon FTS must NOT contain terms that
    appear ONLY in retracted nodes.

    A MATCH on a term that is exclusive to a retracted node must return 0 hits
    on the recon FTS — confirming rebuild_fts_index correctly excluded the
    retracted row from the index (not just from the row count).

    This tests the post-roundtrip recon DB directly by running verify_roundtrip
    and then probing the cleaned-up recon index before it is deleted.  Since
    verify_roundtrip deletes the recon temp file on exit, we run a manual
    dump→restore→rebuild sequence to inspect the recon index ourselves.
    """
    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id          TEXT PRIMARY KEY,
            claim       TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            status      TEXT,
            embedding   TEXT
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
    # Non-retracted node — uses 'xyzzy_active' as a distinctive term.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_active', 'xyzzy_active term present', 'src', 'active node', 'active', NULL)"
    )
    # Retracted node — uses 'xyzzy_retracted' as a distinctive term.
    # Simulate retract trigger exclusion via 'delete'-insert.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_ret', 'xyzzy_retracted term exclusive', 'src', 'retracted ref', 'retracted', NULL)"
    )
    ret_row = conn.execute(
        "SELECT rowid, claim, quoted_text, interpretation FROM nodes WHERE id='ob_ret'"
    ).fetchone()
    conn.execute(
        "INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation) "
        "VALUES ('delete', ?, ?, ?, ?)",
        ret_row,
    )
    conn.commit()
    conn.close()

    # Perform manual dump → restore → rebuild_fts_index and inspect.
    dump_sql = str(tmp_path / "dump.sql")
    recon_db = str(tmp_path / "recon.db")
    engram_backup.dump_stripped(db_path, dump_sql)
    rc = sqlite3.connect(recon_db)
    rc.executescript(open(dump_sql).read())
    rc.close()
    engram_backup.rebuild_fts_index(recon_db)

    rc = sqlite3.connect(recon_db)
    # Term from the retracted node must NOT appear in the recon index.
    retracted_hits = rc.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'xyzzy_retracted'"
    ).fetchone()[0]
    # Term from the active node MUST appear.
    active_hits = rc.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'xyzzy_active'"
    ).fetchone()[0]
    rc.close()

    assert retracted_hits == 0, (
        f"retracted-node term 'xyzzy_retracted' should be absent from recon FTS, got {retracted_hits} hits"
    )
    assert active_hits == 1, (
        f"active-node term 'xyzzy_active' should be present in recon FTS, got {active_hits} hits"
    )


# ---------------------------------------------------------------------------
# #2077 — nodes_fts column list single-sourcing
# ---------------------------------------------------------------------------
#
# Background: the column list was hand-written in two places that drifted —
# engram_core.py's CREATE VIRTUAL TABLE carried 5 columns (claim,
# quoted_text, interpretation, reported_state, trigger_text), while
# rebuild_fts_index here still had the original 3 (claim, quoted_text,
# interpretation). Consequence 1: the delete-insert only scrubbed 3 columns,
# so a retracted node stayed FTS-findable through reported_state/
# trigger_text. Consequence 2: on restore, rebuild_fts_index recreated
# nodes_fts with 3 columns and engram_core's `CREATE TABLE IF NOT EXISTS`
# then permanently no-op'd, leaving the graph under-indexed forever.
#
# All three tests below use only temp DBs (tmp_path) — no ~/.engram is ever
# touched.


def _build_five_column_engram_db(path: str, rows: list) -> None:
    """Build a nodes table + a full 5-column nodes_fts index (the real
    production shape post-#403), matching engram_core's live schema closely
    enough to exercise rebuild_fts_index's column handling.

    ``rows`` is a list of dicts with keys: id, claim, quoted_text,
    interpretation, reported_state, trigger_text, status.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id             TEXT PRIMARY KEY,
            claim          TEXT,
            quoted_text    TEXT,
            interpretation TEXT,
            reported_state TEXT,
            trigger_text   TEXT,
            status         TEXT,
            embedding      TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation, reported_state, trigger_text,
            content='nodes', content_rowid='rowid'
        );
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, claim, quoted_text, interpretation, "
            "reported_state, trigger_text, status, embedding) VALUES (?,?,?,?,?,?,?,NULL)",
            (
                r["id"], r["claim"], r["quoted_text"], r["interpretation"],
                r["reported_state"], r["trigger_text"], r["status"],
            ),
        )
    # Populate the FTS index the way a live install would have it before a
    # rebuild — i.e. everything indexed, including the retracted row, mirroring
    # the pre-#274-migration / pre-repair state.
    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()


def _fts_match_ids(db_path: str, token: str) -> set:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT n.id AS id FROM nodes_fts f JOIN nodes n ON n.rowid = f.rowid "
        "WHERE nodes_fts MATCH ?",
        (token,),
    ).fetchall()
    conn.close()
    return {r["id"] for r in rows}


def test_2077_retracted_node_scrubbed_from_all_five_fts_columns(tmp_path):
    """Regression for #2077 consequence 1 (the issue's exact measured case).

    Before the fix, rebuild_fts_index's delete-insert only touched 3 of the
    5 FTS columns, so a retracted node stayed FTS-findable through
    reported_state and trigger_text (the two columns feeling_report/lesson
    nodes rely on). After rebuild_fts_index, the retracted node's unique
    tokens must be absent from a MATCH probe against ALL five columns, and
    the current node's tokens must remain present.
    """
    db_path = str(tmp_path / "knowledge.db")
    rows = [
        {
            "id": "ob_current",
            "claim": "current2077_claimtoken",
            "quoted_text": "current2077_quotedtoken",
            "interpretation": "current2077_interptoken",
            "reported_state": "current2077_reportedtoken",
            "trigger_text": "current2077_triggertoken",
            "status": "active",
        },
        {
            "id": "ob_retracted",
            "claim": "retracted2077_claimtoken",
            "quoted_text": "retracted2077_quotedtoken",
            "interpretation": "retracted2077_interptoken",
            "reported_state": "retracted2077_reportedtoken",
            "trigger_text": "retracted2077_triggertoken",
            "status": "retracted",
        },
    ]
    _build_five_column_engram_db(db_path, rows)

    # Confirm the pre-rebuild state: the retracted row IS indexed (both nodes
    # were populated by the bare 'rebuild' in the fixture builder).
    assert "ob_retracted" in _fts_match_ids(db_path, "retracted2077_reportedtoken")

    assert engram_backup.rebuild_fts_index(db_path) is True

    # The issue's exact MATCH probes: none of the retracted node's five
    # column tokens may match after rebuild_fts_index.
    for token in (
        "retracted2077_claimtoken",
        "retracted2077_quotedtoken",
        "retracted2077_interptoken",
        "retracted2077_reportedtoken",
        "retracted2077_triggertoken",
    ):
        matched = _fts_match_ids(db_path, token)
        assert "ob_retracted" not in matched, (
            f"retracted node must not match on {token!r} after rebuild_fts_index, got {matched}"
        )

    # The current node's tokens across all five columns must still match.
    for token in (
        "current2077_claimtoken",
        "current2077_quotedtoken",
        "current2077_interptoken",
        "current2077_reportedtoken",
        "current2077_triggertoken",
    ):
        matched = _fts_match_ids(db_path, token)
        assert "ob_current" in matched, (
            f"current node must match on {token!r} after rebuild_fts_index, got {matched}"
        )


def test_2077_repairs_stale_three_column_nodes_fts(tmp_path):
    """Regression for #2077 consequence 2: a stale 3-column nodes_fts table
    (left behind by the pre-fix rebuild_fts_index on a restore) must be
    detected, dropped, and rebuilt to the full 5-column canonical schema —
    not silently skipped by `CREATE TABLE IF NOT EXISTS`.
    """
    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id             TEXT PRIMARY KEY,
            claim          TEXT,
            quoted_text    TEXT,
            interpretation TEXT,
            reported_state TEXT,
            trigger_text   TEXT,
            status         TEXT,
            embedding      TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        -- Stale pre-fix shape: only 3 columns, even though `nodes` has all 5.
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes (id, claim, quoted_text, interpretation, reported_state, "
        "trigger_text, status, embedding) VALUES "
        "('fl_repair', 'a lesson claim', 'src', 'interp', "
        "'repairtoken2077_reportedstate', 'repairtoken2077_triggertext', 'active', NULL)"
    )
    conn.commit()
    conn.close()

    # Confirm the pre-repair gap: reported_state content is not indexable —
    # the stale 3-column table has no such column to match against.
    stale_cols = [row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(nodes_fts)").fetchall()]
    assert stale_cols == ["claim", "quoted_text", "interpretation"]

    assert engram_backup.rebuild_fts_index(db_path) is True

    conn = sqlite3.connect(db_path)
    repaired_cols = [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)").fetchall()]
    assert repaired_cols == list(engram_backup.NODES_FTS_COLUMNS), (
        f"nodes_fts must be repaired to the canonical 5-column schema, got {repaired_cols}"
    )

    # Full reindex: the reported_state / trigger_text tokens must now match,
    # which is only possible once the table has been dropped and recreated.
    hits_rs = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'repairtoken2077_reportedstate'"
    ).fetchone()[0]
    hits_tt = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'repairtoken2077_triggertext'"
    ).fetchone()[0]
    conn.close()
    assert hits_rs == 1, "reported_state token must be indexed after repair"
    assert hits_tt == 1, "trigger_text token must be indexed after repair"


def test_2077_drift_guard_create_and_insert_derive_from_canonical_constant(tmp_path):
    """Drift guard: both consumers of the column list — engram_core's
    CREATE VIRTUAL TABLE and engram_backup's rebuild_fts_index — must
    actually produce a nodes_fts table matching engram_backup.NODES_FTS_COLUMNS,
    not a second hand-written copy that happens to agree today.

    This is a functional (behavioral) guard rather than a source-text grep:
    it exercises each consumer and inspects the resulting table shape, so a
    future hand-written literal that silently diverges from the constant
    fails this test even if it currently matches by coincidence.
    """
    # --- Consumer 1: engram_backup.rebuild_fts_index ---
    backup_db = str(tmp_path / "backup_knowledge.db")
    conn = sqlite3.connect(backup_db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id             TEXT PRIMARY KEY,
            claim          TEXT,
            quoted_text    TEXT,
            interpretation TEXT,
            reported_state TEXT,
            trigger_text   TEXT,
            status         TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_x', 'claim', 'qt', 'interp', 'rs', 'tt', 'active')"
    )
    conn.commit()
    conn.close()
    assert engram_backup.rebuild_fts_index(backup_db) is True
    conn = sqlite3.connect(backup_db)
    backup_cols = [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)").fetchall()]
    conn.close()
    assert backup_cols == list(engram_backup.NODES_FTS_COLUMNS), (
        f"rebuild_fts_index must derive its column list from NODES_FTS_COLUMNS, got {backup_cols}"
    )

    # --- Consumer 2: engram_core's CREATE VIRTUAL TABLE nodes_fts ---
    import importlib
    import shutil
    import tempfile

    tests_dir = os.path.dirname(os.path.abspath(__file__))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from conftest import fresh_module  # local import — keeps this module's own
    # top-level imports free of engram_core/engram_tasks, per this file's
    # existing pure-engram_backup import contract.

    engram_core = importlib.import_module("engram_core")
    core = fresh_module(engram_core)

    core_test_dir = tempfile.mkdtemp(prefix="engram_test_2077_drift_guard_")
    try:
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        core._configure_paths(core_test_dir)
        core._ensure_data_dir()
        conn = core._get_db()
        core_cols = [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)").fetchall()]
        conn.close()
        assert core_cols == list(engram_backup.NODES_FTS_COLUMNS), (
            f"engram_core's CREATE VIRTUAL TABLE nodes_fts must derive its column "
            f"list from engram_backup.NODES_FTS_COLUMNS, got {core_cols}"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2077_startup_self_heal_repairs_stale_nodes_fts_on_get_db(tmp_path):
    """Regression for #2077 finding 1 (reviewer round 2): a LIVE server whose
    nodes_fts is already a stale 3-column table must self-heal on the next
    ``_get_db()`` call (i.e. the next process/server restart), not just on an
    explicit backup-restore via engram_backup.rebuild_fts_index.

    Before this fix, engram_core's ``CREATE VIRTUAL TABLE IF NOT EXISTS``
    silently no-ops once nodes_fts exists in ANY shape, so a corrupted
    3-column table survived every restart forever. This mirrors the
    reviewer's exact probe: build a real DB via engram_core._get_db(),
    manually downgrade nodes_fts to 3 columns behind engram_core's back, then
    re-run _get_db() and assert the table is back to 5 columns with the
    reported_state/trigger_text content actually reindexed (not just present
    in `nodes`).
    """
    import importlib
    import shutil
    import tempfile

    tests_dir = os.path.dirname(os.path.abspath(__file__))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from conftest import fresh_module

    engram_core = importlib.import_module("engram_core")
    core = fresh_module(engram_core)

    core_test_dir = tempfile.mkdtemp(prefix="engram_test_2077_startup_self_heal_")
    try:
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        core._configure_paths(core_test_dir)
        core._ensure_data_dir()

        # First _get_db(): builds the DB fresh, with the correct 5-column
        # nodes_fts (this is the happy path #2077's first fix already covers).
        conn = core._get_db()
        conn.execute(
            "INSERT INTO nodes (id, type, created_at, claim, quoted_text, "
            "interpretation, reported_state, trigger_text, status) VALUES "
            "('fl_selfheal', 'feeling_report', '2026-08-18T00:00:00Z', "
            "'a feeling claim', 'src quote', 'interp', "
            "'selfhealtoken2077_reportedstate', 'selfhealtoken2077_triggertext', "
            "'active')"
        )
        conn.commit()
        conn.close()

        # Simulate a corrupted live server: downgrade nodes_fts to the stale
        # pre-#2077 3-column shape, behind engram_core's back (e.g. as if a
        # pre-fix engram_backup.rebuild_fts_index had run against this file).
        raw = sqlite3.connect(str(core.DB_PATH))
        raw.executescript(
            """
            DROP TABLE IF EXISTS nodes_fts_vocab;
            DROP TABLE IF EXISTS nodes_fts;
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                claim, quoted_text, interpretation,
                content='nodes', content_rowid='rowid'
            );
            INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild');
            """
        )
        raw.commit()
        raw.close()

        stale_cols = [
            row[1]
            for row in sqlite3.connect(str(core.DB_PATH)).execute(
                "PRAGMA table_info(nodes_fts)"
            ).fetchall()
        ]
        assert stale_cols == ["claim", "quoted_text", "interpretation"], (
            "test setup must actually produce the stale 3-column shape"
        )

        # Force the one-time-setup block to re-run, as it would on the next
        # real server restart (a fresh process starts with an empty
        # _db_setup_done_paths — this test reuses one process, so clear the
        # in-process cache to simulate that restart instead of spinning up a
        # second interpreter).
        core._db_setup_done_paths.clear()

        # Second _get_db(): must detect the drift and self-heal, with no
        # caller-visible action beyond calling _get_db() again.
        conn = core._get_db()
        repaired_cols = [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)").fetchall()]
        assert repaired_cols == list(engram_backup.NODES_FTS_COLUMNS), (
            f"nodes_fts must self-heal to the canonical 5-column schema on "
            f"the next _get_db() call, got {repaired_cols}"
        )

        hits_rs = conn.execute(
            "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'selfhealtoken2077_reportedstate'"
        ).fetchone()[0]
        hits_tt = conn.execute(
            "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'selfhealtoken2077_triggertext'"
        ).fetchone()[0]
        conn.close()
        assert hits_rs == 1, "reported_state token must be reindexed after startup self-heal"
        assert hits_tt == 1, "trigger_text token must be reindexed after startup self-heal"
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2077_core_fallback_literal_matches_canonical_constant():
    """Regression for #2077 finding 2 (reviewer round 2): engram_core's
    ImportError fallback literal for the FTS column list
    (``"claim, quoted_text, interpretation, reported_state, trigger_text"``)
    is a third hand-written copy of NODES_FTS_COLUMNS, never exercised by any
    test (the ``import engram_backup`` above always succeeds in this
    environment). Assert it stays byte-identical to the canonical constant so
    a future edit to NODES_FTS_COLUMNS that forgets this literal fails loudly
    here instead of silently drifting a third time.

    Reads the literal out of engram_core.py's source via `ast` rather than
    triggering the fallback at runtime (forcing an ImportError on
    `engram_backup` mid-suite would poison sys.modules for every other test
    that imports it afterwards).
    """
    import ast

    core_src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram", "engram_core.py"
    )
    with open(core_src_path) as f:
        tree = ast.parse(f.read(), filename=core_src_path)

    fallback_literal = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_fts_cols_sql"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            fallback_literal = node.value.value

    assert fallback_literal is not None, (
        "could not find the `_fts_cols_sql = \"...\"` fallback literal in "
        "engram_core.py — has the ImportError fallback been refactored away?"
    )
    canonical_sql = ", ".join(engram_backup.NODES_FTS_COLUMNS)
    assert fallback_literal == canonical_sql, (
        f"engram_core's ImportError fallback literal {fallback_literal!r} has "
        f"drifted from engram_backup.NODES_FTS_COLUMNS {canonical_sql!r} — "
        "update the literal to match."
    )


def test_2087_rebuild_fts_preserves_incoming_user_version(tmp_path):
    """#2087: rebuild_fts_index must PRESERVE an already-migrated DB's
    user_version rather than unconditionally pinning it to 1.

    Before the fix, a live DB at user_version 3 (post migration-3, #195)
    would be silently downgraded to 1 by any call to rebuild_fts_index —
    e.g. #2080's startup self-heal path — which makes
    _run_db_one_time_setup's migration-2/3 blocks re-run on the very same
    setup call, since the self-heal fires before those blocks read
    PRAGMA user_version.
    """
    p = str(tmp_path / "migrated.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            status TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'plugin migration', 'src', 'interp', 'active')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    assert engram_backup.rebuild_fts_index(p) is True

    conn = sqlite3.connect(p)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 3, f"rebuild_fts_index must preserve incoming user_version 3, got {version}"


def test_2087_rebuild_fts_bumps_fresh_db_to_at_least_1(tmp_path):
    """#2087: a genuinely fresh/pre-migration DB (user_version 0 on entry)
    must still come out of rebuild_fts_index at user_version >= 1 — the
    original intent of the PRAGMA write (satisfying the server's one-shot
    #274 migration gate) must survive for the case it was actually meant to
    cover.
    """
    p = str(tmp_path / "fresh.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            status TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'plugin migration', 'src', 'interp', 'active')")
    conn.commit()
    conn.close()

    # Confirm the fixture actually starts at user_version 0.
    conn = sqlite3.connect(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()

    assert engram_backup.rebuild_fts_index(p) is True

    conn = sqlite3.connect(p)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version >= 1, f"rebuild_fts_index must bump a fresh DB (user_version 0) to >= 1, got {version}"
