"""Contract tests for engram-alpha#3488 -- merge() hardening (follow-ups from #3486).

Written BEFORE the implementation (contract-first).

Contract:
  H1  coordination.merge() raises AlreadyClosed when the baton's status is in
      CLOSED_STATUSES (every member, ``merged`` included -- same as close()).
      Same position as close(): under the lock, after not-found and the
      participant gate, before the anchor binding and before a seq is claimed.
      Nothing written, no seq consumed.
  H2  POST /merge on a closed baton answers 409 with /gc's message shape. On an
      ANCHORED baton the 409 comes BEFORE any gh call.
  H3  merge(require_participant=...) defaults to True (secure by default).
"""
from __future__ import annotations

import sqlite3

import pytest

from forum import board_projects
from forum.coordination import (
    AlreadyClosed, AnchorChanged, FileStore, NotAParticipant, ProjectNotFound,
    SeqAllocator, init as _init, merge as _merge_fn,
)
from forum.coordination import markdown as md
from forum.coordination.store_file import CLOSED_STATUSES
from forum.db import init_db
from forum.server import create_app

ANCHOR = "pr/acme/widgets/42"
OUTSIDER = "sweeper-not-listed"


def _seed(store, allocator, pid, github):
    _init(store, allocator, pid, title="t", status="in-progress", turn="ariadne",
          participants=["ariadne", "luria"], turn_reason="start",
          transition_phase="fairy", github=github)


def _force_status(store, allocator, pid, status):
    """Put a baton into ``status`` in place (no archive), as a crash between the
    write and the archive step would leave it."""
    raw = store.read_project(pid)
    store.write_project(pid, md.update_frontmatter(raw, {"status": status}),
                        seq=allocator.current())


@pytest.fixture
def parts(tmp_path):
    store = FileStore(tmp_path / "coord")
    allocator = SeqAllocator(recover=store.recover_max_seq)
    _seed(store, allocator, "PR-42", ANCHOR)
    _seed(store, allocator, "PR-43", None)
    return store, allocator


@pytest.fixture
def client(tmp_path, parts):
    db_path = str(tmp_path / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(tmp_path / "audit.jsonl"))
    app.config["TESTING"] = True
    app.config["COORD_STORE"], app.config["COORD_ALLOCATOR"] = parts
    return app.test_client()


@pytest.fixture
def gh_state(monkeypatch):
    box = {"state": "merged", "calls": []}

    def fake(pr_number, repo):
        box["calls"].append((pr_number, repo))
        return box["state"]

    monkeypatch.setattr(board_projects, "_gh_pr_state", fake)
    return box


def _snap(parts, pid):
    store, allocator = parts
    return store.read_project(pid), allocator.current()


CLOSED = sorted(CLOSED_STATUSES)


# ------------------------------------------------------ H1: merge() directly

class TestMergeRefusesClosedBaton:
    def test_closed_statuses_are_what_we_think(self):
        assert {"merged", "cancelled"} <= set(CLOSED_STATUSES)

    @pytest.mark.parametrize("status", CLOSED)
    def test_raises_already_closed_nothing_written_no_seq(self, parts, status):
        store, allocator = parts
        _force_status(store, allocator, "PR-43", status)
        before = _snap(parts, "PR-43")
        with pytest.raises(AlreadyClosed) as ei:
            _merge_fn(store, allocator, "PR-43", merged_by="luria")
        assert ei.value.status == status
        assert _snap(parts, "PR-43") == before          # bytes unchanged, seq unchanged
        assert store.read_project("PR-43") is not None  # not archived

    @pytest.mark.parametrize("status", CLOSED)
    def test_next_seq_not_burned(self, parts, status):
        store, allocator = parts
        _force_status(store, allocator, "PR-43", status)
        with pytest.raises(AlreadyClosed):
            _merge_fn(store, allocator, "PR-43", merged_by="luria")
        # the next successful write gets current()+1: the refusal claimed nothing
        nxt = allocator.current() + 1
        assert _merge_fn(store, allocator, "PR-42", merged_by="luria",
                         expected_anchor=ANCHOR) == nxt

    def test_status_match_is_case_and_space_insensitive(self, parts):
        store, allocator = parts
        _force_status(store, allocator, "PR-43", "Cancelled ")
        with pytest.raises(AlreadyClosed):
            _merge_fn(store, allocator, "PR-43", merged_by="luria")

    def test_not_found_is_checked_first(self, parts):
        store, allocator = parts
        with pytest.raises(ProjectNotFound):
            _merge_fn(store, allocator, "MISSING", merged_by="luria")

    def test_participant_gate_runs_before_closed_check(self, parts):
        # close() order: not-found, participant, closed. A non-participant on a
        # closed baton gets the participant refusal.
        store, allocator = parts
        _force_status(store, allocator, "PR-43", "cancelled")
        with pytest.raises(NotAParticipant):
            _merge_fn(store, allocator, "PR-43", merged_by=OUTSIDER)

    def test_closed_check_runs_before_anchor_binding(self, parts):
        store, allocator = parts
        _force_status(store, allocator, "PR-42", "cancelled")
        with pytest.raises(AlreadyClosed):
            _merge_fn(store, allocator, "PR-42", merged_by="luria",
                      expected_anchor="pr/other/repo/42")

    def test_active_baton_still_merges(self, parts):
        store, allocator = parts
        assert _merge_fn(store, allocator, "PR-43", merged_by="luria") > 0
        assert store.read_project("PR-43") is None


# --------------------------------------------------------- H2: the /merge route

class TestMergeRouteClosed:
    @pytest.mark.parametrize("status", CLOSED)
    def test_closed_anchored_is_409_and_no_gh_call(self, client, parts, gh_state, status):
        store, allocator = parts
        _force_status(store, allocator, "PR-42", status)
        before = _snap(parts, "PR-42")
        resp = client.post("/api/projects/PR-42/merge", json={"agent": "luria"})
        assert resp.status_code == 409
        assert resp.get_json()["error"] == (
            f"project PR-42 is already {status}; "
            "refusing to overwrite a closed status (reopen first)")
        assert gh_state["calls"] == []
        assert _snap(parts, "PR-42") == before

    @pytest.mark.parametrize("status", CLOSED)
    def test_closed_unanchored_is_409(self, client, parts, gh_state, status):
        store, allocator = parts
        _force_status(store, allocator, "PR-43", status)
        before = _snap(parts, "PR-43")
        resp = client.post("/api/projects/PR-43/merge", json={"agent": "luria"})
        assert resp.status_code == 409
        assert resp.get_json()["error"] == (
            f"project PR-43 is already {status}; "
            "refusing to overwrite a closed status (reopen first)")
        assert _snap(parts, "PR-43") == before

    def test_route_message_matches_gc(self, client, parts, gh_state):
        store, allocator = parts
        _force_status(store, allocator, "PR-43", "cancelled")
        m = client.post("/api/projects/PR-43/merge", json={"agent": "luria"})
        g = client.post("/api/projects/PR-43/gc",
                        json={"agent": "luria", "new_status": "merged", "reason": "r"})
        assert m.status_code == g.status_code == 409
        assert m.get_json()["error"] == g.get_json()["error"]

    def test_open_baton_still_merges(self, client, parts, gh_state):
        resp = client.post("/api/projects/PR-43/merge", json={"agent": "luria"})
        assert resp.status_code == 201


# ------------------------------------------------ H3: secure-by-default gate

class TestRequireParticipantDefault:
    def test_bare_merge_by_non_participant_raises(self, parts):
        store, allocator = parts
        before = _snap(parts, "PR-43")
        with pytest.raises(NotAParticipant):
            _merge_fn(store, allocator, "PR-43", merged_by=OUTSIDER)  # no kwarg
        assert _snap(parts, "PR-43") == before

    def test_bare_merge_by_participant_passes(self, parts):
        store, allocator = parts
        assert _merge_fn(store, allocator, "PR-43", merged_by="LURIA") > 0

    def test_explicit_false_opts_out(self, parts):
        store, allocator = parts
        assert _merge_fn(store, allocator, "PR-43", merged_by=OUTSIDER,
                         require_participant=False) > 0

    def test_signature_default_is_true(self):
        import inspect
        assert inspect.signature(_merge_fn).parameters["require_participant"].default is True
