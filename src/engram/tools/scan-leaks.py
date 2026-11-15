#!/usr/bin/env python3
"""scan-leaks.py — defensive scanner for personal references in this public repository.

Flags committed structural patterns, absolute home paths, and a gitignored
local roster of personal names. Run standalone or wire as a pre-commit hook.

Standalone:
    python3 tools/scan-leaks.py            # exit 0 clean, 1 if leaks, 2 on an
                                           # internal or invocation error,
                                           # 3 if coverage incomplete

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
--roster-file in one shared two-class format: one `A<TAB><regex>` (blocking) or
`B<TAB><regex>` (report-only) per line, blank lines and #-comment lines
ignored. Every line must carry a class; there is no default, and one bad line
refuses the whole roster (exit 2). If NEITHER source supplies a pattern the
scanner runs committed structural patterns only and prints one advisory line.

Class A blocks and prints as a COUNT ONLY (no path, line, label or snippet: a
location in a public log is a lasting pointer to a protected name). Class B is
permitted content, prints per line, and never blocks. COMMITTED_PATTERNS, the
local roster and absolute-home-path findings are class A in code — they are
the patterns this instrument has always blocked on. When one line matches a
class-B and a class-A pattern, the line is class A: a report-only pattern must
never mask a blocking one, whatever order the roster lists them in.

See docs/internal/.scan-leaks-local.json.example for the config shape.

Scope and exclusions (what "the tree" means here — NOT every published file):
  - Candidate set: every enumerated path — every git-tracked file, whatever
    its extension (extensionless executables included), or the os.walk output
    when git is unavailable — minus the SKIP_DIRS components and SKIP_FILES
    names below, BEFORE the file-type filter.  There is no extension
    allowlist.  Every candidate is then classified exactly once (see
    Coverage accounting).
  - A binary (a NUL byte in its first 8 KiB) is skipped, counted, and named.
  - SKIP_FILES: LICENSE, .scan-leaks-local.json, scan-leaks.py,
    test_release_cutover.py, RELEASING.md.
  - SKIP_DIRS: .git, __pycache__, node_modules, .claude.
  - A name in an excluded file is not scanned.
  - A symlink -- git-tracked, or listed by the ``os.walk`` fallback, which
    yields a directory symlink in its ``dirs`` list -- is scanned by its
    PUBLISHED CONTENT, which is its link text (``os.readlink``), in both
    passes; the link is never followed or descended.  A dangling symlink is
    therefore a covered scan (not `missing`), a symlink to a directory is
    scanned (not `not-a-file`), and a symlink whose target is also tracked is
    not read twice.

Coverage accounting (every candidate is accounted for):
  - scan() returns a ScanReport whose invariant is
    scanned + len(skipped_binary_paths) + len(unreadable_paths)
            + len(missing_paths) + len(not_a_file_paths) == candidates.
    A path that cannot be inspected or read is an unreadable path, and a path
    that is not there is a missing path — never a silent skip, and never
    counted as binary.  An unsearchable directory found by os.walk is one
    candidate covering a subtree that could not be enumerated.  "Missing"
    means only that the path vanished between enumeration and classification
    (a real race), not that a symlink dangles: a dangling symlink's link text
    is readable and is scanned.
  - main() prints on stdout, exactly once, the frozen line
    "scan-leaks: scanned N, skipped-binary M" (.github/workflows/
    name-leak-sweep.yml parses it with an anchored regex — do not reformat
    it).  When --roster-file was supplied, the line
    "scan-leaks: roster patterns A=<n>, B=<m>" follows it and reports the
    per-class counts of the PARSED roster, which name-leak-sweep.yml compares
    against the attestation; new information goes BELOW the frozen line,
    never into it.  When M > 0 the frozen line is followed with one line
    naming up to 20 skipped-binary
    paths and a "(+K more)" tail; when any candidate was unreadable it prints
    "scan-leaks: unreadable K (coverage incomplete): <paths>" (the union over
    both passes, deduped by path); when any was missing it prints
    "scan-leaks: missing K (coverage incomplete): <paths>"; when any was not
    a regular file it prints
    "scan-leaks: not-a-file N (not scanned): <paths>" (same 20-path cap).
    When there are no candidates at all it prints
    "scan-leaks: no coverage: 0 candidate files under <root>; nothing was
    scanned" and does NOT print `clean`.

Exit codes:
  0 — no class-A hit and the scan covered at least one candidate file (a
      class-B hit alone does NOT block: B is permitted content, counted and
      printed, never a failure)
  1 — class-A hits: a class-A pattern hit or an absolute home path
  2 — invocation error, including a roster that does not parse (format 2: no
      class token, an unknown class letter, an uncompilable regex, a tab
      after the first, or a class-A pattern with no declared boundary), OR an
      internal error: the scanner crashed.  One stderr line
      `scan-leaks: internal error: <ExceptionType>` says which, so a crash
      never lands on exit 1, which every consumer reads as "leaks found"
  3 — coverage incomplete: at least one candidate was unreadable or missing,
      or there were no candidate files at all, and no class-A hit (a
      not-a-file candidate alone does not make coverage incomplete)
"""

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The two roster classes.  A blocks; B is counted and never blocks.
CLASS_A = "A"
CLASS_B = "B"
ROSTER_CLASSES = (CLASS_A, CLASS_B)

# Structural patterns with NO personal data — safe to commit.  Class A is
# declared in CODE, never inferred from a label: these are the patterns this
# instrument has always blocked on.
COMMITTED_PATTERNS = [
    (re.compile(r"kg_protocol_for_ai"), "main repo path", CLASS_A),
]

# A class-A pattern must DECLARE a boundary — `\b`, a lookaround, or an anchor.
# Refuse it otherwise, never rewrite it: a silently altered pattern is one
# whose meaning differs from what its author wrote.  Class B is exempt.
_A_BOUNDARY_MARKERS = ("\\b", "(?=", "(?!", "(?<=", "(?<!", "^", "$")

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


_HPC = None


def _home_path_class():
    """Load the shared home-path class module, once, on first use.

    Reached from inside main()'s try (through ``_run``), never at import time:
    a scanner deployed without its sibling ``engine/home_path_class.py`` exits
    2 with one ``scan-leaks: internal error: ImportError`` line instead of
    dying on import with a traceback and exit 1, which every consumer reads as
    "leaks found".  The loader's search order and its message are unchanged.
    """
    global _HPC
    if _HPC is None:
        _HPC = _load_home_path_class()
    return _HPC

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

    Every candidate path is accounted for exactly once — scanned, skipped as
    binary, unreadable, missing, or not a regular file — and ``scan()``
    asserts that invariant:

        scanned + len(skipped_binary_paths) + len(unreadable_paths)
                + len(missing_paths) + len(not_a_file_paths) == candidates
    """
    findings: list[tuple[str, int, str, str, str]]
    scanned: int
    skipped_binary_paths: list[str]
    unreadable_paths: list[str]
    candidates: int
    missing_paths: list[str] = field(default_factory=list)
    not_a_file_paths: list[str] = field(default_factory=list)

    @property
    def skipped_binary(self) -> int:
        """The skipped-binary count, derived from the paths (the source of
        truth), so the number can never disagree with the names it prints."""
        return len(self.skipped_binary_paths)


def _classify(path: Path) -> tuple[str, Path | None]:
    """Classify one enumerated candidate by what it is on disk.

    This is the ONE seam both passes use, so neither can crash on a path it
    cannot inspect and neither invents its own filter.  ``path.lstat()`` does
    NOT follow symlinks, so a symlink is classified as itself — its published
    content is its link text, never its target's:

        ("symlink", None)     a symlink; scan its os.readlink() text
        ("file", None)        a regular file; read it
        ("not-a-file", None)  exists, but is not a regular file or a symlink
                              (FIFO, socket, device, gitlink/directory):
                              carries no text to scan
        ("missing", path)     the path itself is not there
        ("unreadable", path)  any other stat failure (EACCES, ELOOP after a
                              race, ENAMETOOLONG, ...)

    Only ENOENT is "missing": every other OSError means the scanner could not
    inspect the path, which is a coverage hole, not an absence.  A dangling
    symlink is NOT missing — lstat succeeds, and its link text is readable.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ("missing", path)
    except OSError:
        return ("unreadable", path)
    if stat.S_ISLNK(info.st_mode):
        return ("symlink", None)
    if not stat.S_ISREG(info.st_mode):
        return ("not-a-file", None)
    return ("file", None)


def _relative_dir(root: Path, directory) -> str:
    """A failed directory as a repo-relative posix path with a trailing '/'.

    One entry covers the whole subtree that could not be enumerated: its file
    count is unknown, so it is one coverage hole, not zero.
    """
    try:
        rel = Path(directory).relative_to(root).as_posix()
    except ValueError:
        rel = os.path.relpath(directory, root).replace(os.sep, "/")
    return "" if rel == "." else rel + "/"


def _escape_path(path: str) -> str:
    """Escape line-breaking characters so one path cannot forge or split a line.

    A tracked name may contain a newline, and a printed name that carried it
    raw would put a second `scan-leaks: scanned N, skipped-binary M` line (or
    half a finding) on stdout.  ``\\n`` becomes ``\\\\n``, ``\\r`` becomes
    ``\\\\r``, any other C0 control becomes ``\\\\xNN``; C1 controls
    (U+0080-U+009F) also become ``\\\\xNN``, and U+2028/U+2029 become
    ``\\\\u2028``/``\\\\u2029`` -- every one of those is a line break to
    ``str.splitlines()``, so no escaped name can split a printed line.  Every
    other character is unchanged.  The frozen summary line carries no path and
    is untouched.
    """
    escaped = []
    for ch in path:
        if ch == "\n":
            escaped.append("\\n")
        elif ch == "\r":
            escaped.append("\\r")
        elif ord(ch) < 0x20:
            escaped.append(f"\\x{ord(ch):02x}")
        elif 0x80 <= ord(ch) <= 0x9f:
            escaped.append(f"\\x{ord(ch):02x}")
        elif ch == chr(0x2028):
            escaped.append("\\u2028")
        elif ch == chr(0x2029):
            escaped.append("\\u2029")
        else:
            escaped.append(ch)
    return "".join(escaped)


def format_paths(paths: list[str], cap: int = NAMED_PATH_CAP) -> str:
    """Join paths for a log line: at most ``cap`` named, then '(+K more)'."""
    shown = ", ".join(_escape_path(p) for p in paths[:cap])
    more = len(paths) - cap
    if more > 0:
        shown += f" (+{more} more)"
    return shown


def load_local_patterns(root: Path, config_path: Path | None = None) -> tuple[list, bool]:
    """Load personal-name patterns from the gitignored local config.

    Returns (patterns, config_present) where patterns is a list of
    (compiled_regex, label, class) tuples — every local pattern is class A in
    code, because it is one of the patterns this instrument has always blocked
    on.  config_present is False when the file does not exist at all; True
    even when the file exists but is malformed or has no valid patterns (so
    the caller can distinguish "no file" from "file exists but produced
    nothing").
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
            patterns.append(
                (re.compile(entry["regex"]), entry["label"], CLASS_A)
            )
        except (KeyError, TypeError, re.error, OverflowError, RecursionError) as exc:
            # The roster is a personal-name list: the entry, its label and
            # str(re.error) can all carry a name.  Report only the 1-based
            # position, the exception class, and (for KeyError) the missing
            # schema key, which is ours and not user data.
            detail = f" (missing key {exc.args[0]!r})" if isinstance(exc, KeyError) else ""
            print(f"scan-leaks: warning — skipping bad pattern entry {i}: "
                  f"{type(exc).__name__}{detail}", file=sys.stderr)
    return patterns, True


def _refuse_roster(lineno: int, detail: str) -> None:
    """Refuse the WHOLE roster, naming the 1-based line number and nothing else.

    The roster is a name list and CI stderr can reach a public log, so no
    message may carry the line, the pattern, or any substring of either — not
    even through ``re.error.__str__``, which embeds part of the pattern.  A
    roster that does not parse is exit 2 (invocation error), never 1 (which
    every consumer reads as "leaks found") and never 3 ("did not look").
    """
    print(f"scan-leaks: roster line {lineno} {detail}", file=sys.stderr)
    sys.exit(2)


def load_roster_file(path: Path) -> list[tuple[re.Pattern, str, str]]:
    """Load a FORMAT-2 roster: ``A<TAB><regex>`` or ``B<TAB><regex>`` per line.

    This is the ONLY parser of the format-2 grammar in the repository.  Each
    line is stripped, then split on its FIRST tab; the class token is the whole
    prefix before that tab and must be exactly ``A`` or ``B`` (case-sensitive).
    Blank lines and lines whose first non-space character is ``#`` are ignored.
    There is NO default class.

    A line with no tab, an unknown class token, a tab after the first, a regex
    that does not compile, or a class-A pattern that declares no boundary is a
    HARD parse error naming the 1-based line number.  ANY bad line refuses the
    WHOLE roster (exit 2): a scan against a subset of the roster is a scan of
    the wrong thing, which is the old skip-and-warn path this replaces.

    Returns (compiled_regex, label, class) triples.  A format-1 roster — plain
    regex lines with no tab — is refused as "no class token"; that is intended,
    and the message tells the operator the line needs a class prefix.
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
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" not in stripped:
            _refuse_roster(
                lineno,
                "has no class token; every line needs an A<TAB> or B<TAB> "
                "prefix",
            )
        class_token, pattern_text = stripped.split("\t", 1)
        if class_token not in ROSTER_CLASSES:
            _refuse_roster(
                lineno, "has an invalid class token; expected A or B"
            )
        if "\t" in pattern_text:
            _refuse_roster(
                lineno,
                "contains a tab after the class token; a pattern cannot "
                "contain a tab",
            )
        try:
            compiled = re.compile(pattern_text)
        except (re.error, OverflowError, RecursionError) as exc:
            detail = (f"is not a valid regular expression: "
                      f"{type(exc).__name__}")
            if isinstance(exc, re.error):
                detail += f" at offset {exc.pos}"
            _refuse_roster(lineno, detail)
        if class_token == CLASS_A and not any(
            marker in pattern_text for marker in _A_BOUNDARY_MARKERS
        ):
            _refuse_roster(
                lineno,
                "is a class-A pattern with no declared boundary; add \\b, a "
                "lookaround, or ^/$",
            )
        patterns.append((compiled, "roster file", class_token))
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


def _attribute_line(line: str, leak_patterns: list
                    ) -> tuple[str, str] | None:
    """Attribute one line to a class, returning (label, class) or None.

    CLASS A WINS ON A SHARED LINE. Every pattern is evaluated against the
    line; if ANY class-A pattern matches, the line is class A and the first
    class-A match supplies the label. Only when no class-A pattern matches
    does the first matching pattern determine the reported label. One finding
    per line is preserved.

    Stopping at the first match is the fail-open this forbids: a class-B
    pattern listed before a class-A pattern would mask the A hit entirely, and
    a blocking gate a report-only pattern can mask is not a gate (measured
    2026-11-15: a line holding both names reported `0 class-A and 2 class-B`
    and exited 0).
    """
    first_label = None
    first_class = None
    first_a_label = None
    for pattern, label, klass in leak_patterns:
        if not pattern.search(line):
            continue
        if first_label is None:
            first_label, first_class = label, klass
        if klass == CLASS_A and first_a_label is None:
            first_a_label = label
    if first_a_label is not None:
        return (first_a_label, CLASS_A)
    if first_label is None:
        return None
    return (first_label, first_class)


def scan(root: Path, leak_patterns: list, use_git: bool = True) -> ScanReport:
    """Scan root for leak_patterns and return a ScanReport.

    ``leak_patterns`` is a list of (compiled, label, class) triples; each
    ``report.findings`` entry is (relpath, lineno, label, snippet, class).
    A line that matches more than one pattern is ONE finding, attributed by
    ``_attribute_line``: class A wins whenever any class-A pattern matches,
    and only otherwise does the first match determine the label.

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

    A candidate whose first 8 KiB contains a NUL byte is binary: skipped,
    counted in ``skipped_binary_paths``, and named by the caller.  Everything
    else is decoded as UTF-8 with errors replaced, so a non-UTF-8 text file is
    still scanned and ``scanned`` counts it.  A symlink is scanned by its
    ``os.readlink`` text and never followed, so it contributes to ``scanned``
    once, no matter what its target is.  A path that cannot be inspected
    or read lands in ``unreadable_paths``; a path that is not there lands in
    ``missing_paths`` — never dropped silently, and never counted as binary.
    A candidate that is not a regular file or a symlink lands in
    ``not_a_file_paths``.
    """
    tracked = _git_tracked_files(root) if use_git else None

    # Enumeration happens BEFORE any file-type filter: the candidate set is
    # what the scan considered, so a path dropped later must still be counted.
    walk_unreadable: list[str] = []
    candidates: list[Path] = []
    if tracked is not None:
        # Git-tracked enumeration: skip SKIP_DIRS and SKIP_FILES by name.
        for fpath in tracked:
            parts = fpath.relative_to(root).parts
            if any(p in SKIP_DIRS for p in parts):
                continue
            if fpath.name in SKIP_FILES:
                continue
            candidates.append(fpath)
    else:
        # Fallback: os.walk (same all-text policy).  onerror records a
        # directory that cannot be listed: without it the whole subtree is
        # pruned silently and the counts reconcile against a set that already
        # lost it.
        def _onerror(exc):
            walk_unreadable.append(_relative_dir(root, exc.filename))

        for dirpath, dirs, files in os.walk(root, onerror=_onerror):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            # os.walk lists a symlink to a directory in ``dirs`` and, with
            # followlinks=False, never descends it -- so without this it is
            # never a candidate either, and its link text is never scanned.
            # Take it as a candidate BEFORE descent and drop it from the walk:
            # order is SKIP_DIRS filter, symlink partition, descent.
            link_dirs = [d for d in dirs
                         if os.path.islink(os.path.join(dirpath, d))]
            for d in link_dirs:
                candidates.append(Path(dirpath) / d)
            dirs[:] = [d for d in dirs if d not in link_dirs]
            for fname in files:
                if fname in SKIP_FILES:
                    continue
                candidates.append(Path(dirpath) / fname)

    findings = []
    scanned = 0
    skipped_binary_paths = []
    unreadable_paths = []
    missing_paths = []
    not_a_file_paths = []
    for fpath in candidates:
        rel = fpath.relative_to(root).as_posix()
        kind, _ = _classify(fpath)
        if kind == "missing":
            missing_paths.append(rel)
            continue
        if kind == "unreadable":
            unreadable_paths.append(rel)
            continue
        if kind == "not-a-file":
            not_a_file_paths.append(rel)
            continue
        if kind == "symlink":
            # Git publishes a symlink's LINK TEXT, not its target's bytes, and
            # the link text is what a reader of the published tree sees.  Scan
            # it; NEVER follow the link (following reads the target twice, once
            # under each path, and misses a home path in the link text).
            try:
                content = os.readlink(fpath)
            except FileNotFoundError:
                # Raced with the lstat above: the link itself vanished.
                missing_paths.append(rel)
                continue
            except OSError:
                unreadable_paths.append(rel)
                continue
            scanned += 1
            for i, line in enumerate(content.splitlines(), 1):
                hit = _attribute_line(line, leak_patterns)
                if hit is not None:
                    findings.append(
                        (str(rel), i, hit[0], line.strip()[:120], hit[1])
                    )
            continue
        try:
            data = fpath.read_bytes()
        except FileNotFoundError:
            # Raced with the stat above: an absence the scan did not cover,
            # never a crash and never a silent skip.
            missing_paths.append(rel)
            continue
        except OSError:
            unreadable_paths.append(rel)
            continue
        if b"\x00" in data[:8192]:
            skipped_binary_paths.append(rel)
            continue
        content = data.decode("utf-8", errors="replace")
        scanned += 1
        for i, line in enumerate(content.splitlines(), 1):
            hit = _attribute_line(line, leak_patterns)
            if hit is not None:
                findings.append(
                    (str(rel), i, hit[0], line.strip()[:120], hit[1])
                )

    # A walk-time unreadable directory is one enumerated candidate covering a
    # subtree whose file count is unknown; it is an unreadable bucket entry,
    # so it must also be counted on the candidates side of the invariant.
    unreadable_paths.extend(walk_unreadable)
    skipped_binary_paths.sort()
    unreadable_paths.sort()
    missing_paths.sort()
    not_a_file_paths.sort()
    report = ScanReport(
        findings=findings,
        scanned=scanned,
        skipped_binary_paths=skipped_binary_paths,
        unreadable_paths=unreadable_paths,
        candidates=len(candidates) + len(walk_unreadable),
        missing_paths=missing_paths,
        not_a_file_paths=not_a_file_paths,
    )
    # The invariant is the contract: a count that does not reconcile is a
    # scanner bug, never something to print and move past.
    accounted = (report.scanned + len(report.skipped_binary_paths)
                 + len(report.unreadable_paths) + len(report.missing_paths)
                 + len(report.not_a_file_paths))
    if accounted != report.candidates:
        raise AssertionError(
            f"scan-leaks coverage does not reconcile: scanned {report.scanned} + "
            f"skipped-binary {len(report.skipped_binary_paths)} + unreadable "
            f"{len(report.unreadable_paths)} + missing {len(report.missing_paths)} "
            f"+ not-a-file {len(report.not_a_file_paths)} = {accounted}, "
            f"candidates {report.candidates}"
        )
    return report


def scan_home_paths(root: Path, use_git: bool = True
                    ) -> tuple[list[tuple[str, int, str]], list[str]]:
    """Return (findings, unreadable_paths) for the absolute-home-path check.

    findings is (relpath, lineno, segment) for every disallowed absolute home
    path.  unreadable_paths is the sorted repo-relative posix paths whose read
    raised OSError, so the caller can say a pass did not cover them.

    Domain: every git-tracked file (``git ls-files``), whatever its extension,
    including extensionless executables, classified by the same ``_classify``
    seam ``scan()`` uses — a path that is missing or is neither a regular file
    nor a symlink is skipped here (``scan()`` reports those buckets), while a
    path this pass cannot inspect is unreadable.  A symlink is scanned by its
    ``os.readlink`` text, never followed.  A file whose first 8 KiB contains a
    NUL byte is binary and skipped, as is any file in HOME_PATH_EXEMPT_FILES.
    Text is decoded as UTF-8 with errors replaced, so a non-UTF-8 file is
    still scanned.  The verdict (allowed or not) is made inside the shared
    regex.  Falls back to an os.walk of root when git is unavailable (scanning
    too much beats scanning nothing); an unsearchable directory is recorded
    unreadable rather than pruned silently.
    """
    tracked = _git_tracked_files(root) if use_git else None
    findings = []
    unreadable_paths = []
    disallowed_home_path_re = _home_path_class().disallowed_home_path_regex()
    if tracked is None:
        tracked = []

        def _onerror(exc):
            unreadable_paths.append(_relative_dir(root, exc.filename))

        for dirpath, dirs, files in os.walk(root, onerror=_onerror):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            # Same symlink partition as scan(): a directory symlink is listed
            # in ``dirs`` and never descended, so take it as a candidate
            # before descent or its link text is never scanned.
            link_dirs = [d for d in dirs
                         if os.path.islink(os.path.join(dirpath, d))]
            tracked.extend(Path(dirpath) / d for d in link_dirs)
            dirs[:] = [d for d in dirs if d not in link_dirs]
            tracked.extend(Path(dirpath) / f for f in files)

    for fpath in tracked:
        rel = fpath.relative_to(root).as_posix()
        if rel in HOME_PATH_EXEMPT_FILES:
            continue
        kind, _ = _classify(fpath)
        if kind == "missing":
            # The accounting pass (scan()) reports absences; this pass has no
            # bucket for them and must not mislabel one as unreadable.
            continue
        if kind == "not-a-file":
            # A FIFO, socket, device or gitlink carries no text: not scanned,
            # and not a coverage hole (scan() counts it in not_a_file_paths).
            continue
        if kind == "unreadable":
            unreadable_paths.append(rel)
            continue
        if kind == "symlink":
            # A symlink's published content is its link text; scan that text
            # (the same rule as scan()) and never follow the link.
            try:
                text = os.readlink(fpath)
            except FileNotFoundError:
                # Raced with the lstat above: the link itself vanished.
                continue
            except OSError:
                unreadable_paths.append(rel)
                continue
            for i, line in enumerate(text.splitlines(), 1):
                for m in disallowed_home_path_re.finditer(line):
                    findings.append((rel, i, m.group(1).rstrip(".")))
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
            for m in disallowed_home_path_re.finditer(line):
                findings.append((rel, i, m.group(1).rstrip(".")))
    unreadable_paths.sort()
    return findings, unreadable_paths


def main():
    """Run the scanner, turning an unexpected crash into exit 2.

    Exit 2 already means "invocation error"; it now also covers an internal
    error, so a crash cannot reach the exit-1 path every consumer reads as
    "a leak was found".  SystemExit (a usage error, --help, or the exit codes
    below) is not an Exception and passes through untouched.
    """
    try:
        _scan_leaks_cli()
    except Exception as exc:
        print(f"scan-leaks: internal error: {type(exc).__name__}", file=sys.stderr)
        sys.exit(2)


def _scan_leaks_cli():
    parser = argparse.ArgumentParser(description="Scan alpha for personal-info leaks")
    parser.add_argument("--root", default=None,
                        help="Root to scan (default: parent of tools/ dir, i.e., the alpha repo)")
    parser.add_argument("--config-path", default=None,
                        help=f"Explicit path to the local config file "
                             f"(default: <root>/{LOCAL_CONFIG_NAME})")
    parser.add_argument("--roster-file", default=None,
                        help="Format-2 roster, the same format as the "
                             "NAME_LEAK_ROSTER secret: one 'A<TAB>regex' "
                             "(blocking) or 'B<TAB>regex' (report-only) per "
                             "line (blank lines and #-comment lines ignored); "
                             "scanned alongside the committed patterns and the "
                             "local roster")
    # A public CI log must not carry a name: `path:line [label] snippet`
    # prints it twice, so --redact keeps only the repo-relative path and line.
    # Class A never prints a location at all, under any flag.
    parser.add_argument("--redact", action="store_true",
                        help="Print each class-B finding as "
                             "repo-relative-path:line only (no label, no "
                             "snippet); a no-op for class A, already "
                             "count-only")
    args = parser.parse_args()

    # Loaded here, inside main()'s try, before anything is scanned or printed:
    # a missing or unreadable sibling module is an internal error (exit 2),
    # never an import-time traceback on exit 1 with a partial stdout.
    _home_path_class()

    if args.root:
        root = Path(args.root).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parent.parent

    if not root.exists():
        print(f"scan-leaks: root not found: {_escape_path(str(root))}", file=sys.stderr)
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
    # an anchored regex, and docs/CHANGELOG.md quotes it.  New information goes
    # on the lines below, never into this one.
    print(f"scan-leaks: scanned {report.scanned}, skipped-binary {report.skipped_binary}")
    if args.roster_file:
        # The per-class counts of the PARSED roster, below the frozen line.
        # name-leak-sweep.yml compares them against the attestation, so a
        # scanner that never parsed format 2 cannot be mistaken for one that
        # did.  Counts only — never a pattern, never a per-class hash (that
        # would be a membership oracle).
        roster_a = sum(1 for _c, _l, k in roster_file_patterns if k == CLASS_A)
        roster_b = len(roster_file_patterns) - roster_a
        print(f"scan-leaks: roster patterns A={roster_a}, B={roster_b}")
    if report.skipped_binary_paths:
        print("scan-leaks: skipped-binary files: "
              + format_paths(report.skipped_binary_paths))
    home_findings, home_unreadable = scan_home_paths(root)
    # The unreadable UNION is deduped by path: two passes over one file are one
    # file.  missing and not-a-file come from the accounting pass only, but are
    # deduped and sorted the same way so each class prints one canonical list.
    unreadable = sorted(set(report.unreadable_paths) | set(home_unreadable))
    missing = sorted(set(report.missing_paths))
    not_a_file = sorted(set(report.not_a_file_paths))
    if unreadable:
        print(f"scan-leaks: unreadable {len(unreadable)} (coverage incomplete): "
              + format_paths(unreadable))
    if missing:
        print(f"scan-leaks: missing {len(missing)} (coverage incomplete): "
              + format_paths(missing))
    if not_a_file:
        print(f"scan-leaks: not-a-file {len(not_a_file)} (not scanned): "
              + format_paths(not_a_file))
    coverage_incomplete = (bool(unreadable) or bool(missing)
                           or report.candidates == 0)
    if report.candidates == 0:
        print(f"scan-leaks: no coverage: 0 candidate files under "
              f"{_escape_path(str(root))}; nothing was scanned")
    if not leaks and not home_findings:
        if coverage_incomplete:
            # A clean result that ranged over nothing (or over files it could
            # not read) is not evidence of a clean tree.  Distinct from 1 so a
            # caller can tell "dirty" from "did not look".
            sys.exit(3)
        print(f"scan-leaks: clean ({_escape_path(str(root))})")
        sys.exit(0)

    # Split the hits by class.  Home-path findings are class A in code.
    class_a_patterns = [f for f in leaks if f[4] == CLASS_A]
    class_b_patterns = [f for f in leaks if f[4] == CLASS_B]
    class_a = len(class_a_patterns) + len(home_findings)
    class_b = len(class_b_patterns)
    print(f"scan-leaks: {class_a} class-A (blocking) and {class_b} "
          f"class-B (report-only) hit(s) in {_escape_path(str(root))}",
          file=sys.stderr)
    # A class-A hit is a COUNT ONLY: no path, no line, no label, no snippet,
    # under every flag.  A location in a public log is a lasting pointer to a
    # name, and the log outlives the fix that removes it.
    for rel, lineno, label, snippet, _klass in class_b_patterns:
        # Class B is permitted content by construction, so it keeps its tag.
        shown_rel = _escape_path(rel)
        if args.redact:
            print(f"{shown_rel}:{lineno}", file=sys.stderr)
        else:
            print(f"  {shown_rel}:{lineno} [{label}] {snippet}", file=sys.stderr)
    # The roster instruction is scoped to CLASS-A ROSTER/PATTERN hits: a roster
    # entry names a string already published, so the fix is in the tree, never
    # in the roster.  Class B is permitted content and gets no "fix it" advice;
    # an absolute-home-path hit has nothing to do with the roster, so telling
    # its operator to leave the roster alone is misdirection.
    if class_a_patterns:
        print(
            "scan-leaks: a class-A pattern hit means the string is already in "
            "the published tree; fix it in the release PR; do not widen or "
            "edit the roster to silence it.",
            file=sys.stderr,
        )
    if home_findings:
        print(
            "scan-leaks: an absolute home path hit means a host path is in the "
            "published tree; remove it in the release PR. The roster does not "
            "apply to this finding.",
            file=sys.stderr,
        )
    if class_a:
        sys.exit(1)
    if coverage_incomplete:
        # A class-B-only run over a tree the scan could not fully read is still
        # "did not look" for the blocking class: never a green tick.
        sys.exit(3)
    # Class B never blocks: it is counted, printed, and reaches the record —
    # the exit is 0 when no class-A hit fired (§2.6 of the format-2 spec).
    sys.exit(0)


if __name__ == "__main__":
    main()
