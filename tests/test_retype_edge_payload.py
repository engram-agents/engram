"""Tests for engram_retype_edge (issue #2265, epic #2252).

Design of record: docs/2265-2347-edge-correction-design.md sec.2. Contract
(write these tests first, per this repo's contract-first convention):
Ariadne's comment on issue #2265 ("Build unit cut..."), quoted here so the
seven numbered tests below map to it one-to-one:

  1. an allowed move succeeds and the endpoints are unchanged;
  2. a move not in the table is refused, and nothing changes;
  3. a stronger move (about -> supported_by) is refused;
  4. a node with a pending marker is refused;
  5. removing a formula-input premise recomputes the source's confidence,
     and the result reports old -> new;
  6. the edit-history row holds both relations and the reason; a missing
     reason is refused;
  7. atomicity: force a failure in step 5, and the edge is unchanged
     afterwards (it must not come out half-retyped).

Discriminating controls for 5 and 7 (Ariadne's own wording): a plausible-
wrong implementation that re-types without recomputing must fail test 5
while tests 1-4 stay green; one that commits the relation before the
recompute must fail test 7. These are properties of tests 5/7 themselves
(the assertions inside them), not separate test functions -- run the two
named mutations against the finished implementation and confirm the split
before calling this contract satisfied.

Tests 8 and 9 are a contract AMENDMENT from the colleague review round
(Borges's B1 + Ariadne's design-conformance read, both independent, both on
head 0a05719): step 5 of the design doc says "recompute the source node's
confidence. Then run the normal downstream recompute" -- the original
implementation only did the first half, leaving cascade descendants stale.
Both reviewers measured the same gap on the same scratch fixture (a
two-level derives_from chain) and pointed at the existing
engram_revision._cascade_recompute_confidence_transitive helper (already
used by engram_cascade_resolve) as the fix.

  8. retyping a premise away from D1 also recomputes D1's own dependent D2
     (a derivation that derives_from D1) -- D2's *stored* confidence must
     equal a fresh recompute, not the stale value computed from D1's old
     confidence. Discriminating control: the original single-node-recompute
     implementation fails this test while tests 1-7 stay green.
  9. retyping an edge whose SOURCE node is non-current (superseded/
     retracted) leaves that source's own stored confidence UNCHANGED --
     _cascade_recompute_confidence_transitive deliberately skips a
     non-current subject ("a dead node's confidence is a historical
     record"), and the original implementation did not honor that: it
     called _recompute_node_confidence unconditionally, which does not
     check is_current at all. Discriminating control: the original
     implementation fails this test (it rewrites the dead node's
     confidence) while tests 1-8 stay green.

Test 10 is Ariadne's design ruling on issue #2265 (posted after her
conformance read on head 362f6d0): retyping a derivation's LAST cascade
premise (derives_from / acknowledged_derives_from) is REFUSED, not silently
left at a stale confidence. A premise-less derivation breaks the
claim-bearing partition #2252 enforces; the refusal names supersede or
retract as the correct route.

  10. a derivation with exactly ONE cascade premise: `derives_from -> about`
      on that sole premise is refused, and the edge, confidence, and
      edit-history are all unchanged. Control: the SAME move on a
      two-premise derivation succeeds (test 5 already covers this half).

Allowed-move table under test (design doc sec.2):
  supported_by -> about
  derives_from -> about
  derives_from -> exemplifies   (target is a lesson or cornerstone)
  cites        -> about

Only `supported_by -> about` and `cites -> about` are confidence-recompute
no-ops by construction (neither relation is in CASCADE_RELATIONS, so neither
feeds _recompute_node_confidence's premise_ids query -- see ob_1689). The two
derives_from moves are the ones that actually exercise step 5.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_retype_edge_")
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


def _seed_observation(server, claim, quote_type="hard_data", suffix=""):
    payload = {
        "url": f"https://example.com/a{suffix}",
        "title": "T",
        "quoted_text": "X says Y is the case.",
        "interpretation": "X claims Y holds.",
        "claim": claim,
        "quote_type": quote_type,
    }
    o = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in o, o
    return o["observation_id"]


def _seed_derivation(server, premise_ids, claim="Derived claim"):
    payload = {
        "claim": claim,
        "supporting_ids": ",".join(premise_ids),
        "logical_chain": "The premises jointly establish the claim.",
        "reasoning_type": "deductive_modus_ponens",
    }
    d = json.loads(server.engram_derive(payload_json=json.dumps(payload)))
    assert "error" not in d, d
    return d["derivation_id"]


def _seed_raw_edge(relation, source_id, target_id):
    """Insert an edge directly, bypassing engram_add_edge's whitelist.

    supported_by/derives_from/cites are all addable_after_creation=False --
    in production they are minted by their owning creation tool
    (engram_add_observation's supporting_ids, engram_derive's
    supporting_ids), never by a generic add call. A retype target needs an
    ALREADY-EXISTING edge of the wrong relation as its "given" state, which
    is exactly what these tools would have produced; inserting it directly
    is the standard way this test suite constructs that prior state (see
    engram_core.py's own migration code, which does the same insert for the
    same reason).
    """
    import engram_core as core
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, ?, ?)",
            (source_id, target_id, relation, core._now()),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_pending_marker(node_id, root_node="ob_9999999"):
    """Insert a pending cascade marker directly -- the minimal fixture for
    exercising step 1's refusal in isolation, without driving the full
    retract/supersede cascade machine to produce one indirectly."""
    import engram_core as core
    conn = core._get_db()
    try:
        conn.execute(
            "INSERT INTO cascade_markers "
            "(node_id, cascade_event, root_node, root_op, status, created_at) "
            "VALUES (?, ?, ?, 'retract', 'pending', ?)",
            (node_id, "test-cascade-event-1", root_node, core._now()),
        )
        conn.commit()
    finally:
        conn.close()


def _get_edge(source_id, target_id):
    import engram_core as core
    conn = core._get_db()
    try:
        return conn.execute(
            "SELECT * FROM edges WHERE source_id = ? AND target_id = ?",
            (source_id, target_id),
        ).fetchone()
    finally:
        conn.close()


def _get_confidence(node_id):
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT confidence FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return row["confidence"] if row else None
    finally:
        conn.close()


def _retype(server, source_id, target_id, relation, new_relation, reason=None):
    payload = {
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "new_relation": new_relation,
    }
    if reason is not None:
        payload["reason"] = reason
    return json.loads(server.engram_retype_edge(payload_json=json.dumps(payload)))


# ── 1. Allowed move succeeds, endpoints unchanged ───────────────────────────


def test_allowed_move_supported_by_to_about_succeeds():
    server = fresh_server()
    premise_id = _seed_observation(server, "The premise claim.")
    source_id = _seed_observation(server, "The source claim (mis-filed as supported_by).")
    _seed_raw_edge("supported_by", source_id, premise_id)

    r = _retype(server, source_id, premise_id, "supported_by", "about",
                reason="Was only related, not a premise.")
    assert "error" not in r, r
    assert r["status"] == "retyped", r
    assert r["source_id"] == source_id
    assert r["target_id"] == premise_id
    assert r["old_relation"] == "supported_by"
    assert r["new_relation"] == "about"

    edge = _get_edge(source_id, premise_id)
    assert edge is not None, "edge must still exist between the same endpoints"
    assert edge["relation"] == "about"
    assert edge["source_id"] == source_id
    assert edge["target_id"] == premise_id


# ── 2. A move not in the table is refused, nothing changes ─────────────────


def test_move_not_in_table_refused():
    server = fresh_server()
    a = _seed_observation(server, "A", suffix="a")
    b = _seed_observation(server, "B", suffix="b")
    _seed_raw_edge("supported_by", a, b)

    r = _retype(server, a, b, "supported_by", "cites", reason="not an allowed move")
    assert "error" in r, r
    assert "is not an allowed re-type move" in r["error"], (
        "pin the move-table guard's own message -- a different guard "
        "standing in for it must not pass this assertion"
    )

    edge = _get_edge(a, b)
    assert edge is not None
    assert edge["relation"] == "supported_by", "edge must be untouched by the refusal"


# ── 3. A stronger move is refused (about -> supported_by) ──────────────────


def test_stronger_move_refused():
    # Kepler's merge-seat mutation (PR-3160 conclusion warrant, fast-follow):
    # adding ("about", "supported_by") to RETYPE_ALLOWED_MOVES did NOT redden
    # this test when the fixture seeded the source (a) before the target (b)
    # -- the call was still refused, but by the UNRELATED temporal_causal
    # guard ("supported_by requires the source to be created at-or-after the
    # target"), not by the table check this test claims to discriminate.
    # Seeding the TARGET first (b, then a) satisfies temporal_causal's
    # at-or-after requirement, so that guard passes silently and the move
    # table is the only thing left that can refuse the call.
    server = fresh_server()
    b = _seed_observation(server, "B", suffix="d")
    a = _seed_observation(server, "A", suffix="c")
    _seed_raw_edge("about", a, b)

    r = _retype(server, a, b, "about", "supported_by", reason="trying to strengthen")
    assert "error" in r, r
    assert "is not an allowed re-type move" in r["error"], (
        "pin the move-table guard's own message (Borges's PR-3167 colleague "
        "review, round 1) -- 'error in r' alone lets ANY guard that fires "
        "first stand in for the one this test names, which is exactly how "
        "the temporal_causal confound this test already fixed once slipped "
        "through undetected"
    )

    edge = _get_edge(a, b)
    assert edge is not None
    assert edge["relation"] == "about"


# ── 4. A node with a pending marker is refused ──────────────────────────────


def test_pending_marker_on_source_refused():
    server = fresh_server()
    premise_id = _seed_observation(server, "premise", suffix="e")
    source_id = _seed_observation(server, "source", suffix="f")
    _seed_raw_edge("supported_by", source_id, premise_id)
    _seed_pending_marker(source_id)

    r = _retype(server, source_id, premise_id, "supported_by", "about", reason="x")
    assert "error" in r, r
    assert "carries a pending cascade marker" in r["error"], (
        "pin the pending-marker guard's own message"
    )

    edge = _get_edge(source_id, premise_id)
    assert edge["relation"] == "supported_by"


def test_pending_marker_on_target_refused():
    server = fresh_server()
    premise_id = _seed_observation(server, "premise", suffix="g")
    source_id = _seed_observation(server, "source", suffix="h")
    _seed_raw_edge("supported_by", source_id, premise_id)
    _seed_pending_marker(premise_id)

    r = _retype(server, source_id, premise_id, "supported_by", "about", reason="x")
    assert "error" in r, r
    assert "carries a pending cascade marker" in r["error"], (
        "pin the pending-marker guard's own message"
    )

    edge = _get_edge(source_id, premise_id)
    assert edge["relation"] == "supported_by"


# ── 5. Confidence recompute reported old -> new ─────────────────────────────


def test_derives_from_retype_recomputes_confidence_reports_old_to_new():
    server = fresh_server()
    # Two hard_data premises -> deductive_modus_ponens -> weakest-link x 0.75-ish
    # discount. Removing one premise (retyping its derives_from edge away)
    # changes the surviving premise set, so the recomputed confidence must
    # differ from the stored value -- this is what makes the test
    # discriminate a no-recompute implementation (Ariadne's control for 5).
    p1 = _seed_observation(server, "Premise one.", quote_type="hard_data", suffix="i")
    p2 = _seed_observation(server, "Premise two.", quote_type="editorial", suffix="j")
    deriv_id = _seed_derivation(server, [p1, p2], claim="Joint claim from two premises.")

    old_confidence = _get_confidence(deriv_id)
    assert old_confidence is not None

    r = _retype(server, deriv_id, p2, "derives_from", "about",
                reason="p2 was cited as background, not a real premise.")
    assert "error" not in r, r
    assert r["status"] == "retyped", r
    assert "confidence_recompute" in r, r
    rc = r["confidence_recompute"]
    assert rc["old"] == old_confidence, rc
    assert rc["new"] is not None, rc
    assert rc["new"] != old_confidence, (
        "removing a real premise must change the weakest-link/discount "
        "computation -- if this equals old, the recompute is decorative"
    )

    new_confidence = _get_confidence(deriv_id)
    assert new_confidence == rc["new"], "the reported new value must be what's actually stored"

    edge = _get_edge(deriv_id, p2)
    assert edge["relation"] == "about"


def test_supported_by_retype_is_confidence_recompute_no_op():
    """supported_by is NOT in CASCADE_RELATIONS post-#2252-Stage-1.3, so it
    cannot feed _recompute_node_confidence's premise_ids query (ob_1689) --
    retyping it away must report old == new, never crash, and never claim a
    recompute happened where none could."""
    server = fresh_server()
    a = _seed_observation(server, "A", suffix="k")
    b = _seed_observation(server, "B", suffix="l")
    _seed_raw_edge("supported_by", a, b)
    old_confidence = _get_confidence(a)

    r = _retype(server, a, b, "supported_by", "about", reason="x")
    assert "error" not in r, r
    rc = r.get("confidence_recompute")
    if rc is not None:
        assert rc["old"] == old_confidence
        assert rc["new"] == old_confidence


# ── 6. Edit-history row + missing-reason refusal ────────────────────────────


def test_edit_history_row_has_both_relations_and_reason():
    server = fresh_server()
    import engram_core as core

    a = _seed_observation(server, "A", suffix="m")
    b = _seed_observation(server, "B", suffix="n")
    _seed_raw_edge("supported_by", a, b)

    reason = "Confirmed b was only related, not a premise."
    r = _retype(server, a, b, "supported_by", "about", reason=reason)
    assert "error" not in r, r

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT * FROM edit_history WHERE node_id = ? AND action = 'edge_retyped' "
            "ORDER BY id DESC LIMIT 1",
            (a,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "engram_retype_edge must write an edit_history row"
    details = json.loads(row["details"])
    assert details.get("old_relation") == "supported_by", details
    assert details.get("new_relation") == "about", details
    assert details.get("reason") == reason, details


def test_missing_reason_refused():
    server = fresh_server()
    a = _seed_observation(server, "A", suffix="o")
    b = _seed_observation(server, "B", suffix="p")
    _seed_raw_edge("supported_by", a, b)

    r = _retype(server, a, b, "supported_by", "about", reason=None)
    assert "error" in r, r
    assert "reason is required and cannot be empty" in r["error"], (
        "pin the reason guard's own message"
    )

    edge = _get_edge(a, b)
    assert edge["relation"] == "supported_by", "refusal must not touch the edge"


def test_empty_string_reason_refused():
    server = fresh_server()
    a = _seed_observation(server, "A", suffix="q")
    b = _seed_observation(server, "B", suffix="r")
    _seed_raw_edge("supported_by", a, b)

    r = _retype(server, a, b, "supported_by", "about", reason="   ")
    assert "error" in r, r
    assert "reason is required and cannot be empty" in r["error"], (
        "pin the reason guard's own message"
    )


# ── 7. Atomicity: a forced step-5 failure leaves the edge unchanged ─────────


def test_atomicity_forced_confidence_recompute_failure_leaves_edge_unchanged(monkeypatch):
    server = fresh_server()
    import engram_core as core

    p1 = _seed_observation(server, "Premise one.", quote_type="hard_data", suffix="s")
    p2 = _seed_observation(server, "Premise two.", quote_type="editorial", suffix="t")
    deriv_id = _seed_derivation(server, [p1, p2], claim="Joint claim.")
    old_confidence = _get_confidence(deriv_id)

    def _boom(conn, node_id, *, write=True):
        raise RuntimeError("forced failure for atomicity test")

    monkeypatch.setattr(core, "_recompute_node_confidence", _boom)

    r = _retype(server, deriv_id, p2, "derives_from", "about", reason="x")
    assert "error" in r, r
    assert "forced failure for atomicity test" in r["error"], (
        "pin the specific forced exception, not just any refusal -- confirms "
        "the atomicity path was actually exercised by THIS mutation"
    )

    edge = _get_edge(deriv_id, p2)
    assert edge is not None
    assert edge["relation"] == "derives_from", (
        "a failure during confidence recompute must leave the edge exactly "
        "as it was -- half-retyped is the atomicity violation this test "
        "exists to catch"
    )
    assert _get_confidence(deriv_id) == old_confidence, (
        "confidence must not be partially updated either"
    )


# ── 8. Downstream cascade: retyping D1's premise also recomputes D2 ─────────


def test_derives_from_retype_recomputes_downstream_cascade_too():
    server = fresh_server()
    p1 = _seed_observation(server, "Premise one.", quote_type="hard_data", suffix="u")
    p2 = _seed_observation(server, "Premise two.", quote_type="editorial", suffix="v")
    d1 = _seed_derivation(server, [p1, p2], claim="D1: joint claim from two premises.")
    d2 = _seed_derivation(server, [d1, p1], claim="D2: derives from D1 and p1.")

    d1_old = _get_confidence(d1)
    d2_old = _get_confidence(d2)
    assert d1_old is not None and d2_old is not None

    r = _retype(server, d1, p2, "derives_from", "about",
                reason="p2 was cited as background, not a real premise.")
    assert "error" not in r, r

    d1_new_stored = _get_confidence(d1)
    d2_new_stored = _get_confidence(d2)

    # D1 itself must have been recomputed (same property test 5 already
    # covers for a single node).
    assert d1_new_stored != d1_old, "D1's own confidence must change"

    # D2 derives_from D1 -- D1's new confidence changes D2's own formula
    # input, so D2's STORED confidence must reflect a fresh recompute, not
    # the value computed back when D1 still held its old confidence.
    import engram_core as core
    conn = core._get_db()
    try:
        d2_fresh = core._recompute_node_confidence(conn, d2, write=False)["new"]
    finally:
        conn.close()
    assert d2_fresh != d2_old, (
        "fixture sanity check: D1's confidence change must actually move "
        "D2's fresh-recompute value, or this test cannot discriminate "
        "anything"
    )
    assert d2_new_stored == d2_fresh, (
        f"D2's stored confidence ({d2_new_stored}) must equal a fresh "
        f"recompute ({d2_fresh}) -- a single-node recompute that stops at "
        f"D1 leaves D2 stale at {d2_old}"
    )


# ── 9. A non-current source's OWN confidence is never rewritten ─────────────


def test_retype_on_noncurrent_source_leaves_its_own_confidence_untouched():
    server = fresh_server()
    import engram_core as core

    p1 = _seed_observation(server, "Premise one.", quote_type="hard_data", suffix="w")
    p2 = _seed_observation(server, "Premise two.", quote_type="editorial", suffix="x")
    deriv_id = _seed_derivation(server, [p1, p2], claim="A superseded derivation.")
    old_confidence = _get_confidence(deriv_id)
    assert old_confidence is not None

    conn = core._get_db()
    try:
        conn.execute("UPDATE nodes SET is_current = 0 WHERE id = ?", (deriv_id,))
        conn.commit()
    finally:
        conn.close()

    r = _retype(server, deriv_id, p2, "derives_from", "about",
                reason="Correcting the historical record on a dead node.")
    assert "error" not in r, r
    assert r["status"] == "retyped", r

    assert _get_confidence(deriv_id) == old_confidence, (
        "a non-current node's own confidence is a historical record -- "
        "_cascade_recompute_confidence_transitive deliberately skips "
        "rewriting it (per its own docstring); a naive single-node "
        "recompute does not check is_current at all and would overwrite it"
    )


# ── S1 (Borges's non-blocking suggestion): a clean refusal, not IntegrityError ──


def test_existing_target_relation_refused_cleanly_not_integrity_error():
    server = fresh_server()
    a = _seed_observation(server, "A", suffix="y")
    b = _seed_observation(server, "B", suffix="z")
    _seed_raw_edge("supported_by", a, b)
    _seed_raw_edge("about", a, b)  # the retype target already exists

    r = _retype(server, a, b, "supported_by", "about", reason="x")
    assert "error" in r, r
    assert "already exists" in r["error"], r
    assert "IntegrityError" not in r["error"], (
        "the caller must see a plain refusal message, not a raw "
        "IntegrityError bubbled up through the rollback path"
    )

    # Two edges share these endpoints (supported_by and about) -- _get_edge
    # is ambiguous here (no relation filter), so check both directly rather
    # than relying on it to disambiguate.
    import engram_core as core
    conn = core._get_db()
    try:
        rows = conn.execute(
            "SELECT relation FROM edges WHERE source_id = ? AND target_id = ? "
            "ORDER BY relation",
            (a, b),
        ).fetchall()
    finally:
        conn.close()
    assert sorted(row["relation"] for row in rows) == ["about", "supported_by"], (
        "refusal must not touch either edge"
    )


# ── 10. Retyping a derivation's LAST cascade premise is refused ─────────────


def test_last_cascade_premise_retype_refused():
    server = fresh_server()
    import engram_core as core

    p1 = _seed_observation(server, "The only premise.", quote_type="hard_data", suffix="aa")
    deriv_id = _seed_derivation(server, [p1], claim="A derivation with exactly one premise.")
    old_confidence = _get_confidence(deriv_id)
    assert old_confidence is not None

    r = _retype(server, deriv_id, p1, "derives_from", "about",
                reason="trying to remove the only premise")
    assert "error" in r, r
    assert "has no other cascade premise besides" in r["error"], (
        "pin the last-cascade-premise guard's own message"
    )

    edge = _get_edge(deriv_id, p1)
    assert edge is not None
    assert edge["relation"] == "derives_from", (
        "refusing must not touch the edge -- a derivation left with no "
        "premises would keep an uncomputed confidence"
    )
    assert _get_confidence(deriv_id) == old_confidence, (
        "confidence must be untouched by a refused retype"
    )

    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT * FROM edit_history WHERE node_id = ? AND action = 'edge_retyped' "
            "ORDER BY id DESC LIMIT 1",
            (deriv_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is None, "a refused retype must not write an edit-history row"


def test_last_cascade_premise_retype_control_two_premises_succeeds():
    """Control for test 10 (Ariadne's own wording): the same move on a
    two-premise derivation succeeds -- this is the property test 5 already
    covers, restated here as an explicit control paired with test 10."""
    server = fresh_server()
    p1 = _seed_observation(server, "Premise one.", quote_type="hard_data", suffix="bb")
    p2 = _seed_observation(server, "Premise two.", quote_type="editorial", suffix="cc")
    deriv_id = _seed_derivation(server, [p1, p2], claim="A derivation with two premises.")

    r = _retype(server, deriv_id, p2, "derives_from", "about", reason="x")
    assert "error" not in r, r
    assert r["status"] == "retyped", r

    edge = _get_edge(deriv_id, p2)
    assert edge["relation"] == "about"
