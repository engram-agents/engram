"""#3227 -- a swept telemetry file must not leave a live offset behind.

``_maybe_sweep_file`` unlinks a fully-ingested file but used to leave its
``ingest_offsets`` row untouched. A writer caches its path and appends by
reopening, so a writer idle for 5+ days that writes again RECREATES the file
under the same key, and the next ingest seeked to the stale offset: shorter
content was never read, longer content lost its first ``offset`` bytes, and
nothing reported it.

Contract under test (written before the implementation):
  * a swept row is marked (``swept_at``); a file recreated under a swept key
    is ingested from byte 0 and the mark is cleared;
  * rows written before this fix (no ``swept_at`` column) are migrated, and
    rows whose file is absent are marked at the next ingest();
  * an offset beyond the file's size is a replaced/truncated file: read from 0;
  * every such reset is counted in ``offsets_reset``; the ordinary incremental
    path never resets;
  * the row is never deleted (WS8's staleness signal reads
    ``MAX(last_ingested_at)`` over the table);
  * ``open_store`` migration is idempotent.

Temp dirs only -- never ``~/.engram``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import engram_telemetry_store as ts

_PRE_FIX_OFFSETS_SCHEMA = """
CREATE TABLE ingest_offsets (
    source_file   TEXT PRIMARY KEY,
    byte_offset   INTEGER NOT NULL DEFAULT 0,
    last_ingested_at TEXT NOT NULL
);
"""


def _line(event_type: str) -> str:
    return json.dumps(
        {
            "ts_utc": "2026-09-11T00:00:00.000Z",
            "manifest_id": "m1",
            "event_type": event_type,
            "fields": {},
        }
    ) + "\n"


class SweptOffsetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram-3227-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tdir = Path(self.tmp) / "telemetry"
        self.tdir.mkdir()
        self.db = Path(self.tmp) / "records.db"

    def write(self, name: str, event_types: list[str]) -> Path:
        p = self.tdir / name
        p.write_text("".join(_line(e) for e in event_types), encoding="utf-8")
        return p

    def age(self, path: Path) -> None:
        t = time.time() - (ts._STALE_FILE_AGE_DAYS + 1) * ts._SECONDS_PER_DAY
        os.utime(path, (t, t))

    def events(self) -> list[str]:
        conn = sqlite3.connect(self.db)
        try:
            return [r[0] for r in conn.execute("SELECT event_type FROM l0_records ORDER BY id")]
        finally:
            conn.close()

    def sweep_bare(self, name: str, event_types: list[str]) -> None:
        """Ingest a bare file, age it past the stale threshold, ingest again
        so it is swept. Leaves the file absent and the row behind."""
        p = self.write(name, event_types)
        ts.ingest(self.db, self.tdir)
        self.age(p)
        summary = ts.ingest(self.db, self.tdir)
        self.assertEqual(summary["files_swept"], 1)
        self.assertFalse(p.exists())


class TestRecreatedAfterSweep(SweptOffsetBase):
    def test_t1_recreated_shorter_file_is_ingested(self):
        self.sweep_bare("100-1.jsonl", ["a1", "a2", "a3"])
        self.write("100-1.jsonl", ["b"])  # shorter than the old offset
        summary = ts.ingest(self.db, self.tdir)
        self.assertEqual(self.events(), ["a1", "a2", "a3", "b"])
        self.assertEqual(summary["offsets_reset"], 1)

    def test_t2_recreated_longer_file_loses_nothing_from_the_head(self):
        self.sweep_bare("101-1.jsonl", ["a1"])
        new = [f"b{i}" for i in range(6)]  # longer than the old offset
        self.write("101-1.jsonl", new)
        summary = ts.ingest(self.db, self.tdir)
        self.assertEqual(self.events(), ["a1"] + new)
        self.assertEqual(summary["offsets_reset"], 1)


class TestLegacyRowReconcile(SweptOffsetBase):
    def test_t3_legacy_row_with_absent_file_is_marked_and_survives(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(_PRE_FIX_OFFSETS_SCHEMA)
        conn.execute(
            "INSERT INTO ingest_offsets VALUES (?, ?, ?)",
            ("102-1.jsonl", 9999, "2026-09-01T00:00:00.000Z"),
        )
        conn.commit()
        conn.close()

        ts.ingest(self.db, self.tdir)  # empty dir

        conn = sqlite3.connect(self.db)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(ingest_offsets)")]
            self.assertIn("swept_at", cols)
            rows = conn.execute(
                "SELECT source_file, swept_at FROM ingest_offsets"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, "the row must survive reconciliation")
        self.assertEqual(rows[0][0], "102-1.jsonl")
        self.assertIsNotNone(rows[0][1], "an absent file's row must be marked swept")

        self.write("102-1.jsonl", ["x", "y"])
        summary = ts.ingest(self.db, self.tdir)
        self.assertEqual(self.events(), ["x", "y"])
        self.assertEqual(summary["offsets_reset"], 1)


class TestShrinkGuard(SweptOffsetBase):
    def test_t4_offset_beyond_file_size_reads_from_zero(self):
        ts.open_store(self.db).close()
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO ingest_offsets (source_file, byte_offset, last_ingested_at) "
            "VALUES (?, ?, ?)",
            ("103-1.jsonl", 100000, "2026-09-01T00:00:00.000Z"),
        )
        conn.commit()
        conn.close()
        self.write("103-1.jsonl", ["p", "q"])  # swept_at is NULL on that row

        summary = ts.ingest(self.db, self.tdir)
        self.assertEqual(self.events(), ["p", "q"])
        self.assertEqual(summary["offsets_reset"], 1)


class TestNoFalseResets(SweptOffsetBase):
    def test_t5_incremental_appends_and_rename_never_reset_or_duplicate(self):
        p = self.tdir / "104-1.jsonl"
        expected: list[str] = []
        for i in range(3):
            with open(p, "a", encoding="utf-8") as f:
                f.write(_line(f"e{i}"))
            expected.append(f"e{i}")
            summary = ts.ingest(self.db, self.tdir)
            self.assertEqual(summary["offsets_reset"], 0)
            self.assertEqual(summary["records_ingested"], 1)
        # an ingest with nothing new: offset == file size, must not reset
        summary = ts.ingest(self.db, self.tdir)
        self.assertEqual(summary["offsets_reset"], 0)
        self.assertEqual(summary["records_ingested"], 0)
        # clean-exit rename, then sweep of the .done file
        os.rename(p, p.with_name(p.name + ".done"))
        summary = ts.ingest(self.db, self.tdir)
        self.assertEqual(summary["offsets_reset"], 0)
        self.assertEqual(summary["records_ingested"], 0)
        self.assertEqual(summary["files_swept"], 1)
        # and once more with the file gone
        summary = ts.ingest(self.db, self.tdir)
        self.assertEqual(summary["offsets_reset"], 0)
        self.assertEqual(self.events(), expected)


class TestWS8Unaffected(SweptOffsetBase):
    def test_t6_row_and_last_ingested_at_survive_a_sweep(self):
        self.sweep_bare("105-1.jsonl", ["a"])
        conn = sqlite3.connect(self.db)
        try:
            (mx,) = conn.execute("SELECT MAX(last_ingested_at) FROM ingest_offsets").fetchone()
            (n,) = conn.execute("SELECT COUNT(*) FROM ingest_offsets").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(mx)
        self.assertEqual(n, 1)


class TestMigration(SweptOffsetBase):
    def test_t7_open_store_migration_is_idempotent(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(_PRE_FIX_OFFSETS_SCHEMA)
        conn.commit()
        conn.close()
        ts.open_store(self.db).close()
        ts.open_store(self.db).close()
        conn = sqlite3.connect(self.db)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(ingest_offsets)")]
        finally:
            conn.close()
        self.assertEqual(cols.count("swept_at"), 1)


class TestMissingTelemetryDirMarksNothing(SweptOffsetBase):
    def test_t8_absent_dir_must_not_mark_rows_swept(self):
        """A telemetry dir that is briefly not a directory lists as [], which
        must NOT read as "every file is gone": the marked rows would make the
        same files re-ingest from byte 0 when the dir returns (duplicates)."""
        self.write("200-1.jsonl", ["a", "b"])
        first = ts.ingest(self.db, self.tdir)
        self.assertEqual(first["records_ingested"], 2)

        moved = Path(self.tmp) / "telemetry-away"
        os.rename(self.tdir, moved)
        s1 = ts.ingest(self.db, self.tdir)  # dir absent
        os.rename(moved, self.tdir)
        s2 = ts.ingest(self.db, self.tdir)  # dir back

        self.assertEqual(self.events(), ["a", "b"], "no duplicate rows")
        self.assertEqual(s1["offsets_reset"], 0)
        self.assertEqual(s2["offsets_reset"], 0)
        conn = sqlite3.connect(self.db)
        try:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM ingest_offsets WHERE swept_at IS NOT NULL"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(n, 0)


class TestConcurrentMigrationRace(SweptOffsetBase):
    def test_t9_duplicate_column_from_a_racing_open_store_is_tolerated(self):
        ts.open_store(self.db).close()  # column now exists
        real_connect = sqlite3.connect

        class _Racy(sqlite3.Connection):
            hid = False

            def execute(self, sql, *a, **k):
                # First PRAGMA table_info reports "column missing" although
                # another process has just added it: the ALTER then races.
                if "table_info(ingest_offsets)" in sql and not _Racy.hid:
                    _Racy.hid = True
                    return super().execute("SELECT 1, 'x' WHERE 0")
                return super().execute(sql, *a, **k)

        with mock.patch.object(
            ts.sqlite3,
            "connect",
            side_effect=lambda *a, **k: real_connect(*a, factory=_Racy, **k),
        ):
            conn = ts.open_store(self.db)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(ingest_offsets)")]
            self.assertEqual(cols.count("swept_at"), 1)
        finally:
            conn.close()
        self.assertTrue(_Racy.hid)


class TestSweepMarkCommittedAtUnlink(SweptOffsetBase):
    def test_t10_mark_is_committed_before_a_later_files_sweep_fails(self):
        a = self.write("300-1.jsonl.done", ["a"])
        self.write("301-1.jsonl.done", ["b"])
        ts.ingest(self.db, self.tdir)  # both ingested and (only then) swept
        # Re-create both so the next ingest has two sweepable files, with
        # rows already at end-of-file (offsets kept, swept_at cleared by hand).
        self.write("300-1.jsonl.done", ["a"])
        self.write("301-1.jsonl.done", ["b"])
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE ingest_offsets SET swept_at = NULL")
        conn.commit()
        conn.close()

        real_sweep = ts._maybe_sweep_file
        calls = {"n": 0}

        def flaky(conn, path, now=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom after the first unlink")
            return real_sweep(conn, path, now=now)

        with mock.patch.object(ts, "_maybe_sweep_file", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                ts.ingest(self.db, self.tdir)

        self.assertFalse((self.tdir / "300-1.jsonl.done").exists())
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT swept_at FROM ingest_offsets WHERE source_file = ?",
                (ts._offset_key(a),),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row[0], "unlinked file's mark must be committed")


if __name__ == "__main__":
    unittest.main()
