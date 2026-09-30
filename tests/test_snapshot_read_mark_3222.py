"""#3222 -- the per-turn snapshot WAL read-mark must not fail silently.

Contract (decided, tests-first): durability wins -- the capture STILL RUNS when
the read-mark fails -- but the failure is observable in the returned dict:

  read_mark               "held" | "failed" | "inherited"
  read_mark_error         str, only on "failed"
  read_mark_release_error str, only when the release COMMIT raised (ROLLBACK is
                          then attempted; its failure text is appended)

plus one stderr line per failure. Covers BOTH ``_commit_snapshot_async`` and
``_commit_snapshot_sync``. Failures are forced with a wrapper around the real
sqlite3.Connection (no global sqlite3 monkeypatching); everything else is
delegated, including ``backup()``. Runs against a temp data dir, never ~/.engram.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class RecordingConn:
    """Delegating wrapper over a real sqlite3.Connection that records executed
    SQL and can be told to raise on the read-mark statements."""

    def __init__(self, real, *, fail_begin=False, fail_select=False,
                 fail_commit=False, fail_rollback=False):
        self._real = real
        self.executed: list[str] = []
        self.fail_begin = fail_begin
        self.fail_select = fail_select
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback

    def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)
        if sql == "BEGIN DEFERRED" and self.fail_begin:
            raise sqlite3.OperationalError("synthetic BEGIN failure")
        if "sqlite_master" in sql and self.fail_select:
            raise sqlite3.OperationalError("synthetic SELECT failure")
        if sql == "COMMIT" and self.fail_commit:
            raise sqlite3.OperationalError("synthetic COMMIT failure")
        if sql == "ROLLBACK" and self.fail_rollback:
            raise sqlite3.OperationalError("synthetic ROLLBACK failure")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):  # in_transaction, backup, commit, close, ...
        return getattr(self._real, name)


@pytest.fixture
def env():
    tmp = Path(tempfile.mkdtemp(prefix="engram_test_readmark_3222_"))
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    import server
    server._configure_paths(tmp)
    server._ensure_data_dir()
    server.core._git_available = True  # bypass git init; _git is mocked per test
    conn = server._get_db()
    conn.execute(
        "INSERT OR IGNORE INTO nodes (id, type, claim, created_at) "
        "VALUES ('ob_rm', 'observation', 'read-mark fixture', '2026-01-01T00:00:00')"
    )
    conn.commit()
    yield server, conn, tmp
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _fake_git(*args, **kwargs):
    r = mock.MagicMock()
    r.returncode = 1 if args and args[0] == "check-ignore" else 0
    r.stdout = "M knowledge.sql" if args and args[0] == "status" else ""
    r.stderr = ""
    return r


def _run_sync(server, conn):
    core = server.core
    with mock.patch.object(core, "_git", side_effect=_fake_git), \
         mock.patch.object(core, "_push_head_ensuring_upstream", return_value=_fake_git()):
        return core._commit_snapshot_sync(conn, "t", "test", skip_checkpoint=True)


def _run_async(server, conn, tmp):
    core = server.core
    import engram_snapshot_worker as sw
    with mock.patch.object(sw, "_data_dir", lambda: tmp), \
         mock.patch.object(sw, "start_worker"), \
         mock.patch.object(sw, "enqueue_job"), \
         mock.patch.object(core, "_maybe_run_daily_git_gc", return_value={"skipped": True}):
        return core._commit_snapshot_async(conn, "t", "test", True)


# Each case returns (wrapper, result, capture_ok) so the same assertions cover both.
def _sync_case(env, **kw):
    server, conn, tmp = env
    w = RecordingConn(conn, **kw)
    result = _run_sync(server, w)
    sql = tmp / "knowledge.sql"
    return w, result, (sql.exists() and sql.stat().st_size > 0)


def _async_case(env, **kw):
    server, conn, tmp = env
    w = RecordingConn(conn, **kw)
    result = _run_async(server, w, tmp)
    ok = (result is not None and "binary_backup" in result
          and "error" not in result["binary_backup"])
    return w, result, ok


CASES = pytest.mark.parametrize(
    "case,fn",
    [(_sync_case, "_commit_snapshot_sync"), (_async_case, "_commit_snapshot_async")],
    ids=["sync", "async"],
)


@CASES
def test_1_happy_path_held(env, case, fn):
    w, result, ok = case(env)
    assert result["read_mark"] == "held"
    assert "read_mark_error" not in result
    assert "read_mark_release_error" not in result
    assert ok
    assert not w.in_transaction


@CASES
def test_2_begin_raises_marks_failed_and_capture_runs(env, case, fn, capsys):
    w, result, ok = case(env, fail_begin=True)
    assert result["read_mark"] == "failed"
    assert "synthetic BEGIN failure" in result["read_mark_error"]
    err = capsys.readouterr().err
    assert f"[engram snapshot] read-mark FAILED ({fn}): synthetic BEGIN failure" in err
    assert "capture will run without a consistent WAL view" in err
    assert ok, "capture must still run when the read-mark fails"
    assert "COMMIT" not in w.executed, "no COMMIT without a successful BEGIN"


@CASES
def test_3_select_raises_after_begin_still_commits(env, case, fn):
    w, result, ok = case(env, fail_select=True)
    assert result["read_mark"] == "failed"
    assert "synthetic SELECT failure" in result["read_mark_error"]
    assert "COMMIT" in w.executed
    assert not w.in_transaction
    assert ok


@CASES
def test_4_already_in_transaction_is_inherited(env, case, fn):
    server, conn, tmp = env
    # A read transaction (no write lock) keeps the async path's other
    # connections from blocking while still making in_transaction True.
    conn.execute("BEGIN")
    conn.execute("SELECT count(*) FROM nodes")
    assert conn.in_transaction
    w, result, ok = case(env)
    assert result["read_mark"] == "inherited"
    assert "BEGIN DEFERRED" not in w.executed
    assert "read_mark_error" not in result
    conn.rollback()


@CASES
def test_5_commit_raises_on_release_reports_and_rolls_back(env, case, fn, capsys):
    w, result, ok = case(env, fail_commit=True)
    assert result["read_mark"] == "held"
    assert "synthetic COMMIT failure" in result["read_mark_release_error"]
    err = capsys.readouterr().err
    assert (f"[engram snapshot] read-mark release COMMIT FAILED ({fn}): "
            "synthetic COMMIT failure") in err
    assert "ROLLBACK" in w.executed
    assert not w.in_transaction, "ROLLBACK must clear the dangling read transaction"


@CASES
def test_5b_rollback_failure_text_is_appended_and_never_raises(env, case, fn):
    w, result, ok = case(env, fail_commit=True, fail_rollback=True)
    msg = result["read_mark_release_error"]
    assert "synthetic COMMIT failure" in msg
    assert "synthetic ROLLBACK failure" in msg
    w._real.execute("ROLLBACK")  # real conn still holds the txn; clean up


def test_6_async_capture_raises_still_releases_read_mark(env):
    """An exception from next_seq()/write_durable_snapshot() must not strand the
    read transaction, and must still propagate (release is added, nothing else)."""
    server, conn, tmp = env
    core = server.core
    import engram_snapshot_worker as sw
    w = RecordingConn(conn)
    with mock.patch.object(sw, "_data_dir", lambda: tmp), \
         mock.patch.object(sw, "start_worker"), \
         mock.patch.object(sw, "enqueue_job"), \
         mock.patch.object(sw, "next_seq",
                           side_effect=RuntimeError("synthetic next_seq failure")), \
         mock.patch.object(core, "_maybe_run_daily_git_gc",
                           return_value={"skipped": True}):
        with pytest.raises(RuntimeError, match="synthetic next_seq failure"):
            core._commit_snapshot_async(w, "t", "test", True)
    assert "BEGIN DEFERRED" in w.executed
    assert "COMMIT" in w.executed, "release COMMIT must be issued when the capture raises"
    assert not w.in_transaction


def _git_variant(**overrides):
    """Fake _git whose per-subcommand (returncode, stdout) can be overridden."""
    def fake(*args, **kwargs):
        r = _fake_git(*args, **kwargs)
        rc, out = overrides.get(args[0], (None, None)) if args else (None, None)
        if rc is not None:
            r.returncode = rc
        if out == "<ARGS>":  # echo the staged paths back (check-ignore: all ignored)
            r.stdout = "\n".join(args[2:])
        elif out is not None:
            r.stdout = out
        return r
    return fake


# Each entry drives a different return path of _commit_snapshot_sync that comes AFTER
# the read-mark block; every one must carry read_mark (it rides on _dump_fields).
@pytest.mark.parametrize(
    "git_overrides,expect",
    [
        ({}, {"git_committed": True}),
        ({"status": (0, "")}, {"git_committed": False, "reason": "no changes since last commit"}),
        ({"commit": (1, "")}, {"git_committed": False}),
        ({"add": (1, "")}, {"git_committed": False}),
        ({"check-ignore": (0, "<ARGS>")},
         {"git_committed": False, "reason": "gitignored"}),
    ],
    ids=["success", "nothing_to_commit", "commit_failed", "add_failed", "all_ignored"],
)
def test_7_sync_every_post_block_return_carries_read_mark(env, git_overrides, expect):
    server, conn, tmp = env
    core = server.core
    with mock.patch.object(core, "_git", side_effect=_git_variant(**git_overrides)), \
         mock.patch.object(core, "_push_head_ensuring_upstream", return_value=_fake_git()):
        result = core._commit_snapshot_sync(conn, "t", "test", skip_checkpoint=True)
    for k, v in expect.items():
        assert k in result, (k, result)
        if isinstance(v, str):
            assert v in result[k], (k, result)
        else:
            assert result[k] == v, (k, result)
    assert result.get("read_mark") == "held", result
