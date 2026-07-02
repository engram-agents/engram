"""Integration test for the forum-app coordination wiring (UCS Slice A → live DM).

Unlike test_dm_api.py (which injects a Stub store post-create), this exercises the
REAL wiring: ``create_app(coord_root=tmp)`` instantiates the file-backed
``FileStore`` + ``SeqAllocator`` into ``app.config``, so a DM round-trips through
actual file persistence and the seq cursor recovers across app restarts. This is
the create_app → FileStore → /api/dm path end-to-end — the thing the foundation
stack exists to make work.
"""
import sqlite3

import pytest

from forum.db import init_db
from forum.server import create_app


def _make_db(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    return db_path, audit_path


@pytest.fixture
def forum_home(tmp_path):
    return tmp_path / "forum_home"


@pytest.fixture
def client(tmp_path, forum_home):
    db_path, audit_path = _make_db(tmp_path)
    app = create_app(db_path, audit_path, coord_root=str(forum_home))
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def unwired_client(tmp_path):
    db_path, audit_path = _make_db(tmp_path)
    app = create_app(db_path, audit_path)  # NO coord_root → store unconfigured
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# The store is actually wired (not 503) when coord_root is passed
# ---------------------------------------------------------------------------
def test_wired_store_is_configured(client):
    r = client.get("/api/dm?agent=ariadne")
    assert r.status_code == 200
    assert r.get_json() == {"threads": [], "agent": "ariadne"}


# ---------------------------------------------------------------------------
# DM round-trips through the REAL FileStore (file persistence, not a stub)
# ---------------------------------------------------------------------------
def test_dm_round_trip_through_real_filestore(client, forum_home):
    r = client.post("/api/dm/sol", json={"agent": "ariadne", "body": "hello via wiring"})
    assert r.status_code == 201
    assert r.get_json()["seq"] == 1  # fresh store → first seq

    r = client.get("/api/dm/sol?agent=ariadne")
    assert r.status_code == 200
    msgs = r.get_json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["body"] == "hello via wiring"
    assert msgs[0]["sender"] == "ariadne"
    assert msgs[0]["recipient"] == "sol"

    # Proof of real persistence: the FileStore wrote the per-pair thread file.
    thread_file = forum_home / "dm" / "ariadne+sol.md"
    assert thread_file.exists()
    assert "hello via wiring" in thread_file.read_text(encoding="utf-8")


def test_seq_monotonic_via_wired_allocator(client):
    s1 = client.post("/api/dm/sol", json={"agent": "ariadne", "body": "one"}).get_json()["seq"]
    s2 = client.post("/api/dm/sol", json={"agent": "ariadne", "body": "two"}).get_json()["seq"]
    assert s2 > s1  # the wired SeqAllocator advances monotonically


def test_order_independent_thread(client):
    client.post("/api/dm/sol", json={"agent": "ariadne", "body": "from ari"})
    # sol reads the SAME thread (order-independent key) and sees the message.
    r = client.get("/api/dm/ariadne?agent=sol")
    assert [m["body"] for m in r.get_json()["messages"]] == ["from ari"]


def test_list_threads_after_send(client):
    client.post("/api/dm/sol", json={"agent": "ariadne", "body": "x"})
    client.post("/api/dm/borges", json={"agent": "ariadne", "body": "y"})
    r = client.get("/api/dm?agent=ariadne")
    cps = sorted(t["counterpart"] for t in r.get_json()["threads"])
    assert cps == ["borges", "sol"]


def test_since_seq_incremental(client):
    s1 = client.post("/api/dm/sol", json={"agent": "ariadne", "body": "m1"}).get_json()["seq"]
    client.post("/api/dm/sol", json={"agent": "ariadne", "body": "m2"})
    r = client.get(f"/api/dm/sol?agent=ariadne&since_seq={s1}")
    assert [m["body"] for m in r.get_json()["messages"]] == ["m2"]  # only after s1


# ---------------------------------------------------------------------------
# The whole point of recover_max_seq: a fresh app on the same forum-home resumes
# the seq cursor ABOVE everything on disk (no re-issue of a live seq).
# ---------------------------------------------------------------------------
def test_seq_recovers_across_app_restart(tmp_path, forum_home):
    db_path, audit_path = _make_db(tmp_path)

    app1 = create_app(db_path, audit_path, coord_root=str(forum_home))
    app1.config["TESTING"] = True
    c1 = app1.test_client()
    s1 = c1.post("/api/dm/sol", json={"agent": "ariadne", "body": "before restart"}).get_json()["seq"]

    # "restart" — a brand-new app instance on the SAME forum-home.
    app2 = create_app(db_path, audit_path, coord_root=str(forum_home))
    app2.config["TESTING"] = True
    c2 = app2.test_client()
    s2 = c2.post("/api/dm/sol", json={"agent": "ariadne", "body": "after restart"}).get_json()["seq"]

    assert s2 > s1  # new allocator recovered the high-water-mark from disk
    r = c2.get("/api/dm/sol?agent=ariadne")
    assert [m["body"] for m in r.get_json()["messages"]] == ["before restart", "after restart"]


# ---------------------------------------------------------------------------
# Unwired (no coord_root) → documented 503, not a crash
# ---------------------------------------------------------------------------
def test_unwired_returns_503(unwired_client):
    assert unwired_client.get("/api/dm?agent=ariadne").status_code == 503
    assert unwired_client.get("/api/dm/sol?agent=ariadne").status_code == 503
    assert unwired_client.post(
        "/api/dm/sol", json={"agent": "ariadne", "body": "x"}
    ).status_code == 503


# ---------------------------------------------------------------------------
# #1468 charset guard — a '+' (or whitespace/separator) in a name is rejected
# with 400, so two distinct pairs can never silently collide onto one DM thread.
# ---------------------------------------------------------------------------
class TestAgentNameCharsetGuard:
    def test_plus_in_sender_rejected(self, client):
        # "a+b" as sender would key "a+b"+"sol" == "a"+"b+sol" — a silent collision.
        r = client.post("/api/dm/sol", json={"agent": "a+b", "body": "x"})
        assert r.status_code == 400

    def test_plus_in_counterpart_rejected(self, client):
        r = client.post("/api/dm/b+c", json={"agent": "ariadne", "body": "x"})
        assert r.status_code == 400

    def test_plus_rejected_on_read_and_list(self, client):
        assert client.get("/api/dm/b+c?agent=ariadne").status_code == 400
        assert client.get("/api/dm?agent=a+b").status_code == 400

    def test_whitespace_and_separators_rejected(self, client):
        for bad in ["a b", "a/b", "a.b", "a:b"]:
            r = client.post("/api/dm/sol", json={"agent": bad, "body": "x"})
            assert r.status_code == 400, f"{bad!r} should be rejected"

    def test_no_collision_after_guard(self, client, forum_home):
        # The pair that WOULD collide is now un-creatable; legit names still work
        # and land in distinct thread files.
        client.post("/api/dm/sol", json={"agent": "ariadne", "body": "to sol"})
        client.post("/api/dm/borges", json={"agent": "ariadne", "body": "to borges"})
        files = sorted(p.name for p in (forum_home / "dm").glob("*.md"))
        assert files == ["ariadne+borges.md", "ariadne+sol.md"]

    def test_valid_names_still_accepted(self, client):
        # Allowlist must not over-reject: hyphen/underscore/digits are fine.
        for ok in ["sol", "agent-2", "luria_x", "clio9"]:
            r = client.post("/api/dm/sol", json={"agent": ok, "body": "x"})
            assert r.status_code == 201, f"{ok!r} should be accepted"
