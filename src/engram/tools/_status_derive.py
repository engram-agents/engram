"""_status_derive — stdlib-only derivation of agent status from local signals.

Shared between tools/forum.py (CLI) and the forum prompt hook so both use the
same logic without coupling the hook to the full forum.py module.

This module is intentionally stdlib-only — no third-party deps, no forum.py
import-back. The hook is critical-path on every user prompt; a heavy dependency
tree here would add startup latency and failure surface.

#1608: ``_held_baton_turns`` (via the private ``_fetch_held_batons`` helper)
reads the live forum coordination API (``GET /api/projects``) instead of
globbing the local ``PROJECTS_DIR`` — the local ``projects/*.md`` files went
dead at the 2026-06-27 UCS cutover, when ``baton.py`` moved to writing
exclusively through the forum API. The HTTP call is ``urllib``-only (stdlib,
no ``forum.py`` import-back) and fails soft (empty queue) on any error, per
this module's existing never-break-a-wake contract.

Exported surface
----------------
derive_own_status(agent_name, ...)
    → (state, activity, queue, expected_republish_seconds)

_read_loop_mode()
    → Optional[dict]

_held_baton_turns(agent_name)
    → list[str]

_recently_engaged()
    → bool

_read_engaged_window()
    → int

Constants
---------
LOOP_MODE_PATH, PROJECTS_DIR, ON_CALL_SENTINEL, _UNSET, ENGRAM_HOME,
LAST_USER_ACTIVITY_PATH
"""

import json
import os
import time
import urllib.request
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

# last-user-activity — epoch-seconds stamp written by the time-bar hook on
# genuine human-typed prompts. Used to derive "engaged" state.
LAST_USER_ACTIVITY_PATH: str = os.path.join(ENGRAM_HOME, "last-user-activity")

# Shared baton/project dir — turn-state files (one .md per project/PR).
# #1608: no longer read by _held_baton_turns (dead since the 2026-06-27 UCS
# cutover — see the module docstring). Kept as a constant only for back-compat:
# tools/forum.py still imports it, and existing tests monkeypatch it.
PROJECTS_DIR: str = (
    os.environ.get("ENGRAM_PROJECTS_DIR") or "/home/agents-shared/projects"
)

# Default forum server URL — mirrors tools/forum.py::_DEFAULT_FORUM_URL.
_DEFAULT_FORUM_URL: str = "http://localhost:5002"

# Short timeout so an unreachable forum never stalls the critical-path hook.
_FORUM_REQUEST_TIMEOUT_SECONDS: float = 2.0

# Sentinel for "event-driven / monitor-only" cadence (no heartbeat).
# Matches forum.db.ON_CALL_SENTINEL — the server renders such an agent
# 'on-call' when quiet rather than flapping it offline.
ON_CALL_SENTINEL: int = 0

# Distinguishes "no override given" from an explicit None.
_UNSET = object()

# Default engaged window in seconds (6 minutes). Overridable via
# cadence.engaged_window_seconds in config.json.
_DEFAULT_ENGAGED_WINDOW: int = 360

# Loop-gate constants (loop_gate_decision).
_LOOP_GATE_MIN_DEFER: int = 60   # never re-arm shorter than this on a defer
_LOOP_GATE_MARGIN: int = 30      # seconds added to remaining for next-wake timing


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


def _load_config() -> dict:
    """Load $ENGRAM_HOME/config.json. Returns {} on any failure.

    Re-implemented here (not imported from tools/forum.py) to keep this module
    stdlib-only / import-back-free — see the module docstring. Mirrors
    tools/forum.py::_load_config exactly.
    """
    config_path = os.path.join(ENGRAM_HOME, "config.json")
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _resolve_forum_url() -> str:
    """Resolve the forum server base URL.

    Priority mirrors tools/forum.py::_resolve_forum_url (re-implemented here,
    not imported — see the module docstring):
      1. config.json["forum"]["url"]
      2. $FORUM_URL env var
      3. default http://localhost:5002

    Not process-cached (unlike forum.py's version): this module's callers are
    hooks/CLIs that invoke it at most a couple of times per short-lived
    process, so the simplicity of a fresh resolve outweighs the marginal
    saved file read, and it sidesteps any shared-mutable-state test surface.
    """
    config = _load_config()
    forum_cfg = config.get("forum")
    if isinstance(forum_cfg, dict):
        url = (forum_cfg.get("url") or "").strip()
        if url:
            return url.rstrip("/")

    env_url = os.environ.get("FORUM_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")

    return _DEFAULT_FORUM_URL


def _fetch_held_batons(agent_name: str) -> list:
    """Return live coordination-store project records whose ``turn:`` is this agent.

    #1608: the single network call backing both ``_held_baton_turns`` (the
    published queue, ids only) and ``derive_own_status``'s activity fallback
    (F2) — both derive from this ONE fetch so a single ``derive_own_status()``
    call never issues more than one HTTP request. Hits the same live source
    ``GET /api/projects`` already serves (``active_only=true`` — terminal/closed
    batons are excluded server-side, so a merged PR can never linger on the
    queue the way the pre-#1608 dead local files did).

    Returns a list of raw dicts (``project_id``, ``title``, ``turn_reason``,
    ``turn_since``, ``status``, ``seq``, ...) in the API's response order
    (server-sorted oldest-turn_since-first — see
    ``forum.coordination.store_file.FileStore.read_projects``), filtered to
    ``turn == agent_name`` (case-insensitive).

    Fail-soft, MANDATORY: any failure (forum unreachable, non-200, malformed
    JSON, unexpected shape) returns ``[]`` rather than raising. This function
    is on the critical path of every user-prompt hook (via
    ``derive_own_status``) and must never break a wake over a forum hiccup —
    mirrors the module's existing fail-soft house style (see
    ``_read_loop_mode``, ``_recently_engaged``).
    """
    if not agent_name:
        return []
    agent_name = agent_name.strip().lower()
    if not agent_name:
        return []

    try:
        base_url = _resolve_forum_url()
        url = f"{base_url}/api/projects?active_only=true"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_FORUM_REQUEST_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return []
        projects = data.get("projects")
        if not isinstance(projects, list):
            return []

        held: list = []
        for item in projects:
            if not isinstance(item, dict):
                continue
            turn = str(item.get("turn") or "").strip().lower()
            if turn != agent_name:
                continue
            held.append(item)
        return held
    except Exception:
        return []


def _held_baton_turns(agent_name: str) -> list:
    """Return project ids of live coordination-store batons whose ``turn:`` is
    this agent.

    A baton in your court = active work you own → it both reinforces 'working'
    and lands on your published queue so peers see your load. #1608: sourced
    from the live forum API (``_fetch_held_batons``) rather than a local-file
    glob — see the module docstring. Same return shape as before
    (``list[str]``), so callers are unaffected by the source change.
    """
    return [
        str(b["project_id"])
        for b in _fetch_held_batons(agent_name)
        if b.get("project_id")
    ]


def _read_engaged_window() -> int:
    """Return engaged_window_seconds from config.json (cadence.engaged_window_seconds).

    Default 360 (6 minutes). Any read/parse error → default. Stdlib-only.
    """
    try:
        config_path = os.path.join(ENGRAM_HOME, "config.json")
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            return _DEFAULT_ENGAGED_WINDOW
        cadence = cfg.get("cadence")
        if not isinstance(cadence, dict):
            return _DEFAULT_ENGAGED_WINDOW
        val = cadence.get("engaged_window_seconds")
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    except Exception:
        pass
    return _DEFAULT_ENGAGED_WINDOW


def _recently_engaged() -> bool:
    """True if last-user-activity stamp is within the engaged window of now.

    Fail-open: missing / unparseable / future-dated stamp → False (never
    stuck-engaged). Window read from config.json cadence.engaged_window_seconds,
    default 360 seconds.
    """
    try:
        with open(LAST_USER_ACTIVITY_PATH, encoding="utf-8") as fh:
            raw = fh.read().strip()
        stamp = float(raw)
        now = time.time()
        age = now - stamp
        # Future-dated stamp (age < 0) → fail-open, not engaged.
        if age < 0:
            return False
        window = _read_engaged_window()
        return age <= window
    except Exception:
        return False


def loop_gate_decision() -> tuple:
    """Decide whether an autonomous loop-wake should run now or re-defer.

    Returns (decision, defer_seconds, reason):
      - ("defer", N, "presence-override-user") when an explicit user-mode override
        is active (highest priority; suspends loop by deliberate user choice).
      - ("defer", N, "presence-derived-user") when derived presence mode is user
        (Lei active within the cooldown window).
      - ("proceed", 0, "presence-override-auto") when an explicit auto-mode override
        is active — this beats the engaged-defer, letting the loop proceed even when
        a recent stamp would otherwise trigger engaged-defer.
      - ("defer", N, "user-engaged") when no presence signal and a recent human
        prompt is within the engaged window (the original #1469 path, unchanged).
      - ("proceed", 0, <reason>) otherwise (not engaged, OR any error).

    Resolution order: presence override-user → presence derived-user →
    presence override-auto → derived-auto / no presence → #1469 engaged-defer.

    FAIL-OPEN to proceed: the loop's liveness must NEVER depend on this gate. Any
    failure in the presence layer falls through to the existing engaged-defer path;
    any failure there returns ("proceed", 0, ...). A broken gate lets the loop run
    (strictly no worse than today's no-gate behavior); it must never deadlock it.
    """
    # Presence consultation — must be inside the outer try/except so any presence
    # bug falls through to the existing #1469 engaged-defer path unchanged.
    # Lazy + guarded import: presence is stdlib-only and does NOT import
    # _status_derive, so there is no circular import.  Absent or erroring presence
    # → fall through to the existing engaged-defer logic below (fail-open).
    try:
        import presence  # noqa: PLC0415
        override = presence.active_override()       # None | {"mode","expires_at",...}
        _mode, bundle = presence.get_mode(override=override)
        if bundle.get("loop_suspended"):            # loop_suspended is True only for the user bundle (2-state spine)
            cooldown = presence.cooldown_seconds()
            if override and override.get("mode") == "user":
                exp = override.get("expires_at")
                if exp:
                    remaining = presence.parse_iso(exp) - time.time()
                    defer = int(max(_LOOP_GATE_MIN_DEFER, min(remaining + _LOOP_GATE_MARGIN, cooldown)))
                else:
                    defer = cooldown                # no-expiry override → bounded re-check
                return ("defer", defer, "presence-override-user")
            # derived user (recent activity within cooldown)
            try:
                with open(LAST_USER_ACTIVITY_PATH, encoding="utf-8") as fh:
                    stamp = float(fh.read().strip())
                remaining = (stamp + cooldown) - time.time()
                defer = int(max(_LOOP_GATE_MIN_DEFER, min(remaining + _LOOP_GATE_MARGIN, cooldown)))
            except Exception:
                defer = cooldown
            return ("defer", defer, "presence-derived-user")
        if override and override.get("mode") == "auto":
            return ("proceed", 0, "presence-override-auto")  # explicit auto beats engaged-defer
        # derived-auto → fall through to the existing #1469 engaged-defer below
    except Exception:
        pass  # presence unavailable / any error → existing engaged-defer path

    # --- existing #1469 engaged-defer logic UNCHANGED below this line ---
    try:
        if not _recently_engaged():
            return ("proceed", 0, "not-engaged")

        # Engaged — compute how long to re-defer.
        try:
            with open(LAST_USER_ACTIVITY_PATH, encoding="utf-8") as fh:
                raw = fh.read().strip()
            stamp = float(raw)
            now = time.time()
            window = _read_engaged_window()
            remaining = (stamp + window) - now
            if remaining <= 0:
                # Window just lapsed between the two reads — proceed.
                return ("proceed", 0, "not-engaged")
            defer_raw = remaining + _LOOP_GATE_MARGIN
            defer = int(max(_LOOP_GATE_MIN_DEFER, min(defer_raw, window)))
            return ("defer", defer, "user-engaged")
        except Exception:
            # Race or read failure after _recently_engaged() was True.
            # Full-window defer is the safe conservative choice.
            # Use the module constant directly — calling _read_engaged_window()
            # here could itself raise, escaping to the outer except and returning
            # ("proceed", 0, "gate-error"), which defeats the engaged→defer
            # guarantee this branch exists to enforce.
            window = _DEFAULT_ENGAGED_WINDOW
            return ("defer", window, "user-engaged-fallback")
    except Exception:
        return ("proceed", 0, "gate-error")


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
    agent's own local signals — loop-mode.json + held batons + last-user-activity.

    This is the client-side derivation the board's architecture depends on
    (the server can't read a cross-host agent's filesystem). Rules:

    - **state**: ``override_state`` wins; else 'engaged' if a recent human-typed
      prompt was detected (within cadence.engaged_window_seconds, default 360s);
      else 'working' if a loop marker is present OR any baton turn is held;
      else 'idle'.
    - **cadence** (#1035): ``on_call`` → ON_CALL_SENTINEL (0, event-driven);
      else ``override_cadence`` if given; else from loop-mode.json
      (``pacer == 'monitor'`` or ``cadence_seconds == 0`` → sentinel; a positive
      ``cadence_seconds`` → that; otherwise None = server's global window).
    - **activity**: ``override_activity`` wins; else the loop marker's ``topic``
      (truncated); else (#1608, F2) the top held baton's ``title``/``turn_reason``
      when there's no loop marker (a real signal even when idle-but-holding-a-
      baton); else None.
    - **queue**: the held baton ids.
    """
    loop = _read_loop_mode()
    # #1608: ONE fetch backs both `batons` (the queue) and the activity fallback
    # below — _fetch_held_batons is the single HTTP call; _held_baton_turns is
    # not used here so a derive_own_status() call never issues a second request.
    held_records = _fetch_held_batons(agent_name)
    batons = [str(b["project_id"]) for b in held_records if b.get("project_id")]

    # state
    if override_state is not None:
        state = override_state
    else:
        # Consult presence mode. The import is lazy + try/except-guarded because
        # presence is an OPTIONAL surface — it's stdlib-only and self-contained
        # (it does NOT import _status_derive, so there is no circular import to
        # avoid). Lazy + guarded means that if presence is absent or errors, we
        # fail-open to the existing engaged/working/idle logic below. Fail-open to
        # "auto" on any error — a broken presence layer must not suppress the loop
        # by silently staying in "user".
        _presence_mode = "auto"
        try:
            import presence as _presence_mod  # noqa: PLC0415
            _presence_mode, _ = _presence_mod.get_mode()
        except Exception:
            _presence_mode = "auto"

        # Both presence mode=user (cooldown window) and _recently_engaged
        # (engaged_window) map to the "engaged" display state, so the effective
        # engaged-display threshold is max(cooldown_seconds, engaged_window_seconds).
        if _presence_mode == "user":
            state = "engaged"
        elif _recently_engaged():
            state = "engaged"
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
    elif held_records:
        # (#1608, F2) No loop marker, but a baton is in your court — surface it
        # as the activity signal rather than falling through to None. "Top" =
        # held_records[0], the API's own ordering (oldest turn_since first —
        # the baton you've been sitting on turn longest). Prefer title (the
        # work item's stable identity) over turn_reason (the last flip's
        # rationale, which may be terse/stale); fall back to turn_reason only
        # when title is blank.
        _top = held_records[0]
        _source = str(_top.get("title") or "").strip() or str(_top.get("turn_reason") or "").strip()
        activity = _source[:120] if _source else None
    else:
        activity = None

    return state, activity, batons, cadence
