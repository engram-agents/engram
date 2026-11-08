"""#1459 PR A: loopback default, per-agent bearer tokens, identity bound to the token.

Contract (pinned before the implementation):

* ONE ``before_request`` guard. A request is guarded iff it is a POST/PUT/PATCH/
  DELETE on any route, OR it is under ``/api/`` and names an agent identity
  (``agent`` in the query string, a JSON object body or form data, or the
  ``<name>`` of ``/api/agent/<name>/...``).
* Guarded + no/unknown/revoked bearer token -> 401 + ``WWW-Authenticate: Bearer``.
* Guarded + valid token for T, request names N with N.strip().lower() != T -> 403
  ``identity mismatch`` (T is never silently substituted for N).
* Public reads naming no agent stay open.
* Tokens are stored as sha256 only; no raw token in the DB, audit log or errors.

These tests opt OUT of the suite-wide auto-auth plumbing in conftest.py
(``raw_auth``) so they exercise the real guard.
"""

import hashlib
import io
import json
import re
import shutil
import sqlite3
import tempfile
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from forum import db
from forum.db import init_db
from forum.server import _parse_args, create_app

pytestmark = pytest.mark.raw_auth


@pytest.fixture
def owned_tmp():
    """A scratch dir this test owns (mkdtemp, removed afterwards). Not pytest's shared
    numbered tmp_path base, which a concurrent pytest session can prune mid-run (#1459 round 1c)."""
    d = Path(tempfile.mkdtemp(prefix="forum1459-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _make_app(owned_tmp, **kw):
    db_path = str(owned_tmp / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(owned_tmp / "audit.jsonl"), **kw)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def app(owned_tmp):
    return _make_app(owned_tmp)


@pytest.fixture
def client(app):
    return app.test_client()


def _issue(app, agent):
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        return db.issue_token(conn, agent)
    finally:
        conn.close()


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def _count(app, table):
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _agent_row(app, name):
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        return conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone()
    finally:
        conn.close()


POST_BODY = {"agent": "alice", "category_slug": "inter-agent", "title": "t", "body_md": "hello"}


def _post(client, agent, headers=None):
    body = dict(POST_BODY, agent=agent)
    return client.post("/api/post", json=body, headers=headers or {})


# 1 ------------------------------------------------------------------
def test_default_host_is_loopback():
    assert _parse_args([]).host == "127.0.0.1"


# 2 ------------------------------------------------------------------
def test_post_without_token_is_401_and_writes_nothing(app, client):
    before = (_count(app, "threads"), _count(app, "posts"), _count(app, "agents"))
    resp = _post(client, "alice")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"
    body = resp.get_json()
    assert body["error"] == "authentication required"
    assert "forum-token" in body["hint"] and "token issue" in body["hint"]
    assert (_count(app, "threads"), _count(app, "posts"), _count(app, "agents")) == before


# 3 ------------------------------------------------------------------
def test_token_for_alice_posting_as_bob_is_403_and_writes_nothing(app, client):
    tok = _issue(app, "alice")
    before = (_count(app, "threads"), _count(app, "posts"), _count(app, "agents"))
    resp = _post(client, "bob", _auth(tok))
    assert resp.status_code == 403
    assert resp.get_json() == {
        "error": "identity mismatch", "token_agent": "alice", "named_agent": "bob"}
    assert (_count(app, "threads"), _count(app, "posts"), _count(app, "agents")) == before


# 4 ------------------------------------------------------------------
def test_token_for_alice_posting_as_alice_succeeds(app, client):
    tok = _issue(app, "alice")
    resp = _post(client, "alice", _auth(tok))
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)
    assert _count(app, "threads") >= 1


def test_name_comparison_is_strip_lower(app, client):
    tok = _issue(app, "alice")
    resp = _post(client, "  ALICE ", _auth(tok))
    assert resp.status_code != 403 and resp.status_code != 401


# 5 ------------------------------------------------------------------
def test_revoked_token_is_401(app, client):
    tok = _issue(app, "alice")
    conn = sqlite3.connect(app.config["DB_PATH"])
    assert db.revoke_tokens(conn, "alice") == 1
    conn.close()
    resp = _post(client, "alice", _auth(tok))
    assert resp.status_code == 401


def test_unknown_token_is_401(client):
    resp = _post(client, "alice", _auth("not-a-real-token"))
    assert resp.status_code == 401


def test_malformed_json_write_is_still_guarded(client):
    resp = client.post("/api/post", data="{not json", content_type="application/json")
    assert resp.status_code == 401


# 6 ------------------------------------------------------------------
def test_dm_read_requires_matching_token(owned_tmp):
    app = _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"))
    client = app.test_client()
    a, b = _issue(app, "alice"), _issue(app, "bob")
    assert client.get("/api/dm?agent=alice").status_code == 401
    assert client.get("/api/dm?agent=alice", headers=_auth(a)).status_code == 200
    assert client.get("/api/dm?agent=alice", headers=_auth(b)).status_code == 403
    assert client.get("/api/dm/bob?agent=alice").status_code == 401
    assert client.get("/api/dm/bob?agent=alice", headers=_auth(b)).status_code == 403


def test_dm_post_requires_token_and_binds_sender(owned_tmp):
    app = _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"))
    client = app.test_client()
    b = _issue(app, "bob")
    body = {"agent": "alice", "body": "hi"}
    assert client.post("/api/dm/bob", json=body).status_code == 401
    assert client.post("/api/dm/bob", json=body, headers=_auth(b)).status_code == 403


# 7 ------------------------------------------------------------------
def test_updates_without_token_is_401(client):
    assert client.get("/api/updates?agent=alice").status_code == 401


# 8 ------------------------------------------------------------------
def test_inbox_requires_matching_token(app, client):
    b = _issue(app, "bob")
    assert client.get("/api/agent/alice/inbox").status_code == 401
    assert client.get("/api/agent/alice/inbox", headers=_auth(b)).status_code == 403
    a = _issue(app, "alice")
    assert client.get("/api/agent/alice/inbox", headers=_auth(a)).status_code == 200


def test_mentions_requires_token(client):
    assert client.get("/api/agent/alice/mentions").status_code == 401


def test_repeated_query_agent_param_cannot_smuggle_identity(app, client):
    a = _issue(app, "alice")
    # handler reads the first value; the guard must check every value.
    resp = client.get("/api/updates?agent=alice&agent=bob", headers=_auth(a))
    assert resp.status_code == 403


# A4 remaining routes ------------------------------------------------
def test_flip_patch_and_pack_form_are_guarded(client):
    assert client.post("/api/projects/x-1/flip", json={"agent": "alice"}).status_code == 401
    assert client.patch("/api/agent/me", json={"agent": "alice"}).status_code == 401
    assert client.post("/api/packs", data={"agent": "alice"}).status_code == 401


def test_pack_form_identity_is_bound(app, client):
    b = _issue(app, "bob")
    resp = client.post("/api/packs", data={"agent": "alice"}, headers=_auth(b))
    assert resp.status_code == 403


# 9 CLASS CELL -------------------------------------------------------
def _fill(rule):
    vals = {}
    for arg in rule.arguments:
        conv = rule._converters[arg]
        if type(conv).__name__ == "IntegerConverter":
            vals[arg] = 1
        elif arg == "name":
            vals[arg] = "probe"
        else:
            vals[arg] = "dummy-1"
    return rule.build(vals, append_unknown=False)[1]


def test_class_cell_no_route_creates_an_agent_unauthenticated(owned_tmp):
    app = _make_app(owned_tmp, coord_root=str(owned_tmp / "coord"))
    client = app.test_client()
    seen = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        path = _fill(rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            seen += 1
            if method == "GET":
                client.get(path, query_string={"agent": "probe"})
            else:
                client.open(path, method=method, json={"agent": "probe"},
                            query_string={"agent": "probe"})
    assert seen > 40  # the enumeration really ran over the app's routes
    assert _agent_row(app, "probe") is None


# 10 -----------------------------------------------------------------
def test_html_thread_agent_param_creates_no_agent(app, client):
    tok = _issue(app, "alice")
    resp = client.post("/api/post", json=dict(POST_BODY), headers=_auth(tok))
    tid = (resp.get_json().get("thread_id") or resp.get_json().get("id") or 1)
    resp = client.get(f"/thread/{tid}?agent=newname")
    assert resp.status_code == 200
    assert _agent_row(app, "newname") is None


# 11 -----------------------------------------------------------------
def test_dm_viewer_404_by_default_and_200_when_enabled(owned_tmp):
    off = _make_app(owned_tmp, coord_root=str(owned_tmp / "c1"))
    assert off.test_client().get("/dm").status_code == 404
    assert off.test_client().get("/dm/alice/bob").status_code == 404
    on_dir = owned_tmp / "on"
    on_dir.mkdir()
    on = _make_app(on_dir, coord_root=str(on_dir / "c2"), enable_dm_viewer=True)
    assert on.test_client().get("/dm").status_code == 200


def test_mailroom_nav_link_only_when_viewer_enabled(owned_tmp):
    off = _make_app(owned_tmp, coord_root=str(owned_tmp / "c1"))
    assert 'href="/dm"' not in off.test_client().get("/").get_data(as_text=True)
    on_dir = owned_tmp / "on"
    on_dir.mkdir()
    on = _make_app(on_dir, coord_root=str(on_dir / "c2"), enable_dm_viewer=True)
    assert 'href="/dm"' in on.test_client().get("/").get_data(as_text=True)


# 12 -----------------------------------------------------------------
def test_public_reads_naming_no_agent_stay_open(app, client):
    tok = _issue(app, "alice")
    r = client.post("/api/post", json=dict(POST_BODY), headers=_auth(tok))
    tid = (r.get_json().get("thread_id") or r.get_json().get("id") or 1)
    assert client.get("/api/threads").status_code == 200
    assert client.get(f"/api/thread/{tid}").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/forum.md").status_code == 200


# token store --------------------------------------------------------
def test_db_token_helpers(owned_tmp):
    p = str(owned_tmp / "f.db")
    conn = sqlite3.connect(p)
    init_db(conn)
    t1 = db.issue_token(conn, "alice")
    t2 = db.issue_token(conn, "alice")
    assert t1 != t2 and len(t1) >= 43
    assert db.resolve_token(conn, t1) == "alice"
    assert db.resolve_token(conn, "nope") is None
    rows = db.list_tokens(conn)
    assert len(rows) == 2
    for r in rows:
        assert t1 not in json.dumps(r, default=str) and t2 not in json.dumps(r, default=str)
        assert len(r["hash_prefix"]) == 8
    assert db.revoke_tokens(conn, "alice") == 2
    assert db.resolve_token(conn, t1) is None
    assert db.revoke_tokens(conn, "alice") == 0
    with pytest.raises(ValueError):
        db.issue_token(conn, "Bad Name!")
    conn.close()


# 13 -----------------------------------------------------------------
def test_admin_token_issue_prints_one_line_and_stores_only_hash(owned_tmp, capsys):
    from forum import admin

    p = str(owned_tmp / "f.db")
    rc = admin.main(["--db", p, "token", "issue", "alice"])
    out, err = capsys.readouterr()
    assert rc == 0
    lines = out.splitlines()
    assert len(lines) == 1
    raw = lines[0]
    assert "not be shown again" in err and raw not in err
    conn = sqlite3.connect(p)
    stored = conn.execute("SELECT token_sha256, agent FROM agent_tokens").fetchall()
    conn.close()
    assert stored == [(hashlib.sha256(raw.encode()).hexdigest(), "alice")]
    assert raw.encode() not in Path(p).read_bytes()
    # list / revoke never print the token
    assert admin.main(["--db", p, "token", "list"]) == 0
    out, _ = capsys.readouterr()
    assert raw not in out and "alice" in out
    assert admin.main(["--db", p, "token", "revoke", "alice"]) == 0
    out, _ = capsys.readouterr()
    assert raw not in out


# 14 -----------------------------------------------------------------
def test_startup_reports_fail_closed_when_no_live_token(owned_tmp, capsys):
    from forum import server

    p = str(owned_tmp / "f.db")
    conn = sqlite3.connect(p)
    init_db(conn)
    server._report_auth_startup(conn, enable_dm_viewer=False)
    err = capsys.readouterr().err
    assert "python -m forum.admin token issue" in err
    assert "refused" in err
    db.issue_token(conn, "alice")
    server._report_auth_startup(conn, enable_dm_viewer=False)
    assert capsys.readouterr().err == ""
    server._report_auth_startup(conn, enable_dm_viewer=True)
    assert "every DM is readable by anyone who can reach this port" in capsys.readouterr().err
    conn.close()


# 15 -----------------------------------------------------------------
def test_no_token_text_in_audit_log_after_authenticated_post(app, client):
    tok = _issue(app, "alice")
    resp = _post(client, "alice", _auth(tok))
    assert resp.status_code in (200, 201)
    audit_text = Path(app.config["AUDIT_PATH"]).read_text()
    assert audit_text.strip()  # a record was written
    assert tok not in audit_text


def test_no_token_echoed_in_error_bodies(app, client):
    tok = _issue(app, "alice")
    resp = _post(client, "bob", _auth(tok))
    assert tok not in resp.get_data(as_text=True)
    resp = _post(client, "alice", _auth("secret-bogus-token-xyz"))
    assert "secret-bogus-token-xyz" not in resp.get_data(as_text=True)
