#!/usr/bin/env python3
"""render_cron — render the ENGRAM cron block from config.json + agent identity.

Output format: a complete ENGRAM block bracketed by `# BEGIN ENGRAM` / `# END
ENGRAM` markers, followed by a trailing newline. The block is what
tools/sync-cron.sh splices into the user's crontab.

This script READS config.json's `cadence` block (added by schema v2; see
tools/migrate_config_v2.py for the migration) and computes per-minute
heartbeat schedules, dynamically skipping (hour, minute) slots that fall
inside the sleep protected window. The sleep time + protected-window width
are the single source of truth for which heartbeat fires get suppressed —
there is no separate `heartbeat_skip_hours` list. (Earlier schema sketches
included one; it was dead code and got removed before merge.)

Sleep is interactive-only as of 2026-05-14 (alpha #133 PR C). The
`sleep_hour` + `sleep_minute` cadence fields are NO LONGER used to schedule
an autonomous sleep cron — they only define the heartbeat protected window
(the user's preferred quiet slot for interactive bedtime + sleep). The
`tools/sleep.sh` script remains on disk for manual catch-up invocation but
is no longer rendered into the crontab. See dv_0636 for the three-routine
self-engram-maintenance architecture and skills/claude/engram-bedtime/.

Compression: heartbeat hour ranges are minimised to standard cron syntax
(`0-19,22-23` not `0,1,2,...,19,22,23`).

Multi-agent stagger: this script does NOT auto-stagger schedules across
co-located agents. Each agent has its own `config.json` with its own
`cadence`; edit `heartbeat_minutes` and `sleep_minute` per agent so the
windows don't overlap. Worked example for the Borges + Mneme setup:
  - Borges:  sleep_hour=20, sleep_minute=30, heartbeat_minutes=[0, 30]
  - Mneme:   sleep_hour=20, sleep_minute=15, heartbeat_minutes=[15, 45]

Usage:
    python tools/render_cron.py
        --config /home/X/.engram/config.json
        --template /home/X/engram-alpha/templates/crontab.template
        --agent-name borges
        --counterpart-name mneme
        --engram-home /home/X/.engram
        --agent-cli claude
        [--path "/home/.../bin:/usr/bin:/bin"]

Self-check: run with no args to execute internal compress_hours unit
assertions (S4 from the 2026-05-05 PR review).
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_PATH = "/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin"


def in_protected_window(hour: int, minute: int, sleep_hour: int,
                        sleep_minute: int, window_minutes: int) -> bool:
    """Is the (hour, minute) slot inside the sleep protected window?

    Window is centered on sleep time, spans `window_minutes` total.
    INCLUSIVE on both edges, deliberately. Two reasons:
      1. Matches the production Borges crontab byte-for-byte. Sleep at
         20:30, window 60 → [20:00, 21:00] inclusive — both edges are
         skipped on the :00 heartbeat schedule (`0-19,22-23`), confirming
         the inclusive-on-both interpretation.
      2. Symmetric semantics are easier to reason about than asymmetric:
         "the slot is N minutes from sleep time, on either side, where
         N <= window/2" is one rule, not two cases.
    """
    sleep_total = sleep_hour * 60 + sleep_minute
    slot_total = hour * 60 + minute
    half = window_minutes / 2
    return sleep_total - half <= slot_total <= sleep_total + half


def compress_hours(hours: list[int]) -> str:
    """Compress a sorted list of unique ints into cron range syntax.

    [0,1,2,5,6,9] → "0-2,5-6,9"
    [0,1,2,...,19,22,23] → "0-19,22-23"
    """
    if not hours:
        return ""
    hours = sorted(set(hours))
    runs = []
    start = prev = hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
        else:
            runs.append((start, prev))
            start = prev = h
    runs.append((start, prev))
    return ",".join(f"{a}-{b}" if a != b else str(a) for a, b in runs)


def render_heartbeat_lines(cadence: dict, engram_home: str) -> str:
    """One cron line per minute in cadence['heartbeat_minutes']."""
    sleep_hour = cadence["sleep_hour"]
    sleep_minute = cadence["sleep_minute"]
    window = cadence["sleep_protected_window_minutes"]

    lines = []
    for minute in cadence["heartbeat_minutes"]:
        firing_hours = [
            h for h in range(24)
            if not in_protected_window(h, minute, sleep_hour, sleep_minute, window)
        ]
        hour_expr = compress_hours(firing_hours)
        lines.append(
            f"{minute} {hour_expr} * * * {engram_home}/tools/heartbeat.sh "
            f">> {engram_home}/heartbeat.log 2>&1"
        )
    return "\n".join(lines)


def render(config: dict, template_text: str, *, agent_name: str,
           counterpart_name: str, engram_home: str, agent_cli: str,
           path: str = DEFAULT_PATH) -> str:
    cadence = config.get("cadence")
    if not cadence:
        raise ValueError(
            "config has no 'cadence' block — run tools/migrate_config_v2.py first"
        )

    timezone = config.get("user", {}).get("timezone", "local time")
    heartbeat_lines = render_heartbeat_lines(cadence, engram_home)

    return template_text.format(
        path=path,
        engram_home=engram_home,
        agent_name=agent_name,
        counterpart_name=counterpart_name,
        agent_cli=agent_cli,
        heartbeat_minutes=cadence["heartbeat_minutes"],
        heartbeat_lines=heartbeat_lines,
        sleep_hour=cadence["sleep_hour"],
        sleep_minute=cadence["sleep_minute"],
        timezone=timezone,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--counterpart-name", default="")
    parser.add_argument("--engram-home", required=True)
    parser.add_argument("--agent-cli", default="claude",
                        choices=["claude", "gemini"])
    parser.add_argument("--path", default=DEFAULT_PATH)
    args = parser.parse_args()

    if not args.config.exists():
        print(f"ERROR: {args.config} does not exist", file=sys.stderr)
        return 1
    if not args.template.exists():
        print(f"ERROR: {args.template} does not exist", file=sys.stderr)
        return 1

    config = json.loads(args.config.read_text())

    try:
        rendered = render(
            config, args.template.read_text(),
            agent_name=args.agent_name,
            counterpart_name=args.counterpart_name,
            engram_home=args.engram_home,
            agent_cli=args.agent_cli,
            path=args.path,
        )
    except (KeyError, ValueError) as e:
        print(f"ERROR: rendering failed: {e}", file=sys.stderr)
        return 1

    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _self_check() -> int:
    """Internal unit assertions for compress_hours + in_protected_window.

    Runs when this script is invoked with `--self-check`. Verifies the small
    pure-functions that drive the cron output don't regress on edge cases.
    """
    # compress_hours
    assert compress_hours([]) == "", "empty list"
    assert compress_hours([5]) == "5", "single hour"
    assert compress_hours([0, 1, 2, 5, 6, 9]) == "0-2,5-6,9", "gap pattern"
    # Borges :00 heartbeat: every hour except [20, 21]
    borges_00 = [h for h in range(24) if h not in (20, 21)]
    assert compress_hours(borges_00) == "0-19,22-23", \
        f"Borges :00 expected 0-19,22-23, got {compress_hours(borges_00)!r}"
    # Borges :30 heartbeat: every hour except [20]
    borges_30 = [h for h in range(24) if h != 20]
    assert compress_hours(borges_30) == "0-19,21-23", \
        f"Borges :30 expected 0-19,21-23, got {compress_hours(borges_30)!r}"

    # in_protected_window — Borges schedule (sleep 20:30, window 60).
    # Window total 60 → half 30 → range [20:00, 21:00] inclusive.
    assert in_protected_window(20, 0, 20, 30, 60) is True, "Borges 20:00 in window (lower edge)"
    assert in_protected_window(20, 30, 20, 30, 60) is True, "Borges 20:30 in window (sleep itself)"
    assert in_protected_window(21, 0, 20, 30, 60) is True, "Borges 21:00 in window (upper edge)"
    assert in_protected_window(21, 30, 20, 30, 60) is False, "Borges 21:30 NOT in window"
    assert in_protected_window(19, 59, 20, 30, 60) is False, "Borges 19:59 NOT in window (just outside lower edge)"
    assert in_protected_window(0, 0, 20, 30, 60) is False, "Borges 00:00 NOT in window (far away)"

    # End-to-end: render_heartbeat_lines reproduces live Borges schedule
    cadence = {
        "heartbeat_minutes": [0, 30],
        "sleep_hour": 20, "sleep_minute": 30,
        "sleep_protected_window_minutes": 60,
    }
    out = render_heartbeat_lines(cadence, "/home/agent-borges/.engram")
    lines = out.split("\n")
    assert "0 0-19,22-23 * * *" in lines[0], f"line 0 expected '0 0-19,22-23', got: {lines[0]!r}"
    assert "30 0-19,21-23 * * *" in lines[1], f"line 1 expected '30 0-19,21-23', got: {lines[1]!r}"

    print("✓ render_cron self-check passed: 11 assertions on compress_hours + in_protected_window + render_heartbeat_lines.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-check":
        sys.exit(_self_check())
    sys.exit(main())
