"""#4211: the session-start registry loop (peer_topology_block).

Contract:
  C1: a registry file whose JSON parse raises RecursionError is skipped like
      any other unparseable file; the good peers still show.
  C2: a naive ``registered_at`` is read as UTC (naive old -> stale, naive
      recent -> live).

(#4255 slice 2 retired the second loop, update_counterparts_from_registry, and
its cells; what is left is the peer_topology_block half of each contract.)
"""

import importlib.util
import json
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
_OWN = socket.gethostname()

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load(monkeypatch, tmp_path):
    engram_home = tmp_path / ".engram"
    engram_home.mkdir()
    registry_dir = tmp_path / "agents-shared" / "agents"
    registry_dir.mkdir(parents=True)
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    monkeypatch.setenv("AGENTS_SHARED_DIR", str(registry_dir.parent))
    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub_ct)
    sys.modules.pop("engram_session_start_hook", None)
    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    config_path = engram_home / "config.json"
    config_path.write_text(json.dumps({"agent_name": "sol"}), encoding="utf-8")
    return mod, registry_dir, config_path


def _write(registry_dir, name, registered_at):
    (registry_dir / f"{name}.json").write_text(
        json.dumps(
            {"agent_name": name, "hostname": _OWN, "pid": 1, "registered_at": registered_at}
        ),
        encoding="utf-8",
    )


def _naive_utc(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).replace(tzinfo=None).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _aware_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_deep(registry_dir):
    # Built here, not a committed fixture: json.loads raises RecursionError.
    (registry_dir / "deep.json").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")


def test_recursion_error_file_topology_keeps_good_peer(tmp_path, monkeypatch):
    mod, reg, _cfg = _load(monkeypatch, tmp_path)
    _write_deep(reg)
    _write(reg, "goodpeer", _aware_now())
    assert "goodpeer" in mod.peer_topology_block("sol")


def test_naive_old_timestamp_stale(tmp_path, monkeypatch):
    mod, reg, cfg = _load(monkeypatch, tmp_path)
    _write(reg, "naiveold", _naive_utc(timedelta(days=30)))
    _write(reg, "goodpeer", _aware_now())
    topo = mod.peer_topology_block("sol")
    assert "naiveold" not in topo and "goodpeer" in topo


def test_naive_recent_timestamp_live(tmp_path, monkeypatch):
    mod, reg, cfg = _load(monkeypatch, tmp_path)
    _write(reg, "naivenew", _naive_utc(timedelta(seconds=5)))
    assert "naivenew" in mod.peer_topology_block("sol")


def test_naive_20h_is_live_read_as_utc_not_local(tmp_path, monkeypatch):
    """Discriminates naive-as-UTC from naive-as-local: 20 h is live as UTC, ~29 h (stale) as UTC+9 local.

    Cell authored by the merge seat (Borges) on #4224: the 5 s / 30 d cells above
    pin both ends, but no offset under 14 h moves either across the
    24 h threshold, so a read-as-local mutant stayed green under three TZs.
    """
    import time
    monkeypatch.setenv("TZ", "Asia/Tokyo"); time.tzset()
    try:
        mod, reg, cfg = _load(monkeypatch, tmp_path)
        _write(reg, "naive20h", _naive_utc(timedelta(hours=20)))
        assert "naive20h" in mod.peer_topology_block("sol")
    finally:
        # undo() restores TZ exactly as it was (set or unset); tzset() applies it,
        # so later tests in this process don't run under Asia/Tokyo (Borges, #4224).
        monkeypatch.undo(); time.tzset()
