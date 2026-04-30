#!/usr/bin/env python3
"""tools/session.py — CLI for managing inter-agent hot-seat sessions.

Usage:
  python3 tools/session.py create <session_id> <participants> [--desc <desc>] [--goal <goal_id>]
  python3 tools/session.py status <session_id> <active|paused|archived>
  python3 tools/session.py touch <session_id>
  python3 tools/session.py list
  python3 tools/session.py show <session_id>
  python3 tools/session.py history <session_id>
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add current dir to path to import helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import channel_dir
from _channel import (
    is_substance_filename, parse_frontmatter, read_frontmatter, atomic_write
)
from _session_context import (
    load_session, save_session, create_session, touch_activity,
    set_status, is_hot_seat_eligible, VALID_STATUSES
)


def cmd_create(args):
    participants = [p.strip() for p in args.participants.split(",")]
    conjectures = [c.strip() for c in args.conjectures.split(",")] if args.conjectures else []
    questions = [q.strip() for q in args.questions.split(",")] if args.questions else []
    
    session = create_session(
        channel_dir(),
        args.session_id,
        participants,
        description=args.desc or "",
        goal_id=args.goal or "",
        linked_conjectures=conjectures,
        linked_questions=questions,
        ttl_seconds=args.ttl,
        hot_seat_enabled=not args.no_hot_seat
    )
    print(f"Created session {args.session_id} at {channel_dir() / f'session_{args.session_id}.json'}")
    print(json.dumps(session, indent=2))


def cmd_status(args):
    session = set_status(channel_dir(), args.session_id, args.status)
    if not session:
        print(f"Error: Session {args.session_id} not found.")
        sys.exit(1)
    print(f"Updated session {args.session_id} status to {args.status}")


def cmd_touch(args):
    session = touch_activity(channel_dir(), args.session_id)
    if not session:
        print(f"Error: Session {args.session_id} not found.")
        sys.exit(1)
    print(f"Touched session {args.session_id}. last_activity_at updated to {session['last_activity_at']}")


def cmd_list(args):
    path = channel_dir()
    session_files = sorted(path.glob("session_*.json"))
    
    if not session_files:
        print("No sessions found.")
        return

    print(f"{'ID':<20} {'Status':<10} {'Hot-Seat?':<10} {'Last Activity'}")
    print("-" * 65)
    
    for p in session_files:
        try:
            session = json.loads(p.read_text())
            sid = session.get("session_id", p.stem.replace("session_", ""))
            status = session.get("status", "unknown")
            eligible = "YES" if is_hot_seat_eligible(session) else "no"
            last = session.get("last_activity_at", "never")
            print(f"{sid:<20} {status:<10} {eligible:<10} {last}")
        except Exception:
            print(f"{p.name:<20} ERROR")


def cmd_show(args):
    session = load_session(channel_dir(), args.session_id)
    if not session:
        print(f"Error: Session {args.session_id} not found.")
        sys.exit(1)
    
    eligible = is_hot_seat_eligible(session)
    print(f"Session: {args.session_id}")
    print(f"Status: {session.get('status')}")
    print(f"Hot-Seat Eligible: {'YES' if eligible else 'no'}")
    print(f"Participants: {', '.join(session.get('participants', []))}")
    print(f"Last Activity: {session.get('last_activity_at')}")
    print(f"TTL: {session.get('expiry_ttl_seconds')}s")
    print(f"Metadata: {json.dumps(session.get('metadata', {}), indent=2)}")


def cmd_history(args):
    path = channel_dir()
    session_id = args.session_id
    
    matches = []
    for p in sorted(path.glob("*.md")):
        front = read_frontmatter(p)
        if front.get("session_id") == session_id:
            matches.append({
                "filename": p.name,
                "from": front.get("from", "?"),
                "to": front.get("to", "?"),
                "timestamp": front.get("timestamp", "?"),
                "message_id": front.get("message_id", "?")
            })
            
    if not matches:
        print(f"No messages found for session {session_id}.")
        return

    print(f"History for session {session_id}:")
    print(f"{'Timestamp':<20} {'From':<10} {'To':<10} {'Filename'}")
    print("-" * 75)
    for m in matches:
        print(f"{m['timestamp']:<20} {m['from']:<10} {m['to']:<10} {m['filename']}")


def cmd_scratchpad(args):
    channel = channel_dir()
    path = channel / f"scratchpad_{args.session_id}.md"
    
    if args.subcommand == "read":
        if not path.exists():
            print(f"Scratchpad {path.name} not found.")
            return
        print(path.read_text())
        
    elif args.subcommand == "write":
        content = sys.stdin.read() if args.content == "-" else args.content
        atomic_write(path, content, mode=0o644)
        print(f"Updated scratchpad {path.name}")
        
    elif args.subcommand == "edit":
        if not args.slot:
            print("Error: --slot is required for 'edit' subcommand.")
            return
        
        content = sys.stdin.read() if args.content == "-" else args.content
        slot_header = f"## [slot:{args.slot}]"
        
        existing_content = ""
        if path.exists():
            existing_content = path.read_text()
            
        import re
        pattern = rf"({re.escape(slot_header)}\n?)(.*?)(\n## \[|$)"
        if re.search(pattern, existing_content, re.DOTALL):
            new_content = re.sub(pattern, rf"\1{content}\n\3", existing_content, flags=re.DOTALL)
        else:
            new_content = existing_content.rstrip() + f"\n\n{slot_header}\n{content}\n"
            
        atomic_write(path, new_content.strip() + "\n", mode=0o644)
        print(f"Updated slot [{args.slot}] in {path.name}")


def main():
    parser = argparse.ArgumentParser(description="Manage inter-agent hot-seat sessions.")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Create
    p_create = subparsers.add_parser("create", help="Create a new session")
    p_create.add_argument("session_id", help="Unique session identifier")
    p_create.add_argument("participants", help="Comma-separated list of participants")
    p_create.add_argument("--desc", help="Optional description")
    p_create.add_argument("--goal", help="Optional goal ID")
    p_create.add_argument("--conjectures", help="Optional comma-separated list of linked conjecture IDs")
    p_create.add_argument("--questions", help="Optional comma-separated list of linked question IDs")
    p_create.add_argument("--ttl", type=int, default=300, help="Expiry TTL in seconds (default 300)")
    p_create.add_argument("--no-hot-seat", action="store_true", help="Disable hot-seat wake-ups for this session")

    # Status
    p_status = subparsers.add_parser("status", help="Update session status")
    p_status.add_argument("session_id", help="Session identifier")
    p_status.add_argument("status", choices=VALID_STATUSES, help="New status")

    # Touch
    p_touch = subparsers.add_parser("touch", help="Reset session TTL timer")
    p_touch.add_argument("session_id", help="Session identifier")

    # List
    subparsers.add_parser("list", help="List all sessions")

    # Show
    p_show = subparsers.add_parser("show", help="Show session details")
    p_show.add_argument("session_id", help="Session identifier")

    # History
    p_hist = subparsers.add_parser("history", help="List session message thread")
    p_hist.add_argument("session_id", help="Session identifier")

    # Scratchpad
    p_scratch = subparsers.add_parser("scratchpad", help="Manage session shared scratchpad")
    p_scratch.add_argument("subcommand", choices=["read", "write", "edit"], help="Scratchpad action")
    p_scratch.add_argument("session_id", help="Session identifier")
    p_scratch.add_argument("content", nargs="?", help="Content to write/edit (use '-' for stdin)")
    p_scratch.add_argument("--slot", help="Slot name for 'edit' command")

    args = parser.parse_args()

    if args.command == "create":
        cmd_create(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "touch":
        cmd_touch(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "history":
        cmd_history(args)
    elif args.command == "scratchpad":
        cmd_scratchpad(args)
    else:
        parser.print_help()



if __name__ == "__main__":
    main()
