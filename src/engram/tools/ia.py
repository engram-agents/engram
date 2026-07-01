#!/usr/bin/env python3
"""ia — Inter-Agent CLI

A thin CLI wrapping the forum DM API for inter-agent messaging.
Replaces the file-based letter protocol with the forum DM channel.

Seven subcommands:
  list        Show my DM threads (GET /api/dm)
  read        Display messages with a counterpart (GET /api/dm/<counterpart>)
  write       Send a DM — body from stdin, --body-file, or $EDITOR
  mark-read   Advance read cursor to current as_of from /api/updates
  cursor      Show or set the read cursor (as_of integer)
  status      Quick health check (agent name, DM threads, unread count)
  peers       Show known agents from the forum registry

PURE LAN-API: no local inter-agent/ filesystem reads or writes.
If the forum API is unreachable, commands FAIL LOUD (stderr + non-zero exit).

Design: UCS PR3a — pure DM-API migration.
Forum API: /api/dm, /api/updates?kinds=dm, /api/agents/online
Hook ref:   hooks/claude/engram-inter-agent-prompt-hook.py (wired in PR3b)
"""

import argparse
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Explicit path insert ensures this import works when ia.py is loaded via
# importlib (e.g. in direct-import tests) — Python's automatic script-dir
# addition only fires when ia.py is the __main__ entry point.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from forum_api import ForumClient, ForumHttpError, ForumNetworkError, forum_url_from_config

# ---------------------------------------------------------------------------
# Environment + paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

# Read cursor: stores the /api/updates as_of integer.
# Path kept stable so PR3b hook can still read it.
READ_CURSOR_PATH = os.path.join(ENGRAM_HOME, "inter-agent-read-cursor.txt")

# Exit codes per DESIGN §7
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3

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


def _is_multi_agent_mode(config: Optional[dict] = None) -> bool:
    """True if config.json mode == 'multi'. Mirrors engram hook helper."""
    if config is None:
        config = _load_config()
    return config.get("mode", "single") == "multi"


# ---------------------------------------------------------------------------
# Mode gate  (DM commands require multi-agent mode)
# ---------------------------------------------------------------------------


def _check_multi_agent_mode(config: Optional[dict] = None) -> None:
    """Exit with code 3 and actionable message if mode != 'multi'."""
    if config is None:
        config = _load_config()
    if not _is_multi_agent_mode(config):
        config_path = Path(ENGRAM_HOME) / "config.json"
        if not config_path.exists():
            mode_detail = "(config.json missing)"
        else:
            try:
                json.loads(config_path.read_text())
                mode_val = config.get("mode", "unset")
                mode_detail = f"(config.json mode='{mode_val}')"
            except (json.JSONDecodeError, OSError):
                mode_detail = "(config.json invalid — failed to parse)"
        print(
            f"ia: this host is in single-agent mode {mode_detail}; "
            "ia is multi-agent-only.",
            file=sys.stderr,
        )
        print(
            "To enable: set mode='multi' in config.json and restart MCP.",
            file=sys.stderr,
        )
        print(
            "See: inter-agent/README.md §2 (Mode gate).",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE)


# ---------------------------------------------------------------------------
# Cursor helpers  (as_of integer stored at READ_CURSOR_PATH)
# ---------------------------------------------------------------------------


def _read_cursor_int(path: str) -> int:
    """Read the cursor file; return int or 0 if absent/invalid."""
    try:
        raw = Path(path).read_text().strip()
        return int(raw)
    except (OSError, ValueError):
        return 0


def _write_cursor_int(path: str, value: int) -> None:
    """Write the cursor int to file atomically (tmp + os.replace).

    Uses the same atomic tmp + os.replace pattern as hook helpers so
    concurrent reads cannot observe a partial write.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(str(value) + "\n")
        os.replace(tmp, dest)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"ia: cursor write failed: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)


# ---------------------------------------------------------------------------
# Loud-fail API helpers  (mirrors baton.py pattern)
# ---------------------------------------------------------------------------


def _api_get(client: ForumClient, path: str, params: Optional[dict] = None) -> dict:
    """GET via forum API; fail loud on network error or server error."""
    try:
        return client.get(path, params=params)
    except ForumNetworkError as e:
        print(f"ia: forum unreachable — {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        print(f"ia: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)


def _api_post(client: ForumClient, path: str, payload: dict) -> dict:
    """POST via forum API; fail loud on network error or server error."""
    try:
        return client.post(path, payload)
    except ForumNetworkError as e:
        print(f"ia: forum unreachable — {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        print(f"ia: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)


# ---------------------------------------------------------------------------
# Editor helper
# ---------------------------------------------------------------------------


def _find_editor() -> Optional[str]:
    """Resolve the editor to use: $VISUAL -> $EDITOR -> nano -> vi."""
    for envvar in ("VISUAL", "EDITOR"):
        val = os.environ.get(envvar, "").strip()
        if val:
            return val
    for fallback in ("nano", "vi"):
        if shutil.which(fallback) is not None:
            return fallback
    return None


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """List DM threads: GET /api/dm?agent=<me>"""
    forum_url = getattr(args, "forum_url", None) or forum_url_from_config(config)
    client = ForumClient(forum_url)

    data = _api_get(client, "/api/dm", params={"agent": agent_name})
    threads = data.get("threads", [])

    if args.format == "json":
        print(json.dumps(threads, indent=2))
        return

    if not threads:
        print("DM threads (0): no active threads.")
        return

    print(f"DM threads ({len(threads)}):")
    for t in threads:
        print(f"  ↔ {t['counterpart']}")


# ---------------------------------------------------------------------------
# Subcommand: read
# ---------------------------------------------------------------------------


def cmd_read(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Read messages with a counterpart: GET /api/dm/<counterpart>?agent=<me>"""
    forum_url = getattr(args, "forum_url", None) or forum_url_from_config(config)
    client = ForumClient(forum_url)

    counterpart = args.counterpart.strip().lower()
    since_seq = getattr(args, "since_seq", 0) or 0

    try:
        data = client.get(
            f"/api/dm/{urllib.parse.quote(counterpart)}",
            params={"agent": agent_name, "since_seq": str(since_seq)},
        )
    except ForumNetworkError as e:
        print(f"ia read: forum unreachable — {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    except ForumHttpError as e:
        if e.status == 404:
            print(f"(no DM thread with {counterpart})")
            return
        print(f"ia read: server error {e.status}: {e.body}", file=sys.stderr)
        sys.exit(EXIT_IO if e.status >= 500 else EXIT_VALIDATION)

    messages = data.get("messages", [])
    if not messages:
        print(f"(no messages with {counterpart})")
        return

    if args.format == "json":
        print(json.dumps(messages, indent=2))
        return

    for msg in messages:
        print(f"\n--- seq={msg['seq']} from={msg['sender']} at={msg['ts']} ---")
        print(msg["body"])


# ---------------------------------------------------------------------------
# Subcommand: write
# ---------------------------------------------------------------------------


def _encode_body(body_text: str, recipients: list, subject: Optional[str]) -> str:
    """Encode the DM body with optional To/Subject header block.

    Client-side encoding convention (lossless):
      - If multiple recipients: prepend ``**To:** r1, r2`` + blank line.
      - If subject given: prepend ``**Subject:** S`` + blank line.
      - Then the body.

    Encoding order: To-line first (if multi-recipient), then Subject line
    (if given), then body — mirrors typical letter header ordering.
    """
    header_lines: list = []
    if len(recipients) > 1:
        header_lines.append(f"**To:** {', '.join(recipients)}")
        header_lines.append("")
    if subject:
        header_lines.append(f"**Subject:** {subject}")
        header_lines.append("")

    if header_lines:
        return "\n".join(header_lines) + "\n" + body_text.lstrip("\n")
    return body_text


def cmd_write(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Send DM(s): POST /api/dm/<recipient> for each recipient in --to."""
    # Parse and validate recipients
    recipients_raw = args.to or ""
    recipients = [r.strip().lower() for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        print(
            "ia write: --to is required (at least one recipient).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    for r in recipients:
        if not r.replace("-", "").replace("_", "").isalnum():
            print(
                f"ia write: invalid recipient name '{r}' — "
                "agent names may only contain letters, digits, hyphens, and underscores.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)

    # Resolve body source
    body_text: str
    if args.body_file:
        body_path = Path(args.body_file)
        if not body_path.exists():
            print(
                f"ia write: --body-file: '{args.body_file}' not found.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        try:
            body_text = body_path.read_text(encoding="utf-8")
        except OSError as e:
            print(
                f"ia write: --body-file: cannot read '{args.body_file}': {e}",
                file=sys.stderr,
            )
            sys.exit(EXIT_IO)
    elif args.from_stdin or not sys.stdin.isatty():
        # Read body from stdin
        body_text = sys.stdin.read()
    else:
        # Open $EDITOR with a simple template (no frontmatter — body only)
        editor = _find_editor()
        if editor is None:
            print(
                "ia write: no editor available — set $EDITOR or install nano/vi.",
                file=sys.stderr,
            )
            sys.exit(EXIT_STATE)

        subject = getattr(args, "subject", None) or ""
        template_lines = [
            "# Write your message body below this line.",
            "# Lines starting with # are not stripped — include them if meaningful.",
            "",
        ]
        if subject:
            template_lines.insert(0, f"# Subject: {subject}")
            template_lines.insert(1, "")
        template = "\n".join(template_lines) + "\n"

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            prefix="ia-write-",
            delete=False,
        ) as tf:
            tf.write(template)
            tmp_name = tf.name

        try:
            result = subprocess.run([editor, tmp_name])
            if result.returncode != 0:
                print(
                    f"ia write: editor '{editor}' exited with code {result.returncode}.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_IO)
            with open(tmp_name, encoding="utf-8") as f:
                body_text = f.read()
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    if not body_text or not body_text.strip():
        print("ia write: body is empty (nothing to send).", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    subject = getattr(args, "subject", None)
    encoded = _encode_body(body_text, recipients, subject)

    forum_url = getattr(args, "forum_url", None) or forum_url_from_config(config)
    client = ForumClient(forum_url)

    # POST to each recipient (N separate 1:1 DM threads). Collect-and-continue:
    # attempt EVERY recipient, report each, and exit non-zero iff any failed —
    # so a mid-list failure neither aborts the remaining sends nor hides the
    # partial delivery (the multi-recipient send is non-atomic by construction;
    # this makes the partial state visible instead of silently truncating).
    failures = []
    for recipient in recipients:
        try:
            result = client.post(
                f"/api/dm/{urllib.parse.quote(recipient)}",
                {"agent": agent_name, "body": encoded},
            )
        except ForumNetworkError as e:
            print(
                f"ia write: FAILED to {recipient} — forum unreachable: {e}",
                file=sys.stderr,
            )
            failures.append(recipient)
            continue
        except ForumHttpError as e:
            print(
                f"ia write: FAILED to {recipient} — server error {e.status}: {e.body}",
                file=sys.stderr,
            )
            failures.append(recipient)
            continue
        print(
            f"ia write: sent to {recipient} "
            f"(seq={result.get('seq')}, ts={result.get('ts')})"
        )
    if failures:
        print(
            f"ia write: {len(failures)} of {len(recipients)} deliveries FAILED "
            f"({', '.join(failures)}); the others were sent. Re-send only to the "
            f"failed recipients to avoid duplicates.",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)


# ---------------------------------------------------------------------------
# Subcommand: mark-read
# ---------------------------------------------------------------------------


def cmd_mark_read(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Advance read cursor to current as_of: GET /api/updates?kinds=dm."""
    forum_url = getattr(args, "forum_url", None) or forum_url_from_config(config)
    client = ForumClient(forum_url)

    cursor = _read_cursor_int(READ_CURSOR_PATH)

    data = _api_get(
        client,
        "/api/updates",
        params={
            "agent": agent_name,
            "since": str(cursor),
            "kinds": "dm",
        },
    )
    as_of = data.get("as_of", cursor)
    _write_cursor_int(READ_CURSOR_PATH, as_of)
    print(f"ia mark-read: cursor advanced to {as_of}")


# ---------------------------------------------------------------------------
# Subcommand: cursor
# ---------------------------------------------------------------------------


def cmd_cursor(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Show or set the read cursor (as_of integer)."""
    if args.set_val is not None:
        try:
            new_val = int(args.set_val)
        except (ValueError, TypeError):
            print(
                f"ia cursor: --set value must be an integer; got '{args.set_val}'.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        _write_cursor_int(READ_CURSOR_PATH, new_val)
        print(f"ia cursor: set to {new_val}")
    else:
        val = _read_cursor_int(READ_CURSOR_PATH)
        print(f"read cursor: {val}")


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Quick health check: agent name, mode, DM threads, unread count."""
    mode = config.get("mode", "single")
    forum_url = getattr(args, "forum_url", None) or forum_url_from_config(config)
    cursor = _read_cursor_int(READ_CURSOR_PATH)

    client = ForumClient(forum_url)

    # Attempt to get DM thread count (graceful on unreachable — status is diagnostic)
    thread_count: Optional[int] = None
    unread_count: Optional[int] = None

    try:
        dm_data = client.get("/api/dm", params={"agent": agent_name})
        thread_count = len(dm_data.get("threads", []))
    except (ForumNetworkError, ForumHttpError):
        pass

    try:
        upd_data = client.get(
            "/api/updates",
            params={"agent": agent_name, "since": str(cursor), "kinds": "dm"},
        )
        unread_count = len(upd_data.get("updates", []))
    except (ForumNetworkError, ForumHttpError):
        pass

    if args.format == "json":
        output = {
            "agent": agent_name or "(unknown)",
            "mode": mode,
            "forum_url": forum_url,
            "read_cursor": cursor,
            "dm_threads": thread_count,
            "unread_dm_messages": unread_count,
        }
        print(json.dumps(output, indent=2))
        return

    # Human format
    thread_str = str(thread_count) if thread_count is not None else "(unreachable)"
    unread_str = str(unread_count) if unread_count is not None else "(unreachable)"

    print(f"agent:        {agent_name or '(unknown)'}")
    print(f"mode:         {mode}")
    print(f"forum:        {forum_url}")
    print(f"read cursor:  {cursor}")
    print(f"DM threads:   {thread_str}")
    print(f"unread DMs:   {unread_str}")


# ---------------------------------------------------------------------------
# Subcommand: peers
# ---------------------------------------------------------------------------


def cmd_peers(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Show known agents from the forum registry: GET /api/agents/online."""
    forum_url = getattr(args, "forum_url", None) or forum_url_from_config(config)
    client = ForumClient(forum_url)

    data = _api_get(client, "/api/agents/online", params={"agent": agent_name})
    online = data.get("online", [])
    registered = data.get("registered", len(online))

    if args.format == "json":
        print(json.dumps(data, indent=2))
        return

    # Filter out self for peers display
    peers = [
        a for a in online
        if (a.get("name") or "").lower() != agent_name.lower()
    ]

    if not peers:
        print(f"peers (0 of {registered} registered): no peers online.")
        return

    print(f"peers ({len(peers)} of {registered} registered):")
    for a in peers:
        name = a.get("name") or "(unknown)"
        state = a.get("state", "")
        state_str = f"  [{state}]" if state else ""
        print(f"  {name:<16}{state_str}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ia",
        description=(
            "Inter-agent CLI — send and receive direct messages via the forum DM channel."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "All DM commands require multi-agent mode (mode='multi' in config.json).\n"
            "Forum API must be reachable for DM commands; `status` is graceful if unreachable.\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")
    subparsers.required = True

    # -- list --
    p_list = subparsers.add_parser(
        "list",
        help="Show my DM threads (GET /api/dm)",
        description=(
            "List all DM threads this agent has. Each thread is a 1:1 conversation "
            "with one counterpart. Use 'ia read <counterpart>' to see messages."
        ),
    )
    p_list.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )
    p_list.add_argument(
        "--forum-url", dest="forum_url", default=None,
        help=argparse.SUPPRESS,  # internal / testing override
    )

    # -- read --
    p_read = subparsers.add_parser(
        "read",
        help="Display messages with a counterpart (GET /api/dm/<counterpart>)",
        description=(
            "Print all DM messages in a thread with a specific counterpart. "
            "Optionally fetch only messages newer than --since-seq."
        ),
    )
    p_read.add_argument(
        "counterpart",
        metavar="COUNTERPART",
        help="The other agent in the thread (e.g. 'borges')",
    )
    p_read.add_argument(
        "--since-seq", dest="since_seq", type=int, default=0, metavar="N",
        help="Return messages with seq > N (default 0 = all)",
    )
    p_read.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )
    p_read.add_argument(
        "--forum-url", dest="forum_url", default=None,
        help=argparse.SUPPRESS,  # internal / testing override
    )

    # -- write --
    p_write = subparsers.add_parser(
        "write",
        help="Send a DM (body from stdin, --body-file, or $EDITOR)",
        description=(
            "Send a direct message to one or more agents. Body comes from stdin "
            "(piped), --body-file, or $EDITOR (when stdin is a terminal). "
            "For multiple recipients, one DM is posted to each 1:1 thread. "
            "A **To:** header is prepended when sending to multiple recipients "
            "so each recipient knows others were included (no silent BCC)."
        ),
    )
    p_write.add_argument(
        "--to", required=True, metavar="AGENT[,AGENT,...]",
        help=(
            "Recipient(s) — one name or a comma-separated list (e.g. 'ariadne,borges'). "
            "Each recipient gets their own 1:1 DM thread with the same body."
        ),
    )
    p_write.add_argument(
        "--subject", metavar="TEXT", default=None,
        help=(
            "Optional subject line; prepended as '**Subject:** TEXT' before the body."
        ),
    )
    p_write.add_argument(
        "--body-file", dest="body_file", metavar="PATH", default=None,
        help=(
            "Path to a file containing the message body. "
            "Mutually exclusive with piped stdin; $EDITOR is skipped when this is set."
        ),
    )
    p_write.add_argument(
        "--from-stdin", dest="from_stdin", action="store_true",
        help=(
            "Force reading body from stdin even if stdin is a terminal. "
            "Useful in scripts that pipe content explicitly."
        ),
    )
    p_write.add_argument(
        "--forum-url", dest="forum_url", default=None,
        help=argparse.SUPPRESS,  # internal / testing override
    )

    # -- mark-read --
    p_mr = subparsers.add_parser(
        "mark-read",
        aliases=["mr"],
        help="Advance read cursor to current as_of from /api/updates",
        description=(
            "Poll GET /api/updates?kinds=dm and advance the local read cursor "
            "to the returned as_of watermark. Used by the prompt-hook to "
            "surface unread DMs."
        ),
    )
    p_mr.add_argument(
        "--forum-url", dest="forum_url", default=None,
        help=argparse.SUPPRESS,  # internal / testing override
    )

    # -- cursor --
    p_cursor = subparsers.add_parser(
        "cursor",
        help="Show or set the read cursor (as_of integer)",
        description=(
            "Show or set the DM read cursor. The cursor is an integer "
            "(the as_of watermark from /api/updates), stored locally at "
            "~/.engram/inter-agent-read-cursor.txt."
        ),
    )
    cursor_action = p_cursor.add_mutually_exclusive_group()
    cursor_action.add_argument(
        "--show", action="store_true",
        help="Print current cursor value (default)",
    )
    cursor_action.add_argument(
        "--set", dest="set_val", metavar="INT",
        help="Set cursor to this integer value",
    )

    # -- status --
    p_status = subparsers.add_parser(
        "status",
        help="Quick health check (agent name, mode, DM threads, unread count)",
        description=(
            "Show a summary of DM state: agent identity, mode, forum URL, "
            "read cursor, DM thread count, and unread message count."
        ),
    )
    p_status.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )
    p_status.add_argument(
        "--forum-url", dest="forum_url", default=None,
        help=argparse.SUPPRESS,  # internal / testing override
    )

    # -- peers --
    p_peers = subparsers.add_parser(
        "peers",
        help="Show known agents from the forum registry",
        description=(
            "GET /api/agents/online — list agents that are currently online "
            "according to the forum registry."
        ),
    )
    p_peers.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )
    p_peers.add_argument(
        "--forum-url", dest="forum_url", default=None,
        help=argparse.SUPPRESS,  # internal / testing override
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = _load_config()

    # All DM commands require multi-agent mode.
    # status is exempt: it's a diagnostic utility that shows current mode.
    _MODE_GATE_EXEMPT = {"status"}
    if args.subcommand not in _MODE_GATE_EXEMPT:
        _check_multi_agent_mode(config)

    agent_name = _get_agent_name(config)

    # Dispatch
    if args.subcommand == "list":
        cmd_list(args, config, agent_name)
    elif args.subcommand == "read":
        cmd_read(args, config, agent_name)
    elif args.subcommand == "write":
        cmd_write(args, config, agent_name)
    elif args.subcommand == "mark-read":
        cmd_mark_read(args, config, agent_name)
    elif args.subcommand == "cursor":
        cmd_cursor(args, config, agent_name)
    elif args.subcommand == "status":
        cmd_status(args, config, agent_name)
    elif args.subcommand == "peers":
        cmd_peers(args, config, agent_name)
    else:
        parser.print_help()
        sys.exit(EXIT_VALIDATION)


if __name__ == "__main__":
    main()
