"""
Tests for quote verification and kg_retract tool.

Layer 1: Prevention — quoted_text must exist in file:// evidence sources
Layer 2: Correction — kg_retract marks errors, cascades taint to downstream nodes
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_retract_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))
THIS_FILE = os.path.abspath(__file__)


EVIDENCE_FILE = os.path.join(TEST_DIR, "evidence_source.txt")


def _ensure_evidence_file():
    """Create a small known-content file to use as evidence."""
    with open(EVIDENCE_FILE, "w") as f:
        f.write("This is line one of the evidence file.\n")
        f.write("The API rate limit is 1000 requests per hour.\n")
        f.write("Revenue grew 15% year-over-year in Q4 2025.\n")


def fresh_client():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR, exist_ok=True)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    _ensure_evidence_file()
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


# ── Layer 1: Quote verification ────────────────────────────────────────────

def test_quote_found_in_file():
    """Quoted text that exists in the file should succeed."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": f"file://{EVIDENCE_FILE}",
        "title": "Evidence source",
        "quoted_text": "The API rate limit is 1000 requests per hour.",
        "interpretation": "Testing quote verification.",
        "claim": "Quote verification passes for real quotes",
        "quote_type": "hard_data",
        "source_class": "introspective",
    })
    assert r["status"] == "created", f"Expected created, got {r}"


def test_quote_not_found_in_file():
    """Fabricated quoted text should be rejected."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": f"file://{EVIDENCE_FILE}",
        "title": "Evidence source",
        "quoted_text": "The API rate limit is 5000 requests per second.",
        "interpretation": "Fabricated quote — not in the file.",
        "claim": "This should be rejected",
        "quote_type": "hard_data",
        "source_class": "introspective",
    })
    assert "error" in r, f"Expected error for fabricated quote, got {r}"
    assert "not found" in r["error"].lower(), f"Error should mention 'not found': {r['error']}"


def test_quote_check_skipped_for_https():
    """https:// URLs should not be quote-checked (can't fetch)."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Web Article",
        "quoted_text": "Anything goes for web URLs since we can't verify",
        "interpretation": "No verification for remote URLs.",
        "claim": "Web quotes are not verified at creation time",
        "quote_type": "hard_data",
    })
    assert r["status"] == "created"


def test_quote_in_jsonl_file():
    """JSONL files should be searchable for quotes within JSON values."""
    # fresh_client() resets TEST_DIR; create the JSONL file after so it survives.
    client = fresh_client()
    jsonl_path = os.path.join(TEST_DIR, "test_evidence.jsonl")
    with open(jsonl_path, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "The deadline is March 15"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": "I'll note that deadline"}}) + "\n")
    r = client.call("engram_add_observation", {
        "url": f"file://{jsonl_path}",
        "title": "Test JSONL transcript",
        "quoted_text": "The deadline is March 15",
        "interpretation": "User stated deadline.",
        "claim": "Deadline is March 15",
        "quote_type": "official_statement",
        "source_class": "user_stated",
    })
    assert r["status"] == "created", f"Expected created for JSONL quote, got {r}"

    # Fabricated quote should fail
    r2 = client.call("engram_add_observation", {
        "evidence_id": r["evidence_id"],
        "quoted_text": "This text is not in the JSONL file at all xyz999",
        "interpretation": "Fake.",
        "claim": "Should fail",
        "quote_type": "hard_data",
    })
    assert "error" in r2, f"Expected error for fabricated JSONL quote, got {r2}"

    os.unlink(jsonl_path)


def test_batch_quote_verification():
    """Batch observations should also verify quotes."""
    client = fresh_client()
    obs = [
        {
            "quoted_text": "Revenue grew 15% year-over-year in Q4 2025.",
            "interpretation": "Real data from evidence file.",
            "claim": "Revenue grew 15%",
            "quote_type": "hard_data",
            "source_class": "introspective",
        },
        {
            "quoted_text": "Revenue grew 50% which is completely made up.",
            "interpretation": "Fabricated.",
            "claim": "This should fail",
            "quote_type": "hard_data",
            "source_class": "introspective",
        },
    ]
    r = client.call("engram_add_observation_batch", {
        "url": f"file://{EVIDENCE_FILE}",
        "title": "Evidence source",
        "observations_json": json.dumps(obs),
    })
    assert r["status"] == "batch_complete"
    # First should succeed, second should fail
    assert r["results"][0]["status"] == "created"
    assert "error" in r["results"][1], f"Expected error for fabricated batch quote: {r['results'][1]}"


# ── Layer 2: kg_retract ────────────────────────────────────────────────────

def test_retract_basic():
    """Retract a node — should mark as retracted with error metadata."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Article",
        "quoted_text": "Some data.",
        "interpretation": "Test.",
        "claim": "Claim to retract",
        "quote_type": "hard_data",
    })
    ob_id = r["observation_id"]

    ret = client.call("engram_retract", {
        "node_id": ob_id,
        "error_type": "fabricated_quote",
        "reason": "The quoted text was not actually in the source.",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    })
    assert ret["status"] == "retracted"
    assert ret["node_id"] == ob_id
    assert ret["error_type"] == "fabricated_quote"

    # Verify the node is marked retracted (use inspect for full node content)
    insp = client.call("engram_inspect", {"node_id": ob_id, "view": "deep"})
    node = insp["node"]
    assert node["status"] == "retracted"
    assert node["is_current"] == 0
    meta = json.loads(node["metadata"]) if isinstance(node["metadata"], str) else node["metadata"]
    assert meta["retracted"] is True
    assert meta["error_type"] == "fabricated_quote"


def test_retract_invalid_error_type():
    """Invalid error_type should be rejected."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/a",
        "title": "A",
        "quoted_text": "Data. in the cited source",
        "interpretation": "Test.",
        "claim": "Test",
        "quote_type": "hard_data",
    })
    ret = client.call("engram_retract", {
        "node_id": r["observation_id"],
        "error_type": "invalid_type",
        "reason": "Test.",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    })
    assert "error" in ret


def test_retract_cascades_taint():
    """Retracting a node should taint all downstream derivations."""
    client = fresh_client()
    # Create two observations
    r1 = client.call("engram_add_observation", {
        "url": "https://example.com/a",
        "title": "Source A",
        "quoted_text": "Premise A.",
        "interpretation": "First premise.",
        "claim": "Premise A is true",
        "quote_type": "hard_data",
    })
    r2 = client.call("engram_add_observation", {
        "url": "https://example.com/b",
        "title": "Source B",
        "quoted_text": "Premise B.",
        "interpretation": "Second premise.",
        "claim": "Premise B is true",
        "quote_type": "hard_data",
    })
    # Derive from both
    d = client.call("engram_derive", {
        "claim": "Conclusion from A and B",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "A + B => conclusion.",
        "reasoning_type": "deductive_modus_ponens",
    })
    dv_id = d["derivation_id"]

    # Retract observation A
    ret = client.call("engram_retract", {
        "node_id": r1["observation_id"],
        "error_type": "fabricated_quote",
        "reason": "Quote was fabricated.",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    })
    assert ret["tainted_count"] >= 1, f"Expected tainted downstream, got {ret}"
    tainted_ids = [t["id"] for t in ret["tainted_downstream"]]
    assert dv_id in tainted_ids, f"Derivation {dv_id} should be tainted, got {tainted_ids}"

    # Verify taint is a pending cascade marker on the derivation, rooted at
    # the retracted observation. Migrated off metadata.tainted_by to
    # cascade_markers (#2013/#2009 slice 1 — the cascade machine); the
    # inspect API's own "warnings.tainted_by" surfaces the same read, so
    # checking it here also exercises engram_query._extract_warnings.
    insp = client.call("engram_inspect", {"node_id": dv_id, "view": "deep"})
    warnings = insp.get("warnings") or {}
    tainted_ids = [w["retracted_id"] for w in warnings.get("tainted_by", [])]
    assert r1["observation_id"] in tainted_ids, f"expected {r1['observation_id']!r} in {tainted_ids}"


def test_retract_multi_level_cascade():
    """Taint should cascade through multiple derivation levels."""
    client = fresh_client()
    r1 = client.call("engram_add_observation", {
        "url": "https://example.com/a",
        "title": "Source",
        "quoted_text": "Base fact.",
        "interpretation": "Foundation.",
        "claim": "Base fact",
        "quote_type": "hard_data",
    })
    d1 = client.call("engram_derive", {
        "claim": "Level 1 derivation",
        "supporting_ids": r1["observation_id"],
        "logical_chain": "From base fact.",
        "reasoning_type": "deductive_modus_ponens",
    })
    d2 = client.call("engram_derive", {
        "claim": "Level 2 derivation",
        "supporting_ids": d1["derivation_id"],
        "logical_chain": "From level 1.",
        "reasoning_type": "deductive_hypothetical_syllogism",
    })

    # Retract the base observation
    ret = client.call("engram_retract", {
        "node_id": r1["observation_id"],
        "error_type": "hallucinated_claim",
        "reason": "Claim was hallucinated.",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    })
    tainted_ids = [t["id"] for t in ret["tainted_downstream"]]
    assert d1["derivation_id"] in tainted_ids, "Level 1 should be tainted"
    assert d2["derivation_id"] in tainted_ids, "Level 2 should be tainted"


def test_retract_with_replacement():
    """Retract with replacement_id — the caller creates the replacement first
    via its own canonical creation tool, then names it (#2612: purely
    relational, engram_retract does NOT create the replacement node)."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/article",
        "title": "Article",
        "quoted_text": "Wrong quote.",
        "interpretation": "Bad interpretation.",
        "claim": "Wrong claim",
        "quote_type": "hard_data",
    })
    ob_id = r["observation_id"]
    ev_id = r["evidence_id"]

    repl = client.call("engram_add_observation", {
        "evidence_id": ev_id,
        "quoted_text": "Correct quote from the article.",
        "interpretation": "Proper interpretation.",
        "claim": "Correct claim",
        "quote_type": "hard_data",
    })
    repl_id = repl["observation_id"]

    ret = client.call("engram_retract", {
        "node_id": ob_id,
        "error_type": "fabricated_quote",
        "reason": "Original quote was fabricated.",
        "replacement_id": repl_id,
    })
    assert ret["status"] == "retracted"
    assert ret.get("replacement_id") == repl_id, f"Expected replacement_id, got {ret}"

    # Verify replacement exists and cites same evidence.
    # include_superseded=True because the retracted node (is_current=0) is the
    # target of the retracts edge; without it the default neighbor filter hides it.
    insp = client.call("engram_inspect", {"node_id": repl_id, "include_superseded": True})
    repl_node = insp["node"]
    assert repl_node["claim"] == "Correct claim"
    # Check retracts edge exists (inspect recall view exposes 1-hop logical neighbours in grouped dict)
    lateral = insp.get("logical_neighbors", {}).get("lateral", [])
    retract_neighbors = [n for n in lateral if n["relation"] == "retracts"]
    assert len(retract_neighbors) >= 1, f"Expected retracts neighbour, got lateral: {lateral}"


def test_reflect_shows_tainted_and_retracted():
    """engram_reflect should surface tainted and retracted nodes."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/a",
        "title": "Source",
        "quoted_text": "Fact. in the cited source",
        "interpretation": "Test.",
        "claim": "A claim to retract for reflect test",
        "quote_type": "hard_data",
    })
    d = client.call("engram_derive", {
        "claim": "Derived from retractable claim",
        "supporting_ids": r["observation_id"],
        "logical_chain": "Test derivation.",
        "reasoning_type": "deductive_modus_ponens",
    })
    client.call("engram_retract", {
        "node_id": r["observation_id"],
        "error_type": "fabricated_quote",
        "reason": "Test retraction.",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "test fixture -- replacement decision not under test",
    })

    reflect = client.call("engram_reflect", {})
    assert reflect["summary"]["retracted_nodes"] >= 1
    assert reflect["summary"]["tainted_nodes"] >= 1
    assert any(t["id"] == d["derivation_id"] for t in reflect["tainted_nodes"])


# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        # Layer 1: Quote verification
        test_quote_found_in_file,
        test_quote_not_found_in_file,
        test_quote_check_skipped_for_https,
        test_quote_in_jsonl_file,
        test_batch_quote_verification,
        # Layer 2: kg_retract
        test_retract_basic,
        test_retract_invalid_error_type,
        test_retract_cascades_taint,
        test_retract_multi_level_cascade,
        test_retract_with_replacement,
        test_reflect_shows_tainted_and_retracted,
    ]

    passed = 0
    failed = 0
    errors = []
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {test.__name__}: {e}")
            failed += 1
            errors.append((test.__name__, str(e)))

    print(f"\n{'='*60}")
    print(f"{passed}/{passed + failed} tests passed")
    if errors:
        print(f"\nFailed tests:")
        for name, err in errors:
            print(f"  - {name}: {err}")
        sys.exit(1)
