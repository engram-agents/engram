"""EngramdOwner -- the owner process skeleton (#2652 Phase-1 Unit 3, issue #3096).

One process per seat holds the only open handle on `knowledge.db`; this
build adds NO domain operations -- no `create_node`, no `retract`, nothing
manager/domain-engine shaped. It is the skeleton: start up, own the AF_UNIX
socket, answer the U1 handshake (a contract-version check on every
request, per `src/engram/engramd_client.py`), answer `ping`, serve
`snapshot()`. Nothing calls this owner yet; U1's client library is the
only thing that speaks its wire protocol, and even that isn't wired to any
real caller yet -- see `tests/test_engramd_owner.py` for the fixtures that
exercise this module directly, standing in for the not-yet-built callers.

Full design context: docs/2652-engrammanager-design.md SS3 ("The ownership
shape") and SS5 ("Rider 2 -- what a caller sees when the owner is down").

## What this deliberately is NOT (out of scope for this build)

- Supervision per platform (systemd --user / launchd) -- a later unit's
  call, per the design doc SS7. This module is a plain Python process; it
  does not write service files or register itself with anything.
- Typed writes of any kind -- no manager, no domain engines, no cascade.
- Wiring any real caller (hooks, the MCP server, viz, ...) to this owner.
  It exists standalone.

## Wire protocol -- speaks U1's contract, does not redefine it

See `src/engram/engramd_client.py`'s own module docstring for the full wire
shape (newline-delimited JSON over AF_UNIX/SOCK_STREAM; two response
states, `ok`/`rejected` -- `unavailable` is purely the CLIENT's own
conclusion and is never sent by this module). This module calls
`engramd_client.check_contract_version()` directly on every request rather
than reimplementing version comparison (docs/2652-engrammanager-design.md
SS5.3), and reuses `engramd_client.CONTRACT_VERSION` and
`engramd_client.DEFAULT_SOCKET_NAME` as the single shared source of truth
for those values -- see "Socket path resolution" below for why this
module does NOT import `engramd_client`'s private `_resolve_socket_path`.

## Lifecycle -- mirrors engram-surface-daemon.py's socket-lifecycle SHAPE

`src/engram/hooks/claude/engram-surface-daemon.py` is the in-repo precedent
this module's startup/shutdown mirrors (a different daemon, the same
lifecycle shape): a pidfile plus a confirmably-alive check via
`os.kill(pid, 0)` (contract items 1+2), an ownership flag
(`_bound_socket`) gating whether cleanup may unlink the socket, signal
handlers calling one `cleanup()` function (contract item 6), and a
thread-per-connection accept loop with `server.settimeout()` on the
LISTENING socket for periodic shutdown checks. One deliberate departure
from that precedent: on finding a confirmably-live prior owner, this
module RAISES (`OwnerAlreadyRunning`) and the caller decides what to do,
rather than the surface daemon's own `sys.exit(0)` -- issue #3096's
contract item 1 asks for a non-zero exit ("refuses to start with a clear
error and exit non-zero"), the opposite of the surface daemon's soft
back-off; `main()` below is what turns that exception into `sys.exit(1)`
for the real process entry point, while tests can assert on the exception
directly.

## Socket path resolution -- replicated, not shared, per the fairy spec's
## scope bounds ("never modify engramd_client.py")

`engramd_client._resolve_socket_path()` is private and this module does
NOT import it, nor does it drop the leading underscore in
`engramd_client.py` to make it a shared public helper (an option the U3
fairy spec explicitly offered as "your call") -- the SAME spec's "Scope
bounds" section says plainly "never modify it", which is the harder
constraint and wins. `_resolve_engram_home()` below replicates
`_resolve_socket_path()`'s exact ENGRAM_HOME-with-tilde-fallback logic
instead.

## Per-request deadline -- mirrors engramd_client._arm_timeout_or_unavailable,
## applied to the READ side instead

Contract item 8 requires an end-to-end deadline per request, measured from
ACCEPT, not per-recv -- the mirror image of the bug U1's own round 5 fixed
client-side (docs/2652-engrammanager-design.md SS5.1: "a per-operation
timeout restarts on every byte, so a peer that trickles data never trips
it"). `_read_request_line()` below computes `deadline` once per
connection (at accept) and recomputes `remaining = deadline -
time.monotonic()` before EVERY blocking `conn.recv()`, re-arming
`conn.settimeout(remaining)` with that fresh value each time -- exactly
`engramd_client._arm_timeout_or_unavailable`'s own technique, mirrored
server-side. A single `conn.settimeout(bound)` set once outside the loop
does NOT satisfy this (see that function's own docstring for why
recomputing alone, without re-arming, still under-specifies).

Thread-per-connection (one thread per accepted connection) is this
module's answer to contract item 9 ("one slow client does not block
another") -- concurrency, not a bounded-wait argument; the design doc's
own item 9 asks the build to say which.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import engramd_client as ec

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_PID_NAME = "engramd.pid"
DEFAULT_STORE_NAME = "knowledge.db"

# Response-size cap for INCOMING requests TO the owner (contract item 10).
# Deliberately smaller than engramd_client.MAX_RESPONSE_BYTES (10 MiB --
# the CLIENT's own cap on the OWNER's *response*, sized for query results):
# incoming REQUESTS are small control-plane JSON (a method name, a handful
# of params, a request_id), never bulk data, so 1 MiB is already generous
# headroom for a well-formed caller while still bounding a runaway/hostile
# peer. See the fairy handoff's design-choices section for this reasoning
# stated plainly, since the fairy spec left the exact number to the
# builder's judgment.
MAX_REQUEST_BYTES = 1 * 1024 * 1024  # 1 MiB

# Production default for the per-request end-to-end deadline (contract item
# 8), measured from ACCEPT. Generous enough for a legitimately slow
# operation (e.g. a large snapshot()) while still bounding a hung or
# trickling client; test fixtures use a much shorter value
# (tests/test_engramd_owner.py: REQUEST_DEADLINE) so the suite runs fast.
DEFAULT_REQUEST_DEADLINE = 5.0  # seconds

# How often the LISTENING socket's accept() wakes up to check the shutdown
# flag. Short enough that .stop() returns quickly in tests (this process's
# accept loop has nothing else to do between wakeups, unlike
# engram-surface-daemon.py's heavier 5.0s, which also runs an idle watchdog
# and an indexer tick this module has neither of).
ACCEPT_POLL_INTERVAL = 0.2  # seconds


class OwnerAlreadyRunning(RuntimeError):
    """Raised by EngramdOwner.start() when a confirmably-live owner already
    holds this seat's socket (contract item 1). `main()` below catches this
    and exits non-zero with a clear message; callers that embed
    EngramdOwner directly (e.g. tests) see the exception itself."""


# ---------------------------------------------------------------------------
# Socket path / ENGRAM_HOME resolution -- replicated from
# engramd_client._resolve_socket_path's exact logic (see module docstring's
# "Socket path resolution" section for why this is a replication, not an
# import of that private function).
# ---------------------------------------------------------------------------


def _resolve_engram_home(engram_home: str | Path | None) -> Path:
    if engram_home is not None:
        return Path(engram_home)
    raw = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
    return Path(raw)


def _rejected_wire(rejected: ec.Rejected) -> dict:
    """Translate an engramd_client.Rejected dataclass into the wire dict
    shape ({"status": "rejected", "reason": ..., "detail": ...,
    "remedy": ...}) -- used for both the contract-version handshake
    rejection (which check_contract_version() already returns as a
    Rejected) and nowhere else; every other rejection in this module is
    built as a plain dict directly at its call site."""
    return {
        "status": "rejected",
        "reason": rejected.reason,
        "detail": rejected.detail,
        "remedy": rejected.remedy,
    }


class EngramdOwner:
    """The owner process skeleton -- one per seat, sole handle on
    `knowledge.db` within this build's scope (handshake + `ping` +
    `snapshot()` only). See the module docstring for the full design
    context and what is deliberately not built yet."""

    def __init__(
        self,
        engram_home: str | Path | None = None,
        *,
        request_deadline: float = DEFAULT_REQUEST_DEADLINE,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        accept_poll_interval: float = ACCEPT_POLL_INTERVAL,
        contract_version: str = ec.CONTRACT_VERSION,
    ) -> None:
        self.engram_home = _resolve_engram_home(engram_home)
        self.socket_path = self.engram_home / ec.DEFAULT_SOCKET_NAME
        self.pid_path = self.engram_home / DEFAULT_PID_NAME
        self.store_path = self.engram_home / DEFAULT_STORE_NAME
        self.request_deadline = request_deadline
        self.max_request_bytes = max_request_bytes
        self.accept_poll_interval = accept_poll_interval
        self.contract_version = contract_version

        self._server: socket.socket | None = None
        self._store_conn: sqlite3.Connection | None = None
        self._store_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._bound_socket = False
        self._accept_thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------

    def start(self, *, install_signal_handlers: bool = True) -> None:
        """Bind the socket, open the store, and begin serving in a
        background thread. Returns once the accept loop is running.

        Raises OwnerAlreadyRunning if a confirmably-live owner already
        holds this seat's socket (contract item 1) -- nothing is touched
        in that case; the existing owner is left completely unaffected.

        `install_signal_handlers=False` (tests only -- see
        tests/test_engramd_owner.py's owner_running fixture) skips
        registering SIGTERM/SIGINT handlers, since signal.signal() is
        process-global and installing handlers repeatedly across a test
        session would clobber pytest's own signal handling. The cleanup
        path this skips is otherwise identical: the real handler
        (_handle_signal) just calls .stop(), the same method tests call
        directly.
        """
        self.engram_home.mkdir(parents=True, exist_ok=True)

        if self.socket_path.exists():
            if self._recorded_pid_is_alive():
                raise OwnerAlreadyRunning(
                    f"a live engramd owner already holds {self.socket_path} "
                    f"-- refusing to start a second owner for this seat; "
                    f"the existing owner is unaffected"
                )
            # Stale socket left by a crashed/killed owner (contract item 2)
            # -- detect it (dead PID) and replace it, never refuse forever.
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        # One process, one open handle on the store (design doc SS3) --
        # opened once here and held for this owner's lifetime; snapshot()
        # backs up FROM this same connection object (contract item 5). No
        # domain-engine/manager machinery sits on top of it in this build.
        # sqlite3.connect() creates an empty file if store_path doesn't yet
        # exist -- acceptable here: nothing in this build's contract
        # requires the store to pre-exist, and a real seat's bootstrap
        # already creates knowledge.db before any daemon starts.
        self._store_conn = sqlite3.connect(str(self.store_path), check_same_thread=False)

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        # Ownership flag flips ONLY after bind() succeeds -- mirrors
        # engram-surface-daemon.py's _bound_socket exactly: cleanup()'s
        # socket unlink is gated on this, never on pidfile identity alone,
        # so a process that never actually bound never deletes a live
        # holder's socket.
        self._bound_socket = True
        self._server.listen(5)
        self._server.settimeout(self.accept_poll_interval)

        self.pid_path.write_text(str(os.getpid()))

        if install_signal_handlers:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)

        self._shutdown.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _handle_signal(self, signum, frame) -> None:
        self.stop()

    def stop(self) -> None:
        """Clean shutdown (contract item 6): stop accepting new
        connections, remove the socket and pidfile (each gated on this
        process actually owning them), close the store connection. After
        this returns, a client's connect() against the now-removed socket
        path gets Unavailable, not a hang."""
        self._shutdown.set()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=self.accept_poll_interval + 2.0)
        self._cleanup()

    def _cleanup(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        # PID_PATH: removed only if it still names THIS process -- mirrors
        # engram-surface-daemon.py's cleanup(): guards against a delayed
        # signal firing after a newer instance already wrote its own fresh
        # pidfile (which would otherwise delete the NEW instance's pidfile
        # out from under it).
        still_ours = False
        try:
            with open(self.pid_path) as f:
                still_ours = int(f.read().strip()) == os.getpid()
        except (OSError, ValueError):
            # No pidfile, or unreadable/garbage content -- nothing to
            # protect, proceed with removing it (matches the surface
            # daemon's identical fallback).
            still_ours = True
        if still_ours:
            try:
                self.pid_path.unlink()
            except FileNotFoundError:
                pass
        # SOCKET_PATH: removed only if THIS process actually bound it
        # (_bound_socket) -- independent of pidfile identity, same
        # reasoning as engram-surface-daemon.py's #2713 fix: a pidfile-
        # identity-only guard can delete a DIFFERENT, actually-live
        # holder's socket on a seat where two owners' lifecycles overlap.
        if self._bound_socket:
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
        if self._store_conn is not None:
            try:
                self._store_conn.close()
            except sqlite3.Error:
                pass
            self._store_conn = None

    def wait_forever(self) -> None:
        """Block the calling thread until shutdown (a signal, or .stop()
        called from elsewhere). Used by the __main__ entry point below;
        tests call .stop() directly instead of relying on signal delivery
        (see the owner_running fixture's own docstring for why)."""
        while not self._shutdown.is_set():
            self._shutdown.wait(timeout=1.0)

    def _recorded_pid_is_alive(self) -> bool:
        """Mirrors engram-surface-daemon.py's _recorded_pid_is_alive()
        exactly (contract items 1+2's shared mechanism): False for a
        missing/unreadable/garbage pidfile, or a PID that signal 0 confirms
        is dead. Best-effort -- any error means "assume dead" so a corrupt
        pidfile never blocks a fresh owner from starting."""
        try:
            with open(self.pid_path) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # Any other errno (e.g. EPERM on a reused PID owned by someone
            # else) -- treat as "not confirmably ours", same as dead.
            return False
        return True

    # -- accept loop ------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._shutdown.is_set():
            try:
                conn, _addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()

    def _handle_connection(self, conn: socket.socket) -> None:
        # Contract item 8: end-to-end deadline measured from ACCEPT (i.e.
        # from entry to this handler), computed ONCE here.
        # _read_request_line() recomputes the remaining budget and re-arms
        # conn.settimeout() with it before EVERY blocking recv -- see the
        # module docstring's "Per-request deadline" section.
        deadline = time.monotonic() + self.request_deadline
        try:
            line = self._read_request_line(conn, deadline)
            if line is None:
                return  # deadline exceeded, oversized, malformed framing, or peer closed -- drop silently
            try:
                request = json.loads(line.decode("utf-8").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return  # malformed request -- drop; a U1 client sees Unavailable, not a hang
            if not isinstance(request, dict):
                return
            response = self._dispatch(request)
            # Bound the response send by the SAME remaining deadline -- a
            # client that stops reading after sending its request
            # shouldn't be able to hold this thread past the request's own
            # budget either. Not explicitly one of the 10 numbered
            # contract items (which name the READ side), but a direct
            # extension of the same "one deadline per request, end to end,
            # on both sides of the socket" principle
            # (docs/2652-engrammanager-design.md SS5.1) -- flagged in the
            # fairy handoff.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            conn.settimeout(remaining)
            try:
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            conn.close()

    def _read_request_line(self, conn: socket.socket, deadline: float) -> bytes | None:
        """Accumulate bytes until a '\\n'-terminated line completes, the
        deadline passes, the request exceeds max_request_bytes, or the
        peer closes. Returns None for every non-success case (the caller
        drops the connection without responding); returns the raw line
        bytes (including the trailing newline) on success."""
        data = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            conn.settimeout(remaining)
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                return None
            if not chunk:
                return None  # peer closed
            data += chunk
            if len(data) > self.max_request_bytes:
                # Contract item 10 -- a runaway peer cannot grow the
                # buffer without limit. Drop rather than respond: this
                # mirrors engramd_client's own symmetric behavior on the
                # opposite side (MAX_RESPONSE_BYTES), which also just
                # gives up rather than trying to compose a reply around an
                # already-oversized, still-incomplete message.
                return None
            if b"\n" in data:
                return data

    # -- dispatch ------------------------------------------------------

    def _dispatch(self, request: dict) -> dict:
        """Contract items 3, 4, 5, 7: version check on every request (not
        a separate handshake step, per U1's own design), then `ping` /
        `snapshot` / unknown-operation."""
        sent_version = request.get("contract_version")
        rejection = ec.check_contract_version(sent_version, owner=self.contract_version)
        if rejection is not None:
            return _rejected_wire(rejection)

        method = request.get("method")
        params = request.get("params")
        if not isinstance(params, dict):
            params = {}

        if method == "ping":
            return {
                "status": "ok",
                "result": {
                    "contract_version": self.contract_version,
                    "store_path": str(self.store_path),
                },
            }
        if method == "snapshot":
            return self._handle_snapshot(params)

        return {
            "status": "rejected",
            "reason": "unknown_operation",
            "detail": f"no such operation: {method!r}",
            "remedy": (
                "check the method name against the owner's supported "
                "operations (this build: ping, snapshot)"
            ),
        }

    def _handle_snapshot(self, params: dict) -> dict:
        """Contract item 5: writes a quiesced, consistent copy of the
        store to a caller-named destination path, using SQLite's backup
        API (sqlite3.Connection.backup(), stdlib) FROM this owner's own
        persistent connection. A client-facing service, not a manager
        mechanism -- no domain-engine machinery here, just a direct
        sqlite3 operation.

        Param shape: {"dest_path": "<path>"} -- the caller names an
        absolute destination path. If the destination's parent directory
        doesn't exist or isn't writable, this REJECTS cleanly rather than
        silently creating it -- a deliberate, conservative choice (see the
        fairy handoff's design-choices section): creating directories on
        the caller's behalf can mask a caller bug (e.g. a typo'd path) and
        is a more surprising default than a clear rejection naming exactly
        what's wrong.
        """
        dest_path = params.get("dest_path")
        if not dest_path or not isinstance(dest_path, str):
            return {
                "status": "rejected",
                "reason": "invalid_params",
                "detail": "params.dest_path is required and must be a non-empty string",
                "remedy": 'call snapshot with params={"dest_path": "<absolute path>"}',
            }
        dest = Path(dest_path)
        parent = dest.parent
        if not parent.is_dir():
            return {
                "status": "rejected",
                "reason": "invalid_destination",
                "detail": f"destination parent directory does not exist: {parent}",
                "remedy": (
                    "create the parent directory first, or pass a dest_path "
                    "whose parent directory already exists"
                ),
            }
        if not os.access(parent, os.W_OK):
            return {
                "status": "rejected",
                "reason": "invalid_destination",
                "detail": f"destination parent directory is not writable: {parent}",
                "remedy": "choose a destination whose parent directory is writable",
            }

        with self._store_lock:
            try:
                dest_conn = sqlite3.connect(str(dest))
                try:
                    assert self._store_conn is not None
                    self._store_conn.backup(dest_conn)
                finally:
                    dest_conn.close()
            except sqlite3.Error as exc:
                return {
                    "status": "rejected",
                    "reason": "snapshot_failed",
                    "detail": str(exc),
                    "remedy": (
                        "retry snapshot(); if it persists, check disk space "
                        "and destination permissions"
                    ),
                }
        return {"status": "ok", "result": {"dest_path": str(dest)}}


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------


def main() -> int:
    owner = EngramdOwner()
    try:
        owner.start(install_signal_handlers=True)
    except OwnerAlreadyRunning as exc:
        print(f"[engramd] {exc}", file=sys.stderr)
        return 1
    print(f"[engramd] listening on {owner.socket_path} (pid {os.getpid()})", file=sys.stderr)
    owner.wait_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
