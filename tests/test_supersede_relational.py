"""Sandbox tests for the relational-only engram_supersede (2026-04-24 refactor).

Verifies: happy paths, validation guards, stale cascade, chain supersede.
Run with: python3 tests/test_supersede_relational.py  OR  pytest tests/test_supersede_relational.py
"""

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server


def _mk_observation(claim: str, quote_type: str = "attributed_analysis") -> str:
    """Create an observation via the canonical tool and return its ID."""
    result = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/test",
        "title": "Test source",
        "domain": "example.com",
        "source_date": "2026-04-24",
        "claim": claim,
        "quoted_text": claim,
        "interpretation": f"Test observation: {claim}",
        "quote_type": quote_type,
    })))
    assert "observation_id" in result, f"obs creation failed: {result}"
    return result["observation_id"]


def _mk_derivation(claim: str, support_ids: list[str], reasoning_type: str = "deductive_hypothetical_syllogism") -> str:
    result = json.loads(server.engram_derive(
        claim=claim,
        supporting_ids=",".join(support_ids),
        logical_chain=f"Derivation: {claim}",
        reasoning_type=reasoning_type,
    ))
    assert "derivation_id" in result, f"derivation failed: {result}"
    return result["derivation_id"]


def _edges(conn, source=None, target=None, relation=None):
    q = "SELECT source_id, target_id, relation, metadata FROM edges WHERE 1=1"
    params = []
    if source:
        q += " AND source_id = ?"
        params.append(source)
    if target:
        q += " AND target_id = ?"
        params.append(target)
    if relation:
        q += " AND relation = ?"
        params.append(relation)
    return conn.execute(q, params).fetchall()


def test_supersede_relational():
    passed = 0
    failed = 0

    with server.engram_sandbox():

        # ---- Test 1: happy path — supersede a derivation ----
        ob1 = _mk_observation("Premise A")
        ob2 = _mk_observation("Premise B")
        dv1 = _mk_derivation("Initial conclusion", [ob1, ob2], "inductive_generalization")
        # Create replacement derivation with corrected reasoning_type
        dv2 = _mk_derivation("Initial conclusion", [ob1, ob2], "authority_expert")
        r = json.loads(server.engram_supersede(dv1, dv2, supersede_reason="reasoning_type correction: inductive_generalization → authority_expert"))
        assert r["status"] == "superseded", f"T1: {r}"
        assert r["old_node_id"] == dv1
        assert r["new_node_id"] == dv2

        conn = server._get_db()
        try:
            # old marked superseded
            old_row = conn.execute("SELECT is_current, superseded_by FROM nodes WHERE id = ?", (dv1,)).fetchone()
            assert old_row["is_current"] == 0, f"T1: old.is_current should be 0"
            assert old_row["superseded_by"] == dv2, f"T1: old.superseded_by should be {dv2}"
            # new points back
            new_row = conn.execute("SELECT supersedes FROM nodes WHERE id = ?", (dv2,)).fetchone()
            assert new_row["supersedes"] == dv1, f"T1: new.supersedes should be {dv1}"
            # supersedes edge exists with metadata reason
            edges = _edges(conn, source=dv2, target=dv1, relation="supersedes")
            assert len(edges) == 1, f"T1: expected 1 supersedes edge, got {len(edges)}"
            meta = json.loads(edges[0]["metadata"]) if edges[0]["metadata"] else {}
            assert "reasoning_type correction" in meta.get("reason", ""), f"T1: reason metadata missing: {meta}"
        finally:
            conn.close()
        print("  T1  PASS — derivation supersede wires correctly + reason lands in edge metadata")
        passed += 1

        # ---- Test 2: supersede with no reason — edge metadata is NULL ----
        ob3 = _mk_observation("Premise C")
        ob4 = _mk_observation("Premise D")
        dv3 = _mk_derivation("Claim X", [ob3], "inductive_generalization")
        dv4 = _mk_derivation("Claim X refined", [ob3, ob4], "inductive_generalization")
        r = json.loads(server.engram_supersede(dv3, dv4))
        assert r["status"] == "superseded", f"T2: {r}"
        assert r["supersede_reason"] is None
        conn = server._get_db()
        try:
            edges = _edges(conn, source=dv4, target=dv3, relation="supersedes")
            assert edges[0]["metadata"] is None, f"T2: metadata should be NULL, got {edges[0]['metadata']}"
        finally:
            conn.close()
        print("  T2  PASS — no reason → edge metadata NULL")
        passed += 1

        # ---- Test 3: observation supersede (quote_type correction is a free feature) ----
        # Old obs with wrong quote_type
        old_ob = _mk_observation("Observed pattern", quote_type="editorial")
        # New obs with corrected quote_type
        new_ob = _mk_observation("Observed pattern", quote_type="hard_data")
        r = json.loads(server.engram_supersede(old_ob, new_ob, supersede_reason="quote_type correction: editorial → hard_data"))
        assert r["status"] == "superseded", f"T3: {r}"
        conn = server._get_db()
        try:
            # Verify different confidences were preserved
            old_conf = conn.execute("SELECT confidence FROM nodes WHERE id = ?", (old_ob,)).fetchone()["confidence"]
            new_conf = conn.execute("SELECT confidence FROM nodes WHERE id = ?", (new_ob,)).fetchone()["confidence"]
            assert new_conf > old_conf, f"T3: hard_data ({new_conf}) should > editorial ({old_conf})"
        finally:
            conn.close()
        print("  T3  PASS — observation supersede preserves independent confidences (quote_type correction is free)")
        passed += 1

        # ---- Test 4: type mismatch blocked ----
        ob5 = _mk_observation("Premise E")
        dv5 = _mk_derivation("Claim Y", [ob5])
        r = json.loads(server.engram_supersede(ob5, dv5))
        assert "error" in r and "Type mismatch" in r["error"], f"T4: expected type mismatch error, got {r}"
        print("  T4  PASS — cross-type supersede (obs → derivation) blocked")
        passed += 1

        # ---- Test 5: feeling_report blocked ----
        # engram_report_feeling migrated to single-payload signature (wave 2a).
        fl_result = json.loads(server.engram_report_feeling(payload_json=json.dumps({
            "reported_state": "test feeling for supersede block",
            "trigger": "sandbox test",
        })))
        fl_id = fl_result.get("feeling_id") or fl_result.get("node_id")
        assert fl_id, f"T5: feeling creation failed: {fl_result}"
        # Can't create another feeling with same-type for cross-feel test; use any node. Type mismatch hits first.
        # To isolate the feeling_report block, craft a same-type attempt — make second feeling.
        fl_result2 = json.loads(server.engram_report_feeling(payload_json=json.dumps({
            "reported_state": "second feeling",
            "trigger": "sandbox test",
        })))
        fl_id2 = fl_result2.get("feeling_id") or fl_result2.get("node_id")
        r = json.loads(server.engram_supersede(fl_id, fl_id2))
        assert "error" in r and "feeling reports cannot be superseded" in r["error"], f"T5: {r}"
        print("  T5  PASS — feeling_report supersede blocked with clear message")
        passed += 1

        # ---- Test 6: new already supersedes — blocked ----
        # dv4 already supersedes dv3 (from T2). Try to make dv4 supersede something else.
        ob6 = _mk_observation("Premise F")
        dv6 = _mk_derivation("Another claim", [ob6])
        r = json.loads(server.engram_supersede(dv6, dv4))
        assert "error" in r and "already supersedes" in r["error"], f"T6: {r}"
        print("  T6  PASS — new node already superseding something → blocked")
        passed += 1

        # ---- Test 7: old already superseded (is_current=0) — blocked ----
        # dv3 is already superseded (from T2). Try to supersede it again.
        dv7 = _mk_derivation("Yet another refinement", [ob3])
        r = json.loads(server.engram_supersede(dv3, dv7))
        assert "error" in r and "not current" in r["error"], f"T7: {r}"
        print("  T7  PASS — old already superseded → blocked")
        passed += 1

        # ---- Test 8: nonexistent IDs blocked ----
        r = json.loads(server.engram_supersede("dv_9999", dv7))
        assert "error" in r and "not found" in r["error"], f"T8a: {r}"
        r = json.loads(server.engram_supersede(dv7, "dv_9999"))
        assert "error" in r and "not found" in r["error"], f"T8b: {r}"
        print("  T8  PASS — missing IDs blocked (both old and new)")
        passed += 1

        # ---- Test 9: old == new blocked ----
        r = json.loads(server.engram_supersede(dv7, dv7))
        assert "error" in r and ("must differ" in r["error"] or "already supersedes" in r["error"]), f"T9: {r}"
        print("  T9  PASS — same-ID supersede blocked")
        passed += 1

        # ---- Test 10: DAG invariant — new older than old → blocked ----
        conn = server._get_db()
        try:
            # Create two derivations, then manually roll new's created_at to BEFORE old's.
            ob7 = _mk_observation("Premise G")
            dv_older = _mk_derivation("Will play role of 'new'", [ob7])
            time.sleep(0.01)
            dv_newer = _mk_derivation("Will play role of 'old'", [ob7])
            # Swap timestamps — dv_older now looks like it was created AFTER dv_newer
            # We want to supersede dv_newer with dv_older but dv_older is older.
            conn.execute(
                "UPDATE nodes SET created_at = '2026-04-01T00:00:00+00:00' WHERE id = ?",
                (dv_older,),
            )
            conn.execute(
                "UPDATE nodes SET created_at = '2026-04-15T00:00:00+00:00' WHERE id = ?",
                (dv_newer,),
            )
            conn.commit()
        finally:
            conn.close()
        r = json.loads(server.engram_supersede(dv_newer, dv_older))
        assert "error" in r and "DAG invariant" in r["error"], f"T10: {r}"
        print("  T10 PASS — DAG invariant violation (new older than old) blocked")
        passed += 1

        # ---- Test 11: stale cascade walks downstream ----
        # Build: ob_X → dv_upstream → dv_downstream. Supersede dv_upstream. dv_downstream should go stale.
        ob_x = _mk_observation("Premise X for cascade")
        dv_upstream = _mk_derivation("Upstream claim", [ob_x])
        dv_downstream = _mk_derivation("Downstream depends on upstream", [dv_upstream])
        dv_upstream_v2 = _mk_derivation("Upstream claim refined", [ob_x])
        r = json.loads(server.engram_supersede(dv_upstream, dv_upstream_v2))
        assert r["stale_count"] >= 1, f"T11: expected cascade, got stale_count={r['stale_count']}"
        stale_ids = {s["id"] for s in r["stale_downstream"]}
        assert dv_downstream in stale_ids, f"T11: {dv_downstream} should be in stale set {stale_ids}"
        conn = server._get_db()
        try:
            ds_row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (dv_downstream,)).fetchone()
            meta = json.loads(ds_row["metadata"])
            assert dv_upstream in meta.get("stale_by", []), f"T11: stale_by metadata missing"
            assert meta.get("stale_replacement") == dv_upstream_v2, f"T11: stale_replacement wrong"
        finally:
            conn.close()
        print("  T11 PASS — stale cascade walks and stamps metadata on downstream")
        passed += 1

        # ---- Test 12: chain supersede A ← B ← C ----
        ob_y = _mk_observation("Chain premise")
        a = _mk_derivation("Version A", [ob_y])
        b = _mk_derivation("Version B", [ob_y])
        c = _mk_derivation("Version C", [ob_y])
        r1 = json.loads(server.engram_supersede(a, b))
        assert r1["status"] == "superseded", f"T12.1: {r1}"
        r2 = json.loads(server.engram_supersede(b, c))
        assert r2["status"] == "superseded", f"T12.2: {r2}"
        conn = server._get_db()
        try:
            a_row = conn.execute("SELECT is_current, superseded_by FROM nodes WHERE id = ?", (a,)).fetchone()
            b_row = conn.execute("SELECT is_current, superseded_by, supersedes FROM nodes WHERE id = ?", (b,)).fetchone()
            c_row = conn.execute("SELECT is_current, supersedes FROM nodes WHERE id = ?", (c,)).fetchone()
            assert a_row["is_current"] == 0 and a_row["superseded_by"] == b
            assert b_row["is_current"] == 0 and b_row["superseded_by"] == c and b_row["supersedes"] == a
            assert c_row["is_current"] == 1 and c_row["supersedes"] == b
        finally:
            conn.close()
        print("  T12 PASS — chain A ← B ← C supersede works; each link independent")
        passed += 1

        # ---- Test 13: old node was retracted (is_current=0, status='retracted') — blocked ----
        ob_retracted = _mk_observation("To be retracted")
        json.loads(server.engram_retract(
            node_id=ob_retracted,
            error_type="hallucinated_claim",
            reason="test",
        ))
        ob_replacement = _mk_observation("Replacement")
        r = json.loads(server.engram_supersede(ob_retracted, ob_replacement))
        assert "error" in r and "not current" in r["error"], f"T13: {r}"
        print("  T13 PASS — supersede of retracted old node blocked (is_current=0)")
        passed += 1

        # ---- Test 14: edit history audit trail recorded ----
        ob_a = _mk_observation("Audit test A")
        ob_b = _mk_observation("Audit test B")
        json.loads(server.engram_supersede(ob_a, ob_b))
        conn = server._get_db()
        try:
            history = conn.execute(
                "SELECT action, node_id, details FROM edit_history WHERE node_id = ? ORDER BY id DESC LIMIT 1",
                (ob_a,),
            ).fetchone()
            assert history["action"] == "superseded", f"T14: expected 'superseded' edit, got {history['action']}"
            details = json.loads(history["details"])
            assert details["replaced_by"] == ob_b
        finally:
            conn.close()
        print("  T14 PASS — edit_history audit trail entry created")
        passed += 1

    print(f"\n{'='*56}")
    print(f"  {passed} passed, {failed} failed")
    print('='*56)
    assert failed == 0, f"{failed} sub-test(s) failed"


if __name__ == "__main__":
    test_supersede_relational()
