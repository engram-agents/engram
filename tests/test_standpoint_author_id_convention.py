"""Tests for the standpoint_author_id stable-ID convention (#1348).

The warn-not-reject check emits a warning in the result under the "warnings"
key when standpoint_author_id looks like a raw display name, and proceeds
with the write regardless. No write is ever blocked by this check.

Coverage:
  1. Valid pn_* node ID → no warning
  2. Valid namespaced strings (orcid:…, doi-author:…, domain:handle) → no warning
  3. Raw display name with space (e.g. "John Smith") → warning emitted
  4. Raw single-word name (e.g. "Lei") → warning emitted
  5. Empty string → no warning (the field is optional)
  6. None / omitted → no warning (the field is optional)
  7. Write always proceeds (status == "created") even when a warning is emitted
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_standpoint_author_id_")
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


def _make_observation(server, standpoint_author_id=None):
    """Helper: file a minimal observation with the given standpoint_author_id.

    Returns the parsed result dict. standpoint_author_id=None means the field
    is omitted entirely from the payload.
    """
    payload = {
        "url": "https://example.com/article",
        "title": "Example Article",
        "quoted_text": "X says Y",
        "interpretation": "X claims that Y is true",
        "claim": "Y is true according to X",
        "quote_type": "attributed_analysis",
    }
    if standpoint_author_id is not None:
        payload["standpoint_author_id"] = standpoint_author_id
    return json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))


# ── 1. Valid pn_* node ID ──────────────────────────────────────────────────


def test_pn_id_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="pn_0001")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings for pn_* ID: {r.get('warnings')}"


def test_pn_id_multi_digit_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="pn_1234")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings: {r.get('warnings')}"


# ── 2. Valid namespaced strings ────────────────────────────────────────────


def test_orcid_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="orcid:0000-0001-2345-6789")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings for orcid: {r.get('warnings')}"


def test_doi_author_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="doi-author:10.1000/xyz123")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings for doi-author: {r.get('warnings')}"


def test_domain_handle_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="domain:handle")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings for domain:handle: {r.get('warnings')}"


def test_generic_namespace_no_warning():
    server = fresh_server()
    # Any <prefix>:<value> where prefix is [a-z][a-z0-9-]* is valid
    r = _make_observation(server, standpoint_author_id="twitter:some-handle")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings: {r.get('warnings')}"


# ── 3. Raw display name with space → warning ───────────────────────────────


def test_display_name_with_space_emits_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="John Smith")
    assert r.get("status") == "created", f"Write should proceed: {r}"
    assert "warnings" in r, f"Expected warnings key for display name: {r}"
    assert len(r["warnings"]) >= 1
    warning_text = r["warnings"][0]
    assert "John Smith" in warning_text, warning_text
    assert "stable ID" in warning_text, warning_text
    assert "#1348" in warning_text, warning_text


# ── 4. Raw single-word name → warning ─────────────────────────────────────


def test_single_word_name_emits_warning():
    server = fresh_server()
    # "Lei" has no colon, doesn't start with pn_ — looks like a display name
    r = _make_observation(server, standpoint_author_id="Lei")
    assert r.get("status") == "created", f"Write should proceed: {r}"
    assert "warnings" in r, f"Expected warnings key for single-word name: {r}"
    assert len(r["warnings"]) >= 1
    warning_text = r["warnings"][0]
    assert "Lei" in warning_text, warning_text
    assert "stable ID" in warning_text, warning_text


# ── 5. Empty string → no warning ──────────────────────────────────────────


def test_empty_string_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings for empty string: {r.get('warnings')}"


# ── 6. None / omitted → no warning ────────────────────────────────────────


def test_omitted_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id=None)
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings when field omitted: {r.get('warnings')}"


# ── 7. Write always proceeds regardless of warning ─────────────────────────


def test_write_proceeds_even_with_warning():
    """The check is advisory; a bad standpoint_author_id must never block filing."""
    server = fresh_server()
    # Use a clearly bad value — it should warn but never error
    r = _make_observation(server, standpoint_author_id="Dr. Jane Doe, PhD")
    assert "error" not in r, f"Bad author_id must not block: {r}"
    assert r.get("status") == "created", f"Write must succeed: {r}"
    assert r.get("observation_id", "").startswith("ob_"), r
