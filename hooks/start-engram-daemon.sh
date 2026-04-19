#!/bin/bash
# SessionStart hook: ensure the engram surface daemon is running.
# If already running, this is a no-op.

SOCKET_PATH="$HOME/.engram/recall-daemon.sock"
PID_PATH="$HOME/.engram/recall-daemon.pid"
DAEMON_SCRIPT="$(dirname "$0")/engram-surface-daemon.py"
LOG_PATH="$HOME/.engram/surface-daemon.log"

# Check if daemon is already running
if [ -f "$PID_PATH" ]; then
    pid=$(cat "$PID_PATH")
    if kill -0 "$pid" 2>/dev/null; then
        # Daemon is running, verify socket is responsive
        if {{PYTHON}} -c "
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect('$SOCKET_PATH')
    s.close()
    sys.exit(0)
except:
    sys.exit(1)
" 2>/dev/null; then
            # Daemon is alive and socket works
            exit 0
        fi
        # Socket not responsive, kill stale process
        kill "$pid" 2>/dev/null
    fi
    rm -f "$PID_PATH" "$SOCKET_PATH"
fi

# Start daemon in background
mkdir -p "$HOME/.engram"
nohup {{PYTHON}} "$DAEMON_SCRIPT" >> "$LOG_PATH" 2>&1 &

# Wait briefly for socket to appear
for i in $(seq 1 20); do
    if [ -S "$SOCKET_PATH" ]; then
        exit 0
    fi
    sleep 0.5
done

# Daemon didn't start in time — non-blocking, hook will fall back to FTS
echo '{"warning": "engram surface daemon did not start within 10s"}' >&2
exit 0
