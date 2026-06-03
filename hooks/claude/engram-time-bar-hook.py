#!/usr/bin/env python3
"""
engram-time-bar-hook.py (MECH-1 of the time-awareness design, the time-awareness derivation)

Prepends a one-line ambient time bar to the UserPromptSubmit output so the agent
has real-world temporal grounding on every turn without having to query it.
Closes the Welcome-Back failure mode and the FROZEN-NOW-IN-
COMPACTION-SUMMARY risk (the CLOCK-DRIFT-hygiene derivation) by re-rendering every turn against wall-clock.

Output (one line, prepended to the injected context):
  [Time: <weekday> <local_date> <local_12h> <tz_abbr> (<utc_hhmmZ>) | session started <hms> ago | last user msg <hms> ago]

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
  - User-local tz: ~/.engram/config.json 'user.timezone' (fallback 'America/Los_Angeles')

Additive-only: if any signal fails to read, the bar renders with a best-effort
label ('unknown', 'parse-error', etc) rather than crashing. The hook writes to
stdout only; stderr is reserved for diagnostics the agent shouldn't see.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

ENGRAM_DIR = Path.home() / ".engram"
SESSIONS_DIR = ENGRAM_DIR / "sessions"
LAST_USER_MSG = ENGRAM_DIR / "last-user-msg.json"
CONFIG = ENGRAM_DIR / "config.json"
DEFAULT_TZ = "America/Los_Angeles"


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


def _get_lei_tz():
    cfg = _read_json(CONFIG) or {}
    user = cfg.get("user") or {}
    return user.get("timezone") or DEFAULT_TZ


def _format_time_field(now_utc: datetime, tz_name: str) -> str:
    """Render the time field with USER-LOCAL prominent and UTC parenthetical.

    weekday / date / 12-hour clock / tz abbr are all derived from the user's
    timezone (so the agent reads the human's real time-of-day, including the
    correct local date — which can differ from the UTC date near midnight).
    UTC is reduced to a small ``(HH:MMZ)`` tail. If the timezone can't be
    resolved (no zoneinfo module, bad tz name), fall back to a clearly-labelled
    UTC so the failure is visible rather than silently mis-rendered as local.
    """
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


def _read_session_id_from_stdin():
    """Pull session_id off this hook's stdin payload. Returns None on any error."""
    try:
        raw = sys.stdin.read()
        if not raw:
            return None
        payload = json.loads(raw)
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    except Exception:
        pass
    return None


def main():
    now_utc = datetime.now(timezone.utc)
    utc_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")  # retained: stamps last-user-msg.json below

    # Per-session marker keyed by session_id (issue #140 retired the global
    # active-session.json). Falls through to unknown when stdin is empty.
    session = {}
    session_id = _read_session_id_from_stdin()
    if session_id:
        session = _read_json(SESSIONS_DIR / f"{session_id}.json") or {}
    session_ago = _hms_ago(now_utc, session.get("started_at"))

    prev = _read_json(LAST_USER_MSG) or {}
    last_msg_ago = _hms_ago(now_utc, prev.get("ts")) if prev.get("ts") else "(this msg)"
    try:
        LAST_USER_MSG.write_text(json.dumps({"ts": utc_iso}))
    except Exception:
        pass

    time_field = _format_time_field(now_utc, _get_lei_tz())
    bar = (
        f"[Time: {time_field} | "
        f"session started {session_ago} ago | "
        f"last user msg {last_msg_ago} ago]"
    )
    print(bar)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never crash the hook chain — emit a stub so the agent sees something.
        print(f"[Time: (hook-error: {type(exc).__name__})]")
        sys.exit(0)
