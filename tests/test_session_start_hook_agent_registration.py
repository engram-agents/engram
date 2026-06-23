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
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = (
    _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
)


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

    def test_no_op_on_unwritable_registry_dir(self, tmp_path, monkeypatch):
        """An unwritable registry dir causes a silent no-op (no raise)."""
        if os.getuid() == 0:
            pytest.skip("root bypasses permission checks")

        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        shared_root = tmp_path / "agents-shared"
        shared_root.mkdir()
        registry_dir = shared_root / "agents"
        registry_dir.mkdir(mode=0o555)  # read + execute, no write

        try:
            # Must not raise
            mod.write_agent_registration("borges", registry_dir=str(registry_dir))
            # No file written
            assert not (registry_dir / "borges.json").exists()
        finally:
            # Restore perms so tmp_path cleanup succeeds
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
