"""Tests for `monitor-supervisor.py --check` (engram-alpha#3311, code half).

Contract-first: these assertions were written (and observed failing) before
`--check` existed. The contract, from the #3311 spec:

  * read-only: never starts, signals, reaps or restarts anything, never writes
    the pidfile or status file;
  * whenever the pidfile is readable, stdout is EXACTLY five lines, in order:
    `supervisor:`, `updates:`, `mention:`, `bell:`, then `CHECK <PASS|FAIL> k/4
    failed` -- never stopping at the first failure;
  * exit 0 iff all four items PASS, 1 on any FAIL, 2 pidfile
    missing/unreadable/not-an-int (ONE line, `CHECK FAIL supervisor not armed`),
    3 no /proc (ONE line, `CHECK FAIL unsupported platform`);
  * two independent upstreams (status file AND process table) must agree for a
    child to PASS; a process-table child of the supervisor that the status file
    does not name is an "extra child" and fails the summary;
  * staleness window (spec amendment): the supervisor records its own
    `status_interval` in the status file; the default max age is 3x that value,
    `--max-status-age` overrides, and an absent field falls back to
    3 x DEFAULT_STATUS_INTERVAL_SECONDS and says so on the supervisor line.

Each test's docstring names the mutation that must turn it red. Reason-text
assertions are deliberate: a later check in the same item often fails too when
an earlier one is skipped, so the REASON is what discriminates which check ran.

Linux-only (/proc); skipped elsewhere.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_monitor_supervisor import (  # noqa: E402
    _SUPERVISOR_SCRIPT,
    _fake_cmd,
    _wait_for_status_children,
    _write_fake_child,
)

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc"), reason="--check is Linux-only (needs /proc)"
)

_NAMES = ("updates", "mention", "bell")
_ARGV_FRAGMENT = {
    "updates": "forum-updates-monitor",
    "mention": "forum-mention-monitor",
    "bell": "umwelt-bell",
}
_ITEM_RE = re.compile(r"^(supervisor|updates|mention|bell):\s+(PASS|FAIL)\b(.*)$")


class _Env:
    """One launched supervisor (fake children) plus its scratch paths.

    The supervisor is launched with a huge --status-interval so it never
    rewrites the status file behind a test's back; tests that need a specific
    recorded interval set `status_interval` in the file themselves."""

    def __init__(self, tmp_path: Path, stdout_kind: str = "socket"):
        self.tmp = tmp_path
        self.uid = uuid.uuid4().hex[:10]
        self.pidfile = tmp_path / "sup.pid"
        self.status = tmp_path / "status.json"
        self._extras: list[subprocess.Popen] = []
        self._socks: list[socket.socket] = []
        self._files: list = []
        fake = _write_fake_child(tmp_path)
        args = [
            "--updates-cmd-json", _fake_cmd(fake, f"{self.uid}-forum-updates-monitor", "quiet"),
            "--mention-cmd-json", _fake_cmd(fake, f"{self.uid}-forum-mention-monitor", "quiet"),
            "--bell-cmd-json", _fake_cmd(fake, f"{self.uid}-umwelt-bell", "quiet"),
            "--pidfile", str(self.pidfile),
            "--status-file", str(self.status),
            "--status-interval", "3600",
        ]
        self.stdout_path = tmp_path / "sup-stdout.txt"
        stdout = self._stdout_target(stdout_kind)
        self.proc = subprocess.Popen(
            [sys.executable, str(_SUPERVISOR_SCRIPT), *args],
            stdout=stdout, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        data = _wait_for_status_children(self.status, 3)
        self.children = {n: data["children"][n]["pid"] for n in _NAMES}
        self._wait_exec_settled()

    def _stdout_target(self, kind: str):
        if kind == "socket":
            a, b = socket.socketpair()
            self._socks += [a, b]
            return a
        f = open(self.stdout_path, "w")
        self._files.append(f)
        return f

    def _wait_exec_settled(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            ok = True
            for n, pid in self.children.items():
                try:
                    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
                except OSError:
                    ok = False
                    break
                if _ARGV_FRAGMENT[n] not in cmd:
                    ok = False
                    break
            if ok and self.pidfile.exists():
                return
            time.sleep(0.05)
        raise AssertionError("fake children never finished exec")

    def spawn_extra(self, argv: list[str], stdout=subprocess.DEVNULL) -> subprocess.Popen:
        p = subprocess.Popen(argv, stdout=stdout, stderr=subprocess.DEVNULL)
        self._extras.append(p)
        return p

    def rewrite_status(self, mutate) -> None:
        data = json.loads(self.status.read_text())
        mutate(data)
        self.status.write_text(json.dumps(data))

    def check(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_SUPERVISOR_SCRIPT), "--check",
             "--pidfile", str(self.pidfile), "--status-file", str(self.status), *extra],
            capture_output=True, text=True, timeout=30,
        )

    def close(self) -> None:
        for p in self._extras:
            p.kill()
            p.wait()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for s in self._socks:
            s.close()
        for f in self._files:
            f.close()


@pytest.fixture
def launch(tmp_path):
    envs: list[_Env] = []

    def _launch(stdout_kind: str = "socket") -> _Env:
        e = _Env(tmp_path, stdout_kind)
        envs.append(e)
        return e

    yield _launch
    for e in envs:
        e.close()


def _parse(res: subprocess.CompletedProcess) -> dict:
    """Assert the five-line invariant (test 8's shared assertion) and return
    {item: (verdict, reason)} plus the summary line under 'summary'."""
    lines = res.stdout.splitlines()
    assert len(lines) == 5, f"expected exactly 5 lines, got {len(lines)}: {res.stdout!r}"
    out = {}
    for line, name in zip(lines[:4], ("supervisor", "updates", "mention", "bell")):
        m = _ITEM_RE.match(line)
        assert m and m.group(1) == name, f"bad item line for {name}: {line!r}"
        out[name] = (m.group(2), m.group(3).strip())
    assert re.match(r"^CHECK (PASS|FAIL) \d/4 failed", lines[4]), lines[4]
    out["summary"] = lines[4]
    return out


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# 1 ---------------------------------------------------------------------
def test_healthy_all_pass(launch):
    """Mutation: check always FAILs (or never reaches PASS) -> red."""
    e = launch("socket")
    res = e.check()
    out = _parse(res)
    assert res.returncode == 0, res.stdout
    assert all(out[n][0] == "PASS" for n in ("supervisor", *_NAMES)), res.stdout
    assert out["summary"].startswith("CHECK PASS 0/4 failed")


# 2 ---------------------------------------------------------------------
def test_pidfile_missing_exit_2_one_line(tmp_path):
    """Mutation: treat a missing pidfile as pid 0 / PASS -> red."""
    res = subprocess.run(
        [sys.executable, str(_SUPERVISOR_SCRIPT), "--check",
         "--pidfile", str(tmp_path / "nope.pid"),
         "--status-file", str(tmp_path / "nope.json")],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 2
    lines = res.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith("CHECK FAIL supervisor not armed"), res.stdout


def test_pidfile_not_an_integer_exit_2_one_line(tmp_path):
    """Mutation: int() failure falls through to a five-line report -> red."""
    pf = tmp_path / "bad.pid"
    pf.write_text("not-a-pid\n")
    res = subprocess.run(
        [sys.executable, str(_SUPERVISOR_SCRIPT), "--check", "--pidfile", str(pf),
         "--status-file", str(tmp_path / "s.json")],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 2
    assert len(res.stdout.splitlines()) == 1
    assert res.stdout.startswith("CHECK FAIL supervisor not armed")


# 3 ---------------------------------------------------------------------
def test_supervisor_stdout_regular_file_fails_naming_path(launch):
    """Mutation: drop the `socket:` condition -> supervisor item PASSes -> red."""
    e = launch("file")
    res = e.check()
    out = _parse(res)
    assert res.returncode == 1
    verdict, reason = out["supervisor"]
    assert verdict == "FAIL"
    assert str(e.stdout_path) in reason, reason
    # Children are healthy: only the supervisor item may fail.
    assert all(out[n][0] == "PASS" for n in _NAMES), res.stdout
    assert out["summary"].startswith("CHECK FAIL 1/4 failed")


# 4 ---------------------------------------------------------------------
def test_dead_status_pid_fails_that_child(launch):
    """Mutation: skip the alive check -> reason no longer says 'not alive' -> red."""
    e = launch()
    dead = _dead_pid()
    e.rewrite_status(lambda d: d["children"]["bell"].update(pid=dead))
    res = e.check()
    out = _parse(res)
    assert res.returncode == 1
    assert out["bell"][0] == "FAIL" and "not alive" in out["bell"][1], out["bell"]
    assert out["updates"][0] == "PASS" and out["mention"][0] == "PASS"


# 5 ---------------------------------------------------------------------
def test_stale_status_file_fails_all_children(launch):
    """Mutation: ignore generated_at -> children PASS -> red."""
    e = launch()
    e.rewrite_status(lambda d: d.update(generated_at=time.time() - 3600, status_interval=10.0))
    res = e.check()
    out = _parse(res)
    assert res.returncode == 1
    for n in _NAMES:
        assert out[n][0] == "FAIL" and "stale" in out[n][1], (n, out[n])
    assert out["supervisor"][0] == "PASS"


def test_max_status_age_flag_is_honoured(launch):
    """Mutation: ignore --max-status-age -> red."""
    e = launch()
    e.rewrite_status(lambda d: d.update(generated_at=time.time() - 100, status_interval=10.0))
    assert _parse(e.check())["updates"][0] == "FAIL"  # 3 x 10s = 30s: stale
    res = e.check("--max-status-age", "1000")
    out = _parse(res)
    assert res.returncode == 0, res.stdout
    assert out["updates"][0] == "PASS"


def test_missing_status_file_fails_all_children(launch):
    """Mutation: missing status file raises / skips children -> red."""
    e = launch()
    e.status.unlink()
    res = e.check()
    out = _parse(res)
    assert res.returncode == 1
    for n in _NAMES:
        assert out[n][0] == "FAIL" and "missing/stale" in out[n][1], (n, out[n])


# staleness window derives from the recorded status_interval (amendment) ----
def test_staleness_window_is_three_times_recorded_interval(launch):
    """A 1.0s-old file is STALE for a supervisor that recorded
    status_interval=0.2 (3x = 0.6s) but fresh for one that recorded 10.0
    (3x = 30s). Mutation: hard-code 30s -> the 0.2 case reads fresh -> red."""
    e = launch()
    e.rewrite_status(lambda d: d.update(generated_at=time.time() - 1.0, status_interval=0.2))
    res = e.check()
    out = _parse(res)
    assert res.returncode == 1
    for n in _NAMES:
        assert out[n][0] == "FAIL" and "stale" in out[n][1], (n, out[n])
    e.rewrite_status(lambda d: d.update(generated_at=time.time() - 1.0, status_interval=10.0))
    res = e.check()
    assert res.returncode == 0, res.stdout
    assert all(_parse(res)[n][0] == "PASS" for n in _NAMES)


def test_absent_status_interval_falls_back_and_says_so(launch):
    """Older supervisor: no status_interval field. Falls back to
    3 x DEFAULT_STATUS_INTERVAL_SECONDS (30s) -- a 20s-old file is fresh, a
    40s-old one stale -- and the supervisor line's reason says the field was
    absent. Mutation: treat an absent field as 0 / crash / stay silent -> red."""
    e = launch()

    def drop(d, age):
        d.pop("status_interval", None)
        d["generated_at"] = time.time() - age

    e.rewrite_status(lambda d: drop(d, 20.0))
    res = e.check()
    out = _parse(res)
    assert res.returncode == 0, res.stdout
    assert "status_interval" in out["supervisor"][1], out["supervisor"]
    e.rewrite_status(lambda d: drop(d, 40.0))
    out = _parse(e.check())
    assert out["updates"][0] == "FAIL" and "stale" in out["updates"][1]


# 6 ---------------------------------------------------------------------
def test_alive_but_not_a_child_of_supervisor_fails(launch):
    """Status pid is a live process (argv even carries the right name) that is
    not the supervisor's child. Mutation: trust the status file without the
    ppid check -> reason no longer mentions the parent -> red."""
    e = launch()
    stand_in = e.spawn_extra(
        [sys.executable, "-c", "import time; time.sleep(120)", f"{e.uid}-umwelt-bell"]
    )
    e.rewrite_status(lambda d: d["children"]["bell"].update(pid=stand_in.pid))
    res = e.check()
    out = _parse(res)
    assert res.returncode == 1
    verdict, reason = out["bell"]
    assert verdict == "FAIL" and "not a child of supervisor" in reason, out["bell"]


# 7 ---------------------------------------------------------------------
def test_extra_process_table_child_named_on_summary(launch):
    """The status file names a stand-in for bell, so the supervisor's REAL bell
    process is a process-table child the status file does not name. The summary
    must name it. Mutation: drop the count rule -> no 'extra child' -> red."""
    e = launch()
    real_bell = e.children["bell"]
    stand_in = e.spawn_extra([sys.executable, "-c", "import time; time.sleep(120)"])
    e.rewrite_status(lambda d: d["children"]["bell"].update(pid=stand_in.pid))
    res = e.check()
    out = _parse(res)
    assert res.returncode == 1
    assert f"extra child pid={real_bell}" in out["summary"], out["summary"]


# 9 ---------------------------------------------------------------------
def test_pidfile_names_live_non_supervisor(tmp_path):
    """A live pid that is not a monitor-supervisor (its stdout is even a socket,
    so skipping identity would PASS it). Mutation: skip identity -> red."""
    a, b = socket.socketpair()
    sleeper = subprocess.Popen(["sleep", "120"], stdout=a)
    try:
        pf = tmp_path / "sup.pid"
        pf.write_text(str(sleeper.pid))
        status = tmp_path / "status.json"
        status.write_text(json.dumps({
            "generated_at": time.time(),
            "status_interval": 10.0,
            "children": {n: {"pid": sleeper.pid, "alive": True,
                              "last_line_ts": None, "restart_count": 0} for n in _NAMES},
        }))
        res = subprocess.run(
            [sys.executable, str(_SUPERVISOR_SCRIPT), "--check",
             "--pidfile", str(pf), "--status-file", str(status)],
            capture_output=True, text=True, timeout=30,
        )
        out = _parse(res)
        assert res.returncode == 1
        assert out["supervisor"][0] == "FAIL"
        assert "not a monitor-supervisor" in out["supervisor"][1], out["supervisor"]
    finally:
        sleeper.kill()
        sleeper.wait()
        a.close()
        b.close()


def test_pidfile_names_dead_pid_still_five_lines(tmp_path):
    """Dead supervisor pid: supervisor FAIL, five lines (never early-return)."""
    pf = tmp_path / "sup.pid"
    pf.write_text(str(_dead_pid()))
    res = subprocess.run(
        [sys.executable, str(_SUPERVISOR_SCRIPT), "--check", "--pidfile", str(pf),
         "--status-file", str(tmp_path / "s.json")],
        capture_output=True, text=True, timeout=30,
    )
    out = _parse(res)
    assert res.returncode == 1
    assert out["supervisor"][0] == "FAIL" and "not alive" in out["supervisor"][1]


# 10 --------------------------------------------------------------------
def test_check_is_read_only(launch):
    """Mutation: --check falls through into normal startup (reaps + respawns)
    -> pids/mtimes change or the supervisor dies -> red."""
    e = launch()
    pid_m, st_m = e.pidfile.stat().st_mtime_ns, e.status.stat().st_mtime_ns
    pid_txt, st_txt = e.pidfile.read_text(), e.status.read_text()
    res = e.check()
    assert res.returncode == 0, res.stdout
    time.sleep(0.5)  # a falling-through startup would have reaped by now
    assert e.proc.poll() is None, "supervisor was killed by --check"
    assert e.pidfile.stat().st_mtime_ns == pid_m and e.pidfile.read_text() == pid_txt
    assert e.status.stat().st_mtime_ns == st_m and e.status.read_text() == st_txt
    for n, pid in e.children.items():
        assert Path(f"/proc/{pid}/cmdline").exists(), f"{n} child {pid} gone"
    assert _parse(e.check())["summary"].startswith("CHECK PASS")  # still healthy


# platform gate -----------------------------------------------------------
def _load_module():
    spec = importlib.util.spec_from_file_location("monitor_supervisor_under_test", _SUPERVISOR_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_proc_exit_3_one_line(tmp_path, monkeypatch, capsys):
    """#3330: exit 3 now means NEITHER backend works -- no /proc AND the
    injected runner reports ps unusable (previously: no /proc alone). One line,
    never advising a three-Monitor check.
    Mutation: no platform gate (falls to pidfile logic / crashes) -> red."""
    mod = _load_module()
    monkeypatch.setattr(mod, "PROC_ROOT", str(tmp_path / "no-such-proc"))

    def no_ps(argv):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(mod, "CHECK_RUNNER", no_ps)
    with pytest.raises(SystemExit) as ei:
        mod.main(["--check", "--pidfile", str(tmp_path / "p"), "--status-file", str(tmp_path / "s")])
    assert ei.value.code == 3
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("CHECK FAIL unsupported platform")
    assert "three-Monitor" not in lines[0]


# ---------------------------------------------------------------------------
# Guards that survived round-1 mutation (#3316 r1): a synthesized /proc tree.
#
# Each test below is built so that EVERY condition passes except the one
# guarded condition. The tree is entirely synthetic (PROC_ROOT seam + the
# `ps` identity lookup stubbed), so no live process is needed and no other
# condition can accidentally fail alongside the guarded one. The healthy
# control proves the tree itself is capable of a PASS.
# ---------------------------------------------------------------------------
_SUP = 424242
_FAKE_KIDS = {"updates": 424243, "mention": 424244, "bell": 424245}
_FAKE_PIPES = {"updates": "pipe:[9001]", "mention": "pipe:[9002]", "bell": "pipe:[9003]"}


class _FakeProc:
    """Synthetic PROC_ROOT: per-pid `stat`, `cmdline`, `fd/` symlinks."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "fakeproc"
        self.root.mkdir()
        self.pidfile = tmp_path / "sup.pid"
        self.status = tmp_path / "status.json"
        self.pidfile.write_text(str(_SUP))
        self.add(_SUP, ppid=1, argv=["python3", str(_SUPERVISOR_SCRIPT)],
                 fds={1: "socket:[7001]", **{3 + i: p for i, p in enumerate(_FAKE_PIPES.values())}})
        for name, pid in _FAKE_KIDS.items():
            self.add(pid, ppid=_SUP, argv=["python3", f"/x/{_ARGV_FRAGMENT[name]}.py"],
                     fds={1: _FAKE_PIPES[name]})
        self.write_status()

    def add(self, pid: int, ppid: int, argv: list[str], fds: dict[int, str]) -> None:
        d = self.root / str(pid)
        (d / "fd").mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(f"{pid} (fake) S {ppid} 0 0 0\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        for fd, target in fds.items():
            link = d / "fd" / str(fd)
            if link.is_symlink():
                link.unlink()
            link.symlink_to(target)

    def write_status(self) -> None:
        self.status.write_text(json.dumps({
            "generated_at": time.time(), "status_interval": 3600,
            "children": {n: {"pid": p} for n, p in _FAKE_KIDS.items()},
        }))

    def run(self, monkeypatch, capsys):
        mod = _load_module()
        monkeypatch.setattr(mod, "PROC_ROOT", str(self.root))
        monkeypatch.setattr(mod, "_pid_cmdline_status",
                            lambda pid: ("ok", f"python3 {_SUPERVISOR_SCRIPT}"))
        with pytest.raises(SystemExit) as ei:
            mod.main(["--check", "--pidfile", str(self.pidfile), "--status-file", str(self.status)])
        res = subprocess.CompletedProcess([], ei.value.code, capsys.readouterr().out, "")
        return res, _parse(res)


def test_fake_tree_healthy_control_passes(tmp_path, monkeypatch, capsys):
    """Control: the synthesized tree with every condition holding is a PASS, so
    the four guard tests below fail ONLY on their one violated condition."""
    res, out = _FakeProc(tmp_path).run(monkeypatch, capsys)
    assert res.returncode == 0, res.stdout
    assert out["summary"] == "CHECK PASS 0/4 failed", out["summary"]


def test_extra_child_alone_fails_verdict(tmp_path, monkeypatch, capsys):
    """All four items PASS; one extra unnamed child of the supervisor exists.
    Mutation: drop `and not extras` from the verdict -> `CHECK PASS 0/4 failed;
    extra child ...`, exit 0 -> red."""
    fp = _FakeProc(tmp_path)
    fp.add(424299, ppid=_SUP, argv=["sleep", "120"], fds={1: "pipe:[9999]"})
    res, out = fp.run(monkeypatch, capsys)
    assert all(out[n][0] == "PASS" for n in ("supervisor", *_NAMES)), res.stdout
    assert res.returncode == 1, res.stdout
    assert out["summary"].startswith("CHECK FAIL 0/4 failed"), out["summary"]
    assert "extra child pid=424299" in out["summary"], out["summary"]


def test_child_with_wrong_argv_fails_on_argv(tmp_path, monkeypatch, capsys):
    """updates slot runs something that is not forum-updates-monitor; alive,
    ppid, pipe-into-supervisor and status all hold. Mutation: drop the argv
    identity check -> that child PASSes -> red."""
    fp = _FakeProc(tmp_path)
    fp.add(_FAKE_KIDS["updates"], ppid=_SUP, argv=["python3", "/x/not-a-monitor.py"],
           fds={1: _FAKE_PIPES["updates"]})
    res, out = fp.run(monkeypatch, capsys)
    assert res.returncode == 1, res.stdout
    verdict, reason = out["updates"]
    assert verdict == "FAIL" and "argv does not identify forum-updates-monitor" in reason, reason
    assert all(out[n][0] == "PASS" for n in ("supervisor", "mention", "bell")), res.stdout


def test_child_pipe_not_held_by_supervisor_fails(tmp_path, monkeypatch, capsys):
    """Child fd1 is pipe:[N] but the supervisor holds no fd on pipe:[N].
    Mutation: drop the `target not in sup_fds` check -> PASS -> red."""
    fp = _FakeProc(tmp_path)
    fp.add(_FAKE_KIDS["mention"], ppid=_SUP, argv=["python3", "/x/forum-mention-monitor.py"],
           fds={1: "pipe:[5555]"})
    res, out = fp.run(monkeypatch, capsys)
    assert res.returncode == 1, res.stdout
    verdict, reason = out["mention"]
    assert verdict == "FAIL" and "not held open by the supervisor" in reason, reason
    assert all(out[n][0] == "PASS" for n in ("supervisor", "updates", "bell")), res.stdout


def test_child_stdout_regular_file_fails(tmp_path, monkeypatch, capsys):
    """Child fd1 is a regular file, not a pipe. The supervisor ALSO holds that
    same file, so the held-by-supervisor check cannot rescue the verdict.
    Mutation: drop the `startswith("pipe:[")` check -> PASS -> red."""
    fp = _FakeProc(tmp_path)
    logf = tmp_path / "child.log"
    logf.write_text("")
    fp.add(_SUP, ppid=1, argv=["python3", str(_SUPERVISOR_SCRIPT)], fds={9: str(logf)})
    fp.add(_FAKE_KIDS["bell"], ppid=_SUP, argv=["python3", "/x/umwelt-bell.py"],
           fds={1: str(logf)})
    res, out = fp.run(monkeypatch, capsys)
    assert res.returncode == 1, res.stdout
    verdict, reason = out["bell"]
    assert verdict == "FAIL" and "not a pipe" in reason, reason
    assert all(out[n][0] == "PASS" for n in ("supervisor", "updates", "mention")), res.stdout
