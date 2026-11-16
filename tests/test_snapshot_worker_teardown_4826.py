"""#4826: conftest's snapshot-worker teardown joins until the thread stops.

Before: the autouse fixture called ``stop_worker(timeout=2.0)`` once. A job still
writing into the store directory after 2 s kept running, and the next test's
``fresh_server()`` rmtree then failed with ``Directory not empty``.

Now: ``_stop_and_reset_snapshot_worker`` keeps joining (bounded), and is loud, not
fatal, if the thread will not stop.

Probe design note: the job must KEEP WRITING during the wait. A job that merely
SLEEPS is not a valid probe: it writes nothing while the next test's rmtree walks
the folder, so there is no overlap and the old one-join shape passes too.
"""

import os
import shutil
import sys
import threading
import time
import warnings

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from conftest import _stop_and_reset_snapshot_worker  # noqa: E402

import engram_snapshot_worker as w  # noqa: E402


def _reset_worker_state():
    w._started = False
    w._worker_thread = None
    w._seq_counter = 0
    w._work_event.clear()


@pytest.fixture
def writing_worker(tmp_path, monkeypatch):
    """A real worker whose job keeps writing small files into _data_dir().

    Yields (data_dir, release, started, writer). ``writer.seconds`` is how long the
    job writes; ``release`` ends it early. Teardown releases the writer and stops
    the worker, so no thread is left behind."""
    data_dir = tmp_path / "store"
    data_dir.mkdir()
    monkeypatch.setattr(w, "_data_dir", lambda: data_dir)
    monkeypatch.setitem(sys.modules, "engram_snapshot_worker", w)
    w.stop_worker(timeout=1.0)
    _reset_worker_state()

    release = threading.Event()
    started = threading.Event()

    def writer(job):
        started.set()
        end = time.monotonic() + writer.seconds
        i = 0
        while time.monotonic() < end and not release.is_set():
            (data_dir / f"churn-{i}.tmp").write_text("x")
            i += 1
            time.sleep(0.005)
        return True

    writer.seconds = 3.0
    # Every worker thread a cell starts, so teardown can join it even after the
    # helper under test has reset _worker_thread to None (reviewer r1 on #4831):
    # a cell whose assertion fails before its own cleanup must not leak a live
    # thread into later tests.
    writer.threads = []
    monkeypatch.setattr(w, "_process_job", writer)
    yield data_dir, release, started, writer
    release.set()
    w._stop.set()
    w._work_event.set()
    for t in writer.threads:
        t.join(timeout=10.0)
    w.stop_worker(timeout=10.0)
    _reset_worker_state()


def _run_one_job(started, writer):
    w.start_worker()
    writer.threads.append(w._worker_thread)
    w.enqueue_job(1, 1, "m", "nap")
    assert started.wait(timeout=10.0), "job never started"


def test_race_helper_waits_for_writing_job_then_rmtree_succeeds(writing_worker):
    """A job that keeps writing past the 2 s first join must be waited out, so the
    thread is gone and rmtree of the store succeeds (no ENOTEMPTY)."""
    data_dir, _release, started, writer = writing_worker
    _run_one_job(started, writer)
    t = w._worker_thread
    assert t is not None
    status = _stop_and_reset_snapshot_worker("race-cell")
    assert status == "stopped"
    assert not t.is_alive()
    shutil.rmtree(data_dir)
    assert not data_dir.exists()


def test_still_alive_past_bound_is_loud_not_fatal(writing_worker, capsys):
    """Writer outlasts a tiny bound: helper returns "still_alive", does not raise,
    prints one stderr line naming the nodeid and the bound, and warns."""
    data_dir, release, started, writer = writing_worker
    writer.seconds = 3.0
    _run_one_job(started, writer)
    t = w._worker_thread
    with pytest.warns(UserWarning, match="still alive"):
        status = _stop_and_reset_snapshot_worker(
            "tests/x.py::the_nodeid", first_join_s=0.2, bound_s=0.5
        )
    assert status == "still_alive"
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "tests/x.py::the_nodeid" in ln]
    assert len(lines) == 1
    assert "0.5" in lines[0]
    # Clean up: the orphan's token stays set (#4833: a stop is never undone), so it
    # exits after its job. End the writer, re-signal stop, join.
    release.set()
    w._stop.set()
    w._work_event.set()
    t.join(timeout=10.0)
    assert not t.is_alive()


def test_still_alive_under_warnings_as_errors_still_resets(writing_worker):
    """Reviewer r1 on #4831: under a `-W error` run the warning raises. The reset
    must already have happened by then (report after reset, outside the try), so
    the next test still starts from a clean singleton."""
    data_dir, release, started, writer = writing_worker
    _run_one_job(started, writer)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(UserWarning, match="still alive"):
            _stop_and_reset_snapshot_worker("tests/x.py::w_error", first_join_s=0.2, bound_s=0.5)
    assert w._worker_thread is None and w._started is False, "reset skipped under -W error"


def test_absent_module_returns_at_once(monkeypatch):
    monkeypatch.delitem(sys.modules, "engram_snapshot_worker", raising=False)
    t0 = time.monotonic()
    assert _stop_and_reset_snapshot_worker("absent-cell") == "absent"
    assert time.monotonic() - t0 < 0.05
