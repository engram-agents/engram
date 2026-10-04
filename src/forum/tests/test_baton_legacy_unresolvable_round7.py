"""Contract tests: #3039 U2b round 7 -- a legacy baton never reads ``merge`` via an
unresolvable pool sentinel.

Written BEFORE the fix (contract-first).

  F1  PR-shaped baton + colleague + NO anchor + BLANK phase (legacy file), pool
      sentinel unresolvable: a flip is a 422 naming the sentinel; file bytes and
      seq unchanged; the baton can never subsequently read ``merge`` by a later
      request (the 4-step sequence ends with step 2 refused, step 4 not stamping).
  F2  a route-created baton (explicit ``phase: fairy``) stays ``fairy`` and its
      flip is NOT refused merely because the sentinel is unresolvable.
  F3  a colleague-held ``fairy`` PR baton with NO anchor flipped to the merge seat
      is a 422 naming the missing anchor, bytes and seq unchanged.
  F4  batons with no colleague behave as before.

Identities are SYNTHETIC.
"""

import sys

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import (  # noqa: E402
    World, AUTHOR, COLLEAGUE, SEAT, SENTINEL, REPO,
)
from test_baton_anchor_binding import disk, seq_state  # noqa: E402

LEGACY_PID = "PR-9400"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def make_unresolvable(world, monkeypatch):
    def boom(*a, **kw):
        raise world.gate.PoolSentinelUnresolved("no sentinel in this test")
    monkeypatch.setattr(world.gate, "_resolve_pool_sentinel", boom)


def make_resolvable(world, monkeypatch):
    monkeypatch.setattr(world.gate, "_resolve_pool_sentinel", lambda *a, **kw: SENTINEL)
    world.gate._POOL_SENTINEL_CACHE.clear()


def seed_legacy(world, **kw):
    world.seed(LEGACY_PID, phase=None, github=None, turn=AUTHOR, **kw)
    assert "phase:" not in world.store.read_project(LEGACY_PID), "seed must be phase-less"


def test_F1_flip_refused_422_names_sentinel_and_changes_nothing(world, monkeypatch):
    seed_legacy(world)
    before, seq0 = disk(world, LEGACY_PID), seq_state(world)
    make_unresolvable(world, monkeypatch)
    resp = world.flip(LEGACY_PID, agent=AUTHOR, to=SENTINEL, reason="legacy flip")
    assert resp.status_code == 422, resp.get_json()
    assert "sentinel" in resp.get_json()["error"].lower()
    assert disk(world, LEGACY_PID) == before and seq_state(world) == seq0


def test_F1_four_step_sequence_never_stamps_merge(world, monkeypatch):
    seed_legacy(world)
    make_unresolvable(world, monkeypatch)
    r2 = world.flip(LEGACY_PID, agent=AUTHOR, to=SENTINEL, reason="legacy flip")
    assert r2.status_code == 422, r2.get_json()
    make_resolvable(world, monkeypatch)
    r4 = world.client.post(f"/api/projects/{LEGACY_PID}/rename",
                           json={"agent": AUTHOR, "new_title": "t2"})
    assert "phase: merge" not in world.store.read_project(LEGACY_PID), r4.get_json()
    assert world.fields(LEGACY_PID)[0].get("phase") != "merge"


def test_F2_route_created_baton_stays_fairy_and_flip_not_refused(world, monkeypatch):
    r = world.client.post("/api/projects", json={
        "project_id": "PR-9401", "title": "t", "agent": AUTHOR, "status": "in-progress",
        "turn": AUTHOR, "participants": [AUTHOR, COLLEAGUE, SEAT],
        "colleague": COLLEAGUE, "turn_reason": "start",
        "github": f"pr/{REPO}/9401"})  # #3366: a PR-shaped init must name its PR
    assert r.status_code == 201, r.get_json()
    assert world.fields("PR-9401")[0]["phase"] == "fairy"
    # #3366: the original "unanchored flip to the sentinel with an unresolvable
    # sentinel is not refused" half is no longer reachable through init (a PR-shaped
    # init must carry an anchor, and an anchored flip legitimately needs the
    # sentinel resolved). That behaviour stays pinned on a seeded unanchored fairy
    # baton by test_F2_explicit_fairy_unanchored_flip_does_not_need_the_sentinel.
    make_unresolvable(world, monkeypatch)
    make_resolvable(world, monkeypatch)
    world.client.post("/api/projects/PR-9401/rename", json={"agent": AUTHOR, "new_title": "t2"})
    assert world.fields("PR-9401")[0]["phase"] == "fairy"


def test_F2_explicit_fairy_unanchored_flip_does_not_need_the_sentinel(world, monkeypatch):
    world.seed(LEGACY_PID, phase="fairy", github=None, turn=AUTHOR)
    make_unresolvable(world, monkeypatch)
    resp = world.flip(LEGACY_PID, agent=AUTHOR, to=COLLEAGUE, reason="please review")
    assert resp.status_code == 201, resp.get_json()
    # #3366: the flip-to-the-pool-SENTINEL half (formerly pinned on an init-created
    # anchorless baton, unreachable through init now) lives here on the seeded one.
    resp = world.flip(LEGACY_PID, agent=COLLEAGUE, to=SENTINEL, reason="x")
    assert resp.status_code == 201, resp.get_json()
    make_resolvable(world, monkeypatch)
    world.client.post(f"/api/projects/{LEGACY_PID}/rename", json={"agent": AUTHOR, "new_title": "t2"})
    assert world.fields(LEGACY_PID)[0]["phase"] == "fairy"


def test_F3_fairy_colleague_held_unanchored_flip_to_merge_seat_refused(world):
    world.seed(phase="fairy", github=None, turn=COLLEAGUE)
    before, seq0 = disk(world, "PR-9001"), seq_state(world)
    resp = world.flip()
    assert resp.status_code == 422, resp.get_json()
    assert "no github anchor" in resp.get_json()["error"].lower()
    assert disk(world, "PR-9001") == before and seq_state(world) == seq0


def test_F4_no_colleague_legacy_baton_unchanged(world, monkeypatch):
    seed_legacy(world, colleague=None, participants=(AUTHOR, SEAT))
    make_unresolvable(world, monkeypatch)
    resp = world.flip(LEGACY_PID, agent=AUTHOR, to=SENTINEL, reason="solo")
    assert resp.status_code == 201, resp.get_json()
