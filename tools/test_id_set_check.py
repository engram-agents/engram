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

Escape hatch — a line, in EITHER the PR body OR the base..head diff itself
(#2568):

    Removed-test: <id> — <reason>

optionally prefixed by a markdown list marker (`- `, `* `, `+ `, #2421) or a
source-code comment marker (`# `, `// `, `<!-- `, #2568 — this is what lets
the SAME line, written as a comment beside the actual removal, account for
it), accounts for a disappeared test, where <id> is either the full id
(`path::name`) or a bare function/method name (accepted only when exactly one
disappeared id ends with `::<name>` — an ambiguous bare name is NOT accounted,
and the failure message says the full id is needed). The separator is an
em-dash or a hyphen; the reason must be non-empty after stripping. A marker
naming an id that did NOT disappear is a warning, not a failure: deliberate,
because a PR author who correctly restored a test after writing the marker
line should not be blocked by their own now-stale housekeeping.

The two surfaces are read independently (never concatenated into one text
before parsing — a stray backtick in one must never be able to swallow a
marker in the other) and unioned before the accounting check. Placing the
marker in the diff, next to the removal, is the more durable choice: it
survives the PR closing, it's versioned in the repo, and a future reader of
the file finds it where the removal actually happened, none of which is
true of a PR body. The gate does not care which surface an author picks.

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
#
# #2568 -- a leading source-code comment marker (`# `, `// `, `<!-- `) is
# ALSO permitted, same slot as the list markers above. This is what makes
# a marker written as a comment beside the actual removal in a diff (not
# just in the PR body) parse at all -- see parse_removed_test_markers_
# from_diff() below, the second surface that reuses this SAME regex so the
# two call sites (body vs diff) can't drift apart the way #2421 fixed for
# body-only list markers. Widening this ONE shared anchor means a
# `# Removed-test: ...` line in the PR body ALSO now parses, which is
# correct and intentional (same reasoning as the list-marker widening: a
# marker is a marker regardless of which surface's decoration wraps it).
_MARKER_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|#[ \t]+|//[ \t]+|<!--[ \t]+)?removed-test:[ \t]+(?P<id>\S+)[ \t]+[—-][ \t]*(?P<reason>.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# A GFM fenced code block's opening delimiter: 3+ backticks OR 3+ tildes,
# optionally indented, optionally followed by a language tag on the same
# line. Captured so the closing-fence check below can require the SAME
# character repeated AT LEAST as many times (CommonMark's own rule) --
# without that, a stray "```" inside a ```` ```python ```` ... ```` ``` ````
# block (or a tilde fence closed by a shorter run of tildes) could close
# early or never.
_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def _split_out_fenced_code_blocks(text: str) -> list[tuple[bool, str]]:
    """Split `text` into (is_fenced, segment) pairs, line-aligned, so a
    caller can leave fenced-block content untouched while still processing
    everything else (#2539/#2540).

    Root cause this exists to fix: `parse_removed_test_markers`'s inline-
    code strip (`re.sub(r"`[^`]*`", "", pr_body)`) treats a fence's THREE
    backticks as one-and-a-half single-backtick spans -- the first two pair
    off as an empty span, the third dangles and pairs with the *closing*
    fence's first backtick, silently swallowing every line in between
    (measured: a real 7-marker fenced block parsed as 0 markers). A fence
    is not inline code and not a typo; it is the standard, correct markdown
    for presenting a literal multi-line list -- exactly what a
    `Removed-test:` block is. Segmenting fenced regions OUT before the
    inline-code strip runs (and leaving their content byte-for-byte
    untouched) means a marker written inside a proper fence parses like any
    other marker, while an unmatched/typo'd single backtick OUTSIDE a fence
    still gets the existing (documented, fail-safe) swallow-to-next-
    backtick treatment -- this only widens what counts as "not inline
    code," it does not change how inline code itself is handled.

    LIMIT (by design, same posture as the module's other LIMIT entries):
    this recognizes GFM fences by their DELIMITER LINES only (indentation
    allowed, an optional language tag on the open line, and a closing line
    of the same fence character repeated at least as many times) -- it is
    not a CommonMark parser and does not handle nested fences of the same
    character (CommonMark itself does not define nesting for identical
    fence characters; a real block needing that should use the OTHER fence
    character, same as any markdown renderer). An unterminated fence (no
    matching close before the body ends) is treated as extending to the end
    of the text, matching CommonMark's own end-of-document closing rule.

    Pure. Returns [(False, text)] unchanged when no fence is found.
    """
    lines = text.split("\n")
    segments: list[tuple[bool, str]] = []
    current: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in lines:
        if not in_fence:
            m = _FENCE_OPEN_RE.match(line)
            if m:
                if current:
                    segments.append((False, "\n".join(current)))
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                current = [line]
                in_fence = True
            else:
                current.append(line)
            continue

        current.append(line)
        stripped = line.strip()
        if stripped and set(stripped) == {fence_char} and len(stripped) >= fence_len:
            segments.append((True, "\n".join(current)))
            current = []
            in_fence = False

    if current:
        segments.append((in_fence, "\n".join(current)))

    return segments


def _strip_inline_code_preserving_fences(pr_body: str) -> str:
    """The `parse_removed_test_markers`/`find_unparsed_removed_test_lines`
    strip step, fence-aware (#2539/#2540): identical single-backtick-span
    stripping outside a fenced code block, and a byte-for-byte pass-through
    for everything inside one."""
    parts = []
    for is_fenced, segment in _split_out_fenced_code_blocks(pr_body):
        parts.append(segment if is_fenced else re.sub(r"`[^`]*`", "", segment))
    return "\n".join(parts)


def parse_removed_test_markers(pr_body: str) -> list[Marker]:
    """Parse every `Removed-test: <id> — <reason>` line in a PR body.

    Inline-code spans (`` `...` ``) are stripped BEFORE parsing, the same way
    the sibling anti-dup workflow step strips them before grepping
    (`sed 's/`[^`]*`//g'`) — this repo has a documented incident class (the
    close-keyword trap discipline in CLAUDE.md) for an unbackticked example
    firing a body-text parser. A PR body that *illustrates* the
    `Removed-test:` syntax inside backticks must not be parsed as a real
    marker — an intentional marker must be written as plain text.

    A fenced code block (```` ``` ```` ... ```` ``` ````) is NOT treated as
    inline code for this strip (#2539/#2540) — its content passes through
    untouched, via `_strip_inline_code_preserving_fences`. Without this, a
    fence's three backticks are read as one-and-a-half single-backtick
    spans by the naive strip (the first two pair off empty, the third
    dangles and pairs with the *closing* fence), swallowing every marker
    inside — measured: a real 7-marker fenced block parsed as 0. A fence is
    the standard, correct markdown for presenting a literal multi-line
    list, which is exactly what a `Removed-test:` block is; unlike an
    unmatched single-backtick typo (still handled by the LIMIT below), it
    is not a mistake to guard against.

    LIMIT (by design, not oversight — narrower after #2539/#2540, not
    removed): OUTSIDE a fence, the strip is still a regex over the whole
    body, not a markdown parser, so it has no notion of "this backtick was
    never closed." An unmatched backtick anywhere earlier in the
    non-fenced text pairs with the next one that appears later — even
    across unrelated paragraphs — and everything between the two,
    including a well-formed `Removed-test:` marker sitting in that gap, is
    swallowed along with it. This fails SAFE: the swallowed marker never
    reaches the marker regex, so the disappearance it would have accounted
    for reads as unaccounted and the gate FAILS LOUDLY, rather than a real
    deletion slipping through silently. A markdown-correct parser (one
    that actually tracks whether a given single backtick opens or closes a
    span) is out of scope for the same reason the module docstring's other
    LIMIT entries are: real parsing machinery this tool deliberately
    avoids. Practical guidance, narrowed to what still applies: write
    `Removed-test:` marker lines as isolated lines, away from any
    single-backtick inline code (a fenced block is now safe).

    Pure. Returns a Marker even when the reason is empty after stripping —
    evaluate() is the layer that decides an empty-reason marker accounts for
    nothing (see module docstring: "reason must be non-empty after stripping"
    is an accounting rule, not a parse-time rejection).
    """
    if not pr_body:
        return []
    stripped_body = _strip_inline_code_preserving_fences(pr_body)
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

    Uses the same fence-aware strip as `parse_removed_test_markers`
    (#2539/#2540) — a marker inside a fence parses cleanly there, so it
    correctly never reaches this warning path at all.

    Pure. Returns the raw (stripped) line text for each candidate that
    mentions the key but does not parse — empty list when every mention
    parses or none exists.
    """
    if not pr_body:
        return []
    stripped_body = _strip_inline_code_preserving_fences(pr_body)
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
            f"(add 'Removed-test: {tid} — <reason>' to the PR body, or as a "
            f"comment beside the removal in the diff, if this deletion is "
            f"legitimate)"
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


# Every directory tests.yml's full-suite invocation collects
# (`pytest tests/ src/forum/tests/ -v --tb=short` — see
# .github/workflows/tests.yml). This tool's own git-ls-tree scan roots
# must track that list by hand — the two are NOT auto-synced (there is no
# shared config file between a Python tool and a YAML workflow step), so a
# THIRD scan root added to tests.yml in the future needs a matching entry
# here, same "duplicated by hand, change together" convention as this
# repo's other cross-file-kept-in-sync lists. tests/test_test_id_set_check.py
# now CI-enforces this sync (#2950) — see `_parse_workflow_scan_roots` below.
TEST_SCAN_ROOTS: tuple[str, ...] = ("tests/", "src/forum/tests/")


# Matches a GENUINE YAML `run:` step key invoking pytest (e.g. `        run:
# pytest tests/ src/forum/tests/ -v --tb=short`), tolerant of extra
# whitespace around `run:`/`pytest` — see `_parse_workflow_scan_roots`.
#
# Anchored to the actual START of the line (ignoring only leading
# whitespace/indentation and an optional YAML list-item `-` marker before
# the key) and matched with `.match()`, not `.search()` — reviewer-fairy
# round 2 (#2950/#2959): the prior unanchored `run:\s*pytest\s+(.+)` was
# applied via `.search()`, which finds the pattern ANYWHERE in a line, so
# both of these false-positived as the live invocation:
#   `# run: pytest tests/ src/forum/tests/ -v --tb=short` (a COMMENT)
#   `run: echo "run: pytest tests/ src/forum/tests/ -v"`   (pytest TEXT
#       embedded inside a different command's string argument -- the
#       line's real command is `echo`, not `pytest`)
# Commenting out the real invocation line could therefore leave a
# false-green sync check. Anchoring to the line's own start (via `.match()`,
# which only tries position 0) and requiring `pytest` immediately after
# `run:` (only whitespace between) means neither false shape can match: a
# `#`-prefixed comment line fails the anchor, and `run: echo ...` fails
# because the token right after `run:` is `echo`, not `pytest` -- the
# `pytest` text later in the same line is never reached, since `.match()`
# does not retry from later positions the way `.search()` does.
_WORKFLOW_PYTEST_RUN_RE = re.compile(r"^[ \t]*(?:-[ \t]+)?run:[ \t]*pytest[ \t]+(.+)")


def _parse_workflow_scan_roots(workflow_text: str) -> tuple[str, ...]:
    """Parse the bare positional path arguments out of a workflow YAML's
    full-suite `run: pytest ...` invocation line (#2950) — the counterpart
    a CI-enforced test compares against TEST_SCAN_ROOTS above to catch the
    two drifting apart (a third scan root added to one side and not the
    other).

    Pure — takes the workflow file's TEXT CONTENT as a string; does no file
    I/O of its own (the caller reads the file and passes the text), matching
    this whole test suite's stated "no git, no tmp repos" pure-function
    testability-seam design philosophy (see this file's module docstring).

    Robust to minor formatting variation (extra whitespace around `run:`/
    `pytest`, a single root, no trailing flags) — deliberately NOT
    brittle-matched to today's exact byte string. Scans `workflow_text`
    line by line for a line whose own START (ignoring only leading
    whitespace and an optional YAML list-item `-` marker) matches
    `run:[ \\t]*pytest[ \\t]+(.+)` — NOT a bare substring search, so a
    comment (`# run: pytest ...`) or pytest-shaped text embedded inside a
    different command's string argument (`run: echo "run: pytest ..."`)
    is never mistaken for the real invocation (#2950 reviewer-fairy round
    2; see `_WORKFLOW_PYTEST_RUN_RE`'s own comment for the false-positive
    shapes this closes). From the matching line's captured remainder, takes
    the BARE POSITIONAL tokens — those that don't start with `-` — stopping
    at the first flag-shaped token (`-v`, `--tb=short`, …), which mirrors
    the real invocation's own shape (positional path args first, flags
    last).

    Raises ValueError — never returns an empty tuple or None — when NO
    `run: pytest ...` line is found at all, or when MORE THAN ONE is found
    (reviewer-fairy round 1, #2950: the original version returned on the
    first match, which is safe today — the real workflow has exactly one
    such line — but would silently pick a decoy if a second `run: pytest
    ...` step were ever added elsewhere in the workflow, e.g. a narrower
    smoke-test job). A workflow file restructured such that this parser can
    no longer find its own invocation line UNAMBIGUOUSLY is exactly the
    kind of drift this gate exists to catch; staying silent here (an empty
    tuple comparing "equal" to nothing, a None the caller might not check,
    or an arbitrarily-chosen one of several matches) would defeat the
    gate's own purpose.
    """
    matches = [
        _WORKFLOW_PYTEST_RUN_RE.match(line) for line in workflow_text.splitlines()
    ]
    matches = [m for m in matches if m is not None]
    if not matches:
        raise ValueError(
            "_parse_workflow_scan_roots: no 'run: pytest ...' invocation line "
            "found in the given workflow text — the workflow may have been "
            "restructured (matrix/composite action/etc.); this parser needs "
            "updating to match the new shape (#2950)."
        )
    if len(matches) > 1:
        raise ValueError(
            f"_parse_workflow_scan_roots: found {len(matches)} 'run: pytest ...' "
            "invocation lines in the given workflow text -- expected exactly one. "
            "Ambiguous: this parser can't tell which is the full-suite invocation "
            "TEST_SCAN_ROOTS should track (#2950)."
        )
    roots: list[str] = []
    for token in matches[0].group(1).split():
        if token.startswith("-"):
            break
        roots.append(token)
    return tuple(roots)


def list_test_files(repo_root: Path, rev: str) -> list[str]:
    """Enumerate every *.py under any of TEST_SCAN_ROOTS at `rev`, POSIX-relpath,
    via git ls-tree."""
    paths: list[str] = []
    for root in TEST_SCAN_ROOTS:
        out = _run_git(repo_root, ["ls-tree", "-r", "--name-only", rev, "--", root])
        paths.extend(line for line in out.splitlines() if line.endswith(".py"))
    return paths


def read_sources_at_rev(repo_root: Path, rev: str) -> dict[str, str]:
    """Read every tests/*.py file's source text at `rev`, without checkout."""
    sources: dict[str, str] = {}
    for path in list_test_files(repo_root, rev):
        sources[path] = _run_git(repo_root, ["show", f"{rev}:{path}"])
    return sources


def read_diff_added_text_by_file(repo_root: Path, base: str, head: str) -> list[str]:
    """#2568 — the added (`+`) lines of the base..head unified diff, `+`
    prefix stripped, grouped ONE BLOB PER FILE (files with no added lines
    are omitted).

    A `Removed-test:` marker written as a comment beside the actual
    removal (in the diff, not the PR body) is real accounting -- it sits
    next to the code a future reader will look at, it's versioned in the
    repo, and it survives the PR closing (see the issue's own case: three
    well-formed markers at a specific file:line, invisible to a
    body-only parser, failing a PR whose accounting was actually
    complete). No path restriction to tests/ -- an author may document a
    removal anywhere in the diff, same as the PR-body path has no
    restriction on where in the body a marker may appear.

    Per-file grouping (not one flat blob for the whole diff) matters for
    the same reason #2539/#2540 fixed cross-blob leakage at the
    body-vs-diff boundary: `_strip_inline_code_preserving_fences`'s
    unmatched-backtick-swallow LIMIT pairs an unclosed backtick with the
    NEXT backtick it finds, however far away. A flat whole-diff blob lets
    a stray backtick added in one file swallow a marker added in an
    unrelated file elsewhere in the same PR -- caught live on #2632's own
    review, by running this exact function against #2632's own diff.
    Callers strip each file's blob independently (see
    parse_removed_test_markers_from_diff) so backtick-pairing stays
    scoped to the file that introduced it.

    Diff header lines (`diff --git`, `index`, `--- a/path`, `+++ b/path`,
    mode/similarity/rename lines) are excluded by POSITION relative to the
    file's first hunk, not by content shape. Every one of those header
    lines appears ONLY before a file's first `@@ ... @@` hunk marker --
    that placement is fixed by the diff format itself, never by content --
    so everything before the first `@@` for a file is unconditionally
    skipped, and everything from the first `@@` onward is unconditionally
    real hunk content (a `+`-prefixed line is always a genuine addition
    there, never a header).

    A shape-based check (`line.startswith("+++")`) instead matches a
    genuine ADDED line whose own content starts with `++` -- git's own `+`
    prefix turns "++x; ..." into "+++x; ...", indistinguishable from a
    header by shape alone (caught on #2632's own review, round 1). A
    NARROWER positional check (skip a `+++` line only when it immediately
    follows a `--- ` line) still collides: a REMOVED line whose own
    content starts with `-- ` becomes, after git's single-`-` prefix, a
    line that also starts with `--- `, so an adjacent removed-`-- `/
    added-`++` pair in the same one-line hunk is still misread as a
    genuine header pair (caught on #2632's review, round 2). Anchoring on
    "has this file's first hunk marker been seen yet" has no shape
    collision to inherit, because it never inspects the CONTENT of the
    line being classified -- only whether a `@@` has been seen since the
    last `diff --git`.
    """
    diff_text = _run_git(repo_root, ["diff", "--unified=0", f"{base}..{head}"])
    files: list[str] = []
    current: list[str] = []
    in_hunk = False  # this file's first "@@ ... @@" hunk marker seen yet?
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current:
                files.append("\n".join(current))
                current = []
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            # Still in this file's extended header block (index/---/+++/
            # mode/similarity/rename lines, or a "Binary files ... differ"
            # line for a binary diff) -- none of it is hunk content,
            # regardless of what any individual line's text looks like.
            continue
        if line.startswith("+"):
            current.append(line[1:])
    if current:
        files.append("\n".join(current))
    return files


def parse_removed_test_markers_from_diff(repo_root: Path, base: str, head: str) -> list[Marker]:
    """#2568 — the diff-sourced sibling of parse_removed_test_markers.

    Deliberately parsed as its OWN text blob PER FILE, never concatenated
    across files or with the PR body before parsing:
    `_strip_inline_code_preserving_fences`'s unmatched-backtick-swallow
    LIMIT is a real (accepted) hazard, and concatenating semantically
    unrelated blobs would let a stray backtick in one swallow a marker in
    another -- exactly the cross-file leakage class #2539/#2540 fixed for
    markdown fences, just at a different boundary (see
    read_diff_added_text_by_file's docstring). Parsing each file's added
    text independently and UNIONING the resulting marker lists (across
    files here, and across the body/diff surfaces at this function's
    caller) keeps every blob's backtick-pairing failure domain separate.
    """
    markers: list[Marker] = []
    for file_added_text in read_diff_added_text_by_file(repo_root, base, head):
        stripped = _strip_inline_code_preserving_fences(file_added_text)
        markers.extend(
            Marker(id=m.group("id"), reason=m.group("reason").strip(), raw_line=m.group(0))
            for m in _MARKER_RE.finditer(stripped)
        )
    return markers


def find_unparsed_removed_test_lines_from_diff(repo_root: Path, base: str, head: str) -> list[str]:
    """#2568 — the diff-sourced sibling of find_unparsed_removed_test_lines,
    same reasoning: a `Removed-test:`-shaped line added in the diff that
    fails to parse deserves the identical #2393 fail-loud treatment a
    malformed body line gets. Same per-file scoping as
    parse_removed_test_markers_from_diff, for the same reason."""
    unparsed = []
    for file_added_text in read_diff_added_text_by_file(repo_root, base, head):
        stripped = _strip_inline_code_preserving_fences(file_added_text)
        for line in stripped.splitlines():
            if "removed-test:" not in line.lower():
                continue
            if _MARKER_RE.match(line):
                continue
            unparsed.append(line.strip())
    return unparsed


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

    # #2568: accounting comes from EITHER surface -- the PR body (unchanged)
    # OR a Removed-test marker written as a comment beside the removal in
    # the diff itself. Each is parsed independently (never concatenated —
    # see parse_removed_test_markers_from_diff's docstring for why) and the
    # resulting marker/warning lists are unioned before evaluate() sees them.
    try:
        diff_markers = parse_removed_test_markers_from_diff(repo_root, args.base, args.head)
        diff_unparsed = find_unparsed_removed_test_lines_from_diff(repo_root, args.base, args.head)
    except GitReadError as exc:
        print(f"test_id_set_check: error — {exc}", file=sys.stderr)
        return 2

    markers = parse_removed_test_markers(pr_body) + diff_markers
    unparsed_removed_test_lines = find_unparsed_removed_test_lines(pr_body) + diff_unparsed
    report = evaluate(base_ids, head_ids, markers, tuple(unparsed_removed_test_lines))

    if args.format == "json":
        print(format_report_json(report))
    else:
        print(format_report_text(report))

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
