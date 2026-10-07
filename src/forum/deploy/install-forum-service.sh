#!/usr/bin/env bash
# Install the ENGRAM forum as a systemd --user service from the blueprint in
# this folder.
#
# Run AS THE FORUM ADMIN (the account that runs the forum day to day) — NOT via
# sudo. The admin owns + runs the service so upgrades/restarts + the direct-DB
# admin CLI (admin.py) need no operator round-trip. The forum is hosted in a
# SHARED dir (--service-dir) so the data outlives the admin baton and a
# counterpart agent in the shared group can cover. The one sudo exception is
# enabling linger for reboot-survival (printed at the end if it's not set).
#
# Idempotent: re-run to upgrade code + restart. Never overwrites forum.db.
#
# Options:
#   --src PATH          engram-alpha checkout containing src/forum/ (required)
#   --service-dir PATH  shared dir for the forum (DB + code + venv). Required on a
#                       first install (e.g. /home/<shared>/forum). On a re-install
#                       it may be omitted: the FORUM_HOME of the existing
#                       engram-forum.service is reused, so a re-run can never start a
#                       second, empty forum somewhere new. An existing unit with no
#                       parsable FORUM_HOME is refused (exit 2), never guessed at.
#   --port PORT         port the service listens on. Default: 5002
#   --admin-user USER   systemd --user account running the service.
#                       Default: current user ($(whoami))
#   --group NAME        group that co-admins share (group-writable data).
#                       Pass the shared group your forum seats belong to. If
#                       omitted, the installer REFUSES (exit 2) unless your primary
#                       group (`id -gn`) is literally `agents` -- it never silently
#                       adopts an arbitrary primary group. Pass it explicitly.
#   --baton-login-map PATH
#                       absolute path of the baton login map (#3039 U2b). Rendered
#                       into the unit as a real ExecStart `--baton-login-map PATH`.
#                       Letters, digits and / . _ - only. If the unit being
#                       replaced already carries a map (or admin uid) the installer
#                       REFUSES unless this flag is passed again, so a re-install
#                       can never silently drop it (#3312).
#   --baton-login-map-admin-uid UID
#                       repeatable; extra uid (besides root) allowed to own the map.
#                       Requires --baton-login-map.
#   --github-token-file PATH
#                       absolute path of the root/admin-installed file holding the
#                       gate's dedicated read-only `GH_TOKEN=...` line (#3283).
#                       Rendered into [Service] as ONE `EnvironmentFile=-PATH` line
#                       (the `-` is the installer's: a missing or unreadable file
#                       lets the unit start and the gate refuses instead).
#                       Letters, digits and / . _ - only. The path is NEVER read or
#                       dereferenced here (it may not exist on this host yet; keep it
#                       out of any group-readable shared tree, mode 0640). If the unit
#                       being replaced already carries a github-token EnvironmentFile
#                       line the installer REFUSES unless this flag is passed again,
#                       so a re-install can never silently drop it.
#   --dry-run           print every action without executing (CI-safe)
#   --no-start          install + enable but don't start
#
# This script NEVER touches an existing forum.db — the DB copy is a manual
# runbook step (see README.md) so the installer cannot clobber live data.
#
# Part of #868 (forum as second deploy target).
set -euo pipefail

SERVICE_DIR=""   # no default (#3568): a flag, else the existing unit's FORUM_HOME
PORT="5002"
ADMIN_USER="$(whoami)"
GROUP=""
BATON_MAP=""
BATON_UIDS=""   # space-separated, validated integers
GITHUB_TOKEN_FILE=""
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
    --baton-login-map)
      BATON_MAP="$2"
      case "$BATON_MAP" in
        /*) ;;
        *) echo "ERROR: --baton-login-map must be an absolute path (got '$BATON_MAP')" >&2; exit 2 ;;
      esac
      case "$BATON_MAP" in
        *[!A-Za-z0-9/._-]*)
          echo "ERROR: --baton-login-map may contain only letters, digits and / . _ - (it lands in a unit file); got '$BATON_MAP'" >&2; exit 2 ;;
      esac
      shift 2 ;;
    --baton-login-map-admin-uid)
      case "$2" in
        ''|*[!0-9]*)
          echo "ERROR: --baton-login-map-admin-uid must be an integer uid (got '$2')" >&2; exit 2 ;;
      esac
      BATON_UIDS="$BATON_UIDS $2"; shift 2 ;;
    --github-token-file)
      GITHUB_TOKEN_FILE="$2"
      case "$GITHUB_TOKEN_FILE" in
        /*) ;;
        *) echo "ERROR: --github-token-file must be an absolute path (got '$GITHUB_TOKEN_FILE')" >&2; exit 2 ;;
      esac
      case "$GITHUB_TOKEN_FILE" in
        *[!A-Za-z0-9/._-]*)
          echo "ERROR: --github-token-file may contain only letters, digits and / . _ - (it lands in a unit file); got '$GITHUB_TOKEN_FILE'" >&2; exit 2 ;;
      esac
      # The re-install guard below finds an existing line by the name
      # `github-token`, so the FILE NAME (not just a directory) must carry it.
      case "${GITHUB_TOKEN_FILE##*/}" in
        *github-token*) ;;
        *) echo "ERROR: --github-token-file's file name must contain 'github-token' (got '$GITHUB_TOKEN_FILE'); the re-install guard keys on that name, so any other name could be dropped silently" >&2; exit 2 ;;
      esac
      shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --no-start)     START=0; shift ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [ -n "$BATON_UIDS" ] && [ -z "$BATON_MAP" ]; then
  echo "ERROR: --baton-login-map-admin-uid requires --baton-login-map" >&2; exit 2
fi

# --group: never silently adopt the admin's primary group (#3312 -- it bit
# operators twice). With --group omitted we proceed only if the primary group
# already IS the literal `agents` (the group our own deployment uses); anyone
# else must say which shared group their forum seats belong to.
if [ -z "$GROUP" ]; then
  _primary_group="$(id -gn)"
  if [ "$_primary_group" != "agents" ]; then
    echo "ERROR: --group not given: pass --group <the shared group your forum seats belong to>" >&2
    echo "       (your primary group is '$_primary_group'; the installer never guesses the shared group)." >&2
    exit 2
  fi
  GROUP="$_primary_group"
fi

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
[ -f "$SRC/tools/engram-pkg/engram-pkg" ] || {
  echo "ERROR: $SRC/tools/engram-pkg/engram-pkg not found — pack validator required" >&2; exit 2;
}
[ -f "$SRC/tools/baton_gate.py" ] || {
  echo "ERROR: $SRC/tools/baton_gate.py not found — the server's baton gate module is required" >&2; exit 2;
}
[ -f "$SRC/tools/forum_backup.py" ] || {
  echo "ERROR: $SRC/tools/forum_backup.py not found — the backup units run the snapshot of this script" >&2; exit 2;
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
# Baton login map must not be silently dropped (#3312). The unit is re-rendered
# from the template on every run, so a map the admin added by hand (or via an
# earlier --baton-login-map) would vanish and the #3039 U2b gate would fail
# closed. If the unit about to be replaced carries a map in a NON-comment line
# and this run does not re-supply it, refuse before anything is written. Reads
# only, so it runs in --dry-run too.
# ---------------------------------------------------------------------------
# One source for the unit path: the guard below and the write further down both
# use these, so they cannot drift apart (a guard reading a file that is never
# replaced would silently stop protecting anything).
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="engram-forum.service"
_EXISTING_UNIT="$UNIT_DIR/$UNIT"
if [ -f "$_EXISTING_UNIT" ]; then
  _live="$(grep -v '^[[:space:]]*[#;]' "$_EXISTING_UNIT" || true)"
  _has_uid=0; _has_map=0
  if printf '%s\n' "$_live" | grep -Eq -- '--baton-login-map-admin-uid([[:space:]=]|$)|ENGRAM_BATON_LOGIN_MAP_ADMIN_UIDS'; then
    _has_uid=1; _has_map=1
  fi
  if printf '%s\n' "$_live" | grep -Eq -- '--baton-login-map([[:space:]=]|$)|ENGRAM_BATON_LOGIN_MAP([^A-Za-z0-9_]|$)'; then
    _has_map=1
  fi
  if [ "$_has_map" -eq 1 ] && [ -z "$BATON_MAP" ]; then
    echo "ERROR: $_EXISTING_UNIT already carries a baton login map; re-installing without it would" >&2
    echo "       silently drop it and the baton peer->merge gate would fail closed (no login map configured)." >&2
    echo "       Nothing was written. Re-run with --baton-login-map <path> (plus --baton-login-map-admin-uid <uid> if used)." >&2
    exit 2
  fi
  if [ "$_has_uid" -eq 1 ] && [ -z "$BATON_UIDS" ]; then
    echo "ERROR: $_EXISTING_UNIT already carries a baton login map admin uid; re-installing without" >&2
    echo "       --baton-login-map-admin-uid <uid> would silently drop it. Nothing was written." >&2
    echo "       Re-run with --baton-login-map <path> --baton-login-map-admin-uid <uid>." >&2
    exit 2
  fi
  # GitHub token file (#3283): same no-silent-drop rule. A NON-comment
  # EnvironmentFile= line naming a github-token file must be re-supplied, or the
  # re-rendered unit would lose the gate's dedicated identity (the gate then
  # refuses every advance). Reads only, so it runs in --dry-run too.
  if printf '%s\n' "$_live" | grep -Eq -- '^[[:space:]]*EnvironmentFile[[:space:]]*=.*github-token' && [ -z "$GITHUB_TOKEN_FILE" ]; then
    echo "ERROR: $_EXISTING_UNIT already carries a github-token EnvironmentFile line; re-installing without" >&2
    echo "       --github-token-file <path> would silently drop the gate's dedicated GitHub identity." >&2
    echo "       Nothing was written. Re-run with --github-token-file <path>." >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# --service-dir (#3568): no baked-in location. A flag wins. Without one, reuse the
# FORUM_HOME of the unit being replaced (so the redeploy command, which passes no
# --service-dir, keeps working and can never start a second, empty forum
# somewhere new). No unit at all = a first install: refuse. A unit whose FORUM_HOME
# cannot be read: refuse naming it, never guess. Reads only (runs in --dry-run too),
# and runs before anything is written. forum-drift-check.sh carries the same rule.
# ---------------------------------------------------------------------------
# Print FORUM_HOME from a unit's live (non-comment) Environment= line, plain or
# quoted form, the last assignment winning; print nothing when there is none or it
# is not an absolute path. A FUNCTION defined here, called inside $( ) below: the
# case patterns must not sit inside a command substitution (macOS bash 3.2).
_unit_forum_home() {
  local _l _v="" _w
  while IFS= read -r _l || [ -n "$_l" ]; do
    _l="${_l#"${_l%%[![:space:]]*}"}"
    case "$_l" in
      '#'*|';'*) continue ;;
      'Environment=FORUM_HOME='*) _w="${_l#Environment=FORUM_HOME=}" ;;
      'Environment="FORUM_HOME='*) _w="${_l#Environment=\"FORUM_HOME=}"; _w="${_w%\"*}" ;;
      *) continue ;;
    esac
    _w="${_w%"${_w##*[![:space:]]}"}"
    _v="$_w"
  done < "$1"
  case "$_v" in
    *[[:space:]\"\']*) ;;
    /*) printf '%s\n' "$_v" ;;
  esac
}
if [ -z "$SERVICE_DIR" ]; then
  if [ ! -f "$_EXISTING_UNIT" ]; then
    echo "ERROR: --service-dir is required on a first install (e.g. /home/<shared>/forum)" >&2
    exit 2
  fi
  SERVICE_DIR="$(_unit_forum_home "$_EXISTING_UNIT")"
  if [ -z "$SERVICE_DIR" ]; then
    echo "ERROR: $_EXISTING_UNIT has no parsable absolute Environment=FORUM_HOME=<dir> line, so the" >&2
    echo "       service dir cannot be reused. Nothing was written. Pass --service-dir <dir> explicitly." >&2
    exit 2
  fi
  echo "==> --service-dir not given; reusing FORUM_HOME=$SERVICE_DIR from $_EXISTING_UNIT"
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

echo "==> Snapshotting pack validator → $APP/tools/engram-pkg"
RUN mkdir -p "$APP/tools/engram-pkg"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] cp $SRC/tools/engram-pkg/engram-pkg → $APP/tools/engram-pkg/engram-pkg"
elif command -v rsync >/dev/null; then
  rsync -a --delete \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='README.md' \
      "$SRC/tools/engram-pkg/" "$APP/tools/engram-pkg/"
else
  cp "$SRC/tools/engram-pkg/engram-pkg" "$APP/tools/engram-pkg/engram-pkg"
fi

# The baton gate module. server.py's _load_baton_gate() walk-parents-searches for
# tools/baton_gate.py, which in this layout is $APP/tools/baton_gate.py. Without
# it the loader returns None and every PR-anchored flip / claim / release fails
# closed (#3039 U2b). tools/baton_gate.py is a symlink to
# src/engram/tools/baton_gate.py, so copy the RESOLVED file (cp -L), never the link.
echo "==> Snapshotting baton gate → $APP/tools/baton_gate.py"
RUN mkdir -p "$APP/tools"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] cp -L $SRC/tools/baton_gate.py → $APP/tools/baton_gate.py (resolves the symlink)"
else
  cp -L "$SRC/tools/baton_gate.py" "$APP/tools/baton_gate.py"
fi

# The backup tool (#3568). The backup and alarm units run THIS snapshot, never a
# checkout, so the deployed backup code moves with deploys and not with whatever
# branch a checkout happens to be on. cp -L: copy the resolved file, never a link.
echo "==> Snapshotting backup tool → $APP/tools/forum_backup.py"
RUN mkdir -p "$APP/tools"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] cp -L $SRC/tools/forum_backup.py → $APP/tools/forum_backup.py (resolves any symlink)"
else
  cp -L "$SRC/tools/forum_backup.py" "$APP/tools/forum_backup.py"
fi

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
# Optional baton login map args (#3312): real continued ExecStart arguments, or
# nothing at all when no map was given.
BATON_ARGS=""
if [ -n "$BATON_MAP" ]; then
  BATON_ARGS=" \\"$'\n'"    --baton-login-map $BATON_MAP"
  for _u in $BATON_UIDS; do
    BATON_ARGS="$BATON_ARGS \\"$'\n'"    --baton-login-map-admin-uid $_u"
  done
fi
_TOKEN_PLACEHOLDER='@@GITHUB_TOKEN_ENVFILE_LINE@@'
_PLACEHOLDER='{{BATON_LOGIN_MAP_ARGS}}'
# Replace the baton-args placeholder line by line. A FUNCTION, deliberately: inline
# in `$( )` the quotes/`)` here are mis-parsed by macOS /bin/bash 3.2 (the pre-#3312
# script ran there; keep it that way). if/else, not `case`, for the same reason.
_fill_baton_placeholder() {
  local _line
  while IFS= read -r _line || [ -n "$_line" ]; do
    if [ "${_line#*"$_PLACEHOLDER"}" != "$_line" ]; then
      printf '%s%s\n' "${_line%"$_PLACEHOLDER"*}" "$BATON_ARGS"
    else
      printf '%s\n' "$_line"
    fi
  done
}
# The github-token EnvironmentFile line (#3283): the placeholder sits on a line of
# its own; it becomes the one `EnvironmentFile=-PATH` line, or the whole line is
# dropped when no --github-token-file was given. Same function shape, same reason.
_fill_token_placeholder() {
  local _line
  while IFS= read -r _line || [ -n "$_line" ]; do
    if [ "$_line" = "$_TOKEN_PLACEHOLDER" ]; then
      if [ -n "$GITHUB_TOKEN_FILE" ]; then
        printf 'EnvironmentFile=-%s\n' "$GITHUB_TOKEN_FILE"
      fi
    else
      printf '%s\n' "$_line"
    fi
  done
}
# Strip the >>>TEMPLATE-ONLY ... <<<TEMPLATE-ONLY block, then substitute
# {{FORUM_HOME}} (template placeholder), {{PORT}}, and the baton map args.
RENDERED="$(sed '/>>>TEMPLATE-ONLY/,/<<<TEMPLATE-ONLY/d' "$SRC/src/forum/deploy/engram-forum.service.template" \
    | sed "s|{{FORUM_HOME}}|$SERVICE_DIR|g" \
    | sed "s|--port 5002|--port $PORT|" \
    | sed "s|Flask, port 5002|Flask, port $PORT|" \
    | _fill_baton_placeholder \
    | _fill_token_placeholder)"
if printf '%s\n' "$RENDERED" | grep -Eq '[{][{]|@@GITHUB_TOKEN'; then
  echo "ERROR: unsubstituted placeholders remain in the rendered unit" >&2; exit 2
fi
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] sed TEMPLATE-ONLY strip + substitute {{FORUM_HOME}}→$SERVICE_DIR, {{PORT}}→$PORT"
  echo "  [dry-run] --- rendered unit begin ---"
  printf '%s\n' "$RENDERED"
  echo "  [dry-run] --- rendered unit end ---"
  echo "  [dry-run] install rendered unit → $UNIT_DIR/$UNIT"
else
  printf '%s\n' "$RENDERED" > "$UNIT_DIR/$UNIT"
fi

# Backup units (#3276, #3568). The backup service and the failure-alarm unit are
# RENDERED from templates ({{FORUM_HOME}} -> the service dir), never copied, so
# they name this install's own paths and no checkout. Operators hand-edit the
# backup service/timer (--packs-dir; see README.md), so those are installed ONLY IF
# ABSENT and a reinstall never reverts an edit; a kept unit that differs from what
# would be installed gets exactly one notice line. The failure-alarm unit is ours
# and is always (re)installed. A drop-in is ALWAYS written so even a hand-edited
# backup unit gains OnFailure= without being overwritten. The alarm unit has no
# [Install] section and is deliberately NOT enabled: it is a oneshot started only
# via OnFailure=, and enabling it would start it at login and post a false alarm.
# The timer is installed but not enabled (that stays the README runbook step);
# the daemon-reload below picks everything up.
# The alarm's @mention is per-install, never shipped: it is written into the
# alarm unit's drop-in mention.conf from $FORUM_BACKUP_ALARM_MENTION if set, else
# the installing admin's forum agent name ($USER minus a leading "agent-"), else
# empty (the alarm then posts without a mention). The value lands in a unit file,
# so it is validated BEFORE anything is written.
# >>> backup-units
_mention="${FORUM_BACKUP_ALARM_MENTION-}"
if [ -z "${FORUM_BACKUP_ALARM_MENTION+x}" ]; then
  _mention="${USER-}"
  _mention="${_mention#agent-}"
fi
case "$_mention" in
  *[!A-Za-z0-9_\ ,-]*)
    echo "ERROR: FORUM_BACKUP_ALARM_MENTION may contain only letters, digits, '_', '-', ',' and spaces (got an invalid character); refusing to write it into a unit file." >&2
    exit 2 ;;
esac
echo "==> Installing backup + failure-alarm units → $UNIT_DIR"
_BU_DEPLOY="$SRC/src/forum/deploy"
for _bu in engram-forum-backup.service.template engram-forum-backup.timer engram-forum-backup-alarm.service.template; do
  [ -f "$_BU_DEPLOY/$_bu" ] || { echo "ERROR: $_bu not found under $_BU_DEPLOY/" >&2; exit 2; }
done
# Render one unit template to stdout: drop the >>>TEMPLATE-ONLY block, substitute
# {{FORUM_HOME}}. A leftover placeholder is a refusal, never an installed unit.
_render_unit() {
  local _out
  _out="$(sed '/>>>TEMPLATE-ONLY/,/<<<TEMPLATE-ONLY/d' "$1" | sed "s|{{FORUM_HOME}}|$SERVICE_DIR|g")"
  if printf '%s\n' "$_out" | grep -q '[{][{]'; then
    echo "ERROR: unsubstituted placeholders remain in the rendered $(basename "$1")" >&2; exit 2
  fi
  printf '%s\n' "$_out"
}
# Install a rendered unit (always overwrites): $1 template file, $2 unit name.
_install_rendered_unit() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] --- rendered $2 begin ---"
    _render_unit "$1"
    echo "  [dry-run] --- rendered $2 end ---"
    echo "  [dry-run] install rendered unit → $UNIT_DIR/$2"
  else
    _render_unit "$1" > "$UNIT_DIR/$2"
  fi
}
# One notice line when a KEPT unit is not what this installer would put there.
_note_if_kept_unit_differs() {
  local _name="$1" _same=0
  if [ "$_name" = "engram-forum-backup.timer" ]; then
    cmp -s "$_BU_DEPLOY/$_name" "$UNIT_DIR/$_name" && _same=1
  else
    _render_unit "$_BU_DEPLOY/$_name.template" | cmp -s - "$UNIT_DIR/$_name" && _same=1
  fi
  if [ "$_same" -eq 0 ]; then
    echo "    NOTE: kept $UNIT_DIR/$_name differs from what this installer would install (left as is)"
  fi
}
RUN mkdir -p "$UNIT_DIR"
_install_rendered_unit "$_BU_DEPLOY/engram-forum-backup-alarm.service.template" engram-forum-backup-alarm.service
for _bu in engram-forum-backup.service engram-forum-backup.timer; do
  if [ -e "$UNIT_DIR/$_bu" ]; then
    echo "    keeping existing $_bu (not overwritten; may be hand-edited)"
    _note_if_kept_unit_differs "$_bu"
  elif [ "$_bu" = "engram-forum-backup.timer" ]; then
    RUN cp "$_BU_DEPLOY/$_bu" "$UNIT_DIR/$_bu"
  else
    _install_rendered_unit "$_BU_DEPLOY/$_bu.template" "$_bu"
  fi
done
RUN mkdir -p "$UNIT_DIR/engram-forum-backup.service.d"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] write drop-in $UNIT_DIR/engram-forum-backup.service.d/alarm.conf (OnFailure=engram-forum-backup-alarm.service)"
else
  printf '[Unit]\nOnFailure=engram-forum-backup-alarm.service\n' \
    > "$UNIT_DIR/engram-forum-backup.service.d/alarm.conf"
fi
RUN mkdir -p "$UNIT_DIR/engram-forum-backup-alarm.service.d"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] write drop-in $UNIT_DIR/engram-forum-backup-alarm.service.d/mention.conf (FORUM_BACKUP_ALARM_MENTION=\"$_mention\")"
else
  printf '[Service]\nEnvironment="FORUM_BACKUP_ALARM_MENTION=%s"\n' "$_mention" \
    > "$UNIT_DIR/engram-forum-backup-alarm.service.d/mention.conf"
fi

# Loud on a dead target (#3568). A backup timer that points at nothing fails
# silently every day, so before anything is restarted read the backup service that
# will actually be LIVE (rendered or kept) and refuse unless every absolute path in
# its ExecStart exists: the interpreter and the script as files, and the parent
# directory of each --db / --backup-dir / --audit value. Reads only. An ExecStart
# that is absent or cannot be read this way is a refusal naming the unit, never a
# silent pass. %h is expanded; continuation lines are joined; tokens split on
# whitespace (no quote handling). Under --dry-run the venv and the snapshot do not
# exist, so it only says what it would do. ALL the branching lives in this one
# function (called right after this region, i.e. after the venv and the snapshot
# exist and before any systemctl action) so a test can extract and drive it.
_check_backup_exec_paths() {
  local _unit="$1"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] would check every ExecStart path of $_unit"
    return 0
  fi
  _bce_fail() {
    echo "ERROR: $_unit: $1" >&2
    echo "       Nothing was restarted. Fix the unit (or delete it so it is re-rendered) and re-run." >&2
    exit 2
  }
  [ -f "$_unit" ] || _bce_fail "unit file not found, so its ExecStart cannot be checked"
  local _line _t _acc="" _in=0 _cmds="" _cmd _n=0
  while IFS= read -r _line || [ -n "$_line" ]; do
    _t="${_line#"${_line%%[![:space:]]*}"}"
    if [ "$_in" -eq 0 ]; then
      case "$_t" in
        ExecStart=*) _acc="${_t#ExecStart=}" ;;
        *) continue ;;
      esac
    else
      case "$_t" in
        '#'*|';'*) continue ;;
      esac
      _acc="$_acc $_t"
    fi
    case "$_acc" in
      *\\) _acc="${_acc%\\}"; _in=1 ;;
      *)
        _in=0
        if [ -z "${_acc//[[:space:]]/}" ]; then
          _cmds=""   # an empty ExecStart= resets the list, as systemd does
        else
          _cmds="$_cmds$_acc"$'\n'
        fi
        _acc="" ;;
    esac
  done < "$_unit"
  [ -n "$_cmds" ] || _bce_fail "no ExecStart= command found (absent or empty)"
  local _toks _tok _first _x _script _i _flag _val _parent
  set -f
  while IFS= read -r _cmd; do
    [ -n "$_cmd" ] || continue
    _toks=($_cmd)
    _first="${_toks[0]}"
    while :; do
      case "$_first" in
        [-@+!:]*) _first="${_first#?}" ;;
        *) break ;;
      esac
    done
    _first="${_first//\%h/$HOME}"
    case "$_first" in
      /*) ;;
      *) set +f; _bce_fail "ExecStart interpreter '$_first' is not an absolute path" ;;
    esac
    [ -f "$_first" ] || { set +f; _bce_fail "ExecStart interpreter $_first does not exist (or is not a file)"; }
    _script=""
    _i=1
    while [ "$_i" -lt "${#_toks[@]}" ]; do
      _tok="${_toks[$_i]}"
      case "$_tok" in
        -*) ;;
        *) _script="${_tok//\%h/$HOME}"; break ;;
      esac
      _i=$((_i + 1))
    done
    [ -n "$_script" ] || { set +f; _bce_fail "ExecStart names no script after the interpreter"; }
    case "$_script" in
      /*) ;;
      *) set +f; _bce_fail "ExecStart script '$_script' is not an absolute path" ;;
    esac
    [ -f "$_script" ] || { set +f; _bce_fail "ExecStart script $_script does not exist (or is not a file)"; }
    _i=1
    while [ "$_i" -lt "${#_toks[@]}" ]; do
      _tok="${_toks[$_i]}"
      _flag=""; _val=""
      case "$_tok" in
        --db|--backup-dir|--audit)
          _flag="$_tok"; _i=$((_i + 1))
          [ "$_i" -lt "${#_toks[@]}" ] && _val="${_toks[$_i]}" ;;
        --db=*|--backup-dir=*|--audit=*)
          _flag="${_tok%%=*}"; _val="${_tok#*=}" ;;
      esac
      if [ -n "$_flag" ]; then
        _val="${_val//\%h/$HOME}"
        case "$_val" in
          /*)
            _parent="${_val%/*}"
            [ -n "$_parent" ] || _parent="/"
            [ -d "$_parent" ] || { set +f; _bce_fail "ExecStart $_flag parent directory $_parent does not exist (for $_val)"; } ;;
        esac
      fi
      _i=$((_i + 1))
    done
  done <<EOF_BCE
$_cmds
EOF_BCE
  set +f
  echo "    backup unit ExecStart paths all exist: $_unit"
}
# <<< backup-units
_check_backup_exec_paths "$UNIT_DIR/engram-forum-backup.service"

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
    echo "==> Post-install gate 1: HTTP health check (up to 20s — model load grace)"
    _health_ok=0
    for _attempt in $(seq 1 20); do
      if curl --fail --silent --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "    OK: forum /health responding on :${PORT} (attempt ${_attempt})"
        _health_ok=1
        break
      fi
      sleep 1
    done
    if [ "$_health_ok" -eq 0 ]; then
      echo "ERROR: /health not responding after 20s — service failed to start." >&2
      echo "       Check: journalctl --user -u $UNIT -n 30" >&2
      exit 2
    fi

    echo "==> Post-install gate 2: boot-verify probe"
    # Run from $APP so `python -m forum.server` can import the `forum` package —
    # mirrors the service's WorkingDirectory=$APP. Without this cd the probe
    # runs in the deploy cwd, hits ModuleNotFoundError: forum, and false-alarms
    # "service may be degraded" on a perfectly healthy deploy (#1617).
    if ( cd "$APP" && "$PYTHON" -m forum.server \
        --db "$SERVICE_DIR/forum.db" \
        --audit "$SERVICE_DIR/forum-audit.jsonl" \
        --verify-only ); then
      echo "    verify-only passed"
    else
      echo "ERROR: verify-only probe returned non-zero — dep check failed; service may be degraded." >&2
      exit 2
    fi
  fi
else
  echo "==> --no-start: unit installed + enabled, NOT started (do the DB cutover, then: systemctl --user start $UNIT)"
fi

# forum.url for the ADMIN seat's own config (#3557). "Forum configured" means
# config.json forum.url is a non-empty string, and this is the one place that
# knows this host now has a forum on $PORT. All branching lives in the tool
# (src/engram/tools/set_forum_url.py); a refusal or failure only WARNs with the
# exact manual command and never changes this installer's exit status. Other
# seats are told what to run; the LAN address is never guessed.
# >>> forum-url-step
_FU_TOOL="$SRC/src/engram/tools/set_forum_url.py"
_FU_CFG="${ENGRAM_HOME:-$HOME/.engram}/config.json"
_FU_LOCAL="http://localhost:$PORT"
# Printed commands are shell-quoted (printf %q, fine in bash 3.2) so a path with
# a space still pastes into a shell and runs; the real call below is quoted too.
_FU_TOOL_Q="$(printf '%q' "$_FU_TOOL")"
_FU_CFG_Q="$(printf '%q' "$_FU_CFG")"
echo "==> Setting forum.url for the admin seat → $_FU_CFG"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  [dry-run] python3 $_FU_TOOL_Q $_FU_LOCAL --config $_FU_CFG_Q"
else
  _fu_rc=0
  _fu_out="$(python3 "$_FU_TOOL" "$_FU_LOCAL" --config "$_FU_CFG" 2>&1)" || _fu_rc=$?
  echo "    forum.url: ${_fu_out:-no output from the tool} (exit $_fu_rc)"
  if [ "$_fu_rc" -ne 0 ]; then
    echo "    WARN: forum.url was NOT set; the install itself succeeded. To set it by hand:" >&2
    echo "      python3 $_FU_TOOL_Q $_FU_LOCAL --config $_FU_CFG_Q" >&2
    echo "    (add --force to overwrite a different existing url)" >&2
  fi
fi
echo "    Other seats on THIS host:       python3 $_FU_TOOL_Q http://localhost:$PORT"
echo "    Seats on ANOTHER host (LAN):    python3 $_FU_TOOL_Q http://<this-host-address>:$PORT"
# <<< forum-url-step

echo "==> Done."
echo "    Manage: systemctl --user {status,restart,stop} $UNIT  ·  Logs: journalctl --user -u $UNIT -f"
echo "    Shared data: $SERVICE_DIR/forum.db (group $GROUP, group-writable)"
if [ "$DRY_RUN" -eq 0 ] && [ "$LINGER_OK" -eq 0 ]; then
  echo "    REMAINING STEP for reboot-survival (one-time, needs sudo):  sudo loginctl enable-linger $ADMIN_USER"
fi
