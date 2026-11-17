"""#4843 visibility half: a snapshot job that cannot commit must be SEEN.

Before #4843 the only witness of a pending snapshot job was a nested
``version_control.snapshot_lag`` field that warned only at two distinct pending
turns, plus server stderr. The design ruling (issue comment 6950441690) makes the
backlog visible on two surfaces, through one reader:

- ``engram_snapshot_backlog.read_backlog`` reads the worker's queue file directly
  (no worker import, no directory creation) and reports a backlog once a pending
  job is older than ``BACKLOG_THRESHOLD_SECS``;
- SessionStart prints its line in the substrate-health block;
- the nap record (``compact/last-checkpoint.json``) carries it beside, never
  inside, ``backup_ok``; the nap and sleep skills print it.

Cells (contract: /home/agents-shared/halley/4843-contract.md, mirrored in the PR):
  V1 no queue -> ok, nothing created     V6 CLI exit codes and stdout
  V2 young pending -> ok, no line        V7 SessionStart prints the line / not on a healthy home
  V3 old pending -> exact numbers        V8 commit-failed shape, real store, end to end
  V4 parity with the worker's parser     V9 nap record carries it; backup_ok unchanged
  V5 unreadable queue is loud            V10 nap and sleep skills read it
"""

import contextlib
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "engram"
_HOOK_PATH = _SRC / "hooks" / "claude" / "engram-session-start-hook.py"
_MODULE_PATH = _SRC / "engram_snapshot_backlog.py"
if str(_SRC) not in sys.path:
    sys.path.append(str(_SRC))

LINE_RE = re.compile(
    r"^Snapshot commits: (\d+) job\(s\) pending, oldest (\d+)s old, (\d+) bytes held; "
    r"git history is not advancing$"
)
UNREADABLE_PREFIX = "Snapshot commits: queue could not be read ("


def _bl():
    """Import the reader fresh. On a tree without it, each cell fails on its own."""
    sys.modules.pop("engram_snapshot_backlog", None)
    return importlib.import_module("engram_snapshot_backlog")


def _pending_dir(home: Path) -> Path:
    return home / "db-backup" / ".pending"


def _write_queue(home: Path, entries, raw_lines=()):
    d = _pending_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "queue.jsonl", "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
        for raw in raw_lines:
            f.write(raw + "\n")
    return d / "queue.jsonl"


def _job(turn, seq, ts, snapshot=None):
    e = {"type": "job", "turn": turn, "seq": seq, "ts": ts, "message": "m", "mode": "nap"}
    e["snapshot"] = snapshot or f"snapshot-{turn}-{seq}.db"
    return e


def _done(turn, seq, ts):
    return {"type": "done", "turn": turn, "seq": seq, "ts": ts}


# ── V1 ──────────────────────────────────────────────────────────────────────
def test_v1_no_queue_is_ok_and_creates_nothing(tmp_path):
    bl = _bl()
    r = bl.read_backlog(str(tmp_path))
    assert set(r) == {"status", "pending", "oldest_age_secs", "bytes", "line"}
    assert r["status"] == "ok" and r["pending"] == 0 and r["line"] is None
    assert r["oldest_age_secs"] is None and r["bytes"] == 0
    assert not (tmp_path / "db-backup").exists(), "the reader must never create directories"


def test_v1b_empty_queue_is_ok(tmp_path):
    bl = _bl()
    _write_queue(tmp_path, [])
    r = bl.read_backlog(str(tmp_path))
    assert r["status"] == "ok" and r["pending"] == 0 and r["line"] is None


# ── V2 ──────────────────────────────────────────────────────────────────────
def test_v2_pending_younger_than_threshold_prints_nothing(tmp_path):
    bl = _bl()
    now = 1_000_000.0
    _write_queue(tmp_path, [_job(3, 1, now - (bl.BACKLOG_THRESHOLD_SECS - 1))])
    r = bl.read_backlog(str(tmp_path), now=now)
    assert r["status"] == "ok" and r["line"] is None
    assert r["pending"] == 1  # still counted; just not a backlog


def test_v2b_threshold_is_the_measured_basis():
    bl = _bl()
    assert bl.BACKLOG_THRESHOLD_SECS == 300
    src = _MODULE_PATH.read_text(encoding="utf-8")
    # the basis is stated in the code, not only in the PR
    for fact in ("1.38", "0.51", "30"):
        assert fact in src, f"threshold basis must cite {fact!r}"


# ── V3 ──────────────────────────────────────────────────────────────────────
def test_v3_old_pending_reports_exact_count_age_bytes(tmp_path):
    bl = _bl()
    now = 2_000_000.0
    d = _pending_dir(tmp_path)
    _write_queue(tmp_path, [
        _job(4, 1, now - 1000),             # oldest
        _job(4, 2, now - 10),               # young, still counted
        _job(4, 3, now - 500), _done(4, 3, now - 400),   # retired: not counted
    ])
    (d / "snapshot-4-1.db").write_bytes(b"x" * 1000)
    (d / "snapshot-4-1.db-wal").write_bytes(b"x" * 24)   # sidecar counts
    (d / "snapshot-4-2.db").write_bytes(b"x" * 500)
    (d / "snapshot-4-3.db").write_bytes(b"x" * 7777)     # done job's file: not counted
    r = bl.read_backlog(str(tmp_path), now=now)
    assert r["status"] == "backlog"
    assert r["pending"] == 2 and r["bytes"] == 1524
    assert int(r["oldest_age_secs"]) == 1000
    m = LINE_RE.match(r["line"])
    assert m, r["line"]
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (2, 1000, 1524)
    assert "\n" not in r["line"] and len(r["line"]) <= 300


def test_v3b_missing_snapshot_file_counts_zero_bytes(tmp_path):
    bl = _bl()
    now = 2_000_000.0
    _write_queue(tmp_path, [_job(1, 1, now - 900)])
    r = bl.read_backlog(str(tmp_path), now=now)
    assert r["status"] == "backlog" and r["bytes"] == 0


def test_v3c_exactly_at_threshold_is_not_a_backlog(tmp_path):
    bl = _bl()
    now = 2_000_000.0
    _write_queue(tmp_path, [_job(1, 1, now - bl.BACKLOG_THRESHOLD_SECS)])
    assert bl.read_backlog(str(tmp_path), now=now)["status"] == "ok"


# ── V4 ──────────────────────────────────────────────────────────────────────
def test_v4_pending_set_matches_the_worker_parser(tmp_path, monkeypatch):
    bl = _bl()
    import engram_snapshot_worker as w
    monkeypatch.setattr(w, "_data_dir", lambda: tmp_path)
    now = 3_000_000.0
    _write_queue(
        tmp_path,
        [_job(1, 1, now - 900), _job(1, 2, now - 800), _done(1, 1, now - 700), _job(2, 3, now - 600)],
        raw_lines=["this is not json", "", '{"type": "job", "turn": 2, "seq": 4, "ts": %f}' % (now - 650)],
    )
    with contextlib.redirect_stderr(io.StringIO()):
        worker_pending = w._pending_jobs()
    r = bl.read_backlog(str(tmp_path), now=now)
    assert r["pending"] == len(worker_pending) == 3
    assert int(r["oldest_age_secs"]) == 800  # (1, 2) is the oldest still pending


# ── V5 ──────────────────────────────────────────────────────────────────────
def test_v5_queue_that_is_a_directory_is_reported_unreadable(tmp_path):
    bl = _bl()
    (_pending_dir(tmp_path) / "queue.jsonl").mkdir(parents=True)
    r = bl.read_backlog(str(tmp_path))
    assert r["status"] == "unreadable"
    assert r["line"] and r["line"].startswith(UNREADABLE_PREFIX)
    assert "\n" not in r["line"] and len(r["line"]) <= 300


def test_v5b_queue_with_invalid_utf8_is_reported_unreadable(tmp_path):
    bl = _bl()
    q = _write_queue(tmp_path, [])
    q.write_bytes(b'{"type": "job", "turn": 1, "seq": 1, "ts": 1}\n\xff\xfe\xfd\n')
    r = bl.read_backlog(str(tmp_path))
    assert r["status"] == "unreadable" and r["line"].startswith(UNREADABLE_PREFIX)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_v5c_queue_without_read_permission_is_reported_unreadable(tmp_path):
    bl = _bl()
    q = _write_queue(tmp_path, [_job(1, 1, 1.0)])
    q.chmod(0)
    try:
        r = bl.read_backlog(str(tmp_path))
    finally:
        q.chmod(0o600)
    assert r["status"] == "unreadable" and r["line"].startswith(UNREADABLE_PREFIX)
    assert "PermissionError" in r["line"]


# ── V6 ──────────────────────────────────────────────────────────────────────
def _cli(home):
    return subprocess.run(
        [sys.executable, str(_MODULE_PATH), str(home)], capture_output=True, text=True, timeout=60
    )


def test_v6_cli_exit_codes_and_stdout(tmp_path):
    assert _MODULE_PATH.exists(), "engram_snapshot_backlog.py is missing"
    healthy = tmp_path / "healthy"
    healthy.mkdir()
    r = _cli(healthy)
    assert (r.returncode, r.stdout) == (0, ""), r

    backlog = tmp_path / "backlog"
    _write_queue(backlog, [_job(1, 1, time.time() - 5000)])
    r = _cli(backlog)
    assert r.returncode == 1 and LINE_RE.match(r.stdout.strip()), r

    bad = tmp_path / "bad"
    (_pending_dir(bad) / "queue.jsonl").mkdir(parents=True)
    r = _cli(bad)
    assert r.returncode == 2 and r.stdout.strip().startswith(UNREADABLE_PREFIX), r


# ── V7 ──────────────────────────────────────────────────────────────────────
def _load_hook(home: Path):
    os.environ["ENGRAM_HOME"] = str(home)
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct
    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.detect_harness = lambda: "claude-code"
    mod.tool_capability_index_block = lambda: ""
    return mod


def _hook_context(tmp_path, home: Path) -> str:
    mod = _load_hook(home)
    client = MagicMock()
    client.call.side_effect = lambda *a, **k: {}
    buf = io.StringIO()
    payload = json.dumps({"session_id": "v7-4843", "transcript_path": str(tmp_path / "t.jsonl"),
                          "source": "startup"})
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=MagicMock(return_value=client))}):
        with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload.encode()))):
            with contextlib.redirect_stdout(buf):
                mod.main()
    return json.loads(buf.getvalue().strip())["hookSpecificOutput"]["additionalContext"]


@pytest.mark.usefixtures("isolate_hook_loads")
def test_v7_session_start_prints_the_backlog_line(tmp_path):
    home = tmp_path / "engram"
    (home / "sessions").mkdir(parents=True)
    _write_queue(home, [_job(9, 1, time.time() - 5000)])
    (_pending_dir(home) / "snapshot-9-1.db").write_bytes(b"x" * 4096)
    ctx = _hook_context(tmp_path, home)
    lines = [l.strip() for l in ctx.splitlines()]
    hits = [l for l in lines if LINE_RE.match(l)]
    assert len(hits) == 1, ctx[:3000]
    m = LINE_RE.match(hits[0])
    assert (int(m.group(1)), int(m.group(3))) == (1, 4096)
    assert "ENGRAM substrate health" in ctx


@pytest.mark.usefixtures("isolate_hook_loads")
def test_v7b_session_start_healthy_home_prints_no_snapshot_line(tmp_path):
    home = tmp_path / "engram"
    (home / "sessions").mkdir(parents=True)
    ctx = _hook_context(tmp_path, home)
    assert "Snapshot commits:" not in ctx


@pytest.mark.usefixtures("isolate_hook_loads")
def test_v7c_session_start_unreadable_queue_is_not_silent(tmp_path):
    home = tmp_path / "engram"
    (home / "sessions").mkdir(parents=True)
    (_pending_dir(home) / "queue.jsonl").mkdir(parents=True)
    ctx = _hook_context(tmp_path, home)
    assert UNREADABLE_PREFIX in ctx


# ── V8 / V9: a real store ──────────────────────────────────────────────────
@contextlib.contextmanager
def _real_store(monkeypatch, tag):
    """A real store (server._configure_paths + git init); environment, sys.path and
    the server/engram_* modules are restored afterwards (the #4833 cell-3 pattern)."""
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    env_before = dict(os.environ)
    path_before = list(sys.path)
    mods_before = {k: v for k, v in sys.modules.items() if k == "server" or k.startswith("engram_")}
    d = tempfile.mkdtemp(prefix=f"vis4843-{tag}-")
    try:
        for k in [k for k in sys.modules if k == "server" or k.startswith("engram_")]:
            del sys.modules[k]
        sys.path.insert(0, str(_REPO_ROOT))
        import server
        server._configure_paths(d)
        server._ensure_data_dir()
        for a in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                  ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)
        yield server, d
    finally:
        for k in [k for k in sys.modules if k == "server" or k.startswith("engram_")]:
            del sys.modules[k]
        sys.modules.update(mods_before)
        sys.path[:] = path_before
        os.environ.clear()
        os.environ.update(env_before)
        shutil.rmtree(d, ignore_errors=True)


def _pending_nap(server, message):
    import engram_snapshot_worker as wk
    real = wk.start_worker
    wk.start_worker = lambda: None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            server._nap_impl(message=message)
    finally:
        wk.start_worker = real
    return wk


def test_v8_commit_failed_shape_is_visible_end_to_end(monkeypatch):
    with _real_store(monkeypatch, "v8") as (server, d):
        hook = Path(d, ".git", "hooks", "pre-commit")
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        wk = _pending_nap(server, "v8")
        with contextlib.redirect_stderr(io.StringIO()):
            for _ in range(3):
                wk._drain_pending()
        assert len(wk._pending_jobs()) == 1, "rig: the commit-failed job must stay pending"
        bl = _bl()
        assert bl.read_backlog(d)["status"] == "ok", "a fresh pending job is not yet a backlog"
        r = bl.read_backlog(d, now=time.time() + bl.BACKLOG_THRESHOLD_SECS + 1)
        assert r["status"] == "backlog" and LINE_RE.match(r["line"]), r
        assert r["pending"] == 1 and r["bytes"] == os.path.getsize(
            Path(d, "db-backup", ".pending", wk._pending_jobs()[0]["snapshot"]))


def _record(d):
    return json.loads(Path(d, "compact", "last-checkpoint.json").read_text(encoding="utf-8"))


def test_v9_nap_record_carries_backlog_and_backup_ok_is_unchanged(monkeypatch):
    with _real_store(monkeypatch, "v9") as (server, d):
        import engram_lifecycle as lc
        _write_queue(Path(d), [_job(0, 99, time.time() - 5000)])
        lc._write_last_checkpoint_record("2026-11-17T00:00:00", None,
                                         {"db_backup": {"path": "x"}, "binary_backup": {"path": "y"}})
        rec = _record(d)
        assert rec["backup_ok"] is True, "a pending commit is not a failed backup"
        assert rec["snapshot_backlog"]["status"] == "backlog"
        assert LINE_RE.match(rec["snapshot_backlog"]["line"])


def test_v9b_nap_record_without_backlog_has_no_line(monkeypatch):
    with _real_store(monkeypatch, "v9b") as (server, d):
        import engram_lifecycle as lc
        lc._write_last_checkpoint_record("2026-11-17T00:00:00", None,
                                         {"db_backup": {"path": "x"}, "binary_backup": {"path": "y"}})
        rec = _record(d)
        assert rec["backup_ok"] is True
        assert rec["snapshot_backlog"] == {"status": "ok", "line": None}


def test_v9c_failed_backup_still_fails_with_or_without_backlog(monkeypatch):
    with _real_store(monkeypatch, "v9c") as (server, d):
        import engram_lifecycle as lc
        _write_queue(Path(d), [_job(0, 98, time.time() - 5000)])
        lc._write_last_checkpoint_record("2026-11-17T00:00:00", None,
                                         {"db_backup": {"error": "disk"}, "binary_backup": {}})
        rec = _record(d)
        assert rec["backup_ok"] is False and rec["snapshot_backlog"]["status"] == "backlog"


# ── V10 ─────────────────────────────────────────────────────────────────────
def test_v10_nap_and_sleep_skills_read_the_backlog():
    nap = (_SRC / "skills" / "claude" / "engram-nap" / "SKILL.md").read_text(encoding="utf-8")
    assert "snapshot_backlog" in nap, "the nap skill must print the record's snapshot_backlog line"
    sleep = (_SRC / "skills" / "claude" / "engram-sleep" / "SKILL.md").read_text(encoding="utf-8")
    assert "engram_snapshot_backlog.py" in sleep, "sleep Phase A must run the backlog reader"
    assert "snapshot commits:" in sleep


# ── Review folds (Halley's review of 641d78a6) ─────────────────────────────
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_v5d_queue_that_is_a_fifo_is_unreadable_and_does_not_block(tmp_path):
    """SessionStart runs this reader; a FIFO at the queue path must not hang it."""
    import threading
    bl = _bl()
    d = _pending_dir(tmp_path)
    d.mkdir(parents=True)
    os.mkfifo(d / "queue.jsonl")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", bl.read_backlog(str(tmp_path))), daemon=True)
    t.start()
    t.join(5)
    if t.is_alive():
        # unblock the reader so the thread can exit, then fail
        fd = os.open(d / "queue.jsonl", os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        t.join(5)
        pytest.fail("read_backlog blocked on a FIFO at the queue path")
    assert out["r"]["status"] == "unreadable" and out["r"]["line"].startswith(UNREADABLE_PREFIX)


def test_v3d_snapshot_name_is_confined_to_the_pending_dir(tmp_path):
    bl = _bl()
    now = 2_000_000.0
    _write_queue(tmp_path, [_job(1, 1, now - 900, snapshot="../outside.db")])
    (tmp_path / "db-backup" / "outside.db").write_bytes(b"x" * 999)
    r = bl.read_backlog(str(tmp_path), now=now)
    assert r["status"] == "backlog" and r["bytes"] == 0, "a name may not reach outside .pending"


@pytest.mark.usefixtures("isolate_hook_loads")
def test_v7d_unreadable_queue_arrow_does_not_claim_a_commit_failure(tmp_path):
    home = tmp_path / "engram"
    (home / "sessions").mkdir(parents=True)
    (_pending_dir(home) / "queue.jsonl").mkdir(parents=True)
    ctx = _hook_context(tmp_path, home)
    i = ctx.index(UNREADABLE_PREFIX)
    following = ctx[i:].splitlines()[1]
    assert "cannot commit" not in following, following


def test_v6b_cli_default_home_goes_through_the_resolver(tmp_path):
    """No argument: the home comes from engram_home_resolver.engram_home() (the raw-home
    gate). A relative ENGRAM_HOME is refused there; the CLI reports it as unreadable
    (exit 2), never as a backlog (1) and never as healthy (0)."""
    env = dict(os.environ, ENGRAM_HOME="relative/home")
    r = subprocess.run([sys.executable, str(_MODULE_PATH)], capture_output=True, text=True,
                       timeout=60, env=env, cwd=str(tmp_path))
    assert r.returncode == 2, r
    assert r.stdout.strip().startswith(UNREADABLE_PREFIX) and "ValueError" in r.stdout, r

    home = tmp_path / "abs-home"
    _write_queue(home, [_job(1, 1, time.time() - 5000)])
    env = dict(os.environ, ENGRAM_HOME=str(home))
    r = subprocess.run([sys.executable, str(_MODULE_PATH)], capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.returncode == 1 and LINE_RE.match(r.stdout.strip()), r


def test_v10b_nap_skill_is_loud_when_the_record_cannot_be_used():
    """Reviewer round 1: a missing or mismatched checkpoint record must not silently drop
    the backlog line; the nap skill prints that the backlog is unknown."""
    nap = (_SRC / "skills" / "claude" / "engram-nap" / "SKILL.md").read_text(encoding="utf-8")
    assert "snapshot backlog unknown" in nap
