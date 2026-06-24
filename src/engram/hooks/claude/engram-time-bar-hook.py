#!/usr/bin/env python3
"""
engram-time-bar-hook.py (MECH-1 of the time-awareness design, the time-awareness derivation)

Prepends a one-line ambient time bar to the UserPromptSubmit output so the agent
has real-world temporal grounding on every turn without having to query it.
Closes the Welcome-Back failure mode and the FROZEN-NOW-IN-
COMPACTION-SUMMARY risk (the CLOCK-DRIFT-hygiene derivation) by re-rendering every turn against wall-clock.

Output (one line, prepended to the injected context):
  Configured tz:   [Time: <weekday> <local_date> <local_12h> <tz_abbr> (<utc_hhmmZ>) | session started <hms> ago | last user msg <hms> ago]
  Unconfigured tz: [Time: <weekday> <utc_date> <utc_HHMM> UTC (tz not configured) | session started <hms> ago | last user msg <hms> ago]

User-local time is PROMINENT — weekday, date, 12-hour clock, and tz abbr are all
derived from the user's timezone (not UTC), so the agent reads the human's actual
time-of-day directly. UTC is a small parenthetical, kept only for correlating
UTC-stamped surfaces (forum / inter-agent letters / git). If the local tz can't
be resolved, the bar falls back to a clearly-labelled UTC. (Origin: an agent read
the leading UTC field as the local time and confabulated 'it's 2am'; #721.)

Signal sources:
  - UTC now: datetime.now(timezone.utc)
  - Session start: ~/.engram/sessions/<session_id>.json 'started_at'
    (SessionStart hook writes per-session marker; session_id read from this
    hook's stdin payload per issue #140)
  - Last user msg: ~/.engram/last-user-msg.json (self-maintained by this hook)
  - User-local tz: ~/.engram/config.json 'user.timezone' (None when absent →
    UTC shown with '(tz not configured)' label rather than a silently-wrong
    default coast; set user.timezone to an IANA name, e.g. 'America/New_York')

Additive-only: if any signal fails to read, the bar renders with a best-effort
label ('unknown', 'parse-error', etc) rather than crashing. The hook writes to
stdout only; stderr is reserved for diagnostics the agent shouldn't see.
"""
import os as _os, sys as _sys
# Guard against source: directory marketplace double-fire (#1066).
_plugin_root = _os.environ.get("CLAUDE_PLUGIN_ROOT", "")
_engram_home = _os.environ.get("ENGRAM_HOME") or _os.path.expanduser("~/.engram")
if _plugin_root.startswith(_os.path.join(_engram_home, "marketplace") + _os.sep):
    _sys.exit(0)  # empty stdout is valid no-op per #824/#832 contract
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

ENGRAM_DIR = Path(os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram"))
SESSIONS_DIR = ENGRAM_DIR / "sessions"
LAST_USER_MSG = ENGRAM_DIR / "last-user-msg.json"
LAST_USER_ACTIVITY = ENGRAM_DIR / "last-user-activity"
CONFIG = ENGRAM_DIR / "config.json"

# Prompt-body prefixes that identify non-human events (loop self-wakes and
# monitor notifications). Used as the fallback discriminator when promptSource
# is absent from the hook payload.
_NON_HUMAN_PREFIXES = (
    "<task-notification",
    "<command-message",
    "<command-name",
    "<local-command",
)


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _hms_ago(now_utc: datetime, then_iso):
    if not then_iso:
        return "unknown"
    try:
        then = datetime.fromisoformat(then_iso.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except Exception:
        return "parse-error"
    secs = int((now_utc - then).total_seconds())
    if secs < 0:
        return "future?"
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m"
    hrs, mins = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h{mins}m" if mins else f"{hrs}h"
    days, hrs = divmod(hrs, 24)
    return f"{days}d{hrs}h" if hrs else f"{days}d"


def _get_user_tz():
    """Return the user's IANA timezone name, or None if not configured."""
    cfg = _read_json(CONFIG) or {}
    user = cfg.get("user") or {}
    tz = user.get("timezone")
    return tz if isinstance(tz, str) and tz.strip() else None


def _format_time_field(now_utc: datetime, tz_name) -> str:
    """Render the time field with USER-LOCAL prominent and UTC parenthetical.

    weekday / date / 12-hour clock / tz abbr are all derived from the user's
    timezone (so the agent reads the human's real time-of-day, including the
    correct local date — which can differ from the UTC date near midnight).
    UTC is reduced to a small ``(HH:MMZ)`` tail.

    When tz_name is None (user.timezone absent from config.json), the bar
    shows UTC with a visible '(tz not configured)' label — an actionable
    signal, never a silently-wrong local time defaulting to the wrong coast.
    Set user.timezone in config.json to an IANA name (e.g. 'America/New_York').

    If the timezone can't be resolved (no zoneinfo module, bad tz name), fall
    back to a clearly-labelled UTC so the failure is visible rather than
    silently mis-rendered as local.
    """
    if tz_name is None:
        # Not configured — show UTC with an actionable label.
        return (
            f"{now_utc.strftime('%a')} {now_utc.strftime('%Y-%m-%d')} "
            f"{now_utc.strftime('%H:%M')} UTC (tz not configured)"
        )
    if ZoneInfo is not None:
        try:
            local = now_utc.astimezone(ZoneInfo(tz_name))
            weekday = local.strftime("%a")
            local_date = local.strftime("%Y-%m-%d")
            # 12-hour clock, strip the leading zero ("07:34 PM" -> "7:34 PM").
            # Safe: %I produces "01".."12" (never "00"), so there is at most one
            # leading zero and lstrip("0") never eats a meaningful digit
            # ("10"/"11"/"12" start with 1; "12:00 AM/PM" survive intact).
            local_12h = local.strftime("%I:%M %p").lstrip("0")
            tz_abbr = local.strftime("%Z") or tz_name
            utc_short = now_utc.strftime("%H:%MZ")
            return f"{weekday} {local_date} {local_12h} {tz_abbr} ({utc_short})"
        except Exception:
            pass
    # Local tz unavailable — show UTC prominently and SAY so (don't pretend it's local).
    return f"{now_utc.strftime('%a')} {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} UTC (local tz unavailable)"


def _read_stdin_payload() -> dict:
    """Read and parse the hook's stdin payload once. Returns {} on any error."""
    try:
        raw = sys.stdin.read()
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _is_human_prompt(payload: dict) -> bool:
    """Return True if the payload represents a genuine human-typed prompt.

    Discriminator (per spec, verified in transcript 2026-06-13):
      1. If promptSource is present: stamp only when promptSource == "typed".
         - "typed" = genuine human keyboard input
         - "system" = monitor task-notification event
         - absent = loop self-wake (ScheduleWakeup) carrying a <command-*> body
      2. Fallback when promptSource absent: check whether the prompt body starts
         with any known non-human prefix (<task-notification>, <command-message>,
         <command-name>, <local-command>). If yes → not human.

    Fail-safe: on any parsing problem, returns False (no stamp) rather than
    risking a false "human" classification.
    """
    try:
        prompt_source = payload.get("promptSource")
        if prompt_source is not None:
            # promptSource is present in payload — trust it directly.
            return prompt_source == "typed"
        # promptSource absent: fall back to body-prefix exclusion.
        prompt = payload.get("prompt") or ""
        if isinstance(prompt, str):
            stripped = prompt.lstrip()
            for prefix in _NON_HUMAN_PREFIXES:
                if stripped.startswith(prefix):
                    return False
            # No known non-human prefix AND a non-empty body = positive
            # evidence of a human prompt. An empty/absent body (absence of
            # evidence — e.g. an unreadable or empty payload) is NOT treated
            # as human: stamping on it would falsely mark 'engaged'.
            return bool(stripped)
    except Exception:
        pass
    return False


def _stamp_user_activity(now_epoch: float) -> None:
    """Write epoch seconds to LAST_USER_ACTIVITY. Fail-safe: swallow all errors."""
    try:
        LAST_USER_ACTIVITY.write_text(str(int(now_epoch)))
    except Exception:
        pass


def main():
    now_utc = datetime.now(timezone.utc)
    utc_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")  # retained: stamps last-user-msg.json below

    # Read stdin once; extract all needed fields from it.
    payload = _read_stdin_payload()

    # --- Stamp last-user-activity EARLY, before any other work (spec requirement).
    # Only on genuine human-typed prompts — loop self-wakes and monitor events
    # must NOT update the stamp (they would falsely mark the agent as "engaged").
    if _is_human_prompt(payload):
        _stamp_user_activity(now_utc.timestamp())

    # Per-session marker keyed by session_id (issue #140 retired the global
    # active-session.json). Falls through to unknown when stdin is empty.
    session = {}
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        session = _read_json(SESSIONS_DIR / f"{session_id}.json") or {}
    session_ago = _hms_ago(now_utc, session.get("started_at"))

    prev = _read_json(LAST_USER_MSG) or {}
    last_msg_ago = _hms_ago(now_utc, prev.get("ts")) if prev.get("ts") else "(this msg)"
    try:
        LAST_USER_MSG.write_text(json.dumps({"ts": utc_iso}))
    except Exception:
        pass

    time_field = _format_time_field(now_utc, _get_user_tz())
    bar = (
        f"[Time: {time_field} | "
        f"session started {session_ago} ago | "
        f"last user msg {last_msg_ago} ago]"
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": bar}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never crash the hook chain — emit a stub so the agent sees something.
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": f"[Time: (hook-error: {type(exc).__name__})]"}}))
        sys.exit(0)
