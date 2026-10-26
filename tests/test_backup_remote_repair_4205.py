"""Contract tests for #4205 part 3: the explicit repair verb.

Written before the tool.  ``src/engram/tools/backup_remote_repair.py`` refuses by
default; it force-pushes (with lease) only when the tracked tree is secret-clean
and every remote-only commit is a same-message twin of a local commit (the amend
signature).  Tmp repos and a bare tmp remote only.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "src" / "engram" / "tools" / "backup_remote_repair.py"
STATUS = "backup-push-status.json"


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def run(home, *args):
    env = {k: v for k, v in os.environ.items() if k != "ENGRAM_HOME"}
    return subprocess.run(
        [sys.executable, str(TOOL), "--home", str(home), *args],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def amended(tmp_path):
    """Local history whose last pushed commit was then amended: remote holds the
    pre-amend twin.  -> (home, bare)."""
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
    git(h, "commit", "-q", "-m", "base")
    git(h, "push", "-q", "-u", "origin", "master")
    (h / "a.txt").write_text("2\n")
    git(h, "commit", "-q", "-am", "turn advance")
    git(h, "push", "-q", "origin", "master")
    (h / "a.txt").write_text("2-amended\n")
    git(h, "commit", "-q", "-a", "--amend", "--no-edit")
    # a later local commit on top
    (h / "a.txt").write_text("3\n")
    git(h, "commit", "-q", "-am", "later")
    return h, bare


def test_status_output_shape(amended):
    h, _ = amended
    (h / STATUS).write_text(json.dumps({"result": "refused", "last_success_utc": None}))
    r = run(h, "--status")
    assert r.returncode == 0, r.stderr
    assert '"result": "refused"' in r.stdout
    assert "ahead 2 behind 1" in r.stdout  # live, from git (fetches first)


def test_status_without_status_file(amended):
    h, _ = amended
    r = run(h, "--status")
    assert r.returncode == 0 and "absent" in r.stdout


def test_repair_refuses_on_secret_hit(amended):
    h, bare = amended
    (h / "leak.txt").write_text("token = ghp_" + "a" * 24 + "\n")
    git(h, "add", "leak.txt")
    git(h, "commit", "-q", "-m", "oops")
    before = git(bare, "rev-parse", "master")
    r = run(h, "--repair")
    assert r.returncode == 2
    assert "leak.txt" in r.stdout + r.stderr
    assert git(bare, "rev-parse", "master") == before


@pytest.mark.parametrize("secret", [
    "sk-ant-usr-" + base64.urlsafe_b64encode(os.urandom(68)).decode().rstrip("=")[:90],
    "sk-ant-api03-" + "A" * 24,
    "github_pat_" + "B" * 24,
    "AKIA" + "C" * 16,
    "-----BEGIN OPENSSH PRIVATE KEY",
])
def test_repair_refuses_on_each_secret_form(amended, secret):
    h, _ = amended
    (h / "s.txt").write_text(secret + "\n")
    git(h, "add", "s.txt")
    git(h, "commit", "-q", "-m", "s")
    assert run(h, "--repair").returncode == 2


def test_repair_refuses_on_non_twin_remote_only_commit(amended, tmp_path):
    h, bare = amended
    # replace the twin situation by adding a genuinely different remote-only commit
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(bare), str(other))
    git(other, "config", "user.email", "t@example.invalid")
    git(other, "config", "user.name", "t")
    (other / "x.txt").write_text("x")
    git(other, "add", "x.txt")
    git(other, "commit", "-q", "-m", "somebody else's work")
    git(other, "push", "-q", "origin", "master")
    before = git(bare, "rev-parse", "master")
    r = run(h, "--repair")
    assert r.returncode == 3
    assert "somebody else's work" in r.stdout
    assert git(bare, "rev-parse", "master") == before


def test_repair_pushes_twin_case_and_updates_status(amended):
    h, bare = amended
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(bare, "rev-parse", "master") == git(h, "rev-parse", "HEAD")
    assert "ahead 0 behind 0" in r.stdout
    st = json.loads((h / STATUS).read_text())
    assert st["result"] == "ok" and (st["ahead"], st["behind"]) == (0, 0)
    assert st["last_success_utc"]


def test_repair_is_a_noop_when_not_diverged(tmp_path, amended):
    h, _ = amended
    assert run(h, "--repair").returncode == 0
    r = run(h, "--repair")
    assert r.returncode == 0 and "ahead 0 behind 0" in r.stdout


def test_no_action_flag_prints_usage_and_does_nothing(amended):
    h, bare = amended
    before = git(bare, "rev-parse", "master")
    r = run(h)
    assert r.returncode != 0
    assert git(bare, "rev-parse", "master") == before


def _synthetic_key():
    return "sk-ant-usr-" + base64.urlsafe_b64encode(os.urandom(68)).decode().rstrip("=")[:90]


def test_control_passes_when_every_key_file_matches(amended):
    h, bare = amended
    (h / "secrets").mkdir()
    (h / "secrets" / "k1").write_text(_synthetic_key() + "\n")
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(bare, "rev-parse", "master") == git(h, "rev-parse", "HEAD")


def test_control_failure_refuses_and_names_counts(amended):
    h, bare = amended
    (h / "secrets").mkdir()
    (h / "secrets" / "odd").write_text("not shaped like any known key\n")
    before = git(bare, "rev-parse", "master")
    r = run(h, "--repair")
    assert r.returncode == 5
    assert "control failed: pattern matched 0 of 1 key files" in r.stdout + r.stderr
    assert git(bare, "rev-parse", "master") == before


def test_control_failure_when_only_some_key_files_match(amended):
    h, _ = amended
    (h / "secrets").mkdir()
    (h / "secrets" / "good").write_text(_synthetic_key() + "\n")
    (h / "secrets" / "odd").write_text("nope\n")
    r = run(h, "--repair")
    assert r.returncode == 5
    assert "control failed: pattern matched 1 of 2 key files" in r.stdout + r.stderr


def test_key_file_contents_are_never_printed(amended):
    h, _ = amended
    key = _synthetic_key()
    (h / "secrets").mkdir()
    (h / "secrets" / "k").write_text(key + "\n")
    (h / "leak.txt").write_text(key + "\n")
    git(h, "add", "leak.txt")
    git(h, "commit", "-q", "-m", "oops")
    r = run(h, "--repair")
    assert r.returncode == 2
    assert key not in r.stdout + r.stderr


def test_repair_refuses_on_oversize_tracked_blob_and_names_path(amended):
    h, bare = amended
    (h / "big.bin").write_bytes(b"\0" * 5000)
    git(h, "add", "big.bin")
    git(h, "commit", "-q", "-m", "big")
    before = git(bare, "rev-parse", "master")
    r = run(h, "--repair", "--max-blob-bytes", "1000")
    assert r.returncode == 4
    assert "big.bin" in r.stdout
    assert git(bare, "rev-parse", "master") == before
    assert "big.bin" in git(h, "ls-files")  # never rewrites history or the index


def test_default_blob_limit_is_100_mb(amended):
    h, _ = amended
    (h / "mid.bin").write_bytes(b"\0" * 5000)
    git(h, "add", "mid.bin")
    git(h, "commit", "-q", "-m", "mid")
    assert run(h, "--repair").returncode == 0


# -- round 2: ahead-range history scan, plain push, unproven control --------------
def test_secret_committed_then_deleted_in_ahead_range_still_refuses(amended):
    h, bare = amended
    (h / "tmp.txt").write_text("token = " + _synthetic_key() + "\n")
    git(h, "add", "tmp.txt")
    git(h, "commit", "-q", "-m", "add key")
    git(h, "rm", "-q", "tmp.txt")
    git(h, "commit", "-q", "-m", "remove key")
    assert "tmp.txt" not in git(h, "ls-files")  # the tree is clean; only history carries it
    before = git(bare, "rev-parse", "master")
    r = run(h, "--repair")
    assert r.returncode == 2
    assert git(bare, "rev-parse", "master") == before
    assert "token" not in r.stdout + r.stderr


@pytest.fixture
def ahead_only(tmp_path):
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
    git(h, "commit", "-q", "-m", "base")
    git(h, "push", "-q", "-u", "origin", "master")
    (h / "a.txt").write_text("2\n")
    git(h, "commit", "-q", "-am", "more")
    return h, bare


def test_behind_zero_does_plain_push(ahead_only):
    h, bare = ahead_only
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing diverged; plain push" in r.stdout
    assert git(bare, "rev-parse", "master") == git(h, "rev-parse", "HEAD")
    assert "ahead 0 behind 0" in r.stdout


def test_control_with_no_key_files_prints_unproven_and_continues(ahead_only):
    h, bare = ahead_only
    (h / "secrets").mkdir()  # exists, empty
    r = run(h, "--repair")
    assert r.returncode == 0
    assert "control: no key files under secrets/; pattern unproven on this seat" in r.stdout


def test_control_with_no_secrets_dir_is_also_unproven(ahead_only):
    h, _ = ahead_only
    r = run(h, "--repair")
    assert "pattern unproven on this seat" in r.stdout
