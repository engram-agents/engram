"""Tests for engram-alpha#2531: the viz server's startup-captured version
report (``GET /api/version``).

Contract-first (per this repo's CLAUDE.md "Contract-first tests" convention):
written BEFORE ``viz/version_info.py`` and the ``/api/version`` route exist.
The contract, from the issue spec, verbatim:

1. Capture once at startup. Resolve the git root of the directory the
   ``viz`` package was imported from by walking parents (never a fixed
   ``parents[N]`` -- the plugin build flattens paths, see this repo's
   CLAUDE.md "Plugin build restructures code paths"); a worktree's ``.git``
   may be a FILE, not a directory. Capture ``sha`` (full), ``branch`` (or
   null when detached), ``dirty`` (bool, from ``git status --porcelain``),
   and ``started_at`` (ISO-8601 UTC). Log exactly one INFO line with all
   four.
2. ``GET /api/version`` returns JSON ``{sha, branch, dirty, started_at}``
   holding the values captured at startup -- it must NOT re-read git per
   request. Discriminating test: capture, then mutate the checkout (new
   commit + branch switch) in a temp repo, and the HTTP response is
   unchanged.
3. Warning, never refusal: if the branch is not ``dev``/``main``, or
   ``dirty`` is true, log a WARNING at startup and include a ``"warning"``
   string in the response. Startup still succeeds and the server still
   serves normally. Clean ``dev`` -> no ``"warning"`` key.
4. No git available (not a repo, git binary missing, or any subprocess
   error): startup still succeeds; ``/api/version`` returns 200 with
   ``sha: null, branch: null, dirty: null`` plus a ``"reason"`` string. Git
   calls use a short-timeout ``subprocess.run`` and never raise out.
5. ``/api/version`` needs no auth beyond other ``/api/*`` read endpoints --
   it is not agent-scoped (no ``?agent=`` handling), matching the
   ``/api/agents``/``/api/schema`` pattern, not the ``/api/graph``/
   ``/api/node`` per-agent pattern.

House style follows tests/test_viz_server_gzip_threading.py (real
HTTPServer/ThreadingHTTPServer + urlopen, no mocking of the HTTP layer
itself) and tests/test_deploy_tag_guard.py (real git-plumbing scratch repos
in tmp dirs, never mocked git).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents
from viz import server as viz_server
from viz import version_info as viz_version_info


# ---------------------------------------------------------------------------
# shared git helpers -- subprocess only, never shell=True (see
# tests/test_deploy_tag_guard.py / tests/test_health_version_gate.py for the
# same convention).
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo(path: Path, branch: str = "dev") -> None:
    _git(["init", "-q", "-b", branch], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "f.txt").write_text("1")
    # The tracking-check guard (engram-alpha#2531 reviewer round) only
    # accepts a found git root if it actually tracks the real server.py
    # living next to version_info.py -- i.e. `start.resolve() / "server.py"`
    # for whatever `start_path` capture_version_info() is called with.
    # Every test in this file uses a repo built by this helper AS that
    # start_path, so it must carry a genuinely committed (not merely
    # present-on-disk) server.py for the guard to accept it -- mirroring a
    # real tracked checkout rather than an installed-plugin copy.
    (path / "server.py").write_text("# stub for tracking-check tests\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "init"], path)


# ---------------------------------------------------------------------------
# Item 1 + 3 + 4 -- capture_version_info() itself (pure function, no HTTP)
# ---------------------------------------------------------------------------

class TestCaptureVersionInfo(unittest.TestCase):
    """Unit tests for viz.version_info.capture_version_info() -- exercised
    against real scratch git repos, never a mocked git."""

    def test_captures_sha_branch_dirty_started_at_on_clean_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")
            sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

            info = viz_version_info.capture_version_info(repo)

            self.assertEqual(info["sha"], sha)
            self.assertEqual(info["branch"], "dev")
            self.assertIs(info["dirty"], False)
            self.assertIn("started_at", info)
            self.assertNotIn("warning", info)  # clean dev -> no warning key
            self.assertNotIn("reason", info)

    def test_walks_parents_to_find_git_root_not_fixed_depth(self):
        """Never a fixed parents[N] -- start several levels below the git
        root and confirm the walk still finds it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")
            nested = repo / "a" / "b" / "c"
            nested.mkdir(parents=True)
            # start_path for this test IS `nested` -- the tracking-check
            # guard checks `nested / "server.py"`, so a server.py committed
            # only at the repo ROOT (via _init_repo) does not satisfy it;
            # this nested copy must be committed too.
            (nested / "server.py").write_text("# stub\n")
            _git(["add", "."], repo)
            _git(["commit", "-q", "-m", "nested server.py"], repo)

            info = viz_version_info.capture_version_info(nested)

            self.assertIsNotNone(info["sha"])
            self.assertEqual(info["branch"], "dev")

    def test_non_dev_non_main_branch_sets_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="feature/whatever")

            info = viz_version_info.capture_version_info(repo)

            self.assertIn("warning", info)
            self.assertIn("feature/whatever", info["warning"])

    def test_dirty_checkout_sets_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")
            (repo / "f.txt").write_text("dirty change")

            info = viz_version_info.capture_version_info(repo)

            self.assertIs(info["dirty"], True)
            self.assertIn("warning", info)

    def test_untracked_only_file_is_not_dirty(self):
        """PR #3174 contract item 4: capture_version_info()'s dirty check
        uses --untracked-files=no (unified with viz.footer's own prior
        convention, which this module's capture now feeds directly) -- a
        bare untracked file must NOT count as dirty; only a modified
        TRACKED file does (see test_dirty_checkout_sets_warning above)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")
            (repo / "untracked.txt").write_text("never added")

            info = viz_version_info.capture_version_info(repo)

            self.assertIs(info["dirty"], False)
            self.assertNotIn("warning", info)

    def test_clean_main_branch_no_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="main")

            info = viz_version_info.capture_version_info(repo)

            self.assertIs(info["dirty"], False)
            self.assertNotIn("warning", info)

    def test_detached_head_reports_branch_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")
            sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
            _git(["checkout", "-q", sha], repo)  # detach

            info = viz_version_info.capture_version_info(repo)

            self.assertIsNone(info["branch"])
            # detached (branch None, not dev/main) -> still a warning
            self.assertIn("warning", info)

    def test_not_a_git_checkout_returns_null_triple_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = viz_version_info.capture_version_info(Path(tmp))

            self.assertIsNone(info["sha"])
            self.assertIsNone(info["branch"])
            self.assertIsNone(info["dirty"])
            self.assertIn("reason", info)
            self.assertIn("started_at", info)
            self.assertNotIn("warning", info)

    def test_git_binary_missing_returns_null_triple_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            with mock.patch(
                "viz.version_info.subprocess.run",
                side_effect=FileNotFoundError("git binary not found"),
            ):
                info = viz_version_info.capture_version_info(repo)

            self.assertIsNone(info["sha"])
            self.assertIsNone(info["branch"])
            self.assertIsNone(info["dirty"])
            self.assertIn("reason", info)

    def test_git_subprocess_error_never_raises(self):
        """Any subprocess error (timeout, OSError, ...) must be swallowed --
        never propagate out of capture_version_info()."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            with mock.patch(
                "viz.version_info.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="git", timeout=3.0),
            ):
                info = viz_version_info.capture_version_info(repo)  # must not raise

            self.assertIsNone(info["sha"])
            self.assertIn("reason", info)

    def test_status_call_failure_sets_dirty_none_reason_and_warning(self):
        """engram-alpha#2531 follow-up: a failed `git status --porcelain`
        (sha + branch resolve fine) must NOT silently default dirty to
        False. Contract: dirty -> None, "reason" carries a
        status-call-failed string, and "dirty state unknown" is added to
        "warning" (so it isn't mistaken for a clean tree)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            original_run_git = viz_version_info._run_git

            def _fail_status_only(args, cwd):
                if args[:1] == ["status"]:
                    return None
                return original_run_git(args, cwd)

            with mock.patch(
                "viz.version_info._run_git", side_effect=_fail_status_only
            ):
                info = viz_version_info.capture_version_info(repo)

            self.assertIsNotNone(info["sha"])  # sha call untouched, still resolves
            self.assertEqual(info["branch"], "dev")  # branch call untouched
            self.assertIsNone(info["dirty"])
            self.assertIn("reason", info)
            self.assertIn("could not determine dirty state", info["reason"])
            self.assertIn("warning", info)
            self.assertIn("dirty state unknown", info["warning"])

    def test_branch_call_failure_sets_branch_none_and_reason(self):
        """A failed `git rev-parse --abbrev-ref HEAD` (sha + status resolve
        fine) must add a "reason" string distinguishing it from a genuine
        detached HEAD (which is branch=None with NO reason entry)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            original_run_git = viz_version_info._run_git

            def _fail_branch_only(args, cwd):
                if args[:2] == ["rev-parse", "--abbrev-ref"]:
                    return None
                return original_run_git(args, cwd)

            with mock.patch(
                "viz.version_info._run_git", side_effect=_fail_branch_only
            ):
                info = viz_version_info.capture_version_info(repo)

            self.assertIsNotNone(info["sha"])  # sha call untouched, still resolves
            self.assertIsNone(info["branch"])
            self.assertIs(info["dirty"], False)  # status call untouched
            self.assertIn("reason", info)
            self.assertIn("branch unknown", info["reason"])

    def test_branch_call_failure_warning_text_distinguishes_from_detached(self):
        """Reviewer round-1 Suggestion: when the branch LOOKUP itself
        failed (not a genuine detached HEAD), the human-facing "warning"
        string must say so distinctly -- "branch unknown (lookup failed)"
        -- not the generic "branch is None, not dev/main" text that a
        genuine detached HEAD (test_detached_head_reports_branch_null
        above) also produces. The "reason" field already gets this right;
        this test covers the "warning" field, which didn't."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            original_run_git = viz_version_info._run_git

            def _fail_branch_only(args, cwd):
                if args[:2] == ["rev-parse", "--abbrev-ref"]:
                    return None
                return original_run_git(args, cwd)

            with mock.patch(
                "viz.version_info._run_git", side_effect=_fail_branch_only
            ):
                info = viz_version_info.capture_version_info(repo)

            self.assertIn("warning", info)
            self.assertIn("branch unknown (lookup failed)", info["warning"])
            self.assertNotIn("branch is None, not dev/main", info["warning"])

    def test_detached_head_has_no_reason_unlike_branch_call_failure(self):
        """Control for the two tests above: a genuine detached HEAD (no git
        call failed) is branch=None with NO "reason" key at all -- the
        failure-vs-detached distinction is real, not just a label."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")
            sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
            _git(["checkout", "-q", sha], repo)  # detach

            info = viz_version_info.capture_version_info(repo)

            self.assertIsNone(info["branch"])
            self.assertNotIn("reason", info)

    def test_worktree_dot_git_as_a_file_is_still_found(self):
        """A git WORKTREE's .git is a FILE (containing 'gitdir: ...'), not a
        directory -- the root-walk must accept either (per the spec's
        explicit 'it may be a file (worktree) or a dir')."""
        with tempfile.TemporaryDirectory() as tmp:
            main_repo = Path(tmp) / "main"
            main_repo.mkdir()
            _init_repo(main_repo, branch="dev")

            worktree_dir = Path(tmp) / "wt"
            _git(["worktree", "add", "-q", "-b", "wt-branch", str(worktree_dir)],
                 main_repo)

            self.assertTrue((worktree_dir / ".git").is_file())

            info = viz_version_info.capture_version_info(worktree_dir)

            self.assertIsNotNone(info["sha"])
            self.assertEqual(info["branch"], "wt-branch")
            # Positive control for the tracking-check guard (#2531 reviewer
            # round) on the worktree layout specifically: server.py is
            # tracked (committed at main_repo init, checked out into the
            # worktree via the shared history), so the guard accepts this
            # root rather than falling into the not-tracked/null path.
            self.assertNotIn("reason", info)


# ---------------------------------------------------------------------------
# Reviewer round-1 Blocker: the unbounded parent walk must not accept an
# ancestor repo that merely ENCLOSES the running code without actually
# TRACKING it -- reproduced here as a real scratch-repo shape of the live
# defect (an outer repo with a `.gitignore` excluding a subdir, and a fake
# `viz`-package copy living inside that gitignored subdir; the real-world
# instance is `~/.engram` enclosing the plugin-flattened `viz/` tree under
# a gitignored `marketplace/`).
# ---------------------------------------------------------------------------

class TestUntrackedAncestorRepoRejected(unittest.TestCase):
    """Contract-first (per this repo's CLAUDE.md): written BEFORE the
    tracking-check guard exists. test_untracked_ancestor_repo_rejected...
    must be RED on a03662bc (the unguarded code happily returns the outer
    repo's own sha/branch/dirty); test_tracked_ancestor_repo_is_accepted
    is the positive control that the guard doesn't reject a legitimate
    checkout."""

    @staticmethod
    def _make_outer_with_gitignored_subdir(tmp: Path, *, track_file: bool):
        outer = tmp / "outer"
        outer.mkdir()
        _git(["init", "-q", "-b", "dev"], outer)
        _git(["config", "user.email", "test@example.com"], outer)
        _git(["config", "user.name", "Test"], outer)
        (outer / ".gitignore").write_text("marketplace/\n")
        (outer / "f.txt").write_text("1")
        _git(["add", ".gitignore", "f.txt"], outer)
        _git(["commit", "-q", "-m", "init"], outer)

        # The fake plugin-flattened viz package, living inside the
        # gitignored subdir -- mirrors the real deployed topology
        # (~/.engram/marketplace/plugins/engram/viz/), never committed by
        # default because of the outer repo's own .gitignore.
        fake_pkg = outer / "marketplace" / "plugins" / "engram" / "viz"
        fake_pkg.mkdir(parents=True)
        (fake_pkg / "server.py").write_text("# fake server.py stub\n")

        if track_file:
            # -f: force-add past .gitignore -- the positive control, where
            # the file genuinely IS tracked by the outer repo.
            _git(["add", "-f", str(fake_pkg / "server.py")], outer)
            _git(["commit", "-q", "-m", "track the fake server.py"], outer)

        return outer, fake_pkg

    def test_untracked_ancestor_repo_rejected_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer, fake_pkg = self._make_outer_with_gitignored_subdir(
                Path(tmp), track_file=False
            )

            info = viz_version_info.capture_version_info(fake_pkg)

            self.assertIsNone(info["sha"])
            self.assertIsNone(info["branch"])
            self.assertIsNone(info["dirty"])
            self.assertIn("reason", info)
            self.assertIn("not tracked", info["reason"])
            self.assertNotIn("warning", info)

    def test_tracked_ancestor_repo_is_accepted(self):
        """Positive control: the SAME layout, but the file genuinely IS
        tracked in the outer repo -- the guard must not reject a real,
        legitimate checkout just because the root it finds sits several
        levels above where the code physically lives."""
        with tempfile.TemporaryDirectory() as tmp:
            outer, fake_pkg = self._make_outer_with_gitignored_subdir(
                Path(tmp), track_file=True
            )
            outer_sha = _git(["rev-parse", "HEAD"], outer).stdout.strip()

            info = viz_version_info.capture_version_info(fake_pkg)

            self.assertEqual(info["sha"], outer_sha)
            self.assertEqual(info["branch"], "dev")
            self.assertNotIn("reason", info)


# ---------------------------------------------------------------------------
# git ownership protection (PR #3174 second half): `_run_git` no longer
# bypasses git's dubious-ownership refusal via `-c safe.directory=...` (nor
# the `core.fsmonitor` / `core.hooksPath` neutralizers that bypass required
# as its own companion) -- see version_info.py's module + `_run_git`
# docstrings for why. Round 4's own safe.directory override opened a
# config-driven-exec path the neutralizer list did not close: a repo-local
# `filter.<x>.clean` script fires under `git status --porcelain
# --untracked-files=no` even with `core.fsmonitor=false` and
# `core.hooksPath=/dev/null` both set (measured on this host's git 2.43.0).
# The production viz server's own git calls already work under its real
# user without any override -- that user's gitconfig evidently already
# permits the served checkout -- so the right fix is to stop bypassing
# git's ownership protection rather than patch each exec surface it opens.
#
# These tests simulate a cross-user checkout via git's own test-only hook,
# `GIT_TEST_ASSUME_DIFFERENT_OWNER=1` (verified directly against this
# host's git 2.43.0 -- a bare `git status --porcelain` under it exits 128
# with "fatal: detected dubious ownership" on stderr), combined with HOME
# pointed at an empty temp dir so no global safe.directory entry masks the
# effect.
#
# Contract-first (per this repo's CLAUDE.md "Contract-first tests"
# convention): rewritten BEFORE the bypass-removal fix landed, against
# 177ba143 (this PR's pre-fix tip, which still carries the `-c
# safe.directory=...` override) -- must be RED there (the override there
# makes capture SUCCEED under simulated different ownership instead of
# refusing).
# ---------------------------------------------------------------------------

class TestSafeDirectoryOwnershipBypass(unittest.TestCase):

    def test_capture_returns_dubious_ownership_under_simulated_different_ownership(self):
        """Contract item 1 (behavioral): with the safe.directory bypass
        removed, capture_version_info() must return the dubious-ownership
        null shape when git refuses the repo on ownership grounds -- never
        silently succeed by overriding the check. Must be RED on 177ba143
        (pre-fix), whose `_run_git` still passes `-c safe.directory=...`
        and so resolves the real sha here instead."""
        with tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as empty_home:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            with mock.patch.dict(
                os.environ,
                {"HOME": empty_home, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
            ):
                info = viz_version_info.capture_version_info(repo)

            self.assertIsNone(info["sha"])
            self.assertIsNone(info["branch"])
            self.assertIsNone(info["dirty"])
            self.assertIn("reason", info)
            self.assertIn("dubious ownership", info["reason"])
            self.assertIn("safe.directory", info["reason"])

    def test_no_repo_config_exec_surface_fires_under_simulated_different_ownership(self):
        """Contract item 2 (now structural, not a neutralizer list): with
        the safe.directory bypass removed entirely, git's own ownership
        refusal fires BEFORE any repo-local config is ever read -- so
        NEITHER a `core.fsmonitor` hook NOR a `filter.<x>.clean` driver can
        run, regardless of what either is configured to do. Covers BOTH
        exec surfaces in one repo (a fsmonitor hook script, plus a
        clean-filter script wired via .gitattributes to a tracked file),
        which is stronger than checking either alone -- a fix that merely
        swapped one neutralizer for another (rather than removing the
        bypass) could still pass a fsmonitor-only check while leaving the
        clean-filter surface open (this is exactly what round 4's
        `-c safe.directory=... -c core.fsmonitor=false -c
        core.hooksPath=/dev/null` combination did). Must be RED on 177ba143
        (pre-fix): its safe.directory override lets git read repo config at
        all, and the clean-filter marker is measured to appear there even
        with the fsmonitor/hooksPath neutralizers in place."""
        with tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as marker_dir, \
             tempfile.TemporaryDirectory() as empty_home:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            fsmonitor_marker = Path(marker_dir) / "fsmonitor-ran"
            fsmonitor_script = repo / "fsmonitor-hook.sh"
            fsmonitor_script.write_text(
                "#!/bin/bash\n"
                f"echo ran > {fsmonitor_marker}\n"
                "echo '{\"version\":2}'\n"
                "exit 0\n"
            )
            fsmonitor_script.chmod(0o755)
            _git(["config", "core.fsmonitor", str(fsmonitor_script)], repo)

            clean_marker = Path(marker_dir) / "clean-filter-ran"
            clean_script = repo / "clean-filter.sh"
            clean_script.write_text(
                "#!/bin/bash\n"
                f"echo ran > {clean_marker}\n"
                "cat\n"
            )
            clean_script.chmod(0o755)
            (repo / ".gitattributes").write_text("tracked.txt filter=x\n")
            (repo / "tracked.txt").write_text("hello\n")
            _git(["config", "filter.x.clean", str(clean_script)], repo)
            _git(["add", "."], repo)
            _git(["commit", "-q", "-m", "wire filters"], repo)

            # Touch the tracked file (content rewrite -> new mtime) so a
            # clean-filter re-check has something to fire against on the
            # next `status` -- mirrors the round-4 measurement, where an
            # identical-content rewrite was enough to trigger it.
            (repo / "tracked.txt").write_text("hello\n")

            # Both scripts legitimately ran already, as the REAL owner,
            # during the setup `config`/`add`/`commit` calls above (e.g.
            # `commit` reads status to decide what changed, which can
            # invoke `core.fsmonitor`). Reset both markers here so what
            # follows measures only the actual `capture_version_info()`
            # call under simulated different ownership, not the setup.
            if fsmonitor_marker.exists():
                fsmonitor_marker.unlink()
            if clean_marker.exists():
                clean_marker.unlink()

            with mock.patch.dict(
                os.environ,
                {"HOME": empty_home, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
            ):
                viz_version_info.capture_version_info(repo)

            self.assertFalse(
                fsmonitor_marker.exists(),
                "core.fsmonitor ran despite git's ownership refusal -- the "
                "safe.directory bypass was not fully removed",
            )
            self.assertFalse(
                clean_marker.exists(),
                "filter.x.clean ran despite git's ownership refusal -- the "
                "safe.directory bypass was not fully removed",
            )

    def test_dubious_ownership_stderr_yields_specific_reason(self):
        """Contract item 3: when ownership still blocks despite
        safe.directory (e.g. an older git that ignores the command-line
        override), the result must carry a reason DISTINCT from the
        generic "git command failed" text. Simulated via monkeypatching
        `_run_git` to return a CompletedProcess carrying the real stderr
        shape -- per the spec, "`_run_git` or its caller needs access to
        stderr to do it"."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            dubious_proc = subprocess.CompletedProcess(
                args=["git", "ls-files"], returncode=128, stdout="",
                stderr=(
                    "fatal: detected dubious ownership in repository at "
                    f"'{repo}'\n"
                ),
            )
            with mock.patch(
                "viz.version_info._run_git", return_value=dubious_proc
            ):
                info = viz_version_info.capture_version_info(repo)

            self.assertIsNone(info["sha"])
            self.assertIsNone(info["branch"])
            self.assertIsNone(info["dirty"])
            self.assertIn("reason", info)
            self.assertIn("dubious ownership", info["reason"])
            self.assertIn("safe.directory", info["reason"])
            self.assertNotIn("git command failed", info["reason"])


# ---------------------------------------------------------------------------
# Item 1 -- startup logging (exactly one INFO line; WARNING iff it fired)
# ---------------------------------------------------------------------------

class TestStartupCaptureLogging(unittest.TestCase):
    """viz.server's startup-capture wrapper must log exactly one INFO line
    carrying all four fields, plus a WARNING line iff capture_version_info()
    returned a "warning" key."""

    def test_emits_exactly_one_info_line_with_all_four_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            with self.assertLogs("viz.server.version", level="INFO") as cm:
                info = viz_server._capture_and_log_version_info(repo)

            info_lines = [line for line in cm.output if line.startswith("INFO:")]
            self.assertEqual(len(info_lines), 1)
            line = info_lines[0]
            for key in ("sha", "branch", "dirty", "started_at"):
                self.assertIn(key, line)
            self.assertEqual(info["branch"], "dev")

    def test_warning_logged_when_branch_not_dev_or_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="topic")

            with self.assertLogs("viz.server.version", level="INFO") as cm:
                viz_server._capture_and_log_version_info(repo)

            self.assertTrue(any(line.startswith("WARNING:") for line in cm.output))

    def test_no_warning_logged_when_clean_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")

            with self.assertLogs("viz.server.version", level="INFO") as cm:
                viz_server._capture_and_log_version_info(repo)

            self.assertFalse(any(line.startswith("WARNING:") for line in cm.output))

    def test_startup_succeeds_even_with_no_git_available(self):
        """Warning, never refusal -- and the no-git case logs no WARNING
        (there is nothing to warn about branch/dirty when git isn't
        available; contract item 4 covers this case separately from item 3)."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertLogs("viz.server.version", level="INFO") as cm:
                info = viz_server._capture_and_log_version_info(Path(tmp))

            self.assertIsNone(info["sha"])
            self.assertIn("reason", info)
            self.assertTrue(any(line.startswith("INFO:") for line in cm.output))


# ---------------------------------------------------------------------------
# Item 2 + 3 + 4 + 5 -- the actual HTTP endpoint
# ---------------------------------------------------------------------------

class TestVersionEndpoint(unittest.TestCase):
    """GET /api/version -- real ThreadingHTTPServer + urlopen, house style
    per tests/test_viz_server_gzip_threading.py."""

    @classmethod
    def setUpClass(cls):
        cls._saved_agents = viz_agents.AGENTS
        cls._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "test_agent": {"name": "test_agent", "label": "Test",
                           "db": "/nonexistent/knowledge.db"}
        }
        viz_agents.DEFAULT_AGENT = "test_agent"

    @classmethod
    def tearDownClass(cls):
        viz_agents.AGENTS = cls._saved_agents
        viz_agents.DEFAULT_AGENT = cls._saved_default

    def setUp(self):
        self._saved_version_info = viz_server._VERSION_INFO
        self._saved_version_dir = viz_server._VERSION_INFO_DIR

    def tearDown(self):
        viz_server._VERSION_INFO = self._saved_version_info
        viz_server._VERSION_INFO_DIR = self._saved_version_dir

    @staticmethod
    def _start_server():
        server = ThreadingHTTPServer(("127.0.0.1", 0), viz_server.Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        return server, port

    def _get(self, port: int, path: str = "/api/version"):
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp, json.loads(resp.read())

    def test_response_unchanged_after_checkout_mutates(self):
        """The discriminating test named in the spec: capture once, then
        mutate the checkout (new commit + branch switch) in a temp repo --
        the HTTP response must be unchanged. A plausible-wrong
        implementation that re-reads git per request goes red here (see the
        coder-fairy handoff for the mutation + its measured effect)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="dev")
            captured = viz_version_info.capture_version_info(repo)
            viz_server._VERSION_INFO = captured
            viz_server._VERSION_INFO_DIR = repo

            server, port = self._start_server()
            try:
                _, body1 = self._get(port)
                self.assertEqual(body1["sha"], captured["sha"])
                self.assertEqual(body1["branch"], captured["branch"])
                self.assertEqual(body1["dirty"], captured["dirty"])

                # Mutate the checkout: new commit + branch switch.
                (repo / "new.txt").write_text("x")
                _git(["add", "."], repo)
                _git(["commit", "-q", "-m", "second"], repo)
                _git(["checkout", "-q", "-b", "some-other-branch"], repo)

                # Sanity check: re-capturing directly from the SAME
                # directory WOULD see the change -- proves the mutation
                # actually took effect and this isn't a vacuous test.
                recaptured = viz_version_info.capture_version_info(repo)
                self.assertNotEqual(recaptured["sha"], captured["sha"])
                self.assertNotEqual(recaptured["branch"], captured["branch"])

                # The endpoint's own response must be UNCHANGED.
                _, body2 = self._get(port)
                self.assertEqual(body2, body1)
            finally:
                server.shutdown()
                server.server_close()

    def test_response_contains_warning_key_when_captured_with_warning(self):
        captured = {
            "sha": "deadbeef", "branch": "feature/x", "dirty": False,
            "started_at": "2026-01-01T00:00:00+00:00",
            "warning": "branch is 'feature/x', not dev/main",
        }
        viz_server._VERSION_INFO = captured
        server, port = self._start_server()
        try:
            _, body = self._get(port)
            self.assertEqual(body["warning"], captured["warning"])
        finally:
            server.shutdown()
            server.server_close()

    def test_response_has_no_warning_key_when_clean_dev(self):
        captured = {
            "sha": "deadbeef", "branch": "dev", "dirty": False,
            "started_at": "2026-01-01T00:00:00+00:00",
        }
        viz_server._VERSION_INFO = captured
        server, port = self._start_server()
        try:
            _, body = self._get(port)
            self.assertNotIn("warning", body)
        finally:
            server.shutdown()
            server.server_close()

    def test_response_null_triple_and_reason_when_no_git_available(self):
        captured = {
            "sha": None, "branch": None, "dirty": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "reason": "not a git checkout",
        }
        viz_server._VERSION_INFO = captured
        server, port = self._start_server()
        try:
            resp, body = self._get(port)
            self.assertEqual(resp.status, 200)
            self.assertIsNone(body["sha"])
            self.assertIsNone(body["branch"])
            self.assertIsNone(body["dirty"])
            self.assertEqual(body["reason"], captured["reason"])
        finally:
            server.shutdown()
            server.server_close()

    def test_endpoint_needs_no_agent_param_like_api_agents(self):
        """Not agent-scoped -- no ?agent= handling, matching /api/agents and
        /api/schema (contract item 5), not the /api/graph, /api/node
        per-agent pattern."""
        captured = {
            "sha": "deadbeef", "branch": "dev", "dirty": False,
            "started_at": "2026-01-01T00:00:00+00:00",
        }
        viz_server._VERSION_INFO = captured
        server, port = self._start_server()
        try:
            resp, body = self._get(port, "/api/version")
            self.assertEqual(resp.status, 200)
            self.assertEqual(body["sha"], "deadbeef")
            # Passing an unrelated/unknown ?agent= must not change the
            # response or error out -- the endpoint ignores it entirely.
            resp2, body2 = self._get(port, "/api/version?agent=nonexistent")
            self.assertEqual(resp2.status, 200)
            self.assertEqual(body2, body)
        finally:
            server.shutdown()
            server.server_close()

    def test_response_plugin_shape_includes_plugin_block(self):
        """PR #3174 contract item 3: when capture_version_info() is null
        because the code isn't tracked by any enclosing repo (or there's
        no checkout at all), and .deployed-version has a real alpha_sha,
        /api/version's response additionally carries a
        "plugin": {"alpha_sha": ..., "alpha_dirty": ...} block -- reusing
        footer's existing .deployed-version reader (moved to
        version_info.read_deployed_version, see this PR). The capture's
        own "reason" is kept exactly as returned, not replaced."""
        with tempfile.TemporaryDirectory() as tmp, \
             tempfile.TemporaryDirectory() as engram_home:
            # tmp has no .git anywhere above it -> "not a git checkout".
            (Path(engram_home) / ".deployed-version").write_text(
                "alpha_sha=beefcaf\nalpha_dirty=dirty\n"
            )
            saved_environ = dict(os.environ)
            os.environ["ENGRAM_HOME"] = engram_home
            try:
                captured = viz_server._capture_and_log_version_info(Path(tmp))
            finally:
                os.environ.clear()
                os.environ.update(saved_environ)

            self.assertIsNone(captured["sha"])
            self.assertEqual(captured["reason"], "not a git checkout")
            self.assertIn("plugin", captured)
            self.assertEqual(captured["plugin"]["alpha_sha"], "beefcaf")
            self.assertEqual(captured["plugin"]["alpha_dirty"], "dirty")

            viz_server._VERSION_INFO = captured
            server, port = self._start_server()
            try:
                _, body = self._get(port)
                self.assertIn("plugin", body)
                self.assertEqual(body["plugin"]["alpha_sha"], "beefcaf")
                self.assertEqual(body["plugin"]["alpha_dirty"], "dirty")
                self.assertEqual(body["reason"], "not a git checkout")
            finally:
                server.shutdown()
                server.server_close()

    def test_response_no_plugin_block_when_reason_is_git_failure(self):
        """Control for the case above: a null sha from a GIT FAILURE
        reason (dubious ownership / git-command-failed), as opposed to
        genuinely no tracking checkout, must NOT gain a "plugin" block --
        version_info.is_plugin_fallback_reason() is False for those two
        reasons, per its own docstring: a checkout genuinely exists but
        git couldn't read it, so silently substituting a possibly-stale
        plugin marker would misreport this run's actual code provenance."""
        captured = {
            "sha": None, "branch": None, "dirty": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "reason": "git command failed (missing binary, timeout, or error)",
        }
        viz_server._VERSION_INFO = captured
        server, port = self._start_server()
        try:
            _, body = self._get(port)
            self.assertNotIn("plugin", body)
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# Startup banner visibility (follow-up to #2531): _VERSION_LOGGER.info(...)
# is silently dropped in production -- viz.server never configures Python
# logging (no basicConfig/handlers), so the logger calls in
# _capture_and_log_version_info above are invisible on stdout/journal.
# Contract-first (per this repo's CLAUDE.md "Contract-first tests"
# convention): written BEFORE the ``_version_banner_lines`` helper exists,
# against a spec stated verbatim in the task:
#
#   A person reading the service's stdout/journal must see the loaded sha,
#   branch and dirty flag on every start. If a "warning" is present, it
#   must appear there too.
#   - one line: "Loaded commit     ->  <sha or 'unknown (<reason>)'> on
#     <branch or 'detached'>", followed by ", dirty" when dirty.
#   - if _VERSION_INFO has a "warning", a second line:
#     "[warn] version warning: <warning>".
#
# The logger calls themselves are untouched (harmless, kept for anyone who
# DOES configure logging) -- this is purely about main()'s own stdout
# banner, which is the server's actual, always-visible startup output.
# ---------------------------------------------------------------------------

class TestVersionBannerLines(unittest.TestCase):
    """Unit tests for viz.server._version_banner_lines(info) -- the helper
    factored out of main() so the banner content is directly testable
    without spinning up argparse/HTTPServer. Cover: a normal capture, a
    capture with a warning, and a null capture with a reason (the three
    cases named in the spec)."""

    def test_normal_capture_produces_single_loaded_commit_line(self):
        info = {
            "sha": "abcd1234567890", "branch": "dev", "dirty": False,
            "started_at": "2026-01-01T00:00:00+00:00",
        }
        lines = viz_server._version_banner_lines(info)

        self.assertEqual(len(lines), 1)  # no warning key -> exactly one line
        self.assertIn("abcd1234567890", lines[0])
        self.assertIn("dev", lines[0])
        self.assertNotIn(", dirty", lines[0])
        self.assertIn("Loaded commit", lines[0])

    def test_dirty_true_appends_dirty_suffix(self):
        info = {
            "sha": "abcd1234567890", "branch": "dev", "dirty": True,
            "started_at": "2026-01-01T00:00:00+00:00",
        }
        lines = viz_server._version_banner_lines(info)

        self.assertIn(", dirty", lines[0])

    def test_warning_present_adds_a_second_line(self):
        info = {
            "sha": "abcd1234567890", "branch": "feature/x", "dirty": False,
            "started_at": "2026-01-01T00:00:00+00:00",
            "warning": "branch is 'feature/x', not dev/main",
        }
        lines = viz_server._version_banner_lines(info)

        self.assertEqual(len(lines), 2)
        self.assertIn("version warning", lines[1])
        self.assertIn("branch is 'feature/x', not dev/main", lines[1])

    def test_null_capture_with_reason_shows_unknown_and_reason(self):
        info = {
            "sha": None, "branch": None, "dirty": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "reason": "not a git checkout",
        }
        lines = viz_server._version_banner_lines(info)

        self.assertEqual(len(lines), 1)  # no "warning" key -> one line
        self.assertIn("unknown", lines[0])
        self.assertIn("not a git checkout", lines[0])
        self.assertIn("detached", lines[0])  # branch None -> "detached" per spec
        self.assertNotIn(", dirty", lines[0])  # dirty is None, not True

    def test_main_prints_banner_lines_to_stdout(self):
        """Integration: main() itself must actually print these lines (not
        just the helper existing in isolation) -- this is the real defect
        the spec names (the logger calls are dropped, the print() banner is
        the server's actual visible output)."""
        import io
        import sys as _sys
        from unittest.mock import MagicMock, patch

        mock_server_instance = MagicMock()
        mock_server_instance.serve_forever.side_effect = KeyboardInterrupt()

        captured = io.StringIO()
        with patch.object(_sys, "argv", ["viz_server", "--no-autodetect"]):
            with patch("viz.server.ThreadingHTTPServer",
                       return_value=mock_server_instance):
                with mock.patch.object(
                    viz_server, "_VERSION_INFO",
                    {"sha": "cafef00d", "branch": "dev", "dirty": False,
                     "started_at": "2026-01-01T00:00:00+00:00"},
                ):
                    with mock.patch.object(_sys, "stdout", captured):
                        try:
                            viz_server.main()
                        except KeyboardInterrupt:
                            pass

        output = captured.getvalue()
        self.assertIn("cafef00d", output)
        self.assertIn("Loaded commit", output)


if __name__ == "__main__":
    unittest.main()
