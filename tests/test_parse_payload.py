"""Tests for the shared _parse_payload() helper (issue #1683).

Issue #1683: 52 @mcp.tool()-decorated functions each inlined an identical
try/except block to parse `payload_json` that caught only
`json.JSONDecodeError`. When `payload_json` arrives as a non-str value (a
dict, None, int — an MCP-boundary mishap), `json.loads` raises `TypeError`,
which escaped uncaught and produced a 320-error burst on `engram_surface`
(all duration_ms=0). This suite covers the extracted `_parse_payload()`
helper directly, plus the regression it fixes on 5 representative migrated
tool call sites.

Coverage:
  1. `_parse_payload()` unit tests — all three branches (success,
     JSONDecodeError, non-dict).
  2. `_parse_payload()` TypeError branch — non-str payload_json (dict, None,
     int) returns the standard error envelope instead of raising.
  3. Regression coverage on 5 representative migrated tools (engram_surface,
     engram_inspect, and 3 others) — non-str payload_json (dict, None, int)
     must return a JSON-parseable {"error": ...} envelope, not an uncaught
     exception.
  4. Malformed-JSON-string coverage on the same 5 tools — the
     JSONDecodeError path is still intact post-migration.
  5. Non-object-JSON coverage on the same 5 tools — the isinstance(dict)
     check is still intact post-migration.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_parse_payload_")
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


# ---------------------------------------------------------------------------
# 1 & 2. _parse_payload() unit tests — all branches, including TypeError
# ---------------------------------------------------------------------------

def test_parse_payload_success():
    """Valid JSON object string parses to (dict, None)."""
    server = fresh_server()
    params, err = server._parse_payload(json.dumps({"a": 1, "b": "two"}))
    assert err is None, f"Expected no error, got: {err}"
    assert params == {"a": 1, "b": "two"}


def test_parse_payload_json_decode_error():
    """Malformed JSON string returns (None, error-envelope) with 'Invalid JSON'."""
    server = fresh_server()
    params, err = server._parse_payload("{bad")
    assert params is None
    assert err is not None
    r = json.loads(err)
    assert "error" in r
    assert "Invalid JSON in payload_json" in r["error"]


def test_parse_payload_non_dict():
    """Valid JSON that isn't an object (e.g. a bare number) returns the
    'must be a JSON object' error envelope."""
    server = fresh_server()
    params, err = server._parse_payload("123")
    assert params is None
    assert err is not None
    r = json.loads(err)
    assert "error" in r
    assert r["error"] == "payload_json must be a JSON object"


def test_parse_payload_type_error_dict():
    """A dict passed directly (not a JSON string) raises TypeError inside
    json.loads — must be caught and return the standard error envelope,
    not propagate uncaught (the core #1683 regression)."""
    server = fresh_server()
    params, err = server._parse_payload({"already": "a dict"})
    assert params is None
    assert err is not None
    r = json.loads(err)
    assert "error" in r
    assert "Invalid JSON in payload_json" in r["error"]


def test_parse_payload_type_error_none():
    """payload_json=None raises TypeError inside json.loads — must be caught."""
    server = fresh_server()
    params, err = server._parse_payload(None)
    assert params is None
    assert err is not None
    r = json.loads(err)
    assert "error" in r
    assert "Invalid JSON in payload_json" in r["error"]


def test_parse_payload_type_error_int():
    """payload_json=123 (an int) raises TypeError inside json.loads — must be caught."""
    server = fresh_server()
    params, err = server._parse_payload(123)
    assert params is None
    assert err is not None
    r = json.loads(err)
    assert "error" in r
    assert "Invalid JSON in payload_json" in r["error"]


# ---------------------------------------------------------------------------
# 3. Regression coverage — non-str payload_json on 5 representative tools
# ---------------------------------------------------------------------------

# 5 representative migrated tools spanning different families:
#   engram_surface (query/recall), engram_inspect (query/recall),
#   engram_query (query/recall), engram_add_observation (ingestion),
#   engram_derive (derivation).
_REPRESENTATIVE_TOOLS = [
    "engram_surface",
    "engram_inspect",
    "engram_query",
    "engram_add_observation",
    "engram_derive",
]

_NON_STR_PAYLOADS = [
    ("dict", {"already": "a dict"}),
    ("none", None),
    ("int", 123),
]


def test_non_str_payload_returns_error_envelope_not_exception():
    """For each representative tool, calling with a non-str payload_json
    (dict, None, int) must return a JSON-parseable {"error": ...} envelope
    rather than raising an uncaught exception. This is the #1683 regression:
    prior to the fix, TypeError from json.loads() escaped uncaught here."""
    server = fresh_server()
    for tool_name in _REPRESENTATIVE_TOOLS:
        tool_fn = getattr(server, tool_name)
        for label, bad_payload in _NON_STR_PAYLOADS:
            try:
                result_str = tool_fn(payload_json=bad_payload)
            except Exception as exc:  # pragma: no cover - the regression itself
                raise AssertionError(
                    f"{tool_name}(payload_json={label}) raised {type(exc).__name__}: "
                    f"{exc!r} instead of returning an error envelope"
                )
            r = json.loads(result_str)
            assert "error" in r, (
                f"{tool_name}(payload_json={label}) did not return an error envelope: {r}"
            )


# ---------------------------------------------------------------------------
# 4. Malformed-JSON-string coverage — JSONDecodeError path still intact
# ---------------------------------------------------------------------------

def test_malformed_json_string_still_rejected():
    """Malformed JSON string still returns 'Invalid JSON in payload_json'
    for each representative tool, post-migration."""
    server = fresh_server()
    for tool_name in _REPRESENTATIVE_TOOLS:
        tool_fn = getattr(server, tool_name)
        result_str = tool_fn(payload_json="{bad")
        r = json.loads(result_str)
        assert "error" in r, f"{tool_name}: expected error key, got: {r}"
        assert "Invalid JSON in payload_json" in r["error"], (
            f"{tool_name}: expected 'Invalid JSON in payload_json', got: {r['error']}"
        )


# ---------------------------------------------------------------------------
# 5. Non-object JSON coverage — isinstance(dict) check still intact
# ---------------------------------------------------------------------------

def test_non_object_json_still_rejected():
    """Valid-JSON-but-non-object payload (e.g. a bare number) still returns
    'payload_json must be a JSON object' for each representative tool,
    post-migration."""
    server = fresh_server()
    for tool_name in _REPRESENTATIVE_TOOLS:
        tool_fn = getattr(server, tool_name)
        result_str = tool_fn(payload_json="123")
        r = json.loads(result_str)
        assert "error" in r, f"{tool_name}: expected error key, got: {r}"
        assert "payload_json must be a JSON object" in r["error"], (
            f"{tool_name}: expected 'payload_json must be a JSON object', got: {r['error']}"
        )


# ---------------------------------------------------------------------------
# 6. engram_set_recall_summaries — delegation-boundary regression (#1683)
# ---------------------------------------------------------------------------
#
# engram_set_recall_summaries (server.py) delegates straight to
# _set_recall_summaries_impl in engram_recall_summaries.py, which parses
# payload_json independently of the shared _parse_payload() helper. A
# reviewer found this delegation boundary still had the vulnerable
# except-JSONDecodeError-only pattern, so a non-str payload_json reached this
# tool via a registered MCP tool without being caught by the round-1 fix.


def test_set_recall_summaries_non_str_payload_returns_error_envelope():
    """engram_set_recall_summaries with a non-str payload_json (dict, None,
    int) must return a JSON-parseable {"error": ...} envelope rather than
    raising an uncaught exception."""
    server = fresh_server()
    for label, bad_payload in _NON_STR_PAYLOADS:
        try:
            result_str = server.engram_set_recall_summaries(payload_json=bad_payload)
        except Exception as exc:  # pragma: no cover - the regression itself
            raise AssertionError(
                f"engram_set_recall_summaries(payload_json={label}) raised "
                f"{type(exc).__name__}: {exc!r} instead of returning an error envelope"
            )
        r = json.loads(result_str)
        assert "error" in r, (
            f"engram_set_recall_summaries(payload_json={label}) did not return "
            f"an error envelope: {r}"
        )


# ---------------------------------------------------------------------------
# 7. alpha#1944 — override_lesson stripped centrally
# ---------------------------------------------------------------------------
#
# override_lesson is a reserved field the PreToolUse lesson-tripwire hook
# reads directly off the raw tool_input to let a single call through a
# "blocking" lesson (see engram-lesson-tripwire-hook.py's check_blocking_deny
# / _find_override). By the time _parse_payload runs, the hook has already
# made its allow/deny decision on the RAW payload string — this field has
# done its job and must not reach any tool's own unknown-field check.


def test_override_lesson_stripped_from_params():
    server = fresh_server()
    payload = {"a": 1, "override_lesson": "ls_0018: verified safe"}
    params, err = server._parse_payload(json.dumps(payload))
    assert err is None, err
    assert params == {"a": 1}, params


def test_override_lesson_absent_is_a_no_op():
    """The overwhelming majority of calls never carry override_lesson —
    confirm the pop() doesn't touch anything when the key is absent."""
    server = fresh_server()
    payload = {"a": 1, "b": "two"}
    params, err = server._parse_payload(json.dumps(payload))
    assert err is None, err
    assert params == {"a": 1, "b": "two"}, params


def test_override_lesson_does_not_trip_unknown_field_check():
    """End-to-end: a representative tool's own unknown-field check never
    sees override_lesson, so a call carrying it is not rejected for that
    reason (it may still be rejected for missing required fields — this
    only proves the field itself isn't the cause)."""
    server = fresh_server()
    payload = {"node_id": "ob_0001", "override_lesson": "ls_0018: verified safe"}
    result_str = server.engram_inspect(payload_json=json.dumps(payload))
    r = json.loads(result_str)
    # Whatever the real outcome (node not found, etc.), it must not be
    # "Unknown fields" naming override_lesson.
    if "error" in r:
        assert "override_lesson" not in r["error"], r
