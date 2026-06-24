"""Tests for engram_set_person_lineage (issue #820).

Coverage:
  1. Happy path — updates self-anchor with a Claude model ID.
  2. Explicit target_pn — updates a specific non-self person node.
  3. target_pn defaults to self-anchor when omitted.
  4. Idempotent — calling twice overwrites to latest value.
  5. model_id parsing — sonnet, opus, haiku, and date-suffix variants.
  6. Error: missing model_id.
  7. Error: missing self-anchor when target_pn omitted.
  8. Error: non-existent target_pn.
  9. Error: unknown payload fields rejected.
  10. _parse_model_id unit tests — all format variants.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_person_lineage_")
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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_self_anchor(srv):
    r = json.loads(srv.engram_add_person(json.dumps({
        "name": "Sol",
        "role": "agent self-anchor",
        "is_self": True,
    })))
    assert "error" not in r, r
    return r["person_id"]


def _make_person(srv, name="Testperson"):
    r = json.loads(srv.engram_add_person(json.dumps({
        "name": name,
        "role": "test collaborator",
    })))
    assert "error" not in r, r
    return r["person_id"]


def _get_metadata(srv, pn_id):
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id=?", (pn_id,)
        ).fetchone()
        return json.loads(row["metadata"] or "{}")
    finally:
        conn.close()


# ── Happy path ────────────────────────────────────────────────────────────────

def test_happy_path_self_anchor():
    srv = fresh_server()
    pn = _make_self_anchor(srv)
    r = json.loads(srv.engram_set_person_lineage(json.dumps({"model_id": "claude-sonnet-4-6"})))
    assert "error" not in r, r
    assert r["status"] == "ok"
    assert r["person_id"] == pn
    assert r["model_provider"] == "anthropic"
    assert r["model_family"] == "claude-sonnet"
    assert r["model_version"] == "4.6"


def test_metadata_persisted_to_db():
    srv = fresh_server()
    pn = _make_self_anchor(srv)
    srv.engram_set_person_lineage(json.dumps({"model_id": "claude-opus-4-8"}))
    meta = _get_metadata(srv, pn)
    assert meta.get("model_provider") == "anthropic"
    assert meta.get("model_family") == "claude-opus"
    assert meta.get("model_version") == "4.8"


def test_explicit_target_pn():
    srv = fresh_server()
    _make_self_anchor(srv)
    pn2 = _make_person(srv, "Colleague")
    r = json.loads(srv.engram_set_person_lineage(json.dumps({
        "target_pn": pn2,
        "model_id": "claude-haiku-4-5",
    })))
    assert "error" not in r, r
    assert r["person_id"] == pn2
    assert r["model_family"] == "claude-haiku"


def test_default_to_self_anchor():
    srv = fresh_server()
    pn = _make_self_anchor(srv)
    # No target_pn — should resolve to self-anchor
    r = json.loads(srv.engram_set_person_lineage(json.dumps({"model_id": "claude-sonnet-4-6"})))
    assert "error" not in r, r
    assert r["person_id"] == pn


def test_idempotent_overwrites_to_latest():
    srv = fresh_server()
    _make_self_anchor(srv)
    srv.engram_set_person_lineage(json.dumps({"model_id": "claude-opus-4-8"}))
    r = json.loads(srv.engram_set_person_lineage(json.dumps({"model_id": "claude-sonnet-4-6"})))
    assert r["model_family"] == "claude-sonnet"
    # Second call overwrote first
    pn = r["person_id"]
    meta = _get_metadata(srv, pn)
    assert meta["model_family"] == "claude-sonnet"
    assert meta["model_version"] == "4.6"


def test_existing_metadata_preserved():
    srv = fresh_server()
    pn = _make_self_anchor(srv)
    # Verify existing metadata fields are not wiped
    meta_before = _get_metadata(srv, pn)
    assert "name" in meta_before
    assert "is_self" in meta_before
    srv.engram_set_person_lineage(json.dumps({"model_id": "claude-sonnet-4-6"}))
    meta_after = _get_metadata(srv, pn)
    assert meta_after["name"] == meta_before["name"]
    assert meta_after["is_self"] == meta_before["is_self"]
    assert meta_after["model_family"] == "claude-sonnet"


# ── model_id parsing variants ─────────────────────────────────────────────────

def test_parse_haiku_with_date_suffix():
    """Date-style suffix (8 digits) in model ID is ignored in version."""
    srv = fresh_server()
    _make_self_anchor(srv)
    r = json.loads(srv.engram_set_person_lineage(json.dumps({
        "model_id": "claude-haiku-4-5-20251001"
    })))
    assert r["model_family"] == "claude-haiku"
    assert r["model_version"] == "4.5"


def test_parse_opus():
    srv = fresh_server()
    _make_self_anchor(srv)
    r = json.loads(srv.engram_set_person_lineage(json.dumps({"model_id": "claude-opus-4-8"})))
    assert r["model_provider"] == "anthropic"
    assert r["model_family"] == "claude-opus"
    assert r["model_version"] == "4.8"


# ── _parse_model_id unit tests ────────────────────────────────────────────────

def test_parse_model_id_sonnet():
    import engram_trust
    p = engram_trust._parse_model_id("claude-sonnet-4-6")
    assert p == {"provider": "anthropic", "family": "claude-sonnet", "version": "4.6"}


def test_parse_model_id_opus():
    import engram_trust
    p = engram_trust._parse_model_id("claude-opus-4-8")
    assert p == {"provider": "anthropic", "family": "claude-opus", "version": "4.8"}


def test_parse_model_id_haiku_no_date():
    import engram_trust
    p = engram_trust._parse_model_id("claude-haiku-4-5")
    assert p == {"provider": "anthropic", "family": "claude-haiku", "version": "4.5"}


def test_parse_model_id_haiku_with_date():
    import engram_trust
    p = engram_trust._parse_model_id("claude-haiku-4-5-20251001")
    assert p == {"provider": "anthropic", "family": "claude-haiku", "version": "4.5"}


def test_parse_model_id_unrecognised():
    import engram_trust
    p = engram_trust._parse_model_id("gpt-4o-mini")
    assert p["provider"] == ""
    assert p["family"] == "gpt-4o-mini"
    assert p["version"] == ""


# ── Error cases ───────────────────────────────────────────────────────────────

def test_error_missing_model_id():
    srv = fresh_server()
    _make_self_anchor(srv)
    r = json.loads(srv.engram_set_person_lineage(json.dumps({})))
    assert "error" in r
    assert "model_id" in r["error"].lower()


def test_error_empty_model_id():
    srv = fresh_server()
    _make_self_anchor(srv)
    r = json.loads(srv.engram_set_person_lineage(json.dumps({"model_id": ""})))
    assert "error" in r


def test_error_no_self_anchor():
    """Omitted target_pn with no self-anchor → clear error."""
    srv = fresh_server()
    r = json.loads(srv.engram_set_person_lineage(json.dumps({"model_id": "claude-sonnet-4-6"})))
    assert "error" in r
    assert "self-anchor" in r["error"].lower()


def test_error_nonexistent_target_pn():
    srv = fresh_server()
    _make_self_anchor(srv)
    r = json.loads(srv.engram_set_person_lineage(json.dumps({
        "target_pn": "pn_9999",
        "model_id": "claude-sonnet-4-6",
    })))
    assert "error" in r
    assert "not found" in r["error"].lower()


def test_error_unknown_fields():
    srv = fresh_server()
    _make_self_anchor(srv)
    r = json.loads(srv.engram_set_person_lineage(json.dumps({
        "model_id": "claude-sonnet-4-6",
        "unexpected_field": "boom",
    })))
    assert "error" in r
    assert "Unknown fields" in r["error"]


def test_error_invalid_json():
    srv = fresh_server()
    r = json.loads(srv.engram_set_person_lineage("not-json"))
    assert "error" in r


def test_error_non_object_payload():
    srv = fresh_server()
    r = json.loads(srv.engram_set_person_lineage(json.dumps(["model_id", "claude-sonnet-4-6"])))
    assert "error" in r
