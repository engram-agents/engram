#!/usr/bin/env python3
"""scan-leaks.py — defensive scanner for personal references in this public repository.

Flags committed structural patterns, absolute home paths, and a gitignored
local roster of personal names. Run standalone or wire as a pre-commit hook.

Standalone:
    python3 tools/scan-leaks.py            # exit 0 clean, 1 if leaks

Pre-commit (from inside ~/engram-alpha):
    ln -s ../../tools/scan-leaks.py .git/hooks/pre-commit

Patterns flagged (committed structural patterns):
  - Main repo path: kg_protocol_for_ai

Absolute home paths (default-deny, every git-tracked text file):
  - Any path of the form /home/<name> or /Users/<name> (the name is one whole
    segment, ended by any non-segment character or end of string; Users is
    case-insensitive) whose <name> is not in ALLOWED_HOME_SEGMENTS is a
    finding.  Public text carries no host paths,
    usernames or seat folder names.  The check is generated from the class,
    not from a list of known names: whole path segments only, so an allowed
    name never allows a longer name that merely starts with it.  It reads
    every tracked file whatever its extension (extensionless executables
    included) and skips only binary files and the declared exemptions.
    Known limit: JSON-escaped paths (backslash-escaped slashes) are not matched.

Personal-name and user-specific patterns are loaded at runtime from
.scan-leaks-local.json in the repo root (gitignored), and optionally from a
--roster-file carrying the same newline-separated regex format as the
NAME_LEAK_ROSTER CI secret. If NEITHER source supplies a pattern the scanner
runs committed structural patterns only and prints one advisory line.

See docs/internal/.scan-leaks-local.json.example for the config shape.

Scope and exclusions (what "the tree" means here — NOT every published file):
  - Candidate set: every git-tracked file, whatever its extension
    (extensionless executables included), minus the SKIP_DIRS components and
    SKIP_FILES names below, after the is_file() filter.  There is no
    extension allowlist.
  - A binary (a NUL byte in its first 8 KiB) is skipped, counted, and named.
  - SKIP_FILES: LICENSE, .scan-leaks-local.json, scan-leaks.py,
    test_release_cutover.py, RELEASING.md.
  - SKIP_DIRS: .git, __pycache__, node_modules, .claude.
  - A name in an excluded file is not scanned.
  - Known limit: a tracked symlink is a candidate only when it resolves to a
    regular file.  A symlink to a directory, or a dangling symlink, is not a
    candidate and is not reported unreadable — is_file() is the filter.

Coverage accounting (every candidate is accounted for):
  - scan() returns a ScanReport whose invariant is
    scanned + len(skipped_binary_paths) + len(unreadable_paths) == candidates.
    A file that cannot be read at all is an unreadable path — never a silent
    skip, and never counted as binary.
  - main() prints on stdout, exactly once, the frozen line
    "scan-leaks: scanned N, skipped-binary M" (.github/workflows/
    name-leak-sweep.yml parses it with an anchored regex — do not reformat
    it).  When M > 0 it follows with one line naming up to 20 skipped-binary
    paths and a "(+K more)" tail; when any candidate was unreadable it prints
    "scan-leaks: unreadable K (coverage incomplete): <paths>" (the union over
    both passes, deduped by path, same 20-path cap); when there are no
    candidates at all it prints
    "scan-leaks: no coverage: 0 candidate files under <root>; nothing was
    scanned" and does NOT print `clean`.

Exit codes:
  0 — clean, and the scan covered at least one candidate file
  1 — leaks found (a pattern hit or an absolute home path); takes precedence
      over 3 when coverage is also incomplete, and the coverage lines still
      print
  2 — invocation error
  3 — coverage incomplete: at least one candidate was unreadable, or there
      were no candidate files at all, and nothing was found
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Structural patterns with NO personal data — safe to commit.
COMMITTED_PATTERNS = [
    (re.compile(r"kg_protocol_for_ai"), "main repo path"),
]

LOCAL_CONFIG_NAME = ".scan-leaks-local.json"

# --- Absolute home paths: default-deny -------------------------------------
# The class definition (regex + declared allowed set with reasons) lives in ONE
# place, shared with the build engine's foreign-bundle leak scan:
# engine/home_path_class.py.  Deployed it sits beside this file under engine/;
# in the source tree it is under src/build/engine/.
def _load_home_path_class():
    import importlib.util
    here = Path(__file__).resolve().parent
    candidates = [
        here / "engine" / "home_path_class.py",
        here.parent.parent / "build" / "engine" / "home_path_class.py",
    ]
    for cand in candidates:
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("home_path_class", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("home_path_class.py not found beside scan-leaks.py: "
                      + ", ".join(str(c) for c in candidates))


_hpc = _load_home_path_class()
HOME_PATH_RE = _hpc.HOME_PATH_RE
ALLOWED_HOME_SEGMENTS = _hpc.ALLOWED_HOME_SEGMENTS
_DISALLOWED_HOME_PATH_RE = _hpc.disallowed_home_path_regex()

# Files exempt from the home-path check, by repo-relative path, each with a
# reason.  Deliberately EMPTY: the scanner, its definition module and every test
# plant their paths in forms the class does not match.  Keep the mechanism: a
# future exemption is a declared "path: reason" entry here, never a directory.
HOME_PATH_EXEMPT_FILES: dict[str, str] = {}
# LICENSE: copyright lines are intentional.
# .scan-leaks-local.json: the local personal-name roster must be skipped —
#   it contains the exact patterns the scanner is looking for (by design).
# scan-leaks.py: STRUCTURAL self-skip — the scanner's own source always
#   contains the literal patterns it checks (docstring + COMMITTED_PATTERNS
#   definition).  This is inherent to the tool's design, not personal-data.
# test_release_cutover.py: deliberately plants the structural pattern as test
#   fixtures (e.g. "kg_protocol_for_ai" in _commit_file calls) to verify the
#   release-cutover scanner picks them up.  Same structural rationale as the
#   scan-leaks.py self-skip: patterns present by design, not as actual leaks.
# RELEASING.md: the dev-internal release-process doc.  The scan no longer flags
#   its repo names, so this entry, like test_release_cutover.py's, is kept only
#   until the retired release tooling and RELEASING.md are removed by the
#   release-procedure change.
SKIP_FILES = {"LICENSE", LOCAL_CONFIG_NAME, "scan-leaks.py", "test_release_cutover.py", "RELEASING.md"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".claude"}
# .claude: harness-internal (agent worktrees + transcripts) — stale fairy
# worktrees carry pre-fix file copies that read as false-positive leaks.

# How many paths a naming line shows before its "(+K more)" tail.
NAMED_PATH_CAP = 20


@dataclass
class ScanReport:
    """What a scan ranged over, as well as what it found.

    Every candidate file is accounted for exactly once — scanned, skipped as
    binary, or unreadable — and ``scan()`` asserts that invariant:

        scanned + len(skipped_binary_paths) + len(unreadable_paths) == candidates
    """
    findings: list[tuple[str, int, str, str]]
    scanned: int
    skipped_binary_paths: list[str]
    unreadable_paths: list[str]
    candidates: int

    @property
    def skipped_binary(self) -> int:
        """The skipped-binary count, derived from the paths (the source of
        truth), so the number can never disagree with the names it prints."""
        return len(self.skipped_binary_paths)


def format_paths(paths: list[str], cap: int = NAMED_PATH_CAP) -> str:
    """Join paths for a log line: at most ``cap`` named, then '(+K more)'."""
    shown = ", ".join(paths[:cap])
    more = len(paths) - cap
    if more > 0:
        shown += f" (+{more} more)"
    return shown


def load_local_patterns(root: Path, config_path: Path | None = None) -> tuple[list, bool]:
    """Load personal-name patterns from the gitignored local config.

    Returns (patterns, config_present) where patterns is a list of
    (compiled_regex, label) tuples.  config_present is False when the file
    does not exist at all; True even when the file exists but is malformed or
    has no valid patterns (so the caller can distinguish "no file" from
    "file exists but produced nothing").
    """
    if config_path is None:
        config_path = root / LOCAL_CONFIG_NAME
    if not config_path.exists():
        return [], False

    try:
        raw = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"scan-leaks: warning — could not load {config_path.name}: {exc}",
              file=sys.stderr)
        return [], True  # file existed but malformed; treat as present

    patterns = []
    for i, entry in enumerate(raw.get("patterns", []), start=1):
        try:
            patterns.append((re.compile(entry["regex"]), entry["label"]))
        except (KeyError, TypeError, re.error, OverflowError, RecursionError) as exc:
            # The roster is a personal-name list: the entry, its label and
            # str(re.error) can all carry a name.  Report only the 1-based
            # position, the exception class, and (for KeyError) the missing
            # schema key, which is ours and not user data.
            detail = f" (missing key {exc.args[0]!r})" if isinstance(exc, KeyError) else ""
            print(f"scan-leaks: warning — skipping bad pattern entry {i}: "
                  f"{type(exc).__name__}{detail}", file=sys.stderr)
    return patterns, True


def load_roster_file(path: Path) -> list[tuple[re.Pattern, str]]:
    """Load newline-separated regexes from an external roster file.

    This is the SAME format as the ``NAME_LEAK_ROSTER`` CI secret: one regex
    per line, with blank lines and lines whose first non-space character is
    ``#`` ignored.  The label is fixed and generic; the file carries no labels.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # An explicitly requested roster that cannot be read must not silently
        # shrink the scan: that is an invocation error, not a clean run.
        print(f"scan-leaks: cannot read roster file ({type(exc).__name__})",
              file=sys.stderr)
        sys.exit(2)

    patterns = []
    supplied = 0
    rejected = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        supplied += 1
        try:
            patterns.append((re.compile(stripped), "roster file"))
        except (re.error, OverflowError, RecursionError) as exc:
            rejected += 1
            # The roster file is a personal-name list: the line and
            # str(re.error) can both carry a name.  Report only the 1-based
            # line number, the exception class, and (for re.error) its offset
            # — never the pattern text.
            detail = (f" at offset {exc.pos}" if isinstance(exc, re.error)
                      else "")
            print(f"scan-leaks: warning — skipping bad roster line {lineno}: "
                  f"{type(exc).__name__}{detail}", file=sys.stderr)

    if supplied and not patterns:
        # An explicitly requested roster that supplied no usable pattern must
        # not become a clean run against nothing.  CI pre-validates its own
        # roster before invoking --roster-file, so this is the standalone
        # flag's contract, not CI's.  The count only — never the pattern text.
        print(
            f"scan-leaks: all {rejected} roster line(s) were rejected as "
            "invalid; refusing to scan against an empty roster",
            file=sys.stderr,
        )
        sys.exit(2)
    return patterns


def _git_tracked_files(root: Path) -> list[Path] | None:
    """Return the list of git-tracked files under root, or None on failure.

    Uses ``git ls-files -z`` so filenames with spaces/newlines are handled
    correctly.  Returns None if git is unavailable or the command fails (e.g.
    root is not inside a git repo), so the caller can fall back to os.walk.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
        )
    except FileNotFoundError:
        # git not installed
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout
    if not raw:
        return []
    # NUL-split; decode each path; filter empty strings from trailing NUL
    return [root / p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p]


def scan(root: Path, leak_patterns: list, use_git: bool = True) -> ScanReport:
    """Scan root for leak_patterns and return a ScanReport.

    ``report.findings`` is a list of (relpath, lineno, label, snippet).

    Candidate set: every git-tracked file, whatever its extension
    (extensionless executables included), minus the SKIP_DIRS components and
    SKIP_FILES names, after the ``is_file()`` filter — the same all-text
    domain ``scan_home_paths`` uses.  An extension allowlist used to select
    files here, so the release leak gate never read shipped
    ``.js``/``.html``/``.ts``/``.css``/``.txt`` files or extensionless scripts
    (#3732).

    When use_git is True (default), candidate files are enumerated via
    ``git ls-files -z`` so that untracked files — gitignored local notes,
    developer scratch, active-work dirs — are invisible to the scan.  This
    keeps the repo-clean test (test_real_repo_scan_no_config_exits_clean) env-
    independent: it passes on both CI fresh checkouts and developer machines
    with local untracked cruft.

    If git is unavailable or the command fails the scanner falls back to an
    os.walk of root — scanning too much is safer than scanning nothing.

    When use_git is False (explicit-path invocation), the os.walk path is used
    unconditionally so that callers can target specific non-repo directories.

    A file whose first 8 KiB contains a NUL byte is binary: skipped, counted
    in ``skipped_binary_paths``, and named by the caller.  Everything else is
    decoded as UTF-8 with errors replaced, so a non-UTF-8 text file is still
    scanned and ``scanned`` counts it.  A file that cannot be read at all is
    recorded in ``unreadable_paths`` — never dropped silently, and never
    counted as binary.
    """
    tracked = _git_tracked_files(root) if use_git else None

    if tracked is not None:
        # Git-tracked enumeration: skip SKIP_DIRS and SKIP_FILES by name.
        # Every remaining file is a candidate, whatever its extension.
        candidates = []
        for fpath in tracked:
            # Check every component for SKIP_DIRS membership
            parts = fpath.relative_to(root).parts
            if any(p in SKIP_DIRS for p in parts):
                continue
            if fpath.name in SKIP_FILES:
                continue
            candidates.append(fpath)
    else:
        # Fallback: os.walk (same all-text policy)
        candidates = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if fname in SKIP_FILES:
                    continue
                candidates.append(Path(dirpath) / fname)

    # A tracked symlink can point at a directory (tools/engine, tools/engram-pkg
    # in this repo).  It is not a text file to scan, so it is not a candidate —
    # which is what makes the three buckets partition the files considered.
    # A dangling symlink falls out here too: is_file() is False, so it is not
    # a candidate and not unreadable (the documented known limit).
    candidates = [f for f in candidates if f.is_file()]

    findings = []
    scanned = 0
    skipped_binary_paths = []
    unreadable_paths = []
    for fpath in candidates:
        rel = fpath.relative_to(root)
        try:
            data = fpath.read_bytes()
        except OSError:
            unreadable_paths.append(rel.as_posix())
            continue
        if b"\x00" in data[:8192]:
            skipped_binary_paths.append(rel.as_posix())
            continue
        content = data.decode("utf-8", errors="replace")
        scanned += 1
        for i, line in enumerate(content.splitlines(), 1):
            for pattern, label in leak_patterns:
                if pattern.search(line):
                    findings.append((str(rel), i, label, line.strip()[:120]))
                    break

    skipped_binary_paths.sort()
    unreadable_paths.sort()
    report = ScanReport(
        findings=findings,
        scanned=scanned,
        skipped_binary_paths=skipped_binary_paths,
        unreadable_paths=unreadable_paths,
        candidates=len(candidates),
    )
    # The invariant is the contract: a count that does not reconcile is a
    # scanner bug, never something to print and move past.
    accounted = (report.scanned + len(report.skipped_binary_paths)
                 + len(report.unreadable_paths))
    if accounted != report.candidates:
        raise AssertionError(
            f"scan-leaks coverage does not reconcile: scanned {report.scanned} + "
            f"skipped-binary {len(report.skipped_binary_paths)} + unreadable "
            f"{len(report.unreadable_paths)} = {accounted}, candidates "
            f"{report.candidates}"
        )
    return report


def scan_home_paths(root: Path, use_git: bool = True
                    ) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Return (findings, unreadable_paths) for the absolute-home-path check.

    findings is (relpath, lineno, segment) for every disallowed absolute home
    path.  unreadable_paths is the sorted repo-relative posix paths whose read
    raised OSError, so the caller can say a pass did not cover them.

    Domain: every git-tracked file (``git ls-files``), whatever its extension,
    including extensionless executables, after the same ``is_file()`` filter
    ``scan()`` applies — a tracked symlink to a directory is not a file to
    read and is not unreadable.  A file whose first 8 KiB contains a NUL byte
    is binary and skipped, as is any file in HOME_PATH_EXEMPT_FILES.  Text is
    decoded as UTF-8 with errors replaced, so a non-UTF-8 file is still
    scanned.  The verdict (allowed or not) is made inside the shared regex.
    Falls back to an os.walk of root
    when git is unavailable (scanning too much beats scanning nothing).
    """
    tracked = _git_tracked_files(root) if use_git else None
    if tracked is None:
        tracked = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            tracked.extend(Path(dirpath) / f for f in files)
    tracked = [f for f in tracked if f.is_file()]

    findings = []
    unreadable_paths = []
    for fpath in tracked:
        rel = fpath.relative_to(root).as_posix()
        if rel in HOME_PATH_EXEMPT_FILES:
            continue
        try:
            data = fpath.read_bytes()
        except OSError:
            unreadable_paths.append(rel)
            continue
        if b"\x00" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            for m in _DISALLOWED_HOME_PATH_RE.finditer(line):
                findings.append((rel, i, m.group(1).rstrip(".")))
    unreadable_paths.sort()
    return findings, unreadable_paths


def main():
    parser = argparse.ArgumentParser(description="Scan alpha for personal-info leaks")
    parser.add_argument("--root", default=None,
                        help="Root to scan (default: parent of tools/ dir, i.e., the alpha repo)")
    parser.add_argument("--config-path", default=None,
                        help=f"Explicit path to the local config file "
                             f"(default: <root>/{LOCAL_CONFIG_NAME})")
    parser.add_argument("--roster-file", default=None,
                        help="Newline-separated regexes, the same format as the "
                             "NAME_LEAK_ROSTER secret (blank lines and #-comment "
                             "lines ignored); scanned alongside the committed "
                             "patterns and the local roster")
    # A public CI log must not carry a name: `path:line [label] snippet`
    # prints it twice, so --redact keeps only the repo-relative path and line.
    parser.add_argument("--redact", action="store_true",
                        help="Print each finding as repo-relative-path:line only "
                             "(no label, no snippet, no home-path segment)")
    args = parser.parse_args()

    if args.root:
        root = Path(args.root).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parent.parent

    if not root.exists():
        print(f"scan-leaks: root not found: {root}", file=sys.stderr)
        sys.exit(2)

    config_path = Path(args.config_path).expanduser().resolve() if args.config_path else None
    local_patterns, config_present = load_local_patterns(root, config_path)
    roster_file_patterns = (
        load_roster_file(Path(args.roster_file).expanduser().resolve())
        if args.roster_file else []
    )

    personal_patterns = local_patterns + roster_file_patterns
    if not personal_patterns:
        # Advisory fires only when NEITHER roster source produced a pattern —
        # covers "file absent" and "file present but malformed/empty".  With
        # --roster-file supplying patterns the personal-name net DID run, so
        # claiming a skip here would be false and must not print.
        if args.roster_file:
            reason = (f"{LOCAL_CONFIG_NAME} absent"
                      if not config_present
                      else f"{LOCAL_CONFIG_NAME} produced no patterns")
            reason += " and --roster-file produced no patterns"
        else:
            reason = (f"{LOCAL_CONFIG_NAME} absent"
                      if not config_present else f"{LOCAL_CONFIG_NAME} produced no patterns")
        print(
            f"scan-leaks: no local roster ({reason})"
            " — personal-name scan skipped; structural patterns only"
        )

    all_patterns = COMMITTED_PATTERNS + personal_patterns

    report = scan(root, all_patterns)
    leaks = report.findings
    # One greppable summary line, on clean and dirty runs alike: a clean result
    # says what it ranged over, so an empty finding list cannot hide a blind spot.
    # FROZEN SHAPE: .github/workflows/name-leak-sweep.yml parses this line with
    # an anchored regex and the release docs quote it.  New information goes on
    # the lines below, never into this one.
    print(f"scan-leaks: scanned {report.scanned}, skipped-binary {report.skipped_binary}")
    if report.skipped_binary_paths:
        print("scan-leaks: skipped-binary files: "
              + format_paths(report.skipped_binary_paths))
    home_findings, home_unreadable = scan_home_paths(root)
    # The union is deduped by path: two passes over one file are one file.
    unreadable = sorted(set(report.unreadable_paths) | set(home_unreadable))
    if unreadable:
        print(f"scan-leaks: unreadable {len(unreadable)} (coverage incomplete): "
              + format_paths(unreadable))
    coverage_incomplete = bool(unreadable) or report.candidates == 0
    if report.candidates == 0:
        print(f"scan-leaks: no coverage: 0 candidate files under {root}; "
              "nothing was scanned")
    if not leaks and not home_findings:
        if coverage_incomplete:
            # A clean result that ranged over nothing (or over files it could
            # not read) is not evidence of a clean tree.  Distinct from 1 so a
            # caller can tell "dirty" from "did not look".
            sys.exit(3)
        print(f"scan-leaks: clean ({root})")
        sys.exit(0)

    print(f"scan-leaks: {len(leaks) + len(home_findings)} potential leak(s) in {root}",
          file=sys.stderr)
    for rel, lineno, label, snippet in leaks:
        if args.redact:
            print(f"{rel}:{lineno}", file=sys.stderr)
        else:
            print(f"  {rel}:{lineno} [{label}] {snippet}", file=sys.stderr)
    for rel, lineno, seg in home_findings:
        if args.redact:
            print(f"{rel}:{lineno}", file=sys.stderr)
        else:
            print(f"  {rel}:{lineno} [absolute home path] segment: {seg}", file=sys.stderr)
    # The roster instruction is scoped to ROSTER/PATTERN hits: a roster entry
    # names a string already published, so the fix is in the tree, never in the
    # roster.  An absolute-home-path hit has nothing to do with the roster, so
    # telling its operator to leave the roster alone is misdirection.
    if leaks:
        print(
            "scan-leaks: a pattern hit means the string is already in the "
            "published tree; fix it in the release PR; do not widen or edit "
            "the roster to silence it.",
            file=sys.stderr,
        )
    if home_findings:
        print(
            "scan-leaks: an absolute home path hit means a host path is in the "
            "published tree; remove it in the release PR. The roster does not "
            "apply to this finding.",
            file=sys.stderr,
        )
    sys.exit(1)


if __name__ == "__main__":
    main()
