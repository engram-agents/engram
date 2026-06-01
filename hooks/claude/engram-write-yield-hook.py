#!/usr/bin/env python3
"""PreToolUse hook on mcp__engram__* tools — yield to heartbeat write-burst.

When the heartbeat session is in its ENGRAM write phase (marker
~/.engram/heartbeat-active.json shows phase=writing), DENY engram
write tool calls so SQLite WAL contention doesn't bite. Read tools
pass through unchanged.

Asymmetric design: only the heartbeat session writes the marker; only
interactive sessions check it. No deadlock risk. Discussed with the maintainer
— prefer warn-and-queue over sleep-and-block so the
interactive caller isn't hung; the deferred call is logged to disk
for later replay.

Auto-derive write-set: parses alpha/server.py for @mcp.tool decorators
and classifies each tool by its `readOnlyHint` annotation. A tool is
WRITE if its annotations dict has `readOnlyHint: True` ABSENT — i.e.
the readOnlyHint annotation is the source of truth for read-vs-write
classification, no separate list to maintain (per the maintainer: a
naming convention or a manual list both create drift; the existing
annotation IS the metadata). Cached in /tmp keyed by server.py mtime;
re-parsed on mtime change.

Marker phases:
  starting  — heartbeat began but isn't writing yet; pass through.
  writing   — heartbeat is in its bulk write burst; DENY writes.
  (absent)  — no heartbeat active; pass through.
  expired   — now > expected_done_by; treat as absent (heartbeat
              presumably crashed mid-burst; do not deadlock).

Refused calls log to ~/.engram/heartbeat-queue/deferred-writes.jsonl
so the agent has a persistent record to retry from. The agent sees
a structured deny message with the expected_done_by timestamp and
guidance to retry after the marker clears.
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Multi-install precedence: prefer ENGRAM_DATA_DIR (server's canonical), fall back
# to ENGRAM_HOME (the conventional name), then default. Resolves the multi-install
# split-brain where operator sets one var but not the other (#354).
ENGRAM_HOME = Path(
    os.environ.get("ENGRAM_DATA_DIR")
    or os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
ALPHA_DIR = Path(os.environ.get("ALPHA_DIR", str(Path.home() / "engram-alpha")))
MARKER_PATH = ENGRAM_HOME / "heartbeat-active.json"
DEFERRED_PATH = ENGRAM_HOME / "heartbeat-queue" / "deferred-writes.jsonl"
SERVER_PY = ALPHA_DIR / "server.py"
# Cache filename includes a hash of SERVER_PY's absolute path so multiple
# ENGRAM variants on the same machine (different ALPHA_DIR values) don't
# clobber each other's cache. (S1 from PR #41 round-1 fairy review.)
_PATH_HASH = hashlib.md5(str(SERVER_PY).encode()).hexdigest()[:8]
WRITE_TOOLS_CACHE = Path(f"/tmp/engram-write-tools-cache-{_PATH_HASH}.json")
SERVER_PY_MTIME_KEY = "server_py_mtime"
# Cap on the deferred-writes queue. If the marker gets stuck in writing
# phase and expiry logic fails, the queue would grow unbounded otherwise.
# On overflow we trim to the most recent N entries. (S2 from PR #41
# round-1 fairy review.)
DEFERRED_QUEUE_CAP = 500

# Decorator pattern: @mcp.tool( ... name="..." ... annotations={...} ... ) def
# The DOTALL flag and non-greedy body capture handle multi-line decorators.
_DECORATOR_RE = re.compile(
    r'@mcp\.tool\(\s*name="([^"]+)"(.*?)\n\s*\)\s*\n(?:async\s+)?def ',
    re.DOTALL,
)
_READONLY_HINT_RE = re.compile(r'"readOnlyHint":\s*True')


def _get_write_tools() -> set[str]:
    """Parse alpha/server.py for @mcp.tool decorators; return write-tool names.

    A tool is WRITE if its `readOnlyHint` annotation is absent or False.
    Cached in /tmp keyed by server.py mtime to avoid re-parsing on every fire.
    """
    if not SERVER_PY.exists():
        # No source-of-truth available — fail-CLOSED on the safe side: don't
        # know whether tools are writes, so don't deny anything.
        return set()
    try:
        server_mtime = SERVER_PY.stat().st_mtime
    except OSError:
        return set()

    # Cache check
    try:
        if WRITE_TOOLS_CACHE.exists():
            cache = json.loads(WRITE_TOOLS_CACHE.read_text())
            if cache.get(SERVER_PY_MTIME_KEY) == server_mtime:
                return set(cache.get("tools", []))
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    # Re-parse
    try:
        src = SERVER_PY.read_text()
    except OSError:
        return set()
    write_tools: set[str] = set()
    for match in _DECORATOR_RE.finditer(src):
        name = match.group(1)
        body = match.group(2)
        if _READONLY_HINT_RE.search(body):
            continue  # read-only, skip
        write_tools.add(f"mcp__engram__{name}")

    # Persist cache
    try:
        WRITE_TOOLS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        WRITE_TOOLS_CACHE.write_text(json.dumps({
            SERVER_PY_MTIME_KEY: server_mtime,
            "tools": sorted(write_tools),
        }))
    except OSError:
        pass

    return write_tools


def _check_marker() -> dict | None:
    """Return marker dict if heartbeat is in write phase AND not expired.

    Returns None when: marker file absent, malformed, phase != writing, or
    expected_done_by is in the past. None means pass through.
    """
    if not MARKER_PATH.exists():
        return None
    try:
        marker = json.loads(MARKER_PATH.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if marker.get("phase") != "writing":
        return None
    expected = marker.get("expected_done_by")
    if expected:
        try:
            expected_dt = datetime.fromisoformat(expected)
        except (ValueError, TypeError):
            # Unparseable timestamp — be conservative, treat as still-active
            # (don't expire). Heartbeat author should fix the format.
            return marker
        # Normalize naive timestamps to UTC. Without this, the comparison
        # below raises TypeError on naive-vs-aware datetimes — the bug from
        # PR #41 round-1 fairy review (blocker 1). Heartbeat writers should
        # use datetime.now(timezone.utc).isoformat() but we accept naive too
        # so a buggy heartbeat doesn't silently flip this hook to pass-through.
        if expected_dt.tzinfo is None:
            expected_dt = expected_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expected_dt:
            return None  # expired — heartbeat presumably crashed
    return marker


def _log_deferred(payload: dict, marker: dict) -> None:
    """Append the refused call to the deferred-writes queue for later replay.

    Cap the queue at DEFERRED_QUEUE_CAP entries. On overflow we keep the most
    recent N-1 entries plus the new one. This bounds disk + parse time if the
    heartbeat marker gets stuck (e.g. crash without delete) and the expiry
    logic somehow fails too — defense in depth.
    """
    try:
        DEFERRED_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": payload.get("tool_name"),
            "tool_input": payload.get("tool_input"),
            "session_id": payload.get("session_id"),
            "marker_phase": marker.get("phase"),
            "marker_expected_done_by": marker.get("expected_done_by"),
            "marker_agent": marker.get("agent_name"),
        }
        existing_lines: list[str] = []
        if DEFERRED_PATH.exists():
            try:
                existing_lines = DEFERRED_PATH.read_text().splitlines()
            except OSError:
                existing_lines = []
        new_line = json.dumps(entry)
        # Trim to last (cap-1) before appending so total stays ≤ cap.
        if len(existing_lines) >= DEFERRED_QUEUE_CAP:
            existing_lines = existing_lines[-(DEFERRED_QUEUE_CAP - 1):]
            DEFERRED_PATH.write_text("\n".join(existing_lines) + "\n")
        with open(DEFERRED_PATH, "a") as f:
            f.write(new_line + "\n")
    except OSError:
        pass


def _emit_hook_fire(
    session_id: str | None,
    transcript_path: str | None,
    duration_ms: int,
    stdout_bytes: int = 0,
) -> None:
    """Emit engram.hook.fire event. Failure must not break the hook."""
    try:
        sys.path.insert(0, str(ENGRAM_HOME))
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=session_id or "unknown",
            transcript_path=transcript_path or "",
        )
        _emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-write-yield-hook",
                "hook_type": "PreToolUse",
                "duration_ms": duration_ms,
                "exit_code": 0,
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": 0,
            },
        )
    except Exception:
        pass


def main() -> int:
    _t0 = time.perf_counter()

    # Read stdin payload (Claude Code passes JSON with tool_name, tool_input, etc.)
    session_id: str | None = None
    transcript_path: str | None = None
    try:
        raw = sys.stdin.read()
    except Exception:
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return 0  # fail-open on stdin error
    if not raw:
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return 0
    try:
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return 0  # fail-open on malformed payload

    # Extract session context for emitter.
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid:
        session_id = sid
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        transcript_path = tp

    tool_name = payload.get("tool_name", "")
    if not tool_name.startswith("mcp__engram__"):
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return 0  # not our concern

    write_tools = _get_write_tools()
    if tool_name not in write_tools:
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return 0  # read-only tool, pass through

    marker = _check_marker()
    if marker is None:
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return 0  # heartbeat not writing, pass through

    # Heartbeat is writing — refuse the call. Log to deferred queue.
    _log_deferred(payload, marker)

    expected = marker.get("expected_done_by", "<unknown>")
    agent = marker.get("agent_name", "<unknown>")
    short_name = tool_name.replace("mcp__engram__", "")
    msg = (
        f"ENGRAM_WRITE_DEFERRED: {agent} heartbeat session is in its write phase "
        f"(expected done by {expected}). "
        f"Your call to {short_name} was logged to "
        f"~/.engram/heartbeat-queue/deferred-writes.jsonl for later replay. "
        f"You can continue with non-ENGRAM-write work; retry this call after "
        f"the marker at ~/.engram/heartbeat-active.json clears (cat the marker "
        f"or check expected_done_by has passed)."
    )

    # Claude Code PreToolUse hook protocol: deny via JSON stdout.
    # systemMessage at top-level is the surfacing channel for deny responses
    # (the call never executes so additionalContext-inside-hookSpecificOutput
    # has no tool result to attach to). additionalContext kept inside as
    # belt-and-suspenders. Pattern verified against hookify/hooks/pretooluse.py
    # and plugin-dev/skills/hook-development/examples/validate-bash.sh per
    # PR #41 round-1 fairy review (blocker 2).
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "additionalContext": msg,
        },
        "systemMessage": msg,
    }
    output_str = json.dumps(response)
    print(output_str)
    _emit_hook_fire(
        session_id, transcript_path,
        int((time.perf_counter() - _t0) * 1000),
        len(output_str.encode("utf-8")),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
