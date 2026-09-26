"""Contract tests for src/engram/engramd_owner.py -- #2652 Phase-1 Unit 3
(U3), issue #3096.

This unit is the owner process SKELETON: one owner per seat, sole handle
on knowledge.db, speaking U1's already-built wire contract
(src/engram/engramd_client.py) -- handshake (contract-version check on
every request) + `ping` + `snapshot()`. **No domain operations of any
kind** (no create_node, no retract) -- those are out of scope for this
build; see docs/2652-engrammanager-design.md SS3 and SS5, and the U3 fairy
spec, for the full reasoning this file's tests are checked against.

The 10 numbered contract items below are issue #3096's body PLUS its
comment (Borges's items 8-10, added from a U1 round-5 finding), verbatim --
this is the authoritative list:

  1.  One owner per seat: a second instance for the same $ENGRAM_HOME
      refuses to start with a clear error; the first stays up, unaffected.
  2.  A stale socket/pid left by a crashed owner does not block a restart
      -- detect it (dead PID) and replace it, never refuse forever.
  3.  Handshake uses U1's version check: MAJOR mismatch ->
      Rejected(contract_version); MINOR mismatch is accepted.
  4.  `ping` reports Ok with the owner's contract version and the store
      path it holds.
  5.  `snapshot()` writes a quiesced, consistent copy via SQLite's backup
      API; the copy opens and has the same row counts as the source at
      that moment. Not a manager mechanism -- a direct sqlite3 operation.
  6.  Clean shutdown removes the socket; after that a U1 client gets
      Unavailable, not a hang.
  7.  Unknown operation -> Rejected(reason="unknown_operation"), never
      Unavailable.
  8.  An end-to-end deadline per request, on the OWNER side, measured from
      ACCEPT (not per-recv): a client that connects and trickles its
      request (never completing a line) is dropped once the deadline
      passes; the owner itself stays up.
  9.  One slow client does not block another: with client A trickling as
      in item 8, client B's `ping` succeeds well within B's own bound --
      this build's answer is thread-per-connection CONCURRENCY (stated
      explicitly per the design doc's own ask).
  10. Response size is capped on the READ side (incoming requests TO the
      owner), so a runaway peer can't grow the buffer without limit.

Plus two discrimination controls (contract-first can-it-fail, per this
repo's CLAUDE.md), both run BY HAND as part of the fairy's own
verification (not part of this automated file -- see the fairy handoff for
the actual mutated-run pytest output):

  - Item 1: make the second-instance liveness check a no-op -> test 1 must
    go red, every other test stays green.
  - Item 8: change the owner to per-recv timeouts (a single
    conn.settimeout(bound) set once, never re-armed with a recomputed
    remaining value) -> tests 8 AND 9 must go red, every other test stays
    green.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

# The server-side deadline used across every test fixture in this file.
# Short enough that the suite runs fast; long enough (per the fairy spec's
# own steer, following U1's own low-single-digit-seconds scale) that the
# trickle-vs-instant distinction below is unambiguous rather than flaky.
REQUEST_DEADLINE = 0.6


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engram_home():
    """A short-path temp directory for this seat's $ENGRAM_HOME.

    Short prefix, never pytest's nested tmp_path -- AF_UNIX socket paths
    are constrained by the kernel's sockaddr_un buffer (~104 bytes on
    macOS/BSD), and tmp_path's long per-test nesting routinely exceeds it
    on macOS (same constraint documented in
    tests/test_engramd_client.py's own sock_dir fixture and
    tests/test_surface_daemon_pid_reap.py).
    """
    d = tempfile.mkdtemp(prefix="edo-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def owner_running(engram_home):
    """A started EngramdOwner, stopped at teardown. Signal handlers are
    deliberately NOT installed (install_signal_handlers=False) -- tests run
    inside the pytest process itself, and installing SIGTERM/SIGINT
    handlers there would clobber pytest's own signal handling across
    however many of these fixtures run in one session. The cleanup path
    tested here (.stop()) is exactly what the real signal handler calls
    (see engramd_owner.EngramdOwner._handle_signal), so this does not skip
    any contract behavior -- only the OS-level signal *delivery* itself is
    left unexercised (a subprocess/real-signal integration test is future
    work, not a gap in this build's contract; see the fairy handoff)."""
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        owner.stop()


def _spawn_and_kill() -> int:
    """A real subprocess, terminated and waited-on -- so its PID is
    confirmably dead. Mirrors tests/test_surface_daemon_pid_reap.py's own
    _spawn_sleeper() + terminate() pattern rather than guessing a
    hardcoded "big number" PID that might collide with something real."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    proc.terminate()
    proc.wait(timeout=5)
    return proc.pid


def _raw_request(socket_path: Path, payload: dict, *, timeout: float = 2.0) -> dict | None:
    """Send a hand-built wire request directly over the socket, bypassing
    engramd_client's convenience wrappers entirely. Used where a test needs
    to control the wire's contract_version field directly -- something
    ec.read()/ec.write() cannot do (they always send the client's own
    engramd_client.CONTRACT_VERSION). Returns the parsed response dict, or
    None if the connection closed/timed out without a complete line."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            data += chunk
            if b"\n" in data:
                break
        return json.loads(data.decode("utf-8").strip())
    except OSError:
        return None
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Test 1 -- one owner per seat: a second instance refuses to start; the
# first stays up, unaffected.
# ---------------------------------------------------------------------------


def test_second_instance_refuses_to_start_and_first_remains_unaffected(engram_home):
    owner1 = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner1.start(install_signal_handlers=False)
    try:
        owner2 = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
        with pytest.raises(eo.OwnerAlreadyRunning):
            owner2.start(install_signal_handlers=False)

        # The first owner is completely unaffected -- still serving.
        result = ec.read("ping", {}, timeout=1.0, socket_path=owner1.socket_path)
        assert isinstance(result, ec.Ok)
    finally:
        owner1.stop()


# ---------------------------------------------------------------------------
# Test 2 -- a stale socket/pid left by a crashed owner does not block a
# restart.
# ---------------------------------------------------------------------------


def test_stale_socket_and_pid_does_not_block_restart(engram_home):
    dead_pid = _spawn_and_kill()
    stale_socket = engram_home / ec.DEFAULT_SOCKET_NAME
    stale_pid_file = engram_home / eo.DEFAULT_PID_NAME

    # A socket FILE with no live listener (simulates a crashed owner that
    # never cleaned up), plus a pidfile naming a confirmably-dead process.
    dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dummy.bind(str(stale_socket))
    dummy.close()
    stale_pid_file.write_text(str(dead_pid))

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)  # must NOT raise -- never refuse forever
    try:
        result = ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path)
        assert isinstance(result, ec.Ok)
    finally:
        owner.stop()


# ---------------------------------------------------------------------------
# Test 3 -- handshake uses U1's version check. MAJOR mismatch -> Rejected
# (contract_version); MINOR mismatch is accepted. Uses the raw wire helper
# (not ec.read()/write()) because this test needs to control the wire's
# contract_version field directly.
# ---------------------------------------------------------------------------


def test_handshake_major_version_mismatch_is_rejected(owner_running):
    response = _raw_request(
        owner_running.socket_path,
        {"contract_version": "99.0", "method": "ping", "params": {}, "request_id": None},
    )
    assert response is not None
    assert response["status"] == "rejected"
    assert response["reason"] == "contract_version"
    assert response["remedy"]


def test_handshake_minor_version_mismatch_is_accepted(owner_running):
    # Owner's contract version is engramd_client.CONTRACT_VERSION ("1.0" --
    # MAJOR "1"). Sending "1.7" is a MINOR skew on the SAME major, which
    # check_contract_version() accepts (None -- proceed to serve the call).
    assert owner_running.contract_version == "1.0"
    response = _raw_request(
        owner_running.socket_path,
        {"contract_version": "1.7", "method": "ping", "params": {}, "request_id": None},
    )
    assert response is not None
    assert response["status"] == "ok"


# ---------------------------------------------------------------------------
# Test 4 -- `ping` reports Ok with the owner's contract version and the
# path of the store it holds.
# ---------------------------------------------------------------------------


def test_ping_reports_ok_with_contract_version_and_store_path(owner_running):
    result = ec.read("ping", {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Ok)
    assert result.result["contract_version"] == owner_running.contract_version
    assert result.result["store_path"] == str(owner_running.store_path)


# ---------------------------------------------------------------------------
# Test 5 -- snapshot() writes a quiesced, consistent copy via SQLite's
# backup API; the copy opens and has the same row counts as the source.
# ---------------------------------------------------------------------------


def test_snapshot_writes_consistent_copy_with_matching_row_counts(engram_home):
    # Seed the store BEFORE the owner starts (mirrors a real seat: bootstrap
    # creates knowledge.db, then the owner starts and holds it open) --
    # this build has no write operations to layer a dynamic write on top of
    # (see the fairy handoff's design-choices section), so the contract is
    # checked against a static pre-populated store.
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    seed = sqlite3.connect(str(store_path))
    seed.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    seed.executemany("INSERT INTO widgets (name) VALUES (?)", [("a",), ("b",), ("c",)])
    seed.commit()
    seed.close()

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "snap.db"
        result = ec.read(
            "snapshot", {"dest_path": str(dest_path)}, timeout=3.0, socket_path=owner.socket_path
        )
        assert isinstance(result, ec.Ok)
        assert result.result["dest_path"] == str(dest_path)

        dest_conn = sqlite3.connect(str(dest_path))
        try:
            count = dest_conn.execute("SELECT COUNT(*) FROM widgets").fetchone()[0]
            assert count == 3
        finally:
            dest_conn.close()
    finally:
        owner.stop()


def test_snapshot_missing_dest_path_param_is_rejected(owner_running):
    """Not one of the 10 numbered contract items -- covers this build's own
    documented design choice for snapshot()'s param shape (see the fairy
    handoff)."""
    result = ec.read("snapshot", {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Rejected)
    assert result.reason == "invalid_params"


def test_snapshot_nonexistent_dest_parent_is_rejected_cleanly(owner_running):
    """Not one of the 10 numbered contract items -- covers this build's own
    documented design choice: reject cleanly rather than silently create
    the missing parent directory (see the fairy handoff)."""
    missing_parent_dest = owner_running.engram_home / "no-such-subdir" / "snap.db"
    result = ec.read(
        "snapshot",
        {"dest_path": str(missing_parent_dest)},
        timeout=1.0,
        socket_path=owner_running.socket_path,
    )
    assert isinstance(result, ec.Rejected)
    assert result.reason == "invalid_destination"
    assert not missing_parent_dest.parent.exists()


# ---------------------------------------------------------------------------
# Test 6 -- clean shutdown removes the socket; after that a U1 client gets
# Unavailable, not a hang.
# ---------------------------------------------------------------------------


def test_clean_shutdown_removes_socket_and_client_sees_unavailable(engram_home):
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    socket_path = owner.socket_path
    assert socket_path.exists()

    # Sanity: owner responds while up.
    result = ec.read("ping", {}, timeout=1.0, socket_path=socket_path)
    assert isinstance(result, ec.Ok)

    owner.stop()
    assert not socket_path.exists()

    result2 = ec.read("ping", {}, timeout=0.5, socket_path=socket_path)
    assert isinstance(result2, ec.Unavailable)


# ---------------------------------------------------------------------------
# Test 7 -- unknown operation -> Rejected(reason="unknown_operation"),
# never Unavailable.
# ---------------------------------------------------------------------------


def test_unknown_operation_returns_rejected_not_unavailable(owner_running):
    result = ec.read("no_such_method", {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Rejected)
    assert not isinstance(result, ec.Unavailable)
    assert result.reason == "unknown_operation"


# ---------------------------------------------------------------------------
# Test 8 -- an end-to-end deadline per request, on the OWNER side, measured
# from ACCEPT (not per-recv). A trickling client (bytes arriving slower
# than the deadline, never completing a line) is dropped once the deadline
# passes; the owner itself stays up.
#
# The trickle MUST be genuine (repeated small sends with gaps shorter than
# the deadline) rather than a single send-then-silence: a single blocking
# recv() after one partial send would time out correctly under BOTH the
# correct implementation AND the item-8 discrimination-control mutation
# (a single conn.settimeout(bound) set once behaves identically to the
# correct re-armed version when there is only ever one more blocking wait
# left) -- this is the exact "total silence doesn't distinguish" trap
# engramd_client.py's own test suite already documents for the client-side
# mirror of this same bug (see test_trickling_owner_is_bounded_by_end_to_
# end_deadline's comment in tests/test_engramd_client.py).
# ---------------------------------------------------------------------------


def _trickle_and_measure(socket_path: Path, bound: float) -> tuple[float, bytes]:
    """Connect, trickle small sends (gaps shorter than `bound`, total
    scheduled duration several times `bound`) until the peer drops the
    connection or the trickle completes, then read back whatever the peer
    sends (expected: nothing -- a closed connection). Returns
    (elapsed_seconds, final_recv_bytes)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    start = time.monotonic()
    try:
        for _ in range(30):
            sock.sendall(b"x")
            time.sleep(bound / 5)  # gap < bound; 30 * bound/5 = 6*bound total if never dropped
    except OSError:
        pass  # owner already closed its end once the deadline passed
    sock.settimeout(bound * 3)
    try:
        response = sock.recv(4096)
    except OSError:
        response = b""
    elapsed = time.monotonic() - start
    sock.close()
    return elapsed, response


def test_trickling_client_is_dropped_at_the_end_to_end_deadline(owner_running):
    bound = owner_running.request_deadline
    elapsed, response = _trickle_and_measure(owner_running.socket_path, bound)

    assert response == b""  # dropped without ever completing a response
    # Bounded roughly at the deadline, not at the full 6x-bound trickle
    # schedule -- the generous upper multiple avoids CI flakiness while
    # still clearly distinguishing from the ~6x-bound (or worse) overshoot
    # the item-8 discrimination-control mutation produces.
    assert elapsed < bound * 2.5, f"elapsed={elapsed}, bound={bound}"

    # The owner itself is unaffected -- still serving other requests.
    result = ec.read("ping", {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Ok)


# ---------------------------------------------------------------------------
# Test 9 -- one slow client does not block another: with client A
# trickling as in item 8, client B's `ping` succeeds well within B's own
# bound. This build's answer is thread-per-connection CONCURRENCY (stated
# explicitly per the fairy spec's own ask) -- proven by running A's trickle
# concurrently with B's ping and asserting B is fast. This test ALSO
# reuses item 8's own bounded-elapsed assertion on A (embedded here rather
# than only in test 8) -- that is deliberate: it is what makes the item-8
# discrimination-control mutation redden BOTH tests 8 and 9 (a broken
# per-connection deadline computation does not, by itself, slow down a
# DIFFERENT connection's thread under thread-per-connection concurrency, so
# without this embedded check test 9 would stay green under that mutation,
# contradicting the fairy spec's explicit "8 and 9 must go red" -- see the
# fairy handoff's Ambiguous decisions section).
# ---------------------------------------------------------------------------


def test_one_slow_client_does_not_block_another(owner_running):
    bound = owner_running.request_deadline
    result_holder: dict = {}

    def _run_trickle():
        elapsed, response = _trickle_and_measure(owner_running.socket_path, bound)
        result_holder["elapsed"] = elapsed
        result_holder["response"] = response

    a_thread = threading.Thread(target=_run_trickle, daemon=True)
    a_thread.start()
    time.sleep(bound / 10)  # let A connect and begin trickling first

    b_start = time.monotonic()
    b_result = ec.read("ping", {}, timeout=bound, socket_path=owner_running.socket_path)
    b_elapsed = time.monotonic() - b_start

    assert isinstance(b_result, ec.Ok)
    assert b_elapsed < bound / 2, (
        f"B's ping should succeed quickly even while A trickles, got {b_elapsed}"
    )

    a_thread.join(timeout=bound * 4)
    assert not a_thread.is_alive(), "A's own connection should have been dropped by the owner's deadline"
    assert result_holder.get("response") == b""
    assert result_holder["elapsed"] < bound * 2.5, result_holder.get("elapsed")


# ---------------------------------------------------------------------------
# Test 10 -- response size is capped on the READ side (incoming requests TO
# the owner), so a runaway peer can't grow the buffer without limit.
# ---------------------------------------------------------------------------


def test_oversized_incoming_request_is_dropped_and_owner_stays_up(owner_running):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(owner_running.socket_path))
    oversized = b"x" * (owner_running.max_request_bytes + 1024)  # no '\n' anywhere
    try:
        sock.sendall(oversized)
    except OSError:
        pass  # owner may drop the connection mid-send once the cap is exceeded
    sock.settimeout(owner_running.request_deadline + 2.0)
    try:
        response = sock.recv(4096)
    except OSError:
        response = b""
    sock.close()

    assert response == b""  # dropped, no response ever sent

    # The owner itself remains alive and serving other clients.
    result = ec.read("ping", {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Ok)
