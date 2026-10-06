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

Design constraints (forum thread #166, updated #1608, #2007):
- No stored copy: read fresh on every render, never persist to DB.
- Read-only: this module never writes to the coordination store.
- gh-reconcile on read: PR refs that are merged/closed render as 'done'
  regardless of file status. Degrade gracefully if gh is unavailable.
- Batch gh lookups: one call per distinct PR ref per render, not per row.
- #2007: gh lookups are skipped entirely for records whose file status is
  already file-done (merged/cancelled) — resolving gh for those can never
  change their effective_status (see _effective_status), so this is a lossless
  reduction of the gh working set, not an approximation. Such records report
  gh_state='skipped'. The remaining (active) lookups run concurrently via a
  bounded thread pool (_batch_gh_reconcile).
"""

from __future__ import annotations

import concurrent.futures
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

# GitHub anchor parsing: pr/<N> or pr/<owner>/<repo>/<N> (#1715 -- vendored
# from tools/baton.py's _parse_github_anchor; kept in sync by hand since
# this is server-side code and baton.py is the client CLI, same
# cross-boundary-vendoring precedent as compression_fidelity's own
# constants.py.
#
# #1918 / #1919: there is NO default repo for a bare pr/<N> anchor,
# deliberately -- do not revisit (same rule as tools/baton.py). A bare
# anchor is never looked up on GitHub (neither a default repo nor gh's
# ambient-cwd resolution -- #1715's root cause: PR-22 in engram-paper
# reconciled against engram-alpha's own #22); it keeps its stored status and
# is flagged gh_state='unqualified' / gh_unknown=True. Only a repo-qualified
# pr/<owner>/<repo>/<N> anchor is reconciled.
_PR_ANCHOR_RE = re.compile(r"^pr/(?:([\w.-]+)/([\w.-]+)/)?(\d+)$", re.IGNORECASE)


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

# Injection point for the GitHub identity of the board's `gh` read (#3509). The
# forum server sets it from main() to the baton gate's public `gh_call_identity`
# helper, so this read runs under the SAME explicit minimal env as the gate. A
# provider is a zero-arg callable returning ``(extra_run_kwargs, refusal)`` where
# ``refusal`` is None or a non-None "do not call gh" verdict. None (the default:
# tests, anything not configured) keeps today's behaviour exactly: gh inherits
# the process environment and no `env` kwarg is passed. Deliberately not an
# import of the gate -- board_projects must not locate it by path.
_GH_IDENTITY_PROVIDER = None


def configure_gh_identity(provider) -> None:
    """Set (or, with None, clear) the gh identity provider for `_gh_pr_state`."""
    global _GH_IDENTITY_PROVIDER
    _GH_IDENTITY_PROVIDER = provider


def _gh_pr_state(pr_number: str, repo: str) -> str:
    """Query GitHub for a PR's state via `gh pr view --repo <repo>`.

    Returns one of:
      'merged'  — PR was merged.
      'closed'  — PR was closed without merge.
      'open'    — PR is still open.
      'unknown' — gh unavailable or non-zero exit.

    Never raises — callers must degrade gracefully on 'unknown'.

    #1715: `--repo` is REQUIRED (never omitted to fall back on gh's ambient
    cwd) -- that ambient-cwd fallback is exactly what let a PR-baton for one
    repo silently reconcile against a same-numbered PR in a different repo.
    """
    gh_kw: dict = {}
    provider = _GH_IDENTITY_PROVIDER
    if provider is not None:
        gh_kw, refusal = provider()
        if refusal is not None:
            return "unknown"  # no identity: never call gh under an ambient login
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--repo", repo, "--json", "state"],
            capture_output=True,
            text=True,
            timeout=10,
            **gh_kw,
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
#
# #1715: cache key is (repo, pr_number), NOT bare pr_number -- two different
# repos' PR of the same number must never collide in this cache (a second,
# independent instance of the same bug class: even after `gh pr view` itself
# is repo-qualified, a bare-number cache key would still conflate them).
_GH_STATE_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_GH_CACHE_TTL_SECS = 90.0
# Flask serves threaded by default, so concurrent ~2s polls touch the cache
# from multiple threads. The lock guards the dict reads/writes (no torn state);
# the `gh` subprocess runs OUTSIDE the lock so I/O never serializes behind it.
_GH_CACHE_LOCK = threading.Lock()

# Bound on concurrent `gh pr view` subprocess fetches in _batch_gh_reconcile's
# Phase 2 (#2007). These are independent I/O-bound subprocess calls, safe to
# run in parallel -- bounded to avoid a subprocess-spawn storm under a
# suddenly-large active set (e.g. a bulk status change touching many PRs at
# once), not because concurrency itself is unsafe.
_GH_FETCH_MAX_WORKERS = 10


def _clear_gh_cache() -> None:
    """Test helper: drop the gh-state TTL cache."""
    with _GH_CACHE_LOCK:
        _GH_STATE_CACHE.clear()


def _batch_gh_reconcile(pr_refs: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Resolve gh state for distinct (repo, pr_number) pairs, TTL-cached.

    Returns a dict mapping (repo, pr_number) → state string
    ('merged', 'closed', 'open', 'unknown').

    One real `gh` call per distinct (repo, pr_number) per TTL window — never
    per row, and never per poll: frequent renders and the ~2s-polled updates
    endpoint reuse cached states instead of re-spawning a subprocess fan-out
    each request. 'unknown' results are NOT cached, so a transient gh outage
    self-heals on the next call rather than sticking for the whole TTL window.

    Thread-safe: cache reads/writes are lock-guarded; the `gh` subprocess runs
    outside the lock so concurrent polls don't serialize on I/O. (Two threads
    that miss the same ref in the same instant may both fetch it — bounded and
    harmless: same answer, just one redundant call.)

    #2007: Phase 2 misses are fetched concurrently via a bounded thread pool
    (_GH_FETCH_MAX_WORKERS) rather than serially — each `gh pr view` call is
    an independent I/O-bound subprocess, so running them in parallel is safe:
    `_gh_pr_state` spawns its own `subprocess.run` per call with no shared
    mutable state of its own, and the only shared state (`_GH_STATE_CACHE`) is
    already lock-guarded. Return-dict shape and the non-unknown-only caching
    behavior are unchanged — this is purely a fan-out of the same per-ref work.
    """
    result: dict[tuple[str, str], str] = {}
    now = time.monotonic()

    # Phase 1 (locked, fast): serve cache hits, collect the misses to fetch.
    to_fetch: list[tuple[str, str]] = []
    with _GH_CACHE_LOCK:
        for ref in pr_refs:
            cached = _GH_STATE_CACHE.get(ref)
            if cached is not None and (now - cached[1]) < _GH_CACHE_TTL_SECS:
                result[ref] = cached[0]
            else:
                to_fetch.append(ref)

    # Phase 2 (unlocked I/O): one gh call per distinct miss, run concurrently;
    # store non-unknown results. Bounded worker count avoids a subprocess-spawn
    # storm when the active (non-file-done) set is suddenly large.
    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_GH_FETCH_MAX_WORKERS
        ) as executor:
            future_to_ref = {
                executor.submit(_gh_pr_state, pr_num, repo): (repo, pr_num)
                for (repo, pr_num) in to_fetch
            }
            for future in concurrent.futures.as_completed(future_to_ref):
                ref = future_to_ref[future]
                state = future.result()
                result[ref] = state
                if state != "unknown":
                    with _GH_CACHE_LOCK:
                        _GH_STATE_CACHE[ref] = (state, time.monotonic())

    return result


# ---------------------------------------------------------------------------
# Effective status computation
# ---------------------------------------------------------------------------

def _effective_status(
    file_status: str,
    github_ref: str,
    gh_states: dict[tuple[str, str], str],
) -> tuple[str, str]:
    """Compute effective_status and gh_state for a project item.

    Args:
        file_status:  The status field from the baton file.
        github_ref:   The github field value (e.g. 'pr/1005' or
                      'pr/engram-agents/engram-paper/22') or ''.
        gh_states:    Batch-resolved dict of (repo, pr_number) → state. A ref
                      absent from this dict means gh reconciliation was never
                      attempted for it this call (see 'skipped' below) — this
                      is distinct from a genuine gh lookup that came back
                      'unknown'.

    Returns:
        (effective_status, gh_state)
        effective_status is 'done' when file says done OR gh says merged/closed.
        gh_state is 'merged'/'closed'/'open'/'unknown'/'skipped'/'unqualified'/''
        (empty when no ref). 'unqualified' (#1918/#1919) means the anchor is a
        bare pr/<N> with no repo: no gh lookup is ever made for it and the
        stored status is kept as-is (there is NO default repo). 'skipped' (#2007) means the record's file status was
        already file-done (_FILE_DONE_STATUSES), so gh reconciliation was
        deliberately never attempted for it — distinct from 'unknown', which
        means a lookup was attempted and failed. A ref shared with another,
        still-active record (and therefore actually resolved this call) always
        reports the real resolved state here, never 'skipped'.
    """
    # File-level done detection
    file_done = file_status in _FILE_DONE_STATUSES

    # Extract (repo, PR number) from github ref. #1918: an unqualified
    # pr/<N> anchor has NO repo (no default, no ambient cwd) -- it is never
    # looked up and keeps its stored status.
    m = _PR_ANCHOR_RE.match(github_ref.strip())
    if not m:
        # No PR ref — use file status directly.
        eff = "done" if file_done else file_status
        return eff, ""

    owner, repo_name, pr_num = m.group(1), m.group(2), m.group(3)
    if not (owner and repo_name):
        return ("done" if file_done else file_status), "unqualified"
    ref = (f"{owner}/{repo_name}", pr_num)
    if ref in gh_states:
        # Resolved this call (whether because the record itself is active, or
        # because another record sharing this ref is) — report the real state.
        gh_state = gh_states[ref]
    else:
        # Never resolved this call. #2007: a file-done record's ref is
        # deliberately never added to the resolve set (resolving it can never
        # change effective_status — see the file_done branch below) — report
        # that deliberate skip distinctly from a genuine gh miss. The
        # not-file-done+absent combination should not occur in practice
        # (every active record's ref is always added to the resolve set), but
        # 'unknown' is the safe fallback if it ever does.
        gh_state = "skipped" if file_done else "unknown"

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
    pr_refs_to_resolve: list[tuple[str, str]] = []
    _seen_pr: set[tuple[str, str]] = set()

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

        # Collect (repo, PR number) for batch gh resolution. #1918: only a
        # repo-qualified pr/<owner>/<repo>/<N> anchor is collected; a bare
        # pr/<N> has no repo (no default, never gh's ambient cwd), so it gets
        # no gh lookup at all (_pr_ref stays None).
        #
        # #2007: skip adding the ref to the resolve set when this record's
        # file status is already file-done (_FILE_DONE_STATUSES). This is not
        # a heuristic — _effective_status's file_done branch returns 'done'
        # unconditionally regardless of what gh_state comes back as, so
        # resolving gh for an already-file-done record can never change its
        # effective_status. pr_ref itself is still captured below (for
        # _effective_status's lookup and for the 'skipped' discriminator);
        # only the *resolve-set membership* is conditional. A ref shared with
        # another, still-active record still gets resolved via that record's
        # own (non-skipped) addition — _seen_pr dedupes the resolve set, not
        # per-item pr_ref capture, so ordering between a done and an active
        # record sharing a ref does not matter.
        pr_m = _PR_ANCHOR_RE.match(github_ref)
        pr_ref = None
        if pr_m and pr_m.group(1) and pr_m.group(2):
            pr_ref = (f"{pr_m.group(1)}/{pr_m.group(2)}", pr_m.group(3))
            if file_status not in _FILE_DONE_STATUSES and pr_ref not in _seen_pr:
                _seen_pr.add(pr_ref)
                pr_refs_to_resolve.append(pr_ref)

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
            "_pr_ref": pr_ref,
        })

    # Batch gh reconciliation — one call per distinct (repo, PR), never per row.
    gh_states: dict[tuple[str, str], str] = {}
    if pr_refs_to_resolve:
        try:
            gh_states = _batch_gh_reconcile(pr_refs_to_resolve)
        except Exception as exc:  # noqa: BLE001
            # Degrade gracefully — gh unavailable → all PRs show 'unknown'.
            print(
                f"[forum/board] warning: gh reconciliation failed: {exc}",
                file=sys.stderr,
            )
            gh_states = {ref: "unknown" for ref in pr_refs_to_resolve}

    # Second pass: apply gh reconciliation and build final items.
    for raw in raw_items:
        file_status = raw["status"]
        github_ref = raw["github"]
        pr_ref = raw["_pr_ref"]

        effective_status, gh_state = _effective_status(
            file_status, github_ref, gh_states
        )

        # gh_unknown flag: set when we have a PR ref but couldn't resolve state.
        # #1918: a bare (unqualified) anchor is likewise unresolvable -- flag
        # it, except on an already-file-done record (nothing to warn about).
        gh_unknown = bool(pr_ref and gh_states.get(pr_ref) == "unknown") or (
            gh_state == "unqualified" and file_status not in _FILE_DONE_STATUSES
        )

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
