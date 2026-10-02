#!/usr/bin/env bash
# forum-mention-monitor.sh — persistent Monitor for new forum @-mentions of
# this agent.
#
# Arm via the Monitor tool with persistent: true (session-length watch).
# Stop with TaskStop when the loop ends.
#
# Usage:
#   forum-mention-monitor.sh [--purpose <key>]
#
# Multi-agent hosts may symlink this into /home/agents-shared/bin/ alongside
# ia/baton/forum (same convention; no install step creates the symlink — it is
# a manual host-operator action today).
#
# Discipline notes (load-bearing — do not remove):
#   - resolves forum base URL from config, same precedence as forum CLI:
#     config.json forum.url > $FORUM_URL > localhost:5002.
#   - seed-retry-until-first-successful-poll: baseline is always the real
#     current state. Handles arm-while-forum-down (waits), arm-while-up (seeds
#     now), and genuine-zero-mentions (empty baseline on a successful poll).
#   - never clobber seen-set on failed poll: curl --fail returns non-zero on
#     forum-unreachable; skip the cycle and preserve $SEEN. Otherwise the next
#     recovery would flood every historical mention as "new" (#743 / 2026-06-03
#     forum cutover incident).
#   - "failed poll" is BROADER than curl (#2225). curl --fail only rejects HTTP
#     >= 400, so a maintenance page / truncated body reaches ids() intact; and a
#     body can parse cleanly and still never REPORT a mentions list (`{}`, an
#     error envelope, a JSON array, `"mentions"` set to a non-list). Both look
#     identical to "zero mentions" and would overwrite $SEEN empty. ids() signals
#     both by EXIT STATUS and the callers skip the cycle.
#     `{"mentions":[]}` is the ONE that is genuinely empty — it parses AND it
#     reports, it just reports nothing — so it still rewrites $SEEN as it always
#     has. That is the whole boundary: reported-zero rewrites, failed-to-report
#     preserves. Note `{}` sits on the PRESERVE side, matching as_of()'s reading
#     of the same body in the sibling script; a healthy forum cannot send it on a
#     200 anyway (src/forum/server.py returns {"mentions": ...} on 200 and 400 on
#     both error paths, which curl --fail already rejects).
#   - engaged-suppression (#1077 part 2): while last-user-activity is within
#     the engaged window (default 360s = 6 min), skip the emit cycle entirely —
#     mentions accumulate as unseen and are emitted after the window expires.
#     The 'engaged' status is published by the forum prompt hook independently.
#   - unreachable-vs-quiet is EDGE-TRIGGERED, not per-cycle (#3062): a forum
#     outage and a quiet-but-reachable forum used to be byte-identical at this
#     script's output (none) — the expiry notice reads "no events delivered"
#     either way, and there is no way to tell a 7h blackout from a calm night.
#     Fix mirrors umwelt-bell.py's post-umwelt#637 _ARMED_REACHABLE pattern:
#     print exactly once on the transition INTO forum-unreachable and once on
#     the transition back OUT, never once per failed poll. Level-triggered (a
#     line every cycle) was considered and rejected in that precedent for the
#     same reason it would be wrong here — at this script's 30s poll interval
#     an 8h outage is ~960 lines, which destroys the very signal it exists to
#     give. This does NOT touch $SEEN handling — see the never-clobber
#     bullet above; the two are orthogonal.
#     The SEED loop announces on the same edge (#3067), because the poll-loop
#     announce above sits BELOW a seed that blocks until the first good poll —
#     so a monitor armed while the forum is down never reached it and stayed
#     completely mute, which is the case the announce is most needed in (seats
#     arm at session start, and session start follows a reboot / redeploy /
#     overnight shutdown). One line on the first failed seed attempt ("not yet
#     seeded"), one when the seed finally succeeds, nothing at all when the
#     first attempt succeeds, and nothing YET while the user is engaged —
#     deferred, not swallowed: the latch stays clear so the next retry past the
#     engaged window announces once. The seed says it cannot SEED rather than
#     that the forum is "unreachable", because its failure predicate is broader
#     than the poll loop's: it also covers a reachable 200 whose body does not
#     parse. The seed's retry and parse predicate are unchanged: blocking until
#     a readable body is CORRECT (#743 / #2225) — the defect was that the
#     waiting was silent, not that it waited.
#   - cross-cycle coalesce window (#1616 / UCS rc2 F10): the first new
#     mention detected opens a fixed coalesce window (default 120s, override
#     via $FORUM_MENTION_COALESCE_SECONDS); further new mentions across
#     subsequent polls accumulate in a buffer WITHOUT emitting; at window
#     close, ONE summary line is emitted for the whole burst (a hot thread
#     now wakes at most once per window instead of once per poll). $SEEN
#     still advances every successful poll regardless of window state —
#     mentions seen during the window are not re-emitted after flush; the
#     window changes WHEN/HOW a mention is announced, never the seen-set
#     semantics. The buffer is untouched on a failed poll — same
#     never-clobber discipline as $SEEN (#743), so a forum-down cycle
#     mid-window loses nothing. CAVEAT: on TaskStop/SIGTERM mid-window the
#     buffered-but-unflushed mentions are dropped (not flushed on exit);
#     $SEEN has already advanced for them, so they won't re-emit next arm —
#     recovery is the browse-on-wake routine (collab-loop 3b re-reads the
#     forum), not this monitor. Acceptable: TaskStop = session ending anyway.

set -u
# NOT -e: a transient failure must not kill a session-length watch.

# ---------------------------------------------------------------------------
# argument parsing: --help, --purpose <key>
# ---------------------------------------------------------------------------
PURPOSE="default"
while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            cat <<'EOF'
forum-mention-monitor.sh — watch the LAN forum for new direct @-mentions of me

Usage: forum-mention-monitor.sh [--purpose <key>]

  --purpose <key>  Purpose key for this instance (default: "default").
                   Duplicate-instance DETECTION (not reap — see #1933) is
                   scoped to same-purpose instances only, so two instances
                   launched for different purposes do not cross-flag. This
                   monitor never kills a suspected duplicate — it prints a
                   warning naming the pid to stdout (the Monitor event
                   stream) and lets the agent decide whether to `kill <pid>`
                   by hand.

Resolves SELF (URL-encoded) and forum BASE URL from ~/.engram/config.json.
URL resolution order: config.json forum.url > $FORUM_URL > http://localhost:5002

Seeds on first successful poll (retries until forum is reachable). Never
clobbers the seen-set on a failed poll — preserves prior baseline on
transient forum-down cycles.

Coalesces new @-mentions into a summary: the first new mention opens a
coalesce window (default 120s, $FORUM_MENTION_COALESCE_SECONDS); further
new mentions across subsequent polls accumulate without emitting; at
window close, ONE summary line covers the whole burst, e.g.:
  "N new @-mentions across M threads: #200(x3), #199(x1)"
A hot thread wakes at most once per window instead of once per poll.
Polls every 30s. Arm via Monitor tool with persistent: true; stop with TaskStop.
EOF
            exit 0
            ;;
        --purpose)
            PURPOSE="${2:?--purpose requires a value}"
            shift 2
            ;;
        *)
            echo "forum-mention-monitor.sh: unrecognised argument: $1" >&2
            exit 1
            ;;
    esac
done

# Validate purpose key: only [a-zA-Z0-9_-] safe in pgrep -f regex (#1307).
case "$PURPOSE" in
    *[!a-zA-Z0-9_-]*)
        echo "forum-mention-monitor: --purpose key must contain only [a-zA-Z0-9_-]; got: '$PURPOSE'" >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# resolve identity + forum URL
# ---------------------------------------------------------------------------
SELF="$(python3 -c 'import json,os,urllib.parse;print(urllib.parse.quote(json.load(open(os.path.expanduser("~/.engram/config.json"))).get("agent_name",""),safe=""))')"
BASE="$(python3 -c 'import json,os
c=json.load(open(os.path.expanduser("~/.engram/config.json")))
print(((c.get("forum") or {}).get("url") or os.environ.get("FORUM_URL") or "http://localhost:5002").rstrip("/"))')"
URL="$BASE/api/agent/$SELF/mentions?kind=at_mention"   # #1040: at_mention only — a reply to a thread you authored is NOT a direct @-mention and must not over-fire this real-time wake

# ---------------------------------------------------------------------------
# duplicate-instance detection: WARN, never kill (#1933). A persistent
# Monitor's bash process outlives its arming session — it survives compaction
# and session-end. Without SOME check for a still-running prior instance, a
# fresh arm after a compaction runs ALONGSIDE the surviving orphan and BOTH
# poll the same mentions endpoint, so every new @-mention would fire the wake
# TWICE (double-delivery incident, 2026-06-13) — that risk is real and still
# worth flagging. But the old fix (SIGTERM every pgrep -f match) assumed
# pgrep's unanchored substring match over the WHOLE cmdline could only ever
# hit a genuine prior monitor. It cannot be trusted that way: `agentctl
# --charge-file` appends the boot charge to claude's own argv, #1865 put this
# script's path INSIDE that charge text, and claude sits as this monitor's
# GRANDPARENT — outside the $$/$PPID exemptions below — so every
# charge-booted session was reaping (killing) itself four days running. The
# old comment here called the substring-match risk "harmless in production"
# (an editor merely mentioning the filename) — #1933 is the counterexample:
# a substring match must never be an execution decision (Lei's ruling). This
# block now only WARNS on stdout — the Monitor event stream, so the warning
# reaches the agent as a notification — and leaves every matched process
# alone. A true duplicate left alive double-delivers every event; the agent
# inspects the flagged pid and kills it by hand if it really is stale.
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
# quoted argument (e.g. `bash -c "... forum-mention-monitor.sh ..."`) can
# manufacture a false token boundary and over-match — measured on Linux
# (issue #2134 comments) against a `bash -c` wrapper. Kept anyway because a
# weaker-but-labeled detector beats no detection at all on the one platform
# that has no /proc to fall back on. rc 3 (below) marks this weaker verdict
# distinctly from rc 0's /proc-verified one so the two warning classes don't
# get conflated into a single alarm-fatigue channel.
#
# Set $FORUM_MENTION_NO_REAP to any non-empty value to disable (now disables
# the detection/warning pass, not a kill).
# ---------------------------------------------------------------------------
_check_forum_mention_dup() {
    # $1=pid $2=script-basename. Delegates to the shared _proc_identity.py
    # (#2321 -- extracted from a 1629-byte block that used to be
    # byte-identical, hand-synced, in this file and
    # forum-updates-monitor.sh). See that module's own docstring for the
    # full exit-code table and the /proc-vs-ps-fallback reasoning; this
    # wrapper's only job is finding the sibling script and forwarding argv.
    local _self _proc_identity
    _self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
    _proc_identity="$(dirname "$_self")/_proc_identity.py"
    python3 "$_proc_identity" "$1" "$2" 2>/dev/null
}

if [ -z "${FORUM_MENTION_NO_REAP:-}" ] && command -v pgrep >/dev/null 2>&1; then
    # Purpose-scoped detection: only flag prior instances running with the
    # SAME --purpose key as suspects. For the "default" purpose (no --purpose
    # arg passed), match by script name only — identical candidate set to the
    # pre-#1183 behaviour. For an explicit non-default purpose key, match by
    # script name AND purpose so default instances (and instances of a
    # different non-default purpose) are never cross-flagged (#1183).
    #
    # #2029 inventory answers: could this candidate pattern match the
    # invoking process (a boot charge quoting this script's path, #1933)?
    # YES -- by design, at the pgrep-candidate stage alone. That is exactly
    # why this site is two-layered: -u "$(id -u)" scopes to this uid only
    # (so a co-tenant's equivalent process on a shared host can never be a
    # candidate at all), and every candidate is then re-verified against
    # /proc/<pid>/cmdline below (_check_forum_mention_dup) before it is even
    # WARNED about -- and the action is warn-not-kill regardless, so an
    # over-matched candidate can never cause a reap. See
    # tests/test_pgrep_liveness_reap_patterns.py for the synthetic-table
    # proof that this candidate pattern over-matches at layer 1, and
    # tests/test_forum_monitor_duplicate_warn.py for the end-to-end proof
    # that the self-quoting decoy is neither reported nor killed.
    if [ "$PURPOSE" = "default" ]; then
        _pgrep_pattern='forum-mention-monitor\.sh'
    else
        _pgrep_pattern="forum-mention-monitor\.sh.*--purpose ${PURPOSE}"
    fi
    for _pid in $(pgrep -u "$(id -u)" -f "$_pgrep_pattern" 2>/dev/null); do
        [ "$_pid" = "$$" ] && continue
        [ "$_pid" = "${PPID:-0}" ] && continue
        _out="$(_check_forum_mention_dup "$_pid" "forum-mention-monitor.sh")"
        _rc=$?
        if [ "$_rc" -eq 0 ]; then
            echo "⚠️  forum-mention-monitor.sh: possible duplicate instance — pid $_pid cmdline: ${_out}"
            echo "⚠️  forum-mention-monitor.sh: if pid $_pid is a stale duplicate, inspect it and \`kill $_pid\` by hand — a live duplicate double-delivers every event."
        elif [ "$_rc" -eq 3 ]; then
            echo "⚠️  forum-mention-monitor.sh: possible duplicate instance (WEAK match — no /proc on this platform, verified via \`ps\` string-split only; may false-positive on wrapper processes, #2134) — pid $_pid cmdline: ${_out}"
            echo "⚠️  forum-mention-monitor.sh: if pid $_pid is a stale duplicate, inspect it and \`kill $_pid\` by hand — a live duplicate double-delivers every event."
        elif [ "$_rc" -eq 2 ]; then
            echo "⚠️  forum-mention-monitor.sh: could not verify candidate pid $_pid (${_out}) — NOT auto-flagged as a duplicate; check it by hand if you suspect a stale instance."
        fi
        # rc == 1: not a match (incl. "process gone mid-read") — silent.
    done
fi

# ---------------------------------------------------------------------------
# seen-set
# ---------------------------------------------------------------------------
# Seen-set keyed to the polled URL so concurrent instances (smoke tests,
# alternate forums) never share state — a fixed path lets a second instance
# clobber the live monitor's baseline and flood it (#743 class via
# cross-instance clobber). Override with $FORUM_MENTION_SEEN_FILE (tests).
# Note (#1040): appending ?kind=at_mention changed $URL, so the cksum changes —
# the monitor reseeds its baseline once on the post-upgrade first run (harmless;
# the seed-poll just re-establishes "current state" before emitting).
SEEN="${FORUM_MENTION_SEEN_FILE:-$HOME/.engram/.forum-mention-seen.$(printf '%s' "$URL" | cksum | cut -d' ' -f1)}"

# helper: extract "post_id\tthread_title" lines from JSON stdin
#
# EXIT STATUS answers "did this body REPORT a mentions list?" (#2225) — the
# same shape as as_of()'s "did I obtain a usable watermark", and deliberately
# NOT the weaker "did the body parse".
#   - exit 0: `mentions` is present and is a list. An EMPTY list is a real
#     answer — the forum is telling us there are none right now — so it still
#     rewrites $SEEN, which is what keeps the seen-set a true snapshot.
#   - exit non-zero: the body did not parse, OR it parsed without reporting
#     (`{}`, `{"error":...}`, a top-level array, `"mentions"` set to a string
#     or null). A body with no `mentions` key has not reported zero mentions;
#     it has FAILED TO REPORT, and writing an empty seen-set from it hands the
#     next good poll every historical mention as new.
# The distinction between those two is exactly the distinction as_of() draws
# between a served `as_of: 0` and a missing one. `{}` must not mean "authoritative
# zero" here while meaning "we never learned" there — one changeset cannot read
# the same body two opposite ways.
# Everything AFTER that check keeps its old blanket-except,
# degrade-to-fewer-lines behaviour and exits 0.
ids() {
    python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(d, dict) or not isinstance(d.get("mentions"), list):
    sys.exit(1)
try:
    for m in d["mentions"]: print(str(m.get("post_id"))+"\t"+m.get("thread_title","?"))
except Exception: pass'
}

# Poll cadence override — tests only (mirrors $FORUM_MENTION_SEEN_FILE's
# test-only role above); default of 30 is unchanged for real arms. Defined
# ABOVE the seed (not with COALESCE_SECONDS below) because the seed-retry sleep
# uses it too.
POLL_SECONDS="${FORUM_MENTION_POLL_SECONDS:-30}"

# Path to the last-user-activity stamp (written by the time-bar hook on each
# genuine human-typed prompt). Used to detect the 'engaged' / talking-to-user
# state — same source as _status_derive._recently_engaged() in Python.
# Declared ABOVE the seed (it used to sit between the seed and the poll loop)
# because the seed's announce consults it too — see _engaged() below.
LAST_USER_ACTIVITY="$HOME/.engram/last-user-activity"
# Engaged-window in seconds: matches _status_derive._DEFAULT_ENGAGED_WINDOW (360).
# If cadence.engaged_window_seconds is customised in config.json, update this too.
_ENGAGED_WINDOW=360

# True (exit 0) while the user is actively conversing. No stamp file means
# never engaged; $_age is recomputed from the stamp on every call so a window
# that expires mid-wait is seen; the `-ge 0` half rejects a FUTURE stamp (a
# clock skew or a hand-edited file would otherwise make $_age negative, which
# `-le $_ENGAGED_WINDOW` would read as engaged forever).
#
# A MALFORMED stamp must fail towards "not engaged" — i.e. announce — because
# this predicate now gates the seed's arm-time warning, and the whole point of
# that warning is that a monitor which cannot report is worse than a noisy one.
# Getting there needs the validation below rather than the `|| _age=999999`
# fallback the poll loop still carries, which cannot fire: under `set -u` a
# non-numeric stamp makes the arithmetic expansion itself fail, so `_age` is
# never assigned at all. Measured on #3111 — a stamp reading `garbage` gives
# `unbound variable`, rc=1, and the monitor EXITS, silently and permanently;
# a float or a `0999` aborts the enclosing `if` instead, so the announce is
# skipped and the monitor goes on waiting in exactly the silence #3067 fixed.
# Validating first makes the arithmetic total: the `case` rejects anything that
# is not all-digits (empty, float, whitespace, `garbage`) and `10#` forces base
# 10 so a zero-padded stamp cannot trip "value too great for base".
#
# The poll loop below keeps its own inline copy unchanged (#3062's code is
# deliberately untouched here, and its reachability is not what changed) — if
# either is ever changed, change both. Unifying the two is the follow-up.
_engaged() {
    [ -f "$LAST_USER_ACTIVITY" ] || return 1
    local _stamp _age
    _stamp="$(cat "$LAST_USER_ACTIVITY" 2>/dev/null || echo 0)"
    case "$_stamp" in ''|*[!0-9]*) _stamp=0 ;; esac
    _age=$(( $(date +%s) - 10#$_stamp ))
    [ "$_age" -ge 0 ] && [ "$_age" -le "$_ENGAGED_WINDOW" ]
}

# --max-time (#3106): neither curl call below bounded a STALLED response —
# one that is accepted and sends valid headers but then withholds the body
# forever without closing. `--fail` only rejects a non-2xx status or a
# connection failure; a stall is neither, so pre-#3106 curl never returns and
# the monitor hangs silently, reaching neither #3065's nor #3067's
# unreachable announce (measured: Darwin, a 200+Content-Length response that
# never sends its body did not self-bound within 8s of `curl -s --fail`
# alone). Chosen at 10s, deliberately NOT copying the sibling
# forum-updates-monitor.sh's `--max-time 60` (#2007): that script's poll
# cadence is also 30s, but this monitor additionally runs a coalesce-window
# check every cycle (COALESCE_SECONDS below) — a stall that ate 60s of a 30s
# cycle would double the effective cycle length and skew how many real polls
# land inside a coalesce window, whereas 10s (comfortably above LAN tail
# latency, comfortably below one poll interval) keeps a stall's cost to at
# most one extended cycle, never approaching two. A stalled call still
# returns non-zero exactly like any other curl failure once --max-time
# elapses, so the existing `|| continue` / `if ! RESP=...` handling below
# needs no other change — the fix is the flag, on both call sites.
CURL_MAX_TIME="${FORUM_MENTION_MAX_TIME_SECONDS:-10}"

# ---------------------------------------------------------------------------
# seed — retry until the first poll that both SUCCEEDS and PARSES
# Handles: arm-while-forum-down (waits), arm-while-up (seeds now), and
# genuine-zero-mentions (empty baseline on a *successful* poll).
#
# The parse half is the seed-side twin of the poll-path guard below (#2225): a
# 200 whose body does not parse passes curl --fail, yields no id lines, and
# would seed an EMPTY baseline — so the first good poll then reports every
# historical mention as new, the same clobber-replay the poll path guards
# against, just arriving at arm time. An empty seen-set from a body we could
# not read is not "genuine zero mentions"; wait for a body we can read.
#
# The WAITING is announced, edge-triggered (#3067). #3062's announce lives in
# the poll loop below, which an arm-time outage never reaches: the seed blocks
# here until the first good poll, so a monitor armed while the forum was down
# emitted nothing at all — byte-identical, on every surface an agent reads, to
# a healthy monitor on a quiet channel. Arming during an outage is the routine
# case, not the rare one (a seat arms its monitors at session start, and
# session start is what follows a reboot / redeploy / overnight shutdown).
# Same edge discipline as the poll loop, for the same reason: exactly one line
# on the FIRST failed attempt, exactly one when the seed finally succeeds,
# NEVER one per attempt — the retry sleeps on $POLL_SECONDS too, so a
# level-triggered seed is the same ~960-lines-per-8h-outage flood umwelt#637
# rejected. $_SEED_ANNOUNCED is what keeps the healthy path silent: when the
# first attempt succeeds (the normal case) neither line is printed.
# This changes only the OUTPUT — the retry, the success predicate, the staging
# through $SEEN.now and the parse guard are all untouched.
#
# The WORDING deliberately diverges from the poll loop's "forum unreachable".
# The two sites know different things. The poll loop's announce sits inside the
# curl-failure branch, so "unreachable" is precisely what it observed. The
# seed's failure predicate is BROADER: it fails on a curl failure AND on a
# reachable HTTP 200 whose body does not parse / carries no usable watermark
# (see the parse half above — that is not a hypothetical, it is what #2225 and
# the malformed-200 suite are about). Calling that "unreachable" would be
# false. What the seed actually knows is that it cannot establish a baseline,
# which is both true in every case and the thing an operator needs to hear.
# The success line says "seeded" rather than "reachable AGAIN" for the same
# reason: on this path the forum was never reachable in the first place.
#
# ENGAGED-WINDOW handling, mirroring the poll loop (#3067): while the user is
# actively conversing, do not emit — and, critically, DO NOT SET THE LATCH.
# The poll loop's documented semantics are defer-not-swallow (skip the emit
# cycle, emit once the window expires); latching a suppressed announce here
# would swallow it FOREVER, because nothing re-offers it: the seed would go on
# retrying in silence and the operator would never learn the monitor is
# unseeded — the exact defect #3067 exists to fix, reintroduced through the
# suppression path. Leaving $_SEED_ANNOUNCED clear means the next retry past
# the engaged window announces exactly once.
# The SUCCESS line is deliberately NOT engaged-suppressed, and the asymmetry is
# forced rather than chosen: the failure announce has a retry that re-offers it,
# so deferral costs nothing, while the success line fires on the iteration that
# breaks out of the loop. Deferring it would mean either delaying the seed (a
# change to the retry semantics, which must not change) or latching a
# suppressed line (swallowed forever, above). It also only ever CLOSES a
# warning the operator has already received.
# ---------------------------------------------------------------------------
_SEED_ANNOUNCED=0
while true; do
    # Stage through $SEEN.now and mv, exactly as the poll loop does, so a
    # retrying seed never truncates a prior run's baseline on its way past.
    if RESP="$(curl -s --fail --max-time "$CURL_MAX_TIME" "$URL" 2>/dev/null)" \
       && ( set -o pipefail; printf '%s' "$RESP" | ids | sort > "$SEEN.now" ); then
        mv "$SEEN.now" "$SEEN"
        if [ "$_SEED_ANNOUNCED" = 1 ]; then
            echo "forum-mention-monitor.sh: seeded from the forum — now receiving"
        fi
        break
    fi
    rm -f "$SEEN.now"
    # Note the latch is set INSIDE the not-engaged branch, never beside it:
    # see the defer-not-swallow paragraph above.
    if [ "$_SEED_ANNOUNCED" = 0 ] && ! _engaged; then
        echo "⚠️  forum-mention-monitor.sh: armed, but cannot seed from the forum — not yet seeded and not receiving; will report when it can"
        _SEED_ANNOUNCED=1
    fi
    sleep "$POLL_SECONDS"
done

# ---------------------------------------------------------------------------
# cross-cycle coalesce window (#1616 / UCS rc2 F10)
# A hot thread can produce a new @-mention on almost every 30s poll, which
# without coalescing wakes the agent every cycle for as long as the thread
# stays hot. Fix: the FIRST new mention detected opens a FIXED (not
# sliding/extended) coalesce window; further new mentions across subsequent
# polls accumulate into a buffer WITHOUT emitting; at window close, ONE
# summary line is emitted for the whole burst (the Monitor tool still
# batches it as a single wake). The ScheduleWakeup heartbeat already floors
# liveness, so this loses zero responsiveness — just fewer, batched wakes.
#
# The buffer is a plain file, untouched on a failed poll (curl --fail
# non-zero -> `continue` below skips straight past both the $SEEN update AND
# the buffer/window logic) — same never-clobber discipline that protects
# $SEEN (#743), applied to the buffer too, so a forum-down cycle mid-window
# loses nothing.
# ---------------------------------------------------------------------------
COALESCE_SECONDS="${FORUM_MENTION_COALESCE_SECONDS:-120}"
# ($POLL_SECONDS is defined above the seed, which needs it for its retry sleep.)
BUF="$SEEN.buf"
: > "$BUF"          # fresh arm — no pending burst carried over from a prior run
_WINDOW_OPEN_TS=""  # empty = no window currently open

# flush_buffer: $BUF holds one thread_id per buffered new mention (one line
# per mention, so a thread_id repeats once per mention in that thread — the
# source of the "×N" tally below). On a non-empty buffer, emit ONE summary
# line; always clear the buffer and close the window afterward. No-op
# (silently) on an empty buffer — called unconditionally once the window
# has elapsed, whether or not anything is actually buffered.
flush_buffer() {
    if [ -s "$BUF" ]; then
        python3 -c '
import collections, sys
lines = [l.strip() for l in sys.stdin if l.strip()]
n = len(lines)
counts = collections.OrderedDict()
for tid in lines:
    counts[tid] = counts.get(tid, 0) + 1
parts = ", ".join("#" + str(t) + "(×" + str(c) + ")" for t, c in counts.items())
mention_word = "mention" if n == 1 else "mentions"
thread_word = "thread" if len(counts) == 1 else "threads"
print("\U0001f4e3 " + str(n) + " new @-" + mention_word + " across " + str(len(counts)) + " " + thread_word + ": " + parts)
' < "$BUF"
        : > "$BUF"
    fi
    _WINDOW_OPEN_TS=""
}

# Edge-triggered reachability state for the unreachable-vs-quiet announce
# (#3062 — see the discipline-notes bullet above). Seeded reachable: the seed
# loop just above only exits once a poll has actually succeeded, so the poll
# loop below always starts in the reachable state.
_FORUM_REACHABLE=1

# ---------------------------------------------------------------------------
# poll loop
# ---------------------------------------------------------------------------
while true; do
    sleep "$POLL_SECONDS"
    # Suppress events during interactive (talking-to-user / 'engaged') sessions
    # (#1077 part 2). When the user is actively conversing, waking the agent
    # mid-turn adds noise. Mentions accumulate as unseen; the monitor emits
    # them once the engaged window expires (default 6 min of user inactivity).
    if [ -f "$LAST_USER_ACTIVITY" ]; then
        _stamp="$(cat "$LAST_USER_ACTIVITY" 2>/dev/null || echo 0)"
        _age=$(( $(date +%s) - ${_stamp:-0} )) || _age=999999
        if [ "$_age" -ge 0 ] && [ "$_age" -le "$_ENGAGED_WINDOW" ]; then
            continue
        fi
    fi
    # Forum unreachable -> curl --fail returns non-zero -> skip the cycle,
    # PRESERVE $SEEN (the fix for the seed-clobber flood, #743) AND the
    # coalesce buffer/window (nothing below this line runs).
    #
    # #3062: the `continue` itself is unchanged ($SEEN and the coalesce
    # buffer/window still preserved, exactly as before) — only the EDGE is
    # now announced, once on entry and once on recovery, via
    # $_FORUM_REACHABLE (declared above the loop).
    if ! RESP="$(curl -s --fail --max-time "$CURL_MAX_TIME" "$URL" 2>/dev/null)"; then
        if [ "$_FORUM_REACHABLE" = 1 ]; then
            echo "⚠️  forum-mention-monitor.sh: forum unreachable — still armed, not receiving; will report when it returns"
            _FORUM_REACHABLE=0
        fi
        continue
    fi
    if [ "$_FORUM_REACHABLE" = 0 ]; then
        echo "forum-mention-monitor.sh: forum reachable again — receiving resumed"
        _FORUM_REACHABLE=1
    fi
    # A 200 whose body does not parse is NOT "zero mentions" (#2225). Left
    # alone it produces an empty $SEEN.now, and the unconditional mv below
    # overwrites $SEEN empty — after which the next GOOD poll marks every
    # historical mention new. ids() exits non-zero there; run it under a
    # pipefail SUBSHELL so that status survives the `| sort` (a plain
    # pipeline reports sort's status, which is always 0), then skip the cycle
    # exactly as a failed curl does. Remove $SEEN.now so no half-written
    # scratch file is left for a later cycle to pick up.
    if ! ( set -o pipefail; printf '%s' "$RESP" | ids | sort > "$SEEN.now" ); then
        rm -f "$SEEN.now"
        continue
    fi
    _new_pids="$(comm -13 "$SEEN" "$SEEN.now" | cut -f1)"
    mv "$SEEN.now" "$SEEN"
    if [ -n "$_new_pids" ]; then
        # Look up each newly-seen post's thread_id in the just-fetched $RESP
        # (no extra HTTP call) and append one line per mention to the buffer
        # — the coalesce window changes WHEN/HOW these are announced, never
        # whether $SEEN advances (already done above).
        printf '%s' "$RESP" | python3 -c '
import json, sys
wanted = set(sys.argv[1].split())
# Blanket-except + isinstance guards so a status-200-but-malformed body
# (non-dict, or "mentions" a non-list) degrades to "append nothing this
# cycle" rather than throwing mid-loop — matches ids()'"'"'s defensive style.
try:
    d = json.load(sys.stdin)
    mentions = d.get("mentions", []) if isinstance(d, dict) else []
    for m in mentions:
        if isinstance(m, dict) and str(m.get("post_id")) in wanted:
            print(m.get("thread_id", "?"))
except Exception:
    pass
' "$_new_pids" >> "$BUF"
        [ -z "$_WINDOW_OPEN_TS" ] && _WINDOW_OPEN_TS="$(date +%s)"
    fi
    # Check window elapsed every cycle (not just cycles with fresh mentions)
    # so a previously-opened window still flushes on schedule.
    if [ -n "$_WINDOW_OPEN_TS" ]; then
        _now="$(date +%s)"
        _elapsed=$(( _now - _WINDOW_OPEN_TS ))
        [ "$_elapsed" -ge "$COALESCE_SECONDS" ] && flush_buffer
    fi
done
