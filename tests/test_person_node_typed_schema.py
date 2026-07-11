"""Tests for the person node typed schema (feat/person-node-typed-schema).

Coverage:
  1. engram_add_person with typed fields — no prose bin accepted
  2. Backward compat: role as deprecated alias for relation
  3. engram_update_person — field trace storage + cascade flag emission
  4. trust_tier update → elevated flag (review_required=True)
  5. Stage 1 auto-suggest: name-match fires suggestion, does NOT auto-wire about-edge
  6. Stage 2 auto-suggest: link_about response carries special_moment_suggestion
  7. special_moments cap (7) + rotation: oldest removed from list, about-edge preserved
  8. engram_add_special_moment MCP tool — payload validation + happy path +
     round-trip with engram_link_about's special_moment_suggestion (#1705)
"""

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_person_schema_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Provision a clean server instance backed by a temporary directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


# ---------------------------------------------------------------------------
# §1 — engram_add_person with typed fields
# ---------------------------------------------------------------------------

def test_add_person_typed_fields_accepted():
    """Full typed-field payload creates a person with correct metadata."""
    server = fresh_server()
    payload = {
        "name": "Mira",
        "relation": "daughter",
        "pronouns": "she/her",
        "trust_tier": "user_family",
        "aliases": ["Leo", "Leonie"],
        "location_contact": "123 Elm St",
        "birthday": "2010-03-15",
    }
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    pid = r["person_id"]
    assert pid.startswith("pn_"), r
    assert r["name"] == "Mira"
    assert r["relation"] == "daughter"

    # Verify metadata contains typed fields
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT claim, metadata, trust_tier FROM nodes WHERE id = ?", (pid,)
        ).fetchone()
        assert row is not None
        meta = json.loads(row["metadata"])
        assert meta["name"] == "Mira"
        assert meta["relation"] == "daughter"
        assert meta["pronouns"] == "she/her"
        assert meta["trust_tier"] == "user_family"
        assert meta["aliases"] == ["Leo", "Leonie"]
        assert meta["location_contact"] == "123 Elm St"
        assert meta["birthday"] == "2010-03-15"
        assert meta["special_moments"] == []        # §3: initialized empty
        assert meta["field_traces"] == {}           # §2: empty at creation
        # claim = "name — relation"
        assert row["claim"] == "Mira — daughter"
        # trust_tier column set
        assert row["trust_tier"] == "user_family"
    finally:
        conn.close()


def test_add_person_description_not_accepted():
    """description is not in the new schema — should be rejected as unknown field."""
    server = fresh_server()
    payload = {"name": "X", "description": "some free text"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "description" in r["error"]


def test_add_person_logical_chain_not_accepted():
    """logical_chain is not in the person schema — should be rejected."""
    server = fresh_server()
    payload = {"name": "X", "logical_chain": "some prose"}
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" in r and "Unknown fields" in r["error"], r


def test_add_person_name_only_is_valid():
    """name alone (no relation) is accepted — relation is optional."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({"name": "Anon"})))
    assert "error" not in r, r
    assert r["name"] == "Anon"
    assert r["relation"] == ""

    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT claim FROM nodes WHERE id = ?", (r["person_id"],)
        ).fetchone()
        # claim is just the name when no relation
        assert row["claim"] == "Anon"
    finally:
        conn.close()


def test_add_person_role_deprecated_alias():
    """role is accepted as deprecated alias for relation; flagged in response."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Bob",
        "role": "external evaluator",
    })))
    assert "error" not in r, r
    assert r["relation"] == "external evaluator"
    assert "deprecation_warning" in r
    assert "role" in r["deprecation_warning"]

    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (r["person_id"],)
        ).fetchone()
        meta = json.loads(row["metadata"])
        # Stored as 'relation', NOT as 'role'
        assert "relation" in meta
        assert meta["relation"] == "external evaluator"
        assert "role" not in meta
    finally:
        conn.close()


def test_add_person_agent_fields():
    """Agent-specific immutable fields (lineage, architecture, first_session_date)."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Borges",
        "lineage": "anthropic:claude-sonnet",
        "architecture": "transformer",
        "first_session_date": "2026-01-01",
    })))
    assert "error" not in r, r

    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (r["person_id"],)
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["lineage"] == "anthropic:claude-sonnet"
        assert meta["architecture"] == "transformer"
        assert meta["first_session_date"] == "2026-01-01"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §2 — engram_update_person: trace storage + cascade flags
# ---------------------------------------------------------------------------

def test_update_person_mutable_fields_traced():
    """Updating mutable fields stores prior values in field_traces."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Mira",
        "relation": "external",
        "trust_tier": "known_external",
    })))
    assert "error" not in r, r
    pid = r["person_id"]

    upd = json.loads(server.engram_update_person(payload_json=json.dumps({
        "node_id": pid,
        "updates": {"relation": "daughter"},
        "reason": "relationship clarified",
    })))
    assert "error" not in upd, upd
    assert upd["status"] == "updated"
    assert upd["updated_fields"]["relation"] == "daughter"
    assert "relation" in upd["field_traces_delta"]
    assert upd["field_traces_delta"]["relation"]["prior_value"] == "external"

    # Verify DB: field_traces is append-only, current value updated
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (pid,)
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["relation"] == "daughter"
        assert meta["field_traces"]["relation"] == [
            {"prior_value": "external", "changed_at": upd["field_traces_delta"]["relation"]["changed_at"]}
        ]
    finally:
        conn.close()


def test_update_person_trace_is_append_only():
    """Multiple updates append to field_traces — no mutation of existing entries."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "X",
        "relation": "v1",
    })))
    pid = r["person_id"]

    server.engram_update_person(payload_json=json.dumps({
        "node_id": pid,
        "updates": {"relation": "v2"},
    }))
    server.engram_update_person(payload_json=json.dumps({
        "node_id": pid,
        "updates": {"relation": "v3"},
    }))

    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (pid,)).fetchone()
        meta = json.loads(row["metadata"])
        traces = meta["field_traces"]["relation"]
        assert len(traces) == 2, f"Expected 2 trace entries, got {len(traces)}: {traces}"
        assert traces[0]["prior_value"] == "v1"
        assert traces[1]["prior_value"] == "v2"
        assert meta["relation"] == "v3"
    finally:
        conn.close()


def test_update_person_cascade_flag_emitted():
    """Nodes that cite or are about this person receive a field_changed_flags entry."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "CascadeTest",
        "relation": "colleague",
    })))
    pid = r["person_id"]

    # Create an observation that will cite the person via about edge
    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "CascadeTest is a reliable colleague",
        "quoted_text": "reliable colleague",
        "interpretation": "positive signal",
        "quote_type": "personal_communication",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    obs_id = obs.get("observation_id")
    assert obs_id, obs

    # Wire about edge
    server.engram_link_about(payload_json=json.dumps({
        "node_id": obs_id,
        "person_id": pid,
    }))

    # Update person
    upd = json.loads(server.engram_update_person(payload_json=json.dumps({
        "node_id": pid,
        "updates": {"relation": "trusted colleague"},
    })))
    assert "error" not in upd, upd
    assert obs_id in upd["cascade_flagged_nodes"]
    assert upd["cascade_flag_type"] == "field-value-changed"

    # Verify the observation's metadata contains the flag
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (obs_id,)).fetchone()
        meta = json.loads(row["metadata"] or "{}")
        flags = meta.get("field_changed_flags", [])
        assert len(flags) >= 1
        flag = flags[0]
        assert flag["person_id"] == pid
        assert flag["field"] == "relation"
        assert flag["prior_value"] == "colleague"
        assert flag["review_required"] is False
        assert flag["dismissible"] is True
    finally:
        conn.close()


def test_update_person_immutable_field_rejected():
    """Updating an immutable field returns a clear error."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Immutable",
        "birthday": "2000-01-01",
    })))
    pid = r["person_id"]

    upd = json.loads(server.engram_update_person(payload_json=json.dumps({
        "node_id": pid,
        "updates": {"name": "NewName"},
    })))
    assert "error" in upd, upd
    assert "immutable" in upd["error"].lower() or "Cannot update" in upd["error"]
    assert "name" in upd["error"]


def test_update_person_unknown_field_rejected():
    """Updating an unknown field is rejected."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({"name": "X"})))
    pid = r["person_id"]

    upd = json.loads(server.engram_update_person(payload_json=json.dumps({
        "node_id": pid,
        "updates": {"bogus_field": "value"},
    })))
    assert "error" in upd, upd
    assert "bogus_field" in upd["error"]


# ---------------------------------------------------------------------------
# §2 — trust_tier elevated flag
# ---------------------------------------------------------------------------

def test_update_trust_tier_elevated_flag():
    """trust_tier update produces trust_tier_elevated=True and review_required flag."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "TierTest",
        "trust_tier": "known_external",
    })))
    pid = r["person_id"]

    # Create and wire an observation so cascade fires
    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "TierTest updated trust",
        "quoted_text": "updated trust",
        "interpretation": "tier change",
        "quote_type": "personal_communication",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    obs_id = obs.get("observation_id")
    server.engram_link_about(payload_json=json.dumps({"node_id": obs_id, "person_id": pid}))

    upd = json.loads(server.engram_update_person(payload_json=json.dumps({
        "node_id": pid,
        "updates": {"trust_tier": "our_side"},
    })))
    assert "error" not in upd, upd
    assert upd["trust_tier_elevated"] is True

    # Check cascade flag has review_required=True
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (obs_id,)).fetchone()
        meta = json.loads(row["metadata"] or "{}")
        flags = meta.get("field_changed_flags", [])
        tier_flags = [f for f in flags if f["field"] == "trust_tier"]
        assert tier_flags, f"No trust_tier flag found; flags: {flags}"
        assert tier_flags[0]["review_required"] is True
        assert tier_flags[0]["dismissible"] is False
    finally:
        conn.close()

    # Verify trust_tier column also updated
    conn = core._get_db()
    try:
        row = conn.execute("SELECT trust_tier FROM nodes WHERE id = ?", (pid,)).fetchone()
        assert row["trust_tier"] == "our_side"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §4 Stage 1 — auto-suggest: name-match fires suggestion, no auto-wire
# ---------------------------------------------------------------------------

def test_stage1_person_mention_suggestion_fires():
    """Stage 1: when obs claim mentions a person's name, person_mentions is returned."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "Valentina",
        "relation": "colleague",
    })))
    assert "error" not in r, r

    # File an observation that mentions the person by name
    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "Valentina shared an insight about epistemic calibration",
        "quoted_text": "shared an insight",
        "interpretation": "a colleague contributed",
        "quote_type": "personal_communication",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    assert "error" not in obs, obs
    # suggestion is present
    assert "person_mentions" in obs, f"person_mentions missing from result: {obs}"
    pms = obs["person_mentions"]
    assert len(pms) >= 1
    match = next((p for p in pms if p["person_name"] == "Valentina"), None)
    assert match is not None, f"Valentina not in person_mentions: {pms}"
    assert "wire an about-edge?" in match["message"]

    # CRITICAL: no about-edge was auto-wired
    import engram_core as core
    conn = core._get_db()
    try:
        obs_id = obs["observation_id"]
        person_id = r["person_id"]
        about_edge = conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
            (obs_id, person_id),
        ).fetchone()
        assert about_edge is None, (
            "BUG: Stage 1 auto-WIRED an about-edge. mentions ≠ about — "
            "the suggestion must never auto-wire."
        )
    finally:
        conn.close()


def test_stage1_suggestion_fires_on_alias():
    """Stage 1 fires on alias match, not just primary name."""
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "LongFullName",
        "aliases": ["LFN", "shortname"],
    })))
    assert "error" not in r, r

    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "shortname pointed out a key design flaw",
        "quoted_text": "key design flaw",
        "interpretation": "someone flagged an issue",
        "quote_type": "personal_communication",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    assert "person_mentions" in obs, f"No person_mentions: {obs}"
    pms = obs["person_mentions"]
    match = next((p for p in pms if p["person_name"] == "LongFullName"), None)
    assert match is not None, f"LongFullName not found via alias in: {pms}"


def test_stage1_suggestion_fires_on_cjk_alias():
    """Stage 1 fires on a CJK alias, and on an ASCII alias glued to CJK text.

    \\b forms no boundary between adjacent CJK characters and a \\w-class
    term (Python's re treats CJK as \\w), so a pure word-boundary match
    silently misses bilingual mentions — exactly the case this feature is
    meant to serve (aliases like ["Wei", "王伟"]).
    """
    server = fresh_server()
    r = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "WangWei",
        "aliases": ["Wei", "王伟"],
    })))
    assert "error" not in r, r

    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "我和王伟讨论了这个设计",
        "quoted_text": "王伟讨论了这个设计",
        "interpretation": "discussed the design with Wang Wei",
        "quote_type": "personal_communication",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    assert "person_mentions" in obs, f"CJK alias not detected: {obs}"
    match = next((p for p in obs["person_mentions"] if p["person_name"] == "WangWei"), None)
    assert match is not None, f"WangWei not found via CJK alias in: {obs['person_mentions']}"

    obs2 = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "我见到了Wei昨天",
        "quoted_text": "见到了Wei昨天",
        "interpretation": "saw Wei yesterday",
        "quote_type": "personal_communication",
        "url": "https://example.com/test-evidence-2",
        "title": "test evidence 2",
    })))
    assert "person_mentions" in obs2, f"ASCII-alias-glued-to-CJK not detected: {obs2}"
    match2 = next((p for p in obs2["person_mentions"] if p["person_name"] == "WangWei"), None)
    assert match2 is not None, f"WangWei not found via ASCII-in-CJK alias in: {obs2['person_mentions']}"


def test_stage1_no_match_no_suggestion():
    """Stage 1: when claim doesn't mention any person, no person_mentions key."""
    server = fresh_server()
    server.engram_add_person(payload_json=json.dumps({
        "name": "UniqueXYZPersonName99",
        "relation": "ghost",
    }))

    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "The coffee machine broke down today",
        "quoted_text": "coffee machine broke",
        "interpretation": "equipment failure",
        "quote_type": "hard_data",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    assert "person_mentions" not in obs or obs["person_mentions"] == [], (
        f"Unexpected person_mentions: {obs.get('person_mentions')}"
    )


# ---------------------------------------------------------------------------
# §4 Stage 2 — link_about response carries special_moment_suggestion
# ---------------------------------------------------------------------------

def test_stage2_link_about_carries_suggestion():
    """Stage 2: engram_link_about response includes special_moment_suggestion."""
    server = fresh_server()
    rp = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "SuggestionTarget",
        "relation": "collaborator",
    })))
    pid = rp["person_id"]

    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "A notable thing happened",
        "quoted_text": "notable thing",
        "interpretation": "interesting event",
        "quote_type": "hard_data",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    obs_id = obs["observation_id"]

    link = json.loads(server.engram_link_about(payload_json=json.dumps({
        "node_id": obs_id,
        "person_id": pid,
    })))
    assert "error" not in link, link
    assert "special_moment_suggestion" in link, f"No special_moment_suggestion: {link}"
    sug = link["special_moment_suggestion"]
    assert sug["person_id"] == pid
    assert sug["source_node_id"] == obs_id
    assert "SuggestionTarget" in sug["person_name"]
    assert "add to" in sug["message"].lower()
    assert "how" in sug, f"No 'how' key (self-documenting nudge): {sug}"
    assert "engram_add_special_moment" in sug["how"]


# ---------------------------------------------------------------------------
# §3 — special_moments cap + rotation
# ---------------------------------------------------------------------------

def test_special_moments_cap_and_rotation():
    """At 7 special_moments, adding an 8th removes the oldest from the list
    but the about-edge from the oldest entry's node_id is preserved."""
    server = fresh_server()
    rp = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "CapTest",
        "relation": "family",
    })))
    pid = rp["person_id"]

    # Add 7 special moments directly via _add_special_moment_impl
    moment_node_ids = []
    for i in range(7):
        # Create a real observation to anchor each moment
        obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "claim": f"Moment {i+1} for CapTest",
            "quoted_text": f"moment {i+1}",
            "interpretation": "a special event",
            "quote_type": "hard_data",
            "url": "https://example.com/test-evidence",
            "title": "test evidence",
        })))
        obs_id = obs["observation_id"]
        moment_node_ids.append(obs_id)

        # Wire about-edge first (as the normal workflow would)
        server.engram_link_about(payload_json=json.dumps({
            "node_id": obs_id,
            "person_id": pid,
        }))

        # Add the special moment
        import engram_trust
        result = json.loads(engram_trust._add_special_moment_impl(
            person_id=pid,
            description=f"Description for moment {i+1}",
            node_id=obs_id,
        ))
        assert "error" not in result, result

    # Verify 7 moments in list
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (pid,)).fetchone()
        meta = json.loads(row["metadata"])
        assert len(meta["special_moments"]) == 7
        first_node_id = meta["special_moments"][0]["node_id"]
    finally:
        conn.close()

    # Add 8th moment — should rotate (oldest removed from list)
    obs8 = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "Moment 8 for CapTest",
        "quoted_text": "moment 8",
        "interpretation": "another special event",
        "quote_type": "hard_data",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    obs8_id = obs8["observation_id"]
    server.engram_link_about(payload_json=json.dumps({"node_id": obs8_id, "person_id": pid}))

    import engram_trust
    result8 = json.loads(engram_trust._add_special_moment_impl(
        person_id=pid,
        description="Description for moment 8",
        node_id=obs8_id,
    ))
    assert "error" not in result8, result8

    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (pid,)).fetchone()
        meta = json.loads(row["metadata"])
        moments = meta["special_moments"]
        # Still 7 slots (oldest rotated out)
        assert len(moments) == 7, f"Expected 7 slots, got {len(moments)}"
        # The oldest moment is no longer in the list
        remaining_ids = [m["node_id"] for m in moments]
        assert first_node_id not in remaining_ids, (
            f"Oldest moment {first_node_id} should have been rotated out"
        )
        # The newest moment is in the list
        assert obs8_id in remaining_ids

        # BUT the about-edge for the oldest moment is preserved
        about_edge = conn.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ? AND relation = 'about'",
            (first_node_id, pid),
        ).fetchone()
        assert about_edge is not None, (
            f"About-edge for rotated moment {first_node_id} should be preserved"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §8 — engram_add_special_moment MCP tool (#1705)
#
# The internal impl (_add_special_moment_impl) is already exercised directly
# above (§3, test_special_moments_cap_and_rotation) and in
# tests/test_person_node_typed_schema.py's own history — that internal-logic
# coverage is NOT duplicated here. These tests go through the MCP wrapper
# specifically: payload validation, delegate-through-correctly, and a
# lightweight spot-check that the wrapper doesn't break cap+rotation.
# ---------------------------------------------------------------------------

def _make_person_and_anchor(server, name="MomentTarget", claim="A notable thing happened"):
    """Helper: create a person node + a real, linked anchor observation.

    Returns (person_id, node_id) suitable for engram_add_special_moment.
    """
    rp = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": name,
        "relation": "colleague",
    })))
    assert "error" not in rp, rp
    pid = rp["person_id"]

    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": claim,
        "quoted_text": claim[:20],
        "interpretation": "interesting event",
        "quote_type": "hard_data",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    assert "error" not in obs, obs
    obs_id = obs["observation_id"]

    link = json.loads(server.engram_link_about(payload_json=json.dumps({
        "node_id": obs_id,
        "person_id": pid,
    })))
    assert "error" not in link, link

    return pid, obs_id, link


def test_add_special_moment_tool_happy_path():
    """MCP wrapper: happy path creates a special_moments entry."""
    server = fresh_server()
    pid, obs_id, _link = _make_person_and_anchor(server)

    r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": pid,
        "description": "First real conversation about the project's direction.",
        "node_id": obs_id,
    })))
    assert "error" not in r, r
    assert r["status"] == "added", r
    assert r["person_id"] == pid
    assert r["slot_count"] == 1
    descriptions = [m["description"] for m in r["special_moments"]]
    assert "First real conversation about the project's direction." in descriptions

    # Confirm it landed in the person node's metadata, not just the response.
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT metadata FROM nodes WHERE id = ?", (pid,)).fetchone()
        meta = json.loads(row["metadata"])
        assert len(meta["special_moments"]) == 1
        assert meta["special_moments"][0]["node_id"] == obs_id
    finally:
        conn.close()


def test_add_special_moment_tool_unknown_field_rejected():
    """Unknown top-level fields in payload_json are rejected before delegation."""
    server = fresh_server()
    pid, obs_id, _link = _make_person_and_anchor(server)

    r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": pid,
        "description": "x",
        "node_id": obs_id,
        "bogus": "reject",
    })))
    assert "error" in r, r
    assert "Unknown fields" in r["error"]
    assert "bogus" in r["error"]


def test_add_special_moment_tool_missing_required_fields():
    """Each of person_id/description/node_id blank is rejected by the impl,
    passed through unmangled by the MCP wrapper."""
    server = fresh_server()
    pid, obs_id, _link = _make_person_and_anchor(server)

    missing_person = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "description": "x",
        "node_id": obs_id,
    })))
    assert "error" in missing_person, missing_person
    assert "person_id" in missing_person["error"]

    missing_description = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": pid,
        "node_id": obs_id,
    })))
    assert "error" in missing_description, missing_description
    assert "description" in missing_description["error"]

    missing_node_id = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": pid,
        "description": "x",
    })))
    assert "error" in missing_node_id, missing_node_id
    assert "node_id" in missing_node_id["error"]


def test_add_special_moment_tool_person_not_found():
    """A nonexistent person_id is rejected — delegate-through-correctly, no
    re-derivation of internal validation logic."""
    server = fresh_server()
    _pid, obs_id, _link = _make_person_and_anchor(server)

    r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": "pn_9999999",
        "description": "x",
        "node_id": obs_id,
    })))
    assert "error" in r, r
    assert "not found" in r["error"].lower()


def test_add_special_moment_tool_person_not_type_person():
    """A node_id that resolves but isn't type='person' is rejected."""
    server = fresh_server()
    _pid, obs_id, _link = _make_person_and_anchor(server)

    r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": obs_id,  # an observation node, not a person node
        "description": "x",
        "node_id": obs_id,
    })))
    assert "error" in r, r
    assert "not 'person'" in r["error"] or "not a" in r["error"].lower()


def test_add_special_moment_tool_person_not_current():
    """A retracted person node is rejected."""
    server = fresh_server()
    pid, obs_id, _link = _make_person_and_anchor(server)

    retract = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": pid,
        "error_type": "hallucinated_claim",
        "reason": "Test retraction — MCP wrapper is_current guard test.",
    })))
    assert "error" not in retract, retract

    r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": pid,
        "description": "x",
        "node_id": obs_id,
    })))
    assert "error" in r, r
    assert "not current" in r["error"].lower()


def test_add_special_moment_tool_anchor_not_found_or_not_current():
    """The anchor node_id must reference a real, current node."""
    server = fresh_server()
    pid, obs_id, _link = _make_person_and_anchor(server)

    not_found = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": pid,
        "description": "x",
        "node_id": "ob_9999999",
    })))
    assert "error" in not_found, not_found
    assert "not found" in not_found["error"].lower()

    retract = json.loads(server.engram_retract(payload_json=json.dumps({
        "node_id": obs_id,
        "error_type": "hallucinated_claim",
        "reason": "Test retraction — anchor not-current guard test.",
    })))
    assert "error" not in retract, retract

    not_current = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": pid,
        "description": "x",
        "node_id": obs_id,
    })))
    assert "error" in not_current, not_current
    assert "not current" in not_current["error"].lower()


def test_add_special_moment_tool_cap_and_rotation_spot_check():
    """Lighter spot-check (3 calls) that the MCP wrapper doesn't break the
    cap+rotation behavior already exhaustively tested in §3 for the
    internal impl. Uses a cap smaller than 7 isn't available (cap is a
    module constant), so this only confirms accumulation works through the
    wrapper — full 7-then-8 rotation coverage stays in test_special_moments_
    cap_and_rotation above."""
    server = fresh_server()
    pid, _obs_id, _link = _make_person_and_anchor(server)

    node_ids = []
    for i in range(3):
        obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "claim": f"Moment {i+1}",
            "quoted_text": f"moment {i+1}",
            "interpretation": "a special event",
            "quote_type": "hard_data",
            "url": "https://example.com/test-evidence",
            "title": "test evidence",
        })))
        obs_id = obs["observation_id"]
        server.engram_link_about(payload_json=json.dumps({
            "node_id": obs_id, "person_id": pid,
        }))
        node_ids.append(obs_id)

        r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
            "person_id": pid,
            "description": f"Description for moment {i+1}",
            "node_id": obs_id,
        })))
        assert "error" not in r, r
        assert r["slot_count"] == i + 1, r

    assert len(r["special_moments"]) == 3
    assert [m["node_id"] for m in r["special_moments"]] == node_ids


def test_add_special_moment_tool_round_trip_with_link_about_suggestion():
    """The person_id/node_id from a real engram_link_about suggestion can be
    fed directly into engram_add_special_moment and succeeds."""
    server = fresh_server()
    rp = json.loads(server.engram_add_person(payload_json=json.dumps({
        "name": "RoundTripTarget",
        "relation": "collaborator",
    })))
    pid = rp["person_id"]

    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": "A genuinely notable exchange happened",
        "quoted_text": "notable exchange",
        "interpretation": "worth remembering",
        "quote_type": "hard_data",
        "url": "https://example.com/test-evidence",
        "title": "test evidence",
    })))
    obs_id = obs["observation_id"]

    link = json.loads(server.engram_link_about(payload_json=json.dumps({
        "node_id": obs_id,
        "person_id": pid,
    })))
    assert "error" not in link, link
    assert "special_moment_suggestion" in link, link
    sug = link["special_moment_suggestion"]
    assert sug["person_id"] == pid
    assert sug["source_node_id"] == obs_id

    # Feed the suggestion's own fields straight into the confirm tool.
    r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
        "person_id": sug["person_id"],
        "description": "Confirmed via the special_moment_suggestion round-trip.",
        "node_id": sug["source_node_id"],
    })))
    assert "error" not in r, r
    assert r["status"] == "added", r
