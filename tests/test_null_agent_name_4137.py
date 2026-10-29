"""alpha #4137: a null (or non-string) `agent_name` / `hostname` must be treated like "".

`config.get("agent_name", "")` returns None for `"agent_name": null` (the default applies
only to an ABSENT key), so `.strip()` raised AttributeError, and `str(None)` made the
literal name "None". At every site the missing / null / non-string value must fall to the
site's EXISTING empty-name fallback; a string keeps its behaviour.

Modules are loaded BY PATH under unique names (never `import forum`, which collides with
the src/forum package; see tests/test_forum_db_hostname.py).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "src" / "engram" / "tools"
HOOKS = REPO / "src" / "engram" / "hooks" / "claude"

BAD = [None, 42, ["x"], {"a": 1}]
BAD_IDS = ["null", "int", "list", "dict"]


def _load(unique_name: str, path: Path):
    if path.suffix:
        spec = importlib.util.spec_from_file_location(unique_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    # Extensionless tool (e.g. engram-snapshot): spec_from_file_location returns
    # None for it, so go through SourceFileLoader explicitly.
    loader = importlib.machinery.SourceFileLoader(unique_name, str(path))
    spec = importlib.util.spec_from_loader(unique_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def env_user(monkeypatch):
    monkeypatch.setenv("USER", "agent-envuser")
    monkeypatch.delenv("LOGNAME", raising=False)


# --- the "env chain" resolvers -------------------------------------------------------

RESOLVERS = [
    ("forum", TOOLS / "forum.py", "_get_agent_name"),
    ("ia", TOOLS / "ia.py", "_get_agent_name"),
    ("baton", TOOLS / "baton.py", "_get_agent_name"),
    ("engram_snapshot", TOOLS / "engram-snapshot", "_get_agent_name"),
    ("prompthooklib", HOOKS / "_prompthooklib.py", "get_agent_name"),
]
RESOLVER_IDS = [r[0] for r in RESOLVERS]


@pytest.mark.parametrize("bad", BAD, ids=BAD_IDS)
@pytest.mark.parametrize("name,path,fn", RESOLVERS, ids=RESOLVER_IDS)
def test_resolver_bad_agent_name_falls_to_env_chain(name, path, fn, bad, env_user):
    f = getattr(_load(f"_{name}_4137", path), fn)
    assert f({"agent_name": bad}) == "envuser"


@pytest.mark.parametrize("name,path,fn", RESOLVERS, ids=RESOLVER_IDS)
def test_resolver_string_name_unchanged(name, path, fn, env_user):
    f = getattr(_load(f"_{name}_4137", path), fn)
    assert f({"agent_name": "  zed  "}) == "zed"
    assert f({"agent_name": ""}) == "envuser"
    assert f({}) == "envuser"


@pytest.mark.parametrize("bad", BAD, ids=BAD_IDS)
def test_migrate_bad_agent_name_falls_to_env_chain(bad, env_user):
    mod = _load("_migrate_4137", TOOLS / "baton-migrate-to-coord-store.py")
    assert mod._get_agent_name({"agent_name": bad}) == "envuser"
    assert mod._get_agent_name({"agent_name": " zed "}) == "zed"


# --- supervision_probe: str(None) -> "None" ------------------------------------------

@pytest.mark.parametrize("bad", BAD, ids=BAD_IDS)
def test_supervision_probe_never_names_the_seat_none(bad, tmp_path, env_user):
    sp = _load("_sp_4137", TOOLS / "supervision_probe.py")
    (tmp_path / "config.json").write_text(json.dumps({"agent_name": bad}))
    result = sp.seat_name(tmp_path)
    assert result == "envuser"
    assert result not in ("None", "42")


def test_supervision_probe_string_name_unchanged(tmp_path):
    sp = _load("_sp_4137", TOOLS / "supervision_probe.py")
    (tmp_path / "config.json").write_text('{"agent_name": " zed "}')
    assert sp.seat_name(tmp_path) == "zed"


# --- session-start hook: peer records ------------------------------------------------

HOST = socket.gethostname()


def _load_hook(monkeypatch, home: Path, shared: Path):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setenv("AGENTS_SHARED_DIR", str(shared))
    stub = MagicMock()
    stub.write_baseline = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "context_tracker", stub)
    return _load("_hook_4137", HOOKS / "engram-session-start-hook.py")


def _registry(tmp_path):
    shared = tmp_path / "shared"
    reg = shared / "agents"
    reg.mkdir(parents=True)
    return shared, reg


def _fresh():
    return datetime.now(timezone.utc).isoformat()


def _write_peers(reg: Path, bad_rec: dict):
    (reg / "0-bad.json").write_text(json.dumps({**bad_rec, "registered_at": _fresh()}))
    (reg / "1-good.json").write_text(
        json.dumps({"agent_name": "goodpeer", "hostname": HOST, "registered_at": _fresh()})
    )


BAD_RECORDS = [
    {"agent_name": None, "hostname": HOST},
    {"agent_name": 42, "hostname": HOST},
    {"agent_name": "ghost", "hostname": None},
    {"agent_name": "ghost", "hostname": 42},
]
BAD_RECORD_IDS = ["name-null", "name-int", "host-null", "host-int"]


@pytest.mark.parametrize("rec", BAD_RECORDS, ids=BAD_RECORD_IDS)
def test_peer_topology_bad_record_is_skipped_not_fatal(rec, tmp_path, monkeypatch):
    """The bad record sorts FIRST; the good peer after it must still be found. Pre-fix the
    AttributeError was swallowed by the function's broad except, hiding every later peer."""
    shared, reg = _registry(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    hook = _load_hook(monkeypatch, home, shared)
    monkeypatch.setattr(hook, "AGENT_REGISTRY_DIR", str(reg))
    _write_peers(reg, rec)
    out = hook.peer_topology_block("me")
    assert "goodpeer" in out
    assert "ghost" not in out and "None" not in out


@pytest.mark.parametrize("bad", [None, 42], ids=["null", "int"])
def test_session_start_registration_bad_name_registers_nothing(bad, tmp_path):
    """Subprocess run of the hook. NOTE: the registration block sits inside a try/except, so
    pre-fix the AttributeError was swallowed with the same visible result; this cell pins the
    outcome (clean run, nobody registered), it does not discriminate."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agent_name": bad}))
    shared = tmp_path / "shared"
    (shared / "agents").mkdir(parents=True)
    env = {**os.environ, "ENGRAM_HOME": str(home), "AGENTS_SHARED_DIR": str(shared)}
    r = subprocess.run(
        [sys.executable, str(HOOKS / "engram-session-start-hook.py")],
        input=json.dumps({"source": "startup", "session_id": "t4137"}),
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    assert "Traceback" not in r.stderr, r.stderr[-800:]
    assert list((shared / "agents").glob("*.json")) == []
