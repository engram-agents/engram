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
    Single-agent users see zero behavior change.
  - No LLM calls, no DB — file-system reads and optional gh calls only.
    Per-gh-call timeout 4s; worst case one gh call per in-court baton on a
    full cache miss (N_batons calls). The 30s-TTL status cache makes the
    steady-state cost ~0 — most prompts hit cache and call gh zero times.
  - Missing/malformed project files are silently skipped (hook must
    not crash on corrupted state).
  - gh absent / auth failure / timeout: degrades gracefully to today's
    behavior (turn_reason only). The live-status block is wrapped in
    try/except so it can NEVER crash the hook.

Part of the baton turn-state system (PR baton). Mirrors the structure
of engram-inter-agent-prompt-hook.py.

Protocol doc: docs/baton-protocol.md
CLI:          tools/baton.py
"""
import os as _os, sys as _sys
# Guard against source: directory marketplace double-fire (#1066).
_plugin_root = _os.environ.get("CLAUDE_PLUGIN_ROOT", "")
_engram_home = _os.environ.get("ENGRAM_HOME") or _os.path.expanduser("~/.engram")
if _plugin_root.startswith(_os.path.join(_engram_home, "marketplace") + _os.sep):
    _sys.exit(0)  # empty stdout is valid no-op per #824/#832 contract

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
BATON_PROJECTS_DIR = os.environ.get("BATON_PROJECTS_DIR", "/home/agents-shared/projects")

# Statuses that mean "closed" — these projects don't surface
CLOSED_STATUSES = {"merged", "cancelled"}

# Cache TTL in seconds — entries younger than this are reused without a gh call
_CACHE_TTL = 30

# Per-gh-call timeout in seconds — quick-fail so the hook stays fast
_GH_TIMEOUT = 4

# GitHub Project owner (used for project-anchor queries)
_GH_OWNER = os.environ.get("BATON_GH_OWNER", "engram-agents")

# Frontmatter parsing (same regex as ia.py / inter-agent hook)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)

# PR-ID fallback pattern: "PR-NNN"
_PR_ID_RE = re.compile(r"^PR-(\d+)$")

# Fallback path for the baton CLI when shutil.which("baton") returns None.
# Defined at module level so tests can patch it.
_BATON_FALLBACK = "/home/agents-shared/bin/baton"


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
# GitHub anchor resolution
# ---------------------------------------------------------------------------

def _resolve_anchor(project_id: str, github_field: str) -> Optional[str]:
    """Resolve the GitHub anchor for a baton.

    Resolution order (per spec):
      1. Explicit ``github:`` frontmatter field — value ``pr/<N>`` or
         ``project/<N>``.
      2. Fallback: if project_id matches ``^PR-(\\d+)$`` → ``pr/<N>``.
      3. No anchor → return None.

    Returns a canonical anchor string like ``"pr/455"`` or
    ``"project/4"``, or None.
    """
    if github_field:
        val = github_field.strip().lower()
        if val.startswith("pr/") or val.startswith("project/"):
            return val
    # Fallback: PR-NNN project_id
    m = _PR_ID_RE.match(project_id)
    if m:
        return f"pr/{m.group(1)}"
    return None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    """Return the path to the baton status cache file."""
    return Path(ENGRAM_HOME) / "baton-status-cache.json"


def _load_cache() -> dict:
    """Load the cache from disk. Returns {} on any failure."""
    path = _cache_path()
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_cache(cache: dict) -> None:
    """Write cache to disk. Silently ignores write failures."""
    path = _cache_path()
    try:
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def _cache_get(cache: dict, anchor: str) -> Optional[str]:
    """Return a cached render string if the entry is fresh (<TTL), else None."""
    entry = cache.get(anchor)
    if not entry:
        return None
    fetched_at = entry.get("fetched_at", 0)
    if (time.time() - fetched_at) < _CACHE_TTL:
        return entry.get("render", "")
    return None


def _cache_set(cache: dict, anchor: str, render: str) -> None:
    """Store a render string in the cache (mutates cache in place)."""
    cache[anchor] = {"fetched_at": time.time(), "render": render}


# ---------------------------------------------------------------------------
# Live GitHub status fetch
# ---------------------------------------------------------------------------

def _gh_available() -> bool:
    """Return True if the ``gh`` CLI is on PATH."""
    return shutil.which("gh") is not None


def _fetch_pr_status(pr_num: str) -> Optional[str]:
    """Call ``gh pr view`` and return a short render string, or None on error.

    Returns strings like ``"MERGED"``, ``"OPEN · review:APPROVED"``,
    ``"OPEN · review:REVIEW_REQUIRED"``, etc.
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view", pr_num,
                "--json", "state,reviewDecision,title",
                "--jq", "[.state, .reviewDecision] | join(\"|\")",
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip()
        parts = raw.split("|")
        state = parts[0] if parts else ""
        review = parts[1] if len(parts) > 1 else ""
        if not state:
            return None
        if state == "MERGED":
            return "MERGED"
        if state == "CLOSED":
            return "CLOSED"
        # OPEN
        if review:
            return f"OPEN · review:{review}"
        return "OPEN"
    except Exception:
        # Degrade-to-None helper: any gh/parse failure → omit the clause.
        # The outer _get_live_status also guards, but catching here keeps the
        # render path clean. (Not the #307 swallow anti-pattern — returning
        # None on any fetch problem IS this helper's contract.)
        return None


def _fetch_project_status(project_num: str) -> Optional[str]:
    """Call ``gh project item-list`` and return a tally render string, or None.

    Returns strings like ``"2 Done · 1 In Progress · 2 Blocked"``.
    """
    try:
        result = subprocess.run(
            [
                "gh", "project", "item-list", project_num,
                "--owner", _GH_OWNER,
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        # gh project item-list --format json returns {"items": [...]}
        # Each item has a "status" field (or similar). Tally by status.
        items = data.get("items", [])
        if not items:
            return None
        counts: dict = {}
        for item in items:
            # Status field may be under "status", "fieldValues", etc.
            # The simplest shape: item["status"] is a string.
            status = item.get("status", "")
            if not status:
                # Try nested fieldValues if present
                for fv in item.get("fieldValues", []):
                    if isinstance(fv, dict) and fv.get("field", {}).get("name", "").lower() == "status":
                        status = fv.get("name", "") or fv.get("text", "") or ""
                        break
            if status:
                counts[status] = counts.get(status, 0) + 1
        if not counts:
            return None
        parts = [f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])]
        return " · ".join(parts)
    except Exception:
        # Degrade-to-None: covers gh failure/timeout, malformed JSON, AND an
        # unexpected JSON shape (e.g. a top-level list → AttributeError on
        # .get). Same degrade-to-None contract as _fetch_pr_status.
        return None


def _get_live_status(anchor: str, cache: dict) -> str:
    """Return a live status string for the given anchor.

    Uses cache if fresh; otherwise calls gh. On any gh failure/timeout,
    falls back to the cached render (even if stale) or returns "".

    This function NEVER raises — all exceptions are swallowed.
    """
    try:
        # Check cache first
        cached = _cache_get(cache, anchor)
        if cached is not None:
            return cached

        # Need a fresh fetch — wrap in its own try so exceptions still
        # reach the stale-cache fallback below.
        render: Optional[str] = None
        try:
            if anchor.startswith("pr/"):
                pr_num = anchor[3:]
                render = _fetch_pr_status(pr_num)
            elif anchor.startswith("project/"):
                project_num = anchor[8:]
                render = _fetch_project_status(project_num)
        except Exception:
            render = None  # fall through to stale-cache check

        if render is not None:
            _cache_set(cache, anchor, render)
            return render

        # gh failed — fall back to stale cache if any
        stale = cache.get(anchor, {}).get("render", "")
        return stale if stale else ""
    except Exception:  # belt-and-suspenders: swallow everything
        return ""


# ---------------------------------------------------------------------------
# Project scanning
# ---------------------------------------------------------------------------

def _scan_my_batons(agent_name: str) -> list:
    """Scan BATON_PROJECTS_DIR for projects where turn == agent_name.

    Returns list of dicts:
        {project_id, title, status, turn_since, turn_reason, github_anchor}.
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

        # Resolve GitHub anchor (may be None)
        github_field = fields.get("github", "").strip()
        github_anchor = _resolve_anchor(project_id, github_field)

        my_batons.append({
            "project_id": project_id,
            "title": title,
            "status": status,
            "turn_since": turn_since,
            "turn_reason": turn_reason,
            "github_anchor": github_anchor,
        })

    # Oldest baton first (most overdue = highest urgency)
    my_batons.sort(key=lambda b: b["turn_since"] or datetime.min.replace(tzinfo=timezone.utc))
    return my_batons


# ---------------------------------------------------------------------------
# Auto-archive pass
# ---------------------------------------------------------------------------

def _auto_archive_merged_pr_batons(cache: dict) -> None:
    """Scan ALL open PR-batons and close any whose live GitHub status is terminal.

    Handles two terminal GitHub PR states:
    - MERGED → baton closed with status ``merged``
    - CLOSED (without merge) → baton closed with status ``cancelled``

    This pass is independent of the in-court rendering loop — it runs over
    every baton file regardless of which agent holds the turn.  Merged PRs
    always have turn==<maintainer> (the merge-ready signal), so they are
    never in any agent's court and would never be reached by the rendering
    loop scan.  Closed-without-merge PRs can have any turn value (the PR may
    have been closed mid-review from any turn), so the second-scan model is
    the only reliable detection path for those too.

    Instrumentation: every outcome (success, failure, skip) is written to
    stderr (the hook log channel).  Stdout is reserved for prompt context.

    Cost discipline: reuses the existing 30s-TTL cache passed in from main();
    adds zero gh calls for cache-fresh anchors.
    """
    # Resolve the baton binary once; reuse the result for the guard check.
    _baton_which = shutil.which("baton")
    baton_bin = _baton_which or _BATON_FALLBACK
    if not _baton_which and not Path(_BATON_FALLBACK).exists():
        print(
            "baton auto-archive: baton CLI not found; skipping",
            file=sys.stderr,
        )
        return

    projects_dir = Path(BATON_PROJECTS_DIR)
    if not projects_dir.is_dir():
        return

    for md_file in projects_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        fields, _body = _parse_frontmatter(text)
        if not fields:
            continue

        # Skip already-closed batons (idempotency gate)
        status = fields.get("status", "").strip().lower()
        if status in CLOSED_STATUSES:
            continue

        # Only PR-batons (project_id matches ^PR-\d+$)
        project_id = fields.get("project", "").strip()
        if not project_id:
            project_id = md_file.stem
        if not _PR_ID_RE.match(project_id):
            continue

        # Resolve GitHub anchor — fallback to PR-NNN pattern
        github_field = fields.get("github", "").strip()
        anchor = _resolve_anchor(project_id, github_field)
        if not anchor:
            continue

        # Fetch live status via the existing cache mechanism (30s TTL)
        live_status = _get_live_status(anchor, cache)

        # Map live GitHub terminal states to baton close statuses.
        if live_status == "MERGED" or live_status.startswith("MERGED"):
            close_status = "merged"
            log_reason = "PR merged"
        elif live_status == "CLOSED":
            close_status = "cancelled"
            log_reason = "PR closed without merge"
        else:
            continue

        # Live PR is terminal — close the baton
        pid = project_id
        try:
            result = subprocess.run(
                [baton_bin, "close", pid, "--status", close_status],
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT,
            )
            if result.returncode != 0:
                stderr_snippet = (result.stderr or "")[:200]
                print(
                    f"baton auto-archive: close {pid} failed (rc={result.returncode}): "
                    f"{stderr_snippet}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"baton auto-archive: closed {pid} ({log_reason})",
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"baton auto-archive: close {pid} raised: {e!r}",
                file=sys.stderr,
            )


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

    # ── Detect gh availability once (don't retry per baton) ──────────────────
    gh_ok = _gh_available()

    # ── Load cache (once for all batons and the auto-archive pass) ───────────
    cache: dict = {}
    if gh_ok:
        try:
            cache = _load_cache()
        except Exception:
            cache = {}

    # ── Scan for my batons ────────────────────────────────────────────────────
    my_batons = _scan_my_batons(agent_name)

    # ── Build injection block (only when there are in-court batons) ───────────
    if my_batons:
        n = len(my_batons)
        baton_word = "baton" if n == 1 else "batons"
        lines = [
            f"\U0001f3be {n} {baton_word} in your court — YOUR MOVE on each "
            f"(a baton here = ACTION PENDING from you, NOT 'parked', NOT 'waiting on them'):"
        ]

        for b in my_batons:
            pid = b["project_id"]
            turn_since = b["turn_since"]
            turn_reason = b["turn_reason"]
            github_anchor = b["github_anchor"]

            # Format: since HH:MMZ, Xh ago
            if turn_since:
                since_short = turn_since.strftime("%H:%MZ")
                age = _format_age(turn_since)
                age_part = f", {age}" if age else ""
                since_str = f"since {since_short}{age_part}"
            else:
                since_str = ""

            reason_part = f" — {turn_reason}" if turn_reason else ""

            # Live GitHub status clause (best-effort; never blocks or raises)
            live_clause = ""
            if gh_ok and github_anchor:
                try:
                    live_status = _get_live_status(github_anchor, cache)
                    if live_status:
                        live_clause = f" [{live_status}]"
                except Exception:
                    pass

            if since_str:
                lines.append(f"  - {pid} ({since_str}){reason_part}{live_clause}")
            else:
                lines.append(f"  - {pid}{reason_part}{live_clause}")

        # Explicit decision prompt — a baton in your court is a PENDING ACTION, not a
        # status line. Force classification rather than glazing past "in your court"
        # as "parked." (Origin: a baton sat un-flipped 37m, misread as "waiting on
        # peer" when the peer had no signal — the holder must flip for them to know.)
        lines.append("  ↳ For EACH above, decide which state it's in — then ACT, don't defer:")
        lines.append("     • still working on it → keep it (no flip needed)")
        lines.append("     • done, waiting on a peer → FLIP IT NOW to them: baton flip <id> <peer>")
        lines.append("     • colleague-approved + CI green → FLIP IT NOW to the maintainer")
        lines.append(
            "     A baton left in your court un-flipped is a DROPPED baton: the peer/maintainer "
            "gets NO signal until you flip. \"In your court\" means YOUR move — it is never \"parked.\""
        )

        context = "\n".join(lines)

        response = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }

        print(json.dumps(response))

    # ── Auto-archive pass: runs even when my_batons is empty ─────────────────
    # Terminal PR-batons (MERGED or CLOSED) may have any turn value, so they
    # are not reliably caught by the in-court scan above.  This second scan
    # covers ALL open PR-batons regardless of turn, closing any whose live
    # GitHub status is a terminal state (MERGED → "merged"; CLOSED → "cancelled").
    # Guard: skip when gh is unavailable (no live status reachable anyway).
    if gh_ok:
        _auto_archive_merged_pr_batons(cache)

    # ── Persist cache after auto-archive pass ────────────────────────────────
    # Single save here, after both the in-court rendering pass and the
    # auto-archive pass have completed.  Covers archive-pass cache additions
    # on both the in-court and zero-in-court paths.
    if gh_ok and cache:
        try:
            _save_cache(cache)
        except Exception:
            pass


if __name__ == "__main__":
    main()
