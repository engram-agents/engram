#!/usr/bin/env python3
"""baton — Multi-Agent Turn-State CLI

A thin CLI wrapping the baton file protocol at
/home/agents-shared/projects/. Provides explicit turn declarations for
shared projects (PRs, designs, etc.) so agents always know whose move it
is next.

Six subcommands:
  init        Create a new project baton file
  flip        Pass the baton to another participant
  status      List active projects (not merged or cancelled)
  mine        Shorthand for status --mine
  show        Display a project file
  close       Mark a project merged or cancelled

Design: Lei + Borges, 2026-05-28 (solving the dv_0038-shaped misalignment
incident where Borges and Ariadne disagreed about whose court PR #425 was in).

Protocol ref: docs/baton-protocol.md
Hook ref:     hooks/claude/engram-baton-prompt-hook.py
"""

import argparse
import json
import os
import pwd
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Environment + paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", str(Path.home() / ".engram"))
BATON_PROJECTS_DIR = os.environ.get("BATON_PROJECTS_DIR", "/home/agents-shared/projects")

# Exit codes
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3

# Valid project statuses
VALID_STATUSES = {"planning", "in-progress", "in-review", "merged", "cancelled"}
CLOSED_STATUSES = {"merged", "cancelled"}

# ---------------------------------------------------------------------------
# Config helpers (mirrors ia.py conventions)
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


def _is_multi_agent_mode(projects_dir: Optional[Path] = None) -> bool:
    """True if baton projects directory exists (multi-agent gate).

    Baton's multi-agent guard is the projects directory itself — if it
    doesn't exist, we're in single-agent mode (or the operator hasn't
    deployed baton). This is simpler than ia.py's config-based gate because
    baton's deployment model is: multi-agent operators mkdir the directory.
    """
    if projects_dir is None:
        projects_dir = Path(BATON_PROJECTS_DIR)
    return projects_dir.is_dir()


# ---------------------------------------------------------------------------
# Frontmatter parsing and generation
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)

# Participants list format: [alice, bob] or alice,bob
_PARTICIPANTS_LIST_RE = re.compile(r"^\[?(.*?)\]?$")


def _parse_frontmatter(text: str) -> tuple:
    """Extract YAML-ish frontmatter from a baton markdown file.

    Returns (fields_dict, body_text). On parse failure returns ({}, text).
    Fields are returned as strings; callers parse participants specially.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_block = m.group(1)
    body = text[m.end():]
    fields: dict = {}
    for field_m in _FRONTMATTER_FIELD_RE.finditer(fm_block):
        key = field_m.group(1).strip().lower()
        val = field_m.group(2).strip()
        fields[key] = val
    return fields, body


def _parse_participants(participants_str: str) -> list:
    """Parse participants field: '[borges, ariadne]' or 'borges,ariadne'.

    Returns a list of lowercase stripped agent names.
    """
    # Strip surrounding brackets if present
    m = _PARTICIPANTS_LIST_RE.match(participants_str.strip())
    if m:
        inner = m.group(1)
    else:
        inner = participants_str
    parts = [p.strip().lower() for p in inner.split(",") if p.strip()]
    return parts


def _format_participants(participants: list) -> str:
    """Format participants list for frontmatter: [borges, ariadne]"""
    return "[" + ", ".join(participants) + "]"


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp; return None on failure."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_str(ts: Optional[datetime]) -> str:
    """Return human-readable age string like '1h ago', '2d ago'."""
    if ts is None:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - ts
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
# Project ID validation
# ---------------------------------------------------------------------------

_PROJECT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*-[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validate_project_id(project_id: str) -> Optional[str]:
    """Validate project ID format (TYPE-ID). Returns error string or None."""
    if not _PROJECT_ID_RE.match(project_id):
        return (
            f"baton: invalid project ID '{project_id}' — "
            "expected TYPE-ID format (e.g. PR-425, DESIGN-trust-tier-v2). "
            "Must start with a letter, use only letters/digits/hyphens/underscores, "
            "and contain at least one hyphen separating type from id."
        )
    return None


def _project_path(project_id: str, projects_dir: Optional[Path] = None) -> Path:
    """Resolve the filesystem path for a project file."""
    if projects_dir is None:
        projects_dir = Path(BATON_PROJECTS_DIR)
    return projects_dir / f"{project_id}.md"


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

def _atomic_write(dest: Path, content: str) -> None:
    """Write content to dest atomically via tmp + os.rename.

    Writes to <dest>.tmp first, then renames to dest. On POSIX systems
    os.rename is atomic within the same filesystem — a concurrent reader
    cannot see a partial file.
    """
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.rename(str(tmp), str(dest))
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"baton: write failed: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)


# ---------------------------------------------------------------------------
# Frontmatter replacement helper
# ---------------------------------------------------------------------------

def _update_frontmatter(text: str, updates: dict) -> str:
    """Apply updates dict to frontmatter fields; return full updated text.

    Replaces individual frontmatter values in-place (preserving order of
    other fields and the body). If a key from updates doesn't exist in the
    frontmatter, it is appended before the closing ---.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter — can't update; caller must handle
        return text

    fm_block = m.group(1)
    body = text[m.end():]
    fm_lines = fm_block.split("\n")
    found_keys = set()

    new_lines = []
    for line in fm_lines:
        fm = _FRONTMATTER_FIELD_RE.match(line)
        if fm:
            key = fm.group(1).strip().lower()
            if key in updates:
                new_lines.append(f"{key}: {updates[key]}")
                found_keys.add(key)
                continue
        new_lines.append(line)

    # Append any keys that weren't already present
    for key, val in updates.items():
        if key not in found_keys:
            new_lines.append(f"{key}: {val}")

    new_fm_block = "\n".join(new_lines)
    return f"---\n{new_fm_block}\n---\n{body}"


# ---------------------------------------------------------------------------
# Turn log helpers
# ---------------------------------------------------------------------------

def _append_turn_log(body: str, entry: str) -> str:
    """Append a new turn log entry to the body.

    Finds the '## Turn log' section and appends. Creates the section if
    absent.
    """
    section_header = "## Turn log"
    if section_header in body:
        # Append at end of the turn log section
        return body.rstrip() + "\n" + entry + "\n"
    else:
        # Create the section
        separator = "\n\n" if body.strip() else ""
        return body.rstrip() + separator + f"\n{section_header}\n\n" + entry + "\n"


# ---------------------------------------------------------------------------
# Project scanning
# ---------------------------------------------------------------------------

def _scan_projects(
    projects_dir: Path,
    agent_name: str = "",
    active_only: bool = True,
) -> list:
    """Scan projects_dir for baton project files.

    Returns list of dicts with keys:
      project_id, title, status, turn, turn_since, turn_reason, participants, path
    Files that don't parse cleanly are silently skipped.
    Sorts by turn_since ascending (oldest baton first).
    """
    if not projects_dir.is_dir():
        return []

    results = []
    for md_file in projects_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        fields, _body = _parse_frontmatter(text)
        if not fields:
            continue

        project_id = fields.get("project", "").strip()
        if not project_id:
            # Try inferring from filename
            project_id = md_file.stem

        status = fields.get("status", "").strip().lower()
        turn = fields.get("turn", "").strip().lower()
        turn_since_str = fields.get("turn_since", "").strip()
        turn_reason = fields.get("turn_reason", "").strip()
        # Strip surrounding quotes from turn_reason if present
        if turn_reason.startswith('"') and turn_reason.endswith('"'):
            turn_reason = turn_reason[1:-1]
        title = fields.get("title", "").strip()
        participants_str = fields.get("participants", "").strip()
        participants = _parse_participants(participants_str) if participants_str else []
        turn_since = _parse_iso(turn_since_str)

        if active_only and status in CLOSED_STATUSES:
            continue

        results.append({
            "project_id": project_id,
            "title": title,
            "status": status,
            "turn": turn,
            "turn_since": turn_since,
            "turn_since_str": turn_since_str,
            "turn_reason": turn_reason,
            "participants": participants,
            "path": str(md_file),
        })

    results.sort(key=lambda p: p["turn_since"] or datetime.min.replace(tzinfo=timezone.utc))
    return results


# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)

    if not projects_dir.is_dir():
        print(
            f"baton: projects directory not found: {projects_dir}. "
            "baton is a multi-agent tool. Create the directory first: "
            f"mkdir -p {projects_dir}",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)

    project_id = args.project_id
    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if dest.exists():
        print(
            f"baton init: project file already exists: {dest}. "
            "Use 'baton flip' to pass the baton, or 'baton show' to view.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Parse participants
    participants_raw = args.participants
    participants = [p.strip().lower() for p in participants_raw.split(",") if p.strip()]
    if len(participants) < 1:
        print(
            "baton init: --participants requires at least one agent name "
            "(e.g. --participants borges,ariadne).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Default turn: the invoking agent (if in participants) else first participant
    if args.turn:
        initial_turn = args.turn.strip().lower()
    elif agent_name and agent_name.lower() in participants:
        initial_turn = agent_name.lower()
    else:
        initial_turn = participants[0]

    # Validate initial turn
    if initial_turn != "lei" and initial_turn not in participants:
        print(
            f"baton init: turn '{initial_turn}' must be in participants "
            f"{participants} or 'lei'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    status = args.status or "planning"
    if status not in VALID_STATUSES:
        print(
            f"baton init: invalid status '{status}'. "
            f"Valid statuses: {', '.join(sorted(VALID_STATUSES))}.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    now = _now_iso()
    participants_str = _format_participants(participants)
    title = args.title or project_id

    # Build the initial reason
    init_reason = f"project initialized by {agent_name or 'unknown'}"

    content_lines = [
        "---",
        f"project: {project_id}",
        f"title: {title}",
        f"status: {status}",
        f"turn: {initial_turn}",
        f"turn_since: {now}",
        f'turn_reason: "{init_reason}"',
        f"participants: {participants_str}",
        "---",
        "",
        f"# {project_id} — {title}",
        "",
        "",
        "## Turn log",
        "",
        f"- {now} initialized → {initial_turn}: {init_reason}",
        "",
    ]
    content = "\n".join(content_lines)

    _atomic_write(dest, content)
    print(f"baton init: created {dest}")
    print(f"  project:      {project_id}")
    print(f"  title:        {title}")
    print(f"  participants: {', '.join(participants)}")
    print(f"  turn:         {initial_turn}")
    print(f"  status:       {status}")


# ---------------------------------------------------------------------------
# Subcommand: flip
# ---------------------------------------------------------------------------

def cmd_flip(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    to_agent = args.to.strip().lower()
    reason = args.reason

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton flip: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton flip: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton flip: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate: to_agent must be in participants or 'lei'
    participants_str = fields.get("participants", "").strip()
    participants = _parse_participants(participants_str) if participants_str else []

    if to_agent != "lei" and to_agent not in participants:
        print(
            f"baton flip: '{to_agent}' is not a participant in {project_id}. "
            f"Participants: {participants}. "
            "Only participants (or 'lei') can receive the baton.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Check project is not closed
    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton flip: project '{project_id}' is {current_status} — "
            "cannot flip a closed project.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    now = _now_iso()
    from_agent = fields.get("turn", agent_name or "unknown").strip().lower()

    # Update frontmatter
    updates = {
        "turn": to_agent,
        "turn_since": now,
        "turn_reason": f'"{reason}"',
    }
    updated_text = _update_frontmatter(text, updates)

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append turn log entry
    log_entry = f"- {now} {from_agent} → {to_agent}: {reason}"
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton flip: {project_id} → {to_agent}")
    print(f"  from:   {from_agent}")
    print(f"  reason: {reason}")
    print(f"  at:     {now}")


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)

    if not projects_dir.is_dir():
        # Single-agent or not deployed — silent exit per spec
        sys.exit(EXIT_OK)

    projects = _scan_projects(projects_dir, agent_name, active_only=True)

    mine_only = getattr(args, "mine", False)
    if mine_only and agent_name:
        projects = [p for p in projects if p["turn"] == agent_name.lower()]

    if not projects:
        if mine_only:
            print("baton mine: no batons in your court.")
        else:
            print("baton status: no active projects.")
        return

    label = "MINE" if mine_only else "ACTIVE"
    print(f"{label} ({len(projects)}):")
    for p in projects:
        ts_age = _age_str(p["turn_since"])
        age_part = f" ({ts_age})" if ts_age else ""
        turn_info = f"turn: {p['turn']}{age_part}"
        reason_part = f" — {p['turn_reason']}" if p['turn_reason'] else ""
        print(f"  {p['project_id']:<28} {turn_info}{reason_part}")
        if p["title"] and p["title"] != p["project_id"]:
            print(f"    {p['title']}")


# ---------------------------------------------------------------------------
# Subcommand: mine
# ---------------------------------------------------------------------------

def cmd_mine(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Shorthand for status --mine."""
    # Inject mine=True and delegate to cmd_status
    args.mine = True
    cmd_status(args, config, agent_name)


# ---------------------------------------------------------------------------
# Subcommand: show
# ---------------------------------------------------------------------------

def cmd_show(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton show: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton show: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    print(text, end="" if text.endswith("\n") else "\n")


# ---------------------------------------------------------------------------
# Subcommand: close
# ---------------------------------------------------------------------------

def cmd_close(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    new_status = args.status.strip().lower()

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    if new_status not in CLOSED_STATUSES:
        print(
            f"baton close: --status must be one of: {', '.join(sorted(CLOSED_STATUSES))}. "
            f"Got: '{new_status}'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton close: project not found: '{project_id}' "
            f"(looked for {dest}).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton close: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, _body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton close: cannot parse frontmatter from '{dest}'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton close: project '{project_id}' is already {current_status}.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    updated_text = _update_frontmatter(text, {"status": new_status})
    _atomic_write(dest, updated_text)
    print(f"baton close: {project_id} → {new_status}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baton",
        description=(
            "Multi-agent turn-state CLI — declare whose move it is on a "
            "shared project via explicit baton files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Protocol docs: docs/baton-protocol.md\n"
            "Projects dir:  /home/agents-shared/projects/ (manual deploy)\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")
    subparsers.required = True

    # -- init --
    p_init = subparsers.add_parser(
        "init",
        help="Create a new project baton file",
        description=(
            "Create a baton file for a shared project. Fails if the file "
            "already exists. Turn defaults to the invoking agent if they are "
            "in the participants list."
        ),
    )
    p_init.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier in TYPE-ID format (e.g. PR-425, DESIGN-trust-tier-v2)",
    )
    p_init.add_argument(
        "--title", metavar="TEXT", required=True,
        help="Human-readable project title",
    )
    p_init.add_argument(
        "--participants", metavar="AGENTS", required=True,
        help="Comma-separated list of participant agent names (e.g. borges,ariadne)",
    )
    p_init.add_argument(
        "--status",
        choices=list(VALID_STATUSES),
        default="planning",
        help="Initial project status (default: planning)",
    )
    p_init.add_argument(
        "--turn", metavar="AGENT",
        help="Who holds the baton initially (default: invoking agent if in participants)",
    )

    # -- flip --
    p_flip = subparsers.add_parser(
        "flip",
        help="Pass the baton to another participant",
        description=(
            "Atomically update turn, turn_since, and turn_reason in the "
            "project file, and append a turn-log line. "
            "TO must be a participant or 'lei'."
        ),
    )
    p_flip.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_flip.add_argument(
        "to",
        metavar="TO",
        help="Agent or 'lei' to pass the baton to",
    )
    p_flip.add_argument(
        "reason",
        metavar="REASON",
        help="Short description of why the baton is being passed",
    )

    # -- status --
    p_status = subparsers.add_parser(
        "status",
        help="List active projects (sorted by turn_since ascending)",
        description=(
            "List all active projects (status not in merged, cancelled). "
            "Use --mine to filter to projects where turn == invoking agent."
        ),
    )
    p_status.add_argument(
        "--mine", action="store_true", default=False,
        help="Only show projects where the baton is in my court",
    )

    # -- mine --
    p_mine = subparsers.add_parser(
        "mine",
        help="Shorthand for 'baton status --mine'",
        description="Show projects where the baton is currently in your court.",
    )

    # -- show --
    p_show = subparsers.add_parser(
        "show",
        help="Display a project file (read-only)",
        description="Print the full contents of a project baton file.",
    )
    p_show.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )

    # -- close --
    p_close = subparsers.add_parser(
        "close",
        help="Mark a project merged or cancelled",
        description=(
            "Update the project's status to 'merged' or 'cancelled'. "
            "Closed projects no longer appear in 'baton status' or 'baton mine'."
        ),
    )
    p_close.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_close.add_argument(
        "--status",
        required=True,
        choices=list(CLOSED_STATUSES),
        help="Final status: merged or cancelled",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = _load_config()
    agent_name = _get_agent_name(config)

    # Dispatch
    if args.subcommand == "init":
        cmd_init(args, config, agent_name)
    elif args.subcommand == "flip":
        cmd_flip(args, config, agent_name)
    elif args.subcommand == "status":
        cmd_status(args, config, agent_name)
    elif args.subcommand == "mine":
        cmd_mine(args, config, agent_name)
    elif args.subcommand == "show":
        cmd_show(args, config, agent_name)
    elif args.subcommand == "close":
        cmd_close(args, config, agent_name)
    else:
        parser.print_help()
        sys.exit(EXIT_VALIDATION)


if __name__ == "__main__":
    main()
