"""_telegram_api — shared Telegram Bot API helpers.

Centralizes raw HTTP calls used by:
- telegram_bot.py (send, poll, populate-chat-id, discover-chat-id)
- telegram_dispatcher.py (long-poll, ack reaction, getMe at startup, error-post-back)
- channel_dispatcher.py (future: post failure-back to telegram for hot-seat)

Stdlib only (urllib + json) — no pip dependencies.

Single source of truth: any change to Telegram API endpoints, retry logic,
or request shape lands here.
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


# --- base call helper ---

def _api_url(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def call(token: str, method: str, params: Optional[dict] = None,
         timeout: int = 30) -> dict:
    """Call a Telegram Bot API method. Returns parsed JSON response dict.

    Caller checks `result.get("ok")` and `result.get("result")` per Telegram's
    response envelope. Raises urllib.error.URLError on network failures.
    """
    url = _api_url(token, method)
    if params:
        data = urllib.parse.urlencode(params).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


# --- specific API methods ---

def get_me(token: str) -> dict:
    """getMe — returns bot info. Used to discover own bot username."""
    return call(token, "getMe")


def get_me_with_retry(token: str, max_attempts: int = 5) -> str:
    """Discover own bot username with exponential backoff on transient errors.

    Returns the bot username (not the full envelope). sys.exit(2) on hard failure.
    Used by long-running daemons that must not crash on startup TCP-reset.
    """
    last_err = None
    for attempt in range(max_attempts):
        try:
            data = get_me(token)
            if not data.get("ok"):
                print(f"[_telegram_api] getMe returned not-ok: {data}", file=sys.stderr)
                sys.exit(2)
            return data["result"]["username"]
        except (urllib.error.URLError, ConnectionResetError, OSError) as e:
            last_err = e
            backoff = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
            print(f"[_telegram_api] getMe attempt {attempt + 1} failed: {e}; "
                  f"retrying in {backoff}s", file=sys.stderr, flush=True)
            time.sleep(backoff)
    print(f"[_telegram_api] getMe failed after {max_attempts} retries; last: {last_err}",
          file=sys.stderr)
    sys.exit(2)


def send_message(token: str, chat_id: int, text: str) -> dict:
    """sendMessage — post a text message to a chat. Returns full response envelope."""
    return call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
    })


def send_reaction(token: str, chat_id: int, message_id: int, emoji: str) -> bool:
    """setMessageReaction — react to a message with an emoji. Best-effort.

    Returns True on success, False on failure. Failure does not raise — used
    for ack-feedback where silent failure is acceptable.
    """
    try:
        data = call(token, "setMessageReaction", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
        }, timeout=5)
        return bool(data.get("ok"))
    except Exception as e:
        print(f"[_telegram_api] setMessageReaction failed: {e}", file=sys.stderr)
        return False


def get_updates(token: str, offset: Optional[int] = None,
                long_poll_timeout: int = 60) -> dict:
    """getUpdates — fetch new updates. With long_poll_timeout > 0, the API
    holds the request open until a message arrives or timeout (seconds).

    Caller checks `data.get("ok")` and iterates `data.get("result", [])`.
    Caller is responsible for committing offset (passing offset = max(update_id) + 1
    on the next call) to acknowledge consumed updates.
    """
    params: dict = {"timeout": long_poll_timeout}
    if offset:
        params["offset"] = offset
    return call(token, "getUpdates", params, timeout=long_poll_timeout + 10)


def summarize_message(msg: dict) -> dict:
    """Extract human-readable fields from a getUpdates message dict.

    Returns a flat dict suitable for logging, inbox entries, dispatch context.
    """
    chat = msg.get("chat", {}) or {}
    sender = msg.get("from", {}) or {}
    return {
        "message_id": msg.get("message_id"),
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type", ""),
        "chat_title": chat.get("title", ""),
        "from": (sender.get("first_name") or "").strip()
                or (sender.get("username") or "").strip()
                or f"id_{sender.get('id', '?')}",
        "from_id": sender.get("id"),
        "is_bot": sender.get("is_bot", False),
        "text": msg.get("text", "") or "",
        "date": msg.get("date"),
    }


# --- addressed-to-self detection (for dispatchers) ---

def is_addressed_to_self(message: dict, own_username: str) -> bool:
    """True if the message is addressed to our bot.

    Three trigger forms (matches OpenClaw + Telegram Bot API conventions):

    A. **@-mention of own bot** in the message text. Uses Telegram's
       `entities` field (correct) rather than text-search (fragile).

    B. **Reply to a message originally sent by own bot.** When a user
       reply-quotes one of our outbound messages, the parent's
       `reply_to_message.from.username` matches our bot username.

    C. **1:1 private chat with the bot.** When `chat.type == "private"`,
       every human message is implicitly addressed to the bot — there's
       no other recipient.
    """
    # Form C: private (1:1) chat — every human message is for the bot
    chat_type = (message.get("chat") or {}).get("type", "")
    if chat_type == "private":
        return True

    text = message.get("text", "") or ""
    expected = f"@{own_username}".lower()

    # Form A: @-mention via entities
    for ent in message.get("entities", []):
        if ent.get("type") != "mention":
            continue
        offset = ent.get("offset", 0)
        length = ent.get("length", 0)
        mention_text = text[offset:offset + length].lower()
        if mention_text == expected:
            return True

    # Form B: reply-to-own-bot
    reply_to = message.get("reply_to_message", {}) or {}
    parent_sender = (reply_to.get("from") or {}).get("username", "")
    if parent_sender and parent_sender.lower() == own_username.lower():
        return True

    return False
