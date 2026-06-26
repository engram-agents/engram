"""presence — 2-state mode spine (user/auto) for presence-aware agent behavior.

The SSoT is ``MODE_BUNDLES``: the single table that maps mode name → operational
knobs.  A mode switch always produces a consistent bundle — no partial applies.

States
------
``user``  — human is present; loop suspended, feed narrowed.
``auto``  — autonomous; loop runs, full feed.

Resolution order (get_mode)
---------------------------
1. Explicit override (``~/.engram/presence-mode.json``) if present and not expired.
2. Derived: ``user`` if ``last-user-activity`` is within the cooldown window; else ``auto``.
3. Fail-open to ``auto`` on any error — a broken presence layer must never trap
   the agent in ``user`` (loop suspended, feed muted).  Same discipline as the
   #1469 loop-gate: degrade toward more autonomy, never less.

Stdlib-only — no third-party deps.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# The ONE definition — mode → operational knobs. (2-state spine; flavors later.)
# ---------------------------------------------------------------------------

MODE_BUNDLES: dict = {
    "user": {
        "feed": "direct-only",
        "status": "with user — focused",
        "loop_suspended": True,
    },
    "auto": {
        "feed": "all",
        "status": "auto",
        "loop_suspended": False,
    },
}

# ~10 min starting value (design §6); tunable via config.json cadence.cooldown_seconds.
_DEFAULT_COOLDOWN_SECONDS: int = 600

# ---------------------------------------------------------------------------
# Paths — mirrors _status_derive.py pattern exactly (both derive from ENGRAM_HOME).
# ---------------------------------------------------------------------------

ENGRAM_HOME: str = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

# The same last-user-activity stamp that _status_derive._recently_engaged() reads.
# Defined independently (not imported from _status_derive) to keep this module
# self-contained and to allow per-module monkeypatching in tests.
LAST_USER_ACTIVITY_PATH: str = os.path.join(ENGRAM_HOME, "last-user-activity")

# Explicit mode override file.
PRESENCE_MODE_PATH: str = os.path.join(ENGRAM_HOME, "presence-mode.json")


# Distinguishes "no override given" from an explicit None passed to get_mode().
_UNSET = object()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def parse_iso(iso_str: str) -> float:
    """Parse an ISO 8601 string to a POSIX float. Raises on error."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _read_cooldown_seconds() -> int:
    """Return cooldown_seconds from config.json (cadence.cooldown_seconds).

    Default _DEFAULT_COOLDOWN_SECONDS (600).  Any read/parse error → default.
    Mirrors _status_derive._read_engaged_window exactly, different key.
    """
    try:
        config_path = os.path.join(ENGRAM_HOME, "config.json")
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            return _DEFAULT_COOLDOWN_SECONDS
        cadence = cfg.get("cadence")
        if not isinstance(cadence, dict):
            return _DEFAULT_COOLDOWN_SECONDS
        val = cadence.get("cooldown_seconds")
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    except Exception:
        pass
    return _DEFAULT_COOLDOWN_SECONDS


def _derive_mode() -> str:
    """Derive mode purely from the last-user-activity stamp.

    Returns ``'user'`` if the stamp is within the cooldown window; ``'auto'``
    otherwise.  Fail-open to ``'auto'`` on any error (missing stamp, parse
    failure, future-dated stamp).
    """
    try:
        with open(LAST_USER_ACTIVITY_PATH, encoding="utf-8") as fh:
            raw = fh.read().strip()
        stamp = float(raw)
        now = time.time()
        age = now - stamp
        # Future-dated stamp → fail-open (never stuck in 'user').
        if age < 0:
            return "auto"
        cooldown = _read_cooldown_seconds()
        return "user" if age <= cooldown else "auto"
    except Exception:
        return "auto"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def active_override() -> Optional[dict]:
    """Return the parsed override dict if a non-expired explicit override is active.

    Returns the full dict (``{"mode", "since", "expires_at", ...}``) when
    ``~/.engram/presence-mode.json`` exists, has a known mode, and is not
    expired.  ``expires_at=None`` means permanent (no expiry) → valid.
    Any error (missing file, corrupt JSON, unknown mode, parse failure,
    expired TTL) → ``None``.  Fail-open to None — same spirit as get_mode's
    fail-open-to-auto: a broken presence layer must never deadlock a caller.
    """
    try:
        with open(PRESENCE_MODE_PATH, encoding="utf-8") as fh:
            override = json.load(fh)
        if not isinstance(override, dict):
            return None
        if override.get("mode") not in MODE_BUNDLES:
            return None
        expires_at = override.get("expires_at")
        if expires_at is None:
            return override  # permanent override — valid indefinitely
        if time.time() < parse_iso(expires_at):
            return override  # not yet expired — valid
        return None  # expired
    except Exception:
        return None


def cooldown_seconds() -> int:
    """Return the configured cooldown_seconds (public wrapper of _read_cooldown_seconds).

    Lets callers (e.g. ``_status_derive.loop_gate_decision``) read the cooldown
    without reaching into a private name.
    """
    return _read_cooldown_seconds()


def get_mode(override=_UNSET) -> tuple:
    """Return ``(mode_name, resolved_bundle)``.

    Parameters
    ----------
    override:
        If not supplied (default ``_UNSET``), ``active_override()`` is called
        to read the current explicit override — the normal path for callers that
        have not already fetched it.  If supplied, that value is used directly
        (dict → override path; ``None`` → derived path), avoiding a second
        ``active_override()`` read per call.  This is the only reason to pass
        the argument; existing callers that pass nothing are unaffected.

    Resolution order:
    1. Explicit override (``override`` arg or ``active_override()`` result) if a
       non-``None`` dict with a known mode is provided.
    2. Derived from ``last-user-activity`` stamp vs cooldown window.
    3. Fail-open to ``("auto", MODE_BUNDLES["auto"])`` on any error.

    The returned bundle is always ``MODE_BUNDLES[mode_name]`` — one table,
    consistent-by-construction.
    """
    if override is _UNSET:
        override = active_override()
    if override is not None:
        mode = override["mode"]
        return (mode, MODE_BUNDLES[mode])

    # Derived path.
    mode = _derive_mode()
    return (mode, MODE_BUNDLES[mode])


def set_mode(name: str, *, ttl_seconds: Optional[int] = None) -> None:
    """Write an explicit mode override to ``~/.engram/presence-mode.json``.

    Parameters
    ----------
    name:
        Must be a key in MODE_BUNDLES (``'user'`` or ``'auto'``).
    ttl_seconds:
        If given, the override expires this many seconds from now.
        If None, the override is permanent until ``clear_mode()`` is called.

    Raises ValueError for unknown mode names.
    Uses tmp + os.replace for atomicity.
    """
    if name not in MODE_BUNDLES:
        raise ValueError(
            f"Unknown mode {name!r}; must be one of {sorted(MODE_BUNDLES)}"
        )
    now = datetime.now(tz=timezone.utc)
    expires_at: Optional[str] = None
    if ttl_seconds is not None:
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()

    payload = {
        "mode": name,
        "since": now.isoformat(),
        "expires_at": expires_at,
    }

    tmp_path = PRESENCE_MODE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp_path, PRESENCE_MODE_PATH)


def clear_mode() -> None:
    """Remove the explicit mode override (revert to derived mode).

    Idempotent — missing override file is a no-op.
    """
    try:
        os.remove(PRESENCE_MODE_PATH)
    except FileNotFoundError:
        pass
