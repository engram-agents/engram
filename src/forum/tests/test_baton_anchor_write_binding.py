"""Contract tests: #3039 U2b round 9 -- the anchor is id-bound at every WRITE.

Written BEFORE the implementation (contract-first). The id binding (the anchor's
PR number == the baton id's number; when the id's prefix is in the admin
``repos`` table, the anchor's repo == that entry) was checked only at
``advance``. The anchor is write-once past ``fairy``, so a wrong anchor written
in ``fairy`` stranded the baton in ``peer`` (advance 422, /anchor 422, send-back
422, id not reusable until a made-up merge). Now it is refused where it is
WRITTEN: init (qualified and unqualified) and ``POST /anchor`` in fairy/legacy.

  B1-1  /anchor on a PR baton: wrong number 422, wrong mapped repo 422, match 201;
        the 422 names id / number / repo; refusals write nothing (bytes + seq).
  B1-2  init: same checks, qualified AND unqualified; a refused init creates no
        file and the id is then re-initable with the right anchor.
  B1-3  the stranded state is unreachable (sequence test).
  B1-4  NOT checked: id without a number, bunches/project anchors, repo when the
        prefix is unmapped / no map (number still checked), legacy files on disk.
  B1-5  E2: no-colleague PR baton cannot be steered onto an unrelated PR.
  B1-6  ONE function does the check for all three write sites.

Identities are SYNTHETIC; no real fleet name appears here.
"""

import sys
from pathlib import Path

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
WRONG_NUM = f"pr/{REPO}/1234"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def world_nomap(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, with_map=False)


def _seed_fairy(world, pid=PID, github=None, **kw):
    world.seed(pid=pid, phase="fairy", github=github, turn=AUTHOR, **kw)


# ------------------------------------------------------------------- B1-1 ----

class TestAnchorEndpointBindsToId:
    @pytest.mark.parametrize("value", [WRONG_NUM, "pr/1234", f"pr/{REPO}/90010"])
    def test_wrong_number_is_422_names_id_number_repo_writes_nothing(self, world, value):
        _seed_fairy(world)
        before, seq0 = disk(world, PID), seq_state(world)
        r = anchor(world, value)
        assert r.status_code == 422, r.get_json()
        err = r.get_json()["error"]
        assert PID in err and "9001" in err and REPO in err, err
        assert disk(world, PID) == before and seq_state(world) == seq0

    def test_wrong_mapped_repo_is_422_writes_nothing(self, world):
        _seed_fairy(world)
        before, seq0 = disk(world, PID), seq_state(world)
        r = anchor(world, f"pr/{OTHER_REPO}/9001")
        assert r.status_code == 422, r.get_json()
        err = r.get_json()["error"]
        assert PID in err and "9001" in err and REPO in err, err
        assert disk(world, PID) == before and seq_state(world) == seq0

    @pytest.mark.parametrize("value", [ANCHOR, ANCHOR.upper(), "pr/9001"])
    def test_matching_anchor_is_201(self, world, value):
        _seed_fairy(world)
        r = anchor(world, value)
        assert r.status_code == 201, r.get_json()

    def test_a_wrong_re_anchor_over_a_good_one_is_refused_too(self, world):
        _seed_fairy(world, github=ANCHOR)
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, WRONG_NUM).status_code == 422
        assert disk(world, PID) == before and seq_state(world) == seq0
        assert world.fields()[0]["github"].strip() == ANCHOR

    def test_legacy_phaseless_baton_is_checked_too(self, world):
        world.seed(phase=None, github=None, turn=AUTHOR)  # resolves "" -> anchor writable
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, WRONG_NUM).status_code == 422
        assert disk(world, PID) == before and seq_state(world) == seq0
        assert anchor(world, ANCHOR).status_code == 201

    def test_a_pr_id_re_anchored_from_project_to_a_wrong_pr_is_refused(self, world):
        _seed_fairy(world, github="project/7")
        before, seq0 = disk(world, PID), seq_state(world)
        assert anchor(world, WRONG_NUM).status_code == 422
        assert disk(world, PID) == before and seq_state(world) == seq0


# ------------------------------------------------------------------- B1-2 ----

class TestInitBindsToId:
    @pytest.mark.parametrize("pid,value", [
        ("PR-9002", WRONG_NUM),                      # the Borges reproduction
        ("PR-9003", f"pr/{OTHER_REPO}/9003"),        # mapped repo mismatch
        ("PR-9004", "pr/1234"),                      # unqualified, number mismatch
    ])
    def test_mismatch_is_422_creates_no_file_consumes_no_seq(self, world, pid, value):
        seq0 = seq_state(world)
        r = init_post(world, pid, value)
        assert r.status_code == 422, r.get_json()
        assert pid in r.get_json()["error"]
        assert world.store.read_project(pid) is None and seq_state(world) == seq0

    def test_qualified_mismatch_message_names_expected_number_and_repo(self, world):
        err = init_post(world, "PR-9002", WRONG_NUM).get_json()["error"]
        assert "9002" in err and REPO in err, err

    def test_re_init_with_the_correct_anchor_is_201(self, world):
        assert init_post(world, "PR-9002", WRONG_NUM).status_code == 422
        r = init_post(world, "PR-9002", f"pr/{REPO}/9002")
        assert r.status_code == 201, r.get_json()
        assert world.fields("PR-9002")[0]["github"].strip() == f"pr/{REPO}/9002"

    def test_unqualified_still_qualified_from_the_map(self, world):
        assert init_post(world, "PR-9005", "pr/9005").status_code == 201
        assert world.fields("PR-9005")[0]["github"].strip() == f"pr/{REPO}/9005"

    def test_unqualified_unmapped_prefix_still_422(self, world):
        assert init_post(world, "ZZ-PR-9006", "pr/9006").status_code == 422

    def test_unqualified_no_map_still_422(self, world_nomap):
        assert init_post(world_nomap, "PR-9006", "pr/9006").status_code == 422

    def test_matching_qualified_is_201(self, world):
        assert init_post(world, "PR-9007", f"pr/{REPO}/9007").status_code == 201


# ------------------------------------------------------------------- B1-3 ----

class TestStrandedStateUnreachable:
    def test_borges_sequence_ends_at_the_anchor_step_and_correct_path_advances(self, world):
        _seed_fairy(world)
        # the mismatched write is refused ...
        assert anchor(world, WRONG_NUM).status_code == 422
        # ... so the baton can still go peer and advance once correctly anchored.
        assert anchor(world, ANCHOR).status_code == 201
        r = world.flip(to=COLLEAGUE, agent=AUTHOR, reason="fairy-converged",
                       transition_phase="peer")
        assert r.status_code == 201, r.get_json()
        assert world.fields()[0]["phase"].strip() == "peer"
        assert world.fields()[0]["github"].strip() == ANCHOR
        adv = world.advance()
        assert adv.status_code == 201, adv.get_json()

    def test_init_mismatch_creates_nothing_to_strand(self, world):
        assert init_post(world, "PR-9002", WRONG_NUM).status_code == 422
        assert world.store.read_project("PR-9002") is None
        assert anchor(world, WRONG_NUM, pid="PR-9002").status_code == 404

    @pytest.mark.parametrize("first", [None, "project/7", "pr/9001"])
    def test_no_bad_anchor_can_be_written_before_the_baton_goes_peer(self, world, first):
        _seed_fairy(world, github=first)
        for bad in (WRONG_NUM, "pr/1234", f"pr/{OTHER_REPO}/9001"):
            assert anchor(world, bad).status_code == 422
        assert (world.fields()[0].get("github") or "").strip() == (first or "")
        r = world.flip(to=COLLEAGUE, agent=AUTHOR, reason="x", transition_phase="peer")
        assert r.status_code in (201, 422), r.get_json()
        cur = (world.fields()[0].get("github") or "").strip()
        assert cur in ("", "project/7", "pr/9001")  # never a mismatched pr anchor


# ------------------------------------------------------------------- B1-4 ----

class TestNotChecked:
    def test_id_without_a_number_is_untouched(self, world):
        r = init_post(world, "DESIGN-trust-tier-v2", f"pr/{REPO}/5")
        assert r.status_code == 201, r.get_json()
        assert anchor(world, f"pr/{OTHER_REPO}/77", pid="DESIGN-trust-tier-v2").status_code == 201

    def test_bunches_and_project_anchors_are_untouched(self, world):
        _init(world.store, world.allocator, "pool-x-5", title="bunch", status="in-progress",
              turn=OTHER, participants=[AUTHOR, OTHER], turn_reason="start",
              github="project/7", author=AUTHOR, transition_phase="pool")
        assert anchor(world, "project/9", pid="pool-x-5").status_code == 201
        assert init_post(world, "pool-y-5", "project/9", transition_phase="pool").status_code == 201
        # round 10: a mapped-prefix numbered (PR-shaped) id takes only a pr/ anchor
        assert init_post(world, "PR-9008", "project/3").status_code == 422
        assert init_post(world, "misc-9008", "project/3").status_code == 201

    def test_unmapped_prefix_number_mismatch_422_right_number_any_repo_201(self, world):
        _seed_fairy(world, pid="ZZ-PR-9301", github=ANCHOR)
        before, seq0 = disk(world, "ZZ-PR-9301"), seq_state(world)
        assert anchor(world, f"pr/{OTHER_REPO}/5", pid="ZZ-PR-9301").status_code == 422
        assert disk(world, "ZZ-PR-9301") == before and seq_state(world) == seq0
        assert anchor(world, f"pr/{OTHER_REPO}/9301", pid="ZZ-PR-9301").status_code == 201
        assert init_post(world, "ZZ-PR-9302", f"pr/{OTHER_REPO}/5").status_code == 422
        assert init_post(world, "ZZ-PR-9302", f"pr/{OTHER_REPO}/9302").status_code == 201

    def test_no_map_number_mismatch_422_right_number_any_repo_201(self, world_nomap):
        w = world_nomap
        _seed_fairy(w, github=ANCHOR)
        assert anchor(w, f"pr/{OTHER_REPO}/5").status_code == 422
        assert anchor(w, f"pr/{OTHER_REPO}/9001").status_code == 201
        assert init_post(w, "PR-9303", f"pr/{OTHER_REPO}/5").status_code == 422
        assert init_post(w, "PR-9303", f"pr/{OTHER_REPO}/9303").status_code == 201

    def test_legacy_mismatched_files_on_disk_are_not_rewritten(self, world):
        _seed_fairy(world, github=WRONG_NUM)
        world.seed(pid="PR-9100", phase="peer", github=f"pr/{OTHER_REPO}/9100")
        world.client.post(f"/api/projects/{PID}/participants",
                          json={"agent": AUTHOR, "participant": SEAT})
        assert world.fields()[0]["github"].strip() == WRONG_NUM
        assert world.fields("PR-9100")[0]["github"].strip() == f"pr/{OTHER_REPO}/9100"
        # and advance still refuses them (the defensive re-check stays)
        assert world.advance(pid="PR-9100").status_code == 422


# ------------------------------------------------------------------- B1-5 ----

class TestNoColleagueCannotBeSteered:
    def test_e2_sequence_ends_at_the_anchor_step(self, world):
        world.seed(phase="fairy", github=None, colleague=None, turn=AUTHOR,
                   participants=(AUTHOR, SEAT))
        assert anchor(world, WRONG_NUM).status_code == 422
        assert anchor(world, "pr/1234").status_code == 422
        assert (world.fields()[0].get("github") or "").strip() == ""
        # the weak flip then has no unrelated PR to consult
        n_gh = len(world.gh.calls)
        world.flip(to=SEAT, agent=AUTHOR, reason="go")
        assert len(world.gh.calls) == n_gh


# ------------------------------------------------------------------- B1-6 ----

class TestOneSharedFunction:
    def test_all_three_sites_call_the_one_function(self, world, monkeypatch):
        calls = []
        real = server_mod._anchor_write_binding_refusal

        def spy(gate, pid, is_pr, anchor_norm, login_map_cfg):
            calls.append((pid, bool(is_pr), anchor_norm))
            return real(gate, pid, is_pr, anchor_norm, login_map_cfg)

        monkeypatch.setattr(server_mod, "_anchor_write_binding_refusal", spy)
        init_post(world, "PR-9401", f"pr/{REPO}/9401")      # init qualified
        init_post(world, "PR-9402", "pr/9402")              # init unqualified
        _seed_fairy(world)
        anchor(world, ANCHOR)                               # /anchor
        pids = {c[0] for c in calls}
        assert {"PR-9401", "PR-9402", PID} <= pids, calls

    def test_pure_function_contract(self, world):
        f = server_mod._anchor_write_binding_refusal
        cfg = server_mod._login_map_cfg(world.app)
        g = world.gate
        assert f(g, "PR-9001", True, ANCHOR, cfg) is None
        assert f(g, "PR-9001", True, "pr/9001", cfg) is None
        assert f(g, "PR-9001", True, WRONG_NUM, cfg)
        assert f(g, "PR-9001", True, f"pr/{OTHER_REPO}/9001", cfg)
        assert f(g, "PR-9001", False, WRONG_NUM, cfg) is None       # not PR-shaped
        assert f(g, "PR-9001", True, "project/5", cfg)              # round 10: PR-shaped id, project anchor refused
        assert f(g, "DESIGN-v2", True, WRONG_NUM, cfg) is None      # no number


def test_docs_state_the_binding_is_checked_at_every_write():
    text = " ".join(Path("docs/baton-protocol.md").read_text().lower().split())
    assert "at every write" in text and "not only at advance" in text
