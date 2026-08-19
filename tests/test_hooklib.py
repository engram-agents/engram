"""Tests for hooks/claude/_hooklib.py (gh#1657 -- shared prompt-hook helper).

Covers resolve_tools_dir()'s three resolution paths (CLAUDE_PLUGIN_ROOT set,
walk-parents fallback, marker-not-found -> None) and its sys.path side
effect, isolated from any real hook or real tools/ directory via a fresh
module load + a synthetic directory tree per test.

Also covers resolve_own_server_py_path() / is_own_server_py_running() --
extracted from engram-session-start-hook.py and engram-surface-hook.py after
PR #1839's colleague review found the surface hook had copied the
session-start hook's already-broken UNSCOPED `pgrep -f "server.py"` verbatim.
Every existing test in either hook's own test file stubs the pgrep-backed
function completely (`monkeypatch.setattr(mod, "_is_any_server_py_running",
lambda: True/False)`), so the real subprocess call never ran under CI -- the
TestIsOwnServerPyRunningScoping tests below exercise it directly with a live
decoy process instead of stubbing it away.

Also covers atomic_write_json() (gh#2035 -- one atomic-write convention for
hook state files): no temp file left behind on the success or exception
path, and a failure mid-write leaves the original file intact -- the actual
property atomicity buys.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
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


def _spawn_decoy(path):
    """Start a short-lived process whose argv contains `path` literally, so
    an UNSCOPED `pgrep -f server.py` would match it by substring."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)", str(path)],
    )
    # Give pgrep something to find -- Popen returns before the process's
    # argv is necessarily visible to /proc or ps on every platform.
    time.sleep(0.3)
    return proc


class TestResolveOwnServerPyPath:
    def test_never_returns_bare_filename(self, tmp_path):
        """The resolved candidate must be a path pgrep can scope to, never
        the literal unscoped string "server.py" that caused the regression."""
        mod = _load_module()
        result = mod.resolve_own_server_py_path(str(tmp_path))
        assert result != "server.py"
        assert result.endswith("server.py")

    def test_prefers_engram_home_candidate_when_present(self, tmp_path):
        """$ENGRAM_HOME/server.py wins when there's no hook-colocated one
        (this test module isn't colocated with the real server.py)."""
        (tmp_path / "server.py").write_text("# fixture\n")
        mod = _load_module()
        result = mod.resolve_own_server_py_path(str(tmp_path))
        assert result == str(tmp_path / "server.py")

    def test_resolves_runtime_root_server_py_in_flattened_deployed_topology(
        self, tmp_path
    ):
        """Round-2 regression (reviewer-fairy caught this): the plugin build
        FLATTENS hooks/claude/ -> hooks/, and server.py is a SIBLING of that
        hooks/ directory (one level above _hooklib.py itself), not colocated
        inside it -- verified empirically against a live plugin install
        (server.py at `<plugin>/server.py`, _hooklib.py at
        `<plugin>/hooks/_hooklib.py`). An earlier version of
        resolve_own_server_py_path used dirname(this file) directly -- one
        level too shallow -- which silently fell through to the dev
        fallback in exactly this topology. Recreate the deployed layout
        (root/hooks/_hooklib.py + root/server.py as its sibling-of-parent)
        and confirm the runtime-root candidate is found, not the fallback.
        Same method test_baton_hook.py's flattened-topology regression test
        uses for the sibling gh#1657 bug class.
        """
        root = tmp_path / "plugin_root"
        (root / "hooks").mkdir(parents=True)
        hooklib_src = MODULE_PATH.read_text()
        (root / "hooks" / "_hooklib.py").write_text(hooklib_src)
        (root / "server.py").write_text("# fixture standing in for the real server.py\n")

        spec = importlib.util.spec_from_file_location(
            "_hooklib_flattened_instance", root / "hooks" / "_hooklib.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        empty_engram_home = tmp_path / "engram_home_no_server"
        empty_engram_home.mkdir()
        result = mod.resolve_own_server_py_path(str(empty_engram_home))

        assert result == str(root / "server.py")
        # Guard against the exact round-2 regression: must not silently fall
        # through to the dev-fallback candidate in this topology.
        assert "engram-alpha" not in result


class TestIsOwnServerPyRunningScoping:
    def test_decoy_with_matching_substring_at_unrelated_path_is_not_matched(
        self, tmp_path
    ):
        """The regression this fixes: a process whose command line contains
        "server.py" as a substring, at a path that is NOT this agent's own
        resolved server.py, must not be treated as this agent's server.

        Pre-fix, `pgrep -f "server.py"` (unscoped) WOULD have matched this
        decoy -- that was exactly the false-healthy failure mode PR #1839's
        colleague review flagged (every OTHER agent's server.py, matched by
        bare substring).
        """
        decoy_dir = tmp_path / "other_agent_home"
        decoy_dir.mkdir()
        decoy_path = decoy_dir / "server.py"
        decoy_path.write_text("# decoy, not this agent's real server\n")

        mod = _load_module()

        # engram_home points somewhere with NO server.py at all, so
        # resolve_own_server_py_path falls through to the dev-fallback
        # candidate (~/engram-alpha/server.py) -- never the decoy's path.
        empty_home = tmp_path / "empty_engram_home"
        empty_home.mkdir()
        resolved = mod.resolve_own_server_py_path(str(empty_home))
        assert resolved != str(decoy_path)

        proc = _spawn_decoy(decoy_path)
        try:
            # Sanity check the decoy is actually visible to pgrep at all
            # (i.e. this isn't a false negative from the process never
            # starting) -- an UNSCOPED search for "server.py" must find it.
            unscoped = subprocess.run(
                ["pgrep", "-f", "server.py"], capture_output=True, timeout=2
            )
            assert unscoped.returncode == 0, (
                "decoy process not visible to pgrep -- test setup is broken, "
                "not the code under test"
            )

            # The scoped check must NOT be fooled by the decoy.
            assert mod.is_own_server_py_running(str(empty_home)) is False
        finally:
            proc.terminate()
            proc.wait(timeout=2)

    def test_matches_when_own_resolved_server_py_is_actually_running(
        self, tmp_path
    ):
        """Positive case: when a process is running at exactly the resolved
        path, the scoped check does find it -- confirms the fix narrows the
        match, it doesn't just always return False."""
        mod = _load_module()
        engram_home = tmp_path / "engram_home"
        engram_home.mkdir()
        own_server = engram_home / "server.py"
        own_server.write_text("# fixture standing in for the real server.py\n")

        proc = _spawn_decoy(own_server)
        try:
            assert mod.is_own_server_py_running(str(engram_home)) is True
        finally:
            proc.terminate()
            proc.wait(timeout=2)

    def test_returns_false_on_no_match_anywhere(self, tmp_path):
        """No decoy running at all -> False, not an exception."""
        mod = _load_module()
        engram_home = tmp_path / "engram_home_nothing_running"
        engram_home.mkdir()
        assert mod.is_own_server_py_running(str(engram_home)) is False


# ---------------------------------------------------------------------------
# atomic_write_json() -- gh#2035
# ---------------------------------------------------------------------------


class TestAtomicWriteJson:
    def test_success_path_writes_content_and_leaves_no_temp_file(self, tmp_path):
        mod = _load_module()
        path = tmp_path / "state.json"
        data = {"pending": True, "count": 3}

        mod.atomic_write_json(str(path), data)

        assert path.exists()
        assert json.loads(path.read_text()) == data
        # No stray *.tmp sibling of any name (mkstemp uses a random infix,
        # so this checks the whole directory rather than one literal name).
        leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
        assert leftovers == []

    def test_exception_path_leaves_no_temp_file(self, tmp_path, monkeypatch):
        mod = _load_module()
        path = tmp_path / "state.json"

        monkeypatch.setattr(
            mod.json, "dump", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        )

        with pytest.raises(ValueError):
            mod.atomic_write_json(str(path), {"x": 1})

        assert not path.exists()
        leftovers = list(tmp_path.iterdir())
        assert leftovers == []

    def test_failure_mid_write_leaves_original_file_intact(self, tmp_path, monkeypatch):
        """The actual property atomicity buys: a write that fails partway
        through must never corrupt or truncate the file that was already
        there."""
        mod = _load_module()
        path = tmp_path / "state.json"
        original_content = json.dumps({"transcript_path": "/a/b.jsonl", "byte_offset": 42})
        path.write_text(original_content)

        monkeypatch.setattr(
            mod.json, "dump", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        )

        with pytest.raises(ValueError):
            mod.atomic_write_json(str(path), {"transcript_path": "/other.jsonl", "byte_offset": 0})

        # Byte-identical to the pre-failure content -- not merely "still
        # valid JSON" or "still parseable".
        assert path.read_text() == original_content

    def test_writes_across_multiple_calls_replace_cleanly(self, tmp_path):
        """Regression guard for the temp-file-cleanup path: repeated
        successful writes to the same destination never accumulate stray
        temp files (each write's mkstemp result is consumed by os.replace)."""
        mod = _load_module()
        path = tmp_path / "state.json"

        for i in range(5):
            mod.atomic_write_json(str(path), {"n": i})

        assert json.loads(path.read_text()) == {"n": 4}
        leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
        assert leftovers == []

    def test_indent_and_encoding_pass_through(self, tmp_path):
        mod = _load_module()
        path = tmp_path / "state.json"

        mod.atomic_write_json(str(path), {"a": 1}, indent=2, encoding="utf-8")

        text = path.read_text(encoding="utf-8")
        assert json.loads(text) == {"a": 1}
        assert "\n" in text  # indent=2 produces multi-line output

    # Cross-filesystem behavior (os.replace() requires same filesystem as
    # the destination) is not testable here -- see the docstring's "Same
    # directory matters" note. No test for that case per this issue's spec.
