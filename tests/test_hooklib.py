"""Tests for hooks/claude/_hooklib.py (gh#1657 -- shared prompt-hook helper).

Covers resolve_tools_dir()'s three resolution paths (CLAUDE_PLUGIN_ROOT set,
walk-parents fallback, marker-not-found -> None) and its sys.path side
effect, isolated from any real hook or real tools/ directory via a fresh
module load + a synthetic directory tree per test.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "_hooklib.py"
)


def _load_module():
    """Load _hooklib as a fresh module instance, isolated per test."""
    spec = importlib.util.spec_from_file_location("_hooklib_test_instance", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_sys_path():
    """Snapshot/restore sys.path so a test's insertion doesn't leak."""
    before = list(sys.path)
    yield
    sys.path[:] = before


def test_resolves_via_plugin_root_when_set(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin"
    (plugin_root / "tools").mkdir(parents=True)
    (plugin_root / "tools" / "marker.py").write_text("")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    mod = _load_module()
    result = mod.resolve_tools_dir("marker.py")

    assert result == plugin_root / "tools"
    assert str(plugin_root / "tools") in sys.path


def test_walks_parents_when_plugin_root_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    mod = _load_module()

    # _hooklib.py's own real location provides a genuine parent chain;
    # the repo's real tools/ contains forum_api.py, so that's a reliable,
    # always-true marker to probe the walk-parents path against without
    # needing to fake a whole tree.
    result = mod.resolve_tools_dir("forum_api.py")

    assert result is not None
    assert (result / "forum_api.py").exists()
    assert str(result) in sys.path


def test_returns_none_when_marker_not_found_anywhere(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    mod = _load_module()

    result = mod.resolve_tools_dir("definitely-not-a-real-marker-file.xyz")

    assert result is None


def test_plugin_root_takes_precedence_over_walk_parents(tmp_path, monkeypatch):
    """When CLAUDE_PLUGIN_ROOT is set but its tools/ lacks the marker, the
    walk-parents fallback still runs (plugin-root candidate is tried FIRST,
    not exclusively) -- confirms the candidate list is ordered, not either/or."""
    plugin_root = tmp_path / "plugin"
    (plugin_root / "tools").mkdir(parents=True)  # no marker file here
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    mod = _load_module()
    result = mod.resolve_tools_dir("forum_api.py")

    # Falls through to the real repo's tools/ via walk-parents, not None.
    assert result is not None
    assert result != plugin_root / "tools"


def test_does_not_duplicate_sys_path_entry_on_repeat_call(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    mod = _load_module()

    first = mod.resolve_tools_dir("forum_api.py")
    count_after_first = sys.path.count(str(first))
    second = mod.resolve_tools_dir("forum_api.py")

    assert first == second
    assert sys.path.count(str(first)) == count_after_first
