"""Tests for GET /api/updates — the unified wake-cursor feed route (UCS Slice B).

Route-layer concerns (validation, response shape, the seq-cursor end-to-end via
the shared allocator). The relevance-filter logic itself is unit-tested in
test_coordination_updates.py.
"""

import sqlite3

import pytest

from forum.coordination import CoordinationStore, DmMessage, ProjectRecord, dm_thread_key
from forum.coordination.seq import SeqAllocator
from forum.db import init_db
from forum.server import create_app


class StubStore(CoordinationStore):
    def __init__(self, *, projects=None):
        self._threads = {}
        self._max_seq = 0
        self._projects = list(projects or [])

    def read_projects(self, *, active_only=True):
        if active_only:
            return [p for p in self._projects if p.status != "closed"]
        return list(self._projects)

    def read_project(self, pid):
        return None

    def write_project(self, pid, content, *, seq):
        pass

    def archive_project(self, project_id):
        pass

    def read_dm_thread(self, a, b, *, since_seq=0):
        key = dm_thread_key(a, b)
        return [m for m in self._threads.get(key, []) if m.seq > since_seq]

    def append_dm(self, sender, recipient, body, *, seq, ts):
        key = dm_thread_key(sender, recipient)
        msg = DmMessage(seq, sender, recipient, body, ts)
        self._threads.setdefault(key, []).append(msg)
        self._max_seq = max(self._max_seq, seq)
        return msg

    def list_dm_threads(self, agent):
        out = []
        for key in self._threads:
            parts = key.split("+")
            if agent in parts:
                out.append(next((p for p in parts if p != agent), parts[0]))
        return out

    def list_all_dm_threads(self):
        pairs = []
        for key in self._threads:
            parts = key.split("+")
            if len(parts) == 2:
                pairs.append((parts[0], parts[1]))
        return sorted(pairs)

    def recover_max_seq(self):
        return self._max_seq


def _proj(pid, turn, seq, *, status="in-progress"):
    return ProjectRecord(
        project_id=pid, title="t", status=status, turn=turn,
        turn_since="2026-06-26T00:00:00Z", turn_reason="r",
        participants=("ariadne", "borges"), seq=seq, raw="",
    )


@pytest.fixture
def make_app(tmp_path):
    def _make(projects=None):
        db_path = str(tmp_path / "forum.db")
        audit_path = str(tmp_path / "audit.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit_path)
        app.config["TESTING"] = True
        app.config["COORD_STORE"] = StubStore(projects=projects)
        app.config["COORD_ALLOCATOR"] = SeqAllocator()
        return app
    return _make


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def test_requires_agent(make_app):
    resp = make_app().test_client().get("/api/updates")
    assert resp.status_code == 400


def test_invalid_agent_name_rejected(make_app):
    resp = make_app().test_client().get("/api/updates?agent=a+b")
    assert resp.status_code == 400


def test_non_integer_since_rejected(make_app):
    resp = make_app().test_client().get("/api/updates?agent=ariadne&since=abc")
    assert resp.status_code == 400


def test_503_when_store_unconfigured(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, audit_path)  # no COORD_STORE / COORD_ALLOCATOR
    resp = app.test_client().get("/api/updates?agent=ariadne")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# shape + liveness
# ---------------------------------------------------------------------------
def test_empty_feed_is_alive_shape(make_app):
    data = make_app().test_client().get("/api/updates?agent=ariadne").get_json()
    assert data["updates"] == []
    assert data["as_of"] == 0
    assert isinstance(data["ts"], str) and data["ts"]  # liveness tick present


# ---------------------------------------------------------------------------
# seq-cursor end-to-end (POST /api/dm bumps the same allocator the feed reads)
# ---------------------------------------------------------------------------
def test_dm_round_trips_into_feed(make_app):
    client = make_app().test_client()
    sent = client.post("/api/dm/ariadne", json={"agent": "borges", "body": "hi ari"})
    assert sent.status_code == 201
    seq = sent.get_json()["seq"]

    data = client.get("/api/updates?agent=ariadne").get_json()
    assert data["as_of"] == seq  # the served watermark == the DM's seq
    assert [(u["kind"], u["seq"], u["sender"]) for u in data["updates"]] == [("dm", seq, "borges")]
    assert data["updates"][0]["wake"] == "act-now"


def test_since_exclusive_over_route(make_app):
    client = make_app().test_client()
    seq = client.post("/api/dm/ariadne", json={"agent": "borges", "body": "hi"}).get_json()["seq"]
    # since == the delivered seq → nothing new.
    data = client.get(f"/api/updates?agent=ariadne&since={seq}").get_json()
    assert data["updates"] == []
    assert data["as_of"] == seq  # as_of still reports the watermark (frozen, not dead)


def test_baton_turn_mine_in_feed(make_app):
    app = make_app(projects=[_proj("PR-9", turn="ariadne", seq=3)])
    # the allocator must report a watermark >= the project's seq for it to be served
    app.config["COORD_ALLOCATOR"] = SeqAllocator(recover=lambda: 3)
    data = app.test_client().get("/api/updates?agent=ariadne").get_json()
    assert [(u["kind"], u["project_id"]) for u in data["updates"]] == [("baton", "PR-9")]


def test_kinds_narrows_over_route(make_app):
    app = make_app(projects=[_proj("PR-9", turn="ariadne", seq=2)])
    app.config["COORD_ALLOCATOR"] = SeqAllocator(recover=lambda: 9)
    client = app.test_client()
    client.post("/api/dm/ariadne", json={"agent": "borges", "body": "hi"})
    data = client.get("/api/updates?agent=ariadne&kinds=baton").get_json()
    assert all(u["kind"] == "baton" for u in data["updates"])
