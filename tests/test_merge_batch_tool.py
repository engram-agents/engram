"""merge_batch tool: the merge seat's batched press, with its gates made mechanical.

Contract-first: these cells were written before the implementation.

Real git in temp repos for build / tree cells; a fake gh client for everything
GitHub-side. Cells (numbering follows the spec):

  1  strict read shapes (baton_gate._strict_required_check_state)
  2  single home: the tool's CI read IS the baton_gate function; no second reader
  3  build: step trees equal the real merge trees; a conflicting PR stops the build
  4  record-run refuses a --tree that is not the final step tree
  5  press refuses with no recorded window seq
  6  press: UNKNOWN + pending twice, then success -> presses (poll-then-succeed)
  7  press: UNKNOWN + pending throughout -> refuses at the ceiling
  8  press: base tip moved -> refuses before merging
  9  press: no non-author APPROVED review on the head -> refuses
  10 press: post-merge tree differs from the step tree -> stops, next step unpressed
  11 planted flake: red cell in an untouched file stops the press; --accept-red presses
  12 happy path: two steps pressed, base and tree asserted each step, exit 0
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_TOOLS_DIR = str(_ROOT / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.append(_TOOLS_DIR)

import baton_gate  # noqa: E402

REPO = "example-org/example-repo"
BASE = "dev"
AUTHOR = "author1"
REVIEWER = "reviewer1"


def _mb():
    import merge_batch
    return merge_batch


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------

def git(cwd, *args, check=True):
    res = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True)
    if check and res.returncode != 0:
        raise AssertionError(f"git {args} failed: {res.stderr}")
    return res.stdout.strip()


def make_remote(tmp_path, prs):
    """Bare remote with `dev` plus refs/pull/N/head for each (n, {file: text}) PR."""
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "--bare", "-b", BASE, str(bare))
    git(tmp_path, "init", "-q", "-b", BASE, str(seed))
    (seed / "base.txt").write_text("base\n")
    (seed / "shared.txt").write_text("one\ntwo\nthree\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "base")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", BASE)
    for n, files in prs:
        git(seed, "checkout", "-q", "-b", f"pr{n}", BASE)
        for name, text in files.items():
            (seed / name).write_text(text)
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", f"pr {n}")
        git(seed, "push", "-q", "origin", f"pr{n}:refs/pull/{n}/head")
    return bare


def junit(path, passed=3, failed=()):
    cases = "".join(f'<testcase classname="tests.test_ok" name="test_{i}"/>' for i in range(passed))
    for cls, name in failed:
        cases += f'<testcase classname="{cls}" name="{name}"><failure message="boom"/></testcase>'
    path.write_text(
        f'<testsuites><testsuite name="pytest" tests="{passed + len(failed)}" '
        f'failures="{len(failed)}">{cases}</testsuite></testsuites>')
    return path


# ---------------------------------------------------------------------------
# fake gh client
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start=1_800_000_000.0):
        self.t = start
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps += 1
        if self.sleeps > 2000:
            raise RuntimeError("runaway poll: no ceiling")
        self.t += s


def runs_payload(*runs, total=None):
    return {"total_count": len(runs) if total is None else total, "check_runs": list(runs)}


def cr(id_, status="completed", conclusion="success", name="pr-checks"):
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion}


GREEN = runs_payload(cr(1))
PENDING = runs_payload(cr(1, status="in_progress", conclusion=None))


class FakeGh:
    def __init__(self, remote, tmp_path):
        self.remote = remote
        self.wc = tmp_path / "fake_wc"
        self.merges = []
        self.base_calls = 0
        self.runs_calls = 0
        self.runs_calls_at_merge = []
        self.merge_states = ["CLEAN"]
        self.runs_seq = [GREEN]
        self.author = AUTHOR
        self.reviews_override = None
        self.base_override = None
        self.head_override = {}
        self.tamper_on = set()
        self.stack = set()
        self.merge_ok = True
        self.merge_raises = None
        self.interleave_on = set()

    # -- reads
    def _ref(self, ref):
        return git(self.remote, "rev-parse", ref)

    def base_tip(self, repo, base):
        self.base_calls += 1
        return self.base_override or self._ref(f"refs/heads/{base}")

    def pr_head(self, repo, n):
        return self.head_override.get(int(n)) or self._ref(f"refs/pull/{n}/head")

    def pr_author(self, repo, n):
        return self.author

    def reviews(self, repo, n):
        if self.reviews_override is not None:
            return self.reviews_override
        return [{"user": {"login": REVIEWER}, "state": "APPROVED",
                 "commit_id": self.pr_head(repo, n)}]

    def merge_state(self, repo, n):
        s = self.merge_states
        return s.pop(0) if len(s) > 1 else s[0]

    def check_runs(self, repo, sha):
        self.runs_calls += 1
        s = self.runs_seq
        return s.pop(0) if len(s) > 1 else s[0]

    def pr_stack(self, repo, n):
        return int(n) in self.stack

    # -- write: a real squash merge into the bare remote
    def merge(self, repo, n, head):
        self.runs_calls_at_merge.append(self.runs_calls)
        self.merges.append((int(n), head))
        if self.merge_raises is not None:
            raise self.merge_raises
        if not self.merge_ok:
            return False, "refused"
        if not self.wc.exists():
            git(self.wc.parent, "clone", "-q", str(self.remote), str(self.wc))
        git(self.wc, "fetch", "-q", "origin", f"+refs/heads/{BASE}:refs/remotes/origin/{BASE}",
            f"refs/pull/{n}/head")
        git(self.wc, "checkout", "-q", "-B", "land", f"origin/{BASE}")
        if int(n) in self.interleave_on:   # someone else pushes an empty commit first
            git(self.wc, "commit", "-q", "--allow-empty", "-m", "interleaved push")
            git(self.wc, "push", "-q", "origin", f"HEAD:refs/heads/{BASE}")
        git(self.wc, "merge", "-q", "--squash", head)
        if int(n) in self.tamper_on:
            (self.wc / "tamper.txt").write_text("extra\n")
            git(self.wc, "add", ".")
        git(self.wc, "commit", "-q", "-m", f"squash #{n}")
        git(self.wc, "push", "-q", "origin", f"HEAD:refs/heads/{BASE}")
        return True, "merged"


class Env:
    def __init__(self, tmp_path, prs=((1, {"a.txt": "a\n"}), (2, {"b.txt": "b\n"}))):
        self.tmp = tmp_path
        self.prs = prs
        self.remote = make_remote(tmp_path, prs)
        self.state = tmp_path / "state.json"
        self.scratch = tmp_path / "scratch"
        self.gh = FakeGh(self.remote, tmp_path)
        self.clock = FakeClock()

    def run(self, *argv, state=True):
        mb = _mb()
        args = list(argv)
        if state:
            args = [args[0], "--state", str(self.state), *args[1:]]
        return mb.main(args, client=self.gh, clock=self.clock.now, sleep=self.clock.sleep)

    def build(self):
        nums = ",".join(str(n) for n, _ in self.prs)
        return self.run("build", "--repo", REPO, "--base", BASE, "--prs", nums,
                        "--scratch", str(self.scratch), "--remote", str(self.remote))

    def st(self):
        return json.loads(self.state.read_text())

    def record_green(self, **kw):
        j = junit(self.tmp / "junit.xml", **kw)
        return self.run("record-run", "--junit", str(j), "--rc", "0" if not kw.get("failed") else "1",
                        "--tree", self.st()["steps"][-1]["tree"])

    def window(self, seq="seq-7"):
        assert self.run("window", "--minutes", "30") == 0
        assert self.run("window", "--record-seq", seq) == 0

    def ready(self):
        assert self.build() == 0
        assert self.record_green() == 0
        self.window()

    def press(self, *extra):
        return self.run("press", "--interval", "10", *extra)


# ---------------------------------------------------------------------------
# 1. strict read shapes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,payload,expected", [
    ("none", runs_payload(), "none"),
    ("other-name-only", runs_payload(cr(1, name="other")), "none"),
    ("one-success", runs_payload(cr(1)), "success"),
    ("success+in_progress", runs_payload(cr(1), cr(2, status="in_progress", conclusion=None)), "pending"),
    ("queued-alone", runs_payload(cr(1, status="queued", conclusion=None)), "pending"),
    ("cancelled-then-success", runs_payload(cr(1, conclusion="cancelled"), cr(2)), "not-all-success"),
    ("success-then-cancelled", runs_payload(cr(1), cr(2, conclusion="cancelled")), "not-all-success"),
    ("failure+success", runs_payload(cr(1, conclusion="failure"), cr(2)), "not-all-success"),
    ("skipped", runs_payload(cr(1, conclusion="skipped")), "not-all-success"),
    ("too-many", runs_payload(cr(1), total=101), "too-many"),
    ("total-count-absent", {"check_runs": [cr(1)]}, "unknown"),
    ("total-count-string", {"total_count": "101", "check_runs": [cr(1)]}, "unknown"),
    ("total-count-null", {"total_count": None, "check_runs": [cr(1)]}, "unknown"),
    ("total-count-bool", {"total_count": True, "check_runs": [cr(1)]}, "unknown"),
    ("not-an-object", {"error": "boom"}, "unknown"),
])
def test_cell1_strict_read_shapes(label, payload, expected):
    state, detail = baton_gate._strict_required_check_state(payload, "pr-checks")
    assert state == expected, (label, state, detail)
    assert isinstance(detail, str) and detail


def test_cell1_detail_names_offending_runs():
    _, detail = baton_gate._strict_required_check_state(
        runs_payload(cr(11, conclusion="cancelled"), cr(12)), "pr-checks")
    assert "11" in detail and "cancelled" in detail


def test_cell1_conclusion_case_insensitive():
    state, _ = baton_gate._strict_required_check_state(runs_payload(cr(1, conclusion="SUCCESS")), "pr-checks")
    assert state == "success"


# ---------------------------------------------------------------------------
# 2. single home
# ---------------------------------------------------------------------------

def test_cell2_single_home():
    mb = _mb()
    assert mb._strict_required_check_state is baton_gate._strict_required_check_state
    src = Path(mb.__file__).read_text()
    names = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}
    assert "_strict_required_check_state" not in names
    assert not any("dedupe" in n for n in names)
    for banned in ("completed_at", "completedAt", "started_at", "updated_at", ".sort(", "sorted(", "conclusion"):
        assert banned not in src, f"tool must not read or order runs itself: {banned}"


# ---------------------------------------------------------------------------
# 3. build
# ---------------------------------------------------------------------------

def test_cell3_build_step_trees_match_real_merges(tmp_path):
    env = Env(tmp_path)
    assert env.build() == 0
    st = env.st()
    repo_dir = Path(st["worktree"])
    assert [s["pr"] for s in st["steps"]] == [1, 2]
    assert st["base_sha"] == git(env.remote, "rev-parse", f"refs/heads/{BASE}")
    assert st["steps"][1]["tree"] == git(repo_dir, "rev-parse", "HEAD^{tree}")
    assert st["steps"][0]["tree"] == git(repo_dir, "rev-parse", "HEAD~1^{tree}")
    assert st["steps"][0]["tree"] != st["steps"][1]["tree"]
    for s in st["steps"]:
        assert s["head"] == git(env.remote, "rev-parse", f"refs/pull/{s['pr']}/head")


def test_cell3_conflict_stops_build_and_names_pr(tmp_path, capsys):
    prs = ((1, {"shared.txt": "one\nTWO-from-1\nthree\n"}), (2, {"shared.txt": "one\nTWO-from-2\nthree\n"}),
           (3, {"c.txt": "c\n"}))
    env = Env(tmp_path, prs)
    rc = env.build()
    out = capsys.readouterr()
    assert rc != 0
    assert "#2" in out.out + out.err
    assert not env.state.exists() or all(s["pr"] != 3 for s in env.st().get("steps", []))


def test_cell3_build_never_touches_callers_clone(tmp_path):
    env = Env(tmp_path)
    assert env.build() == 0
    assert Path(env.st()["worktree"]).resolve().is_relative_to(env.scratch.resolve())


# ---------------------------------------------------------------------------
# 4. record-run
# ---------------------------------------------------------------------------

def test_cell4_record_run_refuses_wrong_tree(tmp_path):
    env = Env(tmp_path)
    assert env.build() == 0
    j = junit(tmp_path / "junit.xml")
    first_step_tree = env.st()["steps"][0]["tree"]
    assert env.run("record-run", "--junit", str(j), "--rc", "0", "--tree", first_step_tree) != 0
    assert not env.st().get("run")
    assert env.run("record-run", "--junit", str(j), "--rc", "0", "--tree", env.st()["steps"][-1]["tree"]) == 0
    assert env.st()["run"]["failures"] == 0


def test_cell4_record_run_stores_failing_ids(tmp_path):
    env = Env(tmp_path)
    assert env.build() == 0
    j = junit(tmp_path / "junit.xml", failed=[("tests.test_x", "test_flaky")])
    assert env.run("record-run", "--junit", str(j), "--rc", "1", "--tree", env.st()["steps"][-1]["tree"]) == 0
    run = env.st()["run"]
    assert run["failures"] == 1 and run["errors"] == 0
    assert run["failing"] == ["tests.test_x::test_flaky"]


# ---------------------------------------------------------------------------
# 5-12 press
# ---------------------------------------------------------------------------

def test_cell5_press_refuses_without_window_seq(tmp_path):
    env = Env(tmp_path)
    assert env.build() == 0
    assert env.record_green() == 0
    assert env.run("window", "--minutes", "30") == 0   # printed but not recorded
    assert env.press() != 0
    assert env.gh.merges == []


def test_cell6_poll_then_succeed(tmp_path):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    env.gh.merge_states = ["UNKNOWN"]
    env.gh.runs_seq = [PENDING, PENDING, GREEN]
    assert env.press() == 0
    assert len(env.gh.merges) == 1
    assert env.gh.runs_calls_at_merge == [3]


def test_cell7_refuses_at_ceiling(tmp_path, capsys):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    env.gh.merge_states = ["UNKNOWN"]
    env.gh.runs_seq = [PENDING]
    capsys.readouterr()
    rc = env.press("--ceiling", "60")
    out = capsys.readouterr()
    assert rc != 0
    assert env.gh.merges == []
    text = (out.out + out.err)
    assert "UNKNOWN" in text and "pending" in text


def test_cell8_base_moved_refuses_before_merge(tmp_path):
    env = Env(tmp_path)
    env.ready()
    env.gh.base_override = "f" * 40
    assert env.press() != 0
    assert env.gh.merges == []


@pytest.mark.parametrize("variant", ["stale-commit", "author-only", "none"])
def test_cell9_requires_current_non_author_approval(tmp_path, variant):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    head = env.gh.pr_head(REPO, 1)
    env.gh.reviews_override = {
        "stale-commit": [{"user": {"login": REVIEWER}, "state": "APPROVED", "commit_id": "0" * 40}],
        "author-only": [{"user": {"login": AUTHOR}, "state": "APPROVED", "commit_id": head}],
        "none": [],
    }[variant]
    assert env.press() != 0
    assert env.gh.merges == []


def test_cell10_post_merge_tree_mismatch_stops(tmp_path, capsys):
    env = Env(tmp_path)
    env.ready()
    env.gh.tamper_on = {1}
    rc = env.press()
    assert rc != 0
    assert [m[0] for m in env.gh.merges] == [1]
    assert not env.st()["steps"][1].get("merge_sha")
    err = capsys.readouterr().err
    assert "landing mismatch" in err and "#1" in err


def test_cell11_planted_flake_stops_then_accept_red(tmp_path, capsys):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    assert env.build() == 0
    assert env.record_green(failed=[("tests.test_untouched", "test_flaky")]) == 0
    env.window()
    capsys.readouterr()
    assert env.press() != 0
    out = capsys.readouterr()
    assert "tests.test_untouched::test_flaky" in out.out + out.err
    assert env.gh.merges == []
    assert env.press("--accept-red", "") != 0
    assert env.gh.merges == []
    assert env.press("--accept-red", "RULING-17222") == 0
    assert len(env.gh.merges) == 1
    assert env.st()["accept_red"] == "RULING-17222"


def test_cell12_happy_path_two_steps(tmp_path):
    env = Env(tmp_path)
    env.ready()
    assert env.press() == 0
    assert [m[0] for m in env.gh.merges] == [1, 2]
    assert env.gh.base_calls >= 2
    st = env.st()
    tip = git(env.remote, "rev-parse", f"refs/heads/{BASE}")
    assert st["steps"][1]["merge_sha"] == tip
    assert git(env.remote, "rev-parse", f"{tip}^") == st["steps"][0]["merge_sha"]
    assert git(env.remote, "rev-parse", f"{st['steps'][0]['merge_sha']}^") == st["base_sha"]
    assert git(env.remote, "rev-parse", f"{tip}^{{tree}}") == st["steps"][1]["tree"]
    assert all(s["pressed"] for s in st["steps"])


def test_window_first_line_format(tmp_path, capsys):
    env = Env(tmp_path)
    assert env.build() == 0
    capsys.readouterr()
    assert env.run("window", "--minutes", "30") == 0
    first = capsys.readouterr().out.splitlines()[0]
    import re
    assert re.fullmatch(r"dev moving, 2 presses \(#1, #2\), until \d\d:\d\dZ", first), first


def test_stack_member_refused(tmp_path):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    env.gh.stack = {1}
    assert env.press() != 0
    assert env.gh.merges == []


def test_head_moved_refused(tmp_path):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    env.gh.head_override = {1: "e" * 40}
    assert env.press() != 0
    assert env.gh.merges == []


# ---------------------------------------------------------------------------
# round 2: gates the round-1 mutations showed unpinned
# ---------------------------------------------------------------------------

def _rev(login, state, commit):
    return {"user": {"login": login}, "state": state, "commit_id": commit}


H = "a" * 40
OLD = "b" * 40


@pytest.mark.parametrize("label,reviews,expected", [
    ("approved-on-head", [_rev(REVIEWER, "APPROVED", H)], True),
    ("approved-then-dismissed", [_rev(REVIEWER, "APPROVED", H), _rev(REVIEWER, "DISMISSED", H)], False),
    ("approved-then-changes-requested",
     [_rev(REVIEWER, "APPROVED", H), _rev(REVIEWER, "CHANGES_REQUESTED", H)], False),
    ("changes-requested-then-approved",
     [_rev(REVIEWER, "CHANGES_REQUESTED", H), _rev(REVIEWER, "APPROVED", H)], True),
    ("approved-then-comment", [_rev(REVIEWER, "APPROVED", H), _rev(REVIEWER, "COMMENTED", H)], True),
    ("approved-on-older-commit-only", [_rev(REVIEWER, "APPROVED", OLD)], False),
    ("older-approval-then-head-approval", [_rev(REVIEWER, "APPROVED", OLD), _rev(REVIEWER, "APPROVED", H)], True),
    ("author-approval-ignored", [_rev(AUTHOR, "APPROVED", H)], False),
    ("other-reviewers-block-does-not-revoke",
     [_rev(REVIEWER, "APPROVED", H), _rev("reviewer2", "CHANGES_REQUESTED", H)], True),
    ("no-login", [{"user": None, "state": "APPROVED", "commit_id": H}], False),
    ("empty", [], False),
])
def test_r2_has_current_approval(label, reviews, expected):
    assert _mb()._has_current_approval(reviews, AUTHOR, H) is expected, label


@pytest.mark.parametrize("variant", ["dismissed", "changes-requested", "older-commit-only"])
def test_r2_press_refuses_revoked_or_stale_approval(tmp_path, variant):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    head = env.gh.pr_head(REPO, 1)
    env.gh.reviews_override = {
        "dismissed": [_rev(REVIEWER, "APPROVED", head), _rev(REVIEWER, "DISMISSED", head)],
        "changes-requested": [_rev(REVIEWER, "APPROVED", head), _rev(REVIEWER, "CHANGES_REQUESTED", head)],
        "older-commit-only": [_rev(REVIEWER, "APPROVED", "0" * 40)],
    }[variant]
    assert env.press() != 0
    assert env.gh.merges == []


def test_r2_wrong_parent_right_tree_stops_next_step(tmp_path, capsys):
    """An interleaved push leaves the tree right and the parent wrong."""
    env = Env(tmp_path)
    env.ready()
    env.gh.interleave_on = {1}
    capsys.readouterr()
    assert env.press() != 0
    assert [m[0] for m in env.gh.merges] == [1]
    st = env.st()
    assert st["steps"][0]["landed"]["tree"] == st["steps"][0]["tree"]   # the tree was right
    assert not st["steps"][0]["pressed"] and not st["steps"][1].get("merge_sha")
    assert "landing mismatch" in capsys.readouterr().err


def test_r2_nonzero_rc_with_no_failing_cells_refuses(tmp_path, capsys):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    assert env.build() == 0
    j = junit(tmp_path / "junit.xml")          # zero failing cells
    assert env.run("record-run", "--junit", str(j), "--rc", "2", "--tree", env.st()["steps"][-1]["tree"]) == 0
    env.window()
    capsys.readouterr()
    assert env.press() != 0
    assert env.gh.merges == []
    assert "rc=2" in capsys.readouterr().err


def test_r2_press_resumes_after_partial_run_without_remerging(tmp_path):
    env = Env(tmp_path)
    env.ready()
    env.gh.head_override = {2: "e" * 40}       # step 2 refuses; step 1 presses
    assert env.press() != 0
    assert [m[0] for m in env.gh.merges] == [1]
    assert env.st()["steps"][0]["pressed"] and not env.st()["steps"][1]["pressed"]
    env.gh.head_override = {}
    assert env.press() == 0
    assert [m[0] for m in env.gh.merges] == [1, 2]      # step 1 never merged twice
    assert all(s["pressed"] for s in env.st()["steps"])


@pytest.mark.parametrize("label,exc", [
    ("timeout", subprocess.TimeoutExpired(cmd="gh", timeout=120)),
    ("no-gh", FileNotFoundError("gh")),
    ("nonzero-exit", None),
])
def test_r2_merge_outcome_unknown_is_refused(tmp_path, capsys, label, exc):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    if exc is None:
        env.gh.merge_ok = False
    else:
        env.gh.merge_raises = exc
    capsys.readouterr()
    assert env.press() == 1                      # a Refused, not a traceback
    assert "merge outcome unknown for #1; read the base before re-running" in capsys.readouterr().err
    assert not env.st()["steps"][0]["pressed"]


def test_r2_landing_mismatch_records_merged_and_rerun_refuses(tmp_path, capsys):
    env = Env(tmp_path)
    env.ready()
    env.gh.tamper_on = {1}
    assert env.press() != 0
    step = env.st()["steps"][0]
    assert step["merged"] is True and step["pressed"] is False       # distinguishable from unmerged
    assert step["observed_tip"] == git(env.remote, "rev-parse", f"refs/heads/{BASE}")
    env.gh.tamper_on = set()
    capsys.readouterr()
    assert env.press() != 0                                          # no auto-repair
    err = capsys.readouterr().err
    assert "#1" in err and "already merged" in err
    assert [m[0] for m in env.gh.merges] == [1]


def test_r2_clean_press_records_merged_on_each_step(tmp_path):
    env = Env(tmp_path)
    env.ready()
    assert env.press() == 0
    for s in env.st()["steps"]:
        assert s["merged"] is True and s["pressed"] is True and s["observed_tip"] == s["merge_sha"]


def test_r2_press_refuses_after_window_closed(tmp_path, capsys):
    env = Env(tmp_path)
    env.ready()
    env.clock.t += 31 * 60
    capsys.readouterr()
    assert env.press() != 0
    assert env.gh.merges == []
    assert "new window" in capsys.readouterr().err


def test_r2_press_within_window_ok(tmp_path):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    env.clock.t += 29 * 60
    assert env.press() == 0


def test_r2_new_window_line_requires_a_new_seq(tmp_path):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    env.clock.t += 31 * 60
    assert env.run("window", "--minutes", "30") == 0      # a new line, seq not yet recorded
    assert env.press() != 0
    assert env.gh.merges == []
    assert env.run("window", "--record-seq", "seq-8") == 0
    assert env.press() == 0


@pytest.mark.parametrize("seq", ["", "   ", "\t"])
def test_r2_blank_record_seq_refused(tmp_path, seq):
    env = Env(tmp_path)
    assert env.build() == 0
    assert env.run("window", "--minutes", "30") == 0
    assert env.run("window", "--record-seq", seq) != 0
    assert not (env.st().get("window") or {}).get("seq")


@pytest.mark.parametrize("ref", ["   ", "\t"])
def test_r2_blank_accept_red_refused(tmp_path, ref):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    assert env.build() == 0
    assert env.record_green(failed=[("tests.test_untouched", "test_flaky")]) == 0
    env.window()
    assert env.press("--accept-red", ref) != 0
    assert env.gh.merges == []
    assert "accept_red" not in env.st()


def test_r2_record_run_refuses_overwrite_unless_replace(tmp_path):
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    assert env.build() == 0
    tree = env.st()["steps"][-1]["tree"]
    red = junit(tmp_path / "red.xml", failed=[("tests.test_x", "test_flaky")])
    green = junit(tmp_path / "green.xml")
    assert env.run("record-run", "--junit", str(red), "--rc", "1", "--tree", tree) == 0
    assert env.run("record-run", "--junit", str(green), "--rc", "0", "--tree", tree) != 0
    assert env.st()["run"]["failures"] == 1 and "previous_runs" not in env.st()
    assert env.run("record-run", "--junit", str(green), "--rc", "0", "--tree", tree, "--replace") == 0
    st = env.st()
    assert st["run"]["failures"] == 0
    assert [r["failing"] for r in st["previous_runs"]] == [["tests.test_x::test_flaky"]]


# ---------------------------------------------------------------------------
# round 3: in-loop window check, floored window end, identity refusal wording
# ---------------------------------------------------------------------------

def test_r3_window_expiring_between_steps_stops_before_second_merge(tmp_path, capsys):
    """The window is re-checked per step: it closes after step 1's merge, before step 2's."""
    env = Env(tmp_path)
    env.ready()                                  # window: 30 minutes from the clock's start
    real_merge = env.gh.merge

    def merge_then_run_out_the_window(repo, n, head):
        res = real_merge(repo, n, head)
        env.clock.t += 31 * 60                   # step 1 merged inside the window; now past it
        return res

    env.gh.merge = merge_then_run_out_the_window
    capsys.readouterr()
    assert env.press() == 1
    assert [m[0] for m in env.gh.merges] == [1]           # no second merge call
    assert "window ended" in capsys.readouterr().err      # the refusal names the window
    steps = env.st()["steps"]
    assert steps[0]["pressed"] is True
    assert steps[1]["pressed"] is False and not steps[1].get("merged")


def test_r3_window_until_is_floored_to_the_announced_minute(tmp_path, capsys):
    """Announced HH:MMZ and the enforced bound are the same instant."""
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    assert env.build() == 0
    assert env.record_green() == 0
    env.clock.t = 1_800_014_445.0                # 2027-01-15 12:00:45Z
    capsys.readouterr()
    assert env.run("window", "--minutes", "10") == 0
    assert "until 12:10Z" in capsys.readouterr().out
    assert env.st()["window"]["until"] == "2027-01-15T12:10:00Z"
    assert env.run("window", "--record-seq", "seq-7") == 0
    env.clock.t = 1_800_014_445.0 + 10 * 60 + 16     # 12:10:01Z
    capsys.readouterr()
    assert env.press() == 1
    assert env.gh.merges == []
    assert "window ended" in capsys.readouterr().err


def _identity_refusing(monkeypatch):
    mb = _mb()
    monkeypatch.setattr(mb, "_gh_identity", lambda: ({}, ("unknown", "service identity not configured")))
    return mb


def test_r3_merge_identity_refusal_says_not_sent(monkeypatch):
    mb = _identity_refusing(monkeypatch)
    ran = []
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: ran.append(a))
    ok, detail = mb.GhClient().merge(REPO, 1, "a" * 40)
    assert ok is False and ran == []             # gh never ran
    assert "NOT sent" in detail
    assert "outcome unknown" not in detail


def test_r3_press_identity_refusal_is_not_reported_as_outcome_unknown(tmp_path, monkeypatch, capsys):
    mb = _identity_refusing(monkeypatch)
    env = Env(tmp_path, prs=((1, {"a.txt": "a\n"}),))
    env.ready()
    env.gh.merge = mb.GhClient().merge
    capsys.readouterr()
    assert env.press() == 1
    err = capsys.readouterr().err
    assert "NOT sent" in err and "#1" in err
    assert "outcome unknown" not in err
    step = env.st()["steps"][0]
    assert not step["pressed"] and not step.get("merged")


# ---------------------------------------------------------------------------
# #4191 colleague round-1 fold (contract-first: written before the fixes)
# ---------------------------------------------------------------------------

# -- 1. the merge argv is pinned (--match-head-commit is the head-race guard)

def test_r1_merge_argv_pinned(monkeypatch):
    mb = _mb()
    calls = []

    class Res:
        returncode, stdout, stderr = 0, "ok", ""

    monkeypatch.setattr(mb, "_gh_identity", lambda: ({"env": {"X": "1"}}, None))
    monkeypatch.setattr(mb.subprocess, "run", lambda argv, **kw: calls.append(argv) or Res())
    ok, _ = mb.GhClient().merge(REPO, 7, "abc123")
    assert ok is True
    assert calls == [["gh", "pr", "merge", "7", "--repo", REPO, "--squash",
                      "--match-head-commit", "abc123", "--body", ""]]


def test_r1_merge_identity_refusal_never_runs_gh(monkeypatch):
    mb = _mb()
    calls = []
    monkeypatch.setattr(mb, "_gh_identity", lambda: (None, (False, "no identity")))
    monkeypatch.setattr(mb.subprocess, "run", lambda *a, **k: calls.append(a))
    ok, detail = mb.GhClient().merge(REPO, 7, "abc123")
    assert ok is False and detail.startswith(mb._NOT_SENT)
    assert calls == []


# -- 2. default remote is HTTPS

def test_r1_default_remote_is_https():
    assert _mb()._default_remote(REPO) == f"https://github.com/{REPO}.git"


def test_r1_build_without_remote_uses_default_and_stores_it(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.setattr(_mb(), "_default_remote", lambda repo: str(env.remote))
    rc = env.run("build", "--repo", REPO, "--base", BASE, "--prs", "1,2",
                 "--scratch", str(env.scratch))
    assert rc == 0 and env.st()["remote"] == str(env.remote)


def test_r1_explicit_remote_wins_over_default(tmp_path, monkeypatch):
    env = Env(tmp_path)
    monkeypatch.setattr(_mb(), "_default_remote", lambda repo: "https://invalid.example/x.git")
    assert env.build() == 0
    assert env.st()["remote"] == str(env.remote)


# -- 3. record-run stores and echoes its own totals

def _junit_cases(path, cases):
    """cases: list of (name, kind), kind in pass|skip|fail|error|skipfail."""
    body = ""
    for name, kind in cases:
        inner = {"pass": "", "skip": "<skipped/>", "fail": "<failure/>",
                 "error": "<error/>", "skipfail": "<skipped/><failure/>"}[kind]
        body += f'<testcase classname="t.c" name="{name}">{inner}</testcase>'
    path.write_text(f'<testsuites><testsuite name="p" tests="999" failures="0">{body}</testsuite></testsuites>')
    return path


def test_r1_record_run_stores_and_echoes_totals(tmp_path, capsys):
    env = Env(tmp_path)
    assert env.build() == 0
    j = _junit_cases(tmp_path / "j.xml", [("a", "pass"), ("b", "pass"), ("c", "skip"),
                                          ("d", "fail"), ("e", "error")])
    capsys.readouterr()
    assert env.run("record-run", "--junit", str(j), "--rc", "1",
                   "--tree", env.st()["steps"][-1]["tree"]) == 0
    run = env.st()["run"]
    assert (run["tests"], run["passed"], run["skipped"], run["failures"], run["errors"]) == (5, 2, 1, 1, 1)
    assert run["rc"] == 1 and run["failing"] and run["tree"]
    assert "recorded run: rc=1 tests=5 passed=2 skipped=1 failures=1 errors=1" in capsys.readouterr().out


def test_r1_record_run_refuses_zero_testcases(tmp_path, capsys):
    env = Env(tmp_path)
    assert env.build() == 0
    j = tmp_path / "empty.xml"
    j.write_text('<testsuites><testsuite name="p" tests="12" failures="0"/></testsuites>')
    before = env.state.read_text()
    assert env.run("record-run", "--junit", str(j), "--rc", "0",
                   "--tree", env.st()["steps"][-1]["tree"]) == 1
    assert "REFUSED" in capsys.readouterr().err
    assert env.state.read_text() == before


def test_r1_skipped_and_failed_case_counts_once_as_failed(tmp_path):
    env = Env(tmp_path)
    assert env.build() == 0
    j = _junit_cases(tmp_path / "j.xml", [("a", "pass"), ("b", "skipfail")])
    assert env.run("record-run", "--junit", str(j), "--rc", "1",
                   "--tree", env.st()["steps"][-1]["tree"]) == 0
    run = env.st()["run"]
    assert (run["tests"], run["passed"], run["skipped"], run["failures"]) == (2, 1, 0, 1)


# -- 4. stack membership reads both sources, fails closed

_MEASURED_STACK = {"base": {"ref": "dev", "sha": None}, "id": 2539173, "number": 4110,
                   "position": 1, "size": 4}


@pytest.mark.parametrize("label,pull,events,expected", [
    ("measured-4108", {"number": 4108, "stack": _MEASURED_STACK}, [], True),
    ("absent-no-events", {"number": 5}, [], False),
    ("absent-last-added", {"number": 5}, [{"event": "labeled"}, {"event": "added_to_stack"}], True),
    ("absent-added-then-removed", {"number": 5},
     [{"event": "added_to_stack"}, {"event": "removed_from_stack"}], False),
    ("present-removed-last", {"number": 5, "stack": _MEASURED_STACK},
     [{"event": "added_to_stack"}, {"event": "removed_from_stack"}], True),
])
def test_r1_stack_member(label, pull, events, expected):
    assert _mb()._stack_member(pull, events) is expected, label


def test_r1_pr_stack_reads_both_sources_and_paginates(monkeypatch):
    mb = _mb()
    seen = []
    page1 = [{"event": "labeled"}] * 100
    page2 = [{"event": "added_to_stack"}]

    def fake_json(self, args):
        seen.append(args[1])
        if args[1].endswith("/events?per_page=100&page=1"):
            return page1
        if args[1].endswith("/events?per_page=100&page=2"):
            return page2
        return {"number": 9}

    monkeypatch.setattr(mb.GhClient, "_json", fake_json)
    assert mb.GhClient().pr_stack(REPO, 9) is True
    assert f"repos/{REPO}/pulls/9" in seen
    assert any("/issues/9/events" in s for s in seen)


def test_r1_pr_stack_events_over_cap_refuses(monkeypatch):
    mb = _mb()

    def fake_json(self, args):
        return [{"event": "labeled"}] * 100 if "/events" in args[1] else {"number": 9}

    monkeypatch.setattr(mb.GhClient, "_json", fake_json)
    with pytest.raises(mb.Refused):
        mb.GhClient().pr_stack(REPO, 9)


# -- _paged ceiling boundary: refuses exactly when it would have to request page 11

def _paged_fake(monkeypatch, full_pages, last_len, requested):
    """List endpoint with `full_pages` pages of 100 rows, then a page of `last_len`."""
    mb = _mb()

    def fake_json(self, args):
        if "page=" not in args[1]:          # the pull object itself
            return {"number": 3}
        page = int(args[1].rsplit("page=", 1)[1])
        requested.append(page)
        return [{"event": "labeled", "state": "COMMENTED"}] * (100 if page <= full_pages else last_len)

    monkeypatch.setattr(mb.GhClient, "_json", fake_json)
    return mb


def test_r1_paged_nine_full_pages_plus_short_tenth_returns_all(monkeypatch):
    requested = []
    mb = _paged_fake(monkeypatch, 9, 7, requested)
    assert len(mb.GhClient().reviews(REPO, 3)) == 907
    assert requested == list(range(1, 11))


def test_r1_paged_ten_full_pages_refuses_without_page_11_reviews(monkeypatch):
    requested = []
    mb = _paged_fake(monkeypatch, 10, 0, requested)
    with pytest.raises(mb.Refused, match="more than 1000 reviews"):
        mb.GhClient().reviews(REPO, 3)
    assert requested == list(range(1, 11))


def test_r1_paged_ten_full_pages_refuses_without_page_11_events(monkeypatch):
    requested = []
    mb = _paged_fake(monkeypatch, 10, 0, requested)
    with pytest.raises(mb.Refused, match="more than 1000 issue events"):
        mb.GhClient().pr_stack(REPO, 3)
    assert requested == list(range(1, 11))
