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
#   0   ran to completion (regardless of how much/little is covered — this is
#       a finder, not a gate)
#   1   setup/environment error (missing venv, git failure, etc.)
#   2   the affected-tests run itself failed (red suite) — the diff-coverage
#       report is still produced against whatever coverage.xml resulted, same
#       "a red suite still yields its partial report" spirit as the nightly.
#
# TIER: dev (T3 — developer tooling only, tiers.json)

set -uo pipefail

# ---------------------------------------------------------------------------
# Repo root resolution — robust, not fixed parent count (same pattern as
# tools/run-all-tests.sh).
# ---------------------------------------------------------------------------
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_ROOT="$(git -C "$(dirname "$SCRIPT_PATH")" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
    echo "coverage-diff.sh: ERROR — could not resolve repo root via git" >&2
    exit 1
fi
cd "$REPO_ROOT" || {
    echo "coverage-diff.sh: ERROR — could not cd to repo root: $REPO_ROOT" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Python interpreter resolution — same override convention as
# tools/run-all-tests.sh (ENGRAM_PY env var, default ~/.engram/venv).
# ---------------------------------------------------------------------------
ENGRAM_PY="${ENGRAM_PY:-${HOME}/.engram/venv/bin/python}"

if [ ! -x "$ENGRAM_PY" ]; then
    echo "coverage-diff.sh: ERROR — python not found at: $ENGRAM_PY" >&2
    echo "  Set ENGRAM_PY=/path/to/python to override." >&2
    exit 1
fi

DIFF_COVER="$(dirname "$ENGRAM_PY")/diff-cover"
if [ ! -x "$DIFF_COVER" ]; then
    echo "coverage-diff.sh: ERROR — diff-cover not found at: $DIFF_COVER" >&2
    echo "  Install it into that venv: $ENGRAM_PY -m pip install diff-cover" >&2
    echo "  (it's in requirements/requirements-dev.txt)" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
BASE="origin/dev"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base)
            BASE="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,42p' "$SCRIPT_PATH" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "coverage-diff.sh: unknown option: $1" >&2
            echo "Usage: $0 [--base REF]" >&2
            exit 1
            ;;
    esac
done

COVERAGE_XML="$REPO_ROOT/coverage.xml"

# ---------------------------------------------------------------------------
# Step 1 — affected tests, with branch coverage, against $BASE.
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
echo "coverage-diff.sh: running affected tests vs $BASE with coverage..." >&2
SUITE_RC=0
SUITE_LOG="$(mktemp)"
trap 'rm -f "$SUITE_LOG"' EXIT
# stdout is piped through tee (to capture $SUITE_LOG for TESTS_RAN_DESC below)
# while stderr still goes straight to the terminal; CPython block-buffers
# stdout once it's not a tty, so without this the two streams arrive out of
# order for a human watching live. PYTHONUNBUFFERED=1 keeps stdout
# line-buffered so the interleaving stays real-time — captured content is
# unaffected either way.
PYTHONUNBUFFERED=1 "$ENGRAM_PY" tools/run_touched_tests.py --base "$BASE" -- \
    --cov=src --cov-branch \
    --cov-report="xml:${COVERAGE_XML}" --cov-report=term \
    | tee "$SUITE_LOG"
SUITE_RC="${PIPESTATUS[0]}"

if [ ! -f "$COVERAGE_XML" ]; then
    echo "coverage-diff.sh: ERROR — $COVERAGE_XML was not produced (the affected-tests run likely failed before pytest-cov could write it)" >&2
    exit 1
fi

# Which tests actually ran this invocation — captured from
# run_touched_tests.py's own honesty-line output (see its docstring's
# "Output honesty" section), so the caveat below can name the ACTUAL subset
# rather than a generic disclaimer. Falls back to a mechanism description if
# neither line is found (e.g. a red run that exited before printing either).
TESTS_RAN_DESC="$(grep -E '^SUBSET green|^\[run_touched_tests\] FULL MAIN SUITE' "$SUITE_LOG" | tail -1)"
if [ -z "$TESTS_RAN_DESC" ]; then
    TESTS_RAN_DESC="affected-tests selection via tools/run_touched_tests.py --base $BASE (selection heuristic — see the run output above for detail)"
fi

# ---------------------------------------------------------------------------
# Step 2 — diff-scoped report: uncovered lines within the CHANGED lines only,
# compared against $BASE. No --fail-under: this is a finder, not a gate.
# ---------------------------------------------------------------------------
echo "coverage-diff.sh: diff coverage vs $BASE (changed lines only)..." >&2
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

# ---------------------------------------------------------------------------
# Caveat — printed EVERY run, unconditionally, immediately after the report
# above (never only on a flag, never only in a comment/README). diff-cover's
# "Missing lines" is computed from THIS run's coverage.xml only, which came
# from the narrow affected-tests subset above (or the full suite, if that's
# what got selected) — NOT from the full suite by default. A changed line
# this run reports as "Missing" may still be genuinely covered by tests this
# invocation didn't select; run_touched_tests.py's own docstring says the
# full suite remains the convergence gate, i.e. this selection is an admitted
# heuristic, not a completeness guarantee. So: read "Missing lines" above as
# "not covered by the tests THIS RUN selected" — never as "uncovered".
# ---------------------------------------------------------------------------
cat <<CAVEAT

-----------------------------------------------------------------------------
CAVEAT: the "Missing lines" report above reflects coverage from the tests
THIS RUN selected — it is NOT a claim that those lines are genuinely
uncovered.
  Tests that ran this invocation: ${TESTS_RAN_DESC}
A line flagged above may still be exercised by tests this run did not
select or run (affected-tests selection is a heuristic, not a completeness
guarantee — see tools/run_touched_tests.py's docstring: "Full suite remains
the convergence gate"). Treat a flagged line as a lead to investigate, not
a verdict — confirm against the nightly's full-suite coverage report (the
authoritative source) before concluding a line is genuinely unspecified.
-----------------------------------------------------------------------------
CAVEAT

if [ "$SUITE_RC" -ne 0 ]; then
    echo "coverage-diff.sh: NOTE — the affected-tests run above was red (exit $SUITE_RC); the diff-coverage report above reflects a partial/failed run." >&2
    exit 2
fi

exit "$DIFF_RC"
