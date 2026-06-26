"""Tests for agent-name charset guard (#1468).

Covers:
- is_valid_agent_name: valid + invalid inputs, boundary lengths.
- upsert_agent: raises ForumInvalidAgentName on invalid names.
- ForumInvalidAgentName is a ForumBadRequest subclass.
- POST endpoints return 400 on invalid agent names.
- GET-path presence bumps silently skip invalid names (no 400, full response).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from forum import db
from forum.db import (
    ForumBadRequest,
    ForumInvalidAgentName,
    init_db,
    is_valid_agent_name,
    upsert_agent,
)
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    c = sqlite3.connect(db_path)
    init_db(c)
    c.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# is_valid_agent_name
# ---------------------------------------------------------------------------


class TestIsValidAgentName:
    def test_valid_names(self):
        for name in ("sol", "borges", "ariadne", "agent-sol", "a1b", "x", "mira"):
            assert is_valid_agent_name(name), f"{name!r} should be valid"

    def test_valid_underscore(self):
        assert is_valid_agent_name("agent_sol")
        assert is_valid_agent_name("a_b_c")

    def test_invalid_plus(self):
        assert not is_valid_agent_name("a+b")
        assert not is_valid_agent_name("sol+evil")

    def test_invalid_uppercase(self):
        assert not is_valid_agent_name("Sol")
        assert not is_valid_agent_name("BORGES")

    def test_invalid_space(self):
        assert not is_valid_agent_name("lei shi")
        assert not is_valid_agent_name("sol evil")

    def test_invalid_empty(self):
        assert not is_valid_agent_name("")

    def test_invalid_special_chars(self):
        for name in ("a@b", "a/b", "a.b", "a:b", "a#b"):
            assert not is_valid_agent_name(name), f"{name!r} should be invalid"

    def test_invalid_leading_special(self):
        assert not is_valid_agent_name("_sol")
        assert not is_valid_agent_name("-sol")

    def test_valid_max_length(self):
        # 63 chars (1 + 62) — exactly at the limit
        name = "a" + "b" * 62
        assert len(name) == 63
        assert is_valid_agent_name(name)

    def test_invalid_over_max_length(self):
        # 64 chars — one over
        name = "a" + "b" * 63
        assert len(name) == 64
        assert not is_valid_agent_name(name)

    def test_single_char(self):
        assert is_valid_agent_name("a")
        assert is_valid_agent_name("1")
        assert not is_valid_agent_name("_")


# ---------------------------------------------------------------------------
# upsert_agent charset guard
# ---------------------------------------------------------------------------


class TestUpsertAgentCharsetGuard:
    def test_valid_name_succeeds(self, conn):
        agent_id = upsert_agent(conn, "sol")
        assert isinstance(agent_id, int)
        assert agent_id > 0

    def test_invalid_name_raises(self, conn):
        with pytest.raises(ForumInvalidAgentName):
            upsert_agent(conn, "sol+evil")

    def test_uppercase_raises(self, conn):
        with pytest.raises(ForumInvalidAgentName):
            upsert_agent(conn, "Sol")

    def test_space_raises(self, conn):
        with pytest.raises(ForumInvalidAgentName):
            upsert_agent(conn, "lei shi")

    def test_exception_is_bad_request_subclass(self):
        assert issubclass(ForumInvalidAgentName, ForumBadRequest)

    def test_exception_message_contains_name(self, conn):
        with pytest.raises(ForumInvalidAgentName, match="sol\\+evil"):
            upsert_agent(conn, "sol+evil")


# ---------------------------------------------------------------------------
# POST /api/post — agent field validation
# ---------------------------------------------------------------------------


class TestPostEndpointCharsetGuard:
    def test_invalid_agent_returns_400(self, client):
        resp = client.post(
            "/api/post",
            json={
                "agent": "sol+evil",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "body",
            },
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "invalid agent name" in data["error"]

    def test_valid_agent_succeeds(self, client):
        resp = client.post(
            "/api/post",
            json={
                "agent": "sol",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "body",
            },
        )
        assert resp.status_code == 201

    def test_invalid_agent_in_thread_read_returns_400(self, client):
        # First create a thread with a valid agent
        resp = client.post(
            "/api/post",
            json={
                "agent": "sol",
                "category_slug": "inter-agent",
                "title": "T",
                "body_md": "body",
            },
        )
        assert resp.status_code == 201
        tid = json.loads(resp.data)["thread_id"]

        resp = client.post(
            f"/api/thread/{tid}/read",
            json={"agent": "sol+evil"},
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "invalid agent name" in data["error"]

    def test_invalid_agent_in_status_returns_400(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "sol+evil", "state": "idle"},
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "invalid agent name" in data["error"]


# ---------------------------------------------------------------------------
# GET /api/agent/<name>/inbox — URL path param
# ---------------------------------------------------------------------------


class TestInboxEndpointCharsetGuard:
    def test_invalid_name_in_url_returns_400(self, client):
        resp = client.get("/api/agent/sol+evil/inbox")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "invalid agent name" in data["error"]

    def test_valid_name_in_url_succeeds(self, client):
        resp = client.get("/api/agent/sol/inbox")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "inbox" in data
        assert "unread_all" in data


# ---------------------------------------------------------------------------
# GET-path presence bumps silently skip invalid names
# ---------------------------------------------------------------------------


class TestGetPathPresenceBumps:
    def test_threads_endpoint_ignores_invalid_agent(self, client):
        resp = client.get("/api/threads?agent=sol%2Bevil")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "threads" in data

    def test_agents_online_ignores_invalid_agent(self, client):
        resp = client.get("/api/agents/online?agent=sol%2Bevil")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "online" in data
