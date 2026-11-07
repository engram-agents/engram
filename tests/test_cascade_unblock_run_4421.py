"""Contract cells for #4421: eligible shapes at hand, resolve replies that say what is next, and the unblock run.

Written BEFORE the implementation (contract-first; project CLAUDE.md). Spec: sections A (eligible shapes + the
frontier `next_step` sentence), B (`next` / `done` on resolve replies), C (the unblock run: view, summary,
auto-start from a refused derive, done, conflict, sessions), D (the stateless promise).

Import-pattern contract (#948 wave B, R1): imports engram_core, engram_revision, engram_observation, engram_derive
directly and contains no ``import server``; the tool-surface cells live in test_unblock_run_tools_4421.py.

Every cell runs against a REAL engramd owner started in-process on a scratch home (the run state lives in the owner),
with the session id taken from CLAUDE_CODE_SESSION_ID exactly as the MCP server takes it. Nothing touches ~/.engram:
the owner socket is derived from the scratch data dir the core is configured with, never from the ambient ENGRAM_HOME.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 -- must follow sys.path setup
from _owner_platform import requires_linux_owner  # noqa: E402
from _owner_store import stamp_current_schema  # noqa: E402

import engram_core
import engram_derive
import engram_observation
import engram_revision
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

pytestmark = requires_linux_owner

J = json.loads
_SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "ANTHROPIC_SESSION_ID")
RETRACT = dict(resolution_kind="retracted", replacement_absent_because="NONE_BECAUSE_UNKNOWN",
               replacement_absent_note="test fixture -- replacement decision not under test")


@pytest.fixture
def env(monkeypatch):
    home = Path(tempfile.mkdtemp(prefix="ubi-"))
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    for n in _SESSION_ENV:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    rev = fresh_module(engram_revision)
    obs = importlib.import_module("engram_observation")
    derive = importlib.import_module("engram_derive")
    ur = importlib.import_module("engram_unblock_run")
    core = sys.modules["engram_core"]
    core._configure_paths(home)
    core._ensure_data_dir()
    core._get_db().close()                                # built by the real migration ladder (lands at the latest schema)
    stamp_current_schema(home / "knowledge.db")           # and stated explicitly: the owner refuses a store behind it
    owner = eo.EngramdOwner(home)
    owner.start(install_signal_handlers=False)
    clock = {"now": datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)}
    owner._unblock_now = lambda: clock["now"]
    e = SimpleNamespace(rev=rev, obs=obs, derive=derive, ur=ur, core=core, owner=owner, home=home, clock=clock,
                        mp=monkeypatch, seq=[0])
    try:
        yield e
    finally:
        try:
            owner.stop()
        except Exception:
            pass
        shutil.rmtree(home, ignore_errors=True)


# ------------------------------------------------------------------------------------------------ seeding


def ob(e, claim="A premise.", quote_type="attributed_analysis"):
    e.seq[0] += 1
    r = J(e.obs._add_observation_impl(
        url=f"https://example.com/s{e.seq[0]}", title=f"Source {e.seq[0]}", quoted_text=claim + " in the cited source",
        interpretation=f"Interpretation: {claim}", claim=claim, quote_type=quote_type))
    assert "error" not in r, r
    return r["observation_id"]


def dv(e, claim, premises):
    r = J(e.derive._derive_impl(claim=claim, supporting_ids=",".join(premises), logical_chain=f"Chain: {claim}",
                                reasoning_type="deductive_modus_ponens"))
    assert "error" not in r and "derivation_id" in r, r
    return r["derivation_id"]


def retract(e, node):
    r = J(e.rev._retract_impl(node_id=node, error_type="hallucinated_claim", reason="wrong",
                              replacement_absent_because="NONE_BECAUSE_UNKNOWN",
                              replacement_absent_note="test fixture -- replacement decision not under test"))
    assert "error" not in r, r


def supersede(e, old, new):
    r = J(e.rev._supersede_impl(old_node_id=old, new_node_id=new, supersede_reason="refined"))
    assert "error" not in r, r


def chain(e, n, label="c"):
    """ob -> d1 -> ... -> dn, ob retracted. Returns (ob, [d1..dn])."""
    o = ob(e, f"{label} root premise")
    ds, prev = [], o
    for i in range(n):
        d = dv(e, f"{label} link {i + 1}", [prev])
        ds.append(d)
        prev = d
    retract(e, o)
    return o, ds


def resolve(e, node, **kw):
    kw = kw or RETRACT
    return J(e.rev._cascade_resolve_impl(node_id=node, **kw))


def frontier(e, **kw):
    return J(e.rev._cascade_frontier_session_impl(**kw))


def start_run(e, ids):
    return J(e.rev._cascade_start_unblock_impl(node_ids=ids))


def end_run(e):
    return J(e.rev._cascade_end_unblock_impl())


def run_of(e, session="sess-A"):
    view = e.ur.fetch(e.core.DATA_DIR, session, touch=False)
    return view["run"]


def try_derive(e, premises, claim="A new derivation."):
    return J(e.derive._derive_impl(claim=claim, supporting_ids=",".join(premises), logical_chain="chain",
                                   reasoning_type="deductive_modus_ponens"))


def use_session(e, name):
    e.mp.setenv("CLAUDE_CODE_SESSION_ID", name)


# ================================================================================ A. eligible shapes at hand


def test_every_packet_carries_the_eligible_shapes_of_the_function_resolve_checks(env):
    e = env
    _o, (d1,) = chain(e, 1)
    r = J(e.rev._cascade_frontier_impl())
    pk = r["packets"][0]
    conn = e.core._get_db()
    try:
        want = e.rev._cascade_eligible_shapes(conn, d1)
    finally:
        conn.close()
    assert pk["eligible_shapes"] == want["eligible"] == ["retracted", "superseded"]
    assert pk["acknowledge_blocked_by"] == want["acknowledge_blocked_by"] and pk["acknowledge_blocked_by"]


def test_a_supersede_rooted_packet_lists_all_three_and_omits_acknowledge_blocked_by_when_empty(env):
    e = env
    o = ob(e, "old")
    d1 = dv(e, "depends", [o])
    supersede(e, o, ob(e, "new"))
    pk = J(e.rev._cascade_frontier_impl())["packets"][0]
    assert pk["id"] == d1
    assert pk["eligible_shapes"] == ["retracted", "superseded", "acknowledged"]
    assert "acknowledge_blocked_by" not in pk


def test_next_step_names_the_allowed_kinds_per_node_in_frontier_order_in_the_exact_form(env):
    e = env
    o1 = ob(e, "retracted root")
    d1 = dv(e, "downstream of a retract", [o1])
    retract(e, o1)
    o2 = ob(e, "superseded root")
    d2 = dv(e, "downstream of a supersede", [o2])
    supersede(e, o2, ob(e, "its successor"))
    r = J(e.rev._cascade_frontier_impl())
    order = [s["id"] for s in r["frontier"]]
    assert order == [d1, d2]
    assert r["next_step"] == (
        "Resolve each frontier node with engram_cascade_resolve. Allowed resolution_kind per node: "
        f"{d1}: retracted or superseded (not acknowledged); {d2}: retracted, superseded or acknowledged.")
    assert "(not acknowledged)" in r["next_step"]                      # (a) retract-downstream
    assert f"{d2}: retracted, superseded or acknowledged" in r["next_step"]   # (b) supersede-only lists all three


def test_next_step_covers_every_frontier_node_including_those_beyond_the_packet_cap(env):
    e = env
    ids = []
    for i in range(6):
        o = ob(e, f"root {i}")
        ids.append(dv(e, f"dependent {i}", [o]))
        retract(e, o)
    r = J(e.rev._cascade_frontier_impl())
    assert len(r["packets"]) == 5 and r["packets_limit"] == 5 and r["frontier_count"] == 6
    for d in ids:
        assert d in r["next_step"], (d, r["next_step"])


def test_an_empty_frontier_says_nothing_is_ready_and_next_step_sits_right_after_frontier_count(env):
    e = env
    r = J(e.rev._cascade_frontier_impl())
    assert r["next_step"] == "Nothing is ready to adjudicate."
    keys = list(r)
    assert keys[keys.index("frontier_count") + 1] == "next_step"
    assert keys[0] == "unblock_run" and r["unblock_run"] is None       # the impl alone knows no run


def test_a_contradiction_frontier_node_lists_resolved_in_the_same_words(env):
    e = env
    a, b = ob(e, "A holds"), ob(e, "A does not hold")
    ct = J(e.derive._contradict_impl(node_id_a=a, node_id_b=b, description="dispute"))["contradiction_id"]
    supersede(e, a, ob(e, "A, restated"))
    r = J(e.rev._cascade_frontier_impl())
    assert f"{ct}: retracted, superseded, acknowledged or resolved" in r["next_step"]


# ================================================================================ B. resolve says what is next


def test_next_is_exactly_the_mode_b_frontier_after_every_resolve_in_a_three_node_chain(env):
    e = env
    _o, (d1, d2, d3) = chain(e, 3)
    for node in (d1, d2, d3):
        r = resolve(e, node)
        assert "error" not in r, r
        assert r["next"] == J(e.rev._cascade_frontier_impl(None))      # evaluated after the commit, same function


def test_done_flips_true_exactly_on_the_last_resolve(env):
    e = env
    _o, (d1, d2, d3) = chain(e, 3)
    flags = [resolve(e, n)["done"] for n in (d1, d2, d3)]
    assert flags == [False, False, True]     # mutant `len(frontier) <= 1` says True on the middle resolve


def test_done_with_no_run_says_the_cascade_is_closed_and_not_before(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    first = resolve(e, d1)
    assert "the cascade is closed" not in json.dumps(first)
    last = resolve(e, d2)
    assert last["done"] is True and last["next"]["frontier"] == []
    assert "the cascade is closed" in last["next_step"]


def test_with_a_run_active_next_follows_the_run_and_not_the_whole_graph(env):
    e = env
    _o, (d1, d2) = chain(e, 2, "run")
    _u, (x,) = chain(e, 1, "unrelated")
    assert "error" not in start_run(e, [d2])
    r = resolve(e, d1)
    assert [s["id"] for s in r["next"]["frontier"]] == [d2]
    assert x not in json.dumps(r["next"])
    assert r["next"]["unblock_run"]["targets"] == [d2]
    assert r["unblock_run"] == r["next"]["unblock_run"]
    assert r["done"] is False
    assert list(r)[0] == "unblock_run"


def test_finishing_the_run_ends_it_and_says_so(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    start_run(e, [d2])
    resolve(e, d1)
    r = resolve(e, d2)
    assert r["done"] is True
    assert f"unblock run for {d2} complete; you can retry your derivation" in r["next_step"]
    assert run_of(e) is None
    assert end_run(e)["ended"] is None                                  # C5: nothing left to end


def test_a_refused_resolve_carries_neither_next_nor_done(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    r = resolve(e, d2)                                                  # d2 is not on the frontier
    assert "error" in r and "next" not in r and "done" not in r


# ================================================================================ C2. the roots parameter


def test_roots_with_one_id_is_byte_identical_to_node_id_over_three_shapes(env):
    e = env
    o1 = ob(e, "p1")
    x_frontier = dv(e, "frontier itself", [o1])
    o2 = ob(e, "p2")
    p2 = dv(e, "ancestor", [o2])
    x_one = dv(e, "one pending ancestor", [p2])
    o3 = ob(e, "p3")
    a, b = dv(e, "left", [o3]), dv(e, "right", [o3])
    x_diamond = dv(e, "diamond tip", [a, b])
    for o in (o1, o2, o3):
        retract(e, o)
    for x in (x_frontier, x_one, x_diamond):
        assert e.rev._cascade_frontier_impl(roots=[x]) == e.rev._cascade_frontier_impl(node_id=x), x


def test_roots_union_view_is_the_union_of_pending_ancestors_cleared_against_the_global_set(env):
    e = env
    _o1, (p1, x1) = chain(e, 2, "left")
    _o2, (p2, x2) = chain(e, 2, "right")
    r = J(e.rev._cascade_frontier_impl(roots=[x1, x2]))
    assert r["mode"] == "C" and r["node_id"] is None and r["roots"] == sorted([x1, x2])
    assert {s["id"] for s in r["frontier"]} == {p1, p2}
    assert r["pending_ancestor_count"] == 2
    resolve(e, p1)
    r2 = J(e.rev._cascade_frontier_impl(roots=[x1, x2]))
    assert {s["id"] for s in r2["frontier"]} == {x1, p2}
    assert r2["pending_ancestor_count"] == 1                            # p1 resolved: only p2 is still a pending ancestor


def test_node_id_and_roots_together_is_an_error_and_so_are_bad_roots(env):
    e = env
    _o, (d1,) = chain(e, 1)
    assert "error" in J(e.rev._cascade_frontier_impl(node_id=d1, roots=[d1]))
    assert "error" in J(e.rev._cascade_frontier_impl(roots=[]))
    assert "error" in J(e.rev._cascade_frontier_impl(roots="dv_1"))
    assert "error" in J(e.rev._cascade_frontier_impl(roots=["nope_1"]))


# ================================================================================ C1 / C3. start and end


def test_start_returns_the_summary_and_names_the_replaced_run(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    r = start_run(e, [d2])
    assert r["unblock_run"] == {"targets": [d2], "started_by": "explicit", "remaining_pending": 2}
    assert r["replaced"] is None
    r2 = start_run(e, [d1])
    assert r2["replaced"] == {"targets": [d2]}
    assert run_of(e)["targets"] == [d1]
    assert end_run(e)["ended"] == {"targets": [d1]}


@pytest.mark.parametrize("ids", [[], "dv_1", [1], None])
def test_start_refuses_a_bad_node_ids_list_and_creates_nothing(env, ids):
    e = env
    r = J(e.rev._cascade_start_unblock_impl(node_ids=ids))
    assert "error" in r and run_of(e) is None


def test_start_refuses_an_unknown_id_and_an_empty_view_and_replaces_nothing(env):
    e = env
    _o, (d1,) = chain(e, 1)
    start_run(e, [d1])
    assert "error" in start_run(e, ["dv_9999"])
    clean = ob(e, "an unmarked premise")
    r = start_run(e, [clean])
    assert "nothing pending under these nodes; nothing to unblock" in r["error"]
    assert run_of(e)["targets"] == [d1]                                 # the refusals replaced nothing


def test_a_frontier_call_reports_the_run_and_follows_its_view(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    _u, (x,) = chain(e, 1, "other")
    start_run(e, [d2])
    r = frontier(e)
    assert list(r)[0] == "unblock_run"
    assert r["unblock_run"] == {"targets": [d2], "started_by": "explicit", "remaining_pending": 2}
    assert {s["id"] for s in r["frontier"]} == {d1}
    explicit = frontier(e, node_id=x)                                   # an explicit node_id still means mode C of that node
    assert {s["id"] for s in explicit["frontier"]} == {x}
    assert explicit["unblock_run"]["targets"] == [d2]


def test_a_replayed_start_after_a_lost_reply_reports_the_original_replaced_line(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    real = ec.write
    calls = []

    def flaky(method, params, **kw):
        res = real(method, params, **kw)           # the owner DID execute it
        calls.append((method, kw.get("request_id")))
        if method == "cascade_unblock_start" and len([c for c in calls if c[0] == method]) == 1:
            return ec.Unavailable(socket_path="x", bound=1.0, agent_steps=["retry"], human_steps=["none"],
                                  request_id=kw.get("request_id"), reason="protocol")
        return res

    e.mp.setattr(ec, "write", flaky)
    r = start_run(e, [d2])
    starts = [c for c in calls if c[0] == "cascade_unblock_start"]
    assert len(starts) == 2 and starts[0][1] == starts[1][1]            # one retry with the SAME request id
    assert r["replaced"] is None                                        # a fresh id would say it replaced itself


# ================================================================================ D. the stateless promise


def test_with_and_without_a_run_the_same_marker_state_differs_only_in_unblock_run(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    _u, (x,) = chain(e, 1, "other")
    without = frontier(e, node_id=x)
    start_run(e, [d2])
    with_run = frontier(e, node_id=x)
    assert with_run["unblock_run"] is not None and without["unblock_run"] is None
    a = {k: v for k, v in without.items() if k != "unblock_run"}
    b = {k: v for k, v in with_run.items() if k != "unblock_run"}
    assert a == b                                                       # run state never leaks into frontier/packets


# ================================================================================ C4. auto-start from a refused derive


def test_c4a_one_stale_premise_is_refused_and_starts_a_run(env):
    e = env
    o = ob(e, "old")
    d1 = dv(e, "depends", [o])
    supersede(e, o, ob(e, "new"))
    r = try_derive(e, [d1])
    assert r["status"] == "blocked_stale"
    assert r["guidance"].splitlines()[0] == f"unblock run started for {d1}"
    assert run_of(e)["targets"] == [d1] and run_of(e)["started_by"] == "derive_refusal"


def test_c4b_one_tainted_premise_is_refused_starts_a_run_and_carries_the_second_line(env):
    e = env
    _o, (d1,) = chain(e, 1)
    r = try_derive(e, [d1])
    assert r["status"] == "blocked_tainted"
    lines = r["guidance"].splitlines()
    assert lines[0] == f"unblock run started for {d1}"
    assert lines[1] == ("acknowledged is never eligible downstream of a retract: the path ends with that premise "
                        "retracted (drop it from your derivation) or superseded (cite its successor), then retry.")
    assert run_of(e)["targets"] == [d1]


def test_c4c_one_tainted_and_one_stale_premise_make_ONE_run_with_both_targets(env):
    e = env
    _o, (t,) = chain(e, 1, "tainted")
    o2 = ob(e, "old")
    s = dv(e, "stale one", [o2])
    supersede(e, o2, ob(e, "new"))
    r = try_derive(e, [t, s])
    assert r["status"] == "blocked_tainted"
    assert [p["id"] for p in r["tainted_premises"]] == [t]              # the refusal alone names only the tainted one
    assert run_of(e)["targets"] == sorted([t, s])
    assert r["guidance"].splitlines()[0] == f"unblock run started for {', '.join(sorted([t, s]))}"


def test_c4d_a_directly_retracted_premise_only_is_refused_unchanged_and_starts_no_run(env):
    e = env
    o = ob(e, "retracted directly")
    retract(e, o)
    r = try_derive(e, [o])
    assert r["status"] == "blocked_tainted"
    assert "unblock run" not in json.dumps(r) and run_of(e) is None


def test_c4e_a_directly_superseded_premise_only_is_refused_unchanged_and_starts_no_run(env):
    e = env
    o = ob(e, "superseded directly")
    supersede(e, o, ob(e, "its successor"))
    r = try_derive(e, [o])
    assert r["status"] == "blocked_stale"
    assert "unblock run" not in json.dumps(r) and run_of(e) is None


def test_c4d2_a_retracted_premise_that_STILL_carries_a_marker_is_never_a_target(env):
    """(d) alone cannot tell the exclusion from 'it has no marker anyway': a directly retracted node carries none. This
    state (retracted AND a pending marker) is reached by surgery only, which is exactly why the rule names it."""
    e = env
    _o, (d1,) = chain(e, 1)
    conn = e.core._get_db()
    conn.execute("UPDATE nodes SET status = 'retracted', is_current = 0 WHERE id = ?", (d1,))
    conn.commit()
    conn.close()
    r = try_derive(e, [d1])
    assert r["status"] == "blocked_tainted"
    assert "unblock run" not in json.dumps(r) and run_of(e) is None


def test_c4e2_a_superseded_premise_that_STILL_carries_a_marker_is_never_a_target(env):
    e = env
    o = ob(e, "old")
    d1 = dv(e, "depends", [o])
    supersede(e, o, ob(e, "new"))
    successor = ob(e, "the successor of the marked premise")
    conn = e.core._get_db()
    conn.execute("UPDATE nodes SET is_current = 0, superseded_by = ? WHERE id = ?", (successor, d1))
    conn.commit()
    conn.close()
    r = try_derive(e, [d1])
    assert r["status"] == "blocked_stale"
    assert "unblock run" not in json.dumps(r) and run_of(e) is None


def test_c4f_tainted_premise_resolved_superseded_then_replacement_accepted_original_refused_with_successor(env):
    e = env
    _o, (d1,) = chain(e, 1)
    assert try_derive(e, [d1])["status"] == "blocked_tainted"
    good = ob(e, "a sound premise")
    replacement = dv(e, "the corrected derivation", [good])
    r = resolve(e, d1, resolution_kind="superseded", replacement_id=replacement)
    assert r["done"] is True and f"unblock run for {d1} complete" in r["next_step"]
    ok = try_derive(e, [replacement], claim="Built on the replacement.")
    assert ok.get("status") == "created", ok
    refused = try_derive(e, [d1], claim="Built on the original.")
    assert refused["status"] == "blocked_stale"
    assert refused["stale_premises"][0]["stale_because"][0]["replaced_by_id"] == replacement
    assert run_of(e) is None                                            # a superseded premise is no run target


def test_c4g_blocked_contradicted_and_other_refusals_start_no_run(env):
    e = env
    a, b = ob(e, "A holds"), ob(e, "A does not hold")
    J(e.derive._contradict_impl(node_id_a=a, node_id_b=b, description="dispute"))
    r = try_derive(e, [a])
    assert r["status"] == "blocked_contradicted"
    assert "unblock run" not in json.dumps(r) and run_of(e) is None


# ================================================================================ C6. conflict


def test_c6_a_refusal_with_a_run_active_names_both_sets_and_gives_the_three_exact_calls(env):
    e = env
    _o, (d1, d2) = chain(e, 2, "active")
    start_run(e, [d2])                                                  # 2 pending under it
    _p, (t1,) = chain(e, 1, "t1")
    o = ob(e, "old")
    t2 = dv(e, "stale", [o])
    supersede(e, o, ob(e, "new"))
    r = try_derive(e, [t1, t2])
    new = sorted([t1, t2])
    want = (
        f"Blocked by marked premises {', '.join(new)}. An unblock run is already active for {d2} (2 pending). "
        "Choose one:\n"
        "(a) finish that run first: call engram_cascade_frontier() and resolve the node it gives;\n"
        f"(b) switch to the new premises: call engram_cascade_start_unblock(node_ids={json.dumps(new, separators=(',', ':'))});\n"
        f"(c) do both together: call engram_cascade_start_unblock(node_ids={json.dumps(sorted([d2, *new]), separators=(',', ':'))}).")
    assert want in r["guidance"]
    b = re.search(r"\(b\).*?node_ids=(\[.*?\])\)", r["guidance"]).group(1)
    c = re.search(r"\(c\).*?node_ids=(\[.*?\])\)", r["guidance"]).group(1)
    assert json.loads(b) == new and json.loads(c) == sorted([d2, *new])
    assert run_of(e)["targets"] == [d2]                                 # not replaced, no second run
    assert "unblock run started" not in r["guidance"]


def test_c6_targets_already_inside_the_active_run_get_the_finish_it_sentence_and_no_menu(env):
    e = env
    _o, (d1, d2) = chain(e, 2, "active")
    start_run(e, [d1, d2])
    r = try_derive(e, [d2])                                             # d2 is marked and already a target
    assert r["status"] == "blocked_tainted"
    want = (f"Blocked by marked premises {d2}; they are already in the active unblock run for {', '.join(sorted([d1, d2]))} "
            "(2 pending). Finish it: call engram_cascade_frontier() and resolve the node it gives, then retry your derivation.")
    assert want in r["guidance"]
    for menu in ("Choose one", "(a)", "(b)", "(c)", "engram_cascade_start_unblock"):
        assert menu not in r["guidance"], menu
    assert "acknowledged is never eligible downstream of a retract" in r["guidance"]   # the tainted line still follows
    assert run_of(e)["targets"] == sorted([d1, d2])                     # not replaced, no second run


def test_c6_overlapping_but_not_contained_targets_keep_the_full_menu_with_the_right_sets(env):
    e = env
    _o, (d1, d2) = chain(e, 2, "active")
    start_run(e, [d2])
    _p, (t1,) = chain(e, 1, "new")
    r = try_derive(e, [d2, t1])                                         # d2 overlaps the run; t1 does not
    new = sorted([d2, t1])
    assert "Choose one" in r["guidance"] and "already in the active unblock run" not in r["guidance"]
    b = re.search(r"\(b\).*?node_ids=(\[.*?\])\)", r["guidance"]).group(1)
    c = re.search(r"\(c\).*?node_ids=(\[.*?\])\)", r["guidance"]).group(1)
    assert json.loads(b) == new and json.loads(c) == sorted(set([d2]) | set(new))
    assert run_of(e)["targets"] == [d2]


def test_c6_the_conflict_text_is_absent_when_no_run_is_active(env):
    e = env
    _o, (d1,) = chain(e, 1)
    r = try_derive(e, [d1])
    assert "An unblock run is already active" not in json.dumps(r)
    assert "Choose one" not in json.dumps(r)


# ================================================================================ C7. sessions do not collide


def test_c7_two_sessions_hold_independent_runs_and_a_frontier_call_never_reports_the_other(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    start_run(e, [d2])
    use_session(e, "sess-B")
    r = frontier(e)
    assert r["unblock_run"] is None and r["mode"] == "B"
    assert start_run(e, [d1])["replaced"] is None
    use_session(e, "sess-A")
    assert frontier(e)["unblock_run"]["targets"] == [d2]
    end_run(e)
    use_session(e, "sess-B")
    assert frontier(e)["unblock_run"]["targets"] == [d1]


# ================================================================================ TTL and the expiry notice


def test_an_expired_run_is_reported_on_every_reply_until_start_or_end_and_not_after(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    start_run(e, [d2])
    expires = e.clock["now"] + timedelta(hours=24)
    e.clock["now"] += timedelta(hours=24, seconds=1)
    r = frontier(e)
    assert r["unblock_run"] is None and r["mode"] == "B"
    assert r["unblock_run_expired"] == {"targets": [d2], "expired_at": expires.isoformat()}
    again = frontier(e)                                                 # sticky: a second reply (the first may be lost) has it too
    assert again["unblock_run_expired"] == r["unblock_run_expired"] and again["unblock_run"] is None
    assert end_run(e)["unblock_run_expired"] == r["unblock_run_expired"]   # `end` is the one that removes it ...
    last = frontier(e)
    assert "unblock_run_expired" not in last and last["unblock_run"] is None  # ... and then it is gone


def test_a_resolve_after_expiry_carries_the_notice_and_the_mode_b_head(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    start_run(e, [d2])
    e.clock["now"] += timedelta(hours=25)
    r = resolve(e, d1)
    assert r["unblock_run"] is None and r["unblock_run_expired"]["targets"] == [d2]
    assert r["next"] == J(e.rev._cascade_frontier_impl(None))


def test_every_cascade_read_slides_the_expiry(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    start_run(e, [d2])
    e.clock["now"] += timedelta(hours=20)
    frontier(e)
    e.clock["now"] += timedelta(hours=20)                               # 40 h after start, 20 h after the last read
    assert frontier(e)["unblock_run"] is not None


# ================================================================================ loud on bad state / unreachable owner


def test_owner_unreachable_replies_carry_unavailable_never_null_and_start_end_are_loud(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    e.owner.stop()
    r = frontier(e)
    assert r["unblock_run"] == {"unavailable": "owner unreachable"}
    assert "error" in start_run(e, [d2]) and "error" in end_run(e)
    rr = resolve(e, d1)
    assert rr["unblock_run"] == {"unavailable": "owner unreachable"}
    d = try_derive(e, [d2])
    assert "owner unreachable" in d["guidance"] and "engram_cascade_frontier(node_id=" in d["guidance"]


def test_a_malformed_run_file_makes_replies_unavailable_and_errors_name_the_path(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    path = e.home / "unblock-runs.json"
    path.write_text("{garbage")
    r = frontier(e)
    assert set(r["unblock_run"]) == {"unavailable"} and str(path) in r["unblock_run"]["unavailable"]
    err = start_run(e, [d2])
    assert str(path) in err["error"]
    assert path.read_text() == "{garbage"


def test_no_session_id_start_refuses_naming_the_variable_and_derive_adds_one_line_and_starts_nothing(env):
    e = env
    _o, (d1,) = chain(e, 1)
    for n in _SESSION_ENV:
        e.mp.delenv(n, raising=False)
    err = start_run(e, [d1])
    assert "CLAUDE_CODE_SESSION_ID" in err["error"]
    r = try_derive(e, [d1])
    assert "no session id; use engram_cascade_frontier(node_id=...)" in r["guidance"]
    assert "unblock run started" not in r["guidance"]
    assert frontier(e)["unblock_run"] is None
    assert not (e.home / "unblock-runs.json").exists()


def test_a_payload_session_id_is_never_accepted_by_the_run_entry_points(env):
    e = env
    for fn in (e.rev._cascade_start_unblock_impl,):
        with pytest.raises(TypeError):
            fn(node_ids=["dv_1"], session_id="sess-X")
    with pytest.raises(TypeError):
        e.rev._cascade_frontier_session_impl(session_id="sess-X")


# ================================================================================ reviewer round 1 (#4428)


def test_a_healthy_unmarked_premise_is_never_a_run_target_nor_in_the_menu(env):
    e = env
    _o, (d1,) = chain(e, 1, "marked")
    healthy = ob(e, "a sound, unmarked, current premise")
    r = try_derive(e, [d1, healthy])
    assert run_of(e)["targets"] == [d1]
    assert r["guidance"].splitlines()[0] == f"unblock run started for {d1}"
    assert healthy not in r["guidance"]
    # and with a run already active the menu's id sets leave it out too
    _p, (t2,) = chain(e, 1, "other")
    start_run(e, [t2])
    r2 = try_derive(e, [d1, healthy])
    assert "Choose one" in r2["guidance"] and healthy not in r2["guidance"]


def test_a_failure_computing_next_after_the_commit_keeps_the_resolution_and_reports_next_error(env):
    e = env
    _o, (d1, d2) = chain(e, 2)

    def boom(*a, **k):
        raise RuntimeError("frontier exploded")

    e.mp.setattr(e.rev, "_cascade_frontier_impl", boom)
    r = resolve(e, d1)
    assert r["resolution_kind"] == "retracted" and "next_error" in r and "RuntimeError" in r["next_error"]
    assert "done" not in r and "next" not in r
    conn = e.core._get_db()
    try:
        assert conn.execute("SELECT is_current FROM nodes WHERE id = ?", (d1,)).fetchone()[0] == 0   # committed
    finally:
        conn.close()


def test_an_owner_end_failure_at_completion_is_reported_as_unblock_run_end_failed(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    start_run(e, [d2])
    resolve(e, d1)
    e.mp.setattr(e.ur, "end", lambda *a, **k: {"ok": False, "error": "owner exploded"})
    r = resolve(e, d2)
    assert r["done"] is True and r["unblock_run_end_failed"] == "owner exploded"


def test_an_empty_frontier_over_pending_ancestors_is_not_a_completed_run_and_keeps_the_run(env):
    e = env
    _o, (d1, d2) = chain(e, 2)
    start_run(e, [d2])
    real = e.rev._cascade_frontier_impl

    def anomaly(*a, **k):
        d = J(real(*a, **k))
        d["frontier"] = []                      # the invariant-1 shape: nothing resolvable, yet pending remains
        return json.dumps(d)

    e.mp.setattr(e.rev, "_cascade_frontier_impl", anomaly)
    r = resolve(e, d1)
    assert r["done"] is True
    assert "not a completed run" in r["next_step"] and "complete; you can retry" not in r["next_step"]
    assert run_of(e) is not None and run_of(e)["targets"] == [d2]


def test_an_unexpected_error_in_the_derive_side_unblock_logic_never_changes_the_refusal(env):
    e = env
    _o, (d1,) = chain(e, 1)

    def boom(*a, **k):
        raise RuntimeError("not a KeyError")

    e.mp.setattr(e.rev, "_cascade_unblock_on_derive_refusal", boom)
    r = try_derive(e, [d1])
    assert r["status"] == "blocked_tainted" and "RuntimeError" in r["unblock_run_error"]


def test_a_refused_derive_after_expiry_surfaces_the_expiry_notice_in_the_block_and_the_guidance(env):
    e = env
    _o, (d1,) = chain(e, 1)
    start_run(e, [d1])
    expires = e.clock["now"] + timedelta(hours=24)
    e.clock["now"] += timedelta(hours=25)
    r = try_derive(e, [d1])
    assert r["unblock_run_expired"] == {"targets": [d1], "expired_at": expires.isoformat()}
    assert f"your earlier unblock run for {d1} expired" in r["guidance"]


def test_end_unblock_on_an_expired_entry_reports_ended_null_and_the_expiry_notice(env):
    e = env
    _o, (d1,) = chain(e, 1)
    start_run(e, [d1])
    expires = e.clock["now"] + timedelta(hours=24)
    e.clock["now"] += timedelta(hours=25)
    r = end_run(e)
    assert r["ended"] is None
    assert r["unblock_run_expired"] == {"targets": [d1], "expired_at": expires.isoformat()}
    assert "unblock_run_expired" not in end_run(e)                      # delivered once


def test_an_owner_timeout_on_the_run_read_is_unavailable_never_null(env):
    e = env
    _o, (d1,) = chain(e, 1)
    e.mp.setattr(e.ur._ec, "read", lambda *a, **k: ec.Unavailable(
        socket_path="x", bound=2.0, agent_steps=["retry"], human_steps=["none"], reason="timeout"))
    r = frontier(e)
    assert r["unblock_run"] is not None and "unavailable" in r["unblock_run"]


# ================================================================================ colleague round (#4428): the notice is never burned


def _expired_session(e):
    _o, (d1, d2) = chain(e, 2)
    start_run(e, [d2])
    e.clock["now"] += timedelta(hours=25)
    return d1, d2


def test_f1_a_failing_frontier_call_after_expiry_carries_the_notice_instead_of_burning_it(env):
    e = env
    _expired_session(e)
    for kw in (dict(node_id="ob_9999"), dict(limit=-1)):
        r = frontier(e, **kw)
        assert "error" in r
        assert "unblock_run_expired" in r, ("notice lost", kw)
        assert "unblock_run_expired" in frontier(e), ("notice not sticky", kw)   # and the next call still has it
        e.clock["now"] += timedelta(hours=1)
        _expired_session(e)                                             # a fresh expired run for the next error shape


def test_f2_an_explicit_start_over_an_expired_entry_relays_the_notice_and_replaced_stays_null(env):
    e = env
    d1, d2 = _expired_session(e)
    r = start_run(e, [d1])
    assert r["status"] == "started" and r["replaced"] is None
    assert r["unblock_run_expired"]["targets"] == [d2]
    assert "unblock_run_expired" not in frontier(e)                     # delivered once, by the start reply


def test_f3_a_failure_computing_next_after_a_resolve_still_carries_the_notice(env):
    e = env
    d1, d2 = _expired_session(e)

    def boom(*a, **k):
        raise RuntimeError("frontier exploded")

    real = e.rev._cascade_frontier_impl
    e.mp.setattr(e.rev, "_cascade_frontier_impl", boom)
    r = resolve(e, d1)
    assert "next_error" in r and "done" not in r
    assert r["unblock_run_expired"]["targets"] == [d2]
    e.mp.setattr(e.rev, "_cascade_frontier_impl", real)
    assert frontier(e)["unblock_run_expired"]["targets"] == [d2]            # sticky: still there on the next call
