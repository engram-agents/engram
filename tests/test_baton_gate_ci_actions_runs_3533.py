"""#3533 -- the gate's CI read works with a fine-grained PAT.

Contract-first: written before the implementation.

GitHub's fine-grained PAT UI has no "Checks" permission, so under the forum
service's token `gh pr view --json statusCheckRollup` is refused ("Resource not
accessible by personal access token") and `_pr_ci_state` could never go green.
The same token CAN read `repos/<repo>/actions/runs?head_sha=<oid>`. The contract:

  K1  under the service identity skip the rollup call; under the ambient identity
      keep the rollup call first and fall through ONLY on the refusal text.
  K2  `gh pr view --json mergeStateStatus,headRefOid`; CLEAN is green, no runs call.
  K3  validate headRefOid / repo before building a URL; parse JSON in Python;
      anything malformed or truncated is unknown.
  K4  newest run per workflow name by updated_at; tie takes the worst; only
      completed+success is green, every other conclusion red; red > pending > green.
  K5  no runs and not CLEAN is pending, never green.
  K6  every failure is unknown with a scrubbed detail, never green, never raises.
  K7  the identity kwargs ride on every call; the refusal precedes any subprocess.

This is a merge gate: every cell that matters asserts the FAIL-CLOSED direction.
All token values are fake and built at runtime; no cell touches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_TOOLS_DIR = _ROOT / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton_gate  # noqa: E402

TOKEN = "fake-token-for-test-" + "3533"
OID = "b" * 40
REPO = "example-org/example-repo"
PR = "9001"
REFUSED = ("GraphQL: Resource not accessible by personal access token "
           "(repository.pullRequest.statusCheckRollup.nodes.0)")
RUNS_ARGV = ["gh", "api", f"repos/{REPO}/actions/runs?head_sha={OID}&per_page=100"]

T1 = "2026-10-06T10:00:00Z"
T2 = "2026-10-06T10:05:00Z"
T3 = "2026-10-06T10:10:00Z"


def run(name, conclusion="success", status="completed", updated=T1):
    return {"name": name, "status": status, "conclusion": conclusion, "updated_at": updated}


def runs_body(runs, total=None):
    return json.dumps({"total_count": len(runs) if total is None else total,
                       "workflow_runs": runs})


def kind(cmd):
    """Which of the gate's three reads this argv is."""
    if cmd[:3] == ["gh", "pr", "view"]:
        js = cmd[cmd.index("--json") + 1]
        return {"statusCheckRollup,mergeStateStatus": "A",
                "mergeStateStatus,headRefOid": "B"}.get(js, "?pr-view:" + js)
    if cmd[:2] == ["gh", "api"]:
        return "C"
    return "?" + " ".join(cmd[:3])


class Fake:
    """Stands in for subprocess.run. `script` maps A/B/C to a result.

    A result is (stdout, returncode, stderr), or an Exception to raise, or a
    callable returning one of those. Every call's argv and kwargs are recorded.
    An unscripted kind raises AssertionError, so an unexpected call is loud.
    """

    def __init__(self, **script):
        self.script = script
        self.calls = []

    def kinds(self):
        return [kind(c) for c, _kw in self.calls]

    def __call__(self, cmd, *a, **kw):
        cmd = list(cmd)
        self.calls.append((cmd, kw))
        k = kind(cmd)
        if k not in self.script:
            raise AssertionError(f"unexpected gh call {k}: {cmd!r}")
        res = self.script[k]
        if callable(res):
            res = res()
        if isinstance(res, BaseException):
            raise res
        out, rc, err = res
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)


def B(state="BLOCKED", oid=OID):
    return (json.dumps({"mergeStateStatus": state, "headRefOid": oid}), 0, "")


def C(runs, total=None):
    return (runs_body(runs, total), 0, "")


@pytest.fixture(autouse=True)
def _reset_identity():
    baton_gate._service_identity_reset()
    yield
    baton_gate._service_identity_reset()


@pytest.fixture
def svc(tmp_path):
    d = tmp_path / "gh-config"
    d.mkdir(mode=0o700)
    baton_gate.configure_service_github_identity(environ={"GH_TOKEN": TOKEN}, config_dir=str(d))
    return str(d)


def _go(monkeypatch, fake, repo=REPO):
    monkeypatch.setattr(baton_gate.subprocess, "run", fake)
    return baton_gate._pr_ci_state(PR, repo)


# ---------------------------------------------------------------------------
# (a) K2: CLEAN short-circuits, exactly one call
# ---------------------------------------------------------------------------

def test_a_service_identity_clean_is_green_with_one_call(svc, monkeypatch):
    fake = Fake(B=B("CLEAN"))
    assert _go(monkeypatch, fake) == ("green", "mergeStateStatus=CLEAN")
    assert fake.kinds() == ["B"]


def test_a_clean_is_case_insensitive_and_needs_no_oid(svc, monkeypatch):
    fake = Fake(B=(json.dumps({"mergeStateStatus": "clean"}), 0, ""))
    assert _go(monkeypatch, fake)[0] == "green"
    assert fake.kinds() == ["B"]


def test_a_service_identity_never_asks_for_the_rollup(svc, monkeypatch):
    fake = Fake(B=B("BLOCKED"), C=C([run("pr-checks")]))
    _go(monkeypatch, fake)
    assert "A" not in fake.kinds()
    for cmd, _kw in fake.calls:
        assert "statusCheckRollup" not in " ".join(cmd)


# ---------------------------------------------------------------------------
# (b)-(e), (h): the dedupe-and-map rules
# ---------------------------------------------------------------------------

def _state(svc_, monkeypatch, runs, merge="BLOCKED", total=None):
    fake = Fake(B=B(merge), C=C(runs, total))
    return _go(monkeypatch, fake), fake


def test_b_not_clean_and_all_success_is_green(svc, monkeypatch):
    (state, _d), fake = _state(svc, monkeypatch, [run("pr-checks"), run("anti-dup")])
    assert state == "green"
    assert fake.kinds() == ["B", "C"]


def test_c_newest_cancelled_beside_older_success_is_red(svc, monkeypatch):
    (state, detail), _ = _state(svc, monkeypatch, [
        run("pr-checks", "success", updated=T1),
        run("pr-checks", "cancelled", updated=T2)])
    assert state == "red" and "pr-checks" in detail


def test_c_order_in_the_list_does_not_matter(svc, monkeypatch):
    (state, _d), _ = _state(svc, monkeypatch, [
        run("pr-checks", "cancelled", updated=T2),
        run("pr-checks", "success", updated=T1)])
    assert state == "red"


def test_d_older_failure_beside_newer_success_is_green(svc, monkeypatch):
    for runs in ([run("pr-checks", "failure", updated=T1), run("pr-checks", "success", updated=T2)],
                 [run("pr-checks", "success", updated=T2), run("pr-checks", "failure", updated=T1)]):
        (state, _d), _ = _state(svc, monkeypatch, runs)
        assert state == "green", runs


def test_d_the_issue_measured_shape_newest_success_supersedes_failure_and_cancelled(svc, monkeypatch):
    """Lei's measurement: success + success/failure/cancelled of pr-checks."""
    (state, _d), _ = _state(svc, monkeypatch, [
        run("anti-dup-retro-check", updated=T1),
        run("pr-checks", "success", updated=T3),
        run("pr-checks", "failure", updated=T2),
        run("pr-checks", "cancelled", updated=T1)])
    assert state == "green"


def test_e_newest_in_progress_is_pending(svc, monkeypatch):
    (state, detail), _ = _state(svc, monkeypatch, [
        run("pr-checks", "success", updated=T1),
        run("pr-checks", None, status="in_progress", updated=T2)])
    assert state == "pending" and "pr-checks" in detail


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
def test_e_every_non_completed_status_is_pending(svc, monkeypatch, status):
    (state, _d), _ = _state(svc, monkeypatch, [run("pr-checks", None, status=status)])
    assert state == "pending"


def test_e_non_completed_status_with_a_success_conclusion_is_still_pending(svc, monkeypatch):
    (state, _d), _ = _state(svc, monkeypatch, [run("pr-checks", "success", status="in_progress")])
    assert state == "pending"


def test_e_completed_with_null_conclusion_is_pending(svc, monkeypatch):
    (state, _d), _ = _state(svc, monkeypatch, [run("pr-checks", None, status="completed")])
    assert state == "pending"


def test_e_pending_beats_an_unrelated_green(svc, monkeypatch):
    (state, detail), _ = _state(svc, monkeypatch, [
        run("anti-dup"), run("pr-checks", None, status="queued")])
    assert state == "pending" and "pr-checks" in detail


def test_e_red_beats_pending_and_green(svc, monkeypatch):
    for runs in ([run("a"), run("b", None, status="queued"), run("c", "failure")],
                 [run("c", "failure"), run("b", None, status="queued"), run("a")]):
        (state, detail), _ = _state(svc, monkeypatch, runs)
        assert state == "red" and "'c'" in detail, runs


@pytest.mark.parametrize("conclusion", [
    "failure", "cancelled", "timed_out", "action_required", "startup_failure",
    "stale", "neutral", "skipped", "some_future_value"])
def test_g_every_non_success_conclusion_is_red(svc, monkeypatch, conclusion):
    (state, _d), _ = _state(svc, monkeypatch, [run("pr-checks", conclusion)])
    assert state == "red", conclusion


@pytest.mark.parametrize("conclusion", ["SUCCESS", "Success", " success", "success "])
def test_g_only_the_exact_lowercase_success_is_green(svc, monkeypatch, conclusion):
    (state, _d), _ = _state(svc, monkeypatch, [run("pr-checks", conclusion)])
    assert state == "red", repr(conclusion)


def test_h_tie_on_updated_at_between_failure_and_success_is_red(svc, monkeypatch):
    for runs in ([run("pr-checks", "failure"), run("pr-checks", "success")],
                 [run("pr-checks", "success"), run("pr-checks", "failure")]):
        (state, _d), _ = _state(svc, monkeypatch, runs)
        assert state == "red", runs


def test_h_tie_between_pending_and_success_is_pending(svc, monkeypatch):
    for runs in ([run("pr-checks", None, status="queued"), run("pr-checks", "success")],
                 [run("pr-checks", "success"), run("pr-checks", None, status="queued")]):
        (state, _d), _ = _state(svc, monkeypatch, runs)
        assert state == "pending", runs


def test_h_tie_between_red_and_pending_is_red(svc, monkeypatch):
    (state, _d), _ = _state(svc, monkeypatch, [
        run("pr-checks", None, status="queued"), run("pr-checks", "failure")])
    assert state == "red"


def test_dedupe_is_per_workflow_name_not_global(svc, monkeypatch):
    """An older red of workflow X is NOT superseded by a newer green of workflow Y."""
    (state, _d), _ = _state(svc, monkeypatch, [
        run("x", "failure", updated=T1), run("y", "success", updated=T3)])
    assert state == "red"


# ---------------------------------------------------------------------------
# (f) K5: no runs
# ---------------------------------------------------------------------------

def test_f_empty_runs_and_not_clean_is_pending_never_green(svc, monkeypatch):
    (state, detail), fake = _state(svc, monkeypatch, [])
    assert (state, detail) == ("pending", "no workflow runs on the head")
    assert fake.kinds() == ["B", "C"]


# ---------------------------------------------------------------------------
# (i) K3: malformed / truncated -> unknown
# ---------------------------------------------------------------------------

def test_i_truncated_total_count_is_unknown(svc, monkeypatch):
    runs = [run(f"w{i}") for i in range(100)]
    (state, _d), _ = _state(svc, monkeypatch, runs, total=150)
    assert state == "unknown"


def test_i_control_total_count_equal_to_returned_is_not_truncated(svc, monkeypatch):
    runs = [run(f"w{i}") for i in range(100)]
    (state, _d), _ = _state(svc, monkeypatch, runs, total=100)
    assert state == "green"


@pytest.mark.parametrize("body", [
    "not json at all",
    "",
    "   ",
    "null",
    "[]",
    "[1, 2]",
    '"a string"',
    "{}",
    json.dumps({"total_count": 0}),
    json.dumps({"workflow_runs": []}),
    json.dumps({"total_count": 1, "workflow_runs": None}),
    json.dumps({"total_count": 1, "workflow_runs": {"name": "x"}}),
    json.dumps({"total_count": 1, "workflow_runs": "x"}),
    json.dumps({"total_count": "1", "workflow_runs": [run("x")]}),
    json.dumps({"total_count": None, "workflow_runs": [run("x")]}),
    json.dumps({"total_count": True, "workflow_runs": [run("x")]}),
    json.dumps({"total_count": 1, "workflow_runs": [["not", "a", "dict"]]}),
    json.dumps({"total_count": 1, "workflow_runs": [None]}),
], ids=lambda s: s[:40] or "<empty>")
def test_i_malformed_runs_body_is_unknown(svc, monkeypatch, body):
    fake = Fake(B=B(), C=(body, 0, ""))
    state, detail = _go(monkeypatch, fake)
    assert state == "unknown", (state, detail)


@pytest.mark.parametrize("bad", [
    {"status": "completed", "conclusion": "success", "updated_at": T1},
    {"name": None, "status": "completed", "conclusion": "success", "updated_at": T1},
    {"name": 7, "status": "completed", "conclusion": "success", "updated_at": T1},
    {"name": "", "status": "completed", "conclusion": "success", "updated_at": T1},
    {"name": ["x"], "status": "completed", "conclusion": "success", "updated_at": T1},
], ids=["no-name", "null-name", "int-name", "empty-name", "list-name"])
def test_i_a_run_without_a_string_name_is_unknown(svc, monkeypatch, bad):
    (state, _d), _ = _state(svc, monkeypatch, [run("good"), bad])
    assert state == "unknown"


@pytest.mark.parametrize("stamp", [
    None, "", "yesterday", "2026-10-06", "2026-10-06 10:00:00", "2026-10-06T10:00:00",
    "2026-10-06T10:00:00+00:00", "2026-13-40T99:00:00Z", 1696586400, ["2026-10-06T10:00:00Z"],
])
def test_i_unparseable_or_missing_updated_at_is_unknown(svc, monkeypatch, stamp):
    bad = run("pr-checks")
    bad["updated_at"] = stamp
    (state, _d), _ = _state(svc, monkeypatch, [bad])
    assert state == "unknown", repr(stamp)


def test_i_missing_updated_at_key_is_unknown(svc, monkeypatch):
    bad = run("pr-checks")
    del bad["updated_at"]
    (state, _d), _ = _state(svc, monkeypatch, [bad])
    assert state == "unknown"


def test_i_one_bad_timestamp_poisons_the_whole_read_even_beside_a_green(svc, monkeypatch):
    bad = run("other")
    bad["updated_at"] = "garbage"
    (state, _d), _ = _state(svc, monkeypatch, [run("pr-checks"), bad])
    assert state == "unknown"


@pytest.mark.parametrize("patch", [
    {"status": None}, {"status": 3}, {"conclusion": 3}, {"conclusion": ["success"]},
])
def test_i_wrongly_typed_status_or_conclusion_is_unknown(svc, monkeypatch, patch):
    r = run("pr-checks")
    r.update(patch)
    (state, _d), _ = _state(svc, monkeypatch, [r])
    assert state == "unknown", patch


# ---------------------------------------------------------------------------
# (j) K3: validate before building a URL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("oid", [
    "b" * 39, "b" * 41, "B" * 40, "b" * 39 + "/", "b" * 39 + "&", "b" * 39 + "?",
    "b" * 39 + "g", "", " " + "b" * 39, "b" * 40 + "\n", "../" + "b" * 37, None, 123, ["b" * 40],
])
def test_j_invalid_head_oid_is_unknown_and_makes_no_runs_call(svc, monkeypatch, oid):
    fake = Fake(B=(json.dumps({"mergeStateStatus": "BLOCKED", "headRefOid": oid}), 0, ""))
    state, _d = _go(monkeypatch, fake)
    assert state == "unknown", repr(oid)
    assert fake.kinds() == ["B"]


def test_j_missing_head_oid_key_is_unknown_and_makes_no_runs_call(svc, monkeypatch):
    fake = Fake(B=(json.dumps({"mergeStateStatus": "BLOCKED"}), 0, ""))
    assert _go(monkeypatch, fake)[0] == "unknown"
    assert fake.kinds() == ["B"]


@pytest.mark.parametrize("repo", [
    "owner", "a/b/c", "owner/repo?x=1", "owner/repo&x=1", "a b/c", "owner/re po",
    "owner/repo\n", "/repo", "owner/", "owner/repo#x", "owner/repo%2f..", "own er/repo",
    "../repo", "owner/..", "./repo", "owner/.", "owner/repo/",
], ids=repr)
def test_j_invalid_repo_is_unknown_and_makes_no_runs_call(svc, monkeypatch, repo):
    fake = Fake(B=B())
    state, _d = _go(monkeypatch, fake, repo=repo)
    assert state == "unknown", repr(repo)
    assert "C" not in fake.kinds()


@pytest.mark.parametrize("repo", ["owner/repo", "Org-1/my_repo.v2", "a/b", "example-org/example-repo-2"])
def test_j_control_valid_repo_shapes_reach_the_runs_call(svc, monkeypatch, repo):
    fake = Fake(B=B(), C=C([run("pr-checks")]))
    assert _go(monkeypatch, fake, repo=repo)[0] == "green"
    assert fake.kinds() == ["B", "C"]
    assert fake.calls[1][0][2].startswith(f"repos/{repo}/actions/runs?head_sha=")


# ---------------------------------------------------------------------------
# (k) K6: every failure is unknown, scrubbed, never raises
# ---------------------------------------------------------------------------

LEAK = f"token {TOKEN} in stderr"


def test_k_b_fails_with_planted_token_is_unknown_scrubbed(svc, monkeypatch):
    fake = Fake(B=("", 1, LEAK))
    state, detail = _go(monkeypatch, fake)
    assert state == "unknown" and TOKEN not in detail and "***" in detail
    assert fake.kinds() == ["B"]


def test_k_c_exit_1_with_403_and_planted_token_is_unknown_scrubbed(svc, monkeypatch):
    fake = Fake(B=B(), C=("", 1, f"gh: Resource not accessible (HTTP 403) {LEAK}"))
    state, detail = _go(monkeypatch, fake)
    assert state == "unknown" and TOKEN not in detail and "***" in detail and "403" in detail


def test_k_c_404_is_unknown(svc, monkeypatch):
    fake = Fake(B=B(), C=("", 1, "gh: Not Found (HTTP 404)"))
    assert _go(monkeypatch, fake)[0] == "unknown"


def test_k_exit_without_stderr_reports_the_exit_code(svc, monkeypatch):
    fake = Fake(B=B(), C=("", 4, ""))
    state, detail = _go(monkeypatch, fake)
    assert state == "unknown" and "4" in detail


@pytest.mark.parametrize("where", ["B", "C"])
@pytest.mark.parametrize("exc,needle", [
    (subprocess.TimeoutExpired(["gh"], 15), "timed out"),
    (FileNotFoundError("gh"), "not found"),
    (OSError(f"cannot exec with {TOKEN}"), "exec error"),
], ids=["timeout", "not-found", "oserror"])
def test_k_timeouts_and_exec_errors_are_unknown_and_scrubbed(svc, monkeypatch, where, exc, needle):
    script = {"B": B(), "C": C([run("pr-checks")])}
    script[where] = exc
    fake = Fake(**script)
    state, detail = _go(monkeypatch, fake)
    assert state == "unknown" and needle in detail and TOKEN not in detail


@pytest.mark.parametrize("where", ["B", "C"])
def test_k_empty_stdout_is_unknown(svc, monkeypatch, where):
    script = {"B": B(), "C": C([run("pr-checks")])}
    script[where] = ("   \n", 0, "")
    state, _d = _go(monkeypatch, Fake(**script))
    assert state == "unknown"


@pytest.mark.parametrize("b_body", ["not json", "[]", "null", '"CLEAN"', "7"])
def test_k_b_with_malformed_or_non_object_json_is_unknown(svc, monkeypatch, b_body):
    fake = Fake(B=(b_body, 0, ""))
    assert _go(monkeypatch, fake)[0] == "unknown"
    assert fake.kinds() == ["B"]


@pytest.mark.parametrize("merge", [None, 5, ["CLEAN"], {"s": "CLEAN"}])
def test_k_b_with_a_non_string_merge_state_is_never_green_by_clean(svc, monkeypatch, merge):
    fake = Fake(B=(json.dumps({"mergeStateStatus": merge, "headRefOid": OID}), 0, ""),
                C=C([run("pr-checks", "failure")]))
    assert _go(monkeypatch, fake)[0] == "red"


# ---------------------------------------------------------------------------
# (l) K1: path selection under the ambient identity
# ---------------------------------------------------------------------------

def _rollup(checks, merge="BLOCKED"):
    return (json.dumps({"statusCheckRollup": checks, "mergeStateStatus": merge}), 0, "")


def test_l_ambient_a_succeeding_is_unchanged_and_makes_one_call(monkeypatch):
    fake = Fake(A=_rollup([{"name": "x", "conclusion": "SUCCESS", "status": "COMPLETED"}]))
    assert _go(monkeypatch, fake) == ("green", "all checks passed")
    assert fake.kinds() == ["A"]


def test_l_ambient_a_red_is_red_without_the_runs_path(monkeypatch):
    fake = Fake(A=_rollup([{"name": "x", "conclusion": "FAILURE", "status": "COMPLETED"}]))
    assert _go(monkeypatch, fake)[0] == "red"
    assert fake.kinds() == ["A"]


def test_l_ambient_a_clean_is_green_without_further_calls(monkeypatch):
    fake = Fake(A=_rollup([], merge="CLEAN"))
    assert _go(monkeypatch, fake) == ("green", "mergeStateStatus=CLEAN")
    assert fake.kinds() == ["A"]


@pytest.mark.parametrize("text", [
    REFUSED, REFUSED.lower(), REFUSED.upper(),
    "error: something\n" + REFUSED + "\nmore text",
])
def test_l_ambient_a_refused_falls_through_and_does_not_report_unknown(monkeypatch, text):
    fake = Fake(A=("", 1, text), B=B(), C=C([run("pr-checks")]))
    state, _d = _go(monkeypatch, fake)
    assert state == "green"
    assert fake.kinds() == ["A", "B", "C"]


def test_l_ambient_a_refused_then_b_clean_is_green_without_c(monkeypatch):
    fake = Fake(A=("", 1, REFUSED), B=B("CLEAN"))
    assert _go(monkeypatch, fake) == ("green", "mergeStateStatus=CLEAN")
    assert fake.kinds() == ["A", "B"]


def test_l_ambient_a_refused_then_runs_red_is_red(monkeypatch):
    fake = Fake(A=("", 1, REFUSED), B=B(), C=C([run("pr-checks", "failure")]))
    assert _go(monkeypatch, fake)[0] == "red"


def test_l_ambient_a_refused_then_b_fails_is_unknown(monkeypatch):
    fake = Fake(A=("", 1, REFUSED), B=("", 1, "boom"))
    state, detail = _go(monkeypatch, fake)
    assert state == "unknown" and detail == "boom"


@pytest.mark.parametrize("text", [
    "GraphQL: Could not resolve to a PullRequest with the number of 9001.",
    "gh: HTTP 502", "", "Resource not accessible by integration",
    "not accessible by personal access token",
])
def test_l_ambient_a_failing_for_any_other_reason_is_unknown_with_no_b_call(monkeypatch, text):
    fake = Fake(A=("", 1, text))
    state, _d = _go(monkeypatch, fake)
    assert state == "unknown"
    assert fake.kinds() == ["A"]


def test_l_ambient_a_refusal_text_with_a_zero_exit_is_not_a_fallthrough(monkeypatch):
    """Stderr text alone must not trigger the fallthrough: the exit must be nonzero."""
    fake = Fake(A=("", 0, REFUSED))
    state, _d = _go(monkeypatch, fake)
    assert state == "unknown"
    assert fake.kinds() == ["A"]


def test_l_ambient_a_timeout_and_missing_gh_stay_unknown_with_no_fallthrough(monkeypatch):
    for exc in (subprocess.TimeoutExpired(["gh"], 15), FileNotFoundError("gh"), OSError("x")):
        fake = Fake(A=exc)
        assert _go(monkeypatch, fake)[0] == "unknown"
        assert fake.kinds() == ["A"]


# ---------------------------------------------------------------------------
# (m) K7: identity on every call; refusal before any subprocess
# ---------------------------------------------------------------------------

def _expected_env(svc_dir):
    return baton_gate._gh_identity()[0]["env"]


def test_m_service_identity_rides_on_every_call(svc, monkeypatch):
    fake = Fake(B=B(), C=C([run("pr-checks")]))
    _go(monkeypatch, fake)
    assert fake.kinds() == ["B", "C"]
    for cmd, kw in fake.calls:
        env = kw.get("env")
        assert isinstance(env, dict), f"no env= on {cmd[:3]}"
        assert env["GH_TOKEN"] == TOKEN and env["GH_CONFIG_DIR"] == svc
        assert env == _expected_env(svc)


@pytest.mark.parametrize("scenario", ["clean", "green", "red", "pending", "empty", "b_fail", "c_fail"])
def test_m_every_call_of_every_service_scenario_carries_the_identity(svc, monkeypatch, scenario):
    script = {
        "clean": dict(B=B("CLEAN")),
        "green": dict(B=B(), C=C([run("x")])),
        "red": dict(B=B(), C=C([run("x", "failure")])),
        "pending": dict(B=B(), C=C([run("x", None, status="queued")])),
        "empty": dict(B=B(), C=C([])),
        "b_fail": dict(B=("", 1, "x")),
        "c_fail": dict(B=B(), C=("", 1, "x")),
    }[scenario]
    fake = Fake(**script)
    _go(monkeypatch, fake)
    assert fake.calls
    for cmd, kw in fake.calls:
        assert kw.get("env") == _expected_env(svc), (scenario, cmd[:3])


def test_m_ambient_calls_carry_no_env_kwarg(monkeypatch):
    fake = Fake(A=("", 1, REFUSED), B=B(), C=C([run("x")]))
    _go(monkeypatch, fake)
    assert fake.kinds() == ["A", "B", "C"]
    for _cmd, kw in fake.calls:
        assert "env" not in kw


def test_m_every_call_has_a_positive_timeout_and_captures_output(svc, monkeypatch):
    fake = Fake(B=B(), C=C([run("x")]))
    _go(monkeypatch, fake)
    for cmd, kw in fake.calls:
        assert isinstance(kw.get("timeout"), (int, float)) and 0 < kw["timeout"] <= 30, cmd[:3]
        assert kw.get("capture_output") is True and kw.get("text") is True
    ambient = Fake(A=("", 1, REFUSED), B=B(), C=C([run("x")]))
    _go(monkeypatch, ambient)
    for cmd, kw in ambient.calls:
        assert isinstance(kw.get("timeout"), (int, float)) and 0 < kw["timeout"] <= 30, cmd[:3]


@pytest.mark.parametrize("environ", [{}, {"GH_TOKEN": ""}, {"GH_TOKEN": "   "}])
def test_m_no_token_refuses_before_any_subprocess(tmp_path, monkeypatch, environ):
    baton_gate.configure_service_github_identity(environ=environ, config_dir=str(tmp_path))
    fake = Fake()  # any call raises AssertionError
    state, detail = _go(monkeypatch, fake)
    assert state == "unknown" and "#3283" in detail
    assert fake.calls == []


def test_m_empty_repo_is_refused_before_any_subprocess(svc, monkeypatch):
    fake = Fake()
    state, _d = _go(monkeypatch, fake, repo="")
    assert state == "unknown" and fake.calls == []


# ---------------------------------------------------------------------------
# (n) the argv of the runs call
# ---------------------------------------------------------------------------

def test_n_runs_call_argv_is_exactly_the_expected_list_and_carries_no_token(svc, monkeypatch):
    fake = Fake(B=B(), C=C([run("pr-checks")]))
    _go(monkeypatch, fake)
    c_cmd = [cmd for cmd, _kw in fake.calls if kind(cmd) == "C"]
    assert c_cmd == [RUNS_ARGV]
    for cmd, kw in fake.calls:
        assert TOKEN not in " ".join(cmd)
        assert "--jq" not in cmd and "-q" not in cmd and "--template" not in cmd
        assert "--hostname" not in cmd and "-H" not in cmd and "--header" not in cmd
        assert kw.get("shell") is not True


def test_n_b_call_argv_is_exactly_the_expected_list(svc, monkeypatch):
    fake = Fake(B=B(), C=C([run("pr-checks")]))
    _go(monkeypatch, fake)
    assert fake.calls[0][0] == ["gh", "pr", "view", PR, "--json",
                                "mergeStateStatus,headRefOid", "--repo", REPO]


def test_n_the_oid_in_the_url_is_the_one_b_returned(svc, monkeypatch):
    other = "c" * 40
    fake = Fake(B=B(oid=other), C=C([run("pr-checks")]))
    _go(monkeypatch, fake)
    assert fake.calls[1][0][2] == f"repos/{REPO}/actions/runs?head_sha={other}&per_page=100"
