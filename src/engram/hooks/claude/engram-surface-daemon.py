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
    """Per-install data dir: knowledge.db, sockets, briefing, history."""
    return (
        os.environ.get("ENGRAM_HOME")
        or os.path.expanduser("~/.engram")
    )


def _resolve_runtime_dir(engram_home: str) -> str:
    """Locate where engram_client.py lives for import.

    Priority:
      1. $ENGRAM_RUNTIME_DIR if set explicitly.
      2. Plugin root: hook lives at <plugin_root>/hooks/hook.py (flat layout —
         tools/build-plugin.sh copies hooks into <plugin_root>/hooks/ without
         a platform subdir), so the plugin root is two dirname() levels up
         from __file__. engram_client.py lives at <plugin_root>/. The plugin
         bundle is the canonical runtime; when present it MUST win so a stale
         data-dir snapshot can never shadow it (fixes #1152: scatter cleanup
         can leave ~/.engram/engram_client.py that does `import server` against
         a removed ~/.engram/server.py, crash-looping the daemon).
      3. $ENGRAM_HOME if it bundles a snapshot (scatter-install fallback only —
         reached only when there is no plugin bundle; covers scatter installs
         that copy engram_client.py into the data dir).
      4. ~/engram-alpha (live-source fallback for dev installs).
    """
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
    plugin_root = os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    if os.path.exists(os.path.join(plugin_root, "engram_client.py")):
        return plugin_root
    if os.path.exists(os.path.join(engram_home, "engram_client.py")):
        return engram_home
    return os.path.expanduser("~/engram-alpha")


ENGRAM_HOME = _resolve_engram_home()
PROJECT_DIR = _resolve_runtime_dir(ENGRAM_HOME)
SOCKET_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.sock")
PID_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.pid")

# Bridge for downstream callers: ensure ENGRAM_HOME is set so any sibling process inherits it.
os.environ.setdefault("ENGRAM_HOME", ENGRAM_HOME)

_last_activity = time.time()
_shutdown = threading.Event()


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
    """Remove socket and PID files on exit."""
    _shutdown.set()
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    try:
        os.unlink(PID_PATH)
    except FileNotFoundError:
        pass
    sys.exit(0)


def main():
    global _client

    # Remove stale socket
    if os.path.exists(SOCKET_PATH):
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
