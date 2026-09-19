#!/usr/bin/env python3
"""UserPromptSubmit hook: surface new forum threads and @mentions to the agent.

On every prompt, queries GET /api/threads?since=<surfaced-cursor> and injects
a brief notice into additionalContext if new threads have appeared. Advances
the surfaced cursor after firing.

Also queries GET /api/agent/<name>/inbox and filters to kind=at_mention items,
then injects a 🔔 mention line ABOVE the generic 📢 threads line when pending
@mentions exist. The inbox endpoint uses server-side per-thread read marks
(p.id > COALESCE(r.last_read_post_id, 0)), so reading a thread server-side
clears the mention automatically — no local read-cursor involved.

Also auto-publishes derived agent status to /api/agents/status on a throttled
schedule (Q1–Q4 from spec #1077 part 1): synchronous POST with timeout=
PUBLISH_TIMEOUT_SECONDS, swallow-all-errors, throttle on max(cadence_seconds,
60), always-publish-on-state-change, mid-sleep guard. The publish never
breaks the existing thread/mention surfacing.

Two-cursor state for threads (surfaced cursor is still hook-owned):
  - forum-surfaced-cursor.txt: advances on every hook fire. Drives "new since
    last prompt." After the hook fires, threads that were "new" appear in the
    surfaced window on the next prompt.
  - forum-read-cursor.txt: vestigial — no longer consumed by this hook for
    mention queries. The `cursor` subcommand stays for manual override/back-compat.

Cache: 30s file-based cache at ~/.engram/forum-hook-cache.json to avoid
hammering the server on rapid successive prompts. Cache stores both threads and
inbox mentions together. Stale-cache fallback on server-unreachable (never break
the hook chain if the forum is down).

Publish state: separate ~/.engram/forum-status-publish.json tracks last-published
status with its own throttle interval (never shares the 30s-TTL cache).

comms_surface gate (alpha#2019 slice A): config.json["comms_surface"] ("umwelt"
| "forum", resolved via _prompthooklib.get_comms_surface()) decides which lines
this hook is allowed to emit. Under "forum" (the default -- absent key), the
🔔/📢 lines above are unchanged. Under "umwelt", both are suppressed (the
umwelt plugin's own hooks already surface world state) EXCEPT a single
compact 📪 advisory line when pending ADDRESSED items (mentions) exist --
the anti-black-hole clause: a world-preferring seat must never silently miss
an addressed forum item pre-bridge (umwelt#599 slice 4 will bridge these).
The guarantee is bounded by cache availability: a first-contact outage with
no prior cache is still silent, matching the existing forum-mode outage
contract above -- the advisory's absence is not proof of an empty inbox
during an outage. Broadcast-only counts (new threads, no mention) are fully
suppressed under "umwelt".

Silent no-op conditions:
  - forum.url and agent_name not configured.
  - Server unreachable (uses cached data if available, else silent skip).
  - No new threads since last prompt and no pending @mentions.
  - Inbox endpoint absent / error -> no mention line; generic behavior unchanged.
  - Status publish failure (forum-status-publish.json) -> silently swallowed;
    existing output unaffected.
  - comms_surface=umwelt with no pending addressed items -> fully silent
    (broadcast-only new-thread counts are not surfaced under umwelt).

Design doc: forum/spec.md
"""

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

SURFACED_CURSOR_PATH = os.path.join(ENGRAM_HOME, "forum-surfaced-cursor.txt")
# READ_CURSOR_PATH is no longer consumed by this hook for mention queries.
# Mention state is now derived from GET /api/agent/<name>/inbox, which uses
# server-side per-thread read marks — no local cursor needed.
# The `forum cursor` subcommand in tools/forum.py retains READ_CURSOR_PATH
# for manual override / back-compat only.
HOOK_CACHE_PATH = os.path.join(ENGRAM_HOME, "forum-hook-cache.json")

# Publish state file — separate from the 30s-TTL cache; its own throttle interval.
STATUS_PUBLISH_PATH = os.path.join(ENGRAM_HOME, "forum-status-publish.json")

CACHE_TTL_SECONDS = 30

_DEFAULT_FORUM_URL = "http://localhost:5002"

# hooks.json budgets this hook at 5s total. FETCH_TIMEOUT_SECONDS previously
# matched that budget exactly (5s), leaving zero margin for process startup +
# JSON parsing — on a genuinely unreachable forum the hook call itself would
# often exceed the outer budget and the harness would report a hook timeout
# instead of the intended graceful degrade-to-cache.
#
# Worst case per fire covered by THESE two constants is THREE calls, not two:
# _fetch_threads, then (only if threads succeeded) _fetch_inbox, then the
# unconditional publish POST. A prior version of this comment assumed at most
# one fetch plus the publish — true for a dead host (inbox is skipped when
# threads returns None) but false for a SLOW host: threads can succeed just
# under its own ceiling, then inbox times out at its own full ceiling, then
# publish still fires. FETCH_TIMEOUT_SECONDS covers both the threads and inbox
# call sites, so the invariant for THIS three-call class is
# 2*FETCH_TIMEOUT_SECONDS + PUBLISH_TIMEOUT_SECONDS must stay comfortably
# under 5s (colleague catch, PR #1642 review, 2026-08-03 — see
# test_worst_case_three_call_budget_under_hook_timeout for the regression
# guard on this exact sum).
#
# KNOWN GAP (gh#1906, not closed by this fix): the auto-publish block below
# also calls derive_own_status(), which unconditionally makes a FOURTH,
# uncached HTTP call (_fetch_held_batons, in _status_derive.py) on its own
# independent _FORUM_REQUEST_TIMEOUT_SECONDS=2.0 -- not derived from either
# constant here, and not covered by this module's 30s threads/mentions cache.
# The true worst case (threads + inbox + held-batons + publish) can still
# exceed 5s on a slow-but-alive host even with the constants below. Left
# open rather than shrinking these two constants further to compensate,
# since that constant is shared with tools/forum.py's unconstrained
# status-auto CLI path and squeezing these fetches much smaller trades away
# real margin for legitimately-slow (not dead) hosts. See gh#1906 for the
# candidate fixes (cache the held-batons call; a shared cross-call deadline;
# or the connect-level reachability precheck already flagged in #1641).
FETCH_TIMEOUT_SECONDS = 1.5
PUBLISH_TIMEOUT_SECONDS = 1.0

# ---------------------------------------------------------------------------
# _status_derive import (spec §2, Q2)
#
# Resolve tools/ in BOTH topologies via the shared _hooklib helper (gh#1657,
# slice 3 of 3 -- this walk-parents logic was near-identical to the baton/
# inter-agent hooks' own copies, parameterized on a different marker file:
# _status_derive.py rather than forum_api.py, since this hook's own import
# needs a different tools/ module). _hooklib.py lives alongside this file in
# both topologies (source: hooks/claude/; deployed: hooks/), so no
# walk-parents is needed to find it -- but this repo's own test loaders
# (importlib.util.spec_from_file_location) don't auto-add a script's own
# directory to sys.path the way a real `python3 hooks/x.py` invocation does,
# so this hook adds its own directory explicitly before importing _hooklib.
# If the import fails for ANY reason, derive_own_status is set to None and the
# publish silently no-ops — the existing thread/mention surfacing is unaffected.
#
# gh#1680 slice 1: the config/agent-name/resolve-tools-dir prologue
# previously inlined here now lives in the shared _prompthooklib module
# (same directory, same sys.path bootstrap used below). _PROMPTHOOKLIB_OK
# gates main() -- if the shared module somehow fails to import, the hook
# degrades to a silent no-op rather than crashing, same discipline as every
# other best-effort import in this prologue.
#
# engram-alpha#2863 WS2: this hook's own response-dict construction at the
# end of main() now routes through _assemble_response() below, which calls
# `_hooklib.emit_context()` (WS1, #2874) -- the shared bounded-injection
# renderer, imported unconditionally alongside it just below (same
# unconditional-import precedent as engram-surface-hook.py's own #2876
# migration: _hooklib.py / _harness.py are guaranteed sibling files in both
# build topologies, per the module docstring's #1539/#1558 note above, so
# this import doesn't need the defensive try/except _PROMPTHOOKLIB_OK gets).
#
# NAMING COLLISION worth flagging (mirrors Luria's #2876 note on
# check_principle_triggers_full): `_prompthooklib.py` already exports its
# OWN, differently-scoped `emit_context()` -- "print the additionalContext
# hookSpecificOutput envelope to stdout" for the baton/inter-agent hooks,
# with no budget/bounding logic at all (see that module's own docstring).
# This hook does NOT import that name (only the four _prompthooklib names
# below), so there is no runtime collision -- but two functions named
# `emit_context()` now exist in this directory with unrelated contracts;
# don't assume they're the same function when reading either hook.
# ---------------------------------------------------------------------------

_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import emit_context
from _harness import detect_harness, get_harness_budgets
try:
    from _prompthooklib import (
        load_config as _load_config_impl,
        get_agent_name as _get_agent_name_impl,
        get_comms_surface as _get_comms_surface_impl,
        bootstrap_tools_dir as _bootstrap_tools_dir,
    )
    _PROMPTHOOKLIB_OK = True
except Exception:
    _PROMPTHOOKLIB_OK = False
_tools_dir = _bootstrap_tools_dir("_status_derive.py") if _PROMPTHOOKLIB_OK else None
try:
    from _status_derive import derive_own_status, _read_loop_mode
except Exception:
    derive_own_status = None  # publish becomes a no-op; never break the hook
    _read_loop_mode = None    # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Config helpers -- delegate to _prompthooklib (gh#1680 slice 1). Thin
# per-hook wrappers, not bare aliases: they pin ENGRAM_HOME (this hook's own
# module constant, captured once at import time above) as an explicit arg,
# preserving both (a) the exact zero-arg / single-arg call signatures every
# existing call site and test-monkeypatch already depends on, and (b) the
# "frozen at import time" ENGRAM_HOME timing the pre-extraction inline code
# had.
# ---------------------------------------------------------------------------

def _ipv4_only_urlopen(req: urllib.request.Request, timeout: float):
    """urlopen, forced to IPv4-only DNS resolution.

    The forum host's mDNS record advertises unreachable IPv6 candidates (a
    link-local address plus globally-scoped ones with no route on this LAN)
    ahead of the working IPv4 address in getaddrinfo's result order. stdlib
    socket.create_connection tries each candidate serially at the full
    per-attempt timeout before falling through to the next — enough,
    compounded across the dead entries, to blow this hook's outer
    UserPromptSubmit budget (observed: multi-second stalls vs. a 5s budget).
    curl doesn't hit this because it races families (Happy Eyeballs); stdlib
    urllib has no such racing, so pin the family explicitly. See
    tools/forum_api.py's ForumClient._execute for the canonical writeup —
    duplicated here (not imported) per this hook's zero-import-back design.
    """
    real_getaddrinfo = socket.getaddrinfo

    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only_getaddrinfo
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    finally:
        socket.getaddrinfo = real_getaddrinfo


def _load_config() -> dict:
    """Load $ENGRAM_HOME/config.json. Returns {} on any failure."""
    return _load_config_impl(ENGRAM_HOME)


def _get_agent_name(config: dict) -> str:
    """Resolve agent name from config. Returns '' if not set."""
    return _get_agent_name_impl(config, ENGRAM_HOME)


def _get_comms_surface(config: dict) -> str:
    """Resolve this agent's comms surface ('umwelt' or 'forum') from config.

    See _prompthooklib.get_comms_surface() for the full semantics
    (alpha#2019 slice A).
    """
    return _get_comms_surface_impl(config, ENGRAM_HOME)


def _get_forum_url(config: dict) -> str:
    """Resolve forum server URL from config or env. Returns default if absent."""
    forum_cfg = config.get("forum", {})
    if isinstance(forum_cfg, dict):
        url = forum_cfg.get("url", "").strip()
        if url:
            return url.rstrip("/")
    env_url = os.environ.get("FORUM_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return _DEFAULT_FORUM_URL


def _is_forum_configured(config: dict) -> bool:
    """Return True if forum is explicitly configured (url or agent_name set).

    Silent no-op gate: if neither forum.url nor agent_name is in config, skip
    the hook to avoid noise on non-forum installs.
    """
    forum_cfg = config.get("forum", {})
    has_url = isinstance(forum_cfg, dict) and bool(forum_cfg.get("url", "").strip())
    has_agent = bool(config.get("agent_name", "").strip())
    return has_url or has_agent


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------

def _read_cursor(path: str) -> Optional[datetime]:
    """Read a cursor file; return UTC datetime or None if absent/invalid."""
    try:
        raw = Path(path).read_text().strip()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _write_cursor(path: str, ts: datetime) -> None:
    """Write a UTC datetime to a cursor file atomically."""
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(ts_str + "\n")
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> Optional[dict]:
    """Load the hook cache. Returns None if absent, expired, or invalid."""
    try:
        raw = Path(HOOK_CACHE_PATH).read_text()
        data = json.loads(raw)
        age = time.time() - data.get("fetched_at", 0)
        if age > CACHE_TTL_SECONDS:
            return None
        return data
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _save_cache(threads: list, mentions: list) -> None:
    """Save fetched threads and mentions to the hook cache."""
    dest = Path(HOOK_CACHE_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        payload = {"fetched_at": time.time(), "threads": threads, "mentions": mentions}
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Publish state helpers (separate from the 30s cache)
# ---------------------------------------------------------------------------

def _load_publish_state() -> Optional[dict]:
    """Load the last-published status state. Returns None if absent/invalid."""
    try:
        return json.loads(Path(STATUS_PUBLISH_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_publish_state(state: str, activity: Optional[str],
                        queue: list, cadence: Optional[int],
                        published_at: float) -> None:
    """Save publish state atomically (same discipline as _save_cache)."""
    dest = Path(STATUS_PUBLISH_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        payload = {
            "state": state,
            "activity": activity,
            "queue": queue,
            "cadence": cadence,
            "published_at": published_at,
        }
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Publish decision helpers (pure — unit-testable without a live server)
# ---------------------------------------------------------------------------

def _should_publish(
    last: Optional[dict],
    current_tuple: tuple,
    now: float,
    throttle: float,
) -> bool:
    """Return True if a status publish should fire.

    Rules (spec Q3):
      (a) No last file → publish.
      (b) Status tuple changed since last publish → publish.
      (c) Unchanged AND within throttle window → skip.
      (d) Unchanged AND past throttle window → publish.

    Args:
        last:           The last-published state dict (None if no file).
        current_tuple:  (state, activity, queue_tuple, cadence) — queue must be
                        a hashable type (tuple) so equality works correctly.
        now:            Current epoch float.
        throttle:       Throttle interval in seconds.
    """
    if last is None:
        return True

    # Compare current tuple against last-published tuple.
    # queue is stored as a list in JSON; normalise both sides to tuple for equality.
    last_tuple = (
        last.get("state"),
        last.get("activity"),
        tuple(last.get("queue") or []),
        last.get("cadence"),
    )
    if current_tuple != last_tuple:
        return True

    # Unchanged — check throttle window.
    last_published_at = last.get("published_at", 0) or 0
    return (now - last_published_at) > throttle


def _apply_sleep_guard(
    state: str,
    activity: Optional[str],
    queue: list,
    cadence: Optional[int],
    last: Optional[dict],
    now: float,
) -> tuple:
    """Apply the Q4 mid-sleep guard.

    If there is no loop marker AND the last-published state was 'sleeping' AND
    we are still within the throttle window, keep ALL of the derived fields at
    their last-published 'sleeping' values (state, activity, queue, AND cadence)
    to suppress the false-wake publish. Common case self-corrects on the next
    genuine wake.

    Cadence must be restored alongside the other fields: derive_own_status with
    no loop marker returns cadence=None, but the last sleeping publish carried
    the sleep cadence (e.g. 270). If only state/activity/queue were restored,
    the cadence mismatch (None vs 270) would make _should_publish see a changed
    tuple and fire a spurious POST that DROPS expected_republish_seconds — the
    server would then fall back to its global window. (Reviewer catch, #1077 r2.)

    Returns (state, activity, queue, cadence) — possibly overridden.
    """
    if _read_loop_mode is None:
        return state, activity, queue, cadence

    loop = _read_loop_mode()
    if loop is not None:
        # Loop marker present — genuine wake. No guard needed.
        return state, activity, queue, cadence

    if last is None:
        return state, activity, queue, cadence

    if last.get("state") != "sleeping":
        return state, activity, queue, cadence

    last_published_at = last.get("published_at", 0) or 0
    last_cadence = last.get("cadence")
    throttle = max(last_cadence or 0, 60)
    if (now - last_published_at) <= throttle:
        # Within the window — keep ALL last-sleeping fields (incl. cadence).
        return "sleeping", last.get("activity"), last.get("queue") or [], last_cadence

    return state, activity, queue, cadence


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def _fetch_threads(forum_url: str, since: Optional[str], agent_name: str) -> Optional[list]:
    """GET /api/threads?since=<cursor>&agent=<name>.

    Returns thread list on success, None on any error (caller uses cache).
    Timeout: FETCH_TIMEOUT_SECONDS (see module header for the budget math).
    """
    params = {}
    if since:
        params["since"] = since
    if agent_name:
        params["agent"] = agent_name

    if params:
        import urllib.parse
        qs = urllib.parse.urlencode(params)
        url = f"{forum_url}/api/threads?{qs}"
    else:
        url = f"{forum_url}/api/threads"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
        )
        with _ipv4_only_urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data.get("threads", [])
    except Exception:
        return None


def _fetch_inbox(forum_url: str, agent_name: str) -> Optional[list]:
    """GET /api/agent/<name>/inbox — returns at_mention items only.

    The inbox endpoint uses server-side per-thread read marks so "reading the
    thread clears the mention" — no local cursor needed.

    Returns a list of inbox items filtered to kind=at_mention on success,
    None on any error (advisory; never breaks the hook chain). Swallows ALL
    errors including 404 (old server without the inbox endpoint) — same
    bare-except discipline as _fetch_threads. Timeout: FETCH_TIMEOUT_SECONDS.
    URL-quotes the agent name for safety.

    Response shape: {"inbox": [...], "unread_all": N}
    Item shape: {post_id, thread_id, thread_title, author, kind, created_at}
    """
    import urllib.parse
    quoted_name = urllib.parse.quote(agent_name, safe="")
    url = f"{forum_url}/api/agent/{quoted_name}/inbox"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json"},
        )
        with _ipv4_only_urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            inbox = data.get("inbox", [])
            # Filter to at_mention only — reply_on_my_thread items are out of scope
            # for the banner (they are a different notification class).
            return [item for item in inbox if item.get("kind") == "at_mention"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

# engram-alpha#2863 WS2: mention_line becomes `head` -- the "must not miss"
# content routed through _assemble_response()/emit_context() below -- and
# thread titles are USER-CONTROLLED, unbounded content: the forum server's
# create-thread handler only .strip()s and rejects empty (src/forum/server.py,
# no max-length validation). Unlike engram-surface-hook.py's own head content
# (#2876), which is naturally short because it's built entirely from fixed
# system-health strings, this hook's head embeds real user-authored text --
# so WITHOUT a defensive per-title cap, a single pathologically long title
# could make `head` (mention_line) itself exceed the harness's head_budget
# BEFORE it ever reaches emit_context() (which only bounds the render IT
# produces, not the raw string a caller hands it). This is exactly the
# "never load content of unpredictable size" case (WS2 spec rule 2).
_MENTION_TITLE_MAX_CHARS = 150


def _truncate_title(title: str) -> str:
    """Defensively cap a single thread title before it enters mention_line.

    150 chars is a generous display width for a thread title (real titles
    observed in production top out far shorter -- census: 608-char total
    mention line, 2026-09-12) while keeping even the worst case (3 shown
    items, each at the cap) comfortably under head_budget (1,800)."""
    if len(title) <= _MENTION_TITLE_MAX_CHARS:
        return title
    return title[: _MENTION_TITLE_MAX_CHARS - 1] + "…"


def _format_mention_line(mentions: list) -> Optional[str]:
    """Format the 🔔 mention summary line for injection into additionalContext.

    Mirrors the wording of forum status's _format_mention_line (tools/forum.py)
    but uses "mention"/"mentions" (not "post"/"posts") for noun agreement, caps
    inline display at 3, and appends the `forum read <id>` nudge.

    Returns None when the list is empty.
    """
    if not mentions:
        return None

    MAX_SHOWN = 3
    shown = mentions[:MAX_SHOWN]
    rest = len(mentions) - MAX_SHOWN

    parts = []
    for m in shown:
        tid = m.get("thread_id", "?")
        title = _truncate_title(m.get("thread_title", "?"))
        author = m.get("author", "?")
        kind = m.get("kind", "")
        if kind == "at_mention":
            desc = f"@mention by {author}"
        else:
            desc = f"reply to your thread by {author}"
        parts.append(f'#{tid} "{title}" ({desc})')

    summary = ", ".join(parts)
    if rest > 0:
        summary += f", +{rest} more"

    total = len(mentions)
    noun = "mention" if total == 1 else "mentions"
    return (
        f"\U0001f514 {total} forum {noun} waiting on you: {summary}"
        f" — `forum read <id>` to clear."
    )


def _format_umwelt_advisory_line(mentions: list) -> Optional[str]:
    """Format the 📪 comms_surface=umwelt anti-black-hole advisory line.

    alpha#2019 slice A: under comms_surface=umwelt, the ordinary 🔔/📢
    hook-surface lines are suppressed (the umwelt plugin's own hooks
    already surface world state). But an ADDRESSED forum item (an
    at_mention -- a broadcast-only new-thread has no addressee) must never
    silently vanish pre-bridge (umwelt#599 slice 4), so this single
    compact line fires whenever pending addressed items exist. Returns
    None when there are none (broadcast-only counts are fully suppressed
    under umwelt -- no line at all in that case).
    """
    if not mentions:
        return None
    total = len(mentions)
    noun = "item" if total == 1 else "items"
    return (
        f"\U0001f4ea comms_surface=umwelt: {total} addressed forum {noun} exist; "
        f"the mention bridge is not yet live — `forum read <id>` to collect "
        f"(alpha#2019/umwelt#599 slice 4 will bridge these)."
    )


def _assemble_response(head_blocks: list, body_blocks: list) -> dict:
    """Build the UserPromptSubmit response dict from this prompt's fired
    head/body blocks, routed through the shared bounded renderer
    `_hooklib.emit_context()` (engram-alpha#2863 WS2).

    `head_blocks` / `body_blocks` are the already-rendered, non-empty block
    strings `main()` collected this prompt (see the "Build injection block"
    section below) -- this function does no filtering of its own beyond the
    "did anything fire at all" check. Split out from `main()` purely for
    testability, mirroring engram-surface-hook.py's own `_assemble_response()`
    (#2876) -- `main()`'s own behavior is otherwise unchanged.

    Census (this hook, unlike surface-hook's ~11 blocks): only 3 possible
    blocks total, across two branches main()'s own comms_surface gate keeps
    mutually exclusive within a single invocation:
      - comms_surface="forum" (default): mention_line (head) + threads_line
        (body) -- mention_line is the single actionable/addressed-item fact
        for this prompt (an @mention), threads_line the lower-priority
        broadcast-only new-threads count.
      - comms_surface="umwelt": the addressed-item advisory line (head) only
        -- conceptually the same "actionable fact" role as mention_line
        above, just this branch's own equivalent wording.
    So head is always at most one block; body is at most one block, and only
    ever fires in forum mode.

    Returns `{}` when nothing fired this prompt. In practice this call site
    never reaches that branch -- main()'s own pre-existing silent-no-op exits
    (three separate bare `sys.exit(0)` checks: forum-not-configured,
    advisory_line is None, threads-and-mention_line both absent) all run
    BEFORE this function is ever called, preserving the exact pre-migration
    behavior of emitting NO stdout at all (not even `{}`) on those paths --
    unlike engram-surface-hook.py, which always prints something. The `{}`
    branch here is defensive belt-and-suspenders only, matching the shared
    `_assemble_response()` contract established by #2876 for consistency;
    `emit_context()` is never called with two empty inputs either way.
    """
    head = "\n\n".join(head_blocks)
    body = list(body_blocks)
    if not head and not body:
        return {}

    harness = detect_harness()
    budgets = get_harness_budgets(harness)
    rendered = emit_context(
        head,
        body,
        budget=budgets["budget"],
        # DELIBERATE DEVIATION from engram-surface-hook.py's own #2876 choice
        # (target=budgets["budget"], the hard ceiling): here target is
        # budgets["head_budget"] instead. Reasoning: this hook's head
        # (mention_line, or its umwelt-mode equivalent) embeds real
        # user-authored thread titles -- genuinely unbounded content, unlike
        # surface-hook's head, which is built entirely from fixed
        # system-health strings and stays under head_budget only because
        # those three messages happen to be short today (documented there as
        # a canary, not a guarantee). Passing target=head_budget makes
        # emit_context()'s own Stage-1 line-boundary trim (see its docstring)
        # do the bounding for us, using the SAME tested mechanism the
        # tripwire hook's own local economy target relies on (_harness.py's
        # own docstring names this exact precedent: "its own soft target of
        # 1800 happens to equal claude-code's head_budget here"). This
        # hook's normal content (max delivered historically: 608 chars, per
        # the #2863 census) is nowhere near either ceiling, so no real
        # content is lost by choosing the smaller target -- it only
        # tightens the worst-case guarantee. (Also see _truncate_title()'s
        # own defensive cap on individual titles -- belt-and-suspenders
        # with this target choice, not a substitute for it: together they
        # make the "head stays under head_budget" property a real
        # invariant here, not a canary.)
        target=budgets["head_budget"],
        # No single block in this hook is known to run large enough to need
        # the allow_single_overflow exception (unlike surface-hook's
        # principle_triggers) -- left at the default (False).
        allow_single_overflow=False,
        # No shared overflow-persistence helper exists yet in _hooklib.py
        # (see engram-surface-hook.py's own #2876 note) -- cut content is
        # dropped with a "M cut" count and no "-- see <path>" pointer.
        overflow_writer=None,
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": rendered,
        }
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # ── Prologue-lib gate (gh#1680 slice 1) ───────────────────────────────────
    # If _prompthooklib somehow failed to import, _load_config/_get_agent_name
    # are unavailable -- degrade to a silent no-op, matching every other
    # best-effort import in this hook's prologue.
    if not _PROMPTHOOKLIB_OK:
        sys.exit(0)

    # ── Config ────────────────────────────────────────────────────────────────
    config = _load_config()

    # ── Silent no-op gate ────────────────────────────────────────────────────
    # If forum isn't configured (neither url nor agent_name in config), skip.
    if not _is_forum_configured(config):
        sys.exit(0)

    agent_name = _get_agent_name(config)
    forum_url = _get_forum_url(config)
    comms_surface = _get_comms_surface(config)

    # ── Read surfaced cursor ──────────────────────────────────────────────────
    surfaced_cursor = _read_cursor(SURFACED_CURSOR_PATH)
    since_str = surfaced_cursor.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if surfaced_cursor else None

    # Mention state no longer uses a local read cursor. The inbox endpoint
    # (GET /api/agent/<name>/inbox) uses server-side per-thread read marks, so
    # "reading a thread clears the mention" is handled by the server. No local
    # cursor read needed here.

    # ── Fetch threads + inbox mentions (with cache + stale-cache fallback) ────
    threads: Optional[list] = None
    mentions: Optional[list] = None

    cached = _load_cache()
    if cached is not None:
        # Cache is fresh — display it, but do NOT advance the surfaced cursor.
        # The live fetch that populated this cache already advanced the cursor
        # to its fetch time. Advancing again to `now` here would step the
        # cursor past posts made AFTER that fetch but before cache-expiry, so
        # the next live fetch's since=cursor would exclude them and they'd
        # never surface. (Twin of the unreachable-no-cache guard below.)
        threads = cached.get("threads", [])
        # Backward-compat: old cache files have no "mentions" key → default [].
        mentions = cached.get("mentions", [])
    else:
        # Live fetch with since=current surfaced cursor.
        threads = _fetch_threads(forum_url, since_str, agent_name)
        if threads is not None:
            # Also fetch inbox mentions (advisory; failure → None, not a blocker).
            # Filtered to kind=at_mention inside _fetch_inbox.
            if agent_name:
                mentions = _fetch_inbox(forum_url, agent_name)
            else:
                mentions = None
            _save_cache(threads, mentions if mentions is not None else [])
            # Only here did we query the server with the current cursor, so
            # only here is it safe to advance: everything up to `now` is now
            # covered by a real since=cursor query.
            now = datetime.now(timezone.utc)
            _write_cursor(SURFACED_CURSOR_PATH, now)
        else:
            # Server unreachable — stale-cache fallback (any age); do NOT
            # advance, so the next reachable query re-checks the same window.
            try:
                raw = Path(HOOK_CACHE_PATH).read_text()
                stale = json.loads(raw)
                threads = stale.get("threads", [])
                mentions = stale.get("mentions", [])
            except (OSError, json.JSONDecodeError):
                threads = None
                mentions = None

    # ── Build injection block ─────────────────────────────────────────────────
    # Mention line (higher priority) goes ABOVE the generic threads line.
    mention_line = _format_mention_line(mentions or [])

    # ── Auto-publish derived status (Q1–Q4, spec #1077 part 1) ───────────────
    # This block runs AFTER the fetch/format above but BEFORE the final output.
    # Its success/failure must NEVER gate the existing thread/mention output.
    try:
        if derive_own_status is not None and agent_name:
            _now = time.time()

            # Step 2: derive status from local signals.
            state, activity, queue, cadence = derive_own_status(agent_name)

            # Step 3: apply Q4 sleep guard (restores cadence too — see fn doc).
            last_pub = _load_publish_state()
            state, activity, queue, cadence = _apply_sleep_guard(
                state, activity, queue, cadence, last_pub, _now
            )

            # Step 4: compute throttle interval.
            throttle = max(cadence or 0, 60)

            # Step 5: decide whether to publish.
            current_tuple = (state, activity, tuple(queue), cadence)
            if _should_publish(last_pub, current_tuple, _now, throttle):
                # Step 6: POST to /api/agents/status with timeout=PUBLISH_TIMEOUT_SECONDS.
                # Only on success, write the publish state file.
                payload: dict = {
                    "agent": agent_name,
                    "state": state,
                    "activity": activity,
                    "queue": queue,
                }
                # cadence None → omit (server uses its global window);
                # 0 (on-call) or positive → include (mirror cmd_status_auto).
                if cadence is not None:
                    payload["expected_republish_seconds"] = cadence

                post_data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    f"{forum_url}/api/agents/status",
                    data=post_data,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    method="POST",
                )
                try:
                    with _ipv4_only_urlopen(req, timeout=PUBLISH_TIMEOUT_SECONDS) as resp:
                        resp.read()  # consume response body; discard result
                    # POST succeeded — advance the throttle timestamp.
                    _save_publish_state(state, activity, queue, cadence, _now)
                except Exception:
                    # POST failed — do NOT advance the timestamp so the next
                    # prompt retries.
                    pass
    except Exception:
        # Whole publish block raised — swallow completely. The existing
        # thread/mention output is unaffected.
        pass

    # ── comms_surface gate (alpha#2019 slice A) ───────────────────────────────
    # Under "umwelt", the ordinary 🔔/📢 lines are suppressed (the umwelt
    # plugin's own hooks already surface world state) EXCEPT a single
    # compact advisory when pending ADDRESSED items (mentions) exist -- the
    # anti-black-hole clause: a world-preferring seat must never silently
    # miss an addressed forum item pre-bridge. Broadcast-only counts (new
    # threads, no mention) are fully suppressed under umwelt.
    #
    # engram-alpha#2863 WS2: head_blocks/body_blocks split, routed through
    # _assemble_response()/emit_context() below -- preserves the exact
    # existing branching for what fires this prompt (including the three
    # pre-existing bare `sys.exit(0)` silent-no-op exits below, untouched);
    # only WHERE the resulting string goes has changed. head_blocks carries
    # the single actionable/addressed-item fact for this prompt (an
    # @mention, or its umwelt-mode equivalent); body_blocks carries the
    # lower-priority broadcast-only new-threads count, forum mode only.
    head_blocks: list = []
    body_blocks: list = []
    if comms_surface == "umwelt":
        advisory_line = _format_umwelt_advisory_line(mentions or [])
        if advisory_line is None:
            sys.exit(0)
        head_blocks = [advisory_line]
    else:
        # ── Exit silently if nothing to report ──────────────────────────────
        if not threads and not mention_line:
            sys.exit(0)

        # ── Count new threads since the surfaced cursor ─────────────────────
        new_count = len(threads) if threads else 0

        # 🔔 mention line first (if any) -- head: the actionable fact.
        if mention_line:
            head_blocks.append(mention_line)

        # 📢 generic threads line (if any) -- body: broadcast-only, lower priority.
        if new_count > 0:
            post_word = "post" if new_count == 1 else "posts"
            body_blocks.append(
                f"\U0001f4e2 {new_count} new forum {post_word} since last read"
                f" — run `forum read <id>` or `forum status` to catch up."
            )

        if not head_blocks and not body_blocks:
            sys.exit(0)

    response = _assemble_response(head_blocks, body_blocks)
    if not response:
        # Defensive only -- see _assemble_response()'s own docstring on why
        # this call site never actually reaches its `{}` branch (the three
        # sys.exit(0) checks above already cover every "nothing to report"
        # case with the pre-migration silent-no-op behavior, no print at all).
        sys.exit(0)

    print(json.dumps(response))


if __name__ == "__main__":
    main()
