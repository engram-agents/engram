"""Tests for engram_snapshot_worker (#1673 async snapshot).

Covers the novel, durability-critical surface in isolation: the append-only
durable queue (enqueue / pending / FIFO / compaction / malformed-line tolerance),
per-process seq monotonicity + disk-seed (rider b: nap-within-turn collisions),
the R3 snapshot_lag signal, stale .git/index.lock clearing (rider a), the sync
write_durable_snapshot capture, and _process_job's orchestration (success →
done+unlink, commit-fail → stays pending, missing-snapshot → retired).

The real git-commit + dump_stripped + _generate_snapshot integration is validated
on-branch against the live code path (increment 4), which is more faithful than a
synthetic mini-DB; here those two helpers are monkeypatched so _process_job's
control flow is tested directly.
"""

import json
import os
import sqlite3
import sys
import time

import pytest

# #2565: append (not insert(0)) -- putting src/engram at position 0
# outranks the repo-root tools/ package for the rest of the process,
# a live instance of the tools/-package-name-clash class found while
# building #2565's durable guard (tests/conftest.py).
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))
import engram_snapshot_worker as w  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Point the worker at a fresh tmp DATA_DIR and reset module state per test."""
    monkeypatch.setattr(w, "_data_dir", lambda: tmp_path)
    # Reset singleton state (module-global across tests).
    w.stop_worker(timeout=1.0)
    w._seq_counter = 0
    w._started = False
    w._worker_thread = None
    w._stop.clear()
    w._work_event.clear()
    yield tmp_path
    w.stop_worker(timeout=1.0)


# ── durable queue ──────────────────────────────────────────────────────────
def test_enqueue_appears_pending_then_clears_on_done():
    assert w._pending_jobs() == []
    w.enqueue_job(10, 1, "advance to 10", "sleep")
    pending = w._pending_jobs()
    assert len(pending) == 1
    assert (pending[0]["turn"], pending[0]["seq"]) == (10, 1)
    assert pending[0]["snapshot"] == "snapshot-10-1.db"
    w._mark_done(10, 1)
    assert w._pending_jobs() == []


def test_queue_fsync_writes_a_real_file():
    w.enqueue_job(5, 3, "m", "nap")
    qp = w._queue_path()
    assert qp.exists()
    line = json.loads(qp.read_text().splitlines()[0])
    assert line["type"] == "job" and line["turn"] == 5 and line["seq"] == 3


def test_pending_is_fifo_order():
    for t in (7, 8, 9):
        w.enqueue_job(t, 1, f"m{t}", "nap")
    turns = [j["turn"] for j in w._pending_jobs()]
    assert turns == [7, 8, 9]


def test_malformed_line_is_skipped_not_fatal():
    w.enqueue_job(1, 1, "m", "nap")
    with open(w._queue_path(), "a", encoding="utf-8") as f:
        f.write("this is not json\n")
    w.enqueue_job(2, 1, "m", "nap")
    pending = w._pending_jobs()
    assert [j["turn"] for j in pending] == [1, 2]


def test_compaction_drops_done_pairs_keeps_pending():
    for t in (1, 2, 3):
        w.enqueue_job(t, 1, "m", "nap")
    w._mark_done(1, 1)
    w._mark_done(2, 1)
    lines_before = len(w._queue_path().read_text().splitlines())
    assert lines_before == 5  # 3 jobs + 2 done
    with w._lock:
        w._compact_queue_locked()
    lines_after = w._queue_path().read_text().splitlines()
    assert len(lines_after) == 1
    remaining = json.loads(lines_after[0])
    assert remaining["turn"] == 3 and remaining["type"] == "job"
    # still pending after compaction
    assert [j["turn"] for j in w._pending_jobs()] == [3]


def test_compaction_noop_when_all_pending():
    w.enqueue_job(1, 1, "m", "nap")
    with w._lock:
        w._compact_queue_locked()
    assert len(w._queue_path().read_text().splitlines()) == 1


# ── seq monotonicity + disk seed (rider b) ─────────────────────────────────
def test_next_seq_is_monotonic():
    assert [w.next_seq() for _ in range(3)] == [1, 2, 3]


def test_seq_seeds_above_leftover_snapshot_files():
    # Simulate a crashed prior process that left snapshot-4-7.db behind.
    pending = w._pending_dir()
    (pending / "snapshot-4-7.db").write_bytes(b"x")
    w.enqueue_job(4, 5, "m", "nap")  # a pending job at seq 5
    w._seed_seq_counter()
    # Next seq must exceed BOTH the pending job seq (5) and the file seq (7).
    assert w.next_seq() == 8


# ── R3 snapshot_lag ────────────────────────────────────────────────────────
def test_lag_zero_when_empty():
    lag = w.compute_snapshot_lag()
    assert lag["behind_turns"] == 0
    assert lag["oldest_pending_turn"] is None
    assert "warning" not in lag


def test_lag_counts_distinct_turns_and_warns():
    w.enqueue_job(10, 1, "m", "nap")
    w.enqueue_job(10, 2, "m", "nap")  # same turn, second nap → not a new turn
    w.enqueue_job(11, 1, "m", "sleep")
    lag = w.compute_snapshot_lag()
    assert lag["behind_turns"] == 2  # turns {10, 11}
    assert lag["oldest_pending_turn"] == 10
    assert lag["pending_jobs"] == 3
    assert "warning" in lag  # >= _LAG_WARN_TURNS and worker not alive


def test_lag_warns_when_worker_dead_with_backlog():
    w.enqueue_job(1, 1, "m", "nap")  # single turn, below turn-threshold
    lag = w.compute_snapshot_lag()
    # worker_alive False + pending → warning even at 1 turn
    assert lag["worker_alive"] is False
    assert "warning" in lag


# ── stale .git/index.lock (rider a) ────────────────────────────────────────
def _make_index_lock(tmp_path, content: str):
    gitdir = tmp_path / ".git"
    gitdir.mkdir(exist_ok=True)
    lock = gitdir / "index.lock"
    lock.write_text(content)
    return lock


def test_index_lock_removed_when_old_regardless_of_content(_isolated):
    # index.lock carries NO owner pid (git never writes one); detection is age-based.
    # An old lock is an abandoned orphan and is removed whatever bytes it holds.
    lock = _make_index_lock(_isolated, "999999")  # pid-looking content is ignored now
    old = time.time() - (w._INDEX_LOCK_STALE_SECS + 10)
    os.utime(lock, (old, old))
    w._clear_stale_index_lock()
    assert not lock.exists()


def test_index_lock_kept_when_fresh_regardless_of_content(_isolated):
    # A fresh lock might belong to a genuinely in-flight git op → conservatively kept
    # (git's own O_EXCL lock prevents corruption; the stall self-heals + shows in lag).
    lock = _make_index_lock(_isolated, str(os.getpid()))  # content irrelevant
    w._clear_stale_index_lock()
    assert lock.exists()


def test_index_lock_removed_when_old_and_no_pid(_isolated):
    lock = _make_index_lock(_isolated, "")  # no pid recorded
    old = time.time() - (w._INDEX_LOCK_STALE_SECS + 10)
    os.utime(lock, (old, old))
    w._clear_stale_index_lock()
    assert not lock.exists()


def test_index_lock_kept_when_fresh_and_no_pid(_isolated):
    lock = _make_index_lock(_isolated, "")  # no pid, fresh mtime
    w._clear_stale_index_lock()
    assert lock.exists()


def test_clear_index_lock_noop_when_absent(_isolated):
    (_isolated / ".git").mkdir(exist_ok=True)
    w._clear_stale_index_lock()  # must not raise


# ── write_durable_snapshot (sync capture) ──────────────────────────────────
def test_write_durable_snapshot_creates_fsynced_immutable_file(_isolated):
    src = _isolated / "live.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    try:
        res = w.write_durable_snapshot(conn, turn=12, seq=1)
    finally:
        conn.close()
    assert res.get("fsynced") is True
    snap = _isolated / "db-backup" / ".pending" / "snapshot-12-1.db"
    assert snap.exists() and res["path"] == str(snap)
    assert res["bytes"] > 0
    # the captured copy is a real, queryable, independent DB
    c2 = sqlite3.connect(str(snap))
    assert c2.execute("SELECT v FROM t").fetchone()[0] == "hello"
    c2.close()
    # no leftover tmp
    assert not (snap.parent / "snapshot-12-1.db.tmp").exists()


# ── _process_job orchestration (helpers monkeypatched) ─────────────────────
def _stub_ok_helpers(monkeypatch, commit_result):
    monkeypatch.setattr(w, "_regenerate_derived_files", lambda p: None)
    monkeypatch.setattr(w, "_commit_snapshot_files", lambda mode, msg: commit_result)


def test_process_job_success_marks_done_and_unlinks(monkeypatch, _isolated):
    snap = w._pending_dir() / "snapshot-3-1.db"
    snap.write_bytes(b"binary")
    w.enqueue_job(3, 1, "m", "sleep")
    _stub_ok_helpers(monkeypatch, {"git_committed": True, "commit_sha": "abc"})
    job = w._pending_jobs()[0]
    assert w._process_job(job) is True
    assert not snap.exists()              # consumed
    assert w._pending_jobs() == []        # marked done


def test_process_job_commit_failure_leaves_pending(monkeypatch, _isolated):
    snap = w._pending_dir() / "snapshot-3-1.db"
    snap.write_bytes(b"binary")
    w.enqueue_job(3, 1, "m", "sleep")
    _stub_ok_helpers(monkeypatch, {"git_committed": False, "reason": "git commit failed: boom"})
    job = w._pending_jobs()[0]
    assert w._process_job(job) is False
    assert snap.exists()                  # NOT consumed
    assert len(w._pending_jobs()) == 1    # still pending → will retry


def test_process_job_benign_noop_marks_done(monkeypatch, _isolated):
    snap = w._pending_dir() / "snapshot-3-1.db"
    snap.write_bytes(b"binary")
    w.enqueue_job(3, 1, "m", "sleep")
    _stub_ok_helpers(monkeypatch, {"git_committed": False, "reason": "no changes since last commit"})
    job = w._pending_jobs()[0]
    assert w._process_job(job) is True    # nothing-to-commit is success-equivalent
    assert w._pending_jobs() == []


def test_process_job_missing_snapshot_is_retired(monkeypatch, _isolated):
    w.enqueue_job(9, 1, "m", "sleep")     # enqueue but never create the file
    _stub_ok_helpers(monkeypatch, {"git_committed": True})
    job = w._pending_jobs()[0]
    assert w._process_job(job) is False    # couldn't process
    assert w._pending_jobs() == []         # but retired so the lane isn't wedged


def test_drain_stops_on_failure_preserving_order(monkeypatch, _isolated):
    for t in (1, 2, 3):
        (w._pending_dir() / f"snapshot-{t}-1.db").write_bytes(b"x")
        w.enqueue_job(t, 1, "m", "nap")
    # Fail on turn 2 → drain should stop, leaving 2 and 3 pending.
    def commit(mode, msg):
        return {"git_committed": False, "reason": "git commit failed"} if _current["turn"] == 2 \
            else {"git_committed": True}
    _current = {"turn": None}
    def regen(p):
        # p is snapshot-<turn>-1.db
        _current["turn"] = int(p.stem.split("-")[1])
    monkeypatch.setattr(w, "_regenerate_derived_files", regen)
    monkeypatch.setattr(w, "_commit_snapshot_files", commit)
    done = w._drain_pending()
    assert done == 1                       # only turn 1 committed
    assert [j["turn"] for j in w._pending_jobs()] == [2, 3]


# ── startup replay (server-boot wiring, increment 3) ───────────────────────
def test_start_worker_replays_pending_on_startup(monkeypatch, _isolated):
    """A job left pending by a prior crash (enqueued, snapshot on disk, never
    committed) is replayed on start_worker() BEFORE the thread serves — the
    crash-safety the server-boot wiring provides. start_worker drains synchronously
    before starting the thread, so the replay is observable on return."""
    snap = w._pending_dir() / "snapshot-5-1.db"
    snap.write_bytes(b"binarydata")
    w.enqueue_job(5, 1, "pre-crash job", "sleep")
    assert len(w._pending_jobs()) == 1
    # Stub the git/regen helpers so replay does no real git.
    monkeypatch.setattr(w, "_regenerate_derived_files", lambda p: None)
    monkeypatch.setattr(w, "_commit_snapshot_files", lambda mode, msg: {"git_committed": True})
    w.start_worker()
    try:
        assert w._pending_jobs() == [], "startup did not replay the pending job"
        assert not snap.exists(), "replayed snapshot not consumed"
        assert w._worker_thread is not None and w._worker_thread.is_alive()
    finally:
        w.stop_worker(timeout=2.0)


def test_reclaim_orphan_snapshots_deletes_unreferenced(_isolated):
    """A snapshot on disk with NO pending job (crash between capture and enqueue) is
    reclaimed; a snapshot referenced by a pending job is kept for replay (R4)."""
    orphan = w._pending_dir() / "snapshot-3-1.db"
    orphan.write_bytes(b"orphan")
    (w._pending_dir() / "snapshot-3-1.db-wal").write_bytes(b"wal")  # sidecar too
    kept = w._pending_dir() / "snapshot-4-1.db"
    kept.write_bytes(b"kept")
    w.enqueue_job(4, 1, "referenced", "nap")

    n = w._reclaim_orphan_snapshots()
    assert n == 1
    assert not orphan.exists()
    assert not (w._pending_dir() / "snapshot-3-1.db-wal").exists()  # sidecar reclaimed
    assert kept.exists()                                            # pending → kept


def test_reclaim_orphan_snapshots_noop_when_all_referenced(_isolated):
    (w._pending_dir() / "snapshot-9-1.db").write_bytes(b"x")
    w.enqueue_job(9, 1, "m", "nap")
    assert w._reclaim_orphan_snapshots() == 0
    assert (w._pending_dir() / "snapshot-9-1.db").exists()
