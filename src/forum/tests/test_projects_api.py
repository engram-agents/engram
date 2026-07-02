"""Tests for /api/projects/* endpoints — uses a FileStore via tmp_path for real write coverage.

Route-layer concerns (validation, response shape, 503/400/404 guards). The
writer-fn logic itself is unit-tested in test_coordination_projects.py.
"""

import sqlite3
import pytest

from forum.coordination import SeqAllocator, init as _init
from forum.coordination.store_file import FileStore
from forum.db import init_db
from forum.server import create_app


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()

    store = FileStore(tmp_path / "coord")
    allocator = SeqAllocator(recover=store.recover_max_seq)
    # seed one baton for mutation tests
    _init(store, allocator, "PR-42",
          title="test pr", status="in-progress", turn="ariadne",
          participants=["ariadne", "luria"], turn_reason="start")

    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    application.config["COORD_STORE"] = store
    application.config["COORD_ALLOCATOR"] = allocator
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# GET /api/projects — list
# ---------------------------------------------------------------------------

class TestProjectsList:
    def test_returns_200(self, client):
        resp = client.get("/api/projects")
        assert resp.status_code == 200

    def test_returns_seeded_project(self, client):
        data = client.get("/api/projects").get_json()
        assert "projects" in data
        ids = [p["project_id"] for p in data["projects"]]
        assert "PR-42" in ids

    def test_project_fields_present(self, client):
        data = client.get("/api/projects").get_json()
        proj = next(p for p in data["projects"] if p["project_id"] == "PR-42")
        for field in ("project_id", "title", "status", "turn", "turn_since", "turn_reason", "participants", "seq", "github"):
            assert field in proj, f"missing field: {field}"

    def test_agent_filter(self, client):
        data = client.get("/api/projects?agent=ariadne").get_json()
        assert all("ariadne" in p["participants"] for p in data["projects"])

    def test_agent_filter_excludes_nonmember(self, client):
        data = client.get("/api/projects?agent=borges").get_json()
        # borges is not a participant in PR-42, so the list should be empty
        assert data["projects"] == []

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.get("/api/projects").status_code == 503


# ---------------------------------------------------------------------------
# GET /api/projects/<pid> — single project
# ---------------------------------------------------------------------------

class TestProjectShow:
    def test_returns_200_for_existing(self, client):
        resp = client.get("/api/projects/PR-42")
        assert resp.status_code == 200

    def test_returns_raw_content(self, client):
        data = client.get("/api/projects/PR-42").get_json()
        assert "raw" in data
        assert "PR-42" in data["raw"]

    def test_returns_project_id_in_response(self, client):
        data = client.get("/api/projects/PR-42").get_json()
        assert data["project_id"] == "PR-42"

    def test_returns_404_for_missing(self, client):
        resp = client.get("/api/projects/MISSING")
        assert resp.status_code == 404

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.get("/api/projects/PR-42").status_code == 503


# ---------------------------------------------------------------------------
# POST /api/projects — init
# ---------------------------------------------------------------------------

class TestProjectInit:
    def test_creates_new_project(self, client):
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-99",
            "title": "New Baton",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria", "ariadne"],
            "turn_reason": "start",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert "seq" in data
        assert data["project_id"] == "PR-99"

    def test_409_on_duplicate(self, client):
        # PR-42 already seeded
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-42",
            "title": "Dup",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria"],
            "turn_reason": "dup",
        })
        assert resp.status_code == 409

    def test_400_on_missing_fields(self, client):
        # missing project_id
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "title": "No ID",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria"],
            "turn_reason": "r",
        })
        assert resp.status_code == 400

    def test_400_on_invalid_agent(self, client):
        resp = client.post("/api/projects", json={
            "agent": "a+b",
            "project_id": "PR-100",
            "title": "t",
            "status": "in-progress",
            "turn": "luria",
            "participants": ["luria"],
            "turn_reason": "r",
        })
        assert resp.status_code == 400

    def test_participants_as_string(self, client):
        resp = client.post("/api/projects", json={
            "agent": "luria",
            "project_id": "PR-101",
            "title": "t",
            "status": "in-progress",
            "turn": "luria",
            "participants": "luria,ariadne",
            "turn_reason": "r",
        })
        assert resp.status_code == 201

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.post("/api/projects", json={"agent": "luria"}).status_code == 503


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/flip
# ---------------------------------------------------------------------------

class TestProjectFlip:
    def test_flip_updates_turn(self, client):
        resp = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
            "reason": "please review",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert "seq" in data

    def test_flip_returns_seq(self, client):
        data = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
            "reason": "r",
        }).get_json()
        assert data["seq"] >= 1

    def test_flip_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
            "reason": "r",
        })
        assert resp.status_code == 404

    def test_flip_400_missing_to_agent(self, client):
        resp = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "reason": "r",
        })
        assert resp.status_code == 400

    def test_flip_400_missing_reason(self, client):
        resp = client.post("/api/projects/PR-42/flip", json={
            "agent": "ariadne",
            "to_agent": "luria",
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/claim
# ---------------------------------------------------------------------------

class TestProjectClaim:
    def test_claim_returns_201(self, client):
        resp = client.post("/api/projects/PR-42/claim", json={
            "agent": "luria",
            "pool_sentinel": "lei",
        })
        assert resp.status_code == 201

    def test_claim_400_missing_pool_sentinel(self, client):
        resp = client.post("/api/projects/PR-42/claim", json={
            "agent": "luria",
        })
        assert resp.status_code == 400

    def test_claim_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/claim", json={
            "agent": "luria",
            "pool_sentinel": "lei",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/release
# ---------------------------------------------------------------------------

class TestProjectRelease:
    def test_release_returns_201(self, client):
        resp = client.post("/api/projects/PR-42/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
            "reason": "all done",
        })
        assert resp.status_code == 201

    def test_release_done_true(self, client):
        resp = client.post("/api/projects/PR-42/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
            "reason": "done",
            "done": True,
        })
        assert resp.status_code == 201
        # verify title was marked (done) via list endpoint
        data = client.get("/api/projects?active_only=false").get_json()
        proj = next((p for p in data["projects"] if p["project_id"] == "PR-42"), None)
        assert proj is not None
        assert "(done)" in proj["title"]

    def test_release_400_missing_reason(self, client):
        resp = client.post("/api/projects/PR-42/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
        })
        assert resp.status_code == 400

    def test_release_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/release", json={
            "agent": "ariadne",
            "pool_sentinel": "lei",
            "reason": "r",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/status — close/reopen dispatcher
# ---------------------------------------------------------------------------

class TestProjectStatus:
    def test_close_dispatches_correctly(self, client):
        resp = client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "new_status": "cancelled",
            "reason": "scope dropped",
        })
        assert resp.status_code == 201

    def test_reopen_dispatches_for_active_status(self, client):
        # first close it
        client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "new_status": "cancelled",
            "reason": "closed first",
        })
        # now reopen
        resp = client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "new_status": "in-progress",
            "reason": "reopened",
        })
        assert resp.status_code == 201

    def test_status_400_missing_new_status(self, client):
        resp = client.post("/api/projects/PR-42/status", json={
            "agent": "ariadne",
            "reason": "r",
        })
        assert resp.status_code == 400

    def test_status_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/status", json={
            "agent": "ariadne",
            "new_status": "merged",
            "reason": "r",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/rename
# ---------------------------------------------------------------------------

class TestProjectRename:
    def test_rename_returns_201(self, client):
        resp = client.post("/api/projects/PR-42/rename", json={
            "agent": "ariadne",
            "new_title": "Renamed Baton",
        })
        assert resp.status_code == 201

    def test_rename_400_missing_new_title(self, client):
        resp = client.post("/api/projects/PR-42/rename", json={
            "agent": "ariadne",
        })
        assert resp.status_code == 400

    def test_rename_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/rename", json={
            "agent": "ariadne",
            "new_title": "X",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/anchor
# ---------------------------------------------------------------------------

class TestProjectAnchor:
    def test_anchor_returns_201(self, client):
        resp = client.post("/api/projects/PR-42/anchor", json={
            "agent": "ariadne",
            "github": "pr/42",
        })
        assert resp.status_code == 201

    def test_anchor_400_missing_github(self, client):
        resp = client.post("/api/projects/PR-42/anchor", json={
            "agent": "ariadne",
        })
        assert resp.status_code == 400

    def test_anchor_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/anchor", json={
            "agent": "ariadne",
            "github": "pr/99",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/participants
# ---------------------------------------------------------------------------
# Seeded PR-42 participants: [ariadne, luria] (see the `app` fixture above).

class TestProjectAddParticipant:
    def test_add_participant_returns_201_and_added_true(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "ariadne",
            "participant": "borges",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["added"] is True
        assert "seq" in data

    def test_add_participant_idempotent_no_op_returns_added_false(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "ariadne",
            "participant": "ariadne",  # already a seeded participant
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["added"] is False

    def test_add_participant_403_when_agent_not_a_participant(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "casey",  # not in [ariadne, luria]
            "participant": "borges",
        })
        assert resp.status_code == 403

    def test_add_participant_400_missing_agent(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "participant": "borges",
        })
        assert resp.status_code == 400

    def test_add_participant_400_missing_participant(self, client):
        resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "ariadne",
        })
        assert resp.status_code == 400

    def test_add_participant_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/participants", json={
            "agent": "ariadne",
            "participant": "borges",
        })
        assert resp.status_code == 404

    def test_add_participant_persists_in_subsequent_read(self, client):
        """The added participant is visible on a later GET (persisted, not just echoed).

        This is the API-layer half of the flip-unblock regression guard — the
        CLI-level half (baton flip actually SUCCEEDS post-add, the check that
        matters since 'not a participant' is enforced client-side in
        cmd_flip) lives in tests/test_baton.py.
        """
        add_resp = client.post("/api/projects/PR-42/participants", json={
            "agent": "ariadne",
            "participant": "borges",
        })
        assert add_resp.status_code == 201

        show_resp = client.get("/api/projects/PR-42")
        assert show_resp.status_code == 200
        assert "borges" in show_resp.get_json()["raw"]


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/gc
# ---------------------------------------------------------------------------

class TestProjectGc:
    def test_gc_closes_with_new_status(self, client):
        resp = client.post("/api/projects/PR-42/gc", json={
            "agent": "ariadne",
            "new_status": "merged",
            "reason": "pr merged on gh",
        })
        assert resp.status_code == 201

    def test_gc_400_missing_new_status(self, client):
        resp = client.post("/api/projects/PR-42/gc", json={
            "agent": "ariadne",
            "reason": "r",
        })
        assert resp.status_code == 400

    def test_gc_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/gc", json={
            "agent": "ariadne",
            "new_status": "merged",
            "reason": "r",
        })
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/<pid>/merge
# ---------------------------------------------------------------------------

class TestProjectMerge:
    def test_merge_returns_201_and_seq(self, client):
        resp = client.post("/api/projects/PR-42/merge", json={
            "agent": "luria",
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert "seq" in data
        assert data["seq"] >= 1

    def test_merge_archives_baton(self, client, app):
        client.post("/api/projects/PR-42/merge", json={"agent": "luria"})
        # After merge, project should not appear in active list
        data = client.get("/api/projects").get_json()
        ids = [p["project_id"] for p in data["projects"]]
        assert "PR-42" not in ids

    def test_merge_forced(self, client):
        resp = client.post("/api/projects/PR-42/merge", json={
            "agent": "luria",
            "forced": True,
        })
        assert resp.status_code == 201

    def test_merge_400_missing_agent(self, client):
        resp = client.post("/api/projects/PR-42/merge", json={})
        assert resp.status_code == 400

    def test_merge_404_missing_project(self, client):
        resp = client.post("/api/projects/MISSING/merge", json={
            "agent": "luria",
        })
        assert resp.status_code == 404

    def test_merge_400_invalid_agent(self, client):
        resp = client.post("/api/projects/PR-42/merge", json={
            "agent": "a+b",
        })
        assert resp.status_code == 400

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.post("/api/projects/PR-42/merge", json={"agent": "luria"}).status_code == 503
