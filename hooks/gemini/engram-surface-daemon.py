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

IDLE_TIMEOUT = 28800  # 8 hours without queries -> exit


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
      2. $ENGRAM_HOME if it bundles a snapshot (alpha-install pattern —
         install-gemini.sh copies server.py + engram_client.py into the
         data dir).
      3. ~/engram-alpha (live-source fallback for dev installs that
         haven't bundled a snapshot into $ENGRAM_HOME).
    """
    explicit = os.environ.get("ENGRAM_RUNTIME_DIR")
    if explicit:
        return explicit
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

        if not query:
            response = {"status": "ok", "result": {}}
        else:
            result = _client.call("engram_surface", {"query": query, "top_k": top_k, "semantic": True})
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


def idle_watchdog():
    """Exit if no queries received within IDLE_TIMEOUT."""
    while not _shutdown.is_set():
        elapsed = time.time() - _last_activity
        if elapsed > IDLE_TIMEOUT:
            print(f"[engram-daemon] Idle for {IDLE_TIMEOUT}s, shutting down.", file=sys.stderr)
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

    # Start idle watchdog
    watchdog = threading.Thread(target=idle_watchdog, daemon=True)
    watchdog.start()

    # Create Unix socket server
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
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
