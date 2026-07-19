"""Tests for #257 recs 1+2 (forum thread #266, issue #1778):

  1. Prompt-embedding-proximity habituation decay, extending #1700's
     decay-not-suppress policy to the surfaced-ledger render suppression
     (recall-triggering blueprint §3-P1 / issue #1689). Replaces the
     trailing-k occurrence-COUNT-only classification with a comparison
     against the embedding stored at a node's most recent render, once
     count alone would demote/suppress it.
  2. Same-session-echo guard: never re-render a node created in the current
     session within N minutes of its creation.

Follows the sibling pattern established in test_surface_ledger_suppression.py
(hyphenated-filename module loader, local fixture builders, isolated
ENGRAM_HOME per test).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _load_hook_module():
    """Import the surface hook as a module despite the hyphenated filename."""
    hook_path = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"
    spec = importlib.util.spec_from_file_location("engram_recall_decay_echo_test", str(hook_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim_entry(*, node_id, claim="a claim", confidence=0.85,
                  created_at=None, created_minutes_ago=60.0,
                  recall_summary=None, recall_keywords=None,
                  ntype="observation_factual"):
    return {
        "id": node_id,
        "type": ntype,
        "claim": claim,
        "confidence": confidence,
        "created_at": created_at,
        "created_ago": "1h ago",
        "created_minutes_ago": created_minutes_ago,
        "recall_summary": recall_summary,
        "recall_keywords": recall_keywords or ["kw"],
    }


def _special_entry(*, node_id, claim="an axiom", ntype="axiom", confidence=0.9,
                    created_at=None, created_minutes_ago=2880.0,
                    recall_summary=None, recall_keywords=None):
    return {
        "id": node_id,
        "type": ntype,
        "claim": claim,
        "confidence": confidence,
        "created_at": created_at,
        "created_ago": "2d ago",
        "created_minutes_ago": created_minutes_ago,
        "recall_summary": recall_summary,
        "recall_keywords": recall_keywords or ["kw"],
    }


def _make_result(*, special_nodes=None, top_claims=None, match_count=None,
                  matched_ids=None, matched_meta=None, not_recalled_recently=None,
                  query_embedding=None):
    sn = special_nodes or []
    tc = top_claims or []
    mc = match_count if match_count is not None else len(sn) + len(tc)
    mids = matched_ids or [n["id"] for n in sn + tc]
    mm = matched_meta if matched_meta is not None else [
        {"id": n["id"], "type": n.get("type"), "recall_keywords": n.get("recall_keywords")}
        for n in sn + tc
    ]
    result = {
        "match_count": mc,
        "special_nodes": sn,
        "top_claims": tc,
        "type_counts": {},
        "age": {"not_recalled_recently": not_recalled_recently or []},
        "issues": {},
        "matched_ids": mids,
        "matched_meta": mm,
    }
    if query_embedding is not None:
        result["query_embedding"] = query_embedding
    return result


class DecayEchoTestBase(unittest.TestCase):
    """Common setUp/tearDown: isolated ENGRAM_HOME per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_decay_echo_test_")
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger_path(self):
        return os.path.join(self.tmp, "surface-ledger.json")

    def _seed_ledger(self, session_id: str, entries: list):
        """Write ledger entries for session_id at successive fake
        timestamps (oldest first). Each entry in `entries` is either a
        plain id-list (legacy shape) or a dict {"ids": [...],
        "prompt_embedding": [...]}."""
        ledger = {}
        base = time.time() - 10 * len(entries)
        rows = []
        for i, e in enumerate(entries):
            if isinstance(e, dict):
                row = {"ts": base + i, "ids": e["ids"]}
                if "prompt_embedding" in e:
                    row["prompt_embedding"] = e["prompt_embedding"]
            else:
                row = {"ts": base + i, "ids": e}
            rows.append(row)
        ledger[session_id] = rows
        with open(self._ledger_path(), "w", encoding="utf-8") as f:
            json.dump(ledger, f)

    def _write_session_marker(self, session_id: str, started_at: datetime):
        sessions_dir = os.path.join(self.tmp, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        with open(os.path.join(sessions_dir, f"{session_id}.json"), "w", encoding="utf-8") as f:
            json.dump({"session_id": session_id, "started_at": _iso(started_at)}, f)

    def _config(self, section: str, values: dict):
        path = os.path.join(self.tmp, "config.json")
        cfg = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        cfg[section] = values
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)


# ---------------------------------------------------------------------------
# Cosine similarity + decay-tier unit-level checks (pure functions, no hook)
# ---------------------------------------------------------------------------


class TestCosineSimilarityHelper(DecayEchoTestBase):
    def test_identical_vectors_similarity_one(self):
        self.assertAlmostEqual(self.hook._cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]), 1.0)

    def test_orthogonal_vectors_similarity_zero(self):
        self.assertAlmostEqual(self.hook._cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_mismatched_length_returns_zero(self):
        self.assertEqual(self.hook._cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]), 0.0)

    def test_empty_vector_returns_zero(self):
        self.assertEqual(self.hook._cosine_similarity([], [1.0]), 0.0)


class TestDecayTierForDirect(unittest.TestCase):
    """Direct unit tests of _decay_tier_for, isolated from the render loop."""

    def setUp(self):
        self.hook = _load_hook_module()

    def test_count_le_1_is_full_no_decay_event(self):
        tier, event = self.hook._decay_tier_for(
            "ob_x", 1, 3, [1.0, 0.0], [], 0.75,
        )
        self.assertEqual(tier, "full")
        self.assertIsNone(event)

    def test_high_similarity_decays_to_others_never_suppress(self):
        window = [{"ts": 1, "ids": ["ob_x"], "prompt_embedding": [1.0, 0.0]}]
        # count would classify as "suppress" under the old count-only rule
        # (>=m), but decay floors it at "others".
        tier, event = self.hook._decay_tier_for(
            "ob_x", 5, 3, [1.0, 0.0], window, 0.75,
        )
        self.assertEqual(tier, "others")
        self.assertEqual(event["id"], "ob_x")
        self.assertEqual(event["tier"], "others")
        self.assertAlmostEqual(event["cosine"], 1.0)

    def test_low_similarity_renders_full_despite_high_count(self):
        window = [{"ts": 1, "ids": ["ob_x"], "prompt_embedding": [1.0, 0.0]}]
        tier, event = self.hook._decay_tier_for(
            "ob_x", 5, 3, [0.0, 1.0], window, 0.75,
        )
        self.assertEqual(tier, "full")
        self.assertEqual(event["tier"], "full")
        self.assertAlmostEqual(event["cosine"], 0.0)

    def test_missing_query_embedding_falls_back_to_count_only(self):
        window = [{"ts": 1, "ids": ["ob_x"], "prompt_embedding": [1.0, 0.0]}]
        tier, event = self.hook._decay_tier_for(
            "ob_x", 5, 3, None, window, 0.75,
        )
        self.assertEqual(tier, "suppress")  # today's count-only classification
        self.assertEqual(event["cosine"], None)

    def test_missing_stored_embedding_falls_back_to_count_only(self):
        window = [{"ts": 1, "ids": ["ob_x"]}]  # no prompt_embedding field
        tier, event = self.hook._decay_tier_for(
            "ob_x", 2, 3, [1.0, 0.0], window, 0.75,
        )
        self.assertEqual(tier, "others")  # count-only: 2 < m=3 -> others
        self.assertEqual(event["cosine"], None)


# ---------------------------------------------------------------------------
# End-to-end via format_nudge: rec 1 wired into the render loop
# ---------------------------------------------------------------------------


class TestDecayEndToEnd(DecayEchoTestBase):
    def test_high_similarity_demotes_not_suppresses(self):
        """A node surfaced enough times to hit the old 'suppress' tier
        renders at Others instead when this prompt closely matches the
        prompt that last rendered it."""
        self._seed_ledger("s1", [
            {"ids": ["ob_decay_0001"], "prompt_embedding": [1.0, 0.0]},
            {"ids": ["ob_decay_0001"], "prompt_embedding": [1.0, 0.0]},
            {"ids": ["ob_decay_0001"], "prompt_embedding": [1.0, 0.0]},
        ])
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_decay_0001", claim="repeat claim")],
            query_embedding=[1.0, 0.0],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("suppress", nudge.lower())
        self.assertNotIn("Top claims:", nudge)
        self.assertIn("Others:", nudge)
        self.assertIn("[ob_decay_0001]", nudge)

    def test_low_similarity_renders_full_despite_high_count(self):
        """Same repeat history, but this prompt has moved on (orthogonal
        embedding) — renders full, the literal Ariadne's-6th-fire fix."""
        self._seed_ledger("s1", [
            {"ids": ["ob_decay_0002"], "prompt_embedding": [1.0, 0.0]},
            {"ids": ["ob_decay_0002"], "prompt_embedding": [1.0, 0.0]},
            {"ids": ["ob_decay_0002"], "prompt_embedding": [1.0, 0.0]},
        ])
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_decay_0002", claim="repeat claim")],
            query_embedding=[0.0, 1.0],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("Top claims:", nudge)
        self.assertIn("[ob_decay_0002]", nudge)

    def test_missing_query_embedding_falls_back_to_count_classification(self):
        """No query_embedding in the result (e.g. FTS-only fallback path) —
        behaves exactly like the pre-#266 count-only suppression."""
        self._seed_ledger("s1", [["ob_decay_0003"], ["ob_decay_0003"], ["ob_decay_0003"]])
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_decay_0003", claim="repeat claim")],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("[ob_decay_0003]", nudge)  # suppressed entirely, as before #266

    def test_decay_events_recorded_in_ledger(self):
        """The decay classifier's firing (tier + cosine) is written to the
        ledger as a distinct event, per Kepler's #266 review — not silently
        applied."""
        self._seed_ledger("s1", [
            {"ids": ["ob_decay_0004"], "prompt_embedding": [1.0, 0.0]},
            {"ids": ["ob_decay_0004"], "prompt_embedding": [1.0, 0.0]},
        ])
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_decay_0004", claim="repeat claim")],
            query_embedding=[1.0, 0.0],
        )
        self.hook.format_nudge(result, session_id="s1")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        last_entry = ledger["s1"][-1]
        self.assertIn("decay_events", last_entry)
        events = [e for e in last_entry["decay_events"] if e["id"] == "ob_decay_0004"]
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["cosine"], 1.0)
        self.assertEqual(events[0]["tier"], "others")

    def test_prompt_embedding_persisted_in_ledger_entry(self):
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_decay_0005", claim="fresh claim")],
            query_embedding=[0.5, 0.5],
        )
        self.hook.format_nudge(result, session_id="s1")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        self.assertEqual(ledger["s1"][-1]["prompt_embedding"], [0.5, 0.5])

    def test_count_le_1_never_engages_decay_no_event_recorded(self):
        """A fresh/once-seen node stays on the pure count fast path — no
        decay comparison happens, so nothing is recorded for it."""
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_decay_0006", claim="fresh claim")],
            query_embedding=[1.0, 0.0],
        )
        self.hook.format_nudge(result, session_id="s1")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        last_entry = ledger["s1"][-1]
        events = [e for e in last_entry.get("decay_events", []) if e["id"] == "ob_decay_0006"]
        self.assertEqual(events, [])


# ---------------------------------------------------------------------------
# Same-session-echo guard (rec 2)
# ---------------------------------------------------------------------------


class TestSameSessionEchoGuard(DecayEchoTestBase):
    def test_node_created_moments_ago_this_session_is_dropped(self):
        now = datetime.now(timezone.utc)
        self._write_session_marker("s1", now - timedelta(minutes=30))
        created_at = now - timedelta(minutes=2)
        result = _make_result(
            top_claims=[_claim_entry(
                node_id="ob_echo_0001", claim="just wrote this",
                created_at=_iso(created_at), created_minutes_ago=2.0,
            )],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("ob_echo_0001", nudge)
        self.assertNotIn("Top claims:", nudge)

    def test_node_older_than_threshold_renders_normally(self):
        now = datetime.now(timezone.utc)
        self._write_session_marker("s1", now - timedelta(minutes=30))
        created_at = now - timedelta(minutes=25)  # older than default 10min
        result = _make_result(
            top_claims=[_claim_entry(
                node_id="ob_echo_0002", claim="written a while ago",
                created_at=_iso(created_at), created_minutes_ago=25.0,
            )],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("[ob_echo_0002]", nudge)
        self.assertIn("Top claims:", nudge)

    def test_node_predates_session_start_not_treated_as_echo(self):
        """A node created 2 minutes ago, but the session itself only
        started 1 minute ago — the node was written by a PREVIOUS session
        moments before this one started, not by this session. Must not be
        misclassified as an echo."""
        now = datetime.now(timezone.utc)
        self._write_session_marker("s1", now - timedelta(minutes=1))
        created_at = now - timedelta(minutes=2)
        result = _make_result(
            top_claims=[_claim_entry(
                node_id="ob_echo_0003", claim="from a previous session",
                created_at=_iso(created_at), created_minutes_ago=2.0,
            )],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("[ob_echo_0003]", nudge)

    def test_missing_session_marker_disables_guard(self):
        """No ~/.engram/sessions/<id>.json for this session_id — the guard
        can't verify the session boundary, so it stays inactive rather than
        risk dropping a legitimate render (fail-open)."""
        now = datetime.now(timezone.utc)
        created_at = now - timedelta(minutes=1)
        result = _make_result(
            top_claims=[_claim_entry(
                node_id="ob_echo_0004", claim="no session marker on disk",
                created_at=_iso(created_at), created_minutes_ago=1.0,
            )],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("[ob_echo_0004]", nudge)

    def test_custom_minutes_threshold_respected(self):
        now = datetime.now(timezone.utc)
        self._write_session_marker("s1", now - timedelta(minutes=30))
        self._config("same_session_echo", {"minutes": 1})
        created_at = now - timedelta(minutes=2)  # would echo at default 10min, not at 1min
        result = _make_result(
            top_claims=[_claim_entry(
                node_id="ob_echo_0005", claim="2 minutes old",
                created_at=_iso(created_at), created_minutes_ago=2.0,
            )],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertIn("[ob_echo_0005]", nudge)  # 2min > custom 1min threshold -> not an echo

    def test_echo_drop_recorded_in_ledger_not_silent(self):
        now = datetime.now(timezone.utc)
        self._write_session_marker("s1", now - timedelta(minutes=30))
        created_at = now - timedelta(minutes=2)
        result = _make_result(
            top_claims=[_claim_entry(
                node_id="ob_echo_0006", claim="just wrote this",
                created_at=_iso(created_at), created_minutes_ago=2.0,
            )],
        )
        self.hook.format_nudge(result, session_id="s1")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        last_entry = ledger["s1"][-1]
        self.assertIn("suppressed_echo_ids", last_entry)
        self.assertIn("ob_echo_0006", last_entry["suppressed_echo_ids"])
        # Dropped, so never counted as actually rendered.
        self.assertNotIn("ob_echo_0006", last_entry["ids"])

    def test_session_id_none_disables_echo_guard_entirely(self):
        """Legacy call shape (session_id omitted) — suppression is inactive
        wholesale, echo guard included."""
        now = datetime.now(timezone.utc)
        created_at = now - timedelta(minutes=1)
        result = _make_result(
            top_claims=[_claim_entry(
                node_id="ob_echo_0007", claim="just wrote this",
                created_at=_iso(created_at), created_minutes_ago=1.0,
            )],
        )
        nudge = self.hook.format_nudge(result)  # no session_id
        self.assertIn("[ob_echo_0007]", nudge)

    def test_special_node_echo_guard_applies_too(self):
        now = datetime.now(timezone.utc)
        self._write_session_marker("s1", now - timedelta(minutes=30))
        created_at = now - timedelta(minutes=1)
        result = _make_result(
            special_nodes=[_special_entry(
                node_id="ax_echo_0001", claim="just filed this axiom",
                created_at=_iso(created_at), created_minutes_ago=1.0,
            )],
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("ax_echo_0001", nudge)
        self.assertNotIn("Specials:", nudge)

    # -- Regression coverage for colleague-review catch, PR #1779 round 1:
    # the echo guard originally only fired for specials/top_claims (full
    # entry dicts) — matched_meta-only candidates (Others pool, the
    # not-recalled reservation pool, and any `evidence`-type node, which
    # never appears in specials/top_claims at all) fell through a bare-id
    # call path where the guard could never engage. Fixed by carrying
    # created_at/created_minutes_ago on matched_meta entries too and
    # passing the full dict at every _tier_for call site.

    def test_others_pool_candidate_echo_guard_applies(self):
        """A node that ONLY ever appears in matched_meta (never ranks into
        top_claims/special_nodes) — the class an `evidence`-type node is
        permanently stuck in — must still be echo-guarded."""
        now = datetime.now(timezone.utc)
        self._write_session_marker("s1", now - timedelta(minutes=30))
        created_at = now - timedelta(minutes=1)
        result = _make_result(
            matched_ids=["ob_echo_others_0001"],
            matched_meta=[{
                "id": "ob_echo_others_0001", "type": "evidence",
                "recall_keywords": ["kw"],
                "created_at": _iso(created_at), "created_minutes_ago": 1.0,
            }],
            match_count=1,
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("ob_echo_others_0001", nudge)
        self.assertNotIn("Others:", nudge)

    def test_not_recalled_reservation_candidate_echo_guard_applies(self):
        """A node reserved via the not-recalled slot (freed by suppression
        elsewhere) must also be echo-guarded — it only ever reaches the
        reservation loop's bare-id-shaped call before the fix."""
        now = datetime.now(timezone.utc)
        self._write_session_marker("s1", now - timedelta(minutes=30))
        created_at = now - timedelta(minutes=1)
        # Suppress one top_claim (frees a reservation slot) via a seeded
        # high-count ledger with no stored embedding, so it falls back to
        # count-only "suppress" deterministically.
        self._seed_ledger("s1", [["ob_filler"], ["ob_filler"], ["ob_filler"]])
        result = _make_result(
            top_claims=[_claim_entry(node_id="ob_filler", claim="filler")],
            matched_meta=[
                {"id": "ob_filler", "type": "observation_factual", "recall_keywords": ["kw"]},
                {
                    "id": "ob_echo_reserved_0001", "type": "observation_factual",
                    "recall_keywords": ["kw2"],
                    "created_at": _iso(created_at), "created_minutes_ago": 1.0,
                },
            ],
            not_recalled_recently=["ob_echo_reserved_0001"],
            match_count=2,
        )
        nudge = self.hook.format_nudge(result, session_id="s1")
        self.assertNotIn("ob_echo_reserved_0001", nudge)
        self.assertNotIn("Worth revisiting", nudge)


class TestDecayEventDedup(DecayEchoTestBase):
    def test_decay_event_not_duplicated_when_classified_in_two_loops(self):
        """A node eligible for both the not-recalled reservation loop AND
        the Others loop (both walk overlapping matched_meta-derived pools)
        must be tier-classified only once in the ledger's decay_events —
        colleague-review catch, PR #1779 round 1: _tier_for's side effect
        (appending to decay_events) was not idempotent per id, so a node
        reachable from both loops could silently double-count a firing."""
        self._seed_ledger("s1", [
            {"ids": ["ob_dup_0001"], "prompt_embedding": [1.0, 0.0]},
            {"ids": ["ob_dup_0001"], "prompt_embedding": [1.0, 0.0]},
        ])
        result = _make_result(
            matched_ids=["ob_dup_0001"],
            matched_meta=[{
                "id": "ob_dup_0001", "type": "observation_factual",
                "recall_keywords": ["kw"],
            }],
            not_recalled_recently=["ob_dup_0001"],
            match_count=1,
            query_embedding=[1.0, 0.0],
        )
        self.hook.format_nudge(result, session_id="s1")
        with open(self._ledger_path(), "r", encoding="utf-8") as f:
            ledger = json.load(f)
        last_entry = ledger["s1"][-1]
        events = [e for e in last_entry.get("decay_events", []) if e["id"] == "ob_dup_0001"]
        self.assertEqual(len(events), 1, f"Expected exactly one decay_event, got: {events}")


class TestDecayTierForShapeMismatch(unittest.TestCase):
    """Colleague-review catch, PR #1779 round 1: a present-but-wrong-shape
    embedding must route through the same baseline-tier + cosine=None
    fallback as a genuinely-missing one, not reach _cosine_similarity (whose
    0.0-on-mismatch guard would be indistinguishable in the ledger from a
    real near-orthogonal comparison)."""

    def setUp(self):
        self.hook = _load_hook_module()

    def test_mismatched_shape_falls_back_to_count_only_not_zero_cosine(self):
        window = [{"ts": 1, "ids": ["ob_x"], "prompt_embedding": [1.0, 0.0]}]
        tier, event = self.hook._decay_tier_for(
            "ob_x", 5, 3, [1.0, 0.0, 0.0], window, 0.75,  # 3-dim query vs 2-dim stored
        )
        self.assertEqual(tier, "suppress")  # count-only fallback: count=5 >= m=3
        self.assertIsNone(event["cosine"])


if __name__ == "__main__":
    unittest.main()
