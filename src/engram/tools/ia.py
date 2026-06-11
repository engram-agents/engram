#!/usr/bin/env python3
"""ia — Inter-Agent CLI

A thin CLI wrapping the inter-agent file protocol at
/home/agents-shared/inter-agent/. Provides validated letter creation,
cursor-tracked reading, and quick status checks.

Nine subcommands:
  list        Show letters addressed to me (default: unread only)
  read        Display a letter; advance read cursor to its timestamp
  write       Create a new letter via $EDITOR with validated frontmatter
  mark-read   Advance read cursor without displaying a letter
  cursor      Show or set the read cursor
  status      Quick health check (agent name, mode, cursor, unread count)
  star        Star a letter to re-surface it at continuity-reset points
  unstar      Remove a letter from the starred list
  starred     List all starred letters

Design doc: ariadne-desk/projects/inter-agent-cli/DESIGN.md (v2, 2026-05-23)
Protocol:   inter-agent/README.md
Hook ref:   hooks/claude/engram-inter-agent-prompt-hook.py (parsing conventions)
"""

import argparse
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Environment + paths
# ---------------------------------------------------------------------------

ENGRAM_HOME = (
    os.environ.get("ENGRAM_HOME")
    or str(Path.home() / ".engram")
)
INTER_AGENT_DIR = os.environ.get("INTER_AGENT_DIR", "/home/agents-shared/inter-agent")

READ_CURSOR_PATH = os.path.join(ENGRAM_HOME, "inter-agent-read-cursor.txt")
SURFACED_CURSOR_PATH = os.path.join(ENGRAM_HOME, "inter-agent-surfaced-cursor.txt")
STARRED_LIST_PATH = os.path.join(ENGRAM_HOME, "inter-agent-starred.json")

# Exit codes per DESIGN §7
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3

# ---------------------------------------------------------------------------
# Config helpers  (mirrors hook conventions, does NOT import from hook)
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
      3. $LOGNAME env var, agent- prefix stripped.
      4. pwd.getpwuid(os.getuid()).pw_name, agent- prefix stripped.
      5. Empty string.
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


def _is_multi_agent_mode(config: Optional[dict] = None) -> bool:
    """True if config.json mode == 'multi'. Mirrors engram hook helper."""
    if config is None:
        config = _load_config()
    return config.get("mode", "single") == "multi"


# ---------------------------------------------------------------------------
# Mode gate  (DESIGN §9.4)
# ---------------------------------------------------------------------------

def _check_multi_agent_mode(config: Optional[dict] = None) -> None:
    """Exit with code 3 and actionable message if mode != 'multi'."""
    if config is None:
        config = _load_config()
    if not _is_multi_agent_mode(config):
        config_path = Path(ENGRAM_HOME) / "config.json"
        if not config_path.exists():
            mode_detail = "(config.json missing)"
        else:
            try:
                json.loads(config_path.read_text())
                mode_val = config.get("mode", "unset")
                mode_detail = f"(config.json mode='{mode_val}')"
            except (json.JSONDecodeError, OSError):
                mode_detail = "(config.json invalid — failed to parse)"
        print(
            f"ia: this host is in single-agent mode {mode_detail}; "
            "ia is multi-agent-only.",
            file=sys.stderr,
        )
        print(
            "To enable: set mode='multi' in config.json and restart MCP.",
            file=sys.stderr,
        )
        print(
            "See: inter-agent/README.md §2 (Mode gate).",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE)


# ---------------------------------------------------------------------------
# Cursor helpers  (identical contract to hook's _read_cursor / _write_cursor)
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
        print(
            f"ia: cursor at {path} is not an ISO-8601 timestamp "
            f"(got: {raw[:200]!r}) — treating as unread. "
            f"Expected format: '2026-05-22T14:30:00Z'. "
            f"See inter-agent/README.md §4.",
            file=sys.stderr,
        )
        return None


def _write_cursor(path: str, ts: datetime) -> None:
    """Write a UTC datetime to a cursor file atomically (tmp + os.rename).

    Uses the same atomic tmp + os.replace pattern as the hook's _write_cursor
    so concurrent hook fires cannot read a partial cursor file.
    """
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(ts_str + "\n")
        os.replace(tmp, dest)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"ia: cursor write failed: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)


def _cursor_str(ts: Optional[datetime]) -> str:
    """Format a cursor datetime for display, or '(none)' if absent."""
    if ts is None:
        return "(none)"
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_timestamp(value: str, context: str = "") -> datetime:
    """Parse an ISO-8601 UTC timestamp string; exit 1 on failure.

    Rejects filename-format values (e.g. '2026-05-22T07-15-30Z_borges.md')
    with a clear error message pointing to README §4.
    """
    prefix = f"ia {context}: " if context else "ia: "
    # Detect filename-format (dashes-for-colons pattern after the T)
    if re.search(r"T\d{2}-\d{2}-\d{2}", value):
        print(
            f"{prefix}invalid cursor format — got filename-format "
            f"'{value}' (dashes instead of colons after T). "
            f"The cursor stores the ISO timestamp, not the filename. "
            f"Expected format: '2026-05-22T07:15:30Z'. "
            f"See inter-agent/README.md §4.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Ensure timezone-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        print(
            f"{prefix}invalid ISO-8601 timestamp '{value}'. "
            f"Expected format: '2026-05-22T14:30:00Z'. "
            f"See inter-agent/README.md §4.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)


# ---------------------------------------------------------------------------
# Frontmatter parsing  (identical contract to hook's _parse_frontmatter)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w[\w-]*):\s*(.*)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> tuple:
    """Extract YAML-ish frontmatter from a markdown letter.

    Returns (fields_dict, body_text). On parse failure returns ({}, text).
    Only parses simple key: value pairs — no nested YAML; sufficient for
    the established frontmatter format (from, to, timestamp, re).

    # v2 candidate: switch to yaml.safe_load if frontmatter grows beyond
    # the simple 4-field flat-string shape. Would require vendoring PyYAML.
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

    Precedence (mirrors hook's _extract_title):
      1. First non-frontmatter `# Heading` line in the body.
      2. The `re:` frontmatter field.
      3. First 60 chars of body text (stripped).
    """
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title

    re_field = fields.get("re", "").strip()
    if re_field:
        return re_field

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


# ---------------------------------------------------------------------------
# Frontmatter validation  (DESIGN §5)
# ---------------------------------------------------------------------------

def _validate_frontmatter(
    fields: dict,
    agent_name: str,
    inter_agent_dir: Path,
    context: str = "write",
) -> list:
    """Validate frontmatter fields per DESIGN §5.

    Returns a list of error strings (empty = valid).
    """
    errors = []

    # from: required, must match our agent name (no impersonation)
    from_val = fields.get("from", "").strip()
    if not from_val:
        errors.append(
            f"ia {context}: invalid frontmatter — `from:` is missing or empty "
            f"(must be '{agent_name}')"
        )
    elif from_val != agent_name:
        errors.append(
            f"ia {context}: invalid frontmatter — `from: {from_val}` does not match "
            f"agent name `{agent_name}` (impersonation check). "
            f"See inter-agent/README.md §2."
        )

    # to: required, non-empty
    to_val = fields.get("to", "").strip()
    if not to_val:
        errors.append(
            f"ia {context}: invalid frontmatter — `to:` is missing or empty. "
            f"See inter-agent/README.md §2."
        )

    # timestamp: required, valid ISO-8601 UTC
    ts_val = fields.get("timestamp", "").strip()
    if not ts_val:
        errors.append(
            f"ia {context}: invalid frontmatter — `timestamp:` is missing or empty. "
            f"Expected format: '2026-05-23T14:10:00Z'. "
            f"See inter-agent/README.md §2."
        )
    else:
        if re.search(r"T\d{2}-\d{2}-\d{2}", ts_val):
            errors.append(
                f"ia {context}: invalid frontmatter — `timestamp: {ts_val}` uses "
                f"filename-format (dashes instead of colons). "
                f"Use ISO format with colons: '2026-05-23T14:10:00Z'. "
                f"See inter-agent/README.md §2."
            )
        else:
            try:
                datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
            except ValueError:
                errors.append(
                    f"ia {context}: invalid frontmatter — `timestamp: {ts_val}` is not "
                    f"valid ISO-8601. Expected: '2026-05-23T14:10:00Z'. "
                    f"See inter-agent/README.md §2."
                )

    # re: optional; if present, the referenced file must exist
    re_val = fields.get("re", "").strip()
    if re_val:
        re_path = inter_agent_dir / re_val
        if not re_path.exists():
            errors.append(
                f"ia {context}: invalid frontmatter — `re: {re_val}` references "
                f"non-existent file (looked in {inter_agent_dir}). "
                f"See inter-agent/README.md §2."
            )

    return errors


# ---------------------------------------------------------------------------
# Letter scanning
# ---------------------------------------------------------------------------

def _is_recipient(to_field: str, agent_name: str) -> bool:
    """Return True iff agent_name is a whole-name match in the to: field.

    Membership test: split on commas, strip whitespace, exact match — NOT
    substring. "mira" does not match "miranda"; "ariadne" does not match a
    to: field of "aria, borges".

    Both to_field and agent_name are compared case-insensitively.

    Examples:
      _is_recipient("ariadne", "ariadne")          -> True
      _is_recipient("ariadne, borges", "borges")   -> True
      _is_recipient("ariadne, borges", "aria")     -> False
      _is_recipient("mira", "mir")                 -> False
    """
    if not agent_name or not to_field:
        return False
    agent_lower = agent_name.strip().lower()
    for name in to_field.split(","):
        if name.strip().lower() == agent_lower:
            return True
    return False


def _scan_letters(agent_name: str, inter_agent_dir: Path) -> tuple:
    """Scan inter_agent_dir for letters addressed to agent_name.

    Returns (letters, skipped_count) where:
      letters      — list of dicts: {path, filename, timestamp, from, title, fields}
      skipped_count — count of .md files dropped due to unparseable frontmatter
    """
    if not inter_agent_dir.is_dir():
        return [], 0

    letters = []
    skipped = 0
    for md_file in inter_agent_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue

        fields, body = _parse_frontmatter(text)
        if not fields:
            skipped += 1
            continue

        to_field = fields.get("to", "").strip()
        if not _is_recipient(to_field, agent_name):
            continue

        ts = _parse_letter_timestamp(fields.get("timestamp", ""))
        if ts is None:
            skipped += 1
            continue

        title = _extract_title(fields, body)
        from_field = fields.get("from", "unknown").strip()

        letters.append({
            "path": str(md_file),
            "filename": md_file.name,
            "timestamp": ts,
            "from": from_field,
            "title": title,
            "fields": fields,
        })

    letters.sort(key=lambda l: l["timestamp"])
    return letters, skipped


def _filter_unread(letters: list, read_cursor: Optional[datetime]) -> list:
    """Return letters with timestamp > read_cursor (or all if cursor is None)."""
    if read_cursor is None:
        return letters
    return [l for l in letters if l["timestamp"] > read_cursor]


def _scan_replied_filenames(agent_name: str, inter_agent_dir: Path) -> set:
    """Scan inter_agent_dir for outgoing letters (from: agent_name) with a re: field.

    Returns the set of all filenames referenced by `re:` across outgoing letters.
    Graceful on malformed letters (skips silently — frontmatter errors are not
    load-bearing for this scan).
    """
    if not inter_agent_dir.is_dir():
        return set()

    replied = set()
    for md_file in inter_agent_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        fields, _ = _parse_frontmatter(text)
        if not fields:
            continue

        from_field = fields.get("from", "").strip().lower()
        if not agent_name or from_field != agent_name.lower():
            continue

        re_field = fields.get("re", "").strip()
        if re_field:
            replied.add(re_field)

    return replied


# ---------------------------------------------------------------------------
# Starred letters helpers  (per-agent star list, pointer-not-injection)
# ---------------------------------------------------------------------------

STARRED_CAP = 10  # max entries rendered in the starred surface block
STARRED_STALE_DAYS = 7  # soft TTL for staleness nudge (nudge only, never auto-drop)


def _read_starred(starred_path: str = STARRED_LIST_PATH) -> list:
    """Read the starred-letters list; return [] on missing/corrupt file.

    Each entry is a dict with at minimum {"filename": ..., "starred_at": ..., "note": ...}.
    Snapshot entries (added at star-time) also carry {"from": ..., "title": ...} so the
    list surface can render without re-parsing the source letter.
    Tolerates any read or parse failure (missing file, invalid JSON, non-list shape).
    """
    try:
        raw = Path(starred_path).read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _write_starred(entries: list, starred_path: str = STARRED_LIST_PATH) -> None:
    """Write the starred-letters list atomically (tmp + os.replace).

    Uses the same atomic tmp + os.replace pattern as _write_cursor so concurrent
    hook fires cannot read a partial starred file.
    """
    dest = Path(starred_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        os.replace(tmp, dest)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"ia: starred-list write failed: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)


# ---------------------------------------------------------------------------
# $EDITOR helpers  (DESIGN §9.2)
# ---------------------------------------------------------------------------

def _find_editor() -> Optional[str]:
    """Resolve the editor to use: $VISUAL -> $EDITOR -> nano -> vi.

    Returns the editor command string, or None if none are available.
    """
    for envvar in ("VISUAL", "EDITOR"):
        val = os.environ.get(envvar, "").strip()
        if val:
            return val

    for fallback in ("nano", "vi"):
        if shutil.which(fallback) is not None:
            return fallback

    return None


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    inter_agent_dir = Path(INTER_AGENT_DIR)
    all_letters, skipped_count = _scan_letters(agent_name, inter_agent_dir)

    # Apply --from filter
    if args.from_agent:
        all_letters = [l for l in all_letters if l["from"].lower() == args.from_agent.lower()]

    # Apply --since filter
    since_dt: Optional[datetime] = None
    if args.since:
        since_dt = _parse_iso_timestamp(args.since, context="list")
        all_letters = [l for l in all_letters if l["timestamp"] > since_dt]

    # Apply --all vs --unread (default unread)
    if not args.all:
        read_cursor = _read_cursor(READ_CURSOR_PATH)
        all_letters = _filter_unread(all_letters, read_cursor)

    # Apply --limit
    if args.limit is not None:
        all_letters = all_letters[:args.limit]

    if args.format == "json":
        output = []
        for l in all_letters:
            output.append({
                "timestamp": l["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "from": l["from"],
                "title": l["title"],
                "path": l["path"],
                "filename": l["filename"],
            })
        print(json.dumps(output, indent=2))
        if skipped_count > 0:
            print(
                f"note: skipped {skipped_count} file(s) with unparseable frontmatter",
                file=sys.stderr,
            )
        return

    # Compute which incoming letters have been replied to (outgoing re: references)
    replied_set = _scan_replied_filenames(agent_name, inter_agent_dir)

    # Human format
    label = "ALL" if args.all else "UNREAD"
    count = len(all_letters)
    if count == 0:
        print(f"{label} (0): no letters found.")
    else:
        print(f"{label} ({count}):")
        for l in all_letters:
            ts_str = _format_ts(l["timestamp"])
            replied_marker = "  [REPLIED]" if l["filename"] in replied_set else ""
            print(f"  {ts_str}  {l['from']:<12}  \"{l['title']}\"{replied_marker}")
            print(f"    {l['filename']}")

    if skipped_count > 0:
        print(
            f"note: skipped {skipped_count} file(s) with unparseable frontmatter",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Subcommand: read
# ---------------------------------------------------------------------------

def cmd_read(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    inter_agent_dir = Path(INTER_AGENT_DIR)

    if args.latest:
        # Find the most recent unread letter
        all_letters, _ = _scan_letters(agent_name, inter_agent_dir)
        read_cursor = _read_cursor(READ_CURSOR_PATH)
        unread = _filter_unread(all_letters, read_cursor)
        if not unread:
            print("ia read: no unread letters.")
            sys.exit(EXIT_OK)
        letter_info = unread[-1]  # most recent
        letter_path = Path(letter_info["path"])
    else:
        # Resolve the named letter
        filename = args.filename
        # Accept bare filename or full path
        candidate = Path(filename)
        if not candidate.is_absolute():
            candidate = inter_agent_dir / candidate
        if not candidate.exists():
            print(
                f"ia read: file not found: '{filename}' "
                f"(looked in {inter_agent_dir}). "
                f"Use 'ia list' to see available letters. "
                f"See inter-agent/README.md §4.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        letter_path = candidate
        try:
            text = letter_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"ia read: cannot read '{letter_path}': {e}", file=sys.stderr)
            sys.exit(EXIT_IO)
        fields, body = _parse_frontmatter(text)
        if not fields:
            letter_info = {"timestamp": None}
        else:
            ts = _parse_letter_timestamp(fields.get("timestamp", ""))
            letter_info = {"timestamp": ts, "fields": fields}

    # Print the letter
    try:
        text = letter_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"ia read: cannot read '{letter_path}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    print(text, end="" if text.endswith("\n") else "\n")

    # Advance read cursor to letter's timestamp (monotonic; --force overrides)
    ts = letter_info.get("timestamp")
    if ts is not None:
        current_cursor = _read_cursor(READ_CURSOR_PATH)
        if current_cursor is not None and ts < current_cursor and not args.force:
            # Letter is older than cursor — don't retreat
            print(
                "note: read cursor not advanced — letter is older than current cursor "
                "(use --force to advance)",
                file=sys.stderr,
            )
        else:
            _write_cursor(READ_CURSOR_PATH, ts)


# ---------------------------------------------------------------------------
# Subcommand: write
# ---------------------------------------------------------------------------

def cmd_write(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    inter_agent_dir = Path(INTER_AGENT_DIR)

    # Handle --reply FILENAME: resolve source, auto-fill --to, set --re
    if args.reply:
        reply_filename = args.reply
        reply_path = inter_agent_dir / reply_filename
        if not reply_path.exists():
            print(
                f"ia write: --reply: source letter '{reply_filename}' not found "
                f"(looked in {inter_agent_dir}). "
                f"Use 'ia list --all' to see available letters.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        try:
            reply_text = reply_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"ia write: --reply: cannot read '{reply_path}': {e}", file=sys.stderr)
            sys.exit(EXIT_IO)
        reply_fields, _ = _parse_frontmatter(reply_text)
        if not reply_fields:
            print(
                f"ia write: --reply: cannot parse frontmatter from '{reply_filename}'. "
                f"See inter-agent/README.md §2.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        source_from = reply_fields.get("from", "").strip()
        if not source_from:
            print(
                f"ia write: --reply: source letter '{reply_filename}' has no `from:` field.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        # If --to was explicitly given, verify it matches the source's sender
        if args.to is not None and args.to != source_from:
            print(
                f"ia write: --reply conflict: source letter is from '{source_from}' "
                f"but --to '{args.to}' was also specified. "
                f"Remove --to to reply to '{source_from}', or use "
                f"'--re {reply_filename} --to {args.to}' to reference the letter "
                f"while writing to a different recipient.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        # Auto-fill --to from source and set --re
        args.to = source_from
        args.re = reply_filename

    # Guard: --to must be set by now (either directly or via --reply)
    if not args.to:
        print(
            "ia write: --to is required (or use --reply FILENAME to auto-fill from source).",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Normalize --to: trim whitespace around commas; canonical form is "name1, name2".
    # Applies to both single-recipient ("borges") and multi-recipient ("ariadne, borges").
    args.to = ", ".join(n.strip() for n in args.to.split(",") if n.strip())
    if not args.to:
        print(
            "ia write: --to value is empty after stripping; specify at least one recipient.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Build the timestamp for the new letter
    now = datetime.now(timezone.utc)
    ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build filename: <ISO-dashes>_<agent-name>.md
    ts_dashes = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    filename = f"{ts_dashes}_{agent_name}.md"
    dest_path = inter_agent_dir / filename

    # Build frontmatter
    fm_lines = [
        "---",
        f"from: {agent_name}",
        f"to: {args.to}",
        f"timestamp: {ts_str}",
    ]
    if args.re:
        fm_lines.append(f"re: {args.re}")
    fm_lines.append("---")
    fm_lines.append("")
    if args.title:
        fm_lines.append(f"# {args.title}")
        fm_lines.append("")

    template = "\n".join(fm_lines) + "\n"

    # Get body
    if args.body_file and args.from_stdin:
        print(
            "ia write: --body-file and --from-stdin are mutually exclusive; "
            "use one or the other.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    if args.body_file:
        body_path = Path(args.body_file)
        if not body_path.exists():
            print(
                f"ia write: --body-file: '{args.body_file}' not found.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        try:
            body = body_path.read_text(encoding="utf-8")
        except OSError as e:
            print(
                f"ia write: --body-file: cannot read '{args.body_file}': {e}",
                file=sys.stderr,
            )
            sys.exit(EXIT_IO)
        if body.lstrip().startswith("---"):
            print(
                "ia write: warning: --body-file content starts with '---'; "
                "frontmatter is auto-assembled, so this will appear as literal "
                "body text. Supply ONLY the body.",
                file=sys.stderr,
            )
        full_content = template + body
    elif args.from_stdin:
        # Stdin mode: read the full letter content from stdin.
        # The caller is expected to provide complete content (frontmatter + body).
        full_content = sys.stdin.read()
    else:
        # Open $EDITOR
        editor = _find_editor()
        if editor is None:
            print(
                "ia write: no editor available — set $EDITOR or install nano/vi.",
                file=sys.stderr,
            )
            sys.exit(EXIT_STATE)

        # Write template to a temp file and open the editor
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            prefix="ia-write-",
            delete=False,
            dir="/tmp",
        ) as tf:
            tf.write(template)
            tmp_name = tf.name

        try:
            result = subprocess.run([editor, tmp_name])
            if result.returncode != 0:
                print(
                    f"ia write: editor '{editor}' exited with code {result.returncode}.",
                    file=sys.stderr,
                )
                sys.exit(EXIT_IO)
            with open(tmp_name, encoding="utf-8") as f:
                full_content = f.read()
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    # Pre-parse guard: catches bare 'to:' (empty value) before the regex parser's
    # merge quirk, which would otherwise silently misroute the letter. Runs for
    # --from-stdin, --body-file, and editor mode (the user can clear the 'to:'
    # field in the editor, or a body file can supply incomplete content).
    fm_raw_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", full_content, re.DOTALL)
    if fm_raw_match:
        fm_raw = fm_raw_match.group(1)
        if re.search(r"^to:\s*$", fm_raw, re.MULTILINE):
            print(
                "ia write: invalid frontmatter — 'to:' line is empty; "
                "specify a recipient.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)

    # Validate the frontmatter of what's been written
    fields, body_text = _parse_frontmatter(full_content)
    if not fields:
        print(
            "ia write: could not parse frontmatter from the letter. "
            "Make sure the letter starts with --- ... --- block. "
            "See inter-agent/README.md §2.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    errors = _validate_frontmatter(fields, agent_name, inter_agent_dir, context="write")
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    # Warn if body is empty
    if not body_text.strip():
        print(
            "ia write: warning — letter body is empty. Sending anyway.",
            file=sys.stderr,
        )

    # Atomic write: tmp + os.link (collision-safe), then chmod 644.
    #
    # Why os.link instead of os.rename:
    #   os.rename(src, dst) silently replaces dst on POSIX — so two `ia write`
    #   calls by the same agent in the same second produce the same dest_path and
    #   the second clobbers the first (silent data loss).  os.link(src, dst) is
    #   atomic AND raises FileExistsError if dst already exists, so it is race-free
    #   unlike a check-then-rename (TOCTOU).  On collision we bump a numeric suffix
    #   (-2, -3, …) and retry, capping at _LINK_MAX_ATTEMPTS.
    #
    # Suffix scheme: first letter keeps the clean name <ts>_<agent>.md; on
    # collision use <ts>_<agent>-2.md, -3.md, etc.
    if not inter_agent_dir.is_dir():
        print(
            f"ia write: inter-agent dir not found: {inter_agent_dir}. "
            f"See inter-agent/README.md §8.",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)

    _LINK_MAX_ATTEMPTS = 100
    tmp_path = inter_agent_dir / (filename + ".tmp")
    try:
        tmp_path.write_text(full_content, encoding="utf-8")
    except OSError as e:
        print(f"ia write: failed to write letter: {e}", file=sys.stderr)
        sys.exit(EXIT_IO)

    # Attempt to claim a unique destination name.
    # First attempt: the clean name.  On FileExistsError, bump suffix.
    base, ext = filename[:-3], ".md"  # split "2026-…T…Z_name.md" → ("2026-…T…Z_name", ".md")
    claimed_path = None
    for attempt in range(1, _LINK_MAX_ATTEMPTS + 1):
        candidate_name = filename if attempt == 1 else f"{base}-{attempt}{ext}"
        candidate_path = inter_agent_dir / candidate_name
        try:
            os.link(str(tmp_path), str(candidate_path))
            claimed_path = candidate_path
            break
        except FileExistsError:
            continue
        except OSError as e:
            # Unexpected error (e.g. cross-device, permissions) — clean up and abort
            print(f"ia write: failed to publish letter: {e}", file=sys.stderr)
            try:
                tmp_path.unlink()
            except OSError:
                pass
            sys.exit(EXIT_IO)

    try:
        tmp_path.unlink()
    except OSError:
        pass

    if claimed_path is None:
        print(
            f"ia write: collision loop exhausted after {_LINK_MAX_ATTEMPTS} attempts "
            f"(all candidate names already exist — this should not happen in normal use).",
            file=sys.stderr,
        )
        sys.exit(EXIT_IO)

    try:
        os.chmod(claimed_path, 0o644)
    except OSError as e:
        print(f"ia write: warning — could not set permissions on letter: {e}", file=sys.stderr)

    print(f"ia write: sent → {claimed_path.name}")


# ---------------------------------------------------------------------------
# Subcommand: mark-read
# ---------------------------------------------------------------------------

def cmd_mark_read(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    inter_agent_dir = Path(INTER_AGENT_DIR)
    current_cursor = _read_cursor(READ_CURSOR_PATH)

    if args.all:
        # Advance cursor to now
        new_ts = datetime.now(timezone.utc)
    elif args.up_to:
        new_ts = _parse_iso_timestamp(args.up_to, context="mark-read")
    else:
        # Resolve letter filename
        filename = args.filename
        candidate = Path(filename)
        if not candidate.is_absolute():
            candidate = inter_agent_dir / candidate
        if not candidate.exists():
            print(
                f"ia mark-read: file not found: '{filename}' "
                f"(looked in {inter_agent_dir}). "
                f"See inter-agent/README.md §4.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"ia mark-read: cannot read '{candidate}': {e}", file=sys.stderr)
            sys.exit(EXIT_IO)
        fields, _ = _parse_frontmatter(text)
        ts_str = fields.get("timestamp", "")
        new_ts = _parse_letter_timestamp(ts_str)
        if new_ts is None:
            print(
                f"ia mark-read: cannot parse timestamp from '{filename}'. "
                f"Expected '2026-05-22T14:30:00Z'. "
                f"See inter-agent/README.md §2.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)

    # Monotonic check (unless --force)
    if current_cursor is not None and new_ts < current_cursor and not args.force:
        print(
            f"ia mark-read: refusing to move cursor backward "
            f"(current: {_cursor_str(current_cursor)}, requested: {_cursor_str(new_ts)}). "
            f"Pass --force to override (e.g., cursor recovery). "
            f"See inter-agent/README.md §4.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    _write_cursor(READ_CURSOR_PATH, new_ts)
    print(f"ia mark-read: cursor advanced to {_cursor_str(new_ts)}")


# ---------------------------------------------------------------------------
# Subcommand: cursor
# ---------------------------------------------------------------------------

def cmd_cursor(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    # --type requires --advanced
    if hasattr(args, "type") and args.type and not getattr(args, "advanced", False):
        print(
            "ia cursor: --type requires --advanced. "
            "If you're sure you want to touch the hook's surfaced cursor, "
            "pass --advanced --type surfaced. "
            "See inter-agent/README.md §4.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Determine which cursor to operate on
    cursor_type = getattr(args, "type", None) or "read"
    if cursor_type == "surfaced":
        cursor_path = SURFACED_CURSOR_PATH
    else:
        cursor_path = READ_CURSOR_PATH

    if args.set_ts:
        # Set the cursor (with monotonic check)
        new_ts = _parse_iso_timestamp(args.set_ts, context="cursor")
        current = _read_cursor(cursor_path)

        if current is not None and new_ts < current and not args.force:
            print(
                f"ia cursor: refusing to set cursor backward "
                f"(current: {_cursor_str(current)}, requested: {_cursor_str(new_ts)}). "
                f"Pass --force to override. "
                f"See inter-agent/README.md §4.",
                file=sys.stderr,
            )
            sys.exit(EXIT_VALIDATION)

        _write_cursor(cursor_path, new_ts)
        print(f"ia cursor: {cursor_type} cursor set to {_cursor_str(new_ts)}")
    else:
        # Show cursor (default)
        ts = _read_cursor(cursor_path)
        print(f"{cursor_type} cursor: {_cursor_str(ts)}")


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    inter_agent_dir = Path(INTER_AGENT_DIR)

    mode = config.get("mode", "single")
    read_cursor = _read_cursor(READ_CURSOR_PATH)
    surfaced_cursor = _read_cursor(SURFACED_CURSOR_PATH)

    # Check dir writability
    dir_exists = inter_agent_dir.is_dir()
    dir_writable = os.access(inter_agent_dir, os.W_OK) if dir_exists else False
    dir_status = "writable" if dir_writable else ("exists (not writable)" if dir_exists else "NOT FOUND")

    # Count unread (scan once; reuse for correspondents below)
    if dir_exists and agent_name:
        all_letters, _ = _scan_letters(agent_name, inter_agent_dir)
        unread = _filter_unread(all_letters, read_cursor)
        unread_count = len(unread)
        # Unreplied: all incoming letters (any time) with no outgoing re: reference
        replied_set = _scan_replied_filenames(agent_name, inter_agent_dir)
        unreplied_count = sum(
            1 for l in all_letters if l["filename"] not in replied_set
        )
    else:
        all_letters = []
        unread_count = 0
        replied_set = set()
        unreplied_count = 0

    # Unique senders seen in letters to me (recently-active proxy; DESIGN §9.3 note)
    active_correspondents: list = []
    if dir_exists and agent_name:
        seen_from = sorted({l["from"] for l in all_letters})
        active_correspondents = seen_from

    now = datetime.now(timezone.utc)

    def _age_str(ts: Optional[datetime]) -> str:
        if ts is None:
            return ""
        delta = now - ts
        total_secs = int(delta.total_seconds())
        if total_secs < 60:
            return f" ({total_secs}s ago)"
        elif total_secs < 3600:
            return f" ({total_secs // 60} min ago)"
        elif total_secs < 86400:
            return f" ({total_secs // 3600}h ago)"
        else:
            return f" ({total_secs // 86400}d ago)"

    if args.format == "json":
        output = {
            "agent": agent_name or "(unknown)",
            "mode": mode,
            "inter_agent_dir": str(inter_agent_dir),
            "dir_writable": dir_writable,
            "read_cursor": _cursor_str(read_cursor),
            "surfaced_cursor": _cursor_str(surfaced_cursor),
            "unread_letters": unread_count,
            "unreplied_letters": unreplied_count,
            "active_correspondents": active_correspondents,
        }
        print(json.dumps(output, indent=2))
        return

    # Human format
    print(f"agent:           {agent_name or '(unknown)'}")
    print(f"mode:            {mode}")
    print(f"inter-agent-dir: {inter_agent_dir} ({dir_status})")
    print(f"read cursor:     {_cursor_str(read_cursor)}{_age_str(read_cursor)}")
    print(f"surfaced cursor: {_cursor_str(surfaced_cursor)}{_age_str(surfaced_cursor)}")
    print(f"unread letters:  {unread_count}")
    if unreplied_count > 0:
        print(f"unreplied:       {unreplied_count}")
    if active_correspondents:
        print(f"correspondents:  {', '.join(active_correspondents)}")
    else:
        print(f"correspondents:  (none)")


# ---------------------------------------------------------------------------
# Subcommand: star
# ---------------------------------------------------------------------------

def cmd_star(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    inter_agent_dir = Path(INTER_AGENT_DIR)

    filename = args.filename
    # Validate the filename exists in inter-agent/ and parse it once to snapshot from+title
    candidate = Path(filename)
    if not candidate.is_absolute():
        candidate = inter_agent_dir / filename
    if not candidate.exists():
        print(
            f"ia star: file not found: '{filename}' "
            f"(looked in {inter_agent_dir}). "
            f"Use 'ia list --all' to see available letters.",
            file=sys.stderr,
        )
        sys.exit(EXIT_VALIDATION)

    # Parse letter once (validation + snapshot)
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"ia star: cannot read '{candidate}': {e}", file=sys.stderr)
        sys.exit(EXIT_IO)
    fields, body = _parse_frontmatter(text)
    snapshot_from = fields.get("from", "unknown").strip() if fields else "unknown"
    snapshot_title = _extract_title(fields, body) if fields else "(no title)"

    # Use basename only in the starred list (not full path)
    if Path(filename).is_absolute():
        filename = Path(filename).name

    entries = _read_starred()
    note = args.note or ""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Idempotent: update note (and refresh snapshot fields) if already starred
    for entry in entries:
        if entry.get("filename") == filename:
            if note:
                entry["note"] = note
            # Refresh snapshot fields in case letter was edited since last star
            entry["from"] = snapshot_from
            entry["title"] = snapshot_title
            print(f"ia star: '{filename}' already starred (updated note)" if note else
                  f"ia star: '{filename}' already starred (no-op)")
            _write_starred(entries)
            return

    entries.append({
        "filename": filename,
        "from": snapshot_from,
        "title": snapshot_title,
        "starred_at": now_str,
        "note": note,
    })
    _write_starred(entries)
    print(f"ia star: starred '{filename}'")


# ---------------------------------------------------------------------------
# Subcommand: unstar
# ---------------------------------------------------------------------------

def cmd_unstar(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    filename = args.filename
    # Normalize to basename
    filename = Path(filename).name

    entries = _read_starred()
    new_entries = [e for e in entries if e.get("filename") != filename]

    if len(new_entries) == len(entries):
        print(f"ia unstar: '{filename}' was not starred (no-op)")
        return

    _write_starred(new_entries)
    print(f"ia unstar: unstarred '{filename}'")


# ---------------------------------------------------------------------------
# Subcommand: starred
# ---------------------------------------------------------------------------

def cmd_starred(args: argparse.Namespace, config: dict, agent_name: str) -> None:
    inter_agent_dir = Path(INTER_AGENT_DIR)

    entries = _read_starred()

    if args.format == "json":
        print(json.dumps(entries, indent=2))
        return

    # Human format: show each entry with from · title · filename · note · staleness
    if not entries:
        print("starred (0): no starred letters.")
        return

    now = datetime.now(timezone.utc)
    print(f"starred ({len(entries)}):")
    skipped = 0
    shown = 0
    missing_count = 0
    for entry in entries:
        filename = entry.get("filename", "").strip()
        if not filename:
            skipped += 1
            continue

        # Read from/title from snapshot; graceful fallback for old entries lacking snapshot fields
        from_agent = (entry.get("from") or "").strip() or "unknown"
        title = (entry.get("title") or "").strip() or "(no title)"
        note = entry.get("note", "").strip()
        starred_at_str = entry.get("starred_at", "")

        # Check source letter existence (warn but still render from snapshot)
        letter_path = inter_agent_dir / filename
        if not letter_path.exists():
            missing_count += 1

        note_part = f"  note: {note}" if note else ""
        missing_part = "  [source letter no longer present]" if not letter_path.exists() else ""

        # Staleness nudge
        stale_part = ""
        if starred_at_str:
            try:
                starred_dt = datetime.strptime(starred_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                age_days = (now - starred_dt).days
                if age_days >= STARRED_STALE_DAYS:
                    stale_part = f"  ⚠ stale {age_days}d — unstar if resolved"
            except ValueError:
                pass

        print(f"  {from_agent:<12}  \"{title}\"")
        print(f"    {filename}{note_part}{missing_part}{stale_part}")
        shown += 1

    if missing_count > 0:
        print(
            f"note: {missing_count} starred letter(s) no longer exist in {inter_agent_dir}",
            file=sys.stderr,
        )
    if skipped > 0:
        print(
            f"note: {skipped} starred entry/entries skipped (missing filename field)",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ia",
        description=(
            "Inter-agent CLI — list, read, and write letters over the "
            "file-based inter-agent protocol."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Protocol docs: inter-agent/README.md\n"
            "Design doc:    ariadne-desk/projects/inter-agent-cli/DESIGN.md\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="subcommand")
    subparsers.required = True

    # -- list --
    p_list = subparsers.add_parser(
        "list",
        help="Show letters addressed to me (default: unread only)",
        description=(
            "List letters addressed to this agent. "
            "Defaults to unread (timestamp > read cursor). "
            "Use --all to see every letter."
        ),
    )
    list_mode = p_list.add_mutually_exclusive_group()
    list_mode.add_argument(
        "--all", action="store_true", default=False,
        help="Show all letters (not just unread)",
    )
    list_mode.add_argument(
        "--unread", action="store_true", default=False,
        help="Show only unread letters (default)",
    )
    p_list.add_argument(
        "--from", dest="from_agent", metavar="AGENT",
        help="Filter to letters from a specific sender",
    )
    p_list.add_argument(
        "--since", metavar="ISO-TIMESTAMP",
        help="Filter to letters after this timestamp (ISO-8601, e.g. '2026-05-22T14:00:00Z')",
    )
    p_list.add_argument(
        "--limit", type=int, metavar="N",
        help="Cap output to N letters",
    )
    p_list.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # -- read --
    p_read = subparsers.add_parser(
        "read",
        help="Display a letter; advance read cursor to its timestamp",
        description=(
            "Print a letter's full contents (frontmatter + body) to stdout "
            "and advance the read cursor to the letter's timestamp."
        ),
    )
    read_target = p_read.add_mutually_exclusive_group(required=True)
    read_target.add_argument(
        "filename", nargs="?", default=None,
        metavar="FILENAME",
        help="Letter filename (full path or basename from inter-agent/)",
    )
    read_target.add_argument(
        "--latest", action="store_true",
        help="Read the most recent unread letter",
    )
    p_read.add_argument(
        "--force", action="store_true",
        help="Advance cursor even if it would move backward (cursor recovery)",
    )

    # -- write --
    p_write = subparsers.add_parser(
        "write",
        help="Create a new letter via $EDITOR with validated frontmatter",
        description=(
            "Open $EDITOR (or $VISUAL → nano → vi fallback) with a pre-populated "
            "frontmatter template, validate on save, and write atomically to "
            "inter-agent/."
        ),
    )
    p_write.add_argument(
        "--to", required=False, default=None, metavar="AGENT[,AGENT,...]",
        help=(
            "Recipient(s) — one name or a comma-separated list (e.g. 'ariadne,borges'). "
            "Whitespace around commas is tolerated. ONE file is written regardless of "
            "recipient count; the to: frontmatter line carries all names. "
            "Required unless --reply is used (which auto-fills --to from the source letter)."
        ),
    )
    p_write.add_argument(
        "--re", metavar="FILENAME",
        help="Filename of the letter being replied to (must exist in inter-agent/)",
    )
    p_write.add_argument(
        "--reply", metavar="FILENAME",
        help=(
            "Convenience shorthand: reply to a specific letter by filename. "
            "Auto-fills --to from the source letter's `from:` field and populates `re:`. "
            "If --to is also given, it must match the source's sender or the command errors out."
        ),
    )
    p_write.add_argument(
        "--title", metavar="TEXT",
        help="Optional # Heading for the letter body",
    )
    p_write.add_argument(
        "--from-stdin", action="store_true",
        help=(
            "Read full letter content from stdin instead of opening $EDITOR. "
            "(filename timestamp is always send-time; if your stdin frontmatter "
            "specifies a different timestamp, they will diverge by design — "
            "frontmatter ts is authoritative for readers.)"
        ),
    )
    p_write.add_argument(
        "--body-file", metavar="PATH",
        help=(
            "Path to a file containing ONLY the letter body; "
            "frontmatter (from/to/timestamp) is auto-assembled. "
            "Body is read verbatim (no shell processing) — the safe path "
            "for content with backticks/$vars/angle-brackets. "
            "Mutually exclusive with --from-stdin."
        ),
    )

    # -- mark-read --
    p_mr = subparsers.add_parser(
        "mark-read",
        help="Advance read cursor without displaying a letter",
        description=(
            "Advance the read cursor (marks letters as acknowledged) "
            "without printing them. Cursor is monotonic — pass --force "
            "to move it backward (recovery scenarios)."
        ),
    )
    mr_target = p_mr.add_mutually_exclusive_group(required=True)
    mr_target.add_argument(
        "filename", nargs="?", default=None,
        metavar="FILENAME",
        help="Advance cursor to this letter's timestamp",
    )
    mr_target.add_argument(
        "--all", action="store_true",
        help="Advance cursor to now (clears all unread)",
    )
    mr_target.add_argument(
        "--up-to", metavar="ISO-TIMESTAMP",
        help="Advance cursor to a specific timestamp",
    )
    p_mr.add_argument(
        "--force", action="store_true",
        help="Allow cursor to move backward (recovery scenarios)",
    )

    # -- cursor --
    p_cursor = subparsers.add_parser(
        "cursor",
        help="Show or set the read cursor",
        description=(
            "Show or set the inter-agent read cursor. "
            "The cursor stores an ISO-8601 UTC timestamp — not a filename. "
            "Cursor advance is monotonic by default; use --force for recovery."
        ),
    )
    cursor_action = p_cursor.add_mutually_exclusive_group()
    cursor_action.add_argument(
        "--show", action="store_true",
        help="Print current cursor value (default)",
    )
    cursor_action.add_argument(
        "--set", dest="set_ts", metavar="ISO-TIMESTAMP",
        help="Set cursor to this timestamp (ISO-8601, e.g. '2026-05-22T14:30:00Z')",
    )
    p_cursor.add_argument(
        "--advanced", action="store_true",
        help="Enable advanced cursor operations (required for --type surfaced)",
    )
    p_cursor.add_argument(
        "--type", dest="type", choices=["read", "surfaced"], default=None,
        help=(
            "Which cursor to operate on: 'read' (default) or 'surfaced' "
            "(hook-managed; requires --advanced)."
        ),
    )
    p_cursor.add_argument(
        "--force", action="store_true",
        help="Allow cursor to move backward (recovery scenarios)",
    )

    # -- status --
    p_status = subparsers.add_parser(
        "status",
        help="Quick health check (agent name, mode, cursor, unread count)",
        description=(
            "Show a quick summary of inter-agent state: "
            "agent identity, mode, directory writability, cursor values, "
            "and unread letter count."
        ),
    )
    p_status.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    # -- star --
    p_star = subparsers.add_parser(
        "star",
        help="Star a letter to re-surface it at session-start and post-compaction",
        description=(
            "Mark a letter as key cross-session context. Starred letters are "
            "surfaced as concise pointers at session-start and post-compaction "
            "so important agreements survive experiential resets. "
            "Idempotent: starring an already-starred letter updates the note "
            "or is a no-op."
        ),
    )
    p_star.add_argument(
        "filename",
        metavar="FILENAME",
        help="Letter filename (basename or full path in inter-agent/)",
    )
    p_star.add_argument(
        "--note", metavar="TEXT", default="",
        help="Optional note describing why this letter is starred",
    )

    # -- unstar --
    p_unstar = subparsers.add_parser(
        "unstar",
        help="Remove a letter from the starred list",
        description=(
            "Remove a letter from the starred list. No-op if the letter is not "
            "currently starred."
        ),
    )
    p_unstar.add_argument(
        "filename",
        metavar="FILENAME",
        help="Letter filename (basename or full path) to unstar",
    )

    # -- starred --
    p_starred = subparsers.add_parser(
        "starred",
        help="List starred letters with from · title · filename · note",
        description=(
            "Show all starred letters with their sender, title, filename, and note. "
            "Warns if a starred letter's file no longer exists."
        ),
    )
    p_starred.add_argument(
        "--format", choices=["human", "json"], default="human",
        help="Output format: human (default) or json",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = _load_config()

    # Mode gate for all subcommands except status
    # (status shows single-mode state to help diagnose misconfiguration)
    if args.subcommand != "status":
        _check_multi_agent_mode(config)

    agent_name = _get_agent_name(config)

    # Dispatch
    if args.subcommand == "list":
        cmd_list(args, config, agent_name)
    elif args.subcommand == "read":
        cmd_read(args, config, agent_name)
    elif args.subcommand == "write":
        cmd_write(args, config, agent_name)
    elif args.subcommand == "mark-read":
        cmd_mark_read(args, config, agent_name)
    elif args.subcommand == "cursor":
        cmd_cursor(args, config, agent_name)
    elif args.subcommand == "status":
        cmd_status(args, config, agent_name)
    elif args.subcommand == "star":
        cmd_star(args, config, agent_name)
    elif args.subcommand == "unstar":
        cmd_unstar(args, config, agent_name)
    elif args.subcommand == "starred":
        cmd_starred(args, config, agent_name)
    else:
        parser.print_help()
        sys.exit(EXIT_VALIDATION)


if __name__ == "__main__":
    main()
