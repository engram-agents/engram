#!/usr/bin/env python3
"""tools/check_stubbed_signatures.py — CI gate: find tests that STUB a
changed function/method BY BARE NAME, invisible to run_touched_tests.py's
import-graph + convention-map selection (#3118).

Origin
======
PR #3110 changed `_fetch_threads` / `_fetch_inbox`'s signatures in
src/engram/hooks/claude/engram-forum-prompt-hook.py and moved a POST call off
`urlopen` onto `http.client`. Two test files
(tests/test_forum_hook_injection_bounds.py, tests/test_forum_hook_publish.py)
stub those exact names by BARE NAME — `monkeypatch.setattr(hook,
"_fetch_threads", ...)`, `hook.urllib.request.urlopen` — and were never run
by the author's hand-picked "affected tests" list, because a path/import
heuristic doesn't see a test that binds a function by bare name rather than
by import. Nobody caught it until the full suite ran on the release PR.

This is a DIFFERENT gap from tools/run_touched_tests.py's import-graph +
convention-map selection — this tool is additive, not a replacement, and does
not touch that tool or its workflow.

Two detection lanes
====================
1. NARROW — a `def`/`async def` line for the same bare name appears on both
   an added and a removed line WITHIN THE SAME DIFF HUNK (a signature
   change, e.g. #3110's "add a trailing param" shape: the old and new `def`
   line for one edit sit a line or two apart, inside one hunk). Resolved by
   grepping tests/ for that exact bare name (word-bounded, exact) — no
   restriction to monkeypatch-shaped lines; deliberately unrestricted, since
   narrowing to "looks like a monkeypatch call" is exactly the kind of
   judgment call this mechanical gate exists to remove.

   Both the both-sides requirement and the same-hunk requirement were
   measured necessary, not merely tidy — see collect_narrow_names' own
   docstring for the two noise explosions (370 files, then 218 files) that
   an added-OR-removed / whole-diff reading produces on the real #3110
   fixture.

2. WIDE — a small fixed watchlist of stdlib-transport symbols (`urlopen`,
   `requests.get`, `requests.post`, `http.client`, `httplib`) appearing as a
   substring on a REMOVED diff line (e.g. `_ipv4_only_urlopen(req, ...)`
   disappearing when a hook moves off urlopen). Resolved the same way, but a
   raw bare-name grep for a stdlib symbol is often far too noisy to act on —
   measured on #3110 itself, "urlopen" alone matches 20-21 files across
   tests/, none of which are actually affected by the hook's transport
   change. When the raw match count exceeds WIDE_RESOLVE_THRESHOLD, this
   tool DISCARDS that name's raw result and falls back to a MODULE-SCOPED
   pattern instead: for each source file where the wide symbol was removed,
   join that file's basename tokens (stripped of .py, split on -/_) with a
   `[-_]` separator class, so a hyphenated source basename
   ("engram-forum-prompt-hook.py") matches an underscored or hyphenated
   reference to it inside test content — the pattern this repo's own
   import-by-path test harnesses actually use (e.g.
   `HOOK_PATH = ... / "engram-forum-prompt-hook.py"` /
   `importlib.util.spec_from_file_location(..., HOOK_PATH)`). Measured
   against the real #3110 fixture: this recovers
   tests/test_forum_hook_publish.py (which references the hook module by its
   full basename but never calls `_fetch_threads`/`_fetch_inbox` directly)
   at 7 total files, well under the raw 20-21-file dump.

   LIMIT (by design, not oversight): this fallback pattern is built by
   joining ALL of a basename's tokens together in original order with a
   flexible separator — not a token-level alternation ("engram|forum|
   prompt|hook", measured to hit 300+ files — catastrophically broad, since
   "engram" alone appears almost everywhere) and not an adjacent-bigram
   alternation ("forum-prompt|prompt-hook", measured to hit 17 files on the
   real fixture — noisy, since English prose in unrelated docstrings
   coincidentally uses adjacent watchlist-adjacent words). A test that
   references the source module by some OTHER partial name (e.g. only
   "forum_hook" while the source basename is "engram-forum-prompt-hook",
   skipping "prompt" and "engram") is invisible to this fallback unless it
   also contains the full basename somewhere (as both of #3110's real test
   files happen to, via a HOOK_PATH-style literal). Not yet observed to be a
   problem in practice; noted here so a future miss is recognized against
   this line rather than rediscovered as a surprise.

Output
======
Union of all resolved files (narrow + wide/fallback), sorted, one path per
line, to stdout. Exit 0 whenever the diff/grep mechanics themselves
succeeded — an empty result is a valid, correct answer (zero hits is fine),
never an error.

Refusal paths (loud, non-zero exit, message on stderr, never a silently
empty list):
  - an unresolvable --base or --head ref
  - no tests/ directory found under the current working directory

NOT a refusal path:
  - --base == --head (no diff at all): empty result, exit 0.

CLI
===
  python tools/check_stubbed_signatures.py --base <base-ref> [--head <head-ref>]

`--head` defaults to `HEAD`. Diff is computed with merge-base semantics
(`git diff <base>...<head>`), matching tools/run_touched_tests.py's own
`{base}...HEAD` convention, restricted to `*.py` files.

Scope bound: does not touch tools/run_touched_tests.py or
.github/workflows/affected-tests.yml — a different, complementary mechanism.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Same scan roots as tools/test_id_set_check.py's TEST_SCAN_ROOTS (duplicated
# by hand, not imported — this tool is deliberately standalone, see module
# docstring's "additive, not a replacement" framing). Checked directly
# against this repo: both directories exist (`tests/`, `src/forum/tests/`).
TEST_ROOTS: tuple[str, ...] = ("tests/", "src/forum/tests/")

# Fixed watchlist of stdlib/requests transport symbols (#3118 issue text) --
# NOT extensible via CLI; a new transport surface is a code change here, not
# a config change.
WIDE_WATCHLIST: tuple[str, ...] = (
    "urlopen",
    "requests.get",
    "requests.post",
    "http.client",
    "httplib",
)

# Above this many raw bare-name hits, a wide-name's raw grep result is
# discarded as too noisy to act on (see module docstring) and the
# module-scoped fallback is used instead. Narrow names have no such
# threshold -- they are exact function/method names, not common English
# words or stdlib module names, so a large raw hit count is itself
# meaningful signal rather than noise.
WIDE_RESOLVE_THRESHOLD = 10

# Matches a `def`/`async def <name>(` line on either side of a unified diff
# ('+' added or '-' removed; explicitly NOT '+++'/'---' file headers).
_DEF_LINE_RE = re.compile(
    r"^[+-](?!\+\+|--)\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
)

# The destination path out of a unified diff's "diff --git a/X b/X" header.
# Assumes no spaces in tracked paths (true throughout this repo); a path
# containing " b/" would break this, same simplifying assumption
# tools/test_id_set_check.py's git-backed reader makes elsewhere in this repo.
_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/.* b/(.+)$")


class GitCommandError(Exception):
    """A git subprocess invocation failed (bad ref, git error, etc.)."""


# ---------------------------------------------------------------------------
# Git-backed helpers
# ---------------------------------------------------------------------------


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )


def _run_git_or_raise(repo_root: Path, args: list[str]) -> str:
    result = _run_git(repo_root, args)
    if result.returncode != 0:
        raise GitCommandError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _resolve_ref(repo_root: Path, ref: str) -> None:
    """Raise GitCommandError if `ref` does not resolve to a commit."""
    result = _run_git(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if result.returncode != 0:
        raise GitCommandError(
            f"'{ref}' does not resolve to a commit ({result.stderr.strip()})"
        )


def get_diff(repo_root: Path, base: str, head: str) -> str:
    """Unified diff of base...head (merge-base semantics, matching
    run_touched_tests.py's own `{base}...HEAD` convention), restricted to
    *.py files."""
    return _run_git_or_raise(repo_root, ["diff", f"{base}...{head}", "--", "*.py"])


def _git_grep_files(
    repo_root: Path, pattern: str, rev: str, *, fixed: bool, word: bool
) -> list[str]:
    """`git grep -l [-w] [-F|-E] -e <pattern> <rev> -- TEST_ROOTS`, filtered to
    `.py` files only.

    Returns [] on no match (git grep's own exit code 1 -- not an error).
    Raises GitCommandError on a real git error (exit code > 1, e.g. a
    genuinely malformed pattern or an unreadable rev).

    The TEST_ROOTS pathspec scopes the grep to the test directories but does
    NOT restrict it to *.py files within them -- a non-Python file (a
    README, a fixture data file, anything) that happens to contain the
    matched pattern would otherwise be included in the returned file list
    and fed straight into `pytest` by the CI step that consumes this
    output, which exits 4 ("no tests ran") on a non-Python path. A test
    file is a `.py` file, full stop -- this filter is a plain post-filter on
    the already-collected list, not a change to the git pathspec/TEST_ROOTS
    mechanism itself (#3118 round-3, Kepler's repro)."""
    args = ["grep", "-l"]
    if word:
        args.append("-w")
    args.append("-F" if fixed else "-E")
    args += ["-e", pattern, rev, "--", *TEST_ROOTS]
    result = _run_git(repo_root, args)
    if result.returncode == 1:
        return []
    if result.returncode not in (0, 1):
        raise GitCommandError(
            f"git grep failed for pattern {pattern!r}: {result.stderr.strip()}"
        )
    prefix = f"{rev}:"
    files: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(prefix):
            line = line[len(prefix):]
        files.append(line)
    return [f for f in files if f.endswith(".py")]


# ---------------------------------------------------------------------------
# Diff parsing (pure — no git I/O; takes diff TEXT, matching this repo's
# testability-seam convention, e.g. run_touched_tests.py's
# _parse_workflow_scan_roots)
# ---------------------------------------------------------------------------


def collect_narrow_names(diff_text: str) -> set[str]:
    """Every bare function/method name whose `def`/`async def` line appears
    on BOTH an added AND a removed line, WITHIN THE SAME HUNK, of the diff --
    the #3110 shape: same name, changed parameter list (a removed
    `def _fetch_threads(...)` line paired with an added
    `def _fetch_threads(..., deadline)` line a line or two below it).

    TWO deliberate departures from "on any changed line" (added-only OR
    removed-only, anywhere in the diff), both measured directly against the
    real #3110 fixture -- each was tried and each independently produced a
    noise explosion:

    1. Added-only / removed-only names are excluded. That same diff adds a
       brand-new test file (tests/test_forum_monitor_unreachable_edge.py)
       defining a fake-socket/HTTPConnection test harness with generic
       ADDED-only method names (`close`, `connect`, `start`, `stop`, `read`,
       `request`, ...). Collecting those too (an added-OR-removed reading)
       resolves to 370 of this repo's ~380 total test files -- effectively
       the whole suite. A name that appears on only one side was never both
       a removal (something a stub relied on that stopped existing under
       that name) and a reintroduction (a still-existing name a stub could
       stub *by*) -- no existing stub can be affected by a name that is
       purely new, and a purely-removed name with no replacement is a
       deletion, already covered by test-id-set-check (#2350) if the
       deleted def was itself a test.

    2. Restricted to the SAME diff hunk (bounded by consecutive `@@ ... @@`
       markers), not merely "somewhere in the whole diff". Even after (1),
       requiring only "both an added and a removed line, anywhere" still
       collects coincidental same-name collisions across UNRELATED hunks --
       measured on this same fixture: the new test harness above defines
       `def read(self, n=-1)` in one class (added-only, hunk A) while an
       older removed helper elsewhere in the SAME new file happened to
       define `def read(self)` in a different class (removed-only, hunk B).
       Neither occurrence is a signature change of the other, but a
       whole-diff added/removed intersection pairs them anyway, and "read"
       alone resolves to 218 files. Requiring the pair to share a hunk
       matches the real signature-change shape (both the old and new `def`
       line for the SAME edit sit a line or two apart, inside one hunk) and
       excludes the coincidental cross-hunk collision.

    LIMIT (by design, not oversight): the same-hunk requirement in (2) that
    excludes coincidental cross-hunk collisions also excludes a real
    same-commit signature change when the function is RELOCATED far enough
    within the file that git renders the move as a delete-in-place-A +
    insert-in-place-B pair, rather than a single hunk showing both the old
    and new `def` line together. Confirmed via a constructed repro (a
    function moved ~40 lines within the same file, with its signature
    widened in the same commit): the removed `def` line lands in one hunk
    and the added `def` line lands in a separate hunk, so this function's
    same name never appears on both an added AND a removed line within one
    hunk -- the tool returns empty output, exit 0, silently missing exactly
    the bare-name-stub failure class this tool exists to catch. Not fixed
    here: distinguishing "the same function, moved" from "two unrelated
    functions that happen to share a name" across arbitrary hunk distance is
    a much harder problem (it would need name-tracking across the whole
    diff, plus a heuristic for which same-named add+remove pairs are really
    one edit) that this mechanical, same-hunk-scoped gate is not trying to
    solve. Noted here, previously undocumented, so a future miss against
    this shape is recognized against this paragraph rather than rediscovered
    as a surprise."""
    result: set[str] = set()
    hunk_added: set[str] = set()
    hunk_removed: set[str] = set()

    def _flush() -> None:
        result.update(hunk_added & hunk_removed)

    for line in diff_text.splitlines():
        if line.startswith("@@") or line.startswith("diff --git "):
            _flush()
            hunk_added = set()
            hunk_removed = set()
            continue
        m = _DEF_LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if line.startswith("+"):
            hunk_added.add(name)
        elif line.startswith("-"):
            hunk_removed.add(name)
    _flush()
    return result


def collect_wide_hits(diff_text: str) -> dict[str, set[str]]:
    """Map each WIDE_WATCHLIST symbol found (as a substring) on a REMOVED
    ('-', not '---') diff line to the set of source file paths where it was
    removed. Symbols on ADDED lines are not collected -- moving an import IN
    is not the shape this lane guards against; moving a transport call OUT
    from under a test's existing stub is."""
    hits: dict[str, set[str]] = {}
    current_file: str | None = None
    for line in diff_text.splitlines():
        header = _DIFF_GIT_HEADER_RE.match(line)
        if header:
            current_file = header.group(1)
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-") and current_file is not None:
            for symbol in WIDE_WATCHLIST:
                if symbol in line:
                    hits.setdefault(symbol, set()).add(current_file)
    return hits


def _basename_tokens(path: str) -> list[str]:
    """basename, extension stripped, split on runs of '-'/'_'."""
    stem = Path(path).stem
    return [t for t in re.split(r"[-_]+", stem) if t]


def _module_scoped_pattern(paths: set[str]) -> str | None:
    """Build one regex alternative per removal-site path, each alternative
    joining that path's basename tokens with a `[-_]` separator class (see
    module docstring's WIDE lane + LIMIT paragraph for the rationale and the
    two broader alternatives measured and rejected). Returns None if no path
    yields any tokens at all."""
    alternatives: list[str] = []
    for path in sorted(paths):
        tokens = _basename_tokens(path)
        if not tokens:
            continue
        # Plain "(...)" grouping, NOT "(?:...)" -- git grep's -E is POSIX
        # ERE, which has no non-capturing-group syntax; "(?:" is read as a
        # literal "?" immediately after an opening group, which is an
        # invalid repetition target ("Invalid preceding regular
        # expression"). A capturing group costs nothing here since nothing
        # reads the captured groups -- only -l (matched filenames) is used.
        joined = "[-_]".join(re.escape(t) for t in tokens)
        alternatives.append(f"({joined})")
    if not alternatives:
        return None
    return "|".join(alternatives)


# ---------------------------------------------------------------------------
# Resolution — narrow/wide names -> affected test files
# ---------------------------------------------------------------------------


def resolve_narrow_names(repo_root: Path, names: set[str], head: str) -> set[str]:
    resolved: set[str] = set()
    for name in sorted(names):
        resolved.update(
            _git_grep_files(repo_root, name, head, fixed=True, word=True)
        )
    return resolved


def resolve_wide_names(
    repo_root: Path, wide_hits: dict[str, set[str]], head: str
) -> set[str]:
    resolved: set[str] = set()
    for symbol, removal_paths in sorted(wide_hits.items()):
        raw_files = _git_grep_files(repo_root, symbol, head, fixed=True, word=False)
        if len(raw_files) > WIDE_RESOLVE_THRESHOLD:
            pattern = _module_scoped_pattern(removal_paths)
            if pattern is None:
                continue
            resolved.update(
                _git_grep_files(repo_root, pattern, head, fixed=False, word=False)
            )
        else:
            resolved.update(raw_files)
    return resolved


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find tests that stub a changed function/method BY BARE NAME, "
            "invisible to run_touched_tests.py's import-graph + "
            "convention-map selection (#3118)."
        )
    )
    parser.add_argument("--base", required=True, metavar="REF", help="Base git ref.")
    parser.add_argument(
        "--head", default="HEAD", metavar="REF", help="Head git ref (default: HEAD)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = Path.cwd()

    if not (repo_root / "tests").is_dir():
        print(
            "check_stubbed_signatures: error — no tests/ directory found "
            f"under {repo_root}. Run this tool from the repository root.",
            file=sys.stderr,
        )
        return 2

    try:
        _resolve_ref(repo_root, args.base)
        _resolve_ref(repo_root, args.head)
    except GitCommandError as exc:
        print(f"check_stubbed_signatures: error — {exc}", file=sys.stderr)
        return 2

    try:
        diff_text = get_diff(repo_root, args.base, args.head)
        narrow_names = collect_narrow_names(diff_text)
        wide_hits = collect_wide_hits(diff_text)

        affected: set[str] = set()
        affected |= resolve_narrow_names(repo_root, narrow_names, args.head)
        affected |= resolve_wide_names(repo_root, wide_hits, args.head)
    except GitCommandError as exc:
        print(f"check_stubbed_signatures: error — {exc}", file=sys.stderr)
        return 2

    for path in sorted(affected):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
