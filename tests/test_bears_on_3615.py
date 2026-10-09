"""Contract tests for the `bears_on` relation (engram-alpha#3615, tests first).

`bears_on` is a question -> claim relation: *once this question is answered,
the answer will count for or against that claim.* It is direction-neutral,
non-cascade, addable after creation, surfaced when the question is resolved,
and its out-degree is the open-question priority signal.

These tests are written BEFORE the implementation (contract-first convention,
Lei 2026-09-19 / 2026-09-30). On current dev every test that touches the new
relation is RED; the pre-existing checks stay green. The contract has four
surfaces:

  A. Classification  -- EDGE_CLASSIFICATIONS / EDGE_TIERS / VALID_RELATIONS.
  B. engram_add_edge / engram_remove_edge -- endpoint rule, refusals, idempotency.
     Also engram_ask's `bears_on` field (strict, all-or-nothing at creation).
  C. engram_resolve -- names the question's live targets in its return.
  D. engram_list    -- bears_on_count / bears_on_weight and sort_by="bears_on".
  E. Non-cascade    -- no confidence change, no taint, retract leaves rows.

DESIGN CHOICES MADE HERE (the issue left them open; each is a decision a
reviewer may overturn, and each is pinned by a test so the change is loud):

  1. Target types = CLAIM_BEARING_TYPES plus `contradiction`. Derived from
     core.CLAIM_BEARING_TYPES, never re-enumerated (the #2252 rule), so
     `axiom` and `theory` are legal targets too. A `prediction` is NOT
     claim-bearing and is refused.
  2. A NON-CURRENT target (retracted or superseded) follows the existing
     inspect convention: hidden from the question's edges view by default and
     counted in `truncated_superseded_count`, shown with include_superseded.
     (The issue text said "visible but marked"; that would be a second,
     different convention for the same situation.) The edge ROW is never
     deleted by a retract. The priority signal counts CURRENT targets only.
  3. engram_resolve lists LIVE targets under `bears_on`. A SUPERSEDED target
     is followed to its live successor (listed as the successor, marked
     `superseded_from`); `bears_on_omitted_noncurrent` counts only targets
     with no live successor (retracted, or a chain that ends non-current).
     The prompt says "partially resolved" when the resolve left the question
     partially_resolved. The keys are absent when the target is not a question
     or has no bears_on edges. They are also present on the idempotent no_op
     path, so a retry does not lose the prompt. (Round 1 of the PR listed
     CURRENT targets only; round 2 added the successor and wording rules.) `engram_cascade_resolve` is OUT OF SCOPE: it works
     on tainted/stale claim-bearing nodes, and a question is not
     claim-bearing, so no question reaches it (read from engram_resolve's
     RESOLVABLE_TYPES; cascade_resolve's own code was not read -- inferred).
  4. `bears_on_weight` = sum of the CURRENT targets' importance_score, rounded
     to 3 places. It is only a tie-break behind `bears_on_count`. This is the
     least-grounded choice: the issue says "importance/confidence stake" and
     names no formula.
  5. engram_ask gains `bears_on` (comma-separated ids, like context_ids) but,
     unlike context_ids (lenient), is STRICT: any invalid target refuses the
     whole call and creates no question.

Mutation discipline: after the implementation lands, each guard here gets a
mutation where the new assertion goes red and the pre-existing checks stay
green; results go in the PR body.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_bears_on_3615_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    return server


# ── seeding helpers ─────────────────────────────────────────────────────────


def _call(fn, **payload):
    return json.loads(fn(payload_json=json.dumps(payload)))


def _seed_question(server, text="Does the answer settle the claim?", **extra) -> str:
    r = _call(server.engram_ask, question=text, **extra)
    assert "error" not in r, r
    return r["question_id"]


def _seed_conjecture(server, claim="Test conjecture claim.") -> str:
    r = _call(server.engram_add_conjecture, claim=claim, basis="test basis")
    assert "error" not in r, r
    return r["conjecture_id"]


def _seed_observation(server, tag="x") -> str:
    r = _call(
        server.engram_add_observation,
        quoted_text=f"Observed {tag}.",
        interpretation="Test interpretation.",
        claim=f"Test observation claim {tag}.",
        quote_type="personal_communication",
        url=f"https://example.com/bears-on-{tag}",
        title=f"Test obs {tag}",
    )
    assert "error" not in r, r
    return r["observation_id"]


def _seed_derivation(server, tag="d") -> str:
    a, b = _seed_observation(server, f"{tag}a"), _seed_observation(server, f"{tag}b")
    r = _call(
        server.engram_derive,
        claim=f"Derived claim {tag}.",
        supporting_ids=f"{a},{b}",
        logical_chain="From a and b, the claim follows.",
        reasoning_type="deductive_modus_ponens",
    )
    assert "error" not in r, r
    return r["derivation_id"]


def _seed_contradiction(server) -> str:
    a, b = _seed_observation(server, "ca"), _seed_observation(server, "cb")
    r = _call(
        server.engram_contradict,
        node_id_a=a, node_id_b=b,
        description="a and b disagree.", root_cause="different sources",
    )
    assert "error" not in r, r
    return r["contradiction_id"]


def _seed_person(server) -> str:
    r = _call(server.engram_add_person, name="Bears On Person", role="tester")
    assert "error" not in r, r
    return r.get("person_id") or r["node_id"]


def _add(server, source, target, relation="bears_on"):
    return _call(server.engram_add_edge, source_id=source, target_id=target,
                 relation=relation)


def _wire(server, source, target):
    """Create a bears_on edge and ASSERT it was created, so a test that needs
    the edge can never pass vacuously on a build where the relation is absent."""
    r = _add(server, source, target)
    assert r.get("status") == "created", r
    return r


def _assert_endpoint_refusal(r, *must_name):
    """The refusal must come from the ENDPOINT rule, not from the relation
    being unknown/unaddable (which is what a build without bears_on says)."""
    assert "error" in r, r
    assert "not addable" not in r["error"], r["error"]
    assert "Unknown fields" not in r["error"], r["error"]
    for needle in must_name:
        assert needle in r["error"], (needle, r["error"])


def _retract(server, node_id):
    r = _call(
        server.engram_retract, node_id=node_id, error_type="other",
        reason="test fixture retraction",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement not under test",
    )
    assert "error" not in r, r


def _node_row(server, node_id):
    conn = server.core._get_db()
    try:
        return dict(conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())
    finally:
        conn.close()


def _edge_rows(server, source_id, relation="bears_on"):
    conn = server.core._get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM edges WHERE source_id = ? AND relation = ?",
            (source_id, relation)).fetchall()]
    finally:
        conn.close()


def _question_count(server) -> int:
    conn = server.core._get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM nodes WHERE type='question'").fetchone()[0]
    finally:
        conn.close()


# ── A. classification ───────────────────────────────────────────────────────


def test_classification_flags_pinned():
    server = fresh_server()
    c = server.core.EDGE_CLASSIFICATIONS.get("bears_on")
    assert c is not None, "bears_on missing from EDGE_CLASSIFICATIONS"
    assert c == {
        "cascade": False,
        "temporal_causal": False,   # the answer may pre-date the question's filing
        "dag_family": None,         # a promise about future evidence is not grounding
        "removable": True,          # over-applied wiring must be correctable
        "addable_after_creation": True,
        "provenance": False,
    }


def test_tier_is_relational_and_needs_no_divergence_ack():
    server = fresh_server()
    assert server.core.EDGE_TIERS.get("bears_on") == "relational"
    # relational tier expects temporal_causal=False, which matches; so no ack.
    assert "bears_on" not in server.core.EDGE_TIER_DIVERGENCE_ACK


def test_in_valid_relations_and_derived_sets():
    server = fresh_server()
    core = server.core
    assert "bears_on" in core.VALID_RELATIONS
    assert "bears_on" in core._ADDABLE_AFTER_CREATION_RELATIONS
    assert "bears_on" in core._REMOVABLE_EDGE_RELATIONS
    assert "bears_on" not in core.CASCADE_RELATIONS
    assert "bears_on" not in core.PROVENANCE_RELATIONS


def test_endpoint_helper_carries_the_rule_so_retype_cannot_bypass_it():
    """The shared endpoint helper (also used by engram_retype_edge) refuses a
    non-question source and a non-claim target, and accepts question -> claim."""
    server = fresh_server()
    import engram_cornerstone as cs

    def row(i, t):
        return {"id": i, "type": t}

    assert cs._relation_endpoint_error(
        "bears_on", row("qu_1", "question"), row("cj_1", "conjecture")) is None
    assert cs._relation_endpoint_error(
        "bears_on", row("qu_1", "question"), row("ct_1", "contradiction")) is None
    err = cs._relation_endpoint_error(
        "bears_on", row("ob_1", "observation_factual"), row("cj_1", "conjecture"))
    assert err and "question" in err
    err = cs._relation_endpoint_error(
        "bears_on", row("qu_1", "question"), row("pn_1", "person"))
    assert err and "person" in err


# ── B1. add_edge: accepted pairs ────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["conjecture", "derivation", "observation", "contradiction"])
def test_add_edge_accepts_question_to_claim_node(kind):
    server = fresh_server()
    qu = _seed_question(server)
    target = {
        "conjecture": _seed_conjecture,
        "derivation": _seed_derivation,
        "observation": _seed_observation,
        "contradiction": _seed_contradiction,
    }[kind](server)
    before_t, before_q = _node_row(server, target), _node_row(server, qu)
    r = _add(server, qu, target)
    assert r.get("status") == "created", r
    assert len(_edge_rows(server, qu)) == 1
    after_t, after_q = _node_row(server, target), _node_row(server, qu)
    # non-cascade: the relation changes no confidence and no status.
    assert after_t["confidence"] == before_t["confidence"]
    assert after_t["status"] == before_t["status"]
    assert after_q["status"] == before_q["status"] == "open"


def test_add_edge_is_idempotent():
    server = fresh_server()
    qu, cj = _seed_question(server), _seed_conjecture(server)
    assert _add(server, qu, cj).get("status") == "created"
    again = _add(server, qu, cj)
    assert again.get("status") == "no_op_already_exists", again
    assert len(_edge_rows(server, qu)) == 1


# ── B2. add_edge: refusals ──────────────────────────────────────────────────


def test_add_edge_refuses_non_question_source_and_names_the_allowed_type():
    server = fresh_server()
    ob, cj = _seed_observation(server), _seed_conjecture(server)
    r = _add(server, ob, cj)
    _assert_endpoint_refusal(r, "question")
    assert _edge_rows(server, ob) == []


def test_add_edge_refuses_person_target():
    server = fresh_server()
    qu, pn = _seed_question(server), _seed_person(server)
    r = _add(server, qu, pn)
    _assert_endpoint_refusal(r, "person")
    assert _edge_rows(server, qu) == []


def test_add_edge_refuses_question_to_question():
    """Also covers the self-loop case: a question is not a claim-bearing target."""
    server = fresh_server()
    q1, q2 = _seed_question(server, "one?"), _seed_question(server, "two?")
    _assert_endpoint_refusal(_add(server, q1, q2), "question")
    _assert_endpoint_refusal(_add(server, q1, q1), "question")
    assert _edge_rows(server, q1) == []


def test_add_edge_refuses_non_claim_bearing_prediction_target():
    server = fresh_server()
    core = server.core
    assert "prediction" not in core.CLAIM_BEARING_TYPES  # the premise of this test
    qu = _seed_question(server)
    # Seed a `prediction` row directly: engram_predict needs a full chain that
    # is not under test here; the endpoint rule only reads the node type.
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, status, is_current) "
            "VALUES ('pr_9001', 'prediction', 'p', '2026-01-01T00:00:00+00:00', 'open', 1)")
        conn.commit()
    finally:
        conn.close()
    _assert_endpoint_refusal(_add(server, qu, "pr_9001"), "prediction")


def test_add_edge_refuses_retracted_target():
    server = fresh_server()
    qu, cj = _seed_question(server), _seed_conjecture(server)
    _retract(server, cj)
    r = _add(server, qu, cj)
    assert "error" in r and "not current" in r["error"], r


def test_new_rule_does_not_leak_onto_other_relations():
    """The bears_on endpoint rule is keyed on the relation. Pairs that other
    relations accept today (observation -> goal via serves/instantiates) must
    still be accepted by the shared helper."""
    server = fresh_server()
    import engram_cornerstone as cs

    ob = {"id": "ob_1", "type": "observation_factual"}
    goal = {"id": "gl_1", "type": "goal"}
    assert cs._relation_endpoint_error("serves", ob, goal) is None
    assert cs._relation_endpoint_error("instantiates", ob, goal) is None


# ── B3. remove_edge ─────────────────────────────────────────────────────────


def test_remove_edge_removes_then_is_idempotent():
    server = fresh_server()
    qu, cj = _seed_question(server), _seed_conjecture(server)
    _wire(server, qu, cj)
    r = _call(server.engram_remove_edge, source_id=qu, target_id=cj, relation="bears_on")
    assert r.get("status") == "removed", r
    assert _edge_rows(server, qu) == []
    r2 = _call(server.engram_remove_edge, source_id=qu, target_id=cj, relation="bears_on")
    assert r2.get("status") == "no_op_not_found", r2


# ── B4. engram_ask `bears_on` field ─────────────────────────────────────────


def test_ask_with_bears_on_writes_edges_at_creation():
    server = fresh_server()
    cj, dv = _seed_conjecture(server), _seed_derivation(server)
    r = _call(server.engram_ask, question="Settles both?", bears_on=f"{cj},{dv}")
    assert "error" not in r, r
    targets = {e["target_id"] for e in _edge_rows(server, r["question_id"])}
    assert targets == {cj, dv}


@pytest.mark.parametrize("bad", ["cj_9999", "PERSON", "QUESTION", "RETRACTED"])
def test_ask_with_invalid_bears_on_refuses_whole_call_and_creates_no_question(bad):
    server = fresh_server()
    good = _seed_conjecture(server)
    bad_id = {
        "cj_9999": "cj_9999",                      # does not exist
        "PERSON": _seed_person(server),            # wrong type
        "QUESTION": _seed_question(server, "q"),   # wrong type
        "RETRACTED": None,
    }[bad]
    if bad == "RETRACTED":
        bad_id = _seed_conjecture(server, "to be retracted")
        _retract(server, bad_id)
    before = _question_count(server)
    r = _call(server.engram_ask, question="Should not exist?", bears_on=f"{good},{bad_id}")
    assert "error" in r, r
    assert "Unknown fields" not in r["error"], r["error"]
    assert bad_id in r["error"], r["error"]
    assert _question_count(server) == before, "a refused ask must not create a question"


def test_ask_context_ids_stays_lenient():
    """Unchanged behaviour: an unknown context id is skipped, not an error."""
    server = fresh_server()
    r = _call(server.engram_ask, question="Still lenient?", context_ids="cj_9999")
    assert "error" not in r, r


# ── C. engram_resolve names the targets ─────────────────────────────────────


def _resolve(server, qu, resolver):
    return _call(server.engram_resolve, target_id=qu, resolving_node_id=resolver)


def test_resolve_lists_live_targets_and_prompts_for_each():
    server = fresh_server()
    qu = _seed_question(server)
    cj, dv = _seed_conjecture(server), _seed_derivation(server)
    _wire(server, qu, cj)
    _wire(server, qu, dv)
    ans = _seed_observation(server, "answer")
    r = _resolve(server, qu, ans)
    assert "error" not in r, r
    listed = {t["target_id"]: t["target_type"] for t in r["bears_on"]}
    assert listed == {cj: "conjecture", dv: "derivation"}, r
    assert cj in r["bears_on_prompt"] and dv in r["bears_on_prompt"], r
    assert r.get("bears_on_omitted_noncurrent", 0) == 0


def test_resolve_without_bears_on_edges_has_unchanged_shape():
    server = fresh_server()
    qu, ans = _seed_question(server), _seed_observation(server, "answer")
    r = _resolve(server, qu, ans)
    assert "error" not in r, r
    for k in ("bears_on", "bears_on_prompt", "bears_on_omitted_noncurrent"):
        assert k not in r, (k, r)


def test_resolve_of_non_question_target_has_no_bears_on_keys():
    server = fresh_server()
    cj, ans = _seed_conjecture(server), _seed_observation(server, "answer")
    r = _call(server.engram_resolve, target_id=cj, resolving_node_id=ans,
              prediction_outcome="supported")
    assert "error" not in r, r
    assert "bears_on" not in r


def test_resolve_retry_no_op_still_carries_the_prompt():
    server = fresh_server()
    qu, cj = _seed_question(server), _seed_conjecture(server)
    _wire(server, qu, cj)
    ans = _seed_observation(server, "answer")
    first = _resolve(server, qu, ans)
    again = _resolve(server, qu, ans)
    assert again.get("status") == "no_op", again
    assert [t["target_id"] for t in again["bears_on"]] == [cj]
    assert [t["target_id"] for t in first["bears_on"]] == [cj]


def test_resolve_skips_retracted_target_and_counts_it():
    server = fresh_server()
    qu = _seed_question(server)
    live, dead = _seed_conjecture(server, "live"), _seed_conjecture(server, "dead")
    _wire(server, qu, live)
    _wire(server, qu, dead)
    _retract(server, dead)
    r = _resolve(server, qu, _seed_observation(server, "answer"))
    assert [t["target_id"] for t in r["bears_on"]] == [live], r
    assert r["bears_on_omitted_noncurrent"] == 1, r


def test_resolve_does_not_touch_target_confidence():
    server = fresh_server()
    qu, cj = _seed_question(server), _seed_conjecture(server)
    _wire(server, qu, cj)
    before = _node_row(server, cj)["confidence"]
    _resolve(server, qu, _seed_observation(server, "answer"))
    assert _node_row(server, cj)["confidence"] == before


def _supersede(server, old, new):
    r = _call(server.engram_supersede, old_node_id=old, new_node_id=new,
              supersede_reason="contract test: the claim was refined")
    assert "error" not in r, r


def _weak_observation(server, tag="weak"):
    """An editorial (0.35) observation: below the 0.70 resolution threshold."""
    r = _call(
        server.engram_add_observation,
        quoted_text="An opinion.", interpretation="weak support.",
        claim=f"Weak claim {tag}.", quote_type="editorial",
        url=f"https://example.com/bears-on-{tag}", title=f"weak {tag}",
    )
    assert "error" not in r, r
    return r["observation_id"]


def _strong_observation(server, tag="strong"):
    """A hard_data (0.95) observation: at or above the 0.70 resolution threshold.
    (`_seed_observation` files personal_communication at 0.40, which RESOLVES
    only partially; do not use it where a full resolution is the point.)"""
    r = _call(
        server.engram_add_observation,
        quoted_text=f"Measured {tag}.", interpretation="strong support.",
        claim=f"Strong claim {tag}.", quote_type="hard_data",
        url=f"https://example.com/bears-on-{tag}", title=f"strong {tag}",
    )
    assert "error" not in r, r
    return r["observation_id"]


def test_resolve_prompt_does_not_claim_answered_on_a_partial_resolution():
    """Below the 0.70 threshold the question stays partially_resolved, so the
    prompt must not say it was answered; a full resolution still says so."""
    server = fresh_server()
    qu, cj = _seed_question(server, "partial?"), _seed_conjecture(server)
    _wire(server, qu, cj)
    r = _resolve(server, qu, _weak_observation(server))
    assert r.get("resolution_status") == "partially_resolved", r
    assert cj in r["bears_on_prompt"]
    assert "partially" in r["bears_on_prompt"].lower(), r["bears_on_prompt"]
    assert "Now that it is answered" not in r["bears_on_prompt"], r["bears_on_prompt"]

    server2 = fresh_server()
    qu2, cj2 = _seed_question(server2, "full?"), _seed_conjecture(server2)
    _wire(server2, qu2, cj2)
    full = _resolve(server2, qu2, _strong_observation(server2))
    assert full.get("resolution_status") == "resolved", full
    assert "answered" in full["bears_on_prompt"]


def test_resolve_follows_a_superseded_target_to_its_live_successor():
    """A superseded target has a live successor: the stake must not read as
    'nothing to re-assess'. The successor is listed, marked as coming from the
    superseded id, and the prompt names it."""
    server = fresh_server()
    qu = _seed_question(server)
    old, new = _seed_conjecture(server, "old form"), _seed_conjecture(server, "new form")
    _wire(server, qu, old)
    _supersede(server, old, new)
    r = _resolve(server, qu, _seed_observation(server, "answer"))
    assert "error" not in r, r
    assert r["bears_on"] == [
        {"target_id": new, "target_type": "conjecture", "superseded_from": old}
    ], r
    assert new in r["bears_on_prompt"], r["bears_on_prompt"]
    assert "nothing to re-assess" not in r["bears_on_prompt"]
    assert r.get("bears_on_omitted_noncurrent", 0) == 0, r


def test_resolve_with_only_a_retracted_target_lists_nothing_and_says_so():
    """Retracted (no successor): nothing live to re-assess. The one-branch
    where every target is non-current."""
    server = fresh_server()
    qu, cj = _seed_question(server), _seed_conjecture(server)
    _wire(server, qu, cj)
    _retract(server, cj)
    r = _resolve(server, qu, _seed_observation(server, "answer"))
    assert r["bears_on"] == [], r
    assert r["bears_on_omitted_noncurrent"] == 1, r
    assert cj not in r["bears_on_prompt"], r["bears_on_prompt"]


# ── D. engram_list: the priority signal ─────────────────────────────────────


def _list_questions(server, **extra):
    r = _call(server.engram_list, node_type="question", **extra)
    assert r.get("status") == "ok", r
    return r["nodes"]


def test_list_question_entries_carry_count_and_weight_including_zero():
    server = fresh_server()
    q0, q1 = _seed_question(server, "none?"), _seed_question(server, "one?")
    _wire(server, q1, _seed_conjecture(server))
    by_id = {n["id"]: n for n in _list_questions(server)}
    assert by_id[q0]["bears_on_count"] == 0
    assert by_id[q1]["bears_on_count"] == 1
    assert isinstance(by_id[q1]["bears_on_weight"], float)


def test_list_non_question_entries_keep_their_shape():
    server = fresh_server()
    _seed_conjecture(server)
    r = _call(server.engram_list, node_type="conjecture")
    assert r["nodes"] and all("bears_on_count" not in n for n in r["nodes"])


def test_list_sort_by_bears_on_orders_by_count_desc_and_asc_reverses():
    server = fresh_server()
    q0, q1, q3 = (_seed_question(server, "zero?"), _seed_question(server, "one?"),
                  _seed_question(server, "three?"))
    _wire(server, q1, _seed_conjecture(server, "a"))
    for i in range(3):
        _wire(server, q3, _seed_conjecture(server, f"b{i}"))
    desc = [n["id"] for n in _list_questions(server, sort_by="bears_on")]
    assert desc == [q3, q1, q0], desc
    asc = [n["id"] for n in _list_questions(server, sort_by="bears_on", sort_order="asc")]
    assert asc == [q0, q1, q3], asc


def test_list_count_ignores_retracted_targets():
    server = fresh_server()
    qu = _seed_question(server)
    a, b = _seed_conjecture(server, "a"), _seed_conjecture(server, "b")
    _wire(server, qu, a)
    _wire(server, qu, b)
    assert {n["id"]: n for n in _list_questions(server)}[qu]["bears_on_count"] == 2
    _retract(server, b)
    assert {n["id"]: n for n in _list_questions(server)}[qu]["bears_on_count"] == 1


def test_list_equal_count_breaks_ties_on_target_importance():
    server = fresh_server()
    q_low, q_high = _seed_question(server, "low?"), _seed_question(server, "high?")
    t_low, t_high = _seed_conjecture(server, "low"), _seed_conjecture(server, "high")
    _wire(server, q_low, t_low)
    _wire(server, q_high, t_high)
    conn = server.core._get_db()
    try:
        conn.execute("UPDATE nodes SET importance_score = 0.9 WHERE id = ?", (t_high,))
        conn.execute("UPDATE nodes SET importance_score = 0.1 WHERE id = ?", (t_low,))
        conn.commit()
    finally:
        conn.close()
    by_id = {n["id"]: n for n in _list_questions(server, sort_by="bears_on")}
    order = [n["id"] for n in _list_questions(server, sort_by="bears_on")]
    assert by_id[q_low]["bears_on_count"] == by_id[q_high]["bears_on_count"] == 1
    assert by_id[q_high]["bears_on_weight"] == 0.9
    assert by_id[q_low]["bears_on_weight"] == 0.1
    assert order == [q_high, q_low], order


def test_default_compact_list_of_non_question_types_never_touches_bears_on():
    """The priority signal must not tax the default path: listing a type that
    has no bears_on edges runs no statement that mentions the relation."""
    server = fresh_server()
    _seed_conjecture(server, "plain")
    seen = []
    real = server.core._get_db

    def traced():
        conn = real()
        conn.set_trace_callback(seen.append)
        return conn

    server.core._get_db = traced
    try:
        r = _call(server.engram_list, node_type="conjecture")
    finally:
        server.core._get_db = real
    assert r.get("status") == "ok" and r["nodes"], r
    assert not [q for q in seen if "bears_on" in q], [q for q in seen if "bears_on" in q]


def test_post_fetch_lookup_drives_from_edges_not_from_a_node_scan():
    """The per-page lookup for question rows must start from the page's bears_on
    edges. Measured on a 20k-node graph, letting SQLite drive from nodes via
    idx_nodes_current (a scan of every current node) cost ~230 ms per call,
    against ~3 ms on dev and ~8 ms for the join it replaced. The bad plan only
    appears when the page carries MANY question ids (a one-id IN list takes a
    different path), so this seeds a realistic page of 60."""
    server = fresh_server()
    core = server.core
    n = 60
    conn = core._get_db()
    try:
        conn.executemany(
            "INSERT INTO nodes (id, type, claim, created_at, is_current, confidence, "
            "importance_score) VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', 1, ?, 0.5)",
            [(f"zq_{i:04d}", "question", f"q{i}", None) for i in range(n)]
            + [(f"zc_{i:04d}", "conjecture", f"c{i}", 0.4) for i in range(n)]
            + [(f"zo_{i:04d}", "observation_factual", f"o{i}", 0.9) for i in range(8 * n)],
        )
        conn.executemany(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'bears_on', '2026-01-01')",
            [(f"zq_{i:04d}", f"zc_{i:04d}") for i in range(n)],
        )
        conn.commit()
    finally:
        conn.close()
    seen = []
    real = core._get_db

    def traced():
        c = real()
        c.set_trace_callback(seen.append)
        return c

    core._get_db = traced
    try:
        r = _call(server.engram_list, node_type="question", limit=n)
    finally:
        core._get_db = real
    assert len(r["nodes"]) == n and all(x["bears_on_count"] == 1 for x in r["nodes"]), r["nodes"][:2]
    lookups = [q for q in seen if "bears_on" in q and "IN (" in q]
    assert len(lookups) == 1, seen
    conn = real()
    try:
        plan = " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + lookups[0]))
    finally:
        conn.close()
    assert "idx_nodes_current" not in plan, plan


def test_unfiltered_compact_list_still_carries_counts_on_question_rows():
    server = fresh_server()
    qu = _seed_question(server)
    _wire(server, qu, _seed_conjecture(server))
    r = _call(server.engram_list)
    by_id = {n["id"]: n for n in r["nodes"]}
    assert by_id[qu]["bears_on_count"] == 1, by_id[qu]
    assert all("bears_on_count" not in n for n in r["nodes"] if n["type"] != "question")


def test_list_unknown_sort_key_still_refused():
    """Pre-existing guard: adding `bears_on` must not loosen the allowed set."""
    server = fresh_server()
    r = _call(server.engram_list, node_type="question", sort_by="nonsense")
    assert r.get("status") == "error" and "bears_on" in r["error"], r


def test_list_sort_by_bears_on_with_limit_returns_the_top_k():
    """`limit` must apply AFTER the bears_on ordering, not before it."""
    server = fresh_server()
    q0, q1, q2 = (_seed_question(server, "zero?"), _seed_question(server, "one?"),
                  _seed_question(server, "two?"))
    _wire(server, q1, _seed_conjecture(server, "a"))
    for i in range(2):
        _wire(server, q2, _seed_conjecture(server, f"b{i}"))
    top = [n["id"] for n in _list_questions(server, sort_by="bears_on", limit=2)]
    assert top == [q2, q1], top


def test_list_sort_by_bears_on_works_in_fields_projection_mode():
    """The projected (fields_json) path joins the same aggregate for ordering."""
    server = fresh_server()
    q0, q2 = _seed_question(server, "zero?"), _seed_question(server, "two?")
    for i in range(2):
        _wire(server, q2, _seed_conjecture(server, f"p{i}"))
    r = _call(server.engram_list, node_type="question", sort_by="bears_on",
              fields_json=json.dumps(["id"]))
    assert r.get("status") == "ok", r
    assert [n["id"] for n in r["nodes"]] == [q2, q0], r


def test_retype_cannot_smuggle_a_non_question_edge_into_bears_on():
    """engram_retype_edge shares the endpoint helper: a LOOSE `cites` edge
    (not observation -> evidence, which is never retypable) from a non-question
    source cannot be retyped to bears_on. The refusal must come from the
    endpoint rule, not from the evidence-link guard."""
    server = fresh_server()
    ob, cj = _seed_observation(server, "retype"), _seed_conjecture(server, "t")
    conn = server.core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'cites', ?)", (ob, cj, server.core._now()))
        conn.commit()
    finally:
        conn.close()
    r = _call(server.engram_retype_edge, source_id=ob, target_id=cj,
              relation="cites", new_relation="bears_on",
              reason="contract test: must be refused")
    _assert_endpoint_refusal(r, "question")
    assert "not retypable" not in r["error"], r["error"]
    assert [e["target_id"] for e in _edge_rows(server, ob, relation="cites")
            if e["target_id"] == cj] == [cj], "the cites edge must survive"
    assert _edge_rows(server, ob) == []


# ── E. non-cascade and revision behaviour ───────────────────────────────────


def _marker_count(server, node_id) -> int:
    conn = server.core._get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM cascade_markers WHERE node_id = ?", (node_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_retracting_the_target_taints_nothing_and_keeps_the_edge_row():
    server = fresh_server()
    qu, cj = _seed_question(server), _seed_conjecture(server)
    _wire(server, qu, cj)
    _retract(server, cj)
    assert _marker_count(server, qu) == 0, "bears_on must not carry a cascade"
    assert _node_row(server, qu)["is_current"] == 1
    assert len(_edge_rows(server, qu)) == 1, "a retract never deletes the edge row"


def test_retracted_target_hidden_by_default_and_counted_shown_on_request():
    server = fresh_server()
    qu = _seed_question(server)
    live, dead = _seed_conjecture(server, "live"), _seed_conjecture(server, "dead")
    _wire(server, qu, live)
    _wire(server, qu, dead)
    _retract(server, dead)

    def targets(**extra):
        r = _call(server.engram_inspect, node_id=qu, view="edges", **extra)
        edges = r["topology"][qu]
        return {e["dst"] for e in edges if e["relation"] == "bears_on"}, r

    default, r = targets()
    assert default == {live}
    assert r.get("truncated_superseded_count") == 1
    shown, _ = targets(include_superseded=True)
    assert shown == {live, dead}


def test_retracting_the_question_leaves_the_target_untouched():
    server = fresh_server()
    qu, cj = _seed_question(server), _seed_conjecture(server)
    _wire(server, qu, cj)
    before = _node_row(server, cj)
    _call(
        server.engram_retract, node_id=qu, error_type="other",
        reason="test fixture retraction",
        replacement_absent_because="NONE_BECAUSE_UNKNOWN",
        replacement_absent_note="test fixture -- replacement not under test",
    )
    after = _node_row(server, cj)
    for col in ("confidence", "status", "is_current"):
        assert after[col] == before[col], col
    assert _marker_count(server, cj) == 0
