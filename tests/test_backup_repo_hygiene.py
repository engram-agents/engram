"""Tests for the backup-repo hygiene fix (db-backup/, claude-projects-archive/,
session-logs-archive/, session_log.md).

Coverage:

  1. New .gitignore patterns actually match their target paths.
  2. _untrack_gitignored_paths removes tracked paths from the git index while
     leaving the file on disk (the critical assertion — this is a backup dir,
     deleting local copies would be destructive and unrecoverable).
  3. _untrack_gitignored_paths is idempotent (second call is a no-op).
  4. _untrack_gitignored_paths skips cleanly when the directory is not a git repo.
  5. _maybe_run_daily_git_gc: the date-stamp prevents a second same-day run.
  6. _maybe_run_daily_git_gc: skips cleanly when the directory is not a git repo.
  7. _ensure_engram_gitignore remains idempotent with the new patterns present,
     and still no-ops when all patterns are already present.
  8. _ensure_engram_gitignore preserves a hand-added negation line (e.g.
     `!diary/diary.db`) across a refresh forced by the new patterns, instead of
     silently dropping it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


NEW_GITIGNORE_PATTERNS = [
    "db-backup/",
    "claude-projects-archive/",
    "session-logs-archive/",
    "transcript-archive/",
    "session_log.md",
]


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run_git("init", cwd=repo_dir)
    _run_git("config", "user.email", "test@test.com", cwd=repo_dir)
    _run_git("config", "user.name", "Test", cwd=repo_dir)


def _commit_all(repo_dir: Path, message: str = "commit") -> None:
    _run_git("add", "-A", cwd=repo_dir)
    _run_git("commit", "-m", message, cwd=repo_dir)


def _tracked_paths(repo_dir: Path) -> set[str]:
    # -z: NUL-terminated, unquoted raw paths — git's default ls-files output
    # C-quotes paths containing a double quote (unlike git rm's own message,
    # which doesn't), so plain-`ls-files` parsing would itself mis-assert on
    # the quoted-path fixtures used by the accurate-count regression test.
    r = _run_git("ls-files", "-z", cwd=repo_dir)
    return {p for p in r.stdout.split("\0") if p}


def _fresh_server_module(tmp: Path):
    """Reload server/engram_* modules and point them at tmp, matching the
    convention used throughout tests/test_git_backup_embeddings.py."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    import server
    server._configure_paths(tmp)
    return server


# ---------------------------------------------------------------------------
# Area 1: new .gitignore patterns match their target paths
# ---------------------------------------------------------------------------

class TestNewGitignorePatternsMatch:
    """Each new canonical pattern actually ignores its intended target."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_hygiene_patterns_"))
        self.server = _fresh_server_module(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_four_new_patterns_present_after_write(self):
        self.server._ensure_engram_gitignore(self.tmp)
        content = (self.tmp / ".gitignore").read_text(encoding="utf-8")
        lines = {l.strip() for l in content.splitlines()}
        missing = [p for p in NEW_GITIGNORE_PATTERNS if p not in lines]
        assert not missing, f".gitignore missing new patterns: {missing}\nFound: {sorted(lines)}"

    @pytest.mark.parametrize(
        "target",
        [
            "db-backup/knowledge-20260101.sql",
            "claude-projects-archive/some-session.jsonl",
            "session-logs-archive/session_log-2026-01-01.md",
            "transcript-archive/transcripts-20260101T000000Z-001.xz",
            "session_log.md",
        ],
    )
    def test_pattern_actually_ignores_target(self, target):
        self.server._ensure_engram_gitignore(self.tmp)
        _init_repo(self.tmp)
        target_path = self.tmp / target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("x", encoding="utf-8")

        r = subprocess.run(
            ["git", "check-ignore", "--", target],
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, (
            f"Expected '{target}' to be gitignored (check-ignore rc=0); "
            f"got rc={r.returncode}, stdout={r.stdout!r}, stderr={r.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Area 2-4: _untrack_gitignored_paths
# ---------------------------------------------------------------------------

class TestUntrackGitignoredPaths:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_hygiene_untrack_"))
        self.server = _fresh_server_module(self.tmp)
        self.core = self.server.core

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_tracked_backup_tree(self):
        _init_repo(self.tmp)
        (self.tmp / "db-backup").mkdir()
        (self.tmp / "db-backup" / "knowledge-20260101.sql").write_text("dump\n", encoding="utf-8")
        (self.tmp / "claude-projects-archive").mkdir()
        (self.tmp / "claude-projects-archive" / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
        (self.tmp / "transcript-archive").mkdir()
        (self.tmp / "transcript-archive" / "transcripts-20260101T000000Z-001.xz").write_bytes(b"xz")
        (self.tmp / "session_log.md").write_text("log entry\n", encoding="utf-8")
        (self.tmp / "graph_snapshot.md").write_text("snapshot\n", encoding="utf-8")
        _commit_all(self.tmp, "seed tracked backup files")

    def test_untrack_removes_from_index_but_keeps_file_on_disk(self):
        """The critical assertion: --cached only, files survive on disk."""
        self._make_tracked_backup_tree()

        before = _tracked_paths(self.tmp)
        assert "db-backup/knowledge-20260101.sql" in before
        assert "claude-projects-archive/transcript.jsonl" in before
        assert "transcript-archive/transcripts-20260101T000000Z-001.xz" in before
        assert "session_log.md" in before

        removed = self.core._untrack_gitignored_paths(self.tmp, NEW_GITIGNORE_PATTERNS)

        # Removed from disk? Must NOT be — this is the load-bearing guarantee.
        assert (self.tmp / "db-backup" / "knowledge-20260101.sql").exists(), \
            "File was deleted from disk — --cached must never touch the working tree"
        assert (self.tmp / "claude-projects-archive" / "transcript.jsonl").exists()
        assert (self.tmp / "transcript-archive" / "transcripts-20260101T000000Z-001.xz").exists()
        assert (self.tmp / "session_log.md").exists()

        # Removed from the index?
        after = _tracked_paths(self.tmp)
        assert "db-backup/knowledge-20260101.sql" not in after
        assert "claude-projects-archive/transcript.jsonl" not in after
        assert "transcript-archive/transcripts-20260101T000000Z-001.xz" not in after
        assert "session_log.md" not in after
        # graph_snapshot.md is not gitignored — must remain tracked.
        assert "graph_snapshot.md" in after

        assert removed, "Expected a non-empty list of untracked paths"

    def test_untrack_is_idempotent(self):
        self._make_tracked_backup_tree()
        self.core._configure_paths(self.tmp)

        first = self.core._untrack_gitignored_paths(self.tmp, NEW_GITIGNORE_PATTERNS)
        assert first, "First call should have untracked something"

        second = self.core._untrack_gitignored_paths(self.tmp, NEW_GITIGNORE_PATTERNS)
        assert second == [], f"Second call should be a no-op; got {second}"

        # Files still on disk after both calls.
        assert (self.tmp / "db-backup" / "knowledge-20260101.sql").exists()

    def test_untrack_skips_cleanly_when_not_a_git_repo(self):
        # No git init at all.
        (self.tmp / "db-backup").mkdir()
        (self.tmp / "db-backup" / "knowledge-20260101.sql").write_text("dump\n", encoding="utf-8")
        self.core._configure_paths(self.tmp)

        removed = self.core._untrack_gitignored_paths(self.tmp, NEW_GITIGNORE_PATTERNS)
        assert removed == []
        # File must still be there — untouched.
        assert (self.tmp / "db-backup" / "knowledge-20260101.sql").exists()

    def test_untrack_skips_glob_patterns(self):
        """Glob patterns (*.db etc.) are not concrete git rm targets — skip them
        rather than erroring."""
        self._make_tracked_backup_tree()
        self.core._configure_paths(self.tmp)
        # Should not raise even though *.db / *?mode=* have no literal path meaning.
        removed = self.core._untrack_gitignored_paths(
            self.tmp, ["*.db", "*?mode=*", "db-backup/"]
        )
        assert "db-backup/knowledge-20260101.sql" in removed

    def test_untrack_operates_on_the_data_dir_argument_not_the_module_global(self):
        """Regression test for the `data_dir` parameter being a lie: `_git()` is
        hardcoded to the module-global DATA_DIR, so a caller passing a DIFFERENT
        directory as `data_dir` would previously have git commands run against
        whatever repo the global happened to point at — silently modifying the
        WRONG repo while reporting success. Point the module global at repo A,
        call the function against repo B, and assert repo B (the argument) is
        the one actually changed, while repo A (the global) is untouched."""
        repo_a = Path(tempfile.mkdtemp(prefix="engram_test_hygiene_repoA_"))
        repo_b = Path(tempfile.mkdtemp(prefix="engram_test_hygiene_repoB_"))
        try:
            for repo in (repo_a, repo_b):
                _init_repo(repo)
                (repo / "db-backup").mkdir()
                (repo / "db-backup" / "knowledge-20260101.sql").write_text(
                    "dump\n", encoding="utf-8"
                )
                _commit_all(repo, "seed tracked backup files")

            # Module global points at repo_a; the CALL is against repo_b.
            self.core._configure_paths(repo_a)
            removed = self.core._untrack_gitignored_paths(repo_b, NEW_GITIGNORE_PATTERNS)

            assert removed, "Expected the call to report something untracked"
            assert "db-backup/knowledge-20260101.sql" not in _tracked_paths(repo_b), (
                "The repo actually PASSED IN as data_dir (repo_b) must be the one "
                "untracked"
            )
            assert "db-backup/knowledge-20260101.sql" in _tracked_paths(repo_a), (
                "The module-global's repo (repo_a) must be UNTOUCHED — the bug this "
                "guards against is git commands silently running against the global "
                "instead of the data_dir argument, while still reporting success"
            )
        finally:
            shutil.rmtree(repo_a, ignore_errors=True)
            shutil.rmtree(repo_b, ignore_errors=True)

    def test_untrack_reports_accurate_count_for_paths_needing_quoting(self):
        """The summary count must be structural, not a hand-parse of `git rm`'s
        porcelain stdout (`line.startswith("rm '") and line.endswith("'")`) —
        that shape is fragile against paths git may choose to quote differently
        (e.g. wrapped in double quotes instead of single), which would silently
        drop the line from the count entirely rather than merely mis-render it.
        Cover a path containing a space and a path containing a double quote."""
        _init_repo(self.tmp)
        (self.tmp / "db-backup").mkdir()
        space_path = self.tmp / "db-backup" / "has space.sql"
        quote_path = self.tmp / "db-backup" / 'has"quote.sql'
        space_path.write_text("dump\n", encoding="utf-8")
        quote_path.write_text("dump\n", encoding="utf-8")
        _commit_all(self.tmp, "seed tracked backup files with unusual names")

        before = _tracked_paths(self.tmp)
        assert "db-backup/has space.sql" in before
        assert 'db-backup/has"quote.sql' in before

        removed = self.core._untrack_gitignored_paths(self.tmp, NEW_GITIGNORE_PATTERNS)

        after = _tracked_paths(self.tmp)
        assert "db-backup/has space.sql" not in after
        assert 'db-backup/has"quote.sql' not in after

        assert len(removed) == 2, (
            f"Expected exactly 2 paths reported removed (space + double-quote "
            f"names); got {len(removed)}: {removed}"
        )
        assert "db-backup/has space.sql" in removed
        assert 'db-backup/has"quote.sql' in removed

    @pytest.mark.skipif(
        os.name == "nt", reason="a literal newline is not a legal Windows filename"
    )
    def test_untrack_counts_a_path_with_an_embedded_newline(self):
        """Empirically (this repo's git 2.43.0), `git rm`'s own message does NOT
        C-quote a space or a double quote — the two fixtures the #2048 review
        named — so the OLD porcelain-parsing code happens to still recover those
        two correctly here, and does not by itself distinguish old from new
        behavior in this environment (see PR handoff). A path containing a
        literal newline DOES reproduce a genuine, reproducible under-count on
        the old code, independent of quoting: `git rm` prints the raw byte
        `rm 'sub/has\\nnewline.txt'`, and `.splitlines()` (used by the old parser)
        splits THAT single removed-path message into two lines — neither of which
        starts with "rm '" AND ends with "'" — silently dropping it from the
        count entirely. This is the fixture that actually falsifies the old
        parser in this environment; kept alongside the two the review explicitly
        asked for, not instead of them."""
        _init_repo(self.tmp)
        (self.tmp / "db-backup").mkdir()
        nl_path = self.tmp / "db-backup" / "has\nnewline.sql"
        nl_path.write_text("dump\n", encoding="utf-8")
        _commit_all(self.tmp, "seed tracked backup file with an embedded newline")

        before = _tracked_paths(self.tmp)
        assert "db-backup/has\nnewline.sql" in before

        removed = self.core._untrack_gitignored_paths(self.tmp, NEW_GITIGNORE_PATTERNS)

        after = _tracked_paths(self.tmp)
        assert "db-backup/has\nnewline.sql" not in after
        assert removed == ["db-backup/has\nnewline.sql"], (
            f"Expected the newline-containing path to be reported removed; got {removed}"
        )

    def test_untrack_logs_stderr_on_persistent_git_rm_failure(self, capsys):
        """A `git rm --cached` failure (index lock, permissions, corruption) must
        be reported on stderr, not silently `continue`d — otherwise a persistent
        failure fails every run forever, invisibly. Reproduce a real locked-index
        failure with a stale `.git/index.lock` rather than mocking, so this
        exercises the actual git error path."""
        self._make_tracked_backup_tree()
        lock_path = self.tmp / ".git" / "index.lock"
        lock_path.write_text("", encoding="utf-8")
        try:
            removed = self.core._untrack_gitignored_paths(self.tmp, NEW_GITIGNORE_PATTERNS)
        finally:
            lock_path.unlink(missing_ok=True)

        assert removed == [], "A locked index should fail every path — nothing removed"
        captured = capsys.readouterr()
        assert "git rm --cached exited" in captured.err, (
            f"Expected a loud stderr report of the git rm failure; got: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# Area 5-6: _maybe_run_daily_git_gc
# ---------------------------------------------------------------------------

class TestMaybeRunDailyGitGc:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_hygiene_gc_"))
        self.server = _fresh_server_module(self.tmp)
        self.core = self.server.core

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skips_cleanly_when_not_a_git_repo(self):
        result = self.core._maybe_run_daily_git_gc(self.tmp)
        assert result == {"skipped": "not a git repo"}

    def test_first_call_runs_gc_and_writes_stamp(self):
        _init_repo(self.tmp)
        (self.tmp / "file.txt").write_text("hello\n", encoding="utf-8")
        _commit_all(self.tmp, "seed")

        result = self.core._maybe_run_daily_git_gc(self.tmp)
        assert result.get("ran") is True, f"Expected a successful gc run; got {result}"
        assert "duration_s" in result

        stamp = self.tmp / ".git-gc-stamp"
        assert stamp.exists(), "Expected a date stamp file after a successful gc run"

    def test_stamp_prevents_second_same_day_run(self):
        _init_repo(self.tmp)
        (self.tmp / "file.txt").write_text("hello\n", encoding="utf-8")
        _commit_all(self.tmp, "seed")

        first = self.core._maybe_run_daily_git_gc(self.tmp)
        assert first.get("ran") is True

        # Second call same day: must NOT invoke git gc again. Patch subprocess.Popen
        # (gc launches via Popen, not run — see #2048 review) to explode if called,
        # proving the skip path short-circuits before it.
        with mock.patch.object(
            self.core.subprocess, "Popen",
            side_effect=AssertionError("git gc should not run twice on the same day"),
        ):
            second = self.core._maybe_run_daily_git_gc(self.tmp)
        assert second == {"skipped": "already ran today"}

    def test_never_raises_on_git_failure(self):
        """Best-effort discipline: a git gc failure must be surfaced as a dict,
        never propagate as an exception."""
        _init_repo(self.tmp)
        (self.tmp / "file.txt").write_text("hello\n", encoding="utf-8")
        _commit_all(self.tmp, "seed")

        fake_proc = mock.Mock()
        fake_proc.pid = 999999
        fake_proc.returncode = 1
        fake_proc.communicate.return_value = ("", "simulated failure")
        with mock.patch.object(self.core.subprocess, "Popen", return_value=fake_proc):
            result = self.core._maybe_run_daily_git_gc(self.tmp)
        assert "error" in result
        # No stamp written on failure — must retry later today.
        assert not (self.tmp / ".git-gc-stamp").exists()

    def test_timeout_kills_the_whole_process_group(self):
        """#2048 review: `git gc` forks `git pack-objects`/`git repack`; a bare
        subprocess.run(timeout=...) only kills the immediate `git gc` process,
        which can orphan a still-writing child. On TimeoutExpired, the process
        GROUP must be killed (os.killpg), not just the single process."""
        _init_repo(self.tmp)
        (self.tmp / "file.txt").write_text("hello\n", encoding="utf-8")
        _commit_all(self.tmp, "seed")

        fake_proc = mock.Mock()
        fake_proc.pid = 424242
        fake_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd=["git", "gc"], timeout=self.core.GIT_GC_TIMEOUT),
            ("", ""),  # the post-kill reap call
        ]

        with mock.patch.object(self.core.subprocess, "Popen", return_value=fake_proc), \
             mock.patch.object(self.core.os, "killpg") as mock_killpg, \
             mock.patch.object(self.core.os, "getpgid", return_value=424242):
            result = self.core._maybe_run_daily_git_gc(self.tmp)

        assert "error" in result
        assert "timed out" in result["error"]
        mock_killpg.assert_called_once_with(424242, self.core.signal.SIGKILL)
        fake_proc.kill.assert_called_once()
        # No stamp written on a timeout — must retry later today.
        assert not (self.tmp / ".git-gc-stamp").exists()


# ---------------------------------------------------------------------------
# Area 7-8: _ensure_engram_gitignore idempotency + negation preservation
# ---------------------------------------------------------------------------

class TestEnsureEngramGitignoreWithNewPatterns:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_hygiene_ensure_"))
        self.server = _fresh_server_module(self.tmp)

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_ops_when_all_patterns_present(self):
        gitignore = self.tmp / ".gitignore"
        self.server._ensure_engram_gitignore(self.tmp)
        original = gitignore.read_text(encoding="utf-8")

        import time
        time.sleep(0.05)
        self.server._ensure_engram_gitignore(self.tmp)

        assert gitignore.read_text(encoding="utf-8") == original

    def test_refresh_adds_new_patterns_to_incomplete_file(self):
        gitignore = self.tmp / ".gitignore"
        # Simulate a pre-fix .gitignore: has the old patterns, not the new ones.
        gitignore.write_text("*.db\n*.log\n", encoding="utf-8")

        self.server._ensure_engram_gitignore(self.tmp)

        content = gitignore.read_text(encoding="utf-8")
        lines = {l.strip() for l in content.splitlines()}
        missing = [p for p in NEW_GITIGNORE_PATTERNS if p not in lines]
        assert not missing, f"Refresh did not add new patterns: {missing}"

    def test_refresh_preserves_hand_added_negation(self):
        """A deployed install can hand-add `!diary/diary.db` outside the
        canonical pattern list. A refresh forced by the new patterns must not
        silently drop it."""
        gitignore = self.tmp / ".gitignore"
        gitignore.write_text(
            "*.db\n"
            "*.db-shm\n"
            "*.db-wal\n"
            "*.db-journal\n"
            "!diary/diary.db\n"
            "*?mode=*\n"
            "*.bak\n"
            "*.bak.*\n"
            "config.json.bak*\n"
            "*.backfill-snapshot-*\n"
            "*.log\n"
            "marketplace/\n"
            "venv/\n"
            "__pycache__/\n"
            "*.pyc\n",
            encoding="utf-8",
        )

        self.server._ensure_engram_gitignore(self.tmp)

        content = gitignore.read_text(encoding="utf-8")
        lines = {l.strip() for l in content.splitlines()}
        assert "!diary/diary.db" in lines, (
            "Refresh dropped the hand-added negation — this would silently "
            "re-ignore diary.db on an install that had un-ignored it"
        )
        missing = [p for p in NEW_GITIGNORE_PATTERNS if p not in lines]
        assert not missing, f"Refresh should still add the new patterns: {missing}"

    def test_refresh_logs_preserved_negations_on_stderr(self, capsys):
        """Keeping every `!`-prefixed line (not just `!diary/diary.db`) is
        deliberate, but that means a stale hand-added negation would also
        survive a refresh silently unless it's named on stderr. #2048 review."""
        gitignore = self.tmp / ".gitignore"
        gitignore.write_text(
            "*.db\n!diary/diary.db\n!some/stale/negation\n*.log\n",
            encoding="utf-8",
        )

        self.server._ensure_engram_gitignore(self.tmp)

        captured = capsys.readouterr()
        assert "preserving 2" in captured.err
        assert "!diary/diary.db" in captured.err
        assert "!some/stale/negation" in captured.err

    def test_c6_transcript_archive_is_in_the_canonical_patterns(self):
        """#4647 part 2: the compressed transcript archive holds raw chat transcripts and
        must stay on the machine, never ride into the off-disk git remote."""
        import engram_core

        assert "transcript-archive/" in engram_core.ENGRAM_GITIGNORE_PATTERNS

    def test_forum_token_is_in_the_canonical_patterns(self):
        """#4503: the #1459 bearer token (`$ENGRAM_HOME/forum-token`) must be a
        canonical pattern, not a seat-side hand-add — a canonical refresh
        rewrites the file and a hand-added line would be dropped."""
        import engram_core

        assert "forum-token" in engram_core.ENGRAM_GITIGNORE_PATTERNS

    def test_forced_refresh_ignores_forum_token_and_keeps_negation(self):
        """#4503 contract, on the REWRITE path: a `forum-token` file present in
        a home is ignored after `_ensure_engram_gitignore` runs, and the
        hand-added `!diary/diary.db` negation still survives that same refresh.

        The fixture .gitignore is deliberately incomplete, which is what makes
        the function rewrite instead of early-returning; the guard below
        asserts that precondition so this cell cannot silently pass on the
        early-return path and prove nothing.
        """
        import engram_core

        gitignore = self.tmp / ".gitignore"
        gitignore.write_text(
            "*.db\n"
            "!diary/diary.db\n"
            "*.log\n",
            encoding="utf-8",
        )

        # Guard: the fixture MUST be missing at least one canonical pattern, or
        # _ensure_engram_gitignore early-returns and this cell proves nothing.
        existing_lines = {
            l.strip() for l in gitignore.read_text(encoding="utf-8").splitlines()
        }
        missing = [
            p for p in engram_core.ENGRAM_GITIGNORE_PATTERNS
            if p not in existing_lines
        ]
        assert missing, (
            "fixture .gitignore already contains every canonical pattern — the "
            "function would early-return and this cell would not exercise the "
            "rewrite path it exists to test"
        )

        (self.tmp / "forum-token").write_text("deadbeef\n", encoding="utf-8")

        self.server._ensure_engram_gitignore(self.tmp)

        content = gitignore.read_text(encoding="utf-8")
        lines = {l.strip() for l in content.splitlines()}
        assert "forum-token" in lines, (
            "forced refresh did not write a forum-token line — the #1459 bearer "
            "token would stay stageable by a hand `git add -A`"
        )
        assert "!diary/diary.db" in lines, (
            "Refresh dropped the hand-added negation — this would silently "
            "re-ignore diary.db on an install that had un-ignored it"
        )

        # The file is actually ignored by git (this half is skipped cleanly
        # where git is unavailable).
        if shutil.which("git") is None:
            return
        _init_repo(self.tmp)
        r = subprocess.run(
            ["git", "check-ignore", "--", "forum-token"],
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, (
            "Expected 'forum-token' to be gitignored (check-ignore rc=0); "
            f"got rc={r.returncode}, stdout={r.stdout!r}, stderr={r.stderr!r}"
        )
