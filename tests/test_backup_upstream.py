"""Tests for #2046 — the ~/.engram backup branch must always have an upstream
configured so `git status` / `git status -sb` can report push backlog instead of
staying silent.

Real git repos only (a local temp dir as the "clone", and a local *bare* repo as
the fake `origin` remote) — no mocked git subprocess calls, so this exercises the
actual `git push -u` behavior these tests are meant to pin down. Never touches a
real `~/.engram`: DATA_DIR is rerouted via `server._configure_paths(tmp)` per the
established test pattern (see test_git_backup_embeddings.py), which is the fix for
the 2026-08-10 incident where a module-level default DATA_DIR path let a fairy's
test run clobber production config.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )


@pytest.fixture
def repo_pair(tmp_path):
    """A bare 'origin' repo plus a clone-shaped local repo pointed at it, with no
    upstream configured yet — i.e. exactly the state #2046 describes: a real,
    working push target, but `git status -sb` cannot report ahead/behind."""
    bare = tmp_path / "origin.git"
    _run_git(tmp_path, "init", "--bare", "-b", "master", str(bare))

    local = tmp_path / "local"
    local.mkdir()
    _run_git(local, "init", "-b", "master")
    _run_git(local, "config", "user.email", "engram@local")
    _run_git(local, "config", "user.name", "KG Memory")
    _run_git(local, "remote", "add", "origin", str(bare))

    (local / "graph_snapshot.md").write_text("# snapshot v1\n")
    _run_git(local, "add", "-A")
    _run_git(local, "commit", "-m", "initial")

    yield local, bare


@pytest.fixture
def core(tmp_path, repo_pair, monkeypatch):
    """Import engram_core (via server._configure_paths) rerouted at the local repo
    from repo_pair, never at a real ~/.engram."""
    local, bare = repo_pair
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    monkeypatch.setenv("ENGRAM_NO_DB_GUARDS", "1")
    import server

    server._configure_paths(local)
    server._ensure_data_dir()
    server.core._git_available = True
    yield server.core


def _upstream_ref(repo: Path) -> str | None:
    r = _run_git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return r.stdout.strip() if r.returncode == 0 else None


def _status_sb_branch_line(repo: Path) -> str:
    """The `## <branch>...<upstream> [ahead N]` header line only — `git status -sb`
    also lists untracked files (e.g. `server._ensure_data_dir()` writes config.json
    into DATA_DIR alongside the git repo in these tests), which is noise for what
    this file is checking: the branch/upstream/divergence line itself."""
    r = _run_git(repo, "status", "-sb")
    return r.stdout.splitlines()[0].strip() if r.stdout.strip() else ""


class TestFirstPushSetsUpstream:
    """A branch with a valid remote but no upstream: the first push must set one."""

    def test_no_upstream_before_first_push(self, repo_pair):
        local, _bare = repo_pair
        assert _upstream_ref(local) is None
        # Confirms the fixture actually reproduces #2046's starting condition:
        # a bare `## master` with no divergence marker.
        assert _status_sb_branch_line(local) == "## master"

    def test_push_head_ensuring_upstream_sets_it(self, core, repo_pair):
        local, bare = repo_pair
        result = core._push_head_ensuring_upstream()
        assert result.returncode == 0, result.stderr
        assert _upstream_ref(local) == "origin/master"

    def test_status_sb_reports_clean_after_first_push(self, core, repo_pair):
        local, bare = repo_pair
        core._push_head_ensuring_upstream()
        assert _status_sb_branch_line(local) == "## master...origin/master"

    def test_second_push_is_a_noop_on_already_set_upstream(self, core, repo_pair):
        local, bare = repo_pair
        first = core._push_head_ensuring_upstream()
        assert first.returncode == 0
        (local / "graph_snapshot.md").write_text("# snapshot v2\n")
        _run_git(local, "add", "-A")
        _run_git(local, "commit", "-m", "second")
        second = core._push_head_ensuring_upstream()
        assert second.returncode == 0
        assert _upstream_ref(local) == "origin/master"
        assert _status_sb_branch_line(local) == "## master...origin/master"

    def test_has_upstream_false_then_true(self, core, repo_pair):
        local, _bare = repo_pair
        assert core._has_upstream() is False
        core._push_head_ensuring_upstream()
        assert core._has_upstream() is True


class TestBacklogVisibleAfterUpstreamSet:
    """Once the upstream is wired up, a skipped/failed push must show up as an
    ahead-count on `git status -sb` — the actual detectability #2046 is about."""

    def test_ahead_count_visible_when_push_is_skipped(self, core, repo_pair):
        local, bare = repo_pair
        # First push establishes the upstream tracking ref (the fix).
        core._push_head_ensuring_upstream()
        assert _status_sb_branch_line(local) == "## master...origin/master"

        # Simulate a later commit whose push never happens (e.g. worker died, key
        # expired) — commit locally but do NOT call the push helper.
        (local / "graph_snapshot.md").write_text("# snapshot v2\n")
        _run_git(local, "add", "-A")
        _run_git(local, "commit", "-m", "second, never pushed")

        status = _status_sb_branch_line(local)
        assert "ahead 1" in status, (
            f"expected the un-pushed commit to surface as ahead-count; got: {status!r}"
        )

    def test_ahead_count_visible_when_push_fails(self, core, repo_pair):
        local, bare = repo_pair
        core._push_head_ensuring_upstream()

        # Break the remote (simulates a rejected/failed push — e.g. expired SSH key,
        # remote gone) by pointing origin somewhere unreachable, then commit+push
        # through the real helper and confirm it reports failure without crashing.
        _run_git(local, "remote", "set-url", "origin", "/nonexistent/no-such-repo.git")
        (local / "graph_snapshot.md").write_text("# snapshot v3\n")
        _run_git(local, "add", "-A")
        _run_git(local, "commit", "-m", "third, push will fail")

        result = core._push_head_ensuring_upstream()
        assert result.returncode != 0

        status = _status_sb_branch_line(local)
        assert "ahead 1" in status, (
            f"expected the failed-push commit to surface as ahead-count; got: {status!r}"
        )

    def test_before_fix_status_sb_would_show_nothing(self, repo_pair):
        """Negative control: without the upstream ever being set (the pre-#2046
        behavior — plain `git push origin HEAD`, no `-u`), a locally-committed,
        never-pushed change is invisible to `git status -sb`. This pins down the
        exact silent-failure the issue describes."""
        local, bare = repo_pair
        # Old behavior: push without -u. Upstream never gets configured.
        _run_git(local, "push", "origin", "HEAD:master")
        assert _upstream_ref(local) is None

        (local / "graph_snapshot.md").write_text("# snapshot v2, never pushed\n")
        _run_git(local, "add", "-A")
        _run_git(local, "commit", "-m", "second, never pushed")

        status = _status_sb_branch_line(local)
        assert status == "## master", (
            f"expected the pre-fix silent failure (no divergence marker); got: {status!r}"
        )
        assert "ahead" not in status
