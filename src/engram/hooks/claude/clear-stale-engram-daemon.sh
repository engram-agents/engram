#!/bin/bash
# OPERATOR-INVOKED recovery verb (engram-alpha#3493). Run by a HUMAN, never by
# a hook, a supervisor unit, or the daemon -- nothing in the launch path
# references this file, and that is a tested contract.
#
# Why it exists: the surface daemon's startup gate (#3361, exit 75 HELD / exit
# 76 UNPROVEN) refuses to unlink recall-daemon.sock unless it can POSITIVELY
# show nobody holds it. On macOS there is no /proc/net/unix, so a crash-stale
# socket (SIGKILL, OOM, power loss) cannot be told apart from a live holder
# owned by another user, and the daemon keeps refusing. No heuristic may decide
# "stale" automatically (an own-uid socket whose only live reference is an fd
# queued via SCM_RIGHTS looks exactly like a dead one to lsof). This verb is
# the human's deliberate decision to clear it.
#
# What it does: unlinks the daemon's socket and pidfile -- exactly those two
# paths, resolved the way the daemon resolves them -- and nothing else.
#
#   clear-stale-engram-daemon.sh --yes           clear them
#   clear-stale-engram-daemon.sh                 show what WOULD be removed, exit 2
#   clear-stale-engram-daemon.sh --print-paths   print the two paths only (read-only)
#
# Refuses (unlinks NOTHING, non-zero) unless the socket path is a real socket
# and the pidfile path is a regular file; symlinks are refused for both.
# All-or-nothing: both paths are checked before either is unlinked.
#
# Self-contained on purpose: the plugin build flattens hooks/claude/* ->
# hooks/*, so this script locates nothing relative to itself. Written for
# bash 3.2 (macOS /bin/bash): no associative arrays, no ${var,,}, no mapfile.

set -u

# A RELATIVE ENGRAM_HOME is refused (alpha #3863): it would resolve against the calling process's cwd, a
# different home for every process. Unset or empty means unset; a leading ~ is expanded against $HOME and
# re-exported (~user is not expanded: it is not absolute, so it is refused). Checked before anything else
# runs; `return` covers a script that is sourced, `exit` one that is executed.
if [ -n "${ENGRAM_HOME-}" ]; then
    _eh_raw="$ENGRAM_HOME"
    case "$ENGRAM_HOME" in
        "~") ENGRAM_HOME="${HOME-}"; export ENGRAM_HOME ;;
        "~/"*) ENGRAM_HOME="${HOME-}/${ENGRAM_HOME#"~/"}"; export ENGRAM_HOME ;;
    esac
    case "$ENGRAM_HOME" in
        /*) ;;
        *)
            printf "ENGRAM_HOME must be an absolute path, got '%s'\n" "$_eh_raw" >&2
            unset _eh_raw
            return 2 2>/dev/null || exit 2 ;;
    esac
    unset _eh_raw
fi

# A set CDPATH makes `cd rel` resolve elsewhere AND print the directory to
# stdout, corrupting the path captured below. Never inherit it.
unset CDPATH

SELF="clear-stale-engram-daemon"

usage() {
    echo "usage: $SELF.sh [--yes | --print-paths]" >&2
    echo "  --yes          actually remove the stale socket and pidfile" >&2
    echo "  --print-paths  print the two resolved paths and do nothing else" >&2
    echo "  (no flag)      show what would be removed and exit 2" >&2
}

# ---- argument parsing (strict: unknown flags and extra args are refused) ----
YES=0
PRINT_PATHS=0
for _arg in "$@"; do
    case "$_arg" in
        --yes) YES=1 ;;
        --print-paths) PRINT_PATHS=1 ;;
        *)
            echo "[$SELF] unrecognized argument: $_arg" >&2
            usage
            exit 2
            ;;
    esac
done

# ---- K2: path resolution, mirroring engram-surface-daemon.py ----------------
# Daemon: ENGRAM_HOME = os.path.abspath(env ENGRAM_HOME or expanduser("~/.engram"));
# SOCKET_PATH = ENGRAM_HOME/recall-daemon.sock; PID_PATH = ENGRAM_HOME/recall-daemon.pid.
# Read-only: unlike start-engram-daemon.sh this never mkdir's anything.
_home="${ENGRAM_HOME:-$HOME/.engram}"
# `$(cd ... && pwd)` strips trailing newlines, so a home ending in a newline
# would silently resolve to its stripped sibling. The daemon would not: refuse.
case "$_home" in
    *"
"*)
        echo "[$SELF] REFUSED: ENGRAM_HOME contains a newline; this verb will not guess which directory was meant. Nothing was touched." >&2
        exit 2
        ;;
esac
# A RELATIVE home is joined with the PHYSICAL cwd: the daemon's os.path.abspath
# uses os.getcwd(), which resolves symlinks, whereas $PWD / a bare `pwd` give
# the logical path an operator reached the directory through.
case "$_home" in
    /*) ;;
    *) _home="$(pwd -P)/$_home" ;;
esac
# From here _home is absolute. `cd -L` + `pwd` normalise ./ ../ // LEXICALLY and
# do not resolve symlinks inside the path, exactly like abspath.
if _abs="$(cd -- "$_home" 2>/dev/null && pwd)"; then
    _home="$_abs"
else
    # directory absent: strip trailing slashes without touching the filesystem
    while [ "${_home%/}" != "$_home" ] && [ "$_home" != "/" ]; do
        _home="${_home%/}"
    done
fi
SOCKET_PATH="$_home/recall-daemon.sock"
PID_PATH="$_home/recall-daemon.pid"

if [ "$PRINT_PATHS" -eq 1 ]; then
    printf 'SOCKET_PATH=%s\n' "$SOCKET_PATH"
    printf 'PID_PATH=%s\n' "$PID_PATH"
    exit 0
fi

# ---- K3: classify both paths BEFORE touching either -------------------------
# state: absent | ok | bad ; on bad, why holds the reason.
SOCK_STATE=absent
SOCK_WHY=""
if [ -L "$SOCKET_PATH" ]; then
    SOCK_STATE=bad; SOCK_WHY="is a symlink"
elif [ -S "$SOCKET_PATH" ]; then
    SOCK_STATE=ok
elif [ -e "$SOCKET_PATH" ]; then
    SOCK_STATE=bad; SOCK_WHY="exists but is not a socket"
fi

PID_STATE=absent
PID_WHY=""
if [ -L "$PID_PATH" ]; then
    PID_STATE=bad; PID_WHY="is a symlink"
elif [ -f "$PID_PATH" ]; then
    PID_STATE=ok
elif [ -e "$PID_PATH" ]; then
    PID_STATE=bad; PID_WHY="exists but is not a regular file"
fi

if [ "$SOCK_STATE" = bad ] || [ "$PID_STATE" = bad ]; then
    [ "$SOCK_STATE" = bad ] && echo "[$SELF] REFUSED: socket path $SOCKET_PATH $SOCK_WHY" >&2
    [ "$PID_STATE" = bad ] && echo "[$SELF] REFUSED: pidfile path $PID_PATH $PID_WHY" >&2
    echo "[$SELF] refusing to unlink anything (all-or-nothing); nothing was removed" >&2
    exit 1
fi

if [ "$SOCK_STATE" = absent ] && [ "$PID_STATE" = absent ]; then
    echo "[$SELF] nothing to clear: neither $SOCKET_PATH nor $PID_PATH exists; nothing was touched" >&2
    exit 1
fi

# ---- K4: the one loud line naming the invoker + the warning -----------------
_user="$(id -un 2>/dev/null || echo unknown)"
_tty="$(tty 2>/dev/null)" || true
case "$_tty" in
    /*) ;;
    *) _tty="not a tty" ;;
esac
_parent="$(ps -o comm= -p "$PPID" 2>/dev/null)" || true
[ -n "$_parent" ] || _parent="unknown"
echo "[$SELF] WARNING: run by user=$_user tty=$_tty parent=$_parent pid=$$; no-holder safety is unproven; this is the human's deliberate recovery decision, never automatic cleanup" >&2

# ---- K5: informational only -- NEVER used to decide anything ----------------
# Scoped like start-engram-daemon.sh's _reap_pattern: the daemon's launch argv
# ends with its ENGRAM_HOME, so a daemon of ANOTHER home is not listed. The home
# is ERE-escaped here (the start script does not escape).
_esc="$(printf '%s\n' "$_home" | sed 's/[][\.*^$+?(){}|\\]/\\&/g')"
_pat='engram-surface-daemon\.py.*'"$_esc"'$'
_procs="$(pgrep -u "$(id -u)" -f "$_pat" 2>/dev/null | tr '\n' ' ')" || true
echo "[$SELF] CONFIRM before clearing that no engram-surface-daemon of THIS user is running for ENGRAM_HOME=$_home (pgrep scoped to that home, information only, never used to decide: ${_procs:-none listed})" >&2

_would="socket $SOCKET_PATH"
[ "$SOCK_STATE" = ok ] || _would="socket (already absent)"
_would2="pidfile $PID_PATH"
[ "$PID_STATE" = ok ] || _would2="pidfile (already absent)"

if [ "$YES" -ne 1 ]; then
    echo "[$SELF] would remove: $_would; $_would2" >&2
    echo "[$SELF] nothing removed. Re-run with --yes to confirm this deliberate recovery." >&2
    exit 2
fi

# ---- unlink exactly the verified paths --------------------------------------
_removed=""
_failed=0
if [ "$SOCK_STATE" = ok ]; then
    if rm -f -- "$SOCKET_PATH"; then
        _removed="$_removed socket $SOCKET_PATH;"
    else
        echo "[$SELF] FAILED to remove socket $SOCKET_PATH" >&2
        _failed=1
    fi
fi
if [ "$PID_STATE" = ok ]; then
    if rm -f -- "$PID_PATH"; then
        _removed="$_removed pidfile $PID_PATH;"
    else
        echo "[$SELF] FAILED to remove pidfile $PID_PATH" >&2
        _failed=1
    fi
fi

_absent=""
[ "$SOCK_STATE" = absent ] && _absent="$_absent socket was already absent;"
[ "$PID_STATE" = absent ] && _absent="$_absent pidfile was already absent;"

echo "[$SELF] removed:${_removed:- nothing;}${_absent}" >&2
exit "$_failed"
