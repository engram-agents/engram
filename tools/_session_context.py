"""_session_context — shared session-context-file CRUD for hot-seat dispatch.

Per Mneme's design (2026-04-29 PM, msg `2026-04-29T19-35-00Z_mneme.md`):
session-context files at `inter-agent/session_<id>.json` track active design
threads with TTL-based hot-seat eligibility. Schema:

    {
      "session_id": "string (e.g., cj_NNNN or hot-seat-design)",
      "status": "active" | "paused" | "archived",
      "participants": ["mneme", "borges", "maintainer"],
      "last_activity_at": "ISO8601",
      "expiry_ttl_seconds": 300,
      "hot_seat_enabled": true,
      "metadata": {"goal_id": "gl_XXXX", "description": "..."}
    }

Hot-seat dispatch fires IFF:
- session file exists (looked up from message frontmatter `session_id` field)
- status == "active"
- hot_seat_enabled == true
- now < last_activity_at + expiry_ttl_seconds

Otherwise: dispatcher skips (self-paced loop's ScheduleWakeup covers as fallback cadence).

Shared between Borges + Mneme channel_dispatchers via this module — single
source of truth for the session-state semantics.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Import _channel for atomic_write
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _channel import atomic_write  # noqa: E402


DEFAULT_TTL_SECONDS = 300  # Aligns with Anthropic prompt-cache 5-min TTL
VALID_STATUSES = ("active", "paused", "archived")


def session_path(channel_dir: Path, session_id: str) -> Path:
    """Path to a session-context file."""
    return channel_dir / f"session_{session_id}.json"


def load_session(channel_dir: Path, session_id: str) -> Optional[dict]:
    """Load a session-context file. Returns None if missing or unparseable."""
    p = session_path(channel_dir, session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_session(channel_dir: Path, session: dict) -> Path:
    """Atomic write a session-context dict to its file. Returns path."""
    p = session_path(channel_dir, session["session_id"])
    atomic_write(p, json.dumps(session, indent=2), mode=0o644)
    return p


def create_session(channel_dir: Path, session_id: str,
                   participants: list,
                   description: str = "",
                   goal_id: str = "",
                   linked_conjectures: list = None,
                   linked_questions: list = None,
                   ttl_seconds: int = DEFAULT_TTL_SECONDS,
                   hot_seat_enabled: bool = True) -> dict:
    """Create a new active session-context file. Returns the dict."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session = {
        "session_id": session_id,
        "status": "active",
        "participants": list(participants),
        "last_activity_at": now_iso,
        "expiry_ttl_seconds": ttl_seconds,
        "hot_seat_enabled": hot_seat_enabled,
        "metadata": {
            "goal_id": goal_id,
            "description": description,
            "linked_conjectures": linked_conjectures or [],
            "linked_questions": linked_questions or [],
        },
    }
    save_session(channel_dir, session)
    return session


def is_hot_seat_eligible(session: dict, now: Optional[datetime] = None) -> bool:
    """True if a session is currently in hot-seat mode (status active, within TTL).

    Returns False for:
    - paused or archived status
    - hot_seat_enabled == false
    - last_activity_at + ttl is in the past (cooldown elapsed)
    - malformed session dict
    """
    if not session:
        return False
    if session.get("status") != "active":
        return False
    if not session.get("hot_seat_enabled", False):
        return False
    last_activity_str = session.get("last_activity_at", "")
    ttl = session.get("expiry_ttl_seconds", DEFAULT_TTL_SECONDS)
    if not last_activity_str:
        return False
    try:
        # Try ISO8601 parse — handle both "Z" suffix and no-suffix forms
        if last_activity_str.endswith("Z"):
            last_activity = datetime.fromisoformat(last_activity_str[:-1]).replace(tzinfo=timezone.utc)
        else:
            last_activity = datetime.fromisoformat(last_activity_str)
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    now = now or datetime.now(timezone.utc)
    expiry = last_activity + timedelta(seconds=ttl)
    return now < expiry


def touch_activity(channel_dir: Path, session_id: str) -> Optional[dict]:
    """Update last_activity_at to now on a session. Returns updated dict or None.

    Atomic via _channel.atomic_write to avoid partial-read races between
    Borges + Mneme dispatchers updating concurrently.
    """
    session = load_session(channel_dir, session_id)
    if not session:
        return None
    session["last_activity_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_session(channel_dir, session)
    return session


def set_status(channel_dir: Path, session_id: str, status: str) -> Optional[dict]:
    """Change session status (active/paused/archived). Returns updated dict or None."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {status!r}; must be one of {VALID_STATUSES}")
    session = load_session(channel_dir, session_id)
    if not session:
        return None
    session["status"] = status
    save_session(channel_dir, session)
    return session
