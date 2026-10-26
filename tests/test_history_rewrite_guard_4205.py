"""Contract tests for #4205 part 2: pushed history cannot be rewritten.

Written before the implementation.  ``engram_core._init_git`` installs a
``reference-transaction`` hook into ``<ENGRAM_HOME>/.git/hooks/`` that refuses a
rewrite (amend / reset / rebase) of a commit already reachable from
``refs/remotes/origin/*``.  Tmp repos and a bare tmp remote only.
"""

import os
import subprocess

import pytest

MSG = "engram: refusing to rewrite pushed commit {old} on master; make a new commit"


def git(cwd, *args, check=True, env_extra=None):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env_extra or {})}
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check:
        assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r


@pytest.fixture(autouse=True)
def _no_opt_out(monkeypatch):
    monkeypatch.delenv("ENGRAM_ALLOW_HISTORY_REWRITE", raising=False)
    monkeypatch.delenv("ENGRAM_DISABLE_GIT", raising=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A tmp ENGRAM_HOME clone of a bare remote with a pushed commit, the guard installed."""
    import engram_core

    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare", "-b", "master")
    h = tmp_path / "home"
    h.mkdir()
    git(h, "init", "-q", "-b", "master")
    git(h, "config", "user.email", "t@example.invalid")
    git(h, "config", "user.name", "t")
    git(h, "remote", "add", "origin", str(bare))
    (h / "a.txt").write_text("1\n")
    git(h, "add", "a.txt")
    git(h, "commit", "-q", "-m", "first")
    (h / "a.txt").write_text("2\n")
    git(h, "commit", "-q", "-am", "second")
    git(h, "push", "-q", "-u", "origin", "master")
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    assert engram_core._install_rewrite_guard() == "installed"
    return h


def head(h):
    return git(h, "rev-parse", "HEAD").stdout.strip()


def test_amend_of_pushed_commit_is_refused_with_message(repo):
    old = head(repo)
    (repo / "a.txt").write_text("amended\n")
    r = git(repo, "commit", "-q", "-a", "--amend", "--no-edit", check=False)
    assert r.returncode != 0
    assert MSG.format(old=old[:8]) in r.stderr
    assert "ENGRAM_ALLOW_HISTORY_REWRITE=1" in r.stderr
    assert head(repo) == old


def test_dream_record_amend_sequence_is_refused(repo):
    """The measured sequence: commit, worker pushes, a later step edits a file and
    runs `git commit --amend --no-edit`.  Refused; HEAD stays the pushed commit."""
    (repo / "record.md").write_text("placeholders\n")
    git(repo, "add", "record.md")
    git(repo, "commit", "-q", "-m", "turn advance")
    git(repo, "push", "-q", "origin", "HEAD")
    pushed = head(repo)
    (repo / "record.md").write_text("finalized\n")
    git(repo, "add", "record.md")
    r = git(repo, "commit", "-q", "--amend", "--no-edit", check=False)
    assert r.returncode != 0
    assert MSG.format(old=pushed[:8]) in r.stderr
    assert head(repo) == pushed
    # the sanctioned alternative: a NEW commit
    git(repo, "commit", "-q", "-m", "dream: record finalized")
    assert git(repo, "merge-base", "--is-ancestor", pushed, "HEAD", check=False).returncode == 0


def test_amend_of_unpushed_commit_succeeds(repo):
    (repo / "a.txt").write_text("3\n")
    git(repo, "commit", "-q", "-am", "third")
    old = head(repo)
    (repo / "a.txt").write_text("3b\n")
    git(repo, "commit", "-q", "-a", "--amend", "--no-edit")
    assert head(repo) != old


def test_plain_new_commit_and_fast_forward_still_work(repo):
    (repo / "a.txt").write_text("4\n")
    git(repo, "commit", "-q", "-am", "fourth")
    git(repo, "push", "-q", "origin", "HEAD")
    git(repo, "reset", "-q", "--hard", "HEAD")  # same-value reset is not a rewrite


def test_reset_hard_to_pre_push_commit_is_refused(repo):
    pushed = head(repo)
    r = git(repo, "reset", "--hard", "HEAD~1", check=False)
    assert r.returncode != 0
    assert MSG.format(old=pushed[:8]) in r.stderr
    assert head(repo) == pushed


def test_opt_out_env_allows_and_logs(repo):
    old = head(repo)
    (repo / "a.txt").write_text("amended\n")
    git(repo, "commit", "-q", "-a", "--amend", "--no-edit", env_extra={"ENGRAM_ALLOW_HISTORY_REWRITE": "1"})
    assert head(repo) != old
    log = (repo / "backup-rewrite.log").read_text().strip().splitlines()
    assert len(log) == 1 and "master" in log[0] and old[:8] in log[0]


def test_install_is_idempotent_and_heals_drift(repo):
    import engram_core
    hook = repo / ".git" / "hooks" / "reference-transaction"
    assert os.access(hook, os.X_OK)
    first = hook.read_bytes()
    assert engram_core._install_rewrite_guard() == "installed"
    assert hook.read_bytes() == first
    hook.write_bytes(first + b"# local drift of the managed file\n")  # still ours: healed
    assert engram_core._install_rewrite_guard() == "installed"
    assert hook.read_bytes() == first


def test_init_git_installs_the_guard(tmp_path, monkeypatch):
    import engram_core
    h = tmp_path / "fresh"
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    monkeypatch.setattr(engram_core, "_git_available", False)
    engram_core._init_git()
    hook = h / ".git" / "hooks" / "reference-transaction"
    assert hook.is_file() and os.access(hook, os.X_OK)
    first = hook.read_bytes()
    engram_core._init_git()  # second call on an existing repo
    assert hook.read_bytes() == first


def test_unsupported_git_installs_nothing_and_reports(tmp_path, monkeypatch):
    import engram_core
    h = tmp_path / "old"
    h.mkdir()
    git(h, "init", "-q", "-b", "master")
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    monkeypatch.setattr(engram_core, "_git_version", lambda: (2, 27, 0))
    assert engram_core._install_rewrite_guard() == "unsupported"
    assert not (h / ".git" / "hooks" / "reference-transaction").exists()


def test_opt_out_state_reported(repo, monkeypatch):
    import engram_core
    monkeypatch.setenv("ENGRAM_ALLOW_HISTORY_REWRITE", "1")
    assert engram_core._install_rewrite_guard() == "opt-out"


def test_foreign_reference_transaction_hook_is_not_overwritten(tmp_path, monkeypatch):
    import engram_core
    h = tmp_path / "foreign"
    h.mkdir()
    git(h, "init", "-q", "-b", "master")
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    hook = h / ".git" / "hooks" / "reference-transaction"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\n# someone else's hook\nexit 0\n")
    theirs = hook.read_bytes()
    assert engram_core._install_rewrite_guard() == "foreign-hook"
    assert hook.read_bytes() == theirs


def test_foreign_hook_state_lands_in_the_status_file(tmp_path, monkeypatch):
    import json
    import engram_core
    h = tmp_path / "foreign2"
    h.mkdir()
    git(h, "init", "-q", "-b", "master")
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    hook = h / ".git" / "hooks" / "reference-transaction"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    engram_core._write_backup_push_status(result="ok", stderr="", commit_sha="a", ahead=0, behind=0)
    assert json.loads((h / "backup-push-status.json").read_text())["rewrite_guard"] == "foreign-hook"


def test_hooks_path_config_is_honoured(tmp_path, monkeypatch):
    import engram_core
    h = tmp_path / "hp"
    h.mkdir()
    git(h, "init", "-q", "-b", "master")
    git(h, "config", "core.hooksPath", str(tmp_path / "myhooks"))
    monkeypatch.setattr(engram_core, "DATA_DIR", h)
    assert engram_core._install_rewrite_guard() == "installed"
    assert (tmp_path / "myhooks" / "reference-transaction").is_file()


def test_branch_creation_is_allowed(repo):
    git(repo, "branch", "topic")  # old value is all zeros
    assert git(repo, "rev-parse", "topic").stdout.strip() == head(repo)


def test_branch_deletion_is_allowed(repo):
    git(repo, "branch", "topic")  # points at a pushed commit
    r = git(repo, "branch", "-D", "topic", check=False)
    assert r.returncode == 0, r.stderr
    assert git(repo, "rev-parse", "--verify", "-q", "topic", check=False).returncode != 0
