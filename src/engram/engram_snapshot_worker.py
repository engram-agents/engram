"""Async snapshot worker (#1673) — moves the iterdump→knowledge.sql text dump and
the git commit/push off the turn-advance path.

Design (approved by Kepler, issue #1673):

  SYNC (in _commit_snapshot, before the tool returns — the durability boundary):
    a per-turn fsync'd binary snapshot of knowledge.db is captured under the #786
    WAL read-mark and its job is enqueued+fsync'd to a durable queue. That fsync'd
    binary is the durability backstop the #1684 WAL+synchronous=NORMAL window leans
    on — strictly stronger than the previous un-fsync'd text+git-commit.

  ASYNC (this module's single serialized worker thread, FIFO):
    consumes the queue in order; for each job it opens the *immutable* captured
    snapshot-<turn>-<seq>.db (never the live WAL — #786/R5 race window does not
    exist for the worker), regenerates knowledge.sql (dump_stripped) AND
    graph_snapshot.md from that captured image, git-commits the explicit file set,
    pushes best-effort, then marks the job done and unlinks the consumed snapshot.

Crash safety:
  * Each captured snapshot is fsync'd and immutable; the queue append is fsync'd
    before advance_turn returns. A crash between enqueue and commit loses nothing
    durable — the binary is on disk and the commit is replayed on restart.
  * Job key is (turn, seq): multiple naps within one turn must not collide on a
    turn-only filename (Kepler rider b).
  * Restart replay clears a stale .git/index.lock left by a process SIGKILL'd
    mid-commit before replaying, loudly — otherwise one such kill deadlocks the
    whole async lane and R3's worker_alive=false would not reveal it as
    self-inflicted (Kepler rider a).

R-map (issue #1673): R1/R2 sync fsync'd per-turn binary (in _commit_snapshot);
R3 loud snapshot_lag surfaced at the next turn (compute_snapshot_lag); R4 retention
≥ async lag is structural (a snapshot file survives exactly until its commit lands);
R5 #786 invariants — read-mark wraps the sync .backup(), worker touches only the
captured file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# ── Durable queue layout ───────────────────────────────────────────────────
# Everything lives under DATA_DIR/db-backup/.pending/ (db-backup is gitignored,
# so captured snapshots + the queue are never committed).
#   queue.jsonl            — append-only durable log: {"type":"job"|"done", turn, seq, ...}
#   snapshot-<turn>-<seq>.db — immutable captured binary, unlinked once committed
_PENDING_SUBPATH = ("db-backup", ".pending")

# Explicit commit set (never `git add .`). knowledge.db (binary) is gitignored;
# knowledge.sql is the linear-diff-friendly text equivalent. This set moves here
# from engram_core._commit_snapshot when the sync git step is gutted (increment 2).
_COMMIT_FILES = (
    "graph_snapshot.md",
    "knowledge.sql",
    "session_log.md",
    "config.json",
    "warm-briefing.md",
)

# Compact the append-only queue when it grows past this many lines (hygiene —
# append-only done-markers grow unbounded otherwise; Kepler note).
_QUEUE_COMPACT_THRESHOLD = 256

# A snapshot lag of >= this many un-committed turns reads as a warning (R3).
_LAG_WARN_TURNS = 2

# Age (seconds) past which a .git/index.lock whose owner is gone is deemed stale.
_INDEX_LOCK_STALE_SECS = 120


# ── Module state (per process; the worker is a singleton) ──────────────────
_lock = threading.Lock()          # serializes ALL queue-file writes + seq handout
_work_event = threading.Event()   # set when a new job is enqueued
_worker_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_seq_counter = 0                  # monotonic per-process; seeded from disk on start
_started = False


def _log(msg: str) -> None:
    """Loud, unconditional stderr log — a silent durability failure (a backup/commit
    that fails with no one noticing) is exactly the class this module exists to
    eliminate, so every failure path here is noisy by design."""
    print(f"[engram snapshot-worker] {msg}", file=sys.stderr, flush=True)


# ── Path + fsync helpers ───────────────────────────────────────────────────
def _data_dir() -> Path:
    import engram_core  # lazy: avoid circular import at module load
    return engram_core.DATA_DIR


def _pending_dir() -> Path:
    d = _data_dir().joinpath(*_PENDING_SUBPATH)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _queue_path() -> Path:
    return _pending_dir() / "queue.jsonl"


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    # Directory fsync makes a rename/create durable. Best-effort on platforms
    # that refuse O_RDONLY on a dir (none of our targets), never fatal.
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        _log(f"dir fsync skipped for {path}: {e}")


def _snapshot_name(turn: int, seq: int) -> str:
    return f"snapshot-{turn}-{seq}.db"


# ── Durable queue read/append/compact ──────────────────────────────────────
def _read_queue_lines() -> list[dict]:
    qp = _queue_path()
    if not qp.exists():
        return []
    out: list[dict] = []
    for line in qp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            _log(f"skipping malformed queue line: {line[:120]}")
    return out


def _pending_jobs(entries: Optional[list[dict]] = None) -> list[dict]:
    """Return job entries with no matching done marker, in FIFO (append) order."""
    if entries is None:
        entries = _read_queue_lines()
    done: set[tuple] = set()
    jobs: list[dict] = []
    for e in entries:
        key = (e.get("turn"), e.get("seq"))
        if e.get("type") == "done":
            done.add(key)
        elif e.get("type") == "job":
            jobs.append(e)
    return [j for j in jobs if (j.get("turn"), j.get("seq")) not in done]


def _append_entry(entry: dict) -> None:
    """Append one JSON line to the durable queue and fsync (file + dir)."""
    qp = _queue_path()
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(qp, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    _fsync_dir(qp.parent)


def _compact_queue_locked() -> None:
    """Rewrite queue.jsonl keeping only still-pending jobs (drop job+done pairs).
    Atomic: write tmp, fsync, rename, fsync dir. Caller must hold _lock."""
    qp = _queue_path()
    entries = _read_queue_lines()
    pending = _pending_jobs(entries)
    if len(entries) <= len(pending):
        return  # nothing to reclaim
    tmp = qp.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for j in pending:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(qp))
    _fsync_dir(qp.parent)
    _log(f"compacted queue: {len(entries)} → {len(pending)} lines")


# ── Sync-side helpers (called from _commit_snapshot under the read-mark) ────
def next_seq() -> int:
    """Hand out a monotonic per-process sequence number (guards nap-within-turn
    collisions on the snapshot filename)."""
    global _seq_counter
    with _lock:
        _seq_counter += 1
        return _seq_counter


def write_durable_snapshot(conn: sqlite3.Connection, turn: int, seq: int) -> dict:
    """Capture a fsync'd, immutable per-turn binary snapshot of the live DB (R1/R2).

    MUST be called while the caller holds the #786 WAL read-mark on ``conn`` so the
    temporary destination connection is never seen as the last WAL reader (R5).
    write-to-tmp → fsync file → atomic rename → fsync dir.

    Returns {"path", "bytes", "fsynced": True} on success or {"error": str}.
    """
    pending = _pending_dir()
    final = pending / _snapshot_name(turn, seq)
    tmp = pending / (_snapshot_name(turn, seq) + ".tmp")
    try:
        dest = sqlite3.connect(str(tmp))
        try:
            conn.backup(dest)  # consistent hot copy of the live main+WAL view
        finally:
            dest.close()
        _fsync_file(tmp)
        os.replace(str(tmp), str(final))
        _fsync_dir(pending)
        return {"path": str(final), "bytes": final.stat().st_size, "fsynced": True}
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        _log(f"CRITICAL: durable snapshot capture failed (turn={turn},seq={seq}): {e}")
        return {"error": str(e)}


def enqueue_job(turn: int, seq: int, message: str, mode: str) -> dict:
    """Durably enqueue the async commit job for a captured snapshot and wake the
    worker. fsync'd before returning — advance_turn returns only after this."""
    entry = {
        "type": "job",
        "turn": turn,
        "seq": seq,
        "snapshot": _snapshot_name(turn, seq),
        "ts": time.time(),
        "message": message,
        "mode": mode,
    }
    with _lock:
        _append_entry(entry)
    _work_event.set()
    return {"enqueued": [turn, seq]}


def compute_snapshot_lag() -> dict:
    """R3 liveness signal, surfaced in the advance_turn/nap tool result.

    behind_turns counts distinct un-committed turns still pending. worker_alive is
    False if the singleton worker thread has died — a git history that silently
    stops advancing while the graph advances is a false-in-the-graph failure."""
    pending = _pending_jobs()
    turns = sorted({j.get("turn") for j in pending if j.get("turn") is not None})
    alive = bool(_worker_thread and _worker_thread.is_alive())
    lag = {
        "behind_turns": len(turns),
        "oldest_pending_turn": turns[0] if turns else None,
        "pending_jobs": len(pending),
        "worker_alive": alive,
    }
    if len(turns) >= _LAG_WARN_TURNS or (not alive and pending):
        lag["warning"] = (
            f"snapshot worker behind by {len(turns)} turn(s)"
            + ("" if alive else "; worker thread is NOT alive")
        )
    return lag


# ── Git commit of one snapshot's derived files (moved async from _commit_snapshot) ──
def _commit_snapshot_files(mode: str, message: str) -> dict:
    """Stage the explicit file set, commit, and push (best-effort). Mirrors the
    steps previously inline in engram_core._commit_snapshot; reuses engram_core._git.

    knowledge.sql + graph_snapshot.md are regenerated from the captured snapshot by
    the caller before this runs."""
    import engram_core  # lazy
    git = engram_core._git
    data_dir = engram_core.DATA_DIR

    files = [f for f in _COMMIT_FILES if (data_dir / f).exists()]
    diary = data_dir / "diary"
    if diary.is_dir():
        for f in diary.iterdir():
            if f.is_file() and f.name != ".key" and "__pycache__" not in str(f):
                files.append(f"diary/{f.name}")
    if not files:
        return {"git_committed": False, "reason": "no tracked files exist"}

    # Filter gitignored paths (#731). rc 0 → some ignored (listed on stdout);
    # rc 1 → none; rc >1 → git error, keep all (best-effort).
    ci = git("check-ignore", "--", *files)
    if ci.returncode == 0:
        ignored = {p.strip() for p in ci.stdout.splitlines() if p.strip()}
        files = [p for p in files if p not in ignored]
    if not files:
        return {"git_committed": False, "reason": "all stageable files are gitignored"}

    add = git("add", "--", *files)
    if add.returncode != 0:
        return {"git_committed": False, "reason": f"git add failed: {add.stderr.strip()}"}

    status = git("status", "--porcelain")
    if status.returncode == 0 and not status.stdout.strip():
        sha = git("rev-parse", "HEAD")
        return {
            "git_committed": False,
            "reason": "no changes since last commit",
            "head_sha": sha.stdout.strip() if sha.returncode == 0 else None,
        }

    commit_msg = f"[{mode}] {message}".strip()
    if len(commit_msg) > 500:
        commit_msg = commit_msg[:497] + "..."
    commit = git("commit", "-m", commit_msg)
    if commit.returncode != 0:
        return {"git_committed": False, "reason": f"git commit failed: {commit.stderr.strip()}"}

    sha = git("rev-parse", "HEAD")
    result = {
        "git_committed": True,
        "commit_sha": sha.stdout.strip() if sha.returncode == 0 else "unknown",
        "files_committed": files,
    }
    push = git("push", "origin", "HEAD")
    result["remote_push"] = "success" if push.returncode == 0 else f"failed: {push.stderr.strip()[:200]}"
    return result


def _regenerate_derived_files(snapshot_path: Path) -> None:
    """Regenerate knowledge.sql AND graph_snapshot.md from the *captured* immutable
    snapshot (not the live DB).

    Design nuance beyond the issue spec (flagged for review): graph_snapshot.md must
    be regenerated from the captured image too, not left as the live sync-written
    copy — otherwise the next turn's sync step overwrites it before this job commits,
    the same producer/consumer race Kepler flagged for the .db. Both derived text
    files are thus consistent with the snapshot the commit represents. The identity
    files (session_log/config/warm-briefing/diary) are committed at their live state
    — they are append/identity artifacts and a small turn-tag skew is acceptable."""
    import engram_core  # lazy
    # knowledge.sql from the captured binary
    import engram_backup
    engram_backup.dump_stripped(str(snapshot_path), str(engram_core.DATA_DIR / "knowledge.sql"))
    # graph_snapshot.md from the captured binary (read-only)
    # immutable=1: the captured snapshot never changes, so tell SQLite to skip all
    # locking and WAL/-shm handling — otherwise a WAL-header'd snapshot leaves -wal/
    # -shm sidecars in .pending/ that outlive the unlinked snapshot (reviewer catch).
    conn = sqlite3.connect(f"file:{snapshot_path}?mode=ro&immutable=1", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        md = engram_core._generate_snapshot(conn)
    finally:
        conn.close()
    (engram_core.DATA_DIR / "graph_snapshot.md").write_text(md, encoding="utf-8")


def _process_job(job: dict) -> bool:
    """Process one pending job: regenerate derived files from its captured snapshot,
    commit, mark done, unlink the snapshot. Returns True on success (job done),
    False on failure (job left pending for replay). Never raises."""
    turn, seq = job.get("turn"), job.get("seq")
    snap = _pending_dir() / job.get("snapshot", _snapshot_name(turn, seq))
    try:
        if not snap.exists():
            # The captured binary is gone but the job wasn't marked done. This is a
            # corrupted state (enqueue fsync'd it) — record loudly and retire the job
            # so the lane isn't wedged forever; the graph is unharmed (live DB intact).
            _log(f"CRITICAL: snapshot missing for job turn={turn},seq={seq} ({snap.name}); retiring job")
            _mark_done(turn, seq)
            return False
        _regenerate_derived_files(snap)
        commit = _commit_snapshot_files(job.get("mode", "nap"), job.get("message", ""))
        if not commit.get("git_committed") and commit.get("reason", "").startswith("git commit failed"):
            _log(f"commit FAILED for turn={turn},seq={seq}: {commit['reason']} — leaving job pending")
            return False
        # git_committed True, or a benign no-op ("no changes"/"gitignored"/git unavailable):
        # the snapshot has been processed as far as it can be — mark done + reclaim.
        _mark_done(turn, seq)
        # Reclaim the snapshot AND any -wal/-shm sidecars (dump_stripped opens its own
        # connection to the snapshot; a WAL-header'd image can spawn sidecars).
        for p in (snap, snap.with_name(snap.name + "-wal"), snap.with_name(snap.name + "-shm")):
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                _log(f"could not unlink {p.name}: {e}")
        return True
    except Exception as e:
        _log(f"job turn={turn},seq={seq} raised, leaving pending: {e}")
        return False


def _mark_done(turn: int, seq: int) -> None:
    with _lock:
        _append_entry({"type": "done", "turn": turn, "seq": seq, "ts": time.time()})


# ── Stale index-lock clearing (Kepler rider a) ─────────────────────────────
def _clear_stale_index_lock() -> None:
    """Remove a .git/index.lock abandoned by a process killed mid-commit, before
    replay — else one such kill wedges the whole async lane (rider a).

    Detection is AGE-BASED, not pid-based. git's index.lock carries no owner pid:
    git creates it via O_EXCL and, while writing a new index, fills it with the new
    index *bytes* (never a pid), leaving it empty when idle-locked (verified against
    git 2.50.1). So liveness cannot be read from the file. Instead: a healthy local
    index op (add/commit) holds index.lock for milliseconds, so a lock older than
    _INDEX_LOCK_STALE_SECS is an abandoned orphan. The age gate is also the guard
    that a (pathological, unsupported) concurrently-live git op is never ripped out
    from under — ENGRAM is single-server-per-install and this runs at
    startup/first-worker-start, before this process performs any git op, so a present
    lock is from the prior (now-exited) process. A fresh lock is left in place; the
    resulting commit stall surfaces via snapshot_lag (R3) and self-heals on the next
    start or once it ages out. (git's own O_EXCL lock prevents actual corruption if
    two writers ever do race — one fails cleanly rather than corrupting the index.)
    """
    lock = _data_dir() / ".git" / "index.lock"
    if not lock.exists():
        return
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        age = None  # can't stat → treat as orphan (the file exists but is unreadable)
    if age is None or age >= _INDEX_LOCK_STALE_SECS:
        age_str = "unknown" if age is None else f"{age:.0f}s"
        try:
            lock.unlink()
            _log(f"removed abandoned .git/index.lock (age {age_str} ≥ "
                 f"{_INDEX_LOCK_STALE_SECS}s; index.lock carries no owner-pid to check)")
        except OSError as e:
            _log(f"could not remove abandoned index.lock: {e}")
    else:
        _log(f".git/index.lock present but only {age:.0f}s old (< {_INDEX_LOCK_STALE_SECS}s) "
             f"— leaving it in case a git op is genuinely in flight; a resulting commit "
             f"stall surfaces via snapshot_lag (R3)")


# ── Worker thread + lifecycle ──────────────────────────────────────────────
def _reclaim_orphan_snapshots() -> int:
    """Delete .pending/snapshot-*.db (+ sidecars) with NO pending job entry.

    Such a file is an orphan from a crash in the narrow window between
    write_durable_snapshot's fsync'd rename (the file is on disk) and enqueue_job's
    fsync (the job is recorded). Without this, that snapshot is never consumed and
    leaks unboundedly across crashes — breaking R4's "a snapshot survives exactly
    until consumed". A crash in this window loses nothing durable: the turn's data is
    still in the live DB (and the git history simply skips that un-enqueued snapshot),
    so discarding the orphan is safe. Called once at startup, after the seq counter is
    seeded from these files. Returns the count reclaimed. (Colleague-review catch.)
    """
    pending = _pending_dir()
    keep = {(j.get("turn"), j.get("seq")) for j in _pending_jobs()}
    try:
        snaps = list(pending.glob("snapshot-*.db"))
    except OSError:
        return 0
    reclaimed = 0
    for snap in snaps:
        parts = snap.stem.split("-")  # snapshot-<turn>-<seq>
        if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
            continue
        if (int(parts[1]), int(parts[2])) in keep:
            continue  # referenced by a pending job → replay will consume it
        for p in (snap, snap.with_name(snap.name + "-wal"), snap.with_name(snap.name + "-shm")):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        reclaimed += 1
    if reclaimed:
        _log(f"reclaimed {reclaimed} orphaned snapshot file(s) with no job entry "
             f"(crash between capture and enqueue)")
    return reclaimed


def _seed_seq_counter() -> None:
    """Seed the per-process seq counter above the max seq seen in pending jobs and
    leftover snapshot files, so a restart can't reuse a filename still on disk."""
    global _seq_counter
    max_seq = 0
    for j in _pending_jobs():
        if isinstance(j.get("seq"), int):
            max_seq = max(max_seq, j["seq"])
    try:
        for p in _pending_dir().glob("snapshot-*-*.db"):
            parts = p.stem.split("-")  # snapshot-<turn>-<seq>
            if len(parts) == 3 and parts[2].isdigit():
                max_seq = max(max_seq, int(parts[2]))
    except OSError:
        pass
    _seq_counter = max_seq


def _drain_pending() -> int:
    """Process all currently-pending jobs FIFO. Returns count successfully done.
    Stops early on a job that stays pending (failure) to avoid a tight retry loop."""
    done = 0
    for job in _pending_jobs():
        if _stop.is_set():
            break
        if _process_job(job):
            done += 1
        else:
            break  # leave the rest for the next wake / restart; don't hot-loop
    return done


def _worker_loop() -> None:
    while not _stop.is_set():
        _work_event.wait(timeout=30.0)
        _work_event.clear()
        if _stop.is_set():
            break
        try:
            _drain_pending()
            with _lock:
                if len(_read_queue_lines()) > _QUEUE_COMPACT_THRESHOLD:
                    _compact_queue_locked()
        except Exception as e:  # a loop that dies silently is the failure we guard against
            _log(f"worker loop iteration error: {e}")


def start_worker() -> None:
    """Idempotent: seed seq, clear a stale index-lock, compact + replay pending
    jobs, then start the singleton worker thread. Call once at server startup."""
    global _worker_thread, _started
    with _lock:
        if _started and _worker_thread and _worker_thread.is_alive():
            return
        _started = True
    _clear_stale_index_lock()
    _seed_seq_counter()
    _reclaim_orphan_snapshots()  # drop snapshots from a crash between capture and enqueue
    with _lock:
        _compact_queue_locked()
    replayed = _drain_pending()
    if replayed:
        _log(f"replayed {replayed} pending snapshot job(s) on startup")
    _stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="engram-snapshot-worker", daemon=True
    )
    _worker_thread.start()


def stop_worker(timeout: float = 5.0) -> None:
    """Signal the worker to stop and join (used by tests / clean shutdown)."""
    _stop.set()
    _work_event.set()
    t = _worker_thread
    if t and t.is_alive():
        t.join(timeout=timeout)
