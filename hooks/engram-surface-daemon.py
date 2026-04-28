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


def _resolve_runtime_dir() -> str:
    """Locate the engram runtime snapshot (server.py + engram_client.py).

    The alpha installer copies these into $ENGRAM_HOME — that snapshot is
    the canonical import source. $ENGRAM_HOME is the env override;
    ~/.engram is the default.
    """
    return os.environ.get(
        "ENGRAM_HOME", os.path.expanduser("~/.engram")
    )


PROJECT_DIR = _resolve_runtime_dir()
ENGRAM_HOME = PROJECT_DIR  # Per-install runtime+data root; multi-tenant via $ENGRAM_HOME
SOCKET_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.sock")
PID_PATH = os.path.join(ENGRAM_HOME, "recall-daemon.pid")

# Bridge: server.py reads ENGRAM_DATA_DIR; if only ENGRAM_HOME was set
# (e.g., per-surface hook env), inherit the same value so the embedded
# EngramClient opens the right knowledge.db.
os.environ.setdefault("ENGRAM_DATA_DIR", ENGRAM_HOME)

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
