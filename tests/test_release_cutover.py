"""Tests for tools/release-cutover.py — hermetic, no network.

Coverage (per spec):
  1. minus-set parsing: comments/blank lines ignored; dir-entry removes subtree; glob matches.
  2. snapshot excludes untracked + gitignored files (each absent from output).
  3. minus-set application removes listed paths; non-matching entry warns, not fails.
  4. leak gate: roster-matching string in source → tool aborts; target untouched.
  5. happy path (no leaks): target gets exactly one new commit + tag; author == --author;
     minus-set paths absent; LICENSE present.
  6. re-run same version without --force → refuses (idempotence gate).

All tests use throwaway git repos built in tmp. No real network, no production ENGRAM DB.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "release-cutover.py"
SCAN_LEAKS_PATH = REPO_ROOT / "src" / "engram" / "tools" / "scan-leaks.py"

# Load release-cutover module (hyphen-free import via importlib).
_spec = importlib.util.spec_from_file_location("release_cutover", TOOL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

parse_minus_set = _mod._parse_minus_set
apply_minus_set = _mod._apply_minus_set


# ---------------------------------------------------------------------------
# Helpers for building throwaway git repos
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=True, **kwargs
    )


def _make_git_repo(path: Path) -> Path:
    """Init a bare-minimum git repo at path."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], cwd=str(path))
    _git(["config", "user.email", "test@test.local"], cwd=str(path))
    _git(["config", "user.name", "Test User"], cwd=str(path))
    return path


def _commit_file(repo: Path, relpath: str, content: str, msg: str = "add file") -> None:
    """Write a file, stage it, and commit."""
    p = repo / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(["add", relpath], cwd=str(repo))
    _git(["commit", "-m", msg], cwd=str(repo))


def _make_source_repo(tmp: Path, extra_files: dict[str, str] | None = None,
                      gitignore: str | None = None) -> Path:
    """Build a minimal valid source repo with LICENSE, README, and the minus-set file.

    The README contains the required disclaimer phrase so sanity gates pass by default.
    """
    src = tmp / "source"
    _make_git_repo(src)

    # Minimal LICENSE with a parseable copyright line.
    _commit_file(src, "LICENSE",
                 "Apache License\nVersion 2.0\n\nCopyright 2026 Test Author\n")

    # Minimal README with the required disclaimer phrase.
    _commit_file(src, "README.md",
                 "# ENGRAM\n\nThis is a personal project by Test Author.\n")

    # Minimal minus-set (empty — tests add their own if needed).
    _commit_file(src, "release-minus-set.txt",
                 "# minus-set\n")

    # Copy scan-leaks.py into the source repo (so the tool can call it).
    sl_dest = src / "tools" / "scan-leaks.py"
    sl_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(SCAN_LEAKS_PATH), str(sl_dest))
    _git(["add", "tools/scan-leaks.py"], cwd=str(src))
    _git(["commit", "-m", "add scan-leaks"], cwd=str(src))

    # Extra files requested by test.
    if extra_files:
        for relpath, content in extra_files.items():
            _commit_file(src, relpath, content)

    # .gitignore (untracked from git's perspective).
    if gitignore:
        (src / ".gitignore").write_text(gitignore)

    return src


def _make_target_repo(tmp: Path) -> Path:
    """Build a minimal empty target repo (clean working tree, one initial commit)."""
    tgt = tmp / "target"
    _make_git_repo(tgt)
    # Need at least one commit so git rm -rf . works later.
    _commit_file(tgt, ".gitkeep", "# placeholder\n", "init")
    return tgt


def _run_tool(args: list[str]) -> subprocess.CompletedProcess:
    """Run the tool as a subprocess and return the result (no check — tests inspect exit code)."""
    return subprocess.run(
        [sys.executable, str(TOOL_PATH)] + args,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# 1. Minus-set parsing
# ---------------------------------------------------------------------------

class TestMinusSetParsing:
    def test_comments_and_blank_lines_ignored(self, tmp_path):
        f = tmp_path / "minus-set.txt"
        f.write_text("# this is a comment\n\n  # another comment\nreal_entry\n")
        entries = parse_minus_set(str(f))
        assert entries == ["real_entry"]

    def test_multiple_entries_parsed(self, tmp_path):
        f = tmp_path / "minus-set.txt"
        f.write_text("CLAUDE.md\n.claude/\ntools/bot.py\n")
        entries = parse_minus_set(str(f))
        assert entries == ["CLAUDE.md", ".claude/", "tools/bot.py"]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "minus-set.txt"
        f.write_text("# only comments\n\n")
        entries = parse_minus_set(str(f))
        assert entries == []


class TestMinusSetApplication:
    def _make_tree(self, tmp_path: Path, files: dict[str, str]) -> Path:
        d = tmp_path / "snap"
        d.mkdir()
        for relpath, content in files.items():
            p = d / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return d

    def test_dir_entry_removes_subtree(self, tmp_path):
        snap = self._make_tree(tmp_path, {
            ".claude/settings.json": "{}",
            ".claude/skills/foo.md": "# skill",
            "server.py": "# server",
        })
        removed, warned = apply_minus_set(snap, [".claude/"])
        assert not (snap / ".claude").exists()
        assert (snap / "server.py").exists()
        assert len(removed) >= 1
        assert warned == []

    def test_exact_file_entry_removed(self, tmp_path):
        snap = self._make_tree(tmp_path, {
            "CLAUDE.md": "# dev",
            "README.md": "# public",
        })
        removed, warned = apply_minus_set(snap, ["CLAUDE.md"])
        assert not (snap / "CLAUDE.md").exists()
        assert (snap / "README.md").exists()
        assert "CLAUDE.md" in removed

    def test_glob_entry_removes_matches(self, tmp_path):
        snap = self._make_tree(tmp_path, {
            "tools/bot.py": "# bot",
            "tools/scan.py": "# scan",
            "src/main.py": "# main",
        })
        removed, warned = apply_minus_set(snap, ["tools/bot.py"])
        assert not (snap / "tools" / "bot.py").exists()
        assert (snap / "tools" / "scan.py").exists()

    def test_non_matching_entry_warns_not_fails(self, tmp_path):
        snap = self._make_tree(tmp_path, {"server.py": "# server"})
        removed, warned = apply_minus_set(snap, ["nonexistent_path.py"])
        assert "nonexistent_path.py" in warned
        assert removed == []


# ---------------------------------------------------------------------------
# 1b. Gitignore-style anchoring (#2516) — contract tests, written before the
#     implementation per CLAUDE.md's contract-first-tests convention.
#
#     Directory entries root-anchor only when they carry a non-trailing slash
#     (or a leading slash); a bare `name/` entry matches a directory named
#     `name` at ANY depth, matching the minus-set file's own documented
#     "gitignore-style globs" header. `*` never crosses a `/` boundary.
# ---------------------------------------------------------------------------

class TestMinusSetGitignoreAnchoring:
    def _make_tree(self, tmp_path: Path, files: dict[str, str]) -> Path:
        d = tmp_path / "snap"
        d.mkdir()
        for relpath, content in files.items():
            p = d / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return d

    def test_c1_unanchored_dir_entry_matches_any_depth(self, tmp_path):
        """C1: a `tests/` entry removes a nested a/b/tests/x.py AND a
        top-level tests/y.py."""
        snap = self._make_tree(tmp_path, {
            "a/b/tests/x.py": "# nested test fixture",
            "tests/y.py": "# top-level test",
        })
        removed, warned = apply_minus_set(snap, ["tests/"])
        assert not (snap / "a" / "b" / "tests").exists()
        assert not (snap / "tests").exists()
        assert warned == []

    def test_c2_anchored_dir_entry_matches_root_only(self, tmp_path):
        """C2: a `/deploy/` entry removes top-level deploy/z.sh but NOT
        src/forum/deploy/install.sh."""
        snap = self._make_tree(tmp_path, {
            "deploy/z.sh": "# top-level deploy",
            "src/forum/deploy/install.sh": "# nested deploy — must survive",
        })
        removed, warned = apply_minus_set(snap, ["/deploy/"])
        assert not (snap / "deploy").exists()
        assert (snap / "src" / "forum" / "deploy" / "install.sh").exists()

    def test_c3_sibling_of_nested_matched_dir_still_ships(self, tmp_path):
        """C3 positive control: with `tests/` in the list, a sibling file of
        the nested tests dir still ships."""
        snap = self._make_tree(tmp_path, {
            "src/forum/tests/x.py": "# nested test fixture",
            "src/forum/server.py": "# sibling — must survive",
        })
        removed, warned = apply_minus_set(snap, ["tests/"])
        assert not (snap / "src" / "forum" / "tests").exists()
        assert (snap / "src" / "forum" / "server.py").exists()

    def test_c4_anchored_file_entry_matches_root_only(self, tmp_path):
        """C4: an anchored file entry `tools/foo.py` removes top-level
        tools/foo.py but NOT src/engram/tools/foo.py."""
        snap = self._make_tree(tmp_path, {
            "tools/foo.py": "# top-level",
            "src/engram/tools/foo.py": "# nested — must survive",
        })
        removed, warned = apply_minus_set(snap, ["tools/foo.py"])
        assert not (snap / "tools" / "foo.py").exists()
        assert (snap / "src" / "engram" / "tools" / "foo.py").exists()

    def test_c5_bare_file_entry_matches_any_depth(self, tmp_path):
        """C5: a bare file entry `CLAUDE.md` removes both top-level and
        nested CLAUDE.md (unchanged behavior, pinned)."""
        snap = self._make_tree(tmp_path, {
            "CLAUDE.md": "# top-level",
            "sub/CLAUDE.md": "# nested",
        })
        removed, warned = apply_minus_set(snap, ["CLAUDE.md"])
        assert not (snap / "CLAUDE.md").exists()
        assert not (snap / "sub" / "CLAUDE.md").exists()

    def test_c6_dir_entry_never_removes_same_named_file(self, tmp_path):
        """C6: a dir entry never removes a same-named FILE (a `tests` file
        at root is not removed by `tests/`)."""
        snap = self._make_tree(tmp_path, {
            "tests": "# this is a FILE named tests, not a directory",
            "other.py": "# unrelated",
        })
        removed, warned = apply_minus_set(snap, ["tests/"])
        assert (snap / "tests").exists()
        assert (snap / "other.py").exists()

    def test_c7_glob_does_not_cross_slash(self, tmp_path):
        """C7: `*` does not cross `/`: an entry `docs/*.md` removes
        docs/a.md but not docs/sub/b.md."""
        snap = self._make_tree(tmp_path, {
            "docs/a.md": "# direct child — should be removed",
            "docs/sub/b.md": "# grandchild — must survive",
        })
        removed, warned = apply_minus_set(snap, ["docs/*.md"])
        assert not (snap / "docs" / "a.md").exists()
        assert (snap / "docs" / "sub" / "b.md").exists()

    def test_c8_entry_matching_nothing_warns(self, tmp_path):
        """C8: an entry matching nothing lands in `warned`."""
        snap = self._make_tree(tmp_path, {"server.py": "# server"})
        removed, warned = apply_minus_set(snap, ["nonexistent/"])
        assert "nonexistent/" in warned
        assert removed == []


# ---------------------------------------------------------------------------
# 1c. Symlink-in-removal-loop (PR #3164 review, round 1) — contract tests
#     written before the fix per CLAUDE.md's contract-first-tests convention.
#
#     A matched path that is itself a symlink (to a directory OR a file) must
#     be removed as a link (p.unlink()), never followed into via rmtree/read.
#     A matched real directory is still removed with rmtree; a matched real
#     file with unlink (unchanged). A broken symlink that matches is removed
#     without error. `is_symlink()` must be checked BEFORE `is_dir()`, since
#     `is_dir()` follows links.
# ---------------------------------------------------------------------------

class TestMinusSetSymlinkRemoval:
    def _make_tree(self, tmp_path: Path, files: dict[str, str]) -> Path:
        d = tmp_path / "snap"
        d.mkdir()
        for relpath, content in files.items():
            p = d / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return d

    def test_symlink_to_dir_matched_removed_as_link_not_rmtree(self, tmp_path):
        """A dir-entry match against a directory SYMLINK removes the link
        itself; the link's target directory and its contents survive.

        Regression test for shutil.rmtree(p) crashing with OSError when p is
        a symlink to a directory (reviewer-flagged, PR #3164 round 1 --
        `tools/engine` and `tools/engram-pkg` are real instances of this
        shape in the repo, and this PR's any-depth unanchored matching
        widens the reachable surface vs. the old root-only matching)."""
        snap = self._make_tree(tmp_path, {
            "real_target/file.txt": "# lives inside the real dir",
        })
        link = snap / "engine"
        link.symlink_to(snap / "real_target", target_is_directory=True)

        removed, warned = apply_minus_set(snap, ["engine/"])

        assert not link.is_symlink(), "the symlink itself must be gone"
        assert (snap / "real_target").is_dir(), "the link's target directory must survive"
        assert (snap / "real_target" / "file.txt").exists(), "the target's contents must survive"
        assert warned == []

    def test_broken_symlink_matched_removed_without_error(self, tmp_path):
        """A matched symlink whose target no longer exists (a broken/dangling
        symlink) is removed without raising."""
        snap = self._make_tree(tmp_path, {"other.py": "# unrelated"})
        link = snap / "dangling"
        link.symlink_to(snap / "does_not_exist")

        removed, warned = apply_minus_set(snap, ["dangling"])

        assert not link.is_symlink(), "the broken symlink must be removed"
        assert (snap / "other.py").exists()
        assert warned == []


# ---------------------------------------------------------------------------
# 2. Snapshot excludes untracked + gitignored files
# ---------------------------------------------------------------------------

class TestSnapshotExclusion:
    """The tool uses git-archive which inherently excludes untracked and gitignored files."""

    def test_untracked_and_gitignored_absent_from_snapshot(self, tmp_path):
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        # Write untracked file (not staged, not committed).
        (src / "untracked_secret.py").write_text("# untracked — must not appear\n")

        # Write gitignored file.
        (src / ".gitignore").write_text("ignored_file.py\n")
        (src / "ignored_file.py").write_text("# gitignored — must not appear\n")

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        # The sanity gate for README disclaimer will pass because our _make_source_repo
        # includes the phrase. If the tool exits 0, check the target.
        assert result.returncode == 0, (
            f"Tool failed unexpectedly:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Untracked and gitignored must NOT be in the target.
        assert not (tgt / "untracked_secret.py").exists()
        assert not (tgt / "ignored_file.py").exists()


# ---------------------------------------------------------------------------
# 3. Minus-set application removes paths in a real tool run
# ---------------------------------------------------------------------------

class TestMinusSetToolIntegration:
    def test_minus_set_paths_absent_in_target(self, tmp_path):
        src = _make_source_repo(tmp_path, extra_files={
            "CLAUDE.md": "# internal\n",
            "tools/telegram_bot.py": "# telegram\n",
        })
        tgt = _make_target_repo(tmp_path)

        # Write a custom minus-set that removes those files.
        minus_set = tmp_path / "my-minus-set.txt"
        minus_set.write_text("CLAUDE.md\ntools/telegram_bot.py\n")

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--minus-set", str(minus_set),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"Tool failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert not (tgt / "CLAUDE.md").exists(), "CLAUDE.md should be excluded"
        assert not (tgt / "tools" / "telegram_bot.py").exists(), "telegram_bot.py should be excluded"

    def test_stale_minus_set_entry_warns_not_fails(self, tmp_path):
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        minus_set = tmp_path / "stale-minus-set.txt"
        minus_set.write_text("tools/nonexistent_tool.py\n")

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--minus-set", str(minus_set),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"Tool failed on stale entry:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "WARN" in result.stdout and "nonexistent_tool.py" in result.stdout


# ---------------------------------------------------------------------------
# 4. Leak gate: roster-matching string causes abort + target untouched
# ---------------------------------------------------------------------------

class TestLeakGate:
    def test_leak_aborts_and_target_untouched(self, tmp_path):
        # Plant a file in source that contains the structural leak pattern.
        # scan-leaks COMMITTED_PATTERNS includes "kg_protocol_for_ai".
        src = _make_source_repo(tmp_path, extra_files={
            "tainted.py": "# path: kg_protocol_for_ai\n",
        })
        tgt = _make_target_repo(tmp_path)

        # Capture the current commit SHA in target before running.
        before_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])

        assert result.returncode != 0, (
            f"Tool should have aborted on leak but exited 0.\nstdout: {result.stdout}"
        )
        assert "abort" in result.stderr.lower() or "leak" in result.stderr.lower(), (
            f"Expected leak-abort message in stderr: {result.stderr!r}"
        )

        # Target must be untouched — same HEAD SHA.
        after_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert before_sha == after_sha, (
            "Target HEAD changed despite abort — tool must not modify target on leak."
        )


# ---------------------------------------------------------------------------
# 5. Happy path: one commit + tag, correct author, minus-set paths absent, LICENSE present
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_single_commit_and_tag(self, tmp_path):
        src = _make_source_repo(tmp_path, extra_files={
            "src/server.py": "# server\n",
        })
        tgt = _make_target_repo(tmp_path)

        # Record number of commits before.
        before_count = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip())

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"Tool failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Exactly one new commit.
        after_count = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip())
        assert after_count == before_count + 1, (
            f"Expected 1 new commit, got {after_count - before_count}"
        )

        # Tag v0.1.0 exists.
        tags = subprocess.run(
            ["git", "tag", "-l", "v0.1.0"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert "v0.1.0" in tags, f"Tag v0.1.0 not found in target. Tags: {tags!r}"

    def test_commit_author_matches_flag(self, tmp_path):
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"Tool failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Check the commit's author.
        log = subprocess.run(
            ["git", "log", "-1", "--format=%ae %an"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert "test@test.local" in log, f"Expected author email in commit log: {log!r}"
        assert "Test Author" in log, f"Expected author name in commit log: {log!r}"

    def test_license_present_in_target(self, tmp_path):
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"Tool failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert (tgt / "LICENSE").exists(), "LICENSE must be present in target after cutover"

    def test_minus_set_paths_absent_in_target_full(self, tmp_path):
        src = _make_source_repo(tmp_path, extra_files={
            "CLAUDE.md": "# dev\n",
        })
        tgt = _make_target_repo(tmp_path)
        minus_set = tmp_path / "ms.txt"
        minus_set.write_text("CLAUDE.md\n")

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--minus-set", str(minus_set),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"Tool failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert not (tgt / "CLAUDE.md").exists(), "CLAUDE.md must be excluded from target"


# ---------------------------------------------------------------------------
# 6. Idempotence: re-run same version without --force refuses
# ---------------------------------------------------------------------------

class TestIdempotence:
    def test_rerun_same_version_refuses(self, tmp_path):
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        # First run succeeds.
        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"First run failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Second run with same version, no --force → must refuse.
        result2 = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result2.returncode != 0, (
            "Second run with same version should refuse but exited 0."
        )
        combined = result2.stdout + result2.stderr
        assert "force" in combined.lower() or "already exists" in combined.lower(), (
            f"Expected 'force' or 'already exists' in output: {combined!r}"
        )

    def test_rerun_with_force_succeeds(self, tmp_path):
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        # First run.
        r1 = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert r1.returncode == 0

        # Second run with --force.
        r2 = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--force",
            "--author", "Test Author <test@test.local>",
        ])
        assert r2.returncode == 0, (
            f"Run with --force failed:\nstdout: {r2.stdout}\nstderr: {r2.stderr}"
        )

        # Tag still present.
        tags = subprocess.run(
            ["git", "tag", "-l", "v0.1.0"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert "v0.1.0" in tags


# ---------------------------------------------------------------------------
# 7. --force + planted leak: tag must survive leak abort (BLOCKER 2 ordering)
# ---------------------------------------------------------------------------

class TestForceLeakOrdering:
    def test_force_leak_aborts_and_tag_survives(self, tmp_path):
        """--force + a leaked file: the pre-existing tag must NOT be deleted.

        Proves the fail-closed ordering fix: the tag deletion was moved to
        AFTER all gates pass, so a leak-abort no longer leaves the tag gone.
        """
        # Step 1: Create a clean source and do a first run to establish the tag.
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        r1 = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert r1.returncode == 0, (
            f"Setup run failed:\nstdout: {r1.stdout}\nstderr: {r1.stderr}"
        )

        # Confirm the tag exists.
        tags_before = subprocess.run(
            ["git", "tag", "-l", "v0.1.0"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert "v0.1.0" in tags_before, "Setup run should have created v0.1.0 tag"

        # Step 2: Add a tainted file to the source so the next run will hit the leak gate.
        _commit_file(src, "tainted.py", "# path: kg_protocol_for_ai\n", "plant leak")

        # Step 3: Second run with --force — must abort at leak gate.
        r2 = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--force",
            "--author", "Test Author <test@test.local>",
        ])
        assert r2.returncode != 0, (
            f"Tool should have aborted on leak but exited 0.\nstdout: {r2.stdout}"
        )

        # The tag must STILL be present — the deletion must not have happened.
        tags_after = subprocess.run(
            ["git", "tag", "-l", "v0.1.0"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert "v0.1.0" in tags_after, (
            "Tag was deleted despite leak abort — fail-closed ordering is broken. "
            f"r2 stdout:\n{r2.stdout}\nr2 stderr:\n{r2.stderr}"
        )


# ---------------------------------------------------------------------------
# 8. Sanity gate aborts: LICENSE-missing + README-disclaimer-missing
# ---------------------------------------------------------------------------

class TestSanityGateAborts:
    def test_license_missing_aborts(self, tmp_path):
        """If the snapshot has no LICENSE file, the tool must abort before writing to target."""
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        # Add LICENSE to the minus-set so it is removed from the snapshot.
        minus_set = tmp_path / "ms-no-license.txt"
        minus_set.write_text("LICENSE\n")

        # Record target state before.
        before_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--minus-set", str(minus_set),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode != 0, (
            "Tool should have aborted when LICENSE is absent from snapshot."
        )
        assert "LICENSE" in result.stderr, (
            f"Expected 'LICENSE' in stderr: {result.stderr!r}"
        )

        # Target must be untouched.
        after_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert before_sha == after_sha, "Target HEAD changed despite LICENSE-missing abort."

    def test_readme_disclaimer_missing_aborts(self, tmp_path):
        """If the README lacks the required disclaimer phrase, the tool must abort."""
        # Build a source repo whose README does NOT contain the disclaimer phrase.
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        # Overwrite README.md without the disclaimer phrase.
        (src / "README.md").write_text("# ENGRAM\n\nNo disclaimer here.\n")
        _git(["add", "README.md"], cwd=str(src))
        _git(["commit", "-m", "remove disclaimer"], cwd=str(src))

        before_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode != 0, (
            "Tool should have aborted when README disclaimer is missing."
        )
        assert "disclaimer" in result.stderr.lower() or "README" in result.stderr, (
            f"Expected disclaimer-related error in stderr: {result.stderr!r}"
        )

        after_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert before_sha == after_sha, "Target HEAD changed despite README-disclaimer-missing abort."


# ---------------------------------------------------------------------------
# 9. source == target → abort (SAFETY 1)
# ---------------------------------------------------------------------------

class TestSourceEqualsTarget:
    def test_source_equals_target_aborts(self, tmp_path):
        """When source and target resolve to the same path the tool must abort immediately."""
        src = _make_source_repo(tmp_path)

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(src),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode != 0, (
            "Tool should abort when source == target, but exited 0."
        )
        combined = result.stdout + result.stderr
        assert "same" in combined.lower() or "source" in combined.lower(), (
            f"Expected error about source==target: {combined!r}"
        )

    def test_symlink_target_equals_source_aborts(self, tmp_path):
        """When --target is a symlink pointing to source, the tool must abort.

        os.path.samefile catches this via inode identity; a resolved-path string
        compare would miss it because the symlink path differs from the source path.
        """
        src = _make_source_repo(tmp_path)
        # Create a symlink that points at src — different path, same inode tree.
        symlink_target = tmp_path / "source_symlink"
        symlink_target.symlink_to(src)

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(symlink_target),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode != 0, (
            "Tool should abort when --target is a symlink pointing to source, but exited 0."
        )
        combined = result.stdout + result.stderr
        assert "same" in combined.lower() or "source" in combined.lower(), (
            f"Expected error about source==target via symlink: {combined!r}"
        )


# ---------------------------------------------------------------------------
# 10. Empty target (first-release case): no tracked files in target → succeeds
# ---------------------------------------------------------------------------

def _make_empty_target_repo(tmp: Path) -> Path:
    """Build a target git repo with NO tracked files — models a fresh public repo.

    Uses a single empty commit (git commit --allow-empty) so the repo has a HEAD
    but git ls-files returns nothing. This is the exact state of a newly created
    GitHub repo with no files pushed yet.
    """
    tgt = tmp / "empty_target"
    _make_git_repo(tgt)
    # One empty commit — HEAD exists but no tracked files.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "initial empty commit"],
        cwd=str(tgt), capture_output=True, text=True, check=True,
    )
    return tgt


class TestEmptyTarget:
    def test_first_release_empty_target_succeeds(self, tmp_path):
        """Regression test for the first-release / empty-target bug.

        When the target git repo has NO tracked files (fresh public repo), the
        old `git rm -rf .` errored with 'pathspec did not match any files' (exit 128).
        The fix uses --ignore-unmatch so the wipe is a no-op on an empty target.

        Asserts:
        - Tool exits 0.
        - Exactly one new commit is produced.
        - The version tag is present.
        - Snapshot files (e.g. LICENSE, README.md) are present in the target.
        - Minus-set paths are absent from the target.
        """
        src = _make_source_repo(tmp_path, extra_files={
            "CLAUDE.md": "# internal\n",
            "src/server.py": "# server\n",
        })
        tgt = _make_empty_target_repo(tmp_path)

        # Verify the target has no tracked files before the run.
        ls_before = subprocess.run(
            ["git", "ls-files"], cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert ls_before == "", (
            f"Test setup error: target should have no tracked files, got: {ls_before!r}"
        )

        # Record commit count before.
        before_count = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip())

        minus_set = tmp_path / "ms.txt"
        minus_set.write_text("CLAUDE.md\n")

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--minus-set", str(minus_set),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"Tool failed on empty target (first-release case):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Exactly one new commit.
        after_count = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip())
        assert after_count == before_count + 1, (
            f"Expected exactly 1 new commit on empty target, got {after_count - before_count}"
        )

        # Tag v0.1.0 present.
        tags = subprocess.run(
            ["git", "tag", "-l", "v0.1.0"],
            cwd=str(tgt), capture_output=True, text=True
        ).stdout.strip()
        assert "v0.1.0" in tags, f"Tag v0.1.0 not found in target after empty-target run. Tags: {tags!r}"

        # Snapshot files are present.
        assert (tgt / "LICENSE").exists(), "LICENSE must be present in target after empty-target cutover"
        assert (tgt / "README.md").exists(), "README.md must be present in target after empty-target cutover"
        assert (tgt / "src" / "server.py").exists(), "src/server.py must be present in target"

        # Minus-set path is absent.
        assert not (tgt / "CLAUDE.md").exists(), "CLAUDE.md (in minus-set) must be absent from target"


# ---------------------------------------------------------------------------
# 11. Symlink preservation: symlinks in source survive as symlinks in target
# ---------------------------------------------------------------------------

class TestSymlinkPreservation:
    def test_symlink_preserved_not_dereferenced(self, tmp_path):
        """A symlink in the source snapshot must land as a symlink in the target,
        NOT as a dereferenced real file.

        Regression test for the shutil.copytree/copy2 default-symlinks=False bug:
        the old code called shutil.copytree(item, dest) without symlinks=True,
        which dereferenced every symlink into a duplicate real file.  With 42
        tools/X -> ../src/engram/tools/X symlinks in the repo this shipped ~42
        duplicate copies and lost the symlink convention.
        """
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)

        # Add a real file and a symlink pointing at it, both committed.
        real_file = src / "lib" / "util.py"
        real_file.parent.mkdir(parents=True, exist_ok=True)
        real_file.write_text("# real implementation\n")
        _git(["add", "lib/util.py"], cwd=str(src))

        # tools/util.py -> ../lib/util.py  (mirrors the real repo's tools->src convention)
        tools_dir = src / "tools2"
        tools_dir.mkdir(parents=True, exist_ok=True)
        link = tools_dir / "util.py"
        link.symlink_to("../lib/util.py")
        _git(["add", "tools2/util.py"], cwd=str(src))
        _git(["commit", "-m", "add symlink"], cwd=str(src))

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(tgt),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])
        assert result.returncode == 0, (
            f"Tool failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        target_link = tgt / "tools2" / "util.py"
        # Must be a symlink — not a plain file.
        assert target_link.is_symlink(), (
            f"tools2/util.py should be a symlink in target but is a plain file "
            f"(dereferenced). This is the regression the fix addresses."
        )
        # Symlink target must be preserved verbatim.
        assert os.readlink(str(target_link)) == "../lib/util.py", (
            f"Symlink target mismatch: got {os.readlink(str(target_link))!r}, "
            f"expected '../lib/util.py'"
        )
        # The real file must also be present (both sides of the symlink).
        assert (tgt / "lib" / "util.py").exists(), (
            "lib/util.py (the symlink's target) must also be in the target"
        )


# ---------------------------------------------------------------------------
# 12. Target-is-a-subdirectory → abort (SAFETY: git-root assertion)
# ---------------------------------------------------------------------------

class TestTargetIsSubdirectory:
    def test_target_subdirectory_aborts(self, tmp_path):
        """When --target points at a SUBDIRECTORY of a git repo (not the root),
        the tool must abort before touching anything.

        Previously `git rev-parse --is-inside-work-tree` returned exit 0 for any
        path inside a work-tree, so a --target like /repo/src would pass the
        is-git-repo check and proceed to `git rm -rf .` inside that subdirectory.
        The new `--show-toplevel` root assertion catches this case.
        """
        src = _make_source_repo(tmp_path)

        # Build a real git repo, then point --target at a subdirectory of it.
        outer_repo = tmp_path / "outer_repo"
        _make_git_repo(outer_repo)
        _commit_file(outer_repo, ".gitkeep", "# placeholder\n", "init")

        # Create a subdirectory inside the outer repo (not committed — just exists).
        subdir = outer_repo / "subdir"
        subdir.mkdir()

        # Record outer_repo HEAD before the run.
        before_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(outer_repo),
            capture_output=True, text=True,
        ).stdout.strip()

        result = _run_tool([
            "--version", "v0.1.0",
            "--source", str(src),
            "--target", str(subdir),
            "--yes",
            "--author", "Test Author <test@test.local>",
        ])

        assert result.returncode != 0, (
            "Tool should abort when --target is a subdirectory of a git repo, "
            f"but exited 0.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "ROOT" in combined or "subdirectory" in combined.lower(), (
            f"Expected root-assertion error message: {combined!r}"
        )

        # The outer repo must be untouched.
        after_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(outer_repo),
            capture_output=True, text=True,
        ).stdout.strip()
        assert before_sha == after_sha, (
            "Outer repo HEAD changed despite subdirectory-target abort — target was mutated."
        )


# ---------------------------------------------------------------------------
# Definition-of-released: the --push summary prints the two remaining public steps
# ---------------------------------------------------------------------------

class TestFinalSummaryRemainingSteps:
    """Contract: --push summary prints a REMAINING block (before the CAVEAT lines);
    a local-only run prints a one-line pointer instead; the existing 'target tag'
    summary line stays in both. Driven end to end via subprocess, with a local
    bare repo as the target's origin so --push really pushes."""

    _AUTHOR = "Test Author <test@test.local>"

    def _run(self, tmp_path, push: bool):
        src = _make_source_repo(tmp_path)
        tgt = _make_target_repo(tmp_path)
        argv = ["--version", "v0.1.0", "--source", str(src), "--target", str(tgt),
                "--yes", "--author", self._AUTHOR]
        if push:
            bare = tmp_path / "origin.git"
            _git(["init", "--bare", str(bare)], cwd=str(tmp_path))
            _git(["remote", "add", "origin", str(bare)], cwd=str(tgt))
            argv.append("--push")
        result = _run_tool(argv)
        assert result.returncode == 0, (
            f"Tool failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return result.stdout

    def test_push_summary_prints_remaining_block(self, tmp_path):
        out = self._run(tmp_path, push=True)
        assert ('REMAINING — the release is NOT done until these are live '
                '(docs/RELEASING.md, "Announcing" + "Tell the public"):') in out
        assert ("  [ ] Website Versions entry, en + zh, live at "
                "https://engram-agents.org/versions  "
                "(owner: Ariadne; Zephyr may do zh)") in out
        assert ("  [ ] Newsletter sent to ENGRAM Communications (Buttondown), "
                "linking the live Versions entry and the Releases page  "
                "(owner: Ariadne)") in out
        assert ("Report the version as released only after all three; "
                "Releases entry first, then website, then newsletter.") in out
        lines = out.splitlines()
        rel = [i for i, l in enumerate(lines) if "[ ] Releases entry" in l]
        web = [i for i, l in enumerate(lines) if "[ ] Website" in l]
        assert rel and web and rel[0] < web[0]
        assert "owner: Ariadne" in out
        # Ordering: after the summary lines, before the CAVEAT.
        assert out.index("target tag") < out.index("REMAINING") < out.index("CAVEAT:")
        assert "After --push, three public steps remain" not in out

    def test_local_summary_prints_one_line_note_not_block(self, tmp_path):
        out = self._run(tmp_path, push=False)
        assert ('  After --push, three public steps remain (Releases entry, '
                'website entry, newsletter); see docs/RELEASING.md '
                '"Announcing" + "Tell the public".') in out
        assert "three public steps remain" in out
        assert "REMAINING" not in out
        assert "[ ] Website Versions entry" not in out

    @pytest.mark.parametrize("push", [True, False])
    def test_target_tag_summary_line_preserved(self, tmp_path, push):
        out = self._run(tmp_path, push=push)
        assert "  target tag   : v0.1.0 @ " in out
        assert "=== Final summary ===" in out
        assert "CAVEAT: scan-leaks is necessary-but-not-sufficient" in out
