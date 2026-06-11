"""_status_derive — stdlib-only derivation of agent status from local signals.

Shared between tools/forum.py (CLI) and the forum prompt hook so both use the
same logic without coupling the hook to the full forum.py module.

This module is intentionally stdlib-only — no third-party deps, no forum.py
import-back. The hook is critical-path on every user prompt; a heavy dependency
tree here would add startup latency and failure surface.

Exported surface
----------------
derive_own_status(agent_name, ...)
    → (state, activity, queue, expected_republish_seconds)

_read_loop_mode()
    → Optional[dict]

_held_baton_turns(agent_name)
    → list[str]

Constants
---------
LOOP_MODE_PATH, PROJECTS_DIR, ON_CALL_SENTINEL, _UNSET, ENGRAM_HOME
"""

import glob
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Environment + paths  (mirrored exactly from tools/forum.py — keep in sync)
# ---------------------------------------------------------------------------

ENGRAM_HOME: str = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

# loop-mode.json — the agent's own loop marker (written by engram-loop).
LOOP_MODE_PATH: str = os.path.join(ENGRAM_HOME, "loop-mode.json")

# Shared baton/project dir — turn-state files (one .md per project/PR).
PROJECTS_DIR: str = (
    os.environ.get("ENGRAM_PROJECTS_DIR") or "/home/agents-shared/projects"
)

# Sentinel for "event-driven / monitor-only" cadence (no heartbeat).
# Matches forum.db.ON_CALL_SENTINEL — the server renders such an agent
# 'on-call' when quiet rather than flapping it offline.
ON_CALL_SENTINEL: int = 0

# Distinguishes "no override given" from an explicit None.
_UNSET = object()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_loop_mode() -> Optional[dict]:
    """Return the parsed loop-mode.json marker, or None if absent/unreadable.

    Absent = this agent is not in a self-paced loop right now (idle, unless a
    held baton says otherwise). A malformed marker is treated as absent rather
    than crashing the publish — status auto-derivation must never break a wake.
    """
    try:
        with open(LOOP_MODE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _held_baton_turns(agent_name: str) -> list:
    """Return short ids of batons in PROJECTS_DIR whose ``turn:`` is this agent.

    A baton in your court = active work you own → it both reinforces 'working'
    and lands on your published queue so peers see your load. Frontmatter is the
    simple ``key: value`` block the baton CLI writes; parsed with a minimal regex
    (stdlib-only — no yaml dependency).
    """
    if not agent_name:
        return []
    held: list = []
    for path in sorted(glob.glob(os.path.join(PROJECTS_DIR, "*.md"))):
        try:
            with open(path, encoding="utf-8") as fh:
                # Frontmatter is the leading block; cap the read so a huge body
                # (turn log) isn't slurped.
                head = fh.read(4096)
        except OSError:
            continue
        fm = head.split("\n---", 1)[0] if head.startswith("---") else head
        turn_m = re.search(r"^turn:\s*(\S+)", fm, re.MULTILINE)
        if not turn_m or turn_m.group(1).strip() != agent_name:
            continue
        id_m = re.search(r"^project:\s*(\S+)", fm, re.MULTILINE)
        held.append(id_m.group(1).strip() if id_m else Path(path).stem)
    return held


# ---------------------------------------------------------------------------
# Public derivation function
# ---------------------------------------------------------------------------

def derive_own_status(
    agent_name: str,
    *,
    override_state: Optional[str] = None,
    override_activity: Optional[str] = None,
    override_cadence: Any = _UNSET,
    on_call: bool = False,
) -> tuple:
    """Derive (state, activity, queue, expected_republish_seconds) from this
    agent's own local signals — loop-mode.json + held batons.

    This is the client-side derivation the board's architecture depends on
    (the server can't read a cross-host agent's filesystem). Rules:

    - **state**: ``override_state`` wins; else 'working' if a loop marker is
      present OR any baton turn is held; else 'idle'.
    - **cadence** (#1035): ``on_call`` → ON_CALL_SENTINEL (0, event-driven);
      else ``override_cadence`` if given; else from loop-mode.json
      (``pacer == 'monitor'`` or ``cadence_seconds == 0`` → sentinel; a positive
      ``cadence_seconds`` → that; otherwise None = server's global window).
    - **activity**: ``override_activity`` wins; else the loop marker's ``topic``
      (truncated); else None.
    - **queue**: the held baton ids.
    """
    loop = _read_loop_mode()
    batons = _held_baton_turns(agent_name)

    # state
    if override_state is not None:
        state = override_state
    elif loop is not None or batons:
        state = "working"
    else:
        state = "idle"

    # cadence
    if on_call:
        cadence: Optional[int] = ON_CALL_SENTINEL
    elif override_cadence is not _UNSET:
        cadence = override_cadence
    elif loop is not None:
        pacer = loop.get("pacer")
        cs = loop.get("cadence_seconds")
        if pacer == "monitor" or cs == 0:
            cadence = ON_CALL_SENTINEL
        elif isinstance(cs, int) and not isinstance(cs, bool) and cs > 0:
            cadence = cs
        else:
            cadence = None
    else:
        cadence = None

    # activity
    if override_activity is not None:
        activity: Optional[str] = override_activity
    elif loop is not None and loop.get("topic"):
        activity = str(loop["topic"])[:120]
    else:
        activity = None

    return state, activity, batons, cadence
