"""Tests for the engram-nap SKILL.md backup-health check (engram-alpha#2680).

The skill's Step 3 has the agent surface a degrading backup after calling
engram_checkpoint() (formerly engram_nap()) instead of burying it in the
return dict. Since #4012 the verdict is computed once, by the server's
``_backup_ok`` helper, and the skill reads it from compact/last-checkpoint.json.
This file pins down the CORRECT predicate by calling that helper
(_backup_health_ok adds only the reason text) and verifies it against REAL
captured return shapes from the two underlying functions it reads
(engram_core._write_binary_backup, engram_snapshot_worker.write_durable_
snapshot) rather than hand-typed fixtures, plus a synthetic failure case
for each.

## The bug (#2680), and why it existed

The pre-fix snippet was:

    db_backup = result.get("db_backup", {})
    if db_backup.get("status") not in ("ok", "skipped"):
        print(f"... FAILED: {db_backup.get('error', db_backup)}")

Two independent defects, both traced to source rather than assumed:

1. `db_backup` is nested under `result["version_control"]`
   (engram_lifecycle.py's `_checkpoint_internal`), not top-level -- the
   naive `.get("db_backup", {})` always returned `{}`.
2. Neither `_write_binary_backup` nor `write_durable_snapshot` EVER
   returns a `"status"` key, in any of their documented return shapes
   (read directly from each function's own docstring, quoted in the
   fixtures below) -- so even reaching the real dict, `.get("status")`
   is always `None`, permanently outside `("ok", "skipped")`.

Both defects independently make the check report FAILED on every input,
healthy or not -- a check with no passing state is not a check.
"""

from __future__ import annotations

import sqlite3

import pytest

import engram_core as core
import engram_snapshot_worker as worker
from engram_lifecycle import _backup_ok


def _backup_health_ok(version_control: dict) -> tuple[bool, str | None]:
    """The CORRECTED predicate. The verdict is the server's own ``_backup_ok``
    (engram_lifecycle.py, #4012), the one definition of the rule that the
    engram-nap skill's Step 3 reads from compact/last-checkpoint.json; this
    wrapper only adds the reason text. Returns (healthy, reason) -- reason is
    None when healthy, else every non-None error string found, joined with
    "; " (a same-nap dual failure surfaces both, not just the first).

    Per each function's own return contract: the ONLY unhealthy shape is
    an "error" key. A fresh write ({"path","bytes",...}) and an idempotent
    skip ({"skipped": ..., "path"}) are BOTH healthy -- there is no
    "status" field in any real return to check.
    """
    vc = version_control or {}
    db_backup = vc.get("db_backup") or {}
    binary_backup = vc.get("binary_backup") or {}
    if _backup_ok(vc):
        return True, None
    reasons = [e for e in (db_backup.get("error"), binary_backup.get("error")) if e]
    return False, "; ".join(reasons)


def _buggy_pre_fix_check(result: dict) -> bool:
    """The ORIGINAL (broken) predicate, reproduced exactly as it read
    before this fix -- used only as a regression demonstration below, never
    as something to keep working. Returns True if it would report FAILED."""
    db_backup = result.get("db_backup", {})
    return db_backup.get("status") not in ("ok", "skipped")


# ---------------------------------------------------------------------------
# Real captured returns from _write_binary_backup (db_backup's real source)
# ---------------------------------------------------------------------------


def test_real_fresh_write_is_healthy(tmp_path):
    """A genuine, real _write_binary_backup() call on a fresh backup_dir --
    the {"path","bytes","pruned"} success shape."""
    src_db = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(src_db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    real_db_backup = core._write_binary_backup(src_db, tmp_path / "db-backup")
    assert "path" in real_db_backup and "error" not in real_db_backup  # sanity: fresh-write shape

    healthy, reason = _backup_health_ok({"db_backup": real_db_backup, "binary_backup": {}})
    assert healthy, f"a real fresh backup write must read healthy, got reason={reason!r}"


def test_real_same_day_skip_is_healthy(tmp_path):
    """A genuine, real _write_binary_backup() SECOND call on the same day --
    the {"skipped": ..., "path"} idempotent-skip shape. This is the exact
    shape #2680's own bug report captured live and is the case the pre-fix
    check most commonly misreported (a healthy, working seat, degraded to
    'FAILED' every single day after the first nap)."""
    src_db = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(src_db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    backup_dir = tmp_path / "db-backup"

    core._write_binary_backup(src_db, backup_dir)  # first call: fresh write
    real_db_backup = core._write_binary_backup(src_db, backup_dir)  # second: same-day skip
    assert real_db_backup.get("skipped") == "same-day copy exists"  # sanity: skip shape

    healthy, reason = _backup_health_ok({"db_backup": real_db_backup, "binary_backup": {}})
    assert healthy, f"a real same-day skip must read healthy, got reason={reason!r}"


def test_real_write_failure_is_unhealthy(tmp_path):
    """A genuine _write_binary_backup() failure -- {"error": str} -- forced
    by pointing "src_db_path" at a file that exists but isn't a valid
    SQLite database (sqlite3.connect() lazily creates/opens rather than
    validating on connect, so a merely-missing path doesn't fail here --
    confirmed empirically; conn.backup() against a corrupt/non-DB file
    does raise, inside _write_binary_backup's own try/except)."""
    corrupt_src = tmp_path / "not-a-database.db"
    corrupt_src.write_bytes(b"this is not a sqlite file, just noise bytes")
    real_db_backup = core._write_binary_backup(str(corrupt_src), tmp_path / "db-backup")
    assert "error" in real_db_backup  # sanity: this really is the failure shape

    healthy, reason = _backup_health_ok({"db_backup": real_db_backup, "binary_backup": {}})
    assert not healthy
    assert reason == real_db_backup["error"]


# ---------------------------------------------------------------------------
# Real captured returns from write_durable_snapshot (binary_backup's real source)
# ---------------------------------------------------------------------------


def test_real_durable_snapshot_success_is_healthy(tmp_path, monkeypatch):
    """A genuine, real write_durable_snapshot() call -- the
    {"path","bytes","fsynced": True} success shape."""
    monkeypatch.setattr(worker, "_data_dir", lambda: tmp_path)
    src_db = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(src_db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    conn.commit()

    real_binary_backup = worker.write_durable_snapshot(conn, turn=1, seq=1)
    conn.close()
    assert real_binary_backup.get("fsynced") is True  # sanity: real success shape
    assert "error" not in real_binary_backup

    healthy, reason = _backup_health_ok({"db_backup": {}, "binary_backup": real_binary_backup})
    assert healthy, f"a real durable-snapshot success must read healthy, got reason={reason!r}"


def test_synthetic_durable_snapshot_failure_is_unhealthy():
    """The {"error": str} failure shape write_durable_snapshot documents
    for any capture exception -- synthetic here since forcing a REAL
    fsync/rename failure portably in a test sandbox isn't practical, but
    the shape itself is copied verbatim from the function's own docstring
    (see the module docstring above)."""
    synthetic_failure = {"error": "disk full: [Errno 28] No space left on device"}
    healthy, reason = _backup_health_ok({"db_backup": {}, "binary_backup": synthetic_failure})
    assert not healthy
    assert reason == synthetic_failure["error"]


# ---------------------------------------------------------------------------
# The real bug, reproduced -- and the required control (a check that has
# never been seen to correctly report BOTH healthy and unhealthy is not
# shown to be discriminating).
# ---------------------------------------------------------------------------


def test_regression_old_predicate_misreported_a_healthy_skip_as_failed(tmp_path):
    """THE bug this issue reports, reproduced against a real capture: the
    pre-fix predicate reads a genuinely healthy same-day-skip as FAILED,
    on both defects at once (top-level lookup misses the version_control
    nesting; even if it didn't, 'status' is never a real key)."""
    src_db = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(src_db)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    backup_dir = tmp_path / "db-backup"
    core._write_binary_backup(src_db, backup_dir)
    real_db_backup = core._write_binary_backup(src_db, backup_dir)  # same-day skip

    # Shape exactly as engram_checkpoint() (formerly engram_nap()) actually returns it: nested under version_control.
    real_result = {"version_control": {"db_backup": real_db_backup, "binary_backup": {}}}

    assert _buggy_pre_fix_check(real_result) is True, (
        "control failed: the pre-fix predicate must misreport this real, "
        "healthy skip as FAILED -- if it doesn't, this is no longer "
        "demonstrating the reported bug"
    )

    # The fix: read the same real capture through the corrected predicate.
    healthy, reason = _backup_health_ok(real_result["version_control"])
    assert healthy, f"the corrected predicate must report this healthy, got reason={reason!r}"


def test_dual_failure_surfaces_both_reasons_not_just_one():
    """Fairy-round-1 suggestion (PR #2729): a same-nap failure of BOTH
    db_backup and binary_backup must not silently drop one error string --
    the check still correctly reports FAILED either way, but the pre-fold
    predicate (`db_backup.get("error") or binary_backup.get("error")`) only
    ever surfaced the first, which the operator diagnosing a real dual
    failure would want to see."""
    healthy, reason = _backup_health_ok(
        {"db_backup": {"error": "disk full"}, "binary_backup": {"error": "fsync failed"}}
    )
    assert not healthy
    assert "disk full" in reason
    assert "fsync failed" in reason


def test_mutation_control_both_defects_independently_break_the_fix():
    """Required control (#2680's own 'Suggested acceptance test': a test
    that only exercises the failure path would pass on today's [buggy]
    code and is the specific thing to avoid). Confirms _backup_health_ok
    itself is discriminating by breaking it two ways -- un-nesting
    version_control, and re-checking a nonexistent 'status' key -- and
    showing each mutation flips a genuinely healthy real capture to
    unhealthy."""
    real_db_backup = {"skipped": "same-day copy exists", "path": "/tmp/x.db"}
    healthy_vc = {"db_backup": real_db_backup, "binary_backup": {"path": "/tmp/y", "fsynced": True}}
    healthy, _ = _backup_health_ok(healthy_vc)
    assert healthy  # sanity: the real fix reads this correctly

    # Mutation 1: defect 1 alone (top-level lookup, no nesting fix) --
    # _backup_health_ok always takes version_control pre-unwrapped, so this
    # mutation is exercised by the OLD predicate above, not this function;
    # already covered by test_regression_old_predicate_misreported_a_healthy_skip_as_failed.

    # Mutation 2: defect 2 alone -- revert to checking a "status" key that
    # never exists, keeping the (now-correct) nesting.
    def _mutant_status_only(vc):
        db = (vc or {}).get("db_backup") or {}
        bin_ = (vc or {}).get("binary_backup") or {}
        return db.get("status") in ("ok", "skipped") and bin_.get("status") in ("ok", "skipped")

    assert _mutant_status_only(healthy_vc) is False, (
        "mutation control failed: reverting to a 'status' check must "
        "misreport this same real, healthy capture as unhealthy -- if it "
        "doesn't, 'status' being absent isn't actually what the fix is "
        "protecting against"
    )
