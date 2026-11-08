"""Contract tests: a request string that cannot be encoded as UTF-8 is a 422, never a 500.

engram-alpha#3294, written BEFORE the fix. ``json.loads`` accepts the six-character
escape ``"x\\ud800y"`` and yields a lone surrogate. ``_read_fields`` (the one shared
reader of every baton write route's body) normalised it like any other text, so the
store's UTF-8 encode raised ``UnicodeEncodeError`` at write time: a 500, a burned
sequence number (and, on advance, after the GitHub check).

The contract these tests pin:

  K1. The shared reader refuses -- through the existing field-refusal path, so 422
      naming route and field -- any string that is not UTF-8-encodable, in a field of
      kind text / id / idl and in each item of a list field (string and list form).
  K2. A refused request writes NOTHING: baton bytes identical, no file created for an
      init, seq counter unchanged, and on advance NO GitHub call is made.
  K3. Valid non-BMP characters (emoji, astral CJK, an ESCAPED surrogate PAIR) are still
      accepted and stored unchanged: the refusal is not over-broad.
  K4. Every route that reads through ``_read_fields`` is in the matrix below, field for
      field; a canary parses server.py so a NEW call site / field cannot go untested.

Identities are SYNTHETIC; no real fleet name appears here.
"""

import ast
import inspect
import json
from pathlib import Path

import pytest

from forum import server as server_mod
from forum.tests.test_baton_advance_api import (
    AUTHOR,
    COLLEAGUE,
    OTHER,
    REPO,
    SEAT,
    SENTINEL,
    World,
)

LONE = "x\ud800y"          # json.loads('"x\\ud800y"') yields exactly this
LONE_LOW = "x\udfffy"      # the other end of the surrogate range
PID = "PR-9001"

# route label -> (URL suffix, minimal valid body, {field: kind}).  This mirrors the
# _read_fields call sites; the canary at the bottom checks it against server.py.
INIT = "POST /api/projects (init)"
ROUTES = {
    INIT: ("", {
        "agent": AUTHOR, "project_id": "PR-9101", "title": "t", "status": "in-progress",
        "turn": AUTHOR, "turn_reason": "start", "participants": [AUTHOR, COLLEAGUE],
        "github": f"pr/{REPO}/9101", "colleague": COLLEAGUE, "author": AUTHOR,
    }, {"agent": "idl", "project_id": "id", "title": "text", "status": "id", "turn": "idl",
        "turn_reason": "text", "github": "id", "colleague": "id", "author": "id",
        "phase": "idl", "transition_phase": "idl", "participants": "list"}),
    "POST /api/projects/<pid>/flip": ("flip", {"agent": AUTHOR, "to_agent": COLLEAGUE, "reason": "r"},
        {"agent": "idl", "to_agent": "idl", "reason": "text", "phase": "idl",
         "transition_phase": "idl"}),
    "POST /api/projects/<pid>/advance": ("advance", {"agent": COLLEAGUE, "to_agent": SEAT, "reason": "ok"},
        {"agent": "idl", "to_agent": "idl", "reason": "text"}),
    "POST /api/projects/<pid>/claim": ("claim", {"agent": OTHER, "pool_sentinel": SENTINEL},
        {"agent": "idl", "pool_sentinel": "idl", "phase": "idl", "transition_phase": "idl"}),
    "POST /api/projects/<pid>/release": ("release", {"agent": OTHER, "pool_sentinel": SENTINEL, "reason": "r"},
        {"agent": "idl", "pool_sentinel": "idl", "reason": "text", "done": "bool",
         "phase": "idl", "transition_phase": "idl"}),
    "POST /api/projects/<pid>/status": ("status", {"agent": AUTHOR, "new_status": "cancelled", "reason": "r"},
        {"agent": "idl", "new_status": "idl", "reason": "text", "phase": "idl"}),
    "POST /api/projects/<pid>/rename": ("rename", {"agent": AUTHOR, "new_title": "renamed"},
        {"agent": "idl", "new_title": "text", "phase": "idl"}),
    "POST /api/projects/<pid>/anchor": ("anchor", {"agent": AUTHOR, "github": f"pr/{REPO}/9001"},
        {"agent": "idl", "github": "id", "phase": "idl"}),
    "POST /api/projects/<pid>/participants": ("participants", {"agent": AUTHOR, "participant": "example-agent-new"},
        {"agent": "idl", "participant": "idl", "phase": "idl"}),
    "POST /api/projects/<pid>/colleague": ("colleague", {"agent": AUTHOR, "colleague": OTHER},
        {"agent": "idl", "colleague": "idl", "phase": "idl"}),
    "POST /api/projects/<pid>/gc": ("gc", {"agent": AUTHOR, "new_status": "merged", "reason": "r"},
        {"agent": "idl", "new_status": "idl", "reason": "text", "phase": "idl"}),
    "POST /api/projects/<pid>/merge": ("merge", {"agent": AUTHOR},
        {"agent": "idl", "forced": "bool", "phase": "idl"}),
}

# (route, field, variant, value): every string-valued field gets a lone-surrogate cell;
# a list field gets one per accepted form.  bool fields are not strings (their own 422).
CELLS = []
for _route, (_s, _b, _kinds) in ROUTES.items():
    for _field, _kind in _kinds.items():
        if _kind == "bool":
            continue
        if _kind == "list":
            CELLS.append((_route, _field, "csv-string", f"{AUTHOR},{LONE}"))
            CELLS.append((_route, _field, "list-item", [AUTHOR, LONE]))
            CELLS.append((_route, _field, "list-item-low", [AUTHOR, LONE_LOW]))
        else:
            CELLS.append((_route, _field, "high", LONE))
            CELLS.append((_route, _field, "low", LONE_LOW))


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    # Production answers an uncaught exception with a 500; TESTING=True would
    # re-raise it into the test instead.  Turn propagation OFF to see what clients see.
    w.app.config["PROPAGATE_EXCEPTIONS"] = False
    return w


def url_for(route, pid=PID):
    suffix = ROUTES[route][0]
    return "/api/projects" if route == INIT else f"/api/projects/{pid}/{suffix}"


def post_raw(world, url, body):
    """POST a body serialised with ensure_ascii=True, so a lone surrogate travels as the
    six-character escape exactly as a real client sends it."""
    return world.client.post(url, data=json.dumps(body), content_type="application/json")


def baton_path(world, pid):
    return Path(world.store.root) / "projects" / f"{pid}.md"


def snapshot(world, pid):
    p = baton_path(world, pid)
    files = sorted(x.name for x in (Path(world.store.root) / "projects").glob("*")) \
        if (Path(world.store.root) / "projects").exists() else []
    return (p.read_bytes() if p.exists() else None, files,
            world.allocator.current(), world.allocator._seq)


def err_of(resp):
    return (resp.get_json() or {}).get("error", "")


def test_premise_json_loads_accepts_a_lone_surrogate():
    # The whole bug rests on this: the JSON layer lets it through.
    assert json.loads('{"t": "x\\ud800y"}')["t"] == LONE
    with pytest.raises(UnicodeEncodeError):
        LONE.encode("utf-8")


# ---------------------------------------------------------------------------
# K1 + K2 + K4: the matrix -- every route x every string field x lone surrogate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route,field,variant,value", CELLS,
                         ids=[f"{r.split('/api/projects')[-1] or 'init'}:{f}:{v}" for r, f, v, _ in CELLS])
def test_lone_surrogate_is_a_422_that_writes_nothing(world, route, field, variant, value):
    suffix, base, _kinds = ROUTES[route]
    if route != INIT:
        world.seed(PID)
    body = dict(base)
    body[field] = value
    before = snapshot(world, PID if route != INIT else base["project_id"])
    calls_before = len(world.gh.calls)

    resp = post_raw(world, url_for(route), body)

    after = snapshot(world, PID if route != INIT else base["project_id"])
    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"
    err = err_of(resp)
    assert route in err and f"'{field}'" in err, err
    # K2: nothing written, no seq consumed, no file created, no GitHub call.
    assert after == before, "a refused request changed the baton file / file set / seq"
    assert len(world.gh.calls) == calls_before, "GitHub was called for a refused request"


def test_advance_refusal_precedes_any_github_call(world):
    # The explicit K2 statement for advance, on a baton that WOULD otherwise advance:
    # control first (the same request with clean text reaches GitHub and succeeds)...
    world.seed(PID)
    ok = post_raw(world, url_for("POST /api/projects/<pid>/advance"),
                  {"agent": COLLEAGUE, "to_agent": SEAT, "reason": "clean"})
    assert ok.status_code in (200, 201), err_of(ok)
    assert world.gh.calls, "control: a valid advance should consult GitHub"
    # ...then on a twin baton the lone surrogate must never get that far.
    world.gh.calls.clear()
    world.seed("PR-9002", github=f"pr/{REPO}/9002")
    resp = post_raw(world, "/api/projects/PR-9002/advance",
                    {"agent": COLLEAGUE, "to_agent": SEAT, "reason": LONE})
    assert resp.status_code == 422
    assert world.gh.calls == []


# ---------------------------------------------------------------------------
# K3: valid non-BMP text still accepted and stored unchanged
# ---------------------------------------------------------------------------

EMOJI = "\U0001F600"      # U+1F600
CJK_X = "\U00020000"      # U+20000, astral CJK


def test_init_accepts_non_bmp_text_and_stores_it(world):
    body = dict(ROUTES[INIT][1])
    body["title"] = f"t {EMOJI} {CJK_X}"
    body["turn_reason"] = f"why {EMOJI}"
    resp = post_raw(world, "/api/projects", body)
    assert resp.status_code in (200, 201), err_of(resp)
    raw = baton_path(world, "PR-9101").read_bytes().decode("utf-8")
    assert f"t {EMOJI} {CJK_X}" in raw and f"why {EMOJI}" in raw


def test_escaped_surrogate_pair_is_accepted_as_one_astral_character(world):
    # '😀' in the JSON text is a PAIR; json.loads combines it to U+1F600.
    assert json.loads('"\\ud83d\\ude00"') == EMOJI
    text = ('{"agent": "%s", "project_id": "PR-9101", "title": "e \\ud83d\\ude00 \\ud840\\udc00", '
            '"status": "in-progress", "turn": "%s", "turn_reason": "s", "participants": ["%s", "%s"], '
            '"github": "pr/%s/9101", "colleague": "%s", "author": "%s"}'
            % (AUTHOR, AUTHOR, AUTHOR, COLLEAGUE, REPO, COLLEAGUE, AUTHOR))
    resp = world.client.post("/api/projects", data=text, content_type="application/json")
    assert resp.status_code in (200, 201), err_of(resp)
    raw = baton_path(world, "PR-9101").read_bytes().decode("utf-8")
    assert f"e {EMOJI} {CJK_X}" in raw


@pytest.mark.parametrize("route,field", [
    ("POST /api/projects/<pid>/rename", "new_title"),
    ("POST /api/projects/<pid>/flip", "reason"),
    ("POST /api/projects/<pid>/advance", "reason"),
])
def test_non_bmp_text_accepted_on_other_routes(world, route, field):
    world.seed(PID)
    body = dict(ROUTES[route][1])
    body[field] = f"ok {EMOJI} {CJK_X}"
    resp = post_raw(world, url_for(route), body)
    assert resp.status_code in (200, 201), err_of(resp)
    raw = baton_path(world, PID).read_bytes().decode("utf-8")
    assert f"ok {EMOJI} {CJK_X}" in raw


def test_non_ascii_identifier_is_not_over_refused(world):
    # M3 guard: refusing is about UTF-8-encodability, not about ASCII.  An accented
    # free-text title round-trips.
    body = dict(ROUTES[INIT][1])
    body["title"] = "café 中文"
    resp = post_raw(world, "/api/projects", body)
    assert resp.status_code in (200, 201), err_of(resp)
    assert "café 中文" in baton_path(world, "PR-9101").read_bytes().decode("utf-8")


# ---------------------------------------------------------------------------
# K1 (pid): what a surrogate in the URL path actually does
# ---------------------------------------------------------------------------

def test_surrogate_cannot_reach_the_reader_through_the_url_path(world, monkeypatch):
    # MEASURED, not assumed: werkzeug decodes the WSGI path with errors="replace", so
    # neither a raw surrogate nor its CESU-8 bytes (%ED%A0%80) ever arrives as a lone
    # surrogate in ``pid``.  Recorded as a test so a framework change that lets one
    # through fails loudly here instead of 500ing in production.
    world.seed(PID)
    seen = []
    orig = server_mod._read_fields

    def spy(route, data, spec, *, pid=None):
        seen.append(pid)
        return orig(route, data, spec, pid=pid)

    monkeypatch.setattr(server_mod, "_read_fields", spy)
    resp = world.client.post("/api/projects/PR-9001%ED%A0%80/rename",
                             data=json.dumps({"agent": AUTHOR, "new_title": "x"}),
                             content_type="application/json")
    # Measured: 404 (no such baton), and the reader saw the id with U+FFFD in place of
    # the bad bytes.  A raw "\ud800" in the path never reaches the app at all (the test
    # client's own UTF-8 encode raises first), so there is no server-side pid case.
    assert resp.status_code == 404, resp.status_code
    assert seen and all(p is not None and "�" in p for p in seen), seen
    for p in seen:
        p.encode("utf-8")  # must not raise


# ---------------------------------------------------------------------------
# K4 canary: the matrix cannot silently go stale
# Bound (reviewer-fairy r1): these canaries watch calls to `_read_fields` only. A baton
# field read AROUND the reader (e.g. `data.get(...)` directly) is invisible to them. The one
# such read today, `project_id` in init, is safe because it is also read as kind `id` first.
# New request-field reads belong in the reader; the text-injection tests treat a bypass as a defect.
# ---------------------------------------------------------------------------

def _source_call_sites():
    tree = ast.parse(inspect.getsource(server_mod))
    sites = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_read_fields"):
            label = ast.literal_eval(node.args[0])
            spec = ast.literal_eval(node.args[2])
            assert label not in sites, f"{label} read through _read_fields twice"
            sites[label] = spec
    return sites


def test_canary_matrix_covers_every_read_fields_call_site_and_field():
    sites = _source_call_sites()
    assert sites, "found no _read_fields call sites -- the canary's parse is broken"
    assert set(sites) == set(ROUTES), (
        f"call sites missing from the matrix: {sorted(set(sites) - set(ROUTES))}; "
        f"matrix routes with no call site: {sorted(set(ROUTES) - set(sites))}")
    for label, spec in sites.items():
        # #1459: `agent` is read by the auth guard, not by a handler's _read_fields spec; the
        # matrix keeps its agent cells (refused by the guard with the same 422).
        matrix_spec = {k: v for k, v in ROUTES[label][2].items() if k != "agent"}
        assert spec == matrix_spec, f"{label}: the matrix's fields/kinds differ from server.py's spec"


def test_canary_every_string_field_has_a_cell():
    have = {(r, f) for r, f, _v, _x in CELLS}
    want = {(r, f) for r, (_s, _b, kinds) in ROUTES.items() for f, k in kinds.items() if k != "bool"}
    assert have == want
