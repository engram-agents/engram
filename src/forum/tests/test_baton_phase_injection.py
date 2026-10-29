"""Contract tests: no request body can make a baton read `phase: merge`.

#3039 U2b round 2 (security fix). The forum server used to pass a client-supplied
`phase` (conversion) and `transition_phase` to the coordination write-bodies
verbatim, so with NO approval anywhere:

  1. `POST .../release` or `.../claim` carrying `transition_phase: "merge"` wrote
     `phase: merge` on a peer/fairy/legacy baton (a full bypass of the strict
     peer->merge gate), and
  2. `POST .../colleague|anchor|participants` carrying `phase: "merge"` stamped
     `phase: merge` on a LEGACY baton (stamp-if-absent "conversion").

The contract these tests pin (written BEFORE the fix):

  C1. The server never trusts a client `phase`. It derives the conversion value
      itself from the baton file (`gate._resolve_phase`, correct is_bunch). A
      differing client value is ignored; a client `merge` that differs from the
      server-resolved value is a 422 naming the route.
  C2. `transition_phase` is an allow-list per route (what the CLI legitimately
      sends). `merge` is accepted from no request body except through the
      strict-gate path. Out-of-list => 422, nothing written, no seq consumed.
  C3. claim and release are BUNCH verbs: on a PR baton they are a 422.

Identities are SYNTHETIC; no real fleet name appears here.
"""

import sys
from pathlib import Path

import pytest

from forum import server as server_mod
from forum.coordination import init as _init
from forum.coordination.markdown import parse_frontmatter
from forum.tests.test_baton_advance_api import (
    ANCHOR,
    AUTHOR,
    COLLEAGUE,
    OTHER,
    REPO,
    SEAT,
    SENTINEL,
    World,
    rv,
)

_TOOLS_DIR = Path(__file__).resolve().parents[3] / "tools"
# append (not insert(0)): tools/ also holds forum.py, which would shadow the
# `forum` package if it came first (same rationale as tests/test_baton_gate.py).
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))


@pytest.fixture(autouse=True)
def _gh_says_merged(monkeypatch):
    """#3416: /gc and /merge on an anchored baton read GitHub server-side. This
    file tests phase handling, not PR state; pin the read so it never shells out."""
    from forum import board_projects
    # WARNING: pins "merged" for EVERY test here, whatever the client asks for. A
    # future test that closes an anchored baton as ``cancelled`` would silently get
    # ``merged``; give that test its own stub.
    monkeypatch.setattr(board_projects, "_gh_pr_state", lambda n, r: "merged")


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.gh.reviews = []  # NO approval anywhere
    return w


PR = "PR-9001"
BUNCH = "pool-bunch"
NEWBIE = "example-agent-new"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def read_any(world, pid):
    """File text whether the baton is active or archived (merge archives)."""
    raw = world.store.read_project(pid)
    if raw is None:
        p = Path(world.store.root) / "archive" / f"{pid}.md"
        raw = p.read_text() if p.exists() else None
    return raw


def phase_of(world, pid):
    raw = read_any(world, pid)
    if raw is None:
        return None
    return (parse_frontmatter(raw)[0].get("phase") or "").strip() or None


def snapshot(world, pid):
    return (read_any(world, pid), world.allocator.current())


def seed_pr(world, phase, *, turn=COLLEAGUE, **kw):
    world.seed(PR, phase=phase, turn=turn, **kw)


def seed_bunch(world, phase, *, turn=OTHER):
    _init(world.store, world.allocator, BUNCH, title="bunch", status="in-progress",
          turn=turn, participants=[AUTHOR, COLLEAGUE, OTHER], turn_reason="start",
          github="project/7", author=AUTHOR, transition_phase=phase)


SEEDS = {
    "peer": lambda w: seed_pr(w, "peer"),
    "fairy": lambda w: seed_pr(w, "fairy", turn=AUTHOR),
    "legacy": lambda w: seed_pr(w, None),
    "merge": lambda w: seed_pr(w, "merge"),
    "pool": lambda w: seed_bunch(w, "pool", turn=SENTINEL),
    "claimed": lambda w: seed_bunch(w, "claimed"),
}
SEED_PID = {"peer": PR, "fairy": PR, "legacy": PR, "merge": PR, "pool": BUNCH, "claimed": BUNCH}

VARIANTS = {
    "phase=merge": {"phase": "merge"},
    "transition_phase=merge": {"transition_phase": "merge"},
    "both=merge": {"phase": "merge", "transition_phase": "merge"},
    "phase=peer": {"phase": "peer"},
    "transition_phase=peer": {"transition_phase": "peer"},
    "bogus": {"phase": "bogus-string", "transition_phase": "bogus-string"},
}

# route name -> (url suffix, body) for an EXISTING baton. Every body is a valid
# request apart from the injected phase fields.
ROUTES = {
    "flip-to-seat": ("flip", {"agent": AUTHOR, "to_agent": SEAT, "reason": "ready"}),
    "flip-to-colleague": ("flip", {"agent": AUTHOR, "to_agent": COLLEAGUE, "reason": "please review"}),
    "claim": ("claim", {"agent": OTHER, "pool_sentinel": SENTINEL}),
    "release": ("release", {"agent": OTHER, "pool_sentinel": SENTINEL, "reason": "back"}),
    "release-done": ("release", {"agent": OTHER, "pool_sentinel": SENTINEL, "reason": "done", "done": True}),
    "close": ("status", {"agent": AUTHOR, "new_status": "cancelled", "reason": "drop"}),
    "reopen": ("status", {"agent": AUTHOR, "new_status": "in-progress", "reason": "again"}),
    "gc": ("gc", {"agent": AUTHOR, "new_status": "merged", "reason": "gc"}),
    "merge": ("merge", {"agent": AUTHOR}),
    "rename": ("rename", {"agent": AUTHOR, "new_title": "renamed"}),
    "anchor": ("anchor", {"agent": AUTHOR, "github": ANCHOR}),
    "participants": ("participants", {"agent": AUTHOR, "participant": NEWBIE}),
    "colleague": ("colleague", {"agent": AUTHOR, "colleague": OTHER}),
}


def post(world, pid, route, extra):
    suffix, body = ROUTES[route]
    return world.client.post(f"/api/projects/{pid}/{suffix}", json={**body, **extra})


# ---------------------------------------------------------------------------
# MATRIX: no route x seed x variant writes `phase: merge`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", list(VARIANTS))
@pytest.mark.parametrize("seed", list(SEEDS))
@pytest.mark.parametrize("route", list(ROUTES))
def test_matrix_no_request_body_writes_phase_merge(world, route, seed, variant):
    SEEDS[seed](world)
    pid = SEED_PID[seed]
    before_text, before_seq = snapshot(world, pid)
    before_phase = phase_of(world, pid)

    resp = post(world, pid, route, VARIANTS[variant])

    after_phase = phase_of(world, pid)
    # The invariant: merge is never newly written by a request body. (The fixture
    # has no approval and a legacy turn that is not the pool sentinel, so no
    # server-derived legacy conversion can yield merge either.)
    assert after_phase != "merge" or before_phase == "merge", (
        f"{route} on a {seed} baton with {variant} wrote phase: merge "
        f"(status {resp.status_code}: {resp.get_json()})"
    )
    if resp.status_code >= 400:
        after_text, after_seq = snapshot(world, pid)
        assert after_text == before_text, f"rejected {route}/{variant} still wrote the file"
        assert after_seq == before_seq, f"rejected {route}/{variant} consumed a seq"


@pytest.mark.parametrize("variant", list(VARIANTS))
@pytest.mark.parametrize("kind", ["pr-anchored", "bunch"])
def test_matrix_init_of_a_new_baton_never_writes_merge(world, kind, variant):
    pid, github = ("PR-9100", f"pr/{REPO}/9100") if kind == "pr-anchored" else ("pool-new", "project/8")
    before_seq = world.allocator.current()
    resp = world.client.post("/api/projects", json={
        "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
        "turn": AUTHOR, "turn_reason": "start", "participants": [AUTHOR, COLLEAGUE],
        "github": github, **VARIANTS[variant],
    })
    assert phase_of(world, pid) != "merge", resp.get_json()
    if resp.status_code >= 400:
        assert read_any(world, pid) is None
        assert world.allocator.current() == before_seq


def test_matrix_covers_every_route_that_reads_phase():
    """If a route starts reading `phase`/`transition_phase`, it must join ROUTES."""
    import inspect
    import re
    src = inspect.getsource(server_mod.create_app)
    routes_reading_phase = set()
    for m in re.finditer(r'@app\.route\("/api/projects(?:/<pid>/(\w+))?", methods=\["POST"\]\)(.*?)(?=\n    @app\.route|\n    return app)', src, re.S):
        name, text = m.group(1), m.group(2)
        if '"phase"' in text or '"transition_phase"' in text:
            routes_reading_phase.add(name or "init")
    covered = {suffix for suffix, _ in ROUTES.values()} | {"init"}
    assert routes_reading_phase <= covered, routes_reading_phase - covered
    assert "release" in routes_reading_phase  # the extraction itself works


# ---------------------------------------------------------------------------
# The two bypass repros, verbatim
# ---------------------------------------------------------------------------

class TestMeasuredBypasses:
    def test_release_with_transition_phase_merge_on_a_peer_baton(self, world):
        seed_pr(world, "peer")
        for done in (False, True):
            resp = world.client.post(f"/api/projects/{PR}/release", json={
                "agent": COLLEAGUE, "pool_sentinel": SENTINEL, "reason": "x",
                "done": done, "transition_phase": "merge"})
            assert resp.status_code == 422, resp.get_json()
        f, _ = world.fields(PR)
        assert f["phase"] == "peer" and f["turn"] == COLLEAGUE

    def test_claim_with_transition_phase_merge_on_a_peer_baton(self, world):
        seed_pr(world, "peer")
        resp = world.client.post(f"/api/projects/{PR}/claim", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "transition_phase": "merge"})
        assert resp.status_code == 422
        assert world.fields(PR)[0]["phase"] == "peer"

    @pytest.mark.parametrize("route", ["colleague", "anchor", "participants"])
    def test_legacy_baton_conversion_phase_merge_is_not_stamped(self, world, route):
        seed_pr(world, None)
        resp = post(world, PR, route, {"phase": "merge"})
        assert resp.status_code == 422, resp.get_json()
        assert not world.fields(PR)[0].get("phase")


# ---------------------------------------------------------------------------
# C3: claim / release are bunch verbs
# ---------------------------------------------------------------------------

class TestClaimReleaseAreBunchVerbs:
    @pytest.mark.parametrize("route", ["claim", "release", "release-done"])
    @pytest.mark.parametrize("seed", ["peer", "fairy", "legacy", "merge"])
    def test_pr_anchored_baton_is_refused_with_nothing_written(self, world, route, seed):
        SEEDS[seed](world)
        before = snapshot(world, PR)
        # CLI-shaped bodies (the legitimate bunch shape) are refused on a PR baton too.
        extra = {"claim": {"phase": "pool", "transition_phase": "claimed"}}.get(
            route, {"phase": "claimed", "transition_phase": "done" if route == "release-done" else "pool"})
        resp = post(world, PR, route, extra)
        assert resp.status_code == 422, resp.get_json()
        assert snapshot(world, PR) == before

    def test_unanchored_pr_named_baton_is_a_pr_baton(self, world):
        seed_pr(world, "peer", github="")
        assert post(world, PR, "claim", {}).status_code == 422

    def test_anchor_decides_over_the_name(self, world):
        # name looks like a PR, anchor says bunch => bunch verbs allowed.
        _init(world.store, world.allocator, "PR-looking", title="b", status="in-progress",
              turn=SENTINEL, participants=[AUTHOR, OTHER], turn_reason="s",
              github="project/3", transition_phase="pool")
        resp = world.client.post("/api/projects/PR-looking/claim", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "transition_phase": "claimed"})
        assert resp.status_code == 201, resp.get_json()

    def test_pool_prefixed_unanchored_name_is_always_a_bunch(self, world):
        _init(world.store, world.allocator, "pool-pr-followups", title="b",
              status="in-progress", turn=SENTINEL, participants=[AUTHOR, OTHER],
              turn_reason="s", transition_phase="pool")
        resp = world.client.post("/api/projects/pool-pr-followups/claim", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "transition_phase": "claimed"})
        assert resp.status_code == 201, resp.get_json()


# ---------------------------------------------------------------------------
# POSITIVE: every legitimate CLI shape still works, and stamps SERVER values
# ---------------------------------------------------------------------------

class TestLegitimateCliShapesStillWork:
    def test_flip_fairy_to_peer(self, world):
        seed_pr(world, "fairy", turn=AUTHOR)
        resp = world.client.post(f"/api/projects/{PR}/flip", json={
            "agent": AUTHOR, "to_agent": COLLEAGUE, "reason": "please review",
            "phase": "fairy", "transition_phase": "peer"})
        assert resp.status_code == 201, resp.get_json()
        f, _ = world.fields(PR)
        assert f["phase"] == "peer" and f["turn"] == COLLEAGUE

    def test_flip_fairy_to_peer_on_legacy_baton_stamps_the_server_fairy_value(self, world):
        seed_pr(world, None, turn=AUTHOR)
        resp = world.client.post(f"/api/projects/{PR}/flip", json={
            "agent": AUTHOR, "to_agent": COLLEAGUE, "reason": "please review",
            "phase": "peer", "transition_phase": "peer"})  # client conversion value is WRONG
        assert resp.status_code == 201, resp.get_json()
        assert world.fields(PR)[0]["phase"] == "peer"
        assert world.log_lines("phase stamped: 'fairy'", PR)
        assert not world.log_lines("phase stamped: 'peer'", PR)

    def test_plain_within_phase_flip_converts_a_legacy_baton_with_the_server_value(self, world):
        # legacy PR baton at the pool sentinel resolves "merge" SERVER-side: the
        # one legacy conversion that may yield merge. Client says something else.
        seed_pr(world, None, turn=SENTINEL)
        resp = world.client.post(f"/api/projects/{PR}/flip", json={
            "agent": SENTINEL, "to_agent": AUTHOR, "reason": "needs a fix", "phase": "peer"})
        assert resp.status_code == 201, resp.get_json()
        assert world.fields(PR)[0]["phase"] == "merge"
        assert world.log_lines("phase stamped: 'merge'", PR)

    def test_no_colleague_maintainer_flip_still_writes_merge_through_the_weak_gate(self, world):
        # No colleague => no peer phase; the unchanged weak gate (CI green) is the
        # judge and the CLI's transition_phase=merge is honoured because the SERVER
        # derives the same transition from the baton itself.
        world.seed(PR, colleague=None, participants=(AUTHOR,), turn=AUTHOR, phase="fairy")
        resp = world.client.post(f"/api/projects/{PR}/flip", json={
            "agent": AUTHOR, "to_agent": SENTINEL, "reason": "ready",
            "phase": "fairy", "transition_phase": "merge"})
        assert resp.status_code == 201, resp.get_json()
        assert world.fields(PR)[0]["phase"] == "merge"

    def test_no_colleague_merge_claim_on_a_non_maintainer_flip_is_refused(self, world):
        world.seed(PR, colleague=None, participants=(AUTHOR, OTHER), turn=AUTHOR, phase="fairy")
        resp = world.client.post(f"/api/projects/{PR}/flip", json={
            "agent": AUTHOR, "to_agent": OTHER, "reason": "hand over", "transition_phase": "merge"})
        assert resp.status_code == 422
        assert world.fields(PR)[0]["phase"] == "fairy"

    def test_claim_and_release_on_a_bunch(self, world):
        seed_bunch(world, "pool", turn=SENTINEL)
        r = world.client.post(f"/api/projects/{BUNCH}/claim", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "phase": "pool", "transition_phase": "claimed"})
        assert r.status_code == 201, r.get_json()
        assert world.fields(BUNCH)[0]["phase"] == "claimed"
        r = world.client.post(f"/api/projects/{BUNCH}/release", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "reason": "back",
            "phase": "claimed", "transition_phase": "pool"})
        assert r.status_code == 201, r.get_json()
        assert world.fields(BUNCH)[0]["phase"] == "pool"
        world.client.post(f"/api/projects/{BUNCH}/claim", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "transition_phase": "claimed"})
        r = world.client.post(f"/api/projects/{BUNCH}/release", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "reason": "done", "done": True,
            "phase": "claimed", "transition_phase": "done"})
        assert r.status_code == 201, r.get_json()
        assert world.fields(BUNCH)[0]["phase"] == "done"

    def test_claim_on_a_legacy_bunch_stamps_the_server_pool_value(self, world):
        seed_bunch(world, None, turn=SENTINEL)  # legacy bunch at the sentinel => "pool"
        r = world.client.post(f"/api/projects/{BUNCH}/claim", json={
            "agent": OTHER, "pool_sentinel": SENTINEL,
            "phase": "done", "transition_phase": "claimed"})  # client conversion value is WRONG
        assert r.status_code == 201, r.get_json()
        assert world.fields(BUNCH)[0]["phase"] == "claimed"
        assert world.log_lines("phase stamped: 'pool'", BUNCH)
        assert not world.log_lines("phase stamped: 'done'", BUNCH)

    def test_release_on_a_legacy_held_bunch_stamps_claimed(self, world):
        seed_bunch(world, None, turn=OTHER)  # legacy, held by a real agent
        r = world.client.post(f"/api/projects/{BUNCH}/release", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "reason": "back",
            "phase": "pool", "transition_phase": "pool"})
        assert r.status_code == 201, r.get_json()
        assert world.log_lines("phase stamped: 'claimed'", BUNCH)

    # #3296: `colleague` is no longer a row here -- a legacy PR baton at the sentinel
    # resolves `merge`, where the colleague is refused (see
    # test_baton_colleague_at_merge.py), exactly as `anchor` left these rows in R6.
    @pytest.mark.parametrize("route", ["close", "reopen", "gc", "merge", "rename",
                                       "participants"])
    def test_conversion_routes_stamp_the_server_value_for_a_legacy_pr_baton_at_the_sentinel(self, world, route):
        # server-resolved "merge" (legacy PR baton, turn == sentinel): allowed, and a
        # client value that differs is ignored in favour of the server's.
        seed_pr(world, None, turn=SENTINEL)
        resp = post(world, PR, route, {"phase": "peer"})
        assert resp.status_code in (200, 201), (route, resp.get_json())
        assert phase_of(world, PR) == "merge"

    @pytest.mark.parametrize("route", ["rename", "participants", "close"])
    def test_conversion_routes_accept_a_matching_client_merge_when_the_server_agrees(self, world, route):
        seed_pr(world, None, turn=SENTINEL)
        resp = post(world, PR, route, {"phase": "merge"})
        assert resp.status_code in (200, 201), (route, resp.get_json())
        assert phase_of(world, PR) == "merge"

    @pytest.mark.parametrize("client_phase", ["peer", "merge"])
    def test_anchor_route_on_a_legacy_pr_baton_at_the_sentinel_is_refused_not_stamped(self, world, client_phase):
        # R6 (was the `anchor` row of the two conversion tests above): a legacy
        # phase-less PR baton at the sentinel resolves `merge`, so its anchor is
        # write-once with NO exceptions -- the route refuses and stamps nothing.
        seed_pr(world, None, turn=SENTINEL)
        before = snapshot(world, PR)
        resp = post(world, PR, "anchor", {"phase": client_phase})
        assert resp.status_code == 422, resp.get_json()
        assert phase_of(world, PR) is None
        assert snapshot(world, PR) == before

    @pytest.mark.parametrize("route", ["rename", "anchor", "participants", "colleague", "close"])
    def test_conversion_routes_leave_a_legacy_non_sentinel_baton_unstamped_whatever_the_client_says(self, world, route):
        seed_pr(world, None)  # turn is a real agent => server resolves "" => no conversion
        resp = post(world, PR, route, {"phase": "peer"})
        assert resp.status_code in (200, 201), (route, resp.get_json())
        assert phase_of(world, PR) is None

    def test_conversion_on_a_legacy_bunch_stamps_pool_not_the_client_value(self, world):
        seed_bunch(world, None, turn=SENTINEL)
        resp = post(world, BUNCH, "rename", {"phase": "claimed"})
        assert resp.status_code == 201
        assert phase_of(world, BUNCH) == "pool"

    def test_conversion_never_overrides_an_explicit_phase(self, world):
        seed_pr(world, "peer")
        resp = post(world, PR, "rename", {"phase": "fairy"})
        assert resp.status_code == 201
        assert phase_of(world, PR) == "peer"

    def test_init_with_the_cli_initial_phases(self, world):
        for pid, github, tp in (("PR-9101", f"pr/{REPO}/9101", "fairy"), ("pool-two", "project/9", "pool")):
            resp = world.client.post("/api/projects", json={
                "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
                "turn": AUTHOR, "turn_reason": "start", "participants": [AUTHOR, COLLEAGUE],
                "github": github, "transition_phase": tp})
            assert resp.status_code == 201, resp.get_json()
            assert phase_of(world, pid) == tp

    def test_init_without_phase_fields_stamps_fairy(self, world):
        # Round 5 (B1) made this `fairy` on purpose: no new baton is ever phase-less
        # (a phase-less PR baton at the pool sentinel reads as the legacy `merge`).
        resp = world.client.post("/api/projects", json={
            "agent": AUTHOR, "project_id": "PR-9102", "title": "t", "status": "in-progress",
            "turn": AUTHOR, "turn_reason": "start", "participants": [AUTHOR],
            "github": f"pr/{REPO}/9102"})  # #3366: a PR-shaped init must name its PR
        assert resp.status_code == 201
        assert phase_of(world, "PR-9102") == "fairy"

    def test_init_ignores_a_client_conversion_phase(self, world):
        resp = world.client.post("/api/projects", json={
            "agent": AUTHOR, "project_id": "PR-9103", "title": "t", "status": "in-progress",
            "turn": AUTHOR, "turn_reason": "start", "participants": [AUTHOR],
            "github": f"pr/{REPO}/9103", "phase": "peer", "transition_phase": "fairy"})
        assert resp.status_code == 201
        assert phase_of(world, "PR-9103") == "fairy"

    @pytest.mark.parametrize("pid, github, bad", [
        ("PR-9104", f"pr/{REPO}/9104", "pool"),      # bunch value on a PR baton
        ("pool-three", "project/10", "fairy"),       # PR value on a bunch
        ("PR-9105", f"pr/{REPO}/9105", "peer"),      # a PR baton does not START at peer
        ("PR-9106", f"pr/{REPO}/9106", "merge"),
    ])
    def test_init_out_of_allow_list_is_a_422_writing_nothing(self, world, pid, github, bad):
        before = world.allocator.current()
        resp = world.client.post("/api/projects", json={
            "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
            "turn": AUTHOR, "turn_reason": "start", "participants": [AUTHOR],
            "github": github, "transition_phase": bad})
        assert resp.status_code == 422
        assert read_any(world, pid) is None and world.allocator.current() == before

    def test_the_refusal_names_the_route_and_the_value(self, world):
        seed_bunch(world, "claimed")
        resp = world.client.post(f"/api/projects/{BUNCH}/release", json={
            "agent": OTHER, "pool_sentinel": SENTINEL, "reason": "x", "transition_phase": "merge"})
        assert resp.status_code == 422
        msg = resp.get_json()["error"]
        assert "release" in msg and "merge" in msg

    def test_nonexistent_baton_is_still_a_404(self, world):
        for route in ("rename", "release", "claim"):
            resp = post(world, "PR-0000", route, {"phase": "merge"})
            assert resp.status_code in (404,), (route, resp.status_code)

    def test_gate_module_unavailable_never_trusts_a_client_merge(self, world, monkeypatch):
        seed_pr(world, None)
        monkeypatch.setattr(server_mod, "_BATON_GATE_MODULE_CACHE", [None])
        assert post(world, PR, "rename", {"phase": "merge"}).status_code == 422
        assert not world.fields(PR)[0].get("phase")
        assert post(world, PR, "rename", {}).status_code == 201


# ---------------------------------------------------------------------------
# The server's PR-vs-bunch decision is the CLI's (parity)
# ---------------------------------------------------------------------------

class TestPrVsBunchParityWithTheCli:
    CASES = [
        ("PR-1", ""), ("pr-1", ""), ("UMWELT-PR-775", ""), ("PR-92-paper", ""),
        ("pool-x", ""), ("pool-pr-followups", ""), ("POOL-PR-DISCIPLINE", ""),
        ("pool-pr-discipline", "project/11"), ("PR-5", "project/3"),
        ("issue-5", ""), ("slice-1", ""), ("pool-x", f"pr/{REPO}/5"), ("misc", "pr/5"),
        ("PR-5", "garbage"), ("PR-5", f"PR/{REPO}/5"),
    ]

    def test_is_pr_baton_matches_baton_py(self):
        import baton as cli_baton
        gate = server_mod._load_baton_gate()
        for pid, github in self.CASES:
            fields = {"github": github}
            assert gate._is_pr_baton(pid, fields) == cli_baton._is_pr_baton(pid, fields), (pid, github)
