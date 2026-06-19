#!/usr/bin/env bash
# run-all-tests.sh — unified test runner for the two-suite, two-venv split.
#
# WHY TWO SUITES / TWO VENVS
# ---------------------------
# engram-alpha has two independent deployables:
#   1. The ENGRAM plugin — tests in tests/, run with the engram venv
#      (~/.engram/venv) which has sqlite-vec, anthropic, etc. Scope = `tests/`
#      to mirror CI's tests.yml exactly (NOT full discovery — see the ENGRAM
#      SUITE block for why over-collection of docs/archive produces false-RED).
#   2. The forum service (src/forum/) — tests in src/forum/tests/ and
#      tools/test_forum_cli.py, run with the forum venv
#      (/home/agents-shared/forum/.venv) which has flask, bs4, etc.
#
# Running forum tests in the engram venv produces env-skew false failures
# (missing deps, wrong versions).  run_touched_tests.py already prints a
# NOTE warning that forum/tests/ is excluded and must be run separately —
# this script IS that "run them separately" solution.
#
# USAGE
# -----
#   tools/run-all-tests.sh                  # run both suites
#   tools/run-all-tests.sh --engram-only    # engram suite only
#   tools/run-all-tests.sh --forum-only     # forum suite only
#   tools/run-all-tests.sh -- -x -v         # pass pytest flags to both suites
#   tools/run-all-tests.sh --engram-only -- -k test_foo
#
# EXIT CODES
#   0   both (or the selected) suite(s) passed
#   1   one or more suites failed
#
# TIER: dev (developer tooling only — not shipped in any install tier)

set -uo pipefail

# ---------------------------------------------------------------------------
# Repo root resolution — robust, not fixed parent count
# ---------------------------------------------------------------------------
# NOTE: readlink -f is GNU/Linux-only (BSD/macOS readlink lacks -f). Fine for
# this dev-tier, Linux-only repo; revisit if macOS support is ever needed.
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
# Resolve via git to handle symlink invocation from tools/ as well
REPO_ROOT="$(git -C "$(dirname "$SCRIPT_PATH")" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
    echo "run-all-tests.sh: ERROR — could not resolve repo root via git" >&2
    exit 1
fi

# Run from repo root so the suite paths below (src/forum/tests, tools/...) and
# pytest's pytest.ini-rooted discovery resolve regardless of the caller's CWD.
# REPO_ROOT is resolved robustly above (readlink + git rev-parse); honor it.
cd "$REPO_ROOT" || {
    echo "run-all-tests.sh: ERROR — could not cd to repo root: $REPO_ROOT" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Python interpreter resolution — captured BEFORE any HOME override (the
# isolated-HOME below must not change which python we resolve). Existence is
# checked AFTER arg parsing, gated per suite, so `--engram-only` does not error
# on a missing forum venv it never uses.
# ---------------------------------------------------------------------------
ENGRAM_PY="${ENGRAM_PY:-${HOME}/.engram/venv/bin/python}"
FORUM_PY="${FORUM_PY:-/home/agents-shared/forum/.venv/bin/python}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
RUN_ENGRAM=1
RUN_FORUM=1
PASSTHROUGH_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --engram-only)
            RUN_ENGRAM=1
            RUN_FORUM=0
            shift
            ;;
        --forum-only)
            RUN_ENGRAM=0
            RUN_FORUM=1
            shift
            ;;
        --)
            shift
            PASSTHROUGH_ARGS=("$@")
            break
            ;;
        *)
            echo "run-all-tests.sh: unknown option: $1" >&2
            echo "Usage: $0 [--engram-only|--forum-only] [-- PYTEST_ARGS...]" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Venv existence checks — gated per selected suite (only error on a venv we
# will actually use).
# ---------------------------------------------------------------------------
if [ "$RUN_ENGRAM" -eq 1 ] && [ ! -x "$ENGRAM_PY" ]; then
    echo "run-all-tests.sh: ERROR — engram venv python not found at: $ENGRAM_PY" >&2
    echo "  Set ENGRAM_PY=/path/to/python to override." >&2
    exit 1
fi

if [ "$RUN_FORUM" -eq 1 ] && [ ! -x "$FORUM_PY" ]; then
    echo "run-all-tests.sh: ERROR — forum venv python not found at: $FORUM_PY" >&2
    echo "  Set FORUM_PY=/path/to/python to override." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Isolated HOME (empty temp dir) — prevents live ~/.engram runtime-state
# contamination during test runs (e.g. a test that touches the DB or
# session files should not stomp on the real ENGRAM install).
# ---------------------------------------------------------------------------
ISOLATED_HOME="$(mktemp -d /tmp/run-all-tests-home-XXXXXX)"

cleanup() {
    rm -rf "$ISOLATED_HOME"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Suite 1: engram suite
# ---------------------------------------------------------------------------
rc_engram=0
if [ "$RUN_ENGRAM" -eq 1 ]; then
    echo "================================================================"
    echo "  ENGRAM SUITE  ($ENGRAM_PY)"
    echo "  targets: tests/   (mirrors CI tests.yml engram scope)"
    echo "================================================================"
    # Explicit `tests/` — NOT full discovery. CI (.github/workflows/tests.yml)
    # runs `pytest tests/ src/forum/tests/`, so full discovery would over-collect
    # archived/orphan tests (e.g. docs/archive/) that CI never sees, producing
    # false-RED that doesn't reflect CI's verdict. ENGRAM_NO_EMBEDDINGS=1 matches
    # CI's env (skips the optional sentence-transformers path). The result is the
    # load-bearing property: this suite green <=> CI's engram suite green.
    HOME="$ISOLATED_HOME" ENGRAM_NO_EMBEDDINGS=1 "$ENGRAM_PY" -m pytest \
        tests/ \
        "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}" \
        || rc_engram=$?
fi

# ---------------------------------------------------------------------------
# Suite 2: forum suite
# ---------------------------------------------------------------------------
rc_forum=0
if [ "$RUN_FORUM" -eq 1 ]; then
    echo "================================================================"
    echo "  FORUM SUITE   ($FORUM_PY)"
    echo "  targets: src/forum/tests  tools/test_forum_cli.py"
    echo "================================================================"
    # src/forum/tests mirrors CI; tools/test_forum_cli.py is a DELIBERATE superset
    # — CI omits it only pending #616, but it covers real forum-CLI behaviour, so
    # a local health-check should run it. Both in the forum venv (flask/bs4/etc).
    HOME="$ISOLATED_HOME" ENGRAM_NO_EMBEDDINGS=1 "$FORUM_PY" -m pytest \
        src/forum/tests \
        tools/test_forum_cli.py \
        "${PASSTHROUGH_ARGS[@]+"${PASSTHROUGH_ARGS[@]}"}" \
        || rc_forum=$?
fi

# ---------------------------------------------------------------------------
# Combined report
# ---------------------------------------------------------------------------
echo ""
echo "================================================================"
echo "  OVERALL RESULTS"
echo "================================================================"

if [ "$RUN_ENGRAM" -eq 1 ]; then
    if [ "$rc_engram" -eq 0 ]; then
        echo "  ENGRAM suite : PASSED  (rc=0)"
    else
        echo "  ENGRAM suite : FAILED  (rc=$rc_engram)"
    fi
fi

if [ "$RUN_FORUM" -eq 1 ]; then
    if [ "$rc_forum" -eq 0 ]; then
        echo "  FORUM suite  : PASSED  (rc=0)"
    else
        echo "  FORUM suite  : FAILED  (rc=$rc_forum)"
    fi
fi

if [ "$rc_engram" -ne 0 ] || [ "$rc_forum" -ne 0 ]; then
    echo ""
    echo "  OVERALL: FAILED"
    echo "================================================================"
    exit 1
else
    echo ""
    echo "  OVERALL: PASSED"
    echo "================================================================"
    exit 0
fi
