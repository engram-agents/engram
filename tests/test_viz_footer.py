"""Tests for viz.footer — the shared page-footer partial (engram-alpha#2555
PR B amendment-1, requirement 4; Kepler's ruling, forum #389 reply #7271).

Covers:
  1. _data_provenance: nonexistent dir, non-git dir, real git dir -> a real
     short sha.
  2. _code_provenance: both labeled install shapes (worktree / plugin),
     the -dirty suffix, and the unversioned fallback -- module-cache reset
     between cases since the real function caches per-process.
  3. render_footer: content shape, unversioned-data-while-code-renders
     (Kepler's stated falsifier for the PR, reproduced directly against
     the function rather than only at the live-HTTP level below).
  4. Live-server: footer present on all 4 real routes; the SAME falsifier
     end-to-end (bare knowledge.db in a real non-git tmpdir).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents
from viz import footer as viz_footer
from viz import server as viz_server


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=15)


def _init_tmp_repo(path: Path) -> str:
    """Create a real, throwaway git repo at *path* with one commit. Returns
    the full HEAD sha."""
    _git(["init", "-q"], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    (path / "knowledge.db").write_bytes(b"")
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-q", "-m", "initial"], cwd=path)
    result = _git(["rev-parse", "HEAD"], cwd=path)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# 1. _data_provenance
# ---------------------------------------------------------------------------

class TestDataProvenance(unittest.TestCase):
    def test_none_data_dir_is_unversioned(self):
        self.assertEqual(viz_footer._data_provenance(None), "unversioned")

    def test_empty_string_data_dir_is_unversioned(self):
        self.assertEqual(viz_footer._data_provenance(""), "unversioned")

    def test_nonexistent_dir_is_unversioned(self):
        self.assertEqual(
            viz_footer._data_provenance("/nonexistent/does-not-exist-at-all"),
            "unversioned",
        )

    def test_real_non_git_dir_is_unversioned(self):
        """THE FALSIFIER, at the unit level: Kepler's stated PR falsifier is
        --db pointed at a bare knowledge.db in a non-git directory -> data
        renders unversioned. This is that exact scenario, minus the HTTP
        layer (see TestLiveServerFooterFalsifier for the full-stack form)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "knowledge.db").write_bytes(b"")
            self.assertEqual(viz_footer._data_provenance(tmpdir), "unversioned")

    def test_real_git_dir_returns_short_sha(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            full_sha = _init_tmp_repo(Path(tmpdir))
            result = viz_footer._data_provenance(tmpdir)
            self.assertEqual(result, full_sha[:7])
            self.assertNotEqual(result, "unversioned")


# ---------------------------------------------------------------------------
# 2. _code_provenance -- both install shapes
# ---------------------------------------------------------------------------

class TestCodeProvenance(unittest.TestCase):
    def setUp(self):
        # _code_provenance caches its result module-globally (deliberate --
        # code doesn't change mid-process). Save/clear/restore around every
        # test so cases don't leak into each other.
        self._saved_cache = viz_footer._CODE_PROVENANCE_CACHE
        viz_footer._CODE_PROVENANCE_CACHE = None

    def tearDown(self):
        viz_footer._CODE_PROVENANCE_CACHE = self._saved_cache

    def test_worktree_shape_real_repo(self):
        """The real case this test process is actually running under: this
        repo checkout has a real .git above viz/footer.py, so
        _code_provenance() should resolve the worktree shape against it."""
        result = viz_footer._code_provenance()
        self.assertIn("(worktree)", result)
        self.assertNotEqual(result, "unversioned")

    def test_worktree_shape_via_synthetic_repo(self):
        """Isolate the worktree-shape LOGIC from this real repo's actual
        state (dirty or not) by pointing _find_git_root at a synthetic,
        controlled repo instead."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            full_sha = _init_tmp_repo(repo)
            saved_finder = viz_footer._find_git_root
            viz_footer._find_git_root = lambda start: repo
            try:
                result = viz_footer._code_provenance()
            finally:
                viz_footer._find_git_root = saved_finder
            self.assertEqual(result, f"{full_sha[:7]} (worktree)")

    def test_worktree_shape_dirty_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            full_sha = _init_tmp_repo(repo)
            (repo / "uncommitted.txt").write_text("dirty")  # untracked-only change
            # untracked-files=no means a bare untracked file does NOT count
            # as dirty (matches install-local-marketplace.sh's own
            # --untracked-files=no convention) -- modify a TRACKED file
            # instead to genuinely dirty the tree.
            (repo / "knowledge.db").write_bytes(b"modified")
            saved_finder = viz_footer._find_git_root
            viz_footer._find_git_root = lambda start: repo
            try:
                result = viz_footer._code_provenance()
            finally:
                viz_footer._find_git_root = saved_finder
            self.assertEqual(result, f"{full_sha[:7]}-dirty (worktree)")

    def test_plugin_shape_via_deployed_version(self):
        """No .git found (simulated) -> reads .deployed-version instead."""
        with tempfile.TemporaryDirectory() as engram_home:
            (Path(engram_home) / ".deployed-version").write_text(
                "alpha_sha=abc1234\nalpha_dirty=clean\n"
            )
            saved_finder = viz_footer._find_git_root
            saved_environ = dict(os.environ)
            viz_footer._find_git_root = lambda start: None
            os.environ["ENGRAM_HOME"] = engram_home
            try:
                result = viz_footer._code_provenance()
            finally:
                viz_footer._find_git_root = saved_finder
                os.environ.clear()
                os.environ.update(saved_environ)
            self.assertEqual(result, "abc1234 (plugin)")

    def test_plugin_shape_dirty_suffix(self):
        with tempfile.TemporaryDirectory() as engram_home:
            (Path(engram_home) / ".deployed-version").write_text(
                "alpha_sha=abc1234\nalpha_dirty=dirty\n"
            )
            saved_finder = viz_footer._find_git_root
            saved_environ = dict(os.environ)
            viz_footer._find_git_root = lambda start: None
            os.environ["ENGRAM_HOME"] = engram_home
            try:
                result = viz_footer._code_provenance()
            finally:
                viz_footer._find_git_root = saved_finder
                os.environ.clear()
                os.environ.update(saved_environ)
            self.assertEqual(result, "abc1234-dirty (plugin)")

    def test_plugin_shape_no_marker_is_unversioned(self):
        with tempfile.TemporaryDirectory() as engram_home:
            saved_finder = viz_footer._find_git_root
            saved_environ = dict(os.environ)
            viz_footer._find_git_root = lambda start: None
            os.environ["ENGRAM_HOME"] = engram_home
            try:
                result = viz_footer._code_provenance()
            finally:
                viz_footer._find_git_root = saved_finder
                os.environ.clear()
                os.environ.update(saved_environ)
            self.assertEqual(result, "unversioned")

    def test_code_provenance_is_cached(self):
        first = viz_footer._code_provenance()
        # Even if the underlying resolution would now differ (simulate by
        # swapping the finder), the cached value is returned unchanged --
        # code doesn't change mid-process, per the module's own docstring.
        saved_finder = viz_footer._find_git_root
        viz_footer._find_git_root = lambda start: None
        try:
            second = viz_footer._code_provenance()
        finally:
            viz_footer._find_git_root = saved_finder
        self.assertEqual(first, second)


class TestFindGitRootBounded(unittest.TestCase):
    """_find_git_root's walk is deliberately bounded (reviewer-fairy finding
    on PR#2564) -- unlike its unbounded siblings elsewhere in viz/data/, it
    searches for "any .git", a broad target where an unrelated ancestor
    repo (a dotfiles-tracked $HOME, a git-tracked container root) is a
    real, plausible false positive, not an astronomically unlikely one."""

    def test_git_within_bound_is_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            full_sha = _init_tmp_repo(repo)
            nested = repo
            for i in range(5):
                nested = nested / f"level{i}"
                nested.mkdir()
            result = viz_footer._find_git_root(nested)
            self.assertEqual(result, repo)

    def test_git_beyond_bound_is_not_found(self):
        """THE FALSIFIER: an ancestor .git that exists but sits further up
        than _GIT_ROOT_SEARCH_DEPTH must NOT be found -- confirms the bound
        is real, not just documented."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _init_tmp_repo(repo)
            nested = repo
            # One level deeper than the search depth, so the real .git at
            # `repo` sits just outside the searched range.
            for i in range(viz_footer._GIT_ROOT_SEARCH_DEPTH + 1):
                nested = nested / f"level{i}"
                nested.mkdir()
            result = viz_footer._find_git_root(nested)
            self.assertIsNone(
                result,
                "found a .git beyond the documented search bound -- the "
                "cap is not actually being enforced",
            )

    def test_no_git_anywhere_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(viz_footer._find_git_root(Path(tmpdir)))


# ---------------------------------------------------------------------------
# 3. render_footer -- unit level
# ---------------------------------------------------------------------------

class TestRenderFooter(unittest.TestCase):
    def test_contains_product_provenance_and_disclaimer(self):
        html = viz_footer.render_footer(None)
        self.assertIn('class="viz-footer"', html)
        self.assertIn("ENGRAM", html)
        self.assertIn("Apache-2.0", html)
        self.assertIn("data anchor unversioned", html)

    def test_falsifier_unversioned_data_code_still_renders(self):
        """Kepler's exact falsifier, reproduced directly against
        render_footer(): a data_dir that resolves to unversioned must NOT
        suppress the code half."""
        html = viz_footer.render_footer("/nonexistent/does-not-exist")
        self.assertIn("data anchor unversioned", html)
        self.assertNotIn("code unversioned", html,
                         "code half incorrectly rendered unversioned when "
                         "only the data half should have failed")


# ---------------------------------------------------------------------------
# 4. Live-server: footer on every real route, plus the full-stack falsifier
# ---------------------------------------------------------------------------

class _LiveServerBase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path: str) -> bytes:
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.read()


class TestLiveServerFooterPresence(_LiveServerBase):
    def test_footer_present_on_every_page_route(self):
        for path in ("/", "/health", "/stats", "/config"):
            body = self._get(path).decode()
            self.assertIn('<footer class="viz-footer">', body,
                          f"{path} response missing the footer")
            self.assertNotIn("FOOTER_PLACEHOLDER", body,
                             f"{path} response leaked the raw placeholder "
                             f"sentinel instead of the rendered footer")


class TestLiveServerFooterFalsifier(_LiveServerBase):
    """Kepler's PR falsifier, full stack: --db pointed at a bare
    knowledge.db in a non-git directory -> footer's data half says
    "unversioned" while the code half still renders."""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "knowledge.db"
        self.db_path.write_bytes(b"")
        self._saved_agents = dict(viz_agents.AGENTS)
        self._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "bareagent": {"name": "bareagent", "label": "Bare",
                          "db": str(self.db_path)},
        }
        viz_agents.DEFAULT_AGENT = "bareagent"

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        viz_agents.DEFAULT_AGENT = self._saved_default
        self._tmpdir.cleanup()
        super().tearDown()

    def test_data_unversioned_code_still_renders(self):
        body = self._get("/health?agent=bareagent").decode()
        self.assertIn("data anchor unversioned", body)
        self.assertNotIn("code unversioned", body,
                         "code half should still render even when the "
                         "selected agent's own data dir isn't a git repo")


if __name__ == "__main__":
    unittest.main()
