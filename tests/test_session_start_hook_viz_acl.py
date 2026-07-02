"""Tests for _apply_pending_viz_acl in the SessionStart hook.

Issue #1525: newly-born agents lack the viz/operator ACL on ~/.engram because
finalize-name's Step 7 setfacl is gated on [[ -d "$_engram_dir" ]], which is
always false when the agent installs ENGRAM post-finalize.

Fix: finalize-name writes ~/.engram-finalize-pending.json (HOME root) and the
SessionStart hook picks it up after ENGRAM is installed.

Tests cover the six cases specified in the PR:
  a. No marker → no-op, no setfacl called.
  b. Marker + .engram exists + ACL absent → calls the 3 setfacl commands + removes marker.
  c. Marker + no .engram → skips, marker REMAINS.
  d. ACL already present (getfacl shows user:<op>:) → removes marker, NO setfacl re-run.
  e. setfacl raises/fails → marker REMAINS, no exception propagates.
  f. Invalid viz_operator (e.g. 'a; rm -rf') → marker removed, no setfacl.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch


_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"


def _load_hook_module(monkeypatch, engram_home: Path):
    """Load the session-start hook module in isolation with a stubbed context_tracker."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))

    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)

    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location(
        "engram_session_start_hook", _HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_marker(home: Path, viz_operator: str = "spt", chosen_name: str = "testagent") -> Path:
    """Write a synthetic pending marker at HOME root."""
    marker = home / ".engram-finalize-pending.json"
    marker.write_text(json.dumps({
        "viz_operator": viz_operator,
        "chosen_name": chosen_name,
        "finalized_at": "2026-06-27T12:00:00Z",
    }))
    return marker


class _Completed:
    """Minimal subprocess.CompletedProcess stand-in."""
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Case a: no marker → no-op, no setfacl called
# ---------------------------------------------------------------------------

class TestNoMarker:
    def test_no_marker_returns_immediately(self, tmp_path, monkeypatch):
        """When no pending marker exists, _apply_pending_viz_acl is a no-op."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        home = tmp_path  # no marker at tmp_path / ".engram-finalize-pending.json"

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))

        run_mock = MagicMock()
        monkeypatch.setattr(mod.subprocess, "run", run_mock)

        # Must not raise
        mod._apply_pending_viz_acl(str(engram_home))

        run_mock.assert_not_called()

    def test_no_marker_does_not_remove_any_file(self, tmp_path, monkeypatch):
        """No files are removed when no marker exists."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        home = tmp_path

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))

        # Drop a sentinel to prove no removal happens
        sentinel = home / "sentinel.txt"
        sentinel.write_text("keep me")

        mod._apply_pending_viz_acl(str(engram_home))

        assert sentinel.exists(), "No files should be removed when marker is absent"


# ---------------------------------------------------------------------------
# Case b: marker + .engram exists + ACL absent → 3 setfacl calls + marker removed
# ---------------------------------------------------------------------------

class TestMarkerAppliesAcl:
    def _setup(self, tmp_path, monkeypatch):
        """Shared setup: home dir with marker, engram_home dir, no ACL yet."""
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()
        config_json = engram_home / "config.json"
        config_json.write_text(json.dumps({"agent_name": "testagent"}))
        marker = _write_marker(home, viz_operator="spt", chosen_name="testagent")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))

        # shutil.which returns a path (truthy) for both tools
        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        # Patch shutil inside the loaded module too
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        # getfacl returns output WITHOUT the operator entry (ACL absent)
        def fake_run(cmd, **kwargs):
            if cmd[0] == "getfacl":
                return _Completed(0, stdout="# file: .engram\nuser::rwx\ngroup::r-x\n")
            return _Completed(0)

        monkeypatch.setattr(mod.subprocess, "run", MagicMock(side_effect=fake_run))

        return mod, engram_home, marker, home

    def test_calls_setfacl_rX(self, tmp_path, monkeypatch):
        """setfacl -R -m u:<op>:rX is called when ACL is absent."""
        mod, engram_home, marker, home = self._setup(tmp_path, monkeypatch)
        mod._apply_pending_viz_acl(str(engram_home))
        calls = [c.args[0] for c in mod.subprocess.run.call_args_list]
        rX_call = next(
            (c for c in calls if "setfacl" in c and "-R" in c and "-m" in c
             and "-d" not in c and "rX" in " ".join(c)),
            None,
        )
        assert rX_call is not None, f"Expected setfacl -R -m u:spt:rX call, got: {calls}"

    def test_calls_setfacl_default_acl(self, tmp_path, monkeypatch):
        """setfacl -R -d -m u:<op>:rX (default ACL for inheritance) is called."""
        mod, engram_home, marker, home = self._setup(tmp_path, monkeypatch)
        mod._apply_pending_viz_acl(str(engram_home))
        calls = [c.args[0] for c in mod.subprocess.run.call_args_list]
        default_call = next(
            (c for c in calls if "setfacl" in c and "-d" in c and "rX" in " ".join(c)),
            None,
        )
        assert default_call is not None, f"Expected setfacl -R -d -m u:spt:rX call, got: {calls}"

    def test_calls_setfacl_config_rw(self, tmp_path, monkeypatch):
        """setfacl -m u:<op>:rw config.json is called when config.json exists."""
        mod, engram_home, marker, home = self._setup(tmp_path, monkeypatch)
        mod._apply_pending_viz_acl(str(engram_home))
        calls = [c.args[0] for c in mod.subprocess.run.call_args_list]
        config_call = next(
            (c for c in calls if "setfacl" in c and "rw" in " ".join(c)
             and "config.json" in " ".join(str(x) for x in c)),
            None,
        )
        assert config_call is not None, f"Expected setfacl -m u:spt:rw config.json call, got: {calls}"

    def test_marker_removed_on_success(self, tmp_path, monkeypatch):
        """Pending marker is removed after successful ACL application."""
        mod, engram_home, marker, home = self._setup(tmp_path, monkeypatch)
        mod._apply_pending_viz_acl(str(engram_home))
        assert not marker.exists(), "Pending marker must be removed on success"

    def test_setfacl_args_are_list_not_shell(self, tmp_path, monkeypatch):
        """All subprocess.run calls use list args (never shell=True)."""
        mod, engram_home, marker, home = self._setup(tmp_path, monkeypatch)
        mod._apply_pending_viz_acl(str(engram_home))
        for c in mod.subprocess.run.call_args_list:
            args, kwargs = c.args, c.kwargs
            cmd = args[0] if args else kwargs.get("args")
            assert isinstance(cmd, list), f"subprocess.run must use list args, got: {cmd!r}"
            assert kwargs.get("shell", False) is False, "shell=True must never be used"


# ---------------------------------------------------------------------------
# Case c: marker + no .engram → skips, marker REMAINS
# ---------------------------------------------------------------------------

class TestMarkerRetainedWhenEngramAbsent:
    def test_marker_remains_when_engram_home_absent(self, tmp_path, monkeypatch):
        """When engram_home doesn't exist yet, marker is left for next session."""
        home = tmp_path / "agent-home"
        home.mkdir()
        # engram_home does NOT exist (pre-install state)
        engram_home = home / ".engram"
        assert not engram_home.exists()

        marker = _write_marker(home, viz_operator="spt")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        run_mock = MagicMock()
        monkeypatch.setattr(mod.subprocess, "run", run_mock)

        mod._apply_pending_viz_acl(str(engram_home))

        assert marker.exists(), "Marker must REMAIN when engram_home is absent"
        run_mock.assert_not_called()

    def test_no_exception_when_engram_home_absent(self, tmp_path, monkeypatch):
        """No exception propagates when engram_home doesn't exist."""
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        _write_marker(home, viz_operator="spt")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(mod.subprocess, "run", MagicMock())

        # Must not raise
        mod._apply_pending_viz_acl(str(engram_home))


# ---------------------------------------------------------------------------
# Case d: ACL already present → removes marker, NO setfacl re-run
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_marker_removed_when_acl_already_present(self, tmp_path, monkeypatch):
        """When getfacl already shows user:<op>:, marker is removed without re-running setfacl."""
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()
        marker = _write_marker(home, viz_operator="spt")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        # getfacl already shows the operator entry
        def fake_run(cmd, **kwargs):
            if cmd[0] == "getfacl":
                return _Completed(0, stdout="user:spt:r-x\ngroup::r-x\n")
            return _Completed(0)

        run_mock = MagicMock(side_effect=fake_run)
        monkeypatch.setattr(mod.subprocess, "run", run_mock)

        mod._apply_pending_viz_acl(str(engram_home))

        assert not marker.exists(), "Marker must be removed when ACL is already present"

    def test_setfacl_not_called_when_acl_already_present(self, tmp_path, monkeypatch):
        """setfacl is NOT called when getfacl already shows the operator entry."""
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()
        _write_marker(home, viz_operator="spt")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        setfacl_calls = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "getfacl":
                return _Completed(0, stdout="user:spt:r-x\n")
            setfacl_calls.append(cmd)
            return _Completed(0)

        monkeypatch.setattr(mod.subprocess, "run", MagicMock(side_effect=fake_run))

        mod._apply_pending_viz_acl(str(engram_home))

        assert setfacl_calls == [], f"setfacl must NOT be called when ACL already present; got: {setfacl_calls}"


# ---------------------------------------------------------------------------
# Case e: setfacl raises/fails → marker REMAINS, no exception propagates
# ---------------------------------------------------------------------------

class TestSetfaclFailure:
    def _base_setup(self, tmp_path, monkeypatch):
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()
        marker = _write_marker(home, viz_operator="spt")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        return mod, engram_home, marker, home

    def test_marker_remains_when_setfacl_returns_nonzero(self, tmp_path, monkeypatch):
        """When setfacl returns non-zero exit, marker is NOT removed."""
        mod, engram_home, marker, home = self._base_setup(tmp_path, monkeypatch)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "getfacl":
                return _Completed(0, stdout="user::rwx\n")  # no spt entry
            # setfacl -R -m fails
            return _Completed(1)

        monkeypatch.setattr(mod.subprocess, "run", MagicMock(side_effect=fake_run))

        mod._apply_pending_viz_acl(str(engram_home))

        assert marker.exists(), "Marker must REMAIN when setfacl fails"

    def test_no_exception_when_setfacl_raises(self, tmp_path, monkeypatch):
        """No exception propagates when setfacl itself raises an OSError."""
        mod, engram_home, marker, home = self._base_setup(tmp_path, monkeypatch)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "getfacl":
                return _Completed(0, stdout="user::rwx\n")
            raise OSError("setfacl: Permission denied")

        monkeypatch.setattr(mod.subprocess, "run", MagicMock(side_effect=fake_run))

        # Must not raise
        mod._apply_pending_viz_acl(str(engram_home))

    def test_marker_remains_when_setfacl_raises(self, tmp_path, monkeypatch):
        """Marker is NOT removed when setfacl raises."""
        mod, engram_home, marker, home = self._base_setup(tmp_path, monkeypatch)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "getfacl":
                return _Completed(0, stdout="user::rwx\n")
            raise OSError("setfacl: Permission denied")

        monkeypatch.setattr(mod.subprocess, "run", MagicMock(side_effect=fake_run))

        mod._apply_pending_viz_acl(str(engram_home))

        assert marker.exists(), "Marker must REMAIN when setfacl raises"


# ---------------------------------------------------------------------------
# Case f: invalid viz_operator → marker removed, no setfacl
# ---------------------------------------------------------------------------

class TestInvalidVizOperator:
    def _setup_with_operator(self, tmp_path, monkeypatch, viz_operator: str):
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()
        marker = _write_marker(home, viz_operator=viz_operator)

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        run_mock = MagicMock()
        monkeypatch.setattr(mod.subprocess, "run", run_mock)
        return mod, engram_home, marker, run_mock

    def test_shell_injection_operator_removed_not_run(self, tmp_path, monkeypatch):
        """viz_operator with shell-injection chars removes marker, skips setfacl."""
        mod, engram_home, marker, run_mock = self._setup_with_operator(
            tmp_path, monkeypatch, viz_operator="a; rm -rf /"
        )
        mod._apply_pending_viz_acl(str(engram_home))
        assert not marker.exists(), "Marker must be removed for invalid viz_operator"
        # Only getfacl might be called if validation happens after tool check;
        # but setfacl must NOT be called with the malicious string
        setfacl_calls = [
            c.args[0] for c in run_mock.call_args_list
            if c.args and "setfacl" in str(c.args[0])
        ]
        assert setfacl_calls == [], (
            f"setfacl must not be called with invalid operator; got: {setfacl_calls}"
        )

    def test_uppercase_operator_removed_not_run(self, tmp_path, monkeypatch):
        """viz_operator with uppercase letters (not POSIX username chars) removes marker."""
        mod, engram_home, marker, run_mock = self._setup_with_operator(
            tmp_path, monkeypatch, viz_operator="SPT"
        )
        mod._apply_pending_viz_acl(str(engram_home))
        assert not marker.exists(), "Marker must be removed for uppercase viz_operator"

    def test_empty_operator_removes_marker(self, tmp_path, monkeypatch):
        """viz_operator empty string removes marker without running setfacl."""
        mod, engram_home, marker, run_mock = self._setup_with_operator(
            tmp_path, monkeypatch, viz_operator=""
        )
        mod._apply_pending_viz_acl(str(engram_home))
        assert not marker.exists(), "Marker must be removed when viz_operator is empty"
        run_mock.assert_not_called()

    def test_valid_operator_passes_validation(self, tmp_path, monkeypatch):
        """A valid POSIX-username viz_operator (e.g. 'spt') passes charset validation."""
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()
        marker = _write_marker(home, viz_operator="spt")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        # getfacl shows no entry → will attempt setfacl
        setfacl_was_called = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "getfacl":
                return _Completed(0, stdout="user::rwx\n")
            if cmd[0] == "setfacl":
                setfacl_was_called.append(cmd)
            return _Completed(0)

        monkeypatch.setattr(mod.subprocess, "run", MagicMock(side_effect=fake_run))

        mod._apply_pending_viz_acl(str(engram_home))

        assert setfacl_was_called, "Valid operator 'spt' must proceed to setfacl calls"


# ---------------------------------------------------------------------------
# Outer guard: function must NEVER propagate exceptions under any circumstance
# ---------------------------------------------------------------------------

class TestNeverRaises:
    def test_completely_broken_marker_no_exception(self, tmp_path, monkeypatch):
        """Malformed (non-JSON) marker content must not cause any exception, AND
        a corrupt marker must be DELETED (not zombie every future session)."""
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()

        marker = home / ".engram-finalize-pending.json"
        marker.write_bytes(b"\xff\xfe not json at all \x00")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        # Must not raise regardless
        mod._apply_pending_viz_acl(str(engram_home))

        # A corrupt marker is unrecoverable → it must be deleted, not retained
        # (locks in the parse-error deletion policy against a silent-zombie regression).
        assert not marker.exists()

    def test_setfacl_unavailable_retains_marker(self, tmp_path, monkeypatch):
        """When setfacl/getfacl aren't installed, the marker is RETAINED (a richer
        environment can apply the ACL later) and NO subprocess is run."""
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()
        _write_marker(home, viz_operator="spt")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: None)  # tools absent
        ran = {"run": False}
        monkeypatch.setattr(
            mod.subprocess, "run",
            lambda *a, **k: ran.__setitem__("run", True),
        )

        mod._apply_pending_viz_acl(str(engram_home))

        marker = home / ".engram-finalize-pending.json"
        assert marker.exists(), "marker must be retained when setfacl is unavailable"
        assert not ran["run"], "no subprocess should run when tools are absent"

    def test_exception_in_subprocess_run_no_propagation(self, tmp_path, monkeypatch):
        """subprocess.run raising RuntimeError must not propagate."""
        home = tmp_path / "agent-home"
        home.mkdir()
        engram_home = home / ".engram"
        engram_home.mkdir()
        _write_marker(home, viz_operator="spt")

        mod = _load_hook_module(monkeypatch, engram_home)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        import shutil as _shutil
        monkeypatch.setattr(_shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(mod.subprocess, "run", MagicMock(side_effect=RuntimeError("boom")))

        # Must not raise
        mod._apply_pending_viz_acl(str(engram_home))
