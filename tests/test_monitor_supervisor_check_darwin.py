"""Tests for the `monitor-supervisor.py --check` ps/lsof backend (#3330).

Contract-first: written (and observed failing) before the backend existed.

  C1  output shape + exit codes identical across backends (5 lines, 0/1/2);
  C2  /proc present -> proc backend; no /proc -> ps backend; exit 3 ONLY when
      neither works, and that message never advises a three-Monitor check;
  C3  ps items from ONE uid-scoped snapshot: liveness (Z = dead), ppid ==
      supervisor, script name in argv (not argv[0]); extra-child rule shared;
  C4  stdout item: child fd 1 is a PIPE whose device/peer pair the supervisor
      holds in BOTH directions; lsof absent/erroring -> FAIL naming it, never
      PASS;
  C5  ps/lsof go through one injectable runner, `CHECK_RUNNER(argv) ->
      (returncode, stdout)`, raising OSError when a binary cannot run;
  F1  pipe peer rule (both directions); F2 uid scope; F3 stat S/Z, argv.

Fixtures are REAL Mac captures (tests/fixtures/darwin_check/). The captures
carry the forum-updates child only; the mention/bell children's lsof output is
SYNTHETIC, built by `_child_lsof` in the capture's exact format and paired
against the supervisor's real fd 5 / fd 7 by the F1 rule.

Each test's docstring names the mutation that must turn it red.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_monitor_supervisor_check import _load_module, _parse  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "darwin_check"
SUP = 65874
KIDS = {"updates": 65876, "mention": 65877, "bell": 65878}


def _fx(name: str) -> str:
    return (FIX / name).read_text()


def _child_lsof(pid: int, dev: str, peer: str) -> str:
    """SYNTHETIC child capture in the real format (see module docstring)."""
    return (f"p{pid}\nf0\ntCHR\nn/dev/null\nf1\ntPIPE\nd{dev}\nn->{peer}\n"
            "f2\ntPIPE\nd0x1111111111111111\nn->0x2222222222222222\n")


# supervisor real fd5 = d0xb3d6...ef95e n->0x45d5...600d7 ; fd7 = d0xa460...98b12 n->0x2c96...23f02
_SYNTH = {
    KIDS["mention"]: _child_lsof(KIDS["mention"], "0x45d5e09e767600d7", "0xb3d65210023ef95e"),
    KIDS["bell"]: _child_lsof(KIDS["bell"], "0x2c96cfcf3bc23f02", "0xa460b04221d98b12"),
}


# Captured at import so tests that patch pwd.getpwuid do not break the fake.
_SCOPE_TOKENS = {pwd.getpwuid(os.getuid()).pw_name, str(os.getuid())}


def _scope_tokens() -> set[str]:
    """Values `-U` may carry for this process: the login name or the numeric uid."""
    return _SCOPE_TOKENS


def _default_lsof() -> dict:
    return {SUP: _fx("lsof_supervisor_pid65874.txt"),
            KIDS["updates"]: _fx("lsof_child_pid65876_fd0-2.txt"),
            **_SYNTH}


class FakeRunner:
    """Models ps (honouring `-U <uid>` scoping: without it, foreign rows are
    listed too) and lsof (by `-p <pid>`)."""

    def __init__(self, ps_text=None, lsof=None, foreign_rows=(), ps_error=None,
                 lsof_error=None, lsof_rc=0, lsof_rc_stdout=None, ps_rc=0):
        self.ps_text = _fx("ps_snapshot.txt") if ps_text is None else ps_text
        self.lsof = _default_lsof() if lsof is None else lsof
        self.foreign_rows = list(foreign_rows)
        self.ps_error, self.lsof_error, self.lsof_rc = ps_error, lsof_error, lsof_rc
        # lsof_rc_stdout: fn(pid) -> stdout returned WITH the non-zero lsof rc
        # (default: empty). ps_rc: exit code returned with the ps text.
        self.lsof_rc_stdout, self.ps_rc = lsof_rc_stdout, ps_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "ps":
            if self.ps_error is not None:
                raise self.ps_error
            text = self.ps_text
            scoped = "-U" in argv and argv[argv.index("-U") + 1] in _scope_tokens()
            if not scoped:
                text = text.rstrip("\n") + "\n" + "\n".join(self.foreign_rows) + "\n"
            return self.ps_rc, text
        if argv[0] == "lsof":
            if self.lsof_error is not None:
                raise self.lsof_error
            pid = int(argv[argv.index("-p") + 1])
            if self.lsof_rc:
                return self.lsof_rc, (self.lsof_rc_stdout(pid) if self.lsof_rc_stdout else "")
            return (0, self.lsof[pid]) if pid in self.lsof else (1, "")
        raise AssertionError(f"unexpected command {argv!r}")


def _write_status(tmp_path: Path, pids, age: float) -> Path:
    p = tmp_path / "status.json"
    p.write_text(json.dumps({"generated_at": time.time() - age, "status_interval": 3600,
                             "children": {n: {"pid": v} for n, v in pids.items()}}))
    return p


def run(tmp_path, monkeypatch, capsys, runner, *, pidfile_pid=SUP, status_pids=None,
        status_age=0.0, no_pidfile=False, proc_root=None):
    mod = _load_module()
    monkeypatch.setattr(mod, "PROC_ROOT", str(proc_root or tmp_path / "no-such-proc"))
    monkeypatch.setattr(mod, "CHECK_RUNNER", runner)
    pidfile = tmp_path / "sup.pid"
    if not no_pidfile:
        pidfile.write_text(str(pidfile_pid))
    status = _write_status(tmp_path, status_pids or KIDS, status_age)
    with pytest.raises(SystemExit) as ei:
        mod.main(["--check", "--pidfile", str(pidfile), "--status-file", str(status)])
    return subprocess.CompletedProcess([], ei.value.code, capsys.readouterr().out, "")


def ps_with(*edits, drop=(), add=()):
    """The real ps fixture with edits [(pid, fn(line) -> line)], dropped pids,
    and appended rows."""
    out = []
    for line in _fx("ps_snapshot.txt").splitlines():
        pid = int(line.split()[0])
        if pid in drop:
            continue
        for p, fn in edits:
            if p == pid:
                line = fn(line)
        out.append(line)
    out.extend(add)
    return "\n".join(out) + "\n"


# --- C2: backend selection -------------------------------------------------
def test_proc_backend_selected_when_proc_root_exists(tmp_path, monkeypatch, capsys):
    """/proc present -> the proc backend; the ps/lsof runner is NEVER called.
    Mutation: select the ps backend unconditionally -> runner called -> red."""
    root = tmp_path / "fakeproc"
    root.mkdir()

    def boom(argv):
        raise AssertionError(f"runner called on the proc path: {argv!r}")

    res = run(tmp_path, monkeypatch, capsys, boom, no_pidfile=True, proc_root=root)
    assert res.returncode == 2 and "supervisor not armed" in res.stdout


def test_no_proc_ps_usable_no_pidfile_exit_2_via_ps_backend(tmp_path, monkeypatch, capsys):
    """The old no-/proc scenario's NEW behaviour: ps backend, pidfile missing
    -> exit 2 `supervisor not armed`, one line, and ps WAS consulted.
    Mutation: keep the old exit-3 gate -> red."""
    r = FakeRunner()
    res = run(tmp_path, monkeypatch, capsys, r, no_pidfile=True)
    assert res.returncode == 2, res.stdout
    lines = res.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith("CHECK FAIL supervisor not armed")
    assert any(c[0] == "ps" for c in r.calls)


@pytest.mark.parametrize("how", ["oserror", "rc"])
def test_neither_backend_works_exit_3(tmp_path, monkeypatch, capsys, how):
    """No /proc AND ps unusable (cannot run / exits nonzero) -> exit 3, ONE
    line, never advising the three-Monitor check.
    Mutation: exit 3 on a missing /proc alone, or keep the old message -> red."""
    if how == "oserror":
        r = FakeRunner(ps_error=FileNotFoundError("ps"))
    else:
        def r(argv):
            return 127, ""
    res = run(tmp_path, monkeypatch, capsys, r, no_pidfile=True)
    assert res.returncode == 3, res.stdout
    lines = res.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith("CHECK FAIL unsupported platform")
    assert "three-Monitor" not in lines[0]


# --- C1 / C3: real fixtures ------------------------------------------------
def test_healthy_real_supervisor_and_updates_child_pass_five_lines(tmp_path, monkeypatch, capsys):
    """Control: real ps + real supervisor lsof + real updates-child lsof (and
    synthetic mention/bell) -> exit 0, five lines, `CHECK PASS 0/4 failed`.
    Mutation: never reaches PASS, or matches argv[0] (the real supervisor
    argv[0] is `Python`, capitalised) -> red."""
    res = run(tmp_path, monkeypatch, capsys, FakeRunner())
    assert res.returncode == 0, res.stdout
    assert _parse(res)["summary"] == "CHECK PASS 0/4 failed"


def test_real_captures_only_updates_passes_others_fail(tmp_path, monkeypatch, capsys):
    """Only the updates child has a real lsof capture; the others get none
    (lsof finds nothing) and must FAIL rather than pass by default.
    Mutation: a child with no lsof record passes -> red."""
    lsof = _default_lsof()
    del lsof[KIDS["mention"]], lsof[KIDS["bell"]]
    res = run(tmp_path, monkeypatch, capsys, FakeRunner(lsof=lsof))
    out = _parse(res)
    assert res.returncode == 1
    assert out["updates"][0] == "PASS"
    assert out["mention"][0] == "FAIL" and out["bell"][0] == "FAIL"


def test_one_ps_snapshot_uid_scoped_no_pgrep(tmp_path, monkeypatch, capsys):
    """ONE ps call, scoped `-U <getuid>`, columns pid,ppid,stat,args; no pgrep.
    Mutation: per-pid ps calls / pgrep -f / unscoped ps -> red."""
    r = FakeRunner()
    run(tmp_path, monkeypatch, capsys, r)
    ps_calls = [c for c in r.calls if c[0] == "ps"]
    assert len(ps_calls) == 1
    c = ps_calls[0]
    assert c[c.index("-U") + 1] in _scope_tokens()
    assert "pid=,ppid=,stat=,args=" in "".join(c).replace(" ", "")
    assert not any("pgrep" in " ".join(x) for x in r.calls)


def test_foreign_uid_row_with_ppid_equal_supervisor_ignored(tmp_path, monkeypatch, capsys):
    """F2: proves the ps ARGV carries the uid scope (`-U`), not that macOS
    honours it: the fake drops foreign rows only when `-U` names this user, so
    a foreign row with ppid == supervisor pid would show up as an extra child
    without it. Constructed case; real-OS behaviour is unverified here.
    Mutation: drop the uid scope from the ps argv -> foreign row listed as an
    extra child -> red."""
    foreign = (f"65990 {SUP} S    /usr/bin/python3 /Users/other/umwelt-bell.py "
               "x https://world.example:5006 c 5.0")
    res = run(tmp_path, monkeypatch, capsys, FakeRunner(foreign_rows=[foreign]))
    assert res.returncode == 0, res.stdout
    assert "65990" not in res.stdout
    assert _parse(res)["summary"] == "CHECK PASS 0/4 failed"


def test_zombie_child_fails(tmp_path, monkeypatch, capsys):
    """F3: stat `Z` = not alive (named child), and not counted as extra.
    Mutation: ignore stat -> updates PASS -> red."""
    ps = ps_with((KIDS["updates"], lambda ln: ln.replace(" S    ", " Z    ", 1)))
    res = run(tmp_path, monkeypatch, capsys, FakeRunner(ps_text=ps))
    out = _parse(res)
    assert res.returncode == 1
    assert out["updates"][0] == "FAIL" and "not alive" in out["updates"][1]
    assert "extra child" not in out["summary"]


def test_zombie_unnamed_child_not_extra(tmp_path, monkeypatch, capsys):
    """A zombie child the status file does not name is not an extra child.
    Mutation: extras include Z rows -> red."""
    z = f"65991 {SUP} Z    (forum-mention-monitor)"
    res = run(tmp_path, monkeypatch, capsys, FakeRunner(ps_text=ps_with(add=[z])))
    assert res.returncode == 0, res.stdout


def test_wrong_ppid_child_fails_and_real_child_is_extra(tmp_path, monkeypatch, capsys):
    """Status names 65999 (ppid 1) for mention; the true child 65877 is unnamed
    -> mention FAIL `not a child`, summary names `extra child pid=65877`.
    Mutation: skip the ppid check (mention PASS) or drop extras -> red."""
    stray = "65999 1 S    bash /x/forum-mention-monitor.sh --purpose collab-loop"
    lsof = _default_lsof()
    lsof[65999] = _SYNTH[KIDS["mention"]].replace("p65877", "p65999")
    res = run(tmp_path, monkeypatch, capsys,
              FakeRunner(ps_text=ps_with(add=[stray]), lsof=lsof),
              status_pids={**KIDS, "mention": 65999})
    out = _parse(res)
    assert res.returncode == 1
    assert out["mention"][0] == "FAIL" and "not a child" in out["mention"][1]
    assert "extra child pid=65877" in out["summary"]


def test_extra_child_alone_fails_verdict(tmp_path, monkeypatch, capsys):
    """All items PASS; one unnamed live child of the supervisor.
    Mutation: drop `and not extras` from the verdict -> red."""
    extra = f"65992 {SUP} S    sleep 1000"
    res = run(tmp_path, monkeypatch, capsys, FakeRunner(ps_text=ps_with(add=[extra])))
    out = _parse(res)
    assert res.returncode == 1
    assert out["summary"].startswith("CHECK FAIL 0/4 failed; extra child pid=65992")


def test_child_wrong_argv_fails_on_argv(tmp_path, monkeypatch, capsys):
    """Right ppid, wrong script name in args -> FAIL on argv. Mutation: skip
    the argv check -> red."""
    ps = ps_with((KIDS["mention"], lambda ln: "65877 65874 S    bash /x/other.sh --purpose collab-loop"))
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(ps_text=ps)))
    assert out["mention"][0] == "FAIL"
    assert "argv does not identify forum-mention-monitor" in out["mention"][1]


# --- C4 / F1: the pipe rule ------------------------------------------------
def test_child_pipe_peer_not_held_by_supervisor_fails(tmp_path, monkeypatch, capsys):
    """Child fd1 is a PIPE but the supervisor holds no matching end.
    Mutation: skip the supervisor-held check -> PASS -> red."""
    lsof = _default_lsof()
    lsof[KIDS["updates"]] = _child_lsof(KIDS["updates"], "0xdeadbeefdeadbeef", "0xfeedfacefeedface")
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(lsof=lsof)))
    assert out["updates"][0] == "FAIL" and "not held" in out["updates"][1]


@pytest.mark.parametrize("which", ["device-only", "peer-only"])
def test_pipe_match_requires_both_directions(tmp_path, monkeypatch, capsys, which):
    """F1, plausible-wrong implementation: only ONE direction matches.
    device-only: the child's device equals supervisor fd4's peer but the child's
    peer is wrong. peer-only: the child's peer equals fd4's device but the
    child's device is wrong. Both must FAIL.
    Mutation: match on either single direction -> red."""
    if which == "device-only":
        bad = _child_lsof(KIDS["updates"], "0xcadbd948a8da3fb1", "0xaaaaaaaaaaaaaaaa")
    else:
        bad = _child_lsof(KIDS["updates"], "0xbbbbbbbbbbbbbbbb", "0xf77e69700b296caa")
    lsof = _default_lsof()
    lsof[KIDS["updates"]] = bad
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(lsof=lsof)))
    assert out["updates"][0] == "FAIL" and "not held" in out["updates"][1]


def test_child_fd1_not_a_pipe_fails(tmp_path, monkeypatch, capsys):
    """Child fd1 is a regular file -> FAIL `not a pipe`.
    Mutation: skip the type check -> red."""
    lsof = _default_lsof()
    lsof[KIDS["updates"]] = _fx("lsof_child_pid65876_fd0-2.txt").replace("f1\ntPIPE", "f1\ntREG")
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(lsof=lsof)))
    assert out["updates"][0] == "FAIL" and "not a pipe" in out["updates"][1]


@pytest.mark.parametrize("kind", ["missing", "rc"])
def test_lsof_unavailable_pipe_items_fail_naming_lsof(tmp_path, monkeypatch, capsys, kind):
    """lsof absent (OSError) or failing (rc != 0): the supervisor item and the three
    pipe items FAIL and the reason names lsof; never PASS, never skipped.
    Mutation: treat missing/erroring lsof as PASS -> red."""
    r = FakeRunner(lsof_error=FileNotFoundError("lsof")) if kind == "missing" else FakeRunner(lsof_rc=1)
    res = run(tmp_path, monkeypatch, capsys, r)
    out = _parse(res)
    assert res.returncode == 1
    for n in ("supervisor", "updates", "mention", "bell"):
        assert out[n][0] == "FAIL" and "lsof" in out[n][1], out[n]


# --- R1: `-U` takes the login NAME (BSD ps acceptance of a number unverified) --
def _ps_u_value(tmp_path, monkeypatch, capsys):
    r = FakeRunner()
    run(tmp_path, monkeypatch, capsys, r)
    c = next(x for x in r.calls if x[0] == "ps")
    return c[c.index("-U") + 1]


def test_ps_scope_is_login_name_when_pwd_resolves(tmp_path, monkeypatch, capsys):
    """pwd resolves -> `-U <login name>`. Mutation: always the numeric uid -> red."""
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: type("P", (), {"pw_name": "seatuser"})())
    assert _ps_u_value(tmp_path, monkeypatch, capsys) == "seatuser"


def test_ps_scope_falls_back_to_numeric_uid_when_pwd_raises(tmp_path, monkeypatch, capsys):
    """pwd raises KeyError -> `-U <numeric uid>`. Mutation: let the error escape
    or send an empty/None name -> red."""
    def nope(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", nope)
    assert _ps_u_value(tmp_path, monkeypatch, capsys) == str(os.getuid())


# --- R2: narrow non-zero-rc tolerance (lsof only; ps non-zero is unusable) -------------------------------------
def _real_stdout(pid):
    return _default_lsof().get(pid, "")


def test_lsof_nonzero_rc_with_own_pid_record_passes_pipe_item(tmp_path, monkeypatch, capsys):
    """lsof exits 1 (e.g. `can't stat()` warnings) but its stdout is the REAL
    capture with a `p<pid>` record: parse it; the updates item still needs the
    full pipe match. Mutation: any non-zero rc -> FAIL (no tolerance) -> red."""
    r = FakeRunner(lsof_rc=1, lsof_rc_stdout=_real_stdout)
    out = _parse(run(tmp_path, monkeypatch, capsys, r))
    assert out["updates"][0] == "PASS", out["updates"]


def test_lsof_nonzero_rc_with_record_still_needs_pipe_match(tmp_path, monkeypatch, capsys):
    """Tolerance does not make a false PASS: rc=1 + own-pid record whose pipe is
    not held by the supervisor still FAILs. Mutation: PASS on tolerated rc -> red."""
    bad = _child_lsof(KIDS["updates"], "0xdeadbeefdeadbeef", "0xfeedfacefeedface")
    r = FakeRunner(lsof_rc=1, lsof_rc_stdout=lambda pid: bad if pid == KIDS["updates"] else _real_stdout(pid))
    out = _parse(run(tmp_path, monkeypatch, capsys, r))
    assert out["updates"][0] == "FAIL" and "not held" in out["updates"][1]


def test_lsof_nonzero_rc_empty_stdout_fails(tmp_path, monkeypatch, capsys):
    """rc=1 + empty stdout -> FAIL naming `lsof exited 1`.
    Mutation: accept any non-zero rc regardless of output -> red."""
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(lsof_rc=1)))
    assert out["updates"][0] == "FAIL" and "lsof exited 1" in out["updates"][1]


def test_lsof_nonzero_rc_other_pid_output_fails(tmp_path, monkeypatch, capsys):
    """rc=1 + a record for a DIFFERENT pid: not the requested pid's data ->
    FAIL `lsof exited 1`. Mutation: accept any non-empty output -> red."""
    other = _child_lsof(99999, "0xcadbd948a8da3fb1", "0xf77e69700b296caa")
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(lsof_rc=1, lsof_rc_stdout=lambda pid: other)))
    for n in ("supervisor", "updates", "mention", "bell"):
        assert out[n][0] == "FAIL" and "lsof exited 1" in out[n][1], out[n]


def test_lsof_nonzero_rc_near_miss_record_fails(tmp_path, monkeypatch, capsys):
    """rc=1 + stdout whose only record is `p<pid>0` (a different pid sharing the
    digits as a prefix), no exact `p<pid>` line -> FAIL `lsof exited 1`.
    Mutation: substring match (`f"p{pid}" in out`) instead of exact line -> red."""
    def near(pid):
        return _child_lsof(int(f"{pid}0"), "0x45d5e09e767600d7", "0xb3d65210023ef95e")

    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(lsof_rc=1, lsof_rc_stdout=near)))
    for n in ("supervisor", "updates", "mention", "bell"):
        assert out[n][0] == "FAIL" and "lsof exited 1" in out[n][1], out[n]


def test_ps_nonzero_rc_with_rows_is_unusable_exit_3(tmp_path, monkeypatch, capsys):
    """ps exits non-zero: the table is not trusted even when it parses to rows
    (completeness matters: a truncated table would under-report extra children)
    -> exit 3, same as no usable ps.
    Mutation: tolerate a non-zero ps rc when rows parse -> exit 0 -> red."""
    res = run(tmp_path, monkeypatch, capsys, FakeRunner(ps_rc=1))
    assert res.returncode == 3, res.stdout
    assert "ps is unusable" in res.stdout


# --- supervisor item -------------------------------------------------------
def test_supervisor_pidfile_names_the_shell_wrapper_fails(tmp_path, monkeypatch, capsys):
    """pidfile names the `/bin/zsh -c ... monitor-supervisor.py ...` wrapper:
    the script name sits inside a shell string and argv[0] is not python.
    Mutation: substring match on the script name -> PASS -> red."""
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(), pidfile_pid=65872))
    assert out["supervisor"][0] == "FAIL" and "not a monitor-supervisor" in out["supervisor"][1]


def test_supervisor_not_in_snapshot_fails_five_lines(tmp_path, monkeypatch, capsys):
    """Supervisor pid absent from the snapshot -> FAIL not alive, five lines.
    Mutation: assume alive -> red."""
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(ps_text=ps_with(drop={SUP}))))
    assert out["supervisor"][0] == "FAIL"
    assert f"pid {SUP} not alive" in out["supervisor"][1], out["supervisor"]


def test_child_reason_names_supervisor_not_alive_once(tmp_path, monkeypatch, capsys):
    """Supervisor not alive: a child's pipe reason reads `supervisor not alive`
    exactly once, never `supervisor supervisor not alive`.
    Mutation: restore the sentinel text `supervisor not alive` -> doubled -> red."""
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(ps_text=ps_with(drop={SUP}))))
    reason = out["updates"][1]
    assert reason.count("supervisor not alive") == 1, reason
    assert "supervisor supervisor" not in reason, reason


def test_supervisor_stdout_not_a_socket_fails(tmp_path, monkeypatch, capsys):
    """Supervisor fd1 is a regular file (lsof `tREG`), not the harness unix
    socket -> FAIL naming the type. Mutation: skip the fd1 check -> red."""
    lsof = _default_lsof()
    lsof[SUP] = _fx("lsof_supervisor_pid65874.txt").replace("f1\ntunix", "f1\ntREG")
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(lsof=lsof)))
    assert out["supervisor"][0] == "FAIL" and "REG" in out["supervisor"][1]


def test_stale_status_file_shared_logic(tmp_path, monkeypatch, capsys):
    """Staleness is the shared logic: all three children FAIL `stale`.
    Mutation: ps backend ignores staleness -> red."""
    out = _parse(run(tmp_path, monkeypatch, capsys, FakeRunner(), status_age=999999))
    for n in ("updates", "mention", "bell"):
        assert out[n][0] == "FAIL" and "stale" in out[n][1]


def test_fixtures_carry_only_placeholder_identity():
    """Pinned fixtures use the placeholder seat home and world URL only: no other
    /Users/<x>, no tailnet host, no URL host but world.example (patterns only)."""
    text = "".join(p.read_text() for p in FIX.iterdir())
    assert "/Users/seat" in text and "https://world.example:5006" in text
    assert not re.search(r"/Users/(?!seat\b)[^/\s]+", text)
    assert not re.search(r"[\w.-]+\.ts\.net", text)
    hosts = set(re.findall(r"https?://([^/:\s\"']+)", text))
    assert hosts <= {"world.example"}, hosts
