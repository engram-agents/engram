"""Tests for tools/enumerate_armed_monitors.py — engram-alpha#3049.

Covers:

  1. A normal Monitor arm (tool_use name=="Monitor" + matching tool_result)
     is found, with its task_id extracted correctly.
  2. Multiple distinct arms are all found, in first-seen order.
  3. Self-contamination mode 1: a DIFFERENT tool's tool_result (e.g. an
     engram_inspect result) whose text happens to quote a prior arm line
     is NOT matched — only a real Monitor tool_use's own tool_result counts.
  4. Self-contamination mode 2: this script's own prior printed output,
     now sitting in the transcript as a Bash tool_result, is NOT matched
     on a second pass — no substring grep ever runs.
  5. A re-armed id (same task_id text appearing twice, from two separate
     real Monitor arms) is de-duplicated, preserving first-seen order.
  6. Malformed / non-JSON lines in the transcript are skipped without
     raising.
  7. An empty transcript (or one with zero Monitor arms) returns [].
  8. A Monitor tool_use with no matching tool_result yet (arm result not
     yet delivered) contributes nothing — never a false positive.
  9. CLI: prints one id per line by default, a JSON array with --json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.enumerate_armed_monitors import find_armed_monitor_task_ids  # noqa: E402


def _tool_use_record(tool_use_id: str, name: str, input_: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": name,
                    "input": input_ or {},
                }
            ],
        },
    }


def _tool_result_record(tool_use_id: str, content: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            ],
        },
    }


def _write_transcript(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "transcript.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return p


def _real_arm(tool_use_id: str, task_id: str, description: str = "test monitor") -> list[dict]:
    """A real Monitor arm: tool_use(name=Monitor) + its own tool_result."""
    return [
        _tool_use_record(tool_use_id, "Monitor", {"description": description}),
        _tool_result_record(
            tool_use_id,
            f"Monitor started (task {task_id}, expires in 30m unless the source "
            "ends first; you get one notice at expiry — re-arm if you still "
            "need the watch). You will be notified on each event.",
        ),
    ]


def test_single_real_arm_is_found(tmp_path):
    records = _real_arm("toolu_001", "abc123")
    path = _write_transcript(tmp_path, records)
    assert find_armed_monitor_task_ids(path) == ["abc123"]


def test_list_shaped_tool_result_content_is_found(tmp_path):
    """engram-alpha#3050 review round 1: tool_result.content appears in
    production as EITHER a bare string OR a list of typed block dicts (the
    same ambiguity _stophooklib.walk_content_blocks() normalizes for the
    Stop-hook family) -- a prior version of this tool hard-required a bare
    string and silently dropped any arm whose result was list-shaped."""
    records = [
        _tool_use_record("toolu_001", "Monitor", {"description": "test monitor"}),
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_001",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Monitor started (task listshape1, expires "
                                    "in 30m unless the source ends first)."
                                ),
                            }
                        ],
                    }
                ],
            },
        },
    ]
    path = _write_transcript(tmp_path, records)
    assert find_armed_monitor_task_ids(path) == ["listshape1"]


def test_multiple_arms_found_in_order(tmp_path):
    records = (
        _real_arm("toolu_001", "first111")
        + _real_arm("toolu_002", "second22")
        + _real_arm("toolu_003", "third333")
    )
    path = _write_transcript(tmp_path, records)
    assert find_armed_monitor_task_ids(path) == ["first111", "second22", "third333"]


def test_self_contamination_mode_1_engram_node_quoting_arm_line(tmp_path):
    """An engram_inspect result whose text quotes a prior arm line must not
    be read as a new arm — only a tool_use literally named "Monitor" counts."""
    records = _real_arm("toolu_001", "realarm1")
    # A DIFFERENT tool (engram_inspect) whose result text happens to contain
    # the exact phrase a Monitor arm produces, from a node's quoted_text.
    records += [
        _tool_use_record("toolu_fake", "engram_inspect", {"payload_json": "{}"}),
        _tool_result_record(
            "toolu_fake",
            json.dumps(
                {
                    "result": (
                        "{\"quoted_text\": \"Monitor started (task ghostbad1, "
                        "expires in 30m unless the source ends first)\"}"
                    )
                }
            ),
        ),
    ]
    path = _write_transcript(tmp_path, records)
    assert find_armed_monitor_task_ids(path) == ["realarm1"]


def test_self_contamination_mode_2_own_prior_output_not_matched(tmp_path):
    """This script's own prior stdout, now sitting in the transcript as a
    Bash tool_result, must not be re-matched as a new arm on a later parse.

    Uses a DISTINCT fabricated id (``bashecho1``) in the simulated echoed
    output rather than repeating the real arm's own id — a naive
    whole-file regex scan would find this second, bogus id and (wrongly)
    include it; only a structural per-record parse correctly ignores it."""
    records = _real_arm("toolu_001", "realarm2")
    # Simulate a prior run of this very script (invoked via Bash) whose
    # printed output — one CLI arg is itself the arm-shaped text a naive
    # grep would key on — is now transcript content under a Bash tool.
    records += [
        _tool_use_record("toolu_bash", "Bash", {"command": "echo 'Monitor started (task bashecho1,'"}),
        _tool_result_record("toolu_bash", "Monitor started (task bashecho1, expires in 30m)"),
    ]
    path = _write_transcript(tmp_path, records)
    # Only the real arm's id — the Bash tool_result is never treated as a
    # Monitor arm (name != "Monitor"), so the fabricated id inside it never
    # surfaces, unlike a whole-file substring scan would produce.
    assert find_armed_monitor_task_ids(path) == ["realarm2"]


def test_duplicate_task_id_deduplicated_preserving_order(tmp_path):
    records = _real_arm("toolu_001", "dupe0001") + _real_arm("toolu_002", "unique01") + _real_arm(
        "toolu_003", "dupe0001"
    )
    path = _write_transcript(tmp_path, records)
    assert find_armed_monitor_task_ids(path) == ["dupe0001", "unique01"]


def test_malformed_lines_are_skipped(tmp_path):
    p = tmp_path / "transcript.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        fh.write("not json at all {{{\n")
        fh.write("\n")  # blank line
        for r in _real_arm("toolu_001", "clean001"):
            fh.write(json.dumps(r) + "\n")
        fh.write('{"incomplete": \n')
    assert find_armed_monitor_task_ids(p) == ["clean001"]


def test_empty_transcript_returns_empty_list(tmp_path):
    p = tmp_path / "transcript.jsonl"
    p.write_text("", encoding="utf-8")
    assert find_armed_monitor_task_ids(p) == []


def test_no_monitor_arms_returns_empty_list(tmp_path):
    records = [
        _tool_use_record("toolu_x", "Bash", {"command": "echo hi"}),
        _tool_result_record("toolu_x", "hi"),
    ]
    path = _write_transcript(tmp_path, records)
    assert find_armed_monitor_task_ids(path) == []


def test_tool_use_without_matching_result_contributes_nothing(tmp_path):
    """A Monitor arm still in flight (no tool_result yet) must not appear —
    never a false positive from the request half alone."""
    records = [_tool_use_record("toolu_pending", "Monitor", {"description": "in flight"})]
    path = _write_transcript(tmp_path, records)
    assert find_armed_monitor_task_ids(path) == []


def test_cli_default_prints_one_id_per_line(tmp_path):
    records = _real_arm("toolu_001", "cliline1") + _real_arm("toolu_002", "cliline2")
    path = _write_transcript(tmp_path, records)
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "tools", "enumerate_armed_monitors.py"), str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == ["cliline1", "cliline2"]


def test_cli_json_flag_prints_json_array(tmp_path):
    records = _real_arm("toolu_001", "clijson1")
    path = _write_transcript(tmp_path, records)
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "tools", "enumerate_armed_monitors.py"), str(path), "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == ["clijson1"]
