import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = REPO_ROOT / "src" / "engram" / "skills"
HEALTH_EXAM_SKILL = (
    REPO_ROOT
    / "src"
    / "engram"
    / "skills"
    / "claude"
    / "engram-health-exam"
    / "SKILL.md"
)


def test_health_exam_skill_uses_supported_selftest_smoke_flag() -> None:
    content = HEALTH_EXAM_SKILL.read_text(encoding="utf-8")

    assert '"$HOME/.engram/venv/bin/python3" tools/selftest.py --smoke 2>&1 | tail -20' in content
    assert "python3 tools/selftest.py --quick" not in content


# ---------------------------------------------------------------------------
# #2132 / #2133 — the pgrep liveness idiom across ALL shipped skill docs
# ---------------------------------------------------------------------------
#
# engram-health-exam/SKILL.md used to carry `pgrep -f <pat> || echo "not
# running"`, broken three independent ways:
#
#   1. No `-u`, so it counted OTHER SEATS' processes. Measured on a shared
#      macOS host: unscoped 2, `-u "$(id -u)"` 1 — the extra belonged to a
#      co-resident agent. Measured on a shared Linux host, same shipped line:
#      unscoped 5, scoped 1. The exam reported a healthy-looking count made
#      mostly of other people's processes, and would report the same with this
#      seat's own daemon dead.
#   2. `-a` on one site. Darwin pgrep(1) excludes the current process AND all
#      its ancestors by default; `-a` puts the ancestors back. (procps-ng `-a`
#      is `--list-full` — formatting only. Same flag, different meaning per
#      platform.) Since an agent runs these docs through a harness whose
#      wrapper-shell argv contains the command text — including the search
#      pattern — an ancestor matches and the `|| echo` branch never fires.
#      Scope it precisely: run from a clean script where the pattern appears
#      in no ancestor's argv, rc=1 and the branch IS reachable. Unreachable in
#      practice, reachable in principle.
#   3. `||` conflated "found nothing" with "no instrument". pgrep exits 1 =
#      ran, found nothing; >1 = could not run (e.g. 127, not installed). As
#      written, an absent pgrep printed the same reassuring "not running" line
#      as a genuine absence — a different claim entirely.
#
# None of the three is visible to a diff reviewer or to CI reading live pgrep
# output: each fails by producing a plausible, healthy-looking result.
#
# WHY THIS GATE WALKS THE WHOLE SKILL-DOC POPULATION rather than the one file
# that was broken: tests/test_pgrep_liveness_reap_patterns.py already carries a
# SAFE/MITIGATED/FIXED inventory of this exact failure mode, built for #2029 —
# and it missed these three sites because it is scoped by ARTIFACT TYPE. It
# covers scripts and modules; a skill doc is neither. An inventory scoped by
# artifact type is blind exactly where a different artifact type carries the
# same idiom, and is worse than no inventory there, because its existence reads
# as coverage. Gating only engram-health-exam/SKILL.md would reproduce that
# mistake one level down, so the gate below takes `skills/**/*.md` as a class.
#
# NOT COVERED HERE, DELIBERATELY: the Python liveness sites (_hooklib.py,
# engram-session-start-hook.py, supervision_probe.py) scope by RESOLVED
# ABSOLUTE PATH rather than by `-u`. Each seat's path differs, so path-scoping
# achieves the same isolation; their docstrings name the #1839 regression that
# established it. That is a different correct answer, not an oversight.

# A `pgrep` token counts as an INVOCATION only when it appears inside a fenced
# code block AND sits in COMMAND POSITION — start of line, or immediately after
# one of the operators or reserved words enumerated in `_COMMAND_POSITION`
# below. That enumeration is the whole of what "command position" means here,
# so it is written out there rather than summarised twice; #3070 found this
# summary and the pattern already disagreeing.
#
# The two conditions do different jobs, and #3070's review showed that the
# earlier comment here mis-assigned the work. `_code_blocks()` ALONE excludes
# every prose mention — "`pgrep -u`-scoped so it never touches another agent's
# monitor" (engram-collaborating-loop), "`pgrep -f` self-matched the invoking
# shell" (engram-upgrade), "# Wait 10s, then re-check pgrep" (a whole-line
# comment) — because none of them is executable text inside a fence. So the
# second condition was never what kept prose out, and claiming it was made the
# condition look better-motivated than it was. What command position actually
# adds is the exclusion of non-invocation mentions that ARE inside a fence and
# ARE on an executable line: `echo "could not check: pgrep exited $rc"`, where
# the token sits mid-line inside a quoted string.
#
# WHY COMMAND POSITION RATHER THAN "the next token looks like argv": the
# previous form required the first argv token to begin with `-`, `'` or `"`,
# which let
#     pgrep engram-surface-daemon || echo "daemon not running"
# through EVERY gate in this file — unscoped, and carrying the exact `|| echo`
# conflation these gates exist to forbid. Measured on the pre-fix gate, that
# line planted in a skill doc gave 0 failures; the same line with the pattern
# quoted gave 2, so quoting alone was the axis. Widening the character class
# after the token preserves the SHAPE of that hole: argv spellings are an
# UNBOUNDED set, so there is always one more unquoted, unflagged spelling and
# no amount of widening ever finishes.
#
# WHAT ANCHORING ON POSITION ACTUALLY BUYS, stated precisely because the
# earlier version of this comment over-claimed it. It does NOT make the hole
# go away; it MOVES the hole, from "what follows the token" to "the
# enumeration of positions". The move is still worth making, because the two
# sets differ in kind rather than in size: argv spellings are unbounded and
# open-ended, whereas the OPERATOR and COMPOUND-COMMAND positions of POSIX XCU
# 2.9.4 are a finite list fixed by the shell grammar, which can therefore be
# enumerated and locked by a test. Once the token is matched, its argv is read
# to end of line whatever it looks like, so nothing about the argv can hide it.
#
# The enumeration below is exhaustive over THOSE positions and nothing wider.
# Two classes of real invocation are NOT covered, named here so that the next
# maintainer reads the enumeration as the bounded thing it is:
#
#   1. The XCU 2.9.1 `cmd_prefix` position. A simple command is
#      `cmd_prefix cmd_word cmd_suffix`, so the command word may be preceded by
#      assignments and redirections — `LC_ALL=C pgrep ...`,
#      `>/dev/null pgrep ...` — neither of which this pattern sees.
#   2. WRAPPER PREFIXES, where pgrep is the command actually run although the
#      grammar calls it an argument: `sudo`, `env`, `nohup`, `timeout`,
#      `command`, `eval`, `exec`, `xargs` — and `time -p`, while bare `time`
#      IS covered, because `time` is a reserved word in the list below and
#      `-p` then occupies the slot the pattern expects pgrep in.
#
# #3070 measured all 11 of those spellings returning ZERO invocations here
# (Darwin 25.6.0, CPython 3.12.10), against four controls returning 1 — `pgrep`
# at line start, `if pgrep`, a `case` branch body, and bare `time pgrep`. The
# `time` / `time -p` pair is the sharpest statement of the bound: the
# enumeration is not exhaustive even over a word it already contains.
#
# WHY THE CLAIM IS BOUNDED RATHER THAN THE PATTERN WIDENED — and this is the
# same argument as the argv one above, one level up. The wrapper set is OPEN:
# any command can wrap another, so `stdbuf`, `setsid` and whatever a future doc
# reaches for belong to it too. An open set cannot be enumerated, so
# exhaustiveness over command position is unattainable in principle here, and
# widening would buy a third exhaustiveness claim rather than a closed hole.
# Stating what IS covered and naming what is NOT is the only remedy that does
# not re-create the problem one level down. A doc using one of the two
# uncovered classes above is read by NO gate in this file and goes green — so
# when reviewing such a line, check it by hand, or extend the enumeration and
# this note together.
#
# The covered part is not optional diligence: #3070 measured the previous,
# shorter version of the enumeration returning ZERO invocations for a doc
# spelling `if pgrep -af "..."; then`, `while pgrep ... ; do`, `until ...`,
# `elif ...`, `! pgrep ... && echo ...` and `( pgrep ... ) || echo ...` — six
# ordinary spellings, all invisible, silently satisfying four gates at once. A
# false CLEAN is the exact failure these gates exist to prevent, so the covered
# positions are locked below by
# test_pgrep_invocation_is_seen_in_every_ENUMERATED_command_position.
_FENCED_BLOCK_RE = re.compile(r"^```[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)

# Punctuation that ends one command and so opens the next. These are single
# characters on purpose: `||`, `&&`, `;;` and bash's `;&` / `;;&` all END with
# one of them, so the class matches the final character and the `[ \t]*` below
# absorbs the gap — the pairs need no separate spelling.
#
#   ( `     open a command: a subshell `( pgrep ... )`, a `$(...)` command
#           substitution, a `<(...)` process substitution, and the legacy
#           backtick form. `(` is why `$(` no longer needs its own alternative.
#   )       opens a `case` BRANCH BODY — `status) pgrep -u ... ;;` is an
#           invocation in every sense these gates care about. `)` also CLOSES
#           a subshell, where what follows is not a command; including it
#           therefore over-matches slightly, which is the correct direction
#           for a guard (the same reasoning the argv-to-end-of-line note below
#           already relies on) — over-matching fails loud and is fixed by
#           looking, while under-matching is the silent false CLEAN above.
#   {       brace group, `{ pgrep ...; }`.
#   !       pipeline negation, `! pgrep ... && echo ...`.
_COMMAND_OPERATOR = r"[|;&(){!`]"

# Reserved words whose grammar slot is a COMMAND (POSIX XCU 2.9.4 compound
# commands). `time` and `coproc` are not POSIX reserved words but are reserved
# in bash/zsh and each takes a command, so they are the same slot.
#
# DELIBERATELY ABSENT, because their slot is NOT a command and adding them
# would match text that never runs anything:
#   for, select   take a NAME  (`for pgrep in ...` binds a variable)
#   in            takes a word list (`for x in a b`) or, after `case`, a
#                 PATTERN — `case $1 in pgrep) ...` is a label, not a call
#   case          takes the WORD being matched
#   fi done esac  terminate a construct rather than introduce one; `}` too
_COMMAND_RESERVED_WORD = r"\b(?:if|elif|then|else|while|until|do|time|coproc)\b"

_COMMAND_POSITION = (
    r"(?:^|" + _COMMAND_OPERATOR + r"|" + _COMMAND_RESERVED_WORD + r")[ \t]*"
)
_PGREP_INVOCATION_RE = re.compile(
    _COMMAND_POSITION + r"pgrep\b(?P<argv>[^\n]*)", re.M
)

# Short-option clusters only: `-u`, `-f`, `-af`. The lookahead is what stops
# this from matching the `-u` inside `"$(id -u)"`, which is an argument rather
# than a flag — there it is followed by `)`, not by whitespace or end-of-line.
#
# NOTE the argv above runs to END OF LINE, not to the first `)`. Stopping at
# `)` would truncate `pgrep -u "$(id -u)" -af "x"` to `-u "$(id -u` and lose
# the `-af` entirely — i.e. the -a gate would pass on the very mutation it
# exists to catch. Over-reading the rest of the line errs loud, which is the
# correct direction for a guard.
_SHORT_FLAG_RE = re.compile(r"(?<![\w-])-([A-Za-z]+)(?=[ \t]|$)")

# The one-liner form: a pgrep whose failure is handled by `|| echo` on the same
# line. Applied to the CAPTURED ARGV, which begins after the pgrep token, so it
# inherits the command-position anchor above instead of re-deriving its own
# notion of what an invocation looks like — the previous copy of that argv
# condition lived here too, and carried the same blind spot (`pgrep foo || echo
# ...` was invisible to this gate for exactly the same reason).
#
# The fixed form's `[ "$rc" -eq 1 ] && echo ... || echo "could not check: pgrep
# exited $rc"` cannot false-positive: there the `||` precedes the word `pgrep`
# and so is not part of any argv.
_OR_ECHO_RE = re.compile(r"\|\|[^\n]*\becho\b")


def _skill_docs() -> list[Path]:
    return sorted(SKILLS_ROOT.rglob("*.md"))


def _code_blocks(content: str) -> str:
    """Fenced-block bodies, with WHOLE-LINE shell comments dropped.

    Comment lines are documentation, not commands — the fixed Step 0 block
    below literally spells out `for path in ...` in a comment explaining why
    it must not be used, and a guard that fires on its own rationale is a
    guard people delete. Only lines whose FIRST non-space character is `#` are
    dropped: a trailing `#` cannot be stripped safely (it may sit inside a
    quoted string), and dropping from a mid-line `#` could hide real code.
    Nothing executable can follow a leading `#`, so this cannot hide a defect.
    """
    body = "\n".join(_FENCED_BLOCK_RE.findall(content))
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def _pgrep_invocations(content: str) -> list[str]:
    code = _code_blocks(content)
    return [m.group("argv").strip() for m in _PGREP_INVOCATION_RE.finditer(code)]


def _short_flags(argv: str) -> set[str]:
    return {c for cluster in _SHORT_FLAG_RE.findall(argv) for c in cluster}


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_skill_docs_are_discoverable_and_invoke_pgrep() -> None:
    """Guard the guard. If the glob stops resolving, or the docs stop invoking
    pgrep at all, every assertion below would pass vacuously — which is the
    same silent-coverage failure this gate was written to end. Fail loudly."""
    docs = _skill_docs()
    assert docs, f"no skill docs found under {SKILLS_ROOT}"

    invocations = [
        argv
        for doc in docs
        for argv in _pgrep_invocations(doc.read_text(encoding="utf-8"))
    ]
    assert len(invocations) >= 3, (
        "expected at least 3 pgrep invocations across "
        f"{len(docs)} skill docs, found {len(invocations)}: {invocations}"
    )


# ---------------------------------------------------------------------------
# #3070 — the command-position enumeration itself, locked
# ---------------------------------------------------------------------------
#
# Every gate in this file is downstream of `_pgrep_invocations()`, so a
# position the enumeration omits is not one missed check but FOUR silently
# satisfied at once, and the suite goes green on a doc that plainly invokes
# pgrep. That is a false CLEAN — the precise failure these gates exist to
# prevent — and it is invisible in exactly the way an omission always is:
# nothing anywhere reports "this line was never examined".
#
# So the enumeration is pinned here rather than left to be re-derived. The six
# spellings measured in #3070 are marked; the rest come from walking the POSIX
# XCU 2.9.4 operator and compound-command slots, which are the positions — and
# the only ones — the pattern above claims to implement. The `cmd_prefix` and
# wrapper-prefix classes it does NOT claim are named at `_COMMAND_OPERATOR`,
# with the measurement and the reason widening is the wrong remedy. The next
# narrowing of `_COMMAND_POSITION` fails LOUDLY here instead of quietly
# widening the hole again.
_POSITION_FIXTURES = [
    # id, single fenced line. Each must yield EXACTLY ONE invocation whose
    # captured argv still runs to end of line.
    ("line_start", 'pgrep -af "engram-surface-daemon" || echo "not running"'),
    ("pipe", 'true | pgrep -af "engram-surface-daemon"'),
    ("or_list", 'true || pgrep -af "engram-surface-daemon"'),
    ("semicolon", 'true; pgrep -af "engram-surface-daemon"'),
    ("and_list", 'true && pgrep -af "engram-surface-daemon"'),
    ("background", 'sleep 1 & pgrep -af "engram-surface-daemon"'),
    ("command_subst", 'rc=$(pgrep -af "engram-surface-daemon")'),
    ("backtick_subst", 'rc=`pgrep -af "engram-surface-daemon"`'),
    # #3070's six measured spellings, verbatim.
    (
        "if_then_else",
        'if pgrep -af "engram-surface-daemon"; then echo up; '
        'else echo "daemon not running"; fi',
    ),
    ("while_do", 'while pgrep -af "engram-surface-daemon"; do echo spin; done'),
    ("until_do", 'until pgrep -af "engram-surface-daemon"; do echo wait; done'),
    ("elif_then", 'elif pgrep -af "engram-surface-daemon"; then echo up; fi'),
    ("bang_negation", '! pgrep -af "engram-surface-daemon" && echo "not running"'),
    ("subshell", '( pgrep -af "engram-surface-daemon" ) || echo "not running"'),
    # Remaining grammar slots, enumerated rather than observed.
    ("then", 'if true; then pgrep -af "engram-surface-daemon"; fi'),
    ("else", 'if false; then :; else pgrep -af "engram-surface-daemon"; fi'),
    ("do_for", 'for p in 1; do pgrep -af "engram-surface-daemon"; done'),
    ("brace_group", '{ pgrep -af "engram-surface-daemon"; }'),
    ("case_branch_body", 'case $1 in s) pgrep -af "engram-surface-daemon" ;; esac'),
    ("time", 'time pgrep -af "engram-surface-daemon"'),
    ("coproc", 'coproc pgrep -af "engram-surface-daemon"'),
]


@pytest.mark.parametrize(
    "line", [f[1] for f in _POSITION_FIXTURES], ids=[f[0] for f in _POSITION_FIXTURES]
)
def test_pgrep_invocation_is_seen_in_every_ENUMERATED_command_position(
    line: str,
) -> None:
    """#3070 — the position enumeration must be exhaustive over the operator
    and compound-command positions of POSIX XCU 2.9.4, not over the spellings
    someone happened to think of. ENUMERATED is in the name because that is the
    whole of the claim: the XCU 2.9.1 `cmd_prefix` position
    (`LC_ALL=C pgrep ...`, `>/dev/null pgrep ...`) and wrapper prefixes where
    pgrep is grammatically an argument (`sudo`, `env`, `nohup`, `timeout`,
    `command`, `eval`, `exec`, `xargs`, `time -p` — bare `time` IS covered) are
    NOT asserted here. #3070 measured all 11 returning zero. The wrapper set is
    open and so cannot be enumerated, which is why the claim is bounded instead
    of the pattern widened; see the note at `_COMMAND_OPERATOR`.

    Measured on the pre-fix pattern, which listed only start-of-line, `|`,
    `;`, `&`, `$(`, a backtick, `then`, `do` and `else`: the six `if` /
    `while` / `until` / `elif` / `!` / `( ... )` spellings below each returned
    ZERO invocations, so a planted doc containing all six gave 167 passed, 0
    failed. The same doc carrying only the start-of-line spelling gave 3
    failed — same corpus, same file, only the token's POSITION differing. The
    green was the enumeration's hole, not the doc's innocence."""
    doc = "```sh\n" + line + "\n```\n"
    invocations = _pgrep_invocations(doc)

    assert len(invocations) == 1, (
        "pgrep in command position was not seen as an invocation, so every "
        "gate downstream of _pgrep_invocations() passes vacuously on this "
        f"line (#3070):\n    {line}\n  found {len(invocations)}: {invocations}"
    )
    # The other half of what anchoring on position buys: once the token is
    # matched, its argv is read to END OF LINE, so no argv spelling can hide
    # from the flag and `|| echo` gates. A truncated argv would re-open that.
    assert "engram-surface-daemon" in invocations[0], (
        "argv did not run to end of line, so the flag gates would read a "
        f"truncated invocation:\n    {line}\n  captured: {invocations[0]!r}"
    )


# The negative direction, for the same reason the execution controls below run
# both ways: a pattern that matched every `pgrep` anywhere would satisfy the
# test above while making the gates fire on documentation. These are the
# non-invocations that must stay invisible — and `for` / `in` / `case` are here
# rather than in `_COMMAND_POSITION` precisely because their slot takes a name,
# a word list or a pattern instead of a command.
_NON_INVOCATION_FIXTURES = [
    # The shipped fixed form's own diagnostic: the token sits mid-line inside a
    # quoted string. This line is live in engram-health-exam/SKILL.md.
    (
        "quoted_diagnostic",
        '[ "$rc" -eq 1 ] && echo "not running" '
        '|| echo "could not check: pgrep exited $rc"',
    ),
    ("for_binds_a_name", 'for pgrep in a b; do echo "$pgrep"; done'),
    ("case_pattern_label", 'case $1 in pgrep) echo "that is a label" ;; esac'),
    ("longer_command_name", 'mypgrep -af "engram-surface-daemon"'),
    # The SUFFIX direction of the name check, in COMMAND POSITION — which is
    # what makes it discriminating where its predecessor was not. #3070's
    # review found the previous fixture here, `echo "$pgrep_out"`, unable to
    # report failure: the token was preceded by `$`, which the position anchor
    # already rejects, so the trailing `\b` of `pgrep\b` never got a vote and
    # the whole file stayed at 188 passed under the very mutation the fixture
    # was shaped to catch. Measured on this line instead (Darwin 25.6.0,
    # CPython 3.12.10): 0 invocations pristine, 1 with that `\b` dropped.
    ("suffixed_name_in_command_position", "pgrep_out=$(true)"),
]


@pytest.mark.parametrize(
    "line",
    [f[1] for f in _NON_INVOCATION_FIXTURES],
    ids=[f[0] for f in _NON_INVOCATION_FIXTURES],
)
def test_non_invocations_are_not_counted_as_pgrep_invocations(line: str) -> None:
    """Guards the false-ALARM direction of the #3070 widening. Widening the
    position enumeration must not turn prose, labels, variable names or a
    diagnostic string into invocations — a gate that fires on the doc's own
    explanation of itself is a gate people delete."""
    doc = "```sh\n" + line + "\n```\n"
    invocations = _pgrep_invocations(doc)

    assert invocations == [], (
        "a non-invocation was counted as a pgrep invocation, which makes the "
        f"gates fire on documentation:\n    {line}\n  captured: {invocations}"
    )


# ---------------------------------------------------------------------------
# #3070 — this gate must be SELECTED for every skill doc, not merely present
# ---------------------------------------------------------------------------
#
# Every assertion in this file is inert if tools/run_touched_tests.py never
# picks the file for a skill-doc change. Layer-2 selection through
# src/engram/packaging/test-map.json is FIRST-MATCH-WINS —
# `_resolve_convention_map()` `break`s on the first entry whose
# `path_prefix_or_glob` matches the changed path — so a narrow
# `skills/claude/<name>/` entry placed ABOVE the broad `skills/claude/` entry
# SHADOWS it and silently drops every gate the broad entry carries. The narrow
# entry's own test_globs run and go green; nothing anywhere reports that the
# broad entry was skipped. That is the same shape of false CLEAN as an omitted
# command position, one layer out in the build system.
#
# Both #3070 review rounds flagged that nothing asserted this. It is asserted
# CORPUS-DRIVEN rather than as a count of entries: the invariant is "whichever
# entry WINS for a skill doc runs this gate", which happens to be satisfied by
# two entries today (one for engram-loop/SKILL.md, one for the other 28) and
# must still be satisfied by a third narrow entry added tomorrow. A hardcoded
# `== 2` would pass for exactly the change that breaks it.
TEST_MAP_JSON = REPO_ROOT / "src" / "engram" / "packaging" / "test-map.json"
THIS_GATE = "tests/test_health_exam_skill.py"


def _shipped_path_candidates(changed_file: str) -> tuple[str, ...]:
    """Mirror of the same-named helper in tools/run_touched_tests.py.

    Map entries are written in the manifest's shipped-path convention
    (`skills/claude/...`), while a changed file is reported at its real path
    (`src/engram/skills/claude/...`), so the selector matches against the
    literal path AND its de-prefixed form. Mirrored rather than imported
    because tools/ is not importable as a package from the suite; the mirror is
    deliberately a transcription, so a future divergence in the selector shows
    up as this test asserting an invariant the selector no longer has, rather
    than as a subtly different matching rule invented here.
    """
    candidates = [changed_file]
    for prefix in ("src/engram/", "src/build/"):
        if changed_file.startswith(prefix):
            candidates.append(changed_file[len(prefix) :])
    if changed_file.startswith("src/build/"):
        candidates.append("tools/" + changed_file[len("src/build/") :])
    return tuple(candidates)


def _winning_map_entry(changed_file: str, mappings: list[dict]) -> dict | None:
    """The one entry the selector would use for *changed_file*: first, then stop.

    None when no entry matches at all. That case does NOT drop the gate — the
    selector's `unresolved` branch falls through to the full suite (`pytest
    tests/`), which includes this file — so it is not treated as a violation
    below; it simply contributes no winning entry to check.
    """
    candidates = _shipped_path_candidates(changed_file)
    for entry in mappings:
        pattern = entry.get("path_prefix_or_glob", "")
        if any(
            c.startswith(pattern) or fnmatch.fnmatch(c, pattern) for c in candidates
        ):
            return entry
    return None


def _entry_runs_this_gate(entry: dict) -> bool:
    """Whether *entry*, if it wins, actually runs this file.

    Two ways it can, both mirroring the selector: one of its `test_globs`
    expands onto this path (the selector globs them relative to the repo root,
    so a literal path and any glob covering it are the same thing to it), or it
    sets `full_suite_fallback`, which short-circuits to `pytest tests/` and
    therefore includes this file. Anything else drops the gate.
    """
    if entry.get("full_suite_fallback", False):
        return True
    for tglob in entry.get("test_globs", []):
        for match in glob.glob(str(REPO_ROOT / tglob)):
            if str(Path(match).relative_to(REPO_ROOT)) == THIS_GATE:
                return True
    return False


def test_every_winning_test_map_entry_for_a_skill_doc_runs_this_gate() -> None:
    """#3070 — first-match-wins makes a narrow map entry able to disable this
    whole file for the doc it covers, silently and with the suite green."""
    mappings = json.loads(TEST_MAP_JSON.read_text(encoding="utf-8")).get(
        "mappings", []
    )
    assert mappings, f"no mappings found in {TEST_MAP_JSON}"

    docs = _skill_docs()
    assert docs, f"no skill docs found under {SKILLS_ROOT}"

    winners: dict[str, list[str]] = {}
    offenders: dict[str, list[str]] = {}
    for doc in docs:
        entry = _winning_map_entry(_rel(doc), mappings)
        if entry is None:
            continue  # no map entry → full-suite fallback, see _winning_map_entry
        pattern = entry.get("path_prefix_or_glob", "")
        winners.setdefault(pattern, []).append(_rel(doc))
        if not _entry_runs_this_gate(entry):
            offenders.setdefault(pattern, []).append(_rel(doc))

    # Vacuity guard, for the same reason as
    # test_skill_docs_are_discoverable_and_invoke_pgrep: if the shipped-path
    # convention changes and nothing matches any more, the loop above would
    # find no offenders and pass while asserting nothing at all.
    assert winners, (
        "no test-map entry matches ANY skill doc, so this test would pass "
        "vacuously — either the shipped-path convention in test-map.json or "
        "the selector's matching rule in tools/run_touched_tests.py has moved "
        "away from what _shipped_path_candidates above mirrors (#3070)"
    )

    assert not offenders, (
        "test-map.json entry wins for a skill doc but does not run "
        f"{THIS_GATE} (#3070):\n"
        + "\n".join(
            f"    {pattern}  ->  {len(docs_for)} doc(s), e.g. {docs_for[0]}"
            for pattern, docs_for in sorted(offenders.items())
        )
        + "\n  Selection in tools/run_touched_tests.py is FIRST-MATCH-WINS: it "
        "stops at the first matching entry, so a narrow entry SHADOWS the "
        "broad `skills/claude/` one for the paths it covers and every gate "
        "listed only on the broad entry never runs for those paths — the suite "
        "stays green and nothing reports the omission. Add "
        f"{THIS_GATE} to the offending entry's test_globs (the broad entry's "
        "note says the same), or move the narrow entry below the broad one."
    )


@pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.parent.name)
def test_skill_doc_pgrep_is_always_uid_scoped(doc: Path) -> None:
    """#2132 defect 1 — an unscoped pgrep counts other seats' processes, so a
    check can report a healthy count with this seat's own process dead."""
    for argv in _pgrep_invocations(doc.read_text(encoding="utf-8")):
        assert "u" in _short_flags(argv), (
            f"{_rel(doc)}: pgrep invocation is not uid-scoped (#2132):\n"
            f"    pgrep {argv}\n"
            '  Use: pgrep -u "$(id -u)" -f "<pattern>"'
        )


@pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.parent.name)
def test_skill_doc_pgrep_never_uses_dash_a(doc: Path) -> None:
    """#2133 defect 2 — Darwin pgrep(1) excludes the current process and all
    its ancestors by default; `-a` puts the ancestors back, so the not-running
    branch stops being reachable. procps-ng `-a` is `--list-full` (formatting
    only). A flag that changes the match set on one platform and only the
    formatting on the other is never the right way to get full argv."""
    for argv in _pgrep_invocations(doc.read_text(encoding="utf-8")):
        assert "a" not in _short_flags(argv), (
            f"{_rel(doc)}: pgrep invocation uses -a/-af (#2133):\n"
            f"    pgrep {argv}\n"
            "  On Darwin -a re-admits ancestors to the match set. For the full "
            "command line, run `ps -o args=` on the returned pids instead."
        )


@pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.parent.name)
def test_skill_doc_pgrep_distinguishes_not_found_from_cannot_run(doc: Path) -> None:
    """#2132 defect 3 — `pgrep ... || echo "not running"` prints the same
    reassuring line for exit 1 (ran, found nothing) and exit >1 (could not run
    at all, e.g. 127 = not installed). Those are different claims."""
    offenders = [
        argv
        for argv in _pgrep_invocations(doc.read_text(encoding="utf-8"))
        if _OR_ECHO_RE.search(argv)
    ]

    assert not offenders, (
        f"{_rel(doc)}: bare `pgrep ... || echo ...` one-liner(s) (#2132):\n"
        + "\n".join(f"    pgrep {o}" for o in offenders)
        + "\n  Branch on the exit status so 'found nothing' (rc 1) and 'could "
        "not check' (rc > 1) are reported as the different claims they are."
    )


# ---------------------------------------------------------------------------
# #3032 — GNU-only regex escapes, re-asserted over the SKILL-DOC population
# ---------------------------------------------------------------------------
#
# Real BSD pgrep(1) compiles a POSIX ERE in which `\s \S \d \D \w \W \b \B` are
# NOT special. tests/test_pgrep_liveness_reap_patterns.py already refuses them
# — but only inside `simulate_pgrep_f`, which is reachable only from patterns
# fed by its _TOOLS / _HOOKS / _AUTOSTART constants. A skill doc is in none of
# those sets, so a doc could reintroduce `\s` into a pgrep pattern and the
# entire suite stayed green. Measured on the pre-fix gate: a planted skill doc
# carrying `pgrep -u "$(id -u)" -f 'engram-surface-daemon\.py\s+--foo'` gave
# 161 passed, 0 failed.
#
# That is the SAME artifact-type blindness this file's header argues against,
# reproduced one level down inside the gate written to end it — which is why it
# is fixed here rather than noted as a limitation.
_GNU_ONLY_ESCAPE_RE = re.compile(r"\\[sSdDwWbB]")

# A pattern is frequently bound to a shell variable before use — engram-upgrade
# ships `PAT='^[^ ]*[Pp]ython...'` on one line and `pgrep -u "$USER" -f "$PAT"`
# on the next — so an argv-only scan would read the literal text `"$PAT"`, find
# no backslash at all, and pass. Resolve one level of `VAR=<literal>` from the
# same doc's code before scanning.
#
# One level only, and names with no in-doc assignment (e.g. `$USER`, supplied
# by the environment) are simply skipped. Note the direction: this only ever
# ADDS text to the scan, so an unresolved name can cost a missed defect but can
# never manufacture a false positive.
_VAR_REF_RE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _assigned_literals(code: str, name: str) -> list[str]:
    rx = re.compile(
        rf"^[ \t]*{re.escape(name)}=(?P<val>'[^'\n]*'|\"[^\"\n]*\"|[^\s;|&]+)",
        re.M,
    )
    return [m.group("val") for m in rx.finditer(code)]


def _pgrep_pattern_text(code: str, argv: str) -> str:
    """The argv, plus the literal value of any same-doc variable it names."""
    parts = [argv]
    for m in _VAR_REF_RE.finditer(argv):
        parts.extend(_assigned_literals(code, m.group(1) or m.group(2)))
    return "\n".join(parts)


@pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.parent.name)
def test_skill_doc_pgrep_uses_no_gnu_only_escapes(doc: Path) -> None:
    """#3032 defect class — `\\s` and friends are GNU extensions. A real BSD
    pgrep treats `\\s` as a literal `s`, so the pattern silently matches
    nothing and the liveness check reports a confident, healthy-looking
    absence. Use POSIX bracket classes (`[[:space:]]`, `[^[:space:]]`)."""
    content = doc.read_text(encoding="utf-8")
    code = _code_blocks(content)

    for argv in _pgrep_invocations(content):
        offending = sorted(
            set(_GNU_ONLY_ESCAPE_RE.findall(_pgrep_pattern_text(code, argv)))
        )
        assert not offending, (
            f"{_rel(doc)}: pgrep pattern carries GNU-only regex escape(s) "
            f"{offending} that real BSD pgrep(1) does not treat as special "
            f"(#3032):\n"
            f"    pgrep {argv}\n"
            "  Substitute POSIX bracket classes: [[:space:]] for \\s, "
            "[^[:space:]] for \\S."
        )


# ---------------------------------------------------------------------------
# #3068 — shell loops must never bind a zsh-tied variable name
# ---------------------------------------------------------------------------
#
# engram-health-exam/SKILL.md Step 0 shipped `for path in tools hooks; do`.
# In zsh, `path` is a TIED ARRAY bound to $PATH, so the loop destroys PATH for
# the rest of the shell. Two silent consequences, both measured on a Darwin
# seat (zsh 5.9), each control varying exactly one axis:
#
#   - zsh + the shipped `for path in`  -> PATH becomes "hooks",
#     "command not found: python3", and the step STILL printed PASS.
#   - bash + the identical loop        -> correct. So it is zsh-specific.
#   - zsh + the loop renamed to `p`    -> correct. So it is name-specific.
#
# Consequence 1 is the same class as the `||` conflation above: the python3
# that resolved the symlink target sat inside `$(...)`, so when it died the
# substitution yielded empty, `echo` still succeeded, and Step 0 printed
# "PASS: ~/.engram/tools -> " with a BLANK target — the check reporting
# healthy while its own instrument was dead. Consequence 2 is that every later
# step in that shell (3, 6, 7 are python3/curl) simply fails.
#
# Uppercase `PATH` is included below because it clobbers in EVERY shell, not
# just zsh. The lowercase names are zsh's tied set.
#
# NOT GATED, and deliberately so: a bare assignment (`path=...`) outside a
# loop clobbers identically, but gating it inside code fences false-positives
# on commented-out and illustrative lines. Loops are what the sweep found and
# what this asserts.
_ZSH_TIED_NAMES = ("path", "PATH", "cdpath", "fpath", "manpath")
_TIED_LOOP_RE = re.compile(
    r"\bfor[ \t]+(?P<name>" + "|".join(_ZSH_TIED_NAMES) + r")\b[ \t]+in\b"
    r"|\bwhile[ \t]+read\b[^\n]*?[ \t](?P<read>"
    + "|".join(_ZSH_TIED_NAMES)
    + r")\b"
)


@pytest.mark.parametrize("doc", _skill_docs(), ids=lambda p: p.parent.name)
def test_skill_doc_shell_loops_never_bind_a_zsh_tied_name(doc: Path) -> None:
    """#3068 — `for path in ...` wipes $PATH on a zsh seat, and the check that
    follows reports PASS with a blank target rather than failing."""
    code = _code_blocks(doc.read_text(encoding="utf-8"))
    offenders = [m.group(0) for m in _TIED_LOOP_RE.finditer(code)]

    assert not offenders, (
        f"{_rel(doc)}: shell loop binds a zsh-tied variable name (#3068):\n"
        + "\n".join(f"    {o}" for o in offenders)
        + "\n  In zsh these names are tied to their uppercase scalars — binding "
        "`path` destroys $PATH for the rest of the shell, and every later step "
        "that shells out then fails while the check still prints PASS. "
        "Rename the loop variable (e.g. `p`)."
    )


# ---------------------------------------------------------------------------
# Execution-level controls: RUN the shipped snippets, don't just read them
# ---------------------------------------------------------------------------
#
# The text assertions above stop the defective idiom from returning. They do
# not show that the replacement actually reports the right thing, so the
# snippets are executed below — single-sourced from the doc at test time,
# never retyped, so a doc edit that breaks behaviour cannot pass by leaving
# the test's private copy intact.
#
# WHY BOTH DIRECTIONS ARE NEEDED, and this is the load-bearing point:
#
#   failure mode is a false CLEAN  -> plant an ABSENCE,  require it to FAIL
#   failure mode is a false ALARM  -> plant a PRESENCE,  require it to PASS
#
# A control of the form "can this check report absence?" is SATISFIED BY AN
# INSTRUMENT THAT IS ENTIRELY MISSING — a vanished `pgrep` produces the
# absence branch too. It varies the target, not the instrument. Only a planted
# PRESENCE distinguishes "ran and found nothing" from "could not run at all",
# because a destroyed PATH cannot fake a positive match.
#
# That is not hypothetical here: the two defects COMPOUND. With PATH destroyed
# by Step 0's `for path in` loop, `pgrep` is not found, the shell returns 127,
# and the old `|| echo "daemon not running"` fired — so on a zsh seat the exam
# printed a confident FALSE ALARM for a healthy system, and someone restarts a
# daemon that was never down. The `$?` split is what separates the three
# outcomes; these tests are what prove it separates them.
#
# Note on self-match avoidance: each snippet is written to a script FILE whose
# NAME contains no part of the search pattern, then run as `bash <file>`. The
# pattern therefore appears in no ancestor's argv on either platform — which
# matters because Darwin pgrep excludes ancestors by default and procps-ng
# does not, so embedding the pattern in a `-c` command line would self-match
# on Linux only, and the test would pass on the maintainer's Mac and fail in
# CI for a reason that has nothing to do with the code under test.

_BASH = shutil.which("bash")
_ZSH = shutil.which("zsh")
_SHELLS = [s for s in (_BASH, _ZSH) if s]


def _fenced_bash_blocks(doc: Path) -> list[str]:
    return re.findall(
        r"^```bash\n(.*?)^```$", doc.read_text(encoding="utf-8"), re.S | re.M
    )


def _block_containing(doc: Path, needle: str) -> str:
    matches = [b for b in _fenced_bash_blocks(doc) if needle in b]
    assert len(matches) == 1, (
        f"expected exactly 1 fenced bash block in {doc.name} containing "
        f"{needle!r}, found {len(matches)} — the doc moved, so this test is "
        "no longer single-sourced from the thing it claims to exercise"
    )
    return matches[0]


def _run_snippet(
    shell: str, snippet: str, tmp_path: Path, env_overrides: dict | None = None
) -> subprocess.CompletedProcess:
    """Run a snippet from a script FILE (see self-match note above)."""
    script = tmp_path / "runner.sh"  # name deliberately shares nothing with patterns
    script.write_text(snippet, encoding="utf-8")
    env = dict(os.environ)
    # Hermetic against a seat that exports ENGRAM_HOME (#3863): the snippets
    # resolve the home as ${ENGRAM_HOME:-$HOME/.engram}, and these tests plant
    # their fake home through HOME unless they override ENGRAM_HOME themselves.
    env.pop("ENGRAM_HOME", None)
    env.update(env_overrides or {})
    return subprocess.run(
        [shell, str(script)], capture_output=True, text=True, timeout=60, env=env
    )


# --- Step 4's pgrep liveness check -----------------------------------------


def _pgrep_snippet(pattern: str) -> str:
    # Anchored on Step 4's own comment line, which is stable across the fix:
    # it is present both in the pre-#2132 one-liner and in the replacement.
    # Anchoring on something only the FIXED form contains (e.g. `ps -o args=`)
    # would make a reverted fix fail these tests by "block not found" instead
    # of by producing the wrong ANSWER — which is the thing under test.
    block = _block_containing(HEALTH_EXAM_SKILL, "# Check the daemon process:")
    assert block.count('"engram-surface-daemon"') == 1, (
        "expected exactly one occurrence of the search pattern to substitute"
    )
    return block.replace('"engram-surface-daemon"', f'"{pattern}"')


@pytest.mark.skipif(not _SHELLS, reason="no bash or zsh available")
@pytest.mark.parametrize("shell", _SHELLS, ids=lambda s: Path(s).name)
def test_pgrep_snippet_reports_not_running_for_a_planted_ABSENCE(
    shell: str, tmp_path: Path
) -> None:
    """ABSENCE control — guards the false-CLEAN direction."""
    absent = f"zzz-absent-{uuid.uuid4().hex}"
    r = _run_snippet(shell, _pgrep_snippet(absent), tmp_path)

    assert "daemon not running" in r.stdout, (shell, r.stdout, r.stderr)
    assert "could not check" not in r.stdout, (
        "reported could-not-check when pgrep was perfectly available",
        r.stdout,
    )


@pytest.mark.skipif(not _SHELLS, reason="no bash or zsh available")
@pytest.mark.parametrize("shell", _SHELLS, ids=lambda s: Path(s).name)
def test_pgrep_snippet_finds_a_planted_PRESENCE(shell: str, tmp_path: Path) -> None:
    """PRESENCE control — guards the false-ALARM direction, and is the ONLY
    control that can tell a working check apart from a missing instrument."""
    token = f"zzz-present-{uuid.uuid4().hex}"
    target = tmp_path / f"{token}.sh"  # token rides in the TARGET's argv
    target.write_text("sleep 60\n", encoding="utf-8")

    proc = subprocess.Popen(
        [_BASH or shell, str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Give the kernel a moment to publish the new process's argv.
        for _ in range(50):
            probe = subprocess.run(
                ["pgrep", "-u", str(os.getuid()), "-f", token],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0:
                break
            subprocess.run(["sleep", "0.1"])
        else:
            pytest.skip("could not plant a detectable process on this host")

        r = _run_snippet(shell, _pgrep_snippet(token), tmp_path)
    finally:
        proc.terminate()  # a pid WE captured — never a process-table search
        proc.wait(timeout=10)

    assert str(proc.pid) in r.stdout, (
        "the planted process was running, but the shipped check did not "
        "report its pid — this is the false-ALARM direction",
        r.stdout,
        r.stderr,
    )
    assert "daemon not running" not in r.stdout, (shell, r.stdout)
    assert "could not check" not in r.stdout, (shell, r.stdout)


@pytest.mark.skipif(not _SHELLS, reason="no bash or zsh available")
@pytest.mark.parametrize("shell", _SHELLS, ids=lambda s: Path(s).name)
def test_pgrep_snippet_says_could_not_check_when_the_INSTRUMENT_is_gone(
    shell: str, tmp_path: Path
) -> None:
    """INSTRUMENT control — the case the two defects compound into. With PATH
    destroyed, pgrep exits 127; the old `||` form printed "daemon not running"
    and a healthy system looked dead. It must now say could-not-check."""
    shim = tmp_path / "empty-bin"  # ahead of PATH, containing no pgrep
    shim.mkdir()
    absent = f"zzz-absent-{uuid.uuid4().hex}"

    r = _run_snippet(
        shell, _pgrep_snippet(absent), tmp_path, env_overrides={"PATH": str(shim)}
    )

    assert "could not check" in r.stdout, (
        "pgrep was unavailable and the check did not say so",
        r.stdout,
        r.stderr,
    )
    assert "daemon not running" not in r.stdout, (
        "a missing instrument was reported as a genuine absence — this is the "
        "false-ALARM that #2132 defect 3 exists to prevent",
        r.stdout,
    )


# --- Step 0's symlink-resolution check (#3068) ------------------------------


def _step0_snippet() -> str:
    # Anchored on the plugin-tree line, which is unchanged by the #3068 fix.
    # Anchoring on `for p in` (fixed-form-only) would make a reverted rename
    # fail by "block not found" rather than by destroying $PATH — see the same
    # reasoning at _pgrep_snippet above.
    return _block_containing(
        HEALTH_EXAM_SKILL,
        'test -d "${ENGRAM_HOME:-$HOME/.engram}"/marketplace/plugins/engram',
    )


def _fake_engram_home(tmp_path: Path, *, symlink: bool, present: bool = True) -> Path:
    """Plant a fake $HOME in one of three states the doc must tell apart.

    present=True,  symlink=True   -> healthy compatibility symlink
    present=True,  symlink=False  -> a real directory pinned to old content
    present=False                 -> the compatibility path does not exist

    The third state had NO fixture before #3070, which is why nothing caught
    Step 0 calling an absent path "a real directory, not a symlink" and sending
    its reader to the pinned-scatter remediation for a path that is simply not
    there. A fixture set that omits a state the code branches on cannot report
    a wrong answer for that state — the same shape of blind spot as scoping an
    inventory by artifact type.
    """
    home = tmp_path / "home"
    plugin = home / ".engram" / "marketplace" / "plugins" / "engram"
    plugin.mkdir(parents=True)
    for name in ("tools", "hooks"):
        (plugin / name).mkdir()
        if not present:
            continue  # deliberately leave ~/.engram/<name> uncreated
        link = home / ".engram" / name
        if symlink:
            link.symlink_to(plugin / name)
        else:
            link.mkdir()
    return home


@pytest.mark.skipif(not _SHELLS, reason="no bash or zsh available")
@pytest.mark.parametrize("shell", _SHELLS, ids=lambda s: Path(s).name)
def test_step0_PASSES_on_a_planted_healthy_symlink(
    shell: str, tmp_path: Path
) -> None:
    """PRESENCE control — guards the false-ALARM direction for Step 0."""
    home = _fake_engram_home(tmp_path, symlink=True)
    r = _run_snippet(shell, _step0_snippet(), tmp_path, {"HOME": str(home)})

    assert "FAIL:" not in r.stdout, (shell, r.stdout, r.stderr)
    assert r.stdout.count("PASS:") == 2, (shell, r.stdout)
    assert "NOTE:" not in r.stdout, ("real symlinks must not be flagged", r.stdout)
    # The resolved target must actually be printed — a blank one is the #3068
    # failure signature and "PASS: ... -> " would otherwise satisfy the above.
    for line in r.stdout.splitlines():
        if line.startswith("PASS:"):
            assert line.rstrip().endswith(("tools", "hooks")), (
                "PASS printed with a blank/garbled target",
                line,
            )


@pytest.mark.skipif(not _SHELLS, reason="no bash or zsh available")
@pytest.mark.parametrize("shell", _SHELLS, ids=lambda s: Path(s).name)
def test_step0_follows_ENGRAM_HOME_not_the_default_home(
    shell: str, tmp_path: Path
) -> None:
    """#3863: with a custom ENGRAM_HOME the check must read THAT tree. The
    default $HOME/.engram is planted as an EMPTY decoy, so a Step 0 that
    hard-codes the default FAILs here instead of passing by luck."""
    custom = _fake_engram_home(tmp_path / "custom", symlink=True) / ".engram"
    decoy_home = tmp_path / "decoy"
    (decoy_home / ".engram").mkdir(parents=True)
    r = _run_snippet(
        shell,
        _step0_snippet(),
        tmp_path,
        {"HOME": str(decoy_home), "ENGRAM_HOME": str(custom)},
    )

    assert "FAIL:" not in r.stdout, (shell, r.stdout, r.stderr)
    assert r.stdout.count("PASS:") == 2, (shell, r.stdout, r.stderr)


@pytest.mark.skipif(not _SHELLS, reason="no bash or zsh available")
@pytest.mark.parametrize("shell", _SHELLS, ids=lambda s: Path(s).name)
def test_step0_FAILS_when_the_resolver_INSTRUMENT_is_gone(
    shell: str, tmp_path: Path
) -> None:
    """INSTRUMENT control — guards the false-CLEAN direction. The old block
    resolved inside `$(...)`, so a dead python3 yielded an empty substitution,
    `echo` still succeeded, and Step 0 printed `PASS: ~/.engram/tools -> `
    with a blank target: the check reporting healthy while its own instrument
    was dead."""
    home = _fake_engram_home(tmp_path, symlink=True)
    shim = tmp_path / "empty-bin"
    shim.mkdir()

    r = _run_snippet(
        shell, _step0_snippet(), tmp_path, {"HOME": str(home), "PATH": str(shim)}
    )

    assert r.stdout.count("FAIL:") == 2, (
        "the resolver was unavailable and Step 0 did not report FAIL",
        r.stdout,
        r.stderr,
    )
    assert "PASS:" not in r.stdout, (
        "printed PASS while its own resolver was dead",
        r.stdout,
    )


@pytest.mark.skipif(not _SHELLS, reason="no bash or zsh available")
@pytest.mark.parametrize("shell", _SHELLS, ids=lambda s: Path(s).name)
def test_step0_NOTES_a_real_directory_rather_than_a_symlink(
    shell: str, tmp_path: Path
) -> None:
    """A real directory pinned to pre-migration content passes `test -e`
    identically to a healthy symlink, so `test -e` alone cannot see it."""
    home = _fake_engram_home(tmp_path, symlink=False)
    r = _run_snippet(shell, _step0_snippet(), tmp_path, {"HOME": str(home)})

    assert r.stdout.count("NOTE:") == 2, (shell, r.stdout, r.stderr)


@pytest.mark.skipif(not _SHELLS, reason="no bash or zsh available")
@pytest.mark.parametrize("shell", _SHELLS, ids=lambda s: Path(s).name)
def test_step0_does_not_call_an_ABSENT_path_a_real_directory(
    shell: str, tmp_path: Path
) -> None:
    """ABSENCE control for the NOTE: line. `[ -L path ]` is false for a path
    that does not exist just as it is for a real directory, so the ungated
    `[ -L ... ] || echo "NOTE: ... is a real directory"` announced a confident,
    specific, WRONG diagnosis for an absent path — and pointed its reader at
    engram-upgrade's pinned-scatter remediation, which is the remediation for
    preserving a directory that is there. FAIL is still correct and must stay;
    only the NOTE must go."""
    home = _fake_engram_home(tmp_path, symlink=False, present=False)
    r = _run_snippet(shell, _step0_snippet(), tmp_path, {"HOME": str(home)})

    assert r.stdout.count("FAIL:") == 2, (
        "an absent compatibility path must still FAIL",
        r.stdout,
        r.stderr,
    )
    assert "NOTE:" not in r.stdout, (
        "an ABSENT path was reported as 'a real directory, not a symlink' — "
        "the reader is sent to the pinned-scatter remediation for a path that "
        "is not there",
        r.stdout,
    )
    assert "PASS:" not in r.stdout, (shell, r.stdout)


@pytest.mark.skipif(_ZSH is None, reason="zsh not available on this host")
def test_step0_does_not_destroy_PATH_under_zsh(tmp_path: Path) -> None:
    """#3068 itself. In zsh `path` is a tied array bound to $PATH, so
    `for path in tools hooks` wiped PATH for the rest of the shell — every
    later step (3, 6, 7 are python3/curl) then failed. Measured before the
    fix: PATH became the literal string "hooks"."""
    home = _fake_engram_home(tmp_path, symlink=True)
    snippet = _step0_snippet() + '\necho "PATH_AFTER=[$PATH]"\n'
    before = os.environ.get("PATH", "")

    r = _run_snippet(_ZSH, snippet, tmp_path, {"HOME": str(home)})

    assert f"PATH_AFTER=[{before}]" in r.stdout, (
        "Step 0 modified $PATH — a zsh-tied loop variable is back (#3068)",
        r.stdout,
        r.stderr,
    )
