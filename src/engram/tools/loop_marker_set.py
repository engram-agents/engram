#!/usr/bin/env python3
"""Set ONE top-level key of the loop marker, preserving the file's mode and group [#4025].

The engram-loop skill used to patch ``$ENGRAM_HOME/loop-mode.json`` with ``mktemp`` + ``jq`` +
``mv``.  That replaces the file through a new inode, which comes back mode 0600 and without the
directory's default-ACL group entry, so a second account that reads the marker loses access on
every write; it also needs ``jq``, which a seat may not have.  This helper is the one writer
every recipe calls instead.  It reads the marker, changes the one key, and writes the whole
file back through ``engram_config_io.atomic_write_text`` (the shared mode- and group-preserving
writer, #4013) with ``follow_symlinks=False``: the data directory may be group-writable, so a
planted link must not redirect the write.

    loop_marker_set.py KEY JSON-VALUE       3300 stores a number, "3300" stores a string
    loop_marker_set.py --string KEY TEXT    stores TEXT literally (no JSON quoting needed)
    --marker PATH                           override the default $ENGRAM_HOME/loop-mode.json

Option rule: only `--`-prefixed tokens are options; a key beginning with a single `-` is accepted as a key,
and `--` ends option parsing (what follows is KEY and the value, even if it starts with `--`).

Every other key keeps its value and its position.  The helper NEVER creates a marker (creating
one is Step 1 of the skill, a different act): a missing, unparseable or non-object marker is a
refusal.  A refusal is exit 1 and ONE stderr line starting ``loop_marker_set:``; the file is
untouched.  Success is exit 0 and exactly one stdout line, ``loop_marker_set: <KEY> set``.

Standard library only; runs under ``python3 -I``.  Core modules sit at the package root, and the
parent of this file's directory is the package root in BOTH layouts (src/engram/ and the deployed
plugin root), so it is found by that, never by a fixed ``parents[N]``.
"""

from __future__ import annotations

import json
import os
import sys

USAGE = "usage: loop_marker_set.py [--marker PATH] KEY JSON-VALUE | --string KEY TEXT"


class Refusal(Exception):
    """A named, expected refusal: printed as one line, exit 1."""


def _package_root() -> str:
    pkg_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    return pkg_root


def default_marker_path() -> str:
    """``loop-mode.json`` under the ENGRAM home, resolved exactly as loop_prompt does: through the
    one shared resolver, ``tools/_owner_read.engram_home()`` (a relative ENGRAM_HOME is refused)."""
    _package_root()
    from tools import _owner_read

    return str(_owner_read.engram_home() / "loop-mode.json")


def _parse_args(argv):
    """-> (marker_override_or_None, as_string, key, raw_value).  Hand-parsed so every
    refusal is one line (argparse prints a multi-line usage block)."""
    marker = None
    as_string = False
    rest = []
    it = iter(argv)
    for tok in it:
        if tok == "--":
            rest.extend(it)
            break
        if tok == "--string":
            as_string = True
        elif tok == "--marker":
            marker = next(it, None)
            if marker is None:
                raise Refusal("--marker needs a path; " + USAGE)
        elif tok.startswith("--"):
            raise Refusal(f"unknown option {tok}; " + USAGE)
        else:
            rest.append(tok)
    if len(rest) != 2:
        raise Refusal(f"expected KEY and a value, got {len(rest)} argument(s); " + USAGE)
    return marker, as_string, rest[0], rest[1]


def _load_marker(path: str) -> dict:
    _package_root()
    from engram_regular_io import read_regular_text

    if not os.path.lexists(path):
        raise Refusal(f"marker file not found: {path} (this helper never creates one)")
    try:
        data = json.loads(read_regular_text(path, encoding="utf-8"))
    except OSError as exc:  # a FIFO, a directory, a file we cannot read: not a content problem
        raise Refusal(f"marker path is not a readable regular file: {path} ({' '.join(str(exc).split())})")
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise Refusal(f"marker file is not valid JSON: {path} ({' '.join(str(exc).split())})")
    if not isinstance(data, dict):
        raise Refusal(f"marker file is not a JSON object: {path}")
    return data


def set_key(marker_path: str, key: str, value) -> None:
    """Patch ``key`` in the marker at ``marker_path`` and write it back; raises Refusal."""
    data = _load_marker(marker_path)
    data[key] = value
    try:
        text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError as exc:
        raise Refusal(f"value for {key!r} cannot be stored as JSON ({exc})")
    _package_root()
    from engram_config_io import atomic_write_text

    try:
        atomic_write_text(marker_path, text, encoding="utf-8", follow_symlinks=False)
    except (OSError, UnicodeError) as exc:
        raise Refusal(f"could not write {marker_path} ({' '.join(str(exc).split())})")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        override, as_string, key, raw = _parse_args(argv)
        if not key:
            raise Refusal("KEY is empty; " + USAGE)
        if as_string:
            value = raw
        else:
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise Refusal(
                    f"JSON-VALUE does not parse ({' '.join(str(exc).split())}); "
                    "use --string KEY TEXT for free text"
                )
        if override is not None:
            marker_path = os.path.expanduser(override)
        else:
            try:
                marker_path = default_marker_path()
            except ValueError as exc:
                raise Refusal(str(exc))
        set_key(marker_path, key, value)
    except Refusal as exc:
        sys.stderr.write(f"loop_marker_set: {exc}\n")
        return 1
    sys.stdout.write(f"loop_marker_set: {key} set\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
