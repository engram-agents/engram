#!/usr/bin/env python3
"""Set ``forum.url`` in an ENGRAM ``config.json`` -- the one shared writer.

Since the forum, baton and inter-agent hooks and CLIs treat only a non-empty
string ``forum.url`` as "forum configured", something has to write that key
explicitly; the gate refuses to infer it. This tool writes ``forum.url`` and
NOTHING else.

Usage::

    set_forum_url.py URL [--config PATH] [--force]

``--config`` defaults to ``$ENGRAM_HOME/config.json`` (``ENGRAM_HOME`` defaults
to ``~/.engram`` unless ``ENGRAM_HOME`` is set).

Exit codes:
    0  written, or already set to this value (unchanged)
    2  refused: invalid URL, missing/corrupt config, non-object config, or a
       non-dict ``forum`` value (``--force`` replaces the latter), or the
       write itself failed
    3  refused: a DIFFERENT non-empty ``forum.url`` exists (``--force``
       overwrites it)

The URL must be http(s) with a host, an optional port in 1..65535, no
whitespace or control characters, no userinfo, and no query string or
fragment (a path such as ``/api`` is allowed: a prefix deployment). The host
is a bracketed IPv6 literal, or an IPv4/DNS name of letters, digits, ``-``,
``_`` and ``.`` (no empty label, no label starting or ending in ``-``, labels
at most 63 and the name at most 253 characters); an empty port (``h:``) is
refused. One trailing ``/`` is removed (a second one is refused, so the stored
value never ends in ``/`` and a re-run is idempotent), and an existing
``forum.url`` that differs from the new value only by one trailing ``/``
counts as already set (unchanged). The write is the file guard's MERGE class
(``engram_file_guard.apply_merge``): ``forum.url`` is the one key written, every
other key keeps its value and order, and the file is re-serialized with its own
indent (2 when it has none) plus a trailing newline. Before a write that changes
the file, its current bytes are saved to ``config.json.engram-backup.<UTC>``
beside it (backups accumulate; nothing deletes them). A file that does not
parse, holds a duplicate key or a non-finite number is refused and left as it
is. ``--force`` is the user's yes to replacing a DIFFERENT url (or a ``forum``
value that is not an object); an agent shows the user the old and new value and
where the backup goes before it passes ``--force``.
The write is atomic (same-directory temp file, fsync, ``os.replace``), keeps
the file mode, and follows a symlinked ``config.json`` to its target. Ownership
and file-level ACL entries are not preserved by the atomic replace (a directory
default ACL re-applies on the new file).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from typing import Any, NamedTuple, Optional
from pathlib import Path
from urllib.parse import urlsplit

# The ENGRAM home has ONE definition: tools/_owner_read.engram_home() (#3863). `tools` is found one directory above
# this file in BOTH layouts (src/engram/ and the deployed plugin root). Appended, not inserted: an in-process load
# (a test) must not leave src/engram at sys.path[0] (#4029).
_PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if _PKG_ROOT not in sys.path:
    sys.path.append(_PKG_ROOT)
from tools import _owner_read as owner_read  # noqa: E402
# The write goes through the one file guard (engram_file_guard.py), which sits in the same
# folder as `tools` in both layouts: backup first, then an atomic write.
import engram_file_guard as fg  # noqa: E402

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_CONFLICT = 3


class SetResult(NamedTuple):
    status: str  # "written" | "unchanged" | "refused"
    message: str
    exit_code: int


def _refuse(message: str, code: int = EXIT_INVALID) -> SetResult:
    return SetResult("refused", message, code)


_LABEL = re.compile(r"[A-Za-z0-9_-]+")


def _host_problem(netloc: str) -> Optional[str]:
    """Why the host part of ``netloc`` (no userinfo) is not plausible, or None."""
    if netloc.startswith("["):
        end = netloc.find("]")
        if end == -1:
            return "unterminated IPv6 literal"
        try:
            ipaddress.IPv6Address(netloc[1 : end])
        except ValueError:
            return "not a valid IPv6 literal"
        rest = netloc[end + 1 :]
        if rest and not (rest.startswith(":") and len(rest) > 1):
            return "empty port or stray text after the IPv6 literal"
        return None
    host, sep, port = netloc.partition(":")
    if sep and not port:
        return "empty port after ':'"
    if not host or len(host) > 253:
        return "host name is empty or longer than 253 characters"
    for label in host.split("."):
        if not label or len(label) > 63:
            return "host has an empty label or a label longer than 63 characters"
        if not _LABEL.fullmatch(label) or label[0] == "-" or label[-1] == "-":
            return "host label has a bad character or starts/ends with '-'"
    return None


def validate_url(url: Any) -> "tuple[Optional[str], str]":
    """Return ``(normalized, "")`` or ``(None, reason)``."""
    if not isinstance(url, str):
        return None, "URL must be a string"
    value = url.strip()
    if not value:
        return None, "URL is empty"
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None, "URL contains whitespace or control characters"
    try:
        parts = urlsplit(value)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        return None, f"URL is malformed ({exc})"
    if parts.scheme.lower() not in ("http", "https"):
        return None, "URL scheme must be http or https"
    if "?" in value or "#" in value:
        return None, "URL must not carry a query string or fragment"
    if "@" in parts.netloc:
        return None, "URL must not carry credentials (user:pass@)"
    if not host:
        return None, "URL has no host"
    if port is not None and not 1 <= port <= 65535:
        return None, "URL port must be 1..65535"
    problem = _host_problem(parts.netloc)
    if problem:
        return None, f"URL host is not plausible: {problem}"
    if value.endswith("/"):
        value = value[:-1]
    if value.endswith("/"):
        return None, "URL must not end in more than one '/'"
    return value, ""


def set_forum_url(config_path, url, *, force: bool = False) -> SetResult:
    """Set ``forum.url`` in ``config_path``; touch nothing else."""
    value, reason = validate_url(url)
    if value is None:
        return _refuse(f"refused: {reason}")

    path = os.path.realpath(os.fspath(config_path))
    if not os.path.isfile(path):
        return _refuse(
            f"refused: {config_path} does not exist (run first-session first; "
            "this tool never creates a config)"
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
        return _refuse(f"refused: cannot read {config_path} as JSON ({exc})")
    if not isinstance(config, dict):
        return _refuse(f"refused: {config_path} is not a JSON object")
    try:  # the guard's own strict reading: a duplicate key or a non-finite number is refused before any decision
        fg.merge_json_text("config.json", Path(path).read_text(encoding="utf-8"), {})
    except fg.MergeRefused as exc:
        return _refuse(f"refused: {exc}; file unchanged")

    forum = config.get("forum")
    replace_non_object = False  # becomes True only on the --force path below, after the user's yes
    if forum is None:
        pass  # absent or an explicit null: the guard treats both as absent
    elif isinstance(forum, dict):
        existing = forum.get("url")
        if isinstance(existing, str) and existing.strip():
            current = existing.strip()
            if current.endswith("/"):
                current = current[:-1]  # exactly one, same rule as the new value
            if current == value:
                return SetResult("unchanged", f"unchanged: forum.url already {value}", EXIT_OK)
            if not force:
                return _refuse(
                    f"refused: forum.url is already {existing.strip()!r}; "
                    "pass --force to overwrite it (the old file is saved to a backup first). "
                    "An agent shows the user the old and the new url and where the backup goes "
                    "first, and passes --force only on the user's yes",
                    EXIT_CONFLICT,
                )
    else:
        if not force:
            return _refuse(
                f"refused: 'forum' in {config_path} is a "
                f"{type(forum).__name__}, not an object; pass --force to replace it (the old "
                "file is saved to a backup first). An agent shows the user that value and "
                "where the backup goes first, and passes --force only on the user's yes"
            )
        replace_non_object = True

    # The write is the file guard's MERGE class: forum.url is the one key written, every other
    # key keeps its value and order, and the file is saved to a time-stamped backup first.
    try:
        fg.apply_merge(Path(path), {"forum.url": value}, filename="config.json",
                       replace_non_object=replace_non_object)
    except fg.MergeRefused as exc:
        return _refuse(f"refused: {exc}; file unchanged")
    except fg.WriteRefused as exc:
        # The guard's line names the backup; this sentence says which state the file is in.
        if exc.outcome == "may_be_incomplete":
            state = "the file may be incomplete; restore it from the backup named above"
        elif exc.outcome == "restored":
            state = "file unchanged in content"
        else:
            state = "file unchanged"
        return _refuse(f"refused: could not write {config_path} ({exc}); {state}")
    except (OSError, SystemExit) as exc:
        return _refuse(f"refused: could not write {config_path} ({exc}); file unchanged")
    return SetResult("written", f"written: forum.url set to {value}", EXIT_OK)


def _default_config() -> str:
    return os.path.join(str(owner_read.engram_home()), "config.json")  # ValueError on a relative ENGRAM_HOME (main refuses)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set forum.url in an ENGRAM config.json (writes nothing else).")
    parser.add_argument("url", help="forum base URL, e.g. http://localhost:5002")
    parser.add_argument("--config", help="config.json path (default: $ENGRAM_HOME/config.json)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a different existing forum.url, or a non-object forum")
    args = parser.parse_args(argv)  # a missing URL exits 2 (argparse)
    try:
        config = args.config or _default_config()
    except ValueError as exc:  # a relative ENGRAM_HOME: refused, nothing read or written (#3863)
        print(f"set_forum_url: {exc}", file=sys.stderr)
        return EXIT_INVALID
    result = set_forum_url(config, args.url, force=args.force)
    print(result.message, file=sys.stderr if result.status == "refused" else sys.stdout)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
