"""Sandbox test for inductive_enumeration reasoning type + tool_timing diagnostic."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server


def test_inductive_enumeration_and_timing():
    with server.engram_sandbox() as sandbox_dir:
        print(f"Sandbox: {sandbox_dir}\n")

        # ── Setup: one evidence, three observations citing it ──────────
        ev = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "url": "https://plato.stanford.edu/entries/test-enumeration/",
            "title": "Test SEP entry",
            "claim": "System A is a paraconsistent logic that blocks modus ponens.",
            "quoted_text": "System A blocks modus ponens.",
            "quote_type": "attributed_analysis",
            "interpretation": "testing enumeration",
        })))
        print(f"obs1: {ev.get('observation_id')} cites {ev.get('evidence_id')}")
        obs1 = ev["observation_id"]
        ev_id = ev["evidence_id"]

        obs2 = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "evidence_id": ev_id,
            "claim": "System B is a paraconsistent logic that uses relevance constraints.",
            "quoted_text": "System B uses relevance.",
            "quote_type": "attributed_analysis",
            "interpretation": "testing",
        })))["observation_id"]
        obs3 = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "evidence_id": ev_id,
            "claim": "System C is a paraconsistent logic that blocks conjunction introduction.",
            "quoted_text": "System C blocks adjunction.",
            "quote_type": "attributed_analysis",
            "interpretation": "testing",
        })))["observation_id"]
        print(f"  obs2={obs2}, obs3={obs3} (all cite {ev_id})\n")

        # ── Test 1: inductive_enumeration with same-source premises → NO warning ──
        print("Test 1: inductive_enumeration across 3 same-source observations")
        result = json.loads(server.engram_derive(
            claim="Paraconsistent logic has at least 3 structurally-distinct strategies.",
            supporting_ids=f"{obs1},{obs2},{obs3}",
            logical_chain="Source enumerates three systems (A, B, C) each rejecting a different classical rule.",
            reasoning_type="inductive_enumeration",
        ))
        warnings = result.get("structure_warnings", [])
        print(f"  → dv_id={result.get('derivation_id')}, conf={result.get('confidence')}, warnings={warnings}")
        assert result.get("derivation_id"), f"No id returned: {result}"
        assert not any("independent sources" in w for w in warnings), \
            f"UNEXPECTED same-source warning for inductive_enumeration: {warnings}"
        print("  PASS: no same-source warning for inductive_enumeration\n")

        # ── Test 2: inductive_generalization still WARNS on same-source ──
        print("Test 2: inductive_generalization across same 3 same-source observations (regression check)")
        result2 = json.loads(server.engram_derive(
            claim="Paraconsistent logic is a family (repeated claim).",
            supporting_ids=f"{obs1},{obs2},{obs3}",
            logical_chain="Same premises as test 1 but using inductive_generalization instead.",
            reasoning_type="inductive_generalization",
        ))
        warnings2 = result2.get("structure_warnings", [])
        print(f"  → dv_id={result2.get('derivation_id')}, warnings={warnings2}")
        assert any("independent sources" in w for w in warnings2), \
            f"EXPECTED same-source warning missing for inductive_generalization: {warnings2}"
        print("  PASS: inductive_generalization still warns correctly\n")

        # ── Test 3: tool_timing table populated ──
        print("Test 3: tool_timing table populated after tool calls")
        conn = server._get_db()
        rows = conn.execute("SELECT tool_name, duration_ms, status FROM tool_timing ORDER BY id").fetchall()
        print(f"  Found {len(rows)} timing rows")
        tool_counts = {}
        for r in rows:
            tool_counts[r["tool_name"]] = tool_counts.get(r["tool_name"], 0) + 1
        for name, c in sorted(tool_counts.items(), key=lambda x: -x[1]):
            print(f"    {name}: {c}")
        assert len(rows) >= 5, f"Expected ≥5 timing rows, got {len(rows)}"
        assert "engram_add_observation" in tool_counts, "add_observation not timed"
        assert "engram_derive" in tool_counts, "derive not timed"
        for r in rows:
            assert r["duration_ms"] >= 0, f"Negative duration: {r['duration_ms']}"
            assert r["status"] in ("success", "error"), f"Bad status: {r['status']}"
        print("  PASS: tool_timing populated with valid durations\n")

        # ── Test 4: engram_diagnose exposes tool_timing section ──
        print("Test 4: engram_diagnose exposes tool_timing section")
        diag = json.loads(server.engram_diagnose())
        assert "tool_timing" in diag, f"tool_timing section missing from diagnose: {list(diag.keys())}"
        tt = diag["tool_timing"]
        print(f"  window_days={tt['window_days']}, total_calls={tt['total_calls']}, per_tool_keys={list(tt['per_tool'].keys())}")
        assert tt["total_calls"] >= 5
        assert "engram_derive" in tt["per_tool"]
        d = tt["per_tool"]["engram_derive"]
        assert all(k in d for k in ("count", "mean_ms", "p50_ms", "p95_ms", "max_ms"))
        print(f"  engram_derive stats: {d}")
        print("  PASS: tool_timing section present and well-formed\n")

        # ── Test 5: invalid reasoning_type still rejected ──
        print("Test 5: invalid reasoning_type rejection (regression check)")
        result5 = json.loads(server.engram_derive(
            claim="x",
            supporting_ids=f"{obs1},{obs2}",
            logical_chain="x",
            reasoning_type="inductive_nonsense",
        ))
        assert "error" in result5, f"Expected error for invalid type: {result5}"
        assert "inductive_enumeration" in result5.get("valid_types", []), \
            f"New type missing from valid_types list: {result5.get('valid_types')}"
        print(f"  Error returned correctly; valid_types includes inductive_enumeration")
        print("  PASS: validation works, new type exposed\n")

    print("All tests passed.")


if __name__ == "__main__":
    test_inductive_enumeration_and_timing()
