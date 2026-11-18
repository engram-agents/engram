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
    pytest.param(
        "sk-ant-usr-" + base64.urlsafe_b64encode(os.urandom(68)).decode().rstrip("=")[:90],
        id="sk-ant-usr-random",
    ),
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


# -- #4214: the ahead-range scan must read merge commits ---------------------------
def _short(h, rev="HEAD"):
    return git(h, "rev-parse", "--short=8", rev)


def test_secret_only_in_merge_resolution_refuses_and_names_the_merge(amended):
    h, bare = amended
    git(h, "branch", "side")
    (h / "c.txt").write_text("main\n")
    git(h, "add", "c.txt")
    git(h, "commit", "-q", "-m", "main edits c")
    git(h, "checkout", "-q", "side")
    (h / "c.txt").write_text("side\n")
    git(h, "add", "c.txt")
    git(h, "commit", "-q", "-m", "side edits c")
    git(h, "checkout", "-q", "master")
    m = subprocess.run(["git", "merge", "--no-edit", "side"], cwd=h, capture_output=True, text=True)
    assert m.returncode != 0  # a real conflict
    (h / "c.txt").write_text("token = " + _synthetic_key() + "\n")  # in neither parent
    git(h, "add", "c.txt")
    git(h, "commit", "-q", "--no-edit")
    merge = _short(h)
    git(h, "rm", "-q", "c.txt")
    git(h, "commit", "-q", "-m", "drop c")
    assert "c.txt" not in git(h, "ls-files")  # tree clean; only the merge's diff carries it
    before = git(bare, "rev-parse", "master")
    r = run(h, "--repair")
    assert r.returncode == 2
    assert merge in r.stdout + r.stderr
    # `-m` prints the merge's diff once per parent; the per-commit dedupe must list the merge once.
    assert (r.stdout + r.stderr).count(f"commit {merge}") == 1
    assert git(bare, "rev-parse", "master") == before


def test_secret_on_side_branch_then_merged_refuses(amended):
    """A regression guard, not a `-m` discriminator: the side commit is an ordinary commit in the
    range, so this passes with or without `-m`. The next cells carry the discrimination."""
    h, bare = amended
    git(h, "checkout", "-q", "-b", "side")
    (h / "k.txt").write_text("token = " + _synthetic_key() + "\n")
    git(h, "add", "k.txt")
    git(h, "commit", "-q", "-m", "side adds key")
    git(h, "checkout", "-q", "master")
    (h / "m.txt").write_text("main\n")
    git(h, "add", "m.txt")
    git(h, "commit", "-q", "-m", "main work")
    git(h, "merge", "-q", "--no-edit", "side")
    git(h, "rm", "-q", "k.txt")
    git(h, "commit", "-q", "-m", "drop key")
    assert run(h, "--repair").returncode == 2


def test_clean_merge_in_ahead_range_does_not_refuse(amended):
    """A control for the false-positive direction (a clean merge is not refused and is not listed
    twice). No mutant of the log flags reddened it, so it guards the property; it does not discriminate."""
    h, bare = amended
    git(h, "checkout", "-q", "-b", "side")
    (h / "s.txt").write_text("side\n")
    git(h, "add", "s.txt")
    git(h, "commit", "-q", "-m", "side work")
    git(h, "checkout", "-q", "master")
    (h / "m.txt").write_text("main\n")
    git(h, "add", "m.txt")
    git(h, "commit", "-q", "-m", "main work")
    git(h, "merge", "-q", "--no-edit", "side")
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr


def test_secret_on_side_branch_deleted_by_merge_resolution_refuses(amended):
    """Discriminates `-m` from `--first-parent -m`: the key lives only on a side-branch
    commit, and the merge resolution drops it."""
    h, bare = amended
    git(h, "checkout", "-q", "-b", "side")
    (h / "k.txt").write_text("token = " + _synthetic_key() + "\n")
    git(h, "add", "k.txt")
    git(h, "commit", "-q", "-m", "side adds key")
    side = _short(h)
    git(h, "checkout", "-q", "master")
    (h / "m.txt").write_text("main\n")
    git(h, "add", "m.txt")
    git(h, "commit", "-q", "-m", "main work")
    git(h, "merge", "-q", "--no-edit", "-s", "ours", "side")
    assert "k.txt" not in git(h, "ls-files")
    r = run(h, "--repair")
    assert r.returncode == 2
    assert side in r.stdout + r.stderr


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


# -- #4214 / #4875: added lines the ahead-range scan must still see ---------------
def _key_line():
    return "token = " + _synthetic_key()


def _plant_then_delete(h, name, content, msg="add key"):
    """Commit `content` (bytes) as `name` in the ahead range, delete it in a LATER commit, so the
    tracked-tree scan cannot see it and only the history scan can.  -> short sha of the add."""
    (h / name).write_bytes(content)
    git(h, "add", name)
    git(h, "commit", "-q", "-m", msg)
    added = _short(h)
    git(h, "rm", "-q", name)
    git(h, "commit", "-q", "-m", "remove " + name)
    assert name not in git(h, "ls-files").split("\n")
    return added


def _expect_refused(h, bare, added):
    before = git(bare, "rev-parse", "master")
    r = run(h, "--repair")
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert added in out
    assert "token" not in out
    assert git(bare, "rev-parse", "master") == before


def test_added_ordinary_key_line_is_flagged_control(amended):
    h, bare = amended
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", (_key_line() + "\n").encode()))


def test_added_line_starting_with_two_plus_is_flagged(amended):
    # the diff line is `+++token = ...`, which the old `not startswith("+++")` filter dropped
    h, bare = amended
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", ("++" + _key_line() + "\n").encode()))


def test_added_line_starting_with_three_plus_is_flagged(amended):
    h, bare = amended
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", ("+++" + _key_line() + "\n").encode()))


@pytest.mark.parametrize("setting", ["color.ui", "color.diff"])
def test_key_is_flagged_when_repo_config_forces_colour(amended, setting):
    h, bare = amended
    git(h, "config", setting, "always")
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", (_key_line() + "\n").encode()))


def test_forced_colour_on_a_clean_history_does_not_refuse(amended):
    h, bare = amended
    git(h, "config", "color.ui", "always")
    (h / "ok.txt").write_text("plain text\n")
    git(h, "add", "ok.txt")
    git(h, "commit", "-q", "-m", "clean work")
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr


def test_key_in_a_binary_file_is_flagged(amended):
    h, bare = amended
    _expect_refused(h, bare, _plant_then_delete(h, "k.bin", b"\x00\n" + (_key_line() + "\n").encode()))


def test_binary_file_without_a_key_does_not_refuse(amended):
    h, bare = amended
    (h / "k.bin").write_bytes(b"\x00\nnothing here\n")
    git(h, "add", "k.bin")
    git(h, "commit", "-q", "-m", "binary, clean")
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr


def test_key_is_flagged_when_a_textconv_driver_prints_nothing(amended):
    # `diff.<name>.textconv` plus an attributes line (not committed) changes what `git log -p` prints
    h, bare = amended
    git(h, "config", "diff.hide.textconv", "true")  # `true` ignores its argument and prints nothing
    with open(h / ".git" / "info" / "attributes", "a") as fh:
        fh.write("*.txt diff=hide\n")
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", (_key_line() + "\n").encode()))


# header-parse controls: green before and after, they pin that header skipping eats no hunk line
def test_key_as_first_added_line_of_a_new_file_is_flagged(amended):
    h, bare = amended
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", (_key_line() + "\nsecond\n").encode()))


def test_key_in_the_second_file_of_a_commit_is_flagged(amended):
    h, bare = amended
    (h / "a1.txt").write_text("harmless\n")
    (h / "b1.txt").write_text(_key_line() + "\n")
    git(h, "add", "a1.txt", "b1.txt")
    git(h, "commit", "-q", "-m", "two files")
    added = _short(h)
    git(h, "rm", "-q", "a1.txt", "b1.txt")
    git(h, "commit", "-q", "-m", "remove both")
    _expect_refused(h, bare, added)


def test_header_state_does_not_leak_into_the_next_commit(amended):
    """A mutant that forgets to reset at `@@commit` keeps header state across commits.  One
    commit is a pure rename (its diff ends in a header with no hunk).  `git log` prints newest
    first, so the rename is printed right BEFORE the key commit; a leaked header state would
    swallow the key commit's first hunk line."""
    h, bare = amended
    (h / "r1.txt").write_text("same\n")
    git(h, "add", "r1.txt")
    git(h, "commit", "-q", "-m", "add r1")
    (h / "k.txt").write_text(_key_line() + "\n")
    git(h, "add", "k.txt")
    git(h, "commit", "-q", "-m", "add key")
    added = _short(h)
    git(h, "mv", "r1.txt", "r2.txt")
    git(h, "commit", "-q", "-m", "pure rename")
    git(h, "rm", "-q", "k.txt")
    git(h, "commit", "-q", "-m", "remove key")
    assert "k.txt" not in git(h, "ls-files").split("\n")
    _expect_refused(h, bare, added)


# -- #4214: bytes that are not valid UTF-8, and separators that are not newlines -----
_BAD_BYTES = bytes(range(128, 256)) * 4 + b"\x00"


def test_invalid_utf8_binary_file_without_a_key_does_not_crash_or_refuse(amended):
    h, bare = amended
    _plant_then_delete(h, "blob.bin", _BAD_BYTES)
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Traceback" not in r.stderr


def test_key_after_invalid_utf8_bytes_is_flagged(amended):
    h, bare = amended
    added = _plant_then_delete(h, "blob.bin", _BAD_BYTES + b"\xff\xfe\n" + (_key_line() + "\n").encode())
    _expect_refused(h, bare, added)


@pytest.mark.parametrize("sep", [b"\x0c", b"\x0b", b"\x1c", b"\xc2\x85", b"\xe2\x80\xa8"],
                         ids=["ff", "vt", "fs", "nel", "ls"])
def test_key_after_a_line_ending_in_a_diff_header_lookalike_is_flagged(amended, sep):
    # `str.splitlines()` also breaks at these separators, which would turn the tail of the first
    # line into a standalone `diff --git ` line and open the header state over the key line
    h, bare = amended
    content = b"harmless" + sep + b"diff --git a/x b/x\n" + (_key_line() + "\n").encode()
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", content))


def test_crlf_file_with_a_key_line_is_flagged(amended):
    h, bare = amended
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", ("harmless\r\n" + _key_line() + "\r\n").encode()))


def test_clean_file_containing_a_form_feed_does_not_refuse(amended):
    h, bare = amended
    (h / "ff.txt").write_bytes(b"page one\x0cpage two\n")
    git(h, "add", "ff.txt")
    git(h, "commit", "-q", "-m", "form feed")
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr


# -- #4214: the log output must reach the parser byte for byte --------------------------
def test_key_after_a_lone_carriage_return_is_flagged(amended):
    # text-mode pipes translate a lone "\r" into "\n", which strips the "+" off the key line
    h, bare = amended
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", b"pad\r" + (_key_line() + "\n").encode()))


def test_key_after_several_carriage_returns_is_flagged(amended):
    h, bare = amended
    _expect_refused(h, bare, _plant_then_delete(h, "k.txt", b"a\rb\r" + (_key_line() + "\n").encode()))


def test_clean_cr_only_file_does_not_refuse(amended):
    h, bare = amended
    (h / "cr.txt").write_bytes(b"one\rtwo\rthree\n")
    git(h, "add", "cr.txt")
    git(h, "commit", "-q", "-m", "cr only")
    r = run(h, "--repair")
    assert r.returncode == 0, r.stdout + r.stderr


def _load_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("backup_remote_repair_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_byte_value_before_a_key_line_still_flags_the_commit(tmp_path):
    """The class cell: one repo, one file replaced in each of 255 commits (every byte value but
    "\\n", followed at once by a key line), one scan.  Every commit must be flagged, whatever
    the byte layer does with that byte (decoding, newline translation, line splitting)."""
    h = tmp_path / "repo"
    h.mkdir()
    git(h, "init", "-q", "-b", "master")
    git(h, "config", "user.email", "t@example.invalid")
    git(h, "config", "user.name", "t")
    git(h, "config", "commit.gpgsign", "false")
    (h / "base.txt").write_text("base\n")
    git(h, "add", "base.txt")
    git(h, "commit", "-q", "-m", "base")
    key_line = (_key_line() + "\n").encode()
    values = [b for b in range(256) if b != 0x0A]
    for b in values:
        (h / "f.bin").write_bytes(bytes([b]) + key_line)
        git(h, "add", "f.bin")
        git(h, "commit", "-q", "-m", f"byte {b:#04x}")
    shas = git(h, "rev-list", "--reverse", "master", "^master~255").split("\n")
    assert len(shas) == 255
    hits = set(_load_tool()._added_line_hits(str(h), "master~255..master"))
    missed = [f"{b:#04x}" for b, s in zip(values, shas) if s[:8] not in hits]
    assert not missed, f"commits not flagged for bytes: {missed}"
