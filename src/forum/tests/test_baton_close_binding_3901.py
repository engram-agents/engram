"""Contract tests: `merge`, `gc` and `status merged` apply the anchor-to-id binding.

engram-alpha #3901. Before this, only init / anchor / advance / flip checked that a baton's
PR anchor is the one its id names. `merge` and `gc` close an anchored baton from GitHub's
state, and `/status` with `merged` writes the same outcome on the caller's word, so a baton
anchored to a PR its id does not name could be closed as `merged` on that PR's strength.
The three routes now share one helper, `server._close_binding_check`.

The rule these cells pin (merge-seat ruling on the issue):

  * a PR anchor (qualified, or unqualified `pr/<N>` read through the `repos` table) must be
    the one the id names, whenever a login map loads; checked BEFORE any GitHub read, so a
    refusal is 422, writes nothing, and makes no gh call;
  * no login map configured: no check, and the 201 body says so
    (`anchor_binding: "not_checked: no login map configured"`); a map that is configured
    but cannot load refuses (fail closed);
  * ON or OFF (GitHub integration) alike: the binding is a local check;
  * the refusal names the way out: a missing `repos` row -> add it and re-run; a mis-bound
    baton -> supersede it and close this one `cancelled` (which keeps working through
    `/status`, because a cancel asserts nothing about a PR).

Identities are SYNTHETIC; GitHub is never contacted (the `gh` boundary is the FakeGh seam
from test_baton_advance_api).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from forum import server as server_mod
from forum.coordination.markdown import parse_frontmatter
from forum.tests.test_baton_advance_api import (
    AUTHOR,
    COLLEAGUE,
    OTHER,
    REPO,
    SEAT,
    SENTINEL,
    World,
)
from forum.tests.test_baton_anchor_binding import disk, seq_state, set_map
from forum.tests.test_baton_prefix_table_3884 import merged_seam

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "engram" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton as _baton  # noqa: E402
from forum_api import ForumHttpError  # noqa: E402

PID = "PR-9001"
BOUND = f"pr/{REPO}/9001"
OTHER_REPO = "example-org/other-repo"
NOT_CHECKED = "not_checked: no login map configured"

CLOSERS = ["merge", "gc", "status"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def world_nomap(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, with_map=False)


def close_as_merged(world, pid, how):
    if how == "merge":
        return world.client.post(f"/api/projects/{pid}/merge", json={"agent": SEAT})
    if how == "gc":
        return world.client.post(
            f"/api/projects/{pid}/gc",
            json={"agent": SEAT, "new_status": "merged", "reason": "gc: merged"})
    return world.client.post(
        f"/api/projects/{pid}/status",
        json={"agent": SEAT, "new_status": "merged", "reason": "merged"})


def closed_status(world, pid):
    """The status on disk, wherever the baton lives (merge archives it)."""
    return parse_frontmatter(disk(world, pid))[0]["status"]


def assert_nothing_written(world, pid, before, seq0):
    assert disk(world, pid) == before and seq_state(world) == seq0
    assert world.fields(pid)[0]["status"] == "in-progress"
    assert world.gh.calls == [], f"a GitHub call was made: {world.gh.calls}"


def refuse(world, monkeypatch, pid, how, *needles, absent=()):
    merged_seam(world, monkeypatch)
    before, seq0 = disk(world, pid), seq_state(world)
    resp = close_as_merged(world, pid, how)
    assert resp.status_code == 422, resp.get_json()
    err = resp.get_json()["error"]
    for n in needles:
        assert n in err, (n, err)
    for n in absent:
        assert n not in err, (n, err)
    assert_nothing_written(world, pid, before, seq0)
    return err


# ---------------------------------------------------------------------------
# 1. Row absent (map loaded)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
def test_row_absent_refuses_naming_the_missing_row_and_the_rerun(world, monkeypatch, how):
    set_map(world, repos={"OTHER": REPO})
    world.seed(PID, github=BOUND)
    err = refuse(world, monkeypatch, PID, how, "'repos' table", "'PR'",
                 "'repos' row", "re-run", absent=("status cancelled",))
    # merge names gc: the PR is already merged on GitHub, so re-running merge would fail.
    assert {"merge": "baton gc", "gc": "baton gc",
            "status": "baton close --status merged"}[how] in err


# ---------------------------------------------------------------------------
# 2 / 3. Mis-bound: other repo, other number
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
@pytest.mark.parametrize("anchor", [f"pr/{OTHER_REPO}/9001", f"pr/{REPO}/5"],
                         ids=["other-repo", "other-number"])
def test_mis_bound_refuses_with_the_cancel_and_replace_way_out(world, monkeypatch, how, anchor):
    world.seed(PID, github=anchor)
    refuse(world, monkeypatch, PID, how, "does not match its id", anchor,
           "status cancelled", "superseded by", "correctly prefixed baton",
           absent=("'repos' row",))


# ---------------------------------------------------------------------------
# 4. Bound
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
def test_bound_baton_closes_and_reports_the_binding_was_checked(world, monkeypatch, how):
    world.seed(PID, github=BOUND)
    merged_seam(world, monkeypatch)
    resp = close_as_merged(world, PID, how)
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["anchor_binding"] == "checked"
    assert closed_status(world, PID) == "merged"


# ---------------------------------------------------------------------------
# 5. Unqualified pr/<N>: the repo is DERIVED from the table, the literal anchor is named
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
class TestUnqualifiedAnchor:
    def test_with_a_row_and_the_right_number_it_is_bound(self, world, monkeypatch, how):
        world.seed(PID, github="pr/9001")
        merged_seam(world, monkeypatch)
        resp = close_as_merged(world, PID, how)
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["anchor_binding"] == "checked"

    def test_a_wrong_number_refuses_naming_the_literal_anchor(self, world, monkeypatch, how):
        world.seed(PID, github="pr/5")
        refuse(world, monkeypatch, PID, how, "'pr/5'", "9001", "status cancelled")

    def test_without_a_row_it_is_the_table_refusal(self, world, monkeypatch, how):
        world.seed("ZZ-9001", github="pr/9001")
        refuse(world, monkeypatch, "ZZ-9001", how, "'repos' table", "'ZZ'", "'repos' row")


# ---------------------------------------------------------------------------
# 6. No map configured: no check, and the response says so
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
def test_no_map_configured_closes_and_says_the_binding_was_not_checked(world_nomap, monkeypatch, how):
    world_nomap.seed(PID, github=f"pr/{OTHER_REPO}/5")  # would be mis-bound if checked
    merged_seam(world_nomap, monkeypatch)
    resp = close_as_merged(world_nomap, PID, how)
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["anchor_binding"] == NOT_CHECKED
    assert closed_status(world_nomap, PID) == "merged"


# ---------------------------------------------------------------------------
# 7. A map that is configured but cannot load refuses (fail closed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
def test_configured_but_unloadable_map_refuses_with_the_load_error(world, monkeypatch, how):
    world.seed(PID, github=BOUND)
    world.map_path.write_text("{ nope")
    refuse(world, monkeypatch, PID, how, "not valid JSON")


# ---------------------------------------------------------------------------
# 8. GitHub integration OFF: the binding is a local check and still applies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
class TestGithubOff:
    def test_mis_bound_still_refuses(self, world, monkeypatch, how):
        world.app.config["GITHUB_INTEGRATION"] = "off"
        world.seed(PID, github=f"pr/{REPO}/5")
        refuse(world, monkeypatch, PID, how, "does not match its id", "status cancelled")

    def test_bound_still_closes(self, world, monkeypatch, how):
        world.app.config["GITHUB_INTEGRATION"] = "off"
        world.seed(PID, github=BOUND)
        resp = close_as_merged(world, PID, how)
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["anchor_binding"] == "checked"
        assert world.gh.calls == []


# ---------------------------------------------------------------------------
# 9. Unanchored baton: unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
class TestUnanchored:
    def test_a_participant_closes_it_as_before(self, world, how):
        world.seed("ISSUE-5", github=None)
        resp = close_as_merged(world, "ISSUE-5", how)
        assert resp.status_code == 201, resp.get_json()
        assert "anchor_binding" not in resp.get_json()
        assert world.gh.calls == []

    def test_a_non_participant_is_still_refused_403(self, world, how):
        world.seed("ISSUE-5", github=None, participants=(AUTHOR, COLLEAGUE))
        resp = close_as_merged(world, "ISSUE-5", how)
        assert resp.status_code == 403, resp.get_json()


# ---------------------------------------------------------------------------
# 12. /status: merged is checked, cancelled is not (and is the documented way out)
# ---------------------------------------------------------------------------

class TestStatusCancelIsTheWayOut:
    def test_merged_on_a_mis_bound_baton_is_refused_then_cancelled_succeeds(self, world):
        world.seed(PID, github=f"pr/{REPO}/5")
        before, seq0 = disk(world, PID), seq_state(world)
        resp = close_as_merged(world, PID, "status")
        assert resp.status_code == 422, resp.get_json()
        assert "status cancelled" in resp.get_json()["error"]
        assert_nothing_written(world, PID, before, seq0)

        resp = world.client.post(
            f"/api/projects/{PID}/status",
            json={"agent": SEAT, "new_status": "cancelled",
                  "reason": f"mis-bound: anchor pr/{REPO}/5 does not match prefix PR; "
                            "superseded by PR-9010"})
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["status"] == "cancelled"
        assert "anchor_binding" not in resp.get_json()
        assert world.fields(PID)[0]["status"] == "cancelled"

    def test_an_already_closed_baton_still_answers_409(self, world):
        world.seed(PID, github=f"pr/{REPO}/5")
        world.client.post(f"/api/projects/{PID}/status",
                          json={"agent": SEAT, "new_status": "cancelled", "reason": "x"})
        resp = close_as_merged(world, PID, "status")
        assert resp.status_code == 409, resp.get_json()


# ---------------------------------------------------------------------------
# 13. GitHub OFF + a map that is configured but cannot load: still refuses (fail closed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
def test_github_off_with_an_unloadable_map_refuses_with_the_load_error(world, monkeypatch, how):
    world.app.config["GITHUB_INTEGRATION"] = "off"
    world.seed(PID, github=BOUND)
    world.map_path.write_text("{ nope")
    refuse(world, monkeypatch, PID, how, "not valid JSON")


# ---------------------------------------------------------------------------
# 14. Re-anchored between the binding check and the write: 409, nothing written
# ---------------------------------------------------------------------------

def reanchor_after_binding_check(world, monkeypatch, pid, new_anchor):
    """Run the real binding check, then move the anchor on disk (a concurrent `anchor`)."""
    real = server_mod._close_binding_check
    path = Path(world.store.root) / "projects" / f"{pid}.md"

    def check_then_reanchor(*a, **kw):
        out = real(*a, **kw)
        path.write_text(re.sub(r"(?m)^github:.*$", f"github: {new_anchor}",
                               path.read_text(), count=1))
        return out

    monkeypatch.setattr(server_mod, "_close_binding_check", check_then_reanchor)


@pytest.mark.parametrize("how", CLOSERS)
def test_an_anchor_moved_after_the_binding_check_is_a_409_and_writes_nothing(
        world, monkeypatch, how):
    moved = f"pr/{REPO}/5"  # mis-bound for PR-9001: closing on its strength is the hazard
    world.seed(PID, github=BOUND)
    merged_seam(world, monkeypatch)
    reanchor_after_binding_check(world, monkeypatch, PID, moved)
    seq0 = seq_state(world)
    resp = close_as_merged(world, PID, how)
    assert resp.status_code == 409, resp.get_json()
    err = resp.get_json()["error"]
    assert "anchor changed" in err and BOUND in err and moved in err, err
    assert world.fields(PID)[0]["status"] == "in-progress"
    assert closed_status(world, PID) == "in-progress"
    assert seq_state(world) == seq0
    assert Path(world.store.root, "archive", f"{PID}.md").exists() is False


# ---------------------------------------------------------------------------
# 15. The gate module unavailable on an anchored baton: 503, nothing written
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
def test_gate_module_unavailable_is_a_503_and_writes_nothing(world, monkeypatch, how):
    world.seed(PID, github=BOUND)
    merged_seam(world, monkeypatch)
    before, seq0 = disk(world, PID), seq_state(world)
    monkeypatch.setattr(server_mod, "_load_baton_gate", lambda: None)
    resp = close_as_merged(world, PID, how)
    assert resp.status_code == 503, resp.get_json()
    assert "baton gate is unavailable" in resp.get_json()["error"]
    assert disk(world, PID) == before and seq_state(world) == seq0
    assert world.fields(PID)[0]["status"] == "in-progress"


# ---------------------------------------------------------------------------
# 16. An id with no PR number: the refusal names the cancel way out
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
def test_an_id_without_a_pr_number_refuses_naming_the_cancel_way_out(world, monkeypatch, how):
    world.seed("NONUM", github=BOUND)
    refuse(world, monkeypatch, "NONUM", how, "carries no PR number", "status cancelled",
           absent=("'repos' row", "correctly prefixed baton"))


# ---------------------------------------------------------------------------
# Client cells: the baton CLI shows the server's refusal text
# ---------------------------------------------------------------------------

class ServerClient:
    """The baton CLI's client interface, answered by the real Flask app of a World."""

    def __init__(self, world):
        self.world = world

    def get(self, path, params=None):
        resp = self.world.client.get(path, query_string=params or {})
        if resp.status_code >= 400:
            raise ForumHttpError(resp.status_code, resp.get_data(as_text=True))
        return resp.get_json()

    def post(self, path, body=None):
        resp = self.world.client.post(path, json=body or {})
        if resp.status_code >= 400:
            raise ForumHttpError(resp.status_code, resp.get_data(as_text=True))
        return resp.get_json()


CLI_CONFIG = {"mode": "multi", "forum": {"url": "http://test-forum:5002"}}


def cli_gh_seam(world, monkeypatch, merge_calls=None):
    """The client's gh calls (`--json state -q .state`, `pr merge`) and the server's, one seam."""
    inner = world.gh

    def run(cmd, *a, **kw):
        if cmd[:3] == ["gh", "pr", "view"] and "-q" in cmd and ".state" in cmd:
            world.gh.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="MERGED\n", stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            if merge_calls is not None:
                merge_calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "view"] and cmd[-2:] == ["--json", "state"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"state": "MERGED"}), stderr="")
        return inner(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")


class TestClientGcSweep:
    def test_a_mis_bound_baton_is_reported_with_the_server_text_and_fails_the_sweep(
            self, world, monkeypatch, capsys):
        import argparse
        world.seed("PR-9001", github=BOUND)
        world.seed("PR-9002", github=f"pr/{REPO}/5")
        cli_gh_seam(world, monkeypatch)
        args = argparse.Namespace(dry_run=False, limit=30)
        with pytest.raises(SystemExit) as exc:
            _baton.cmd_gc(args, CLI_CONFIG, SEAT, ServerClient(world))
        assert exc.value.code != 0
        out = capsys.readouterr()
        assert closed_status(world, "PR-9001") == "merged"
        assert world.fields("PR-9002")[0]["status"] == "in-progress"
        assert "server refused PR-9002 (HTTP 422)" in out.err
        assert "does not match its id" in out.err and "status cancelled" in out.err
        assert "ForumHttpError" not in out.err
        assert "failed 1" in out.out
        assert "closed PR-9001" in out.out


class TestClientMerge:
    def test_a_422_prints_the_server_text_and_not_the_close_as_merged_hint(
            self, world, monkeypatch, capsys):
        import argparse
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: SENTINEL)
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda *a, **kw: ("green", "ok"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda *a, **kw: ("fresh", "ok"))
        world.seed("PR-9001", github=f"pr/{OTHER_REPO}/9001", turn=SEAT)
        merge_calls = []
        cli_gh_seam(world, monkeypatch, merge_calls)
        args = argparse.Namespace(pr="9001", force=False, dry_run=False)
        with pytest.raises(SystemExit) as exc:
            _baton.cmd_merge(args, CLI_CONFIG, SEAT, ServerClient(world))
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert len(merge_calls) == 1  # the GitHub merge ran; the baton did not close
        assert "GitHub merge succeeded" in err
        assert "does not match its id" in err and "status cancelled" in err
        assert "--status merged" not in err
        assert world.fields("PR-9001")[0]["status"] == "in-progress"


class TestClientClose:
    def test_close_as_merged_prints_the_server_text_on_a_422(self, world, capsys):
        import argparse
        world.seed("PR-9001", github=f"pr/{REPO}/5")
        args = argparse.Namespace(project_id="PR-9001", status="merged")
        with pytest.raises(SystemExit) as exc:
            _baton.cmd_close(args, CLI_CONFIG, SEAT, ServerClient(world))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "refused (HTTP 422)" in err and "does not match its id" in err
        assert not err.lstrip().startswith("baton close: server error")
        assert '{"error"' not in err


# ---------------------------------------------------------------------------
# 17. init: a baton is born open (the fourth door to `merged`)
# ---------------------------------------------------------------------------

def init_body(pid, status, github=None):
    body = {
        "agent": AUTHOR, "project_id": pid, "title": "t", "status": status,
        "turn": AUTHOR, "turn_reason": "r",
        "participants": [AUTHOR, COLLEAGUE, SENTINEL, SEAT],
        "colleague": COLLEAGUE, "author": AUTHOR,
    }
    if github is not None:
        body["github"] = github
    return body


@pytest.mark.parametrize("closed", ["merged", "cancelled"])
def test_init_refuses_a_closed_status_and_creates_nothing(world, closed):
    seq0 = seq_state(world)
    resp = world.client.post("/api/projects", json=init_body("ZED-9001", closed, github=BOUND))
    assert resp.status_code == 422, resp.get_json()
    err = resp.get_json()["error"]
    assert "born open" in err and "close it" in err and "Nothing was created" in err, err
    assert world.store.read_project("ZED-9001") is None
    assert disk(world, "ZED-9001") is None and seq_state(world) == seq0


@pytest.mark.parametrize("how", CLOSERS)
def test_init_then_merged_still_goes_through_the_binding_helper(world, monkeypatch, how):
    # GitHub OFF so init skips its PR-exists lookup; prefix ZED has no `repos` row, which
    # init lets through (it checks the repo only when a row exists) and the close refuses.
    world.app.config["GITHUB_INTEGRATION"] = "off"
    resp = world.client.post("/api/projects", json=init_body("ZED-9001", "in-progress", github=BOUND))
    assert resp.status_code == 201, resp.get_json()
    before, seq0 = disk(world, "ZED-9001"), seq_state(world)
    resp = close_as_merged(world, "ZED-9001", how)
    assert resp.status_code == 422, resp.get_json()
    assert "'repos' row" in resp.get_json()["error"]
    assert disk(world, "ZED-9001") == before and seq_state(world) == seq0
    assert world.fields("ZED-9001")[0]["status"] == "in-progress"


@pytest.mark.parametrize("closed", ["merged", "cancelled"])
def test_client_init_status_choices_are_the_active_statuses(closed, capsys):
    parser = _baton.build_parser()
    argv = ["init", "PR-9001", "--title", "t", "--participants", "a,b"]
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv + ["--status", closed])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert parser.parse_args(argv + ["--status", "in-review"]).status == "in-review"


# ---------------------------------------------------------------------------
# 17. Case: the id prefix and the stored anchor are read case-insensitively
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
def test_a_lower_case_id_with_a_matching_anchor_closes_checked(world, monkeypatch, how):
    # The 'repos' table is keyed by the UPPER-case prefix; the id's prefix is upper-cased
    # before the lookup, so 'pr-9001' finds the 'PR' row.
    world.seed("pr-9001", github="pr/9001")
    merged_seam(world, monkeypatch)
    resp = close_as_merged(world, "pr-9001", how)
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["anchor_binding"] == "checked"
    assert closed_status(world, "pr-9001") == "merged"


@pytest.mark.parametrize("how", CLOSERS)
def test_an_upper_case_stored_anchor_that_matches_closes_checked(world, monkeypatch, how):
    # A hand-edited file can hold the anchor in any case; the check lower-cases it first.
    world.seed(PID, github=BOUND.upper())
    merged_seam(world, monkeypatch)
    resp = close_as_merged(world, PID, how)
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["anchor_binding"] == "checked"
    assert closed_status(world, PID) == "merged"


# ---------------------------------------------------------------------------
# 18. A project/<N> anchor binds to nothing: closes with no anchor_binding key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("how", CLOSERS)
@pytest.mark.parametrize("pid", ["bunch-lab-7", PID], ids=["bunch-id", "pr-shaped-id"])
def test_a_project_anchor_with_a_map_configured_closes_with_no_binding_key(
        world, monkeypatch, how, pid):
    world.seed(pid, github="project/7")
    merged_seam(world, monkeypatch)
    resp = close_as_merged(world, pid, how)
    assert resp.status_code == 201, resp.get_json()
    assert "anchor_binding" not in resp.get_json()
    assert closed_status(world, pid) == "merged"
