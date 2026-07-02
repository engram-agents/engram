"""Tests for the operator DM viewer — GET /dm (overview) and GET /dm/<a>/<b> (thread).

Covers:
1. GET /dm returns 200 + lists seeded pairs.
2. GET /dm/<a>/<b> returns 200 + the thread messages.
3. Missing pair (no messages) is handled as empty 200, not a 500.
4. 503 when COORD_STORE is not configured.
5. list_all_dm_threads() (FileStore) returns the right pairs.
6. Invalid agent names in /dm/<a>/<b> return 400.
"""

import sqlite3

import pytest

from forum.coordination import CoordinationStore, DmMessage, FileStore, dm_thread_key
from forum.coordination.seq import SeqAllocator
from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Stub store — in-memory impl for web route tests
# ---------------------------------------------------------------------------
class StubStore(CoordinationStore):
    """Minimal in-memory store for DM viewer tests."""

    def __init__(self):
        self._threads: dict[str, list[DmMessage]] = {}

    # --- DM ---
    def read_dm_thread(self, a, b, *, since_seq=0):
        key = dm_thread_key(a, b)
        return [m for m in self._threads.get(key, []) if m.seq > since_seq]

    def append_dm(self, sender, recipient, body, *, seq, ts):
        key = dm_thread_key(sender, recipient)
        msg = DmMessage(seq, sender, recipient, body, ts)
        self._threads.setdefault(key, []).append(msg)
        return msg

    def list_dm_threads(self, agent):
        results = []
        for key in self._threads:
            parts = key.split("+")
            if agent in parts:
                other = next((p for p in parts if p != agent), parts[0])
                results.append(other)
        return results

    def list_all_dm_threads(self):
        pairs = []
        for key in self._threads:
            parts = key.split("+")
            if len(parts) == 2:
                pairs.append((parts[0], parts[1]))
        return sorted(pairs)

    # --- projects (not under test here) ---
    def read_projects(self, *, active_only=True):
        return []

    def read_project(self, pid):
        return None

    def write_project(self, pid, content, *, seq):
        pass

    def archive_project(self, pid):
        pass

    def recover_max_seq(self):
        seqs = [m.seq for ms in self._threads.values() for m in ms]
        return max(seqs, default=0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def make_app(tmp_path):
    """Factory: returns (app, stub_store)."""
    def _make():
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit_path)
        app.config["TESTING"] = True
        stub = StubStore()
        app.config["COORD_STORE"] = stub
        app.config["COORD_ALLOCATOR"] = SeqAllocator()
        return app, stub
    return _make


def _seeded_app(make_app):
    """App with two seeded DM threads: ariadne↔borges (2 msgs) and ariadne↔sol (1 msg)."""
    app, stub = make_app()
    stub.append_dm("ariadne", "borges", "hello borges", seq=1, ts="2026-06-26T10:00:00Z")
    stub.append_dm("borges", "ariadne", "hello back", seq=2, ts="2026-06-26T10:01:00Z")
    stub.append_dm("ariadne", "sol", "hey sol", seq=3, ts="2026-06-26T10:02:00Z")
    return app


# ---------------------------------------------------------------------------
# 1. GET /dm — overview
# ---------------------------------------------------------------------------
def test_dm_overview_200_empty(make_app):
    app, _stub = make_app()
    resp = app.test_client().get("/dm")
    assert resp.status_code == 200


def test_dm_overview_lists_seeded_pairs(make_app):
    app = _seeded_app(make_app)
    resp = app.test_client().get("/dm")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Both pairs must appear in the rendered page.
    assert "ariadne" in body
    assert "borges" in body
    assert "sol" in body


def test_dm_overview_links_to_threads(make_app):
    app = _seeded_app(make_app)
    body = app.test_client().get("/dm").get_data(as_text=True)
    # The overview must link to each thread's detail URL.
    assert "/dm/ariadne/borges" in body or "/dm/borges/ariadne" in body
    assert "/dm/ariadne/sol" in body or "/dm/sol/ariadne" in body


def test_dm_overview_shows_message_count(make_app):
    app = _seeded_app(make_app)
    body = app.test_client().get("/dm").get_data(as_text=True)
    # ariadne↔borges has 2 messages; the count must appear somewhere.
    assert "2" in body


def test_dm_overview_shows_last_preview(make_app):
    app = _seeded_app(make_app)
    body = app.test_client().get("/dm").get_data(as_text=True)
    # Last message in the ariadne↔borges thread is "hello back".
    assert "hello back" in body


def test_dm_overview_truncated_preview_shows_ellipsis(make_app):
    # A body longer than 120 chars is truncated → the ellipsis must render.
    app, stub = make_app()
    stub.append_dm("ariadne", "borges", "x" * 200, seq=1, ts="2026-06-26T10:00:00Z")
    body = app.test_client().get("/dm").get_data(as_text=True)
    assert "&hellip;" in body


def test_dm_overview_exactly_120_preview_no_ellipsis(make_app):
    # A body of exactly 120 chars is NOT truncated → no ellipsis (the off-by-one
    # the `>= 120` template check got wrong; the ellipsis keys on last_truncated,
    # derived from the original body length server-side, not the pre-sliced preview).
    app, stub = make_app()
    stub.append_dm("ariadne", "borges", "y" * 120, seq=1, ts="2026-06-26T10:00:00Z")
    body = app.test_client().get("/dm").get_data(as_text=True)
    assert "&hellip;" not in body


def test_dm_overview_nav_has_dms_link(make_app):
    app, _stub = make_app()
    body = app.test_client().get("/dm").get_data(as_text=True)
    assert 'href="/dm"' in body


# ---------------------------------------------------------------------------
# 2. GET /dm/<a>/<b> — thread view
# ---------------------------------------------------------------------------
def test_dm_thread_200_with_messages(make_app):
    app = _seeded_app(make_app)
    resp = app.test_client().get("/dm/ariadne/borges")
    assert resp.status_code == 200


def test_dm_thread_shows_all_messages(make_app):
    app = _seeded_app(make_app)
    body = app.test_client().get("/dm/ariadne/borges").get_data(as_text=True)
    assert "hello borges" in body
    assert "hello back" in body


def test_dm_thread_shows_senders(make_app):
    app = _seeded_app(make_app)
    body = app.test_client().get("/dm/ariadne/borges").get_data(as_text=True)
    assert "ariadne" in body
    assert "borges" in body


def test_dm_thread_order_independent(make_app):
    """GET /dm/ariadne/borges and GET /dm/borges/ariadne must return the same thread."""
    app = _seeded_app(make_app)
    client = app.test_client()
    body_ab = client.get("/dm/ariadne/borges").get_data(as_text=True)
    body_ba = client.get("/dm/borges/ariadne").get_data(as_text=True)
    assert "hello borges" in body_ab
    assert "hello borges" in body_ba


# ---------------------------------------------------------------------------
# 3. Missing pair — graceful empty page, not 500
# ---------------------------------------------------------------------------
def test_dm_thread_missing_pair_is_empty_200(make_app):
    app, _stub = make_app()
    resp = app.test_client().get("/dm/nobody/either")
    # Valid names, no messages — graceful empty page, not a 500.
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The agent names must appear in the page (the pair header is rendered).
    assert "nobody" in body
    assert "either" in body
    # The empty-thread message must appear; no messages listed.
    assert "No messages" in body


# ---------------------------------------------------------------------------
# 4. 503 when COORD_STORE not configured
# ---------------------------------------------------------------------------
def test_dm_overview_503_without_store(tmp_path):
    db_path = str(tmp_path / "f.db")
    audit = str(tmp_path / "a.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, audit)
    app.config["TESTING"] = True
    # COORD_STORE intentionally not set.
    resp = app.test_client().get("/dm")
    assert resp.status_code == 503


def test_dm_thread_503_without_store(tmp_path):
    db_path = str(tmp_path / "f.db")
    audit = str(tmp_path / "a.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, audit)
    app.config["TESTING"] = True
    resp = app.test_client().get("/dm/ariadne/borges")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 5. Invalid agent names → 400
# ---------------------------------------------------------------------------
def test_dm_thread_invalid_agent_name_400(make_app):
    app, _stub = make_app()
    # "+" is reserved in agent names (it is the pair-key separator).
    resp = app.test_client().get("/dm/a%2Bb/sol")
    # Flask decodes %2B to "+" in the path segment, so the route's
    # is_valid_agent_name check sees "a+b", rejects it, and returns 400.
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 6. FileStore.list_all_dm_threads() — unit test
# ---------------------------------------------------------------------------
def test_list_all_dm_threads_empty(tmp_path):
    store = FileStore(tmp_path)
    assert store.list_all_dm_threads() == []


def test_list_all_dm_threads_returns_pairs(tmp_path):
    store = FileStore(tmp_path)
    store.append_dm("ariadne", "borges", "hi", seq=1, ts="t")
    store.append_dm("ariadne", "sol", "hey", seq=2, ts="t")
    pairs = store.list_all_dm_threads()
    assert sorted(pairs) == [("ariadne", "borges"), ("ariadne", "sol")]


def test_list_all_dm_threads_sorted_tuples(tmp_path):
    store = FileStore(tmp_path)
    store.append_dm("sol", "ariadne", "msg", seq=1, ts="t")
    pairs = store.list_all_dm_threads()
    # dm_thread_key sorts lexicographically, so "ariadne+sol" → ("ariadne", "sol").
    assert pairs == [("ariadne", "sol")]


def test_list_all_dm_threads_no_duplicates(tmp_path):
    """Multiple messages to the same pair must not produce duplicate pair entries."""
    store = FileStore(tmp_path)
    store.append_dm("ariadne", "borges", "first", seq=1, ts="t")
    store.append_dm("borges", "ariadne", "second", seq=2, ts="t")
    pairs = store.list_all_dm_threads()
    assert len(pairs) == 1
    assert pairs[0] == ("ariadne", "borges")


# ---------------------------------------------------------------------------
# 7. Read-only contract — POST must return 405
# ---------------------------------------------------------------------------
def test_dm_overview_post_returns_405(make_app):
    """POST /dm must return 405 — the route is read-only (GET only)."""
    app, _stub = make_app()
    resp = app.test_client().post("/dm")
    assert resp.status_code == 405


def test_dm_thread_post_returns_405(make_app):
    """POST /dm/<a>/<b> must return 405 — the route is read-only (GET only)."""
    app, _stub = make_app()
    resp = app.test_client().post("/dm/ariadne/borges")
    assert resp.status_code == 405
