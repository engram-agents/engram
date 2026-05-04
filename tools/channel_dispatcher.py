#!/usr/bin/env python3
"""channel_dispatcher — hot-seat real-time inter-agent dispatcher.

Watches $INTER_AGENT_DIR (default /home/agents-shared/inter-agent) for new
substance files from the counterpart
agent. When one arrives, dispatches a focused agent invocation to read +
respond, mirroring the same shape as telegram_dispatcher but for the
filesystem channel substrate.

v0.1 hot-seat layer per ob_1379 (Lei's 3-layer framing): channel (async,
existing) / GitHub-scaffold / **local-hotseat (this file)**. Sub-second
latency for back-and-forth thinking; the cron-poll heartbeat layer remains
the slower, durable cadence for routine check-ins.

Architecture mirrors telegram_dispatcher:
- Long-running daemon (tmux session)
- Watches a notification source (filesystem inotify here vs Telegram getUpdates)
- New addressed-to-self event → spawn focused agent invocation via _dispatch
- Failure handling: log + (here) write a status file to channel for visibility

Per-install env contract (same as telegram_dispatcher):
    AGENT_NAME=borges  COUNTERPART_NAME=mneme  ENGRAM_HOME=$HOME/.engram          AGENT_CLI=claude
    AGENT_NAME=mneme   COUNTERPART_NAME=borges ENGRAM_HOME=$HOME/.engram-gemini   AGENT_CLI=gemini

Hot-seat trigger filter: only fires on counterpart's NATIVE substance
(filename pattern `<UTC>_<counterpart>.md`) with frontmatter `to: <self>`.
Skips telegram-mirrors, pulses, and self's own outbound. Trigger frontmatter
field `urgency: hot` could be added in v0.2 to distinguish "respond now"
from "log for routine cadence."

Polling-based watch (not inotify) for stdlib-only portability — checks
channel dir every 2 seconds. inotify would be sub-100ms but adds a
pyinotify dependency; for human-paced inter-agent thinking, 2s is fine.
"""

import os
import sys
import time
from pathlib import Path

# Import shared helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import env, engram_home, channel_dir, agent_name, counterpart_name  # noqa: E402
from _channel import (  # noqa: E402
    is_substance_filename, is_mirror_filename, read_frontmatter, atomic_write,
    utc_iso_timestamp,
)
from _dispatch import run_agent_subprocess  # noqa: E402
from _session_context import (  # noqa: E402
    load_session, is_hot_seat_eligible, touch_activity,
)


WATCH_INTERVAL_SECONDS = 2  # polling cadence; sub-100ms via inotify would be v0.2


def _state_path() -> Path:
    """Track the latest seen channel filename so we don't re-dispatch on restart."""
    return engram_home() / "channel-dispatcher-state.json"


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {"last_seen_filename": ""}
    import json
    return json.loads(p.read_text())


def _save_state(state: dict) -> None:
    import json
    atomic_write(_state_path(), json.dumps(state, indent=2), mode=0o644)


def _agent_cli() -> str:
    return env("AGENT_CLI", default="claude")


def _should_dispatch(filename: str, channel: Path) -> tuple:
    """Decide whether to fire a hot-seat dispatch for this channel file.

    Returns (should_dispatch: bool, reason: str, frontmatter: dict).

    Per Mneme's session-context design (msg 19:35Z, 2026-04-29):
    - Counterpart substance addressed to self → eligible
    - If frontmatter has `session_id`: lookup session-context file, dispatch
      ONLY if session is hot-seat-eligible (active + within TTL window)
    - If no session_id: no hot-seat fire (cron-poll heartbeat handles routine)
    - Defensive: never fire on own outbound, never on telegram-mirrors

    The `from: <self>` defensive check protects against filename-spoofing
    (someone writes `<UTC>_<counterpart>.md` but frontmatter says `from: <self>`).
    """
    cp = counterpart_name()
    self_name = agent_name()

    if not is_substance_filename(filename, cp):
        return (False, "not counterpart substance (filename pattern)", {})
    if is_mirror_filename(filename, cp):
        return (False, "telegram-mirror (no dispatch needed)", {})

    front = read_frontmatter(channel / filename)
    to_field = (front.get("to") or "").strip().lower()
    from_field = (front.get("from") or "").strip().lower()

    # Defensive: never fire on own outbound (filename spoof guard)
    if from_field == self_name.lower():
        return (False, "frontmatter from: matches self", front)
    # Must be addressed to self
    if to_field != self_name.lower():
        return (False, f"frontmatter to:{to_field!r} not self", front)

    # Session-context gating per Mneme's design
    session_id = (front.get("session_id") or "").strip()
    if not session_id:
        # Backward-compat: no session_id = no hot-seat fire (cron-poll handles routine)
        return (False, "no session_id (cron-poll handles)", front)
    session = load_session(channel, session_id)
    if not session:
        return (False, f"session_{session_id}.json missing (cron-poll handles)", front)

    # Participants validation
    participants = [p.lower() for p in session.get("participants", [])]
    if from_field not in participants:
        return (False, f"sender {from_field!r} not in session participants {participants}", front)

    if not is_hot_seat_eligible(session):
        status = session.get("status", "?")
        return (False, f"session not hot-seat-eligible (status={status})", front)

    return (True, f"session {session_id} active, hot-seat fire", front)


def _dispatch(filename: str, channel: Path) -> None:
    """Spawn focused agent invocation to read + respond to the new channel message."""
    cli = _agent_cli()
    full_path = channel / filename

    prompt = (
        f"Hot-seat dispatch — {counterpart_name()} just posted a substance "
        f"message to the inter-agent channel addressed to you ({agent_name()}).\n\n"
        f"File: {full_path}\n\n"
        f"Read the message, compose a response, and post it back to the "
        f"inter-agent channel via a new file at "
        f"$INTER_AGENT_DIR/<UTC>_{agent_name()}.md with proper frontmatter "
        f"(from: {agent_name()}, to: {counterpart_name()}, timestamp, "
        f"re: {filename}). Use atomic write (temp+rename) to avoid partial-read "
        f"races. After posting, exit cleanly. Skip if your read concludes the "
        f"message is informational and warrants no response (channel-noise discipline)."
    )

    log_path = engram_home() / "logs" / "channel-dispatcher.log"

    if cli == "claude":
        cmd = [
            "claude", "-p",
            "--permission-mode", "bypassPermissions",
            "--setting-sources", "user,project,local",
            "--add-dir", str(engram_home()),
            "--add-dir", str(channel),
        ]
    elif cli == "gemini":
        cmd = ["gemini", "-y", "--model", "gemini-3-flash-preview", "-p", prompt]
    else:
        print(f"[channel_dispatcher] ERROR: unknown AGENT_CLI={cli}", file=sys.stderr)
        return

    rc, _stdout, _stderr, failure_reason = run_agent_subprocess(
        cli=cli, cmd=cmd, prompt=prompt, log_path=log_path, timeout=180,
    )

    # On failure: write a status file to the channel for counterpart visibility
    if failure_reason:
        from _channel import write_channel_file, utc_filename_timestamp
        ts_filename = utc_filename_timestamp()
        write_channel_file(
            channel_dir=channel,
            filename=f"{ts_filename}_{agent_name()}.md",
            frontmatter={
                "from": agent_name(),
                "to": counterpart_name(),
                "timestamp": utc_iso_timestamp(),
                "re": filename,
                "type": "dispatch-failure-status",
            },
            body=(f"[channel-dispatch failure: {failure_reason}]\n\n"
                  f"My hot-seat dispatcher tried to compose a response to your "
                  f"{filename} but the agent invocation failed. Will retry on "
                  f"the next regular heartbeat cycle when capacity allows."),
        )


def main() -> int:
    channel = channel_dir()
    if not channel.exists():
        print(f"[channel_dispatcher] ERROR: channel dir missing: {channel}", file=sys.stderr)
        return 2

    print(f"[channel_dispatcher] starting; agent={agent_name()} "
          f"watching={channel} interval={WATCH_INTERVAL_SECONDS}s", flush=True)

    state = _load_state()
    last_seen = state.get("last_seen_filename", "")

    # On startup, advance to the current latest counterpart substance to avoid
    # firing on backlog. Hot-seat is for NEW messages.
    cp = counterpart_name()
    initial_filenames = sorted(
        e.name for e in channel.iterdir()
        if e.is_file() and is_substance_filename(e.name, cp)
    )
    if initial_filenames:
        state["last_seen_filename"] = initial_filenames[-1]
        last_seen = state["last_seen_filename"]
        _save_state(state)
        print(f"[channel_dispatcher] startup: advanced cursor to {last_seen} "
              f"(skipping backlog of {len(initial_filenames)} substance files)", flush=True)

    while True:
        try:
            new_files = sorted(
                e.name for e in channel.iterdir()
                if e.is_file() and is_substance_filename(e.name, cp) and e.name > last_seen
            )
            for filename in new_files:
                should, reason, frontmatter = _should_dispatch(filename, channel)
                if should:
                    print(f"[channel_dispatcher] dispatch: {filename} ({reason})", flush=True)
                    _dispatch(filename, channel)
                    # Touch activity to keep session warm during back-and-forth
                    session_id = (frontmatter.get("session_id") or "").strip()
                    if session_id:
                        touch_activity(channel, session_id)
                else:
                    print(f"[channel_dispatcher] skip: {filename} — {reason}",
                          flush=True)
                last_seen = filename
                state["last_seen_filename"] = last_seen
                _save_state(state)
            time.sleep(WATCH_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("[channel_dispatcher] stopping (SIGINT)", flush=True)
            return 0
        except Exception as e:
            print(f"[channel_dispatcher] ERROR loop iteration: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
