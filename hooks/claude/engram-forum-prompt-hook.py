#!/usr/bin/env python3
"""UserPromptSubmit hook: surface new forum threads to the agent.

On every prompt, queries GET /api/threads?since=<surfaced-cursor> and injects
a brief notice into additionalContext if new threads have appeared. Advances
the surfaced cursor after firing.

Two-cursor state (mirrors engram-inter-agent-prompt-hook.py):
  - forum-surfaced-cursor.txt: advances on every hook fire. Drives "new since
    last prompt." After the hook fires, threads that were "new" appear in the
    read-cursor window on the next prompt.
  - forum-read-cursor.txt: advances only when the agent explicitly reads a
    thread (via `forum read <id>`) or acknowledges via `forum status --ack`.
    This hook never touches the read cursor.

Cache: 30s file-based cache at ~/.engram/forum-hook-cache.json to avoid
hammering the server on rapid successive prompts. Stale-cache fallback on
server-unreachable (never break the hook chain if the forum is down).

Silent no-op conditions:
  - forum.url and agent_name not configured.
  - Server unreachable (uses cached data if available, else silent skip).
  - No new threads since last prompt.

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
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
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


def _save_cache(threads: list) -> None:
    """Save fetched threads to the hook cache."""
    dest = Path(HOOK_CACHE_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        payload = {"fetched_at": time.time(), "threads": threads}
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

    # ── Read surfaced cursor ──────────────────────────────────────────────────
    surfaced_cursor = _read_cursor(SURFACED_CURSOR_PATH)
    since_str = surfaced_cursor.strftime("%Y-%m-%dT%H:%M:%SZ") if surfaced_cursor else None

    # ── Fetch threads (with cache + stale-cache fallback) ────────────────────
    threads: Optional[list] = None

    cached = _load_cache()
    if cached is not None:
        # Cache is fresh — display it, but do NOT advance the surfaced cursor.
        # The live fetch that populated this cache already advanced the cursor
        # to its fetch time. Advancing again to `now` here would step the
        # cursor past posts made AFTER that fetch but before cache-expiry, so
        # the next live fetch's since=cursor would exclude them and they'd
        # never surface. (Twin of the unreachable-no-cache guard below.)
        threads = cached.get("threads", [])
    else:
        # Live fetch with since=current surfaced cursor.
        threads = _fetch_threads(forum_url, since_str, agent_name)
        if threads is not None:
            _save_cache(threads)
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
            except (OSError, json.JSONDecodeError):
                threads = None

    # ── Exit silently if nothing to report ────────────────────────────────────
    if not threads:
        sys.exit(0)

    # ── Count new threads since the surfaced cursor ───────────────────────────
    # Filter threads that are genuinely newer than the cursor (server's since=
    # filter uses >=, so we need to count carefully)
    new_count = len(threads)
    if new_count == 0:
        sys.exit(0)

    # ── Build injection block ─────────────────────────────────────────────────
    post_word = "post" if new_count == 1 else "posts"
    context = (
        f"\U0001f4e2 {new_count} new forum {post_word} since last read"
        f" — run `forum read <id>` or `forum status` to catch up."
    )

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(response))


if __name__ == "__main__":
    main()
