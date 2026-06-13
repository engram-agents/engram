#!/usr/bin/env bash
# tools/setup-surface-daemon-service.sh — Install the ENGRAM surface daemon as a
# persistent OS service (systemd user service on Linux, LaunchAgent on macOS).
#
# The daemon will start at login and restart automatically on exit. The
# ENGRAM_DAEMON_IDLE_TIMEOUT=0 environment variable is baked into the service
# unit so the daemon never self-exits on idle (persistent-service mode).
#
# Usage:
#   tools/setup-surface-daemon-service.sh
#
# The script detects the OS via `uname -s`:
#   Linux  → renders engram-surface-daemon.service.template and installs it as a
#             systemd user service (~/.config/systemd/user/).
#   Darwin → renders engram-surface-daemon.plist.template and installs it as a
#             LaunchAgent (~/Library/LaunchAgents/).
#
# Idempotent: re-running re-renders the unit/plist and re-enables the service.
# Fails loudly if systemctl (Linux) or launchctl (macOS) is absent.
#
# Placeholder substitutions (matching both templates):
#   {{PYTHON}}           — canonical Python interpreter (prefers ~/.engram/venv)
#   {{ENGRAM_HOOKS_DIR}} — directory containing engram-surface-daemon.py
#   {{ENGRAM_HOME}}      — ENGRAM data directory (~/.engram if unset)
#
# Mirror of conventions from tools/operator-setup-viz.sh.
#
# Override / testing:
#   ENGRAM_HOOKS_DIR — if set, skips auto-resolution and uses this directory
#                      directly as HOOKS_DIR (also used by layer-b testing against
#                      the real deployed bundle).

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve script location and repo root (template source)
# ---------------------------------------------------------------------------
_self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
_script_dir="$(dirname "$_self")"

# Templates live alongside this script's parent bundle root.
# In a plugin install the layout is:
#   <plugin_root>/tools/setup-surface-daemon-service.sh  (this file)
#   <plugin_root>/templates/*.template
#   <plugin_root>/hooks/engram-surface-daemon.py         (deployed bundle — flat)
# In a repo-clone install, script_dir is src/engram/tools/ → step up two
# levels to reach src/engram/. Either way, the template dir is:
#   <script_dir>/../templates/
PLUGIN_ROOT="$(cd "$_script_dir/.." && pwd)"
TEMPLATE_DIR="$PLUGIN_ROOT/templates"

# ---------------------------------------------------------------------------
# Resolve HOOKS_DIR — where engram-surface-daemon.py lives.
#
# Precedence (first match wins):
#   1. ENGRAM_HOOKS_DIR env var (explicit override; also used for layer-b testing)
#   2. $PLUGIN_ROOT/hooks/engram-surface-daemon.py exists → flat bundle layout
#   3. $PLUGIN_ROOT/hooks/claude/engram-surface-daemon.py exists → source-tree layout
#   4. Neither found → fail loud (surface at setup-time, not as a silent broken service)
# ---------------------------------------------------------------------------
if [[ -n "${ENGRAM_HOOKS_DIR:-}" ]]; then
    HOOKS_DIR="$ENGRAM_HOOKS_DIR"
elif [[ -f "$PLUGIN_ROOT/hooks/engram-surface-daemon.py" ]]; then
    HOOKS_DIR="$PLUGIN_ROOT/hooks"
elif [[ -f "$PLUGIN_ROOT/hooks/claude/engram-surface-daemon.py" ]]; then
    HOOKS_DIR="$PLUGIN_ROOT/hooks/claude"
else
    echo "ERROR: could not locate engram-surface-daemon.py under $PLUGIN_ROOT/hooks or $PLUGIN_ROOT/hooks/claude" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"

# ---------------------------------------------------------------------------
# Python interpreter — prefer the ENGRAM venv, fall back to system python3
# ---------------------------------------------------------------------------
ENGRAM_VENV_PYTHON="$HOME/.engram/venv/bin/python3"
if [[ -x "$ENGRAM_VENV_PYTHON" ]]; then
    PYTHON_BIN="$ENGRAM_VENV_PYTHON"
else
    PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || echo "")"
fi

if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: python3 not found. Install Python 3.10+ or create a venv at ~/.engram/venv." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Validate the daemon script exists
# ---------------------------------------------------------------------------
DAEMON_PY="$HOOKS_DIR/engram-surface-daemon.py"
if [[ ! -f "$DAEMON_PY" ]]; then
    echo "ERROR: daemon script not found: $DAEMON_PY" >&2
    echo "       Run this script from within an engram-alpha repo clone or a" >&2
    echo "       plugin bundle where hooks/engram-surface-daemon.py exists." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
OS="$(uname -s)"

echo "=== ENGRAM surface daemon service setup ==="
echo "  OS:            $OS"
echo "  python:        $PYTHON_BIN"
echo "  hooks dir:     $HOOKS_DIR"
echo "  ENGRAM_HOME:   $ENGRAM_HOME"
echo

# ---------------------------------------------------------------------------
# Linux: systemd user service
# ---------------------------------------------------------------------------
if [[ "$OS" == "Linux" ]]; then
    if ! command -v systemctl &>/dev/null; then
        echo "ERROR: systemctl not found." >&2
        echo "       This script requires systemd on Linux." >&2
        echo "       On non-systemd Linux, manage the daemon manually:" >&2
        echo "         $PYTHON_BIN $DAEMON_PY" >&2
        exit 1
    fi

    SERVICE_TEMPLATE="$TEMPLATE_DIR/engram-surface-daemon.service.template"
    if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
        echo "ERROR: service template not found: $SERVICE_TEMPLATE" >&2
        exit 1
    fi

    SERVICE_NAME="engram-surface-daemon.service"
    SERVICE_DEST="$HOME/.config/systemd/user/$SERVICE_NAME"
    mkdir -p "$(dirname "$SERVICE_DEST")"

    echo "Step 1: Rendering systemd unit → $SERVICE_DEST"
    sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
        -e "s|{{ENGRAM_HOOKS_DIR}}|$HOOKS_DIR|g" \
        -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
        "$SERVICE_TEMPLATE" > "$SERVICE_DEST"
    echo "  done."

    echo
    echo "Step 2: Enabling and starting via systemctl --user"
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    echo "  Status: $(systemctl --user is-active "$SERVICE_NAME" 2>/dev/null || echo unknown)"

    echo
    echo "=== Setup complete ==="
    echo "  Check status:  systemctl --user status $SERVICE_NAME"
    echo "  Follow logs:   journalctl --user -u $SERVICE_NAME -f"
    echo "  Logs file:     $ENGRAM_HOME/surface-daemon.log"
    echo
    echo "  Note: For service persistence across user logout, run:"
    echo "        sudo loginctl enable-linger $USER"

# ---------------------------------------------------------------------------
# macOS: LaunchAgent
# ---------------------------------------------------------------------------
elif [[ "$OS" == "Darwin" ]]; then
    if ! command -v launchctl &>/dev/null; then
        echo "ERROR: launchctl not found." >&2
        echo "       This script requires launchctl on macOS." >&2
        echo "       Manage the daemon manually:" >&2
        echo "         $PYTHON_BIN $DAEMON_PY" >&2
        exit 1
    fi

    PLIST_TEMPLATE="$TEMPLATE_DIR/engram-surface-daemon.plist.template"
    if [[ ! -f "$PLIST_TEMPLATE" ]]; then
        echo "ERROR: plist template not found: $PLIST_TEMPLATE" >&2
        exit 1
    fi

    PLIST_LABEL="com.engram.surface-daemon"
    AGENTS_DIR="$HOME/Library/LaunchAgents"
    PLIST_DEST="$AGENTS_DIR/$PLIST_LABEL.plist"
    mkdir -p "$AGENTS_DIR"

    echo "Step 1: Rendering LaunchAgent plist → $PLIST_DEST"
    sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
        -e "s|{{ENGRAM_HOOKS_DIR}}|$HOOKS_DIR|g" \
        -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
        "$PLIST_TEMPLATE" > "$PLIST_DEST"
    echo "  done."

    echo
    echo "Step 2: Loading via launchctl"
    # Modern bootstrap/bootout API (macOS 10.11+); load/unload are Apple-deprecated.
    # bootout targets the service by domain/label; bootstrap takes the plist path.
    launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
    echo "  Loaded."

    echo
    echo "=== Setup complete ==="
    echo "  Check status:  launchctl list | grep engram"
    echo "  Logs file:     $ENGRAM_HOME/surface-daemon.log"

# ---------------------------------------------------------------------------
# Unsupported OS
# ---------------------------------------------------------------------------
else
    echo "ERROR: Unsupported OS: $OS" >&2
    echo "       This script supports Linux (systemd) and macOS (launchd)." >&2
    echo "       On other platforms, manage the daemon manually:" >&2
    echo "         $PYTHON_BIN $DAEMON_PY" >&2
    exit 1
fi
