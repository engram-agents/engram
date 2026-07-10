#!/usr/bin/env python3
"""UserPromptSubmit hook: surface baton-in-court projects to the agent.

On every prompt, in multi-agent mode, queries the forum coordination API
(GET /api/projects) for project records where turn == this agent and status
is active (not merged or cancelled). Injects a summary into the session
context reminding the agent of pending batons. Reads are 100% via the LAN
forum API — no local-filesystem path (UCS pure-API invariant). If the API is
unreachable, the hook surfaces a loud "⚠️ UCS unreachable" banner rather than
silently rendering nothing.

Unlike the inter-agent hook, baton has no cursor — turn state is the
explicit current-state field in the file, read fresh each prompt. No
surfaced/read cursor mechanism needed; the baton file IS the state.

Anti-patterns guarded:
  - Single-agent guard: if config.json mode != 'multi', exits silently.
    Single-agent users see zero behavior change.
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
# import, the hook degrades to a silent no-op rather than crashing, same
# discipline as every other best-effort import in this prologue.
_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
try:
    from _prompthooklib import (
        load_config as _load_config_impl,
        get_agent_name as _get_agent_name_impl,
        emit_context as _emit_context,
        bootstrap_tools_dir as _bootstrap_tools_dir,
    )
    _PROMPTHOOKLIB_OK = True
except Exception:
    _PROMPTHOOKLIB_OK = False
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

# GitHub Project owner (used for project-anchor queries)
_GH_OWNER = os.environ.get("BATON_GH_OWNER", "engram-agents")

# #1715: default repo a BARE pr/<N> anchor resolves against. Same variable
# name as tools/baton.py's own DEFAULT_GITHUB_REPO / board_projects.py's own
# copy -- read here too (self-contained; this hook is a standalone deployed
# script and must not assume it can import a sibling module post-plugin-
# restructure, per this repo's CLAUDE.md). Public-safe fallback, not the
# private dev-repo literal (tools/scan-leaks.py flags that shape of leak).
_DEFAULT_GITHUB_REPO = os.environ.get("ENGRAM_DEFAULT_GITHUB_REPO", "engram-agents/engram")

# #1715: pr/<N> (bare) or pr/<owner>/<repo>/<N> (repo-qualified) -- mirrors
# tools/baton.py's _GITHUB_ANCHOR_RE / _parse_github_anchor.
_PR_ANCHOR_RE = re.compile(r"^pr/(?:([\w.-]+)/([\w.-]+)/)?(\d+)$", re.IGNORECASE)


def _parse_pr_anchor(anchor: str) -> Optional[tuple]:
    """Parse a pr/<N> or pr/<owner>/<repo>/<N> anchor into (repo, pr_number).

    repo defaults to _DEFAULT_GITHUB_REPO for a bare anchor. Returns None if
    the anchor isn't PR-shaped at all.
    """
    m = _PR_ANCHOR_RE.match(anchor.strip().lower())
    if not m:
        return None
    owner, repo_name, pr_num = m.groups()
    repo = f"{owner}/{repo_name}" if owner and repo_name else _DEFAULT_GITHUB_REPO
    return (repo, pr_num)

# Frontmatter parsing (same regex as ia.py / inter-agent hook)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)

# PR-ID fallback pattern: "PR-NNN"
_PR_ID_RE = re.compile(r"^PR-(\d+)$")


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

def _load_config() -> dict:
    """Load $ENGRAM_HOME/config.json. Returns {} on any failure."""
    return _load_config_impl(ENGRAM_HOME)


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
                # #1715: parse (repo, pr_num) instead of a naive anchor[3:]
                # slice -- the slice produced "owner/repo/N" verbatim for a
                # repo-qualified anchor, which gh would reject as a garbage
                # PR-number argument.
                parsed = _parse_pr_anchor(anchor)
                if parsed is not None:
                    pr_repo, pr_num = parsed
                    render = _fetch_pr_status(pr_num, repo=pr_repo)
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
# Forum API (pure-API coordination reads — UCS invariant)
# ---------------------------------------------------------------------------

def _is_multi_agent_mode(config: dict) -> bool:
    """True if config.json mode == 'multi' (mirrors ia.py / baton.py)."""
    return config.get("mode", "single") == "multi"


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
    """
    # Resolve the baton binary; skip if not in PATH.
    _baton_which = shutil.which("baton")
    if not _baton_which:
        print(
            "baton auto-archive: baton CLI not found in PATH; skipping",
            file=sys.stderr,
        )
        return

    for p in projects:
        # Skip already-closed batons (idempotency gate). The fetched list is
        # already active_only, so this is belt-and-suspenders.
        status = (p.get("status") or "").strip().lower()
        if status in CLOSED_STATUSES:
            continue

        # Only PR-batons (project_id matches ^PR-\d+$)
        project_id = (p.get("project_id") or "").strip()
        if not _PR_ID_RE.match(project_id):
            continue

        # Resolve GitHub anchor. The auto-archive pass only runs on PR-N
        # batons so the id-fallback suffices; the github: field is a bonus.
        anchor = _resolve_anchor(project_id, p.get("github", ""))
        if not anchor:
            continue

        # Cache-only: skip uncached anchors to stay within the hook's 5s timeout.
        # With 430+ baton files, uncached gh calls (~0.4s each) would exceed the
        # timeout before completing the scan. Use 'baton gc' for full offline sweep.
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# _emit_context now lives in _prompthooklib (gh#1680 slice 1); imported above.


def main() -> None:
    # ── Prologue-lib gate (gh#1680 slice 1) ───────────────────────────────────
    # If _prompthooklib somehow failed to import, _load_config/_get_agent_name/
    # _emit_context are unavailable -- degrade to a silent no-op, matching
    # every other best-effort import in this hook's prologue.
    if not _PROMPTHOOKLIB_OK:
        sys.exit(0)

    # ── Config + agent name ───────────────────────────────────────────────────
    config = _load_config()
    agent_name = _get_agent_name(config)
    # Even if agent_name is empty, filtering returns nothing (no turn match).

    # ── Multi-agent gate ──────────────────────────────────────────────────────
    # Pure-API: single-agent installs (mode != 'multi'), or a missing forum_api
    # import, exit silently — zero behavior change, and no local-FS fallback.
    if not _FORUM_API_OK or not _is_multi_agent_mode(config):
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
