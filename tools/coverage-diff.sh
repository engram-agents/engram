#!/usr/bin/env bash
# coverage-diff.sh — local diff-coverage helper (issue #2774).
#
# WHAT THIS IS
# ------------
# Coverage here is a SPEC-COMPLETENESS FINDER, not a quality metric: under the
# prescriptive-SPEC plan, production code no test executes is code whose
# behaviour is probably unspecified. This script reports coverage of ONLY the
# lines the CURRENT BRANCH changes vs a base ref (default origin/dev) — not a
# global percentage, which would be meaningless from a partial (affected-only)
# test run. Output is a plain list of uncovered changed lines (file:line).
#
# There is NO score, NO --fail-under, NO gate. This is a finder, not a build
# check — nothing here should produce a number anyone is tempted to optimise.
#
# HOW
# ---
# 1. Run the AFFECTED tests (via tools/run_touched_tests.py — the same
#    import-graph selection used for local PR iteration, per project
#    CLAUDE.md's "run affected-tests locally" convention) with branch
#    coverage enabled, producing coverage.xml.
# 2. Feed that coverage.xml to diff-cover, scoped to the diff against the
#    base ref, with branch coverage and uncovered-line reporting on.
#
# Per-PR *automatic* diff coverage in CI is explicitly NOT implemented here —
# see issue #2774: it would require running tests on dev PRs, which #1597
# deliberately removed on cost grounds, and reversing that is the
# maintainer's call, not something to slip into a tooling PR.
#
# USAGE
#   tools/coverage-diff.sh [--base REF]
#
# EXIT CODES
#   Advisory by contract (#4358): nonzero ONLY for a usage error, and that
#   code is 2. Everything else exits 0 and prints one plain line saying what
#   happened.
#   2   usage error: an unknown option; --base with no value; a --base ref
#       that does not resolve to a commit (checked before any test run); or
#       the selector's own refusal (its exit 2-6, read from a `--json`
#       classify call BEFORE any test run; its output is printed).
#   0   everything else, including:
#       - the selector named a full-suite trigger (its exit 7 on the classify
#         call): no tests ran,
#         so there is no coverage report and diff-cover is not called;
#       - the interpreter, coverage, pytest-cov or diff-cover is missing: the
#         line names the missing piece, the interpreter path and how it was
#         chosen (ENGRAM_PY or the default); nothing is run;
#       - the test run exited nonzero (from the run phase that is PYTEST's
#         code, never a refusal: 1 = tests failed, 2 = interrupted or a
#         collection error): the report is still produced from whatever
#         coverage.xml resulted, and says so;
#       - no coverage.xml was written after a run; or diff-cover itself failed
#         (its exit code is printed).
#   A report is always followed by a caveat: it reflects only the tests this
#   run selected, the full suite runs nightly, and code that runs as a
#   subprocess is not measured.
#
# TIER: dev (T3 — developer tooling only, tiers.json)

set -uo pipefail

say() { echo "coverage-diff.sh: $*"; }

# ---------------------------------------------------------------------------
# Repo root resolution — robust, not fixed parent count (same pattern as
# tools/run-all-tests.sh). Advisory contract: not being able to find the repo
# is "nothing to report", not a usage error, so it exits 0 with a line.
# ---------------------------------------------------------------------------
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(git -C "$(dirname "$SCRIPT_PATH")" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
    say "could not resolve the repo root via git; nothing was run."
    exit 0
fi
cd "$REPO_ROOT" || {
    say "could not cd to the repo root ($REPO_ROOT); nothing was run."
    exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing — the only place besides the base-ref check and the
# selector's own refusal that exits nonzero (2).
# ---------------------------------------------------------------------------
BASE="origin/dev"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base)
            if [ $# -lt 2 ] || [ -z "$2" ]; then
                echo "coverage-diff.sh: --base needs a value (a git ref)." >&2
                echo "Usage: $0 [--base REF]" >&2
                exit 2
            fi
            BASE="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,/^# TIER/p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "coverage-diff.sh: unknown option: $1" >&2
            echo "Usage: $0 [--base REF]" >&2
            exit 2
            ;;
    esac
done

if ! git rev-parse --verify --quiet "${BASE}^{commit}" >/dev/null 2>&1; then
    echo "coverage-diff.sh: --base $BASE does not resolve to a commit in this repo; nothing was run." >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Tool check — BEFORE running anything. A missing piece is an environment
# fact to report, not a failure of the PR: say what is missing, which
# interpreter was looked at and how it was chosen, and exit 0.
# ---------------------------------------------------------------------------
if [ -n "${ENGRAM_PY:-}" ]; then
    PY_HOW="ENGRAM_PY from the environment"
else
    PY_HOW="the default ~/.engram/venv/bin/python (ENGRAM_PY is not set)"
fi
ENGRAM_PY="${ENGRAM_PY:-${HOME}/.engram/venv/bin/python}"
DIFF_COVER="$(dirname "$ENGRAM_PY")/diff-cover"

MISSING=()
if [ ! -x "$ENGRAM_PY" ]; then
    MISSING+=("the python interpreter")
else
    "$ENGRAM_PY" -c 'import coverage' >/dev/null 2>&1 || MISSING+=("the python package coverage")
    "$ENGRAM_PY" -c 'import pytest_cov' >/dev/null 2>&1 || MISSING+=("the python package pytest_cov")
fi
[ -x "$DIFF_COVER" ] || MISSING+=("diff-cover at $DIFF_COVER")

if [ "${#MISSING[@]}" -gt 0 ]; then
    say "no coverage report: missing ${MISSING[*]}."
    say "  interpreter: $ENGRAM_PY, chosen via $PY_HOW."
    say "  Install: $ENGRAM_PY -m pip install coverage pytest-cov diff-cover (see requirements/requirements-dev.txt), or set ENGRAM_PY=/path/to/python."
    exit 0
fi

COVERAGE_XML="$REPO_ROOT/coverage.xml"

# ---------------------------------------------------------------------------
# Phase 1 — CLASSIFY, with no test run. `--json` makes the selector print its
# selection and exit with ITS OWN code only (it never starts pytest in that
# mode): 0 = a selection, 7 = full-suite trigger, 2-6 = its own refusal. The
# selector's stderr (the refusal text) goes straight to the terminal.
# ---------------------------------------------------------------------------
SELECTION_JSON="$(mktemp)"
SUITE_LOG="$(mktemp)"
trap 'rm -f "$SELECTION_JSON" "$SUITE_LOG"' EXIT
"$ENGRAM_PY" tools/run_touched_tests.py --base "$BASE" --json >"$SELECTION_JSON"
SELECT_RC=$?

# The selector's exit code decides what happened, never its wording.
if [ "$SELECT_RC" -eq 7 ]; then
    say "no tests ran because the selector named a full-suite trigger (exit 7), so there is no coverage report."
    exit 0
fi
if [ "$SELECT_RC" -ge 2 ] && [ "$SELECT_RC" -le 6 ]; then
    say "the selector refused (exit $SELECT_RC); its output is above."
    exit 2
fi
if [ "$SELECT_RC" -ne 0 ]; then
    say "no coverage report: the selector exited $SELECT_RC, which is not a code it documents; nothing was run."
    exit 0
fi

# ---------------------------------------------------------------------------
# Coverage flags for the Phase 2 run below (the two phases follow this note).
#
# ⚠ `--cov=src`, ONE root — NOT `--cov=src/engram --cov=src/forum`. Do not
# split this "for clarity"; it silently reintroduces a real, measured defect.
# Two roots make coverage.py's Cobertura XML writer emit BOTH
# src/engram/server.py and src/forum/server.py as filename="server.py" (both
# roots have a "." package containing a file of that name) — one silently
# overwrites the other's numbers in the coverage.xml this script feeds to
# diff-cover below (verified: reordering the two --cov flags doesn't change
# which one wins). A single `--cov=src` root gives distinct
# filename="engram/server.py" / filename="forum/server.py" entries instead.
#
# The single root's OWN failure mode is handled in `.coveragerc`
# (`[report] include_namespace_packages = True`, NOT optional): neither
# src/engram nor src/build has a top-level __init__.py, and without that
# setting coverage.py's "report never-imported files as a gap" walk silently
# drops both packages entirely under a combined `src` root. See
# `.coveragerc`'s own comment for the measured before/after. Same fix,
# same reasoning, as .github/workflows/nightly.yml's "Run full pytest suite"
# step — keep both in sync if this ever changes.
# ---------------------------------------------------------------------------
# Phase 2 — RUN the affected tests, with branch coverage, against $BASE. From
# here the exit code is PYTEST's, not the selector's: any nonzero is a note,
# never a usage error (2 here means interrupted or a collection error).
# ---------------------------------------------------------------------------
say "running affected tests vs $BASE with coverage..." >&2
# A coverage.xml left by an earlier run must not be mistaken for this run's.
rm -f "$COVERAGE_XML"
# stdout is piped through tee (to capture $SUITE_LOG for TESTS_RAN_DESC below)
# while stderr still goes straight to the terminal; PYTHONUNBUFFERED=1 keeps
# the two streams interleaved in real time for a human watching live.
PYTHONUNBUFFERED=1 "$ENGRAM_PY" tools/run_touched_tests.py --base "$BASE" -- \
    --cov=src --cov-branch \
    --cov-report="xml:${COVERAGE_XML}" --cov-report=term \
    | tee "$SUITE_LOG"
SUITE_RC="${PIPESTATUS[0]}"

if [ ! -f "$COVERAGE_XML" ]; then
    say "no coverage data was written ($COVERAGE_XML is absent after the run, test run exit $SUITE_RC)."
    exit 0
fi

if [ "$SUITE_RC" -ne 0 ]; then
    say "NOTE — the test run exited $SUITE_RC (pytest's code; 1 = tests failed, 2 = interrupted or a collection error); the report reflects that run."
fi

# Which tests ran this invocation — the selector's own SUBSET line, if it
# printed one, so the caveat names the ACTUAL subset.
TESTS_RAN_DESC="$(grep -E '^SUBSET green' "$SUITE_LOG" | tail -1)"
if [ -z "$TESTS_RAN_DESC" ]; then
    TESTS_RAN_DESC="see the selector's output above"
fi

# ---------------------------------------------------------------------------
# Step 2 — diff-scoped report: uncovered lines within the CHANGED lines only,
# compared against $BASE. No --fail-under: this is a finder, not a gate.
# ---------------------------------------------------------------------------
say "diff coverage vs $BASE (changed lines only)..." >&2
# NOTE: deliberately NOT --show-uncovered — that flag additionally dumps the
# full highlighted SOURCE of every uncovered line, which is far more than the
# "uncovered changed lines as file:line" this tool is asked for (#2774). The
# default report already gives exactly that: one line per changed file with
# its uncovered line numbers (e.g. "engram_core.py (0.0%): Missing lines
# 12,45-50"). No --fail-under anywhere here — finder, not a gate.
"$DIFF_COVER" "$COVERAGE_XML" \
    --compare-branch="$BASE" \
    --branch-coverage
DIFF_RC=$?

if [ "$DIFF_RC" -ne 0 ]; then
    say "diff-cover failed (exit $DIFF_RC); no report."
    exit 0
fi

# ---------------------------------------------------------------------------
# Caveat — printed EVERY time a report is printed, right after it. Read
# "Missing lines" above as "not covered by the tests THIS RUN selected",
# never as "uncovered".
# ---------------------------------------------------------------------------
cat <<CAVEAT

-----------------------------------------------------------------------------
CAVEAT: the "Missing lines" report above reflects only the tests THIS run selected.
It is NOT a claim that those lines are genuinely uncovered.
  Tests that ran this invocation: ${TESTS_RAN_DESC}
- A flagged line may be exercised by tests this run did not select.
- The full suite runs nightly on dev and at the release cut.
- Code that runs as a subprocess (hooks, shell scripts, tools a test starts as a process) is not measured, because subprocess coverage is not configured, so such changed lines can read as missing even when a test runs them.
Treat a flagged line as a lead to investigate, not a verdict.
-----------------------------------------------------------------------------
CAVEAT

exit 0
