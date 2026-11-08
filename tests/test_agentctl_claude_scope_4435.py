"""Tests for `agentctl __scope-exec` (engram-alpha#4435).

Why the verb exists: on 2026-11-07 the kernel OOM-killed ONE process in a
seat's session cgroup and systemd's OOMPolicy=stop then stopped the WHOLE unit.
A transient user scope created with `-p OOMPolicy=continue` keeps its other
processes alive through an OOM kill. OOMPolicy is creation-time only, so every
Claude launch goes through `agentctl __scope-exec <seat> <cmd> [args...]`,
which execs the command inside `agent-claude-<seat>.scope`.

Contract under test (the cell names match the spec's T1..T9):

  1. Not a systemd host (no systemd-run on PATH): exec the command, NOTHING
     on stderr, systemctl never called.
  2. Invalid seat name: exactly one WARNING line naming the seat, then exec.
  3. XDG_RUNTIME_DIR unset: if <base>/<uid> is a directory, export it and go
     on; otherwise exactly one WARNING naming XDG_RUNTIME_DIR, then exec.
  4. User manager unreachable (the ONE probe exits non-zero): one WARNING with
     the first stderr line, then exec.
  5. Name collision (probe prints active/activating/reloading/deactivating):
     one WARNING naming the scope, then exec. One systemctl call, no retry.
  6. Otherwise: exec systemd-run with the fixed argv; the child's argv arrives
     byte-for-byte.
  7. Exit status is the child's. Never prompt, never sleep.

The prologue text is NOT touched by this change: four launch sites run it
through `sudo -i`, which backslash-escapes braces and parens, so it must stay
free of `${}`/`$()` and on one line. Cells below pin that.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

AGENTCTL = Path(__file__).resolve().parent.parent / "tools" / "agentctl"
BASH = shutil.which("bash") or "/bin/bash"

WARN_PREFIX = "[agentctl] WARNING: session NOT in a protected scope ("
WARN_SUFFIX = "); launching unconfined."

# Tools the verb and agentctl's start-up may need, linked into a private PATH
# dir so "no systemd-run on PATH" can be tested without hiding the real tools.
_BASE_TOOLS = ("id", "cat", "head", "sed", "readlink", "dirname", "grep", "tr",
               "stat", "mkdir", "env", "sleep")


def _exe(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


_FAKE_SYSTEMD_RUN = r"""#!/bin/bash
printf '%s\0' "$@" >> "$FAKE_REC/sdrun.argv"
echo called >> "$FAKE_REC/sdrun.calls"
printf '%s' "${XDG_RUNTIME_DIR-<unset>}" > "$FAKE_REC/sdrun.xdg"
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""

_FAKE_SYSTEMCTL = r"""#!/bin/bash
echo "$*" >> "$FAKE_REC/systemctl.calls"
n=$(grep -c '' "$FAKE_REC/systemctl.calls")
printf '%s\0' "$@" > "$FAKE_REC/systemctl.argv.$n"
printf '%s' "${XDG_RUNTIME_DIR-<unset>}" > "$FAKE_REC/systemctl.xdg.$n"
case "$*" in
  *ControlGroup*)
    [ -n "${FAKE_CG_OUT-}" ] && printf '%s\n' "$FAKE_CG_OUT"
    exit "${FAKE_CG_RC:-0}" ;;
esac
[ -n "${FAKE_SC_OUT-}" ] && printf '%s\n' "$FAKE_SC_OUT"
[ -n "${FAKE_SC_ERR-}" ] && printf '%s\n' "$FAKE_SC_ERR" >&2
exit "${FAKE_SC_RC:-0}"
"""

_CHILD = r"""#!/bin/bash
printf '%s\0' "$@" > "$FAKE_REC/child.argv"
echo ran > "$FAKE_REC/child.ran"
exit "${FAKE_CHILD_RC:-0}"
"""


class Rig:
    """A private PATH dir of fakes plus a recording dir, per test."""

    def __init__(self, tmp: Path, *, systemd_run: bool = True):
        self.tmp = tmp
        self.rec = tmp / "rec"
        self.rec.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for t in _BASE_TOOLS:
            real = shutil.which(t)
            if real:
                (self.bin / t).symlink_to(real)
        if systemd_run:
            _exe(self.bin / "systemd-run", _FAKE_SYSTEMD_RUN)
        _exe(self.bin / "systemctl", _FAKE_SYSTEMCTL)
        # Platform is a test input: the verb asks `uname -s` only to decide
        # whether a missing systemd-run is expected (macOS) or a fault (Linux).
        _exe(self.bin / "uname", '#!/bin/bash\necho "${FAKE_UNAME:-Linux}"\n')
        self.child = _exe(tmp / "child", _CHILD)
        self.run_dir = tmp / "run"
        self.run_dir.mkdir()

    def run(self, seat: str, *args: str, sc_out="inactive", sc_err="", sc_rc=0,
            child_rc=0, xdg="set", base: Path | None = None, uname="Linux",
            cg_out="", cg_rc=0, cg_root: Path | None = None):
        env = {
            "FAKE_UNAME": uname,
            "PATH": str(self.bin),
            "HOME": str(self.tmp / "home"),
            "FAKE_REC": str(self.rec),
            "FAKE_SC_OUT": sc_out,
            "FAKE_SC_ERR": sc_err,
            "FAKE_SC_RC": str(sc_rc),
            "FAKE_CHILD_RC": str(child_rc),
            "FAKE_CG_OUT": cg_out,
            "FAKE_CG_RC": str(cg_rc),
        }
        if cg_root is not None:
            env["ENGRAM_CGROUP_ROOT"] = str(cg_root)
        if xdg == "set":
            env["XDG_RUNTIME_DIR"] = str(self.run_dir)
        if base is not None:
            env["ENGRAM_RUNTIME_BASE"] = str(base)
        return subprocess.run(
            [BASH, str(AGENTCTL), "__scope-exec", seat, str(self.child), *args],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def sdrun_argv(self):
        f = self.rec / "sdrun.argv"
        return f.read_bytes().split(b"\0")[:-1] if f.exists() else None

    def sdrun_calls(self) -> int:
        f = self.rec / "sdrun.calls"
        return len(f.read_text().splitlines()) if f.exists() else 0

    def systemctl_calls(self) -> list[str]:
        f = self.rec / "systemctl.calls"
        return f.read_text().splitlines() if f.exists() else []

    def systemctl_argvs(self) -> list[list[bytes]]:
        """Exact argv of each systemctl call, in call order."""
        out = []
        for i in range(1, len(self.systemctl_calls()) + 1):
            out.append((self.rec / f"systemctl.argv.{i}").read_bytes().split(b"\0")[:-1])
        return out

    def systemctl_xdgs(self) -> list[str]:
        """XDG_RUNTIME_DIR each systemctl call saw ('<unset>' if absent)."""
        return [(self.rec / f"systemctl.xdg.{i}").read_text()
                for i in range(1, len(self.systemctl_calls()) + 1)]

    def child_argv(self):
        f = self.rec / "child.argv"
        return f.read_bytes().split(b"\0")[:-1] if f.exists() else None

    def child_ran(self) -> bool:
        return (self.rec / "child.ran").exists()


def _warn_lines(stderr: str) -> list[str]:
    return [l for l in stderr.splitlines() if l]


def _assert_one_warning(proc, reason_fragment: str) -> str:
    lines = _warn_lines(proc.stderr)
    assert len(lines) == 1, proc.stderr
    line = lines[0]
    assert line.startswith(WARN_PREFIX) and line.endswith(WARN_SUFFIX), line
    reason = line[len(WARN_PREFIX):-len(WARN_SUFFIX)]
    assert reason_fragment in reason, line
    return reason


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# --- T1: the scope path ----------------------------------------------------

def test_t1_scope_path_argv_exact_and_child_argv_byte_identical(rig):
    proc = rig.run("sol", "a b", "", "-x", child_rc=7, sc_out="inactive")
    assert proc.returncode == 7, proc.stderr
    assert proc.stderr == ""
    assert rig.sdrun_calls() == 1
    assert rig.sdrun_argv() == [
        b"--user", b"--scope", b"--quiet", b"--collect",
        b"--unit=agent-claude-sol", b"-p", b"OOMPolicy=continue", b"--",
        str(rig.child).encode(), b"a b", b"", b"-x",
    ]
    assert rig.child_argv() == [b"a b", b"", b"-x"]
    assert len(rig.systemctl_calls()) == 1
    assert "agent-claude-sol.scope" in rig.systemctl_calls()[0]


# --- T2/T3: XDG_RUNTIME_DIR -------------------------------------------------

def test_t2_xdg_unset_runtime_dir_present_is_exported(tmp_path):
    rig = Rig(tmp_path)
    base = tmp_path / "base"
    (base / str(os.getuid())).mkdir(parents=True)
    proc = rig.run("sol", xdg="unset", base=base)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert rig.sdrun_calls() == 1
    assert (rig.rec / "sdrun.xdg").read_text() == str(base / str(os.getuid()))


def test_t3_xdg_unset_runtime_dir_missing_warns_and_runs_unconfined(tmp_path):
    rig = Rig(tmp_path)
    base = tmp_path / "nobase"
    proc = rig.run("sol", "x", xdg="unset", base=base, child_rc=7)
    assert proc.returncode == 7
    reason = _assert_one_warning(proc, "XDG_RUNTIME_DIR")
    assert reason == f"XDG_RUNTIME_DIR unset and {base}/{os.getuid()} missing"
    assert rig.sdrun_calls() == 0
    assert rig.systemctl_calls() == []
    assert rig.child_ran() and rig.child_argv() == [b"x"]


# --- T4: user manager unreachable ------------------------------------------

def test_t4_systemctl_failure_warns_with_first_stderr_line(rig):
    proc = rig.run("sol", sc_rc=1, sc_out="",
                   sc_err="Failed to connect to bus: No medium found\nsecond line")
    assert proc.returncode == 0
    reason = _assert_one_warning(proc, "user manager unreachable: Failed to connect to bus")
    assert reason == "user manager unreachable: Failed to connect to bus: No medium found"
    assert "second line" not in proc.stderr
    assert rig.sdrun_calls() == 0
    assert len(rig.systemctl_calls()) == 1
    assert rig.child_ran()


# --- T5: name collision -----------------------------------------------------

@pytest.mark.parametrize("state", ["active", "activating", "reloading", "deactivating"])
def test_t5_collision_warns_once_and_probes_once(rig, state):
    proc = rig.run("sol", "y", sc_out=state, child_rc=7)
    assert proc.returncode == 7
    reason = _assert_one_warning(proc, f"agent-claude-sol.scope already {state}")
    assert reason == f"agent-claude-sol.scope already {state}"
    assert rig.sdrun_calls() == 0
    # probe + the one ControlGroup call, on the collision branch only
    assert len(rig.systemctl_calls()) == 2
    assert "ControlGroup" in rig.systemctl_calls()[1]
    assert rig.child_argv() == [b"y"]


@pytest.mark.parametrize("state", ["inactive", "failed", "maintenance", "refreshing"])
def test_t5b_non_busy_states_take_the_scope_path(rig, state):
    proc = rig.run("sol", sc_out=state)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert rig.sdrun_calls() == 1


# --- F2: the state is the last line that is exactly an ActiveState word -------

def test_f2a_leading_noise_then_active_is_a_collision(rig):
    proc = rig.run("sol", "y", sc_out="Warning: x\nactive")
    _assert_one_warning(proc, "agent-claude-sol.scope already active")
    assert rig.sdrun_calls() == 0
    assert len(rig.systemctl_calls()) == 2
    assert rig.child_argv() == [b"y"]


def test_f2b_active_then_trailing_noise_is_a_collision(rig):
    proc = rig.run("sol", "y", sc_out="active\nWarning: y")
    _assert_one_warning(proc, "agent-claude-sol.scope already active")
    assert rig.sdrun_calls() == 0
    assert len(rig.systemctl_calls()) == 2
    assert rig.child_argv() == [b"y"]


@pytest.mark.parametrize("out", ["Warning: z", ""])
def test_f2c_no_state_word_is_an_unreadable_probe(rig, out):
    proc = rig.run("sol", "y", sc_out=out)
    assert proc.returncode == 0
    reason = _assert_one_warning(proc, "user manager probe unreadable")
    assert reason == f"user manager probe unreadable: {out}"
    assert rig.sdrun_calls() == 0
    assert len(rig.systemctl_calls()) == 1
    assert rig.child_argv() == [b"y"]


# --- T6: not a systemd host -------------------------------------------------

def test_t6_no_systemd_run_on_macos_is_silent_and_never_probes(tmp_path):
    rig = Rig(tmp_path, systemd_run=False)
    proc = rig.run("sol", "z", child_rc=7, uname="Darwin")
    assert proc.returncode == 7
    assert proc.stderr == ""
    assert rig.systemctl_calls() == []
    assert rig.child_argv() == [b"z"]


def test_t6b_no_systemd_run_on_linux_warns_once_and_runs_unconfined(tmp_path):
    """On Linux a missing systemd-run is a fault (broken PATH), not a platform
    fact, so it must not drop the protection silently (Zephyr, #4473 read)."""
    rig = Rig(tmp_path, systemd_run=False)
    proc = rig.run("sol", "z", child_rc=7, uname="Linux")
    assert proc.returncode == 7
    _assert_one_warning(proc, "systemd-run not found on PATH")
    assert rig.systemctl_calls() == []
    assert rig.child_argv() == [b"z"]


# --- T7: invalid seat -------------------------------------------------------

@pytest.mark.parametrize("seat", ["Sol;rm", "", "-sol", "so l", "sol\nx", "so\x01l"])
def test_t7_invalid_seat_warns_once_and_runs_unconfined(rig, seat):
    proc = rig.run(seat, "q")
    assert proc.returncode == 0
    lines = _warn_lines(proc.stderr)
    assert len(lines) == 1, proc.stderr
    assert "invalid seat name" in lines[0]
    assert rig.sdrun_calls() == 0
    assert rig.systemctl_calls() == []
    assert rig.child_argv() == [b"q"]
    if seat.isprintable():
        assert f"invalid seat name '{seat}'" in lines[0]


@pytest.mark.parametrize("seat", ["sol_x", "Foo", "_a", "A-b_9"])
def test_t7b_valid_seat_names_reach_systemd_run(rig, seat):
    """systemd unit names allow `_` and uppercase; agentctl accepts `_` in
    agent names elsewhere."""
    proc = rig.run(seat, "q")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert rig.sdrun_calls() == 1
    assert f"--unit=agent-claude-{seat}".encode() in rig.sdrun_argv()
    assert rig.child_argv() == [b"q"]


# --- refuses root ---------------------------------------------------------------

def _userns_root_works() -> bool:
    unshare = shutil.which("unshare")
    if not unshare:
        return False
    try:
        return subprocess.run([unshare, "-r", "true"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _userns_root_works(),
                    reason="`unshare -r true` fails here (no unshare, e.g. macOS, or "
                           "unprivileged user namespaces disabled): no way to get EUID 0")
def test_refuses_to_run_as_root_and_never_execs_the_child(tmp_path):
    """`__` verbs run with no require_root, and this one execs an arbitrary
    command, so it must refuse EUID 0 (exit 2, one ERROR line, no exec).
    Run under a REAL user namespace (`unshare -r`: EUID is 0 inside)."""
    rig = Rig(tmp_path)
    env = {"PATH": str(rig.bin), "HOME": str(tmp_path / "home"),
           "FAKE_REC": str(rig.rec), "FAKE_SC_OUT": "inactive",
           "XDG_RUNTIME_DIR": str(rig.run_dir)}
    proc = subprocess.run(
        [shutil.which("unshare"), "-r", BASH, str(AGENTCTL), "__scope-exec",
         "sol", str(rig.child), "x"],
        capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 2, proc.stderr
    assert proc.stderr.splitlines() == [
        "[agentctl] ERROR: __scope-exec refuses to run as root "
        "(it execs an arbitrary command)"]
    assert not rig.child_ran()
    assert rig.sdrun_calls() == 0 and rig.systemctl_calls() == []


# --- the prologue is NOT touched (sudo -i escapes braces and parens) --------

# sha256 of `agentctl __session-prologue` on dev 4f3270dc. Changing the prologue
# text means re-deciding the sudo -i constraint; do that deliberately.
_PROLOGUE_SHA256 = "760ae66c52b2f7c3533db8dd7885beea358f5681081aaa6dcce3a36ce1834b90"


def _prologue() -> str:
    proc = subprocess.run([BASH, str(AGENTCTL), "__session-prologue"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_prologue_text_is_unchanged_from_dev():
    assert hashlib.sha256(_prologue().encode()).hexdigest() == _PROLOGUE_SHA256


def test_prologue_is_one_line_without_brace_or_paren_expansions():
    text = _prologue()
    assert text.count("\n") == 1 and text.endswith("\n")
    assert "${" not in text and "$(" not in text
    assert "_engram_scope_exec" not in text


# --- launch strings handed to `sudo -i bash -c` ------------------------------

_FAKE_ID = ('#!/bin/sh\nif [ "$1" = "-un" ]; then echo agent-zed; '
            'elif [ "$1" = "-u" ]; then exec /usr/bin/id -u; else exit 0; fi\n')

# Records each call's argv (NUL-separated) to $FAKE_REC/sudo.<n>; the
# `command -v` probe succeeds or fails per SUDO_BINARY_RC, `test -x` succeeds.
_FAKE_SUDO = r"""#!/bin/bash
n=$(ls "$FAKE_REC" | grep -c '^sudo\.')
printf '%s\0' "$@" > "$FAKE_REC/sudo.$n"
prev=""
for a in "$@"; do
  if [ "$prev" = "-c" ]; then
    case "$a" in
      "command -v"*) exit "${SUDO_BINARY_RC:-0}" ;;
      "test -x"*) exit 0 ;;
    esac
  fi
  prev="$a"
done
exit 0
"""


def _sudo_launch_strings(tmp_path: Path, *, binary_on_path: bool, cd: bool) -> str:
    """Run the real cmd_session NON-as-self with a fake sudo; return the launch
    string (the `bash -c` argument carrying `exec`)."""
    rec = tmp_path / "rec"
    rec.mkdir()
    bindir = tmp_path / "bin"
    _exe(bindir / "id", _FAKE_ID)
    _exe(bindir / "sudo", _FAKE_SUDO)
    for t in ("cat", "head", "sed", "readlink", "dirname", "grep", "tr", "ls",
              "mkdir", "stat", "env", "sort", "wc", "uniq"):
        real = shutil.which(t)
        if real and not (bindir / t).exists():
            (bindir / t).symlink_to(real)
    proj = tmp_path / "proj"
    proj.mkdir()
    args = [BASH, str(AGENTCTL), "session", "zed", "--platform", "claude", "--no-share"]
    if cd:
        args += ["--cd", str(proj)]
    args += ["-c"]
    env = {"PATH": str(bindir), "HOME": str(tmp_path / "home"), "FAKE_REC": str(rec),
           "SUDO_BINARY_RC": "0" if binary_on_path else "1"}
    proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    strings = []
    for f in sorted(rec.glob("sudo.*")):
        argv = f.read_bytes().split(b"\0")[:-1]
        for i, a in enumerate(argv):
            if a == b"-c" and i + 1 < len(argv) and b"exec" in argv[i + 1]:
                strings.append(argv[i + 1].decode())
    assert len(strings) == 1, (strings, proc.stderr)
    return strings[0]


@pytest.mark.parametrize("binary_on_path", [True, False])
@pytest.mark.parametrize("cd", [True, False])
def test_sudo_launch_strings_carry_scope_exec_without_expansion_chars(
        tmp_path, binary_on_path, cd):
    s = _sudo_launch_strings(tmp_path, binary_on_path=binary_on_path, cd=cd)
    prologue = _prologue().rstrip("\n")
    assert s.startswith(prologue + ";"), s
    rest = s[len(prologue) + 1:]
    assert f" __scope-exec zed " in rest
    assert str(AGENTCTL.resolve()) in rest
    # Everything the outer shell inserted must be free of $ { } ( ) -- sudo -i
    # backslash-escapes braces and parens. Only the pre-existing quoted "$@"
    # token is allowed to carry a `$`.
    check = rest.replace('"$@"', "")
    for ch in "${}()":
        assert ch not in check, (ch, rest)
    # The exec word is the scope verb for the right target.
    target = '"claude"' if binary_on_path else "~/.runtime/start.sh"
    assert re.search(r"exec \S+ __scope-exec zed " + re.escape(target) + r" ", rest), rest


# --- T9: the as-self e2e goes through systemd-run ----------------------------

def test_t9_as_self_launch_goes_through_scope_unit(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    rec = tmp_path / "rec"
    rec.mkdir()
    bindir = tmp_path / "bin"
    _exe(bindir / "id", _FAKE_ID.replace("agent-zed", "agent-zed"))
    _exe(bindir / "systemd-run", _FAKE_SYSTEMD_RUN)
    _exe(bindir / "systemctl", _FAKE_SYSTEMCTL)
    _exe(bindir / "claude", '#!/bin/sh\necho "ARGV:$*"\n')
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home), "FAKE_REC": str(rec),
           "FAKE_SC_OUT": "inactive", "XDG_RUNTIME_DIR": str(tmp_path)}
    proc = subprocess.run(
        [BASH, str(AGENTCTL), "session", "zed", "--platform", "claude", "--as-self", "-c"],
        capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "ARGV:--continue" in proc.stdout.splitlines()
    argv = (rec / "sdrun.argv").read_bytes().split(b"\0")[:-1]
    assert b"--unit=agent-claude-zed" in argv
    assert b"OOMPolicy=continue" in argv
    assert argv[argv.index(b"--") + 1:] == [b"claude", b"--continue"]


def test_xdg_derivation_uses_bash_uid_not_a_shadowed_id(tmp_path):
    """The runtime dir comes from bash's own $UID, like the root guard: a fake
    `id` first on PATH that lies about the uid must not change the dir."""
    rig = Rig(tmp_path)
    (rig.bin / "id").unlink()  # a symlink to the real id: never write through it
    _exe(rig.bin / "id", '#!/bin/bash\necho 99999\n')
    base = tmp_path / "rt"
    (base / str(os.getuid())).mkdir(parents=True)
    proc = rig.run("sol", "w", xdg="unset", base=base)
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert (rig.rec / "sdrun.xdg").read_text() == str(base / str(os.getuid()))


# --- __scope-stop: agent-session@ stop/restart still ends the session --------
# Sol's #4473 read: claude now lives outside agent-session@<seat>'s cgroup, so a
# unit stop no longer reaches it by itself; the unit's ExecStop calls this verb.

def _run_stop(rig: "Rig", seat: str, *, sc_out="active", sc_rc=0, xdg="set",
              base: Path | None = None):
    env = {"PATH": str(rig.bin), "HOME": str(rig.tmp / "home"), "FAKE_REC": str(rig.rec),
           "FAKE_SC_OUT": sc_out, "FAKE_SC_ERR": "", "FAKE_SC_RC": str(sc_rc)}
    if xdg == "set":
        env["XDG_RUNTIME_DIR"] = str(rig.run_dir)
    if base is not None:
        env["ENGRAM_RUNTIME_BASE"] = str(base)
    return subprocess.run([BASH, str(AGENTCTL), "__scope-stop", seat],
                          capture_output=True, text=True, env=env, timeout=30)


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "reloading"])
def test_scope_stop_stops_a_live_scope(rig, state):
    proc = _run_stop(rig, "sol", sc_out=state)
    assert proc.returncode == 0, proc.stderr
    calls = rig.systemctl_calls()
    assert len(calls) == 2, calls
    assert calls[1] == "--user stop agent-claude-sol.scope"


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_scope_stop_is_a_quiet_noop_when_nothing_runs(rig, state):
    proc = _run_stop(rig, "sol", sc_out=state)
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert len(rig.systemctl_calls()) == 1


@pytest.mark.parametrize("seat", ["Sol;rm", "", "-sol", "so l"])
def test_scope_stop_refuses_invalid_seat_without_calling_systemctl(rig, seat):
    proc = _run_stop(rig, seat)
    assert proc.returncode == 2
    assert "invalid seat name" in proc.stderr
    assert rig.systemctl_calls() == []


def test_scope_stop_refuses_root_under_a_real_user_namespace(rig):
    unshare = shutil.which("unshare")
    if not unshare or subprocess.run([unshare, "-r", "true"], capture_output=True).returncode != 0:
        pytest.skip("unprivileged user namespaces unavailable (unshare -r)")
    env = {"PATH": str(rig.bin), "FAKE_REC": str(rig.rec), "XDG_RUNTIME_DIR": str(rig.run_dir),
           "FAKE_SC_OUT": "active", "FAKE_SC_RC": "0"}
    proc = subprocess.run([unshare, "-r", BASH, str(AGENTCTL), "__scope-stop", "sol"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 2
    assert "refuses to run as root" in proc.stderr
    assert rig.systemctl_calls() == []


def test_agent_session_unit_stops_the_claude_scope():
    """The installed unit's ExecStop must call __scope-stop for its instance,
    prefixed with `-` so a failure never fails the unit's own stop."""
    unit = (AGENTCTL.parent.parent / "deploy" / "autostart" / "agent-session@.service").read_text()
    lines = [l.strip() for l in unit.splitlines() if l.strip().startswith("ExecStop=")]
    assert lines == ["ExecStop=-/usr/local/bin/agentctl __scope-stop %i"], lines


# --- Z1: the unit's ExecStop depends on User=agent-%i ---------------------------

def test_z1_unit_with_scope_stop_execstop_also_has_user_line():
    """Without `User=agent-%i` ExecStop runs as root, `__scope-stop` refuses, the
    `-` prefix swallows the refusal, and the scope survives the unit stop."""
    unit = (AGENTCTL.parent.parent / "deploy" / "autostart" / "agent-session@.service").read_text()
    lines = [l.strip() for l in unit.splitlines()]
    if any(l.startswith("ExecStop=") and "__scope-stop" in l for l in lines):
        assert "User=agent-%i" in lines


# --- Z2 / Z3: __scope-stop edge cases ------------------------------------------

def test_z2_stop_unreadable_probe_warns_and_stops_anyway(rig):
    proc = _run_stop(rig, "sol", sc_out="Warning: z")
    assert proc.returncode == 0, proc.stderr
    lines = _warn_lines(proc.stderr)
    assert lines == ["[agentctl] WARNING: __scope-stop: user manager probe unreadable: "
                     "Warning: z; stopping anyway"], proc.stderr
    assert rig.systemctl_calls()[-1] == "--user stop agent-claude-sol.scope"
    assert len(rig.systemctl_calls()) == 2
    assert rig.systemctl_argvs() == [PROBE_ARGV, STOP_ARGV]
    assert rig.systemctl_xdgs() == [str(rig.run_dir)] * 2


def test_z3_stop_xdg_unset_and_runtime_dir_missing_exits_0_without_systemctl(rig, tmp_path):
    base = tmp_path / "emptybase"
    base.mkdir()
    proc = _run_stop(rig, "sol", xdg="unset", base=base)
    assert proc.returncode == 0, proc.stderr
    lines = _warn_lines(proc.stderr)
    assert lines == [f"[agentctl] WARNING: __scope-stop: XDG_RUNTIME_DIR unset and "
                     f"{base}/{os.getuid()} missing; nothing to stop"], proc.stderr
    assert rig.systemctl_calls() == []


# --- K1: live-claude refusal on the collision branch ---------------------------

PROBE_ARGV = [b"--user", b"show", b"-p", b"ActiveState", b"--value",
              b"agent-claude-sol.scope"]
CG_ARGV = [b"--user", b"show", b"-p", b"ControlGroup", b"--value",
           b"agent-claude-sol.scope"]
STOP_ARGV = [b"--user", b"stop", b"agent-claude-sol.scope"]

REFUSE_PREFIX = "[agentctl] ERROR: claude is already running in agent-claude-"


def _procs_file(rig: "Rig", seat: str, pids) -> Path:
    d = rig.tmp / "cgroot" / "fake" / f"agent-claude-{seat}.scope"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "cgroup.procs"
    f.write_text("".join(f"{p}\n" for p in pids))
    return f


def _spawn_named(rig: "Rig", name: str) -> subprocess.Popen:
    """A long sleep whose /proc comm is `name` (a copy of /bin/sleep)."""
    exe = rig.tmp / "named" / name
    exe.parent.mkdir(exist_ok=True)
    shutil.copy(shutil.which("sleep"), exe)
    exe.chmod(0o755)
    return subprocess.Popen([str(exe), "300"])


def _collide(rig: "Rig", *, seat="sol", **kw):
    kw.setdefault("cg_out", f"/fake/agent-claude-{seat}.scope")
    kw.setdefault("cg_root", rig.tmp / "cgroot")
    return rig.run(seat, "q", sc_out="active", child_rc=7, **kw)


def _assert_collision_warning_and_child_ran(rig, proc, seat="sol"):
    assert proc.returncode == 7, proc.stderr
    _assert_one_warning(proc, f"agent-claude-{seat}.scope already active")
    assert rig.sdrun_calls() == 0
    assert rig.child_argv() == [b"q"]


def test_c1_live_claude_in_the_scope_refuses_with_exit_3(rig):
    p = _spawn_named(rig, "claude")
    try:
        _procs_file(rig, "sol", [p.pid])
        proc = _collide(rig)
        assert proc.returncode == 3, proc.stderr
        assert proc.stdout == ""
        lines = _warn_lines(proc.stderr)
        assert len(lines) == 1, proc.stderr
        assert lines[0] == (
            f"[agentctl] ERROR: claude is already running in agent-claude-sol.scope "
            f"(pid {p.pid}); not starting a second session. Attach with "
            f"'agentctl attach sol' or stop it with 'agentctl __scope-stop sol'.")
        assert not rig.child_ran()
        assert rig.sdrun_calls() == 0
        assert len(rig.systemctl_calls()) == 2
        assert rig.systemctl_argvs() == [PROBE_ARGV, CG_ARGV]
        assert rig.systemctl_xdgs() == [str(rig.run_dir)] * 2
    finally:
        p.kill()
        p.wait()


def test_c2_plain_sleep_in_the_scope_keeps_the_collision_warning(rig):
    p = _spawn_named(rig, "sleep")
    try:
        _procs_file(rig, "sol", [p.pid])
        _assert_collision_warning_and_child_ran(rig, _collide(rig))
    finally:
        p.kill()
        p.wait()


def test_c3_controlgroup_call_failing_keeps_the_collision_warning(rig):
    p = _spawn_named(rig, "claude")
    try:
        _procs_file(rig, "sol", [p.pid])
        _assert_collision_warning_and_child_ran(rig, _collide(rig, cg_rc=1))
        assert len(rig.systemctl_calls()) == 2
    finally:
        p.kill()
        p.wait()


@pytest.mark.parametrize("cg_out", ["", "relative/path"])
def test_c3b_empty_or_non_slash_controlgroup_keeps_the_collision_warning(rig, cg_out):
    p = _spawn_named(rig, "claude")
    try:
        _procs_file(rig, "sol", [p.pid])
        _assert_collision_warning_and_child_ran(rig, _collide(rig, cg_out=cg_out))
    finally:
        p.kill()
        p.wait()


def test_c4_missing_cgroup_procs_keeps_the_collision_warning(rig):
    (rig.tmp / "cgroot").mkdir()
    _assert_collision_warning_and_child_ran(rig, _collide(rig))


def test_c5_dead_pid_then_plain_sleep_warns_without_crashing(rig):
    dead = subprocess.Popen(["true"])
    dead.wait()
    p = _spawn_named(rig, "sleep")
    try:
        _procs_file(rig, "sol", [dead.pid, p.pid])
        _assert_collision_warning_and_child_ran(rig, _collide(rig))
    finally:
        p.kill()
        p.wait()


def test_c5b_dead_pid_then_live_claude_still_refuses(rig):
    dead = subprocess.Popen(["true"])
    dead.wait()
    p = _spawn_named(rig, "claude")
    try:
        _procs_file(rig, "sol", [dead.pid, p.pid])
        proc = _collide(rig)
        assert proc.returncode == 3, proc.stderr
        assert f"(pid {p.pid})" in proc.stderr
    finally:
        p.kill()
        p.wait()


def test_inactive_scope_makes_no_controlgroup_call(rig):
    proc = rig.run("sol", "q", sc_out="inactive")
    assert proc.returncode == 0
    assert len(rig.systemctl_calls()) == 1


# --- C6: real systemd (skipped unless a user manager is reachable) -------------

def _real_systemd_reason() -> str | None:
    if not sys.platform.startswith("linux"):
        return "not Linux"
    if os.geteuid() == 0:
        return "running as root"
    if not (shutil.which("systemctl") and shutil.which("systemd-run")):
        return "no systemctl/systemd-run"
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env = dict(os.environ, XDG_RUNTIME_DIR=xdg)
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "Version"],
                           capture_output=True, text=True, env=env, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"systemctl --user failed: {e}"
    if r.returncode != 0:
        return f"systemctl --user show -p Version failed: {r.stderr.strip()[:80]}"
    # The suite-wide #3519 guard (repo-root conftest.py) puts a refusing systemd-run
    # shim first on PATH for every test; the cell cannot create its holder scope.
    if "service_manager_guard_3519" in (shutil.which("systemd-run") or ""):
        return ("the suite-wide #3519 guard refuses `systemd-run` in tests, so a real "
                "holder scope cannot be created here (checked by hand outside pytest)")
    return None


def _scope_active(seat: str, env) -> bool:
    r = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "--value",
                        f"agent-claude-{seat}.scope"], capture_output=True, text=True, env=env)
    return r.stdout.strip() == "active"


def test_c6_real_systemd_refuses_live_claude_and_warns_for_plain_sleep(tmp_path):
    # Evaluated at run time: the #3519 guard's PATH shim exists only once the
    # autouse fixture has run, never at collection.
    reason = _real_systemd_reason()
    if reason is not None:
        pytest.skip(reason)
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env = dict(os.environ, XDG_RUNTIME_DIR=xdg)
    rec = tmp_path / "rec"
    rec.mkdir()
    child = _exe(tmp_path / "child", _CHILD)
    claude = tmp_path / "claude"
    shutil.copy(shutil.which("sleep"), claude)
    claude.chmod(0o755)
    seats = {"claude": f"t4473x{secrets.token_hex(4)}", "sleep": f"t4473x{secrets.token_hex(4)}"}
    holders = []
    try:
        for kind, seat in seats.items():
            exe = str(claude) if kind == "claude" else shutil.which("sleep")
            holders.append(subprocess.Popen(
                ["systemd-run", "--user", "--scope", "--collect", "--quiet",
                 f"--unit=agent-claude-{seat}", exe, "300"], env=env))
            deadline = time.time() + 5
            while time.time() < deadline and not _scope_active(seat, env):
                time.sleep(0.1)
            assert _scope_active(seat, env), f"scope for {seat} never became active"
        run_env = dict(env, FAKE_REC=str(rec))
        # REAL PATH: no fakes.
        r1 = subprocess.run([BASH, str(AGENTCTL), "__scope-exec", seats["claude"], str(child)],
                            capture_output=True, text=True, env=run_env, timeout=30)
        assert r1.returncode == 3, r1.stderr
        assert REFUSE_PREFIX + f"{seats['claude']}.scope (pid " in r1.stderr
        assert re.search(r"\(pid \d+\)", r1.stderr)
        assert not (rec / "child.ran").exists()
        r2 = subprocess.run([BASH, str(AGENTCTL), "__scope-exec", seats["sleep"], str(child)],
                            capture_output=True, text=True, env=run_env, timeout=30)
        assert r2.returncode == 0, r2.stderr
        lines = _warn_lines(r2.stderr)
        assert len(lines) == 1 and f"agent-claude-{seats['sleep']}.scope already active" in lines[0]
        assert (rec / "child.ran").exists()
    finally:
        for seat in seats.values():
            subprocess.run(["systemctl", "--user", "stop", f"agent-claude-{seat}.scope"],
                           capture_output=True, env=env, timeout=30)
        for h in holders:
            try:
                h.wait(timeout=10)
            except subprocess.TimeoutExpired:
                h.kill()
                h.wait()


# --- S-a..S-e (AMENDMENT 3b): the fakes record what systemctl SEES ----------------
# In production __scope-stop runs from a SYSTEM unit with XDG_RUNTIME_DIR unset, so
# the derivation of the runtime dir is load-bearing and must be asserted by what the
# fake systemctl saw, not only that a call happened.

def test_sa_stop_xdg_unset_uses_runtime_base_uid_dir(rig, tmp_path):
    base = tmp_path / "base"
    (base / str(os.getuid())).mkdir(parents=True)
    proc = _run_stop(rig, "sol", xdg="unset", base=base)
    assert proc.returncode == 0, proc.stderr
    assert rig.systemctl_xdgs() == [str(base / str(os.getuid()))] * 2


@pytest.mark.skipif(not os.path.isdir(f"/run/user/{os.getuid()}"),
                    reason="/run/user/<uid> does not exist on this host")
def test_sb_stop_xdg_unset_and_base_unset_uses_run_user_uid(rig):
    proc = _run_stop(rig, "sol", xdg="unset")
    assert proc.returncode == 0, proc.stderr
    assert rig.systemctl_xdgs() == [f"/run/user/{os.getuid()}"] * 2


def test_sc_exec_scope_path_exact_probe_argv(rig):
    rig.run("sol", "q", sc_out="inactive")
    assert rig.systemctl_argvs() == [PROBE_ARGV]
    assert rig.systemctl_xdgs() == [str(rig.run_dir)]


def test_sc_stop_exact_argv(rig):
    proc = _run_stop(rig, "sol", sc_out="active")
    assert proc.returncode == 0, proc.stderr
    assert rig.systemctl_argvs() == [PROBE_ARGV, STOP_ARGV]


def test_sc_exec_collision_exact_argv(rig):
    rig.run("sol", "q", sc_out="active")
    assert rig.systemctl_argvs() == [PROBE_ARGV, CG_ARGV]


def test_sd_stop_unreachable_manager_exits_1_with_one_warning(rig):
    env = {"PATH": str(rig.bin), "HOME": str(rig.tmp / "home"), "FAKE_REC": str(rig.rec),
           "FAKE_SC_OUT": "", "FAKE_SC_ERR": "Failed to connect to bus: No medium found",
           "FAKE_SC_RC": "1", "XDG_RUNTIME_DIR": str(rig.run_dir)}
    proc = subprocess.run([BASH, str(AGENTCTL), "__scope-stop", "sol"],
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 1, proc.stderr
    lines = _warn_lines(proc.stderr)
    assert len(lines) == 1 and "user manager unreachable" in lines[0], proc.stderr
    assert "Failed to connect to bus" in lines[0]
    assert rig.systemctl_argvs() == [PROBE_ARGV]


@pytest.mark.skipif(os.path.isdir(f"/run/user/{os.getuid()}"),
                    reason="/run/user/<uid> exists on this host, so the 'missing' branch of the "
                           "default base cannot be reached")
def test_se_exec_xdg_unset_default_base_missing_names_run_user_uid(rig):
    proc = rig.run("sol", "x", xdg="unset", child_rc=7)
    assert proc.returncode == 7
    reason = _assert_one_warning(proc, "XDG_RUNTIME_DIR")
    assert reason == f"XDG_RUNTIME_DIR unset and /run/user/{os.getuid()} missing"
    assert rig.systemctl_calls() == []
