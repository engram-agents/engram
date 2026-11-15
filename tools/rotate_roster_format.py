#!/usr/bin/env python3
"""rotate_roster_format.py — one-time format-1 -> format-2 roster migration.

The roster secret is a list of name *patterns*. Format 1 is one regex per line;
format 2 prefixes every line with a class: ``A<TAB>pattern`` blocks, ``B<TAB>
pattern`` is report-only (permitted content). This helper performs the
conversion on the machine of the person who holds the secret, and prints only
counts, the whole-file sha256, per-class line numbers, and the class-B names
(the caller's own input of permitted names). It never prints a roster line.

Classification (R4) -- the roster lines are regexes, so "exact match" between a
pattern and a name is not string equality. Each roster line and each B-file
entry is reduced to a NAME KEY:

    strip -> drop a leading (?i) -> drop a leading and a trailing \\b
          -> strip -> casefold

The key strips only wrappers that do not change which name is named (``\\b``,
``(?i)``) and casefolds; it NEVER rewrites an emitted pattern -- the pattern
text is carried through verbatim. A roster line is class B iff its key equals
the name key of a B-file entry; everything else is class A. Any roster line
whose key matches nothing is class A -- blocking -- so a failed match blocks a
name unnecessarily and is visible, rather than classifying a private name as
report-only.

The produced roster is validated by the scanner's real format-2 parser
(``load_roster_file``) before it is written; if the parser refuses, nothing is
written. The output is one file, ``DIR/roster-format2.txt``, created 0600 by
``os.open``; nothing else is written anywhere.

Stdlib only. No network. No subprocess.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

OUTPUT_NAME = "roster-format2.txt"

NAME_KEY_RULE = (
    "strip -> drop a leading (?i) -> drop a leading and a trailing \\b "
    "-> strip -> casefold"
)


class _Parser(argparse.ArgumentParser):
    """argparse with R7's usage-error code: 1, not argparse's default 2."""

    def error(self, message: str) -> "None":
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _fail(code: int, message: str) -> "None":
    print(f"rotate_roster_format: {message}", file=sys.stderr)
    raise SystemExit(code)


def name_key(text: str) -> str:
    """The name key, applied identically to a roster line and a B-file entry.

    strip -> drop a leading ``(?i)`` -> drop a leading and a trailing ``\\b``
    -> strip -> casefold.
    """
    key = text.strip()
    if key.startswith("(?i)"):
        key = key[4:]
    if key.startswith("\\b"):
        key = key[2:]
    if key.endswith("\\b"):
        key = key[:-2]
    return key.strip().casefold()


def _skippable(stripped: str) -> bool:
    return not stripped or stripped.startswith("#")


def _read_text(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        # The path is not secret; the roster text is. Report the class only.
        _fail(1, f"cannot read {what} ({type(exc).__name__})")
    except UnicodeDecodeError as exc:
        _fail(1, f"cannot decode {what} as UTF-8 ({type(exc).__name__})")


def _scanner_candidates(cli_scanner: "str | None") -> "list[Path]":
    if cli_scanner:
        return [Path(cli_scanner).expanduser()]
    candidates = []
    env_scanner = os.environ.get("ENGRAM_SCANNER")
    if env_scanner:
        candidates.append(Path(env_scanner).expanduser())
    here = Path(__file__).resolve().parent
    candidates.append(here / "scan-leaks.py")                 # the script's sibling
    candidates.append(here.parent / "src" / "engram" / "tools" / "scan-leaks.py")
    return candidates


def _resolve_scanner(cli_scanner: "str | None") -> "Path | None":
    for candidate in _scanner_candidates(cli_scanner):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _load_parser(scanner_path: Path):
    """Load ``load_roster_file`` by explicit file path (importlib).

    Bytecode caching is disabled for the duration: importing the scanner must
    not create a ``__pycache__`` beside it (R2 -- nothing written outside
    ``--out-dir``).
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_rotate_roster_scan_leaks", scanner_path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
    except Exception as exc:  # noqa: BLE001 - any load failure is "no parser"
        print(
            f"rotate_roster_format: could not load scanner ({type(exc).__name__})",
            file=sys.stderr,
        )
        return None
    loader = getattr(module, "load_roster_file", None)
    return loader if callable(loader) else None


def main(argv: "list[str] | None" = None) -> int:
    parser = _Parser(
        prog="rotate_roster_format.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Convert a format-1 roster (one regex per line) to format 2\n"
            "(A<TAB>pattern blocks, B<TAB>pattern is report-only), printing\n"
            "only counts, the sha256 and the class-B names. Never prints a\n"
            "roster line."
        ),
        epilog=(
            "Name key (applied to each roster line and to each B-file entry):\n"
            f"  {NAME_KEY_RULE}\n"
            "\n"
            "A roster line is class B iff its name key equals the name key of a\n"
            "B-file entry; every other line is class A (blocking). Roster lines\n"
            "are regexes, so 'exact match' between a pattern and a name is not\n"
            "string equality: the key strips only wrappers that do not change\n"
            "which name is named (\\b, (?i)) and casefolds. It never rewrites\n"
            "the emitted pattern. A line whose key matches nothing is class A,\n"
            "so a failed match blocks a name unnecessarily and is visible,\n"
            "rather than classifying a private name as report-only.\n"
            "\n"
            "Output: DIR/roster-format2.txt, created mode 0600 by os.open and\n"
            "refused when it already exists unless --force is passed. Nothing\n"
            "else is written, anywhere."
        ),
    )
    parser.add_argument(
        "--roster", required=True, metavar="PATH",
        help="Format-1 roster: one regex per line; blank and #-comment lines skipped.",
    )
    parser.add_argument(
        "--b-names", required=True, metavar="PATH", dest="b_names",
        help="File of class-B permitted names, one per line.",
    )
    parser.add_argument(
        "--out-dir", required=True, metavar="DIR", dest="out_dir",
        help="Existing directory that will receive roster-format2.txt.",
    )
    parser.add_argument(
        "--scanner", metavar="PATH", default=None,
        help="Scanner module to validate with (default: $ENGRAM_SCANNER, then "
             "the script's sibling, then src/engram/tools/scan-leaks.py).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing DIR/roster-format2.txt.",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).expanduser()
    if not out_dir.is_dir():
        _fail(1, "--out-dir is not an existing directory; refusing to create it")

    # ---- read the roster -------------------------------------------------
    roster_text = _read_text(Path(args.roster).expanduser(), "the roster")
    raw_lines = roster_text.splitlines()
    total_input_lines = len(raw_lines)

    # R3: refuse a roster that is already class-prefixed, naming the first
    # offending 1-based line. Do this before anything else touches the file.
    for lineno, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()
        if _skippable(stripped):
            continue
        if stripped.startswith("A\t") or stripped.startswith("B\t"):
            _fail(
                2,
                f"roster line {lineno} already starts with A<TAB> or B<TAB>; "
                "this roster is already class-prefixed (converting again would "
                "double-prefix). Refusing; nothing written.",
            )

    entries = []  # (input line number, pattern text, name key)
    skipped = 0
    for lineno, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()
        if _skippable(stripped):
            skipped += 1
            continue
        entries.append((lineno, stripped, name_key(stripped)))

    # ---- read the B names ------------------------------------------------
    b_text = _read_text(Path(args.b_names).expanduser(), "the B-names file")
    b_entries = []  # (B-file line number, permitted name, name key)
    for b_lineno, raw in enumerate(b_text.splitlines(), start=1):
        stripped = raw.strip()
        if _skippable(stripped):
            continue
        b_entries.append((b_lineno, stripped, name_key(stripped)))

    # ---- classify --------------------------------------------------------
    b_name_by_key = {}
    for _b_lineno, name, key in b_entries:
        b_name_by_key.setdefault(key, name)

    out_lines = []
    b_roster_matches = []  # (roster input line number, permitted B name)
    matched_b_keys = set()
    class_a = 0
    class_b = 0
    for lineno, pattern, key in entries:
        if key in b_name_by_key:
            class_b += 1
            matched_b_keys.add(key)
            out_lines.append("B\t" + pattern)
            b_roster_matches.append((lineno, b_name_by_key[key]))
        else:
            class_a += 1
            out_lines.append("A\t" + pattern)

    unmatched_b = [
        name for _b_lineno, name, key in b_entries if key not in matched_b_keys
    ]

    produced = "".join(line + "\n" for line in out_lines)
    canonical = produced.rstrip("\n") + "\n"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ---- resolve and load the real parser --------------------------------
    out_path = out_dir / OUTPUT_NAME
    if out_path.exists() and not args.force:
        _fail(2, f"{out_path} already exists; pass --force to overwrite. "
                 "Nothing written.")

    scanner_path = _resolve_scanner(args.scanner)
    if scanner_path is None:
        tried = ", ".join(str(c) for c in _scanner_candidates(args.scanner))
        _fail(
            1,
            "no scanner found; the roster cannot be validated without the "
            f"parser, and an unvalidated roster is never emitted (tried: {tried})",
        )
    parse_roster = _load_parser(scanner_path)
    if parse_roster is None:
        _fail(
            1,
            f"{scanner_path} has no usable load_roster_file; the roster cannot "
            "be validated without the parser. Nothing written.",
        )

    # ---- validate from a 0600 temp file INSIDE --out-dir ------------------
    temp_path = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".rotate-roster-validate-", suffix=".tmp", dir=str(out_dir)
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(produced)
        try:
            parse_roster(temp_path)
        except SystemExit as exc:
            # The parser has already printed its line-number-only refusal.
            _fail(
                2,
                "the produced roster was refused by the parser "
                f"(exit {exc.code}); nothing written.",
            )
        except Exception as exc:  # noqa: BLE001
            _fail(
                1,
                "the scanner's load_roster_file raised "
                f"{type(exc).__name__}; nothing written.",
            )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass

    # ---- write the one output file, 0600 by os.open ----------------------
    if out_path.exists():  # --force was passed to get here
        try:
            out_path.unlink()
        except OSError as exc:
            _fail(1, f"cannot replace the existing output ({type(exc).__name__})")
    try:
        out_fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        _fail(1, f"cannot create {out_path} ({type(exc).__name__})")
    try:
        with os.fdopen(out_fd, "w", encoding="utf-8") as handle:
            handle.write(produced)
    except OSError as exc:
        try:
            out_path.unlink()
        except OSError:
            pass
        _fail(1, f"cannot write {out_path} ({type(exc).__name__})")

    # ---- the audit summary (R6 order) ------------------------------------
    print(f"total_input_lines: {total_input_lines}")
    print(f"blank_or_comment_skipped: {skipped}")
    print(f"class_a: {class_a}")
    print(f"class_b: {class_b}")
    for lineno, name in b_roster_matches:
        print(f"  line {lineno}: {name}")
    print(f"b_names_with_no_match: {len(unmatched_b)} of {len(b_entries)}")
    for name in unmatched_b:
        print(f"  unmatched_b_name: {name}")
    print(f"roster_sha256: {digest}")
    print("roster_format: 2")
    print(f"pattern_count_a: {class_a}")
    print(f"pattern_count_b: {class_b}")
    print(f"name_key: {NAME_KEY_RULE}")
    print(f"scanner: {scanner_path}")
    print(f"output: {out_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # never leak content through an unhandled traceback
        print(
            f"rotate_roster_format: internal error ({type(exc).__name__})",
            file=sys.stderr,
        )
        sys.exit(1)
