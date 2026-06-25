"""Project board read model for the forum.

Live-reads BATON_PROJECTS_DIR/*.md on every call — no stored table, no writes.
The board is a read-only index over the baton files.

Design constraints (forum thread #166):
- No stored copy: read fresh on every render, never persist to DB.
- Never write to projects/*.md — read-only.
- gh-reconcile on read: PR refs that are merged/closed render as 'done'
  regardless of file status. Degrade gracefully if gh is unavailable.
- Batch gh lookups: one call per distinct PR ref per render, not per row.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default projects directory — override via BATON_PROJECTS_DIR env var in tests.
_DEFAULT_PROJECTS_DIR = "/home/agents-shared/projects"

# Valid status values from baton protocol.
VALID_STATUSES = {"planning", "in-progress", "in-review", "merged", "cancelled"}

# Statuses that mean the item is "done" for board display purposes.
_FILE_DONE_STATUSES = {"merged", "cancelled"}

# Frontmatter parsing (mirrors baton.py pattern exactly).
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)
_PARTICIPANTS_LIST_RE = re.compile(r"^\[?(.*?)\]?$")

# GitHub anchor validation: pr/<N>
_PR_ANCHOR_RE = re.compile(r"^pr/(\d+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# ISO helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts_str: str) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp string; return None on failure."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_str(ts: Optional[datetime]) -> str:
    """Return a human-readable age string like '5m ago', '3h ago', '2d ago'."""
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
# Frontmatter parsing
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML-ish frontmatter from a baton markdown file.

    Returns (fields_dict, body_text). On parse failure returns ({}, text).
    Mirrors baton.py _parse_frontmatter exactly.
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


def _parse_participants(participants_str: str) -> list[str]:
    """Parse participants field: '[borges, ariadne]' or 'borges,ariadne'.

    Returns a list of lowercase stripped agent names.
    """
    m = _PARTICIPANTS_LIST_RE.match(participants_str.strip())
    inner = m.group(1) if m else participants_str
    return [p.strip().lower() for p in inner.split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Kind inference
# ---------------------------------------------------------------------------

def _infer_kind(project_id: str, github_ref: str) -> str:
    """Infer item kind from filename and/or github ref.

    PR-* → 'pr', ISSUE-* → 'issue', else 'project'.
    """
    pid_upper = project_id.upper()
    if pid_upper.startswith("PR-"):
        return "pr"
    if pid_upper.startswith("ISSUE-"):
        return "issue"
    # Fallback: check github ref
    if github_ref and github_ref.lower().startswith("pr/"):
        return "pr"
    return "project"


# ---------------------------------------------------------------------------
# gh PR state reconciliation
# ---------------------------------------------------------------------------

def _gh_pr_state(pr_number: str) -> str:
    """Query GitHub for a PR's state via `gh pr view`.

    Returns one of:
      'merged'  — PR was merged.
      'closed'  — PR was closed without merge.
      'open'    — PR is still open.
      'unknown' — gh unavailable or non-zero exit.

    Never raises — callers must degrade gracefully on 'unknown'.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "state"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return "unknown"
    except subprocess.TimeoutExpired:
        return "unknown"
    except OSError:
        return "unknown"

    if result.returncode != 0:
        return "unknown"

    raw = (result.stdout or "").strip()
    if not raw:
        return "unknown"

    try:
        data = json.loads(raw)
        state = (data.get("state") or "").upper()
    except (json.JSONDecodeError, ValueError, AttributeError):
        return "unknown"

    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed"
    if state == "OPEN":
        return "open"
    return "unknown"


# Module-level TTL cache for gh PR states. Without it, every board render AND
# every ~2s poll of /api/board/updates would fan out one `gh pr view` subprocess
# per distinct PR — across multiple polling loop-agents that floods both the host
# (subprocess spawns) and the GitHub API (rate limits). The cache bounds real gh
# calls to one per distinct PR per TTL window regardless of request frequency.
_GH_STATE_CACHE: dict[str, tuple[str, float]] = {}
_GH_CACHE_TTL_SECS = 90.0
# Flask serves threaded by default, so concurrent ~2s polls touch the cache
# from multiple threads. The lock guards the dict reads/writes (no torn state);
# the `gh` subprocess runs OUTSIDE the lock so I/O never serializes behind it.
_GH_CACHE_LOCK = threading.Lock()


def _clear_gh_cache() -> None:
    """Test helper: drop the gh-state TTL cache."""
    with _GH_CACHE_LOCK:
        _GH_STATE_CACHE.clear()


def _batch_gh_reconcile(pr_numbers: list[str]) -> dict[str, str]:
    """Resolve gh state for distinct PR numbers, TTL-cached across calls.

    Returns a dict mapping pr_number → state string
    ('merged', 'closed', 'open', 'unknown').

    One real `gh` call per distinct PR per TTL window — never per row, and never
    per poll: frequent renders and the ~2s-polled updates endpoint reuse cached
    states instead of re-spawning a subprocess fan-out each request. 'unknown'
    results are NOT cached, so a transient gh outage self-heals on the next call
    rather than sticking for the whole TTL window.

    Thread-safe: cache reads/writes are lock-guarded; the `gh` subprocess runs
    outside the lock so concurrent polls don't serialize on I/O. (Two threads
    that miss the same PR in the same instant may both fetch it — bounded and
    harmless: same answer, just one redundant call.)
    """
    result: dict[str, str] = {}
    now = time.monotonic()

    # Phase 1 (locked, fast): serve cache hits, collect the misses to fetch.
    to_fetch: list[str] = []
    with _GH_CACHE_LOCK:
        for pr_num in pr_numbers:
            cached = _GH_STATE_CACHE.get(pr_num)
            if cached is not None and (now - cached[1]) < _GH_CACHE_TTL_SECS:
                result[pr_num] = cached[0]
            else:
                to_fetch.append(pr_num)

    # Phase 2 (unlocked I/O): one gh call per distinct miss; store non-unknown.
    for pr_num in to_fetch:
        state = _gh_pr_state(pr_num)
        result[pr_num] = state
        if state != "unknown":
            with _GH_CACHE_LOCK:
                _GH_STATE_CACHE[pr_num] = (state, time.monotonic())

    return result


# ---------------------------------------------------------------------------
# Effective status computation
# ---------------------------------------------------------------------------

def _effective_status(
    file_status: str,
    github_ref: str,
    gh_states: dict[str, str],
) -> tuple[str, str]:
    """Compute effective_status and gh_state for a project item.

    Args:
        file_status:  The status field from the baton file.
        github_ref:   The github field value (e.g. 'pr/1005') or ''.
        gh_states:    Batch-resolved dict of pr_number → state.

    Returns:
        (effective_status, gh_state)
        effective_status is 'done' when file says done OR gh says merged/closed.
        gh_state is 'merged'/'closed'/'open'/'unknown'/'' (empty when no ref).
    """
    # File-level done detection
    file_done = file_status in _FILE_DONE_STATUSES

    # Extract PR number from github ref
    m = _PR_ANCHOR_RE.match(github_ref.strip())
    if not m:
        # No PR ref — use file status directly.
        eff = "done" if file_done else file_status
        return eff, ""

    pr_num = m.group(1)
    gh_state = gh_states.get(pr_num, "unknown")

    # gh-reconcile: merged or closed → treat as done regardless of file
    if gh_state in ("merged", "closed"):
        return "done", gh_state

    if file_done:
        return "done", gh_state

    return file_status, gh_state


# ---------------------------------------------------------------------------
# Core read model
# ---------------------------------------------------------------------------

def _projects_dir() -> Path:
    """Return the baton projects directory path."""
    env = os.environ.get("BATON_PROJECTS_DIR", "").strip()
    return Path(env) if env else Path(_DEFAULT_PROJECTS_DIR)


def read_project_board(projects_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """Parse all baton .md files and return a list of project board items.

    This is the live read model — called fresh on every render.
    Never writes to any file.

    Args:
        projects_dir: Override directory (for tests). Defaults to
            $BATON_PROJECTS_DIR or /home/agents-shared/projects.

    Returns:
        List of dicts with keys:
          project, title, kind, status, effective_status,
          turn, turn_since, turn_since_str, participants,
          github, gh_state, updated_at, age_str
        Sorted: active items by turn_since (oldest first), then done items.

    Malformed files are skipped with a logged warning; they never 500 the board.
    """
    if projects_dir is None:
        projects_dir = _projects_dir()

    if not projects_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    pr_numbers_to_resolve: list[str] = []
    _seen_pr: set[str] = set()

    # First pass: parse all files, collect PR numbers for batch gh lookup.
    raw_items: list[dict[str, Any]] = []
    for md_file in sorted(projects_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(
                f"[forum/board] warning: cannot read {md_file}: {exc}",
                file=sys.stderr,
            )
            continue

        try:
            fields, _body = _parse_frontmatter(text)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[forum/board] warning: malformed frontmatter in {md_file}: {exc}",
                file=sys.stderr,
            )
            continue

        if not fields:
            # No frontmatter — skip silently (plain .md, not a baton file).
            print(
                f"[forum/board] warning: no frontmatter in {md_file}, skipping",
                file=sys.stderr,
            )
            continue

        # Extract fields with safe defaults.
        project_id = (fields.get("project") or "").strip() or md_file.stem
        title = (fields.get("title") or "").strip() or project_id
        file_status = (fields.get("status") or "").strip().lower()
        turn = (fields.get("turn") or "").strip().lower()
        turn_since_str = (fields.get("turn_since") or "").strip()
        participants_str = (fields.get("participants") or "").strip()
        github_ref = (fields.get("github") or "").strip().lower()

        # Strip surrounding quotes from turn_reason (matches baton.py).
        turn_reason = (fields.get("turn_reason") or "").strip()
        if turn_reason.startswith('"') and turn_reason.endswith('"'):
            turn_reason = turn_reason[1:-1]

        participants = _parse_participants(participants_str) if participants_str else []
        turn_since = _parse_iso(turn_since_str)

        # Use file mtime as fallback for updated_at.
        try:
            file_mtime = datetime.fromtimestamp(md_file.stat().st_mtime, tz=timezone.utc)
        except OSError:
            file_mtime = None

        updated_at = turn_since or file_mtime
        updated_at_str = (
            updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if updated_at else ""
        )

        kind = _infer_kind(project_id, github_ref)

        # Collect PR number for batch gh resolution.
        pr_m = _PR_ANCHOR_RE.match(github_ref)
        if pr_m:
            pr_num = pr_m.group(1)
            if pr_num not in _seen_pr:
                _seen_pr.add(pr_num)
                pr_numbers_to_resolve.append(pr_num)

        raw_items.append({
            "project": project_id,
            "title": title,
            "kind": kind,
            "status": file_status,
            "turn": turn,
            "turn_since": turn_since,
            "turn_since_str": turn_since_str,
            "turn_reason": turn_reason,
            "participants": participants,
            "github": github_ref,
            "updated_at": updated_at_str,
            "updated_at_dt": updated_at,
            "_pr_num": pr_m.group(1) if pr_m else None,
        })

    # Batch gh reconciliation — one call per distinct PR, never per row.
    gh_states: dict[str, str] = {}
    if pr_numbers_to_resolve:
        try:
            gh_states = _batch_gh_reconcile(pr_numbers_to_resolve)
        except Exception as exc:  # noqa: BLE001
            # Degrade gracefully — gh unavailable → all PRs show 'unknown'.
            print(
                f"[forum/board] warning: gh reconciliation failed: {exc}",
                file=sys.stderr,
            )
            gh_states = {pr: "unknown" for pr in pr_numbers_to_resolve}

    # Second pass: apply gh reconciliation and build final items.
    for raw in raw_items:
        file_status = raw["status"]
        github_ref = raw["github"]
        pr_num = raw["_pr_num"]

        effective_status, gh_state = _effective_status(
            file_status, github_ref, gh_states
        )

        # gh_unknown flag: set when we have a PR ref but couldn't resolve state.
        gh_unknown = bool(pr_num and gh_states.get(pr_num) == "unknown")

        age = _age_str(raw["turn_since"])

        item: dict[str, Any] = {
            "project": raw["project"],
            "title": raw["title"],
            "kind": raw["kind"],
            "status": file_status,
            "effective_status": effective_status,
            "turn": raw["turn"],
            "turn_since": raw["turn_since_str"],
            "turn_reason": raw["turn_reason"],
            "participants": raw["participants"],
            "github": github_ref,
            "gh_state": gh_state,
            "gh_unknown": gh_unknown,
            "updated_at": raw["updated_at"],
            "age_str": age,
        }
        items.append(item)

    # Sort: active items (not done) by turn_since ascending (oldest first),
    # then done items by turn_since descending (most recently done first).
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    active = [i for i in items if i["effective_status"] != "done"]
    done = [i for i in items if i["effective_status"] == "done"]

    active.sort(
        key=lambda i: _parse_iso(i["turn_since"]) or epoch
    )
    done.sort(
        key=lambda i: _parse_iso(i["turn_since"]) or epoch,
        reverse=True,
    )

    return active + done


def get_board_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    """Return counts by effective_status for the board API response."""
    counts: dict[str, int] = {}
    for item in items:
        eff = item["effective_status"]
        counts[eff] = counts.get(eff, 0) + 1
    return counts


def filter_updates(
    items: list[dict[str, Any]],
    since: Optional[str],
    agent: Optional[str],
) -> list[dict[str, Any]]:
    """Filter board items to those changed after `since`, optionally for one agent.

    Args:
        items:  Full board item list from read_project_board().
        since:  ISO-8601 cursor string (exclusive: items with updated_at > since).
                None returns all items.
        agent:  If provided, only items whose turn == agent.

    Returns:
        Filtered list preserving original order.

    Since-cursor correctness: we use `turn_since` as the change-time when
    available, falling back to updated_at (which itself falls back to file
    mtime). This matches the spec: "change-time (turn_since, else file mtime)".
    """
    since_dt: Optional[datetime] = None
    if since:
        since_dt = _parse_iso(since)

    result = []
    for item in items:
        # Agent filter first (cheap).
        if agent is not None and item["turn"] != agent.lower().strip():
            continue

        # Since filter: compare updated_at (already the turn_since or mtime).
        if since_dt is not None:
            item_dt = _parse_iso(item["updated_at"])
            if item_dt is None or item_dt <= since_dt:
                continue

        result.append(item)

    return result
