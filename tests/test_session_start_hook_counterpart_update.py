"""Tests for update_counterparts_from_registry in engram-session-start-hook.py.

Part 2 of issue #1266 (Option B): the function reads the agent registry and
updates config.json's counterparts list with co-host peer names (same hostname
as the current agent), excluding stale and malformed entries.

Covers:
- Co-host peers set as counterparts.
- Cross-host peers excluded.
- Stale registry entries excluded.
- Self excluded.
- Empty registry yields counterparts=[].
- Malformed JSON files skipped without crash.
"""

import importlib.util
import json
import os
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = (
    _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
)

OWN_HOSTNAME = socket.gethostname()
OTHER_HOSTNAME = "other-host-xyz"


# ---------------------------------------------------------------------------
# Module loader helper (mirrors test_session_start_hook_agent_registration.py)
# ---------------------------------------------------------------------------

def _load_hook_module(monkeypatch, engram_home: Path):
    """Load the session-start hook module, stubbing context_tracker."""
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
# Registry entry builder helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_hours_ago(hours: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_registry_entry(
    registry_dir: Path,
    agent_name: str,
    hostname: str,
    registered_at: str | None = None,
) -> None:
    """Write a well-formed registry JSON file for an agent."""
    record = {
        "agent_name": agent_name,
        "hostname": hostname,
        "pid": 12345,
        "registered_at": registered_at or _now_iso(),
    }
    (registry_dir / f"{agent_name}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def _make_config(engram_home: Path, initial_counterparts: list | None = None) -> Path:
    """Write a minimal config.json and return its path."""
    config = {
        "agent_name": "sol",
        "counterparts": initial_counterparts if initial_counterparts is not None else [],
    }
    config_path = engram_home / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUpdateCounterpartsFromRegistry:

    def test_cohost_peers_set_as_counterparts(self, tmp_path, monkeypatch):
        """Registry has 2 co-host agents → both appear in counterparts."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)

        config_path = _make_config(engram_home)

        _write_registry_entry(registry_dir, "borges", OWN_HOSTNAME)
        _write_registry_entry(registry_dir, "ariadne", OWN_HOSTNAME)

        mod.update_counterparts_from_registry(
            "sol", str(config_path), str(registry_dir)
        )

        result = json.loads(config_path.read_text())
        assert sorted(result["counterparts"]) == ["ariadne", "borges"]

    def test_crosshost_peers_excluded(self, tmp_path, monkeypatch):
        """An agent with a different hostname is not added to counterparts."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)

        config_path = _make_config(engram_home)

        _write_registry_entry(registry_dir, "borges", OWN_HOSTNAME)
        _write_registry_entry(registry_dir, "remote-agent", OTHER_HOSTNAME)

        mod.update_counterparts_from_registry(
            "sol", str(config_path), str(registry_dir)
        )

        result = json.loads(config_path.read_text())
        assert result["counterparts"] == ["borges"]
        assert "remote-agent" not in result["counterparts"]

    def test_stale_peers_excluded(self, tmp_path, monkeypatch):
        """A registry entry older than 24 hours is not included in counterparts."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)

        config_path = _make_config(engram_home)

        # stale: 25 hours ago (beyond 24h threshold)
        _write_registry_entry(
            registry_dir, "old-agent", OWN_HOSTNAME,
            registered_at=_iso_hours_ago(25)
        )
        # fresh: 1 hour ago
        _write_registry_entry(
            registry_dir, "fresh-agent", OWN_HOSTNAME,
            registered_at=_iso_hours_ago(1)
        )

        mod.update_counterparts_from_registry(
            "sol", str(config_path), str(registry_dir)
        )

        result = json.loads(config_path.read_text())
        assert "old-agent" not in result["counterparts"]
        assert "fresh-agent" in result["counterparts"]

    def test_self_excluded(self, tmp_path, monkeypatch):
        """The agent's own registration file is not added to counterparts."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)

        config_path = _make_config(engram_home)

        # Self-registration (same name as agent_name argument)
        _write_registry_entry(registry_dir, "sol", OWN_HOSTNAME)
        _write_registry_entry(registry_dir, "borges", OWN_HOSTNAME)

        mod.update_counterparts_from_registry(
            "sol", str(config_path), str(registry_dir)
        )

        result = json.loads(config_path.read_text())
        assert "sol" not in result["counterparts"]
        assert "borges" in result["counterparts"]

    def test_empty_registry_clears_counterparts(self, tmp_path, monkeypatch):
        """No valid peers in registry → counterparts list is set to []."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)

        # Start with a non-empty counterparts list
        config_path = _make_config(engram_home, initial_counterparts=["old-peer"])

        # Registry dir is empty (no json files)
        mod.update_counterparts_from_registry(
            "sol", str(config_path), str(registry_dir)
        )

        result = json.loads(config_path.read_text())
        assert result["counterparts"] == []

    def test_malformed_json_skipped(self, tmp_path, monkeypatch):
        """A malformed registry file is skipped without crashing."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)

        config_path = _make_config(engram_home)

        # Write a malformed file (not valid JSON)
        (registry_dir / "broken-agent.json").write_text(
            "{invalid json {{", encoding="utf-8"
        )
        # Write a valid co-host peer
        _write_registry_entry(registry_dir, "borges", OWN_HOSTNAME)

        # Must not raise
        mod.update_counterparts_from_registry(
            "sol", str(config_path), str(registry_dir)
        )

        result = json.loads(config_path.read_text())
        # Valid peer is present; malformed entry was skipped
        assert "borges" in result["counterparts"]

    def test_missing_registry_dir_is_noop(self, tmp_path, monkeypatch):
        """Non-existent registry dir → no-op, no exception, config unchanged."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        nonexistent_registry = tmp_path / "nonexistent" / "agents"
        config_path = _make_config(
            engram_home, initial_counterparts=["existing-peer"]
        )

        # Must not raise
        mod.update_counterparts_from_registry(
            "sol", str(config_path), str(nonexistent_registry)
        )

        # Config unchanged because the function returned early
        result = json.loads(config_path.read_text())
        assert result["counterparts"] == ["existing-peer"]

    def test_counterparts_are_sorted(self, tmp_path, monkeypatch):
        """counterparts list is sorted alphabetically."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        mod = _load_hook_module(monkeypatch, engram_home)

        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)

        config_path = _make_config(engram_home)

        # Write in reverse-alphabetical order to test sort
        _write_registry_entry(registry_dir, "zephyr", OWN_HOSTNAME)
        _write_registry_entry(registry_dir, "ariadne", OWN_HOSTNAME)
        _write_registry_entry(registry_dir, "mira", OWN_HOSTNAME)

        mod.update_counterparts_from_registry(
            "sol", str(config_path), str(registry_dir)
        )

        result = json.loads(config_path.read_text())
        assert result["counterparts"] == ["ariadne", "mira", "zephyr"]
