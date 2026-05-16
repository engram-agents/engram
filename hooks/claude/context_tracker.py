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

# Drowsiness display + warning thresholds. Calibrated for both 200K and 1M
# context modes. Below the urgent threshold, the display shows a level word
# only — no raw percentage. This is the cj_0054 / Lei 2026-05-12 PM design
# change: raw numbers were producing an affective tiredness-response that
# the structural warning words did not fully recalibrate (dv_0623 / ob_1683
# wrap-up-framing pattern). Replacing numbers with level words below the
# urgent threshold lets the agent operate against the threshold semantics
# rather than against an implicit "60% tired" feel.
#
# Default thresholds depend on context-window mode. 200K mode needs more
# percentage-wise buffer for running the nap (at 90% in 200K, only ~15K
# headroom remains — barely enough for a write-cluster + checkpoint). 1M
# mode at the same percentage has ~80K headroom — plenty for nap work.
#
#   200k mode (ceiling ~152K after safety factor):
#     85% = 129K used, ~23K headroom — urgent threshold for 200k
#     90% = 137K used, ~15K headroom — too close, post-urgent
#   1m mode (ceiling ~807K after safety factor):
#     80% = 645K used, ~155K headroom — getting drowsy / a-little-drowsy
#     90% = 726K used, ~80K headroom — urgent / needs-a-nap
#
# Earlier values (0.50 / 0.70) were calibrated when 200K was the only mode
# and fired far too early on 1M sessions — at 70% on 1M, 240K headroom
# remains, more than a full fresh 200K window. Lei's framing 2026-05-04:
# only fire warnings when truly close to compaction, not as a routine nudge.
DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE = {
    "200k": {
        # 200K needs more buffer — urgent fires earlier.
        "refreshed_below":      0.50,
        "energetic_below":      0.70,
        "a_little_drowsy_below": 0.85,
        "urgent_at_or_above":   0.85,
    },
    "1m": {
        # 1M has plenty of headroom — Lei's stated spec.
        "refreshed_below":      0.50,
        "energetic_below":      0.80,
        "a_little_drowsy_below": 0.90,
        "urgent_at_or_above":   0.90,
    },
}
# Legacy compatibility — old name-based references; structural warnings
# still fire at these thresholds for the multi-line warning text.
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
# can only be 1m mode — 200k mode would have already compacted. Used by
# _detect_mode path 3 (mid-session inference). Set above the 200k auto-
# compact ceiling with a small buffer to avoid false-positive 1m detection
# right at the 200k auto-compact point. Previously 300_000 (too high to
# trigger in practice — most sessions never reach 300K input_tokens before
# either compacting or napping). Lowered 2026-05-06 after a 1m-mode session
# at 184K input_tokens was misclassified as 200k → reported 122% drowsy when
# truly at ~23%.
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
    the transcript file itself doesn't exist yet (before Claude Code has
    flushed the new JSONL). Callers MUST pass session_id — there is no
    global fallback.
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

    The transcript_path argument is the per-event path Claude Code provides
    on each hook's stdin payload (session_id + transcript_path are uniform
    across UserPromptSubmit, Stop, and other hook events). When the hook
    wrapper threads that value into the tracker, every path lookup in this
    module routes through the caller's own session — race-free even when
    other Claude sessions are running concurrently. Falls back to
    find_active_jsonl(session_id) when transcript_path is None, an empty
    string, or points to a nonexistent file (the empty/missing case can
    arise on older Claude Code versions that don't yet emit transcript_path
    on UserPromptSubmit, or in sub-agent / headless contexts).
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
      2. The session's per-session marker `context_mode` field — session
         metadata written by SessionStart hook at
         ~/.engram/sessions/<session_id>.json.
      3. current input tokens > MODE_BOUNDARY → "1m" (200K-mode would have
         auto-compacted by now).
      4. Current JSONL has any prior compact_boundary > MODE_BOUNDARY → "1m".
      5. Default "200k" (the harness default).

    Limitation: early 1M-mode sessions below MODE_BOUNDARY with no prior
    boundary cannot be auto-detected from observable signals alone (the
    system prompt's `[1m]` model tag is not surfaced in JSONL records).
    The per-session marker `context_mode` field exists for that case —
    the SessionStart hook should populate it whenever it can detect mode
    from harness env vars or the system prompt content.
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
    """Return the byte-offset where the last /compact fired, if known.

    Reads ~/.engram/last-compact-at.json (written by the PostCompact hook).
    Returns None if the marker is missing, stale (records a different
    JSONL than the current session), or unreadable. As of ~2026-05-07
    Claude Code no longer writes a `compact_boundary` JSONL entry, so
    this marker file is the only reliable source of "where did the
    post-compact window begin." Older sessions still have JSONL markers
    and rely on the legacy fallback path in read_current_tokens.
    """
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

    Two boundary-detection paths, in priority order:

    1. ~/.engram/last-compact-at.json marker file written by the PostCompact
       hook. If present and matches the current JSONL, only the post-marker
       portion of the file is scanned — pre-compact usage records are
       structurally invisible. This is the canonical path on Claude Code
       versions that no longer write compact_boundary JSONL entries
       (~2026-05-07 onward; ob_1532).

    2. Fallback: scan the last TAIL_SCAN_BYTES of the JSONL in reverse.
       If a `compact_boundary` event appears before any usage record,
       return None. This path catches older JSONL formats and any session
       that compacted before the PostCompact hook was deployed.

    Returns None when the post-compact window has no usage record yet —
    the correct behavior in the turn between /compact and the first
    post-compact assistant message (which prevents the bogus drowsiness
    spike documented in ob_1013 and ob_1532).
    """
    jsonl_path = _resolve_jsonl(transcript_path)
    if not jsonl_path:
        return None
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Path 1: if a post-compact marker exists for this JSONL, start
            # scanning from that offset. Records before it are pre-compact.
            compact_offset = _last_compact_offset(jsonl_path)
            if compact_offset is not None and 0 <= compact_offset <= size:
                # Use the marker even when offset == size: that means
                # /compact just fired and no post-compact content has been
                # written yet. The post-compact window is empty → reading
                # an empty slice yields no usage records → returns None,
                # which suppresses the drowsiness banner correctly.
                start = compact_offset
            else:
                start = max(0, size - TAIL_SCAN_BYTES)
            f.seek(start)
            tail = f.read()
    except OSError:
        return None

    for line in reversed(tail.split(b"\n")):
        if b'"compact_boundary"' in line:
            # Path 2 fallback: legacy JSONL format had this marker.
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

    Both arguments come from this hook's stdin payload (Claude Code emits
    session_id + transcript_path on every hook event). Threading them
    through means every path lookup resolves to THIS session — race-free
    even when other Claude sessions run concurrently. When either is
    missing, callers fall back to looking up the per-session marker at
    ~/.engram/sessions/<session_id>.json by session_id.

    Note on triple-resolve: read_current_tokens() and compute_ceiling() each
    re-call _resolve_jsonl(transcript_path, session_id) internally rather
    than receiving the path resolved here. This is intentional — each
    sub-function accepts both so it remains independently callable.
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


def _load_drowsiness_thresholds(mode: str) -> dict:
    """Load drowsiness display thresholds for the given context-window mode.

    Reads $ENGRAM_HOME/config.json's `drowsiness_display.<mode>` block if
    present, otherwise returns DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE[mode].
    Per-field fallback: any field missing in config falls back to its
    default value, so partial config customisation works without forcing
    the user to copy all four fields.

    Config schema (all keys optional):
      {
        "drowsiness_display": {
          "200k": {
            "refreshed_below":       0.50,
            "energetic_below":       0.70,
            "a_little_drowsy_below": 0.85,
            "urgent_at_or_above":    0.85
          },
          "1m": { ...same shape... }
        }
      }

    Threshold-relationship guidance (not enforced):
      Defaults set a_little_drowsy_below == urgent_at_or_above (the drowsy
      band has zero width — at-or-above urgent immediately triggers the
      urgent warning). If you customise one without the other (e.g. push
      urgent_at_or_above to 0.95 but leave a_little_drowsy_below at 0.90),
      pct in the gap (0.90–0.95) will fire the 'a little drowsy' warning,
      not the urgent one. This is the intended behavior: the threshold
      dispatch ensures every band above a_little_drowsy_below gets at
      least the drowsy warning. Move both fields together if you want a
      different urgent point with no drowsy band.
    """
    defaults = DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE.get(
        mode, DEFAULT_DROWSINESS_THRESHOLDS_BY_MODE["1m"]
    )
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return defaults
    display_block = (config.get("drowsiness_display") or {}).get(mode) or {}
    if not isinstance(display_block, dict):
        return defaults
    return {
        key: display_block.get(key, default)
        for key, default in defaults.items()
    }


def drowsiness_level(pct: float, thresholds: dict) -> str:
    """Map a drowsiness percentage to a level word.

    Per cj_0054 conjecture (Lei 2026-05-12 PM): below the urgent threshold,
    only a level word is shown; raw percentage is hidden. At or above
    urgent, the caller renders the percentage explicitly. Thresholds are
    strict-less-than for the first three levels; urgent is at-or-above.
    """
    if pct < thresholds["refreshed_below"]:
        return "refreshed"
    if pct < thresholds["energetic_below"]:
        return "energetic"
    if pct < thresholds["a_little_drowsy_below"]:
        return "a little drowsy"
    return "needs a nap"


def format_drowsiness(usage: dict) -> str:
    """Generate drowsiness banner. Returns the banner line(s).

    Per cj_0054 conjecture + Lei 2026-05-12 design: hide raw percentages
    below the urgent threshold; show level words instead. At or above
    urgent, render the raw percentage so the agent has the calibration
    signal exactly when it's structurally warranted.

    In loop mode ($ENGRAM_HOME/loop-mode.json present), warnings are
    suppressed; the urgent-level percentage is still reported but
    nap/stop directives are not issued.
    """
    pct = usage["usage_pct"]
    est_tokens = usage["estimated_tokens"]
    limit = usage["context_limit"]
    mode = usage.get("mode", "1m")

    thresholds = _load_drowsiness_thresholds(mode)
    urgent = pct >= thresholds["urgent_at_or_above"]

    loop_mode = os.path.exists(LOOP_MARKER_PATH)

    if loop_mode:
        # Loop mode: surface the percentage at urgent, level word otherwise.
        if urgent:
            return f"[Drowsiness: needs a nap: {pct:.0%} — loop mode, compaction will auto-fire]"
        return f"[Drowsiness: {drowsiness_level(pct, thresholds)} — loop mode, compaction will auto-fire]"

    if urgent:
        return (
            f"[Drowsiness: needs a nap: {pct:.0%}]\n"
            f"  (~{est_tokens:,} / {limit:,} tokens)\n"
            "  You're close to compaction. Stop current work and consolidate:\n"
            f"  1. Write key observations and derivations to ENGRAM\n"
            f"  2. Update your warm briefing ({WARM_BRIEFING_PATH})\n"
            "  3. Run engram_nap\n"
            "  Tell the user: \"I'm very drowsy — let me take a nap to consolidate.\""
        )
    # Dispatch on threshold, not on the level word, so partial-config setups
    # where energetic_below and urgent_at_or_above diverge (e.g. user
    # sets urgent=0.95, leaves a_little_drowsy_below at default 0.90) still
    # fire the drowsy warning in the gap band rather than swallowing it.
    # energetic_below is the LOWER bound of the a_little_drowsy band
    # (level becomes a_little_drowsy when pct >= energetic_below); using
    # this as the dispatch threshold ensures the warning fires across the
    # entire a_little_drowsy band regardless of where urgent_at_or_above
    # is set. Token-count line intentionally omitted at this tier — Lei's
    # design hides numeric drowsiness signals below urgent.
    level = drowsiness_level(pct, thresholds)
    if pct >= thresholds["energetic_below"]:
        # Use the computed level rather than a hardcoded string. Under a
        # partial-config setup where urgent_at_or_above > a_little_drowsy_below
        # (user pushed urgent up, left a_little_drowsy at default), pct in the
        # gap renders as "needs a nap" per the level mapping but is still
        # below the urgent threshold — so the banner reads correctly without
        # firing the urgent multi-line block. Defaults make the two
        # thresholds equal so this branch always renders "a little drowsy"
        # in the default case.
        return (
            f"[Drowsiness: {level}]\n"
            "  Keep writing to ENGRAM. Plan a nap soon.\n"
            "  Record important decisions and observations before context fills."
        )
    return f"[Drowsiness: {level}]"
