#!/usr/bin/env python3
"""telegram_dispatcher — long-polling daemon that dispatches @-mentions to the agent.

Architecture (v0.1):
    - Each agent runs its own dispatcher in a tmux session, polling its own
      bot's getUpdates via long-poll (timeout=60s; Telegram pushes within
      sub-second of message arrival).
    - For every new message:
        * Append to $ENGRAM_HOME/telegram-inbox.md (durable log; foreground
          and heartbeat scan on their own cadence for non-urgent messages).
        * If addressed-to-self (Form A @-mention, Form B reply-quote, Form C
          private DM), spawn a focused `claude -p` (or gemini -y -p) session.
        * Otherwise, no dispatch.

Refactored 2026-04-29 PM (Lei: "aggressively refactor common functionalities
into shared libraries"). All Telegram API calls live in _telegram_api.py; all
subprocess-spawn + failure classification lives in _dispatch.py; common channel
logic in _channel.py. This file is the orchestrator only.

Per-install env contract:
    AGENT_NAME=borges  COUNTERPART_NAME=mneme  ENGRAM_HOME=$HOME/.engram          AGENT_CLI=claude
    AGENT_NAME=mneme   COUNTERPART_NAME=borges ENGRAM_HOME=$HOME/.engram-gemini   AGENT_CLI=gemini
"""

import json
import os
import sys
import time
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Import shared helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import env, engram_home, channel_dir, agent_name, counterpart_name  # noqa: E402
from _channel import atomic_write, utc_iso_timestamp  # noqa: E402
from _telegram_api import (  # noqa: E402
    get_me_with_retry, get_updates, send_message, send_reaction,
    summarize_message, is_addressed_to_self,
)
from _dispatch import run_agent_subprocess  # noqa: E402


LONG_POLL_TIMEOUT = 60  # seconds — Telegram getUpdates timeout
INBOX_FILENAME = "telegram-inbox.md"
ACK_REACTION_EMOJI = "👀"  # OpenClaw-style ack: signals "got it, thinking"

# Per-day-persistent session per agent: deterministic UUID v5 derived from
# agent_name + UTC date. All Telegram chats (group + 1:1 DMs) on the same
# UTC day share ONE claude session — per Mneme's original design (msg
# 2026-04-29T19-35-00Z): "have both group and private context just in case
# I want to switch to 1:1 mid discussion. The topic on telegram would be
# unified." — but rotated per day so the session has a clean midnight
# boundary aligned with the coordinated sleep cycle. Cross-day context
# survives via ENGRAM, not via session resume.
SESSION_NAMESPACE_UUID = uuid.UUID("d05e3c4b-6e2a-5e2c-9f8b-7a4c1e3d5f6a")


def _session_uuid_for_telegram() -> str:
    """Derive a deterministic UUID per agent per UTC day.

    All Telegram dispatches on the same UTC date resume the same session;
    midnight crosses into a fresh session because the date string changes.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return str(uuid.uuid5(SESSION_NAMESPACE_UUID, f"telegram:agent:{agent_name()}:date:{today}"))


# --- paths + state ---

def _secrets_path() -> Path:
    return engram_home() / "secrets" / "telegram-bot.json"


def _state_path() -> Path:
    """Reuse the same state file telegram_bot.py uses for offset tracking."""
    return engram_home() / "telegram-state.json"


def _inbox_path() -> Path:
    return engram_home() / INBOX_FILENAME


def _agent_cli() -> str:
    """Which CLI to invoke for dispatch — 'claude' (Borges) or 'gemini' (Mneme)."""
    return env("AGENT_CLI", default="claude")


def _load_secrets() -> dict:
    p = _secrets_path()
    if not p.exists():
        print(f"[telegram_dispatcher] ERROR: secrets file missing at {p}", file=sys.stderr)
        sys.exit(2)
    with p.open() as f:
        return json.load(f)


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {"last_update_id": 0}
    with p.open() as f:
        return json.load(f)


def _save_state(s: dict) -> None:
    atomic_write(_state_path(), json.dumps(s, indent=2), mode=0o644)


# --- inbox logging ---

def _append_to_inbox(message_summary: dict, dispatched: bool) -> None:
    """Append a single-line entry to $ENGRAM_HOME/telegram-inbox.md."""
    path = _inbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"- [{message_summary['date']}] "
        f"chat={message_summary['chat_title']!r} "
        f"from={message_summary['from']!r} "
        f"text={message_summary['text']!r} "
        f"dispatched={dispatched}"
        "\n"
    )
    with path.open("a") as f:
        f.write(line)


# --- dispatch ---

def _dispatch_to_agent(message_summary: dict) -> None:
    """Spawn a focused agent invocation to respond to an addressed message.

    Refactored to use _dispatch.run_agent_subprocess + _telegram_api.send_message.
    Failure (non-zero exit, timeout, exception) → posts a brief error reason
    back to the original Telegram chat for visibility (no retry; user re-sends).
    """
    cli = _agent_cli()
    chat_type = message_summary.get('chat_type', '?')
    is_private = chat_type == 'private'
    chat_descriptor = (
        f"{'private DM with ' + message_summary['from'] if is_private else 'group ' + repr(message_summary['chat_title'])}"
    )
    source_chat_id = message_summary['chat_id']
    prompt = (
        f"Telegram dispatch — you were addressed in {chat_descriptor} "
        f"(chat_type={chat_type!r}) by {message_summary['from']!r} at "
        f"unix-time {message_summary['date']}. Their message:\n\n"
        f"  {message_summary['text']}\n\n"
        f"Compose a focused response (1-3 short paragraphs is plenty; this is a "
        f"phone-friendly chat). Read context as needed via your tools.\n\n"
        f"**ROUTE THE RESPONSE BACK TO THE SAME CHAT** (DM stays DM, group "
        f"stays group). Use the --chat-id flag explicitly:\n\n"
        f"  AGENT_NAME={agent_name()} COUNTERPART_NAME={counterpart_name()} "
        f"ENGRAM_HOME={engram_home()} INTER_AGENT_DIR={channel_dir()} "
        f"python3 $ALPHA_DIR/tools/telegram_bot.py "
        f"send --chat-id {source_chat_id} \"<your response text>\"\n\n"
        f"This auto-mirrors your response into the inter-agent filesystem channel "
        f"per the cross-post pattern. Then exit cleanly."
    )

    log_path = engram_home() / "logs" / "telegram-dispatcher.log"

    # Send ack-emoji reaction first — gives Lei immediate visual signal
    secrets = _load_secrets()
    token = secrets.get("bot_token", "")
    if token and not token.startswith("PASTE"):
        send_reaction(token, message_summary["chat_id"],
                      message_summary["message_id"], ACK_REACTION_EMOJI)

    # Persistent session per agent across all Telegram chats. With the
    # ANTHROPIC_API_KEY auth-resolution fix (now using OAuth/Max 20x), the
    # earlier 429 cascade from accumulated session payloads is resolved —
    # cross-call cache hits (within 5-min TTL) make resume even more efficient.
    # First dispatch creates the session via --session-id (an unused UUID;
    # claude creates it). Subsequent dispatches must resume via --resume
    # (claude rejects --session-id on existing UUIDs as "already in use").
    if cli == "claude":
        session_uuid = _session_uuid_for_telegram()
        # Detect session existence by globbing for its JSONL (claude's project
        # path encoding replaces both / and _ with -; glob is encoding-robust).
        projects_root = Path.home() / ".claude" / "projects"
        session_exists = bool(list(projects_root.glob(f"*/{session_uuid}.jsonl")))
        cmd = ["claude", "-p"]
        if session_exists:
            cmd += ["--resume", session_uuid]
        else:
            cmd += ["--session-id", session_uuid]
        cmd += [
            "--permission-mode", "bypassPermissions",
            "--setting-sources", "user,project,local",
            "--add-dir", str(engram_home()),
            "--add-dir", str(channel_dir()),
        ]
    elif cli == "gemini":
        # Gemini's --resume strictly errors on missing session (unlike claude's
        # --session-id create-or-resume). Keep fresh-session-per-dispatch for
        # gemini path; cross-dispatch memory comes from ENGRAM.
        cmd = ["gemini", "-y", "--model", "gemini-3-flash-preview", "-p", prompt]
    else:
        print(f"[telegram_dispatcher] ERROR: unknown AGENT_CLI={cli}", file=sys.stderr)
        return

    rc, _stdout, _stderr, failure_reason = run_agent_subprocess(
        cli=cli, cmd=cmd, prompt=prompt, log_path=log_path, timeout=180,
    )

    # On failure: post a brief error status back to Telegram for visibility
    if failure_reason:
        try:
            send_message(token, message_summary["chat_id"],
                         f"[dispatch failed: {failure_reason}]")
        except Exception as e:
            with log_path.open("a") as logf:
                logf.write(f"=== ERROR-POSTING-FAILED {utc_iso_timestamp()}: {e} ===\n")


# --- main loop ---

def main() -> int:
    secrets = _load_secrets()
    token = secrets.get("bot_token", "")
    if not token or token.startswith("PASTE"):
        print("[telegram_dispatcher] ERROR: bot_token unset or placeholder", file=sys.stderr)
        return 2

    own_username = get_me_with_retry(token)
    print(f"[telegram_dispatcher] starting; agent={agent_name()} bot={own_username}", flush=True)

    while True:
        try:
            state = _load_state()
            offset = state.get("last_update_id", 0) + 1 if state.get("last_update_id") else None

            resp = get_updates(token, offset=offset, long_poll_timeout=LONG_POLL_TIMEOUT)
            if not resp.get("ok"):
                print(f"[telegram_dispatcher] WARN getUpdates not ok: {resp}", flush=True)
                time.sleep(5)
                continue
            updates = resp.get("result", [])

            for u in updates:
                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue
                summary = summarize_message(msg)
                # Skip messages from other bots (Telegram blocks them anyway, defensive)
                if (msg.get("from", {}) or {}).get("is_bot"):
                    _append_to_inbox(summary, dispatched=False)
                    continue
                if is_addressed_to_self(msg, own_username):
                    print(f"[telegram_dispatcher] dispatch: msg_id={summary['message_id']} "
                          f"from={summary['from']!r}", flush=True)
                    _append_to_inbox(summary, dispatched=True)
                    _dispatch_to_agent(summary)
                else:
                    _append_to_inbox(summary, dispatched=False)

            if updates:
                state["last_update_id"] = max(u["update_id"] for u in updates)
                _save_state(state)

        except urllib.error.URLError as e:
            # HTTP 409 from getUpdates means Telegram still holds a server-side
            # long-poll session for this bot — typically because the previous
            # call timed out client-side (network jitter) but the server didn't
            # release. Retrying immediately gets another 409 in a self-
            # perpetuating loop. Sleep slightly longer than the long-poll
            # timeout so the stale session expires before we retry.
            # (2026-05-01: Mneme dispatcher thrashed ~14 min on this loop
            # after morning DNS/TCP-reset cascade until manually killed.)
            if isinstance(e, urllib.error.HTTPError) and e.code == 409:
                backoff = LONG_POLL_TIMEOUT + 15  # 75s
                print(f"[telegram_dispatcher] WARN 409 conflict — Telegram "
                      f"holds a stale long-poll session; sleeping {backoff}s "
                      f"for it to expire", flush=True)
                time.sleep(backoff)
            else:
                print(f"[telegram_dispatcher] WARN network: {e}", flush=True)
                time.sleep(5)
        except KeyboardInterrupt:
            print("[telegram_dispatcher] stopping (SIGINT)", flush=True)
            return 0
        except Exception as e:
            print(f"[telegram_dispatcher] ERROR loop iteration: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
