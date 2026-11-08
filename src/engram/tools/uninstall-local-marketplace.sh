#!/usr/bin/env bash
# uninstall-local-marketplace.sh — removes the ENGRAM local plugin marketplace.
#
# Safe defaults — this script removes the plugin registration and assembled
# marketplace tree WITHOUT touching your ENGRAM data or agent identity:
#
#   STOPPED AND REMOVED FIRST (#4446, before the tree below goes; only what belongs to THIS home):
#     LaunchAgents / systemd user units of this ENGRAM_HOME (surface daemon, engramd owner, and the
#     old fixed-name surface unit): stopped (launchctl bootout / systemctl --user disable --now),
#     then their plist / unit file and enable link are deleted. Ownership is the unit's own
#     ENGRAM_HOME; another home's units, and any whose home cannot be read, are left and named.
#     Hook-started daemons named by $ENGRAM_HOME/recall-daemon.pid and engramd.pid: SIGTERM, a bounded
#     wait, never SIGKILL, and only when the pidfile IN THIS HOME names a live process that carries
#     that daemon's marker. That proves "a live daemon of that kind", not "this home's daemon": the
#     marker check does not read the process's own ENGRAM_HOME.
#     The viz units are NOT per-home: left and named unless --include-viz. The Linux viz units
#     (engram-viz-user.service, engram-workbench-user.service) get no home check at all, so
#     --include-viz is the only consent; the macOS viz plist additionally needs its logs under this home.
#
#   REMOVED:
#     $ENGRAM_HOME/marketplace/       assembled plugin tree
#     $ENGRAM_HOME/.deployed-version  install version marker
#     host CLI registration           (claude plugin marketplace remove / codex plugin remove)
#
#   PRESERVED:
#     $ENGRAM_HOME/knowledge.db       your entire ENGRAM graph
#     $ENGRAM_HOME/history/           awake-state milestone log
#     $ENGRAM_HOME/diary/             private diary entries
#     $ENGRAM_HOME/warm-briefing.md   relational context letter
#     $ENGRAM_HOME/config.json        agent configuration
#     $HOME/.engram/venv/              Python environment
#     ~/.claude/CLAUDE.md             agent identity configuration
#
#   RESTORED (only if a pre-engram backup exists, #4440):
#     ~/.claude/CLAUDE.md.pre-engram  -> ~/.claude/CLAUDE.md   (--target claude-code)
#     ~/.codex/config.toml.pre-engram -> ~/.codex/config.toml  (--target codex)
#     ~/.codex/AGENTS.md.pre-engram   -> ~/.codex/AGENTS.md    (--target codex, #4452)
#     The CURRENT file is first copied to <name>.pre-uninstall.<UTC timestamp>, so edits
#     made since install are never lost. No backup: the file is left untouched.
#
# For a complete data reset, remove $ENGRAM_HOME/ manually AFTER running this
# script. See README.md § Uninstall for the full decision tree.
#
# USAGE:
#   bash tools/uninstall-local-marketplace.sh [--help] [--dry-run]
#       [--skip-cli] [--include-viz] [--target <claude-code|codex>]
#
# OPTIONS:
#   --help        Show this help and exit.
#   --dry-run     Print what would be done without making any changes.
#   --skip-cli    Skip the host CLI unregistration step (marketplace tree
#                 removal still proceeds). Use when the CLI is unavailable
#                 or the marketplace was never registered.
#   --include-viz Also stop and remove the viz units (engram-viz-user.service,
#                 engram-workbench-user.service, com.engram.viz-server). They carry no
#                 home (a fixed name on one account), so by default they are left alone.
#   --target T    Host target: claude-code (default) or codex.
#
# ENGRAM_UNINSTALL_TERM_WAIT_S (default 10): seconds to wait for a SIGTERMed daemon to exit.

set -euo pipefail

# A RELATIVE ENGRAM_HOME is refused (alpha #3863): it would resolve against the calling process's cwd, a
# different home for every process. Unset or empty means unset; a leading ~ is expanded against $HOME and
# re-exported (~user is not expanded: it is not absolute, so it is refused). Checked before anything else
# runs; `return` covers a script that is sourced, `exit` one that is executed.
if [ -n "${ENGRAM_HOME-}" ]; then
    _eh_raw="$ENGRAM_HOME"
    case "$ENGRAM_HOME" in
        "~") ENGRAM_HOME="${HOME-}"; export ENGRAM_HOME ;;
        "~/"*) ENGRAM_HOME="${HOME-}/${ENGRAM_HOME#"~/"}"; export ENGRAM_HOME ;;
    esac
    case "$ENGRAM_HOME" in
        /*) ;;
        *)
            printf "ENGRAM_HOME must be an absolute path, got '%s'\n" "$_eh_raw" >&2
            unset _eh_raw
            return 2 2>/dev/null || exit 2 ;;
    esac
    unset _eh_raw
fi

log()  { printf '[uninstall-local-marketplace] %s\n' "$*"; }
warn() { printf '[uninstall-local-marketplace] WARN: %s\n' "$*"; }
die()  { printf '[uninstall-local-marketplace] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: bash tools/uninstall-local-marketplace.sh [--help] [--dry-run]
    [--skip-cli] [--include-viz] [--target <claude-code|codex>]

Stops and removes this home's services and daemons, then removes the ENGRAM plugin
registration and marketplace tree.
Preserves all ENGRAM data ($ENGRAM_HOME/knowledge.db, history, diary, etc.).

Options:
  --help        Show this help and exit.
  --dry-run     Print what would be done without making any changes.
  --skip-cli    Skip host CLI unregistration (remove marketplace tree only).
  --include-viz Also stop and remove the (shared, not per-home) viz units.
  --target T    claude-code (default) or codex.

To reinstall after uninstalling:
  bash tools/install-local-marketplace.sh

For a full data reset, remove $ENGRAM_HOME/ manually after running this script.
EOF
  exit 0
}

DRY_RUN=0
SKIP_CLI=0
INCLUDE_VIZ=0
TARGET="claude-code"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)    usage ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --skip-cli)   SKIP_CLI=1; shift ;;
    --include-viz) INCLUDE_VIZ=1; shift ;;
    --target)
      [[ $# -ge 2 ]] || die "--target requires a value: claude-code or codex"
      TARGET="$2"; shift 2 ;;
    --target=*)   TARGET="${1#--target=}"; shift ;;
    *)            die "Unknown argument: $1. Run with --help for usage." ;;
  esac
done
case "$TARGET" in
  claude-code|codex) ;;
  *) die "Invalid --target: $TARGET (expected claude-code or codex)" ;;
esac

ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
MARKETPLACE_ROOT="$ENGRAM_HOME/marketplace"
VERSION_MARKER="$ENGRAM_HOME/.deployed-version"

[[ $DRY_RUN -eq 1 ]] && log "[dry-run — no changes will be made]"

# ── Identity-file restore helper (#4440) ───────────────────────────────────────
# Only when a <name>.pre-engram backup exists. The current file is saved first (the agent and
# the user edit it for weeks, so restoring over it would silently drop all of that).
restore_identity_file() {
  local file="$1" backup="$1.pre-engram" stamp saved
  if [[ ! -e "$backup" && ! -L "$backup" ]]; then
    log "No $backup — $file left untouched."
    return 0
  fi
  if [[ -L "$backup" && ! -e "$backup" ]]; then
    warn "$backup is a dangling symlink — $file left untouched."
    return 0
  fi
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  saved="$file.pre-uninstall.$stamp"
  if [[ -e "$file" || -L "$file" ]]; then
    if [[ -L "$file" && ! -e "$file" ]]; then
      warn "$file is a dangling symlink — not restoring over it."
      return 0
    fi
    if [[ -e "$saved" || -L "$saved" ]]; then
      warn "$saved already exists — $file left untouched; re-run in a second."
      return 0
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
      log "(dry-run) Would copy $file to $saved, then restore $backup to $file"
      return 0
    fi
    cp -p "$file" "$saved" || die "could not save $file to $saved; $file left untouched"
    log "Saved your current $file to $saved"
  elif [[ $DRY_RUN -eq 1 ]]; then
    log "(dry-run) Would restore $backup to $file"
    return 0
  fi
  cp -p "$backup" "$file" || die "could not restore $backup to $file"
  log "Restored $backup to $file (the backup is kept)."
}

# The identity files this target restores (Codex has two: config.toml and AGENTS.md, #4452).
restore_identity_files() {
  if [[ "$TARGET" == "codex" ]]; then
    restore_identity_file "$CODEX_HOME/config.toml"
    restore_identity_file "$CODEX_HOME/AGENTS.md"
  else
    restore_identity_file "$CLAUDE_HOME/CLAUDE.md"
  fi
}

# ── Services and daemons of THIS home (#4446) ────────────────────────────────────
# Runs BEFORE the marketplace tree goes (their ExecStart trees live in it) and on the "Nothing to
# uninstall" path too. No unit stops itself once its tree is gone, so every unit is stopped first and
# only then deleted. Ownership is the unit's own ENGRAM_HOME; keyed names only find the candidates.
# The helpers are sourced from this script's OWN directory (never from the marketplace tree).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_SUMMARY=""
SVC_NEED_RELOAD=0

svc_note() { SVC_SUMMARY="${SVC_SUMMARY}   $*"$'\n'; }
svc_left() { # <name> <reason>
  log "Left alone: $1 ($2)"
  svc_note "Left alone: $1 ($2)"
}

# The fixed supervisor names of the pre-#4305 install (the legacy-transition code; the G1 cell of
# tests/test_surface_label_keyed_4305.py allowlists these two functions by name).
_uninst_legacy_surface_unit() {
  printf '%s' 'engram-surface-daemon.service'
}
_uninst_legacy_surface_label() {
  printf '%s' 'com.engram.surface-daemon'
}

# _uninst_home_norm_into <path> <varname>: the physical path of <path> if it is a directory, else the raw
# string with trailing slashes stripped (the same rule as setup-surface-daemon-service.sh's
# _home_norm_into, which lives in a script that cannot be sourced).
_uninst_home_norm_into() {
  local LC_ALL=C _in="${1-}" _name="${2-}" _out
  _out="$(cd "$_in" 2>/dev/null && pwd -P)" || {
    _out="$_in"
    while [[ "${#_out}" -gt 1 && "$_out" == */ ]]; do _out="${_out%/}"; done
  }
  printf -v "$_name" '%s' "$_out"
}

# _svc_same_home <home as read from a unit>: succeeds when it is this home.
_svc_same_home() {
  local a b
  _uninst_home_norm_into "$1" a
  _uninst_home_norm_into "$ENGRAM_HOME" b
  [[ "$a" == "$b" ]]
}

# _plist_string <key> <plist>: the <string> that follows <key> (one line or several).
_plist_string() {
  tr '\n' ' ' < "$2" | sed -n "s|.*<key>$1</key>[[:space:]]*<string>\([^<]*\)</string>.*|\1|p" | head -n 1
}

# _svc_path_under_home <path>: its directory is inside this home.
_svc_path_under_home() {
  local d h
  _uninst_home_norm_into "$(dirname "$1")" d
  _uninst_home_norm_into "$ENGRAM_HOME" h
  [[ "$d" == "$h" || "$d" == "$h"/* ]]
}

# _svc_mac_one <label> <home|viz>: one LaunchAgent. "home" = ownership by ENGRAM_HOME; "viz" = shared
# (left unless --include-viz, then only when its log paths are under this home).
_svc_mac_one() {
  local label="$1" mode="$2" plist="$AGENTS_DIR/$1.plist" dom out loaded=0 jobhome="" lp i
  dom="gui/$(id -u)/$label"
  if out="$(launchctl print "$dom" 2>/dev/null)"; then
    loaded=1
    jobhome="$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*ENGRAM_HOME => //p' | head -n 1 || true)"
  fi
  if [[ $loaded -eq 0 && ! -f "$plist" ]]; then return 0; fi
  if [[ "$mode" == "viz" ]]; then
    if [[ $INCLUDE_VIZ -eq 0 ]]; then
      svc_left "$label" "shared, not tied to this home; pass --include-viz to remove"
      return 0
    fi
    if [[ ! -f "$plist" ]]; then
      svc_left "$label" "no plist to show its log paths are under this home"
      return 0
    fi
    for lp in "$(_plist_string StandardOutPath "$plist" || true)" "$(_plist_string StandardErrorPath "$plist" || true)"; do
      if [[ -z "$lp" ]] || ! _svc_path_under_home "$lp"; then
        svc_left "$label" "its log paths are not under this home ($ENGRAM_HOME), so it is not provably this home's"
        return 0
      fi
    done
  else
    if [[ -z "$jobhome" && -f "$plist" ]]; then
      jobhome="$(_plist_string ENGRAM_HOME "$plist" || true)"
    fi
    if [[ -z "$jobhome" ]]; then
      svc_left "$label" "could not read its ENGRAM_HOME"
      return 0
    fi
    if ! _svc_same_home "$jobhome"; then
      svc_left "$label" "belongs to ENGRAM_HOME=$jobhome, not this home"
      return 0
    fi
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    if [[ $loaded -eq 1 ]]; then
      log "(dry-run) Would run: launchctl bootout $dom, then delete $plist"
    else
      log "(dry-run) Would delete $plist ($label is not loaded; no bootout needed)"
    fi
    svc_note "(dry-run) would stop and remove: $label"
    return 0
  fi
  if [[ $loaded -eq 1 ]]; then
    launchctl bootout "$dom" >/dev/null 2>&1 || true
    i=0
    while launchctl print "$dom" >/dev/null 2>&1; do
      if [[ $i -ge 20 ]]; then
        svc_left "$label" "could not stop it: still loaded after launchctl bootout; plist kept"
        return 0
      fi
      i=$((i + 1))
      sleep 0.1
    done
    log "Stopped: $label (launchctl bootout)"
    svc_note "Stopped: $label"
  fi
  if [[ -f "$plist" ]]; then
    if [[ $loaded -eq 0 ]]; then
      # `launchctl print` failing does not prove the job is not loaded (it also fails on a headless
      # Mac). Deleting the plist under a running job would leave it running on a tree about to go, so
      # bootout first; it is harmless when nothing is loaded.
      launchctl bootout "$dom" >/dev/null 2>&1 || true
    fi
    rm -f "$plist"
    if [[ $loaded -eq 1 ]]; then
      log "Removed: $plist"
    else
      log "Removed: $plist ($label was not loaded)"
    fi
    svc_note "Removed: $plist"
  fi
}

# _svc_linux_one <unit> <home|viz>: one systemd user unit.
_svc_linux_one() {
  local unit="$1" mode="$2" file="$UNIT_DIR/$1" jobhome=""
  if [[ ! -e "$file" && ! -L "$file" ]]; then return 0; fi
  if [[ "$mode" == "viz" ]]; then
    if [[ $INCLUDE_VIZ -eq 0 ]]; then
      svc_left "$unit" "shared, not tied to this home; pass --include-viz to remove"
      return 0
    fi
  else
    jobhome="$(sed -n 's/^Environment=ENGRAM_HOME=//p' "$file" | head -n 1 || true)"
    if [[ -z "$jobhome" ]]; then
      svc_left "$unit" "could not read its ENGRAM_HOME"
      return 0
    fi
    if ! _svc_same_home "$jobhome"; then
      svc_left "$unit" "belongs to ENGRAM_HOME=$jobhome, not this home"
      return 0
    fi
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    log "(dry-run) Would run: systemctl --user disable --now $unit, then delete $UNIT_DIR/default.target.wants/$unit and $file"
    svc_note "(dry-run) would stop and remove: $unit"
    SVC_NEED_RELOAD=1
    return 0
  fi
  if ! systemctl --user disable --now "$unit" >/dev/null 2>&1; then
    svc_left "$unit" "could not stop and disable it; unit file kept"
    return 0
  fi
  log "Stopped: $unit (systemctl --user disable --now)"
  svc_note "Stopped: $unit"
  rm -f "$UNIT_DIR/default.target.wants/$unit"
  rm -f "$file"
  log "Removed: $file"
  svc_note "Removed: $file"
  SVC_NEED_RELOAD=1
}

# _svc_other_keyed <own1> <own2> <suffix> <file>...: name the keyed units of OTHER homes (read-only).
_svc_other_keyed() {
  local own1="$1" own2="$2" suffix="$3" f n
  shift 3
  for f in "$@"; do
    [[ -e "$f" || -L "$f" ]] || continue
    n="$(basename "$f")"; n="${n%"$suffix"}"
    if [[ "$n" != "$own1" && "$n" != "$own2" ]]; then
      svc_left "$(basename "$f")" "keyed for a different home, not this one"
    fi
  done
}

# Section D: daemons a hook started (no unit). A pid is signalled only when the pidfile IN THIS HOME
# names a live process carrying that daemon's marker (engram_pidfile's recorded_live_pid with
# require_marker=True). That is "a live daemon of that kind", not proof it is this home's daemon.
# SIGTERM, a bounded wait, never SIGKILL.
uninstall_hook_daemons() {
  local py="$HOME/.engram/venv/bin/python3" root out rc=0 line
  [[ -x "$py" ]] || py="$(command -v python3 || true)"
  root="$(cd "$SCRIPT_DIR/.." && pwd)"
  if [[ -z "$py" ]]; then
    warn "no python3 found — hook-started daemons (recall-daemon.pid, engramd.pid) were not checked."
    return 0
  fi
  if [[ ! -f "$root/engram_pidfile.py" ]]; then
    warn "$root/engram_pidfile.py not found — hook-started daemons (recall-daemon.pid, engramd.pid) were not checked."
    return 0
  fi
  out="$("$py" -I -c '
import os, signal, stat, sys, time
root, home, dry, wait_s = sys.argv[1], sys.argv[2], sys.argv[3] == "1", float(sys.argv[4])
sys.path.insert(0, root)
import engram_pidfile as ep

def why_not(path, marker):
    # Report only; nothing here signals anything.
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return "not a regular file"
        with open(path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return "garbage or unreadable pidfile"
    if pid <= 0:
        return "garbage pidfile (not a positive pid)"
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return "dead: no such process"
    return "pid %d is alive but its argv has no %r, so it is not that daemon" % (pid, marker)

for fname, marker, name in (("recall-daemon.pid", "engram-surface-daemon", "recall daemon"),
                            ("engramd.pid", "engramd_owner", "engramd owner")):
    path = os.path.join(home, fname)
    if not os.path.lexists(path):
        continue
    # Passing means: the pidfile in this home names a live process whose argv carries the marker of
    # this kind of daemon. It does not read the ENGRAM_HOME of that process.
    pid = ep.recorded_live_pid(path, marker, require_marker=True)
    if pid is None:
        print("Not signalled: %s (%s; %s)" % (path, name, why_not(path, marker)))
        continue
    if dry:
        print("(dry-run) Would send SIGTERM to pid %d (a live %s process named by %s) and wait up to %gs" % (pid, name, path, wait_s))
        continue
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("Stopped: pid %d (%s) was already gone" % (pid, name))
        continue
    except OSError as exc:
        print("Still running: pid %d (%s); SIGTERM failed: %s" % (pid, name, exc))
        continue
    end = time.time() + wait_s
    alive = True
    while alive and time.time() < end:
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            alive = False
    if alive:
        print("Still running: pid %d (%s) did not exit within %gs of SIGTERM; not killed" % (pid, name, wait_s))
    else:
        print("Stopped: pid %d (a live %s process named by %s) after SIGTERM" % (pid, name, path))
' "$root" "$ENGRAM_HOME" "$DRY_RUN" "${ENGRAM_UNINSTALL_TERM_WAIT_S:-10}" 2>&1)" || rc=$?
  if [[ $rc -ne 0 ]]; then
    warn "checking hook-started daemons failed (exit $rc): $out"
    return 0
  fi
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    log "$line"
    svc_note "$line"
  done <<< "$out"
}

uninstall_services() {
  local os key="" s_name o_name bak
  os="$(uname -s)"
  if key="$(engramd_owner_home_key "$ENGRAM_HOME" 2>/dev/null)" && [[ -n "$key" ]]; then
    :
  else
    key=""
    warn "cannot key unit names on $ENGRAM_HOME (it must exist); only the old fixed-name units are checked."
  fi
  case "$os" in
    Darwin)
      if ! command -v launchctl >/dev/null 2>&1; then
        warn "'launchctl' not found — LaunchAgents were not checked."
      else
        AGENTS_DIR="$HOME/Library/LaunchAgents"
        if [[ -n "$key" ]]; then
          s_name="com.engram.surface-daemon.$key"
          o_name="com.engram.engramd-owner.$key"
          _svc_mac_one "$s_name" home
          _svc_mac_one "$o_name" home
          _svc_other_keyed "$s_name" "$o_name" ".plist" \
            "$AGENTS_DIR/$(_uninst_legacy_surface_label)".*.plist "$AGENTS_DIR"/com.engram.engramd-owner.*.plist
        fi
        _svc_mac_one "$(_uninst_legacy_surface_label)" home
        _svc_mac_one "com.engram.viz-server" viz
        for bak in "$AGENTS_DIR"/com.engram.viz-server.plist.bak-pre-setup-*; do
          [[ -e "$bak" ]] || continue
          log "Kept: $bak (your earlier plist from before the viz setup)"
          svc_note "Kept: $bak (your earlier plist)"
        done
      fi ;;
    Linux)
      if ! command -v systemctl >/dev/null 2>&1; then
        warn "'systemctl' not found — systemd user units were not checked."
      else
        UNIT_DIR="$HOME/.config/systemd/user"
        if [[ -n "$key" ]]; then
          s_name="engram-surface-daemon-$key.service"
          o_name="engramd-owner-$key.service"
          _svc_linux_one "$s_name" home
          _svc_linux_one "$o_name" home
          _svc_other_keyed "$s_name" "$o_name" "" \
            "$UNIT_DIR"/engram-surface-daemon-*.service "$UNIT_DIR"/engramd-owner-*.service
        fi
        _svc_linux_one "$(_uninst_legacy_surface_unit)" home
        _svc_linux_one "engram-viz-user.service" viz
        _svc_linux_one "engram-workbench-user.service" viz
        if [[ $SVC_NEED_RELOAD -eq 1 ]]; then
          if [[ $DRY_RUN -eq 1 ]]; then
            log "(dry-run) Would run: systemctl --user daemon-reload"
          else
            systemctl --user daemon-reload >/dev/null 2>&1 \
              || warn "'systemctl --user daemon-reload' failed; run it by hand."
          fi
        fi
      fi ;;
    *) log "Unsupported OS '$os' — no services or LaunchAgents to check." ;;
  esac
  uninstall_hook_daemons
}

print_services_summary() {
  local line
  log " "
  log " Services and daemons of this home:"
  if [[ -z "$SVC_SUMMARY" ]]; then
    log "   (none found)"
  else
    while IFS= read -r line; do
      [[ -n "$line" ]] && log "$line"
    done <<< "$SVC_SUMMARY"
  fi
  return 0
}

# shellcheck source=engramd-owner-home-key.sh
if [[ -f "$SCRIPT_DIR/engramd-owner-home-key.sh" ]]; then
  source "$SCRIPT_DIR/engramd-owner-home-key.sh"
else
  engramd_owner_home_key() { return 1; }
fi
uninstall_services

# ── Nothing to do? ──────────────────────────────────────────────────────────
if [[ ! -d "$MARKETPLACE_ROOT" && ! -f "$VERSION_MARKER" ]]; then
  log "Nothing to uninstall — marketplace and version marker both absent."
  # A leftover .pre-engram backup must still be restored (the restore has no dependence on
  # the marketplace tree); with no backup this logs "left untouched" and changes nothing.
  restore_identity_files
  if [[ $SKIP_CLI -eq 0 ]]; then
    warn "If the plugin is still registered with Claude Code, run:"
    warn "  claude plugin marketplace remove engram-local"
    warn "  (or from inside Claude Code: /plugin disable engram)"
  fi
  print_services_summary
  exit 0
fi

# ── 1. Unregister from host CLI ─────────────────────────────────────────────
if [[ $SKIP_CLI -eq 1 ]]; then
  log "Skipping host CLI unregistration (--skip-cli)"
elif [[ "$TARGET" == "codex" ]]; then
  if command -v codex >/dev/null 2>&1; then
    log "Removing plugin from Codex..."
    if [[ $DRY_RUN -eq 0 ]]; then
      codex plugin remove engram@engram-local 2>/dev/null \
        && log "  Codex plugin removed." \
        || warn "  'codex plugin remove' failed or not supported. Remove manually: codex plugin remove engram@engram-local"
    else
      log "  (dry-run) Would run: codex plugin remove engram@engram-local"
    fi
  else
    warn "'codex' not on PATH — skipping CLI unregistration."
    warn "  Remove manually: codex plugin remove engram@engram-local"
  fi
else
  if command -v claude >/dev/null 2>&1; then
    log "Unregistering marketplace from Claude Code..."
    if [[ $DRY_RUN -eq 0 ]]; then
      claude plugin marketplace remove engram-local 2>/dev/null \
        && log "  Marketplace unregistered." \
        || {
          warn "  'claude plugin marketplace remove' returned non-zero."
          warn "  If the plugin is still visible in Claude Code, run inside a session:"
          warn "    /plugin disable engram"
          warn "    /plugin marketplace remove engram-local"
        }
    else
      log "  (dry-run) Would run: claude plugin marketplace remove engram-local"
    fi
  else
    warn "'claude' not on PATH — skipping CLI unregistration."
    warn "  Run inside a Claude Code session: /plugin disable engram"
  fi
fi

# ── 2. Remove marketplace tree ──────────────────────────────────────────────
REMOVED_MARKETPLACE=0
if [[ -d "$MARKETPLACE_ROOT" ]]; then
  if [[ $DRY_RUN -eq 0 ]]; then
    log "Removing $MARKETPLACE_ROOT"
    rm -rf "$MARKETPLACE_ROOT"
    REMOVED_MARKETPLACE=1
  else
    log "(dry-run) Would remove: $MARKETPLACE_ROOT"
  fi
fi

# ── 3. Remove version marker ─────────────────────────────────────────────────
REMOVED_MARKER=0
if [[ -f "$VERSION_MARKER" ]]; then
  if [[ $DRY_RUN -eq 0 ]]; then
    log "Removing $VERSION_MARKER"
    rm -f "$VERSION_MARKER"
    REMOVED_MARKER=1
  else
    log "(dry-run) Would remove: $VERSION_MARKER"
  fi
fi

# ── 4. Restore the identity file (function defined above, before the early exit) ──
restore_identity_files

# ── Done ─────────────────────────────────────────────────────────────────────
log ""
log "═══════════════════════════════════════════════════════════════════"
print_services_summary
if [[ $DRY_RUN -eq 1 ]]; then
  log " Dry run complete. Run without --dry-run to apply the changes."
else
  log " Uninstall complete."
  log " "
  log " Removed:"
  [[ $REMOVED_MARKETPLACE -eq 1 ]] && log "   $MARKETPLACE_ROOT (plugin tree)"
  [[ $REMOVED_MARKER -eq 1 ]]      && log "   $VERSION_MARKER (version marker)"
  [[ $REMOVED_MARKETPLACE -eq 0 && $REMOVED_MARKER -eq 0 ]] && log "   (nothing — both were already absent)"
  log " "
  log " Preserved (ENGRAM data is intact):"
  log "   $ENGRAM_HOME/knowledge.db  — your ENGRAM graph"
  log "   $ENGRAM_HOME/history/      — session history"
  log "   $ENGRAM_HOME/diary/        — diary entries"
  log "   $ENGRAM_HOME/config.json   — configuration"
  log "   $HOME/.engram/venv/         — Python environment"
  log "   ~/.claude/CLAUDE.md        — agent identity"
  log " "
  log " To reinstall: bash tools/install-local-marketplace.sh"
  log " For a full data reset: manually remove $ENGRAM_HOME/"
fi
log "═══════════════════════════════════════════════════════════════════"
