"""Unit tests for src/engram/tools/supervision_probe.py (#2749, part of #2652).

Follows tests/test_proc_identity.py's convention: import the module directly
from src/engram/tools/ (sys.path append, no package install needed), and
inject synthetic filesystem/process state via fixtures + monkeypatched
subprocess.run rather than spawning real processes or requiring a live
systemd/launchd host -- these tests must pass on any CI runner, Linux or
Darwin, regardless of what supervision is actually configured there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _REPO_ROOT / "src" / "engram" / "tools"
sys.path.append(str(_TOOLS))

import supervision_probe as sp  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_proc(tmp_path):
    """A fresh synthetic /proc dir per test."""
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    return proc_dir


def _write_cgroup(proc_dir: Path, pid: str, content: bytes) -> None:
    pid_dir = proc_dir / pid
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "cgroup").write_bytes(content)


def _write_stat(proc_dir: Path, pid: str, starttime_ticks: int, comm: str = "python3") -> None:
    """Synthetic /proc/<pid>/stat -- field 22 (starttime) is the 20th
    whitespace-separated field AFTER the closing ')' of the comm field
    (fields 3-21 are 19 filler values before starttime at field 22)."""
    pid_dir = proc_dir / pid
    pid_dir.mkdir(parents=True, exist_ok=True)
    # after comm: field2=comm already consumed by the ')' split; fields
    # 3..21 are 19 values total before field 22 (starttime). Build exactly
    # 19 filler fields then starttime as the 20th token after ')'.
    after_comm_fields = ["0"] * 19
    after_comm_fields[19 - 1] = str(starttime_ticks)  # field 22 == 20th token after ')'
    stat_line = f"{pid} ({comm}) R " + " ".join(after_comm_fields)
    (pid_dir / "stat").write_text(stat_line)


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


# ---------------------------------------------------------------------------
# criterion 2 (Linux): cgroup verdict + grep exit-code discipline
# ---------------------------------------------------------------------------


class TestCgroupVerdict:
    def test_pass_when_marker_absent(self, synthetic_proc):
        """grep exit 1 (no match) -> PASS."""
        _write_cgroup(synthetic_proc, "100", b"0::/user.slice/user@501.service\n")
        verdict = sp._read_cgroup_verdict("100", str(synthetic_proc))
        assert verdict.status == "PASS"

    def test_fail_when_session_marker_present(self, synthetic_proc):
        """grep exit 0 (match) -> FAIL -- the exact pathology criterion 2 exists to catch."""
        _write_cgroup(synthetic_proc, "101", b"0::/user.slice/agent-session@luria.service\n")
        verdict = sp._read_cgroup_verdict("101", str(synthetic_proc))
        assert verdict.status == "FAIL"

    def test_error_on_missing_pid_ariadne_control(self, synthetic_proc):
        """Ariadne's control: /proc/999999/cgroup (nonexistent) -> ERROR, never PASS."""
        verdict = sp._read_cgroup_verdict("999999", str(synthetic_proc))
        assert verdict.status == "ERROR"

    def test_error_never_collapses_into_pass(self, synthetic_proc):
        """A dead pid's exit-2-equivalent (unreadable) must never read as PASS
        -- the naive `!`/`|| pass` idiom this rule exists to forbid."""
        verdict = sp._read_cgroup_verdict("nonexistent-pid", str(synthetic_proc))
        assert verdict.status != "PASS"
        assert verdict.status == "ERROR"

    def test_empty_cgroup_file_is_error_not_pass(self, synthetic_proc):
        """Answers Ariadne's named open question directly: a zero-byte file
        gives grep the same exit 1 as a genuine clean non-match -- this must
        NOT read as PASS. A dead instrument must never read as a clean
        answer."""
        _write_cgroup(synthetic_proc, "102", b"")
        verdict = sp._read_cgroup_verdict("102", str(synthetic_proc))
        assert verdict.status == "ERROR"
        assert "empty" in verdict.why

    def test_grep_exit_2_reads_as_error(self, synthetic_proc, monkeypatch):
        """A live-but-unreadable cgroup file (e.g. a genuine permission
        boundary) makes grep itself exit 2 -- must read as ERROR, distinct
        from both PASS (exit 1) and FAIL (exit 0)."""
        _write_cgroup(synthetic_proc, "103", b"something\n")

        def fake_run(cmd, **kwargs):
            assert cmd[0] == "grep"
            return _FakeCompletedProcess(returncode=2)

        monkeypatch.setattr(subprocess, "run", fake_run)
        verdict = sp._read_cgroup_verdict("103", str(synthetic_proc))
        assert verdict.status == "ERROR"


# ---------------------------------------------------------------------------
# pid resolution: zero-or-many
# ---------------------------------------------------------------------------


class TestResolvePid:
    def test_single_match_found(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0, "4242\n")
        )
        r = sp.resolve_pid("/some/path/server.py")
        assert r.status == "found"
        assert r.pid == "4242"

    def test_zero_matches_is_not_found(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompletedProcess(1, "")
        )
        r = sp.resolve_pid("/some/path/server.py")
        assert r.status == "not-found"
        assert r.pid is None

    def test_many_matches_is_ambiguous_never_a_literal_pid(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0, "111\n222\n333\n")
        )
        r = sp.resolve_pid("/some/path/server.py")
        assert r.status == "ambiguous"
        assert r.pid is None
        assert r.count == 3


class TestProbeOnePidResolutionShapes:
    """probe_one's own zero/many handling -- the pinned "zero=FAIL,
    many=ERROR" asymmetry, not just resolve_pid's own status labels."""

    def test_zero_matches_reports_fail_not_error(self, monkeypatch, synthetic_proc):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompletedProcess(1, "")
        )
        check = sp.ProcessCheck("daemon", "/nonexistent", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, is_ok = sp.probe_one("testseat", check, proc_root=str(synthetic_proc))
        assert "pid=none" in line
        assert "cgroup=FAIL:not-found" in line
        assert is_ok is False

    def test_many_matches_never_cites_a_literal_pid(self, monkeypatch, synthetic_proc):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0, "111\n222\n")
        )
        check = sp.ProcessCheck("daemon", "/nonexistent", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, is_ok = sp.probe_one("testseat", check, proc_root=str(synthetic_proc))
        assert "pid=ambiguous" in line
        assert "111" not in line
        assert "222" not in line
        assert "ERROR:2-matches" in line
        assert is_ok is False


def _dispatch_by_command(pgrep_pid="4242", grep_rc=1, systemctl_stdout="", launchctl_stdout="", launchctl_rc=0):
    """Build a fake subprocess.run that routes by argv[0], for tests that
    exercise probe_one's full "pid found" path -- which calls pgrep, then
    either (grep + systemctl) on Linux or (launchctl) on Darwin, in sequence.
    """

    def fake_run(cmd, **kwargs):
        tool = cmd[0]
        if tool == "pgrep":
            return _FakeCompletedProcess(0, f"{pgrep_pid}\n")
        if tool == "grep":
            return _FakeCompletedProcess(grep_rc)
        if tool == "systemctl":
            return _FakeCompletedProcess(0, systemctl_stdout)
        if tool == "launchctl":
            return _FakeCompletedProcess(launchctl_rc, launchctl_stdout)
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


class TestProbeOneFoundPath:
    """The "pid resolved, full check runs" path -- reviewer-fairy finding on
    this PR: the original suite only ever exercised not-found/ambiguous."""

    def test_linux_pass_reports_ok(self, monkeypatch, synthetic_proc):
        _write_cgroup(synthetic_proc, "4242", b"irrelevant\n")
        monkeypatch.setattr(
            subprocess,
            "run",
            _dispatch_by_command(
                pgrep_pid="4242",
                grep_rc=1,  # PASS: marker absent
                systemctl_stdout="LoadState=not-found\nActiveState=inactive\nActiveEnterTimestampMonotonic=0\n",
            ),
        )
        check = sp.ProcessCheck("daemon", "/some/daemon.py", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, is_ok = sp.probe_one("testseat", check, proc_root=str(synthetic_proc))
        assert line == "testseat daemon pid=4242 cgroup=PASS start=UNKNOWN supervisor=none"
        assert is_ok is True

    def test_linux_fail_reports_not_ok(self, monkeypatch, synthetic_proc):
        _write_cgroup(synthetic_proc, "4242", b"agent-session@luria.service\n")
        monkeypatch.setattr(
            subprocess,
            "run",
            _dispatch_by_command(pgrep_pid="4242", grep_rc=0),  # FAIL: marker present
        )
        check = sp.ProcessCheck("daemon", "/some/daemon.py", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, is_ok = sp.probe_one("testseat", check, proc_root=str(synthetic_proc))
        assert "cgroup=FAIL" in line
        assert "cgroup=FAIL:" not in line  # plain FAIL (not the not-found variant) carries no suffix
        assert is_ok is False

    def test_linux_supervisor_field_reports_actual_registration_not_expected_name(self, monkeypatch, synthetic_proc):
        """A checked-for unit that reads LoadState=not-found must report
        supervisor=none, never the unit name it merely checked for -- the bug
        this test would have caught before the supervisor-field fix."""
        _write_cgroup(synthetic_proc, "4242", b"irrelevant\n")
        monkeypatch.setattr(
            subprocess,
            "run",
            _dispatch_by_command(
                pgrep_pid="4242",
                grep_rc=1,
                systemctl_stdout="LoadState=not-found\nActiveState=inactive\nActiveEnterTimestampMonotonic=0\n",
            ),
        )
        check = sp.ProcessCheck("daemon", "/some/daemon.py", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, _ = sp.probe_one("testseat", check, proc_root=str(synthetic_proc))
        assert "supervisor=none" in line

    def test_linux_supervisor_field_reports_real_registration(self, monkeypatch, synthetic_proc):
        _write_cgroup(synthetic_proc, "4242", b"irrelevant\n")
        monkeypatch.setattr(
            subprocess,
            "run",
            _dispatch_by_command(
                pgrep_pid="4242",
                grep_rc=1,
                systemctl_stdout="LoadState=loaded\nActiveState=active\nActiveEnterTimestampMonotonic=100000000\n",
            ),
        )
        check = sp.ProcessCheck("daemon", "/some/daemon.py", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, _ = sp.probe_one("testseat", check, proc_root=str(synthetic_proc))
        assert "supervisor=engram-surface-daemon.service" in line

    def test_darwin_pass_reports_ok(self, monkeypatch, tmp_path):
        no_proc_root = tmp_path / "no-proc-here"
        monkeypatch.setattr(
            subprocess,
            "run",
            _dispatch_by_command(
                pgrep_pid="4242",
                launchctl_stdout="gui/501/com.engram.surface-daemon = {\n\tpid = 4242\n}\n",
            ),
        )
        check = sp.ProcessCheck("daemon", "/some/daemon.py", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, is_ok = sp.probe_one("testseat", check, proc_root=str(no_proc_root))
        assert line == "testseat daemon pid=4242 cgroup=PASS start=UNKNOWN supervisor=com.engram.surface-daemon"
        assert is_ok is True

    def test_darwin_no_registration_fails(self, monkeypatch, tmp_path):
        no_proc_root = tmp_path / "no-proc-here"
        monkeypatch.setattr(
            subprocess,
            "run",
            _dispatch_by_command(pgrep_pid="4242", launchctl_rc=113, launchctl_stdout=""),
        )
        check = sp.ProcessCheck("daemon", "/some/daemon.py", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, is_ok = sp.probe_one("testseat", check, proc_root=str(no_proc_root))
        assert "cgroup=FAIL:not registered under com.engram.surface-daemon" in line
        assert "supervisor=none" in line
        assert is_ok is False

    def test_moved_start_does_not_gate_exit_code(self, monkeypatch, synthetic_proc):
        """Reviewer-fairy finding: only criterion 2 (cgroup) gates the exit
        code, per #2749's own exit rule (FAIL/ERROR only) -- a MOVED start
        alongside a PASS cgroup is intentionally still is_ok=True. Locks in
        the exit-code contract explicitly rather than leaving it implicit."""
        _write_cgroup(synthetic_proc, "4242", b"irrelevant\n")
        _write_stat(synthetic_proc, "4242", starttime_ticks=100)  # far from unit's activation time
        monkeypatch.setattr(
            subprocess,
            "run",
            _dispatch_by_command(
                pgrep_pid="4242",
                grep_rc=1,  # PASS
                systemctl_stdout="LoadState=loaded\nActiveState=active\nActiveEnterTimestampMonotonic=999999999999\n",
            ),
        )
        check = sp.ProcessCheck("daemon", "/some/daemon.py", "engram-surface-daemon.service", "com.engram.surface-daemon")
        line, is_ok = sp.probe_one("testseat", check, proc_root=str(synthetic_proc))
        assert "cgroup=PASS" in line
        assert "start=MOVED" in line
        assert is_ok is True  # MOVED alone never flips the exit code


# ---------------------------------------------------------------------------
# criterion 1: start-time consistency
# ---------------------------------------------------------------------------


def _real_clk_tck() -> float:
    """This host's real SC_CLK_TCK (typically 100 on Linux), with the same
    fallback the code under test itself uses -- shared by every test that
    needs to convert ticks to seconds, so the conversion basis can never
    silently diverge between a test and `_proc_stat_start_seconds`/
    `_start_verdict`, which both call `os.sysconf("SC_CLK_TCK")` for real."""
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return 100


class TestProcStatStartSeconds:
    def test_parses_field_22_correctly(self, synthetic_proc):
        _write_stat(synthetic_proc, "200", starttime_ticks=5000)
        seconds = sp._proc_stat_start_seconds("200", str(synthetic_proc))
        assert seconds == pytest.approx(5000 / _real_clk_tck())

    def test_missing_stat_file_returns_none(self, synthetic_proc):
        assert sp._proc_stat_start_seconds("999999", str(synthetic_proc)) is None

    def test_comm_containing_spaces_and_parens_does_not_misalign_fields(self, synthetic_proc):
        """man 5 proc's own warning: comm can contain ')' -- rsplit(')', 1)
        must skip past the LAST paren, not the first."""
        pid_dir = synthetic_proc / "201"
        pid_dir.mkdir(parents=True)
        after_comm_fields = ["0"] * 19
        after_comm_fields[18] = "7777"
        stat_line = f"201 (weird (name) proc) R " + " ".join(after_comm_fields)
        (pid_dir / "stat").write_text(stat_line)
        seconds = sp._proc_stat_start_seconds("201", str(synthetic_proc))
        assert seconds == pytest.approx(7777 / _real_clk_tck())


class TestStartVerdict:
    def test_unchanged_within_tolerance(self, synthetic_proc):
        clk = _real_clk_tck()
        _write_stat(synthetic_proc, "300", starttime_ticks=1000 * clk)  # 1000s since boot
        unit_state = sp.SystemdUnitState("loaded", "active", active_enter_seconds=1001.0)
        assert sp._start_verdict("300", unit_state, str(synthetic_proc)) == "UNCHANGED"

    def test_moved_beyond_tolerance(self, synthetic_proc):
        clk = _real_clk_tck()
        _write_stat(synthetic_proc, "301", starttime_ticks=1000 * clk)
        unit_state = sp.SystemdUnitState("loaded", "active", active_enter_seconds=5000.0)
        assert sp._start_verdict("301", unit_state, str(synthetic_proc)) == "MOVED"

    def test_unknown_when_no_unit_state(self, synthetic_proc):
        _write_stat(synthetic_proc, "302", starttime_ticks=100)
        unit_state = sp.SystemdUnitState("", "", None)
        assert sp._start_verdict("302", unit_state, str(synthetic_proc)) == "UNKNOWN"

    def test_unknown_on_darwin_no_proc_root(self, tmp_path):
        """No /proc at all (Darwin) -- always UNKNOWN regardless of unit_state."""
        fake_proc = tmp_path / "does-not-exist"
        unit_state = sp.SystemdUnitState("loaded", "active", active_enter_seconds=42.0)
        assert sp._start_verdict("1", unit_state, str(fake_proc)) == "UNKNOWN"


# ---------------------------------------------------------------------------
# systemd registration read (LoadState/ActiveState/timing in one call)
# ---------------------------------------------------------------------------


class TestSystemdShow:
    def test_not_found_unit(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                0, "LoadState=not-found\nActiveState=inactive\nActiveEnterTimestampMonotonic=0\n"
            ),
        )
        state = sp._systemd_show("some.service")
        assert state.load_state == "not-found"
        assert state.active_enter_seconds is None

    def test_active_unit_parses_timing(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                0, "LoadState=loaded\nActiveState=active\nActiveEnterTimestampMonotonic=2000000\n"
            ),
        )
        state = sp._systemd_show("some.service")
        assert state.load_state == "loaded"
        assert state.active_state == "active"
        assert state.active_enter_seconds == pytest.approx(2.0)

    def test_nonzero_exit_is_unavailable_not_not_found(self, monkeypatch):
        """A dead/wedged bus (nonzero exit) must not be indistinguishable
        from a well-formed 'not-found' answer -- checked via the returned
        (empty) load_state, since this module never collapses the two."""
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompletedProcess(1, "")
        )
        state = sp._systemd_show("some.service")
        assert state.load_state == ""  # distinct from "not-found"
        assert state.active_enter_seconds is None

    def test_none_unit_is_unavailable(self):
        state = sp._systemd_show(None)
        assert state.load_state == ""
        assert state.active_enter_seconds is None


# ---------------------------------------------------------------------------
# launchd (Darwin) registration + verdict
# ---------------------------------------------------------------------------


class TestLaunchdVerdict:
    def test_pass_when_registered_pid_matches(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0, "gui/501/com.engram.x = {\n\tpid = 4242\n}\n"),
        )
        state = sp._launchd_state("com.engram.x")
        assert state.registered is True
        assert state.pid == "4242"
        verdict = sp._read_launchd_verdict("4242", "com.engram.x", state)
        assert verdict.status == "PASS"

    def test_fail_when_pid_mismatch(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0, "gui/501/com.engram.x = {\n\tpid = 4242\n}\n"),
        )
        state = sp._launchd_state("com.engram.x")
        verdict = sp._read_launchd_verdict("9999", "com.engram.x", state)
        assert verdict.status == "FAIL"

    def test_fail_when_not_registered_at_all(self, monkeypatch):
        """Live-verified shape on this repo's own Darwin dev seat: a
        nonexistent label makes `launchctl print` exit nonzero ("Could not
        find service ... in domain", rc=113)."""
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompletedProcess(113, "")
        )
        state = sp._launchd_state("com.engram.doesnotexist")
        assert state.registered is False
        assert state.pid is None
        verdict = sp._read_launchd_verdict("1", "com.engram.doesnotexist", state)
        assert verdict.status == "FAIL"
        assert "not registered" in verdict.why

    def test_fail_when_registered_but_no_live_pid(self, monkeypatch):
        """Live-verified shape: a registered-but-not-running job (`state =
        spawn scheduled`, `active count = 0`) prints no 'pid = ' line at
        all -- registered=True, pid=None. Must be FAIL, and must be
        distinguishable in the reason text from 'not registered'."""
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                0, "gui/501/com.engram.x = {\n\tactive count = 0\n\tstate = spawn scheduled\n}\n"
            ),
        )
        state = sp._launchd_state("com.engram.x")
        assert state.registered is True
        assert state.pid is None
        verdict = sp._read_launchd_verdict("1", "com.engram.x", state)
        assert verdict.status == "FAIL"
        assert "no live pid" in verdict.why

    def test_fail_when_no_label_defined(self):
        verdict = sp._read_launchd_verdict("1", None, None)
        assert verdict.status == "FAIL"
        assert "no LaunchAgent label" in verdict.why


# ---------------------------------------------------------------------------
# seat_name
# ---------------------------------------------------------------------------


class TestSeatName:
    def test_reads_config_json_agent_name(self, tmp_path):
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        (engram_home / "config.json").write_text('{"agent_name": "zephyr"}')
        assert sp.seat_name(engram_home) == "zephyr"

    def test_falls_back_to_user_env_with_agent_prefix_stripped(self, tmp_path, monkeypatch):
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()  # no config.json
        monkeypatch.setenv("USER", "agent-borges")
        monkeypatch.delenv("LOGNAME", raising=False)
        assert sp.seat_name(engram_home) == "borges"

    def test_missing_engram_home_degrades_to_env_fallback(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "does-not-exist"
        monkeypatch.setenv("USER", "sol")
        monkeypatch.delenv("LOGNAME", raising=False)
        assert sp.seat_name(engram_home) == "sol"


# ---------------------------------------------------------------------------
# main(): exit code discipline
# ---------------------------------------------------------------------------


class TestMainExitCode:
    def test_exit_nonzero_when_any_line_fails(self, monkeypatch, tmp_path, synthetic_proc, capsys):
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        monkeypatch.setenv("USER", "testseat")
        monkeypatch.delenv("LOGNAME", raising=False)
        # No processes findable at all -> both lines FAIL:not-found.
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompletedProcess(1, "")
        )
        rc = sp.main(["--engram-home", str(engram_home), "--proc-root", str(synthetic_proc)])
        assert rc == 1
        out = capsys.readouterr().out
        assert out.count("testseat") == 2
        assert "cgroup=FAIL:not-found" in out


# ---------------------------------------------------------------------------
# path resolution -- source vs. deployed (flattened hooks/) layout
# ---------------------------------------------------------------------------


class TestPathResolutionLayouts:
    """This project's CLAUDE.md prescribes exactly this regression-test
    shape for path resolvers that must work in both the source tree and the
    build-flattened deployed tree: recreate each layout under tmp_path and
    monkeypatch this module's own __file__ so `Path(__file__).resolve()`
    resolves against the synthetic tree instead of the real one -- reviewer-
    fairy finding on this PR (previously zero coverage on these functions,
    even though the pinned "zero matches -> FAIL" rule depends on them
    resolving to the right pgrep pattern)."""

    def test_server_py_resolves_when_colocated_with_tools(self, tmp_path, monkeypatch):
        """Mirrors BOTH layouts at once: tools/ sits at the same depth
        relative to server.py in source (src/engram/) and deployed
        (<plugin>/) trees alike -- this repo's build does not flatten
        tools/, only hooks/claude/ -> hooks/."""
        plugin_root = tmp_path / "plugin-root"
        tools_dir = plugin_root / "tools"
        tools_dir.mkdir(parents=True)
        (plugin_root / "server.py").write_text("# fake server.py")
        fake_module_file = tools_dir / "supervision_probe.py"
        monkeypatch.setattr(sp, "__file__", str(fake_module_file))

        resolved = sp.resolve_own_server_py_path(plugin_root / ".engram")
        assert resolved == str(plugin_root / "server.py")

    def test_server_py_falls_back_to_engram_home(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin-root"
        tools_dir = plugin_root / "tools"
        tools_dir.mkdir(parents=True)
        # No server.py colocated with tools/.
        fake_module_file = tools_dir / "supervision_probe.py"
        monkeypatch.setattr(sp, "__file__", str(fake_module_file))

        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        (engram_home / "server.py").write_text("# scatter-install server.py")

        resolved = sp.resolve_own_server_py_path(engram_home)
        assert resolved == str(engram_home / "server.py")

    def test_daemon_script_resolves_in_deployed_flattened_layout(self, tmp_path, monkeypatch):
        """The DEPLOYED tree: hooks/claude/ -> hooks/ (flattened, per this
        repo's plugin build) -- verified against the live deployed tree
        elsewhere in this session; this test recreates that shape locally so
        CI catches a regression without needing a real plugin install."""
        plugin_root = tmp_path / "plugin-root"
        tools_dir = plugin_root / "tools"
        tools_dir.mkdir(parents=True)
        hooks_dir = plugin_root / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "engram-surface-daemon.py").write_text("# deployed daemon")
        fake_module_file = tools_dir / "supervision_probe.py"
        monkeypatch.setattr(sp, "__file__", str(fake_module_file))

        resolved = sp.resolve_daemon_script_path(plugin_root / ".engram")
        assert resolved == str(hooks_dir / "engram-surface-daemon.py")

    def test_daemon_script_resolves_in_source_unflattened_layout(self, tmp_path, monkeypatch):
        """The SOURCE tree: hooks/claude/ (unflattened)."""
        repo_root = tmp_path / "src-engram"
        tools_dir = repo_root / "tools"
        tools_dir.mkdir(parents=True)
        hooks_claude_dir = repo_root / "hooks" / "claude"
        hooks_claude_dir.mkdir(parents=True)
        (hooks_claude_dir / "engram-surface-daemon.py").write_text("# source daemon")
        fake_module_file = tools_dir / "supervision_probe.py"
        monkeypatch.setattr(sp, "__file__", str(fake_module_file))

        resolved = sp.resolve_daemon_script_path(repo_root / ".engram")
        assert resolved == str(hooks_claude_dir / "engram-surface-daemon.py")

    def test_daemon_script_prefers_deployed_over_source_when_both_exist(self, tmp_path, monkeypatch):
        """Both candidates present (shouldn't happen in practice, but the
        resolver's own priority order should be deterministic, not
        filesystem-iteration-order-dependent)."""
        plugin_root = tmp_path / "plugin-root"
        tools_dir = plugin_root / "tools"
        tools_dir.mkdir(parents=True)
        hooks_dir = plugin_root / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "engram-surface-daemon.py").write_text("# deployed")
        hooks_claude_dir = hooks_dir / "claude"
        hooks_claude_dir.mkdir()
        (hooks_claude_dir / "engram-surface-daemon.py").write_text("# source")
        fake_module_file = tools_dir / "supervision_probe.py"
        monkeypatch.setattr(sp, "__file__", str(fake_module_file))

        resolved = sp.resolve_daemon_script_path(plugin_root / ".engram")
        assert resolved == str(hooks_dir / "engram-surface-daemon.py")

    def test_daemon_script_falls_back_to_engram_home(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin-root"
        tools_dir = plugin_root / "tools"
        tools_dir.mkdir(parents=True)
        # Neither hooks/ nor hooks/claude/ exists at all.
        fake_module_file = tools_dir / "supervision_probe.py"
        monkeypatch.setattr(sp, "__file__", str(fake_module_file))

        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        (engram_home / "engram-surface-daemon.py").write_text("# scatter-install daemon")

        resolved = sp.resolve_daemon_script_path(engram_home)
        assert resolved == str(engram_home / "engram-surface-daemon.py")
