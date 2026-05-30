"""Tests for engram_list recall_summary integration (Wave D rollout).

Covers:
  1. Default triage view: node with populated recall_summary → triage row's
     `claim` field equals the recall_summary text, not the original long claim.
  2. Default triage view: node WITHOUT recall_summary → falls back to existing
     truncated-claim behavior (regression check; legacy nodes still work).
  3. fields_json=["id","recall_summary"] returns rows with that projection
     (smoke test for whitelist addition).
  4. fields_json=["id","recall_keywords"] works (smoke test for keywords
     whitelist addition).
  5. filters_json using recall_summary as a filter field works.
  6. filters_json using recall_keywords as a filter field works.

Fixture pattern: same fresh_server() + _add_observation() shape as
test_engram_filter.py (TestEngramListStructured).
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_list_recall_summary_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Reset state and return the server module rebound to the test dir."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(srv, claim: str, url: str = "https://example.com/test") -> str:
    r = json.loads(srv.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test source",
        "quoted_text": claim,
        "interpretation": f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    assert "error" not in r, r
    return r["observation_id"]


def _set_recall_fields(srv, node_id: str, summary: str, keywords: list[str]) -> None:
    """Directly write recall_summary + recall_keywords to the DB (backfill pattern)."""
    conn = srv._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET recall_summary = ?, recall_keywords = ? WHERE id = ?",
            (summary, json.dumps(keywords), node_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 1: recall_summary present → triage `claim` field equals the summary
# ---------------------------------------------------------------------------


def test_triage_claim_sources_from_recall_summary_when_populated():
    """When a node has recall_summary, the default triage `claim` key equals
    the recall_summary text, not the (possibly long) original claim."""
    srv = fresh_server()

    long_original_claim = (
        "A very long original claim that exceeds one hundred and thirty characters "
        "and would normally be truncated by the legacy fallback path in engram_list "
        "triage output so we can distinguish the two code paths clearly here"
    )
    assert len(long_original_claim) > 130, "Pre-condition: original claim must exceed 130 chars"

    ob_id = _add_observation(srv, long_original_claim)

    summary = "Curated dense summary for triage recognition purposes."
    _set_recall_fields(srv, ob_id, summary, ["curated", "dense", "summary"])

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"

    assert match["claim"] == summary, (
        f"Expected triage `claim` to equal recall_summary.\n"
        f"  Got:      {match['claim']!r}\n"
        f"  Expected: {summary!r}"
    )
    # Confirm the original long claim text is NOT present in the claim field
    assert long_original_claim not in match["claim"], (
        "Original long claim leaked into triage output when recall_summary was present"
    )


# ---------------------------------------------------------------------------
# Test 2: recall_summary absent → falls back to truncated claim (regression)
# ---------------------------------------------------------------------------


def test_triage_claim_falls_back_to_truncated_claim_when_no_summary():
    """Legacy nodes without recall_summary: triage `claim` field is the
    original claim truncated to 130 chars with '...' suffix, unchanged."""
    srv = fresh_server()

    long_claim = (
        "A long original claim that is definitely more than one hundred and thirty "
        "characters so that the truncation path fires and we can check the suffix "
        "appears correctly in the triage output for legacy nodes without a summary"
    )
    assert len(long_claim) > 130, "Pre-condition: claim must exceed 130 chars"

    ob_id = _add_observation(srv, long_claim)
    # No recall_summary written — this is the legacy/unfilled node path.

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"

    expected = long_claim[:130] + "..."
    assert match["claim"] == expected, (
        f"Expected legacy truncation fallback in triage `claim`.\n"
        f"  Got:      {match['claim']!r}\n"
        f"  Expected: {expected!r}"
    )


def test_triage_claim_short_claim_no_truncation_when_no_summary():
    """Short claim (≤130 chars) without recall_summary: returned verbatim,
    no truncation suffix."""
    srv = fresh_server()

    short_claim = "A short claim that fits within the 130-char limit."
    assert len(short_claim) <= 130, "Pre-condition: claim must fit within 130 chars"

    ob_id = _add_observation(srv, short_claim)

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"
    assert match["claim"] == short_claim, (
        f"Short claim should be verbatim (no truncation): {match['claim']!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: fields_json=["id","recall_summary"] projection works
# ---------------------------------------------------------------------------


def test_fields_json_recall_summary_projection():
    """fields_json=['id','recall_summary'] returns rows with exactly those
    columns (whitelist addition smoke test)."""
    srv = fresh_server()
    ob_id = _add_observation(srv, "projection test claim for recall_summary field")
    summary = "Projection test summary."
    _set_recall_fields(srv, ob_id, summary, ["proj", "test", "field"])

    result = json.loads(srv.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({"field": "type", "op": "eq", "value": "observation_factual"}),
        "fields_json": json.dumps(["id", "recall_summary"]),
    })))
    assert result["status"] == "ok", f"Unexpected error: {result.get('error')}"
    assert result["nodes"], "Expected at least one node in projection result"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not in projection result"

    # Only id + recall_summary should be present (id is always prepended)
    assert set(match.keys()) == {"id", "recall_summary"}, (
        f"Unexpected keys in projection result: {set(match.keys())}"
    )
    assert match["recall_summary"] == summary, (
        f"recall_summary mismatch: {match['recall_summary']!r} vs {summary!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: fields_json=["id","recall_keywords"] projection works
# ---------------------------------------------------------------------------


def test_fields_json_recall_keywords_projection():
    """fields_json=['id','recall_keywords'] returns rows with exactly those
    columns (whitelist addition smoke test for recall_keywords)."""
    srv = fresh_server()
    ob_id = _add_observation(srv, "projection test claim for recall_keywords field")
    keywords = ["kw-alpha", "kw-beta", "kw-gamma"]
    _set_recall_fields(srv, ob_id, "Some summary.", keywords)

    result = json.loads(srv.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({"field": "type", "op": "eq", "value": "observation_factual"}),
        "fields_json": json.dumps(["id", "recall_keywords"]),
    })))
    assert result["status"] == "ok", f"Unexpected error: {result.get('error')}"
    assert result["nodes"], "Expected at least one node in projection result"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not in projection result"

    assert set(match.keys()) == {"id", "recall_keywords"}, (
        f"Unexpected keys in projection result: {set(match.keys())}"
    )
    # recall_keywords stored as JSON string in DB; custom projection returns raw value
    kw_raw = match["recall_keywords"]
    assert kw_raw is not None, "recall_keywords should not be None after _set_recall_fields"
    assert json.loads(kw_raw) == keywords, (
        f"recall_keywords content mismatch: {kw_raw!r} vs {keywords!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: filters_json using recall_summary as filter field works
# ---------------------------------------------------------------------------


def test_filters_json_recall_summary_contains():
    """filters_json with recall_summary contains filter returns matching nodes
    and rejects unknown-field error (smoke test that recall_summary is in
    ALLOWED_NODE_COLUMNS and filter evaluation works end-to-end)."""
    srv = fresh_server()

    ob_id = _add_observation(srv, "filter test claim for recall_summary predicate")
    summary = "Unique sentinel phrase for filter test xyzzy42."
    _set_recall_fields(srv, ob_id, summary, ["filter", "test", "sentinel"])

    # Also add a node without the sentinel phrase to confirm selectivity
    _add_observation(srv, "unrelated claim without the sentinel phrase",
                     url="https://example.com/other")

    result = json.loads(srv.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({
            "field": "recall_summary",
            "op": "contains",
            "value": "xyzzy42",
        }),
    })))
    assert result["status"] == "ok", (
        f"Unexpected error from recall_summary filter: {result.get('error')}"
    )
    assert result["total_matched"] >= 1, "Expected at least one match for recall_summary filter"
    returned_ids = {n["id"] for n in result["nodes"]}
    assert ob_id in returned_ids, (
        f"Node {ob_id} with matching recall_summary not in filter result"
    )


# ---------------------------------------------------------------------------
# Test 6: filters_json using recall_keywords as filter field works
# ---------------------------------------------------------------------------


def test_filters_json_recall_keywords_is_not_null():
    """filters_json with recall_keywords is_not_null filter returns only nodes
    that have recall_keywords populated (smoke test for filter evaluation)."""
    srv = fresh_server()

    ob_with_kw = _add_observation(srv, "node with keywords populated",
                                   url="https://example.com/with-kw")
    ob_without_kw = _add_observation(srv, "node without keywords unfilled",
                                      url="https://example.com/without-kw")
    _set_recall_fields(srv, ob_with_kw, "Summary for kw node.", ["alpha", "beta", "gamma"])
    # ob_without_kw intentionally left unfilled.

    result = json.loads(srv.engram_list(payload_json=json.dumps({
        "filters_json": json.dumps({
            "field": "recall_keywords",
            "op": "is_not_null",
        }),
    })))
    assert result["status"] == "ok", (
        f"Unexpected error from recall_keywords filter: {result.get('error')}"
    )
    returned_ids = {n["id"] for n in result["nodes"]}
    assert ob_with_kw in returned_ids, (
        f"Node {ob_with_kw} with recall_keywords should appear in is_not_null filter result"
    )
    assert ob_without_kw not in returned_ids, (
        f"Node {ob_without_kw} without recall_keywords should NOT appear in is_not_null filter"
    )


# ---------------------------------------------------------------------------
# Test 7: oversize recall_summary (>200 chars via direct SQL) → truncated
#         to 200 chars + "..." in triage output (defense-in-depth branch)
# ---------------------------------------------------------------------------


def test_triage_oversize_recall_summary_truncated_with_ellipsis():
    """A node whose recall_summary exceeds 200 chars (written via direct SQL,
    bypassing the validator) should appear in triage with claim truncated to
    200 chars + '...' (length 203), matching the legacy claim-truncation
    convention."""
    srv = fresh_server()

    ob_id = _add_observation(srv, "claim for oversize recall_summary test")

    # Build a summary that is definitely >200 chars.
    oversize_summary = "X" * 250
    assert len(oversize_summary) == 250, "Pre-condition: summary must be 250 chars"

    # Write directly to the DB bypassing the validator (the only way to get a
    # >200-char value into the table, since the batch tool enforces the hard cap).
    conn = srv._get_db()
    try:
        conn.execute(
            "UPDATE nodes SET recall_summary = ? WHERE id = ?",
            (oversize_summary, ob_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = json.loads(srv.engram_list(payload_json=json.dumps({"node_type": "observation_factual"})))
    assert result["status"] == "ok"

    match = next((n for n in result["nodes"] if n["id"] == ob_id), None)
    assert match is not None, f"Node {ob_id} not found in engram_list output"

    expected_claim = "X" * 200 + "..."
    assert match["claim"] == expected_claim, (
        f"Expected oversize recall_summary truncated to 200 chars + '...' (len 203).\n"
        f"  Got (len {len(match['claim'])}): {match['claim']!r}\n"
        f"  Expected (len 203):              {expected_claim!r}"
    )
    assert len(match["claim"]) == 203, (
        f"Expected length 203, got {len(match['claim'])}"
    )
