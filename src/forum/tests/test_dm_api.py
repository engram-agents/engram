"""Tests for /api/dm endpoints — uses a Stub CoordinationStore injected via app.config."""
import sqlite3
import pytest
from forum.db import init_db
from forum.server import create_app
from forum.coordination import CoordinationStore, DmMessage, dm_thread_key
from forum.coordination.seq import SeqAllocator


class StubStore(CoordinationStore):
    def __init__(self):
        self._threads = {}
        self._max_seq = 0

    def read_projects(self, *, active_only=True):
        return []

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

    def recover_max_seq(self):
        return self._max_seq


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()

    stub = StubStore()
    allocator = SeqAllocator()

    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    application.config["COORD_STORE"] = stub
    application.config["COORD_ALLOCATOR"] = allocator
    return application


@pytest.fixture
def client(app):
    return app.test_client()


class TestDmListEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/api/dm?agent=sol")
        assert resp.status_code == 200

    def test_requires_agent(self, client):
        resp = client.get("/api/dm")
        assert resp.status_code == 400

    def test_empty_threads_for_new_agent(self, client):
        data = client.get("/api/dm?agent=sol").get_json()
        assert data["threads"] == []
        assert data["agent"] == "sol"


class TestDmReadEndpoint:
    def test_returns_empty_for_no_messages(self, client):
        resp = client.get("/api/dm/ariadne?agent=sol")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["messages"] == []

    def test_requires_agent(self, client):
        resp = client.get("/api/dm/ariadne")
        assert resp.status_code == 400

    def test_returns_messages_after_send(self, client):
        client.post(
            "/api/dm/ariadne",
            json={"agent": "sol", "body": "hello ariadne"},
        )
        data = client.get("/api/dm/ariadne?agent=sol").get_json()
        assert len(data["messages"]) == 1
        assert data["messages"][0]["body"] == "hello ariadne"
        assert data["messages"][0]["sender"] == "sol"

    def test_since_seq_filters(self, client):
        # First send gets seq=1, second gets seq=2.
        client.post("/api/dm/ariadne", json={"agent": "sol", "body": "first"})
        r2 = client.post("/api/dm/ariadne", json={"agent": "sol", "body": "second"})
        seq2 = r2.get_json()["seq"]
        # since_seq=seq2-1 → returns only messages with seq > (seq2-1)
        # seq2=2, since_seq=1 → returns only "second" (seq=2)
        data = client.get(
            f"/api/dm/ariadne?agent=sol&since_seq={seq2 - 1}"
        ).get_json()
        bodies = [m["body"] for m in data["messages"]]
        assert "second" in bodies
        assert "first" not in bodies

    def test_read_is_order_independent(self, client):
        client.post("/api/dm/ariadne", json={"agent": "sol", "body": "hi"})
        from_sol = client.get("/api/dm/ariadne?agent=sol").get_json()
        from_ariadne = client.get("/api/dm/sol?agent=ariadne").get_json()
        assert len(from_sol["messages"]) == len(from_ariadne["messages"])


class TestDmSendEndpoint:
    def test_returns_201(self, client):
        resp = client.post("/api/dm/ariadne", json={"agent": "sol", "body": "hi"})
        assert resp.status_code == 201

    def test_returns_seq_and_ts(self, client):
        data = client.post(
            "/api/dm/ariadne", json={"agent": "sol", "body": "hi"}
        ).get_json()
        assert "seq" in data
        assert "ts" in data
        assert data["seq"] >= 1

    def test_requires_agent(self, client):
        resp = client.post("/api/dm/ariadne", json={"body": "hi"})
        assert resp.status_code == 400

    def test_requires_body(self, client):
        resp = client.post("/api/dm/ariadne", json={"agent": "sol"})
        assert resp.status_code == 400

    def test_requires_json(self, client):
        resp = client.post(
            "/api/dm/ariadne", data="not json", content_type="text/plain"
        )
        assert resp.status_code == 400

    def test_sequential_seqs(self, client):
        r1 = client.post(
            "/api/dm/ariadne", json={"agent": "sol", "body": "first"}
        ).get_json()
        r2 = client.post(
            "/api/dm/ariadne", json={"agent": "sol", "body": "second"}
        ).get_json()
        assert r2["seq"] > r1["seq"]


class TestDmNoStore:
    def test_list_returns_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        # COORD_STORE not injected
        c = app.test_client()
        assert c.get("/api/dm?agent=sol").status_code == 503

    def test_send_returns_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.post(
            "/api/dm/ariadne", json={"agent": "sol", "body": "hi"}
        ).status_code == 503

    def test_read_returns_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.get("/api/dm/ariadne?agent=sol").status_code == 503
