"""Contract for config.json wrong-type guards at three more sites (#4229, siblings of #4203).

Written BEFORE the fix (contract-first). Each site's own convention is "an unreadable
config means defaults"; this extends it to "a wrong-type config means defaults".
Nothing is refused, exited or printed.

Contract:
  C1 (baton-migrate-to-coord-store._load_config): returns {} when the parsed JSON is
     not a dict, so the script behaves exactly as with no config (--forum-url, then
     $FORUM_URL, then the default; agent-name fall-through). A dict is unchanged.
  C2 (engram-deference-detector-stop._cooldown_minutes): returns 10.0 when the top
     level is not a dict or `deference_detector` is not a dict. A valid number is
     honoured exactly as before.
  C3 (context_tracker._load_drowsiness_thresholds): returns the mode's defaults when
     the top level is not a dict or `drowsiness_display` is not a dict. A per-mode
     block that is not a dict still gives defaults; a valid partial block merges per
     field.
  C4: no site prints anything new; a valid config behaves as before.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("isolate_hook_loads_module")

SRC = Path(__file__).resolve().parent.parent / "src" / "engram"
TOOLS = SRC / "tools"
HOOKS = SRC / "hooks" / "claude"

# Bad top-level values and bad block values.
BAD_TOP = [[1, 2], "x"]
BAD_TOP_IDS = ["list", "str"]
BAD_BLOCK = [None, "x", [1]]
BAD_BLOCK_IDS = ["none", "str", "list"]

_DEFAULT_URL = "http://localhost:5002"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(home: Path, raw) -> None:
    (home / "config.json").write_text(json.dumps(raw))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    monkeypatch.delenv("FORUM_URL", raising=False)
    return tmp_path


# --- C1: baton-migrate _load_config ---------------------------------------------------

@pytest.fixture
def mig(monkeypatch, home):
    monkeypatch.syspath_prepend(str(TOOLS))
    return _load("_mig_4229", TOOLS / "baton-migrate-to-coord-store.py")


@pytest.fixture
def run_migrate(mig, home, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mig, "ForumClient", lambda url: object())
    monkeypatch.setattr(mig, "migrate", lambda *a, **k: (0, 0, 0))

    def run(raw, *extra):
        _write(home, raw)
        monkeypatch.setattr(
            sys, "argv", ["m", "--projects-dir", str(tmp_path), "--dry-run", *extra]
        )
        mig.main()
        out, err = capsys.readouterr()
        assert err == ""
        for line in out.splitlines():
            if line.strip().startswith("forum-url:"):
                return line.split("forum-url:", 1)[1].strip()
        raise AssertionError("no forum-url line printed")

    return run


@pytest.mark.parametrize("bad", BAD_TOP, ids=BAD_TOP_IDS)
def test_migrate_non_dict_toplevel_loads_empty(mig, home, bad):
    _write(home, bad)
    assert mig._load_config() == {}


@pytest.mark.parametrize("bad", BAD_TOP, ids=BAD_TOP_IDS)
def test_migrate_non_dict_toplevel_main_uses_defaults(run_migrate, bad):
    assert run_migrate(bad) == _DEFAULT_URL


@pytest.mark.parametrize("bad", BAD_TOP, ids=BAD_TOP_IDS)
def test_migrate_non_dict_toplevel_env_then_flag_precedence(run_migrate, monkeypatch, bad):
    monkeypatch.setenv("FORUM_URL", "http://env.example:9")
    assert run_migrate(bad) == "http://env.example:9"
    assert run_migrate(bad, "--forum-url", "http://arg:2") == "http://arg:2"


def test_migrate_valid_config_honoured(mig, run_migrate, home):
    assert run_migrate({"forum": {"url": "http://a:1"}}) == "http://a:1"
    assert mig._load_config() == {"forum": {"url": "http://a:1"}}


# --- C2: deference-detector-stop _cooldown_minutes ------------------------------------

@pytest.fixture
def dd(monkeypatch, home):
    monkeypatch.syspath_prepend(str(HOOKS))
    mod = _load("_dd_4229", HOOKS / "engram-deference-detector-stop.py")
    monkeypatch.setattr(mod, "CONFIG_PATH", str(home / "config.json"))
    return mod


@pytest.mark.parametrize("bad", BAD_TOP, ids=BAD_TOP_IDS)
def test_cooldown_non_dict_toplevel_is_default(dd, home, bad):
    _write(home, bad)
    assert dd._cooldown_minutes() == 10.0


@pytest.mark.parametrize("bad", BAD_BLOCK, ids=BAD_BLOCK_IDS)
def test_cooldown_bad_block_is_default(dd, home, bad):
    _write(home, {"deference_detector": bad})
    assert dd._cooldown_minutes() == 10.0


def test_cooldown_valid_number_honoured(dd, home):
    _write(home, {"deference_detector": {"cooldown_minutes": 3}})
    assert dd._cooldown_minutes() == 3.0


# --- C3: context_tracker _load_drowsiness_thresholds ----------------------------------

@pytest.fixture
def ct(monkeypatch, home):
    monkeypatch.syspath_prepend(str(HOOKS))
    mod = _load("_ct_4229", HOOKS / "context_tracker.py")
    monkeypatch.setattr(mod, "ENGRAM_HOME", str(home))
    return mod


@pytest.mark.parametrize("mode", ["200k", "1m"])
@pytest.mark.parametrize("bad", BAD_TOP, ids=BAD_TOP_IDS)
def test_drowsiness_non_dict_toplevel_is_defaults(ct, home, mode, bad):
    _write(home, bad)
    assert ct._load_drowsiness_thresholds(mode) == ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE[mode]


@pytest.mark.parametrize("mode", ["200k", "1m"])
@pytest.mark.parametrize("bad", BAD_BLOCK, ids=BAD_BLOCK_IDS)
def test_drowsiness_bad_display_block_is_defaults(ct, home, mode, bad):
    _write(home, {"drowsiness_display": bad})
    assert ct._load_drowsiness_thresholds(mode) == ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE[mode]


@pytest.mark.parametrize("bad", BAD_BLOCK, ids=BAD_BLOCK_IDS)
def test_drowsiness_bad_mode_block_is_defaults(ct, home, bad):
    _write(home, {"drowsiness_display": {"1m": bad}})
    assert ct._load_drowsiness_thresholds("1m") == ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"]


def test_drowsiness_valid_partial_block_merges(ct, home):
    _write(home, {"drowsiness_display": {"1m": {"refreshed_below": 0.42}}})
    got = ct._load_drowsiness_thresholds("1m")
    assert got["refreshed_below"] == 0.42
    want = dict(ct.DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"], refreshed_below=0.42)
    assert got == want
