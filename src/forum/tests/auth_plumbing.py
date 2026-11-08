"""TEST-ONLY plumbing for the #1459 auth guard. Lives under tests; production code has no
switch that disables the guard.

The server's one ``before_request`` guard refuses writes (and reads naming an agent)
without a bearer token for that agent. Test suites that predate it call the Flask test
client as a named agent. Rather than weaken the guard, ``wrap_client`` makes a Flask test
client attach a token for the identity each request names (issued into the app's own DB,
cached per (db, agent)); a request that names no identity, or a malformed one, carries the
harness agent's token (so the real guard then refuses the malformed name as 403).

Used by ``src/forum/tests/conftest.py`` (autouse, for the forum suite) and by the other
test roots that wire a real Flask app (``tools/test_forum_cli.py``, ``tests/test_baton.py``).
Tests of the guard itself opt out (``@pytest.mark.raw_auth``) and see the unaided server.
"""
from __future__ import annotations

import json
import re
import sqlite3
import types
from urllib.parse import parse_qsl, urlsplit

HARNESS_AGENT = "test-harness"
_AGENT_PATH = re.compile(r"^/api/agent/([^/]+)/")


def named_identity(path, query, json_body, form):
    """The identity a request names, or None (test-side mirror of the guard's rule)."""
    m = _AGENT_PATH.match(path)
    if m:
        return m.group(1).strip().lower()
    for src in (query, json_body if isinstance(json_body, dict) else {}, form):
        v = src.get("agent") if hasattr(src, "get") else None
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def wrap_client(client, default_agent=HARNESS_AGENT):
    """Make ``client`` (a Flask test client) attach a bearer token to every request.

    ``default_agent``: whose token a request carries when it visibly names no (valid)
    identity, e.g. a raw multipart body the plumbing cannot parse."""
    from forum import db as _db

    real_open = client.open
    cache: dict = {}

    def token_for(app, agent):
        key = (app.config["DB_PATH"], agent)
        if key not in cache:
            conn = sqlite3.connect(app.config["DB_PATH"])
            try:
                cache[key] = _db.issue_token(conn, agent)
            finally:
                conn.close()
        return cache[key]

    def open_with_auth(self, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        env = kwargs.get("environ_overrides") or {}
        if "Authorization" not in headers and "HTTP_AUTHORIZATION" not in env:
            path = args[0] if args and isinstance(args[0], str) else kwargs.get("path", "")
            if not isinstance(path, str):
                return real_open(*args, **kwargs)
            sp = urlsplit(path)
            query = dict(parse_qsl(sp.query))
            qs = kwargs.get("query_string") or {}
            if isinstance(qs, (str, bytes)):
                qs = dict(parse_qsl(qs if isinstance(qs, str) else qs.decode()))
            query.update(qs or {})
            data = kwargs.get("data")
            form = data if isinstance(data, dict) else {}
            body = kwargs.get("json")
            if body is None and isinstance(data, (str, bytes)):
                try:
                    body = json.loads(data)
                except ValueError:
                    body = None
            method = str(kwargs.get("method", "GET")).upper()
            agent = named_identity(sp.path, query, body, form)
            if agent is None or "\n" in agent or not _db.is_valid_agent_name(agent):
                agent = default_agent
            if method in {"POST", "PUT", "PATCH", "DELETE"} or sp.path.startswith("/api/"):
                headers["Authorization"] = "Bearer " + token_for(self.application, agent)
                kwargs["headers"] = headers
        return real_open(*args, **kwargs)

    client.open = types.MethodType(open_with_auth, client)
    return client


def authed_test_client(app, default_agent=HARNESS_AGENT):
    """``app.test_client()`` with the token plumbing attached."""
    return wrap_client(app.test_client(), default_agent)
