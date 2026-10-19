"""Contract tests: the baton login map's `repos` (id-prefix) table gate on a non-PR prefix.

engram-alpha #3884. `baton flip` / `advance` on `STUDIO-PR-34` was refused 422
because the deployed map's admin-owned `repos` table had no `STUDIO-PR` row.
These cells pin what the server does TODAY for a prefix other than `PR`, so a
missing or mis-keyed row stays a loud, specific refusal. They add no real map
rows: the table is admin config on the forum host.

Measured on dev (see the PR body), prefix row ABSENT:

  advance                       -> 422, the repos-table refusal, nothing written
  flip to the merge seat        -> 422, the same refusal, nothing written
  flip to a non-merge seat      -> accepted (never reaches the strict gate)
  merge                         -> NOT prefix-gated: closes when GitHub reports
                                   MERGED (a separate question whether it should bind)
  close                         -> accepted

Identities are SYNTHETIC; GitHub is never contacted (the `gh` boundary is the
FakeGh seam from test_baton_advance_api).
"""

import json
import subprocess

import pytest

from forum.tests.test_baton_advance_api import (
    AUTHOR,
    COLLEAGUE,
    REPO,
    SEAT,
    World,
)
from forum.tests.test_baton_anchor_binding import (
    assert_refused_clean,
    attempt,
    disk,
    seq_state,
    set_map,
)

PID = "STUDIO-PR-34"
STUDIO_REPO = "example-org/studio-repo"
ANCHOR = f"pr/{STUDIO_REPO}/34"
ROW = {"PR": REPO, "STUDIO-PR": STUDIO_REPO}
NO_ROW = {"PR": REPO}
TABLE_REFUSAL = "'repos' table"

GATED = ["advance", "flip"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def merged_seam(world, monkeypatch):
    """Answer `gh pr view <n> --repo <r> --json state` with MERGED; defer the rest to FakeGh."""
    inner = world.gh

    def run(cmd, *a, **kw):
        if cmd[:3] == ["gh", "pr", "view"] and cmd[-2:] == ["--json", "state"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"state": "MERGED"}), stderr="")
        return inner(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


def merge(world, pid=PID):
    return world.client.post(f"/api/projects/{pid}/merge", json={"agent": COLLEAGUE})


def close(world, pid=PID):
    return world.client.post(
        f"/api/projects/{pid}/status",
        json={"agent": COLLEAGUE, "new_status": "closed", "reason": "done"})


# ---------------------------------------------------------------------------
# 1. Row absent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", GATED)
class TestRowAbsentRefusesTheGatedVerbs:
    def test_refuses_naming_the_table_prefix_and_id_and_writes_nothing(self, world, spelling):
        set_map(world, repos=NO_ROW)
        world.seed(PID, github=ANCHOR)
        before, seq0 = disk(world, PID), seq_state(world)
        resp = attempt(world, PID, spelling)
        assert_refused_clean(world, PID, resp, TABLE_REFUSAL, "STUDIO-PR", PID)
        assert disk(world, PID) == before and seq_state(world) == seq0
        f, _ = world.fields(PID)
        assert f["turn"] == COLLEAGUE and f["status"] == "in-progress" and f["phase"] == "peer"


class TestRowAbsentLeavesTheOtherVerbsAlone:
    def test_flip_to_a_non_merge_seat_is_accepted(self, world):
        set_map(world, repos=NO_ROW)
        world.seed(PID, github=ANCHOR)
        resp = world.flip(PID, to=AUTHOR, agent=COLLEAGUE, reason="changes requested")
        assert resp.status_code == 201, resp.get_json()
        assert world.fields(PID)[0]["turn"] == AUTHOR
        assert world.gh.calls == []

    def test_merge_is_not_prefix_gated_and_closes_on_github_merged(self, world, monkeypatch):
        set_map(world, repos=NO_ROW)
        world.seed(PID, github=ANCHOR)
        merged_seam(world, monkeypatch)
        resp = merge(world)
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["status"] == "merged"
        assert TABLE_REFUSAL not in json.dumps(resp.get_json())
        assert disk(world, PID) is not None  # archived, not lost

    def test_close_is_accepted(self, world):
        set_map(world, repos=NO_ROW)
        world.seed(PID, github=ANCHOR)
        resp = close(world)
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["status"] == "closed"
        assert world.fields(PID)[0]["status"] == "closed"
        assert world.gh.calls == []


# ---------------------------------------------------------------------------
# 2. Row present
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", GATED)
def test_row_present_gets_past_the_prefix_gate_and_advances(world, spelling):
    set_map(world, repos=ROW)
    world.seed(PID, github=ANCHOR)
    resp = attempt(world, PID, spelling)
    assert resp.status_code == 201, resp.get_json()
    assert TABLE_REFUSAL not in json.dumps(resp.get_json())
    f, _ = world.fields(PID)
    assert f["phase"] == "merge" and f["turn"] == SEAT


@pytest.mark.parametrize("spelling", GATED)
def test_row_present_but_unapproved_stops_at_a_later_gate_not_the_table(world, spelling):
    set_map(world, repos=ROW)
    world.seed(PID, github=ANCHOR)
    world.gh.reviews = []
    resp = attempt(world, PID, spelling)
    assert resp.status_code == 422, resp.get_json()
    err = resp.get_json()["error"]
    assert TABLE_REFUSAL not in err
    assert "approval check refused on PR #34" in err and "no current approval" in err
    assert world.fields(PID)[0]["phase"] == "peer"


# ---------------------------------------------------------------------------
# 5. Batons with no PR anchor never consult the table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid", ["ISSUE-5", "PR1-5"])
class TestUnanchoredBatonsNeedNoRow:
    def test_advance_is_refused_for_the_missing_anchor_not_the_missing_row(self, world, pid):
        set_map(world, repos=NO_ROW)
        world.seed(pid, github=None)
        resp = attempt(world, pid, "advance")
        assert_refused_clean(world, pid, resp, "has no github anchor")
        assert TABLE_REFUSAL not in resp.get_json()["error"]

    def test_flip_to_the_merge_seat_is_accepted(self, world, pid):
        set_map(world, repos=NO_ROW)
        world.seed(pid, github=None)
        resp = attempt(world, pid, "flip")
        assert resp.status_code == 201, resp.get_json()
        assert world.gh.calls == []


def test_row_present_flip_to_non_merge_seat_merge_and_close_still_work(world, monkeypatch):
    set_map(world, repos=ROW)
    world.seed(PID, github=ANCHOR)
    assert world.flip(PID, to=AUTHOR, agent=COLLEAGUE, reason="changes requested").status_code == 201
    merged_seam(world, monkeypatch)
    assert merge(world).status_code == 201


# ---------------------------------------------------------------------------
# 3. Case-insensitive prefix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", GATED)
class TestPrefixIsCaseInsensitive:
    def test_lowercase_id_matches_the_uppercase_row(self, world, spelling):
        set_map(world, repos=ROW)
        world.seed("studio-pr-34", github=ANCHOR)
        resp = attempt(world, "studio-pr-34", spelling)
        assert resp.status_code == 201, resp.get_json()
        assert world.fields("studio-pr-34")[0]["phase"] == "merge"

    def test_lowercase_id_with_no_row_names_the_upper_cased_prefix(self, world, spelling):
        set_map(world, repos=NO_ROW)
        world.seed("studio-pr-34", github=ANCHOR)
        resp = attempt(world, "studio-pr-34", spelling)
        assert_refused_clean(world, "studio-pr-34", resp, TABLE_REFUSAL, "STUDIO-PR")
        # assert_refused_clean lower-cases both sides, so pin the case exactly here.
        assert "prefix 'STUDIO-PR'" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# 4. Wrong repo for the prefix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", GATED)
def test_row_present_but_anchor_names_another_repo_refuses_on_the_repo_not_the_table(world, spelling):
    set_map(world, repos=ROW)
    world.seed(PID, github=f"pr/{REPO}/34")
    before, seq0 = disk(world, PID), seq_state(world)
    resp = attempt(world, PID, spelling)
    assert_refused_clean(world, PID, resp, "does not match its id", STUDIO_REPO, REPO)
    assert TABLE_REFUSAL not in resp.get_json()["error"]
    assert disk(world, PID) == before and seq_state(world) == seq0
