"""#3039 U2b round 5 (S1-S4): anchor-number and approval-tie hardening.

S1  a huge-digit anchor / id number must never raise (Python 3.12 caps
    int-from-str at 4300 digits) nor leave a peer baton stuck: it is a 422 at
    anchor time (digits capped in the shared regex), and number comparison does
    not go through ``int()``.
S2  ASCII digits only: Arabic-Indic digits (accepted by ``\\d`` / ``isdigit`` /
    ``int``) are refused everywhere an id or anchor number is read.
S3  for a PR baton the repo SET or COMPLETED on a peer/merge baton must equal
    the repo the id's prefix maps to in the admin ``repos`` table (wrong repo =>
    422 at anchor time instead of a baton locked on it). Map unavailable =>
    the previous narrow behaviour.
S4  ANY two opinionated reviews by the wanted login that share the MAXIMUM
    ``submittedAt`` and differ in state => ``ambiguous`` (not only the last two).

Identities are SYNTHETIC.
"""

import sys

import pytest

sys.path.insert(0, "src/forum/tests")
from test_baton_advance_api import (  # noqa: E402
    World, FakeGh, rv, SENTINEL, AUTHOR, COLLEAGUE, SEAT, REPO, HEAD, MAP,
)

import subprocess  # noqa: E402
from test_baton_anchor_binding import disk, seq_state  # noqa: E402

AR = "٩٠٠١"  # Arabic-Indic digits for 9001
REPOS = {"PR": REPO}


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _anchor(world, pid, value):
    return world.client.post(f"/api/projects/{pid}/anchor", json={"agent": AUTHOR, "github": value})


# ---------------------------------------------------------------- S1 ----------

class TestS1HugeDigits:
    BIG = "9" * 5000

    def test_regex_rejects_over_cap_numbers(self, world):
        g = world.gate
        assert g._GITHUB_ANCHOR_RE.match(f"pr/{REPO}/{'9' * 15}")
        assert not g._GITHUB_ANCHOR_RE.match(f"pr/{REPO}/{'9' * 16}")
        assert not g._GITHUB_ANCHOR_RE.match(f"pr/{self.BIG}")
        assert not g._GITHUB_ANCHOR_RE.match(f"project/{'9' * 16}")
        assert g._parse_github_anchor(f"pr/{REPO}/{self.BIG}") is None

    def test_binding_refusal_does_not_raise_on_huge_numbers(self, world):
        g = world.gate
        # huge ANCHOR number
        assert isinstance(g._anchor_id_binding_refusal("PR-9001", REPO, self.BIG, REPOS), str)
        # huge ID number
        assert isinstance(g._anchor_id_binding_refusal(f"PR-{self.BIG}", REPO, "1", REPOS), str)
        # equal huge numbers compare equal, unequal differ, leading zeros still equal
        assert g._anchor_id_binding_refusal(f"PR-{self.BIG}", REPO, self.BIG, REPOS) is None
        assert g._anchor_id_binding_refusal("PR-9001", REPO, "09001", REPOS) is None
        assert g._anchor_id_binding_refusal("PR-9001", REPO, "9002", REPOS) is not None

    def test_anchor_route_huge_number_is_422_never_500_on_fairy(self, world):
        world.seed("PR-9001", phase="fairy", github=None)
        r = _anchor(world, "PR-9001", f"pr/{REPO}/{self.BIG}")
        assert r.status_code == 422

    def test_anchor_route_huge_number_does_not_strand_a_peer_baton(self, world):
        world.seed("PR-9001", phase="peer", github="pr/9001")
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        r = _anchor(world, "PR-9001", f"pr/{REPO}/{self.BIG}")
        assert r.status_code == 422
        # R6: nothing is completable by a request any more -- the baton is not
        # stranded because the gate DERIVES the repo (D1) and can still advance.
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0
        ok = world.advance("PR-9001")
        assert ok.status_code == 201, ok.get_json()

    def test_anchor_completes_does_not_raise(self, world):
        from forum import server as s
        g = world.gate
        assert s._anchor_completes(g, "pr/9001", f"pr/{REPO}/{self.BIG}") is False


# ---------------------------------------------------------------- S2 ----------

class TestS2AsciiDigitsOnly:
    def test_anchor_regex_and_parser(self, world):
        g = world.gate
        assert not g._GITHUB_ANCHOR_RE.match(f"pr/{AR}")
        assert not g._GITHUB_ANCHOR_RE.match(f"pr/{REPO}/{AR}")
        assert not g._GITHUB_ANCHOR_RE.match(f"project/{AR}")
        assert g._parse_github_anchor(f"pr/{REPO}/{AR}") is None
        assert g._parse_github_anchor(f"pr/{AR}") is None
        assert g._parse_github_anchor(f"project/{AR}") is None

    def test_id_binding(self, world):
        g = world.gate
        assert g._anchor_id_binding_refusal(f"PR-{AR}", REPO, "9001", REPOS) is not None
        assert g._anchor_id_binding_refusal("PR-9001", REPO, AR, REPOS) is not None

    def test_gh_state_helpers_refuse_non_ascii_numbers(self, world):
        g = world.gate
        st, _ = g._reviewer_approval_state(AR, REPO, "x")
        assert st == "unknown"
        st, _ = g._pr_approval_state(AR, REPO)
        assert st == "unknown"
        assert world.gh.calls == []  # refused before any gh call

    def test_anchor_route_422(self, world):
        world.seed("PR-9001", phase="fairy", github=None)
        assert _anchor(world, "PR-9001", f"pr/{REPO}/{AR}").status_code == 422


# ---------------------------------------------------------------- S3 ----------

class TestS3RepoMustMatchPrefix:
    WRONG = "example-org/other-repo"

    def test_set_when_none_on_peer_wrong_repo_refused(self, world):
        world.seed("PR-9001", phase="peer", github=None)
        r = _anchor(world, "PR-9001", f"pr/{self.WRONG}/9001")
        assert r.status_code == 422
        assert (world.fields("PR-9001")[0].get("github") or "") == ""

    def test_complete_unqualified_on_peer_wrong_repo_refused(self, world):
        world.seed("PR-9001", phase="peer", github="pr/9001")
        r = _anchor(world, "PR-9001", f"pr/{self.WRONG}/9001")
        assert r.status_code == 422
        assert world.fields("PR-9001")[0]["github"].strip() == "pr/9001"

    def test_right_repo_is_still_refused_on_peer_set_and_complete(self, world):
        # R6: no exceptions -- even the CORRECT repo cannot be set or completed in peer.
        world.seed("PR-9001", phase="peer", github=None)
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        assert _anchor(world, "PR-9001", f"pr/{REPO}/9001").status_code == 422
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0
        world.seed("PR-9002", phase="peer", github="pr/9002")
        before, seq0 = disk(world, "PR-9002"), seq_state(world)
        assert _anchor(world, "PR-9002", f"pr/{REPO.upper()}/9002").status_code == 422
        assert disk(world, "PR-9002") == before and seq_state(world) == seq0

    def test_fairy_baton_may_still_be_set_and_reanchored(self, world):
        world.seed("PR-9001", phase="fairy", github=None)
        # r9: a fairy baton may still be set and re-anchored, but the anchor must be
        # the one its id names -- a wrong (mapped) repo is refused at the write.
        before, seq0 = disk(world, "PR-9001"), seq_state(world)
        assert _anchor(world, "PR-9001", f"pr/{self.WRONG}/9001").status_code == 422
        assert disk(world, "PR-9001") == before and seq_state(world) == seq0
        assert _anchor(world, "PR-9001", f"pr/{REPO}/9001").status_code == 201
        assert _anchor(world, "PR-9001", f"pr/{REPO}/9001").status_code == 201  # identical re-send

    def test_map_unavailable_still_refuses_completion(self, tmp_path, monkeypatch):
        w = World(tmp_path, monkeypatch, with_map=False)
        w.seed("PR-9001", phase="peer", github="pr/9001")
        w.seed("PR-9002", phase="peer", github="pr/9002")
        before = {pid: disk(w, pid) for pid in ("PR-9001", "PR-9002")}
        seq0 = seq_state(w)
        # R6: no exceptions, with or without a map to check a repo against
        assert _anchor(w, "PR-9001", f"pr/{self.WRONG}/9001").status_code == 422
        assert _anchor(w, "PR-9002", f"pr/{self.WRONG}/9003").status_code == 422  # still no re-point
        assert {pid: disk(w, pid) for pid in before} == before and seq_state(w) == seq0

    def test_unmapped_prefix_still_refuses_completion(self, world):
        world.seed("ZZ-9001", phase="peer", github="pr/9001")
        before, seq0 = disk(world, "ZZ-9001"), seq_state(world)
        assert _anchor(world, "ZZ-9001", f"pr/{self.WRONG}/9001").status_code == 422
        assert disk(world, "ZZ-9001") == before and seq_state(world) == seq0


# ---------------------------------------------------------------- S4 ----------

class TestS4AmbiguousTies:
    def _state(self, world, monkeypatch, reviews):
        world.gh.reviews = reviews
        return world.gate._reviewer_approval_state("9001", REPO, "example-gh-colleague")

    T = "2026-10-01T10:00:00Z"

    def test_cr_approved_approved_at_one_second_is_ambiguous(self, world, monkeypatch):
        L = "example-gh-colleague"
        st, _ = self._state(world, monkeypatch, [
            rv("R1", L, "CHANGES_REQUESTED", ts=self.T),
            rv("R2", L, "APPROVED", ts=self.T),
            rv("R3", L, "APPROVED", ts=self.T)])
        assert st == "ambiguous"

    def test_approved_approved_cr_at_one_second_is_ambiguous(self, world, monkeypatch):
        L = "example-gh-colleague"
        st, _ = self._state(world, monkeypatch, [
            rv("R1", L, "APPROVED", ts=self.T),
            rv("R2", L, "APPROVED", ts=self.T),
            rv("R3", L, "CHANGES_REQUESTED", ts=self.T)])
        assert st == "ambiguous"

    def test_same_state_ties_are_not_ambiguous(self, world, monkeypatch):
        L = "example-gh-colleague"
        st, _ = self._state(world, monkeypatch, [
            rv("R1", L, "APPROVED", ts=self.T), rv("R2", L, "APPROVED", ts=self.T)])
        assert st == "approved_current"

    def test_older_disagreement_is_not_a_tie(self, world, monkeypatch):
        L = "example-gh-colleague"
        st, _ = self._state(world, monkeypatch, [
            rv("R1", L, "CHANGES_REQUESTED", ts="2026-10-01T09:00:00Z"),
            rv("R2", L, "APPROVED", ts=self.T), rv("R3", L, "APPROVED", ts=self.T)])
        assert st == "approved_current"

    def test_last_two_still_ambiguous(self, world, monkeypatch):
        L = "example-gh-colleague"
        st, _ = self._state(world, monkeypatch, [
            rv("R1", L, "APPROVED", ts=self.T), rv("R2", L, "CHANGES_REQUESTED", ts=self.T)])
        assert st == "ambiguous"
