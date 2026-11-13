"""Tests for the agent-registration helper in engram-session-start-hook.py.

Part 1 of issue #1266: SessionStart writes/refreshes a JSON registration file
in /home/agents-shared/agents/ (or an override dir) so peers can discover
co-host vs cross-host topology.

Covers:
  - Well-formed JSON is written atomically on first call.
  - Re-run overwrites cleanly (idempotent).
  - Missing registry dir is created.
  - Missing shared-fs root -> no-op (no raise).
  - Unwritable registry dir -> no-op (no raise).
  - Empty agent_name -> no-op (no raise).
"""

import importlib.util
import json
import os
import socket
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = (
    _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
)

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(monkeypatch, engram_home: Path):
    """Load the session-start hook as a module, stubbing context_tracker."""
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


# ---------------------------------------------------------------------------
# write_agent_registration unit tests
# ---------------------------------------------------------------------------


class TestWriteAgentRegistration:
    def test_writes_well_formed_json(self, tmp_path, monkeypatch):
        """Written file is valid JSON with all required fields."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        # The parent (shared root) must exist for the write to proceed.
        registry_dir.parent.mkdir(parents=True)

        mod.write_agent_registration("borges", registry_dir=str(registry_dir))

        out_file = registry_dir / "borges.json"
        assert out_file.exists(), "registration file was not created"

        data = json.loads(out_file.read_text())
        assert data["agent_name"] == "borges"
        assert data["hostname"] == socket.gethostname()
        assert isinstance(data["pid"], int)
        assert data["pid"] > 0
        # ISO-8601 UTC format check
        assert data["registered_at"].endswith("Z")
        assert "T" in data["registered_at"]

    def test_overwrites_on_second_call(self, tmp_path, monkeypatch):
        """Second call overwrites cleanly; JSON remains valid."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.parent.mkdir(parents=True)

        mod.write_agent_registration("borges", registry_dir=str(registry_dir))
        first_mtime = (registry_dir / "borges.json").stat().st_mtime

        # Call again — should overwrite atomically
        mod.write_agent_registration("borges", registry_dir=str(registry_dir))
        out_file = registry_dir / "borges.json"

        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert data["agent_name"] == "borges"
        # No stale .tmp file left behind
        tmp_files = list(registry_dir.glob("*.tmp"))
        assert tmp_files == [], f"stale temp files found: {tmp_files}"

    def test_creates_missing_registry_dir(self, tmp_path, monkeypatch):
        """Registry dir is created when it doesn't exist yet."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        shared_root = tmp_path / "agents-shared"
        shared_root.mkdir()  # shared root exists
        registry_dir = shared_root / "agents"
        assert not registry_dir.exists()

        mod.write_agent_registration("ariadne", registry_dir=str(registry_dir))

        assert registry_dir.exists(), "registry dir was not created"
        assert (registry_dir / "ariadne.json").exists()

    def test_no_op_when_shared_root_absent(self, tmp_path, monkeypatch):
        """When the shared-fs root doesn't exist, silently no-op (no raise)."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "nonexistent-shared" / "agents"
        # The parent (shared root) does NOT exist — this simulates single-host
        # install with no shared filesystem.
        assert not registry_dir.parent.exists()

        # Must not raise
        mod.write_agent_registration("borges", registry_dir=str(registry_dir))
        # Nothing was written
        assert not registry_dir.exists()

    def test_no_op_on_chmod_eperm(self, tmp_path, monkeypatch):
        """A registry dir whose mode we cannot change (another agent's) is a no-op.

        #4026: an existing directory WE own is repaired by the mode assertion, so
        the remaining unwritable case is an fchmod we are not allowed to make (the
        dir belongs to another agent -> EPERM). Simulate that, leaving the dir at
        0o555 so the write itself still fails. No raise, nothing registered.
        """
        if os.getuid() == 0:
            pytest.skip("root bypasses permission checks")

        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        shared_root = tmp_path / "agents-shared"
        shared_root.mkdir()
        registry_dir = shared_root / "agents"
        registry_dir.mkdir(mode=0o555)  # read + execute, no write
        os.chmod(registry_dir, 0o555)

        real_fchmod, real_fstat = os.fchmod, os.fstat

        def _deny_dir_fchmod(fd, mode, *args, **kwargs):
            if stat.S_ISDIR(real_fstat(fd).st_mode):
                raise PermissionError("simulated: registry dir belongs to another agent")
            return real_fchmod(fd, mode, *args, **kwargs)

        monkeypatch.setattr(mod.os, "fchmod", _deny_dir_fchmod)
        try:
            # Must not raise
            status = mod.write_agent_registration(
                "borges", registry_dir=str(registry_dir)
            )
            # No file written
            assert not (registry_dir / "borges.json").exists()
            assert status["registered"] is False
            assert status["dir_error"]
        finally:
            # Restore fchmod (and perms) so tmp_path cleanup succeeds
            monkeypatch.setattr(mod.os, "fchmod", real_fchmod)
            registry_dir.chmod(0o755)

    def test_no_op_on_empty_agent_name(self, tmp_path, monkeypatch):
        """Empty agent_name is a no-op (no file written, no raise)."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        shared_root = tmp_path / "agents-shared"
        shared_root.mkdir()
        registry_dir = shared_root / "agents"

        mod.write_agent_registration("", registry_dir=str(registry_dir))
        assert not registry_dir.exists()


# ---------------------------------------------------------------------------
# #4026: the registry must end up group-writable, whatever the umask
# ---------------------------------------------------------------------------


def _registry_pair(tmp_path):
    """A shared root that exists plus the registry dir path under it."""
    shared_root = tmp_path / "agents-shared"
    shared_root.mkdir()
    return shared_root, shared_root / "agents"


class TestRegistryPermissions4026:
    def test_umask_022_fresh_dir_is_group_writable_and_setgid(
        self, tmp_path, monkeypatch
    ):
        """#4026 cell 1: os.makedirs' mode goes through the umask (022 -> 2755)."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)

        old_umask = os.umask(0o022)
        try:
            status = mod.write_agent_registration(
                "borges", registry_dir=str(registry_dir)
            )
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE(os.stat(registry_dir).st_mode) == 0o2775
        file_st = os.stat(registry_dir / "borges.json")
        assert file_st.st_mode & 0o040, "registration file is not group-readable"
        assert status["registered"] is True

    def test_existing_dir_at_wrong_mode_is_repaired(self, tmp_path, monkeypatch):
        """#4026 cell 2: exist_ok=True never repairs an existing directory."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)
        registry_dir.mkdir()
        os.chmod(registry_dir, 0o2750)
        assert stat.S_IMODE(os.stat(registry_dir).st_mode) == 0o2750

        status = mod.write_agent_registration("borges", registry_dir=str(registry_dir))

        assert stat.S_IMODE(os.stat(registry_dir).st_mode) == 0o2775
        assert status["registered"] is True

    def test_umask_077_file_is_still_group_readable(self, tmp_path, monkeypatch):
        """#4026 cell 3: a 077 session would land the file 0600, defeating the registry."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)

        old_umask = os.umask(0o077)
        try:
            status = mod.write_agent_registration(
                "borges", registry_dir=str(registry_dir)
            )
        finally:
            os.umask(old_umask)

        mode = stat.S_IMODE(os.stat(registry_dir / "borges.json").st_mode)
        assert mode & 0o040, f"registration file is not group-readable: {mode:o}"
        assert mode & 0o020 == 0, "registration file is group-writable"
        assert mode == 0o644
        assert status["registered"] is True

    def test_chmod_failure_is_soft_and_reported(self, tmp_path, monkeypatch):
        """#4026 cell 4: a directory owned by another agent raises EPERM on chmod.

        SessionStart must still run, and the failure must be visible in the
        returned status rather than silent.
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)
        # Pre-create it writable so the registration WRITE itself still succeeds.
        registry_dir.mkdir()
        os.chmod(registry_dir, 0o2775)

        real_fchmod, real_fstat = os.fchmod, os.fstat

        def _deny_dir_fchmod(fd, mode, *args, **kwargs):
            if stat.S_ISDIR(real_fstat(fd).st_mode):
                raise PermissionError("simulated: not the registry dir's owner")
            return real_fchmod(fd, mode, *args, **kwargs)

        monkeypatch.setattr(mod.os, "fchmod", _deny_dir_fchmod)

        status = mod.write_agent_registration("borges", registry_dir=str(registry_dir))

        assert status["registered"] is True
        assert (registry_dir / "borges.json").exists()
        assert status["dir_mode"] is None
        assert isinstance(status["dir_error"], str) and status["dir_error"]

    def test_positive_control_registers_normally(self, tmp_path, monkeypatch):
        """#4026 cell 5: the normal path still registers and reports its file."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)

        status = mod.write_agent_registration("borges", registry_dir=str(registry_dir))

        assert isinstance(status, dict)
        assert status["registered"] is True
        assert status["file_path"] and os.path.exists(status["file_path"])
        assert status["dir_error"] is None
        assert status["file_error"] is None

    def test_file_is_not_group_writable_but_dir_is_group_writable(
        self, tmp_path, monkeypatch
    ):
        """#4026 security property: the DIRECTORY is shared, the FILE is not.

        A group-writable directory is what lets each co-tenant create its own
        registration file. A group-writable FILE would let any co-tenant
        rewrite another agent's record, and that record feeds peer topology
        and liveness — an impersonation surface for no benefit.
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)

        mod.write_agent_registration("borges", registry_dir=str(registry_dir))

        file_mode = stat.S_IMODE(os.stat(registry_dir / "borges.json").st_mode)
        assert file_mode & 0o220 == 0o200, (
            f"file must be owner-writable, not group-writable: {file_mode:o}"
        )
        assert file_mode & 0o020 == 0, f"file is group-writable: {file_mode:o}"
        assert file_mode & 0o040, f"file is not group-readable: {file_mode:o}"

        dir_mode = stat.S_IMODE(os.stat(registry_dir).st_mode)
        assert dir_mode & 0o020, f"dir is not group-writable: {dir_mode:o}"
        assert dir_mode & 0o2000, f"dir is not setgid: {dir_mode:o}"

    def test_symlinked_registry_dir_is_not_followed(self, tmp_path, monkeypatch):
        """#4682: a planted `agents` symlink must not be chmodded or written through.

        /home/agents-shared is group-writable, so any group member can create the
        `agents` entry pointing at a directory the hook's user owns. A path-based
        chmod follows it and widens the VICTIM's directory; the registration write
        would land inside it too. Both must be refused, and reported.
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        shared_root, registry_dir = _registry_pair(tmp_path)
        victim = tmp_path / "victim-dir"
        victim.mkdir()
        os.chmod(victim, 0o700)
        os.symlink(victim, registry_dir)

        status = mod.write_agent_registration("borges", registry_dir=str(registry_dir))

        assert stat.S_IMODE(os.stat(victim).st_mode) == 0o700, (
            "the victim directory's mode was widened through the planted symlink"
        )
        assert os.listdir(victim) == [], "a registration file was written through the symlink"
        assert status["registered"] is False
        assert status["dir_mode"] is None
        assert isinstance(status["dir_error"], str) and status["dir_error"]

    def test_dir_mode_mismatch_is_reported(self, tmp_path, monkeypatch):
        """#4682: the read-back mode is checked against 0o2775, not assumed.

        POSIX may silently drop the setgid bit for a caller that is not a member
        of the directory's group; that must report an error, not success.
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)

        def _noop_fchmod(fd, mode, *args, **kwargs):
            return None

        monkeypatch.setattr(mod.os, "fchmod", _noop_fchmod)

        old_umask = os.umask(0o022)
        try:
            status = mod.write_agent_registration(
                "borges", registry_dir=str(registry_dir)
            )
        finally:
            os.umask(old_umask)

        # makedirs' 0o2775 goes through the umask (and the kernel may drop
        # setgid); the no-op fchmod leaves whatever was created.
        actual = stat.S_IMODE(os.stat(registry_dir).st_mode)
        assert actual != 0o2775, "precondition: the no-op fchmod left the wrong mode"
        assert status["dir_mode"] == actual
        assert isinstance(status["dir_error"], str) and status["dir_error"]
        assert "expected" in status["dir_error"]
        assert status["registered"] is True

    def test_file_fchmod_failure_is_reported(self, tmp_path, monkeypatch):
        """#4682: the file's mode is set on the temp file before the atomic replace.

        If that fchmod fails, the temp file is removed and the write fails, so the
        call reports file_error and registers nothing (no post-replace chmod to
        fail independently).
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)

        real_fchmod, real_fstat = os.fchmod, os.fstat

        def _deny_file_fchmod(fd, mode, *args, **kwargs):
            if not stat.S_ISDIR(real_fstat(fd).st_mode):
                raise OSError("simulated: cannot set the registration file's mode")
            return real_fchmod(fd, mode, *args, **kwargs)

        monkeypatch.setattr(mod.os, "fchmod", _deny_file_fchmod)

        status = mod.write_agent_registration("borges", registry_dir=str(registry_dir))

        assert status["registered"] is False
        assert isinstance(status["file_error"], str) and status["file_error"]
        assert status["dir_error"] is None
        assert not (registry_dir / "borges.json").exists()
        assert list(registry_dir.glob("*.tmp")) == [], "a temp file was left behind"

    def test_file_mode_is_set_before_the_replace(self, tmp_path, monkeypatch):
        """#4682: the 0o644 is applied to the TEMP file, before os.replace.

        That is what removes the attacker window between the replace and a
        post-write chmod on a path in a group-writable directory.
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)
        _, registry_dir = _registry_pair(tmp_path)

        replace_modes = []
        real_replace, real_stat = os.replace, os.stat

        def _recording_replace(src, dst):
            replace_modes.append(stat.S_IMODE(real_stat(src).st_mode))
            return real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", _recording_replace)

        old_umask = os.umask(0o077)
        try:
            status = mod.write_agent_registration(
                "borges", registry_dir=str(registry_dir)
            )
        finally:
            os.umask(old_umask)

        assert status["registered"] is True
        assert replace_modes == [0o644], (
            f"mode at replace time was {[f'{m:o}' for m in replace_modes]}, not 644"
        )
        assert stat.S_IMODE(os.stat(registry_dir / "borges.json").st_mode) == 0o644
