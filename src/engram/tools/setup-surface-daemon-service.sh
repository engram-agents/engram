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
#   tools/setup-surface-daemon-service.sh --owner     # the engramd OWNER, Linux only (#3663)
#
# --owner installs the engramd owner (engramd_owner.py) instead, as the systemd USER
# unit engramd-owner-<id>.service rendered from engramd-owner.service.template, where
# <id> is the first 8 hex chars of sha256 over the NORMALIZED (absolute, physical,
# no trailing slash) ENGRAM_HOME -- one unit per ENGRAM_HOME, not per user. Linux only:
# on macOS it exits non-zero and writes/calls nothing (there is no macOS launcher
# for the owner yet, #3709). The ephemeral-path refusal below
# applies to engramd_owner.py's physical path in this mode. Without --owner the
# behaviour is exactly what it always was.
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
#
# Ephemeral-path refusal (#3519 item 1):
#   The script REFUSES (exit 2, before writing any plist/unit and before calling
#   launchctl/systemctl) when the physical, symlink-resolved path of
#   engram-surface-daemon.py lies under a temp root (/tmp, /private/tmp,
#   /var/tmp, /private/var/tmp, /var/folders, /private/var/folders, or $TMPDIR
#   when set and non-empty) or contains a /.claude/worktrees/ component pair
#   (agent/fairy worktrees). A service pointing at such a path outlives its
#   directory and respawns forever (2026-10-03: 27,605 launchd respawns).
#   Override, when you really mean it: pass --allow-path, or set
#   ENGRAM_SETUP_ALLOW_PATH=1 (exactly 1). The script then prints one WARN line.

set -euo pipefail

# ---------------------------------------------------------------------------
# Arguments — the only option is --allow-path; anything else is ignored, as it
# always was.
# ---------------------------------------------------------------------------
ALLOW_PATH=0
OWNER_MODE=0
for _arg in "$@"; do
    if [[ "$_arg" == "--allow-path" ]]; then
        ALLOW_PATH=1
    elif [[ "$_arg" == "--owner" ]]; then
        OWNER_MODE=1
    fi
done
if [[ "${ENGRAM_SETUP_ALLOW_PATH:-}" == "1" ]]; then
    ALLOW_PATH=1
fi

# --owner is Linux only (#3663): refuse on anything else BEFORE touching anything.
if [[ "$OWNER_MODE" == "1" ]]; then
    _owner_os="$(uname -s)"
    if [[ "$_owner_os" == "Darwin" ]]; then
        echo "ERROR: --owner is not supported on macOS yet: there is no macOS (launchd) launcher for the owner yet (#3709). Nothing was written or loaded." >&2
        exit 1
    elif [[ "$_owner_os" != "Linux" ]]; then
        echo "ERROR: --owner supports Linux (systemd --user) only; this is $_owner_os. Nothing was written." >&2
        exit 1
    fi
fi

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
if [[ "$OWNER_MODE" == "1" ]]; then
    HOOKS_DIR=""   # --owner needs no hooks dir; engramd_owner.py sits at $PLUGIN_ROOT
elif [[ -n "${ENGRAM_HOOKS_DIR:-}" ]]; then
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
if [[ "$OWNER_MODE" == "1" ]]; then
    # In --owner mode the "daemon script" every check below guards IS the owner.
    # engramd_owner.py sits flat at the plugin root in the bundle AND in src/engram/.
    DAEMON_PY="$PLUGIN_ROOT/engramd_owner.py"
fi
if [[ ! -f "$DAEMON_PY" ]]; then
    echo "ERROR: daemon script not found: $DAEMON_PY" >&2
    echo "       Run this script from within an engram-alpha repo clone or a" >&2
    echo "       plugin bundle where hooks/engram-surface-daemon.py exists." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Refuse an ephemeral daemon path (#3519 item 1) — BEFORE any OS branching,
# plist/unit write, or launchctl/systemctl call.
#
# Physical resolution in plain bash (macOS bash 3.2: no readlink -f, no
# realpath): follow a file symlink chain with plain `readlink`, then resolve the
# directory with `cd ... && pwd -P`.
# ---------------------------------------------------------------------------
_physical_path() {
    local p="$1" target n=0
    while [[ -L "$p" && $n -lt 40 ]]; do
        target="$(readlink "$p")"
        if [[ "$target" == /* ]]; then
            p="$target"
        else
            p="$(dirname "$p")/$target"
        fi
        n=$((n + 1))
    done
    local d b
    d="$(dirname "$p")"
    b="${p##*/}"
    if d="$(cd "$d" 2>/dev/null && pwd -P)"; then
        if [[ "$d" == "/" ]]; then
            echo "/$b"
        else
            echo "$d/$b"
        fi
    else
        echo "$p"
    fi
}

_under_root() {
    # $1 = path, $2 = root. Component-bounded prefix match. An empty root (or
    # "/" once trailing slashes are stripped) matches nothing.
    local path="$1" root="$2"
    while [[ "$root" == */ ]]; do root="${root%/}"; done
    [[ -n "$root" ]] || return 1
    [[ "$path" == "$root" || "$path" == "$root"/* ]]
}

_ephemeral_reason() {
    local path="$1" root
    local roots=(/tmp /private/tmp /var/tmp /private/var/tmp /var/folders /private/var/folders)
    if [[ -n "${TMPDIR:-}" ]]; then
        roots+=("$TMPDIR")
        roots+=("$(cd "$TMPDIR" 2>/dev/null && pwd -P || true)")
    fi
    for root in "${roots[@]}"; do
        if _under_root "$path" "$root"; then
            echo "it is under the temp directory $root"
            return 0
        fi
    done
    if [[ "$path/" == */.claude/worktrees/* ]]; then
        echo "it is inside an agent/fairy worktree (.claude/worktrees)"
        return 0
    fi
    return 1
}

DAEMON_REAL="$(_physical_path "$DAEMON_PY")"
if _why="$(_ephemeral_reason "$DAEMON_REAL")"; then
    if [[ "$ALLOW_PATH" == "1" ]]; then
        echo "WARN: registering a service for an ephemeral daemon path $DAEMON_REAL ($_why); proceeding because --allow-path / ENGRAM_SETUP_ALLOW_PATH=1 was given." >&2
    else
        echo "ERROR: refusing to register a service for $DAEMON_REAL: ephemeral location ($_why); the service would respawn forever once that directory is deleted. Override with --allow-path or ENGRAM_SETUP_ALLOW_PATH=1." >&2
        exit 2
    fi
fi

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
OS="$(uname -s)"

# --owner: refuse values the unit render cannot carry (I1). The unit is rendered with sed
# (`&` expands to the match, `\` is eaten, `|` aborts the s||| and leaves a truncated
# file) and then read by systemd (whitespace splits Environment=/ExecStart, `%` and `$`
# expand, quotes are interpreted). A wrong-home unit named for home X is silent wrong
# supervision, so refuse BEFORE the destination is opened or any directory is created.
_owner_bad_char() {
    case "$1" in
        *' '*) printf 'space' ;;
        *$'\t'*) printf 'tab' ;;
        *$'\n'*) printf 'newline' ;;
        *$'\r'*) printf 'carriage-return' ;;
        *'&'*) printf 'ampersand' ;;
        *'\'*) printf 'backslash' ;;
        *'|'*) printf 'pipe' ;;
        *'%'*) printf 'percent' ;;
        *'"'*) printf 'double-quote' ;;
        *"'"*) printf 'single-quote' ;;
        *'$'*) printf 'dollar' ;;
        # sed substitutes the python and owner paths BEFORE ENGRAM_HOME, so a literal
        # {{ENGRAM_HOME}}/{{PYTHON}} inside them would be re-substituted: refuse {{ and }}
        # in every value (one uniform rule).
        *'{{'*) printf 'open-brace-pair' ;;
        *'}}'*) printf 'close-brace-pair' ;;
        *) return 1 ;;
    esac
}
_owner_refuse_if_bad() {
    # $1 = what the value is, $2 = the value
    local _name _art="a"
    if _name="$(_owner_bad_char "$2")"; then
        case "$_name" in a*|e*|i*|o*|u*) _art="an" ;; esac
        echo "ERROR: unsupported in --owner mode: $1 contains $_art $_name character ($(printf '%q' "$2")); the unit would be rendered or read wrongly. No unit was written or loaded." >&2
        exit 2
    fi
}
if [[ "$OWNER_MODE" == "1" ]]; then
    case "$ENGRAM_HOME" in
        "~") _owner_home_raw="$HOME" ;;
        "~/"*) _owner_home_raw="$HOME/${ENGRAM_HOME#"~/"}" ;;
        *) _owner_home_raw="$ENGRAM_HOME" ;;
    esac
    _owner_refuse_if_bad "ENGRAM_HOME" "$_owner_home_raw"
    _owner_refuse_if_bad "the python path" "$PYTHON_BIN"
    _owner_refuse_if_bad "the owner script path" "$DAEMON_PY"
fi

if [[ "$OWNER_MODE" == "1" ]]; then
    # Normalize ENGRAM_HOME (after the refusals above, so a refusal creates nothing):
    # ~ expanded, created, made absolute and physical, no trailing slash. The unit name
    # is keyed on this string, so every spelling of one home must give the same one.
    case "$ENGRAM_HOME" in
        "~") ENGRAM_HOME="$HOME" ;;
        "~/"*) ENGRAM_HOME="$HOME/${ENGRAM_HOME#"~/"}" ;;
    esac
    mkdir -p "$ENGRAM_HOME"
    ENGRAM_HOME="$(cd "$ENGRAM_HOME" && pwd -P)"
    _owner_refuse_if_bad "ENGRAM_HOME (physical path)" "$ENGRAM_HOME"
    while [[ "$ENGRAM_HOME" != "/" && "$ENGRAM_HOME" == */ ]]; do ENGRAM_HOME="${ENGRAM_HOME%/}"; done
    if command -v sha256sum &>/dev/null; then
        _home_id="$(printf '%s' "$ENGRAM_HOME" | sha256sum | cut -c1-8)"
    elif command -v shasum &>/dev/null; then
        _home_id="$(printf '%s' "$ENGRAM_HOME" | shasum -a 256 | cut -c1-8)"
    else
        echo "ERROR: neither sha256sum nor shasum found; cannot key the unit name on ENGRAM_HOME." >&2
        exit 1
    fi
    echo "=== ENGRAM owner service setup ==="
else
    echo "=== ENGRAM surface daemon service setup ==="
fi
echo "  OS:            $OS"
echo "  python:        $PYTHON_BIN"
if [[ "$OWNER_MODE" == "1" ]]; then
    echo "  owner script:  $DAEMON_PY"
else
    echo "  hooks dir:     $HOOKS_DIR"
fi
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
    SERVICE_NAME="engram-surface-daemon.service"
    SERVICE_LOG_NAME="surface-daemon.log"
    if [[ "$OWNER_MODE" == "1" ]]; then
        SERVICE_TEMPLATE="$TEMPLATE_DIR/engramd-owner.service.template"
        SERVICE_NAME="engramd-owner-$_home_id.service"
        SERVICE_LOG_NAME="engramd-owner.log"
    fi
    if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
        echo "ERROR: service template not found: $SERVICE_TEMPLATE" >&2
        exit 1
    fi

    SERVICE_DEST="$HOME/.config/systemd/user/$SERVICE_NAME"
    mkdir -p "$(dirname "$SERVICE_DEST")"

    echo "Step 1: Rendering systemd unit → $SERVICE_DEST"
    if [[ "$OWNER_MODE" == "1" ]]; then
        sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
            -e "s|{{ENGRAM_OWNER_PY}}|$DAEMON_PY|g" \
            -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
            "$SERVICE_TEMPLATE" > "$SERVICE_DEST"
    else
        sed -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
            -e "s|{{ENGRAM_HOOKS_DIR}}|$HOOKS_DIR|g" \
            -e "s|{{ENGRAM_HOME}}|$ENGRAM_HOME|g" \
            "$SERVICE_TEMPLATE" > "$SERVICE_DEST"
    fi
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
    echo "  Logs file:     $ENGRAM_HOME/$SERVICE_LOG_NAME"
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
