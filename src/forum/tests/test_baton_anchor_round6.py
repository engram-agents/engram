"""Contract tests: #3039 U2b round 6 -- the anchor is write-once with NO exceptions.

Written BEFORE the implementation (contract-first). The merge seat's ruling on
the anchor rule: past ``fairy`` a request may never write the anchor at all.
Setting an absent anchor at peer/merge is the same attack as re-anchoring; the
repo is DERIVED from the baton-id prefix via the admin-owned login map's
``repos`` table (never written by a peer/merge request); an identical re-send is
a write (a row, a seq).

  R-a  ``POST /anchor`` on a baton whose server-resolved phase is ``peer`` /
       ``merge`` (or cannot be resolved) is a 422 in EVERY case -- absent,
       unqualified-being-completed, identical re-send, different -- naming the
       current anchor (or saying there is none); no row, no seq.
  R-b  ``POST /advance`` (and the flip spelling) on a PR baton with NO anchor is
       a 422 naming the missing anchor: phase unchanged, zero gh calls, no seq.
  R-c  an identical re-send in peer/merge is a 422 and consumes no seq.
  D1   in peer/merge the gate reads an UNQUALIFIED ``pr/<N>`` as
       ``pr/<repos[prefix]>/<N>`` for the id binding (derive, never write); the
       number must still equal the id's; an unmapped prefix still refuses.
  D2   init of a PR-shaped baton with an UNQUALIFIED ``pr/<N>`` anchor qualifies
       it from the admin map by id prefix, or refuses 422 (unmapped / no map /
       number differs from the id's). Qualified anchors, ``project/<N>`` and
       bunches are untouched.
  S5   an over-cap (5000-digit) anchor number is a 422 at init, and a baton that
       somehow carries one never 500s on ``/advance`` or ``/anchor``.

Identities are SYNTHETIC; no real fleet name appears here.
"""

import sys

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import (  # noqa: E402
    World, ANCHOR, AUTHOR, COLLEAGUE, OTHER, SEAT, SENTINEL, REPO,
)
from test_baton_anchor_binding import disk, seq_state  # noqa: E402

from forum import server as server_mod  # noqa: E402
from forum.coordination import init as _init  # noqa: E402

OTHER_REPO = "example-org/other-repo"
PID = "PR-9001"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def world_nomap(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, with_map=False)


def anchor(world, value, pid=PID, **extra):
    return world.client.post(f"/api/projects/{pid}/anchor",
                             json={"agent": AUTHOR, "github": value, **extra})


def make_unresolvable(world, monkeypatch):
    def boom(*a, **kw):
        raise world.gate.PoolSentinelUnresolved("no sentinel in this test")
    monkeypatch.setattr(world.gate, "_resolve_pool_sentinel", boom)


def seed_state(world, monkeypatch, kind, github):
    """kind: peer | merge | unresolved (legacy phase-less baton, sentinel unresolvable)."""
    if kind == "unresolved":
        world.seed(phase=None, turn=AUTHOR, github=github)
        make_unresolvable(world, monkeypatch)
    else:
        world.seed(phase=kind, github=github)


# ------------------------------------------------------------------ R-a / R-c --

NEW_VALUES = {
    "identical": ANCHOR,
    "identical-upper": ANCHOR.upper(),
    "different-number": f"pr/{REPO}/5",
    "different-repo": f"pr/{OTHER_REPO}/9001",
    "unqualified": "pr/9001",
}
# (current anchor on the baton, new values that are meaningful against it)
CURRENT = {
    "absent": (None, ["identical", "different-number", "different-repo", "unqualified"]),
    "unqualified": ("pr/9001", ["identical", "different-number", "different-repo", "unqualified"]),
    "qualified": (ANCHOR, list(NEW_VALUES)),
}


def _cases():
    for kind in ("peer", "merge", "unresolved"):
        for cur, (gh, values) in CURRENT.items():
            for v in values:
                yield pytest.param(kind, cur, v, id=f"{kind}-{cur}-{v}")


class TestAnchorNeverWrittenPastFairy:
    @pytest.mark.parametrize("kind,cur,value", list(_cases()))
    def test_refused_in_every_case_nothing_written(self, world, monkeypatch, kind, cur, value):
        seed_state(world, monkeypatch, kind, CURRENT[cur][0])
        before, seq0 = disk(world, PID), seq_state(world)
        resp = anchor(world, NEW_VALUES[value])
        assert resp.status_code == 422, (kind, cur, value, resp.get_json())
        err = resp.get_json()["error"].lower()
        if cur == "absent":
            assert "no github anchor" in err, err
        else:
            assert CURRENT[cur][0] in err, err
        assert disk(world, PID) == before          # file bytes unchanged
        assert seq_state(world) == seq0            # no seq consumed, no log row

    @pytest.mark.parametrize("kind", ["peer", "merge"])
    def test_R_c_identical_resend_is_422_and_consumes_no_seq(self, world, monkeypatch, kind):
        seed_state(world, monkeypatch, kind, ANCHOR)
        seq0, before = seq_state(world), disk(world, PID)
        for _ in range(2):
            assert anchor(world, ANCHOR).status_code == 422
        assert seq_state(world) == seq0 and disk(world, PID) == before
        assert world.fields()[0]["github"] == ANCHOR

    def test_fairy_keeps_set_change_and_identical_resend(self, world):
        world.seed(phase="fairy", github=None, turn=AUTHOR)
        assert anchor(world, ANCHOR).status_code == 201          # set when none
        assert anchor(world, ANCHOR).status_code == 201          # identical
        # r9: a change is still allowed in fairy, but only to an id-bound anchor
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, f"pr/{OTHER_REPO}/9001").status_code == 422  # wrong repo
        assert disk(world, PID) == before and seq_state(world) == seq0
        assert anchor(world, "pr/9001").status_code == 201                 # change (qualified -> bare)
        assert world.fields()[0]["github"] == "pr/9001"

    def test_legacy_unset_baton_keeps_set_and_identical_resend(self, world):
        world.seed(phase=None, github=None, turn=AUTHOR)  # resolves "" (real agent holds it)
        assert anchor(world, ANCHOR).status_code == 201
        assert anchor(world, ANCHOR).status_code == 201

    def test_bunch_baton_still_re_anchors(self, world):
        _init(world.store, world.allocator, "pool-x", title="bunch", status="in-progress",
              turn=OTHER, participants=[AUTHOR, OTHER], turn_reason="start",
              github="project/7", author=AUTHOR, transition_phase="pool")
        assert anchor(world, "project/9", pid="pool-x").status_code == 201

    def test_a_request_repo_or_phase_field_cannot_unlock_it(self, world):
        world.seed(phase="peer", github=None)
        before, seq0 = disk(world, PID), seq_state(world)
        r = anchor(world, ANCHOR, phase="fairy", repo=REPO, transition_phase="fairy")
        assert r.status_code == 422
        assert disk(world, PID) == before and seq_state(world) == seq0


# ----------------------------------------------------------------------- R-b --

class TestAdvanceRequiresAnAnchor:
    def test_advance_without_anchor_is_422_names_it_zero_gh_zero_seq(self, world):
        world.seed(github=None)
        before, seq0 = disk(world, PID), seq_state(world)
        resp = world.advance()
        assert resp.status_code == 422, resp.get_json()
        assert "no github anchor" in resp.get_json()["error"].lower()
        assert world.gh.calls == []
        assert world.fields()[0]["phase"] == "peer"
        assert disk(world, PID) == before and seq_state(world) == seq0

    @pytest.mark.parametrize("phase", ["peer", None, "fairy"])
    def test_flip_to_the_merge_seat_without_anchor_is_refused_too(self, world, phase):
        # a colleague-held PR baton flipped to the merge seat with no anchor: the
        # weak path used to let this through (nothing to gate on) and hand the
        # seat a baton with no PR behind it.
        world.seed(phase=phase, github=None, turn=COLLEAGUE)
        before, seq0 = disk(world, PID), seq_state(world)
        resp = world.flip()
        assert resp.status_code == 422, resp.get_json()
        assert "no github anchor" in resp.get_json()["error"].lower()
        assert world.gh.calls == []
        assert disk(world, PID) == before and seq_state(world) == seq0

    def test_unanchored_flip_does_not_start_needing_the_pool_sentinel(self, world, monkeypatch):
        # An unanchored baton was never gated and never resolved the sentinel; an
        # unresolvable sentinel must not turn its ordinary review-stage flip into a 422.
        world.seed(phase="fairy", github=None, turn=AUTHOR)
        make_unresolvable(world, monkeypatch)
        resp = world.flip(agent=AUTHOR, to=COLLEAGUE, reason="please review")
        assert resp.status_code == 201, resp.get_json()
        assert world.gh.calls == []

    def test_flip_without_anchor_on_a_baton_with_no_colleague_is_unchanged(self, world):
        # single-agent mode: no peer phase exists, nothing to gate on -- not touched.
        world.seed(phase="fairy", github=None, colleague=None, turn=AUTHOR,
                   participants=(AUTHOR, SEAT))
        resp = world.flip(agent=AUTHOR)
        assert resp.status_code == 201, resp.get_json()


# ------------------------------------------------------------------------ D1 --

class TestGateDerivesTheRepo:
    def _repos_called(self, world):
        return {tok for c in world.gh.calls for tok in c if "/" in tok and not tok.startswith("-")}

    def test_unqualified_anchor_advances_with_the_admin_map_repo_and_no_write(self, world):
        world.seed(github="pr/9001")
        anchor_before = world.fields()[0]["github"]
        resp = world.advance(client=None)
        assert resp.status_code == 201, resp.get_json()
        assert world.gh.calls, "the gate must reach gh once the repo is derived"
        blob = " ".join(" ".join(c) for c in world.gh.calls)
        assert REPO in blob
        assert OTHER_REPO not in blob
        assert world.fields()[0]["github"] == anchor_before == "pr/9001"  # derived, never written

    def test_repo_comes_from_the_map_not_the_request_body(self, world):
        world.seed(github="pr/9001")
        resp = world.client.post(f"/api/projects/{PID}/advance", json={
            "agent": COLLEAGUE, "to_agent": SEAT, "reason": "r",
            "repo": OTHER_REPO, "github": f"pr/{OTHER_REPO}/9001", "anchor": f"pr/{OTHER_REPO}/9001"})
        assert resp.status_code == 201, resp.get_json()
        blob = " ".join(" ".join(c) for c in world.gh.calls)
        assert REPO in blob and OTHER_REPO not in blob

    def test_flip_spelling_derives_too(self, world):
        world.seed(github="pr/9001")
        resp = world.flip()
        assert resp.status_code == 201, resp.get_json()
        blob = " ".join(" ".join(c) for c in world.gh.calls)
        assert REPO in blob and OTHER_REPO not in blob
        assert world.fields()[0]["phase"] == "merge"

    def test_number_mismatch_is_422_before_any_gh_call(self, world):
        world.seed(github="pr/5")
        before, seq0 = disk(world, PID), seq_state(world)
        resp = world.advance()
        assert resp.status_code == 422, resp.get_json()
        assert "does not match its id" in resp.get_json()["error"]
        assert world.gh.calls == []
        assert disk(world, PID) == before and seq_state(world) == seq0

    def test_unmapped_prefix_is_422_before_any_gh_call(self, world):
        world.seed("ZZ-9001", github="pr/9001")
        resp = world.advance("ZZ-9001")
        assert resp.status_code == 422, resp.get_json()
        assert world.gh.calls == []
        assert world.fields("ZZ-9001")[0]["phase"] == "peer"

    def test_no_map_configured_is_422_before_any_gh_call(self, world_nomap):
        world_nomap.seed(github="pr/9001")
        resp = world_nomap.advance()
        assert resp.status_code == 422
        assert world_nomap.gh.calls == []

    def test_qualified_anchor_still_checked_against_the_map(self, world):
        world.seed(github=f"pr/{OTHER_REPO}/9001")
        assert world.advance().status_code == 422
        assert world.gh.calls == []


# ------------------------------------------------------------------------ D2 --

def init_post(world, pid, github, **extra):
    body = {"agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
            "turn": AUTHOR, "turn_reason": "start", "participants": [AUTHOR, COLLEAGUE]}
    if github is not None:
        body["github"] = github
    body.update(extra)
    return world.client.post("/api/projects", json=body)


class TestInitQualifiesOrRefuses:
    def test_unqualified_anchor_is_qualified_from_the_map_by_prefix(self, world):
        r = init_post(world, "PR-9201", "pr/9201")
        assert r.status_code == 201, r.get_json()
        f, _ = world.fields("PR-9201")
        assert f["github"].strip() == f"pr/{REPO}/9201"
        assert f["phase"].strip() == "fairy"

    def test_the_repo_never_comes_from_the_request(self, world):
        r = init_post(world, "PR-9202", "pr/9202", repo=OTHER_REPO, owner="x")
        assert r.status_code == 201, r.get_json()
        assert world.fields("PR-9202")[0]["github"].strip() == f"pr/{REPO}/9202"

    def test_number_that_differs_from_the_id_is_422_nothing_written(self, world):
        seq0 = seq_state(world)
        r = init_post(world, "PR-9203", "pr/5")
        assert r.status_code == 422, r.get_json()
        assert world.store.read_project("PR-9203") is None and seq_state(world) == seq0

    def test_unmapped_prefix_is_422(self, world):
        r = init_post(world, "ZZ-9204", "pr/9204")
        assert r.status_code == 422, r.get_json()
        assert world.store.read_project("ZZ-9204") is None

    def test_no_map_configured_is_422_only_for_the_unqualified_shape(self, world_nomap):
        r = init_post(world_nomap, "PR-9205", "pr/9205")
        assert r.status_code == 422, r.get_json()
        assert world_nomap.store.read_project("PR-9205") is None
        ok = init_post(world_nomap, "PR-9206", f"pr/{REPO}/9206")
        assert ok.status_code == 201, ok.get_json()

    def test_qualified_anchor_for_another_repo_is_refused_at_init(self, world):
        # r9: was "accepted as today (the binding is checked at advance)"; the
        # binding is now checked at the write, so another repo is a 422, no file.
        seq0 = seq_state(world)
        r = init_post(world, "PR-9207", f"pr/{OTHER_REPO}/9207")
        assert r.status_code == 422, r.get_json()
        assert world.store.read_project("PR-9207") is None and seq_state(world) == seq0
        ok = init_post(world, "PR-9207", f"pr/{REPO}/9207")
        assert ok.status_code == 201, ok.get_json()
        assert world.fields("PR-9207")[0]["github"].strip() == f"pr/{REPO}/9207"

    def test_project_anchor_and_bunches_untouched(self, world):
        r = init_post(world, "pool-d2", "project/7", transition_phase="pool")
        assert r.status_code == 201, r.get_json()
        assert world.fields("pool-d2")[0]["github"].strip() == "project/7"
        # #3366: no anchor at all is refused for a PR-shaped id, allowed for a non-PR id.
        assert init_post(world, "PR-9208", None).status_code == 422
        assert world.store.read_project("PR-9208") is None
        assert init_post(world, "DESIGN-9208", None).status_code == 201

    def test_without_the_gate_module_an_unqualified_anchor_fails_closed(self, world):
        saved = list(server_mod._BATON_GATE_MODULE_CACHE)
        server_mod._BATON_GATE_MODULE_CACHE[:] = [None]
        try:
            r = init_post(world, "PR-9209", "pr/9209", transition_phase="fairy")
            assert r.status_code == 422, r.get_json()
            assert world.store.read_project("PR-9209") is None
            ok = init_post(world, "PR-9210", f"pr/{REPO}/9210", transition_phase="fairy")
            assert ok.status_code == 201, ok.get_json()
        finally:
            server_mod._BATON_GATE_MODULE_CACHE[:] = saved


# ------------------------------------------------------------------------ S5 --

class TestHugeAnchorNumber:
    BIG = "9" * 5000

    def test_init_refuses_an_over_cap_anchor_number(self, world):
        seq0 = seq_state(world)
        for g in (f"pr/{self.BIG}", f"pr/{REPO}/{self.BIG}", f"project/{self.BIG}"):
            r = init_post(world, "PR-9301", g)
            assert r.status_code == 422, (g[:20], r.get_json())
        assert world.store.read_project("PR-9301") is None and seq_state(world) == seq0

    @pytest.mark.parametrize("phase", ["peer", "fairy"])
    def test_a_baton_carrying_one_never_500s_on_advance_or_anchor(self, world, phase):
        # seeded straight through the writer (as a pre-fix init could have)
        world.seed(phase=phase, github=f"pr/{self.BIG}")
        probes = [world.advance(), anchor(world, f"pr/{self.BIG}")]
        if phase == "peer":  # (in fairy a valid anchor is legitimately settable -> 201)
            probes.append(anchor(world, ANCHOR))
        for resp in probes:
            assert 400 <= resp.status_code < 500, (phase, resp.status_code, resp.get_data()[:200])
        assert world.gh.calls == []


# ---------------------------------------------------------------------- docs --

def test_docs_state_no_exceptions_derivation_and_init_qualification():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    flat = " ".join((root / "docs" / "baton-protocol.md").read_text().split())
    assert "no exceptions" in flat.lower()
    assert "derived from the id prefix" in flat.lower()
    assert "init qualifies" in flat.lower()
