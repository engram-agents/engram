#!/usr/bin/env bash
# pre-dispatch-check.sh — pre-spec duplicate PR / in-flight baton check.
#
# Run this BEFORE drafting a fairy spec that names an issue number.  It guards
# against the PR-517 class of incident (2026-05-30): a coder-fairy dispatched
# for issue #486, producing a duplicate of the counterpart's already-converged
# PR-502.  The check would have caught it in under 5 seconds.
#
# Usage:
#   tools/pre-dispatch-check.sh <issue-number>
#
#   <issue-number>  bare integer OR with leading '#' (both accepted).
#
# Exit codes:
#   0  CLEAR — no open PR or in-flight baton references the issue
#   1  DUPLICATE RISK — at least one hit (open PR or baton file)
#   2  usage error — missing or invalid argument
#   3  UNKNOWN — gh unavailable; fall back to manual check
#
# Multi-agent mode: if /home/agents-shared/projects/ exists (or $BATON_PROJECTS_DIR),
# baton files are scanned for exact-token matches (#NN).  Dir absent → single-agent
# mode, baton check skipped.

set -u
# NOT -e: we capture gh failures explicitly rather than dying on them.

SCRIPT="pre-dispatch-check.sh"

# ---------------------------------------------------------------------------
# Argument parsing — strip leading '#', validate bare integer
# ---------------------------------------------------------------------------
if [ $# -eq 0 ]; then
    echo "Usage: $SCRIPT <issue-number>" >&2
    echo "  <issue-number>  bare integer or with leading '#'" >&2
    exit 2
fi

RAW_ARG="$1"
# Strip optional leading '#'
NN="${RAW_ARG#\#}"

# Validate: must be a non-empty sequence of digits
if [ -z "$NN" ] || ! printf '%s' "$NN" | grep -qE '^[0-9]+$'; then
    echo "$SCRIPT: invalid issue number: '$RAW_ARG' (expected bare integer)" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Check (a): open PR with #NN in title — exact-token match
# ---------------------------------------------------------------------------
# The prose version had a false-positive risk: '#100' would match a search for
# '#1006' because GitHub's in:title search is substring.  We fix it by
# post-filtering the returned JSON with a word-boundary-aware pattern:
# "#NN" not followed by an alphanumeric character or underscore.
GH_EXIT=0
# 2>&1 below intentionally merges stderr: a gh warning mixed into stdout will
# fail the JSON parse and surface as UNKNOWN (exit 3) — safe over false-CLEAR.
PR_JSON=$(gh pr list \
    --search "in:title \"#${NN}\"" \
    --state open \
    --json number,title,author \
    2>&1) || GH_EXIT=$?

if [ $GH_EXIT -ne 0 ]; then
    echo "$SCRIPT: UNKNOWN — gh unavailable (exit $GH_EXIT: $PR_JSON); fall back to manual check" >&2
    exit 3
fi

# Filter titles in Python: keep only PRs where the title contains "#NN" not
# immediately followed by an alphanumeric character or underscore.
# We write the Python to a temp file to avoid shell-quoting issues with
# f-strings and embedded quotes when using python3 -c.
_PY_TMP=$(mktemp /tmp/pre-dispatch-check-XXXXXX.py)
trap 'rm -f "$_PY_TMP"' EXIT

cat > "$_PY_TMP" << 'PYEOF'
import json, re, sys

nn = sys.argv[1]
pattern = re.compile(r'#' + re.escape(nn) + r'(?![0-9A-Za-z_])')

try:
    prs = json.loads(sys.stdin.read())
except ValueError:
    # Unparseable input (e.g. a gh warning merged into stdout) must surface
    # as UNKNOWN upstream — silently treating it as zero PRs would be a
    # false CLEAR, the one failure direction this gate must never have.
    sys.exit(4)

for pr in prs:
    title = pr.get('title', '')
    if pattern.search(title):
        author = pr.get('author') or {}
        login = author.get('login', 'unknown') if isinstance(author, dict) else str(author)
        num = pr.get('number', '?')
        print('  PR #' + str(num) + '  "' + title + '"  (author: ' + login + ')')
PYEOF

PY_EXIT=0
MATCHING_PRS=$(printf '%s' "$PR_JSON" | python3 "$_PY_TMP" "$NN") || PY_EXIT=$?
if [ $PY_EXIT -ne 0 ]; then
    echo "$SCRIPT: UNKNOWN — gh output was not parseable JSON (likely a gh warning mixed into stdout); fall back to manual check" >&2
    exit 3
fi

# ---------------------------------------------------------------------------
# Check (b): baton files — multi-agent mode only
# ---------------------------------------------------------------------------
BATON_HITS=""
BATON_NOTE=""
BATON_DIR="${BATON_PROJECTS_DIR:-/home/agents-shared/projects}"

if [ -d "$BATON_DIR" ]; then
    # grep for exact-token "#NN" not followed by an alphanumeric/underscore across *.md files
    BATON_HITS=$(
        grep -lE "#${NN}([^0-9A-Za-z_]|$)" "$BATON_DIR"/*.md 2>/dev/null \
        | while IFS= read -r f; do
            echo "  baton: $(basename "$f")"
          done
    )
else
    BATON_NOTE="single-agent: baton check skipped (${BATON_DIR} not found)"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
if [ -n "$MATCHING_PRS" ] || [ -n "$BATON_HITS" ]; then
    echo "DUPLICATE RISK — open PR or in-flight baton references #${NN}:"
    [ -n "$MATCHING_PRS" ] && printf '%s\n' "$MATCHING_PRS"
    [ -n "$BATON_HITS" ]   && printf '%s\n' "$BATON_HITS"
    echo ""
    echo "Review the hits above before dispatching a coder-fairy for #${NN}."
    exit 1
fi

echo "CLEAR — no open PR or in-flight baton references #${NN}"
if [ -n "$BATON_NOTE" ]; then
    echo "  ($BATON_NOTE)"
fi
exit 0
