"""Tests for tools/backup_knowledge_db.py — content-aware knowledge.db backup CLI.

Covers:
  1. Happy path: real SQLite db → dated .sql archive produced.
  2. Sentinel-without-watermark: a pre-existing same-day file with no matching
     watermark is untouched, and a new suffixed archive is written alongside it
     (#2151 — no longer a bare filename-existence skip).
  3. Creates out-dir: tool creates the output directory when absent.
  4. Retain pruning: archives older than the threshold are deleted; recent kept.
  5. Retain zero keeps all: --retain 0 never deletes.
  6. Dry-run: no file created, no file deleted.
  7. Skip-with-retain: retention pruning still fires when a real backup
     happens (via the suffix path) on top of an existing same-day file.
  8. DB missing: tool exits 1 when --db points to a non-existent file.
  9. Watermark match: a second run with no DB changes is a genuine
     content-aware skip (no new file, content-aware message).
  10. Watermark mismatch same-day: a DB change between two same-day runs
      produces a suffixed archive, original untouched, sidecar updated.
  11. Three-in-a-day: a third same-day backup lands at -3, not colliding
      with -2.
  12. --force: bypasses the watermark even when nothing changed.
  13. Retention pruning handles suffixed filenames.
  14. Missing/corrupt .watermark.json is treated as "no prior watermark" —
      always backs up, never crashes, never silently skips.
  15. Watermark match but the archive it names is missing from disk: NOT
      trusted as a skip — a real backup is written instead (#2235 review
      blocker: reproduced the exact #2151 failure shape through a new
      mechanism).
  16. Edge-only content change (a new edge, no new node) is caught by the
      widened watermark — no longer a false "up to date" skip (#2235 review
      round 1 suggestion).
  17. edit_history-only content change (a supersede/retract with no new
      node or edge row — the pure-in-place-UPDATE case) is caught by the
      round-2-widened watermark (#2235 review round 2 suggestion).
  18. Old-format persisted watermarks (nodes-only, or nodes+edges but no
      history fields) are treated as absent rather than trusted as a match.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
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


def _build_minimal_db(path: str, created_at: str = "2026-01-01T00:00:00Z") -> None:
    """Create a small but real SQLite db at *path* for use with dump_stripped.

    dump_stripped runs UPDATE nodes SET embedding=NULL on its snapshot, so the
    db must have a nodes table with an embedding column.  A minimal schema is
    sufficient — no FTS, no vec_nodes.  Includes created_at because the
    content-watermark check (#2151) queries MAX(created_at) against a live
    read-only connection. Also has (initially empty) edges and edit_history
    tables — the watermark covers both too (#2235 review, rounds 1 and 2),
    so _read_live_watermark's queries against them need the tables to exist
    even with zero rows.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, embedding TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id TEXT, target_id TEXT, "
        "relation TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE edit_history (id INTEGER PRIMARY KEY, timestamp TEXT, turn INTEGER, "
        "action TEXT, node_id TEXT, node_type TEXT, details TEXT)"
    )
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'hello', NULL, ?)", (created_at,))
    conn.commit()
    conn.close()


def _build_nodes_only_db(path: str, created_at: str = "2026-01-01T00:00:00Z") -> None:
    """Like _build_minimal_db, but WITHOUT the edges/edit_history tables --
    simulates a DB never opened by a current-version ENGRAM server, or a
    non-ENGRAM SQLite file (#2244)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, claim TEXT, embedding TEXT, created_at TEXT)"
    )
    conn.execute("INSERT INTO nodes VALUES ('ob_1', 'hello', NULL, ?)", (created_at,))
    conn.commit()
    conn.close()


def _insert_row(db_path: str, node_id: str, created_at: str) -> None:
    """Insert an additional node row into an existing db.

    Used to simulate a watermark-changing write to the source DB between two
    backup runs on the same local day.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO nodes VALUES (?, ?, NULL, ?)",
        (node_id, f"claim for {node_id}", created_at),
    )
    conn.commit()
    conn.close()


def _insert_edge(db_path: str, edge_id: int, created_at: str) -> None:
    """Insert an edge row with NO accompanying node change — used to prove
    the watermark catches an edges-only content change (#2235 review)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO edges VALUES (?, 'ob_1', 'ob_1', 'self_test', ?)",
        (edge_id, created_at),
    )
    conn.commit()
    conn.close()


def _insert_edit_history(db_path: str, row_id: int, timestamp: str) -> None:
    """Insert an edit_history row with NO accompanying node/edge change --
    simulates the pure-in-place-UPDATE case (e.g. a supersede/retract that
    writes no new node or edge row) that motivated widening the watermark
    to cover edit_history too (#2235 review, round 2)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO edit_history VALUES (?, ?, 0, 'retracted', 'ob_1', 'observation', '{}')",
        (row_id, timestamp),
    )
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
    def test_sentinel_without_watermark_writes_suffixed_archive(self):
        """A pre-existing same-day file with no matching watermark (e.g. a
        sentinel with no real backup behind it, or no .watermark.json sidecar
        at all) is NOT trusted as "already backed up" (#2151) -- the tool
        writes a fresh, suffixed archive alongside it rather than overwriting
        it or silently skipping."""
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
            # Original sentinel file must not be overwritten (property
            # preserved from the old skip-on-filename behavior).
            with open(archive, "rb") as f:
                self.assertEqual(f.read(), sentinel)
            # A NEW suffixed archive is created alongside it (property added
            # — this is the whole fix: no silent no-op).
            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(
                os.path.exists(suffixed), f"expected suffixed archive at {suffixed}"
            )
            self.assertGreater(os.path.getsize(suffixed), 0)


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
        """Explicit --retain 0 keeps its pre-#4208 meaning (keep all); only the ABSENT flag
        selects the default policy. 20 distinct dated dailies + a very old archive: the default
        policy WOULD prune 2026-01-02 (not newest-14, not a month anchor), so this goes red if
        an explicit 0 ever means "default" again."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            today = datetime.date.today()
            old_date = (today - datetime.timedelta(days=3650)).isoformat()
            old_archive = os.path.join(out_dir, f"knowledge-{old_date}.sql")
            with open(old_archive, "w") as f:
                f.write("-- very old archive\n")
            dailies = _daily_names("2026-01-01", 20)
            for n in dailies:
                with open(os.path.join(out_dir, n), "w") as f:
                    f.write("-- archive\n")

            rc = backup_knowledge_db.main([
                "--db", db,
                "--out-dir", out_dir,
                "--retain", "0",
            ])

            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(old_archive), "explicit --retain 0 must not delete any archive")
            for n in dailies:
                self.assertTrue(
                    os.path.exists(os.path.join(out_dir, n)),
                    f"explicit --retain 0 means keep all, but {n} was pruned (default policy applied?)",
                )


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
        """Retention pruning still fires on a run that performs a real
        backup rather than skipping — a same-day sentinel file with no
        matching watermark no longer triggers a skip (#2151), so this test
        exercises pruning-alongside-a-real-write, not pruning-alongside-a-
        skip as its original docstring/comment described pre-#2151-fix."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            today = datetime.date.today()
            retain_days = 7

            # Pre-create today's archive as a sentinel with no watermark
            # behind it -- content-aware, so this no longer skips (#2151);
            # the run below writes a real suffixed archive instead.
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
            # Old archive must be pruned regardless of what happened on today's side.
            self.assertFalse(os.path.exists(old_archive), "old archive should be pruned on a real-backup run too")
            # Today's sentinel must be unchanged (never overwritten).
            with open(today_archive) as f:
                self.assertEqual(f.read(), "-- today sentinel\n")
            # And a real, suffixed archive was written alongside it (#2151 —
            # the sentinel-with-no-watermark case is a write, not a skip).
            suffixed = os.path.join(out_dir, f"knowledge-{today.isoformat()}-2.sql")
            self.assertTrue(os.path.exists(suffixed), "expected a real suffixed archive, not a skip")


class TestDbMissing(unittest.TestCase):
    def test_exits_1_when_db_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "nonexistent.db")
            out_dir = os.path.join(tmp, "db-backup")

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 1)


class TestWatermarkMatchSkip(unittest.TestCase):
    def test_second_run_no_changes_is_genuine_content_aware_skip(self):
        """Run the tool twice with no DB changes in between. The second run
        must skip (no new file, today's single dated archive unchanged) with
        the new content-aware skip message, not the old bare 'already exists'
        one."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            self.assertTrue(os.path.exists(archive))
            with open(archive, "rb") as f:
                first_content = f.read()

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            # No new (suffixed) archive created -- genuine skip.
            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertFalse(os.path.exists(suffixed))
            # Today's single dated archive is unchanged.
            with open(archive, "rb") as f:
                self.assertEqual(f.read(), first_content)
            # The skip message is content-aware, not the old bare form.
            output = buf.getvalue()
            self.assertIn("skipping", output)
            self.assertIn("up to date", output)
            self.assertNotIn("already exists", output)


class TestWatermarkMismatchSuffixedWrite(unittest.TestCase):
    def test_same_day_content_change_writes_suffixed_archive_not_overwrite(self):
        """A DB change between two same-day runs must produce a NEW suffixed
        archive containing the change, leaving the original archive
        byte-unchanged, and update .watermark.json to the second backup."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            with open(archive, "rb") as f:
                first_content = f.read()

            _insert_row(db, "ob_2", "2026-01-02T00:00:00Z")

            rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            # Original archive byte-unchanged.
            with open(archive, "rb") as f:
                self.assertEqual(f.read(), first_content)

            # New suffixed archive exists and reflects the new row.
            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(os.path.exists(suffixed))
            with open(suffixed, "r", encoding="utf-8") as f:
                suffixed_content = f.read()
            self.assertIn("ob_2", suffixed_content)

            # Watermark sidecar now reflects the second backup.
            with open(os.path.join(out_dir, ".watermark.json")) as f:
                watermark = json.load(f)
            self.assertEqual(watermark["row_count"], 2)
            self.assertEqual(watermark["archive"], f"knowledge-{_today()}-2.sql")


class TestThreeInADaySuffixIncrement(unittest.TestCase):
    def test_third_same_day_backup_lands_at_dash_3(self):
        """A third real backup on the same day (after another DB change)
        lands at -3, not colliding with -2."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            self.assertEqual(
                backup_knowledge_db.main(["--db", db, "--out-dir", out_dir]), 0
            )
            _insert_row(db, "ob_2", "2026-01-02T00:00:00Z")
            self.assertEqual(
                backup_knowledge_db.main(["--db", db, "--out-dir", out_dir]), 0
            )
            _insert_row(db, "ob_3", "2026-01-03T00:00:00Z")
            self.assertEqual(
                backup_knowledge_db.main(["--db", db, "--out-dir", out_dir]), 0
            )

            base = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            second = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            third = os.path.join(out_dir, f"knowledge-{_today()}-3.sql")
            self.assertTrue(os.path.exists(base))
            self.assertTrue(os.path.exists(second))
            self.assertTrue(os.path.exists(third))


class TestForceFlag(unittest.TestCase):
    def test_force_bypasses_watermark_even_when_unchanged(self):
        """Even with a matching watermark (nothing changed), --force still
        performs a real backup, suffixed since today's file already exists."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)

            rc2 = backup_knowledge_db.main(
                ["--db", db, "--out-dir", out_dir, "--force"]
            )
            self.assertEqual(rc2, 0)

            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(
                os.path.exists(suffixed), "expected --force to write a suffixed archive"
            )
            self.assertGreater(os.path.getsize(suffixed), 0)


class TestRetentionPruningSuffixed(unittest.TestCase):
    def test_prunes_old_suffixed_keeps_recent_suffixed(self):
        """An old suffixed archive past the retention threshold is pruned
        exactly like its unsuffixed sibling would be; a suffixed archive
        within the threshold is kept."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            today = datetime.date.today()
            retain_days = 7

            old_date = (today - datetime.timedelta(days=retain_days + 1)).isoformat()
            old_suffixed = os.path.join(out_dir, f"knowledge-{old_date}-2.sql")
            with open(old_suffixed, "w") as f:
                f.write("-- old suffixed archive\n")

            recent_date = (today - datetime.timedelta(days=retain_days - 1)).isoformat()
            recent_suffixed = os.path.join(out_dir, f"knowledge-{recent_date}-2.sql")
            with open(recent_suffixed, "w") as f:
                f.write("-- recent suffixed archive\n")

            rc = backup_knowledge_db.main(
                ["--db", db, "--out-dir", out_dir, "--retain", str(retain_days)]
            )

            self.assertEqual(rc, 0)
            self.assertFalse(
                os.path.exists(old_suffixed), "old suffixed archive should have been pruned"
            )
            self.assertTrue(
                os.path.exists(recent_suffixed), "recent suffixed archive should be kept"
            )


class TestCorruptOrMissingWatermark(unittest.TestCase):
    def test_malformed_watermark_json_treated_as_no_prior_watermark(self):
        """Malformed .watermark.json must not crash the tool and must not be
        trusted as a match -- the tool still produces a correct backup."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            with open(os.path.join(out_dir, ".watermark.json"), "w") as f:
                f.write("{not valid json::")

            # Pre-existing dated archive too, to exercise the suffix path.
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            with open(archive, "w") as f:
                f.write("-- stale archive, no valid watermark\n")

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 0)
            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(
                os.path.exists(suffixed), "corrupt watermark must not block a real backup"
            )
            self.assertGreater(os.path.getsize(suffixed), 0)

    def test_missing_sidecar_with_existing_archive_still_backs_up(self):
        """A dated archive exists but the sidecar is entirely absent (e.g. an
        archive from before this fix shipped) -- must not be trusted as a
        match; a real backup is written."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            with open(archive, "w") as f:
                f.write("-- archive with no sidecar at all\n")

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 0)
            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(os.path.exists(suffixed))


class TestSkipRequiresArchiveOnDisk(unittest.TestCase):
    def test_matching_watermark_but_missing_archive_writes_a_real_backup(self):
        """A watermark match alone is not sufficient to skip — if the archive
        the sidecar names was deleted, the tool must not report 'skipping'
        with nothing on disk to back it. #2235 review blocker: reproduced
        exactly this (back up, rm the .sql, keep .watermark.json, re-run ->
        'backup up to date... skipping' rc=0 with ZERO archives on disk),
        the same failure shape #2151 fixed, recurring through a different
        mechanism."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            self.assertTrue(os.path.exists(archive))

            # Delete the archive but keep the watermark sidecar -- the exact
            # reproduction from the review.
            os.unlink(archive)
            self.assertTrue(os.path.exists(os.path.join(out_dir, ".watermark.json")))

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            # A real backup must have been written -- NOT a silent skip.
            self.assertTrue(
                os.path.exists(archive),
                "archive must be re-written when the watermark-matched archive was missing",
            )
            output = buf.getvalue()
            self.assertNotIn("skipping", output)

    def test_missing_archive_control_deleting_sidecar_too_also_backs_up(self):
        """Control for the above: deleting the sidecar as well (not just the
        archive) also results in a real backup — confirms the sidecar's
        presence, not something else, is what drove the (fixed) false skip."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            os.unlink(archive)
            os.unlink(os.path.join(out_dir, ".watermark.json"))

            rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)
            self.assertTrue(os.path.exists(archive))


class TestEdgeOnlyChangeCaughtByWidenedWatermark(unittest.TestCase):
    def test_edge_insert_with_no_node_change_is_not_a_false_skip(self):
        """An edges-only content change (a new edge, no new/changed node) must
        be caught by the widened watermark — not a false 'up to date' skip.
        #2235 review suggestion: pre-widening, the watermark covered `nodes`
        only, so this exact shape of change was invisible to it."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            with open(archive, "rb") as f:
                first_content = f.read()

            _insert_edge(db, 1, "2026-01-02T00:00:00Z")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(
                os.path.exists(suffixed),
                "an edge-only change must not be treated as 'nothing new'",
            )
            # Original archive untouched.
            with open(archive, "rb") as f:
                self.assertEqual(f.read(), first_content)
            self.assertNotIn("skipping", buf.getvalue())

    def test_second_run_no_changes_including_edges_is_still_a_genuine_skip(self):
        """The widened watermark must not become OVER-sensitive — a second
        run with truly nothing changed (nodes or edges) is still a genuine
        skip."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)
            _insert_edge(db, 1, "2026-01-01T00:00:00Z")

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertFalse(os.path.exists(suffixed))
            self.assertIn("skipping", buf.getvalue())


class TestEditHistoryOnlyChangeCaughtByWidenedWatermark(unittest.TestCase):
    def test_edit_history_insert_with_no_node_or_edge_change_is_not_a_false_skip(self):
        """An edit_history-only content change (e.g. a supersede/retract that
        writes no new node or edge row -- the pure-in-place-UPDATE case) must
        be caught by the round-2-widened watermark. #2235 review, round 2:
        a live-graph measurement found a retraction usually leaves no new
        node AND no new edge, so edit_history is the one place tool-mediated
        supersede/retract activity is guaranteed to show up."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            with open(archive, "rb") as f:
                first_content = f.read()

            _insert_edit_history(db, 1, "2026-01-02T00:00:00Z")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(
                os.path.exists(suffixed),
                "an edit_history-only change must not be treated as 'nothing new'",
            )
            with open(archive, "rb") as f:
                self.assertEqual(f.read(), first_content)
            self.assertNotIn("skipping", buf.getvalue())

    def test_second_run_no_changes_including_edit_history_is_still_a_genuine_skip(self):
        """Still not over-sensitive: a second run with truly nothing changed
        (nodes, edges, or edit_history) is a genuine skip."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)
            _insert_edit_history(db, 1, "2026-01-01T00:00:00Z")

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertFalse(os.path.exists(suffixed))
            self.assertIn("skipping", buf.getvalue())


class TestOldFormatWatermarkTreatedAsAbsent(unittest.TestCase):
    def test_nodes_only_sidecar_from_before_edge_widening_is_not_trusted(self):
        """A .watermark.json written by a pre-#2235-review version of this
        tool (nodes-only: row_count/max_created_at, no edge or history
        fields) must be treated as absent, not as a match -- it never
        computed that coverage, so trusting it as a match could hide an
        edges- or edit_history-only change."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            with open(archive, "w") as f:
                f.write("-- archive from the old format\n")
            # Old-format sidecar: matches live nodes state, but has no edge
            # or edit_history fields at all.
            with open(os.path.join(out_dir, ".watermark.json"), "w") as f:
                json.dump({"row_count": 1, "max_created_at": "2026-01-01T00:00:00Z", "archive": os.path.basename(archive)}, f)

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 0)
            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(
                os.path.exists(suffixed),
                "an old-format (nodes-only) watermark must not be trusted as a match",
            )

    def test_round1_format_sidecar_no_history_fields_is_not_trusted(self):
        """A .watermark.json written by the round-1 (#2235) version of this
        tool -- nodes+edges, but no history_count/history_max_timestamp --
        must ALSO be treated as absent under round 2, for the same reason:
        it never computed edit_history coverage."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            os.makedirs(out_dir)
            _build_minimal_db(db)

            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            with open(archive, "w") as f:
                f.write("-- archive from the round-1 format\n")
            with open(os.path.join(out_dir, ".watermark.json"), "w") as f:
                json.dump(
                    {
                        "row_count": 1,
                        "max_created_at": "2026-01-01T00:00:00Z",
                        "edge_count": 0,
                        "edge_max_created_at": None,
                        "archive": os.path.basename(archive),
                    },
                    f,
                )

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 0)
            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(
                os.path.exists(suffixed),
                "a round-1-format (nodes+edges, no history) watermark must not be trusted as a match",
            )


class TestOptionalTablesMissingDegradesGracefully(unittest.TestCase):
    """#2244: a DB lacking `edges`/`edit_history` entirely must not make the
    tool refuse to back up -- it should degrade to a weaker watermark, the
    same fail-toward-backing-up direction as every other decision in this
    file."""

    def test_db_with_only_nodes_table_still_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_nodes_only_db(db)

            rc = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])

            self.assertEqual(rc, 0, "a nodes-only DB must still produce a real backup, not exit 1")
            archive = os.path.join(out_dir, f"knowledge-{_today()}.sql")
            self.assertTrue(os.path.exists(archive))

    def test_second_run_no_changes_on_nodes_only_db_is_still_a_genuine_skip(self):
        """The graceful degrade must not become a permanent no-skip-ever --
        two runs against the SAME unchanged nodes-only DB should still skip
        the second time (the persisted watermark's (None, None) for the
        missing tables matches the live (None, None))."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_nodes_only_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertFalse(os.path.exists(suffixed), "unchanged nodes-only DB should still genuinely skip")
            self.assertIn("skipping", buf.getvalue())

    def test_skip_message_on_nodes_only_db_reads_na_not_none(self):
        """Reviewer-fairy suggestion (#2245): the skip message must not
        print a bare 'None' for an absent table's count/timestamp -- that
        reads as a leaked value, not an intentional 'not applicable'."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_nodes_only_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)

            output = buf.getvalue()
            self.assertIn("skipping", output)
            self.assertIn("n/a edges", output)
            self.assertIn("n/a edit_history rows", output)
            self.assertNotIn("None", output)

    def test_edges_table_appearing_later_forces_a_real_backup_not_a_false_skip(self):
        """Safety check: if a DB starts nodes-only (watermark has edge_count
        None) and LATER gains an edges table with real rows, the mismatch
        (None vs an actual count) must force a real backup -- not be
        mistaken for 'still nothing new'."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_nodes_only_db(db)

            rc1 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc1, 0)

            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id TEXT, target_id TEXT, "
                "relation TEXT, created_at TEXT)"
            )
            conn.execute(
                "INSERT INTO edges VALUES (1, 'ob_1', 'ob_1', 'self_test', '2026-01-02T00:00:00Z')"
            )
            conn.commit()
            conn.close()

            rc2 = backup_knowledge_db.main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc2, 0)
            suffixed = os.path.join(out_dir, f"knowledge-{_today()}-2.sql")
            self.assertTrue(
                os.path.exists(suffixed),
                "edges table appearing with real content must force a real backup, not a false skip",
            )


# ---------------------------------------------------------------------------
# #4208 -- default retention, binary rollback copies, untracking.
# Written BEFORE the implementation (contract-first). Cells are named for the
# contract item they pin (C1..C4 of the spec).
# ---------------------------------------------------------------------------
import pathlib  # noqa: E402
import subprocess  # noqa: E402
from unittest import mock  # noqa: E402


def _touch_archives(out_dir: str, names) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for n in names:
        with open(os.path.join(out_dir, n), "w") as f:
            f.write("-- archive\n")


def _daily_names(start: str, count: int) -> list:
    d0 = datetime.date.fromisoformat(start)
    return [f"knowledge-{(d0 + datetime.timedelta(days=i)).isoformat()}.sql" for i in range(count)]


def _sql_set(out_dir: str) -> set:
    return {n for n in os.listdir(out_dir) if n.endswith(".sql")}


def _run_main(argv):
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = backup_knowledge_db.main(argv)
    return rc, buf.getvalue(), err.getvalue()


class TestDefaultRetention4208(unittest.TestCase):
    """C1: 14 newest dailies + the first archive of each calendar month."""

    def _run(self, out_dir, extra=()):
        # Drive the dispatch helper directly so the cell does not depend on a db
        # or on today's date (the default policy is filename-date based).
        args = backup_knowledge_db._parse_args(["--out-dir", out_dir, *extra])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            backup_knowledge_db._apply_retention(out_dir, args)
        return buf.getvalue()

    def test_c1a_20_dailies_keep_14_newest_plus_monthly_anchor(self):
        with tempfile.TemporaryDirectory() as out_dir:
            names = _daily_names("2026-09-20", 20)  # 09-20 .. 10-09
            _touch_archives(out_dir, names)
            self._run(out_dir)
            expected = set(names[-14:]) | {"knowledge-2026-09-20.sql"}
            self.assertEqual(_sql_set(out_dir), expected)
            # 10-01 is October's anchor and is also inside the newest 14.
            self.assertIn("knowledge-2026-10-01.sql", _sql_set(out_dir))

    def test_c1a2_lone_old_archive_is_its_months_anchor_and_kept(self):
        with tempfile.TemporaryDirectory() as out_dir:
            names = _daily_names("2026-10-01", 14) + ["knowledge-2026-08-17.sql", "knowledge-2026-08-18.sql"]
            _touch_archives(out_dir, names)
            self._run(out_dir)
            self.assertIn("knowledge-2026-08-17.sql", _sql_set(out_dir))      # August anchor
            self.assertNotIn("knowledge-2026-08-18.sql", _sql_set(out_dir))   # not anchor, not newest 14

    def test_c1b_same_day_suffix_prunes_with_its_day(self):
        with tempfile.TemporaryDirectory() as out_dir:
            names = _daily_names("2026-09-20", 20) + ["knowledge-2026-09-21-2.sql"]
            _touch_archives(out_dir, names)
            self._run(out_dir)
            left = _sql_set(out_dir)
            self.assertNotIn("knowledge-2026-09-21.sql", left)
            self.assertNotIn("knowledge-2026-09-21-2.sql", left)

    def test_c1b2_same_day_suffix_is_kept_with_its_day(self):
        with tempfile.TemporaryDirectory() as out_dir:
            names = _daily_names("2026-09-20", 20) + ["knowledge-2026-09-20-2.sql", "knowledge-2026-10-09-2.sql"]
            _touch_archives(out_dir, names)
            self._run(out_dir)
            left = _sql_set(out_dir)
            self.assertIn("knowledge-2026-09-20-2.sql", left)   # anchor day: all its files kept
            self.assertIn("knowledge-2026-10-09-2.sql", left)   # newest day

    def test_c1c_keep_all_prunes_nothing(self):
        with tempfile.TemporaryDirectory() as out_dir:
            names = _daily_names("2026-09-20", 20)
            _touch_archives(out_dir, names)
            self._run(out_dir, ["--keep-all"])
            self.assertEqual(_sql_set(out_dir), set(names))

    def test_c1d_prune_days_override_prunes_by_cutoff_only_even_a_monthly_anchor(self):
        with tempfile.TemporaryDirectory() as out_dir:
            today = datetime.date.today()
            old = (today - datetime.timedelta(days=10)).isoformat()   # lone => an anchor by default
            new = (today - datetime.timedelta(days=1)).isoformat()
            _touch_archives(out_dir, [f"knowledge-{old}.sql", f"knowledge-{new}.sql"])
            self._run(out_dir, ["--prune-days", "3"])
            self.assertEqual(_sql_set(out_dir), {f"knowledge-{new}.sql"})

    def test_c1e_dry_run_prints_each_path_and_removes_nothing(self):
        with tempfile.TemporaryDirectory() as out_dir:
            names = _daily_names("2026-09-20", 20)
            _touch_archives(out_dir, names)
            out = self._run(out_dir, ["--dry-run"])
            self.assertEqual(_sql_set(out_dir), set(names))
            would = {
                os.path.basename(l.split("would prune:", 1)[1].strip())
                for l in out.splitlines() if l.startswith("[dry-run] would prune:")
            }
            self.assertEqual(would, set(names[1:6]))  # 09-21 .. 09-25

    def test_c1f_keep_all_with_prune_days_is_a_usage_error_naming_both(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            backup_knowledge_db._parse_args(["--keep-all", "--prune-days", "3"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--keep-all", err.getvalue())
        self.assertIn("--prune-days", err.getvalue())

    def test_c1f2_keep_all_with_prune_days_zero_is_not_an_error_and_prunes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)
            names = _daily_names("2026-01-01", 20)
            _touch_archives(out_dir, names)
            rc, _, err = _run_main(
                ["--db", db, "--out-dir", out_dir, "--no-binary", "--keep-all", "--prune-days", "0"]
            )
            self.assertEqual(rc, 0, err)
            self.assertTrue(set(names) <= _sql_set(out_dir))

    def test_c1g2_absent_flag_parses_to_none(self):
        self.assertIsNone(backup_knowledge_db._parse_args([]).retain)
        self.assertEqual(backup_knowledge_db._parse_args(["--retain", "0"]).retain, 0)

    def test_c1g_retain_and_prune_days_are_the_same_flag(self):
        a = backup_knowledge_db._parse_args(["--retain", "5"])
        b = backup_knowledge_db._parse_args(["--prune-days", "5"])
        self.assertEqual((a.retain, b.retain), (5, 5))

    def test_c1h_non_archive_files_and_watermark_are_never_touched(self):
        with tempfile.TemporaryDirectory() as out_dir:
            extras = [".watermark.json", "notes.sql", "knowledge-latest.sql"]
            _touch_archives(out_dir, _daily_names("2026-09-20", 20) + extras)
            self._run(out_dir)
            left = set(os.listdir(out_dir))
            for keep in extras:
                self.assertIn(keep, left)

    def test_c1i_main_applies_default_retention_after_a_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "knowledge.db")
            out_dir = os.path.join(tmp, "db-backup")
            _build_minimal_db(db)
            _touch_archives(out_dir, _daily_names("2026-01-01", 20))
            rc, _, _ = _run_main(["--db", db, "--out-dir", out_dir, "--no-binary"])
            self.assertEqual(rc, 0)
            left = _sql_set(out_dir)
            self.assertIn("knowledge-2026-01-01.sql", left)       # anchor
            self.assertNotIn("knowledge-2026-01-02.sql", left)    # pruned
            self.assertIn(f"knowledge-{_today()}.sql", left)


def _binary_files(out_dir: str) -> list:
    d = os.path.join(out_dir, "binary")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


class TestBinaryRollbackCopies4208(unittest.TestCase):
    """C2: db-backup/binary/knowledge-<ISO-UTC>.db, newest 2 kept."""

    def _setup(self, tmp):
        db = os.path.join(tmp, "knowledge.db")
        out_dir = os.path.join(tmp, "db-backup")
        _build_minimal_db(db)
        return db, out_dir

    def test_c2a_three_writes_leave_the_two_newest_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            written = []
            for _ in range(3):
                rc, out, _ = _run_main(["--db", db, "--out-dir", out_dir, "--force"])
                self.assertEqual(rc, 0)
                lines = [l for l in out.splitlines() if l.startswith("binary copy written: ")]
                self.assertEqual(len(lines), 1)
                written.append(os.path.basename(lines[0].split(": ", 1)[1]))
            self.assertEqual(len(set(written)), 3)
            self.assertEqual(written, sorted(written))
            self.assertEqual(_binary_files(out_dir), written[-2:])
            for n in written:
                self.assertRegex(n, r"^knowledge-\d{4}-\d{2}-\d{2}T\d{6}Z\.db$")

    def test_c2a2_rotation_prints_pruned_binary_and_leaves_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            bdir = os.path.join(out_dir, "binary")
            os.makedirs(bdir)
            for n in ("knowledge-2020-01-01T000000Z.db", "knowledge-2020-01-02T000000Z.db", "notes.txt"):
                open(os.path.join(bdir, n), "w").close()
            rc, out, _ = _run_main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc, 0)
            left = _binary_files(out_dir)
            self.assertNotIn("knowledge-2020-01-01T000000Z.db", left)
            self.assertIn("knowledge-2020-01-02T000000Z.db", left)
            self.assertIn("notes.txt", left)
            self.assertIn("pruned binary: ", out)

    def test_c2b_no_binary_writes_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            rc, out, _ = _run_main(["--db", db, "--out-dir", out_dir, "--no-binary"])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(os.path.join(out_dir, "binary")))
            self.assertNotIn("binary copy", out)

    def test_c2b2_no_binary_also_skips_its_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            bdir = os.path.join(out_dir, "binary")
            os.makedirs(bdir)
            names = [f"knowledge-2020-01-0{i}T000000Z.db" for i in (1, 2, 3)]
            for n in names:
                open(os.path.join(bdir, n), "w").close()
            _run_main(["--db", db, "--out-dir", out_dir, "--no-binary"])
            self.assertEqual(_binary_files(out_dir), names)

    def test_c2c_content_skip_writes_none_and_prunes_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            rc, _, _ = _run_main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc, 0)
            bdir = os.path.join(out_dir, "binary")
            open(os.path.join(bdir, "knowledge-2020-01-01T000000Z.db"), "w").close()
            before = _binary_files(out_dir)  # 2 files; a skip run must not touch them
            self.assertEqual(len(before), 2)
            rc, out, _ = _run_main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc, 0)
            self.assertIn("skipping", out)
            self.assertEqual(_binary_files(out_dir), before)
            self.assertNotIn("binary copy written", out)

    def test_c2c2_content_skip_does_not_rotate_an_oversized_binary_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            _run_main(["--db", db, "--out-dir", out_dir, "--no-binary"])
            bdir = os.path.join(out_dir, "binary")
            os.makedirs(bdir)
            names = [f"knowledge-2020-01-0{i}T000000Z.db" for i in (1, 2, 3)]
            for n in names:
                open(os.path.join(bdir, n), "w").close()
            rc, out, _ = _run_main(["--db", db, "--out-dir", out_dir])
            self.assertIn("skipping", out)
            self.assertEqual(_binary_files(out_dir), names)

    def test_c2d_copy_opens_as_sqlite_with_same_node_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            _insert_row(db, "ob_2", "2026-01-02T00:00:00Z")
            rc, _, _ = _run_main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc, 0)
            (name,) = _binary_files(out_dir)
            conn = sqlite3.connect(os.path.join(out_dir, "binary", name))
            try:
                n_copy = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n_copy, 2)

    def test_c2e_binary_dir_is_0700(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            _run_main(["--db", db, "--out-dir", out_dir])
            mode = os.stat(os.path.join(out_dir, "binary")).st_mode & 0o777
            self.assertEqual(mode, 0o700)

    def test_c2f_dry_run_prints_would_write_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)
            bdir = os.path.join(out_dir, "binary")
            os.makedirs(bdir)
            names = [f"knowledge-2020-01-0{i}T000000Z.db" for i in (1, 2, 3)]
            for n in names:
                open(os.path.join(bdir, n), "w").close()
            rc, out, _ = _run_main(["--db", db, "--out-dir", out_dir, "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("[dry-run] would write binary copy: ", out)
            self.assertEqual(_binary_files(out_dir), names)

    def test_c2g_owner_path_writes_binary_copy(self):
        """Owner path (flag ON): the copy comes from the owner snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            db, out_dir = self._setup(tmp)

            def fake_snapshot(spec, dest):
                import shutil as _sh
                _sh.copyfile(db, str(dest))
                return dest

            with mock.patch.object(backup_knowledge_db.owner_read, "flag_applies", return_value=True), \
                 mock.patch.object(backup_knowledge_db.owner_read, "snapshot", side_effect=fake_snapshot):
                rc, out, _ = _run_main(["--db", db, "--out-dir", out_dir])
            self.assertEqual(rc, 0)
            self.assertEqual(len(_binary_files(out_dir)), 1)


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout


def _tracked_repo(home: str) -> None:
    _git(home, "init", "-q")
    _git(home, "config", "user.email", "t@example.invalid")
    _git(home, "config", "user.name", "t")
    os.makedirs(os.path.join(home, "backups"))
    for rel in ("knowledge.db.2026-08-03-0045", "backups/old.db", "knowledge.sql", "x.bak-2026-09-09"):
        with open(os.path.join(home, rel), "w") as f:
            f.write("data\n")
    _git(home, "add", "-f", ".")
    _git(home, "commit", "-q", "-m", "base")


class TestUntrackIgnoredBinaries4208(unittest.TestCase):
    def test_c4a_untracks_binaries_keeps_sql_and_files_on_disk(self):
        with tempfile.TemporaryDirectory() as home:
            _tracked_repo(home)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = backup_knowledge_db._untrack_ignored_binaries(pathlib.Path(home), False)
            self.assertEqual(
                sorted(got),
                ["backups/old.db", "knowledge.db.2026-08-03-0045", "x.bak-2026-09-09"],
            )
            self.assertEqual(_git(home, "ls-files").split(), ["knowledge.sql"])
            for rel in ("knowledge.db.2026-08-03-0045", "backups/old.db", "knowledge.sql", "x.bak-2026-09-09"):
                self.assertTrue(os.path.exists(os.path.join(home, rel)), rel)
            self.assertEqual(buf.getvalue().count("untracked (stays on disk, no longer pushed): "), 3)

    def test_c4b_dry_run_leaves_the_index_unchanged(self):
        with tempfile.TemporaryDirectory() as home:
            _tracked_repo(home)
            before = _git(home, "ls-files")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                backup_knowledge_db._untrack_ignored_binaries(pathlib.Path(home), True)
            self.assertEqual(_git(home, "ls-files"), before)
            self.assertIn("[dry-run] would untrack:", buf.getvalue())

    def test_c4c_non_repo_returns_empty_and_prints_skip(self):
        with tempfile.TemporaryDirectory() as home:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                got = backup_knowledge_db._untrack_ignored_binaries(pathlib.Path(home), False)
            self.assertEqual(got, [])
            self.assertIn(f"untrack: {home} is not a git repo, skipped", buf.getvalue())

    def test_c4d_main_untracks_via_engram_home_after_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            _tracked_repo(home)
            db = os.path.join(tmp, "src.sqlite")
            _build_minimal_db(db)
            out_dir = os.path.join(tmp, "out")
            with mock.patch.object(backup_knowledge_db.owner_read, "engram_home",
                                   return_value=pathlib.Path(home)):
                rc, out, _ = _run_main(["--db", db, "--out-dir", out_dir, "--no-binary"])
            self.assertEqual(rc, 0)
            self.assertEqual(_git(home, "ls-files").split(), ["knowledge.sql"])
            self.assertTrue(os.path.exists(os.path.join(home, "backups", "old.db")))

    def test_c5_tracked_binary_paths_is_read_only_and_matches_untrack(self):
        with tempfile.TemporaryDirectory() as home:
            _tracked_repo(home)
            before = _git(home, "ls-files")
            import engram_core
            got = engram_core._tracked_binary_paths(pathlib.Path(home))
            self.assertEqual(
                sorted(got),
                ["backups/old.db", "knowledge.db.2026-08-03-0045", "x.bak-2026-09-09"],
            )
            self.assertEqual(_git(home, "ls-files"), before)


class TestRound3Fixes4208(unittest.TestCase):
    def test_r3_1_negative_retain_is_usage_error_and_deletes_nothing(self):
        for flag in ("--retain", "--prune-days"):
            with tempfile.TemporaryDirectory() as tmp:
                db = os.path.join(tmp, "k.db")
                out_dir = os.path.join(tmp, "out")
                _build_minimal_db(db)
                names = ["knowledge-2020-01-01.sql", "knowledge-2020-02-01.sql", "knowledge-2020-03-01.sql"]
                _touch_archives(out_dir, names)
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    backup_knowledge_db.main(["--db", db, "--out-dir", out_dir, flag, "-5"])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("must be 0 or a positive number of days (got -5)", err.getvalue())
                self.assertEqual(_sql_set(out_dir), set(names))

    def test_r3_2_untrack_uses_engram_core_git_exe_and_timeout(self):
        import engram_core
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".git"))
            calls = []

            def fake_run(cmd, **kw):
                calls.append((cmd, kw))
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(engram_core, "GIT_EXE", "/fake/bin/git-xyz"), \
                 mock.patch.object(engram_core, "GIT_TIMEOUT", 7), \
                 mock.patch.object(engram_core, "_tracked_binary_paths", return_value=["a.db.1"]), \
                 mock.patch.object(backup_knowledge_db.subprocess, "run", side_effect=fake_run), \
                 contextlib.redirect_stdout(io.StringIO()):
                backup_knowledge_db._untrack_ignored_binaries(pathlib.Path(home), False)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][0], "/fake/bin/git-xyz")
            self.assertEqual(calls[0][1]["timeout"], 7)

    def test_r3_3_basename_globs_root_anchored_prefixes(self):
        import engram_core
        with tempfile.TemporaryDirectory() as home:
            _git(home, "init", "-q")
            _git(home, "config", "user.email", "t@example.invalid")
            _git(home, "config", "user.name", "t")
            rels = ("my.db.d/readme.txt", "sub/dir/knowledge.db.2026-08-03-0045", "nested/backups/old.db",
                    "backups/root.db", "db-backup/z.sql")
            for rel in rels:
                os.makedirs(os.path.dirname(os.path.join(home, rel)), exist_ok=True)
                with open(os.path.join(home, rel), "w") as f:
                    f.write("d\n")
            _git(home, "add", "-f", ".")
            got = sorted(engram_core._tracked_binary_paths(pathlib.Path(home)))
            self.assertEqual(got, ["backups/root.db", "db-backup/z.sql",
                                   "sub/dir/knowledge.db.2026-08-03-0045"])

    def test_r3_4_stray_tmp_swept_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "k.db")
            out_dir = os.path.join(tmp, "out")
            _build_minimal_db(db)
            bdir = os.path.join(out_dir, "binary")
            os.makedirs(bdir)
            stray = os.path.join(bdir, ".knowledge-2020-01-01T000000Z.db.tmp")
            keep = os.path.join(bdir, "notes.txt")
            for f in (stray, keep):
                open(f, "w").write("x")
            backup_knowledge_db._write_binary_copy(out_dir, db)
            self.assertFalse(os.path.exists(stray))
            self.assertTrue(os.path.exists(keep))
            self.assertEqual(len([n for n in os.listdir(bdir) if n.endswith(".db")]), 1)


    def test_r4_1_untrack_stops_after_first_git_timeout(self):
        import engram_core
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".git"))
            calls = []

            def fake_run(cmd, **kw):
                calls.append(cmd)
                raise subprocess.TimeoutExpired(cmd, 1)

            err = io.StringIO()
            with mock.patch.object(engram_core, "_tracked_binary_paths",
                                   return_value=["a.db.1", "b.db.2", "c.db.3"]), \
                 mock.patch.object(backup_knowledge_db.subprocess, "run", side_effect=fake_run), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                got = backup_knowledge_db._untrack_ignored_binaries(pathlib.Path(home), False)
            self.assertEqual(len(calls), 1)
            self.assertEqual(got, [])
            self.assertIn("a.db.1", err.getvalue())
            self.assertIn("remaining paths are skipped this run", err.getvalue())

    def test_r4_1b_timeout_does_not_change_backup_exit(self):
        import engram_core
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(os.path.join(home, ".git"))
            db = os.path.join(tmp, "src.sqlite")
            _build_minimal_db(db)
            out_dir = os.path.join(tmp, "out")

            def fake_run(cmd, **kw):
                raise subprocess.TimeoutExpired(cmd, 1)

            with mock.patch.object(backup_knowledge_db.owner_read, "engram_home",
                                   return_value=pathlib.Path(home)), \
                 mock.patch.object(engram_core, "_tracked_binary_paths", return_value=["a.db.1", "b.db.2"]), \
                 mock.patch.object(backup_knowledge_db.subprocess, "run", side_effect=fake_run):
                rc, _, _ = _run_main(["--db", db, "--out-dir", out_dir, "--no-binary"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
