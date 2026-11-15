#!/usr/bin/env python3
"""name_leak_warn.py — warn-only scanner for personal/agent name mentions in PR diffs.

Reads roster patterns from a roster file (or stdin in roster mode), reads a unified
diff from a diff file (or stdin in diff mode), and reports added lines that match
any pattern.

Usage:
    git diff origin/dev...HEAD | python3 tools/name_leak_warn.py --roster-file ROSTER
    python3 tools/name_leak_warn.py --diff-file DIFF --roster-file ROSTER
    python3 tools/name_leak_warn.py --diff-file DIFF --roster-file ROSTER --format json

Roster format: both formats are read through the scanner's parser
(``src/engram/tools/scan-leaks.py: parse_roster_text``), with format
inference. Format 2 — one ``A<TAB><regex>`` (blocking) or ``B<TAB><regex>``
(report-only) per line — keeps the class each line declares. Format 1 — plain
regex lines, blank lines and #-comments ignored — is still accepted during the
transition, and every pattern in it is class A (the legacy format's own
meaning). A roster that mixes the two is refused. This tool has no roster
parser of its own.

Output (text, default):
    path/to/file.py:42:[pattern_index=0] + the offending line content
A format-2 roster adds `` [class=A|B]`` immediately after the
``[pattern_index=N]`` tag; a format-1 roster's text output carries no tag and
is unchanged.

Output (JSON, --format json):
    {"matches": [{"file": "...", "line_number": 42, "pattern_index": 0,
                  "class": "A"|"B", "line": "..."}]}

Exit codes:
    0 — scan completed (matches found or not — this is warn-only)
    1 — usage/IO error (bad arguments, unreadable file, invalid roster pattern)
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

_SCANNER_MODULE_NAME = "_name_leak_warn_scanner"
_PARSER = None


def _load_parser():
    """Locate and load the scanner's ``parse_roster_text`` by explicit path.

    Two candidates, in order:

    1. a SIBLING ``scan-leaks.py`` — in the repo ``tools/scan-leaks.py`` is a
       symlink to the real module and ``spec_from_file_location`` reads
       through it; in CI the workflow extracts the base commit's scanner next
       to this file;
    2. the source-tree path, for a copy of this tool that sits outside
       ``tools/``.

    Never ``sys.path``: a name lookup could resolve an unrelated module. If no
    candidate loads, exit 1 with ONE line naming what could not be found and no
    traceback — this tool never falls back to its own parsing (C6).
    """
    global _PARSER
    if _PARSER is not None:
        return _PARSER
    candidates = [
        Path(__file__).with_name("scan-leaks.py"),
        Path(__file__).resolve().parent.parent
        / "src" / "engram" / "tools" / "scan-leaks.py",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                _SCANNER_MODULE_NAME, candidate
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            parser = module.parse_roster_text
        except Exception as exc:
            print(
                f"name_leak_warn: error — could not load the roster parser "
                f"from {candidate} ({type(exc).__name__})",
                file=sys.stderr,
            )
            sys.exit(1)
        _PARSER = parser
        return parser
    print(
        "name_leak_warn: error — could not locate the roster parser "
        f"(scan-leaks.py) beside {candidates[0]} or at {candidates[1]}",
        file=sys.stderr,
    )
    sys.exit(1)


def load_roster(
    roster_source: str | None, roster_file: str | None
) -> tuple[list[tuple[re.Pattern, str]], int]:
    """Load roster entries from a string or a file path.

    Both paths are parsed by the scanner's ``parse_roster_text`` — the ONE
    implementation of the roster grammar (C1) — with format inference
    (``require_classes=False``): a format-2 roster keeps the classes its lines
    declare; a format-1 roster is class A throughout. A roster that mixes the
    two is refused.

    Returns ``(entries, roster_format)`` where each entry is
    ``(compiled_pattern, class)``. Blank lines and ``#``-comment lines are
    skipped; an empty or absent source returns ``([], ...)`` (the
    skip-when-absent contract).

    Exit 1 on an unreadable file or a roster line the parser refuses. The
    parser's message names the 1-based line number, the exception class and,
    for ``re.error``, the offset — never the line, the pattern or ``str(exc)``:
    the roster is the NAME_LEAK_ROSTER secret (real names), CI stderr can reach
    a public log, and ``re.error.__str__`` embeds part of the pattern.
    """
    if roster_file is not None:
        try:
            text = Path(roster_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"name_leak_warn: error reading roster file: {exc}", file=sys.stderr)
            sys.exit(1)
    elif roster_source is not None:
        text = roster_source
    else:
        return [], 1

    parse_roster_text = _load_parser()
    patterns, roster_format = parse_roster_text(
        text,
        require_classes=False,
        program="name_leak_warn",
        exit_code=1,
    )
    # Keep the pattern and its class; the scanner's label is not part of this
    # tool's output contract.
    return [(pattern, klass) for pattern, _label, klass in patterns], roster_format


def scan_diff(
    diff_text: str, entries: list[tuple[re.Pattern, str]]
) -> list[dict]:
    """Scan added lines in a unified diff for pattern matches.

    Added lines start with '+' but NOT '+++' (which are diff headers).
    Tracks the current file path and line number in the new file.

    ``entries`` is the roster loader's ``(compiled_pattern, class)`` list. Each
    match dict carries ``file``, ``line_number``, ``pattern_index``, ``line``
    and ``class``. ONE finding per line is preserved (``break`` after the first
    match).
    """
    matches = []
    current_file = ""
    new_line_number = 0

    for raw_line in diff_text.splitlines():
        # Track current file from diff header
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            continue
        if raw_line.startswith("+++ "):
            # Handle "+++ /dev/null" or other forms
            current_file = raw_line[4:]
            continue
        if raw_line.startswith("@@"):
            # Parse hunk header: @@ -a,b +c,d @@ — extract new-file start line
            # Format: @@ -old_start[,old_count] +new_start[,new_count] @@
            try:
                hunk_info = raw_line.split("+")[1].split("@@")[0].strip()
                new_line_number = int(hunk_info.split(",")[0]) - 1
            except (IndexError, ValueError):
                new_line_number = 0
            continue
        if raw_line.startswith("---"):
            # Old file header — skip
            continue
        if raw_line.startswith("+"):
            # Added line — increment and check
            new_line_number += 1
            line_content = raw_line[1:]  # strip leading '+'
            for idx, (pattern, klass) in enumerate(entries):
                if pattern.search(line_content):
                    matches.append({
                        "file": current_file,
                        "line_number": new_line_number,
                        "pattern_index": idx,
                        "line": line_content,
                        "class": klass,
                    })
                    break  # one match per line is enough
        elif raw_line.startswith(" ") or raw_line == "":
            # Context line — counts toward new-file line numbers
            new_line_number += 1
        # Removed lines ('-') do not appear in the new file — no increment

    return matches


def format_text(matches: list[dict], roster_format: int) -> str:
    """Format matches as human-readable text.

    A ``[class=...]`` tag is appended immediately after the
    ``[pattern_index=N]`` tag ONLY when the detected roster format is 2, so a
    format-1 roster's text output stays byte-identical to the pre-inference
    form.
    """
    if not matches:
        return ""
    lines = []
    for m in matches:
        tagged = f"{m['file']}:{m['line_number']}:[pattern_index={m['pattern_index']}]"
        if roster_format == 2:
            tagged += f" [class={m['class']}]"
        lines.append(f"{tagged} {m['line']}")
    return "\n".join(lines)


def format_json(matches: list[dict]) -> str:
    """Format matches as JSON."""
    return json.dumps({"matches": matches}, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Warn-only scanner for name mentions in PR diffs."
    )
    parser.add_argument(
        "--roster-file",
        metavar="PATH",
        help="Path to newline-separated regex pattern file. "
             "If absent, reads from stdin when --diff-file is also given.",
    )
    parser.add_argument(
        "--diff-file",
        metavar="PATH",
        help="Path to unified diff file. "
             "If absent, reads diff from stdin (requires --roster-file).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    args = parser.parse_args()

    # Validate argument combination: need exactly one stdin source
    if args.roster_file is None and args.diff_file is None:
        print(
            "name_leak_warn: error — provide --roster-file when reading diff from stdin, "
            "or provide both --roster-file and --diff-file.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load roster — both formats, through the scanner's parser.
    entries, roster_format = load_roster(
        roster_source=None, roster_file=args.roster_file
    )

    # Load diff
    if args.diff_file is not None:
        try:
            diff_text = Path(args.diff_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"name_leak_warn: error reading diff file: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        diff_text = sys.stdin.read()

    # Scan
    matches = scan_diff(diff_text, entries)

    # Output
    if args.format == "json":
        print(format_json(matches))
    else:
        if matches:
            print(format_text(matches, roster_format))

    # Always exit 0 on scan completion (warn-only)
    sys.exit(0)


if __name__ == "__main__":
    main()
