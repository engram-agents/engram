"""Project board read model for the forum.

Reads the live UCS coordination store (``CoordinationStore.read_projects()``)
on every call — no stored table, no writes. The board is a read-only index
over the same store ``GET /api/projects`` already serves.

**#1608**: repoints the board off the pre-cutover ``BATON_PROJECTS_DIR/*.md``
glob. That glob went dead at the 2026-06-27 UCS cutover, when ``baton.py``
moved to writing exclusively through the forum coordination API — the 502
local files it used to read froze at that instant and never got another
write. The live source is the same store every other coordination surface
(``/api/projects``, ``/api/updates``) already reads.

Design constraints (forum thread #166, updated #1608):
- No stored copy: read fresh on every render, never persist to DB.
- Read-only: this module never writes to the coordination store.
- gh-reconcile on read: PR refs that are merged/closed render as 'done'
  regardless of file status. Degrade gracefully if gh is unavailable.
- Batch gh lookups: one call per distinct PR ref per render, not per row.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid status values from baton protocol.
VALID_STATUSES = {"planning", "in-progress", "in-review", "merged", "cancelled"}

# Statuses that mean the item is "done" for board display purposes.
_FILE_DONE_STATUSES = {"merged", "cancelled"}

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

def read_project_board(store) -> list[dict[str, Any]]:
    """Read every project record from the live coordination store and return
    a list of project board items.

    This is the live read model — called fresh on every render. Never writes
    to the store.

    Args:
        store: a ``CoordinationStore`` (e.g. ``forum.coordination.FileStore``)
            — the same live backend ``GET /api/projects`` reads. ``None``
            (coordination store not configured) returns ``[]``.

    Returns:
        List of dicts with keys:
          project, title, kind, status, effective_status,
          turn, turn_since, turn_reason, participants,
          github, gh_state, gh_unknown, updated_at, seq, age_str
        Sorted: active items by turn_since (oldest first), then done items
        by turn_since (most-recently-done first).

    A record whose raw content has no parseable frontmatter (the store's
    ``read_projects()`` degrades those to an all-empty ``ProjectRecord``
    rather than raising) is skipped here — same "malformed → skipped, never
    500s the board" contract the old glob-reader had for a file with no
    frontmatter.
    """
    if store is None:
        return []

    records = store.read_projects(active_only=False)

    items: list[dict[str, Any]] = []
    pr_numbers_to_resolve: list[str] = []
    _seen_pr: set[str] = set()

    # First pass: normalize record fields, collect PR numbers for batch gh lookup.
    raw_items: list[dict[str, Any]] = []
    for record in records:
        # A record with no parseable frontmatter block in its raw content is the
        # store-backed equivalent of the old "no frontmatter → skip" glob case —
        # store.read_projects() never raises on it, it just returns empty fields,
        # so detect it here from the raw text rather than a fragile all-fields-empty
        # heuristic.
        if not record.raw.lstrip().startswith("---"):
            print(
                f"[forum/board] warning: no frontmatter in project {record.project_id!r}, skipping",
                file=sys.stderr,
            )
            continue

        project_id = record.project_id
        title = (record.title or "").strip() or project_id
        file_status = (record.status or "").strip().lower()
        turn = (record.turn or "").strip().lower()
        turn_since_str = (record.turn_since or "").strip()
        turn_reason = (record.turn_reason or "").strip()
        participants = list(record.participants)
        github_ref = (record.github or "").strip().lower()

        turn_since = _parse_iso(turn_since_str)

        # Display time: prefer turn_since ("whose turn, since when"). Unlike the
        # pre-#1608 glob reader there is no filesystem mtime to fall back on for a
        # store record, so an unstamped turn_since simply has no display time.
        updated_at = turn_since
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
            # The coordination store's module-assigned seq — see filter_updates()
            # for why this (not a timestamp) is the /updates cursor key post-#1608.
            "seq": record.seq,
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
            "seq": raw["seq"],
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
    since: Optional[int],
    agent: Optional[str],
) -> list[dict[str, Any]]:
    """Filter board items to those changed after `since`, optionally for one agent.

    Args:
        items:  Full board item list from read_project_board().
        since:  EXCLUSIVE seq cursor (items with seq > since). None returns all
                items (no since-filtering).
        agent:  If provided, only items whose turn == agent.

    Returns:
        Filtered list preserving original order.

    Since-cursor correctness (#1608): the cursor keys on each item's `seq` — the
    coordination store's module-assigned, monotonically increasing sequence
    number, allocated under the `SeqAllocator` lock co-atomically with the write
    that committed it (fork-4; see `forum.coordination.seq`). This mirrors the
    unified `/api/updates` feed's cursor (`forum.coordination.updates.build_updates`)
    and is a STRONGER server-observed-order guarantee than the pre-#1608 mechanism
    (the baton file's filesystem mtime): seq is assigned atomically at the instant
    a mutation commits, with no possible race window, so a since-filter keyed on it
    can never reproduce the #1445 silent-miss class (a court-change visible before
    its cursor advances past it).
    """
    result = []
    for item in items:
        # Agent filter first (cheap).
        if agent is not None and item["turn"] != agent.lower().strip():
            continue

        # Since filter keyed on the seq cursor (#1608).
        if since is not None and item["seq"] <= since:
            continue

        result.append(item)

    return result
