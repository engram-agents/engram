"""Tests for engram_add_evidence single-payload signature (wave 3 of ob_1499 — issue #99).

The migration unifies the 5-arg surface (url, title, domain, source_date,
content_snippet) into one payload_json object. Same pattern as PR #110 (derive)
and PR #111 (add_observation_batch).

engram_add_evidence is not exposed as an @mcp.tool decorator, but it IS
callable through EngramClient.call() via the TOOL_SCHEMAS dispatch table —
the migration matters for the public client surface and for in-server callers
(now routed through _add_evidence_impl).

Coverage:
  1. Happy path — well-formed payload creates evidence (uses file:// to skip DNS).
  2. Schema enforcement — invalid JSON, non-object, unknown fields.
  3. Required-field validation — missing url / title rejected.
  4. Internal helper — _add_evidence_impl callable with named kwargs.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_add_evidence_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))
TEST_FILE = os.path.join(TEST_DIR, "evidence_source.md")


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
    # Local file source — file:// URLs skip DNS validation. The file lives
    # outside any git repo so _capture_file_version returns ok=True with no
    # version data required (versioning only applies inside git).
    with open(TEST_FILE, "w") as f:
        f.write("Test source content for add_evidence payload test.\n")
    return server


def _file_url():
    return f"file://{TEST_FILE}"


def test_payload_json_creates_evidence():
    server = fresh_server()
    payload = {
        "url": _file_url(),
        "title": "Test source for add_evidence payload test",
    }
    r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert "evidence_id" in r and r["evidence_id"].startswith("ev_"), r
    assert r.get("status") == "created"


def test_payload_json_reuses_existing_evidence_for_same_url():
    """Same URL → same evidence node (URL-keyed reuse semantics)."""
    server = fresh_server()
    payload = {
        "url": _file_url(),
        "title": "Test source",
    }
    r1 = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
    assert r1.get("status") == "created", r1
    first_id = r1["evidence_id"]

    r2 = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
    assert r2.get("status") == "already_exists", r2
    assert r2["evidence_id"] == first_id, r2


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_evidence(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_evidence(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_fields_rejected():
    server = fresh_server()
    payload = {
        "url": _file_url(),
        "title": "Test source",
        "bogus_field": "reject me",
    }
    r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "bogus_field" in r["error"]


def test_payload_json_missing_url_rejected():
    server = fresh_server()
    payload = {"title": "Test source"}
    r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
    assert "error" in r and "url" in r["error"], r


def test_payload_json_missing_title_rejected():
    server = fresh_server()
    payload = {"url": _file_url()}
    r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
    assert "error" in r and "title" in r["error"], r


def test_payload_json_with_optional_fields():
    """All optional fields round-trip cleanly through the payload."""
    server = fresh_server()
    payload = {
        "url": _file_url(),
        "title": "Test source",
        "domain": "test-domain.local",
        "source_date": "2026-05-14",
        "content_snippet": "Brief excerpt for offline re-reading.",
    }
    r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    assert r.get("status") == "created"
    assert r.get("domain") == "test-domain.local"
    assert r.get("source_date") == "2026-05-14"


def test_payload_json_whitespace_only_url_rejected():
    """Lock in the .strip() guard on required fields — a whitespace-only url
    must be rejected, not silently treated as 'present'. If a future
    simplification swaps `if not url or not url.strip()` for `if not url`,
    this test catches the regression. Fairy round-1 suggestion on PR #113."""
    server = fresh_server()
    for bad_url in ["   ", "\t", "\n", "  \t \n  "]:
        payload = {"url": bad_url, "title": "Test source"}
        r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
        assert "error" in r, f"url={bad_url!r} should be rejected: {r}"
        assert "url" in r["error"].lower(), r
    # Symmetric check on title — whitespace-only title with valid url.
    for bad_title in ["   ", "\t"]:
        payload = {"url": _file_url(), "title": bad_title}
        r = json.loads(server.engram_add_evidence(payload_json=json.dumps(payload)))
        assert "error" in r, f"title={bad_title!r} should be rejected: {r}"
        assert "title" in r["error"].lower(), r


def test_impl_callable_with_kwargs():
    """In-server callers should be able to call _add_evidence_impl directly
    with named kwargs — this is the path engram_add_observation and
    engram_add_observation_batch take."""
    server = fresh_server()
    r = json.loads(server._add_evidence_impl(
        url=_file_url(),
        title="Internal kwargs path test",
    ))
    assert "error" not in r, r
    assert "evidence_id" in r and r["evidence_id"].startswith("ev_"), r
    assert r.get("status") == "created"
