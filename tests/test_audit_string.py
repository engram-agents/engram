"""Tests for engram_audit_string — literal-substring audit primitive.

The tool exists to catch text-layer leaks that semantic search misses
(prototype: the Mao-Cao incident, ob_1492 / ls_0020). Coverage:

  1. Empty needle returns error JSON.
  2. Invalid field name returns error JSON.
  3. Single-field scan finds substring in claim text.
  4. Multi-field scan ('all') finds matches across multiple fields.
  5. case_sensitive=True respects exact case.
  6. include_superseded=False excludes non-current nodes; True includes them.
  7. SQL LIKE metachars in needle ('%' and '_') are escaped (treated literally).
  8. Snippet contains the substring with surrounding context.
  9. limit caps the result count.
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = os.path.join(tempfile.gettempdir(), "engram_test_audit_string")


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    for mod in ("server", "engram_client"):
        sys.modules.pop(mod, None)
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _add_observation(server, claim: str, quoted: str = None, interp: str = None,
                     url: str = "https://example.com/test") -> str:
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "url": url,
        "title": "Test source",
        "quoted_text": quoted or claim,
        "interpretation": interp or f"Interpretation of: {claim}",
        "claim": claim,
        "quote_type": "hard_data",
    })))
    return r["observation_id"]


def test_empty_needle_errors():
    server = fresh_server()
    r = json.loads(server.engram_audit_string(needle=""))
    assert "error" in r
    assert "non-empty" in r["error"]


def test_invalid_field_errors():
    server = fresh_server()
    r = json.loads(server.engram_audit_string(needle="anything", fields="not_a_field"))
    assert "error" in r
    assert "invalid field" in r["error"]


def test_single_field_finds_substring():
    server = fresh_server()
    _add_observation(server, claim="the LLM produced Mao-Cao as the author")
    _add_observation(server, claim="unrelated claim about pumpkins")
    r = json.loads(server.engram_audit_string(needle="Mao-Cao"))
    assert r["count"] == 1
    assert r["hits"][0]["field"] == "claim"
    assert "Mao-Cao" in r["hits"][0]["snippet"]


def test_all_fields_scans_multiple_columns():
    server = fresh_server()
    _add_observation(
        server,
        claim="claim does NOT mention the pattern",
        quoted="but the quoted text does — Mao-Cao",
        interp="and the interpretation also does — Mao-Cao again",
    )
    r = json.loads(server.engram_audit_string(needle="Mao-Cao", fields="all"))
    fields_hit = {h["field"] for h in r["hits"]}
    # Both quoted_text and interpretation contain the needle; claim does not.
    assert "quoted_text" in fields_hit
    assert "interpretation" in fields_hit
    assert "claim" not in fields_hit


def test_case_sensitive_respects_case():
    server = fresh_server()
    _add_observation(server, claim="Mao-Cao appears in proper case")
    # Default case_sensitive=False: hit
    r1 = json.loads(server.engram_audit_string(needle="mao-cao"))
    assert r1["count"] == 1
    # case_sensitive=True with mismatched case: no hit
    r2 = json.loads(server.engram_audit_string(needle="mao-cao", case_sensitive=True))
    assert r2["count"] == 0
    # case_sensitive=True with proper case: hit
    r3 = json.loads(server.engram_audit_string(needle="Mao-Cao", case_sensitive=True))
    assert r3["count"] == 1


def test_include_superseded_toggles_currency_filter():
    server = fresh_server()
    a = _add_observation(server, claim="Original claim with Mao-Cao reference")
    b = _add_observation(server, claim="Replacement claim without the bad name")
    server.engram_supersede(old_node_id=a, new_node_id=b, supersede_reason="testing supersede")
    # Default: only current nodes scanned — replacement has no Mao-Cao, original is superseded
    r1 = json.loads(server.engram_audit_string(needle="Mao-Cao"))
    assert r1["count"] == 0
    # include_superseded=True: original (now non-current) is scanned
    r2 = json.loads(server.engram_audit_string(needle="Mao-Cao", include_superseded=True))
    assert r2["count"] == 1
    assert r2["hits"][0]["is_current"] is False


def test_sql_metachars_in_needle_treated_literally():
    server = fresh_server()
    _add_observation(server, claim="contains 100% literal percent sign here")
    _add_observation(server, claim="contains a_b underscore here")
    _add_observation(server, claim="completely unrelated content")

    # '100%' should match the first node only — % is NOT a wildcard
    r1 = json.loads(server.engram_audit_string(needle="100%"))
    assert r1["count"] == 1
    assert "100%" in r1["hits"][0]["snippet"]

    # 'a_b' should match the second node only — _ is NOT a wildcard
    r2 = json.loads(server.engram_audit_string(needle="a_b"))
    assert r2["count"] == 1
    assert "a_b" in r2["hits"][0]["snippet"]


def test_snippet_includes_surrounding_context():
    server = fresh_server()
    _add_observation(
        server,
        claim="The quick brown fox NEEDLE jumps over the lazy dog and the long sentence continues here.",
    )
    r = json.loads(server.engram_audit_string(needle="NEEDLE"))
    assert r["count"] == 1
    snippet = r["hits"][0]["snippet"]
    assert "NEEDLE" in snippet
    # Snippet should have surrounding context (40 chars before/after).
    assert "fox" in snippet or "jumps" in snippet


def test_limit_caps_results():
    server = fresh_server()
    for i in range(5):
        _add_observation(server, claim=f"Match TARGET number {i}")
    r = json.loads(server.engram_audit_string(needle="TARGET", limit=3))
    assert r["count"] == 3


def test_case_sensitive_with_limit_finds_uppercase_after_lowercase_noise():
    """Regression for the bug where case_sensitive=True + LIMIT produced
    silent false negatives: SQL's default LIKE is ASCII-case-insensitive,
    so the SQL arm fetched up to `limit` rows that included case-wrong
    matches; the Python post-filter then discarded them, returning count=0
    even when exact-case matches existed beyond the LIMIT boundary.

    Fixed 2026-05-07 (PR #37 round 1) by setting PRAGMA case_sensitive_like
    = ON when case_sensitive=True, so SQL itself only returns case-correct
    matches. This test inserts 5 lowercase-needle rows BEFORE 2 uppercase-
    needle rows; under the buggy implementation, limit=3 would saturate
    with the 5 lowercase rows and return count=0."""
    server = fresh_server()
    for i in range(5):
        _add_observation(server, claim=f"row {i} contains target lowercase")
    _add_observation(server, claim="row A contains TARGET uppercase first")
    _add_observation(server, claim="row B contains TARGET uppercase second")

    r = json.loads(server.engram_audit_string(
        needle="TARGET", case_sensitive=True, limit=3
    ))
    assert r["count"] == 2
    for hit in r["hits"]:
        assert "TARGET" in hit["snippet"]


def test_limit_clamped_against_negative_or_zero():
    """Negative limit silently disabled the cap (SQLite LIMIT -1 = no limit),
    a resource-exhaustion path on large corpora. Fixed 2026-05-07 (PR #37
    round 1) by clamping limit to [1, 10000]."""
    server = fresh_server()
    for i in range(20):
        _add_observation(server, claim=f"Row {i} TARGET")
    r = json.loads(server.engram_audit_string(needle="TARGET", limit=-1))
    assert r["count"] == 1
    r = json.loads(server.engram_audit_string(needle="TARGET", limit=0))
    assert r["count"] == 1


def test_no_hits_returns_empty_list_not_error():
    server = fresh_server()
    _add_observation(server, claim="completely unrelated content")
    r = json.loads(server.engram_audit_string(needle="not-present-anywhere"))
    assert r["count"] == 0
    assert r["hits"] == []
    assert "No hits" in r["message"]
