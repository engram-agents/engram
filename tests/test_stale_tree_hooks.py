"""Tests for hooks/claude/engram-stale-tree-pull-hook.py (Layer 1, SessionStart
prevention) and hooks/claude/engram-stale-tree-warn-hook.py (Layer 2,
PreToolUse catch) -- the #1203 stale-tree tripwire.

Uses REAL git repos in tmp_path (a bare "origin" + a working clone, plus a
throwaway "pusher" clone to advance origin) rather than subprocess mocks --
fast-forward / diverge / dirty / wrong-branch / detached-HEAD behavior then
exercises actual git semantics instead of a guessed mock of them.

Both hooks are loaded fresh per test via importlib (hyphenated filenames
aren't importable modules -- same convention as test_baton_hook.py /
test_hooklib.py), and ENGRAM_HOME is monkeypatched directly onto the loaded
module object so each test's config.json is fully isolated.
"""

import importlib.util
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

PULL_HOOK_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "engram-stale-tree-pull-hook.py"
)
WARN_HOOK_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "engram-stale-tree-warn-hook.py"
)


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_LOAD_COUNTER = 0

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_module(path: Path):
    """Import a hyphenated hook file as a fresh, uniquely-named module instance."""
    global _LOAD_COUNTER
    _LOAD_COUNTER += 1
    spec = importlib.util.spec_from_file_location(
        f"{path.stem.replace('-', '_')}_{_LOAD_COUNTER}", path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Git test-repo helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def _make_origin_and_clone(root: Path, branch: str = "main") -> tuple[Path, Path]:
    """Build a bare 'origin' repo + a real clone tracking it under `root`.

    Returns (origin_path, clone_path).
    """
    root.mkdir(parents=True, exist_ok=True)
    origin = root / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", branch)

    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", branch)
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "f.txt").write_text("v1\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", str(origin), f"{branch}:{branch}")

    clone = root / "clone"
    _git(root, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")
    return origin, clone


def _advance_origin(root: Path, origin: Path, branch: str, msg: str) -> None:
    """Push one more commit to `origin` via a throwaway pusher clone, so the
    repo under test (`clone`) is never touched by the advance itself."""
    pusher = root / f"pusher-{msg.replace(' ', '_')}"
    _git(root, "clone", str(origin), str(pusher))
    _git(pusher, "config", "user.email", "test@example.com")
    _git(pusher, "config", "user.name", "Test")
    (pusher / "f.txt").write_text(msg + "\n")
    _git(pusher, "add", "f.txt")
    _git(pusher, "commit", "-m", msg)
    _git(pusher, "push", str(origin), f"{branch}:{branch}")


def _write_config(engram_home: Path, watch=None) -> None:
    cfg: dict = {}
    if watch is not None:
        cfg["stale_tree_watch"] = watch
    (engram_home / "config.json").write_text(json.dumps(cfg))


# ---------------------------------------------------------------------------
# Layer 1 -- pull hook: config no-op paths
# ---------------------------------------------------------------------------

class TestPullHookConfigNoOp:
    def test_absent_config_is_noop(self, tmp_path, capsys):
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_empty_watch_list_is_noop(self, tmp_path, capsys):
        _write_config(tmp_path, watch=[])
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_missing_stale_tree_watch_key_is_noop(self, tmp_path, capsys):
        (tmp_path / "config.json").write_text(json.dumps({"agent_name": "borges"}))
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_malformed_config_json_is_noop(self, tmp_path, capsys):
        (tmp_path / "config.json").write_text("{not valid json")
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_watched_path_absent_on_disk_is_silent_skip(self, tmp_path, capsys):
        _write_config(
            tmp_path,
            watch=[{"path": str(tmp_path / "does-not-exist"), "branch": "main"}],
        )
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Layer 1 -- pull hook: safety gates
# ---------------------------------------------------------------------------

class TestPullHookSafety:
    def test_fast_forwards_on_clean_default_branch(self, tmp_path, capsys):
        origin, clone = _make_origin_and_clone(tmp_path / "repo", branch="main")
        _advance_origin(tmp_path / "repo", origin, "main", "second commit")
        after_origin = _git(origin, "rev-parse", "main").stdout.strip()
        before = _git(clone, "rev-parse", "HEAD").stdout.strip()
        assert before != after_origin  # sanity: clone genuinely behind

        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0

        after = _git(clone, "rev-parse", "HEAD").stdout.strip()
        assert after == after_origin
        assert "pulled" in capsys.readouterr().out

    def test_skips_on_wrong_branch(self, tmp_path, capsys):
        origin, clone = _make_origin_and_clone(tmp_path / "repo", branch="main")
        _git(clone, "checkout", "-b", "feature/foo")
        before = _git(clone, "rev-parse", "HEAD").stdout.strip()

        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0

        after = _git(clone, "rev-parse", "HEAD").stdout.strip()
        assert after == before  # untouched
        out = capsys.readouterr().out
        assert "feature/foo" in out
        assert "not auto-pulling" in out

    def test_skips_on_detached_head(self, tmp_path, capsys):
        origin, clone = _make_origin_and_clone(tmp_path / "repo", branch="main")
        sha = _git(clone, "rev-parse", "HEAD").stdout.strip()
        _git(clone, "checkout", sha)  # detach

        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0

        out = capsys.readouterr().out
        assert "detached HEAD" in out
        assert "not auto-pulling" in out

    def test_skips_on_dirty_tree(self, tmp_path, capsys):
        origin, clone = _make_origin_and_clone(tmp_path / "repo", branch="main")
        (clone / "f.txt").write_text("dirty uncommitted change\n")
        before = _git(clone, "rev-parse", "HEAD").stdout.strip()

        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0

        after = _git(clone, "rev-parse", "HEAD").stdout.strip()
        assert after == before  # never reset/stashed
        out = capsys.readouterr().out
        assert "uncommitted changes" in out
        assert "not auto-pulling" in out

    def test_skips_on_diverged_history(self, tmp_path, capsys):
        root = tmp_path / "repo"
        origin, clone = _make_origin_and_clone(root, branch="main")
        _advance_origin(root, origin, "main", "origin-side commit")

        # Local-only commit on clone's main -> real divergence from origin.
        (clone / "local.txt").write_text("local-only\n")
        _git(clone, "add", "local.txt")
        _git(clone, "commit", "-m", "local-only commit")
        before = _git(clone, "rev-parse", "HEAD").stdout.strip()

        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0

        after = _git(clone, "rev-parse", "HEAD").stdout.strip()
        assert after == before  # untouched -- ff-only failed closed
        out = capsys.readouterr().out
        assert "diverged" in out
        assert "resolve manually" in out


class TestPullHookMultiRepo:
    def test_multiple_entries_processed_independently(self, tmp_path, capsys):
        origin1, clone1 = _make_origin_and_clone(tmp_path / "r1", branch="main")
        origin2, clone2 = _make_origin_and_clone(tmp_path / "r2", branch="main")
        _git(clone2, "checkout", "-b", "other")  # r2 on wrong branch -> skip+warn

        _write_config(
            tmp_path,
            watch=[
                {"path": str(clone1), "branch": "main"},
                {"path": str(clone2), "branch": "main"},
            ],
        )
        mod = _load_module(PULL_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{}")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0

        out = capsys.readouterr().out
        assert "pulled" in out  # clone1, already-current, clean fast-forward
        assert str(clone2) in out
        assert "not auto-pulling" in out  # clone2, wrong branch


# ---------------------------------------------------------------------------
# Layer 2 -- warn hook: config no-op paths
# ---------------------------------------------------------------------------

def _pretooluse_payload(tool_name: str, tool_input: dict) -> str:
    return json.dumps({"tool_name": tool_name, "tool_input": tool_input})


class TestWarnHookConfigNoOp:
    def test_absent_config_is_noop(self, tmp_path, capsys):
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        payload = _pretooluse_payload("Read", {"file_path": str(tmp_path / "x")})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_empty_watch_list_is_noop(self, tmp_path, capsys):
        _write_config(tmp_path, watch=[])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        payload = _pretooluse_payload("Read", {"file_path": str(tmp_path / "x")})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_malformed_stdin_is_noop(self, tmp_path, capsys):
        _write_config(tmp_path, watch=[{"path": str(tmp_path), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)
        with patch("sys.stdin", io.StringIO("{not valid json")):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Layer 2 -- warn hook: behind-count behavior
# ---------------------------------------------------------------------------

class TestWarnHookBehindCount:
    def test_warns_when_behind(self, tmp_path, capsys):
        root = tmp_path / "repo"
        origin, clone = _make_origin_and_clone(root, branch="main")
        _advance_origin(root, origin, "main", "new commit")
        _git(clone, "fetch", "origin", "main")  # simulate Layer 1 already fetched

        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Read", {"file_path": str(clone / "f.txt")})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0

        out = capsys.readouterr().out
        assert out.strip() != ""
        data = json.loads(out)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "commit(s) behind" in ctx
        assert str(clone) in ctx
        assert "pull --ff-only" in ctx

    def test_silent_when_current(self, tmp_path, capsys):
        root = tmp_path / "repo"
        origin, clone = _make_origin_and_clone(root, branch="main")
        _git(clone, "fetch", "origin", "main")

        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Read", {"file_path": str(clone / "f.txt")})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_never_fetches_itself(self, tmp_path, capsys):
        """The warn hook must reuse already-fetched refs, never `git fetch`
        on its own -- that per-call latency is the whole reason this is a
        behind-COUNT and not a pull."""
        root = tmp_path / "repo"
        origin, clone = _make_origin_and_clone(root, branch="main")
        _advance_origin(root, origin, "main", "new commit")
        # Deliberately do NOT fetch clone's refs: clone's local knowledge of
        # origin/main is still the pre-advance tip, same as its own HEAD. If
        # the hook fetched on its own, origin/main would update and the
        # count would go positive -- a warning firing here would prove an
        # unwanted fetch occurred.

        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Read", {"file_path": str(clone / "f.txt")})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Layer 2 -- warn hook: path-matching across tool shapes
# ---------------------------------------------------------------------------

class TestWarnHookPathMatching:
    def _behind_clone(self, tmp_path):
        root = tmp_path / "repo"
        origin, clone = _make_origin_and_clone(root, branch="main")
        _advance_origin(root, origin, "main", "new commit")
        _git(clone, "fetch", "origin", "main")
        return clone

    def test_read_file_path_matches(self, tmp_path, capsys):
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Read", {"file_path": str(clone / "f.txt")})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert "commit(s) behind" in capsys.readouterr().out

    def test_grep_path_arg_matches(self, tmp_path, capsys):
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Grep", {"pattern": "foo", "path": str(clone)})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert "commit(s) behind" in capsys.readouterr().out

    def test_glob_path_arg_matches(self, tmp_path, capsys):
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Glob", {"pattern": "*.py", "path": str(clone)})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert "commit(s) behind" in capsys.readouterr().out

    def test_bash_command_substring_matches(self, tmp_path, capsys):
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Bash", {"command": f"grep -rn foo {clone}/f.txt"})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert "commit(s) behind" in capsys.readouterr().out

    def test_bash_command_no_match_is_silent(self, tmp_path, capsys):
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Bash", {"command": "ls /tmp/totally-unrelated"})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_bash_command_bare_root_matches(self, tmp_path, capsys):
        """A command referencing the watched root exactly, with no trailing
        path, still matches (end-of-string is a valid boundary)."""
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Bash", {"command": f"ls -la {clone}"})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert "commit(s) behind" in capsys.readouterr().out

    def test_bash_command_root_suffix_is_not_a_false_positive(self, tmp_path, capsys):
        """Round-2 fix: a command referencing <root>XYZ -- a different,
        longer path that merely starts with the watched root's characters --
        must NOT warn. The match is boundary-aware, not a bare substring."""
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        sibling = str(clone) + "XYZ"
        payload = _pretooluse_payload("Bash", {"command": f"cat {sibling}/f.txt"})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_unwired_tool_is_ignored(self, tmp_path, capsys):
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        payload = _pretooluse_payload("Write", {"file_path": str(clone / "f.txt")})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""

    def test_path_outside_any_watched_root_is_silent(self, tmp_path, capsys):
        clone = self._behind_clone(tmp_path)
        _write_config(tmp_path, watch=[{"path": str(clone), "branch": "main"}])
        mod = _load_module(WARN_HOOK_PATH)
        mod.ENGRAM_HOME = str(tmp_path)

        unrelated = tmp_path / "unrelated-dir" / "g.txt"
        payload = _pretooluse_payload("Read", {"file_path": str(unrelated)})
        with patch("sys.stdin", io.StringIO(payload)):
            with pytest.raises(SystemExit) as exc:
                mod.main()
        assert exc.value.code == 0
        assert capsys.readouterr().out == ""
