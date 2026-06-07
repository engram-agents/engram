"""Tests for the recall_summary + recall_keywords substrate (PR A).

Covers:
  1. Schema migration is idempotent — running _ensure_schema twice doesn't
     error, both columns exist.
  2. _search_nodes SELECT returns recall_summary and recall_keywords for nodes
     (both NULL on fresh nodes; both populated after manual UPDATE).
  3. engram_surface top_claims and special_nodes entries include
     recall_summary and recall_keywords fields.
  4. recall_keywords returned as Python list (not JSON string) — verify type.
  5. recall_keywords is None when DB value is NULL (not [None] or []).

Hook rendering (format_nudge end-to-end with synthetic result dicts):
  6. Both fields present → renders with keyword prefix + summary.
  7. Summary only → no keyword prefix, summary in line.
  8. Neither → falls back to claim[:117]+"…".
  9. Specials section appears before top_claims in output.
  10. Types: line is no longer rendered (dropped per 2026-05-19 redesign).
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Project-root on path
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Server sandbox helpers (same pattern as test_special_type_bypass.py)
# ---------------------------------------------------------------------------

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_recall_summary_substrate_")
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


def fresh_conn(srv):
    return srv._get_db()


def _add_obs(srv, claim: str, quoted_text: str = "test quote") -> str:
    payload = {
        "quoted_text": quoted_text,
        "interpretation": "test interpretation",
        "claim": claim,
        "quote_type": "personal_communication",
        "url": "https://example.com/test",
        "title": "Test",
    }
    r = json.loads(srv.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["observation_id"]


def _add_axiom(srv, claim: str) -> str:
    payload = {
        "claim": claim,
        "basis": "test basis for recall summary substrate test",
    }
    r = json.loads(srv.engram_add_axiom(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["axiom_id"]


# ---------------------------------------------------------------------------
# Hook module loader (same pattern as test_engram_surface_hook_idf_gate.py)
# ---------------------------------------------------------------------------


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename."""
    hook_path = os.path.join(PROJECT_ROOT, "hooks", "claude", "engram-surface-hook.py")
    spec = importlib.util.spec_from_file_location("engram_surface_hook", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Test 1: Schema migration is idempotent
# ---------------------------------------------------------------------------


def test_schema_migration_idempotent():
    """Running _get_db twice does not raise and both columns exist.

    The migration code runs inline inside _get_db. Idempotency is guaranteed
    because the migration checks PRAGMA table_info before each ALTER TABLE.
    This test exercises that path by calling _get_db a second time after the
    initial schema-init done by fresh_server.
    """
    srv = fresh_server()

    # First call happened inside fresh_server → verify columns exist.
    conn1 = fresh_conn(srv)
    try:
        cols1 = {r[1] for r in conn1.execute("PRAGMA table_info(nodes)").fetchall()}
        assert "recall_summary" in cols1, (
            f"recall_summary column missing after first schema init. cols={cols1}"
        )
        assert "recall_keywords" in cols1, (
            f"recall_keywords column missing after first schema init. cols={cols1}"
        )
    finally:
        conn1.close()

    # Second call — must not raise despite columns already existing.
    conn2 = fresh_conn(srv)
    try:
        cols2 = {r[1] for r in conn2.execute("PRAGMA table_info(nodes)").fetchall()}
        assert "recall_summary" in cols2, (
            f"recall_summary column missing after second _get_db call. cols={cols2}"
        )
        assert "recall_keywords" in cols2, (
            f"recall_keywords column missing after second _get_db call. cols={cols2}"
        )
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Test 2: _search_nodes returns recall_summary and recall_keywords
# ---------------------------------------------------------------------------


def test_search_nodes_returns_recall_fields_null_on_fresh_node():
    """Fresh nodes have NULL recall_summary and recall_keywords; _search_nodes
    does not include absent keys (None values are stripped by _node_to_dict)."""
    srv = fresh_server()
    keyword = "quasar"
    _add_obs(srv, f"{keyword} active galactic nucleus recall substrate test")

    conn = fresh_conn(srv)
    try:
        result = srv._search_nodes(conn, keyword, use_semantic=False, top_k=5)
    finally:
        conn.close()

    assert result["results"], "Expected at least one result"
    node = result["results"][0]
    # NULL columns are stripped by _node_to_dict → key absent → .get() returns None
    assert node.get("recall_summary") is None, (
        f"Expected recall_summary=None on fresh node, got {node.get('recall_summary')!r}"
    )
    assert node.get("recall_keywords") is None, (
        f"Expected recall_keywords=None on fresh node, got {node.get('recall_keywords')!r}"
    )


def test_search_nodes_returns_recall_fields_populated_after_update():
    """After manual UPDATE, _search_nodes returns recall_summary str and
    recall_keywords as the raw JSON string (parsing to list is engram_surface's job)."""
    srv = fresh_server()
    keyword = "magnetar"
    obs_id = _add_obs(srv, f"{keyword} neutron star high magnetic field substrate test")

    # Manually set the recall fields directly in DB (backfill PR B pattern).
    conn = fresh_conn(srv)
    try:
        keywords_json = json.dumps(["magnetar", "neutron-star", "magnetic"])
        summary = "A magnetar is a neutron star with an extremely strong magnetic field."
        conn.execute(
            "UPDATE nodes SET recall_summary = ?, recall_keywords = ? WHERE id = ?",
            (summary, keywords_json, obs_id),
        )
        conn.commit()

        result = srv._search_nodes(conn, keyword, use_semantic=False, top_k=5)
    finally:
        conn.close()

    match = next((n for n in result["results"] if n["id"] == obs_id), None)
    assert match is not None, f"Node {obs_id} not found in results"
    assert match.get("recall_summary") == summary, (
        f"recall_summary mismatch: {match.get('recall_summary')!r} vs {summary!r}"
    )
    # _node_to_dict returns the raw string; engram_surface does the json.loads
    assert match.get("recall_keywords") == keywords_json, (
        f"recall_keywords should be raw JSON string from _node_to_dict: "
        f"{match.get('recall_keywords')!r} vs {keywords_json!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: engram_surface top_claims and special_nodes include recall fields
# ---------------------------------------------------------------------------


def test_engram_surface_top_claims_include_recall_fields():
    """engram_surface top_claims entries contain recall_summary and recall_keywords keys."""
    srv = fresh_server()
    keyword = "pulsar"
    _add_obs(srv, f"{keyword} rotating neutron star radio emission recall surface test")

    result = json.loads(srv.engram_surface(payload_json=json.dumps({"query": keyword, "top_k": 10, "semantic": False})))
    top_claims = result.get("top_claims", [])
    assert top_claims, "Expected at least one top_claim"

    for entry in top_claims:
        assert "recall_summary" in entry, (
            f"recall_summary key missing from top_claim entry {entry.get('id')}"
        )
        assert "recall_keywords" in entry, (
            f"recall_keywords key missing from top_claim entry {entry.get('id')}"
        )


def test_engram_surface_special_nodes_include_recall_fields():
    """engram_surface special_nodes entries contain recall_summary and recall_keywords keys."""
    srv = fresh_server()
    keyword = "blazar"
    _add_axiom(srv, f"{keyword} jet aligned relativistic emission recall surface test")

    result = json.loads(srv.engram_surface(payload_json=json.dumps({"query": keyword, "top_k": 10, "semantic": False})))
    special_nodes = result.get("special_nodes", [])
    assert special_nodes, "Expected at least one special_node (axiom)"

    for entry in special_nodes:
        assert "recall_summary" in entry, (
            f"recall_summary key missing from special_node entry {entry.get('id')}"
        )
        assert "recall_keywords" in entry, (
            f"recall_keywords key missing from special_node entry {entry.get('id')}"
        )


# ---------------------------------------------------------------------------
# Test 4: recall_keywords returned as Python list (not JSON string)
# ---------------------------------------------------------------------------


def test_engram_surface_recall_keywords_is_list_when_populated():
    """engram_surface top_claims entry with populated recall_keywords returns a
    Python list, not a raw JSON string."""
    srv = fresh_server()
    keyword = "graviton"
    obs_id = _add_obs(srv, f"{keyword} hypothetical quantum of gravity recall type test")

    # Populate via direct DB update.
    conn = fresh_conn(srv)
    try:
        keywords_json = json.dumps(["graviton", "quantum", "gravity"])
        summary = "Hypothetical force-carrier particle for gravity in quantum field theory."
        conn.execute(
            "UPDATE nodes SET recall_summary = ?, recall_keywords = ? WHERE id = ?",
            (summary, keywords_json, obs_id),
        )
        conn.commit()
    finally:
        conn.close()

    result = json.loads(srv.engram_surface(payload_json=json.dumps({"query": keyword, "top_k": 10, "semantic": False})))
    top_claims = result.get("top_claims", [])
    match = next((c for c in top_claims if c["id"] == obs_id), None)
    assert match is not None, f"Node {obs_id} not found in top_claims"

    kw = match.get("recall_keywords")
    assert isinstance(kw, list), (
        f"recall_keywords should be a Python list after json.loads, got {type(kw)}: {kw!r}"
    )
    assert kw == ["graviton", "quantum", "gravity"], (
        f"recall_keywords list content mismatch: {kw!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: recall_keywords is None when DB value is NULL
# ---------------------------------------------------------------------------


def test_engram_surface_recall_keywords_none_when_null():
    """engram_surface entries have recall_keywords=None (not [] or [None]) for
    nodes where the DB value is NULL."""
    srv = fresh_server()
    keyword = "tachyon"
    _add_obs(srv, f"{keyword} hypothetical faster than light particle recall null test")

    result = json.loads(srv.engram_surface(payload_json=json.dumps({"query": keyword, "top_k": 10, "semantic": False})))
    top_claims = result.get("top_claims", [])
    assert top_claims, "Expected at least one top_claim"

    for entry in top_claims:
        kw = entry.get("recall_keywords")
        assert kw is None, (
            f"Expected recall_keywords=None for fresh node (NULL in DB), "
            f"got {kw!r} (type={type(kw)})"
        )


# ---------------------------------------------------------------------------
# Tests 6–10: Hook format_nudge rendering
# ---------------------------------------------------------------------------


def _make_result(
    *,
    special_nodes=None,
    top_claims=None,
    match_count=None,
    matched_ids=None,
    matched_meta=None,
) -> dict:
    """Build a synthetic engram_surface result dict for format_nudge testing."""
    sn = special_nodes or []
    tc = top_claims or []
    mc = match_count if match_count is not None else len(sn) + len(tc)
    mids = matched_ids or [n["id"] for n in sn + tc]
    # Default matched_meta echoes matched_ids with no keywords (legacy shape).
    # Tests that exercise the Others section can pass matched_meta explicitly.
    mm = matched_meta if matched_meta is not None else [
        {"id": mid, "type": "observation_factual", "recall_keywords": None}
        for mid in mids
    ]
    return {
        "match_count": mc,
        "special_nodes": sn,
        "top_claims": tc,
        "type_counts": {},
        "age": {},
        "issues": {},
        "matched_ids": mids,
        "matched_meta": mm,
    }


def _claim_entry(
    *,
    node_id: str,
    claim: str,
    confidence: float = 0.85,
    recall_summary=None,
    recall_keywords=None,
    ntype: str = "observation_factual",
) -> dict:
    return {
        "id": node_id,
        "type": ntype,
        "claim": claim,
        "confidence": confidence,
        "recall_summary": recall_summary,
        "recall_keywords": recall_keywords,
        "created_ago": "1 hour ago",
    }


def _special_entry(
    *,
    node_id: str,
    claim: str,
    ntype: str = "axiom",
    confidence=None,
    recall_summary=None,
    recall_keywords=None,
) -> dict:
    entry = {
        "id": node_id,
        "type": ntype,
        "claim": claim,
        "recall_summary": recall_summary,
        "recall_keywords": recall_keywords,
        "created_ago": "2 days ago",
    }
    if confidence is not None:
        entry["confidence"] = confidence
    return entry


# ---- Test 6: Both fields present → keyword prefix + summary ----


def test_format_nudge_both_fields_renders_kw_prefix_and_summary():
    """When both recall_summary and recall_keywords are set, the line shows
    `kw1` · `kw2` · `kw3` — <summary>."""
    hook = _load_hook_module()

    claim_entry = _claim_entry(
        node_id="ob_1954",
        claim="A very long claim text that would normally be truncated by the fallback path because it exceeds 120 characters in total length here",
        confidence=0.95,
        recall_summary="Post-PR#209 composite = 0.7×relevance + 0.3×utility_score; FTS_SIM_FLOOR=0.30 drops off-topic hits.",
        recall_keywords=["composite", "FTS-floor", "utility"],
    )
    result = _make_result(top_claims=[claim_entry])
    output = hook.format_nudge(result)

    # Keyword prefix present with backticks and middots
    assert "`composite`" in output, f"Expected backtick-wrapped keyword in output:\n{output}"
    assert " · " in output, f"Expected middot separator in output:\n{output}"
    assert " — " in output, f"Expected em-dash separator in output:\n{output}"
    # Summary used (not truncated claim)
    assert "FTS_SIM_FLOOR=0.30" in output, f"Expected summary text in output:\n{output}"
    # Fallback truncation text NOT used
    assert "…" not in output or "FTS_SIM_FLOOR" in output, (
        f"Unexpected ellipsis without summary in output:\n{output}"
    )


# ---- Test 7: Summary only → no keyword prefix, summary in line ----


def test_format_nudge_summary_only_no_kw_prefix():
    """When only recall_summary is set (no keywords), no keyword prefix is
    rendered but the summary is still used."""
    hook = _load_hook_module()

    claim_entry = _claim_entry(
        node_id="ob_1955",
        claim="A very long claim text that would be truncated without the summary fallback here it is",
        confidence=0.90,
        recall_summary="Post-PR#209 composite formula with FTS floor.",
        recall_keywords=None,
    )
    result = _make_result(top_claims=[claim_entry])
    output = hook.format_nudge(result)

    # Summary used
    assert "Post-PR#209 composite formula with FTS floor." in output, (
        f"Expected summary in output:\n{output}"
    )
    # No keyword prefix (no backticks before middot pattern)
    assert " · " not in output, f"Unexpected middot (keyword prefix) in output:\n{output}"


def test_format_nudge_keywords_two_elements_renders_kw_prefix():
    """Contract: keyword prefix renders for any non-empty recall_keywords list
    (render_one_node_line path uses >=1 per PR #268, mirroring PR #235's fix
    at the Others-section path).
    A 2-element list renders `kw1` · `kw2` — <summary> on the output line.
    """
    hook = _load_hook_module()

    claim_entry = _claim_entry(
        node_id="ob_wrong_kw_len",
        claim="A short claim that wouldn't truncate.",
        confidence=0.70,
        recall_summary="Short summary for the fallback path.",
        recall_keywords=["only", "two"],  # 2 elements — now renders with >=1 guard
    )
    result = _make_result(top_claims=[claim_entry])
    output = hook.format_nudge(result)

    # Keyword prefix renders: `only` · `two` —
    assert "`only`" in output, (
        f"Expected backtick-quoted first keyword in output:\n{output}"
    )
    assert " · " in output, (
        f"Expected middot separator in 2-element keyword prefix:\n{output}"
    )
    # Summary still used as body after keyword prefix
    assert "Short summary for the fallback path." in output, (
        f"Expected summary in output:\n{output}"
    )


# ---- Test 8: Neither field → fallback to claim[:117]+"…" ----


def test_format_nudge_neither_field_falls_back_to_claim_truncation():
    """When both recall_summary and recall_keywords are None, the formatter
    falls back to claim[:117]+"…" for long claims."""
    hook = _load_hook_module()

    long_claim = "PR review batch outcome (2026-05-06, 12 parallel general-purpose review subagents on engram-alpha PRs #20-#34). Verdi style"
    # Confirm the claim exceeds 120 chars so truncation fires
    assert len(long_claim) > 120, "Pre-condition: claim must exceed 120 chars"

    claim_entry = _claim_entry(
        node_id="ob_1531",
        claim=long_claim,
        confidence=0.85,
        recall_summary=None,
        recall_keywords=None,
    )
    result = _make_result(top_claims=[claim_entry])
    output = hook.format_nudge(result)

    expected_truncated = long_claim[:117] + "…"
    assert expected_truncated in output, (
        f"Expected truncated claim in output.\n"
        f"Expected: {expected_truncated!r}\n"
        f"Output:\n{output}"
    )


# ---- Test 9: Specials section appears before top_claims in output ----


def test_format_nudge_specials_appear_before_top_claims():
    """The Specials section must appear before the Top claims section."""
    hook = _load_hook_module()

    special = _special_entry(
        node_id="ax_0001",
        claim="Honesty is structural, not ethical.",
        ntype="axiom",
        confidence=0.99,
    )
    claim = _claim_entry(
        node_id="ob_0042",
        claim="Some observation about a topic.",
        confidence=0.80,
    )
    result = _make_result(special_nodes=[special], top_claims=[claim])
    output = hook.format_nudge(result)

    specials_pos = output.find("Specials:")
    top_claims_pos = output.find("Top claims:")
    assert specials_pos != -1, f"'Specials:' not found in output:\n{output}"
    assert top_claims_pos != -1, f"'Top claims:' not found in output:\n{output}"
    assert specials_pos < top_claims_pos, (
        f"Specials section (pos={specials_pos}) must come before Top claims "
        f"(pos={top_claims_pos}):\n{output}"
    )


# ---- Test 10: Types: line is no longer rendered ----


def test_format_nudge_types_line_not_rendered():
    """The 'Types: ...' line must not appear in format_nudge output."""
    hook = _load_hook_module()

    special = _special_entry(
        node_id="ls_0010",
        claim="Practice produces knowledge.",
        ntype="lesson",
    )
    claim = _claim_entry(
        node_id="ob_0099",
        claim="Calibration improves recall quality.",
        confidence=0.75,
    )
    result = _make_result(
        special_nodes=[special],
        top_claims=[claim],
        # Populate type_counts to confirm it is NOT rendered
        match_count=2,
    )
    result["type_counts"] = {"lesson": 1, "observation_factual": 1}

    output = hook.format_nudge(result)
    assert "Types:" not in output, (
        f"'Types:' line must not appear in output (dropped per 2026-05-19 redesign):\n{output}"
    )


# ---- Test 11: recall_keywords rendering under >=1 contract (PR #268) ----


def test_format_nudge_recall_keywords_empty_no_kw_prefix():
    """Empty recall_keywords list → no keyword prefix (>=1 guard, PR #268).
    Summary still renders as the body."""
    hook = _load_hook_module()

    claim_entry = _claim_entry(
        node_id="ob_test",
        claim="A claim long enough that the fallback would normally truncate it past the 120 character soft cap so we can see what renders here",
        confidence=0.80,
        recall_summary="A curated summary that should still appear since it is not None.",
        recall_keywords=[],
    )
    result = _make_result(top_claims=[claim_entry])
    output = hook.format_nudge(result)
    # Empty list → no keyword prefix → no middot
    assert " · " not in output, (
        f"Expected no middot for empty keyword list, but output had it:\n{output}"
    )
    # Summary still renders
    assert "A curated summary that should still appear" in output, (
        f"Expected summary to still render with empty keyword list:\n{output}"
    )


def test_format_nudge_recall_keywords_nonempty_renders_kw_prefix():
    """Non-empty recall_keywords (1, 2, or 4+ entries) → keyword prefix renders
    (>=1 guard per PR #268, mirroring PR #235 at the Others-section path).
    Summary is used as body after the keyword prefix."""
    hook = _load_hook_module()

    for kw in (["only-one"], ["one", "two"], ["one", "two", "three", "four"]):
        claim_entry = _claim_entry(
            node_id="ob_test",
            claim="A claim long enough that the fallback would normally truncate it past the 120 character soft cap so we can see what renders here",
            confidence=0.80,
            recall_summary="A curated summary that should still appear since it is not None.",
            recall_keywords=kw,
        )
        result = _make_result(top_claims=[claim_entry])
        output = hook.format_nudge(result)
        # Keywords render as backtick-quoted prefix
        first_kw = kw[0]
        assert f"`{first_kw}`" in output, (
            f"Expected backtick-quoted first keyword for keywords={kw!r}:\n{output}"
        )
        # Summary still renders as body after keyword prefix
        assert "A curated summary that should still appear" in output, (
            f"Expected summary to still render with keywords={kw!r}:\n{output}"
        )


# ---- Test 12: Axiom in specials renders its confidence ----


def test_format_nudge_axiom_special_renders_confidence():
    """Specials with a confidence value (axioms, conjectures) render
    `(conf X.XX)` in the digest. Locks in the conf_prefix=True behavior for
    specials with confidence — deviates from the spec's literal conf_prefix=False
    suggestion because axiom/conjecture confidence carries real epistemic weight."""
    hook = _load_hook_module()

    axiom = _special_entry(
        node_id="ax_0001",
        claim="Honesty is structural, not ethical.",
        ntype="axiom",
        confidence=0.99,
    )
    result = _make_result(special_nodes=[axiom])
    output = hook.format_nudge(result)
    assert "(conf 0.99)" in output, (
        f"Expected '(conf 0.99)' for axiom in Specials line:\n{output}"
    )


# ---- Test 13: Specials without confidence (person/definition/goal/etc) don't render conf ----


def test_format_nudge_person_special_no_confidence_rendered():
    """Specials without a confidence value (person, definition, goal, question,
    lesson, contradiction) must NOT render `(conf X.XX)`. The None-guard in
    render_one_node_line suppresses the prefix when confidence is absent."""
    hook = _load_hook_module()

    person = _special_entry(
        node_id="pn_0005",
        claim="A person node used for confidence-rendering test.",
        ntype="person",
        confidence=None,  # persons have no confidence field
    )
    result = _make_result(special_nodes=[person])
    output = hook.format_nudge(result)
    assert "(conf " not in output, (
        f"Person special should not render confidence:\n{output}"
    )


# ---- Test 14: Others section renders keyword prefix for matched IDs not in top/special ----


def test_format_nudge_others_section_renders_keywords():
    """Matched IDs not in top_claims or specials appear in an Others: block.
    Only nodes with at least 1 recall_keyword are rendered (issue #234 fix,
    2026-05-20): bare-ID lines (no keywords) are dropped entirely as noise.
    Each rendered line shows `[id] kw1 · kw2 · ...`."""
    hook = _load_hook_module()

    claim = _claim_entry(
        node_id="ob_top",
        claim="Top claim.",
        confidence=0.85,
        recall_summary="Top claim summary.",
        recall_keywords=["top-kw1", "top-kw2", "top-kw3"],
    )
    matched_meta = [
        {"id": "ob_top", "type": "observation_factual",
         "recall_keywords": ["top-kw1", "top-kw2", "top-kw3"]},
        {"id": "ob_other_with_kw", "type": "observation_factual",
         "recall_keywords": ["recall_summary", "keywords", "surface-render"]},
        {"id": "ob_other_no_kw", "type": "observation_factual",
         "recall_keywords": None},
    ]
    result = _make_result(
        top_claims=[claim],
        matched_ids=["ob_top", "ob_other_with_kw", "ob_other_no_kw"],
        matched_meta=matched_meta,
    )
    output = hook.format_nudge(result)

    # Others: section header present (at least one keyword-bearing other exists)
    assert "Others:" in output, f"Expected Others: section header:\n{output}"
    # Node with keywords: full prefix
    assert "[ob_other_with_kw]" in output, f"Missing other-with-kw id:\n{output}"
    assert "`recall_summary`" in output, f"Missing keyword prefix:\n{output}"
    assert "`surface-render`" in output, f"Missing third keyword:\n{output}"
    # Node without keywords: dropped entirely (issue #234 Finding 2)
    assert "[ob_other_no_kw]" not in output, (
        f"No-keywords node should be dropped, not shown as bare ID:\n{output}"
    )
    # Top-rendered node should NOT appear in Others
    others_section = output[output.index("Others:"):]
    others_lines = [ln for ln in others_section.split("\n") if ln.startswith("    -")]
    assert all("ob_top" not in ln for ln in others_lines), (
        f"Top-rendered node leaked into Others:\n{output}"
    )


# ---- Test 14b: Others section renders at boundary values of >=1 (1 and 2 keywords) ----


def test_format_nudge_others_section_renders_one_and_two_keywords():
    """Boundary-value test for the Others-section >=1 keyword contract (PR #235).
    A node with exactly 1 keyword must render with that keyword; a node with
    exactly 2 keywords must render with both. Exercises the lower boundary of
    the >=1 gate that replaced the previous ==3 filter."""
    hook = _load_hook_module()

    # One keyword
    matched_meta_1 = [
        {"id": "ob_one_kw", "type": "observation_factual",
         "recall_keywords": ["single-keyword"]},
    ]
    result_1 = _make_result(
        match_count=1,
        matched_ids=["ob_one_kw"],
        matched_meta=matched_meta_1,
    )
    output_1 = hook.format_nudge(result_1)
    assert "Others:" in output_1, (
        f"Expected Others: section with 1-keyword node:\n{output_1}"
    )
    assert "[ob_one_kw]" in output_1, (
        f"1-keyword node should appear in Others:\n{output_1}"
    )
    assert "`single-keyword`" in output_1, (
        f"1-keyword node's keyword should render:\n{output_1}"
    )

    # Two keywords
    matched_meta_2 = [
        {"id": "ob_two_kw", "type": "observation_factual",
         "recall_keywords": ["first-keyword", "second-keyword"]},
    ]
    result_2 = _make_result(
        match_count=1,
        matched_ids=["ob_two_kw"],
        matched_meta=matched_meta_2,
    )
    output_2 = hook.format_nudge(result_2)
    assert "Others:" in output_2, (
        f"Expected Others: section with 2-keyword node:\n{output_2}"
    )
    assert "[ob_two_kw]" in output_2, (
        f"2-keyword node should appear in Others:\n{output_2}"
    )
    assert "`first-keyword`" in output_2, (
        f"First keyword should render for 2-keyword node:\n{output_2}"
    )
    assert "`second-keyword`" in output_2, (
        f"Second keyword should render for 2-keyword node:\n{output_2}"
    )


# ---- Test 15: Others section fallback to legacy IDs line when no top/special exist ----


def test_format_nudge_others_legacy_fallback_when_only_others():
    """If matched_meta is empty/absent and no specials or top_claims are
    rendered, the formatter falls back to the legacy `IDs: ...` line so the
    digest is never silently empty when matches exist."""
    hook = _load_hook_module()

    # No top_claims, no specials, just matched_ids present
    result = {
        "match_count": 2,
        "special_nodes": [],
        "top_claims": [],
        "type_counts": {},
        "age": {},
        "issues": {},
        "matched_ids": ["ob_only_1", "ob_only_2"],
        "matched_meta": [],  # empty meta — no Others rendering path
    }
    output = hook.format_nudge(result)
    # Falls back to legacy IDs line
    assert "IDs:" in output, (
        f"Expected fallback 'IDs:' line when no Others to render:\n{output}"
    )
    assert "ob_only_1" in output and "ob_only_2" in output, (
        f"Expected both matched IDs in fallback:\n{output}"
    )
