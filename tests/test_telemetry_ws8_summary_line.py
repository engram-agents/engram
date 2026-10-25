"""Tests for `engram_telemetry_verdicts.summary_line()` -- WS8 (issue #3137,
epic #2574 phase 2): the ONE summary line rendered onto both
`engram_diagnose`'s output (`engram_stats.py`) and the nightly dream record
(`agents/claude/engram-dream-master.md`).

Contract-first per this repo's CLAUDE.md "Contract-first tests" convention:
every test below existed before `summary_line()` / the `engram_stats.py`
guarded-import wiring were implemented against them.

The whole point of this unit (issue #3137's own framing): "we could not
check" must NEVER render as "everything is healthy" -- a missing/stale/
absent telemetry store must never render as a line of zero counts. C2 below
is that contract, directly.

Naming note (mid-task correction from the spec author, folded in before this
file was ever committed): the 7 named counts use their STORED verdict-column
values character-for-character -- no display-only renaming anywhere,
including `UNWIRED-SILENT` (issue #3137's own amendment; the issue's earlier
"NOT WIRED" example line was the issue author's own typo/slip, not a design
decision).
"""

from __future__ import annotations

import builtins
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
from conftest import fresh_module  # noqa: E402 -- must follow sys.path setup

import engram_stats  # noqa: E402
import engram_telemetry_store as ts  # noqa: E402
import engram_telemetry_verdicts as v  # noqa: E402

WINDOW = "2026-09-25"
OTHER_WINDOW = "2026-09-20"

# Fixed display order -- issue #3137's own line, verdict-column values
# verbatim (no renaming; see module docstring above for the mid-task fix).
_NAMED_VERDICTS_IN_ORDER = [
    "DARK",
    "DEGRADED",
    "INIT-FAILED",
    "INCONSISTENT",
    "NOT REACHED",
    "GUARDED OUT",
    "UNWIRED-SILENT",
]


# ---------------------------------------------------------------------------
# Fixture helpers (summary_line -- plain sqlite, no ENGRAM server needed)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_dir():
    d = tempfile.mkdtemp(prefix="engram_test_ws8_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _make_db(tmp_dir_path) -> Path:
    """A real records.db with the real l0_records/ingest_offsets schema
    (via engram_telemetry_store.open_store) PLUS the verdicts table
    (via engram_telemetry_verdicts._ensure_verdicts_table) -- matches what a
    real deployment's records.db looks like once both WS6 (ingest) and WS7
    (compute_verdicts) have run at least once."""
    db_path = Path(tmp_dir_path) / "records.db"
    conn = ts.open_store(db_path)
    v._ensure_verdicts_table(conn)
    conn.close()
    return db_path


def _insert_verdict(db_path: Path, window: str, verdict: str, site: str | None = None) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO verdicts (window, site, verdict, r, d, x, n, i, unparseable, flags_json, computed_at) "
            "VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, '[]', ?)",
            (window, site or f"fixture.site.{verdict}.{id(object())}", verdict, _fresh_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_ingest_offset(db_path: Path, last_ingested_at: str, source_file: str = "fixture.jsonl") -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO ingest_offsets (source_file, byte_offset, last_ingested_at) VALUES (?, 0, ?)",
            (source_file, last_ingested_at),
        )
        conn.commit()
    finally:
        conn.close()


def _fresh_iso(delta: timedelta | None = None) -> str:
    """Same rendering shape as engram_telemetry_store/engram_telemetry_verdicts's
    own `_now_utc_iso` -- UTC, explicit 'Z', millisecond precision."""
    dt = datetime.now(timezone.utc)
    if delta is not None:
        dt = dt - delta
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _assert_no_zero_verdict_counts(line: str) -> None:
    """C2's own contract, written as an actual check (not an eyeball
    comment): a not-aggregated line must not carry any of the 7 named
    verdicts rendered with a trailing zero count (e.g. "DARK 0",
    "NOT REACHED 0") -- that shape is exactly what a missing/stale store
    pretending to be all-zeros would look like."""
    for name in _NAMED_VERDICTS_IN_ORDER:
        assert not re.search(rf"{re.escape(name)} 0(\D|$)", line), (
            f"expected NO zero-count rendering for {name!r} in a "
            f"not-aggregated line, got: {line!r}"
        )


# ---------------------------------------------------------------------------
# C1 -- healthy line, all 7 named counts, fixed order, DELIVERING counted
# in the total but not named.
# ---------------------------------------------------------------------------


def test_c1_healthy_line_all_seven_named_counts_fixed_order(tmp_dir):
    db_path = _make_db(tmp_dir)
    _insert_ingest_offset(db_path, _fresh_iso())

    counts = {
        "DARK": 1,
        "DEGRADED": 1,
        "INIT-FAILED": 1,
        "INCONSISTENT": 1,
        "NOT REACHED": 2,
        "GUARDED OUT": 1,
        "UNWIRED-SILENT": 1,
        "DELIVERING": 3,  # healthy verdict -- counted in total, no named entry
    }
    for verdict, n in counts.items():
        for idx in range(n):
            _insert_verdict(db_path, WINDOW, verdict, site=f"site.{verdict}.{idx}")

    line = v.summary_line(db_path, window=WINDOW)

    total = sum(counts.values())
    expected = (
        f"telemetry {WINDOW}: {total} sites · DARK 1 · DEGRADED 1 · "
        f"INIT-FAILED 1 · INCONSISTENT 1 · NOT REACHED 2 · GUARDED OUT 1 · "
        f"UNWIRED-SILENT 1"
    )
    assert line == expected, line
    assert "DELIVERING" not in line, "DELIVERING must not get a named breakdown entry"


def test_c1_default_window_resolves_to_max_window_present(tmp_dir):
    """window=None must resolve to the MOST RECENT window present in the
    verdicts table (`SELECT MAX(window) FROM verdicts`), not the first one
    inserted -- additional coverage for behavior-spec item 2, beyond the
    four required contract tests."""
    db_path = _make_db(tmp_dir)
    _insert_ingest_offset(db_path, _fresh_iso())
    _insert_verdict(db_path, OTHER_WINDOW, "NOT REACHED")  # older window
    _insert_verdict(db_path, WINDOW, "DELIVERING")  # newer window (lexicographically MAX)

    line = v.summary_line(db_path, window=None)
    assert line.startswith(f"telemetry {WINDOW}:"), line
    assert "1 sites" in line, line


def test_named_verdicts_membership_pinned_to_verdict_conditions():
    """Design-conformance read (PR-3170, Borges): `_SUMMARY_NAMED_VERDICTS`
    is a hand-typed list, disconnected from WS7's own `_VERDICT_CONDITIONS`.
    If WS7 ever gains a new fault verdict without this module being updated,
    `summary_line` would count it into `N sites` and render it nowhere --
    a new fault silently hiding inside a "healthy-looking" total, the exact
    failure this whole unit exists to prevent, arriving by a different
    route. Pin the MEMBERSHIP (not the display order, which is a genuine
    presentation choice) to WS7's table directly, so an unpinned drift goes
    red here instead of silently under-rendering in production."""
    verdict_condition_names = {name for name, _cond in v._VERDICT_CONDITIONS}
    assert set(v._SUMMARY_NAMED_VERDICTS) == verdict_condition_names - {"DELIVERING"}, (
        "_SUMMARY_NAMED_VERDICTS has drifted from _VERDICT_CONDITIONS -- "
        "every non-DELIVERING verdict name must have a named breakdown slot"
    )


def test_unrecognized_verdict_still_renders_instead_of_disappearing(tmp_dir):
    """Borges's cheap fallback, folded in alongside the membership pin
    above: even if the pin test above were somehow bypassed, a verdict name
    outside the named 7 and outside DELIVERING must still show up on the
    line (appended after the fixed 7), not vanish into the total silently."""
    db_path = _make_db(tmp_dir)
    _insert_ingest_offset(db_path, _fresh_iso())
    _insert_verdict(db_path, WINDOW, "NOT REACHED")
    _insert_verdict(db_path, WINDOW, "SOME-FUTURE-VERDICT")  # not in _SUMMARY_NAMED_VERDICTS

    line = v.summary_line(db_path, window=WINDOW)
    assert "2 sites" in line, line
    assert "SOME-FUTURE-VERDICT 1" in line, (
        f"an unrecognized verdict must still be rendered, not silently "
        f"folded into the total: {line!r}"
    )


# ---------------------------------------------------------------------------
# C2 -- the reason this unit exists: "we could not check" must never render
# as "everything is healthy".
# ---------------------------------------------------------------------------


def test_c2a_missing_db_file_reads_not_aggregated_no_zero_counts(tmp_dir):
    missing_path = Path(tmp_dir) / "does-not-exist.db"
    line = v.summary_line(missing_path)
    assert "not aggregated" in line, line
    assert line == "telemetry: not aggregated (records.db missing)", line
    _assert_no_zero_verdict_counts(line)


def test_c2a_variant_verdicts_table_missing_reads_not_aggregated(tmp_dir):
    """File exists (real l0_records/ingest_offsets schema via open_store),
    but `compute_verdicts` has never run at all -- no `verdicts` table.
    Behavior-spec item 1's own second clause; additional coverage beyond
    the four required contract tests."""
    db_path = Path(tmp_dir) / "records.db"
    conn = ts.open_store(db_path)  # creates l0_records/ingest_offsets, NOT verdicts
    conn.close()

    line = v.summary_line(db_path)
    assert "verdicts table absent" in line, line
    assert "records.db missing" not in line, line
    _assert_no_zero_verdict_counts(line)


def test_c2a_variant_verdicts_table_empty_reads_not_aggregated(tmp_dir):
    """File exists with a real verdicts table (via _make_db) but zero rows.
    MAX(window) returns NULL -- fresh/empty store. Must NOT say
    "records.db missing" (records.db is present and verdicts was found)."""
    db_path = _make_db(tmp_dir)  # verdicts table created, zero rows

    line = v.summary_line(db_path)
    assert "verdicts table empty" in line, line
    assert "records.db missing" not in line, line
    _assert_no_zero_verdict_counts(line)


def test_c2b_stale_ingest_reads_not_aggregated_with_timestamp(tmp_dir):
    db_path = _make_db(tmp_dir)
    stale_ts = _fresh_iso(timedelta(days=5))
    _insert_ingest_offset(db_path, stale_ts)
    _insert_verdict(db_path, WINDOW, "DELIVERING")

    line = v.summary_line(db_path, window=WINDOW)
    assert "not aggregated" in line, line
    _assert_no_zero_verdict_counts(line)
    assert stale_ts in line, f"expected the raw stale timestamp {stale_ts!r} in {line!r}"


def test_c2c_zero_rows_for_requested_window_reads_not_aggregated(tmp_dir):
    db_path = _make_db(tmp_dir)
    fresh_ts = _fresh_iso()
    _insert_ingest_offset(db_path, fresh_ts)
    _insert_verdict(db_path, OTHER_WINDOW, "DELIVERING")  # rows exist, but not for WINDOW

    line = v.summary_line(db_path, window=WINDOW)
    assert "not aggregated" in line, line
    _assert_no_zero_verdict_counts(line)


def test_verdicts_present_but_no_ingest_offset_rows_names_the_real_cause(tmp_dir):
    """Reviewer-fairy round 1 (PR-3170) suggestion, folded in: `verdicts`
    genuinely exists with real, healthy rows, but `ingest_offsets` has zero
    rows (never ingested, or only `compute_verdicts` has ever run against a
    hand-built fixture). This must never render the generic "records.db
    missing" message -- that name points at the wrong table when records.db
    and verdicts both demonstrably exist. It gets its own distinct message
    naming the ingest timestamp specifically, still "not aggregated", still
    zero verdict counts."""
    db_path = _make_db(tmp_dir)
    _insert_verdict(db_path, WINDOW, "DELIVERING")  # verdicts has real rows
    # No _insert_ingest_offset call -- ingest_offsets stays empty.

    line = v.summary_line(db_path, window=WINDOW)
    assert "not aggregated" in line, line
    assert "records.db missing" not in line, (
        f"must not blame the wrong table when records.db and verdicts both "
        f"genuinely exist: {line!r}"
    )
    assert line == "telemetry: not aggregated (ingest timestamp unavailable)", line
    _assert_no_zero_verdict_counts(line)


def test_malformed_window_raises_valueerror_propagates(tmp_dir):
    """Step 2's own contract: a malformed `window` argument is a caller bug,
    not a data-quality question -- `_validate_window`'s ValueError must
    propagate, never get swallowed into a "not aggregated" string."""
    db_path = _make_db(tmp_dir)
    _insert_ingest_offset(db_path, _fresh_iso())
    with pytest.raises(ValueError):
        v.summary_line(db_path, window="not-a-window")


# ---------------------------------------------------------------------------
# C3 -- engram_stats._diagnose_impl's guarded-import fallback, when
# engram_telemetry_verdicts genuinely cannot import.
# ---------------------------------------------------------------------------


def _make_fresh_stats():
    """Same recipe as tests/test_diagnose_calibration.py's own
    `_make_fresh_mods` -- reload the engram_* family fresh (purging any
    already-imported engram_telemetry_verdicts too) against a scratch
    ENGRAM_HOME, per this repo's IMPORT_RUBRIC R4 convention."""
    test_dir = tempfile.mkdtemp(prefix="engram_test_ws8_diagnose_")
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    stats = fresh_module(engram_stats)
    core = sys.modules["engram_core"]
    core._configure_paths(test_dir)
    core._ensure_data_dir()
    return stats, core, test_dir


def test_c3_diagnose_telemetry_not_installed_when_import_fails():
    stats, _core, test_dir = _make_fresh_stats()
    try:
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "engram_telemetry_verdicts":
                raise ImportError("simulated: engram_telemetry_verdicts not installed")
            return real_import(name, globals, locals, fromlist, level)

        with mock.patch.object(builtins, "__import__", side_effect=fake_import):
            result = stats._diagnose_impl()

        # _diagnose_impl() must return normally -- confirm valid JSON, no
        # exception escaped past the guarded import.
        data = json.loads(result)
        assert data["telemetry"] == "telemetry: not installed (convenience tier)", data.get("telemetry")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# C4 -- engram_stats._diagnose_impl's guarded CALL, when summary_line itself
# raises (a corrupt db, an unexpected schema, whatever) -- the caller's job
# per Part 1 step 5.
# ---------------------------------------------------------------------------


def test_c4_diagnose_telemetry_unavailable_when_summary_line_raises():
    stats, _core, test_dir = _make_fresh_stats()
    try:
        import engram_telemetry_verdicts as tv_mod  # fresh copy, post-purge

        def boom(*args, **kwargs):
            raise RuntimeError("simulated summary_line failure")

        with mock.patch.object(tv_mod, "summary_line", side_effect=boom):
            result = stats._diagnose_impl()

        data = json.loads(result)  # must not raise -- _diagnose_impl returns normally
        assert data["telemetry"].startswith("telemetry: unavailable ("), data.get("telemetry")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Mutation control (run by hand per this repo's Conclusion Warrant
# discipline -- see this PR's own handoff for the observed split). Not a
# permanent test: kept here as a comment for anyone re-running it, per the
# spec's "describe it in your handoff" instruction; the actual bypass edit
# is applied to engram_telemetry_verdicts.py itself, run, and reverted.
# ---------------------------------------------------------------------------
