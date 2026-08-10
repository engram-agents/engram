"""Tests for the per-prompt injection budget + render-size telemetry in
engram-surface-hook.py (recall-triggering blueprint §3-P4, issue #1692).

Follows the sibling patterns already established in this test suite:
  - test_surface_ledger_suppression.py for the hyphenated-filename
    module-loading helper, LedgerTestBase-style isolated ENGRAM_HOME, and the
    _claim_entry / _special_entry / _make_result fixture-builder pattern.
  - test_surface_hook_attached_packs.py's TestMiniFooterPointsAtEngramPkg for
    the full hook.main() in-process integration-test harness (mocked stdin,
    query_daemon/query_fts_fallback, context_tracker) — reused here for the
    render_size-emission test.

Coverage (per the resolved design — see PR body for the "existing render
order as relevance signal" simplification from the original three-factor
novelty × relevance × type-weight formula):
  1. _get_budget_config() reading + defaults + malformed-config fallback.
  2. Backward-compat: default config (recall_budget absent, or default
     total_chars) renders byte-identical to the pre-#1692 legacy call shape
     for a realistically-sized render — nothing gets cut.
  3. Budget binds: an explicit small total_chars forces cuts across tiers,
     in fixed tier-priority order (Specials → Top claims → not-recalled
     reservation → Others), never re-sorting within a tier.
  4. stats out-parameter: populated (including on the total==0 early return),
     correct chars_rendered / nodes_shown / nodes_suppressed / nodes_cut_budget.
  5. Budget-cut candidates are NOT written to the surfaced ledger (the agent
     never saw them — same discipline as the render-slice-must-match-ledger-
     write fix from #1697).
  6. recall_budget.enabled=False is a hard kill switch: unlimited budget
     regardless of total_chars, byte-identical to the pre-#1692 fixed caps.
  7. engram.surface.render_size event actually gets emitted through
     hook.main() with the expected fields.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename."""
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook_budget_test", str(hook_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixture builders (mirrors test_surface_ledger_suppression.py's style)
# ---------------------------------------------------------------------------


def _claim_entry(*, node_id, claim="a claim", confidence=0.85,
                  recall_summary=None, recall_keywords=None, ntype="observation_factual"):
    return {
        "id": node_id,
        "type": ntype,
        "claim": claim,
        "confidence": confidence,
        "recall_summary": recall_summary,
        "recall_keywords": recall_keywords or ["kw"],
        "created_ago": "1h ago",
    }


def _special_entry(*, node_id, claim="an axiom", ntype="axiom", confidence=0.9,
                    recall_summary=None, recall_keywords=None):
    return {
        "id": node_id,
        "type": ntype,
        "claim": claim,
        "confidence": confidence,
        "recall_summary": recall_summary,
        "recall_keywords": recall_keywords or ["kw"],
        "created_ago": "2d ago",
    }


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


def _dense_result():
    """A realistically dense render: 3 specials, 3 top claims, 2 reservable
    not-recalled candidates, and several Others candidates — exercises every
    tier the fixed 3/3/2/15 caps + budget allocator touch."""
    specials = [_special_entry(node_id=f"ax_budget_{i:04d}", claim=f"axiom claim {i}") for i in range(3)]
    top_claims = [_claim_entry(node_id=f"ob_budget_{i:04d}", claim=f"top claim {i}") for i in range(3)]
    others_meta = [
        {"id": f"ob_budget_other_{i:04d}", "type": "observation", "recall_keywords": [f"kw{i}"]}
        for i in range(5)
    ]
    reservable_ids = [f"ob_budget_nr_{i:04d}" for i in range(2)]
    reservable_meta = [
        {"id": nid, "type": "observation", "recall_keywords": [f"nrkw{i}"]}
        for i, nid in enumerate(reservable_ids)
    ]
    matched_meta = [
        {"id": n["id"], "type": n.get("type"), "recall_keywords": n.get("recall_keywords")}
        for n in specials + top_claims
    ] + others_meta + reservable_meta
    matched_ids = [n["id"] for n in specials + top_claims] + [m["id"] for m in others_meta] + reservable_ids
    return _make_result(
        special_nodes=specials,
        top_claims=top_claims,
        matched_ids=matched_ids,
        matched_meta=matched_meta,
        not_recalled_recently=reservable_ids,
    )


class BudgetTestBase(unittest.TestCase):
    """Common setUp/tearDown: isolated ENGRAM_HOME per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_budget_test_")
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, data: dict) -> None:
        with open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)


# ---------------------------------------------------------------------------
# 1. _get_budget_config() — reading + defaults + malformed fallback
# ---------------------------------------------------------------------------


class TestBudgetConfigReading(BudgetTestBase):
    def test_defaults_when_config_absent(self):
        cfg = self.hook._get_budget_config()
        self.assertEqual(cfg, {
            "total_chars": self.hook.DEFAULT_RECALL_BUDGET_TOTAL_CHARS,
            "enabled": self.hook.DEFAULT_RECALL_BUDGET_ENABLED,
        })

    def test_reads_config_section(self):
        self._write_config({"recall_budget": {"total_chars": 1234, "enabled": False}})
        cfg = self.hook._get_budget_config()
        self.assertEqual(cfg, {"total_chars": 1234, "enabled": False})

    def test_partial_config_falls_back_per_field(self):
        self._write_config({"recall_budget": {"total_chars": 999}})
        cfg = self.hook._get_budget_config()
        self.assertEqual(cfg["total_chars"], 999)
        self.assertEqual(cfg["enabled"], self.hook.DEFAULT_RECALL_BUDGET_ENABLED)

    def test_malformed_config_falls_back_to_defaults(self):
        with open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8") as f:
            f.write("{not valid json")
        cfg = self.hook._get_budget_config()
        self.assertEqual(cfg, {
            "total_chars": self.hook.DEFAULT_RECALL_BUDGET_TOTAL_CHARS,
            "enabled": self.hook.DEFAULT_RECALL_BUDGET_ENABLED,
        })

    def test_section_absent_falls_back_to_defaults(self):
        self._write_config({"some_other_section": {}})
        cfg = self.hook._get_budget_config()
        self.assertEqual(cfg["total_chars"], self.hook.DEFAULT_RECALL_BUDGET_TOTAL_CHARS)
        self.assertEqual(cfg["enabled"], self.hook.DEFAULT_RECALL_BUDGET_ENABLED)


# ---------------------------------------------------------------------------
# 2. Backward-compat: default config renders byte-identical to legacy shape
# ---------------------------------------------------------------------------


class TestBackwardCompatDefault(BudgetTestBase):
    def test_no_config_default_budget_never_cuts_dense_render(self):
        """A dense render (3 specials + 3 top claims + 2 reservations + 5
        Others candidates) under the default (no config.json) budget renders
        every candidate — the generous default must never bind in practice."""
        result = _dense_result()
        stats = {}
        nudge = self.hook.format_nudge(result, session_id="s1", stats=stats)
        self.assertFalse(stats["budget_exhausted"])
        self.assertEqual(stats["nodes_cut_budget"], 0)
        for i in range(3):
            self.assertIn(f"[ax_budget_{i:04d}]", nudge)
            self.assertIn(f"[ob_budget_{i:04d}]", nudge)
        for i in range(2):
            self.assertIn(f"[ob_budget_nr_{i:04d}]", nudge)
        for i in range(5):
            self.assertIn(f"[ob_budget_other_{i:04d}]", nudge)

    def test_legacy_call_shape_byte_identical_to_stats_tracked_call(self):
        """Adding the stats out-parameter (or passing session_id, under the
        generous default budget) must not change a single byte of the
        rendered nudge for a realistically-sized render — the budget layer
        is purely an allocator over content that already fit."""
        result = _dense_result()
        legacy_nudge = self.hook.format_nudge(result)  # no session_id, no stats — pure legacy shape
        tracked_nudge = self.hook.format_nudge(result, session_id="s2", stats={})
        # Suppression is inactive either way (fresh session / no ledger
        # history) so the only variable under test is the budget layer.
        self.assertEqual(legacy_nudge, tracked_nudge)

    def test_explicit_generous_budget_matches_default(self):
        """An explicit but generous recall_budget.total_chars produces the
        same render as the implicit default — confirms the config read path
        and the built-in default agree in effect, not just in shape."""
        self._write_config({"recall_budget": {"total_chars": 50000, "enabled": True}})
        result = _dense_result()
        nudge = self.hook.format_nudge(result, session_id="s3", stats={})
        no_config_hook = _load_hook_module()
        no_config_hook.ENGRAM_HOME = tempfile.mkdtemp(prefix="engram_budget_nocfg_")
        try:
            nudge_default = no_config_hook.format_nudge(result, session_id="s3", stats={})
            self.assertEqual(nudge, nudge_default)
        finally:
            import shutil
            shutil.rmtree(no_config_hook.ENGRAM_HOME, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. Budget binds: a small explicit total_chars forces cuts, tier-priority
#    order preserved, no re-sorting within a tier.
# ---------------------------------------------------------------------------


class TestBudgetBinds(BudgetTestBase):
    def test_tiny_budget_cuts_everything(self):
        """A budget too small for even one line renders no node content at
        all (only structural header/footer lines survive)."""
        self._write_config({"recall_budget": {"total_chars": 5, "enabled": True}})
        result = _dense_result()
        stats = {}
        nudge = self.hook.format_nudge(result, session_id="s1", stats=stats)
        self.assertTrue(stats["budget_exhausted"])
        self.assertEqual(stats["nodes_shown"], 0)
        self.assertGreater(stats["nodes_cut_budget"], 0)
        self.assertNotIn("ax_budget_", nudge)
        self.assertNotIn("ob_budget_", nudge)

    def test_mid_size_budget_cuts_lower_priority_tiers_first(self):
        """A budget sized to fit exactly Specials + Top claims cuts the
        lower-priority not-recalled reservation AND Others tiers while
        leaving the higher-priority tiers intact — confirms fixed
        tier-priority order (Specials → Top claims → not-recalled
        reservation → Others) is respected, not re-sorted.

        Measures the budget threshold via stats["chars_rendered"] (the exact
        node-line-only quantity the allocator spends against), NOT the full
        rendered string length — the header/Noteworthy/section-title/footer
        lines are NOT counted against the budget (see format_nudge's
        docstring), so sizing the budget off len(nudge) would include that
        structural slack and could let a stray Others line slip through,
        defeating the tier-boundary assertion below.
        """
        result = _dense_result()
        partial_result = _make_result(
            special_nodes=result["special_nodes"],
            top_claims=result["top_claims"],
            matched_meta=[
                m for m in result["matched_meta"]
                if m["id"].startswith(("ax_budget_", "ob_budget_0", "ob_budget_1", "ob_budget_2"))
            ],
            matched_ids=[n["id"] for n in result["special_nodes"] + result["top_claims"]],
            not_recalled_recently=[],
        )
        partial_stats = {}
        self.hook.format_nudge(partial_result, session_id="measure", stats=partial_stats)
        budget_for_specials_and_top_claims = partial_stats["chars_rendered"]
        self.assertGreater(budget_for_specials_and_top_claims, 0)

        self._write_config({"recall_budget": {
            "total_chars": budget_for_specials_and_top_claims, "enabled": True,
        }})
        stats = {}
        nudge = self.hook.format_nudge(result, session_id="s2", stats=stats)
        for i in range(3):
            self.assertIn(f"[ax_budget_{i:04d}]", nudge)
            self.assertIn(f"[ob_budget_{i:04d}]", nudge)
        self.assertNotIn("ob_budget_nr_", nudge)
        self.assertNotIn("ob_budget_other_", nudge)
        self.assertTrue(stats["budget_exhausted"])
        self.assertGreater(stats["nodes_cut_budget"], 0)

    def test_budget_cut_candidates_not_written_to_ledger(self):
        """A node cut by the budget must NOT appear in the surfaced ledger —
        the agent never actually saw it, so its occurrence count must not
        creep toward suppression/demotion from a render it never received."""
        self._write_config({"recall_budget": {"total_chars": 5, "enabled": True}})
        result = _dense_result()
        self.hook.format_nudge(result, session_id="ledger-check", stats={})
        ledger_path = os.path.join(self.tmp, "surface-ledger.json")
        with open(ledger_path, "r", encoding="utf-8") as f:
            ledger = json.load(f)
        entries = ledger.get("ledger-check", [])
        self.assertTrue(entries)
        recorded_ids = entries[-1]["ids"]
        # Nothing rendered under a 5-char budget — the ledger entry for this
        # prompt must be empty, not full of would-have-rendered ids.
        self.assertEqual(recorded_ids, [])

    def test_nodes_cut_budget_counts_every_eligible_candidate_not_just_first(self):
        """Regression test for PR #1701 round-1 review finding: the Others
        and not-recalled-reservation loops used to `break` as soon as the
        budget was exhausted, which stopped calling `_budget_try_spend` for
        every remaining candidate — silently undercounting nodes_cut_budget
        (reproduced: 15 eligible Others candidates, budget for ~3 → reported
        only 1 cut instead of the true 12). `_budget_try_spend`'s own
        docstring documents the correct contract (every remaining eligible
        candidate must be walked, not early-broken-out-of); this test pins
        that contract against both the Others and reservation loops.
        """
        # 15 Others-eligible candidates (all with keywords, none suppressed).
        others_meta = [
            {"id": f"ob_cutcount_{i:04d}", "type": "observation", "recall_keywords": [f"kw{i}"]}
            for i in range(15)
        ]
        result = _make_result(
            matched_meta=others_meta,
            matched_ids=[m["id"] for m in others_meta],
            match_count=len(others_meta),
        )

        # Size the budget to fit only the first ~3 lines: measure one line's
        # cost, then size total_chars for 3 lines + slack.
        probe_stats = {}
        self._write_config({"recall_budget": {"total_chars": 100000, "enabled": True}})
        self.hook.format_nudge(result, session_id="probe", stats=probe_stats)
        per_node_chars = probe_stats["chars_rendered"] / len(others_meta)
        budget_for_three = int(per_node_chars * 3) + 5

        self._write_config({"recall_budget": {"total_chars": budget_for_three, "enabled": True}})
        stats = {}
        self.hook.format_nudge(result, session_id="cutcount", stats=stats)
        self.assertTrue(stats["budget_exhausted"])
        # Must count EVERY eligible candidate that didn't fit, not just the
        # first one hit after exhaustion — with ~3 shown, ~12 of the 15
        # should be counted as cut (allow the boundary node itself: shown +
        # cut must account for all 15 candidates, none silently dropped).
        self.assertEqual(stats["nodes_shown"] + stats["nodes_cut_budget"], 15)
        self.assertGreater(stats["nodes_cut_budget"], 1, "must count more than just the first post-exhaustion candidate")

    def test_reservation_nodes_cut_budget_counts_every_eligible_candidate(self):
        """Same regression as above, for the not-recalled-reservation loop
        specifically (it has its own early-break site, separate from the
        Others loop) — 10 reservable candidates competing for a 2-slot
        reservation cap under a tight budget; must count every eligible-but-
        cut candidate it walks, not just the first.

        Note: candidates in `not_recalled_recently` that AREN'T reserved
        (beyond the reserve_n<=2 cap, or cut by budget) still appear in
        `matched_meta` and so also flow into the general Others-tier pool —
        this is pre-existing #1689 behavior (reservation and Others are not
        mutually exclusive membership, only mutually exclusive per actual
        RENDER). So this test verifies the balance invariant (every
        candidate ends up counted as shown, suppressed, or cut — never
        silently dropped) rather than asserting a specific loop's count in
        isolation, since a candidate cut by the reservation loop's budget
        check can still be picked up and counted by the Others loop right
        after (both loops are correctly instrumented post-fix; only the
        overall total needs to balance).
        """
        specials = [_special_entry(node_id="ax_rescut_0000", claim="axiom 0")]
        top_claims = [_claim_entry(node_id="ob_rescut_0000", claim="claim 0")]
        reservable_ids = [f"ob_rescut_nr_{i:04d}" for i in range(10)]
        reservable_meta = [
            {"id": nid, "type": "observation", "recall_keywords": [f"nrkw{i}"]}
            for i, nid in enumerate(reservable_ids)
        ]
        matched_meta = [
            {"id": n["id"], "type": n.get("type"), "recall_keywords": n.get("recall_keywords")}
            for n in specials + top_claims
        ] + reservable_meta
        total_candidates = 2 + len(reservable_ids)  # 1 special + 1 top claim + 10 reservable
        result = _make_result(
            special_nodes=specials,
            top_claims=top_claims,
            matched_meta=matched_meta,
            matched_ids=[n["id"] for n in specials + top_claims] + reservable_ids,
            not_recalled_recently=reservable_ids,
            match_count=total_candidates,
        )

        # A budget tight enough to fit only the special (not the top claim,
        # not any reservation/Others candidate) — forces exhaustion well
        # before the reservation loop, so every one of the 10 reservable
        # candidates it walks is eligible-but-cut.
        probe_stats = {}
        self._write_config({"recall_budget": {"total_chars": 100000, "enabled": True}})
        self.hook.format_nudge(
            _make_result(special_nodes=specials, matched_meta=matched_meta[:1],
                         matched_ids=["ax_rescut_0000"], match_count=1),
            session_id="probe_special_only", stats=probe_stats,
        )
        budget_for_special_only = probe_stats["chars_rendered"]

        self._write_config({"recall_budget": {
            "total_chars": budget_for_special_only, "enabled": True,
        }})
        stats = {}
        self.hook.format_nudge(result, session_id="rescut", stats=stats)
        self.assertTrue(stats["budget_exhausted"])
        self.assertGreater(
            stats["nodes_cut_budget"], 1,
            "must count more than just the first post-exhaustion candidate across the reservation+Others loops",
        )
        # Balance invariant: nothing silently vanishes — every candidate is
        # either shown or counted as cut (none suppressed by the ledger in
        # this fresh single-render session).
        self.assertEqual(stats["nodes_shown"] + stats["nodes_cut_budget"], total_candidates)


# ---------------------------------------------------------------------------
# 4. stats out-parameter: shape + values
# ---------------------------------------------------------------------------


class TestStatsPopulated(BudgetTestBase):
    def test_stats_populated_on_zero_match_early_return(self):
        stats = {"preexisting": "must be cleared"}
        nudge = self.hook.format_nudge({"match_count": 0}, stats=stats)
        self.assertEqual(nudge, "")
        self.assertEqual(stats, {
            "budget_enabled": True,
            "budget_total_chars": self.hook.DEFAULT_RECALL_BUDGET_TOTAL_CHARS,
            "budget_exhausted": False,
            "chars_rendered": 0,
            "nodes_shown": 0,
            "nodes_suppressed": 0,
            "nodes_cut_budget": 0,
            "nodes_suppressed_echo": 0,
            "sets_checked": 0,
            "set_lines_rendered": 0,
        })

    def test_stats_none_default_is_a_pure_no_op(self):
        """Omitting stats (the default) must not raise and must not change
        rendered output — matches the pack_results/session_id optional-param
        precedent."""
        result = _make_result(top_claims=[_claim_entry(node_id="ob_stats_0001")])
        nudge = self.hook.format_nudge(result)  # stats omitted
        self.assertIn("[ob_stats_0001]", nudge)

    def test_stats_reflects_nodes_shown_and_chars_rendered(self):
        result = _make_result(top_claims=[
            _claim_entry(node_id="ob_stats_0002"),
            _claim_entry(node_id="ob_stats_0003"),
        ])
        stats = {}
        nudge = self.hook.format_nudge(result, session_id="s1", stats=stats)
        self.assertEqual(stats["nodes_shown"], 2)
        self.assertGreater(stats["chars_rendered"], 0)
        self.assertFalse(stats["budget_exhausted"])
        self.assertEqual(stats["nodes_cut_budget"], 0)

    def test_stats_nodes_suppressed_reflects_ledger_suppression(self):
        """nodes_suppressed counts the #1689 ledger-suppression mechanism —
        independent of the budget."""
        ledger_path = os.path.join(self.tmp, "surface-ledger.json")
        with open(ledger_path, "w", encoding="utf-8") as f:
            json.dump({"s1": [{"ts": 1.0, "ids": ["ob_stats_0004"]}] * 3}, f)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_stats_0004")])
        stats = {}
        self.hook.format_nudge(result, session_id="s1", stats=stats)
        self.assertEqual(stats["nodes_suppressed"], 1)

    def test_kill_switch_stats_shape(self):
        self._write_config({"recall_budget": {"total_chars": 1, "enabled": False}})
        result = _make_result(top_claims=[_claim_entry(node_id="ob_stats_0005")])
        stats = {}
        self.hook.format_nudge(result, session_id="s1", stats=stats)
        self.assertFalse(stats["budget_enabled"])
        self.assertIsNone(stats["budget_total_chars"])
        self.assertFalse(stats["budget_exhausted"])
        self.assertEqual(stats["nodes_cut_budget"], 0)


# ---------------------------------------------------------------------------
# 5. enabled=False kill switch: unlimited regardless of total_chars
# ---------------------------------------------------------------------------


class TestKillSwitch(BudgetTestBase):
    def test_tiny_total_chars_with_enabled_false_renders_everything(self):
        self._write_config({"recall_budget": {"total_chars": 1, "enabled": False}})
        result = _dense_result()
        stats = {}
        nudge = self.hook.format_nudge(result, session_id="s1", stats=stats)
        self.assertFalse(stats["budget_exhausted"])
        self.assertEqual(stats["nodes_cut_budget"], 0)
        for i in range(3):
            self.assertIn(f"[ax_budget_{i:04d}]", nudge)
            self.assertIn(f"[ob_budget_{i:04d}]", nudge)
        for i in range(5):
            self.assertIn(f"[ob_budget_other_{i:04d}]", nudge)

    def test_enabled_false_matches_pre_budget_legacy_output(self):
        """enabled=False must render byte-identical to the pure legacy call
        shape (no session_id, no stats, no budget concept at all)."""
        result = _dense_result()
        legacy = self.hook.format_nudge(result)
        self._write_config({"recall_budget": {"total_chars": 1, "enabled": False}})
        killed = self.hook.format_nudge(result, session_id="s2", stats={})
        self.assertEqual(legacy, killed)


# ---------------------------------------------------------------------------
# 6. engram.surface.render_size emission through hook.main()
# ---------------------------------------------------------------------------


class TestRenderSizeEmission(unittest.TestCase):
    """Full main() integration: verify the engram.surface.render_size event
    is actually written to the structured event log. Harness mirrors
    test_surface_hook_attached_packs.py's TestMiniFooterPointsAtEngramPkg."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_budget_emission_test_")
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp
        self.hook.COUNTER_PATH = os.path.join(self.tmp, "prompt-counter.json")
        self.hook.WRITE_REMINDER_PATH = os.path.join(self.tmp, "write-reminder.json")
        self.hook.REPAIR_MARKER_PATH = os.path.join(self.tmp, "toolcall-repair-pending.json")
        self.hook.FEELING_NUDGE_MARKER = os.path.join(self.tmp, "feeling-nudge-active.json")
        self.hook.WARM_BRIEFING_PATH = os.path.join(self.tmp, "warm-briefing.md")
        self.hook.ERROR_PATTERNS_PATH = os.path.join(self.tmp, "error_patterns.json")
        self.hook.ERROR_INCIDENTS_PATH = os.path.join(self.tmp, "error_incidents.json")
        self.hook.KNOWLEDGE_DB_PATH = os.path.join(self.tmp, "knowledge.db")
        self.hook.SOCKET_PATH = os.path.join(self.tmp, "recall-daemon.sock")

        # engram_log_emitter's Emitter singleton keys off the real
        # ENGRAM_HOME env var (not the hook module's ENGRAM_HOME attribute)
        # — redirect it too, and reset the singleton so this test doesn't
        # inherit state from an earlier test in the same process.
        self._orig_engram_home_env = os.environ.get("ENGRAM_HOME")
        os.environ["ENGRAM_HOME"] = self.tmp
        import engram_log_emitter
        self._log_emitter_module = engram_log_emitter
        engram_log_emitter._EMITTER_INSTANCE = None

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._log_emitter_module._EMITTER_INSTANCE = None
        if self._orig_engram_home_env is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = self._orig_engram_home_env

    def _run_main(self, own_graph_result, session_id="render-size-test"):
        orig_daemon = self.hook.query_daemon
        orig_fts = self.hook.query_fts_fallback
        # #1698 slice 3: main() now calls check_principle_triggers_full
        # (returns (text, rendered)) rather than check_principle_triggers
        # directly — stub the function main() actually invokes.
        orig_tripwire = self.hook.check_principle_triggers_full
        orig_patterns = self.hook.check_error_patterns
        orig_write = self.hook.check_write_reminder
        orig_repair = self.hook.check_repair_marker
        orig_feeling = self.hook.check_feeling_nudge_marker
        orig_warm = self.hook.check_warm_briefing
        orig_packs = self.hook._query_attached_packs

        self.hook.query_daemon = lambda *a, **kw: own_graph_result
        self.hook.query_fts_fallback = lambda *a, **kw: own_graph_result
        self.hook.check_principle_triggers_full = lambda *a, **kw: ("", [])
        self.hook.check_error_patterns = lambda *a, **kw: ""
        self.hook.check_write_reminder = lambda *a, **kw: ""
        self.hook.check_repair_marker = lambda *a, **kw: ""
        self.hook.check_feeling_nudge_marker = lambda *a, **kw: ""
        self.hook.check_warm_briefing = lambda *a, **kw: ""
        self.hook._query_attached_packs = lambda *a, **kw: []

        hook_input = json.dumps({
            "prompt": "epistemology knowledge memory",
            "session_id": session_id,
            "transcript_path": os.path.join(self.tmp, "t.jsonl"),
        })

        captured_output = io.StringIO()
        orig_stdout = sys.stdout
        orig_stdin = sys.stdin
        orig_context_tracker = sys.modules.get("context_tracker")
        sys.modules["context_tracker"] = types.SimpleNamespace(
            estimate_usage=lambda *a, **kw: None,
            format_drowsiness=lambda *a, **kw: "",
        )
        try:
            sys.stdin = io.StringIO(hook_input)
            sys.stdout = captured_output
            try:
                self.hook.main()
            except SystemExit:
                pass
        finally:
            sys.stdout = orig_stdout
            sys.stdin = orig_stdin
            self.hook.query_daemon = orig_daemon
            self.hook.query_fts_fallback = orig_fts
            self.hook.check_principle_triggers_full = orig_tripwire
            self.hook.check_error_patterns = orig_patterns
            self.hook.check_write_reminder = orig_write
            self.hook.check_repair_marker = orig_repair
            self.hook.check_feeling_nudge_marker = orig_feeling
            self.hook.check_warm_briefing = orig_warm
            self.hook._query_attached_packs = orig_packs
            if orig_context_tracker is not None:
                sys.modules["context_tracker"] = orig_context_tracker
            else:
                sys.modules.pop("context_tracker", None)

        return captured_output.getvalue()

    def _read_events(self, session_id: str, level: int = 1) -> list[dict]:
        log_path = os.path.join(self.tmp, "logs", "sessions", f"{session_id}.l{level}.jsonl")
        if not os.path.exists(log_path):
            return []
        events = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def test_render_size_event_emitted_on_normal_render(self):
        result = _make_result(top_claims=[_claim_entry(node_id="ob_emit_0001")])
        self._run_main(result, session_id="render-size-normal")
        events = self._read_events("render-size-normal")
        render_size_events = [e for e in events if e.get("event_type") == "engram.surface.render_size"]
        self.assertEqual(len(render_size_events), 1)
        data = render_size_events[0]["data"]
        self.assertIn("budget_enabled", data)
        self.assertIn("budget_total_chars", data)
        self.assertIn("budget_exhausted", data)
        self.assertIn("chars_rendered", data)
        self.assertIn("nodes_shown", data)
        self.assertIn("nodes_suppressed", data)
        self.assertIn("nodes_cut_budget", data)
        self.assertEqual(data["nodes_shown"], 1)
        self.assertFalse(data["budget_exhausted"])

    def test_render_size_event_emitted_even_on_zero_match(self):
        """match_count=0 still runs format_nudge (with stats) inside main(),
        so a render_size event fires even though the nudge text is empty."""
        result = {"match_count": 0, "age": {}, "issues": {}}
        self._run_main(result, session_id="render-size-empty")
        events = self._read_events("render-size-empty")
        render_size_events = [e for e in events if e.get("event_type") == "engram.surface.render_size"]
        self.assertEqual(len(render_size_events), 1)
        self.assertEqual(render_size_events[0]["data"]["nodes_shown"], 0)

    def test_render_size_event_reflects_budget_cuts(self):
        with open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"recall_budget": {"total_chars": 5, "enabled": True}}, f)
        result = _dense_result()
        self._run_main(result, session_id="render-size-cut")
        events = self._read_events("render-size-cut")
        render_size_events = [e for e in events if e.get("event_type") == "engram.surface.render_size"]
        self.assertEqual(len(render_size_events), 1)
        data = render_size_events[0]["data"]
        self.assertTrue(data["budget_exhausted"])
        self.assertGreater(data["nodes_cut_budget"], 0)


if __name__ == "__main__":
    unittest.main()
