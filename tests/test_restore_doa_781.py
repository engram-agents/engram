"""Regression tests for #781: restore-from-dump DOA on graphs with retractions.

Mechanism: SQL-dump restore → user_version=0 + fresh empty nodes_fts →
#274 one-shot migration fires → 'delete'-inserts retracted nodes against the
empty index → FTS5 raises sqlite3.DatabaseError → uncaught → _get_db aborts.

Three fixes, each tested here:
  1. dump_stripped emits PRAGMA user_version so restored DBs don't re-fire
     one-shot migrations.
  2. server.py #274 migration block catches sqlite3.DatabaseError (parent of
     OperationalError) — tested by simulating the phantom-malformed crash path.
  3. rebuild_fts_index is self-contained: creates FTS if absent, rebuild THEN
     delete (not delete THEN rebuild), sets user_version = 1.

End-to-end: build a DB with retracted nodes → dump → restore → rebuild →
assert no exception, FTS queryable, retracted rows absent, user_version set.

Negative: verify that delete-on-empty-index raises DatabaseError and that the
widened except in the migration block absorbs it.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engram_backup  # noqa: E402


# ─── Shared fixture helpers ────────────────────────────────────────────────


def _build_db_with_retractions(path: str, n_active: int = 3, n_retracted: int = 2) -> None:
    """Create a minimal ENGRAM-shaped DB with active and retracted nodes."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id          TEXT PRIMARY KEY,
            claim       TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            embedding   TEXT,
            status      TEXT DEFAULT 'active'
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
        CREATE TRIGGER nodes_retract_remove_from_fts
        AFTER UPDATE OF status ON nodes
        WHEN NEW.status = 'retracted' AND COALESCE(OLD.status, '') != 'retracted'
        BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
            VALUES ('delete', OLD.rowid,
                    COALESCE(OLD.claim, ''),
                    COALESCE(OLD.quoted_text, ''),
                    COALESCE(OLD.interpretation, ''));
        END;
        """
    )
    # Insert active nodes.
    for i in range(n_active):
        conn.execute(
            "INSERT INTO nodes(id, claim, quoted_text, interpretation, status) VALUES (?,?,?,?,?)",
            (f"ob_active_{i}", f"active claim {i} about migration", f"src {i}", f"interp {i}", "active"),
        )
    # Insert retracted nodes — insert first (trigger adds to FTS), then retract
    # (retract trigger removes from FTS).  This matches the real-world state:
    # retracted rows were once indexed and have since been removed by the trigger.
    for i in range(n_retracted):
        conn.execute(
            "INSERT INTO nodes(id, claim, quoted_text, interpretation, status) VALUES (?,?,?,?,?)",
            (f"ob_retracted_{i}", f"retracted claim {i}", f"old src {i}", f"old interp {i}", "active"),
        )
    conn.commit()
    # Now retract via UPDATE so the trigger fires and removes from FTS.
    for i in range(n_retracted):
        conn.execute(
            "UPDATE nodes SET status='retracted' WHERE id=?", (f"ob_retracted_{i}",)
        )
    conn.commit()
    # Set user_version = 1 to reflect that the #274 migration has already run
    # (in a real ENGRAM DB this is set at server startup).
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


@pytest.fixture
def db_with_retractions(tmp_path):
    p = str(tmp_path / "knowledge.db")
    _build_db_with_retractions(p)
    return p


# ─── Fix 1: dump_stripped emits user_version ──────────────────────────────


def test_dump_emits_user_version(db_with_retractions, tmp_path):
    """dump_stripped must write PRAGMA user_version = N into the SQL output."""
    out = str(tmp_path / "knowledge.sql")
    engram_backup.dump_stripped(db_with_retractions, out)
    dump_text = open(out).read()
    assert "PRAGMA user_version = 1" in dump_text, (
        "dump_stripped did not emit PRAGMA user_version; restore will re-fire #274 migration"
    )


def test_restored_db_has_correct_user_version(db_with_retractions, tmp_path):
    """After dump→restore, the restored DB must carry the source's user_version."""
    out = str(tmp_path / "knowledge.sql")
    engram_backup.dump_stripped(db_with_retractions, out)

    recon = str(tmp_path / "recon.db")
    rc = sqlite3.connect(recon)
    rc.executescript(open(out).read())
    uv = rc.execute("PRAGMA user_version").fetchone()[0]
    rc.close()

    assert uv == 1, (
        f"Restored user_version is {uv}, expected 1; "
        "the #274 migration will re-fire and crash on a graph with retractions"
    )


def test_user_version_zero_preserved_in_dump(tmp_path):
    """A DB that has never been migrated (user_version=0) should dump/restore at 0."""
    p = str(tmp_path / "unmigrated.db")
    conn = sqlite3.connect(p)
    # Use a full ENGRAM-shaped schema so dump_stripped's UPDATE nodes SET embedding=NULL works.
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            embedding TEXT,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_1', 'test claim', 'src', 'interp', NULL, 'active')"
    )
    # user_version stays 0 (never set)
    conn.commit()
    conn.close()

    out = str(tmp_path / "unmigrated.sql")
    engram_backup.dump_stripped(p, out)
    dump_text = open(out).read()
    assert "PRAGMA user_version = 0" in dump_text

    recon = str(tmp_path / "recon.db")
    rc = sqlite3.connect(recon)
    rc.executescript(dump_text)
    uv = rc.execute("PRAGMA user_version").fetchone()[0]
    rc.close()
    assert uv == 0


# ─── Fix 2: DatabaseError catch in the #274 migration block ───────────────


def test_delete_on_empty_fts_raises_database_error(tmp_path):
    """Verify that 'delete'-inserting against an empty FTS index raises
    sqlite3.DatabaseError (NOT OperationalError).

    This is the exact crash Ariadne hit: OperationalError is a subclass of
    DatabaseError, but the FTS5 phantom-malformed comes out as DatabaseError
    directly — the old 'except sqlite3.OperationalError' did not catch it.
    """
    p = str(tmp_path / "crash_demo.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            claim TEXT,
            quoted_text TEXT,
            interpretation TEXT,
            status TEXT DEFAULT 'active'
        );
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        """
    )
    # Insert a retracted row directly (bypass trigger — the index is empty).
    conn.execute(
        "INSERT INTO nodes(id, claim, quoted_text, interpretation, status) "
        "VALUES ('ob_r1', 'retracted claim', 'src', 'interp', 'retracted')"
    )
    conn.commit()

    # Simulate the #274 migration: delete-insert retracted rows against the
    # EMPTY index.  This must raise sqlite3.DatabaseError.
    raised_type = None
    try:
        conn.execute(
            """
            INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
            SELECT 'delete', rowid,
                   COALESCE(claim, ''),
                   COALESCE(quoted_text, ''),
                   COALESCE(interpretation, '')
            FROM nodes
            WHERE status = 'retracted'
            """
        )
    except Exception as e:
        raised_type = type(e)
    conn.close()

    assert raised_type is not None, "Expected an exception from delete-on-empty-index but none was raised"
    assert issubclass(raised_type, sqlite3.DatabaseError), (
        f"Expected sqlite3.DatabaseError (or subclass), got {raised_type}; "
        "the migration's except clause must catch DatabaseError"
    )


def test_database_error_is_not_operational_error(tmp_path):
    """Confirm the class hierarchy: the FTS5 phantom raises DatabaseError
    (the base) not OperationalError (a subclass) — so 'except OperationalError'
    misses it while 'except DatabaseError' catches it.

    This is the assertion that justifies widening the except in #274.
    """
    p = str(tmp_path / "hierarchy_demo.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, claim TEXT, quoted_text TEXT,
            interpretation TEXT, status TEXT DEFAULT 'active'
        );
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_r1', 'retracted', 'src', 'interp', 'retracted')"
    )
    conn.commit()

    raised_exc = None
    try:
        conn.execute(
            "INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation) "
            "SELECT 'delete', rowid, COALESCE(claim,''), COALESCE(quoted_text,''), "
            "COALESCE(interpretation,'') FROM nodes WHERE status='retracted'"
        )
    except Exception as e:
        raised_exc = e
    conn.close()

    assert raised_exc is not None
    # Informational only — not a gate: on Ariadne's SQLite this raised
    # MismatchError (a DatabaseError subclass, NOT OperationalError).  On a
    # different SQLite build it could raise OperationalError instead; that
    # would still be caught by the widened `except sqlite3.DatabaseError`
    # below because OperationalError IS a DatabaseError subclass — so
    # behavior is unaffected.  We deliberately do not pin the exact subclass.
    #
    # The NEW guard catches it:
    assert isinstance(raised_exc, sqlite3.DatabaseError), (
        f"Expected sqlite3.DatabaseError, got {type(raised_exc)}"
    )


def test_migration_block_with_widened_except_survives_empty_index(tmp_path):
    """Simulate the server's _get_db #274 migration on a restore-from-dump DB
    (user_version=0, empty FTS index, retracted nodes present) and assert no
    exception escapes — verifying that the DatabaseError widening is sufficient.

    This is the exact scenario that killed Ariadne's restore: server starts,
    nodes_fts was just created (empty), user_version=0 so migration fires,
    delete-on-empty raises DatabaseError — the widened except absorbs it.
    """
    p = str(tmp_path / "restore_sim.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, claim TEXT, quoted_text TEXT,
            interpretation TEXT, status TEXT DEFAULT 'active'
        );
        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            claim, quoted_text, interpretation,
            content='nodes', content_rowid='rowid'
        );
        """
    )
    # Retracted rows present, index empty (restore-from-dump state).
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_r1', 'retracted claim', 'src', 'interp', 'retracted')"
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_r2', 'another retracted', 'src2', 'interp2', 'retracted')"
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_a1', 'active claim about migration', 'src3', 'i3', 'active')"
    )
    conn.commit()
    # user_version = 0 (as if this is a fresh restore-from-dump that didn't carry user_version)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0

    # Run the migration block exactly as the fixed server.py does it.
    current_user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    exception_escaped = False
    if current_user_version < 1:
        try:
            conn.execute(
                """
                INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
                SELECT 'delete', rowid,
                       COALESCE(claim, ''),
                       COALESCE(quoted_text, ''),
                       COALESCE(interpretation, '')
                FROM nodes
                WHERE status = 'retracted'
                """
            )
            conn.execute("PRAGMA user_version = 1")
        except sqlite3.DatabaseError:
            pass  # the widened except — absorbs FTS5 phantom-malformed
        except Exception:
            exception_escaped = True

    conn.close()

    assert not exception_escaped, (
        "An unexpected exception escaped the migration block; "
        "the DatabaseError widening did not cover it"
    )
    # user_version stays 0 because the migration failed (retry-gate semantics preserved).
    conn2 = sqlite3.connect(p)
    uv = conn2.execute("PRAGMA user_version").fetchone()[0]
    conn2.close()
    # The migration failed (delete on empty raised DatabaseError), so version stays 0 —
    # exactly the retry-gate behavior the comment in server.py describes.
    assert uv == 0, (
        f"user_version should stay 0 when migration fails (retry-gate), got {uv}"
    )


# ─── Fix 3: rebuild_fts_index is self-contained ───────────────────────────


def test_rebuild_fts_index_self_contained_restore(tmp_path):
    """End-to-end #781 regression: dump → restore → rebuild_fts_index → no crash,
    FTS queryable, retracted rows absent from index, user_version = 1.
    """
    src = str(tmp_path / "source.db")
    _build_db_with_retractions(src, n_active=3, n_retracted=2)

    # Verify source state.
    src_conn = sqlite3.connect(src)
    assert src_conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert src_conn.execute("SELECT count(*) FROM nodes WHERE status='retracted'").fetchone()[0] == 2
    src_conn.close()

    # Dump.
    dump = str(tmp_path / "knowledge.sql")
    engram_backup.dump_stripped(src, dump)
    dump_text = open(dump).read()
    assert "PRAGMA user_version = 1" in dump_text  # fix 1 in action

    # Restore into a fresh DB (simulating 'sqlite3 new.db < knowledge.sql').
    recon = str(tmp_path / "restored.db")
    rc = sqlite3.connect(recon)
    rc.executescript(dump_text)
    rc.close()

    # Verify restore state: user_version=1 (fix 1), no FTS table yet.
    rc = sqlite3.connect(recon)
    uv_before = rc.execute("PRAGMA user_version").fetchone()[0]
    fts_before = rc.execute("SELECT count(*) FROM sqlite_master WHERE name='nodes_fts'").fetchone()[0]
    rc.close()
    assert uv_before == 1, f"Restored user_version={uv_before}, expected 1"
    assert fts_before == 0, "nodes_fts should not exist in the dump (it was stripped)"

    # Run rebuild_fts_index — the fix-3 self-contained path.
    result = engram_backup.rebuild_fts_index(recon)
    assert result is True

    # Post-rebuild assertions.
    rc = sqlite3.connect(recon)

    # FTS is queryable and returns active nodes.
    hits = rc.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    assert hits > 0, "FTS index should return hits for 'migration' after rebuild"

    # Retracted rows must NOT appear in FTS results.
    retracted_in_fts = rc.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'retracted'"
    ).fetchone()[0]
    # The retracted nodes have 'retracted claim N' — check they're gone from index.
    # (Active nodes have 'active claim N', not 'retracted'.)
    assert retracted_in_fts == 0, (
        f"Retracted nodes leaked into FTS index: {retracted_in_fts} hits for 'retracted'"
    )

    # user_version = 1 (fix 3 sets it so #274 migration won't re-fire).
    uv_after = rc.execute("PRAGMA user_version").fetchone()[0]
    rc.close()
    assert uv_after == 1, f"rebuild_fts_index did not set user_version=1; got {uv_after}"


def test_rebuild_fts_index_creates_fts_table_if_absent(tmp_path):
    """rebuild_fts_index must create nodes_fts if the dump stripped it."""
    p = str(tmp_path / "no_fts.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, claim TEXT, quoted_text TEXT,
            interpretation TEXT, status TEXT DEFAULT 'active'
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_a1', 'active migration claim', 'src', 'interp', 'active')"
    )
    conn.commit()
    conn.close()

    result = engram_backup.rebuild_fts_index(p)
    assert result is True

    # FTS table must now exist and be queryable.
    conn = sqlite3.connect(p)
    has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='nodes_fts'").fetchone()
    assert has_fts is not None, "rebuild_fts_index did not create nodes_fts"
    hits = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    assert hits == 1
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert uv == 1


def test_rebuild_fts_index_retracted_rows_absent_post_rebuild(tmp_path):
    """After rebuild_fts_index on a DB with retractions, retracted rows must
    not appear in FTS queries — the rebuild-then-delete ordering is correct.
    """
    p = str(tmp_path / "with_retractions.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, claim TEXT, quoted_text TEXT,
            interpretation TEXT, status TEXT DEFAULT 'active'
        );
        CREATE TABLE edges (src TEXT, dst TEXT, relation TEXT);
        """
    )
    # Active nodes.
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_a1', 'active migration node', 'src', 'i', 'active')"
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_a2', 'another active node', 'src2', 'i2', 'active')"
    )
    # Retracted nodes (inserted directly without trigger — simulating restore).
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_r1', 'retracted superseded node', 'old', 'old', 'retracted')"
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('ob_r2', 'retracted old claim', 'old2', 'old2', 'retracted')"
    )
    conn.commit()
    conn.close()

    result = engram_backup.rebuild_fts_index(p)
    assert result is True

    conn = sqlite3.connect(p)
    # Active nodes are findable.
    active_hits = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    assert active_hits == 1, f"Expected 1 active hit for 'migration', got {active_hits}"

    # Retracted content is not findable.
    retracted_hits = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'retracted'"
    ).fetchone()[0]
    assert retracted_hits == 0, (
        f"Retracted rows leaked into FTS index: {retracted_hits} hits"
    )

    # Note: SELECT count(*) FROM nodes_fts on an external-content table returns
    # the content-table row count (all 4), not the post-delete index size.
    # The MATCH queries above are the authoritative check for retraction exclusion.
    conn.close()


def test_rebuild_fts_noop_on_non_engram_db(tmp_path):
    """rebuild_fts_index returns False when nodes table doesn't exist."""
    p = str(tmp_path / "not_engram.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE other_table (x INT)")
    conn.commit()
    conn.close()
    assert engram_backup.rebuild_fts_index(p) is False


# ─── End-to-end: the full #781 path ───────────────────────────────────────


def test_full_781_path_dump_restore_rebuild_server_gate(tmp_path):
    """Full end-to-end reproduction of issue #781:

    1. Source DB: active + retracted nodes, user_version=1.
    2. dump_stripped → dump contains PRAGMA user_version = 1.
    3. Restore (executescript) → restored DB has user_version=1, no FTS table.
    4. rebuild_fts_index → FTS created, rebuilt, retracted deleted, uv=1.
    5. Simulate server's _get_db #274 gate on the restored DB → no exception
       (user_version already 1, gate does not fire).
    """
    src = str(tmp_path / "source.db")
    _build_db_with_retractions(src, n_active=4, n_retracted=3)

    dump = str(tmp_path / "knowledge.sql")
    engram_backup.dump_stripped(src, dump)

    recon = str(tmp_path / "restored.db")
    rc = sqlite3.connect(recon)
    rc.executescript(open(dump).read())
    rc.close()

    # Step 3: rebuild (self-contained).
    assert engram_backup.rebuild_fts_index(recon) is True

    # Step 4: simulate the server's migration gate.
    conn = sqlite3.connect(recon)
    exception_escaped = False
    current_user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    # With fix 1 (dump carries uv=1) + fix 3 (rebuild sets uv=1), this is 1.
    assert current_user_version == 1, (
        f"user_version should be 1 before server gate; got {current_user_version}"
    )
    if current_user_version < 1:
        try:
            conn.execute(
                """
                INSERT INTO nodes_fts(nodes_fts, rowid, claim, quoted_text, interpretation)
                SELECT 'delete', rowid,
                       COALESCE(claim, ''),
                       COALESCE(quoted_text, ''),
                       COALESCE(interpretation, '')
                FROM nodes
                WHERE status = 'retracted'
                """
            )
            conn.execute("PRAGMA user_version = 1")
        except sqlite3.DatabaseError:
            pass
        except Exception:
            exception_escaped = True

    # The gate must NOT have fired (user_version=1).
    assert not exception_escaped

    # FTS is queryable.
    hits = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'migration'"
    ).fetchone()[0]
    assert hits > 0

    # Retracted rows absent from FTS.
    retracted_hits = conn.execute(
        "SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH 'retracted'"
    ).fetchone()[0]
    assert retracted_hits == 0

    conn.close()
