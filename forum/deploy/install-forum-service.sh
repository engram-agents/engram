#!/usr/bin/env bash
# Install the ENGRAM forum as a systemd --user service from the blueprint in
# this folder. EXPERIMENTAL / UNDER DEVELOPMENT — review + adapt before running.
#
# Run AS THE FORUM ADMIN (the account that runs the forum day to day) — NOT via
# sudo. The admin owns + runs the service so upgrades/restarts + the direct-DB
# admin CLI (admin.py) need no operator round-trip. The forum is hosted in a
# SHARED dir (default below) so the data outlives the admin baton and a
# counterpart agent in the shared group can cover. The one sudo exception is
# enabling linger for reboot-survival (printed at the end if it's not set).
#
# Customize for your install via flags (nothing host-specific is hard-coded):
#   --src PATH         engram-alpha checkout containing forum/ (required)
#   --forum-home PATH  shared dir for the forum (DB + code + venv).
#                      Default: /home/agents-shared/forum
#   --group NAME       group that co-admins share (group-writable data).
#                      Default: the admin's primary group
#   --no-start         install + enable but don't start (do the DB cutover first)
#
# This script never touches an existing DB — the DB copy is a manual runbook step
# (see README.md) so the installer can't clobber live data.
set -euo pipefail

FORUM_HOME="/home/agents-shared/forum"
GROUP=""
SRC=""
START=1
while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$2"; shift 2 ;;
    --forum-home) FORUM_HOME="$2"; shift 2 ;;
    --group) GROUP="$2"; shift 2 ;;
    --no-start) START=0; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
: "${GROUP:=$(id -gn)}"

[ -z "$SRC" ] && { echo "ERROR: --src PATH required (path to engram-alpha checkout)" >&2; exit 2; }
[ -d "$SRC/forum" ] || { echo "ERROR: $SRC/forum not found — is --src an engram-alpha checkout?" >&2; exit 2; }
[ -f "$SRC/forum/deploy/engram-forum.service.template" ] || { echo "ERROR: unit template not found under $SRC/forum/deploy/" >&2; exit 2; }
command -v systemctl >/dev/null || { echo "ERROR: systemctl not found (this blueprint targets systemd --user)" >&2; exit 2; }
id -nG | tr ' ' '\n' | grep -qx "$GROUP" || { echo "ERROR: $(whoami) is not in group '$GROUP' — needed to own the shared forum data" >&2; exit 2; }

APP="$FORUM_HOME/app"
VENV="$FORUM_HOME/.venv"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="engram-forum.service"

# Group-writable creations so a counterpart admin in $GROUP can also write data.
umask 002

echo "==> Forum home: $FORUM_HOME (group $GROUP); admin: $(whoami)"
mkdir -p "$FORUM_HOME"
chgrp "$GROUP" "$FORUM_HOME" 2>/dev/null || true
chmod 2775 "$FORUM_HOME" 2>/dev/null || true   # setgid: new files inherit $GROUP
# Verify the group AND the setgid bit actually took — silent failure on either
# breaks a counterpart admin's writes (wrong group on new files / can't inherit).
ACT_GRP="$(stat -c '%G' "$FORUM_HOME" 2>/dev/null || echo '?')"
[ "$ACT_GRP" = "$GROUP" ] || echo "    WARN: $FORUM_HOME group is '$ACT_GRP', not '$GROUP' — a counterpart admin may not be able to write. Fix: sudo chgrp -R $GROUP $FORUM_HOME && chmod 2775 $FORUM_HOME"
find "$FORUM_HOME" -maxdepth 0 -perm -2000 >/dev/null 2>&1 || echo "    WARN: setgid bit not set on $FORUM_HOME — new files won't inherit group '$GROUP', breaking cover. Fix: chmod g+s $FORUM_HOME"

echo "==> Snapshotting forum package → $APP/forum"
mkdir -p "$APP/forum"
# Snapshot the runtime package: *.py PLUS the runtime data the app resolves
# relative to the package dir — templates/ (render_template), static/ (Flask),
# seeds/ (seed.py reads seeds/*.md + categories.default.json), and FORUM.md
# (the /forum.md route reads it). Exclude non-runtime: tests/, deploy/, the
# fairy-spec / spec / README markdown, and bytecode.
RSYNC_EXCLUDES=(--exclude='__pycache__' --exclude='*.pyc' --exclude='tests/'
                --exclude='deploy/' --exclude='fairy-spec-*.md' --exclude='spec.md'
                --exclude='README.md')
if command -v rsync >/dev/null; then
  rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SRC/forum/" "$APP/forum/"
else
  rm -rf "$APP/forum"; mkdir -p "$APP/forum"
  cp "$SRC/forum/"*.py "$APP/forum/"
  cp "$SRC/forum/FORUM.md" "$APP/forum/" 2>/dev/null || true
  for d in templates static seeds; do
    [ -d "$SRC/forum/$d" ] && cp -r "$SRC/forum/$d" "$APP/forum/$d"
  done
  find "$APP/forum" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi
touch "$APP/forum/__init__.py"

# Build the venv ONLY if it isn't already a working one. A counterpart admin
# covering for the owner can't rewrite the owner's venv (group-readable, not
# -writable) — and doesn't need to; skip the rebuild and reuse it.
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import flask" 2>/dev/null; then
  echo "==> Reusing existing venv → $VENV ($("$VENV/bin/python" -c 'import flask;print("flask",flask.__version__)'))"
else
  echo "==> Building self-contained venv → $VENV (group-readable so any admin can run it)"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$SRC/forum/requirements.txt"
  "$VENV/bin/python" -c "import flask; print('    flask', flask.__version__)"
  chmod -R g+rX "$APP" "$VENV" 2>/dev/null || true
fi

echo "==> Rendering unit from template → $UNIT_DIR/$UNIT"
mkdir -p "$UNIT_DIR"
# Strip the >>>TEMPLATE-ONLY ... <<<TEMPLATE-ONLY block (its placeholder warning
# is meaningless once rendered), then substitute {{FORUM_HOME}}.
sed '/>>>TEMPLATE-ONLY/,/<<<TEMPLATE-ONLY/d' "$SRC/forum/deploy/engram-forum.service.template" \
  | sed "s|{{FORUM_HOME}}|$FORUM_HOME|g" > "$UNIT_DIR/$UNIT"
grep -q '{{' "$UNIT_DIR/$UNIT" && { echo "ERROR: unsubstituted placeholders remain in the rendered unit" >&2; exit 1; }

LINGER_OK=1
echo "==> Enabling linger (so the service survives logout/reboot)"
if loginctl enable-linger "$USER" 2>/dev/null; then
  echo "    linger enabled for $USER"
else
  LINGER_OK=0
fi

echo "==> systemctl --user daemon-reload + enable"
systemctl --user daemon-reload
systemctl --user enable "$UNIT"

if [ "$START" -eq 1 ]; then
  echo "==> Starting service"
  systemctl --user restart "$UNIT"
  sleep 2
  systemctl --user --no-pager status "$UNIT" | head -6 || true
  echo "==> Health check"
  curl -sf "http://127.0.0.1:5002/api/threads" >/dev/null && echo "    OK: forum responding on :5002" || echo "    WARN: not responding yet — check: journalctl --user -u $UNIT -n 30"
else
  echo "==> --no-start: unit installed + enabled, NOT started (do the DB cutover, then: systemctl --user start $UNIT)"
fi

echo "==> Done. Manage: systemctl --user {status,restart,stop} $UNIT  ·  Logs: journalctl --user -u $UNIT -f"
echo "    Shared data: $FORUM_HOME/forum.db (group $GROUP, group-writable)"
[ "$LINGER_OK" -eq 0 ] && echo "    ⚠ REMAINING STEP for reboot-survival (one-time, needs sudo):  sudo loginctl enable-linger $USER"
