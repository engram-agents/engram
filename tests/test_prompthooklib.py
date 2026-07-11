"""Tests for hooks/claude/_prompthooklib.py (gh#1680 slice 1 -- shared
UserPromptSubmit prologue helpers).

Covers load_config(), get_agent_name(), emit_context(), and
bootstrap_tools_dir() as pure/unit-testable functions, isolated from any
real hook via a fresh module load + synthetic ENGRAM_HOME per test (mirrors
tests/test_hooklib.py's structure for the sibling _hooklib module).
"""

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "_prompthooklib.py"
)


def _load_module():
    """Load _prompthooklib as a fresh module instance, isolated per test."""
    spec = importlib.util.spec_from_file_location(
        "_prompthooklib_test_instance", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_sys_path():
    """Snapshot/restore sys.path so a test's insertion doesn't leak."""
    before = list(sys.path)
    yield
    sys.path[:] = before


# ---------------------------------------------------------------------------
# load_config()
# ---------------------------------------------------------------------------


def test_load_config_reads_existing_file(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"agent_name": "borges"}))
    mod = _load_module()
    result = mod.load_config(str(tmp_path))
    assert result == {"agent_name": "borges"}


def test_load_config_missing_file_returns_empty_dict(tmp_path):
    mod = _load_module()
    result = mod.load_config(str(tmp_path / "does-not-exist"))
    assert result == {}


def test_load_config_malformed_json_returns_empty_dict(tmp_path):
    (tmp_path / "config.json").write_text("{not valid json")
    mod = _load_module()
    result = mod.load_config(str(tmp_path))
    assert result == {}


def test_load_config_default_engram_home_env(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"agent_name": "ariadne"}))
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    mod = _load_module()
    result = mod.load_config()
    assert result == {"agent_name": "ariadne"}


# ---------------------------------------------------------------------------
# get_agent_name()
# ---------------------------------------------------------------------------


def test_get_agent_name_config_wins(monkeypatch):
    monkeypatch.setenv("USER", "agent-mallory")
    mod = _load_module()
    result = mod.get_agent_name({"agent_name": "borges"})
    assert result == "borges"


def test_get_agent_name_user_env_fallback(monkeypatch):
    monkeypatch.setenv("USER", "agent-borges")
    monkeypatch.delenv("LOGNAME", raising=False)
    mod = _load_module()
    result = mod.get_agent_name({})
    assert result == "borges"


def test_get_agent_name_logname_when_user_empty(monkeypatch):
    monkeypatch.setenv("USER", "")
    monkeypatch.setenv("LOGNAME", "agent-borges")
    mod = _load_module()
    result = mod.get_agent_name({})
    assert result == "borges"


def test_get_agent_name_uid_fallback(monkeypatch):
    monkeypatch.setenv("USER", "")
    monkeypatch.setenv("LOGNAME", "")
    mod = _load_module()
    mock_pw = type("PW", (), {"pw_name": "agent-borges"})()
    with patch.object(mod.pwd, "getpwuid", return_value=mock_pw):
        result = mod.get_agent_name({})
    assert result == "borges"


def test_get_agent_name_all_paths_fail(monkeypatch):
    monkeypatch.setenv("USER", "")
    monkeypatch.setenv("LOGNAME", "")
    mod = _load_module()
    with patch.object(mod.pwd, "getpwuid", side_effect=KeyError("no uid")):
        result = mod.get_agent_name({})
    assert result == ""


def test_get_agent_name_raw_username_no_prefix(monkeypatch):
    monkeypatch.setenv("USER", "localdev")
    monkeypatch.delenv("LOGNAME", raising=False)
    mod = _load_module()
    result = mod.get_agent_name({})
    assert result == "localdev"


def test_get_agent_name_none_config_loads_via_engram_home(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"agent_name": "luria"}))
    mod = _load_module()
    result = mod.get_agent_name(None, str(tmp_path))
    assert result == "luria"


# ---------------------------------------------------------------------------
# emit_context()
# ---------------------------------------------------------------------------


def test_emit_context_envelope_shape():
    mod = _load_module()
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.emit_context("hello world")
    envelope = json.loads(buf.getvalue())
    assert envelope == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "hello world",
        }
    }


# ---------------------------------------------------------------------------
# bootstrap_tools_dir()
# ---------------------------------------------------------------------------


def test_bootstrap_tools_dir_delegates_to_hooklib(monkeypatch):
    """bootstrap_tools_dir finds the real repo's tools/ via _hooklib's
    walk-parents fallback, same as a direct _hooklib.resolve_tools_dir call
    would (this module lives in the same directory as _hooklib.py, so the
    parent chain is identical)."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    # Ensure _hooklib is importable: add this module's own directory to
    # sys.path, mirroring what every hook's bootstrap already does before
    # calling bootstrap_tools_dir.
    sys.path.insert(0, str(MODULE_PATH.parent))
    mod = _load_module()

    result = mod.bootstrap_tools_dir("forum_api.py")

    assert result is not None
    assert (result / "forum_api.py").exists()


def test_bootstrap_tools_dir_returns_none_on_missing_marker(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    sys.path.insert(0, str(MODULE_PATH.parent))
    mod = _load_module()

    result = mod.bootstrap_tools_dir("definitely-not-a-real-marker-file.xyz")

    assert result is None


def test_bootstrap_tools_dir_degrades_to_none_when_hooklib_unimportable(monkeypatch):
    """If _hooklib somehow can't be imported (e.g. this module's own
    directory isn't on sys.path), bootstrap_tools_dir degrades to None
    rather than raising -- the 'never crash the prompt' discipline."""
    # Other test modules in this suite (test_baton_hook.py etc.) load the
    # real hooks via spec_from_file_location, which triggers each hook's own
    # `sys.path.insert(0, _this_dir)` bootstrap -- that insertion is never
    # cleaned up by those tests, so hooks/claude/ can already be sitting on
    # sys.path when this test runs as part of the full suite. Explicitly
    # strip it (the _clean_sys_path autouse fixture restores sys.path after
    # this test regardless) so "this module's own directory isn't on
    # sys.path" is actually true for this test, not just when run in
    # isolation.
    hooks_dir = str(MODULE_PATH.parent)
    sys.path[:] = [p for p in sys.path if p != hooks_dir]
    # Scrub any stale _hooklib from a prior test's sys.modules cache.
    monkeypatch.delitem(sys.modules, "_hooklib", raising=False)
    mod = _load_module()

    result = mod.bootstrap_tools_dir("forum_api.py")

    assert result is None


# ---------------------------------------------------------------------------
# Flattened deployed-topology trap (mandatory per spec)
# ---------------------------------------------------------------------------


def test_prompthooklib_importable_in_flattened_deployed_topology(tmp_path, monkeypatch):
    """The plugin build FLATTENS hooks/claude/* -> hooks/* -- one level
    shallower than source. Recreate that layout (_prompthooklib.py and
    _hooklib.py as siblings in a bare hooks/ dir, with a further-nested
    tools/) with CLAUDE_PLUGIN_ROOT UNSET, and confirm _prompthooklib's own
    exports still work correctly when imported at that nesting depth: (a)
    bootstrap_tools_dir resolves tools/, (b) load_config loads, (c)
    get_agent_name resolves, (d) emit_context prints the expected envelope.

    This is the module-level counterpart to the per-hook deployed-layout
    tests in test_baton_hook.py / test_inter_agent_hook.py / test_forum_hook.py
    -- those confirm each HOOK still works after importing _prompthooklib at
    the flattened depth; this one confirms _prompthooklib ITSELF has no
    hidden assumption about the source-tree layout.
    """
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    root = tmp_path / "engram"
    (root / "hooks").mkdir(parents=True)
    (root / "tools").mkdir()

    hooklib_src = (
        Path(__file__).parent.parent
        / "src" / "engram" / "hooks" / "claude" / "_hooklib.py"
    )
    (root / "hooks" / "_hooklib.py").write_text(hooklib_src.read_text())
    (root / "hooks" / "_prompthooklib.py").write_text(MODULE_PATH.read_text())
    (root / "tools" / "forum_api.py").write_text("# marker only\n")

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agent_name": "luria"}))

    import subprocess

    probe_script = (
        "import sys, io, json, contextlib\n"
        f"sys.path.insert(0, r'{root}/hooks')\n"
        "import _prompthooklib as m\n"
        "print('bootstrap_ok' if m.bootstrap_tools_dir('forum_api.py') is not None else 'bootstrap_broken')\n"
        f"cfg = m.load_config(r'{home}')\n"
        "print('config_ok' if cfg.get('agent_name') == 'luria' else f'config_broken:{cfg!r}')\n"
        "name = m.get_agent_name(cfg)\n"
        "print('name_ok' if name == 'luria' else f'name_broken:{name!r}')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    m.emit_context('hello world')\n"
        "envelope = json.loads(buf.getvalue())\n"
        "expected = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'hello world'}}\n"
        "print('emit_ok' if envelope == expected else f'emit_broken:{envelope!r}')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe_script],
        capture_output=True, text=True,
        env={k: v for k, v in __import__("os").environ.items() if k != "CLAUDE_PLUGIN_ROOT"},
    )
    assert "bootstrap_ok" in r.stdout, (
        f"_prompthooklib.bootstrap_tools_dir failed in flattened topology "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "config_ok" in r.stdout, (
        f"_prompthooklib.load_config failed in flattened topology "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "name_ok" in r.stdout, (
        f"_prompthooklib.get_agent_name failed in flattened topology "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "emit_ok" in r.stdout, (
        f"_prompthooklib.emit_context envelope diverged in flattened topology "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
