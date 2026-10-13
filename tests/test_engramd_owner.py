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

Tests 11-18 below are #3194 (P1-U4a, "the owner socket permission layer" --
0700 dir, peer credentials, stale-socket safety, end-to-end deadline),
which builds on top of this file's original 10. See
docs/2652-security-slice-design.md SS2 for the full design and SS5 for its
own numbered test list (tests 1-5 and 20 there map onto tests 11-18 here;
the mapping is noted per test below):

  11. The socket's parent directory (`run/`) is 0700 with no ACL entries
      after start() -- design doc SS5 test 4, upgraded per Borges's
      issue-#3194 ruling to assert on the ACL, not just the mode.
  12. The socket's own mode is not bind()'s 0755 default.
  13. Peer-uid mismatch -> Rejected(peer_identity) -- design doc SS5 test
      1. Needs a second uid; skipped with a named reason (SS6).
  14. The peer struct is parsed with struct.unpack_from, never a slice --
      design doc SS5 test 2.
  15. Stale-socket replacement refuses a symlink and a non-socket regular
      file -- design doc SS5 test 5.
  16. A silent peer (connects, sends nothing) also fails within the
      end-to-end deadline, as the control for tests 8/9's trickling-peer
      coverage -- design doc SS5 test 20's missing half.
  17. `_resolve_engram_home()` returns an absolute path for two different
      launch CWDs with the same relative ENGRAM_HOME -- #3152 item 2.
  18. A genuinely simultaneous double launch (the bind()-time race, not
      the pre-check liveness race already covered by test 1) surfaces as
      OwnerAlreadyRunning, not a raw OSError -- #3152 item 3.

Test 19 is #3194 ROUND 2 (a real gap found by Mira's security pre-review on
tests 8/9/16 above, then ruled on by Borges on the same issue thread):
tests 8/9/16 cover the end-to-end deadline's FRAMING side only
(_read_request_line()'s per-recv re-arming). `_dispatch()` itself ran
synchronously with no deadline awareness at all -- specifically
`_handle_snapshot()`'s single `self._owner_conn.backup(dest_conn)` call,
which ran to completion under `_owner_lock` regardless of the deadline, so
an overrun request held the lock past its budget and blocked every OTHER
request needing it (today: only another `snapshot`) -- the same
denial-of-service shape tests 8/9 exist to prevent, triggered by slow
EXECUTION instead of a slow peer. Design doc SS5 test 20b (added by
Borges's ruling, corrected once for a non-discriminating lock-release
assertion -- see the test's own docstring):

  19. snapshot() itself now runs under the SAME per-request deadline
      through execution, not just framing -- forced to overrun a tiny,
      per-instance snapshot budget mid-backup (a large seeded store,
      stepped backup via sqlite3.Connection.backup()'s own `pages=`/
      `progress=` mechanism, NOT a sleep inside the owner): no success
      response, the destination path does not exist, and -- the
      lock-release proof, NOT a `ping` (which never takes `_owner_lock`
      on this baseline) -- a FOLLOW-UP snapshot sized to fit its own
      (widened) budget succeeds.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engramd_read_registry as rr  # noqa: E402

# setfacl/getfacl availability -- tests 11 (ACL) rely on real POSIX-ACL
# tooling; the whole class of test is Linux-ACL-specific and skips
# cleanly (not a failure) when unavailable, matching this repo's own
# grant_viz_operator_acl.py convention (src/engram/tools/grant_viz_operator_acl.py).
_HAS_SETFACL = shutil.which("setfacl") is not None and shutil.which("getfacl") is not None

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


@requires_linux_owner
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


@requires_linux_owner
def test_stale_socket_and_pid_does_not_block_restart(engram_home):
    dead_pid = _spawn_and_kill()
    # Read the resolved socket path (now inside run/, #3194) off a
    # throwaway EngramdOwner instance rather than re-deriving it by hand,
    # so this test can't drift from EngramdOwner.__init__'s own
    # resolution again if that ever changes a second time.
    throwaway = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    stale_socket = throwaway.socket_path
    stale_pid_file = engram_home / eo.DEFAULT_PID_NAME

    # start() normally creates run/ itself -- this test binds a dummy
    # stale socket into it BEFORE calling the real start(), so the
    # directory has to exist first.
    stale_socket.parent.mkdir(parents=True, exist_ok=True)

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


@requires_linux_owner
def test_handshake_major_version_mismatch_is_rejected(owner_running):
    response = _raw_request(
        owner_running.socket_path,
        {"contract_version": "99.0", "method": "ping", "params": {}, "request_id": None},
    )
    assert response is not None
    assert response["status"] == "rejected"
    assert response["reason"] == "contract_version"
    assert response["remedy"]


@requires_linux_owner
def test_handshake_minor_version_mismatch_is_accepted(owner_running):
    # Owner's contract version is engramd_client.CONTRACT_VERSION ("1.1" --
    # MAJOR "1"). Sending "1.7" is a MINOR skew on the SAME major, which
    # check_contract_version() accepts (None -- proceed to serve the call).
    assert owner_running.contract_version == "1.1"
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


@requires_linux_owner
def test_ping_reports_ok_with_contract_version_and_store_path(owner_running):
    result = ec.read("ping", {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Ok)
    assert result.result["contract_version"] == owner_running.contract_version
    assert result.result["store_path"] == str(owner_running.store_path)


# ---------------------------------------------------------------------------
# Test 5 -- snapshot() writes a quiesced, consistent copy via SQLite's
# backup API; the copy opens and has the same row counts as the source.
# ---------------------------------------------------------------------------


@requires_linux_owner
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


@requires_linux_owner
def test_snapshot_missing_dest_path_param_is_rejected(owner_running):
    """Not one of the 10 numbered contract items -- covers this build's own
    documented design choice for snapshot()'s param shape (see the fairy
    handoff)."""
    result = ec.read("snapshot", {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Rejected)
    assert result.reason == "invalid_params"


@requires_linux_owner
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


@requires_linux_owner
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


@requires_linux_owner
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


@requires_linux_owner
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


@requires_linux_owner
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


@requires_linux_owner
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
# Test 11 -- the socket's parent directory (`run/`) is 0700 with no ACL
# entries after start() (#3194, design doc SS2.1, upgraded per Borges's
# issue-#3194 ruling to assert on the ACL, not just the mode).
# ---------------------------------------------------------------------------


def _getfacl_text(path: Path) -> str:
    return subprocess.run(
        ["getfacl", "-p", str(path)], capture_output=True, text=True, check=True
    ).stdout


@pytest.mark.skipif(
    not _HAS_SETFACL, reason="setfacl/getfacl not on PATH -- Linux-ACL-specific test"
)
@requires_linux_owner
def test_run_dir_is_0700_with_no_acl_entries_after_start(engram_home):
    # The fixture plants a DEFAULT ACL on the PARENT (engram_home) before
    # start() -- mirroring this fleet's real ~/.engram shape (measured on
    # the dispatching agent's own seat: `chmod 0700` alone does NOT strip
    # an inherited default ACL; see issue #3194's comment thread). Without
    # this, the test can't discriminate: it would pass even with the
    # ACL-strip code entirely absent, on a test host with nothing
    # inherited to begin with.
    import pwd

    username = pwd.getpwuid(os.getuid()).pw_name
    subprocess.run(
        ["setfacl", "-d", "-m", f"u:{username}:rw-", str(engram_home)],
        capture_output=True,
        text=True,
        check=True,
    )

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        run_dir = owner.socket_path.parent
        mode = stat.S_IMODE(run_dir.stat().st_mode)
        assert mode == 0o700, oct(mode)

        acl_text = _getfacl_text(run_dir)
        # No inherited DEFAULT ACL, and no named user/group entry -- only
        # the standard user::/group::/other:: mode-bit-equivalent lines
        # should remain (see the "acl after strip" measurement quoted in
        # this build's PR body).
        assert "default:" not in acl_text, acl_text
        assert f"user:{username}:" not in acl_text, acl_text
    finally:
        owner.stop()


# ---------------------------------------------------------------------------
# Test 12 -- the socket's own mode is not bind()'s 0755 default (#3194,
# design doc SS2.1/P5).
# ---------------------------------------------------------------------------


@requires_linux_owner
def test_socket_file_mode_is_not_bind_default(owner_running):
    mode = stat.S_IMODE(owner_running.socket_path.stat().st_mode)
    assert mode == eo.SOCKET_FILE_MODE, oct(mode)
    assert mode != 0o755, "socket left at bind()'s default mode"


# ---------------------------------------------------------------------------
# Test 13 -- peer-uid mismatch -> Rejected(peer_identity) (design doc SS5
# test 1; issue #3194, made an automated control by #3197).
#
# What it controls: a client whose uid differs from the owner's gets
# Rejected(reason="peer_identity") from EngramdOwner._check_peer_credentials
# (SO_PEERCRED read on every accepted connection). The control ran by hand
# once on 2026-09-29 (a sudo uid-0 client against a uid-1006 owner); this is
# that run, repeatable.
#
# Why it needs sudo: the socket is 0o700 in a 0o700 directory, so the only
# principal that can even connect from another uid is root. `sudo -n` (no
# password prompt) is therefore the only way to produce a foreign peer.
#
# Where it runs: GitHub's ubuntu-latest runners have passwordless sudo, so it
# runs in tests.yml (the full suite on the dev->main release PR) and in
# affected-tests.yml when dispatched by hand. Agent seats have no passwordless
# sudo, and there it SKIPS BY NAME (the reason says which precondition
# failed); it never passes silently.
#
# Discrimination: the SAME helper script is first run as this process's own
# uid and must get an accepted ping (so a helper that always errors cannot
# satisfy the foreign-uid assertion), and every assertion is on the helper's
# parsed typed result, never on stderr text. Mutant for a runner: make
# _check_peer_credentials return None unconditionally -> this test goes red.
#
# What it does NOT discriminate: the foreign peer here is always uid 0, and
# _check_peer_credentials rejects uid 0 through a SECOND branch (the
# `peer_uid == 0 and own_uid != 0` defence-in-depth clause) as well as the
# `peer_uid != own_uid` branch, with identical output. So this real-socket
# test proves a non-owner uid-0 peer is rejected by the live owner, but it
# cannot tell WHICH branch fired; removing only the mismatch branch would
# leave it green. The non-root mismatch branch is covered by the fake-conn
# cell in tests/test_engramd_owner_displacement_3240.py
# (test_snapshot_dest_rights_premise_peer_uid_equals_owner, peer uid own+1).
# ---------------------------------------------------------------------------

# Prints the typed result of one `ping` as a single JSON object. Run as a
# separate process (own uid under sudo), so it imports engramd_client the same
# way any client would.
_CROSSUID_HELPER = """\
import dataclasses, json, sys
from pathlib import Path
import engramd_client as ec
r = ec.read("ping", {}, timeout=5.0, socket_path=Path(sys.argv[1]))
print(json.dumps({"type": type(r).__name__, **dataclasses.asdict(r)}))
"""


def _run_crossuid_helper(socket_path: Path, *, via_sudo: bool) -> dict:
    # Explicit environment: sudo resets the caller's, and bytecode writes are
    # off so a root run cannot leave root-owned __pycache__ in the checkout.
    cmd = [
        "env",
        f"PYTHONPATH={_SRC_ENGRAM}",
        "PYTHONDONTWRITEBYTECODE=1",
        sys.executable,
        "-c",
        _CROSSUID_HELPER,
        str(socket_path),
    ]
    if via_sudo:
        cmd = ["sudo", "-n"] + cmd
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, (
        f"helper (via_sudo={via_sudo}) exited {proc.returncode}; stderr={proc.stderr!r}"
    )
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AssertionError(
            f"helper (via_sudo={via_sudo}) printed no JSON result: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        ) from exc


@requires_linux_owner
def test_peer_uid_mismatch_is_rejected(owner_running):
    if os.getuid() == 0:
        pytest.skip(
            "cross-uid control needs a foreign uid: this process already runs as uid 0, "
            "so sudo cannot produce a different uid (#3197)"
        )
    if shutil.which("sudo") is None:
        pytest.skip("cross-uid control needs sudo: `sudo` is not on PATH (#3197)")
    try:
        probe = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        pytest.skip("cross-uid control needs passwordless sudo: `sudo -n true` timed out (#3197)")
    if probe.returncode != 0:
        pytest.skip(
            "cross-uid control needs passwordless sudo: `sudo -n true` failed "
            f"(exit {probe.returncode}) -- runs on GitHub runners, skips on agent seats (#3197)"
        )

    own_uid = os.getuid()

    # Control (discrimination): the same helper as OUR uid gets an accepted ping.
    own = _run_crossuid_helper(owner_running.socket_path, via_sudo=False)
    assert own["type"] == "Ok", f"same-uid helper did not get an accepted ping: {own!r}"
    assert own["result"]["store_path"] == str(owner_running.store_path), own

    # Act: the same helper as uid 0 is refused by name.
    foreign = _run_crossuid_helper(owner_running.socket_path, via_sudo=True)
    assert foreign["type"] == "Rejected", f"foreign-uid client was not rejected: {foreign!r}"
    assert foreign["reason"] == "peer_identity", foreign
    assert "uid 0" in foreign["detail"], foreign
    assert f"uid {own_uid}" in foreign["detail"], foreign


# ---------------------------------------------------------------------------
# Test 14 -- the peer struct is parsed with struct.unpack_from, and a
# too-short buffer RAISES rather than silently decoding to uid 0 (design
# doc SS5 test 2). Runs anywhere, no privileges: exercises the PARSE FORM
# directly against a manufactured short buffer via a fake conn stub,
# rather than a real socket connection -- a genuine same-uid SO_PEERCRED
# read on Linux always returns the full 12-byte buffer, so it cannot
# itself distinguish struct.unpack_from from the slice form
# int.from_bytes(raw[4:8], "little").
# ---------------------------------------------------------------------------


class _FakeConnShortPeercred:
    """A conn stub whose getsockopt() returns a too-short SO_PEERCRED
    buffer -- standing in for the P3-primitive class of failure (wrong
    socket level/option returns a shorter-than-expected buffer) without
    needing a second platform (Darwin) to trigger it for real."""

    def getsockopt(self, level, optname, buflen):
        return b"\x00\x00\x00\x00"  # 4 bytes -- shorter than PEERCRED_STRUCT_SIZE (12)


@requires_linux_owner  # reads socket.SO_PEERCRED, part of the owner's predicate
def test_peer_struct_parse_form_raises_on_short_buffer(engram_home):
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    with pytest.raises(struct.error):
        owner._check_peer_credentials(_FakeConnShortPeercred())


# ---------------------------------------------------------------------------
# Test 15 -- stale-socket replacement refuses a symlink and a non-socket
# regular file (design doc SS5 test 5).
# ---------------------------------------------------------------------------


@requires_linux_owner
def test_stale_socket_replacement_refuses_symlink(engram_home):
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.socket_path.parent.mkdir(parents=True, exist_ok=True)
    target = engram_home / "elsewhere.txt"
    target.write_text("not a socket")
    owner.socket_path.symlink_to(target)

    with pytest.raises(eo.OwnerStalePathUnsafe):
        owner.start(install_signal_handlers=False)

    # Refused, not unlinked-and-bound-through -- the symlink and its
    # target are both untouched.
    assert owner.socket_path.is_symlink()
    assert target.read_text() == "not a socket"


@requires_linux_owner
def test_stale_socket_replacement_refuses_non_socket_regular_file(engram_home):
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.socket_path.parent.mkdir(parents=True, exist_ok=True)
    owner.socket_path.write_text("just a regular file, not a socket")

    with pytest.raises(eo.OwnerStalePathUnsafe):
        owner.start(install_signal_handlers=False)

    assert owner.socket_path.read_text() == "just a regular file, not a socket"


# ---------------------------------------------------------------------------
# Test 16 -- a silent peer (connects, sends nothing at all) also fails
# within the end-to-end deadline -- the control half of design doc SS5
# test 20 that tests 8/9 above (the TRICKLING peer, and "doesn't block
# another client") do not themselves cover.
# ---------------------------------------------------------------------------


def _silent_peer_and_measure(socket_path: Path, bound: float) -> tuple[float, bytes]:
    """Connect and send NOTHING at all, then read back whatever the peer
    sends (expected: nothing -- a closed connection) once past the
    deadline. Mirrors _trickle_and_measure's shape (connect, wait,
    read-back) but with no sends -- a genuinely silent peer, not a
    slowly-trickling one."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    start = time.monotonic()
    sock.settimeout(bound * 3)
    try:
        response = sock.recv(4096)
    except OSError:
        response = b""
    elapsed = time.monotonic() - start
    sock.close()
    return elapsed, response


@requires_linux_owner
def test_silent_peer_is_dropped_within_deadline_and_owner_stays_up(owner_running):
    bound = owner_running.request_deadline
    elapsed, response = _silent_peer_and_measure(owner_running.socket_path, bound)

    assert response == b""  # dropped without ever completing a response
    assert elapsed < bound * 2.5, f"elapsed={elapsed}, bound={bound}"

    # The owner keeps serving other clients afterward.
    result = ec.read("ping", {}, timeout=1.0, socket_path=owner_running.socket_path)
    assert isinstance(result, ec.Ok)


# ---------------------------------------------------------------------------
# Test 17 -- _resolve_engram_home() returns an absolute path (#3152 item
# 2). Two different launch CWDs with the SAME relative ENGRAM_HOME string
# resolve to the identical absolute path.
# ---------------------------------------------------------------------------


def test_resolve_engram_home_returns_absolute_path_across_cwds(tmp_path, monkeypatch):
    # launcher-a and launcher-b are SIBLING directories under a shared
    # parent, so the SAME relative string "../engramhome" reaches the
    # SAME absolute target from either one -- this is the realistic case
    # #3152 item 2 names: two launchers at the same depth relative to a
    # shared seat home, each launched from a different CWD.
    launcher_a = tmp_path / "launcher-a"
    launcher_b = tmp_path / "launcher-b"
    launcher_a.mkdir()
    launcher_b.mkdir()
    expected_target = Path(os.path.abspath(str(tmp_path / "engramhome")))

    monkeypatch.setenv("ENGRAM_HOME", "../engramhome")

    monkeypatch.chdir(launcher_a)
    resolved_a = eo._resolve_engram_home(None)

    monkeypatch.chdir(launcher_b)
    resolved_b = eo._resolve_engram_home(None)

    assert resolved_a.is_absolute()
    assert resolved_b.is_absolute()
    assert resolved_a == resolved_b == expected_target


# ---------------------------------------------------------------------------
# Test 18 -- a genuinely simultaneous double launch (the bind()-TIME race,
# NOT test 1's pre-check liveness race) surfaces as OwnerAlreadyRunning,
# never a raw OSError (#3152 item 3).
# ---------------------------------------------------------------------------


@requires_linux_owner
def test_simultaneous_double_launch_surfaces_as_owner_already_running(engram_home):
    """Read test_second_instance_refuses_to_start_and_first_remains_unaffected
    (test 1) first: that test goes through two full EngramdOwner.start()
    calls and is caught by the PRE-BIND liveness check
    (_check_stale_socket_or_raise, via a live pidfile). This test
    deliberately does NOT exercise that path -- it binds a raw "winner"
    socket to the owner's OWN resolved socket_path in the narrow window
    AFTER the owner's own pre-bind checks have already run and found
    nothing there (so they do not raise), but BEFORE the owner's own
    bind() call -- simulating a genuinely concurrent second process that
    wins the real kernel-level bind() race. The owner must translate the
    resulting OSError(errno=EADDRINUSE) into the handled
    OwnerAlreadyRunning path (_bind_socket_or_raise), not let it
    propagate raw.

    #3194, Mira's run/-symlink ordering finding on #3201: `start()` now
    calls `_harden_run_dir()` BEFORE `_check_stale_socket_or_raise()` (the
    run/ directory must be verified safe before anything touches
    `self.socket_path`, which traverses it as a path component) -- so the
    injection point below moved from `_harden_run_dir` to
    `_check_stale_socket_or_raise`, which is now the step immediately
    preceding the owner's own store-connect+bind sequence."""
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    winner_holder: dict = {}

    real_check_stale_socket_or_raise = owner._check_stale_socket_or_raise

    def _check_then_let_a_concurrent_winner_bind_first():
        # _check_stale_socket_or_raise() is the last step before the
        # owner opens its store connection and attempts its own bind() --
        # inserting the "concurrent" winner's bind here, after the
        # owner's own pre-bind checks have already run and found nothing
        # there, but before the owner's own bind() call, is what makes
        # this a genuine bind()-time race rather than a pre-check hit.
        real_check_stale_socket_or_raise()
        winner = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        winner.bind(str(owner.socket_path))
        winner.listen(1)
        winner_holder["winner"] = winner

    owner._check_stale_socket_or_raise = _check_then_let_a_concurrent_winner_bind_first
    try:
        with pytest.raises(eo.OwnerAlreadyRunning):
            owner.start(install_signal_handlers=False)
    finally:
        winner = winner_holder.get("winner")
        if winner is not None:
            winner.close()


# ---------------------------------------------------------------------------
# Test 19 -- #3194 ROUND 2 (design doc SS5 test 20b, per Borges's ruling on
# issue #3194's comment thread, following Mira's security pre-review
# finding): the end-to-end deadline (item 8) now covers DISPATCH/EXECUTION,
# not just frame acquisition/response delivery. Tests 8/9/16 above are
# framing-only -- _read_request_line()'s per-recv re-arming -- and never
# exercise `_dispatch()` itself. On the U3/round-1 baseline,
# `_handle_connection()` computed `deadline` once at accept but called
# `self._dispatch(request)` synchronously with no deadline awareness at
# all, checking `remaining` only AFTER dispatch returned. `snapshot()`'s
# single `self._owner_conn.backup(dest_conn)` call ran to completion under
# `_owner_lock`, held for the WHOLE call -- an overrun request didn't just
# reply late, it held the lock past the deadline and blocked every OTHER
# request needing it (today: only another `snapshot`). Same
# denial-of-service shape test 9 exists to prevent, triggered by slow
# EXECUTION instead of a slow peer.
#
# Borges's ORIGINAL test-20b design used a follow-up `ping` as the
# lock-release proof; he self-corrected in his very next comment (issue
# #3194) -- on this baseline `ping` never takes `_owner_lock` (only
# `_handle_snapshot` does), so "a following ping succeeds" passes even if
# an aborted snapshot never released the lock. The amended, discriminating
# proof below uses a SECOND `snapshot` instead; `ping` is kept only as a
# cheap liveness extra, never the proof.
# ---------------------------------------------------------------------------


def _seed_large_store(store_path: Path, *, rows: int = 40_000, blob_size: int = 2000) -> None:
    """A store large enough (~80 MB on this seat: rows * (blob_size + row
    overhead)) that a `pages=SNAPSHOT_BACKUP_STEP_PAGES`-stepped backup
    takes on the order of 100+ milliseconds and ~200 progress-callback
    invocations, rather than completing in a single step -- measured by
    the dispatching agent (~76ms, 201 callbacks at pages=100) before this
    fairy spec was written. Needed so a tiny snapshot_request_deadline
    genuinely overruns MID-backup (proving execution-phase enforcement,
    not just a same-instant "was it already too late" check) rather than
    completing before the deadline is ever exceeded. Deliberately real
    work, not a sleep inside the owner (Borges: "do not sleep inside the
    owner")."""
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, blob TEXT)")
    big_val = "x" * blob_size
    conn.executemany("INSERT INTO widgets (blob) VALUES (?)", [(big_val,) for _ in range(rows)])
    conn.commit()
    conn.close()


@requires_linux_owner
def test_snapshot_deadline_exceeded_aborts_work_and_releases_lock(engram_home):
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    _seed_large_store(store_path)

    # A tiny snapshot budget -- small enough that even this seat's fast
    # ~76ms/~200-callback backup (see _seed_large_store's docstring)
    # overruns it partway through, well before completion, so the abort
    # genuinely interrupts in-progress work rather than racing a
    # near-instant backup to the finish line.
    tiny_snapshot_deadline = eo.SNAPSHOT_RESPONSE_RESERVE + 0.02  # round 6: 0.02s of EXECUTION budget
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=tiny_snapshot_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "overrun-snap.db"
        # #3200's security re-read (Mira) found round 2's original version
        # of this test left the overrun allowance unbounded ("deadline plus
        # one step" with no stated/measured step size). Borges's fold: name
        # the stop step and its MEASURED maximum wall time
        # (SNAPSHOT_BACKUP_STEP_MAX_WALL_TIME, see its own docstring for the
        # measurement), and assert completion by deadline + that stated
        # allowance -- not an unbounded "eventually". A small
        # CLIENT_OVERHEAD_MARGIN is added on top for this test's own
        # AF_UNIX round-trip + thread-scheduling noise, kept separate from
        # (and much smaller than) the production allowance itself so it
        # isn't mistaken for a second invented number.
        client_overhead_margin = 0.05  # seconds -- test-harness slack only
        overrun_start = time.monotonic()
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=tiny_snapshot_deadline + 5.0,
            socket_path=owner.socket_path,
        )
        overrun_elapsed = time.monotonic() - overrun_start
        # No success response -- whether the client sees this as
        # Unavailable (the owner dropped the connection without sending
        # anything, per _handle_connection's own post-dispatch
        # `remaining <= 0` check) or as a Rejected(deadline_exceeded) in
        # the rare case where some budget remained, isinstance(result,
        # ec.Ok) must be False either way.
        assert not isinstance(result, ec.Ok), result

        assert not dest_path.exists(), (
            "an aborted snapshot must leave no destination file, partial or otherwise"
        )

        max_allowed = (
            (tiny_snapshot_deadline - eo.SNAPSHOT_RESPONSE_RESERVE)
            + eo.SNAPSHOT_BACKUP_STEP_MAX_WALL_TIME
            + client_overhead_margin
        )
        assert overrun_elapsed <= max_allowed, (
            f"snapshot() must abort within its stated, measured allowance "
            f"(deadline={tiny_snapshot_deadline} + step_bound="
            f"{eo.SNAPSHOT_BACKUP_STEP_MAX_WALL_TIME} + margin="
            f"{client_overhead_margin} = {max_allowed}), took {overrun_elapsed}"
        )

        # The lock-release proof (Borges's AMENDED test 20b -- a second
        # snapshot, not a ping). Widen the budget on the SAME owner
        # instance (snapshot_request_deadline is a plain, mutable
        # attribute -- see its own docstring) rather than constructing a
        # second owner, so this genuinely proves the FIRST call's abort
        # released _owner_lock rather than merely testing a fresh lock.
        owner.snapshot_request_deadline = 5.0
        second_dest_path = engram_home / "followup-snap.db"
        second_result = ec.read(
            "snapshot",
            {"dest_path": str(second_dest_path)},
            timeout=5.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(second_result, ec.Ok), second_result
        assert second_dest_path.exists()

        # Cheap liveness extra (Borges: "may still be included ... if you
        # like, but it is NOT the lock-release proof").
        ping_result = ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path)
        assert isinstance(ping_result, ec.Ok)
    finally:
        owner.stop()


def test_snapshot_request_deadline_default_exceeds_base_request_deadline():
    """Not one of the numbered contract items -- a simple property
    assertion (per the fairy spec: "don't over-invest here") that the
    production default for snapshot()'s own budget is genuinely larger
    than the base per-request deadline, matching Borges's ruling that 5s
    (DEFAULT_REQUEST_DEADLINE) is not a realistic backup budget for a real
    knowledge.db."""
    assert eo.SNAPSHOT_REQUEST_DEADLINE > eo.DEFAULT_REQUEST_DEADLINE
    assert eo.SNAPSHOT_REQUEST_DEADLINE == 30.0
    assert eo.EngramdOwner(Path("/tmp/does-not-need-to-exist-for-this-check")).snapshot_request_deadline == (
        eo.SNAPSHOT_REQUEST_DEADLINE
    )


# ---------------------------------------------------------------------------
# Test 20 -- #3194, Mira's security review on #3201's exact head 18cd14b2:
# a pre-existing `run -> elsewhere` symlink must be refused before any
# hardening (chmod/ACL-strip) touches it, and refused BEFORE
# _check_stale_socket_or_raise() gets a chance to unlink a stale socket
# under the symlink's target (the ordering finding, same issue thread).
# Mirrors Mira's own exact-head reproduction fixture precisely.
# ---------------------------------------------------------------------------


@requires_linux_owner
def test_run_dir_symlink_is_refused_target_untouched_no_socket_bound(engram_home):
    outside = engram_home / "outside"
    outside.mkdir(mode=0o755)
    outside_before_mode = stat.S_IMODE(outside.stat().st_mode)
    # Resolve the real run/ path the same way EngramdOwner itself does,
    # via a throwaway instance, rather than hard-coding the string "run"
    # a second time.
    probe = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    run_dir_path = probe.socket_path.parent
    run_dir_path.symlink_to(outside, target_is_directory=True)

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    with pytest.raises(eo.OwnerStalePathUnsafe):
        owner.start(install_signal_handlers=False)

    # The symlink itself is untouched -- still a symlink, still pointing
    # at `outside`.
    assert run_dir_path.is_symlink()
    assert os.readlink(run_dir_path) == str(outside)

    # `outside`'s mode/ownership are byte-for-byte unchanged -- the exact
    # thing Mira's fixture showed the OLD code mutating (0755 -> 0700).
    assert stat.S_IMODE(outside.stat().st_mode) == outside_before_mode == 0o755

    # No socket was bound anywhere under `outside/`.
    assert not any(outside.iterdir()), list(outside.iterdir())


# ---------------------------------------------------------------------------
# Test 21 -- #3194, Mira's third security finding + Borges's stage-then-
# atomic-replace ruling on #3201's 18cd14b2: a deadline-aborted snapshot
# must never touch a pre-existing file at dest_path, an absent dest_path
# must stay absent on abort, and a SUCCESSFUL snapshot must atomically
# replace a pre-existing dest_path (not unlink-then-recreate). The
# mutation (write straight to dest + unlink on abort, the ORIGINAL
# round-2 shape) must redden the preserve-existing-file case specifically.
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@requires_linux_owner
def test_snapshot_abort_preserves_pre_existing_destination_byte_identical(engram_home):
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    _seed_large_store(store_path)

    # A real, non-trivial "prior" file at the destination -- a sentinel
    # SQLite DB with its own content, exactly Mira's exact-head repro
    # shape (an 8192-byte existing.db with a sentinel row).
    dest_path = engram_home / "existing.db"
    sentinel_conn = sqlite3.connect(str(dest_path))
    sentinel_conn.execute("CREATE TABLE sentinel (marker TEXT)")
    sentinel_conn.execute("INSERT INTO sentinel VALUES ('do-not-touch')")
    sentinel_conn.commit()
    sentinel_conn.close()
    sentinel_sha = _sha256(dest_path)
    assert dest_path.stat().st_size > 0

    tiny_snapshot_deadline = eo.SNAPSHOT_RESPONSE_RESERVE + 0.02  # round 6: 0.02s of EXECUTION budget
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=tiny_snapshot_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=tiny_snapshot_deadline + 5.0,
            socket_path=owner.socket_path,
        )
        assert not isinstance(result, ec.Ok), result

        # Byte-identical -- not just "still exists". A weaker "exists"
        # check would pass even if the abort path had truncated or
        # partially overwritten it.
        assert _sha256(dest_path) == sentinel_sha, (
            "a pre-existing destination must be untouched by an aborted snapshot"
        )

        # No stray temp file left behind either.
        leftovers = [
            p for p in engram_home.iterdir()
            if p.name.startswith(f".{dest_path.name}.") and p.name.endswith(".snapshot-tmp")
        ]
        assert leftovers == [], leftovers
    finally:
        owner.stop()


@requires_linux_owner
def test_snapshot_abort_on_absent_destination_leaves_it_absent(engram_home):
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    _seed_large_store(store_path)

    dest_path = engram_home / "never-existed.db"
    assert not dest_path.exists()

    tiny_snapshot_deadline = eo.SNAPSHOT_RESPONSE_RESERVE + 0.02  # round 6: 0.02s of EXECUTION budget
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=tiny_snapshot_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=tiny_snapshot_deadline + 5.0,
            socket_path=owner.socket_path,
        )
        assert not isinstance(result, ec.Ok), result
        assert not dest_path.exists(), (
            "an absent destination must stay absent after an aborted snapshot"
        )
    finally:
        owner.stop()


@requires_linux_owner
def test_snapshot_success_atomically_replaces_pre_existing_destination(engram_home):
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE real_data (x INTEGER)")
    conn.execute("INSERT INTO real_data VALUES (42)")
    conn.commit()
    conn.close()

    dest_path = engram_home / "existing.db"
    sentinel_conn = sqlite3.connect(str(dest_path))
    sentinel_conn.execute("CREATE TABLE sentinel (marker TEXT)")
    sentinel_conn.execute("INSERT INTO sentinel VALUES ('should-be-replaced')")
    sentinel_conn.commit()
    sentinel_conn.close()

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=5.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(result, ec.Ok), result

        # The OLD sentinel content is gone -- replaced, not merged with.
        check_conn = sqlite3.connect(str(dest_path))
        tables = {
            row[0]
            for row in check_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "sentinel" not in tables, tables
        assert "real_data" in tables, tables
        assert check_conn.execute("SELECT x FROM real_data").fetchone() == (42,)
        check_conn.close()

        leftovers = [
            p for p in engram_home.iterdir()
            if p.name.startswith(f".{dest_path.name}.") and p.name.endswith(".snapshot-tmp")
        ]
        assert leftovers == [], leftovers
    finally:
        owner.stop()


# ---------------------------------------------------------------------------
# Round 5 (#3194/#3201, Borges's REVISED protocol -- his first ruling on
# Mira's fsync/deadline findings was superseded TWICE, in-thread, after
# Mira found a real gap in it: moving fsync/os.replace off _owner_lock
# (round 4, above) bounds *other* clients, but gives no finite bound on
# *the request itself* -- the request thread and its temp file were held
# for as long as the disk took, and concurrent slow fsyncs could
# accumulate without limit. Round 4's tests (d)/(e)/(f) -- named
# test_snapshot_fsync_delay_past_deadline_..., test_snapshot_fsync_oserror_...,
# test_snapshot_deadline_exceeded_between_fsync_and_replace_... -- exercised
# an inline-fsync shape that no longer exists; they are REMOVED here rather
# than adapted, and replaced by the (d)-(h) tests below, which exercise the
# bounded finalize worker, the SNAPSHOT_FINALIZE_SLOTS capacity gate, and
# the pending/committing/committed/aborted commit-state protocol instead.
# Letters (d)-(h) are the fairy spec's own naming (round 5's spec continues
# round 4's (d)/(e)/(f) lettering rather than restarting it, then adds (g)
# and (h) new).
# ---------------------------------------------------------------------------


def _patch_slow_fsync(monkeypatch: pytest.MonkeyPatch, delay: float) -> None:
    """Wrap os.fsync so it sleeps `delay` seconds of REAL time (not a
    mocked clock) before performing the real fsync -- models a slow disk
    (Borges: "the test-only fsync delay is fine here because it models the
    disk, not the owner"). Patched on the `os` module object itself, not an
    owner attribute: `_handle_snapshot`'s finalize worker calls
    `os.fsync()` directly, and `engramd_owner`'s own `import os` binds the
    SAME module object (Python caches modules in sys.modules), so this
    patch is visible there too. Shared seam for tests (d)/(e)/(f) below."""
    real_fsync = os.fsync

    def _slow_fsync(fd: int) -> None:
        time.sleep(delay)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _slow_fsync)


def _patch_raising_fsync(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Wrap os.fsync so it raises `exc` instead of performing a real fsync
    -- models a hard disk failure. Same patch target as _patch_slow_fsync
    above (the `os` module object, not an owner attribute). Used by test
    (h)."""

    def _raising_fsync(fd: int) -> None:
        raise exc

    monkeypatch.setattr(os, "fsync", _raising_fsync)


def _patch_slow_replace(monkeypatch: pytest.MonkeyPatch, delay: float) -> None:
    """Wrap os.replace so it sleeps `delay` seconds of REAL time before
    performing the real rename -- used ONLY by test (g), to straddle the
    deadline specifically at the commit point (state == "committing")
    rather than during fsync (that's test (f)'s shape). Same patch-target
    reasoning as _patch_slow_fsync above."""
    real_replace = os.replace

    def _slow_replace(src, dst) -> None:
        time.sleep(delay)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _slow_replace)


def _leftover_temp_files(directory: Path, dest_name: str) -> list[Path]:
    """The same `.{dest.name}.*.snapshot-tmp` leftover check the round-3
    tests above each inline -- factored out here since tests (d)-(h) below
    all need it at least once."""
    return [
        p for p in directory.iterdir()
        if p.name.startswith(f".{dest_name}.") and p.name.endswith(".snapshot-tmp")
    ]


def _expected_full_snapshot_sha(store_path: Path) -> str:
    """Independently produce the exact bytes a COMPLETE, uninterrupted
    snapshot of `store_path` would contain -- via the same sqlite3 backup
    API `_handle_snapshot()` itself uses -- so test (g) can assert dest's
    final sha256 against a REAL third value, per the fairy spec's own
    instruction ("construct the assertion so it would fail on any THIRD
    value ... not just check 'it's one of two strings' loosely") rather
    than merely asserting membership in a hand-picked pair of strings."""
    fd, tmp = tempfile.mkstemp(
        dir=str(store_path.parent), suffix=".expected-full-snapshot.db"
    )
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        src_conn = sqlite3.connect(str(store_path))
        dest_conn = sqlite3.connect(str(tmp_path))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            src_conn.close()
        return _sha256(tmp_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


@requires_linux_owner
def test_snapshot_response_time_bounded_by_deadline_under_slow_fsync(engram_home, monkeypatch):
    """(d) Borges's round-5 ruling, item 2: the finalize work (fsync then
    os.replace) runs on a worker thread; the REQUEST thread waits on the
    worker's completion event with timeout = time remaining, so the
    RESPONSE time is owner-bounded (deadline + a small epsilon) no matter
    how slow the disk is. Same slow-fsync injection seam as round 4's own
    tests.

    Discriminating mutation (fairy spec's own instruction): make the
    request thread JOIN the worker thread directly with no timeout
    (blocks until the worker finishes) instead of waiting on the
    completion event bounded by `remaining` -- the response-time assertion
    must go RED.
    """
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    fsync_delay = 2.0  # comfortably longer than the deadline below
    _patch_slow_fsync(monkeypatch, fsync_delay)

    tiny_snapshot_deadline = eo.SNAPSHOT_RESPONSE_RESERVE + 0.02  # round 6: 0.02s of EXECUTION budget
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=tiny_snapshot_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "existing.db"
        sentinel_conn = sqlite3.connect(str(dest_path))
        sentinel_conn.execute("CREATE TABLE sentinel (marker TEXT)")
        sentinel_conn.execute("INSERT INTO sentinel VALUES ('do-not-touch')")
        sentinel_conn.commit()
        sentinel_conn.close()
        sentinel_sha = _sha256(dest_path)

        # Test-only response-time slack -- OS/thread-scheduling jitter for
        # the Event.wait()/socket round-trip itself, NOT a new production
        # allowance (the round-5 protocol's whole point is that the
        # response no longer depends on the disk at all -- see the fairy
        # handoff's ambiguous-decision log). Modeled on the existing
        # client_overhead_margin idiom (test 19 / round 4's own delay
        # test), deliberately much smaller than fsync_delay above.
        response_time_epsilon = 0.3

        start = time.monotonic()
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=fsync_delay + 5.0,
            socket_path=owner.socket_path,
        )
        elapsed = time.monotonic() - start

        assert not isinstance(result, ec.Ok), result
        max_allowed = (tiny_snapshot_deadline - eo.SNAPSHOT_RESPONSE_RESERVE) + response_time_epsilon
        assert elapsed <= max_allowed, (
            f"response took {elapsed}s, expected by deadline "
            f"({tiny_snapshot_deadline}) + epsilon ({response_time_epsilon}) "
            f"= {max_allowed}s -- the owner must not block the response on "
            "the slow fsync"
        )
        assert _sha256(dest_path) == sentinel_sha, (
            "dest must be byte-identical to before the call while the "
            "delayed fsync is still outstanding"
        )

        # Once the delayed fsync eventually returns, the worker has
        # exited: no temp file remains, and the slot it held is free
        # again (proven by the follow-up snapshot succeeding below).
        time.sleep(fsync_delay + 0.5)
        assert _leftover_temp_files(engram_home, dest_path.name) == []

        second_dest_path = engram_home / "followup-snap.db"
        owner.snapshot_request_deadline = 5.0
        second_result = ec.read(
            "snapshot",
            {"dest_path": str(second_dest_path)},
            timeout=fsync_delay + 5.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(second_result, ec.Ok), second_result
        assert second_dest_path.exists()
    finally:
        owner.stop()


@requires_linux_owner
def test_snapshot_capacity_gate_rejects_busy_without_starting_backup(engram_home, monkeypatch):
    """(e) Borges's round-5 ruling, item 3: SNAPSHOT_FINALIZE_SLOTS bounds
    concurrent finalize work. With that many finalizes already held (each
    past its own backup step and into a delayed fsync), the NEXT
    snapshot() call must be refused `rejected/busy` immediately, WITHOUT
    ever starting a backup -- no lock taken, no bytes copied into a new
    temp file for it.

    Discriminating mutation (fairy spec's own instruction): remove/bypass
    the slot non-blocking-acquire check -- the busy call must then either
    succeed or hang instead of returning `busy` immediately, and this test
    must go RED.
    """
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    fsync_delay = 5.0  # long enough to hold every slot for this whole test
    _patch_slow_fsync(monkeypatch, fsync_delay)

    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        # Generous -- these occupier calls are meant to eventually
        # SUCCEED (once their fsync delay elapses), not time out.
        snapshot_request_deadline=fsync_delay + 10.0,
    )
    owner.start(install_signal_handlers=False)
    try:
        occupier_results: list = []
        occupier_threads = []
        for i in range(eo.SNAPSHOT_FINALIZE_SLOTS):
            dest_i = engram_home / f"occupier-{i}.db"

            def _run(dest=dest_i) -> None:
                occupier_results.append(
                    ec.read(
                        "snapshot",
                        {"dest_path": str(dest)},
                        timeout=fsync_delay + 15.0,
                        socket_path=owner.socket_path,
                    )
                )

            t = threading.Thread(target=_run)
            t.start()
            occupier_threads.append(t)

        # Give every occupier time to accept, finish its (near-instant,
        # tiny-store) backup, and land inside its injected fsync sleep --
        # i.e. holding its slot -- before the busy call below.
        time.sleep(0.5)

        busy_dest = engram_home / "busy-attempt.db"
        busy_start = time.monotonic()
        busy_result = ec.read(
            "snapshot",
            {"dest_path": str(busy_dest)},
            timeout=2.0,
            socket_path=owner.socket_path,
        )
        busy_elapsed = time.monotonic() - busy_start

        assert isinstance(busy_result, ec.Rejected), busy_result
        assert busy_result.reason == "busy", busy_result
        # Near-instant -- no backup, no lock wait, nothing disk-bound.
        assert busy_elapsed < 1.0, (
            f"busy rejection took {busy_elapsed}s -- should be immediate, "
            "not gated on a backup or a lock"
        )
        # No temp file was ever created for the busy call's own dest --
        # i.e. no backup was ever started for it.
        assert _leftover_temp_files(engram_home, busy_dest.name) == []
        assert not busy_dest.exists()

        for t in occupier_threads:
            t.join(timeout=fsync_delay + 15.0)
            assert not t.is_alive(), "an occupier snapshot call never completed"
        for result in occupier_results:
            assert isinstance(result, ec.Ok), result
    finally:
        owner.stop()


@requires_linux_owner
def test_snapshot_deadline_abandoned_before_commit_never_replaces(engram_home, monkeypatch):
    """(f) Borges's round-5 ruling, item 4: when the deadline passes while
    the finalize worker is still inside fsync (before it ever reaches the
    state-lock commit-gate check), the REQUEST thread's own timeout sets
    `abandoned` and returns immediately; the worker's later state check
    sees `abandoned` and never issues os.replace(). No success is ever
    sent -- the response comes from the request thread's own "abandoned"
    branch, well before the worker even finishes fsync; dest never
    changes; no temp remains once the worker eventually exits.

    Discriminating mutation (fairy spec's own instruction): the worker
    checks only the deadline it captured at start (or nothing) instead of
    the LIVE `abandoned` flag under the state lock -- dest must change
    AFTER the response was already sent, and this test must go RED.

    NOT asserting `isinstance(result, ec.Rejected)` here, deliberately
    (see the fairy handoff's ambiguous-decision log for the full finding):
    `_handle_connection()`'s own pre-existing post-dispatch send-gate
    (`remaining = deadline - time.monotonic(); if remaining <= 0: return`,
    unchanged by this round and out of this spec's scope) drops the
    connection WITHOUT sending anything whenever the response is composed
    at-or-after the SAME deadline it's gated on -- which a timeout-driven
    "abandoned" response, by construction, always is. This is the EXACT
    same pre-existing behavior this file's own module docstring already
    documents for round 2/3/4's backup-phase and fsync/replace-phase
    deadline aborts ("that dict will typically never actually reach the
    wire ... it mainly matters for this method's own direct testability")
    -- confirmed empirically here too (5/5 repeat runs, deterministic, not
    flaky: the client always observes `ec.Unavailable`, never
    `ec.Rejected`, for this exact scenario). `not isinstance(result,
    ec.Ok)` is what tests 19 and the round-3 abort tests already assert
    for the identical reason, and is exactly what the spec's own (f)
    wording requires ("no success is ever sent") -- it does not ask for a
    specific Rejected reason.
    """
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    fsync_delay = 1.0  # comfortably longer than the deadline below
    _patch_slow_fsync(monkeypatch, fsync_delay)

    tiny_snapshot_deadline = eo.SNAPSHOT_RESPONSE_RESERVE + 0.02  # round 6: 0.02s of EXECUTION budget
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=tiny_snapshot_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "existing.db"
        sentinel_conn = sqlite3.connect(str(dest_path))
        sentinel_conn.execute("CREATE TABLE sentinel (marker TEXT)")
        sentinel_conn.execute("INSERT INTO sentinel VALUES ('do-not-touch')")
        sentinel_conn.commit()
        sentinel_conn.close()
        sentinel_sha = _sha256(dest_path)

        response_start = time.monotonic()
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=fsync_delay + 5.0,
            socket_path=owner.socket_path,
        )
        response_elapsed = time.monotonic() - response_start

        assert not isinstance(result, ec.Ok), result
        # The response itself came back well before the worker's fsync
        # delay elapsed -- proving it came from the request thread's own
        # "abandoned" branch, not from waiting out the worker.
        assert response_elapsed < fsync_delay, (
            f"response took {response_elapsed}s -- expected it to return "
            f"promptly at the deadline, well before the {fsync_delay}s "
            "fsync delay elapses"
        )
        assert _sha256(dest_path) == sentinel_sha, (
            "dest must never change once the request abandons a call "
            "before its commit reservation"
        )

        # Let the worker's delayed fsync finish and the worker exit.
        time.sleep(fsync_delay + 0.5)
        assert _sha256(dest_path) == sentinel_sha, (
            "dest must still be untouched even after the abandoned "
            "worker eventually finishes"
        )
        assert _leftover_temp_files(engram_home, dest_path.name) == []
    finally:
        owner.stop()


@requires_linux_owner
def test_snapshot_indeterminate_commit_when_deadline_lands_during_replace(engram_home, monkeypatch):
    """(g) Borges's round-5 ruling, item 4's R1 residual: a test-only
    delay is injected specifically inside os.replace, straddling the
    deadline, so the request thread observes the worker already in
    `"committing"` (a commit was reserved before the deadline; the rename
    may be issued and completed after it) once its own wait times out. The response must carry
    `commit: "indeterminate"` -- distinct from test (f)'s "never touched"
    shape, which carries no `commit` key at all (the fairy's own design
    choice for distinguishing the two response shapes -- see the
    handoff's ambiguous-decision log). dest's sha256, once everything
    settles, must be EITHER the pre-existing file's hash OR the complete
    new snapshot's hash (computed independently via
    _expected_full_snapshot_sha, not assumed) -- never a third
    (torn/partial) value.

    ROUND 6 NOTE: the socket-drop described below is FIXED by the split
    deadline; test_r6_committing_* now asserts this same shape over the
    real socket. This direct-call test is kept as is.

    Calls `owner._handle_snapshot()` DIRECTLY rather than through the
    socket -- a deliberate, load-bearing deviation from every other test
    in this file, flagged prominently in the fairy handoff (see its
    ambiguous-decision log): `_handle_connection()`'s own pre-existing
    post-dispatch send-gate (`if remaining <= 0: return`, unchanged by
    this round, out of this spec's scope) drops the connection WITHOUT
    sending anything whenever the response is composed at-or-after the
    deadline it's gated on -- which every timeout-driven finalize response
    (abandoned, committing, aborted-via-timeout) is, by construction.
    Confirmed empirically (5/5 repeat runs, deterministic, not flaky): a
    socket-based version of this exact test always got `raw_response is
    None` (the connection closed with no data) -- the owner NEVER
    delivers `commit: "indeterminate"` over the wire under the CURRENT,
    pre-existing `_handle_connection()` architecture, which this spec did
    not ask to change. Calling `_handle_snapshot()` directly exercises the
    exact same concurrent worker/commit-state-machine code this method
    always runs (the finalize worker is still a real, independently
    scheduled thread; the only thing bypassed is the outer socket framing)
    -- consistent with this method's own docstring precedent, inherited
    from round 3/4, that its return dict "mainly matters for this
    method's own direct testability" precisely because the wire often
    cannot carry it.
    """
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE real_data (x INTEGER)")
    conn.execute("INSERT INTO real_data VALUES (42)")
    conn.commit()
    conn.close()
    expected_new_sha = _expected_full_snapshot_sha(store_path)

    replace_delay = 0.3
    _patch_slow_replace(monkeypatch, replace_delay)

    # fsync (unpatched here) and the tiny backup both complete near-
    # instantly, well before this deadline -- so the commit-gate check
    # (right after fsync, before replace) finds itself NOT yet past the
    # deadline and proceeds into "committing". The deadline then lands
    # squarely inside the (patched-slow) replace call itself.
    snapshot_deadline = eo.SNAPSHOT_RESPONSE_RESERVE + 0.1  # round 6: 0.1s of EXECUTION budget
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=snapshot_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "existing.db"
        sentinel_conn = sqlite3.connect(str(dest_path))
        sentinel_conn.execute("CREATE TABLE sentinel (marker TEXT)")
        sentinel_conn.execute("INSERT INTO sentinel VALUES ('do-not-touch')")
        sentinel_conn.commit()
        sentinel_conn.close()
        sentinel_sha = _sha256(dest_path)

        deadline = time.monotonic() + snapshot_deadline
        response = owner._handle_snapshot({"dest_path": str(dest_path)}, deadline)

        assert response["status"] == "rejected", response
        assert response["reason"] == "deadline_exceeded", response
        assert response.get("commit") == "indeterminate", response

        # Let the (patched-slow) replace actually finish and the worker
        # exit before reading dest's final content.
        time.sleep(replace_delay + 0.5)
        final_sha = _sha256(dest_path)
        assert final_sha in (sentinel_sha, expected_new_sha), (
            f"dest sha256 ({final_sha}) must be either the pre-existing "
            f"file's hash ({sentinel_sha}) or the complete new snapshot's "
            f"hash ({expected_new_sha}) -- never a third (torn/partial) value"
        )
        assert _leftover_temp_files(engram_home, dest_path.name) == []
    finally:
        owner.stop()


@requires_linux_owner
def test_snapshot_fsync_oserror_aborts_with_snapshot_failed_and_cleans_up(engram_home, monkeypatch):
    """(h) fsync OSError, carried over from round 4's original intent and
    retested against the new bounded-worker shape. A generous (non-tiny)
    deadline is used deliberately: the point of this test is the OSError
    handling path, not the deadline path, and an immediate-raising fsync
    (not a delay) fails well within any reasonable deadline -- so the
    correct response here is `snapshot_failed`, never `deadline_exceeded`
    (verified by running this exact test; see the fairy handoff's
    ambiguous-decision log for the reasoning the fairy spec asked to be
    checked rather than guessed).

    Discriminating mutation (fairy spec's own instruction): narrow the
    worker's error handling back to catching nothing / only a subset --
    this test must go RED.
    """
    _patch_raising_fsync(monkeypatch, OSError("simulated disk failure"))

    store_path = engram_home / eo.DEFAULT_STORE_NAME
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        # Case 1: a pre-existing sentinel at dest_path.
        dest_path = engram_home / "existing.db"
        sentinel_conn = sqlite3.connect(str(dest_path))
        sentinel_conn.execute("CREATE TABLE sentinel (marker TEXT)")
        sentinel_conn.execute("INSERT INTO sentinel VALUES ('do-not-touch')")
        sentinel_conn.commit()
        sentinel_conn.close()
        sentinel_sha = _sha256(dest_path)

        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=5.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(result, ec.Rejected), result
        assert result.reason == "snapshot_failed", result
        assert _sha256(dest_path) == sentinel_sha
        assert _leftover_temp_files(engram_home, dest_path.name) == []

        # Case 2: an absent dest_path stays absent.
        absent_dest = engram_home / "never-existed.db"
        assert not absent_dest.exists()
        result2 = ec.read(
            "snapshot",
            {"dest_path": str(absent_dest)},
            timeout=5.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(result2, ec.Rejected), result2
        assert result2.reason == "snapshot_failed", result2
        assert not absent_dest.exists()
        assert _leftover_temp_files(engram_home, absent_dest.name) == []

        # The socket handler didn't crash -- confirm the owner is still up
        # and serving, and (both slots were correctly released) a normal
        # snapshot still succeeds.
        ping_result = ec.read("ping", {}, timeout=1.0, socket_path=owner.socket_path)
        assert isinstance(ping_result, ec.Ok)
    finally:
        owner.stop()


# ---------------------------------------------------------------------------
# Round 6 (#3194, Borges's ruling on Sol's round-5 finding): the snapshot()
# deadline is SPLIT. `execution_deadline = request_deadline -
# SNAPSHOT_RESPONSE_RESERVE`; every execution-side use (the backup progress
# check, the request thread's wait on the finalize worker, the worker's
# abandon-or-commit gate) takes the execution deadline, so a timeout-driven
# response is composed with the reserve still in hand and the (unchanged)
# send gate in `_handle_connection()` lets it through. Contract items R6-1
# .. R6-5 below; each test names the mutation that must turn it red.
#
# The reserve is always read through `_reserve()` at CALL time, never
# captured, so that a mutation which sets the module constant to 0 changes
# what these tests compute too -- the tests must go red under that
# mutation because the OWNER's behaviour degrades, not because the tests
# hard-code a number the mutation cannot reach.
# ---------------------------------------------------------------------------


def _reserve() -> float:
    """The shipped reserve, read now. Falls back to 0.0 only so these tests
    RUN (and fail for the right reason: the defect, not an AttributeError)
    against round 5's f7c3a07d, which has no such constant."""
    return getattr(eo, "SNAPSHOT_RESPONSE_RESERVE", 0.0)


def _seed_small_store(store_path: Path) -> None:
    conn = sqlite3.connect(str(store_path))
    conn.execute("CREATE TABLE real_data (x INTEGER)")
    conn.execute("INSERT INTO real_data VALUES (42)")
    conn.commit()
    conn.close()


def _make_sentinel(dest_path: Path) -> str:
    c = sqlite3.connect(str(dest_path))
    c.execute("CREATE TABLE sentinel (marker TEXT)")
    c.execute("INSERT INTO sentinel VALUES ('do-not-touch')")
    c.commit()
    c.close()
    return _sha256(dest_path)


class _ClockShim:
    """Stand-in for the `time` module INSIDE engramd_owner only: real time
    plus a test-controlled offset, so a test can move the owner's clock
    forward at an exact point of the snapshot (e.g. between the backup and
    the worker's commit gate) instead of racing real sleeps. Everything but
    `monotonic` passes through to the real module."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def __getattr__(self, name):
        return getattr(time, name)


@requires_linux_owner
def test_r6_abandon_reaches_real_client_as_deadline_exceeded_within_request_deadline(
    engram_home, monkeypatch
):
    """R6-4(i): a timeout-driven ABANDON (test-only fsync delay past the
    execution deadline) arrives at a REAL client over the owner's socket as
    Rejected/deadline_exceeded -- not Unavailable -- no later than the
    request deadline, and dest is untouched.

    Mutation m1: SNAPSHOT_RESPONSE_RESERVE = 0 -- the request thread then
    composes at the request deadline, the send gate drops it, the client
    sees Unavailable: RED. Mutation m2 (wait until the REQUEST deadline):
    same. Round 5's f7c3a07d fails this the same way (5/5)."""
    _seed_small_store(engram_home / eo.DEFAULT_STORE_NAME)
    request_deadline = _reserve() + 0.3
    fsync_delay = request_deadline + 1.0
    _patch_slow_fsync(monkeypatch, fsync_delay)
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=request_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "existing.db"
        sentinel_sha = _make_sentinel(dest_path)

        start = time.monotonic()
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=request_deadline + 3.0,
            socket_path=owner.socket_path,
        )
        elapsed = time.monotonic() - start

        assert isinstance(result, ec.Rejected), (
            f"expected the owner's deadline_exceeded to REACH the client, got {result!r}"
        )
        assert result.reason == "deadline_exceeded", result
        # abandon shape: the typed marker is ABSENT, meaning dest was never touched
        assert result.commit is None, result
        assert elapsed <= request_deadline, (
            f"response arrived after {elapsed}s, past the request deadline {request_deadline}s"
        )
        assert _sha256(dest_path) == sentinel_sha

        time.sleep(fsync_delay + 0.5)  # let the delayed fsync return; worker aborts
        assert _sha256(dest_path) == sentinel_sha
        assert _leftover_temp_files(engram_home, dest_path.name) == []
    finally:
        owner.stop()


def _committing_scenario(engram_home, monkeypatch):
    """Shared setup for the two `committing` tests: a test-only delay INSIDE
    the replace that straddles the execution deadline (fsync and the tiny
    backup finish near-instantly, so the commit gate passes and the state
    is `committing` when the request thread's wait times out)."""
    _seed_small_store(engram_home / eo.DEFAULT_STORE_NAME)
    expected_new_sha = _expected_full_snapshot_sha(engram_home / eo.DEFAULT_STORE_NAME)
    request_deadline = _reserve() + 0.3
    replace_delay = request_deadline + 0.5
    _patch_slow_replace(monkeypatch, replace_delay)
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=request_deadline,
    )
    owner.start(install_signal_handlers=False)
    dest_path = engram_home / "existing.db"
    sentinel_sha = _make_sentinel(dest_path)
    return owner, dest_path, sentinel_sha, expected_new_sha, request_deadline, replace_delay


@requires_linux_owner
def test_r6_committing_reaches_real_client_as_indeterminate_within_request_deadline(
    engram_home, monkeypatch
):
    """R6-4(ii), through the REAL client: the `committing` path arrives as
    Rejected/deadline_exceeded carrying the indeterminate-commit detail, no
    later than the request deadline, and dest ends up EITHER the old file
    or the complete new snapshot -- never a third value.

    (The typed `Rejected.commit` field carries the owner's `commit` key;
    the outcome is asserted on it, never on the detail prose. The next test
    asserts the same key on the raw wire.)

    Mutation m1 (reserve = 0) / m2: Unavailable -- RED."""
    owner, dest_path, sentinel_sha, expected_new_sha, request_deadline, replace_delay = (
        _committing_scenario(engram_home, monkeypatch)
    )
    try:
        start = time.monotonic()
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=request_deadline + 3.0,
            socket_path=owner.socket_path,
        )
        elapsed = time.monotonic() - start

        assert isinstance(result, ec.Rejected), (
            f"expected the indeterminate response to REACH the client, got {result!r}"
        )
        assert result.reason == "deadline_exceeded", result
        # the typed marker (never the detail prose) distinguishes the outcome
        assert result.commit == "indeterminate", result
        assert elapsed <= request_deadline, (
            f"response arrived after {elapsed}s, past the request deadline {request_deadline}s"
        )

        time.sleep(replace_delay + 0.5)
        final_sha = _sha256(dest_path)
        assert final_sha in (sentinel_sha, expected_new_sha), (
            f"dest sha {final_sha} is neither the old file's {sentinel_sha} "
            f"nor the complete new snapshot's {expected_new_sha}"
        )
        assert _leftover_temp_files(engram_home, dest_path.name) == []
    finally:
        owner.stop()


@requires_linux_owner
def test_r6_committing_wire_response_carries_commit_indeterminate(engram_home, monkeypatch):
    """R6-4(ii), raw wire: the response the owner puts on the socket carries
    the top-level `commit: "indeterminate"` key (the real client's Rejected
    type drops it, so the wire is the only place to assert it).
    Mutation m1 / m2: no response at all (None) -- RED."""
    owner, dest_path, sentinel_sha, expected_new_sha, request_deadline, replace_delay = (
        _committing_scenario(engram_home, monkeypatch)
    )
    try:
        response = _raw_request(
            owner.socket_path,
            {
                "contract_version": ec.CONTRACT_VERSION,
                "method": "snapshot",
                "params": {"dest_path": str(dest_path)},
                "request_id": None,
            },
            timeout=request_deadline + 3.0,
        )
        assert response is not None, "the owner dropped the connection instead of responding"
        assert response["status"] == "rejected", response
        assert response["reason"] == "deadline_exceeded", response
        assert response.get("commit") == "indeterminate", response
        time.sleep(replace_delay + 0.5)
        assert _sha256(dest_path) in (sentinel_sha, expected_new_sha)
    finally:
        owner.stop()


@requires_linux_owner
def test_r6_send_gate_still_drops_a_response_composed_after_the_request_deadline(engram_home):
    """R6-3: the send gate in `_handle_connection()` is UNCHANGED -- a
    response composed after the REQUEST deadline is still dropped and the
    client sees Unavailable (it is the section-4 guard against delivering
    after the promise expired). Pinned by making `_handle_snapshot` itself
    overrun the request deadline, independent of any reserve.

    Mutation m5: delete the gate -- the late response is sent and the
    client sees a result instead of Unavailable: RED."""
    request_deadline = 0.2
    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=request_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:

        def _late(params, deadline):
            time.sleep(request_deadline + 0.2)
            return {"status": "rejected", "reason": "deadline_exceeded", "detail": "late", "remedy": "x"}

        owner._handle_snapshot = _late  # instance attribute; _dispatch resolves it via self
        result = ec.read(
            "snapshot",
            {"dest_path": str(engram_home / "d.db")},
            timeout=request_deadline + 3.0,
            socket_path=owner.socket_path,
        )
        assert isinstance(result, ec.Unavailable), result
    finally:
        owner.stop()


@requires_linux_owner
def test_r6_degenerate_execution_deadline_returns_bounded_rejection_without_work(
    engram_home, monkeypatch
):
    """R6-5: when `request_deadline - SNAPSHOT_RESPONSE_RESERVE` is already
    <= now at call start (a tiny test deadline, or a deadline smaller than
    the reserve), the call still returns a bounded rejected/deadline_exceeded
    -- no backup started (no temp file is even created), dest untouched, no
    raise, no slot consumed.

    Mutation m7 (remove the guard): a temp file is created (the mkstemp spy
    fires) -- RED."""
    _seed_small_store(engram_home / eo.DEFAULT_STORE_NAME)
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "existing.db"
        sentinel_sha = _make_sentinel(dest_path)

        made = []
        real_mkstemp = eo.tempfile.mkstemp

        def _spy_mkstemp(*a, **kw):
            made.append(1)
            return real_mkstemp(*a, **kw)

        monkeypatch.setattr(eo.tempfile, "mkstemp", _spy_mkstemp)

        # request deadline strictly inside the reserve, and one already past
        for request_deadline in (
            time.monotonic() + max(_reserve(), 0.2) * 0.5,
            time.monotonic() - 1.0,
        ):
            start = time.monotonic()
            response = owner._handle_snapshot({"dest_path": str(dest_path)}, request_deadline)
            assert time.monotonic() - start < 0.5, "must return promptly (bounded)"
            assert response["status"] == "rejected", response
            assert response["reason"] == "deadline_exceeded", response
            assert "commit" not in response, response  # nothing was ever reserved
            assert made == [], "a temp file was created: the backup was started"
            assert _sha256(dest_path) == sentinel_sha
            assert _leftover_temp_files(engram_home, dest_path.name) == []

        # no slot leaked: a healthy follow-up still succeeds
        ok = owner._handle_snapshot({"dest_path": str(engram_home / "f.db")}, time.monotonic() + 5.0)
        assert ok["status"] == "ok", ok
    finally:
        owner.stop()


@requires_linux_owner
def test_r6_reserve_is_read_at_call_time_not_captured(engram_home, monkeypatch):
    """R6-1: the constant is read when a snapshot call runs. An owner
    already constructed and started sees a monkeypatched reserve on its
    next call -- here one larger than the whole budget, which must trigger
    the degenerate-config rejection. Mutation: capture the constant at
    import/__init__ -- the patched value is ignored, the call proceeds and
    succeeds: RED."""
    _seed_small_store(engram_home / eo.DEFAULT_STORE_NAME)
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "out.db"
        monkeypatch.setattr(eo, "SNAPSHOT_RESPONSE_RESERVE", 100.0, raising=False)
        response = owner._handle_snapshot({"dest_path": str(dest_path)}, time.monotonic() + 5.0)
        assert response["status"] == "rejected", response
        assert response["reason"] == "deadline_exceeded", response
        assert not dest_path.exists()

        monkeypatch.setattr(eo, "SNAPSHOT_RESPONSE_RESERVE", 0.0, raising=False)
        response = owner._handle_snapshot({"dest_path": str(dest_path)}, time.monotonic() + 5.0)
        assert response["status"] == "ok", response
    finally:
        owner.stop()


@requires_linux_owner
def test_r6_worker_commit_gate_uses_execution_deadline_not_request_deadline(
    engram_home, monkeypatch
):
    """R6-2(c): the worker's abandon-or-commit gate compares against the
    EXECUTION deadline. The owner's clock is moved (via a shim, not a real
    sleep) to a point strictly between the execution deadline and the
    request deadline while the worker is in fsync, so the request thread is
    still waiting (has not abandoned) and the gate's own clock comparison is
    the only thing that can stop the commit. Correct: worker aborts, dest
    untouched, the call is not Ok.

    Mutations m3 (gate compares the request deadline) and m6 (reserve
    applied to the wait only): the worker commits -- the call returns Ok /
    dest changes: RED."""
    _seed_small_store(engram_home / eo.DEFAULT_STORE_NAME)
    reserve = max(_reserve(), 0.2)  # the window needs width even on f7c3a07d
    request_deadline = reserve + 1.0
    shim = _ClockShim()
    monkeypatch.setattr(eo, "time", shim)
    real_fsync = os.fsync

    def _fsync_then_jump(fd: int) -> None:
        time.sleep(0.05)  # the request thread is now parked in its wait
        real_fsync(fd)
        shim.offset += 1.0 + reserve / 2  # into (execution deadline, request deadline)

    monkeypatch.setattr(os, "fsync", _fsync_then_jump)

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "existing.db"
        sentinel_sha = _make_sentinel(dest_path)
        response = owner._handle_snapshot(
            {"dest_path": str(dest_path)}, shim.monotonic() + request_deadline
        )
        assert response["status"] != "ok", response
        assert response["reason"] == "deadline_exceeded", response
        time.sleep(0.3)
        assert _sha256(dest_path) == sentinel_sha, "worker committed past the execution deadline"
        assert _leftover_temp_files(engram_home, dest_path.name) == []
    finally:
        owner.stop()


@requires_linux_owner
def test_r6_backup_progress_check_uses_execution_deadline_not_request_deadline(
    engram_home, monkeypatch
):
    """R6-2(a): the backup() progress check compares against the EXECUTION
    deadline. The owner's clock is moved (shim) to between the execution
    and request deadlines right before the backup starts; the first
    progress callback must abort the backup, so NO finalize worker is ever
    spawned and fsync is never reached.

    Mutations m4 (progress compares the request deadline) and m6: the
    backup runs to completion and the finalize worker calls fsync: RED."""
    store_path = engram_home / eo.DEFAULT_STORE_NAME
    _seed_large_store(store_path, rows=3000)  # ~1500 pages: many backup steps
    reserve = max(_reserve(), 0.2)
    request_deadline = reserve + 5.0
    shim = _ClockShim()
    monkeypatch.setattr(eo, "time", shim)

    real_mkstemp = eo.tempfile.mkstemp

    def _mkstemp_then_jump(*a, **kw):
        out = real_mkstemp(*a, **kw)
        shim.offset += 5.0 + reserve / 2  # into (execution deadline, request deadline)
        return out

    monkeypatch.setattr(eo.tempfile, "mkstemp", _mkstemp_then_jump)
    fsync_calls = []
    real_fsync = os.fsync

    def _spy_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy_fsync)

    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "out.db"
        response = owner._handle_snapshot(
            {"dest_path": str(dest_path)}, shim.monotonic() + request_deadline
        )
        assert response["status"] == "rejected", response
        assert response["reason"] == "deadline_exceeded", response
        assert fsync_calls == [], "the backup ran to completion: progress check used the request deadline"
        assert not dest_path.exists()
        assert _leftover_temp_files(engram_home, dest_path.name) == []
    finally:
        owner.stop()


@requires_linux_owner
def test_r6_invalid_params_and_destination_take_precedence_over_degenerate_guard(engram_home):
    """G2 (reviewer follow-up): `invalid_params` and `invalid_destination`
    are decided BEFORE the degenerate-deadline guard, so a caller with a bad
    request and an already-past execution deadline is told what is wrong
    with the request, not `deadline_exceeded`. Pinned so that hoisting the
    guard above the validation (which would still 'work' by position alone)
    is noticed.

    Mutation: move the degenerate guard above the validation -- every case
    below then reports deadline_exceeded: RED; the neighbouring tests, whose
    requests are valid, stay green."""
    _seed_small_store(engram_home / eo.DEFAULT_STORE_NAME)
    owner = eo.EngramdOwner(engram_home, request_deadline=REQUEST_DEADLINE)
    owner.start(install_signal_handlers=False)
    try:
        past = time.monotonic() - 1.0  # execution deadline long gone
        good_dest = engram_home / "ok.db"

        cases = [
            ({}, "invalid_params"),
            ({"dest_path": ""}, "invalid_params"),
            ({"dest_path": 5}, "invalid_params"),
            ({"dest_path": str(engram_home / "no-such-dir" / "x.db")}, "invalid_destination"),
        ]
        ro_dir = engram_home / "ro"
        ro_dir.mkdir()
        ro_dir.chmod(0o500)
        try:
            if not os.access(ro_dir, os.W_OK):  # skipped as root, where 0500 is still writable
                cases.append(({"dest_path": str(ro_dir / "x.db")}, "invalid_destination"))
            for params, expected in cases:
                response = owner._handle_snapshot(params, past)
                assert response["status"] == "rejected", (params, response)
                assert response["reason"] == expected, (params, response)
        finally:
            ro_dir.chmod(0o700)

        # control: a VALID request with the same past deadline is the degenerate rejection
        response = owner._handle_snapshot({"dest_path": str(good_dest)}, past)
        assert response["reason"] == "deadline_exceeded", response
        assert not good_dest.exists()
    finally:
        owner.stop()


@requires_linux_owner
def test_r6_mid_backup_abort_reaches_real_client_as_deadline_exceeded_within_request_deadline(
    engram_home, monkeypatch
):
    """G3 (reviewer follow-up): the MID-BACKUP abort (the progress check
    raising at the execution deadline; no finalize worker is ever spawned)
    reaches a REAL socket client as Rejected/deadline_exceeded within the
    request deadline. Existing mid-backup tests only assert `not Ok`, which
    an `Unavailable` also satisfies.

    Timing note: this path is composed up to ONE BACKUP STEP
    (SNAPSHOT_BACKUP_STEP_MAX_WALL_TIME, 0.1 s allowance) after the
    execution deadline, not right at it like the worker paths, plus the
    temp-file close/unlink. The reserve (0.25 s) must therefore cover step
    overrun + delivery: 0.1 + a few ms < 0.25. The budget here is the
    reserve plus 0.02 s of execution time, so the large-store backup
    (~76 ms on the measuring seat) is certain to be cut off mid-way.

    Mutation m1 (reserve = 0): the request deadline equals the execution
    deadline, the abort response is composed after it, the send gate drops
    it and the client sees Unavailable: RED."""
    _seed_large_store(engram_home / eo.DEFAULT_STORE_NAME)
    request_deadline = _reserve() + 0.02
    fsync_calls = []
    real_fsync = os.fsync

    def _spy_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy_fsync)

    owner = eo.EngramdOwner(
        engram_home,
        request_deadline=REQUEST_DEADLINE,
        snapshot_request_deadline=request_deadline,
    )
    owner.start(install_signal_handlers=False)
    try:
        dest_path = engram_home / "overrun-snap.db"
        start = time.monotonic()
        result = ec.read(
            "snapshot",
            {"dest_path": str(dest_path)},
            timeout=request_deadline + 3.0,
            socket_path=owner.socket_path,
        )
        elapsed = time.monotonic() - start

        assert isinstance(result, ec.Rejected), (
            f"expected the mid-backup abort response to REACH the client, got {result!r}"
        )
        assert result.reason == "deadline_exceeded", result
        assert elapsed <= request_deadline, (
            f"response arrived after {elapsed}s, past the request deadline {request_deadline}s"
        )
        assert fsync_calls == [], "backup completed: this did not exercise the mid-backup abort path"
        assert not dest_path.exists()
        assert _leftover_temp_files(engram_home, dest_path.name) == []
    finally:
        owner.stop()


# ---------------------------------------------------------------------------
# #2652 Phase-1 Unit 4b (issue #3195, merged via PR #3221) -- the owner's two
# connections and the client-read authorizer floor. These tests landed on dev
# while this branch (U4a, #3194) was in review; they were re-applied here
# verbatim from dev when the two branches were merged.
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


@requires_linux_owner
def test_expensive_read_is_aborted_by_deadline_and_cheap_read_is_not(owner_running, tmp_path):
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
    # U4c (#3196): the owner's own gate now refuses every statement not in
    # the committed template list, so this test (whose subject is the
    # deadline axis, not the registry) gets a gate over the SAME client-read
    # connection with a fixture registry admitting the two statements it uses.
    fixture_registry = rr.build_registry(
        ["SELECT 1", _EXPENSIVE_CARTESIAN_SQL], sqlite3.connect(":memory:")
    )
    gate = eo._ClientReadGate(owner_running._client_read_conn, registry=fixture_registry, home=tmp_path)

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


@requires_linux_owner
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
        #
        # (Merge note, #3194 + #3195: dev's original trigger here was a
        # pre-existing garbage file at the destination, which used to make
        # backup() itself raise. Since #3194 stages the copy into a private
        # temp file and atomically replaces the destination, such a file is
        # simply replaced and the call succeeds -- so the error is now
        # injected directly at the `.backup()` call on the owner-internal
        # connection, which is still a genuine sqlite3.Error raised BY
        # backup() and still exercises the same `except sqlite3.Error`
        # clause. The garbage destination is kept as a control: it must be
        # left byte-identical by the failed call.)
        bad_dest = engram_home / "snap-bad.db"
        garbage = b"not a sqlite database, just garbage bytes" * 4
        bad_dest.write_bytes(garbage)

        real_owner_conn = owner._owner_conn

        class _BackupRaises:
            """Forwards everything to the real owner-internal connection
            except backup(), which raises a genuine sqlite3.Error."""

            def __getattr__(self, name):
                return getattr(real_owner_conn, name)

            def backup(self, *args, **kwargs):
                raise sqlite3.OperationalError("injected backup() failure")

        owner._owner_conn = _BackupRaises()
        try:
            result2 = ec.read(
                "snapshot",
                {"dest_path": str(bad_dest)},
                timeout=3.0,
                socket_path=owner.socket_path,
            )
        finally:
            owner._owner_conn = real_owner_conn
        assert isinstance(result2, ec.Rejected)
        assert result2.reason == "snapshot_failed"
        assert "injected backup() failure" in result2.detail
        assert bad_dest.read_bytes() == garbage, "a failed snapshot must leave the destination untouched"

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


@requires_linux_owner
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


@requires_linux_owner
def test_single_value_size_is_bounded_independently_of_progress_handler(owner_running, tmp_path):
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
    limit = eo.CLIENT_READ_LIMIT_LENGTH
    # U4c (#3196): a `?` inside a function argument (`zeroblob(?)`) is an
    # unsafe placeholder the registry refuses (R7), so the two statements are
    # registered as LITERALS carrying the same sizes (limit-1 / limit+1), over
    # a fixture registry on the owner's real client-read connection.
    under_sql = f"SELECT length(zeroblob({limit - 1}))"
    over_sql = f"SELECT zeroblob({limit + 1})"
    fixture_registry = rr.build_registry([under_sql, over_sql], sqlite3.connect(":memory:"))
    gate = eo._ClientReadGate(owner_running._client_read_conn, registry=fixture_registry, home=tmp_path)

    under = gate.execute(under_sql, deadline=time.monotonic() + 5.0)
    assert under == [(limit - 1,)]

    with pytest.raises(sqlite3.Error):
        gate.execute(over_sql, deadline=time.monotonic() + 5.0)

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


@requires_linux_owner
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

    # U4d (#3197, cell O3): the client-facing `read` method now exists, so the
    # property is asserted for it too -- the handler reaches the store ONLY
    # through `_client_read_gate`, never through either connection directly.
    # Mutation: have `_handle_read` execute on `self._owner_conn` (or on
    # `self._client_read_conn` bypassing the gate) -> red.
    import ast
    import inspect
    import textwrap

    assert "read" in owner_running._methods
    handler = ast.parse(textwrap.dedent(inspect.getsource(eo.EngramdOwner._handle_read)))
    touched = {n.attr for n in ast.walk(handler) if isinstance(n, ast.Attribute)}
    assert "_client_read_gate" in touched
    assert not touched & {"_owner_conn", "_owner_lock", "_client_read_conn"}, touched

    # #3665 round 2 (F3): the lock / execute tail moved into `_run_client_read`
    # and the typed `df` method has its own handler; neither may reference the
    # owner-internal connection or lock. Mutation: reference `self._owner_conn`
    # inside `_run_client_read` -> red.
    for fn in (eo.EngramdOwner._handle_df, eo.EngramdOwner._run_client_read):
        fn_tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        fn_touched = {n.attr for n in ast.walk(fn_tree) if isinstance(n, ast.Attribute)}
        assert not fn_touched & {"_owner_conn", "_owner_lock"}, (fn.__name__, fn_touched)

    # And live, over the socket: `read` calls the gate (a spy stands in for it)
    # while `_owner_conn` is a tripwire that fails on any use.
    seen = []

    class _SpyGate:
        def execute(self, sql, params=(), *, deadline):
            seen.append((sql, params))
            return [(1, "x")]

    class _Tripwire:
        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            raise AssertionError(f"`read` touched the owner-internal connection: .{name}")

    real_gate, real_conn = owner_running._client_read_gate, owner_running._owner_conn
    owner_running._client_read_gate, owner_running._owner_conn = _SpyGate(), _Tripwire()
    try:
        result = ec.read("read", {"sql": "SELECT 1", "args": ["a"]}, timeout=2.0,
                         socket_path=owner_running.socket_path)
    finally:
        owner_running._client_read_gate, owner_running._owner_conn = real_gate, real_conn
    assert isinstance(result, ec.Ok), result
    assert result.result == [[1, "x"]]
    assert seen == [("SELECT 1", ("a",))]


@requires_linux_owner
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


@requires_linux_owner
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


# ---------------------------------------------------------------------------
# #3292 -- the owner refuses on an UNSUPPORTED platform BY DESIGN (#3194). This
# test carries no requires_linux_owner marker: it runs on every platform, and is
# the one that proves the refusal the other tests' skip marker relies on.
# #3666: Darwin is now a SUPPORTED platform (its own prerequisites, below), so
# the "unsupported" case here is FreeBSD -- a platform that is neither.
# ---------------------------------------------------------------------------


def test_start_refuses_on_an_unsupported_platform_before_any_side_effect(engram_home, monkeypatch):
    monkeypatch.setattr(eo.platform, "system", lambda: "FreeBSD")
    fresh_home = engram_home / "not-yet-created"
    assert not fresh_home.exists()
    threads_before = set(threading.enumerate())
    owner = eo.EngramdOwner(fresh_home)
    with pytest.raises(eo.OwnerPermissionUnsupported) as ei:
        owner.start(install_signal_handlers=False)
    assert "FreeBSD" in str(ei.value)
    assert not fresh_home.exists()
    assert not owner.socket_path.exists()
    assert not owner.pid_path.exists()
    assert not owner.store_path.exists()
    assert owner._accept_thread is None
    assert set(threading.enumerate()) == threads_before


# ---------------------------------------------------------------------------
# #3292 fold 2 -- ONE predicate, `eo.platform_unsupported_reason()`, is what
# both the owner's refusal and the test marker key on. These cells carry no
# marker: they run (and must pass) on every platform.
# ---------------------------------------------------------------------------


def _patch_platform(monkeypatch, *, system="Linux", setfacl=True, peercred=True):
    monkeypatch.setattr(eo.platform, "system", lambda: system)
    monkeypatch.setattr(
        eo.shutil, "which", lambda name, *a, **k: "/usr/bin/" + name if setfacl else None
    )
    if peercred:
        monkeypatch.setattr(eo.socket, "SO_PEERCRED", 17, raising=False)
    else:
        monkeypatch.delattr(eo.socket, "SO_PEERCRED", raising=False)


_PLATFORM_CASES = [
    pytest.param({"system": "FreeBSD"}, "platform.system()", id="unsupported-platform"),
    pytest.param({"setfacl": False}, "setfacl", id="no-setfacl"),
    pytest.param({"peercred": False}, "SO_PEERCRED", id="no-so-peercred"),
]


def test_platform_unsupported_reason_none_when_all_satisfied(monkeypatch):
    _patch_platform(monkeypatch)
    assert eo.platform_unsupported_reason() is None


@pytest.mark.parametrize("kw,needle", _PLATFORM_CASES)
def test_platform_unsupported_reason_names_each_failing_condition(monkeypatch, kw, needle):
    _patch_platform(monkeypatch, **kw)
    reason = eo.platform_unsupported_reason()
    assert reason is not None
    assert needle in reason


def test_platform_unsupported_reason_order_platform_first(monkeypatch):
    _patch_platform(monkeypatch, system="FreeBSD", peercred=False)
    assert "platform.system()" in eo.platform_unsupported_reason()
    _patch_platform(monkeypatch, setfacl=False, peercred=False)
    assert "setfacl" in eo.platform_unsupported_reason()


@pytest.mark.parametrize("kw,needle", _PLATFORM_CASES)
def test_start_refusal_message_equals_shared_predicate(engram_home, monkeypatch, kw, needle):
    _patch_platform(monkeypatch, **kw)
    expected = eo.platform_unsupported_reason()
    assert expected is not None and needle in expected
    fresh_home = engram_home / "not-yet-created"
    threads_before = set(threading.enumerate())
    owner = eo.EngramdOwner(fresh_home)
    with pytest.raises(eo.OwnerPermissionUnsupported) as ei:
        owner.start(install_signal_handlers=False)
    assert str(ei.value) == expected
    assert not fresh_home.exists()
    assert not owner.socket_path.exists()
    assert not owner.pid_path.exists()
    assert not owner.store_path.exists()
    assert owner._accept_thread is None
    assert set(threading.enumerate()) == threads_before


# ---------------------------------------------------------------------------
# #3666 -- the Darwin branch of the same predicate. SIMULATED here so the logic
# is checked on every platform (patching each primitive the branch reads, never
# `platform.system` alone -- #3375); the REAL-Mac evidence for the same
# prerequisites is tests/test_engramd_owner_darwin_3666.py.
# ---------------------------------------------------------------------------


def _patch_darwin(monkeypatch, *, peercred=True, chmod=True, devfd=True):
    monkeypatch.setattr(eo.platform, "system", lambda: "Darwin")
    if peercred:
        monkeypatch.setattr(eo.socket, "LOCAL_PEERCRED", 1, raising=False)
    else:
        monkeypatch.delattr(eo.socket, "LOCAL_PEERCRED", raising=False)
    monkeypatch.setattr(eo, "DARWIN_CHMOD", __file__ if chmod else "/nonexistent-3666/chmod")
    monkeypatch.setattr(
        eo, "DARWIN_DEV_FD", str(Path(__file__).parent) if devfd else "/nonexistent-3666/fd"
    )


_DARWIN_CASES = [
    pytest.param({"peercred": False}, "LOCAL_PEERCRED", id="darwin-no-local-peercred"),
    pytest.param({"chmod": False}, "/nonexistent-3666/chmod", id="darwin-no-chmod"),
    pytest.param({"devfd": False}, "/nonexistent-3666/fd", id="darwin-no-dev-fd"),
]


def test_platform_unsupported_reason_none_for_a_darwin_with_every_prerequisite(monkeypatch):
    _patch_darwin(monkeypatch)
    assert eo.platform_unsupported_reason() is None


@pytest.mark.parametrize("kw,needle", _DARWIN_CASES)
def test_darwin_reason_names_the_one_missing_prerequisite(monkeypatch, kw, needle):
    _patch_darwin(monkeypatch, **kw)
    reason = eo.platform_unsupported_reason()
    assert reason is not None and needle in reason
    # a Darwin seat is never asked for Linux's tools
    assert "setfacl" not in reason and "SO_PEERCRED" not in reason


@pytest.mark.parametrize("kw,needle", _DARWIN_CASES)
def test_darwin_start_refusal_message_equals_shared_predicate(engram_home, monkeypatch, kw, needle):
    _patch_darwin(monkeypatch, **kw)
    expected = eo.platform_unsupported_reason()
    fresh_home = engram_home / "not-yet-created"
    owner = eo.EngramdOwner(fresh_home)
    with pytest.raises(eo.OwnerPermissionUnsupported) as ei:
        owner.start(install_signal_handlers=False)
    assert str(ei.value) == expected
    assert not fresh_home.exists()


def test_marker_and_predicate_agree_at_import_time():
    # The mark's skipif condition was evaluated at import; the predicate is
    # evaluated now. The platform does not change within a run, so they agree.
    assert requires_linux_owner.args[0] == (eo.platform_unsupported_reason() is not None)
