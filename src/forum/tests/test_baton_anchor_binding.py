"""Contract tests: the anchor is write-once after `fairy` and bound to the baton id.

#3039 U2b round 4 (merge-seat ruling on the second reviewer finding), written
BEFORE the fix. The reviewer's repro: a baton `PR-9001` anchored to PR 9001 has
no approval; PR 5 has one. `POST .../anchor` re-pointed the baton at PR 5 and
`advance` then passed on PR 5's approval -- a bypass of the strict gate that
needs no text injection at all.

  A1  `POST .../anchor` on a baton whose SERVER-resolved phase is `peer` or
      `merge` is refused (422, naming the current anchor) for ANY value. Round 6
      (merge-seat ruling) removed the three narrow exceptions this round first
      allowed -- setting an absent anchor, completing an unqualified `pr/<N>`,
      re-sending the identical value: past `fairy` there are NO exceptions and a
      refused request writes nothing. The gate instead DERIVES the repo of an
      unqualified anchor from the admin `repos` table (see
      test_baton_anchor_round6.py). `fairy` / legacy-unset may still re-anchor.
  A2  The baton id binds the anchor: `<prefix>-<N>[-suffix]` => the anchor must be
      `pr/<owner>/<repo>/<N>` with `<owner>/<repo>` equal to the repo the
      admin-owned login map's `repos` table gives for `<prefix>`. Checked in
      `_advance_gate` (so both the `advance` route and the flip spelling), BEFORE
      any GitHub call. `repos` is required in the map file.
  A3  The anchor body is an identifier: control characters => 422, and the value
      must parse the way `baton anchor` accepts it.

Identities are SYNTHETIC; no real fleet name appears here.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from forum import server as server_mod
from forum.coordination import init as _init
from forum.tests.test_baton_advance_api import (
    ANCHOR,
    AUTHOR,
    COLLEAGUE,
    MAP,
    OTHER,
    REPO,
    SEAT,
    SENTINEL,
    FakeGh,
    World,
    rv,
)

OTHER_REPO = "example-org/other-repo"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def disk(world, pid):
    for d in ("projects", "archive"):
        p = Path(world.store.root) / d / f"{pid}.md"
        if p.exists():
            return p.read_bytes().decode("utf-8")
    return None


def seq_state(world):
    return (world.allocator.current(), world.allocator._seq)


def set_map(world, *, repos="default", mutate=None):
    data = json.loads(json.dumps(MAP))
    if repos == "absent":
        data.pop("repos", None)
    elif repos != "default":
        data["repos"] = repos
    if mutate:
        mutate(data)
    world.map_path.write_text(json.dumps(data))


def attempt(world, pid, spelling):
    if spelling == "advance":
        return world.advance(pid)
    return world.flip(pid, to=SEAT)


SPELLINGS = ["advance", "flip"]


def assert_refused_clean(world, pid, resp, *needles, no_gh=True):
    assert resp.status_code == 422, resp.get_json()
    err = resp.get_json()["error"]
    for n in needles:
        assert n.lower() in err.lower(), (n, err)
    if no_gh:
        assert world.gh.calls == [], f"a GitHub call was made before the refusal: {world.gh.calls}"


# ---------------------------------------------------------------------------
# A2 -- the id binds the anchor's number AND repo (both spellings)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spelling", SPELLINGS)
class TestIdBindsAnchor:
    def test_matching_id_number_and_repo_still_advances(self, world, spelling):
        world.seed()
        assert attempt(world, "PR-9001", spelling).status_code == 201

    def test_anchor_number_different_from_the_id_number_refuses(self, world, spelling):
        world.seed(github=f"pr/{REPO}/5")
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        resp = attempt(world, "PR-9001", spelling)
        assert_refused_clean(world, "PR-9001", resp, "9001", "5", REPO)
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0

    def test_anchor_repo_different_from_the_prefix_repo_refuses(self, world, spelling):
        world.seed(github=f"pr/{OTHER_REPO}/9001")
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        resp = attempt(world, "PR-9001", spelling)
        assert_refused_clean(world, "PR-9001", resp, OTHER_REPO, REPO)
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0

    def test_repo_comparison_is_case_insensitive(self, world, spelling):
        world.seed(github="pr/Example-Org/Example-Repo/9001")
        assert attempt(world, "PR-9001", spelling).status_code == 201

    def test_unmapped_prefix_refuses_naming_the_prefix(self, world, spelling):
        world.seed("OTHER-PR-9001", github=f"pr/{REPO}/9001")
        resp = attempt(world, "OTHER-PR-9001", spelling)
        assert_refused_clean(world, "OTHER-PR-9001", resp, "OTHER-PR")

    def test_a_mapped_second_prefix_binds_its_own_repo(self, world, spelling):
        set_map(world, repos={"PR": REPO, "UMWELT-PR": OTHER_REPO})
        world.seed("UMWELT-PR-925", github=f"pr/{OTHER_REPO}/925")
        assert attempt(world, "UMWELT-PR-925", spelling).status_code == 201

    def test_a_mapped_second_prefix_refuses_the_wrong_repo(self, world, spelling):
        set_map(world, repos={"PR": REPO, "UMWELT-PR": OTHER_REPO})
        world.seed("UMWELT-PR-925", github=f"pr/{REPO}/925")
        resp = attempt(world, "UMWELT-PR-925", spelling)
        assert_refused_clean(world, "UMWELT-PR-925", resp, OTHER_REPO, REPO)

    def test_id_with_a_suffix_binds_the_number_before_the_suffix(self, world, spelling):
        world.seed("PR-92-paper", github=f"pr/{REPO}/92")
        assert attempt(world, "PR-92-paper", spelling).status_code == 201
        world.seed("PR-93-paper", github=f"pr/{REPO}/94")
        resp = attempt(world, "PR-93-paper", spelling)
        assert_refused_clean(world, "PR-93-paper", resp, "93", "94", no_gh=False)

    def test_prefix_lookup_is_case_insensitive(self, world, spelling):
        world.seed("pr-9001", github=ANCHOR)
        assert attempt(world, "pr-9001", spelling).status_code == 201

    def test_id_without_a_number_cannot_bind_and_refuses(self, world, spelling):
        world.seed("PR-alpha", github=ANCHOR)
        resp = attempt(world, "PR-alpha", spelling)
        assert_refused_clean(world, "PR-alpha", resp, "PR-alpha")

    def test_legacy_phase_less_baton_is_held_to_the_same_binding(self, world, spelling):
        world.seed(phase=None, github=f"pr/{REPO}/5")
        resp = attempt(world, "PR-9001", spelling)
        assert_refused_clean(world, "PR-9001", resp, "9001", "5")
        world.seed("PR-9002", phase=None, github=f"pr/{REPO}/9002")
        assert attempt(world, "PR-9002", spelling).status_code == 201

    def test_the_flip_that_is_not_peer_to_merge_is_untouched_by_the_binding(self, world, spelling):
        # A review-stage flip to the colleague never reaches the strict gate.
        if spelling == "flip":
            world.seed("PR-9001", phase="fairy", turn=AUTHOR, github=f"pr/{REPO}/5")
            resp = world.flip("PR-9001", to=COLLEAGUE, agent=AUTHOR, reason="please review")
            assert resp.status_code == 201, resp.get_json()


@pytest.mark.parametrize("spelling", SPELLINGS)
class TestRepoTableInTheMap:
    def test_map_without_repos_fails_closed_naming_repos(self, world, spelling):
        set_map(world, repos="absent")
        world.seed()
        resp = attempt(world, "PR-9001", spelling)
        assert_refused_clean(world, "PR-9001", resp, "repos")

    @pytest.mark.parametrize("bad", [
        [], {}, ["PR"], "PR", 5, {"PR": ""}, {"PR": 5}, {"PR": "no-slash"}, {"PR": "a/b/c"},
        {"": REPO}, {"PR": "bad repo/x"}, {"PR": REPO, "pr": OTHER_REPO},
    ], ids=repr)
    def test_malformed_repos_fails_closed_naming_repos(self, world, spelling, bad):
        set_map(world, repos=bad)
        world.seed()
        resp = attempt(world, "PR-9001", spelling)
        assert_refused_clean(world, "PR-9001", resp, "repos")

    def test_repos_is_read_per_request(self, world, spelling):
        world.seed()
        set_map(world, repos={"PR": OTHER_REPO})
        resp = attempt(world, "PR-9001", spelling)
        assert_refused_clean(world, "PR-9001", resp, OTHER_REPO, REPO)
        set_map(world)
        assert attempt(world, "PR-9001", spelling).status_code == 201


# ---------------------------------------------------------------------------
# A1 -- the anchor is write-once after `fairy`
# ---------------------------------------------------------------------------

class PerPrGh(FakeGh):
    """Reviews depend on which PR number the REST reviews path names (#3541)."""

    def __init__(self):
        super().__init__()
        self.by_pr = {}

    def __call__(self, cmd, *a, **kw):
        if cmd[:2] == ["gh", "api"] and "/pulls/" in cmd[2]:
            m = re.search(r"/pulls/([0-9]+)", cmd[2])
            n = m.group(1) if m else None
            self.reviews = self.by_pr.get(int(n), []) if n else []
        return super().__call__(cmd, *a, **kw)


def re_anchor(world, pid, value, agent=AUTHOR):
    return world.client.post(f"/api/projects/{pid}/anchor", json={"agent": agent, "github": value})


class TestReviewersExactRepro:
    def test_approval_on_another_pr_cannot_be_borrowed_by_re_anchoring(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        gh = PerPrGh()
        gh.by_pr = {5: [rv("R1", "example-gh-colleague", "APPROVED")]}
        monkeypatch.setattr(subprocess, "run", gh)
        world.gh = gh
        world.seed()  # peer baton PR-9001 anchored to PR 9001; the approval is on PR 5
        assert world.advance().status_code == 422
        resp = re_anchor(world, "PR-9001", f"pr/{REPO}/5")
        assert resp.status_code == 422, resp.get_json()
        assert world.advance().status_code == 422
        f, _ = world.fields()
        assert f["phase"] == "peer" and f["github"] == ANCHOR


ANCHOR_VALUES = {
    "other-number": f"pr/{REPO}/5",
    "other-repo": f"pr/{OTHER_REPO}/9001",
    "garbage": "garbage",
    "control": f"{ANCHOR}\nphase: merge",
}
ANCHOR_SEEDS = {
    "peer": dict(phase="peer", turn=COLLEAGUE),
    "merge": dict(phase="merge", turn=COLLEAGUE),
    "fairy": dict(phase="fairy", turn=AUTHOR),
    "legacy": dict(phase=None, turn=AUTHOR),
}


@pytest.mark.parametrize("value", list(ANCHOR_VALUES))
@pytest.mark.parametrize("seed", list(ANCHOR_SEEDS))
def test_re_anchor_matrix(world, seed, value):
    world.seed(**ANCHOR_SEEDS[seed])
    new = ANCHOR_VALUES[value]
    before, seq0 = disk(world, "PR-9001"), seq_state(world)
    resp = re_anchor(world, "PR-9001", new)
    locked = seed in ("peer", "merge")
    shaped = value in ("other-number", "other-repo")
    # r9: a fairy/legacy baton may still be re-anchored, but only to the anchor its
    # id names (PR-9001 -> PR 9001 in the mapped repo); a number/repo mismatch is a
    # 422 at the write, bytes and seq unchanged, and the matching anchor is a 201.
    bound_refused = (not locked) and shaped
    if locked or not shaped or bound_refused:
        assert resp.status_code == 422, (seed, value, resp.get_json())
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0
        if locked and value != "control":
            assert ANCHOR in resp.get_json()["error"].lower(), resp.get_json()
        if bound_refused:
            ok = re_anchor(world, "PR-9001", ANCHOR)
            assert ok.status_code == 201, (seed, value, ok.get_json())
            assert world.fields()[0]["github"] == ANCHOR
    else:
        assert resp.status_code == 201, (seed, value, resp.get_json())
        f, _ = world.fields()
        assert f["github"] == new


class TestWriteOnceDetails:
    def test_identical_value_is_refused_not_re_written(self, world):
        # R6 (merge-seat ruling): NO exceptions -- an identical re-send is a write
        # (a row, a seq), so it is refused like any other change.
        world.seed()
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        for value in (ANCHOR, ANCHOR.upper()):
            resp = re_anchor(world, "PR-9001", value)
            assert resp.status_code == 422, resp.get_json()
            assert ANCHOR in resp.get_json()["error"].lower()
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0

    def test_a_legacy_baton_at_the_pool_sentinel_resolves_merge_and_is_locked(self, world):
        world.seed(phase=None, turn=SENTINEL, participants=(AUTHOR, COLLEAGUE, OTHER, SEAT))
        resp = re_anchor(world, "PR-9001", f"pr/{REPO}/5")
        assert resp.status_code == 422 and ANCHOR in resp.get_json()["error"].lower()

    def test_a_baton_with_no_anchor_yet_may_not_be_anchored_in_peer(self, world):
        # R6: setting an absent anchor past `fairy` is the same attack as re-anchoring.
        world.seed(github=None)
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        resp = re_anchor(world, "PR-9001", ANCHOR)
        assert resp.status_code == 422, resp.get_json()
        assert "no github anchor" in resp.get_json()["error"].lower()
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0

    def test_completing_an_unqualified_anchor_with_the_same_number_is_refused_in_peer(self, world):
        # R6: the repo is DERIVED at the gate (D1), never written by a peer/merge request.
        world.seed(github="pr/9001")
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        resp = re_anchor(world, "PR-9001", ANCHOR)
        assert resp.status_code == 422, resp.get_json()
        assert "pr/9001" in resp.get_json()["error"].lower()
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0

    def test_completing_an_unqualified_anchor_with_another_number_is_refused_in_peer(self, world):
        world.seed(github="pr/9001")
        resp = re_anchor(world, "PR-9001", f"pr/{REPO}/5")
        assert resp.status_code == 422 and "pr/9001" in resp.get_json()["error"].lower()

    def test_a_qualified_anchor_cannot_be_dropped_back_to_unqualified_in_peer(self, world):
        world.seed()
        assert re_anchor(world, "PR-9001", "pr/9001").status_code == 422

    def test_a_bunch_baton_may_still_be_re_anchored(self, world):
        _init(world.store, world.allocator, "pool-x", title="bunch", status="in-progress",
              turn=OTHER, participants=[AUTHOR, OTHER], turn_reason="start",
              github="project/7", author=AUTHOR, transition_phase="pool")
        assert re_anchor(world, "pool-x", "project/9").status_code == 201

    def test_without_the_gate_module_a_changed_anchor_fails_closed(self, world):
        world.seed()
        saved = list(server_mod._BATON_GATE_MODULE_CACHE)
        server_mod._BATON_GATE_MODULE_CACHE[:] = [None]
        try:
            resp = re_anchor(world, "PR-9001", f"pr/{REPO}/5")
            assert resp.status_code == 422
            f, _ = world.fields()
            assert f["github"] == ANCHOR
        finally:
            server_mod._BATON_GATE_MODULE_CACHE[:] = saved

    def test_re_anchor_is_refused_before_the_phase_stamp_or_any_write(self, world):
        world.seed(phase=None, turn=SENTINEL)
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        assert re_anchor(world, "PR-9001", f"pr/{REPO}/5").status_code == 422
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0


# ---------------------------------------------------------------------------
# A3 -- the anchor body is an identifier AND must be a shape the CLI can send
# ---------------------------------------------------------------------------

ACCEPTED = ["pr/42", f"pr/{REPO}/42", "project/4", "PR/Example-Org/Example-Repo/42", "pr/o.x/r_y-z/7"]
REJECTED = ["garbage", "pr/", "pr/a/b", "https://github.com/o/r/pull/1", "pr/o/r/12/", "pr/o/r/x",
            "project/", "project/x", "pr/42 ", "pr/ 42", "pr/o/r/ 12", "pr//r/12", "pr/o/r/12 extra"]


# r9: shape tests use a PR-shaped id that carries NO number, so the id binding (which
# would reject numbers like 42 against PR-9001) does not apply; shape is the subject.
SHAPE_ID = "PR-shape"


class TestAnchorShape:
    @pytest.mark.parametrize("value", ACCEPTED)
    def test_cli_shapes_are_accepted_on_a_fairy_baton(self, world, value):
        world.seed(SHAPE_ID, phase="fairy", turn=AUTHOR)
        resp = re_anchor(world, SHAPE_ID, value)
        assert resp.status_code == 201, (value, resp.get_json())

    @pytest.mark.parametrize("value", REJECTED)
    def test_other_shapes_are_422_on_a_fairy_baton(self, world, value):
        world.seed(SHAPE_ID, phase="fairy", turn=AUTHOR)
        before, seq0 = disk(world, SHAPE_ID), seq_state(world)
        resp = re_anchor(world, SHAPE_ID, value)
        # `.strip()` makes the trailing-space forms equal to a valid value: they
        # are the CLI's own `args.github.strip()`; anything that still fails the
        # CLI's regex must fail here.
        stripped_ok = value.strip().lower() in [a.lower() for a in ACCEPTED]
        if stripped_ok:
            assert resp.status_code == 201
        else:
            assert resp.status_code == 422, (value, resp.get_json())
            assert disk(world, SHAPE_ID) == before and seq_state(world) == seq0

    def test_server_accepts_exactly_what_the_cli_regex_accepts(self, world):
        gate = server_mod._load_baton_gate()
        world.seed(SHAPE_ID, phase="fairy", turn=AUTHOR)  # `fairy` may be re-anchored any number of times
        for value in ACCEPTED + REJECTED + ["pr/42\n", "pr/42\r", "PR-42"]:
            resp = re_anchor(world, SHAPE_ID, value)
            cli_ok = bool(gate._GITHUB_ANCHOR_RE.match(value.strip().lower())) and \
                not any(c in value for c in "\n\r")
            assert (resp.status_code == 201) == cli_ok, (value, resp.status_code, resp.get_json())

    def test_control_characters_in_the_anchor_are_422_naming_route_and_field(self, world):
        world.seed(phase="fairy", turn=AUTHOR)
        resp = re_anchor(world, "PR-9001", f"{ANCHOR}\x0bx")
        assert resp.status_code == 422
        err = resp.get_json()["error"]
        assert "/api/projects/<pid>/anchor" in err and "'github'" in err


def test_docs_state_the_write_once_anchor_the_binding_and_the_text_rule():
    root = Path(__file__).resolve().parents[3]
    text = (root / "docs" / "baton-protocol.md").read_text()
    flat = " ".join(text.split())
    assert "write-once" in flat and "`repos`" in flat
    assert "normalised or rejected" in flat
