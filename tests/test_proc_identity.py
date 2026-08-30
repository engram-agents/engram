"""Unit tests for src/engram/tools/_proc_identity.py (#2321).

Extraction target: this module used to be a 1629-byte inline `python3 -c`
block, byte-identical in forum-mention-monitor.sh and
forum-updates-monitor.sh (see that module's own docstring for the full
history). tests/test_forum_monitor_duplicate_warn.py already exercises the
REAL end-to-end behavior (live process spawning, real /proc reads) for both
callers post-extraction — this file adds fast, granular, no-subprocess unit
coverage of check_process_identity()'s branches directly, using
ENGRAM_PROC_DIR to point at a synthetic procfs rather than the real one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = _REPO_ROOT / "src" / "engram" / "tools"
sys.path.append(str(_TOOLS))

from _proc_identity import check_process_identity  # noqa: E402


def _write_cmdline(proc_dir: Path, pid: str, argv: list[str]) -> None:
    """Build a synthetic /proc/<pid>/cmdline: NUL-separated argv, matching
    the real kernel's own format exactly (trailing NUL included, mirroring
    a real /proc/<pid>/cmdline file)."""
    pid_dir = proc_dir / pid
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    # check_process_identity's /proc-vs-fallback branch decision reads
    # <proc_dir>/self/cmdline's existence, not the target pid's — a
    # synthetic procfs must provide it too, mirroring the real kernel
    # exposing /proc/self for the reading process itself.
    self_dir = proc_dir / "self"
    if not self_dir.exists():
        self_dir.mkdir(parents=True, exist_ok=True)
        (self_dir / "cmdline").write_bytes(b"pytest\0")


@pytest.fixture
def synthetic_proc(tmp_path, monkeypatch):
    """A fresh synthetic procfs dir per test, wired via ENGRAM_PROC_DIR."""
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    monkeypatch.setenv("ENGRAM_PROC_DIR", str(proc_dir))
    return proc_dir


class TestProcVerifiedBranch:
    """rc=0: the strong, /proc-verified duplicate confirmation."""

    def test_genuine_duplicate_confirmed(self, synthetic_proc):
        _write_cmdline(synthetic_proc, "4242", ["bash", "/opt/tools/forum-mention-monitor.sh", "--purpose", "x"])
        code, text = check_process_identity("4242", "forum-mention-monitor.sh")
        assert code == 0
        assert "forum-mention-monitor.sh" in text

    def test_login_shell_argv0_still_matches(self, synthetic_proc):
        """argv[0] == '-bash' (a login shell) must not defeat the interpreter
        check — the historical login-shell false-negative this exact logic
        was fixed for (#1933's case 2b)."""
        _write_cmdline(synthetic_proc, "4243", ["-bash", "forum-mention-monitor.sh"])
        code, text = check_process_identity("4243", "forum-mention-monitor.sh")
        assert code == 0

    def test_decoy_argument_is_not_a_match(self, synthetic_proc):
        """A process whose argv merely CONTAINS the script name as a
        one-liner's string argument (mirrors the real #1865/#1933 boot-charge
        repro) is a genuinely different interpreter, not a shell — no
        interpreter in _SHELL_INTERPRETERS names it, so it's correctly
        rejected at the interp check, never reaching the argv scan."""
        _write_cmdline(synthetic_proc, "4244", ["python3", "-c", "print('forum-mention-monitor.sh')"])
        code, text = check_process_identity("4244", "forum-mention-monitor.sh")
        assert code == 1
        assert text == ""

    def test_shell_running_a_different_script_is_not_a_match(self, synthetic_proc):
        _write_cmdline(synthetic_proc, "4245", ["bash", "/opt/tools/some-other-script.sh"])
        code, text = check_process_identity("4245", "forum-mention-monitor.sh")
        assert code == 1

    def test_cmdline_truncated_to_100_chars(self, synthetic_proc):
        long_arg = "x" * 300
        _write_cmdline(synthetic_proc, "4246", ["bash", "forum-mention-monitor.sh", long_arg])
        code, text = check_process_identity("4246", "forum-mention-monitor.sh")
        assert code == 0
        assert len(text) == 100

    def test_process_gone_mid_read_is_silent(self, synthetic_proc):
        """No cmdline file at all for this pid (but /proc/self exists, so
        we're on the /proc-capable branch) -- ENOENT, treated as a race lost,
        not an error."""
        code, text = check_process_identity("99999", "forum-mention-monitor.sh")
        assert code == 1
        assert text == ""

    def test_empty_argv_is_not_a_match(self, synthetic_proc):
        pid_dir = synthetic_proc / "4247"
        pid_dir.mkdir()
        (pid_dir / "cmdline").write_bytes(b"")
        code, _ = check_process_identity("4247", "forum-mention-monitor.sh")
        assert code == 1

    def test_permission_denied_reports_could_not_verify(self, synthetic_proc, monkeypatch):
        """A real read failure that is NOT ENOENT/ESRCH must surface as rc=2
        ('could not verify'), never silently collapse into rc=1 ('not a
        match') -- the exact distinction #2321's source module's docstring
        calls out as load-bearing."""
        _write_cmdline(synthetic_proc, "4248", ["bash", "forum-mention-monitor.sh"])
        cmdline_path = synthetic_proc / "4248" / "cmdline"
        real_open = open

        def _denying_open(path, *args, **kwargs):
            if str(path) == str(cmdline_path):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _denying_open)
        code, text = check_process_identity("4248", "forum-mention-monitor.sh")
        assert code == 2
        assert text  # a non-empty reason string, per the module's contract


class TestPsFallbackBranch:
    """rc=3 / weak-match: the macOS (/proc-absent) portable fallback."""

    def test_no_proc_uses_ps_fallback(self, tmp_path, monkeypatch):
        # An ENGRAM_PROC_DIR that exists but has no "self/cmdline" simulates
        # "no /proc on this platform" without needing an actual macOS host.
        empty_proc = tmp_path / "no-proc-here"
        empty_proc.mkdir()
        monkeypatch.setenv("ENGRAM_PROC_DIR", str(empty_proc))

        import subprocess

        class _FakeCompletedProcess:
            stdout = "bash /opt/tools/forum-mention-monitor.sh --purpose x\n"

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess())
        code, text = check_process_identity("5000", "forum-mention-monitor.sh")
        assert code == 3
        assert "forum-mention-monitor.sh" in text

    def test_ps_fallback_no_such_pid_is_silent(self, tmp_path, monkeypatch):
        empty_proc = tmp_path / "no-proc-here"
        empty_proc.mkdir()
        monkeypatch.setenv("ENGRAM_PROC_DIR", str(empty_proc))

        import subprocess

        class _FakeCompletedProcess:
            stdout = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess())
        code, text = check_process_identity("5001", "forum-mention-monitor.sh")
        assert code == 1

    def test_ps_fallback_raising_reports_could_not_verify(self, tmp_path, monkeypatch):
        empty_proc = tmp_path / "no-proc-here"
        empty_proc.mkdir()
        monkeypatch.setenv("ENGRAM_PROC_DIR", str(empty_proc))

        import subprocess

        def _raising_run(*a, **k):
            raise OSError("ps not found")

        monkeypatch.setattr(subprocess, "run", _raising_run)
        code, text = check_process_identity("5002", "forum-mention-monitor.sh")
        assert code == 2
        assert text

    def test_ps_fallback_weaker_string_split_can_overmatch(self, tmp_path, monkeypatch):
        """Documents the KNOWN weakness (module docstring, #2134): ps's
        space-joined args= means a quoted-argument boundary can be
        misread as a real token boundary. Not a bug to fix here -- a
        positive control proving the documented limitation is real,
        reproducible, and exactly as described."""
        empty_proc = tmp_path / "no-proc-here"
        empty_proc.mkdir()
        monkeypatch.setenv("ENGRAM_PROC_DIR", str(empty_proc))

        import subprocess

        class _FakeCompletedProcess:
            stdout = 'bash -c "echo hi forum-mention-monitor.sh done"\n'

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompletedProcess())
        code, _ = check_process_identity("5003", "forum-mention-monitor.sh")
        # Over-matches: "forum-mention-monitor.sh" appears as its own
        # whitespace-split token inside the quoted -c argument.
        assert code == 3


class TestEnvOverrideDefault:
    def test_absent_env_var_uses_real_proc_by_default(self, monkeypatch):
        """No ENGRAM_PROC_DIR set at all: must resolve against the REAL
        system /proc, not silently no-op. Verified against this test
        process's own real pid (guaranteed to exist and be a genuine
        pytest/python interpreter, not a shell -- so the correct, provable
        outcome is rc=1, not an exception or a false rc=0)."""
        monkeypatch.delenv("ENGRAM_PROC_DIR", raising=False)
        if not os.path.exists("/proc/self/cmdline"):
            pytest.skip("no real /proc on this platform")
        code, _ = check_process_identity(str(os.getpid()), "forum-mention-monitor.sh")
        assert code == 1  # this test process is python, not bash -- correctly not a match
