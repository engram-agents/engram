"""Tests for peer_topology_block in engram-session-start-hook.py.

Part 2 of issue #1266: SessionStart injects a co-host / cross-host peer summary
into additionalContext at startup so agents walk in knowing which peers are
ia/baton-reachable vs forum-only.

Covers:
  - Returns "" when registry dir is absent (single-agent install).
  - Returns "" when registry is empty.
  - Returns "" when all peers are stale.
  - Returns "" when only self is in the registry.
  - Co-host entry appears for same-hostname peer.
  - Cross-host entry appears for different-hostname peer.
  - Both co-host and cross-host appear when both exist.
  - Stale entries (older than 24h) are excluded.
  - Malformed JSON in a registry file is skipped gracefully.
"""

import importlib.util
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = (
    _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
)
_OWN_HOSTNAME = socket.gethostname()

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(monkeypatch, engram_home: Path, registry_dir: Path):
    """Load the session-start hook as a module with test overrides."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    monkeypatch.setenv("AGENTS_SHARED_DIR", str(registry_dir.parent))

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


def _reg_json(name: str, hostname: str, age_seconds: float = 10) -> str:
    """Build a registration JSON string with the given age."""
    reg_at = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).isoformat()
    return json.dumps({
        "agent_name": name,
        "hostname": hostname,
        "pid": 12345,
        "registered_at": reg_at,
    })


class TestPeerTopologyBlock:
    def test_absent_registry_returns_empty(self, tmp_path, monkeypatch):
        """Returns "" when the agents/ registry dir does not exist."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        # Do NOT create registry_dir
        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        assert mod.peer_topology_block("sol") == ""

    def test_empty_registry_returns_empty(self, tmp_path, monkeypatch):
        """Returns "" when the registry exists but has no peer files."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        assert mod.peer_topology_block("sol") == ""

    def test_self_excluded(self, tmp_path, monkeypatch):
        """Agent's own registration file is excluded from the topology output."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "sol.json").write_text(_reg_json("sol", _OWN_HOSTNAME))

        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        assert mod.peer_topology_block("sol") == ""

    def test_empty_agent_name_returns_empty(self, tmp_path, monkeypatch):
        """Empty agent_name (config-read failure) returns '' without listing any peers.

        If _agent_name="" reaches peer_topology_block, the self-exclusion guard
        peer_name == agent_name never fires (no peer has name ""), so the current
        agent would appear in its own peer list.  The one-line guard at the top of
        peer_topology_block catches this before the registry is even read.
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "borges.json").write_text(_reg_json("borges", _OWN_HOSTNAME))

        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        assert mod.peer_topology_block("") == ""

    def test_cohost_peer_detected(self, tmp_path, monkeypatch):
        """Same-hostname peer appears as co-host (ia/baton)."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "borges.json").write_text(_reg_json("borges", _OWN_HOSTNAME))

        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        result = mod.peer_topology_block("sol")
        assert "co-host (ia/baton): borges" in result
        assert "cross-host" not in result

    def test_crosshost_peer_detected(self, tmp_path, monkeypatch):
        """Different-hostname peer appears as cross-host (forum)."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "luria.json").write_text(_reg_json("luria", "other-machine"))

        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        result = mod.peer_topology_block("sol")
        assert "cross-host (forum): luria" in result
        assert "co-host" not in result

    def test_mixed_topology(self, tmp_path, monkeypatch):
        """Co-host and cross-host peers both appear in the output."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "borges.json").write_text(_reg_json("borges", _OWN_HOSTNAME))
        (registry_dir / "luria.json").write_text(_reg_json("luria", "other-machine"))

        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        result = mod.peer_topology_block("sol")
        assert "co-host (ia/baton): borges" in result
        assert "cross-host (forum): luria" in result

    def test_stale_entries_excluded(self, tmp_path, monkeypatch):
        """Peers registered more than 24h ago are excluded as stale."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        # 25 hours old — just past the 24h stale threshold
        (registry_dir / "borges.json").write_text(
            _reg_json("borges", _OWN_HOSTNAME, age_seconds=25 * 3600)
        )
        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        assert mod.peer_topology_block("sol") == ""

    def test_fresh_entries_included(self, tmp_path, monkeypatch):
        """Peers registered less than 24h ago appear in the output."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "borges.json").write_text(
            _reg_json("borges", _OWN_HOSTNAME, age_seconds=10)
        )
        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        assert "co-host (ia/baton): borges" in mod.peer_topology_block("sol")

    def test_malformed_json_skipped(self, tmp_path, monkeypatch):
        """Malformed JSON in a registry file is silently skipped."""
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "bad.json").write_text("{not valid json")
        (registry_dir / "borges.json").write_text(_reg_json("borges", _OWN_HOSTNAME))

        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        result = mod.peer_topology_block("sol")
        assert "borges" in result  # good entry still appears

    @pytest.mark.parametrize("bad_body", ["[1, 2]", '"just a string"', "12345"])
    def test_non_dict_record_skipped_other_peers_survive(
        self, tmp_path, monkeypatch, bad_body
    ):
        """P1 (#4145): a registry file whose JSON is not an object is skipped.

        "aaa-bad.json" sorts BEFORE the good record, so an unguarded
        record.get() would raise and the broad except would hide every peer.
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "aaa-bad.json").write_text(bad_body)
        (registry_dir / "borges.json").write_text(_reg_json("borges", _OWN_HOSTNAME))

        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        assert "co-host (ia/baton): borges" in mod.peer_topology_block("sol")

    @pytest.mark.parametrize("bad_reg_at", [12345, ["x"], {"a": 1}, True])
    def test_non_string_registered_at_treated_as_missing(
        self, tmp_path, monkeypatch, bad_reg_at
    ):
        """P2 (#4145 sibling): a non-string registered_at is treated like a
        missing timestamp (fail-open, peer is live) and hides no other peer.
        """
        engram_home = tmp_path / ".engram"
        engram_home.mkdir()
        registry_dir = tmp_path / "agents-shared" / "agents"
        registry_dir.mkdir(parents=True)
        (registry_dir / "aaa.json").write_text(json.dumps({
            "agent_name": "aaa",
            "hostname": _OWN_HOSTNAME,
            "registered_at": bad_reg_at,
        }))
        (registry_dir / "luria.json").write_text(_reg_json("luria", "other-machine"))

        mod = _load_hook_module(monkeypatch, engram_home, registry_dir)
        result = mod.peer_topology_block("sol")
        assert "co-host (ia/baton): aaa" in result
        assert "cross-host (forum): luria" in result
