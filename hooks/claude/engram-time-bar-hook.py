#!/usr/bin/env python3
"""
engram-time-bar-hook.py (MECH-1 of the time-awareness design, dv_0472)

Prepends a one-line ambient time bar to the UserPromptSubmit output so Borges
has real-world temporal grounding on every turn without having to query it.
Closes the Welcome-Back failure mode (qu_0223) and the FROZEN-NOW-IN-
COMPACTION-SUMMARY risk (dv_0469) by re-rendering every turn against wall-clock.

Output (one line, prepended to the injected context):
  [Time: <UTC_iso_zulu> (<weekday>) | <tz_label> <local_iso> | session started <hms> ago | last user msg <hms> ago]

Signal sources:
  - UTC now: datetime.now(timezone.utc)
  - Session start: ~/.engram/active-session.json 'started_at' (SessionStart hook writes it)
  - Last user msg: ~/.engram/last-user-msg.json (self-maintained by this hook)
  - Lei-local tz: ~/.engram/config.json 'user.timezone' (fallback 'America/Los_Angeles')

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
ACTIVE_SESSION = ENGRAM_DIR / "active-session.json"
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


def main():
    now_utc = datetime.now(timezone.utc)
    utc_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    weekday = now_utc.strftime("%a")

    session = _read_json(ACTIVE_SESSION) or {}
    session_ago = _hms_ago(now_utc, session.get("started_at"))

    prev = _read_json(LAST_USER_MSG) or {}
    last_msg_ago = _hms_ago(now_utc, prev.get("ts")) if prev.get("ts") else "(this msg)"
    try:
        LAST_USER_MSG.write_text(json.dumps({"ts": utc_iso}))
    except Exception:
        pass

    tz_name = _get_lei_tz()
    local_iso = ""
    tz_label = tz_name
    if ZoneInfo is not None:
        try:
            local = now_utc.astimezone(ZoneInfo(tz_name))
            local_iso = local.strftime("%Y-%m-%d %H:%M")
            tz_abbr = local.strftime("%Z")
            if tz_abbr:
                tz_label = tz_abbr
        except Exception:
            local_iso = "tz-error"

    bar = (
        f"[Time: {utc_iso} ({weekday}) | "
        f"{tz_label} {local_iso} | "
        f"session started {session_ago} ago | "
        f"last user msg {last_msg_ago} ago]"
    )
    print(bar)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never crash the hook chain — emit a stub so Borges sees something.
        print(f"[Time: (hook-error: {type(exc).__name__})]")
        sys.exit(0)
