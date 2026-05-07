"""Tests for engram-write-yield-hook.py — interactive yields to heartbeat write-burst.

Covers the four discrete logic paths plus the regression case from PR #41
round-1 review (TypeError on naive-vs-aware datetime). Each test invokes the
hook as a subprocess with a fake ENGRAM_HOME and ALPHA_DIR so the auto-derive
+ marker-check + log-write paths run end-to-end.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "hooks" / "claude" / "engram-write-yield-hook.py"


def _fake_server_py(write_tool_names: list[str], readonly_tool_names: list[str]) -> str:
    """Build a minimal server.py-like source string with given tool decorators."""
    lines = ["import json", ""]
    for name in write_tool_names:
        lines.extend([
            "@mcp.tool(",
            f'    name="{name}",',
            "    annotations={",
            f'        "title": "{name}",',
            '        "destructiveHint": True,',
            "    },",
            ")",
            f"def {name}(x: str) -> str:",
            "    return ''",
            "",
        ])
    for name in readonly_tool_names:
        lines.extend([
            "@mcp.tool(",
            f'    name="{name}",',
            "    annotations={",
            f'        "title": "{name}",',
            '        "readOnlyHint": True,',
            "    },",
            ")",
            f"def {name}(x: str) -> str:",
            "    return ''",
            "",
        ])
    return "\n".join(lines)


@pytest.fixture
def fake_dirs(tmp_path: Path):
    """Create fake ENGRAM_HOME + ALPHA_DIR with minimal server.py."""
    engram_home = tmp_path / "engram_home"
    alpha_dir = tmp_path / "alpha"
    engram_home.mkdir()
    alpha_dir.mkdir()
    server_py = alpha_dir / "server.py"
    server_py.write_text(_fake_server_py(
        write_tool_names=["engram_add_observation", "engram_supersede"],
        readonly_tool_names=["engram_query", "engram_inspect"],
    ))
    yield {
        "engram_home": engram_home,
        "alpha_dir": alpha_dir,
    }


def _run_hook(payload: dict, env_overrides: dict) -> tuple[int, str, str]:
    """Run the hook with given stdin payload + env, return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env.update(env_overrides)
    # NOTE: TMPDIR override below is benign no-op for the cache-isolation
    # purpose. The hook hardcodes WRITE_TOOLS_CACHE = Path("/tmp/...") — a
    # literal /tmp path, not tempfile.gettempdir() — so TMPDIR doesn't move
    # the cache. Cache isolation actually works because each test gets a
    # unique fake_dirs["alpha_dir"] (tmp_path-scoped) which produces a
    # distinct MD5 hash in the cache filename. Kept for parity in case the
    # hook ever moves to tempfile.gettempdir(). (PR #43 round-1 fairy nit.)
    env["TMPDIR"] = str(Path(env_overrides["ENGRAM_HOME"]).parent / "tmp")
    Path(env["TMPDIR"]).mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_marker(engram_home: Path, phase: str, expected_done_by: str | None) -> None:
    marker = {
        "agent_name": "borges",
        "phase": phase,
        "started_at": "2026-05-07T15:00:00+00:00",
        "pid": 12345,
    }
    if expected_done_by is not None:
        marker["expected_done_by"] = expected_done_by
    if phase == "writing":
        marker["writing_started_at"] = "2026-05-07T15:00:00+00:00"
    (engram_home / "heartbeat-active.json").write_text(json.dumps(marker))


def _env(fake_dirs) -> dict:
    return {
        "ENGRAM_HOME": str(fake_dirs["engram_home"]),
        "ALPHA_DIR": str(fake_dirs["alpha_dir"]),
    }


def test_pass_through_when_no_marker(fake_dirs):
    """Marker absent → silent pass-through (exit 0, no stdout)."""
    code, out, err = _run_hook(
        payload={"tool_name": "mcp__engram__engram_add_observation",
                 "tool_input": {"claim": "test"}},
        env_overrides=_env(fake_dirs),
    )
    assert code == 0
    assert out == ""


def test_pass_through_when_marker_phase_starting(fake_dirs):
    """phase=starting (heartbeat began but not writing yet) → pass-through."""
    _write_marker(fake_dirs["engram_home"], "starting",
                  (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())
    code, out, err = _run_hook(
        payload={"tool_name": "mcp__engram__engram_add_observation",
                 "tool_input": {"claim": "test"}},
        env_overrides=_env(fake_dirs),
    )
    assert code == 0
    assert out == ""


def test_pass_through_when_read_only_tool(fake_dirs):
    """Read-only tool + writing marker → pass-through (auto-derive excludes it)."""
    _write_marker(fake_dirs["engram_home"], "writing",
                  (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
    code, out, err = _run_hook(
        payload={"tool_name": "mcp__engram__engram_query",
                 "tool_input": {"query": "test"}},
        env_overrides=_env(fake_dirs),
    )
    assert code == 0
    assert out == ""


def test_deny_when_writing_aware_datetime(fake_dirs):
    """Write tool + writing marker + aware ISO timestamp → DENY with systemMessage."""
    _write_marker(fake_dirs["engram_home"], "writing",
                  (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
    code, out, err = _run_hook(
        payload={"tool_name": "mcp__engram__engram_add_observation",
                 "tool_input": {"claim": "test"},
                 "session_id": "abc"},
        env_overrides=_env(fake_dirs),
    )
    assert code == 0
    response = json.loads(out)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "systemMessage" in response  # top-level for visibility on deny
    assert "ENGRAM_WRITE_DEFERRED" in response["systemMessage"]


def test_deny_when_writing_naive_datetime_regression(fake_dirs):
    """Write tool + writing marker + NAIVE timestamp → DENY (regression for PR #41 r1 bug).

    Before the fix, naive datetime caused TypeError when comparing against
    aware datetime.now(timezone.utc), the exception propagated, hook printed
    traceback + exited 1, Claude Code treated as pass-through. Fix: normalize
    naive to UTC before comparing.
    """
    naive_ts = (datetime.now(timezone.utc) + timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    _write_marker(fake_dirs["engram_home"], "writing", naive_ts)
    code, out, err = _run_hook(
        payload={"tool_name": "mcp__engram__engram_add_observation",
                 "tool_input": {"claim": "test"}},
        env_overrides=_env(fake_dirs),
    )
    assert code == 0
    assert err == ""  # no Python traceback — the bug printed one
    response = json.loads(out)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pass_through_when_marker_expired(fake_dirs):
    """phase=writing but expected_done_by is in the past → treat as crashed, pass through."""
    past_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _write_marker(fake_dirs["engram_home"], "writing", past_ts)
    code, out, err = _run_hook(
        payload={"tool_name": "mcp__engram__engram_add_observation",
                 "tool_input": {"claim": "test"}},
        env_overrides=_env(fake_dirs),
    )
    assert code == 0
    assert out == ""  # passes through, no deny


def test_pass_through_when_marker_corrupt(fake_dirs):
    """Marker file with malformed JSON → defensive pass-through."""
    (fake_dirs["engram_home"] / "heartbeat-active.json").write_text("{not valid json")
    code, out, err = _run_hook(
        payload={"tool_name": "mcp__engram__engram_add_observation",
                 "tool_input": {"claim": "test"}},
        env_overrides=_env(fake_dirs),
    )
    assert code == 0
    assert out == ""


def test_deferred_writes_log_appended_on_deny(fake_dirs):
    """A denied call appends one entry to deferred-writes.jsonl with all 7 documented fields.

    Spec for queue entry: timestamp, tool_name, tool_input, session_id,
    marker_phase, marker_expected_done_by, marker_agent. Earlier version of
    this test only checked 4 of the 7 — the missing 3 (timestamp,
    marker_expected_done_by, marker_agent) are the ones that pull from the
    `marker` dict rather than the call payload, so they're the most likely
    to regress silently if `_log_deferred` ever changes shape. (PR #43
    round-1 fairy suggestion 1.)
    """
    expected_done_by = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    _write_marker(fake_dirs["engram_home"], "writing", expected_done_by)
    _run_hook(
        payload={"tool_name": "mcp__engram__engram_add_observation",
                 "tool_input": {"claim": "test"},
                 "session_id": "session-xyz"},
        env_overrides=_env(fake_dirs),
    )
    queue = fake_dirs["engram_home"] / "heartbeat-queue" / "deferred-writes.jsonl"
    assert queue.exists()
    entries = [json.loads(line) for line in queue.read_text().splitlines() if line]
    assert len(entries) == 1
    e = entries[0]
    # Payload-derived fields
    assert e["tool_name"] == "mcp__engram__engram_add_observation"
    assert e["tool_input"] == {"claim": "test"}
    assert e["session_id"] == "session-xyz"
    # Marker-derived fields (the silent-regression-risk set)
    assert e["marker_phase"] == "writing"
    assert e["marker_expected_done_by"] == expected_done_by
    assert e["marker_agent"] == "borges"
    # Hook-generated field
    assert "timestamp" in e
    # Verify it parses as a valid ISO datetime
    datetime.fromisoformat(e["timestamp"])


def test_deferred_queue_cap_trims_at_500(fake_dirs):
    """The DEFERRED_QUEUE_CAP=500 trim path: pre-seed 500 dummy entries, run
    one deny, assert exactly 500 lines remain with the new entry last.

    Directly exercises the trim-and-rewrite code path in `_log_deferred`
    (PR #41 suggestion S2 — defense against unbounded queue growth if the
    marker gets stuck and expiry logic fails). Without this test, the
    cap behavior is regression-fragile. (PR #43 round-1 fairy suggestion 5.)
    """
    queue_dir = fake_dirs["engram_home"] / "heartbeat-queue"
    queue_dir.mkdir()
    queue = queue_dir / "deferred-writes.jsonl"
    # Pre-seed 500 dummy lines (each with a marker so it's parseable JSON)
    seed_lines = [
        json.dumps({"timestamp": f"2026-01-01T00:00:{i:02d}Z",
                    "tool_name": "mcp__engram__engram_add_observation",
                    "tool_input": {"claim": f"seed-{i}"},
                    "session_id": "seed",
                    "marker_phase": "writing",
                    "marker_expected_done_by": "2026-01-01T00:00:00Z",
                    "marker_agent": "seed-agent"})
        for i in range(500)
    ]
    queue.write_text("\n".join(seed_lines) + "\n")

    _write_marker(fake_dirs["engram_home"], "writing",
                  (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
    _run_hook(
        payload={"tool_name": "mcp__engram__engram_add_observation",
                 "tool_input": {"claim": "the-new-one"},
                 "session_id": "fresh"},
        env_overrides=_env(fake_dirs),
    )
    final_lines = queue.read_text().splitlines()
    # Exactly 500 (cap); not 501 (no trim) and not 499 (over-trim)
    assert len(final_lines) == 500
    # The new entry is the last line
    last = json.loads(final_lines[-1])
    assert last["tool_input"] == {"claim": "the-new-one"}
    assert last["session_id"] == "fresh"
    # The first seed (i=0) was dropped to make room
    first = json.loads(final_lines[0])
    assert first["tool_input"] != {"claim": "seed-0"}
    assert first["tool_input"] == {"claim": "seed-1"}  # i=1 is now first


def test_pass_through_when_payload_not_engram(fake_dirs):
    """Non-engram tool name → silent pass-through, no marker check."""
    _write_marker(fake_dirs["engram_home"], "writing",
                  (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
    code, out, err = _run_hook(
        payload={"tool_name": "Bash", "tool_input": {"command": "ls"}},
        env_overrides=_env(fake_dirs),
    )
    assert code == 0
    assert out == ""


def test_pass_through_when_stdin_empty(fake_dirs):
    """Empty stdin → fail-open pass-through (don't block on hook errors)."""
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="",
        capture_output=True,
        text=True,
        env={**os.environ, **_env(fake_dirs)},
        timeout=5,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
