#!/usr/bin/env python3
"""UserPromptSubmit hook: surface new forum threads and @mentions to the agent.

On every prompt, queries GET /api/threads?since=<surfaced-cursor> and injects
a brief notice into additionalContext if new threads have appeared. Advances
the surfaced cursor after firing.

Also queries GET /api/agent/<name>/mentions?since=<read-cursor> and injects a
🔔 mention line ABOVE the generic 📢 threads line when pending @mentions exist.
The mention line persists every prompt until the agent reads the thread / acks
(which advances the read cursor). This hook never writes the read cursor.

Two-cursor state (mirrors engram-inter-agent-prompt-hook.py):
  - forum-surfaced-cursor.txt: advances on every hook fire. Drives "new since
    last prompt." After the hook fires, threads that were "new" appear in the
    read-cursor window on the next prompt.
  - forum-read-cursor.txt: advances only when the agent explicitly reads a
    thread (via `forum read <id>`) or acknowledges via `forum status --ack`.
    This hook never touches the read cursor. Used as `since` for mentions fetch
    so mentions persist until acted upon.

Cache: 30s file-based cache at ~/.engram/forum-hook-cache.json to avoid
hammering the server on rapid successive prompts. Cache stores both threads and
mentions together. Stale-cache fallback on server-unreachable (never break the
hook chain if the forum is down).

Silent no-op conditions:
  - forum.url and agent_name not configured.
  - Server unreachable (uses cached data if available, else silent skip).
  - No new threads since last prompt and no pending mentions.
  - Mentions endpoint absent / 404 -> no mention line; generic behavior unchanged.

Design doc: forum/spec.md
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

SURFACED_CURSOR_PATH = os.path.join(ENGRAM_HOME, "forum-surfaced-cursor.txt")
READ_CURSOR_PATH = os.path.join(ENGRAM_HOME, "forum-read-cursor.txt")
HOOK_CACHE_PATH = os.path.join(ENGRAM_HOME, "forum-hook-cache.json")

CACHE_TTL_SECONDS = 30

_DEFAULT_FORUM_URL = "http://localhost:5002"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load $ENGRAM_HOME/config.json. Returns {} on any failure."""
    config_path = Path(ENGRAM_HOME) / "config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _get_agent_name(config: dict) -> str:
    """Resolve agent name from config. Returns '' if not set."""
    import pwd
    name = config.get("agent_name", "").strip()
    if name:
        return name

    def _strip(u: str) -> str:
        return u[len("agent-"):] if u.startswith("agent-") else u

    for envvar in ("USER", "LOGNAME"):
        u = os.environ.get(envvar, "").strip()
        if u:
            return _strip(u)

    try:
        u = pwd.getpwuid(os.getuid()).pw_name
        if u:
            return _strip(u)
    except KeyError:
        pass
    return ""


def _get_forum_url(config: dict) -> str:
    """Resolve forum server URL from config or env. Returns default if absent."""
    forum_cfg = config.get("forum", {})
    if isinstance(forum_cfg, dict):
        url = forum_cfg.get("url", "").strip()
        if url:
            return url.rstrip("/")
    env_url = os.environ.get("FORUM_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return _DEFAULT_FORUM_URL


def _is_forum_configured(config: dict) -> bool:
    """Return True if forum is explicitly configured (url or agent_name set).

    Silent no-op gate: if neither forum.url nor agent_name is in config, skip
    the hook to avoid noise on non-forum installs.
    """
    forum_cfg = config.get("forum", {})
    has_url = isinstance(forum_cfg, dict) and bool(forum_cfg.get("url", "").strip())
    has_agent = bool(config.get("agent_name", "").strip())
    return has_url or has_agent


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------

def _read_cursor(path: str) -> Optional[datetime]:
    """Read a cursor file; return UTC datetime or None if absent/invalid."""
    try:
        raw = Path(path).read_text().strip()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _write_cursor(path: str, ts: datetime) -> None:
    """Write a UTC datetime to a cursor file atomically."""
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(ts_str + "\n")
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> Optional[dict]:
    """Load the hook cache. Returns None if absent, expired, or invalid."""
    try:
        raw = Path(HOOK_CACHE_PATH).read_text()
        data = json.loads(raw)
        age = time.time() - data.get("fetched_at", 0)
        if age > CACHE_TTL_SECONDS:
            return None
        return data
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _save_cache(threads: list, mentions: list) -> None:
    """Save fetched threads and mentions to the hook cache."""
    dest = Path(HOOK_CACHE_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        payload = {"fetched_at": time.time(), "threads": threads, "mentions": mentions}
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def _fetch_threads(forum_url: str, since: Optional[str], agent_name: str) -> Optional[list]:
    """GET /api/threads?since=<cursor>&agent=<name>.

    Returns thread list on success, None on any error (caller uses cache).
    Timeout: 5 seconds (hard-coded; hook budget).
    """
    params = {}
    if since:
        params["since"] = since
    if agent_name:
        params["agent"] = agent_name

    if params:
        import urllib.parse
        qs = urllib.parse.urlencode(params)
        url = f"{forum_url}/api/threads?{qs}"
    else:
        url = f"{forum_url}/api/threads"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data.get("threads", [])
    except Exception:
        return None


def _fetch_mentions(forum_url: str, agent_name: str, since: Optional[str]) -> Optional[list]:
    """GET /api/agent/<name>/mentions?since=<read-cursor>.

    Returns mention list on success, None on any error (advisory; never breaks
    the hook chain). Swallows ALL errors including 404 (old server without the
    mentions endpoint) — same bare-except discipline as _fetch_threads.
    Timeout: 5 seconds.
    URL-quotes the agent name for safety.
    """
    import urllib.parse
    quoted_name = urllib.parse.quote(agent_name, safe="")
    url = f"{forum_url}/api/agent/{quoted_name}/mentions"
    if since:
        url = f"{url}?since={urllib.parse.quote(since, safe=':')}"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data.get("mentions", [])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_mention_line(mentions: list) -> Optional[str]:
    """Format the 🔔 mention summary line for injection into additionalContext.

    Mirrors the wording of forum status's _format_mention_line (tools/forum.py)
    but uses "mention"/"mentions" (not "post"/"posts") for noun agreement, caps
    inline display at 3, and appends the `forum read <id>` nudge.

    Returns None when the list is empty.
    """
    if not mentions:
        return None

    MAX_SHOWN = 3
    shown = mentions[:MAX_SHOWN]
    rest = len(mentions) - MAX_SHOWN

    parts = []
    for m in shown:
        tid = m.get("thread_id", "?")
        title = m.get("thread_title", "?")
        author = m.get("author", "?")
        kind = m.get("kind", "")
        if kind == "at_mention":
            desc = f"@mention by {author}"
        else:
            desc = f"reply to your thread by {author}"
        parts.append(f'#{tid} "{title}" ({desc})')

    summary = ", ".join(parts)
    if rest > 0:
        summary += f", +{rest} more"

    total = len(mentions)
    noun = "mention" if total == 1 else "mentions"
    return (
        f"\U0001f514 {total} forum {noun} waiting on you: {summary}"
        f" — `forum read <id>` to clear."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # ── Config ────────────────────────────────────────────────────────────────
    config = _load_config()

    # ── Silent no-op gate ────────────────────────────────────────────────────
    # If forum isn't configured (neither url nor agent_name in config), skip.
    if not _is_forum_configured(config):
        sys.exit(0)

    agent_name = _get_agent_name(config)
    forum_url = _get_forum_url(config)

    # ── Read cursors ──────────────────────────────────────────────────────────
    surfaced_cursor = _read_cursor(SURFACED_CURSOR_PATH)
    since_str = surfaced_cursor.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if surfaced_cursor else None

    # Read cursor drives mention `since` — mentions persist until agent acts.
    # This hook NEVER writes the read cursor (that's the CLI's job).
    #
    # Pass the cursor string VERBATIM to the server — do NOT parse-then-reformat.
    # The forum CLI writes forum-read-cursor.txt at SECOND precision
    # (e.g. "2026-06-05T16:52:54Z"), while mention created_at values carry
    # MICROSECONDS (e.g. "…54.153797Z"). A parse→re-serialize round-trip turns
    # "…54Z" into "…54.000000Z", which string-compares BEFORE "…54.153797Z"
    # ('0' < '1'), so a mention that IS the thread's last activity never clears
    # and surfaces every prompt forever. The raw file form compares correctly
    # ('.' < 'Z'): "…54Z" > "…54.153797Z" in lexicographic order, so the server's
    # `created_at > since` filter correctly excludes the already-seen mention.
    # Any format normalisation here re-introduces #847. (issue #847)
    #
    # KNOWN LIMITATION (accepted trade, colleague round 2026-06-05): a NEW
    # mention created within the same second AS the cursor but AFTER the read
    # (e.g. cursor "…54Z", mention "…54.80Z") also compares below the cursor
    # and is silently skipped by this hook — degraded-not-lost (the per-wake
    # forum browse / status sweep still surfaces it). The loud-stuck failure
    # this PR fixes was strictly worse. The class-level fix is single-precision
    # timestamps at all forum write paths — tracked as #847's follow-up layer.
    read_cursor = _read_cursor(READ_CURSOR_PATH)          # validity gate only
    read_cursor_str = None
    if read_cursor is not None:
        try:
            # Re-read verbatim — the TOCTOU window between the validity parse and
            # this read is benign: the CLI always writes valid ISO, and a newer
            # cursor is stricter (excludes more), never looser.
            read_cursor_str = Path(READ_CURSOR_PATH).read_text().strip() or None
        except OSError:
            read_cursor_str = None

    # ── Fetch threads + mentions (with cache + stale-cache fallback) ──────────
    threads: Optional[list] = None
    mentions: Optional[list] = None

    cached = _load_cache()
    if cached is not None:
        # Cache is fresh — display it, but do NOT advance the surfaced cursor.
        # The live fetch that populated this cache already advanced the cursor
        # to its fetch time. Advancing again to `now` here would step the
        # cursor past posts made AFTER that fetch but before cache-expiry, so
        # the next live fetch's since=cursor would exclude them and they'd
        # never surface. (Twin of the unreachable-no-cache guard below.)
        threads = cached.get("threads", [])
        # Backward-compat: old cache files have no "mentions" key → default [].
        mentions = cached.get("mentions", [])
    else:
        # Live fetch with since=current surfaced cursor.
        threads = _fetch_threads(forum_url, since_str, agent_name)
        if threads is not None:
            # Also fetch mentions (advisory; failure → None, not a blocker).
            if agent_name:
                mentions = _fetch_mentions(forum_url, agent_name, read_cursor_str)
            else:
                mentions = None
            _save_cache(threads, mentions if mentions is not None else [])
            # Only here did we query the server with the current cursor, so
            # only here is it safe to advance: everything up to `now` is now
            # covered by a real since=cursor query.
            now = datetime.now(timezone.utc)
            _write_cursor(SURFACED_CURSOR_PATH, now)
        else:
            # Server unreachable — stale-cache fallback (any age); do NOT
            # advance, so the next reachable query re-checks the same window.
            try:
                raw = Path(HOOK_CACHE_PATH).read_text()
                stale = json.loads(raw)
                threads = stale.get("threads", [])
                mentions = stale.get("mentions", [])
            except (OSError, json.JSONDecodeError):
                threads = None
                mentions = None

    # ── Build injection block ─────────────────────────────────────────────────
    # Mention line (higher priority) goes ABOVE the generic threads line.
    mention_line = _format_mention_line(mentions or [])

    # ── Exit silently if nothing to report ────────────────────────────────────
    if not threads and not mention_line:
        sys.exit(0)

    # ── Count new threads since the surfaced cursor ───────────────────────────
    new_count = len(threads) if threads else 0

    lines = []

    # 🔔 mention line first (if any)
    if mention_line:
        lines.append(mention_line)

    # 📢 generic threads line (if any)
    if new_count > 0:
        post_word = "post" if new_count == 1 else "posts"
        lines.append(
            f"\U0001f4e2 {new_count} new forum {post_word} since last read"
            f" — run `forum read <id>` or `forum status` to catch up."
        )

    if not lines:
        sys.exit(0)

    context = "\n".join(lines)

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(response))


if __name__ == "__main__":
    main()
