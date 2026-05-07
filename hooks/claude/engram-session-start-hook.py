#!/usr/bin/env python3
"""Session start: record active-session marker, reset context-tracker baseline,
clear sticky user identity, and inject session info into the conversation.

Reading-reminder injection is source-conditional:
  - source=="startup" (fresh terminal): warm-briefing + newest history file +
    Piece C git log (last 2 days of commits from both repos).
  - source=="compact"/"resume" (or anything else): warm-briefing only. The
    history file is skipped because the compaction summary carries
    work-thread continuity, and re-reading today's active-day log mid-day
    introduces "yesterday/today" framing confusion (ob_1264, 2026-04-24).

The history/git-log bundle is Piece C of the three-piece session-handover
fix (dv_0478): on startup the agent cross-checks pending-status claims
("MECH-N deferred", "still uncommitted") against the git log before
reporting to the user.

The active-session marker (~/.engram/active-session.json) is the canonical
pointer to the current JSONL. Fixes the turn-1 drowsiness bug where the
tracker picked newest-by-mtime globally and grabbed yesterday's file before
the new conversation's JSONL had flushed. Also gives observation tooling a
reliable transcript_path for citing the current conversation.
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from context_tracker import write_baseline

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", os.path.expanduser("~/.engram"))
SESSION_ROLE = os.environ.get("ENGRAM_SESSION_ROLE", "interactive").strip().lower()
SESSION_PURPOSE = os.environ.get("ENGRAM_SESSION_PURPOSE", "").strip()
SESSIONS_DIR = os.path.join(ENGRAM_HOME, "sessions")
ROLE_MARKER_PATH = os.path.join(SESSIONS_DIR, f"{SESSION_ROLE}.json")
ACTIVE_SESSION_PATH = os.path.join(ENGRAM_HOME, "active-session.json")
CURRENT_USER_PATH = os.path.join(ENGRAM_HOME, "current_user.json")
WARM_BRIEFING_PATH = os.path.join(ENGRAM_HOME, "warm-briefing.md")
HISTORY_DIR = os.path.join(ENGRAM_HOME, "history")

# Role → default purpose if ENGRAM_SESSION_PURPOSE not set by spawner.
DEFAULT_PURPOSES = {
    "interactive": "Interactive session with user",
    "heartbeat": "Coordination heartbeat (cron-fired, periodic)",
    "telegram": "Telegram dispatch (user messages via Telegram bridge)",
    "sleep": "Coordinated daily sleep / consolidation",
}
# Repos scanned for Piece C (recent commit cross-check at startup).
# ENGRAM_HOME is always included. Extra repos via ENGRAM_PIECE_C_REPOS env var
# (colon-separated). Default extra: ~/kg_protocol_for_ai if it exists (spt's
# dev repo); for agents installed via alpha, the default silently no-ops.
_PIECE_C_EXTRA = os.environ.get("ENGRAM_PIECE_C_REPOS", "").strip()
if _PIECE_C_EXTRA:
    PIECE_C_REPOS = [os.path.expanduser(p) for p in _PIECE_C_EXTRA.split(":") if p] + [ENGRAM_HOME]
else:
    _default_extra = os.path.expanduser("~/kg_protocol_for_ai")
    PIECE_C_REPOS = ([_default_extra] if os.path.isdir(_default_extra) else []) + [ENGRAM_HOME]
PIECE_C_WINDOW = "2 days ago"
PIECE_C_MAX_LINES = 20
PIECE_C_MAX_LINE_CHARS = 140

# Sleep-debt detection: surface a banner on fresh-terminal start (interactive
# role) if the last coordinated sleep was longer ago than this threshold.
# 24h + 4h grace covers normal cron-late, morning routine, etc.
LAST_SLEEP_MARKER_PATH = os.path.join(SESSIONS_DIR, "last-sleep-success.json")
ENGRAM_DB_PATH = os.path.join(ENGRAM_HOME, "knowledge.db")
SLEEP_DEBT_HOURS_THRESHOLD = 28
# Node-count above which a missing marker is treated as a real anomaly
# (mechanism-just-shipped, or marker accidentally deleted) rather than a
# genuine first-ever boot. A fresh install has only a handful of seed
# nodes; any established graph is well above 100.
SLEEP_BASELINE_NODE_THRESHOLD = 100


def write_marker(hook_input: dict) -> dict | None:
    session_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    if not session_id or not transcript_path:
        return None
    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "source": hook_input.get("source"),
        "cwd": hook_input.get("cwd"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "role": SESSION_ROLE,
        "purpose": SESSION_PURPOSE or DEFAULT_PURPOSES.get(SESSION_ROLE, SESSION_ROLE),
    }
    context_mode_env = os.environ.get("ENGRAM_CONTEXT_MODE", "").strip().lower()
    if context_mode_env in ("200k", "1m"):
        marker["context_mode"] = context_mode_env
    try:
        # Role-specific marker (canonical for multi-role isolation).
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        with open(ROLE_MARKER_PATH, "w") as f:
            json.dump(marker, f)
        # Back-compat: interactive role ALSO writes the legacy active-session.json
        # so context_tracker.py and observation citation continue working
        # against the human-facing session without a code change.
        if SESSION_ROLE == "interactive":
            os.makedirs(os.path.dirname(ACTIVE_SESSION_PATH), exist_ok=True)
            with open(ACTIVE_SESSION_PATH, "w") as f:
                json.dump(marker, f)
    except OSError:
        return None
    return marker


def piece_c_git_log() -> str:
    """Collect recent commits from both canonical repos for startup cross-check.

    Returns a multi-line string ready to append to SessionStart additionalContext.
    Silent empty-string on any failure — hook must not break session start.
    """
    blocks = []
    for repo in PIECE_C_REPOS:
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        try:
            out = subprocess.run(
                ["git", "-C", repo, "log", f"--since={PIECE_C_WINDOW}", "--oneline"],
                capture_output=True, text=True, timeout=3,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if out.returncode != 0:
            continue
        raw = [l for l in out.stdout.splitlines() if l.strip()]
        if not raw:
            continue
        lines = []
        for l in raw[:PIECE_C_MAX_LINES]:
            if len(l) > PIECE_C_MAX_LINE_CHARS:
                l = l[:PIECE_C_MAX_LINE_CHARS - 1] + "…"
            lines.append(l)
        if len(raw) > PIECE_C_MAX_LINES:
            lines.append(f"... ({len(raw) - PIECE_C_MAX_LINES} older commits omitted)")
        label = os.path.basename(repo) or repo
        blocks.append(f"  {label}:\n" + "\n".join(f"    {l}" for l in lines))
    if not blocks:
        return ""
    header = (
        "[Piece C — recent commits (last 2 days). Cross-check pending-status "
        f"claims in the newest {HISTORY_DIR}/ file against these commits "
        "before reporting to the user; don't propagate stale status.]"
    )
    return header + "\n" + "\n".join(blocks)


def sleep_status_block() -> str:
    """Surface a sleep-status banner if last coordinated sleep is stale,
    or if the baseline marker is missing on an established graph.

    Only fires for interactive role (other roles can't decide to catch up).
    The decision to run /engram-sleep stays with the user — this just
    surfaces the data so the user can choose case-by-case.

    Three paths:
      - Marker fresh (≤ threshold): silent.
      - Marker stale (> threshold): banner with hours-since and nodes-added.
      - Marker missing on established DB (> SLEEP_BASELINE_NODE_THRESHOLD):
        banner asking the user to run /engram-sleep to establish a baseline.
        Genuine first-ever boot (DB below threshold) stays silent.
    """
    if SESSION_ROLE != "interactive":
        return ""

    marker: dict | None = None
    completed_at = None
    completed_at_str = None
    if os.path.exists(LAST_SLEEP_MARKER_PATH):
        try:
            with open(LAST_SLEEP_MARKER_PATH) as f:
                marker = json.load(f)
            completed_at_str = marker.get("completed_at")
            if completed_at_str:
                completed_at = datetime.fromisoformat(
                    completed_at_str.replace("Z", "+00:00")
                )
            else:
                marker = None
        except (OSError, ValueError, json.JSONDecodeError):
            marker = None

    if marker is None:
        total_nodes: int | None = None
        try:
            conn = sqlite3.connect(ENGRAM_DB_PATH, timeout=2.0)
            cur = conn.execute("SELECT COUNT(*) FROM nodes")
            total_nodes = cur.fetchone()[0]
            conn.close()
        except sqlite3.Error:
            return ""
        if total_nodes is None or total_nodes < SLEEP_BASELINE_NODE_THRESHOLD:
            return ""
        return (
            f"[SLEEP STATUS: no baseline sleep marker at "
            f"{LAST_SLEEP_MARKER_PATH}. DB has {total_nodes} nodes — not a "
            "fresh install, so either the sleep-debt mechanism just "
            "shipped (no successful coordinated sleep has run yet) or the "
            "marker was deleted. Run /engram-sleep to establish a "
            "baseline; subsequent skipped-sleep nights will banner "
            "normally once a marker exists.]"
        )

    hours_since = (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600
    if hours_since <= SLEEP_DEBT_HOURS_THRESHOLD:
        return ""
    nodes_since: int | None = None
    try:
        conn = sqlite3.connect(ENGRAM_DB_PATH, timeout=2.0)
        cur = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE created_at > ?",
            (completed_at_str,),
        )
        nodes_since = cur.fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass
    nodes_part = (
        f"{nodes_since} nodes added since"
        if nodes_since is not None
        else "node count unavailable"
    )
    turn = marker.get("turn_advanced_to", "?")
    return (
        f"[SLEEP STATUS: last coordinated sleep {hours_since:.1f}h ago "
        f"({completed_at_str}, turn {turn}). {nodes_part}. "
        "The midnight cron likely did not fire (host was off). "
        "Decide case-by-case whether to run /engram-sleep now to consolidate "
        "the gap, or skip if the pending cohort is non-essential.]"
    )


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}

    marker = write_marker(hook_input)
    write_baseline(use_compact_boundary=True)

    if os.path.exists(CURRENT_USER_PATH):
        try:
            os.remove(CURRENT_USER_PATH)
        except OSError:
            pass

    source = (hook_input.get("source") or "").lower()

    lines = []
    if marker:
        lines.append(
            f"[Active session: {marker['session_id']} "
            f"(source={marker.get('source') or 'unknown'}) — "
            f"transcript at {marker['transcript_path']}]"
        )
    if source == "startup":
        lines.append(
            "[Session start reading — before responding to the first task, read "
            f"{WARM_BRIEFING_PATH} (relational context) and the newest file "
            f"in {HISTORY_DIR}/ (prior active day's milestones + current focus; "
            "may span gaps like weekends — filename is the active date).]"
        )
        git_log = piece_c_git_log()
        if git_log:
            lines.append(git_log)
        sleep_block = sleep_status_block()
        if sleep_block:
            lines.append(sleep_block)
    else:
        lines.append(
            "[Session start reading — before responding to the first task, read "
            f"{WARM_BRIEFING_PATH} (relational context). History file is "
            "skipped post-compact; the compaction summary carries work-thread "
            "continuity and git log is already in the summary's trajectory.]"
        )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }))


if __name__ == "__main__":
    main()
