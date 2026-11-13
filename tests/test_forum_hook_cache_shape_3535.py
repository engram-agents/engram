"""#3535: the forum prompt hook's `_load_cache` must treat a malformed cache as no cache.

`_load_cache` called `.get("fetched_at", 0)` on whatever `json.loads` returned and caught
only OSError / JSONDecodeError / KeyError, so a cache file holding `[1,2]` or `null` raised
AttributeError (and a non-numeric `fetched_at` raised TypeError): the hook exited rc=1 with a
traceback and surfaced no forum signal at all.

Contract:
  C1. A cache whose JSON is not an object (list, null, number, string) is an invalid cache:
      `_load_cache()` returns None, no exception.
  C2. A valid object cache behaves as before (fresh -> the dict, expired -> None).
  C3. A `fetched_at` that is not a finite number is also an invalid cache, not a crash.
  End to end: the real hook, run as a subprocess against a list-valued cache, exits 0.

In-process cells load the hook as a module (same pattern as
tests/test_forum_hook_unknown_state_3521.py).
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
HOOK_PATH = HOOK_DIR / "engram-forum-prompt-hook.py"
CACHE_NAME = "forum-hook-cache.json"

# in-process hook loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook(name="forum_hook_3535"):
    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    spec = importlib.util.spec_from_file_location(name, str(HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def hook(tmp_path, monkeypatch):
    mod = _load_hook()
    monkeypatch.setattr(mod, "HOOK_CACHE_PATH", str(tmp_path / CACHE_NAME))
    return mod


def _write(tmp_path, raw: str):
    (tmp_path / CACHE_NAME).write_text(raw)


# ---- C1: not an object ------------------------------------------------------

@pytest.mark.parametrize("raw", ["[1,2]", "null", "7", '"x"', "true", "[]"],
                         ids=["list", "null", "number", "string", "bool", "empty-list"])
def test_c1_non_object_cache_is_no_cache(hook, tmp_path, raw):
    _write(tmp_path, raw)
    assert hook._load_cache() is None


# ---- C2: a valid object cache is unchanged ---------------------------------

def test_c2_fresh_object_cache_is_returned(hook, tmp_path):
    payload = {"fetched_at": time.time(), "threads": [{"id": "1"}], "mentions": []}
    _write(tmp_path, json.dumps(payload))
    assert hook._load_cache() == payload


def test_c2_expired_object_cache_is_none(hook, tmp_path):
    payload = {"fetched_at": time.time() - 3600, "threads": []}
    _write(tmp_path, json.dumps(payload))
    assert hook._load_cache() is None


def test_c2_absent_and_invalid_json_stay_none(hook, tmp_path):
    assert hook._load_cache() is None            # no file
    _write(tmp_path, "{not json")
    assert hook._load_cache() is None


# ---- C3: fetched_at not a finite number ------------------------------------

@pytest.mark.parametrize("fetched", ['"yesterday"', "null", "[1]", '{"a":1}', "true", "NaN", "Infinity"],
                         ids=["string", "null", "list", "object", "bool", "nan", "inf"])
def test_c3_non_numeric_fetched_at_is_no_cache(hook, tmp_path, fetched):
    _write(tmp_path, '{"fetched_at": %s, "threads": []}' % fetched)
    assert hook._load_cache() is None


def test_c3_missing_fetched_at_stays_expired(hook, tmp_path):
    # absent key keeps its old meaning: treated as epoch 0, i.e. long expired
    _write(tmp_path, '{"threads": []}')
    assert hook._load_cache() is None


# ---- end to end -------------------------------------------------------------

def _dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("raw", ["[1,2]", "null", '{"fetched_at": "x", "threads": []}'],
                         ids=["list", "null", "string-fetched-at"])
def test_e2e_hook_survives_a_malformed_cache(tmp_path, raw):
    """The real hook against a dead local URL (never the live forum on :5002) and a malformed
    cache: rc 0, no traceback."""
    home = tmp_path / "engram_home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"agent_name": "testbot", "forum": {"url": _dead_url()}}))
    (home / CACHE_NAME).write_text(raw)
    env = {k: v for k, v in os.environ.items() if k not in ("FORUM_URL", "CLAUDE_PLUGIN_ROOT")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{PROJECT_ROOT / 'src' / 'engram'}:{PROJECT_ROOT / 'src'}"
    p = subprocess.run([sys.executable, str(HOOK_PATH)], input="{}", capture_output=True,
                       text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    assert "Traceback" not in p.stderr
