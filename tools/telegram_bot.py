#!/usr/bin/env python3
"""telegram_bot — Telegram bot CLI for inter-agent + Lei channel.

Refactored 2026-04-29 PM to use shared modules per Lei's single-source-of-truth
mandate:
- Bot API calls in _telegram_api.py
- Filename/frontmatter conventions in _channel.py
- env helpers in _common.py

This file is now thin glue + CLI subcommands. Behavior unchanged from the
pre-refactor version.

CLI:
    python tools/telegram_bot.py send "Hello from Borges" [--chat-id <id>]
    python tools/telegram_bot.py poll
    python tools/telegram_bot.py discover-chat-id
    python tools/telegram_bot.py populate-chat-id    # value-blind chat_id population

Reads bot_token + chat_id from $ENGRAM_HOME/secrets/telegram-bot.json.
Tracks last-seen update_id in $ENGRAM_HOME/telegram-state.json.

Send: cross-posts a Telegram-mirror file to inter-agent channel for counterpart
visibility (Telegram blocks bot-to-bot message visibility).
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Shared helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import engram_home, channel_dir, agent_name, counterpart_name  # noqa: E402
from _channel import (  # noqa: E402
    atomic_write, write_channel_file, utc_filename_timestamp, utc_iso_timestamp,
)
from _telegram_api import (  # noqa: E402
    send_message as _api_send_message,
    get_updates as _api_get_updates,
    summarize_message as _api_summarize,
)


# --- secrets + state ---

def _secrets_path() -> Path:
    return engram_home() / "secrets" / "telegram-bot.json"


def _state_path() -> Path:
    return engram_home() / "telegram-state.json"


def _load_secrets() -> dict:
    p = _secrets_path()
    if not p.exists():
        print(f"[telegram_bot] ERROR: secrets file missing at {p}", file=sys.stderr)
        sys.exit(2)
    with p.open() as f:
        return json.load(f)


def _save_secrets(d: dict) -> None:
    """Atomic write so chat_id population doesn't race with reads."""
    atomic_write(_secrets_path(), json.dumps(d, indent=2), mode=0o600)


def _load_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {"last_update_id": 0}
    with p.open() as f:
        return json.load(f)


def _save_state(s: dict) -> None:
    atomic_write(_state_path(), json.dumps(s, indent=2), mode=0o644)


# --- send + cross-post mirror ---

def send_message(text: str, chat_id: Optional[int] = None) -> dict:
    """Send a Telegram message + cross-post mirror to filesystem channel.

    Telegram blocks bot-to-bot visibility, so the mirror is how the
    counterpart agent sees what was sent. Mirror failure doesn't fail send.
    """
    secrets = _load_secrets()
    token = secrets.get("bot_token")
    if not token or token.startswith("PASTE"):
        print("[telegram_bot] ERROR: bot_token is unset or placeholder", file=sys.stderr)
        sys.exit(2)
    if chat_id is None:
        chat_id = secrets.get("chat_id")
    if chat_id is None:
        print("[telegram_bot] ERROR: chat_id missing — pass --chat-id or populate secrets",
              file=sys.stderr)
        sys.exit(2)

    result = _api_send_message(token, chat_id, text)

    if result.get("ok"):
        try:
            chat_title = result["result"].get("chat", {}).get("title", "")
            message_id = result["result"]["message_id"]
            _mirror_to_channel(text, message_id, chat_title)
        except Exception as e:
            # Best-effort mirror — don't fail send
            print(f"[telegram_bot] WARN: mirror write failed: {e}", file=sys.stderr)
    return result


def _mirror_to_channel(text: str, telegram_message_id: int, chat_title: str) -> Path:
    """Write a telegram-mirror file to the inter-agent channel.

    Filename: <UTC>_<agent>_telegram-mirror.md
    Mirror is recognized as substance for pulse purposes (per
    _channel.is_substance_filename) but distinguishable from native channel
    writes via the _telegram-mirror suffix.
    """
    return write_channel_file(
        channel_dir=channel_dir(),
        filename=f"{utc_filename_timestamp()}_{agent_name()}_telegram-mirror.md",
        frontmatter={
            "from": agent_name(),
            "to": counterpart_name(),
            "timestamp": utc_iso_timestamp(),
            "type": "telegram-mirror",
            "telegram_message_id": telegram_message_id,
            "telegram_chat_title": chat_title,
        },
        body=text,
        mode=0o644,
    )


# --- poll + discover + populate ---

def poll_updates(commit_offset: bool = True) -> dict:
    """Fetch updates since last-seen offset. If commit_offset, advance cursor."""
    secrets = _load_secrets()
    token = secrets.get("bot_token")
    if not token or token.startswith("PASTE"):
        print("[telegram_bot] ERROR: bot_token is unset or placeholder", file=sys.stderr)
        sys.exit(2)
    state = _load_state()
    offset = state.get("last_update_id", 0) + 1 if state.get("last_update_id") else None
    resp = _api_get_updates(token, offset=offset, long_poll_timeout=0)  # short poll for CLI
    updates = resp.get("result", [])
    if commit_offset and updates:
        state["last_update_id"] = max(u["update_id"] for u in updates)
        _save_state(state)
    return resp


def summarize_updates(resp: dict) -> list:
    """Extract human-readable summaries from a getUpdates response."""
    out = []
    for u in resp.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        if not msg:
            continue
        s = _api_summarize(msg)
        s["update_id"] = u["update_id"]
        # date isoformat for CLI display (separate from _api_summarize's unix timestamp)
        if s.get("date"):
            try:
                s["date"] = datetime.fromtimestamp(s["date"], tz=timezone.utc).isoformat()
            except Exception:
                pass
        out.append(s)
    return out


def discover_chat_ids() -> list:
    """Non-committing poll; return distinct chat_ids seen."""
    resp = poll_updates(commit_offset=False)
    msgs = summarize_updates(resp)
    seen: dict = {}
    for m in msgs:
        if m.get("chat_id") is not None:
            seen[m["chat_id"]] = m
    return list(seen.values())


def populate_chat_id() -> dict:
    """Discover chat_id from pending updates and write to secrets value-blind (per ls_0016)."""
    chats = discover_chat_ids()
    if not chats:
        return {"status": "no_pending",
                "hint": "Send a message in the group first, then re-run."}
    distinct_ids = {c["chat_id"] for c in chats if c.get("chat_id") is not None}
    if len(distinct_ids) > 1:
        return {"status": "ambiguous",
                "count": len(distinct_ids),
                "hint": "Multiple distinct chat_ids in pending updates. "
                        "Resolve by sending fresh messages in only the target group, "
                        "or set --chat-id manually."}
    chat_id = next(iter(distinct_ids))
    secrets = _load_secrets()
    if secrets.get("chat_id") == chat_id:
        return {"status": "already_set", "hint": "Secrets already had this chat_id."}
    secrets["chat_id"] = chat_id
    _save_secrets(secrets)
    return {"status": "set", "hint": "chat_id written to secrets file."}


# --- CLI ---

def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal Telegram bot integration")
    sub = parser.add_subparsers(dest="cmd", required=True)

    send_p = sub.add_parser("send", help="Send a message to the configured chat")
    send_p.add_argument("text", help="Message text")
    send_p.add_argument("--chat-id", type=int, default=None,
                        help="Override chat_id from secrets")

    sub.add_parser("poll", help="Poll for new updates and commit offset")
    sub.add_parser("discover-chat-id", help="Show chat_ids in pending updates without committing")
    sub.add_parser("populate-chat-id", help="Write discovered chat_id to secrets value-blind")

    args = parser.parse_args()

    if args.cmd == "send":
        result = send_message(args.text, chat_id=args.chat_id)
        if not result.get("ok"):
            err = {"ok": False, "description": result.get("description", "unknown")}
            print(json.dumps(err, indent=2), file=sys.stderr)
            return 1
        # Per ls_0016: don't echo chat_id in CLI output.
        print(json.dumps({"sent": True,
                          "message_id": result["result"]["message_id"]}, indent=2))
        return 0

    if args.cmd == "poll":
        resp = poll_updates(commit_offset=True)
        msgs = summarize_updates(resp)
        print(json.dumps({"count": len(msgs), "messages": msgs}, indent=2))
        return 0

    if args.cmd == "discover-chat-id":
        chats = discover_chat_ids()
        if not chats:
            print(json.dumps({"discovered": [],
                              "hint": "No pending updates. Send a message first."}, indent=2))
            return 0
        print(json.dumps({"discovered": chats}, indent=2))
        return 0

    if args.cmd == "populate-chat-id":
        result = populate_chat_id()
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in ("set", "already_set") else 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
