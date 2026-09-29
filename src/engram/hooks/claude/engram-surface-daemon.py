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


def _resolve_engram_home() -> str:
    """Per-install data dir: knowledge.db, sockets, briefing, history.

    Always returns an ABSOLUTE path (gh#2695): a relative ENGRAM_HOME would
    make this process bind its AF_UNIX socket with a relative path string,
    so /proc/net/unix records the bind exactly as passed to bind() --
    relative text. start-engram-daemon.sh's unit-2 ownership guard
    (#2652 phase-0) always compares against its own absolute $SOCKET_PATH
    (built from whatever ENGRAM_HOME the HOOK resolved, which may differ in
    absolute-vs-relative form and cwd from this process), so a relative
    bind means the guard's path-text match finds zero rows, the guard
    concludes "no live holder", and fails OPEN -- allowing exactly the
    delete it exists to prevent. abspath() (not realpath()) is used
    deliberately: it makes the path absolute relative to cwd without
    resolving symlinks, which matches the existing expanduser-only
    behavior for the already-absolute common case (an intermediate
    symlinked ENGRAM_HOME component would keep resolving the same way it
    always has -- realpath would be a second, unrelated behavior change
    bundled into this fix).
    """
    return os.path.abspath(
        os.environ.get("ENGRAM_HOME")
        or os.path.expanduser("~/.engram")
    )


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

PROJECT_DIR = resolve_runtime_dir(ENGRAM_HOME, "engram_client.py")
SOCKET_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.sock")
PID_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.pid")

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
                with open(tombstone_path, "w") as f:
                    f.write(str(int(time.time())))
            except OSError:
                pass
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
        with open(PID_PATH) as f:
            still_ours = int(f.read().strip()) == os.getpid()
    except (OSError, ValueError):
        # No pidfile, or unreadable/garbage content -- nothing to protect
        # for the PIDFILE, proceed with removing IT (matches the prior
        # unconditional behavior for this case). Says nothing about the
        # socket -- see _bound_socket above.
        still_ours = True
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
    """Return True if PID_PATH names a process that is still running.

    False for: PID_PATH absent, unreadable, non-integer content, or a PID
    that signal 0 confirms is dead. Best-effort -- any error means "assume
    dead" so a corrupt pidfile never blocks a fresh daemon from starting.
    """
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Any other errno (e.g. EPERM on a reused PID owned by someone
        # else) -- treat as "not confirmably ours", same as dead.
        return False
    return True


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

    # Write PID file
    os.makedirs(os.path.dirname(PID_PATH), exist_ok=True)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

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
