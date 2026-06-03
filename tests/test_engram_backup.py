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


def test_rebuild_fts_noop_without_table(tmp_path):
    """rebuild_fts_index returns False (not crash) when there's no FTS table."""
    p = str(tmp_path / "plain.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE nodes(id TEXT, claim TEXT)")
    conn.commit()
    conn.close()
    assert engram_backup.rebuild_fts_index(p) is False
