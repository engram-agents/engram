"""Tests for tools/backup_knowledge_db.py — dated knowledge.db backup CLI.

Covers:
  1. Happy path: real SQLite db → dated .sql archive produced.
  2. Skip on existing: tool exits 0 and does not overwrite today's archive.
  3. Creates out-dir: tool creates the output directory when absent.
  4. Retain pruning: archives older than the threshold are deleted; recent kept.
  5. Retain zero keeps all: --retain 0 never deletes.
  6. Dry-run: no file created, no file deleted.
  7. DB missing: tool exits 1 when --db points to a non-existent file.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest

# conftest.py inserts src/engram onto sys.path; backup_knowledge_db lives in
# src/engram/tools/ and does its own sys.path.insert to reach engram_backup.
TOOLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "engram", "tools",
)
# append (not insert(0)) -- src/engram/tools/ also holds forum.py (the CLI
# script tools/forum.py symlinks to); making it a sys.path *root* at
# position 0 would outrank src/forum for the rest of the pytest run,
# shadowing the real `forum` package for every test collected afterward
# (#2037/#2063).
if TOOLS_DIR not in sys.path:
    sys.path.append(TOOLS_DIR)

import backup_knowledge_db  # noqa: E402


def _build_minimal_db(path: str) -> None:
    """Create a small but real SQLite db at *path* for use with dump_stripped.

    dump_stripped runs UPDATE nodes SET embedding=NULL on its snapshot, so the
    db must have a nodes table with an embedding column.  A minimal schema is
    sufficient — no FTS, no vec_nodes.
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, embedding TEXT)")
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'hello', NULL)")
    conn.commit()
    conn.close()


def _today() -> str:
    return datetime.date.today().isoformat()


class TestHappyPath(unittest.TestCase):
    def test_produces_dated_sql_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 0)
            expected = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            self.assertTrue(os.path.exists(expected), f"expected archive at {expected}")
            # Archive should be non-empty.
            self.assertGreater(os.path.getsize(expected), 0)


class TestSkipOnExisting(unittest.TestCase):
    def test_skips_when_archive_already_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            # Pre-create today's archive with sentinel content.
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            sentinel = b"-- sentinel content\n"
            with open(archive, "wb") as f:
                f.write(sentinel)

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 0)
            # File must not be overwritten.
            with open(archive, "rb") as f:
                self.assertEqual(f.read(), sentinel)


class TestCreatesOutDir(unittest.TestCase):
    def test_creates_missing_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "nested", "db-backup")
            _build_minimal_db(db)

            self.assertFalse(os.path.exists(out_dir))
            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isdir(out_dir))
            expected = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            self.assertTrue(os.path.exists(expected))


class TestRetainPruning(unittest.TestCase):
    def test_prunes_old_keeps_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            today = datetime.date.today()
            retain_days = 7

            # Create an archive that is older than the threshold (should be pruned).
            old_date = (today - datetime.timedelta(days=retain_days + 1)).isoformat()
            old_archive = os.path.join(out_dir, f"knowledge-{old_date}.sql")
            with open(old_archive, "w") as f:
                f.write("-- old archive\n")

            # Create an archive that is within the threshold (should be kept).
            recent_date = (today - datetime.timedelta(days=retain_days - 1)).isoformat()
            recent_archive = os.path.join(out_dir, f"knowledge-{recent_date}.sql")
            with open(recent_archive, "w") as f:
                f.write("-- recent archive\n")

            rc = backup_knowledge_db.main([
                "--db", db,
                "--out-dir", out_dir,
                "--retain", str(retain_days),
            ])

            self.assertEqual(rc, 0)
            # Old archive pruned.
            self.assertFalse(os.path.exists(old_archive), "old archive should have been pruned")
            # Recent archive kept.
            self.assertTrue(os.path.exists(recent_archive), "recent archive should be kept")
            # Today's new archive exists.
            today_archive = os.path.join(out_dir, f"knowledge-{today.isoformat()}.sql")
            self.assertTrue(os.path.exists(today_archive), "today's archive should exist")


class TestRetainZeroKeepsAll(unittest.TestCase):
    def test_retain_zero_never_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            today = datetime.date.today()

            # Create a very old archive.
            old_date = (today - datetime.timedelta(days=3650)).isoformat()
            old_archive = os.path.join(out_dir, f"knowledge-{old_date}.sql")
            with open(old_archive, "w") as f:
                f.write("-- very old archive\n")

            # --retain 0 is the default; pass explicitly to be clear.
            rc = backup_knowledge_db.main([
                "--db", db,
                "--out-dir", out_dir,
                "--retain", "0",
            ])

            self.assertEqual(rc, 0)
            # Old archive must survive.
            self.assertTrue(os.path.exists(old_archive), "retain=0 must not delete any archive")


class TestDryRun(unittest.TestCase):
    def test_dry_run_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir, "--dry-run"])

            self.assertEqual(rc, 0)
            # Archive must NOT be created.
            expected = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            self.assertFalse(os.path.exists(expected), "dry-run must not create archive")

    def test_dry_run_does_not_delete_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            today = datetime.date.today()
            old_date = (today - datetime.timedelta(days=100)).isoformat()
            old_archive = os.path.join(out_dir, f"knowledge-{old_date}.sql")
            with open(old_archive, "w") as f:
                f.write("-- old archive\n")

            rc = backup_knowledge_db.main([
                "--db", db,
                "--out-dir", out_dir,
                "--retain", "7",
                "--dry-run",
            ])

            self.assertEqual(rc, 0)
            # Old archive must survive (dry-run only prints, never deletes).
            self.assertTrue(os.path.exists(old_archive), "dry-run must not delete any archive")


class TestSkipOnExistingWithRetain(unittest.TestCase):
    def test_skip_still_prunes_old_archives(self):
        """When today's archive already exists, retention pruning still fires."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            today = datetime.date.today()
            retain_days = 7

            # Pre-create today's archive (triggers the skip path).
            today_archive = os.path.join(out_dir, f"knowledge-{today.isoformat()}.sql")
            with open(today_archive, "w") as f:
                f.write("-- today sentinel\n")

            # Create an old archive that should be pruned.
            old_date = (today - datetime.timedelta(days=retain_days + 1)).isoformat()
            old_archive = os.path.join(out_dir, f"knowledge-{old_date}.sql")
            with open(old_archive, "w") as f:
                f.write("-- old archive\n")

            rc = backup_knowledge_db.main([
                "--db", db,
                "--out-dir", out_dir,
                "--retain", str(retain_days),
            ])

            self.assertEqual(rc, 0)
            # Old archive must be pruned even though today's backup was skipped.
            self.assertFalse(os.path.exists(old_archive), "old archive should be pruned even on skip")
            # Today's sentinel must be unchanged.
            with open(today_archive) as f:
                self.assertEqual(f.read(), "-- today sentinel\n")


class TestDbMissing(unittest.TestCase):
    def test_exits_1_when_db_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "nonexistent.db")
            out_dir = os.path.join(tmp, "db-backup")

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
