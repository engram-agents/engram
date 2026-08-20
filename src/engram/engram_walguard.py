"""WAL/shm self-guard for ENGRAM — displacement detection and degraded-state marker.

Detects split-brain WAL-index conditions (knowledge.db-shm deleted or replaced
while the server holds it open) and provides marker-file primitives for
coordinating degraded-mode across all DB-touching processes.

Detection is Linux-only in v1: it relies on /proc/self/fd/ symlinks to compare
the inode the live server process holds open against the on-disk file.  On
non-Linux hosts or when /proc is absent, detect_shm_displacement() returns
None (graceful no-op).

Motivating incident (2026-06-03): knowledge.db-shm was deleted under the live
MCP server.  The server stayed coherent for ~6 hours while fresh external
connections saw a stale snapshot (split-brain WAL-index).  At session teardown
the dying server's final checkpoint truncated the main DB 31 pages short of its
header — total corruption, recovered only via the per-checkpoint SQL dump.

That framing is read-side only, and it is incomplete: the same window is also a
**write-durability** hazard.  On 2026-08-07 (alpha#1947) two observations filed
through the degraded server returned full success payloads — status `created`,
node id, evidence id, computed confidence, dedup neighbours — the same as any
other successful write.  Both were silently lost when the server was replaced:
the graph's max observation id rolled back on restart, and the emergency dump
predated the writes, so it did not rescue them either.  A caller filing during
split-brain has no way to tell a durable write from a doomed one — the "6 hours"
figure should be read as write exposure, not merely stale-read exposure.

This module:
  - detect_shm_displacement()  — inode-compare check via /proc/self/fd/
  - write_degraded_marker()    — atomic marker write
  - read_degraded_marker()     — read the marker (returns None when absent)
  - clear_degraded_marker()    — remove the marker
  - recovery_block()           — structured recovery guidance (#1947), also
                                  embedded in write_degraded_marker()'s output
  - recovery_banner_text()     — compact prose form of the same guidance, used
                                  by engram_core's CRITICAL log line and the
                                  SUBSTRATE DEGRADED banner
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Recovery guidance (#1947)
# ---------------------------------------------------------------------------
# Single source of truth for the recovery instruction, shared by:
#   - engram_core._walguard_startup_clear's "[walguard] CRITICAL: ..." log line
#   - engram_core._walguard_degraded_banner's "SUBSTRATE DEGRADED" banner
#   - write_degraded_marker()'s structured "recovery" key
# so the three can never drift out of sync (alpha#1947 slice A/B — the prior
# "request a clean MCP restart" wording named only one of the two long-lived
# holders of knowledge.db; the surface daemon is the other, and recovery
# requires BOTH to release the DB).

_RECOVERY_ISSUE_URL = "https://github.com/engram-agents/engram/issues/786"

_RECOVERY_CONDITION = (
    "every process holding knowledge.db must release it; a displaced "
    "WAL-index cannot be rebuilt while any connection is open"
)

_RECOVERY_STEPS: list[str] = [
    "restart the MCP server (/mcp reconnect)",
    "stop the surface daemon: kill -TERM $(cat ~/.engram/recall-daemon.pid)",
    'restart it: bash "$CLAUDE_PLUGIN_ROOT/hooks/start-engram-daemon.sh"',
    'VERIFY before believing it worked: sqlite3 ~/.engram/knowledge.db '
    '"select count(*) from nodes" must succeed -- a reconnect that reports '
    'success is NOT evidence the index recovered',
]

_RECOVERY_VERIFY = 'sqlite3 <db_path> "select count(*) from nodes"'


def recovery_block() -> dict:
    """Structured recovery guidance (#1947), embedded verbatim in the degraded
    marker's "recovery" key.  Built from the same constants as
    recovery_banner_text() so the marker and the log/banner text can never
    drift out of sync.
    """
    return {
        "condition": _RECOVERY_CONDITION,
        "steps": list(_RECOVERY_STEPS),
        "verify": _RECOVERY_VERIFY,
        "issue": _RECOVERY_ISSUE_URL,
    }


def recovery_banner_text() -> str:
    """Compact prose form of the same recovery guidance as recovery_block(),
    for the CRITICAL stderr log line and the SUBSTRATE DEGRADED banner.

    Names every long-lived holder (MCP server AND the surface daemon) and
    ends with a verify step — the two gaps that cost ~20 extra minutes on
    2026-08-07 (alpha#1947): the operator restarted only the MCP server and
    had no stated way to check whether the remedy actually worked.
    """
    numbered = "; ".join(f"({i}) {step}" for i, step in enumerate(_RECOVERY_STEPS, 1))
    return (
        f"Recovery requires EVERY holder to release the DB, not just the MCP "
        f"server: {numbered}. (#786)"
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_shm_displacement(db_path: str) -> Optional[dict]:
    """Check whether the live process's shm fd has been displaced.

    Compares the inode of the -shm file the current process holds open
    (via /proc/self/fd/) against the inode of the on-disk <db_path>-shm.

    Returns:
        None  — healthy (inodes match) OR no shm fd held by this process
                (connection may not have opened WAL yet) OR /proc not available
                (non-Linux; detection is Linux-only in v1).
        {"reason": "shm_deleted", "fd": <int>}
            — the fd symlink ends with " (deleted)", meaning the on-disk shm
              was removed while the process held it open.
        {"reason": "shm_replaced", "fd": <int>, "fd_inode": <int>, "disk_inode": <int>}
            — the on-disk shm exists but has a different inode than the one
              this process holds open (file was replaced, not deleted).  This
              is the atomic-replacement case — e.g. another process called
              os.replace() on the shm path — not the simple deletion case
              (which produces "shm_deleted" via the "(deleted)" suffix).  On
              Linux, unlinking always produces the "(deleted)" suffix, so
              "shm_replaced" requires a live replacement file to exist at the
              shm path with a mismatched inode.
    """
    try:
        shm_path = db_path + "-shm"
        proc_fd = "/proc/self/fd"
        if not os.path.isdir(proc_fd):
            return None  # non-Linux or /proc not mounted; detection unavailable

        shm_fd: Optional[int] = None
        shm_target: Optional[str] = None

        # os.listdir intentional: closes dir fd before return, preventing a
        # transient self-referencing fd that os.scandir (which keeps the dir fd
        # open across iterations) would create in /proc/self/fd/.
        for entry in os.listdir(proc_fd):
            try:
                fd_num = int(entry)
            except ValueError:
                continue
            try:
                target = os.readlink(os.path.join(proc_fd, entry))
            except OSError:
                continue
            # Match the exact shm path (ignoring any " (deleted)" suffix for
            # comparison purposes — the suffix itself is a displacement signal)
            # rstrip() is cosmetic — the ' (deleted)' suffix is handled by
            # endswith() below, not stripped here.  We strip any trailing
            # whitespace so the equality/startswith comparisons don't miss a
            # path that happens to have a trailing space on an exotic fs.
            base_target = target.rstrip()
            if base_target == shm_path or base_target.startswith(shm_path + " "):
                shm_fd = fd_num
                shm_target = target
                break

        if shm_fd is None:
            # No fd held for this shm — cannot judge; connection may not have
            # opened WAL yet (e.g. the DB was opened in WAL mode but no
            # read/write has forced shm creation yet).
            return None

        # fd target ends with " (deleted)" → on-disk shm is gone
        if shm_target is not None and shm_target.endswith(" (deleted)"):
            return {"reason": "shm_deleted", "fd": shm_fd}

        # Compare inodes
        fd_link = os.path.join(proc_fd, str(shm_fd))
        try:
            fd_stat = os.stat(fd_link)
        except OSError:
            return None  # can't stat the fd link; give up gracefully

        try:
            disk_stat = os.stat(shm_path)
        except FileNotFoundError:
            return {"reason": "shm_deleted", "fd": shm_fd}
        except OSError:
            return None  # unexpected; fail open (don't raise)

        if fd_stat.st_ino != disk_stat.st_ino:
            return {
                "reason": "shm_replaced",
                "fd": shm_fd,
                "fd_inode": fd_stat.st_ino,
                "disk_inode": disk_stat.st_ino,
            }

        return None  # healthy

    except Exception:
        # Any unexpected error → graceful no-op (the guard must never crash the
        # caller)
        return None


# ---------------------------------------------------------------------------
# Degraded-state marker
# ---------------------------------------------------------------------------

_MARKER_FILENAME = ".substrate-degraded.json"


def _marker_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / _MARKER_FILENAME


def write_degraded_marker(
    data_dir: str | Path,
    detection: dict,
    dump_info: Optional[dict] = None,
    *,
    detected_at: Optional[str] = None,
) -> str:
    """Write (or overwrite) the degraded marker atomically.

    Contents written:
        {
          "reason":              str,          # from detection["reason"]
          "detected_at":         str,          # UTC ISO-8601 (original detection time)
          "pid":                 int,          # os.getpid()
          "dump_committed":      bool,         # True when emergency dump succeeded
          "dump_sha":            str | null,   # git SHA of the emergency dump commit
          "last_emergency_dump": str | null,   # UTC ISO-8601 of most recent dump attempt
          "details":             dict,         # full detection dict + any dump_info
          "recovery":            dict,         # recovery_block() (#1947) — condition,
                                                # steps, verify, issue; same source as
                                                # the CRITICAL log line / degraded banner
        }

    Uses write-tmp + os.replace for atomicity (no partial-read window).

    Args:
        data_dir: Directory where the marker file lives.
        detection: Dict from detect_shm_displacement() with at least "reason".
        dump_info: Optional dict from _commit_snapshot() with "git_committed" /
            "commit_sha" / etc.
        detected_at: When provided, used verbatim as the "detected_at" field
            (preserves the original timestamp on hourly re-dumps).  When None,
            set to the current UTC time.

    Returns the marker path as a string.
    """
    target = _marker_path(data_dir)
    dump_committed = bool(dump_info and dump_info.get("git_committed"))
    dump_sha = (dump_info or {}).get("commit_sha", None)
    now_iso = datetime.now(timezone.utc).isoformat()

    payload = {
        "reason": detection.get("reason", "unknown"),
        "detected_at": detected_at if detected_at is not None else now_iso,
        "pid": os.getpid(),
        "dump_committed": dump_committed,
        "dump_sha": dump_sha,
        # Track when the last dump fired; updated on each dump attempt so the
        # hourly-refresh gate in _run_walguard_check can age-check without
        # touching detected_at (which must remain the original detection time).
        "last_emergency_dump": now_iso if dump_info is not None else None,
        "details": {**detection, **(dump_info or {})},
        "recovery": recovery_block(),
    }

    # Atomic write: write to a sibling tmp file, then os.replace
    fd, tmp_path = tempfile.mkstemp(
        suffix=".tmp",
        dir=str(Path(data_dir)),
        prefix=".substrate-degraded-",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, str(target))
    except Exception:
        # Clean up the tmp file on failure; do not leave a partial marker
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return str(target)


def read_degraded_marker(data_dir: str | Path) -> Optional[dict]:
    """Read the degraded marker if it exists.

    Returns the parsed JSON dict, or None when the marker is absent or
    unreadable (treats unreadable as absent — paranoia: the guard must not
    block recovery paths).
    """
    p = _marker_path(data_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def clear_degraded_marker(data_dir: str | Path) -> bool:
    """Remove the degraded marker.

    Returns True if removed, False if it was already absent or removal failed.
    """
    p = _marker_path(data_dir)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
