"""Tests for engram_update_lesson (#1826 gap-2 fix).

engram_add_lesson only accepts situation_pattern at creation time; this tool
backfills/corrects it on an existing lesson node, in-place (no supersede).
Mirrors engram_update_person's pattern; situation_pattern updates run the
SAME #1864 prose-shape gate _add_lesson_impl runs at creation.

Coverage:
  1. Happy path — set a valid regex, verify stored + field_traces recorded.
  2. Happy path — clear situation_pattern (empty string) on a lesson that
     currently has a prose pattern; no prose-gate error.
  3. Reject — prose-shaped value without bypass_prose_check.
  4. Accept — prose-shaped value WITH bypass_prose_check=true; stamped.
  5. Reject — invalid regex, regardless of bypass_prose_check.
  6. Reject — unknown field in updates dict.
  7. Reject — node_id not found / not type='lesson' / not is_current=1.
  8. Reject — bypass_prose_check non-bool (bool-string truthy trap).
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_update_lesson_")
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


_PROSE_PATTERN = "When about to run gh pr create, check the issue first"
_REGEX_PATTERN = r"(?:gh\s+pr\s+create|ia\s+write)"
_UNCOMPILABLE_PATTERN = "(unclosed"


def _lesson_payload(inc_id: str, **overrides) -> dict:
    payload = {
        "claim": "When diagnosing errors, read the output before hypothesizing.",
        "incident_ids": inc_id,
        "scaffolding_nudge": "Stop. Read the error output verbatim before forming a hypothesis.",
        "logical_chain": "Single incident → action-focused tripwire pattern.",
    }
    payload.update(overrides)
    return payload


def _make_lesson(server, **overrides) -> str:
    inc_id = _seed_incident(server)
    payload = _lesson_payload(inc_id, **overrides)
    r = json.loads(server.engram_add_lesson(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["lesson_id"]


def _lesson_metadata(server, lesson_id: str) -> dict:
    conn = server.core._get_db()
    try:
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (lesson_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"lesson {lesson_id} not found"
    return json.loads(row["metadata"])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_set_valid_regex_traces_prior_value():
    server = fresh_server()
    lesson_id = _make_lesson(server)  # no situation_pattern initially

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _REGEX_PATTERN},
        "reason": "backfilling per #1826",
    })))
    assert "error" not in upd, upd
    assert upd["status"] == "updated"
    assert upd["updated_fields"]["situation_pattern"] == _REGEX_PATTERN
    assert upd["field_traces_delta"]["situation_pattern"]["prior_value"] is None

    meta = _lesson_metadata(server, lesson_id)
    assert meta["situation_pattern"] == _REGEX_PATTERN
    assert meta["field_traces"]["situation_pattern"] == [
        {
            "prior_value": None,
            "changed_at": upd["field_traces_delta"]["situation_pattern"]["changed_at"],
        }
    ]


def test_field_traces_append_only_across_updates():
    server = fresh_server()
    lesson_id = _make_lesson(server, situation_pattern=_REGEX_PATTERN)

    other_pattern = r"gh\s+issue\s+close"
    server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": other_pattern},
    }))

    meta = _lesson_metadata(server, lesson_id)
    traces = meta["field_traces"]["situation_pattern"]
    assert len(traces) == 1, traces
    assert traces[0]["prior_value"] == _REGEX_PATTERN
    assert meta["situation_pattern"] == other_pattern


def test_clear_situation_pattern_on_prose_lesson_no_gate_error():
    """A lesson currently holding a prose-shaped pattern (created via bypass)
    can be cleared to "" with no prose-gate error."""
    server = fresh_server()
    lesson_id = _make_lesson(
        server, situation_pattern=_PROSE_PATTERN, bypass_prose_check=True,
    )
    meta_before = _lesson_metadata(server, lesson_id)
    assert meta_before["situation_pattern"] == _PROSE_PATTERN

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": ""},
    })))
    assert "error" not in upd, upd
    assert upd["updated_fields"]["situation_pattern"] == ""

    meta = _lesson_metadata(server, lesson_id)
    assert meta["situation_pattern"] == ""


# ---------------------------------------------------------------------------
# Prose gate
# ---------------------------------------------------------------------------

def test_prose_value_rejected_without_bypass():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _PROSE_PATTERN},
    })))
    assert "error" in upd, upd
    assert "prose" in upd["error"], upd

    meta = _lesson_metadata(server, lesson_id)
    assert "situation_pattern" not in meta, meta


def test_prose_value_with_bypass_stored_and_stamped():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _PROSE_PATTERN},
        "bypass_prose_check": True,
    })))
    assert "error" not in upd, upd
    assert upd["updated_fields"]["situation_pattern"] == _PROSE_PATTERN

    meta = _lesson_metadata(server, lesson_id)
    assert meta["situation_pattern"] == _PROSE_PATTERN
    assert meta.get("bypass_prose_check") is True, meta


def test_invalid_regex_rejected_no_bypass():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _UNCOMPILABLE_PATTERN},
    })))
    assert "error" in upd, upd
    assert "regex" in upd["error"], upd


def test_invalid_regex_rejected_even_with_bypass():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _UNCOMPILABLE_PATTERN},
        "bypass_prose_check": True,
    })))
    assert "error" in upd, upd
    assert "regex" in upd["error"], upd


def test_bypass_prose_check_non_bool_rejected():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _PROSE_PATTERN},
        "bypass_prose_check": "false",
    })))
    assert "error" in upd, upd
    assert "bypass_prose_check" in upd["error"], upd
    assert "boolean" in upd["error"], upd


# ---------------------------------------------------------------------------
# Field / node validation
# ---------------------------------------------------------------------------

def test_unknown_field_rejected():
    """claim is epistemic content -- stays supersede-only, never mutable here."""
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"claim": "a rewritten claim"},
    })))
    assert "error" in upd, upd
    assert "claim" in upd["error"], upd
    assert "situation_pattern" in upd["error"], upd
    assert "scaffolding_nudge" in upd["error"], upd


def test_node_not_found_rejected():
    server = fresh_server()
    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": "ls_9999",
        "updates": {"situation_pattern": _REGEX_PATTERN},
    })))
    assert "error" in upd, upd
    assert "not found" in upd["error"], upd


def test_node_wrong_type_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({"name": "NotALesson"})))
    assert "error" not in r, r
    pid = r["person_id"]

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": pid,
        "updates": {"situation_pattern": _REGEX_PATTERN},
    })))
    assert "error" in upd, upd
    assert "not \'lesson\'" in upd["error"] or "not 'lesson'" in upd["error"], upd


def test_node_not_current_rejected():
    """A superseded lesson (is_current=0) is rejected, pointing at
    engram_inspect for the current successor."""
    server = fresh_server()
    lesson_id = _make_lesson(server)

    # engram_supersede is pure-wire: create the replacement lesson first via
    # its canonical creation tool, then wire the supersedes edge.
    inc_id = _seed_incident(server)
    new_lesson = json.loads(server.engram_add_lesson(payload_json=json.dumps(
        _lesson_payload(
            inc_id,
            claim="When diagnosing errors, read the output before hypothesizing (v2).",
        )
    )))
    assert "error" not in new_lesson, new_lesson
    new_lesson_id = new_lesson["lesson_id"]

    sup = json.loads(server.engram_supersede(payload_json=json.dumps({
        "old_node_id": lesson_id,
        "new_node_id": new_lesson_id,
        "supersede_reason": "wording refinement",
    })))
    assert "error" not in sup, sup

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _REGEX_PATTERN},
    })))
    assert "error" in upd, upd
    assert "not current" in upd["error"], upd
    assert "engram_inspect" in upd["error"], upd


# ---------------------------------------------------------------------------
# Payload-level validation (mirrors engram_update_person's wrapper tests)
# ---------------------------------------------------------------------------

def test_updates_must_be_dict():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": "situation_pattern=foo",
    })))
    assert "error" in upd, upd
    assert "JSON object" in upd["error"], upd


def test_unknown_payload_field_rejected():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _REGEX_PATTERN},
        "bogus": "reject",
    })))
    assert "error" in upd, upd
    assert "Unknown fields" in upd["error"], upd
    assert "bogus" in upd["error"], upd


def test_wrapper_empty_node_id_rejected():
    server = fresh_server()
    _make_lesson(server)  # ensure a lesson exists; irrelevant to this call

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": "",
        "updates": {"situation_pattern": _REGEX_PATTERN},
    })))
    assert "error" in upd, upd
    assert "node_id is required" in upd["error"], upd


def test_wrapper_empty_updates_rejected():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {},
    })))
    assert "error" in upd, upd
    assert "non-empty dict" in upd["error"], upd

# ---------------------------------------------------------------------------
# scaffolding_nudge (widened 2026-08-03 per Lei — mechanical/actionability
# metadata, same non-epistemic class as situation_pattern, no gate)
# ---------------------------------------------------------------------------

def test_set_scaffolding_nudge_traces_prior_value():
    server = fresh_server()
    lesson_id = _make_lesson(
        server, scaffolding_nudge="Original nudge text."
    )

    new_nudge = "Stop and re-read the error output before hypothesizing — verbatim."
    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"scaffolding_nudge": new_nudge},
        "reason": "sharpened after a real firing that wasn't specific enough",
    })))
    assert "error" not in upd, upd
    assert upd["status"] == "updated"
    assert upd["updated_fields"]["scaffolding_nudge"] == new_nudge
    assert upd["field_traces_delta"]["scaffolding_nudge"]["prior_value"] == "Original nudge text."

    meta = _lesson_metadata(server, lesson_id)
    assert meta["scaffolding_nudge"] == new_nudge


def test_scaffolding_nudge_has_no_prose_gate():
    """scaffolding_nudge is free text -- a long, space-heavy value (which
    would trip #1864's prose gate on situation_pattern) must NOT be rejected."""
    server = fresh_server()
    lesson_id = _make_lesson(server)

    long_prose = (
        "About to hypothesize a root cause without first reading the full "
        "error output verbatim, including any stack trace context lines"
    )
    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"scaffolding_nudge": long_prose},
    })))
    assert "error" not in upd, upd
    assert upd["updated_fields"]["scaffolding_nudge"] == long_prose


def test_both_mutable_fields_in_one_call():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {
            "situation_pattern": _REGEX_PATTERN,
            "scaffolding_nudge": "Do the corrective action now.",
        },
    })))
    assert "error" not in upd, upd
    assert set(upd["updated_fields"].keys()) == {"situation_pattern", "scaffolding_nudge"}

    meta = _lesson_metadata(server, lesson_id)
    assert meta["situation_pattern"] == _REGEX_PATTERN
    assert meta["scaffolding_nudge"] == "Do the corrective action now."


# ---------------------------------------------------------------------------
# edit_history audit logging (Lei: updates should still show up as a graph
# mutation event, not just a metadata change invisible outside the node)
# ---------------------------------------------------------------------------

def test_update_logged_to_edit_history():
    server = fresh_server()
    lesson_id = _make_lesson(server)

    server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"situation_pattern": _REGEX_PATTERN},
        "reason": "backfilling per #1826",
    }))

    conn = server.core._get_db()
    try:
        rows = conn.execute(
            "SELECT action, node_id, node_type, details FROM edit_history "
            "WHERE node_id = ? AND action = 'lesson_field_updated'",
            (lesson_id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, rows
    row = rows[0]
    assert row["node_type"] == "lesson"
    details = json.loads(row["details"])
    assert details["updated_fields"] == {"situation_pattern": _REGEX_PATTERN}
    assert details["reason"] == "backfilling per #1826"


def test_edit_history_entry_per_call_not_per_field():
    """Two fields updated in one call -> one edit_history row, not two --
    matches engram_update_person's per-call (not per-field) logging shape."""
    server = fresh_server()
    lesson_id = _make_lesson(server)

    server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {
            "situation_pattern": _REGEX_PATTERN,
            "scaffolding_nudge": "Do the corrective action now.",
        },
    }))

    conn = server.core._get_db()
    try:
        rows = conn.execute(
            "SELECT details FROM edit_history WHERE node_id = ? "
            "AND action = 'lesson_field_updated'",
            (lesson_id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, rows
    details = json.loads(rows[0]["details"])
    assert set(details["updated_fields"].keys()) == {"situation_pattern", "scaffolding_nudge"}


# ---------------------------------------------------------------------------
# alpha#3920 (part of #3911): match_scope
# ---------------------------------------------------------------------------

def test_match_scope_full_is_stored_and_traced():
    server = fresh_server()
    lesson_id = _make_lesson(server, situation_pattern=_REGEX_PATTERN)

    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id,
        "updates": {"match_scope": "full"},
        "reason": "depends on body text (#3920)",
    })))
    assert "error" not in upd, upd
    assert upd["updated_fields"]["match_scope"] == "full"
    assert upd["field_traces_delta"]["match_scope"]["prior_value"] is None

    meta = _lesson_metadata(server, lesson_id)
    assert meta["match_scope"] == "full"
    assert meta["situation_pattern"] == _REGEX_PATTERN  # untouched


def test_match_scope_command_is_accepted():
    server = fresh_server()
    lesson_id = _make_lesson(server, situation_pattern=_REGEX_PATTERN)
    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id, "updates": {"match_scope": "command"},
    })))
    assert "error" not in upd, upd
    assert _lesson_metadata(server, lesson_id)["match_scope"] == "command"


def test_match_scope_unknown_value_is_rejected_and_nothing_is_stored():
    server = fresh_server()
    lesson_id = _make_lesson(server, situation_pattern=_REGEX_PATTERN)
    upd = json.loads(server.engram_update_lesson(payload_json=json.dumps({
        "node_id": lesson_id, "updates": {"match_scope": "cmd"},
    })))
    assert "Invalid match_scope" in upd.get("error", ""), upd
    assert upd["valid_types"] == ["command", "full"]
    assert "match_scope" not in _lesson_metadata(server, lesson_id)
