"""Tests for the WAL/shm self-guard (#786).

Coverage (7 cases):

  1. Detection happy-path: open a WAL-mode db, write a row (forces -shm/-wal
     creation), call detect → None (healthy).

  2. Detection on deletion: same setup, then os.unlink(<db>-shm) while the
     connection is open → detect returns reason "shm_deleted".
     Guarded by skipif for non-Linux / /proc absent.

  3. Detection on replacement: delete + create a new file at the -shm path
     (different inode) → reason "shm_replaced".

  4. Marker write/read/clear roundtrip + atomicity (tmp file absent after write).

  5. skip_checkpoint honored: with a degraded marker present in a scratch
     DATA_DIR, _commit_snapshot runs the dump but does NOT checkpoint (assert
     wal file size unchanged / non-truncated; assert dump file written).

  6. Startup clearing: stale marker + healthy db → cleared at first
     _get_db-equivalent init; corrupt-state simulation (marker + still-displaced)
     → NOT cleared.

  7. Guard never raises: monkeypatch detect to throw →
     _run_walguard_check swallows + logs.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import engram_walguard  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LINUX_PROC = pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"),
    reason="WAL-guard detection requires /proc/self/fd (Linux only)",
)


def _open_wal_db(path: str) -> sqlite3.Connection:
    """Open a WAL-mode DB and force shm/wal creation with a write."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'hello')")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1. Detection happy-path
# ---------------------------------------------------------------------------

class TestDetectHealthy:
    """detect_shm_displacement returns None when shm inode matches."""

    @_LINUX_PROC
    def test_healthy_returns_none(self, tmp_path):
        db_path = str(tmp_path / "knowledge.db")
        conn = _open_wal_db(db_path)
        try:
            # shm must have been created by the write above
            shm = db_path + "-shm"
            if not os.path.exists(shm):
                pytest.skip("shm file not created (WAL not active on this build)")
            result = engram_walguard.detect_shm_displacement(db_path)
            assert result is None, f"Expected None for healthy db; got {result}"
        finally:
            conn.close()

    def test_no_proc_returns_none(self, tmp_path, monkeypatch):
        """When /proc/self/fd is absent, detect_shm_displacement returns None."""
        db_path = str(tmp_path / "knowledge.db")
        conn = _open_wal_db(db_path)
        try:
            monkeypatch.setattr(os.path, "isdir", lambda p: False)
            result = engram_walguard.detect_shm_displacement(db_path)
            assert result is None
        finally:
            conn.close()

    def test_no_shm_fd_held_returns_none(self, tmp_path):
        """When this process holds no shm fd, returns None (cannot judge)."""
        db_path = str(tmp_path / "other.db")
        # Use a db path we know no fd is open for
        result = engram_walguard.detect_shm_displacement(db_path)
        assert result is None


# ---------------------------------------------------------------------------
# 2. Detection on deletion
# ---------------------------------------------------------------------------

class TestDetectDeleted:
    """detect_shm_displacement returns {"reason": "shm_deleted"} when shm is removed."""

    @_LINUX_PROC
    def test_shm_deleted_returns_reason(self, tmp_path):
        db_path = str(tmp_path / "knowledge.db")
        conn = _open_wal_db(db_path)
        try:
            shm = db_path + "-shm"
            if not os.path.exists(shm):
                pytest.skip("shm file not created (WAL not active on this build)")
            # Delete shm while connection is open — the fd stays in /proc as "(deleted)"
            os.unlink(shm)
            result = engram_walguard.detect_shm_displacement(db_path)
            assert result is not None, "Expected displacement detection after shm deletion"
            assert result["reason"] == "shm_deleted", f"Expected shm_deleted; got {result}"
            assert "fd" in result
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 3. Detection on replacement
# ---------------------------------------------------------------------------

class TestDetectReplaced:
    """detect_shm_displacement returns {"reason": "shm_replaced"} when shm has a new inode."""

    @_LINUX_PROC
    def test_shm_replaced_returns_reason(self, tmp_path):
        """Test shm_replaced via hardlink trick: fd keeps a valid (non-deleted) path
        to the original inode, while a new file is placed at the canonical shm path.

        Scenario: open a WAL db (fd points to shm at canonical path).  Create a
        hardlink to the shm at an alternate path (fd_inode still live).  Unlink the
        canonical shm path and create a new file there (different inode).  The fd
        still resolves cleanly (no ' (deleted)' suffix) because the alternate hardlink
        keeps the original inode alive — so detect sees inode mismatch → shm_replaced.
        """
        db_path = str(tmp_path / "knowledge.db")
        conn = _open_wal_db(db_path)
        try:
            shm = db_path + "-shm"
            if not os.path.exists(shm):
                pytest.skip("shm file not created (WAL not active on this build)")

            # Find which fd the process holds for the shm
            shm_fd = None
            for entry in os.listdir("/proc/self/fd"):
                try:
                    if not entry.isdigit():
                        continue
                    target = os.readlink(f"/proc/self/fd/{entry}")
                    if target == shm or target.startswith(shm + " "):
                        shm_fd = int(entry)
                        break
                except OSError:
                    continue
            if shm_fd is None:
                pytest.skip("no fd held for shm (connection may not have opened WAL yet)")

            # Create a hardlink to the shm at an alternate path
            alt_path = shm + ".hardlink"
            os.link(shm, alt_path)

            # The fd in /proc now points to the first path it had, but the
            # hardlink keeps the inode alive.  Unfortunately, after os.link the
            # /proc entry still shows the original canonical path (not the
            # hardlink).  When we then unlink the canonical path the original
            # inode stays alive via the hardlink, but /proc shows " (deleted)".
            # To avoid the " (deleted)" we need to use a symlink approach or
            # directly verify inode mismatch via stat without the fd path.

            # Simplest approach: exercise the stat-path directly.
            # Since we can't easily make /proc show a non-deleted live path
            # on Linux (unlink always marks the fd as deleted even with a
            # surviving hardlink), we test the shm_replaced path by monkeypatching
            # the readlink call to simulate a non-deleted fd pointing at a different
            # inode file.
            os.unlink(alt_path)

            # Monkeypatch os.readlink so the fd appears to point to a non-deleted path.
            # Then create a new file at shm_path so inode comparison sees a difference.
            original_readlink = os.readlink
            original_stat = os.stat

            # Save original shm inode (held by fd)
            fd_link = f"/proc/self/fd/{shm_fd}"
            # The fd is showing " (deleted)" now (since shm was already unlinked
            # by the replacement steps above).  We need to test shm_replaced
            # via a clean approach: create a new db, then use a direct inode
            # mismatch by patching the readlink to not show " (deleted)".

            # Write a new file at the shm path (different inode)
            with open(shm, "wb") as f:
                f.write(b"\x00" * 32768)
            new_inode = os.stat(shm).st_ino

            # Patch readlink so fd appears to point to a non-deleted shm with
            # a different inode than the disk file.  We simulate by making
            # readlink return the path WITHOUT " (deleted)" so the inode-compare
            # path triggers.
            #
            # Use a secondary fd that points to the new shm to get a "real" fd
            # pointing to a file that has yet a third inode (we open the new
            # shm, compare its inode against a third file at the shm path).
            fake_inode_file = tmp_path / "fake_old_shm"
            fake_inode_file.write_bytes(b"\x01" * 100)
            fake_inode = fake_inode_file.stat().st_ino
            assert fake_inode != new_inode, "Need distinct inodes for the test"

            def patched_readlink(path):
                if f"/proc/self/fd/{shm_fd}" in path:
                    # Simulate a non-deleted fd pointing at the fake old inode
                    return shm
                return original_readlink(path)

            def patched_stat(path, *args, **kwargs):
                if path == f"/proc/self/fd/{shm_fd}":
                    # Return stat of fake_old_shm (the "old inode" the fd holds)
                    return original_stat(str(fake_inode_file), *args, **kwargs)
                return original_stat(path, *args, **kwargs)

            import unittest.mock as _mock
            with _mock.patch("os.readlink", side_effect=patched_readlink), \
                 _mock.patch("os.stat", side_effect=patched_stat):
                result = engram_walguard.detect_shm_displacement(db_path)

            assert result is not None, "Expected displacement detection after shm replacement"
            assert result["reason"] == "shm_replaced", f"Expected shm_replaced; got {result}"
            assert "fd_inode" in result
            assert "disk_inode" in result
            assert result["fd_inode"] != result["disk_inode"]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 4. Marker write/read/clear roundtrip
# ---------------------------------------------------------------------------

class TestMarkerRoundtrip:
    """write_degraded_marker / read_degraded_marker / clear_degraded_marker roundtrip."""

    def test_write_read_clear(self, tmp_path):
        data_dir = str(tmp_path)
        detection = {"reason": "shm_deleted", "fd": 42}
        dump_info = {"git_committed": True, "commit_sha": "abc123"}

        # Write
        marker_path = engram_walguard.write_degraded_marker(data_dir, detection, dump_info)
        assert os.path.exists(marker_path)

        # Read back
        marker = engram_walguard.read_degraded_marker(data_dir)
        assert marker is not None
        assert marker["reason"] == "shm_deleted"
        assert marker["dump_committed"] is True
        assert marker["dump_sha"] == "abc123"
        assert "detected_at" in marker
        assert "pid" in marker
        assert marker["pid"] == os.getpid()
        assert "details" in marker

    def test_write_atomicity(self, tmp_path):
        """The tmp file used for atomic write must be absent after write completes."""
        data_dir = str(tmp_path)
        detection = {"reason": "shm_replaced", "fd": 7, "fd_inode": 100, "disk_inode": 200}
        engram_walguard.write_degraded_marker(data_dir, detection)
        # No .substrate-degraded-*.tmp files should remain
        tmp_files = list(tmp_path.glob(".substrate-degraded-*.tmp"))
        assert tmp_files == [], f"Leftover tmp files: {tmp_files}"

    def test_read_absent_returns_none(self, tmp_path):
        result = engram_walguard.read_degraded_marker(str(tmp_path))
        assert result is None

    def test_clear_returns_true_when_present(self, tmp_path):
        data_dir = str(tmp_path)
        engram_walguard.write_degraded_marker(data_dir, {"reason": "shm_deleted", "fd": 1})
        assert engram_walguard.clear_degraded_marker(data_dir) is True
        assert engram_walguard.read_degraded_marker(data_dir) is None

    def test_clear_returns_false_when_absent(self, tmp_path):
        assert engram_walguard.clear_degraded_marker(str(tmp_path)) is False

    def test_write_no_dump_info(self, tmp_path):
        """write_degraded_marker works with dump_info=None."""
        data_dir = str(tmp_path)
        engram_walguard.write_degraded_marker(data_dir, {"reason": "shm_deleted", "fd": 3})
        marker = engram_walguard.read_degraded_marker(data_dir)
        assert marker is not None
        assert marker["dump_committed"] is False
        assert marker["dump_sha"] is None


# ---------------------------------------------------------------------------
# 5. skip_checkpoint honored
# ---------------------------------------------------------------------------

class TestSkipCheckpoint:
    """With a degraded marker present, _commit_snapshot skips the WAL checkpoint."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_walguard_"))
        # Fresh server module import
        for key in list(sys.modules):
            if key == "server" or (key.startswith("engram_") and key != "engram_walguard"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        # Do NOT set ENGRAM_DISABLE_GIT — we need the git path reached so the
        # dump block executes.  We'll set _git_available=True + mock _git calls.
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        # Bypass real git: _git_available=True, mock _git to return plausible results
        server.core._git_available = True
        self.server = server

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_minimal_db(self) -> sqlite3.Connection:
        """Bootstrap a full-schema DB via server._get_db() and return the connection.

        Uses server._get_db() to create all tables (needed for _generate_snapshot).
        Inserts a seed axiom so the empty-graph guard is satisfied.
        """
        db_path = self.tmp / "knowledge.db"
        # Pre-create a minimal schema so _get_db doesn't fail on empty-graph guard
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        # Insert placeholder so _get_db() seed guard passes
        conn.executescript("""
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
        """)
        conn.execute(
            "INSERT OR IGNORE INTO nodes (id, type, claim, created_at) "
            "VALUES ('ax_0001', 'axiom', 'test axiom', '2026-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()
        # Now open via server._get_db() to get full schema + correct row_factory
        return self.server._get_db()

    def _make_fake_git_result(self, returncode=0, stdout="", stderr=""):
        """Return a subprocess.CompletedProcess-like object for mocking _git."""
        r = mock.MagicMock()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def test_skip_checkpoint_when_degraded_marker_present(self):
        """Degraded marker forces skip_checkpoint=True: wal file not truncated, dump written."""
        conn = self._make_minimal_db()
        wal_path = self.tmp / "knowledge.db-wal"

        # Write a degraded marker
        engram_walguard.write_degraded_marker(
            str(self.tmp),
            {"reason": "shm_deleted", "fd": 1},
        )

        conn.execute("INSERT OR IGNORE INTO nodes (id, type, claim, created_at) "
                     "VALUES ('ob_test', 'observation', 'test', '2026-01-01T00:00:00')")
        conn.commit()

        wal_size_before = wal_path.stat().st_size if wal_path.exists() else 0

        # Mock _git so commit/add/status/push appear to succeed without real git
        def _mock_git(*args, **kwargs):
            if args and args[0] == "check-ignore":
                return self._make_fake_git_result(returncode=1)  # none ignored
            if args and args[0] == "status":
                return self._make_fake_git_result(returncode=0, stdout="M knowledge.sql")
            if args and args[0] == "commit":
                return self._make_fake_git_result(returncode=0)
            if args and args[0] == "rev-parse":
                return self._make_fake_git_result(returncode=0, stdout="abc123fake")
            if args and args[0] == "push":
                return self._make_fake_git_result(returncode=0)
            return self._make_fake_git_result(returncode=0)

        with mock.patch.object(self.server.core, "_git", side_effect=_mock_git):
            result = self.server._commit_snapshot(conn, "test snapshot", "test")
        conn.close()

        # WAL should not be truncated (TRUNCATE checkpoint would make it 0 bytes)
        if wal_path.exists():
            wal_size_after = wal_path.stat().st_size
            assert wal_size_after >= wal_size_before, (
                f"WAL was truncated ({wal_size_before} → {wal_size_after}); "
                f"skip_checkpoint was not honored"
            )

        # SQL dump file should have been written
        sql_path = self.tmp / "knowledge.sql"
        assert sql_path.exists(), f"knowledge.sql dump was not written; result={result}"
        assert sql_path.stat().st_size > 0, "knowledge.sql dump is empty"

    def test_skip_checkpoint_explicit_parameter(self):
        """skip_checkpoint=True passed explicitly also skips the checkpoint."""
        conn = self._make_minimal_db()
        wal_path = self.tmp / "knowledge.db-wal"

        conn.execute("INSERT OR IGNORE INTO nodes (id, type, claim, created_at) "
                     "VALUES ('ob_test2', 'observation', 'test2', '2026-01-01T00:00:00')")
        conn.commit()

        wal_size_before = wal_path.stat().st_size if wal_path.exists() else 0

        def _mock_git(*args, **kwargs):
            if args and args[0] == "check-ignore":
                return self._make_fake_git_result(returncode=1)
            if args and args[0] == "status":
                return self._make_fake_git_result(returncode=0, stdout="M knowledge.sql")
            if args and args[0] == "commit":
                return self._make_fake_git_result(returncode=0)
            if args and args[0] == "rev-parse":
                return self._make_fake_git_result(returncode=0, stdout="def456fake")
            if args and args[0] == "push":
                return self._make_fake_git_result(returncode=0)
            return self._make_fake_git_result(returncode=0)

        with mock.patch.object(self.server.core, "_git", side_effect=_mock_git):
            result = self.server._commit_snapshot(
                conn, "emergency test", "emergency", skip_checkpoint=True
            )
        conn.close()

        if wal_path.exists():
            wal_size_after = wal_path.stat().st_size
            assert wal_size_after >= wal_size_before, (
                f"WAL truncated despite skip_checkpoint=True "
                f"({wal_size_before} → {wal_size_after})"
            )

        sql_path = self.tmp / "knowledge.sql"
        assert sql_path.exists(), f"knowledge.sql not written; result={result}"


# ---------------------------------------------------------------------------
# 6. Startup clearing
# ---------------------------------------------------------------------------

class TestStartupClearing:
    """_walguard_startup_clear clears stale markers on healthy DB; leaves on displaced."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_walguard_startup_"))
        for key in list(sys.modules):
            if key == "server" or (key.startswith("engram_") and key != "engram_walguard"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        self.server = server

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_healthy_db(self) -> sqlite3.Connection:
        """Create a minimal DB and return an open connection."""
        db_path = self.tmp / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS nodes ("
            "id TEXT PRIMARY KEY, type TEXT NOT NULL, claim TEXT, "
            "created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00', "
            "is_current INTEGER DEFAULT 1, confidence REAL, "
            "importance_base REAL DEFAULT 0.5, importance_score REAL DEFAULT 0.5, "
            "recall_turn INTEGER DEFAULT 0, recall_count INTEGER DEFAULT 0, "
            "memory_status TEXT DEFAULT 'active', utility_score REAL DEFAULT 0.0, "
            "metadata TEXT DEFAULT '{}', embedding TEXT)"
        )
        conn.commit()
        return conn

    def test_stale_marker_cleared_on_healthy_restart(self):
        """Stale degraded marker is removed when detect returns None and integrity_check passes."""
        conn = self._make_healthy_db()
        try:
            # Write a stale marker
            engram_walguard.write_degraded_marker(
                str(self.tmp), {"reason": "shm_deleted", "fd": 99}
            )
            assert engram_walguard.read_degraded_marker(str(self.tmp)) is not None

            # Call startup clear with healthy state
            with mock.patch("engram_walguard.detect_shm_displacement", return_value=None):
                self.server._walguard_startup_clear(conn)

            # Marker should be gone
            assert engram_walguard.read_degraded_marker(str(self.tmp)) is None, (
                "Degraded marker should have been cleared after healthy restart"
            )
        finally:
            conn.close()

    def test_displaced_marker_not_cleared(self):
        """Degraded marker is NOT cleared when detect still shows displacement."""
        conn = self._make_healthy_db()
        try:
            engram_walguard.write_degraded_marker(
                str(self.tmp), {"reason": "shm_replaced", "fd": 5, "fd_inode": 1, "disk_inode": 2}
            )

            with mock.patch(
                "engram_walguard.detect_shm_displacement",
                return_value={"reason": "shm_replaced", "fd": 5, "fd_inode": 1, "disk_inode": 2},
            ):
                self.server._walguard_startup_clear(conn)

            assert engram_walguard.read_degraded_marker(str(self.tmp)) is not None, (
                "Degraded marker must NOT be cleared when displacement is still present"
            )
        finally:
            conn.close()

    def test_marker_not_cleared_when_integrity_check_fails(self):
        """Degraded marker is NOT cleared when integrity_check returns a failure row.

        We simulate the failing integrity_check by patching the _walguard_startup_clear
        function's inner PRAGMA call path: since sqlite3.Connection.execute is
        C-extension read-only, we wrap the connection in a proxy object and pass
        that to _walguard_startup_clear directly.
        """
        conn = self._make_healthy_db()
        try:
            engram_walguard.write_degraded_marker(
                str(self.tmp), {"reason": "shm_deleted", "fd": 3}
            )

            # Build a proxy that intercepts the integrity_check PRAGMA and
            # returns a failure result.  For all other SQL, delegate to the
            # real connection.
            class _IntegrityFailProxy:
                def __init__(self, real):
                    self._real = real

                def execute(self, sql, *args, **kwargs):
                    if "integrity_check" in sql:
                        r = mock.MagicMock()
                        r.fetchall.return_value = [("*** in page 1: malformed",)]
                        return r
                    return self._real.execute(sql, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(self._real, name)

            proxy = _IntegrityFailProxy(conn)

            with mock.patch("engram_walguard.detect_shm_displacement", return_value=None):
                self.server._walguard_startup_clear(proxy)

            # Marker should still exist
            assert engram_walguard.read_degraded_marker(str(self.tmp)) is not None, (
                "Degraded marker must NOT be cleared when integrity_check fails"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 7. Guard never raises
# ---------------------------------------------------------------------------

class TestGuardNeverRaises:
    """_run_walguard_check swallows errors from the detector."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_walguard_guard_"))
        for key in list(sys.modules):
            if key == "server" or (key.startswith("engram_") and key != "engram_walguard"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        self.server = server

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_raising_detector_swallowed(self):
        """If detect_shm_displacement raises, _run_walguard_check does not propagate."""
        db_path = self.tmp / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
        conn.commit()

        # Force the throttle timer to zero so the check runs
        self.server.core._walguard_last_check = 0.0

        with mock.patch(
            "engram_walguard.detect_shm_displacement",
            side_effect=RuntimeError("simulated detector crash"),
        ):
            # Must not raise
            try:
                self.server._run_walguard_check(conn)
            except Exception as e:
                pytest.fail(f"_run_walguard_check raised unexpectedly: {e!r}")
        conn.close()

    def test_throttle_prevents_repeated_calls(self):
        """With a recent timestamp, the check is skipped (no actual work done)."""
        db_path = self.tmp / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
        conn.commit()

        # Set last_check to now — throttle should fire
        import time
        self.server.core._walguard_last_check = time.monotonic()

        call_count = {"n": 0}

        def counting_detect(path):
            call_count["n"] += 1
            return None

        with mock.patch("engram_walguard.detect_shm_displacement", side_effect=counting_detect):
            self.server._run_walguard_check(conn)

        # Should NOT have called detect (throttled)
        assert call_count["n"] == 0, (
            f"detect was called {call_count['n']} time(s) despite throttle"
        )
        conn.close()


# ---------------------------------------------------------------------------
# 8. Banner placement regression — _walguard_warning inside JSON, not prepended
# ---------------------------------------------------------------------------

class TestBannerPlacement:
    """Degraded-state banner is injected as '_walguard_warning' inside the JSON
    response dict, not prepended outside it.

    Regression test for the round-1 blocker: the old prepend behaviour produced
    invalid JSON (e.g. "⚠️ SUBSTRATE DEGRADED: ...\n{...}") that broke every
    Python caller relying on json.loads() of the tool return value.

    Each test:
      1. Writes a degraded marker into the isolated DATA_DIR.
      2. Calls the affected tool via the MCP entry-point function.
      3. Asserts json.loads(result) SUCCEEDS (i.e. result is valid JSON).
      4. Asserts the parsed dict contains '_walguard_warning' with the expected
         text fragment.

    A helper also verifies the old prepend behaviour FAILS json.loads so the
    test provably discriminates old from new.
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_walguard_banner_"))
        for key in list(sys.modules):
            if key == "server" or (key.startswith("engram_") and key != "engram_walguard"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        server.core._git_available = True
        self.server = server

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_minimal_db(self) -> sqlite3.Connection:
        """Bootstrap a minimal DB suitable for stats/diagnose/surface/nap calls."""
        db_path = self.tmp / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
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
        """)
        conn.execute(
            "INSERT OR IGNORE INTO nodes (id, type, claim, created_at) "
            "VALUES ('ax_0001', 'axiom', 'test axiom', '2026-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()
        return self.server._get_db()

    def _write_degraded_marker(self):
        """Write a degraded marker into the isolated DATA_DIR."""
        engram_walguard.write_degraded_marker(
            str(self.tmp), {"reason": "shm_deleted", "fd": 42}
        )

    def _make_fake_git_result(self, returncode=0, stdout="", stderr=""):
        r = mock.MagicMock()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def _mock_git(self, *args, **kwargs):
        if args and args[0] == "check-ignore":
            return self._make_fake_git_result(returncode=1)
        if args and args[0] == "status":
            return self._make_fake_git_result(returncode=0, stdout="M knowledge.sql")
        if args and args[0] == "commit":
            return self._make_fake_git_result(returncode=0)
        if args and args[0] == "rev-parse":
            return self._make_fake_git_result(returncode=0, stdout="abc123fake")
        if args and args[0] == "push":
            return self._make_fake_git_result(returncode=0)
        return self._make_fake_git_result(returncode=0)

    def test_engram_stats_warning_in_json(self):
        """engram_stats with degraded marker: result is valid JSON with _walguard_warning."""
        import json as _json
        self._make_minimal_db()
        self._write_degraded_marker()

        raw = self.server.engram_stats("{}")
        parsed = _json.loads(raw)  # must not raise JSONDecodeError
        assert "_walguard_warning" in parsed, (
            f"_walguard_warning key missing from engram_stats response; keys={list(parsed)}"
        )
        assert "SUBSTRATE DEGRADED" in parsed["_walguard_warning"], (
            f"Unexpected warning text: {parsed['_walguard_warning']!r}"
        )

    def test_engram_diagnose_warning_in_json(self):
        """engram_diagnose with degraded marker: result is valid JSON with _walguard_warning."""
        import json as _json
        self._make_minimal_db()
        self._write_degraded_marker()

        raw = self.server.engram_diagnose()
        parsed = _json.loads(raw)  # must not raise JSONDecodeError
        assert "_walguard_warning" in parsed, (
            f"_walguard_warning key missing from engram_diagnose response; keys={list(parsed)}"
        )
        assert "SUBSTRATE DEGRADED" in parsed["_walguard_warning"], (
            f"Unexpected warning text: {parsed['_walguard_warning']!r}"
        )

    def test_engram_surface_warning_in_json(self):
        """engram_surface with degraded marker: result is valid JSON with _walguard_warning."""
        import json as _json
        self._make_minimal_db()
        self._write_degraded_marker()

        raw = self.server.engram_surface('{"query": "test axiom"}')
        parsed = _json.loads(raw)  # must not raise JSONDecodeError
        assert "_walguard_warning" in parsed, (
            f"_walguard_warning key missing from engram_surface response; keys={list(parsed)}"
        )
        assert "SUBSTRATE DEGRADED" in parsed["_walguard_warning"], (
            f"Unexpected warning text: {parsed['_walguard_warning']!r}"
        )

    def test_engram_nap_warning_in_json(self):
        """engram_nap with degraded marker: result is valid JSON with _walguard_warning."""
        import json as _json
        self._make_minimal_db()
        self._write_degraded_marker()

        with mock.patch.object(self.server.core, "_git", side_effect=self._mock_git):
            raw = self.server.engram_nap('{"message": "banner test nap"}')
        parsed = _json.loads(raw)  # must not raise JSONDecodeError
        assert "_walguard_warning" in parsed, (
            f"_walguard_warning key missing from engram_nap response; keys={list(parsed)}"
        )
        assert "SUBSTRATE DEGRADED" in parsed["_walguard_warning"], (
            f"Unexpected warning text: {parsed['_walguard_warning']!r}"
        )

    def test_engram_advance_turn_warning_in_json(self):
        """engram_advance_turn with degraded marker: result is valid JSON with _walguard_warning."""
        import json as _json
        self._make_minimal_db()
        self._write_degraded_marker()

        with mock.patch.object(self.server.core, "_git", side_effect=self._mock_git):
            raw = self.server.engram_advance_turn('{"message": "banner test advance"}')
        parsed = _json.loads(raw)  # must not raise JSONDecodeError
        assert "_walguard_warning" in parsed, (
            f"_walguard_warning key missing from engram_advance_turn response; keys={list(parsed)}"
        )
        assert "SUBSTRATE DEGRADED" in parsed["_walguard_warning"], (
            f"Unexpected warning text: {parsed['_walguard_warning']!r}"
        )

    def test_no_warning_when_no_marker(self):
        """Without a degraded marker, _walguard_warning key is absent from engram_stats."""
        import json as _json
        self._make_minimal_db()
        # No marker written

        raw = self.server.engram_stats("{}")
        parsed = _json.loads(raw)
        assert "_walguard_warning" not in parsed, (
            f"_walguard_warning should be absent when no degraded marker is present; "
            f"got: {parsed.get('_walguard_warning')!r}"
        )

    def test_old_prepend_behaviour_is_invalid_json(self):
        """Validates the discriminating property: the old prepend produced invalid JSON.

        This test demonstrates why the fix was necessary: prepending a non-JSON
        banner string to a JSON object string produces a string that is NOT valid
        JSON.  If this test fails (i.e. the prepended form IS valid JSON), the
        regression test loses its discriminating power.
        """
        import json as _json
        banner = (
            "⚠️ SUBSTRATE DEGRADED: WAL-index displaced at 2026-06-03T00:00:00+00:00; "
            "emergency dump abc123; "
            "do not hard-kill the session — request a clean MCP restart. (#786)\n"
        )
        payload = '{"status": "ok"}'
        prepended = banner + payload
        try:
            _json.loads(prepended)
            pytest.fail(
                "Old prepend behaviour unexpectedly produced valid JSON — "
                "the regression test's discriminating property no longer holds."
            )
        except _json.JSONDecodeError:
            pass  # expected: prepend produces invalid JSON — this is the bug the fix addresses


# ---------------------------------------------------------------------------
# 9. Storm-gate regression — no repeated dumps under persistent displacement
# ---------------------------------------------------------------------------

class TestStormGate:
    """_run_walguard_check gates the expensive dump path on the existing marker.

    With a committed marker already present:
      - Double-call with a fresh marker → ZERO additional dumps/commits.
      - Double-call with a stale marker (last_emergency_dump >1 h ago) → exactly
        ONE re-dump fires and detected_at is preserved.
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_walguard_storm_"))
        for key in list(sys.modules):
            if key == "server" or (key.startswith("engram_") and key != "engram_walguard"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        self.server = server

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_conn(self) -> sqlite3.Connection:
        db_path = self.tmp / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
        conn.commit()
        return conn

    def _force_throttle_expired(self):
        """Reset the throttle so the next _run_walguard_check actually runs."""
        self.server.core._walguard_last_check = 0.0

    def test_no_redump_when_committed_marker_is_fresh(self):
        """Two calls with a fresh committed marker → zero additional dumps/commits.

        Scenario:
          1. Write a committed marker with last_emergency_dump = now.
          2. Call _run_walguard_check twice (throttle reset between calls).
          3. Assert _commit_snapshot was NOT called (dump count = 0).
          4. Assert detected_at is unchanged across both calls.
        """
        conn = self._make_conn()
        try:
            from datetime import datetime, timezone
            import time

            detection = {"reason": "shm_deleted", "fd": 5}
            original_detected_at = "2026-06-03T10:00:00+00:00"

            # Write a fresh committed marker (last_emergency_dump = now)
            engram_walguard.write_degraded_marker(
                str(self.tmp),
                detection,
                dump_info={"git_committed": True, "commit_sha": "abc123"},
                detected_at=original_detected_at,
            )

            dump_call_count = {"n": 0}

            def counting_commit_snapshot(*args, **kwargs):
                dump_call_count["n"] += 1
                return {"git_committed": True, "commit_sha": "should_not_happen"}

            # Patch: detect returns displacement, _commit_snapshot counts calls
            with mock.patch(
                "engram_walguard.detect_shm_displacement",
                return_value={"reason": "shm_deleted", "fd": 5},
            ), mock.patch.object(
                self.server.core, "_commit_snapshot", side_effect=counting_commit_snapshot
            ):
                # First call
                self._force_throttle_expired()
                self.server._run_walguard_check(conn)

                # Second call
                self._force_throttle_expired()
                self.server._run_walguard_check(conn)

            assert dump_call_count["n"] == 0, (
                f"Expected 0 dumps with a fresh committed marker; "
                f"got {dump_call_count['n']}"
            )

            # detected_at must be unchanged
            marker = engram_walguard.read_degraded_marker(str(self.tmp))
            assert marker is not None
            assert marker["detected_at"] == original_detected_at, (
                f"detected_at was clobbered: expected {original_detected_at!r}, "
                f"got {marker['detected_at']!r}"
            )
        finally:
            conn.close()

    def test_hourly_refresh_fires_once_and_preserves_detected_at(self):
        """Stale committed marker (last_emergency_dump >1 h old) triggers exactly
        one re-dump, and detected_at is preserved after the refresh.

        Scenario:
          1. Write a committed marker with last_emergency_dump = 2 hours ago.
          2. Call _run_walguard_check → exactly ONE dump fires.
          3. Call again (fresh marker after refresh) → NO second dump.
          4. Assert detected_at is unchanged across all calls.
        """
        from datetime import datetime, timezone, timedelta

        conn = self._make_conn()
        try:
            detection = {"reason": "shm_deleted", "fd": 7}
            original_detected_at = "2026-06-03T08:00:00+00:00"

            # Craft a marker whose last_emergency_dump is 2 hours ago (stale)
            two_hours_ago = (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat()

            # Write the marker directly with a stale last_emergency_dump.
            # We build the payload manually and write it atomically to simulate
            # a marker that was written 2 hours ago.
            import json as _json
            import tempfile as _tempfile
            stale_payload = {
                "reason": "shm_deleted",
                "detected_at": original_detected_at,
                "pid": os.getpid(),
                "dump_committed": True,
                "dump_sha": "stalesha",
                "last_emergency_dump": two_hours_ago,
                "details": {**detection},
            }
            marker_path = self.tmp / ".substrate-degraded.json"
            fd, tmp_path = _tempfile.mkstemp(
                suffix=".tmp", dir=str(self.tmp), prefix=".substrate-degraded-"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                _json.dump(stale_payload, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, str(marker_path))

            dump_call_count = {"n": 0}
            new_sha = "refreshsha"

            def counting_commit_snapshot(*args, **kwargs):
                dump_call_count["n"] += 1
                return {"git_committed": True, "commit_sha": new_sha}

            with mock.patch(
                "engram_walguard.detect_shm_displacement",
                return_value={"reason": "shm_deleted", "fd": 7},
            ), mock.patch.object(
                self.server.core, "_commit_snapshot", side_effect=counting_commit_snapshot
            ):
                # First call: stale marker → should fire one refresh dump
                self._force_throttle_expired()
                self.server._run_walguard_check(conn)

                # Second call: marker is now fresh → should NOT fire again
                self._force_throttle_expired()
                self.server._run_walguard_check(conn)

            assert dump_call_count["n"] == 1, (
                f"Expected exactly 1 refresh dump for stale marker; "
                f"got {dump_call_count['n']}"
            )

            # detected_at must still be the original value
            marker = engram_walguard.read_degraded_marker(str(self.tmp))
            assert marker is not None
            assert marker["detected_at"] == original_detected_at, (
                f"detected_at was clobbered during refresh: "
                f"expected {original_detected_at!r}, got {marker['detected_at']!r}"
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 10. Disabled-guard observability — once-per-process stderr line
# ---------------------------------------------------------------------------

class TestDisabledGuardObservability:
    """When ENGRAM_NO_DB_GUARDS=1, exactly one stderr line is emitted on the
    first skipped check; subsequent calls are silent.
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_walguard_obs_"))
        for key in list(sys.modules):
            if key == "server" or (key.startswith("engram_") and key != "engram_walguard"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        # No pre-created DB needed: ENGRAM_NO_DB_GUARDS=1 bypasses the DB-existence
        # and empty-graph guards in _get_db(), so _get_db() will create the full
        # schema on first call.
        self.server = server

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exactly_one_disabled_line_across_two_get_db_calls(self):
        """Two _get_db() calls with ENGRAM_NO_DB_GUARDS=1 → exactly one stderr
        line containing 'walguard disabled'.
        """
        import io

        # Ensure the once-flag is clear (setup calls _configure_paths which resets it)
        assert self.server.core._walguard_disabled_logged is False, (
            "Flag should be False after _configure_paths"
        )

        stderr_capture = io.StringIO()
        with mock.patch("sys.stderr", stderr_capture):
            conn1 = self.server._get_db()
            conn1.close()
            conn2 = self.server._get_db()
            conn2.close()

        output = stderr_capture.getvalue()
        lines = [ln for ln in output.splitlines() if "walguard" in ln and "disabled" in ln]
        assert len(lines) == 1, (
            f"Expected exactly 1 'walguard ... disabled' line; got {len(lines)}.\n"
            f"Full stderr:\n{output}"
        )


# ---------------------------------------------------------------------------
# 11. #786 ordering + read-mark regression tests
# ---------------------------------------------------------------------------

class TestNapWalShmRaceOrderingFix:
    """Regression tests for the #786 nap WAL/shm race fix.

    Two tests:
      A. Ordering test: dump_stripped is called BEFORE wal_checkpoint(TRUNCATE).
         Spies on both calls, records invocation order, asserts backup precedes
         checkpoint.  This test would FAIL against the pre-fix code.

      B. Round-trip freshness test: write data that sits in the WAL
         (uncommitted to the main file at checkpoint time), run _commit_snapshot,
         restore knowledge.sql into a fresh DB, assert row counts match —
         proving no committed write is lost across the new ordering.
    """

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_test_786_ordering_"))
        for key in list(sys.modules):
            if key == "server" or (key.startswith("engram_") and key != "engram_walguard"):
                del sys.modules[key]
        os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
        os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
        import server
        server._configure_paths(self.tmp)
        server._ensure_data_dir()
        server.core._git_available = True
        self.server = server

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_minimal_db(self) -> sqlite3.Connection:
        """Bootstrap a minimal WAL-mode DB via server._get_db()."""
        db_path = self.tmp / "knowledge.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
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
        """)
        conn.execute(
            "INSERT OR IGNORE INTO nodes (id, type, claim, created_at) "
            "VALUES ('ax_0001', 'axiom', 'test axiom', '2026-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()
        return self.server._get_db()

    def _make_fake_git_result(self, returncode=0, stdout="", stderr=""):
        r = mock.MagicMock()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def _mock_git(self, *args, **kwargs):
        if args and args[0] == "check-ignore":
            return self._make_fake_git_result(returncode=1)
        if args and args[0] == "status":
            return self._make_fake_git_result(returncode=0, stdout="M knowledge.sql")
        if args and args[0] == "commit":
            return self._make_fake_git_result(returncode=0)
        if args and args[0] == "rev-parse":
            return self._make_fake_git_result(returncode=0, stdout="abc123fake")
        if args and args[0] == "push":
            return self._make_fake_git_result(returncode=0)
        return self._make_fake_git_result(returncode=0)

    def test_dump_before_checkpoint_ordering(self):
        """dump_stripped is invoked BEFORE wal_checkpoint — the #786 Option A fix.

        Spies on engram_backup.dump_stripped and on the PRAGMA wal_checkpoint
        call via a wrapper around engram_core's ``_commit_snapshot`` internals.
        sqlite3.Connection.execute is a C-extension method and cannot be monkey-
        patched directly; instead we intercept at the engram_backup module level
        (spy on dump_stripped) and intercept the checkpoint PRAGMA by wrapping
        the connection in a subclass that records checkpoint calls.

        Strategy: pass a custom Connection subclass that logs wal_checkpoint
        calls, then assert dump_stripped precedes any checkpoint.  The subclass
        technique works because sqlite3.Connection supports subclassing.

        This test would FAIL against the pre-fix ordering (checkpoint at step 2,
        dump at step 2b).
        """
        call_order = []

        import engram_backup as _eb

        original_dump_stripped = _eb.dump_stripped

        def spy_dump_stripped(*args, **kwargs):
            call_order.append("dump_stripped")
            return original_dump_stripped(*args, **kwargs)

        # Build a subclass of sqlite3.Connection that records wal_checkpoint calls.
        class SpyConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if isinstance(sql, str) and "wal_checkpoint" in sql.lower():
                    call_order.append("wal_checkpoint")
                return super().execute(sql, parameters)

        # Bootstrap the DB using the server helper, then reopen via SpyConnection.
        self._make_minimal_db()  # creates schema + seeds data; returns and closes conn
        db_path = str(self.tmp / "knowledge.db")
        spy_conn = SpyConnection(db_path)
        spy_conn.row_factory = sqlite3.Row

        with mock.patch.object(_eb, "dump_stripped", side_effect=spy_dump_stripped), \
             mock.patch.object(self.server.core, "_git", side_effect=self._mock_git):
            result = self.server._commit_snapshot(spy_conn, "ordering test", "test")
        spy_conn.close()

        assert "dump_stripped" in call_order, (
            f"dump_stripped was never called; result={result}"
        )
        # Under skip_checkpoint=False (default) and _dump_error=None the checkpoint
        # must fire exactly once.  Assert unconditionally so the test fails closed
        # if the checkpoint never fires rather than passing vacuously.
        assert call_order.count("wal_checkpoint") == 1, (
            f"expected exactly one wal_checkpoint call; call_order={call_order}"
        )
        dump_idx = call_order.index("dump_stripped")
        ckpt_idx = call_order.index("wal_checkpoint")
        assert dump_idx < ckpt_idx, (
            f"wal_checkpoint fired BEFORE dump_stripped — #786 ordering regression!\n"
            f"call_order={call_order}"
        )

    def test_dump_failure_skips_checkpoint_and_releases_read_mark(self):
        """dump_stripped failure: no wal_checkpoint, wal_warning reflects skip, conn not in txn.

        Regression test for the dump-failure safety branch of _commit_snapshot
        (PR #1219, #786 fix).  When dump_stripped raises, the function must:

          (a) NOT call wal_checkpoint — skipping WAL compaction is conservative
              (WAL carries to next nap); checkpointing after a failed dump risks
              losing the WAL-resident data that the dump was meant to capture.

          (b) Set wal_warning to a string that indicates the checkpoint was
              skipped due to the failed dump (contains "skipped" + "dump failed").

          (c) Leave conn with no dangling open transaction: the finally-COMMIT
              in the read-mark acquisition block must fire regardless of the
              dump exception, so conn.in_transaction is False after the call.
        """
        import engram_backup as _eb

        call_order = []

        class SpyConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if isinstance(sql, str) and "wal_checkpoint" in sql.lower():
                    call_order.append("wal_checkpoint")
                return super().execute(sql, parameters)

        # Bootstrap the DB using the server helper, then reopen via SpyConnection.
        self._make_minimal_db()
        db_path = str(self.tmp / "knowledge.db")
        spy_conn = SpyConnection(db_path)
        spy_conn.row_factory = sqlite3.Row

        def failing_dump_stripped(*args, **kwargs):
            raise RuntimeError("simulated dump failure")

        with mock.patch.object(_eb, "dump_stripped", side_effect=failing_dump_stripped), \
             mock.patch.object(self.server.core, "_git", side_effect=self._mock_git):
            result = self.server._commit_snapshot(spy_conn, "dump failure test", "test")

        # (a) wal_checkpoint must NOT have been called.
        assert "wal_checkpoint" not in call_order, (
            f"wal_checkpoint was called despite dump_stripped raising; "
            f"call_order={call_order}. The dump-failure safety branch is broken: "
            f"checkpointing after a failed dump risks losing WAL-resident data."
        )

        # (b) wal_warning must reflect the skipped checkpoint due to dump failure.
        assert "wal_warning" in result, (
            f"wal_warning key absent from result after dump failure; result={result}. "
            f"The agent has no visibility that the checkpoint was skipped."
        )
        wal_warning = result["wal_warning"]
        assert "skipped" in wal_warning, (
            f"wal_warning does not contain 'skipped': {wal_warning!r}"
        )
        assert "dump failed" in wal_warning, (
            f"wal_warning does not contain 'dump failed': {wal_warning!r}"
        )

        # (c) The read-mark's finally-COMMIT must have fired: no dangling txn.
        assert not spy_conn.in_transaction, (
            f"conn.in_transaction is True after _commit_snapshot — the read-mark's "
            f"finally-COMMIT did not fire after dump_stripped raised. "
            f"This leaves a dangling open transaction on the connection."
        )

        spy_conn.close()

    def test_wal_data_survives_in_roundtrip(self):
        """Data written to the WAL before _commit_snapshot survives in the SQL dump.

        Empirical proof of the fix: write nodes to a WAL-mode DB so the data
        lives in the WAL (not yet checkpointed to the main file), run
        _commit_snapshot (which now dumps THEN checkpoints), restore
        knowledge.sql into a fresh DB, and verify row counts match.

        This confirms Connection.backup() captures WAL-resident writes without
        a prior checkpoint — the theoretical basis for Option A being safe.
        """
        import sys as _sys

        conn = self._make_minimal_db()

        # Write two extra nodes — they land in the WAL, not the main db file.
        for i in range(2):
            conn.execute(
                "INSERT OR IGNORE INTO nodes (id, type, claim, created_at) VALUES (?, ?, ?, ?)",
                (f"ob_wal_{i}", "observation", f"wal-resident claim {i}", "2026-01-01T00:00:00"),
            )
        conn.commit()

        # Record expected row count from the live DB before the snapshot.
        expected_nodes = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
        assert expected_nodes >= 3, f"fixture must have >= 3 nodes; got {expected_nodes}"

        with mock.patch.object(self.server.core, "_git", side_effect=self._mock_git):
            result = self.server._commit_snapshot(conn, "wal freshness test", "test")
        conn.close()

        # knowledge.sql must have been written.
        sql_path = self.tmp / "knowledge.sql"
        assert sql_path.exists(), f"knowledge.sql was not written; result={result}"
        assert sql_path.stat().st_size > 0, "knowledge.sql is empty"

        # Restore into a fresh DB and verify counts.
        recon_path = self.tmp / "recon.db"
        rc = sqlite3.connect(str(recon_path))
        rc.executescript(sql_path.read_text(encoding="utf-8"))
        restored_nodes = rc.execute("SELECT count(*) FROM nodes").fetchone()[0]
        rc.close()

        assert restored_nodes == expected_nodes, (
            f"WAL-resident data lost in round-trip: "
            f"expected {expected_nodes} nodes, restored {restored_nodes}.\n"
            f"This means dump_stripped missed WAL-resident writes — #786 fix broken."
        )


# ---------------------------------------------------------------------------
# 12. Read-mark statement actually acquires a WAL read mark
# ---------------------------------------------------------------------------

class TestReadMarkStatementActuallyHoldsMark:
    """Regression test proving that the read-mark statement reads a real page.

    `SELECT 1` is a constant-expression query — SQLite evaluates it without
    touching any database page, so it acquires NO WAL read mark.  A concurrent
    `wal_checkpoint(TRUNCATE)` from another connection therefore returns
    busy=0 (not blocked), which means the Option B belt does nothing.

    `SELECT count(*) FROM sqlite_master` reads page 1 (the schema page, always
    present), which forces SQLite to assign a read mark in the WAL read-lock
    table.  The same concurrent TRUNCATE checkpoint then returns busy=1
    (blocked), confirming the mark is held.

    Two sub-tests:
      - Positive control: the production statement (sqlite_master count) holds
        the mark → checkpoint returns busy == 1.
      - Negative control: the old statement (`SELECT 1`) does NOT hold the
        mark → checkpoint returns busy == 0.

    This test would have caught the silent no-op before the fix landed.
    """

    def _run_read_mark_scenario(self, read_stmt: str) -> int:
        """Return the busy count from TRUNCATE checkpoint after read_stmt.

        Opens a WAL-mode temp DB, writes WAL frames from connection W,
        then from connection R does BEGIN DEFERRED + read_stmt, and
        concurrently from connection W runs PRAGMA wal_checkpoint(TRUNCATE).
        Returns the busy value (0 = not blocked, 1 = blocked by R's read mark).
        """
        import tempfile as _tmp
        import os as _os

        db_dir = _tmp.mkdtemp(prefix="engram_readmark_")
        db_path = _os.path.join(db_dir, "test.db")
        try:
            # W: set up WAL-mode DB and write frames into the WAL.
            w = sqlite3.connect(db_path)
            w.execute("PRAGMA journal_mode=WAL")
            w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            w.execute("INSERT INTO t VALUES (1, 'hello')")
            w.commit()
            # Force a second WAL frame so there's real content to checkpoint.
            w.execute("INSERT INTO t VALUES (2, 'world')")
            w.commit()

            # R: open a reader, start a deferred transaction, execute the
            # candidate read-mark statement.
            r = sqlite3.connect(db_path)
            r.execute("BEGIN DEFERRED")
            r.execute(read_stmt)
            # R now holds (or does not hold) a WAL read mark depending on
            # whether read_stmt touched a real page.

            # W: attempt TRUNCATE checkpoint while R has the "read mark".
            row = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            # row = (busy, log, ckpt): busy==1 means at least one reader
            # blocked the truncation.
            busy = row[0]

            # Cleanup
            r.execute("COMMIT")
            r.close()
            w.close()
            return busy
        finally:
            import shutil as _shutil
            _shutil.rmtree(db_dir, ignore_errors=True)

    def test_read_mark_statement_actually_holds_mark(self):
        """Positive control: sqlite_master count blocks TRUNCATE (busy == 1)."""
        busy = self._run_read_mark_scenario("SELECT count(*) FROM sqlite_master")
        assert busy == 1, (
            f"Expected busy=1 (TRUNCATE blocked by read mark) with "
            f"'SELECT count(*) FROM sqlite_master'; got busy={busy}. "
            f"The read-mark statement is NOT acquiring a real WAL read mark — "
            f"Option B belt is a silent no-op."
        )

    def test_select_one_does_not_hold_mark(self):
        """Negative control: SELECT 1 does NOT block TRUNCATE (busy == 0).

        This is the pre-fix behaviour that prompted the defect report.
        If this assertion fails it means SQLite's behaviour changed and
        SELECT 1 now touches a page (unlikely but worth knowing).
        """
        busy = self._run_read_mark_scenario("SELECT 1")
        assert busy == 0, (
            f"Expected busy=0 (TRUNCATE NOT blocked) with 'SELECT 1'; "
            f"got busy={busy}. SQLite may have changed how it handles "
            f"constant-expression queries — re-evaluate whether 'SELECT 1' "
            f"now acquires a WAL read mark."
        )
