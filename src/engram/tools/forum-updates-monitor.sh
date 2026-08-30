#!/usr/bin/env bash
# forum-updates-monitor.sh — persistent Monitor for new dm + baton wake-events
# from the unified /api/updates coordination feed (UCS Slice B).
#
# Replaces the local-FS collab-letter-monitor (inter-agent letters now live in
# the forum as DMs) AND adds real-time baton-turn wake (the prompt-hook only
# surfaces in-court batons at prompt time, not in real time). 100% LAN API — no
# local-filesystem watch — so it works on any machine, not just the 5090 (the
# UCS pure-API invariant; sibling to forum-mention-monitor.sh).
#
# Arm via the Monitor tool with persistent: true (session-length watch).
# Stop with TaskStop when the loop ends.
#
# Usage:
#   forum-updates-monitor.sh [--purpose <key>] [--kinds dm,baton]
#
# Multi-agent hosts may symlink this into /home/agents-shared/bin/ alongside
# ia/baton/forum (same convention; symlinking is a manual host-operator action).
#
# Discipline notes (load-bearing — do not remove; mirror forum-mention-monitor):
#   - resolves forum base URL from config, same precedence as the forum CLI:
#     config.json forum.url > $FORUM_URL > localhost:5002.
#   - CURSOR model (not seen-set): /api/updates is a (since, as_of] cursor feed —
#     the SERVER does the diffing. We persist the last as_of and poll
#     since=<cursor>; the feed never replays already-seen seqs. Seed = baseline to
#     the current as_of on the first successful poll (NO history replay on arm).
#   - never-advance-cursor on failed poll: curl --fail returns non-zero on
#     forum-unreachable; skip the cycle and PRESERVE the cursor. Advancing past a
#     window we never emitted would silently drop those wakes.
#   - "failed poll" is BROADER than curl (#2225). curl --fail only rejects HTTP
#     >= 400, so a 200 maintenance page / truncated body reaches as_of() intact;
#     and a body that parses perfectly well can still carry no watermark (`{}`,
#     a missing or null as_of, a JSON array). as_of() exits non-zero for BOTH,
#     because the question it answers is "did I obtain a usable watermark", not
#     the narrower "did the body parse" — and every caller treats that exactly
#     like a non-zero curl. Both call sites want that same predicate, for
#     different reasons: the POLL has the -ge monotonicity guard and merely
#     declines the write, while the SEED has no guard to lean on, so a missing
#     watermark there baselines at 0 and replays the entire backlog on the very
#     next poll. NOTE 0 IS A VALID WATERMARK — a fresh forum genuinely serves
#     as_of: 0 — so the test is presence-and-coercibility, NEVER truthiness.
#   - engaged-suppression (#1077 part 2): while last-user-activity is within the
#     engaged window (default 360s), skip the emit cycle entirely AND do not
#     advance the cursor — events accumulate in (cursor, as_of] and emit once the
#     window expires.

set -u
# NOT -e: a transient failure must not kill a session-length watch.

# ---------------------------------------------------------------------------
# argument parsing: --help, --purpose <key>, --kinds <csv>
# ---------------------------------------------------------------------------
PURPOSE="default"
KINDS="${FORUM_UPDATES_KINDS:-dm,baton}"
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            cat <<'EOF'
forum-updates-monitor.sh — watch the LAN coordination feed for new dm + baton events

Usage: forum-updates-monitor.sh [--purpose <key>] [--kinds dm,baton]

  --purpose <key>  Purpose key for this instance (default: "default").
                   Duplicate-instance DETECTION (not reap — see #1933) is
                   purpose-scoped, with one asymmetry (matches
                   forum-mention-monitor): a NAMED --purpose only flags
                   same-named instances as suspects, so two different named
                   purposes (e.g. a smoke test) never cross-flag. The DEFAULT
                   purpose (no --purpose) flags ALL instances incl. named
                   ones. This monitor never kills a suspected duplicate — it
                   prints a warning naming the pid to stdout (the Monitor
                   event stream) and lets the agent decide whether to `kill
                   <pid>` by hand.
  --kinds <csv>    Comma-separated update kinds to watch (default: dm,baton).

Resolves SELF and forum BASE URL from ~/.engram/config.json.
URL resolution order: config.json forum.url > $FORUM_URL > http://localhost:5002

Cursor-based on GET /api/updates (since,as_of] — baselines to the current as_of
on first successful poll (no history replay on arm), never advances the cursor
on a failed poll. Emits one line per new dm / baton event. Polls every 30s.
Arm via Monitor tool with persistent: true; stop with TaskStop.
EOF
            exit 0
            ;;
        --purpose)
            PURPOSE="${2:?--purpose requires a value}"
            shift 2
            ;;
        --kinds)
            KINDS="${2:?--kinds requires a value}"
            shift 2
            ;;
        *)
            echo "forum-updates-monitor.sh: unrecognised argument: $1" >&2
            exit 1
            ;;
    esac
done

# Validate purpose key: only [a-zA-Z0-9_-] safe in pgrep -f regex (#1307).
case "$PURPOSE" in
    *[!a-zA-Z0-9_-]*)
        echo "forum-updates-monitor: --purpose key must contain only [a-zA-Z0-9_-]; got: '$PURPOSE'" >&2
        exit 1
        ;;
esac
# Validate kinds: comma-separated [a-z] tokens only (goes into a URL query).
case "$KINDS" in
    *[!a-z,]*)
        echo "forum-updates-monitor: --kinds must be comma-separated lowercase tokens; got: '$KINDS'" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# resolve identity + forum URL (same precedence as forum-mention-monitor)
# ---------------------------------------------------------------------------
SELF="$(python3 -c 'import json,os,urllib.parse;print(urllib.parse.quote(json.load(open(os.path.expanduser("~/.engram/config.json"))).get("agent_name",""),safe=""))')"
BASE="$(python3 -c 'import json,os
c=json.load(open(os.path.expanduser("~/.engram/config.json")))
print(((c.get("forum") or {}).get("url") or os.environ.get("FORUM_URL") or "http://localhost:5002").rstrip("/"))')"
URL_BASE="$BASE/api/updates?agent=$SELF&kinds=$KINDS"

# ---------------------------------------------------------------------------
# duplicate-instance detection: WARN, never kill (#1933). A persistent
# Monitor's bash process outlives its arming session; without SOME check for
# a still-running prior instance, a post-compaction re-arm would run
# ALONGSIDE the orphan and double-deliver every wake — that risk is real and
# still worth flagging. But the old fix (SIGTERM every pgrep -f match)
# assumed pgrep's unanchored substring match over the WHOLE cmdline could
# never hit anything except a genuine prior monitor. It can: `agentctl
# --charge-file` appends the boot charge to claude's own argv, #1865 put this
# script's path INSIDE that charge text, and claude sits as this monitor's
# GRANDPARENT — outside the $$/$PPID exemptions below — so every
# charge-booted session was reaping (killing) itself four days running. A
# substring match must never be an execution decision (Lei's ruling, #1933):
# this block now only WARNS on stdout — the Monitor event stream, so the
# warning reaches the agent as a notification — and leaves every matched
# process alone. A true duplicate left alive double-delivers every event; the
# agent inspects the flagged pid and kills it by hand if it really is stale.
#
# Detection is two-layered so the warning is worth reading: pgrep -f finds
# loose CANDIDATES first (same substring pattern as the old reap — cheap, but
# exactly the over-match above), then each candidate is re-verified against
# /proc/<pid>/cmdline, requiring argv[0] (leading `-` stripped, since a
# login-shell invocation like `exec -a "-bash" bash <script>` sets argv[0] to
# "-bash") to be a bash-family interpreter AND a later argv token to BE the
# script (exact match, or a ".../<script>" path suffix) — not merely present
# anywhere in the cmdline. Mirrors umwelt-bell.py's _is_bell_process (umwelt
# #205 — the same bug class: "matched editors/grep/review-shells/wrappers
# whose cmdline merely contained the filename"). $$/$PPID exemptions are kept
# as a cheap second layer on top of the identity check, not the only guard.
#
# Verification has THREE outcomes, not two — mirrors umwelt-bell.py's
# proc_cmdline()'s ok/gone/unknown split, which the very first port of this
# collapsed into a silent no-match. A process that vanishes mid-read (ENOENT/
# ESRCH — the file or pid is simply gone) is correctly silent: it lost the
# race, not a duplicate. But any OTHER read failure (e.g. EACCES — /proc read
# denied) means verification could NOT be completed, which is a DIFFERENT
# claim than "verified not a match" and must not be silently folded into it —
# say so loudly so the agent knows this candidate needs a manual look.
#
# Portability (#2134): /proc does not exist on macOS, so the whole
# verification pass above used to raise ENOENT on the FIRST open() and land
# in the "process gone" branch — dead and SILENT, byte-identical to "checked,
# found no duplicates." Mirrors umwelt-bell.py's `_is_bell_process` fix
# (umwelt #205): when /proc is absent, fall back to the portable
# `ps -p <pid> -o args=`. That fallback is WEAKER than the /proc read and is
# reported as such: `ps -o args=` returns a space-JOINED string, not true
# NUL-separated argv, so a wrapper whose script path sits inside a single
# quoted argument (e.g. `bash -c "... forum-updates-monitor.sh ..."`) can
# manufacture a false token boundary and over-match — measured on Linux
# (issue #2134 comments) against a `bash -c` wrapper. Kept anyway because a
# weaker-but-labeled detector beats no detection at all on the one platform
# that has no /proc to fall back on. rc 3 (below) marks this weaker verdict
# distinctly from rc 0's /proc-verified one so the two warning classes don't
# get conflated into a single alarm-fatigue channel.
#
# Set $FORUM_UPDATES_NO_REAP to any non-empty value to disable (now disables
# the detection/warning pass, not a kill).
# ---------------------------------------------------------------------------
_check_forum_updates_dup() {
    # $1=pid $2=script-basename. Delegates to the shared _proc_identity.py
    # (#2321 -- extracted from a 1629-byte block that used to be
    # byte-identical, hand-synced, in this file and
    # forum-mention-monitor.sh). See that module's own docstring for the
    # full exit-code table and the /proc-vs-ps-fallback reasoning; this
    # wrapper's only job is finding the sibling script and forwarding argv.
    local _self _proc_identity
    _self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
    _proc_identity="$(dirname "$_self")/_proc_identity.py"
    python3 "$_proc_identity" "$1" "$2" 2>/dev/null
}

if [ -z "${FORUM_UPDATES_NO_REAP:-}" ] && command -v pgrep >/dev/null 2>&1; then
    # Asymmetry (matches forum-mention-monitor): the DEFAULT purpose matches
    # the bare script name → candidates include ALL instances incl. named
    # ones. A NAMED purpose matches script + that --purpose only → two
    # different named purposes never cross-flag, but a default arm still
    # considers them candidates. Doc'd in --help.
    #
    # #2029 inventory answers: could this candidate pattern match the
    # invoking process (a boot charge quoting this script's path, #1933)?
    # YES, at the pgrep-candidate stage -- mitigated the same way as
    # forum-mention-monitor.sh: -u "$(id -u)" excludes any co-tenant on a
    # shared host from ever being a candidate, every candidate is
    # re-verified against /proc/<pid>/cmdline before being reported
    # (_check_forum_updates_dup), and the action is warn-not-kill
    # regardless. See tests/test_pgrep_liveness_reap_patterns.py and
    # tests/test_forum_monitor_duplicate_warn.py.
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='forum-updates-monitor\.sh'
    else
        _pgrep_pattern="forum-updates-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        _out="$(_check_forum_updates_dup "$_pid" "forum-updates-monitor.sh")"
        _rc=$?
        if [ "$_rc" -eq 0 ]; then
            echo "⚠️  forum-updates-monitor.sh: possible duplicate instance — pid $_pid cmdline: ${_out}"
            echo "⚠️  forum-updates-monitor.sh: if pid $_pid is a stale duplicate, inspect it and \`kill $_pid\` by hand — a live duplicate double-delivers every event."
        elif [ "$_rc" -eq 3 ]; then
            echo "⚠️  forum-updates-monitor.sh: possible duplicate instance (WEAK match — no /proc on this platform, verified via \`ps\` string-split only; may false-positive on wrapper processes, #2134) — pid $_pid cmdline: ${_out}"
            echo "⚠️  forum-updates-monitor.sh: if pid $_pid is a stale duplicate, inspect it and \`kill $_pid\` by hand — a live duplicate double-delivers every event."
        elif [ "$_rc" -eq 2 ]; then
            echo "⚠️  forum-updates-monitor.sh: could not verify candidate pid $_pid (${_out}) — NOT auto-flagged as a duplicate; check it by hand if you suspect a stale instance."
        fi
        # rc == 1: not a match (incl. "process gone mid-read") — silent.
    done
fi

# ---------------------------------------------------------------------------
# cursor file — keyed to the polled URL so concurrent instances never share
# state. Override with $FORUM_UPDATES_CURSOR_FILE (tests).
# ---------------------------------------------------------------------------
CURSOR="${FORUM_UPDATES_CURSOR_FILE:-$HOME/.engram/.forum-updates-cursor.$(printf '%s' "$URL_BASE" | cksum | cut -d' ' -f1)}"

# helper: read /api/updates JSON on stdin, print one wake line per update.
# Python uses ONLY double-quoted strings so the whole program can sit inside
# bash single-quotes with no nested-quote escaping (the fragile '"'"' dance).
emit() {
    python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for u in d.get("updates", []):
    k = u.get("kind")
    if k == "dm":
        body = (u.get("body") or "").replace("\n", " ").strip()
        if len(body) > 90:
            body = body[:90] + "…"
        print("✉️  new DM from " + str(u.get("sender", "?")) + ": " + body)
    elif k == "baton":
        title = (u.get("title") or "").strip()
        if len(title) > 50:
            title = title[:50] + "…"
        print("\U0001f3be baton " + str(u.get("project_id", "?")) + " → turn: " + str(u.get("turn", "?")) + " (" + title + ")")
    else:
        print("\U0001f514 new " + str(k) + " update (seq " + str(u.get("seq", "?")) + ")")
'
}

# helper: read /api/updates JSON on stdin, print the served as_of watermark.
#
# EXIT STATUS answers "did I obtain a usable watermark?" (#2225) — deliberately
# NOT the narrower "did the body parse?". A sentinel VALUE could collide with
# real data, and the layer above already uses exit status for the same class of
# question (curl --fail).
#   - exit 0, print the int: "as_of" is present and is a NON-NEGATIVE,
#     NON-BOOLEAN, int-coercible value. That INCLUDES A GENUINE 0 — a fresh
#     forum with nothing in the feed yet serves as_of: 0, and that is a
#     perfectly good baseline. NEVER test this value for truthiness: a
#     `-gt 0` guard would spin the seed forever on a healthy empty feed. What
#     must be rejected is the ABSENCE of a watermark, not a watermark that
#     happens to be zero.
#     The bool and negative rungs are not pedantry, they are the same site-3
#     flood by other doors. `bool` IS AN `int` SUBCLASS in Python, so a JSON
#     `"as_of": false` passes an `is None` check AND an `int()` call and
#     arrives as 0 — seeding the baseline at 0 and replaying the entire
#     backlog on the next poll. A negative watermark does the same by a
#     different route: per src/forum/coordination/updates.py, a negative
#     `since` "simply admits everything <= as_of", so it is a full replay
#     wearing a plausible number.
#     The 2**63-1 ceiling is the same rule, not a new one: that is EXACTLY the
#     domain the consumer — bash's `[ -ge ]` in the poll loop below — can
#     evaluate, and a watermark our own comparison cannot evaluate is not a
#     usable watermark. Measured: `[ 9223372036854775807 -ge 0 ]` succeeds,
#     `[ 9223372036854775808 -ge 0 ]` exits 2. Without this rung a body like
#     `{"as_of": 1e29}` goes PERMANENTLY DEAF WITH NO ERROR ANYWHERE: the seed
#     has no numeric test so it writes the 29-digit value; the poll's
#     `[ <big> -ge $_cur ]` exits 2 and that failure is swallowed by the
#     existing `2>/dev/null`; and once seeded, every later REAL watermark
#     fails the same comparison against it, so the cursor never recovers.
#   - exit non-zero, print nothing: EITHER the body did not parse (200
#     maintenance page, truncated JSON, empty string) OR it parsed and carries
#     no usable watermark (a JSON array, a missing "as_of", a null one, a
#     non-coercible one). Both mean the same thing to a caller — we did not
#     learn where this window ends — so neither may move state.
#
# Folding those two into one predicate is the point. The SEED has no
# monotonicity guard to fall back on, so baselining 0 from a body that never
# said 0 makes the next poll ask since=0 and replay the whole backlog. On the
# POLL path the -ge guard already declined every one of these bodies, so
# skipping the write there is behaviourally what already happened.
as_of() {
    python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(d, dict) or d.get("as_of") is None:
    sys.exit(1)
v = d["as_of"]
if isinstance(v, bool):
    sys.exit(1)
try:
    v = int(v)
except Exception:
    sys.exit(1)
if v < 0:
    sys.exit(1)
if v > 2**63 - 1:
    sys.exit(1)
print(v)'
}

# Poll cadence override — tests only (mirrors forum-mention-monitor.sh's
# $FORUM_MENTION_POLL_SECONDS); the default of 30 is unchanged for real arms.
# Used for BOTH the seed-retry sleep and the poll-loop sleep so a test can
# exercise the seed retry below without a 30s wall-clock wait.
POLL_SECONDS="${FORUM_UPDATES_POLL_SECONDS:-30}"

# ---------------------------------------------------------------------------
# seed — retry until the first poll that SUCCEEDS and hands back a USABLE
# WATERMARK; that watermark is the baseline, so arming does NOT replay history.
# Handles arm-while-forum-down (waits).
#
# The second half of that condition is not decoration (#2225), and it is
# strictly stronger than "the body parsed". curl --fail passes a 200 carrying a
# maintenance page; a clean `{}` or a null as_of passes a JSON parse. Either
# way we never learned where the window ends, and seeding from it lands 0 — so
# the very next poll asks since=0 and emit() runs the ENTIRE dm+baton backlog.
# emit() has no aggregation (one printed line per update, unlike
# forum-mention-monitor's flush_buffer), so that is a genuine per-update flood:
# precisely the history replay this seed exists to prevent.
#
# Unlike the poll loop below there is NO monotonicity guard here to catch it —
# 0 simply becomes the baseline. That asymmetry is why as_of() rejects a
# missing watermark rather than reporting 0: the poll can afford to shrug at
# these bodies and the seed cannot, so the helper answers for the stricter
# caller. A watermark of 0 that the feed actually SERVED is still accepted —
# see as_of() above; a fresh forum's as_of: 0 is a real baseline, not an
# absence.
# ---------------------------------------------------------------------------
while true; do
    if RESP="$(curl -s --fail "$URL_BASE&since=0" 2>/dev/null)" \
       && _seed="$(printf '%s' "$RESP" | as_of)"; then
        printf '%s\n' "$_seed" > "$CURSOR"
        break
    fi
    sleep "$POLL_SECONDS"
done

# Path to the last-user-activity stamp (written by the time-bar hook on each
# genuine human-typed prompt) — same engaged-detection source as the
# mention-monitor / _status_derive._recently_engaged().
LAST_USER_ACTIVITY="$HOME/.engram/last-user-activity"
_ENGAGED_WINDOW=360  # matches _status_derive._DEFAULT_ENGAGED_WINDOW

# ---------------------------------------------------------------------------
# poll loop
# ---------------------------------------------------------------------------
while true; do
    sleep "$POLL_SECONDS"
    # engaged-suppression: while the user is actively conversing, skip the emit
    # cycle AND leave the cursor put — events accumulate in (cursor, as_of] and
    # emit once the engaged window expires.
    if [ -f "$LAST_USER_ACTIVITY" ]; then
        _stamp="$(cat "$LAST_USER_ACTIVITY" 2>/dev/null || echo 0)"
        _age=$(( $(date +%s) - ${_stamp:-0} )) || _age=999999
        if [ "$_age" -ge 0 ] && [ "$_age" -le "$_ENGAGED_WINDOW" ]; then
            continue
        fi
    fi
    _cur="$(cat "$CURSOR" 2>/dev/null || echo 0)"
    # Forum unreachable -> curl --fail returns non-zero -> skip the cycle and
    # PRESERVE the cursor (never advance past an un-emitted window).
    RESP="$(curl -s --fail "$URL_BASE&since=$_cur" 2>/dev/null)" || continue
    printf '%s' "$RESP" | emit
    # Advance the cursor only when this poll actually handed back a watermark.
    # `$( )` swallows the command's status into the assignment, so the status
    # has to be READ — hence `if _new="$(...)"` rather than a bare assignment
    # plus a value test (#2225). The old `[ -n "$_new" ]` here was dead code:
    # as_of() printed on every path, so _new was never empty and the guard it
    # advertised did not exist. The real guard is the exit status; the `-ge`
    # monotonicity check stays as-is.
    #
    # For a body that parses with no usable watermark this is behaviourally
    # what already happened — as_of() used to report 0 and `-ge` declined the
    # write — so nothing about ordinary quiet polling changes here. The
    # stricter helper exists for the SEED, which has no such guard.
    if _new="$(printf '%s' "$RESP" | as_of)"; then
        [ "$_new" -ge "$_cur" ] 2>/dev/null && printf '%s' "$_new" > "$CURSOR"
    fi
done
