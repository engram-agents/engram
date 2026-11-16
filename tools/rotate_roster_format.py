#!/usr/bin/env python3
"""rotate_roster_format.py — one-time format-1 -> format-2 roster migration.

The roster secret is a list of name *patterns*. Format 1 is one regex per line;
format 2 prefixes every line with a class: ``A<TAB>pattern`` blocks, ``B<TAB>
pattern`` is report-only (permitted content). This helper performs the
conversion on the machine of the person who holds the secret, and prints only
counts, whole-file sha256 values, per-class line numbers, and the class-B names
(the caller's own input of permitted names). It never prints a roster line.

A captured parser refusal that carries roster text is withheld whole and
reported by exit code alone. The test is containment of a produced pattern, or
of a produced line's bare name key, verbatim or with every backslash doubled --
the rendering repr() produces, so an echoed exception object is covered too.
Two limits bound that, and both are real: text the parser writes to stdout, or
directly to file descriptor 2, is not captured (this tool redirects only
sys.stderr), and a PARTIAL echo is still not detected unless it is exactly a
produced line's bare name key, so a parser that prints any other fragment of a
pattern can still carry name text to the operator. Neither limit fires with the
repo's own scanner, which prints a line number and an exception class and
nothing else; they matter only for a parser supplied by someone else.

Classification (R4) -- the roster lines are regexes, so "exact match" between a
pattern and a name is not string equality. Each roster line and each B-file
entry is reduced to a NAME by stripping ONLY wrappers that do not change which
name is named:

    strip -> drop a leading (?i) -> drop \\b only when BOTH ends carry it
          -> strip

A one-sided \\b is NOT a wrapper: it makes the pattern a prefix or suffix
match, a different pattern, so it is left in place and that line cannot be
class B. Case is folded ONLY when the pattern declares case-insensitivity with
a leading ``(?i)``; without it the comparison is case-sensitive, so a
case-distinct name is not silently treated as the permitted one.

The name is never used to rewrite an emitted pattern -- the pattern text is
carried through verbatim. A roster line is class B iff its name equals a
B-file entry's name under the rule above; everything else is class A. Any
roster line that matches nothing is class A -- blocking -- so a failed match
blocks a name unnecessarily and is visible, rather than classifying a private
name as report-only.

Input identity. A GitHub secret cannot be read back, so the operator may be
holding a stale copy. The committed attestation carries the live roster's
whole-file sha256 under the gate's canonical form (all trailing newlines
stripped, exactly one appended). The tool computes the same value over the
input and refuses (exit 2, nothing written) unless it matches the attested
value. ``--attestation PATH`` overrides the default, which is derived from the
scanner path as ``<repo>/.github/name-leak-roster.attestation.json``.

Parser identity. A pre-two-class scanner also exposes ``load_roster_file`` (the
format-1 one), and handed ``A<TAB>pattern`` it compiles the prefix as regex
text and returns success. The tool therefore probes the loaded parser with two
synthetic rosters -- an invalid class token and a class-A line with no declared
boundary, both built from a fresh ``secrets.token_hex(8)`` per run so a scanner
cannot recognise them -- and refuses (exit 1, nothing written) if either is
accepted. It also checks the parser's RETURN VALUE for the produced roster: the
entry count must equal the produced line count, and each entry's class must
match the class produced for that line, in order. A parser that cannot pass
those checks is not a validator.

Class-A boundaries are audited on EVERY run, not only in a dry run: every
produced class-A line is checked with the scanner's own ``_declares_boundary``,
and if any lack one the tool lists ALL their source line numbers and refuses
(exit 2) before parsing the produced roster. It never adds a boundary itself.

A class-B line is reported as ``b_wider_than_its_name`` when its pattern is
wider than the bare permitted name it matched -- no declared boundary, or
regex metacharacters beyond ``\\b`` and a leading ``(?i)``. That is a warning,
never a refusal: making a line class B makes everything it matches report-only,
and only the operator can say whether that width is intended.

``--dry-run`` runs the whole pipeline and writes nothing; it prints the audit,
including every class-A line without a declared boundary (by source line
number) and the unmatched permitted names, and exits 2 if the real run would
be refused.

The produced roster is validated by the scanner's real format-2 parser before
it is written; the real run parses it from a 0600 temp file inside
``--out-dir``. The output is one file, ``DIR/roster-format2.txt``, created 0600
by ``os.open``; nothing else is written anywhere (a dry run writes no file at
all).

Stdlib only. No network. No subprocess.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

OUTPUT_NAME = "roster-format2.txt"
ATTESTATION_REL = Path(".github") / "name-leak-roster.attestation.json"

# Synthetic format-2 rosters for the parser positive control. The token is
# fresh per run (a scanner must not be able to recognise it) and the probe
# text is never printed.
def _probe_rosters():
    token = secrets.token_hex(8)
    return (
        ("an invalid class token", "C\t\\b" + token + "\\b\n"),
        ("a class-A line with no declared boundary", "A\t" + token + "\n"),
    )

NAME_KEY_RULE = (
    "strip -> drop a leading (?i) -> drop \\b only when BOTH ends carry it "
    "(a one-sided \\b is kept) -> strip -> casefold only when (?i) is present"
)

# Regex metacharacters (besides \b and a leading (?i)) that make a class-B
# pattern wider than the bare name it matched. A backslash escape other than
# \b is handled separately: it widens a pattern just as these do.
METACHARS = frozenset(".([|*+?{")


class _Parser(argparse.ArgumentParser):
    """argparse with R7's usage-error code: 1, not argparse's default 2."""

    def error(self, message: str) -> "None":
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


class _TextSource:
    """A read-only path-like source: the parser needs only ``read_text``.

    Used by ``--dry-run`` so the whole pipeline can run with no file written.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text


def _fail(code: int, message: str) -> "None":
    print(f"rotate_roster_format: {message}", file=sys.stderr)
    raise SystemExit(code)


def _key_parts(text: str) -> "tuple[str, bool]":
    """Return ``(name, ignore_case)`` after stripping only real wrappers.

    strip -> drop a leading ``(?i)`` -> drop ``\\b`` only when BOTH ends carry
    it -> strip. A one-sided ``\\b`` is kept: it is a prefix/suffix match, not
    a wrapper around the bare name.
    """
    key = text.strip()
    ignore_case = False
    if key.startswith("(?i)"):
        ignore_case = True
        key = key[4:]
    if key.startswith("\\b") and key.endswith("\\b"):
        key = key[2:-2]
    return key.strip(), ignore_case


def _names_match(roster_line: str, b_parts: "tuple[str, bool]") -> bool:
    """Compare a roster line's name to a B entry's under the R4 rule.

    Case is folded only when one side declares ``(?i)``; otherwise the
    comparison is case-sensitive.
    """
    roster_name, roster_ci = _key_parts(roster_line)
    b_name, b_ci = b_parts
    if roster_ci or b_ci:
        return roster_name.casefold() == b_name.casefold()
    return roster_name == b_name


def _skippable(stripped: str) -> bool:
    return not stripped or stripped.startswith("#")


def _read_text(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        # The path is not secret; the text is. Report the class only.
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


def _derive_repo_root(scanner_path: Path) -> "Path | None":
    """The repo root above a ``<repo>/src/engram/tools/scan-leaks.py`` path."""
    parts = scanner_path.parts
    if len(parts) >= 4 and Path(*parts[-4:]) == Path(
        "src/engram/tools/scan-leaks.py"
    ):
        return Path(*parts[:-4])
    return None


def _resolve_attestation(
    cli_attestation: "str | None", scanner_path: Path
) -> "Path | None":
    if cli_attestation:
        return Path(cli_attestation).expanduser()
    repo = _derive_repo_root(scanner_path)
    if repo is None:
        return None
    return repo / ATTESTATION_REL


def _read_attested_sha(path: Path) -> "tuple[str | None, str | None]":
    """Return ``(sha, reason)``; reason names the file, never its content."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, (
            f"cannot read an attestation at {path} ({type(exc).__name__})"
        )
    if not isinstance(document, dict):
        return None, f"the attestation at {path} is not a JSON object"
    value = document.get("roster_sha256")
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        return None, (
            f"the attestation at {path} has no usable roster_sha256 "
            "(missing or a placeholder)"
        )
    return value, None


def _verify_attestation(
    attestation_path: "Path | None", input_sha: str
) -> str:
    """Refuse unless the input's canonical sha256 equals the attested value."""
    if attestation_path is None:
        _fail(
            2,
            "cannot locate the roster attestation: the scanner path is not "
            "shaped as <repo>/src/engram/tools/scan-leaks.py and --attestation "
            "was not given. The input cannot be confirmed as the live list; "
            "nothing written.",
        )
    attested, reason = _read_attested_sha(attestation_path)
    if attested is None:
        _fail(
            2,
            f"{reason}. The input cannot be confirmed as the live list; "
            "nothing written.",
        )
    if attested != input_sha:
        print(f"attestation_roster_sha256: {attested}", file=sys.stderr)
        print(f"input_roster_sha256: {input_sha}", file=sys.stderr)
        _fail(
            2,
            "the input roster does not match the attested live list; this "
            "tool must run against the live roster copy. Nothing written.",
        )
    return attested


def _load_parser(scanner_path: Path):
    """Load ``load_roster_file`` and ``_declares_boundary`` by explicit path.

    Bytecode caching is disabled for the duration: importing the scanner must
    not create a ``__pycache__`` beside it (nothing is written outside
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
    if not callable(loader):
        return None
    boundary = getattr(module, "_declares_boundary", None)
    return loader, (boundary if callable(boundary) else None)


def _parse_capture(parse_roster, source):
    """Call the real parser, capturing its refusal and its return value.

    Returns ``(exit_code, stderr_text, result)``: ``(code, text, None)`` on a
    refusal, or ``(None, "", result)`` on success. The captured text is
    re-emitted verbatim where appropriate, so the parser's own reason is
    preserved.
    """
    buffer = io.StringIO()
    previous = sys.stderr
    sys.stderr = buffer
    result = None
    try:
        result = parse_roster(source)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code, buffer.getvalue(), None
    finally:
        sys.stderr = previous
    return None, "", result


def _pattern_present(text: str, pattern: str) -> bool:
    """True when ``text`` carries ``pattern`` verbatim or backslash-doubled."""
    return pattern in text or pattern.replace("\\", "\\\\") in text


def _name_key_present(text: str, pattern: str) -> bool:
    """True when ``text`` carries the name key of ``pattern`` verbatim or
    backslash-doubled.

    The name key is ``_key_parts(pattern)[0]``: the pattern minus only its
    wrappers. A parser that echoes the name without the ``\\b`` pair is a
    partial echo that a whole-pattern containment test misses. An empty key
    matches nothing (the tool refuses one elsewhere).
    """
    key = _key_parts(pattern)[0]
    return bool(key) and _pattern_present(text, key)


def _reemit_parser_refusal(refusal: str, code: int, patterns) -> None:
    """Re-emit the parser's captured refusal, or withhold it whole.

    The no-roster-text guarantee covers text this tool emits, and a re-emitted
    parser stderr is such text. If the buffer carries (a) any produced roster
    pattern verbatim or backslash-doubled, or (b) the name key of any produced
    pattern (non-empty) verbatim or backslash-doubled, nothing of it is
    written: exactly one line names the refusal by exit code only.
    """
    if any(
        pattern
        and (
            _pattern_present(refusal, pattern)
            or _name_key_present(refusal, pattern)
        )
        for pattern in patterns
    ):
        print(
            f"rotate_roster_format: the parser refused (exit {code}); its "
            "output was withheld because it contained roster text",
            file=sys.stderr,
        )
        return
    sys.stderr.write(refusal)


def _parse_text(parse_roster, text: str, *, out_dir: Path, use_file: bool):
    """Parse ``text``. The real run uses a 0600 temp file inside ``out_dir``
    (R5's required form); a dry run uses an in-memory path-like and writes
    no file at all."""
    if not use_file:
        return _parse_capture(parse_roster, _TextSource(text))
    fd, name = tempfile.mkstemp(
        prefix=".rotate-roster-parse-", suffix=".tmp", dir=str(out_dir)
    )
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return _parse_capture(parse_roster, path)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _assert_parser_understands_format2(
    parse_roster, scanner_path: Path, *, out_dir: Path, use_file: bool
) -> None:
    """Positive control: a stale format-1 parser accepts format-2 text."""
    for _label, probe in _probe_rosters():
        try:
            code, _refusal, _result = _parse_text(
                parse_roster, probe, out_dir=out_dir, use_file=use_file
            )
        except Exception as exc:  # noqa: BLE001
            _fail(
                1,
                f"the scanner at {scanner_path} could not validate the "
                f"two-class format ({type(exc).__name__}); an unvalidated "
                "roster is never emitted",
            )
        if code is None:
            _fail(
                1,
                f"the scanner at {scanner_path} predates the two-class "
                "format; an unvalidated roster is never emitted",
            )


def _validate_parser_result(result, out_lines, scanner_path: Path) -> None:
    """The produced roster's parse must describe exactly what was produced.

    A parser that returns the wrong number of entries, or the wrong class for
    a line, is not validating the two-class format. Refuse with the same
    message as a parser that accepts the probes.
    """
    stale = (
        f"the scanner at {scanner_path} predates the two-class format; "
        "an unvalidated roster is never emitted"
    )
    # Format 2 is the only format this tool emits, so an int other than 2 is a
    # stale parser; a bare entries sequence is still accepted because the probe
    # control independently proves the parser understands format 2 -- the shape
    # is not the evidence.
    if (
        isinstance(result, (list, tuple))
        and len(result) == 2
        and isinstance(result[1], int)
    ):
        if result[1] != 2:
            _fail(1, stale)
        result = result[0]
    expected = [line[0] for line in out_lines]   # "A" or "B"
    if not isinstance(result, (list, tuple)) or len(result) != len(expected):
        _fail(1, stale)
    for entry, klass in zip(result, expected):
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            _fail(1, stale)
        if entry[2] != klass:
            _fail(1, stale)


def _pattern_is_wider_than_name(pattern: str, declares_boundary) -> bool:
    """True when a class-B pattern is wider than the bare name it matched."""
    if not declares_boundary(pattern):
        return True
    text = pattern
    if text.startswith("(?i)"):
        text = text[4:]
    # Every backslash escape other than \b (\s, \d, \w, \S, \D, \W, ...)
    # widens the pattern exactly as '.' does.
    for escape in re.finditer(r"\\(.)?", text):
        if escape.group(1) != "b":
            return True
    text = text.replace("\\b", "")
    return any(char in METACHARS for char in text)


def _refuse_empty_name_key(lineno: int, what: str) -> None:
    """An empty name key is a prefix match on everything: fail closed.

    ``what`` names the caller's file; only the 1-based line number is said,
    never the text.
    """
    _fail(
        2,
        f"{what} line {lineno} has an empty name key; an empty key is a "
        "prefix match on everything, so no classification from it can be "
        "trusted. Refusing; nothing written.",
    )


def _no_boundary_message(linenos: "list[int]") -> str:
    joined = ", ".join(str(n) for n in linenos)
    noun = "line" if len(linenos) == 1 else "lines"
    return (
        f"{len(linenos)} class-A line(s) declare no boundary: source roster "
        f"{noun} {joined}. This tool will NOT rewrite the pattern: the roster "
        "must declare the boundary itself -- \\b, a lookaround, or ^/$ -- on "
        "each such line. Nothing written."
    )


def main(argv: "list[str] | None" = None) -> int:
    parser = _Parser(
        prog="rotate_roster_format.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Convert a format-1 roster (one regex per line) to format 2\n"
            "(A<TAB>pattern blocks, B<TAB>pattern is report-only), printing\n"
            "only counts, sha256 values and the class-B names. Never prints\n"
            "a roster line."
        ),
        epilog=(
            "Name rule (applied to each roster line and to each B-file entry):\n"
            f"  {NAME_KEY_RULE}\n"
            "\n"
            "A roster line is class B iff its name equals a B-file entry's name;\n"
            "every other line is class A (blocking). Roster lines are regexes,\n"
            "so 'exact match' between a pattern and a name is not string\n"
            "equality: only wrappers that do not change which name is named are\n"
            "stripped -- a leading (?i), and \\b only as a MATCHED PAIR. A\n"
            "one-sided \\b is a prefix/suffix match, a different pattern, so it\n"
            "is kept and that line stays class A. Case is folded only when the\n"
            "pattern declares (?i); without it the comparison is case-sensitive.\n"
            "The tool never rewrites the emitted pattern.\n"
            "\n"
            "Input identity: the input roster's canonical sha256 (all trailing\n"
            "newlines stripped, exactly one appended) must equal the attested\n"
            "roster_sha256; otherwise the tool exits 2 and writes nothing.\n"
            "--attestation PATH overrides the default, which is derived from\n"
            "the scanner path as <repo>/.github/name-leak-roster.attestation.json.\n"
            "\n"
            "Parser identity: the loaded parser is probed with two synthetic\n"
            "format-2 rosters built from a fresh random token per run (an\n"
            "invalid class token; a class-A line with no declared boundary),\n"
            "and the produced roster's parse result is checked entry-by-entry\n"
            "against the produced lines. A parser that accepts either probe\n"
            "or returns a mismatched result predates the two-class format;\n"
            "the tool exits 1 and writes nothing.\n"
            "\n"
            "Class-A boundaries: every class-A line is checked with the\n"
            "scanner's own _declares_boundary. If any lack one, the tool lists\n"
            "ALL their source line numbers and exits 2 without parsing or\n"
            "writing. It never adds a boundary.\n"
            "\n"
            "Class-B width: a class-B line whose pattern declares no boundary,\n"
            "or contains regex metacharacters beyond \\b and a leading (?i),\n"
            "is reported as b_wider_than_its_name. A warning only.\n"
            "\n"
            "--dry-run runs the whole pipeline, prints counts, every class-A\n"
            "line without a declared boundary (by source line number) and the\n"
            "unmatched permitted names, and writes no file. It exits 2 if the\n"
            "real run would be refused.\n"
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
        "--attestation", metavar="PATH", default=None,
        help="Attestation JSON holding the live roster's canonical sha256 "
             "(default: <repo>/.github/name-leak-roster.attestation.json, "
             "derived from the scanner path).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing DIR/roster-format2.txt.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Run the whole pipeline and print the audit, but write no file. "
             "Exits 2 if the real run would be refused.",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).expanduser()
    if not out_dir.is_dir():
        _fail(1, "--out-dir is not an existing directory; refusing to create it")

    # ---- read the roster: bytes for the canonical hash, text for parsing ---
    roster_path = Path(args.roster).expanduser()
    try:
        roster_bytes = roster_path.read_bytes()
    except OSError as exc:
        _fail(1, f"cannot read the roster ({type(exc).__name__})")
    try:
        roster_text = roster_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(1, f"cannot decode the roster as UTF-8 ({type(exc).__name__})")
    input_sha = hashlib.sha256(roster_bytes.rstrip(b"\n") + b"\n").hexdigest()
    raw_lines = roster_text.splitlines()
    total_input_lines = len(raw_lines)

    # ---- resolve and load the real parser, then prove it knows format 2 ----
    scanner_path = _resolve_scanner(args.scanner)
    if scanner_path is None:
        tried = ", ".join(str(c) for c in _scanner_candidates(args.scanner))
        _fail(
            1,
            "no scanner found; the roster cannot be validated without the "
            f"parser, and an unvalidated roster is never emitted (tried: {tried})",
        )
    parser_fns = _load_parser(scanner_path)
    if parser_fns is None:
        _fail(
            1,
            f"{scanner_path} has no usable load_roster_file; the roster cannot "
            "be validated without the parser. Nothing written.",
        )
    parse_roster, declares_boundary = parser_fns
    _assert_parser_understands_format2(
        parse_roster, scanner_path, out_dir=out_dir, use_file=not args.dry_run
    )
    if declares_boundary is None:
        _fail(
            1,
            f"{scanner_path} has no _declares_boundary; class-A boundaries "
            "cannot be audited, and an unvalidated roster is never emitted. "
            "Nothing written.",
        )

    # ---- the input must be the live list ----------------------------------
    attestation_path = _resolve_attestation(args.attestation, scanner_path)
    attested_sha = _verify_attestation(attestation_path, input_sha)

    # R3: refuse a roster that is already class-prefixed, naming the first
    # offending 1-based line.
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

    entries = []  # (input line number, pattern text)
    skipped = 0
    for lineno, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()
        if _skippable(stripped):
            skipped += 1
            continue
        entries.append((lineno, stripped))

    # An empty name key is a prefix match on everything: refuse it on either
    # side, naming only the line number.
    for lineno, pattern in entries:
        if not _key_parts(pattern)[0]:
            _refuse_empty_name_key(lineno, "roster")

    # ---- read the B names ------------------------------------------------
    b_text = _read_text(Path(args.b_names).expanduser(), "the B-names file")
    b_entries = []  # (B-file line number, permitted name, _key_parts result)
    for b_lineno, raw in enumerate(b_text.splitlines(), start=1):
        stripped = raw.strip()
        if _skippable(stripped):
            continue
        b_entries.append((b_lineno, stripped, _key_parts(stripped)))
    for b_lineno, _b_name, b_parts in b_entries:
        if not b_parts[0]:
            _refuse_empty_name_key(b_lineno, "B-names file")

    # ---- classify --------------------------------------------------------
    out_lines = []
    b_lines = []  # (source line number, pattern text, permitted name)
    class_a = 0
    class_b = 0
    for lineno, pattern in entries:
        matched_name = None
        for _b_lineno, b_name, b_parts in b_entries:
            if _names_match(pattern, b_parts):
                matched_name = b_name
                break
        if matched_name is not None:
            class_b += 1
            out_lines.append("B\t" + pattern)
            b_lines.append((lineno, pattern, matched_name))
        else:
            class_a += 1
            out_lines.append("A\t" + pattern)

    # A B entry is unmatched only if it matches NO roster line (so duplicate
    # B entries and a line matching more than one entry are all reported
    # correctly).
    matched_b_linenos = set()
    for b_lineno, _b_name, b_parts in b_entries:
        for _lineno, pattern in entries:
            if _names_match(pattern, b_parts):
                matched_b_linenos.add(b_lineno)
                break
    unmatched_b = [
        name for b_lineno, name, _parts in b_entries
        if b_lineno not in matched_b_linenos
    ]

    produced = "".join(line + "\n" for line in out_lines)
    produced_patterns = [pattern for _lineno, pattern in entries]
    canonical = produced.rstrip("\n") + "\n"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ---- audit EVERY class-A line's boundary, all of them -----------------
    no_boundary = [
        entries[index][0]
        for index, line in enumerate(out_lines)
        if line.startswith("A\t")
        and not declares_boundary(entries[index][1])
    ]
    if no_boundary and not args.dry_run:
        _fail(2, _no_boundary_message(no_boundary))

    wider_b = [
        (lineno, name)
        for lineno, pattern, name in b_lines
        if _pattern_is_wider_than_name(pattern, declares_boundary)
    ]

    # ---- dry run: report the whole audit, write nothing -------------------
    if args.dry_run:
        code, refusal, result = _parse_text(
            parse_roster, produced, out_dir=out_dir, use_file=False
        )
        if code is None:
            _validate_parser_result(result, out_lines, scanner_path)
        if refusal:
            _reemit_parser_refusal(refusal, code, produced_patterns)
        print("dry_run: true")
        print(f"total_input_lines: {total_input_lines}")
        print(f"blank_or_comment_skipped: {skipped}")
        print(f"class_a: {class_a}")
        print(f"class_b: {class_b}")
        for lineno, name in ((ln, nm) for ln, _pat, nm in b_lines):
            print(f"  line {lineno}: {name}")
        for lineno, name in wider_b:
            print(f"b_wider_than_its_name: source roster line {lineno}: {name}")
        print(f"class_a_without_boundary: {len(no_boundary)}")
        for lineno in no_boundary:
            print(f"  no_boundary_source_line: {lineno}")
        print(f"b_names_with_no_match: {len(unmatched_b)} of {len(b_entries)}")
        for name in unmatched_b:
            print(f"  unmatched_b_name: {name}")
        print(f"roster_sha256: {digest}")
        print(f"attestation: {attestation_path}")
        print(f"attested_roster_sha256: {attested_sha}")
        print("roster_format: 2")
        print(f"pattern_count_a: {class_a}")
        print(f"pattern_count_b: {class_b}")
        print(f"name_key: {NAME_KEY_RULE}")
        print(f"scanner: {scanner_path}")
        print("output: none (dry run)")
        refused = bool(no_boundary) or code is not None
        print(f"dry_run_result: {'would refuse' if refused else 'would succeed'}")
        return 2 if refused else 0

    # ---- real run: one output file, validated first -----------------------
    out_path = out_dir / OUTPUT_NAME
    if out_path.exists() and not args.force:
        _fail(2, f"{out_path} already exists; pass --force to overwrite. "
                 "Nothing written.")

    code, refusal, result = _parse_text(
        parse_roster, produced, out_dir=out_dir, use_file=True
    )
    if code is None:
        _validate_parser_result(result, out_lines, scanner_path)
    if code is not None:
        if refusal:
            _reemit_parser_refusal(refusal, code, produced_patterns)
        _fail(
            2,
            "the produced roster was refused by the parser "
            f"(exit {code}); nothing written.",
        )

    # Unlink ONLY with --force. Without it the pre-check above has already
    # refused an existing file; a file that appeared since may belong to
    # another writer, and deleting it here would destroy it before the
    # O_EXCL create below refuses.
    if args.force and out_path.exists():
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
    for lineno, name in ((ln, nm) for ln, _pat, nm in b_lines):
        print(f"  line {lineno}: {name}")
    for lineno, name in wider_b:
        print(f"b_wider_than_its_name: source roster line {lineno}: {name}")
    print(f"b_names_with_no_match: {len(unmatched_b)} of {len(b_entries)}")
    for name in unmatched_b:
        print(f"  unmatched_b_name: {name}")
    print(f"roster_sha256: {digest}")
    print(f"attestation: {attestation_path}")
    print(f"attested_roster_sha256: {attested_sha}")
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
