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


# Removed-test: src/forum/tests/test_nonbaton_non_string_3436.py::test_empty_string_agent_is_still_400_required — replaced, not dropped: #1459 makes the token's agent the caller, so the old handler-level refusal no longer exists; the cell below asserts the new contract
@pytest.mark.parametrize("cell", AGENT_ROUTES, ids=[route_id(c) for c in AGENT_ROUTES])
def test_empty_string_agent_names_nothing_and_the_token_agent_is_the_caller(world, cell):
    # #1459: the caller is the token's agent; an empty `agent` names nobody, so it is no
    # longer a 400 "agent is required" (the handler does not read the field at all).
    route, variant = cell
    method, url, body = agent_request(world, route, variant, "")
    resp = send(world, method, url, body)
    assert err_of(resp) != "agent is required", err_of(resp)
    assert resp.status_code in (200, 201, 403), (resp.status_code, resp.data[:200])  # 403: accept = asker only


# Removed-test: src/forum/tests/test_nonbaton_non_string_3436.py::test_invalid_string_agent_is_still_400_invalid — replaced, not dropped: #1459 makes the token's agent the caller, so the old handler-level refusal no longer exists; the cell below asserts the new contract
@pytest.mark.parametrize("cell", AGENT_ROUTES, ids=[route_id(c) for c in AGENT_ROUTES])
def test_invalid_string_agent_is_a_403_identity_mismatch_that_writes_nothing(world, cell):
    # #1459: a malformed name cannot be the token's agent, so the guard refuses it as an
    # identity mismatch before any handler runs (it used to be the handler's 400).
    route, variant = cell
    prime(world, route)
    method, url, body = agent_request(world, route, variant, "bad name!")
    before = (snapshot(world), extra_state(world))
    resp = send(world, method, url, body)
    assert resp.status_code == 403, resp.data
    assert err_of(resp) == "identity mismatch", err_of(resp)
    assert (snapshot(world), extra_state(world)) == before, "a refused request wrote something"


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
    # #3438: pin the full-string ASCII-digit rule (`_INT_STR.fullmatch`).  A `.match`
    # mutant kept every earlier cell green: "5x" / "5 " would pass the guard.
    ("trailing_junk", "5x", "str"),
    ("leading_space", " 5", "str"),
    ("plus_sign", "+5", "str"),
    ("trailing_space", "5 ", "str"),
    ("empty", "", "str"),
    ("fullwidth_digit", "５", "str"),
    ("trailing_newline", "5\n", "str"),
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


# ---------------------------------------------------------------------------
# #3438: integers outside SQLite's signed 64-bit range
# ---------------------------------------------------------------------------
#
# Measured at 20c1e81f: `_require_int` accepted any Python int, so thread_id (POST
# /api/post reply), post_id (accept) and last_read_post_id (read) = 2**63, -(2**63)-1 or
# 10**30 -- as an int AND as a digit string -- overflowed at the sqlite binding: a 500
# (on /read AFTER upsert_agent, so an agent row was created).  expected_republish_seconds
# (agents/status) did not use `_require_int`: an int >= 2**63 was a 500, a digit string
# and a negative were a 400 from set_agent_status.  Now a value outside [-(2**63), 2**63-1]
# is a 422 before the first read or write; the boundary values themselves still pass.

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1
OUT_OF_RANGE = [("2**63", 2**63), ("-(2**63)-1", -(2**63) - 1), ("10**30", 10**30)]
OOR_FORMS = [(f"{n}_{kind}", conv(v)) for n, v in OUT_OF_RANGE for kind, conv in (("int", int), ("str", str))]
RANGE_MSG = "must be an integer within the 64-bit range"


@pytest.mark.parametrize("val_name,value", OOR_FORMS, ids=[v[0] for v in OOR_FORMS])
@pytest.mark.parametrize("cell", INT_CELLS, ids=[int_cell_id(c) for c in INT_CELLS])
def test_out_of_range_id_is_a_422_that_writes_nothing(world, cell, val_name, value):
    route, variant, field = cell
    prime(world, route)
    method, url, body = agent_request(world, route, variant, value, field=field)
    before = (snapshot(world), extra_state(world))

    resp = send(world, method, url, body)

    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"
    assert err_of(resp).startswith(f"{route}: field '{field}' {RANGE_MSG} (got "), err_of(resp)
    assert "\n" not in err_of(resp)
    assert (snapshot(world), extra_state(world)) == before, "a refused request wrote something"


@pytest.mark.parametrize("val_name,value", OOR_FORMS, ids=[v[0] for v in OOR_FORMS])
def test_read_out_of_range_creates_no_agent_row(world, val_name, value):
    # The old 500 came AFTER upsert_agent: the sender row survived the failed request.
    tid, _ = seed_thread(world)
    assert q(world, "SELECT name FROM agents WHERE name = 'bob'") == []
    resp = send(world, "POST", f"/api/thread/{tid}/read", {"agent": "bob", "last_read_post_id": value})
    assert resp.status_code == 422, resp.data
    assert q(world, "SELECT name FROM agents WHERE name = 'bob'") == []
    assert q(world, "SELECT * FROM reads") == []


def test_out_of_range_message_does_not_echo_a_huge_value(world):
    tid, _ = seed_thread(world)
    resp = send(world, "POST", f"/api/thread/{tid}/read",
                {"agent": "bob", "last_read_post_id": "9" * 5000})
    assert resp.status_code == 422, resp.data
    assert len(err_of(resp)) < 200, len(err_of(resp))
    assert "9" * 40 not in err_of(resp)


@pytest.mark.parametrize("value", [INT64_MAX, INT64_MIN, str(INT64_MAX), str(INT64_MIN)],
                         ids=["max_int", "min_int", "max_str", "min_str"])
@pytest.mark.parametrize("cell", INT_CELLS, ids=[int_cell_id(c) for c in INT_CELLS])
def test_boundary_ids_pass_the_guard(world, cell, value):
    route, variant, field = cell
    method, url, body = agent_request(world, route, variant, value, field=field)
    resp = send(world, method, url, body)
    # Whatever the route then does with a nonexistent id (404; a read just stores the
    # watermark -> 200) -- it is neither the guard's 422 nor a 500.
    assert resp.status_code in (200, 404), f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"


@pytest.mark.parametrize("cell", INT_CELLS, ids=[int_cell_id(c) for c in INT_CELLS])
def test_zero_padded_digit_string_beyond_the_int_str_limit_is_not_a_500(world, cell):
    # 5000 leading zeros is the value 5 but int() refuses a >4300-digit string; the guard
    # must neither crash on it nor change the route's own answer (400 "must be an integer").
    route, variant, field = cell
    method, url, body = agent_request(world, route, variant, "0" * 5000 + "5", field=field)
    resp = send(world, method, url, body)
    assert resp.status_code == 400, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"


@pytest.mark.parametrize("cell", INT_CELLS, ids=[int_cell_id(c) for c in INT_CELLS])
def test_huge_digit_string_is_a_422_not_a_crash(world, cell):
    route, variant, field = cell
    method, url, body = agent_request(world, route, variant, "7" * 5000, field=field)
    resp = send(world, method, url, body)
    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"


# expected_republish_seconds (agents/status): not routed through `_require_int` (a digit
# string stays the 400 set_agent_status gave it), but a real int beyond 64 bits is the same
# class and the same 422.

def status_body(value):
    return {"agent": "bob", "state": "working", "expected_republish_seconds": value}


@pytest.mark.parametrize("value", [2**63, 10**30, -(2**63) - 1], ids=["2**63", "10**30", "-(2**63)-1"])
def test_expected_republish_seconds_out_of_range_int_is_a_422_that_writes_nothing(world, value):
    prime(world, STATUS)
    before = snapshot(world)
    resp = send(world, "POST", "/api/agents/status", status_body(value))
    assert resp.status_code == 422, f"{resp.status_code}: {err_of(resp) or resp.data[:200]!r}"
    assert err_of(resp).startswith(f"{STATUS}: field 'expected_republish_seconds' {RANGE_MSG} (got "), err_of(resp)
    assert snapshot(world) == before, "a refused request wrote something"


def test_expected_republish_seconds_boundaries_and_strings_keep_their_answers(world):
    prime(world, STATUS)
    # max int64 is a valid non-negative int; the negative boundary is still set_agent_status' 400.
    r = send(world, "POST", "/api/agents/status", status_body(INT64_MAX))
    assert r.status_code == 200, r.data
    assert q(world, "SELECT expected_republish_seconds FROM agents WHERE name='bob'") == [(INT64_MAX,)]
    r = send(world, "POST", "/api/agents/status", status_body(INT64_MIN))
    assert r.status_code == 400 and "non-negative int" in err_of(r), r.data
    # digit strings were and stay a 400 from set_agent_status (any magnitude).
    for s in ("5", str(2**63), str(10**30)):
        r = send(world, "POST", "/api/agents/status", status_body(s))
        assert r.status_code == 400 and "non-negative int" in err_of(r), (s, r.data)
