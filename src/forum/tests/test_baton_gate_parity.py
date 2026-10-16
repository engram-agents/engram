"""Contract tests: multi-login approval semantics (R4) and flip-classification parity (R6).

#3039 U2b round 4, written BEFORE the fix.

  R4  `_advance_gate` over a colleague's several mapped logins passes only if at
      least one login is `approved_current` AND no mapped login's verdict is
      `changes_requested` or `ambiguous` (and, this implementation's reading of
      "unknown must not render as permitted", none is `unknown` / `incomplete`).
      A login with `no_review` / `dismissed` / `stale_head` alongside an
      approving login is fine. Same answer from both spellings.
  R6  `_flip_phase_inputs` and `_check_flip_gate` must classify "is this the
      peer -> merge flip" identically. A parity grid drives both over the
      cross-product of (seed phase, colleague set/unset, to_agent role, client
      phase/transition_phase variant) and asserts they agree with an independent
      oracle and with each other.

Identities are SYNTHETIC; no real fleet name appears here.
"""

import json

import pytest

from forum import server as server_mod
from forum.tests.test_baton_advance_api import (
    ANCHOR,
    AUTHOR,
    COLLEAGUE,
    MAP,
    OTHER,
    REPO,
    SEAT,
    SENTINEL,
    World,
    rv,
)

A = "example-gh-colleague"
B = "example-gh-colleague-alt"
T = "2026-10-01T10:00:00Z"
T2 = "2026-10-01T11:00:00Z"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def two_logins(world, order=(A, B)):
    data = json.loads(json.dumps(MAP))
    data["map"][COLLEAGUE] = list(order)
    world.map_path.write_text(json.dumps(data))


def spell(world, spelling):
    return world.advance() if spelling == "advance" else world.flip(to=SEAT)


SPELLINGS = ["advance", "flip"]
ORDERS = [(A, B), (B, A)]


def other_login_cases():
    """(id, reviews, passes, verdict word expected in the refusal)."""
    appr = rv("R1", A, "APPROVED", ts=T)
    return [
        ("other-requests-changes", [appr, rv("R2", B, "CHANGES_REQUESTED", ts=T2)], False, "changes_requested"),
        ("other-ambiguous-tie", [appr, rv("R2", B, "APPROVED", ts=T), rv("R3", B, "CHANGES_REQUESTED", ts=T)],
         False, "ambiguous"),
        ("other-unknown-null-ts", [appr, rv("R2", B, "APPROVED", ts=None)], False, "unknown"),
        ("other-no-review", [appr], True, None),
        ("other-dismissed", [appr, rv("R2", B, "APPROVED", ts=T), rv("R3", B, "DISMISSED", ts=T2)], True, None),
        ("other-stale-head", [appr, rv("R2", B, "APPROVED", oid="b" * 40, ts=T)], True, None),
        ("other-approved-too", [appr, rv("R2", B, "APPROVED", ts=T)], True, None),
    ]


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("order", ORDERS, ids=["A-first", "B-first"])
@pytest.mark.parametrize("case", other_login_cases(), ids=[c[0] for c in other_login_cases()])
def test_R4_one_approving_login_does_not_outvote_another_login(world, spelling, order, case):
    _id, reviews, passes, word = case
    two_logins(world, order)
    world.gh.reviews = reviews
    world.seed()
    resp = spell(world, spelling)
    if passes:
        assert resp.status_code == 201, (resp.status_code, resp.get_json())
    else:
        assert resp.status_code == 422, resp.get_json()
        assert word in resp.get_json()["error"]
        f, _ = world.fields()
        assert f["phase"] == "peer"


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_R4_no_approving_login_still_refuses(world, spelling):
    two_logins(world)
    world.gh.reviews = [rv("R1", B, "APPROVED", oid="b" * 40)]
    world.seed()
    assert spell(world, spelling).status_code == 422


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_R4_a_single_ambiguous_login_refuses_through_the_server(world, spelling):
    world.gh.reviews = [rv("R1", A, "APPROVED", ts=T), rv("R2", A, "CHANGES_REQUESTED", ts=T)]
    world.seed()
    resp = spell(world, spelling)
    assert resp.status_code == 422 and "ambiguous" in resp.get_json()["error"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_R3_a_null_submitted_at_on_the_approval_refuses_through_the_server(world, spelling):
    world.gh.reviews = [rv("R1", A, "APPROVED", ts=None)]
    world.seed()
    resp = spell(world, spelling)
    assert resp.status_code == 422 and "unknown" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# R6 -- parity of the two classifiers
# ---------------------------------------------------------------------------

HOLDER = OTHER  # the seed's current turn holder

SEED_PHASES = ["peer", "fairy", None, "merge"]
COLLEAGUES = [COLLEAGUE, None]
TARGETS = {
    "merge-seat": SEAT,
    "sentinel": SENTINEL,
    "author": AUTHOR,
    "colleague": COLLEAGUE,
    "holder": HOLDER,
}
CLIENT = {
    "none": (None, None),
    "phase=merge": ("merge", None),
    "transition=merge": (None, "merge"),
    "phase=peer": ("peer", None),
    "transition=peer": (None, "peer"),
}


def oracle(phase, colleague, to_agent, client):
    """Independent statement of the rule both functions must implement."""
    resolved = phase or ""
    maintainer = to_agent == SENTINEL or to_agent not in {HOLDER, colleague or "", AUTHOR}
    claims_merge = "merge" in client
    strict = (maintainer or claims_merge) and (
        resolved == "peer" or (resolved in ("", "fairy") and bool(colleague)))
    expects_merge = maintainer and resolved in ("merge", "fairy", "")
    expects_peer = (not maintainer) and bool(colleague) and to_agent == colleague \
        and resolved in ("fairy", "")
    return strict, ("merge" if expects_merge else "peer" if expects_peer else None)


@pytest.mark.parametrize("client", list(CLIENT))
@pytest.mark.parametrize("target", list(TARGETS))
@pytest.mark.parametrize("colleague", COLLEAGUES, ids=["colleague", "no-colleague"])
@pytest.mark.parametrize("phase", SEED_PHASES, ids=["peer", "fairy", "legacy", "merge"])
def test_R6_flip_classifiers_agree(world, monkeypatch, phase, colleague, target, client):
    world.seed(phase=phase, colleague=colleague, turn=HOLDER)
    to_agent = TARGETS[target]
    cphase, ctrans = CLIENT[client]
    want_strict, want_expected = oracle(phase, colleague, to_agent, (cphase, ctrans))

    called = []
    monkeypatch.setattr(server_mod, "_advance_gate",
                        lambda *a, **k: (called.append(1) or (None, {"noop": True})))
    server_mod._check_flip_gate(world.store, "PR-9001", to_agent,
                                phase=cphase, transition_phase=ctrans, login_map_cfg={})
    used_strict = bool(called)

    def expected_transition():
        for probe in ("merge", "peer"):
            _p, _t, refusal = server_mod._flip_phase_inputs(
                "route", "PR-9001", world.store, to_agent, None, probe)
            if refusal is None:
                return probe
        return None

    got_expected = expected_transition()

    assert used_strict == want_strict, ("_check_flip_gate", used_strict, want_strict)
    assert got_expected == want_expected, ("_flip_phase_inputs", got_expected, want_expected)
    # The parity statement itself: when the weak path would write a `merge`
    # transition from fairy / legacy-unset on a baton WITH a colleague, the gate
    # must already have gone strict -- the two may never both let it through.
    resolved = phase or ""
    if got_expected == "merge" and resolved in ("", "fairy") and colleague:
        assert used_strict, "flip path expects merge on a colleague baton but the strict gate was skipped"
    if resolved == "peer":
        assert got_expected != "merge", "weak path expects a merge transition from peer"


def test_R6_both_classifiers_carry_the_parity_comment():
    import inspect
    for fn in (server_mod._flip_phase_inputs, server_mod._check_flip_gate):
        text = inspect.getsource(fn)
        assert "classify" in text and "identically" in text, fn.__name__
