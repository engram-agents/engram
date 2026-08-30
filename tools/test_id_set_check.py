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
  - LIMIT (by design, not yet oversight): #2421's widened marker anchor
    accepts unordered-list markers (`-`, `*`, `+`) only — an ORDERED list
    (`1.`, `2)`), a GFM task-list checkbox (`- [ ] `), or a BLOCKQUOTE
    (`> `) still hits the same root as #2421 (a shape-list standing in
    for a site-list) and would each be a further variant. Borges's
    colleague pass on PR#2422 drove the parser against a numbered-list
    line and a blockquoted line directly: both currently parse 0 and
    warn 1 (same graceful-degradation the anchor already gives any
    unrecognized prefix — no fabricated accounted-for id, but no
    recognition either). A numbered list is at least as natural a
    choice as a bullet for an itemized removed-test list. Scoped to the
    three forms actually observed in practice (#2393's two, plus
    #2421's bulleted case on the live v0.5.0 release PR); the ordered/
    checkbox/blockquote forms are follow-up-shaped, not yet observed,
    and noted here so the next incident is recognized against this
    line rather than re-discovered as a surprise.

The base/head comparison reads git objects directly (`git ls-tree` +
`git show <rev>:<path>`) — it never checks anything out.

LIMIT (by design, not oversight) — the comparison is ONE-DIRECTIONAL, so a
REVERTED CORRECTION is invisible, and worse, it reads as improvement. This gate
reports ids that LEFT the base set. Undoing a fix moves the head set back TOWARD
base, which is the quiet direction: an id that had been disappearing simply
stops disappearing, and the unaccounted count goes DOWN.

Measured in the wild on #2349:

    pre-rebase  (505b25d -> 1c03cc6), no markers:  disappeared=3
    post-rebase (db955ed -> 7694dd5), 2 markers:   disappeared=2, PASS

A test rename the PR had made was reverted by an over-broad "resolve toward
dev" conflict rule. Two of the three disappearances were legitimately accounted
for by the author's markers; the third was NOT accounted for — it ceased to
exist as a disappearance — and the gate PASSED. Anyone comparing an earlier
published figure of 3 against "disappeared=2, both marked, PASS" concludes the
accounting is complete. `3 -> 2` reads as progress, and one disappearance of
the three has silently stopped being one.

No set difference over ids can separate "this correction was never made" from
"this correction was made and then reverted": both yield the same head set.
This does NOT reopen the opening argument that only a set diff of names is
sound — within a single comparison it still is. The limit is that two separate
invocations share no memory of each other, so the direction of travel between
them is invisible to both.

The gate is not entirely silent here, and the accurate version of that matters
more than the tidy one. On the post-rebase run above it emits, from the PR body
it already reads:

    ::warning::Removed-test marker names
    'tests/test_supported_by.py::test_cascade_relations_is_derives_from_only',
    which did not disappear between base and head (stale marker).

That warning is a fingerprint of this scenario — the marker was written when the
rename was made, and goes stale at exactly the moment the revert makes the id
reappear. So detection did not need the PR's history; it needed someone to read
a line the gate already prints.

BUT THE FINGERPRINT IS CONDITIONAL, and the condition is doing real work: it
exists only because a marker had ALREADY been filed for that disappearance. The
warning is emitted from inside the loop over markers, so an id with no marker
naming it is never examined. A revert that erases a correction before any
marker was ever written for it — corrected and reverted between gate runs, say
— produces NO signal of any kind. Measured on the same pair with an empty PR
body: the reverted id appears zero times in the output, which names only the
two genuine disappearances. That case is exactly as invisible as this LIMIT's
opening paragraph describes.

The warning also cannot be promoted past a warning. The escape-hatch section
below states half the reason — a marker naming an id that did not disappear is
deliberately a warning and not a failure, so that an author who correctly
restored a test is not blocked by their own stale housekeeping. The other half
follows from it rather than being written there: the gate holds no information
distinguishing that benign restoration from a revert that undid a correction,
because both produce exactly the same stale marker over the same id set.
Hardening this to a failure would therefore false-positive on the case the
escape hatch exists to permit. Hence a stated limit rather than a patch.

The practical consequence for a reviewer: a FALLING unaccounted count across a
rebase is not evidence of accounting. Read the stale-marker warnings, and diff
the two runs' id sets, rather than taking the smaller number as progress.

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
    python3 tools/test_id_set_check.py \
        --base "$(git merge-base origin/dev HEAD)" --head HEAD \
        [--pr-body-file PATH] [--repo-root PATH] [--format text|json]

`--base` MUST be the merge-base with the target branch, never the target
branch's current tip — see #2381. Passing the tip fabricates disappearances
for every test that landed on the target branch after this branch was cut
(the branch deletes nothing; it simply predates those tests). CI's own
invocation (`.github/workflows/pr-checks.yml`) computes this correctly; a
manual reproduction that copies the target branch name instead reproduces
the bug, not the gate. If `--base` is not an ancestor of `--head`, `main()`
prints a `::warning::` naming the real merge-base — read it, and re-run with
that value, before trusting a reported disappearance.

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
#
# #2421 — an optional leading markdown list marker (`- `, `* `, `+ `) is
# permitted between the line-start whitespace and the key. Measured live on
# the v0.5.0 release PR: an author wrote 15 markers as a bullet list (the
# obvious way to write fifteen of anything in a PR body), and the pre-#2421
# anchor -- whitespace only, no list marker -- silently dropped all 15. This
# is the THIRD confirmed variant of one root (a shape-list standing in for a
# site-list): #2393's own glued-mid-line and malformed-separator cases were
# the first two. The fix widens the SAME anchor both `parse_removed_test_
# markers` and `find_unparsed_removed_test_lines` share, so a bulleted
# marker now genuinely parses (accounts for the disappearance) rather than
# merely stopping the fail-loud warning from firing on it -- the warning
# path is untouched and still catches variants 1 and 2, which a bulleted
# marker was never the shape of anyway.
_MARKER_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?removed-test:[ \t]+(?P<id>\S+)[ \t]+[—-][ \t]*(?P<reason>.*?)[ \t]*$",
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


def find_unparsed_removed_test_lines(pr_body: str) -> list[str]:
    """#2393 — the fail-loud half `parse_removed_test_markers` cannot provide
    on its own: which lines LOOKED like a marker but did not parse.

    A parse failure here is not silent about the DISAPPEARANCE — that
    already fails loudly via `unaccounted`, correctly. It is silent about
    the CAUSE: the author is told "test X disappeared without a marker"
    while looking straight at a marker they wrote, because the line failed
    the strict `<id> [—-] <reason>` shape. That is a wrong diagnostic, not
    an absent one, and it is what this function surfaces.

    Deliberately does NOT reuse `_MARKER_RE`'s line anchor as the detector —
    reusing it would inherit its exact blind spot (a marker glued mid-line
    onto the preceding line by a text-replace eating a newline never starts
    at column 0, so a line-anchored detector misses precisely the case that
    motivated this function; #2393's second recorded instance). Instead:
    scan each backtick-stripped line for `removed-test:` ANYWHERE
    (case-insensitive), then ask whether THAT LINE, matched from its own
    start, parses as a marker — the same question `_MARKER_RE` answers, just
    without assuming the interesting text starts the line.

    Pure. Returns the raw (stripped) line text for each candidate that
    mentions the key but does not parse — empty list when every mention
    parses or none exists.
    """
    if not pr_body:
        return []
    stripped_body = re.sub(r"`[^`]*`", "", pr_body)
    unparsed = []
    for line in stripped_body.splitlines():
        if "removed-test:" not in line.lower():
            continue
        if _MARKER_RE.match(line):
            continue
        unparsed.append(line.strip())
    return unparsed


# ---------------------------------------------------------------------------
# Verdict — pure
# ---------------------------------------------------------------------------


def evaluate(base_ids: set[str] | frozenset[str], head_ids: set[str] | frozenset[str],
             markers: list[Marker],
             unparsed_removed_test_lines: tuple[str, ...] = ()) -> Report:
    """Compute the whole verdict from two id sets and a marker list. Pure.

    unparsed_removed_test_lines (#2393, optional, default empty): lines from
    `find_unparsed_removed_test_lines` that mentioned `removed-test:` but did
    not parse. Advisory only, same as a stale marker below — a malformed
    line must not block a PR that is otherwise accounted for; it should
    just stop being invisible.
    """
    base_ids = set(base_ids)
    head_ids = set(head_ids)
    disappeared = base_ids - head_ids
    appeared = head_ids - base_ids

    accounted: set[str] = set()
    warnings: list[str] = []
    ambiguous: list[str] = []

    for line in unparsed_removed_test_lines:
        warnings.append(
            f"a 'Removed-test:' line did not parse and was ignored — expected "
            f"'Removed-test: <path::name> — <reason>' with an em-dash or "
            f"hyphen separator. Line: {line!r}"
        )

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


def _is_ancestor(repo_root: Path, base: str, head: str) -> bool | None:
    """True if `base` is an ancestor of (or equal to) `head`, False if not,
    None if git could not answer at all. Measured exit codes (Ariadne,
    #2381): 0=ancestor, 1=NOT an ancestor (both a genuine divergence AND
    unrelated histories share this code), 128=invalid revision. Checks
    `== 1` specifically rather than `!= 0`, per that measurement — the
    128 case is unreachable in practice (`main()` only calls this after
    `read_sources_at_rev` has already resolved both revs via `ls-tree`,
    which fails fast with its own `GitReadError` on a bad rev first), but
    coding the precise value rather than the negation is correct on its
    own terms and does not depend on staying unreachable. The caller
    treats None the same as True (stay silent) since this check is
    advisory only and must never itself crash or gate the tool.
    Deliberately a raw `subprocess.run`, not `_run_git`: a non-ancestor
    result is exit code 1, which `_run_git` would otherwise raise as a
    `GitReadError`."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", base, head],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _merge_base(repo_root: Path, base: str, head: str) -> str | None:
    """Return the real merge-base of `base` and `head`, or None if `base`
    and `head` share no common ancestor at all (unrelated histories).
    Measured (Ariadne, #2381): `git merge-base` on two unrelated histories
    exits 1 with EMPTY stdout — a hazard for a naive implementation that
    checked only stdout-non-empty and missed the exit code, but this
    function checks `returncode != 0` first, and exit 1 is already
    nonzero, so that single check catches the unrelated-histories case
    with no separate stdout check needed. Verified by mutation: dropping
    to only a `not stdout.strip()` check (no returncode check at all)
    would fail to distinguish "no merge-base" from "merge-base is the
    empty tree", which does not exist as a real git output — so the
    returncode check alone is both necessary and sufficient, and an
    additional stdout-emptiness check on top of it is dead code that adds
    a rationale for a case that cannot occur (the same shape as the
    128-exit-code case `_is_ancestor` explicitly declines to test). The
    caller must still handle None as its own distinct case (say so
    explicitly) rather than treat it as "nothing to report" — printing a
    warning with a blank interpolated into it is worse than no warning."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", base, head],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def warn_if_base_is_not_ancestor(repo_root: Path, base: str, head: str) -> None:
    """#2381: `--base` naming the target branch's current tip instead of the
    merge-base fabricates disappearances for anything that landed on the
    target branch after this branch was cut. This is advisory ONLY —
    deliberately a `::warning::`, never a failure or a silent auto-resolve
    (see the module docstring's #2381 note for why: a legitimate
    non-ancestor base is conceivable, and a tool that quietly reinterprets
    the rev it was handed is a worse trap than the one this closes). Prints
    the real merge-base so the reader has the exact fix in hand rather than
    just being told something is wrong — UNLESS `base`/`head` share no
    common ancestor at all (`_merge_base` returns None), in which case
    there is no fix to hand over and the message says so instead of
    printing a warning with a blank where the SHA belongs. Silently does
    nothing if the ancestor check itself cannot be answered at all
    (`_is_ancestor` returns None) — an advisory check must never become a
    second way for this tool to error out."""
    is_ancestor = _is_ancestor(repo_root, base, head)
    if is_ancestor is not False:
        return
    merge_base = _merge_base(repo_root, base, head)
    if merge_base is None:
        print(
            f"::warning::test_id_set_check: --base {base} is not an ancestor of "
            f"--head {head} (see #2381), and the two have no common ancestor at "
            f"all (unrelated histories) -- some reported disappearances below "
            f"may be an artifact of that rather than real deletions.",
            file=sys.stderr,
        )
        return
    print(
        f"::warning::test_id_set_check: --base {base} is not an ancestor of "
        f"--head {head} (see #2381) -- some reported disappearances below may "
        f"be commits this branch simply predates, not real deletions. The "
        f"actual merge-base is {merge_base}; re-run with "
        f"--base {merge_base} before trusting a disappearance.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail a PR whose test-id set shrinks without an explicit Removed-test marker (#2350)."
    )
    parser.add_argument(
        "--base",
        required=True,
        metavar="REV",
        help="Base git revision — MUST be the merge-base with the target branch "
        '(e.g. $(git merge-base origin/dev HEAD)), never the target branch\'s '
        "current tip. Passing the tip fabricates disappearances for anything "
        "that landed on the target branch after this branch was cut (#2381).",
    )
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

    # Deliberately AFTER both revs are already confirmed resolvable above:
    # an invalid --base/--head fails loudly via GitReadError first, so this
    # advisory check only ever runs on revs git can already read (#2381).
    warn_if_base_is_not_ancestor(repo_root, args.base, args.head)

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
    unparsed_removed_test_lines = find_unparsed_removed_test_lines(pr_body)
    report = evaluate(base_ids, head_ids, markers, tuple(unparsed_removed_test_lines))

    if args.format == "json":
        print(format_report_json(report))
    else:
        print(format_report_text(report))

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
