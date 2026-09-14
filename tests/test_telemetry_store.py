"""Tests for engram_telemetry_store.py -- the telemetry store's WS1 slice
(engram-alpha#2574, epic #2013): the per-process JSONL writer
(``write_telemetry_record``), the rename-on-exit-to-``.done`` mechanism, the
two-mechanism drop-counting (row-on-next-success + stderr-flush-at-exit),
and the ``records.db`` aggregator (``open_store``/``ingest``) -- truncated-
trailing-line tolerance, ingest-offset tracking, and the age-based sweep.

WS1 spec: an implementation-ready spec handed to the coder-fairy for this
build (part of engram-alpha#2574, epic #2013) -- its "Tests to write"
section is the checklist this file implements one-for-one. Not committed
anywhere durable; the design doc below is the citable source of record.
Design doc: docs/2574-telemetry-design.md.

Fixture strategy: each test points ``ENGRAM_HOME`` at a fresh scratch temp
directory (mirrors tests/test_log_emitter.py's own EmitterTestBase pattern)
and directly resets engram_telemetry_store's module-level singleton state
(``_PROCESS_FILE_PATH``, ``_DROP_COUNT``) in setUp/tearDown -- same "poke the
private module global directly" convention test_log_emitter.py uses for
``_EMITTER_INSTANCE``. ``_ATEXIT_REGISTERED`` is deliberately left alone: it
is a per-*process* (not per-test) flag, and ``_ensure_atexit_registered()``
is idempotent, so leaving it set after the first test that touches it is
both correct and avoids stacking duplicate atexit callbacks across the whole
pytest run.

Two acceptance tests intentionally use REAL OS failure mechanisms rather
than mocks, per the WS1 spec's explicit instruction not to skip them as
"hard to test":

  - missing DATA_DIR: ENGRAM_HOME is pointed at a path *underneath a regular
    file* (not a nonexistent-but-creatable directory), so
    ``Path.mkdir(parents=True)`` genuinely fails with NotADirectoryError --
    a real filesystem failure, not a monkeypatched one.
  - full disk: simulated via ``resource.RLIMIT_FSIZE`` (the process's
    max-file-size limit) rather than literally filling a filesystem, which
    would need mount/tmpfs privileges this test environment cannot assume.
    Lowering RLIMIT_FSIZE and ignoring SIGXFSZ (its default disposition
    would otherwise kill the process, not raise) makes the write() syscall
    fail with a genuine ``OSError: [Errno 27] File too large`` -- a real
    kernel-level write failure through the same code path a full disk would
    hit, not a mock of one. See ``full_disk`` context manager below.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover -- POSIX-only module
    resource = None  # type: ignore[assignment]

# conftest.py (tests/conftest.py) already puts src/engram ahead of the repo
# root on sys.path -- inserting either ourselves here would risk the
# repo-root-vs-src/engram `tools` package collision conftest.py's own guard
# checks for (see that file's "#2565" comment), so this file relies on
# conftest.py's ordering rather than re-deriving it.
import engram_telemetry_store as ts

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"


@contextmanager
def full_disk(max_bytes: int = 5):
    """Simulate a full disk for the duration of the ``with`` block: any
    write() syscall past ``max_bytes`` for THIS process raises
    ``OSError(errno.EFBIG, "File too large")`` instead of succeeding. See
    module docstring for why this -- not a literal filled filesystem -- is
    the right instrument here."""
    if resource is None:
        raise unittest.SkipTest("resource module unavailable on this platform")
    import signal

    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    old_handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, old_handler)


class TelemetryStoreTestBase(unittest.TestCase):
    def setUp(self):
        ts._PROCESS_FILE_PATH = None
        ts._DROP_COUNT = 0
        self.tmp = tempfile.mkdtemp(prefix="engram_telemetry_test_")
        os.environ["ENGRAM_HOME"] = self.tmp

    def tearDown(self):
        ts._PROCESS_FILE_PATH = None
        ts._DROP_COUNT = 0
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ENGRAM_HOME", None)

    def telemetry_dir(self) -> Path:
        return Path(self.tmp) / "telemetry"

    def telemetry_files(self) -> list[Path]:
        d = self.telemetry_dir()
        if not d.is_dir():
            return []
        return sorted(d.iterdir())

    def read_records(self) -> list[dict]:
        """Every JSON record across every telemetry file, in file-then-line
        order (helper for tests that only ever produce one file/one line)."""
        out = []
        for f in self.telemetry_files():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    out.append(json.loads(line))
        return out


# ---------------------------------------------------------------------------
# write_telemetry_record: happy path
# ---------------------------------------------------------------------------


class TestWriteHappyPath(TelemetryStoreTestBase):
    def test_writes_one_line_with_required_fields_and_merged_fields(self):
        ok = ts.write_telemetry_record(
            "engram_log_emitter.py:emit", "engram.test.happy", fields={"foo": "bar", "n": 3}
        )
        self.assertTrue(ok)

        records = self.read_records()
        self.assertEqual(len(records), 1)
        record = records[0]

        self.assertEqual(record["manifest_id"], "engram_log_emitter.py:emit")
        self.assertEqual(record["event_type"], "engram.test.happy")
        self.assertIn("ts_utc", record)
        self.assertTrue(record["ts_utc"].endswith("Z"))
        self.assertEqual(record["fields"], {"foo": "bar", "n": 3})
        self.assertNotIn("dropped_total", record)

    def test_second_write_same_process_reuses_same_file(self):
        ts.write_telemetry_record("m1", "engram.test.a")
        ts.write_telemetry_record("m2", "engram.test.b")
        files = self.telemetry_files()
        self.assertEqual(len(files), 1, f"expected one file per process, got {files}")
        records = self.read_records()
        self.assertEqual(len(records), 2)

    def test_fields_defaults_to_empty_dict_when_omitted(self):
        ok = ts.write_telemetry_record("m1", "engram.test.nofields")
        self.assertTrue(ok)
        record = self.read_records()[0]
        self.assertEqual(record["fields"], {})


# ---------------------------------------------------------------------------
# write_telemetry_record: rejection (missing required fields)
# ---------------------------------------------------------------------------


class TestWriteRejection(TelemetryStoreTestBase):
    """Missing manifest_id/event_type -> False, no line written, and NOT
    counted as a drop (see write_telemetry_record's own docstring
    "Rejection vs drop" section for why -- this is a documented judgment
    call, not an accident)."""

    def test_missing_manifest_id_rejected(self):
        ok = ts.write_telemetry_record("", "engram.test.x")
        self.assertFalse(ok)
        self.assertEqual(self.telemetry_files(), [])
        self.assertEqual(ts._current_drop_count(), 0)

    def test_none_manifest_id_rejected(self):
        ok = ts.write_telemetry_record(None, "engram.test.x")  # type: ignore[arg-type]
        self.assertFalse(ok)
        self.assertEqual(self.telemetry_files(), [])
        self.assertEqual(ts._current_drop_count(), 0)

    def test_missing_event_type_rejected(self):
        ok = ts.write_telemetry_record("m1", "")
        self.assertFalse(ok)
        self.assertEqual(self.telemetry_files(), [])
        self.assertEqual(ts._current_drop_count(), 0)

    def test_non_dict_fields_rejected(self):
        ok = ts.write_telemetry_record("m1", "engram.test.x", fields="not-a-dict")  # type: ignore[arg-type]
        self.assertFalse(ok)
        self.assertEqual(self.telemetry_files(), [])
        self.assertEqual(ts._current_drop_count(), 0)

    def test_rejection_does_not_prevent_a_later_good_write(self):
        ts.write_telemetry_record("", "engram.test.bad")
        ok = ts.write_telemetry_record("m1", "engram.test.good")
        self.assertTrue(ok)
        records = self.read_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event_type"], "engram.test.good")


# ---------------------------------------------------------------------------
# ts_utc: genuinely UTC regardless of local timezone
# ---------------------------------------------------------------------------


class TestUtcTimestamp(TelemetryStoreTestBase):
    def test_ts_utc_is_utc_under_non_utc_local_tz(self):
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset() unavailable on this platform")

        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            before = datetime.now(timezone.utc)
            ok = ts.write_telemetry_record("m1", "engram.test.tz")
            after = datetime.now(timezone.utc)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

        self.assertTrue(ok)
        record = self.read_records()[0]
        raw = record["ts_utc"]
        self.assertTrue(raw.endswith("Z"), raw)
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        # The local TZ was 4-5h behind UTC (America/New_York); a local-time
        # bug (the exact §7 incident) would show up as several HOURS off
        # from [before, after] -- so a couple of seconds of slack is ample
        # tolerance while a timezone-confused timestamp would blow well past it.
        self.assertGreaterEqual(parsed, before - timedelta(seconds=5))
        self.assertLessEqual(parsed, after + timedelta(seconds=5))


# ---------------------------------------------------------------------------
# Never-raise acceptance tests (doc's own acceptance test; WS1 spec's
# explicit "don't skip these as hard to test")
# ---------------------------------------------------------------------------


class TestNeverRaise(TelemetryStoreTestBase):
    def test_missing_data_dir_returns_false_never_raises(self):
        # Point ENGRAM_HOME underneath a REGULAR FILE, not a nonexistent
        # directory -- mkdir(parents=True) on a nonexistent-but-creatable
        # path would just create it. Putting a file in the way makes the
        # mkdir genuinely, permanently fail (NotADirectoryError), which is
        # what "DATA_DIR missing/unusable" needs to mean for this test to be
        # non-vacuous.
        blocker = tempfile.NamedTemporaryFile(delete=False)
        blocker.close()
        self.addCleanup(lambda: os.unlink(blocker.name))
        os.environ["ENGRAM_HOME"] = os.path.join(blocker.name, "sub")

        try:
            ok = ts.write_telemetry_record("m1", "engram.test.baddir")
        except Exception as exc:  # pragma: no cover -- this IS the failure mode under test
            self.fail(f"write_telemetry_record raised: {exc!r}")

        self.assertFalse(ok)
        self.assertEqual(ts._current_drop_count(), 1)

    def test_full_disk_returns_false_never_raises(self):
        ts.write_telemetry_record("m0", "engram.test.pre")  # establish the process file first

        with full_disk(max_bytes=5):
            try:
                ok = ts.write_telemetry_record("m1", "engram.test.fulldisk")
            except Exception as exc:  # pragma: no cover -- this IS the failure mode under test
                self.fail(f"write_telemetry_record raised: {exc!r}")

        self.assertFalse(ok)
        self.assertEqual(ts._current_drop_count(), 1)

        # And the process is still usable afterward -- never raising also
        # means never corrupting module state for subsequent calls.
        ok2 = ts.write_telemetry_record("m2", "engram.test.recovered")
        self.assertTrue(ok2)

    def test_atexit_registration_failure_returns_false_never_raises(self):
        """Round-1 review regression (#2847): _ensure_atexit_registered() was
        called BEFORE write_telemetry_record's try/except, so a failure in
        IT (not in the inner write) escaped the function's own documented
        never-raise contract. atexit.register() essentially never fails
        under normal conditions -- that's exactly the "rare but real"
        failure class this test exists to pin, since "essentially never"
        passing every existing test is precisely how the gap went
        undetected: none of the other 21 tests exercise this call at all."""
        orig_register = ts.atexit.register
        pre_registered = ts._ATEXIT_REGISTERED
        ts.atexit.register = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("simulated atexit.register failure")
        )
        # Force through the registration attempt regardless of whether an
        # earlier test in this process already registered for real.
        ts._ATEXIT_REGISTERED = False
        try:
            try:
                ok = ts.write_telemetry_record("m1", "engram.test.atexitfail")
            except Exception as exc:  # pragma: no cover -- this IS the failure mode under test
                self.fail(f"write_telemetry_record raised: {exc!r}")
            self.assertFalse(ok)
            # The failed attempt must not have flipped the flag -- confirms
            # the register() failure never reached the "success" branch.
            self.assertFalse(ts._ATEXIT_REGISTERED)
        finally:
            ts.atexit.register = orig_register
            ts._ATEXIT_REGISTERED = pre_registered


# ---------------------------------------------------------------------------
# Drop-counting: row-on-next-success + stderr-flush-at-exit
# ---------------------------------------------------------------------------


class TestDropCounting(TelemetryStoreTestBase):
    def test_next_success_carries_the_accumulated_drop_count(self):
        ts.write_telemetry_record("m0", "engram.test.pre")  # establish the process file

        n_failures = 3
        with full_disk(max_bytes=5):
            for i in range(n_failures):
                ok = ts.write_telemetry_record("m1", f"engram.test.fail{i}")
                self.assertFalse(ok)

        self.assertEqual(ts._current_drop_count(), n_failures)

        ok = ts.write_telemetry_record("m2", "engram.test.recover")
        self.assertTrue(ok)

        records = self.read_records()
        recovered = records[-1]
        self.assertEqual(recovered["event_type"], "engram.test.recover")
        self.assertEqual(recovered["dropped_total"], n_failures)

    def test_stderr_flush_at_exit_shows_the_same_count(self):
        """Subprocess-based: N consecutive failures then one success, then
        the process exits cleanly. Both the recorded row (mechanism 2) and
        the stderr flush at exit (mechanism 3) must show the SAME N -- see
        write_telemetry_record's docstring for why the drop counter is a
        cumulative running total rather than a delta-since-last-report."""
        n_failures = 4
        code = f"""
import sys, resource, signal
sys.path.insert(0, {str(SRC_ENGRAM)!r})
import engram_telemetry_store as ts

ts.write_telemetry_record("m0", "engram.test.pre")

soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
resource.setrlimit(resource.RLIMIT_FSIZE, (5, hard))
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
try:
    for i in range({n_failures}):
        ok = ts.write_telemetry_record("m1", "engram.test.fail")
        assert ok is False, "expected failure under full-disk simulation"
finally:
    resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)

ok = ts.write_telemetry_record("m2", "engram.test.recover")
assert ok is True, "expected the recovery write to succeed"
# process now exits cleanly -- atexit should fire _finalize_process()
"""
        env = dict(os.environ)
        env["ENGRAM_HOME"] = self.tmp
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(
            result.returncode, 0, f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        self.assertIn(f"{n_failures} records dropped this process", result.stderr)

        # And the recovered record on disk (renamed to .done on clean exit)
        # shows the same total.
        done_files = glob.glob(str(self.telemetry_dir() / "*.jsonl.done"))
        self.assertEqual(len(done_files), 1, done_files)
        lines = [
            json.loads(line)
            for line in Path(done_files[0]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        recovered = [r for r in lines if r["event_type"] == "engram.test.recover"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["dropped_total"], n_failures)


# ---------------------------------------------------------------------------
# Rename-on-exit (subprocess -- observes atexit actually firing)
# ---------------------------------------------------------------------------


class TestRenameOnExit(TelemetryStoreTestBase):
    def test_clean_exit_renames_file_to_done_suffix(self):
        code = f"""
import sys
sys.path.insert(0, {str(SRC_ENGRAM)!r})
import engram_telemetry_store as ts
ok = ts.write_telemetry_record("m1", "engram.test.exit")
assert ok is True
"""
        env = dict(os.environ)
        env["ENGRAM_HOME"] = self.tmp
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(
            result.returncode, 0, f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        )

        files = self.telemetry_files()
        self.assertEqual(len(files), 1, files)
        self.assertTrue(files[0].name.endswith(".jsonl.done"), files[0].name)
        self.assertFalse(files[0].name.endswith(".jsonl"))

    def test_process_that_never_writes_leaves_no_file_and_no_error(self):
        code = f"""
import sys
sys.path.insert(0, {str(SRC_ENGRAM)!r})
import engram_telemetry_store as ts
# never call write_telemetry_record -- exit should be a clean no-op
"""
        env = dict(os.environ)
        env["ENGRAM_HOME"] = self.tmp
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.telemetry_files(), [])


# ---------------------------------------------------------------------------
# Ingest: truncated trailing line tolerance
# ---------------------------------------------------------------------------


class TestIngestTruncatedLine(TelemetryStoreTestBase):
    def setUp(self):
        super().setUp()
        self.telemetry_dir().mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.tmp) / "records.db"

    def _write_raw(self, path: Path, content: bytes) -> None:
        with open(path, "ab") as f:
            f.write(content)

    def test_truncated_trailing_line_is_tolerated_and_retried(self):
        path = self.telemetry_dir() / "111-1.jsonl"
        complete1 = json.dumps({"ts_utc": "2026-09-11T00:00:00.000Z", "manifest_id": "m1", "event_type": "e1", "fields": {}})
        complete2 = json.dumps({"ts_utc": "2026-09-11T00:00:01.000Z", "manifest_id": "m1", "event_type": "e2", "fields": {}})
        full3 = json.dumps(
            {"ts_utc": "2026-09-11T00:00:02.000Z", "manifest_id": "m1", "event_type": "e3", "session_id": None, "fields": {}}
        )
        # A genuine truncated trailing write: only the first half of the
        # THIRD record's bytes landed (no trailing \n at all yet) -- split
        # at a clean byte boundary of the real serialized record, not a
        # hand-typed partial string, so "completing" it later is just
        # writing the rest of the SAME bytes.
        split_at = len(full3) // 2
        truncated = full3[:split_at]
        rest = full3[split_at:] + "\n"

        self._write_raw(path, (complete1 + "\n" + complete2 + "\n" + truncated).encode("utf-8"))

        summary = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary["records_ingested"], 2)
        self.assertEqual(summary["truncated_lines"], 1)

        conn = ts.open_store(self.db_path)
        rows = conn.execute("SELECT event_type FROM l0_records ORDER BY id").fetchall()
        self.assertEqual([r["event_type"] for r in rows], ["e1", "e2"])

        offset_row = conn.execute(
            "SELECT byte_offset FROM ingest_offsets WHERE source_file = ?", (path.name,)
        ).fetchone()
        expected_offset = len((complete1 + "\n" + complete2 + "\n").encode("utf-8"))
        self.assertEqual(offset_row["byte_offset"], expected_offset)
        conn.close()

        # A second ingest with nothing new appended must not advance past
        # the truncated line, and must not re-ingest the two complete ones.
        summary2 = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary2["records_ingested"], 0)
        self.assertEqual(summary2["truncated_lines"], 1)

        # Complete the truncated line and append its trailing newline --
        # the next ingest must pick it up cleanly.
        self._write_raw(path, rest.encode("utf-8"))

        summary3 = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary3["records_ingested"], 1)
        self.assertEqual(summary3["truncated_lines"], 0)

        conn = ts.open_store(self.db_path)
        rows = conn.execute("SELECT event_type FROM l0_records ORDER BY id").fetchall()
        self.assertEqual([r["event_type"] for r in rows], ["e1", "e2", "e3"])
        conn.close()

    def test_drop_count_rows_are_counted_in_summary(self):
        path = self.telemetry_dir() / "222-1.jsonl"
        rec1 = json.dumps({"ts_utc": "2026-09-11T00:00:00.000Z", "manifest_id": "m1", "event_type": "e1", "fields": {}})
        rec2 = json.dumps(
            {
                "ts_utc": "2026-09-11T00:00:01.000Z",
                "manifest_id": "m1",
                "event_type": "e2",
                "fields": {},
                "dropped_total": 7,
            }
        )
        self._write_raw(path, (rec1 + "\n" + rec2 + "\n").encode("utf-8"))

        summary = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary["records_ingested"], 2)
        self.assertEqual(summary["drop_count_rows"], 1)

        conn = ts.open_store(self.db_path)
        row = conn.execute(
            "SELECT fields_json FROM l0_records WHERE event_type = 'e2'"
        ).fetchone()
        self.assertEqual(json.loads(row["fields_json"])["dropped_total"], 7)
        conn.close()


# ---------------------------------------------------------------------------
# Ingest sweep
# ---------------------------------------------------------------------------


class TestIngestSweep(TelemetryStoreTestBase):
    def setUp(self):
        super().setUp()
        self.telemetry_dir().mkdir(parents=True, exist_ok=True)
        self.db_path = Path(self.tmp) / "records.db"

    def _write_complete_file(self, path: Path, n: int = 1) -> None:
        lines = [
            json.dumps(
                {
                    "ts_utc": "2026-09-11T00:00:00.000Z",
                    "manifest_id": "m1",
                    "event_type": f"e{i}",
                    "fields": {},
                }
            )
            for i in range(n)
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_fully_ingested_done_file_is_deleted(self):
        path = self.telemetry_dir() / "111-1.jsonl.done"
        self._write_complete_file(path)

        summary = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary["records_ingested"], 1)
        self.assertEqual(summary["files_swept"], 1)
        self.assertFalse(path.exists())

    def test_partially_ingested_done_file_is_not_deleted(self):
        path = self.telemetry_dir() / "222-1.jsonl.done"
        complete = json.dumps({"ts_utc": "2026-09-11T00:00:00.000Z", "manifest_id": "m1", "event_type": "e1", "fields": {}})
        truncated = '{"ts_utc": "2026-09-11T00:00:01.000Z", "manifest_id": "m1"'  # no \n
        path.write_bytes((complete + "\n" + truncated).encode("utf-8"))

        summary = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary["records_ingested"], 1)
        self.assertEqual(summary["files_swept"], 0)
        self.assertTrue(path.exists(), "a .done file with unfinished content must not be swept")

    def test_stale_bare_jsonl_past_age_threshold_is_deleted_after_full_ingestion(self):
        path = self.telemetry_dir() / "333-1.jsonl"
        self._write_complete_file(path)
        # Backdate mtime well past the stale threshold.
        stale_time = time.time() - (ts._STALE_FILE_AGE_DAYS + 1) * ts._SECONDS_PER_DAY
        os.utime(path, (stale_time, stale_time))

        summary = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary["records_ingested"], 1)
        self.assertEqual(summary["files_swept"], 1)
        self.assertFalse(path.exists())

    def test_fresh_bare_jsonl_is_not_deleted_regardless_of_ingestion_state(self):
        path = self.telemetry_dir() / "444-1.jsonl"
        self._write_complete_file(path)
        # mtime stays fresh (just written) -- fully ingested but NOT stale.

        summary = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary["records_ingested"], 1)
        self.assertEqual(summary["files_swept"], 0)
        self.assertTrue(path.exists(), "a fresh-mtime bare .jsonl must never be swept, ingested or not")

        # Confirm it holds even though ingestion is complete for it -- run
        # ingest again (idempotent no-op re-scan) and it should still survive.
        summary2 = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary2["records_ingested"], 0)
        self.assertEqual(summary2["files_swept"], 0)
        self.assertTrue(path.exists())

    def test_stale_mtime_but_not_fully_ingested_bare_jsonl_is_not_deleted(self):
        path = self.telemetry_dir() / "555-1.jsonl"
        complete = json.dumps({"ts_utc": "2026-09-11T00:00:00.000Z", "manifest_id": "m1", "event_type": "e1", "fields": {}})
        truncated = '{"ts_utc": "2026-09-11T00:00:01.000Z"'  # no \n -- perpetually "still being written"
        path.write_bytes((complete + "\n" + truncated).encode("utf-8"))
        stale_time = time.time() - (ts._STALE_FILE_AGE_DAYS + 1) * ts._SECONDS_PER_DAY
        os.utime(path, (stale_time, stale_time))

        summary = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary["records_ingested"], 1)
        self.assertEqual(summary["files_swept"], 0)
        self.assertTrue(path.exists(), "never delete a file with unfinished content, however stale its mtime")

    def test_rename_to_done_between_two_ingest_calls_does_not_duplicate(self):
        """Round-1 review regression (#2847): a file the aggregator observes
        WHILE STILL OPEN (bare .jsonl) and observes AGAIN after the writer's
        clean-exit rename to .done must not be re-ingested -- offset
        tracking has to resolve to the SAME row across that rename, or every
        already-ingested byte gets silently re-inserted as a duplicate and
        then the .done file is swept, destroying the only evidence the
        count was ever wrong. This is the NORMAL lifecycle for any process
        the aggregator polls more than once before it exits, not an edge
        case -- every existing TestIngestSweep test above uses one static
        filename per test and never renames between two ingest() calls, so
        none of them discriminate on this."""
        bare = self.telemetry_dir() / "666-1.jsonl"
        self._write_complete_file(bare, n=2)

        summary1 = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(summary1["records_ingested"], 2)
        self.assertEqual(summary1["files_swept"], 0, "still bare + fresh mtime -- must not sweep yet")

        done = bare.with_name(bare.name + ".done")
        os.rename(bare, done)

        summary2 = ts.ingest(self.db_path, self.telemetry_dir())
        self.assertEqual(
            summary2["records_ingested"], 0,
            "the rename must not reset offset tracking and re-ingest already-seen bytes",
        )
        self.assertEqual(summary2["files_swept"], 1, "now .done and fully ingested -- safe to sweep")

        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM l0_records").fetchone()
        finally:
            conn.close()
        self.assertEqual(count, 2, "exactly the 2 records written, not 4")


if __name__ == "__main__":
    unittest.main()
