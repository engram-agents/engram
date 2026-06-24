#!/usr/bin/env bash
# engram-fix-git-backup.sh — ONE-TIME cleanup for existing ENGRAM installs
#
# Existing installs may have a bloated ~/.engram/.git from:
#   (a) Binary files tracked: knowledge.db, *.db-wal, *.db-shm, *.bak, diary.db
#   (b) knowledge.sql containing full ~384-float embedding vectors per node
#       (~79 MB per commit, no delta compression → .git balloons to GBs)
#
# This script creates a clean, embedding-stripped git history from scratch.
# It is SAFE (never deletes or mutates knowledge.db) and IDEMPOTENT.
#
# Usage:
#   bash tools/engram-fix-git-backup.sh              # live run
#   bash tools/engram-fix-git-backup.sh --dry-run    # preview only, no changes
#
# Env vars:
#   ENGRAM_HOME   ENGRAM data directory (default: ~/.engram)
#   PYTHON_BIN    Python interpreter (default: $ENGRAM_HOME/venv/bin/python if
#                 present, else python3 with a warning about sqlite_vec)
#
# Steps:
#   0. Refuse unless knowledge.db exists (safety gate)
#   1. Create out-of-tree safety snapshot of knowledge.db (Python sqlite3.backup API)
#      and verify node count matches live DB
#   2. Write / refresh .gitignore with canonical patterns
#   3. Regenerate knowledge.sql embedding-stripped (pure-Python: backup→null→dump)
#   4. Verify stripped dump rebuilds losslessly (node/edge/edit_history counts)
#      BEFORE touching git
#   5. Drop old git history + fresh git init + add (respecting .gitignore)
#      Verify NO *.db/*.log/junk staged before commit
#      Create clean root commit
#   6. Print the force-push command for the operator to run manually
#      (never auto-force-push — destructive on shared remote)
#
# ENGRAM data (knowledge.db and friends) is NEVER deleted or mutated.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENGRAM_HOME="${ENGRAM_HOME:-$HOME/.engram}"
# Resolve python: honour explicit $PYTHON_BIN override; else prefer the ENGRAM
# venv (which has sqlite_vec); else fall back to system python3 with a warning.
if [[ -z "${PYTHON_BIN:-}" ]]; then
    if [[ -x "$ENGRAM_HOME/venv/bin/python" ]]; then
        PYTHON_BIN="$ENGRAM_HOME/venv/bin/python"
    else
        PYTHON_BIN="python3"
        echo "[engram-fix-git-backup] WARNING: venv python not found at $ENGRAM_HOME/venv/bin/python; falling back to system python3 — the SQL dump may fail on vec0 tables if sqlite_vec is missing." >&2
    fi
fi
DRY_RUN=0

# Resolve the engram_backup.py module (same dir as this script's repo root)
ENGRAM_BACKUP="$(dirname "$(readlink -f "$0")")/../engram_backup.py"
if [[ ! -f "$ENGRAM_BACKUP" ]]; then
    echo "[engram-fix-git-backup] ERROR: engram_backup.py not found at $ENGRAM_BACKUP" >&2
    exit 1
fi
PY="$PYTHON_BIN"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--dry-run]" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
TS=$(date -u '+%Y%m%dT%H%M%SZ')

log()  { echo "[engram-fix-git-backup] $*"; }
warn() { echo "[engram-fix-git-backup] WARNING: $*" >&2; }
die()  { echo "[engram-fix-git-backup] ERROR: $*" >&2; exit 1; }

dry_or_run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [DRY-RUN] would run: $*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Step 0: Preflight checks
# ---------------------------------------------------------------------------
log "Preflight checks..."

DB_PATH="$ENGRAM_HOME/knowledge.db"
if [[ ! -f "$DB_PATH" ]]; then
    die "knowledge.db not found at $DB_PATH — refusing to proceed (no data to protect)"
fi

if ! command -v git &>/dev/null; then
    die "git not found in PATH — install git and retry"
fi

log "ENGRAM_HOME : $ENGRAM_HOME"
log "DB_PATH     : $DB_PATH"
log "DRY_RUN     : $([[ $DRY_RUN -eq 1 ]] && echo yes || echo no)"

# ---------------------------------------------------------------------------
# Step 1: Out-of-tree safety snapshot
# ---------------------------------------------------------------------------
SAFETY_SNAPSHOT="$HOME/.engram-db-safety-$TS.db"
log ""
log "Step 1: Creating safety snapshot at $SAFETY_SNAPSHOT"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [DRY-RUN] would run: $PY -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' $DB_PATH $SAFETY_SNAPSHOT"
    echo "  [DRY-RUN] would verify: node count in snapshot matches live DB"
else
    "$PY" -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" "$DB_PATH" "$SAFETY_SNAPSHOT"
    if [[ ! -f "$SAFETY_SNAPSHOT" ]]; then
        die "Safety snapshot was not created — aborting"
    fi

    # Verify node count matches
    LIVE_NODES=$("$PY" -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT count(*) FROM nodes').fetchone()[0])" "$DB_PATH")
    SNAP_NODES=$("$PY" -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT count(*) FROM nodes').fetchone()[0])" "$SAFETY_SNAPSHOT")
    if [[ "$LIVE_NODES" != "$SNAP_NODES" ]]; then
        die "Node count mismatch: live=$LIVE_NODES snap=$SNAP_NODES — snapshot is corrupted, aborting"
    fi
    log "  Safety snapshot verified: $LIVE_NODES nodes match"
fi

# ---------------------------------------------------------------------------
# Step 2: Write / refresh .gitignore
# ---------------------------------------------------------------------------
log ""
log "Step 2: Writing canonical .gitignore"

GITIGNORE_PATH="$ENGRAM_HOME/.gitignore"
GITIGNORE_CONTENT='*.db
*.db-shm
*.db-wal
*.db-journal
*?mode=*
*.bak
*.bak.*
config.json.bak*
*.backfill-snapshot-*
*.log
marketplace/
venv/
__pycache__/
*.pyc
'

if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [DRY-RUN] would write .gitignore with patterns:"
    echo "$GITIGNORE_CONTENT" | sed 's/^/    /'
else
    printf '%s' "$GITIGNORE_CONTENT" > "$GITIGNORE_PATH"
    log "  .gitignore written ($GITIGNORE_PATH)"
fi

# ---------------------------------------------------------------------------
# Step 3: Generate embedding-stripped knowledge.sql
# ---------------------------------------------------------------------------
log ""
log "Step 3: Generating embedding-stripped knowledge.sql"

SQL_PATH="$ENGRAM_HOME/knowledge.sql"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [DRY-RUN] would run: $PY $ENGRAM_BACKUP dump $DB_PATH $SQL_PATH"
else
    "$PY" "$ENGRAM_BACKUP" dump "$DB_PATH" "$SQL_PATH"

    SQL_SIZE=$(du -sh "$SQL_PATH" 2>/dev/null | awk '{print $1}' || echo "unknown")
    log "  knowledge.sql generated: $SQL_SIZE"
fi

# ---------------------------------------------------------------------------
# Step 4: Verify the stripped dump rebuilds losslessly
# ---------------------------------------------------------------------------
log ""
log "Step 4: Verifying lossless rebuild from stripped dump"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [DRY-RUN] would run: $PY $ENGRAM_BACKUP verify $DB_PATH"
else
    "$PY" "$ENGRAM_BACKUP" verify "$DB_PATH"
    log "  PASS: lossless round-trip verified by engram_backup.verify_roundtrip"
fi

# ---------------------------------------------------------------------------
# Step 5: Drop old git history + fresh git init + clean commit
# ---------------------------------------------------------------------------
log ""
log "Step 5: Replacing git history with a clean root commit"

GIT_DIR="$ENGRAM_HOME/.git"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [DRY-RUN] would: rm -rf .git && git init && git add explicit files && verify no *.db staged && git commit"
else
    # Remove existing git history (the bloated one)
    if [[ -d "$GIT_DIR" ]]; then
        log "  Removing old .git history..."
        rm -rf "$GIT_DIR"
    fi

    # Fresh init
    log "  Running git init..."
    git -C "$ENGRAM_HOME" init -q
    git -C "$ENGRAM_HOME" config user.email "engram@local"
    git -C "$ENGRAM_HOME" config user.name "KG Memory"

    # Stage only the tracked files (same set as _commit_snapshot)
    STAGED_FILES=()
    for fname in "graph_snapshot.md" "knowledge.sql" "session_log.md" "config.json" "warm-briefing.md" ".gitignore"; do
        if [[ -f "$ENGRAM_HOME/$fname" ]]; then
            git -C "$ENGRAM_HOME" add -- "$fname"
            STAGED_FILES+=("$fname")
        fi
    done

    # Stage diary contents (excluding .key and __pycache__)
    if [[ -d "$ENGRAM_HOME/diary" ]]; then
        while IFS= read -r -d '' f; do
            basename_f=$(basename "$f")
            if [[ "$basename_f" != ".key" ]] && [[ "$f" != *__pycache__* ]]; then
                rel="diary/$basename_f"
                git -C "$ENGRAM_HOME" add -- "$rel" 2>/dev/null || true
                STAGED_FILES+=("$rel")
            fi
        done < <(find "$ENGRAM_HOME/diary" -maxdepth 1 -type f -print0 2>/dev/null)
    fi

    if [[ ${#STAGED_FILES[@]} -eq 0 ]]; then
        die "No files to commit — something went wrong"
    fi

    log "  Staged: ${STAGED_FILES[*]}"

    # Safety: verify NO *.db or *.log or *.bak files are staged
    STAGED_STATUS=$(git -C "$ENGRAM_HOME" status --porcelain)
    JUNK_STAGED=$(echo "$STAGED_STATUS" | grep -E '\.(db|log|bak|db-wal|db-shm|db-journal)$' || true)
    if [[ -n "$JUNK_STAGED" ]]; then
        die "Binary/log files were staged despite .gitignore — aborting:\n$JUNK_STAGED"
    fi

    # Commit
    git -C "$ENGRAM_HOME" commit -q -m "[fix-git-backup] clean root commit — embedding-stripped SQL dump, binary files gitignored (engram-fix-git-backup.sh $TS)"
    COMMIT_SHA=$(git -C "$ENGRAM_HOME" rev-parse HEAD)
    log "  Clean root commit: $COMMIT_SHA"
fi

# ---------------------------------------------------------------------------
# Step 6: Print force-push command (operator runs manually)
# ---------------------------------------------------------------------------
log ""
log "Step 6: Force-push command (DO NOT AUTO-RUN — operator must execute manually)"
log ""

REMOTE_URL=$(git -C "$ENGRAM_HOME" remote get-url origin 2>/dev/null || echo "<remote-url>")
CURRENT_BRANCH=$(git -C "$ENGRAM_HOME" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")

echo "============================================================"
echo "  To update the remote, run:"
echo ""
echo "    git -C $ENGRAM_HOME push --force origin $CURRENT_BRANCH"
echo ""
echo "  Remote: $REMOTE_URL"
echo "  Branch: $CURRENT_BRANCH"
echo "============================================================"
echo ""
log "Safety snapshot preserved at: $SAFETY_SNAPSHOT"
log ""
log "Next step: run 'python tools/engram-regenerate-embeddings.py' to restore semantic search."
log "Done."
