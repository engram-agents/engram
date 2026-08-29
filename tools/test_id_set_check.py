#!/usr/bin/env python3
"""test_id_set_check.py — CI gate: fail a PR whose test-id SET shrinks without an
explicit marker (#2350).

A deleted test cannot fail, so a PR that removes coverage is exactly as green as
a PR that adds it. No count-based check can catch this: a rename plus a deletion
nets to zero on a count, and worse, an unrelated coincidence can make a wrong
count look like a right one. Measured on the motivating commit (`2ecf88c`,
`tests/test_cascade_resolve.py`): base 28 ids / head 21 ids / count delta -7,
while the real set diff is 10 disappeared / 3 appeared — the count even matches
the number of *collateral* deletions (7), which is arithmetically unrelated to
the actual damage. Only a set diff of names is sound.

Collection is STATIC AST, never `pytest --collect-only` and never grep:
collection needs the test's dependencies installed and dies on an unrelated
import error (a gate that fails for reasons outside its subject gets disabled);
grep on `def test_` matches inside strings/comments and misses methods on
`Test*` classes. `ast.parse` reads the file without importing it.

Collected, for every `*.py` under `tests/`:
  - module-level `def test_*` / `async def test_*`            -> "<relpath>::<name>"
  - module-level `class Test*`, OR a class whose DIRECT base is `TestCase`
    (from-import) or `unittest.TestCase` (attribute access) regardless of its
    name — pytest collects a `unittest.TestCase` subclass no matter what it is
    named, so a name-only rule is blind to e.g. `class WidgetRenderingTest
    (unittest.TestCase)`. Its DIRECT `def test_*` / `async def test_*`
    methods are collected                                     -> "<relpath>::<ClassName>::<name>"
  - NOT functions nested inside other functions (pytest never collects them).
  - NOT anything inside a string or comment (free, given AST).
  - LIMIT (by design, not oversight): only DIRECT bases are checked, never
    resolved transitively — `class FooTest(SomeLocalBase)` where
    `SomeLocalBase` itself extends `TestCase` is invisible to this rule,
    because resolving that requires import machinery this tool deliberately
    does not use (see module-level rationale above). Measured against the
    current tree (2026-08-29): every class under `tests/` that inherits from a
    local base class is already `Test`-prefixed, and therefore already
    collected by the name rule — so this limit has ZERO live exposure today.
    It is a real limit in principle, not a hand-waved one; a future local base
    class named without a `Test`-prefixed subclass would be invisible until
    resolved by hand.
  - LIMIT (by design): a `@pytest.mark.parametrize`d test whose case count
    shrinks (e.g. 5 params -> 3 params) is invisible to this gate — the `def`
    itself does not disappear, only the cases it is invoked with, and this
    tool collects at the `def` granularity, never expanding parametrize
    decorators. A real, deliberate limit of the no-import static-AST design:
    resolving parametrize arguments generally requires evaluating arbitrary
    expressions, which is exactly the import machinery this tool deliberately
    avoids.
  - LIMIT (by design): the PR-body marker parser's inline-code strip has its
    own unterminated-backtick swallow limit — see `parse_removed_test_markers`'s
    docstring.

The base/head comparison reads git objects directly (`git ls-tree` +
`git show <rev>:<path>`) — it never checks anything out.

Escape hatch — a PR body line:

    Removed-test: <id> — <reason>

accounts for a disappeared test, where <id> is either the full id
(`path::name`) or a bare function/method name (accepted only when exactly one
disappeared id ends with `::<name>` — an ambiguous bare name is NOT accounted,
and the failure message says the full id is needed). The separator is an
em-dash or a hyphen; the reason must be non-empty after stripping. A marker
naming an id that did NOT disappear is a warning, not a failure: deliberate,
because a PR author who correctly restored a test after writing the marker
line should not be blocked by their own now-stale housekeeping.

Usage:
    python3 tools/test_id_set_check.py --base <rev> --head <rev> \
        [--pr-body-file PATH] [--repo-root PATH] [--format text|json]

Exit codes:
    0 — pass (no disappearances, or every disappearance is accounted for;
        stale-marker warnings do not affect the exit code)
    1 — gate failure (>=1 unaccounted disappeared test id)
    2 — usage / IO / git-read / parse error (a file under tests/ failed to
        parse is included here — it is a hard error, never silently skipped,
        because a skipped file makes the id set incomplete, which is exactly
        the silent-corruption class this gate exists to close)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Pure data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Marker:
    """A parsed `Removed-test: <id> — <reason>` line from a PR body."""

    id: str
    reason: str
    raw_line: str


@dataclass(frozen=True)
class Report:
    """The whole verdict, produced by evaluate(). Pure data — no I/O."""

    base_count: int
    head_count: int
    disappeared: tuple[str, ...]
    appeared: tuple[str, ...]
    unaccounted: tuple[str, ...]
    ambiguous: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not self.unaccounted


class ParseError(Exception):
    """A tests/*.py file failed ast.parse(). Always a hard error (exit 2)."""

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


class GitReadError(Exception):
    """A git ls-tree/show invocation failed while reading base or head."""


# ---------------------------------------------------------------------------
# Test-id collection — pure, AST-based
# ---------------------------------------------------------------------------


def _has_direct_testcase_base(node: ast.ClassDef) -> bool:
    """True if `node` DIRECTLY subclasses `TestCase` (from-import) or
    `unittest.TestCase` (attribute access) — the two AST shapes for the same
    base. Transitive bases (a local helper class that itself extends
    TestCase) are NOT resolved; see the module docstring's collection-limit
    note."""
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "TestCase":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "TestCase":
            return True
    return False


def _ids_from_module(tree: ast.Module, relpath: str) -> set[str]:
    ids: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            ids.add(f"{relpath}::{node.name}")
        elif isinstance(node, ast.ClassDef) and (node.name.startswith("Test") or _has_direct_testcase_base(node)):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                    ids.add(f"{relpath}::{node.name}::{item.name}")
    return ids


def collect_ids(sources: Mapping[str, str]) -> set[str]:
    """Collect the test-id set from a {relpath: source_text} mapping.

    Pure — no filesystem, no git. Raises ParseError (never skips) on any file
    that fails ast.parse().
    """
    ids: set[str] = set()
    for relpath in sorted(sources):
        text = sources[relpath]
        try:
            tree = ast.parse(text, filename=relpath)
        except SyntaxError as exc:
            raise ParseError(relpath, str(exc)) from exc
        ids |= _ids_from_module(tree, relpath)
    return ids


# ---------------------------------------------------------------------------
# Marker parsing — pure
# ---------------------------------------------------------------------------

# Line-anchored, leading whitespace allowed, case-insensitive key. Whitespace
# is REQUIRED immediately around the separator (em-dash or hyphen) so that a
# hyphen appearing inside a free-text reason can never be mistaken for the
# separator — the id (\S+) cannot itself contain whitespace, so this bounds
# it cleanly without needing to special-case hyphens inside ids.
_MARKER_RE = re.compile(
    r"^[ \t]*removed-test:[ \t]+(?P<id>\S+)[ \t]+[—-][ \t]*(?P<reason>.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_removed_test_markers(pr_body: str) -> list[Marker]:
    """Parse every `Removed-test: <id> — <reason>` line in a PR body.

    Inline-code spans (`` `...` ``) are stripped BEFORE parsing, the same way
    the sibling anti-dup workflow step strips them before grepping
    (`sed 's/`[^`]*`//g'`) — this repo has a documented incident class (the
    close-keyword trap discipline in CLAUDE.md) for an unbackticked example
    firing a body-text parser. A PR body that *illustrates* the
    `Removed-test:` syntax inside backticks must not be parsed as a real
    marker — an intentional marker must be written as plain text.

    LIMIT (by design, not oversight): the strip is a regex over the whole
    body, not a markdown parser, so it has no notion of "this backtick was
    never closed." An unmatched backtick anywhere earlier in the body pairs
    with the next backtick that appears later in the document — even across
    unrelated paragraphs — and everything between the two, including a
    well-formed `Removed-test:` marker sitting in that gap, is swallowed
    along with it. This fails SAFE: the swallowed marker never reaches the
    marker regex, so the disappearance it would have accounted for reads as
    unaccounted and the gate FAILS LOUDLY, rather than a real deletion
    slipping through silently. A markdown-correct parser (one that actually
    tracks whether a given backtick opens or closes a span) is out of scope
    for the same reason the module docstring's other LIMIT entries are: real
    parsing machinery this tool deliberately avoids. Practical guidance:
    write `Removed-test:` marker lines as isolated lines, away from any
    inline code, so an unrelated backtick typo elsewhere in the body can
    never reach across and swallow one.

    Pure. Returns a Marker even when the reason is empty after stripping —
    evaluate() is the layer that decides an empty-reason marker accounts for
    nothing (see module docstring: "reason must be non-empty after stripping"
    is an accounting rule, not a parse-time rejection).
    """
    if not pr_body:
        return []
    stripped_body = re.sub(r"`[^`]*`", "", pr_body)
    markers = []
    for m in _MARKER_RE.finditer(stripped_body):
        markers.append(Marker(id=m.group("id"), reason=m.group("reason").strip(), raw_line=m.group(0)))
    return markers


# ---------------------------------------------------------------------------
# Verdict — pure
# ---------------------------------------------------------------------------


def evaluate(base_ids: set[str] | frozenset[str], head_ids: set[str] | frozenset[str],
             markers: list[Marker]) -> Report:
    """Compute the whole verdict from two id sets and a marker list. Pure."""
    base_ids = set(base_ids)
    head_ids = set(head_ids)
    disappeared = base_ids - head_ids
    appeared = head_ids - base_ids

    accounted: set[str] = set()
    warnings: list[str] = []
    ambiguous: list[str] = []

    for marker in markers:
        reason = marker.reason.strip()
        if not reason:
            # An empty-reason marker accounts for nothing — it is neither a
            # valid accounting nor a stale-marker warning, it is simply
            # dropped from consideration.
            continue

        if "::" in marker.id:
            if marker.id in disappeared:
                accounted.add(marker.id)
            else:
                warnings.append(
                    f"Removed-test marker names {marker.id!r}, which did not disappear "
                    f"between base and head (stale marker)."
                )
            continue

        # Bare function/method name: accept only when exactly one disappeared
        # id ends with '::<name>'.
        matches = sorted(d for d in disappeared if d.rsplit("::", 1)[-1] == marker.id)
        if len(matches) == 1:
            accounted.add(matches[0])
        elif len(matches) == 0:
            warnings.append(
                f"Removed-test marker names {marker.id!r}, which did not disappear "
                f"between base and head (stale marker)."
            )
        else:
            ambiguous.append(
                f"Removed-test marker names {marker.id!r} ambiguously — {len(matches)} "
                f"disappeared ids end with '::{marker.id}' ({', '.join(matches)}); "
                f"the full id (path::name) is needed to disambiguate."
            )

    unaccounted = sorted(disappeared - accounted)
    return Report(
        base_count=len(base_ids),
        head_count=len(head_ids),
        disappeared=tuple(sorted(disappeared)),
        appeared=tuple(sorted(appeared)),
        unaccounted=tuple(unaccounted),
        ambiguous=tuple(ambiguous),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_report_text(report: Report) -> str:
    lines = [
        f"test-id-set-check: base={report.base_count} ids, head={report.head_count} ids, "
        f"disappeared={len(report.disappeared)}, appeared={len(report.appeared)}"
    ]
    if report.disappeared:
        lines.append("Disappeared test ids:")
        for tid in report.disappeared:
            lines.append(f"  - {tid}")
    for w in report.warnings:
        lines.append(f"::warning::{w}")
    for amb in report.ambiguous:
        lines.append(f"::error::{amb}")
    for tid in report.unaccounted:
        lines.append(
            f"::error::disappeared test id not accounted for: {tid} "
            f"(add 'Removed-test: {tid} — <reason>' to the PR body if this "
            f"deletion is legitimate)"
        )
    if report.passed:
        lines.append("PASS — no unaccounted test-id disappearances.")
    else:
        lines.append(f"FAIL — {len(report.unaccounted)} disappeared test id(s) not accounted for.")
    return "\n".join(lines)


def format_report_json(report: Report) -> str:
    return json.dumps(
        {
            "base_count": report.base_count,
            "head_count": report.head_count,
            "disappeared": list(report.disappeared),
            "appeared": list(report.appeared),
            "unaccounted": list(report.unaccounted),
            "ambiguous": list(report.ambiguous),
            "warnings": list(report.warnings),
            "passed": report.passed,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Git-backed reader — the ONLY layer that touches git
# ---------------------------------------------------------------------------


def _run_git(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitReadError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def list_test_files(repo_root: Path, rev: str) -> list[str]:
    """Enumerate every *.py under tests/ at `rev`, POSIX-relpath, via git ls-tree."""
    out = _run_git(repo_root, ["ls-tree", "-r", "--name-only", rev, "--", "tests/"])
    return [line for line in out.splitlines() if line.endswith(".py")]


def read_sources_at_rev(repo_root: Path, rev: str) -> dict[str, str]:
    """Read every tests/*.py file's source text at `rev`, without checkout."""
    sources: dict[str, str] = {}
    for path in list_test_files(repo_root, rev):
        sources[path] = _run_git(repo_root, ["show", f"{rev}:{path}"])
    return sources


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail a PR whose test-id set shrinks without an explicit Removed-test marker (#2350)."
    )
    parser.add_argument("--base", required=True, metavar="REV", help="Base git revision (e.g. merge-base SHA).")
    parser.add_argument("--head", required=True, metavar="REV", help="Head git revision.")
    parser.add_argument(
        "--pr-body-file",
        metavar="PATH",
        default=None,
        help="Path to a file containing the PR body (read from a file, never argv — "
        "bodies contain newlines/quotes/backticks). Absent means no markers.",
    )
    parser.add_argument("--repo-root", metavar="PATH", default=".", help="Repository root (default: current directory).")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format (default: text).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    if not repo_root.is_dir():
        print(f"test_id_set_check: error — {repo_root} is not a directory", file=sys.stderr)
        return 2

    try:
        base_sources = read_sources_at_rev(repo_root, args.base)
        head_sources = read_sources_at_rev(repo_root, args.head)
    except GitReadError as exc:
        print(f"test_id_set_check: error — {exc}", file=sys.stderr)
        return 2

    try:
        base_ids = collect_ids(base_sources)
        head_ids = collect_ids(head_sources)
    except ParseError as exc:
        print(f"::error::test_id_set_check: could not parse {exc.path}: {exc.message}", file=sys.stderr)
        return 2

    pr_body = ""
    if args.pr_body_file:
        try:
            pr_body = Path(args.pr_body_file).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"test_id_set_check: error — could not read --pr-body-file: {exc}", file=sys.stderr)
            return 2

    markers = parse_removed_test_markers(pr_body)
    report = evaluate(base_ids, head_ids, markers)

    if args.format == "json":
        print(format_report_json(report))
    else:
        print(format_report_text(report))

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
