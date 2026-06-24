"""Tests for the engram_add_observation single-payload signature (ob_1499 + ls_0023).

The MCP-exposed engram_add_observation now takes one parameter:
    payload_json: str  (a JSON object with the observation fields)

This eliminates the antml-prefix multi-parameter swallow risk that the
N-parameter signature was prone to (~26% of add_observation calls were
auto-repaired by hooks/engram-toolcall-repair.py per 7-day telemetry as
of 2026-05-08). Internal callers continue to use _add_observation_impl
with named kwargs.

Coverage:
  1. Happy path — well-formed payload creates an observation.
  2. Schema enforcement — invalid JSON, non-object payload, unknown fields.
  3. Required-field validation — missing required fields are reported.
  4. Field validation — invalid quote_type / source_class still rejected.
  5. Internal helper — _add_observation_impl works with named kwargs.
  6. Client backward compatibility — EngramClient.call auto-wraps legacy
     dict form into payload_json so pre-migration callers keep working.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_obs_")
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


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_creates_observation():
    server = fresh_server()
    payload = {
        "url": "https://example.com/article",
        "title": "Example",
        "quoted_text": "X says Y",
        "interpretation": "X claims that Y is true",
        "claim": "Y is true according to X",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert r["observation_id"].startswith("ob_"), r
    assert r.get("evidence_id", "").startswith("ev_"), r


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    # JSON array is valid JSON but not an object → reject
    r = json.loads(server.engram_add_observation(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_field_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data", "url": "https://x.com", "title": "T",
        "made_up_field": "value",
    })))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "made_up_field" in r["error"], r


# ── 3. Required-field validation ───────────────────────────────────────────


def test_payload_json_missing_required():
    server = fresh_server()
    # Empty payload — missing all four required fields
    r = json.loads(server.engram_add_observation(payload_json="{}"))
    assert "error" in r and "Missing required fields" in r["error"], r
    for field in ("quoted_text", "interpretation", "claim", "quote_type"):
        assert field in r["error"], f"{field} not mentioned: {r['error']}"


def test_payload_json_partial_missing():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q",
        "interpretation": "i",
        # claim + quote_type missing
        "url": "https://x.com",
        "title": "T",
    })))
    assert "error" in r and "Missing required fields" in r["error"], r
    assert "claim" in r["error"] and "quote_type" in r["error"], r["error"]


# ── 4. Field-value validation (still applies through impl) ────────────────


def test_payload_json_invalid_quote_type():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "fabricated_type",
        "url": "https://x.com", "title": "T",
    })))
    assert "error" in r and "Invalid quote_type" in r["error"], r


def test_payload_json_invalid_source_class():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data",
        "url": "https://x.com", "title": "T",
        "source_class": "made_up_class",
    })))
    assert "error" in r and "Invalid source_class" in r["error"], r


def test_payload_json_missing_evidence_source():
    """Neither url nor evidence_id provided → impl returns error."""
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data",
        # No url, no evidence_id
    })))
    assert "error" in r, r
    assert "url" in r["error"] or "evidence_id" in r["error"], r["error"]


# ── 5. Internal impl callable with kwargs (in-server callers) ─────────────


def test_internal_impl_accepts_kwargs():
    server = fresh_server()
    r = json.loads(server._add_observation_impl(
        url="https://example.com/internal",
        title="Internal call",
        quoted_text="internal payload",
        interpretation="exercising the impl directly",
        claim="impl callable with kwargs",
        quote_type="attributed_analysis",
    ))
    assert r.get("status") == "created", r


# ── 6. EngramClient backward compatibility ────────────────────────────────


def test_client_call_legacy_dict_autowrapped():
    """EngramClient.call should auto-wrap legacy named-field dict for tools
    whose schema declares only payload_json. This keeps pre-migration
    callers working without each having to know about the JSON shape."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    client = EngramClient(db_dir=TEST_DIR)

    # Pass legacy named-field dict — should auto-wrap into payload_json
    r = client.call("engram_add_observation", {
        "url": "https://example.com/legacy",
        "title": "Legacy form",
        "quoted_text": "still works",
        "interpretation": "legacy callers preserved",
        "claim": "Legacy dict form auto-wrapped",
        "quote_type": "hard_data",
    })
    assert r.get("status") == "created", f"Legacy dict should auto-wrap: {r}"


def test_client_call_explicit_payload_json_passthrough():
    """When the caller already passes payload_json, don't double-wrap."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    client = EngramClient(db_dir=TEST_DIR)

    r = client.call("engram_add_observation", {
        "payload_json": json.dumps({
            "url": "https://example.com/explicit",
            "title": "Explicit form",
            "quoted_text": "passes through unchanged",
            "interpretation": "no double-wrap",
            "claim": "Explicit payload_json passthrough",
            "quote_type": "hard_data",
        }),
    })
    assert r.get("status") == "created", f"Explicit payload_json should pass through: {r}"


# ── 7. source_repo field ───────────────────────────────────────────────────


def test_source_repo_accepted_as_known_field():
    """source_repo and repo_path are in _ADD_OBSERVATION_FIELDS — must not be rejected as unknown."""
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data",
        "url": "https://example.com/doc",
        "title": "T",
        "source_repo": "",
        "repo_path": "",
    })))
    assert "Unknown fields" not in r.get("error", ""), (
        f"source_repo/repo_path should be known fields, not rejected as unknown: {r}"
    )


def test_source_repo_nonexistent_dir_rejected():
    """source_repo pointing to a non-existent path returns a 'not a directory' error."""
    server = fresh_server()
    content = "The rate of Y is 42%."
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        f.write(content)
        f.close()
        tmp_path = f.name
        r = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "quoted_text": content,
            "interpretation": "Known rate.",
            "claim": "Rate of Y is 42%",
            "quote_type": "hard_data",
            "url": f"file://{tmp_path}",
            "title": "Test temp file",
            "source_repo": "/nonexistent/no/such/dir",
        })))
        assert "error" in r, f"Expected error for bad source_repo, got: {r}"
        assert "not a directory" in r["error"], (
            f"Expected 'not a directory' in error message: {r['error']}"
        )
    finally:
        os.unlink(f.name)
