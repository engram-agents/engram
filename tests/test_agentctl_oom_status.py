"""Tests for ``agentctl oom-status`` (engram-alpha#4436).

Contract under test (round-2 verdict table, engram-alpha#4436):

  UNKNOWN                no SESSION process found, or a session
                         process's /proc/<pid>/oom_score_adj is
                         unreadable                               -> exit 3
  OUTSIDE-UNIT           at least one SESSION process has
                         cgroup != UNIT_CG                       -> exit 1
  OK                     every SESSION process reads < 0 and
                         MemoryMax is finite                     -> exit 0
  ACCEPTED-NOT-APPLIED   requested negative, but at least one
                         SESSION process reads >= 0              -> exit 1
  UNPROTECTED            requested not negative (with a
                         non-negative effective score), or the
                         bound is not finite                     -> exit 1

Precedence, stated because an earlier version was ambiguous: OK is decided on
the EFFECTIVE state, not the requested value. If every session process reads
negative and the bound is finite, the verdict is OK even when the unit
property is 0 -- the verb measures what is true, not what was asked. The
reason string names that case explicitly, because protection that did not
come from the policy will not survive a relaunch.

Let UNIT_CG be the unit's ``ControlGroup``. A process is a SESSION process
iff its cgroup path equals UNIT_CG, or it is a ``.scope`` anywhere under the
seat's own user slice ``user-<uid>.slice/`` -- both a tmux-spawn scope under
``user@<uid>.service`` and a logind ``session-N.scope`` directly under the
slice. Two exceptions are CONTEXT: the user manager's own
``user@<uid>.service/init.scope``, and a default-named
``systemd-run --user --scope`` transient ``run-<32 hex>.scope`` (measured on
uid 1008). Stated bound on the latter: a transient launched with an explicit
``--unit=<name>`` becomes ``<name>.scope``, which the exclusion does not
catch and still counts as session. Everything else owned by the uid --
``init.scope``, ``engramd-owner-*.service``, ``dbus.service`` -- is CONTEXT:
printed, never counted in the verdict. ``.scope`` is how systemd names an
externally-created process group, while ``.service`` names a managed daemon
the seat does not run as part of its session.

UNKNOWN has its own exit code so a script cannot collapse "could not
tell" into "fine". Exit 2 is a usage error (an unknown seat) or a seat
that could not be reported at all.

Roll-up across seats: a definite problem (1) outranks an unreportable
seat (2) and an unknown (3); 0 only when every seat is OK. Numeric max
would let one UNKNOWN mask a run holding several definite failures.

Why the verb exists: systemd *accepting* an ``OOMScoreAdjust`` property
is not evidence it took effect (an unprivileged ``systemd-run
--user -p OOMScoreAdjust=-200`` accepts the property, runs the process at
100, and still reports ``result success``). Only ``/proc/<pid>/oom_score_adj``
inside the live process settles what took effect, so the cells below pin
the comparison between the two and refuse to infer one from the other.

A zombie (Z) or dead (X) process is not part of the protection surface: it
has no memory, cannot be OOM-killed, and its ``oom_score_adj`` still reads
0. Counting it would turn a fully protected seat into a false
ACCEPTED-NOT-APPLIED, so it is skipped like a process that exited mid-scan.

Seam: the verb reads its process tree from ``AGENTCTL_OOM_PROC_ROOT``
(default ``/proc``). States the host cannot produce unprivileged -- a
negative live score (only *raising* oom_score_adj is permitted without
CAP_SYS_RESOURCE), zero processes, an unreadable file -- are driven
through a fixture root. The +500 cell and cells (a)/(e) run against the
real ``/proc`` and a real process, so the suite is not wholly fake.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENTCTL = ROOT / "tools" / "agentctl"

# The ControlGroup a real template instance reports (measured on this host:
# /system.slice/system-agent\x2dsession.slice/agent-session@zephyr.service).
# _proc_entry's default cgroup is this same path, so the default fixture has
# exactly one SESSION process inside the unit.
UNIT_CG = (
    "/system.slice/system-agent\\x2dsession.slice/agent-session@testseat.service"
)

# A transient scope root in the seat's own user manager. A process here runs
# outside the seat unit's cgroup and the unit's drop-in never reaches it.
USER_SCOPE_ROOT = (
    f"/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service"
)

# A logind login-session scope: a transient .scope directly under the user
# slice, not nested in the user manager. A seat relaunched from a plain login
# shell lives here.
LOGIN_SCOPE_ROOT = f"/user.slice/user-{os.geteuid()}.slice"


def _own_cgroup_path() -> str:
    """This test process's unified cgroup path ("" when it has none)."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return line[3:]
    except OSError:
        pass
    return ""


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/proc/self/oom_score_adj").exists(),
    reason="oom-status reads the Linux /proc tree",
)


def _gnu_stat_ok() -> bool:
    try:
        r = subprocess.run(
            ["stat", "-c", "%u", "/"], capture_output=True, text=True
        )
    except OSError:
        return False
    return r.returncode == 0 and r.stdout.strip().isdigit()


needs_gnu_stat = pytest.mark.skipif(
    not _gnu_stat_ok(), reason="needs GNU stat -c %u (proc owner lookup)"
)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _run(*args: str, path_dir: Path | None = None, proc_root: Path | None = None):
    """Run agentctl with a controlled PATH and/or fixture proc root."""
    env = dict(os.environ)
    if path_dir is not None:
        env["PATH"] = str(path_dir) + os.pathsep + env.get("PATH", "")
    if proc_root is not None:
        env["AGENTCTL_OOM_PROC_ROOT"] = str(proc_root)
    return subprocess.run(
        ["bash", str(AGENTCTL), *args], capture_output=True, text=True, env=env
    )


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _stub_dir(
    tmp_path: Path,
    *,
    members: list[str],
    uid: int,
    requested: str = "0",
    memory: str = "infinity",
    load: str = "loaded",
    active: str = "active",
    control_group: str = UNIT_CG,
    systemctl_exit: int = 0,
    log_systemctl: Path | None = None,
    seat_overrides: dict[str, dict] | None = None,
    getent_exit: int = 0,
    missing_users: frozenset[str] = frozenset(),
) -> Path:
    """A PATH holding stubs for getent/id/systemctl.

    ``uid`` is what the stub ``id`` reports for every seat: the fixture
    proc tree is owned by the test user, so the verb's owner match only
    succeeds when this equals ``os.geteuid()``. ``seat_overrides`` gives
    individual seats a different uid (so a mixed-seat test can make one
    seat own no processes) or different unit facts. ``getent_exit`` makes
    the group lookup itself fail; ``missing_users`` makes ``id -u`` fail for
    a seat that IS listed in the group.
    """
    seat_overrides = seat_overrides or {}

    def facts(m: str) -> dict:
        f = {
            "uid": uid,
            "requested": requested,
            "memory": memory,
            "load": load,
            "active": active,
            "control_group": control_group,
            "systemctl_exit": systemctl_exit,
        }
        f.update(seat_overrides.get(m, {}))
        return f

    d = tmp_path / "stub-bin"
    d.mkdir()
    real_getent = shutil.which("getent") or "/usr/bin/getent"
    real_id = shutil.which("id") or "/usr/bin/id"

    if getent_exit:
        getent_answer = f"    exit {getent_exit}\n"
    else:
        member_csv = ",".join("agent-" + m for m in members)
        getent_answer = f'    echo "agents:x:1003:{member_csv}"\n    exit 0\n'
    _write_exec(
        d / "getent",
        "#!/bin/bash\n"
        'if [ "$1" = group ] && [ "$2" = agents ]; then\n'
        + getent_answer
        + "fi\n"
        + f'exec {real_getent} "$@"\n',
    )

    id_lines = []
    for m in members:
        if m in missing_users:
            id_lines.append(f'[ "$1" = -u ] && [ "$2" = "agent-{m}" ] && exit 1')
        else:
            id_lines.append(
                f'[ "$1" = -u ] && [ "$2" = "agent-{m}" ] && {{ echo {facts(m)["uid"]}; exit 0; }}'
            )
    id_cases = "\n".join(id_lines)
    _write_exec(
        d / "id",
        f'#!/bin/bash\n{id_cases}\nexec {real_id} "$@"\n',
    )

    log_line = (
        f'echo "systemctl $*" >> {shlex.quote(str(log_systemctl))}\n'
        if log_systemctl is not None
        else ""
    )
    unit_cases: list[str] = []
    for m in members:
        f = facts(m)
        unit_cases.append(
            f"  agent-session@{m}.service)\n"
            f'    echo "LoadState={f["load"]}"\n'
            f'    echo "ActiveState={f["active"]}"\n'
            f'    echo "OOMScoreAdjust={f["requested"]}"\n'
            f'    echo "MemoryMax={f["memory"]}"\n'
            f'    echo "ControlGroup={f["control_group"]}"\n'
            f'    exit {f["systemctl_exit"]}\n'
            "    ;;"
        )
    _write_exec(
        d / "systemctl",
        "#!/bin/bash\n"
        + log_line
        + 'if [ "$1" = "show" ]; then\n'
        + '  case "$2" in\n'
        + "\n".join(unit_cases)
        + "\n  esac\n"
        "fi\n"
        "exit 1\n",
    )
    return d


def _proc_entry(
    root: Path,
    pid: int,
    *,
    comm: str = "worker",
    cgroup: str = "0::" + UNIT_CG,
    score: str | None = "0",
    mode: int | None = None,
    state: str | None = None,
) -> Path:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "cgroup").write_text(cgroup + "\n")
    if state is not None:
        (d / "stat").write_text(f"{pid} ({comm}) {state} 1 1 1 1\n")
    if score is not None:
        f = d / "oom_score_adj"
        f.write_text(score + "\n")
        if mode is not None:
            f.chmod(mode)
    return d


def _rows(stdout: str) -> dict[int, list[str]]:
    """Effective-process rows: pid -> [pid, comm, score, cgroup].

    Parsed by the printf table's FIXED columns, not by whitespace: a comm can
    contain a space ("tmux: server"), and a whitespace split would shift it
    into the score column.
    """
    out: dict[int, list[str]] = {}
    for line in stdout.splitlines():
        if len(line) < 41 or not line[:8].strip().isdigit():
            continue
        out[int(line[:8].strip())] = [
            line[:8].strip(),
            line[9:25].strip(),
            line[26:40].strip(),
            line[41:].strip(),
        ]
    return out


def _line_starting(stdout: str, prefix: str) -> str:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starts with {prefix!r} in:\n{stdout}")


def _requested_value(stdout: str) -> str:
    """The requested cell's value, isolated from its [note] suffix."""
    line = _line_starting(stdout, "requested:")
    return line.split("requested:", 1)[1].split("[", 1)[0].strip()


def _real_seat() -> str | None:
    """A real seat whose Linux user is the uid running this test."""
    try:
        out = subprocess.run(
            ["getent", "group", "agents"], capture_output=True, text=True
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    for member in out.stdout.strip().split(":")[-1].split(","):
        if not member.startswith("agent-"):
            continue
        name = member[len("agent-") :]
        r = subprocess.run(["id", "-u", member], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() == str(os.geteuid()):
            return name
    return None


@pytest.fixture
def real_seat() -> str:
    seat = _real_seat()
    if seat is None:
        pytest.skip("no agent-<name> seat whose Linux user is this test's uid")
    return seat


def _snapshot_scores(uid: int) -> dict[int, str]:
    scores: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            scores[int(entry.name)] = (entry / "oom_score_adj").read_text().strip()
        except OSError:
            continue
    return scores


# ---------------------------------------------------------------------------
# cell (a): requested and effective are BOTH printed; disagreement is named
# ---------------------------------------------------------------------------


class TestRequestedVsEffective:
    def test_disagreement_names_accepted_not_applied(self, tmp_path, real_seat):
        """requested=-200 (stub unit) vs a real live process's own value.

        The effective value is READ from /proc, never assumed to be 0: the
        moment a drop-in makes a spawned process inherit a negative score, a
        cell hardcoding "0" would fail a CORRECT implementation. Writing the
        maximum 1000 is always permitted unprivileged (only lowering is
        refused), so it is an increase from any inherited value.

        A single number could be either one; the cell pins that both appear
        and the verdict names the gap.

        The stub must name the REAL unit cgroup, otherwise the sleeper (and
        this test process) are context, not session, and the verdict is
        UNKNOWN. A tmux-relaunched seat puts its processes in a user scope
        instead of the unit's cgroup; that launch path is exercised by
        TestSessionPlacement, so skip here rather than assert a placement the
        host does not have.
        """
        unit_cg = (
            "/system.slice/system-agent\\x2dsession.slice/"
            f"agent-session@{real_seat}.service"
        )
        if _own_cgroup_path() != unit_cg:
            pytest.skip(
                "test process is outside agent-session@<seat>.service "
                "(tmux-relaunched seat); this cell needs the unit cgroup"
            )
        stub = _stub_dir(
            tmp_path,
            members=[real_seat],
            uid=os.geteuid(),
            requested="-200",
            control_group=unit_cg,
        )
        sleeper = subprocess.Popen(["sleep", "60"])
        try:
            try:
                Path(f"/proc/{sleeper.pid}/oom_score_adj").write_text("1000\n")
            except PermissionError:
                pytest.skip("cannot raise oom_score_adj on this host")
            live = Path(f"/proc/{sleeper.pid}/oom_score_adj").read_text().strip()
            proc = _run("oom-status", real_seat, path_dir=stub)
        finally:
            sleeper.kill()
            sleeper.wait()

        assert proc.returncode == 1, proc.stderr
        assert _requested_value(proc.stdout) == "-200"
        rows = _rows(proc.stdout)
        assert sleeper.pid in rows, proc.stdout
        assert rows[sleeper.pid][1] == "sleep"
        assert rows[sleeper.pid][2] == live, rows[sleeper.pid]
        assert live == "1000", "the raised score was not the live value"
        assert "verdict: ACCEPTED-NOT-APPLIED" in proc.stdout


# ---------------------------------------------------------------------------
# cell (b): a process at a negative score is read from /proc, not inferred
# ---------------------------------------------------------------------------


class TestEffectiveIsRead:
    def test_negative_effective_score_is_not_taken_from_the_unit(self, tmp_path):
        """Unit says 0; the process says -200. The -200 must be reported.

        A negative live score cannot be produced unprivileged (only
        raising oom_score_adj is permitted), so this cell uses the
        fixture proc root; the real-/proc cells are next section.
        """
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid(), requested="0"
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 4242, comm="worker", score="-200")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert _requested_value(proc.stdout) == "0"
        req = _line_starting(proc.stdout, "requested:")
        assert "-200" not in req, "the effective value leaked into the requested cell"
        rows = _rows(proc.stdout)
        assert rows[4242][2] == "-200", rows[4242]
        assert proc.returncode == 1, proc.stdout

    def test_comm_control_characters_do_not_break_the_row(self, tmp_path):
        """comm is arbitrary bytes; a newline must not split the row."""
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid(), requested="0"
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="evil\nname", score="0")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        rows = _rows(proc.stdout)
        assert 101 in rows, "the newline broke the row:\n" + proc.stdout
        assert rows[101][1] == "evil name", rows[101]


# ---------------------------------------------------------------------------
# selection is by USER, never by cgroup -- the design decision this verb exists for
# ---------------------------------------------------------------------------


class TestSelectionIsByUser:
    @pytest.mark.parametrize(
        "cgroup, marker, state, code",
        [
            (
                "0::" + UNIT_CG,
                "agent-session@testseat.service",
                "OK",
                0,
            ),
            (
                # logind login-session scope: directly under the user slice.
                "0::" + LOGIN_SCOPE_ROOT + "/session-3.scope",
                "session-3.scope",
                "OUTSIDE-UNIT",
                1,
            ),
            (
                "0::"
                + USER_SCOPE_ROOT
                + "/app.slice/app-tmux.slice/tmux-spawn-abc.scope",
                "tmux-spawn-abc.scope",
                "OUTSIDE-UNIT",
                1,
            ),
        ],
    )
    def test_process_in_any_scope_is_still_found(
        self, tmp_path, cgroup, marker, state, code
    ):
        """A seat's processes live in different cgroups by launch path.

        Selection is still by USER, so every row is found and printed
        whatever its cgroup. The round-2 verdict additionally says WHETHER
        that placement is inside the unit: a process in the unit's cgroup is
        OK (drop-in reaches it), while a transient scope under the seat's own
        user manager reads OUTSIDE-UNIT -- never silently counted as a
        policy failure (ACCEPTED-NOT-APPLIED) nor as OK.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="worker", cgroup=cgroup, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        rows = _rows(proc.stdout)
        assert 101 in rows, proc.stdout
        assert marker in rows[101][3], rows[101]
        assert f"verdict: {state}" in proc.stdout, proc.stdout
        assert proc.returncode == code, proc.stdout


# ---------------------------------------------------------------------------
# real /proc against a real process (the spec's anti-"all fake" requirement)
# ---------------------------------------------------------------------------


class TestRealProc:
    def test_raised_score_is_read_back(self, real_seat):
        """Raise a real sleep's score to 500 and assert the verb reports 500."""
        sleeper = subprocess.Popen(["sleep", "120"])
        try:
            try:
                Path(f"/proc/{sleeper.pid}/oom_score_adj").write_text("500\n")
            except PermissionError:
                pytest.skip(
                    "cannot raise oom_score_adj on this host (needs write access)"
                )
            assert (
                Path(f"/proc/{sleeper.pid}/oom_score_adj").read_text().strip()
                == "500"
            )
            proc = _run("oom-status", real_seat)
        finally:
            sleeper.kill()
            sleeper.wait()

        rows = _rows(proc.stdout)
        assert sleeper.pid in rows, proc.stdout
        assert rows[sleeper.pid][1] == "sleep"
        assert rows[sleeper.pid][2] == "500", rows[sleeper.pid]
        # The requested cell is the unit's property, which on a host with
        # no applied policy is 0 -- never the observed 500.
        assert _requested_value(proc.stdout) != "500"


# ---------------------------------------------------------------------------
# cell (c): zero processes is UNKNOWN/exit 3, distinct from OK/exit 0
# ---------------------------------------------------------------------------


class TestNoProcesses:
    def test_zero_processes_is_unknown_exit_3(self, tmp_path):
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid(), requested="0"
        )
        root = tmp_path / "proc-empty"
        root.mkdir()

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 3, proc.stdout
        assert proc.returncode != 0
        assert "verdict: UNKNOWN" in proc.stdout
        assert "no live process" in proc.stdout


# ---------------------------------------------------------------------------
# cell (d): unreadable / unparseable score is UNKNOWN, never a default of 0
# ---------------------------------------------------------------------------


class TestUnreadableScore:
    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
    def test_unreadable_score_is_unknown_never_zero(self, tmp_path):
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid(), requested="0"
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 4242, comm="worker", score="0", mode=0o000)

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 3, proc.stdout
        assert "verdict: UNKNOWN" in proc.stdout
        rows = _rows(proc.stdout)
        assert 4242 in rows, "the process was silently dropped:\n" + proc.stdout
        assert rows[4242][2] == "?", rows[4242]
        assert "first: pid 4242" in proc.stdout

    def test_unparseable_score_is_unknown_never_zero(self, tmp_path):
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid(), requested="0"
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 4243, comm="worker", score="not-a-number")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 3, proc.stdout
        assert "verdict: UNKNOWN" in proc.stdout
        assert _rows(proc.stdout)[4243][2] == "?"


class TestUnplaceableCgroup:
    """An unreadable cgroup is UNKNOWN, never silently CONTEXT.

    Falling back to CONTEXT would zero session_total and land on the SIBLING
    UNKNOWN ("no session process found"), which is also exit 3 -- so a cell
    asserting only UNKNOWN/exit 3 proves nothing. These cells pin the reason
    only the cgroup branch produces, and assert the sibling reason is absent.
    """

    @pytest.mark.parametrize(
        "unreadable", [False, True], ids=["absent", "unreadable"]
    )
    def test_unreadable_cgroup_is_unknown_never_context(self, tmp_path, unreadable):
        if unreadable and os.geteuid() == 0:
            pytest.skip("root reads mode-000 files; the absent variant still runs")
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        d = root / "4242"
        d.mkdir()
        (d / "comm").write_text("worker\n")
        (d / "oom_score_adj").write_text("-100\n")
        if unreadable:
            # Present but not readable: same branch as absent, and the score is
            # fully readable, so the score-read UNKNOWN cannot fire instead.
            f = d / "cgroup"
            f.write_text("0::" + UNIT_CG + "\n")
            f.chmod(0o000)
        # absent: no cgroup file at all

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 3, proc.stdout
        assert "verdict: UNKNOWN" in proc.stdout
        # The reason ONLY the cgroup_unknown branch produces.
        assert "could not be read" in proc.stdout
        assert "session placement could not be determined" in proc.stdout
        # The sibling UNKNOWN must not be what fired.
        assert "no session process found" not in proc.stdout
        assert "verdict: OK" not in proc.stdout
        # The process was found; it just cannot be placed.
        assert 4242 in _rows(proc.stdout), proc.stdout


class TestVanishedProcess:
    def test_process_that_exited_mid_scan_is_skipped_not_unknown(self, tmp_path):
        """A process alive at enumeration but gone by its read is not live.

        Its score file is absent (ENOENT) -- that is not "could not tell"
        about the seat: every remaining live process is fully readable. Treating
        the vanish as UNKNOWN made a healthy seat flake whenever a transient
        process exited mid-scan (reproduced: 1 in 30 runs with 30 ms-lived
        processes). A file that EXISTS but is unreadable still yields UNKNOWN
        (the cells above).
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="live", score="-100")
        _proc_entry(root, 202, comm="gone", score=None)

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        assert "verdict: OK" in proc.stdout
        assert "all 1 live process" in proc.stdout
        assert "202" not in proc.stdout, "the vanished process leaked into the table"
        assert "UNKNOWN" not in proc.stdout


# ---------------------------------------------------------------------------
# zombies and dead processes are not part of the protection surface
# ---------------------------------------------------------------------------


class TestZombieProcess:
    @pytest.mark.parametrize("state", ["Z", "X"])
    def test_dead_process_does_not_flip_a_protected_seat(self, tmp_path, state):
        """A forked-but-unreaped child still reads oom_score_adj 0.

        It has no memory and cannot be OOM-killed, so counting it would turn a
        fully protected seat (all real processes negative, finite bound) into a
        false ACCEPTED-NOT-APPLIED. It is skipped like a vanished process.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="live", score="-100", state="S")
        _proc_entry(root, 202, comm="defunct", score="0", state=state)

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        assert "verdict: OK" in proc.stdout
        assert "all 1 live process" in proc.stdout
        assert "202" not in proc.stdout, "the dead process leaked into the table"


# ---------------------------------------------------------------------------
# cell (e): read-only -- nothing on the host changes
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_run_changes_no_score_and_calls_no_setter(self, tmp_path, real_seat):
        log = tmp_path / "systemctl.calls"
        stub = _stub_dir(
            tmp_path,
            members=[real_seat],
            uid=os.geteuid(),
            requested="0",
            log_systemctl=log,
        )
        before = _snapshot_scores(os.geteuid())

        proc = _run("oom-status", real_seat, path_dir=stub)

        assert "verdict:" in proc.stdout, "the run did not actually report"
        after = _snapshot_scores(os.geteuid())

        changed = {
            pid: (before[pid], after[pid])
            for pid in before.keys() & after.keys()
            if before[pid] != after[pid]
        }
        assert changed == {}, f"a live process score changed: {changed}"

        calls = log.read_text().splitlines()
        assert calls, "the verb never queried systemctl"
        assert all(call.startswith("systemctl show ") for call in calls), calls
        assert not any("set-property" in call for call in calls), calls


# ---------------------------------------------------------------------------
# verdict states that need no real process state
# ---------------------------------------------------------------------------


class TestVerdictStates:
    def test_ok_requires_negative_scores_and_a_finite_bound(self, tmp_path):
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")
        _proc_entry(root, 102, score="-50")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        assert "verdict: OK" in proc.stdout
        assert "MemoryMax=1073741824" in proc.stdout

    def test_requested_zero_is_unprotected(self, tmp_path):
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid(), requested="0"
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="0")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        assert "verdict: UNPROTECTED" in proc.stdout

    def test_positive_effective_score_with_negative_request_is_accepted_not_applied(
        self, tmp_path
    ):
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid(), requested="-200"
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        assert "verdict: ACCEPTED-NOT-APPLIED" in proc.stdout

    def test_leading_zero_score_is_not_parsed_as_octal(self, tmp_path):
        """``(( 09 >= 0 ))`` errors and reads as false; the score is 09, not 0.

        Unreachable from a real procfs, reachable through the fixture seam.
        Treated as a non-negative score: ACCEPTED-NOT-APPLIED, never OK.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="worker", score="09")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        assert "verdict: ACCEPTED-NOT-APPLIED" in proc.stdout
        assert _rows(proc.stdout)[101][2] == "09"

    def test_gap_all_applied_but_no_finite_bound_is_unprotected(self, tmp_path):
        """Pinned decision: the score is applied, but the bound is not finite.

        The verdict table has no row for this combination. Accepted
        behaviour: UNPROTECTED (exit 1), with the reason naming the missing
        bound -- the seat is not protected AS ASKED, and claiming OK would
        contradict OK's own finite-MemoryMax clause. Pinned here so the
        label is a decision, not a fallthrough.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="infinity",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        assert "verdict: UNPROTECTED" in proc.stdout
        assert "verdict: OK" not in proc.stdout
        assert "MemoryMax=infinity" in proc.stdout
        assert "is not finite" in proc.stdout

    def test_systemctl_query_failure_is_unknown_not_unprotected(self, tmp_path):
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
            systemctl_exit=1,
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 3, proc.stdout
        assert "verdict: UNKNOWN" in proc.stdout
        # Pin WHICH UNKNOWN branch fired: without that branch the fixture has
        # no session process and falls through to the sibling reason.
        assert "could not query systemd unit" in proc.stdout

    def test_unparseable_requested_value_is_unknown_not_ok(self, tmp_path):
        """A requested value that cannot be parsed is not a policy of 0.

        Without this branch the ladder reaches the OK row (every session
        process reads negative, the bound is finite), claiming protection
        whose policy was never read.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="not-a-number",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 3, proc.stdout
        assert "verdict: UNKNOWN" in proc.stdout
        assert "no parseable OOMScoreAdjust" in proc.stdout
        assert "verdict: OK" not in proc.stdout


# ---------------------------------------------------------------------------
# verdict REASONS must not contradict the displayed requested/memory cells
# ---------------------------------------------------------------------------


class TestReasonWording:
    def test_ok_with_non_negative_request_notes_it_is_not_policy(self, tmp_path):
        """OK is decided on the EFFECTIVE state; the reason names it is not policy.

        Every session process reads -100 and the bound is finite, so the seat
        genuinely IS OOM-protected even though the unit requests 0. Reporting
        UNPROTECTED here would push the reader to install a drop-in that does
        not reach these processes -- the placement error this verb exists to
        expose. The note carries the "will not survive a relaunch" warning,
        which is the designed remedy.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="0",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        verdict = _line_starting(proc.stdout, "verdict:")
        assert "verdict: OK" in verdict
        assert "did not come from the unit policy" in verdict
        assert "will not survive a relaunch" in verdict
        assert f"unit cgroup: {UNIT_CG}" in proc.stdout
        assert "session: 1 process(es), 0 outside the unit cgroup" in proc.stdout

    def test_all_negative_without_a_finite_bound_is_unprotected_for_the_bound(
        self, tmp_path
    ):
        """OK's finite-bound clause still governs when the request is 0.

        The effective scores are all negative, but MemoryMax=infinity means the
        seat is not protected AS ASKED. The reason must name the missing bound,
        not the non-negative request, so the reader knows which half is absent.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="0",
            memory="infinity",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        verdict = _line_starting(proc.stdout, "verdict:")
        assert "verdict: UNPROTECTED" in verdict
        assert "is not finite" in verdict
        assert "verdict: OK" not in proc.stdout
        assert "MemoryMax=infinity" in proc.stdout

    def test_applied_without_a_bound_outranks_the_inactive_property_reason(
        self, tmp_path
    ):
        """The missing bound is the reason, even on an inactive unit.

        The ladder checks "requested negative, every session score negative,
        no finite bound" before the generic requested-value reasons. Without
        that branch an inactive unit would instead report the property-default
        wording. Both are UNPROTECTED/exit 1, so only the reason distinguishes
        them -- the last unpinned branch of the ladder.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            active="inactive",
            memory="infinity",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        verdict = _line_starting(proc.stdout, "verdict:")
        assert "verdict: UNPROTECTED" in verdict
        assert "is not finite" in verdict
        assert "property default" not in verdict

    def test_missing_unit_reason_does_not_cite_a_requested_value(self, tmp_path):
        """The display says n/a; the verdict must not cite the synthesized 0."""
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="0",
            load="not-found",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="0")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        verdict = _line_starting(proc.stdout, "verdict:")
        assert "not found" in verdict
        assert "OOMScoreAdjust=0" not in verdict

    def test_inactive_unit_reason_does_not_claim_a_running_policy(self, tmp_path):
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="0",
            active="inactive",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="0")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        verdict = _line_starting(proc.stdout, "verdict:")
        assert "inactive" in verdict
        assert "requested OOMScoreAdjust=0 is not negative" not in verdict

    def test_inactive_oneshot_with_negative_request_is_accepted_not_applied(
        self, tmp_path
    ):
        """An inactive oneshot still owns the processes left in its cgroup.

        agent-session@<seat>.service is a oneshot: after its main process
        exits it reads SubState=exited and normally ActiveState=inactive, yet
        its ControlGroup can still hold the seat's claude (measured on this
        host). The round-2 table has no ActiveState exception: a negative
        request against a session process reading >= 0 is
        ACCEPTED-NOT-APPLIED. The inactive state stays visible in the
        requested cell as context, not as a verdict override.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            active="inactive",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="0")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        assert "verdict: ACCEPTED-NOT-APPLIED" in proc.stdout
        assert "inactive" in _line_starting(proc.stdout, "requested:")


# ---------------------------------------------------------------------------
# roll-up: a definite outcome outranks a softer one (1 > 2 > 3 > 0)
# ---------------------------------------------------------------------------


class TestExitAggregation:
    def test_definite_problem_outranks_unknown(self, tmp_path):
        """UNPROTECTED + UNKNOWN -> exit 1, not 3.

        The seat with the test user's uid sees the fixture process (score 0,
        requested 0 -> UNPROTECTED); the other seat resolves to a uid that
        owns nothing -> UNKNOWN.
        """
        uid = os.geteuid()
        stub = _stub_dir(
            tmp_path,
            members=["red", "grey"],
            uid=uid,
            requested="0",
            seat_overrides={"grey": {"uid": uid + 1}},
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="0")

        proc = _run("oom-status", "red", "grey", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        assert "=== red" in proc.stdout and "=== grey" in proc.stdout
        assert "verdict: UNPROTECTED" in proc.stdout
        assert "verdict: UNKNOWN" in proc.stdout

    def test_ok_does_not_mask_an_unknown(self, tmp_path):
        """OK + UNKNOWN -> exit 3; 0 is the floor, never a mask."""
        uid = os.geteuid()
        stub = _stub_dir(
            tmp_path,
            members=["green", "grey"],
            uid=uid,
            requested="-200",
            memory="1073741824",
            seat_overrides={"grey": {"uid": uid + 1}},
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")

        proc = _run("oom-status", "green", "grey", path_dir=stub, proc_root=root)

        assert proc.returncode == 3, proc.stdout
        assert "verdict: OK" in proc.stdout
        assert "verdict: UNKNOWN" in proc.stdout


# ---------------------------------------------------------------------------
# display truncation: the verdict is computed over ALL processes found
# ---------------------------------------------------------------------------


class TestTruncatedDisplay:
    def test_hidden_process_still_drives_the_verdict(self, tmp_path):
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        for pid in range(1, 55):  # 54 processes at -100
            _proc_entry(root, pid, score="-100")
        _proc_entry(root, 9999, score="100")  # 1 positive, sorts last -> hidden

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        # Verdict over the visible slice would be OK/exit 0; over all 55 it
        # is ACCEPTED-NOT-APPLIED/exit 1.
        assert proc.returncode == 1, proc.stdout
        assert "verdict: ACCEPTED-NOT-APPLIED" in proc.stdout
        assert "5 more process(es) not shown" in proc.stdout
        assert "all 55" in proc.stdout
        assert "9999" not in proc.stdout, "the hidden process leaked into the table"
        shown = [line for line in proc.stdout.splitlines() if line.split()[:1] and line.split()[0].isdigit()]
        assert len(shown) == 50, f"expected 50 shown rows, got {len(shown)}"


# ---------------------------------------------------------------------------
# CLI surface: seat selection, unknown seat, absent unit
# ---------------------------------------------------------------------------


class TestCliSurface:
    def test_unknown_seat_is_a_usage_error_naming_it(self, tmp_path):
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid()
        )
        proc = _run("oom-status", "not-a-seat", path_dir=stub)

        assert proc.returncode == 2, proc.stdout + proc.stderr
        # The distinguishing text, not just the code: the downstream
        # "no Linux user" fallback ALSO exits 2 with the name in stderr, so
        # asserting only rc+name would leave the pre-validation block
        # unpinned (measured: deleting it still passed 20/20).
        assert "not a member of the 'agents' group" in proc.stderr
        assert "not-a-seat" in proc.stderr

    def test_seat_in_group_without_a_linux_user_is_a_usage_error(self, tmp_path):
        """The second validation branch: in the group, but no Linux user."""
        stub = _stub_dir(
            tmp_path,
            members=["testseat", "ghost"],
            uid=os.geteuid(),
            missing_users=frozenset({"ghost"}),
        )
        proc = _run("oom-status", "ghost", path_dir=stub)

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "unknown seat 'ghost' (no Linux user 'agent-ghost')" in proc.stderr

    def test_group_lookup_failure_is_unknown_exit_3(self, tmp_path):
        """A failed lookup is not an empty universe: nothing was determined."""
        stub = _stub_dir(
            tmp_path, members=["testseat"], uid=os.geteuid(), getent_exit=2
        )
        proc = _run("oom-status", path_dir=stub)

        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert "could not look up the 'agents' group" in proc.stderr

    def test_existing_empty_group_is_success_exit_0(self, tmp_path):
        """The group exists and has no agent-* members: an empty universe."""
        stub = _stub_dir(tmp_path, members=[], uid=os.geteuid())
        proc = _run("oom-status", path_dir=stub)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "no agent seats found" in proc.stderr

    def test_no_arguments_checks_every_seat(self, tmp_path):
        stub = _stub_dir(
            tmp_path,
            members=["alpha", "beta"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="-100")

        proc = _run("oom-status", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        assert "=== alpha (agent-alpha, uid " in proc.stdout
        assert "=== beta (agent-beta, uid " in proc.stdout
        assert proc.stdout.count("verdict: OK") == 2

    def test_inactive_unit_is_distinguished_from_a_configured_zero(self, tmp_path):
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="0",
            active="inactive",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="0")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        req = _line_starting(proc.stdout, "requested:")
        assert _requested_value(proc.stdout) == "0"
        assert "inactive" in req, req

    def test_missing_unit_is_reported_as_absent_not_as_zero(self, tmp_path):
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="0",
            load="not-found",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, score="0")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        req = _line_starting(proc.stdout, "requested:")
        assert _requested_value(proc.stdout) == "n/a"
        assert "not found" in req, req


# ---------------------------------------------------------------------------
# round-2 contract: SESSION processes vs CONTEXT, and OUTSIDE-UNIT placement
# ---------------------------------------------------------------------------

# A managed daemon the seat does not run as part of its session: `.service`
# under /system.slice (engramd-owner, engram-surface-daemon). It must be
# printed and must NOT be counted in the verdict.
CONTEXT_SERVICE_CG = "0::/system.slice/engramd-owner-0badc0de.service"


class TestSessionPlacement:
    def test_ok_when_every_session_process_is_in_the_unit(self, tmp_path):
        """The baseline: one session process inside UNIT_CG, requested -200.

        OK additionally requires a finite MemoryMax in round 2, and the report
        must print UNIT_CG and the session process's own cgroup so the
        placement question is answerable from the output alone.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="claude", cgroup="0::" + UNIT_CG, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        assert "verdict: OK" in proc.stdout
        assert f"unit cgroup: {UNIT_CG}" in proc.stdout
        assert f"  session cgroup: {UNIT_CG}" in proc.stdout
        assert "session: 1 process(es), 0 outside the unit cgroup" in proc.stdout

    def test_context_service_at_positive_score_does_not_block_ok(self, tmp_path):
        """Defect 1: the uid also owns unrelated services; they are not the seat.

        Measured on uid 1008: engramd-owner-*.service runs at oom_score_adj
        200. Counting it made OK unreachable and reported every seat as
        ACCEPTED-NOT-APPLIED, which cannot distinguish "the drop-in failed"
        from "the user owns a daemon". It is CONTEXT: printed, not counted.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="claude", cgroup="0::" + UNIT_CG, score="-100")
        _proc_entry(
            root, 202, comm="engramd-owner", cgroup=CONTEXT_SERVICE_CG, score="100"
        )

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        assert "verdict: OK" in proc.stdout
        assert "session: 1 process(es), 0 outside the unit cgroup" in proc.stdout
        assert "context: 1 process(es)" in proc.stdout
        rows = _rows(proc.stdout)
        assert 202 in rows, "the context process was not printed:\n" + proc.stdout
        assert rows[202][2] == "100", rows[202]

    def test_user_manager_init_scope_is_context_not_session(self, tmp_path):
        """init.scope IS the user manager, not a transient scope of the seat.

        Measured on this host: user@1008.service/init.scope holds systemd and
        sd-pam at oom_score_adj 100. A literal "ends in .scope under
        user@<uid>.service" match counted them, so every healthy seat read
        OUTSIDE-UNIT. The rule is about TRANSIENT scopes (tmux-spawn,
        login-session, systemd-run), and the user manager's own init.scope is
        not one: the seat unit's drop-in cannot reach it.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="claude", cgroup="0::" + UNIT_CG, score="-100")
        _proc_entry(
            root,
            202,
            comm="systemd",
            cgroup="0::" + USER_SCOPE_ROOT + "/init.scope",
            score="100",
        )

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        assert "verdict: OK" in proc.stdout
        assert "session: 1 process(es), 0 outside the unit cgroup" in proc.stdout
        assert "context: 1 process(es)" in proc.stdout

    def test_systemd_run_transient_scope_is_context_not_session(self, tmp_path):
        """A `systemd-run --user --scope` one-shot is not the seat's session.

        Default transient naming is run-<32 hex>.scope under
        user@<uid>.service/app.slice/ (measured on uid 1008: `systemd-run
        --user --scope --quiet sleep 3` produced
        run-r30af8eafd81248d5a8731c3db2d1e815.scope). Counting it as session
        flipped an otherwise in-unit, fully protected seat to OUTSIDE-UNIT.
        Stated bound: an explicit --unit=<name> produces <name>.scope and is
        still counted as session.
        """
        run_scope = (
            USER_SCOPE_ROOT
            + "/app.slice/run-r30af8eafd81248d5a8731c3db2d1e815.scope"
        )
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="claude", cgroup="0::" + UNIT_CG, score="-100")
        _proc_entry(root, 202, comm="sleep", cgroup="0::" + run_scope, score="100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 0, proc.stdout
        assert "verdict: OK" in proc.stdout
        assert "session: 1 process(es), 0 outside the unit cgroup" in proc.stdout
        assert "context: 1 process(es)" in proc.stdout
        rows = _rows(proc.stdout)
        assert 202 in rows, "the transient scope was not printed:\n" + proc.stdout
        assert "run-r30af8eafd81248d5a8731c3db2d1e815.scope" in rows[202][3], rows[202]

    def test_session_in_a_tmux_scope_is_outside_unit(self, tmp_path):
        """Defect 2: a hand-relaunched seat lives outside the unit's cgroup.

        Measured placement after a tmux relaunch:
        user@<uid>.service/app.slice/app-tmux.slice/tmux-spawn-*.scope. The
        unit's drop-in never reaches that process, so reporting
        ACCEPTED-NOT-APPLIED ("the policy failed") is wrong and OK is wrong.
        The verdict names the count and the remedy.
        """
        scope = USER_SCOPE_ROOT + "/app.slice/app-tmux.slice/tmux-spawn-abc.scope"
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="claude", cgroup="0::" + scope, score="-100")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        assert "verdict: OUTSIDE-UNIT" in proc.stdout
        assert "1 of 1 session process(es) live outside unit cgroup" in proc.stdout
        assert (
            "relaunch the seat through its unit so the drop-in applies"
            in proc.stdout
        )
        assert f"  session cgroup: {scope}" in proc.stdout
        assert f"unit cgroup: {UNIT_CG}" in proc.stdout

    def test_unit_process_at_zero_with_negative_request_is_accepted_not_applied(
        self, tmp_path
    ):
        """The policy was accepted but the session process reads 0."""
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="claude", cgroup="0::" + UNIT_CG, score="0")

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 1, proc.stdout
        assert "verdict: ACCEPTED-NOT-APPLIED" in proc.stdout
        assert "at least one session process reads >= 0" in proc.stdout
        assert f"unit cgroup: {UNIT_CG}" in proc.stdout

    def test_only_context_processes_is_unknown_never_ok(self, tmp_path):
        """No SESSION process: the verb cannot tell -- never OK.

        Only context processes (a managed .service here) exist, so nothing
        tells us where the seat runs or whether the drop-in reaches it. OK
        would claim protection for a process set the verb never found.
        """
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(
            root, 101, comm="engramd-owner", cgroup=CONTEXT_SERVICE_CG, score="-100"
        )

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        assert proc.returncode == 3, proc.stdout
        assert "verdict: UNKNOWN" in proc.stdout
        assert "no session process" in proc.stdout
        # The reason must name the rule's actual boundary: the seat's user
        # slice. A logind session-*.scope under it is SESSION now, so it must
        # not be named as an example of context.
        assert f"outside user-{os.geteuid()}.slice" in proc.stdout
        assert "verdict: OK" not in proc.stdout
        assert 101 in _rows(proc.stdout), "the context process was not printed"

    def test_run_leaves_scores_and_unit_property_unchanged(self, tmp_path):
        """Read-only: no process score changes and no setter is ever called."""
        log = tmp_path / "systemctl.calls"
        stub = _stub_dir(
            tmp_path,
            members=["testseat"],
            uid=os.geteuid(),
            requested="-200",
            memory="1073741824",
            log_systemctl=log,
        )
        root = tmp_path / "proc-fixture"
        root.mkdir()
        _proc_entry(root, 101, comm="claude", cgroup="0::" + UNIT_CG, score="-100")
        _proc_entry(
            root, 202, comm="engramd-owner", cgroup=CONTEXT_SERVICE_CG, score="100"
        )
        before = {
            pid: (root / str(pid) / "oom_score_adj").read_text()
            for pid in (101, 202)
        }

        proc = _run("oom-status", "testseat", path_dir=stub, proc_root=root)

        after = {
            pid: (root / str(pid) / "oom_score_adj").read_text()
            for pid in (101, 202)
        }
        assert before == after, "the verb rewrote a process's oom_score_adj"
        assert "verdict:" in proc.stdout
        assert f"unit cgroup: {UNIT_CG}" in proc.stdout

        calls = log.read_text().splitlines()
        assert calls, "the verb never queried systemctl"
        assert all(call.startswith("systemctl show ") for call in calls), calls
        assert not any("set-property" in call for call in calls), calls
