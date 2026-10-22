"""Tests for engram_telemetry_verdicts.py -- WS7 (issue #3136, epic #2574
phase 2): the section-1 VERDICTS runtime report.

Design doc: docs/internal/2574-telemetry-design.md §1 (the VERDICTS block, quoted
condition-for-condition in engram_telemetry_verdicts.py's own
`_VERDICT_CONDITIONS` -- see that module's docstring). Contract-first per
issue #3136's own explicit test list and this repo's CLAUDE.md
"Contract-first tests" convention: every test below existed before
`compute_verdicts`/`_classify`/`brute_force_verdicts` were implemented
against them.

REVISION 2026-09-27 (issue #3136's own "REVISION 2026-09-27" block, the D1
fix): `compute_verdicts`'s signature dropped `manifest`/`src_root` entirely
in favor of a single `targets_map` argument -- the loaded, committed
`telemetry_targets.json` (or an equivalent fixture dict). This file's real-
data fixtures build `TARGETS_MAP` once via
`tools.telemetry_manifest_gate.build_dispatch_map` over the REAL manifest and
REAL source tree (so the derived target ids these tests assert against are
the ACTUAL ones the backward call-graph walk produces on THIS tree today,
matching the committed `src/engram/telemetry_targets.json` -- see
`test_tiers_manifest.py`'s own drift-gate test, which is what keeps the
committed file from silently diverging from this same walk). Only the
`l0_records` rows are synthetic (this repo has no live daemon populating
records.db yet -- WS6, #3135).

The join-key STATIC check (typo/missing-literal controls) lives in
tests/test_tiers_manifest.py, alongside that file's pre-existing
telemetry_manifest_gate pytest coverage (§9 tests 2/3/3b already live
there) -- a judgment call, documented in this PR's own handoff, to keep one
home for gate-level tests rather than splitting it across two files.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path

import pytest

import engram_telemetry_store as ts
import engram_telemetry_verdicts as v
from tools import telemetry_manifest_gate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ENGRAM = os.path.join(REPO_ROOT, "src", "engram")

MANIFEST = telemetry_manifest_gate.load_manifest()
SRC_ROOT = telemetry_manifest_gate.source_root(MANIFEST)
TARGETS_MAP = telemetry_manifest_gate.build_dispatch_map(SRC_ROOT, MANIFEST)

WINDOW = "2026-09-25"  # matches issue #3136's own "my seat's 2026-09-25 window"
OTHER_WINDOW = "2026-09-24"  # a different day -- window-scoping controls


def _make_db(tmp_dir):
    db_path = Path(tmp_dir) / "records.db"
    return ts.open_store(db_path)


def _insert_l0(conn, ts_utc, manifest_id, event_type, fields=None):
    conn.execute(
        "INSERT INTO l0_records (ts_utc, manifest_id, event_type, session_id, fields_json, source_file, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            ts_utc,
            manifest_id,
            event_type,
            None,
            json.dumps(fields or {}),
            "test-fixture.jsonl",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def _row_for(rows, site):
    matches = [r for r in rows if r["site"] == site]
    assert len(matches) == 1, f"expected exactly one row for site {site!r}, got {matches}"
    return matches[0]


@pytest.fixture()
def tmp_dir():
    d = tempfile.mkdtemp(prefix="engram_test_ws7_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# §12's brute force: the VERDICTS predicate block is exhaustive and
# mutually exclusive. Both numbers MUST be 0 (issue #3136's own contract
# test -- "paste unmatched/multi-match counts in your PR body").
# ---------------------------------------------------------------------------


def test_brute_force_verdicts_unmatched_and_multi_match_are_zero():
    result = v.brute_force_verdicts(thr=0.5)
    assert result["total"] == 243
    assert result["unmatched"] == 0, f"unmatched={result['unmatched']}, first={result['first_unmatched']}"
    assert result["multi_match"] == 0, f"multi_match={result['multi_match']}, first={result['first_multi_match']}"


def test_brute_force_control_degraded_line_mutation_reproduces_12():
    """§1's own stated CAN-IT-FAIL CONTROL: restoring `and I=0` to the
    DEGRADED line specifically reproduces unmatched=12 (the design doc's own
    number). Runs the SAME enumeration (`brute_force_verdicts`) against a
    deliberately-mutated condition list, never touching the real module
    state -- this is what proves `brute_force_verdicts` itself discriminates
    a broken predicate set, rather than trivially returning 0/0 regardless
    of input."""
    mutated = list(v._VERDICT_CONDITIONS)
    for idx, (name, _cond) in enumerate(mutated):
        if name == "DEGRADED":
            mutated[idx] = (
                name,
                lambda R, D, X, N, I, thr: R > 0 and I == 0 and D > 0 and (X / (D + X)) > thr,
            )
    result = v.brute_force_verdicts(thr=0.5, conditions=mutated)
    assert result["unmatched"] == 12, result
    assert result["multi_match"] == 0, result


def test_brute_force_control_delivering_line_mutation_reproduces_60():
    """Same recipe applied to DELIVERING instead -- §1's own text: "the same
    recipe applied to DELIVERING gives 60"."""
    mutated = list(v._VERDICT_CONDITIONS)
    for idx, (name, _cond) in enumerate(mutated):
        if name == "DELIVERING":
            mutated[idx] = (
                name,
                lambda R, D, X, N, I, thr: R > 0 and I == 0 and D > 0 and (X / (D + X)) <= thr,
            )
    result = v.brute_force_verdicts(thr=0.5, conditions=mutated)
    assert result["unmatched"] == 60, result
    assert result["multi_match"] == 0, result


def test_classify_raises_on_unmatched_tuple(monkeypatch):
    """`_classify`'s own error-handling contract: fail LOUD (ValueError)
    rather than silently returning a wrong verdict when a tuple resolves to
    ZERO VERDICTS lines. The REAL table is exhaustive for every non-negative
    integer (not bounded to the brute force's {0,1,2} domain -- verified by
    case analysis in this module's own docstring), so this branch is
    unreachable under real conditions; exercised here by monkeypatching a
    plausible-wrong (incomplete) condition list."""
    broken = [(name, cond) for name, cond in v._VERDICT_CONDITIONS if name != "NOT REACHED"]
    monkeypatch.setattr(v, "_VERDICT_CONDITIONS", broken)
    with pytest.raises(ValueError):
        v._classify(0, 0, 0, 0, 0, thr=0.5)


def test_classify_raises_on_multi_matched_tuple(monkeypatch):
    """Same contract, the other failure shape: a tuple matching MORE than
    one line."""
    duplicated = list(v._VERDICT_CONDITIONS) + [
        ("NOT REACHED (dup)", lambda R, D, X, N, I, thr: R == 0 and D == 0 and X == 0 and N == 0 and I == 0)
    ]
    monkeypatch.setattr(v, "_VERDICT_CONDITIONS", duplicated)
    with pytest.raises(ValueError):
        v._classify(0, 0, 0, 0, 0, thr=0.5)


# ---------------------------------------------------------------------------
# compute_verdicts -- error handling on a malformed window (issue #3136 D4:
# `_validate_window` uses `fullmatch`, not `match` -- a trailing newline
# bypasses `match`'s own `^...$` anchors, since `$` matches just before a
# trailing '\n', not only at the true end of string).
# ---------------------------------------------------------------------------


def test_compute_verdicts_rejects_malformed_window(tmp_dir):
    conn = _make_db(tmp_dir)
    with pytest.raises(ValueError):
        v.compute_verdicts(conn, TARGETS_MAP, "not-a-window")
    conn.close()


def test_compute_verdicts_rejects_trailing_newline_window(tmp_dir):
    """Issue #3136 D4's own contract test: `"2026-09-25\\n"` raises. Verified
    directly against `_validate_window` too (the exact regex-behavior
    argument), not only against `compute_verdicts`'s own top-level call."""
    import re

    assert re.compile(r"^\d{4}-\d{2}-\d{2}$").match("2026-09-25\n") is not None, (
        "sanity: bare `match` really does accept the trailing-newline string "
        "(this is the bug D4 fixes, not a fact about `fullmatch`)"
    )
    assert re.compile(r"^\d{4}-\d{2}-\d{2}$").fullmatch("2026-09-25\n") is None

    conn = _make_db(tmp_dir)
    with pytest.raises(ValueError):
        v.compute_verdicts(conn, TARGETS_MAP, "2026-09-25\n")
    conn.close()


# ---------------------------------------------------------------------------
# Real-data fixtures (issue #3136's own three, "from my seat's 2026-09-25
# window") -- real manifest + real source tree (via TARGETS_MAP, built from
# the same `build_dispatch_map` walk the committed telemetry_targets.json
# is generated from), synthetic l0_records rows.
# ---------------------------------------------------------------------------


def test_guarded_out_real_fixture_stop_hook_hook_fire(tmp_dir):
    """`stop-hook.hook_fire`'s derived dispatch target
    (hooks/claude/engram-stop-hook.py:main) is reached, but the site's own
    early return (_maturity_muted(), issue #845) means every outcome count
    stays 0 -- R>0, D=X=N=I=0 -> GUARDED OUT."""
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{WINDOW}T10:00:00.000Z", "hooks/claude/engram-stop-hook.py:main", "engram.telemetry.reached")

    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    row = _row_for(rows, "stop-hook.hook_fire")

    assert row["verdict"] == "GUARDED OUT", row
    assert row["R"] > 0, row
    assert (row["D"], row["X"], row["N"], row["I"]) == (0, 0, 0, 0), row
    assert row["flags"] == []
    assert row["unparseable"] == 0, row

    # Persisted, and readable straight back off the verdicts table.
    stored = conn.execute(
        "SELECT verdict, r, d, x, n, i FROM verdicts WHERE window = ? AND site = ?",
        (WINDOW, "stop-hook.hook_fire"),
    ).fetchone()
    assert stored["verdict"] == "GUARDED OUT"
    assert stored["r"] > 0 and stored["d"] == stored["x"] == stored["n"] == stored["i"] == 0
    conn.close()


def test_not_reached_real_fixture_server_engram_contradict(tmp_dir):
    """`server.engram_contradict`'s derived dispatch target
    (server.py:engram_contradict) is never reached in this window -- R=0,
    D=X=N=I=0 -> NOT REACHED (§1: "NOT a fault")."""
    conn = _make_db(tmp_dir)
    # Deliberately nothing inserted for this site/target within WINDOW.
    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    row = _row_for(rows, "server.engram_contradict")

    assert row["verdict"] == "NOT REACHED", row
    assert (row["R"], row["D"], row["X"], row["N"], row["I"]) == (0, 0, 0, 0, 0), row
    assert row["flags"] == []
    conn.close()


def test_dark_synthetic_fixture_server_engram_query(tmp_dir):
    """Synthetic (R>0, D=0, X>0): the dispatch target is reached, but every
    delivery attempt for the site itself is dropped -- DARK ("every attempt
    failed")."""
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{WINDOW}T09:00:00.000Z", "server.py:engram_query", "engram.telemetry.reached")
    _insert_l0(
        conn,
        f"{WINDOW}T09:00:01.000Z",
        "server.engram_query",
        "engram.tool.engram_call",
        fields={"outcome": "dropped"},
    )

    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    row = _row_for(rows, "server.engram_query")

    assert row["verdict"] == "DARK", row
    assert row["R"] > 0, row
    assert row["D"] == 0, row
    assert row["X"] > 0, row
    conn.close()


def test_window_scoping_excludes_rows_from_other_days(tmp_dir):
    """A row outside the requested window must not count toward R or any
    outcome bucket -- §1's WINDOW definition ("a UTC calendar day")."""
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{OTHER_WINDOW}T10:00:00.000Z", "hooks/claude/engram-stop-hook.py:main", "engram.telemetry.reached")

    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    row = _row_for(rows, "stop-hook.hook_fire")

    assert row["verdict"] == "NOT REACHED", (
        f"expected the OTHER_WINDOW reached row to be excluded from {WINDOW}'s R, got {row}"
    )
    assert row["R"] == 0, row
    conn.close()


def test_verdicts_table_written_for_every_declared_site(tmp_dir):
    """§1/the issue: "write every row, healthy included" -- an empty table
    must mean nothing ran, not nothing was wrong. One run over an entirely
    empty l0_records table must still write one row per declared site, and
    ZERO orphan/UNKNOWN-ID rows (there is nothing in l0_records to be
    orphaned)."""
    conn = _make_db(tmp_dir)
    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)

    declared_sites = set(TARGETS_MAP["sites"].keys())
    assert {r["site"] for r in rows} == declared_sites
    assert all(r["verdict"] == "NOT REACHED" for r in rows), (
        "every site should read NOT REACHED against a wholly empty store"
    )

    count = conn.execute("SELECT COUNT(*) FROM verdicts WHERE window = ?", (WINDOW,)).fetchone()[0]
    assert count == len(declared_sites)
    conn.close()


def test_flags_partial_and_init_flapping(tmp_dir):
    """PARTIAL (N>0 and D>0) and INIT-FLAPPING (I>0 and D>0) are FLAGS, not
    verdict lines (§1) -- they annotate whichever rate verdict fires."""
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{WINDOW}T09:00:00.000Z", "server.py:engram_query", "engram.telemetry.reached")
    _insert_l0(conn, f"{WINDOW}T09:00:01.000Z", "server.engram_query", "engram.tool.engram_call", fields={"outcome": "delivered"})
    _insert_l0(conn, f"{WINDOW}T09:00:02.000Z", "server.engram_query", "engram.tool.engram_call", fields={"outcome": "no_emitter"})
    _insert_l0(conn, f"{WINDOW}T09:00:03.000Z", "server.engram_query", "engram.tool.engram_call", fields={"outcome": "init_failed"})

    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    row = _row_for(rows, "server.engram_query")

    assert row["D"] == 1 and row["N"] == 1 and row["I"] == 1
    assert set(row["flags"]) == {"PARTIAL", "INIT-FLAPPING"}
    # D>0, X=0 -> DELIVERING (rate lines no longer gate on I=0, post round 10).
    assert row["verdict"] == "DELIVERING", row
    conn.close()


# ---------------------------------------------------------------------------
# Issue #3136 D2 -- §1 RUNTIME clause ("REVISION 2026-09-27" item 5): a
# `reached` row naming an id outside the map, or an outcome row naming an
# undeclared site, gets its OWN row: verdict INCONSISTENT, flag UNKNOWN-ID.
# ---------------------------------------------------------------------------


def test_unknown_id_reached_row_not_in_map(tmp_dir):
    """Plant a `reached` row whose manifest_id is not a target anywhere in
    TARGETS_MAP -- an INCONSISTENT + UNKNOWN-ID row must name it."""
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{WINDOW}T10:00:00.000Z", "totally.bogus.target.id", "engram.telemetry.reached")

    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    matches = [r for r in rows if r["site"] == "totally.bogus.target.id"]

    assert len(matches) == 1, matches
    assert matches[0]["verdict"] == "INCONSISTENT", matches[0]
    assert "UNKNOWN-ID" in matches[0]["flags"], matches[0]
    assert matches[0]["R"] == 1, matches[0]
    conn.close()


def test_unknown_id_outcome_row_undeclared_site(tmp_dir):
    """Plant an outcome row whose manifest_id is not a declared site at all
    -- the same INCONSISTENT + UNKNOWN-ID shape, the other direction."""
    conn = _make_db(tmp_dir)
    _insert_l0(
        conn,
        f"{WINDOW}T10:00:01.000Z",
        "totally.undeclared.site",
        "engram.tool.engram_call",
        fields={"outcome": "delivered"},
    )

    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    matches = [r for r in rows if r["site"] == "totally.undeclared.site"]

    assert len(matches) == 1, matches
    assert matches[0]["verdict"] == "INCONSISTENT", matches[0]
    assert "UNKNOWN-ID" in matches[0]["flags"], matches[0]
    conn.close()


def test_zero_orphans_surfaces_nothing(tmp_dir):
    """A run with zero orphaned ids surfaces nothing beyond the declared
    sites -- no INCONSISTENT/UNKNOWN-ID marker rows appear."""
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{WINDOW}T10:00:00.000Z", "hooks/claude/engram-stop-hook.py:main", "engram.telemetry.reached")

    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    declared = set(TARGETS_MAP["sites"].keys())
    extra = {r["site"] for r in rows} - declared
    assert not extra, f"expected no orphan/UNKNOWN-ID rows, got extra sites: {extra}"
    assert not any("UNKNOWN-ID" in r["flags"] for r in rows)
    conn.close()


# ---------------------------------------------------------------------------
# Issue #3136 D3 -- unparseable outcome rows are COUNTED, not silently
# dropped: a site with R>0 whose only outcome rows are malformed must read
# GUARDED OUT WITH the UNPARSEABLE flag and unparseable=N, never a bare
# GUARDED OUT (which would hide a real DARK/DELIVERING site).
# ---------------------------------------------------------------------------


def test_unparseable_outcome_rows_flagged_not_dropped(tmp_dir):
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{WINDOW}T10:00:00.000Z", "hooks/claude/engram-stop-hook.py:main", "engram.telemetry.reached")
    # Malformed fields_json (not valid JSON at all).
    conn.execute(
        "INSERT INTO l0_records (ts_utc, manifest_id, event_type, session_id, fields_json, source_file, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"{WINDOW}T10:00:01.000Z",
            "stop-hook.hook_fire",
            "engram.hook.fire",
            None,
            "{not valid json",
            "test-fixture.jsonl",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    # A second malformed row: valid JSON, but an outcome value outside the
    # 4 known keys.
    _insert_l0(conn, f"{WINDOW}T10:00:02.000Z", "stop-hook.hook_fire", "engram.hook.fire", fields={"outcome": "sideways"})

    rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    row = _row_for(rows, "stop-hook.hook_fire")

    assert row["verdict"] == "GUARDED OUT", row
    assert "UNPARSEABLE" in row["flags"], row
    assert row["unparseable"] == 2, row
    assert (row["D"], row["X"], row["N"], row["I"]) == (0, 0, 0, 0), row
    conn.close()


# ---------------------------------------------------------------------------
# Issue #3136's own mutation control, kept from before (revised for the
# map-based design, per the issue's "REVISION 2026-09-27": "the join is now
# 'map lookup' instead of 'walk', so the control replaces the map lookup
# with 'any record for this site exists'"). Discriminates: the stop-hook
# fixture (whose `reached` row is keyed by the DERIVED TARGET id, not the
# site literal) flips to NOT REACHED under the naive query; the DARK
# fixture (whose OUTCOME row happens to share manifest_id with the site
# literal itself) stays DARK.
# ---------------------------------------------------------------------------


def _naive_any_record_for_site_R(targets_map, conn, site_literal, window):
    """Mutation-control replacement for `_compute_R`: 'any record for this
    site exists' -- ignores the map's own derived target-id list entirely,
    querying the SITE LITERAL directly (any event_type) as if it were its
    own reached-target id."""
    row = conn.execute(
        "SELECT COUNT(*) FROM l0_records WHERE manifest_id = ? AND substr(ts_utc, 1, 10) = ?",
        (site_literal, window),
    ).fetchone()
    return row[0] if row else 0


def test_mutation_control_naive_any_record_flips_stop_hook_to_not_reached(tmp_dir):
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{WINDOW}T10:00:00.000Z", "hooks/claude/engram-stop-hook.py:main", "engram.telemetry.reached")

    with mock.patch.object(v, "_compute_R", side_effect=_naive_any_record_for_site_R):
        rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    row = _row_for(rows, "stop-hook.hook_fire")

    assert row["verdict"] == "NOT REACHED", (
        f"expected the naive site-literal query to MISS the stop-hook's real "
        f"reached row (keyed by the DERIVED target id, not the site literal), "
        f"got {row} -- if this fails, the map-lookup join is NOT actually "
        f"load-bearing (round-10-style non-discriminating control)."
    )
    conn.close()


def test_mutation_control_naive_any_record_leaves_dark_as_dark(tmp_dir):
    conn = _make_db(tmp_dir)
    _insert_l0(conn, f"{WINDOW}T09:00:00.000Z", "server.py:engram_query", "engram.telemetry.reached")
    _insert_l0(
        conn,
        f"{WINDOW}T09:00:01.000Z",
        "server.engram_query",
        "engram.tool.engram_call",
        fields={"outcome": "dropped"},
    )

    with mock.patch.object(v, "_compute_R", side_effect=_naive_any_record_for_site_R):
        rows = v.compute_verdicts(conn, TARGETS_MAP, WINDOW)
    row = _row_for(rows, "server.engram_query")

    assert row["verdict"] == "DARK", (
        f"expected DARK to survive the naive query (the site's OWN outcome "
        f"row happens to share manifest_id with the site literal, so the "
        f"naive 'any record exists' check still finds SOMETHING), got {row}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# Issue #3136 D1's own authoritative regression test ("REVISION 2026-09-27"
# contract test list): copy engram_telemetry_verdicts.py +
# telemetry_targets.json (+ its legitimate sibling runtime dependency,
# engram_telemetry_store.py -- a real convenience-tier file that ships
# alongside it in the real bundle) into an ISOLATED directory with NO src/,
# NO tiers.json, and NO tools/telemetry_manifest_gate.py present at all,
# then import and run compute_verdicts from there. The test must fail if
# the module still imports the gate or resolves any path outside its own
# directory -- which it would, as either a ModuleNotFoundError at import
# time or a stray path-resolution error, since neither is reachable from
# the isolated directory.
# ---------------------------------------------------------------------------


def test_deployed_layout_isolation_no_gate_import(tmp_path):
    iso_dir = tmp_path / "isolated_deploy"
    iso_dir.mkdir()
    for fname in ("engram_telemetry_verdicts.py", "telemetry_targets.json", "engram_telemetry_store.py"):
        shutil.copy2(os.path.join(SRC_ENGRAM, fname), iso_dir / fname)

    # Purge any already-imported copies (and the gate/tools package) so the
    # isolated import below cannot silently reuse them from this same
    # pytest process's module cache.
    purge_prefixes = ("engram_telemetry_verdicts", "engram_telemetry_store", "tools")
    saved_modules = {
        name: mod for name, mod in sys.modules.items()
        if name == purge_prefixes[0] or name == purge_prefixes[1] or name == "tools" or name.startswith("tools.")
    }
    for name in list(saved_modules):
        del sys.modules[name]

    saved_path = list(sys.path)
    # Strip every sys.path entry that could resolve the real tree (repo
    # root, src/engram, or anything inside the repo) -- keep stdlib/
    # site-packages entries, plus the isolated dir itself at the front.
    filtered = [p for p in saved_path if REPO_ROOT not in os.path.abspath(p or ".")]
    sys.path[:] = [str(iso_dir)] + filtered

    try:
        import importlib

        v_iso = importlib.import_module("engram_telemetry_verdicts")
        ts_iso = importlib.import_module("engram_telemetry_store")

        assert "tools" not in sys.modules, (
            "the isolated import pulled in a `tools` package -- "
            "engram_telemetry_verdicts.py must import NOTHING from "
            "tools.telemetry_manifest_gate at runtime (issue #3136 D1)"
        )

        db_path = iso_dir / "records.db"
        conn = ts_iso.open_store(db_path)
        with open(iso_dir / "telemetry_targets.json", encoding="utf-8") as f:
            targets_map = json.load(f)

        # Plant one real reached row for a real declared site with a
        # non-empty target list, then confirm compute_verdicts (the
        # ISOLATED copy) returns the correct verdict with zero gate/
        # manifest access.
        site, target_ids = next((s, ids) for s, ids in targets_map["sites"].items() if ids)
        conn.execute(
            "INSERT INTO l0_records (ts_utc, manifest_id, event_type, session_id, fields_json, source_file, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"{WINDOW}T00:00:00.000Z",
                target_ids[0],
                "engram.telemetry.reached",
                None,
                "{}",
                "iso-fixture.jsonl",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()

        rows = v_iso.compute_verdicts(conn, targets_map, WINDOW)
        row = next(r for r in rows if r["site"] == site)
        assert row["verdict"] == "GUARDED OUT", row
        assert row["R"] > 0, row
        conn.close()
    finally:
        sys.path[:] = saved_path
        for name in ("engram_telemetry_verdicts", "engram_telemetry_store"):
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
