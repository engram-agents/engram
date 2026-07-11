#!/usr/bin/env bash
# ghpr.sh — lightweight repo-local PR helper: bundle touched-tests + cost-log + push.
#
# WHY (Lei, 2026-06-12): the full pytest suite is main-only in CI (CI-economics,
# private repo = 2000 Actions-min/month, and CI fires on EVERY push so one PR ×
# N review rounds = N runs). Before committing budget to a GitHub PR-suite
# workflow, we SIMULATE the proposed gate LOCALLY on each PR push and LOG the
# cost, to get real metrics by end of day.
#
# What it does, on `push`:
#   1. diff the branch vs its base (default origin/dev)
#   2. run run_touched_tests.py on that diff, in an ISOLATED HOME (a live ~/.engram
#      contaminates ~180 tests with false failures — empty HOME avoids that),
#      timed.  Fail-open to the full suite is inherited from run_touched_tests
#      (conftest/pytest.ini/requirements/test-map changes -> full).
#   3. append a cost record to ~/.engram/ci-cost-sim/YYYY-MM-DD.jsonl
#   4. GATE: if tests failed, abort the push (override with --no-gate)
#   5. git push (--force-with-lease optional).  NOT merge — Lei merges via UI.
#
# Usage:
#   tools/ghpr.sh push [--base REF] [--full] [--force-with-lease]
#                      [--no-gate] [--no-push] [--dry-run] [-- PYTEST_ARGS...]
#   tools/ghpr.sh cost            # print today's cost log + day totals
#
# This is a Tier-3 (dev) tool; it never touches CI minutes — it's the local
# stand-in we measure against.
set -uo pipefail

# Resolve the real script path (handles symlink invocation from tools/ghpr.sh),
# then use git to find the repo root — robust regardless of script location.
_self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
REPO_ROOT="$(git -C "$(dirname "$_self")" rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO_ROOT" ] || { echo "ghpr: ERROR — could not resolve repo root via git" >&2; exit 1; }
LOG_DIR="${HOME}/.engram/ci-cost-sim"
# Resolve the test python BEFORE any HOME override (run_touched_tests runs pytest
# via sys.executable, so it must be the venv that actually has pytest). Per-agent
# venv via the real HOME; override with GHPR_PYTHON; fall back to system python3.
PYTHON="${GHPR_PYTHON:-${HOME}/.engram/venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
# Proxy constants for the "implied CI minutes" estimate (rough — local wall != GitHub
# runner wall, but the RATIO touched-vs-full is the decision-relevant signal):
CI_MATRIX_JOBS=2                 # tests.yml runs python 3.11 + 3.12
FULL_SUITE_WALL_SECONDS=235      # measured full-suite wall today (~3m55s)

die() { echo "ghpr: $*" >&2; exit 1; }

cmd_cost() {
  local f="${LOG_DIR}/$(date -u +%Y-%m-%d).jsonl"
  [ -f "$f" ] || { echo "no cost log for today ($f)"; return 0; }
  echo "=== ghpr cost log $(date -u +%Y-%m-%d) ==="
  cat "$f"
  echo "--- day totals ---"
  python3 - "$f" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
n = len(rows)
wall = sum(r.get("wall_seconds", 0) for r in rows)
ci_touched = sum(r.get("est_ci_min_touched", 0) for r in rows)
ci_full = sum(r.get("est_ci_min_full_baseline", 0) for r in rows)
fulls = sum(1 for r in rows if r.get("full_suite"))
fails = sum(1 for r in rows if r.get("result") == "fail")
print(f"pushes simulated : {n}  ({fulls} full-suite, {fails} test-fail)")
print(f"local wall total : {wall}s ({wall/60:.1f} min)")
print(f"est CI-min if touched-on-PR : {ci_touched:.1f}")
print(f"est CI-min if full-on-PR    : {ci_full:.1f}   (baseline we'd pay without selection)")
saved = ci_full - ci_touched
print(f"est CI-min saved by selection: {saved:.1f}  ({100*saved/ci_full:.0f}% reduction)" if ci_full else "")
PY
}

cmd_push() {
  local base="origin/dev" full="" fwl="" gate=1 dopush=1 dry="" pytest_args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --base) base="$2"; shift 2 ;;
      --full) full="--full"; shift ;;
      --force-with-lease) fwl="--force-with-lease"; shift ;;
      --no-gate) gate=0; shift ;;
      --no-push) dopush=0; shift ;;
      --dry-run) dry="--dry-run"; shift ;;
      --) shift; pytest_args=("$@"); break ;;
      *) die "unknown push arg: $1" ;;
    esac
  done

  cd "$REPO_ROOT"
  local branch base_branch
  branch="$(git rev-parse --abbrev-ref HEAD)"
  base_branch="${base#origin/}"
  [ "$branch" = "HEAD" ] && die "detached HEAD — checkout a branch first"
  [[ "$branch" =~ ^(dev|main)$ ]] && die "refusing to push from $branch directly"

  echo "==> fetching $base ..."
  git fetch origin "$base_branch" >/dev/null 2>&1 || die "git fetch origin $base_branch failed"

  # We test the COMMITTED tip (a clean worktree @ HEAD); warn on uncommitted changes.
  if [ -n "$(git status --porcelain | grep -v '^??' || true)" ]; then
    echo "==> WARNING: uncommitted tracked changes will be NEITHER tested NOR pushed"
    echo "    (the run uses a clean worktree at HEAD) — commit them first to include them."
  fi

  local changed
  changed="$(git diff --name-only "${base}...HEAD" 2>/dev/null | wc -l | tr -d ' ')"
  echo "==> ${changed} file(s) changed vs ${base}"

  # ---- run touched-tests against the COMMITTED tip, in a clean worktree +
  #      isolated HOME, timed.  The worktree means we test EXACTLY the commits we
  #      push (no untracked scratch files inflating the change-set to a full-suite
  #      fail-open, no dirty-tree drift); isolated HOME means the live ~/.engram
  #      can't shadow modules or leak runtime state into the run. --------------
  local wt="" clean_home="" out="" start end wall rc subset_line sel tot full_flag="false"
  # Clean up the worktree + tmp dirs on ANY exit (incl. a die before explicit cleanup).
  trap 'rm -rf "$clean_home" "$out" 2>/dev/null; [ -n "$wt" ] && { git worktree remove --force "$wt" 2>/dev/null; rm -rf "$wt" 2>/dev/null; }' EXIT
  wt="$(mktemp -d "${TMPDIR:-/tmp}/ghpr-wt.XXXX")"
  clean_home="$(mktemp -d "${TMPDIR:-/tmp}/ghpr-home.XXXX")"
  out="$(mktemp)"
  git worktree add --detach "$wt" HEAD >/dev/null 2>&1 || die "git worktree add failed"
  echo "==> running ${full:+FULL }touched-tests (clean worktree @ HEAD, isolated HOME)..."
  start="$(date +%s)"
  # NB: pass --repo-root explicitly. run_touched_tests' own repo-root self-resolve
  # (__file__.parent.parent.parent) is off-by-one for the post-restructure
  # src/engram/tools/ depth — it lands on .../src and pytest can't find tests/.
  # --repo-root takes precedence and sidesteps that (tracked separately).
  ( cd "$wt" && HOME="$clean_home" "$PYTHON" src/engram/tools/run_touched_tests.py \
       --repo-root "$wt" --base "$base" $full $dry ${pytest_args:+-- "${pytest_args[@]}"} ) >"$out" 2>&1
  rc=$?                                   # capture the subshell exit BEFORE any pipe (a pipe would mask it)
  end="$(date +%s)"; wall=$((end - start))
  git worktree remove --force "$wt" >/dev/null 2>&1 || true
  rm -rf "$clean_home"

  tail -25 "$out"
  # Parse "SUBSET green (N/TOTAL selected ...)" or full-suite signal (best-effort).
  subset_line="$(grep -iE 'SUBSET|full suite|selected' "$out" | tail -1 || true)"
  sel="$(printf '%s' "$subset_line" | grep -oE '[0-9]+/[0-9]+' | head -1 | cut -d/ -f1 || true)"
  tot="$(printf '%s' "$subset_line" | grep -oE '[0-9]+/[0-9]+' | head -1 | cut -d/ -f2 || true)"
  grep -qiE 'full.{0,6}suite' "$out" && full_flag="true"
  [ -n "$full" ] && full_flag="true"
  sel="${sel:-0}"; tot="${tot:-0}"

  # ---- cost record --------------------------------------------------------
  local result ci_touched ci_full
  [ "$rc" -eq 0 ] && result="pass" || result="fail"
  # est CI minutes = wall * matrix_jobs / 60 ; full baseline = full_wall * jobs / 60
  ci_touched="$(python3 -c "print(round(${wall}*${CI_MATRIX_JOBS}/60,2))")"
  ci_full="$(python3 -c "print(round(${FULL_SUITE_WALL_SECONDS}*${CI_MATRIX_JOBS}/60,2))")"
  mkdir -p "$LOG_DIR"
  local rec
  rec="$(python3 -c "import json,sys;print(json.dumps({
    'ts': sys.argv[1], 'branch': sys.argv[2], 'base': sys.argv[3],
    'changed_files': int(sys.argv[4]), 'tests_selected': int(sys.argv[5]),
    'tests_total': int(sys.argv[6]), 'full_suite': sys.argv[7]=='true',
    'wall_seconds': int(sys.argv[8]), 'result': sys.argv[9],
    'est_ci_min_touched': float(sys.argv[10]), 'est_ci_min_full_baseline': float(sys.argv[11]),
    'note': 'local sim; CI fires per push so multiply by review rounds'
  }))" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$branch" "$base" "$changed" "$sel" "$tot" \
      "$full_flag" "$wall" "$result" "$ci_touched" "$ci_full")"
  echo "$rec" >> "${LOG_DIR}/$(date -u +%Y-%m-%d).jsonl"
  rm -f "$out"

  echo "==> result=${result}  selected=${sel}/${tot}  full=${full_flag}  wall=${wall}s  (~${ci_touched} CI-min vs ~${ci_full} full-baseline)"

  # ---- gate + push --------------------------------------------------------
  if [ "$rc" -ne 0 ] && [ "$gate" -eq 1 ]; then
    die "touched-tests FAILED (rc=$rc) — push aborted. Fix, or re-run with --no-gate."
  fi
  if [ "$dopush" -eq 0 ] || [ -n "$dry" ]; then
    echo "==> --no-push/--dry-run: skipping git push (tested + logged only)"
    return 0
  fi
  echo "==> git push ${fwl} origin ${branch}"
  git push $fwl -u origin "$branch"
}

sub="${1:-}"; shift || true
case "$sub" in
  push) cmd_push "$@" ;;
  cost) cmd_cost ;;
  ""|-h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown subcommand: $sub (use: push | cost)" ;;
esac
