#!/usr/bin/env python3
"""UserPromptSubmit hook: surface unread GitHub notifications with delta semantics.

On every prompt, runs `gh api notifications` and injects a summary of new and
still-unread notifications into the session context.

Feature-gated: the hook returns silently unless config["github"]["enabled"]
is true. Set github.enabled: true in ~/.engram/config.json to activate.
No API calls are made when the feature is disabled.

Four improvements over the original local-only hook (2026-05-22):

1. Filter self-author noise
   Fetches the actor (user.login) on the latest comment/event for each
   notification and skips it when actor == self_login. This eliminates the
   "reason: author" fire on own commits/comments without discarding cases
   where someone else comments on a thread we authored (which IS actionable).
   Actor lookups are cached in ~/.engram/github-notifications-actor-cache.json
   (keyed by thread URL -> actor login + timestamp; entries expire after 24h).

2. Delta semantics ("N new since last prompt")
   Cursor at ~/.engram/github-notifications-cursor.txt (mirrors the
   inter-agent hook pattern). Output leads with "N new since last prompt";
   older still-unread notifications get a count-only line at the bottom.
   Cursor advances on every hook fire (even if zero new items).

3. Config-driven 3-tier classification (collaborator / peer agents / outside)
   Reads github.primary_user_login and github.counterparts_logins from
   ~/.engram/config.json. Renders collaborator notifications first (most
   prominent), peer-agent second, and outside as a count-only line (collapsed
   by default to reduce noise from testers/bots). Self is filtered out.

   Tier definitions:
     - Tier 1 (collaborator): the human user this agent works with.
       Configured as config["github"]["primary_user_login"] (str).
     - Tier 2 (peer agents): same-install collaborating agents.
       Configured as config["github"]["counterparts_logins"] (list of str).
     - Tier 3 (outside): everyone else (other agents, other humans, bots).
     - Self-filter: actor == self_login is discarded silently.

   Backward-compat: if both optional fields are absent (but github.enabled
   is true), falls back to flat 2-tier rendering (self vs other) with a
   one-line config-prompt note at the top. If only one field is present,
   missing tier collapses to outside.

4. Outside actors rendered as count-only
   Tier-3 notifications are shown as "(K from outside — use `gh api
   notifications` to inspect)" to keep output concise. Expand if needed.

CI-noise filtering: skips reason=ci_activity (same as original hook).
Graceful: falls silent on any error; never blocks the prompt.
Timeout budget: 5s total (hook call); per-fetch cap 1s; cold-cache fetch
  cap is 15 notifications (overflow rendered conservatively as outside).
Output budget: <=30 lines max (5 collaborator + 5 peer + headers + outside count
+ older count + config-prompt-note if any). Heavier deltas are capped per tier
with overflow counts.

Built 2026-05-23. Round-2 (2026-05-23): replaced hardcoded KNOWN_AGENT_LOGINS
with config-driven tier logic per Lei's feedback (Aleph belongs to Lin's
install, not Borges's; hardcoding is alpha-distribution broken).
Round-3 (2026-05-23): case-insensitive GH login matching + per-tier render caps
+ removed dead config_complete variable + README case-sensitivity note.
Round-5 (2026-05-23): nested config under github.* + github.enabled section
gate (Ari fresh-eye on PR #313).
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ENGRAM_HOME = os.environ.get("ENGRAM_HOME", str(Path.home() / ".engram"))

# Cursor: tracks timestamp of last hook fire for delta semantics
CURSOR_PATH = os.path.join(ENGRAM_HOME, "github-notifications-cursor.txt")

# Actor cache: avoids redundant API calls across hook fires
ACTOR_CACHE_PATH = os.path.join(ENGRAM_HOME, "github-notifications-actor-cache.json")

# Self-login cache: stable, written once on first fire
SELF_LOGIN_PATH = os.path.join(ENGRAM_HOME, "github-self-login.txt")

# Reasons we explicitly skip (CI noise).
NOISE_REASONS: set[str] = {"ci_activity"}

# Max notifications to do cold-cache actor fetches for.
# Overflow notifications are classified conservatively as outside.
ACTOR_FETCH_CAP = 15

# Actor cache TTL: 24 hours
ACTOR_CACHE_TTL_HOURS = 24

# Per-tier render caps: line-by-line items shown before overflow count kicks in.
# Collaborator and peer tiers: 5 items each.
# Flat fallback (config-empty): 8 items.
# Outside tier is always count-only (no cap needed).
COLLABORATOR_RENDER_CAP = 5
PEER_RENDER_CAP = 5
FLAT_RENDER_CAP = 8


# ---------------------------------------------------------------------------
# Config helpers (mirrors engram-inter-agent-prompt-hook.py)
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


def _get_tier_config(config: dict) -> tuple[Optional[str], list[str]]:
    """Read collaborator + peer logins from the github.* config block.

    Both fields optional. Returns (collaborator_login | None, peer_logins).
    Normalizes to lowercase (GH logins are case-insensitive).
    """
    github_block = config.get("github") or {}
    collaborator = (github_block.get("primary_user_login") or "").strip().lower() or None
    peers_raw = github_block.get("counterparts_logins") or []
    if not isinstance(peers_raw, list):
        peers_raw = []
    peers = [str(p).strip().lower() for p in peers_raw if p and isinstance(p, str)]
    peers = [p for p in peers if p]
    return collaborator, peers


# ---------------------------------------------------------------------------
# Cursor helpers (mirrors engram-inter-agent-prompt-hook.py)
# ---------------------------------------------------------------------------

def _read_cursor(path: str) -> Optional[datetime]:
    """Read a cursor file; return UTC datetime or None if absent/invalid."""
    try:
        raw = Path(path).read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _write_cursor(path: str, ts: datetime) -> None:
    """Write a UTC datetime to a cursor file atomically (tmp + os.replace)."""
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    dest = Path(path)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(ts_str + "\n")
        os.replace(tmp, dest)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"WARN: cursor write failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Self-login resolution
# ---------------------------------------------------------------------------

def _get_self_login() -> Optional[str]:
    """Return this agent's own GitHub login.

    Reads ~/.engram/github-self-login.txt on hits; fetches via gh api /user
    on miss and caches the result. Returns None on any failure.
    """
    cache_path = Path(SELF_LOGIN_PATH)
    if cache_path.exists():
        try:
            login = cache_path.read_text().strip()
            if login:
                # Normalize to lowercase — GH logins are case-insensitive
                return login.lower()
        except OSError:
            pass

    # Fetch from GitHub API
    try:
        result = subprocess.run(
            ["gh", "api", "/user", "--jq", ".login"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            login = result.stdout.strip()
            if login:
                try:
                    cache_path.write_text(login + "\n")
                except OSError:
                    pass
                # Normalize to lowercase — GH logins are case-insensitive
                return login.lower()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


# ---------------------------------------------------------------------------
# Actor cache helpers
# ---------------------------------------------------------------------------

def _load_actor_cache() -> dict:
    """Load the actor cache from disk. Returns {} on any failure."""
    try:
        text = Path(ACTOR_CACHE_PATH).read_text()
        return json.loads(text)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_actor_cache(cache: dict) -> None:
    """Save the actor cache to disk atomically."""
    dest = Path(ACTOR_CACHE_PATH)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=2))
        os.replace(tmp, dest)
    except OSError:
        pass


def _cache_get(cache: dict, url: str, now: datetime) -> Optional[str]:
    """Return cached actor login for url if present and not expired."""
    entry = cache.get(url)
    if not entry:
        return None
    try:
        cached_at = datetime.fromisoformat(entry["cached_at"].replace("Z", "+00:00"))
        if (now - cached_at) < timedelta(hours=ACTOR_CACHE_TTL_HOURS):
            return entry["login"]
    except (KeyError, ValueError):
        pass
    return None


def _cache_set(cache: dict, url: str, login: str, now: datetime) -> None:
    """Store an actor login in the cache."""
    cache[url] = {
        "login": login,
        "cached_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# Actor fetching
# ---------------------------------------------------------------------------

def _fetch_actor_for_url(url: str) -> Optional[str]:
    """Fetch the actor login from a GitHub API URL (comment or PR/issue endpoint).

    Returns the login string or None on any failure.
    Per-call timeout: 1s.
    """
    if not url:
        return None
    try:
        result = subprocess.run(
            ["gh", "api", url, "--jq", ".user.login"],
            capture_output=True, text=True, timeout=1,
        )
        if result.returncode == 0:
            login = result.stdout.strip()
            if login:
                return login
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def _short_time(iso_ts: str) -> str:
    """ISO timestamp -> 'HH:MM' or 'MM-DD HH:MM' if not today."""
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        today = datetime.now(ts.tzinfo).date()
        if ts.date() == today:
            return ts.strftime("%H:%M")
        return ts.strftime("%m-%d %H:%M")
    except (ValueError, AttributeError):
        return iso_ts[:16] if iso_ts else "??"


def _subject_id(subject: dict) -> str:
    """Extract issue/PR number from subject.url for compact display."""
    url = subject.get("url", "")
    parts = url.rstrip("/").split("/")
    if parts and parts[-1].isdigit():
        kind = parts[-2] if len(parts) >= 2 else ""
        prefix = "PR" if kind == "pulls" else "#"
        return f"{prefix}{parts[-1]}"
    return ""


def _html_url(notif: dict) -> str:
    """Build a clickable HTML URL for the notification subject.

    For PR/issue subjects, converts the API URL
    (https://api.github.com/repos/owner/repo/pulls/N) to the HTML URL
    (https://github.com/owner/repo/pull/N). Falls back to repository
    html_url if conversion fails.
    """
    subject = notif.get("subject", {})
    api_url = subject.get("url", "")
    repo = notif.get("repository", {})
    repo_html = repo.get("html_url", "")

    if api_url:
        # api.github.com/repos/OWNER/REPO/pulls/N -> github.com/OWNER/REPO/pull/N
        # api.github.com/repos/OWNER/REPO/issues/N -> github.com/OWNER/REPO/issues/N
        converted = (
            api_url
            .replace("https://api.github.com/repos/", "https://github.com/")
            .replace("/pulls/", "/pull/")
        )
        if "github.com" in converted and converted != api_url.replace(
                "https://api.github.com/repos/", "https://github.com/"):
            return converted
        # Simpler fallback: just replace domain and /repos/
        converted = api_url.replace("https://api.github.com/repos/", "https://github.com/")
        converted = converted.replace("/pulls/", "/pull/")
        if converted.startswith("https://github.com/"):
            return converted

    return repo_html


def _classify_actor(
    login: Optional[str],
    collaborator_login: Optional[str],
    peer_logins: list[str],
) -> str:
    """Classify actor into tier string.

    Returns one of: 'collaborator', 'peer_agent', 'outside', 'unknown'.
    'unknown' is used when login is None (unresolvable actor).
    """
    if login is None:
        return "unknown"
    # Normalize to lowercase — GH logins are case-insensitive
    login_lc = login.lower()
    if collaborator_login and login_lc == collaborator_login:
        return "collaborator"
    if login_lc in peer_logins:
        return "peer_agent"
    return "outside"


def _render_notif_line(notif: dict, indent: str = "    ") -> str:
    """Render a single notification as a display line + URL continuation."""
    t = _short_time(notif.get("updated_at", ""))
    repo = notif.get("repository", {}).get("name", "?")
    sid = _subject_id(notif.get("subject", {}))
    title = (notif.get("subject", {}).get("title", "") or "")[:60]
    reason = notif.get("reason", "")
    url = _html_url(notif)
    line = f"{indent}[{t}] {repo} {sid} ({reason}) — {title}"
    if url:
        line += f"\n{indent}   {url}"
    return line


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # ── Section gate: return silently if github.enabled is not true ──────────
    config = _load_config()
    if not (config.get("github") or {}).get("enabled", False):
        # Feature opt-in: not enabled. No output, no gh api fetch, no cost.
        return 0

    # ── Fetch notifications from GitHub ─────────────────────────────────────
    try:
        result = subprocess.run(
            ["gh", "api", "notifications", "--paginate=false"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return 0  # silent on error
        notifs = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return 0

    if not isinstance(notifs, list):
        return 0

    now = datetime.now(timezone.utc)

    # ── Load tier classification from config ─────────────────────────────────
    collaborator_login, peer_logins = _get_tier_config(config)
    config_empty = (collaborator_login is None) and not peer_logins

    # ── Read cursor ──────────────────────────────────────────────────────────
    cursor = _read_cursor(CURSOR_PATH)  # None on first run

    # ── Advance cursor immediately (always, even if zero items) ─────────────
    _write_cursor(CURSOR_PATH, now)

    # ── Get self login for self-filter ───────────────────────────────────────
    self_login = _get_self_login()

    # ── Load actor cache ─────────────────────────────────────────────────────
    actor_cache = _load_actor_cache()
    cache_dirty = False

    # ── Filter CI noise ──────────────────────────────────────────────────────
    notifs = [n for n in notifs if n.get("reason") not in NOISE_REASONS]

    # ── Sort newest first ─────────────────────────────────────────────────────
    notifs.sort(key=lambda n: n.get("updated_at", ""), reverse=True)

    # ── Partition into new (since cursor) and older-unread ──────────────────
    new_notifs = []
    older_notifs = []

    for n in notifs:
        updated = n.get("updated_at", "")
        if cursor is None:
            # First ever run: treat everything as "new"
            new_notifs.append(n)
        else:
            try:
                updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if updated_dt > cursor:
                    new_notifs.append(n)
                else:
                    older_notifs.append(n)
            except ValueError:
                older_notifs.append(n)

    # ── Resolve actors for new notifications; apply self-filter ──────────────
    # Track how many cold-cache fetches we've done to respect ACTOR_FETCH_CAP.
    cold_fetches = 0

    classified_new: list[tuple[dict, str]] = []  # (notif, tier)
    for n in new_notifs:
        url = (n.get("subject", {}).get("latest_comment_url")
               or n.get("subject", {}).get("url") or "")
        cache_hit = _cache_get(actor_cache, url, now) if url else None
        fetch_allowed = cold_fetches < ACTOR_FETCH_CAP

        if cache_hit is not None:
            actor = cache_hit
        elif fetch_allowed and url:
            actor = _fetch_actor_for_url(url)
            if actor:
                _cache_set(actor_cache, url, actor, now)
                cache_dirty = True
                cold_fetches += 1
            else:
                actor = None
                cold_fetches += 1
        else:
            actor = None

        # Self-filter: skip when we are the actor
        if self_login and (actor or "").lower() == self_login:
            continue

        tier = _classify_actor(actor, collaborator_login, peer_logins)
        classified_new.append((n, tier))

    # ── Save actor cache if modified ─────────────────────────────────────────
    if cache_dirty:
        _save_actor_cache(actor_cache)

    # ── Build output ─────────────────────────────────────────────────────────
    n_new = len(classified_new)
    n_older = len(older_notifs)

    # Zero new, possibly some older
    if n_new == 0:
        if n_older == 0:
            return 0  # completely silent
        # Single short line
        output = f"📬 0 new GitHub notifications ({n_older} older still-unread)."
        _emit(output)
        return 0

    lines = []
    plural = "s" if n_new != 1 else ""
    lines.append(f"📬 {n_new} new GitHub notification{plural} since last prompt")

    # Config-prompt note when both optional fields are absent (2-tier fallback)
    if config_empty:
        lines.append(
            "(note: configure github.primary_user_login + github.counterparts_logins"
            " in ~/.engram/config.json for priority emphasis)"
        )

    if config_empty:
        # 2-tier flat fallback: self filtered already; render all others flat.
        # Items already sorted newest-first; cap at FLAT_RENDER_CAP.
        visible = classified_new[:FLAT_RENDER_CAP]
        overflow = len(classified_new) - len(visible)
        for notif, _ in visible:
            lines.append(_render_notif_line(notif, indent="  "))
        if overflow > 0:
            lines.append(f"  ... (+{overflow} more)")
    else:
        # 3-tier rendering
        collab_items = [(n, t) for n, t in classified_new if t == "collaborator"]
        peer_items = [(n, t) for n, t in classified_new if t == "peer_agent"]
        # outside = everything else (including unknown when cap exceeded)
        outside_items = [(n, t) for n, t in classified_new
                         if t not in ("collaborator", "peer_agent")]

        if collab_items:
            lines.append("  ↳ from collaborator:")
            # Sort newest-first, cap at COLLABORATOR_RENDER_CAP
            collab_sorted = sorted(
                collab_items, key=lambda x: x[0].get("updated_at", ""), reverse=True
            )
            visible = collab_sorted[:COLLABORATOR_RENDER_CAP]
            overflow = len(collab_sorted) - len(visible)
            for notif, _ in visible:
                lines.append(_render_notif_line(notif, indent="    "))
            if overflow > 0:
                lines.append(f"    ... (+{overflow} more from collaborator)")

        if peer_items:
            lines.append("  ↳ from peer agents:")
            # Sort newest-first, cap at PEER_RENDER_CAP
            peer_sorted = sorted(
                peer_items, key=lambda x: x[0].get("updated_at", ""), reverse=True
            )
            visible = peer_sorted[:PEER_RENDER_CAP]
            overflow = len(peer_sorted) - len(visible)
            for notif, _ in visible:
                lines.append(_render_notif_line(notif, indent="    "))
            if overflow > 0:
                lines.append(f"    ... (+{overflow} more from peer agents)")

        if outside_items:
            k = len(outside_items)
            lines.append(
                f"  ↳ from outside: {k} notification{'s' if k != 1 else ''}"
                " (use `gh api notifications` to inspect)"
            )

    if n_older > 0:
        lines.append(f"(Plus {n_older} older still-unread.)")

    _emit("\n".join(lines))
    return 0


def _emit(text: str) -> None:
    """Emit hook output as a UserPromptSubmit hookSpecificOutput JSON blob."""
    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    print(json.dumps(response))


if __name__ == "__main__":
    sys.exit(main())
