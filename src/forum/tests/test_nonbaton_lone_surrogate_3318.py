"""Contract tests: a lone surrogate in free text on the NON-baton routes is a 422, never a 500.

engram-alpha#3318 (sibling of #3294 / PR #3317, which fixed the baton routes), written
BEFORE the fix. ``json.loads`` accepts the escape ``"x\\ud800y"`` and yields a lone
surrogate. These routes do not read through ``_read_fields``, so the string reached a
store (sqlite binding, or the DM file's UTF-8 write) and the client got a 500.

The contract these tests pin:

  C1. A UTF-8-unencodable string in any free-text field of these routes is a 422 with
      the shared ``_NOT_UTF8`` message, through the same ``_RequestRefusal`` path and
      body shape (``{"error": "<route>: field '<field>' must be valid UTF-8 text ..."}``)
      the baton routes use.
  C2. A refused request writes NOTHING: no thread / post / agent row / audit line for
      /api/post, no verification (and no new agent row) for verify, the agent's
      pair_initials unchanged, no DM appended (and no sequence number burned), the
      published status unchanged.
  C3. A normal valid string (including non-BMP text) on the same route still succeeds.
  C4. /api/agents/status: ``queue`` (a surrogate inside it used to be a 200 and was
      stored as a JSON escape that decodes back to a lone surrogate on every read) and
      ``activity`` (a 400 carrying the unrelated "state not publishable" message,
      because UnicodeEncodeError is a ValueError) are both a 422 too.

Identities are SYNTHETIC.
"""

import json
import sqlite3

import pytest

from forum.coordination.seq import SeqAllocator
from forum.coordination.store_file import FileStore
from forum.db import init_db
from forum.server import _NOT_UTF8, create_app

LONE = "x\ud800y"        # json.loads('"x\\ud800y"') yields exactly this
LONE_LOW = "x\udfffy"    # the other end of the surrogate range
EMOJI = "\U0001F600"

POST = "POST /api/post"
VERIFY = "POST /api/post/<pid>/verify"
ME = "PATCH /api/agent/me"
DM = "POST /api/dm/<counterpart>"
STATUS = "POST /api/agents/status"

# (route label, field, variant) -- every free-text field, high and low surrogate.
# "new" / "reply" distinguish the two shapes of POST /api/post.
CELLS = []
for _field in ("body_md", "title", "hostname", "category_slug"):
    CELLS.append((POST, _field, "new"))
for _field in ("body_md", "hostname"):
    CELLS.append((POST, _field, "reply"))
CELLS += [
    (VERIFY, "note", "-"),
    (ME, "pair_initials", "-"),
    (DM, "body", "-"),
    (STATUS, "queue", "-"),
    (STATUS, "activity", "-"),
]
SURROGATES = [("high", LONE), ("low", LONE_LOW)]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("FORUM_NO_EMBEDDINGS", "1")
    db_path = str(tmp_path / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(tmp_path / "audit.jsonl"))
    app.config["TESTING"] = True
    # Production answers an uncaught exception with a 500; TESTING=True would re-raise
    # it into the test instead.  Turn propagation OFF to see what a client sees.
    app.config["PROPAGATE_EXCEPTIONS"] = False
    store = FileStore(tmp_path / "coord")
    allocator = SeqAllocator(recover=store.recover_max_seq)
    app.config["COORD_STORE"] = store
    app.config["COORD_ALLOCATOR"] = allocator

    class W:
        pass

    w = W()
    w.app, w.client, w.store, w.allocator = app, app.test_client(), store, allocator
    w.db_path, w.audit_path, w.coord = db_path, tmp_path / "audit.jsonl", tmp_path / "coord"
    return w


def send(world, method, url, body):
    """Send a body serialised with ensure_ascii=True, so a lone surrogate travels as the
    six-character escape exactly as a real client sends it."""
    return world.client.open(url, method=method, data=json.dumps(body),
                             content_type="application/json")


def q(world, sql, args=()):
    conn = sqlite3.connect(world.db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def seed_thread(world):
    r = send(world, "POST", "/api/post", {"agent": "alice", "category_slug": "inter-agent",
                                          "title": "seed", "body_md": "seed body"})
    assert r.status_code == 201, r.data
    return r.get_json()["thread_id"], r.get_json()["post_id"]


def err_of(resp):
    return (resp.get_json() or {}).get("error", "")


def audit_lines(world):
    return world.audit_path.read_text().splitlines() if world.audit_path.exists() else []


def coord_files(world):
    return sorted(str(p.relative_to(world.coord)) for p in world.coord.rglob("*") if p.is_file())


def agent_state(world):
    return q(world, "SELECT name, pair_initials, status_state, status_activity, status_queue "
                    "FROM agents ORDER BY name")


def snapshot(world):
    """Everything a refused request must leave alone."""
    return {
        "threads": q(world, "SELECT id, title, body_md FROM threads ORDER BY id"),
        "posts": q(world, "SELECT id, body_md FROM posts ORDER BY id"),
        "verifications": q(world, "SELECT * FROM post_verifications ORDER BY id"),
        "agents": agent_state(world),
        "audit": audit_lines(world),
        "coord": coord_files(world),
        "seq": world.allocator.current(),
    }


def request_for(world, route, field, variant, value):
    """(method, url, body) for one cell, with `value` in `field` and everything else valid."""
    if route == POST and variant == "new":
        body = {"agent": "bob", "category_slug": "inter-agent", "title": "T", "body_md": "b"}
        body[field] = value
        return "POST", "/api/post", body
    if route == POST:
        tid, _ = seed_thread(world)
        body = {"agent": "bob", "thread_id": tid, "body_md": "r"}
        body[field] = value
        return "POST", "/api/post", body
    if route == VERIFY:
        _, pid = seed_thread(world)
        return "POST", f"/api/post/{pid}/verify", {"agent": "bob", "note": value}
    if route == ME:
        return "PATCH", "/api/agent/me", {"agent": "bob", "pair_initials": value}
    if route == DM:
        return "POST", "/api/dm/carol", {"agent": "bob", "body": value}
    assert route == STATUS
    body = {"agent": "bob", "state": "working"}
    body[field] = [value] if field == "queue" else value
    return "POST", "/api/agents/status", body


def cell_id(c):
    route, field, variant = c
    return f"{route.split(' ', 1)[1]}:{field}" + ("" if variant == "-" else f":{variant}")


def prime(world, route):
    """Give a route a pre-existing record for 'unchanged' assertions (pair_initials,
    status), and make sure the sender row exists so 'no agent row created' is the
    refused request's doing, not a primed one."""
    if route == ME:
        r = send(world, "PATCH", "/api/agent/me", {"agent": "bob", "pair_initials": "AB"})
        assert r.status_code == 200, r.data
    if route == STATUS:
        r = send(world, "POST", "/api/agents/status",
                 {"agent": "bob", "state": "idle", "activity": "before", "queue": ["q1"]})
        assert r.status_code == 200, r.data


# ---------------------------------------------------------------------------
# C1 + C2: every route x every free-text field x high and low surrogate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sur_name,value", SURROGATES, ids=[n for n, _ in SURROGATES])
@pytest.mark.parametrize("cell", CELLS, ids=[cell_id(c) for c in CELLS])
def test_lone_surrogate_is_a_422_that_writes_nothing(world, cell, sur_name, value):
    route, field, variant = cell
    prime(world, route)
    method, url, body = request_for(world, route, field, variant, value)
    before = snapshot(world)

    resp = send(world, method, url, body)

    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"
    err = err_of(resp)
    assert err == f"{route}: field '{field}' {_NOT_UTF8}", err
    assert snapshot(world) == before, "a refused request wrote something"


def test_premise_json_loads_accepts_a_lone_surrogate():
    # The whole bug rests on this: the JSON layer lets it through.
    assert json.loads('{"t": "x\\ud800y"}')["t"] == LONE
    with pytest.raises(UnicodeEncodeError):
        LONE.encode("utf-8")


def test_canary_every_cell_names_a_free_text_field_the_route_reads():
    # The matrix is hand-written; pin that it covers the five routes the issue names.
    assert {c[0] for c in CELLS} == {POST, VERIFY, ME, DM, STATUS}


# ---------------------------------------------------------------------------
# C3: controls -- a normal valid string still succeeds on every route
# ---------------------------------------------------------------------------

def test_control_post_new_thread(world):
    r = send(world, "POST", "/api/post", {"agent": "bob", "category_slug": "inter-agent",
                                          "title": f"T {EMOJI}", "body_md": f"b {EMOJI}",
                                          "hostname": "host-a"})
    assert r.status_code == 201, r.data
    assert q(world, "SELECT title, body_md FROM threads") == [(f"T {EMOJI}", f"b {EMOJI}")]


def test_control_post_reply(world):
    tid, _ = seed_thread(world)
    r = send(world, "POST", "/api/post", {"agent": "bob", "thread_id": tid, "body_md": "r",
                                          "hostname": "host-a"})
    assert r.status_code == 201, r.data
    assert q(world, "SELECT count(*) FROM posts") == [(2,)]


def test_control_post_accepts_an_escaped_surrogate_pair(world):
    # '😀' is a PAIR; json.loads combines it to U+1F600: not over-refused.
    raw = ('{"agent": "bob", "category_slug": "inter-agent", "title": "T", '
           '"body_md": "pair \\ud83d\\ude00"}')
    r = world.client.post("/api/post", data=raw, content_type="application/json")
    assert r.status_code == 201, r.data
    assert q(world, "SELECT body_md FROM threads") == [(f"pair {EMOJI}",)]


def test_control_verify(world):
    _, pid = seed_thread(world)
    r = send(world, "POST", f"/api/post/{pid}/verify", {"agent": "bob", "note": f"checked {EMOJI}"})
    assert r.status_code == 200, r.data
    assert q(world, "SELECT note FROM post_verifications") == [(f"checked {EMOJI}",)]


def test_control_patch_agent(world):
    r = send(world, "PATCH", "/api/agent/me", {"agent": "bob", "pair_initials": "CD"})
    assert r.status_code == 200, r.data
    assert q(world, "SELECT pair_initials FROM agents WHERE name = 'bob'") == [("CD",)]


def test_control_dm(world):
    r = send(world, "POST", "/api/dm/carol", {"agent": "bob", "body": f"hello {EMOJI}"})
    assert r.status_code == 201, r.data
    msgs = world.store.read_dm_thread("bob", "carol")
    assert [m.body for m in msgs] == [f"hello {EMOJI}"]


def test_control_status(world):
    r = send(world, "POST", "/api/agents/status",
             {"agent": "bob", "state": "working", "activity": "doing", "queue": ["a", f"b {EMOJI}"]})
    assert r.status_code == 200, r.data
    (row,) = q(world, "SELECT status_state, status_activity, status_queue FROM agents WHERE name='bob'")
    assert row[0] == "working" and row[1] == "doing"
    assert json.loads(row[2]) == ["a", f"b {EMOJI}"]


# ---------------------------------------------------------------------------
# C4 detail: a surrogate in ANY queue item (not just the first) is refused
# ---------------------------------------------------------------------------

def test_status_queue_surrogate_in_a_later_item_is_refused(world):
    prime(world, STATUS)
    before = snapshot(world)
    r = send(world, "POST", "/api/agents/status",
             {"agent": "bob", "state": "working", "queue": ["fine", "also fine", LONE]})
    assert r.status_code == 422
    assert err_of(r) == f"{STATUS}: field 'queue' {_NOT_UTF8}"
    assert snapshot(world) == before


# ---------------------------------------------------------------------------
# Review round 2: nested queue items (list / dict value / dict KEY, any depth)
# ---------------------------------------------------------------------------

NESTED_SHAPES = [
    ("list_in_list", lambda s: [["a", s]]),
    ("dict_value", lambda s: [{"k": s}]),
    ("dict_key", lambda s: [{s: "v"}]),
    ("deep", lambda s: ["ok", [{"a": [{"b": s}]}]]),
]


@pytest.mark.parametrize("sur_name,value", SURROGATES, ids=[n for n, _ in SURROGATES])
@pytest.mark.parametrize("shape", NESTED_SHAPES, ids=[n for n, _ in NESTED_SHAPES])
def test_status_queue_nested_surrogate_is_a_422_that_writes_nothing(world, shape, sur_name, value):
    prime(world, STATUS)
    before = snapshot(world)
    r = send(world, "POST", "/api/agents/status",
             {"agent": "bob", "state": "working", "queue": shape[1](value)})
    assert r.status_code == 422, f"{r.status_code}: {err_of(r) or r.data[:200]!r}"
    assert err_of(r) == f"{STATUS}: field 'queue' {_NOT_UTF8}"
    assert snapshot(world) == before, "a refused request wrote something"


def test_control_status_nested_valid_queue_is_stored(world):
    queue = [["a", f"b {EMOJI}"], {"k": "v", "n": [1, {"z": None}]}, "plain"]
    r = send(world, "POST", "/api/agents/status",
             {"agent": "bob", "state": "working", "queue": queue})
    assert r.status_code == 200, r.data
    (row,) = q(world, "SELECT status_queue FROM agents WHERE name='bob'")
    assert json.loads(row[0]) == queue


# ---------------------------------------------------------------------------
# Review round 2: a REPLY ignores title / category_slug, so a surrogate there is
# not refused (deliberate difference from #3317; pinned so a later "consistency"
# change does not flip it silently).
# ---------------------------------------------------------------------------

def test_reply_with_surrogate_title_and_category_slug_is_accepted(world):
    tid, _ = seed_thread(world)
    r = send(world, "POST", "/api/post",
             {"agent": "bob", "thread_id": tid, "body_md": "valid reply",
              "title": LONE, "category_slug": LONE_LOW})
    assert r.status_code == 201, f"{r.status_code}: {err_of(r) or r.data[:200]!r}"
    assert q(world, "SELECT count(*) FROM posts") == [(2,)]
    assert q(world, "SELECT body_md FROM posts ORDER BY id DESC LIMIT 1") == [("valid reply",)]


# ---------------------------------------------------------------------------
# Review round 3: the deep check must not recurse.  json.loads accepts nesting far
# past the interpreter's recursion limit, so a recursive walk turned a valid deeply
# nested queue (stored with a 200 at the base) into a 500.
# ---------------------------------------------------------------------------

DEEP = 3000


def post_raw_status(world, queue_json):
    """Post a hand-built body string: json.dumps of a deep Python object may itself
    recurse, and the escape must reach the server as six characters."""
    raw = '{"agent": "bob", "state": "working", "queue": ' + queue_json + "}"
    return world.client.post("/api/agents/status", data=raw, content_type="application/json")


def nested_lists(depth, leaf):
    return "[" * depth + leaf + "]" * depth


def test_status_deep_valid_queue_is_stored(world):
    r = post_raw_status(world, nested_lists(DEEP, '"ok"'))
    assert r.status_code == 200, f"{r.status_code}: {err_of(r) or r.data[:200]!r}"
    (row,) = q(world, "SELECT status_queue FROM agents WHERE name='bob'")
    assert row[0] is not None and "ok" in row[0]


def test_status_deep_queue_with_surrogate_at_the_bottom_is_a_422(world):
    prime(world, STATUS)
    before = snapshot(world)
    r = post_raw_status(world, nested_lists(DEEP, r'"x\ud800y"'))
    assert r.status_code == 422, f"{r.status_code}: {err_of(r) or r.data[:200]!r}"
    assert err_of(r) == f"{STATUS}: field 'queue' {_NOT_UTF8}"
    assert snapshot(world) == before, "a refused request wrote something"


def test_status_deep_dict_chain_with_surrogate_is_a_422(world):
    prime(world, STATUS)
    before = snapshot(world)
    chain = '{"a": ' * DEEP + r'"x\ud800y"' + "}" * DEEP
    r = post_raw_status(world, "[" + chain + "]")
    assert r.status_code == 422, f"{r.status_code}: {err_of(r) or r.data[:200]!r}"
    assert err_of(r) == f"{STATUS}: field 'queue' {_NOT_UTF8}"
    assert snapshot(world) == before, "a refused request wrote something"
