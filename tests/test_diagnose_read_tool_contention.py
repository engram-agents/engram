"""Tests for engram_diagnose read_tool_contention tripwire (issue #351).

Coverage:
  1. Section fires when a high-contention hour exists (mean > 5s).
  2. Section omitted entirely when all tool_timing rows are fast (< 1s).
  3. 7-day window is respected: old high-contention rows (30 days ago)
     do not trigger the section.

Import-pattern contract (#948 wave A — R1):
    This file imports engram_stats and engram_core directly and contains no
    ``import server``. The module objects are passed to fresh_module() (R4)
    for per-test reload isolation.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add tests/ dir so conftest.fresh_module is importable both under pytest and
# when the file is run as a standalone script.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core
import engram_stats

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_diagnose_read_tool_contention_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def _make_fresh_mods():
    """Reload all family modules, configure a clean test DB, and return modules.

    Uses conftest.fresh_module(engram_stats) per R4: the module object is
    passed (not a string), so the ``import engram_stats`` at the top of
    this file is the AST-visible dependency edge for run_touched_tests.py.

    Returns (stats, core) — freshly reloaded module objects with
    engram_core pointing to TEST_DIR.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    # Purge server + engram_* from sys.modules and reload engram_stats.
    stats = fresh_module(engram_stats)
    core = sys.modules["engram_core"]
    core._configure_paths(TEST_DIR)
    core._ensure_data_dir()
    # Trigger schema creation so tool_timing (and all other tables) exist
    # before any direct SQLite inserts in test fixtures.
    core._get_db().close()
    return stats, core


def _insert_timing_rows(
    db_path: str,
    tool_name: str,
    duration_ms: int,
    count: int,
    timestamp_iso: str,
) -> None:
    """Insert synthetic tool_timing rows directly into the DB."""
    conn = sqlite3.connect(db_path)
    for _ in range(count):
        conn.execute(
            "INSERT INTO tool_timing (timestamp, tool_name, duration_ms, status, turn) "
            "VALUES (?, ?, ?, 'ok', 0)",
            (timestamp_iso, tool_name, duration_ms),
        )
    conn.commit()
    conn.close()


def _db_path() -> str:
    return os.path.join(TEST_DIR, "knowledge.db")


# ── 1. Tripwire fires for a high-contention hour ─────────────────────────────


def test_contention_section_fires_on_high_mean():
    """read_tool_contention must appear when mean duration > 5000ms."""
    stats, _core = _make_fresh_mods()
    # Use a recent timestamp within the 7-day window.
    recent_hour = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:00:00")

    _insert_timing_rows(
        _db_path(),
        tool_name="engram_inspect",
        duration_ms=6000,
        count=100,
        timestamp_iso=recent_hour,
    )

    r = json.loads(stats._diagnose_impl())

    assert "read_tool_contention" in r, (
        f"read_tool_contention section missing despite high-mean hour. "
        f"Keys present: {list(r.keys())}"
    )
    section = r["read_tool_contention"]
    assert section["worst_hours_shown"] >= 1, (
        f"expected worst_hours_shown >= 1, got {section['worst_hours_shown']}"
    )
    assert section["total_flagged_buckets"] >= 1, (
        f"expected total_flagged_buckets >= 1, got {section['total_flagged_buckets']}"
    )
    assert "worst" in section, f"'worst' list missing from section: {section}"
    assert len(section["worst"]) >= 1, "worst list is empty"

    worst = section["worst"][0]
    assert worst["tool"] == "engram_inspect", (
        f"expected tool=engram_inspect, got {worst['tool']}"
    )
    assert worst["calls"] == 100, f"expected calls=100, got {worst['calls']}"
    assert worst["mean_ms"] >= 6000, (
        f"expected mean_ms >= 6000, got {worst['mean_ms']}"
    )

    assert "see" in section, f"'see' field missing from section: {section}"
    assert "the cornerstone-evolution derivation" in section["see"], (
        f"expected cornerstone-evolution derivation reference in see field: {section['see']}"
    )


def test_contention_section_fires_on_high_max():
    """read_tool_contention must appear when max duration > 15000ms (even if mean is low)."""
    stats, _core = _make_fresh_mods()
    recent_hour = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:00:00")

    # Mean will be low: 99 rows at 100ms + 1 row at 20000ms
    # Max = 20000ms > 15000ms threshold
    db = _db_path()
    _insert_timing_rows(db, "engram_query", 100, 99, recent_hour)
    _insert_timing_rows(db, "engram_query", 20000, 1, recent_hour)

    r = json.loads(stats._diagnose_impl())

    assert "read_tool_contention" in r, (
        "read_tool_contention section missing despite max > 15000ms"
    )
    section = r["read_tool_contention"]
    assert section["worst_hours_shown"] >= 1


# ── 2. Section omitted when all rows are fast ─────────────────────────────────


def test_contention_section_absent_when_clean():
    """read_tool_contention must NOT appear when all durations are well below thresholds."""
    stats, _core = _make_fresh_mods()
    recent_hour = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:00:00")

    # 200 fast rows (500ms each) — mean=500ms, max=500ms — both below thresholds.
    _insert_timing_rows(
        _db_path(),
        tool_name="engram_inspect",
        duration_ms=500,
        count=200,
        timestamp_iso=recent_hour,
    )

    r = json.loads(stats._diagnose_impl())

    assert "read_tool_contention" not in r, (
        f"read_tool_contention section appeared unexpectedly with fast rows. "
        f"Section contents: {r.get('read_tool_contention')}"
    )


def test_contention_section_absent_with_empty_timing():
    """read_tool_contention must NOT appear when tool_timing table is empty."""
    stats, _core = _make_fresh_mods()
    # Fresh DB: no rows at all.
    r = json.loads(stats._diagnose_impl())
    assert "read_tool_contention" not in r, (
        "read_tool_contention appeared with empty tool_timing table"
    )


# ── 3. 7-day window is honoured ───────────────────────────────────────────────


def test_contention_7day_window_excludes_old_rows():
    """High-contention rows older than 7 days must NOT trigger the section."""
    stats, _core = _make_fresh_mods()
    # 30 days ago — outside the 7-day window.
    old_hour = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).strftime("%Y-%m-%dT%H:00:00")

    _insert_timing_rows(
        _db_path(),
        tool_name="engram_inspect",
        duration_ms=12000,
        count=500,
        timestamp_iso=old_hour,
    )

    r = json.loads(stats._diagnose_impl())

    assert "read_tool_contention" not in r, (
        "read_tool_contention fired on 30-day-old rows (window not respected). "
        f"Section: {r.get('read_tool_contention')}"
    )


def test_contention_7day_window_includes_6day_old_rows():
    """High-contention rows from 6 days ago (within window) must trigger the section."""
    stats, _core = _make_fresh_mods()
    # 6 days ago — within the 7-day window.
    recent_enough = (
        datetime.now(timezone.utc) - timedelta(days=6)
    ).strftime("%Y-%m-%dT%H:00:00")

    _insert_timing_rows(
        _db_path(),
        tool_name="engram_list",
        duration_ms=8000,
        count=50,
        timestamp_iso=recent_enough,
    )

    r = json.loads(stats._diagnose_impl())

    assert "read_tool_contention" in r, (
        "read_tool_contention missing for 6-day-old high-contention rows (should be in window)"
    )


def test_total_flagged_buckets_exceeds_worst_hours_shown_when_many_flagged():
    """When >10 (hour, tool) buckets are flagged, total_flagged_buckets > worst_hours_shown.

    Creates 11 distinct (hour, tool) flagged combinations by spreading rows
    across 11 different hours in the last 6 days, then checks that
    total_flagged_buckets > worst_hours_shown (which is capped at 10).
    """
    stats, _core = _make_fresh_mods()
    db = _db_path()

    # Insert high-contention rows in 11 distinct hours within the 7-day window.
    base = datetime.now(timezone.utc) - timedelta(days=6)
    for i in range(11):
        hour_ts = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00")
        _insert_timing_rows(db, "engram_query", 8000, 5, hour_ts)

    r = json.loads(stats._diagnose_impl())

    assert "read_tool_contention" in r, (
        "read_tool_contention section missing despite 11 flagged hours"
    )
    section = r["read_tool_contention"]
    assert section["total_flagged_buckets"] > section["worst_hours_shown"], (
        f"expected total_flagged_buckets ({section['total_flagged_buckets']}) "
        f"> worst_hours_shown ({section['worst_hours_shown']})"
    )
    assert section["worst_hours_shown"] == 10, (
        f"expected worst_hours_shown == 10 (LIMIT), got {section['worst_hours_shown']}"
    )
    assert section["total_flagged_buckets"] == 11, (
        f"expected total_flagged_buckets == 11, got {section['total_flagged_buckets']}"
    )


if __name__ == "__main__":
    tests = [
        test_contention_section_fires_on_high_mean,
        test_contention_section_fires_on_high_max,
        test_contention_section_absent_when_clean,
        test_contention_section_absent_with_empty_timing,
        test_contention_7day_window_excludes_old_rows,
        test_contention_7day_window_includes_6day_old_rows,
        test_total_flagged_buckets_exceeds_worst_hours_shown_when_many_flagged,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as e:
            import traceback

            print(f"  FAIL: {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed}/{passed + failed} tests passed")
    if failed:
        sys.exit(1)
