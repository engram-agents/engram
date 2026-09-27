"""Contract tests for engram-alpha#2635 unit 2 — fail closed on WAL/-shm
displacement.

Written BEFORE the implementation, per this repo's contract-first-tests
convention (CLAUDE.md "Contract-first tests — write the assertions before
the implementation"). The invariant under test (from the build contract
posted on #2635, quoted verbatim):

    No tool call reports success on a connection whose -wal/-shm are not
    the on-disk files. When displacement is confirmed, the call fails
    loudly and names the remedy. It never runs, and never returns
    `created`. Reads are refused too (a displaced holder's reads show its
    own private orphan state, which is exactly the lie the walguard
    exists to prevent).

Coverage, matching the spec's own lettering:
  C1  — the harm: a stale long-lived connection's displaced -wal/-shm
        cause a FRESH per-call connection (opened via _get_db() during real
        tool dispatch) to be refused, not silently succeed.
  C1b — discriminating control for C1's fixture: closing the stale
        long-lived connection BEFORE the tool call lifts the refusal —
        proves the gate is keyed on "a stale holder currently exists in
        this process," not "displacement happened at some point."
  C2  — discriminating control against the throttle: refusal fires WELL
        INSIDE the existing 30s _run_walguard_check throttle window.
  C3  — -wal-only displacement (-shm left intact) is refused — this must
        go red against pre-widening (-shm-only) detection and green after.
  C4  — a READ tool call is refused too, not just a write.
  C5  — detection_unavailable is NOT treated as a refusal.
  C6  — the healthy path is unchanged (writes/reads succeed as today).
  C7  — the remedy field is the SAME object/text every other degraded-state
        surface uses (recovery_banner_text()), not a second hand-copied
        string.

Fixture note (judgment call, flagged in the handoff): the "stale long-lived
connection standing in for the server's real long-lived _timing_conn" is
built via `server._get_timing_conn()` itself (the spec's stated preference)
rather than a plain sqlite3.connect() stand-in — it is cleanly reachable
from these tests (module-level function + lock, both accessible off the
imported `server` module), so the "ideally" branch of the spec is taken
for every test in this file rather than only the primary harm case.

These tests exercise the REAL, guard-ON `_get_db()` path — unlike most of
test_walguard_786.py's classes, which set ENGRAM_NO_DB_GUARDS=1 and call
walguard's internal functions directly. conftest.py sets
ENGRAM_NO_DB_GUARDS=1 as a SESSION-WIDE default (os.environ.setdefault), so
setup/teardown here explicitly pop it (and ENGRAM_BOOTSTRAP) for the
duration of each test and restore whatever was there afterward, so later
tests in the same pytest session are unaffected.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import engram_walguard  # noqa: E402

_LINUX_PROC = pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"),
    reason="WAL-guard detection requires /proc/self/fd (Linux only)",
)


def _make_minimal_seeded_db(tmp_dir: Path) -> None:
    """Create a minimal WAL-mode knowledge.db with one seed axiom, closed
    before returning — matches _get_db()'s two fail-loud preconditions (DB
    file exists; at least one axiom row) so the REAL guard-ON _get_db() path
    proceeds past both without bypassing them.
    """
    db_path = tmp_dir / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, claim TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00',
            source_url TEXT, source_title TEXT, source_domain TEXT,
            source_date TEXT, source_accessed TEXT, content_snippet TEXT,
            evidence_id TEXT, quoted_text TEXT, interpretation TEXT, quote_type TEXT,
            predicted_event TEXT, resolution_timeframe TEXT,
            status TEXT DEFAULT 'active', resolved_by TEXT,
            logical_chain TEXT, confidence REAL, confidence_history TEXT DEFAULT '[]',
            supersedes TEXT, superseded_by TEXT, is_current INTEGER DEFAULT 1,
            metadata TEXT DEFAULT '{}', importance_base REAL DEFAULT 0.5,
            importance_score REAL DEFAULT 0.5, recall_turn INTEGER DEFAULT 0,
            recall_count INTEGER DEFAULT 0, memory_status TEXT DEFAULT 'active',
            utility_score REAL DEFAULT 0.0, embedding TEXT
        );
        CREATE TABLE IF NOT EXISTS edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            relation TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00',
            UNIQUE(source_id, target_id, relation)
        );
        CREATE TABLE IF NOT EXISTS sequences (
            node_type TEXT PRIMARY KEY, last_num INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO nodes (id, type, claim, created_at) "
        "VALUES ('ax_0001', 'axiom', 'test axiom', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()


def _independent_process_row_count(db_path: str, claim: str) -> int:
    """Query db_path for rows matching `claim`, from a GENUINELY SEPARATE OS
    process (not merely a fresh in-process connection).

    Required because, empirically, while this process still holds a stale
    fd on a displaced -wal/-shm pair, EVERY connection THIS PROCESS opens to
    the same db is itself doomed — even a bare, pragma-less SELECT raises
    "disk I/O error". A separate process has no such fd and reads cleanly.
    """
    code = (
        "import sqlite3, sys\n"
        f"conn = sqlite3.connect({db_path!r})\n"
        "row = conn.execute(\n"
        "    'SELECT COUNT(*) FROM nodes WHERE claim = ?', (sys.argv[1],)\n"
        ").fetchone()\n"
        "print(row[0])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, claim],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"independent-process row check failed: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return int(result.stdout.strip())


def _fresh_engram_walguard():
    """Re-fetch engram_walguard from sys.modules AT CALL TIME.

    Necessary in a large multi-file test sweep: many OTHER test files in
    this suite reload `engram_*` modules via a broad
    `startswith("engram_")` sys.modules purge in their own fixtures,
    WITHOUT excluding engram_walguard the way this file's own
    TestFailClosedOnDisplacement.setup_method deliberately does. This
    file's top-level `import engram_walguard` is bound ONCE, at collection
    time — if some earlier-running file's fixture deletes and re-imports
    engram_walguard in between, that binding goes stale relative to
    whatever is CURRENTLY in sys.modules["engram_walguard"], while
    mock.patch("engram_walguard.X", ...) always resolves the CURRENT
    sys.modules entry by name (same resolution importlib.import_module
    uses here). Calling through a fresh import keeps a test's own direct
    calls pointed at the SAME object mock.patch is patching. (Measured:
    this exact staleness made test_genuine_displacement_raises_with_
    detection_and_remedy silently NOT raise when run inside a ~124-file
    sweep, while passing in isolation.)
    """
    import importlib
    return importlib.import_module("engram_walguard")


@_LINUX_PROC
class TestFailClosedOnDisplacement:
    """Real, guard-ON _get_db() dispatch through the tool-call surface."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_2635_failclosed_"))
        for key in list(sys.modules):
            if key == "server" or (key.startswith("engram_") and key != "engram_walguard"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        # Guards ON for this class: unset (not merely "not 1") both bypass
        # env vars, overriding conftest.py's session-wide
        # os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1") default. Snapshot
        # so teardown can restore it for later tests in the same session.
        self._saved_no_db_guards = os.environ.pop("ENGRAM_NO_DB_GUARDS", None)
        self._saved_bootstrap = os.environ.pop("ENGRAM_BOOTSTRAP", None)

        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        self.server = server

        _make_minimal_seeded_db(self.tmp)
        self.db_path = str(self.tmp / "knowledge.db")

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._saved_no_db_guards is not None:
            os.environ["ENGRAM_NO_DB_GUARDS"] = self._saved_no_db_guards
        else:
            os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        if self._saved_bootstrap is not None:
            os.environ["ENGRAM_BOOTSTRAP"] = self._saved_bootstrap
        else:
            os.environ.pop("ENGRAM_BOOTSTRAP", None)

    # -- fixture helper -----------------------------------------------------

    def _open_stale_holder(self) -> sqlite3.Connection:
        """Open (and force WAL/shm creation on) the server's real long-lived
        tool-timing connection — the spec's preferred stand-in for "the
        server's real long-lived _timing_conn"."""
        with self.server._timing_conn_lock:
            conn = self.server._get_timing_conn()
        conn.execute(
            "INSERT INTO tool_timing (timestamp, tool_name, duration_ms, status, turn) "
            "VALUES ('2026-01-01T00:00:00', 'warmup', 0, 'success', 0)"
        )
        conn.commit()
        assert os.path.exists(self.db_path + "-shm"), "fixture bug: shm not created"
        assert os.path.exists(self.db_path + "-wal"), "fixture bug: wal not created"
        return conn

    def _displace(self, wal: bool = True, shm: bool = True) -> None:
        if wal:
            os.unlink(self.db_path + "-wal")
        if shm:
            os.unlink(self.db_path + "-shm")

    # -- C1 -------------------------------------------------------------

    def test_c1_stale_holder_causes_fresh_dispatch_to_be_refused(self):
        self._open_stale_holder()
        self._displace(wal=True, shm=True)

        raw = self.server.engram_add_goal(json.dumps({
            "claim": "c1 should never be written",
            "motivation": "2635 C1 harm reproduction",
        }))
        parsed = json.loads(raw)

        assert parsed.get("error") == "substrate_displaced", (
            f"expected a structured refusal on a displaced substrate; got {parsed}"
        )
        assert parsed.get("status") != "created", f"must never return created; got {parsed}"
        assert "reason" in parsed and parsed["reason"], f"missing reason: {parsed}"
        assert "remedy" in parsed and parsed["remedy"], f"missing remedy: {parsed}"

        # Independent fresh PROCESS view (not just a fresh in-process
        # connection): while the stale holder from _open_stale_holder()
        # remains open, EVERY connection in THIS process to this db is
        # itself doomed (measured empirically — even a bare, pragma-less
        # SELECT raises "disk I/O error" as long as the stale fd lives in
        # this process). A genuinely separate OS process is unaffected by
        # this process's stale fd table, so it is the only way to confirm
        # "no new row landed" without tripping the same crash the gate
        # itself exists to convert into a clean refusal.
        count = _independent_process_row_count(self.db_path, "c1 should never be written")
        assert count == 0, "the refused write must not have landed on disk"

    # -- C1b ------------------------------------------------------------

    def test_c1b_closing_the_stale_holder_lifts_the_refusal(self):
        stale = self._open_stale_holder()
        self._displace(wal=True, shm=True)

        # Close the stale holder BEFORE the tool call — the process now
        # holds no fd on the deleted pair.
        stale.close()
        with self.server._timing_conn_lock:
            self.server._timing_conn = None
            self.server._timing_conn_path = None

        raw = self.server.engram_add_goal(json.dumps({
            "claim": "c1b should be written",
            "motivation": "2635 C1b control",
        }))
        parsed = json.loads(raw)

        assert parsed.get("error") is None, (
            f"closing the stale holder before the call must lift the refusal "
            f"(refusal is keyed on 'a stale holder exists in this process now', "
            f"not 'displacement happened at some point'); got {parsed}"
        )
        assert parsed.get("status") == "created", f"expected a normal write; got {parsed}"

    # -- C2 -------------------------------------------------------------

    def test_c2_refused_well_within_the_throttle_window(self):
        # t0: one healthy call — seeds _walguard_last_check via the throttled
        # path (the very first _get_db() call is never itself throttled).
        raw0 = self.server.engram_stats(json.dumps({"sections": ["structure"]}))
        parsed0 = json.loads(raw0)
        assert parsed0.get("error") is None, f"expected a healthy baseline call; got {parsed0}"

        self._open_stale_holder()
        self._displace(wal=True, shm=True)

        # Immediately — well inside the 30s throttle window seeded above.
        raw1 = self.server.engram_add_goal(json.dumps({
            "claim": "c2 should never be written",
            "motivation": "2635 C2 throttle control",
        }))
        parsed1 = json.loads(raw1)
        assert parsed1.get("error") == "substrate_displaced", (
            f"expected refusal well within the 30s throttle window (an "
            f"implementation that reused _run_walguard_check's throttle to "
            f"gate this new check would incorrectly let this call through "
            f"and return created); got {parsed1}"
        )

    # -- C3 -------------------------------------------------------------

    def test_c3_wal_only_displacement_is_refused(self):
        """Must go red against today's shm-only detection; green after widening."""
        self._open_stale_holder()
        self._displace(wal=True, shm=False)
        assert os.path.exists(self.db_path + "-shm"), "fixture bug: shm must stay intact"

        raw = self.server.engram_add_goal(json.dumps({
            "claim": "c3 should never be written",
            "motivation": "2635 C3 wal-only widening",
        }))
        parsed = json.loads(raw)
        assert parsed.get("error") == "substrate_displaced", (
            f"-wal-only displacement must be refused (widened detection); got {parsed}"
        )
        assert parsed.get("reason") == "wal_deleted", f"expected wal_deleted; got {parsed}"

    # -- C4 -------------------------------------------------------------

    def test_c4_read_tool_is_refused_too(self):
        self._open_stale_holder()
        self._displace(wal=True, shm=True)

        raw = self.server.engram_stats(json.dumps({}))
        parsed = json.loads(raw)
        assert parsed.get("error") == "substrate_displaced", (
            f"a read tool call must also be refused when displaced, not just "
            f"a write; got {parsed}"
        )
        assert "node_counts_by_type" not in parsed, (
            f"a refused read must not carry data; got {parsed}"
        )

    # -- C5 -------------------------------------------------------------

    def test_c5_detection_unavailable_is_not_a_refusal(self):
        with mock.patch(
            "engram_walguard.detect_shm_displacement",
            return_value={"reason": "detection_unavailable", "why": "simulated (C5)"},
        ):
            raw = self.server.engram_add_goal(json.dumps({
                "claim": "c5 should be written",
                "motivation": "2635 C5 detection_unavailable control",
            }))
        parsed = json.loads(raw)
        assert parsed.get("error") is None, (
            f"detection_unavailable must NOT be treated as a confirmed "
            f"displacement; got {parsed}"
        )
        assert parsed.get("status") == "created", f"expected a normal write; got {parsed}"

    # -- C6 -------------------------------------------------------------

    def test_c6_healthy_path_unchanged_write_and_read(self):
        raw_w = self.server.engram_add_goal(json.dumps({
            "claim": "c6 healthy write",
            "motivation": "2635 C6 healthy-path control",
        }))
        parsed_w = json.loads(raw_w)
        assert parsed_w.get("error") is None, f"healthy write must succeed; got {parsed_w}"
        assert parsed_w.get("status") == "created", f"expected created; got {parsed_w}"

        raw_r = self.server.engram_stats(json.dumps({}))
        parsed_r = json.loads(raw_r)
        assert parsed_r.get("error") is None, f"healthy read must succeed; got {parsed_r}"
        assert "node_counts_by_type" in parsed_r, f"expected real stats data; got {parsed_r}"

    # -- C7 -------------------------------------------------------------

    def test_c7_remedy_is_the_same_text_as_the_recovery_banner(self):
        self._open_stale_holder()
        self._displace(wal=True, shm=True)

        raw = self.server.engram_add_goal(json.dumps({
            "claim": "c7 should never be written",
            "motivation": "2635 C7 remedy-identity control",
        }))
        parsed = json.loads(raw)
        assert parsed.get("error") == "substrate_displaced", f"expected refusal; got {parsed}"
        # recovery_banner_text() is a pure function of module-level constants
        # (identical content regardless of which engram_walguard module
        # instance holds it), so the file-level import is fine here — but
        # use the same fresh-fetch helper as the class below for consistency.
        assert parsed.get("remedy") == _fresh_engram_walguard().recovery_banner_text(), (
            "remedy must be IDENTICAL (via import) to the shared "
            "recovery_banner_text() every other degraded-state surface uses, "
            "not a second hand-copied string; "
            f"got {parsed.get('remedy')!r}"
        )

    # -- C8 (round-2 blocker) --------------------------------------------

    def test_c8_early_except_path_produces_degraded_marker(self):
        """Round-2 blocker (reviewer-fairy finding on the round-1 PR): the
        early-except block (fired when this process's own stale long-lived
        connection already holds a displaced -wal/-shm pair, so even a
        FRESH connection's own PRAGMA journal_mode=WAL raises) must call
        _run_walguard_check(conn) BEFORE assert_not_displaced()/raise --
        mirroring the main gate's ordering -- so the marker+forensic-capture
        machinery this whole feature exists to produce (#786, #2653) still
        runs for the incident's own primary/motivating scenario (a stale
        _timing_conn-shaped holder), which C1-C7 above already establish is
        the path that actually fires for that scenario, not the main gate.
        """
        wg = _fresh_engram_walguard()
        assert wg.read_degraded_marker(str(self.tmp)) is None, (
            "fixture bug: no marker should exist before the displacing call"
        )

        self._open_stale_holder()
        self._displace(wal=True, shm=True)

        raw = self.server.engram_add_goal(json.dumps({
            "claim": "c8 should never be written",
            "motivation": "2635 round-2 blocker repro",
        }))
        parsed = json.loads(raw)
        assert parsed.get("error") == "substrate_displaced", (
            f"expected the usual refusal; got {parsed}"
        )

        marker = wg.read_degraded_marker(str(self.tmp))
        assert marker is not None, (
            "the early-except path must produce a degraded marker on disk "
            "-- the same diagnostic/postmortem artifact the main gate's own "
            "refusal produces -- via _run_walguard_check(conn) called "
            "before assert_not_displaced()/raise; found None"
        )

    # -- genuine unrelated OperationalError (round-2 suggestion 1a) -------

    def test_genuine_unrelated_operational_error_propagates_unchanged(self):
        """When the early PRAGMA journal_mode=WAL raises for a reason that
        is NOT a confirmed displacement (detect_shm_displacement genuinely
        reports healthy), the ORIGINAL OperationalError must propagate
        unchanged -- never converted into SubstrateDisplaced.

        sqlite3.Connection is an immutable C type -- mock.patch.object can't
        assign to its `execute` attribute directly (raises TypeError). The
        forcing mechanism instead runs the real PRAGMA through a Connection
        SUBCLASS (subclassing works; per-instance attribute assignment on
        the base type does not), installed via sqlite3.connect(factory=...).
        """
        real_connect = sqlite3.connect

        class _RaiseOnPragmaConnection(sqlite3.Connection):
            def execute(self, sql, *a, **kw):
                if sql == "PRAGMA journal_mode=WAL":
                    raise sqlite3.OperationalError(
                        "simulated genuine disk error (not a displacement)"
                    )
                return super().execute(sql, *a, **kw)

        def _fake_connect(db_path, *a, **kw):
            kw.pop("factory", None)
            return real_connect(db_path, *a, factory=_RaiseOnPragmaConnection, **kw)

        with mock.patch("sqlite3.connect", _fake_connect), \
                mock.patch("engram_walguard.detect_shm_displacement", return_value=None):
            with pytest.raises(
                sqlite3.OperationalError,
                match="simulated genuine disk error",
            ):
                self.server._get_db()

    # -- bootstrap_mode skip (round-2 suggestion 1b) ----------------------

    def test_bootstrap_mode_skips_walguard_checks_in_early_except(self):
        """With ENGRAM_BOOTSTRAP=1, the early-except block must not call
        _run_walguard_check() or assert_not_displaced() at all -- confirmed
        by forcing a genuine displacement detection and showing
        detect_shm_displacement is NEVER consulted, even though a version of
        this block that forgot the bootstrap_mode guard would call this a
        confirmed displacement and raise SubstrateDisplaced instead of
        letting the original OperationalError through.

        Same Connection-subclass forcing mechanism as the test above --
        see its docstring for why mock.patch.object on sqlite3.Connection
        itself doesn't work."""
        os.environ["ENGRAM_BOOTSTRAP"] = "1"
        try:
            real_connect = sqlite3.connect

            class _RaiseOnPragmaConnection(sqlite3.Connection):
                def execute(self, sql, *a, **kw):
                    if sql == "PRAGMA journal_mode=WAL":
                        raise sqlite3.OperationalError("simulated (bootstrap mode)")
                    return super().execute(sql, *a, **kw)

            def _fake_connect(db_path, *a, **kw):
                kw.pop("factory", None)
                return real_connect(db_path, *a, factory=_RaiseOnPragmaConnection, **kw)

            with mock.patch("sqlite3.connect", _fake_connect), \
                    mock.patch(
                        "engram_walguard.detect_shm_displacement",
                        return_value={"reason": "shm_deleted", "fd": 7},
                    ) as mocked_detect:
                with pytest.raises(
                    sqlite3.OperationalError,
                    match=r"simulated \(bootstrap mode\)",
                ):
                    self.server._get_db()
            mocked_detect.assert_not_called()
        finally:
            os.environ.pop("ENGRAM_BOOTSTRAP", None)

    # -- main gate independent of the early-except path (round-2 suggestion 2)

    def test_main_gate_fires_independent_of_early_except_path(self):
        """Force a connection through a SUCCESSFUL initial PRAGMA (reaching
        the main gate past the early-except block entirely -- no stale
        holder, no unlinked -wal/-shm), then simulate displacement being
        discovered specifically at the main gate's own later call site by
        mocking detect_shm_displacement to report a genuine displacement.
        Confirms the main gate's own assert_not_displaced() call actually
        fires and refuses, independent of the early-except path that C1-C4
        and C7-C8 above all exercise instead."""
        detection = {"reason": "shm_deleted", "fd": 99}
        with mock.patch(
            "engram_walguard.detect_shm_displacement", return_value=detection
        ):
            raw = self.server.engram_add_goal(json.dumps({
                "claim": "main-gate should never be written",
                "motivation": "2635 main-gate independent-path coverage",
            }))
        parsed = json.loads(raw)
        assert parsed.get("error") == "substrate_displaced", (
            f"expected the main gate's own assert_not_displaced() call to "
            f"fire and refuse, with no stale holder and no real file "
            f"displacement involved at all; got {parsed}"
        )


@_LINUX_PROC
class TestAssertNotDisplacedUnit:
    """Narrower, module-level unit coverage for engram_walguard.assert_not_displaced()
    and SubstrateDisplaced — no server/tool dispatch involved."""

    def test_healthy_returns_quietly(self, tmp_path):
        wg = _fresh_engram_walguard()
        with mock.patch("engram_walguard.detect_shm_displacement", return_value=None):
            wg.assert_not_displaced(str(tmp_path / "knowledge.db"))  # must not raise

    def test_detection_unavailable_returns_quietly(self, tmp_path):
        wg = _fresh_engram_walguard()
        with mock.patch(
            "engram_walguard.detect_shm_displacement",
            return_value={"reason": "detection_unavailable", "why": "x"},
        ):
            wg.assert_not_displaced(str(tmp_path / "knowledge.db"))  # must not raise

    def test_genuine_displacement_raises_with_detection_and_remedy(self, tmp_path):
        wg = _fresh_engram_walguard()
        detection = {"reason": "shm_deleted", "fd": 7}
        with mock.patch(
            "engram_walguard.detect_shm_displacement", return_value=detection
        ):
            with pytest.raises(wg.SubstrateDisplaced) as excinfo:
                wg.assert_not_displaced(str(tmp_path / "knowledge.db"))
        assert excinfo.value.detection == detection
        assert excinfo.value.remedy == wg.recovery_banner_text()


class TestDetectDisplacementWalWidening:
    """Unit coverage for the -wal widening of detect_shm_displacement()."""

    @_LINUX_PROC
    def test_wal_deleted_returns_wal_deleted_reason(self, tmp_path):
        db_path = str(tmp_path / "knowledge.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'hello')")
        conn.commit()
        try:
            wal = db_path + "-wal"
            if not os.path.exists(wal):
                pytest.skip("wal file not created (WAL not active on this build)")
            os.unlink(wal)
            result = engram_walguard.detect_shm_displacement(db_path)
            assert result is not None, "expected displacement detection after wal deletion"
            assert result["reason"] == "wal_deleted", f"expected wal_deleted; got {result}"
            assert "fd" in result
        finally:
            conn.close()

    @_LINUX_PROC
    def test_shm_intact_wal_deleted_still_detected(self, tmp_path):
        """Today's (pre-widening) shm-only detection would call this healthy —
        the regression this widening exists to close."""
        db_path = str(tmp_path / "knowledge.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'hello')")
        conn.commit()
        try:
            shm = db_path + "-shm"
            wal = db_path + "-wal"
            if not os.path.exists(shm) or not os.path.exists(wal):
                pytest.skip("shm/wal not created (WAL not active on this build)")
            os.unlink(wal)
            assert os.path.exists(shm), "fixture bug: shm must stay intact"
            result = engram_walguard.detect_shm_displacement(db_path)
            assert result is not None and result.get("reason") == "wal_deleted", (
                f"expected wal_deleted even though shm is healthy; got {result}"
            )
        finally:
            conn.close()
