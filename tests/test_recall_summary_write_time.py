"""Tests for the write-time recall_summary + recall_keywords contract (#1952 PR1).

Covers the NEW optional recall_summary/recall_keywords kwargs on the six
write-time filing tools (engram_add_observation[_batch], engram_add_definition,
engram_add_conjecture, engram_add_lesson, engram_derive) and the shared
engram_core._validate_recall_fields helper.

Resolution 1 (round 3 spec): presence is soft, not a hard gate. An absent/
omitted recall_summary normalizes to 0 chars (clean-accepts); an absent/
omitted recall_keywords normalizes to []. Only an explicitly-provided
recall_summary >= 280 chars is a hard reject (no node created).

Resolution 2: write-time recall_keywords uses a 3-6 band (never rejects on
count) — distinct from the OLDER engram_set_recall_summaries tool's 3-5 band
in tools/recall_summary_validator.py, which is untouched by this PR and not
covered here.

Round 4 (Kepler's ruling): absent recall_summary is NOT clean-equivalent to a
fine short summary — it gets its own distinct summary_missing flag, mutually
exclusive with needs_tightening (fail-loud-surface principle: absence is
queryable, distinct from "present but over-limit"). recall_keywords behavior
is unchanged — no distinct flag, asymmetric/additive by design.

Coverage:

Section 1 — engram_core._validate_recall_fields (pure function, no DB):
  1.  test_validate_summary_under_target_clean_accept
  2.  test_validate_summary_soft_accept_band
  3.  test_validate_summary_at_exactly_140_is_clean
  4.  test_validate_summary_hard_reject_at_280
  5.  test_validate_summary_absent_clean_accept
  6.  test_validate_summary_none_input_treated_as_absent
  7.  test_validate_keywords_normalization
  8.  test_validate_keywords_absent_clean_accept
  9.  test_validate_keywords_never_rejects_low_count
  10. test_validate_keywords_non_list_input_normalizes_to_empty

Section 2 — engram_add_observation (MCP-level, DB integration):
  11. test_observation_summary_under_140_clean_accepts
  12. test_observation_summary_soft_accepts_140_to_279
  13. test_observation_summary_hard_rejects_280_plus
  14. test_observation_summary_omitted_clean_accepts
  15. test_observation_keywords_normalization_at_write_time
  16. test_observation_keywords_omitted_clean_accepts
  27. test_observation_summary_missing_mutually_exclusive_with_tightening

Section 3 — engram_add_observation_batch:
  17. test_batch_partial_success_mixed_reject_and_omit

Section 4 — smoke coverage across the other four write paths:
  18. test_definition_recall_fields_roundtrip
  19. test_definition_hard_reject
  20. test_conjecture_recall_fields_roundtrip
  21. test_conjecture_hard_reject
  22. test_lesson_recall_fields_roundtrip
  23. test_lesson_hard_reject
  24. test_derive_recall_fields_roundtrip
  25. test_derive_hard_reject
  28. test_lesson_summary_missing_flag
  29. test_derive_summary_missing_flag

Section 5 — regression guard:
  26. test_retract_replacement_path_unaffected (delegates to
      test_retract_payload.py; see run instructions in the PR handoff —
      this file does not duplicate that suite, just documents the
      cross-reference).
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engram_core as core  # noqa: E402

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_recall_summary_write_time_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Create a clean server module instance in a temp sandbox directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _row(srv, node_id: str):
    conn = srv._get_db()
    try:
        return conn.execute(
            "SELECT recall_summary, recall_keywords, needs_tightening, "
            "summary_missing FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    finally:
        conn.close()


def _node_count(srv) -> int:
    conn = srv._get_db()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
    finally:
        conn.close()


def _add_obs(srv, claim: str, **extra) -> dict:
    payload = {
        "quoted_text": "test quote",
        "interpretation": "test interpretation",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/test",
        "title": "Test",
    }
    payload.update(extra)
    return json.loads(srv.engram_add_observation(payload_json=json.dumps(payload)))


# ===========================================================================
# Section 1 — engram_core._validate_recall_fields unit tests
# ===========================================================================


def test_validate_summary_under_target_clean_accept():
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields(
        "A short summary.", ["a", "b", "c"]
    )
    assert error is None
    assert summary == "A short summary."
    assert needs_tightening is False
    assert summary_missing is False


def test_validate_summary_soft_accept_band():
    text = "X" * 200
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields(text, None)
    assert error is None
    assert summary == text
    assert needs_tightening is True
    assert summary_missing is False


def test_validate_summary_at_exactly_140_is_clean():
    text = "Y" * 140
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields(text, None)
    assert error is None
    assert needs_tightening is False, (
        "140 chars is the target (<=140), not the soft-accept band — must not flag."
    )
    assert summary_missing is False


def test_validate_summary_hard_reject_at_280():
    text = "Z" * 280
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields(text, None)
    assert summary is None
    assert keywords is None
    assert error is not None
    assert "280 chars" in error, error
    assert "limit is 140" in error, error
    assert text in error, "error must echo the text verbatim"

    # One below the hard-reject threshold (279) must still soft-accept.
    text279 = "Z" * 279
    summary2, keywords2, needs_tightening2, summary_missing2, error2 = core._validate_recall_fields(text279, None)
    assert error2 is None
    assert needs_tightening2 is True
    assert summary_missing2 is False


def test_validate_summary_absent_clean_accept():
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields("", None)
    assert error is None
    assert summary == ""
    assert keywords == []
    assert needs_tightening is False
    assert summary_missing is True, (
        "round 4 (Kepler's ruling): absent gets its own distinct flag, "
        "not clean-equivalent to a fine short summary."
    )


def test_validate_summary_none_input_treated_as_absent():
    # Signature types recall_summary as str, but a caller could still hand a
    # non-str (e.g. JSON null decoded as None) — must degrade to "absent",
    # never raise.
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields(None, None)
    assert error is None
    assert summary == ""
    assert needs_tightening is False
    assert summary_missing is True


def test_validate_summary_whitespace_only_treated_as_absent():
    # Kepler's colleague-review catch on engram-alpha#2157: a whitespace-only
    # summary has nonzero raw length, so it must not dodge summary_missing by
    # sneaking past as a "present" summary.
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields(
        "   \n\t  ", None
    )
    assert error is None
    assert summary == ""
    assert needs_tightening is False
    assert summary_missing is True, (
        "whitespace-only input must strip to empty and set summary_missing, "
        "not silently clean-accept as if it were a real short summary."
    )


def test_validate_keywords_normalization():
    raw = ["  alpha  ", "beta", "beta", "", "  ", "gamma", "delta", "epsilon", "zeta"]
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields("ok", raw)
    assert error is None
    # trimmed, empties dropped, deduped, capped at 6
    assert keywords == ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"], keywords


def test_validate_keywords_absent_clean_accept():
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields("ok", None)
    assert error is None
    assert keywords == []


def test_validate_keywords_never_rejects_low_count():
    # Even 0 or 1 keywords must not error (unlike the older 3-5 tool).
    for kw in ([], ["only-one"]):
        summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields("ok", kw)
        assert error is None, (kw, error)
        assert keywords == kw or keywords == [k.strip() for k in kw if k.strip()]


def test_validate_keywords_non_list_input_normalizes_to_empty():
    # A non-list recall_keywords (e.g. a stray string) must normalize to []
    # rather than raise or reject — never-rejects contract.
    summary, keywords, needs_tightening, summary_missing, error = core._validate_recall_fields("ok", "not-a-list")
    assert error is None
    assert keywords == []


# ===========================================================================
# Section 2 — engram_add_observation (MCP-level, DB integration)
# ===========================================================================


def test_observation_summary_under_140_clean_accepts():
    srv = fresh_server()
    r = _add_obs(srv, "obs summary under 140", recall_summary="Short recall scent.",
                 recall_keywords=["kw1", "kw2", "kw3"])
    assert "error" not in r, r
    row = _row(srv, r["observation_id"])
    assert row["recall_summary"] == "Short recall scent."
    assert row["needs_tightening"] in (0, None, False)
    assert row["summary_missing"] in (0, None, False)
    assert json.loads(row["recall_keywords"]) == ["kw1", "kw2", "kw3"]


def test_observation_summary_soft_accepts_140_to_279():
    srv = fresh_server()
    text = "S" * 200
    r = _add_obs(srv, "obs summary soft accept", recall_summary=text)
    assert "error" not in r, r
    row = _row(srv, r["observation_id"])
    assert row["recall_summary"] == text
    assert row["needs_tightening"] == 1
    assert row["summary_missing"] in (0, None, False)


def test_observation_summary_hard_rejects_280_plus():
    srv = fresh_server()
    before = _node_count(srv)
    text = "R" * 280
    r = _add_obs(srv, "obs summary hard reject", recall_summary=text)
    assert "error" in r, r
    assert "280 chars" in r["error"], r
    assert "limit is 140" in r["error"], r
    after = _node_count(srv)
    assert after == before, "hard-reject must create no node (not even the evidence node's observation) — round 4 didn't disturb this"


def test_observation_summary_omitted_clean_accepts():
    """Resolution 1's core behavior — omission normalizes to 0 chars, clean-accepts.
    Round 4: omission is now flagged summary_missing=1 (distinct from
    needs_tightening=0) — a fail-loud-surface flag, not an error."""
    srv = fresh_server()
    r = _add_obs(srv, "obs summary omitted entirely")
    assert "error" not in r, r
    row = _row(srv, r["observation_id"])
    assert row["recall_summary"] is None
    assert row["needs_tightening"] in (0, None, False)
    assert row["summary_missing"] == 1
    assert row["recall_keywords"] is None


def test_observation_summary_missing_mutually_exclusive_with_tightening():
    """summary_missing and needs_tightening must never both be 1 at once
    (Kepler's ruling, #1952 PR1 round 4) — checked across all three
    recall_summary states."""
    srv = fresh_server()
    cases = {
        "missing": {},
        "clean": {"recall_summary": "Short recall scent."},
        "soft": {"recall_summary": "S" * 200},
    }
    for label, extra in cases.items():
        r = _add_obs(srv, f"mutual exclusivity case {label}", **extra)
        assert "error" not in r, (label, r)
        row = _row(srv, r["observation_id"])
        assert not (row["summary_missing"] == 1 and row["needs_tightening"] == 1), (
            label, dict(row)
        )


def test_observation_keywords_normalization_at_write_time():
    srv = fresh_server()
    raw = ["dup", "dup", " padded ", "", "three", "four", "five", "six", "seven"]
    r = _add_obs(srv, "obs keyword normalization", recall_keywords=raw)
    assert "error" not in r, r
    row = _row(srv, r["observation_id"])
    stored = json.loads(row["recall_keywords"])
    assert stored == ["dup", "padded", "three", "four", "five", "six"], stored


def test_observation_keywords_omitted_clean_accepts():
    srv = fresh_server()
    r = _add_obs(srv, "obs keywords omitted entirely")
    assert "error" not in r, r
    row = _row(srv, r["observation_id"])
    assert row["recall_keywords"] is None


# ===========================================================================
# Section 3 — engram_add_observation_batch
# ===========================================================================


def test_batch_partial_success_mixed_reject_and_omit():
    """One item >=280 chars (rejected), one item omits recall_summary entirely
    (clean-accepted) — mixes both resolved behaviors in one call. Partial-
    success semantics: the bad item errors, the other item still creates."""
    srv = fresh_server()
    observations = [
        {
            "quoted_text": "batch quote one",
            "interpretation": "batch interp one",
            "claim": "batch claim recall-reject",
            "quote_type": "personal_communication",
            "recall_summary": "B" * 280,
        },
        {
            "quoted_text": "batch quote two",
            "interpretation": "batch interp two",
            "claim": "batch claim recall-omitted",
            "quote_type": "personal_communication",
            # recall_summary/recall_keywords entirely omitted
        },
    ]
    payload = {
        "observations_json": json.dumps(observations),
        "url": "https://example.com/batch-recall-test",
        "title": "Batch Recall Test",
    }
    r = json.loads(srv.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["total"] == 2, r
    assert r["created"] == 1, r

    results = sorted(r["results"], key=lambda x: x["index"])
    assert "error" in results[0], results[0]
    assert "280 chars" in results[0]["error"], results[0]
    assert results[1].get("status") == "created", results[1]

    row = _row(srv, results[1]["observation_id"])
    assert row["recall_summary"] is None


# ===========================================================================
# Section 4 — smoke coverage across the other four write paths
# ===========================================================================


def test_definition_recall_fields_roundtrip():
    srv = fresh_server()
    r = json.loads(srv.engram_add_definition(payload_json=json.dumps({
        "term": "recall-scent",
        "definition": "A short compressed recognition cue, not full content.",
        "recall_summary": "What recall-scent means.",
        "recall_keywords": ["definition", "recall", "scent"],
    })))
    assert "error" not in r, r
    row = _row(srv, r["definition_id"])
    assert row["recall_summary"] == "What recall-scent means."
    assert json.loads(row["recall_keywords"]) == ["definition", "recall", "scent"]


def test_definition_hard_reject():
    srv = fresh_server()
    before = _node_count(srv)
    r = json.loads(srv.engram_add_definition(payload_json=json.dumps({
        "term": "over-long-term",
        "definition": "A definition whose summary is too long.",
        "recall_summary": "D" * 280,
    })))
    assert "error" in r, r
    assert "280 chars" in r["error"], r
    assert _node_count(srv) == before


def test_conjecture_recall_fields_roundtrip():
    srv = fresh_server()
    r = json.loads(srv.engram_add_conjecture(payload_json=json.dumps({
        "claim": "conjecture recall fields roundtrip",
        "basis": "testing write-time recall contract",
        "recall_summary": "Conjecture recall summary.",
        "recall_keywords": ["conj", "recall"],
    })))
    assert "error" not in r, r
    row = _row(srv, r["conjecture_id"])
    assert row["recall_summary"] == "Conjecture recall summary."
    assert json.loads(row["recall_keywords"]) == ["conj", "recall"]


def test_conjecture_hard_reject():
    srv = fresh_server()
    before = _node_count(srv)
    r = json.loads(srv.engram_add_conjecture(payload_json=json.dumps({
        "claim": "conjecture over-long summary",
        "basis": "testing hard reject",
        "recall_summary": "C" * 280,
    })))
    assert "error" in r, r
    assert "280 chars" in r["error"], r
    assert _node_count(srv) == before


def test_lesson_recall_fields_roundtrip():
    srv = fresh_server()
    incident = _add_obs(srv, "lesson incident base claim")
    assert "error" not in incident, incident
    r = json.loads(srv.engram_add_lesson(payload_json=json.dumps({
        "claim": "lesson recall fields roundtrip",
        "incident_ids": incident["observation_id"],
        "scaffolding_nudge": "Do the thing.",
        "logical_chain": "One incident generalizes to this pattern.",
        "recall_summary": "Lesson recall summary.",
        "recall_keywords": ["lesson", "recall"],
    })))
    assert "error" not in r, r
    row = _row(srv, r["lesson_id"])
    assert row["recall_summary"] == "Lesson recall summary."
    assert json.loads(row["recall_keywords"]) == ["lesson", "recall"]


def test_lesson_hard_reject():
    srv = fresh_server()
    incident = _add_obs(srv, "lesson hard reject incident claim")
    assert "error" not in incident, incident
    before = _node_count(srv)
    r = json.loads(srv.engram_add_lesson(payload_json=json.dumps({
        "claim": "lesson over-long summary",
        "incident_ids": incident["observation_id"],
        "scaffolding_nudge": "Do the thing.",
        "logical_chain": "One incident generalizes to this pattern.",
        "recall_summary": "L" * 280,
    })))
    assert "error" in r, r
    assert "280 chars" in r["error"], r
    assert _node_count(srv) == before


def test_lesson_summary_missing_flag():
    """Representative of the definition/conjecture/lesson family (#1952 PR1
    round 4): omitted recall_summary -> summary_missing=1, needs_tightening=0."""
    srv = fresh_server()
    incident = _add_obs(srv, "lesson summary missing incident claim")
    assert "error" not in incident, incident
    r = json.loads(srv.engram_add_lesson(payload_json=json.dumps({
        "claim": "lesson summary omitted entirely",
        "incident_ids": incident["observation_id"],
        "scaffolding_nudge": "Do the thing.",
        "logical_chain": "One incident generalizes to this pattern.",
        # recall_summary omitted
    })))
    assert "error" not in r, r
    row = _row(srv, r["lesson_id"])
    assert row["recall_summary"] is None
    assert row["summary_missing"] == 1
    assert row["needs_tightening"] in (0, None, False)


def test_derive_recall_fields_roundtrip():
    srv = fresh_server()
    premise = _add_obs(srv, "derive premise base claim")
    assert "error" not in premise, premise
    r = json.loads(srv.engram_derive(payload_json=json.dumps({
        "claim": "derive recall fields roundtrip",
        "supporting_ids": premise["observation_id"],
        "logical_chain": "Premise directly supports this conclusion.",
        "reasoning_type": "authority_expert",
        "recall_summary": "Derivation recall summary.",
        "recall_keywords": ["derive", "recall"],
    })))
    assert "error" not in r, r
    row = _row(srv, r["derivation_id"])
    assert row["recall_summary"] == "Derivation recall summary."
    assert json.loads(row["recall_keywords"]) == ["derive", "recall"]


def test_derive_hard_reject():
    srv = fresh_server()
    premise = _add_obs(srv, "derive hard reject premise claim")
    assert "error" not in premise, premise
    before = _node_count(srv)
    r = json.loads(srv.engram_derive(payload_json=json.dumps({
        "claim": "derive over-long summary",
        "supporting_ids": premise["observation_id"],
        "logical_chain": "Premise directly supports this conclusion.",
        "reasoning_type": "authority_expert",
        "recall_summary": "V" * 280,
    })))
    assert "error" in r, r
    assert "280 chars" in r["error"], r
    assert _node_count(srv) == before


def test_derive_summary_missing_flag():
    """#1952 PR1 round 4: omitted recall_summary on engram_derive ->
    summary_missing=1, needs_tightening=0 (mutually exclusive states)."""
    srv = fresh_server()
    premise = _add_obs(srv, "derive summary missing premise claim")
    assert "error" not in premise, premise
    r = json.loads(srv.engram_derive(payload_json=json.dumps({
        "claim": "derive summary omitted entirely",
        "supporting_ids": premise["observation_id"],
        "logical_chain": "Premise directly supports this conclusion.",
        "reasoning_type": "authority_expert",
        # recall_summary omitted
    })))
    assert "error" not in r, r
    row = _row(srv, r["derivation_id"])
    assert row["recall_summary"] is None
    assert row["summary_missing"] == 1
    assert row["needs_tightening"] in (0, None, False)
