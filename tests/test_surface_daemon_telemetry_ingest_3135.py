"""#3135 (telemetry WS6): the surface daemon owns the records.db ingest.

run_indexer_tick() gains a second, independent step after the log-indexer
pass: engram_telemetry_store.ingest(<ENGRAM_HOME>/telemetry/records.db,
<ENGRAM_HOME>/telemetry).

Contracts asserted here:
  C1  one tick ingests two .jsonl.done files into records.db (actual rows)
      and sweeps them.
  C2  independence in both directions (each step in its own try).
  C3  two ticks over the same files -> no duplicate rows.
  C4  engram_telemetry_store unimportable (Essential-only build) -> the log
      indexer still runs and the tick returns normally, silently.
  C5  idle telemetry dir -> no telemetry stderr line.

Every test uses a temp ENGRAM_HOME; nothing touches ~/.engram.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
_HOOK_DIR = _ROOT / "src" / "engram" / "hooks" / "claude"
_SRC_DIR = _ROOT / "src" / "engram"
for _p in (str(_HOOK_DIR), str(_SRC_DIR), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DAEMON_PATH = _HOOK_DIR / "engram-surface-daemon.py"

import engram_telemetry_store  # noqa: E402  (convenience tier; present in the repo)


def _load_daemon():
    spec = importlib.util.spec_from_file_location("engram_surface_daemon_3135", str(_DAEMON_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _record_line(n: int) -> str:
    return json.dumps({
        "ts_utc": f"2026-09-29T00:00:0{n}Z",
        "manifest_id": f"m{n}",
        "event_type": "test.event",
        "session_id": "s1",
        "fields": {"n": n},
    }) + "\n"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.tdir = self.home / "telemetry"
        self.tdir.mkdir()
        self.mod = _load_daemon()
        self.mod.ENGRAM_HOME = str(self.home)
        self.run_once = MagicMock(return_value={"files_processed": 0, "rows_inserted": 0})
        fake_indexer = MagicMock(Indexer=MagicMock(run_once=self.run_once))
        p = patch.dict("sys.modules", {"engram_log_indexer": fake_indexer})
        p.start()
        self.addCleanup(p.stop)

    def _write_done_files(self):
        (self.tdir / "111-aaa.jsonl.done").write_text(_record_line(1) + _record_line(2))
        (self.tdir / "222-bbb.jsonl.done").write_text(_record_line(3))

    def _tick(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            ts = self.mod.run_indexer_tick(float("-inf"), 60)
        return ts, buf.getvalue()

    def _rows(self):
        conn = sqlite3.connect(str(self.tdir / "records.db"))
        try:
            return conn.execute(
                "SELECT manifest_id, event_type, session_id, fields_json FROM l0_records ORDER BY manifest_id"
            ).fetchall()
        finally:
            conn.close()


class TestC1IngestAndSweep(_Base):
    def test_tick_ingests_rows_and_sweeps_done_files(self):
        self._write_done_files()
        ts, err = self._tick()
        self.assertNotEqual(ts, float("-inf"))
        self.assertEqual(
            self._rows(),
            [
                ("m1", "test.event", "s1", '{"n":1}'),
                ("m2", "test.event", "s1", '{"n":2}'),
                ("m3", "test.event", "s1", '{"n":3}'),
            ],
        )
        self.assertEqual(sorted(p.name for p in self.tdir.glob("*.jsonl*")), [])
        self.assertIn("[engram-daemon] telemetry tick: 2 file(s), 3 record(s), 2 swept", err)
        self.assertNotIn("error", err)


class TestC2Independence(_Base):
    def test_raising_telemetry_ingest_does_not_skip_log_indexer(self):
        self._write_done_files()
        with patch.object(engram_telemetry_store, "ingest", side_effect=RuntimeError("boom")):
            ts, err = self._tick()
        self.run_once.assert_called_once()
        self.assertNotEqual(ts, float("-inf"))
        self.assertIn("[engram-daemon] telemetry ingest failed (suppressed): boom", err)

    def test_raising_log_indexer_does_not_skip_telemetry_ingest(self):
        self._write_done_files()
        self.run_once.side_effect = RuntimeError("indexer down")
        ts, err = self._tick()
        self.assertNotEqual(ts, float("-inf"))
        self.assertIn("indexer tick failed (suppressed): indexer down", err)
        self.assertEqual(len(self._rows()), 3)


class TestC3Idempotence(_Base):
    def test_two_ticks_do_not_duplicate(self):
        # Bare .jsonl (not swept: fresh mtime) so the second tick re-reads it.
        (self.tdir / "333-ccc.jsonl").write_text(_record_line(1) + _record_line(2))
        self._tick()
        self._tick()
        self.assertEqual([r[0] for r in self._rows()], ["m1", "m2"])
        self.assertTrue((self.tdir / "333-ccc.jsonl").exists())


class TestC4TierFallback(_Base):
    def test_store_unimportable_runs_log_indexer_and_returns(self):
        self._write_done_files()
        with patch.dict("sys.modules", {"engram_telemetry_store": None}):
            ts, err = self._tick()
        self.run_once.assert_called_once()
        self.assertNotEqual(ts, float("-inf"))
        self.assertNotIn("telemetry", err)
        self.assertFalse((self.tdir / "records.db").exists())


class TestC5QuietWhenIdle(_Base):
    def test_empty_dir_prints_nothing_telemetry(self):
        # The step must actually run (spy), and still print nothing.
        with patch.object(engram_telemetry_store, "ingest", wraps=engram_telemetry_store.ingest) as spy:
            ts, err = self._tick()
        spy.assert_called_once_with(
            str(self.home / "telemetry" / "records.db"), str(self.home / "telemetry")
        )
        self.assertNotEqual(ts, float("-inf"))
        self.assertNotIn("telemetry", err)


class TestC6MissingTelemetryDir(_Base):
    """A seat that has never written telemetry has no telemetry/ dir; ingest()
    would raise (sqlite cannot create records.db there). That is the normal
    fresh-install state, not a failure: silent, and nothing is created."""

    def test_missing_dir_is_silent_and_creates_nothing(self):
        self.tdir.rmdir()
        ts, err = self._tick()
        self.run_once.assert_called_once()
        self.assertNotEqual(ts, float("-inf"))
        self.assertEqual(err, "")
        self.assertFalse(self.tdir.exists())


class TestC7LogConditionClauses(_Base):
    """Each clause of `records_ingested > 0 OR files_swept > 0 OR errors` must
    fire the telemetry line on its own (the all-zero/quiet case is C5). C1's
    swept files also carry records, so it cannot pin the swept or errors
    clauses; these stub ingest() with summaries that isolate one clause."""

    def _tick_with_summary(self, **overrides):
        summary = {"files_processed": 0, "records_ingested": 0, "files_swept": 0, "errors": []}
        summary.update(overrides)
        with patch.object(engram_telemetry_store, "ingest", return_value=summary):
            return self._tick()

    def test_swept_only_prints_line(self):
        _, err = self._tick_with_summary(files_swept=2)
        self.assertIn("[engram-daemon] telemetry tick:", err)
        self.assertIn("2 swept", err)
        self.assertNotIn("error(s)", err)

    def test_errors_only_prints_line(self):
        _, err = self._tick_with_summary(errors=["x"])
        self.assertIn("[engram-daemon] telemetry tick:", err)
        self.assertIn("1 error(s)", err)


if __name__ == "__main__":
    unittest.main()
