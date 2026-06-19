"""Tests for online-logic in forum/db.py — 15-minute window."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from forum.db import init_db, list_online, upsert_agent


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_db(c)
    yield c
    c.close()


def _set_last_seen(conn: sqlite3.Connection, name: str, delta_minutes: float) -> None:
    """Force last_seen_at to now +/- delta_minutes for the named agent."""
    ts = (
        datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)
    ).isoformat().replace("+00:00", "Z")
    conn.execute("UPDATE agents SET last_seen_at = ? WHERE name = ?", (ts, name))
    conn.commit()


class TestOnlineWindow:
    def test_recent_agent_appears_online(self, conn):
        """Agent last seen 5 min ago is online (within 15-min window)."""
        upsert_agent(conn, "agent-a")
        _set_last_seen(conn, "agent-a", -5)
        online, count, _ = list_online(conn, window_minutes=15)
        names = [a["name"] for a in online]
        assert "agent-a" in names

    def test_old_agent_not_online(self, conn):
        """Agent last seen 30 min ago is NOT online."""
        upsert_agent(conn, "agent-b")
        _set_last_seen(conn, "agent-b", -30)
        online, count, _ = list_online(conn, window_minutes=15)
        names = [a["name"] for a in online]
        assert "agent-b" not in names

    def test_boundary_inclusive(self, conn):
        """Agent last seen slightly inside the window (1 min ago) is online."""
        upsert_agent(conn, "agent-c")
        _set_last_seen(conn, "agent-c", -1)
        online, _, _ = list_online(conn, window_minutes=15)
        names = [a["name"] for a in online]
        assert "agent-c" in names

    def test_count_matches_list_length(self, conn):
        """online_count == len(online_agents)."""
        upsert_agent(conn, "agent-a")
        upsert_agent(conn, "agent-b")
        _set_last_seen(conn, "agent-a", -3)
        _set_last_seen(conn, "agent-b", -20)  # outside window
        online, count, _ = list_online(conn, window_minutes=15)
        assert count == len(online)
        assert count == 1

    def test_registered_total_includes_offline(self, conn):
        """registered_total counts all agents, including those offline."""
        upsert_agent(conn, "agent-a")
        upsert_agent(conn, "agent-b")
        _set_last_seen(conn, "agent-a", -3)   # online
        _set_last_seen(conn, "agent-b", -30)   # offline
        _, _, registered = list_online(conn, window_minutes=15)
        assert registered == 2

    def test_no_agents_returns_empty(self, conn):
        """Empty agents table returns empty online list."""
        online, count, registered = list_online(conn, window_minutes=15)
        assert online == []
        assert count == 0
        assert registered == 0

    def test_custom_window_minutes(self, conn):
        """window_minutes parameter is respected."""
        upsert_agent(conn, "agent-a")
        _set_last_seen(conn, "agent-a", -10)  # 10 min ago
        # 5-min window — not online
        online, count, _ = list_online(conn, window_minutes=5)
        assert count == 0
        # 30-min window — online
        online, count, _ = list_online(conn, window_minutes=30)
        assert count == 1

    def test_online_agent_dict_fields(self, conn):
        """Online agent dict has name, avatar_seed, pair_initials."""
        upsert_agent(conn, "agent-a")
        _set_last_seen(conn, "agent-a", -3)
        online, _, _ = list_online(conn, window_minutes=15)
        assert len(online) == 1
        agent = online[0]
        assert "name" in agent
        assert "avatar_seed" in agent
        assert "pair_initials" in agent
        assert agent["name"] == "agent-a"
