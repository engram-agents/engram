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
LAST_COMPACT_PATH = os.path.join(ENGRAM_HOME, "last-compact-at.json")
LOOP_MARKER_PATH = os.path.join(ENGRAM_HOME, "loop-mode.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")

SAFETY_FACTOR = 0.95            # Report 100% at 95% of mean auto ceiling
AUTO_SAMPLE_WINDOW = 5          # Average of last N auto compactions
FALLBACK_CEILING = 160_000      # Legacy alias; prefer FALLBACK_CEILING_BY_MODE
TAIL_SCAN_BYTES = 5_000_000     # How far back to look for the latest usage

# Drowsiness warning thresholds. Calibrated for both 200K and 1M context modes:
#   200k mode (ceiling ~152K after safety factor): 80% = 121K used, ~47K headroom
#                                                  90% = 137K used, ~31K headroom
#   1m mode   (ceiling ~807K after safety factor): 80% = 645K used, ~155K headroom
#                                                  90% = 726K used, ~80K headroom
# Earlier values (0.50 / 0.70) were calibrated when 200K was the only mode and
# fired far too early on 1M sessions — at 70% on 1M, 240K headroom remains, more
# than a full fresh 200K window. Lei's framing 2026-05-04: only fire warnings
# when truly close to compaction, not as a routine nudge.
DROWSY_THRESHOLD = 0.80
VERY_DROWSY_THRESHOLD = 0.90

# Two distinct context-window modes the harness supports:
#   "200k" — default Anthropic API context (auto preTokens cluster ~150-200K)
#   "1m"   — beta extended context (auto preTokens cluster ~800-1000K)
# These produce ~5x different ceilings; mixing samples in the mean gives a
# contaminated value and reports drowsiness wrong (ob_1473 — 2026-05-03 PM:
# three 170K + one 967K averaged into ~370K, reported 29% drowsy when truth
# was ~10%). The harness does not surface mode in JSONL message.model, so we
# detect from current input_tokens and partition samples by magnitude class.
SAMPLE_PARTITIONS = {
    "200k": (0, 300_000),
    "1m":   (300_000, 1_500_000),
}
FALLBACK_CEILING_BY_MODE = {
    "200k": 160_000,
    "1m":   850_000,
}
# 200k mode auto-compacts at ~160-165K preTokens. If current_tokens exceed
# this AND no compact_boundary has fired in the session JSONL, the session
# can only be 1m mode — 200k mode would have already compacted. Lowered
# 2026-05-06 from 300_000 after a 1m-mode session at 184K input_tokens
# was misclassified as 200k → reported 122% drowsy when truly at ~23%.
MODE_BOUNDARY = 175_000


def find_active_jsonl(session_id: str | None = None) -> str | None:
    """Return the transcript path for a given session_id, or None.

    Reads $ENGRAM_HOME/sessions/<session_id>.json (per-session marker,
    written by SessionStart hook). Issue #140 retired the global
    active-session.json marker that this replaced — each session now owns
    its own file by session_id, so concurrent sessions cannot clobber each
    other's transcript_path.

    Returns None when session_id is missing, the per-session file doesn't
    exist (turn-1 of a fresh conversation before SessionStart fires), or
    the transcript file itself doesn't exist yet. Callers MUST pass
    session_id — there is no global fallback.
    """
    if not session_id:
        return None
    marker_path = os.path.join(ENGRAM_HOME, "sessions", f"{session_id}.json")
    try:
        with open(marker_path) as f:
            marker = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    path = marker.get("transcript_path")
    if not path or not os.path.exists(path):
        return None
    return path


def _resolve_jsonl(transcript_path: str | None = None, session_id: str | None = None) -> str | None:
    """Resolution order: explicit transcript_path → per-session marker by id.

    Both arguments come from this hook's stdin payload (harness emits
    session_id + transcript_path uniformly). When the wrapper threads them
    in, every path lookup routes through the caller's own session —
    race-free even when other sessions run concurrently. Falls back to
    find_active_jsonl(session_id) when transcript_path is None, an empty
    string, or points to a nonexistent file (older harness versions,
    sub-agent / headless contexts).
    """
    if transcript_path and os.path.exists(transcript_path):
        return transcript_path
    return find_active_jsonl(session_id)


def _project_jsonls(transcript_path: str | None = None) -> list[str]:
    """All JSONL files in the active project dir, newest first."""
    active = _resolve_jsonl(transcript_path)
    if not active:
        return []
    project_dir = os.path.dirname(active)
    files = glob.glob(os.path.join(project_dir, "*.jsonl"))
    return sorted(files, key=os.path.getmtime, reverse=True)


def _detect_mode(current_tokens: int | None, jsonl_path: str | None, session_id: str | None = None) -> str:
    """Detect this session's context-window mode from observable signals.

    Order of precedence:
      1. ENGRAM_CONTEXT_MODE env var override ("200k" or "1m") — explicit.
      2. The session's per-session marker `context_mode` field at
         ~/.engram/sessions/<session_id>.json, written by SessionStart hook
         from ENGRAM_CONTEXT_MODE.
      3. current input tokens > MODE_BOUNDARY → "1m" (200K-mode would have
         auto-compacted by now).
      4. Current JSONL has any prior compact_boundary > MODE_BOUNDARY → "1m".
      5. Default "200k" (the harness default).

    Limitation: early 1M-mode sessions below MODE_BOUNDARY with no prior
    boundary cannot be auto-detected (the system prompt's `[1m]` model tag
    is not surfaced in JSONL records). Set ENGRAM_CONTEXT_MODE=1m at session
    launch when in 1M mode for accurate early-session drowsiness reporting.
    """
    override = os.environ.get("ENGRAM_CONTEXT_MODE", "").strip().lower()
    if override in SAMPLE_PARTITIONS:
        return override
    if session_id:
        marker_path = os.path.join(ENGRAM_HOME, "sessions", f"{session_id}.json")
        try:
            with open(marker_path) as f:
                marker = json.load(f)
            marker_mode = (marker.get("context_mode") or "").strip().lower()
            if marker_mode in SAMPLE_PARTITIONS:
                return marker_mode
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if current_tokens is not None and current_tokens > MODE_BOUNDARY:
        return "1m"
    if jsonl_path:
        try:
            with open(jsonl_path, "rb") as f:
                for line in f:
                    if b"compact_boundary" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, json.JSONDecodeError):
                        continue
                    meta = obj.get("compactMetadata") or {}
                    pt = meta.get("preTokens")
                    if isinstance(pt, int) and pt > MODE_BOUNDARY:
                        return "1m"
        except OSError:
            pass
    return "200k"


def collect_auto_pretokens(
    mode: str = "200k",
    max_samples: int = AUTO_SAMPLE_WINDOW,
    transcript_path: str | None = None,
) -> list[int]:
    """Most recent auto-compaction preTokens in the given mode magnitude class.

    Filters by SAMPLE_PARTITIONS[mode] to keep 200K-mode and 1M-mode samples
    from contaminating each other's mean.
    """
    lo, hi = SAMPLE_PARTITIONS[mode]
    collected: list[int] = []
    for path in _project_jsonls(transcript_path):  # newest file first
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
                    if isinstance(pt, int) and lo <= pt <= hi:
                        file_samples.append(pt)
        except OSError:
            continue
        # Older file → older samples → prepend.
        collected = file_samples + collected
        if len(collected) >= max_samples:
            break
    return collected[-max_samples:]


def compute_ceiling(mode: str = "200k", transcript_path: str | None = None) -> int:
    samples = collect_auto_pretokens(mode, transcript_path=transcript_path)
    if len(samples) < 2:
        return int(FALLBACK_CEILING_BY_MODE[mode] * SAFETY_FACTOR)
    mean = sum(samples) // len(samples)
    return int(mean * SAFETY_FACTOR)


def _last_compact_offset(jsonl_path: str) -> int | None:
    """Byte-offset of last /compact, from ~/.engram/last-compact-at.json.
    Returns None if marker missing/stale/malformed. See Claude sidecar."""
    try:
        with open(LAST_COMPACT_PATH) as f:
            marker = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if marker.get("jsonl_path") != jsonl_path:
        return None
    offset = marker.get("byte_offset")
    if not isinstance(offset, int) or offset < 0:
        return None
    return offset


def read_current_tokens(transcript_path: str | None = None) -> int | None:
    """Input-token count of the most recent post-compact assistant usage record.

    Two boundary-detection paths: (1) ~/.engram/last-compact-at.json marker
    written by PostCompact hook (canonical on Claude Code 2026-05-07+);
    (2) legacy compact_boundary JSONL scan (older formats). See the Claude
    sidecar for full rationale (ob_1532).
    """
    jsonl_path = _resolve_jsonl(transcript_path)
    if not jsonl_path:
        return None
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            compact_offset = _last_compact_offset(jsonl_path)
            if compact_offset is not None and 0 <= compact_offset <= size:
                start = compact_offset
            else:
                start = max(0, size - TAIL_SCAN_BYTES)
            f.seek(start)
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


def estimate_usage(transcript_path: str | None = None, session_id: str | None = None) -> dict | None:
    """Compute current drowsiness reading.

    Both arguments come from this hook's stdin payload (harness emits
    session_id + transcript_path uniformly). Threading them through means
    every path lookup resolves to THIS session — race-free even when other
    sessions run concurrently. When either is missing, callers fall back
    to looking up the per-session marker at
    ~/.engram/sessions/<session_id>.json by session_id.

    Note on triple-resolve: read_current_tokens() and compute_ceiling() each
    re-call _resolve_jsonl(transcript_path, session_id) internally rather
    than receiving the resolved path. Intentional — keeps each sub-function
    independently callable.
    """
    jsonl = _resolve_jsonl(transcript_path, session_id)
    current = read_current_tokens(transcript_path)
    if current is None:
        return None
    mode = _detect_mode(current, jsonl, session_id=session_id)
    ceiling = compute_ceiling(mode, transcript_path=transcript_path)
    if ceiling <= 0:
        return None
    return {
        "estimated_tokens": current,
        "context_limit": ceiling,
        "usage_pct": current / ceiling,
        "mode": mode,
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
            "  3. Run engram_nap\n"
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
