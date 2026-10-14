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


def _build_six_column_engram_db(path: str, rows: list) -> None:
    """Build a nodes table + a full 6-column nodes_fts index (the real
    production shape post-#1952 PR2), matching engram_core's live schema
    closely enough to exercise rebuild_fts_index's column handling.

    ``rows`` is a list of dicts with keys: id, claim, quoted_text,
    interpretation, trigger_text, recall_summary, recall_keywords, status.
    reported_state stays a real ``nodes`` column (PR2 keeps it there for
    rollback safety) but is no longer FTS-indexed, so it's not part of the
    row-dict contract here — the INSERT below just writes NULL for it.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id              TEXT PRIMARY KEY,
            claim           TEXT,
            quoted_text     TEXT,
            interpretation  TEXT,
            reported_state  TEXT,
            trigger_text    TEXT,
            recall_summary  TEXT,
            recall_keywords TEXT,
            status          TEXT,
            embedding       TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation, trigger_text, recall_summary, recall_keywords,
            content='nodes', content_rowid='rowid'
        );
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, claim, quoted_text, interpretation, "
            "reported_state, trigger_text, recall_summary, recall_keywords, status, embedding) "
            "VALUES (?,?,?,?,NULL,?,?,?,?,NULL)",
            (
                r["id"], r["claim"], r["quoted_text"], r["interpretation"],
                r["trigger_text"], r["recall_summary"], r["recall_keywords"], r["status"],
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


def test_2077_retracted_node_scrubbed_from_all_current_fts_columns(tmp_path):
    """Regression for #2077 consequence 1 (the issue's exact measured case).

    Before the fix, rebuild_fts_index's delete-insert only touched 3 of the
    5 FTS columns, so a retracted node stayed FTS-findable through
    reported_state and trigger_text (the two columns feeling_report/lesson
    nodes rely on). After rebuild_fts_index, the retracted node's unique
    tokens must be absent from a MATCH probe against ALL current FTS
    columns, and the current node's tokens must remain present.
    """
    db_path = str(tmp_path / "knowledge.db")
    rows = [
        {
            "id": "ob_current",
            "claim": "current2077_claimtoken",
            "quoted_text": "current2077_quotedtoken",
            "interpretation": "current2077_interptoken",
            "trigger_text": "current2077_triggertoken",
            "recall_summary": "current2077_summarytoken",
            "recall_keywords": "current2077_keywordtoken",
            "status": "active",
        },
        {
            "id": "ob_retracted",
            "claim": "retracted2077_claimtoken",
            "quoted_text": "retracted2077_quotedtoken",
            "interpretation": "retracted2077_interptoken",
            "trigger_text": "retracted2077_triggertoken",
            "recall_summary": "retracted2077_summarytoken",
            "recall_keywords": "retracted2077_keywordtoken",
            "status": "retracted",
        },
    ]
    _build_six_column_engram_db(db_path, rows)

    # Confirm the pre-rebuild state: the retracted row IS indexed (both nodes
    # were populated by the bare 'rebuild' in the fixture builder).
    assert "ob_retracted" in _fts_match_ids(db_path, "retracted2077_summarytoken")

    assert engram_backup.rebuild_fts_index(db_path) is True

    # The issue's exact MATCH probes: none of the retracted node's current
    # column tokens may match after rebuild_fts_index.
    for token in (
        "retracted2077_claimtoken",
        "retracted2077_quotedtoken",
        "retracted2077_interptoken",
        "retracted2077_triggertoken",
        "retracted2077_summarytoken",
        "retracted2077_keywordtoken",
    ):
        matched = _fts_match_ids(db_path, token)
        assert "ob_retracted" not in matched, (
            f"retracted node must not match on {token!r} after rebuild_fts_index, got {matched}"
        )

    # The current node's tokens across all current FTS columns must still match.
    for token in (
        "current2077_claimtoken",
        "current2077_quotedtoken",
        "current2077_interptoken",
        "current2077_triggertoken",
        "current2077_summarytoken",
        "current2077_keywordtoken",
    ):
        matched = _fts_match_ids(db_path, token)
        assert "ob_current" in matched, (
            f"current node must match on {token!r} after rebuild_fts_index, got {matched}"
        )


def test_2077_repairs_stale_three_column_nodes_fts(tmp_path):
    """Regression for #2077 consequence 2: a stale 3-column nodes_fts table
    (left behind by the pre-fix rebuild_fts_index on a restore) must be
    detected, dropped, and rebuilt to the full canonical schema — not
    silently skipped by `CREATE TABLE IF NOT EXISTS`.
    """
    db_path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id              TEXT PRIMARY KEY,
            claim           TEXT,
            quoted_text     TEXT,
            interpretation  TEXT,
            reported_state  TEXT,
            trigger_text    TEXT,
            recall_summary  TEXT,
            recall_keywords TEXT,
            status          TEXT,
            embedding       TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        -- Stale pre-fix shape: only 3 columns, even though `nodes` has all 6.
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes (id, claim, quoted_text, interpretation, reported_state, "
        "trigger_text, recall_summary, recall_keywords, status, embedding) VALUES "
        "('fl_repair', 'a lesson claim', 'src', 'interp', "
        "'repairtoken2077_reportedstate', 'repairtoken2077_triggertext', "
        "'repairtoken2077_summarytext', 'repairtoken2077_keywordtext', 'active', NULL)"
    )
    conn.commit()
    conn.close()

    # Confirm the pre-repair gap: trigger_text/recall_summary content is not
    # indexable — the stale 3-column table has neither column to match against.
    stale_cols = [row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(nodes_fts)").fetchall()]
    assert stale_cols == ["claim", "quoted_text", "interpretation"]

    assert engram_backup.rebuild_fts_index(db_path) is True

    conn = sqlite3.connect(db_path)
    repaired_cols = [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)").fetchall()]
    assert repaired_cols == list(engram_backup.NODES_FTS_COLUMNS), (
        f"nodes_fts must be repaired to the canonical schema, got {repaired_cols}"
    )

    # Full reindex: the trigger_text / recall_summary tokens must now match,
    # which is only possible once the table has been dropped and recreated.
    hits_tt = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'repairtoken2077_triggertext'"
    ).fetchone()[0]
    hits_summary = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'repairtoken2077_summarytext'"
    ).fetchone()[0]
    conn.close()
    assert hits_tt == 1, "trigger_text token must be indexed after repair"
    assert hits_summary == 1, "recall_summary token must be indexed after repair"


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
            id              TEXT PRIMARY KEY,
            claim           TEXT,
            quoted_text     TEXT,
            interpretation  TEXT,
            reported_state  TEXT,
            trigger_text    TEXT,
            recall_summary  TEXT,
            recall_keywords TEXT,
            status          TEXT
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_x', 'claim', 'qt', 'interp', 'rs', 'tt', 'rsum', 'rkw', 'active')"
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
    re-run _get_db() and assert the table is back to the canonical column set
    with the recall_summary/trigger_text content actually reindexed (not
    just present in `nodes`).
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

        # First _get_db(): builds the DB fresh, with the correct canonical
        # nodes_fts (this is the happy path #2077's first fix already covers).
        conn = core._get_db()
        conn.execute(
            "INSERT INTO nodes (id, type, created_at, claim, quoted_text, "
            "interpretation, reported_state, trigger_text, recall_summary, "
            "recall_keywords, status) VALUES "
            "('fl_selfheal', 'feeling_report', '2026-08-18T00:00:00Z', "
            "'a feeling claim', 'src quote', 'interp', "
            "'selfhealtoken2077_reportedstate', 'selfhealtoken2077_triggertext', "
            "'selfhealtoken2077_summarytext', 'selfhealtoken2077_keywordtext', "
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
            f"nodes_fts must self-heal to the canonical schema on "
            f"the next _get_db() call, got {repaired_cols}"
        )

        hits_summary = conn.execute(
            "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'selfhealtoken2077_summarytext'"
        ).fetchone()[0]
        hits_tt = conn.execute(
            "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'selfhealtoken2077_triggertext'"
        ).fetchone()[0]
        conn.close()
        assert hits_summary == 1, "recall_summary token must be reindexed after startup self-heal"
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


# ─────────────────────────────────────────────────────────────────────────
# Round-3 reviewer-fairy fault-injection finding (PR2, #1952): migration
# rung 4's trigger-drop-before-rebuild ordering. See engram_core.py's
# "Migration version 4" block (grep for it) for the fix itself.
#
# Before the fix, migration rung 4 delegated the FTS table rebuild to
# rebuild_fts_index BEFORE dropping+recreating the four FTS-sync triggers.
# rebuild_fts_index's DROP TABLE/CREATE VIRTUAL TABLE is DDL, and DDL
# auto-commits immediately in SQLite regardless of any surrounding
# transaction — so a fault raised between the rebuild returning and the old
# trigger-recreation code finishing left nodes_fts already in its NEW
# 6-column shape (auto-committed, can't be rolled back) while the triggers
# were still the OLD 5-column versions, still referencing `reported_state` —
# a column the rebuilt table no longer has. The migration's own except
# handler correctly left PRAGMA user_version at 3 for retry, but every node
# write in that SAME PROCESS then crashed with `OperationalError: table
# nodes_fts has no column named reported_state` — only a genuine process
# restart (which reruns the whole rung from scratch against user_version=3)
# actually recovered. The fix drops the triggers BEFORE delegating to
# rebuild_fts_index instead of after, closing the crash window: a fault
# mid-rebuild now leaves writes silently under-indexed until the retry
# succeeds — the same risk profile migration rung 2 already accepts —
# instead of hard-crashing on a stale-column reference.
# ─────────────────────────────────────────────────────────────────────────

_MIGRATION4_TAIL_TOKEN = "migration4roundthreeuniquetailtoken"


def _build_pre_migration4_truncated_feeling_db(core) -> None:
    """Build a realistic user_version=3 DB — reached via a real
    ``core._get_db()`` pass over the actual production schema — then
    regress ``nodes_fts`` and its four sync triggers to the pre-rung-4,
    5-column shape and stamp a ``feeling_report`` node with a truncated
    old-style claim (but a full, untruncated ``reported_state``). This is
    the exact pre-condition migration rung 4 exists to repair.

    ``core`` must already be a ``fresh_module(engram_core)`` instance with
    ``_configure_paths`` already pointed at an isolated temp dir. Leaves
    ``core._db_setup_done_paths`` cleared so the next ``core._get_db()``
    call re-runs ``_run_db_one_time_setup``, as it would on a real process
    restart against this same DB file.
    """
    conn = core._get_db()
    conn.close()

    long_reported_state = "s" * 200 + " " + _MIGRATION4_TAIL_TOKEN
    truncated_claim = "I reported feeling: " + "s" * 50 + "..."

    raw = sqlite3.connect(str(core.DB_PATH))
    raw.executescript(
        """
        DROP TABLE IF EXISTS nodes_fts_vocab;
        DROP TABLE IF EXISTS nodes_fts;
        DROP TRIGGER IF EXISTS nodes_fts_insert;
        DROP TRIGGER IF EXISTS nodes_fts_delete;
        DROP TRIGGER IF EXISTS nodes_retract_remove_from_fts;
        DROP TRIGGER IF EXISTS nodes_fts_supersede_remove;
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation, reported_state, trigger_text,
            content='nodes', content_rowid='rowid'
        );
        CREATE TRIGGER nodes_fts_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES (new.rowid, new.claim, new.quoted_text, new.interpretation, new.reported_state, new.trigger_text);
        END;
        CREATE TRIGGER nodes_fts_delete AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES ('delete', old.rowid, old.claim, old.quoted_text, old.interpretation, old.reported_state, old.trigger_text);
        END;
        CREATE TRIGGER nodes_retract_remove_from_fts
        AFTER UPDATE OF status ON nodes
        WHEN NEW.status = 'retracted' AND COALESCE(OLD.status, '') != 'retracted'
          AND OLD.is_current = 1
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES ('delete', OLD.rowid, COALESCE(OLD.claim, ''), COALESCE(OLD.quoted_text, ''),
                    COALESCE(OLD.interpretation, ''), COALESCE(OLD.reported_state, ''), COALESCE(OLD.trigger_text, ''));
        END;
        CREATE TRIGGER nodes_fts_supersede_remove
        AFTER UPDATE OF is_current ON nodes
        WHEN NEW.is_current = 0 AND OLD.is_current = 1
          AND COALESCE(NEW.status, '') != 'retracted'
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation, reported_state, trigger_text)
            VALUES ('delete', OLD.rowid, COALESCE(OLD.claim, ''), COALESCE(OLD.quoted_text, ''),
                    COALESCE(OLD.interpretation, ''), COALESCE(OLD.reported_state, ''), COALESCE(OLD.trigger_text, ''));
        END;
        """
    )
    raw.execute(
        "INSERT INTO nodes (id, type, created_at, claim, reported_state, status) "
        "VALUES ('fl_migration4_round3', 'feeling_report', '2026-08-21T00:00:00Z', ?, ?, 'active')",
        (truncated_claim, long_reported_state),
    )
    # Populate the FTS index the way a live pre-rung-4 install would have it.
    raw.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild')")
    raw.execute("PRAGMA user_version = 3")
    raw.commit()
    raw.close()

    core._db_setup_done_paths.clear()


def test_migration4_fault_injected_rebuild_stays_at_v3_and_does_not_crash_writes(monkeypatch):
    """Round-3 reviewer-fairy fault-injection regression (the Blocker).

    Fault-injects engram_backup.rebuild_fts_index to raise right after doing
    its real DDL swap — the reviewer's exact reproduction — against a
    realistic user_version=3 DB with a truncated feeling_report claim, then
    runs the real _get_db()/_run_db_one_time_setup migration path.

    Asserts: PRAGMA user_version stays at 3 (not bumped — the migration's
    own except handler already covered this), AND — the actual regression
    check the reviewer's finding was about — a subsequent node write does
    NOT raise OperationalError, proving the trigger-drop-before-rebuild
    reorder actually closed the crash window.
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

    core_test_dir = tempfile.mkdtemp(prefix="engram_test_migration4_fault_")
    try:
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        core._configure_paths(core_test_dir)
        core._ensure_data_dir()

        _build_pre_migration4_truncated_feeling_db(core)

        # Confirm the fixture actually starts at user_version 3 with the
        # stale, pre-rung-4 5-column nodes_fts shape.
        conn = sqlite3.connect(str(core.DB_PATH))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        pre_cols = [row[1] for row in conn.execute("PRAGMA table_info(nodes_fts)").fetchall()]
        conn.close()
        assert pre_cols == ["claim", "quoted_text", "interpretation", "reported_state", "trigger_text"], (
            f"test setup must actually produce the pre-rung-4, 5-column nodes_fts shape, got {pre_cols}"
        )

        # Patch the SAME engram_backup module object that
        # core._run_db_one_time_setup's lazy `import engram_backup` will
        # resolve to. fresh_module() purges the engram_* module family but
        # only re-imports engram_core itself by name; engram_backup is a
        # function-local, lazy import inside engram_core, so it is only
        # (re-)cached in sys.modules the first time _run_db_one_time_setup
        # actually runs — which the fixture builder's own core._get_db()
        # call above already triggered. Patching this cached module object
        # directly (rather than the top-of-file `engram_backup` name, which
        # after fresh_module()'s purge refers to a stale, orphaned module
        # object engram_core will never call into again) is what makes the
        # fault injection actually reach the code under test.
        live_engram_backup = sys.modules["engram_backup"]
        original_rebuild = live_engram_backup.rebuild_fts_index

        def _faulty_rebuild(db_path, *a, **kw):
            original_rebuild(db_path, *a, **kw)  # do the REAL DDL swap first
            raise sqlite3.DatabaseError(
                "round-3 regression test: simulated crash right after "
                "rebuild_fts_index's real DDL swap (reviewer-fairy's exact repro)"
            )

        monkeypatch.setattr(live_engram_backup, "rebuild_fts_index", _faulty_rebuild)

        conn = core._get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3, (
            f"migration rung 4 must leave PRAGMA user_version at 3 (not bumped) "
            f"when rebuild_fts_index faults mid-rebuild, got {version}"
        )

        # The actual regression check.
        try:
            conn.execute(
                "INSERT INTO nodes (id, type, created_at, claim, status) VALUES "
                "('ob_migration4_round3_write', 'observation', '2026-08-21T00:00:01Z', "
                "'post-fault write must not crash', 'active')"
            )
            conn.commit()
        except sqlite3.OperationalError as e:
            pytest.fail(
                f"a node write after the fault-injected migration-4 rung raised "
                f"OperationalError — the stale-trigger-vs-rebuilt-table crash "
                f"window is NOT closed: {e!r}"
            )
        conn.close()
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_migration4_happy_path_reindexes_full_claim_and_reaches_v4():
    """Companion happy-path test (also reviewer-requested): with no fault,
    migration rung 4 must still run clean, reach user_version 4, and the
    previously-truncated feeling_report claim must be FTS-findable in full
    — the ordering-correctness check (claim backfill BEFORE FTS reindex)
    the reviewer separately asked for, on the same realistic fixture used
    by the fault-injection test above.
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

    core_test_dir = tempfile.mkdtemp(prefix="engram_test_migration4_happy_")
    try:
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        core._configure_paths(core_test_dir)
        core._ensure_data_dir()

        _build_pre_migration4_truncated_feeling_db(core)

        conn = sqlite3.connect(str(core.DB_PATH))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        conn.close()

        conn = core._get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == LATEST_RUNG, (
            f"a clean run must carry the ladder all the way to "
            f"v{LATEST_RUNG}, got {version}"
        )

        claim = conn.execute(
            "SELECT claim FROM nodes WHERE id = 'fl_migration4_round3'"
        ).fetchone()[0]
        assert _MIGRATION4_TAIL_TOKEN in claim, (
            f"claim must be recomputed to the full, untruncated reported_state "
            f"text, got {claim!r}"
        )

        hits = conn.execute(
            "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH ?",
            (_MIGRATION4_TAIL_TOKEN,),
        ).fetchone()[0]
        assert hits == 1, (
            f"the previously-truncated claim's tail token must be FTS-findable "
            f"in full after migration rung 4, got {hits} hits"
        )
        conn.close()
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


# ── #2181: pre-migration backup — a copy taken BECAUSE a migration is about
# to run, keyed to migration rather than to nap. Reuses
# _build_pre_migration4_truncated_feeling_db (above) as the realistic
# pending-migration fixture — same user_version=3, truncated-claim
# feeling_report shape migration rung 4's own tests already exercise.

def _latest_rung_target():
    """The highest `PRAGMA user_version = N` any rung in
    _run_db_one_time_setup actually writes — parsed from the source, not
    hardcoded.

    Every version assertion in this file used to spell `4` literally, which
    meant adding rung 5 (#2324) broke eight unrelated tests that were only
    ever trying to say "the ladder finished". Hand-listing a value the
    codebase owns is the same staleness trap this file's own
    LATEST_SCHEMA_VERSION test exists to catch; deriving it means
    rung 6 costs nothing here.
    """
    import ast
    import re

    core_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "engram", "engram_core.py",
    )
    with open(core_path) as f:
        source = f.read()
    tree = ast.parse(source)
    func_node = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_run_db_one_time_setup"),
        None,
    )
    assert func_node is not None, "_run_db_one_time_setup not found via ast"
    func_source = ast.get_source_segment(source, func_node)
    written = [
        int(m) for m in re.findall(
            r'conn\.execute\(\s*"PRAGMA user_version = (\d+)"\s*\)', func_source
        )
    ]
    assert written, "no PRAGMA user_version writes found in the migration ladder"
    return max(written)


LATEST_RUNG = _latest_rung_target()


def _fresh_core_in_tmp(prefix: str):
    """Return (core, core_test_dir) — a fresh_module(engram_core) instance
    configured against a fresh temp dir, ready for _get_db()/
    _run_db_one_time_setup exercise. Caller owns cleanup (shutil.rmtree
    core_test_dir in a finally), matching the migration4 tests above.
    """
    import importlib
    import tempfile as _tempfile

    tests_dir = os.path.dirname(os.path.abspath(__file__))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from conftest import fresh_module

    engram_core = importlib.import_module("engram_core")
    core = fresh_module(engram_core)
    core_test_dir = _tempfile.mkdtemp(prefix=prefix)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    core._configure_paths(core_test_dir)
    core._ensure_data_dir()
    return core, core_test_dir


def test_2181_pre_migration_backup_fires_on_upgrade():
    """Test 1: a DB at user_version < LATEST with >=1 row in `nodes` gets a
    pre-migration-v*.db taken before the migration rewrites anything, and
    the backup holds the PRE-migration row content (the truncated claim),
    not the post-migration rewritten one.
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_fires_")
    try:
        _build_pre_migration4_truncated_feeling_db(core)

        conn = sqlite3.connect(str(core.DB_PATH))
        pre_claim = conn.execute(
            "SELECT claim FROM nodes WHERE id = 'fl_migration4_round3'"
        ).fetchone()[0]
        conn.close()
        assert pre_claim.startswith("I reported feeling: " + "s" * 50), (
            "test setup must actually start with the truncated pre-migration claim"
        )

        conn = core._get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == LATEST_RUNG, (
                f"expected migration to complete and reach "
                f"v{LATEST_RUNG}, got {version}"
            )
        post_claim = conn.execute(
            "SELECT claim FROM nodes WHERE id = 'fl_migration4_round3'"
        ).fetchone()[0]
        conn.close()
        assert _MIGRATION4_TAIL_TOKEN in post_claim, (
            "live DB should hold the POST-migration (rewritten) claim"
        )

        backup_dir = core.DATA_DIR / "db-backup"
        pre_mig_files = sorted(backup_dir.glob("pre-migration-v*.db"))
        assert len(pre_mig_files) == 1, (
            f"expected exactly one pre-migration-v*.db, got "
            f"{[p.name for p in pre_mig_files]}"
        )
        pre_mig_path = pre_mig_files[0]
        assert pre_mig_path.name.startswith("pre-migration-v3-"), (
            f"pre-migration filename must be keyed to the pre-migration "
            f"user_version (3), got {pre_mig_path.name}"
        )

        # Must be a valid SQLite DB holding the PRE-migration row content.
        backup_conn = sqlite3.connect(str(pre_mig_path))
        backup_claim = backup_conn.execute(
            "SELECT claim FROM nodes WHERE id = 'fl_migration4_round3'"
        ).fetchone()[0]
        backup_conn.close()
        assert backup_claim == pre_claim, (
            f"pre-migration backup must hold the PRE-migration claim "
            f"{pre_claim!r}, got {backup_claim!r}"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2181_pre_migration_backup_does_not_fire_on_fresh_db():
    """Test 2 — the regression this whole gate exists to prevent: a fresh,
    zero-row DB (user_version 0) must NOT get a pre-migration copy. Without
    the row-count gate this would fire on every install and every CI run.
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_fresh_")
    try:
        conn = core._get_db()  # fresh DB: creates schema, runs rungs 1-4 with 0 rows
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == LATEST_RUNG, (
            f"a fresh DB should still reach v{LATEST_RUNG} (0 rows, nothing "
            f"to migrate but the version gate must still advance), got {version}"
        )

        backup_dir = core.DATA_DIR / "db-backup"
        pre_mig_files = (
            list(backup_dir.glob("pre-migration-v*.db")) if backup_dir.exists() else []
        )
        assert pre_mig_files == [], (
            f"a fresh, zero-row DB must NOT get a pre-migration backup, "
            f"found {pre_mig_files}"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2181_pre_migration_backup_does_not_fire_when_already_current():
    """Test 3: a DB already at user_version == LATEST (4) with real rows
    must not get a pre-migration copy, or re-run any rung, on a second
    setup pass (_db_setup_done_paths cleared to simulate a second process
    startup against an already-migrated DB file).
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_current_")
    try:
        conn = core._get_db()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_RUNG
        conn.execute(
            "INSERT INTO nodes (id, type, created_at, claim, status) VALUES "
            "('ob_2181_current', 'observation', '2026-08-22T00:00:00Z', "
            "'already-current DB', 'active')"
        )
        conn.commit()
        conn.close()

        core._db_setup_done_paths.clear()
        conn = core._get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == LATEST_RUNG, (
            f"already-current DB must stay at v{LATEST_RUNG}, got {version}"
        )

        backup_dir = core.DATA_DIR / "db-backup"
        pre_mig_files = (
            list(backup_dir.glob("pre-migration-v*.db")) if backup_dir.exists() else []
        )
        assert pre_mig_files == [], (
            f"an already-current DB (user_version == LATEST) must not get "
            f"a pre-migration backup, found {pre_mig_files}"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2181_backup_failure_blocks_migration():
    """Test 4 — the control that proves the guard is a guard. Force the
    pre-migration copy to fail (an unwritable db-backup/ dir) against a
    realistic user_version=3 DB with a real pending rewrite, and assert
    the migration did NOT proceed: user_version stays at 3, and the
    feeling_report claim is still the truncated PRE-migration value.
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_blocks_")
    try:
        _build_pre_migration4_truncated_feeling_db(core)

        backup_dir = core.DATA_DIR / "db-backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(backup_dir), 0o500)  # r-x: exists()/mkdir ok, file write fails
        try:
            conn = core._get_db()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 3, (
                f"a failed pre-migration backup must leave user_version "
                f"unchanged (3) so the whole migration retries next "
                f"startup, got {version}"
            )
            claim = conn.execute(
                "SELECT claim FROM nodes WHERE id = 'fl_migration4_round3'"
            ).fetchone()[0]
            conn.close()
            assert (
                claim.startswith("I reported feeling: " + "s" * 50)
                and _MIGRATION4_TAIL_TOKEN not in claim
            ), (
                f"data must be UNREWRITTEN when the pre-migration backup "
                f"fails — got claim {claim!r}"
            )
        finally:
            os.chmod(str(backup_dir), 0o700)
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2181_escape_hatch_proceeds_and_is_loud(capsys):
    """Test 5: ENGRAM_SKIP_PRE_MIGRATION_BACKUP=1 with a failing backup dir
    — the migration must proceed anyway (user_version advances) and a
    stderr line must say the backup was skipped deliberately.
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_escape_")
    try:
        _build_pre_migration4_truncated_feeling_db(core)

        backup_dir = core.DATA_DIR / "db-backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(backup_dir), 0o500)
        os.environ["ENGRAM_SKIP_PRE_MIGRATION_BACKUP"] = "1"
        try:
            capsys.readouterr()  # clear anything already buffered
            conn = core._get_db()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            assert version == LATEST_RUNG, (
                f"the escape hatch must let the migration proceed to "
                f"completion even though the backup dir is unwritable, "
                f"got {version}"
            )
            captured = capsys.readouterr()
            assert "ENGRAM_SKIP_PRE_MIGRATION_BACKUP" in captured.err, (
                f"escape-hatch use must be logged loudly to stderr, got "
                f"stderr={captured.err!r}"
            )
        finally:
            os.environ.pop("ENGRAM_SKIP_PRE_MIGRATION_BACKUP", None)
            os.chmod(str(backup_dir), 0o700)
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2181_nothing_reaches_stdout_during_migrating_setup(capsys):
    """Test 6: across a full _run_db_one_time_setup pass on an upgrading
    DB, stdout must be completely empty (this is an MCP stdio server;
    anything on stdout corrupts the protocol frame) and the diagnostics
    must land on stderr instead. Mutation-verify this by hand (not part of
    the automated suite): temporarily drop `file=sys.stderr` from one of
    the pre-migration-backup print() calls, confirm this test FAILS, then
    restore it.

    Deliberately does NOT reuse _build_pre_migration4_truncated_feeling_db's
    stale-5-column nodes_fts shape: that shape trips the PRE-EXISTING,
    out-of-scope #2080 startup self-heal (engram_core.py ~2033-2044), which
    itself prints to stdout independent of anything in this PR — the spec's
    own "STDOUT IS FORBIDDEN HERE" note names that exact leak as a known,
    separately-scoped issue (only the two rung-4 failure-path prints were
    in-scope to fix here). Building the FTS/triggers in their already-
    canonical shape (via a real _get_db() pass) and only then rewinding
    user_version + re-truncating the claim isolates this test to the code
    path THIS PR actually touches (the pre-migration backup + rung 4's
    happy path), without silently laundering an unrelated, already-flagged
    bug into a passing assertion.
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_stdout_")
    try:
        # Bootstrap a fresh DB (0 rows) so the schema -- including
        # nodes_fts + its sync triggers -- comes out in the real,
        # already-canonical 6-column shape the current code produces.
        conn = core._get_db()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_RUNG
        conn.close()

        # Rewind to a pending-rung-4 precondition WITHOUT touching the
        # already-canonical FTS/trigger shape: insert a feeling_report with
        # the old truncated-claim form, then set user_version back to 3.
        raw = sqlite3.connect(str(core.DB_PATH))
        raw.execute(
            "INSERT INTO nodes (id, type, created_at, claim, reported_state, status) "
            "VALUES ('fl_2181_stdout_check', 'feeling_report', "
            "'2026-08-21T00:00:00Z', ?, ?, 'active')",
            (
                "I reported feeling: " + "s" * 50 + "...",
                "s" * 200 + " " + _MIGRATION4_TAIL_TOKEN,
            ),
        )
        raw.execute("PRAGMA user_version = 3")
        raw.commit()
        raw.close()
        core._db_setup_done_paths.clear()

        capsys.readouterr()

        conn = core._get_db()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == LATEST_RUNG, (
            f"fixture must actually re-run rung 4 and carry the ladder to "
            f"v{LATEST_RUNG}, got {version}"
        )

        captured = capsys.readouterr()
        assert captured.out == "", (
            f"an MCP stdio server must never write to stdout during "
            f"one-time DB setup — got stdout={captured.out!r}"
        )
        assert captured.err != "", (
            "expected at least one diagnostic on stderr during a "
            "migrating setup pass, got none"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2181_existing_daily_backup_callers_unchanged():
    """Test 7: the two _write_binary_backup call sites in the nap paths
    (both call it as `_write_binary_backup(str(DB_PATH), DATA_DIR /
    "db-backup")` — two positional args, no filename/glob_pattern) must
    keep producing knowledge-YYYYMMDD.db with the unchanged same-day-skip
    and max_keep=7 rotation now that those two new parameters exist.
    """
    import datetime as _datetime
    import shutil
    import tempfile
    from pathlib import Path

    import engram_core as core

    tmp = tempfile.mkdtemp(prefix="engram_test_2181_callers_")
    try:
        src_db = os.path.join(tmp, "knowledge.db")
        conn = sqlite3.connect(src_db)
        conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT)")
        conn.execute("INSERT INTO nodes VALUES ('ob_1', 'hello')")
        conn.commit()
        conn.close()

        backup_dir = Path(tmp) / "db-backup"
        backup_dir.mkdir()

        # 8 pre-existing dated files, NOT today, staggered mtimes so
        # pruning order is deterministic.
        today = _datetime.date.today()
        for i in range(8):
            d = (today - _datetime.timedelta(days=i + 1)).strftime("%Y%m%d")
            p = backup_dir / f"knowledge-{d}.db"
            p.write_bytes(b"x")
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))

        # Call exactly as the two nap-path call sites do: two positional args.
        result = core._write_binary_backup(src_db, backup_dir)
        assert "path" in result, f"expected a successful write, got {result}"
        expected = backup_dir / f"knowledge-{today.strftime('%Y%m%d')}.db"
        assert Path(result["path"]) == expected, (
            f"default naming must be unchanged: expected {expected}, got "
            f"{result['path']}"
        )
        # 8 old + 1 new = 9 total; max_keep default 7 -> 2 pruned.
        assert result["pruned"] == 2, (
            f"expected max_keep=7 default to prune 2, got {result}"
        )
        remaining = sorted(p.name for p in backup_dir.glob("knowledge-*.db"))
        assert len(remaining) == 7, f"expected 7 files to remain, got {remaining}"

        # Second call same day -> same-day idempotency skip, unchanged.
        result2 = core._write_binary_backup(src_db, backup_dir)
        assert result2.get("skipped") == "same-day copy exists", (
            f"same-day idempotency skip must still fire with the default "
            f"(filename=None) call, got {result2}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def test_2181_pre_migration_backup_sources_from_conn_not_db_path():
    """Round-2 blocker fix: the backup SOURCE must come from the connection
    actually being migrated (PRAGMA database_list), never the module-global
    DB_PATH. Reproduces the coordinator's own probe: seed DB_PATH with an
    unrelated, already-current (v4) graph carrying its own marker row, then
    build a SEPARATE db file with a DIFFERENT marker row at a pending
    user_version (3) and migrate THAT connection directly (bypassing
    _get_db(), which always opens against DB_PATH -- the only way to
    exercise a "foreign" conn is to call _run_db_one_time_setup directly,
    exactly as the probe did). The resulting pre-migration backup must
    contain the FOREIGN row and must NOT contain the DB_PATH row.
    """
    import shutil
    import tempfile

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_foreign_")
    foreign_dir = None
    try:
        # DB_PATH: a different, already-current graph. Must never appear in
        # the foreign connection's backup below.
        conn = core._get_db()
        conn.execute(
            "INSERT INTO nodes (id, type, created_at, claim, status) VALUES "
            "('zz_DBPATH_MARKER', 'observation', '2026-08-22T00:00:00Z', "
            "'DB_PATH graph -- must never appear in the foreign backup', "
            "'active')"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_RUNG
        conn.close()

        # A SEPARATE db file -- built via a first _run_db_one_time_setup
        # pass so its schema is the real production schema (not a hand-
        # rolled subset), then rewound to a pending user_version with its
        # own marker row, exactly as _build_pre_migration4_truncated_feeling_db
        # does above for DB_PATH.
        foreign_dir = tempfile.mkdtemp(prefix="engram_test_2181_foreign_db_")
        foreign_path = os.path.join(foreign_dir, "foreign.db")
        foreign_conn = sqlite3.connect(foreign_path)
        foreign_conn.row_factory = sqlite3.Row
        core._run_db_one_time_setup(foreign_conn)  # bootstrap real schema, 0 rows -> v4
        assert foreign_conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_RUNG

        foreign_conn.execute(
            "INSERT INTO nodes (id, type, created_at, claim, status) VALUES "
            "('zz_FOREIGN_MARKER', 'observation', '2026-08-22T00:00:01Z', "
            "'foreign graph row -- the one actually being migrated', "
            "'active')"
        )
        foreign_conn.execute("PRAGMA user_version = 3")
        foreign_conn.commit()

        # Migrate the FOREIGN connection directly -- the exact reproduction
        # shape of the coordinator's probe.
        core._run_db_one_time_setup(foreign_conn)
        foreign_version = foreign_conn.execute("PRAGMA user_version").fetchone()[0]
        foreign_conn.close()
        assert foreign_version == LATEST_RUNG, (
            f"the foreign connection should still migrate normally, got {foreign_version}"
        )

        backup_dir = core.DATA_DIR / "db-backup"
        pre_mig_files = sorted(backup_dir.glob("pre-migration-v*.db"))
        assert len(pre_mig_files) == 1, (
            f"expected exactly one pre-migration backup, got "
            f"{[p.name for p in pre_mig_files]}"
        )

        backup_conn = sqlite3.connect(str(pre_mig_files[0]))
        backup_ids = {row[0] for row in backup_conn.execute("SELECT id FROM nodes")}
        backup_conn.close()

        assert "zz_FOREIGN_MARKER" in backup_ids, (
            f"the pre-migration backup must contain the FOREIGN connection's "
            f"row (the one actually being migrated), got {backup_ids}"
        )
        assert "zz_DBPATH_MARKER" not in backup_ids, (
            f"the pre-migration backup must NOT be sourced from the "
            f"module-global DB_PATH -- got {backup_ids}"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)
        if foreign_dir is not None:
            shutil.rmtree(foreign_dir, ignore_errors=True)


def test_2181_pre_migration_backup_skips_for_in_memory_connection(capsys):
    """An in-memory connection (":memory:") has no on-disk file to copy, so
    the pre-migration-backup step must skip it (loudly, on stderr) rather
    than silently falling back to DB_PATH -- which would reintroduce the
    wrong-source bug for the one caller most likely to hit it.

    Both calls below now raise ValueError out of migration rung 4 -- updated
    for #2186 (rebuild_fts_index now refuses an empty db_path instead of
    silently opening an unrelated scratch database). Confirmed by direct
    repro against the UNMODIFIED base commit (cae464e) that migration rung 4
    itself cannot complete against a genuinely in-memory connection --
    rebuild_fts_index re-opens its OWN connection via a
    PRAGMA-database_list-derived path, which is empty for ":memory:". Before
    #2186 that silently operated on an unrelated scratch database instead of
    the real one and rung 4 quietly gave up (caught by its own
    sqlite3.DatabaseError handler, printed "FAILED, will retry", never
    raised); after #2186 the same empty path is a refused ValueError, which
    is NOT a sqlite3.DatabaseError/ImportError so rung 4's existing except
    clauses do not catch it and it propagates out of
    _run_db_one_time_setup instead -- the exact "fails LOUDLY instead of
    silently stalling" behavior #2186 is about. This is a real, pre-existing,
    orthogonal limitation in rung 4 (predates this PR entirely -- reproduced
    identically on cae464e before any of #2181's changes), not something
    #2181 or #2186 is scoped to fix (#2186 deliberately fixes only
    rebuild_fts_index itself and the #2080 startup self-heal call site, not
    rung 4). What THIS test asserts is only what #2181's own code is
    responsible for: the backup step does not crash, does not write a
    backup file, and does not fall back to DB_PATH -- it correctly skips
    (visible on stderr before rung 4's later, unrelated raise) and lets the
    (separately, pre-existingly limited) migration attempt proceed on its
    own terms.
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_inmemory_")
    try:
        mem_conn = sqlite3.connect(":memory:")
        mem_conn.row_factory = sqlite3.Row
        capsys.readouterr()
        with pytest.raises(ValueError, match="db_path is empty"):
            core._run_db_one_time_setup(mem_conn)  # bootstrap pass, 0 rows;
            # reaches rung 4 (out of #2186's scope) and raises there -- see
            # docstring.

        backup_dir = core.DATA_DIR / "db-backup"
        pre_mig_files = (
            list(backup_dir.glob("pre-migration-v*.db")) if backup_dir.exists() else []
        )
        assert pre_mig_files == [], (
            f"an in-memory connection must never produce a pre-migration "
            f"backup file (nothing on disk to copy), found {pre_mig_files}"
        )

        # Force a pending-migration precondition without going through
        # rung 4's own re-open path a second time: rewind user_version and
        # insert a marker row directly, then re-invoke and confirm the
        # SKIP branch (not a crash, not a DB_PATH fallback) is what fires
        # for the pre-migration-backup step -- before rung 4 (unmodified,
        # out of #2186's scope) raises again.
        mem_conn.execute(
            "INSERT INTO nodes (id, type, created_at, claim, status) VALUES "
            "('zz_MEM_MARKER', 'observation', '2026-08-22T00:00:02Z', "
            "'in-memory graph row', 'active')"
        )
        mem_conn.execute("PRAGMA user_version = 3")
        mem_conn.commit()

        capsys.readouterr()
        with pytest.raises(ValueError, match="db_path is empty"):
            core._run_db_one_time_setup(mem_conn)
        mem_conn.close()

        captured = capsys.readouterr()
        assert "in-memory" in captured.err and "SKIPPED" in captured.err, (
            f"expected the in-memory-skip diagnostic on stderr, got "
            f"stderr={captured.err!r}"
        )

        pre_mig_files = (
            list(backup_dir.glob("pre-migration-v*.db")) if backup_dir.exists() else []
        )
        assert pre_mig_files == [], (
            f"an in-memory connection must never produce a pre-migration "
            f"backup file on a second pass either, found {pre_mig_files}"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2181_latest_schema_version_constant_matches_max_rung_target():
    """Hardening (flagged, now closed): the latest schema version has no
    structural link to the rungs -- it is a hand-maintained mirror of
    "the highest PRAGMA user_version = N a rung actually writes". Add a
    rung 5 and forget to bump the constant, and current_user_version < 4
    is False for an already-v4 graph, so no backup is taken before rung 5
    rewrites data -- silent, and in the reassuring direction. This test
    parses engram_core.py, finds every `conn.execute("PRAGMA user_version
    = N")` literal actually written inside _run_db_one_time_setup, and
    asserts the MODULE-LEVEL `LATEST_SCHEMA_VERSION` (#3753: promoted from a
    function-local so the owner's start() gate reads the same constant)
    equals the max.
    """
    import ast
    import re

    core_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "engram", "engram_core.py",
    )
    with open(core_path) as f:
        source = f.read()

    tree = ast.parse(source)
    func_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_db_one_time_setup":
            func_node = node
            break
    assert func_node is not None, "_run_db_one_time_setup not found via ast"

    func_source = ast.get_source_segment(source, func_node)
    assert func_source is not None, "could not extract function source"

    # Only the actual write call, not any comment/docstring mentioning the
    # same phrase -- comments are free text and must not feed the oracle.
    versions_written = [
        int(m)
        for m in re.findall(
            r'conn\.execute\(\s*"PRAGMA user_version = (\d+)"\s*\)',
            func_source,
        )
    ]
    assert versions_written, (
        "expected at least one `conn.execute(\"PRAGMA user_version = N\")` "
        "write inside _run_db_one_time_setup"
    )
    max_rung_version = max(versions_written)

    # The constant lives at MODULE level (a top-level assignment), and the
    # migration function must not carry a function-local copy of it.
    module_assigns = [
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "LATEST_SCHEMA_VERSION" for t in n.targets)
    ]
    assert len(module_assigns) == 1, (
        "expected exactly one module-level `LATEST_SCHEMA_VERSION = <int>` in engram_core.py"
    )
    assert isinstance(module_assigns[0].value, ast.Constant) and isinstance(
        module_assigns[0].value.value, int
    )
    latest_const = module_assigns[0].value.value
    assert not re.search(r"\bLATEST_SCHEMA_VERSION\s*=", func_source), (
        "_run_db_one_time_setup must read the module-level LATEST_SCHEMA_VERSION, "
        "not assign a local copy"
    )
    assert "LATEST_SCHEMA_VERSION" in func_source

    assert latest_const == max_rung_version, (
        f"LATEST_SCHEMA_VERSION={latest_const} must equal the "
        f"highest `PRAGMA user_version = N` literal a rung actually writes "
        f"({max_rung_version}) -- add a rung without bumping this constant "
        f"and the pre-migration backup silently stops firing before the "
        f"new rung runs."
    )



class _CountFailingConnProxy:
    """Wraps a real sqlite3.Connection; forwards everything through except
    `execute("SELECT COUNT(*) FROM nodes")`, which raises
    sqlite3.OperationalError -- reproducing a transient SQLITE_BUSY on
    exactly the row-count read the fresh-DB gate performs. Matches the
    coordinator's own reproduction shape for the round-5 blocker.
    """

    def __init__(self, real_conn):
        self._real = real_conn

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and "SELECT COUNT(*) FROM nodes" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_2181_row_count_read_failure_does_not_skip_the_backup():
    """Round-5 blocker: a FAILED row-count read must be treated as
    unknown-therefore-protect, never as "empty graph". Before the fix, a
    transient OperationalError on the `SELECT COUNT(*) FROM nodes` query
    (e.g. SQLITE_BUSY) was silently mapped to 0 rows, the fresh-DB gate
    returned True (nothing worth protecting), and the irreversible
    migration proceeded with NO restore point on a genuinely populated
    graph.

    Reproduces the coordinator's own probe: a populated DB with one row at
    user_version=3, the COUNT query proxied to raise, migrate the proxied
    connection, and assert a real pre-migration backup WAS taken (the
    underlying file on disk is not actually locked in this test -- only
    the proxy's COUNT call is faked -- so the fixed behavior is a normal,
    successful backup followed by a completed migration; the pre-fix
    behavior was a completed migration with ZERO backup files, exactly
    the coordinator's own measured output).
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2181_countfail_")
    try:
        _build_pre_migration4_truncated_feeling_db(core)

        real_conn = sqlite3.connect(str(core.DB_PATH))
        real_conn.row_factory = sqlite3.Row
        proxy_conn = _CountFailingConnProxy(real_conn)

        core._run_db_one_time_setup(proxy_conn)
        version = real_conn.execute("PRAGMA user_version").fetchone()[0]
        real_conn.close()

        backup_dir = core.DATA_DIR / "db-backup"
        pre_mig_files = (
            list(backup_dir.glob("pre-migration-v*.db")) if backup_dir.exists() else []
        )

        assert version == LATEST_RUNG, (
            f"migration should reach completion once the (unaffected, "
            f"file-level) backup succeeds despite the COUNT read failing, "
            f"got user_version={version}"
        )
        assert len(pre_mig_files) == 1, (
            f"expected exactly one pre-migration backup taken -- a failed "
            f"row-count read must fall through to a real backup attempt, "
            f"not skip it as if the graph were empty. Found: "
            f"{[p.name for p in pre_mig_files]}"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)


# ── #2186: rebuild_fts_index("") silently rebuilt the WRONG (an implicit
# SQLite scratch) database for an in-memory connection, because SQLite
# treats an empty-string path as "open a private temp DB", not an error.
# Every caller that derives db_path from
# conn.execute("PRAGMA database_list").fetchone()[2] gets an empty string
# for an in-memory connection, so this silently corrupted the intended
# repair instead of failing. Fixed at the layer closest to the effect: one
# guard inside rebuild_fts_index itself, covering every call site.

def test_2186_rebuild_fts_index_rejects_empty_db_path():
    """The most direct regression guard for the actual library-level fix,
    independent of the migration-ladder integration test below:
    rebuild_fts_index must reject a falsy db_path with a loud ValueError
    rather than silently opening an implicit SQLite scratch database.
    """
    with pytest.raises(ValueError, match="db_path is empty"):
        engram_backup.rebuild_fts_index("")

    with pytest.raises(ValueError, match="db_path is empty"):
        engram_backup.rebuild_fts_index(None)


def test_2186_migration_ladder_against_in_memory_reaches_v4_or_fails_loudly():
    """#2186's own required test (from the issue): run
    _run_db_one_time_setup against a fresh, genuinely in-memory (":memory:")
    connection and assert the migration ladder either reaches the latest
    PRAGMA user_version (4) or fails LOUDLY -- it must never silently
    stall while returning normally.

    Pre-#2186, this genuinely DID silently stall: rung 4 delegated to
    rebuild_fts_index("") -- an empty PRAGMA-database_list-derived path --
    which silently opened an unrelated scratch database, leaving the real
    connection's nodes_fts stuck at a stale column set. Rung 4's own
    subsequent write against that stale table then raised
    OperationalError('table nodes_fts has no column named recall_summary'),
    which rung 4's `except sqlite3.DatabaseError` handler caught, printed
    "FAILED, will retry on next startup" for, and swallowed --
    _run_db_one_time_setup returned normally with user_version stuck at 3,
    with no exception ever surfacing to the caller (see
    test_2181_pre_migration_backup_skips_for_in_memory_connection's
    docstring for the same repro, confirmed directly against the
    unmodified base commit).

    As of #2186, rebuild_fts_index refuses an empty db_path outright
    (raises ValueError). Migration rung 4 itself is deliberately NOT
    modified by #2186 (out of scope -- see the #2080 startup self-heal
    fix in engram_core.py, which IS in scope, versus rung 4's own
    version-gated block, which is not), so it still cannot complete
    against a genuinely in-memory connection: the ValueError it now gets
    from rebuild_fts_index is not a sqlite3.DatabaseError/ImportError, so
    rung 4's existing except clauses do not catch it and it propagates
    out of _run_db_one_time_setup uncaught instead. That satisfies this
    test's actual requirement -- fails LOUDLY rather than silently
    stalling -- even though it does not reach v4. The except clause below
    is narrowed to ValueError specifically (today's actual failure mode,
    documented above) rather than a bare Exception, so an unrelated bug
    elsewhere is not silently absorbed as "loud failure, that counts";
    if rung 4 is ever fixed to complete against an in-memory connection,
    the `else` branch below already covers that outcome without needing
    to touch this test.
    """
    core, core_test_dir = _fresh_core_in_tmp("engram_test_2186_ladder_")
    try:
        mem_conn = sqlite3.connect(":memory:")
        mem_conn.row_factory = sqlite3.Row
        try:
            core._run_db_one_time_setup(mem_conn)
        except ValueError as exc:
            assert "db_path is empty" in str(exc) or "rebuild_fts_index" in str(exc), (
                f"expected the #2186 empty-db_path guard's ValueError, got "
                f"a differently-worded ValueError: {exc!r}"
            )
        else:
            version = mem_conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == LATEST_RUNG, (
                f"_run_db_one_time_setup returned without raising, so the "
                f"migration ladder must have reached the latest version "
                f"({LATEST_RUNG}) -- silently stalling at an intermediate version "
                f"while returning normally is exactly the #2186 bug. Got "
                f"user_version={version}"
            )
        mem_conn.close()
    finally:
        import shutil
        shutil.rmtree(core_test_dir, ignore_errors=True)


def test_2186_self_heal_skip_branch_fires_on_stale_in_memory_nodes_fts():
    """Direct exercise of the new SKIP branch inside the #2080 self-heal
    block (reviewer-fairy suggestion on PR #2230) -- neither
    test_2181_pre_migration_backup_skips_for_in_memory_connection nor
    test_2186_migration_ladder_against_in_memory_reaches_v4_or_fails_loudly
    reaches it: both hit rung 4's raise before nodes_fts could ever have a
    stale schema, because a freshly-bootstrapped in-memory connection gets a
    canonical nodes_fts from the CREATE VIRTUAL TABLE IF NOT EXISTS earlier
    in _run_db_one_time_setup, so `_existing_fts_cols != _canonical_fts_cols`
    (the self-heal's own trigger condition) is never true on either of their
    calls.

    This test manufactures that condition directly: bootstrap a `:memory:`
    connection once (canonical nodes_fts), then corrupt nodes_fts to a stale
    3-column shape on that SAME connection object (mirroring
    test_2077_startup_self_heal_repairs_stale_nodes_fts_on_get_db's
    file-backed technique, adapted for in-memory -- there is no file to
    reopen, so the corruption happens directly on the live connection), then
    re-invoke _run_db_one_time_setup and confirm the SKIP branch actually
    ran (the "in-memory ... SKIPPED" diagnostic on stderr) rather than
    silently falling through some other path.
    """
    import shutil

    core, core_test_dir = _fresh_core_in_tmp("engram_test_2186_selfheal_skip_")
    try:
        mem_conn = sqlite3.connect(":memory:")
        mem_conn.row_factory = sqlite3.Row

        # First call: bootstrap. Reaches rung 4 and raises (established by
        # test_2186_migration_ladder_...) -- irrelevant here, the self-heal
        # block (which runs BEFORE the version-gated rungs) has already done
        # its job by then, creating a canonical nodes_fts.
        try:
            core._run_db_one_time_setup(mem_conn)
        except ValueError:
            pass

        canonical_cols = [
            row[1] for row in mem_conn.execute("PRAGMA table_info(nodes_fts)").fetchall()
        ]
        assert canonical_cols, (
            "test setup must produce a real nodes_fts table on the first "
            "bootstrap call before it can be corrupted"
        )

        # Corrupt nodes_fts to a stale 3-column shape directly on the live
        # connection -- same technique as test_2077, adapted: no file to
        # reopen for an in-memory DB, so the DROP/CREATE runs on mem_conn
        # itself rather than a second sqlite3.connect(path).
        mem_conn.executescript(
            """
            DROP TABLE IF EXISTS nodes_fts;
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                claim, quoted_text, interpretation,
                content='nodes', content_rowid='rowid'
            );
            INSERT INTO nodes_fts(nodes_fts) VALUES ('rebuild');
            """
        )
        mem_conn.commit()

        stale_cols = [
            row[1] for row in mem_conn.execute("PRAGMA table_info(nodes_fts)").fetchall()
        ]
        assert stale_cols == ["claim", "quoted_text", "interpretation"], (
            f"test setup must actually produce the stale 3-column shape, got {stale_cols}"
        )
        assert stale_cols != canonical_cols, (
            "the corrupted shape must actually differ from the canonical one, "
            "or the self-heal's own trigger condition won't fire"
        )

        import io
        import contextlib

        # The self-heal block's SKIP diagnostic is a plain print() (stdout),
        # matching its own sibling "repairing..." branch in the same
        # if/else -- NOT the file=sys.stderr convention
        # _take_pre_migration_backup_if_pending uses for its own, differently
        # worded in-memory-skip message. Capture stdout accordingly; an
        # earlier draft of this test wrongly assumed stderr by analogy with
        # that other function and failed against the real code, which is
        # exactly the kind of assumption this test exists to catch rather
        # than make.
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            try:
                core._run_db_one_time_setup(mem_conn)
            except ValueError:
                pass  # rung 4, same as every other #2186 in-memory test -- irrelevant here
        mem_conn.close()

        captured_out = stdout_buf.getvalue()
        assert "in-memory" in captured_out and "SKIPPED" in captured_out, (
            f"expected the #2186 self-heal SKIP diagnostic on stdout (the "
            f"branch this test exists to exercise), got stdout={captured_out!r}"
        )
        assert "#2186" in captured_out, (
            f"expected the SKIP diagnostic to cite #2186 (matching the "
            f"engram_core.py source), got stdout={captured_out!r}"
        )
    finally:
        shutil.rmtree(core_test_dir, ignore_errors=True)
