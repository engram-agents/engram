#!/usr/bin/env python3
"""UserPromptSubmit hook: surface unread inter-agent letters to the recipient.

On every prompt, if the agent is in multi-agent mode, scans
/home/agents-shared/inter-agent/ for markdown letters addressed to this
agent and injects a summary of new and still-unread letters into the
session context.

Two-cursor state (see DESIGN.md Layer 3):
  - inter-agent-surfaced-cursor.txt: advances on every hook fire. Drives
    the "new since last prompt" bucket. After the hook fires, letters that
    were "new" drop to "older still-unread" on the next prompt.
  - inter-agent-read-cursor.txt: advances only when the agent explicitly
    acknowledges a letter (via engram-letter skill or manual cursor edit).
    Drives the "older still-unread" tally.

Title extraction precedence (per DESIGN.md Layer 2):
  1. First non-frontmatter `# Heading` line in the body.
  2. The `re:` frontmatter field.
  3. First 60 characters of the body text.

Token budget cap: if "new since last prompt" exceeds 10 letters,
group-summarize with a "+ N older from <authors>" line.

Anti-patterns guarded:
  - Mode gate is the first check — single-agent installs
    see exactly zero behavior change.
  - Read cursor is NEVER advanced here; only surfaced cursor advances.
  - No LLM calls, no network, no DB — file-system reads only.
    Timeout budget: 5 seconds max.

Part of the inter-agent-comms-v1 cohort (PR 3), stacked on PR 1's
mode-gate foundation (is_multi_agent_mode, get_counterparts helpers).

Design doc: see project design notes.
"""

import hashlib
import json
import os
import pwd
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
INTER_AGENT_DIR = os.environ.get("INTER_AGENT_DIR", "/home/agents-shared/inter-agent")

SURFACED_CURSOR_PATH = os.path.join(ENGRAM_HOME, "inter-agent-surfaced-cursor.txt")
READ_CURSOR_PATH = os.path.join(ENGRAM_HOME, "inter-agent-read-cursor.txt")

# If "new since last prompt" exceeds this, group-summarize instead of listing.
LIST_CAP = 10


# ---------------------------------------------------------------------------
# Config helpers
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


def _get_agent_name(config: dict | None = None) -> str:
    """Resolve this agent's own name.

    Priority:
      1. config.json["agent_name"] field (explicit; wins if set).
      2. $USER env var, agent- prefix stripped.
      3. $LOGNAME env var, agent- prefix stripped (Claude Code hook context
         populates $LOGNAME but not $USER).
      4. pwd.getpwuid(os.getuid()).pw_name, agent- prefix stripped.
      5. Empty string (hook will see no matching letters — safe).

    Username layers (USER, LOGNAME, pwd) return the raw username if it doesn't
    start with `agent-` — caller (multi-agent mode) must validate via
    counterparts list. Old `_get_agent_name` returned "" for non-agent
    usernames; this is a deliberate behavioral change.
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


def _is_multi_agent_mode(config: dict | None = None) -> bool:
    """True if config.json mode == 'multi'. Mirrors engram_client helper."""
    if config is None:
        config = _load_config()
    return config.get("mode", "single") == "multi"


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
        # Normalize trailing Z to +00:00 for fromisoformat compat
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        print(
            f"[engram-inter-agent-hook] cursor at {path} is not an ISO-8601 timestamp "
            f"(got: {raw[:200]!r}) — treating all letters as unread. "
            f"Expected format: '2026-05-22T14:30:00Z'. See inter-agent/README.md §4.",
            file=sys.stderr,
        )
        return None


def _write_cursor(path: str, ts: datetime) -> None:
    """Write a UTC datetime to a cursor file (ISO format with Z suffix).

    Uses an atomic tmp + os.replace pattern so a concurrent hook fire cannot
    read an empty or partial file between O_TRUNC and the write completing.
    """
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    dest = Path(path)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(ts_str + "\n")
        os.replace(tmp, dest)  # atomic on POSIX
    except OSError as e:
        # Best-effort cleanup of tmp on failure; preserve old cursor.
        try:
            tmp.unlink()
        except OSError:
            pass
        # Don't crash the hook on cursor-write failure — log to stderr and continue.
        print(f"WARN: cursor write failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Letter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML-ish frontmatter from a markdown letter.

    Returns (fields_dict, body_text). On parse failure returns ({}, text).
    Only parses simple key: value pairs — no nested YAML; sufficient for
    the established frontmatter format (from, to, timestamp, re).
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_block = m.group(1)
    body = text[m.end():]
    fields = {}
    for field_m in _FRONTMATTER_FIELD_RE.finditer(fm_block):
        key = field_m.group(1).strip().lower()
        val = field_m.group(2).strip()
        fields[key] = val
    return fields, body


def _extract_title(fields: dict, body: str) -> str:
    """Extract a display title from a parsed letter.

    Precedence:
      1. First non-frontmatter `# Heading` line in the body.
      2. The `re:` frontmatter field.
      3. First 60 chars of body text (stripped).
    """
    # Priority 1: first # heading in body
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title

    # Priority 2: re: frontmatter field
    re_field = fields.get("re", "").strip()
    if re_field:
        return re_field

    # Priority 3: body-first-60 fallback. Skip hash-only / empty lines (those
    # would produce a degenerate title like '##' or '').
    body_lines = [
        ln for ln in body.split("\n")
        if ln.strip() and ln.strip().lstrip("#").strip()
    ]
    if body_lines:
        first_line = body_lines[0].strip().lstrip("#").strip()
        if len(first_line) > 60:
            return first_line[:57] + "..."
        return first_line

    return "(no title)"


def _parse_letter_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse a frontmatter timestamp string into a UTC datetime."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_ts(ts: datetime) -> str:
    """Format a datetime for display: YYYY-MM-DDTHH:MMZ"""
    return ts.strftime("%Y-%m-%dT%H:%MZ")


def _format_senders(letters: list[dict]) -> str:
    """Return a comma-joined sender string from a list of letter dicts.

    Extracts sorted distinct 'from' values. If more than 3 distinct senders,
    shows first 2 followed by '+ K other(s)'.
    """
    senders = sorted({l["from"] for l in letters})
    if len(senders) <= 3:
        return ", ".join(senders)
    shown = senders[:2]
    rest = len(senders) - 2
    return ", ".join(shown) + f", + {rest} other(s)"


# ---------------------------------------------------------------------------
# Letter scanning
# ---------------------------------------------------------------------------

def _is_recipient(to_field: str, agent_name: str) -> bool:
    """Return True iff agent_name is a whole-name match in the to: field.

    Membership test: split on commas, strip whitespace, exact match — NOT
    substring. "mira" does not match "miranda"; "ariadne" does not match a
    to: field of "aria, borges".

    Both to_field and agent_name are compared case-insensitively.
    """
    if not agent_name or not to_field:
        return False
    agent_lower = agent_name.strip().lower()
    for name in to_field.split(","):
        if name.strip().lower() == agent_lower:
            return True
    return False


def _scan_letters(agent_name: str) -> list[dict]:
    """Scan INTER_AGENT_DIR for letters addressed to agent_name.

    Returns a list of dicts with keys: path, timestamp, from, title.
    Files that don't parse cleanly are silently skipped.
    """
    inter_dir = Path(INTER_AGENT_DIR)
    if not inter_dir.is_dir():
        return []

    letters = []
    for md_file in inter_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        fields, body = _parse_frontmatter(text)
        if not fields:
            # No frontmatter — skip (malformed or README)
            continue

        # Must have a valid 'to' field that names our agent (membership test —
        # exact whole-name match in the comma-split list, not substring).
        to_field = fields.get("to", "").strip()
        if not _is_recipient(to_field, agent_name):
            continue

        # Must have a parseable timestamp
        ts = _parse_letter_timestamp(fields.get("timestamp", ""))
        if ts is None:
            continue

        title = _extract_title(fields, body)
        from_field = fields.get("from", "unknown").strip()

        letters.append({
            "path": str(md_file),
            "timestamp": ts,
            "from": from_field,
            "title": title,
        })

    # Sort by timestamp ascending (oldest first within each bucket)
    letters.sort(key=lambda l: l["timestamp"])
    return letters


# ---------------------------------------------------------------------------
# Shared-bin drift detection
# ---------------------------------------------------------------------------

def _shared_bin_drift_lines(engram_home: Path, session_id: str) -> list[str]:
    """Return banner lines for drifted shared-bin CLIs, or [] if clean/skip."""
    shared_bin = Path("/home/agents-shared/bin")
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not shared_bin.exists() or not plugin_root:
        return []

    FAMILY = [("ia", "ia.py"), ("baton", "baton.py"), ("forum", "forum.py")]

    def _md5(p: Path) -> str:
        return hashlib.md5(p.read_bytes()).hexdigest()

    drifted = []
    for cli_name, src_name in FAMILY:
        shared = shared_bin / cli_name
        src = Path(plugin_root) / "tools" / src_name
        if not shared.exists() or not src.exists():
            continue
        if _md5(shared) != _md5(src):
            drifted.append((cli_name, src_name))

    if not drifted:
        return []

    # Compute fingerprint of the drift set
    fingerprint = hashlib.md5(str(sorted(drifted)).encode()).hexdigest()

    # Once-per-session throttle: skip if same fingerprint + session already warned
    state_file = engram_home / "shared-bin-drift.state"
    try:
        state = json.loads(state_file.read_text())
        if state.get("fingerprint") == fingerprint and state.get("session_id") == session_id:
            return []  # throttled — already warned this session
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # Write state (update fingerprint + session)
    try:
        state_file.write_text(json.dumps({"fingerprint": fingerprint, "session_id": session_id}))
    except OSError:
        pass  # non-fatal: if we can't write state, we'll warn again next prompt

    lines = ["⚠️ shared-bin CLI drift detected (multi-agent):"]
    for cli_name, src_name in drifted:
        lines.append(
            f"  - {cli_name}: sudo cp \"$CLAUDE_PLUGIN_ROOT/tools/{src_name}\""
            f" /home/agents-shared/bin/{cli_name}"
        )
    lines.append(
        "Refresh before agents resume to avoid silent divergence. "
        "Use `engram_diagnose` for the on-demand check."
    )
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # ── Read stdin payload (session_id for drift throttle) ────────────────────
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        hook_input = {}
    session_id = hook_input.get("session_id", "")

    # ── Config (single load) ──────────────────────────────────────────────────
    config = _load_config()

    # ── Mode gate ─────────────────────────────────────────────────────────────
    # First check — single-agent installs see zero output, zero state mutation.
    if not _is_multi_agent_mode(config):
        sys.exit(0)

    # ── Read cursors ──────────────────────────────────────────────────────────
    surfaced_cursor = _read_cursor(SURFACED_CURSOR_PATH)  # may be None
    read_cursor = _read_cursor(READ_CURSOR_PATH)           # may be None

    # ── Resolve own name ──────────────────────────────────────────────────────
    agent_name = _get_agent_name(config)
    # Even if agent_name is empty, scanning returns nothing (no match).

    # ── Scan letters ──────────────────────────────────────────────────────────
    all_letters = _scan_letters(agent_name)

    # ── Partition ─────────────────────────────────────────────────────────────
    # Acknowledged (ts <= read_cursor) → invisible first, regardless of surfaced cursor.
    # Of the remaining unread letters:
    #   new_letters:   ts > surfaced_cursor (arrived since last prompt)
    #   unread_letters: ts <= surfaced_cursor (seen before, still unread)
    now = datetime.now(timezone.utc)

    new_letters = []
    unread_letters = []

    for letter in all_letters:
        ts = letter["timestamp"]
        # Already acknowledged (ts <= read_cursor) → invisible, regardless of the
        # surfaced cursor. The read cursor can run AHEAD of the surfaced cursor when
        # a letter is read via `ia read` without an intervening UserPromptSubmit fire
        # (e.g. a Monitor task-notification wake), so the "new" bucket must also
        # require the letter to be unread. (#627)
        is_unread = read_cursor is None or ts > read_cursor
        if not is_unread:
            continue
        if surfaced_cursor is None or ts > surfaced_cursor:
            new_letters.append(letter)
        else:
            unread_letters.append(letter)

    # ── Advance surfaced cursor ───────────────────────────────────────────────
    # Always advance, even if nothing to surface — keeps the delta window current.
    # Read cursor is NOT touched here; only explicit acknowledgment advances it.
    _write_cursor(SURFACED_CURSOR_PATH, now)

    # ── Shared-bin drift check (inside mode gate; prepend when present) ───────
    # Fail-open: drift check is advisory; letters channel is primary.
    # An unreadable file during a shared-bin refresh must never kill the hook.
    try:
        drift_lines = _shared_bin_drift_lines(Path(ENGRAM_HOME), session_id)
    except Exception:
        drift_lines = []

    # ── Exit silently if nothing to report ────────────────────────────────────
    if not new_letters and not unread_letters and not drift_lines:
        sys.exit(0)

    # ── Build injection block ─────────────────────────────────────────────────
    # Drift warning comes first — it is more operationally urgent than the letter list.
    lines = list(drift_lines)

    n_new = len(new_letters)
    if n_new > 0:
        senders_new = _format_senders(new_letters)
        letter_word = "letter" if n_new == 1 else "letters"
        lines.append(
            f"\U0001f4ec {n_new} new {letter_word} from {senders_new}"
            f" — read before responding"
            f" (counterpart-agent letters carry context from the user you'd otherwise miss)."
        )
        if n_new <= LIST_CAP:
            for letter in new_letters:
                ts_str = _format_ts(letter["timestamp"])
                lines.append(
                    f"  - [{ts_str}] from {letter['from']} — \"{letter['title']}\""
                )
        else:
            # Group-summarize when over the cap
            shown = new_letters[:LIST_CAP]
            rest = new_letters[LIST_CAP:]
            for letter in shown:
                ts_str = _format_ts(letter["timestamp"])
                lines.append(
                    f"  - [{ts_str}] from {letter['from']} — \"{letter['title']}\""
                )
            rest_authors = sorted({l["from"] for l in rest})
            authors_str = ", ".join(rest_authors)
            lines.append(
                f"  + {len(rest)} older from {authors_str}"
            )

    n_unread = len(unread_letters)
    if n_unread > 0:
        if n_new > 0:
            senders_unread = _format_senders(unread_letters)
            lines.append(f"  (Plus {n_unread} older still-unread from {senders_unread}.)")
        else:
            senders_unread = _format_senders(unread_letters)
            lines.append(
                f"\U0001f4ec {n_unread} older still-unread letter{'s' if n_unread != 1 else ''}"
                f" from {senders_unread} — finish reading at the next natural break."
            )

    # Footer: name the CLI so the natural-affordance path IS the cursor-aware one.
    # Only add if there are letters (the footer is letter-context — not drift-only).
    if new_letters or unread_letters:
        lines.append(
            "Tools: `ia read <filename>` (read + advance cursor) · "
            "`ia write --re <filename>` (reply with reference) · "
            "`ia status` (show cursor + counts)."
        )

    context = "\n".join(lines)

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(response))


if __name__ == "__main__":
    main()
