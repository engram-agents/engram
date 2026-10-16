#!/usr/bin/env python3
"""UserPromptSubmit hook: surface baton-in-court projects to the agent.

On every prompt, when a forum is configured, queries the forum coordination
API (GET /api/projects) for project records where turn == this agent and
status is active (not merged or cancelled). Injects a summary into the
session context reminding the agent of pending batons. Reads are 100% via
the LAN forum API — no local-filesystem path (UCS pure-API invariant). If
the API is unreachable, the hook surfaces a loud "⚠️ UCS unreachable" banner
rather than silently rendering nothing.

Unlike the inter-agent hook, baton has no cursor — turn state is the
explicit current-state field in the file, read fresh each prompt. No
surfaced/read cursor mechanism needed; the baton file IS the state.

Anti-patterns guarded:
  - Forum-configured guard (#2857 step 1): if no forum.url is set in
    config.json (agent_name alone does not count), exits silently. Installs
    with no forum configured see zero behavior change. (Previously gated on
    config.json mode == 'multi' — baton is symmetric for any agent once a
    forum is configured, regardless of host topology.)
  - No LLM calls, no DB — one forum API call (+ optional gh calls) only.
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

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Pure-API (UCS invariant): this hook reads coordination state ONLY via the
# forum HTTP API — no local-filesystem fallback. forum_api ships in tools/.
#
# Resolve tools/ in BOTH topologies via the shared _hooklib helper (gh#1657 —
# this walk-parents logic was duplicated byte-for-byte across this hook and
# engram-inter-agent-prompt-hook.py; extracted to stop the two from silently
# drifting apart the way forum's own copy once did, #1558). _hooklib.py lives
# alongside this file in both topologies (source: hooks/claude/; deployed:
# hooks/), so no walk-parents is needed to FIND it — but the module-loading
# path used by this repo's own tests (importlib.util.spec_from_file_location)
# does not auto-add a script's own directory to sys.path the way a real
# `python3 hooks/x.py` invocation does, so this hook adds its own directory
# explicitly before importing _hooklib. Import is best-effort throughout:
# any failure degrades the hook to a silent no-op rather than crashing the
# prompt. (Behavior note: the pre-extraction inline code did NOT wrap the
# walk-parents loop itself in try/except -- a mid-walk exception, e.g. a
# permission error from Path.exists(), would have propagated and crashed
# the hook. Wrapping it here is a deliberate tightening to match this
# file's existing "never crash the prompt" discipline for every other
# best-effort import below, not an accidental behavior change.)
#
# gh#1680 slice 1: the config/agent-name/emit-context/resolve-tools-dir
# prologue previously inlined here now lives in the shared _prompthooklib
# module (same directory, same sys.path bootstrap as _hooklib below).
# _PROMPTHOOKLIB_OK gates main() -- if the shared module somehow fails to
# import, the hook reports the exception class (one stderr line, one injected
# context line; #3521) and exits 0 rather than crashing.
_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
try:
    from _prompthooklib import (
        load_config_checked as _load_config_checked_impl,
        report_config_unknown as _report_config_unknown,
        get_agent_name as _get_agent_name_impl,
        emit_context as _emit_context,
        bootstrap_tools_dir as _bootstrap_tools_dir,
        # #3628: the cache read/write are non-blocking and regular-file-only.
        hooklib_io as _hooklib_io,
    )
    _PROMPTHOOKLIB_OK = True
except Exception as _lib_exc:
    # #3521: remember the CLASS only (never the message); main() reports it.
    _PROMPTHOOKLIB_OK = False
    _PROMPTHOOKLIB_IMPORT_ERROR = type(_lib_exc).__name__
_TOOLS_DIR = _bootstrap_tools_dir("forum_api.py") if _PROMPTHOOKLIB_OK else None
try:
    from forum_api import (
        ForumClient,
        ForumNetworkError,
        ForumHttpError,
        forum_url_from_config,
    )
    _FORUM_API_OK = True
except Exception:
    _FORUM_API_OK = False

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

# Statuses that mean "closed" — these projects don't surface
CLOSED_STATUSES = {"merged", "cancelled"}

# Cache TTL in seconds — entries younger than this are reused without a gh call
_CACHE_TTL = 30

# Per-gh-call timeout in seconds — quick-fail so the hook stays fast
_GH_TIMEOUT = 4

# #3568 F1: the GitHub owner of a project anchor comes from the anchor itself
# (project/<owner>/<N>), else from $BATON_GH_OWNER if set and non-empty, else there is
# NO owner: no gh call, and the render says so. There is deliberately NO hardcoded
# default org -- a released feature must not name a team (same rule as the bare pr/<N>
# anchor below). Resolved at call time, not import time.
_NO_OWNER_NOTICE = "project anchor has no owner: re-anchor as project/<owner>/<N>"

# #1918 / #1650 / #1919: there is NO default repo for a bare pr/<N> anchor,
# deliberately -- do not revisit (same rule as tools/baton.py and
# tools/baton_gate.py). This hook used to resolve a bare anchor against
# $ENGRAM_DEFAULT_GITHUB_REPO (falling back to the public repo) and then
# AUTO-ARCHIVE the baton from the cached result, so on a seat that set the
# variable, a legacy bare PR-<N> baton belonging to another repo was closed
# from the wrong repo's #N (#1650's 07-03 incident). A bare anchor is now
# never fetched, never cached and never auto-archived, whatever the
# environment says; the render says it needs a qualified anchor and the
# auto-archive pass counts what it skipped.

# #1715: pr/<N> (bare) or pr/<owner>/<repo>/<N> (repo-qualified) -- mirrors
# tools/baton.py's _GITHUB_ANCHOR_RE / _parse_github_anchor.
_PR_ANCHOR_RE = re.compile(r"^pr/(?:([\w.-]+)/([\w.-]+)/)?(\d+)$", re.IGNORECASE)


# #3568: project/<N> (bare) or project/<owner>/<N>. MIRRORS baton_gate.py's
# _PROJECT_OWNER_PATTERN / _PROJECT_OWNER_ANCHOR_RE (the source of truth; kept in sync
# by hand like _PR_ANCHOR_RE above). Owner = GitHub login rule: 1-39 chars, alnum or
# hyphen, not starting with a hyphen. Anchors are lowercased before parsing, and
# GitHub logins are case-insensitive, so re.IGNORECASE changes nothing real.
_PROJECT_OWNER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_PROJECT_ANCHOR_RE = re.compile(
    r"^project/(?:(" + _PROJECT_OWNER_PATTERN + r")/)?([0-9]{1,15})$", re.IGNORECASE
)


def _parse_project_anchor(anchor: str) -> Optional[tuple]:
    """Parse project/<N> or project/<owner>/<N> into (owner_or_None, number).

    Returns None if the anchor is not a well-formed project anchor.
    """
    m = _PROJECT_ANCHOR_RE.match(anchor.strip().lower())
    if not m:
        return None
    return (m.group(1), m.group(2))


def _project_anchor_owner(anchor: Optional[str]) -> Optional[str]:
    """The owner carried by a project/<owner>/<N> anchor, else None (bare or malformed)."""
    if not anchor:
        return None
    parsed = _parse_project_anchor(anchor)
    return parsed[0] if parsed else None


def _parse_pr_anchor(anchor: str) -> Optional[tuple]:
    """Parse a pr/<owner>/<repo>/<N> anchor into (repo, pr_number).

    Returns None if the anchor isn't PR-shaped at all, AND for a bare pr/<N>
    anchor (#1918: no default repo -- a bare anchor has no repo to return).
    """
    m = _PR_ANCHOR_RE.match(anchor.strip().lower())
    if not m:
        return None
    owner, repo_name, pr_num = m.groups()
    if not (owner and repo_name):
        return None
    return (f"{owner}/{repo_name}", pr_num)


def _is_bare_pr_anchor(anchor: Optional[str]) -> bool:
    """True for an unqualified pr/<N> anchor (PR-shaped, but no repo).

    Derived from _parse_pr_anchor so there is ONE place that decides whether
    an anchor carries a repo: PR-shaped yet unparseable == bare.
    """
    if not anchor:
        return False
    return bool(_PR_ANCHOR_RE.match(anchor.strip().lower())) and _parse_pr_anchor(anchor) is None


# Render text for an in-court baton whose anchor is a bare pr/<N>.
_NEEDS_QUALIFIED_ANCHOR = "needs a qualified anchor: pr/<owner>/<repo>/<N>"

# Frontmatter parsing (same regex as ia.py / inter-agent hook)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):[ \t]*(.*)$", re.MULTILINE)

# PR-ID fallback pattern: "PR-NNN"
_PR_ID_RE = re.compile(r"^PR-(\d+)$")

# Matches a PR-baton's project_id by PREFIX ONLY: an optional
# single-segment repo-qualifier (e.g. "UMWELT-") followed by "PR-". This
# is a pure shape test -- what follows the "PR-" prefix (a bare number, or
# a number plus a suffix like "-paper", e.g. "PR-37-paper") is irrelevant
# to this gate; the actual PR number/repo always come from the
# separately-stored `github:` anchor field, never from this regex. #2010:
# the original prefix-only check required the id to START with "PR-", so
# a repo-qualified id like "UMWELT-PR-572" was invisible to this gate; a
# first fix over-corrected into a full-string ("...(\d+)$") match, which
# then made a live "PR-<N>-<repo-slug>" naming shape (e.g. "PR-37-paper")
# invisible too. This prefix-only form fixes both. Kept in sync by hand
# with the sibling copy in tools/baton.py. Deliberately separate from
# _PR_ID_RE above, which has a
# narrower, still-correct job (_resolve_anchor's ID-based anchor fallback,
# used only when no explicit github: field is present) -- widening
# _PR_ID_RE itself would make _resolve_anchor start guessing an anchor
# from a repo-prefixed id like "UMWELT-PR-572" as if it meant an
# unqualified pr/572 in some ambient/default repo, exactly the guessing
# behavior #1715/#1918 deliberately removed elsewhere in this file.
_PR_BATON_NAME_RE = re.compile(r"^(?:[A-Z][A-Z0-9]*-)?PR-")


# ---------------------------------------------------------------------------
# Config helpers -- delegate to _prompthooklib (gh#1680 slice 1). Thin
# per-hook wrappers, not bare aliases: they pin ENGRAM_HOME (this hook's own
# module constant, captured once at import time above) as an explicit arg,
# preserving both (a) the exact zero-arg call signature every existing call
# site and test-monkeypatch already depends on, and (b) the "frozen at
# import time" ENGRAM_HOME timing the pre-extraction inline code had --
# the shared load_config()'s own default (re-reading os.environ at call
# time) is NOT equivalent when ENGRAM_HOME changes between a hook's import
# and its main() call, which the test harness's `patch.dict` (scoped only
# around exec_module) actually does.
# ---------------------------------------------------------------------------

# #3521 row 6: why the last _load_config() could not read config.json, or None
# when it could (or the file is simply absent). main() reads this right after
# _load_config() so an unreadable file is reported as UNKNOWN instead of being
# treated as "not configured". Kept as module state, not a changed return
# type, so _load_config() keeps its zero-arg dict contract for every existing
# caller and test monkeypatch.
_config_unknown_reason: Optional[str] = None


def _load_config() -> dict:
    """Load $ENGRAM_HOME/config.json. Returns {} on any failure (and records
    the reason in _config_unknown_reason when the file is present but unusable)."""
    global _config_unknown_reason
    cfg, _config_unknown_reason = _load_config_checked_impl(ENGRAM_HOME)
    return cfg or {}


def _get_agent_name(config: Optional[dict] = None) -> str:
    """Resolve this agent's own name. See _prompthooklib.get_agent_name."""
    return _get_agent_name_impl(config, ENGRAM_HOME)


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
            return json.loads(_hooklib_io().read_regular_text(path, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_cache(cache: dict) -> None:
    """Write cache to disk. Silently ignores write failures."""
    path = _cache_path()
    try:
        with _hooklib_io().open_regular_text_write(path, encoding="utf-8") as f:
            f.write(json.dumps(cache))
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


def _fetch_pr_status(pr_num: str, repo: Optional[str] = None) -> Optional[str]:
    """Call ``gh pr view`` and return a short render string, or None on error.

    Returns strings like ``"MERGED"``, ``"OPEN · review:APPROVED"``,
    ``"OPEN · review:REVIEW_REQUIRED"``, etc.

    #1715: pass `repo` (owner/repo) for a repo-qualified anchor so this
    queries the ANCHOR's actual repo, not gh's ambient-cwd guess.
    """
    cmd = [
        "gh", "pr", "view", pr_num,
        "--json", "state,reviewDecision,title",
        "--jq", "[.state, .reviewDecision] | join(\"|\")",
    ]
    if repo:
        cmd += ["--repo", repo]
    try:
        result = subprocess.run(
            cmd,
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


def _fetch_project_status(project_num: str, owner: str) -> Optional[str]:
    """Call ``gh project item-list`` and return a tally render string, or None.

    ``owner`` is required (#3568): the caller resolves it from the anchor or the
    environment and never calls this without one.

    Returns strings like ``"2 Done · 1 In Progress · 2 Blocked"``.
    """
    try:
        result = subprocess.run(
            [
                "gh", "project", "item-list", project_num,
                "--owner", owner,
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
        # #1918: a bare pr/<N> anchor has no repo -- never consult the cache
        # (a stale/poisoned entry under that key must not render), never
        # fetch, never cache. Say what it needs instead of a status.
        if _is_bare_pr_anchor(anchor):
            return _NEEDS_QUALIFIED_ANCHOR

        # Check cache first
        cached = _cache_get(cache, anchor)
        if cached is not None:
            return cached

        # Need a fresh fetch — wrap in its own try so exceptions still
        # reach the stale-cache fallback below.
        render: Optional[str] = None
        try:
            if anchor.startswith("pr/"):
                # #1715: parse (repo, pr_num) instead of a naive anchor[3:]
                # slice -- the slice produced "owner/repo/N" verbatim for a
                # repo-qualified anchor, which gh would reject as a garbage
                # PR-number argument.
                # #1918: a bare anchor parses to None (no default repo) and
                # was already handled above; only a qualified one fetches.
                parsed = _parse_pr_anchor(anchor)
                if parsed is not None:
                    pr_repo, pr_num = parsed
                    render = _fetch_pr_status(pr_num, repo=pr_repo)
            elif anchor.startswith("project/"):
                parsed_proj = _parse_project_anchor(anchor)
                if parsed_proj is not None:
                    anchor_owner, project_num = parsed_proj
                    # #3568: anchor owner -> $BATON_GH_OWNER (non-empty) -> none.
                    owner = anchor_owner or os.environ.get("BATON_GH_OWNER", "").strip()
                    if not owner:
                        # No owner anywhere: make no gh call, cache nothing (the
                        # stale-cache fallback below is keyed on the same anchor),
                        # and say so.
                        return _NO_OWNER_NOTICE
                    render = _fetch_project_status(project_num, owner)
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
# Forum API (pure-API coordination reads — UCS invariant)
# ---------------------------------------------------------------------------

def _is_forum_configured(config: dict) -> bool:
    """Return True if a forum is configured: ``forum.url`` is a non-empty string.

    Silent no-op gate: no ``forum.url`` -> skip the hook, so an install with no
    forum server never reports one as unreachable. ``agent_name`` alone is this
    agent's identity, not forum configuration, and does not pass the gate;
    neither does the ``FORUM_URL`` env var by itself. Total: any other shape
    (non-dict ``forum``, non-string or blank ``url``) is "not configured".
    """
    if not isinstance(config, dict):
        return False
    forum_cfg = config.get("forum")
    if not isinstance(forum_cfg, dict):
        return False
    url = forum_cfg.get("url")
    return isinstance(url, str) and bool(url.strip())


def _fetch_active_projects(config: dict) -> list:
    """GET active project records via the forum API.

    Returns the list of project dicts. Raises ForumNetworkError /
    ForumHttpError on failure — the caller (main) decides whether to surface
    the loud UCS-unreachable banner. There is NO local-filesystem fallback:
    coordination state lives only in the LAN forum service.

    Short timeout (3s, vs the CLI's 10s default): this runs SYNCHRONOUSLY on
    every prompt, so a half-dead forum (accepts the socket, never responds)
    must not stall the prompt up to 10s. Connection-refused already fails fast;
    this bounds the slow-loris case to stay within the hook's time budget.
    """
    client = ForumClient(forum_url_from_config(config), timeout=3)
    resp = client.get("/api/projects", params={"active_only": "true"})
    return resp.get("projects", [])


# ---------------------------------------------------------------------------
# Project filtering
# ---------------------------------------------------------------------------

def _my_batons(projects: list, agent_name: str) -> list:
    """Filter fetched project records to those where turn == agent_name.

    Returns list of dicts:
        {project_id, title, status, turn_since, turn_reason, github_anchor}.
    Sorted by turn_since ascending (oldest baton first = most overdue first).

    ``projects`` is the list from GET /api/projects (already active_only).
    The list endpoint now carries the ``github`` field, so both PR-anchored
    (pr/N) and project-anchored (project/N) batons resolve to a live-status
    clause via _resolve_anchor.
    """
    if not agent_name:
        return []
    me = agent_name.lower()
    my_batons = []
    for p in projects:
        status = (p.get("status") or "").strip().lower()
        if status in CLOSED_STATUSES:
            continue
        if (p.get("turn") or "").strip().lower() != me:
            continue
        project_id = (p.get("project_id") or "").strip()
        my_batons.append({
            "project_id": project_id,
            "title": (p.get("title") or "").strip(),
            "status": status,
            "turn_since": _parse_iso((p.get("turn_since") or "").strip()),
            "turn_reason": (p.get("turn_reason") or "").strip(),
            "github_anchor": _resolve_anchor(project_id, p.get("github", "")),
        })
    # Oldest baton first (most overdue = highest urgency)
    my_batons.sort(key=lambda b: b["turn_since"] or datetime.min.replace(tzinfo=timezone.utc))
    return my_batons


# ---------------------------------------------------------------------------
# Auto-archive pass
# ---------------------------------------------------------------------------

def _auto_archive_merged_pr_batons(projects: list, cache: dict) -> None:
    """Scan ALL open PR-batons and close any whose cached GitHub status is terminal.

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

    Cache-only mode: this function skips any PR-baton whose anchor is not
    already present in the 30s-TTL cache.  With 430+ baton files, making a
    fresh gh API call (~0.4s each) per uncached anchor would far exceed the
    hook's 5s timeout before completing the scan.  Use ``baton gc`` for an
    explicit full offline sweep that queries live GitHub state for every
    uncached PR-baton.

    Detection order is ANCHOR-FIRST (#2010 rider 1, Kepler DM 2026-09-21),
    mirroring tools/baton.py's cmd_gc: a project whose ``github:`` field
    resolves to a repo-qualified PR anchor is a PR-baton unconditionally,
    whatever its ``project_id`` looks like.

    #1918 (closes #1650/#1919): there is NO default repo. A baton whose
    anchor is a bare ``pr/<N>`` -- explicit, or the ``PR-<N>`` id-fallback --
    is NEVER closed here, whatever the cache holds or the environment says;
    the skips are counted and reported in one stderr line per run. A
    non-PR anchor or no anchor at all is ignored silently, as before. The
    old name-shape fallback (_PR_BATON_NAME_RE) only ever applied to
    unqualified anchors, so it can no longer make anything closable and is
    not consulted.

    Two DISTINCT skip reasons exist past this point, and rider 2 requires
    they stay distinguishable rather than conflated into one "skipped"
    bucket:
      1. NO-QUALIFIED-ANCHOR -- the baton has no repo-qualified PR anchor
         (bare, non-PR, or absent). An anchor-shape decision, made before
         the cache is ever consulted; the bare case is counted.
      2. UNCACHED-ANCHOR -- the baton IS a repo-qualified PR-baton, but its
         anchor's live status hasn't been fetched into the 30s-TTL cache
         yet. This is a cache-BUDGET decision, made only after the baton
         has already cleared reason 1.
    """
    # Resolve the baton binary; skip if not in PATH.
    _baton_which = shutil.which("baton")
    if not _baton_which:
        print(
            "baton auto-archive: baton CLI not found in PATH; skipping",
            file=sys.stderr,
        )
        return

    skipped_bare = 0  # #1918: batons with an unqualified pr/<N> anchor

    for p in projects:
        # Skip already-closed batons (idempotency gate). The fetched list is
        # already active_only, so this is belt-and-suspenders.
        status = (p.get("status") or "").strip().lower()
        if status in CLOSED_STATUSES:
            continue

        project_id = (p.get("project_id") or "").strip()

        # #2010 rider 1: resolve the GitHub anchor FIRST. For a bare
        # "PR-<N>" id, _resolve_anchor's own id-fallback suffices with no
        # explicit github: field. For a repo-qualified id (e.g.
        # "UMWELT-PR-572") or any id shape at all, an explicit github:
        # field produces the anchor directly, independent of project_id.
        anchor = _resolve_anchor(project_id, p.get("github", ""))

        # #1918: a bare pr/<N> anchor (explicit, or the "PR-<N>" id-fallback)
        # has no repo and there is NO default repo -- it is never closed
        # from a cached status, whatever any cache or environment says.
        # Counted, and reported once per run after the loop.
        if _is_bare_pr_anchor(anchor):
            skipped_bare += 1
            continue

        # Only a repo-qualified PR anchor can reach the cache lookup below.
        # Everything else (a non-PR anchor such as project/<N>, or no anchor
        # at all) is not a PR-baton this pass can close.
        if not (anchor and _parse_pr_anchor(anchor)):
            continue

        # Cache-only (skip reason 2, above): skip uncached anchors to stay
        # within the hook's 5s timeout. With 430+ baton files, uncached gh
        # calls (~0.4s each) would exceed the timeout before completing the
        # scan. Use 'baton gc' for full offline sweep. This is a
        # cache-budget skip, NOT a name-shape rejection -- a
        # qualified-anchor PR-baton that reaches this line has already
        # cleared the name gate entirely (it never consulted it).
        cached = _cache_get(cache, anchor)
        if cached is None:
            continue
        live_status = cached

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
                [_baton_which, "close", pid, "--status", close_status],
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

    # #1918: ONE counted line per run for the batons refused above.
    if skipped_bare:
        print(
            f"baton auto-archive: skipped {skipped_bare} baton(s) with an "
            f"unqualified pr/<N> anchor (run baton anchor <id> --github "
            f"pr/<owner>/<repo>/<N>)",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# _emit_context now lives in _prompthooklib (gh#1680 slice 1); imported above.


def _report_prompthooklib_import_failed() -> None:
    """#3521: a failed `_prompthooklib` import is VISIBLE, not a silent no-op.

    One class-only stderr line plus one class-only injected context line, on
    every run (deliberately no rate limit: a broken install is rare and
    severe). The lib -- and its `emit_context` -- is the thing that failed, so
    the envelope is built here with `json` alone. Each emission is separately
    guarded: this function never raises, and if a write fails the hook falls
    back to the silent exit 0 it had before.
    """
    try:
        cls = "".join(
            ch if ch.isprintable() else "?" for ch in _PROMPTHOOKLIB_IMPORT_ERROR
        )[:80]
        line = f"engram-baton-prompt-hook: _prompthooklib import failed: {cls}"
    except Exception:
        return
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": line,
        }}))
        sys.stdout.flush()
    except Exception:
        pass


def main() -> None:
    # ── Prologue-lib gate (gh#1680 slice 1) ───────────────────────────────────
    # If _prompthooklib somehow failed to import, _load_config/_get_agent_name/
    # _emit_context are unavailable. Report the failure (class-only stderr
    # line + injected context line, #3521) and exit 0 without doing any other
    # work. When run as a script the exit is os._exit(0), not sys.exit(0):
    # the reporter swallows a failed write on a closed pipe, which leaves
    # data buffered, and the interpreter's shutdown flush would then fail and
    # turn the exit code into 120. When the module is loaded in-process (as
    # the tests do), it raises SystemExit(0) instead, because os._exit would
    # end the host process.
    if not _PROMPTHOOKLIB_OK:
        _report_prompthooklib_import_failed()
        try:
            sys.stderr.flush()
        except Exception:
            pass
        # Only a hook run as a script may skip shutdown: an in-process caller
        # (such as a test) must get a catchable SystemExit, never a killed process.
        if __name__ == "__main__":
            os._exit(0)
        sys.exit(0)

    # ── Config + agent name ───────────────────────────────────────────────────
    config = _load_config()
    if _config_unknown_reason is not None:
        # #3521 row 6: unreadable config.json is UNKNOWN, never "not configured".
        _report_config_unknown(
            "engram-baton-prompt-hook", "config:baton-prompt-hook", ENGRAM_HOME,
            _config_unknown_reason, _emit_context, "baton check skipped",
        )
        sys.exit(0)
    agent_name = _get_agent_name(config)
    # Even if agent_name is empty, filtering returns nothing (no turn match).

    # ── Forum-configured gate ────────────────────────────────────────────────
    # Pure-API: no forum configured (no forum.url), or a missing
    # forum_api import, exit silently — zero behavior change, and no local-FS
    # fallback.
    if not _FORUM_API_OK or not _is_forum_configured(config):
        sys.exit(0)

    # ── Fetch coordination state via the LAN forum API (loud on failure) ──────
    try:
        projects = _fetch_active_projects(config)
    except (ForumNetworkError, ForumHttpError) as e:
        # UCS invariant: fail LOUD — surface a banner rather than silently
        # rendering nothing. The forum being down IS a problem the agent should
        # see (even on the 5090 server host); it is never masked by a local read.
        _emit_context(
            "⚠️ UCS unreachable — baton/coordination state is "
            f"unavailable (forum API error: {e}). Coordination reads/writes will "
            "fail until the forum service is back."
        )
        return
    except Exception:
        # Belt-and-suspenders: an unexpected error must never crash the prompt.
        return

    # ── Detect gh availability once (don't retry per baton) ──────────────────
    gh_ok = _gh_available()

    # ── Load cache (once for all batons and the auto-archive pass) ───────────
    cache: dict = {}
    if gh_ok:
        try:
            cache = _load_cache()
        except Exception:
            cache = {}

    # ── Filter to my batons ───────────────────────────────────────────────────
    my_batons = _my_batons(projects, agent_name)

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
        _emit_context(context)

    # ── Auto-archive pass: runs even when my_batons is empty ─────────────────
    # Terminal PR-batons (MERGED or CLOSED) may have any turn value, so they
    # are not reliably caught by the in-court scan above.  This second scan
    # covers ALL open PR-batons regardless of turn, closing any whose live
    # GitHub status is a terminal state (MERGED → "merged"; CLOSED → "cancelled").
    # Guard: skip when gh is unavailable (no live status reachable anyway).
    if gh_ok:
        _auto_archive_merged_pr_batons(projects, cache)

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
