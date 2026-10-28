"""Sandbox tests for the relational-only engram_supersede (2026-04-24 refactor).

Verifies: happy paths, validation guards, stale cascade, chain supersede.
Run with: python3 tests/test_supersede_relational.py  OR  pytest tests/test_supersede_relational.py

Import-pattern contract (#948 wave B — R1):
    This file imports engram_revision, engram_cornerstone, engram_derive,
    engram_observation, engram_tasks, engram_trust, and engram_core directly
    and contains no ``import server``. The sandbox pattern (engram_core.engram_sandbox)
    manages db isolation per-test; no fresh_module() is needed for this file.
"""

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_core
import engram_cornerstone
import engram_derive
import engram_observation
import engram_revision
import engram_tasks
import engram_trust


def _mk_observation(claim: str, quote_type: str = "attributed_analysis") -> str:
    """Create an observation via the impl and return its ID."""
    result = json.loads(engram_observation._add_observation_impl(
        url="https://example.com/test",
        title="Test source",
        domain="example.com",
        source_date="2026-04-24",
        claim=claim,
        quoted_text=claim + " in the cited source",
        interpretation=f"Test observation: {claim}",
        quote_type=quote_type,
    ))
    assert "observation_id" in result, f"obs creation failed: {result}"
    return result["observation_id"]


def _mk_person(name: str, role: str = "test collaborator") -> str:
    """Create a person node via the impl and return its ID."""
    result = json.loads(engram_trust._add_person_impl(
        name=name,
        role=role,
    ))
    assert "person_id" in result, f"person creation failed: {result}"
    return result["person_id"]


def _mk_derivation(claim: str, support_ids: list, reasoning_type: str = "deductive_hypothetical_syllogism") -> str:
    result = json.loads(engram_derive._derive_impl(
        claim=claim,
        supporting_ids=",".join(support_ids),
        logical_chain=f"Derivation: {claim}",
        reasoning_type=reasoning_type,
    ))
    assert "derivation_id" in result, f"derivation failed: {result}"
    return result["derivation_id"]


def _mk_cornerstone(tag: str, title: str, supporting_ids: list = None) -> str:
    """Create a cornerstone node via the impl and return its ID."""
    result = json.loads(engram_cornerstone._add_cornerstone_impl(
        tag=tag,
        title=title,
        new_frame=f"New frame for {title}",
        prior_frame=f"Prior frame for {title}",
        triggering_experience=f"Triggering experience for {title}",
        supporting_ids=",".join(supporting_ids) if supporting_ids else "",
    ))
    assert "cornerstone_id" in result, f"cornerstone creation failed: {result}"
    return result["cornerstone_id"]


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

    with engram_core.engram_sandbox():

        # ---- Test 1: happy path — supersede a derivation ----
        ob1 = _mk_observation("Premise A")
        ob2 = _mk_observation("Premise B")
        dv1 = _mk_derivation("Initial conclusion", [ob1, ob2], "inductive_generalization")
        # Create replacement derivation with corrected reasoning_type
        dv2 = _mk_derivation("Initial conclusion", [ob1, ob2], "authority_expert")
        r = json.loads(engram_revision._supersede_impl(dv1, dv2, supersede_reason="reasoning_type correction: inductive_generalization → authority_expert"))
        assert r["status"] == "superseded", f"T1: {r}"
        assert r["old_node_id"] == dv1
        assert r["new_node_id"] == dv2

        conn = engram_core._get_db()
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
        r = json.loads(engram_revision._supersede_impl(dv3, dv4))
        assert r["status"] == "superseded", f"T2: {r}"
        assert r["supersede_reason"] is None
        conn = engram_core._get_db()
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
        r = json.loads(engram_revision._supersede_impl(old_ob, new_ob, supersede_reason="quote_type correction: editorial → hard_data"))
        assert r["status"] == "superseded", f"T3: {r}"
        conn = engram_core._get_db()
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
        r = json.loads(engram_revision._supersede_impl(ob5, dv5))
        assert "error" in r and "Type mismatch" in r["error"], f"T4: expected type mismatch error, got {r}"
        print("  T4  PASS — cross-type supersede (obs → derivation) blocked")
        passed += 1

        # ---- Test 5: feeling_report blocked ----
        # engram_report_feeling migrated to single-payload signature (wave 2a).
        fl_result = json.loads(engram_tasks._report_feeling_impl(
            reported_state="test feeling for supersede block",
            trigger="sandbox test",
        ))
        fl_id = fl_result.get("feeling_id") or fl_result.get("node_id")
        assert fl_id, f"T5: feeling creation failed: {fl_result}"
        # Can't create another feeling with same-type for cross-feel test; use any node. Type mismatch hits first.
        # To isolate the feeling_report block, craft a same-type attempt — make second feeling.
        fl_result2 = json.loads(engram_tasks._report_feeling_impl(
            reported_state="second feeling",
            trigger="sandbox test",
        ))
        fl_id2 = fl_result2.get("feeling_id") or fl_result2.get("node_id")
        r = json.loads(engram_revision._supersede_impl(fl_id, fl_id2))
        assert "error" in r and "feeling reports cannot be superseded" in r["error"], f"T5: {r}"
        print("  T5  PASS — feeling_report supersede blocked with clear message")
        passed += 1

        # ---- Test 6: new already supersedes — blocked ----
        # dv4 already supersedes dv3 (from T2). Try to make dv4 supersede something else.
        ob6 = _mk_observation("Premise F")
        dv6 = _mk_derivation("Another claim", [ob6])
        r = json.loads(engram_revision._supersede_impl(dv6, dv4))
        assert "error" in r and "already supersedes" in r["error"], f"T6: {r}"
        print("  T6  PASS — new node already superseding something → blocked")
        passed += 1

        # ---- Test 7: old already superseded (is_current=0) — blocked ----
        # dv3 is already superseded (from T2). Try to supersede it again.
        dv7 = _mk_derivation("Yet another refinement", [ob3])
        r = json.loads(engram_revision._supersede_impl(dv3, dv7))
        assert "error" in r and "not current" in r["error"], f"T7: {r}"
        print("  T7  PASS — old already superseded → blocked")
        passed += 1

        # ---- Test 8: nonexistent IDs blocked ----
        r = json.loads(engram_revision._supersede_impl("dv_9999", dv7))
        assert "error" in r and "not found" in r["error"], f"T8a: {r}"
        r = json.loads(engram_revision._supersede_impl(dv7, "dv_9999"))
        assert "error" in r and "not found" in r["error"], f"T8b: {r}"
        print("  T8  PASS — missing IDs blocked (both old and new)")
        passed += 1

        # ---- Test 9: old == new blocked ----
        r = json.loads(engram_revision._supersede_impl(dv7, dv7))
        assert "error" in r and ("must differ" in r["error"] or "already supersedes" in r["error"]), f"T9: {r}"
        print("  T9  PASS — same-ID supersede blocked")
        passed += 1

        # ---- Test 10: DAG invariant — new older than old → blocked ----
        conn = engram_core._get_db()
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
        r = json.loads(engram_revision._supersede_impl(dv_newer, dv_older))
        assert "error" in r and "DAG invariant" in r["error"], f"T10: {r}"
        print("  T10 PASS — DAG invariant violation (new older than old) blocked")
        passed += 1

        # ---- Test 11: stale cascade walks downstream ----
        # Build: ob_X → dv_upstream → dv_downstream. Supersede dv_upstream. dv_downstream should go stale.
        ob_x = _mk_observation("Premise X for cascade")
        dv_upstream = _mk_derivation("Upstream claim", [ob_x])
        dv_downstream = _mk_derivation("Downstream depends on upstream", [dv_upstream])
        dv_upstream_v2 = _mk_derivation("Upstream claim refined", [ob_x])
        r = json.loads(engram_revision._supersede_impl(dv_upstream, dv_upstream_v2))
        assert r["stale_count"] >= 1, f"T11: expected cascade, got stale_count={r['stale_count']}"
        stale_ids = {s["id"] for s in r["stale_downstream"]}
        assert dv_downstream in stale_ids, f"T11: {dv_downstream} should be in stale set {stale_ids}"
        # Migrated off metadata.stale_by/stale_replacement to cascade_markers
        # (#2013/#2009 slice 1) — dv_downstream should carry a pending
        # marker rooted at dv_upstream; dv_upstream's own superseded_by
        # gives the replacement (stale_replacement is retired, see
        # engram_revision.py's module docstring).
        conn = engram_core._get_db()
        try:
            marker = conn.execute(
                "SELECT 1 FROM cascade_markers WHERE node_id = ? AND root_node = ? "
                "AND root_op = 'supersede' AND status = 'pending'",
                (dv_downstream, dv_upstream),
            ).fetchone()
            assert marker is not None, "T11: pending marker missing on downstream node"
            up_row = conn.execute(
                "SELECT superseded_by FROM nodes WHERE id = ?", (dv_upstream,)
            ).fetchone()
            assert up_row["superseded_by"] == dv_upstream_v2, (
                f"T11: dv_upstream.superseded_by wrong: {up_row['superseded_by']}"
            )
        finally:
            conn.close()
        print("  T11 PASS — stale cascade walks and marks downstream")
        passed += 1

        # ---- Test 12: chain supersede A ← B ← C ----
        ob_y = _mk_observation("Chain premise")
        a = _mk_derivation("Version A", [ob_y])
        b = _mk_derivation("Version B", [ob_y])
        c = _mk_derivation("Version C", [ob_y])
        r1 = json.loads(engram_revision._supersede_impl(a, b))
        assert r1["status"] == "superseded", f"T12.1: {r1}"
        r2 = json.loads(engram_revision._supersede_impl(b, c))
        assert r2["status"] == "superseded", f"T12.2: {r2}"
        conn = engram_core._get_db()
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
        json.loads(engram_revision._retract_impl(
            node_id=ob_retracted,
            error_type="hallucinated_claim",
            reason="test",
            replacement_absent_because="NONE_BECAUSE_UNKNOWN",
            replacement_absent_note="test fixture -- replacement decision not under test",
        ))
        ob_replacement = _mk_observation("Replacement")
        r = json.loads(engram_revision._supersede_impl(ob_retracted, ob_replacement))
        assert "error" in r and "not current" in r["error"], f"T13: {r}"
        print("  T13 PASS — supersede of retracted old node blocked (is_current=0)")
        passed += 1

        # ---- Test 14: edit history audit trail recorded ----
        ob_a = _mk_observation("Audit test A")
        ob_b = _mk_observation("Audit test B")
        json.loads(engram_revision._supersede_impl(ob_a, ob_b))
        conn = engram_core._get_db()
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

        # ---- Test 15a: #2396 — retract of an already-SUPERSEDED node is
        #      blocked, and the error names the current successor ----
        ob15 = _mk_observation("Premise for #2396 sibling test")
        dv15_old = _mk_derivation("Claim to be superseded", [ob15])
        dv15_new = _mk_derivation("Replacement claim", [ob15])
        sup15 = json.loads(engram_revision._supersede_impl(dv15_old, dv15_new))
        assert sup15["status"] == "superseded", f"T15a setup: {sup15}"
        r = json.loads(engram_revision._retract_impl(
            node_id=dv15_old, error_type="other", reason="T15a plain retract of superseded node",
        ))
        assert "error" in r and "not current" in r["error"], f"T15a: {r}"
        assert dv15_new in r["error"], f"T15a: error should name the successor: {r}"
        assert r.get("successor_id") == dv15_new, f"T15a: successor_id should be {dv15_new}: {r}"
        print("  T15a PASS — retract of already-superseded node blocked, names successor (#2396)")
        passed += 1

        # ---- Test 15b: #2396 — retract of an already-RETRACTED node is
        #      blocked (idempotency); no successor_id since none exists ----
        r2 = json.loads(engram_revision._retract_impl(
            node_id=ob_retracted, error_type="other", reason="T15b second retract of retracted node",
        ))
        assert "error" in r2 and "not current" in r2["error"], f"T15b: {r2}"
        assert "successor_id" not in r2, f"T15b: a purely-retracted node has no successor: {r2}"
        print("  T15b PASS — second retract of already-retracted node blocked (idempotency, #2396)")
        passed += 1

    print(f"\n{'='*56}")
    print(f"  {passed} passed, {failed} failed")
    print('='*56)
    assert failed == 0, f"{failed} sub-test(s) failed"


def test_supersede_person_migrates_about_edges():
    passed = 0
    failed = 0

    with engram_core.engram_sandbox():

        # ---- Test A: person supersede migrates incoming about-edges ----
        pn_a = _mk_person("Person A")
        ob_x = _mk_observation("Observation about person A")
        link_r = json.loads(engram_cornerstone._link_about_impl(
            node_id=ob_x,
            person_id=pn_a,
        ))
        assert link_r.get("status") in ("linked", "already_linked"), f"TA link_about failed: {link_r}"
        pn_b = _mk_person("Person B")
        sup_r = json.loads(engram_revision._supersede_impl(pn_a, pn_b, supersede_reason="test person supersede"))
        assert sup_r["status"] == "superseded", f"TA supersede failed: {sup_r}"

        conn = engram_core._get_db()
        try:
            # ob_x now has an about-edge to pn_b (the new current node)
            new_edge = conn.execute(
                "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
                (ob_x, pn_b),
            ).fetchone()
            assert new_edge is not None, f"TA: about edge {ob_x}→{pn_b} not found after person supersede"
            # ob_x's original about-edge to pn_a is still present (audit trail)
            old_edge = conn.execute(
                "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
                (ob_x, pn_a),
            ).fetchone()
            assert old_edge is not None, f"TA: original about edge {ob_x}→{pn_a} should remain (audit trail)"
        finally:
            conn.close()
        print("  TA  PASS — person supersede migrates about-edges to new node; old edge preserved")
        passed += 1

        # ---- Test B: duplicate guard — about-edge already on new node is not doubled ----
        pn_c = _mk_person("Person C")
        ob_y = _mk_observation("Observation about person C")
        pn_d = _mk_person("Person D")
        # Pre-link ob_y to BOTH pn_c and pn_d before superseding
        json.loads(engram_cornerstone._link_about_impl(node_id=ob_y, person_id=pn_c))
        json.loads(engram_cornerstone._link_about_impl(node_id=ob_y, person_id=pn_d))
        sup_r2 = json.loads(engram_revision._supersede_impl(pn_c, pn_d))
        assert sup_r2["status"] == "superseded", f"TB supersede failed: {sup_r2}"

        conn = engram_core._get_db()
        try:
            # Exactly one about-edge from ob_y to pn_d (no duplicate)
            about_edges = conn.execute(
                "SELECT COUNT(*) AS cnt FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
                (ob_y, pn_d),
            ).fetchone()
            assert about_edges["cnt"] == 1, f"TB: expected 1 about edge {ob_y}→{pn_d}, got {about_edges['cnt']}"
        finally:
            conn.close()
        print("  TB  PASS — duplicate guard: pre-existing about-edge on new node not doubled")
        passed += 1

        # ---- Test C: non-person supersede does NOT create about-edges spuriously ----
        ob_p = _mk_observation("Claim P")
        ob_q = _mk_observation("Claim Q")
        # Set up a migratable incoming about-edge on ob_p so this guard is
        # non-vacuous: if the (observation) supersede wrongly ran the person
        # migration, it would create an about-edge TARGETING ob_q.
        _setup = engram_core._get_db()
        try:
            _setup.execute(
                "INSERT INTO edges (source_id, target_id, relation, created_at)"
                " VALUES (?, ?, 'about', ?)",
                (ob_q, ob_p, "2026-01-01T00:00:00Z"),
            )
            _setup.commit()
        finally:
            _setup.close()

        sup_r3 = json.loads(engram_revision._supersede_impl(ob_p, ob_q))
        assert sup_r3["status"] == "superseded", f"TC supersede failed: {sup_r3}"

        conn = engram_core._get_db()
        try:
            # Gate is type=='person'; ob_p is an observation, so the migration
            # must NOT fire — no about-edge migrated ONTO ob_q. Direction matters:
            # the migration writes edges with target_id=new_node, not source_id.
            about_edges_q = conn.execute(
                "SELECT COUNT(*) AS cnt FROM edges WHERE target_id = ? AND relation = 'about'",
                (ob_q,),
            ).fetchone()
            assert about_edges_q["cnt"] == 0, f"TC: non-person supersede spuriously migrated about-edges: {about_edges_q['cnt']}"
        finally:
            conn.close()
        print("  TC  PASS — non-person supersede does not create spurious about-edges")
        passed += 1

    print(f"\n{'='*56}")
    print(f"  {passed} passed, {failed} failed")
    print('='*56)
    assert failed == 0, f"{failed} sub-test(s) failed"


def test_supersede_person_transfers_self_anchor():
    """Superseding the self-anchor must carry is_self + trust_tier='self' onto
    the replacement. Without the transfer, supersede flips old.is_current=0 and
    leaves no current self-anchor (every lookup is `is_self=1 AND is_current=1`),
    silently breaking link_about-default / focus='self' / tier='self'."""
    passed = 0
    failed = 0

    def _current_selves(conn):
        return conn.execute(
            "SELECT id FROM nodes WHERE type = 'person' "
            "AND json_extract(metadata, '$.is_self') = 1 AND is_current = 1"
        ).fetchall()

    with engram_core.engram_sandbox():

        # ---- Test A: self-anchor supersede transfers is_self + trust_tier ----
        old_self = json.loads(engram_trust._add_person_impl(
            name="Self Old", role="agent self-anchor", is_self=True,
        ))["person_id"]
        # The live self-anchor carries trust_tier='self' (set by backfill at
        # install); mirror that so the transfer is exercised on a real value.
        _s = engram_core._get_db()
        try:
            _s.execute("UPDATE nodes SET trust_tier = 'self' WHERE id = ?", (old_self,))
            _s.commit()
        finally:
            _s.close()

        # The singleton guard blocks creating the replacement WITH is_self while
        # the old anchor is still current — this is exactly why supersede has to
        # carry the flag over rather than the caller setting it at creation.
        blocked = json.loads(engram_trust._add_person_impl(
            name="Self New (blocked)", role="agent self-anchor", is_self=True,
        ))
        assert "error" in blocked and blocked.get("existing_self_id") == old_self, \
            f"TA: singleton guard should block a second is_self while old is current: {blocked}"

        new_self = _mk_person("Self New", role="agent self-anchor")
        sup = json.loads(engram_revision._supersede_impl(
            old_self, new_self, supersede_reason="model lineage update"))
        assert sup["status"] == "superseded", f"TA supersede failed: {sup}"

        conn = engram_core._get_db()
        try:
            row = conn.execute(
                "SELECT metadata, trust_tier, is_current FROM nodes WHERE id = ?",
                (new_self,),
            ).fetchone()
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            assert meta.get("is_self") is True, f"TA: new node missing is_self: {meta}"
            assert row["trust_tier"] == "self", f"TA: new trust_tier not 'self': {row['trust_tier']}"
            assert row["is_current"] == 1, "TA: new node should be current"
            # Exactly one current self-anchor, and it's the replacement.
            selves = _current_selves(conn)
            assert len(selves) == 1 and selves[0]["id"] == new_self, \
                f"TA: expected exactly the new node as current self-anchor, got {[s['id'] for s in selves]}"
            # Old node retains is_self as a historical snapshot, but non-current.
            old_row = conn.execute(
                "SELECT metadata, is_current FROM nodes WHERE id = ?", (old_self,),
            ).fetchone()
            old_meta = json.loads(old_row["metadata"]) if old_row["metadata"] else {}
            assert old_meta.get("is_self") is True, "TA: old node should retain is_self snapshot"
            assert old_row["is_current"] == 0, "TA: old node should be non-current"
        finally:
            conn.close()
        print("  TA  PASS — self-anchor supersede transfers is_self + trust_tier='self'; exactly one current self-anchor")
        passed += 1

        # ---- Test B: superseding a NON-self person mints no spurious self-anchor ----
        pn_x = _mk_person("Plain X")
        pn_y = _mk_person("Plain Y")
        sup2 = json.loads(engram_revision._supersede_impl(pn_x, pn_y))
        assert sup2["status"] == "superseded", f"TB supersede failed: {sup2}"
        conn = engram_core._get_db()
        try:
            row = conn.execute(
                "SELECT metadata, trust_tier FROM nodes WHERE id = ?", (pn_y,),
            ).fetchone()
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            assert not meta.get("is_self"), f"TB: non-self supersede wrongly set is_self: {meta}"
            assert row["trust_tier"] != "self", f"TB: non-self supersede wrongly set trust_tier='self': {row['trust_tier']}"
            # The current self-anchor is unchanged (still new_self from Test A).
            selves = _current_selves(conn)
            assert len(selves) == 1 and selves[0]["id"] == new_self, \
                f"TB: non-self supersede must not change the current self-anchor: {[s['id'] for s in selves]}"
        finally:
            conn.close()
        print("  TB  PASS — non-self person supersede does not create a spurious self-anchor")
        passed += 1

        # ---- Test C: chain supersede of the self-anchor (A→B→C) propagates ----
        # Every future model-version bump is a *chain* supersede of the anchor,
        # so the transfer must survive multiple hops. We continue the chain from
        # new_self (the current self-anchor after Test A) so the singleton guard
        # isn't tripped by a second is_self at creation time.
        chain_b = _mk_person("Self Chain B", role="agent self-anchor")
        sup_cb = json.loads(engram_revision._supersede_impl(
            new_self, chain_b, supersede_reason="chain hop 1"))
        assert sup_cb["status"] == "superseded", f"TC hop1 failed: {sup_cb}"
        chain_c = _mk_person("Self Chain C", role="agent self-anchor")
        sup_cc = json.loads(engram_revision._supersede_impl(
            chain_b, chain_c, supersede_reason="chain hop 2"))
        assert sup_cc["status"] == "superseded", f"TC hop2 failed: {sup_cc}"

        conn = engram_core._get_db()
        try:
            row = conn.execute(
                "SELECT metadata, trust_tier FROM nodes WHERE id = ?", (chain_c,),
            ).fetchone()
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            assert meta.get("is_self") is True, f"TC: is_self lost across chain: {meta}"
            assert row["trust_tier"] == "self", f"TC: trust_tier lost across chain: {row['trust_tier']}"
            selves = _current_selves(conn)
            assert len(selves) == 1 and selves[0]["id"] == chain_c, \
                f"TC: chain end must be the sole current self-anchor: {[s['id'] for s in selves]}"
        finally:
            conn.close()
        print("  TC  PASS — chain supersede (A→B→C) propagates the self-anchor across every hop")
        passed += 1

        # ---- Test D: about-migration AND self-transfer fire together ----
        # The two person-branch sub-paths are independent, but the combined path
        # (a self-anchor that also has incoming about-edges) is the real-world
        # case — exercise both at once. chain_c is the current self-anchor.
        ob_z = _mk_observation("Observation about the self-anchor")
        link_z = json.loads(engram_cornerstone._link_about_impl(
            node_id=ob_z, person_id=chain_c))
        assert link_z.get("status") in ("linked", "already_linked"), f"TD link failed: {link_z}"
        d_new = _mk_person("Self D-new", role="agent self-anchor")
        sup_d = json.loads(engram_revision._supersede_impl(
            chain_c, d_new, supersede_reason="self-anchor with about-edges"))
        assert sup_d["status"] == "superseded", f"TD supersede failed: {sup_d}"

        conn = engram_core._get_db()
        try:
            # self-transfer fired
            row = conn.execute(
                "SELECT metadata, trust_tier FROM nodes WHERE id = ?", (d_new,),
            ).fetchone()
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            assert meta.get("is_self") is True, f"TD: is_self not transferred on combined path: {meta}"
            assert row["trust_tier"] == "self", f"TD: trust_tier not transferred on combined path: {row['trust_tier']}"
            # about-migration fired
            about_edge = conn.execute(
                "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
                (ob_z, d_new),
            ).fetchone()
            assert about_edge is not None, f"TD: about-edge {ob_z}→{d_new} not migrated on combined path"
            selves = _current_selves(conn)
            assert len(selves) == 1 and selves[0]["id"] == d_new, \
                f"TD: combined-path replacement must be the sole current self-anchor: {[s['id'] for s in selves]}"
        finally:
            conn.close()
        print("  TD  PASS — about-migration and self-transfer both fire on a self-anchor with about-edges")
        passed += 1

    print(f"\n{'='*56}")
    print(f"  {passed} passed, {failed} failed")
    print('='*56)
    assert failed == 0, f"{failed} sub-test(s) failed"


def test_supersede_person_migrates_supported_by():
    """Person supersede migrates incoming supported_by edges from cornerstones/lessons.

    Verifies #1209: Aleph's remaining gap — cs/ls supported_by → old_person
    edges are rerooted to the new person; non-cs/ls supported_by edges are NOT
    migrated (they get stale-marked by the cascade instead).
    """
    passed = 0
    failed = 0

    with engram_core.engram_sandbox():

        # ---- Test A: cornerstone with supported_by→old_person gets migrated ----
        pn_old = _mk_person("Person Old")
        ob_anchor = _mk_observation("Anchor observation about pn_old")
        cs_id = _mk_cornerstone("test-anchor", "cornerstone anchored in pn_old",
                                supporting_ids=[pn_old, ob_anchor])
        pn_new = _mk_person("Person New")
        sup_r = json.loads(engram_revision._supersede_impl(pn_old, pn_new,
                                                           supersede_reason="alias correction"))
        assert sup_r["status"] == "superseded", f"TA: supersede failed: {sup_r}"

        conn = engram_core._get_db()
        try:
            # New supported_by edge: cs → pn_new
            new_edge = conn.execute(
                "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='supported_by'",
                (cs_id, pn_new),
            ).fetchone()
            assert new_edge is not None, f"TA: cs→pn_new supported_by edge missing after supersede"
            # Old supported_by edge preserved (audit trail)
            old_edge = conn.execute(
                "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='supported_by'",
                (cs_id, pn_old),
            ).fetchone()
            assert old_edge is not None, f"TA: cs→pn_old supported_by edge should remain as audit trail"
        finally:
            conn.close()
        print("  TA  PASS — person supersede migrates cs supported_by to new node; old preserved")
        passed += 1

        # ---- Test B: duplicate guard — pre-existing cs→pn_new edge not doubled ----
        pn_x = _mk_person("Person X")
        pn_y = _mk_person("Person Y")
        cs2 = _mk_cornerstone("test-dup", "cornerstone supported by both persons",
                               supporting_ids=[pn_x, pn_y])
        sup_r2 = json.loads(engram_revision._supersede_impl(pn_x, pn_y))
        assert sup_r2["status"] == "superseded", f"TB: supersede failed: {sup_r2}"

        conn = engram_core._get_db()
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM edges WHERE source_id=? AND target_id=? AND relation='supported_by'",
                (cs2, pn_y),
            ).fetchone()["c"]
            assert cnt == 1, f"TB: duplicate guard failed — {cnt} cs→pn_y edges (expected 1)"
        finally:
            conn.close()
        print("  TB  PASS — duplicate guard: pre-existing cs→pn_y supported_by not doubled")
        passed += 1

        # ---- Test C: cornerstone is NOT in stale list (cascade already skips cs/ls) ----
        pn_a = _mk_person("Person A")
        cs3 = _mk_cornerstone("test-stale", "should not be stale after supersede",
                               supporting_ids=[pn_a])
        pn_b = _mk_person("Person B")
        sup_r3 = json.loads(engram_revision._supersede_impl(pn_a, pn_b))
        assert sup_r3["status"] == "superseded", f"TC: supersede failed: {sup_r3}"
        stale_ids = [s["id"] for s in sup_r3.get("stale_downstream", [])]
        assert cs3 not in stale_ids, f"TC: cornerstone {cs3} incorrectly appears in stale list"
        print("  TC  PASS — cornerstone not in stale list (cs/ls cascade skip rule)")
        passed += 1

        # ---- Test D: non-person supersede does NOT migrate supported_by edges ----
        ob_p = _mk_observation("Claim P — premise")
        ob_q = _mk_observation("Claim Q — replacement")
        cs4 = _mk_cornerstone("test-non-person", "cornerstone anchored in ob_p",
                               supporting_ids=[ob_p])
        sup_r4 = json.loads(engram_revision._supersede_impl(ob_p, ob_q))
        assert sup_r4["status"] == "superseded", f"TD: supersede failed: {sup_r4}"

        conn = engram_core._get_db()
        try:
            # No edge cs4 → ob_q should exist (ob_p is not a person; migration doesn't run)
            migrated = conn.execute(
                "SELECT 1 FROM edges WHERE source_id=? AND target_id=? AND relation='supported_by'",
                (cs4, ob_q),
            ).fetchone()
            assert migrated is None, f"TD: non-person supersede spuriously migrated supported_by to {ob_q}"
        finally:
            conn.close()
        print("  TD  PASS — non-person supersede does not migrate supported_by edges")
        passed += 1

    print(f"\n{'='*56}")
    print(f"  {passed} passed, {failed} failed")
    print('='*56)
    assert failed == 0, f"{failed} sub-test(s) failed"


if __name__ == "__main__":
    test_supersede_relational()
    test_supersede_person_migrates_about_edges()
    test_supersede_person_transfers_self_anchor()
    test_supersede_person_migrates_supported_by()
