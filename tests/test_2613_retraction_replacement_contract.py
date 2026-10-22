"""Contract-first tests for #2613 — the retraction replacement contract
(docs/internal/2613-retraction-replacement-contract.md).

Written before the implementation review lands, per this repo's own
"CONTRACT-FIRST tests, written before the impl" convention (a stateable
contract). Each test pins ONE claim from the design doc's §3/§5/§7/§9, named
in its own docstring so a later reader can trace test <-> design-doc section
without re-deriving it.

Coverage (Borges's build-order letter, verbatim list):
  1. refuse with no decision on BOTH paths (organic retract, cascade-resolve
     retract)
  2. the resolve path cannot be satisfied by omission
  3. both-supplied refused
  4. _UNKNOWN without a note refused
  5. parity refuses task->observation and permits conjecture->derivation
  6. the event row carries the decision on both paths
  7. dead_premises names the root's replacement
  8. the prediction-basis secondary retraction (#2281) still succeeds --
     machine-written, out of population (§9)
  9. can-it-fail: verified by hand (mutation walkthrough in the PR's own
     colleague-review pass), not re-encoded as a meta-test here.

Plus one test for the root-cause IntegrityError fix (§11 / Lei's rule):
a retraction whose 'retracts' edge write fails must not leave the node
retracted with no edge -- the whole retraction rolls back.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_2613_")
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
    return server


_URL_SEQ = [0]


def _seed_observation(server, claim="Test claim."):
    _URL_SEQ[0] += 1
    payload = {
        "url": f"https://example.com/a{_URL_SEQ[0]}", "title": f"A{_URL_SEQ[0]}",
        "quoted_text": claim,
        "interpretation": f"Interp: {claim}",
        "claim": claim,
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def _seed_derivation(server, claim, supporting_ids):
    if isinstance(supporting_ids, list):
        supporting_ids = ",".join(supporting_ids)
    payload = {
        "claim": claim,
        "supporting_ids": supporting_ids,
        "logical_chain": f"Derivation: {claim}",
        "reasoning_type": "deductive_modus_ponens",
    }
    r = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["derivation_id"]


def _seed_conjecture(server, claim="A conjecture."):
    payload = {"claim": claim, "basis": "test fixture"}
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["conjecture_id"]


def _seed_task(server, description="A task."):
    payload = {"description": description}
    r = json.loads(server.engram_add_task(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["task_id"]


# ── 1. Refuse with no decision on BOTH paths ────────────────────────────────


def test_organic_retract_refused_with_no_decision():
    """§3/§6: engram_retract refuses when neither replacement_id nor
    replacement_absent_because is supplied — the required-always gate."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "no decision supplied",
    })))
    assert "error" in r, r
    assert "replacement decision" in r["error"].lower(), r


def test_cascade_resolve_retracted_refused_with_no_decision():
    """§3 change 2: the resolution path must satisfy the SAME chokepoint as
    the organic path -- engram_cascade_resolve(resolution_kind='retracted')
    with no decision is refused, not silently satisfied by a default."""
    server = fresh_server()
    ob_id = _seed_observation(server, "Premise.")
    dv_id = _seed_derivation(server, "Depends on premise.", [ob_id])
    ret = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "wrong",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "setup",
    })))
    assert "error" not in ret, ret

    resp = json.loads(server.engram_cascade_resolve(payload_json=json.dumps({
        "node_id": dv_id, "resolution_kind": "retracted",
    })))
    assert "error" in resp, resp
    assert "replacement decision" in resp["error"].lower(), resp


# ── 2. The resolve path cannot be satisfied by omission ─────────────────────


def test_cascade_resolve_retracted_cannot_default_error_type_shape_into_satisfying_decision():
    """§3's own named anti-pattern: threading error_type='other' as a silent
    default satisfies THAT field without anyone deciding anything. The new
    replacement-decision fields must NOT get the same treatment -- omitting
    them is refused, exactly like error_type='other' being satisfied by a
    default is the shape this test guards against recurring."""
    server = fresh_server()
    ob_id = _seed_observation(server, "Premise.")
    dv_id = _seed_derivation(server, "Depends on premise.", [ob_id])
    json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "wrong",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "setup",
    })))
    # error_type genuinely omitted (defaults to "other" inside
    # _cascade_resolve_impl's own signature) -- confirm that default alone
    # does NOT let the call through; the replacement decision is still
    # missing and must still refuse.
    resp = json.loads(server.engram_cascade_resolve(payload_json=json.dumps({
        "node_id": dv_id, "resolution_kind": "retracted",
    })))
    assert "error" in resp, resp


# ── 3. Both-supplied refused ────────────────────────────────────────────────


def test_retract_refused_when_both_replacement_id_and_absent_because_supplied():
    """§3: replacement_id and replacement_absent_because are mutually
    exclusive -- supplying both is refused, not silently resolved by
    picking one."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    repl_id = _seed_observation(server, "Replacement.")
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "replacement_id": repl_id,
        "replacement_absent_because": "NONE_BECAUSE_NO_SUCCESSOR",
    })))
    assert "error" in r, r
    assert "mutually exclusive" in r["error"].lower(), r


# ── 4. _UNKNOWN without a note refused ──────────────────────────────────────


def test_retract_unknown_reason_without_note_refused():
    """§4: NONE_BECAUSE_UNKNOWN is the escape hatch and must name itself --
    the only member requiring replacement_absent_note."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
    })))
    assert "error" in r, r
    assert "replacement_absent_note" in r["error"], r


def test_retract_unknown_reason_with_note_succeeds():
    """Positive control for the above: supplying the note is sufficient."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "Could not determine a successor.",
    })))
    assert "error" not in r, r


def test_retract_non_unknown_reason_does_not_require_note():
    """The other three members do NOT require a note -- naming the escape
    hatch costs a sentence, naming a real category costs nothing (§4)."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "replacement_absent_because": "NONE_BECAUSE_NO_SUCCESSOR",
    })))
    assert "error" not in r, r


def test_retract_invalid_replacement_absent_because_rejected():
    """Closed set (§4) -- an unrecognized value is refused, not accepted as
    free text."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "replacement_absent_because": "NONE_BECAUSE_I_FELT_LIKE_IT",
    })))
    assert "error" in r, r
    assert "Invalid replacement_absent_because" in r["error"], r


# ── 5. Parity: refuses task->observation, permits conjecture->derivation ────


def test_retract_replacement_parity_refuses_task_for_observation():
    """§7: the floor is claim-bearing PARITY. A task (non-claim-bearing)
    replacing an observation (claim-bearing) is refused -- the exact defect
    that motivated §7 (measured on the live issue before #2612 retired
    mint-inside-retraction)."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    task_id = _seed_task(server)
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "replacement_id": task_id,
    })))
    assert "error" in r, r
    assert "claim-bearing parity" in r["error"], r


def test_retract_replacement_parity_permits_conjecture_replaced_by_derivation():
    """§7: parity is a FLOOR, not same-kind -- a conjecture (claim-bearing)
    may be replaced by a derivation (claim-bearing), the honest shape for a
    cross-epistemic-category correction that _supersede_impl itself routes
    to retract by name."""
    server = fresh_server()
    conj_id = _seed_conjecture(server)
    ob_id = _seed_observation(server, "Basis for the derivation.")
    dv_id = _seed_derivation(server, "Derived claim.", [ob_id])
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": conj_id, "error_type": "other",
        "reason": "The conjecture's honest correction is a derivation, not another conjecture.",
        "replacement_id": dv_id,
    })))
    assert "error" not in r, r
    assert r.get("replacement_id") == dv_id, r


def test_retract_replacement_parity_permits_same_claim_bearing_kind():
    """Positive control: observation replacing observation (both
    claim-bearing) is unaffected by the new floor."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    repl_id = _seed_observation(server, "Corrected claim.")
    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "wrong_citation", "reason": "x",
        "replacement_id": repl_id,
    })))
    assert "error" not in r, r


# ── 6. The event row carries the decision on both paths ────────────────────


def _last_edit_event(server, node_id, action):
    conn = server.core._get_db()
    try:
        row = conn.execute(
            "SELECT details FROM edit_history WHERE node_id = ? AND action = ? "
            "ORDER BY id DESC LIMIT 1",
            (node_id, action),
        ).fetchone()
        return json.loads(row["details"]) if row else None
    finally:
        conn.close()


def test_retract_event_carries_replacement_id():
    """§3 change 3 / §9: the retraction event (edit_history), not just the
    optional edge, records the decision -- this is what makes the invariant
    'checkable against the event log' (§9) actually true."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    repl_id = _seed_observation(server, "Corrected.")
    json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "wrong_citation", "reason": "x",
        "replacement_id": repl_id,
    })))
    details = _last_edit_event(server, ob_id, "retracted")
    assert details is not None, "no retraction event found"
    assert details.get("replacement_id") == repl_id, details


def test_retract_event_carries_replacement_absent_because_and_note():
    """Same claim, absence arm: replacement_absent_because and
    replacement_absent_note land on the event, and the note does NOT share
    the reason[:200] row (§4's hazard)."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "Genuinely could not determine a successor.",
    })))
    details = _last_edit_event(server, ob_id, "retracted")
    assert details.get("replacement_absent_because") == "NONE_BECAUSE_UNKNOWN", details
    assert details.get("replacement_absent_note") == (
        "Genuinely could not determine a successor."
    ), details
    assert details.get("reason") == "x", (
        "the absent-note must not have been folded into reason[:200]", details
    )


def test_cascade_resolve_retracted_event_carries_decision():
    """Same claim, via the resolution path: the delegated _retract_impl call
    writes the SAME event shape, confirming the chokepoint gives both paths
    the obligation by construction (§3)."""
    server = fresh_server()
    ob_id = _seed_observation(server, "Premise.")
    dv_id = _seed_derivation(server, "Depends on premise.", [ob_id])
    repl_id = _seed_observation(server, "Corrected premise.")
    json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "wrong",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "setup",
    })))
    resp = json.loads(server.engram_cascade_resolve(payload_json=json.dumps({
        "node_id": dv_id, "resolution_kind": "retracted",
        "replacement_id": repl_id, "note": "argument corrected",
    })))
    assert "error" not in resp, resp
    details = _last_edit_event(server, dv_id, "retracted")
    assert details.get("replacement_id") == repl_id, details


# ── 7. dead_premises names the root's replacement ───────────────────────────


def test_frontier_dead_premises_names_root_replacement():
    """§5: the cascade ROOT carries no cascade_markers row, so
    _cascade_frontier_node_summary's dead_premises could never name it
    before this fix. ob (the root) is dv's DIRECT premise; after retracting
    ob with a replacement, dv's frontier packet's dead_premises must
    include ob with that replacement -- read from ob's own event, not a
    (nonexistent) marker row."""
    server = fresh_server()
    ob_id = _seed_observation(server, "Root premise.")
    dv_id = _seed_derivation(server, "Direct dependent.", [ob_id])
    repl_id = _seed_observation(server, "Corrected root.")

    ret = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "wrong",
        "replacement_id": repl_id,
    })))
    assert "error" not in ret, ret

    frontier = json.loads(server.engram_cascade_frontier(payload_json=json.dumps({
        "node_id": dv_id,
    })))
    assert "error" not in frontier, frontier
    packets = frontier.get("packets") or []
    assert packets, frontier
    packet = packets[0]
    events = packet.get("events") or []
    assert events, packet
    event = events[0]
    assert event.get("root_is_direct_premise") is True, event
    dead = event.get("dead_premises") or []
    root_entries = [d for d in dead if d.get("premise_id") == ob_id]
    assert root_entries, (
        f"root {ob_id!r} missing from dead_premises entirely: {dead}"
    )
    assert root_entries[0]["resolution_replacement_id"] == repl_id, root_entries[0]
    assert root_entries[0]["resolution_kind"] == "retracted", root_entries[0]


def test_frontier_dead_premises_names_root_absent_reason():
    """Same claim, absence arm: a root retracted WITHOUT a replacement still
    surfaces its decision (the absent-reason) on the frontier packet, not
    just a silently-missing entry."""
    server = fresh_server()
    ob_id = _seed_observation(server, "Root premise.")
    dv_id = _seed_derivation(server, "Direct dependent.", [ob_id])

    ret = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "wrong",
        "replacement_absent_because": "NONE_BECAUSE_NO_SUCCESSOR",
    })))
    assert "error" not in ret, ret

    frontier = json.loads(server.engram_cascade_frontier(payload_json=json.dumps({
        "node_id": dv_id,
    })))
    event = frontier["packets"][0]["events"][0]
    dead = event.get("dead_premises") or []
    root_entries = [d for d in dead if d.get("premise_id") == ob_id]
    assert root_entries, dead
    assert root_entries[0].get("resolution_replacement_id") is None, root_entries[0]
    assert root_entries[0].get("replacement_absent_because") == "NONE_BECAUSE_NO_SUCCESSOR", root_entries[0]


# ── 8. The prediction-basis secondary retraction still succeeds (§9) ────────


def test_prediction_basis_secondary_retraction_unaffected_by_the_gate():
    """§9: retracting an observation_predictive that is a prediction's SOLE
    live evidential basis terminally retracts that prediction too (#2281),
    machine-written, no author decision possible for it. This is OUT of the
    invariant's population -- the outer call's own decision is required as
    usual, but the machine-written secondary event must NOT itself demand
    one (there is no author to supply it)."""
    server = fresh_server()
    ob_pred = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/pred-basis", "title": "Predictive basis",
        "quoted_text": "X will happen.",
        "interpretation": "A predictive claim.",
        "claim": "X will happen by next quarter",
        "quote_type": "attributed_analysis",
        "is_predictive": True,
        "predicted_event": "X happens",
        "resolution_timeframe": "2099-01-01",
    })))
    assert "error" not in ob_pred, ob_pred
    ob_id = ob_pred["observation_id"]
    pred_id = ob_pred.get("prediction_id")
    assert pred_id, f"expected a paired prediction to be minted: {ob_pred}"

    ret = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other",
        "reason": "The predictive basis turned out to be unfounded.",
        "replacement_absent_because": "NONE_BECAUSE_NO_SUCCESSOR",
    })))
    assert "error" not in ret, ret
    retracted_preds = [p["id"] for p in ret.get("retracted_predictions", [])]
    assert pred_id in retracted_preds, ret

    # The prediction's own event is machine-written and out of population --
    # it must exist, carry the cascade_via marker, and inherit the root's
    # decision for display, without itself needing one supplied.
    details = _last_edit_event(server, pred_id, "retracted")
    assert details is not None, "prediction's own retraction event missing"
    assert details.get("cascade_via") == "prediction_basis_retraction", details
    assert details.get("inherited_replacement_absent_because") == "NONE_BECAUSE_NO_SUCCESSOR", details


# ── Root-cause fix: fail loud on a swallowed IntegrityError (§11) ──────────


def test_retract_replacement_edge_failure_rolls_back_the_whole_retraction():
    """§11 / Lei's rule (2026-09-07): a bare `pass` on the 'retracts' edge
    INSERT's IntegrityError used to let the retraction COMMIT while its own
    event reported a replacement whose edge was never written. Force that
    exact failure (duplicate PK via a pre-existing identical edge) and
    confirm the retraction is refused with NO state changed -- the node
    stays current, not silently retracted."""
    server = fresh_server()
    ob_id = _seed_observation(server)
    repl_id = _seed_observation(server, "Replacement.")

    # Pre-insert the exact edge the retraction would try to write, so its
    # own INSERT collides on the primary key / uniqueness constraint.
    conn = server.core._get_db()
    try:
        now = server.core._now()
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) "
            "VALUES (?, ?, 'retracts', ?)",
            (repl_id, ob_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    r = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": ob_id, "error_type": "other", "reason": "x",
        "replacement_id": repl_id,
    })))
    assert "error" in r, (
        "expected the retraction to be refused when its edge write fails", r
    )

    # No partial state: the node must still be current, not retracted.
    conn = server.core._get_db()
    try:
        row = conn.execute(
            "SELECT status, is_current FROM nodes WHERE id = ?", (ob_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["is_current"] == 1, (
        f"node was left retracted despite the edge write failing: {dict(row)}"
    )
    assert row["status"] != "retracted", dict(row)
