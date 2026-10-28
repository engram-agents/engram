"""Tests for the standpoint_author_id stable-ID convention.

#1348 introduced a warn-not-reject advisory: a display-name-shaped
standpoint_author_id emitted a warning in the result under the "warnings"
key, but the write always proceeded. #1827 layer 1 measured that advisory
insufficient (n=4 seats, three independently-filed graphs with incoherent
key spaces despite the warning rendering) and upgraded it to a write-time
hard gate — REJECT by default, with a `standpoint.author_id_hard_gate`
config kill-switch (default true) that restores the pre-#1827 warn-only
behavior byte-for-byte when set false.

Coverage:
  1. Valid pn_* node ID (existing, current person node) → no warning, write
     proceeds
  2. Valid namespaced strings (orcid:…, doi-author:…, domain:handle) → no
     warning, write proceeds
  3. Raw display name (e.g. "John Smith", "Lei") → REJECT (hard gate on,
     the default), upgraded #1348 message + repair hint
  4. Dangling pn_* (well-formed shape, no matching current person node) →
     REJECT, #1827 message + repair hint
  5. Empty string / omitted → no warning, no gate (the field is optional)
  6. Kill-switch (standpoint.author_id_hard_gate = false) → legacy
     warn-not-reject (#1348) behavior, byte-compatible with pre-#1827: write
     always proceeds, dangling pn_* is not checked at all (that check is
     itself new in #1827 and does not exist in the legacy path)
  7. Repair hint: a bad string that fuzzy-matches an existing person's
     name/aliases surfaces that pn_ id in the reject message
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


def _set_hard_gate(server, enabled):
    """Flip standpoint.author_id_hard_gate in the test install's config.json."""
    config = json.loads(server.core.CONFIG_PATH.read_text())
    config["standpoint"] = {"author_id_hard_gate": enabled}
    server.core.CONFIG_PATH.write_text(json.dumps(config))


def _add_person(server, name, aliases=None):
    payload = {"name": name}
    if aliases is not None:
        payload["aliases"] = aliases
    p = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    return p.get("person_id") or p.get("node_id")


def _make_observation(server, standpoint_author_id=None):
    """Helper: file a minimal observation with the given standpoint_author_id.

    Returns the parsed result dict. standpoint_author_id=None means the field
    is omitted entirely from the payload.
    """
    payload = {
        "url": "https://example.com/article",
        "title": "Example Article",
        "quoted_text": "X says Y in the cited source",
        "interpretation": "X claims that Y is true",
        "claim": "Y is true according to X",
        "quote_type": "attributed_analysis",
    }
    if standpoint_author_id is not None:
        payload["standpoint_author_id"] = standpoint_author_id
    return json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))


# ── 1. Valid pn_* node ID (existing, current person) ───────────────────────


def test_existing_pn_id_no_warning():
    server = fresh_server()
    person_id = _add_person(server, "Test Author")
    r = _make_observation(server, standpoint_author_id=person_id)
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings for existing pn_* ID: {r.get('warnings')}"


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


# ── 3. Raw display name → REJECT (hard gate is the default) ────────────────


def test_display_name_with_space_rejected():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="John Smith")
    assert r.get("status") != "created", f"Write must be rejected: {r}"
    assert "error" in r, f"Expected error for display name: {r}"
    error_text = r["error"]
    assert "John Smith" in error_text, error_text
    assert "stable ID" in error_text, error_text
    assert "#1348" in error_text, error_text


def test_single_word_name_rejected():
    server = fresh_server()
    # "Lei" has no colon, doesn't start with pn_ — looks like a display name
    r = _make_observation(server, standpoint_author_id="Lei")
    assert r.get("status") != "created", f"Write must be rejected: {r}"
    assert "error" in r, f"Expected error for single-word name: {r}"
    error_text = r["error"]
    assert "Lei" in error_text, error_text
    assert "stable ID" in error_text, error_text


# ── 4. Dangling pn_* → REJECT ───────────────────────────────────────────────


def test_dangling_pn_id_rejected():
    server = fresh_server()
    # No person node with this ID exists in the fresh test DB.
    r = _make_observation(server, standpoint_author_id="pn_9999")
    assert r.get("status") != "created", f"Write must be rejected: {r}"
    assert "error" in r, f"Expected error for dangling pn_: {r}"
    error_text = r["error"]
    assert "pn_9999" in error_text, error_text
    assert "#1827" in error_text, error_text


def test_dangling_pn_id_multi_digit_rejected():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="pn_1234")
    assert r.get("status") != "created", f"Write must be rejected: {r}"
    assert "error" in r, r


# ── 5. Empty string / omitted → no warning, no gate ─────────────────────────


def test_empty_string_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings for empty string: {r.get('warnings')}"


def test_omitted_no_warning():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id=None)
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings when field omitted: {r.get('warnings')}"


# ── 6. Kill-switch off → legacy warn-not-reject, byte-compatible ───────────


def test_kill_switch_off_display_name_warns_not_rejects():
    """standpoint.author_id_hard_gate = false restores the pre-#1827 #1348
    warn-not-reject advisory byte-for-byte: the write always proceeds."""
    server = fresh_server()
    _set_hard_gate(server, False)
    r = _make_observation(server, standpoint_author_id="Dr. Jane Doe, PhD")
    assert "error" not in r, f"Bad author_id must not block with kill-switch off: {r}"
    assert r.get("status") == "created", f"Write must succeed: {r}"
    assert r.get("observation_id", "").startswith("ob_"), r
    assert "warnings" in r, f"Expected legacy warning: {r}"
    warning_text = r["warnings"][0]
    assert "Dr. Jane Doe, PhD" in warning_text, warning_text
    assert "stable ID" in warning_text, warning_text
    assert "#1348" in warning_text, warning_text
    # Byte-compatible with pre-#1827: no #1827 repair-hint text appended.
    assert "#1827" not in warning_text, warning_text
    assert "engram_add_person" not in warning_text, warning_text


def test_kill_switch_off_dangling_pn_not_checked():
    """With the hard gate off, the dangling-pn_ existence check (itself new
    in #1827) does not run at all — pn_* shape alone is enough to pass, same
    as pre-#1827 (no check, no warning)."""
    server = fresh_server()
    _set_hard_gate(server, False)
    r = _make_observation(server, standpoint_author_id="pn_9999")
    assert r.get("status") == "created", f"Write must succeed: {r}"
    assert "warnings" not in r, f"Unexpected warnings for pn_* shape: {r.get('warnings')}"


def test_kill_switch_off_write_proceeds_even_with_warning():
    """The legacy check is advisory; a bad standpoint_author_id must never
    block filing when the hard gate is off."""
    server = fresh_server()
    _set_hard_gate(server, False)
    r = _make_observation(server, standpoint_author_id="Dr. Jane Doe, PhD")
    assert "error" not in r, f"Bad author_id must not block: {r}"
    assert r.get("status") == "created", f"Write must succeed: {r}"
    assert r.get("observation_id", "").startswith("ob_"), r


def test_kill_switch_off_valid_pn_id_still_no_warning():
    server = fresh_server()
    _set_hard_gate(server, False)
    r = _make_observation(server, standpoint_author_id="pn_0001")
    assert r.get("status") == "created", r
    assert "warnings" not in r, f"Unexpected warnings: {r.get('warnings')}"


# ── 7. Repair hint: fuzzy match against an existing person's name/aliases ──


def test_repair_hint_surfaces_matching_person():
    server = fresh_server()
    person_id = _add_person(server, "Lei Shi", aliases=["Lei"])
    r = _make_observation(server, standpoint_author_id="Lei")
    assert r.get("status") != "created", f"Write must be rejected: {r}"
    error_text = r["error"]
    assert person_id in error_text, (
        f"Expected repair hint to surface {person_id} for a name/alias fuzzy "
        f"match: {error_text}"
    )


def test_repair_hint_absent_when_no_match():
    server = fresh_server()
    _add_person(server, "Completely Unrelated Name")
    r = _make_observation(server, standpoint_author_id="Zzzqx Nonexistent")
    assert r.get("status") != "created", f"Write must be rejected: {r}"
    error_text = r["error"]
    assert "engram_add_person" in error_text, error_text


# ── 8. Reject leaves ZERO rows (PR #1979 review blocker regression) ─────────


def _evidence_count(server):
    conn = server.core._get_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE type = 'evidence'"
        ).fetchone()[0]
    finally:
        conn.close()


def test_display_name_reject_creates_no_orphan_evidence():
    """The gate must fire BEFORE evidence auto-create: a rejected write with
    url+title (no evidence_id) must leave zero evidence rows. Pre-fix, the
    evidence node was committed on its own connection before the reject
    returned (empirically reproduced in the PR #1979 review)."""
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="John Smith")
    assert "error" in r, r
    assert _evidence_count(server) == 0, "reject persisted an orphan evidence node"


def test_dangling_pn_reject_creates_no_orphan_evidence():
    server = fresh_server()
    r = _make_observation(server, standpoint_author_id="pn_9999")
    assert "error" in r, r
    assert _evidence_count(server) == 0, "reject persisted an orphan evidence node"


# ── 9. Repair hint degrades (never crashes) on malformed person metadata ────


def test_repair_hint_survives_non_string_alias_metadata():
    """Legacy/hand-edited person metadata can carry non-string aliases; the
    repair-hint fuzzy match must skip them and still surface the real name,
    never crash the gate (PR #1979 review suggestion)."""
    server = fresh_server()
    person_id = _add_person(server, "RealName")
    conn = server.core._get_db()
    try:
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (person_id,)
        ).fetchone()
        meta = json.loads(row["metadata"] or "{}")
        meta["aliases"] = [123, None, {"x": 1}, "RealName"]
        conn.execute(
            "UPDATE nodes SET metadata = ? WHERE id = ?",
            (json.dumps(meta), person_id),
        )
        conn.commit()
    finally:
        conn.close()

    r = _make_observation(server, standpoint_author_id="RealNam")
    assert "error" in r, r
    assert person_id in r["error"], (
        f"expected repair hint naming {person_id} despite malformed aliases: {r['error']}"
    )
    assert _evidence_count(server) == 0
