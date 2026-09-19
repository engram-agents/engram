"""Tests for the Recall-Set Continuity metric (v1, #1630).

Recall-Set Continuity = Jaccard similarity J(A,B) = |A∩B| / |A∪B| between the
tier1+tier2 ("searchable") node-ID set at consecutive checkpoints (nap AND
sleep/engram_advance_turn — see the cadence-decision note in
_compute_recall_set_continuity's docstring). Computed in the engram_checkpoint
(formerly engram_nap) and engram_advance_turn wrappers (server.py) and
persisted into the
diagnostic_history table's metrics JSON blob, under the
"recall_set_continuity" key. See _compute_recall_set_continuity in
engram_core.py for the full design.

Coverage (spec §3 + reviewer-fairy follow-up + Borges's cadence decision):
  1. Two consecutive naps with a known set of nodes added/superseded between
     them — assert J is computed correctly with hand-verifiable numbers
     (10 tier2 nodes at nap1, supersede 2 before nap2 → J = 8/12 = 0.6667).
  2. First-ever nap (no prior diagnostic_history row) — assert J is None
     with a clear reason, never a fabricated 0.0/1.0.
  3. Regression guard: an ad-hoc engram_diagnose() call (not via nap) does
     NOT carry the tier2_ids node-ID list in its return value — only the
     nap wrapper's stored diagnostic_history row does. This is the
     regression guard for the §2 step-4 design decision (computed in the
     nap wrapper, not inside _diagnose_impl, so ad-hoc engram_diagnose()
     calls stay compact).
  4. Sleep-checkpoint cadence: engram_advance_turn also computes the metric
     (Borges's decision — the sleep boundary is the most meaningful
     measurement point), tagged checkpoint_mode="session"; a nap immediately
     following a sleep diffs against the sleep row (checkpoint_mode="nap",
     prior_checkpoint_mode="session") rather than showing a spurious null.
  5. Zero-division convention: both current and prior tier2 sets empty →
     jaccard=1.0 (never None — None is reserved for "no prior row exists",
     a distinct condition).
  6. Corrupt prior metrics blob (unparseable JSON) → jaccard=None with a
     clear reason, never a crash or a fabricated value.
  7. Storage pruning: tier2_ids is stripped from the prior row's stored
     metrics blob after a later checkpoint has diffed against it (Borges's
     colleague-review storage concern) — only the newest row keeps its
     full ID list at rest.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_recall_set_continuity_")
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


def _insert_node(conn, node_id: str, *, is_current: int = 1) -> None:
    """Insert a minimal `nodes` row directly, bypassing the normal
    engram_add_observation flow so the test controls the exact tier2
    node-ID set (default tier2_max_nodes=1000 means the dynamic
    _get_tier_threshold returns 0.0 for these small fixtures — i.e. every
    is_current=1 row qualifies for tier2, regardless of importance_score).
    """
    conn.execute(
        "INSERT INTO nodes (id, type, claim, created_at, is_current) "
        "VALUES (?, 'observation_factual', ?, ?, ?)",
        (node_id, f"test claim for {node_id}", "2026-07-02T00:00:00", is_current),
    )


# ---------------------------------------------------------------------------
# 1. Two-nap Jaccard computation — hand-verifiable numbers
# ---------------------------------------------------------------------------

def test_jaccard_computed_correctly_across_two_naps():
    """Seed 10 tier2 nodes, nap, supersede 2 of them (add 2 replacements),
    nap again. A = {rsc_001..rsc_010} (10 ids). B = {rsc_003..rsc_010,
    rsc_011, rsc_012} (10 ids). |A∩B| = 8 (rsc_003..rsc_010).
    |A∪B| = 10 + 10 - 8 = 12. J = 8/12 = 0.666667 (rounded to 6 places).
    """
    server = fresh_server()
    conn = server._get_db()
    try:
        for i in range(1, 11):
            _insert_node(conn, f"rsc_{i:03d}")
        conn.commit()
    finally:
        conn.close()

    r1 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 1 — seed 10 nodes"})))
    assert "error" not in r1, f"nap1 returned error: {r1}"
    snap1 = r1.get("diagnostic_snapshot", {})
    assert snap1.get("stored") is True, f"nap1 diagnostic snapshot not stored: {snap1}"
    rsc1 = snap1.get("recall_set_continuity")
    assert rsc1 is not None, f"nap1 missing recall_set_continuity: {snap1}"
    assert rsc1["set_size"] == 10, f"nap1 set_size expected 10, got {rsc1}"
    assert rsc1["jaccard"] is None, (
        f"nap1 (first-ever nap) jaccard must be None, got {rsc1}"
    )

    # Supersede rsc_001, rsc_002; add two replacements rsc_011, rsc_012.
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET is_current = 0 WHERE id IN ('rsc_001', 'rsc_002')"
        )
        _insert_node(conn, "rsc_011")
        _insert_node(conn, "rsc_012")
        conn.commit()
    finally:
        conn.close()

    r2 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 2 — supersede 2, add 2"})))
    assert "error" not in r2, f"nap2 returned error: {r2}"
    snap2 = r2.get("diagnostic_snapshot", {})
    assert snap2.get("stored") is True, f"nap2 diagnostic snapshot not stored: {snap2}"
    rsc2 = snap2.get("recall_set_continuity")
    assert rsc2 is not None, f"nap2 missing recall_set_continuity: {snap2}"
    assert rsc2["set_size"] == 10, f"nap2 set_size expected 10, got {rsc2}"
    assert rsc2["jaccard"] == round(8 / 12, 6), (
        f"nap2 jaccard expected {round(8 / 12, 6)} (8/12), got {rsc2}"
    )
    assert rsc2["prior_snapshot_turn"] == r1.get("current_turn"), (
        f"nap2 prior_snapshot_turn should be nap1's turn (nap doesn't advance "
        f"turn): expected {r1.get('current_turn')}, got {rsc2}"
    )
    # Compact nap-output view never carries the full tier2_ids list.
    assert "tier2_ids" not in rsc2, (
        f"nap-output recall_set_continuity must not carry the full ID list: {rsc2}"
    )


# ---------------------------------------------------------------------------
# 2. First-ever nap — J is None/absent with a clear reason, never fabricated
# ---------------------------------------------------------------------------

def test_first_nap_jaccard_is_null_with_reason():
    """No prior diagnostic_history row exists yet (empty graph, first nap
    ever) — jaccard must be None with a documented reason, never a
    fabricated 0.0 or 1.0.
    """
    server = fresh_server()
    r = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "very first nap"})))
    assert "error" not in r, f"nap returned error: {r}"
    snap = r.get("diagnostic_snapshot", {})
    assert snap.get("stored") is True, f"diagnostic snapshot not stored: {snap}"
    rsc = snap.get("recall_set_continuity")
    assert rsc is not None, f"missing recall_set_continuity: {snap}"
    assert rsc["jaccard"] is None, (
        f"first-ever nap jaccard must be None (not fabricated 0.0/1.0), got {rsc}"
    )
    assert rsc.get("reason"), f"first-ever nap must carry a reason for null jaccard: {rsc}"
    assert "first checkpoint" in rsc["reason"].lower(), (
        f"reason should explain no prior checkpoint exists: {rsc['reason']!r}"
    )
    assert rsc["prior_snapshot_turn"] is None, (
        f"first-ever nap has no prior snapshot turn: {rsc}"
    )


# ---------------------------------------------------------------------------
# 3. Regression guard: ad-hoc engram_diagnose() never carries tier2_ids
# ---------------------------------------------------------------------------

def test_adhoc_diagnose_does_not_carry_full_id_list():
    """engram_diagnose() is its own directly-callable MCP tool (per its
    docstring: "Use any time" — not nap-only). The Recall-Set Continuity
    tier2_ids list must be injected ONLY by the engram_checkpoint (formerly
    engram_nap) wrapper into the stored diagnostic_history row, never into
    _diagnose_impl's own return
    value — otherwise every ad-hoc engram_diagnose() call would carry a
    tier2_max_nodes-sized (default 1000) node-ID list.
    """
    server = fresh_server()
    conn = server._get_db()
    try:
        for i in range(1, 6):
            _insert_node(conn, f"rsc_{i:03d}")
        conn.commit()
    finally:
        conn.close()

    # Ad-hoc call BEFORE any nap has ever happened.
    diag_before = json.loads(server.engram_diagnose())
    assert "recall_set_continuity" not in diag_before, (
        f"ad-hoc engram_diagnose() must not carry recall_set_continuity: "
        f"{list(diag_before.keys())}"
    )

    # Nap once, so diagnostic_history now has a row WITH recall_set_continuity.
    nap_result = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap"})))
    assert "error" not in nap_result, f"nap returned error: {nap_result}"
    assert nap_result["diagnostic_snapshot"]["recall_set_continuity"]["set_size"] == 5

    # Ad-hoc call AFTER a nap has happened — still must not carry tier2_ids,
    # nor a recall_set_continuity key at all (that's the nap wrapper's own
    # post-processing of _diagnose_impl's output, not part of the tool's
    # own computation).
    diag_after = json.loads(server.engram_diagnose())
    assert "recall_set_continuity" not in diag_after, (
        f"ad-hoc engram_diagnose() must not carry recall_set_continuity even "
        f"after a nap has stored one: {list(diag_after.keys())}"
    )
    serialized = json.dumps(diag_after)
    assert "rsc_001" not in serialized, (
        "ad-hoc engram_diagnose() output must not contain tier2 node IDs anywhere"
    )


# ---------------------------------------------------------------------------
# 4. Sleep-checkpoint cadence — engram_advance_turn also computes the metric
# ---------------------------------------------------------------------------

def test_sleep_checkpoint_computes_and_tags_continuity():
    """nap -> sleep (engram_advance_turn) -> nap. Every checkpoint computes
    the metric (Borges's cadence decision, 2026-07-02: the sleep boundary is
    the most meaningful measurement point, not excluded). Each row is
    tagged with its own checkpoint_mode, and the row diffed against is
    identifiable via prior_checkpoint_mode — so a downstream reader can
    tell "this J spans a sleep boundary" from "this J is a plain nap-to-nap
    comparison."
    """
    server = fresh_server()
    conn = server._get_db()
    try:
        for i in range(1, 6):
            _insert_node(conn, f"rsc_{i:03d}")
        conn.commit()
    finally:
        conn.close()

    r1 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 1"})))
    assert "error" not in r1, f"nap1 returned error: {r1}"
    rsc1 = r1["diagnostic_snapshot"]["recall_set_continuity"]
    assert rsc1["checkpoint_mode"] == "nap", f"nap checkpoint_mode should be 'nap': {rsc1}"

    r2 = json.loads(server.engram_advance_turn(payload_json=json.dumps({"message": "sleep 1"})))
    assert "error" not in r2, f"sleep returned error: {r2}"
    snap2 = r2.get("diagnostic_snapshot", {})
    assert snap2.get("stored") is True, f"sleep diagnostic snapshot not stored: {snap2}"
    rsc2 = snap2.get("recall_set_continuity")
    assert rsc2 is not None, (
        f"engram_advance_turn must compute recall_set_continuity too "
        f"(Borges's cadence decision — not nap-only): {snap2}"
    )
    assert rsc2["checkpoint_mode"] == "session", (
        f"sleep checkpoint_mode should be 'session' (the literal mode string "
        f"_checkpoint_internal uses for advance_turn=True): {rsc2}"
    )
    assert rsc2["prior_checkpoint_mode"] == "nap", (
        f"sleep's prior row was the nap — prior_checkpoint_mode should say so: {rsc2}"
    )
    # No node changes between nap1 and sleep1 -> identical tier2 set -> J=1.0.
    assert rsc2["jaccard"] == 1.0, f"unchanged set across nap->sleep should be J=1.0: {rsc2}"

    r3 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 2, post-sleep"})))
    assert "error" not in r3, f"nap2 returned error: {r3}"
    rsc3 = r3["diagnostic_snapshot"]["recall_set_continuity"]
    assert rsc3["checkpoint_mode"] == "nap", f"post-sleep nap checkpoint_mode should be 'nap': {rsc3}"
    assert rsc3["prior_checkpoint_mode"] == "session", (
        f"nap2's prior row was the sleep checkpoint — prior_checkpoint_mode "
        f"should say 'session', proving the spurious-null-after-sleep gap is "
        f"closed: {rsc3}"
    )
    assert rsc3["jaccard"] is not None, (
        f"nap2 must diff against the sleep row, not show a spurious null "
        f"(the whole point of the cadence fix): {rsc3}"
    )


# ---------------------------------------------------------------------------
# 5. Zero-division convention — both sets empty -> jaccard=1.0, not None
# ---------------------------------------------------------------------------

def test_zero_division_both_sets_empty_is_jaccard_one():
    """If both the current and prior tier2 sets are empty (|A ∪ B| = 0),
    jaccard is defined as 1.0 by convention — a distinct case from "no prior
    row exists," which is None. An empty graph naps twice in a row with no
    nodes added between them.
    """
    server = fresh_server()
    r1 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 1, empty graph"})))
    assert "error" not in r1, f"nap1 returned error: {r1}"
    rsc1 = r1["diagnostic_snapshot"]["recall_set_continuity"]
    assert rsc1["set_size"] == 0, f"empty graph should have set_size=0: {rsc1}"
    assert rsc1["jaccard"] is None, f"first nap has no prior row, jaccard must be None: {rsc1}"

    r2 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 2, still empty"})))
    assert "error" not in r2, f"nap2 returned error: {r2}"
    rsc2 = r2["diagnostic_snapshot"]["recall_set_continuity"]
    assert rsc2["set_size"] == 0, f"still-empty graph should have set_size=0: {rsc2}"
    assert rsc2["jaccard"] == 1.0, (
        f"both current and prior sets empty -> jaccard=1.0 by convention, "
        f"not None (a prior row DOES exist here, unlike test 2): {rsc2}"
    )


# ---------------------------------------------------------------------------
# 6. Corrupt prior metrics blob — jaccard=None with a reason, never a crash
# ---------------------------------------------------------------------------

def test_corrupt_prior_metrics_json_is_handled_gracefully():
    """If the most-recent diagnostic_history row's metrics column is not
    parseable JSON (corruption, partial write, etc.), the next checkpoint
    must not crash — jaccard=None with a clear reason, same discipline as
    the other undefined cases.
    """
    server = fresh_server()
    r1 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 1"})))
    assert "error" not in r1, f"nap1 returned error: {r1}"

    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE diagnostic_history SET metrics = ? "
            "WHERE id = (SELECT MAX(id) FROM diagnostic_history)",
            ("{not valid json!!!",),
        )
        conn.commit()
    finally:
        conn.close()

    r2 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 2, corrupt prior row"})))
    assert "error" not in r2, f"nap2 returned error despite corrupt prior row: {r2}"
    rsc2 = r2["diagnostic_snapshot"]["recall_set_continuity"]
    assert rsc2["jaccard"] is None, (
        f"corrupt prior metrics blob must yield jaccard=None, not crash or "
        f"fabricate a value: {rsc2}"
    )
    assert rsc2.get("reason"), f"corrupt-prior-row case must carry a reason: {rsc2}"
    assert "not parseable" in rsc2["reason"].lower() or "json" in rsc2["reason"].lower(), (
        f"reason should mention the JSON-parse failure: {rsc2['reason']!r}"
    )


# ---------------------------------------------------------------------------
# 7. Storage pruning — tier2_ids stripped from the prior row after diffing
# ---------------------------------------------------------------------------

def test_prior_row_tier2_ids_pruned_after_diffing():
    """Borges's colleague-review storage concern (#1632): tier2_ids is only
    ever read from the immediately-prior row, never re-read from older rows
    — so once a checkpoint has diffed against the prior row, that row's
    tier2_ids should be pruned in place (stripped from its stored metrics
    blob) rather than accumulating unboundedly across every checkpoint
    forever. jaccard/checkpoint_mode/etc. on the pruned row must survive
    untouched — only tier2_ids is dropped.
    """
    server = fresh_server()
    conn = server._get_db()
    try:
        for i in range(1, 4):
            _insert_node(conn, f"rsc_{i:03d}")
        conn.commit()
    finally:
        conn.close()

    r1 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 1"})))
    assert "error" not in r1, f"nap1 returned error: {r1}"
    turn1_id = None
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT id, metrics FROM diagnostic_history ORDER BY id ASC LIMIT 1"
        ).fetchone()
        turn1_id = row["id"]
        metrics1 = json.loads(row["metrics"])
        assert "tier2_ids" in metrics1["recall_set_continuity"], (
            "nap1's own row must still carry tier2_ids right after being "
            f"written (nothing has diffed against it yet): {metrics1}"
        )
    finally:
        conn.close()

    r2 = json.loads(server.engram_checkpoint(payload_json=json.dumps({"message": "nap 2"})))
    assert "error" not in r2, f"nap2 returned error: {r2}"
    rsc2 = r2["diagnostic_snapshot"]["recall_set_continuity"]
    assert rsc2["jaccard"] is not None, f"nap2 should have a valid diff against nap1: {rsc2}"

    # Now that nap2 has diffed against nap1's row, nap1's tier2_ids should
    # be pruned — but its jaccard/checkpoint_mode must survive untouched.
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT metrics FROM diagnostic_history WHERE id = ?", (turn1_id,)
        ).fetchone()
        metrics1_after = json.loads(row["metrics"])
        rsc1_after = metrics1_after["recall_set_continuity"]
        assert "tier2_ids" not in rsc1_after, (
            f"nap1's tier2_ids should be pruned after nap2 diffed against it: {rsc1_after}"
        )
        assert rsc1_after["checkpoint_mode"] == "nap", (
            f"nap1's checkpoint_mode must survive pruning: {rsc1_after}"
        )
        assert rsc1_after["jaccard"] is None, (
            f"nap1's own jaccard (first-ever nap, no prior) must survive pruning unchanged: {rsc1_after}"
        )
    finally:
        conn.close()

    # nap2's own row is untouched (nothing has diffed against IT yet) —
    # still carries its own tier2_ids, ready for nap3 to diff against.
    conn = server._get_db()
    try:
        row = conn.execute(
            "SELECT metrics FROM diagnostic_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        metrics2 = json.loads(row["metrics"])
        assert "tier2_ids" in metrics2["recall_set_continuity"], (
            f"nap2's own row must still carry tier2_ids (not yet diffed against): {metrics2}"
        )
    finally:
        conn.close()
