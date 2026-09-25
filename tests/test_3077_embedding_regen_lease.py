"""Contract-first tests for the embedding restore-completeness regeneration
single-flight lease (#3077).

Written BEFORE the implementation, per this repo's contract-first-tests
convention (CLAUDE.md) — same discipline used throughout #3075/PR#3076,
which this issue was split off from (Borges's colleague-pass findings B/C).

Background: every process that runs `_run_db_one_time_setup` with missing
embeddings > 0 spawns its own `_run_embedding_restore_regeneration` daemon
thread. That's safe (NULL-only, idempotent) but wasteful under concurrency —
N processes each redundantly re-embed the same NULL rows. This adds a
cross-process lease: `_try_acquire_regeneration_lease(conn, null_count=...)`
atomically checks-and-sets a `cause="regeneration-running"` state in the
existing `embedding_restore_state` singleton row, using SQLite's own
transactional serialization (a conditional `INSERT ... ON CONFLICT DO UPDATE
... WHERE ...` — verified empirically, not assumed, to behave as a genuine
cross-connection compare-and-swap, including under real thread/connection
concurrency) as the mutual-exclusion primitive. No new lock, no new table.

Contract for `_try_acquire_regeneration_lease(conn, *, null_count: int) -> bool`:

  - Returns True (and sets cause="regeneration-running" with a FRESH
    updated_at, not_fully_restored=1) whenever no fresh lease already
    exists: no row at all, cause is NULL (the cleared/all-clear state),
    cause is any OTHER terminal value (disabled-by-config /
    embedder-unavailable / per-node-failures / detection-failed:... /
    regeneration-pending), or cause IS "regeneration-running" but its
    updated_at is OLDER than the staleness cutoff
    (_REGENERATION_LEASE_STALENESS_SECONDS).
  - Returns False (and makes NO changes to the row -- verified by exact
    updated_at equality, not just "still not_fully_restored") whenever a
    FRESH "regeneration-running" lease already exists.
  - Atomic under real concurrency: of two callers racing on the same DB
    file via independent connections, exactly one acquires.

Contract for `_run_embedding_restore_regeneration(db_path)`'s NEW behavior:

  - Attempts to acquire the lease FIRST, before doing any scan/embed work.
  - If acquisition FAILS (another process's fresh lease exists): returns
    immediately, does NOT call `_reembed_null_nodes`, does NOT call
    `embed()` even once, does NOT touch the existing lease row at all.
  - If acquisition SUCCEEDS: proceeds exactly as before (unchanged
    behavior from #3075's round 4) -- and since `_finalize_embedding_
    restore_flag` always ends by writing a DIFFERENT cause (or clearing
    the flag entirely), the lease is inherently self-releasing on
    completion, success or failure alike.

Out of scope (per issue #3077's own body, a deliberate scope split from
PR#3076): a heartbeat mechanism to refresh the lease mid-pass. A real pass
exceeding the staleness threshold could theoretically let a second process
re-acquire and race -- accepted for v1 since the operation is idempotent and
safe (the issue's own framing: "no data corruption risk"), same trade-off
this whole feature already made peace with for redundant-but-safe work.
The manual regen script (tools/engram-regenerate-embeddings.py) is
deliberately NOT gated behind this lease -- a human/agent running it
explicitly wants a fresh backfill NOW, not to be silently skipped because a
background thread happens to be mid-pass.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# conftest.py adds src/engram to sys.path.
import engram_core  # noqa: E402


def _fresh_core_in_tmp(prefix: str):
    """Return (core, core_test_dir) -- mirrors tests/test_3075_embedding_
    pipeline_restore.py's own identically-named helper (not cross-imported
    -- no cross-test-file import convention exists in this repo).
    """
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from conftest import fresh_module

    core = fresh_module(engram_core)
    core_test_dir = tempfile.mkdtemp(prefix=prefix)
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    core._configure_paths(core_test_dir)
    core._ensure_data_dir()
    return core, core_test_dir


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# _try_acquire_regeneration_lease -- unit-level contract
# ---------------------------------------------------------------------------

class TestTryAcquireRegenerationLease:

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_3077_lease_"))
        self.db_path = self.tmp / "fixture.db"

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        engram_core._ensure_embedding_restore_state_table(conn)
        return conn

    def _row(self, conn):
        return conn.execute(
            "SELECT not_fully_restored, null_count, cause, updated_at "
            "FROM embedding_restore_state WHERE singleton_key = 1"
        ).fetchone()

    def test_acquire_succeeds_when_no_row_exists(self):
        conn = self._conn()
        acquired = engram_core._try_acquire_regeneration_lease(conn, null_count=3)
        row = self._row(conn)
        conn.close()
        assert acquired is True
        assert row is not None
        assert row[2] == "regeneration-running"
        assert row[0] == 1  # not_fully_restored

    def test_acquire_succeeds_when_cause_is_cleared_null(self):
        conn = self._conn()
        engram_core._clear_embedding_restore_flag(conn)
        row_before = self._row(conn)
        assert row_before[2] is None  # sanity: genuinely cleared

        acquired = engram_core._try_acquire_regeneration_lease(conn, null_count=1)
        row_after = self._row(conn)
        conn.close()
        assert acquired is True, (
            "a cleared (cause IS NULL) flag must not block acquisition -- "
            "this is the exact NULL-comparison hazard a naive `cause != "
            "'regeneration-running'` WHERE clause gets wrong in SQL "
            "(NULL != anything is NULL, not TRUE, so the row would never "
            "match and the lease could never be acquired again)"
        )
        assert row_after[2] == "regeneration-running"

    def test_acquire_succeeds_when_cause_is_a_terminal_value(self):
        conn = self._conn()
        engram_core._set_embedding_restore_flag(
            conn, null_count=2, cause="per-node-failures",
        )
        acquired = engram_core._try_acquire_regeneration_lease(conn, null_count=2)
        row = self._row(conn)
        conn.close()
        assert acquired is True
        assert row[2] == "regeneration-running"

    def test_acquire_fails_when_fresh_lease_already_held(self):
        conn = self._conn()
        first = engram_core._try_acquire_regeneration_lease(conn, null_count=5)
        row_after_first = self._row(conn)
        assert first is True

        second = engram_core._try_acquire_regeneration_lease(conn, null_count=5)
        row_after_second = self._row(conn)
        conn.close()

        assert second is False, (
            "a fresh 'regeneration-running' lease must block a second "
            "acquisition attempt"
        )
        assert row_after_second == row_after_first, (
            "a blocked acquisition attempt must make NO changes to the "
            "row -- not just 'still not_fully_restored', the EXACT prior "
            f"row, got before={row_after_first} after={row_after_second}"
        )

    def test_acquire_succeeds_when_lease_is_stale(self):
        conn = self._conn()
        stale_ts = _iso(
            datetime.now(timezone.utc)
            - timedelta(seconds=engram_core._REGENERATION_LEASE_STALENESS_SECONDS + 60)
        )
        conn.execute(
            "INSERT INTO embedding_restore_state "
            "(singleton_key, not_fully_restored, null_count, cause, updated_at) "
            "VALUES (1, 1, 4, 'regeneration-running', ?)",
            (stale_ts,),
        )
        conn.commit()

        acquired = engram_core._try_acquire_regeneration_lease(conn, null_count=4)
        row = self._row(conn)
        conn.close()
        assert acquired is True, (
            "a stale lease (older than the staleness threshold) must be "
            "re-acquirable -- otherwise a crashed lease-holder permanently "
            "blocks all future regeneration"
        )
        assert row[3] != stale_ts, "updated_at must be refreshed on reacquire"

    def test_acquire_is_atomic_under_real_concurrency(self):
        """Can-it-fail via genuine concurrency, not just sequential calls:
        two independent connections (simulating two processes) race to
        acquire on the SAME db file. Exactly one must succeed. Repeated 5
        times for confidence (matches the manual verification this
        contract's design was checked against before writing this test).
        """
        # Ensure the table/row exist before racing (avoids a schema-creation
        # race that isn't the thing under test).
        setup_conn = self._conn()
        setup_conn.close()

        for round_i in range(5):
            # Reset to a clean, unset state before each round.
            reset_conn = sqlite3.connect(str(self.db_path))
            reset_conn.execute("DELETE FROM embedding_restore_state")
            reset_conn.commit()
            reset_conn.close()

            results: dict[str, bool] = {}

            def worker(name: str):
                conn = sqlite3.connect(str(self.db_path), timeout=30.0)
                results[name] = engram_core._try_acquire_regeneration_lease(
                    conn, null_count=1,
                )
                conn.close()

            t1 = threading.Thread(target=worker, args=(f"A{round_i}",))
            t2 = threading.Thread(target=worker, args=(f"B{round_i}",))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert sorted(results.values()) == [False, True], (
                f"round {round_i}: exactly one racer must acquire, got {results}"
            )


# ---------------------------------------------------------------------------
# _run_embedding_restore_regeneration -- integration with the lease
# ---------------------------------------------------------------------------

class TestRegenerationRespectsLease:

    def _insert_null_embedding_nodes(self, conn, n=3):
        for i in range(n):
            conn.execute(
                "INSERT INTO nodes (id, type, created_at, claim, status, is_current) "
                "VALUES (?, 'observation', '2026-01-01T00:00:00Z', ?, 'active', 1)",
                (f"ob_3077_{i}", f"claim text {i}"),
            )
        conn.commit()

    def test_skips_all_work_when_lease_already_held(self):
        """If another process's lease is fresh, _run_embedding_restore_
        regeneration must do ZERO work: no _reembed_null_nodes call (proxy:
        embed() is never called), no flag write at all -- the pre-existing
        lease row is left byte-for-byte untouched."""
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_skip_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 3)
            # Pre-acquire the lease ourselves, simulating another process.
            acquired = core._try_acquire_regeneration_lease(conn, null_count=3)
            assert acquired is True
            lease_before = core._get_embedding_restore_flag(conn)
            conn.close()

            embed_mock = mock.Mock(
                side_effect=AssertionError(
                    "embed() must not be called when the lease is already held"
                )
            )
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", embed_mock):
                core._run_embedding_restore_regeneration(str(core.DB_PATH))

            embed_mock.assert_not_called()
            check_conn = sqlite3.connect(str(core.DB_PATH))
            lease_after = core._get_embedding_restore_flag(check_conn)
            null_after = check_conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            check_conn.close()

            assert null_after == 3, "no node should have been touched"
            assert lease_after == lease_before, (
                f"a blocked regeneration attempt must leave the lease row "
                f"untouched, got before={lease_before} after={lease_after}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_proceeds_and_self_releases_when_no_lease_held(self):
        """Baseline regression guard: with no pre-existing lease, the pass
        runs normally (unchanged #3075-round-4 behavior) and the lease is
        naturally released on completion -- the final cause is never
        'regeneration-running' once the pass has finished."""
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_proceed_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 2)
            conn.close()

            fake_vector = [0.1] * 384
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(core._embedder, "embed", return_value=fake_vector):
                core._run_embedding_restore_regeneration(str(core.DB_PATH))

            check_conn = sqlite3.connect(str(core.DB_PATH))
            flag = core._get_embedding_restore_flag(check_conn)
            null_after = check_conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE is_current=1 AND embedding IS NULL"
            ).fetchone()[0]
            check_conn.close()

            assert null_after == 0, "both nodes should have embedded successfully"
            assert flag["cause"] != "regeneration-running", (
                "the lease must be released (overwritten to a terminal "
                "cause or cleared) once the pass completes -- it must "
                f"never be left as a permanent lease, got {flag}"
            )
            assert flag["not_fully_restored"] is False
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Detection-site / manual-script writers must not clobber a fresh lease
# (round 2, closing the round-1 reviewer-fairy's blocker on PR#3078)
# ---------------------------------------------------------------------------

class TestNonHolderWritersRespectFreshLease:
    """The round-1 reviewer-fairy on PR#3078 found (and I independently
    reproduced, with the shipped functions against a real db file) that the
    lease from round 1 was NOT actually cross-process exclusive under its
    own stated primary scenario: _run_db_one_time_setup's detection-site
    write (_set_embedding_restore_flag, called unconditionally whenever
    missing-embeddings > 0) clobbers a FRESH "regeneration-running" lease
    held by another process to cause="regeneration-pending" -- and that
    process's own subsequent _try_acquire_regeneration_lease call then
    succeeds again, because "regeneration-pending" reads as "any other
    terminal value" to that function's own (individually correct) contract.
    Two processes now run a fully redundant concurrent regeneration pass on
    ordinary concurrent startup -- no crash, no staleness window needed --
    defeating #3077's entire point.

    Fix: _fresh_regeneration_lease_held(conn) -> bool, a read-only check
    used ONLY by writers that structurally never hold the lease themselves
    (the detection site; the manual regen script's own finalize call) to
    skip their write entirely when someone else's lease is fresh. NEVER
    used by _run_embedding_restore_regeneration's own finalize call, which
    IS the lease holder legitimately releasing its own lease -- that path
    stays unconditional, unchanged.
    """

    def _insert_null_embedding_nodes(self, conn, n=3):
        for i in range(n):
            conn.execute(
                "INSERT INTO nodes (id, type, created_at, claim, status, is_current) "
                "VALUES (?, 'observation', '2026-01-01T00:00:00Z', ?, 'active', 1)",
                (f"ob_3077r2_{i}", f"claim text {i}"),
            )
        conn.commit()

    def test_detection_site_does_not_clobber_fresh_lease_end_to_end(self):
        """(round 2) The exact scenario the round-1 reviewer-fairy's
        blocker reproduced, as an end-to-end regression test: process A
        acquires the lease; process B's ordinary startup then runs
        detection with missing embeddings > 0 (embedder available) --
        process B's detection-site write must NOT touch the lease row at
        all, and process B's own subsequent lease-acquisition attempt must
        then correctly return False (no redundant second pass).

        Can-it-fail: against round 1's code (unconditional
        _set_embedding_restore_flag at the detection site) this test must
        FAIL -- process B's detection write clobbers the lease to
        "regeneration-pending" and process B's re-acquisition attempt
        incorrectly succeeds. After the fix, it must PASS.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_r2_detect_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 3)
            conn.close()

            # Process A: acquires the lease (simulating its own in-flight
            # regeneration pass -- we never let it finish, to keep the
            # lease fresh for B's attempt).
            conn_a = sqlite3.connect(str(core.DB_PATH))
            acquired_a = core._try_acquire_regeneration_lease(conn_a, null_count=3)
            assert acquired_a is True
            lease_after_a = core._get_embedding_restore_flag(conn_a)
            conn_a.close()

            # Process B: a second connection runs detection directly
            # (mirrors what _run_db_one_time_setup's own detection block
            # does when embedder is available and missing > 0), then
            # attempts its own lease acquisition -- exactly the fairy's
            # own repro shape, driving the actual shipped functions.
            conn_b = sqlite3.connect(str(core.DB_PATH))
            conn_b.row_factory = sqlite3.Row
            with mock.patch.object(core._embedder, "is_available", return_value=True), \
                 mock.patch.object(
                     core, "_get_embedding_config",
                     return_value={"enabled": True, "model": "x"},
                 ):
                core._run_db_one_time_setup(conn_b)
            lease_after_b_detection = core._get_embedding_restore_flag(conn_b)

            assert lease_after_b_detection == lease_after_a, (
                f"process B's detection-site write must NOT touch process "
                f"A's fresh lease at all -- got before={lease_after_a} "
                f"after={lease_after_b_detection}"
            )

            acquired_b = core._try_acquire_regeneration_lease(conn_b, null_count=3)
            conn_b.close()
            assert acquired_b is False, (
                "process B must NOT be able to acquire the lease while "
                "process A's lease is still fresh -- a successful "
                "acquisition here means a fully redundant concurrent "
                "regeneration pass would run, defeating #3077's entire "
                "point"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_detection_site_clear_branch_does_not_clobber_fresh_lease(self):
        """(round 2) The zero-count branch (_clear_embedding_restore_flag)
        shares the same unconditional-write root cause -- a process that
        samples missing-count as 0 near the tail of another process's
        in-flight pass must not clear away that fresh lease early."""
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_r2_clear_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()  # no NULL-embedding nodes -- count will read 0
            conn_a = conn
            acquired_a = core._try_acquire_regeneration_lease(conn_a, null_count=0)
            assert acquired_a is True
            lease_after_a = core._get_embedding_restore_flag(conn_a)
            conn_a.close()

            conn_b = sqlite3.connect(str(core.DB_PATH))
            conn_b.row_factory = sqlite3.Row
            core._run_db_one_time_setup(conn_b)
            lease_after_b = core._get_embedding_restore_flag(conn_b)
            conn_b.close()

            assert lease_after_b == lease_after_a, (
                f"the zero-count detection branch must not clear a fresh "
                f"lease held by another process, got before={lease_after_a} "
                f"after={lease_after_b}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_manual_script_finalize_does_not_clobber_fresh_lease(self):
        """(round 2, the round-1 reviewer-fairy's non-blocking Suggestion;
        UPDATED round 4 to match the manual script's ACTUAL current call
        shape) The manual regen script never acquires the lease itself,
        so ANY fresh "regeneration-running" lease found at its own
        finalize call is definitionally someone else's -- clobbering it
        with the script's own outcome must not happen.

        Round 2 originally mirrored the manual script's THEN-current
        shape: a separate `if not _fresh_regeneration_lease_held(conn):`
        pre-check wrapping the whole finalize call. Round 4 (Borges's
        colleague-pass required fix) removed that pattern from the real
        script entirely -- it now passes `respect_lease=True` directly
        on the `_finalize_embedding_restore_flag` call, closing the
        check-then-act TOCTOU that wrapper pattern had. This test is
        updated to mirror THAT shape exactly, so it stays a faithful
        proxy for tools/engram-regenerate-embeddings.py's real call site
        rather than testing a pattern the real code no longer uses.
        (Overlaps with test_finalize_respect_lease_refuses_pre_existing_
        fresh_lease above by design -- that one documents the general
        contract, this one exists specifically to track the manual
        script's own call shape.)
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_r2_manual_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 2)
            acquired = core._try_acquire_regeneration_lease(conn, null_count=2)
            assert acquired is True
            lease_before = core._get_embedding_restore_flag(conn)

            # Mirrors the manual script's own finalize call site
            # (tools/engram-regenerate-embeddings.py) exactly -- a fake
            # counts dict since we're testing the guard, not the
            # backfill loop itself.
            core._finalize_embedding_restore_flag(
                conn, {"processed": 2, "embedded": 2, "skipped": 0, "failed": 0},
                respect_lease=True,
            )
            lease_after = core._get_embedding_restore_flag(conn)
            conn.close()

            assert lease_after == lease_before, (
                f"a finalize call from a non-holder must not clobber "
                f"another process's fresh lease, got before={lease_before} "
                f"after={lease_after}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_detection_failed_handler_does_not_clobber_fresh_lease_when_guard_itself_fails(self):
        """(round 3 -- folding the round-2 reviewer-fairy's non-blocking
        Suggestion, and independently self-caught via a completeness grep
        of every _set_embedding_restore_flag/_clear_embedding_restore_flag
        call site before that review landed) A FIFTH unconditional writer
        existed: the detection-failed exception handler at the bottom of
        _run_db_one_time_setup's embedding restore-completeness try/except
        (from #3075 round 5) sat OUTSIDE round 2's guard.
        _fresh_regeneration_lease_held is the FIRST statement in that try
        block, but it is NOT the only one that can raise -- so are
        _count_missing_embeddings, _get_embedding_config,
        _embedder.is_available(), and the flag-write calls themselves.
        This test exercises the narrowest case (the freshness check
        ITSELF raising, e.g. DB contention on the embedding_restore_state
        table) because it's the cleanest to construct deterministically,
        but the fix it verifies -- re-guarding the except handler's own
        write with the same freshness check -- protects the BROADER case
        too: whichever statement in the try block actually raised,
        we genuinely don't know whether a lease is fresh at that point,
        and must fail SAFE (skip the write) rather than assume "no
        lease" and clobber one that IS fresh. (Round-3 reviewer-fairy
        finding: an earlier version of this docstring and the commit
        message both overstated this as the ONLY way to reach the
        handler -- correctness of the fix doesn't depend on that
        narrower framing, but the framing itself was wrong and is
        corrected here.)

        Can-it-fail: against round 2's code (the except handler's flag
        write is unconditional) this test must FAIL -- the fresh lease
        gets clobbered to cause="detection-failed: RuntimeError". After
        the fix, it must PASS.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_r3_detectfail_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 1)
            acquired = core._try_acquire_regeneration_lease(conn, null_count=1)
            assert acquired is True
            lease_before = core._get_embedding_restore_flag(conn)
            conn.close()

            conn_b = sqlite3.connect(str(core.DB_PATH))
            conn_b.row_factory = sqlite3.Row

            def _raise(_conn):
                raise RuntimeError("simulated lease-check failure (test double)")

            with mock.patch.object(core, "_fresh_regeneration_lease_held", side_effect=_raise):
                core._run_db_one_time_setup(conn_b)

            lease_after = core._get_embedding_restore_flag(conn_b)
            conn_b.close()
            assert lease_after == lease_before, (
                f"the detection-failed exception handler must not clobber "
                f"a fresh lease even when the freshness check itself "
                f"fails, got before={lease_before} after={lease_after}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_set_flag_respect_lease_refuses_pre_existing_fresh_lease(self):
        """(round 4, Borges's colleague-pass REQUIRED fix on PR#3078)
        Rounds 2-3's non-holder guard was CHECK-THEN-ACT:
        `if not _fresh_regeneration_lease_held(conn): <separate write>`
        -- a genuine TOCTOU: a lease acquired in the window between that
        read and the later write would be silently clobbered by it,
        because the write itself carried no memory of what it was
        conditioned on. Round 4 closes the window at its SOURCE by
        folding the guard into the write's own atomic SQL
        (`respect_lease=True` on `_set_embedding_restore_flag` itself,
        the same WHERE-clause pattern `_try_acquire_regeneration_lease`
        already uses) -- there is no longer a separate read step for
        anything to race against inside the write function itself.

        Given that, the test that actually matters is simpler than a
        race simulation: does `respect_lease=True` correctly refuse to
        clobber a lease that is ALREADY fresh at write time? (The
        atomicity of the underlying UPSERT-with-WHERE pattern is
        separately, directly verified under real concurrent threads by
        TestTryAcquireRegenerationLease.test_acquire_is_atomic_under_
        real_concurrency, which exercises the identical SQL shape.)

        Can-it-fail: against round 3's code (`_set_embedding_restore_flag`
        with no `respect_lease` parameter at all) this call would
        TypeError; against a hypothetical version that accepts but
        ignores the parameter, this test would fail on the row
        comparison. After the real fix, it passes.
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_r4_set_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            acquired = core._try_acquire_regeneration_lease(conn, null_count=3)
            assert acquired is True
            lease_before = core._get_embedding_restore_flag(conn)

            wrote = core._set_embedding_restore_flag(
                conn, null_count=7, cause="regeneration-pending",
                respect_lease=True,
            )
            lease_after = core._get_embedding_restore_flag(conn)
            conn.close()

            assert wrote is False, (
                "a respect_lease=True write must report it did NOT write "
                "when a fresh lease already exists"
            )
            assert lease_after == lease_before, (
                f"a respect_lease=True write must leave a fresh lease "
                f"completely untouched, got before={lease_before} "
                f"after={lease_after}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_clear_flag_respect_lease_refuses_pre_existing_fresh_lease(self):
        """(round 4) Same contract as the set-flag test above, for
        _clear_embedding_restore_flag's respect_lease parameter -- the
        zero-missing-count detection branch and the manual script's
        processed==0 branch both route through this."""
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_r4_clear_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            acquired = core._try_acquire_regeneration_lease(conn, null_count=0)
            assert acquired is True
            lease_before = core._get_embedding_restore_flag(conn)

            wrote = core._clear_embedding_restore_flag(conn, respect_lease=True)
            lease_after = core._get_embedding_restore_flag(conn)
            conn.close()

            assert wrote is False
            assert lease_after == lease_before, (
                f"a respect_lease=True clear must leave a fresh lease "
                f"completely untouched, got before={lease_before} "
                f"after={lease_after}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)

    def test_finalize_respect_lease_refuses_pre_existing_fresh_lease(self):
        """(round 4, Borges's colleague-pass REQUIRED fix) The manual
        regen script's own finalize call (never the lease holder) must
        thread respect_lease=True all the way through
        _finalize_embedding_restore_flag into its internal clear/set
        writes -- closing the SAME check-then-act TOCTOU one level up
        from the direct flag-write functions (the script previously
        wrapped the WHOLE _finalize_embedding_restore_flag call in its
        own separate `if not fresh_lease_held(conn): ...` pre-check,
        exactly the pattern round 4 eliminates everywhere else).
        """
        core, core_test_dir = _fresh_core_in_tmp("engram_test_3077_r4_finalize_")
        try:
            os.environ.pop("ENGRAM_NO_EMBEDDINGS", None)
            conn = core._get_db()
            self._insert_null_embedding_nodes(conn, 1)
            acquired = core._try_acquire_regeneration_lease(conn, null_count=1)
            assert acquired is True
            lease_before = core._get_embedding_restore_flag(conn)

            core._finalize_embedding_restore_flag(
                conn, {"processed": 1, "embedded": 1, "skipped": 0, "failed": 0},
                respect_lease=True,
            )
            lease_after = core._get_embedding_restore_flag(conn)
            conn.close()

            assert lease_after == lease_before, (
                f"a respect_lease=True finalize call must leave a fresh "
                f"lease completely untouched, got before={lease_before} "
                f"after={lease_after}"
            )
        finally:
            shutil.rmtree(core_test_dir, ignore_errors=True)
