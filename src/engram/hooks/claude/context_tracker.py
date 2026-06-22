#!/usr/bin/env python3
"""
ENGRAM Context Tracker — Drowsiness Monitor

Measures context usage from per-message token counts in the Claude Code
session JSONL, and reports drowsiness as a fraction of the user-configured
ceiling.

Algorithm:
  ceiling = cadence.drowsiness_ceiling_tokens (from config.json, user-set)
  current = (input + cache_read + cache_create) from last assistant usage record
  pct     = current / ceiling

The ceiling is a single explicit integer the user sets after checking their
auto-compaction limit (via the /context slash-command). There is no JSONL
scan, no mode detection, no auto-sampling. See the engram-first-session
skill for the setup ritual.

Stateless: no baseline file, no byte-to-token ratio, no cached state.
Every call reads the JSONL and config.json fresh.
"""

import glob
import json
import os

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or os.path.expanduser("~/.engram")
)
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")  # legacy; unused
LAST_COMPACT_PATH = os.path.join(ENGRAM_HOME, "last-compact-at.json")
LOOP_MARKER_PATH = os.path.join(ENGRAM_HOME, "loop-mode.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")

TAIL_SCAN_BYTES = 5_000_000     # How far back to look for the latest usage

# Fallback ceiling when cadence.drowsiness_ceiling_tokens is absent.
# Conservative: the 200k-mode floor. Users who don't set the field will see
# frequent drowsiness warnings, which is the correct nudge to configure the
# field. Previous value (FALLBACK_CEILING_BY_MODE["200k"] * SAFETY_FACTOR)
# was int(160_000 * 0.95) = 152_000; keeping the same number for continuity.
HARDCODED_FALLBACK_CEILING = 152_000

# Module-level flag: emit the migration notice at most once per process.
# Prevents stderr spam when compute_ceiling is called multiple times per session.
_migration_notice_emitted = False

# Drowsiness display + warning thresholds. Calibrated for both 200K and 1M
# context modes. Below the urgent threshold, the display shows a level word
# only — no raw percentage. This is the cj_NNNN / the maintainer design
# change: raw numbers were producing an affective tiredness-response that
# the structural warning words did not fully recalibrate (dv_NNNN / ob_NNNN
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
# remains, more than a full fresh 200K window. The maintainer's framing:
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
        # 1M has plenty of headroom — the maintainer's spec.
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


def _read_drowsiness_ceiling_tokens() -> int:
    """Read cadence.drowsiness_ceiling_tokens from config.json.

    Single source of truth for the drowsiness denominator (the context-
    window ceiling). User sets this once after checking their /context
    slash-command's auto-compaction limit. Set to the actual auto-compact
    firing threshold, not a fixed percentage of window size (see #1247).
    Empirical values: 200K window → 155_000; 1M window → 950_000.

    Migration path (one upgrade cycle from #314's per-mode shape):
      If cadence.drowsiness_ceiling_tokens is absent, falls back to
      cadence.drowsiness_ceiling_max map, preferring the "1m" value since
      most users on the #314 shape were configuring 1m. Emits a one-time
      stderr notice to prompt migration.

    If neither field is present: returns HARDCODED_FALLBACK_CEILING
    (152_000) and emits a notice asking the user to configure the field.

    Return value is always a positive int. Never raises.

    Defensive type guard: same isinstance(value, int) and not isinstance(
    value, bool) and value > 0 pattern as the retired _read_ceiling_override
    — prevents bool True (= 1) or False (= 0) JSON values from being treated
    as a valid ceiling and producing astronomical drowsiness percentages.
    """
    global _migration_notice_emitted

    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        config = {}

    cadence_block = config.get("cadence")

    # Path 1: new single-value field.
    if isinstance(cadence_block, dict):
        value = cadence_block.get("drowsiness_ceiling_tokens")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value

        # Path 2: migration fallback — old per-mode map from #314.
        legacy_map = cadence_block.get("drowsiness_ceiling_max")
        if isinstance(legacy_map, dict):
            # Prefer "1m" value (most users who had the old shape were on 1m);
            # fall through to "200k" if "1m" is null or missing.
            for mode_key in ("1m", "200k"):
                legacy_val = legacy_map.get(mode_key)
                if isinstance(legacy_val, int) and not isinstance(legacy_val, bool) and legacy_val > 0:
                    if not _migration_notice_emitted:
                        _migration_notice_emitted = True
                        print(
                            "[engram] One-time notice: cadence.drowsiness_ceiling_max is "
                            "deprecated. Migrate to cadence.drowsiness_ceiling_tokens "
                            "(single int) — see README.",
                            file=__import__("sys").stderr,
                        )
                    return legacy_val

    # Path 3: no usable config — hardcoded fallback.
    if not _migration_notice_emitted:
        _migration_notice_emitted = True
        print(
            "[engram] Notice: cadence.drowsiness_ceiling_tokens is not set. "
            "Falling back to 152,000 tokens. Run /context in Claude Code, note "
            "the auto-compaction limit, and set cadence.drowsiness_ceiling_tokens "
            "in ~/.engram/config.json to the actual auto-compact threshold "
            "(e.g. 155_000 for 200K window, 950_000 for 1M window — see #1247).",
            file=__import__("sys").stderr,
        )
    return HARDCODED_FALLBACK_CEILING


def compute_ceiling() -> int:
    """Return the drowsiness ceiling in tokens.

    Reads cadence.drowsiness_ceiling_tokens from config.json (the single
    explicit user-set value). Falls back via _read_drowsiness_ceiling_tokens
    migration path if the new field is absent.
    """
    return _read_drowsiness_ceiling_tokens()


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
       (~2026-05-07 onward; ob_NNNN).

    2. Fallback: scan the last TAIL_SCAN_BYTES of the JSONL in reverse.
       If a `compact_boundary` event appears before any usage record,
       return None. This path catches older JSONL formats and any session
       that compacted before the PostCompact hook was deployed.

    Returns None when the post-compact window has no usage record yet —
    the correct behavior in the turn between /compact and the first
    post-compact assistant message (which prevents the bogus drowsiness
    spike documented in ob_NNNN and ob_NNNN).
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

    The ceiling is sourced from cadence.drowsiness_ceiling_tokens (see
    compute_ceiling / _read_drowsiness_ceiling_tokens). Mode is derived
    from the ceiling value: <= 250_000 → "200k", else "1m". This ensures
    format_drowsiness picks per-mode display thresholds correctly rather
    than always defaulting to the "1m" band (which silently ignores
    drowsiness_display.200k config for low-ceiling users).
    """
    current = read_current_tokens(transcript_path)
    if current is None:
        return None
    ceiling = compute_ceiling()
    if ceiling <= 0:
        return None
    # 200k-mode nominal ceiling is ~152k; 1m-mode is 800k+. 250_000 is the
    # natural breakpoint — anything at or below it selects the tighter 200k
    # display thresholds; anything above selects the 1m thresholds.
    mode = "200k" if ceiling <= 250_000 else "1m"
    return {
        "estimated_tokens": current,
        "context_limit": ceiling,
        "usage_pct": current / ceiling,
        "mode": mode,
    }


def write_baseline(use_compact_boundary: bool = True) -> None:
    """No-op — retained for the session-start hook.

    The stateless tracker recomputes on every call; there is no baseline to
    write. Post-compact hooks no longer import this (cleanup: #982).
    Remove entirely when the session-start hook is updated.
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

    Per cj_NNNN conjecture (the maintainer): below the urgent threshold,
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

    Per cj_NNNN conjecture + the maintainer's design: hide raw percentages
    below the urgent threshold; show level words instead. At or above
    urgent, render the raw percentage so the agent has the calibration
    signal exactly when it's structurally warranted.

    In loop mode ($ENGRAM_HOME/loop-mode.json present), at urgent the agent
    is instructed to nap ONCE (engram_nap) to stage the window for
    auto-compaction, then keep working and ride auto-compaction — no manual
    compaction wait, no re-nap before it fires. Below urgent, loop mode is
    silent: the banner is identical to non-loop mode (calm level word only).
    """
    pct = usage["usage_pct"]
    est_tokens = usage["estimated_tokens"]
    limit = usage["context_limit"]
    mode = usage.get("mode", "1m")

    thresholds = _load_drowsiness_thresholds(mode)
    urgent = pct >= thresholds["urgent_at_or_above"]

    loop_mode = os.path.exists(LOOP_MARKER_PATH)

    if loop_mode:
        # Loop mode: at urgent, nap ONCE to stage the window for compaction,
        # then keep pace and ride auto-compaction (don't wait for a manual
        # compaction, don't re-nap each turn before it fires). Below urgent
        # there's nothing loop-specific to say — render the calm level word
        # exactly like non-loop mode, so the banner isn't a per-prompt alarm.
        if urgent:
            return (
                f"[Drowsiness: {pct:.0%} — loop mode: nap ONCE (engram_nap) to "
                f"stage this window for compaction (skip if you already napped "
                f"this burst), then keep working and ride auto-compaction — "
                f"don't slow down for a manual compaction.]"
            )
        return f"[Drowsiness: {drowsiness_level(pct, thresholds)}]"

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
    # is set. Token-count line intentionally omitted at this tier — the maintainer's
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
