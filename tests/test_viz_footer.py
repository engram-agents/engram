"""Tests for viz.footer — the shared page-footer partial (engram-alpha#2555
PR B amendment-1, requirement 4; Kepler's ruling, forum #389 reply #7271).

Covers:
  1. _data_provenance: nonexistent dir, non-git dir, real git dir -> a real
     short sha.
  2. _code_provenance: both labeled install shapes (worktree / plugin),
     the -dirty suffix, and the unversioned fallback -- module-cache reset
     between cases since the real function caches per-process. PR #3174:
     _code_provenance() now delegates entirely to
     viz.version_info.capture_version_info() rather than walking the tree
     itself, so the worktree/plugin-shape cases below redirect that
     delegation (via a patched __file__, or a patched
     version_info.capture_version_info return value) instead of the old
     _find_git_root monkeypatch, which no longer exists.
  3. render_footer: content shape, unversioned-data-while-code-renders
     (Kepler's stated falsifier for the PR, reproduced directly against
     the function rather than only at the live-HTTP level below).
  4. Live-server: footer present on all 4 real routes; the SAME falsifier
     end-to-end (bare knowledge.db in a real non-git tmpdir).
  5. PR #3174 contract-first: the untracked-ancestor-repo defect this PR
     fixes, reproduced end-to-end through _code_provenance() itself (not
     just at the version_info unit level, which tests/test_viz_version_
     endpoint.py's TestUntrackedAncestorRepoRejected already covers) --
     and the positive control that a genuinely tracked checkout still
     renders (worktree).
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
from unittest import mock
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents
from viz import footer as viz_footer
from viz import server as viz_server
from viz import version_info as viz_version_info


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


def _init_tmp_code_repo(path: Path) -> str:
    """Create a real, throwaway git repo at *path* with a COMMITTED
    server.py. PR #3174: _code_provenance() now delegates to
    capture_version_info(), whose tracking-check guard only accepts a
    found git root if it actually tracks the real server.py living next
    to the start path -- so any test exercising the real delegation
    (rather than mocking capture_version_info's return value) needs a
    committed server.py, mirroring tests/test_viz_version_endpoint.py's
    own _init_repo helper. Returns the full HEAD sha."""
    _git(["init", "-q"], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    (path / "server.py").write_text("# stub for tracking-check tests\n")
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
        state (dirty or not) by redirecting _code_provenance()'s delegated
        capture_version_info() call to a synthetic, controlled repo
        instead. PR #3174: _find_git_root no longer exists (footer
        delegates entirely to version_info.capture_version_info()), so the
        redirection point is now version_info.capture_version_info itself
        -- captured from the REAL, synthetic repo (not hand-built), so the
        patch only changes WHICH directory is walked, not the walking/
        tracking-check logic itself (that logic has its own coverage in
        tests/test_viz_version_endpoint.py)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            full_sha = _init_tmp_code_repo(repo)
            original_capture = viz_footer.version_info.capture_version_info
            with mock.patch.object(
                viz_footer.version_info, "capture_version_info",
                side_effect=lambda *a, **k: original_capture(repo),
            ):
                result = viz_footer._code_provenance()
            self.assertEqual(result, f"{full_sha[:7]} (worktree)")

    def test_worktree_shape_dirty_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            full_sha = _init_tmp_code_repo(repo)
            (repo / "uncommitted.txt").write_text("dirty")  # untracked-only change
            # untracked-files=no (version_info.capture_version_info's own
            # convention, unified with footer's per PR #3174) means a bare
            # untracked file does NOT count as dirty -- modify a TRACKED
            # file instead to genuinely dirty the tree.
            (repo / "server.py").write_text("# modified\n")
            original_capture = viz_footer.version_info.capture_version_info
            with mock.patch.object(
                viz_footer.version_info, "capture_version_info",
                side_effect=lambda *a, **k: original_capture(repo),
            ):
                result = viz_footer._code_provenance()
            self.assertEqual(result, f"{full_sha[:7]}-dirty (worktree)")

    def test_plugin_shape_via_deployed_version(self):
        """capture_version_info() returning a null sha for a genuinely
        no-checkout reason -> reads .deployed-version instead (PR #3174:
        version_info.is_plugin_fallback_reason + read_deployed_version)."""
        with tempfile.TemporaryDirectory() as engram_home:
            (Path(engram_home) / ".deployed-version").write_text(
                "alpha_sha=abc1234\nalpha_dirty=clean\n"
            )
            saved_environ = dict(os.environ)
            os.environ["ENGRAM_HOME"] = engram_home
            null_capture = {
                "sha": None, "branch": None, "dirty": None,
                "started_at": "2026-01-01T00:00:00+00:00",
                "reason": "not a git checkout",
            }
            try:
                with mock.patch.object(
                    viz_footer.version_info, "capture_version_info",
                    return_value=null_capture,
                ):
                    result = viz_footer._code_provenance()
            finally:
                os.environ.clear()
                os.environ.update(saved_environ)
            self.assertEqual(result, "abc1234 (plugin)")

    def test_plugin_shape_dirty_suffix(self):
        with tempfile.TemporaryDirectory() as engram_home:
            (Path(engram_home) / ".deployed-version").write_text(
                "alpha_sha=abc1234\nalpha_dirty=dirty\n"
            )
            saved_environ = dict(os.environ)
            os.environ["ENGRAM_HOME"] = engram_home
            null_capture = {
                "sha": None, "branch": None, "dirty": None,
                "started_at": "2026-01-01T00:00:00+00:00",
                "reason": "not a git checkout",
            }
            try:
                with mock.patch.object(
                    viz_footer.version_info, "capture_version_info",
                    return_value=null_capture,
                ):
                    result = viz_footer._code_provenance()
            finally:
                os.environ.clear()
                os.environ.update(saved_environ)
            self.assertEqual(result, "abc1234-dirty (plugin)")

    def test_plugin_shape_no_marker_is_unversioned(self):
        with tempfile.TemporaryDirectory() as engram_home:
            saved_environ = dict(os.environ)
            os.environ["ENGRAM_HOME"] = engram_home
            null_capture = {
                "sha": None, "branch": None, "dirty": None,
                "started_at": "2026-01-01T00:00:00+00:00",
                "reason": "not a git checkout",
            }
            try:
                with mock.patch.object(
                    viz_footer.version_info, "capture_version_info",
                    return_value=null_capture,
                ):
                    result = viz_footer._code_provenance()
            finally:
                os.environ.clear()
                os.environ.update(saved_environ)
            self.assertEqual(result, "unversioned")

    def test_dubious_ownership_reason_is_unversioned_not_plugin(self):
        """PR #3174 contract item 2: a null sha whose reason is a git
        FAILURE (dubious ownership / git-command-failed) -- as opposed to
        genuinely no tracking checkout -- must render unversioned, never
        silently fall back to a possibly-stale .deployed-version marker as
        if it were this run's live code provenance."""
        with tempfile.TemporaryDirectory() as engram_home:
            (Path(engram_home) / ".deployed-version").write_text(
                "alpha_sha=abc1234\nalpha_dirty=clean\n"
            )
            saved_environ = dict(os.environ)
            os.environ["ENGRAM_HOME"] = engram_home
            null_capture = {
                "sha": None, "branch": None, "dirty": None,
                "started_at": "2026-01-01T00:00:00+00:00",
                "reason": "git refused: dubious ownership (the server's "
                          "user does not own this checkout; add it to "
                          "that user's safe.directory)",
            }
            try:
                with mock.patch.object(
                    viz_footer.version_info, "capture_version_info",
                    return_value=null_capture,
                ):
                    result = viz_footer._code_provenance()
            finally:
                os.environ.clear()
                os.environ.update(saved_environ)
            self.assertEqual(result, "unversioned")

    def test_code_provenance_is_cached(self):
        first = viz_footer._code_provenance()
        # Even if the underlying resolution would now differ (simulate by
        # swapping the delegated capture), the cached value is returned
        # unchanged -- code doesn't change mid-process, per the module's
        # own docstring.
        null_capture = {
            "sha": None, "branch": None, "dirty": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "reason": "not a git checkout",
        }
        with mock.patch.object(
            viz_footer.version_info, "capture_version_info",
            return_value=null_capture,
        ):
            second = viz_footer._code_provenance()
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# 2.5. PR #3174 contract-first: the untracked-ancestor-repo defect this PR
# fixes, reproduced end-to-end through _code_provenance() itself -- footer's
# OWN prior bounded walk (with no tracking check) is what silently
# misattributed an unrelated ancestor repo's HEAD as this code's provenance
# on a real plugin install (measured against the installed plugin copy:
# _code_provenance() returned '~/.engram's HEAD, labeled (worktree)).
#
# Contract-first (per this repo's CLAUDE.md): written BEFORE footer.py
# delegates to version_info -- test_untracked_ancestor_layout_renders_
# plugin_not_worktree must be RED on 8af70cb4 (footer's own unguarded
# bounded walk finds the outer repo and happily reports ITS sha, labeled
# (worktree)); test_tracked_checkout_renders_worktree_not_plugin is the
# positive control that a genuinely tracked checkout still renders
# (worktree) under the new delegation.
#
# Redirection mechanism: _code_provenance() computes
# `Path(__file__).resolve().parent` and passes it explicitly as
# capture_version_info()'s start_path (see footer.py's own docstring for
# why this is the intended calling shape, not a test-only hack) -- so
# patching viz_footer.__file__ to a path inside the synthetic layout makes
# the REAL, unmocked delegation chain (footer -> version_info -> real git
# subprocess calls) walk the synthetic layout instead of this actual dev
# checkout.
# ---------------------------------------------------------------------------

class TestCodeProvenancePluginFallbackIntegration(unittest.TestCase):
    def setUp(self):
        self._saved_cache = viz_footer._CODE_PROVENANCE_CACHE
        viz_footer._CODE_PROVENANCE_CACHE = None

    def tearDown(self):
        viz_footer._CODE_PROVENANCE_CACHE = self._saved_cache

    def test_untracked_ancestor_layout_renders_plugin_not_worktree(self):
        with tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as engram_home:
            outer = Path(tmp) / "outer"
            outer.mkdir()
            _git(["init", "-q"], cwd=outer)
            _git(["config", "user.email", "test@example.com"], cwd=outer)
            _git(["config", "user.name", "Test"], cwd=outer)
            (outer / ".gitignore").write_text("marketplace/\n")
            (outer / "f.txt").write_text("1")
            _git(["add", ".gitignore", "f.txt"], cwd=outer)
            _git(["commit", "-q", "-m", "init"], cwd=outer)

            # The fake plugin-flattened viz package, living inside the
            # gitignored subdir -- mirrors the real deployed topology
            # (~/.engram/marketplace/plugins/engram/viz/): never committed,
            # so the outer repo does NOT track it.
            fake_pkg = outer / "marketplace" / "plugins" / "engram" / "viz"
            fake_pkg.mkdir(parents=True)
            (fake_pkg / "server.py").write_text("# fake server.py stub\n")
            (fake_pkg / "footer.py").write_text("# stand-in for footer's own __file__\n")

            (Path(engram_home) / ".deployed-version").write_text(
                "alpha_sha=deadbee\nalpha_dirty=clean\n"
            )

            saved_environ = dict(os.environ)
            os.environ["ENGRAM_HOME"] = engram_home
            try:
                with mock.patch.object(
                    viz_footer, "__file__", str(fake_pkg / "footer.py")
                ):
                    result = viz_footer._code_provenance()
            finally:
                os.environ.clear()
                os.environ.update(saved_environ)

            self.assertEqual(result, "deadbee (plugin)")
            self.assertNotIn("(worktree)", result)

    def test_tracked_checkout_renders_worktree_not_plugin(self):
        """Positive control: the SAME __file__-redirection mechanism, but
        against a genuinely tracked checkout -- must still resolve the
        worktree shape, never fall through to plugin/unversioned."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(["init", "-q"], cwd=repo)
            _git(["config", "user.email", "test@example.com"], cwd=repo)
            _git(["config", "user.name", "Test"], cwd=repo)
            (repo / "server.py").write_text("# stub\n")
            (repo / "footer.py").write_text("# stand-in for footer's own __file__\n")
            _git(["add", "-A"], cwd=repo)
            _git(["commit", "-q", "-m", "init"], cwd=repo)
            sha = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

            with mock.patch.object(
                viz_footer, "__file__", str(repo / "footer.py")
            ):
                result = viz_footer._code_provenance()

            self.assertEqual(result, f"{sha[:7]} (worktree)")
            self.assertNotIn("(plugin)", result)


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
