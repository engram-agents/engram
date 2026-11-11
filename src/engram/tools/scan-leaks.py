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
  - Any path of the form /home/<name>/ or /Users/<name>/ whose <name> is not
    in ALLOWED_HOME_SEGMENTS is a finding.  Public text carries no host paths,
    usernames or seat folder names.  The check is generated from the class,
    not from a list of known names: whole path segments only, so an allowed
    name never allows a longer name that merely starts with it.  It reads
    every tracked file whatever its extension (extensionless executables
    included) and skips only binary files and the declared exemptions.

Personal-name and user-specific patterns are loaded at runtime from
.scan-leaks-local.json in the repo root (gitignored). If this file is
absent the scanner runs committed structural patterns only and prints one
advisory line.

See docs/internal/.scan-leaks-local.json.example for the config shape.

Allowlist:
  - LICENSE (copyright lines are intentional)
  - .git/, __pycache__/, *.pyc

Exit codes:
  0 — clean
  1 — leaks found
  2 — invocation error
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Structural patterns with NO personal data — safe to commit.
COMMITTED_PATTERNS = [
    (re.compile(r"kg_protocol_for_ai"), "main repo path"),
]

LOCAL_CONFIG_NAME = ".scan-leaks-local.json"

SCAN_EXTENSIONS = {".py", ".md", ".sh", ".json", ".template", ".service", ".timer", ".yml", ".yaml"}

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

# Files exempt from the home-path check, by repo-relative path, each with a reason.
HOME_PATH_EXEMPT_FILES = {
    "src/engram/tools/scan-leaks.py":
        "defines the pattern and the allowed set; contains them by design",
    "tests/test_scan_leaks.py":
        "plants home-path strings to prove the check fires; contains them by design",
    "tests/test_engine_phase3.py":
        "plants home-path leaks to prove the foreign-bundle scan fires; contains them by design",
}
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
    for entry in raw.get("patterns", []):
        try:
            patterns.append((re.compile(entry["regex"]), entry["label"]))
        except (KeyError, re.error) as exc:
            print(f"scan-leaks: warning — skipping bad pattern entry {entry!r}: {exc}",
                  file=sys.stderr)
    return patterns, True


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


def scan(root: Path, leak_patterns: list, use_git: bool = True) -> list[tuple[str, int, str, str]]:
    """Scan root for leak_patterns and return a list of (relpath, lineno, label, snippet).

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
    """
    leaks = []

    tracked = _git_tracked_files(root) if use_git else None

    if tracked is not None:
        # Git-tracked enumeration: skip SKIP_DIRS and SKIP_FILES by name,
        # and apply the same extension filter as the os.walk path.
        candidates = []
        for fpath in tracked:
            # Check every component for SKIP_DIRS membership
            parts = fpath.relative_to(root).parts
            if any(p in SKIP_DIRS for p in parts):
                continue
            if fpath.name in SKIP_FILES:
                continue
            if fpath.suffix not in SCAN_EXTENSIONS:
                continue
            candidates.append(fpath)
    else:
        # Fallback: os.walk (original behavior)
        candidates = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if fname in SKIP_FILES:
                    continue
                if Path(fname).suffix not in SCAN_EXTENSIONS:
                    continue
                candidates.append(Path(dirpath) / fname)

    for fpath in candidates:
        try:
            content = fpath.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = fpath.relative_to(root)
        for i, line in enumerate(content.splitlines(), 1):
            for pattern, label in leak_patterns:
                if pattern.search(line):
                    leaks.append((str(rel), i, label, line.strip()[:120]))
                    break
    return leaks


def scan_home_paths(root: Path, use_git: bool = True) -> list[tuple[str, int, str]]:
    """Return (relpath, lineno, segment) for every disallowed absolute home path.

    Domain: every git-tracked file (``git ls-files``), whatever its extension,
    including extensionless executables.  A file whose first 8 KiB contains a
    NUL byte is binary and skipped; so is a file that does not decode as UTF-8
    and any file in HOME_PATH_EXEMPT_FILES.  Falls back to an os.walk of root
    when git is unavailable (scanning too much beats scanning nothing).
    """
    tracked = _git_tracked_files(root) if use_git else None
    if tracked is None:
        tracked = []
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            tracked.extend(Path(dirpath) / f for f in files)

    findings = []
    for fpath in tracked:
        rel = fpath.relative_to(root).as_posix()
        if rel in HOME_PATH_EXEMPT_FILES:
            continue
        try:
            data = fpath.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for m in HOME_PATH_RE.finditer(line):
                if m.group(1) not in ALLOWED_HOME_SEGMENTS:
                    findings.append((rel, i, m.group(1)))
    return findings


def main():
    parser = argparse.ArgumentParser(description="Scan alpha for personal-info leaks")
    parser.add_argument("--root", default=None,
                        help="Root to scan (default: parent of tools/ dir, i.e., the alpha repo)")
    parser.add_argument("--config-path", default=None,
                        help=f"Explicit path to the local config file "
                             f"(default: <root>/{LOCAL_CONFIG_NAME})")
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

    if not local_patterns:
        # Advisory fires whenever the personal-name net didn't run — covers
        # both "file absent" and "file present but malformed/empty".
        reason = (f"{LOCAL_CONFIG_NAME} absent"
                  if not config_present else f"{LOCAL_CONFIG_NAME} produced no patterns")
        print(
            f"scan-leaks: no local roster ({reason})"
            " — personal-name scan skipped; structural patterns only"
        )

    all_patterns = COMMITTED_PATTERNS + local_patterns

    leaks = scan(root, all_patterns)
    home_findings = scan_home_paths(root)
    if not leaks and not home_findings:
        print(f"scan-leaks: clean ({root})")
        sys.exit(0)

    print(f"scan-leaks: {len(leaks) + len(home_findings)} potential leak(s) in {root}",
          file=sys.stderr)
    for rel, lineno, label, snippet in leaks:
        print(f"  {rel}:{lineno} [{label}] {snippet}", file=sys.stderr)
    for rel, lineno, seg in home_findings:
        print(f"  {rel}:{lineno} [absolute home path] segment: {seg}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
