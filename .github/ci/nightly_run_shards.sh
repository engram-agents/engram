#!/usr/bin/env bash
# Run the nightly suite as N sequential pytest processes (#4420).
#
# One process cannot hold the whole suite: its footprint grows with the number of
# tests run and passed the runner's RAM long before the end. So the suite runs as
# N separate, fresh `pytest` processes, one after another (never in parallel: no
# xdist, no -n/--dist). Which files go to which process is decided by
# nightly_shards.py from pytest's own collection, so a new test file needs no edit
# here or in the workflow.
#
#   * Every shard runs, even after an earlier one failed. The script exits
#     non-zero if any shard did. (No `set -e`: the loop must continue.)
#   * Each shard runs under `timeout -k 60 <limit>`. rc 124 is reported as a
#     NAMED timed-out shard, counts as red, and the loop goes on: a hung shard must
#     not starve the later ones.
#   * Each shard writes its own coverage data file (COVERAGE_FILE=.coverage.<k>);
#     the workflow's combine step merges them. No --cov-append, no per-shard report.
#   * Output is tee'd to $NIGHTLY_OUTPUT_LOG (all shards concatenated: the notify
#     step prints its tail) and to a per-shard log. `set -o pipefail` is what lets
#     pytest's exit status, not tee's, decide a shard (umwelt#738 round 1).
#   * A sampler appends one `[sampler]` line a minute to $NIGHTLY_SAMPLER_LOG and
#     the script brackets each shard with `[shard k start]` / `[shard k end]` lines,
#     so nightly_shard_summary.py can print the peak memory of each shard.
#
# Environment (all but the count have defaults):
#   NIGHTLY_SHARD_COUNT           N, required (named once, in the workflow)
#   NIGHTLY_SHARD_TIMEOUT_MIN     per-shard limit in minutes (default 75)
#   NIGHTLY_SHARD_TIMEOUT_SECONDS if set, the limit in seconds instead (for tests)
#   NIGHTLY_COLLECT_FILE          a saved `--collect-only -qq` output (skips collection)
#   NIGHTLY_OUTPUT_LOG NIGHTLY_SHARD_DIR NIGHTLY_SAMPLER_LOG NIGHTLY_SAMPLE_SECS
#   NIGHTLY_SAMPLER_DISABLE=1     no sampler
#   NIGHTLY_PYTHON                python for the helper tools (default python3)
#
# bash 3.2-safe on purpose (the cells also run on a Mac with the stock bash).

# The shard processes' command lines look like `<python> <bin>/pytest tests/a.py ...`
# or `... pytest src/forum/tests/z.py ...`: the first argument may be under either
# root. `timeout ... pytest` (no `bin/`), the collect step and `tail` do not match.
PGREP_PATTERN='bin/pytest (src/forum/)?tests/'

if [ "${1:-}" = "--print-pgrep-pattern" ]; then
  printf '%s\n' "$PGREP_PATTERN"
  exit 0
fi

: "${NIGHTLY_SHARD_COUNT:?NIGHTLY_SHARD_COUNT must be set (the number of sequential pytest processes)}"
N="$NIGHTLY_SHARD_COUNT"
CI_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${NIGHTLY_PYTHON:-python3}"
OUT_LOG="${NIGHTLY_OUTPUT_LOG:-/tmp/nightly-output.log}"
DIR="${NIGHTLY_SHARD_DIR:-/tmp}"
SAMPLER_LOG="${NIGHTLY_SAMPLER_LOG:-/tmp/sampler.log}"
SAMPLE_SECS="${NIGHTLY_SAMPLE_SECS:-60}"
if [ -n "${NIGHTLY_SHARD_TIMEOUT_SECONDS:-}" ]; then
  LIMIT="${NIGHTLY_SHARD_TIMEOUT_SECONDS}s"
else
  LIMIT="${NIGHTLY_SHARD_TIMEOUT_MIN:-75}m"
fi

TIMEOUT_CMD=""
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD=timeout
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD=gtimeout
else
  echo "::error::no timeout command on PATH; refusing to run shards without a per-shard limit"
  exit 2
fi

: > "$OUT_LOG"
: > "$SAMPLER_LOG"

sampler() {
  while true; do
    PIDS="$(pgrep -f "$PGREP_PATTERN" 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
    RSS_KB=0
    if [ -n "$PIDS" ]; then
      RSS_KB="$(ps -o rss= -p "$PIDS" 2>/dev/null | awk '{s+=$1} END {print s+0}')"
    fi
    MEM="$(free -m 2>/dev/null | awk '/^Mem:/ {printf "used=%sM free=%sM avail=%sM ", $3, $4, $7} /^Swap:/ {printf "swap_used=%sM ", $3}')"
    LOAD="$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
    echo "[sampler] $(date -u +%H:%M:%SZ) ${MEM}pytest_rss=$((RSS_KB / 1024))M pids=${PIDS:-none} load=${LOAD:-n/a}" >> "$SAMPLER_LOG"
    sleep "$SAMPLE_SECS"
  done
}

SAMPLER_PID=""
TAIL_PID=""
if [ "${NIGHTLY_SAMPLER_DISABLE:-}" != "1" ]; then
  sampler >/dev/null 2>&1 &
  SAMPLER_PID=$!
  tail -n +1 -F "$SAMPLER_LOG" 2>/dev/null &
  TAIL_PID=$!
  trap 'kill $SAMPLER_PID $TAIL_PID 2>/dev/null || true' EXIT
fi

PLAN="$DIR/nightly-shard-plan.json"
if [ -n "${NIGHTLY_COLLECT_FILE:-}" ]; then
  "$PY" "$CI_DIR/nightly_shards.py" plan --n "$N" --out "$PLAN" --collect-file "$NIGHTLY_COLLECT_FILE"
else
  "$PY" "$CI_DIR/nightly_shards.py" plan --n "$N" --out "$PLAN"
fi
PLAN_RC=$?
if [ "$PLAN_RC" -ne 0 ]; then
  echo "::error::could not plan the shards (rc=$PLAN_RC); no test ran" | tee -a "$OUT_LOG"
  exit 1
fi
"$PY" "$CI_DIR/nightly_shards.py" table --plan "$PLAN" | tee -a "$OUT_LOG"

RED=""
k=1
while [ "$k" -le "$N" ]; do
  LIST="$("$PY" "$CI_DIR/nightly_shards.py" files --plan "$PLAN" --shard "$k")"
  FILES=()
  while IFS= read -r f; do
    if [ -n "$f" ]; then FILES+=("$f"); fi
  done <<< "$LIST"
  NFILES="${#FILES[@]}"
  SHARD_LOG="$DIR/nightly-shard-$k.log"
  : > "$SHARD_LOG"

  echo "[shard $k start] $(date -u +%H:%M:%SZ) files=$NFILES" >> "$SAMPLER_LOG"
  rc=0
  if [ "$NFILES" -eq 0 ]; then
    # A bare `pytest` would run the whole suite: never invoke it with no files.
    echo "shard $k of $N: no files, skipped" | tee -a "$OUT_LOG" "$SHARD_LOG"
  else
    echo "=== shard $k of $N: $NFILES files ===" | tee -a "$OUT_LOG" "$SHARD_LOG"
    (
      set -o pipefail
      COVERAGE_FILE=".coverage.$k" \
      NIGHTLY_RSS_OUT="$DIR/nightly-rss-shard-$k.md" \
      PYTHONPATH="$CI_DIR${PYTHONPATH:+:$PYTHONPATH}" \
      "$TIMEOUT_CMD" -k 60 "$LIMIT" pytest "${FILES[@]}" -v --tb=short \
        -o tmp_path_retention_policy=failed \
        -m "not timing" \
        --cov=src --cov-branch \
        -p nightly_rss_plugin \
        2>&1 | tee -a "$OUT_LOG" "$SHARD_LOG"
    ) || rc=$?
  fi
  echo "[shard $k end] $(date -u +%H:%M:%SZ) rc=$rc" >> "$SAMPLER_LOG"

  if [ "$rc" -eq 124 ]; then
    echo "::error::shard $k TIMED OUT after $LIMIT (rc=$rc)" | tee -a "$OUT_LOG"
    RED="$RED shard-$k(timed-out)"
  elif [ "$rc" -eq 137 ]; then
    # SIGKILL: either timeout's -k escalation after a hang, or the kernel's OOM
    # killer (the very failure this split exists to avoid). The status cannot say
    # which, so the line must not claim a timeout.
    echo "::error::shard $k was KILLED (rc=137: SIGKILL from the timeout -k escalation or the out-of-memory killer)" | tee -a "$OUT_LOG"
    RED="$RED shard-$k(killed)"
  elif [ "$rc" -ne 0 ]; then
    echo "::error::shard $k failed (rc=$rc)" | tee -a "$OUT_LOG"
    RED="$RED shard-$k(rc=$rc)"
  fi
  k=$((k + 1))
done

if [ -n "$RED" ]; then
  echo "RED shards:$RED" | tee -a "$OUT_LOG"
  exit 1
fi
echo "all $N shards green" | tee -a "$OUT_LOG"
exit 0
