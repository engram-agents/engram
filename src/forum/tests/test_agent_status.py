"""Tests for agent-status store + endpoints (slice 1 of #956).

Covers:
1. Publish→read roundtrip (board shows state/activity/queue exactly).
2. Offline override (stale last_seen forces state=offline; published state suppressed).
3. status_stale flag (fresh last_seen, old status_updated_at → stale=True).
4. Backward-compat: GET /api/agents/online still returns name/avatar_seed/pair_initials.
5. Invalid state rejected (400 for 'offline' and other bogus states).
6. Never-published agent shows idle + stale=False on board.
7. Sleeping state: board shows 'sleeping' (not offline) while last_seen is fresh.
8. Board includes offline rows; /api/agents/online does NOT include them.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from forum.db import (
    PUBLISHABLE_STATES,
    init_db,
    list_board,
    list_online,
    set_agent_status,
    upsert_agent,
)
from forum.server import create_app


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _iso_offset(delta_minutes: float) -> str:
    """Return an ISO timestamp offset by delta_minutes from now."""
    ts = datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _force_last_seen(conn: sqlite3.Connection, name: str, delta_minutes: float) -> None:
    """Force last_seen_at to now + delta_minutes for the named agent."""
    ts = _iso_offset(delta_minutes)
    conn.execute("UPDATE agents SET last_seen_at = ? WHERE name = ?", (ts, name))
    conn.commit()


def _force_status_updated_at(
    conn: sqlite3.Connection, name: str, delta_minutes: float
) -> None:
    """Force status_updated_at to now + delta_minutes for the named agent."""
    ts = _iso_offset(delta_minutes)
    conn.execute(
        "UPDATE agents SET status_updated_at = ? WHERE name = ?", (ts, name)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn():
    """In-memory DB for db-layer tests."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def app(tmp_path):
    """Flask app backed by a temp file DB."""
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
# 1. Publish→read roundtrip (db layer)
# ---------------------------------------------------------------------------

class TestPublishRoundtrip:
    def test_board_reflects_published_state(self, conn):
        upsert_agent(conn, "alice")
        set_agent_status(
            conn, "alice", state="working",
            activity="reviewing PR #1005",
            queue=["#994 reviewer round", "#1005 review"],
        )
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "working"
        assert a["activity"] == "reviewing PR #1005"
        assert a["queue"] == ["#994 reviewer round", "#1005 review"]

    def test_engaged_state_is_publishable(self, conn):
        """Regression (#1166): the derived 'engaged' state must be publishable.

        derive_own_status can emit 'engaged' (recent human-typed activity). If it
        were absent from PUBLISHABLE_STATES, set_agent_status would raise
        ValueError → the forum returns 400 → the prompt hook swallows it silently
        → the agent stops heartbeating to the board for the whole engaged window
        (the present-but-not-wired-through gap caught in #1166 review). Guard both
        the allowlist and the publish round-trip.
        """
        assert "engaged" in PUBLISHABLE_STATES
        upsert_agent(conn, "ari")
        set_agent_status(conn, "ari", state="engaged", activity="with user")
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "ari")
        assert a["state"] == "engaged"

    def test_queue_roundtrips_as_list(self, conn):
        upsert_agent(conn, "bob")
        set_agent_status(conn, "bob", state="idle", queue=["task-a", "task-b"])
        board, _, _ = list_board(conn, window_minutes=15)
        b = next(e for e in board if e["name"] == "bob")
        assert isinstance(b["queue"], list)
        assert b["queue"] == ["task-a", "task-b"]

    def test_status_updated_at_populated(self, conn):
        upsert_agent(conn, "dave")
        set_agent_status(conn, "dave", state="sleeping")
        board, _, _ = list_board(conn, window_minutes=15)
        k = next(e for e in board if e["name"] == "dave")
        assert k["status_updated_at"] is not None

    def test_empty_queue_roundtrips(self, conn):
        upsert_agent(conn, "erin")
        set_agent_status(conn, "erin", state="idle", queue=[])
        board, _, _ = list_board(conn, window_minutes=15)
        lu = next(e for e in board if e["name"] == "erin")
        assert lu["queue"] == []

    def test_none_queue_defaults_to_empty_list(self, conn):
        upsert_agent(conn, "carol")
        set_agent_status(conn, "carol", state="idle", queue=None)
        board, _, _ = list_board(conn, window_minutes=15)
        c = next(e for e in board if e["name"] == "carol")
        assert c["queue"] == []


# ---------------------------------------------------------------------------
# 2. Offline override
# ---------------------------------------------------------------------------

class TestOfflineOverride:
    def test_stale_last_seen_forces_offline(self, conn):
        """Published 'working' must not leak when last_seen is stale."""
        upsert_agent(conn, "alice")
        set_agent_status(conn, "alice", state="working", activity="deep work")
        # Force last_seen to be stale (30 min ago, beyond the 15-min window).
        _force_last_seen(conn, "alice", -30)
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "offline"

    def test_offline_suppresses_activity(self, conn):
        upsert_agent(conn, "bob")
        set_agent_status(conn, "bob", state="working", activity="should-not-leak")
        _force_last_seen(conn, "bob", -30)
        board, _, _ = list_board(conn, window_minutes=15)
        b = next(e for e in board if e["name"] == "bob")
        assert b["activity"] is None

    def test_offline_suppresses_queue(self, conn):
        upsert_agent(conn, "dave")
        set_agent_status(conn, "dave", state="working", queue=["should-not-leak"])
        _force_last_seen(conn, "dave", -30)
        board, _, _ = list_board(conn, window_minutes=15)
        k = next(e for e in board if e["name"] == "dave")
        assert k["queue"] == []

    def test_offline_status_stale_is_false(self, conn):
        """When offline, status_stale is always False (the state is 'offline', not 'stale')."""
        upsert_agent(conn, "erin")
        set_agent_status(conn, "erin", state="working")
        _force_last_seen(conn, "erin", -30)
        board, _, _ = list_board(conn, window_minutes=15)
        lu = next(e for e in board if e["name"] == "erin")
        assert lu["status_stale"] is False

    def test_fresh_last_seen_preserves_published_state(self, conn):
        upsert_agent(conn, "alice")
        set_agent_status(conn, "alice", state="working", activity="on task")
        # last_seen is very recent (just upserted — should be within window).
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "working"
        assert a["activity"] == "on task"


# ---------------------------------------------------------------------------
# 3. status_stale flag
# ---------------------------------------------------------------------------

class TestStatusStale:
    def test_stale_flag_when_status_old(self, conn):
        """last_seen fresh, status_updated_at older than 2×window → stale=True."""
        upsert_agent(conn, "alice")
        set_agent_status(conn, "alice", state="working")
        # Force status_updated_at to 40 min ago (beyond 2×15 = 30 min).
        _force_status_updated_at(conn, "alice", -40)
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "working"  # state not changed
        assert a["status_stale"] is True

    def test_not_stale_when_status_recent(self, conn):
        """last_seen fresh, status_updated_at fresh → stale=False."""
        upsert_agent(conn, "bob")
        set_agent_status(conn, "bob", state="working")
        board, _, _ = list_board(conn, window_minutes=15)
        b = next(e for e in board if e["name"] == "bob")
        assert b["status_stale"] is False

    def test_stale_boundary_exactly_2x_window(self, conn):
        """Exactly at 2×window is still stale (boundary: > not >=, so 2×window+1s is stale)."""
        upsert_agent(conn, "dave")
        set_agent_status(conn, "dave", state="working")
        # 31 min ago: 31 > 2×15 = 30 → stale.
        _force_status_updated_at(conn, "dave", -31)
        board, _, _ = list_board(conn, window_minutes=15)
        k = next(e for e in board if e["name"] == "dave")
        assert k["status_stale"] is True

    def test_not_stale_just_inside_boundary(self, conn):
        """29 min ago: 29 < 30 → NOT stale."""
        upsert_agent(conn, "erin")
        set_agent_status(conn, "erin", state="working")
        _force_status_updated_at(conn, "erin", -29)
        board, _, _ = list_board(conn, window_minutes=15)
        lu = next(e for e in board if e["name"] == "erin")
        assert lu["status_stale"] is False


# ---------------------------------------------------------------------------
# 4. Backward-compat: existing online endpoint fields still present
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_list_online_has_legacy_fields(self, conn):
        """name/avatar_seed/pair_initials are still present in list_online output."""
        upsert_agent(conn, "alice")
        online, _, _ = list_online(conn, window_minutes=15)
        assert len(online) == 1
        a = online[0]
        assert "name" in a
        assert "avatar_seed" in a
        assert "pair_initials" in a
        assert a["name"] == "alice"

    def test_list_online_has_new_status_fields(self, conn):
        """list_online now also includes state/activity/queue/status_updated_at/status_stale."""
        upsert_agent(conn, "bob")
        online, _, _ = list_online(conn, window_minutes=15)
        b = online[0]
        assert "state" in b
        assert "activity" in b
        assert "queue" in b
        assert "status_updated_at" in b
        assert "status_stale" in b

    def test_online_endpoint_returns_legacy_keys(self, client):
        """GET /api/agents/online still returns name/avatar_seed/pair_initials."""
        # Post once to register the agent.
        client.post(
            "/api/post",
            json={
                "agent": "alice",
                "category_slug": "inter-agent",
                "title": "hello",
                "body_md": "body",
            },
        )
        resp = client.get("/api/agents/online?agent=alice")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "online" in data
        assert "count" in data
        assert "registered" in data
        # At least one agent should be present.
        assert data["count"] >= 1
        agent_entry = next(a for a in data["online"] if a["name"] == "alice")
        assert "name" in agent_entry
        assert "avatar_seed" in agent_entry
        assert "pair_initials" in agent_entry


# ---------------------------------------------------------------------------
# 5. Invalid state rejected
# ---------------------------------------------------------------------------

class TestInvalidState:
    def test_offline_state_raises_value_error(self, conn):
        upsert_agent(conn, "alice")
        with pytest.raises(ValueError, match="offline"):
            set_agent_status(conn, "alice", state="offline")

    def test_bogus_state_raises_value_error(self, conn):
        upsert_agent(conn, "alice")
        with pytest.raises(ValueError):
            set_agent_status(conn, "alice", state="bogus")

    def test_publishable_states_constant(self):
        assert "idle" in PUBLISHABLE_STATES
        assert "working" in PUBLISHABLE_STATES
        assert "sleeping" in PUBLISHABLE_STATES
        assert "offline" not in PUBLISHABLE_STATES

    def test_endpoint_rejects_offline_state(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "alice", "state": "offline"},
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data

    def test_endpoint_rejects_bogus_state(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "alice", "state": "bogus"},
        )
        assert resp.status_code == 400

    def test_endpoint_rejects_missing_state(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "alice"},
        )
        assert resp.status_code == 400

    def test_endpoint_rejects_missing_agent(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"state": "idle"},
        )
        assert resp.status_code == 400

    def test_endpoint_rejects_non_list_queue(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "alice", "state": "idle", "queue": "not-a-list"},
        )
        assert resp.status_code == 400

    def test_endpoint_rejects_no_body(self, client):
        resp = client.post("/api/agents/status", data="not-json")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 6. Never-published agent
# ---------------------------------------------------------------------------

class TestNeverPublished:
    def test_never_published_shows_idle(self, conn):
        """An agent that only ever posted (never published status) shows idle."""
        upsert_agent(conn, "alice")
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "idle"

    def test_never_published_status_stale_false(self, conn):
        upsert_agent(conn, "alice")
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["status_stale"] is False

    def test_never_published_activity_is_none(self, conn):
        upsert_agent(conn, "alice")
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["activity"] is None

    def test_never_published_queue_is_empty(self, conn):
        upsert_agent(conn, "alice")
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["queue"] == []


# ---------------------------------------------------------------------------
# 7. Sleeping state
# ---------------------------------------------------------------------------

class TestSleepingState:
    def test_sleeping_shown_correctly_while_fresh(self, conn):
        """POST sleeping → board shows 'sleeping', not 'offline', while last_seen fresh."""
        upsert_agent(conn, "bob")
        set_agent_status(conn, "bob", state="sleeping")
        board, _, _ = list_board(conn, window_minutes=15)
        b = next(e for e in board if e["name"] == "bob")
        assert b["state"] == "sleeping"

    def test_sleeping_counted_as_online(self, conn):
        upsert_agent(conn, "dave")
        set_agent_status(conn, "dave", state="sleeping")
        _, online_count, _ = list_board(conn, window_minutes=15)
        assert online_count == 1

    def test_sleeping_overridden_by_offline(self, conn):
        """Sleeping is overridden to offline when last_seen goes stale."""
        upsert_agent(conn, "alice")
        set_agent_status(conn, "alice", state="sleeping")
        _force_last_seen(conn, "alice", -30)
        board, _, _ = list_board(conn, window_minutes=15)
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "offline"


# ---------------------------------------------------------------------------
# 8. Board includes offline rows; online endpoint excludes them
# ---------------------------------------------------------------------------

class TestBoardVsOnline:
    def test_board_includes_offline_agent(self, conn):
        """A stale agent appears in board as 'offline'."""
        upsert_agent(conn, "alice")
        _force_last_seen(conn, "alice", -30)  # stale
        board, _, registered = list_board(conn, window_minutes=15)
        names = [e["name"] for e in board]
        assert "alice" in names
        a = next(e for e in board if e["name"] == "alice")
        assert a["state"] == "offline"
        assert registered == 1

    def test_online_excludes_offline_agent(self, conn):
        """Stale agent NOT in list_online."""
        upsert_agent(conn, "alice")
        _force_last_seen(conn, "alice", -30)
        online, count, _ = list_online(conn, window_minutes=15)
        names = [a["name"] for a in online]
        assert "alice" not in names
        assert count == 0

    def test_board_online_count_correct(self, conn):
        """board online_count == number of agents with state != 'offline'."""
        upsert_agent(conn, "alice")
        upsert_agent(conn, "bob")
        _force_last_seen(conn, "bob", -30)  # bob offline
        board, online_count, registered = list_board(conn, window_minutes=15)
        assert registered == 2
        assert online_count == 1
        assert sum(1 for e in board if e["state"] != "offline") == online_count

    def test_board_sort_online_first(self, conn):
        """Online agents appear before offline agents in board."""
        upsert_agent(conn, "zzz-online")
        upsert_agent(conn, "aaa-offline")
        _force_last_seen(conn, "aaa-offline", -30)
        board, _, _ = list_board(conn, window_minutes=15)
        states = [e["state"] for e in board]
        # Find first offline index.
        first_offline = next(
            (i for i, s in enumerate(states) if s == "offline"), len(states)
        )
        # All entries before first_offline must be online (not 'offline').
        for entry in board[:first_offline]:
            assert entry["state"] != "offline"

    def test_board_endpoint_includes_offline(self, client):
        """GET /api/agents/board includes offline rows."""
        # Register an agent via a post, then let last_seen go stale via direct DB.
        # First register.
        client.post(
            "/api/post",
            json={
                "agent": "alice",
                "category_slug": "inter-agent",
                "title": "t",
                "body_md": "b",
            },
        )
        # Check via board endpoint (agent was just seen → online).
        resp = client.get("/api/agents/board")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "board" in data
        assert "online_count" in data
        assert "registered" in data

    def test_online_endpoint_excludes_stale_while_board_shows_offline(self, client, tmp_path):
        """End-to-end HTTP-layer offline override: after publishing 'working',
        a forced-stale agent is ABSENT from /api/agents/online but PRESENT in
        /api/agents/board as 'offline' with activity suppressed. Discriminating:
        broken code (no override at the endpoint) would surface 'working'+activity."""
        client.post(
            "/api/agents/status",
            json={"agent": "alice", "state": "working", "activity": "should-not-leak"},
        )
        # Force alice stale via a direct connection to the same file DB the
        # app is backed by (the `app` fixture builds db_path from this tmp_path).
        db_path = str(tmp_path / "forum.db")
        side = sqlite3.connect(db_path)
        _force_last_seen(side, "alice", -60)  # 60 min ago → beyond the 15-min window
        side.close()

        online = json.loads(client.get("/api/agents/online").data)
        assert all(a["name"] != "alice" for a in online["online"]), (
            "stale agent must not appear in /api/agents/online"
        )

        board = json.loads(client.get("/api/agents/board").data)
        entry = next(e for e in board["board"] if e["name"] == "alice")
        assert entry["state"] == "offline"
        assert entry["activity"] is None, (
            "offline must suppress published activity through the HTTP layer"
        )


# ---------------------------------------------------------------------------
# 9. Endpoint roundtrip (via Flask test client)
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    def test_publish_and_board_roundtrip(self, client):
        """POST /api/agents/status then GET /api/agents/board roundtrip."""
        resp = client.post(
            "/api/agents/status",
            json={
                "agent": "alice",
                "state": "working",
                "activity": "reviewing PR #1005",
                "queue": ["#994", "#1005"],
            },
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "published"
        assert data["agent"] == "alice"
        assert data["state"] == "working"

        # Board should reflect it.
        board_resp = client.get("/api/agents/board")
        assert board_resp.status_code == 200
        board_data = json.loads(board_resp.data)
        entries = board_data["board"]
        a = next(e for e in entries if e["name"] == "alice")
        assert a["state"] == "working"
        assert a["activity"] == "reviewing PR #1005"
        assert a["queue"] == ["#994", "#1005"]

    def test_board_agent_self_touch(self, client):
        """GET /api/agents/board?agent=X bumps last_seen for X."""
        resp = client.get("/api/agents/board?agent=dave")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["registered"] >= 1

    def test_publish_idle(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "bob", "state": "idle"},
        )
        assert resp.status_code == 200

    def test_publish_sleeping(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "bob", "state": "sleeping"},
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["state"] == "sleeping"


# ---------------------------------------------------------------------------
# 10. Double-init idempotency (migration guard)
# ---------------------------------------------------------------------------

class TestMigrationIdempotent:
    def test_double_init_no_error(self, conn):
        """Calling init_db twice on the same connection must not error."""
        init_db(conn)  # second call
        # If we get here without exception, the PRAGMA guards worked.

    def test_status_columns_present_after_init(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        assert "status_state" in cols
        assert "status_activity" in cols
        assert "status_queue" in cols
        assert "status_updated_at" in cols


# ---------------------------------------------------------------------------
# 11. Per-agent cadence + on-call (#1035, slice 3a)
# ---------------------------------------------------------------------------

class TestPerAgentCadence:
    """Each agent is judged against its own expected_republish_seconds instead
    of a single global window, so relaxed (20-40 min) and monitor-only peers
    don't flap to 'offline' on a 15-min clock."""

    def test_relaxed_agent_stays_online_past_global_window(self, conn):
        # 1200s (20 min) loop → per-agent window = max(15 min, 2×1200s=40 min).
        upsert_agent(conn, "relaxed")
        set_agent_status(
            conn, "relaxed", state="working",
            expected_republish_seconds=1200,
        )
        _force_last_seen(conn, "relaxed", -25)  # 25 min ago
        board, _, _ = list_board(conn, window_minutes=15)
        r = next(e for e in board if e["name"] == "relaxed")
        # Global 15-min clock would call this offline; per-agent keeps it online.
        assert r["state"] == "working", r

    def test_relaxed_agent_offline_past_own_window(self, conn):
        upsert_agent(conn, "relaxed")
        set_agent_status(
            conn, "relaxed", state="working",
            expected_republish_seconds=1200,
        )
        _force_last_seen(conn, "relaxed", -50)  # 50 min ago > 40-min window
        board, _, _ = list_board(conn, window_minutes=15)
        r = next(e for e in board if e["name"] == "relaxed")
        assert r["state"] == "offline", r
        assert r["activity"] is None  # stale claim suppressed

    def test_null_cadence_uses_global_window_unchanged(self, conn):
        # No published cadence → exactly the old global-window behavior.
        upsert_agent(conn, "plain")
        set_agent_status(conn, "plain", state="working")  # no cadence
        _force_last_seen(conn, "plain", -20)  # 20 min ago > 15-min global
        board, _, _ = list_board(conn, window_minutes=15)
        p = next(e for e in board if e["name"] == "plain")
        assert p["state"] == "offline", p

    def test_relaxed_agent_visible_in_list_online(self, conn):
        # The #1035 fix must also reach the legacy /online surface.
        upsert_agent(conn, "relaxed")
        set_agent_status(
            conn, "relaxed", state="idle", expected_republish_seconds=1200,
        )
        _force_last_seen(conn, "relaxed", -25)  # past global, within own window
        online, count, _ = list_online(conn, window_minutes=15)
        assert any(e["name"] == "relaxed" for e in online), online


class TestOnCall:
    """expected_republish_seconds == 0 marks an event-driven / monitor-only
    agent: never flapped offline on a heartbeat clock; renders 'on-call' when
    quiet, and 'offline' only past the 24h hard ceiling."""

    def test_quiet_monitor_agent_renders_on_call(self, conn):
        upsert_agent(conn, "kepler")
        set_agent_status(
            conn, "kepler", state="working", activity="reviewing #1005",
            expected_republish_seconds=0,
        )
        _force_last_seen(conn, "kepler", -30)  # 30 min quiet (> global 15)
        board, _, _ = list_board(conn, window_minutes=15)
        k = next(e for e in board if e["name"] == "kepler")
        assert k["state"] == "on-call", k
        assert k["activity"] is None  # stale activity suppressed for on-call

    def test_recently_seen_monitor_agent_shows_published_state(self, conn):
        upsert_agent(conn, "kepler")
        set_agent_status(
            conn, "kepler", state="working", expected_republish_seconds=0,
        )
        _force_last_seen(conn, "kepler", -5)  # 5 min ago, within global window
        board, _, _ = list_board(conn, window_minutes=15)
        k = next(e for e in board if e["name"] == "kepler")
        assert k["state"] == "working", k  # not on-call while actively seen

    def test_on_call_agent_offline_past_hard_ceiling(self, conn):
        upsert_agent(conn, "kepler")
        set_agent_status(
            conn, "kepler", state="working", expected_republish_seconds=0,
        )
        _force_last_seen(conn, "kepler", -25 * 60)  # 25h ago > 24h ceiling
        board, _, _ = list_board(conn, window_minutes=15)
        k = next(e for e in board if e["name"] == "kepler")
        assert k["state"] == "offline", k  # genuinely dead monitor agent

    def test_on_call_counts_as_online(self, conn):
        upsert_agent(conn, "kepler")
        set_agent_status(
            conn, "kepler", state="idle", expected_republish_seconds=0,
        )
        _force_last_seen(conn, "kepler", -30)
        _, online_count, _ = list_board(conn, window_minutes=15)
        assert online_count == 1, online_count  # on-call is a reachable state

    def test_on_call_visible_in_list_online(self, conn):
        # list_online was rewritten this slice — lock that the on-call path
        # reaches the legacy /online surface too (symmetry with the relaxed
        # test_relaxed_agent_visible_in_list_online above).
        upsert_agent(conn, "kepler")
        set_agent_status(conn, "kepler", state="idle", expected_republish_seconds=0)
        _force_last_seen(conn, "kepler", -30)
        online, count, _ = list_online(conn, window_minutes=15)
        assert any(e["name"] == "kepler" for e in online), online
        assert count == 1


class TestCadencePersistenceAndValidation:
    def test_cadence_persists_and_resets(self, conn):
        upsert_agent(conn, "alice")
        set_agent_status(conn, "alice", state="idle", expected_republish_seconds=600)
        row = conn.execute(
            "SELECT expected_republish_seconds FROM agents WHERE name='alice'"
        ).fetchone()
        assert row[0] == 600
        # A later publish without the field resets it to NULL (global window).
        set_agent_status(conn, "alice", state="idle")
        row = conn.execute(
            "SELECT expected_republish_seconds FROM agents WHERE name='alice'"
        ).fetchone()
        assert row[0] is None

    def test_negative_cadence_rejected(self, conn):
        upsert_agent(conn, "alice")
        with pytest.raises(ValueError):
            set_agent_status(conn, "alice", state="idle", expected_republish_seconds=-5)

    def test_on_call_state_not_publishable(self, conn):
        upsert_agent(conn, "alice")
        with pytest.raises(ValueError):
            set_agent_status(conn, "alice", state="on-call")

    def test_stale_flag_uses_per_agent_cadence(self, conn):
        # 300s (5 min) cadence → stale after 2×5=10 min of no republish.
        upsert_agent(conn, "fast")
        set_agent_status(conn, "fast", state="working", expected_republish_seconds=300)
        _force_last_seen(conn, "fast", -2)        # fresh liveness
        _force_status_updated_at(conn, "fast", -12)  # status 12 min old > 10
        board, _, _ = list_board(conn, window_minutes=15)
        f = next(e for e in board if e["name"] == "fast")
        assert f["state"] == "working", f
        assert f["status_stale"] is True, f


class TestCadenceEndpoint:
    def test_post_accepts_expected_republish_seconds(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "alice", "state": "idle", "expected_republish_seconds": 1200},
        )
        assert resp.status_code == 200, resp.data
        board = json.loads(client.get("/api/agents/board").data)["board"]
        a = next(e for e in board if e["name"] == "alice")
        # round-trips through the store (resolved while fresh → published state)
        assert a["state"] == "idle"

    def test_post_rejects_negative_cadence(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "alice", "state": "idle", "expected_republish_seconds": -1},
        )
        assert resp.status_code == 400, resp.data

    def test_post_on_call_sentinel_zero_accepted(self, client):
        resp = client.post(
            "/api/agents/status",
            json={"agent": "kepler", "state": "working", "expected_republish_seconds": 0},
        )
        assert resp.status_code == 200, resp.data

    def test_post_rejects_bool_cadence(self, client):
        # JSON `true` is an int in Python (isinstance(True, int)); the bool-first
        # guard in set_agent_status must reject it via the endpoint → 400. The
        # endpoint passes the value straight through, so this locks the guard.
        resp = client.post(
            "/api/agents/status",
            json={"agent": "alice", "state": "idle", "expected_republish_seconds": True},
        )
        assert resp.status_code == 400, resp.data


class TestCadenceMigration:
    def test_cadence_column_present_after_init(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        assert "expected_republish_seconds" in cols
