#!/usr/bin/env python3
"""UserPromptSubmit hook: surface baton-in-court projects to the agent.

On every prompt, if the baton projects directory exists, scans
/home/agents-shared/projects/ for project files where turn == this agent
and status is active (not merged or cancelled). Injects a summary into
the session context reminding the agent of pending batons.

Unlike the inter-agent hook, baton has no cursor — turn state is the
explicit current-state field in the file, read fresh each prompt. No
surfaced/read cursor mechanism needed; the baton file IS the state.

Anti-patterns guarded:
  - Single-agent guard: if projects directory doesn't exist, exits silently.
    Single-agent users (Lin, Chenni) see zero behavior change.
  - No LLM calls, no network, no DB — file-system reads only.
    Timeout budget: 5 seconds max.
  - Missing/malformed project files are silently skipped (hook must
    not crash on corrupted state).

Part of the baton turn-state system (PR baton). Mirrors the structure
of engram-inter-agent-prompt-hook.py.

Protocol doc: docs/baton-protocol.md
CLI:          tools/baton.py
"""

import json
import os
import pwd
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", str(Path.home() / ".engram"))
BATON_PROJECTS_DIR = os.environ.get("BATON_PROJECTS_DIR", "/home/agents-shared/projects")

# Statuses that mean "closed" — these projects don't surface
CLOSED_STATUSES = {"merged", "cancelled"}

# Frontmatter parsing (same regex as ia.py / inter-agent hook)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Config helpers  (mirrors engram-inter-agent-prompt-hook.py exactly)
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
      3. $LOGNAME env var, agent- prefix stripped (Claude Code hook context
         populates $LOGNAME but not $USER).
      4. pwd.getpwuid(os.getuid()).pw_name, agent- prefix stripped.
      5. Empty string (hook will see no matching projects — safe).
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


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple:
    """Extract YAML-ish frontmatter. Returns (fields_dict, body_text)."""
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


# ---------------------------------------------------------------------------
# Age formatting
# ---------------------------------------------------------------------------

def _format_age(ts: Optional[datetime]) -> str:
    """Return human-readable age like '1h ago', '2d ago'."""
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


def _parse_iso(ts_str: str) -> Optional[datetime]:
    """Parse ISO-8601 UTC timestamp; return None on failure."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Project scanning
# ---------------------------------------------------------------------------

def _scan_my_batons(agent_name: str) -> list:
    """Scan BATON_PROJECTS_DIR for projects where turn == agent_name.

    Returns list of dicts: {project_id, title, status, turn_since, turn_reason}.
    Files that don't parse cleanly are silently skipped.
    Sorted by turn_since ascending (oldest baton first = most overdue first).
    """
    projects_dir = Path(BATON_PROJECTS_DIR)
    if not projects_dir.is_dir():
        return []

    my_batons = []
    for md_file in projects_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        fields, _body = _parse_frontmatter(text)
        if not fields:
            continue

        # Check status — skip closed projects
        status = fields.get("status", "").strip().lower()
        if status in CLOSED_STATUSES:
            continue

        # Check turn — must match our agent
        turn = fields.get("turn", "").strip().lower()
        if not agent_name or turn != agent_name.lower():
            continue

        project_id = fields.get("project", "").strip()
        if not project_id:
            project_id = md_file.stem

        title = fields.get("title", "").strip()
        turn_since_str = fields.get("turn_since", "").strip()
        turn_reason = fields.get("turn_reason", "").strip()
        # Strip surrounding quotes from turn_reason if present
        if turn_reason.startswith('"') and turn_reason.endswith('"'):
            turn_reason = turn_reason[1:-1]
        turn_since = _parse_iso(turn_since_str)

        my_batons.append({
            "project_id": project_id,
            "title": title,
            "status": status,
            "turn_since": turn_since,
            "turn_reason": turn_reason,
        })

    # Oldest baton first (most overdue = highest urgency)
    my_batons.sort(key=lambda b: b["turn_since"] or datetime.min.replace(tzinfo=timezone.utc))
    return my_batons


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # ── Single-agent guard ────────────────────────────────────────────────────
    # If the projects directory doesn't exist, baton is not deployed.
    # Exit silently — single-agent installs must see zero behavior change.
    projects_dir = Path(BATON_PROJECTS_DIR)
    if not projects_dir.is_dir():
        sys.exit(0)

    # ── Config + agent name ───────────────────────────────────────────────────
    config = _load_config()
    agent_name = _get_agent_name(config)
    # Even if agent_name is empty, scanning returns nothing (no turn match).

    # ── Scan for my batons ────────────────────────────────────────────────────
    my_batons = _scan_my_batons(agent_name)

    # ── Exit silently if nothing to surface ───────────────────────────────────
    if not my_batons:
        sys.exit(0)

    # ── Build injection block ─────────────────────────────────────────────────
    n = len(my_batons)
    baton_word = "baton" if n == 1 else "batons"
    lines = [
        f"\U0001f3be {n} {baton_word} in your court:"
    ]

    for b in my_batons:
        pid = b["project_id"]
        turn_since = b["turn_since"]
        turn_reason = b["turn_reason"]

        # Format: since HH:MMZ, Xh ago
        if turn_since:
            since_short = turn_since.strftime("%H:%MZ")
            age = _format_age(turn_since)
            age_part = f", {age}" if age else ""
            since_str = f"since {since_short}{age_part}"
        else:
            since_str = ""

        reason_part = f" — {turn_reason}" if turn_reason else ""
        if since_str:
            lines.append(f"  - {pid} ({since_str}){reason_part}")
        else:
            lines.append(f"  - {pid}{reason_part}")

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
