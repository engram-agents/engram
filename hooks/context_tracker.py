#!/usr/bin/env python3
"""
ENGRAM Context Tracker — Drowsiness Monitor

Measures context usage from per-message token counts in the Claude Code
session JSONL, and calibrates the ceiling from auto-compaction samples.

Algorithm:
  ceiling = mean(last N compactMetadata.preTokens where trigger == "auto") * SAFETY_FACTOR
  current = (input + cache_read + cache_create) from last assistant usage record
  pct     = current / ceiling

Only auto-trigger samples calibrate the ceiling — manual compactions
reflect when the agent voluntarily napped, not where the harness cuts
off, and mixing them compounds downward bias (ob_1003). Falls back to
FALLBACK_CEILING when fewer than two auto samples are available across
the project's JSONL files.

Stateless: no baseline file, no byte-to-token ratio, no cached state.
Every call reads the JSONL fresh.
"""

import glob
import json
import os

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram"))
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")  # legacy; unused
ACTIVE_SESSION_PATH = os.path.join(ENGRAM_HOME, "active-session.json")
LOOP_MARKER_PATH = os.path.join(ENGRAM_HOME, "loop-mode.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")

SAFETY_FACTOR = 0.95            # Report 100% at 95% of mean auto ceiling
AUTO_SAMPLE_WINDOW = 5          # Average of last N auto compactions
FALLBACK_CEILING = 160_000      # Used when < 2 auto samples found
TAIL_SCAN_BYTES = 5_000_000     # How far back to look for the latest usage

DROWSY_THRESHOLD = 0.50
VERY_DROWSY_THRESHOLD = 0.70


def find_active_jsonl() -> str | None:
    """Return the current session's transcript path, or None.

    Reads $ENGRAM_HOME/active-session.json, written by the SessionStart hook.
    Returns None if the marker is missing or the transcript file doesn't
    exist yet — the correct behavior on turn 1 of a fresh conversation,
    before Claude Code has flushed the new JSONL. No mtime fallback:
    picking newest-by-mtime globally caused stale drowsiness to leak
    across conversations on turn 1.
    """
    try:
        with open(ACTIVE_SESSION_PATH) as f:
            marker = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    path = marker.get("transcript_path")
    if not path or not os.path.exists(path):
        return None
    return path


def _project_jsonls() -> list[str]:
    """All JSONL files in the active project dir, newest first."""
    active = find_active_jsonl()
    if not active:
        return []
    project_dir = os.path.dirname(active)
    files = glob.glob(os.path.join(project_dir, "*.jsonl"))
    return sorted(files, key=os.path.getmtime, reverse=True)


def collect_auto_pretokens(max_samples: int = AUTO_SAMPLE_WINDOW) -> list[int]:
    """Most recent auto-compaction preTokens in chronological order."""
    collected: list[int] = []
    for path in _project_jsonls():  # newest file first
        file_samples: list[int] = []
        try:
            with open(path, "rb") as f:
                for line in f:
                    if b"compact_boundary" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    meta = obj.get("compactMetadata") or {}
                    if meta.get("trigger") != "auto":
                        continue
                    pt = meta.get("preTokens")
                    if isinstance(pt, int) and pt > 0:
                        file_samples.append(pt)
        except OSError:
            continue
        # Older file → older samples → prepend.
        collected = file_samples + collected
        if len(collected) >= max_samples:
            break
    return collected[-max_samples:]


def compute_ceiling() -> int:
    samples = collect_auto_pretokens()
    if len(samples) < 2:
        return int(FALLBACK_CEILING * SAFETY_FACTOR)
    mean = sum(samples) // len(samples)
    return int(mean * SAFETY_FACTOR)


def read_current_tokens() -> int | None:
    """Input-token count of the most recent post-compact assistant usage record.

    Iterates the tail in reverse. If a compact_boundary event appears before
    any usage record, returns None — the pre-compact usage records that remain
    in the JSONL no longer describe current state, and reporting them produced
    a bogus ~78% drowsiness spike in the turn between /compact and the first
    post-compact assistant message.
    """
    jsonl_path = find_active_jsonl()
    if not jsonl_path:
        return None
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(TAIL_SCAN_BYTES, size)
            f.seek(size - read_size)
            tail = f.read()
    except OSError:
        return None

    for line in reversed(tail.split(b"\n")):
        if b'"compact_boundary"' in line:
            return None
        if b'"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        if not usage:
            continue
        total = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
        )
        if total > 0:
            return total
    return None


def estimate_usage() -> dict | None:
    current = read_current_tokens()
    if current is None:
        return None
    ceiling = compute_ceiling()
    if ceiling <= 0:
        return None
    return {
        "estimated_tokens": current,
        "context_limit": ceiling,
        "usage_pct": current / ceiling,
    }


def write_baseline(use_compact_boundary: bool = True) -> None:
    """No-op — retained so session-start and post-compact hooks still import.

    The stateless tracker recomputes on every call; there is no baseline to
    write. Remove the import from the hooks when convenient.
    """
    return


def format_drowsiness(usage: dict) -> str:
    """Generate drowsiness warning. Returns empty string if alert.

    In loop mode ($ENGRAM_HOME/loop-mode.json present), warnings are suppressed;
    the percentage is still reported but nap/stop directives are not issued.
    """
    pct = usage["usage_pct"]
    est_tokens = usage["estimated_tokens"]
    limit = usage["context_limit"]

    loop_mode = os.path.exists(LOOP_MARKER_PATH)

    if loop_mode:
        return f"[Drowsiness: {pct:.0%} — loop mode, compaction will auto-fire]"

    if pct >= VERY_DROWSY_THRESHOLD:
        return (
            f"[Drowsiness: {pct:.0%} — very drowsy, nap now]\n"
            f"  (~{est_tokens:,} / {limit:,} tokens)\n"
            "  You're close to compaction. Stop current work and consolidate:\n"
            f"  1. Write key observations and derivations to ENGRAM\n"
            f"  2. Update your warm briefing ({WARM_BRIEFING_PATH})\n"
            "  3. Run engram_checkpoint(mode='nap')\n"
            "  Tell the user: \"I'm very drowsy — let me take a nap to consolidate.\""
        )
    elif pct >= DROWSY_THRESHOLD:
        return (
            f"[Drowsiness: {pct:.0%} — getting drowsy]\n"
            f"  (~{est_tokens:,} / {limit:,} tokens)\n"
            "  Keep writing to ENGRAM. Plan a nap soon.\n"
            "  Record important decisions and observations before context fills."
        )
    return f"[Drowsiness: {pct:.0%} — not drowsy]"
