#!/usr/bin/env python3
"""Post-compact: write the last-compact-at marker so the context tracker can
correctly compute drowsiness against the post-compact byte-offset.

Background. Claude Code writes a `compact_boundary` JSONL entry (with full
compactMetadata: preTokens, postTokens, durationMs) at /compact time; the
context_tracker scans the JSONL for that marker to locate the post-compact
window. The marker was transiently absent ~2026-05-07 but is confirmed back
in live sessions (live-verified 2026-06-09). The PostCompact hook writes the
JSONL byte offset at compact time into ~/.engram/last-compact-at.json as a
correctness backstop — for sessions where compact_boundary is absent or
stale the tracker falls back to the marker; for normal sessions it is a
belt-and-suspenders efficiency hedge (avoids rescanning from the top).

Also surfaces starred inter-agent letters as concise pointers in
additionalContext so load-bearing cross-session agreements survive the
experiential reset a compaction represents.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from context_tracker import find_active_jsonl

# Inter-agent dir (mirrors ia.py convention)
INTER_AGENT_DIR = os.environ.get("INTER_AGENT_DIR", "/home/agents-shared/inter-agent")
STARRED_CAP = 10
STARRED_STALE_DAYS = 7  # soft TTL for staleness nudge (nudge only, never auto-drop)


def starred_block(engram_home: str) -> str:
    """Render starred inter-agent letters as concise pointers.

    One line per entry: ⭐ [<from>] "<title>" — ia read <filename>
    Stars older than STARRED_STALE_DAYS get a trailing nudge: ⚠ stale Nd — unstar if resolved
    Reads from/title from snapshot fields stored at star-time — does NOT re-open or re-parse
    the source letter. If the source letter was deleted, still renders from the snapshot.
    Returns empty string when the list is empty, the file is missing, or any
    read error occurs. Never raises — hook must not block post-compact.
    """
    try:
        starred_path = os.path.join(engram_home, "inter-agent-starred.json")
        try:
            raw = Path(starred_path).read_text(encoding="utf-8")
            entries = json.loads(raw)
            if not isinstance(entries, list):
                return ""
        except (OSError, json.JSONDecodeError, ValueError):
            return ""

        if not entries:
            return ""

        now = datetime.now(timezone.utc)
        lines = []
        skipped = 0
        for entry in entries:
            filename = entry.get("filename", "").strip()
            if not filename:
                skipped += 1
                continue

            # Read from/title from snapshot; graceful fallback for old entries lacking snapshot fields
            from_agent = (entry.get("from") or "").strip() or "unknown"
            title = (entry.get("title") or "").strip() or "(no title)"
            note = entry.get("note", "").strip()
            note_part = f" — {note}" if note else ""

            # Staleness nudge: compute age from starred_at
            stale_part = ""
            starred_at_str = entry.get("starred_at", "")
            if starred_at_str:
                try:
                    starred_dt = datetime.strptime(starred_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    age_days = (now - starred_dt).days
                    if age_days >= STARRED_STALE_DAYS:
                        stale_part = f"  ⚠ stale {age_days}d — unstar if resolved"
                except ValueError:
                    pass

            lines.append(f"  ⭐ [{from_agent}] \"{title}\" — ia read {filename}{note_part}{stale_part}")

        if not lines:
            return ""

        total = len(lines)
        display_lines = lines[:STARRED_CAP]
        remaining = total - len(display_lines)
        header = f"⭐ {total} starred letter(s) — key cross-session context, re-read if relevant:"
        block_lines = [header] + display_lines
        if remaining > 0:
            block_lines.append(f"  ... +{remaining} more (ia starred to see all)")
        if skipped > 0:
            block_lines.append(
                f"  (note: {skipped} starred entry/entries skipped (missing filename field))"
            )
        return "\n".join(block_lines)
    except Exception:
        # Hook discipline: never surface starred-block errors at post-compact.
        return ""

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)
MARKER_PATH = os.path.join(ENGRAM_HOME, "last-compact-at.json")


def _emit_hook_fire(session_id: str, transcript_path: str, duration_ms: int) -> None:
    """Emit engram.hook.fire event. Failure must not break the hook."""
    try:
        sys.path.insert(0, ENGRAM_HOME)
        from engram_log_emitter import Emitter
        _emitter = Emitter.init(
            session_id=session_id or "unknown",
            transcript_path=transcript_path or "",
        )
        _emitter.emit(
            event_type="engram.hook.fire",
            level=1,
            data={
                "hook_name": "engram-postcompact-hook",
                "hook_type": "PostCompact",
                "duration_ms": duration_ms,
                "exit_code": 0,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
            },
        )
    except Exception:
        pass


def main() -> None:
    _t0 = time.perf_counter()

    # Issue #140: read session_id + transcript_path from this hook's stdin
    # payload to resolve THIS session's JSONL race-free, instead of reading
    # a single shared global marker.
    session_id: str | None = None
    transcript_path: str | None = None
    try:
        raw = sys.stdin.read()
        if raw:
            payload = json.loads(raw)
            sid = payload.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
            tp = payload.get("transcript_path")
            if isinstance(tp, str) and tp:
                transcript_path = tp
    except (ValueError, json.JSONDecodeError, OSError):
        pass

    if transcript_path and os.path.exists(transcript_path):
        jsonl_path = transcript_path
    else:
        jsonl_path = find_active_jsonl(session_id)
    if not jsonl_path or not os.path.exists(jsonl_path):
        # No active JSONL to mark — defensive no-op.
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return
    try:
        byte_offset = os.path.getsize(jsonl_path)
    except OSError:
        _emit_hook_fire(session_id, transcript_path, int((time.perf_counter() - _t0) * 1000))
        return
    marker = {
        "jsonl_path": jsonl_path,
        "byte_offset": byte_offset,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with open(MARKER_PATH, "w") as f:
            json.dump(marker, f)
    except OSError:
        pass

    # Starred letters surface: emit as additionalContext so load-bearing
    # cross-session agreements survive the experiential reset. Silent skip on
    # any failure — hook must not block post-compact.
    context_lines = []
    try:
        starred = starred_block(ENGRAM_HOME)
        if starred:
            context_lines.append(starred)
    except Exception:
        pass

    _duration_ms = int((time.perf_counter() - _t0) * 1000)

    if context_lines:
        output_obj = {
            "hookSpecificOutput": {
                "hookEventName": "PostCompact",
                "additionalContext": "\n".join(context_lines),
            }
        }
        output_str = json.dumps(output_obj)
        print(output_str)
        _emit_hook_fire(session_id, transcript_path, _duration_ms)
    else:
        _emit_hook_fire(session_id, transcript_path, _duration_ms)


if __name__ == "__main__":
    main()
