"""Contract tests for #3039 U2b -- `peer -> merge` advance, server side.

Written BEFORE the implementation (contract-first). Real Flask app, real
FileStore + SeqAllocator; only `subprocess.run` (the `gh` boundary) is faked.

Covers spec cases T3-T5, T17-T22 (the gate primitives T1/T2/T6-T16/T23/T24 are
in tests/test_baton_advance_gate.py), D1 (one gate, two spellings), D2 (the
stated limit, pinned as behaviour), D3 (start option / env wiring), D4 (CI
`unknown` advisory, approval `unknown` refuses), C3 (check-and-set write
body), C5 (lock-only context on SeqAllocator).

Identities are SYNTHETIC; no real fleet name appears here.
"""

import json
import os
import sqlite3
import subprocess
import threading

import pytest

from forum import server as server_mod
from forum.coordination import SeqAllocator, init as _init, flip as _flip
from forum.coordination import set_colleague as _set_colleague
from forum.coordination.markdown import parse_frontmatter
from forum.coordination.store_file import FileStore
from forum.coordination.updates import build_updates
from forum.db import init_db
from forum.server import create_app

HEAD = "a" * 40
OLD = "b" * 40
REPO = "example-org/example-repo"
ANCHOR = f"pr/{REPO}/9001"
SENTINEL = "example-human"

AUTHOR = "example-agent-author"
COLLEAGUE = "example-agent-colleague"
OTHER = "example-agent-other"
SEAT = "example-agent-seat"

MAP = {
    "version": 1,
    "map": {
        COLLEAGUE: ["Example-GH-Colleague"],
        OTHER: ["example-gh-other"],
        AUTHOR: ["example-gh-author"],
        SEAT: ["example-gh-seat"],
    },
    # id prefix -> the one repo a baton with that prefix may be anchored to (A2).
    "repos": {"PR": REPO},
}


def rv(rid, login, state, oid=HEAD, ts="2026-10-01T10:00:00Z"):
    return {
        "id": rid, "state": state, "submittedAt": ts,
        "author": ({"login": login} if login is not None else None),
        "commit": ({"oid": oid} if oid is not None else None),
    }


def _rest_id(rid):
    """The REST review id is an integer; the fixtures name reviews with strings."""
    if isinstance(rid, str):
        return int.from_bytes(rid.encode("utf-8"), "big")
    return rid


def to_rest(node):
    """Translate a fixture review node (the old GraphQL field names) to the REST
    row the gate reads (#3541)."""
    c = node.get("commit")
    return {"id": _rest_id(node["id"]), "state": node["state"],
            "submitted_at": node["submittedAt"], "user": node["author"],
            "commit_id": c.get("oid") if isinstance(c, dict) else None}


class FakeGh:
    """Routes the gh calls the gate makes: CI (`gh pr view --json
    mergeStateStatus,...`), the head (`gh pr view --json headRefOid,author`) and
    the review rows (`gh api .../reviews?per_page=100&page=K`)."""

    def __init__(self):
        self.ci = "green"          # green | red | pending | unknown
        self.reviews = [rv("R1", "example-gh-colleague", "APPROVED")]
        self.head = HEAD
        self.pr_author = "example-gh-author"
        self.review_mode = None    # None | "nonzero"
        self.on_review_query = None
        self.barrier = None
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, cmd, *a, **kw):
        with self._lock:
            self.calls.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "view"] and "headRefOid,author" in cmd:
            payload = {"headRefOid": self.head, "author": {"login": self.pr_author}}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            if self.ci == "unknown":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ci boom")
            if self.ci == "green":
                out = {"mergeStateStatus": "CLEAN", "statusCheckRollup": []}
            elif self.ci == "red":
                out = {"mergeStateStatus": "BLOCKED", "statusCheckRollup": [
                    {"name": "pr-checks", "conclusion": "FAILURE", "completedAt": "2026-10-01T10:00:00Z"}]}
            else:
                out = {"mergeStateStatus": "BLOCKED", "statusCheckRollup": [
                    {"name": "pr-checks", "status": "IN_PROGRESS", "conclusion": ""}]}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")
        if cmd[:2] == ["gh", "api"] and "/reviews?" in cmd[2]:
            if self.barrier is not None:
                try:
                    self.barrier.wait(timeout=10)
                except threading.BrokenBarrierError:
                    pass
            if self.on_review_query is not None:
                self.on_review_query()
            if self.review_mode == "nonzero":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh boom")
            page_no = int(cmd[2].rsplit("page=", 1)[1])
            page = self.reviews[(page_no - 1) * 100:page_no * 100]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([to_rest(n) for n in page]), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected gh call")


class World:
    def __init__(self, tmp_path, monkeypatch, *, with_map=True):
        self.tmp_path = tmp_path
        db_path = str(tmp_path / "forum.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        self.store = FileStore(tmp_path / "coord")
        self.allocator = SeqAllocator(recover=self.store.recover_max_seq)

        cfg = tmp_path / "cfg"
        cfg.mkdir()
        os.chmod(cfg, 0o755)
        self.map_path = cfg / "baton-login-map.json"
        self.map_path.write_text(json.dumps(MAP))
        os.chmod(self.map_path, 0o644)

        kw = {}
        if with_map:
            kw = {"baton_login_map": str(self.map_path),
                  "baton_login_map_admin_uids": (os.getuid(),)}
        self.app = create_app(db_path, str(tmp_path / "audit.jsonl"), **kw)
        self.app.config["TESTING"] = True
        self.app.config["COORD_STORE"] = self.store
        self.app.config["COORD_ALLOCATOR"] = self.allocator
        # Test seam: production trust anchor is "/" (root + named admin uids).
        self.app.config["BATON_LOGIN_MAP_TRUST_ANCHOR"] = str(tmp_path)
        self.client = self.app.test_client()

        self.gate = server_mod._load_baton_gate()
        assert self.gate is not None
        self.gate._POOL_SENTINEL_CACHE.clear()
        monkeypatch.setattr(self.gate, "_resolve_pool_sentinel", lambda *a, **kw: SENTINEL)
        self.gh = FakeGh()
        monkeypatch.setattr(subprocess, "run", self.gh)

    def seed(self, pid="PR-9001", *, colleague=COLLEAGUE, phase="peer", github=ANCHOR,
             turn=COLLEAGUE, participants=(AUTHOR, COLLEAGUE, OTHER, SEAT), author=AUTHOR,
             status="in-progress"):
        _init(self.store, self.allocator, pid, title="advance test", status=status,
              turn=turn, participants=list(participants), turn_reason="start",
              github=github, colleague=colleague, author=author, transition_phase=phase)

    def fields(self, pid="PR-9001"):
        return parse_frontmatter(self.store.read_project(pid))

    def advance(self, pid="PR-9001", *, to=SEAT, agent=COLLEAGUE, reason="colleague-APPROVED", client=None):
        return (client or self.client).post(
            f"/api/projects/{pid}/advance",
            json={"agent": agent, "to_agent": to, "reason": reason})

    def flip(self, pid="PR-9001", *, to=SEAT, agent=COLLEAGUE, reason="ready for merge", **extra):
        body = {"agent": agent, "to_agent": to, "reason": reason}
        body.update(extra)
        return self.client.post(f"/api/projects/{pid}/flip", json=body)

    def log_lines(self, needle, pid="PR-9001"):
        return [ln for ln in self.store.read_project(pid).splitlines() if needle in ln]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def world_nomap(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch, with_map=False)


# ---------------------------------------------------------------------------
# Passing path, and what the write records
# ---------------------------------------------------------------------------

class TestAdvancePasses:
    def test_T5_advance_on_current_head_approval(self, world):
        world.seed()
        resp = world.advance()
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["advanced"] is True
        assert body["turn"] == SEAT
        f, _ = world.fields()
        assert f["phase"] == "merge"
        assert f["turn"] == SEAT
        assert f["phase"] != "pool"
        assert len(world.log_lines(f"{COLLEAGUE} → {SEAT}")) == 1

    def test_response_keeps_ci_and_approval_verdicts_separate(self, world):
        world.seed()
        body = world.advance().get_json()
        assert "ci" in body and "approval" in body
        assert body["ci"]["state"] == "green"
        assert body["approval"]["state"] == "approved_current"

    def test_approval_by_second_listed_login_of_the_colleague_counts(self, world):
        # Map semantics: any login listed for the colleague may carry the approval.
        data = json.loads(world.map_path.read_text())
        data["map"][COLLEAGUE] = ["Example-GH-Colleague", "example-gh-colleague-alt"]
        world.map_path.write_text(json.dumps(data))
        world.gh.reviews = [rv("R1", "example-gh-colleague-alt", "APPROVED")]
        world.seed()
        assert world.advance().status_code == 201

    def test_legacy_baton_without_phase_is_treated_as_peer_and_converts(self, world):
        world.seed(phase=None)
        f, _ = world.fields()
        assert not f.get("phase")
        resp = world.advance()
        assert resp.status_code == 201, resp.get_json()
        f, _ = world.fields()
        assert f["phase"] == "merge"
        # conversion record + transition record, like cmd_flip's two-step write
        assert world.log_lines("legacy conversion")
        assert world.log_lines("(transition)")


# ---------------------------------------------------------------------------
# T3 / T4 -- map configured, colleague mapped
# ---------------------------------------------------------------------------

class TestMapAndColleague:
    def test_T3_no_login_map_configured_fails_closed(self, world_nomap):
        world_nomap.seed()
        resp = world_nomap.advance()
        assert resp.status_code == 422
        assert "no login map configured" in resp.get_json()["error"]
        f, _ = world_nomap.fields()
        assert f["phase"] == "peer"

    def test_bad_map_file_fails_closed_with_cause(self, world):
        world.map_path.write_text("{ nope")
        world.seed()
        resp = world.advance()
        assert resp.status_code == 422
        assert "not valid JSON" in resp.get_json()["error"]

    def test_group_writable_map_file_fails_closed(self, world):
        os.chmod(world.map_path, 0o664)
        world.seed()
        resp = world.advance()
        assert resp.status_code == 422
        assert "writable" in resp.get_json()["error"]

    def test_unnamed_owner_uid_fails_closed(self, tmp_path, monkeypatch):
        if os.getuid() == 0:
            pytest.skip("root is always a trusted owner")
        w = World(tmp_path, monkeypatch)
        # Server started WITHOUT naming the admin uid: our own uid is not trusted.
        w.app.config["BATON_LOGIN_MAP_ADMIN_UIDS"] = ()
        w.seed()
        resp = w.advance()
        assert resp.status_code == 422
        assert "owner" in resp.get_json()["error"].lower()

    def test_T4_colleague_unset_refuses(self, world):
        world.seed(colleague=None)
        resp = world.advance()
        assert resp.status_code == 422
        assert "colleague" in resp.get_json()["error"].lower()

    def test_T4_colleague_without_map_entry_refuses_naming_agent(self, world):
        world.seed(colleague="example-agent-unlisted",
                   participants=(AUTHOR, "example-agent-unlisted", SEAT), turn="example-agent-unlisted")
        resp = world.advance(agent="example-agent-unlisted")
        assert resp.status_code == 422
        assert "example-agent-unlisted" in resp.get_json()["error"]

    def test_map_is_read_per_request_not_cached_forever(self, world):
        world.seed()
        assert world.advance().status_code == 201
        world.seed("PR-9002", github=f"pr/{REPO}/9002")
        world.map_path.write_text(json.dumps({"version": 1, "map": {OTHER: ["example-gh-other"]}}))
        resp = world.advance("PR-9002")
        assert resp.status_code == 422  # colleague no longer mapped


# ---------------------------------------------------------------------------
# Verdict table through the server (C1 reasons surface as refusals)
# ---------------------------------------------------------------------------

class TestVerdictsRefuseThroughServer:
    @pytest.mark.parametrize(
        "name, reviews, expect_word",
        [
            ("T6-approve-then-changes-requested",
             [rv("R1", "example-gh-colleague", "APPROVED", ts="2026-10-01T10:00:00Z"),
              rv("R2", "example-gh-colleague", "CHANGES_REQUESTED", ts="2026-10-01T11:00:00Z")],
             "changes_requested"),
            ("T8-stale-head",
             [rv("R1", "example-gh-colleague", "APPROVED", oid=OLD)], "stale_head"),
            ("T9-dismissed",
             [rv("R1", "example-gh-colleague", "APPROVED", ts="2026-10-01T10:00:00Z"),
              rv("R2", "example-gh-colleague", "DISMISSED", ts="2026-10-01T11:00:00Z")], "dismissed"),
            ("no-review", [], "no_review"),
            ("T10-approval-by-other-mapped-login",
             [rv("R1", "example-gh-other", "APPROVED")], "no_review"),
            ("T11-approval-by-unmapped-account",
             [rv("R1", "random-stranger", "APPROVED")], "no_review"),
        ],
    )
    def test_refuses(self, world, name, reviews, expect_word):
        world.gh.reviews = reviews
        world.seed()
        resp = world.advance()
        assert resp.status_code == 422, (name, resp.get_json())
        assert expect_word in resp.get_json()["error"]
        f, _ = world.fields()
        assert f["phase"] == "peer"
        assert f["turn"] == COLLEAGUE

    def test_T7_approve_then_commented_still_advances(self, world):
        world.gh.reviews = [rv("R1", "example-gh-colleague", "APPROVED", ts="2026-10-01T10:00:00Z"),
                            rv("R2", "example-gh-colleague", "COMMENTED", ts="2026-10-01T11:00:00Z")]
        world.seed()
        assert world.advance().status_code == 201

    def test_T12_mapped_login_is_the_pr_author(self, world):
        world.gh.pr_author = "Example-GH-Colleague"
        world.seed()
        resp = world.advance()
        assert resp.status_code == 422
        assert "self_review" in resp.get_json()["error"]

    def test_D4_approval_unknown_refuses_not_advisory(self, world):
        world.gh.review_mode = "nonzero"
        world.seed()
        resp = world.advance()
        assert resp.status_code == 422
        assert "unknown" in resp.get_json()["error"]
        f, _ = world.fields()
        assert f["phase"] == "peer"

    def test_D2_limit_pinned_reassigned_colleague_who_approved_passes(self, world):
        """The gate proves an authenticated non-author approval by a MAPPED
        account on the current head; it does NOT prove the ASSIGNED colleague
        approved. `colleague` is caller-declared and mutable, so reassigning it
        to a mapped account that already approved lets the advance through.
        Pinned so the stated limit stays true to the code (and visible)."""
        world.gh.reviews = [rv("R1", "example-gh-other", "APPROVED")]
        world.seed()
        assert world.advance().status_code == 422  # assigned colleague has not approved
        _set_colleague(world.store, world.allocator, "PR-9001", agent=COLLEAGUE, colleague=OTHER)
        assert world.advance().status_code == 201


# ---------------------------------------------------------------------------
# T21 / T22 -- existing gates preserved on the new path
# ---------------------------------------------------------------------------

class TestExistingGatesPreserved:
    def test_T21_unqualified_anchor_refused_with_todays_text(self, world):
        # Round 6 (D1): with a MAPPED prefix an unqualified anchor now advances (the
        # repo is derived from the admin map; pinned in test_baton_anchor_round6).
        # The #1918 refusal still fires where nothing can derive a repo: an
        # UNMAPPED prefix.
        world.seed("ZZ-9001", github="pr/9001")
        resp = world.advance("ZZ-9001")
        assert resp.status_code == 422
        assert "repo-qualified" in resp.get_json()["error"]
        assert "baton anchor ZZ-9001 --github pr/<owner>/<repo>/9001" in resp.get_json()["error"]
        assert world.gh.calls == []

    def test_T21_unqualified_anchor_refused_with_todays_text_when_no_map(self, world_nomap):
        world_nomap.seed(github="pr/9001")
        resp = world_nomap.advance()
        assert resp.status_code == 422
        assert "repo-qualified" in resp.get_json()["error"]
        assert "baton anchor PR-9001 --github pr/<owner>/<repo>/9001" in resp.get_json()["error"]
        assert world_nomap.gh.calls == []

    def test_T21_ci_red_refused(self, world):
        world.gh.ci = "red"
        world.seed()
        resp = world.advance()
        assert resp.status_code == 422
        assert "CI is red" in resp.get_json()["error"]

    def test_T21_ci_pending_refused(self, world):
        world.gh.ci = "pending"
        world.seed()
        resp = world.advance()
        assert resp.status_code == 422
        assert "CI is pending" in resp.get_json()["error"]

    def test_T21_ci_unknown_warns_and_proceeds_and_is_reported_separately(self, world):
        world.gh.ci = "unknown"
        world.seed()
        resp = world.advance()
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["ci"]["state"] == "unknown"
        assert body["approval"]["state"] == "approved_current"

    def test_T21_ci_unknown_does_not_rescue_a_missing_approval(self, world):
        world.gh.ci = "unknown"
        world.gh.reviews = []
        world.seed()
        assert world.advance().status_code == 422

    def test_T22_bunch_baton_refuses_advance(self, world):
        world.seed("pool-9001-bunch", github="project/5", colleague=None, phase="claimed")
        resp = world.advance("pool-9001-bunch")
        assert resp.status_code == 422
        f, _ = world.fields("pool-9001-bunch")
        assert f["phase"] == "claimed"

    def test_unanchored_baton_refuses(self, world):
        world.seed(github=None)
        assert world.advance().status_code == 422

    def test_closed_baton_refuses(self, world):
        world.seed(status="merged")
        assert world.advance().status_code == 422

    def test_to_agent_must_be_a_participant_or_the_sentinel(self, world):
        world.seed()
        assert world.advance(to="example-agent-stranger").status_code == 422

    def test_to_agent_cannot_be_the_colleague(self, world):
        world.seed(turn=AUTHOR)
        assert world.advance(to=COLLEAGUE, agent=AUTHOR).status_code == 422

    def test_unknown_project_is_404(self, world):
        assert world.advance("PR-404404").status_code == 404

    def test_missing_fields_are_400(self, world):
        world.seed()
        r = world.client.post("/api/projects/PR-9001/advance", json={"agent": COLLEAGUE})
        assert r.status_code == 400
        r = world.client.post("/api/projects/PR-9001/advance", json={"to_agent": SEAT})
        assert r.status_code == 400

    def test_fairy_phase_baton_refuses_naming_the_actual_phase(self, world):
        world.seed(phase="fairy")
        resp = world.advance()
        assert resp.status_code == 409
        assert "fairy" in resp.get_json()["error"]

    def test_gate_module_unavailable_fails_closed(self, world, monkeypatch):
        monkeypatch.setattr(server_mod, "_BATON_GATE_MODULE_CACHE", [None])
        world.seed()
        resp = world.advance()
        assert resp.status_code == 422
        assert "gate unavailable" in resp.get_json()["error"]

    def test_no_force_or_skip_field_is_honored(self, world):
        world.gh.reviews = []
        world.seed()
        for extra in ({"force": True}, {"skip_gate": True}, {"allow_unknown": True}):
            r = world.client.post("/api/projects/PR-9001/advance",
                                  json={"agent": COLLEAGUE, "to_agent": SEAT, "reason": "x", **extra})
            assert r.status_code == 422, extra
        f, _ = world.fields()
        assert f["phase"] == "peer"


# ---------------------------------------------------------------------------
# T18 / T17 / T19 -- the check-and-set write
# ---------------------------------------------------------------------------

class TestCheckAndSetWrite:
    def test_T18_second_sequential_advance_is_a_noop(self, world):
        world.seed()
        assert world.advance().status_code == 201
        seq_after_first = world.allocator.current()
        log_before = world.store.read_project("PR-9001")
        resp = world.advance()
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["advanced"] is False
        assert "already merge" in body["reason"]
        assert world.allocator.current() == seq_after_first  # no seq consumed
        assert world.store.read_project("PR-9001") == log_before  # no log line, no rewrite
        got = build_updates(world.store, world.allocator, SEAT, since=seq_after_first)
        assert [u for u in got["updates"] if u["kind"] == "baton"] == []  # no wake

    def test_T18_noop_does_not_need_github(self, world):
        world.seed()
        assert world.advance().status_code == 201
        calls_before = len(world.gh.calls)
        assert world.advance().status_code == 200
        assert len(world.gh.calls) == calls_before

    def test_T17_two_simultaneous_advances_make_exactly_one_transition(self, world):
        world.seed()
        seq_before = world.allocator.current()
        world.gh.barrier = threading.Barrier(2)  # both pass the GitHub check, then race the write
        results = []

        def go():
            c = world.app.test_client()
            results.append(world.advance(client=c))

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert sorted(r.status_code for r in results) == [200, 201]
        assert world.allocator.current() == seq_before + 1  # exactly ONE seq
        assert len(world.log_lines(f"{COLLEAGUE} → {SEAT}")) == 1  # one log line
        got = build_updates(world.store, world.allocator, SEAT, since=seq_before)
        assert len([u for u in got["updates"] if u["kind"] == "baton"]) == 1  # one wake

    def test_T19_phase_changed_between_check_and_write_refuses_with_actual_phase(self, world):
        world.seed()

        def mutate():
            _flip(world.store, world.allocator, "PR-9001", to_agent=AUTHOR,
                  reason="regress", by_agent="x", transition_phase="fairy")

        world.gh.on_review_query = mutate
        resp = world.advance()
        assert resp.status_code == 409
        assert "fairy" in resp.get_json()["error"]
        f, _ = world.fields()
        assert f["phase"] == "fairy"
        assert f["turn"] == AUTHOR

    def test_T19_anchor_changed_between_check_and_write_refuses(self, world):
        world.seed()
        from forum.coordination import anchor as _anchor

        def mutate():
            _anchor(world.store, world.allocator, "PR-9001", github_anchor=f"pr/{REPO}/7777")

        world.gh.on_review_query = mutate
        resp = world.advance()
        assert resp.status_code == 409
        f, _ = world.fields()
        assert f["phase"] == "peer"

    def test_T19_colleague_changed_between_check_and_write_refuses(self, world):
        world.seed()

        def mutate():
            _set_colleague(world.store, world.allocator, "PR-9001", agent=COLLEAGUE, colleague=OTHER)

        world.gh.on_review_query = mutate
        resp = world.advance()
        assert resp.status_code == 409
        f, _ = world.fields()
        assert f["phase"] == "peer"

    def test_github_check_runs_before_the_allocator_lock(self, world):
        """No network call under the allocator lock (C3): while gh is being
        called, another writer must be able to take the lock."""
        world.seed()
        seen = {}

        def probe():
            got = world.allocator._lock.acquire(blocking=False)
            seen["free"] = got
            if got:
                world.allocator._lock.release()

        world.gh.on_review_query = probe
        assert world.advance().status_code == 201
        assert seen.get("free") is True


# ---------------------------------------------------------------------------
# T20 -- D1: one gate, two spellings
# ---------------------------------------------------------------------------

class TestFlipIsTheSameGate:
    @pytest.mark.parametrize(
        "name, reviews",
        [
            ("missing", []),
            ("unmapped", [rv("R1", "random-stranger", "APPROVED")]),
            ("other-mapped-login", [rv("R1", "example-gh-other", "APPROVED")]),
            ("changes-requested",
             [rv("R1", "example-gh-colleague", "APPROVED", ts="2026-10-01T10:00:00Z"),
              rv("R2", "example-gh-colleague", "CHANGES_REQUESTED", ts="2026-10-01T11:00:00Z")]),
            ("stale", [rv("R1", "example-gh-colleague", "APPROVED", oid=OLD)]),
        ],
    )
    def test_T20_raw_flip_to_merge_seat_refused_identically_to_advance(self, world, name, reviews):
        world.gh.reviews = reviews
        world.seed("PR-9001")
        world.seed("PR-9002", github=f"pr/{REPO}/9002")
        adv = world.advance("PR-9001")
        flp = world.flip("PR-9002")
        assert adv.status_code == flp.status_code == 422
        assert adv.get_json()["error"].replace("9001", "N") == flp.get_json()["error"].replace("9002", "N")
        f, _ = world.fields("PR-9002")
        assert f["phase"] == "peer"
        assert f["turn"] == COLLEAGUE

    def test_T20_raw_flip_with_transition_phase_merge_to_a_non_seat_target_is_gated(self, world):
        world.gh.reviews = []
        world.seed()
        resp = world.flip(to=AUTHOR, transition_phase="merge")
        assert resp.status_code == 422
        f, _ = world.fields()
        assert f["phase"] == "peer"

    def test_T20_raw_flip_with_conversion_phase_merge_to_a_non_seat_target_is_gated(self, world):
        world.gh.reviews = []
        world.seed(phase=None)
        resp = world.flip(to=AUTHOR, phase="merge")
        assert resp.status_code == 422
        f, _ = world.fields()
        assert not f.get("phase")

    def test_T20_passing_case_still_works_via_flip_spelling(self, world):
        world.seed()
        resp = world.flip()
        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["turn"] == SEAT
        f, _ = world.fields()
        assert f["phase"] == "merge" and f["turn"] == SEAT

    def test_T20_both_spellings_leave_the_same_baton_shape(self, world):
        world.seed("PR-9001")
        world.seed("PR-9002", github=f"pr/{REPO}/9002")
        assert world.advance("PR-9001").status_code == 201
        assert world.flip("PR-9002").status_code == 201
        a, _ = world.fields("PR-9001")
        b, _ = world.fields("PR-9002")
        for key in ("phase", "turn", "status", "colleague", "author"):
            assert a.get(key) == b.get(key), key
        assert world.log_lines("(transition)", "PR-9001")
        assert world.log_lines("(transition)", "PR-9002")

    def test_flip_to_the_colleague_and_back_to_the_author_stay_ungated(self, world):
        world.gh.reviews = []
        world.gh.ci = "red"
        world.seed(turn=AUTHOR)
        assert world.flip(to=COLLEAGUE, agent=AUTHOR, reason="please review").status_code == 201
        assert world.flip(to=AUTHOR, agent=COLLEAGUE, reason="one blocker").status_code == 201

    def test_flip_within_merge_phase_is_not_the_strict_edge(self, world):
        world.seed()
        assert world.advance().status_code == 201
        # From here only the OLD weak gate applies: any APPROVED review on the
        # head satisfies it (strict would refuse this unmapped account).
        world.gh.reviews = [rv("R1", "random-stranger", "APPROVED")]
        assert world.flip(to=AUTHOR, agent=SEAT, reason="needs a fix").status_code == 201
        assert world.flip(to=SEAT, agent=AUTHOR, reason="fixed").status_code == 201

    def test_single_agent_baton_without_colleague_keeps_the_old_flip_gate(self, world):
        # No colleague => no peer phase to cross; flip-to-maintainer is the
        # unchanged warn-and-proceed single-agent path. `advance` itself refuses.
        world.gh.reviews = []
        world.seed(colleague=None, participants=(AUTHOR,), turn=AUTHOR, phase="fairy")
        assert world.flip(to=SENTINEL, agent=AUTHOR, reason="ready").status_code == 201

    def test_fairy_phase_with_a_colleague_cannot_skip_peer_by_flip(self, world):
        world.gh.reviews = []
        world.seed(phase="fairy", turn=AUTHOR)
        resp = world.flip(to=SEAT, agent=AUTHOR)
        assert resp.status_code in (409, 422)
        f, _ = world.fields()
        assert f["phase"] == "fairy"

    # --- #3914: the fairy-phase 409 names the way out ------------------------
    # Contract (message change only, status stays 409): in the strict
    # peer->merge path, phase 'fairy' + a colleague => today's first sentence
    # plus a hint naming the baton id and the colleague; fairy without a
    # colleague, and any other non-peer phase => today's text exactly.
    # Routes reaching the branch: POST .../advance and a POST .../flip to the
    # merge seat on a colleague-bearing baton (both go through _advance_gate).

    def _first_sentence(self, pid, phase):
        return (f"'{pid}' is in phase {phase!r}, not 'peer'; the peer->merge "
                "advance applies only to a baton in the peer phase.")

    def test_3914_fairy_with_colleague_flip_to_seat_409_names_the_way_out(self, world):
        # turn (holder) deliberately differs from the colleague.
        world.seed(phase="fairy", turn=AUTHOR)
        resp = world.flip(to=SEAT, agent=AUTHOR)
        assert resp.status_code == 409
        err = resp.get_json()["error"]
        assert err.startswith(self._first_sentence("PR-9001", "fairy"))
        assert err == (self._first_sentence("PR-9001", "fairy") + " "
                       f"To reach peer, flip it to its colleague first (baton flip PR-9001 "
                       f"{COLLEAGUE}), which writes the fairy->peer transition; then advance.")

    def test_3914_fairy_with_colleague_advance_route_gives_the_same_409(self, world):
        world.seed(phase="fairy", turn=AUTHOR)
        via_advance = world.advance(agent=AUTHOR)
        via_flip = world.flip(to=SEAT, agent=AUTHOR)
        assert via_advance.status_code == via_flip.status_code == 409
        assert via_advance.get_json() == via_flip.get_json()
        assert f"baton flip PR-9001 {COLLEAGUE})" in via_advance.get_json()["error"]

    def test_3914_fairy_without_colleague_409_is_todays_text_exactly(self, world):
        world.seed(colleague=None, participants=(AUTHOR, SEAT), turn=AUTHOR, phase="fairy")
        resp = world.advance(agent=AUTHOR)
        assert resp.status_code == 409
        err = resp.get_json()["error"]
        assert err == self._first_sentence("PR-9001", "fairy")
        assert "To reach peer" not in err

    def test_3914_other_non_peer_phase_keeps_todays_text_exactly(self, world):
        # An explicit non-fairy, non-peer, non-merge phase genuinely reaches the
        # branch (cur_phase is the frontmatter value verbatim). Even with a colleague.
        world.seed(phase="pool", turn=AUTHOR)
        resp = world.advance(agent=AUTHOR)
        assert resp.status_code == 409
        assert resp.get_json()["error"] == self._first_sentence("PR-9001", "pool")


# ---------------------------------------------------------------------------
# C5 -- lock-only context on SeqAllocator
# ---------------------------------------------------------------------------

class TestSeqLockedContext:
    def test_locked_without_claim_consumes_no_seq(self):
        a = SeqAllocator()
        with a.locked() as claim:
            pass
        assert a.current() == 0
        with a.allocate() as seq:
            assert seq == 1

    def test_claim_assigns_next_seq_and_publishes_after_exit(self):
        a = SeqAllocator()
        with a.allocate():
            pass
        with a.locked() as claim:
            seq = claim()
            assert seq == 2
            assert a.current() == 1  # not published until the body returns
        assert a.current() == 2

    def test_exception_in_body_does_not_publish_and_releases_lock(self):
        a = SeqAllocator()
        with pytest.raises(RuntimeError):
            with a.locked() as claim:
                claim()
                raise RuntimeError("write failed")
        assert a.current() == 0
        with a.allocate() as seq:  # lock was released
            assert seq == 2  # the failed seq is a harmless hole

    def test_second_claim_in_one_context_is_an_error(self):
        a = SeqAllocator()
        with pytest.raises(RuntimeError):
            with a.locked() as claim:
                claim()
                claim()

    def test_locked_excludes_allocate(self):
        a = SeqAllocator()
        got = []
        with a.locked():
            t = threading.Thread(target=lambda: got.append(a._lock.acquire(blocking=False)))
            t.start()
            t.join()
        assert got == [False]


# ---------------------------------------------------------------------------
# D3 -- start option / env wiring; no default path
# ---------------------------------------------------------------------------

class TestStartOption:
    def test_absent_option_and_env_means_none(self):
        ns = server_mod._parse_args([], environ={})
        assert ns.baton_login_map is None
        assert ns.baton_login_map_admin_uid == []

    def test_env_supplies_the_path(self):
        ns = server_mod._parse_args([], environ={"ENGRAM_BATON_LOGIN_MAP": "/some/where/m.json"})
        assert ns.baton_login_map == "/some/where/m.json"

    def test_flag_overrides_env(self):
        ns = server_mod._parse_args(
            ["--baton-login-map", "/flag/m.json"],
            environ={"ENGRAM_BATON_LOGIN_MAP": "/env/m.json"})
        assert ns.baton_login_map == "/flag/m.json"

    def test_admin_uid_flag_is_repeatable_and_integer(self):
        ns = server_mod._parse_args(
            ["--baton-login-map-admin-uid", "1001", "--baton-login-map-admin-uid", "1002"], environ={})
        assert ns.baton_login_map_admin_uid == [1001, 1002]

    def test_admin_uids_never_default_to_the_servers_own_uid(self):
        ns = server_mod._parse_args([], environ={})
        assert os.getuid() not in ns.baton_login_map_admin_uid or os.getuid() == 0
