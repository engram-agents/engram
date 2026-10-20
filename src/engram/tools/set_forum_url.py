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
counts as already set (unchanged). A config file that is formatted
differently is reformatted on write (``json.dump(indent=2)`` plus a trailing
newline, default ASCII escapes for non-ASCII text, as
``install-local-marketplace.sh`` writes it; values round-trip unchanged); key
order is preserved.
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
import tempfile
from typing import Any, NamedTuple, Optional
from urllib.parse import urlsplit

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


def _atomic_write(path: str, data: dict) -> None:
    """Write ``data`` to ``path`` (already symlink-resolved) atomically."""
    directory = os.path.dirname(path) or "."
    mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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

    forum = config.get("forum")
    if forum is None:
        new_forum: dict = {"url": value}
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
                    "pass --force to overwrite it",
                    EXIT_CONFLICT,
                )
        new_forum = dict(forum)
        new_forum["url"] = value
    else:
        if not force:
            return _refuse(
                f"refused: 'forum' in {config_path} is a "
                f"{type(forum).__name__}, not an object; pass --force to replace it"
            )
        new_forum = {"url": value}

    config["forum"] = new_forum
    try:
        _atomic_write(path, config)
    except OSError as exc:
        return _refuse(f"refused: could not write {config_path} ({exc}); file unchanged")
    return SetResult("written", f"written: forum.url set to {value}", EXIT_OK)


def _default_config() -> str:
    home = os.environ.get("ENGRAM_HOME") or os.path.join(os.path.expanduser("~"), ".engram")
    return os.path.join(home, "config.json")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set forum.url in an ENGRAM config.json (writes nothing else).")
    parser.add_argument("url", help="forum base URL, e.g. http://localhost:5002")
    parser.add_argument("--config", help="config.json path (default: $ENGRAM_HOME/config.json)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite a different existing forum.url, or a non-object forum")
    args = parser.parse_args(argv)  # a missing URL exits 2 (argparse)
    result = set_forum_url(args.config or _default_config(), args.url, force=args.force)
    print(result.message, file=sys.stderr if result.status == "refused" else sys.stdout)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
