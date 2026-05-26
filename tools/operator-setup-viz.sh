#!/usr/bin/env bash
# tools/operator-setup-viz.sh — Install the ENGRAM viz server as a per-operator
# systemd user service (systemctl --user), owned by and running as the human
# operator.
#
# This replaces the legacy system-level engram-viz.service (which ran as
# agent-borges and could not write peer agents' configs). The --user service
# runs under the operator's own UID so config writes work without extra
# privilege delegation.
#
# Usage:
#   tools/operator-setup-viz.sh [SOURCE_DIR]
#
#   SOURCE_DIR — optional. Directory containing viz_server.py to copy into
#                ~/.engram-viz/. Defaults to the directory containing this
#                script's parent (the repo root, i.e. "../" relative to
#                tools/).
#
#                NOTE: This default assumes the operator runs the script from
#                an engram-alpha repo clone. For stricter source-pinning (e.g.
#                a fixed versioned install path or a release tarball), pass
#                SOURCE_DIR explicitly. Stricter pinning is Lei's call — the
#                default keeps setup simple for contributors.
#
# What this script does:
#   1. Installs/refreshes the canonical viz copy into ~/.engram-viz/.
#      Copies viz_server.py and the tools/ sub-package it needs
#      (tools/config_schema.py + tools/__init__.py) from SOURCE_DIR.
#   2. Detects the Python interpreter (prefers ~/.engram-venv if available).
#   3. Renders the engram-viz-user.service.template and installs it into
#      ~/.config/systemd/user/.
#   4. Runs daemon-reload, enable, start via systemctl --user.
#   5. Suggests `sudo loginctl enable-linger $USER` for persistence across
#      logout.
#
# Idempotent: re-running overwrites the viz copy and re-enables the service
# harmlessly (systemctl enable is a no-op on an already-enabled unit).
#
# Requirements:
#   - systemd-user bus must be active (systemctl --user is-system-running or
#     status default.target).  Non-systemd envs (macOS, WSL1) are unsupported
#     by this script — use the legacy system service there.
#   - /etc/engram-viz/agents.json must exist (created separately by Lei /
#     Borges during the viz cutover sequence).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Source directory (where to copy viz_server.py from)
# ---------------------------------------------------------------------------
SOURCE_DIR="${1:-$REPO_ROOT}"

if [[ ! -f "$SOURCE_DIR/viz_server.py" ]]; then
  echo "ERROR: viz_server.py not found in SOURCE_DIR: $SOURCE_DIR" >&2
  echo "       Pass the path to an engram-alpha repo clone as the first argument," >&2
  echo "       or run this script from within such a clone (so the default applies)." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Canonical viz install location
# ---------------------------------------------------------------------------
VIZ_HOME="$HOME/.engram-viz"

# ---------------------------------------------------------------------------
# Python interpreter — prefer per-user venv, fall back to system python3
# ---------------------------------------------------------------------------
HOME_VENV_PYTHON="$HOME/.engram-venv/bin/python3"
if [[ -x "$HOME_VENV_PYTHON" ]]; then
  PYTHON_BIN="$HOME_VENV_PYTHON"
else
  PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || echo "")"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: python3 not found. Install Python 3.10+ or create a venv at ~/.engram-venv." >&2
  exit 1
fi

echo "=== ENGRAM viz operator setup ==="
echo "  source:    $SOURCE_DIR"
echo "  viz home:  $VIZ_HOME"
echo "  python:    $PYTHON_BIN"
echo

# ---------------------------------------------------------------------------
# Step 1: Install/refresh canonical viz copy into ~/.engram-viz/
# ---------------------------------------------------------------------------
echo "Step 1: Installing viz copy → $VIZ_HOME"

mkdir -p "$VIZ_HOME"
mkdir -p "$VIZ_HOME/tools"

# Core server file
cp "$SOURCE_DIR/viz_server.py" "$VIZ_HOME/viz_server.py"
echo "  viz_server.py → $VIZ_HOME/viz_server.py"

# tools/config_schema.py — imported by viz_server.py for the config-UI tier.
# Also copies tools/__init__.py so Python treats tools/ as a package.
# viz_server.py falls back gracefully if config_schema is absent, but the
# config tab degrades — copy it so the full feature set is available.
if [[ -f "$SOURCE_DIR/tools/config_schema.py" ]]; then
  cp "$SOURCE_DIR/tools/config_schema.py" "$VIZ_HOME/tools/config_schema.py"
  echo "  tools/config_schema.py → $VIZ_HOME/tools/config_schema.py"
else
  echo "  (tools/config_schema.py not found in source — config tab will degrade gracefully)"
fi

if [[ -f "$SOURCE_DIR/tools/__init__.py" ]]; then
  cp "$SOURCE_DIR/tools/__init__.py" "$VIZ_HOME/tools/__init__.py"
  echo "  tools/__init__.py → $VIZ_HOME/tools/__init__.py"
else
  # Create an empty __init__.py so tools/ is importable as a package
  touch "$VIZ_HOME/tools/__init__.py"
  echo "  tools/__init__.py → $VIZ_HOME/tools/__init__.py (created empty)"
fi

# ---------------------------------------------------------------------------
# Step 2: Check systemd-user availability
# ---------------------------------------------------------------------------
echo
echo "Step 2: Checking systemd-user availability"

if ! command -v systemctl &>/dev/null; then
  echo "ERROR: systemctl not found. This script requires systemd." >&2
  echo "       For non-systemd environments, manage viz_server.py manually." >&2
  exit 1
fi

if ! { systemctl --user --quiet is-system-running 2>/dev/null || \
       systemctl --user --quiet status default.target 2>/dev/null; }; then
  echo "ERROR: systemd-user bus is not active for $USER." >&2
  echo "       Ensure you are in a user session with systemd-user running." >&2
  echo "       On WSL2: run 'systemctl --user status' to diagnose." >&2
  exit 1
fi

echo "  systemd-user bus: active"

# ---------------------------------------------------------------------------
# Step 3: Render and install the systemd user unit
# ---------------------------------------------------------------------------
echo
echo "Step 3: Installing systemd user unit"

# Unit TEMPLATE always comes from the repo clone (REPO_ROOT), not SOURCE_DIR —
# SOURCE_DIR is the viz_server.py source, which the operator may override to a
# pinned install path; the template is repo-infrastructure, not user-overridable.
SERVICE_TEMPLATE="$REPO_ROOT/templates/engram-viz-user.service.template"
SERVICE_NAME="engram-viz-user.service"
SERVICE_DEST="$HOME/.config/systemd/user/$SERVICE_NAME"

if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
  echo "ERROR: unit template not found: $SERVICE_TEMPLATE" >&2
  echo "       Run this script from within an engram-alpha repo clone." >&2
  exit 1
fi

mkdir -p "$(dirname "$SERVICE_DEST")"

sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    -e "s|{{ENGRAM_VIZ_HOME}}|$VIZ_HOME|g" \
    "$SERVICE_TEMPLATE" > "$SERVICE_DEST"

echo "  unit → $SERVICE_DEST"

# ---------------------------------------------------------------------------
# Step 4: daemon-reload + enable + start
# ---------------------------------------------------------------------------
echo
echo "Step 4: Enabling and starting the service"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
# restart (not start): a re-run refreshes the ~/.engram-viz copy; restart loads the new binary.
systemctl --user restart "$SERVICE_NAME"

echo "  systemd user service: enabled + started"
echo "  Status: $(systemctl --user is-active "$SERVICE_NAME")"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo
echo "=== Setup complete ==="
echo "  Viz server running at: http://127.0.0.1:5001"
echo "  Service:               systemctl --user status $SERVICE_NAME"
echo "  Logs:                  journalctl --user -u $SERVICE_NAME -f"
echo
echo "  Note: For service persistence across user logout, run:"
echo "        sudo loginctl enable-linger $USER"
echo
echo "  Config file expected at: /etc/engram-viz/agents.json"
echo "  (Create this as root before the service can serve multi-agent mode.)"
