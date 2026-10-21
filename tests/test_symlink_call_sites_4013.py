"""#4013 round 3: the two `follow_symlinks=False` call sites that had no test.

Removing `follow_symlinks=False` at either site left 837 tests green.  Each cell plants a symlink at
the write path, drives the real function, and asserts the link was REPLACED by a regular file
holding the new content while the old target stayed byte-identical.
"""

import importlib.util
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import engram_walguard

_HOOK = (Path(__file__).resolve().parents[1] / "src" / "engram" / "hooks" / "claude"
         / "engram-session-start-hook.py")


def _load_hook(engram_home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    stub = MagicMock()
    stub.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook_4013_sites", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_write_degraded_marker_replaces_a_planted_symlink(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious, not a marker\n")
    before = victim.read_bytes()
    link = engram_walguard._marker_path(data_dir)
    link.symlink_to(victim)

    returned = engram_walguard.write_degraded_marker(data_dir, {"reason": "planted-link-test"})

    assert Path(returned) == link
    assert not link.is_symlink(), "the planted link was written through, not replaced"
    assert link.is_file()
    assert victim.read_bytes() == before
    assert json.loads(link.read_text())["reason"] == "planted-link-test"


def test_update_counterparts_replaces_a_planted_config_symlink(tmp_path, monkeypatch):
    hook = _load_hook(tmp_path / "home", monkeypatch)
    registry = tmp_path / "agents"
    registry.mkdir()
    (registry / "peer.json").write_text(json.dumps({
        "agent_name": "peer",
        "hostname": socket.gethostname(),
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }))
    victim = tmp_path / "real-config.json"
    victim.write_text(json.dumps({"counterparts": [], "keep": 1}))
    before = victim.read_bytes()
    link = tmp_path / "config.json"
    link.symlink_to(victim)

    hook.update_counterparts_from_registry("sol", str(link), str(registry))

    # The function fails soft: a regular file here proves it really wrote.
    assert not link.is_symlink(), "the planted link was written through, not replaced"
    assert link.is_file()
    assert victim.read_bytes() == before
    config = json.loads(link.read_text())
    assert config["counterparts"] == ["peer"]
    assert config["keep"] == 1
