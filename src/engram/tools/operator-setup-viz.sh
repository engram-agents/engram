#!/usr/bin/env bash
# tools/operator-setup-viz.sh — Install the ENGRAM viz server as a per-operator
# systemd user service (systemctl --user), owned by and running as the human
# operator.
#
# This replaces the legacy system-level engram-viz.service (which ran as a
# system account and could not write per-operator configs). The --user service
# runs under the operator's own UID so config writes work without extra
# privilege delegation.
#
# Run-in-place design (no copying):
#   viz_server.py, engram_stats.py, engram_core.py, and tools/config_schema.py
#   all ship TOGETHER in the plugin bundle. The service runs viz_server.py
#   directly from the plugin dir — no ~/.engram-viz/ copy, no stale-binary risk.
#   The plugin dir is resolved from this script's own real path (readlink -f
#   follows the ~/.local/bin/engram-viz-setup symlink back to the real location).
#
# Usage:
#   engram-viz-setup            # via symlink (created by this script)
#   tools/operator-setup-viz.sh # direct invocation
#   tools/operator-setup-viz.sh --dry-run  # print resolved paths + import check; don't install
#
# What this script does:
#   1. Resolves PLUGIN_ENGRAM_DIR from this script's own real path.
#   2. Detects the Python interpreter (prefers plugin venv ~/.engram/venv;
#      falls back to ~/.engram-venv / system python3 if import gate passes).
#   3. Runs an import-verification gate: refuses to install unless the canonical
#      health-score formula and config schema can be imported with the chosen
#      python + PLUGIN_ENGRAM_DIR on sys.path. Makes a silently-degraded viz
#      structurally impossible to install.
#   4. Renders the engram-viz-user.service.template and installs it into
#      ~/.config/systemd/user/.
#   5. Runs daemon-reload, enable, restart via systemctl --user.
#   6. Creates ~/.local/bin/engram-viz-setup symlink for convenience.
#   7. Suggests `sudo loginctl enable-linger $USER` for persistence across logout.
#
# Idempotent: re-running re-enables the service harmlessly (systemctl enable is
# a no-op on an already-enabled unit).
#
# Requirements:
#   - systemd-user bus must be active (systemctl --user is-system-running or
#     status default.target).  Non-systemd envs (macOS, WSL1) are unsupported
#     by this script — use the legacy system service there.
#   - /etc/engram-viz/agents.json must exist (created separately by Lei /
#     Borges during the viz cutover sequence).

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve this script's real location (follows ~/.local/bin symlink)
# ---------------------------------------------------------------------------
_self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"

# The script lives at <plugin>/engram/tools/operator-setup-viz.sh, so:
#   dirname($_self)        = <plugin>/engram/tools/
#   dirname(dirname(...))  = <plugin>/engram/
PLUGIN_ENGRAM_DIR="$(dirname "$(dirname "$_self")")"
VIZ_PY="$PLUGIN_ENGRAM_DIR/viz_server.py"

# ---------------------------------------------------------------------------
# --dry-run / --check flag
# ---------------------------------------------------------------------------
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" || "${1:-}" == "--check" ]]; then
  DRY_RUN=1
fi

# ---------------------------------------------------------------------------
# Verify viz_server.py is present (plugin bundle integrity check)
# ---------------------------------------------------------------------------
if [[ ! -f "$VIZ_PY" ]]; then
  echo "ERROR: viz_server.py not found in plugin bundle at: $VIZ_PY" >&2
  echo "       Expected location: $PLUGIN_ENGRAM_DIR/viz_server.py" >&2
  echo "       The plugin bundle appears malformed — reinstall ENGRAM." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Python interpreter — prefer plugin venv; fall back only if import gate passes
# ---------------------------------------------------------------------------
PLUGIN_VENV_PYTHON="$HOME/.engram/venv/bin/python3"
HOME_VENV_PYTHON="$HOME/.engram-venv/bin/python3"

# Ordered preference list
PYTHON_CANDIDATES=()
if [[ -x "$PLUGIN_VENV_PYTHON" ]]; then
  PYTHON_CANDIDATES+=("$PLUGIN_VENV_PYTHON")
fi
if [[ -x "$HOME_VENV_PYTHON" ]]; then
  PYTHON_CANDIDATES+=("$HOME_VENV_PYTHON")
fi
# System python3 as last resort
SYSTEM_PY="$(command -v python3 2>/dev/null || true)"
if [[ -n "$SYSTEM_PY" ]]; then
  PYTHON_CANDIDATES+=("$SYSTEM_PY")
fi

if [[ ${#PYTHON_CANDIDATES[@]} -eq 0 ]]; then
  echo "ERROR: python3 not found. Install Python 3.10+ or create a venv at ~/.engram/venv." >&2
  exit 1
fi

# Import-verification gate — find the first candidate that passes
PYTHON_BIN=""
for candidate in "${PYTHON_CANDIDATES[@]}"; do
  if ENGRAM_DIR="$PLUGIN_ENGRAM_DIR" "$candidate" -c "import sys, os; sys.path.insert(0, os.environ['ENGRAM_DIR']); from engram_stats import _compute_health_score; from tools.config_schema import SCHEMA" 2>/dev/null; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: viz cannot import the canonical health-score formula with any available python." >&2
  echo "       Tried: ${PYTHON_CANDIDATES[*]}" >&2
  echo "       Refusing to install a viz that would show a wrong/zero score." >&2
  echo "       Check that the plugin venv has the engram deps:" >&2
  echo "         $PLUGIN_VENV_PYTHON -c \"import engram_stats\"" >&2
  exit 1
fi

echo "=== ENGRAM viz operator setup ==="
echo "  plugin engram dir: $PLUGIN_ENGRAM_DIR"
echo "  viz server:        $VIZ_PY"
echo "  python:            $PYTHON_BIN"
echo

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "--- dry-run: import gate passed. Would install with the above settings. ---"
  echo "--- No changes made. ---"
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: Check systemd-user availability
# ---------------------------------------------------------------------------
echo "Step 1: Checking systemd-user availability"

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
# Step 2: Render and install the systemd user unit
# ---------------------------------------------------------------------------
echo
echo "Step 2: Installing systemd user unit"

SERVICE_TEMPLATE="$PLUGIN_ENGRAM_DIR/templates/engram-viz-user.service.template"
SERVICE_NAME="engram-viz-user.service"
SERVICE_DEST="$HOME/.config/systemd/user/$SERVICE_NAME"

if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
  echo "ERROR: unit template not found: $SERVICE_TEMPLATE" >&2
  echo "       The plugin bundle appears malformed — reinstall ENGRAM." >&2
  exit 1
fi

mkdir -p "$(dirname "$SERVICE_DEST")"

sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    -e "s|{{PLUGIN_ENGRAM_DIR}}|$PLUGIN_ENGRAM_DIR|g" \
    "$SERVICE_TEMPLATE" > "$SERVICE_DEST"

echo "  unit → $SERVICE_DEST"

# ---------------------------------------------------------------------------
# Step 3: daemon-reload + enable + restart
# ---------------------------------------------------------------------------
echo
echo "Step 3: Enabling and starting the service"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
# restart (not start): idempotent — refreshes any previously running instance
# with the current plugin path.
systemctl --user restart "$SERVICE_NAME"

echo "  systemd user service: enabled + started"
echo "  Status: $(systemctl --user is-active "$SERVICE_NAME")"

# ---------------------------------------------------------------------------
# Step 4: Create ~/.local/bin/engram-viz-setup convenience symlink
# ---------------------------------------------------------------------------
echo
echo "Step 4: Creating convenience symlink"

mkdir -p "$HOME/.local/bin"
ln -sf "$_self" "$HOME/.local/bin/engram-viz-setup"
echo "  ~/.local/bin/engram-viz-setup → $_self"
echo "  You can now run 'engram-viz-setup' from anywhere."

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
