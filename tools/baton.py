#!/usr/bin/env python3
"""baton — Multi-Agent Turn-State CLI

A thin CLI wrapping the baton file protocol at
/home/agents-shared/projects/. Provides explicit turn declarations for
shared projects (PRs, designs, etc.) so agents always know whose move it
is next.

Eleven subcommands:
  init        Create a new project baton file
  flip        Pass the baton to another participant (PR layer)
  claim       Take a Project-layer baton from the pool (pool → self)
  release     Return a Project-layer baton to the pool (self → lei)
  status      List active projects (not merged or cancelled)
  mine        Shorthand for status --mine
  show        Display a project file
  close       Mark a project merged or cancelled
  reopen      Flip a closed baton back to an active status (inverse of close)
  rename      Update the human-readable project title
  anchor      Set or update the github: anchor on an existing baton

Design: Lei + Borges, 2026-05-28 (solving the importance-inflation-derivation-shaped misalignment
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

# Multi-install precedence: prefer ENGRAM_DATA_DIR (server's canonical), fall back
# to ENGRAM_HOME (the conventional name), then default. Resolves the multi-install
# split-brain where operator sets one var but not the other (#471, follow-up to #354).
ENGRAM_HOME = (
    os.environ.get("ENGRAM_DATA_DIR")
    or os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
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

# GitHub anchor value validation: pr/<N> or project/<N>
_GITHUB_ANCHOR_RE = re.compile(r"^(pr|project)/\d+$")


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

    # Validate --github anchor value if provided
    github_anchor = getattr(args, "github", None)
    if github_anchor is not None:
        github_anchor = github_anchor.strip().lower()
        if not _GITHUB_ANCHOR_RE.match(github_anchor):
            print(
                f"baton init: invalid --github value '{github_anchor}'. "
                "Expected pr/<N> or project/<N> (e.g. pr/490, project/4).",
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
    ]
    if github_anchor:
        content_lines.append(f"github: {github_anchor}")
    content_lines += [
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
    if github_anchor:
        print(f"  github:       {github_anchor}")


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
# Subcommand: claim  (Project layer — pool sentinel → self)
# ---------------------------------------------------------------------------

# Pool sentinel: the name held between active agent claims.
# Convention: "lei" (Lei's personal sentinel used across baton files).
# Escape valve: if the sentinel changes, update this constant.
_POOL_SENTINEL = "lei"


def cmd_claim(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Claim a Project-layer baton from the pool.

    Equivalent to 'baton flip <project-id> <self> "claimed"' but makes the
    Project-layer grab/ungrab vocabulary first-class. Only succeeds when
    turn == pool sentinel (lei); refuses to steal from another agent.
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton claim: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton claim: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton claim: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate: current turn must be the pool sentinel
    current_turn = fields.get("turn", "").strip().lower()
    if current_turn != _POOL_SENTINEL:
        print(
            f"baton claim: refusing to steal claim from {current_turn}; "
            "use 'baton flip <project-id> <self> <reason>' if you have an "
            "explicit handoff agreement.",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE)

    # Validate: agent must be in participants
    if not agent_name:
        print(
            "baton claim: cannot determine agent name. "
            "Set agent_name in config.json or ensure $USER is set.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    participants_str = fields.get("participants", "").strip()
    participants = _parse_participants(participants_str) if participants_str else []
    if agent_name.lower() not in participants:
        print(
            f"baton claim: '{agent_name}' is not a participant in {project_id}. "
            f"Participants: {participants}.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Check project is not closed
    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton claim: project '{project_id}' is {current_status} — "
            "cannot claim a closed project.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    now = _now_iso()
    self_name = agent_name.lower()

    # Atomic write: turn → self, turn_since → now, turn_reason → "claimed"
    updates = {
        "turn": self_name,
        "turn_since": now,
        "turn_reason": '"claimed"',
    }
    updated_text = _update_frontmatter(text, updates)

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append turn log entry
    log_entry = f"- {now} {_POOL_SENTINEL} → {self_name}: claimed"
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton claim: {project_id} → {self_name}")


# ---------------------------------------------------------------------------
# Subcommand: release  (Project layer — self → pool sentinel)
# ---------------------------------------------------------------------------

def cmd_release(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Release a Project-layer baton back to the pool.

    Equivalent to 'baton flip <project-id> lei <reason>' but restricted to
    the current holder (only you can release what you hold). With --done,
    also appends '(done)' to the project title (idempotent).
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    mark_done = args.done
    reason = args.reason

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton release: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton release: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton release: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Validate: current turn must be this agent (only the holder can release)
    if not agent_name:
        print(
            "baton release: cannot determine agent name. "
            "Set agent_name in config.json or ensure $USER is set.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    current_turn = fields.get("turn", "").strip().lower()
    self_name = agent_name.lower()
    if current_turn != self_name:
        print(
            f"baton release: only the current holder ({current_turn}) can release.",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE)

    # Check project is not closed
    current_status = fields.get("status", "").strip().lower()
    if current_status in CLOSED_STATUSES:
        print(
            f"baton release: project '{project_id}' is {current_status} — "
            "cannot release a closed project.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    now = _now_iso()

    # Build frontmatter updates: turn → lei, turn_since → now, turn_reason → reason
    updates: dict = {
        "turn": _POOL_SENTINEL,
        "turn_since": now,
        "turn_reason": f'"{reason}"',
    }

    # --done: append "(done)" to title (idempotent)
    title_renamed = False
    if mark_done:
        current_title = fields.get("title", "").strip()
        if not current_title.endswith("(done)"):
            new_title = current_title + " (done)"
            updates["title"] = new_title
            title_renamed = True

    updated_text = _update_frontmatter(text, updates)

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append turn log entry (and a second line if title was renamed)
    log_entry = f"- {now} {self_name} → {_POOL_SENTINEL}: {reason}"
    if title_renamed:
        log_entry += f"\n- {now} title marked (done)"
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton release: {project_id} → {_POOL_SENTINEL}")
    if mark_done:
        print("  marked (done)")


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
# Subcommand: reopen
# ---------------------------------------------------------------------------

# Active statuses that reopen can target (complement of CLOSED_STATUSES).
ACTIVE_STATUSES = {"planning", "in-progress", "in-review"}


def cmd_reopen(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Flip a closed baton back to an active status.

    Inverse of cmd_close. Only operates on batons with status: merged or
    cancelled. Refuses if the baton is already in a non-closed status.
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    new_status = args.status.strip().lower()

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    if new_status not in ACTIVE_STATUSES:
        print(
            f"baton reopen: --status must be one of: {', '.join(sorted(ACTIVE_STATUSES))}. "
            f"Got: '{new_status}'. Use 'baton close' for merged/cancelled transitions.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton reopen: project not found: '{project_id}' "
            f"(looked for {dest}).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton reopen: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, _body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton reopen: cannot parse frontmatter from '{dest}'.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    current_status = fields.get("status", "").strip().lower()
    if current_status not in CLOSED_STATUSES:
        print(
            f"baton reopen: project {project_id} is not closed (status: {current_status}); "
            "use 'baton flip' to change turn or 'baton close' to archive.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    now = _now_iso()
    invoker = agent_name or "unknown"

    # Update status in frontmatter
    updated_text = _update_frontmatter(text, {"status": new_status})

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append audit-log entry
    log_entry = f"- {now} reopened → {invoker}: status was {current_status} → {new_status}"
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton reopen: {project_id} → {new_status}")


# ---------------------------------------------------------------------------
# Subcommand: rename
# ---------------------------------------------------------------------------

def cmd_rename(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    new_title = args.title

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    # Validate title before touching the filesystem
    stripped_title = new_title.strip()
    if not stripped_title:
        print(
            "baton rename: --title must be non-empty.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)
    if len(stripped_title) > 200:
        print(
            f"baton rename: --title too long ({len(stripped_title)} chars); "
            "max 200. Did you accidentally paste a large block?",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)
    if "\n" in stripped_title or "\r" in stripped_title:
        print(
            "baton rename: --title must not contain newlines (YAML safety).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton rename: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton rename: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton rename: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    if "title" not in fields:
        print(
            f"baton rename: 'title' field absent in '{dest}' — "
            "refusing to write to a possibly corrupted file.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    old_title = fields["title"]

    # Update frontmatter (preserves field order via _update_frontmatter)
    updated_text = _update_frontmatter(text, {"title": stripped_title})

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append audit line to turn log
    now = _now_iso()
    log_entry = (
        f'- {now} renamed: title from "{old_title}" to "{stripped_title}"'
    )
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f'baton rename: {project_id} title → "{stripped_title}"')


# ---------------------------------------------------------------------------
# Subcommand: anchor
# ---------------------------------------------------------------------------

def cmd_anchor(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    """Set or update the github: anchor on an existing baton.

    Mirrors cmd_rename's atomic frontmatter-update pattern:
    read → validate → _update_frontmatter → atomic write → audit line.
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
    project_id = args.project_id
    github_anchor = args.github.strip().lower()

    err = _validate_project_id(project_id)
    if err:
        print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    # Validate anchor value
    if not _GITHUB_ANCHOR_RE.match(github_anchor):
        print(
            f"baton anchor: invalid --github value '{github_anchor}'. "
            "Expected pr/<N> or project/<N> (e.g. pr/490, project/4).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    dest = _project_path(project_id, projects_dir)
    if not dest.exists():
        print(
            f"baton anchor: project not found: '{project_id}' "
            f"(looked for {dest}). "
            "Use 'baton status' to see active projects.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    try:
        text = dest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"baton anchor: cannot read '{dest}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    fields, body = _parse_frontmatter(text)
    if not fields:
        print(
            f"baton anchor: cannot parse frontmatter from '{dest}'. "
            "The file may be malformed.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    old_anchor = fields.get("github", "").strip()

    # Update (or add) the github: field in frontmatter
    updated_text = _update_frontmatter(text, {"github": github_anchor})

    # Re-parse to get body after frontmatter update
    _updated_fields, updated_body = _parse_frontmatter(updated_text)

    # Append audit line to turn log
    now = _now_iso()
    if old_anchor:
        log_entry = (
            f'- {now} anchor updated: github from "{old_anchor}" to "{github_anchor}"'
        )
    else:
        log_entry = f'- {now} anchor set: github → {github_anchor}'
    final_body = _append_turn_log(updated_body, log_entry)

    # Reconstruct final content: frontmatter + updated body
    fm_m = _FRONTMATTER_RE.match(updated_text)
    if fm_m:
        fm_section = updated_text[:fm_m.end()]
        final_content = fm_section.rstrip("\n") + "\n" + final_body
    else:
        final_content = updated_text

    _atomic_write(dest, final_content)
    print(f"baton anchor: {project_id} github → {github_anchor}")


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
    p_init.add_argument(
        "--github", metavar="ANCHOR",
        help=(
            "GitHub anchor for live status in the auto-pull hook. "
            "Format: pr/<N> or project/<N> (e.g. pr/490, project/4). "
            "Without this, only batons named PR-<N> resolve automatically."
        ),
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

    # -- claim --
    p_claim = subparsers.add_parser(
        "claim",
        help="Claim a Project-layer baton from the pool (pool → self)",
        description=(
            "Take a Project-layer baton for yourself. Only succeeds when the "
            "current turn is the pool sentinel (lei). Refuses to steal from "
            "another agent — use 'baton flip' with an explicit handoff agreement "
            "if needed.\n\n"
            "Vocabulary distinction: claim/release are Project-layer verbs "
            "(single driver, rare passes). flip is the PR-layer verb (frequent "
            "passing through fairy → reviewer → colleague → maintainer)."
        ),
    )
    p_claim.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier (e.g. pool-cli-gap, DESIGN-trust-tier-v2)",
    )

    # -- release --
    p_release = subparsers.add_parser(
        "release",
        help="Return a Project-layer baton to the pool (self → lei)",
        description=(
            "Release a Project-layer baton back to the pool sentinel (lei). "
            "Only the current holder can release. Use --done to also append "
            "'(done)' to the project title (idempotent).\n\n"
            "Vocabulary distinction: claim/release are Project-layer verbs "
            "(single driver, rare passes). flip is the PR-layer verb (frequent "
            "passing through fairy → reviewer → colleague → maintainer)."
        ),
    )
    p_release.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_release.add_argument(
        "--done", action="store_true", default=False,
        help="Also append '(done)' to the project title (idempotent)",
    )
    p_release.add_argument(
        "--reason", metavar="TEXT", default="released",
        help="Short description of why the baton is being released (default: released)",
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

    # -- reopen --
    p_reopen = subparsers.add_parser(
        "reopen",
        help="Flip a closed baton back to an active status (inverse of close)",
        description=(
            "Flip a closed baton (status: merged or cancelled) back to an active status.\n"
            "Inverse of 'baton close'. Refuses if the baton is already active — use\n"
            "'baton flip' instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_reopen.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_reopen.add_argument(
        "--status",
        choices=sorted(ACTIVE_STATUSES),
        default="in-progress",
        help="Active status to set (default: in-progress)",
    )

    # -- rename --
    p_rename = subparsers.add_parser(
        "rename",
        help="Update the human-readable project title",
        description=(
            "Replace the title: field in the project frontmatter with a new "
            "value, atomically. Appends an audit line to the turn log. "
            "Does not change the project ID or file name."
        ),
    )
    p_rename.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_rename.add_argument(
        "--title", metavar="TEXT", required=True,
        help="New human-readable title (max 200 chars, no newlines)",
    )

    # -- anchor --
    p_anchor = subparsers.add_parser(
        "anchor",
        help="Set or update the github: anchor on an existing baton",
        description=(
            "Set or replace the github: frontmatter field on an existing baton "
            "file, enabling live GitHub status in the auto-pull hook. "
            "Atomically updates the field and appends an audit line. "
            "Use this for batons already created without --github, or to "
            "correct an existing anchor."
        ),
    )
    p_anchor.add_argument(
        "project_id",
        metavar="PROJECT-ID",
        help="Project identifier",
    )
    p_anchor.add_argument(
        "--github", metavar="ANCHOR", required=True,
        help=(
            "GitHub anchor value: pr/<N> or project/<N> (e.g. pr/490, project/4)"
        ),
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
    elif args.subcommand == "claim":
        cmd_claim(args, config, agent_name)
    elif args.subcommand == "release":
        cmd_release(args, config, agent_name)
    elif args.subcommand == "status":
        cmd_status(args, config, agent_name)
    elif args.subcommand == "mine":
        cmd_mine(args, config, agent_name)
    elif args.subcommand == "show":
        cmd_show(args, config, agent_name)
    elif args.subcommand == "close":
        cmd_close(args, config, agent_name)
    elif args.subcommand == "reopen":
        cmd_reopen(args, config, agent_name)
    elif args.subcommand == "rename":
        cmd_rename(args, config, agent_name)
    elif args.subcommand == "anchor":
        cmd_anchor(args, config, agent_name)
    else:
        parser.print_help()
        sys.exit(EXIT_VALIDATION)


if __name__ == "__main__":
    main()
