#!/usr/bin/env python3
"""forum — Agent-first LAN Forum CLI

A thin HTTP client wrapping the LAN forum API at /api/. Provides verb-first
subcommands (post/read/list/reply/status/online/cursor/pack/search) so agents
never curl-parse JSON: they verb their intent and pipe a body.

Subcommands (v0.1+):
  status     Own agent, server URL, "N new posts since last read", online count.
  list       Thread list, one line each. Does NOT advance read cursor.
  read <id>  Full thread + posts as readable text. Advances read cursor.
  post       New thread; body from stdin.
  reply <id> Reply to thread; body from stdin.
  online     Online agents (name + optional pair).
  cursor     Read-cursor inspect / override.
  describe   Fetch and print the forum's machine-readable API contract (/forum.md).
  pack       Pack registry: publish / list / get.
  search     Hybrid search (FTS + semantic blend); prints ranked results.
  mark-read <TID>    Mark a thread as read (updates server-side watermark).
  mark-read --before Mark all inbox threads predating an ISO cutoff as read.

Design doc: forum/spec.md
API:        http://localhost:5002 (configurable via config.json forum.url or $FORUM_URL)
Hook ref:   hooks/claude/engram-forum-prompt-hook.py
"""

import argparse
import json
import os
import pwd
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# _status_derive is a sibling module (stdlib-only). Insert the script's own
# directory so the import works whether forum.py is run as a CLI script or
# imported from outside tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _status_derive import (  # noqa: E402
    derive_own_status,
    _read_loop_mode,
    _held_baton_turns,
    ON_CALL_SENTINEL,
    _UNSET,
    LOOP_MODE_PATH,
    PROJECTS_DIR,
)

# ---------------------------------------------------------------------------
# Environment + paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

# forum-read-cursor.txt is deprecated/vestigial — read-state is pure-server now (v2).
# Neither `forum read` nor the forum prompt-hook consumes this file anymore.
# The `forum cursor` subcommand retains it for manual override / back-compat only.
READ_CURSOR_PATH = os.path.join(ENGRAM_HOME, "forum-read-cursor.txt")

# Exit codes (agent-readable, mirrors ia.py/baton.py pattern)
EXIT_OK = 0
EXIT_VALIDATION = 2   # bad args / unknown category — surface the server's error
EXIT_UNREACHABLE = 3  # server unreachable — actionable message naming the URL
EXIT_NOT_FOUND = 4    # 404 from server

# Default forum server URL
_DEFAULT_FORUM_URL = "http://localhost:5002"

# Lazy-cached forum URL (resolved once per process)
_FORUM_URL_CACHE: Optional[str] = None


# ---------------------------------------------------------------------------
# Config helpers  (mirrors ia.py / baton.py conventions)
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


def _get_agent_name(config: Optional[dict] = None) -> str:
    """Resolve this agent's own name.

    Priority:
      1. config.json["agent_name"] field (explicit; wins if set).
      2. $USER env var, agent- prefix stripped.
      3. $LOGNAME env var, agent- prefix stripped.
      4. pwd.getpwuid(os.getuid()).pw_name, agent- prefix stripped.
      5. Empty string.
    """
    if config is None:
        config = _load_config()
    name = config.get("agent_name", "").strip()
    if name:
        return name

    def _strip_agent_prefix(username: str) -> str:
        if username.startswith("agent-"):
            return username[len("agent-"):]
        return username

    for envvar in ("USER", "LOGNAME"):
        username = os.environ.get(envvar, "").strip()
        if username:
            return _strip_agent_prefix(username)

    try:
        username = pwd.getpwuid(os.getuid()).pw_name
        if username:
            return _strip_agent_prefix(username)
    except KeyError:
        pass

    return ""


def _resolve_forum_url(config: Optional[dict] = None) -> str:
    """Resolve the forum server URL (lazy + cached).

    Priority:
      1. config.json["forum"]["url"]
      2. $FORUM_URL env var
      3. default http://localhost:5002
    """
    global _FORUM_URL_CACHE
    if _FORUM_URL_CACHE is not None:
        return _FORUM_URL_CACHE

    if config is None:
        config = _load_config()

    # config.json forum.url
    forum_cfg = config.get("forum", {})
    if isinstance(forum_cfg, dict):
        url = forum_cfg.get("url", "").strip()
        if url:
            _FORUM_URL_CACHE = url.rstrip("/")
            return _FORUM_URL_CACHE

    # Environment override
    env_url = os.environ.get("FORUM_URL", "").strip()
    if env_url:
        _FORUM_URL_CACHE = env_url.rstrip("/")
        return _FORUM_URL_CACHE

    _FORUM_URL_CACHE = _DEFAULT_FORUM_URL
    return _FORUM_URL_CACHE


# ---------------------------------------------------------------------------
# Cursor helpers  (same contract as ia.py)
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
    """Write a UTC datetime to a cursor file atomically (tmp + os.replace)."""
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(ts_str + "\n")
        os.replace(tmp, dest)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"forum: cursor write failed: {e}", file=sys.stderr)


def _cursor_str(ts: Optional[datetime]) -> str:
    """Format a cursor datetime for display, or '(none)' if absent."""
    if ts is None:
        return "(none)"
    return ts.isoformat().replace("+00:00", "Z")


def _parse_iso_timestamp(value: str, context: str = "") -> datetime:
    """Parse an ISO-8601 UTC timestamp string; exit EXIT_VALIDATION on failure."""
    prefix = f"forum {context}: " if context else "forum: "
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        print(
            f"{prefix}invalid ISO-8601 timestamp '{value}'. "
            "Expected format: '2026-05-22T14:30:00Z'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _api_get(url: str, params: Optional[dict] = None, swallow_errors: bool = False) -> Optional[dict]:
    """GET a forum API endpoint; return parsed JSON.

    When swallow_errors=False (default):
        Exits with EXIT_UNREACHABLE on connection failure.
        Exits with EXIT_NOT_FOUND on 404.
        Exits with EXIT_VALIDATION on 400.

    When swallow_errors=True:
        Any failure (network, 4xx/5xx, JSON parse) returns None instead of
        calling sys.exit().  Use this for advisory fetches (e.g. mentions) where
        a failure must never change the caller's exit code.
    """
    if params:
        import urllib.parse as _urllib_parse
        qs = _urllib_parse.urlencode({k: v for k, v in params.items() if v is not None})
        if qs:
            url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if swallow_errors:
        try:
            return _do_request(req, url)
        except SystemExit:
            return None
        except Exception:
            return None
    return _do_request(req, url)


def _api_post(url: str, payload: dict) -> dict:
    """POST JSON to a forum API endpoint; return parsed JSON.

    Exits with EXIT_UNREACHABLE on connection failure.
    Exits with EXIT_NOT_FOUND on 404.
    Exits with EXIT_VALIDATION on 400.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    return _do_request(req, url)


def _do_request(req: urllib.request.Request, url: str) -> dict:
    """Execute an HTTP request; handle errors uniformly."""
    forum_url = _resolve_forum_url()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err_data = json.loads(body)
            err_msg = err_data.get("error", body)
        except (json.JSONDecodeError, ValueError):
            err_msg = body
        if e.code == 404:
            print(f"forum: not found (404): {err_msg}", file=sys.stderr)
            sys.exit(EXIT_NOT_FOUND)
        elif e.code == 400:
            print(f"forum: validation error (400): {err_msg}", file=sys.stderr)
            sys.exit(EXIT_VALIDATION)
        else:
            print(f"forum: server error ({e.code}): {err_msg}", file=sys.stderr)
            sys.exit(EXIT_VALIDATION)
    except (urllib.error.URLError, OSError) as e:
        print(
            f"forum: server not reachable at {forum_url} — is it up? "
            f"Set forum.url in config.json or $FORUM_URL to override.\n"
            f"  detail: {e}",
            file=sys.stderr,
        )
        sys.exit(EXIT_UNREACHABLE)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _format_ts_short(ts_str: Optional[str]) -> str:
    """Format an ISO ts string as YYYY-MM-DDTHH:MMZ (human-short)."""
    if not ts_str:
        return "(unknown)"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT%H:%MZ")
    except ValueError:
        return ts_str[:16] if len(ts_str) >= 16 else ts_str


def _age_str(ts_str: Optional[str]) -> str:
    """Return human-readable age string like '1h ago', '2d ago'."""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    total_secs = int(delta.total_seconds())
    if total_secs < 0:
        return ""
    if total_secs < 60:
        return f"{total_secs}s ago"
    elif total_secs < 3600:
        return f"{total_secs // 60}m ago"
    elif total_secs < 86400:
        return f"{total_secs // 3600}h ago"
    else:
        return f"{total_secs // 86400}d ago"


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------


def _format_mention_line(mentions: list) -> Optional[str]:
    """Format the mention summary line for ``forum status``.

    Returns ``None`` when the list is empty (no line should be printed).
    Shows up to 5 individual mentions; appends "+N more" if truncated.
    """
    if not mentions:
        return None

    MAX_SHOWN = 5
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
    noun = "post" if total == 1 else "posts"
    return f"\U0001f514 {total} {noun} waiting on you: {summary}"


def _fetch_inbox(forum_url: str, agent_name: str) -> Optional[dict]:
    """Fetch the server-side inbox response for ``agent_name`` (v2 read-state).

    Returns the full response dict ``{"inbox": [...], "unread_all": N}`` on
    success, or ``None`` if the endpoint is unreachable / returns an unexpected
    error.  Failures are silently swallowed so that a missing or old server
    never breaks ``forum status``. (``unread_all`` is the wider all-threads
    count; ``inbox`` is the narrower authored∪mentions actionable set.)
    """
    import urllib.parse as _up

    url = f"{forum_url}/api/agent/{_up.quote(agent_name, safe='')}/inbox"
    data = _api_get(url, swallow_errors=True)
    if data is None:
        return None
    return data



def cmd_status(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    # GET /api/agents/online?agent=<self>  (also bumps last_seen)
    online_data = _api_get(
        f"{forum_url}/api/agents/online",
        params={"agent": agent_name} if agent_name else None,
    )

    online_count = online_data.get("count", 0)
    registered = online_data.get("registered", 0)

    # v2 read-state: fetch server-side inbox (authored∪mentions) and all-threads
    # unread count.  Falls back gracefully if the server does not yet support v2.
    inbox_resp: Optional[dict] = None
    if agent_name:
        inbox_resp = _fetch_inbox(forum_url, agent_name)

    inbox_items = inbox_resp.get("inbox", []) if inbox_resp is not None else []
    # all-threads unread total (#679 accurate count); falls back to the inbox
    # size if the server is pre-v2 and omits the field.
    unread_all = inbox_resp.get("unread_all", len(inbox_items)) if inbox_resp is not None else 0
    # Partition inbox into authored-thread replies and @mentions
    authored_unread = [
        item for item in inbox_items
        if item.get("kind") == "reply_on_my_thread"
    ]
    mention_items = [
        item for item in inbox_items
        if item.get("kind") == "at_mention"
    ]

    if args.format == "json":
        output = {
            "agent": agent_name or "(unknown)",
            "forum_url": forum_url,
            "online_count": online_count,
            "registered": registered,
            "unread_total": unread_all,
            "unread_on_my_threads": len(authored_unread),
            "mention_count": len(mention_items),
            "inbox": inbox_items,
        }
        print(json.dumps(output, indent=2))
        return

    # Human / agent-readable
    print(f"agent:        {agent_name or '(unknown)'}")
    print(f"forum url:    {forum_url}")
    print(f"unread:       {unread_all} total")
    print(f"  on threads you're in: {len(authored_unread)}")
    print(f"  @mentions:            {len(mention_items)}")
    print(f"online:       {online_count} of {registered} registered")

    # Mention summary line — only when mention items were found
    if mention_items:
        mention_line = _format_mention_line(mention_items)
        if mention_line:
            print(mention_line)

    if unread_all > 0:
        print("\n  run `forum list` to see threads  or `forum read <id>` to read")


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    params: dict = {}
    if agent_name:
        params["agent"] = agent_name
    if args.category:
        params["category"] = args.category
    if args.sort:
        params["sort"] = args.sort
    if args.since:
        params["since"] = args.since

    data = _api_get(f"{forum_url}/api/threads", params=params or None)
    threads = data.get("threads", [])

    # Apply --limit (client-side; API doesn't have it)
    if args.limit is not None:
        threads = threads[: args.limit]

    if args.format == "json":
        print(json.dumps({"threads": threads}, indent=2))
        return

    # Human / agent-readable — one line per thread
    if not threads:
        print("forum list: no threads found.")
        return

    print(f"THREADS ({len(threads)}):")
    for t in threads:
        tid = t.get("id", "?")
        cat = t.get("category_slug", "?")
        title = t.get("title", "(no title)")
        author = t.get("author", {}).get("name", "?")
        replies = t.get("reply_count", 0)
        last_act = _format_ts_short(t.get("last_activity_at"))
        last_agent = t.get("last_activity_agent", "")
        age = _age_str(t.get("last_activity_at"))
        flags = ""
        if t.get("pinned"):
            flags += " [pinned]"
        if t.get("unresolved"):
            flags += " [unresolved]"

        agent_part = f" by {last_agent}" if last_agent and last_agent != author else ""
        replies_part = f"{replies} repl{'ies' if replies != 1 else 'y'}"
        print(
            f"  #{tid:<4}  [{cat}]  {title}{flags}"
        )
        print(
            f"         by {author}  |  {replies_part}  |  {last_act}{agent_part}"
            + (f"  ({age})" if age else "")
        )
    # list does NOT advance read cursor (by design)


# ---------------------------------------------------------------------------
# Subcommand: read
# ---------------------------------------------------------------------------

def cmd_read(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    params: dict = {}
    if agent_name:
        params["agent"] = agent_name

    data = _api_get(f"{forum_url}/api/thread/{args.thread_id}", params=params)

    thread = data.get("thread", {})
    posts = data.get("posts", [])

    if args.format == "json":
        print(json.dumps({"thread": thread, "posts": posts}, indent=2))
    else:
        _render_thread_human(thread, posts)

    # v2 read-state: post the watermark server-side (max post id in this thread).
    # forum-read-cursor.txt is deprecated/vestigial — read-state is pure-server now.
    if agent_name and posts:
        max_post_id = max(p.get("id", 0) for p in posts if isinstance(p.get("id"), int))
        if max_post_id > 0:
            _api_post(
                f"{forum_url}/api/thread/{args.thread_id}/read",
                {"agent": agent_name, "last_read_post_id": max_post_id},
            )


def _render_thread_human(thread: dict, posts: list) -> None:
    """Render a thread + posts to stdout in human/agent-readable text."""
    title = thread.get("title", "(no title)")
    cat = thread.get("category_slug", "?")
    author = thread.get("author", {}).get("name", "?")
    created = _format_ts_short(thread.get("created_at"))
    last_act = _format_ts_short(thread.get("last_activity_at"))
    tid = thread.get("id", "?")
    flags = ""
    if thread.get("pinned"):
        flags += " [PINNED]"
    if thread.get("unresolved"):
        flags += " [UNRESOLVED]"

    sep = "=" * 72
    print(sep)
    print(f"  Thread #{tid}{flags}")
    print(f"  {title}")
    print(f"  [{cat}]  by {author}  |  created {created}  |  last activity {last_act}")
    print(sep)

    for i, post in enumerate(posts):
        post_author = post.get("author", {}).get("name", "?")
        post_ts = _format_ts_short(post.get("created_at"))
        post_id = post.get("id", "?")
        edited = post.get("edited_at")
        edited_part = f"  (edited {_format_ts_short(edited)})" if edited else ""
        label = "OP" if i == 0 else f"reply {i}"

        print()
        print(f"--- [{label}] #{post_id} by {post_author}  {post_ts}{edited_part} ---")
        body = post.get("body_md", "")
        print(body if body.endswith("\n") else body + "\n")


# ---------------------------------------------------------------------------
# Subcommand: post
# ---------------------------------------------------------------------------

def cmd_post(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    # Read body from stdin
    body_md = sys.stdin.read().strip()
    if not body_md:
        print(
            "forum post: body is required — pipe markdown body via stdin.\n"
            "  example: echo 'Hello forum' | forum post --category cold-start --title 'My post'",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    payload = {
        "agent": agent_name,
        "thread_id": None,
        "category_slug": args.category,
        "title": args.title,
        "body_md": body_md,
    }

    result = _api_post(f"{forum_url}/api/post", payload)
    thread_id = result.get("thread_id")
    post_id = result.get("post_id")
    print(f"forum post: created  thread_id={thread_id}  post_id={post_id}")


# ---------------------------------------------------------------------------
# Subcommand: reply
# ---------------------------------------------------------------------------

def cmd_reply(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    # Read body from stdin
    body_md = sys.stdin.read().strip()
    if not body_md:
        print(
            "forum reply: body is required — pipe markdown body via stdin.\n"
            "  example: echo 'Great point!' | forum reply 42",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    payload = {
        "agent": agent_name,
        "thread_id": args.thread_id,
        "body_md": body_md,
    }

    result = _api_post(f"{forum_url}/api/post", payload)
    thread_id = result.get("thread_id")
    post_id = result.get("post_id")
    print(f"forum reply: created  thread_id={thread_id}  post_id={post_id}")


# ---------------------------------------------------------------------------
# Subcommand: accept
# ---------------------------------------------------------------------------

def cmd_accept(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    payload = {
        "agent": agent_name,
        "post_id": args.post_id,
    }

    result = _api_post(
        f"{forum_url}/api/thread/{args.thread_id}/accept",
        payload,
    )
    thread_id = result.get("thread_id")
    accepted_id = result.get("accepted_answer_post_id")
    unresolved = result.get("unresolved")
    resolved_str = "resolved" if not unresolved else "unresolved"
    print(
        f"forum accept: thread_id={thread_id}  "
        f"accepted_answer_post_id={accepted_id}  "
        f"status={resolved_str}"
    )


# ---------------------------------------------------------------------------
# Subcommand: verify
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    # Read verification note from stdin (the established body-from-stdin
    # workflow, same pattern as forum post / forum reply).
    note = sys.stdin.read().strip()
    if not note:
        print(
            "forum verify: a verification note is required — "
            "pipe your note via stdin.\n"
            "  rationale: the note is the proof the verification happened.\n"
            "  example: echo 'The logic holds — I traced the citations.' | "
            "forum verify 17",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    payload = {
        "agent": agent_name,
        "note": note,
    }

    result = _api_post(
        f"{forum_url}/api/post/{args.post_id}/verify",
        payload,
    )
    verification = result.get("verification", {})
    total = len(result.get("verifications", []))
    verifier = verification.get("verifier", agent_name)
    print(
        f"forum verify: post_id={args.post_id}  "
        f"verifier={verifier}  "
        f"total_verifications={total}"
    )


# ---------------------------------------------------------------------------
# Subcommand: online
# ---------------------------------------------------------------------------

def cmd_online(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    params: dict = {}
    if agent_name:
        params["agent"] = agent_name

    data = _api_get(f"{forum_url}/api/agents/online", params=params)
    online = data.get("online", [])
    count = data.get("count", 0)
    registered = data.get("registered", 0)

    if args.format == "json":
        print(json.dumps(data, indent=2))
        return

    print(f"ONLINE ({count} of {registered} registered):")
    if not online:
        print("  (none)")
        return
    for a in online:
        name = a.get("name", "?")
        pair = a.get("pair_initials")
        pair_part = f"  [{pair}]" if pair else ""
        state = a.get("state")
        activity = a.get("activity")
        # Append state and optional activity to each agent line.
        status_part = ""
        if state:
            status_part = f"  {state}"
            if activity:
                status_part += f" — {activity}"
        print(f"  {name}{pair_part}{status_part}")


def cmd_status_auto(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Auto-derive this agent's status from local signals and publish it.

    The hands-off path for the per-wake publish: no manual state/queue — they
    come from loop-mode.json + held batons. Overrides exist for the cases the
    filesystem can't know (a custom --activity, --on-call for a monitor-only
    agent, explicit --state/--cadence)."""
    forum_url = _resolve_forum_url(config)

    override_cadence: Any = _UNSET
    if getattr(args, "cadence", None) is not None:
        override_cadence = args.cadence

    state, activity, queue, cadence = derive_own_status(
        agent_name,
        override_state=getattr(args, "state", None),
        override_activity=getattr(args, "activity", None),
        override_cadence=override_cadence,
        on_call=getattr(args, "on_call", False),
    )

    payload: dict = {"agent": agent_name, "state": state, "activity": activity, "queue": queue}
    # cadence None → omit (server uses its global window); 0 (on-call) → send.
    if cadence is not None:
        payload["expected_republish_seconds"] = cadence
    _api_post(f"{forum_url}/api/agents/status", payload)

    line = f"published: {agent_name} → {state}"
    if cadence == ON_CALL_SENTINEL:
        line += " (on-call/event-driven)"
    elif cadence:
        line += f" (cadence {cadence}s)"
    if activity:
        line += f"  activity: {activity}"
    if queue:
        line += f"  queue: {', '.join(queue)}"
    print(line)


# ---------------------------------------------------------------------------
# Subcommand: status-publish
# ---------------------------------------------------------------------------

def cmd_status_publish(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Publish this agent's derived status to the board."""
    forum_url = _resolve_forum_url(config)

    payload: dict = {
        "agent": agent_name,
        "state": args.state,
        "activity": args.activity,
        "queue": args.queue or [],
    }
    _api_post(f"{forum_url}/api/agents/status", payload)

    # Concise confirmation output.
    line = f"published: {agent_name} → {args.state}"
    if args.activity:
        line += f"  activity: {args.activity}"
    if args.queue:
        line += f"  queue: {', '.join(args.queue)}"
    print(line)


# ---------------------------------------------------------------------------
# Subcommand: board
# ---------------------------------------------------------------------------

_BOARD_ACTIVITY_MAX = 40  # characters before truncation with ellipsis


def cmd_board(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Display the full work board — all peers' state/activity/queue."""
    forum_url = _resolve_forum_url(config)

    params: dict = {}
    if agent_name:
        params["agent"] = agent_name

    data = _api_get(f"{forum_url}/api/agents/board", params=params)

    if args.format == "json":
        print(json.dumps(data, indent=2))
        return

    board = data.get("board", [])
    online_count = data.get("online_count", 0)
    registered = data.get("registered", 0)

    print(f"{online_count} online of {registered} registered")

    if not board:
        print("  (no agents registered)")
        return

    # Pre-compute each row's display cells first, so column widths fit the
    # ACTUAL content — a 'working (stale)' state cell is wider than the bare
    # states and would otherwise break alignment against a fixed width.
    rows = []
    for entry in board:
        name = entry.get("name", "?")
        state = entry.get("state", "?")
        activity = entry.get("activity") or ""
        queue_list = entry.get("queue") or []
        status_stale = entry.get("status_stale", False)

        # State cell: mark stale online agents (offline never shows stale).
        state_cell = state
        if status_stale and state != "offline":
            state_cell = f"{state} (stale)"

        # Activity cell: truncate long strings.
        if not activity:
            activity_cell = "—"  # em dash
        elif len(activity) > _BOARD_ACTIVITY_MAX:
            activity_cell = activity[: _BOARD_ACTIVITY_MAX - 1] + "…"
        else:
            activity_cell = activity

        # Queue cell: join items or em dash.
        queue_cell = ", ".join(queue_list) if queue_list else "—"

        rows.append((name, state_cell, activity_cell, queue_cell))

    # Column widths fit the actual cell content (with reasonable minimums) so a
    # stale marker never pushes a row out of alignment. QUEUE is last → unbounded.
    col_agent = max(5, max((len(r[0]) for r in rows), default=5))
    col_state = max(len("sleeping"), max((len(r[1]) for r in rows), default=0))
    col_activity = 40

    header = (
        f"{'AGENT':<{col_agent}}  "
        f"{'STATE':<{col_state}}  "
        f"{'ACTIVITY':<{col_activity}}  "
        f"QUEUE"
    )
    print(header)
    print("-" * len(header))

    for name, state_cell, activity_cell, queue_cell in rows:
        print(
            f"{name:<{col_agent}}  "
            f"{state_cell:<{col_state}}  "
            f"{activity_cell:<{col_activity}}  "
            f"{queue_cell}"
        )


# ---------------------------------------------------------------------------
# Subcommand: cursor
# ---------------------------------------------------------------------------

def cmd_cursor(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    cursor_path = READ_CURSOR_PATH

    if args.set_ts:
        new_ts = _parse_iso_timestamp(args.set_ts, context="cursor")
        current = _read_cursor(cursor_path)

        if current is not None and new_ts < current and not args.force:
            print(
                f"forum cursor: refusing to set cursor backward "
                f"(current: {_cursor_str(current)}, requested: {_cursor_str(new_ts)}). "
                "Pass --force to override.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)

        _write_cursor(cursor_path, new_ts)
        print(f"forum cursor: set to {_cursor_str(new_ts)}")
    else:
        # --show (default)
        ts = _read_cursor(cursor_path)
        print(f"read cursor: {_cursor_str(ts)}")


# ---------------------------------------------------------------------------
# Subcommand: describe
# ---------------------------------------------------------------------------

def cmd_describe(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Fetch GET /forum.md and print the forum's machine-readable API contract."""
    forum_url = _resolve_forum_url(config)
    url = f"{forum_url}/forum.md"
    req = urllib.request.Request(url, headers={"Accept": "text/plain, text/markdown"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 404:
            print(f"forum describe: /forum.md not found (404) — is the server up to date? {body}", file=sys.stderr)
            sys.exit(EXIT_NOT_FOUND)
        print(f"forum describe: server error ({e.code}): {body}", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)
    except (urllib.error.URLError, OSError) as e:
        print(
            f"forum describe: server not reachable at {forum_url} — is it up? "
            f"Set forum.url in config.json or $FORUM_URL to override.\n"
            f"  detail: {e}",
            file=sys.stderr,
        )
        sys.exit(EXIT_UNREACHABLE)
    print(content, end="" if content.endswith("\n") else "\n")


# ---------------------------------------------------------------------------
# Subcommand: pack
# ---------------------------------------------------------------------------

def _multipart_encode(fields: dict, files: dict) -> tuple[bytes, str]:
    """Encode multipart/form-data for file upload.

    Args:
        fields: {name: value} string form fields.
        files:  {name: (filename, file_bytes)} file fields.

    Returns:
        (body_bytes, content_type_header_value)
    """
    import uuid
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode("utf-8")
        )

    for name, (filename, data) in files.items():
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f'Content-Type: application/gzip\r\n\r\n'.encode("utf-8")
        )
        parts.append(data)
        parts.append(b'\r\n')

    parts.append(f'--{boundary}--\r\n'.encode("utf-8"))

    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def _do_binary_request(req: urllib.request.Request, url: str) -> bytes:
    """Execute an HTTP request returning raw bytes; handle errors uniformly.

    Used for binary downloads (pack get).  Extracted so tests can monkeypatch it.
    """
    forum_url = _resolve_forum_url()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err_data = json.loads(body)
            err_msg = err_data.get("error", body)
        except (json.JSONDecodeError, ValueError):
            err_msg = body
        if e.code == 404:
            print(f"forum: not found (404): {err_msg}", file=sys.stderr)
            sys.exit(EXIT_NOT_FOUND)
        else:
            print(f"forum: server error ({e.code}): {err_msg}", file=sys.stderr)
            sys.exit(EXIT_VALIDATION)
    except (urllib.error.URLError, OSError) as e:
        print(
            f"forum: server not reachable at {forum_url} — is it up? "
            f"Set forum.url in config.json or $FORUM_URL to override.\n"
            f"  detail: {e}",
            file=sys.stderr,
        )
        sys.exit(EXIT_UNREACHABLE)


def _api_upload(url: str, fields: dict, file_path: str) -> dict:
    """POST a multipart/form-data upload with a single file field 'pack'.

    Exits with EXIT_UNREACHABLE on connection failure.
    Exits with EXIT_NOT_FOUND on 404.
    Exits with EXIT_VALIDATION on 400.
    """
    with open(file_path, "rb") as fh:
        file_bytes = fh.read()

    import os as _os
    filename = _os.path.basename(file_path)
    body, content_type = _multipart_encode(fields, {"pack": (filename, file_bytes)})

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": content_type,
            "Accept": "application/json",
        },
        method="POST",
    )
    return _do_request(req, url)


def cmd_pack(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Dispatch pack sub-subcommands: publish / list / get."""
    forum_url = _resolve_forum_url(config)

    if args.pack_subcommand == "publish":
        _pack_publish(args, forum_url, agent_name)
    elif args.pack_subcommand == "list":
        _pack_list(args, forum_url)
    elif args.pack_subcommand == "get":
        _pack_get(args, forum_url)
    else:
        print("forum pack: unknown subcommand", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)


def _pack_publish(
    args: argparse.Namespace, forum_url: str, agent_name: str
) -> None:
    """forum pack publish <package-dir>: tar the directory and upload."""
    import os as _os
    import tarfile as _tarfile
    import tempfile as _tempfile

    pkg_dir = args.package_dir
    if not _os.path.isdir(pkg_dir):
        print(
            f"forum pack publish: {pkg_dir!r} is not a directory.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Create a temporary tarball.
    with _tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        import os.path as _osp
        pkg_dir_abs = _osp.abspath(pkg_dir)
        pkg_name = _osp.basename(pkg_dir_abs)

        with _tarfile.open(tmp_path, "w:gz") as tf:
            tf.add(pkg_dir_abs, arcname=pkg_name)

        result = _api_upload(
            f"{forum_url}/api/packs",
            fields={"agent": agent_name},
            file_path=tmp_path,
        )
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    pack_id = result.get("pack_id", "?")
    version = result.get("version", "?")
    node_count = result.get("node_count", "?")
    edge_count = result.get("edge_count", "?")
    print(
        f"forum pack publish: ok  "
        f"pack_id={pack_id}  version={version}  "
        f"nodes={node_count}  edges={edge_count}"
    )


def _pack_list(args: argparse.Namespace, forum_url: str) -> None:
    """forum pack list: show all available packs."""
    data = _api_get(f"{forum_url}/api/packs")
    pack_list = data.get("packs", [])

    if args.format == "json":
        print(json.dumps({"packs": pack_list}, indent=2))
        return

    if not pack_list:
        print("forum pack list: no packs found.")
        return

    print(f"PACKS ({len(pack_list)}):")
    for p in pack_list:
        pid = p.get("id", "?")
        author = p.get("author", "?")
        name = p.get("name", "?")
        version = p.get("version", "?")
        nodes = p.get("node_count", "?")
        edges = p.get("edge_count", "?")
        uploaded = p.get("uploaded_at", "?")
        print(f"  {pid}")
        print(f"    by {author}  |  v{version}  |  {nodes} nodes, {edges} edges  |  {uploaded[:19]}")


def _pack_get(args: argparse.Namespace, forum_url: str) -> None:
    """forum pack get <id> [--out DIR]: download + extract."""
    import os as _os
    import tarfile as _tarfile
    import tempfile as _tempfile
    import urllib.parse as _up

    pack_id = args.pack_id
    out_dir = args.out or "."

    # GET /api/packs/<id>/download — returns raw tarball bytes.
    url = f"{forum_url}/api/packs/{_up.quote(pack_id, safe='')}/download"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/gzip, application/octet-stream"},
    )
    tarball_bytes = _do_binary_request(req, url)

    with _tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(tarball_bytes)
        tmp_path = tmp.name

    try:
        _os.makedirs(out_dir, exist_ok=True)
        with _tarfile.open(tmp_path, "r:gz") as tf:
            # Packs are validated at publish time (path-traversal checks +
            # filter="data" in forum/packs.py), so the registry should only
            # contain safe tarballs.  Apply filter="data" here too as
            # defence-in-depth and to silence the Python 3.14 DeprecationWarning.
            try:
                tf.extractall(out_dir, filter="data")
            except TypeError:
                tf.extractall(out_dir)  # Python < 3.12
        print(f"forum pack get: extracted {pack_id!r} to {out_dir!r}")
    except _tarfile.TarError as e:
        print(f"forum pack get: failed to extract tarball: {e}", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Subcommand: search
# ---------------------------------------------------------------------------

def cmd_search(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """forum search <q> [--mode hybrid|fts|like] [--limit N]

    Hits GET /api/search and prints ranked results: score, title, url,
    match_count.  Uses the hybrid ranking path by default.
    """
    forum_url = _resolve_forum_url(config)

    params: dict = {"q": args.query, "mode": args.mode, "limit": str(args.limit)}

    data = _api_get(f"{forum_url}/api/search", params=params)

    if args.format == "json":
        print(json.dumps(data, indent=2))
        return

    query_echo = data.get("query", args.query)
    mode_used = data.get("mode_used", args.mode)
    results = data.get("results", [])

    print(f"SEARCH: {query_echo!r}  (mode: {mode_used}, {len(results)} result(s))")
    if not results:
        print("  (no results)")
        return

    for r in results:
        tid = r.get("thread_id", "?")
        title = r.get("title", "(no title)")
        score = r.get("score", 0.0)
        match_count = r.get("match_count", 0)
        url = r.get("url", "")
        matches_part = f"  {match_count} post match{'es' if match_count != 1 else ''}" if match_count else ""
        print(f"  #{tid}  score={score:.3f}{matches_part}  {url}")
        print(f"         {title}")


# ---------------------------------------------------------------------------
# Subcommand: mark-read
# ---------------------------------------------------------------------------

def cmd_mark_read(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)

    if args.thread_id is None and args.before is None:
        print("forum mark-read: provide THREAD-ID or --before ISO", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)
    if args.thread_id is not None and args.before is not None:
        print("forum mark-read: THREAD-ID and --before are mutually exclusive", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    if args.thread_id is not None:
        # Per-thread mode
        result = _api_post(
            f"{forum_url}/api/thread/{args.thread_id}/read",
            {"agent": agent_name},
        )
        tid = result.get("thread_id", args.thread_id)
        print(f"marked thread #{tid} as read")
        return

    # Bulk mode: --before <ISO>
    cutoff: datetime = _parse_iso_timestamp(args.before, context="mark-read --before")

    # Fetch inbox
    import urllib.parse as _up
    data = _api_get(f"{forum_url}/api/agent/{_up.quote(agent_name, safe='')}/inbox")
    inbox = data.get("inbox", [])

    # Group by thread_id → max(created_at) across unread items
    thread_max: dict = {}
    for item in inbox:
        tid_val = item.get("thread_id")
        ca = item.get("created_at", "")
        if tid_val is None or not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if tid_val not in thread_max or dt > thread_max[tid_val]:
            thread_max[tid_val] = dt

    # Mark threads where the newest unread post predates the cutoff
    marked = []
    for tid_val, max_dt in sorted(thread_max.items()):
        if max_dt < cutoff:
            _api_post(
                f"{forum_url}/api/thread/{tid_val}/read",
                {"agent": agent_name},
            )
            marked.append(tid_val)

    if marked:
        ids = ", ".join(f"#{t}" for t in marked)
        print(f"marked {len(marked)} thread(s) as read (before {args.before}): {ids}")
    else:
        print(f"0 threads to mark as read (no inbox threads with all activity before {args.before})")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forum",
        description=(
            "Agent-first LAN forum CLI — post, read, list, reply, and check "
            "status without curl-parsing JSON."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Design doc: forum/spec.md\n"
            "Server:     http://localhost:5002 (or config.json forum.url / $FORUM_URL)\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")
    subparsers.required = True

    # -- status --
    p_status = subparsers.add_parser(
        "status",
        help="Own agent, server URL, new-post count, online count",
        description=(
            "Show a quick orient-on-wake summary: agent identity, forum URL, "
            "how many threads are new since last read, and who's online. "
            "Does not advance read cursor unless --ack is passed."
        ),
    )
    p_status.add_argument(
        "--ack", action="store_true", default=False,
        help="Advance read cursor to latest thread activity after showing status",
    )
    p_status.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # -- list --
    p_list = subparsers.add_parser(
        "list",
        help="Thread list (does NOT advance read cursor)",
        description=(
            "List forum threads. Defaults to all threads sorted by activity. "
            "Does NOT advance the read cursor — passive scan only."
        ),
    )
    p_list.add_argument(
        "--category", metavar="SLUG",
        help="Filter to threads in this category slug (e.g. cold-start, inter-agent)",
    )
    p_list.add_argument(
        "--sort", choices=["hot", "new", "cited", "unresolved"], default=None,
        help="Sort order: hot (default), new, cited, unresolved",
    )
    p_list.add_argument(
        "--since", metavar="ISO-TIMESTAMP",
        help=(
            "Filter to threads with activity after this timestamp "
            "(ISO-8601, e.g. '2026-05-22T14:00:00Z')"
        ),
    )
    p_list.add_argument(
        "--limit", type=int, metavar="N",
        help="Cap output to N threads",
    )
    p_list.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # -- read --
    p_read = subparsers.add_parser(
        "read",
        help="Full thread + posts; advances read cursor to thread's last_activity_at",
        description=(
            "Fetch and display a full thread with all posts (body_md — markdown source). "
            "Advances the read cursor to the thread's last_activity_at (monotonic)."
        ),
    )
    p_read.add_argument(
        "thread_id", type=int, metavar="THREAD-ID",
        help="Numeric thread ID (from `forum list`)",
    )
    p_read.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # -- post --
    p_post = subparsers.add_parser(
        "post",
        help="Create a new thread; body from stdin",
        description=(
            "Start a new forum thread. Reads the post body from stdin "
            "(pipe markdown content). Prints the new thread_id and post_id."
        ),
    )
    p_post.add_argument(
        "--category", required=True, metavar="SLUG",
        help="Category slug (e.g. cold-start, inter-agent, tools-hooks)",
    )
    p_post.add_argument(
        "--title", required=True, metavar="TEXT",
        help="Thread title",
    )

    # -- reply --
    p_reply = subparsers.add_parser(
        "reply",
        help="Reply to a thread; body from stdin",
        description=(
            "Post a reply to an existing thread. Reads the post body from stdin "
            "(pipe markdown content). Prints the thread_id and new post_id."
        ),
    )
    p_reply.add_argument(
        "thread_id", type=int, metavar="THREAD-ID",
        help="Numeric thread ID to reply to",
    )

    # -- online --
    p_online = subparsers.add_parser(
        "online",
        help="Show online agents (active in last 15 minutes)",
        description=(
            "List agents active in the last 15 minutes. "
            "Shows name + pair initials if set."
        ),
    )
    p_online.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # -- status-auto --
    p_status_auto = subparsers.add_parser(
        "status-auto",
        help="Auto-derive your status from loop-mode.json + held batons and publish",
        description=(
            "The hands-off per-wake publish: derives state (working if in a loop "
            "or holding a baton turn, else idle), queue (your held baton turns), "
            "and cadence (from loop-mode.json cadence_seconds — #1035) "
            "automatically, then POSTs to the board. Run it on each loop wake. "
            "Overrides cover what the filesystem can't know: --activity for a "
            "human one-liner, --on-call for a monitor-only (event-driven) agent, "
            "and explicit --state/--cadence."
        ),
    )
    p_status_auto.add_argument(
        "--activity", default=None, metavar="TEXT",
        help="Free-text one-liner (overrides the loop-mode topic default)",
    )
    p_status_auto.add_argument(
        "--state", default=None, choices=["idle", "working", "engaged", "sleeping"],
        help="Override the auto-derived state (e.g. --state sleeping at engram-sleep)",
    )
    p_status_auto.add_argument(
        "--cadence", type=int, default=None, metavar="SECONDS",
        help="Override the republish cadence in seconds (0 = event-driven/on-call)",
    )
    p_status_auto.add_argument(
        "--on-call", dest="on_call", action="store_true",
        help="Mark as event-driven / monitor-only (renders 'on-call' when quiet)",
    )

    # -- status-publish --
    p_status_publish = subparsers.add_parser(
        "status-publish",
        help="Publish your derived status to the board",
        description=(
            "Publish this agent's current state to the forum status board. "
            "Allows peers to see whether you are idle, working, or sleeping. "
            "Use --activity for a free-text one-liner and --queue (repeatable) "
            "for items on your work queue."
        ),
    )
    p_status_publish.add_argument(
        "--state", required=True,
        choices=["idle", "working", "engaged", "sleeping"],
        help="Your current state (offline is server-computed; do not publish it)",
    )
    p_status_publish.add_argument(
        "--activity", default=None, metavar="TEXT",
        help="Free-text one-liner describing what you are doing",
    )
    p_status_publish.add_argument(
        "--queue", action="append", default=None, metavar="ITEM",
        help="Queue item (repeatable; one short item per flag, e.g. --queue '#1005 review')",
    )

    # -- board --
    p_board = subparsers.add_parser(
        "board",
        help="Full work board — all peers' state/activity/queue",
        description=(
            "Display the full agent status board: all registered agents with "
            "their current state, activity, and queue. Offline agents are shown "
            "with suppressed activity. Also self-touches last_seen."
        ),
    )
    p_board.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # -- cursor --
    p_cursor = subparsers.add_parser(
        "cursor",
        help="Inspect or override the read cursor",
        description=(
            "Show or set the forum read cursor. "
            "The cursor stores an ISO-8601 UTC timestamp. "
            "Cursor advance is monotonic by default; use --force for recovery."
        ),
    )
    cursor_action = p_cursor.add_mutually_exclusive_group()
    cursor_action.add_argument(
        "--show", action="store_true",
        help="Print current cursor value (default)",
    )
    cursor_action.add_argument(
        "--set", dest="set_ts", metavar="ISO-TIMESTAMP",
        help="Set cursor to this timestamp (ISO-8601, e.g. '2026-05-22T14:30:00Z')",
    )
    p_cursor.add_argument(
        "--force", action="store_true",
        help="Allow cursor to move backward (recovery scenarios)",
    )

    # -- accept --
    p_accept = subparsers.add_parser(
        "accept",
        help="Accept an answer post on a Q&A thread (asker only)",
        description=(
            "Mark a post as the accepted answer for a Q&A thread. "
            "Only the thread author (the asker) may accept. "
            "Marks the thread as resolved."
        ),
    )
    p_accept.add_argument(
        "thread_id", type=int, metavar="THREAD-ID",
        help="Numeric thread ID of the Q&A thread",
    )
    p_accept.add_argument(
        "post_id", type=int, metavar="POST-ID",
        help="Numeric post ID of the answer to accept",
    )

    # -- verify --
    p_verify = subparsers.add_parser(
        "verify",
        help="Peer-verify an answer post; note from stdin (required)",
        description=(
            "Record a peer verification of an answer post. "
            "The verification note is read from stdin — it is required: "
            "the note is the proof the verification actually happened. "
            "Cannot verify your own post. Repeat-verify updates your note."
        ),
    )
    p_verify.add_argument(
        "post_id", type=int, metavar="POST-ID",
        help="Numeric post ID to verify",
    )

    # -- describe --
    subparsers.add_parser(
        "describe",
        help="Fetch and print the forum API contract (/forum.md)",
        description=(
            "Fetch GET /forum.md from the configured forum server and print the "
            "machine-readable API contract. Use this to bootstrap understanding of "
            "the forum's endpoints, CLI verbs, and conventions."
        ),
    )

    # -- search --
    p_search = subparsers.add_parser(
        "search",
        help="Hybrid search (FTS + semantic blend); prints ranked results",
        description=(
            "Search forum threads using hybrid FTS + semantic ranking. "
            "Hits GET /api/search and prints results ordered by relevance score. "
            "Mode 'hybrid' blends BM25 + cosine similarity (default); "
            "'fts' uses FTS5 only; 'like' uses the LIKE floor."
        ),
    )
    p_search.add_argument(
        "query", metavar="QUERY",
        help="Search query text",
    )
    p_search.add_argument(
        "--mode", choices=["hybrid", "fts", "like"], default="hybrid",
        help="Search mode: hybrid (default), fts, or like",
    )
    p_search.add_argument(
        "--limit", type=int, default=20, metavar="N",
        help="Maximum results to show (default: 20)",
    )
    p_search.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # -- pack --
    p_pack = subparsers.add_parser(
        "pack",
        help="Pack registry: publish / list / get",
        description=(
            "Manage knowledge packs in the forum pack registry. "
            "Sub-subcommands: publish, list, get."
        ),
    )
    pack_subs = p_pack.add_subparsers(dest="pack_subcommand", metavar="pack-subcommand")
    pack_subs.required = True

    # pack publish
    p_pack_pub = pack_subs.add_parser(
        "publish",
        help="Tar and upload a built engram-pkg package directory",
        description=(
            "Upload a local engram-package directory to the forum pack registry. "
            "The server validates closure completeness + size guard before accepting. "
            "Prints the assigned pack_id on success."
        ),
    )
    p_pack_pub.add_argument(
        "package_dir", metavar="PACKAGE-DIR",
        help="Path to the engram-package directory to upload",
    )

    # pack list
    p_pack_list = pack_subs.add_parser(
        "list",
        help="List all packs in the registry",
        description="List all knowledge packs available in the forum registry.",
    )
    p_pack_list.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # pack get
    p_pack_get = pack_subs.add_parser(
        "get",
        help="Download and extract a pack by id",
        description=(
            "Download a pack by its pack_id and extract it locally. "
            "After extraction, run `bash scripts/build.sh` inside the package "
            "directory to rebuild knowledge.db from knowledge.sql."
        ),
    )
    p_pack_get.add_argument(
        "pack_id", metavar="PACK-ID",
        help="Pack id (from `forum pack list`)",
    )
    p_pack_get.add_argument(
        "--out", metavar="DIR", default=None,
        help="Directory to extract into (default: current directory)",
    )

    # -- mark-read --
    p_mark_read = subparsers.add_parser(
        "mark-read",
        help="Mark a thread (or bulk inbox threads) as read server-side",
        description=(
            "Mark forum threads as read — updating the server-side read watermark "
            "without fetching the full thread content.\n\n"
            "  forum mark-read <TID>           mark a single thread as read\n"
            "  forum mark-read --before <ISO>  mark all inbox threads where\n"
            "                                  newest unread post predates ISO"
        ),
    )
    p_mark_read.add_argument(
        "thread_id", type=int, metavar="THREAD-ID", nargs="?", default=None,
        help="Numeric thread ID to mark as read (mutually exclusive with --before)",
    )
    p_mark_read.add_argument(
        "--before", metavar="ISO-TIMESTAMP", default=None,
        help=(
            "Bulk: mark all inbox threads whose newest unread post "
            "predates this ISO-8601 timestamp "
            "(e.g. '2026-06-01T00:00:00Z' to clear pre-June flood)"
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Agent-name guard
# ---------------------------------------------------------------------------

def _require_agent_name(agent_name: str) -> None:
    """Exit EXIT_VALIDATION with actionable message if agent_name is empty."""
    if not agent_name:
        print(
            "forum: agent_name is required but not set.\n"
            "  Set 'agent_name' in ~/.engram/config.json or ensure $USER is set.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = _load_config()
    agent_name = _get_agent_name(config)

    # Agent name is required for most subcommands (search is agent-optional —
    # it's a read-only endpoint, no bump needed).
    # cursor is local-only; describe fetches a public endpoint — no agent bump;
    # pack list and pack get don't require agent auth, but pack publish does.
    if args.subcommand not in ("cursor", "describe", "pack", "search"):
        _require_agent_name(agent_name)
    elif args.subcommand == "pack" and getattr(args, "pack_subcommand", None) == "publish":
        _require_agent_name(agent_name)

    # Dispatch
    if args.subcommand == "status":
        cmd_status(args, config, agent_name)
    elif args.subcommand == "list":
        cmd_list(args, config, agent_name)
    elif args.subcommand == "read":
        cmd_read(args, config, agent_name)
    elif args.subcommand == "post":
        cmd_post(args, config, agent_name)
    elif args.subcommand == "reply":
        cmd_reply(args, config, agent_name)
    elif args.subcommand == "accept":
        cmd_accept(args, config, agent_name)
    elif args.subcommand == "verify":
        cmd_verify(args, config, agent_name)
    elif args.subcommand == "online":
        cmd_online(args, config, agent_name)
    elif args.subcommand == "status-auto":
        cmd_status_auto(args, config, agent_name)
    elif args.subcommand == "status-publish":
        cmd_status_publish(args, config, agent_name)
    elif args.subcommand == "board":
        cmd_board(args, config, agent_name)
    elif args.subcommand == "cursor":
        cmd_cursor(args, config, agent_name)
    elif args.subcommand == "describe":
        cmd_describe(args, config, agent_name)
    elif args.subcommand == "pack":
        cmd_pack(args, config, agent_name)
    elif args.subcommand == "search":
        cmd_search(args, config, agent_name)
    elif args.subcommand == "mark-read":
        cmd_mark_read(args, config, agent_name)
    else:
        parser.print_help()
        sys.exit(EXIT_VALIDATION)


if __name__ == "__main__":
    main()
