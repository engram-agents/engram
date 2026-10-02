"""Contract tests: #3296 (part of #3039) -- ``POST /colleague`` is refused once a PR
baton is at ``merge``.

Written BEFORE the implementation (contract-first).

The route this closes: the CLI's ``cmd_flip`` ALWAYS sends ``transition_phase:
"merge"`` on the maintainer-flip path, and the server's weak gate (a baton with NO
colleague) writes ``phase: merge`` when a non-author approval is present. A
colleague set AFTER that is a colleague-bearing baton at ``merge`` that never passed
the strict peer -> merge gate. Every flip below therefore sends the CLI-shaped body
(``transition_phase: "merge"``); a flip without it measures a request the real
client never makes.

  S1  set OR change (identical re-send included) on a PR baton whose server-resolved
      phase is ``merge`` is a 422 naming the phase; writes nothing.
  S2  the exact reproduction (weak flip -> merge, then /colleague), over the merge
      seat AND the pool sentinel as flip target.
  S3  a baton that reached ``merge`` through the STRICT gate: a colleague swap is 422.
  S4  not over-reaching: fairy / peer / bunch (any phase) / closed-not-merge: 201.
  S5  legacy phase-less batons: resolved ``merge`` refused; resolved ``peer``/
      ``fairy`` unchanged; unresolvable phase fails CLOSED.

Identities are SYNTHETIC; no real fleet name appears here.
"""

import sys

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import (  # noqa: E402
    World, AUTHOR, COLLEAGUE, OTHER, SEAT, SENTINEL, REPO,
)
from test_baton_anchor_binding import disk, seq_state  # noqa: E402
from forum import server as server_mod  # noqa: E402

PID = "PR-9001"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def set_colleague(world, colleague=COLLEAGUE, pid=PID, agent=AUTHOR):
    return world.client.post(f"/api/projects/{pid}/colleague",
                             json={"agent": agent, "colleague": colleague})


def make_unresolvable(world, monkeypatch):
    def boom(*a, **kw):
        raise world.gate.PoolSentinelUnresolved("no sentinel in this test")
    monkeypatch.setattr(world.gate, "_resolve_pool_sentinel", boom)


def refused_and_untouched(world, call, pid=PID):
    before_bytes, before_seq = disk(world, pid), seq_state(world)
    before_colleague = world.fields(pid)[0].get("colleague")
    resp = call()
    assert resp.status_code == 422, resp.get_json()
    assert disk(world, pid) == before_bytes, "a refused /colleague wrote the file"
    assert seq_state(world) == before_seq, "a refused /colleague consumed a seq"
    assert world.fields(pid)[0].get("colleague") == before_colleague
    return resp.get_json()["error"]


# ---------------------------------------------------------------- S1 / S2 ----

class TestWeakFlipThenColleague:
    @pytest.mark.parametrize("target", ["seat", "sentinel"])
    def test_S2_exact_reproduction(self, world, target):
        world.seed(PID, phase="fairy", colleague=None, github=f"pr/{REPO}/9001", turn=AUTHOR)
        to = SEAT if target == "seat" else SENTINEL
        resp = world.flip(PID, to=to, agent=AUTHOR, reason="ready for merge",
                          transition_phase="merge")
        assert resp.status_code == 201, resp.get_json()
        assert world.fields(PID)[0]["phase"] == "merge"
        assert not world.fields(PID)[0].get("colleague")

        err = refused_and_untouched(world, lambda: set_colleague(world, COLLEAGUE))
        assert "merge" in err
        assert "colleague" in err.lower()
        assert not world.fields(PID)[0].get("colleague")

    def test_S1_message_names_phase_and_the_rule(self, world):
        world.seed(PID, phase="merge", colleague=None, turn=SEAT)
        err = refused_and_untouched(world, lambda: set_colleague(world))
        assert "'merge'" in err
        assert "cannot be set or changed" in err

    @pytest.mark.parametrize("kind", ["set", "change", "identical"])
    def test_S1_set_change_identical_all_refused(self, world, kind):
        colleague = None if kind == "set" else COLLEAGUE
        world.seed(PID, phase="merge", colleague=colleague, turn=SEAT)
        new = OTHER if kind == "change" else COLLEAGUE
        refused_and_untouched(world, lambda: set_colleague(world, new))


# --------------------------------------------------------------------- S3 ----

class TestStrictMergeThenSwap:
    def test_S3_swap_after_strict_advance_is_refused(self, world):
        world.seed()  # peer, colleague set, turn with colleague
        assert set_colleague(world, COLLEAGUE).status_code == 201  # accepted at peer
        assert world.advance().status_code == 201
        assert world.fields(PID)[0]["phase"] == "merge"
        refused_and_untouched(world, lambda: set_colleague(world, OTHER))
        assert world.fields(PID)[0]["colleague"] == COLLEAGUE


# --------------------------------------------------------------------- S4 ----

class TestNotOverReaching:
    def test_fairy_still_201(self, world):
        world.seed(PID, phase="fairy", colleague=None, turn=AUTHOR)
        assert set_colleague(world).status_code == 201

    def test_peer_still_201_set_and_change(self, world):
        world.seed(PID, phase="peer", colleague=COLLEAGUE, turn=COLLEAGUE)
        assert set_colleague(world, OTHER).status_code == 201
        assert set_colleague(world, OTHER).status_code == 201

    @pytest.mark.parametrize("phase", ["pool", "claimed", "done"])
    def test_bunch_any_phase_still_201(self, world, phase):
        world.seed("pool-x", phase=phase, colleague=None, github="project/4", turn=AUTHOR)
        assert set_colleague(world, pid="pool-x").status_code == 201

    @pytest.mark.parametrize("status", ["merged", "cancelled"])
    def test_closed_baton_not_at_merge_unchanged(self, world, status):
        world.seed(PID, phase="fairy", colleague=None, turn=AUTHOR, status=status)
        resp = set_colleague(world)
        # Pins what dev does today; no behaviour is invented for closed batons.
        assert resp.status_code == 201, resp.get_json()


# --------------------------------------------------------------------- S5 ----

class TestLegacyPhaseless:
    def test_legacy_resolving_to_merge_refused(self, world):
        world.seed(PID, phase=None, colleague=None, turn=SENTINEL)
        f, _ = world.fields(PID)
        assert not f.get("phase")
        assert world.gate._resolve_phase(f, is_bunch=False)[0] == "merge"
        err = refused_and_untouched(world, lambda: set_colleague(world))
        assert "'merge'" in err

    @pytest.mark.parametrize("turn", [COLLEAGUE, AUTHOR])
    def test_legacy_resolving_to_peer_or_fairy_unchanged(self, world, turn):
        # Phase-less, turn not at the sentinel => resolves to "" (neither merge nor
        # the sentinel); the server must not refuse it.
        world.seed(PID, phase=None, colleague=None, turn=turn)
        assert world.gate._resolve_phase(world.fields(PID)[0], is_bunch=False)[0] == ""
        assert set_colleague(world).status_code == 201

    def test_unresolvable_phase_fails_closed(self, world, monkeypatch):
        world.seed(PID, phase=None, colleague=None, turn=AUTHOR)
        make_unresolvable(world, monkeypatch)
        err = refused_and_untouched(world, lambda: set_colleague(world))
        assert "sentinel" in err.lower()

    def test_unresolvable_phase_on_bunch_does_not_refuse(self, world, monkeypatch):
        # A bunch's phase never gates the colleague write, resolvable or not.
        world.seed("pool-x", phase=None, colleague=None, github="project/4", turn=AUTHOR)
        make_unresolvable(world, monkeypatch)
        assert set_colleague(world, pid="pool-x").status_code == 201


# ------------------------------------------------- gate module unavailable ----

class TestGateModuleUnavailable:
    def test_gate_module_unavailable_fails_closed(self, world, monkeypatch):
        # The helper must fail CLOSED when the baton gate module cannot be loaded,
        # as _anchor_refusal does: a colleague write it cannot phase-check is a 422.
        # A fail-open (``return None``) here would write a colleague unchecked.
        # Seeded at 'peer' (a phase that would otherwise be a 201), so the refusal can
        # only come from the unavailable-gate branch.
        world.seed(PID, phase="peer", colleague=COLLEAGUE, turn=AUTHOR)
        assert set_colleague(world, colleague=COLLEAGUE).status_code == 201  # control
        monkeypatch.setattr(server_mod, "_BATON_GATE_MODULE_CACHE", [None])
        err = refused_and_untouched(world, lambda: set_colleague(world, colleague=OTHER))
        assert "gate unavailable" in err
        assert "could not load its gate module" in err
