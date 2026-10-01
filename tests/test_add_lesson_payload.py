"""Tests for engram_add_lesson single-payload signature (wave 2c-ii of ob_1499).

Coverage:
  1. Happy path — well-formed payload creates a lesson from an incident observation.
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing claim / incident_ids / scaffolding_nudge / logical_chain rejected.
  4. Empty-string guards on required string fields.
  5. Internal helper — _add_lesson_impl works with named kwargs.
  6. #1864 situation_pattern prose-shape gate — dead-prose patterns blocked by
     default, bypassable with an audit stamp, real regexes unaffected,
     malformed regexes always rejected regardless of bypass.
  7. importance_base anchor (#2268) — lessons are importance-anchored at 2.0
     via _stamp_new_node's importance_base override (one-pass write, no
     interim confidence/surprise-derived value + follow-up UPDATE).
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_lesson_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _seed_incident(server) -> str:
    """Create a claim-bearing observation node usable as an incident."""
    payload = {
        "quoted_text": "I shipped without reading the error.",
        "interpretation": "Missed contextual cue from stderr.",
        "claim": "Skipped reading error output before hypothesizing root cause.",
        "quote_type": "personal_communication",
        "url": "https://example.com/incident",
        "title": "Test incident",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def test_payload_json_creates_lesson():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = {
        "claim": "When diagnosing errors, read the output before hypothesizing.",
        "incident_ids": inc_id,
        "scaffolding_nudge": "Stop. Read the error output verbatim before forming a hypothesis.",
        "logical_chain": "Single incident → action-focused tripwire pattern.",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "lesson_id" in r and r["lesson_id"].startswith("ls_"), r
    assert r["incident_nodes"] == [inc_id]


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_lesson(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_lesson(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {
        "claim": "x", "incident_ids": "ob_0001",
        "scaffolding_nudge": "y", "logical_chain": "z",
        "bogus": "reject",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus" in r["error"]


def test_payload_json_missing_claim_rejected():
    server = fresh_server()
    payload = {
        "incident_ids": "ob_0001",
        "scaffolding_nudge": "y",
        "logical_chain": "z",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "claim" in r["error"], r


def test_payload_json_missing_scaffolding_nudge_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "incident_ids": "ob_0001",
        "logical_chain": "z",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "scaffolding_nudge" in r["error"], r


def test_payload_json_missing_logical_chain_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "incident_ids": "ob_0001",
        "scaffolding_nudge": "y",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "logical_chain" in r["error"], r


def test_payload_json_missing_incident_ids_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "scaffolding_nudge": "y",
        "logical_chain": "z",
    }
    # Per PR #68 fairy round-1: explicit empty-string guard on incident_ids
    # parallel to the other required-field guards.
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "incident_ids" in r["error"], r


def test_payload_json_whitespace_incident_ids_rejected():
    server = fresh_server()
    payload = {
        "claim": "x",
        "incident_ids": "   ",
        "scaffolding_nudge": "y",
        "logical_chain": "z",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "incident_ids" in r["error"], r


def test_payload_json_empty_claim_rejected():
    server = fresh_server()
    payload = {
        "claim": "  ",
        "incident_ids": "ob_0001",
        "scaffolding_nudge": "y",
        "logical_chain": "z",
    }
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r and "claim" in r["error"], r


def test_impl_callable_with_kwargs():
    server = fresh_server()
    inc_id = _seed_incident(server)
    r = json.loads(server._add_lesson_impl(
        claim="Internal kwargs lesson.",
        incident_ids=inc_id,
        scaffolding_nudge="testing _impl is callable.",
        logical_chain="single incident → tripwire.",
    ))
    assert "error" not in r, r
    assert "lesson_id" in r


# ---------------------------------------------------------------------------
# #1864 — situation_pattern prose-shape gate
# ---------------------------------------------------------------------------

_PROSE_PATTERN = "When about to run gh pr create, check the issue first"
_REGEX_PATTERN = r"(?:gh\s+pr\s+create|ia\s+write)"
_UNCOMPILABLE_PATTERN = "(unclosed"

# Colleague-round false-block counter-examples (#1864, Ariadne + Borges):
# real, live command-shape patterns that the first-draft discriminator
# (literal-space + no \s/\b/(?: construct) wrongly blocked. A bare
# subcommand has no reason to carry a \s/\b/(?: construct, and a
# multi-command alternation naturally carries literal spaces -- "gh pr
# diff" IS three space-separated words. The corrected threshold
# (>8 literal spaces) passes both; each carries far fewer than 8.
_SHORT_COMMAND_PATTERN = "gh issue close"
_MULTI_COMMAND_ALTERNATION_PATTERN = r"(?:gh pr diff|git diff|gh pr view)"


def _lesson_metadata(server, lesson_id: str) -> dict:
    """Read back a lesson node's metadata dict directly from the DB."""
    conn = server.core._get_db()
    try:
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (lesson_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"lesson {lesson_id} not found"
    return json.loads(row["metadata"])


def _lesson_payload(inc_id: str, **overrides) -> dict:
    payload = {
        "claim": "When diagnosing errors, read the output before hypothesizing.",
        "incident_ids": inc_id,
        "scaffolding_nudge": "Stop. Read the error output verbatim before forming a hypothesis.",
        "logical_chain": "Single incident → action-focused tripwire pattern.",
    }
    payload.update(overrides)
    return payload


def test_prose_situation_pattern_blocked_by_default():
    """A prose situation_pattern with no bypass is refused; no lesson created."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, situation_pattern=_PROSE_PATTERN)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "prose" in r["error"], r
    assert "lesson_id" not in r, r


def test_prose_situation_pattern_bypass_creates_and_stamps():
    """The SAME prose pattern with bypass_prose_check=True is created, and
    its metadata carries bypass_prose_check: True (the audit stamp)."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(
        inc_id, situation_pattern=_PROSE_PATTERN, bypass_prose_check=True,
    )
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]
    meta = _lesson_metadata(server, lesson_id)
    assert meta.get("bypass_prose_check") is True, meta
    assert meta.get("situation_pattern") == _PROSE_PATTERN, meta


def test_command_shape_pattern_no_bypass_stamp():
    """A real command-shape regex is created successfully; metadata does NOT
    carry a bypass_prose_check key at all (not even False — absent)."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, situation_pattern=_REGEX_PATTERN)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]
    meta = _lesson_metadata(server, lesson_id)
    assert "bypass_prose_check" not in meta, meta
    assert meta.get("situation_pattern") == _REGEX_PATTERN, meta


def test_short_bare_command_pattern_not_blocked():
    """Regression (#1864 colleague round): a bare command with no
    alternation and no \\s/\\b/(?: construct (e.g. `gh issue close`, 2
    literal spaces) is a real, live tripwire pattern -- the corrected
    threshold must not false-block it just because it lacks a regex
    construct the first-draft discriminator wrongly required."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, situation_pattern=_SHORT_COMMAND_PATTERN)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]
    meta = _lesson_metadata(server, lesson_id)
    assert "bypass_prose_check" not in meta, meta
    assert meta.get("situation_pattern") == _SHORT_COMMAND_PATTERN, meta


def test_multi_command_alternation_pattern_not_blocked():
    """Regression (#1864 colleague round): a multi-command alternation
    naturally carries literal spaces -- `(?:gh pr diff|git diff|gh pr view)`
    IS three space-separated words, 5 literal spaces, and a real, live
    tripwire pattern. Must not be false-blocked."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(
        inc_id, situation_pattern=_MULTI_COMMAND_ALTERNATION_PATTERN,
    )
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]
    meta = _lesson_metadata(server, lesson_id)
    assert "bypass_prose_check" not in meta, meta
    assert meta.get("situation_pattern") == _MULTI_COMMAND_ALTERNATION_PATTERN, meta


def test_uncompilable_pattern_rejected_no_bypass():
    """An uncompilable pattern is rejected with a compile-error message."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, situation_pattern=_UNCOMPILABLE_PATTERN)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "regex" in r["error"], r
    assert "lesson_id" not in r, r


def test_uncompilable_pattern_rejected_even_with_bypass():
    """bypass_prose_check=True must NEVER smuggle a malformed regex through —
    the compile check runs before, and independently of, the prose gate."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(
        inc_id, situation_pattern=_UNCOMPILABLE_PATTERN, bypass_prose_check=True,
    )
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "regex" in r["error"], r
    assert "lesson_id" not in r, r


def test_empty_situation_pattern_unaffected():
    """A lesson with no situation_pattern field still creates normally,
    exactly as today — the new gate has no effect when the field is absent."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]
    meta = _lesson_metadata(server, lesson_id)
    assert "situation_pattern" not in meta, meta
    assert "bypass_prose_check" not in meta, meta


def test_mutation_check_prose_gate_actually_blocks():
    """Mutation-verify the block gate (spec step 6): temporarily neutralize
    the prose-shape check in engram_epistemic and confirm the SAME prose
    pattern that test_prose_situation_pattern_blocked_by_default expects to
    be blocked is instead created — proving that test exercises the real
    gate rather than being a false-green.

    Implemented via monkeypatch (in-process equivalent of commenting out the
    `_looks_like_prose` call site) rather than editing the source file on
    disk, so the mutation is scoped to this one test and self-reverting.
    """
    server = fresh_server()
    inc_id = _seed_incident(server)
    epistemic_mod = sys.modules["engram_epistemic"]

    original = epistemic_mod._looks_like_prose
    try:
        # Mutation: hardcode the gate's discriminator to always report
        # "not prose" — equivalent to commenting out the real prose check.
        epistemic_mod._looks_like_prose = lambda pattern: False

        payload = _lesson_payload(inc_id, situation_pattern=_PROSE_PATTERN)
        r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
        # With the gate neutralized, the prose pattern is now let through —
        # this is the mutation-check FAILING for the original test's
        # assertion, proving the gate (not something else) was responsible.
        assert "error" not in r, (
            "mutation check did not reproduce expected bypass — "
            f"gate may not be the actual blocker: {r}"
        )
        assert "lesson_id" in r, r
    finally:
        epistemic_mod._looks_like_prose = original

    # Restore verified: same payload is blocked again with the real gate.
    payload = _lesson_payload(inc_id, situation_pattern=_PROSE_PATTERN)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "lesson_id" not in r, r


# ── 7. importance_base anchor (#2268 dead-write cleanup) ────────────────────


def _read_importance(server, node_id):
    """Read node importance_base/importance_score directly from the DB."""
    conn = sqlite3.connect(str(server.core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT importance_base, importance_score FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    finally:
        conn.close()




def test_lesson_importance_base_anchored_at_2_0():
    """Lesson creation ends at importance_base=2.0 with the matching
    decay-anchored importance_score, even though the interim
    confidence=0.5/surprise=0.0 write (#2268) is no longer performed.

    This is the correctness invariant that actually matters — the final
    stored values are unchanged from before the cleanup, not an assertion
    on the removed dead write itself.
    """
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]

    row = _read_importance(server, lesson_id)
    assert row["importance_base"] == 2.0, (
        f"Expected importance_base=2.0 (unchanged anchor value), got {row['importance_base']}"
    )
    expected_score = server.core._compute_importance(2.0, server.core._get_current_turn())
    assert row["importance_score"] == expected_score, (
        f"Expected importance_score={expected_score} (2.0 decayed to current "
        f"turn), got {row['importance_score']}"
    )
    # Never left at the interim confidence=0.5/surprise=0.0 value.
    assert row["importance_base"] != 0.5


# ---------------------------------------------------------------------------
# #1944 — prevention_type field
# ---------------------------------------------------------------------------

def test_prevention_type_defaults_to_advisory():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["prevention_type"] == "advisory", r

    meta = _lesson_metadata(server, r["lesson_id"])
    assert meta["prevention_type"] == "advisory", meta


def test_prevention_type_shadow_accepted_at_creation():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, prevention_type="shadow")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["prevention_type"] == "shadow", r


def test_prevention_type_blocking_rejected_at_creation():
    """#1944 — eligibility to block is earned, never declared at mint.
    A fresh lesson has zero fires and zero measured precision; minting
    straight to blocking would deadlock the first false positive with no
    prior warning."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, prevention_type="blocking")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "blocking" in r["error"], r
    assert "engram_update_lesson" in r["error"], r


def test_prevention_type_invalid_value_rejected():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, prevention_type="nonsense")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "Invalid prevention_type" in r["error"], r
    assert r["valid_types"] == ["advisory", "blocking", "shadow"], r


def test_prevention_type_promotable_via_update_lesson():
    """Unlike creation, engram_update_lesson MAY promote a lesson straight
    to 'blocking' — that promotion is the whole point of the field, earned
    elsewhere (a shadow window), not re-litigated by this validation."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, prevention_type="shadow")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]

    update_payload = {
        "node_id": lesson_id,
        "updates": {"prevention_type": "blocking"},
        "reason": "measured zero-false-positive shadow window",
    }
    u = json.loads(server.engram_update_lesson(payload_json=json.dumps(update_payload)))
    assert "error" not in u, u
    assert u["updated_fields"]["prevention_type"] == "blocking", u

    meta = _lesson_metadata(server, lesson_id)
    assert meta["prevention_type"] == "blocking", meta


def test_prevention_type_update_rejects_invalid_value():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]

    update_payload = {
        "node_id": lesson_id,
        "updates": {"prevention_type": "nonsense"},
    }
    u = json.loads(server.engram_update_lesson(payload_json=json.dumps(update_payload)))
    assert "error" in u, u
    assert "Invalid prevention_type" in u["error"], u


# ---------------------------------------------------------------------------
# alpha#3270/#3112 — tripwire_priority field
# ---------------------------------------------------------------------------

def test_tripwire_priority_defaults_to_normal():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["tripwire_priority"] == "normal", r

    meta = _lesson_metadata(server, r["lesson_id"])
    assert meta["tripwire_priority"] == "normal", meta


def test_tripwire_priority_high_accepted_at_creation():
    """Unlike prevention_type's 'blocking', 'high' carries no earn-it gate —
    it's the author's own interruption preference, mintable directly."""
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, tripwire_priority="high")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r["tripwire_priority"] == "high", r

    meta = _lesson_metadata(server, r["lesson_id"])
    assert meta["tripwire_priority"] == "high", meta


def test_tripwire_priority_invalid_value_rejected():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, tripwire_priority="destructive")
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" in r, r
    assert "Invalid tripwire_priority" in r["error"], r
    assert r["valid_types"] == ["high", "normal"], r


def test_tripwire_priority_settable_via_update_lesson():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]

    update_payload = {
        "node_id": lesson_id,
        "updates": {"tripwire_priority": "high"},
        "reason": "fires rarely, guards a destructive cross-worktree action",
    }
    u = json.loads(server.engram_update_lesson(payload_json=json.dumps(update_payload)))
    assert "error" not in u, u
    assert u["updated_fields"]["tripwire_priority"] == "high", u

    meta = _lesson_metadata(server, lesson_id)
    assert meta["tripwire_priority"] == "high", meta


def test_tripwire_priority_update_rejects_invalid_value():
    server = fresh_server()
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    lesson_id = r["lesson_id"]

    update_payload = {
        "node_id": lesson_id,
        "updates": {"tripwire_priority": "destructive"},
    }
    u = json.loads(server.engram_update_lesson(payload_json=json.dumps(update_payload)))
    assert "error" in u, u
    assert "Invalid tripwire_priority" in u["error"], u
