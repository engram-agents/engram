"""#4833: a stop given to a snapshot worker is never undone.

The defect: ``engram_snapshot_worker`` kept ONE module-level ``_stop`` Event shared by
every worker thread the process ever started. ``start_worker()`` (and the conftest
reset) CLEARED it. After a reset that returned "still_alive" (a thread mid-job past
the bound), the orphan was revived: it survived its job, and one ``_work_event.set()``
made both the orphan and the next worker drain the queue.

Contract pinned here: each worker thread has its own stop token (a ``threading.Event``)
that is only ever SET, never cleared. A later ``start_worker()`` makes a fresh token and
never touches an earlier worker's token. An orphan exits after at most the job it is in.

Cells:
  1. orphan exits after its job even when a new worker starts mid-job
  2. one wake reaches the new worker only
  3. guard: a concurrent duplicate of one REAL job leaves exactly one commit
  4. guard: stop_worker is loud when the thread outlives the join
  5. guard: plain start/stop, and start twice leaves one worker
  6. guard: no test or src file clears a worker token by a known spelling (tripwire)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
sys.path.append(_TESTS_DIR)
sys.path.append(os.path.join(_REPO_ROOT, "src", "engram"))
from conftest import _stop_and_reset_snapshot_worker  # noqa: E402

import engram_snapshot_worker as w  # noqa: E402

JOB_S = 2.0
WORKER_NAME = "engram-snapshot-worker"


def _worker_threads():
    return [t for t in threading.enumerate() if t.name == WORKER_NAME]


class _Started:
    """Records every (thread, token) a cell starts, so cleanup sets THAT thread's own
    token (never relying on the module attribute, which is rebound per start)."""

    def __init__(self):
        self.threads = []
        self.tokens = []

    def start(self):
        w.start_worker()
        self.threads.append(w._worker_thread)
        self.tokens.append(w._stop)
        return w._worker_thread

    def shutdown(self):
        for tok in self.tokens:
            tok.set()
        w._work_event.set()
        for t in self.threads:
            if t is not None:
                t.join(timeout=JOB_S + 5.0)


@pytest.fixture
def rig(monkeypatch):
    """Slow-first-job rig. ``rig.calls`` holds (thread name, ident) per drain call;
    the FIRST drain on a worker thread sleeps JOB_S (a slow job)."""
    monkeypatch.setitem(sys.modules, "engram_snapshot_worker", w)
    w.stop_worker(timeout=1.0)
    w._started = False
    w._worker_thread = None
    w._seq_counter = 0
    w._work_event.clear()

    calls = []
    first = {"done": False}

    def drain(stop=None):
        me = threading.current_thread()
        calls.append((me.name, me.ident))
        if me.name == WORKER_NAME and not first["done"]:
            first["done"] = True
            time.sleep(JOB_S)
        return 0

    for name in ("_clear_stale_index_lock", "_seed_seq_counter",
                 "_reclaim_orphan_snapshots", "_compact_queue_locked"):
        monkeypatch.setattr(w, name, lambda *a, **k: 0)
    monkeypatch.setattr(w, "_read_queue_lines", lambda: [])
    monkeypatch.setattr(w, "_drain_pending", drain)
    started = _Started()
    started.calls = calls
    yield started
    started.shutdown()


def _orphan_after_still_alive_reset(rig):
    """Start a worker, put it mid-job, reset with a tiny bound (-> still_alive), then
    start the next worker while the orphan is still in its job."""
    old = rig.start()
    w._work_event.set()          # worker enters the slow job
    time.sleep(0.2)
    status = _stop_and_reset_snapshot_worker("cell-4833", first_join_s=0.1, bound_s=0.3)
    assert status == "still_alive", f"rig: expected still_alive, got {status}"
    new = rig.start()
    assert new is not old
    return old, new


# ── Cell 1 ─────────────────────────────────────────────────────────────────
def test_orphan_exits_after_its_job_when_next_worker_starts_mid_job(rig):
    with pytest.warns(UserWarning, match="still alive"):
        old, new = _orphan_after_still_alive_reset(rig)
    old.join(timeout=JOB_S + 1.0)
    assert not old.is_alive(), "orphan revived: it outlived its job after a stop (#4833)"
    assert new.is_alive()


# ── Cell 2 ─────────────────────────────────────────────────────────────────
def test_one_wake_reaches_only_the_new_worker(rig):
    with pytest.warns(UserWarning, match="still alive"):
        old, new = _orphan_after_still_alive_reset(rig)
    time.sleep(JOB_S + 0.5)          # let the orphan's slow job finish
    n0 = len(rig.calls)
    old_ident = old.ident
    new_ident = new.ident
    w._work_event.set()
    time.sleep(0.5)
    idents = [i for _n, i in rig.calls[n0:]]
    assert old_ident not in idents, "orphan drained after a wake (#4833)"
    assert idents and all(i == new_ident for i in idents), idents


# ── Cell 3 ─────────────────────────────────────────────────────────────────
def _fresh(tag):
    d = tempfile.mkdtemp(prefix=f"orph4833-{tag}-")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    for a in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
              ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)
    return server, d


def _make_job(server):
    import engram_snapshot_worker as wk
    real = wk.start_worker
    wk.start_worker = lambda: None          # keep the job pending; no worker thread
    try:
        r = json.loads(server._nap_impl(message="overlap probe"))
    finally:
        wk.start_worker = real
    jobs = wk._pending_jobs()
    assert len(jobs) == 1, f"rig: expected one pending job, got {jobs} / nap {r}"
    snap = wk._pending_dir() / jobs[0].get("snapshot", "")
    assert snap.exists(), f"rig: snapshot missing {snap}"
    return wk, jobs[0], snap


def test_concurrent_duplicate_of_one_real_job_commits_once(monkeypatch):
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    # Importing `server` sets env vars at import time (e.g. HF_HUB_ETAG_TIMEOUT);
    # put the whole environment back so nothing leaks into later modules.
    env_before = dict(os.environ)
    path_before = list(sys.path)
    mods_before = {k: v for k, v in sys.modules.items()
                   if k == "server" or k.startswith("engram_")}
    sys.path.insert(0, _REPO_ROOT)
    dirs = []
    try:
        for trial in range(3):
            server, d = _fresh(f"t{trial}")
            dirs.append(d)
            wk, job, snap = _make_job(server)
            git = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)
            before = int(git("rev-list", "--count", "HEAD").stdout.strip())
            bar = threading.Barrier(2)
            real_commit = wk._commit_snapshot_files

            def commit(mode, msg, _real=real_commit, _bar=bar):
                _bar.wait(timeout=10)
                return _real(mode, msg)

            wk._commit_snapshot_files = commit
            try:
                ts = [threading.Thread(target=wk._process_job, args=(job,)) for _ in (0, 1)]
                [t.start() for t in ts]
                [t.join(30) for t in ts]
                assert not any(t.is_alive() for t in ts), "rig: a process thread hung"
            finally:
                wk._commit_snapshot_files = real_commit
            after = int(git("rev-list", "--count", "HEAD").stdout.strip())
            assert after - before == 1, f"trial {trial}: {after - before} new commits"
            assert wk._pending_jobs() == [], f"trial {trial}: queue not drained"
            assert not os.path.exists(os.path.join(d, ".git", "index.lock"))
            assert not snap.exists(), f"trial {trial}: snapshot file left"
    finally:
        for k in [k for k in sys.modules if k == "server" or k.startswith("engram_")]:
            del sys.modules[k]
        sys.modules.update(mods_before)
        sys.path[:] = path_before
        os.environ.clear()
        os.environ.update(env_before)
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


# ── Cell 4 ─────────────────────────────────────────────────────────────────
def test_stop_worker_is_loud_when_thread_outlives_the_join(rig, capsys):
    release = threading.Event()
    t = threading.Thread(target=release.wait, name=WORKER_NAME, daemon=True)
    t.start()
    w._worker_thread = t
    try:
        w.stop_worker(timeout=0.2)
        err = capsys.readouterr().err
        lines = [ln for ln in err.splitlines() if "still alive" in ln]
        assert len(lines) == 1, err
        assert "0.2" in lines[0]
    finally:
        release.set()
        t.join(timeout=5.0)
        w._worker_thread = None


def test_stop_worker_is_silent_on_a_clean_stop(rig, capsys):
    rig.start()
    w.stop_worker(timeout=2.0)
    err = capsys.readouterr().err
    assert "still alive" not in err, err


# ── Cell 5 ─────────────────────────────────────────────────────────────────
def test_start_then_stop_leaves_no_worker(rig):
    t = rig.start()
    assert t.is_alive()
    w.stop_worker(timeout=2.0)
    assert not t.is_alive()


def test_start_twice_without_stop_leaves_exactly_one_worker(rig):
    baseline = len(_worker_threads())
    first = rig.start()
    second = rig.start()
    assert second is first
    assert len(_worker_threads()) - baseline == 1


# ── Cell 6 (tripwire for known spellings) ──────────────────────────────────
# Matches `<alias>._stop.clear(` and a bare `_stop.clear(`, but not `self._stop`
# (other classes' own flags). It cannot see `getattr(w, "_stop").clear()` or a token
# first bound to another name; the contract itself is pinned by cells 1 and 2.
_CLEAR_RE = re.compile(r"(?<!self\.)(?<!\w)_stop\s*\.\s*clear\s*\(")


def test_no_test_or_src_file_clears_a_worker_token():
    # Planted-match self-check: the pattern must match before an empty scan counts.
    planted = "_sw" + "._stop" + ".clear()"
    assert _CLEAR_RE.search(planted)
    assert _CLEAR_RE.search("_stop" + ".clear()")
    assert not _CLEAR_RE.search("self" + "._stop" + ".clear()")

    this = Path(__file__).resolve()
    roots = [Path(_REPO_ROOT) / "tests", Path(_REPO_ROOT) / "src"]
    scanned = 0
    hits = []
    for root in roots:
        for p in root.rglob("*.py"):
            if p.resolve() == this:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for n, line in enumerate(text.splitlines(), 1):
                if _CLEAR_RE.search(line):
                    hits.append(f"{p.relative_to(_REPO_ROOT)}:{n}: {line.strip()}")
    assert scanned > 100, f"scan looked at only {scanned} files"
    assert hits == [], "a stop token must never be cleared (#4833):\n" + "\n".join(hits)
