#!/usr/bin/env bash
# Read-only drift check: is the deployed forum app tree current relative to
# src/forum/ at a given git ref?
#
# The forum's deployed app tree (<service-dir>/app/forum/) is a hand-synced
# snapshot written by install-forum-service.sh, NOT a git checkout — nothing
# keeps it current automatically. This script answers "is it stale?" as a
# measurement any seat can take, instead of a discovery made only when a
# missing feature 404s in production (issue #2824).
#
# This tool has NO side effects — it only reads --src (via git, at the given
# ref — never the working tree) and --service-dir, and prints a report. It
# never writes, restarts, or redeploys anything. To fix a STALE result, run
# install-forum-service.sh (see src/forum/deploy/README.md).
#
# Options:
#   --service-dir PATH  shared dir for the deployed forum (DB + code + venv).
#                       Default: /home/agents-shared/forum
#   --src PATH          engram-alpha checkout containing src/forum/.
#                       Default: auto-detect via `git rev-parse --show-toplevel`
#                       from this script's own location (i.e. the checkout
#                       this script itself lives in). Falls back to requiring
#                       the flag if that fails, or if the detected checkout
#                       doesn't contain src/forum/.
#   --ref REF           git ref to compare src/forum/ against. Default: HEAD
#                       (the checkout's current commit) — NOT origin/dev.
#                       "Current" depends on the caller's purpose (comparing
#                       against a local branch tip vs. the upstream default
#                       branch are different questions); pass --ref explicitly
#                       for the latter, e.g. --ref origin/dev.
#   --verbose           print a unified diff for each DIFFERS file, not just
#                       its byte/line-count delta. Default: no diff bodies.
#
# Exit codes: 0 = CURRENT (deployed tree matches src/forum/ at --ref).
#             1 = STALE (one or more files differ/missing).
#             2 = usage / setup error (bad args, checkout not found, etc.).
#
# Excludes non-runtime source (tests/, deploy/, fairy-spec-*.md, spec.md,
# README.md) and runtime-only state on the deployed side (forum.db, *.jsonl,
# __pycache__, .venv) — mirrors what install-forum-service.sh actually
# rsyncs into <service-dir>/app/forum/. See that script's RSYNC_EXCLUDES.
#
# Part of #2824.
set -euo pipefail

SERVICE_DIR="/home/agents-shared/forum"
SRC=""
REF="HEAD"
VERBOSE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --service-dir)  SERVICE_DIR="$2"; shift 2 ;;
    --src)          SRC="$2"; shift 2 ;;
    --ref)          REF="$2"; shift 2 ;;
    --verbose)      VERBOSE=1; shift ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve --src: explicit flag wins; otherwise auto-detect from this script's
# own location (it ships inside src/forum/deploy/ of the checkout it checks
# by default).
# ---------------------------------------------------------------------------
if [ -z "$SRC" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  AUTO_SRC="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$AUTO_SRC" ] && [ -d "$AUTO_SRC/src/forum" ]; then
    SRC="$AUTO_SRC"
    echo "==> --src not given; auto-detected engram-alpha checkout at $SRC" >&2
  else
    echo "ERROR: --src PATH required (could not auto-detect an engram-alpha checkout containing src/forum/ from this script's own location: $SCRIPT_DIR)" >&2
    exit 2
  fi
fi

[ -d "$SRC/src/forum" ] || {
  echo "ERROR: $SRC/src/forum not found -- is --src an engram-alpha checkout?" >&2
  exit 2
}
git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "ERROR: $SRC is not a git repository -- --src must be a git checkout (drift is measured against a ref, not the working tree)" >&2
  exit 2
}
git -C "$SRC" rev-parse --verify "${REF}^{commit}" >/dev/null 2>&1 || {
  echo "ERROR: ref '$REF' does not resolve in $SRC" >&2
  exit 2
}

DEPLOYED="$SERVICE_DIR/app/forum"
[ -d "$DEPLOYED" ] || {
  echo "ERROR: $DEPLOYED not found -- is --service-dir correct, and has the forum ever been deployed there?" >&2
  exit 2
}
# Explicit readability check, BEFORE enumeration. Without this, a permission-
# denied `cd` inside the enumeration's process substitution below fails
# silently under `set -e` (a failure inside `<(...)` does not propagate to
# the parent shell -- a known bash gotcha, not a pipefail case) and the
# deployed side enumerates as empty, producing a false "STALE, N files
# missing" verdict instead of a clear permission error. Caught live against
# the real /home/agents-shared/forum/app/forum, whose group ownership predates
# this script and denies read to any account outside agent-ariadne's own
# private group -- exactly the scenario this check exists to name honestly.
if [ ! -r "$DEPLOYED" ] || [ ! -x "$DEPLOYED" ]; then
  echo "ERROR: cannot read $DEPLOYED (permission denied) -- this account may not be in the group that owns the deployed tree. Fix the directory's group ownership/permissions, or run this check as the forum admin." >&2
  exit 2
fi

RESOLVED_REF="$(git -C "$SRC" rev-parse "$REF")"
echo "==> Comparing $SRC/src/forum/ @ $REF ($RESOLVED_REF) against $DEPLOYED"

# ---------------------------------------------------------------------------
# Exclusion policy — mirrors install-forum-service.sh's RSYNC_EXCLUDES
# (--exclude='__pycache__' --exclude='*.pyc' --exclude='tests/'
#  --exclude='deploy/' --exclude='fairy-spec-*.md' --exclude='spec.md'
#  --exclude='README.md'), which match at any depth since rsync excludes
# without a leading slash are basename/any-level matches. Plus a defensive
# set for deployed-side runtime state that should never live inside
# app/forum/ but is excluded here too in case --service-dir points somewhere
# unexpected: forum.db, *.jsonl, .venv.
# ---------------------------------------------------------------------------
_is_excluded() {
  local rel="$1"
  local base
  base="$(basename "$rel")"
  case "$rel" in
    tests/*|*/tests/*)   return 0 ;;
    deploy/*|*/deploy/*) return 0 ;;
    .venv|.venv/*|*/.venv/*) return 0 ;;
  esac
  case "$base" in
    __pycache__)          return 0 ;;
    *.pyc)                return 0 ;;
    fairy-spec-*.md)      return 0 ;;
    spec.md)              return 0 ;;
    README.md)            return 0 ;;
    forum.db)             return 0 ;;
    forum-audit.jsonl)    return 0 ;;
    *.jsonl)              return 0 ;;
  esac
  return 1
}

# ---------------------------------------------------------------------------
# Enumerate the source side: files tracked at $REF under src/forum/, ref-pinned
# (git ls-tree / git show — never the working tree, so a stale local checkout
# cannot reproduce this same bug one level up).
# ---------------------------------------------------------------------------
_SRC_ALL=()
while IFS= read -r _line; do _SRC_ALL+=("$_line"); done < <(git -C "$SRC" ls-tree -r --name-only "$REF" -- src/forum/ | sed 's#^src/forum/##')
SRC_FILES=()
for rel in "${_SRC_ALL[@]}"; do
  _is_excluded "$rel" || SRC_FILES+=("$rel")
done

# ---------------------------------------------------------------------------
# Enumerate the deployed side: files under $DEPLOYED, pruning __pycache__/.venv
# so we never even descend into them (perf + they may be unreadable).
# ---------------------------------------------------------------------------
_DEPLOYED_ALL=()
while IFS= read -r _line; do _DEPLOYED_ALL+=("$_line"); done < <(
  cd "$DEPLOYED" && find . \( -type d \( -name '__pycache__' -o -name '.venv' \) -prune \) -o -type f -print \
    | sed 's#^\./##' | sort
)
DEPLOYED_FILES=()
for rel in "${_DEPLOYED_ALL[@]}"; do
  _is_excluded "$rel" || DEPLOYED_FILES+=("$rel")
done

# ---------------------------------------------------------------------------
# Union of relpaths, sorted, deduped.
# ---------------------------------------------------------------------------
ALL_RELPATHS=()
while IFS= read -r _line; do ALL_RELPATHS+=("$_line"); done < <(printf '%s\n' "${SRC_FILES[@]}" "${DEPLOYED_FILES[@]}" | sort -u)

_in_array() {
  local needle="$1"; shift
  local x
  for x in "$@"; do [ "$x" = "$needle" ] && return 0; done
  return 1
}

TMPDIR_CMP="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_CMP"' EXIT

N_TOTAL=0
N_IDENTICAL=0
N_DIFFERS=0
N_MISSING_DEPLOYED=0
N_MISSING_SOURCE=0

for rel in "${ALL_RELPATHS[@]}"; do
  N_TOTAL=$((N_TOTAL + 1))
  in_src=0; in_dep=0
  _in_array "$rel" "${SRC_FILES[@]}" && in_src=1 || true
  _in_array "$rel" "${DEPLOYED_FILES[@]}" && in_dep=1 || true

  if [ "$in_src" -eq 1 ] && [ "$in_dep" -eq 0 ]; then
    N_MISSING_DEPLOYED=$((N_MISSING_DEPLOYED + 1))
    printf '%-22s %s\n' "MISSING IN DEPLOYED" "src/forum/$rel"
    continue
  fi

  if [ "$in_src" -eq 0 ] && [ "$in_dep" -eq 1 ]; then
    N_MISSING_SOURCE=$((N_MISSING_SOURCE + 1))
    printf '%-22s %s\n' "MISSING IN SOURCE" "app/forum/$rel"
    continue
  fi

  # Present on both sides: ref-pinned content compare.
  src_tmp="$TMPDIR_CMP/src_content"
  git -C "$SRC" show "$REF:src/forum/$rel" > "$src_tmp"
  dep_file="$DEPLOYED/$rel"

  if cmp -s "$src_tmp" "$dep_file"; then
    N_IDENTICAL=$((N_IDENTICAL + 1))
    printf '%-22s %s\n' "IDENTICAL" "src/forum/$rel"
  else
    N_DIFFERS=$((N_DIFFERS + 1))
    src_bytes=$(wc -c < "$src_tmp")
    dep_bytes=$(wc -c < "$dep_file")
    src_lines=$(wc -l < "$src_tmp")
    dep_lines=$(wc -l < "$dep_file")
    printf '%-22s %s   (ref: %s lines/%s bytes | deployed: %s lines/%s bytes)\n' \
      "DIFFERS" "src/forum/$rel" "$src_lines" "$src_bytes" "$dep_lines" "$dep_bytes"
    if [ "$VERBOSE" -eq 1 ]; then
      diff -u "$src_tmp" "$dep_file" | sed 's/^/    /' || true
    fi
  fi
  rm -f "$src_tmp"
done

N_STALE=$((N_DIFFERS + N_MISSING_DEPLOYED + N_MISSING_SOURCE))

echo "==> Summary: $N_TOTAL files compared"
echo "    identical:            $N_IDENTICAL"
echo "    differs:              $N_DIFFERS"
echo "    missing in deployed:  $N_MISSING_DEPLOYED"
echo "    missing in source:    $N_MISSING_SOURCE"

if [ "$N_STALE" -eq 0 ]; then
  echo "==> CURRENT"
  exit 0
else
  echo "==> STALE ($N_STALE files differ)"
  exit 1
fi
