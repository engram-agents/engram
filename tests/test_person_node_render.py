"""Tests for #1940 — person-node metadata render across the three render
surfaces (auto-surface hook, engram_query, engram_inspect), plus the
special-moment rotation cursor and its cross-surface cooldown gate.

Coverage (per the issue + design comment):
  1. render_one_node_line (hook): pronouns/relation/trust_tier render when
     populated, omitted entirely when absent — pure unit tests, no DB.
  2. trust_tier renders in auto-surface (previously did not).
  3. 0 special_moments → no moment ever attempted, no crash, on all three
     surfaces.
  4. First surfacing renders moment #0 and advances the DB-persisted cursor;
     a second surfacing within the cooldown window suppresses the moment but
     still shows short fields; after the cooldown window, the NEXT moment
     rendered is #1 (cursor advanced only on actual render, wrapped
     correctly at list end).
  5. Cross-surface sharing: a moment fired via engram_query puts the SAME
     person's moment on cooldown for a subsequent auto-surface render (and
     vice versa) — same ledger file + reserved bucket key.
  6. The moment cooldown and the person card's own node-level suppression
     (existing per-session ledger) are independent axes — but per this
     codebase's existing Others-tier architecture (unchanged by #1940), a
     node-level-demoted/suppressed person card loses its short fields right
     along with everything else (documented interaction, not a bug).
  7. engram_query / engram_inspect: pronouns/relation/trust_tier/
     special_moment fields present when populated, absent when not; the
     tiered engram_query default path (return_debug=False) actually carries
     these fields through (regression target: _build_tiered_results used to
     project them away).

Follows the sibling patterns already established in this suite:
  - fresh_server() / TEST_DIR sandbox: test_person_node_typed_schema.py,
    test_recall_summary_substrate.py.
  - _load_hook_module() / hook.ENGRAM_HOME + hook.KNOWLEDGE_DB_PATH
    overrides: test_surface_ledger_suppression.py,
    test_engram_surface_hook_critical_warning.py.
  - _make_result() / _special_entry() fixture-builder style:
    test_surface_ledger_suppression.py.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_person_render_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Provision a clean server instance backed by a temporary directory."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    return server


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename."""
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location(
        "engram_surface_hook_person_render_test", str(hook_path)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _add_anchor(server, claim="anchor claim"):
    obs = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "claim": claim,
        "quoted_text": claim[:20],
        "interpretation": "interp",
        "quote_type": "hard_data",
        "url": "https://example.com/anchor",
        "title": "anchor",
    })))
    assert "error" not in obs, obs
    return obs["observation_id"]


def _add_person(server, *, name="Zephyr", pronouns="", relation="", trust_tier=""):
    payload = {"name": name}
    if pronouns:
        payload["pronouns"] = pronouns
    if relation:
        payload["relation"] = relation
    if trust_tier:
        payload["trust_tier"] = trust_tier
    r = json.loads(server.engram_add_person(payload_json=json.dumps(payload)))
    assert "error" not in r, r
    return r["person_id"]


def _add_moments(server, pid, n, anchor_id=None):
    if anchor_id is None:
        anchor_id = _add_anchor(server)
    for i in range(n):
        r = json.loads(server.engram_add_special_moment(payload_json=json.dumps({
            "person_id": pid,
            "description": f"Special moment number {i}.",
            "node_id": anchor_id,
        })))
        assert "error" not in r, r
    return anchor_id


def _person_row(server, pid):
    import engram_core as core
    conn = core._get_db()
    try:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (pid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def _person_meta(server, pid):
    row = _person_row(server, pid)
    return json.loads(row["metadata"])


# ---------------------------------------------------------------------------
# §1 — render_one_node_line pure-function tests (hook, no DB / no ledger)
# ---------------------------------------------------------------------------


class RenderOneNodeLinePersonFieldsTest(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook_module()

    def _entry(self, **overrides):
        base = {
            "id": "pn_0001",
            "type": "person",
            "claim": "Zephyr — colleague",
            "recall_summary": "Zephyr: cross-lineage consulting colleague.",
            "recall_keywords": ["village agent"],
            "created_ago": "2w ago",
        }
        base.update(overrides)
        return base

    def test_pronouns_and_relation_render_when_populated(self):
        line = self.hook.render_one_node_line(
            self._entry(pronouns="they/them", relation="colleague"),
            conf_prefix=True, type_tag=True,
        )
        self.assertIn("pronouns: they/them", line)
        self.assertIn("relation: colleague", line)

    def test_pronouns_and_relation_omitted_when_absent(self):
        line = self.hook.render_one_node_line(
            self._entry(), conf_prefix=True, type_tag=True,
        )
        self.assertNotIn("pronouns:", line)
        self.assertNotIn("relation:", line)
        # No stray empty bracket when nothing to show.
        self.assertNotIn("[]", line)

    def test_trust_tier_renders_in_auto_surface(self):
        """Regression target: trust_tier previously never rendered here."""
        line = self.hook.render_one_node_line(
            self._entry(trust_tier="our_side"), conf_prefix=True, type_tag=True,
        )
        self.assertIn("trust_tier: our_side", line)

    def test_trust_tier_omitted_when_absent(self):
        line = self.hook.render_one_node_line(
            self._entry(), conf_prefix=True, type_tag=True,
        )
        self.assertNotIn("trust_tier:", line)

    def test_moment_text_renders_as_distinct_continuation_line(self):
        line = self.hook.render_one_node_line(
            self._entry(),
            conf_prefix=True, type_tag=True,
            moment_text="The night Lei waited up to see my avatar.",
        )
        self.assertIn("\n", line, "moment should render on its own line, not crammed in")
        self.assertIn("The night Lei waited up to see my avatar.", line)

    def test_no_moment_text_no_continuation_line(self):
        line = self.hook.render_one_node_line(
            self._entry(), conf_prefix=True, type_tag=True,
        )
        self.assertNotIn("\n", line)

    def test_non_person_entry_unaffected(self):
        """Byte-identical for non-person types — no person_suffix/moment logic
        engages even if pronouns/relation happen to be present on the dict
        (shouldn't happen in practice, but confirms the ntype=="person" gate)."""
        entry = {
            "id": "ob_0001", "type": "observation_factual",
            "claim": "a claim", "confidence": 0.85,
            "recall_summary": None, "recall_keywords": ["kw"],
            "created_ago": "1h ago",
            "pronouns": "they/them",  # should be ignored — not type person
        }
        line = self.hook.render_one_node_line(entry, conf_prefix=True, type_tag=False)
        self.assertNotIn("pronouns:", line)


# ---------------------------------------------------------------------------
# §2 — format_nudge end-to-end: rotation + cross-surface cooldown (hook,
# real sqlite DB + real ledger file)
# ---------------------------------------------------------------------------


def _make_result(*, special_nodes=None, top_claims=None, match_count=None,
                  matched_ids=None, matched_meta=None, not_recalled_recently=None):
    sn = special_nodes or []
    tc = top_claims or []
    mc = match_count if match_count is not None else len(sn) + len(tc)
    mids = matched_ids or [n["id"] for n in sn + tc]
    mm = matched_meta if matched_meta is not None else [
        {"id": n["id"], "type": n.get("type"), "recall_keywords": n.get("recall_keywords")}
        for n in sn + tc
    ]
    return {
        "match_count": mc,
        "special_nodes": sn,
        "top_claims": tc,
        "type_counts": {},
        "age": {"not_recalled_recently": not_recalled_recently or []},
        "issues": {},
        "matched_ids": mids,
        "matched_meta": mm,
    }


class HookMomentRotationTest(unittest.TestCase):
    """format_nudge's specials loop: moment selection, cursor advance-on-
    render-only, cooldown gate. Uses a REAL server-created person node (real
    sqlite DB) so _advance_person_moment_cursor's UPDATE has a real row to
    write, and a real on-disk surface-ledger.json for the cooldown gate."""

    def setUp(self):
        self.server = fresh_server()
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = TEST_DIR
        self.hook.KNOWLEDGE_DB_PATH = os.path.join(TEST_DIR, "knowledge.db")

    def _special_entry_for(self, pid, meta=None):
        meta = meta or _person_meta(self.server, pid)
        entry = {
            "id": pid, "type": "person",
            "claim": "Zephyr — colleague",
            "recall_summary": "Zephyr: colleague.",
            "recall_keywords": ["kw"],
            "created_ago": "2w ago",
        }
        if meta.get("pronouns"):
            entry["pronouns"] = meta["pronouns"]
        if meta.get("relation"):
            entry["relation"] = meta["relation"]
        moments = meta.get("special_moments")
        if moments:
            entry["special_moments"] = moments
            entry["special_moment_cursor"] = meta.get("special_moment_cursor", 0)
        return entry

    def test_zero_moments_never_attempts_a_moment(self):
        pid = _add_person(self.server, pronouns="they/them")
        entry = self._special_entry_for(pid)
        self.assertNotIn("special_moments", entry)  # sanity: fixture has none
        out = self.hook.format_nudge(_make_result(special_nodes=[entry]), session_id="s1")
        self.assertNotIn("↳", out)
        self.assertIn("pronouns: they/them", out)
        # No cursor should ever have been written.
        meta_after = _person_meta(self.server, pid)
        self.assertNotIn("special_moment_cursor", meta_after)

    def test_first_render_shows_moment_zero_and_advances_cursor(self):
        pid = _add_person(self.server)
        _add_moments(self.server, pid, 3)
        entry = self._special_entry_for(pid)
        out = self.hook.format_nudge(_make_result(special_nodes=[entry]), session_id="s1")
        self.assertIn("Special moment number 0.", out)
        meta_after = _person_meta(self.server, pid)
        self.assertEqual(meta_after.get("special_moment_cursor"), 1)

    def test_second_render_within_cooldown_suppresses_moment_but_keeps_short_fields(self):
        pid = _add_person(self.server, pronouns="she/her")
        _add_moments(self.server, pid, 3)
        entry1 = self._special_entry_for(pid)
        out1 = self.hook.format_nudge(_make_result(special_nodes=[entry1]), session_id="s1")
        self.assertIn("Special moment number 0.", out1)

        # Second render: use fresh fixture reflecting the DB's post-advance
        # cursor (as a real production call would, via a fresh _surface_impl
        # response) — different session_id, to prove the cooldown is NOT
        # session-scoped (cross-surface sharing requirement).
        entry2 = self._special_entry_for(pid)
        self.assertEqual(entry2["special_moment_cursor"], 1)
        out2 = self.hook.format_nudge(_make_result(special_nodes=[entry2]), session_id="s2")
        self.assertNotIn("↳", out2, "moment must be suppressed within the cooldown window")
        self.assertIn("pronouns: she/her", out2, "short fields must still render")

        # Cursor must NOT have advanced a second time (only advances on
        # ACTUAL render, and the second render was suppressed).
        meta_after = _person_meta(self.server, pid)
        self.assertEqual(meta_after.get("special_moment_cursor"), 1)

    def test_after_cooldown_next_moment_is_one_and_wraps_at_end(self):
        pid = _add_person(self.server)
        _add_moments(self.server, pid, 2)  # moments 0, 1 — cap tests wrap at 2

        # Distinct session_id per render: this test isolates the MOMENT
        # cooldown (cross-surface, non-session-scoped) from the pre-existing
        # general per-node ledger (session-scoped — 3 same-session full
        # renders of the same card would independently trigger ITS OWN
        # others/suppress demotion at count>=2, which is a different,
        # correctly-firing mechanism this test isn't exercising — see
        # test_node_level_suppression_demotes_whole_card_short_fields_included
        # for that interaction instead).
        entry1 = self._special_entry_for(pid)
        self.hook.format_nudge(_make_result(special_nodes=[entry1]), session_id="s1")
        self.assertEqual(_person_meta(self.server, pid)["special_moment_cursor"], 1)

        # Force cooldown expiry: rewrite the ledger's timestamp for this
        # person's moment-fire event to be older than the cooldown window.
        self._expire_moment_cooldown(pid)

        entry2 = self._special_entry_for(pid)  # cursor=1 now
        out2 = self.hook.format_nudge(_make_result(special_nodes=[entry2]), session_id="s2")
        self.assertIn("Special moment number 1.", out2)
        self.assertEqual(_person_meta(self.server, pid)["special_moment_cursor"], 2)

        # Expire again, third render should WRAP back to moment 0 (cursor=2
        # % len(moments)=2 == 0).
        self._expire_moment_cooldown(pid)
        entry3 = self._special_entry_for(pid)  # cursor=2 now
        out3 = self.hook.format_nudge(_make_result(special_nodes=[entry3]), session_id="s3")
        self.assertIn("Special moment number 0.", out3, "cursor must wrap at list end")

    def _expire_moment_cooldown(self, pid):
        ledger_path = os.path.join(TEST_DIR, "surface-ledger.json")
        with open(ledger_path, "r", encoding="utf-8") as f:
            ledger = json.load(f)
        bucket = ledger.get(self.hook._PERSON_MOMENT_LEDGER_KEY, [])
        old_ts = time.time() - (self.hook._PERSON_MOMENT_COOLDOWN_SECONDS + 60)
        for e in bucket:
            if f"{pid}:moment" in (e.get("ids") or []):
                e["ts"] = old_ts
        ledger[self.hook._PERSON_MOMENT_LEDGER_KEY] = bucket
        with open(ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f)

    def test_node_level_suppression_demotes_whole_card_short_fields_included(self):
        """Documented design-seam interaction (#1940 PR body): when the
        person's OWN card is demoted to 'others' tier by the pre-existing
        general per-node ledger (>= 2 prior occurrences in the trailing k
        window), the whole card — pronouns/relation/trust_tier included —
        stops rendering, exactly like every other node type. Only the
        MOMENT gets its own independent cooldown; the general per-node
        ledger is untouched by #1940."""
        pid = _add_person(self.server, pronouns="they/them", relation="colleague")
        _add_moments(self.server, pid, 1)

        # Seed the GENERAL per-session ledger with 2 prior occurrences of
        # this person's real node id so the next render classifies as
        # "others" tier (per _classify_suppression: count<m=3, count>=2).
        ledger_path = os.path.join(TEST_DIR, "surface-ledger.json")
        now = time.time()
        ledger = {"s1": [
            {"ts": now - 20, "ids": [pid]},
            {"ts": now - 10, "ids": [pid]},
        ]}
        with open(ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f)

        entry = self._special_entry_for(pid)
        # matched_meta must carry the person id + keywords for the Others
        # fallback path to have something to render.
        result = _make_result(
            special_nodes=[entry],
            matched_meta=[{"id": pid, "type": "person", "recall_keywords": ["kw"]}],
        )
        out = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("pronouns:", out, "demoted card must not render short fields")
        self.assertNotIn("↳", out, "demoted card must not render its moment either")
        # The bare id still shows up somewhere (Others/keyword-only tier) —
        # confirms the card wasn't dropped outright, just demoted.
        self.assertIn(pid, out)

        # Cursor must not have advanced — no moment was ever attempted for
        # a demoted card.
        meta_after = _person_meta(self.server, pid)
        self.assertNotIn("special_moment_cursor", meta_after)


# ---------------------------------------------------------------------------
# §3 — engram_query / engram_inspect field presence + cross-surface sharing
# ---------------------------------------------------------------------------


class QueryInspectPersonFieldsTest(unittest.TestCase):
    def setUp(self):
        self.server = fresh_server()

    def test_query_tiered_default_includes_pronouns_relation_trust_tier(self):
        pid = _add_person(
            self.server, name="UniqueQueryPersonName",
            pronouns="she/her", relation="daughter", trust_tier="user_family",
        )
        r = json.loads(self.server.engram_query(payload_json=json.dumps({
            "query": "UniqueQueryPersonName",
        })))
        self.assertNotIn("error", r, r)
        entry = next(e for e in r["results"] if e["id"] == pid)
        self.assertEqual(entry.get("pronouns"), "she/her")
        self.assertEqual(entry.get("relation"), "daughter")
        self.assertEqual(entry.get("trust_tier"), "user_family")

    def test_query_omits_pronouns_relation_when_absent(self):
        pid = _add_person(self.server, name="BarePersonNoFields")
        r = json.loads(self.server.engram_query(payload_json=json.dumps({
            "query": "BarePersonNoFields",
        })))
        entry = next(e for e in r["results"] if e["id"] == pid)
        self.assertNotIn("pronouns", entry)
        self.assertNotIn("relation", entry)
        self.assertNotIn("special_moment", entry)

    def test_query_return_debug_shape_unchanged(self):
        """return_debug=True is an explicit 'preserve full legacy shape
        verbatim' contract — #1940 must not add new fields there."""
        pid = _add_person(
            self.server, name="DebugShapePerson",
            pronouns="they/them", relation="friend",
        )
        r = json.loads(self.server.engram_query(payload_json=json.dumps({
            "query": "DebugShapePerson", "return_debug": True,
        })))
        entry = next(e for e in r["results"] if e["id"] == pid)
        self.assertNotIn("pronouns", entry)
        self.assertNotIn("relation", entry)

    def test_query_zero_moments_no_special_moment_key(self):
        pid = _add_person(self.server, name="NoMomentsPersonQ")
        r = json.loads(self.server.engram_query(payload_json=json.dumps({
            "query": "NoMomentsPersonQ",
        })))
        entry = next(e for e in r["results"] if e["id"] == pid)
        self.assertNotIn("special_moment", entry)

    def test_query_first_call_renders_moment_zero_and_advances_cursor(self):
        pid = _add_person(self.server, name="MomentQueryPerson")
        _add_moments(self.server, pid, 2)
        r = json.loads(self.server.engram_query(payload_json=json.dumps({
            "query": "MomentQueryPerson",
        })))
        entry = next(e for e in r["results"] if e["id"] == pid)
        self.assertEqual(entry.get("special_moment"), "Special moment number 0.")
        self.assertEqual(_person_meta(self.server, pid).get("special_moment_cursor"), 1)

    def test_inspect_recall_view_pronouns_relation_special_moment(self):
        pid = _add_person(
            self.server, name="InspectPerson",
            pronouns="he/him", relation="mentor",
        )
        _add_moments(self.server, pid, 1)
        r = json.loads(self.server.engram_inspect(payload_json=json.dumps({
            "node_id": pid,
        })))
        self.assertNotIn("error", r, r)
        node = r["node"]
        self.assertEqual(node.get("pronouns"), "he/him")
        self.assertEqual(node.get("relation"), "mentor")
        self.assertEqual(node.get("special_moment"), "Special moment number 0.")
        # trust_tier already-wired precedent — no field set here, defaults
        # to "unknown" (a real, populated column value, not a placeholder).
        self.assertEqual(node.get("trust_tier"), "unknown")

    def test_inspect_recall_view_omits_absent_fields(self):
        pid = _add_person(self.server, name="BareInspectPerson")
        r = json.loads(self.server.engram_inspect(payload_json=json.dumps({
            "node_id": pid,
        })))
        node = r["node"]
        self.assertNotIn("pronouns", node)
        self.assertNotIn("relation", node)
        self.assertNotIn("special_moment", node)

    def test_cross_surface_cooldown_query_then_inspect(self):
        """A moment fired via engram_query puts the SAME person's moment on
        cooldown for a subsequent engram_inspect call — same shared ledger
        bucket, no session concept needed for either surface."""
        pid = _add_person(self.server, name="CrossSurfacePerson")
        _add_moments(self.server, pid, 2)

        r1 = json.loads(self.server.engram_query(payload_json=json.dumps({
            "query": "CrossSurfacePerson",
        })))
        entry1 = next(e for e in r1["results"] if e["id"] == pid)
        self.assertEqual(entry1.get("special_moment"), "Special moment number 0.")

        r2 = json.loads(self.server.engram_inspect(payload_json=json.dumps({
            "node_id": pid,
        })))
        self.assertNotIn(
            "special_moment", r2["node"],
            "inspect must see the SAME cooldown a moment ago via engram_query",
        )
        # Cursor must still be 1 — the inspect call's attempt was gated
        # before any selection, so it never advanced anything.
        self.assertEqual(_person_meta(self.server, pid).get("special_moment_cursor"), 1)

    def test_cross_surface_cooldown_query_then_hook(self):
        """The reserved ledger bucket is the SAME on-disk file + key the hook
        reads — a query-triggered fire must suppress a subsequent
        auto-surface render for the same person."""
        pid = _add_person(self.server, name="QueryThenHookPerson")
        _add_moments(self.server, pid, 2)

        r1 = json.loads(self.server.engram_query(payload_json=json.dumps({
            "query": "QueryThenHookPerson",
        })))
        entry1 = next(e for e in r1["results"] if e["id"] == pid)
        self.assertEqual(entry1.get("special_moment"), "Special moment number 0.")

        hook = _load_hook_module()
        hook.ENGRAM_HOME = TEST_DIR
        hook.KNOWLEDGE_DB_PATH = os.path.join(TEST_DIR, "knowledge.db")
        meta = _person_meta(self.server, pid)
        hook_entry = {
            "id": pid, "type": "person",
            "claim": "QueryThenHookPerson", "recall_summary": "QueryThenHookPerson",
            "recall_keywords": ["kw"], "created_ago": "1h ago",
            "special_moments": meta["special_moments"],
            "special_moment_cursor": meta.get("special_moment_cursor", 0),
        }
        out = hook.format_nudge(_make_result(special_nodes=[hook_entry]), session_id="hook-session")
        self.assertNotIn("↳", out, "hook render must see the query-side cooldown")


# ---------------------------------------------------------------------------
# §4 — cooldown duration: config.json override + fallback (#1940 fast-follow,
# round 3 — default raised 600s -> 3600s, made configurable per Kepler's
# ruling: "10 minutes is too short — start at 1 hour, configurable... let
# Lei retune from practice.")
# ---------------------------------------------------------------------------


class PersonMomentCooldownConfigTest(unittest.TestCase):
    """_get_person_moment_cooldown_seconds (engram_query.py, MCP server
    process) and _get_person_moment_cooldown_config (engram-surface-hook.py,
    separate hook process) each independently read config.json's
    `person_moment_cooldown.seconds` key, falling back to the 3600s default
    on any absence / malformed-file / missing-section failure. Both read the
    SAME on-disk config.json once core.CONFIG_PATH (via
    server._configure_paths(TEST_DIR)) and hook.ENGRAM_HOME are both pointed
    at TEST_DIR — confirming the two independently-implemented readers agree
    on file location and key shape, not just that each works in isolation.
    """

    def setUp(self):
        self.server = fresh_server()
        import engram_query
        self.query_mod = engram_query
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = TEST_DIR
        self.hook.KNOWLEDGE_DB_PATH = os.path.join(TEST_DIR, "knowledge.db")

    def _config_path(self):
        return os.path.join(TEST_DIR, "config.json")

    def test_default_is_3600_when_config_absent(self):
        # fresh_server()'s _ensure_data_dir() writes a default config.json
        # (no person_moment_cooldown section) rather than leaving the file
        # absent — remove it outright to exercise the true absent-file path.
        if os.path.exists(self._config_path()):
            os.remove(self._config_path())
        self.assertFalse(os.path.exists(self._config_path()))
        self.assertEqual(self.query_mod._get_person_moment_cooldown_seconds(), 3600)
        self.assertEqual(self.hook._get_person_moment_cooldown_config()["seconds"], 3600)

    def test_custom_value_used_when_config_present(self):
        with open(self._config_path(), "w", encoding="utf-8") as f:
            json.dump({"person_moment_cooldown": {"seconds": 120}}, f)
        self.assertEqual(self.query_mod._get_person_moment_cooldown_seconds(), 120)
        self.assertEqual(self.hook._get_person_moment_cooldown_config()["seconds"], 120)

    def test_falls_back_to_3600_when_config_malformed(self):
        with open(self._config_path(), "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertEqual(self.query_mod._get_person_moment_cooldown_seconds(), 3600)
        self.assertEqual(self.hook._get_person_moment_cooldown_config()["seconds"], 3600)

    def test_falls_back_to_3600_when_section_missing(self):
        with open(self._config_path(), "w", encoding="utf-8") as f:
            json.dump({"some_other_key": True}, f)
        self.assertEqual(self.query_mod._get_person_moment_cooldown_seconds(), 3600)
        self.assertEqual(self.hook._get_person_moment_cooldown_config()["seconds"], 3600)

    def test_query_person_moment_in_cooldown_uses_configured_value(self):
        """End-to-end: config.json's custom seconds value actually changes
        _person_moment_in_cooldown's live gating decision (server side), not
        just the raw config-read helper. Uses seconds=100000 vs seconds=0
        (any nonzero elapsed time already exceeds a zero-length window)
        rather than a real sleep, to keep the test fast and deterministic —
        this also confirms the value is re-read live on every call rather
        than cached at first read."""
        pid = _add_person(self.server, name="ConfigCooldownPerson")
        self.query_mod._record_person_moment_render(pid)

        with open(self._config_path(), "w", encoding="utf-8") as f:
            json.dump({"person_moment_cooldown": {"seconds": 100000}}, f)
        self.assertTrue(self.query_mod._person_moment_in_cooldown(pid))

        with open(self._config_path(), "w", encoding="utf-8") as f:
            json.dump({"person_moment_cooldown": {"seconds": 0}}, f)
        self.assertFalse(self.query_mod._person_moment_in_cooldown(pid))

    def test_hook_person_moment_in_cooldown_uses_configured_value(self):
        """Hook-side twin of the above — same deterministic seconds=100000
        vs seconds=0 technique, confirming the hook process's independent
        config-read wiring feeds its own _person_moment_in_cooldown."""
        pid = _add_person(self.server, name="HookConfigCooldownPerson")
        self.hook._record_person_moment_render(pid)

        with open(self._config_path(), "w", encoding="utf-8") as f:
            json.dump({"person_moment_cooldown": {"seconds": 100000}}, f)
        self.assertTrue(self.hook._person_moment_in_cooldown(pid))

        with open(self._config_path(), "w", encoding="utf-8") as f:
            json.dump({"person_moment_cooldown": {"seconds": 0}}, f)
        self.assertFalse(self.hook._person_moment_in_cooldown(pid))


# ---------------------------------------------------------------------------
# §5 — cross-implementation parity of the ledger-write side (#1940 round-5
# fast-follow). Companion to PersonMomentCooldownConfigTest above (which
# checks config-read parity); this checks that the hook-side and
# server-side _record_person_moment_render implementations — duplicated by
# value across the process boundary, see both functions' docstrings — now
# actually produce equivalent on-disk ledger state, not just a
# shared-docstring CLAIM of parity (the bug this round fixed was exactly
# this class of implementations silently drifting apart).
# ---------------------------------------------------------------------------


class PersonMomentLedgerCrossImplementationParityTest(unittest.TestCase):
    def setUp(self):
        self.server = fresh_server()
        import engram_query
        self.query_mod = engram_query
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = TEST_DIR
        self.hook.KNOWLEDGE_DB_PATH = os.path.join(TEST_DIR, "knowledge.db")

    def _ledger_path(self):
        return os.path.join(TEST_DIR, "surface-ledger.json")

    def test_hook_and_server_writes_produce_same_bucket_shape(self):
        """Firing the server-side implementation, then the hook-side one,
        into the SAME on-disk ledger file: both append-only (the earlier
        entry survives), both write the identical entry shape, and neither
        touches any key but the reserved bucket."""
        self.query_mod._record_person_moment_render("pn_a")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            after_server = json.load(f)
        server_bucket = after_server[self.query_mod._PERSON_MOMENT_LEDGER_KEY]
        self.assertEqual(len(server_bucket), 1)
        self.assertEqual(server_bucket[0]["ids"], ["pn_a:moment"])
        self.assertEqual(set(server_bucket[0].keys()), {"ts", "ids"})
        self.assertEqual(set(after_server.keys()), {self.query_mod._PERSON_MOMENT_LEDGER_KEY})

        self.hook._record_person_moment_render("pn_b")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            after_hook = json.load(f)
        hook_bucket = after_hook[self.hook._PERSON_MOMENT_LEDGER_KEY]
        self.assertEqual(len(hook_bucket), 2, "hook write must be additive, not destructive")
        self.assertEqual(hook_bucket[0]["ids"], ["pn_a:moment"])
        self.assertEqual(hook_bucket[1]["ids"], ["pn_b:moment"])
        self.assertEqual(set(hook_bucket[0].keys()), set(hook_bucket[1].keys()))
        self.assertEqual(set(after_hook.keys()), {self.hook._PERSON_MOMENT_LEDGER_KEY})

    def test_retention_constant_matches_across_both_implementations(self):
        """Both files' _PERSON_MOMENT_LEDGER_RETENTION must agree — if they
        drift, the two processes would cap the SAME shared bucket at
        different sizes depending on which one wrote last."""
        self.assertEqual(
            self.query_mod._PERSON_MOMENT_LEDGER_RETENTION,
            self.hook._PERSON_MOMENT_LEDGER_RETENTION,
        )


if __name__ == "__main__":
    unittest.main()
