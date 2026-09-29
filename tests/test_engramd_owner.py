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


# ---------------------------------------------------------------------------
# #2652 Phase-1 Unit 4b (issue #3195) -- the owner's two connections and the
# authorizer floor. Test numbers below are docs/2652-security-slice-design.md
# SS5's own numbering (a SEPARATE list from the 10 numbered contract items in
# this file's own module docstring, which are issue #3096's original items 1-
# 10 for the U3 skeleton build) -- 10, 11, 12, 17, 19, 22 are the subset this
# unit owns.
#
# LOAD-BEARING MEASURED DEVIATION, flagged per this unit's own escape valve
# (see the fairy handoff for the full writeup): sqlite3.Connection.backup()
# never invokes ANY authorizer action, on either connection, at all --
# confirmed with a deny-all authorizer (0 callbacks fired, backup succeeds in
# full) on both Python 3.9/sqlite 3.54.0 and Python 3.12/sqlite 3.53.4. This
# is the C-level backup API bypassing SQL-statement preparation entirely, not
# a version artifact. Consequence: test 17's own literal mutation ("deny
# SQLITE_TRANSACTION on the owner-internal authorizer -- the snapshot must
# fail loudly") CANNOT discriminate against this build's `_handle_snapshot()`
# shape, because denying anything on that authorizer has zero effect on
# `.backup()`'s outcome. test_snapshot_runs_on_owner_internal_path_and_error_
# is_not_swallowed below tests the same underlying "fail loudly, never
# swallowed" property through a mutation that DOES discriminate (corrupting
# the except clause), and documents the measured mismatch rather than
# silently pretending the literal mutation works.
# ---------------------------------------------------------------------------


def test_owner_module_never_calls_executescript():
    """Design doc test 10: a repo-level grep assertion over
    src/engram/engramd_owner.py ONLY -- the census over the whole owner
    PACKAGE is a later unit's job once more files exist (this unit's own
    spec is explicit about that scope bound). Mutation: add an
    `executescript` call anywhere in the file -- this test must go red."""
    source = (REPO_ROOT / "src" / "engram" / "engramd_owner.py").read_text()
    assert "executescript" not in source


def test_pragma_write_is_refused_even_when_its_name_is_allowed():
    """Design doc test 11 (P16): `PRAGMA user_version = 99` under an
    allowlist containing `user_version` must be denied, asserted on the
    VALUE (still 0 after the attempt), not merely on the authorizer's
    return code. Mutation: drop the `arg2 is None` clause from
    eo._client_read_authorizer's pragma branch -- without it, the value
    changes 0 -> 99 even though only `user_version` was allowlisted, and
    this test must go red."""
    conn = sqlite3.connect(":memory:")
    conn.set_authorizer(eo._client_read_authorizer)

    before = conn.execute("PRAGMA user_version").fetchone()[0]
    assert before == 0

    with pytest.raises(sqlite3.Error):
        conn.execute("PRAGMA user_version = 99")

    after = conn.execute("PRAGMA user_version").fetchone()[0]
    assert after == 0, "a pragma WRITE must never take effect, even for an allowlisted name"


# A non-recursive VALUES-based CTE cross-joined 3 ways -- 300**3 = 27M
# combinations, ~0.5s unbounded. Deliberately NOT a `WITH RECURSIVE` CTE:
# that shape fires SQLITE_RECURSIVE, an action outside this build's
# measured baseline allowlist (SQLITE_SELECT/READ/FUNCTION only) that
# would be denied outright by the authorizer floor before the progress
# handler ever got a chance to matter -- caught while writing this test
# (see the fairy handoff). This shape fires only SQLITE_SELECT and
# SQLITE_FUNCTION (measured), so it exercises the deadline axis in
# isolation from the authorizer floor, as test 12 intends.
_EXPENSIVE_CARTESIAN_SQL = (
    "WITH nums(x) AS (VALUES " + ", ".join("(%d)" % i for i in range(300)) + ") "
    "SELECT COUNT(*) FROM nums a, nums b, nums c WHERE a.x + b.x + c.x >= 0"
)


def test_expensive_read_is_aborted_by_deadline_and_cheap_read_is_not(owner_running):
    """Design doc test 12: a deliberately expensive read is aborted by the
    deadline, and a cheap read is not. Bounds VM-instruction cost only
    (test 19 covers the size axis; the end-to-end-deadline axis, test 20,
    is out of scope -- no dispatch operation exists yet to test it
    against). Mutation: remove the progress_handler registration in
    eo._arm_read_deadline -- the expensive read must then complete
    instead of aborting, and this test must go red.

    Also asserts the abort point tracks the PASSED-IN deadline parameter,
    not a fixed constant: shortening the deadline must shorten the abort
    point.
    """
    gate = owner_running._client_read_gate

    # Cheap read: comfortably under a generous deadline, completes.
    rows = gate.execute("SELECT 1", deadline=time.monotonic() + 5.0)
    assert rows == [(1,)]

    # Expensive read: aborted once the deadline passes, rather than
    # running to its own ~0.3s completion.
    start = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        gate.execute(_EXPENSIVE_CARTESIAN_SQL, deadline=time.monotonic() + 0.05)
    elapsed = time.monotonic() - start
    assert elapsed < 0.25, f"abort should happen near the deadline, not run to completion: {elapsed}"

    # The abort point tracks the DEADLINE PARAMETER: a shorter deadline
    # aborts sooner than a longer one, on the SAME expensive query.
    start_short = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        gate.execute(_EXPENSIVE_CARTESIAN_SQL, deadline=time.monotonic() + 0.02)
    elapsed_short = time.monotonic() - start_short

    start_long = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        gate.execute(_EXPENSIVE_CARTESIAN_SQL, deadline=time.monotonic() + 0.15)
    elapsed_long = time.monotonic() - start_long

    assert elapsed_short < elapsed_long, (elapsed_short, elapsed_long)


def test_snapshot_runs_on_owner_internal_path_and_error_is_not_swallowed(engram_home):
    """Design doc test 17, WITH A DOCUMENTED MEASURED DEVIATION from its
    literal mutation -- see this section's module-level comment above and
    the fairy handoff for the full writeup. sqlite3.Connection.backup()
    never invokes the authorizer at all, on either connection, so denying
    SQLITE_TRANSACTION on the owner-internal authorizer cannot make this
    build's _handle_snapshot() fail (there is nothing for the denial to
    interrupt). What IS faithfully tested, matching the underlying "runs
    on the owner-internal path, fails loudly, never swallowed" property:

      1. snapshot() succeeds via the owner-internal connection under its
         normal (permissive) authorizer.
      2. A genuine sqlite3.Error raised BY backup() itself -- triggered
         here the one way this build found that actually makes backup()
         raise (a destination file that exists but is not a valid sqlite
         database; an authorizer denial cannot, per the measurement
         above) -- surfaces as the EXISTING
         Rejected(reason="snapshot_failed", ...) response. Mutation:
         change the `except sqlite3.Error` block in _handle_snapshot to
         swallow the error (e.g. `except sqlite3.Error: pass`, falling
         through to the success response) -- this test must go red.
      3. The client-read connection's authorizer is never touched by a
         snapshot() call (docs/2652-security-slice-design.md SS3.1: "no
         authorizer swapping").
    """
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    seed = sqlite3.connect(str(store_path))
    seed.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    seed.executemany("INSERT INTO widgets (name) VALUES (?)", [("a",), ("b",)])
    seed.commit()
    seed.close()

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        # -- part 1: snapshot() succeeds on the owner-internal connection.
        good_dest = engram_home / "snap-good.db"
        result = ec.read(
            "snapshot",
            {"dest_path": str(good_dest)},
            timeout=3.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(result, ec.Ok)

        # -- part 2: a genuine sqlite3.Error during backup() surfaces as
        # Rejected(reason="snapshot_failed"), not swallowed.
        bad_dest = engram_home / "snap-bad.db"
        bad_dest.write_bytes(b"not a sqlite database, just garbage bytes" * 4)
        result2 = ec.read(
            "snapshot",
            {"dest_path": str(bad_dest)},
            timeout=3.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(result2, ec.Rejected)
        assert result2.reason == "snapshot_failed"

        # -- part 3: the client-read connection's authorizer is never
        # touched by snapshot() -- proven by identity, not "coincidentally
        # still permissive": install a distinguishable marker authorizer
        # directly on the connection, run a snapshot, then confirm the
        # SAME marker (not merely a same-behaving one) is still what
        # fires.
        marker_calls: list = []

        def _marker_authorizer(action_code, arg1, arg2, db_name, trigger_or_view):
            marker_calls.append(action_code)
            return sqlite3.SQLITE_DENY

        owner._client_read_conn.set_authorizer(_marker_authorizer)

        result3 = ec.read(
            "snapshot",
            {"dest_path": str(engram_home / "snap-3.db")},
            timeout=3.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(result3, ec.Ok)
        assert marker_calls == [], "snapshot() must never touch the client-read connection at all"

        with pytest.raises(sqlite3.Error):
            owner._client_read_conn.execute("SELECT 1")
        assert marker_calls == [
            sqlite3.SQLITE_SELECT
        ], "the marker installed before snapshot() must still be the one firing after it"
    finally:
        owner.stop()


def test_snapshot_copies_from_the_owner_internal_connection_not_client_read(engram_home):
    """Mira's finding on PR #3221 (round 4): nothing previously distinguished WHICH
    connection `.backup()` runs on. Both connections point at the same
    `store_path` and `.backup()` never consults an authorizer (measured
    above), so a wrong-connection bug would silently copy the same data
    and pass every existing test -- including test 17's marker-authorizer
    check, which only proves the CLIENT-READ authorizer is untouched, not
    which connection actually served the copy. Confirmed directly: with
    `self._owner_conn.backup(dest_conn)` in `_handle_snapshot()` swapped
    for `self._client_read_conn.backup(dest_conn)`, all pre-existing tests
    stayed green.

    Discriminator: close `_client_read_conn` before calling snapshot(). If
    `_handle_snapshot()` correctly calls `.backup()` on `_owner_conn`
    (which stays open), the snapshot succeeds regardless of the
    client-read connection's state. If it were mutated to call `.backup()`
    on `_client_read_conn` instead, that connection is closed --
    `sqlite3.ProgrammingError` (a `sqlite3.Error` subclass, measured
    directly against both `.execute()` and `.backup()` on a closed
    connection) would raise, which `_handle_snapshot()`'s existing
    `except sqlite3.Error` catches and reports as
    `Rejected(reason="snapshot_failed")` rather than `Ok`.

    Mutation: change `self._owner_conn.backup(dest_conn)` to
    `self._client_read_conn.backup(dest_conn)` in `_handle_snapshot()` --
    this test must go red (`Ok` -> `Rejected`)."""
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    seed = sqlite3.connect(str(store_path))
    seed.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    seed.commit()
    seed.close()

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        owner._client_read_conn.close()

        result = ec.read(
            "snapshot",
            {"dest_path": str(engram_home / "snap-source-check.db")},
            timeout=3.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(result, ec.Ok), (
            "snapshot() must succeed from the owner-internal connection "
            "even when the client-read connection is closed -- a failure "
            "here means the wrong connection is being used as the copy source"
        )
    finally:
        owner.stop()


def test_single_value_size_is_bounded_independently_of_progress_handler(owner_running):
    """Design doc test 19: single-value size is bounded independently of
    the progress handler. `SELECT zeroblob(N)` above the configured
    SQLITE_LIMIT_LENGTH is refused on the client-read connection; a
    result under the limit succeeds. Mutation: remove the `setlimit`
    call in start() -- the oversized result must then be returned, and
    this test must go red.

    Includes the control that makes the point (design doc SS4,
    reproduced here): with a progress handler armed (deadline far in the
    future, so it never fires) and NO setlimit call, the oversized
    zeroblob STILL returns in full -- proving the handler is not what
    bounds size.
    """
    gate = owner_running._client_read_gate
    limit = eo.CLIENT_READ_LIMIT_LENGTH

    under = gate.execute("SELECT length(zeroblob(?))", (limit - 1,), deadline=time.monotonic() + 5.0)
    assert under == [(limit - 1,)]

    with pytest.raises(sqlite3.Error):
        gate.execute("SELECT zeroblob(?)", (limit + 1,), deadline=time.monotonic() + 5.0)

    # Control: progress handler armed but never firing (generous
    # deadline), and NO setlimit anywhere on this bare connection -- the
    # oversized value still comes back in full.
    control_conn = sqlite3.connect(":memory:")
    try:
        deadline = time.monotonic() + 5.0

        def _never_fires() -> int:
            return 1 if time.monotonic() >= deadline else 0

        control_conn.set_progress_handler(_never_fires, eo.PROGRESS_HANDLER_INSTRUCTION_COUNT)
        oversized = 8 * 1024 * 1024
        row = control_conn.execute("SELECT length(zeroblob(?))", (oversized,)).fetchone()
        assert row == (oversized,), "the progress handler alone must not bound single-value size"
    finally:
        control_conn.close()


def test_client_read_gate_holds_no_reference_to_owner_internal_connection(owner_running):
    """Design doc test 22: client SQL cannot reach the unrestricted
    connection, asserted STRUCTURALLY -- the object a future client-facing
    read would dispatch through holds no reference to the owner-internal
    connection. Deliberately independent of the authorizer (it isolates
    routing, not the floor). Mutation: change start() to construct
    `self._client_read_gate = _ClientReadGate(self._owner_conn)` instead
    of `self._client_read_conn` -- this test must go red (run by hand;
    see the fairy handoff)."""
    gate = owner_running._client_read_gate
    assert gate is not None
    assert vars(gate).get("_conn") is owner_running._client_read_conn

    for attr_name, value in vars(gate).items():
        assert value is not owner_running._owner_conn, (
            f"_ClientReadGate.{attr_name} holds the owner-internal connection"
        )


# ---------------------------------------------------------------------------
# Round-2 fix (PR #3221, Sol's review) -- nothing above tests that start()
# actually wires the RESTRICTIVE authorizer onto the REAL
# owner._client_read_conn. Test 11 opens its own bare :memory: connection
# and calls eo._client_read_authorizer directly, never touching
# owner._client_read_conn; tests 12, 19, and 22 use the real connection but
# only check the deadline, the size limit, and the gate's structural
# isolation -- none of them check the connection's *authorizer*. Confirmed
# by hand: swapping start()'s
# `self._client_read_conn.set_authorizer(_client_read_authorizer)` for
# `self._client_read_conn.set_authorizer(_owner_internal_authorizer)`
# (silently wiring the PERMISSIVE owner-internal authorizer instead of the
# restrictive one) leaves all 19 pre-existing tests green. Mutation: that
# same swap -- this test must go red; tests 12, 19, and 22 must stay green.
# ---------------------------------------------------------------------------


def test_start_wires_the_restrictive_authorizer_onto_the_real_client_read_conn(owner_running):
    """Exercises owner_running._client_read_conn directly -- the actual
    connection object opened and authorizer-wired by start(), not an
    isolated :memory: connection with the authorizer function called by
    hand (that is test 11, above). A table is created via the
    owner-internal connection (owner._owner_conn, permissive by design)
    since CREATE TABLE is itself denied on the client-read connection --
    this test only needs a table to exist, not to exercise CREATE TABLE's
    own denial."""
    conn = owner_running._client_read_conn
    owner_running._owner_conn.execute("CREATE TABLE t (x)")
    owner_running._owner_conn.commit()

    with pytest.raises(sqlite3.Error):
        conn.execute("INSERT INTO t VALUES (1)")

    with pytest.raises(sqlite3.Error):
        conn.execute("ATTACH DATABASE ':memory:' AS aux")

    # Pragma WRITE: measured directly against this REAL connection before
    # writing this test -- execute() DOES raise for a denied pragma write
    # here, matching test 11's own isolated-connection measurement. Assert
    # both the raise AND the unchanged value, mirroring test 11's own
    # pattern for the isolated-connection case.
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    with pytest.raises(sqlite3.Error):
        conn.execute("PRAGMA user_version = 5")
    after = conn.execute("PRAGMA user_version").fetchone()[0]
    assert after == before, "a pragma WRITE must never take effect on the real wired connection"

    with pytest.raises(sqlite3.Error):
        conn.execute("BEGIN")

    # The connection is denying dangerous things, not everything.
    assert conn.execute("SELECT 1").fetchall() == [(1,)]


# ---------------------------------------------------------------------------
# Round-2 fix (PR #3221, Sol's review) -- issue #3152 item 1's lock fold in
# _cleanup() (both connections closed under self._owner_lock, so a .stop()
# cannot race an in-flight _handle_snapshot()) has no test proving the lock
# actually does anything. Confirmed by hand: replacing
# `with self._owner_lock:` in _cleanup() with `if True:` (removing the
# lock's blocking behavior while keeping the code structurally parseable)
# leaves all 19 pre-existing tests green. Mutation: that same swap -- this
# test must go red (the cleanup thread would close the connections
# immediately, without waiting for the lock, so the "still open while held"
# assertion below fails).
# ---------------------------------------------------------------------------


def test_cleanup_blocks_on_owner_lock_held_by_an_in_flight_operation(owner_running):
    """Simulates an in-flight _handle_snapshot() by acquiring
    owner._owner_lock directly in THIS test's own thread, then calling
    owner._cleanup() from a second thread. Calls ._cleanup() directly
    rather than .stop() -- .stop() blocks on _accept_thread.join() first,
    a real but separate join-timing concern this test doesn't need to
    exercise; ._cleanup() is the more direct way to check the lock fold
    itself (documented choice, per this fix's spec)."""
    owner = owner_running
    owner._owner_lock.acquire()
    try:
        cleanup_thread = threading.Thread(target=owner._cleanup, daemon=True)
        cleanup_thread.start()

        # The cleanup thread is blocked waiting for the lock this test
        # thread holds. This sleep only needs to be "long enough for a
        # thread switch", not exact timing -- a real threading.Lock
        # genuinely blocks the second thread for as long as this test
        # thread holds it, so there is no race to lose regardless of how
        # long the switch actually takes; 0.2s is comfortably longer than
        # a scheduler switch on any platform this suite runs on.
        time.sleep(0.2)
        assert owner._owner_conn is not None, "cleanup must not have closed this yet"
        assert owner._client_read_conn is not None, "cleanup must not have closed this yet"
        assert cleanup_thread.is_alive(), "the cleanup thread should still be blocked on the lock"
    finally:
        owner._owner_lock.release()

    cleanup_thread.join(timeout=5.0)
    assert not cleanup_thread.is_alive()
    assert owner._owner_conn is None
    assert owner._client_read_conn is None
