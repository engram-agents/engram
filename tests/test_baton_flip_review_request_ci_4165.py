"""#4165: the author -> colleague (review-request) baton flip reads CI too, and
an empty check rollup is pending, never green.

Contract-first cells; each drives ``cmd_flip`` against a mocked forum client and
a recording fake for ``_pr_ci_state``.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_TOOLS_DIR = str(Path(__file__).parent.parent / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.append(_TOOLS_DIR)
_SRC_DIR = str(Path(__file__).parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import baton as _baton  # noqa: E402
from engram.tools import baton_gate  # noqa: E402

EXIT_VALIDATION = _baton.EXIT_VALIDATION
CONFIG = {"mode": "multi", "agent_name": "borges",
          "forum": {"url": "http://test-forum:5002"}, "primary_user": "casey"}
QUALIFIED = "pr/example-org/example-repo/77"


def _raw(turn="borges", github=QUALIFIED, colleague="ariadne"):
    fm = {
        "project": "PR-77", "title": "t", "status": "in-progress",
        "turn": turn, "turn_since": "2026-06-26T12:00:00Z",
        "turn_reason": "start", "participants": "[borges, ariadne, casey]",
        "author": "borges",
    }
    if github:
        fm["github"] = github
    if colleague:
        fm["colleague"] = colleague
    lines = ["---"] + [f"{k}: {v}" for k, v in fm.items()]
    lines += ["---", "", "## Turn log", "",
              "- 2026-06-26T12:00:00Z initialized → borges: start", ""]
    return "\n".join(lines)


class Recorder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, pr, repo=None):
        self.calls.append((pr, repo))
        return self.result


def _flip(monkeypatch, capsys, *, ci, to="ariadne", invoker="borges", force=False,
          **raw_kw):
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    fake = Recorder(ci)
    monkeypatch.setattr(_baton, "_pr_ci_state", fake)
    approvals = Recorder(("fresh", "ok"))
    monkeypatch.setattr(_baton, "_pr_approval_state", approvals)
    client = MagicMock()
    client.post.return_value = {"seq": 2}
    client.get.return_value = {"raw": _raw(**raw_kw)}
    args = argparse.Namespace(project_id="PR-77", to=to, reason="r", force=force)
    code = None
    try:
        _baton.cmd_flip(args, CONFIG, invoker, client)
    except SystemExit as exc:
        code = exc.code
    return code, client, fake, approvals, capsys.readouterr().err


def test_1_red_refuses_names_check_and_leaves_baton(monkeypatch, capsys):
    code, client, fake, _ap, err = _flip(
        monkeypatch, capsys, ci=("red", "check 'pr-checks' conclusion=FAILURE"))
    assert code == EXIT_VALIDATION
    assert "pr-checks" in err
    client.post.assert_not_called()
    assert fake.calls == [("77", "example-org/example-repo")]


def test_2_red_with_force_flips_and_warns_naming_check(monkeypatch, capsys):
    code, client, _f, _ap, err = _flip(
        monkeypatch, capsys, force=True,
        ci=("red", "check 'pr-checks' conclusion=FAILURE"))
    assert code is None
    client.post.assert_called_once()
    assert "warning" in err and "pr-checks" in err


def test_3_pending_flips_with_loud_warning(monkeypatch, capsys):
    code, client, _f, _ap, err = _flip(
        monkeypatch, capsys, ci=("pending", "check 'pr-checks' not yet started"))
    assert code is None
    client.post.assert_called_once()
    assert "pending" in err and "check 'pr-checks' not yet started" in err
    assert "colleague" in err


def test_4_unknown_flips_with_warning(monkeypatch, capsys):
    code, client, _f, _ap, err = _flip(monkeypatch, capsys, ci=("unknown", "gh timed out"))
    assert code is None
    client.post.assert_called_once()
    assert "could not verify CI" in err and "gh timed out" in err


def test_5_green_flips_without_ci_warning(monkeypatch, capsys):
    code, client, fake, _ap, err = _flip(monkeypatch, capsys, ci=("green", "ok"))
    assert code is None
    client.post.assert_called_once()
    assert "CI" not in err
    assert len(fake.calls) == 1


def test_5b_review_request_flip_reads_no_approval_state(monkeypatch, capsys):
    _c, _cl, _f, approvals, _e = _flip(monkeypatch, capsys, ci=("green", "ok"))
    assert approvals.calls == []


def test_6_return_flip_to_author_never_reads_ci(monkeypatch, capsys):
    code, client, fake, _ap, _err = _flip(
        monkeypatch, capsys, ci=("red", "x"), to="borges", invoker="ariadne",
        turn="ariadne")
    assert code is None
    client.post.assert_called_once()
    assert fake.calls == []


def test_7a_self_flip_never_reads_ci(monkeypatch, capsys):
    code, _cl, fake, _ap, _e = _flip(monkeypatch, capsys, ci=("red", "x"), to="borges")
    assert code is None
    assert fake.calls == []


def test_7b_non_pr_baton_to_colleague_never_reads_ci(monkeypatch, capsys):
    code, client, fake, _ap, _e = _flip(
        monkeypatch, capsys, ci=("red", "x"), github=None)
    assert code is None
    client.post.assert_called_once()
    assert fake.calls == []


def test_8_unqualified_anchor_warns_and_proceeds(monkeypatch, capsys):
    code, client, fake, _ap, err = _flip(
        monkeypatch, capsys, ci=("red", "x"), github="pr/77")
    assert code is None
    client.post.assert_called_once()
    assert fake.calls == []
    assert "CI cannot be verified" in err


def test_predicate_classification():
    f = _baton._is_review_request_flip
    assert f("ariadne", "borges", "ariadne") is True
    assert f("ariadne", "ariadne", "ariadne") is False  # self-flip
    assert f("borges", "ariadne", "ariadne") is False  # return flip
    assert f("ariadne", "borges", "") is False  # no colleague registered


def _rollup(rollup, merge_state):
    return baton_gate._ci_state_from_rollup_output(SimpleNamespace(
        stdout=json.dumps({"statusCheckRollup": rollup,
                           "mergeStateStatus": merge_state})))


def test_10_empty_rollup_not_clean_is_pending():
    assert _rollup([], "BLOCKED")[0] == "pending"
    assert _rollup([], "UNKNOWN")[0] == "pending"


def test_10_empty_rollup_clean_is_green():
    assert _rollup([], "CLEAN")[0] == "green"


# --- Riders -----------------------------------------------------------------

def _posted_reason(client):
    return client.post.call_args[0][1]["reason"]


def test_r1_review_request_force_is_recorded_in_flip_reason(monkeypatch, capsys):
    code, client, _f, _ap, _e = _flip(
        monkeypatch, capsys, force=True,
        ci=("red", "check 'pr-checks' conclusion=FAILURE"))
    assert code is None
    reason = _posted_reason(client)
    assert reason.startswith("r")
    assert "forced past CI red" in reason and "pr-checks" in reason
    assert '"' not in reason


def test_r1_unforced_green_flip_reason_is_untouched(monkeypatch, capsys):
    _c, client, _f, _ap, _e = _flip(monkeypatch, capsys, ci=("green", "ok"))
    assert _posted_reason(client) == "r"


def test_r1_maintainer_force_is_recorded_in_flip_reason(monkeypatch, capsys):
    # to the pool sentinel = the merge-signal flip
    code, client, _f, _ap, _e = _flip(
        monkeypatch, capsys, force=True, to="casey",
        ci=("pending", "check 'pr-checks' not yet started"))
    assert code is None
    reason = _posted_reason(client)
    assert "forced past CI pending" in reason and "pr-checks" in reason


def test_r1_flip_reason_reaches_the_stored_turn_log():
    """The store writes the posted reason verbatim into the turn-log line."""
    # Read as text: importing forum.coordination collides with tools/forum.py
    # on sys.path in this test process.
    src = (Path(__file__).parent.parent / "src" / "forum" / "coordination"
           / "projects.py").read_text(encoding="utf-8")
    flip_src = src[src.index("def flip("):]
    assert "{from_agent} → {to}: {reason}" in flip_src.split("\ndef ")[0]


def test_r2_stale_failure_beside_newer_success_is_not_red():
    rollup = [
        {"name": "pr-checks", "status": "COMPLETED", "conclusion": "FAILURE",
         "completedAt": "2026-10-25T10:00:00Z"},
        {"name": "pr-checks", "status": "COMPLETED", "conclusion": "SUCCESS",
         "completedAt": "2026-10-25T10:05:00Z"},
    ]
    assert _rollup(rollup, "BLOCKED")[0] == "green"


def test_r2_control_newest_failure_is_red():
    rollup = [
        {"name": "pr-checks", "status": "COMPLETED", "conclusion": "SUCCESS",
         "completedAt": "2026-10-25T10:00:00Z"},
        {"name": "pr-checks", "status": "COMPLETED", "conclusion": "FAILURE",
         "completedAt": "2026-10-25T10:05:00Z"},
    ]
    assert _rollup(rollup, "BLOCKED")[0] == "red"


ZERO = "0001-01-01T00:00:00Z"


def _inflight(completed_at):
    run = {"name": "pr-checks", "status": "IN_PROGRESS", "conclusion": ""}
    if completed_at != "MISSING":
        run["completedAt"] = completed_at
    return run


def _finished(conclusion):
    return {"name": "pr-checks", "status": "COMPLETED", "conclusion": conclusion,
            "completedAt": "2026-10-02T15:28:12Z"}


def test_r4a_success_plus_inflight_is_pending():
    assert _rollup([_finished("SUCCESS"), _inflight(ZERO)], "BLOCKED")[0] == "pending"


def test_r4a_order_independent():
    assert _rollup([_inflight(ZERO), _finished("SUCCESS")], "BLOCKED")[0] == "pending"


def test_r4b_cancelled_plus_inflight_is_pending_not_red():
    assert _rollup([_finished("CANCELLED"), _inflight(ZERO)], "BLOCKED")[0] == "pending"
    assert _rollup([_finished("FAILURE"), _inflight(ZERO)], "BLOCKED")[0] == "pending"


def test_r4c_inflight_completed_at_null_empty_or_absent_is_pending():
    for variant in (None, "", "MISSING"):
        assert _rollup([_finished("SUCCESS"), _inflight(variant)], "BLOCKED")[0] == "pending", variant


def test_r4_status_context_pending_beats_finished_context():
    rollup = [
        {"context": "ci/x", "state": "FAILURE"},
        {"context": "ci/x", "state": "PENDING"},
    ]
    assert _rollup(rollup, "BLOCKED")[0] == "pending"


# The real store write, exercised in a fresh interpreter. Importing
# forum.coordination inside this test process collides with tools/forum.py on
# sys.path, so the child gets only src/ on its path.
_STORE_SCRIPT = r"""
import json, sys, tempfile
sys.path.insert(0, sys.argv[1])
from forum.coordination import FileStore, SeqAllocator, flip
from forum.coordination import markdown as md

reason = json.loads(sys.argv[2])
root = tempfile.mkdtemp()
store = FileStore(root)
store.write_project("PR-1", (
    "---\nproject_id: PR-1\ntitle: t\nstatus: in-progress\nturn: borges\n"
    "turn_since: 2026-06-26T00:00:00Z\nturn_reason: \"start\"\n"
    "participants: [ariadne, borges]\n---\n# PR-1\n\nbody\n"), seq=0)
alloc = SeqAllocator(recover=store.recover_max_seq)
flip(store, alloc, "PR-1", to_agent="ariadne", reason=reason,
     ts="2026-06-26T12:00:00Z")
raw = store.read_project("PR-1")
fields, _ = md.parse_frontmatter(raw)
print(json.dumps({"raw": raw, "fields": fields}))
"""


def test_r1_forced_suffix_survives_the_real_store_write():
    """Behavioural: the suffix the real helper builds (detail carrying a double
    quote and a single quote) reaches the stored turn-log line and turn_reason
    verbatim, and the frontmatter still parses."""
    suffix = _baton._flip_ci_gate(
        "red", "check \"pr-checks\" conclusion=FAILURE'", "77",
        force=True, pending_refuses=False)
    assert suffix == " [forced past CI red: check 'pr-checks' conclusion=FAILURE']"
    reason = "r" + suffix
    src_dir = str(Path(__file__).parent.parent / "src")
    out = subprocess.run(
        [sys.executable, "-I", "-c", _STORE_SCRIPT, src_dir, json.dumps(reason)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    stored = json.loads(out.stdout)
    assert f"borges → ariadne: {reason}" in stored["raw"]
    assert stored["fields"]["turn_reason"] == f'"{reason}"'
    assert stored["fields"]["turn"] == "ariadne"


def test_r1_maintainer_holder_flip_to_colleague_reads_ci(monkeypatch, capsys):
    """Predicate scope (#4176): the gate keys on "a flip to the registered
    colleague by anyone other than the colleague", not on the author. A
    maintainer handing the PR back for re-review is gated the same way."""
    red = ("red", "check 'pr-checks' conclusion=FAILURE")
    code, client, fake, _ap, err = _flip(
        monkeypatch, capsys, ci=red, invoker="casey", turn="casey")
    assert code == EXIT_VALIDATION
    assert "pr-checks" in err
    client.post.assert_not_called()
    assert len(fake.calls) == 1
    code, client, fake, _ap, _e = _flip(
        monkeypatch, capsys, ci=("green", "ok"), invoker="casey", turn="casey")
    assert code is None
    client.post.assert_called_once()
    assert len(fake.calls) == 1
