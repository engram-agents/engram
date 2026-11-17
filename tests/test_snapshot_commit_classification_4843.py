"""#4843 classification half: a snapshot job is retired without a commit only when
committing is impossible for this store or there is nothing to commit.

The defect: ``_process_job`` kept a job pending only when the reason started with
``git commit failed``. A held ``.git/index.lock`` makes ``git add`` fail; that reason
fell through to "benign", so the job was marked done, its snapshot deleted, and the
turn's commit lost with no log line.

Design ruling (Kepler, issue comments 6950306886 and 6950441690): an ALLOW-LIST of
benign outcomes; every other failure leaves the job pending with its snapshot kept;
no count-based retirement; one log line per job per failure reason.

Allow-list (each match narrow, read from the real text):
  committed | "no changes since last commit" | "no tracked files exist" |
  "all stageable files are gitignored" |
  S1 "git add failed: fatal: not a git repository ..." (git 2.43.0 stderr) |
  S2 "git add failed: [Errno 2] ... '<GIT_EXE>'" (engram_core._git turns
     FileNotFoundError into stderr=str(e))

Cells:
  C1 index.lock held by another process -> pending, snapshot kept, log names it
  C2 no repository -> retired (guard)          C2b no git binary -> retired
  C2c FileNotFoundError naming another path -> pending
  C3 git add timeout -> pending + log          C4 unknown add error -> pending + log
  C5 lock released -> next drain commits once, retires
  C6 nothing to commit (three reasons) -> retired (guard)
  C7 git commit failed -> pending (regression pin)
  C8 one log line per job per reason, across drains
  C9 the add-failed shape is visible to the backlog reader
"""

import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "engram"


# ── A real store per cell (the #4833 cell-3 pattern; everything restored) ──
@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    env_before = dict(os.environ)
    path_before = list(sys.path)
    mods_before = {k: v for k, v in sys.modules.items() if k == "server" or k.startswith("engram_")}
    d = tempfile.mkdtemp(prefix="cls4843-")
    try:
        for k in [k for k in sys.modules if k == "server" or k.startswith("engram_")]:
            del sys.modules[k]
        sys.path.insert(0, str(_REPO_ROOT))
        if str(_SRC) not in sys.path:
            sys.path.append(str(_SRC))
        import server
        server._configure_paths(d)
        server._ensure_data_dir()
        for a in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                  ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)
        yield server, Path(d)
    finally:
        for k in [k for k in sys.modules if k == "server" or k.startswith("engram_")]:
            del sys.modules[k]
        sys.modules.update(mods_before)
        sys.path[:] = path_before
        os.environ.clear()
        os.environ.update(env_before)
        shutil.rmtree(d, ignore_errors=True)


def _make_job(server):
    """One real pending job from a real nap; no worker thread runs."""
    import engram_snapshot_worker as wk
    real = wk.start_worker
    wk.start_worker = lambda: None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            server._nap_impl(message="classification probe")
    finally:
        wk.start_worker = real
    jobs = wk._pending_jobs()
    assert len(jobs) == 1, f"rig: expected one pending job, got {jobs}"
    snap = wk._pending_dir() / jobs[0]["snapshot"]
    assert snap.exists(), "rig: snapshot missing"
    return wk, jobs[0], snap


def _commits(d: Path) -> int:
    r = subprocess.run(["git", "-C", str(d), "rev-list", "--count", "HEAD"],
                       capture_output=True, text=True, check=True)
    return int(r.stdout.strip())


def _process(wk, job):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        ok = wk._process_job(job)
    lines = [l for l in buf.getvalue().splitlines() if l.startswith("[engram snapshot-worker]")]
    return ok, lines


def _drain(wk):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        wk._drain_pending()
    return [l for l in buf.getvalue().splitlines() if l.startswith("[engram snapshot-worker]")]


@contextlib.contextmanager
def _lock_held_by_other_process(d: Path):
    """Another OS process creates .git/index.lock and holds it open until released."""
    lock = d / ".git" / "index.lock"
    ready = d / ".lock-ready"
    p = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time;"
         "fd=os.open(sys.argv[1], os.O_CREAT|os.O_EXCL|os.O_WRONLY);"
         "open(sys.argv[2],'w').close();"
         "time.sleep(120)", str(lock), str(ready)],
    )
    try:
        t0 = time.time()
        while not ready.exists():
            assert time.time() - t0 < 20, "rig: lock holder did not start"
            time.sleep(0.05)
        assert lock.exists()
        yield lock
    finally:
        p.kill()
        p.wait(timeout=10)
        lock.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)


def _stub_add(monkeypatch, wk, returncode, stderr):
    """Make only `git add` fail with the given shape; every other git call is real."""
    import engram_core
    real_git = engram_core._git

    def fake(*args, **kw):
        if args and args[0] == "add":
            r = subprocess.CompletedProcess(args=[engram_core.GIT_EXE, *args], returncode=returncode)
            r.stdout, r.stderr = "", stderr
            return r
        return real_git(*args, **kw)

    monkeypatch.setattr(engram_core, "_git", fake)


def _stub_commit_result(monkeypatch, wk, reason):
    monkeypatch.setattr(wk, "_commit_snapshot_files",
                        lambda mode, msg: {"git_committed": False, "reason": reason})


def _assert_pending(wk, job, snap, ok):
    assert ok is False, "_process_job must report the job as not done"
    keys = {(j["turn"], j["seq"]) for j in wk._pending_jobs()}
    assert (job["turn"], job["seq"]) in keys, "job must stay pending"
    assert snap.exists(), "the snapshot must be kept for the retry"


def _assert_retired(wk, job, snap, ok):
    keys = {(j["turn"], j["seq"]) for j in wk._pending_jobs()}
    assert (job["turn"], job["seq"]) not in keys, "job must be retired"
    assert not snap.exists(), "a retired job's snapshot is reclaimed"


# ── C1 ──────────────────────────────────────────────────────────────────────
def test_c1_index_lock_held_by_another_process_keeps_job_pending(store):
    server, d = store
    wk, job, snap = _make_job(server)
    before = _commits(d)
    with _lock_held_by_other_process(d):
        ok, lines = _process(wk, job)
    _assert_pending(wk, job, snap, ok)
    assert _commits(d) == before
    assert len(lines) == 1 and "index.lock" in lines[0], lines


# ── C2 / C2b / C2c ──────────────────────────────────────────────────────────
def test_c2_no_repository_is_retired(store):
    server, d = store
    wk, job, snap = _make_job(server)
    shutil.rmtree(d / ".git")
    ok, _ = _process(wk, job)
    assert ok is True
    _assert_retired(wk, job, snap, ok)


def test_c2b_git_binary_missing_is_retired(store, monkeypatch):
    server, d = store
    wk, job, snap = _make_job(server)
    import engram_core
    monkeypatch.setattr(engram_core, "GIT_EXE", "/nonexistent-4843/git")
    ok, _ = _process(wk, job)
    assert ok is True
    _assert_retired(wk, job, snap, ok)


def test_c2c_file_not_found_naming_another_path_is_pending(store, monkeypatch):
    server, d = store
    wk, job, snap = _make_job(server)
    _stub_add(monkeypatch, wk, 1, "[Errno 2] No such file or directory: '/some/other/path'")
    ok, lines = _process(wk, job)
    _assert_pending(wk, job, snap, ok)
    assert len(lines) == 1


# ── C3 / C4 ─────────────────────────────────────────────────────────────────
def test_c3_git_add_timeout_is_pending_and_logged(store, monkeypatch):
    server, d = store
    wk, job, snap = _make_job(server)
    import engram_core
    timeout_text = str(subprocess.TimeoutExpired([engram_core.GIT_EXE, "add"], 30))
    _stub_add(monkeypatch, wk, 1, timeout_text)
    ok, lines = _process(wk, job)
    _assert_pending(wk, job, snap, ok)
    assert len(lines) == 1 and "timed out" in lines[0], lines


def test_c4_unknown_add_error_is_pending_and_logged(store, monkeypatch):
    """Pins the allow-list: a deny-list implementation retires this job."""
    server, d = store
    wk, job, snap = _make_job(server)
    _stub_add(monkeypatch, wk, 1, "boom")
    ok, lines = _process(wk, job)
    _assert_pending(wk, job, snap, ok)
    assert len(lines) == 1 and "boom" in lines[0], lines


# ── C5 ──────────────────────────────────────────────────────────────────────
def test_c5_lock_released_next_drain_commits_once_and_retires(store):
    server, d = store
    wk, job, snap = _make_job(server)
    before = _commits(d)
    with _lock_held_by_other_process(d):
        _drain(wk)
    assert len(wk._pending_jobs()) == 1, "while the lock is held the job stays pending"
    _drain(wk)
    assert _commits(d) == before + 1
    assert wk._pending_jobs() == []
    assert not snap.exists()
    _drain(wk)
    assert _commits(d) == before + 1, "a retired job is never committed twice"


# ── C6 ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("reason", [
    "no changes since last commit",
    "no tracked files exist",
    "all stageable files are gitignored",
])
def test_c6_nothing_to_commit_is_retired(store, monkeypatch, reason):
    server, d = store
    wk, job, snap = _make_job(server)
    _stub_commit_result(monkeypatch, wk, reason)
    ok, _ = _process(wk, job)
    assert ok is True
    _assert_retired(wk, job, snap, ok)


# ── C7 ──────────────────────────────────────────────────────────────────────
def test_c7_git_commit_failed_stays_pending(store, monkeypatch):
    server, d = store
    wk, job, snap = _make_job(server)
    _stub_commit_result(monkeypatch, wk, "git commit failed: hook refused")
    ok, lines = _process(wk, job)
    _assert_pending(wk, job, snap, ok)
    assert len(lines) == 1 and "git commit failed" in lines[0]


# ── C8 ──────────────────────────────────────────────────────────────────────
def test_c8_one_log_line_per_job_per_reason(store, monkeypatch):
    server, d = store
    wk, job, snap = _make_job(server)
    with _lock_held_by_other_process(d):
        first = _drain(wk)
        second = _drain(wk)
        third = _drain(wk)
    lock_lines = [l for l in first + second + third if "index.lock" in l]
    assert len(lock_lines) == 1, f"one line for the held lock across three drains: {first, second, third}"
    _stub_add(monkeypatch, wk, 1, "boom")
    fourth = _drain(wk)
    fifth = _drain(wk)
    assert len([l for l in fourth if "boom" in l]) == 1, "a different reason logs again"
    assert not [l for l in fifth if "boom" in l], "and only once"


# ── C9 ──────────────────────────────────────────────────────────────────────
def test_c9_add_failed_shape_is_visible_to_the_backlog_reader(store):
    server, d = store
    wk, job, snap = _make_job(server)
    with _lock_held_by_other_process(d):
        _drain(wk)
    sys.modules.pop("engram_snapshot_backlog", None)
    bl = importlib.import_module("engram_snapshot_backlog")
    r = bl.read_backlog(str(d), now=time.time() + bl.BACKLOG_THRESHOLD_SECS + 1)
    assert r["status"] == "backlog" and r["pending"] == 1, r
    assert r["bytes"] == snap.stat().st_size


# ── Review fold (Halley's review of 641d78a6) ──────────────────────────────
def test_c10_an_exception_is_logged_once_per_job_too(store, monkeypatch):
    """Kepler's rule is one line per job per failure reason, not per drain; a job whose
    processing raises is retried every wake like any other pending job."""
    server, d = store
    wk, job, snap = _make_job(server)

    def boom(_):
        raise RuntimeError("regen exploded")

    monkeypatch.setattr(wk, "_regenerate_derived_files", boom)
    lines = _drain(wk) + _drain(wk) + _drain(wk)
    hits = [l for l in lines if "regen exploded" in l]
    assert len(hits) == 1, lines
    assert snap.exists() and len(wk._pending_jobs()) == 1


# ── Review fold (reviewer round 1 question, measured by Halley) ────────────
@pytest.mark.parametrize("damage", ["head_garbage", "head_missing", "objects_missing"])
def test_c2d_damaged_repository_is_pending_not_retired(store, damage):
    """A damaged repo prints the same `fatal: not a git repository` text as no repo
    (git 2.43.0: garbage, empty or missing .git/HEAD, missing .git/objects). It can be
    repaired and then commit, so its job must stay pending; only a store with no .git
    at all is the benign S1 case."""
    server, d = store
    wk, job, snap = _make_job(server)
    git_dir = d / ".git"
    if damage == "head_garbage":
        (git_dir / "HEAD").write_text("garbage\n")
    elif damage == "head_missing":
        (git_dir / "HEAD").unlink()
    else:
        (git_dir / "objects").rename(git_dir / "objects.moved")
    ok, lines = _process(wk, job)
    _assert_pending(wk, job, snap, ok)
    assert len(lines) == 1 and "not a git repository" in lines[0], lines
