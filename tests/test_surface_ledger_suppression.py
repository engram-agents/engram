"""Tests for surfaced-ledger render-layer suppression + not-recalled slot
reservation in engram-surface-hook.py (recall-triggering blueprint §3-P1,
issue #1689).

Follows the sibling patterns already established in this test suite:
  - test_auto_surface_prepending.py / test_surface_hook_attached_packs.py for
    the hyphenated-filename module-loading helper and format_nudge unit-test
    style (constructed result dicts, no daemon/DB integration).
  - test_recall_summary_substrate.py for the `_make_result` / `_claim_entry` /
    `_special_entry` fixture-builder pattern.

Coverage (per the issue spec):
  1. 0-1x-in-k renders full-tier (baseline, no regression).
  2. 2x-in-k demotes to Others-tier even for a would-be special/top_claim.
  3. >=m-in-k suppresses entirely (not rendered anywhere).
  4. Suppressed-count line: present iff suppression happened this render.
  5. Ledger persists across simulated prompts; demotion kicks in on repeat.
  6. Ledger retention/pruning actually prunes.
  7. Tripwire output is never suppressed (separate channel; confirmed here by
     the tripwire's own matched_ids never touching the surfaced ledger).
  8. Not-recalled slot reservation fills a freed slot.
  9. Synthetic repeat-fraction-at-k check using the SAME definition as
     tools/recall_repetition_analysis.py's repeat_fraction_at_k.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename."""
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_surface_hook_ledger_test", str(hook_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixture builders (mirrors test_recall_summary_substrate.py's style)
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


class LedgerTestBase(unittest.TestCase):
    """Common setUp/tearDown: isolated ENGRAM_HOME per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_ledger_test_")
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger_path(self):
        return os.path.join(self.tmp, "surface-ledger.json")

    def _seed_ledger(self, session_id: str, entries: list[list[str]]):
        """Write `len(entries)` ledger entries for session_id, each with the
        given id list, at successive fake timestamps (oldest first)."""
        ledger = {}
        base = time.time() - 10 * len(entries)
        ledger[session_id] = [
            {"ts": base + i, "ids": ids} for i, ids in enumerate(entries)
        ]
        with open(self._ledger_path(), "w", encoding="utf-8") as f:
            json.dump(ledger, f)


# ---------------------------------------------------------------------------
# 1. Baseline: 0-1x in k → full-tier (no regression)
# ---------------------------------------------------------------------------


class TestBaselineFullTier(LedgerTestBase):
    def test_never_surfaced_renders_full_tier(self):
        """A node with zero ledger history renders full-tier as today."""
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0001", claim="fresh claim")])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("[ob_ledger_0001]", nudge)
        self.assertIn("Top claims:", nudge)
        self.assertNotIn("recently shown", nudge)

    def test_surfaced_once_still_renders_full_tier(self):
        """A node surfaced exactly once in the trailing k window still
        renders full-tier (0 or 1 prior occurrence → full, per the spec)."""
        self._seed_ledger("s1", [["ob_ledger_0001"]])
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0001", claim="fresh claim")])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("[ob_ledger_0001]", nudge)
        self.assertIn("Top claims:", nudge)
        self.assertNotIn("recently shown", nudge)

    def test_session_id_none_disables_suppression_entirely(self):
        """The legacy call shape (session_id omitted) never suppresses,
        regardless of ledger content — old callers stay byte-identical."""
        self._seed_ledger("s1", [["ob_ledger_0001"]] * 10)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0001", claim="fresh claim")])
        nudge = self.hook.format_nudge(result)  # no session_id
        self.assertIn("[ob_ledger_0001]", nudge)
        self.assertNotIn("recently shown", nudge)


# ---------------------------------------------------------------------------
# 2. 2x in k → demote to Others-tier, even for a would-be special/top_claim
# ---------------------------------------------------------------------------


class TestDemotionToOthers(LedgerTestBase):
    def test_top_claim_surfaced_twice_demotes_to_others(self):
        self._seed_ledger("s1", [["ob_ledger_0002"], ["ob_ledger_0002"]])
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0002", claim="repeat claim")])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("Top claims:", nudge)
        self.assertIn("Others:", nudge)
        self.assertIn("[ob_ledger_0002]", nudge)

    def test_special_surfaced_twice_demotes_to_others(self):
        """Specials follow the same demotion rule as top_claims (§3-P1)."""
        self._seed_ledger("s1", [["ax_ledger_0001"], ["ax_ledger_0001"]])
        result = _make_result(special_nodes=[_special_entry(node_id="ax_ledger_0001", claim="axiom claim")])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("Specials:", nudge)
        self.assertIn("Others:", nudge)
        self.assertIn("[ax_ledger_0001]", nudge)

    def test_others_candidate_surfaced_twice_stays_at_others_tier(self):
        """A matched_meta-only candidate (already Others-tier) at count=2 is
        unaffected in appearance — it was already at the bottom rung short of
        suppression."""
        self._seed_ledger("s1", [["ob_ledger_0003"], ["ob_ledger_0003"]])
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_ledger_9999", claim="other")],
            matched_meta=[
                {"id": "ob_ledger_9999", "type": "observation", "recall_keywords": ["kw"]},
                {"id": "ob_ledger_0003", "type": "observation", "recall_keywords": ["repeat-kw"]},
            ],
            matched_ids=["ob_ledger_9999", "ob_ledger_0003"],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("[ob_ledger_0003]", nudge)
        self.assertIn("`repeat-kw`", nudge)


# ---------------------------------------------------------------------------
# 3. >= m in k → suppressed entirely (not rendered anywhere)
# ---------------------------------------------------------------------------


class TestFullSuppression(LedgerTestBase):
    def test_top_claim_surfaced_three_times_suppressed_entirely(self):
        """Default m=3: three prior occurrences → suppressed, not even Others."""
        self._seed_ledger("s1", [["ob_ledger_0004"]] * 3)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0004", claim="triple repeat")])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("ob_ledger_0004", nudge)

    def test_others_candidate_surfaced_three_times_suppressed_from_others(self):
        """A pure Others-tier candidate hitting the suppress threshold is
        dropped from the Others list too — matched_meta candidates follow the
        same rule as specials/top_claims."""
        self._seed_ledger("s1", [["ob_ledger_0005"]] * 3)
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_ledger_9998", claim="other")],
            matched_meta=[
                {"id": "ob_ledger_9998", "type": "observation", "recall_keywords": ["kw"]},
                {"id": "ob_ledger_0005", "type": "observation", "recall_keywords": ["gone-kw"]},
            ],
            matched_ids=["ob_ledger_9998", "ob_ledger_0005"],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("ob_ledger_0005", nudge)
        self.assertNotIn("gone-kw", nudge)

    def test_custom_m_threshold_respected(self):
        """A config.json recall_suppression.m override changes the threshold."""
        with open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"recall_suppression": {"m": 2}}, f)
        self._seed_ledger("s1", [["ob_ledger_0006"]] * 2)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0006", claim="repeat")])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("ob_ledger_0006", nudge)


# ---------------------------------------------------------------------------
# 4. Suppressed-count line — present iff suppression happened this render
# ---------------------------------------------------------------------------


class TestSuppressedCountLine(LedgerTestBase):
    def test_line_absent_when_nothing_suppressed(self):
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0007", claim="fresh")])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("recently shown", nudge)

    def test_line_present_when_something_suppressed(self):
        self._seed_ledger("s1", [["ob_ledger_0008"]] * 3)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0008", claim="repeat")])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("recently shown", nudge)
        self.assertIn("+1 recently shown", nudge)

    def test_line_counts_distinct_suppressed_ids(self):
        self._seed_ledger("s1", [["ob_ledger_0009", "ob_ledger_0010"]] * 3)
        result = _make_result(top_claims=[
            _claim_entry(node_id="ob_ledger_0009", claim="repeat A"),
            _claim_entry(node_id="ob_ledger_0010", claim="repeat B"),
        ])
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("+2 recently shown", nudge)


# ---------------------------------------------------------------------------
# 5. Ledger persists across simulated prompts; demotion kicks in on repeat
# ---------------------------------------------------------------------------


class TestLedgerPersistenceAcrossPrompts(LedgerTestBase):
    def test_repeated_calls_demote_then_suppress(self):
        """Simulate a session: same node surfaced on 4 successive prompts.
        Prompt 1: full. Prompt 2: full (1 prior). Prompt 3: others (2 prior).
        Prompt 4: suppressed (3 prior, default m=3)."""
        node_id = "ob_ledger_0011"

        def _render():
            result = _make_result(top_claims=[_claim_entry(node_id=node_id, claim="persistent claim")])
            return self.hook.format_nudge(result, session_id="persist-session")

        n1 = _render()
        self.assertIn(f"[{node_id}]", n1)
        self.assertIn("Top claims:", n1)

        n2 = _render()
        self.assertIn(f"[{node_id}]", n2)
        self.assertIn("Top claims:", n2)

        n3 = _render()
        self.assertNotIn("Top claims:", n3)
        self.assertIn("Others:", n3)
        self.assertIn(f"[{node_id}]", n3)

        n4 = _render()
        self.assertNotIn(node_id, n4)

        # Ledger file must actually have accumulated 4 entries for the session.
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        self.assertIn("persist-session", ledger)
        self.assertEqual(len(ledger["persist-session"]), 4)

    def test_suppressed_render_not_recorded_in_ledger(self):
        """A suppressed id must not be appended to the ledger entry for the
        render where it was suppressed (only actually-rendered ids count)."""
        node_id = "ob_ledger_0012"
        self._seed_ledger("s1", [[node_id]] * 3)
        result = _make_result(top_claims=[_claim_entry(node_id=node_id, claim="repeat")])
        self.hook.format_nudge(result, session_id="s1")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        last_entry = ledger["s1"][-1]
        self.assertNotIn(node_id, last_entry["ids"])

    def test_others_beyond_render_slice_not_recorded_in_ledger(self):
        """The ledger write must match the render slice (Others[:15]) exactly.

        Kepler's #1697 colleague-review catch: the ledger write recorded the
        full `others_with_kw` set unsliced while the render only shows the
        first 15 -- so ids 16+ were marked "seen" without ever being
        rendered, silently inflating their occurrence count toward
        demotion/suppression before the agent ever saw them once. The
        mirror-image bug of the repetition this PR exists to fix.
        """
        # 20 Others-tier candidates, all with keywords so none get filtered
        # by the no-keywords-is-noise rule -- more than the 15-line render cap.
        matched_meta = [
            {"id": f"ob_ledger_others_{i:02d}", "type": "observation_factual", "recall_keywords": ["kw"]}
            for i in range(20)
        ]
        result = _make_result(
            matched_meta=matched_meta,
            matched_ids=[m["id"] for m in matched_meta],
            match_count=len(matched_meta),
        )
        nudge = self.hook.format_nudge(result, session_id="s1")

        rendered_ids = {m["id"] for m in matched_meta if m["id"] in nudge}
        self.assertEqual(len(rendered_ids), 15, "render slice should be exactly 15")

        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        ledger_ids = set(ledger["s1"][-1]["ids"])

        # The ledger must record exactly what was rendered -- no more.
        self.assertEqual(ledger_ids, rendered_ids)
        for m in matched_meta:
            if m["id"] not in rendered_ids:
                self.assertNotIn(
                    m["id"], ledger_ids,
                    f"{m['id']} was never rendered but got recorded as seen",
                )


# ---------------------------------------------------------------------------
# 6. Ledger retention/pruning actually prunes
# ---------------------------------------------------------------------------


class TestLedgerRetentionPruning(LedgerTestBase):
    def test_entry_count_capped_at_retention_window(self):
        """Appending far more entries than the retention cap must not let the
        per-session entry list grow unbounded."""
        # Seed 100 pre-existing entries, well past any retention cap.
        self._seed_ledger("s1", [["x"]] * 100)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0013", claim="cap test")])
        self.hook.format_nudge(result, session_id="s1")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        # Cap is max(4*k, DEFAULT_LEDGER_RETENTION_ENTRIES) = max(20, 20) = 20 by default.
        self.assertLessEqual(len(ledger["s1"]), 20)

    def test_stale_entries_pruned_by_age(self):
        """Entries older than DEFAULT_LEDGER_ENTRY_MAX_AGE_HOURS are dropped."""
        old_ts = time.time() - (self.hook.DEFAULT_LEDGER_ENTRY_MAX_AGE_HOURS + 1) * 3600
        ledger = {"s1": [{"ts": old_ts, "ids": ["stale_id"]}]}
        with open(self._ledger_path(), "w", encoding="utf-8") as f:
            json.dump(ledger, f)
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0014", claim="fresh append")])
        self.hook.format_nudge(result, session_id="s1")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger_after = json.load(f)
        recorded_ids = [ids for e in ledger_after["s1"] for ids in e["ids"]]
        self.assertNotIn("stale_id", recorded_ids)
        self.assertIn("ob_ledger_0014", recorded_ids)

    def test_inactive_session_dropped_entirely(self):
        """A session with no activity in DEFAULT_LEDGER_SESSION_MAX_AGE_HOURS
        is dropped from the ledger entirely on the next prune."""
        old_ts = time.time() - (self.hook.DEFAULT_LEDGER_SESSION_MAX_AGE_HOURS + 1) * 3600
        ledger = {
            "inactive-session": [{"ts": old_ts, "ids": ["old_id"]}],
        }
        with open(self._ledger_path(), "w", encoding="utf-8") as f:
            json.dump(ledger, f)
        # Trigger a prune via a render for a DIFFERENT (active) session.
        result = _make_result(top_claims=[_claim_entry(node_id="ob_ledger_0015", claim="active session")])
        self.hook.format_nudge(result, session_id="active-session")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger_after = json.load(f)
        self.assertNotIn("inactive-session", ledger_after)
        self.assertIn("active-session", ledger_after)

    def test_prune_function_direct(self):
        """Direct unit test of _prune_surface_ledger's two independent bounds."""
        now = time.time()
        ledger = {
            "recent-many-entries": [{"ts": now - i, "ids": [f"n{i}"]} for i in range(30)],
            "old-session": [{"ts": now - 100 * 3600, "ids": ["ancient"]}],
        }
        pruned = self.hook._prune_surface_ledger(
            ledger, now_ts=now, retention_entries=5,
            entry_max_age_hours=48, session_max_age_hours=48,
        )
        self.assertIn("recent-many-entries", pruned)
        self.assertLessEqual(len(pruned["recent-many-entries"]), 5)
        self.assertNotIn("old-session", pruned)


# ---------------------------------------------------------------------------
# 7. Tripwire output is never suppressed
# ---------------------------------------------------------------------------


class TestTripwireExemptFromSuppression(LedgerTestBase):
    def test_tripwire_ignores_ledger_state(self):
        """check_principle_triggers (lesson kind; #1698 slice 2 renamed this
        from check_incident_tripwire) has no session_id parameter and
        doesn't consult the surfaced ledger at all — it fires purely from
        principle_triggers.json regardless of surfaced-ledger repeat
        count."""
        registry_path = os.path.join(self.tmp, "principle_triggers.json")
        with open(registry_path, "w", encoding="utf-8") as f:
            json.dump({
                "ob_ledger_tripwire": {
                    "principle_id": "ls_0001",
                    "kind": "lesson",
                    "claim": "test lesson",
                    "nudge": "do the corrective action",
                }
            }, f)
        self.hook.PRINCIPLE_TRIGGERS_PATH = registry_path

        # Seed the surfaced ledger as if this id had already been suppressed
        # 10 times over — the tripwire must still fire every time.
        self._seed_ledger("s1", [["ob_ledger_tripwire"]] * 10)

        # Two calls separated by the cross-kind cooldown window: unification
        # (#1698 slice 2) gave lessons a real cross-prompt cooldown they
        # never had before, so two IDENTICAL prompt_counts would now be
        # suppressed by cooldown -- a different mechanism than the one this
        # test targets. Separating by the window isolates ledger-exemption
        # (this test's subject) from cooldown-suppression (a real, unrelated
        # behavior change).
        window = self.hook.PRINCIPLE_TRIGGER_COOLDOWN_PROMPTS
        result1 = self.hook.check_principle_triggers(["ob_ledger_tripwire"], prompt_count=1)
        result2 = self.hook.check_principle_triggers(["ob_ledger_tripwire"], prompt_count=1 + window)
        self.assertIn("ENGRAM Tripwire", result1)
        self.assertIn("ENGRAM Tripwire", result2)
        self.assertEqual(result1, result2)


# ---------------------------------------------------------------------------
# 8. Not-recalled slot reservation
# ---------------------------------------------------------------------------


class TestNotRecalledSlotReservation(LedgerTestBase):
    def test_freed_slot_filled_by_not_recalled_candidate(self):
        """Only 1 top_claim naturally qualifies (2 slots free); a
        not_recalled_recently id with keyword metadata fills a freed slot."""
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_ledger_0016", claim="the only top claim")],
            matched_meta=[
                {"id": "ob_ledger_0016", "type": "observation", "recall_keywords": ["kw"]},
                {"id": "ob_ledger_stale_0001", "type": "observation", "recall_keywords": ["stale-kw", "old"]},
            ],
            matched_ids=["ob_ledger_0016", "ob_ledger_stale_0001"],
            not_recalled_recently=["ob_ledger_stale_0001"],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("Worth revisiting (not recalled recently):", nudge)
        self.assertIn("[ob_ledger_stale_0001]", nudge)
        self.assertIn("`stale-kw`", nudge)

    def test_reservation_inactive_without_session_id(self):
        """The legacy call shape (no session_id) never triggers reservation —
        matches the zero-suppression byte-identical invariant."""
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_ledger_0017", claim="the only top claim")],
            matched_meta=[
                {"id": "ob_ledger_0017", "type": "observation", "recall_keywords": ["kw"]},
                {"id": "ob_ledger_stale_0002", "type": "observation", "recall_keywords": ["stale-kw"]},
            ],
            matched_ids=["ob_ledger_0017", "ob_ledger_stale_0002"],
            not_recalled_recently=["ob_ledger_stale_0002"],
        )
        nudge = self.hook.format_nudge(result)  # no session_id
        self.assertNotIn("Worth revisiting", nudge)

    def test_reservation_skips_candidate_without_metadata(self):
        """A not_recalled_recently id absent from matched_meta is skipped
        (the documented (a) choice: no metadata → skip, don't invent fields)."""
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_ledger_0018", claim="the only top claim")],
            matched_meta=[
                {"id": "ob_ledger_0018", "type": "observation", "recall_keywords": ["kw"]},
            ],
            matched_ids=["ob_ledger_0018", "ob_ledger_ghost_0001"],
            not_recalled_recently=["ob_ledger_ghost_0001"],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("Worth revisiting", nudge)
        self.assertNotIn("ob_ledger_ghost_0001", nudge)

    def test_reservation_respects_two_slot_cap(self):
        """At most 2 not-recalled candidates are reserved, even with more
        freed slots and more eligible candidates available."""
        not_recalled_ids = [f"ob_ledger_nr_{i:04d}" for i in range(5)]
        matched_meta = [
            {"id": nid, "type": "observation", "recall_keywords": [f"kw{i}"]}
            for i, nid in enumerate(not_recalled_ids)
        ]
        result = _make_result(
            top_claims=[],  # 0 top_claims → 3 freed top_claim slots + 3 special slots
            match_count=len(not_recalled_ids),
            matched_meta=matched_meta,
            matched_ids=not_recalled_ids,
            not_recalled_recently=not_recalled_ids,
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        reserved_count = nudge.count("ob_ledger_nr_")
        # Each reserved line references its id once in the "Worth revisiting"
        # block; the rest fall through to the generic Others section (still
        # capped separately), so count only the reservation block lines.
        worth_revisiting_block = nudge.split("Worth revisiting (not recalled recently):")[1]
        worth_revisiting_block = worth_revisiting_block.split("\n\n")[0]
        # Count lines starting with "    - " immediately following the header
        # up to the next section header (a line starting with "  " but not "    -").
        block_lines = []
        for line in worth_revisiting_block.split("\n")[1:]:
            if line.startswith("    - "):
                block_lines.append(line)
            else:
                break
        self.assertLessEqual(len(block_lines), 2)


# ---------------------------------------------------------------------------
# 9. Synthetic repeat-fraction-at-k check — same definition as
#    tools/recall_repetition_analysis.py's repeat_fraction_at_k
# ---------------------------------------------------------------------------


class TestSyntheticRepeatFractionImproves(LedgerTestBase):
    """Builds a synthetic prompt sequence with a known repeat pattern,
    renders it through format_nudge across 10 simulated prompts, and confirms
    the measured trailing-k repeat fraction (same formula as
    tools/recall_repetition_analysis.py::repeat_fraction_at_k) is lower after
    suppression than the raw match-side repeat fraction would have been.
    """

    @staticmethod
    def _repeat_fraction_at_k(sequences: list[list[str]], k: int = 5) -> float:
        """Mirrors tools/recall_repetition_analysis.py's repeat_fraction_at_k
        pooled computation (applied here to a single synthetic session's
        prompt sequence): total repeat-id-instances / total id-instances,
        where an id-instance at prompt i is a "repeat" iff it also appears in
        the union of ids from the trailing k prior prompts."""
        total_ids = 0
        total_repeats = 0
        for i, ids in enumerate(sequences):
            if not ids:
                continue
            window_ids = set()
            for j in range(max(0, i - k), i):
                window_ids.update(sequences[j])
            for nid in ids:
                total_ids += 1
                if nid in window_ids:
                    total_repeats += 1
        return (total_repeats / total_ids) if total_ids else 0.0

    def test_rendered_repeat_fraction_lower_than_raw_match_fraction(self):
        """A hot node matches on every one of 10 prompts (the raw-match
        sequence would show a high repeat fraction). Render through
        format_nudge with suppression active; the RENDERED id sequence's
        repeat fraction must be lower than the raw-matched sequence's."""
        hot_id = "ob_ledger_hot_0001"
        cold_ids = [f"ob_ledger_cold_{i:04d}" for i in range(10)]

        raw_matched_sequence = []
        rendered_sequence = []

        for i in range(10):
            cold_id = cold_ids[i]
            result = _make_result(
                top_claims=[
                    _claim_entry(node_id=hot_id, claim="hot repeating claim"),
                    _claim_entry(node_id=cold_id, claim=f"cold claim {i}"),
                ],
            )
            raw_matched_sequence.append([hot_id, cold_id])
            nudge = self.hook.format_nudge(result, session_id="repeat-frac-session")
            rendered_ids = []
            if f"[{hot_id}]" in nudge:
                rendered_ids.append(hot_id)
            if f"[{cold_id}]" in nudge:
                rendered_ids.append(cold_id)
            rendered_sequence.append(rendered_ids)

        raw_fraction = self._repeat_fraction_at_k(raw_matched_sequence, k=5)
        rendered_fraction = self._repeat_fraction_at_k(rendered_sequence, k=5)

        self.assertGreater(raw_fraction, 0.0, "Sanity: raw match sequence has a nonzero repeat fraction")
        self.assertLess(
            rendered_fraction, raw_fraction,
            f"Rendered repeat fraction ({rendered_fraction}) must be lower than "
            f"raw matched repeat fraction ({raw_fraction}) once suppression kicks in",
        )


if __name__ == "__main__":
    unittest.main()
