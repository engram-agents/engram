"""#4013 colleague finding: the SessionStart registry write must REPLACE a planted symlink.

`write_agent_registration` writes into a shared directory created group-writable (0o2775, no
sticky bit).  A co-tenant could plant `<agent>.json -> <any file the victim can write>`; if the
write followed the link, the victim's next session start would overwrite that file.  This drives
the real function (not the helper) and asserts the link is swapped for a regular file, the old
target is untouched, and the record landed.  The function fails soft, so the new file existing is
what proves it really ran.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

_HOOK = (Path(__file__).resolve().parents[1] / "src" / "engram" / "hooks" / "claude"
         / "engram-session-start-hook.py")

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook(engram_home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    stub = MagicMock()
    stub.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook_4013", _HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_write_replaces_a_planted_symlink_and_spares_its_target(tmp_path, monkeypatch):
    hook = _load_hook(tmp_path / "home", monkeypatch)
    shared = tmp_path / "shared"
    shared.mkdir()
    registry = shared / "agents"
    registry.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious, not a registration\n")
    os.chmod(victim, 0o600)
    before = victim.read_bytes()
    link = registry / "sol.json"
    link.symlink_to(victim)

    hook.write_agent_registration("sol", str(registry))

    assert not link.is_symlink(), "the planted link was written through, not replaced"
    assert link.is_file()
    assert victim.read_bytes() == before
    assert os.stat(victim).st_mode & 0o777 == 0o600
    # New-file behaviour: the victim's 0600 must not leak onto the registry file.
    # #4026 asserts the registry file's mode explicitly (0o644, umask-defeating).
    # Deliberately NOT group-writable: the file feeds peer topology/liveness, so
    # group write would let any co-tenant impersonate another agent.
    mode = os.stat(link).st_mode & 0o777
    assert mode == 0o644
    assert mode != 0o600, "the victim's 0600 leaked onto the registry file"
    assert mode & 0o020 == 0, "the registration file is group-writable"
    record = json.loads(link.read_text())
    assert record["agent_name"] == "sol"
    assert sorted(os.listdir(registry)) == ["sol.json"]
