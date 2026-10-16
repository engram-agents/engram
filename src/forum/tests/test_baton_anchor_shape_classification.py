"""Contract tests: #3039 U2b round 10 -- classification follows the ID SHAPE.

Written BEFORE the implementation (contract-first). A baton whose id carries a
number AND whose id prefix is in the admin ``repos`` table is PR-shaped and may
carry ONLY a binding ``pr/`` anchor. A ``project/<N>`` anchor on such an id is
refused 422 at EVERY anchor write (init, ``/anchor`` in fairy / legacy), which
makes the PR -> bunch -> PR round trip through a ``project/<N>`` anchor
unreachable. Bunches (unmapped prefix) are untouched; legacy files on disk are
never rewritten or refused unless a write is attempted against them.

  K1 init refusal   K2 /anchor refusal   K3 round trip unreachable
  K4 legacy file re-anchorable only to a correct pr/ anchor
  K5 not checked (unmapped prefix, no number, no map, pool bunch, peer/merge)
  K6 one shared function

Identities are SYNTHETIC; no real fleet name appears here.
"""

import sys

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import (  # noqa: E402
    World, ANCHOR, AUTHOR, COLLEAGUE, OTHER, SEAT, REPO,
)
from test_baton_anchor_binding import disk, seq_state  # noqa: E402
from test_baton_anchor_round6 import anchor, init_post  # noqa: E402

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


def _seed_fairy(world, pid=PID, github=None, **kw):
    world.seed(pid=pid, phase="fairy", github=github, turn=AUTHOR, **kw)


# --------------------------------------------------------------------- K1 ----

class TestInitRefusesProjectAnchorOnPrShapedId:
    def test_422_no_file_no_seq_text_names_id_and_pr_only(self, world):
        seq0 = seq_state(world)
        r = init_post(world, PID, "project/5")
        assert r.status_code == 422, r.get_json()
        err = r.get_json()["error"]
        assert PID in err and "pr/" in err and "project/5" in err, err
        assert world.store.read_project(PID) is None and seq_state(world) == seq0

    def test_same_id_with_binding_pr_anchor_is_then_201(self, world):
        assert init_post(world, PID, "project/5").status_code == 422
        r = init_post(world, PID, f"pr/{REPO}/9001")
        assert r.status_code == 201, r.get_json()


# --------------------------------------------------------------------- K2 ----

class TestAnchorEndpointRefusesProjectAnchorOnPrShapedId:
    def test_fairy_422_bytes_and_seq_unchanged_pr_anchor_201(self, world):
        _seed_fairy(world)
        before, seq0 = disk(world, PID), seq_state(world)
        r = anchor(world, "project/5")
        assert r.status_code == 422, r.get_json()
        err = r.get_json()["error"]
        assert PID in err and "pr/" in err, err
        assert disk(world, PID) == before and seq_state(world) == seq0
        assert anchor(world, ANCHOR).status_code == 201

    def test_legacy_phaseless_422_bytes_and_seq_unchanged_pr_anchor_201(self, world):
        world.seed(phase=None, github=None, turn=AUTHOR)
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, "project/5").status_code == 422
        assert disk(world, PID) == before and seq_state(world) == seq0
        assert anchor(world, ANCHOR).status_code == 201

    def test_a_project_anchor_over_a_good_pr_anchor_is_refused_too(self, world):
        _seed_fairy(world, github=ANCHOR)
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, "project/5").status_code == 422
        assert disk(world, PID) == before and seq_state(world) == seq0


# --------------------------------------------------------------------- K3 ----

class TestRoundTripUnreachable:
    def test_first_step_refuses_and_baton_never_changes(self, world):
        _seed_fairy(world)
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, "project/5").status_code == 422
        f, _ = world.fields()
        assert f["phase"].strip() == "fairy" and f["turn"].strip() == AUTHOR
        assert disk(world, PID) == before and seq_state(world) == seq0


# --------------------------------------------------------------------- K4 ----

class TestLegacyProjectAnchoredPrIdFileOnDisk:
    def test_correct_pr_anchor_re_anchors_201(self, world):
        _seed_fairy(world, github="project/7")
        r = anchor(world, f"pr/{REPO}/9001")
        assert r.status_code == 201, r.get_json()
        assert world.fields()[0]["github"].strip() == f"pr/{REPO}/9001"

    def test_wrong_pr_anchor_is_422_unchanged(self, world):
        _seed_fairy(world, github="project/7")
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, f"pr/{REPO}/1234").status_code == 422
        assert disk(world, PID) == before and seq_state(world) == seq0

    def test_unrelated_writes_do_not_touch_the_legacy_file_anchor(self, world):
        _seed_fairy(world, github="project/7")
        world.client.post(f"/api/projects/{PID}/participants",
                          json={"agent": AUTHOR, "participant": SEAT})
        assert world.fields()[0]["github"].strip() == "project/7"


# --------------------------------------------------------------------- K5 ----

class TestNotChecked:
    @pytest.mark.parametrize("pid", ["pool-x-5", "misc-1"])
    def test_unmapped_prefix_behaves_as_today(self, world, pid):
        assert init_post(world, pid, "project/4").status_code == 201
        assert anchor(world, "project/9", pid=pid).status_code == 201
        pid2 = pid + "9"
        assert init_post(world, pid2, f"pr/{OTHER_REPO}/{pid2.rsplit('-', 1)[1]}").status_code == 201

    @pytest.mark.parametrize("pid", ["DESIGN-trust-v2", "PR-shape"])
    def test_id_without_a_number_behaves_as_today(self, world, pid):
        assert init_post(world, pid, "project/4").status_code == 201
        assert anchor(world, "project/6", pid=pid).status_code == 201

    def test_no_map_world_accepts_project_anchor_on_pr_id(self, world_nomap):
        w = world_nomap
        assert init_post(w, "PR-9201", "project/5").status_code == 201
        _seed_fairy(w, pid="PR-9202")
        assert anchor(w, "project/5", pid="PR-9202").status_code == 201

    def test_pool_phase_bunch_project_anchors_untouched(self, world):
        _init(world.store, world.allocator, "pool-z-5", title="bunch", status="in-progress",
              turn=OTHER, participants=[AUTHOR, OTHER], turn_reason="start",
              github="project/7", author=AUTHOR, transition_phase="pool")
        assert anchor(world, "project/9", pid="pool-z-5").status_code == 201
        assert init_post(world, "pool-w-5", "project/9", transition_phase="pool").status_code == 201

    @pytest.mark.parametrize("phase", ["peer", "merge"])
    def test_peer_and_merge_anchor_already_422(self, world, phase):
        world.seed(phase=phase, github=ANCHOR)
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, "project/5").status_code == 422
        assert disk(world, PID) == before and seq_state(world) == seq0

    def test_pr_anchors_on_pr_ids_still_accepted(self, world):
        assert init_post(world, "PR-9301", f"pr/{REPO}/9301").status_code == 201
        assert init_post(world, "PR-9302", "pr/9302").status_code == 201


# --------------------------------------------------------------------- K6 ----

class TestOneSharedFunction:
    def test_pure_function_contract(self, world):
        f = server_mod._anchor_write_binding_refusal
        cfg = server_mod._login_map_cfg(world.app)
        g = world.gate
        for is_pr in (True, False):
            msg = f(g, "PR-9001", is_pr, "project/5", cfg)
            assert msg and "PR-9001" in msg and "pr/" in msg
        assert f(g, "pool-x-5", False, "project/5", cfg) is None   # unmapped prefix
        assert f(g, "PR-shape", False, "project/5", cfg) is None   # no number
        assert f(g, "PR-9001", True, ANCHOR, cfg) is None          # pr/ anchor binds
        assert f(g, "PR-9001", True, "project/5", None) is None    # no map configured

    def test_init_and_anchor_both_go_through_it(self, world, monkeypatch):
        calls = []
        real = server_mod._anchor_write_binding_refusal

        def spy(gate, pid, is_pr, anchor_norm, login_map_cfg):
            calls.append((pid, anchor_norm))
            return real(gate, pid, is_pr, anchor_norm, login_map_cfg)

        monkeypatch.setattr(server_mod, "_anchor_write_binding_refusal", spy)
        init_post(world, "PR-9401", "project/5")
        _seed_fairy(world)
        anchor(world, "project/5")
        assert ("PR-9401", "project/5") in calls and (PID, "project/5") in calls, calls
