"""#4465: a JSON body that parses but is not an object must get a typed 400, never a 500.

Several write handlers did ``data.get(...)`` on whatever ``get_json`` returned, so a
JSON array body (``[1]``) raised AttributeError and the server answered an HTML 500.
The handler set is enumerated from ``app.url_map`` (every POST/PUT/PATCH/DELETE
``/api/`` route), so a write handler added later is covered without editing this file.
"""
import sqlite3

import pytest

from forum.db import init_db
from forum.server import create_app

_WRITE = {"POST", "PUT", "PATCH", "DELETE"}
# A write route that takes no JSON object body at all (e.g. a multipart upload) is out of
# class; it must be named here with a reason, never silently skipped.
_NOT_JSON_BODY: dict = {
    ("POST", "/api/packs/<pack_id>/label"): "reads request.form (a form field), not a JSON body",
}


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(tmp_path / "audit.jsonl"), coord_root=str(tmp_path / "coord"))
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False  # an unhandled error must read as the 500 a caller sees
    return app


def _url(rule):
    vals = {}
    for arg in rule.arguments:
        conv = rule._converters[arg]
        vals[arg] = 1 if type(conv).__name__ == "IntegerConverter" else "probe-1"
    return rule.build(vals, append_unknown=False)[1]


def _routes(app):
    out = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/"):
            continue
        for m in sorted(rule.methods & _WRITE):
            if (m, rule.rule) not in _NOT_JSON_BODY:
                out.append((m, rule))
    return out


def test_enumeration_walks_the_real_url_map(app):
    routes = {(m, r.rule) for m, r in _routes(app)}
    for must in [("POST", "/api/post"), ("POST", "/api/agents/status"),
                 ("PATCH", "/api/agent/me"), ("POST", "/api/projects/<pid>/flip")]:
        assert must in routes, must
    assert len(routes) >= 20


@pytest.mark.parametrize("body", [[], [1], [{"agent": "x"}], None, 1, "s"],
                         ids=["empty-list", "list-of-int", "list-of-obj", "null", "number", "string"])
def test_every_write_route_refuses_a_non_object_body_with_400(app, body):
    client = app.test_client()
    bad = []
    for method, rule in _routes(app):
        resp = client.open(_url(rule), method=method, json=body)
        ok = resp.status_code == 400 and resp.is_json and resp.get_json().get("error")
        if not ok:
            bad.append((method, rule.rule, resp.status_code))
    assert not bad, "\n".join(map(str, bad))


def test_refusal_names_the_route_and_the_requirement(app):
    resp = app.test_client().post("/api/post", json=[1])
    assert resp.status_code == 400
    err = resp.get_json()["error"]
    assert "/api/post" in err and "JSON body must be an object" in err and "list" in err


def test_an_object_body_still_reaches_the_handler(app):
    # not a JSON-body refusal: the handler runs and refuses on its own terms
    resp = app.test_client().post("/api/post", json={"x": 1})
    assert resp.status_code == 400
    assert "JSON body" not in resp.get_json()["error"]
