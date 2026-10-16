"""Contract-first tests: shown-record retention + no-follow append (#3622).

`<ENGRAM_HOME>/sessions/<sid>.shown.jsonl` (#3613 S1) is bounded per file but was
never deleted. `_hooklib.prune_shown_records` is the retention rule; the
SessionStart hook calls it once per run. The append open must not follow a
symlink planted at the record path.

C1  candidates: direct `*.shown.jsonl` entries + stale `*.shown.jsonl.<digits>.tmp`
    of <ENGRAM_HOME>/sessions/ only; nothing else is ever touched.
C2  deleted iff regular (lstat) AND older than max_age_days AND not keep_session_id;
    a future mtime is not deleted; symlink/dir/FIFO/socket candidates untouched.
C3  never raises; at most one stderr line; missing sessions dir -> 0.
C4  returns the count actually deleted.
C5  scans at most 5000 directory entries per call.
C6  an actively written record stays (append refreshes mtime).
A1  append writes nothing through a symlink at the record path.
A2  existing behaviours unchanged (pinned by tests/test_in_context_exclusion_3613.py).
H1  the SessionStart hook prunes once, keeping the current session, fail-open.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
if str(HOOK_DIR) not in sys.path:
    sys.path.insert(0, str(HOOK_DIR))
import _hooklib  # noqa: E402

DAY = 86400
SESSION = "sess-retention-1"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    eh = tmp_path / "engram"
    (eh / "sessions").mkdir(parents=True)
    monkeypatch.setenv("ENGRAM_HOME", str(eh))
    yield


def _sessions() -> Path:
    return Path(os.environ["ENGRAM_HOME"]) / "sessions"


def _mk(name: str, age_days: float = 0, now: float | None = None, content="x\n") -> Path:
    p = _sessions() / name
    p.write_text(content)
    t = (now if now is not None else time.time()) - age_days * DAY
    os.utime(p, (t, t))
    return p


def test_retention_constant():
    assert _hooklib.SHOWN_RECORD_RETENTION_DAYS == 14


# ---- C1 ---------------------------------------------------------------------

def test_c1_non_candidates_never_touched():
    names = [
        "abc.json", "abc.txt", "abc.jsonl", "abc.shown.jsonl.bak",
        "abc-piece-c.txt", "shown.jsonl", "abc.shown.jsonl.tmp",
        "abc.shown.jsonl.12x.tmp", "abc.shown.jsonl.tmp.1",
    ]
    for n in names:
        _mk(n, age_days=100)
    sub = _sessions() / "subdir"
    sub.mkdir()
    inner = sub / "old.shown.jsonl"
    inner.write_text("x")
    os.utime(inner, (1, 1))
    assert _hooklib.prune_shown_records() == 0
    for n in names:
        assert (_sessions() / n).exists(), n
    assert inner.exists()


def test_c1_stale_tmp_leftover_is_a_candidate():
    old_tmp = _mk("abc.shown.jsonl.4242.tmp", age_days=30)
    fresh_tmp = _mk("def.shown.jsonl.4243.tmp", age_days=1)
    assert _hooklib.prune_shown_records() == 1
    assert not old_tmp.exists()
    assert fresh_tmp.exists()


# ---- C2 ---------------------------------------------------------------------

def test_c2_old_deleted_recent_kept_age_boundary():
    now = 2_000_000_000.0
    old = _mk("old.shown.jsonl", age_days=15, now=now)
    recent = _mk("recent.shown.jsonl", age_days=13, now=now)
    edge = _mk("edge.shown.jsonl", age_days=14, now=now)  # not strictly older
    assert _hooklib.prune_shown_records(now=now) == 1
    assert not old.exists()
    assert recent.exists()
    assert edge.exists()


def test_c2_max_age_days_is_honoured():
    now = 2_000_000_000.0
    a = _mk("a.shown.jsonl", age_days=3, now=now)
    b = _mk("b.shown.jsonl", age_days=1, now=now)
    assert _hooklib.prune_shown_records(max_age_days=2, now=now) == 1
    assert not a.exists()
    assert b.exists()


def test_c2_keep_session_id_is_kept_even_when_old():
    now = 2_000_000_000.0
    keep = _mk("keepme.shown.jsonl", age_days=100, now=now)
    other = _mk("other.shown.jsonl", age_days=100, now=now)
    assert _hooklib.prune_shown_records(keep_session_id="keepme", now=now) == 1
    assert keep.exists()
    assert not other.exists()


def test_c2_keep_id_matched_exactly_not_by_prefix():
    now = 2_000_000_000.0
    longer = _mk("abcd.shown.jsonl", age_days=100, now=now)
    exact = _mk("abc.shown.jsonl", age_days=100, now=now)
    assert _hooklib.prune_shown_records(keep_session_id="abc", now=now) == 1
    assert not longer.exists()
    assert exact.exists()


def test_c2_keep_session_own_stale_tmp_is_protected():
    now = 2_000_000_000.0
    keep_tmp = _mk("keepme.shown.jsonl.123.tmp", age_days=100, now=now)
    other_tmp = _mk("other.shown.jsonl.123.tmp", age_days=100, now=now)
    assert _hooklib.prune_shown_records(keep_session_id="keepme", now=now) == 1
    assert keep_tmp.exists()
    assert not other_tmp.exists()


def test_c1_tmp_pid_part_is_ascii_digits_only():
    now = 2_000_000_000.0
    arabic = _mk("a.shown.jsonl.٣.tmp", age_days=100, now=now)  # Arabic-Indic 3
    ascii_tmp = _mk("a.shown.jsonl.123.tmp", age_days=100, now=now)
    assert _hooklib.prune_shown_records(now=now) == 1
    assert arabic.exists()
    assert not ascii_tmp.exists()


def test_c1_empty_session_name_is_not_a_candidate():
    now = 2_000_000_000.0
    bare = _mk(".shown.jsonl", age_days=100, now=now)
    bare_tmp = _mk(".shown.jsonl.5.tmp", age_days=100, now=now)
    assert _hooklib.prune_shown_records(now=now) == 0
    assert bare.exists() and bare_tmp.exists()


def test_c2_future_mtime_not_deleted():
    now = 2_000_000_000.0
    fut = _mk("fut.shown.jsonl", age_days=-400, now=now)
    assert _hooklib.prune_shown_records(now=now) == 0
    assert fut.exists()


def test_c2_now_defaults_to_wall_clock(monkeypatch):
    old = _mk("old.shown.jsonl", age_days=15)
    new = _mk("new.shown.jsonl", age_days=1)
    assert _hooklib.prune_shown_records() == 1
    assert not old.exists() and new.exists()


def test_c2_symlink_candidate_untouched_never_followed(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("precious")
    os.utime(target, (1, 1))
    link = _sessions() / "lnk.shown.jsonl"
    os.symlink(target, link)
    dangling = _sessions() / "dang.shown.jsonl"
    os.symlink(tmp_path / "nope", dangling)
    # Age the LINK ENTRIES themselves (lstat mtime), else a prune that deleted any
    # non-regular candidate would be spared only by the links being new.
    if os.utime not in os.supports_follow_symlinks:
        pytest.skip("os.utime(follow_symlinks=False) unsupported on this platform")
    for ln in (link, dangling):
        os.utime(ln, (1, 1), follow_symlinks=False)
        assert os.lstat(ln).st_mtime == 1
    assert _hooklib.prune_shown_records() == 0
    assert os.path.islink(link) and os.path.islink(dangling)
    assert target.read_text() == "precious"


def test_c2_directory_fifo_socket_candidates_untouched(tmp_path):
    d = _sessions() / "dir.shown.jsonl"
    d.mkdir()
    os.utime(d, (1, 1))
    fifo = _sessions() / "fifo.shown.jsonl"
    os.mkfifo(fifo)
    os.utime(fifo, (1, 1))
    sock_path = Path("/tmp") / f"sr3622-{os.getpid()}.sock"
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(sock_path))
        sock_link_dir = _sessions()
        # AF_UNIX path limit: bind short, then move the socket file in place
        sk = sock_link_dir / "sock.shown.jsonl"
        os.rename(sock_path, sk)
        os.utime(sk, (1, 1))
        assert _hooklib.prune_shown_records() == 0
        assert sk.exists()
    finally:
        s.close()
        if sock_path.exists():
            sock_path.unlink()
    assert d.is_dir() and fifo.exists()


# ---- C3 ---------------------------------------------------------------------

def test_c3_missing_sessions_dir_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "does-not-exist"))
    assert _hooklib.prune_shown_records() == 0
    assert len(capsys.readouterr().err.strip().splitlines()) <= 1


def test_c3_scandir_error_swallowed(monkeypatch, capsys):
    def boom(*a, **k):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(_hooklib.os, "scandir", boom)
    assert _hooklib.prune_shown_records() == 0
    assert len(capsys.readouterr().err.strip().splitlines()) <= 1


def test_c3_unlink_failure_swallowed_one_stderr_line_sanitised(monkeypatch, capsys):
    for i in range(4):
        _mk(f"s{i}.shown.jsonl", age_days=30)

    def boom(p, *a, **k):
        raise PermissionError(13, "denied\x1b[31m\nINJECT")
    monkeypatch.setattr(_hooklib.os, "unlink", boom)
    assert _hooklib.prune_shown_records() == 0
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) <= 1
    assert "\x1b" not in err


def test_c3_race_with_concurrent_unlink(monkeypatch):
    victim = _mk("gone.shown.jsonl", age_days=30)
    real_unlink = os.unlink

    def racy(p, *a, **k):
        real_unlink(p)          # someone else got there first
        return real_unlink(p)   # -> FileNotFoundError
    monkeypatch.setattr(_hooklib.os, "unlink", racy)
    assert _hooklib.prune_shown_records() == 0  # not counted: we did not delete it
    assert not victim.exists()


# ---- C4 ---------------------------------------------------------------------

def test_c4_returns_count_actually_deleted():
    for i in range(7):
        _mk(f"s{i}.shown.jsonl", age_days=30)
    _mk("new.shown.jsonl", age_days=1)
    assert _hooklib.prune_shown_records() == 7
    assert sorted(p.name for p in _sessions().iterdir()) == ["new.shown.jsonl"]


# ---- C5 ---------------------------------------------------------------------

def test_c5_scan_bounded_to_5000_entries():
    now = time.time()
    t = now - 30 * DAY
    for i in range(5200):
        p = _sessions() / f"s{i}.shown.jsonl"
        p.write_text("")
        os.utime(p, (t, t))
    n = _hooklib.prune_shown_records(now=now)
    assert n == 5000
    assert len(list(_sessions().iterdir())) == 200


# ---- C6 ---------------------------------------------------------------------

def test_c6_active_record_survives_until_14_days_after_last_append():
    _hooklib.append_shown_record("prompt", SESSION, ["ob_1"])
    path = _hooklib.shown_record_path(SESSION)
    assert path.exists()
    t0 = time.time()
    assert _hooklib.prune_shown_records(now=t0 + 13 * DAY) == 0
    assert path.exists()
    assert _hooklib.prune_shown_records(now=t0 + 15 * DAY) == 1
    assert not path.exists()


# ---- A1 ---------------------------------------------------------------------

def test_a1_append_does_not_write_through_symlink_to_existing_file(tmp_path, capsys):
    target = tmp_path / "victim.txt"
    target.write_text("original\n")
    path = _hooklib.shown_record_path(SESSION)
    os.symlink(target, path)
    _hooklib.append_shown_record("prompt", SESSION, ["ob_1"])  # must not raise
    assert target.read_bytes() == b"original\n"
    assert os.path.islink(path)
    assert len(capsys.readouterr().err.strip().splitlines()) <= 1


def test_a1_append_does_not_create_through_dangling_symlink(tmp_path):
    target = tmp_path / "not-yet.txt"
    path = _hooklib.shown_record_path(SESSION)
    os.symlink(target, path)
    _hooklib.append_shown_record("prompt", SESSION, ["ob_1"])
    assert not target.exists()


def test_a1_guard_regular_file_append_still_works():
    _hooklib.append_shown_record("prompt", SESSION, ["ob_1"])
    _hooklib.append_shown_record("tool", SESSION, ["ob_2"])
    ids, readable = _hooklib._shown_record_ids(SESSION)
    assert readable and ids == {"ob_1", "ob_2"}


# ---- H1: the SessionStart hook ---------------------------------------------

def _load_hook():
    module_path = HOOK_DIR / "engram-session-start-hook.py"
    name = "engram_session_start_hook_retention_3622"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hook_input(sid):
    return {"session_id": sid, "transcript_path": "/tmp/t.jsonl", "source": "startup",
            "cwd": "/tmp"}


def test_h1_hook_prunes_old_records_keeping_current_session():
    cur = _mk("cur-sess.shown.jsonl", age_days=100)
    old = _mk("old-sess.shown.jsonl", age_days=100)
    new = _mk("new-sess.shown.jsonl", age_days=1)
    mod = _load_hook()
    marker = mod.write_marker(_hook_input("cur-sess"))
    assert marker is not None and marker["session_id"] == "cur-sess"
    assert cur.exists() and new.exists()
    assert not old.exists()


def test_h1_hook_marker_write_survives_a_raising_prune(monkeypatch):
    mod = _load_hook()

    def boom(*a, **k):
        raise RuntimeError("prune bug")
    monkeypatch.setattr(mod, "prune_shown_records", boom)
    marker = mod.write_marker(_hook_input("cur-sess"))
    assert marker is not None
    assert (_sessions() / "cur-sess.json").exists()


def test_h1_hook_passes_current_session_as_keep(monkeypatch):
    mod = _load_hook()
    calls = []
    monkeypatch.setattr(mod, "prune_shown_records",
                        lambda *a, **k: calls.append((a, k)) or 0)
    mod.write_marker(_hook_input("cur-sess"))
    assert len(calls) == 1
    a, k = calls[0]
    assert k.get("keep_session_id", a[0] if a else None) == "cur-sess"
