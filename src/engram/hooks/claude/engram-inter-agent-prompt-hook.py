#!/usr/bin/env python3
"""UserPromptSubmit hook: surface unread DM messages to the agent.

On every prompt, in multi-agent mode, queries the forum DM update feed
(GET /api/updates?agent=<me>&kinds=dm) for unread DMs and injects a
summary into the session context.

Two int (as_of) cursors:
  - inter-agent-surfaced-cursor.txt: advances to the API `as_of` watermark
    on every LIVE fetch. Drives the "new since last prompt" bucket. After the
    hook fires, DMs that were "new" drop to "older still-unread" on the next
    prompt. NOT advanced on outage (banner path) — a DM arriving during an
    outage is never stepped over.
  - inter-agent-read-cursor.txt: advances only when the agent explicitly
    marks DMs as read (via `ia mr`). The hook READS it, NEVER advances it.
    Drives the "older still-unread" tally.

Subject extraction precedence (from DM body):
  1. **Subject:** bold-prefix line (PR3a `ia write` convention).
  2. First `# Heading` line in the body.
  3. First 60 characters of body text.

Token budget cap: if "new since last prompt" exceeds LIST_CAP messages,
group-summarize with a "+ N older from <senders>" line.

Anti-patterns guarded:
  - Mode gate is the first check — single-agent installs
    see exactly zero behavior change.
  - Read cursor is NEVER advanced here; only surfaced cursor advances.
  - No LLM calls, no local-FS letter reads — one forum API call only.
    Fail-open on unreachable: injects a brief warning banner, never crashes.
  - No local INTER_AGENT_DIR fallback (the UCS pure-API invariant).

Part of the UCS PR3b migration (hooks/claude → forum DM API).
Stacked on PR3a (ia.py DM CLI). Pattern reference: engram-baton-prompt-hook.py.
"""

import json
import os
import pwd
import re
import sys
from pathlib import Path
from typing import Optional

# NOTE: `pwd` is no longer used directly in this file (its one use --
# _get_agent_name's uid fallback -- now lives in _prompthooklib.py) but the
# import is kept so `pwd` stays a patchable attribute on THIS module: tests
# patch `hook.pwd.getpwuid` to exercise the uid-fallback path, and since
# `import pwd` always binds the same singleton sys.modules['pwd'] object,
# patching it here also patches what _prompthooklib.get_agent_name() sees.

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)

SURFACED_CURSOR_PATH = os.path.join(ENGRAM_HOME, "inter-agent-surfaced-cursor.txt")
READ_CURSOR_PATH = os.path.join(ENGRAM_HOME, "inter-agent-read-cursor.txt")

# If "new since last prompt" exceeds this, group-summarize instead of listing.
LIST_CAP = 10

# ---------------------------------------------------------------------------
# Pure-API (UCS invariant): this hook reads DM state ONLY via the forum HTTP
# API — no local-filesystem fallback.  forum_api ships in tools/.
#
# Resolve tools/ in BOTH topologies via the shared _hooklib helper (gh#1657 —
# this walk-parents logic was duplicated byte-for-byte across this hook and
# engram-baton-prompt-hook.py, slice 1 of which extracted it; this is slice 2,
# a byte-identical swap since this hook shares baton's marker file
# (forum_api.py) and candidate ordering exactly). _hooklib.py lives alongside
# this file in both topologies (source: hooks/claude/; deployed: hooks/), so
# no walk-parents is needed to find it — but this repo's own test loaders
# (importlib.util.spec_from_file_location) don't auto-add a script's own
# directory to sys.path the way a real `python3 hooks/x.py` invocation does,
# so this hook adds its own directory explicitly before importing _hooklib.
# Import is best-effort throughout: any failure degrades the hook to a
# silent no-op rather than crashing the prompt. (Behavior note, same as
# slice 1: the pre-extraction inline walk-parents loop was NOT itself
# wrapped in try/except -- wrapping it here is a deliberate tightening to
# match this file's own "never crash the prompt" discipline, not an
# accidental behavior change.)
#
# gh#1680 slice 1: the config/agent-name/emit-context/resolve-tools-dir
# prologue previously inlined here now lives in the shared _prompthooklib
# module (same directory, same sys.path bootstrap used for _hooklib above).
# _PROMPTHOOKLIB_OK gates main() -- if the shared module somehow fails to
# import, the hook degrades to a silent no-op rather than crashing, same
# discipline as every other best-effort import in this prologue.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Config helpers -- delegate to _prompthooklib (gh#1680 slice 1). Thin
# per-hook wrappers, not bare aliases: they pin ENGRAM_HOME (this hook's own
# module constant, captured once at import time above) as an explicit arg,
# preserving both (a) the exact zero-arg call signature every existing call
# site and test-monkeypatch already depends on, and (b) the "frozen at
# import time" ENGRAM_HOME timing the pre-extraction inline code had.
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load $ENGRAM_HOME/config.json. Returns {} on any failure."""
    return _load_config_impl(ENGRAM_HOME)


def _get_agent_name(config: Optional[dict] = None) -> str:
    """Resolve this agent's own name. See _prompthooklib.get_agent_name."""
    return _get_agent_name_impl(config, ENGRAM_HOME)


def _is_multi_agent_mode(config: Optional[dict] = None) -> bool:
    """True if config.json mode == 'multi'. Mirrors engram_client helper."""
    if config is None:
        config = _load_config()
    return config.get("mode", "single") == "multi"


# ---------------------------------------------------------------------------
# Int cursor helpers  (as_of integer stored in cursor files)
# ---------------------------------------------------------------------------

def _read_cursor_int(path: str) -> int:
    """Read the cursor file; return int or 0 if absent/invalid."""
    try:
        raw = Path(path).read_text().strip()
        return int(raw)
    except (OSError, ValueError):
        return 0


def _write_cursor_int(path: str, value: int) -> None:
    """Write the cursor int to file atomically (tmp + os.replace).

    Uses atomic tmp + os.replace so a concurrent hook fire cannot read an
    empty or partial file. Fail-open: logs to stderr on write error rather
    than crashing the hook.
    """
    dest = Path(path)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(str(value) + "\n")
        os.replace(tmp, dest)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"WARN: cursor write failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# DM body / subject extraction
# ---------------------------------------------------------------------------

_SUBJECT_RE = re.compile(r"^\*\*Subject:\*\*\s*(.+)$", re.MULTILINE)


def _extract_dm_subject(body: str) -> str:
    """Extract a display subject from a DM body.

    Precedence:
      1. ``**Subject:**`` bold-prefix line (PR3a ia write convention).
      2. First ``# Heading`` line in the body.
      3. First 60 chars of body text (stripped of leading hash lines).
    """
    text = body or ""

    # Priority 1: **Subject:** line
    m = _SUBJECT_RE.search(text)
    if m:
        subj = m.group(1).strip()
        if subj:
            return subj

    # Priority 2: first # heading in body
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title

    # Priority 3: body-first-60 fallback — skip hash-only / empty lines
    body_lines = [
        ln for ln in text.split("\n")
        if ln.strip() and ln.strip().lstrip("#").strip()
    ]
    if body_lines:
        first_line = body_lines[0].strip().lstrip("#").strip()
        if len(first_line) > 60:
            return first_line[:57] + "..."
        return first_line

    return "(no subject)"


# ---------------------------------------------------------------------------
# Sender formatting
# ---------------------------------------------------------------------------

def _format_senders(updates: list) -> str:
    """Return a comma-joined sender string from a list of DM update dicts.

    Extracts sorted distinct 'sender' values. If more than 3 distinct senders,
    shows first 2 followed by '+ K other(s)'.
    """
    senders = sorted({u.get("sender", "unknown") for u in updates})
    if len(senders) <= 3:
        return ", ".join(senders)
    shown = senders[:2]
    rest = len(senders) - 2
    return ", ".join(shown) + f", + {rest} other(s)"


# ---------------------------------------------------------------------------
# Forum API: DM updates fetch
# ---------------------------------------------------------------------------

def _fetch_dm_updates(config: dict, agent_name: str, since: int) -> dict:
    """GET /api/updates?agent=<agent>&kinds=dm&since=<since>.

    Returns the full response dict: {updates: [...], as_of: int, ts: str}.
    Raises ForumNetworkError / ForumHttpError on failure.
    There is NO local-FS fallback — DM state lives only in the forum service.

    Short timeout (3s): runs synchronously on every prompt; a half-dead forum
    must not stall the prompt beyond the hook's time budget.
    """
    client = ForumClient(forum_url_from_config(config), timeout=3)
    return client.get("/api/updates", params={
        "agent": agent_name,
        "kinds": "dm",
        "since": str(since),
    })


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

    # ── Read stdin payload (session_id for drift throttle) ────────────────────
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}
    session_id = hook_input.get("session_id", "")

    # ── Config (single load) ──────────────────────────────────────────────────
    config = _load_config()

    # ── Mode gate (FIRST check) ───────────────────────────────────────────────
    # Single-agent installs (or forum_api import failure) see zero output and
    # zero state mutation.
    if not _FORUM_API_OK or not _is_multi_agent_mode(config):
        sys.exit(0)

    # ── Read cursors ──────────────────────────────────────────────────────────
    surfaced_cursor = _read_cursor_int(SURFACED_CURSOR_PATH)  # 0 if absent
    read_cursor = _read_cursor_int(READ_CURSOR_PATH)           # 0 if absent

    # ── Resolve own name ──────────────────────────────────────────────────────
    agent_name = _get_agent_name(config)

    # ── Fetch DM updates from forum API (loud-on-failure) ────────────────────
    # Pass since=min(surfaced_cursor, read_cursor) to retrieve all items that
    # may fall into either the "new" or "still-unread" buckets.
    since = min(surfaced_cursor, read_cursor)
    live_fetch = False
    new_as_of = surfaced_cursor  # fallback if fetch fails (should not be used)
    try:
        data = _fetch_dm_updates(config, agent_name, since)
        updates = data.get("updates", [])
        new_as_of = int(data.get("as_of", surfaced_cursor))
        live_fetch = True
    except (ForumNetworkError, ForumHttpError) as e:
        # UCS invariant: fail LOUD — inject a banner rather than rendering
        # nothing silently. The forum being down IS a problem the agent should
        # see. Never fall back to local FS reads (the UCS pure-API invariant).
        _emit_context(
            f"⚠️ DM unread check unavailable (UCS) — forum API error: {e}"
        )
        return
    except Exception:
        # Belt-and-suspenders: unexpected error must never crash the prompt.
        _emit_context("⚠️ DM unread check unavailable (UCS)")
        return

    # ── Partition ─────────────────────────────────────────────────────────────
    # Acknowledged (seq <= read_cursor) → invisible, regardless of surfaced.
    # Of the remaining unread items:
    #   new_dms:    seq > surfaced_cursor (arrived since last prompt)
    #   unread_dms: seq <= surfaced_cursor (seen before, not yet read)
    #
    # Guard for the read-cursor-ahead edge case: when `ia mr` was called
    # without an intervening UserPromptSubmit (e.g. during a Monitor wake),
    # read_cursor can exceed surfaced_cursor.  Items with
    # surfaced_cursor < seq <= read_cursor are acknowledged and must be
    # invisible — the is_unread check below catches them before partitioning.
    new_dms = []
    unread_dms = []
    for item in updates:
        seq = int(item.get("seq", 0))
        # Already acknowledged — skip
        is_unread = seq > read_cursor
        if not is_unread:
            continue
        if seq > surfaced_cursor:
            new_dms.append(item)
        else:
            unread_dms.append(item)

    # Sort by seq ascending (oldest first within each bucket)
    new_dms.sort(key=lambda u: int(u.get("seq", 0)))
    unread_dms.sort(key=lambda u: int(u.get("seq", 0)))

    # ── Advance surfaced cursor on LIVE fetch only ────────────────────────────
    # Always advance to the current watermark, even if nothing to surface —
    # keeps the delta window current.  Never advance on banner path (early
    # return above) so DMs arriving during an outage are not stepped over.
    if live_fetch:
        _write_cursor_int(SURFACED_CURSOR_PATH, new_as_of)

    # ── Exit silently if nothing to report ────────────────────────────────────
    if not new_dms and not unread_dms:
        sys.exit(0)

    # ── Build injection block ─────────────────────────────────────────────────
    lines: list = []

    n_new = len(new_dms)
    if n_new > 0:
        senders_new = _format_senders(new_dms)
        msg_word = "message" if n_new == 1 else "messages"
        lines.append(
            f"\U0001f4ec {n_new} new {msg_word} from {senders_new}"
            f" — read before responding"
            f" (counterpart-agent messages carry context from the user you'd otherwise miss)."
        )
        if n_new <= LIST_CAP:
            for item in new_dms:
                subject = _extract_dm_subject(item.get("body", ""))
                sender = item.get("sender", "unknown")
                lines.append(f"  - from {sender} — \"{subject}\"")
        else:
            # Group-summarize when over the cap
            shown = new_dms[:LIST_CAP]
            rest = new_dms[LIST_CAP:]
            for item in shown:
                subject = _extract_dm_subject(item.get("body", ""))
                sender = item.get("sender", "unknown")
                lines.append(f"  - from {sender} — \"{subject}\"")
            rest_senders = sorted({u.get("sender", "unknown") for u in rest})
            lines.append(f"  + {len(rest)} older from {', '.join(rest_senders)}")

    n_unread = len(unread_dms)
    if n_unread > 0:
        if n_new > 0:
            senders_unread = _format_senders(unread_dms)
            lines.append(f"  (Plus {n_unread} older still-unread from {senders_unread}.)")
        else:
            senders_unread = _format_senders(unread_dms)
            lines.append(
                f"\U0001f4ec {n_unread} older still-unread message{'s' if n_unread != 1 else ''}"
                f" from {senders_unread} — finish reading at the next natural break."
            )

    # Footer: name the CLI so the natural-affordance path is the cursor-aware one.
    # Only add if there are DMs (the footer is DM-context — not drift-only).
    if new_dms or unread_dms:
        lines.append(
            "Tools: `ia read <counterpart>` (read thread) · "
            "`ia write --to <counterpart>` (send DM) · "
            "`ia mr` (mark-read) · "
            "`ia status` (show cursor + counts)."
        )

    _emit_context("\n".join(lines))


if __name__ == "__main__":
    main()
