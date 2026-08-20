#!/usr/bin/env python3
"""os-harness-check.py — find OS/harness portability violations in shipped code.

Companion to docs/os-harness-constraints.md. Today it checks one constraint,
the sharpest one: stock macOS ships **bash 3.2.57**, and our shipped shell
scripts run under `set -euo pipefail`, so a single bash-4-only construct aborts
an installer mid-run — after partial work, before the step the operator cared
about. That is a silent-at-the-wrong-moment failure, which is why it is worth a
mechanical check rather than a line in a doc.

WHY THE SELF-TEST IS NOT OPTIONAL
---------------------------------
A scanner that reports "clean" is indistinguishable from a scanner whose
patterns never matched anything -- the same unfalsifiability that makes an
absence-claim worthless without a coverage bound. So `--self-test` runs every
pattern against a known-positive fixture and FAILS LOUD if any pattern cannot
find its own planted sample. A clean tree is only reportable when the self-test
passed; `main()` therefore runs the self-test first and refuses to report
success if it fails.

Empirically grounded: on 2026-08-05 these patterns found 0 hits across 34
tracked .sh files on dev@4a50e64, and 18 hits on PR #1797's head (mapfile x10,
local -A x4, local -n x4) -- so the clean result on dev is a real negative, not
a broken regex.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Each entry: (label, compiled pattern, known-positive sample that MUST match).
# The sample is the positive control -- keep it a real construct, not a
# contrived string, so a pattern that only matches toy input is caught here.
CHECKS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "mapfile/readarray (bash 4+)",
        re.compile(r"\b(?:mapfile|readarray)\b"),
        "mapfile -d '' -t entries < <(find . -print0)",
    ),
    (
        "associative array: declare -A / local -A (bash 4+)",
        re.compile(r"\b(?:declare|local|typeset)\s+-[A-Za-z]*A\b"),
        "local -A seen",
    ),
    (
        "nameref: declare -n / local -n (bash 4.3+)",
        re.compile(r"\b(?:declare|local|typeset)\s+-[A-Za-z]*n\b"),
        'local -n before_ref="$1"',
    ),
    (
        "case modification ${v^^} / ${v,,} (bash 4+)",
        re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?:\^\^|,,)"),
        'echo "${name,,}"',
    ),
    (
        "coproc (bash 4+)",
        re.compile(r"\bcoproc\b"),
        "coproc reader { cat; }",
    ),
    (
        "&>> append-both redirect (bash 4+)",
        re.compile(r"&>>"),
        "cmd &>> out.log",
    ),
]


# A file may declare itself out of scope with this directive in its header.
# Deliberately a MARKER IN THE FILE rather than an allowlist in this script:
# the declaration then travels with the code, and a reviewer editing that file
# sees it. An allowlist here would be invisible from the file it exempts.
EXEMPT_DIRECTIVE = "os-harness-check: linux-only"
_HEADER_LINES = 20  # how far into a file the directive is honored

_SHELL_SHEBANG = re.compile(r"^#!.*\b(?:bash|sh)\b")


def _has_shell_shebang(path: Path) -> bool:
    try:
        with path.open("r", errors="ignore") as fh:
            return bool(_SHELL_SHEBANG.match(fh.readline()))
    except OSError:
        return False


def _exempt_reason(path: Path) -> str | None:
    """Return the exemption line if the file declares itself linux-only.

    Scoped to the header so a directive cannot hide 400 lines down, and so
    the exemption is visible to anyone opening the file.
    """
    try:
        with path.open("r", errors="ignore") as fh:
            for _ in range(_HEADER_LINES):
                line = fh.readline()
                if not line:
                    break
                if EXEMPT_DIRECTIVE in line:
                    return line.strip()
    except OSError:
        pass
    return None


def _tracked_shell_files(root: Path) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Return (scanned, exempted) git-tracked shell scripts.

    Selection is by extension OR shebang. The `*.sh` glob alone was the
    original coverage bound and it was wrong at this tool's own bullseye:
    `tools/agentctl-mac` -- the macOS-targeted script, the exact thing this
    checker exists to protect -- has no extension, so it sat permanently
    outside the scan while the checker printed "clean". A case-modification
    added to it later would have shipped silently. (Found in colleague review
    of the PR that introduced this file.)

    Untracked files are still excluded: scratch never ships.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [], []

    scanned: list[Path] = []
    exempted: list[tuple[Path, str]] = []
    for rel in out.split("\n"):
        rel = rel.strip()
        if not rel:
            continue
        path = root / rel
        if not path.is_file():
            continue
        if path.suffix != ".sh" and not (path.suffix == "" and _has_shell_shebang(path)):
            continue
        reason = _exempt_reason(path)
        if reason:
            exempted.append((path, reason))
        else:
            scanned.append(path)
    return scanned, exempted


def _strip_comment(line: str) -> str:
    """Drop whole-line comments so a doc-comment naming a construct (this file's
    own docstring, or the 'do not re-add these' note in the macOS setup doc)
    isn't reported as a violation. Deliberately NOT a full shell parser: a
    trailing comment after real code still gets scanned, which errs toward
    over-reporting. Over-reporting is recoverable by a human read; a
    false clean is the failure this script exists to prevent.
    """
    return "" if line.lstrip().startswith("#") else line


def scan(root: Path) -> list[tuple[str, Path, int, str]]:
    findings: list[tuple[str, Path, int, str]] = []
    scanned, _exempt = _tracked_shell_files(root)
    for path in scanned:
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = _strip_comment(raw)
            if not line:
                continue
            for label, pattern, _sample in CHECKS:
                if pattern.search(line):
                    findings.append((label, path, lineno, raw.strip()[:100]))
    return findings


def self_test() -> bool:
    """Every pattern must find its own known-positive sample.

    Also asserts each pattern does NOT match a portable bash-3.2 equivalent,
    so a pattern broadened until it matches everything is caught too -- a
    detector with no false-negative bound and no false-positive bound is not
    a detector.
    """
    negatives = [
        'while IFS= read -r l; do arr+=("$l"); done < f',   # portable mapfile
        "local seen",                                        # plain local
        'echo "$name" | tr "[:upper:]" "[:lower:]"',         # portable lowercase
        "cmd >> out.log 2>&1",                               # portable append-both
    ]
    ok = True
    for label, pattern, sample in CHECKS:
        if not pattern.search(sample):
            print(f"SELF-TEST FAIL: {label!r} did not match its own sample: {sample!r}",
                  file=sys.stderr)
            ok = False
        for neg in negatives:
            if pattern.search(neg):
                print(f"SELF-TEST FAIL: {label!r} matched a PORTABLE construct: {neg!r}",
                      file=sys.stderr)
                ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="repo root to scan (default: cwd)")
    ap.add_argument("--self-test", action="store_true",
                    help="run only the detector self-test and exit")
    args = ap.parse_args()

    if not self_test():
        print("os-harness-check: DETECTOR BROKEN -- refusing to report a result.",
              file=sys.stderr)
        return 2
    if args.self_test:
        print(f"os-harness-check: self-test OK ({len(CHECKS)} patterns, "
              "each matches its known-positive and rejects portable equivalents)")
        return 0

    root = Path(args.root).resolve()
    files, exempt = _tracked_shell_files(root)
    findings = scan(root)

    # Report exemptions ALWAYS, on both the clean and dirty paths. A silently
    # skipped file is exactly the coverage bound that made the *.sh glob a
    # defect -- an absence is only meaningful when its scope is stated.
    for path, reason in exempt:
        rel = path.relative_to(root) if path.is_relative_to(root) else path
        print(f"os-harness-check: exempt {rel} -- {reason}", file=sys.stderr)

    if not findings:
        print(f"os-harness-check: clean -- {len(files)} shell scripts scanned "
              f"({len(exempt)} exempt), {len(CHECKS)} constructs, self-test passed.")
        return 0

    print(f"os-harness-check: {len(findings)} violation(s) in {len(files)} "
          f"scanned shell scripts ({len(exempt)} exempt)\n", file=sys.stderr)
    for label, path, lineno, snippet in findings:
        rel = path.relative_to(root) if path.is_relative_to(root) else path
        print(f"  {rel}:{lineno} [{label}]\n      {snippet}", file=sys.stderr)
    print("\nStock macOS is bash 3.2.57. Shipped scripts run under "
          "`set -euo pipefail`, so the first of these aborts the script "
          "mid-run. See docs/os-harness-constraints.md.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
