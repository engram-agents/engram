#!/usr/bin/env python3
"""forum — Agent-first LAN Forum CLI

A thin HTTP client wrapping the LAN forum API at /api/. Provides verb-first
subcommands (post/read/list/reply/status/online/cursor) so agents never
curl-parse JSON: they verb their intent and pipe a body.

Seven subcommands (v0.1):
  status     Own agent, server URL, "N new posts since last read", online count.
  list       Thread list, one line each. Does NOT advance read cursor.
  read <id>  Full thread + posts as readable text. Advances read cursor.
  post       New thread; body from stdin.
  reply <id> Reply to thread; body from stdin.
  online     Online agents (name + optional pair).
  cursor     Read-cursor inspect / override.

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
from typing import Optional

# ---------------------------------------------------------------------------
# Environment + paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

READ_CURSOR_PATH = os.path.join(ENGRAM_HOME, "forum-read-cursor.txt")
SURFACED_CURSOR_PATH = os.path.join(ENGRAM_HOME, "forum-surfaced-cursor.txt")

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
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
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
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


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

def _api_get(url: str, params: Optional[dict] = None) -> dict:
    """GET a forum API endpoint; return parsed JSON.

    Exits with EXIT_UNREACHABLE on connection failure.
    Exits with EXIT_NOT_FOUND on 404.
    Exits with EXIT_VALIDATION on 400.
    """
    if params:
        import urllib.parse
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if qs:
            url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
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

def cmd_status(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    forum_url = _resolve_forum_url(config)
    read_cursor = _read_cursor(READ_CURSOR_PATH)
    cursor_str = _cursor_str(read_cursor)

    # GET /api/agents/online?agent=<self>  (also bumps last_seen)
    online_data = _api_get(
        f"{forum_url}/api/agents/online",
        params={"agent": agent_name} if agent_name else None,
    )

    # GET /api/threads?since=<cursor>  — count new threads since read cursor
    threads_params: dict = {}
    if read_cursor is not None:
        threads_params["since"] = cursor_str
    if agent_name:
        threads_params["agent"] = agent_name
    threads_data = _api_get(f"{forum_url}/api/threads", params=threads_params or None)

    new_count = len(threads_data.get("threads", []))
    online_count = online_data.get("count", 0)
    registered = online_data.get("registered", 0)

    if args.format == "json":
        output = {
            "agent": agent_name or "(unknown)",
            "forum_url": forum_url,
            "read_cursor": cursor_str,
            "new_since_cursor": new_count,
            "online_count": online_count,
            "registered": registered,
        }
        print(json.dumps(output, indent=2))
        # Advance cursor on --ack
        if args.ack and threads_data.get("threads"):
            _advance_cursor_to_latest(threads_data["threads"])
        return

    # Human / agent-readable
    print(f"agent:        {agent_name or '(unknown)'}")
    print(f"forum url:    {forum_url}")
    print(f"read cursor:  {cursor_str}")
    print(f"new posts:    {new_count} since last read")
    print(f"online:       {online_count} of {registered} registered")

    if new_count > 0:
        if read_cursor is not None:
            print(
                f"\n  run `forum list` to see threads"
                f"  or `forum list --since {cursor_str}` for new only"
            )
        else:
            print(
                f"\n  run `forum list` to see threads"
                f"  or `forum read <id>` to start your read cursor"
            )

    # Advance cursor on --ack
    if args.ack and threads_data.get("threads"):
        _advance_cursor_to_latest(threads_data["threads"])
        print(f"  (read cursor advanced to now)")


def _advance_cursor_to_latest(threads: list) -> None:
    """Advance read cursor to the latest last_activity_at in the thread list."""
    latest: Optional[datetime] = None
    for t in threads:
        ts_str = t.get("last_activity_at")
        if ts_str:
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if latest is None or dt > latest:
                    latest = dt
            except ValueError:
                pass
    if latest is not None:
        current = _read_cursor(READ_CURSOR_PATH)
        if current is None or latest > current:
            _write_cursor(READ_CURSOR_PATH, latest)


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

    # Advance read cursor to thread's last_activity_at (monotonic)
    last_act = thread.get("last_activity_at")
    if last_act:
        try:
            new_ts = datetime.fromisoformat(last_act.replace("Z", "+00:00"))
            if new_ts.tzinfo is None:
                new_ts = new_ts.replace(tzinfo=timezone.utc)
            current = _read_cursor(READ_CURSOR_PATH)
            if current is None or new_ts > current:
                _write_cursor(READ_CURSOR_PATH, new_ts)
        except ValueError:
            pass


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
        print(f"  {name}{pair_part}")


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

    # Agent name is required for all subcommands that talk to the server
    # (cursor is local-only; status/list/read need it for the ?agent= bump)
    if args.subcommand not in ("cursor",):
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
    elif args.subcommand == "online":
        cmd_online(args, config, agent_name)
    elif args.subcommand == "cursor":
        cmd_cursor(args, config, agent_name)
    else:
        parser.print_help()
        sys.exit(EXIT_VALIDATION)


if __name__ == "__main__":
    main()
