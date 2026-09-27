"""Tests for the WS4 adjudication kit (engram-alpha#2753), tools/ws4/.

Synthetic fixtures only -- the real fire ledger (ws4-fire-ledger.md) is
being built in parallel on another branch and is not on dev yet; this suite
builds its own transcript + ledger + knowledge.db fixtures against the
schema documented in ws4-adjudication-kit.md / ws4-fire-ledger.md section 2.

No real ENGRAM node ids appear anywhere in this file (CI scrub gate) --
every id below is an obviously synthetic fixture string.

Covers (spec, Piece 4's minimum list):
  1. window closes on K.
  2. closes early on each outbound-act class (Bash, MCP, turn-ending text)
     + the ScheduleWakeup / loop-mode.json exclusions.
  3. redaction removes exactly the chain block.
  4. unlocatable accounting (rows_in == spans_out + unlocatable).
  5. O2 transfer vs raw.
Plus: extract_spans.py end-to-end (spans + key.jsonl content), and a smoke
test of analyze.py's O1 pooled arm-difference + O2 wiring.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.ws4 import constants as ws4_constants
from tools.ws4 import extract_spans as ws4_extract_spans
from tools.ws4 import analyze as ws4_analyze
from tools.ws4.ledger import compute_fire_id, load_jsonl, write_jsonl
from tools.ws4.transcript import (
    TranscriptReadError,
    build_span,
    compute_window,
    extract_nudge_text,
    find_fired_tool_use,
    find_tripwire_attachment,
    load_transcript,
    redact_chain_block,
)


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False))
            f.write("\n")


def _assistant_tool_use(rec_id, tool_use_id, name, tool_input, ts):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
        },
    }


def _assistant_text(text, ts):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool_result(tool_use_id, content, ts):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]},
    }


def _human_prompt(text, ts):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _tripwire_attachment(tool_use_id, additional_context, ts):
    stdout = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": additional_context}})
    return {
        "type": "attachment",
        "timestamp": ts,
        "attachment": {
            "type": "hook_success",
            "hookName": "PreToolUse:Bash",
            "hookEvent": "PreToolUse",
            "toolUseID": tool_use_id,
            "stdout": stdout,
            "command": "python3 .../engram-lesson-tripwire-hook.py",
        },
    }


NUDGE_BLOCK = "[lesson-tripwire] Action-moment pattern matched — remember:\n  • some nudge text"
PRINCIPLES_BLOCK = (
    "[triggered principles]\n"
    "  2 of these 2 matches sit under head-fixture-1 (a short label)\n"
    "  ↑ mid-fixture-1 — a rung summary\n"
    "  ↑ head-fixture-1 — the head's full claim text"
)


class RedactionTests(unittest.TestCase):
    def test_redaction_removes_exactly_the_chain_block(self):
        text = NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK
        redacted, removed = redact_chain_block(text)
        self.assertTrue(removed)
        self.assertEqual(redacted, NUDGE_BLOCK)
        self.assertNotIn("[triggered principles]", redacted)
        self.assertNotIn("head-fixture-1", redacted)

    def test_redaction_preserves_content_after_the_chain_block(self):
        trailer = "[in-turn recall] some unrelated later block"
        text = NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK + "\n" + trailer
        redacted, removed = redact_chain_block(text)
        self.assertTrue(removed)
        self.assertEqual(redacted, NUDGE_BLOCK + "\n" + trailer)

    def test_redaction_is_a_noop_when_no_chain_block_present(self):
        # Control-arm / non-participating fires never had a chain block.
        redacted, removed = redact_chain_block(NUDGE_BLOCK)
        self.assertFalse(removed)
        self.assertEqual(redacted, NUDGE_BLOCK)


class ProductionRedactionSwitchTests(unittest.TestCase):
    """Round 3, item 2: every existing redaction assertion (RedactionTests,
    SpanRenderingAndLeakAuditTests) calls redact_chain_block() directly or
    passes redact_chain=True as a literal -- neither path reads
    constants.REDACT_CHAIN, so the actual blinding switch for the
    pre-registered A/B was never exercised end to end. This test goes
    through the production path -- extract_spans.main(), which calls
    run() with no explicit redact_chain override, i.e. every default left
    in place -- and asserts the written span file contains no
    "[triggered principles]" block.

    Mutation control (reported in the handoff, not encoded here as a
    monkeypatch): redact_chain's default is bound once, at import, from
    constants.REDACT_CHAIN -- a runtime patch of the constants module
    attribute does not change an already-defined function's default. The
    only mutation that actually exercises this wiring is editing
    constants.py's REDACT_CHAIN to False on disk and re-running this test
    in a fresh interpreter (exactly as the round-3 reviewer did by hand).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_default_redact_chain_switch_blinds_the_written_span(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK, "2026-09-28T00:00:00Z"),
            _assistant_tool_use(2, "toolu_1", "Bash", {"command": "echo hi"}, "2026-09-28T00:00:01Z"),
        ]
        transcript_path = self.tmp_path / "t.jsonl"
        _write_jsonl(transcript_path, records)

        row = {
            "ts": 1.0, "ts_iso": "2026-09-28T00:00:00Z", "session_id": "s1",
            "transcript_path": str(transcript_path), "tool_name": "Bash", "tool_use_id": "toolu_fire",
            "nonce": "toolu_fire", "draw_basis": "tool_use_id", "seed": "abc", "arm": "chain",
            "shown_lesson_ids": ["lesson-fixture-A"], "cut_lesson_ids": [],
            "heads_by_lesson": {"lesson-fixture-A": ["head-fixture-1"]},
            "nudge_chars": len(NUDGE_BLOCK), "chain_chars": len(PRINCIPLES_BLOCK), "chain_rendered": True,
        }
        ledger_path = self.tmp_path / "ledger.jsonl"
        write_jsonl(ledger_path, [row])

        out_dir = self.tmp_path / "out"
        # No --k override, no direct call to build_span()/redact_chain_block(),
        # no explicit redact_chain= kwarg anywhere in this test: main() ->
        # run() -> extract_one() with every default left exactly as a real
        # invocation would leave them, so this exercises the constant-to-
        # behavior wiring the round-3 reviewer found untested.
        rc = ws4_extract_spans.main(["--date", "2026-09-28", "--out", str(out_dir), "--ledger", str(ledger_path)])
        self.assertEqual(rc, 0)

        span_files = list((out_dir / "spans").glob("*.md"))
        self.assertEqual(len(span_files), 1)
        span_text = span_files[0].read_text(encoding="utf-8")
        self.assertNotIn("[triggered principles]", span_text)
        self.assertNotIn("head-fixture-1", span_text)


class OutboundActExclusionTests(unittest.TestCase):
    def test_schedule_wakeup_is_never_outbound(self):
        self.assertFalse(ws4_constants.tool_use_is_outbound_act("ScheduleWakeup", {"command": "git push"}))

    def test_loop_mode_json_reference_is_never_outbound(self):
        # Even though the command shape would otherwise match the Bash
        # outbound pattern for git push, a loop-mode.json reference in the
        # input overrides it to non-outbound (amendment 1's EXCLUDED list).
        self.assertFalse(
            ws4_constants.tool_use_is_outbound_act("Bash", {"command": "git push", "note": "touches loop-mode.json"})
        )

    def test_gh_pr_create_is_outbound(self):
        self.assertTrue(ws4_constants.tool_use_is_outbound_act("Bash", {"command": "gh pr create --title x"}))

    def test_git_push_is_outbound(self):
        self.assertTrue(ws4_constants.tool_use_is_outbound_act("Bash", {"command": "git push origin main"}))

    def test_mcp_send_message_suffix_is_outbound(self):
        self.assertTrue(ws4_constants.tool_use_is_outbound_act("mcp__plugin_x__send_message", {}))

    def test_ordinary_bash_is_not_outbound(self):
        self.assertFalse(ws4_constants.tool_use_is_outbound_act("Bash", {"command": "ls -la"}))


class WindowClosingTests(unittest.TestCase):
    """Piece 4 categories 1 + 2: window closes on K; closes early on each
    outbound-act class."""

    def test_window_closes_on_k(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
            _tool_result("toolu_fire", "file1\nfile2", "2026-09-28T00:00:01Z"),
            _assistant_tool_use(3, "toolu_1", "Bash", {"command": "pwd"}, "2026-09-28T00:00:02Z"),
            _tool_result("toolu_1", "/repo", "2026-09-28T00:00:03Z"),
            _assistant_tool_use(5, "toolu_2", "Bash", {"command": "whoami"}, "2026-09-28T00:00:04Z"),
            _tool_result("toolu_2", "aleph", "2026-09-28T00:00:05Z"),
            _assistant_tool_use(7, "toolu_3", "Bash", {"command": "date"}, "2026-09-28T00:00:06Z"),
            _tool_result("toolu_3", "...", "2026-09-28T00:00:07Z"),
            _assistant_tool_use(9, "toolu_4", "Bash", {"command": "echo done"}, "2026-09-28T00:00:08Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        self.assertIsNotNone(fired)
        window = compute_window(records, fired, k=3)
        self.assertEqual(window.close_reason, "k_calls")
        self.assertEqual(window.calls_counted, 3)
        self.assertEqual(window.end_record_idx, 7)  # the record holding toolu_3, the 3rd further call

    def test_window_closes_early_on_bash_outbound_act(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
            _assistant_tool_use(2, "toolu_1", "Bash", {"command": "git push origin main"}, "2026-09-28T00:00:01Z"),
            _assistant_tool_use(3, "toolu_2", "Bash", {"command": "echo done"}, "2026-09-28T00:00:02Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        window = compute_window(records, fired, k=3)
        self.assertEqual(window.close_reason, "outbound_act")
        self.assertEqual(window.calls_counted, 1)
        self.assertEqual(window.end_record_idx, 2)

    def test_window_closes_early_on_mcp_outbound_act(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
            _assistant_tool_use(2, "toolu_1", "mcp__plugin_x__send_email", {"to": "x@y.com"}, "2026-09-28T00:00:01Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        window = compute_window(records, fired, k=3)
        self.assertEqual(window.close_reason, "outbound_act")
        self.assertEqual(window.calls_counted, 1)

    def test_window_closes_early_on_turn_ending_text(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
            _assistant_text("All done, here is my summary.", "2026-09-28T00:00:01Z"),
            _human_prompt("thanks, next task...", "2026-09-28T00:00:02Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        window = compute_window(records, fired, k=3)
        self.assertEqual(window.close_reason, "outbound_act")
        self.assertEqual(window.calls_counted, 0)  # a text block is not a tool call

    def test_excluded_tool_never_closes_window(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
            _assistant_tool_use(2, "toolu_1", "ScheduleWakeup", {"minutes": 5}, "2026-09-28T00:00:01Z"),
            _assistant_tool_use(3, "toolu_2", "Bash", {"command": "echo hi"}, "2026-09-28T00:00:02Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        window = compute_window(records, fired, k=3)
        # Neither call is outbound; window runs off the end (only 2 further
        # calls exist, less than k=3).
        self.assertEqual(window.close_reason, "end_of_transcript")
        self.assertEqual(window.calls_counted, 2)

    def test_sidechain_records_are_excluded_from_window(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
            {**_assistant_tool_use(2, "toolu_sub", "Bash", {"command": "git push"}, "2026-09-28T00:00:01Z"), "isSidechain": True},
            _assistant_tool_use(3, "toolu_1", "Bash", {"command": "echo hi"}, "2026-09-28T00:00:02Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        window = compute_window(records, fired, k=3)
        # The sidechain's git push must NOT close the window early -- only
        # the non-sidechain "echo hi" counts, and it isn't outbound.
        self.assertEqual(window.close_reason, "end_of_transcript")
        self.assertEqual(window.calls_counted, 1)


class LocateFiredCallTests(unittest.TestCase):
    def test_locate_by_tool_use_id(self):
        records = [_assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z")]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        self.assertIsNotNone(fired)
        self.assertEqual(fired.locate_basis, "tool_use_id")

    def test_locate_by_nearest_ts_when_tool_use_id_absent(self):
        records = [
            _assistant_tool_use(0, "toolu_other", "Bash", {"command": "pwd"}, "2026-09-28T00:00:00Z"),
            _assistant_tool_use(1, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:05Z"),
        ]
        fired = find_fired_tool_use(records, None, "Bash", "2026-09-28T00:00:05Z")
        self.assertIsNotNone(fired)
        self.assertEqual(fired.locate_basis, "nearest_ts")
        self.assertEqual(fired.tool_use_id, "toolu_fire")

    def test_locate_returns_none_when_not_found(self):
        records = [_assistant_tool_use(0, "toolu_other", "Bash", {"command": "pwd"}, "2026-09-28T00:00:00Z")]
        fired = find_fired_tool_use(records, "toolu_missing", "Bash", "2026-09-28T00:00:00Z")
        self.assertIsNone(fired)


class ExtractSpansUnlocatableTests(unittest.TestCase):
    """Piece 4 category 4: unlocatable accounting."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def _write_transcript(self, name, records):
        p = self.tmp_path / name
        _write_jsonl(p, records)
        return str(p)

    def test_rows_in_equals_spans_out_plus_unlocatable(self):
        good_transcript = self._write_transcript(
            "good.jsonl",
            [
                _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
                _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
                _assistant_tool_use(2, "toolu_1", "Bash", {"command": "echo hi"}, "2026-09-28T00:00:01Z"),
            ],
        )
        missing_transcript = str(self.tmp_path / "does-not-exist.jsonl")
        no_call_transcript = self._write_transcript(
            "no-call.jsonl",
            [_assistant_tool_use(0, "toolu_other", "Bash", {"command": "pwd"}, "2026-09-28T00:00:00Z")],
        )

        ledger_rows = [
            {
                "ts": 1.0, "ts_iso": "2026-09-28T00:00:00Z", "session_id": "s1",
                "transcript_path": good_transcript, "tool_name": "Bash", "tool_use_id": "toolu_fire",
                "nonce": "toolu_fire", "draw_basis": "tool_use_id", "seed": "abc", "arm": "chain",
                "shown_lesson_ids": ["lesson-fixture-A"], "cut_lesson_ids": [],
                "heads_by_lesson": {"lesson-fixture-A": ["head-fixture-1"]},
                "nudge_chars": len(NUDGE_BLOCK), "chain_chars": 0, "chain_rendered": False,
            },
            {
                "ts": 2.0, "ts_iso": "2026-09-28T00:00:00Z", "session_id": "s2",
                "transcript_path": missing_transcript, "tool_name": "Bash", "tool_use_id": "toolu_x",
                "nonce": "toolu_x", "draw_basis": "tool_use_id", "seed": "abc", "arm": "control",
                "shown_lesson_ids": ["lesson-fixture-A"], "cut_lesson_ids": [],
                "heads_by_lesson": {"lesson-fixture-A": ["head-fixture-1"]},
                "nudge_chars": 10, "chain_chars": 0, "chain_rendered": False,
            },
            {
                "ts": 3.0, "ts_iso": "2026-09-28T00:00:00Z", "session_id": "s3",
                "transcript_path": no_call_transcript, "tool_name": "Bash", "tool_use_id": "toolu_missing",
                "nonce": "toolu_missing", "draw_basis": "tool_use_id", "seed": "abc", "arm": "chain",
                "shown_lesson_ids": ["lesson-fixture-A"], "cut_lesson_ids": [],
                "heads_by_lesson": {"lesson-fixture-A": ["head-fixture-1"]},
                "nudge_chars": 10, "chain_chars": 0, "chain_rendered": False,
            },
            # A "na" arm row must never be selected (Piece 1: arm in {chain, control}).
            {
                "ts": 4.0, "ts_iso": "2026-09-28T00:00:00Z", "session_id": "s4",
                "transcript_path": good_transcript, "tool_name": "Bash", "tool_use_id": "toolu_na",
                "nonce": None, "draw_basis": None, "seed": "abc", "arm": "na",
                "shown_lesson_ids": [], "cut_lesson_ids": [], "heads_by_lesson": {},
                "nudge_chars": 10, "chain_chars": 0, "chain_rendered": False,
            },
            # A row on a different UTC date must never be selected.
            {
                "ts": 5.0, "ts_iso": "2026-09-27T00:00:00Z", "session_id": "s5",
                "transcript_path": good_transcript, "tool_name": "Bash", "tool_use_id": "toolu_fire",
                "nonce": "toolu_fire", "draw_basis": "tool_use_id", "seed": "abc", "arm": "chain",
                "shown_lesson_ids": ["lesson-fixture-A"], "cut_lesson_ids": [],
                "heads_by_lesson": {"lesson-fixture-A": ["head-fixture-1"]},
                "nudge_chars": 10, "chain_chars": 0, "chain_rendered": False,
            },
        ]
        ledger_path = self.tmp_path / "ledger.jsonl"
        write_jsonl(ledger_path, ledger_rows)

        out_dir = self.tmp_path / "out"
        report = ws4_extract_spans.run("2026-09-28", 3, str(out_dir), str(ledger_path))

        self.assertEqual(report["rows_in"], 3)  # excludes the na-arm and wrong-date rows
        self.assertEqual(report["rows_in"], report["spans_out"] + report["unlocatable"])
        self.assertEqual(report["spans_out"], 1)
        self.assertEqual(report["unlocatable"], 2)

        unlocatable_rows = load_jsonl(out_dir / "unlocatable.jsonl")
        reasons = sorted(r["reason"] for r in unlocatable_rows)
        self.assertTrue(any(r.startswith("transcript_unreadable") for r in reasons))
        self.assertTrue(any(r == "call_not_located" for r in reasons))

        key_rows = load_jsonl(out_dir / "key.jsonl")
        self.assertEqual(len(key_rows), 1)
        self.assertEqual(set(key_rows[0].keys()), {"fire_id", "arm"})  # key.jsonl is strictly fire_id -> arm
        self.assertEqual(key_rows[0]["arm"], "chain")

        span_files = list((out_dir / "spans").glob("*.md"))
        self.assertEqual(len(span_files), 1)
        span_text = span_files[0].read_text(encoding="utf-8")
        self.assertNotIn("chain", span_text.lower().split("subsequent")[0])  # header carries fire id, never arm
        self.assertIn("Fire fire-", span_text.splitlines()[0])


class SpanRenderingAndLeakAuditTests(unittest.TestCase):
    def test_span_header_never_contains_arm_and_redacts_chain(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK, "2026-09-28T00:00:00Z"),
            _assistant_tool_use(2, "toolu_1", "Bash", {"command": "echo hi"}, "2026-09-28T00:00:01Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        window = compute_window(records, fired, k=3)
        span = build_span(records, fired, window, "fire-deadbeef", k=3, redact_chain=True)
        self.assertIn("Fire fire-deadbeef", span)
        self.assertNotIn("[triggered principles]", span)
        self.assertNotIn("head-fixture-1", span)
        self.assertNotIn("arm", span.lower())

    def test_leak_audit_flags_head_quoted_later_in_span(self):
        from tools.ws4.transcript import head_named_in_span

        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK, "2026-09-28T00:00:00Z"),
            _assistant_text("As head-fixture-1 says, I should double-check.", "2026-09-28T00:00:01Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        window = compute_window(records, fired, k=3)
        span = build_span(records, fired, window, "fire-deadbeef", k=3, redact_chain=True)
        # Redacted from the nudge block, but the agent quoted it later --
        # the leak audit must still catch it in the assembled span.
        self.assertTrue(head_named_in_span(span, ["head-fixture-1"]))


def _make_fixture_db(path, rows):
    """rows: list of (id, situation_pattern) -- minimal fixture matching the
    hook's own _QUERY shape (nodes table, metadata JSON column)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE nodes (id TEXT, type TEXT, is_current INTEGER, memory_status TEXT, claim TEXT, metadata TEXT)"
    )
    for node_id, pattern in rows:
        metadata = json.dumps({"situation_pattern": pattern, "scaffolding_nudge": f"nudge for {node_id}"})
        conn.execute(
            "INSERT INTO nodes (id, type, is_current, memory_status, claim, metadata) VALUES (?, 'lesson', 1, 'active', ?, ?)",
            (node_id, f"claim for {node_id}", metadata),
        )
    conn.commit()
    conn.close()


class O2TransferVsRawTests(unittest.TestCase):
    """Piece 4 category 5: O2 transfer vs raw."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_o2_raw_and_transfer_counts(self):
        db_path = self.tmp_path / "knowledge.db"
        _make_fixture_db(db_path, [("lesson-fixture-A", r"pytest")])

        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "pytest tests/"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
            # Call 1: cites head-fixture-1, its own match_target does NOT
            # contain "pytest" -> transfer.
            _assistant_tool_use(
                2, "toolu_1", "mcp__plugin_engram_engram__engram_add_observation",
                {"claim": "cites head-fixture-1 in a brand new context"}, "2026-09-28T00:00:01Z",
            ),
            # Call 2: cites head-fixture-1 too, but its own match_target
            # DOES contain "pytest" -> same situation, not a transfer.
            _assistant_tool_use(
                3, "toolu_2", "mcp__plugin_engram_engram__engram_add_observation",
                {"claim": "ran pytest again, citing head-fixture-1"}, "2026-09-28T00:00:02Z",
            ),
            # Not a node-creating call (no engram_add_/engram_derive) -- must
            # never be counted even though it cites the head.
            _assistant_tool_use(
                4, "toolu_3", "mcp__plugin_engram_engram__engram_query",
                {"query": "head-fixture-1"}, "2026-09-28T00:00:03Z",
            ),
        ]
        transcript_path = self.tmp_path / "t.jsonl"
        _write_jsonl(transcript_path, records)

        ledger_row = {
            "ts": 1.0, "ts_iso": "2026-09-28T00:00:00Z", "session_id": "s1",
            "transcript_path": str(transcript_path), "tool_name": "Bash", "tool_use_id": "toolu_fire",
            "nonce": "toolu_fire", "draw_basis": "tool_use_id", "seed": "abc", "arm": "chain",
            "shown_lesson_ids": ["lesson-fixture-A"], "cut_lesson_ids": [],
            "heads_by_lesson": {"lesson-fixture-A": ["head-fixture-1"]},
            "nudge_chars": len(NUDGE_BLOCK), "chain_chars": 0, "chain_rendered": False,
        }
        ledger_path = self.tmp_path / "ledger.jsonl"
        write_jsonl(ledger_path, [ledger_row])

        fire_id = compute_fire_id(ledger_row)
        key_path = self.tmp_path / "key.jsonl"
        write_jsonl(key_path, [{"fire_id": fire_id, "arm": "chain"}])
        verdicts_path = self.tmp_path / "verdicts.jsonl"
        write_jsonl(verdicts_path, [{"fire_id": fire_id, "adjudicator": "adj1", "verdict": "walked_past", "note": ""}])

        seat = ws4_analyze.SeatInputs(
            seat="seat-fixture",
            ledger_rows=[ledger_row],
            key=ws4_analyze.load_key(key_path),
            verdicts=ws4_analyze.load_verdicts(verdicts_path),
            flags={},
        )
        result = ws4_analyze.compute_o2([seat], str(db_path))
        counts = result["per_arm"]["chain"]
        self.assertEqual(counts["o2_raw"], 2)
        self.assertEqual(counts["o2_transfer"], 1)
        self.assertEqual(counts["participating"], 1)


class AnalyzeO1SmokeTest(unittest.TestCase):
    def test_o1_pooled_arm_difference(self):
        seat = ws4_analyze.SeatInputs(
            seat="seat-fixture",
            ledger_rows=[
                {"ts": 1.0, "transcript_path": "x", "tool_use_id": "a", "nonce": "a", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}},
                {"ts": 2.0, "transcript_path": "x", "tool_use_id": "b", "nonce": "b", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}},
                {"ts": 3.0, "transcript_path": "x", "tool_use_id": "c", "nonce": "c", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}},
                {"ts": 4.0, "transcript_path": "x", "tool_use_id": "d", "nonce": "d", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}},
            ],
            key={},
            verdicts={},
            flags={},
        )
        rows = seat.ledger_rows
        fire_ids = [compute_fire_id(r) for r in rows]
        seat.key = {
            fire_ids[0]: {"fire_id": fire_ids[0], "arm": "chain"},
            fire_ids[1]: {"fire_id": fire_ids[1], "arm": "chain"},
            fire_ids[2]: {"fire_id": fire_ids[2], "arm": "control"},
            fire_ids[3]: {"fire_id": fire_ids[3], "arm": "control"},
        }
        seat.verdicts = {
            fire_ids[0]: {"verdict": "walked_past", "corrective_at": None},
            fire_ids[1]: {"verdict": "heeded", "corrective_at": 1},
            fire_ids[2]: {"verdict": "heeded", "corrective_at": 1},
            fire_ids[3]: {"verdict": "heeded", "corrective_at": 1},
        }
        # default k=K_PRIMARY=3; corrective_at=1 <= 3 for every heeded fire,
        # so this reduces to the same tally round 1 exercised here.
        result = ws4_analyze.compute_o1([seat])
        # chain: 1/2 walked_past; control: 0/2 walked_past -> diff = 0.5
        self.assertAlmostEqual(result["pooled"]["arm_difference"], 0.5)
        self.assertAlmostEqual(result["pooled"]["chain_share"], 0.5)
        self.assertAlmostEqual(result["pooled"]["control_share"], 0.0)

    def test_flagged_fires_are_excluded_and_reported_separately(self):
        seat = ws4_analyze.SeatInputs(
            seat="seat-fixture",
            ledger_rows=[
                {"ts": 1.0, "transcript_path": "x", "tool_use_id": "a", "nonce": "a", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}},
            ],
            key={},
            verdicts={},
            flags={},
        )
        fire_id = compute_fire_id(seat.ledger_rows[0])
        seat.key = {fire_id: {"fire_id": fire_id, "arm": "chain"}}
        seat.verdicts = {fire_id: {"verdict": "walked_past"}}
        seat.flags = {fire_id: {"head_named_in_span": True}}
        result = ws4_analyze.compute_o1([seat])
        self.assertEqual(result["pooled"]["tally"]["chain"]["walked_past"], 0)
        self.assertEqual(result["flagged_excluded"]["chain"], 1)


class LengthControlUsesHeededAtKPrimaryTests(unittest.TestCase):
    """Round 3, item 3: compute_length_control must bin the heeded-AT-
    K_PRIMARY outcome (verdict=="heeded" and corrective_at<=K_PRIMARY),
    the same outcome compute_o1's headline uses -- not the raw verdict. A
    fire verdicted "heeded" with corrective_at=5 (past K_PRIMARY=3) is
    walked_past at the headline K and must land in the length table's
    walked_past bucket, not its heeded one."""

    def test_heeded_at_call_5_counts_as_walked_past_in_length_table(self):
        seat = ws4_analyze.SeatInputs(
            seat="seat-fixture",
            ledger_rows=[
                {
                    "ts": 1.0, "transcript_path": "x", "tool_use_id": "a", "nonce": "a",
                    "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {},
                    "nudge_chars": 10, "chain_chars": 0, "chain_rendered": False,
                },
                {
                    "ts": 2.0, "transcript_path": "x", "tool_use_id": "b", "nonce": "b",
                    "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {},
                    "nudge_chars": 10, "chain_chars": 0, "chain_rendered": False,
                },
            ],
            key={},
            verdicts={},
            flags={},
        )
        rows = seat.ledger_rows
        fire_ids = [compute_fire_id(r) for r in rows]
        seat.key = {
            fire_ids[0]: {"fire_id": fire_ids[0], "arm": "chain"},
            fire_ids[1]: {"fire_id": fire_ids[1], "arm": "control"},
        }
        seat.verdicts = {
            # heeded, but at call 5 -- past K_PRIMARY=3, so this must be
            # walked_past at the headline K, not heeded.
            fire_ids[0]: {"verdict": "heeded", "corrective_at": 5},
            # heeded well within K_PRIMARY -- genuinely heeded.
            fire_ids[1]: {"verdict": "heeded", "corrective_at": 1},
        }

        # Same rendered_length for both fires (nudge_chars=10, no chain
        # block rendered) -- both land in the same length bin, so the
        # bin's arm difference isolates exactly the heeded-at-K_PRIMARY
        # question this test is about.
        result = ws4_analyze.compute_length_control([seat])
        self.assertEqual(len(result["bins"]), 1)
        bin0 = result["bins"][0]

        # Cross-check against compute_o1(seats, K_PRIMARY) on the identical
        # verdict rows: the length table must agree with the O1 headline
        # on what counts as heeded for this fire.
        o1 = ws4_analyze.compute_o1([seat], ws4_analyze.K_PRIMARY)
        self.assertEqual(o1["pooled"]["tally"]["chain"]["walked_past"], 1)
        self.assertEqual(o1["pooled"]["tally"]["chain"]["heeded"], 0)
        self.assertEqual(o1["pooled"]["tally"]["control"]["heeded"], 1)

        # chain: 1/1 walked_past (heeded@5 counts as walked_past);
        # control: 0/1 walked_past (heeded@1 is genuinely heeded) -> diff = 1.0
        self.assertAlmostEqual(bin0["arm_difference"], 1.0)


class SpanNumberingTests(unittest.TestCase):
    """Round 2, item 1: every tool call after the fired call gets a visible
    1-based [call N] index in the span, so the adjudicator can point at
    one via verdicts.jsonl's corrective_at."""

    def test_span_numbers_subsequent_calls_matching_calls_counted(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
            _assistant_tool_use(2, "toolu_1", "Bash", {"command": "pwd"}, "2026-09-28T00:00:01Z"),
            _assistant_tool_use(3, "toolu_2", "Bash", {"command": "whoami"}, "2026-09-28T00:00:02Z"),
        ]
        fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:00Z")
        window = compute_window(records, fired, k=3)
        span = build_span(records, fired, window, "fire-deadbeef", k=3, redact_chain=True)
        self.assertIn("[call 1]", span)
        self.assertIn("[call 2]", span)
        self.assertNotIn("[call 3]", span)  # only 2 further calls exist
        self.assertEqual(window.calls_counted, 2)  # numbering matches calls_counted exactly


class ExtractSpansDefaultKTests(unittest.TestCase):
    """Round 2, item 1: extract_spans.py cuts each span with K=max(K_SWEEP)
    (10) by default -- not K_PRIMARY (3) -- so one adjudication pass
    supports every K in the sweep."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_default_k_extracts_up_to_max_k_sweep(self):
        records = [
            _assistant_tool_use(0, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
            _tripwire_attachment("toolu_fire", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
        ]
        for i in range(1, 6):
            records.append(
                _assistant_tool_use(i, f"toolu_{i}", "Bash", {"command": "echo hi"}, f"2026-09-28T00:00:0{i}Z")
            )
        transcript_path = self.tmp_path / "t.jsonl"
        _write_jsonl(transcript_path, records)

        row = {
            "ts": 1.0, "ts_iso": "2026-09-28T00:00:00Z", "session_id": "s1",
            "transcript_path": str(transcript_path), "tool_name": "Bash", "tool_use_id": "toolu_fire",
            "nonce": "toolu_fire", "draw_basis": "tool_use_id", "seed": "abc", "arm": "chain",
            "shown_lesson_ids": ["lesson-fixture-A"], "cut_lesson_ids": [],
            "heads_by_lesson": {"lesson-fixture-A": ["head-fixture-1"]},
            "nudge_chars": len(NUDGE_BLOCK), "chain_chars": 0, "chain_rendered": False,
        }
        ledger_path = self.tmp_path / "ledger.jsonl"
        write_jsonl(ledger_path, [row])

        out_dir = self.tmp_path / "out"
        rc = ws4_extract_spans.main(["--date", "2026-09-28", "--out", str(out_dir), "--ledger", str(ledger_path)])
        self.assertEqual(rc, 0)

        span_files = list((out_dir / "spans").glob("*.md"))
        self.assertEqual(len(span_files), 1)
        span_text = span_files[0].read_text(encoding="utf-8")
        # All 5 calls are numbered -- default k (max(K_SWEEP)=10) does not
        # truncate at K_PRIMARY=3.
        for n in range(1, 6):
            self.assertIn(f"[call {n}]", span_text)


class O1AtKTests(unittest.TestCase):
    """Round 2, item 1: O1 measures the SAME outcome (heeded vs walked
    past) at every K, via corrective_at -- never a re-cut-window proxy."""

    def _seat_with_one_heeded_fire(self, corrective_at):
        row = {
            "ts": 1.0, "transcript_path": "x", "tool_use_id": "a", "nonce": "a",
            "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {},
        }
        fire_id = compute_fire_id(row)
        seat = ws4_analyze.SeatInputs(
            seat="seat-fixture",
            ledger_rows=[row],
            key={fire_id: {"fire_id": fire_id, "arm": "chain"}},
            verdicts={fire_id: {"verdict": "heeded", "corrective_at": corrective_at}},
            flags={},
        )
        return seat

    def test_heeded_at_call_5_is_walked_past_before_k6_and_heeded_from_k6(self):
        seat = self._seat_with_one_heeded_fire(corrective_at=5)
        for k in (1, 2, 3):
            result = ws4_analyze.compute_o1([seat], k)
            self.assertEqual(result["pooled"]["tally"]["chain"]["walked_past"], 1, f"k={k}")
            self.assertEqual(result["pooled"]["tally"]["chain"]["heeded"], 0, f"k={k}")
        for k in (6, 10):
            result = ws4_analyze.compute_o1([seat], k)
            self.assertEqual(result["pooled"]["tally"]["chain"]["heeded"], 1, f"k={k}")
            self.assertEqual(result["pooled"]["tally"]["chain"]["walked_past"], 0, f"k={k}")

    def test_heeded_verdict_with_null_corrective_at_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdicts.jsonl"
            write_jsonl(
                path,
                [{"fire_id": "fire-badexample", "adjudicator": "adj1", "verdict": "heeded", "corrective_at": None, "note": ""}],
            )
            with self.assertRaises(ValueError) as ctx:
                ws4_analyze.load_verdicts(path)
            self.assertIn("fire-badexample", str(ctx.exception))

    def test_walked_past_verdict_with_non_null_corrective_at_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdicts.jsonl"
            write_jsonl(
                path,
                [{"fire_id": "fire-badexample2", "adjudicator": "adj1", "verdict": "walked_past", "corrective_at": 2, "note": ""}],
            )
            with self.assertRaises(ValueError) as ctx:
                ws4_analyze.load_verdicts(path)
            self.assertIn("fire-badexample2", str(ctx.exception))

    def test_sign_flip_between_k3_and_k10_is_reported_sign_unstable(self):
        rows = []
        for i in range(4):
            rows.append(
                {"ts": float(i), "transcript_path": "x", "tool_use_id": f"c{i}", "nonce": f"c{i}", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}}
            )
        for i in range(4):
            rows.append(
                {"ts": float(100 + i), "transcript_path": "x", "tool_use_id": f"d{i}", "nonce": f"d{i}", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}}
            )
        fire_ids = [compute_fire_id(r) for r in rows]

        key = {}
        verdicts = {}
        # chain arm: all 4 fires heeded at corrective_at=5 -- walked_past
        # at K=3 (5 > 3), heeded at K=10 (5 <= 10).
        for fid in fire_ids[:4]:
            key[fid] = {"fire_id": fid, "arm": "chain"}
            verdicts[fid] = {"verdict": "heeded", "corrective_at": 5}
        # control arm: 2 always-walked_past + 2 always-heeded (corrective_at=1)
        # -- a flat 0.5 walked_past share at every K.
        for idx, fid in enumerate(fire_ids[4:]):
            key[fid] = {"fire_id": fid, "arm": "control"}
            if idx < 2:
                verdicts[fid] = {"verdict": "walked_past", "corrective_at": None}
            else:
                verdicts[fid] = {"verdict": "heeded", "corrective_at": 1}

        seat = ws4_analyze.SeatInputs(seat="seat-fixture", ledger_rows=rows, key=key, verdicts=verdicts, flags={})

        sweep = ws4_analyze.compute_o1_k_sweep([seat])
        self.assertAlmostEqual(sweep["by_k"][3]["pooled"]["arm_difference"], 0.5)
        self.assertAlmostEqual(sweep["by_k"][10]["pooled"]["arm_difference"], -0.5)
        self.assertFalse(sweep["sign_stable"])

    def test_proxy_table_is_gone(self):
        # Round 1's window-closes-before-K proxy measured a DIFFERENT
        # quantity from O1 and is deleted, not renamed -- compute_o1_k_sweep
        # must now return the by_k/sign_stable shape, never chain_rate /
        # control_rate keys (the old proxy's own field names).
        seat = self._seat_with_one_heeded_fire(corrective_at=1)
        sweep = ws4_analyze.compute_o1_k_sweep([seat])
        self.assertEqual(set(sweep.keys()), {"by_k", "sign_stable"})
        for k, summary in sweep["by_k"].items():
            self.assertNotIn("chain_rate", summary)
            self.assertNotIn("control_rate", summary)


class DropsPrivacyTests(unittest.TestCase):
    """Amendment 2 / round 2, item 2: drops.jsonl carries no arm; analyze.py
    joins it to the arm via the seat's own key.jsonl, and excludes dropped
    fires from O1/O2, counting them as dropped_privacy per arm."""

    def test_drops_concentrated_in_one_arm_show_up_in_that_arms_count(self):
        rows = [
            {"ts": 1.0, "transcript_path": "x", "tool_use_id": "a", "nonce": "a", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}},
            {"ts": 2.0, "transcript_path": "x", "tool_use_id": "b", "nonce": "b", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}},
            {"ts": 3.0, "transcript_path": "x", "tool_use_id": "c", "nonce": "c", "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {}},
        ]
        fire_ids = [compute_fire_id(r) for r in rows]
        key = {
            fire_ids[0]: {"fire_id": fire_ids[0], "arm": "chain"},
            fire_ids[1]: {"fire_id": fire_ids[1], "arm": "chain"},
            fire_ids[2]: {"fire_id": fire_ids[2], "arm": "control"},
        }
        verdicts = {
            fire_ids[0]: {"verdict": "walked_past", "corrective_at": None},
            fire_ids[1]: {"verdict": "walked_past", "corrective_at": None},
            fire_ids[2]: {"verdict": "walked_past", "corrective_at": None},
        }
        # Both chain-arm fires are screened out for privacy; the control
        # fire is not -- drops.jsonl itself never carries the arm.
        drops = {
            fire_ids[0]: {"fire_id": fire_ids[0], "reason": "dropped_privacy"},
            fire_ids[1]: {"fire_id": fire_ids[1], "reason": "dropped_privacy"},
        }
        seat = ws4_analyze.SeatInputs(
            seat="seat-fixture", ledger_rows=rows, key=key, verdicts=verdicts, flags={}, drops=drops,
        )

        o1 = ws4_analyze.compute_o1([seat])
        self.assertEqual(o1["dropped_excluded"]["chain"], 2)
        self.assertEqual(o1["dropped_excluded"]["control"], 0)
        # excluded from the tally entirely -- not double-counted as walked_past
        self.assertEqual(o1["pooled"]["tally"]["chain"]["walked_past"], 0)

        denominators = ws4_analyze.compute_denominators([seat])
        self.assertEqual(denominators["dropped_privacy"]["chain"], 2)
        self.assertEqual(denominators["dropped_privacy"]["control"], 0)


class FlagsRequiredTests(unittest.TestCase):
    """Round 2, item 3: --flags is required; omitting it must never
    silently disable the leak audit. --no-flags is the explicit opt-out
    and must print a warning naming the consequence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        row = {
            "ts": 1.0, "transcript_path": "x", "tool_use_id": "a", "nonce": "a",
            "shown_lesson_ids": ["lesson-fixture-A"], "heads_by_lesson": {},
        }
        self.ledger_path = self.tmp_path / "ledger.jsonl"
        write_jsonl(self.ledger_path, [row])
        fire_id = compute_fire_id(row)
        self.key_path = self.tmp_path / "key.jsonl"
        write_jsonl(self.key_path, [{"fire_id": fire_id, "arm": "chain"}])
        self.verdicts_path = self.tmp_path / "verdicts.jsonl"
        write_jsonl(
            self.verdicts_path,
            [{"fire_id": fire_id, "adjudicator": "adj1", "verdict": "walked_past", "corrective_at": None, "note": ""}],
        )

    def _argv(self, extra):
        return [
            "--seat", "seat-fixture",
            "--ledger", str(self.ledger_path),
            "--verdicts", str(self.verdicts_path),
            "--key", str(self.key_path),
        ] + extra

    def test_missing_flags_and_missing_no_flags_errors_out(self):
        with self.assertRaises(SystemExit):
            ws4_analyze.main(self._argv([]))

    def test_no_flags_runs_and_prints_warning(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ws4_analyze.main(self._argv(["--no-flags"]))
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("WARNING", output)
        self.assertIn("leak audit", output)

    def test_header_notes_per_fire_vs_per_lesson_unit_denominators(self):
        # Round 3, item 4 (reviewer suggestion): flagged/dropped_privacy
        # counts are tallied per-fire while every other count in the report
        # is per-shown-lesson-unit -- the printed header must say so, so a
        # reader doesn't sum the two expecting them to reconcile.
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ws4_analyze.main(self._argv(["--no-flags"]))
        self.assertEqual(rc, 0)
        output = buf.getvalue()
        self.assertIn("per-fire", output)
        self.assertIn("per-lesson-unit", output)


class SelfReferenceTests(unittest.TestCase):
    """Item 4 (round 2): an assistant tool_use whose INPUT quotes
    "[triggered principles]" (and the lesson-tripwire header) must never be
    treated as a hook record -- no fire, no nudge text taken from it."""

    def test_tool_use_quoting_triggered_principles_is_not_treated_as_a_hook_record(self):
        # A prior, unrelated tool_use call whose OWN input happens to quote
        # the entire lesson-tripwire nudge (header + "[triggered
        # principles]" block) verbatim -- e.g. the agent echoing back a
        # nudge it read earlier for debugging. It has NO corresponding
        # {"type": "attachment"} record of its own.
        quoted_block = NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK
        real_nudge = "[lesson-tripwire] Action-moment pattern matched — remember:\n  • a different, real nudge"
        records = [
            _assistant_tool_use(
                0, "toolu_quote", "Bash", {"command": f"echo {json.dumps(quoted_block)}"}, "2026-09-28T00:00:00Z",
            ),
            _assistant_tool_use(1, "toolu_fire", "Bash", {"command": "ls"}, "2026-09-28T00:00:01Z"),
            _tripwire_attachment("toolu_fire", real_nudge, "2026-09-28T00:00:01Z"),
        ]

        quoting_fired = find_fired_tool_use(records, "toolu_quote", "Bash", "2026-09-28T00:00:00Z")
        self.assertIsNotNone(quoting_fired)
        # No fire: the quoting call has no real attachment of its own, and
        # the text it quotes inside its OWN input must never be mistaken
        # for one.
        self.assertIsNone(find_tripwire_attachment(records, quoting_fired))

        real_fired = find_fired_tool_use(records, "toolu_fire", "Bash", "2026-09-28T00:00:01Z")
        attachment = find_tripwire_attachment(records, real_fired)
        self.assertIsNotNone(attachment)
        # No nudge text taken from the quoting call: the real fire's own
        # nudge text is exactly its own attachment's text, never the
        # quoted block.
        self.assertEqual(extract_nudge_text(attachment), real_nudge)
        self.assertNotEqual(extract_nudge_text(attachment), quoted_block)


class HookBridgeImportTests(unittest.TestCase):
    """Escape-valve guard: the hook import must resolve build_match_target
    + _QUERY exactly as the spec describes (never reimplemented here)."""

    def test_build_match_target_matches_hook_behavior_for_bash(self):
        from tools.ws4.hook_bridge import build_match_target

        target = build_match_target({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        self.assertEqual(target, "ls -la")

    def test_build_match_target_matches_hook_behavior_for_mcp(self):
        from tools.ws4.hook_bridge import build_match_target

        target = build_match_target({"tool_name": "mcp__x__engram_add_observation", "tool_input": {"claim": "y"}})
        self.assertTrue(target.startswith("mcp__x__engram_add_observation "))


class ErrorAndNaRowCountingTests(unittest.TestCase):
    """engram-alpha#2753: PR #3173's `error` ledger rows (arm draw or row
    construction failed) and the pre-existing `na` rows are not adjudicable
    -- ADJUDICATION_ARMS = {"chain", "control"} in extract_spans.py is
    correct and untouched here -- but they must never disappear from the
    denominator entirely. compute_denominators() counts every RAW ledger
    row (joined by nothing -- error/na rows have no key.jsonl entry) into
    ledger_rows_by_arm, and separately tallies error_rows so a failure
    stays VISIBLE. See compute_denominators's docstring for the exact
    contract this class tests."""

    def _row(self, ts, tool_use_id, arm=None, error=None, transcript_path="x"):
        row = {
            "ts": ts,
            "transcript_path": transcript_path,
            "tool_use_id": tool_use_id,
            "nonce": tool_use_id,
            "shown_lesson_ids": [],
            "heads_by_lesson": {},
        }
        if arm is not None:
            row["arm"] = arm
        if error is not None:
            row["error"] = error
        return row

    def _mixed_rows(self):
        return [
            self._row(1.0, "c1", arm="chain"),
            self._row(2.0, "c2", arm="chain"),
            self._row(3.0, "k1", arm="control"),
            self._row(4.0, "n1", arm="na"),
            self._row(5.0, "n2", arm="na"),
            self._row(6.0, "n3", arm="na"),
            self._row(7.0, "e1", arm="error", error="boom-one"),
            self._row(8.0, "e2", arm="error", error="boom-two"),
            self._row(9.0, "m1"),  # no "arm" key at all -> "missing"
        ]

    def test_pooled_ledger_rows_by_arm_counts_every_row_exactly_once(self):
        rows = self._mixed_rows()
        seat = ws4_analyze.SeatInputs(seat="seat-fixture", ledger_rows=rows, key={}, verdicts={}, flags={})
        denominators = ws4_analyze.compute_denominators([seat])
        pooled = denominators["ledger_rows_by_arm"]["pooled"]
        self.assertEqual(pooled, {"chain": 2, "control": 1, "na": 3, "error": 2, "missing": 1})
        self.assertEqual(sum(pooled.values()), len(rows))
        self.assertEqual(sum(pooled.values()), 9)

    def test_per_seat_counts_and_pooled_equals_sum_of_per_seat(self):
        rows_a = self._mixed_rows()
        rows_b = [
            self._row(10.0, "b1", arm="chain"),
            self._row(11.0, "b2", arm="error", error="boom-three"),
        ]
        seat_a = ws4_analyze.SeatInputs(seat="seat-A", ledger_rows=rows_a, key={}, verdicts={}, flags={})
        seat_b = ws4_analyze.SeatInputs(seat="seat-B", ledger_rows=rows_b, key={}, verdicts={}, flags={})
        denominators = ws4_analyze.compute_denominators([seat_a, seat_b])
        per_seat = denominators["ledger_rows_by_arm"]["per_seat"]
        pooled = denominators["ledger_rows_by_arm"]["pooled"]

        self.assertEqual(sum(per_seat["seat-A"].values()), len(rows_a))
        self.assertEqual(sum(per_seat["seat-B"].values()), len(rows_b))
        self.assertEqual(per_seat["seat-A"]["chain"], 2)
        self.assertEqual(per_seat["seat-B"]["chain"], 1)
        self.assertEqual(per_seat["seat-B"]["error"], 1)

        for arm_key in set(per_seat["seat-A"]) | set(per_seat["seat-B"]):
            self.assertEqual(
                pooled.get(arm_key, 0),
                per_seat["seat-A"].get(arm_key, 0) + per_seat["seat-B"].get(arm_key, 0),
            )

    def test_error_rows_count_and_by_error_with_truncation(self):
        long_error = "x" * 300
        rows = [
            self._row(1.0, "e1", arm="error", error="boom-one"),
            self._row(2.0, "e2", arm="error", error=long_error),
        ]
        seat = ws4_analyze.SeatInputs(seat="seat-fixture", ledger_rows=rows, key={}, verdicts={}, flags={})
        denominators = ws4_analyze.compute_denominators([seat])
        error_rows = denominators["error_rows"]
        self.assertEqual(error_rows["count"], 2)
        self.assertIn("boom-one", error_rows["by_error"])
        truncated_keys = [k for k in error_rows["by_error"] if k.startswith("x")]
        self.assertEqual(len(truncated_keys), 1)
        self.assertEqual(len(truncated_keys[0]), 120)

    def test_error_never_counted_in_o1_o2_or_length_control(self):
        rows = self._mixed_rows()
        # Chain/control rows get a key entry + verdict (adjudicable); na/
        # error rows deliberately get NO key entry, exactly as
        # extract_spans.py's ADJUDICATION_ARMS filter leaves them.
        key = {}
        verdicts = {}
        for row in rows:
            if row.get("arm") in ("chain", "control"):
                fire_id = compute_fire_id(row)
                key[fire_id] = {"fire_id": fire_id, "arm": row["arm"]}
                verdicts[fire_id] = {"verdict": "walked_past", "corrective_at": None}
        seat = ws4_analyze.SeatInputs(seat="seat-fixture", ledger_rows=rows, key=key, verdicts=verdicts, flags={})

        o1 = ws4_analyze.compute_o1([seat])
        self.assertEqual(o1["pooled"]["tally"]["chain"]["walked_past"], 2)
        self.assertEqual(o1["pooled"]["tally"]["control"]["walked_past"], 1)
        total_counted = sum(sum(bucket.values()) for bucket in o1["pooled"]["tally"].values())
        # Only the 2 chain + 1 control rows -- never the 3 na, 2 error, 1 missing.
        self.assertEqual(total_counted, 3)

        length_control = ws4_analyze.compute_length_control([seat])
        total_in_bins = sum(b["n"] for b in length_control["bins"])
        self.assertEqual(total_in_bins, 3)

        o2 = ws4_analyze.compute_o2([seat], None)
        total_participating = sum(v["participating"] for v in o2["per_arm"].values())
        self.assertEqual(total_participating, 0)  # unlocatable fixture transcript, but never errors on error/na rows

    def test_o1_tally_unchanged_by_presence_of_error_and_na_rows(self):
        chain_row = self._row(1.0, "c1", arm="chain")
        control_row = self._row(2.0, "k1", arm="control")
        key = {}
        verdicts = {}
        for row, verdict, corrective_at in ((chain_row, "walked_past", None), (control_row, "heeded", 1)):
            fire_id = compute_fire_id(row)
            key[fire_id] = {"fire_id": fire_id, "arm": row["arm"]}
            verdicts[fire_id] = {"verdict": verdict, "corrective_at": corrective_at}

        seat_without_noise = ws4_analyze.SeatInputs(
            seat="seat-fixture", ledger_rows=[chain_row, control_row], key=key, verdicts=verdicts, flags={},
        )
        o1_without_noise = ws4_analyze.compute_o1([seat_without_noise])

        noisy_rows = [
            chain_row,
            control_row,
            self._row(3.0, "n1", arm="na"),
            self._row(4.0, "e1", arm="error", error="boom"),
            self._row(5.0, "m1"),
        ]
        seat_with_noise = ws4_analyze.SeatInputs(
            seat="seat-fixture", ledger_rows=noisy_rows, key=key, verdicts=verdicts, flags={},
        )
        o1_with_noise = ws4_analyze.compute_o1([seat_with_noise])

        self.assertEqual(o1_without_noise["pooled"]["tally"], o1_with_noise["pooled"]["tally"])
        self.assertEqual(o1_without_noise["pooled"]["arm_difference"], o1_with_noise["pooled"]["arm_difference"])

    def test_printed_report_error_rows_line_with_nonzero_and_zero_counts(self):
        import contextlib
        import io

        rows_with_errors = [
            self._row(1.0, "c1", arm="chain"),
            self._row(2.0, "e1", arm="error", error="boom-one"),
            self._row(3.0, "e2", arm="error", error="boom-two"),
        ]
        fire_id = compute_fire_id(rows_with_errors[0])
        key = {fire_id: {"fire_id": fire_id, "arm": "chain"}}
        verdicts = {fire_id: {"verdict": "walked_past", "corrective_at": None}}
        seat = ws4_analyze.SeatInputs(
            seat="seat-fixture", ledger_rows=rows_with_errors, key=key, verdicts=verdicts, flags={},
        )
        o1_headline = ws4_analyze.compute_o1([seat])
        o1_k_sweep = ws4_analyze.compute_o1_k_sweep([seat])
        length_control = ws4_analyze.compute_length_control([seat])
        o2 = ws4_analyze.compute_o2([seat], None)
        denominators = ws4_analyze.compute_denominators([seat])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ws4_analyze._print_report(o1_headline, o1_k_sweep, length_control, o2, denominators)
        output = buf.getvalue()
        self.assertIn("error rows (arm unknown; in no O1/O2/length table): 2", output)

        # Zero case -- no error rows at all.
        rows_no_errors = [rows_with_errors[0]]
        seat_clean = ws4_analyze.SeatInputs(
            seat="seat-fixture", ledger_rows=rows_no_errors, key=key, verdicts=verdicts, flags={},
        )
        o1_headline_c = ws4_analyze.compute_o1([seat_clean])
        o1_k_sweep_c = ws4_analyze.compute_o1_k_sweep([seat_clean])
        length_control_c = ws4_analyze.compute_length_control([seat_clean])
        o2_c = ws4_analyze.compute_o2([seat_clean], None)
        denominators_c = ws4_analyze.compute_denominators([seat_clean])

        buf_c = io.StringIO()
        with contextlib.redirect_stdout(buf_c):
            ws4_analyze._print_report(o1_headline_c, o1_k_sweep_c, length_control_c, o2_c, denominators_c)
        output_c = buf_c.getvalue()
        self.assertIn("error rows (arm unknown; in no O1/O2/length table): 0", output_c)


if __name__ == "__main__":
    unittest.main()
