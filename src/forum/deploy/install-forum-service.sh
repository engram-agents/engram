#!/usr/bin/env bash
# Install the ENGRAM forum as a systemd --user service from the blueprint in
# this folder.
#
# Run AS THE FORUM ADMIN (the account that runs the forum day to day) — NOT via
# sudo. The admin owns + runs the service so upgrades/restarts + the direct-DB
# admin CLI (admin.py) need no operator round-trip. The forum is hosted in a
# SHARED dir (default below) so the data outlives the admin baton and a
# counterpart agent in the shared group can cover. The one sudo exception is
# enabling linger for reboot-survival (printed at the end if it's not set).
#
# Idempotent: re-run to upgrade code + restart. Never overwrites forum.db.
#
# Options:
#   --src PATH          engram-alpha checkout containing src/forum/ (required)
#   --service-dir PATH  shared dir for the forum (DB + code + venv).
#                       Default: /home/agents-shared/forum
#   --port PORT         port the service listens on. Default: 5002
#   --admin-user USER   systemd --user account running the service.
#                       Default: current user ($(whoami))
#   --group NAME        group that co-admins share (group-writable data).
#                       Default: the admin's primary group
#   --dry-run           print every action without executing (CI-safe)
#   --no-start          install + enable but don't start
#
# This script NEVER touches an existing forum.db — the DB copy is a manual
# runbook step (see README.md) so the installer cannot clobber live data.
#
# Part of #868 (forum as second deploy target).
set -euo pipefail

SERVICE_DIR="/home/agents-shared/forum"
PORT="5002"
ADMIN_USER="$(whoami)"
GROUP=""
SRC=""
START=1
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --src)          SRC="$2"; shift 2 ;;
    --service-dir)  SERVICE_DIR="$2"; shift 2 ;;
    --port)         PORT="$2"; shift 2 ;;
    --admin-user)   ADMIN_USER="$2"; shift 2 ;;
    --group)        GROUP="$2"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --no-start)     START=0; shift ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
: "${GROUP:=$(id -gn)}"

# ---------------------------------------------------------------------------
# Dry-run wrapper: prefix every mutating command with RUN.
# In --dry-run mode, RUN just prints "  [dry-run] <cmd>" and returns 0.
# ---------------------------------------------------------------------------
RUN() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> DRY-RUN mode — actions printed, not executed."
fi

# ---------------------------------------------------------------------------
# Pre-flight checks (run even in --dry-run so the plan can be validated).
# ---------------------------------------------------------------------------
[ -z "$SRC" ] && { echo "ERROR: --src PATH required (path to engram-alpha checkout)" >&2; exit 2; }
[ -d "$SRC/src/forum" ] || { echo "ERROR: $SRC/src/forum not found — is --src an engram-alpha checkout?" >&2; exit 2; }
[ -f "$SRC/src/forum/deploy/engram-forum.service.template" ] || {
  echo "ERROR: unit template not found under $SRC/src/forum/deploy/" >&2; exit 2;
}
if [ "$DRY_RUN" -eq 0 ]; then
  command -v systemctl >/dev/null || {
    echo "ERROR: systemctl not found (this installer targets systemd --user)" >&2; exit 2;
  }
  id -nG | tr ' ' '\n' | grep -qx "$GROUP" || {
    echo "ERROR: $(whoami) is not in group '$GROUP' — needed to own the shared forum data" >&2; exit 2;
  }
fi

# ---------------------------------------------------------------------------
# Idempotent guard: if --service-dir exists, check it looks like ours.
# Abort if the directory is non-empty and has no app/ subdirectory
# (foreign installation guard — avoids silently overwriting unrelated data).
# ---------------------------------------------------------------------------
if [ -d "$SERVICE_DIR" ] && [ "$SERVICE_DIR" != "/" ]; then
  if [ -z "$(ls -A "$SERVICE_DIR" 2>/dev/null)" ]; then
    : # empty dir is fine — first install
  elif [ ! -d "$SERVICE_DIR/app" ]; then
    echo "ERROR: $SERVICE_DIR exists and is non-empty but has no app/ subdirectory." >&2
    echo "       This looks like a foreign directory. Aborting to prevent data loss." >&2
    echo "       Pass a different --service-dir or manually create $SERVICE_DIR/app/ to proceed." >&2
    exit 2
  fi
fi

# Guard: NEVER overwrite an existing forum.db.
DB_PATH="$SERVICE_DIR/forum.db"
if [ -f "$DB_PATH" ]; then
  echo "==> Existing forum.db found at $DB_PATH — will NOT overwrite (preserved)."
else
  echo "==> No existing forum.db at $DB_PATH — will be created on first start."
fi

APP="$SERVICE_DIR/app"
VENV="$SERVICE_DIR/.venv"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="engram-forum.service"
PYTHON="$VENV/bin/python"

# Group-writable creations so a counterpart admin in $GROUP can also write data.
RUN umask 002

echo "==> Forum service dir: $SERVICE_DIR (port $PORT, group $GROUP); admin: $ADMIN_USER"
RUN mkdir -p "$SERVICE_DIR"
RUN chgrp "$GROUP" "$SERVICE_DIR" 2>/dev/null || true
RUN chmod 2775 "$SERVICE_DIR" 2>/dev/null || true   # setgid: new files inherit $GROUP

if [ "$DRY_RUN" -eq 0 ]; then
  # Verify the group AND the setgid bit actually took.
  ACT_GRP="$(stat -c '%G' "$SERVICE_DIR" 2>/dev/null || echo '?')"
  [ "$ACT_GRP" = "$GROUP" ] || echo "    WARN: $SERVICE_DIR group is '$ACT_GRP', not '$GROUP' — a counterpart admin may not be able to write."
  find "$SERVICE_DIR" -maxdepth 0 -perm -2000 >/dev/null 2>&1 || echo "    WARN: setgid bit not set on $SERVICE_DIR — new files won't inherit group '$GROUP'."
fi

echo "==> Snapshotting forum package → $APP/forum"
RUN mkdir -p "$APP/forum"
# Snapshot the runtime package: *.py PLUS runtime data (templates/, static/,
# seeds/, FORUM.md). Exclude non-runtime: tests/, deploy/, fairy-spec MDs,
# spec.md, README.md, and bytecode.
RSYNC_EXCLUDES=(--exclude='__pycache__' --exclude='*.pyc' --exclude='tests/'
                --exclude='deploy/' --exclude='fairy-spec-*.md' --exclude='spec.md'
                --exclude='README.md')
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] rsync/cp src/forum/ → $APP/forum/ (excluding tests/, deploy/, fairy-spec MDs, etc.)"
elif command -v rsync >/dev/null; then
  rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SRC/src/forum/" "$APP/forum/"
else
  rm -rf "$APP/forum"; mkdir -p "$APP/forum"
  cp "$SRC/src/forum/"*.py "$APP/forum/"
  cp "$SRC/src/forum/FORUM.md" "$APP/forum/" 2>/dev/null || true
  for d in templates static seeds; do
    [ -d "$SRC/src/forum/$d" ] && cp -r "$SRC/src/forum/$d" "$APP/forum/$d"
  done
  find "$APP/forum" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi
RUN touch "$APP/forum/__init__.py"

# Build the venv ONLY if it isn't already a working one.
if [ "$DRY_RUN" -eq 0 ] && [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import flask" 2>/dev/null; then
  echo "==> Reusing existing venv → $VENV ($("$VENV/bin/python" -c 'import flask;print("flask",flask.__version__)'))"
else
  echo "==> Building self-contained venv → $VENV (group-readable so any admin can run it)"
  RUN python3 -m venv "$VENV"
  RUN "$VENV/bin/pip" install --quiet --upgrade pip
  RUN "$VENV/bin/pip" install --quiet -r "$SRC/src/forum/requirements.txt"
  if [ "$DRY_RUN" -eq 0 ]; then
    "$VENV/bin/python" -c "import flask; print('    flask', flask.__version__)"
    chmod -R g+rX "$APP" "$VENV" 2>/dev/null || true
  fi
fi

echo "==> Rendering unit from template → $UNIT_DIR/$UNIT"
RUN mkdir -p "$UNIT_DIR"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] sed TEMPLATE-ONLY strip + substitute {{SERVICE_DIR}}→$SERVICE_DIR, {{PORT}}→$PORT"
  echo "  [dry-run] install rendered unit → $UNIT_DIR/$UNIT"
else
  # Strip the >>>TEMPLATE-ONLY ... <<<TEMPLATE-ONLY block, then substitute
  # {{FORUM_HOME}} (template placeholder) and {{PORT}}.
  sed '/>>>TEMPLATE-ONLY/,/<<<TEMPLATE-ONLY/d' "$SRC/src/forum/deploy/engram-forum.service.template" \
    | sed "s|{{FORUM_HOME}}|$SERVICE_DIR|g" \
    | sed "s|--port 5002|--port $PORT|" \
    | sed "s|Flask, port 5002|Flask, port $PORT|" \
    > "$UNIT_DIR/$UNIT"
  grep -q '{{' "$UNIT_DIR/$UNIT" && {
    echo "ERROR: unsubstituted placeholders remain in the rendered unit" >&2; exit 2;
  }
fi

LINGER_OK=1
echo "==> Enabling linger (so the service survives logout/reboot)"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] loginctl enable-linger $ADMIN_USER"
elif loginctl enable-linger "$ADMIN_USER" 2>/dev/null; then
  echo "    linger enabled for $ADMIN_USER"
else
  LINGER_OK=0
fi

echo "==> systemctl --user daemon-reload + enable"
RUN systemctl --user daemon-reload
RUN systemctl --user enable "$UNIT"

if [ "$START" -eq 1 ]; then
  echo "==> Starting service"
  RUN systemctl --user restart "$UNIT"

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] health check: curl http://127.0.0.1:${PORT}/health"
    echo "  [dry-run] verify-only:  $VENV/bin/python -m forum.server --db $SERVICE_DIR/forum.db --audit $SERVICE_DIR/forum-audit.jsonl --verify-only"
  else
    sleep 2
    systemctl --user --no-pager status "$UNIT" | head -6 || true

    # ----------------------------------------------------------------
    # Post-install gates (both must pass — #868 slice B).
    # Gate 1: HTTP health check — hard gate, exits 2 on failure.
    # Gate 2: --verify-only (confirms all runtime deps present) — hard gate, exits 2 on failure.
    # ----------------------------------------------------------------
    echo "==> Post-install gate 1: HTTP health check"
    if curl --fail --silent --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null; then
      echo "    OK: forum /health responding on :${PORT}"
    else
      echo "ERROR: /health not responding — service may have failed to start." >&2
      echo "       Check: journalctl --user -u $UNIT -n 30" >&2
      exit 2
    fi

    echo "==> Post-install gate 2: boot-verify probe"
    if "$PYTHON" -m forum.server \
        --db "$SERVICE_DIR/forum.db" \
        --audit "$SERVICE_DIR/forum-audit.jsonl" \
        --verify-only; then
      echo "    verify-only passed"
    else
      echo "ERROR: verify-only probe returned non-zero — dep check failed; service may be degraded." >&2
      exit 2
    fi
  fi
else
  echo "==> --no-start: unit installed + enabled, NOT started (do the DB cutover, then: systemctl --user start $UNIT)"
fi

echo "==> Done."
echo "    Manage: systemctl --user {status,restart,stop} $UNIT  ·  Logs: journalctl --user -u $UNIT -f"
echo "    Shared data: $SERVICE_DIR/forum.db (group $GROUP, group-writable)"
if [ "$DRY_RUN" -eq 0 ] && [ "$LINGER_OK" -eq 0 ]; then
  echo "    REMAINING STEP for reboot-survival (one-time, needs sudo):  sudo loginctl enable-linger $ADMIN_USER"
fi
