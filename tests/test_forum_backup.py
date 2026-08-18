"""Tests for tools/forum_backup.py — vec0 extension-free drop fix (#1057).

Coverage:

1. vec-bearing DB strips and dumps under no extension (#1057 regression):
   - pytest.importorskip("sqlite_vec") gates fixture construction only.
   - _strip_derived_from_snapshot is called on a connection WITHOUT sqlite_vec
     loaded — the fix path is extension-free by construction.
   - Asserts: no raise; no vec_* in sqlite_master; iterdump() succeeds;
     tables_dropped counts vec0 virtual + shadow + fts.

2. vec-less DB is untouched / no writable_schema entry:
   - A DB with no vec0 tables strips cleanly (embeddings nulled, fts dropped).
   - writable_schema block is never entered (verified via sqlite_master query).
   - stats are correct.

3. End-to-end dump excludes vec data:
   - After _strip_derived_from_snapshot + iterdump, the SQL text contains no
     vec0, no CREATE VIRTUAL TABLE...vec0, and no vec_posts/vec_threads CREATE.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# append (not insert(0)) -- tools/forum_backup.py doesn't collide with
# anything in src/, but making tools/ a sys.path *root* here would
# outrank src/forum for the rest of the pytest run, shadowing the real
# `forum` package for every test collected afterward (#2037/#2063).
sys.path.append(str(REPO_ROOT / "tools"))

import forum_backup  # noqa: E402
from forum_backup import _strip_derived_from_snapshot  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _build_vec_bearing_db(path: str) -> None:
    """Build a minimal forum-shaped DB with vec0 virtual tables.

    Requires sqlite_vec loaded (callers must have called importorskip first).
    Creates: posts, threads (with embedding column), vec_posts, vec_threads
    vec0 virtual tables + their shadow tables, and posts_fts FTS5 table.
    """
    import sqlite_vec

    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    conn.executescript("""
        CREATE TABLE posts (
            id      INTEGER PRIMARY KEY,
            body_md TEXT,
            embedding BLOB
        );
        CREATE TABLE threads (
            id      INTEGER PRIMARY KEY,
            title   TEXT,
            embedding BLOB
        );
        CREATE VIRTUAL TABLE vec_posts   USING vec0(embedding float[4]);
        CREATE VIRTUAL TABLE vec_threads USING vec0(embedding float[4]);
        CREATE VIRTUAL TABLE posts_fts USING fts5(
            body_md,
            content='posts', content_rowid='id'
        );
        CREATE TRIGGER posts_fts_insert AFTER INSERT ON posts BEGIN
            INSERT INTO posts_fts(rowid, body_md) VALUES (new.id, new.body_md);
        END;
    """)
    # Insert a couple of rows with embeddings.
    conn.execute(
        "INSERT INTO posts(id, body_md, embedding) VALUES (1, 'hello world', ?)",
        (b'\x00' * 16,),  # 4 floats as raw bytes placeholder
    )
    conn.execute(
        "INSERT INTO threads(id, title, embedding) VALUES (1, 'first thread', ?)",
        (b'\x00' * 16,),
    )
    # Insert into vec tables (need the extension for this).
    conn.execute(
        "INSERT INTO vec_posts(rowid, embedding) VALUES (1, '[0.1, 0.2, 0.3, 0.4]')"
    )
    conn.execute(
        "INSERT INTO vec_threads(rowid, embedding) VALUES (1, '[0.1, 0.2, 0.3, 0.4]')"
    )
    conn.commit()
    conn.close()


def _build_vec_less_db(path: str) -> None:
    """Build a minimal forum-shaped DB WITHOUT any vec0 tables.

    Has posts/threads with embedding column + posts_fts FTS5 table.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE posts (
            id      INTEGER PRIMARY KEY,
            body_md TEXT,
            embedding BLOB
        );
        CREATE TABLE threads (
            id      INTEGER PRIMARY KEY,
            title   TEXT,
            embedding BLOB
        );
        CREATE VIRTUAL TABLE posts_fts USING fts5(
            body_md,
            content='posts', content_rowid='id'
        );
        CREATE TRIGGER posts_fts_insert AFTER INSERT ON posts BEGIN
            INSERT INTO posts_fts(rowid, body_md) VALUES (new.id, new.body_md);
        END;
    """)
    conn.execute(
        "INSERT INTO posts(id, body_md, embedding) VALUES (1, 'hello world', ?)",
        (b'\x00' * 16,),
    )
    conn.execute(
        "INSERT INTO threads(id, title, embedding) VALUES (1, 'first thread', ?)",
        (b'\x00' * 16,),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test 1: vec-bearing DB strips without the extension (#1057 regression)
# ---------------------------------------------------------------------------

class TestVecBearingStrip:
    """_strip_derived_from_snapshot handles vec0 tables WITHOUT the extension.

    pytest.importorskip("sqlite_vec") gates FIXTURE CONSTRUCTION only.
    The code under test (_strip_derived_from_snapshot) is called on a connection
    that does NOT have sqlite_vec loaded — the fix is extension-free by construction.
    """

    def test_strip_does_not_raise_without_extension(self, tmp_path):
        """Core #1057 regression: strip succeeds even without sqlite_vec on the connection."""
        sqlite_vec = pytest.importorskip(
            "sqlite_vec",
            reason="sqlite_vec required to BUILD the vec0 fixture (not to run the fix)",
        )

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        # Open a connection WITHOUT loading sqlite_vec — this is the system-python3 case.
        # The code path under test is extension-independent by construction (#1057 fix).
        conn = sqlite3.connect(db_path)
        try:
            # Must not raise despite the connection lacking the extension.
            stats = _strip_derived_from_snapshot(conn)
        finally:
            conn.close()

        # (a) did not raise — confirmed by reaching here.
        assert stats is not None

    def test_no_vec_tables_remain_in_sqlite_master(self, tmp_path):
        """After strip, no vec_* tables remain in sqlite_master."""
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        conn = sqlite3.connect(db_path)
        try:
            _strip_derived_from_snapshot(conn)
            remaining = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND (name LIKE 'vec_posts%' OR name LIKE 'vec_threads%')"
            ).fetchall()
        finally:
            conn.close()

        assert remaining == [], (
            f"vec_* tables still present after strip: {[r[0] for r in remaining]}"
        )

    def test_iterdump_succeeds_after_strip(self, tmp_path):
        """iterdump() must succeed after strip — this was the exact failure point in #1057."""
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        conn = sqlite3.connect(db_path)
        try:
            _strip_derived_from_snapshot(conn)
            # This is the exact operation that raised "no such module: vec0" before the fix.
            dump_count = sum(1 for _ in conn.iterdump())
        finally:
            conn.close()

        assert dump_count > 0, "iterdump() returned no statements — unexpected"

    def test_tables_dropped_counts_virtual_shadow_and_fts(self, tmp_path):
        """tables_dropped must count vec0 virtual tables + shadow tables + fts."""
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        # Count ALL derived tables before strip so we know the expected total.
        conn_count = sqlite3.connect(db_path)
        import sqlite_vec as sv
        conn_count.enable_load_extension(True)
        sv.load(conn_count)
        conn_count.enable_load_extension(False)
        before = conn_count.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'vec_posts%' OR name LIKE 'vec_threads%' "
            "     OR name LIKE 'posts_fts%')"
        ).fetchone()[0]
        conn_count.close()

        # Now strip WITHOUT the extension.
        conn = sqlite3.connect(db_path)
        try:
            stats = _strip_derived_from_snapshot(conn)
        finally:
            conn.close()

        assert stats["tables_dropped"] == before, (
            f"tables_dropped={stats['tables_dropped']} but expected {before} "
            f"(vec0 virtual + shadow + fts tables)"
        )

    def test_embeddings_are_nulled(self, tmp_path):
        """posts and threads embedding columns are NULLed during strip."""
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        conn = sqlite3.connect(db_path)
        try:
            _strip_derived_from_snapshot(conn)
            post_non_null = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            thread_non_null = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE embedding IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()

        assert post_non_null == 0, f"posts still has {post_non_null} non-NULL embeddings"
        assert thread_non_null == 0, f"threads still has {thread_non_null} non-NULL embeddings"


# ---------------------------------------------------------------------------
# Test 2: vec-less DB — no writable_schema entry, correct stats
# ---------------------------------------------------------------------------

class TestVecLessStrip:
    """A DB with no vec0 tables strips cleanly without touching writable_schema."""

    def test_strip_does_not_raise(self, tmp_path):
        """Strip on a vec-less DB completes without error."""
        db_path = str(tmp_path / "forum_plain.db")
        _build_vec_less_db(db_path)

        conn = sqlite3.connect(db_path)
        try:
            stats = _strip_derived_from_snapshot(conn)
        finally:
            conn.close()

        assert stats is not None

    def test_no_vec_tables_on_vecless_db(self, tmp_path):
        """A vec-less DB strips cleanly: no vec_* tables, writable_schema path skipped.

        (Asserts the observable consequence — no vec_* tables remain — rather than
        spying on the PRAGMA; the guard in step 3 means writable_schema is never
        entered when no vec0 virtual tables exist.)"""
        db_path = str(tmp_path / "forum_plain.db")
        _build_vec_less_db(db_path)

        conn = sqlite3.connect(db_path)
        try:
            _strip_derived_from_snapshot(conn)
            # After strip, no vec_* tables should exist.
            vec_tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND (name LIKE 'vec_%')"
            ).fetchall()
        finally:
            conn.close()

        assert vec_tables == [], (
            f"Unexpected vec_* tables on a vec-less DB: {vec_tables}"
        )

    def test_fts_dropped_and_posts_survive(self, tmp_path):
        """posts_fts is dropped; posts and threads base tables survive."""
        db_path = str(tmp_path / "forum_plain.db")
        _build_vec_less_db(db_path)

        conn = sqlite3.connect(db_path)
        try:
            stats = _strip_derived_from_snapshot(conn)
            fts_left = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'posts_fts%'"
            ).fetchall()
            posts_count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            threads_count = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        finally:
            conn.close()

        assert fts_left == [], f"posts_fts* tables still present: {fts_left}"
        assert posts_count == 1, f"expected 1 post, got {posts_count}"
        assert threads_count == 1, f"expected 1 thread, got {threads_count}"
        assert stats["embeddings_nulled"] == 2  # 1 post + 1 thread
        assert stats["tables_dropped"] > 0  # at least posts_fts + shadow tables

    def test_no_vec_tables_dropped(self, tmp_path):
        """tables_dropped on vec-less DB counts only fts tables, not vec."""
        db_path = str(tmp_path / "forum_plain.db")
        _build_vec_less_db(db_path)

        # Count only fts-prefixed tables before strip.
        conn_pre = sqlite3.connect(db_path)
        fts_count_before = conn_pre.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'posts_fts%'"
        ).fetchone()[0]
        conn_pre.close()

        conn = sqlite3.connect(db_path)
        try:
            stats = _strip_derived_from_snapshot(conn)
        finally:
            conn.close()

        # No vec tables exist, so tables_dropped == fts tables dropped.
        assert stats["tables_dropped"] == fts_count_before, (
            f"tables_dropped={stats['tables_dropped']} != fts_count_before={fts_count_before}"
        )


# ---------------------------------------------------------------------------
# Test 3: end-to-end dump excludes vec data
# ---------------------------------------------------------------------------

class TestEndToEndDumpExcludesVec:
    """After strip + iterdump the SQL text must contain no vec0 artifacts."""

    def test_dump_contains_no_vec0(self, tmp_path):
        """The SQL dump from a vec-bearing DB must contain no vec0 keywords."""
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        # Strip on a connection WITHOUT the extension (the fix path).
        conn = sqlite3.connect(db_path)
        try:
            _strip_derived_from_snapshot(conn)
            sql_lines = list(conn.iterdump())
        finally:
            conn.close()

        sql_text = "\n".join(sql_lines)

        # No vec0 module reference should survive.
        assert "vec0" not in sql_text, (
            "Found 'vec0' in dump SQL — vec0 virtual table was not removed"
        )

    def test_dump_contains_no_create_virtual_vec(self, tmp_path):
        """No CREATE VIRTUAL TABLE ... vec0 statement in the dump."""
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        conn = sqlite3.connect(db_path)
        try:
            _strip_derived_from_snapshot(conn)
            sql_lines = list(conn.iterdump())
        finally:
            conn.close()

        sql_text = "\n".join(sql_lines)

        create_virtual_lines = [
            ln for ln in sql_lines
            if "CREATE VIRTUAL TABLE" in ln.upper()
            and ("vec_posts" in ln.lower() or "vec_threads" in ln.lower())
        ]
        assert create_virtual_lines == [], (
            f"CREATE VIRTUAL TABLE vec_* found in dump:\n"
            + "\n".join(create_virtual_lines)
        )

    def test_dump_contains_no_vec_posts_or_vec_threads(self, tmp_path):
        """No vec_posts or vec_threads CREATE statements in the dump."""
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        conn = sqlite3.connect(db_path)
        try:
            _strip_derived_from_snapshot(conn)
            sql_lines = list(conn.iterdump())
        finally:
            conn.close()

        vec_create_lines = [
            ln for ln in sql_lines
            if "CREATE" in ln.upper()
            and ("vec_posts" in ln.lower() or "vec_threads" in ln.lower())
        ]
        assert vec_create_lines == [], (
            f"vec_posts/vec_threads CREATE statements found in dump:\n"
            + "\n".join(vec_create_lines)
        )

    def test_dump_via_dump_to_sql_excludes_vec(self, tmp_path):
        """_dump_to_sql end-to-end: the output SQL file has no vec artifacts."""
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        snap_path = str(tmp_path / "snap.db")
        # Snapshot the fixture DB (simulates what the backup tool does).
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # _dump_to_sql strips and dumps.
        sql_text = forum_backup._dump_to_sql(snap_path, keep_derived=False)

        assert "vec0" not in sql_text, "vec0 found in _dump_to_sql output"
        assert "vec_posts" not in sql_text, "vec_posts found in _dump_to_sql output"
        assert "vec_threads" not in sql_text, "vec_threads found in _dump_to_sql output"
        # Base posts table must survive.
        assert "CREATE TABLE" in sql_text and "posts" in sql_text.lower(), (
            "posts table missing from dump"
        )

    def test_restore_into_fresh_succeeds(self, tmp_path):
        """The #815 restore axis: post-strip dump must REPLAY into a fresh empty DB.

        This is the gap an iterdump-only test cannot see (iterdump succeeding on the
        stripped snapshot != the dump restoring into a fresh DB). In the real forum
        schema, posts/threads are plain INTEGER PRIMARY KEY (NO AUTOINCREMENT), so
        sqlite_sequence exists ONLY because the vec0 shadow tables use AUTOINCREMENT.
        After the strip removes those shadow tables, sqlite_sequence is present-but-
        empty, and the AUTOINCREMENT-gated filter in _dump_to_sql removes the bare
        `DELETE FROM "sqlite_sequence"` -- so a fresh DB (which has no sqlite_sequence
        table) does not die on it. The restore works because the statement is
        FILTERED OUT, not because anything recreates sqlite_sequence (there is no
        base AUTOINCREMENT table to do so). Pins the exact #815-class restore-DOA.
        (PR #1089, Borges colleague review + repro.)
        """
        pytest.importorskip("sqlite_vec", reason="required to BUILD fixture only")

        db_path = str(tmp_path / "forum_vec.db")
        _build_vec_bearing_db(db_path)

        snap_path = str(tmp_path / "snap.db")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        sql_text = forum_backup._dump_to_sql(snap_path, keep_derived=False)

        # (1) The bare sqlite_sequence DELETE must be FILTERED OUT (the #815 fix) --
        #     not present-and-survived-by-recreation. No base AUTOINCREMENT table
        #     exists here, so a leaked bare DELETE would DOA the fresh restore.
        assert 'DELETE FROM "sqlite_sequence"' not in sql_text, (
            "bare sqlite_sequence DELETE leaked into dump -> fresh-restore DOA (#815)"
        )
        assert "DELETE FROM sqlite_sequence" not in sql_text

        # (2) Replay into a FRESH empty DB must succeed (the missing axis).
        restored = str(tmp_path / "restored.db")
        rc = sqlite3.connect(restored)
        try:
            rc.executescript(sql_text)
            n = rc.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        finally:
            rc.close()
        assert n == 1, "posts row did not survive strip->dump->restore-into-fresh"

    def test_vecless_dump_to_sql_restores(self, tmp_path):
        """Vec-less end-to-end: _dump_to_sql output restores into a fresh DB.

        No sqlite_vec required -- a forum DB that never had vec0 tables must still
        strip + dump + restore cleanly. Runs in every env (no importorskip), so it
        guards the restore path even on hosts without the extension.
        (Reviewer-fairy suggestion, #1089.)
        """
        db_path = str(tmp_path / "forum_plain.db")
        _build_vec_less_db(db_path)

        snap_path = str(tmp_path / "snap_plain.db")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        sql_text = forum_backup._dump_to_sql(snap_path, keep_derived=False)
        assert "vec0" not in sql_text

        # Replay into a FRESH empty DB must succeed.
        restored = str(tmp_path / "restored_plain.db")
        rc = sqlite3.connect(restored)
        try:
            rc.executescript(sql_text)
            # posts table must exist + be queryable after restore.
            rc.execute("SELECT COUNT(*) FROM posts").fetchone()
        finally:
            rc.close()


class TestAutoincrementProbe:
    """#834: the ground-truth AUTOINCREMENT probe must match the EXACT
    'PRIMARY KEY AUTOINCREMENT' token, not a bare 'AUTOINCREMENT' substring.

    No sqlite_vec needed: sqlite_sequence is materialized via a create+drop of a
    real AUTOINCREMENT table (it persists, empty, after the table is dropped),
    leaving sqlite_sequence present with NO surviving AUTOINCREMENT base table —
    exactly the condition where a false-positive probe would keep the bare
    DELETE FROM sqlite_sequence and DOA a fresh-DB restore.
    """

    @staticmethod
    def _materialize_empty_sqlite_sequence(conn):
        # Create + populate + drop a real AUTOINCREMENT table: sqlite_sequence
        # is created and survives the drop (empty), with no AUTOINCREMENT table left.
        conn.executescript(
            "CREATE TABLE _seq_tmp (id INTEGER PRIMARY KEY AUTOINCREMENT);"
            "INSERT INTO _seq_tmp DEFAULT VALUES;"
            "DROP TABLE _seq_tmp;"
        )

    def test_bait_column_does_not_false_positive(self, tmp_path):
        """A column NAMED with the keyword (not PRIMARY KEY AUTOINCREMENT) must
        NOT trip the probe -> sqlite_sequence filtered -> fresh restore succeeds."""
        db_path = str(tmp_path / "forum_bait.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            "CREATE TABLE posts (id INTEGER PRIMARY KEY, body_md TEXT);"
            # bait: keyword appears as a column name, NOT as PRIMARY KEY AUTOINCREMENT
            "CREATE TABLE bait (notautoincrement_marker INTEGER, note TEXT);"
        )
        self._materialize_empty_sqlite_sequence(conn)
        conn.execute("INSERT INTO posts(id, body_md) VALUES (1, 'hi')")
        conn.commit()
        conn.close()

        # sanity: sqlite_sequence exists, but no real AUTOINCREMENT base table.
        c = sqlite3.connect(db_path)
        assert c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
        ).fetchone(), "test setup: sqlite_sequence should be present"
        c.close()

        snap_path = str(tmp_path / "snap.db")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        sql_text = forum_backup._dump_to_sql(snap_path, keep_derived=False)

        # The bait must NOT have been read as a real AUTOINCREMENT table, so the
        # sqlite_sequence statement must be filtered out.
        assert "sqlite_sequence" not in sql_text, (
            "bait column false-positived the AUTOINCREMENT probe -> "
            "sqlite_sequence kept -> fresh restore would DOA (#834)"
        )

        # Restore into a FRESH empty DB must succeed (the #834 failure axis).
        fresh = sqlite3.connect(":memory:")
        try:
            fresh.executescript(sql_text)
            assert fresh.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        finally:
            fresh.close()

    def test_real_autoincrement_still_fires(self, tmp_path):
        """Positive control (ls_0014 mutation guard): a REAL PRIMARY KEY
        AUTOINCREMENT base table makes the probe True, so sqlite_sequence
        statements are KEPT (they're load-bearing) and restore still works."""
        db_path = str(tmp_path / "forum_real_ai.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, body_md TEXT)"
        )
        conn.execute("INSERT INTO posts(body_md) VALUES ('hi')")
        conn.commit()
        conn.close()

        snap_path = str(tmp_path / "snap.db")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        sql_text = forum_backup._dump_to_sql(snap_path, keep_derived=False)

        # Real AUTOINCREMENT -> sequence rows are meaningful -> NOT filtered.
        assert "sqlite_sequence" in sql_text, (
            "real PRIMARY KEY AUTOINCREMENT table should keep sqlite_sequence"
        )
        # And it still restores into a fresh DB (the CREATE recreates the table).
        fresh = sqlite3.connect(":memory:")
        try:
            fresh.executescript(sql_text)
            assert fresh.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
        finally:
            fresh.close()

    def test_whitespace_normalized_in_probe(self, tmp_path):
        """A real AUTOINCREMENT table with non-standard internal whitespace
        (PRIMARY  KEY  AUTOINCREMENT) must still be detected — sqlite_master
        stores CREATE sql verbatim, so the probe normalizes whitespace runs."""
        db_path = str(tmp_path / "forum_ws.db")
        conn = sqlite3.connect(db_path)
        # Extra spaces between the keywords; SQLite stores this verbatim.
        conn.execute(
            "CREATE TABLE posts (id INTEGER PRIMARY  KEY  AUTOINCREMENT, body_md TEXT)"
        )
        conn.execute("INSERT INTO posts(body_md) VALUES ('hi')")
        conn.commit()
        conn.close()

        snap_path = str(tmp_path / "snap.db")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        sql_text = forum_backup._dump_to_sql(snap_path, keep_derived=False)
        # Whitespace-normalized probe must still see it as real AUTOINCREMENT.
        assert "sqlite_sequence" in sql_text, (
            "whitespace-variant PRIMARY KEY AUTOINCREMENT was missed by the probe"
        )
