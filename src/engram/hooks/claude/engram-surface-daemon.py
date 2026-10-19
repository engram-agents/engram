#!/usr/bin/env python3
"""
Persistent ENGRAM recall daemon.

Keeps the sentence-transformers model loaded in memory and serves
engram_surface requests over a Unix socket. Started by SessionStart hook,
auto-exits after idle timeout.

Protocol (newline-delimited JSON):
  Request:  {"query": "...", "top_k": 10}
  Response: {"status": "ok", "result": {...}}  (engram_surface output)
            {"status": "error", "message": "..."}
"""

import json
import os
import signal
import socket
import sys
import threading
import time

IDLE_TIMEOUT = 28800  # 8 hours without queries -> exit (default)


def _resolve_idle_timeout() -> int:
    """Return the idle-shutdown timeout in seconds.

    Reads ENGRAM_DAEMON_IDLE_TIMEOUT from the environment:
      - Unset or empty        → default 28800 (8 h), same behaviour as before.
      - Valid positive integer → use that value.
      - 0 or negative         → disable idle-shutdown (persistent-service mode).
      - Non-integer garbage   → fall back to 28800; do not crash.
    """
    raw = os.environ.get("ENGRAM_DAEMON_IDLE_TIMEOUT", "")
    if not raw:
        return IDLE_TIMEOUT
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return IDLE_TIMEOUT
    return val  # 0 / negative → caller interprets as "disabled"

# ---------------------------------------------------------------------------
# Indexer ownership (issue #861)
#
# The JSONL→index.db ingestion pass (engram_log_indexer.Indexer) had no
# owning process: hooks wrote JSONL, server.py wrote tool events directly
# into index.db, viz_server read index.db read-only, but nobody ran the
# indexer. Hook/surface stats died silently on 2026-05-17 when the last
# manual run aged out of the 7-day stat window.
#
# Fix: this daemon — long-lived, per-agent, essential-tier — owns a periodic
# incremental pass. viz_server stays read-only. server.py continues writing
# its own events directly.
#
# The pass runs in a dedicated daemon thread so the accept loop (primary
# recall-serving path) is never blocked or delayed by an indexer run.
# ---------------------------------------------------------------------------

def _parse_indexer_interval() -> int:
    """Return the indexer tick interval in seconds.

    Reads ENGRAM_INDEXER_INTERVAL_S from the environment; falls back to 60
    on any bad value (non-integer, negative, zero).
    """
    raw = os.environ.get("ENGRAM_INDEXER_INTERVAL_S", "")
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return 60


def run_indexer_tick(last_run_ts: float, interval_s: int) -> float:
    """Run one incremental indexer pass if the interval has elapsed.

    Returns the updated last_run_ts (either the original value if the
    interval has not elapsed, or `now` — the monotonic timestamp captured at
    the gate check — after a successful or failed pass).

    Failure contract: any exception from Indexer.run_once() is caught,
    logged to stderr (one line), and suppressed — the daemon must never
    be killed or delayed by an indexer failure.
    """
    now = time.monotonic()
    if now - last_run_ts < interval_s:
        return last_run_ts

    try:
        from engram_log_indexer import Indexer
        result = Indexer.run_once()
        if result.get("rows_inserted", 0) > 0:
            print(
                f"[engram-daemon] indexer tick: {result['files_processed']} file(s), "
                f"{result['rows_inserted']} row(s) inserted",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[engram-daemon] indexer tick failed (suppressed): {exc}", file=sys.stderr)

    # Step 2 (#3135, telemetry WS6): the daemon owns the records.db ingest.
    # Independent of step 1 -- its own try, so neither step can skip the other.
    # engram_telemetry_store ships at tier=convenience while this daemon is
    # essential-tier, so the import is guarded (same shape as the #2574 WS5
    # guard in server.py; tests/test_tiers_manifest.py::
    # test_import_dep_tier_consistency enforces it). Unavailable == the
    # designed Essential-only state: skip silently.
    try:
        try:
            from engram_telemetry_store import ingest as _telemetry_ingest
        except Exception:  # pragma: no cover -- Essential-only build
            _telemetry_ingest = None
        tdir = os.path.join(ENGRAM_HOME, "telemetry")
        # No telemetry/ dir == this seat has written nothing yet (fresh
        # install); ingest() would raise on it every tick, so skip quietly.
        if _telemetry_ingest is not None and os.path.isdir(tdir):
            summary = _telemetry_ingest(os.path.join(tdir, "records.db"), tdir)
            errors = summary.get("errors") or []
            if (
                summary.get("records_ingested", 0) > 0
                or summary.get("files_swept", 0) > 0
                or errors
            ):
                line = (
                    f"[engram-daemon] telemetry tick: {summary.get('files_processed', 0)} file(s), "
                    f"{summary.get('records_ingested', 0)} record(s), "
                    f"{summary.get('files_swept', 0)} swept"
                )
                if errors:
                    line += f", {len(errors)} error(s)"
                print(line, file=sys.stderr)
    except Exception as exc:
        print(f"[engram-daemon] telemetry ingest failed (suppressed): {exc}", file=sys.stderr)

    return now


def _indexer_loop(interval_s: int, shutdown_event: threading.Event) -> None:
    """Daemon-thread target: fire run_indexer_tick every interval_s seconds.

    Runs as a daemon thread so it does not prevent the process from exiting.
    The thread wakes once per second to check shutdown, but only fires the
    indexer pass when the interval has elapsed — minimising unnecessary wakeups
    while still responding to shutdown within ~1s.
    """
    last_run_ts: float = float("-inf")  # force an immediate pass on first tick
    while not shutdown_event.is_set():
        last_run_ts = run_indexer_tick(last_run_ts, interval_s)
        shutdown_event.wait(timeout=1.0)


_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)
from _hooklib import engram_home_or_exit  # noqa: E402


def _resolve_engram_home() -> str:
    """Per-install data dir: knowledge.db, sockets, briefing, history.

    Always an ABSOLUTE path, or this process exits: a RELATIVE ENGRAM_HOME is
    REFUSED (alpha #3863 6b; engram_home_or_exit() prints one stderr line and
    exits 0 before anything binds). Why it matters (gh#2695): a relative home
    would make this process bind its AF_UNIX socket with a relative path
    string, so /proc/net/unix records relative text, while
    start-engram-daemon.sh's unit-2 ownership guard (#2652 phase-0) compares an
    absolute $SOCKET_PATH, finds zero rows, concludes "no live holder" and
    fails OPEN, allowing the delete it exists to prevent. Refusing the value
    makes that structurally impossible; tests/test_resolve_engram_home_abspath.py
    pins both the refusal and the pre-fix control that reproduces the bug.
    """
    return str(engram_home_or_exit())


ENGRAM_HOME = _resolve_engram_home()

# _resolve_runtime_dir hoisted to _hooklib.resolve_runtime_dir (gh#2570 --
# the maintainer's own count correction: this hook's local copy was an
# accidental omission from the original three-hook enumeration, identical
# in signature/body to engram-session-start-hook.py's pre-migration copy,
# not a fourth variant). This hook's own sentinel, engram_client.py, is
# preserved unchanged. This file's own directory is NOT already on
# sys.path (unlike hooks that self-insert it for other reasons), and
# several tests load this module directly via
# importlib.util.spec_from_file_location (test_surface_daemon_indexer.py,
# test_surface_daemon_idle_timeout.py, test_surface_daemon_pid_reap.py,
# test_pgrep_liveness_reap_patterns.py), which does not implicitly add the
# hook's directory to sys.path the way a subprocess invocation does --
# insert explicitly so both invocation styles resolve _hooklib the same
# way (same fix applied to engram-deference-detector-prompt.py in this
# same change).
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import resolve_runtime_dir
# #3628: the daemon's file opens are non-blocking and regular-file-only (a FIFO
# planted at a daemon-owned path must not hang it); the helpers live in _hooklib.
from _hooklib import open_regular_text, open_regular_text_write

PROJECT_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_client.py")
_PIDFILE_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_pidfile.py")
SOCKET_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.sock")
PID_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.pid")
# argv substring of a live daemon (its script name): the program-identity
# guard in engram_pidfile (#3240).
_PIDFILE_PROGRAM_MARKER = "engram-surface-daemon"

# Bridge for downstream callers: ensure ENGRAM_HOME is set so any sibling process inherits it.
os.environ.setdefault("ENGRAM_HOME", ENGRAM_HOME)

_last_activity = time.time()
_shutdown = threading.Event()

# Socket-ownership tracking (#2713 -- the daemon-side sibling of #2687's
# hook-side socket-ownership guard). Set to True ONLY after this process's
# own server.bind(SOCKET_PATH) succeeds in main(). cleanup()'s SOCKET_PATH
# unlink is gated on THIS flag, independent of PID_PATH identity -- pidfile
# identity answers "did I write the pidfile", not "did I bind the socket",
# and on a two-daemon seat those two questions have different answers: the
# hook-launched instance can write the pidfile while a DIFFERENT,
# supervisor-launched instance holds the actual socket. Unlinking
# SOCKET_PATH on pidfile identity alone deletes the live holder's socket
# out from under it.
_bound_socket = False


def handle_client(conn, client):
    """Handle a single client connection."""
    global _last_activity
    _last_activity = time.time()

    try:
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break

        if not data.strip():
            return

        request = json.loads(data.decode("utf-8").strip())
        query = request.get("query", "")
        top_k = request.get("top_k", 10)
        # Optional separate semantic query (auto-surface prepending for short
        # prompts — alpha #177 area 1). When omitted/None the server defaults
        # to query for both FTS and semantic (backward compat).
        embed_query = request.get("embed_query")

        if not query:
            response = {"status": "ok", "result": {}}
        else:
            args = {"query": query, "top_k": top_k, "semantic": True}
            if embed_query is not None:
                args["embed_query"] = embed_query
            # Render-layer habituation decay (#257 rec 1 / extends #1700)
            # needs this prompt's own embedding to compare against the one
            # stored at a node's last render — opt-in flag so ordinary
            # engram_surface calls (from an agent, not this daemon) don't pay
            # for or receive the raw vector.
            args["include_query_embedding"] = True
            result = _client.call("engram_surface", args)
            response = {"status": "ok", "result": result}

        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

    except Exception as e:
        try:
            error_resp = {"status": "error", "message": str(e)}
            conn.sendall((json.dumps(error_resp) + "\n").encode("utf-8"))
        except Exception:
            pass
    finally:
        conn.close()


def _is_non_regular(path) -> bool:
    """True only when `path` exists and is not a regular file (a FIFO, a
    directory...). #3628: the daemon's file opens log-and-skip ONLY for such a
    target; an OSError on a regular or missing file keeps its previous
    handling. Same predicate as engram-surface-hook.py's _is_non_regular; the
    two private copies are pinned by tests/test_hooks_sqlite_regular_3628s2d.py
    (test_is_non_regular_copies_have_identical_ast and
    test_is_non_regular_behaviour_is_the_same_in_both_copies)."""
    return os.path.exists(path) and not os.path.isfile(path)


def idle_watchdog(idle_timeout: int):
    """Exit if no queries received within idle_timeout seconds.

    When idle_timeout <= 0, idle-shutdown is disabled — the watchdog
    returns immediately without setting _shutdown (persistent-service mode).
    """
    if idle_timeout <= 0:
        return  # persistent-service mode: never self-exit on idle
    while not _shutdown.is_set():
        elapsed = time.time() - _last_activity
        if elapsed > idle_timeout:
            print(
                f"[engram-daemon] Idle for {idle_timeout}s, shutting down.",
                file=sys.stderr,
            )
            # Write idle-shutdown tombstone so the surface hook can give a softer
            # alarm on next startup rather than a generic CRITICAL (#1260).
            tombstone_path = os.path.join(ENGRAM_HOME, "daemon-idle-shutdown")
            try:
                with open_regular_text_write(tombstone_path) as f:
                    f.write(str(int(time.time())))
            except OSError:
                # A regular-file error (read-only dir, ENOSPC...) is still
                # swallowed silently. A FIFO/directory at the path is logged
                # once and skipped: the tombstone is advisory, shutdown goes on.
                if _is_non_regular(tombstone_path):
                    print(
                        f"[engram-daemon] {tombstone_path} is not a regular "
                        f"file; skipping the idle-shutdown tombstone.",
                        file=sys.stderr,
                    )
            _shutdown.set()
            break
        _shutdown.wait(timeout=60)  # check every minute


def cleanup(*args):
    """Remove socket and PID files on exit -- two independent resources,
    two independent predicates. Do not conflate them (#2713).

    PID_PATH: removed only if PID_PATH still names THIS process (#1874
    colleague-review suggestion). Without this guard, a delayed SIGTERM
    (this process was already superseded -- e.g. start-engram-daemon.sh's
    reap loop killed it, but it was slow to actually exit) could fire its
    cleanup AFTER a newer instance already wrote its own fresh PID file
    and bound its socket, deleting the NEW instance's files out from
    under it and reproducing the exact "unreachable-but-alive" shape
    #1874 reported, via a different mechanism than the one that PR
    actually fixes.

    SOCKET_PATH: removed only if THIS process actually bound it
    (`_bound_socket`, set in main() immediately after a successful
    server.bind()) -- independent of PID_PATH identity (#2713). Pidfile
    identity is a proxy for "did I bind the socket", and on a two-daemon
    seat the proxy is wrong: the hook-launched instance is the one that
    wrote the pidfile, but a DIFFERENT, supervisor-launched instance may
    be the one actually serving SOCKET_PATH. Gating the unlink on pidfile
    identity (or its absence -- "nothing to protect, proceed") deletes
    the live holder's socket out from under it even though this process
    never bound it. `_bound_socket` answers the only question that
    matters for this resource; PID_PATH's absence/unreadability is no
    longer sufficient, on its own, to unlink the socket.
    """
    _shutdown.set()
    still_ours = False
    try:
        with open_regular_text(PID_PATH) as f:
            still_ours = int(f.read().strip()) == os.getpid()
    except (OSError, ValueError):
        # No pidfile, or unreadable/garbage content -- nothing to protect
        # for the PIDFILE, proceed with removing IT (matches the prior
        # unconditional behavior for this case). Says nothing about the
        # socket -- see _bound_socket above.
        still_ours = True
        # #3628: a FIFO/directory at PID_PATH is NOT a file this process wrote,
        # so it is never unlinked: log once, leave it, keep going.
        if _is_non_regular(PID_PATH):
            still_ours = False
            print(
                f"[engram-daemon] {PID_PATH} is not a regular file; "
                f"leaving it in place.",
                file=sys.stderr,
            )
    if still_ours:
        try:
            os.unlink(PID_PATH)
        except FileNotFoundError:
            pass
    if _bound_socket:
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
    sys.exit(0)


def _recorded_pid_is_alive() -> bool:
    """Return True if PID_PATH names a live process that could have written
    it. Delegates to engram_pidfile.recorded_pid_is_alive() -- the single
    source shared with engramd_owner.py (#3240 item 1): a pid reused by an
    unrelated process after the instance crashed no longer reads as a live
    daemon. False for PID_PATH absent, unreadable, or non-integer content.
    """
    # Resolved via the runtime-dir mechanism (never a fixed parents[N]; the
    # plugin build flattens hooks/claude/ -> hooks/). PROJECT_DIR is put on
    # sys.path later in main() for engram_client; do it here too, since this
    # check runs first.
    if _PIDFILE_DIR not in sys.path:
        sys.path.insert(0, _PIDFILE_DIR)
    import engram_pidfile

    return engram_pidfile.recorded_pid_is_alive(
        PID_PATH, program_marker=_PIDFILE_PROGRAM_MARKER
    )


# #3361: startup gate exit codes (sysexits 75 = "temporary failure"), so a
# supervisor log reads "refused, did not start" rather than a crash. Distinct
# so a reader can tell "a live holder was positively identified" from "could
# not prove the path unheld". Never 0: exit 0 tells a supervisor all is well.
EXIT_SOCKET_HELD = 75       # another live process holds SOCKET_PATH
EXIT_SOCKET_UNPROVEN = 76   # could not prove SOCKET_PATH is unheld (fail closed)

# #3361: a refusal waits this long BEFORE exiting. Under systemd
# (Restart=always) and launchd (KeepAlive true) an immediate exit would
# relaunch at once and hot-loop; with the wait the refusal repeats about once a
# minute and still self-heals the moment the holder goes away. Deliberately a
# daemon-side wait, not RestartPreventExitStatus / ThrottleInterval: a
# prevented restart leaves the unit `failed`, which start-engram-daemon.sh
# reads as registered-failing and never relaunches, so the refusal would stick
# even after the holder died.
REFUSAL_BACKOFF_S = 60
_REFUSAL_BACKOFF_MAX_S = 3600


def _refusal_backoff_s() -> float:
    """Seconds to wait before a refusal exit.

    ENGRAM_DAEMON_REFUSAL_BACKOFF_S (float seconds) overrides REFUSAL_BACKOFF_S.
    Missing, empty, non-numeric, negative or non-finite falls back to the
    default silently (no extra log line). 0 means no wait. Capped at 3600.
    """
    raw = os.environ.get("ENGRAM_DAEMON_REFUSAL_BACKOFF_S", "")
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return REFUSAL_BACKOFF_S
    if not (val >= 0) or val == float("inf"):  # negative, nan, inf
        return REFUSAL_BACKOFF_S
    return min(val, _REFUSAL_BACKOFF_MAX_S)


def _wait_refusal_backoff(seconds: float) -> None:
    """Sleep `seconds` before a refusal exit.

    No signal handler is installed at the refusal point (the daemon's
    SIGTERM/SIGINT handlers come after the bind), so SIGTERM's default action
    ends a plain sleep at once and `systemctl stop` / a launchd unload are not
    delayed. SIGINT surfaces as KeyboardInterrupt; end the wait quietly so the
    refusal exit code is still returned rather than a traceback.
    """
    if seconds <= 0:
        return
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass


_LSOF_TIMEOUT_S = 5


def _pids_holding_inode(inode, proc_root):
    """Pids (ints) with an fd link `socket:[inode]` under proc_root/<pid>/fd.
    Unreadable pid dirs are skipped (permissions), so [] means "none we
    could see", never "none exist"."""
    target = "socket:[" + inode + "]"
    pids = set()
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        fd_dir = os.path.join(proc_root, name, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link == target:
                pids.add(int(name))
                break
    return sorted(pids)


def _holder_cwds(inode, proc_root):
    """cwd paths of every pid holding `inode`, or None when that cannot be
    established (no visible pid, or any holder's cwd unreadable): the caller
    must then treat a relative-path row as UNKNOWN, not as another file."""
    pids = _pids_holding_inode(inode, proc_root)
    if not pids:
        return None
    cwds = []
    for pid in pids:
        try:
            cwds.append(os.readlink(os.path.join(proc_root, str(pid), "cwd")))
        except OSError:
            return None
    return cwds


def _socket_holder_state(socket_path, proc_net_unix="/proc/net/unix", proc_root="/proc"):
    """Who holds `socket_path`? Returns (state, pids), state in HELD / FREE /
    UNKNOWN, pids a sorted list of ints (may be empty even when HELD).

    Positive identification only, fail CLOSED (#3361). Mirrors the shell
    guard in start-engram-daemon.sh (#3359):

    * Where `proc_net_unix` exists: each named row's PATH-TEXT column is
      classified against `socket_path` (see the comment above the loop:
      aliased and relative spellings are resolved, never compared as bare
      text; never by inode against stat(), never by substring). HELD even if
      no pid can be resolved (permissions); UNKNOWN when a row cannot be
      shown to be another file; FREE only when every row is provably not ours.
    * Else (macOS/BSD): `lsof -t -- <path>`. rc 0 with pid lines is HELD;
      every other outcome (rc 1, other rc, timeout, no lsof, no parsable pid)
      is UNKNOWN. `-U` is deliberately NOT passed: with a path argument lsof
      ORs the selections and would list every unix-socket holder on the host.

    A connect() attempt is never proof of absence: a socket that is bound but
    not listening answers ECONNREFUSED and is still a live holder.
    """
    if os.path.exists(proc_net_unix):
        try:
            with open(proc_net_unix, errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return ("UNKNOWN", [])
        # The kernel lists the BIND-TIME path text, which can be a different
        # spelling of our file (a symlinked parent directory, or a relative
        # bind). So each named row is CLASSIFIED against our file, never
        # just compared as text (#3361, #3490 security review):
        #   absolute P: P == socket_path or realpath(P) == realpath(ours)
        #     -> HELD. Else if P's directory resolves to a real directory
        #     and the realpaths differ, the row cannot be our file -> ignore
        #     (a same-basename decoy elsewhere). Else (directory missing,
        #     dangling symlink, unresolvable) and the basename matches ->
        #     UNKNOWN: we cannot show the row is not our file.
        #   relative P: a different basename -> ignore. Same basename: the
        #     meaning depends on the binder's cwd, so read
        #     /proc/<pid>/cwd of the pid(s) holding the row's inode;
        #     realpath(cwd/P) == realpath(ours) -> HELD, different ->
        #     ignore, no pid or an unreadable cwd -> UNKNOWN.
        #   Aggregate: any HELD -> HELD; else any UNKNOWN -> UNKNOWN; else
        #     FREE. Unnamed and abstract ('@') rows are skipped. Matching is
        #     exact path equality, never substring, and never a comparison of
        #     filesystem inodes against sockfs inodes.
        # Remaining limits, stated precisely (not claimed solved): the
        # kernel text is whatever the binder passed, so (1) a holder that
        # bound through a symlink that was RETARGETED after the bind, or a
        # relative-bind holder that chdir()ed afterwards, is classified by
        # where the text points NOW; (2) a hard link (link() on a socket
        # file) or a bind mount can make our path reach a socket whose row
        # text has a different directory AND basename. All three need a
        # local process that deliberately re-plumbs paths after binding.
        ours_real = os.path.realpath(socket_path)
        ours_base = os.path.basename(socket_path)
        held_inodes = set()
        unknown = False
        for line in lines:
            # 7 fixed columns then the path text (which may contain spaces).
            parts = line.rstrip("\n").split(None, 7)
            if len(parts) != 8:
                continue
            inode, text = parts[6], parts[7]
            if text.startswith("@"):
                continue
            if os.path.isabs(text):
                if text == socket_path or os.path.realpath(text) == ours_real:
                    held_inodes.add(inode)
                elif os.path.isdir(os.path.realpath(os.path.dirname(text))):
                    continue  # provably another file (a decoy)
                elif os.path.basename(text) == ours_base:
                    unknown = True
            elif os.path.basename(text) == ours_base:
                cwds = _holder_cwds(inode, proc_root)
                if not cwds:
                    unknown = True
                    continue
                for cwd in cwds:
                    if os.path.realpath(os.path.join(cwd, text)) == ours_real:
                        held_inodes.add(inode)
                        break
        if not held_inodes:
            return ("UNKNOWN" if unknown else "FREE", [])
        pids = set()
        for inode in held_inodes:
            pids.update(_pids_holding_inode(inode, proc_root))
        return ("HELD", sorted(pids))

    import subprocess

    try:
        r = subprocess.run(
            ["lsof", "-t", "--", socket_path],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ("UNKNOWN", [])
    if r.returncode != 0:
        return ("UNKNOWN", [])
    pids = sorted({int(t) for t in (r.stdout or "").split() if t.isdigit()})
    if not pids:
        return ("UNKNOWN", [])
    return ("HELD", pids)


def _write_pidfile():
    """Write this process's pid to PID_PATH. #3628: a FIFO/directory already at
    PID_PATH is logged once and the daemon runs without a pidfile (no crash, no
    hang); any other OSError -- e.g. a PermissionError on a regular file --
    propagates exactly as it did before."""
    os.makedirs(os.path.dirname(PID_PATH), exist_ok=True)
    try:
        with open_regular_text_write(PID_PATH) as f:
            f.write(str(os.getpid()))
    except OSError:
        if not _is_non_regular(PID_PATH):
            raise
        print(
            f"[engram-daemon] {PID_PATH} is not a regular file; "
            f"running without a pidfile.",
            file=sys.stderr,
        )


def main():
    global _client, _bound_socket

    # #1874: only remove the socket if the PID it was bound by is
    # confirmably dead. The prior unconditional unlink let a fresh daemon
    # silently steal the path out from under a still-alive prior instance
    # -- that instance keeps running (holding its open, now-unlinked file
    # descriptor and any resources it owns) but is no longer reachable via
    # the well-known path, which is exactly the "orphaned-but-alive"
    # shape #1874 found (9 simultaneous processes, only one ever actually
    # serving requests). start-engram-daemon.sh's own reap (same issue)
    # is the primary defense; this is the second, independent layer for
    # any other launch path that skips that script.
    if os.path.exists(SOCKET_PATH):
        if _recorded_pid_is_alive():
            # A confirmably-live prior instance still owns this socket.
            # start-engram-daemon.sh's reap should have killed it before
            # ever launching us, so reaching this path means a DIFFERENT
            # launcher started this process directly. Back off rather
            # than steal the path out from under a working daemon.
            print(
                "[engram-daemon] a live daemon already owns "
                f"{SOCKET_PATH} -- exiting rather than steal it",
                file=sys.stderr,
            )
            sys.exit(0)
        # #3361: the recorded pid is dead, but that does not mean nobody
        # holds the path (pidfile stale / missing / names another process).
        # Unlink only when the path is positively known to be unheld; a live
        # holder or an unprovable answer refuses loudly rather than steal.
        state, holders = _socket_holder_state(SOCKET_PATH)
        # Defensive: HELD with only our own pid is effectively unreachable
        # here, since we have not bound yet at gate time.
        others = [p for p in holders if p != os.getpid()]
        if state == "HELD" and (others or not holders):
            who = ",".join(str(p) for p in others) if others else "unknown"
            print(
                f"[engram-daemon] socket {SOCKET_PATH} is held by live "
                f"process(es) pid={who} -- refusing to steal it (exit "
                f"{EXIT_SOCKET_HELD})",
                file=sys.stderr,
                flush=True,
            )
            _wait_refusal_backoff(_refusal_backoff_s())
            sys.exit(EXIT_SOCKET_HELD)
        if state == "UNKNOWN":
            print(
                f"[engram-daemon] could not prove socket {SOCKET_PATH} is "
                "unheld (no /proc/net/unix answer and no usable lsof "
                "answer) -- refusing to remove it; if you know it is stale, "
                f"remove it by hand (exit {EXIT_SOCKET_UNPROVEN})",
                file=sys.stderr,
                flush=True,
            )
            _wait_refusal_backoff(_refusal_backoff_s())
            sys.exit(EXIT_SOCKET_UNPROVEN)
        # Two race windows, which are NOT the same:
        # (a) FREE -> os.unlink (microseconds): a non-daemon process that
        #     binds a path just proven stale can have its live socket
        #     unlinked here, after which our own bind() succeeds -- a steal.
        #     This gate narrows that window; it does not close it, and
        #     nothing short of an atomic bind-or-fail can.
        # (b) os.unlink -> our bind() (the model load, seconds): a process
        #     that binds in this window wins, and our bind() fails loudly
        #     with EADDRINUSE. bind() never unlinks: the only server.bind
        #     is the one in main(), and SOCKET_PATH is unlinked only in
        #     cleanup() and in this gate.
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass

    # Import and initialize KG client (this loads the embedding model)
    if PROJECT_DIR not in sys.path:
        sys.path.insert(0, PROJECT_DIR)

    from engram_client import EngramClient
    _client = EngramClient()

    # Force-load the embedding model now so first query is fast
    _client.call("engram_surface", {"query": "warmup", "top_k": 1, "semantic": True})

    _write_pidfile()

    # Set up signal handlers
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    # Resolve idle-shutdown timeout (env-gate: 0/negative → disabled)
    _idle_timeout = _resolve_idle_timeout()

    # Start idle watchdog (passes immediately when idle_timeout <= 0)
    watchdog = threading.Thread(
        target=idle_watchdog, args=(_idle_timeout,), daemon=True
    )
    watchdog.start()

    # Start periodic JSONL→index.db indexer (issue #861: daemon owns this pass)
    _indexer_interval = _parse_indexer_interval()
    indexer_thread = threading.Thread(
        target=_indexer_loop,
        args=(_indexer_interval, _shutdown),
        daemon=True,
    )
    indexer_thread.start()

    # Create Unix socket server
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    # Ownership flag flips ONLY after bind() returns successfully (#2713)
    # -- cleanup()'s SOCKET_PATH unlink is gated on this, not on pidfile
    # identity. Set immediately after the call that establishes ownership,
    # never before it (a mutation that flips this earlier would falsely
    # claim ownership for a bind() that actually failed/raised).
    _bound_socket = True
    # Remove any idle-shutdown tombstone — daemon is healthy and running (#1260).
    tombstone_path = os.path.join(ENGRAM_HOME, "daemon-idle-shutdown")
    try:
        os.unlink(tombstone_path)
    except FileNotFoundError:
        pass
    server.listen(5)
    server.settimeout(5.0)  # allow periodic shutdown checks

    print(f"[engram-daemon] Listening on {SOCKET_PATH} (PID {os.getpid()})", file=sys.stderr)

    try:
        while not _shutdown.is_set():
            try:
                conn, addr = server.accept()
                # Handle each client in a thread for safety, though
                # typically only one hook calls at a time
                t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
                t.start()
            except socket.timeout:
                continue
    finally:
        server.close()
        cleanup()


if __name__ == "__main__":
    main()
