"""Tests for engram_set_recall_summaries (batch) MCP tool, recall_summary_validator,
and PayloadBuilder.

PR B-1.5: collapse singular engram_set_recall_summary into batch tool + extract
validator into tools/recall_summary_validator.py.

Coverage:

Batch tool tests (engram_set_recall_summaries):
  1.  test_batch_happy_path                — 3 valid entries → applied=3, failed=0
  2.  test_batch_mixed_valid_and_overcap   — 2 valid + 1 over-cap → applied=2, failed=1,
                                             error has hard_cap+got_length
  3.  test_batch_all_invalid              — 3 wrong-keyword-counts → applied=0, failed=3
  4.  test_batch_empty_list               — summaries=[] → applied=0, failed=0
  5.  test_batch_bad_json                 — unparseable JSON → top-level error field
  6.  test_batch_missing_summaries_key    — no "summaries" key → top-level error
  7.  test_batch_node_not_found           — one entry for nonexistent node → that
                                             entry errors, others apply
  8.  test_batch_idempotent               — second call overwrites first
  9.  test_batch_superseded_node          — superseded node errors with distinct message;
                                             valid current node applies (applied=1, failed=1)
  10. test_batch_passes_through_pre_flight_failures — failures key passes through as
                                             pre_flight_failures; missing key → empty list

Validator unit tests (tools.recall_summary_validator.validate_summary_entry):
  9.  test_validator_valid_entry           — all-good entry → None
  10. test_validator_missing_fields        — missing node_id/recall_summary/recall_keywords
  11. test_validator_summary_empty         — empty string → error
  12. test_validator_summary_overcap       — len > 200 → hard_cap + got_length in error
  13. test_validator_summary_at_cap        — len = 200 → None (valid)
  14. test_validator_keyword_count_min     — 2 keywords → error with got_count + min + max
  15. test_validator_keyword_count_max     — 6 keywords → error
  16. test_validator_keyword_count_exact   — 3 keywords → None; 5 keywords → None
  17. test_validator_keyword_not_str       — int element → error
  18. test_validator_keyword_empty         — empty-string element → error
  19. test_validator_keyword_too_long      — > 30 chars → error
  20. test_validator_keyword_at_limit      — 30 chars → None
  21. test_validator_duplicate_keywords    — duplicate keyword → error

PayloadBuilder unit tests (tools.recall_summary_payload.PayloadBuilder):
  22. test_builder_add_summary_happy       — add 2 valid entries; to_json has both
  23. test_builder_add_summary_rejected    — invalid entry → error returned, not added
  24. test_builder_add_failure             — add_failure → appears in "failures" key
  25. test_builder_to_json_shape           — to_json output has both "summaries" + "failures" keys

Prompts module tests (carried over from PR #223):
  26. test_build_prompt_per_type_guidance  — type-specific guidance in prompt
  27. test_build_prompt_unknown_type_falls_back — DEFAULT_GUIDANCE used for unknown type
  28. test_build_prompt_includes_claim_text — claim text verbatim in prompt
"""

from __future__ import annotations

import atexit
import importlib.util
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

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_recall_summary_writes_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


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


def _valid_entry(node_id: str, summary: str = "A short curated summary for this node.",
                  keywords: list[str] | None = None) -> dict:
    if keywords is None:
        keywords = ["keyword-one", "keyword-two", "keyword-three"]
    return {"node_id": node_id, "recall_summary": summary, "recall_keywords": keywords}


# ===========================================================================
# Section 1 — Batch tool tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Test 1: Happy path — 3 valid entries
# ---------------------------------------------------------------------------

def test_batch_happy_path():
    """3 valid entries → applied=3, failed=0, ok has all 3 node_ids."""
    srv = fresh_server()
    ids = [_add_obs(srv, f"batch happy path claim {i}") for i in range(3)]
    summaries = [_valid_entry(id_, f"Summary for node {i}.", ["a", "b", "c"])
                 for i, id_ in enumerate(ids)]
    r = json.loads(srv.engram_set_recall_summaries(
        payload_json=json.dumps({"summaries": summaries})
    ))
    assert "error" not in r, r
    assert r["applied"] == 3, r
    assert r["failed"] == 0, r
    assert len(r["ok"]) == 3, r
    assert len(r["errors"]) == 0, r
    ok_ids = {entry["node_id"] for entry in r["ok"]}
    assert ok_ids == set(ids), f"ok IDs mismatch: {ok_ids} vs {set(ids)}"

    # Verify DB state for one entry
    conn = fresh_conn(srv)
    try:
        row = conn.execute(
            "SELECT recall_summary, recall_keywords FROM nodes WHERE id = ?",
            (ids[0],),
        ).fetchone()
        assert row["recall_summary"] == summaries[0]["recall_summary"]
        assert json.loads(row["recall_keywords"]) == summaries[0]["recall_keywords"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 2: Mixed — 2 valid + 1 over-cap
# ---------------------------------------------------------------------------

def test_batch_mixed_valid_and_overcap():
    """2 valid + 1 over-cap → applied=2, failed=1, error has hard_cap+got_length."""
    srv = fresh_server()
    ids = [_add_obs(srv, f"batch mixed claim {i}") for i in range(3)]
    overcap_summary = "X" * 201
    summaries = [
        _valid_entry(ids[0], "First valid summary."),
        _valid_entry(ids[1], "Second valid summary."),
        _valid_entry(ids[2], overcap_summary),
    ]
    r = json.loads(srv.engram_set_recall_summaries(
        payload_json=json.dumps({"summaries": summaries})
    ))
    assert r["applied"] == 2, r
    assert r["failed"] == 1, r
    assert len(r["ok"]) == 2, r
    assert len(r["errors"]) == 1, r

    err_entry = r["errors"][0]
    assert err_entry["node_id"] == ids[2], err_entry
    assert "hard_cap" in err_entry, err_entry
    assert err_entry["hard_cap"] == 200, err_entry
    assert "got_length" in err_entry, err_entry
    assert err_entry["got_length"] == 201, err_entry


# ---------------------------------------------------------------------------
# Test 3: All invalid
# ---------------------------------------------------------------------------

def test_batch_all_invalid():
    """3 entries with wrong keyword counts → applied=0, failed=3."""
    srv = fresh_server()
    ids = [_add_obs(srv, f"batch all invalid claim {i}") for i in range(3)]
    summaries = [
        # 1 keyword (below min=3)
        {"node_id": ids[0], "recall_summary": "Valid summary.", "recall_keywords": ["one"]},
        # 6 keywords (above max=5)
        {"node_id": ids[1], "recall_summary": "Valid summary.", "recall_keywords": ["a", "b", "c", "d", "e", "f"]},
        # 0 keywords
        {"node_id": ids[2], "recall_summary": "Valid summary.", "recall_keywords": []},
    ]
    r = json.loads(srv.engram_set_recall_summaries(
        payload_json=json.dumps({"summaries": summaries})
    ))
    assert r["applied"] == 0, r
    assert r["failed"] == 3, r
    assert len(r["ok"]) == 0, r
    assert len(r["errors"]) == 3, r


# ---------------------------------------------------------------------------
# Test 4: Empty list
# ---------------------------------------------------------------------------

def test_batch_empty_list():
    """summaries=[] → applied=0, failed=0, no error."""
    srv = fresh_server()
    r = json.loads(srv.engram_set_recall_summaries(
        payload_json=json.dumps({"summaries": []})
    ))
    assert "error" not in r, r
    assert r["applied"] == 0, r
    assert r["failed"] == 0, r


# ---------------------------------------------------------------------------
# Test 5: Bad JSON payload
# ---------------------------------------------------------------------------

def test_batch_bad_json():
    """Unparseable payload_json → top-level error field, no partial writes."""
    srv = fresh_server()
    r = json.loads(srv.engram_set_recall_summaries(payload_json="{not valid json"))
    assert "error" in r, r
    assert "invalid JSON" in r["error"].lower() or "json" in r["error"].lower(), r


# ---------------------------------------------------------------------------
# Test 6: Missing "summaries" key
# ---------------------------------------------------------------------------

def test_batch_missing_summaries_key():
    """Payload without summaries key → top-level error."""
    srv = fresh_server()
    r = json.loads(srv.engram_set_recall_summaries(
        payload_json=json.dumps({"nodes": []})
    ))
    assert "error" in r, r
    assert "summaries" in r["error"], r


# ---------------------------------------------------------------------------
# Test 7: Node not in DB
# ---------------------------------------------------------------------------

def test_batch_node_not_found():
    """One entry for nonexistent node_id → that entry errors, other valid ones apply."""
    srv = fresh_server()
    real_id = _add_obs(srv, "batch node-not-found test real node")
    fake_id = "ob_9999999_does_not_exist"
    summaries = [
        _valid_entry(real_id, "Real node summary."),
        _valid_entry(fake_id, "Fake node summary."),
    ]
    r = json.loads(srv.engram_set_recall_summaries(
        payload_json=json.dumps({"summaries": summaries})
    ))
    assert r["applied"] == 1, r
    assert r["failed"] == 1, r
    assert r["ok"][0]["node_id"] == real_id, r

    err_entry = r["errors"][0]
    assert err_entry["node_id"] == fake_id, err_entry
    assert "not found" in err_entry["error"].lower(), err_entry


# ---------------------------------------------------------------------------
# Test 8: Idempotent — second call overwrites
# ---------------------------------------------------------------------------

def test_batch_idempotent():
    """Second call with different values on same node_id — second call wins."""
    srv = fresh_server()
    obs_id = _add_obs(srv, "batch idempotent test node")

    first = [_valid_entry(obs_id, "First summary.", ["first", "pass", "test"])]
    r1 = json.loads(srv.engram_set_recall_summaries(payload_json=json.dumps({"summaries": first})))
    assert r1["applied"] == 1, r1

    second = [_valid_entry(obs_id, "Second summary overwrites.", ["second", "overwrite", "test"])]
    r2 = json.loads(srv.engram_set_recall_summaries(payload_json=json.dumps({"summaries": second})))
    assert r2["applied"] == 1, r2

    conn = fresh_conn(srv)
    try:
        row = conn.execute(
            "SELECT recall_summary, recall_keywords FROM nodes WHERE id = ?",
            (obs_id,),
        ).fetchone()
        assert row["recall_summary"] == "Second summary overwrites."
        assert json.loads(row["recall_keywords"]) == ["second", "overwrite", "test"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 9 (batch): Superseded node — distinct error from missing node
# ---------------------------------------------------------------------------

def test_batch_superseded_node():
    """Superseded node (is_current=0) errors with distinct message; valid node applies."""
    srv = fresh_server()

    # Create the node to be superseded + a replacement
    old_id = _add_obs(srv, "original claim to be superseded")
    new_id = _add_obs(srv, "refined replacement claim")

    # Supersede old_id with new_id
    sup_r = json.loads(srv.engram_supersede(payload_json=json.dumps({
        "old_node_id": old_id,
        "new_node_id": new_id,
        "supersede_reason": "Refined in test.",
    })))
    assert "error" not in sup_r, sup_r

    # Add a third node that remains current
    current_id = _add_obs(srv, "current node not superseded")

    summaries = [
        _valid_entry(old_id, "Summary for superseded node."),
        _valid_entry(current_id, "Summary for current node."),
    ]
    r = json.loads(srv.engram_set_recall_summaries(
        payload_json=json.dumps({"summaries": summaries})
    ))
    assert r["applied"] == 1, r
    assert r["failed"] == 1, r
    assert len(r["ok"]) == 1, r
    assert r["ok"][0]["node_id"] == current_id, r

    assert len(r["errors"]) == 1, r
    err_entry = r["errors"][0]
    assert err_entry["node_id"] == old_id, err_entry
    assert err_entry["error"] == "node is not current (superseded)", err_entry


# ---------------------------------------------------------------------------
# Test 10 (batch): pre_flight_failures passthrough
# ---------------------------------------------------------------------------

def test_batch_passes_through_pre_flight_failures():
    """failures key in payload passes through verbatim as pre_flight_failures in response."""
    srv = fresh_server()
    obs_id = _add_obs(srv, "node for pre-flight failure passthrough test")

    # Payload with one valid summary + one pre-flight failure
    pre_flight = [{"node_id": "ob_9999", "reason": "fairy couldn't generate"}]
    payload = {
        "summaries": [_valid_entry(obs_id, "Valid summary for passthrough test.")],
        "failures": pre_flight,
    }
    r = json.loads(srv.engram_set_recall_summaries(payload_json=json.dumps(payload)))
    assert r["applied"] == 1, r
    assert r["failed"] == 0, r
    assert "pre_flight_failures" in r, r
    assert r["pre_flight_failures"] == pre_flight, r

    # Missing failures key → pre_flight_failures is empty list
    payload_no_failures = {
        "summaries": [_valid_entry(obs_id, "Updated summary for passthrough test.")],
    }
    r2 = json.loads(srv.engram_set_recall_summaries(payload_json=json.dumps(payload_no_failures)))
    assert "pre_flight_failures" in r2, r2
    assert r2["pre_flight_failures"] == [], r2


# ===========================================================================
# Section 2 — Validator unit tests
# ===========================================================================

from tools.recall_summary_validator import validate_summary_entry  # noqa: E402


def _valid_dict(node_id: str = "ob_0001",
                summary: str = "A valid summary.",
                keywords: list[str] | None = None) -> dict:
    if keywords is None:
        keywords = ["kw-one", "kw-two", "kw-three"]
    return {"node_id": node_id, "recall_summary": summary, "recall_keywords": keywords}


# ---------------------------------------------------------------------------
# Test 9: Valid entry → None
# ---------------------------------------------------------------------------

def test_validator_valid_entry():
    assert validate_summary_entry(_valid_dict()) is None


# ---------------------------------------------------------------------------
# Test 10: Missing fields
# ---------------------------------------------------------------------------

def test_validator_missing_fields():
    """Missing any required field → error with that field name."""
    for missing_field in ("node_id", "recall_summary", "recall_keywords"):
        entry = _valid_dict()
        del entry[missing_field]
        err = validate_summary_entry(entry)
        assert err is not None, f"Expected error for missing {missing_field}"
        assert "field" in err, err
        assert err["field"] == missing_field, (
            f"Expected field={missing_field!r}, got: {err}"
        )


# ---------------------------------------------------------------------------
# Test 11: recall_summary empty string
# ---------------------------------------------------------------------------

def test_validator_summary_empty():
    entry = _valid_dict(summary="")
    err = validate_summary_entry(entry)
    assert err is not None
    assert "recall_summary" in err.get("error", "") or err.get("field") == "recall_summary"


# ---------------------------------------------------------------------------
# Test 12: recall_summary over hard cap
# ---------------------------------------------------------------------------

def test_validator_summary_overcap():
    """len > 200 → error with hard_cap=200 and got_length=N."""
    entry = _valid_dict(summary="A" * 201)
    err = validate_summary_entry(entry)
    assert err is not None
    assert err.get("hard_cap") == 200, err
    assert err.get("got_length") == 201, err
    assert err.get("field") == "recall_summary", err


# ---------------------------------------------------------------------------
# Test 13: recall_summary exactly at cap → valid
# ---------------------------------------------------------------------------

def test_validator_summary_at_cap():
    entry = _valid_dict(summary="B" * 200)
    assert validate_summary_entry(entry) is None


# ---------------------------------------------------------------------------
# Test 14: keyword count below min
# ---------------------------------------------------------------------------

def test_validator_keyword_count_min():
    """2 keywords → error with got_count, min, max."""
    entry = _valid_dict(keywords=["one", "two"])
    err = validate_summary_entry(entry)
    assert err is not None
    assert err.get("got_count") == 2, err
    assert err.get("min") == 3, err
    assert err.get("max") == 5, err
    assert err.get("field") == "recall_keywords", err


# ---------------------------------------------------------------------------
# Test 15: keyword count above max
# ---------------------------------------------------------------------------

def test_validator_keyword_count_max():
    """6 keywords → error."""
    entry = _valid_dict(keywords=["a", "b", "c", "d", "e", "f"])
    err = validate_summary_entry(entry)
    assert err is not None
    assert err.get("field") == "recall_keywords", err


# ---------------------------------------------------------------------------
# Test 16: keyword count exact boundaries → valid
# ---------------------------------------------------------------------------

def test_validator_keyword_count_exact():
    assert validate_summary_entry(_valid_dict(keywords=["a", "b", "c"])) is None
    assert validate_summary_entry(_valid_dict(keywords=["a", "b", "c", "d", "e"])) is None


# ---------------------------------------------------------------------------
# Test 17: keyword element not a string
# ---------------------------------------------------------------------------

def test_validator_keyword_not_str():
    entry = _valid_dict(keywords=["valid", 42, "also-valid"])
    err = validate_summary_entry(entry)
    assert err is not None
    assert "string" in err.get("error", "").lower(), err


# ---------------------------------------------------------------------------
# Test 18: keyword element empty string
# ---------------------------------------------------------------------------

def test_validator_keyword_empty():
    entry = _valid_dict(keywords=["valid", "", "also-valid"])
    err = validate_summary_entry(entry)
    assert err is not None
    assert "empty" in err.get("error", "").lower(), err


# ---------------------------------------------------------------------------
# Test 19: keyword element too long
# ---------------------------------------------------------------------------

def test_validator_keyword_too_long():
    """keyword > 30 chars → error."""
    long_kw = "x" * 31
    entry = _valid_dict(keywords=["valid", long_kw, "also-valid"])
    err = validate_summary_entry(entry)
    assert err is not None
    assert "30" in err.get("error", ""), err


# ---------------------------------------------------------------------------
# Test 20: keyword element exactly at limit → valid
# ---------------------------------------------------------------------------

def test_validator_keyword_at_limit():
    exactly_30 = "y" * 30
    entry = _valid_dict(keywords=["valid", exactly_30, "also-valid"])
    assert validate_summary_entry(entry) is None


# ---------------------------------------------------------------------------
# Test 21: duplicate keywords
# ---------------------------------------------------------------------------

def test_validator_duplicate_keywords():
    """Duplicate keyword (case-sensitive) → error."""
    entry = _valid_dict(keywords=["alpha", "beta", "alpha"])
    err = validate_summary_entry(entry)
    assert err is not None
    assert "duplicate" in err.get("error", "").lower(), err
    assert err.get("field") == "recall_keywords", err

    # Case-sensitive: "Alpha" and "alpha" are different, should be valid
    entry_case = _valid_dict(keywords=["Alpha", "alpha", "gamma"])
    assert validate_summary_entry(entry_case) is None, (
        "Expected case-sensitive duplicate check to pass for 'Alpha' vs 'alpha'"
    )


def test_validator_rejects_node_id_as_keyword():
    """ENGRAM node ID patterns (e.g. dv_0500, ob_1234, fl_0148) → error.

    IDs carry no recognition signal in the auto-surface skim — the keyword
    slot is for content recognition. Graph navigation lives in edges and
    is accessed via engram_inspect. Disposition rule from KEYWORD_GUIDANCE
    in tools/recall_summary_prompts.py, structurally enforced here.
    """
    # Various ENGRAM ID prefixes that should all be rejected
    for kw in ["dv_0500", "ob_1234", "fl_0148", "ax_0010", "cs_0010",
               "gl_0027", "ls_0011", "df_0048", "qu_0001", "ev_0277",
               "pn_0005", "cj_0056", "ct_0006", "gt_0001", "co_0001",
               "tk_0010", "cn_0001"]:
        entry = _valid_dict(keywords=["concept-one", "concept-two", kw])
        err = validate_summary_entry(entry)
        assert err is not None, f"Expected rejection for keyword {kw!r}"
        assert err.get("field") == "recall_keywords", err
        assert "ENGRAM node ID" in err.get("error", ""), err

    # Non-ID-looking keywords should still pass: tool names, hyphenated
    # phrases, numbers in different positions
    for kw_set in [
        ["engram_focus", "fts_query", "mmr_lambda"],          # tool/symbol names
        ["concept-one", "concept-two", "Lee 2024"],            # has digits but not ID-shape
        ["abc_001", "concept-two", "concept-three"],           # 3 chars but only 3 digits
        ["abcd_0001", "concept-two", "concept-three"],         # 4-char prefix, doesn't match
    ]:
        entry = _valid_dict(keywords=kw_set)
        err = validate_summary_entry(entry)
        assert err is None, (
            f"Expected pass for {kw_set!r} but got error: {err}"
        )


# ===========================================================================
# Section 3 — PayloadBuilder unit tests
# ===========================================================================

from tools.recall_summary_payload import PayloadBuilder  # noqa: E402


# ---------------------------------------------------------------------------
# Test 22: add_summary happy path
# ---------------------------------------------------------------------------

def test_builder_add_summary_happy():
    """Two valid add_summary calls → both appear in summaries list."""
    builder = PayloadBuilder()
    err1 = builder.add_summary("ob_0001", "Summary one.", ["a", "b", "c"])
    err2 = builder.add_summary("ob_0002", "Summary two.", ["d", "e", "f"])
    assert err1 is None, err1
    assert err2 is None, err2
    payload = json.loads(builder.to_json())
    assert len(payload["summaries"]) == 2
    assert payload["summaries"][0]["node_id"] == "ob_0001"
    assert payload["summaries"][1]["node_id"] == "ob_0002"


# ---------------------------------------------------------------------------
# Test 23: add_summary validation-rejected
# ---------------------------------------------------------------------------

def test_builder_add_summary_rejected():
    """Invalid entry returns error dict and is NOT added to summaries."""
    builder = PayloadBuilder()
    err = builder.add_summary("ob_0001", "X" * 201, ["a", "b", "c"])
    assert err is not None, "Expected error for over-cap summary"
    assert "hard_cap" in err, err
    payload = json.loads(builder.to_json())
    assert len(payload["summaries"]) == 0, "Rejected entry must not appear in summaries"


# ---------------------------------------------------------------------------
# Test 24: add_failure
# ---------------------------------------------------------------------------

def test_builder_add_failure():
    """add_failure records in failures list."""
    builder = PayloadBuilder()
    builder.add_failure("ob_0099", "LLM returned empty string")
    payload = json.loads(builder.to_json())
    assert len(payload["failures"]) == 1
    assert payload["failures"][0]["node_id"] == "ob_0099"
    assert payload["failures"][0]["reason"] == "LLM returned empty string"


# ---------------------------------------------------------------------------
# Test 25: to_json shape
# ---------------------------------------------------------------------------

def test_builder_to_json_shape():
    """to_json output always has both 'summaries' and 'failures' keys."""
    builder = PayloadBuilder()
    payload = json.loads(builder.to_json())
    assert "summaries" in payload, payload
    assert "failures" in payload, payload
    assert isinstance(payload["summaries"], list)
    assert isinstance(payload["failures"], list)


# ===========================================================================
# Section 4 — Prompts module tests (carried over from PR #223)
# ===========================================================================

def _load_prompts():
    """Import tools/recall_summary_prompts from the project root."""
    mod_path = os.path.join(PROJECT_ROOT, "tools", "recall_summary_prompts.py")
    spec = importlib.util.spec_from_file_location("recall_summary_prompts", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Test 26
def test_build_prompt_per_type_guidance():
    """Each type in TYPE_GUIDANCE produces a prompt containing the type-specific guidance."""
    prompts = _load_prompts()
    for node_type, guidance_text in prompts.TYPE_GUIDANCE.items():
        prompt = prompts.build_prompt(node_type, "A test claim for this node type.")
        sample = guidance_text[:40]
        assert sample in prompt, (
            f"Type-specific guidance not found in prompt for type={node_type!r}.\n"
            f"Expected to find: {sample!r}\n"
            f"Prompt snippet: {prompt[:300]!r}"
        )


# Test 27
def test_build_prompt_unknown_type_falls_back():
    """Passing an unknown node type uses DEFAULT_GUIDANCE text."""
    prompts = _load_prompts()
    unknown_type = "random_unknown_type_that_is_not_in_type_guidance"
    prompt = prompts.build_prompt(unknown_type, "A claim for an unknown type.")
    sample = prompts.DEFAULT_GUIDANCE[:40]
    assert sample in prompt, (
        f"DEFAULT_GUIDANCE not found in prompt for unknown type.\n"
        f"Expected to find: {sample!r}\n"
        f"Prompt snippet: {prompt[:400]!r}"
    )
    assert unknown_type in prompt, (
        f"Unknown type name not in prompt: {unknown_type!r}"
    )


# Test 28
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
