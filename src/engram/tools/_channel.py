"""_channel — shared helpers for the inter-agent filesystem channel.

Centralizes the substance/pulse/mirror filename conventions, frontmatter
parsing, and atomic file writes used by:
- telegram_bot.py (cross-post mirror writes)
- telegram_dispatcher.py (channel writes for failure status)
- channel_dispatcher.py (future: hot-seat real-time dispatch)

The single-source-of-truth principle (Lei 2026-04-29): any change to
filename conventions, frontmatter shape, or write semantics lands here
and propagates everywhere.

## Filename conventions

Per inter-agent channel substrate at /home/agents-shared/inter-agent/:

    <UTC>_<sender>.md                  — substance message (frontmatter + body)
    <UTC>_<sender>_pulse.md            — liveness pulse (frontmatter only)
    <UTC>_<sender>_telegram-mirror.md  — cross-post of Telegram outbound

Where <UTC> is "%Y-%m-%dT%H-%M-%SZ" (lexically-sortable; hyphens not colons
because filename-safe).

Substance types (count for pulse + dispatch logic):
- Native substance (.md without _pulse / _telegram-mirror suffix)
- Telegram mirror (cross-posted real outbound)

Excluded from substance:
- Pulse files (no message body, just liveness)
- Anything not matching the above patterns
"""

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# --- timestamp helpers (also in _common.py for env helpers; channel-side here) ---

def utc_filename_timestamp() -> str:
    """UTC timestamp formatted for filenames: 2026-04-29T15-37-00Z (hyphens, sortable)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def utc_iso_timestamp() -> str:
    """UTC timestamp in ISO-8601 for frontmatter: 2026-04-29T15:37:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- filename classification ---

def is_substance_filename(name: str, sender: str) -> bool:
    """True if filename is a real substance message from sender.

    Substance forms (count for pulse + dispatch purposes):
        <UTC>_<sender>.md                  — native filesystem-channel writes
        <UTC>_<sender>_telegram-mirror.md  — cross-posted Telegram outbound

    Excluded:
        <UTC>_<sender>_pulse.md            — pure liveness signal, no substance
        any non-.md or non-matching        — unrelated
    """
    if not name.endswith(".md"):
        return False
    if name.endswith(f"_{sender}_pulse.md"):
        return False
    if name.endswith(f"_{sender}.md"):
        return True
    if name.endswith(f"_{sender}_telegram-mirror.md"):
        return True
    return False


def is_pulse_filename(name: str, sender: str) -> bool:
    """True if filename is a liveness pulse from sender."""
    return name.endswith(f"_{sender}_pulse.md")


def is_mirror_filename(name: str, sender: str) -> bool:
    """True if filename is a Telegram cross-post mirror from sender."""
    return name.endswith(f"_{sender}_telegram-mirror.md")


# --- channel scanning ---

def their_latest_substance(channel_dir: Path, counterpart: str) -> Optional[str]:
    """Most recent substance message filename from the counterpart, lexically-sortable.

    Returns None if no substance from counterpart exists.
    """
    if not channel_dir.exists():
        return None
    matches = sorted(
        entry.name for entry in channel_dir.iterdir()
        if entry.is_file() and is_substance_filename(entry.name, counterpart)
    )
    return matches[-1] if matches else None


def my_outbound_after(channel_dir: Path, agent: str, after_id: Optional[str]) -> bool:
    """True if `agent` has any substance message in channel with filename > after_id."""
    if not after_id or not channel_dir.exists():
        return False
    for entry in channel_dir.iterdir():
        if (entry.is_file()
                and is_substance_filename(entry.name, agent)
                and entry.name > after_id):
            return True
    return False


# --- frontmatter parsing ---

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict:
    """Parse YAML-ish frontmatter from a channel file. Returns dict (empty if no frontmatter).

    Simple key: value parsing (one per line; doesn't handle nested or multiline).
    Strips quotes around values; preserves int/None for chat_id-like fields.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    front = m.group(1)
    out: dict = {}
    for line in front.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def read_frontmatter(path: Path) -> dict:
    """Read the frontmatter from a channel file. Returns empty dict on missing file."""
    if not path.exists():
        return {}
    return parse_frontmatter(path.read_text(encoding="utf-8"))


# --- atomic write ---

def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    """Write `content` to `path` atomically via temp+rename.

    Used for state files, pulse files, mirror files, secrets — any file
    where partial writes are dangerous. Sets mode (default 0o600 for
    secrets-friendly default; callers can pass 0o644 for non-secret files).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp",
        delete=False, encoding="utf-8",
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp.name, mode)
        os.replace(tmp.name, path)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def write_channel_file(channel_dir: Path, filename: str, frontmatter: dict,
                       body: str = "", mode: int = 0o644) -> Path:
    """Write a channel file with frontmatter + optional body atomically.

    Frontmatter is rendered as `key: value` lines between `---` delimiters.
    Returns the full path written to.
    """
    channel_dir.mkdir(parents=True, exist_ok=True)
    path = channel_dir / filename
    lines = ["---"]
    for k, v in frontmatter.items():
        if v is None:
            lines.append(f"{k}: null")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    if body:
        lines.append("")
        lines.append(body)
    content = "\n".join(lines) + "\n"
    atomic_write(path, content, mode=mode)
    return path
