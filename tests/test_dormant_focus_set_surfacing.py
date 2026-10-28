"""Tests for #1975 layer 2 — prompt-time dormant focus-set surfacing in
engram-surface-hook.py's format_nudge().

Membership-checks the rendered Specials + Top claims tier node ids (the top
<=6 full-summary lines) against saved, non-active focus_sets. At most 2
pointer lines render, sharing the #1692 recall-injection char budget; these
lines are never written to the surfaced ledger.

Coverage (per issue #1975's spec test list, Layer 2):
  1. A match in a dormant (non-active) set renders a pointer line.
  2. A match that's ONLY in the currently-active set never renders.
  3. >2 matching sets -> top-2 by match count (ties by last_loaded_at desc).
  4. Budget exhaustion cuts set-lines the same way it cuts node lines.
  5. Empty/absent focus_sets -> zero-sets invariant (no set-line, telemetry
     zeroed, no behavior change from pre-#1975 output).
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook_module(engram_home: str):
    """Load the surface hook with ENGRAM_HOME patched BEFORE exec_module.

    The hook's import-time client-locate probe os.path.exists()es
    <ENGRAM_HOME>/engram_client.py at module scope, so an unpatched load
    reads the real ~/.engram even when every test later reassigns the
    module globals (read-only residue found by the #2103 colleague pass —
    same class as the attached-packs fix in this PR, third instance).
    """
    from unittest import mock
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook_dormant_set_test", str(hook_path))
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"ENGRAM_HOME": engram_home}):
        spec.loader.exec_module(mod)
    # Self-guard: the imported module must resolve ENGRAM_HOME to the tmp
    # dir, not a real agent home — fail loudly here, not nondeterministically.
    assert mod.ENGRAM_HOME == engram_home, mod.ENGRAM_HOME
    return mod


def _claim_entry(*, node_id, claim="a claim", confidence=0.85, ntype="observation_factual"):
    return {
        "id": node_id,
        "type": ntype,
        "claim": claim,
        "confidence": confidence,
        "recall_summary": None,
        "recall_keywords": ["kw"],
        "created_ago": "1h ago",
    }


def _make_result(*, top_claims=None, match_count=None):
    tc = top_claims or []
    mc = match_count if match_count is not None else len(tc)
    mids = [n["id"] for n in tc]
    mm = [{"id": n["id"], "type": n.get("type"), "recall_keywords": n.get("recall_keywords")} for n in tc]
    return {
        "match_count": mc,
        "special_nodes": [],
        "top_claims": tc,
        "type_counts": {},
        "age": {"not_recalled_recently": []},
        "issues": {},
        "matched_ids": mids,
        "matched_meta": mm,
    }


def _make_focus_sets_db(db_path, sets, active_set_name=None):
    """sets: list of (name, node_ids_list, last_loaded_at_str_or_None)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE focus_sets (
            name TEXT PRIMARY KEY, node_ids TEXT NOT NULL, description TEXT,
            created_at TEXT NOT NULL, last_loaded_at TEXT,
            load_count INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE focus_state (
            singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1),
            active_set_name TEXT
        )"""
    )
    for name, node_ids, last_loaded_at in sets:
        conn.execute(
            "INSERT INTO focus_sets (name, node_ids, description, created_at, last_loaded_at) "
            "VALUES (?, ?, '', '2026-08-10T10:00:00+00:00', ?)",
            (name, json.dumps(node_ids), last_loaded_at),
        )
    conn.execute(
        "INSERT INTO focus_state (singleton_key, active_set_name) VALUES (1, ?)",
        (active_set_name,),
    )
    conn.commit()
    conn.close()


class DormantFocusSetTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_dormant_set_test_")
        self.hook = self.__class__._hook = _load_hook_module(self.tmp)
        self.hook.ENGRAM_HOME = self.tmp
        self.hook.KNOWLEDGE_DB_PATH = os.path.join(self.tmp, "knowledge.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestDormantSetRendersLine(DormantFocusSetTestBase):
    def test_match_in_dormant_set_renders_pointer_line(self):
        _make_focus_sets_db(
            self.hook.KNOWLEDGE_DB_PATH,
            sets=[("baton-migration", ["ob_dset_0001"], None)],
            active_set_name=None,
        )
        result = _make_result(top_claims=[_claim_entry(node_id="ob_dset_0001")])
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        self.assertIn("💼 saved focus-set 'baton-migration' holds 1 node(s)", nudge)
        self.assertIn("ob_dset_0001", nudge)
        self.assertIn("engram_focus_load to swap it in", nudge)
        self.assertEqual(stats["sets_checked"], 1)
        self.assertEqual(stats["set_lines_rendered"], 1)

    def test_no_match_renders_nothing(self):
        _make_focus_sets_db(
            self.hook.KNOWLEDGE_DB_PATH,
            sets=[("unrelated-set", ["ob_other_9999"], None)],
            active_set_name=None,
        )
        result = _make_result(top_claims=[_claim_entry(node_id="ob_dset_0001")])
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        self.assertNotIn("💼 saved focus-set", nudge)
        self.assertEqual(stats["set_lines_rendered"], 0)


class TestActiveSetNeverRenders(DormantFocusSetTestBase):
    def test_active_set_membership_never_renders(self):
        _make_focus_sets_db(
            self.hook.KNOWLEDGE_DB_PATH,
            sets=[("active-thread", ["ob_dset_0001"], None)],
            active_set_name="active-thread",
        )
        result = _make_result(top_claims=[_claim_entry(node_id="ob_dset_0001")])
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        self.assertNotIn("💼 saved focus-set", nudge)
        self.assertEqual(stats["set_lines_rendered"], 0)


class TestTopTwoByMatchCount(DormantFocusSetTestBase):
    def test_more_than_two_matches_keeps_top_two_by_count(self):
        # set-a matches 1 id, set-b matches 2 ids, set-c matches 3 ids.
        _make_focus_sets_db(
            self.hook.KNOWLEDGE_DB_PATH,
            sets=[
                ("set-a", ["ob_dset_0001"], "2026-08-01T00:00:00+00:00"),
                ("set-b", ["ob_dset_0001", "ob_dset_0002"], "2026-08-02T00:00:00+00:00"),
                ("set-c", ["ob_dset_0001", "ob_dset_0002", "ob_dset_0003"], "2026-08-03T00:00:00+00:00"),
            ],
            active_set_name=None,
        )
        result = _make_result(top_claims=[
            _claim_entry(node_id="ob_dset_0001"),
            _claim_entry(node_id="ob_dset_0002"),
            _claim_entry(node_id="ob_dset_0003"),
        ])
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        self.assertIn("'set-c'", nudge)
        self.assertIn("'set-b'", nudge)
        self.assertNotIn("'set-a'", nudge)
        self.assertEqual(stats["set_lines_rendered"], 2)
        self.assertEqual(stats["sets_checked"], 3)

    def test_tie_broken_by_most_recent_last_loaded_at(self):
        # set-a and set-b both match 1 id (tie); set-b was loaded more recently.
        _make_focus_sets_db(
            self.hook.KNOWLEDGE_DB_PATH,
            sets=[
                ("set-a", ["ob_dset_0001"], "2026-08-01T00:00:00+00:00"),
                ("set-b", ["ob_dset_0002"], "2026-08-05T00:00:00+00:00"),
                ("set-c", ["ob_dset_0003"], "2026-08-03T00:00:00+00:00"),
            ],
            active_set_name=None,
        )
        result = _make_result(top_claims=[
            _claim_entry(node_id="ob_dset_0001"),
            _claim_entry(node_id="ob_dset_0002"),
            _claim_entry(node_id="ob_dset_0003"),
        ])
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        # All three tie on match count (1 each) -> top-2 by most-recent last_loaded_at.
        self.assertIn("'set-b'", nudge)  # 08-05, most recent
        self.assertIn("'set-c'", nudge)  # 08-03, second most recent
        self.assertNotIn("'set-a'", nudge)  # 08-01, oldest -> cut


class TestBudgetExhaustionCutsSetLines(DormantFocusSetTestBase):
    def test_exhausted_budget_cuts_set_lines(self):
        _make_focus_sets_db(
            self.hook.KNOWLEDGE_DB_PATH,
            sets=[("baton-migration", ["ob_dset_0001"], None)],
            active_set_name=None,
        )
        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w") as f:
            json.dump({"recall_budget": {"enabled": True, "total_chars": 1}}, f)

        result = _make_result(top_claims=[_claim_entry(node_id="ob_dset_0001")])
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        self.assertNotIn("💼 saved focus-set", nudge)
        self.assertEqual(stats["set_lines_rendered"], 0)
        # sets_checked still reflects the DB read (only rendering is cut).
        self.assertEqual(stats["sets_checked"], 1)


class TestZeroSetsInvariant(DormantFocusSetTestBase):
    def test_no_knowledge_db_renders_no_set_lines(self):
        """No knowledge.db at all (never created) -> layer 2 contributes
        nothing; telemetry zeroed; output unaffected."""
        result = _make_result(top_claims=[_claim_entry(node_id="ob_dset_0001")])
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        self.assertNotIn("💼 saved focus-set", nudge)
        self.assertEqual(stats["sets_checked"], 0)
        self.assertEqual(stats["set_lines_rendered"], 0)

    def test_empty_focus_sets_table_renders_no_set_lines(self):
        _make_focus_sets_db(self.hook.KNOWLEDGE_DB_PATH, sets=[], active_set_name=None)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_dset_0001")])
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        self.assertNotIn("💼 saved focus-set", nudge)
        self.assertEqual(stats["sets_checked"], 0)
        self.assertEqual(stats["set_lines_rendered"], 0)

    def test_empty_sets_output_byte_identical_to_layer2_disabled(self):
        """The airtight form of the zero-sets invariant: with an empty
        focus_sets table, the rendered nudge is byte-for-byte identical to
        the same render with layer 2 short-circuited entirely (the
        pre-#1975 equivalent: the block contributes zero candidate lines).
        """
        _make_focus_sets_db(self.hook.KNOWLEDGE_DB_PATH, sets=[], active_set_name=None)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_dset_0001")])
        with_feature = self.hook.format_nudge(result, stats={})

        original = self.hook._dormant_focus_set_candidates
        self.hook._dormant_focus_set_candidates = lambda rendered_ids: ([], 0)
        try:
            feature_disabled = self.hook.format_nudge(result, stats={})
        finally:
            self.hook._dormant_focus_set_candidates = original

        self.assertEqual(with_feature, feature_disabled)

    def test_zero_rendered_ids_still_reads_db(self):
        """Documented edge case (#1975 spec: 'one SELECT per prompt,
        unconditional'): even when no Specials/Top-claims ids rendered,
        the layer-2 DB read fires so sets_checked reflects the actual
        per-prompt read rather than a lucky-empty-set skip."""
        _make_focus_sets_db(
            self.hook.KNOWLEDGE_DB_PATH,
            sets=[("parked-set", ["ob_elsewhere_1"], None)],
            active_set_name=None,
        )
        # total > 0 (so the total==0 early-return doesn't fire) with zero
        # renderable claim entries -> no Specials/Top-claims ids render.
        result = _make_result(top_claims=[], match_count=3)
        stats = {}
        nudge = self.hook.format_nudge(result, stats=stats)

        self.assertNotIn("💼 saved focus-set", nudge)
        self.assertEqual(stats["sets_checked"], 1)
        self.assertEqual(stats["set_lines_rendered"], 0)


if __name__ == "__main__":
    unittest.main()
