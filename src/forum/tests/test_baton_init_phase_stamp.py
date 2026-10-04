"""#3039 U2b round 5 (B1): a client-initiated init NEVER produces a phase-less baton.

Reproduced blocker: POST /api/projects with no ``transition_phase`` created a
baton with no ``phase:`` line; ``gate._resolve_phase`` reads a phase-less baton
whose ``turn`` is the pool sentinel as the LEGACY ``merge`` (a rule meant only
for batons that predate phase stamping), so ``advance`` answered 200 "already
merge" with zero gh calls and the next write stamped ``phase: merge`` with no
approval anywhere.

Contract (stated by the spec, asserted here before the implementation):
  * the SERVER stamps the initial phase on every init, whether or not the client
    sent ``transition_phase`` (``fairy`` for PR-shaped ids by the code's existing
    anchor-first/name rule, ``pool`` for bunches);
  * a client ``transition_phase`` outside the existing allow-list is still 422;
  * no route (init, then rename / anchor / colleague / participants / status /
    gc / flip-to-the-seat / advance) can make such a baton read ``merge``;
  * a flip on a baton with no ``pr/`` anchor never leaves a PR baton phase-less
    (so flipping it to the sentinel cannot make it read ``merge``);
  * legacy phase-less files already on disk keep working (legacy rule intact).

Identities are SYNTHETIC.
"""

import sys

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import World, SENTINEL, AUTHOR, COLLEAGUE, SEAT, OTHER, ANCHOR, REPO  # noqa: E402

from forum.coordination.markdown import parse_frontmatter  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _raw(world, pid):
    return world.store.read_project(pid)


def _has_phase_line(raw: str) -> bool:
    head = raw.split("---", 2)[1]
    return any(ln.startswith("phase:") and ln[len("phase:"):].strip() for ln in head.splitlines())


def _reads_merge(world, pid) -> bool:
    raw = _raw(world, pid)
    if raw is None:
        return False
    fields, _ = parse_frontmatter(raw)
    if (fields.get("phase") or "").strip().lower() == "merge":
        return True
    phase, _legacy = world.gate._resolve_phase(fields, is_bunch=False)
    return phase == "merge"


def _init_body(pid, turn, *, anchor, tp):
    body = {
        "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
        "turn": turn, "turn_reason": "r",
        "participants": [AUTHOR, COLLEAGUE, SENTINEL],
        "colleague": COLLEAGUE, "author": AUTHOR,
    }
    if anchor:
        body["github"] = anchor
    if tp != "ABSENT":
        body["transition_phase"] = tp
    return body


def test_exact_repro_phase_less_init_is_stamped_and_cannot_advance(world):
    """The reviewer's repro, verbatim shape."""
    r = world.client.post("/api/projects", json={
        "agent": AUTHOR, "project_id": "PR-9100", "title": "t", "status": "in-progress",
        "turn": SENTINEL, "turn_reason": "r", "participants": [AUTHOR, COLLEAGUE, SENTINEL],
        "github": "pr/example-org/example-repo/9100", "colleague": COLLEAGUE, "author": AUTHOR,
    })
    assert r.status_code == 201
    assert _has_phase_line(_raw(world, "PR-9100")), "init left a phase-less baton"
    assert world.fields("PR-9100")[0]["phase"] == "fairy"
    a = world.client.post("/api/projects/PR-9100/advance",
                          json={"agent": AUTHOR, "to_agent": SEAT, "reason": "x"})
    assert a.status_code in (409, 422), a.get_json()
    assert not _reads_merge(world, "PR-9100")
    rn = world.client.post("/api/projects/PR-9100/rename", json={"agent": AUTHOR, "new_title": "t2"})
    assert rn.status_code == 201
    assert not _reads_merge(world, "PR-9100")
    assert world.fields("PR-9100")[0]["phase"] == "fairy"


TURNS = ["sentinel", "holder", "colleague"]
TPS = ["ABSENT", "fairy", "peer", "merge", "bogus"]
# r9: the anchor must name the id's number (PR-9100), the init id of every cell below.
ANCHORS = [f"pr/{REPO}/9100", None]
ROUTES = ["rename", "anchor", "colleague", "participants", "status", "gc", "flip_seat", "advance"]


def _turn(name):
    return {"sentinel": SENTINEL, "holder": AUTHOR, "colleague": COLLEAGUE}[name]


def _route(world, pid, route):
    c = world.client
    p = f"/api/projects/{pid}"
    if route == "rename":
        return c.post(f"{p}/rename", json={"agent": AUTHOR, "new_title": "t2"})
    if route == "anchor":
        return c.post(f"{p}/anchor", json={"agent": AUTHOR, "github": f"pr/{REPO}/9100"})
    if route == "colleague":
        return c.post(f"{p}/colleague", json={"agent": AUTHOR, "colleague": COLLEAGUE})
    if route == "participants":
        return c.post(f"{p}/participants", json={"agent": AUTHOR, "participant": OTHER})
    if route == "status":
        return c.post(f"{p}/status", json={"agent": AUTHOR, "new_status": "in-progress", "reason": "r"})
    if route == "gc":
        return c.post(f"{p}/gc", json={"agent": AUTHOR, "new_status": "cancelled", "reason": "r"})
    if route == "flip_seat":
        return c.post(f"{p}/flip", json={"agent": AUTHOR, "to_agent": SEAT, "reason": "r"})
    if route == "advance":
        return c.post(f"{p}/advance", json={"agent": AUTHOR, "to_agent": SEAT, "reason": "r"})
    raise AssertionError(route)


@pytest.mark.parametrize("anchor", ANCHORS)
@pytest.mark.parametrize("tp", TPS)
@pytest.mark.parametrize("turn", TURNS)
def test_init_never_leaves_a_phase_less_file(world, turn, tp, anchor):
    r = world.client.post("/api/projects", json=_init_body("PR-9100", _turn(turn), anchor=anchor, tp=tp))
    if anchor is None:
        # #3366: a PR-shaped id with no anchor is refused outright, whatever
        # transition_phase the client sent -- nothing is created at all.
        assert r.status_code == 422, r.get_json()
        assert _raw(world, "PR-9100") is None
    elif tp in ("ABSENT", "fairy"):
        assert r.status_code == 201, r.get_json()
        assert _has_phase_line(_raw(world, "PR-9100")), "phase-less baton created by init"
        assert world.fields("PR-9100")[0]["phase"] == "fairy"
    else:
        assert r.status_code == 422, r.get_json()  # allow-list kept
        assert _raw(world, "PR-9100") is None


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("anchor", [a for a in ANCHORS if a is not None])
@pytest.mark.parametrize("tp", ["ABSENT", "fairy"])
@pytest.mark.parametrize("turn", TURNS)
def test_no_route_makes_an_initted_baton_read_merge(world, turn, tp, anchor, route):
    r = world.client.post("/api/projects", json=_init_body("PR-9100", _turn(turn), anchor=anchor, tp=tp))
    assert r.status_code == 201, r.get_json()
    _route(world, "PR-9100", route)
    assert not _reads_merge(world, "PR-9100"), f"{route} made the baton read merge"
    raw = _raw(world, "PR-9100")
    if raw is not None:  # gc may archive
        assert _has_phase_line(raw)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("turn", TURNS)
def test_no_route_makes_a_seeded_anchorless_fairy_baton_read_merge(world, turn, route):
    """#3366: init now refuses an anchorless PR-shaped id, so the anchorless-fairy
    state (42 such batons exist on disk) is SEEDED; the 3 turns x 8 routes matrix
    that init used to feed is kept."""
    world.seed("PR-9100", phase="fairy", github=None, turn=_turn(turn), colleague=COLLEAGUE,
               participants=(AUTHOR, COLLEAGUE, SENTINEL, OTHER, SEAT))
    _route(world, "PR-9100", route)
    assert not _reads_merge(world, "PR-9100"), f"{route} made the baton read merge"
    raw = _raw(world, "PR-9100")
    if raw is not None:  # gc may archive
        assert _has_phase_line(raw)


@pytest.mark.parametrize("tp", ["peer", "merge", "bogus"])
@pytest.mark.parametrize("route", ROUTES)
def test_refused_init_leaves_nothing_to_convert(world, tp, route):
    r = world.client.post("/api/projects", json=_init_body("PR-9100", SENTINEL, anchor=ANCHOR, tp=tp))
    assert r.status_code == 422
    _route(world, "PR-9100", route)
    assert _raw(world, "PR-9100") is None


def test_bunch_init_is_stamped_pool(world):
    r = world.client.post("/api/projects", json={
        "agent": AUTHOR, "project_id": "pool-x", "title": "t", "status": "in-progress",
        "turn": SENTINEL, "turn_reason": "r", "participants": [AUTHOR, SENTINEL]})
    assert r.status_code == 201
    assert world.fields("pool-x")[0]["phase"] == "pool"
    r = world.client.post("/api/projects", json={
        "agent": AUTHOR, "project_id": "PR-9101", "title": "t", "status": "in-progress",
        "turn": SENTINEL, "turn_reason": "r", "participants": [AUTHOR, SENTINEL],
        "github": "project/4"})
    # round 10: classification follows the id shape; a mapped-prefix numbered id
    # takes only a pr/ anchor, so a project/<N> anchor is refused at init.
    assert r.status_code == 422
    assert _raw(world, "PR-9101") is None


# ---------------------------------------------------------------------------
# Legacy phase-less files already on disk keep working; and a flip cannot
# manufacture `merge` on a baton with no pr/ anchor.
# ---------------------------------------------------------------------------

def test_legacy_phase_less_sentinel_baton_still_resolves_merge(world):
    world.seed("PR-9200", phase=None, turn=SENTINEL, github=ANCHOR, colleague=None)
    assert not (world.fields("PR-9200")[0].get("phase") or "").strip()
    assert _reads_merge(world, "PR-9200")  # legacy rule intact for old files
    rn = world.client.post("/api/projects/PR-9200/rename", json={"agent": AUTHOR, "new_title": "t2"})
    assert rn.status_code == 201
    assert world.fields("PR-9200")[0]["phase"] == "merge"  # legacy conversion unchanged


@pytest.mark.parametrize("colleague", [COLLEAGUE, None])
@pytest.mark.parametrize("github", [None, "project/4"])
def test_flip_to_sentinel_on_non_pr_anchor_never_leaves_pr_baton_phase_less(world, colleague, github):
    """Legacy phase-less PR baton, holder != sentinel, no pr/ anchor. Flipping it
    to the sentinel would make the turn the pool sentinel on a phase-less file =>
    legacy merge. The weak flip must stamp an explicit non-merge phase."""
    pid = "PR-9300"
    world.seed(pid, phase=None, turn=AUTHOR, github=github, colleague=colleague)
    before, seq0 = _raw(world, pid), world.allocator.current()
    r = world.client.post(f"/api/projects/{pid}/flip",
                          json={"agent": AUTHOR, "to_agent": SENTINEL, "reason": "r"})
    if github is None and colleague:
        # Round 6 (R-b): a colleague-held PR baton flipped to the merge seat with NO
        # anchor is refused outright (422 naming the missing anchor), so the baton
        # can never be left phase-less at the sentinel; nothing is written.
        assert r.status_code == 422, r.get_json()
        assert "no github anchor" in r.get_json()["error"].lower()
        assert not _reads_merge(world, pid)
        assert _raw(world, pid) == before and world.allocator.current() == seq0
        return
    assert r.status_code == 201, r.get_json()
    f = world.fields(pid)[0]
    if github is None:  # PR-shaped by id; a project/ anchor makes it a bunch (reads "pool")
        assert (f.get("phase") or "").strip(), "flip left the baton phase-less"
        assert not _reads_merge(world, pid)
    a = world.advance(pid, to=SEAT, agent=AUTHOR)
    assert not (a.status_code == 200 and a.get_json().get("reason") == "already merge")
