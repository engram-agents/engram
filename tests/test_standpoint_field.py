"""
Tests for the standpoint field and provenance-uniformity detection.

Tests cover:
1. standpoint columns written and retrievable from DB
2. _validate_reasoning_structure: same standpoint → per-axis "⚠ uniform" warnings
3. _validate_reasoning_structure: different standpoints → per-axis "diverse" warnings
4. _validate_reasoning_structure: no standpoint data → STANDPOINT: unchecked (#761)
5. Batch observations support per-observation standpoint fields
6. standpoint_override_tag written to DB
7. author-diverse/collection-uniform → ⚠ uniform caught on collection axis (v2 key test)
8. Single-axis premises: author-only reports author axis, omits collection
9. Mixed-axis premises: partial axis omitted (all-or-omit rule)
10. Single-premise derivation triggers STANDPOINT + FALSIFICATION (n >= 1 gate, #958)
11. Single-premise chain: ⚠⚠ fires when lineage-uniform and zero verified leaves
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_standpoint_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_client():
    """Create a fresh EngramClient with an empty database."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


def _read_node_standpoint(client, node_id):
    """Read standpoint columns directly from DB (not exposed in MCP returns)."""
    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT standpoint_author_id, standpoint_collection_id, standpoint_override_tag "
            "FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return {}
        return dict(row)
    finally:
        conn.close()


def _set_self_lineage(value):
    """Write self_lineage into the live test config so _self_lineage() reads it."""
    import server as _server
    cp = _server.core.CONFIG_PATH
    cfg = json.loads(cp.read_text())
    cfg["self_lineage"] = value
    cp.write_text(json.dumps(cfg))


# ---------------------------------------------------------------------------
# Test 1: standpoint columns written to DB
# ---------------------------------------------------------------------------

def test_standpoint_columns_written():
    """standpoint_author_id and standpoint_collection_id are stored in the DB."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/study",
        "title": "Study on X",
        "quoted_text": "The rate of X is 42%.",
        "interpretation": "Empirical rate measure.",
        "claim": "Rate of X is 42%",
        "quote_type": "hard_data",
        "standpoint_author_id": "author_001",
        "standpoint_collection_id": "corpus_alpha",
    })
    assert r.get("status") == "created", f"Expected created, got: {r}"
    sp = _read_node_standpoint(client, r["observation_id"])
    assert sp["standpoint_author_id"] == "author_001", f"Got: {sp}"
    assert sp["standpoint_collection_id"] == "corpus_alpha", f"Got: {sp}"
    assert sp["standpoint_override_tag"] is None
    print("  PASS: standpoint columns written to DB")


def test_standpoint_override_tag_written():
    """standpoint_override_tag is stored in the DB."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/lab",
        "title": "Lab measurement log",
        "quoted_text": "Temp measured at 98.6 F.",
        "interpretation": "Lab measurement.",
        "claim": "Temperature was 98.6 F",
        "quote_type": "hard_data",
        "standpoint_override_tag": "lab_measurement_2026",
    })
    assert r.get("status") == "created", f"Expected created, got: {r}"
    sp = _read_node_standpoint(client, r["observation_id"])
    assert sp["standpoint_override_tag"] == "lab_measurement_2026", f"Got: {sp}"
    print("  PASS: standpoint_override_tag written to DB")


# ---------------------------------------------------------------------------
# Test 2: Same standpoint → "1 standpoint cluster" warning
# ---------------------------------------------------------------------------

def test_same_standpoint_one_cluster_warning():
    """Two observations with the same author+collection produce a 1-cluster warning."""
    client = fresh_client()

    # Create two observations from different URLs but same standpoint
    r1 = client.call("engram_add_observation", {
        "url": "https://reuters.com/gold-article",
        "title": "Reuters gold",
        "quoted_text": "Gold fell 9.6% this week.",
        "interpretation": "Gold price drop.",
        "claim": "Gold fell 9.6% this week",
        "quote_type": "hard_data",
        "standpoint_author_id": "analyst_jones",
        "standpoint_collection_id": "reuters_financial_desk",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://reuters.com/rates-article",
        "title": "Reuters rates",
        "quoted_text": "Interest rates rose to 5.5%.",
        "interpretation": "Rate increase.",
        "claim": "Interest rates rose to 5.5%",
        "quote_type": "hard_data",
        "standpoint_author_id": "analyst_jones",
        "standpoint_collection_id": "reuters_financial_desk",
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    # Derive from both
    dv = client.call("engram_derive", {
        "claim": "Rising rates drove gold lower",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "If gold fell and rates rose, rates drove gold down",
        "reasoning_type": "inductive_causal",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]
    assert len(standpoint_warns) >= 1, f"Expected standpoint warning, got: {warnings}"
    assert any("⚠ uniform" in w for w in standpoint_warns), (
        f"Expected per-axis '⚠ uniform' warning for same-standpoint premises, got: {standpoint_warns}"
    )
    print(f"  PASS: Same standpoint → per-axis ⚠ uniform warning: {standpoint_warns[0][:80]}")


# ---------------------------------------------------------------------------
# Test 3: Different standpoints → "N clusters" warning
# ---------------------------------------------------------------------------

def test_different_standpoints_two_cluster_warning():
    """Two observations with different standpoints produce a 2-clusters warning."""
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://reuters.com/article1",
        "title": "Reuters piece",
        "quoted_text": "Gold fell 9.6% this week.",
        "interpretation": "Gold price drop from Reuters.",
        "claim": "Gold fell 9.6% this week (Reuters)",
        "quote_type": "hard_data",
        "standpoint_author_id": "analyst_jones",
        "standpoint_collection_id": "reuters_desk",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://ft.com/article2",
        "title": "FT piece",
        "quoted_text": "The precious metal lost approximately 10% of its value.",
        "interpretation": "Gold price drop from FT.",
        "claim": "Gold lost approximately 10% this week (FT)",
        "quote_type": "hard_data",
        "standpoint_author_id": "analyst_smith",
        "standpoint_collection_id": "ft_markets_desk",
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    dv = client.call("engram_derive", {
        "claim": "Gold fell roughly 10% this week (cross-source)",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "Reuters and FT independently confirm the decline",
        "reasoning_type": "inductive_generalization",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]
    assert len(standpoint_warns) >= 1, f"Expected standpoint warning, got: {warnings}"
    assert any("2 clusters" in w and "diverse" in w for w in standpoint_warns), (
        f"Expected per-axis '2 clusters (diverse)' warning, got: {standpoint_warns}"
    )
    print(f"  PASS: Different standpoints → per-axis 2-cluster diverse warning: {standpoint_warns[0][:80]}")


# ---------------------------------------------------------------------------
# Test 4: No standpoint data → no standpoint warning
# ---------------------------------------------------------------------------

def test_no_standpoint_data_no_warning():
    """Two observations with no standpoint fields produce STANDPOINT: unchecked (#761).
    Previously this silently skipped; now a low-noise unchecked notice fires
    while the ⚠⚠ composite stays closed (safe degradation preserved)."""
    client = fresh_client()
    _set_self_lineage("")  # disable null=self so cluster_key returns None for all-unmarked

    r1 = client.call("engram_add_observation", {
        "url": "https://reuters.com/article1",
        "title": "Reuters piece",
        "quoted_text": "Gold fell 9.6% this week.",
        "interpretation": "Gold price drop.",
        "claim": "Gold fell 9.6% this week",
        "quote_type": "hard_data",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://ft.com/article2",
        "title": "FT piece",
        "quoted_text": "The precious metal lost approximately 10% of its value.",
        "interpretation": "Gold price drop from FT.",
        "claim": "Gold lost approximately 10% this week",
        "quote_type": "hard_data",
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    dv = client.call("engram_derive", {
        "claim": "Gold fell roughly 10% this week",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "Both sources confirm roughly 10% decline",
        "reasoning_type": "inductive_generalization",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]
    composite_warns = [w for w in warnings if w.startswith("⚠⚠")]
    assert len(standpoint_warns) == 1 and "unchecked" in standpoint_warns[0], (
        f"Expected exactly one STANDPOINT: unchecked warning, got: {standpoint_warns}"
    )
    assert "2/2" in standpoint_warns[0], (
        f"Expected 2/2 proportion in unchecked line: {standpoint_warns[0]}"
    )
    assert len(composite_warns) == 0, (
        f"⚠⚠ composite must stay closed with no standpoint data: {composite_warns}"
    )
    print(f"  PASS: No standpoint data → STANDPOINT: unchecked (2/2 ...), no ⚠⚠")


# ---------------------------------------------------------------------------
# Test 5: Batch observations with per-observation standpoint fields
# ---------------------------------------------------------------------------

def test_batch_with_standpoint_fields():
    """Batch observations pass through per-observation standpoint fields."""
    client = fresh_client()
    obs = [
        {
            "quoted_text": "Revenue grew 15%.",
            "interpretation": "Strong growth.",
            "claim": "Revenue grew 15%",
            "quote_type": "hard_data",
            "standpoint_author_id": "author_alpha",
            "standpoint_collection_id": "corpus_q1",
        },
        {
            "quoted_text": "Costs decreased 5%.",
            "interpretation": "Efficiency gain.",
            "claim": "Costs decreased 5%",
            "quote_type": "hard_data",
            "standpoint_author_id": "author_beta",
            "standpoint_collection_id": "corpus_q2",
            "standpoint_override_tag": "cfo_report_override",
        },
    ]
    r = client.call("engram_add_observation_batch", {
        "url": "https://example.com/report",
        "title": "Quarterly Report",
        "observations_json": json.dumps(obs),
    })
    assert r["status"] == "batch_complete", f"Batch failed: {r}"
    assert r["created"] == 2, f"Expected 2 created, got: {r['created']}"

    # Check first observation's standpoint fields
    ob0_id = r["results"][0]["observation_id"]
    sp0 = _read_node_standpoint(client, ob0_id)
    assert sp0["standpoint_author_id"] == "author_alpha", f"Got: {sp0}"
    assert sp0["standpoint_collection_id"] == "corpus_q1", f"Got: {sp0}"
    assert sp0["standpoint_override_tag"] is None

    # Check second observation's standpoint fields
    ob1_id = r["results"][1]["observation_id"]
    sp1 = _read_node_standpoint(client, ob1_id)
    assert sp1["standpoint_author_id"] == "author_beta", f"Got: {sp1}"
    assert sp1["standpoint_collection_id"] == "corpus_q2", f"Got: {sp1}"
    assert sp1["standpoint_override_tag"] == "cfo_report_override", f"Got: {sp1}"

    print("  PASS: Batch observations pass through standpoint fields")


# ---------------------------------------------------------------------------
# Test 6: Single-premise derivation — STANDPOINT + FALSIFICATION fire (#958)
# ---------------------------------------------------------------------------

def test_single_premise_triggers_standpoint_and_fs():
    """A derivation with only 1 premise now triggers STANDPOINT and FALSIFICATION.

    Before #958 the gate was `if n >= 2`, silently skipping single-premise
    derivations. After the fix the gate is `if n >= 1`, so even a single-premise
    derivation whose observation leaf has standpoint data gets STANDPOINT (trivially
    uniform but technically correct) and FALSIFICATION advisory.
    """
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://example.com/single-source",
        "title": "Single source",
        "quoted_text": "The sky is blue.",
        "interpretation": "Color observation.",
        "claim": "The sky is blue",
        "quote_type": "hard_data",
        "standpoint_author_id": "author_solo",
        "standpoint_collection_id": "corpus_solo",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    dv = client.call("engram_derive", {
        "claim": "The sky has color",
        "supporting_ids": r1["observation_id"],
        "logical_chain": "Blue is a color, sky is blue, so sky has color",
        "reasoning_type": "deductive_modus_ponens",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]
    fs_warns = [w for w in warnings if "FALSIFICATION" in w]
    assert len(standpoint_warns) >= 1, (
        f"Expected STANDPOINT warning for single-premise derivation (n >= 1 gate), "
        f"got none. All warnings: {warnings}"
    )
    assert len(fs_warns) >= 1, (
        f"Expected FALSIFICATION warning for single-premise derivation, "
        f"got none. All warnings: {warnings}"
    )
    print(f"  PASS: Single-premise derivation → STANDPOINT + FALSIFICATION fire. "
          f"standpoint={standpoint_warns[0][:60]!r}")


# ---------------------------------------------------------------------------
# Test 7 (new): Single-premise chain ⚠⚠ escalation fires (#958)
# ---------------------------------------------------------------------------

def test_single_premise_chain_ww_fires():
    """⚠⚠ escalation fires for a single-premise derivation with uniform lineage and zero
    verified leaves.

    Covers the chain case from #958: dv←ob with lineage-uniform + unverified obs
    must escalate to ⚠⚠ now that the n >= 1 gate is in place. Without the #958
    fix, this derivation would have emitted NO advisory; with the fix the ⚠⚠
    composite fires.
    """
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://example.com/chain-base",
        "title": "Chain base observation",
        "quoted_text": "A foundational claim derived from conversation.",
        "interpretation": "Conversational prior — frozen, unverified.",
        "claim": "Foundational claim A",
        "quote_type": "personal_communication",
        "standpoint_lineage": "anthropic:claude",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    dv = client.call("engram_derive", {
        "claim": "Derived claim from chain base",
        "supporting_ids": r1["observation_id"],
        "logical_chain": "The foundational claim implies this derived claim.",
        "reasoning_type": "authority_expert",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    ww_warns = [w for w in warnings if w.startswith("⚠⚠")]
    assert len(ww_warns) >= 1, (
        f"Expected ⚠⚠ escalation for single-premise derivation with lineage-uniform "
        f"unverified leaves, got none. All warnings: {warnings}"
    )
    print(f"  PASS: Single-premise chain ⚠⚠ escalation fires. ww={ww_warns[0][:80]!r}")


# ---------------------------------------------------------------------------
# Test 8 (prev. 7): author-diverse / collection-uniform → ⚠ uniform caught on collection
# ---------------------------------------------------------------------------

def test_author_diverse_collection_uniform_warning():
    """Per-axis v2 key test: different authors, same collection must flag collection as ⚠ uniform.

    This is the counterexample from Ariadne's #756 review that motivated v2:
    - Premise A: (authorX, collectionZ)
    - Premise B: (authorY, collectionZ)
    v1 combined key produced 2 distinct hashes → "2 clusters (diverse)" — FALSE positive.
    v2 per-axis keys must catch: author diverse, collection ⚠ uniform.
    """
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://source-alpha.com/article",
        "title": "Source Alpha",
        "quoted_text": "Metric X increased by 12%.",
        "interpretation": "Growth in X.",
        "claim": "Metric X increased by 12%",
        "quote_type": "hard_data",
        "standpoint_author_id": "author_alpha",
        "standpoint_collection_id": "shared_corpus_Z",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://source-beta.com/article",
        "title": "Source Beta",
        "quoted_text": "Metric X rose by 11.8%.",
        "interpretation": "Growth in X from beta source.",
        "claim": "Metric X rose by 11.8%",
        "quote_type": "hard_data",
        "standpoint_author_id": "author_beta",
        "standpoint_collection_id": "shared_corpus_Z",
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    dv = client.call("engram_derive", {
        "claim": "Metric X grew roughly 12% (two sources)",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "Both sources independently confirm ~12% growth",
        "reasoning_type": "inductive_generalization",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]
    assert len(standpoint_warns) >= 1, f"Expected standpoint warning, got: {warnings}"

    # v2 must report collection as ⚠ uniform (same shared_corpus_Z)
    assert any("collection" in w and "⚠ uniform" in w for w in standpoint_warns), (
        f"Expected 'collection: ... (⚠ uniform)' in warning. Got: {standpoint_warns}"
    )
    # v2 must report author as diverse (different authors)
    assert any("author" in w and "diverse" in w for w in standpoint_warns), (
        f"Expected 'author: ... (diverse)' in warning. Got: {standpoint_warns}"
    )
    print(f"  PASS: Author-diverse/collection-uniform → ⚠ uniform caught: {standpoint_warns[0][:90]}")


# ---------------------------------------------------------------------------
# Test 8: Single-axis premises (author-only) — collection axis omitted
# ---------------------------------------------------------------------------

def test_single_axis_author_only_warning():
    """Premises with only standpoint_author_id report author axis; collection is omitted.

    This exercises the _axis_label({None}) → None → no label path for the collection
    axis, and confirms the author axis still fires correctly.
    """
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://source-one.com/article",
        "title": "Source One — author only",
        "quoted_text": "Value A is 20%.",
        "interpretation": "Measurement of A.",
        "claim": "Value A is 20%",
        "quote_type": "hard_data",
        "standpoint_author_id": "author_one",
        # No standpoint_collection_id set
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://source-two.com/article",
        "title": "Source Two — author only",
        "quoted_text": "Value A is 21%.",
        "interpretation": "Measurement of A from second source.",
        "claim": "Value A is 21%",
        "quote_type": "hard_data",
        "standpoint_author_id": "author_two",
        # No standpoint_collection_id set
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    dv = client.call("engram_derive", {
        "claim": "Value A is approximately 20-21%",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "Both sources independently measure A near 20%",
        "reasoning_type": "inductive_generalization",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]
    assert len(standpoint_warns) >= 1, f"Expected standpoint warning, got: {warnings}"

    # Author axis should be reported (2 distinct authors → diverse)
    assert any("author" in w and "diverse" in w for w in standpoint_warns), (
        f"Expected 'author: ... (diverse)' in warning. Got: {standpoint_warns}"
    )
    # Collection axis should be absent (no data → no label emitted)
    assert not any("collection" in w for w in standpoint_warns), (
        f"Expected no 'collection:' in warning when collection is not set. Got: {standpoint_warns}"
    )
    print(f"  PASS: Author-only premises → author axis reported, collection omitted: {standpoint_warns[0][:90]}")


# ---------------------------------------------------------------------------
# Test 9: Mixed-axis premises — partial axis is omitted (all-or-omit rule)
# ---------------------------------------------------------------------------

def test_mixed_axis_collection_omitted_when_partial():
    """All-or-omit rule: if one premise has collection set and another doesn't,
    the collection axis is omitted entirely — avoids asserting uniformity from
    a single premise's data.

    This is the Borges round-2 blocker case:
    - Premise A: (author_x, collection_z)
    - Premise B: (author_y, no-collection)
    Without the all-or-omit fix, collection would report "1 cluster (⚠ uniform)"
    even though only one premise had collection data — cross-premise uniformity
    asserted that was never actually checked.
    """
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://source-a.com/article",
        "title": "Source A — both axes set",
        "quoted_text": "Result X is 15%.",
        "interpretation": "Measurement with full standpoint.",
        "claim": "Result X is 15%",
        "quote_type": "hard_data",
        "standpoint_author_id": "author_alpha",
        "standpoint_collection_id": "corpus_shared",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://source-b.com/article",
        "title": "Source B — author only",
        "quoted_text": "Result X is 16%.",
        "interpretation": "Measurement with author-only standpoint.",
        "claim": "Result X is 16%",
        "quote_type": "hard_data",
        "standpoint_author_id": "author_beta",
        # No standpoint_collection_id — mixed axis
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    dv = client.call("engram_derive", {
        "claim": "Result X is approximately 15-16%",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "Both sources measure X near 15%",
        "reasoning_type": "inductive_generalization",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]

    # Author axis: both premises have authors (alpha vs beta → diverse) — must fire
    assert any("author" in w and "diverse" in w for w in standpoint_warns), (
        f"Expected 'author: ... (diverse)' in warning. Got: {standpoint_warns}"
    )
    # Collection axis: only one premise has collection data → MUST be omitted
    assert not any("collection" in w for w in standpoint_warns), (
        f"Expected NO 'collection:' in warning (partial data → all-or-omit). Got: {standpoint_warns}"
    )
    print(f"  PASS: Mixed-axis → collection axis omitted (all-or-omit): {standpoint_warns[0][:90]}")


# ---------------------------------------------------------------------------
# Test 10: standpoint_architecture stored and retrievable from DB
# ---------------------------------------------------------------------------

def test_standpoint_architecture_stored():
    """standpoint_architecture is stored in the DB and retrievable."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/arch-test",
        "title": "Architecture test",
        "quoted_text": "The model produced output A.",
        "interpretation": "Transformer model output.",
        "claim": "The model produced output A",
        "quote_type": "hard_data",
        "standpoint_architecture": "transformer",
    })
    assert r.get("status") == "created", f"Expected created, got: {r}"
    node_id = r["observation_id"]

    import server as _server
    conn = sqlite3.connect(str(_server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT standpoint_architecture FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        assert row is not None, "Node not found in DB"
        assert row["standpoint_architecture"] == "transformer", (
            f"Expected 'transformer', got: {row['standpoint_architecture']}"
        )
    finally:
        conn.close()
    print("  PASS: standpoint_architecture stored in DB")


# ---------------------------------------------------------------------------
# Test 11: standpoint_architecture enum validation
# ---------------------------------------------------------------------------

def test_standpoint_architecture_enum_validation():
    """Filing with an invalid standpoint_architecture returns a validation error."""
    client = fresh_client()
    r = client.call("engram_add_observation", {
        "url": "https://example.com/arch-invalid",
        "title": "Invalid arch test",
        "quoted_text": "Some claim here.",
        "interpretation": "Some interpretation.",
        "claim": "Some claim",
        "quote_type": "hard_data",
        "standpoint_architecture": "invalid-architecture-name",
    })
    assert r.get("error") is not None, f"Expected error, got: {r}"
    assert "standpoint_architecture" in r["error"], (
        f"Error should mention standpoint_architecture: {r['error']}"
    )
    assert "invalid-architecture-name" in r["error"], (
        f"Error should mention the bad value: {r['error']}"
    )
    print(f"  PASS: Invalid architecture → validation error: {r['error'][:80]}")


# ---------------------------------------------------------------------------
# Test 12: standpoint_architecture uniform → Class A exposure warning
# ---------------------------------------------------------------------------

def test_standpoint_architecture_uniform_warning():
    """Derivation with all premises tagged same architecture → Class A exposure warning."""
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://example.com/arch-uniform-1",
        "title": "Arch uniform source 1",
        "quoted_text": "Claim alpha from transformer model.",
        "interpretation": "Output from transformer.",
        "claim": "Claim alpha is valid",
        "quote_type": "hard_data",
        "standpoint_architecture": "transformer",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://example.com/arch-uniform-2",
        "title": "Arch uniform source 2",
        "quoted_text": "Claim beta from transformer model.",
        "interpretation": "Output from another transformer.",
        "claim": "Claim beta is valid",
        "quote_type": "hard_data",
        "standpoint_architecture": "transformer",
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    dv = client.call("engram_derive", {
        "claim": "Alpha and beta are jointly valid",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "Both alpha and beta hold independently",
        "reasoning_type": "inductive_causal",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]
    assert len(standpoint_warns) >= 1, f"Expected STANDPOINT warning, got: {warnings}"
    assert any("Class A exposure elevated" in w for w in standpoint_warns), (
        f"Expected 'Class A exposure elevated' in warning, got: {standpoint_warns}"
    )
    assert any("all transformer" in w for w in standpoint_warns), (
        f"Expected 'all transformer' in warning, got: {standpoint_warns}"
    )
    assert any("single-family" in w for w in standpoint_warns), (
        f"Expected 'single-family' in warning, got: {standpoint_warns}"
    )
    print(f"  PASS: All-transformer premises → Class A exposure warning: {standpoint_warns[0][:100]}")


# ---------------------------------------------------------------------------
# Test 13: standpoint_architecture diverse → no Class A exposure warning
# ---------------------------------------------------------------------------

def test_standpoint_architecture_diverse_no_warning():
    """Derivation with premises from different architectures → 'diverse', no Class A warning."""
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://example.com/arch-diverse-1",
        "title": "Arch diverse source 1 — transformer",
        "quoted_text": "Observation from a transformer model.",
        "interpretation": "Transformer output.",
        "claim": "Observation A from transformer",
        "quote_type": "hard_data",
        "standpoint_architecture": "transformer",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://example.com/arch-diverse-2",
        "title": "Arch diverse source 2 — human",
        "quoted_text": "Observation from a human expert.",
        "interpretation": "Human expert observation.",
        "claim": "Observation B from human",
        "quote_type": "personal_communication",
        "standpoint_architecture": "human",
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    dv = client.call("engram_derive", {
        "claim": "Observations A and B jointly support the conclusion",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "AI and human observations corroborate each other",
        "reasoning_type": "inductive_causal",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    standpoint_warns = [w for w in warnings if "STANDPOINT" in w]
    assert len(standpoint_warns) >= 1, f"Expected STANDPOINT warning, got: {warnings}"
    assert any("diverse" in w for w in standpoint_warns), (
        f"Expected 'diverse' in STANDPOINT warning, got: {standpoint_warns}"
    )
    assert not any("Class A exposure elevated" in w for w in standpoint_warns), (
        f"Expected NO 'Class A exposure elevated' for diverse architectures, got: {standpoint_warns}"
    )
    print(f"  PASS: Diverse architectures → 'diverse', no Class A warning: {standpoint_warns[0][:100]}")


def test_standpoint_architecture_partial_data_omit():
    """One premise tagged transformer + one untagged → architecture axis silently omitted (all-or-omit)."""
    client = fresh_client()

    r1 = client.call("engram_add_observation", {
        "url": "https://example.com/arch-partial-1",
        "title": "Arch partial source 1 — tagged transformer",
        "quoted_text": "Observation from a transformer model.",
        "interpretation": "Transformer output.",
        "claim": "Observation A tagged transformer",
        "quote_type": "hard_data",
        "standpoint_architecture": "transformer",
    })
    assert r1.get("status") == "created", f"r1 failed: {r1}"

    r2 = client.call("engram_add_observation", {
        "url": "https://example.com/arch-partial-2",
        "title": "Arch partial source 2 — untagged",
        "quoted_text": "Observation with no architecture tag.",
        "interpretation": "Untagged source.",
        "claim": "Observation B untagged",
        "quote_type": "hard_data",
    })
    assert r2.get("status") == "created", f"r2 failed: {r2}"

    dv = client.call("engram_derive", {
        "claim": "Observations A and B jointly support a conclusion",
        "supporting_ids": f"{r1['observation_id']},{r2['observation_id']}",
        "logical_chain": "Partial architecture data should suppress the architecture axis",
        "reasoning_type": "inductive_causal",
    })
    assert dv.get("status") == "created", f"Derive failed: {dv}"
    warnings = dv.get("structure_warnings", [])
    arch_warns = [w for w in warnings if "architecture" in w.lower()]
    assert not arch_warns, (
        f"Expected no architecture warning when one premise is untagged (all-or-omit), got: {arch_warns}"
    )
    print(f"  PASS: Partial architecture data → axis omitted (all-or-omit). warnings={[w[:80] for w in warnings]}")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_standpoint_columns_written,
        test_standpoint_override_tag_written,
        test_same_standpoint_one_cluster_warning,
        test_different_standpoints_two_cluster_warning,
        test_no_standpoint_data_no_warning,
        test_batch_with_standpoint_fields,
        test_single_premise_triggers_standpoint_and_fs,
        test_single_premise_chain_ww_fires,
        test_author_diverse_collection_uniform_warning,
        test_single_axis_author_only_warning,
        test_mixed_axis_collection_omitted_when_partial,
        test_standpoint_architecture_stored,
        test_standpoint_architecture_enum_validation,
        test_standpoint_architecture_uniform_warning,
        test_standpoint_architecture_diverse_no_warning,
        test_standpoint_architecture_partial_data_omit,
    ]

    print(f"\nRunning {len(tests)} standpoint field tests...\n")
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL: {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if failed:
        sys.exit(1)
    else:
        print("All tests passed!")
