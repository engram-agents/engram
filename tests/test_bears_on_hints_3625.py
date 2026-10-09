"""Contract tests for the bears_on replacement HINT (engram-alpha#3625, tests first).

Lei's ruling, 2026-10-09 19:57Z (in session, verbatim, relayed on #3625):

  "for nodes retracted/superseded with replacement, we should hint on whether
  the bears_on still holds towards the replacement, and let the agent add a new
  edge (or retarget the old edge?). And edges targeting a non-current node,
  should simply be hidden and doesn't count towards the question's importance,
  non-current nodes are essentially just provenance kept, but not considered
  active knowledge."

Read as design:

  1. A non-current target is hidden and uncounted. (Already built by #3618.)
  2. When a target is SUPERSEDED, or RETRACTED WITH a replacement, the system
     HINTS that the question may still bear on the replacement. The agent
     decides, by adding a new edge. The system never adds it, never follows it
     silently, and never lists the replacement as a target of the question.
  3. ADD versus RETARGET was left open; this PR chooses ADD and keeps the old
     edge: the old edge records that the question once bore on the earlier
     claim, which is exactly the provenance the ruling says to keep.

THE CONTRACT

  Two moments carry the hint, with ONE shape:
    - engram_supersede(old, new) and engram_retract(node, replacement_id=new),
      when `old` / `node` is the target of a bears_on edge from a CURRENT
      question; and
    - engram_resolve(question), where #3618's "follow a superseded target to
      its successor and LIST it as a target" becomes a hint.

  Return keys (absent when there is nothing to hint, so every other response
  keeps its exact shape):
    bears_on_hints        list of entries, ordered by question id:
        {"question_id", "from_target", "replacement_id", "replacement_type",
         "add_edge_call": {"source_id", "target_id", "relation": "bears_on"}}
    bears_on_hint_prompt  one string naming the question(s), the old and the new
                          target, and saying the old edge stays as provenance.
  `add_edge_call` is EXACTLY a valid engram_add_edge payload. Because the hint
  advertises a runnable call, these tests run it AS PRINTED against a real
  engine and check the caller's target is still in the result.

  A hint is produced only when ALL hold:
    - the question is CURRENT (a retracted question's edges are hidden provenance);
    - the replacement is current and a legal bears_on target for that question
      (the shared endpoint rule would accept it);
    - the question is NOT already wired to the replacement.
  No replacement (retract WITHOUT replacement_id) -> no hint: nothing to point at.

  engram_resolve: `bears_on` lists LIVE DIRECT targets only. A superseded target
  is never listed; if it has a live successor that passes the conditions above it
  appears under `bears_on_hints`. `bears_on_omitted_noncurrent` counts the
  non-current targets that produced NO hint (retracted, or successor already
  wired, or successor not a legal target). For a supersede chain the hint names
  the END of the chain (core._current_successor), not the first replacement.

  Read-only: producing a hint changes no edge, node, confidence or cascade
  marker.

Mutation discipline: after the implementation lands, each guard here gets a
mutation where the new assertion fires and the pre-existing checks stay green.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_bears_on_3615 as T  # shared fixtures: fresh_server, seeding, _wire, _retract...


# ── helpers ─────────────────────────────────────────────────────────────────


def _supersede(server, old, new):
    return T._call(
        server.engram_supersede, old_node_id=old, new_node_id=new,
        supersede_reason="contract test: the claim was refined")


def _retract_with_replacement(server, node, replacement):
    return T._call(
        server.engram_retract, node_id=node, error_type="other",
        reason="contract test: replaced by a corrected claim",
        replacement_id=replacement)


def _hint_entry(question, old, new, new_type="conjecture"):
    return {
        "question_id": question,
        "from_target": old,
        "replacement_id": new,
        "replacement_type": new_type,
        "add_edge_call": {"source_id": question, "target_id": new, "relation": "bears_on"},
    }


def _bears_on_rows(server):
    conn = server.core._get_db()
    try:
        return sorted(
            (r["source_id"], r["target_id"]) for r in conn.execute(
                "SELECT source_id, target_id FROM edges WHERE relation = 'bears_on'"))
    finally:
        conn.close()


def _no_hint_keys(r):
    for k in ("bears_on_hints", "bears_on_hint_prompt"):
        assert k not in r, (k, r)


def _count_for(server, question):
    r = T._call(server.engram_list, node_type="question")
    return {n["id"]: n for n in r["nodes"]}[question]["bears_on_count"]


# ── supersede ───────────────────────────────────────────────────────────────


def test_supersede_hints_the_replacement_for_each_question_that_bears_on_the_old_target():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old form"), T._seed_conjecture(server, "new form")
    T._wire(server, qu, old)
    r = _supersede(server, old, new)
    assert "error" not in r, r
    assert r["bears_on_hints"] == [_hint_entry(qu, old, new)], r
    prompt = r["bears_on_hint_prompt"]
    assert qu in prompt and old in prompt and new in prompt, prompt
    assert "provenance" in prompt, prompt


def test_the_printed_add_edge_call_runs_as_printed_and_the_caller_target_survives():
    """ls_0055: a hint that advertises a runnable call must run exactly as printed."""
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    hint = _supersede(server, old, new)["bears_on_hints"][0]
    assert _count_for(server, qu) == 0, "the superseded target no longer counts"
    out = json.loads(server.engram_add_edge(payload_json=json.dumps(hint["add_edge_call"])))
    assert out.get("status") == "created", out
    assert out["source_id"] == qu and out["target_id"] == new, out  # the caller's target survived
    assert _count_for(server, qu) == 1, "the new edge counts: the agent re-wired it"
    # The old edge is KEPT as provenance (add, not retarget): row still there.
    assert (qu, old) in _bears_on_rows(server) and (qu, new) in _bears_on_rows(server)


def test_supersede_hint_is_read_only_and_cascades_nothing():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    before_rows = _bears_on_rows(server)
    before_conf = T._node_row(server, new)["confidence"]
    r = _supersede(server, old, new)
    assert r.get("bears_on_hints"), r
    assert _bears_on_rows(server) == before_rows, "producing a hint must add or remove no edge"
    assert T._node_row(server, new)["confidence"] == before_conf
    assert T._marker_count(server, qu) == 0
    assert T._node_row(server, qu)["is_current"] == 1


def test_supersede_without_bears_on_edges_has_unchanged_shape():
    server = T.fresh_server()
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    r = _supersede(server, old, new)
    assert "error" not in r, r
    _no_hint_keys(r)


def test_no_hint_when_the_question_is_already_wired_to_the_replacement():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    T._wire(server, qu, new)
    _no_hint_keys(_supersede(server, old, new))


def test_no_hint_for_a_non_current_question():
    """A retracted question's edges are hidden provenance: nothing to ask it."""
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    T._retract(server, qu)
    _no_hint_keys(_supersede(server, old, new))


def test_two_questions_give_two_entries_ordered_by_question_id():
    server = T.fresh_server()
    q1, q2 = T._seed_question(server, "one?"), T._seed_question(server, "two?")
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, q2, old)
    T._wire(server, q1, old)
    r = _supersede(server, old, new)
    assert [h["question_id"] for h in r["bears_on_hints"]] == sorted([q1, q2]), r


# ── retract ─────────────────────────────────────────────────────────────────


def test_retract_with_a_replacement_carries_the_same_hint():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "wrong"), T._seed_conjecture(server, "corrected")
    T._wire(server, qu, old)
    r = _retract_with_replacement(server, old, new)
    assert "error" not in r, r
    assert r["bears_on_hints"] == [_hint_entry(qu, old, new)], r
    assert new in r["bears_on_hint_prompt"], r


def test_retract_without_a_replacement_carries_no_hint():
    """Nothing to point at: the question's edge just becomes hidden provenance."""
    server = T.fresh_server()
    qu = T._seed_question(server)
    old = T._seed_conjecture(server, "wrong")
    T._wire(server, qu, old)
    r = T._call(
        server.engram_retract, node_id=old, error_type="other",
        reason="contract test", replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="contract test: no successor")
    assert "error" not in r, r
    _no_hint_keys(r)


# ── the shared builder (function-level contract) ────────────────────────────


def test_builder_refuses_a_replacement_the_endpoint_rule_would_refuse():
    """The hint must never advertise a call engram_add_edge would reject."""
    server = T.fresh_server()
    import engram_revision as rev
    qu = T._seed_question(server)
    old = T._seed_conjecture(server, "old")
    T._wire(server, qu, old)
    conn = server.core._get_db()
    try:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, is_current) "
            "VALUES ('zp_0001', 'person', 'p', '2026-01-01T00:00:00+00:00', 1)")
        conn.commit()
        assert rev._bears_on_replacement_hints(conn, old, "zp_0001") == []
        good = T._seed_conjecture(server, "good")
        hints = rev._bears_on_replacement_hints(conn, old, good)
        assert [h["add_edge_call"]["target_id"] for h in hints] == [good]
    finally:
        conn.close()


# ── resolve: the backstop, and the change to #3618's behaviour ──────────────


def test_resolve_hints_a_superseded_target_and_does_not_list_the_successor():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    _supersede(server, old, new)
    r = T._resolve(server, qu, T._strong_observation(server))
    assert r["bears_on"] == [], r  # the successor is NOT listed as a target
    assert r["bears_on_hints"] == [_hint_entry(qu, old, new)], r
    assert new in r["bears_on_hint_prompt"], r
    assert r.get("bears_on_omitted_noncurrent", 0) == 0, r
    # With a hint outstanding there IS something to re-assess: the "none is
    # current, nothing to re-assess" wording must not appear.
    assert "nothing to re-assess" not in r.get("bears_on_prompt", ""), r


def test_resolve_hint_call_runs_as_printed():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    _supersede(server, old, new)
    hint = T._resolve(server, qu, T._strong_observation(server))["bears_on_hints"][0]
    out = json.loads(server.engram_add_edge(payload_json=json.dumps(hint["add_edge_call"])))
    assert out.get("status") == "created" and out["target_id"] == new, out


def test_resolve_mixed_live_superseded_and_retracted():
    server = T.fresh_server()
    qu = T._seed_question(server)
    live = T._seed_conjecture(server, "live")
    sup_old, sup_new = T._seed_conjecture(server, "sup old"), T._seed_conjecture(server, "sup new")
    dead = T._seed_conjecture(server, "dead")
    for t in (live, sup_old, dead):
        T._wire(server, qu, t)
    _supersede(server, sup_old, sup_new)
    T._retract(server, dead)
    r = T._resolve(server, qu, T._strong_observation(server))
    assert [t["target_id"] for t in r["bears_on"]] == [live], r
    assert r["bears_on_hints"] == [_hint_entry(qu, sup_old, sup_new)], r
    assert r["bears_on_omitted_noncurrent"] == 1, r  # only the retracted one


def test_resolve_names_the_end_of_a_supersede_chain():
    server = T.fresh_server()
    qu = T._seed_question(server)
    a, b, c = (T._seed_conjecture(server, "a"), T._seed_conjecture(server, "b"),
               T._seed_conjecture(server, "c"))
    T._wire(server, qu, a)
    _supersede(server, a, b)
    _supersede(server, b, c)
    r = T._resolve(server, qu, T._strong_observation(server))
    assert r["bears_on_hints"] == [_hint_entry(qu, a, c)], r


def test_resolve_with_the_replacement_already_wired_gives_no_hint_and_counts_the_old_edge():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    T._wire(server, qu, new)
    _supersede(server, old, new)
    r = T._resolve(server, qu, T._strong_observation(server))
    assert [t["target_id"] for t in r["bears_on"]] == [new], r
    assert "bears_on_hints" not in r, r
    assert r["bears_on_omitted_noncurrent"] == 1, r


def test_resolve_hints_a_retracted_target_that_has_a_replacement():
    """Lei's ruling covers retracted-WITH-replacement too, and resolve is the
    backstop for an agent that missed the hint at retract time. (A retracted
    node has no supersede chain; its replacement is the node that `retracts` it.)"""
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "wrong"), T._seed_conjecture(server, "corrected")
    T._wire(server, qu, old)
    _retract_with_replacement(server, old, new)
    r = T._resolve(server, qu, T._strong_observation(server))
    assert r["bears_on"] == [], r
    assert r["bears_on_hints"] == [_hint_entry(qu, old, new)], r
    assert r.get("bears_on_omitted_noncurrent", 0) == 0, r


def test_resolve_names_the_end_of_a_chain_that_starts_with_a_retract():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, repl, repl2 = (T._seed_conjecture(server, "wrong"), T._seed_conjecture(server, "fix"),
                        T._seed_conjecture(server, "fix, refined"))
    T._wire(server, qu, old)
    _retract_with_replacement(server, old, repl)
    _supersede(server, repl, repl2)
    r = T._resolve(server, qu, T._strong_observation(server))
    assert r["bears_on_hints"] == [_hint_entry(qu, old, repl2)], r


def test_resolve_retracted_without_replacement_stays_omitted_with_no_hint():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old = T._seed_conjecture(server, "wrong")
    T._wire(server, qu, old)
    T._call(server.engram_retract, node_id=old, error_type="other", reason="contract test",
            replacement_absent_because="NONE_BECAUSE_UNKNOWN",
            replacement_absent_note="contract test: no successor")
    r = T._resolve(server, qu, T._strong_observation(server))
    assert "bears_on_hints" not in r, r
    assert r["bears_on_omitted_noncurrent"] == 1, r


def test_the_call_embedded_in_the_prompt_is_the_structured_call():
    """ls_0055: the string an agent copies must be the same call the structured
    field carries, and must run as printed."""
    import re
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    r = _supersede(server, old, new)
    m = re.search(r'(\{"source_id".*?\})\.', r["bears_on_hint_prompt"])
    assert m, r["bears_on_hint_prompt"]
    printed = json.loads(m.group(1))
    assert printed == r["bears_on_hints"][0]["add_edge_call"]
    out = json.loads(server.engram_add_edge(payload_json=json.dumps(printed)))
    assert out.get("status") == "created" and out["target_id"] == new, out


def test_resolve_retry_no_op_carries_the_hint():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    _supersede(server, old, new)
    ans = T._strong_observation(server)
    first = T._resolve(server, qu, ans)
    again = T._resolve(server, qu, ans)
    assert again.get("status") == "no_op", again
    assert again["bears_on_hints"] == first["bears_on_hints"] == [_hint_entry(qu, old, new)], (first, again)


def test_resolve_without_superseded_targets_has_no_hint_keys():
    server = T.fresh_server()
    qu, cj = T._seed_question(server), T._seed_conjecture(server)
    T._wire(server, qu, cj)
    r = T._resolve(server, qu, T._strong_observation(server))
    _no_hint_keys(r)
    assert [t["target_id"] for t in r["bears_on"]] == [cj]


def test_two_targets_sharing_one_replacement_give_ONE_hint_at_resolve():
    """Advertising the same add_edge_call twice is noise (reviewer round 1). One
    hint per (question, replacement). The deduped target is covered by that hint,
    so it is NOT counted as omitted."""
    server = T.fresh_server()
    qu = T._seed_question(server)
    old1, old2 = T._seed_conjecture(server, "wrong one"), T._seed_conjecture(server, "wrong two")
    shared = T._seed_conjecture(server, "the one correction")
    for o in (old1, old2):
        T._wire(server, qu, o)
    _retract_with_replacement(server, old1, shared)
    _retract_with_replacement(server, old2, shared)
    r = T._resolve(server, qu, T._strong_observation(server))
    assert len(r["bears_on_hints"]) == 1, r
    h = r["bears_on_hints"][0]
    assert h["replacement_id"] == shared and h["from_target"] in (old1, old2), r
    assert h["add_edge_call"] == {"source_id": qu, "target_id": shared, "relation": "bears_on"}
    assert r.get("bears_on_omitted_noncurrent", 0) == 0, r


def test_resolve_follows_a_retract_with_replacement_chain():
    """A replacement that was itself retracted WITH a replacement: the hint names
    the end of the chain (reviewer round 1)."""
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, r1, r2 = (T._seed_conjecture(server, "wrong"), T._seed_conjecture(server, "first fix"),
                   T._seed_conjecture(server, "second fix"))
    T._wire(server, qu, old)
    _retract_with_replacement(server, old, r1)
    _retract_with_replacement(server, r1, r2)
    r = T._resolve(server, qu, T._strong_observation(server))
    assert r["bears_on_hints"] == [_hint_entry(qu, old, r2)], r


def test_a_retract_chain_that_ends_without_a_replacement_stays_omitted():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, r1 = T._seed_conjecture(server, "wrong"), T._seed_conjecture(server, "first fix")
    T._wire(server, qu, old)
    _retract_with_replacement(server, old, r1)
    T._call(server.engram_retract, node_id=r1, error_type="other", reason="contract test",
            replacement_absent_because="NONE_BECAUSE_UNKNOWN",
            replacement_absent_note="contract test: the fix was wrong too")
    r = T._resolve(server, qu, T._strong_observation(server))
    assert "bears_on_hints" not in r, r
    assert r["bears_on_omitted_noncurrent"] == 1, r


def test_a_cycle_in_a_replacement_chain_terminates_on_the_visited_set_alone():
    """The hop cap would also stop an unguarded walk, so a cycle test must take
    the cap away: with it raised absurdly high, only the visited set can end the
    walk (an unguarded one recurses until RecursionError). Hand-built because no
    public verb can create a retract/supersede cycle."""
    server = T.fresh_server()
    import engram_revision as rev
    conn = server.core._get_db()
    try:
        for nid in ("zx_0001", "zy_0001"):
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, is_current) "
                "VALUES (?, 'conjecture', 'c', '2026-01-01T00:00:00+00:00', 0)", (nid,))
        for src, tgt in (("zy_0001", "zx_0001"), ("zx_0001", "zy_0001")):
            conn.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at) "
                "VALUES (?, ?, 'retracts', '2026-01-01')", (src, tgt))
        conn.commit()
        real = rev._BEARS_ON_CHAIN_HOP_CAP
        rev._BEARS_ON_CHAIN_HOP_CAP = 10 ** 6
        try:
            assert rev._bears_on_replacement_of(conn, "zx_0001") is None
        finally:
            rev._BEARS_ON_CHAIN_HOP_CAP = real
    finally:
        conn.close()


# ── failure containment ─────────────────────────────────────────────────────
#
# The hint is computed AFTER supersede / retract / resolve has committed, and it
# is advisory. A failure inside it must not turn a committed mutation into an
# error the caller would retry (a retry of a committed supersede fails with
# "not current"). Contract: the call returns its normal success payload, with
# NO `bears_on_hints`, and with `bears_on_hint_error` naming what went wrong
# ("<ExceptionType>: <message>"), so the agent can see the hint is missing.


def _break(monkeypatch_target, name):
    def boom(*a, **k):
        raise RuntimeError("simulated hint failure")
    setattr(monkeypatch_target, name, boom)


def test_a_failing_hint_does_not_fail_a_committed_supersede():
    server = T.fresh_server()
    import engram_revision as rev
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    _break(rev, "_bears_on_replacement_hints")
    r = _supersede(server, old, new)  # must NOT raise
    assert r.get("status") == "superseded", r
    assert "bears_on_hints" not in r, r
    assert r["bears_on_hint_error"].startswith("RuntimeError"), r
    assert T._node_row(server, old)["is_current"] == 0, "the supersede did commit"


def test_a_failing_hint_does_not_fail_a_committed_retract():
    server = T.fresh_server()
    import engram_revision as rev
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "wrong"), T._seed_conjecture(server, "corrected")
    T._wire(server, qu, old)
    _break(rev, "_bears_on_replacement_hints")
    r = _retract_with_replacement(server, old, new)  # must NOT raise
    assert r.get("status") == "retracted", r
    assert "bears_on_hints" not in r, r
    assert r["bears_on_hint_error"].startswith("RuntimeError"), r
    assert T._node_row(server, old)["is_current"] == 0


def test_a_failing_resolve_surfacing_does_not_fail_a_committed_resolve():
    server = T.fresh_server()
    import engram_revision as rev
    qu, cj = T._seed_question(server), T._seed_conjecture(server)
    T._wire(server, qu, cj)
    _break(rev, "_bears_on_resolve_fields")
    r = T._resolve(server, qu, T._strong_observation(server))  # must NOT raise
    assert "error" not in r and r.get("resolution_status") == "resolved", r
    assert "bears_on" not in r, r
    assert r["bears_on_hint_error"].startswith("RuntimeError"), r
    assert T._node_row(server, qu)["status"] == "resolved", "the resolve did commit"


def test_a_failing_surfacing_does_not_fail_the_idempotent_resolve_retry_either():
    """The no_op path is a second call site of the same surfacing (round 2 review:
    removing the wrapper there left every cell green, and a retried resolve then
    raised). Both the fresh answer and the retry must return normally, the retry
    as `no_op`, each carrying `bears_on_hint_error`."""
    server = T.fresh_server()
    import engram_revision as rev
    qu, cj = T._seed_question(server), T._seed_conjecture(server)
    T._wire(server, qu, cj)
    ans = T._strong_observation(server)
    _break(rev, "_bears_on_resolve_fields")
    first = T._resolve(server, qu, ans)  # must NOT raise
    again = T._resolve(server, qu, ans)  # must NOT raise
    assert "error" not in first and first.get("resolution_status") == "resolved", first
    assert again.get("status") == "no_op", again
    for r in (first, again):
        assert r["bears_on_hint_error"].startswith("RuntimeError"), r
        assert "bears_on" not in r, r


def test_no_hint_error_key_when_nothing_fails():
    server = T.fresh_server()
    qu = T._seed_question(server)
    old, new = T._seed_conjecture(server, "old"), T._seed_conjecture(server, "new")
    T._wire(server, qu, old)
    r = _supersede(server, old, new)
    assert "bears_on_hint_error" not in r, r
    plain = _supersede(server, T._seed_conjecture(server, "a"), T._seed_conjecture(server, "b"))
    assert "bears_on_hint_error" not in plain, plain

