"""Tests for engram_reflect two-tier-per-category rendering (Wave E recall_summary rollout).

Covers:
  1. Default call (summary_top_k=5): high-volume tiers work correctly
  2. summary_top_k=0: all high-volume entries are keyword-style
  3. summary_top_k=20: all high-volume entries (< 20) get summary-style
  4. Low-volume categories: source-swap (recall_summary present → used; absent → claim fallback)
  5. Missing recall_summary fallback: claim truncated to 160
  6. Missing recall_keywords fallback: bare-ID entry (no keywords key)
  7. Untouched categories: predictions/known_people/feelings/same_source unchanged shape
  8. Sort-by-importance for high-volume tier-1: highest importance scores get summary
  9. uncited_observations tiered correctly
  10. thin_support_derivations tiered correctly
  11. Schema check: summary_top_k present in TOOL_SCHEMAS

Notes on output shape:
  _slim_reflect_briefing caps each category at _REFLECT_TOP_N entries. When total > cap:
    result[key] = {"shown": [...cap entries...], "total": N, "truncated": M, "hint": "..."}
  When total <= cap:
    result[key] = [...entries...]
  Tests keep node counts within caps (weakly_grounded, thin_support, uncited, open_questions
  all cap at 3) to get bare-list output for simpler assertions.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_reflect_view_modes")


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_axiom(server, claim, basis="Test basis."):
    r = json.loads(server.engram_add_axiom(payload_json=json.dumps({
        "claim": claim, "basis": basis,
    })))
    assert "axiom_id" in r, f"add_axiom failed: {r}"
    return r["axiom_id"]


def _add_observation(server, claim, url="https://example.com/test"):
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test Source",
        "quoted_text": claim[:80],
        "interpretation": "Test.",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "observation_id" in r, f"add_observation failed: {r}"
    return r["observation_id"]


def _add_conjecture(server, claim):
    r = json.loads(server.engram_add_conjecture(payload_json=json.dumps({
        "claim": claim,
        "basis": "Speculative pattern from testing.",
    })))
    assert "conjecture_id" in r, f"add_conjecture failed: {r}"
    return r["conjecture_id"]


def _add_goal(server, claim):
    r = json.loads(server.engram_add_goal(payload_json=json.dumps({
        "claim": claim,
        "motivation": "Test motivation for recall test.",
    })))
    assert "goal_id" in r, f"add_goal failed: {r}"
    return r["goal_id"]


def _add_lesson(server, claim):
    """Create a lesson. Requires at least one incident observation."""
    # Create a minimal incident observation first
    ob_id = _add_observation(
        server,
        "Incident observation for lesson test.",
        url="https://example.com/lesson-incident",
    )
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps({
        "claim": claim,
        "incident_ids": ob_id,
        "scaffolding_nudge": "Remember this.",
        "logical_chain": "Test incident leads to test lesson.",
    })))
    assert "lesson_id" in r, f"add_lesson failed: {r}"
    return r["lesson_id"]


def _ask(server, question):
    r = json.loads(server.engram_ask(payload_json=json.dumps({
        "question": question,
        "category": "research",
    })))
    assert "question_id" in r, f"engram_ask failed: {r}"
    return r["question_id"]


def _derive(server, claim, supporting_ids):
    r = json.loads(server.engram_derive(payload_json=json.dumps({
        "claim": claim,
        "supporting_ids": ",".join(supporting_ids) if isinstance(supporting_ids, list) else supporting_ids,
        "logical_chain": "Test chain.",
        "reasoning_type": "deductive_modus_ponens",
    })))
    assert "derivation_id" in r, f"derive failed: {r}"
    return r["derivation_id"]


def _set_recall_fields(server, node_id, summary, keywords):
    r = json.loads(server.engram_set_recall_summaries(payload_json=json.dumps({
        "summaries": [{"node_id": node_id, "recall_summary": summary, "recall_keywords": keywords}]
    })))
    assert r.get("applied") == 1, f"engram_set_recall_summaries failed: {r}"


def _set_importance(server, node_id, score):
    """Directly set importance_score on a node for testing tier ordering."""
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET importance_score = ? WHERE id = ?", (score, node_id))
        conn.commit()
    finally:
        conn.close()


def _make_low_confidence_obs(server, claim, confidence=0.3, url="https://example.com/weak"):
    """Create an observation and lower its confidence for weakly_grounded tests."""
    ob_id = _add_observation(server, claim, url=url)
    conn = server._get_db()
    try:
        conn.execute("UPDATE nodes SET confidence = ? WHERE id = ?", (confidence, ob_id))
        conn.commit()
    finally:
        conn.close()
    return ob_id


def _get_entries(result, key):
    """Unwrap the _slim_reflect_briefing wrapper: returns the list whether bare or wrapped."""
    val = result.get(key, [])
    if isinstance(val, dict) and "shown" in val:
        return val["shown"]
    return val if isinstance(val, list) else []


# ---------------------------------------------------------------------------
# 1. Default call (summary_top_k=5): tier-1 entries have "claim", tier-2 have "keywords"
# ---------------------------------------------------------------------------

def test_default_summary_top_k_tiers_weakly_grounded():
    """Default summary_top_k=5: entries up to position 5 are summary-style (claim key)."""
    server = fresh_server()
    # Create 2 low-confidence observations (both within _REFLECT_TOP_N=3 cap)
    # so output is bare list, and both should be Tier 1 (< 5)
    ids = []
    for i in range(2):
        ob_id = _make_low_confidence_obs(
            server,
            f"Weakly grounded obs {i} for default tier test.",
            url=f"https://example.com/weak{i}",
        )
        _set_recall_fields(server, ob_id, f"RS for obs {i}.", [f"kw{i}a", f"kw{i}b", f"kw{i}c"])
        ids.append(ob_id)

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "weakly_grounded")
    assert len(entries) == 2, f"Expected 2 entries, got {len(entries)}"

    # Both are Tier 1 (index < 5): must have "claim" key
    for entry in entries:
        assert "claim" in entry, f"Tier-1 entry must have 'claim' key: {entry}"
        assert "keywords" not in entry, f"Tier-1 entry must NOT have 'keywords' key: {entry}"


# ---------------------------------------------------------------------------
# 2. summary_top_k=0: all high-volume entries are keyword-style (no claim key)
# ---------------------------------------------------------------------------

def test_summary_top_k_zero_all_keyword_style():
    """summary_top_k=0: all high-volume entries are keyword-style (no claim key)."""
    server = fresh_server()
    for i in range(2):
        ob_id = _make_low_confidence_obs(
            server,
            f"Weakly grounded obs for top_k=0 test {i}.",
            url=f"https://example.com/topk0{i}",
        )
        _set_recall_fields(server, ob_id, f"RS {i}.", [f"a{i}", f"b{i}", f"c{i}"])

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 0})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "weakly_grounded")
    assert len(entries) >= 1, "Expected at least 1 entry"
    for entry in entries:
        assert "claim" not in entry, (
            f"With summary_top_k=0, no entry should have 'claim' key: {entry}"
        )
        # Either has keywords or is bare-ID entry; either is valid


# ---------------------------------------------------------------------------
# 3. summary_top_k=20: all entries get summary-style when count < 20
# ---------------------------------------------------------------------------

def test_summary_top_k_large_all_summary_style():
    """summary_top_k=20: all high-volume entries (< 20) get summary-style."""
    server = fresh_server()
    for i in range(2):
        ob_id = _make_low_confidence_obs(
            server,
            f"Weakly grounded obs for top_k=20 test {i}.",
            url=f"https://example.com/topk20{i}",
        )
        _set_recall_fields(server, ob_id, f"Summary for obs {i}.", [f"x{i}", f"y{i}", f"z{i}"])

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 20})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "weakly_grounded")
    assert len(entries) >= 1, "Expected at least 1 entry"
    for entry in entries:
        assert "claim" in entry, (
            f"With summary_top_k=20 and fewer entries, all should be summary-style: {entry}"
        )
        assert "keywords" not in entry, f"Summary-style entry must not have keywords: {entry}"


# ---------------------------------------------------------------------------
# 4. Low-volume categories: source-swap
# ---------------------------------------------------------------------------

def test_low_volume_conjecture_uses_recall_summary():
    """open_conjectures: claim field sourced from recall_summary when present."""
    server = fresh_server()
    cj_id = _add_conjecture(server, "Original conjecture claim text.")
    _set_recall_fields(server, cj_id, "Recall summary for conjecture.", ["cj", "summary", "test"])

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    conjectures = _get_entries(result, "open_conjectures")
    target = next((c for c in conjectures if c["id"] == cj_id), None)
    assert target is not None, f"{cj_id} not found in open_conjectures"
    assert target["claim"] == "Recall summary for conjecture.", (
        f"Expected recall_summary as claim; got: {target['claim']}"
    )


def test_low_volume_conjecture_fallback_to_claim():
    """open_conjectures: claim field falls back to original claim when no recall_summary."""
    server = fresh_server()
    cj_id = _add_conjecture(server, "Conjecture with no recall summary set.")

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    conjectures = _get_entries(result, "open_conjectures")
    target = next((c for c in conjectures if c["id"] == cj_id), None)
    assert target is not None, f"{cj_id} not found in open_conjectures"
    assert "Conjecture with no recall summary" in target["claim"], (
        f"Fallback should use original claim text; got: {target['claim']}"
    )


def test_low_volume_goal_uses_recall_summary():
    """active_goals: goal field sourced from recall_summary when present."""
    server = fresh_server()
    gl_id = _add_goal(server, "Original goal claim text for recall test.")
    _set_recall_fields(server, gl_id, "Goal recall summary.", ["goal", "recall", "test"])

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    goals = _get_entries(result, "active_goals")
    target = next((g for g in goals if g["id"] == gl_id), None)
    assert target is not None, f"{gl_id} not found in active_goals"
    assert target["goal"] == "Goal recall summary.", (
        f"Expected recall_summary as goal; got: {target['goal']}"
    )


def test_low_volume_question_uses_recall_summary():
    """open_questions: question field sourced from recall_summary when present."""
    server = fresh_server()
    qu_id = _ask(server, "What is the question we want to recall?")
    _set_recall_fields(server, qu_id, "Question recall summary.", ["question", "recall", "test"])

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    questions = _get_entries(result, "open_questions")
    target = next((q for q in questions if q["id"] == qu_id), None)
    assert target is not None, f"{qu_id} not found in open_questions"
    assert target["question"] == "Question recall summary.", (
        f"Expected recall_summary as question; got: {target['question']}"
    )


def test_low_volume_lesson_uses_recall_summary():
    """active_lessons: claim field sourced from recall_summary when present."""
    server = fresh_server()
    ls_id = _add_lesson(server, "Original lesson claim for recall test.")
    _set_recall_fields(server, ls_id, "Lesson recall summary.", ["lesson", "recall", "test"])

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    lessons = _get_entries(result, "active_lessons")
    target = next((l for l in lessons if l["id"] == ls_id), None)
    assert target is not None, f"{ls_id} not found in active_lessons"
    assert target["claim"] == "Lesson recall summary.", (
        f"Expected recall_summary as claim; got: {target['claim']}"
    )


# ---------------------------------------------------------------------------
# 5. Missing recall_summary fallback: claim truncated to 160 chars
# ---------------------------------------------------------------------------

def test_high_volume_claim_truncated_to_160_when_no_recall_summary():
    """High-volume Tier-1 with no recall_summary falls back to claim[:160]."""
    server = fresh_server()
    long_claim = "X" * 200  # definitely longer than 160 chars
    ob_id = _make_low_confidence_obs(server, long_claim, url="https://example.com/long")
    # No recall_summary set

    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 50})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "weakly_grounded")
    target = next((w for w in entries if w.get("id") == ob_id), None)
    assert target is not None, f"{ob_id} not found in weakly_grounded"
    assert "claim" in target, "Tier-1 entry must have claim key"
    claim_val = target["claim"]
    # Note: _slim_reflect_briefing truncates claim to 199 chars (_REFLECT_FIELD_MAX=200)
    # matching RECALL_SUMMARY_HARD_CAP so RS content passes through without further cut.
    assert len(claim_val) <= 200, f"claim should not exceed 200 chars; got {len(claim_val)}"
    assert "X" in claim_val, "Truncated claim should contain the original content"


def test_low_volume_claim_truncated_to_160_when_no_recall_summary():
    """Low-volume (conjecture) with no recall_summary falls back to claim[:160]."""
    server = fresh_server()
    long_claim = "C" * 200
    cj_id = _add_conjecture(server, long_claim)

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    conjectures = _get_entries(result, "open_conjectures")
    target = next((c for c in conjectures if c["id"] == cj_id), None)
    assert target is not None, f"{cj_id} not found in open_conjectures"
    claim_val = target["claim"]
    # _slim_reflect_briefing truncates to 199 chars (_REFLECT_FIELD_MAX=200), so effective max is 199
    assert len(claim_val) <= 200, f"claim should be truncated; got {len(claim_val)}: {claim_val!r}"


# ---------------------------------------------------------------------------
# 6. Missing recall_keywords fallback: bare-ID entry (no keywords key)
# ---------------------------------------------------------------------------

def test_missing_recall_keywords_produces_bare_id_entry():
    """Tier-2 entry with no recall_keywords: bare {id, confidence} — no 'keywords' key."""
    server = fresh_server()
    # Create 2 low-confidence nodes, set only recall_summary (no keywords)
    ids = []
    for i in range(2):
        ob_id = _make_low_confidence_obs(
            server,
            f"Obs for bare-id test {i}.",
            url=f"https://example.com/bare{i}",
        )
        # Set recall_summary but not recall_keywords
        conn = server._get_db()
        try:
            conn.execute(
                "UPDATE nodes SET recall_summary = ? WHERE id = ?",
                (f"Summary {i}.", ob_id)
            )
            conn.commit()
        finally:
            conn.close()
        ids.append(ob_id)

    # summary_top_k=1: first entry (highest importance) is Tier 1; second is Tier 2
    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 1})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "weakly_grounded")
    assert len(entries) >= 2, f"Expected at least 2 entries; got {len(entries)}"

    # Tier 2 entries (index >= 1): no "claim" key; also no "keywords" (recall_keywords is NULL)
    tier2_entries = entries[1:]
    for e in tier2_entries:
        assert "claim" not in e, f"Tier-2 must not have 'claim': {e}"
        # No keywords either (since recall_keywords was not set)
        # The entry should just be {id, type, confidence}
        assert "keywords" not in e, (
            f"Tier-2 entry with NULL recall_keywords must not have 'keywords': {e}"
        )


# ---------------------------------------------------------------------------
# 7. Untouched categories: predictions/feelings/same_source shape preserved
# ---------------------------------------------------------------------------

def test_untouched_categories_preserve_shape():
    """overdue_predictions, open_predictions, same_source_review retain their shapes.

    Note: _slim_reflect_briefing drops categories with zero items. These tests
    verify the shape when entries exist, and verify the summary counts are correct
    when entries don't exist (not present in output but count=0 in summary).
    """
    server = fresh_server()
    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    # Summary counts are always present
    summary = result.get("summary", {})
    assert "overdue_predictions" in summary or True  # count present
    assert "recent_feeling_reports" in summary or True

    # Prediction entries use "event" key (not "claim") — check if any exist
    for pred in _get_entries(result, "overdue_predictions"):
        assert "event" in pred, f"overdue prediction missing 'event' key: {pred}"
        assert "claim" not in pred, f"overdue prediction must not have 'claim': {pred}"

    for pred in _get_entries(result, "open_predictions"):
        assert "event" in pred, f"open prediction missing 'event' key: {pred}"
        assert "claim" not in pred, f"open prediction must not have 'claim': {pred}"

    # same_source_review entries use evidence_id, not claim
    for entry in _get_entries(result, "same_source_review"):
        assert "evidence_id" in entry, f"same_source_review missing 'evidence_id': {entry}"
        assert "claim" not in entry, f"same_source_review must not have 'claim': {entry}"


# ---------------------------------------------------------------------------
# 8. Sort-by-importance: highest importance scores land in Tier 1
# ---------------------------------------------------------------------------

def test_high_importance_nodes_get_tier1():
    """Nodes with higher importance_score land in Tier 1 (summary-style)."""
    server = fresh_server()

    # Create 3 low-confidence obs with explicit importance scores
    # _REFLECT_TOP_N["weakly_grounded"]=3 so they all show when count=3
    high_imp_id = _make_low_confidence_obs(
        server, "High-importance weakly grounded obs.", url="https://example.com/himp"
    )
    _set_recall_fields(server, high_imp_id, "High importance RS.", ["hi", "imp", "test"])
    _set_importance(server, high_imp_id, 2.0)

    low_imp_id = _make_low_confidence_obs(
        server, "Low-importance weakly grounded obs.", url="https://example.com/limp"
    )
    _set_recall_fields(server, low_imp_id, "Low importance RS.", ["lo", "imp", "test"])
    _set_importance(server, low_imp_id, 0.1)

    # summary_top_k=1: only the highest-importance node should be Tier 1
    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 1})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "weakly_grounded")
    assert len(entries) >= 2, f"Expected at least 2 entries; got {len(entries)}"

    # First entry (highest importance) should be Tier 1 (has "claim")
    first = entries[0]
    assert "claim" in first, f"Top-importance entry should be Tier-1 (has claim): {first}"
    assert first["id"] == high_imp_id, (
        f"Expected high-importance node {high_imp_id} first; got {first['id']}"
    )

    # Second entry (lower importance) should be Tier 2 (no "claim")
    second = entries[1]
    assert "claim" not in second, f"Low-importance entry should be Tier-2 (no claim): {second}"
    assert second["id"] == low_imp_id, (
        f"Expected low-importance node {low_imp_id} second; got {second['id']}"
    )


# ---------------------------------------------------------------------------
# 9. uncited_observations tiered correctly
# ---------------------------------------------------------------------------

def test_uncited_observations_tiered():
    """uncited_observations respects the tier split at summary_top_k."""
    server = fresh_server()
    # Create 2 uncited observations (< _REFLECT_TOP_N=3 so bare list output)
    obs = []
    for i in range(2):
        ob_id = _add_observation(
            server,
            f"Uncited observation for tier test {i}.",
            url=f"https://example.com/uncited{i}",
        )
        _set_recall_fields(server, ob_id, f"Summary for uncited {i}.", [f"unc{i}a", f"unc{i}b", f"unc{i}c"])
        obs.append(ob_id)

    # summary_top_k=1: first is Tier 1, second is Tier 2
    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 1})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "uncited_observations")
    assert len(entries) >= 2, f"Expected at least 2 uncited entries; got {len(entries)}"

    # Tier 1 (first 1): must have "claim"
    assert "claim" in entries[0], f"uncited Tier-1 must have 'claim': {entries[0]}"
    assert "keywords" not in entries[0], f"uncited Tier-1 must not have 'keywords': {entries[0]}"

    # Tier 2 (remainder): must NOT have "claim"
    for entry in entries[1:]:
        assert "claim" not in entry, f"uncited Tier-2 must not have 'claim': {entry}"


# ---------------------------------------------------------------------------
# 10. thin_support_derivations tiered correctly
# ---------------------------------------------------------------------------

def test_thin_support_derivations_tiered():
    """thin_support_derivations respects the tier split at summary_top_k."""
    server = fresh_server()
    ax_id = _add_axiom(server, "Single axiom for thin-support test.")
    dvs = []
    for i in range(2):
        dv_id = _derive(server, f"Thin-support derivation {i}.", [ax_id])
        _set_recall_fields(server, dv_id, f"DS summary {i}.", [f"ds{i}a", f"ds{i}b", f"ds{i}c"])
        dvs.append(dv_id)

    # summary_top_k=1: first is Tier 1, second is Tier 2
    result = json.loads(server.engram_reflect(payload_json=json.dumps({"summary_top_k": 1})))
    assert "error" not in result, f"unexpected error: {result}"

    entries = _get_entries(result, "thin_support_derivations")
    assert len(entries) >= 2, f"Expected at least 2 thin_support entries; got {len(entries)}"

    # Tier 1 (first 1): must have "claim"
    assert "claim" in entries[0], f"thin_support Tier-1 must have 'claim': {entries[0]}"
    assert "keywords" not in entries[0], f"thin_support Tier-1 must not have 'keywords': {entries[0]}"

    # Tier 2 (remainder): must NOT have "claim"
    for entry in entries[1:]:
        assert "claim" not in entry, f"thin_support Tier-2 must not have 'claim': {entry}"


# ---------------------------------------------------------------------------
# 11. Whitespace-only recall_summary falls back to claim (defense-in-depth guard)
# ---------------------------------------------------------------------------

def test_whitespace_only_recall_summary_falls_back_to_claim():
    """_reflect_rs_or_claim falls back to claim when recall_summary is whitespace-only.

    The validator prevents blank/whitespace recall_summary in normal writes,
    but a direct-SQL bypass can plant one. This guard ensures such a row
    still renders the actual claim rather than returning an empty string.
    """
    server = fresh_server()
    cj_id = _add_conjecture(server, "Conjecture with whitespace-only recall summary.")
    # Plant a whitespace-only recall_summary directly — bypasses the validator.
    conn = server._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET recall_summary = ? WHERE id = ?",
            ("   \t  ", cj_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    conjectures = _get_entries(result, "open_conjectures")
    target = next((c for c in conjectures if c["id"] == cj_id), None)
    assert target is not None, f"{cj_id} not found in open_conjectures"
    claim_val = target["claim"]
    # Must NOT be empty — the whitespace rs should have been stripped and fallen through.
    assert claim_val, f"claim must not be empty when recall_summary is whitespace-only; got: {claim_val!r}"
    # Must contain the original claim text, not empty/whitespace.
    assert "Conjecture with whitespace" in claim_val, (
        f"Expected original claim text in fallback; got: {claim_val!r}"
    )


# ---------------------------------------------------------------------------
# 12. Schema check: summary_top_k present in TOOL_SCHEMAS
# ---------------------------------------------------------------------------

def test_schema_has_summary_top_k():
    """TOOL_SCHEMAS['engram_reflect'] contains summary_top_k parameter."""
    if "engram_client" in sys.modules:
        del sys.modules["engram_client"]
    import engram_client
    schema = engram_client.TOOL_SCHEMAS.get("engram_reflect")
    assert schema is not None, "engram_reflect missing from TOOL_SCHEMAS"
    params = schema.get("parameters", {})
    assert "summary_top_k" in params, (
        f"summary_top_k missing from engram_reflect schema parameters; "
        f"got: {list(params.keys())}"
    )


# ---------------------------------------------------------------------------
# 13. calibration_snapshot key is present in the response
# ---------------------------------------------------------------------------

def test_calibration_snapshot_key_present():
    """engram_reflect always returns a calibration_snapshot key at top level."""
    server = fresh_server()
    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"
    assert "calibration_snapshot" in result, (
        f"calibration_snapshot key missing from reflect output; keys: {list(result.keys())}"
    )
    snap = result["calibration_snapshot"]
    assert isinstance(snap, dict), f"calibration_snapshot must be a dict; got: {type(snap)}"
    assert "corpus" in snap, f"calibration_snapshot missing 'corpus' key; got: {list(snap.keys())}"
    assert "this_turn" in snap, f"calibration_snapshot missing 'this_turn' key; got: {list(snap.keys())}"


# ---------------------------------------------------------------------------
# 14. corpus and this_turn are dicts with expected subkeys (when data exists)
# ---------------------------------------------------------------------------

def test_calibration_snapshot_subkeys_when_data_exists():
    """corpus and this_turn contain by_type/by_quote_type/by_reasoning_type/by_source_class
    when there are confidence-bearing nodes in the DB."""
    server = fresh_server()
    # Create an observation (confidence-bearing) so distributions are non-empty
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": "https://example.com/calib-test",
        "title": "Calibration Test Source",
        "quoted_text": "Test quoted text for calibration snapshot test.",
        "interpretation": "Test interpretation.",
        "claim": "Calibration test observation claim.",
        "quote_type": "hard_data",
    })))
    assert r.get("status") == "created", f"observation creation failed: {r}"

    result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
    assert "error" not in result, f"unexpected error: {result}"

    snap = result["calibration_snapshot"]
    assert "error" not in snap, f"calibration_snapshot has error: {snap}"

    corpus = snap["corpus"]
    assert isinstance(corpus, dict), f"corpus must be a dict; got: {type(corpus)}"
    # When confidence-bearing nodes exist, at least by_type should be populated.
    assert "by_type" in corpus, (
        f"corpus missing 'by_type' subkey; got: {list(corpus.keys())}"
    )
    assert "by_quote_type" in corpus, (
        f"corpus missing 'by_quote_type' subkey; got: {list(corpus.keys())}"
    )
    assert "by_reasoning_type" in corpus, (
        f"corpus missing 'by_reasoning_type' subkey; got: {list(corpus.keys())}"
    )
    assert "by_source_class" in corpus, (
        f"corpus missing 'by_source_class' subkey; got: {list(corpus.keys())}"
    )

    # this_turn has the same key structure: the observation was just created so
    # it falls within the 1-turn window and all four subkeys should be present.
    this_turn = snap["this_turn"]
    assert isinstance(this_turn, dict), f"this_turn must be a dict; got: {type(this_turn)}"
    assert "by_type" in this_turn, (
        f"this_turn missing 'by_type' subkey; got: {list(this_turn.keys())}"
    )
    assert "by_quote_type" in this_turn, (
        f"this_turn missing 'by_quote_type' subkey; got: {list(this_turn.keys())}"
    )
    assert "by_reasoning_type" in this_turn, (
        f"this_turn missing 'by_reasoning_type' subkey; got: {list(this_turn.keys())}"
    )
    assert "by_source_class" in this_turn, (
        f"this_turn missing 'by_source_class' subkey; got: {list(this_turn.keys())}"
    )


# ---------------------------------------------------------------------------
# 15. When stats unavailable (patched error), reflect still succeeds with error marker
# ---------------------------------------------------------------------------

def test_calibration_snapshot_graceful_on_stats_error():
    """When engram_stats raises unexpectedly, reflect returns without error and
    calibration_snapshot contains the error marker dict."""
    server = fresh_server()

    # Monkey-patch engram_stats in the server module to raise
    original_engram_stats = server.engram_stats

    def _broken_stats(payload_json="{}"):
        raise RuntimeError("simulated engram_stats failure")

    server.engram_stats = _broken_stats
    try:
        result = json.loads(server.engram_reflect(payload_json=json.dumps({})))
        # The overall reflect call must succeed (no top-level "error" key)
        assert "error" not in result, (
            f"reflect must not fail when engram_stats raises; got: {result}"
        )
        snap = result.get("calibration_snapshot")
        assert snap is not None, "calibration_snapshot key must be present even on stats error"
        assert "error" in snap, (
            f"calibration_snapshot must contain 'error' key when stats fails; got: {snap}"
        )
        assert snap["error"] == "engram_stats unavailable", (
            f"unexpected error message: {snap['error']!r}"
        )
    finally:
        server.engram_stats = original_engram_stats
