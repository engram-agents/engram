"""Tests for tools/recall_measurement_harness.py -- rec-4 unified recall
measurement harness (#266, #1780).

Coverage:
  1. Engagement-floor hook-exclusion regression (THE load-bearing test): a
     node ID appearing ONLY in hook `additionalContext` / a tool RESULT
     must NOT be counted as "authored" -- if this parse mis-classifies hook
     text as authored, the engagement-floor metric silently inflates
     (~38% -> ~96%, per the #1780 design comment).
  2. Engagement-floor positive case: a rendered node ID referenced in a
     LATER assistant text block IS counted.
  3. Engagement-floor positive case via tool_use input params.
  4. End-to-end compute_engagement_floor() on a tiny ledger + transcript:
     confirms the fraction itself (not just the id-extraction helper).
  5. Junk-fire: all-junk / any-junk / token-fraction on a small crafted
     ledger, computed against the module's seed stoplist.
  6. Same-session-echo: parses pre-#1779 `{ts, ids}` and post-#1779
     `{ts, ids, ..., suppressed_echo_ids, decay_events}` shapes identically
     with no error; counts suppressed_echo_ids correctly; an omitted field
     defaults to 0/empty.
  7. Repeat-render wrapper: compute_repeat_render() aggregates a tiny
     synthetic index.db via the REUSED recall_repetition_analysis module
     (not reimplemented here).

All tests are hermetic: temp files only, no network, no dependency on a
real ~/.engram install.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

TOOLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"
)
# append (not insert(0)) -- recall_measurement_harness.py doesn't collide
# with anything in src/, but making tools/ a sys.path *root* at position 0
# would outrank src/forum for the rest of the pytest run, shadowing the
# real `forum` package for every test collected afterward (#2037/#2063).
if TOOLS_DIR not in sys.path:
    sys.path.append(TOOLS_DIR)

import recall_measurement_harness as rmh  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _write_jsonl(path: str, records) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


class TranscriptFixtureMixin:
    """Shared transcript-building helpers for engagement-floor tests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rmh-test-")
        self.addCleanup(self._cleanup_tmpdir)
        self.fire_dt = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.fire_ts = self.fire_dt.timestamp()

    def _cleanup_tmpdir(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _after(self, seconds: int) -> datetime:
        return self.fire_dt + timedelta(seconds=seconds)


class TestEngagementFloorHookExclusion(TranscriptFixtureMixin, unittest.TestCase):
    """The MUST-HAVE regression: hook additionalContext / tool results never
    count as engagement, even when they contain a rendered node ID."""

    def test_hook_additional_context_id_not_counted_as_authored(self):
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        records = [
            # Hook re-surfaces ob_9001 via additionalContext AFTER the fire.
            # This must NOT register as authored engagement.
            {
                "type": "attachment",
                "attachment": {
                    "type": "hook_additional_context",
                    "content": [
                        "[ENGRAM in-turn recall — novel terms: foo]\n"
                        "  - [ob_9001] some surfaced claim text"
                    ],
                    "hookName": "PreToolUse:Bash",
                },
                "timestamp": _iso(self._after(5)),
            },
        ]
        _write_jsonl(transcript_path, records)

        authored = rmh.parse_transcript_authored_ids(transcript_path)
        self.assertNotIn(
            "ob_9001",
            authored,
            "hook-injected additionalContext must never count as authored output",
        )

    def test_tool_result_id_not_counted_as_authored(self):
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        records = [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "tool_use_id": "toolu_1",
                            "type": "tool_result",
                            "content": "engram_inspect result: ob_9002 has conf 0.8",
                            "is_error": False,
                        }
                    ],
                },
                "timestamp": _iso(self._after(5)),
            },
        ]
        _write_jsonl(transcript_path, records)

        authored = rmh.parse_transcript_authored_ids(transcript_path)
        self.assertNotIn(
            "ob_9002",
            authored,
            "tool RESULT content must never count as authored output",
        )

    def test_compute_engagement_floor_end_to_end_hook_only_scores_zero(self):
        """A fire whose rendered id appears ONLY in hook additionalContext /
        a tool result, and nowhere in assistant text or tool_use params,
        must engagement-floor to 0 for that fire -- reproduces the whole
        point of the #1780 union-join exclusion."""
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        records = [
            {
                "type": "attachment",
                "attachment": {
                    "type": "hook_additional_context",
                    "content": ["[ENGRAM Recall: 1 nodes match] - [ob_9003] re-surfaced"],
                    "hookName": "UserPromptSubmit",
                },
                "timestamp": _iso(self._after(2)),
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "tool_use_id": "toolu_2",
                            "type": "tool_result",
                            "content": "ob_9003 appears in this tool result too",
                        }
                    ],
                },
                "timestamp": _iso(self._after(10)),
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Doing unrelated work now."}],
                },
                "timestamp": _iso(self._after(15)),
            },
        ]
        _write_jsonl(transcript_path, records)

        ledger_path = os.path.join(self.tmpdir, "in-turn-recall-ledger.jsonl")
        _write_jsonl(
            ledger_path,
            [
                {
                    "ts": self.fire_ts,
                    "session_id": "sess-hookonly",
                    "transcript_path": transcript_path,
                    "outcome": "rendered",
                    "rendered_ids": ["ob_9003"],
                    "novel_terms": ["foo"],
                }
            ],
        )
        surface_ledger_path = os.path.join(self.tmpdir, "surface-ledger.json")
        with open(surface_ledger_path, "w", encoding="utf-8") as f:
            json.dump({}, f)

        result = rmh.compute_engagement_floor(
            ledger_path, surface_ledger_path, [], None
        )
        self.assertEqual(result["n_fires_total"], 1)
        self.assertEqual(result["n_engaged"], 0)
        self.assertEqual(result["engagement_floor"], 0.0)


class TestEngagementFloorPositiveCases(TranscriptFixtureMixin, unittest.TestCase):
    def test_id_in_later_assistant_text_is_counted(self):
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Based on ob_9101, I'll proceed with the fix.",
                        }
                    ],
                },
                "timestamp": _iso(self._after(30)),
            },
        ]
        _write_jsonl(transcript_path, records)

        authored = rmh.parse_transcript_authored_ids(transcript_path)
        self.assertIn("ob_9101", authored)

    def test_id_in_tool_use_input_params_is_counted(self):
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__plugin_engram_engram__engram_inspect",
                            "input": {"payload_json": '{"node_id": "ob_9102"}'},
                        }
                    ],
                },
                "timestamp": _iso(self._after(30)),
            },
        ]
        _write_jsonl(transcript_path, records)

        authored = rmh.parse_transcript_authored_ids(transcript_path)
        self.assertIn("ob_9102", authored)

    def test_thinking_block_not_counted(self):
        """Judgment call (flagged in handoff): the #1780 design comment
        enumerates exactly two authored categories (assistant text +
        tool_use params); "thinking" blocks are latent reasoning, not
        authored output, so this harness does not count them."""
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "ob_9103 might be relevant..."}
                    ],
                },
                "timestamp": _iso(self._after(30)),
            },
        ]
        _write_jsonl(transcript_path, records)

        authored = rmh.parse_transcript_authored_ids(transcript_path)
        self.assertNotIn("ob_9103", authored)

    def test_compute_engagement_floor_end_to_end_positive(self):
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Citing ob_9201 for this decision."}
                    ],
                },
                "timestamp": _iso(self._after(20)),
            },
        ]
        _write_jsonl(transcript_path, records)

        ledger_path = os.path.join(self.tmpdir, "in-turn-recall-ledger.jsonl")
        _write_jsonl(
            ledger_path,
            [
                {
                    "ts": self.fire_ts,
                    "session_id": "sess-positive",
                    "transcript_path": transcript_path,
                    "outcome": "rendered",
                    "rendered_ids": ["ob_9201"],
                    "novel_terms": ["bar"],
                }
            ],
        )
        surface_ledger_path = os.path.join(self.tmpdir, "surface-ledger.json")
        with open(surface_ledger_path, "w", encoding="utf-8") as f:
            json.dump({}, f)

        result = rmh.compute_engagement_floor(
            ledger_path, surface_ledger_path, [], None
        )
        self.assertEqual(result["n_fires_total"], 1)
        self.assertEqual(result["n_engaged"], 1)
        self.assertEqual(result["engagement_floor"], 1.0)

    def test_id_referenced_before_fire_does_not_count(self):
        """Only occurrences AFTER the fire timestamp count -- an assistant
        mention of the id before the render happened is not "engagement
        with the recall"."""
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Mentioning ob_9301 early."}],
                },
                "timestamp": _iso(self._after(-30)),
            },
        ]
        _write_jsonl(transcript_path, records)

        ledger_path = os.path.join(self.tmpdir, "in-turn-recall-ledger.jsonl")
        _write_jsonl(
            ledger_path,
            [
                {
                    "ts": self.fire_ts,
                    "session_id": "sess-before",
                    "transcript_path": transcript_path,
                    "outcome": "rendered",
                    "rendered_ids": ["ob_9301"],
                    "novel_terms": ["baz"],
                }
            ],
        )
        surface_ledger_path = os.path.join(self.tmpdir, "surface-ledger.json")
        with open(surface_ledger_path, "w", encoding="utf-8") as f:
            json.dump({}, f)

        result = rmh.compute_engagement_floor(
            ledger_path, surface_ledger_path, [], None
        )
        self.assertEqual(result["n_engaged"], 0)
        self.assertEqual(result["engagement_floor"], 0.0)


class TestEngagementFloorDistinctNode(TranscriptFixtureMixin, unittest.TestCase):
    """#1787 part 2: the distinct-cited-node floor must be invariant to
    render multiplicity -- a node rendered N times in one session should
    only ever count once toward the distinct-node denominator/numerator,
    unlike the per-fire floor, which counts each render separately."""

    def test_distinct_node_floor_invariant_to_render_multiplicity(self):
        transcript_path = os.path.join(self.tmpdir, "session.jsonl")
        # ob_9401 is cited exactly once, well after all three of its renders.
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Citing ob_9401 for this decision."}
                    ],
                },
                "timestamp": _iso(self._after(100)),
            },
        ]
        _write_jsonl(transcript_path, records)

        ledger_path = os.path.join(self.tmpdir, "in-turn-recall-ledger.jsonl")
        _write_jsonl(
            ledger_path,
            [
                # ob_9401 rendered three separate times in the SAME session, at
                # increasing timestamps -- render-multiplicity to be
                # invariant against.
                {
                    "ts": self.fire_ts,
                    "session_id": "sess-distinct",
                    "transcript_path": transcript_path,
                    "outcome": "rendered",
                    "rendered_ids": ["ob_9401"],
                    "novel_terms": ["foo"],
                },
                {
                    "ts": self.fire_ts + 10,
                    "session_id": "sess-distinct",
                    "transcript_path": transcript_path,
                    "outcome": "rendered",
                    "rendered_ids": ["ob_9401"],
                    "novel_terms": ["foo"],
                },
                {
                    "ts": self.fire_ts + 20,
                    "session_id": "sess-distinct",
                    "transcript_path": transcript_path,
                    "outcome": "rendered",
                    "rendered_ids": ["ob_9401"],
                    "novel_terms": ["foo"],
                },
                # ob_9402: a second distinct node, rendered once, never cited.
                {
                    "ts": self.fire_ts + 30,
                    "session_id": "sess-distinct",
                    "transcript_path": transcript_path,
                    "outcome": "rendered",
                    "rendered_ids": ["ob_9402"],
                    "novel_terms": ["bar"],
                },
            ],
        )
        surface_ledger_path = os.path.join(self.tmpdir, "surface-ledger.json")
        with open(surface_ledger_path, "w", encoding="utf-8") as f:
            json.dump({}, f)

        result = rmh.compute_engagement_floor(
            ledger_path, surface_ledger_path, [], None
        )

        self.assertEqual(result["n_distinct_nodes"], 2)
        self.assertEqual(result["n_distinct_engaged"], 1)
        self.assertEqual(result["engagement_floor_distinct"], 0.5)

        # Per-fire floor inflates: all 3 ob_9401 fires are individually
        # "engaged" (the citation is after every one of their fire.ts), so
        # the per-fire numerator/denominator differ from the distinct-node
        # variant -- this locks in that the two metrics genuinely diverge
        # under render multiplicity.
        self.assertNotEqual(
            result["engagement_floor"], result["engagement_floor_distinct"]
        )


class TestJunkFire(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rmh-junk-test-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_junk_fire_fractions_on_crafted_ledger(self):
        ledger_path = os.path.join(self.tmpdir, "ledger.jsonl")
        records = [
            # all-junk fire: every novel term is in the stoplist
            {
                "ts": 1.0,
                "outcome": "rendered",
                "novel_terms": ["str", "sed"],
                "rendered_ids": ["ob_1"],
            },
            # any-junk (but not all-junk) fire: one junk, one real term.
            # NB (#1784): uses "grep" (in the canonical engram_idf.JUNK_STOPLIST
            # that DEFAULT_STOPLIST now aliases), not the former "curl" — curl
            # was one of the harness's ambiguous extras dropped when the
            # measurement reconciled DOWN to the conservative filter list.
            {
                "ts": 2.0,
                "outcome": "rendered",
                "novel_terms": ["grep", "portfolio"],
                "rendered_ids": ["ob_2"],
            },
            # no-junk fire
            {
                "ts": 3.0,
                "outcome": "rendered",
                "novel_terms": ["portfolio", "rebalance"],
                "rendered_ids": ["ob_3"],
            },
            # non-rendered fire -- must be excluded from all denominators
            {
                "ts": 4.0,
                "outcome": "no_matches",
                "novel_terms": ["str", "sed", "grep"],
                "rendered_ids": [],
            },
        ]
        _write_jsonl(ledger_path, records)

        result = rmh.compute_junk_fire(ledger_path, rmh.DEFAULT_STOPLIST)
        self.assertEqual(result["n_rendered_fires"], 3)
        self.assertEqual(result["n_novel_tokens"], 6)  # 2 + 2 + 2, excludes non-rendered
        self.assertEqual(result["n_junk_tokens"], 3)  # str,sed (fire1) + grep (fire2)
        self.assertAlmostEqual(result["token_fraction"], 3 / 6)
        self.assertAlmostEqual(result["any_junk_fraction"], 2 / 3)  # fires 1,2
        self.assertAlmostEqual(result["all_junk_fraction"], 1 / 3)  # fire 1 only


class TestSameSessionEcho(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rmh-echo-test-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pre_1779_shape_omitted_fields_default_to_zero(self):
        path = os.path.join(self.tmpdir, "surface-ledger.json")
        data = {
            "sess-a": [
                {"ts": 1.0, "ids": ["ob_1", "ob_2"]},
                {"ts": 2.0, "ids": ["ob_3"]},
            ]
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        result = rmh.compute_same_session_echo(path)
        self.assertTrue(result["available"])
        self.assertEqual(result["suppressed_echo_count"], 0)
        self.assertEqual(result["n_entries_with_suppression"], 0)
        self.assertFalse(result["has_post_1779_fields"])
        self.assertIsNone(result["decay_cosine_distribution"])

    def test_post_1779_shape_counts_suppressed_and_cosines(self):
        path = os.path.join(self.tmpdir, "surface-ledger.json")
        data = {
            "sess-b": [
                {
                    "ts": 1.0,
                    "ids": ["ob_1", "ob_2"],
                    "prompt_embedding": [0.1, 0.2],
                    "suppressed_echo_ids": ["ob_9"],
                    "decay_events": [{"id": "ob_9", "tier": "suppress", "cosine": 0.81}],
                },
                {
                    "ts": 2.0,
                    "ids": ["ob_3"],
                    "prompt_embedding": [0.3, 0.4],
                    "suppressed_echo_ids": ["ob_10", "ob_11"],
                    "decay_events": [
                        {"id": "ob_10", "tier": "suppress", "cosine": 0.90},
                        {"id": "ob_11", "tier": "others", "cosine": 0.60},
                    ],
                },
            ]
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        result = rmh.compute_same_session_echo(path)
        self.assertTrue(result["available"])
        self.assertEqual(result["suppressed_echo_count"], 3)  # 1 + 2
        self.assertEqual(result["n_entries_with_suppression"], 2)
        self.assertTrue(result["has_post_1779_fields"])
        cs = result["decay_cosine_distribution"]
        self.assertIsNotNone(cs)
        self.assertEqual(cs["count"], 3)
        self.assertAlmostEqual(cs["min"], 0.60)
        self.assertAlmostEqual(cs["max"], 0.90)

    def test_mixed_shapes_parse_with_one_code_path_no_error(self):
        """Pre- and post-#1779 entries in the SAME ledger (e.g. a ledger
        that straddles the #1779 upgrade) must parse without error through
        one code path."""
        path = os.path.join(self.tmpdir, "surface-ledger.json")
        data = {
            "sess-mixed": [
                {"ts": 1.0, "ids": ["ob_1"]},  # pre-#1779, no extra fields
                {
                    "ts": 2.0,
                    "ids": ["ob_2"],
                    "suppressed_echo_ids": ["ob_9"],
                    "decay_events": [{"id": "ob_9", "tier": "suppress", "cosine": 0.77}],
                },  # post-#1779
            ]
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        result = rmh.compute_same_session_echo(path)  # must not raise
        self.assertTrue(result["available"])
        self.assertEqual(result["suppressed_echo_count"], 1)
        self.assertEqual(result["n_entries"], 2)
        self.assertTrue(result["has_post_1779_fields"])


class TestRepeatRenderWrapper(unittest.TestCase):
    """Confirms the wrapper correctly aggregates recall_repetition_analysis's
    own (reused, not reimplemented) functions against a tiny synthetic
    index.db."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rmh-repeat-test-")
        self.addCleanup(self._cleanup)
        self.db_path = os.path.join(self.tmpdir, "index.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE events (uuid TEXT, sessionId TEXT, turn INTEGER, "
            "ts TEXT, event_type TEXT, tool_use_id TEXT, level INTEGER, "
            "daemon_latency_ms INTEGER, fallback_to_fts INTEGER, "
            "hook_name TEXT, hook_duration_ms INTEGER, hook_exit_code INTEGER, "
            "tool_name TEXT, result_status TEXT, data JSON)"
        )
        rows = [
            ("u1", "s1", 1, "1", "engram.surface.fire", None, 1, 0, 0, None, 0, 0, None, None,
             json.dumps({"matched_ids": ["ob_1", "ob_2"]})),
            ("u2", "s1", 2, "2", "engram.surface.fire", None, 1, 0, 0, None, 0, 0, None, None,
             json.dumps({"matched_ids": ["ob_1", "ob_3"]})),  # ob_1 repeats within k=1
        ]
        conn.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_compute_repeat_render_wraps_reused_module(self):
        result = rmh.compute_repeat_render(self.db_path)
        self.assertTrue(result["available"])
        self.assertEqual(result["n_sessions"], 1)
        self.assertEqual(result["n_surface_fire_events"], 2)
        self.assertEqual(result["n_ids_total_incl_repeats"], 4)
        self.assertIn(1, result["windows"])
        # 1 repeat (ob_1) out of 4 total ids checked at k=1 (window covers all k in WINDOWS)
        w1 = result["windows"][1]
        self.assertEqual(w1["ids_checked"], 4)
        self.assertEqual(w1["repeats"], 1)

    def test_compute_repeat_render_missing_db_reports_unavailable(self):
        result = rmh.compute_repeat_render(os.path.join(self.tmpdir, "nope.db"))
        self.assertFalse(result["available"])
        self.assertIn("reason", result)


class TestOutputFormatting(unittest.TestCase):
    """Sanity checks that both output formats render without error."""

    def test_json_and_md_render_without_error(self):
        report = {
            "seat": "test-seat",
            "engram_home": "/tmp/fake",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "sources": {"ledger_path": "x", "surface_ledger_path": "y", "index_db_path": "z"},
            "metrics": {
                "repeat_render": {"available": False, "reason": "no db"},
                "junk_fire": {"available": False, "reason": "no ledger"},
                "same_session_echo": {"available": False, "reason": "no ledger"},
                "engagement_floor": {"available": False},
            },
        }
        md = rmh.render_md(report)
        self.assertIn("test-seat", md)
        js = rmh.render_json(report)
        parsed = json.loads(js)
        self.assertEqual(parsed["seat"], "test-seat")


class TestRobustness(unittest.TestCase):
    """Reviewer-fairy findings (PR #1781): a measurement tool must fail loud
    at the metric level, never crash the whole run on one bad input file."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rmh-robust-")

        def _cleanup():
            import shutil
            shutil.rmtree(self.tmpdir, ignore_errors=True)

        self.addCleanup(_cleanup)

    def test_corrupt_surface_ledger_degrades_not_crash(self):
        p = os.path.join(self.tmpdir, "surface-ledger.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ")
        res = rmh.compute_same_session_echo(p)  # must not raise
        self.assertFalse(res["available"])
        self.assertIn("reason", res)
        # the engagement-floor loader over the same file also degrades to []
        self.assertEqual(rmh._load_surface_fires(p), [])

    def test_missing_or_malformed_ts_record_skipped_not_crash(self):
        p = os.path.join(self.tmpdir, "in-turn-recall-ledger.jsonl")
        _write_jsonl(p, [
            # missing "ts" entirely
            {"outcome": "rendered", "novel_terms": ["portfolio"],
             "rendered_ids": ["ob_0001"]},
            # non-numeric "ts"
            {"ts": "not-a-number", "outcome": "rendered",
             "novel_terms": ["nvda"], "rendered_ids": ["ob_0002"]},
            # well-formed
            {"ts": 5.0, "outcome": "rendered", "novel_terms": ["ewy"],
             "rendered_ids": ["ob_0003"]},
        ])
        fires = rmh._load_in_turn_fires(p)  # must not raise KeyError/ValueError
        self.assertEqual(len(fires), 1)
        self.assertEqual(fires[0].ids, {"ob_0003"})

    def test_build_report_isolates_one_metric_failure(self):
        # A corrupt surface-ledger + an absent index.db must NOT discard the
        # metrics that can still be computed.
        surf = os.path.join(self.tmpdir, "surface-ledger.json")
        with open(surf, "w", encoding="utf-8") as f:
            f.write("{ broken")
        ledger = os.path.join(self.tmpdir, "in-turn-recall-ledger.jsonl")
        _write_jsonl(ledger, [
            {"ts": 1.0, "outcome": "rendered", "novel_terms": ["portfolio"],
             "rendered_ids": ["ob_0001"]},
        ])
        import argparse
        args = argparse.Namespace(
            engram_home=self.tmpdir, seat="test-seat",
            ledger_path=ledger, surface_ledger_path=surf,
            index_db_path=os.path.join(self.tmpdir, "absent.db"),
            stoplist_file=None, transcript=[], transcript_dir=None,
        )
        report = rmh.build_report(args)  # must not raise
        m = report["metrics"]
        # the corrupt-ledger metric degrades (available: False), but junk-fire
        # (which reads the valid in-turn ledger) still reports normally.
        self.assertFalse(m["same_session_echo"]["available"])
        self.assertIn("junk_fire", m)
        self.assertTrue(m["junk_fire"].get("available", True))
        self.assertIn("engagement_floor", m)


class TestToEpoch(unittest.TestCase):
    """#1787: the three data sources store ts in mixed formats -- _to_epoch
    normalizes all of them to epoch seconds for --split-at comparisons."""

    def test_to_epoch_float_passes_through(self):
        self.assertEqual(rmh._to_epoch(1784384729.229103), 1784384729.229103)

    def test_to_epoch_int_passes_through_as_float(self):
        self.assertEqual(rmh._to_epoch(1000), 1000.0)

    def test_to_epoch_iso_string_with_trailing_z_parses_to_correct_epoch(self):
        iso = "2026-05-21T19:35:34.697Z"
        expected = datetime(
            2026, 5, 21, 19, 35, 34, 697000, tzinfo=timezone.utc
        ).timestamp()
        self.assertAlmostEqual(rmh._to_epoch(iso), expected, places=3)

    def test_to_epoch_iso_string_without_z_parses(self):
        iso = "2026-05-21T19:35:34.697+00:00"
        expected = datetime(
            2026, 5, 21, 19, 35, 34, 697000, tzinfo=timezone.utc
        ).timestamp()
        self.assertAlmostEqual(rmh._to_epoch(iso), expected, places=3)

    def test_to_epoch_raises_valueerror_on_malformed_string(self):
        # Contract preserved post-#1787-follow-up dedup via _parse_iso_ts:
        # _to_epoch still RAISES (unlike _parse_iso_ts, which returns None)
        # -- the --split-at window filter (_in_window) relies on the raise.
        with self.assertRaises(ValueError):
            rmh._to_epoch("garbage")


class TestSplitAt(unittest.TestCase):
    """#1787: --split-at partitions fires/events/entries into a before-slice
    (ts < split) and an after-slice (ts >= split), across all three mixed-ts
    data sources, and computes per-metric deltas."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="rmh-split-test-")
        self.addCleanup(self._cleanup)
        # Split boundary.
        self.split_dt = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
        self.split_iso = self.split_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        self.split_epoch = self.split_dt.timestamp()

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _mk_transcript(self, node_id: str, fire_ts: float) -> str:
        """A transcript with an assistant text block citing node_id shortly
        after fire_ts, so the fire counts as engaged."""
        path = os.path.join(self.tmpdir, f"session-{node_id}.jsonl")
        cite_dt = datetime.fromtimestamp(fire_ts, tz=timezone.utc) + timedelta(seconds=5)
        records = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Citing {node_id}."}],
                },
                "timestamp": _iso(cite_dt),
            },
        ]
        _write_jsonl(path, records)
        return path

    def test_in_window_partitions_before_after_half_open(self):
        # before: ts < split; after: ts >= split (half-open, boundary goes
        # to "after").
        self.assertTrue(rmh._in_window(self.split_epoch - 1, (None, self.split_epoch)))
        self.assertFalse(rmh._in_window(self.split_epoch, (None, self.split_epoch)))
        self.assertFalse(rmh._in_window(self.split_epoch - 1, (self.split_epoch, None)))
        self.assertTrue(rmh._in_window(self.split_epoch, (self.split_epoch, None)))
        # No window -> always in.
        self.assertTrue(rmh._in_window(self.split_epoch, None))

    def test_in_window_malformed_ts_excluded_not_raised(self):
        # A malformed ts under an ACTIVE window is excluded (False), not a
        # crash -- _in_window catches _to_epoch's ValueError.
        self.assertFalse(
            rmh._in_window("not-a-timestamp", (0.0, 100.0))
        )

    def test_build_report_split_partitions_fires_and_computes_deltas(self):
        before_ts = self.split_epoch - 3600  # 1h before split
        after_ts = self.split_epoch + 3600   # 1h after split

        t1 = self._mk_transcript("ob_8001", before_ts)
        t2 = self._mk_transcript("ob_8002", before_ts)
        t3 = self._mk_transcript("ob_8003", after_ts)

        ledger_path = os.path.join(self.tmpdir, "in-turn-recall-ledger.jsonl")
        _write_jsonl(
            ledger_path,
            [
                # 2 fires before the split
                {
                    "ts": before_ts, "session_id": "s1", "transcript_path": t1,
                    "outcome": "rendered", "rendered_ids": ["ob_8001"],
                    "novel_terms": ["portfolio"],
                },
                {
                    "ts": before_ts, "session_id": "s2", "transcript_path": t2,
                    "outcome": "rendered", "rendered_ids": ["ob_8002"],
                    "novel_terms": ["str"],  # junk token
                },
                # 1 fire after the split
                {
                    "ts": after_ts, "session_id": "s3", "transcript_path": t3,
                    "outcome": "rendered", "rendered_ids": ["ob_8003"],
                    "novel_terms": ["rebalance"],
                },
            ],
        )
        surface_ledger_path = os.path.join(self.tmpdir, "surface-ledger.json")
        with open(surface_ledger_path, "w", encoding="utf-8") as f:
            json.dump({}, f)

        args = argparse.Namespace(
            engram_home=self.tmpdir, seat="split-seat",
            ledger_path=ledger_path, surface_ledger_path=surface_ledger_path,
            index_db_path=os.path.join(self.tmpdir, "absent.db"),
            stoplist_file=None, transcript=[], transcript_dir=None,
            split_at=self.split_iso,
        )
        report = rmh.build_report(args)

        self.assertIn("before", report)
        self.assertIn("after", report)
        self.assertEqual(report["split_at"], self.split_iso)

        before_ef = report["before"]["engagement_floor"]
        after_ef = report["after"]["engagement_floor"]
        self.assertEqual(before_ef["n_fires_total"], 2)  # N fires before
        self.assertEqual(after_ef["n_fires_total"], 1)   # M fires after
        self.assertEqual(before_ef["n_engaged"], 2)
        self.assertEqual(after_ef["n_engaged"], 1)

        before_jf = report["before"]["junk_fire"]
        after_jf = report["after"]["junk_fire"]
        self.assertEqual(before_jf["n_rendered_fires"], 2)
        self.assertEqual(after_jf["n_rendered_fires"], 1)

        # A delta must be computed for the metrics available in both slices.
        self.assertIn("deltas", report)
        self.assertIn("engagement_floor", report["deltas"])
        self.assertIn("junk_fire", report["deltas"])
        expected_ef_delta = (
            after_ef["engagement_floor"] - before_ef["engagement_floor"]
        )
        self.assertAlmostEqual(
            report["deltas"]["engagement_floor"]["engagement_floor"],
            expected_ef_delta,
        )

        # render_md/render_json must not raise on the split shape.
        md = rmh.render_md(report)
        self.assertIn("Before", md)
        self.assertIn("After", md)
        self.assertIn("Deltas", md)
        js = rmh.render_json(report)
        parsed = json.loads(js)
        self.assertEqual(parsed["split_at"], self.split_iso)

    def test_non_split_path_unaffected_by_split_at_absent(self):
        """args without a split_at attribute at all (mirrors
        TestRobustness's hand-built Namespace) must still produce the
        plain (non-split) report shape."""
        ledger_path = os.path.join(self.tmpdir, "in-turn-recall-ledger.jsonl")
        _write_jsonl(ledger_path, [
            {"ts": 1.0, "outcome": "rendered", "novel_terms": ["portfolio"],
             "rendered_ids": ["ob_0001"]},
        ])
        surface_ledger_path = os.path.join(self.tmpdir, "surface-ledger.json")
        with open(surface_ledger_path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        args = argparse.Namespace(
            engram_home=self.tmpdir, seat="test-seat",
            ledger_path=ledger_path, surface_ledger_path=surface_ledger_path,
            index_db_path=os.path.join(self.tmpdir, "absent.db"),
            stoplist_file=None, transcript=[], transcript_dir=None,
        )
        report = rmh.build_report(args)
        self.assertNotIn("before", report)
        self.assertIn("metrics", report)


class TestSplitAtCLIValidation(unittest.TestCase):
    """#1787 follow-up: a malformed --split-at must degrade to a clean
    argparse usage error (exit code 2), not an unhandled ValueError
    traceback -- consistent with the module's degrade-not-crash
    convention."""

    def test_malformed_split_at_exits_cleanly_via_parser_error(self):
        with self.assertRaises(SystemExit) as cm:
            rmh.main(["--split-at", "not-a-date"])
        self.assertEqual(cm.exception.code, 2)


class TestNodeIdRegex(unittest.TestCase):
    def test_covers_all_18_type_prefixes(self):
        # Mirrors engram_core.TYPE_PREFIX. Colleague-review catch (#1781):
        # pr/th/ct/gt/ts were missing from the alternation, so those node
        # types silently never counted toward engagement.
        prefixes = ["ob", "dv", "cs", "ls", "qu", "cj", "ev", "fl", "tk",
                    "pn", "gl", "ax", "df", "pr", "th", "ct", "gt", "ts"]
        for pre in prefixes:
            self.assertEqual(
                rmh.NODE_ID_RE.findall(f"see {pre}_0001 here"), [f"{pre}_0001"],
                f"prefix {pre} not matched by NODE_ID_RE")


if __name__ == "__main__":
    unittest.main()
