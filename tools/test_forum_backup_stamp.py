"""Contract tests for the forum-backup success stamp (engram-alpha#3276, part A).

Written BEFORE the implementation (contract-first, #3045). The incident: the
nightly backup failed every run for 12 days after a host teardown left 0-byte
git objects (``fatal: bad object HEAD``) and nobody heard about it. The stamp
``<backup-dir>/.last-success.json`` is what /health and ``forum status`` read
to say "last good backup was N hours ago".

Contract (designer: ariadne-calvino, accepted: kepler-somnium, 2026-10-01):
  A1  every SUCCESSFUL run (commit made OR nothing changed) writes
      {"at": <UTC ISO>, "commit": <HEAD sha or null>, "committed": <bool>},
      atomically (temp file + rename).
  A2  every failure path (each ``return 1``) leaves the stamp UNTOUCHED.
  A3  ``.last-success.json`` is in GITIGNORE_CONTENT, so the stamp does not make
      the next run look "changed".
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.forum_backup as fb  # noqa: E402

STAMP_NAME = ".last-success.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE threads (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, thread_id INTEGER, body TEXT)")
    conn.execute("INSERT INTO threads (title) VALUES ('hello')")
    conn.execute("INSERT INTO posts (thread_id, body) VALUES (1, 'foo')")
    conn.commit()
    conn.close()


def _add_row(path: str, body: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO posts (thread_id, body) VALUES (1, ?)", (body,))
    conn.commit()
    conn.close()


def _run(db: str, backup_dir: str, tmp_path: Path, extra: list[str] | None = None) -> int:
    argv = [
        "forum_backup.py", "--db", db, "--backup-dir", backup_dir,
        "--audit", str(tmp_path / "audit.jsonl"),
    ] + (extra or [])
    with mock.patch.object(sys, "argv", argv):
        return fb.main()


def _git(backup_dir: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=backup_dir, capture_output=True, text=True, check=True
    ).stdout.strip()


def _stamp(backup_dir: str) -> Path:
    return Path(backup_dir) / STAMP_NAME


def _parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    assert dt.tzinfo is not None, f"stamp 'at' must be timezone-aware UTC, got {s!r}"
    return dt.astimezone(timezone.utc)


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "forum.db")
    backup_dir = str(tmp_path / "backup")
    _make_db(db)
    return db, backup_dir, tmp_path


def _good_run_then_stamp_bytes(db, backup_dir, tmp_path) -> bytes:
    """Do one real successful run; return the stamp bytes. Asserts the stamp
    exists -- the precondition that makes the 'failure leaves it untouched'
    tests meaningful (and red on a tree that never writes a stamp)."""
    assert _run(db, backup_dir, tmp_path) == 0
    assert _stamp(backup_dir).is_file(), "successful run must write .last-success.json"
    return _stamp(backup_dir).read_bytes()


def _empty_head_commit_object(backup_dir: str) -> None:
    """Reproduce the incident: HEAD points at a 0-byte commit object, so
    ``git commit`` dies with ``fatal: bad object HEAD``."""
    sha = _git(backup_dir, "rev-parse", "HEAD")
    obj = Path(backup_dir) / ".git" / "objects" / sha[:2] / sha[2:]
    assert obj.is_file(), "fixture assumes a loose HEAD commit object"
    obj.chmod(0o644)
    obj.write_bytes(b"")
    assert obj.stat().st_size == 0


# ---------------------------------------------------------------------------
# A1 -- stamp written on every successful run
# ---------------------------------------------------------------------------

class TestStampWrittenOnSuccess:

    def test_stamp_written_when_commit_made(self, env):
        db, backup_dir, tmp_path = env
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        assert _run(db, backup_dir, tmp_path) == 0
        after = datetime.now(timezone.utc) + timedelta(seconds=5)

        stamp = json.loads(_stamp(backup_dir).read_text(encoding="utf-8"))
        assert set(stamp) == {"at", "commit", "committed"}
        assert stamp["committed"] is True
        assert stamp["commit"] == _git(backup_dir, "rev-parse", "HEAD")
        assert before <= _parse_utc(stamp["at"]) <= after

    def test_stamp_written_when_nothing_changed(self, env):
        db, backup_dir, tmp_path = env
        assert _run(db, backup_dir, tmp_path) == 0
        first = json.loads(_stamp(backup_dir).read_text(encoding="utf-8"))
        head = _git(backup_dir, "rev-parse", "HEAD")

        assert _run(db, backup_dir, tmp_path) == 0
        second = json.loads(_stamp(backup_dir).read_text(encoding="utf-8"))

        assert second["committed"] is False
        assert second["commit"] == head, "commit = current HEAD even when nothing was committed"
        assert _parse_utc(second["at"]) >= _parse_utc(first["at"])

    def test_stamp_refreshed_even_though_nothing_committed(self, env):
        """A quiet forum must not look like a dead backup: the 'at' of the
        no-change run is a NEW timestamp, not the old commit's."""
        db, backup_dir, tmp_path = env
        assert _run(db, backup_dir, tmp_path) == 0
        # Age the stamp by hand, then run again with no DB change.
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        _stamp(backup_dir).write_text(
            json.dumps({"at": old.isoformat(), "commit": None, "committed": True})
        )
        assert _run(db, backup_dir, tmp_path) == 0
        new = json.loads(_stamp(backup_dir).read_text(encoding="utf-8"))
        assert datetime.now(timezone.utc) - _parse_utc(new["at"]) < timedelta(minutes=5)

    def test_stamp_written_atomically_via_rename(self, env):
        """Temp file + rename: the final stamp path must be the DESTINATION of
        an os.replace/os.rename, never opened for in-place write."""
        db, backup_dir, tmp_path = env
        seen: list[str] = []
        real_replace, real_rename = os.replace, os.rename

        def spy_replace(src, dst, *a, **k):
            seen.append(str(dst))
            return real_replace(src, dst, *a, **k)

        def spy_rename(src, dst, *a, **k):
            seen.append(str(dst))
            return real_rename(src, dst, *a, **k)

        with mock.patch.object(os, "replace", spy_replace), \
                mock.patch.object(os, "rename", spy_rename):
            assert _run(db, backup_dir, tmp_path) == 0
        assert any(d.endswith(STAMP_NAME) for d in seen), (
            f"stamp not written via temp+rename; renames seen: {seen}"
        )
        assert _stamp(backup_dir).is_file()


# ---------------------------------------------------------------------------
# A3 -- stamp is gitignored; it must not make every run "changed"
# ---------------------------------------------------------------------------

class TestStampGitignored:

    def test_gitignore_content_lists_stamp(self):
        assert STAMP_NAME in fb.GITIGNORE_CONTENT.splitlines()

    def test_written_gitignore_ignores_stamp(self, env):
        db, backup_dir, tmp_path = env
        assert _run(db, backup_dir, tmp_path) == 0
        assert _stamp(backup_dir).is_file()
        r = subprocess.run(
            ["git", "check-ignore", "-q", STAMP_NAME], cwd=backup_dir
        )
        assert r.returncode == 0, "stamp must be git-ignored"

    def test_second_run_no_db_change_commits_nothing(self, env):
        db, backup_dir, tmp_path = env
        assert _run(db, backup_dir, tmp_path) == 0
        n1 = int(_git(backup_dir, "rev-list", "--count", "HEAD"))
        assert _stamp(backup_dir).is_file()
        assert _run(db, backup_dir, tmp_path) == 0
        n2 = int(_git(backup_dir, "rev-list", "--count", "HEAD"))
        assert n2 == n1, "stamp must not create a new commit"
        assert json.loads(_stamp(backup_dir).read_text())["committed"] is False
        assert STAMP_NAME not in _git(backup_dir, "ls-files")


# ---------------------------------------------------------------------------
# A2 -- a failure NEVER touches the stamp (the line that matters most)
# ---------------------------------------------------------------------------

class TestFailureLeavesStampUntouched:

    def test_empty_head_object_incident_shape(self, env):
        """THE incident: 0-byte HEAD commit object -> 'bad object HEAD'."""
        db, backup_dir, tmp_path = env
        before = _good_run_then_stamp_bytes(db, backup_dir, tmp_path)
        _empty_head_commit_object(backup_dir)
        _add_row(db, "new data so there is something to commit")

        rc = _run(db, backup_dir, tmp_path)

        assert rc == 1
        assert _stamp(backup_dir).read_bytes() == before, (
            "a failed run must leave the stamp byte-identical"
        )

    def test_empty_head_object_absent_stamp_stays_absent(self, env):
        db, backup_dir, tmp_path = env
        _good_run_then_stamp_bytes(db, backup_dir, tmp_path)
        _stamp(backup_dir).unlink()
        _empty_head_commit_object(backup_dir)
        _add_row(db, "more data")

        assert _run(db, backup_dir, tmp_path) == 1
        assert not _stamp(backup_dir).exists(), "failure must not create a stamp"

    def test_missing_db_leaves_stamp_untouched(self, env):
        db, backup_dir, tmp_path = env
        before = _good_run_then_stamp_bytes(db, backup_dir, tmp_path)
        rc = _run(str(tmp_path / "does-not-exist.db"), backup_dir, tmp_path)
        assert rc == 1
        assert _stamp(backup_dir).read_bytes() == before

    def test_missing_db_absent_stamp_stays_absent(self, env):
        _, backup_dir, tmp_path = env
        rc = _run(str(tmp_path / "does-not-exist.db"), backup_dir, tmp_path)
        assert rc == 1
        assert not _stamp(backup_dir).exists()

    def test_corrupt_db_snapshot_failure_leaves_stamp_untouched(self, env):
        db, backup_dir, tmp_path = env
        before = _good_run_then_stamp_bytes(db, backup_dir, tmp_path)
        Path(db).write_bytes(b"this is not a sqlite database" * 50)
        rc = _run(db, backup_dir, tmp_path)
        assert rc == 1
        assert _stamp(backup_dir).read_bytes() == before

    def test_verify_failure_leaves_stamp_untouched(self, env):
        db, backup_dir, tmp_path = env
        before = _good_run_then_stamp_bytes(db, backup_dir, tmp_path)
        _add_row(db, "changed")
        with mock.patch.object(fb, "_verify_dump", return_value=False):
            rc = _run(db, backup_dir, tmp_path, extra=["--verify"])
        assert rc == 1
        assert _stamp(backup_dir).read_bytes() == before


# ---------------------------------------------------------------------------
# A3 on an EXISTING (old-style) backup repo: the live repo predates the stamp,
# so its .gitignore has no stamp line and _git_init returns early.
# ---------------------------------------------------------------------------

OLD_GITIGNORE = "*.db\n*.tmp\n"


def _old_style_repo(backup_dir: str, stamp_committed: bool) -> None:
    Path(backup_dir).mkdir(parents=True)
    _git(backup_dir, "init", "-q")
    _git(backup_dir, "config", "--local", "user.name", "t")
    _git(backup_dir, "config", "--local", "user.email", "t@localhost")
    (Path(backup_dir) / ".gitignore").write_text(OLD_GITIGNORE)
    _git(backup_dir, "add", "-A")
    if stamp_committed:
        (Path(backup_dir) / STAMP_NAME).write_text(
            json.dumps({"at": "2026-01-01T00:00:00+00:00", "commit": None, "committed": True})
        )
        _git(backup_dir, "add", "-f", STAMP_NAME)
    _git(backup_dir, "commit", "-q", "-m", "seed", "--allow-empty")


class TestStampIgnoredInPreexistingRepo:

    @pytest.mark.parametrize("stamp_committed", [False, True], ids=["stamp-absent", "stamp-tracked"])
    def test_second_run_on_unchanged_db_commits_nothing(self, env, stamp_committed):
        db, backup_dir, tmp_path = env
        _old_style_repo(backup_dir, stamp_committed)
        assert _run(db, backup_dir, tmp_path) == 0   # first run: real DB content committed
        n1 = int(_git(backup_dir, "rev-list", "--count", "HEAD"))
        assert _run(db, backup_dir, tmp_path) == 0
        n2 = int(_git(backup_dir, "rev-list", "--count", "HEAD"))
        assert n2 == n1, "stamp must not make an unchanged run commit"
        assert json.loads(_stamp(backup_dir).read_text())["committed"] is False
        assert STAMP_NAME not in _git(backup_dir, "ls-files").splitlines()

    def test_retrofit_run_with_unchanged_db_commits_only_the_gitignore(self, env):
        """Old-style repo (2-line .gitignore, no stamp) whose forum.sql ALREADY
        matches the DB: the first run with zero DB change makes exactly one
        commit, and its only diff is .gitignore; the stamp stays untracked."""
        db, backup_dir, tmp_path = env
        assert _run(db, backup_dir, tmp_path) == 0        # produce the current forum.sql
        (Path(backup_dir) / ".gitignore").write_text(OLD_GITIGNORE)  # regress to old style
        _stamp(backup_dir).unlink()
        _git(backup_dir, "add", "-A")
        _git(backup_dir, "commit", "-q", "-m", "old-style baseline")
        n1 = int(_git(backup_dir, "rev-list", "--count", "HEAD"))

        assert _run(db, backup_dir, tmp_path) == 0         # zero DB change

        assert int(_git(backup_dir, "rev-list", "--count", "HEAD")) == n1 + 1
        assert _git(backup_dir, "show", "--name-only", "--format=", "HEAD").splitlines() == [".gitignore"]
        assert STAMP_NAME not in _git(backup_dir, "ls-files").splitlines()
        assert json.loads(_stamp(backup_dir).read_text())["committed"] is True

    def test_existing_gitignore_lines_are_preserved_not_clobbered(self, env):
        db, backup_dir, tmp_path = env
        _old_style_repo(backup_dir, False)
        (Path(backup_dir) / ".gitignore").write_text(OLD_GITIGNORE + "custom-line\n")
        _git(backup_dir, "add", "-A")
        _git(backup_dir, "commit", "-q", "-m", "custom")
        assert _run(db, backup_dir, tmp_path) == 0
        lines = (Path(backup_dir) / ".gitignore").read_text().splitlines()
        assert {"*.db", "*.tmp", "custom-line", STAMP_NAME} <= set(lines)
        assert lines.count(STAMP_NAME) == 1
        assert _run(db, backup_dir, tmp_path) == 0
        assert (Path(backup_dir) / ".gitignore").read_text().splitlines().count(STAMP_NAME) == 1

    def test_failure_in_preexisting_repo_still_leaves_stamp_untouched(self, env):
        db, backup_dir, tmp_path = env
        _old_style_repo(backup_dir, False)
        before = _good_run_then_stamp_bytes(db, backup_dir, tmp_path)
        _empty_head_commit_object(backup_dir)
        _add_row(db, "new data")
        assert _run(db, backup_dir, tmp_path) == 1
        assert _stamp(backup_dir).read_bytes() == before
