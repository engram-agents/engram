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
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

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
            # any-junk (but not all-junk) fire: one junk, one real term
            {
                "ts": 2.0,
                "outcome": "rendered",
                "novel_terms": ["curl", "portfolio"],
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
                "novel_terms": ["str", "sed", "curl"],
                "rendered_ids": [],
            },
        ]
        _write_jsonl(ledger_path, records)

        result = rmh.compute_junk_fire(ledger_path, rmh.DEFAULT_STOPLIST)
        self.assertEqual(result["n_rendered_fires"], 3)
        self.assertEqual(result["n_novel_tokens"], 6)  # 2 + 2 + 2, excludes non-rendered
        self.assertEqual(result["n_junk_tokens"], 3)  # str,sed (fire1) + curl (fire2)
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
