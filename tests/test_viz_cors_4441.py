"""engram-alpha#4441: the viz server and the Curve Workbench send no CORS wildcard,
answer only their own origin, and the config write (PUT) proves same origin.

Contract-first cells (contract C1..C10 of the issue's spec). Every cell talks to a REAL
server on an ephemeral port over a raw socket, so duplicated headers, a missing Host
header, header-name case and trailing spaces reach the server exactly as written.

GRID MAP (dimension -> the cells that vary it; held-constant dimensions are listed at the end):
  Origin value ......... TestReadOrigin (both servers), TestPreflight (viz), TestPutOrigin (viz),
                         TestHostRule.test_tunnel_host_reaches_both_checks
  Referer value ........ TestPutReferer (viz; Origin absent), TestPutOrigin.test_foreign_origin_beats_matching_referer
  X-Engram-Viz value ... TestPutHeader (viz; header-name case, trailing space, empty, duplicate)
  Sec-Fetch-Site value . TestPutSecFetchSite (viz), plus GET/OPTIONS unaffected
  Caller ............... browser-like (Origin set) and non-browser (no Origin, no Referer) in TestPutOrigin
  Verb ................. GET (TestReadOrigin, TestPathKinds), OPTIONS (TestPreflight), PUT (TestPut*),
                         POST/DELETE (TestUnsupportedVerbs, TestHostRule) -- both servers where the verb exists
  Path kind ............ json 200 / json error / html / static / unknown 404 (TestPathKinds, TestHostRule)
  Status ............... 200, 400, 403, 404, 500, 501 (TestPutUnchanged, TestNoWildcard, TestUnsupportedVerbs)
  Server ............... viz AND workbench in every read/Host/wildcard cell; PUT/OPTIONS contract on viz only
                         (the workbench has no PUT, C8)
  Host value ........... TestHostRule (full row set on JSON GET for both servers and on PUT for viz; a
                         five-row subset on every other verb x path-kind cell)
HELD CONSTANT (reported, not hidden): the preflight always carries Access-Control-Request-Method: PUT and
Access-Control-Request-Headers (one extra cell drops them); the Origin grid is crossed with ONE read path
(/api/agents) in full, and with the other path kinds only for {own, foreign, null, none}; every Origin x PUT
row uses one key/value (cadence.drowsiness_caution_pct = 70); the Host header is the server's own form in
every Origin/Referer/header/Sec-Fetch-Site cell; every request sends Connection: close (keep-alive is not
exercised, only that refusals carry a Content-Length); the http.server request line is always well formed.
"""
from __future__ import annotations

import gzip
import importlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO_ROOT / "src" / "engram"
sys.path.insert(0, str(REPO_ROOT))

from viz import agents as viz_agents  # noqa: E402
from viz import server as viz_server  # noqa: E402
from viz import workbench as viz_workbench  # noqa: E402
from viz.data import config as viz_config_data  # noqa: E402

TUNNELS = ["https://viz.example.com", "http://viz.example.com:8080"]
KEY = "cadence.drowsiness_caution_pct"
VAL = 70
DEFAULT = object()


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def _cors():
    return importlib.import_module("viz.cors")


def set_configured(origins):
    _cors().set_configured_origins(list(origins))


class Resp:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def get(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def one(self, name):
        vals = self.get(name)
        return vals[0] if len(vals) == 1 else None

    def cors_names(self):
        return [k for k, _ in self.headers if k.lower().startswith("access-control-")]

    def json(self):
        return json.loads(self.body)

    def vary_has_origin(self):
        toks = [t.strip().lower() for v in self.get("Vary") for t in v.split(",")]
        return "origin" in toks


def parse_response(data: bytes) -> Resp:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = []
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers.append((k.strip(), v.strip()))
    return Resp(status, headers, body)


class Srv:
    def __init__(self, kind, server, paths):
        self.kind, self.server, self.paths = kind, server, paths
        self.port = server.server_address[1]

    def own(self):
        return f"http://127.0.0.1:{self.port}"

    def request(self, method, path, headers=(), body=None, host=DEFAULT, http10=False) -> Resp:
        if host is DEFAULT:
            host = f"127.0.0.1:{self.port}"
        hs = []
        if host is not None:
            hs.append(("Host", host))
        hs.extend(headers)
        if body is not None:
            hs.append(("Content-Length", str(len(body))))
        hs.append(("Connection", "close"))
        ver = "1.0" if http10 else "1.1"
        head = "\r\n".join([f"{method} {path} HTTP/{ver}"] + [f"{k}: {v}" for k, v in hs]) + "\r\n\r\n"
        data = head.encode("latin-1") + (body or b"")
        c = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            c.sendall(data)
            buf = b""
            try:
                while True:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except ConnectionResetError:
                if not buf:
                    raise
        finally:
            c.close()
        return parse_response(buf)


def fmt(t, port):
    return t.replace("{P}", str(port)).replace("{P1}", str(port + 1))


def _config_doc(name):
    return {"schema_version": 2, "agent_name": name, "cadence": {"drowsiness_caution_pct": 80},
            "memory": {"tier2_max_nodes": 1000}}


VIZ_PATHS = {"json200": "/api/agents", "json_error": "/api/calibration?agent=nosuch",
             "html": "/config", "static": "/static/base.css", "unknown": "/nope"}
WB_PATHS = {"json200": "/api/agents", "json_error": "/api/health/v2?agent=nosuch",
            "html": "/", "static": "/static/health-workbench.js", "unknown": "/nope"}


class Env:
    pass


@pytest.fixture(scope="module")
def env():
    e = Env()
    e.tmp = Path(tempfile.mkdtemp(prefix="engram_cors_4441_"))
    e.cfg = {}
    e.orig = {}
    agents = {}
    for name in ("test", "other"):
        d = e.tmp / name
        d.mkdir()
        (d / "knowledge.db").write_bytes(b"")
        p = d / "config.json"
        p.write_text(json.dumps(_config_doc(name), indent=2))
        e.cfg[name] = p
        e.orig[name] = p.read_bytes()
        agents[name] = {"name": name, "label": name, "db": str(d / "knowledge.db")}
    saved = (viz_agents.AGENTS, viz_agents.DEFAULT_AGENT)
    viz_agents.AGENTS, viz_agents.DEFAULT_AGENT = agents, "test"
    servers = []

    def start(kind, handler, paths):
        s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        s.daemon_threads = True
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        servers.append(s)
        return Srv(kind, s, paths)

    e.viz = start("viz", viz_server.Handler, VIZ_PATHS)
    e.wb = start("workbench", viz_workbench.Handler, WB_PATHS)
    yield e
    for s in servers:
        s.shutdown()
        s.server_close()
    viz_agents.AGENTS, viz_agents.DEFAULT_AGENT = saved
    import shutil
    shutil.rmtree(e.tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset(env):
    for n, p in env.cfg.items():
        p.write_bytes(env.orig[n])
    viz_config_data._CONFIG_WRITE_BACKUPS.clear()
    try:
        set_configured(TUNNELS)
    except ImportError:
        pass
    yield
    try:
        set_configured([])
    except ImportError:
        pass


@pytest.fixture(params=["viz", "workbench"])
def srv(request, env):
    return env.viz if request.param == "viz" else env.wb


def cfg_bytes(env, name="test"):
    return env.cfg[name].read_bytes()


def own_put_headers(port):
    return [("Origin", f"http://127.0.0.1:{port}"), ("X-Engram-Viz", "1"),
            ("Content-Type", "application/json")]


def put(srv, headers, key=KEY, value=VAL, agent="test", body=DEFAULT, path=None, host=DEFAULT):
    if body is DEFAULT:
        body = json.dumps({"value": value}).encode()
    path = path if path is not None else f"/api/config/{key}?agent={agent}"
    return srv.request("PUT", path, headers, body=body, host=host)


def assert_no_wildcard_no_credentials(r):
    for k, v in r.headers:
        if k.lower().startswith("access-control-"):
            assert v.strip() != "*", f"wildcard in {k}: {v}"
    assert r.get("Access-Control-Allow-Credentials") == [], "Allow-Credentials must never be sent"


def assert_acao_invariant(r, origin, allowed):
    """Every ACAO is the exact request Origin, only when allowed, with Vary: Origin."""
    acao = r.get("Access-Control-Allow-Origin")
    if not acao:
        return
    assert allowed and origin is not None, f"ACAO {acao} sent for non-allowed origin {origin!r}"
    assert acao == [origin]
    assert r.vary_has_origin()


def assert_refused_put(r):
    assert r.status == 403, (r.status, r.body[:200])
    assert (r.one("Content-Type") or "").startswith("application/json")
    d = r.json()
    assert d["ok"] is False and isinstance(d["error"], str) and d["error"]
    assert r.cors_names() == []
    assert r.one("Content-Length") is not None


# --------------------------------------------------------------------------
# grid rows
# --------------------------------------------------------------------------

# (id, [Origin header values as templates], allowed?)
ORIGIN_ROWS = [
    ("own127", ["http://127.0.0.1:{P}"], True),
    ("ownlocalhost", ["http://localhost:{P}"], True),
    ("configured", ["https://viz.example.com"], True),
    ("configured_port", ["http://viz.example.com:8080"], True),
    ("trailing_slash", ["http://127.0.0.1:{P}/"], False),
    ("upper_scheme", ["HTTP://127.0.0.1:{P}"], False),
    ("upper_host", ["http://LOCALHOST:{P}"], False),
    ("https_scheme", ["https://127.0.0.1:{P}"], False),
    ("other_port", ["http://127.0.0.1:{P1}"], False),
    ("port_suffix", ["http://127.0.0.1:{P}.evil.example"], False),
    ("host_suffix", ["http://127.0.0.1.evil.example:{P}"], False),
    ("userinfo", ["http://evil.example@127.0.0.1:{P}"], False),
    ("ipv6", ["http://[::1]:{P}"], False),
    ("evil", ["http://evil.example"], False),
    ("null", ["null"], False),
    ("empty", [""], False),
    ("dup_ok_foreign", ["http://127.0.0.1:{P}", "http://evil.example"], False),
    ("dup_foreign_ok", ["http://evil.example", "http://127.0.0.1:{P}"], False),
    ("dup_same", ["http://127.0.0.1:{P}", "http://127.0.0.1:{P}"], False),
    ("conf_trailing_slash", ["https://viz.example.com/"], False),
    ("conf_other_scheme", ["http://viz.example.com"], False),
    ("conf_upper", ["https://VIZ.example.com"], False),
    ("conf_default_port", ["https://viz.example.com:443"], False),
    ("junk_in_front", ["xhttp://127.0.0.1:{P}"], False),
    ("junk_in_front_url", ["http://evil.example/http://127.0.0.1:{P}"], False),
]
ORIGIN_IDS = [r[0] for r in ORIGIN_ROWS]


def oh(templates, port):
    return [("Origin", fmt(t, port)) for t in templates]


# (id, Host template or None for missing, ok?)
HOST_ROWS = [
    ("own127", "127.0.0.1:{P}", True),
    ("localhost", "localhost:{P}", True),
    ("other_port", "127.0.0.1:{P1}", False),
    ("noport127", "127.0.0.1", False),
    ("noport_localhost", "localhost", False),
    ("LOCALHOST", "LOCALHOST:{P}", False),
    ("port_suffix", "127.0.0.1:{P}.evil.example", False),
    ("evil", "evil.example", False),
    ("evil_port", "evil.example:{P}", False),
    ("ipv6", "[::1]:{P}", False),
    ("script", "<script>alert(1)</script>", False),
    ("quote_backslash", 'a"b\\c', False),
    ("long", "a" * 5000, False),
    ("empty", "", False),
    ("trailing_space", "127.0.0.1:{P} ", False),
    ("tunnel", "viz.example.com", True),
    ("tunnel_port", "viz.example.com:8080", True),
    ("tunnel_default_port", "viz.example.com:443", False),
    ("tunnel_upper", "VIZ.example.com", False),
    ("missing10", None, False),
    ("missing11", None, False),
]
HOST_SUBSET = ["own127", "evil", "missing10", "script", "long"]
HOST_BY_ID = {r[0]: r for r in HOST_ROWS}


def host_kwargs(row_id, port):
    _, tmpl, _ = HOST_BY_ID[row_id]
    if tmpl is None:
        return {"host": None, "http10": row_id == "missing10"}
    return {"host": fmt(tmpl, port)}


def expected_host_error(row_id, port):
    _, tmpl, _ = HOST_BY_ID[row_id]
    value = "(missing)" if tmpl is None else fmt(tmpl, port)[:200]
    return f"Host not allowed: {value}; add it to viz.allowed_origins"


# --------------------------------------------------------------------------
# C1 no wildcard / no credentials
# --------------------------------------------------------------------------

class TestNoWildcard:
    """C1, one named cell per wildcard SITE so a restored wildcard at one site reddens that site's cells."""

    @pytest.mark.parametrize("origin", ["own", "foreign", "null", "none"])
    def test_preflight_no_wildcard(self, env, origin):
        hdrs = {"own": [("Origin", env.viz.own())], "foreign": [("Origin", "http://evil.example")],
                "null": [("Origin", "null")], "none": []}[origin]
        r = env.viz.request("OPTIONS", "/api/config/" + KEY, hdrs +
                            [("Access-Control-Request-Method", "PUT")])
        assert_no_wildcard_no_credentials(r)
        assert r.status in (204, 403)

    @pytest.mark.parametrize("pk", ["json200", "json_error"])
    @pytest.mark.parametrize("origin", ["own", "foreign", "null", "none"])
    def test_viz_json_no_wildcard(self, env, pk, origin):
        hdrs = {"own": [("Origin", env.viz.own())], "foreign": [("Origin", "http://evil.example")],
                "null": [("Origin", "null")], "none": []}[origin]
        r = env.viz.request("GET", VIZ_PATHS[pk], hdrs)
        assert_no_wildcard_no_credentials(r)
        assert r.status == (200 if pk == "json200" else 404)

    @pytest.mark.parametrize("pk", ["json200", "json_error"])
    @pytest.mark.parametrize("origin", ["own", "foreign", "null", "none"])
    def test_workbench_json_no_wildcard(self, env, pk, origin):
        hdrs = {"own": [("Origin", env.wb.own())], "foreign": [("Origin", "http://evil.example")],
                "null": [("Origin", "null")], "none": []}[origin]
        r = env.wb.request("GET", WB_PATHS[pk], hdrs)
        assert_no_wildcard_no_credentials(r)
        assert r.status == (200 if pk == "json200" else 404)

    @pytest.mark.parametrize("status_case", ["bad_value_400", "unknown_key_400", "bad_path_404", "refused_403",
                                             "server_error_500"])
    def test_put_error_statuses_no_wildcard(self, env, status_case, monkeypatch):
        good = own_put_headers(env.viz.port)
        if status_case == "bad_value_400":
            r = put(env.viz, good, value="banana")
            assert r.status == 400
        elif status_case == "unknown_key_400":
            r = put(env.viz, good, key="totally.fake.key")
            assert r.status == 400
        elif status_case == "bad_path_404":
            r = put(env.viz, good, path="/api/other")
            assert r.status == 404
        elif status_case == "refused_403":
            r = put(env.viz, [("Origin", "http://evil.example"), ("X-Engram-Viz", "1")])
            assert r.status == 403
        else:
            def boom(*a, **k):
                raise RuntimeError("boom")
            monkeypatch.setattr(viz_config_data, "write_config_key", boom)
            r = put(env.viz, good)
            assert r.status == 500
        assert_no_wildcard_no_credentials(r)


class TestNoCredentialsHeader:
    """Allow-Credentials is never sent (controls on the old code: nothing sends it today; the
    Allow-Credentials-added mutants are what these cells kill)."""

    @pytest.mark.parametrize("pk", list(VIZ_PATHS))
    @pytest.mark.parametrize("origin", ["own", "foreign", "none"])
    def test_no_credentials_any_response(self, srv, pk, origin):
        hdrs = {"own": [("Origin", srv.own())], "foreign": [("Origin", "http://evil.example")],
                "none": []}[origin]
        r = srv.request("GET", srv.paths[pk], hdrs)
        assert r.get("Access-Control-Allow-Credentials") == []

    @pytest.mark.parametrize("origin", ["own", "foreign", "none"])
    def test_no_credentials_preflight(self, env, origin):
        hdrs = {"own": [("Origin", env.viz.own())], "foreign": [("Origin", "http://evil.example")],
                "none": []}[origin]
        r = env.viz.request("OPTIONS", "/api/config/" + KEY, hdrs)
        assert r.get("Access-Control-Allow-Credentials") == []

    @pytest.mark.parametrize("case", ["ok", "refused", "bad_value", "bad_path"])
    def test_no_credentials_put(self, env, case):
        good = own_put_headers(env.viz.port)
        if case == "ok":
            r = put(env.viz, good)
        elif case == "refused":
            r = put(env.viz, [("Origin", "http://evil.example"), ("X-Engram-Viz", "1")])
        elif case == "bad_value":
            r = put(env.viz, good, value="banana")
        else:
            r = put(env.viz, good, path="/api/other")
        assert r.get("Access-Control-Allow-Credentials") == []

    @pytest.mark.parametrize("row", ["evil", "missing10"])
    def test_no_credentials_host_refusal(self, srv, row):
        r = srv.request("GET", srv.paths["json200"], [("Origin", srv.own())], **host_kwargs(row, srv.port))
        assert r.status == 403
        assert r.get("Access-Control-Allow-Credentials") == []


class TestPathKinds:
    """C1 + C2 + C3 across path kinds: no wildcard, and an ACAO is only ever an allowed Origin echoed."""

    @pytest.mark.parametrize("pk", list(VIZ_PATHS))
    @pytest.mark.parametrize("origin", ["own", "foreign", "null", "none"])
    def test_acao_invariant(self, srv, pk, origin):
        o = {"own": srv.own(), "foreign": "http://evil.example", "null": "null", "none": None}[origin]
        r = srv.request("GET", srv.paths[pk], [] if o is None else [("Origin", o)])
        assert_no_wildcard_no_credentials(r)
        assert_acao_invariant(r, o, origin == "own")
        if origin != "own":
            assert r.cors_names() == [], r.cors_names()

    @pytest.mark.parametrize("pk", list(VIZ_PATHS))
    def test_foreign_origin_served_exactly_as_without_origin(self, srv, pk):
        base = srv.request("GET", srv.paths[pk])
        foreign = srv.request("GET", srv.paths[pk], [("Origin", "http://evil.example")])
        assert foreign.status == base.status
        assert foreign.body == base.body


class TestUnsupportedVerbs:
    @pytest.mark.parametrize("verb", ["POST", "DELETE"])
    @pytest.mark.parametrize("origin", ["own", "foreign", "none"])
    def test_unsupported_verb_no_wildcard_and_still_501(self, srv, verb, origin):
        o = {"own": srv.own(), "foreign": "http://evil.example", "none": None}[origin]
        r = srv.request(verb, "/api/agents", [] if o is None else [("Origin", o)])
        assert r.status == 501
        assert_no_wildcard_no_credentials(r)
        if origin != "own":
            assert r.cors_names() == []

    @pytest.mark.parametrize("origin", ["own", "foreign"])
    def test_workbench_options_no_wildcard(self, env, origin):
        o = env.wb.own() if origin == "own" else "http://evil.example"
        r = env.wb.request("OPTIONS", "/api/agents", [("Origin", o)])
        assert_no_wildcard_no_credentials(r)


# --------------------------------------------------------------------------
# C2 / C3 reads
# --------------------------------------------------------------------------

class TestReadOrigin:
    @pytest.mark.parametrize("row", ORIGIN_ROWS, ids=ORIGIN_IDS)
    def test_origin_grid_on_json_read(self, srv, row):
        _, templates, allowed = row
        r = srv.request("GET", "/api/agents", oh(templates, srv.port))
        assert r.status == 200
        assert_no_wildcard_no_credentials(r)
        if allowed:
            assert r.get("Access-Control-Allow-Origin") == [fmt(templates[0], srv.port)]
            assert r.vary_has_origin()
        else:
            assert r.cors_names() == [], r.cors_names()

    def test_no_origin_read_has_no_cors_headers(self, srv):
        r = srv.request("GET", "/api/agents")
        assert r.status == 200
        assert r.cors_names() == []

    def test_foreign_origin_read_has_no_cors_headers_and_same_body(self, srv):
        base = srv.request("GET", "/api/agents")
        r = srv.request("GET", "/api/agents", [("Origin", "http://evil.example")])
        assert r.status == 200 and r.body == base.body
        assert r.cors_names() == []

    def test_acao_is_not_sent_for_default_empty_allowlist_tunnel(self, srv):
        set_configured([])
        r = srv.request("GET", "/api/agents", [("Origin", "https://viz.example.com")])
        assert r.cors_names() == []


# --------------------------------------------------------------------------
# C4 preflight
# --------------------------------------------------------------------------

def preflight(srv, headers, path="/api/config/" + KEY, acr=True):
    extra = ([("Access-Control-Request-Method", "PUT"),
              ("Access-Control-Request-Headers", "content-type,x-engram-viz")] if acr else [])
    return srv.request("OPTIONS", path, list(headers) + extra)


class TestPreflight:
    @pytest.mark.parametrize("row", ORIGIN_ROWS, ids=ORIGIN_IDS)
    def test_preflight_origin_grid(self, env, row):
        _, templates, allowed = row
        r = preflight(env.viz, oh(templates, env.viz.port))
        assert_no_wildcard_no_credentials(r)
        if allowed:
            assert r.status == 204
            assert r.get("Access-Control-Allow-Origin") == [fmt(templates[0], env.viz.port)]
            assert r.get("Access-Control-Allow-Methods") == ["GET, PUT, OPTIONS"]
            assert r.get("Access-Control-Allow-Headers") == ["Content-Type, X-Engram-Viz"]
            assert r.vary_has_origin()
        else:
            assert r.status == 403
            assert r.cors_names() == []
            assert r.one("Content-Length") is not None

    def test_preflight_without_origin_is_204_with_no_cors_headers(self, env):
        r = preflight(env.viz, [], acr=False)
        assert r.status == 204
        assert r.cors_names() == []

    def test_preflight_own_origin_without_request_headers_still_allowed(self, env):
        r = preflight(env.viz, [("Origin", env.viz.own())], acr=False)
        assert r.status == 204
        assert r.get("Access-Control-Allow-Origin") == [env.viz.own()]

    def test_allowed_preflight_names_the_custom_header(self, env):
        r = preflight(env.viz, [("Origin", env.viz.own())])
        assert "x-engram-viz" in [t.strip().lower() for t in r.one("Access-Control-Allow-Headers").split(",")]


# --------------------------------------------------------------------------
# C5 PUT
# --------------------------------------------------------------------------

class TestPutOrigin:
    @pytest.mark.parametrize("row", ORIGIN_ROWS, ids=ORIGIN_IDS)
    def test_origin_grid_on_put(self, env, row):
        _, templates, allowed = row
        before = cfg_bytes(env)
        r = put(env.viz, oh(templates, env.viz.port) + [("X-Engram-Viz", "1"),
                                                         ("Content-Type", "application/json")])
        assert_no_wildcard_no_credentials(r)
        if allowed:
            assert r.status == 200, r.body[:200]
            assert r.json()["ok"] is True
            assert r.get("Access-Control-Allow-Origin") == [fmt(templates[0], env.viz.port)]
            assert cfg_bytes(env) != before
        else:
            assert_refused_put(r)
            assert cfg_bytes(env) == before

    @pytest.mark.parametrize("referer", [None, "http://127.0.0.1:{P}/config"], ids=["no_referer", "own_referer"])
    def test_non_browser_without_origin_is_refused_unless_referer(self, env, referer):
        """Caller dimension: no Origin at all. With neither Origin nor Referer: refused."""
        before = cfg_bytes(env)
        hdrs = [("X-Engram-Viz", "1"), ("Content-Type", "application/json")]
        if referer:
            hdrs.append(("Referer", fmt(referer, env.viz.port)))
        r = put(env.viz, hdrs)
        if referer:
            assert r.status == 200
        else:
            assert_refused_put(r)
            assert cfg_bytes(env) == before

    @pytest.mark.parametrize("origin", ["http://evil.example", "null", ""])
    def test_foreign_origin_beats_matching_referer(self, env, origin):
        before = cfg_bytes(env)
        r = put(env.viz, [("Origin", origin), ("Referer", env.viz.own() + "/config"),
                          ("X-Engram-Viz", "1")])
        assert_refused_put(r)
        assert cfg_bytes(env) == before

    def test_origin_in_allowed_set_wins_over_foreign_referer(self, env):
        r = put(env.viz, [("Origin", env.viz.own()), ("Referer", "http://evil.example/x"),
                          ("X-Engram-Viz", "1")])
        assert r.status == 200

    def test_header_without_any_origin_proof_cannot_be_enough(self, env):
        before = cfg_bytes(env)
        r = put(env.viz, [("X-Engram-Viz", "1")])
        assert_refused_put(r)
        assert cfg_bytes(env) == before

    def test_origin_without_header_cannot_be_enough(self, env):
        before = cfg_bytes(env)
        r = put(env.viz, [("Origin", env.viz.own())])
        assert_refused_put(r)
        assert cfg_bytes(env) == before


REFERER_ROWS = [
    ("own_path", "http://127.0.0.1:{P}/config", True),
    ("own_equal", "http://127.0.0.1:{P}", True),
    ("own_slash", "http://127.0.0.1:{P}/", True),
    ("localhost_path", "http://localhost:{P}/config?x=1", True),
    ("configured_path", "https://viz.example.com/config", True),
    ("port_suffix", "http://127.0.0.1:{P}.evil.example/config", False),
    ("userinfo", "http://127.0.0.1:{P}@evil.example/", False),
    ("in_query", "http://evil.example/?http://127.0.0.1:{P}/", False),
    ("other_port", "http://127.0.0.1:{P1}/config", False),
    ("upper_scheme", "HTTP://127.0.0.1:{P}/config", False),
    ("empty", "", False),
    ("evil", "http://evil.example/config", False),
]


class TestPutReferer:
    @pytest.mark.parametrize("row", REFERER_ROWS, ids=[r[0] for r in REFERER_ROWS])
    def test_referer_grid_when_origin_absent(self, env, row):
        _, tmpl, ok = row
        before = cfg_bytes(env)
        r = put(env.viz, [("Referer", fmt(tmpl, env.viz.port)), ("X-Engram-Viz", "1")])
        if ok:
            assert r.status == 200, r.body[:200]
            assert cfg_bytes(env) != before
        else:
            assert_refused_put(r)
            assert cfg_bytes(env) == before

    @pytest.mark.parametrize("refs", [("own", "evil"), ("evil", "own"), ("own", "own")],
                             ids=["good_then_evil", "evil_then_good", "good_twice"])
    def test_two_referer_headers_are_refused(self, env, refs):
        before = cfg_bytes(env)
        pick = {"own": env.viz.own() + "/config", "evil": "http://evil.example/config"}
        r = put(env.viz, [("Referer", pick[refs[0]]), ("Referer", pick[refs[1]]), ("X-Engram-Viz", "1")])
        assert_refused_put(r)
        assert cfg_bytes(env) == before

    def test_referer_is_not_enough_without_the_header(self, env):
        before = cfg_bytes(env)
        r = put(env.viz, [("Referer", env.viz.own() + "/config")])
        assert_refused_put(r)
        assert cfg_bytes(env) == before


HEADER_ROWS = [
    ("one", [("X-Engram-Viz", "1")], True),
    ("lower_name", [("x-engram-viz", "1")], True),
    ("upper_name", [("X-ENGRAM-VIZ", "1")], True),
    ("absent", [], False),
    ("zero", [("X-Engram-Viz", "0")], False),
    ("true", [("X-Engram-Viz", "true")], False),
    ("trailing_space", [("X-Engram-Viz", "1 ")], False),
    ("empty", [("X-Engram-Viz", "")], False),
    ("dup_one_zero", [("X-Engram-Viz", "1"), ("X-Engram-Viz", "0")], False),
    ("dup_zero_one", [("X-Engram-Viz", "0"), ("X-Engram-Viz", "1")], False),
]


class TestPutHeader:
    @pytest.mark.parametrize("row", HEADER_ROWS, ids=[r[0] for r in HEADER_ROWS])
    def test_header_grid(self, env, row):
        _, hdr, ok = row
        before = cfg_bytes(env)
        r = put(env.viz, [("Origin", env.viz.own()), ("Content-Type", "application/json")] + hdr)
        if ok:
            assert r.status == 200, r.body[:200]
            assert cfg_bytes(env) != before
        else:
            assert_refused_put(r)
            assert cfg_bytes(env) == before

    @pytest.mark.parametrize("hdr", [[], [("X-Engram-Viz", "0")]], ids=["absent", "zero"])
    @pytest.mark.parametrize("body", [b"not json at all", b"", b'{"nope": 1}'], ids=["bad_json", "empty", "no_value"])
    def test_check_runs_before_the_body_is_read(self, env, hdr, body):
        before = cfg_bytes(env)
        r = put(env.viz, [("Origin", env.viz.own())] + hdr, body=body)
        assert_refused_put(r)
        assert cfg_bytes(env) == before

    @pytest.mark.parametrize("hdr", [[("Origin", "http://evil.example"), ("X-Engram-Viz", "1")], []],
                             ids=["foreign_origin", "no_proofs"])
    def test_origin_check_runs_before_the_body_is_read(self, env, hdr):
        r = put(env.viz, hdr, body=b"not json at all")
        assert_refused_put(r)

    def test_check_runs_before_routing(self, env):
        r = put(env.viz, [("Origin", "http://evil.example")], path="/api/other")
        assert_refused_put(r)
        r2 = put(env.viz, own_put_headers(env.viz.port), path="/api/other")
        assert r2.status == 404 and r2.json() == {"ok": False, "error": "not found"}


SFS_ROWS = [
    ("absent", None, True),
    ("same_origin", "same-origin", True),
    ("cross_site", "cross-site", False),
    ("same_site", "same-site", False),
    ("none", "none", False),
    ("case", "Same-Origin", False),
    ("empty", "", False),
    ("junk", "banana", False),
]


class TestPutSecFetchSite:
    @pytest.mark.parametrize("row", SFS_ROWS, ids=[r[0] for r in SFS_ROWS])
    def test_sec_fetch_site_grid_with_otherwise_good_request(self, env, row):
        _, val, ok = row
        before = cfg_bytes(env)
        hdrs = own_put_headers(env.viz.port) + ([] if val is None else [("Sec-Fetch-Site", val)])
        r = put(env.viz, hdrs)
        if ok:
            assert r.status == 200, r.body[:200]
            assert cfg_bytes(env) != before
        else:
            assert_refused_put(r)
            assert cfg_bytes(env) == before

    def test_cross_site_with_a_perfect_request_is_refused(self, env):
        before = cfg_bytes(env)
        r = put(env.viz, own_put_headers(env.viz.port) + [("Sec-Fetch-Site", "cross-site")])
        assert_refused_put(r)
        assert cfg_bytes(env) == before

    def test_sec_fetch_site_alone_never_authorises(self, env):
        before = cfg_bytes(env)
        r = put(env.viz, [("Sec-Fetch-Site", "same-origin")])
        assert_refused_put(r)
        assert cfg_bytes(env) == before

    def test_duplicate_sec_fetch_site_is_refused(self, env):
        r = put(env.viz, own_put_headers(env.viz.port) + [("Sec-Fetch-Site", "same-origin"),
                                                         ("Sec-Fetch-Site", "cross-site")])
        assert_refused_put(r)

    def test_referer_only_put_with_cross_site_is_refused(self, env):
        r = put(env.viz, [("Referer", env.viz.own() + "/config"), ("X-Engram-Viz", "1"),
                          ("Sec-Fetch-Site", "cross-site")])
        assert_refused_put(r)

    @pytest.mark.parametrize("verb", ["GET", "OPTIONS"])
    def test_clause_is_for_put_only(self, env, verb):
        r = env.viz.request(verb, "/api/agents", [("Origin", env.viz.own()), ("Sec-Fetch-Site", "cross-site")])
        assert r.status == (200 if verb == "GET" else 204)


class TestPutUnchanged:
    """C5 last sentence and C9: a request that passes behaves exactly as before."""

    def test_valid_write_shape(self, env):
        r = put(env.viz, own_put_headers(env.viz.port))
        assert r.status == 200
        d = r.json()
        assert d["ok"] is True and d["validated_value"] == VAL and d["restart_required"] is False
        assert json.loads(cfg_bytes(env))["cadence"]["drowsiness_caution_pct"] == VAL

    def test_agent_param_selects_the_agent_written(self, env):
        before_test = cfg_bytes(env, "test")
        r = put(env.viz, own_put_headers(env.viz.port), agent="other")
        assert r.status == 200
        assert json.loads(cfg_bytes(env, "other"))["cadence"]["drowsiness_caution_pct"] == VAL
        assert cfg_bytes(env, "test") == before_test

    def test_the_page_style_put_gets_200(self, env):
        """C7 end to end: drive a PUT the way config.html does (own Origin, the header, JSON content type)."""
        r = env.viz.request("PUT", f"/api/config/{KEY}?agent=test",
                            [("Content-Type", "application/json"), ("Origin", env.viz.own()),
                             ("X-Engram-Viz", "1")], body=json.dumps({"value": 55}).encode())
        assert r.status == 200 and r.json()["validated_value"] == 55

    @pytest.mark.parametrize("case,expect", [
        ("bad_value", 400), ("unknown_key", 400), ("unknown_agent", 400), ("bad_path", 404),
        ("no_key", 400), ("empty_body", 400), ("bad_json", 400), ("no_value_field", 400)])
    def test_error_shapes_unchanged_for_a_proven_request(self, env, case, expect):
        good = own_put_headers(env.viz.port)
        kw = {}
        if case == "bad_value":
            kw = dict(value="banana")
        elif case == "unknown_key":
            kw = dict(key="totally.fake.key")
        elif case == "unknown_agent":
            kw = dict(agent="no_such_agent")
        elif case == "bad_path":
            kw = dict(path="/api/other")
        elif case == "no_key":
            kw = dict(path="/api/config/")
        elif case == "empty_body":
            kw = dict(body=b"")
        elif case == "bad_json":
            kw = dict(body=b"{nope")
        elif case == "no_value_field":
            kw = dict(body=b'{"x": 1}')
        r = put(env.viz, good, **kw)
        assert r.status == expect, (case, r.status, r.body[:200])
        assert r.json()["ok"] is False

    def test_internal_error_shape_unchanged(self, env, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(viz_config_data, "write_config_key", boom)
        r = put(env.viz, own_put_headers(env.viz.port))
        assert r.status == 500 and r.json()["error"].startswith("internal error")


# --------------------------------------------------------------------------
# C6 viz.allowed_origins
# --------------------------------------------------------------------------

VALID_LISTS = [[], ["https://viz.example.com"], ["http://example.com:8080"], ["http://127.0.0.1:9", "https://a.b"]]
INVALID_VALUES = [
    ["*"], ["http://*.example"], ["http://example.com/"], ["http://u@example.com"], ["example.com"],
    ["ftp://example.com"], [""], "http://example.com", 5, None, [5], ["http://example.com", 5],
    ["http://example.com/path"], ["http://example.com?x=1"], ["http://example.com#f"], ["http://"],
    ["http://:80"], ["HTTP://example.com"], ["http://example.com "], ["http://exa mple.com"],
    ["https://*"], ["*://example.com"],
]


@pytest.fixture
def key_made_editable(monkeypatch):
    """Grammar cells: the entry check lives in the validator, which refuses a read-only key first, so these
    cells flip the schema row to editable for their own duration only."""
    row = viz_config_data._schema_index()["viz.allowed_origins"]
    monkeypatch.setitem(row, "editable", True)


class TestAllowedOriginsKey:
    @pytest.mark.parametrize("value", VALID_LISTS, ids=[json.dumps(v) for v in VALID_LISTS])
    def test_valid_values_pass_the_entry_check_and_need_a_restart(self, env, key_made_editable, value):
        res = viz_config_data.write_config_key("test", "viz.allowed_origins", value)
        assert res["ok"] is True, res
        assert res["restart_required"] is True
        assert json.loads(cfg_bytes(env))["viz"]["allowed_origins"] == value

    @pytest.mark.parametrize("value", INVALID_VALUES, ids=[json.dumps(v) for v in INVALID_VALUES])
    def test_invalid_values_rejected_for_the_entry_not_for_an_unknown_key(self, env, key_made_editable, value):
        before = cfg_bytes(env)
        res = viz_config_data.write_config_key("test", "viz.allowed_origins", value)
        assert res["ok"] is False
        assert "Unknown config key" not in res["error"], res
        assert "viz.allowed_origins" in res["error"]
        assert cfg_bytes(env) == before

    @pytest.mark.parametrize("value", [[], ["https://later.example"], ["*"], "x"], ids=["empty", "valid", "bad", "str"])
    def test_the_key_is_read_only_over_the_write_function(self, env, value):
        """Zephyr's ruling on R3: a same-origin PUT must not be able to persist an allowed origin."""
        before = cfg_bytes(env)
        res = viz_config_data.write_config_key("test", "viz.allowed_origins", value)
        assert res["ok"] is False
        assert "viz.allowed_origins" in res["error"] and "read-only" in res["error"], res
        assert cfg_bytes(env) == before

    def test_http_put_of_the_key_is_refused_as_read_only(self, env):
        before = cfg_bytes(env)
        r = put(env.viz, own_put_headers(env.viz.port), key="viz.allowed_origins", value=["https://later.example"])
        assert r.status == 400 and r.json()["ok"] is False
        assert "viz.allowed_origins" in r.json()["error"] and "read-only" in r.json()["error"]
        assert cfg_bytes(env) == before

    def test_key_is_in_the_schema_as_a_read_only_list_default_empty(self):
        from tools.config_schema import SCHEMA
        rows = [r for r in SCHEMA if r["key"] == "viz.allowed_origins"]
        assert len(rows) == 1
        assert rows[0]["type"] == "list" and rows[0]["editable"] is False
        assert rows[0]["restart_required"] is True
        assert rows[0].get("default", []) == []

    def test_a_refused_put_does_not_widen_the_running_server(self, env, srv):
        set_configured([])
        good = own_put_headers(env.viz.port)
        assert put(env.viz, good, key="viz.allowed_origins", value=["https://later.example"]).status == 400
        r = srv.request("GET", "/api/agents", [("Origin", "https://later.example")])
        assert r.cors_names() == []
        r2 = srv.request("GET", "/api/agents", host="later.example")
        assert r2.status == 403
        assert "allowed_origins" not in json.loads(cfg_bytes(env)).get("viz", {})


class TestConfiguredOriginsSource:
    def _home(self, tmp_path, doc):
        h = tmp_path / "proc_home"
        h.mkdir(exist_ok=True)
        (h / "config.json").write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return h

    def test_loaded_from_the_serving_process_config_not_the_agents(self, env, srv, tmp_path, monkeypatch):
        h = self._home(tmp_path, {"viz": {"allowed_origins": ["https://proc.example"]}})
        # the ?agent= agent's own config tries to widen the list
        env.cfg["test"].write_text(json.dumps({"viz": {"allowed_origins": ["https://agent.example"]}}))
        monkeypatch.setenv("ENGRAM_HOME", str(h))
        got = _cors().load_configured_origins()
        assert got == ["https://proc.example"]
        r = srv.request("GET", "/api/agents?agent=test", [("Origin", "https://proc.example")])
        assert r.get("Access-Control-Allow-Origin") == ["https://proc.example"]
        r = srv.request("GET", "/api/agents?agent=test", [("Origin", "https://agent.example")])
        assert r.cors_names() == []

    def test_invalid_entries_in_a_hand_edited_file_are_dropped(self, tmp_path, monkeypatch):
        h = self._home(tmp_path, {"viz": {"allowed_origins": ["*", "http://x.example/", "https://ok.example", 5]}})
        monkeypatch.setenv("ENGRAM_HOME", str(h))
        assert _cors().load_configured_origins() == ["https://ok.example"]

    @pytest.mark.parametrize("doc", ["{not json", {}, {"viz": 5}, {"viz": {"allowed_origins": "https://a.b"}}],
                             ids=["bad_json", "no_viz", "viz_not_dict", "not_a_list"])
    def test_missing_or_malformed_config_gives_empty_list_and_never_raises(self, tmp_path, monkeypatch, doc):
        h = self._home(tmp_path, doc)
        monkeypatch.setenv("ENGRAM_HOME", str(h))
        assert _cors().load_configured_origins() == []

    def test_absent_config_file_gives_empty_list(self, tmp_path, monkeypatch):
        (tmp_path / "empty_home").mkdir()
        monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / "empty_home"))
        assert _cors().load_configured_origins() == []


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestMainWiring:
    """The real entry points read the key once, from the process's own ENGRAM_HOME config, and feed
    BOTH the Origin and the Host check (viz.server and viz.workbench each as a real process)."""

    @pytest.mark.parametrize("module", ["viz.server", "viz.workbench"])
    def test_real_process_honours_the_configured_origin_and_host(self, tmp_path, module):
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"viz": {"allowed_origins": ["https://viz.example.com"]}}))
        (home / "knowledge.db").write_bytes(b"")
        port = _free_port()
        penv = dict(os.environ, ENGRAM_HOME=str(home), PYTHONPATH=str(SRC_ENGRAM), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, "-m", module, "--port", str(port), "--db", str(home / "knowledge.db")],
                                env=penv, cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"{module} exited early with {proc.returncode}")
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                pytest.fail(f"{module} did not start")
            s = Srv(module, type("S", (), {"server_address": ("127.0.0.1", port)})(), {})
            ok = s.request("GET", "/api/agents", [("Origin", "https://viz.example.com")], host="viz.example.com")
            assert ok.status == 200
            assert ok.get("Access-Control-Allow-Origin") == ["https://viz.example.com"]
            foreign = s.request("GET", "/api/agents", [("Origin", "http://evil.example")])
            assert foreign.status == 200 and foreign.cors_names() == []
            bad_host = s.request("GET", "/api/agents", host="evil.example")
            assert bad_host.status == 403
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    def test_both_entry_points_call_the_shared_loader(self):
        import inspect
        for mod in (viz_server, viz_workbench):
            assert "load_configured_origins" in inspect.getsource(mod.main), mod.__name__


# --------------------------------------------------------------------------
# C7 the UI keeps working
# --------------------------------------------------------------------------

class TestTemplate:
    TEMPLATE = SRC_ENGRAM / "viz" / "templates" / "config.html"

    def test_both_put_fetches_send_the_header(self):
        html = self.TEMPLATE.read_text(encoding="utf-8")
        blocks = re.findall(r"method:\s*'PUT',\s*headers:\s*\{([^}]*)\}", html)
        assert len(blocks) == 2, f"expected exactly two PUT fetches with a headers literal, got {len(blocks)}"
        assert len(re.findall(r"""['"]PUT['"]""", html)) == 2
        for b in blocks:
            assert re.search(r"""['"]X-Engram-Viz['"]\s*:\s*['"]1['"]""", b), b
            assert "Content-Type" in b


# --------------------------------------------------------------------------
# C8 one shared helper
# --------------------------------------------------------------------------

class TestSharedHelper:
    def test_both_handlers_use_classes_from_the_one_helper_module(self):
        cors = _cors()
        for h in (viz_server.Handler, viz_workbench.Handler):
            assert any(c.__module__ == cors.__name__ for c in h.__mro__), h


# --------------------------------------------------------------------------
# C9 unchanged
# --------------------------------------------------------------------------

class TestUnchangedReads:
    def test_agents_payload(self, srv):
        r = srv.request("GET", "/api/agents")
        d = r.json()
        assert r.status == 200 and d["default"] == "test"
        assert sorted(a["name"] for a in d["agents"]) == ["other", "test"]

    @pytest.mark.parametrize("agent", ["test", "other"])
    def test_agent_param_config_read_unchanged(self, env, agent):
        r = env.viz.request("GET", f"/api/config?agent={agent}")
        expected = json.loads(json.dumps(viz_config_data.get_config_data(agent), default=str))
        assert r.status == 200 and r.json() == expected
        assert r.json()["config_path"] == str(env.cfg[agent])

    def test_html_static_and_unknown(self, srv):
        h = srv.request("GET", srv.paths["html"])
        assert h.status == 200 and h.one("Content-Type").startswith("text/html")
        s = srv.request("GET", srv.paths["static"])
        assert s.status == 200
        assert s.body == (SRC_ENGRAM / "viz" / "static" / Path(srv.paths["static"]).name).read_bytes()
        u = srv.request("GET", srv.paths["unknown"])
        assert u.status == 404 and u.body == b""

    def test_options_on_viz_without_origin_is_204(self, env):
        assert env.viz.request("OPTIONS", "/api/agents").status == 204


# --------------------------------------------------------------------------
# C10 Host rule
# --------------------------------------------------------------------------

def _check_host_row(srv, row_id, method, path, headers=(), body=None, baseline_status=None):
    ok = HOST_BY_ID[row_id][2]
    r = srv.request(method, path, headers, body=body, **host_kwargs(row_id, srv.port))
    if ok:
        assert r.status != 403 or r.one("Content-Type") != "application/json" or \
            not str(r.json().get("error", "")).startswith("Host not allowed"), (row_id, r.status)
        if baseline_status is not None:
            assert r.status == baseline_status
    else:
        assert r.status == 403, (row_id, r.status, r.body[:100])
        assert (r.one("Content-Type") or "").startswith("application/json")
        assert r.json() == {"ok": False, "error": expected_host_error(row_id, srv.port)}
        assert r.cors_names() == []
        assert r.one("Content-Length") is not None
    return r


class TestHostRule:
    @pytest.mark.parametrize("row", [r[0] for r in HOST_ROWS])
    def test_host_grid_on_json_get(self, srv, row):
        r = _check_host_row(srv, row, "GET", "/api/agents", baseline_status=200)
        if HOST_BY_ID[row][2]:
            assert r.status == 200

    @pytest.mark.parametrize("row", HOST_SUBSET)
    @pytest.mark.parametrize("pk", ["json_error", "html", "static", "unknown"])
    def test_every_path_kind(self, srv, pk, row):
        base = srv.request("GET", srv.paths[pk])
        _check_host_row(srv, row, "GET", srv.paths[pk], baseline_status=base.status)

    @pytest.mark.parametrize("row", HOST_SUBSET)
    @pytest.mark.parametrize("verb", ["OPTIONS", "POST", "DELETE"])
    def test_every_other_verb(self, srv, verb, row):
        base = srv.request(verb, "/api/agents", [("Origin", srv.own())])
        _check_host_row(srv, row, verb, "/api/agents", [("Origin", srv.own())], baseline_status=base.status)

    @pytest.mark.parametrize("row", [r[0] for r in HOST_ROWS])
    def test_host_grid_on_put_with_every_proof_present(self, env, row):
        before = cfg_bytes(env)
        r = _check_host_row(env.viz, row, "PUT", f"/api/config/{KEY}?agent=test",
                            own_put_headers(env.viz.port), body=json.dumps({"value": VAL}).encode())
        if HOST_BY_ID[row][2]:
            assert r.status == 200
            assert cfg_bytes(env) != before
        else:
            assert cfg_bytes(env) == before

    @pytest.mark.parametrize("row", HOST_SUBSET)
    def test_workbench_put_is_refused_on_host_before_it_is_unsupported(self, env, row):
        _check_host_row(env.wb, row, "PUT", "/api/agents", [("Origin", env.wb.own())], body=b"{}")

    @pytest.mark.parametrize("hdrs", [["127.0.0.1:{P}", "evil.example"], ["evil.example", "127.0.0.1:{P}"]],
                             ids=["ok_then_evil", "evil_then_ok"])
    def test_duplicate_host_headers_are_refused(self, srv, hdrs):
        r = srv.request("GET", "/api/agents", [("Host", fmt(h, srv.port)) for h in hdrs], host=None)
        assert r.status == 403
        assert r.json()["error"].startswith("Host not allowed: ")

    def test_refusal_with_origin_present_carries_no_cors_headers(self, srv):
        r = srv.request("GET", "/api/agents", [("Origin", srv.own())], host="evil.example")
        assert r.status == 403 and r.cors_names() == []

    def test_tunnel_host_reaches_both_checks(self, env, srv):
        r = srv.request("GET", "/api/agents", [("Origin", "https://viz.example.com")], host="viz.example.com")
        assert r.status == 200
        assert r.get("Access-Control-Allow-Origin") == ["https://viz.example.com"]
        p = env.viz.request("PUT", f"/api/config/{KEY}?agent=test",
                            [("Origin", "https://viz.example.com"), ("X-Engram-Viz", "1"),
                             ("Content-Type", "application/json")],
                            body=json.dumps({"value": VAL}).encode(), host="viz.example.com")
        assert p.status == 200
        q = env.viz.request("PUT", f"/api/config/{KEY}?agent=test",
                            [("Referer", "https://viz.example.com/config"), ("X-Engram-Viz", "1")],
                            body=json.dumps({"value": 66}).encode(), host="viz.example.com")
        assert q.status == 200

    def test_tunnel_host_is_refused_when_nothing_is_configured(self, srv):
        set_configured([])
        r = srv.request("GET", "/api/agents", host="viz.example.com")
        assert r.status == 403
        assert r.json()["error"] == "Host not allowed: viz.example.com; add it to viz.allowed_origins"

    def test_refusal_body_is_truncated_and_escaped_json(self, srv):
        r = srv.request("GET", "/api/agents", host="a" * 5000)
        assert r.status == 403
        assert r.json()["error"] == f"Host not allowed: {'a' * 200}; add it to viz.allowed_origins"
        assert len(r.body) < 600
        r = srv.request("GET", "/api/agents", host='x"y\\z')
        assert r.json()["error"] == 'Host not allowed: x"y\\z; add it to viz.allowed_origins'


# --------------------------------------------------------------------------
# Two dimensions the cells above held constant (found by the owner's own mutants, #4441):
#  * Accept-Encoding: every cell above omits it, so viz.server._json never took its gzip branch;
#    a real browser sends it on every fetch, so that branch is the one a browser actually gets.
#  * Connection: every cell above sends "Connection: close", which makes http.server close the
#    connection by itself; nothing pinned that a REFUSAL closes a keep-alive connection (the PUT
#    body of a refused request is deliberately not read, so the connection must not be reused).
# --------------------------------------------------------------------------

GZ_ROWS = [
    ("own127", "http://127.0.0.1:{P}", True),
    ("ownlocalhost", "http://localhost:{P}", True),
    ("configured", "https://viz.example.com", True),
    ("evil", "http://evil.example", False),
    ("null", "null", False),
    ("no_origin", None, False),
]


class TestGzipJsonPath:
    @pytest.mark.parametrize("ae", ["gzip", "gzip, deflate, br"], ids=["gzip", "browser"])
    @pytest.mark.parametrize("pk", ["json200", "json_error"])
    @pytest.mark.parametrize("row", GZ_ROWS, ids=[r[0] for r in GZ_ROWS])
    def test_cors_headers_on_gzip_responses(self, env, row, pk, ae):
        _, tmpl, allowed = row
        hdrs = [("Accept-Encoding", ae)] + ([] if tmpl is None else [("Origin", fmt(tmpl, env.viz.port))])
        r = env.viz.request("GET", env.viz.paths[pk], hdrs)
        assert r.get("Content-Encoding") == ["gzip"], "the gzip branch of _json must be the one under test"
        json.loads(gzip.decompress(r.body))
        assert_no_wildcard_no_credentials(r)
        if allowed:
            assert r.get("Access-Control-Allow-Origin") == [fmt(tmpl, env.viz.port)]
            assert r.vary_has_origin()
        else:
            assert r.cors_names() == []

    def test_put_response_on_the_gzip_path(self, env):
        r = put(env.viz, own_put_headers(env.viz.port) + [("Accept-Encoding", "gzip, deflate, br")])
        assert r.get("Content-Encoding") == ["gzip"]
        assert json.loads(gzip.decompress(r.body))["ok"] is True
        assert_no_wildcard_no_credentials(r)
        assert r.get("Access-Control-Allow-Origin") == [env.viz.own()]


def _raw_exchange(srv, head_lines, body=b"", wait=3.0):
    """HTTP/1.1 with NO 'Connection: close' request header. Returns (Resp, closed): closed is True iff
    the server hung up after the response (EOF or reset) within *wait* seconds."""
    head = "\r\n".join(head_lines) + "\r\n\r\n"
    c = socket.create_connection(("127.0.0.1", srv.port), timeout=wait)
    buf, closed = b"", False
    try:
        c.sendall(head.encode("latin-1") + body)
        try:
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    closed = True
                    break
                buf += chunk
                h, sep, rest = buf.partition(b"\r\n\r\n")
                if sep:
                    cl = [int(ln.split(":", 1)[1]) for ln in h.decode("latin-1").split("\r\n")[1:]
                          if ln.lower().startswith("content-length:")]
                    if cl and len(rest) >= cl[0]:
                        closed = (c.recv(1) == b"")
                        break
        except socket.timeout:
            closed = False
        except ConnectionResetError:
            closed = True
    finally:
        c.close()
    return parse_response(buf), closed


class TestRefusalsCloseKeepAliveConnections:
    def test_put_proof_refusal_closes_the_connection(self, env):
        body = json.dumps({"value": VAL}).encode()
        r, closed = _raw_exchange(env.viz, [
            f"PUT /api/config/{KEY}?agent=test HTTP/1.1", f"Host: 127.0.0.1:{env.viz.port}",
            "Origin: http://evil.example", "X-Engram-Viz: 1", "Content-Type: application/json",
            f"Content-Length: {len(body)}"], body)
        assert r.status == 403
        assert [v.lower() for v in r.get("Connection")] == ["close"]
        assert closed

    def test_host_refusal_closes_the_connection(self, env):
        r, closed = _raw_exchange(env.viz, ["GET /api/agents HTTP/1.1", "Host: evil.example"])
        assert r.status == 403
        assert [v.lower() for v in r.get("Connection")] == ["close"]
        assert closed

    def test_host_refusal_of_a_put_with_a_body_closes_the_connection(self, env):
        body = b'{"value": 70}'
        r, closed = _raw_exchange(env.viz, [
            f"PUT /api/config/{KEY}?agent=test HTTP/1.1", "Host: evil.example",
            f"Content-Length: {len(body)}"], body)
        assert r.status == 403
        assert closed

    def test_control_an_accepted_request_keeps_the_connection_open(self, env):
        """Without this the cells above could pass because the detector always says closed."""
        r, closed = _raw_exchange(env.viz, ["GET /api/agents HTTP/1.1", f"Host: 127.0.0.1:{env.viz.port}"],
                                  wait=1.0)
        assert r.status == 200
        assert closed is False


# --------------------------------------------------------------------------
# Expect: 100-continue (reviewer-fairy r1): a refused request must not be invited to send its body.
# http.server answers `Expect: 100-continue` inside parse_request, BEFORE any do_<VERB>, so the Host
# rule and a PUT's same-origin proofs have to run before that answer.
# --------------------------------------------------------------------------

def _expect_exchange(srv, head_lines, body=None, wait=3.0):
    """Send only the head (a client with Expect waits). Returns (first status line, final Resp or None):
    when the server says 100 Continue and *body* is given, the body is sent and the final response read."""
    head = "\r\n".join(list(head_lines) + ["Connection: close"]) + "\r\n\r\n"
    c = socket.create_connection(("127.0.0.1", srv.port), timeout=wait)
    try:
        c.sendall(head.encode("latin-1"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
        first = buf.split(b"\r\n", 1)[0].decode("latin-1")
        final = None
        if first.startswith("HTTP/1.1 100") and body is not None:
            c.sendall(body)
            rest = buf.split(b"\r\n\r\n", 1)[1]
            while True:
                try:
                    chunk = c.recv(65536)
                except (socket.timeout, ConnectionResetError):
                    break
                if not chunk:
                    break
                rest += chunk
            final = parse_response(rest)
        elif not first.startswith("HTTP/1.1 100"):
            while True:
                try:
                    chunk = c.recv(65536)
                except (socket.timeout, ConnectionResetError):
                    break
                if not chunk:
                    break
                buf += chunk
            final = parse_response(buf)
    finally:
        c.close()
    return first, final


class TestExpectContinue:
    def _put_head(self, srv, host=None, origin=None, header=True):
        lines = [f"PUT /api/config/{KEY}?agent=test HTTP/1.1", f"Host: {host or '127.0.0.1:%d' % srv.port}",
                 "Content-Type: application/json", "Content-Length: 13", "Expect: 100-continue"]
        if origin is not None:
            lines.append(f"Origin: {origin}")
        if header:
            lines.append("X-Engram-Viz: 1")
        return lines

    def test_bad_host_gets_403_not_100_continue(self, srv):
        first, final = _expect_exchange(srv, self._put_head(srv, host="evil.example", origin=srv.own()))
        assert first.split()[1] == "403", first  # the workbench speaks HTTP/1.0; the status code is what counts
        assert final.status == 403 and final.json()["error"].startswith("Host not allowed: evil.example")

    @pytest.mark.parametrize("kw", [dict(origin="http://evil.example"), dict(origin=None),
                                    dict(origin="own", header=False)],
                             ids=["foreign_origin", "no_origin_no_referer", "no_header"])
    def test_put_without_proofs_gets_403_not_100_continue(self, env, kw):
        if kw.get("origin") == "own":
            kw = dict(kw, origin=env.viz.own())
        before = cfg_bytes(env)
        first, final = _expect_exchange(env.viz, self._put_head(env.viz, **kw))
        assert first.split()[1] == "403", first  # the workbench speaks HTTP/1.0; the status code is what counts
        assert final.status == 403 and final.cors_names() == []
        assert final.json()["ok"] is False  # the whole body is ONE json document: no second response behind it
        assert cfg_bytes(env) == before

    def test_control_a_perfect_put_still_gets_100_continue_then_200(self, env):
        body = b'{"value": 70}'
        first, final = _expect_exchange(env.viz, self._put_head(env.viz, origin=env.viz.own()), body=body)
        assert first.startswith("HTTP/1.1 100"), first
        assert final.status == 200 and final.json()["ok"] is True

    def test_control_a_get_with_expect_is_not_held_to_the_put_proofs(self, env):
        """The proofs are a PUT rule: a GET with Expect and a good Host gets 100 Continue, then its 200."""
        first, final = _expect_exchange(env.viz, ["GET /api/agents HTTP/1.1", f"Host: 127.0.0.1:{env.viz.port}",
                                                  "Expect: 100-continue"], body=b"")
        assert first.startswith("HTTP/1.1 100"), first
        assert final.status == 200
