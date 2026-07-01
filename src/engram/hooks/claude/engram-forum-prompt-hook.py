#!/usr/bin/env python3
"""UserPromptSubmit hook: surface new forum threads and @mentions to the agent.

On every prompt, queries GET /api/threads?since=<surfaced-cursor> and injects
a brief notice into additionalContext if new threads have appeared. Advances
the surfaced cursor after firing.

Also queries GET /api/agent/<name>/inbox and filters to kind=at_mention items,
then injects a 🔔 mention line ABOVE the generic 📢 threads line when pending
@mentions exist. The inbox endpoint uses server-side per-thread read marks
(p.id > COALESCE(r.last_read_post_id, 0)), so reading a thread server-side
clears the mention automatically — no local read-cursor involved.

Also auto-publishes derived agent status to /api/agents/status on a throttled
schedule (Q1–Q4 from spec #1077 part 1): synchronous POST with timeout=2,
swallow-all-errors, throttle on max(cadence_seconds, 60), always-publish-on-
state-change, mid-sleep guard. The publish never breaks the existing thread/
mention surfacing.

Two-cursor state for threads (surfaced cursor is still hook-owned):
  - forum-surfaced-cursor.txt: advances on every hook fire. Drives "new since
    last prompt." After the hook fires, threads that were "new" appear in the
    surfaced window on the next prompt.
  - forum-read-cursor.txt: vestigial — no longer consumed by this hook for
    mention queries. The `cursor` subcommand stays for manual override/back-compat.

Cache: 30s file-based cache at ~/.engram/forum-hook-cache.json to avoid
hammering the server on rapid successive prompts. Cache stores both threads and
inbox mentions together. Stale-cache fallback on server-unreachable (never break
the hook chain if the forum is down).

Publish state: separate ~/.engram/forum-status-publish.json tracks last-published
status with its own throttle interval (never shares the 30s-TTL cache).

Silent no-op conditions:
  - forum.url and agent_name not configured.
  - Server unreachable (uses cached data if available, else silent skip).
  - No new threads since last prompt and no pending @mentions.
  - Inbox endpoint absent / error -> no mention line; generic behavior unchanged.
  - Status publish failure (forum-status-publish.json) -> silently swallowed;
    existing output unaffected.

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
# READ_CURSOR_PATH is no longer consumed by this hook for mention queries.
# Mention state is now derived from GET /api/agent/<name>/inbox, which uses
# server-side per-thread read marks — no local cursor needed.
# The `forum cursor` subcommand in tools/forum.py retains READ_CURSOR_PATH
# for manual override / back-compat only.
HOOK_CACHE_PATH = os.path.join(ENGRAM_HOME, "forum-hook-cache.json")

# Publish state file — separate from the 30s-TTL cache; its own throttle interval.
STATUS_PUBLISH_PATH = os.path.join(ENGRAM_HOME, "forum-status-publish.json")

CACHE_TTL_SECONDS = 30

_DEFAULT_FORUM_URL = "http://localhost:5002"

# ---------------------------------------------------------------------------
# _status_derive import (spec §2, Q2)
#
# Resolve tools/ in BOTH topologies: plugin (CLAUDE_PLUGIN_ROOT/tools) AND
# source-tree/dev (hooks/claude/ -> ../../tools). Keying solely off
# CLAUDE_PLUGIN_ROOT would silently no-op auto-publish in a dev checkout —
# exactly where the feature is most likely to be dogfooded (Kepler S1, #1087).
# If the import fails for ANY reason, derive_own_status is set to None and the
# publish silently no-ops — the existing thread/mention surfacing is unaffected.
# ---------------------------------------------------------------------------

_plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
_candidates = []
if _plugin_root:
    _candidates.append(Path(_plugin_root) / "tools")          # plugin: hooks/ -> tools/
for _parent in Path(__file__).resolve().parents:              # walk-all-parents fallback
    _candidates.append(_parent / "tools")
_tools_dir = next((c for c in _candidates if (c / "_status_derive.py").exists()), None)
if _tools_dir is not None and str(_tools_dir) not in sys.path:
    sys.path.insert(0, str(_tools_dir))
try:
    from _status_derive import derive_own_status, _read_loop_mode
except Exception:
    derive_own_status = None  # publish becomes a no-op; never break the hook
    _read_loop_mode = None    # type: ignore[assignment]


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
# Publish state helpers (separate from the 30s cache)
# ---------------------------------------------------------------------------

def _load_publish_state() -> Optional[dict]:
    """Load the last-published status state. Returns None if absent/invalid."""
    try:
        return json.loads(Path(STATUS_PUBLISH_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_publish_state(state: str, activity: Optional[str],
                        queue: list, cadence: Optional[int],
                        published_at: float) -> None:
    """Save publish state atomically (same discipline as _save_cache)."""
    dest = Path(STATUS_PUBLISH_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        payload = {
            "state": state,
            "activity": activity,
            "queue": queue,
            "cadence": cadence,
            "published_at": published_at,
        }
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Publish decision helpers (pure — unit-testable without a live server)
# ---------------------------------------------------------------------------

def _should_publish(
    last: Optional[dict],
    current_tuple: tuple,
    now: float,
    throttle: float,
) -> bool:
    """Return True if a status publish should fire.

    Rules (spec Q3):
      (a) No last file → publish.
      (b) Status tuple changed since last publish → publish.
      (c) Unchanged AND within throttle window → skip.
      (d) Unchanged AND past throttle window → publish.

    Args:
        last:           The last-published state dict (None if no file).
        current_tuple:  (state, activity, queue_tuple, cadence) — queue must be
                        a hashable type (tuple) so equality works correctly.
        now:            Current epoch float.
        throttle:       Throttle interval in seconds.
    """
    if last is None:
        return True

    # Compare current tuple against last-published tuple.
    # queue is stored as a list in JSON; normalise both sides to tuple for equality.
    last_tuple = (
        last.get("state"),
        last.get("activity"),
        tuple(last.get("queue") or []),
        last.get("cadence"),
    )
    if current_tuple != last_tuple:
        return True

    # Unchanged — check throttle window.
    last_published_at = last.get("published_at", 0) or 0
    return (now - last_published_at) > throttle


def _apply_sleep_guard(
    state: str,
    activity: Optional[str],
    queue: list,
    cadence: Optional[int],
    last: Optional[dict],
    now: float,
) -> tuple:
    """Apply the Q4 mid-sleep guard.

    If there is no loop marker AND the last-published state was 'sleeping' AND
    we are still within the throttle window, keep ALL of the derived fields at
    their last-published 'sleeping' values (state, activity, queue, AND cadence)
    to suppress the false-wake publish. Common case self-corrects on the next
    genuine wake.

    Cadence must be restored alongside the other fields: derive_own_status with
    no loop marker returns cadence=None, but the last sleeping publish carried
    the sleep cadence (e.g. 270). If only state/activity/queue were restored,
    the cadence mismatch (None vs 270) would make _should_publish see a changed
    tuple and fire a spurious POST that DROPS expected_republish_seconds — the
    server would then fall back to its global window. (Reviewer catch, #1077 r2.)

    Returns (state, activity, queue, cadence) — possibly overridden.
    """
    if _read_loop_mode is None:
        return state, activity, queue, cadence

    loop = _read_loop_mode()
    if loop is not None:
        # Loop marker present — genuine wake. No guard needed.
        return state, activity, queue, cadence

    if last is None:
        return state, activity, queue, cadence

    if last.get("state") != "sleeping":
        return state, activity, queue, cadence

    last_published_at = last.get("published_at", 0) or 0
    last_cadence = last.get("cadence")
    throttle = max(last_cadence or 0, 60)
    if (now - last_published_at) <= throttle:
        # Within the window — keep ALL last-sleeping fields (incl. cadence).
        return "sleeping", last.get("activity"), last.get("queue") or [], last_cadence

    return state, activity, queue, cadence


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


def _fetch_inbox(forum_url: str, agent_name: str) -> Optional[list]:
    """GET /api/agent/<name>/inbox — returns at_mention items only.

    The inbox endpoint uses server-side per-thread read marks so "reading the
    thread clears the mention" — no local cursor needed.

    Returns a list of inbox items filtered to kind=at_mention on success,
    None on any error (advisory; never breaks the hook chain). Swallows ALL
    errors including 404 (old server without the inbox endpoint) — same
    bare-except discipline as _fetch_threads. Timeout: 5 seconds.
    URL-quotes the agent name for safety.

    Response shape: {"inbox": [...], "unread_all": N}
    Item shape: {post_id, thread_id, thread_title, author, kind, created_at}
    """
    import urllib.parse
    quoted_name = urllib.parse.quote(agent_name, safe="")
    url = f"{forum_url}/api/agent/{quoted_name}/inbox"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            inbox = data.get("inbox", [])
            # Filter to at_mention only — reply_on_my_thread items are out of scope
            # for the banner (they are a different notification class).
            return [item for item in inbox if item.get("kind") == "at_mention"]
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

    # ── Read surfaced cursor ──────────────────────────────────────────────────
    surfaced_cursor = _read_cursor(SURFACED_CURSOR_PATH)
    since_str = surfaced_cursor.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if surfaced_cursor else None

    # Mention state no longer uses a local read cursor. The inbox endpoint
    # (GET /api/agent/<name>/inbox) uses server-side per-thread read marks, so
    # "reading a thread clears the mention" is handled by the server. No local
    # cursor read needed here.

    # ── Fetch threads + inbox mentions (with cache + stale-cache fallback) ────
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
            # Also fetch inbox mentions (advisory; failure → None, not a blocker).
            # Filtered to kind=at_mention inside _fetch_inbox.
            if agent_name:
                mentions = _fetch_inbox(forum_url, agent_name)
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

    # ── Auto-publish derived status (Q1–Q4, spec #1077 part 1) ───────────────
    # This block runs AFTER the fetch/format above but BEFORE the final output.
    # Its success/failure must NEVER gate the existing thread/mention output.
    try:
        if derive_own_status is not None and agent_name:
            _now = time.time()

            # Step 2: derive status from local signals.
            state, activity, queue, cadence = derive_own_status(agent_name)

            # Step 3: apply Q4 sleep guard (restores cadence too — see fn doc).
            last_pub = _load_publish_state()
            state, activity, queue, cadence = _apply_sleep_guard(
                state, activity, queue, cadence, last_pub, _now
            )

            # Step 4: compute throttle interval.
            throttle = max(cadence or 0, 60)

            # Step 5: decide whether to publish.
            current_tuple = (state, activity, tuple(queue), cadence)
            if _should_publish(last_pub, current_tuple, _now, throttle):
                # Step 6: POST to /api/agents/status with timeout=2.
                # Only on success, write the publish state file.
                payload: dict = {
                    "agent": agent_name,
                    "state": state,
                    "activity": activity,
                    "queue": queue,
                }
                # cadence None → omit (server uses its global window);
                # 0 (on-call) or positive → include (mirror cmd_status_auto).
                if cadence is not None:
                    payload["expected_republish_seconds"] = cadence

                post_data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    f"{forum_url}/api/agents/status",
                    data=post_data,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        resp.read()  # consume response body; discard result
                    # POST succeeded — advance the throttle timestamp.
                    _save_publish_state(state, activity, queue, cadence, _now)
                except Exception:
                    # POST failed — do NOT advance the timestamp so the next
                    # prompt retries.
                    pass
    except Exception:
        # Whole publish block raised — swallow completely. The existing
        # thread/mention output is unaffected.
        pass

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
