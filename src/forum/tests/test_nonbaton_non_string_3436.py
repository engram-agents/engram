"""Contract tests: a non-string value in a free-text JSON field -- or in `agent` / `state`
(round 2, at the bottom) -- on the NON-baton routes is a 422, never a 500 and never a
silent store.

engram-alpha#3436 (follow-up to #3318 / PR #3431), written BEFORE the fix. These routes
do not read through ``_read_fields``, so a number / list / object reached ``.strip()``
or the sqlite binding (500), and ``pair_initials`` went through ``str()`` and stored a
list's repr.

The contract these tests pin:

  C1. A non-string, non-null value in any free-text field of these routes is a 422 whose
      message is ``_read_fields``' precedent for a typed-field error:
      ``"<route>: field '<field>' must be a string (got <type>)"`` (the same
      ``_field_refusal`` / ``_RequestRefusal`` path, status 422).
  C2. A refused request writes NOTHING (same snapshot as #3431's tests).
  C3. A valid string on the same route still succeeds; ``None`` keeps its meaning
      (``pair_initials: null`` clears, ``activity: null`` is absent).

Identities are SYNTHETIC.
"""

import pytest

from forum.tests.test_nonbaton_lone_surrogate_3318 import (  # noqa: F401  (world = fixture)
    DM, ME, POST, STATUS, VERIFY, err_of, prime, q, request_for, send, seed_thread,
    snapshot, world,
)

# (route, field, variant): every free-text field, as in the #3318 matrix, minus `queue`
# (list-typed; a non-list is already a 400 "queue must be a list").
CELLS = []
for _field in ("body_md", "title", "hostname", "category_slug"):
    CELLS.append((POST, _field, "new"))
for _field in ("body_md", "hostname"):
    CELLS.append((POST, _field, "reply"))
CELLS += [
    (VERIFY, "note", "-"),
    (ME, "pair_initials", "-"),
    (DM, "body", "-"),
    (STATUS, "activity", "-"),
]

# number, list, object (truthy, so they used to reach .strip() / the binding) plus the
# falsy spellings, which `x or ""` used to launder into "absent".
VALUES = [
    ("int", 5, "int"),
    ("list", ["x"], "list"),
    ("dict", {"k": "v"}, "dict"),
    ("zero", 0, "int"),
    ("false", False, "bool"),
    ("empty_list", [], "list"),
    ("empty_dict", {}, "dict"),
]


def cell_id(c):
    route, field, variant = c
    return f"{route.split(' ', 1)[1]}:{field}" + ("" if variant == "-" else f":{variant}")


@pytest.mark.parametrize("val_name,value,type_name", VALUES, ids=[v[0] for v in VALUES])
@pytest.mark.parametrize("cell", CELLS, ids=[cell_id(c) for c in CELLS])
def test_non_string_is_a_422_that_writes_nothing(world, cell, val_name, value, type_name):
    route, field, variant = cell
    prime(world, route)
    method, url, body = request_for(world, route, field, variant, value)
    before = snapshot(world)

    resp = send(world, method, url, body)

    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"
    assert err_of(resp) == f"{route}: field '{field}' must be a string (got {type_name})", err_of(resp)
    assert snapshot(world) == before, "a refused request wrote something"


def test_canary_matrix_covers_every_route_and_field_issue_3436_names():
    assert {c[0] for c in CELLS} == {POST, VERIFY, ME, DM, STATUS}
    assert {(c[0], c[1]) for c in CELLS} >= {
        (POST, "body_md"), (POST, "hostname"), (POST, "title"), (VERIFY, "note"),
        (STATUS, "activity"), (ME, "pair_initials"),
    }


def test_a_reply_does_not_refuse_the_title_and_category_slug_it_ignores(world):
    # Same deliberate rule as #3431: a field the route does not read is not refused.
    tid, _ = seed_thread(world)
    r = send(world, "POST", "/api/post",
             {"agent": "bob", "thread_id": tid, "body_md": "reply", "title": 5, "category_slug": [1]})
    assert r.status_code == 201, r.data


# ---------------------------------------------------------------------------
# C3 controls: valid strings succeed; null keeps its meaning
# ---------------------------------------------------------------------------

def test_control_post_new_thread(world):
    r = send(world, "POST", "/api/post", {"agent": "bob", "category_slug": "inter-agent",
                                          "title": "T", "body_md": "b", "hostname": "host-a"})
    assert r.status_code == 201, r.data
    assert q(world, "SELECT title, body_md FROM threads") == [("T", "b")]


def test_control_post_new_thread_null_hostname(world):
    r = send(world, "POST", "/api/post", {"agent": "bob", "category_slug": "inter-agent",
                                          "title": "T", "body_md": "b", "hostname": None})
    assert r.status_code == 201, r.data


def test_control_post_reply(world):
    tid, _ = seed_thread(world)
    r = send(world, "POST", "/api/post", {"agent": "bob", "thread_id": tid, "body_md": "r",
                                          "hostname": "host-a"})
    assert r.status_code == 201, r.data
    assert q(world, "SELECT count(*) FROM posts") == [(2,)]


def test_control_verify(world):
    _, pid = seed_thread(world)
    r = send(world, "POST", f"/api/post/{pid}/verify", {"agent": "bob", "note": "checked"})
    assert r.status_code == 200, r.data
    assert q(world, "SELECT note FROM post_verifications") == [("checked",)]


def test_control_patch_agent_sets_and_null_clears(world):
    r = send(world, "PATCH", "/api/agent/me", {"agent": "bob", "pair_initials": "CD"})
    assert r.status_code == 200, r.data
    assert q(world, "SELECT pair_initials FROM agents WHERE name = 'bob'") == [("CD",)]
    r = send(world, "PATCH", "/api/agent/me", {"agent": "bob", "pair_initials": None})
    assert r.status_code == 200, r.data
    assert q(world, "SELECT pair_initials FROM agents WHERE name = 'bob'") == [(None,)]


def test_control_dm(world):
    r = send(world, "POST", "/api/dm/carol", {"agent": "bob", "body": "hello"})
    assert r.status_code == 201, r.data
    assert [m.body for m in world.store.read_dm_thread("bob", "carol")] == ["hello"]


def test_control_status_string_and_null_activity(world):
    r = send(world, "POST", "/api/agents/status",
             {"agent": "bob", "state": "working", "activity": "doing", "queue": ["a"]})
    assert r.status_code == 200, r.data
    r = send(world, "POST", "/api/agents/status",
             {"agent": "bob", "state": "working", "activity": None})
    assert r.status_code == 200, r.data
    (row,) = q(world, "SELECT status_activity FROM agents WHERE name='bob'")
    assert row[0] is None


# ---------------------------------------------------------------------------
# Round 2: `agent` (every JSON-body route) and `state` (agents/status)
# ---------------------------------------------------------------------------
#
# Before the round-2 fix (measured at 4b25849f): a truthy non-string `agent` was a 500
# on all of these routes (`.strip()` on an int / list / dict); a falsy one (0, False,
# [], {}) was laundered by `x or ""` into "absent" and answered 400 "agent is required".
# `state` (agents/status): list / dict a 500 (unhashable), int / 0 / False a 400
# "state N is not publishable".  Nothing was ever written.  All are now a 422.

ACCEPT = "POST /api/thread/<tid>/accept"
READ = "POST /api/thread/<tid>/read"

AGENT_ROUTES = [
    (POST, "new"), (POST, "reply"), (VERIFY, "-"), (ME, "-"), (DM, "-"), (STATUS, "-"),
    (ACCEPT, "-"), (READ, "-"),
]


def agent_request(world, route, variant, value, field="agent"):
    """(method, url, body) for one route with `value` in `field` and the rest valid."""
    if route == POST and variant == "new":
        body = {"agent": "bob", "category_slug": "inter-agent", "title": "T", "body_md": "b"}
        method, url = "POST", "/api/post"
    elif route == POST:
        tid, _ = seed_thread(world)
        body = {"agent": "bob", "thread_id": tid, "body_md": "r"}
        method, url = "POST", "/api/post"
    elif route == VERIFY:
        _, pid = seed_thread(world)
        body, method, url = {"agent": "bob", "note": "n"}, "POST", f"/api/post/{pid}/verify"
    elif route == ME:
        body, method, url = {"agent": "bob", "pair_initials": "CD"}, "PATCH", "/api/agent/me"
    elif route == DM:
        body, method, url = {"agent": "bob", "body": "hi"}, "POST", "/api/dm/carol"
    elif route == STATUS:
        body, method, url = {"agent": "bob", "state": "working"}, "POST", "/api/agents/status"
    elif route == ACCEPT:
        # accept needs a Q&A thread asked by the caller ("bob") and someone else's reply.
        r = send(world, "POST", "/api/post", {"agent": "bob", "category_slug": "q-and-a",
                                              "title": "Q", "body_md": "question"})
        assert r.status_code == 201, r.data
        tid = r.get_json()["thread_id"]
        r = send(world, "POST", "/api/post", {"agent": "alice", "thread_id": tid, "body_md": "answer"})
        assert r.status_code == 201, r.data
        body = {"agent": "bob", "post_id": r.get_json()["post_id"]}
        method, url = "POST", f"/api/thread/{tid}/accept"
    else:
        assert route == READ
        tid, _ = seed_thread(world)
        body, method, url = {"agent": "bob"}, "POST", f"/api/thread/{tid}/read"
    body[field] = value
    return method, url, body


def route_id(c):
    route, variant = c
    return route.split(" ", 1)[1] + ("" if variant == "-" else f":{variant}")


def extra_state(world):
    """Tables snapshot() does not cover: read watermarks and accepted answers."""
    return (q(world, "SELECT * FROM reads"),
            q(world, "SELECT id, accepted_answer_post_id FROM threads ORDER BY id"))


@pytest.mark.parametrize("val_name,value,type_name", VALUES, ids=[v[0] for v in VALUES])
@pytest.mark.parametrize("cell", AGENT_ROUTES, ids=[route_id(c) for c in AGENT_ROUTES])
def test_non_string_agent_is_a_422_that_writes_nothing(world, cell, val_name, value, type_name):
    route, variant = cell
    prime(world, route)
    method, url, body = agent_request(world, route, variant, value)
    before = (snapshot(world), extra_state(world))

    resp = send(world, method, url, body)

    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"
    assert err_of(resp) == f"{route}: field 'agent' must be a string (got {type_name})", err_of(resp)
    assert (snapshot(world), extra_state(world)) == before, "a refused request wrote something"


@pytest.mark.parametrize("val_name,value,type_name", VALUES, ids=[v[0] for v in VALUES])
def test_non_string_state_is_a_422_that_writes_nothing(world, val_name, value, type_name):
    prime(world, STATUS)
    method, url, body = agent_request(world, STATUS, "-", value, field="state")
    before = snapshot(world)

    resp = send(world, method, url, body)

    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"
    assert err_of(resp) == f"{STATUS}: field 'state' must be a string (got {type_name})", err_of(resp)
    assert snapshot(world) == before, "a refused request wrote something"


def test_agent_canary_covers_every_json_agent_route():
    assert {c[0] for c in AGENT_ROUTES} == {POST, VERIFY, ME, DM, STATUS, ACCEPT, READ}


# String controls: the order of checks for strings is unchanged.

@pytest.mark.parametrize("cell", AGENT_ROUTES, ids=[route_id(c) for c in AGENT_ROUTES])
def test_string_agent_control_still_succeeds(world, cell):
    route, variant = cell
    method, url, body = agent_request(world, route, variant, "bob")
    resp = send(world, method, url, body)
    assert resp.status_code in (200, 201), f"{resp.status_code}: {resp.data[:200]!r}"


@pytest.mark.parametrize("cell", AGENT_ROUTES, ids=[route_id(c) for c in AGENT_ROUTES])
def test_empty_string_agent_is_still_400_required(world, cell):
    route, variant = cell
    method, url, body = agent_request(world, route, variant, "")
    resp = send(world, method, url, body)
    assert (resp.status_code, err_of(resp)) == (400, "agent is required")


@pytest.mark.parametrize("cell", AGENT_ROUTES, ids=[route_id(c) for c in AGENT_ROUTES])
def test_invalid_string_agent_is_still_400_invalid(world, cell):
    route, variant = cell
    method, url, body = agent_request(world, route, variant, "bad name!")
    resp = send(world, method, url, body)
    assert resp.status_code == 400, resp.data
    assert "invalid agent name" in err_of(resp), err_of(resp)


def test_string_state_control_and_invalid_string_state_still_400(world):
    r = send(world, "POST", "/api/agents/status", {"agent": "bob", "state": "idle"})
    assert r.status_code == 200, r.data
    r = send(world, "POST", "/api/agents/status", {"agent": "bob", "state": "offline"})
    assert r.status_code == 400 and "not publishable" in err_of(r), r.data
    r = send(world, "POST", "/api/agents/status", {"agent": "bob"})
    assert (r.status_code, err_of(r)) == (400, "state is required")


# ---------------------------------------------------------------------------
# Round 3: integer-id fields that COERCE-AND-ACCEPT (thread_id, post_id, last_read_post_id)
# ---------------------------------------------------------------------------
#
# Measured at 712ea91b: `int()` accepted `true` and `1.9` (bool is an int subclass), so
# POST /api/post with thread_id=true wrote a REPLY to thread 1 (201), and
# last_read_post_id=true / 1.9 set the read watermark to 1 (200); post_id=true / 1.9 passed
# the parse step of accept.  A list / dict / "abc" was already a 400.  Now: a real int
# (not a bool) or a string of digits (optional leading minus) is accepted as before; a
# bool, a float (even 1.0), any other string, a list and a dict are a 422.

INT_VALUES = [
    ("true", True, "bool"),
    ("false", False, "bool"),
    ("float", 1.9, "float"),
    ("float_whole", 1.0, "float"),
    ("abc", "abc", "str"),
    ("list", [1], "list"),
    ("dict", {}, "dict"),
]

# (route, variant, field)
INT_CELLS = [(POST, "reply", "thread_id"), (ACCEPT, "-", "post_id"), (READ, "-", "last_read_post_id")]


def int_cell_id(c):
    return f"{c[0].split(' ', 1)[1]}:{c[2]}"


@pytest.mark.parametrize("val_name,value,type_name", INT_VALUES, ids=[v[0] for v in INT_VALUES])
@pytest.mark.parametrize("cell", INT_CELLS, ids=[int_cell_id(c) for c in INT_CELLS])
def test_non_integer_id_is_a_422_that_writes_nothing(world, cell, val_name, value, type_name):
    route, variant, field = cell
    prime(world, route)
    method, url, body = agent_request(world, route, variant, value, field=field)
    before = (snapshot(world), extra_state(world))

    resp = send(world, method, url, body)

    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"
    assert err_of(resp) == f"{route}: field '{field}' must be an integer (got {type_name})", err_of(resp)
    assert (snapshot(world), extra_state(world)) == before, "a refused request wrote something"


@pytest.mark.parametrize("value", [True, 1.9])
def test_thread_id_true_or_float_writes_no_reply_to_thread_1(world, value):
    tid, _ = seed_thread(world)
    assert tid == 1
    resp = send(world, "POST", "/api/post", {"agent": "bob", "thread_id": value, "body_md": "stray"})
    assert resp.status_code == 422, resp.data
    assert q(world, "SELECT count(*) FROM posts WHERE thread_id = 1") == [(1,)]


def test_int_id_canary_covers_every_integer_id_field():
    assert {(c[0], c[2]) for c in INT_CELLS} == {
        (POST, "thread_id"), (ACCEPT, "post_id"), (READ, "last_read_post_id"),
    }


# Controls: a real int and a digit string still succeed; null keeps its meaning.

def test_control_thread_id_int_and_digit_string(world):
    tid, _ = seed_thread(world)
    for value in (tid, str(tid)):
        r = send(world, "POST", "/api/post", {"agent": "bob", "thread_id": value, "body_md": "r"})
        assert r.status_code == 201, r.data
    assert q(world, f"SELECT count(*) FROM posts WHERE thread_id = {tid}") == [(3,)]


def test_control_thread_id_null_is_a_new_thread(world):
    r = send(world, "POST", "/api/post", {"agent": "bob", "thread_id": None, "category_slug": "inter-agent",
                                          "title": "T", "body_md": "b"})
    assert r.status_code == 201, r.data


def test_control_thread_id_negative_digit_string_is_still_not_found(world):
    r = send(world, "POST", "/api/post", {"agent": "bob", "thread_id": "-3", "body_md": "r"})
    assert (r.status_code, err_of(r)) == (404, "thread -3 not found")


@pytest.mark.parametrize("as_str", [False, True])
def test_control_post_id_int_and_digit_string_accept(world, as_str):
    method, url, body = agent_request(world, ACCEPT, "-", "bob")
    if as_str:
        body["post_id"] = str(body["post_id"])
    r = send(world, method, url, body)
    assert r.status_code == 200, r.data


def test_control_post_id_null_is_still_400_required(world):
    method, url, body = agent_request(world, ACCEPT, "-", "bob")
    body["post_id"] = None
    r = send(world, method, url, body)
    assert (r.status_code, err_of(r)) == (400, "post_id is required")


@pytest.mark.parametrize("value", [1, "1", 0, "0", -3, "-3"])
def test_control_last_read_post_id_int_and_digit_string(world, value):
    tid, _ = seed_thread(world)
    r = send(world, "POST", f"/api/thread/{tid}/read", {"agent": "bob", "last_read_post_id": value})
    assert r.status_code == 200, r.data
    assert r.get_json()["thread_id"] == tid


def test_control_last_read_post_id_null_defaults_to_max_post(world):
    tid, pid = seed_thread(world)
    r = send(world, "POST", f"/api/thread/{tid}/read", {"agent": "bob", "last_read_post_id": None})
    assert r.status_code == 200, r.data
    assert r.get_json()["last_read_post_id"] == pid
