"""Tests for engram_set_recall_summary MCP tool + tools/recall_summary_prompts module.

PR B-1: write path for recall_summary + recall_keywords columns added in PR A.

Coverage:
  Tool tests:
  1.  test_set_recall_summary_happy_path          — sets both fields on a fresh obs node
  2.  test_set_recall_summary_idempotent           — second call with different values overwrites
  3.  test_set_recall_summary_keywords_round_trip  — 4-element list survives JSON round-trip
  4.  test_set_recall_summary_validates_node_exists — nonexistent node_id → error
  5.  test_set_recall_summary_validates_node_is_current — superseded node → error
  6.  test_set_recall_summary_validates_summary_length  — >200 → err with hard_cap+got_length structured fields; 200 → ok; 155 typical → ok; empty → err
  7.  test_set_recall_summary_validates_keyword_count   — <3 → err; 3 → ok; 5 → ok; 6 → err
  8.  test_set_recall_summary_validates_keyword_strings — non-str → err; empty → err; >30 → err
  9.  test_set_recall_summary_no_side_effects       — importance/utility/recall_turn unchanged

  Prompts module tests:
  10. test_build_prompt_per_type_guidance    — each TYPE_GUIDANCE key → type guidance in prompt
  11. test_build_prompt_unknown_type_falls_back — unknown type → DEFAULT_GUIDANCE in prompt
  12. test_build_prompt_includes_claim_text  — claim text appears verbatim in prompt
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Project-root on path
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Server sandbox helpers (same pattern as test_special_type_bypass.py)
# ---------------------------------------------------------------------------

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_recall_summary_writes")


def fresh_server():
    """Create a clean server module instance in a temp sandbox directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for mod in list(sys.modules.keys()):
        if mod == "server" or mod.startswith("server."):
            del sys.modules[mod]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def fresh_conn(srv):
    return srv._get_db()


def _add_obs(srv, claim: str, quoted_text: str = "test quote") -> str:
    payload = {
        "quoted_text": quoted_text,
        "interpretation": "test interpretation",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/test",
        "title": "Test",
    }
    r = json.loads(srv.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def _make_payload(node_id, summary=None, keywords=None):
    """Build a minimal valid set_recall_summary payload."""
    if summary is None:
        summary = "A short curated summary for this node."
    if keywords is None:
        keywords = ["keyword-one", "keyword-two", "keyword-three"]
    return {
        "node_id": node_id,
        "recall_summary": summary,
        "recall_keywords": keywords,
    }


# ---------------------------------------------------------------------------
# Test 1: Happy path
# ---------------------------------------------------------------------------


def test_set_recall_summary_happy_path():
    """Call tool on a fresh obs node; verify DB row gets both fields."""
    srv = fresh_server()
    obs_id = _add_obs(srv, "happy path recall summary test claim")

    payload = _make_payload(
        obs_id,
        summary="Happy path: obs node gets recall fields set correctly.",
        keywords=["happy-path", "recall", "writes"],
    )
    r = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "set", r
    assert r.get("node_id") == obs_id, r

    # Verify DB state directly
    conn = fresh_conn(srv)
    try:
        row = conn.execute(
            "SELECT recall_summary, recall_keywords FROM nodes WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row is not None, f"Node {obs_id} not found in DB"
        assert row["recall_summary"] == payload["recall_summary"], (
            f"recall_summary mismatch: {row['recall_summary']!r}"
        )
        kw_list = json.loads(row["recall_keywords"])
        assert kw_list == payload["recall_keywords"], (
            f"recall_keywords mismatch: {kw_list!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 2: Idempotent — second call overwrites
# ---------------------------------------------------------------------------


def test_set_recall_summary_idempotent():
    """Calling the tool twice with different values — second call wins."""
    srv = fresh_server()
    obs_id = _add_obs(srv, "idempotent recall summary test claim")

    first_payload = _make_payload(
        obs_id,
        summary="First summary written to this node.",
        keywords=["first", "summary", "test"],
    )
    r1 = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(first_payload)))
    assert "error" not in r1, r1

    second_payload = _make_payload(
        obs_id,
        summary="Second summary that should overwrite the first.",
        keywords=["second", "overwrite", "idempotent"],
    )
    r2 = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(second_payload)))
    assert "error" not in r2, r2
    assert r2.get("status") == "set", r2

    # Second call's values must win
    conn = fresh_conn(srv)
    try:
        row = conn.execute(
            "SELECT recall_summary, recall_keywords FROM nodes WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row["recall_summary"] == second_payload["recall_summary"], (
            f"Expected second summary to overwrite: {row['recall_summary']!r}"
        )
        kw_list = json.loads(row["recall_keywords"])
        assert kw_list == second_payload["recall_keywords"], (
            f"Expected second keywords to overwrite: {kw_list!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 3: Keywords round-trip (4-element list)
# ---------------------------------------------------------------------------


def test_set_recall_summary_keywords_round_trip():
    """Set 4-keyword list, read back via direct SQL, JSON-decode, assert equal."""
    srv = fresh_server()
    obs_id = _add_obs(srv, "keywords round trip recall summary test claim")

    four_keywords = ["alpha", "beta", "gamma", "delta"]
    payload = _make_payload(obs_id, keywords=four_keywords)
    r = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload)))
    assert "error" not in r, r

    conn = fresh_conn(srv)
    try:
        row = conn.execute(
            "SELECT recall_keywords FROM nodes WHERE id = ?",
            (obs_id,),
        ).fetchone()
        raw = row["recall_keywords"]
        # Must be a JSON string, not a Python list, in the DB
        assert isinstance(raw, str), f"Expected JSON string in DB, got {type(raw)}: {raw!r}"
        decoded = json.loads(raw)
        assert decoded == four_keywords, (
            f"Round-trip failed: {decoded!r} != {four_keywords!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 4: Validation — node_id does not exist
# ---------------------------------------------------------------------------


def test_set_recall_summary_validates_node_exists():
    """Call on nonexistent node_id → error response, no exception."""
    srv = fresh_server()
    payload = _make_payload("ob_9999999_does_not_exist")
    r = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload)))
    assert "error" in r, f"Expected error for nonexistent node, got: {r}"
    assert "not found" in r["error"].lower() or "not current" in r["error"].lower(), (
        f"Expected 'not found or not current' message, got: {r['error']!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: Validation — node exists but is_current = 0 (superseded)
# ---------------------------------------------------------------------------


def test_set_recall_summary_validates_node_is_current():
    """Supersede a node, then try to set summary → error."""
    srv = fresh_server()

    # Create two observations, then supersede the first with the second
    old_id = _add_obs(srv, "original claim for supersede test")
    new_id = _add_obs(srv, "revised claim that supersedes the original")

    sup_payload = {
        "old_node_id": old_id,
        "new_node_id": new_id,
        "supersede_reason": "Revised after additional evidence.",
    }
    sup_r = json.loads(srv.engram_supersede(payload_json=json.dumps(sup_payload)))
    assert "error" not in sup_r, f"Supersede failed: {sup_r}"

    # Verify old node is now is_current=0
    conn = fresh_conn(srv)
    try:
        row = conn.execute(
            "SELECT is_current FROM nodes WHERE id = ?", (old_id,)
        ).fetchone()
        assert row["is_current"] == 0, f"Expected is_current=0 after supersede, got {row['is_current']}"
    finally:
        conn.close()

    # Now try to set recall summary on the superseded node
    payload = _make_payload(old_id)
    r = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload)))
    assert "error" in r, f"Expected error for superseded node, got: {r}"
    assert "not found" in r["error"].lower() or "not current" in r["error"].lower(), (
        f"Expected 'not found or not current' message, got: {r['error']!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: Validation — summary length
# ---------------------------------------------------------------------------


def test_set_recall_summary_validates_summary_length():
    """len > 200 → error; len = 200 → success; empty string → error.

    Hard cap = 200 (raised from 140 on 2026-05-19 PM after per-type validation
    showed Sonnet legitimately pushes to ~155 on dense load-bearing claims;
    200 leaves headroom while still guarding against truly runaway output).
    """
    srv = fresh_server()
    obs_id = _add_obs(srv, "summary length validation test claim")

    # Empty string → error
    payload_empty = _make_payload(obs_id, summary="")
    r_empty = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_empty)))
    assert "error" in r_empty, f"Expected error for empty summary, got: {r_empty}"

    # len = 201 → error
    summary_201 = "A" * 201
    payload_too_long = _make_payload(obs_id, summary=summary_201)
    r_too_long = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_too_long)))
    assert "error" in r_too_long, (
        f"Expected error for 201-char summary, got: {r_too_long}"
    )
    assert "200" in r_too_long["error"], (
        f"Expected '200' (the hard cap) in error message, got: {r_too_long['error']!r}"
    )
    # Error response includes structured cap info for caller's runtime decision
    assert r_too_long.get("hard_cap") == 200, (
        f"Expected hard_cap=200 in error response, got: {r_too_long}"
    )
    assert r_too_long.get("got_length") == 201, (
        f"Expected got_length=201 in error response, got: {r_too_long}"
    )

    # len = 200 → success (at the hard cap boundary)
    summary_200 = "B" * 200
    payload_at_cap = _make_payload(obs_id, summary=summary_200)
    r_at_cap = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_at_cap)))
    assert "error" not in r_at_cap, (
        f"Expected success for 200-char summary (at hard cap), got: {r_at_cap}"
    )
    assert r_at_cap.get("status") == "set", r_at_cap

    # len = 155 (typical Sonnet-dense output) → success well within cap
    summary_155 = "C" * 155
    payload_typical = _make_payload(obs_id, summary=summary_155)
    r_typical = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_typical)))
    assert r_typical.get("status") == "set", (
        f"Expected success for 155-char typical-Sonnet output, got: {r_typical}"
    )


# ---------------------------------------------------------------------------
# Test 7: Validation — keyword count
# ---------------------------------------------------------------------------


def test_set_recall_summary_validates_keyword_count():
    """< 3 → error; 3 → success; 5 → success; 6 → error."""
    srv = fresh_server()
    obs_id = _add_obs(srv, "keyword count validation test claim")

    # 2 keywords → error
    payload_2 = _make_payload(obs_id, keywords=["one", "two"])
    r_2 = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_2)))
    assert "error" in r_2, f"Expected error for 2 keywords, got: {r_2}"
    assert "3" in r_2["error"] and "5" in r_2["error"], (
        f"Expected '3' and '5' in error message, got: {r_2['error']!r}"
    )

    # 3 keywords → success
    payload_3 = _make_payload(obs_id, keywords=["one", "two", "three"])
    r_3 = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_3)))
    assert "error" not in r_3, f"Expected success for 3 keywords, got: {r_3}"
    assert r_3.get("status") == "set", r_3

    # 5 keywords → success
    payload_5 = _make_payload(obs_id, keywords=["one", "two", "three", "four", "five"])
    r_5 = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_5)))
    assert "error" not in r_5, f"Expected success for 5 keywords, got: {r_5}"
    assert r_5.get("status") == "set", r_5

    # 6 keywords → error
    payload_6 = _make_payload(obs_id, keywords=["one", "two", "three", "four", "five", "six"])
    r_6 = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_6)))
    assert "error" in r_6, f"Expected error for 6 keywords, got: {r_6}"


# ---------------------------------------------------------------------------
# Test 8: Validation — keyword element constraints
# ---------------------------------------------------------------------------


def test_set_recall_summary_validates_keyword_strings():
    """Non-string element → error; empty-string element → error; >30 chars → error."""
    srv = fresh_server()
    obs_id = _add_obs(srv, "keyword element validation test claim")

    # Non-string element (integer) → error
    payload_nonstr = {
        "node_id": obs_id,
        "recall_summary": "Valid summary for testing keyword validation.",
        "recall_keywords": ["valid", 42, "also-valid"],
    }
    r_nonstr = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_nonstr)))
    assert "error" in r_nonstr, f"Expected error for non-string keyword, got: {r_nonstr}"
    assert "string" in r_nonstr["error"].lower(), (
        f"Expected 'string' in error message, got: {r_nonstr['error']!r}"
    )

    # Empty string element → error
    payload_empty_kw = _make_payload(obs_id, keywords=["valid", "", "also-valid"])
    r_empty_kw = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_empty_kw)))
    assert "error" in r_empty_kw, f"Expected error for empty keyword, got: {r_empty_kw}"
    assert "empty" in r_empty_kw["error"].lower(), (
        f"Expected 'empty' in error message, got: {r_empty_kw['error']!r}"
    )

    # Keyword > 30 chars → error
    long_kw = "a" * 31
    payload_long_kw = _make_payload(obs_id, keywords=["valid", long_kw, "also-valid"])
    r_long_kw = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_long_kw)))
    assert "error" in r_long_kw, f"Expected error for >30 char keyword, got: {r_long_kw}"
    assert "30" in r_long_kw["error"], (
        f"Expected '30' in error message, got: {r_long_kw['error']!r}"
    )

    # Valid 30-char keyword → success
    exactly_30 = "b" * 30
    payload_30 = _make_payload(obs_id, keywords=["valid", exactly_30, "also-valid"])
    r_30 = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload_30)))
    assert "error" not in r_30, f"Expected success for 30-char keyword (at limit), got: {r_30}"
    assert r_30.get("status") == "set", r_30


# ---------------------------------------------------------------------------
# Test 9: No side effects on other node fields
# ---------------------------------------------------------------------------


def test_set_recall_summary_no_side_effects():
    """importance_score, utility_score, recall_count unchanged after set."""
    srv = fresh_server()
    obs_id = _add_obs(srv, "no side effects recall summary test claim")

    # Read baseline values before the call
    conn = fresh_conn(srv)
    try:
        before = conn.execute(
            "SELECT importance_score, utility_score, recall_count FROM nodes WHERE id = ?",
            (obs_id,),
        ).fetchone()
        importance_before = before["importance_score"]
        utility_before = before["utility_score"]
        recall_turn_before = before["recall_count"]
    finally:
        conn.close()

    # Set the recall summary
    payload = _make_payload(obs_id)
    r = json.loads(srv.engram_set_recall_summary(payload_json=json.dumps(payload)))
    assert "error" not in r, r

    # Verify no change to other fields
    conn = fresh_conn(srv)
    try:
        after = conn.execute(
            "SELECT importance_score, utility_score, recall_count FROM nodes WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert after["importance_score"] == importance_before, (
            f"importance_score changed: {importance_before!r} → {after['importance_score']!r}"
        )
        assert after["utility_score"] == utility_before, (
            f"utility_score changed: {utility_before!r} → {after['utility_score']!r}"
        )
        assert after["recall_count"] == recall_turn_before, (
            f"recall_count changed: {recall_turn_before!r} → {after['recall_count']!r}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prompts module tests
# ---------------------------------------------------------------------------

def _load_prompts():
    """Import tools/recall_summary_prompts from the project root."""
    import importlib.util
    mod_path = os.path.join(PROJECT_ROOT, "tools", "recall_summary_prompts.py")
    spec = importlib.util.spec_from_file_location("recall_summary_prompts", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Test 10: Per-type guidance in prompt
def test_build_prompt_per_type_guidance():
    """Each type in TYPE_GUIDANCE produces a prompt containing the type-specific guidance."""
    prompts = _load_prompts()
    for node_type, guidance_text in prompts.TYPE_GUIDANCE.items():
        prompt = prompts.build_prompt(node_type, "A test claim for this node type.")
        # The guidance text may be split across the PROMPT_TEMPLATE substitution;
        # check that at least the first 40 chars of the guidance appear verbatim.
        sample = guidance_text[:40]
        assert sample in prompt, (
            f"Type-specific guidance not found in prompt for type={node_type!r}.\n"
            f"Expected to find: {sample!r}\n"
            f"Prompt snippet: {prompt[:300]!r}"
        )


# Test 11: Unknown type falls back to DEFAULT_GUIDANCE
def test_build_prompt_unknown_type_falls_back():
    """Passing an unknown node type uses DEFAULT_GUIDANCE text."""
    prompts = _load_prompts()
    unknown_type = "random_unknown_type_that_is_not_in_type_guidance"
    prompt = prompts.build_prompt(unknown_type, "A claim for an unknown type.")
    # DEFAULT_GUIDANCE first 40 chars must appear in the prompt
    sample = prompts.DEFAULT_GUIDANCE[:40]
    assert sample in prompt, (
        f"DEFAULT_GUIDANCE not found in prompt for unknown type.\n"
        f"Expected to find: {sample!r}\n"
        f"Prompt snippet: {prompt[:400]!r}"
    )
    # The unknown type name itself should appear (in the Type: line)
    assert unknown_type in prompt, (
        f"Unknown type name not in prompt: {unknown_type!r}"
    )


# Test 12: Claim text appears verbatim
def test_build_prompt_includes_claim_text():
    """The claim text passed to build_prompt appears verbatim in the output."""
    prompts = _load_prompts()
    distinctive_claim = (
        "ENGRAM FTS_SIM_FLOOR=0.30 threshold was calibrated on 2026-05-19 "
        "pilot with 10 nodes including ob_1954 and ax_0010."
    )
    prompt = prompts.build_prompt("observation_factual", distinctive_claim)
    assert distinctive_claim in prompt, (
        f"Claim text not found verbatim in prompt.\n"
        f"Claim: {distinctive_claim!r}\n"
        f"Prompt snippet: {prompt[:500]!r}"
    )
