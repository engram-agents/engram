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

## Socket path resolution -- replicated, not shared; the "never modify
## engramd_client.py" note below is U3's OWN historical scope bound, now
## superseded for the socket-path constant specifically (#3194)

`engramd_client._resolve_socket_path()` is private and this module does
NOT import it, nor does it drop the leading underscore in
`engramd_client.py` to make it a shared public helper (an option the U3
fairy spec explicitly offered as "your call") -- U3's own spec's "Scope
bounds" section said plainly "never modify it", which was the harder
constraint at the time and won. `_resolve_engram_home()` below replicates
`_resolve_socket_path()`'s exact ENGRAM_HOME-with-tilde-fallback logic
instead of importing it.

**#3194 (P1-U4a, the socket permission layer) is a deliberate, narrow
exception to that note, ruled by Borges on issue #3194's comment thread:**
editing `engramd_client.py`'s socket-path *constant* is in-scope for the
permission layer, since the socket's location is part of the permission
model -- there is no deployed caller of either module yet, so moving the
constant costs nothing. This module still does not import the private
`_resolve_socket_path()` function itself; it imports the two now-public
constants (`ec.SOCKET_DIR_NAME`, `ec.DEFAULT_SOCKET_NAME`) and composes
them the same way `_resolve_socket_path()` does, keeping the "run"
subdirectory name single-sourced in `engramd_client.py` rather than
duplicated here as a second string literal.

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

## Per-request deadline, round 2 -- the deadline now covers DISPATCH/
## EXECUTION too, not only frame acquisition and response delivery (#3194
## round 2, Borges's designer ruling on issue #3194's comment thread,
## following Mira's security pre-review finding)

The paragraph above (and tests 8/9/16 in tests/test_engramd_owner.py)
covers only the READ side: `_read_request_line()`'s per-recv re-arming.
Round 1 of #3194 left a real gap Mira's pre-review found: `_dispatch()`
ran SYNCHRONOUSLY with no deadline awareness at all, and the deadline was
only re-checked AFTER it returned. `snapshot()`'s single call to
`sqlite3.Connection.backup()` ran to completion under `_owner_lock`, held
for the whole call -- an overrun request didn't just reply late, it held
the lock past the deadline and blocked every OTHER request needing it
(today: only another `snapshot`). That is the same denial-of-service
shape item 9 exists to prevent, just triggered by slow EXECUTION instead
of a slow peer.

Borges's ruling (contract text, restated here so the mechanism and its
justification live in one place): every client-facing method now runs
under the request's deadline through execution, not only through framing;
the budget MAY differ by method, chosen once the method is parsed
(`_handle_connection()` recomputes `deadline` as `accept_time +
self.snapshot_request_deadline` for `method == "snapshot"` specifically,
never resetting the anchor -- `accept_time`, not "now"); and when
execution passes the deadline, the owner stops the WORK, not just the
wait: `_handle_snapshot()`'s `progress` callback (passed to
`sqlite3.Connection.backup(dest_conn, pages=SNAPSHOT_BACKUP_STEP_PAGES,
progress=..., sleep=0)`) raises `_SnapshotDeadlineExceeded` once
`time.monotonic()` passes the snapshot EXECUTION deadline (the possibly
method-extended request deadline minus `SNAPSHOT_RESPONSE_RESERVE`); the
`with self._owner_lock:` block's own `__exit__` is what guarantees the
lock is released on that exception (no second, hand-rolled release path
-- see `_handle_snapshot`'s own docstring), and the aborted call's partial
destination file is explicitly unlinked before a `{"status": "rejected",
"reason": "deadline_exceeded", ...}` wire dict is returned. Round 6
(#3194) split the budget so that dict normally DOES reach the client:
`snapshot()` stops executing `SNAPSHOT_RESPONSE_RESERVE` seconds before the
request deadline, so its timeout response is composed with the reserve still
in hand. The post-dispatch `remaining <= 0` send gate is unchanged and still
drops a response composed after the request deadline (a scheduler stall past
the reserve), which a snapshot() client reads as `Unavailable` ==
INDETERMINATE -- see `_handle_snapshot()`'s docstring.

`SNAPSHOT_REQUEST_DEADLINE` (30s default) exists because
`DEFAULT_REQUEST_DEADLINE` (5s) is not a realistic backup budget for a
real `knowledge.db` -- "no budget for internal operations" was
explicitly ruled out as an allowed value. See
tests/test_engramd_owner.py Test 19 for the discriminating mutations
(deadline-coverage: revert to synchronous unbounded dispatch, test 19
must go red while 8/9/16 stay green; lock-leak: abort but skip the
`_owner_lock` release, a FOLLOW-UP snapshot -- not a `ping`, which never
takes this lock -- must then fail/hang).

## Two connections, not one (#2652 Phase-1 Unit 4b, issue #3195)

Per Mira's ruling in revision 5 of docs/2652-security-slice-design.md SS3.1
("(b), two connections inside the one owner process"), this module holds
TWO sqlite3 connections against the same `store_path`, never one:

- `self._owner_conn` (was `self._store_conn` in U3) -- owner-internal.
  Serves `snapshot()` and any future owner-internal operation. Client SQL
  never dispatches onto it. Its authorizer (`_owner_internal_authorizer`
  below) is deliberately PERMISSIVE, set explicitly rather than left
  authorizer-less -- a placeholder for future owner-internal operations
  that may need one, not a security boundary. **Not currently exercised
  by any test**: `snapshot()`'s `.backup()` call never consults an
  authorizer at all (measured; see "A measured, load-bearing surprise"
  below), so this authorizer's own behavior is unfalsifiable by any test
  in this build. (An earlier draft of this comment claimed the opposite
  -- that this authorizer exists "for testability" -- which Mira's
  review of PR #3221 measured false: removing the `set_authorizer` call
  entirely leaves every test green. See
  docs/2652-security-slice-design.md SS3.1 revision 6.)
- `self._client_read_conn` -- the permanently restrictive connection.
  Its authorizer (`_client_read_authorizer` below) is the measured
  baseline floor (SS3.1, SS4) and is set ONCE at connection-open time in
  `start()`, never swapped, toggled, or reset for any operation. **No
  operation in this build dispatches a client SQL statement onto this
  connection yet** -- that is U4c's job (the read-template registry) and
  U4d's (wiring real clients). This build's job is the plumbing: the
  connection exists, its authorizer is correct and tested
  (`_ClientReadGate` below is the structurally-isolated object a future
  client-facing read would call through), and nothing in `_dispatch()`
  reaches it.

## A measured, load-bearing surprise: `sqlite3.Connection.backup()` never
## invokes the authorizer, on either side, at all

Measured directly (see the PR body / fairy handoff for the full probe):
with a DENY-ALL authorizer set on the source connection, `.backup()` still
completes successfully with every row copied, and the authorizer callback
is invoked ZERO times. This holds on both Python 3.9/sqlite 3.54.0 and
Python 3.12/sqlite 3.53.4 -- it is the C-level backup API bypassing SQL
statement preparation entirely (the authorizer fires only from
`sqlite3_prepare`), not a version artifact. Consequence: denying
`SQLITE_TRANSACTION` (or anything else) on `_owner_conn`'s authorizer has
NO EFFECT on `_handle_snapshot()`'s current bare-`.backup()`-call shape --
the snapshot succeeds regardless of what the authorizer says, because
`.backup()` never asks it anything. This is flagged prominently in the
fairy handoff as a spec/reality mismatch for test 17's literal mutation,
per this build's escape valve, rather than silently worked around by
adding new SQL-statement scope this unit's spec explicitly says does not
apply to this file.

## Resource bounds on the client-read connection (SS4: three axes, no one
## of them sufficient alone)

`_arm_read_deadline()` below installs a `progress_handler` that aborts
once a caller-supplied ABSOLUTE `time.monotonic()` deadline has passed --
bounding VM-instruction cost. `CLIENT_READ_LIMIT_LENGTH`, set via
`conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, ...)` in `start()`, bounds the
size of any single string/blob/row -- independently of the progress
handler, which does not see allocation at all (SS4's own zeroblob
control, reproduced in this build's test 19). The THIRD axis -- one
end-to-end request deadline covering wall clock across framing and
execution -- is U4a's (issue #3194 / PR #3201), not built here: no client
dispatch operation exists yet to enforce it against.  `_arm_read_deadline`
is designed so that whichever deadline U4a eventually computes can be
passed straight through as its `deadline` parameter -- the mechanism does
not care where the number came from, and must never own an independent
timer or module constant in its place.
"""

from __future__ import annotations

import errno
import json
import os
import platform
import shutil
import signal
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

# Production default for snapshot()'s OWN, larger, named request-deadline
# budget (#3194 round 2, Borges's ruling: "5s is not a realistic backup
# budget for a real knowledge.db. It must still be a named, finite number.
# 'No budget for internal operations' is not an allowed value."). Still
# anchored to the SAME accept-time deadline as every other method -- see
# the module docstring's "Per-request deadline, round 2" section and
# _handle_connection() below. The dispatching agent measured a real 48.62
# MB knowledge.db backing up via this exact call path in 0.0758s on this
# fleet's hardware, so 30s is generous headroom, not a tight fit.
SNAPSHOT_REQUEST_DEADLINE = 30.0  # seconds

# The stepping granularity for snapshot()'s deadline-checked backup (#3194
# round 2). Passed as sqlite3.Connection.backup()'s own `pages=` argument:
# the `progress` callback fires once per this many pages copied, and is
# where the deadline is actually checked/enforced -- see _handle_snapshot()
# below. 100 pages (SQLite's default page size is 4096 bytes, so ~400 KiB
# per step) balances two things: fine enough granularity that "one step" of
# overrun (Borges's #3194 amended-contract ruling, point 3's allowance --
# NOT one of the original issue #3096 numbered contract items, a
# reviewer-fairy-caught miscitation this comment used to carry) stays
# small even for a large store,
# while not so fine (e.g. pages=1) that Python-level callback overhead
# meaningfully slows down a normal, well-inside-budget backup.
SNAPSHOT_BACKUP_STEP_PAGES = 100

# The MAXIMUM wall-clock time a single backup step (SNAPSHOT_BACKUP_STEP_PAGES
# pages) may take, and therefore the deadline-overrun allowance the design
# doc's "budget plus one step" language (§4, ruling on #3194's comment
# thread) actually names -- Mira's security review of the paired doc PR
# (#3200) found the original round-2 text left this allowance unbounded
# ("permits 'deadline plus one step' without stating/bounding that step");
# Borges's fold requires the implementation to name its stop step AND state
# its maximum wall time, with that stated figure as the allowance, no
# number invented. MEASURED, not picked: on this seat, backing up an ~78 MB
# store (the same shape tests/test_engramd_owner.py's _seed_large_store()
# uses) with pages=SNAPSHOT_BACKUP_STEP_PAGES, the slowest of 201 steps was
# 0.0248s (the first step, plausibly warm-up cost; the remaining 200 steps
# were all under 0.0016s). This constant is that measurement with a 4x
# safety margin for a slower/loaded machine, not a tighter fit -- a test
# that asserts completion by (deadline + this constant) is the
# measured/bounded allowance the doc now requires, not an invented one.
SNAPSHOT_BACKUP_STEP_MAX_WALL_TIME = 0.1  # seconds

# Bounds the number of CONCURRENT finalize operations (fsync + os.replace)
# this owner will run at once (#3194 round 5, Borges's revised ruling,
# point 3: "2 is a reasonable start"). A STARTING HYPOTHESIS, not a
# measured ceiling -- unlike SNAPSHOT_BACKUP_STEP_MAX_WALL_TIME above, no
# measurement informs this number; it exists so an unbounded pile of
# slow-disk finalize threads and their temp files can never accumulate. A
# snapshot() call that finds no free slot is refused `rejected/busy`
# immediately, before any work (the store lock, the backup, the temp
# file) begins -- see `_handle_snapshot()`'s own docstring for the full
# protocol.
SNAPSHOT_FINALIZE_SLOTS = 2

# The part of snapshot()'s request deadline held back for DELIVERING the
# response (#3194 round 6, Borges's ruling on the round-5 send-gate
# finding). The design's one deadline covers frame acquisition, execution
# AND response delivery, so execution must stop earlier:
#   execution_deadline = request_deadline - SNAPSHOT_RESPONSE_RESERVE
# and everything execution-side (the backup progress check, the request
# thread's wait on the finalize worker, the worker's abandon-or-commit
# gate) uses it. A timeout-driven response is then composed with this
# reserve still in hand, and the send gate in _handle_connection() (which
# still compares against the REQUEST deadline, and stays) lets it through.
# Read at CALL time, so tests can monkeypatch it.
#
# MEASURED, not picked: time from the execution deadline to the end of
# compose + json.dumps + sendall (+ close) of the largest response the
# owner can produce on a timeout (the 344-byte commit: "indeterminate"
# shape), on a real EngramdOwner and a real unix-socket client, 1,000
# iterations each. Python 3.12.3, Linux 6.6.87.2-microsoft-standard-WSL2
# x86_64, 32 CPUs. Idle: min 0.10 ms / median 0.16 ms / p99 0.60 ms / MAX
# 3.0 ms. Under 4 busy-loop processes: median 0.13 ms / p99 0.31 ms / MAX
# 0.42 ms. 0.25 s is ~80x the worst observed (3.0 ms) -- chosen for scheduler
# stalls (a paused/loaded CI runner or a stop-the-world GC), not to fit the
# steady state. That ~80x is the margin for the worker/committing shape,
# which is composed right at the execution deadline. The MID-BACKUP abort
# shape is composed up to one backup step later (SNAPSHOT_BACKUP_STEP_MAX_
# WALL_TIME, 0.1 s) plus close/unlink, so the inequality that actually has
# to hold is reserve >= step allowance + delivery (0.25 >= 0.1 + ~0.004,
# about 2.4x the allowance); it is exercised over a real socket by
# test_r6_mid_backup_abort_reaches_real_client_*. The margin is a judgment about stalls, not a measurement of
# them; the result is still not a guarantee, which is why a client must read
# an Unavailable from snapshot() as INDETERMINATE (see _handle_snapshot()).
# To re-measure: wrap `_handle_connection()`, note accept time, and record
# (return time) - (accept + snapshot_request_deadline - reserve) for a real
# snapshot() call over the socket with a slow os.replace injected so the
# commit state straddles the execution deadline (temporarily set the reserve
# small enough that the send gate does not drop the response); 1,000+
# iterations, idle and loaded, and report the MAX.
SNAPSHOT_RESPONSE_RESERVE = 0.25  # seconds

# How often the LISTENING socket's accept() wakes up to check the shutdown
# flag. Short enough that .stop() returns quickly in tests (this process's
# accept loop has nothing else to do between wakeups, unlike
# engram-surface-daemon.py's heavier 5.0s, which also runs an idle watchdog
# and an indexer tick this module has neither of).
ACCEPT_POLL_INTERVAL = 0.2  # seconds

# Mode applied to both the socket's `run/` parent directory and the socket
# file itself (#3194, design doc §2.1 + Borges's issue-#3194 ruling). The
# directory is the PRIMARY gate (created 0700 before bind, ACLs stripped);
# the socket's own mode is defense-in-depth, set explicitly rather than
# left to whatever the process umask produces (measured: a plain bind()
# leaves the socket at 0755 -- design doc §2.1/P5).
RUN_DIR_MODE = 0o700
SOCKET_FILE_MODE = 0o700

# Linux ucred{pid, uid, gid} -- SO_PEERCRED's wire shape (design doc §2.2).
# Native alignment ("@") and an explicit expected byte length are BOTH
# load-bearing: a parser written with "=" (standard sizes, no alignment)
# reads the buffer as the wrong shape on some platforms, and pinning the
# length to whatever calcsize() returns would silently agree with that
# mistake instead of catching it. struct.unpack_from() (never a slice) is
# what makes a short/malformed buffer RAISE instead of silently decoding
# to uid 0 -- see _check_peer_credentials() and contract test 2's
# discrimination mutation (slice form int.from_bytes(raw[4:8], "little")).
PEERCRED_STRUCT_FORMAT = "@3i"
PEERCRED_STRUCT_SIZE = struct.calcsize(PEERCRED_STRUCT_FORMAT)  # 12 on this fleet's Linux seats


class OwnerAlreadyRunning(RuntimeError):
    """Raised by EngramdOwner.start() when a confirmably-live owner already
    holds this seat's socket (contract item 1). `main()` below catches this
    and exits non-zero with a clear message; callers that embed
    EngramdOwner directly (e.g. tests) see the exception itself.

    Also raised (#3194, #3152 item 3) when start()'s pre-bind liveness
    check passed but a concurrent process won the actual bind() race --
    the loser reports the same handled exception a pre-check refusal
    would, rather than letting a raw OSError propagate. See
    `_bind_socket_or_raise()` below."""


class OwnerPermissionUnsupported(RuntimeError):
    """Raised by EngramdOwner.start() when this seat's platform/tooling
    cannot support the socket permission layer (#3194, design doc §2.2's
    posture: "the fallback on an unrecognised platform is to REFUSE TO
    START -- never to skip the check", extended here to the ACL-strip
    requirement of §2.1 and to a missing socket.SO_PEERCRED). A permission
    check that silently no-ops where it cannot run is worse than none --
    the deployment would believe it has one. This build only implements
    the Linux arm (issue #3194 scope bounds; Darwin's LOCAL_PEERCRED /
    `chmod -N` equivalents are out of scope), so a non-Linux seat, a
    seat missing the `setfacl` binary, or a Python/OS build with no
    SO_PEERCRED all refuse to start via this exception rather than
    starting with a silently-unenforced permission layer."""


class OwnerStalePathUnsafe(RuntimeError):
    """Raised by EngramdOwner.start() when something other than a plain,
    ownerless socket file sits at the socket path (#3194, design doc
    §2.4: stale-socket replacement must never be a file-overwrite
    primitive). Two cases: a symlink (which could point anywhere -- an
    attacker or a stale reference could make unlink-then-bind write
    through it to an arbitrary location), or a regular file/other
    non-socket object (unknown provenance -- overwriting it sight-unseen
    is exactly the "write a file anywhere the owner can reach" risk the
    design doc names). A distinct exception from OwnerAlreadyRunning
    because the underlying condition is different: this is not "a live
    owner is already here", it's "something unsafe to touch is here"."""


class _SnapshotDeadlineExceeded(Exception):
    """Internal-only control-flow signal (#3194 round 2) -- raised by
    `_handle_snapshot()`'s `progress` callback (passed to
    `sqlite3.Connection.backup()`) once `time.monotonic()` passes the
    request's (possibly method-extended) deadline mid-backup, and caught
    inside `_handle_snapshot()` itself; it never escapes that method or
    reaches a caller. Deliberately NOT a subclass of sqlite3.Error --
    `backup()` propagates a callback's raise cleanly out
    (dispatching-agent-measured, restated in the fairy handoff), and this
    must be caught by its OWN except clause, distinct from the existing
    `except sqlite3.Error` around the whole snapshot attempt, so a genuine
    sqlite3 failure and a deliberate deadline abort are never confused
    with each other."""


class _FinalizeOutcome:
    """Lock-guarded, per-call commit-state machine for the bounded
    finalize worker (#3194 round 5, Borges's revised ruling, point 4).
    One instance per `snapshot()` call -- never shared across calls or
    owned by the EngramdOwner instance itself, unlike
    `SNAPSHOT_FINALIZE_SLOTS`'s shared semaphore. States: `"pending"` ->
    `"committing"` -> `"committed"` / `"aborted"`, plus `"abandoned"` (a
    request-thread-only transition out of `"pending"`, distinct from the
    worker's own `"aborted"` -- see `_handle_snapshot()`'s own docstring
    for why the two are kept separate rather than collapsed into one).

    Plain string states, not an enum, matching this module's existing
    style (`"rejected"`, `"deadline_exceeded"`, etc. are module-level
    string literals already, not enum members).

    `lock` guards every read AND write of `state`/`reason` -- both the
    worker thread and the request thread touch this object, and the whole
    point of the commit protocol is that the two must never observe or
    mutate `state` without holding this lock. `event` is set exactly once,
    by the worker, in its own outer `finally`, on every exit path
    (committed or aborted) -- it is what the request thread waits on,
    bounded by the call's EXECUTION deadline (request deadline minus
    SNAPSHOT_RESPONSE_RESERVE, round 6), rather than joining
    the worker thread directly (see `_handle_snapshot()`'s docstring for
    why a bare `.join()` would defeat the whole response-time bound)."""

    __slots__ = ("state", "reason", "lock", "event")

    def __init__(self) -> None:
        self.state = "pending"
        self.reason: str | None = None
        self.lock = threading.Lock()
        self.event = threading.Event()


# ---------------------------------------------------------------------------
# Socket path / ENGRAM_HOME resolution -- replicated from
# engramd_client._resolve_socket_path's exact logic (see module docstring's
# "Socket path resolution" section for why this is a replication, not an
# import of that private function).
# ---------------------------------------------------------------------------


def _resolve_engram_home(engram_home: str | Path | None) -> Path:
    """Always returns an ABSOLUTE path (#3152 item 2, mirroring
    engram-surface-daemon.py's own gh#2695 hardening -- see that module's
    `_resolve_engram_home()` docstring). A relative ENGRAM_HOME from two
    different launch CWDs would otherwise bind two different sockets --
    "one owner per seat" (contract item 1) fails silently, since the
    liveness/staleness check in start() compares socket PATHS, not
    resolved identities.

    `os.path.abspath()`, not `Path.resolve()`, deliberately -- same
    reasoning as the surface daemon: it makes the path absolute relative
    to cwd WITHOUT resolving symlinks. An intermediate symlinked
    ENGRAM_HOME component keeps resolving the way it always has; using
    `.resolve()` would be a second, unrelated behavior change (following
    symlinks) bundled into this fix."""
    if engram_home is not None:
        return Path(os.path.abspath(str(engram_home)))
    raw = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
    return Path(os.path.abspath(raw))


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


# ---------------------------------------------------------------------------
# The client-read connection's authorizer floor (#2652 Phase-1 Unit 4b,
# issue #3195) -- docs/2652-security-slice-design.md SS3.1, SS4.
# ---------------------------------------------------------------------------

# Baseline allowlist, DERIVED by measurement rather than copied verbatim
# from the design doc's own cited names (see the module docstring's "A
# measured..." section and the fairy handoff / PR body for the full
# measurement output). A representative SELECT against ENGRAM's real
# nodes/edges schema (a WHERE clause, a LIKE predicate, a projection
# touching several columns) was run under an AUDITING authorizer that
# logs every action_code it is asked about, against a fixture DB built
# from the real DDL (src/engram/engram_core.py's `CREATE TABLE nodes` /
# `CREATE TABLE edges`). Only three actions fired: SQLITE_SELECT,
# SQLITE_READ (once per column touched), and SQLITE_FUNCTION (LIKE's
# implicit function call). Nothing beyond these three fired for an
# ordinary read; if a future caller's read pattern fires something else,
# that is real information to report, not a signal to silently widen this
# allowlist.
CLIENT_READ_ALWAYS_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
)

# No real caller is wired to a pragma yet -- this build adds no client
# dispatch path at all (see the module docstring's "Two connections"
# section). `user_version` is kept as the one harmless read-only pragma
# with an obvious future use (a census / health-check probe of the
# store's schema version). Add more names here only once a real caller
# needs them, through the same measurement discipline as the baseline
# above -- this is a builder's-call design choice, not a measured set.
CLIENT_READ_PRAGMA_ALLOWLIST = frozenset({"user_version"})


def _client_read_authorizer(action_code, arg1, arg2, db_name, trigger_or_view):
    """The restrictive floor -- set ONCE on `_client_read_conn` at
    connection-open time in `start()` and NEVER swapped, toggled, or reset
    for any operation (docs/2652-security-slice-design.md SS3.1, Mira's
    revision-5 ruling: "no authorizer swapping"). Allows the measured
    baseline read action set; denies everything else, with two explicit,
    measured exceptions for PRAGMA and TRANSACTION below."""
    if action_code in CLIENT_READ_ALWAYS_ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK

    if action_code == sqlite3.SQLITE_PRAGMA:
        # P16 (docs/2652-security-slice-design.md SS4, quoted in the fairy
        # spec): a naive "allow by name at arg1" lets a PRAGMA WRITE
        # through, because arg1 carries the pragma's NAME for both a read
        # and a write -- only arg2 (the assigned value) distinguishes
        # them, and it is None for a read. Deny outright whenever arg2 is
        # not None, REGARDLESS of whether arg1 is allowlisted -- test 11's
        # mutation (dropping this clause) exists to catch exactly this
        # ordering regressing.
        if arg2 is not None:
            return sqlite3.SQLITE_DENY
        if arg1 in CLIENT_READ_PRAGMA_ALLOWLIST:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    if action_code == sqlite3.SQLITE_TRANSACTION:
        # A bare autocommit SELECT never needs a transaction, so the
        # client-read connection never allows one "just in case". (An
        # earlier draft of this comment justified the denial by contrast
        # with the owner-internal connection allowing SQLITE_TRANSACTION
        # for snapshot()'s BEGIN DEFERRED read-mark -- that read-mark
        # never existed on this build's snapshot() RPC; see
        # docs/2652-security-slice-design.md SS3.1 revision 6.)
        return sqlite3.SQLITE_DENY

    return sqlite3.SQLITE_DENY


def _owner_internal_authorizer(action_code, arg1, arg2, db_name, trigger_or_view):
    """Set explicitly on `_owner_conn` -- PERMISSIVE (allows everything)
    BY DESIGN, not left authorizer-less. This callback is NOT a security
    boundary -- `_client_read_authorizer` above is where that boundary
    lives; this one is a placeholder for future owner-internal
    operations.

    NOT exercised by any test in this build: `snapshot()`'s `.backup()`
    call never consults an authorizer at all (measured -- see "A
    measured, load-bearing surprise" in the module docstring), so this
    callback's own behavior is currently unfalsifiable. (Mira's review of
    PR #3221 measured this directly: removing this `set_authorizer` call
    entirely leaves every test green. An earlier version of this
    docstring claimed the opposite -- that this authorizer was needed to
    make test 17 falsifiable -- which was true only under the design
    doc's now-retracted BEGIN/READ/COMMIT requirement; see
    docs/2652-security-slice-design.md SS3.1 revision 6.)"""
    return sqlite3.SQLITE_OK


# ---------------------------------------------------------------------------
# Resource bounds on the client-read connection (SS4: three axes, no one
# of them sufficient alone -- see the module docstring's "Resource
# bounds" section for the full reasoning).
# ---------------------------------------------------------------------------

# Measured (see the PR body / fairy handoff for the full measurement
# output): against a fixture DB built from ENGRAM's real nodes/edges
# schema, an expensive WITH RECURSIVE query (~0.3s unbounded) aborted
# within single-digit microseconds of its deadline at candidate n=100,
# 500, and 1000 alike; a cheap single-row SELECT showed no measurable
# overhead at any of the three. 1000 is chosen for the lowest
# per-instruction-check overhead on a legitimate long read, since all
# three candidates were otherwise indistinguishable at this query's
# instruction density.
PROGRESS_HANDLER_INSTRUCTION_COUNT = 1000

# Measured (see the PR body / fairy handoff): the real, live
# ~/.engram/knowledge.db (3,324 rows in `nodes`, per the fairy spec) has
# max LENGTH(embedding)=8,474B, max LENGTH(metadata)=21,272B,
# max LENGTH(claim)=2,462B. 2,000,000 gives ~94x headroom over the
# largest of those (metadata) -- room for embedding-dimension growth and
# larger metadata blobs -- while still bounding a zeroblob-style
# single-value allocation (SS4: a progress handler armed with NO setlimit
# lets an 8 MiB zeroblob return in full, 0 progress callbacks -- measured
# again in this build's test 19 control).
CLIENT_READ_LIMIT_LENGTH = 2_000_000


def _arm_read_deadline(
    conn: sqlite3.Connection,
    deadline: float,
    *,
    n: int = PROGRESS_HANDLER_INSTRUCTION_COUNT,
) -> None:
    """Install a progress_handler on `conn` that aborts (SQLite treats any
    truthy return from the callback as an interrupt request, raising
    sqlite3.OperationalError("interrupted") from the in-flight statement)
    once `deadline` -- an ABSOLUTE time.monotonic() value -- has passed.

    `deadline` is ALWAYS an explicit parameter, never a connection-level
    stored timer or a module constant: this is designed to accept
    whatever deadline the eventual client-dispatch path passes in (U4a's
    end-to-end request deadline, issue #3194 / PR #3201, once wired --
    see docs/2652-security-slice-design.md SS4's own wording: "the
    progress_handler must stop against the SAME request deadline, passed
    in -- not an independent timer or constant"). No caller exists yet in
    this build (no operation in `_dispatch()` reaches `_client_read_conn`
    at all), so this build's own tests call this helper directly with an
    explicit deadline rather than going through the socket.
    """

    def _check() -> int:
        return 1 if time.monotonic() >= deadline else 0

    conn.set_progress_handler(_check, n)


class _ClientReadGate:
    """Holds ONLY the client-read connection. Any future client-facing
    read operation (U4c's read-template registry, U4d's real client
    wiring) dispatches through an object shaped like this one, never
    through EngramdOwner directly -- so it is structurally impossible for
    a client-facing read to reach the owner-internal connection even by
    accident (test 22). Deliberately independent of the authorizer: this
    isolates ROUTING, not the floor (`_client_read_authorizer` above is
    the floor itself; test 7 in the design doc's own numbering, not owned
    by this unit, is what checks the floor can't be bypassed by a
    registry bug)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple = (), *, deadline: float) -> list:
        """Minimal execute-with-deadline path, exercised by this build's
        own tests (12, 19, 22) standing in for the not-yet-built real
        client dispatch. Not a public operation of this build -- no
        `method` in `EngramdOwner._dispatch()` calls this; nothing wires
        a real client to it yet."""
        _arm_read_deadline(self._conn, deadline)
        try:
            return self._conn.execute(sql, params).fetchall()
        finally:
            # Progress handlers persist on the connection until replaced
            # or cleared -- clear it so a later call (with its own,
            # different deadline) always starts from a clean slate rather
            # than silently inheriting a deadline that has already
            # passed.
            self._conn.set_progress_handler(None, 0)


class EngramdOwner:
    """The owner process skeleton -- one per seat, sole PROCESS holding
    `knowledge.db` open within this build's scope (handshake + `ping` +
    `snapshot()` only). Per Mira's revision-5 ruling
    (docs/2652-security-slice-design.md SS3.1), this process holds TWO
    connections against that one store, never one: `_owner_conn`
    (permissive, owner-internal -- serves `snapshot()`) and
    `_client_read_conn` (the permanently restrictive floor -- see the
    module docstring's "Two connections" section for the full reasoning;
    nothing in this build's `_dispatch()` reaches the latter yet). See
    the module docstring for the full design context and what is
    deliberately not built yet."""

    def __init__(
        self,
        engram_home: str | Path | None = None,
        *,
        request_deadline: float = DEFAULT_REQUEST_DEADLINE,
        snapshot_request_deadline: float = SNAPSHOT_REQUEST_DEADLINE,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        accept_poll_interval: float = ACCEPT_POLL_INTERVAL,
        contract_version: str = ec.CONTRACT_VERSION,
    ) -> None:
        self.engram_home = _resolve_engram_home(engram_home)
        # The socket's parent directory (self.socket_path.parent) is the
        # `run/` directory this unit hardens -- see start()'s directory
        # setup below (#3194).
        self.socket_path = self.engram_home / ec.SOCKET_DIR_NAME / ec.DEFAULT_SOCKET_NAME
        self.pid_path = self.engram_home / DEFAULT_PID_NAME
        self.store_path = self.engram_home / DEFAULT_STORE_NAME
        self.request_deadline = request_deadline
        # snapshot()'s own, larger, named budget (#3194 round 2) -- a plain
        # instance attribute, not read-once-at-construction-time-only, so a
        # test can mutate it between calls (see
        # tests/test_engramd_owner.py Test 19's lock-release proof, which
        # widens this for its follow-up snapshot rather than constructing a
        # second owner). See the module docstring's "Per-request deadline,
        # round 2" section for the full mechanism.
        self.snapshot_request_deadline = snapshot_request_deadline
        self.max_request_bytes = max_request_bytes
        self.accept_poll_interval = accept_poll_interval
        self.contract_version = contract_version

        self._server: socket.socket | None = None
        # Owner-internal connection (was `_store_conn` in U3) -- serves
        # snapshot() and any future owner-internal operation. Client SQL
        # never dispatches onto it.
        self._owner_conn: sqlite3.Connection | None = None
        # The permanently-restrictive client-read connection. Opened
        # alongside `_owner_conn` in start(), against the same
        # store_path. No operation in this build dispatches client SQL
        # onto it yet -- see the module docstring.
        self._client_read_conn: sqlite3.Connection | None = None
        # The structurally-isolated object a future client-facing read
        # would call through (test 22) -- holds ONLY `_client_read_conn`.
        # Constructed in start(), once that connection exists; None
        # before start() / after stop(), mirroring the connection
        # attributes' own None-until-started lifecycle.
        self._client_read_gate: _ClientReadGate | None = None
        # Guards BOTH connections' lifecycle -- was `_store_lock` in U3,
        # renamed because it now protects two connections, not one (see
        # _cleanup() and _handle_snapshot() below; folds #3152 item 1 for
        # both).
        self._owner_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._bound_socket = False
        self._accept_thread: threading.Thread | None = None
        # #3194 round 5: bounds concurrent finalize (fsync + os.replace)
        # work across every snapshot() call this owner serves -- shared
        # for this owner's whole lifetime, unlike _FinalizeOutcome (which
        # is per-call). See SNAPSHOT_FINALIZE_SLOTS's own comment and
        # _handle_snapshot()'s docstring for the full protocol.
        self._finalize_slots = threading.BoundedSemaphore(SNAPSHOT_FINALIZE_SLOTS)

    # -- lifecycle ------------------------------------------------------

    def start(self, *, install_signal_handlers: bool = True) -> None:
        """Bind the socket, open the store, and begin serving in a
        background thread. Returns once the accept loop is running.

        Raises OwnerAlreadyRunning if a confirmably-live owner already
        holds this seat's socket (contract item 1, and #3152 item 3's
        bind()-race variant) -- nothing is touched in that case; the
        existing owner is left completely unaffected. Raises
        OwnerPermissionUnsupported if this seat's platform/tooling cannot
        support the socket permission layer (#3194). Raises
        OwnerStalePathUnsafe if something other than a plain, ownerless
        socket sits at the socket path (#3194, design doc §2.4).

        `install_signal_handlers=False` (tests only -- see
        tests/test_engramd_owner.py's owner_running fixture) skips
        registering SIGTERM/SIGINT handlers, since signal.signal() is
        process-global and installing handlers repeatedly across a test
        session would clobber pytest's own signal handling. The cleanup
        path this skips is otherwise identical: the real handler
        (_handle_signal) just calls .stop(), the same method tests call
        directly.
        """
        self._check_platform_support()

        self.engram_home.mkdir(parents=True, exist_ok=True)

        # `run/` (the socket's parent) MUST be hardened -- and, load-
        # bearing, its own symlink/ownership refusal checked -- BEFORE
        # `_check_stale_socket_or_raise()` runs, not merely before
        # `_harden_run_dir()`'s own chmod/setfacl calls (#3194, Mira's
        # ordering finding on #3201's exact head 18cd14b2: with the
        # OLD ordering, `self.socket_path` -- which traverses `run/` as a
        # path COMPONENT -- resolves through a `run -> elsewhere` symlink
        # before `_harden_run_dir()` ever got a chance to refuse it, so
        # `_check_stale_socket_or_raise()`'s own `os.lstat(self.socket_path)`
        # (which only refuses the FINAL path component's symlink-ness, not
        # symlinks earlier in the path) could still find-and-unlink a
        # stale socket sitting under the symlink's target -- a path-
        # following side effect that landed before any guard ran, exact-
        # head reproduced with `home/run -> outside` + a stale
        # `outside/engramd.sock`). Hardening `run/` first means every
        # later use of `self.socket_path` in this method is already known
        # to traverse a plain, owned directory, never a redirect. Run on
        # EVERY start(), not just first-creation -- defense in depth so a
        # pre-existing `run/` from an old install gets re-secured too
        # (design doc §2.1's "created 0700 at bind time" language, read
        # literally).
        self._harden_run_dir()

        self._check_stale_socket_or_raise()

        # One process, TWO open connections on the store (design doc SS3;
        # docs/2652-security-slice-design.md SS3.1) -- opened once here
        # and held for this owner's lifetime; snapshot() backs up FROM
        # the owner-internal connection object (contract item 5). No
        # domain-engine/manager machinery sits on top of either in this
        # build. sqlite3.connect() creates an empty file if store_path
        # doesn't yet exist -- acceptable here: nothing in this build's
        # contract requires the store to pre-exist, and a real seat's
        # bootstrap already creates knowledge.db before any daemon
        # starts.
        self._owner_conn = sqlite3.connect(str(self.store_path), check_same_thread=False)
        # Permissive by DESIGN, not authorizer-less -- see the module
        # docstring's "Two connections" section and
        # _owner_internal_authorizer's own docstring for why.
        self._owner_conn.set_authorizer(_owner_internal_authorizer)

        # The permanently-restrictive client-read connection -- opened
        # against the SAME store_path, its authorizer set ONCE here and
        # never swapped/toggled/reset for any operation. Its
        # SQLITE_LIMIT_LENGTH bound (the size axis of SS4's three
        # resource bounds) is likewise set once, at open time.
        self._client_read_conn = sqlite3.connect(str(self.store_path), check_same_thread=False)
        self._client_read_conn.set_authorizer(_client_read_authorizer)
        self._client_read_conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, CLIENT_READ_LIMIT_LENGTH)

        # The structurally-isolated object a future client-facing read
        # would call through (test 22) -- holds ONLY the connection just
        # opened above, never `_owner_conn`.
        self._client_read_gate = _ClientReadGate(self._client_read_conn)

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._bind_socket_or_raise()
        # Ownership flag flips ONLY after bind() succeeds -- mirrors
        # engram-surface-daemon.py's _bound_socket exactly: cleanup()'s
        # socket unlink is gated on this, never on pidfile identity alone,
        # so a process that never actually bound never deletes a live
        # holder's socket.
        self._bound_socket = True
        # The socket's own mode, set explicitly rather than left to
        # whatever the process umask produces (#3194, design doc §2.1/P5:
        # a plain bind() leaves the socket at 0755). Defense-in-depth --
        # the `run/` directory above is the primary gate.
        os.chmod(str(self.socket_path), SOCKET_FILE_MODE)
        self._server.listen(5)
        self._server.settimeout(self.accept_poll_interval)

        self.pid_path.write_text(str(os.getpid()))

        if install_signal_handlers:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)

        self._shutdown.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _check_platform_support(self) -> None:
        """Refuse to start rather than silently degrade the permission
        layer on a platform/toolchain this build cannot secure (#3194,
        design doc §2.2's posture -- "the fallback on an unrecognised
        platform is to REFUSE TO START, never to skip the check" --
        extended here to the ACL-strip requirement of §2.1). This build
        only implements the Linux arm (issue #3194 scope bounds; Darwin's
        LOCAL_PEERCRED / `chmod -N` equivalents are explicitly out of
        scope), so a non-Linux seat, a seat missing `setfacl`, or a
        Python/OS build with no socket.SO_PEERCRED all refuse here rather
        than starting with a silently-unenforced permission layer."""
        if platform.system() != "Linux":
            raise OwnerPermissionUnsupported(
                f"refusing to start: the socket permission layer (ACL "
                f"strip on run/, SO_PEERCRED peer check) is only "
                f"implemented for Linux in this build; this seat reports "
                f"platform.system() == {platform.system()!r}"
            )
        if shutil.which("setfacl") is None:
            raise OwnerPermissionUnsupported(
                "refusing to start: `setfacl` is required to strip "
                "inherited ACLs on the socket's run/ directory (design "
                "doc §2.1) and was not found on PATH"
            )
        if not hasattr(socket, "SO_PEERCRED"):
            raise OwnerPermissionUnsupported(
                "refusing to start: this Python/OS build has no "
                "socket.SO_PEERCRED -- the peer-credential check (design "
                "doc §2.2) cannot run here, and starting without it would "
                "be a silent permission-layer gap"
            )

    def _check_stale_socket_or_raise(self) -> None:
        """Contract items 1+2 (liveness / staleness) plus #3194's §2.4
        safety check, all keyed off ONE `os.lstat()` -- deliberately
        `lstat`, not `stat`: this must see whether the path ITSELF is a
        symlink, not what it points to (a `stat`-based check would follow
        the symlink and inspect the WRONG object).

        A live owner -> OwnerAlreadyRunning, nothing touched. A dead
        owner's stale plain socket -> unlinked, so a restart is never
        blocked forever (contract item 2). Anything else at the path (a
        symlink, or a non-socket file) -> OwnerStalePathUnsafe, and
        nothing is unlinked -- unlink-then-bind through an unknown object
        is exactly the file-overwrite primitive design doc §2.4 forbids.
        """
        try:
            st = os.lstat(self.socket_path)
        except OSError:
            return  # nothing at the path -- ordinary fresh start

        if self._recorded_pid_is_alive():
            raise OwnerAlreadyRunning(
                f"a live engramd owner already holds {self.socket_path} "
                f"-- refusing to start a second owner for this seat; "
                f"the existing owner is unaffected"
            )

        if stat.S_ISLNK(st.st_mode):
            raise OwnerStalePathUnsafe(
                f"refusing to start: {self.socket_path} is a symlink, not "
                f"a plain socket file -- unlinking and binding through it "
                f"could write anywhere the symlink points (design doc "
                f"§2.4); remove it by hand if you are certain it is safe "
                f"to discard"
            )
        if not stat.S_ISSOCK(st.st_mode):
            raise OwnerStalePathUnsafe(
                f"refusing to start: {self.socket_path} exists and is not "
                f"a socket (mode {stat.S_IMODE(st.st_mode):#o}) -- "
                f"refusing to unlink and overwrite an object of unknown "
                f"provenance (design doc §2.4); remove it by hand if you "
                f"are certain it is safe to discard"
            )
        # A genuine stale socket left by a crashed/killed owner (contract
        # item 2) -- detect it (dead PID) and replace it, never refuse
        # forever.
        try:
            self.socket_path.unlink()
        except OSError:
            pass

    def _harden_run_dir(self) -> None:
        """Create (or re-secure) the socket's parent directory, `run/`,
        0700 with no ACL entries (#3194, design doc §2.1 + Borges's
        issue-#3194 ruling). Runs on EVERY start(), not just
        first-creation.

        A fresh subdirectory of $ENGRAM_HOME still inherits $ENGRAM_HOME's
        own POSIX default ACL on this fleet's hosts, even after `chmod
        0700` -- `chmod` only affects the ACCESS ACL/mode, not an
        inherited DEFAULT ACL, and the default ACL survives and would
        apply to the socket file created inside (measured directly by the
        dispatching agent; see issue #3194's comment thread). `setfacl -b`
        strips BOTH the access-ACL's named entries and the inherited
        default ACL, leaving only the standard user/group/other mode-bit
        entries (also measured there).

        `os.umask(0o077)` is set explicitly around the directory creation
        rather than relying on whatever the process's current umask
        happens to be -- an inherited umask is a setting some other part
        of the process could have changed (design doc §2.1).

        PARENT-SYMLINK REFUSAL, SAME-FD MUTATION (#3194, Mira's security
        review on #3201's exact head 18cd14b2 -- a REAL, live-reproduced
        blocker, not a theoretical one; Borges's designer ruling on the
        same issue thread): the ORIGINAL round-1 code called
        `run_dir.mkdir(parents=True, exist_ok=True)` then `run_dir.chmod()`
        then `setfacl -b <path>` as three SEPARATE, path-based operations.
        `Path.mkdir(exist_ok=True)`'s existence check FOLLOWS symlinks
        (`is_dir()` returns True for a symlink pointing at a real
        directory), so a pre-existing `run -> /some/other/dir` symlink
        silently satisfied it and let `chmod`/`setfacl -b` mutate whatever
        `run` pointed at -- Mira's own fixture (`home/run -> outside`,
        outside mode 0755) demonstrated `start()` succeeding, `outside`
        being chmod'd 0700, and the socket binding inside it. This is the
        exact file-overwrite/redirect-primitive class of risk design doc
        SS2.4 names for the socket file itself, extended here to its
        PARENT directory, which the original build never guarded.

        Borges's ruling, implemented below: open the directory with
        `os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)` -- a SINGLE,
        atomic-at-the-kernel-level operation that refuses (`OSError`,
        measured as `ENOTDIR` on this seat's Linux, not `ELOOP` -- both
        are refusals, caught generically) to open a symlink at all, never
        silently following it. Every subsequent mutation
        (ownership check via `os.fstat`, mode via `os.fchmod`, ACL-strip
        via `setfacl -b /proc/self/fd/<fd>` -- the standard Linux
        fd-relative-path trick for tools that only take a path argument,
        verified directly: it operates on the FD'S target, not on
        whatever the original path string re-resolves to) goes through
        this SAME fd, never re-resolving the path string again. A
        directory that does not yet exist is `mkdir()`'d fresh first (so
        we know we created it, hence own it) before this same open+fstat+
        fchmod+setfacl sequence re-secures it -- re-securing is not
        skipped just because we made it ourselves this call, matching the
        "runs on EVERY start()" posture above. `FileExistsError` from that
        `mkdir()` is the normal "already there from a prior start()"
        case, not an error.

        NON-OWNED REFUSAL (Borges's ruling, third clause): for a
        directory that already existed (not created fresh by this call),
        `os.fstat(fd).st_uid` must equal this process's own uid -- refuse
        rather than harden a directory some other identity created,
        mirroring the same "don't touch what you don't know the
        provenance of" posture as the stale-socket check.

        RESIDUAL RISK, NAMED RATHER THAN CLAIMED CLOSED (Borges's ruling,
        explicit and deliberate, NOT a gap to close in this unit): the
        `mkdir()`-then-`open(O_NOFOLLOW)` sequence for a FRESH directory
        still has a small window between "nothing there" and "we hold an
        fd" in which a concurrent writer with access to $ENGRAM_HOME could
        swap in a symlink (out of design doc SS2.0's excluded "hostile
        process at the owner's own uid" case, but potentially in-scope if
        some OTHER identity can write there -- Mira measured her own
        $ENGRAM_HOME carries a named `spt` ACL entry with rwx; the
        premise this ruling rests on is that ONLY the owning uid and
        `spt` can write $ENGRAM_HOME on this fleet, and `spt` can already
        replace `knowledge.db` directly, so it is not a NEW privilege
        this gap would grant). Peer-credential checking still refuses a
        foreign uid wherever the socket ends up bound, regardless of this
        race. Closing the race fully would need the caller's OWN uid
        verified via a different mechanism entirely (client-side
        verification of the server's peer uid) if any seat's
        $ENGRAM_HOME is ever writable by a THIRD identity -- explicitly
        out of scope for this unit."""
        run_dir = self.socket_path.parent

        old_umask = os.umask(0o077)
        try:
            try:
                run_dir.mkdir(parents=True)
                created_fresh = True
            except FileExistsError:
                created_fresh = False
        finally:
            os.umask(old_umask)

        try:
            fd = os.open(str(run_dir), os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise OwnerStalePathUnsafe(
                f"refusing to start: {run_dir} could not be opened as a "
                f"plain directory ({exc}) -- it may be a symlink or "
                f"another non-directory object; remove it by hand if you "
                f"are certain it is safe to discard"
            ) from exc

        try:
            if not created_fresh:
                owner_uid = os.fstat(fd).st_uid
                if owner_uid != os.getuid():
                    raise OwnerStalePathUnsafe(
                        f"refusing to start: {run_dir} exists but is "
                        f"owned by uid {owner_uid}, not this process's "
                        f"uid {os.getuid()} -- refusing to harden a "
                        f"directory this process doesn't own"
                    )
            os.fchmod(fd, RUN_DIR_MODE)
            proc = subprocess.run(
                ["setfacl", "-b", f"/proc/self/fd/{fd}"],
                capture_output=True,
                text=True,
                timeout=30,
                pass_fds=[fd],
            )
            if proc.returncode != 0:
                raise OwnerPermissionUnsupported(
                    f"refusing to start: `setfacl -b` on {run_dir} (via "
                    f"its open fd, never by re-resolving the path) "
                    f"failed (exit {proc.returncode}): {proc.stderr.strip()}"
                )
        finally:
            os.close(fd)

    def _bind_socket_or_raise(self) -> None:
        """bind() the listening socket, translating the double-launch
        race (#3194, #3152 item 3) into the same handled OwnerAlreadyRunning
        path a pre-check refusal would take, instead of letting a raw
        OSError propagate. The pre-bind liveness check in
        `_check_stale_socket_or_raise()` can pass and then lose to a
        concurrent process that binds first -- measured directly (issue
        #3194): on this fleet's Python/Linux, binding an AF_UNIX socket to
        a path another socket already bound raises OSError with
        errno == errno.EADDRINUSE, not a different exception type.

        Any OTHER errno re-raises unchanged -- this must not swallow a
        genuinely different bind failure (e.g. a permissions error, or an
        out-of-space error)."""
        assert self._server is not None
        try:
            self._server.bind(str(self.socket_path))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise OwnerAlreadyRunning(
                    f"lost the bind() race for {self.socket_path} to a "
                    f"concurrent owner -- refusing to start a second "
                    f"owner for this seat; the winner is unaffected"
                ) from exc
            raise

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
        # #3152 item 1, folded into this build for BOTH connections:
        # close under the SAME lock _handle_snapshot() holds during
        # .backup() -- acquiring it here BLOCKS until any in-flight
        # snapshot's critical section has finished, so a .stop() (signal-
        # triggered or direct) can never close a connection out from
        # under a running backup(); it waits for it, then closes. No
        # in-flight _handle_snapshot() call can race this close.
        with self._owner_lock:
            if self._owner_conn is not None:
                try:
                    self._owner_conn.close()
                except sqlite3.Error:
                    pass
                self._owner_conn = None
            if self._client_read_conn is not None:
                try:
                    self._client_read_conn.close()
                except sqlite3.Error:
                    pass
                self._client_read_conn = None
        self._client_read_gate = None

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

    def _check_peer_credentials(self, conn: socket.socket) -> dict | None:
        """Peer-credential check (#3194, design doc §2.2): defence in
        depth, on EVERY accept()ed connection, independent of whether the
        peer ever sends well-formed JSON. Returns a §2.3 rejection wire
        dict if the peer must be refused, or None to proceed.

        Parses the SO_PEERCRED buffer with `struct.unpack_from` --
        NEVER by slicing. This is the load-bearing requirement: a slice
        form (`int.from_bytes(raw[4:8], "little")`) silently decodes a
        too-short buffer to 0 (read as uid 0) with no error, whereas
        `unpack_from` RAISES on a short buffer (contract test 2's
        discrimination mutation targets exactly this parse form, per
        design doc §2.2/§5 test 2)."""
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEERCRED_STRUCT_SIZE)
        _peer_pid, peer_uid, _peer_gid = struct.unpack_from(PEERCRED_STRUCT_FORMAT, raw)
        own_uid = os.getuid()

        if peer_uid != own_uid:
            return {
                "status": "rejected",
                "reason": "peer_identity",
                "detail": f"connecting uid {peer_uid} is not this owner's uid {own_uid}",
                "remedy": "connect to the socket under your own $ENGRAM_HOME",
            }
        # Defence in depth (design doc §2.2): a peer uid of 0 is rejected
        # unless the owner itself runs as root. Given the mismatch rule
        # above, this clause changes NO outcome in any owner configuration
        # today -- a non-root owner already rejected uid 0 as a mismatch
        # (the branch above already returned), and a root owner accepts
        # uid 0 under both rules. Kept as defence-in-depth against a
        # future weakening of the mismatch rule; deliberately NOT claimed
        # to discriminate -- design doc §5 test 3 is explicitly "a
        # property check, not a discriminating test", and no mutation is
        # invented for it here.
        if peer_uid == 0 and own_uid != 0:
            return {
                "status": "rejected",
                "reason": "peer_identity",
                "detail": f"connecting uid {peer_uid} is not this owner's uid {own_uid}",
                "remedy": "connect to the socket under your own $ENGRAM_HOME",
            }
        return None

    def _handle_connection(self, conn: socket.socket) -> None:
        # Contract item 8: end-to-end deadline measured from ACCEPT (i.e.
        # from entry to this handler). `accept_time` is the anchor and is
        # NEVER recomputed relative to "now" -- both the base `deadline`
        # below and any method-specific extension below are always
        # `accept_time + <some budget>`. _read_request_line() recomputes
        # the remaining budget and re-arms conn.settimeout() with it before
        # EVERY blocking recv -- see the module docstring's "Per-request
        # deadline" section. `deadline` itself may be widened once the
        # method is known (see below, round 2 / #3194) -- frame acquisition
        # happens BEFORE the method is parsed, so it is always bound by the
        # base `self.request_deadline`.
        accept_time = time.monotonic()
        deadline = accept_time + self.request_deadline
        try:
            rejection = self._check_peer_credentials(conn)
            if rejection is not None:
                # Bounded by the SAME remaining deadline as every other
                # send in this method (reviewer-fairy catch on #3201: this
                # send previously had no timeout at all, unlike the
                # request's own later, deadline-bound sendall() below --
                # a peer that stops reading after triggering a peer-
                # identity rejection could otherwise hold this thread
                # indefinitely).
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    conn.settimeout(remaining)
                    try:
                        conn.sendall((json.dumps(rejection) + "\n").encode("utf-8"))
                    except OSError:
                        pass
                return
            line = self._read_request_line(conn, deadline)
            if line is None:
                return  # deadline exceeded, oversized, malformed framing, or peer closed -- drop silently
            try:
                request = json.loads(line.decode("utf-8").strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                return  # malformed request -- drop; a U1 client sees Unavailable, not a hang
            if not isinstance(request, dict):
                return
            # Round 2 (#3194): the deadline is method-aware, chosen once
            # the method is parsed, but still anchored to accept_time --
            # "the deadline is still measured from accept and never
            # resets" (Borges's ruling). snapshot() gets its own, larger,
            # named budget because 5s is not a realistic backup budget for
            # a real knowledge.db; "no budget for internal operations" is
            # not an allowed value. See the module docstring's
            # "Per-request deadline, round 2" section for the full
            # mechanism and its justification.
            method = request.get("method")
            if method == "snapshot":
                deadline = accept_time + self.snapshot_request_deadline
            response = self._dispatch(request, deadline)
            # Bound the response send by the SAME (possibly method-
            # extended) remaining deadline -- a client that stops reading
            # after sending its request shouldn't be able to hold this
            # thread past the request's own budget either. Not explicitly
            # one of the 10 numbered contract items (which name the READ
            # side), but a direct extension of the same "one deadline per
            # request, end to end, on both sides of the socket" principle
            # (docs/2652-engrammanager-design.md SS5.1) -- flagged in the
            # fairy handoff. This is ALSO the mechanism by which a
            # deadline-aborted snapshot's own {"status": "rejected", ...}
            # wire dict USED to never reach the wire (round 5: it was
            # composed at the request deadline by construction). Round 6
            # splits the budget instead -- _handle_snapshot() stops
            # execution SNAPSHOT_RESPONSE_RESERVE early, so its response is
            # composed with time still in hand and passes this gate. THIS
            # GATE IS DELIBERATELY UNCHANGED: it still drops a response
            # composed after the request deadline (a scheduler stall past
            # the reserve), which a snapshot() client reads as Unavailable
            # == indeterminate.
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

    def _dispatch(self, request: dict, deadline: float) -> dict:
        """Contract items 3, 4, 5, 7: version check on every request (not
        a separate handshake step, per U1's own design), then `ping` /
        `snapshot` / unknown-operation.

        `deadline` (round 2, #3194) is the SAME (possibly method-extended)
        deadline `_handle_connection()` computed -- threaded through here
        so `_handle_snapshot()` can bound its own execution by it, not just
        the framing around it. `ping` ignores it; it does no bounded work."""
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
            return self._handle_snapshot(params, deadline)

        return {
            "status": "rejected",
            "reason": "unknown_operation",
            "detail": f"no such operation: {method!r}",
            "remedy": (
                "check the method name against the owner's supported "
                "operations (this build: ping, snapshot)"
            ),
        }

    def _handle_snapshot(self, params: dict, deadline: float) -> dict:
        """Contract item 5: writes a quiesced, consistent copy of the
        store to a caller-named destination path, using SQLite's backup
        API (sqlite3.Connection.backup(), stdlib) FROM the owner-internal
        connection (`_owner_conn` -- docs/2652-security-slice-design.md
        SS3.1: snapshot() runs on the owner-internal path, never the
        client-read one). A client-facing service, not a manager
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

        `deadline` (round 2, #3194, Borges's ruling on issue #3194's
        comment thread): the caller's (possibly snapshot-extended)
        end-to-end REQUEST deadline, anchored to accept-time. This method
        derives its own EXECUTION deadline from it (round 6, below) and
        uses only that for execution. The backup
        itself is STEPPED (`pages=SNAPSHOT_BACKUP_STEP_PAGES`) with a
        `progress` callback that raises `_SnapshotDeadlineExceeded` once
        `time.monotonic()` passes the execution deadline -- `backup()` propagates that
        raise cleanly out (measured; see `_SnapshotDeadlineExceeded`'s own
        docstring), so the abort surfaces here as a normal Python
        exception, not a special sqlite3 error code. The
        `with self._owner_lock:` block's own `__exit__` is what releases
        the lock on abort (no second, hand-rolled release path to get
        wrong).

        STAGE-THEN-ATOMIC-REPLACE, never write straight to `dest_path`
        (#3194, Mira's third security finding on #3201's 18cd14b2, Borges's
        designer ruling on the same issue thread): the ORIGINAL round-2
        code backed up directly INTO `dest_path` and, on a deadline abort,
        unconditionally `unlink()`'d it -- Mira's exact-head reproduction
        (an existing sentinel `.db` at the destination) showed this
        DESTROYS a caller's pre-existing file, not just a partial output
        this call created. Fixed: the backup target is a private,
        `O_EXCL`-created temp file in the SAME directory as `dest_path`
        (`tempfile.mkstemp`, so this call is guaranteed to own it, and
        same-directory is what makes the final `os.replace()` atomic --
        `os.replace` across filesystems is NOT atomic). On SUCCESS, the
        temp file is `fsync`'d then `os.replace()`'d onto `dest_path` --
        one atomic rename, so a reader of `dest_path` (viz, health
        snapshots, the sleep backup, per Borges's ruling) never observes a
        torn/partial file, and the previous file at `dest_path` (if any)
        is atomically superseded in one step, not unlinked-then-recreated.
        On ANY failure (deadline abort, or a `sqlite3.Error` mid-backup),
        ONLY the temp file is removed; `dest_path` -- whether it existed
        before this call or not -- is NEVER touched. A `{"status":
        "rejected", "reason": "deadline_exceeded", ...}` wire dict is
        still returned on abort, and (round 6, below) it now reaches the
        wire: it is composed with `SNAPSHOT_RESPONSE_RESERVE` still in
        hand, so `_handle_connection()`'s post-dispatch send gate lets it
        through.

        BOUNDED FINALIZE WORKER, CAPACITY, AND COMMIT-STATE PROTOCOL (#3194
        round 5, Borges's REVISED and FINAL protocol -- his first ruling on
        Mira's fsync/deadline findings (round 4, on #3201's pushed head
        71de4d80) was superseded TWICE, in-thread, after Mira found a real
        gap in it: moving `fsync`/`os.replace` off `_owner_lock` bounds
        *other* clients, but gave no finite bound on *the request itself*
        -- the request thread and its temp file were held for as long as
        the disk took, and concurrent slow fsyncs could accumulate without
        limit. This is the corrected, final protocol, replacing round 4's
        store-lock-only fix above):

        The store lock covers only the backup into the temp file.
        Durability (`fsync`) and the atomic swap (`os.replace`) run on a
        bounded finalize worker: the request waits on it only until the
        deadline, and at most `SNAPSHOT_FINALIZE_SLOTS` finalizes may be
        outstanding, with a call that finds none free refused at once.
        `os.replace` is issued only while the call is neither abandoned nor
        past its deadline, and temp cleanup is owned by a single `finally`.
        Three residuals are outside any in-process owner's control and are
        stated as exceptions: a pre-deadline commit reservation whose
        rename may be both issued and completed after the deadline (the
        worker can be descheduled between reserving and the syscall;
        reported `commit: indeterminate`, never success; the destination
        is atomically old or new); a hung `fsync` or `os.replace` holds one
        slot and one temp until the kernel returns (bounded in count, not
        time); and process death mid-finalize orphans a temp file beside
        the destination.

        Mechanism, stated precisely: `self._finalize_slots` (a
        `threading.BoundedSemaphore(SNAPSHOT_FINALIZE_SLOTS)`, one per
        owner, shared across every snapshot() call) is acquired
        non-blocking BEFORE the backup begins -- see the capacity-gate
        check below, right after param validation. If no slot is free,
        this method returns `{"status": "rejected", "reason": "busy", ...}`
        immediately, before the store lock is ever taken or a temp file
        ever created (test (e), tests/test_engramd_owner.py). If a slot IS
        acquired, this thread does the backup-into-temp step exactly as
        before (unchanged: still under `_owner_lock`, still deadline-
        checked via the `progress` callback, still bounded to the
        execution deadline + one backup step) -- and releases the slot itself if that phase
        aborts or fails, since no worker exists yet to inherit it.

        Once the backup succeeds, a `_FinalizeOutcome` (a small,
        lock-guarded `pending -> committing -> committed`/`aborted` state
        machine -- see its own class docstring) is created for this call,
        and `_finalize_worker()` is spawned on its own `threading.Thread`
        to run `fsync` then `os.replace` -- the WORKER, not the request
        thread, now owns the finalize slot and releases it in its own
        `finally` when it exits, whatever the outcome. The REQUEST thread
        waits on `outcome.event` (a `threading.Event`, set exactly once by
        the worker on every exit path) with `timeout =
        execution_deadline - time.monotonic()` -- NOT a bare `.join()` on
        the worker thread, which would defeat the whole point: the response
        time is bounded by the deadline no matter how slow the disk is
        (test (d)).

        If the wait times out (the worker hasn't finished by the
        deadline), the request thread takes `outcome.lock` and, ONLY if
        the state is still `"pending"` (the worker hasn't reached its own
        commit-gate check yet), sets it to `"abandoned"` and returns
        `deadline_exceeded` at once -- the destination is GUARANTEED
        untouched, because the worker checks for `"abandoned"` under the
        same lock before it ever issues `os.replace` (test (f)). If, by
        the time the request thread takes the lock, the worker has already
        moved past `"pending"` into `"committing"` (a commit was RESERVED
        before the deadline; the rename may be issued and completed after
        it -- Borges's precise R1 wording), the response is
        `deadline_exceeded` with an ADDITIONAL top-level `"commit":
        "indeterminate"` key -- a deliberate design choice (see the fairy
        handoff's ambiguous-decision log) to make the two shapes
        distinguishable: "never touched" carries no `commit` key at all;
        "maybe touched" always does (test (g)).

        SPLIT DEADLINE AND CLIENT CONTRACT (#3194 round 6, Borges's ruling
        on the round-5 finding that the send gate dropped every
        timeout-driven response). The one end-to-end deadline covers
        frame acquisition, execution AND response delivery, so a response
        composed AT the request deadline has spent the whole budget on
        execution and left none for delivery. `execution_deadline =
        deadline - SNAPSHOT_RESPONSE_RESERVE` (a named module constant,
        read at call time; see its comment for the measurement). EVERY
        execution-side use takes the execution deadline, never the request
        deadline: (a) the `backup()` progress check, (b) the request
        thread's wait on the finalize worker, (c) the worker's
        abandon-or-commit gate. The request thread therefore composes its
        `deadline_exceeded` / `commit: "indeterminate"` response with the
        reserve still in hand. Response delivery gets the reserve; the send
        gate in `_handle_connection()` still compares against the REQUEST
        deadline and is unchanged -- it is the design's guard against
        delivering after the promise expired.

        Degenerate configuration: if `deadline - SNAPSHOT_RESPONSE_RESERVE`
        is already <= now when the call starts (a tiny test deadline, or a
        deadline smaller than the reserve), the call returns a bounded
        `rejected` / `deadline_exceeded` at once -- no slot taken, no
        backup started, no temp file, `dest` untouched -- and never raises.

        CLIENT CONTRACT for `snapshot()`: an `Unavailable` (a dropped
        connection) must be read as INDETERMINATE. The reserve makes a
        missed delivery rare, not impossible (scheduler stalls). The
        destination is atomically either the old file or the complete new
        snapshot (R1's guarantee), so R1's client-visible form is
        `commit: "indeterminate"` OR `Unavailable`. Both mean "go look at
        dest"; neither ever means success.
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

        # #3194 round 6 (Borges): the request deadline is SPLIT. Everything
        # below that is EXECUTION-side (the backup progress check, the
        # request thread's wait on the finalize worker, the worker's
        # abandon-or-commit gate) takes `execution_deadline`, never
        # `deadline`; the last `SNAPSHOT_RESPONSE_RESERVE` seconds before
        # the request deadline are left for composing and sending the
        # response. The constant is read HERE, at call time (not captured
        # at import or in __init__), so a test can monkeypatch it.
        execution_deadline = deadline - SNAPSHOT_RESPONSE_RESERVE
        if time.monotonic() >= execution_deadline:
            # Degenerate budget: the execution deadline has already passed
            # (a tiny test deadline, or a request deadline smaller than the
            # reserve). Return a bounded rejection now -- no slot taken, no
            # backup started, no temp file, `dest` untouched.
            return {
                "status": "rejected",
                "reason": "deadline_exceeded",
                "detail": (
                    "snapshot() had no execution time left inside its request "
                    "deadline (after reserving time to deliver the response)"
                ),
                "remedy": (
                    "retry snapshot() with a larger budget; if the configured "
                    "snapshot request deadline is smaller than "
                    "SNAPSHOT_RESPONSE_RESERVE, raise it"
                ),
            }

        # #3194 round 5, Borges's revised ruling, point 3: take a finalize
        # slot non-blocking BEFORE any work begins -- if none is free,
        # refuse `busy` at once and do no work at all (no lock, no backup,
        # no temp file). This thread owns the slot until it either hands
        # it off to a spawned finalize worker (on backup success, below)
        # or releases it itself (on any abort/failure before that handoff
        # -- see the `finally` at the end of this method).
        if not self._finalize_slots.acquire(blocking=False):
            return {
                "status": "rejected",
                "reason": "busy",
                "detail": (
                    f"no finalize slot free (SNAPSHOT_FINALIZE_SLOTS="
                    f"{SNAPSHOT_FINALIZE_SLOTS}); at capacity"
                ),
                "remedy": "retry snapshot() shortly, once an in-flight finalize completes",
            }
        slot_owned_here = True
        try:
            # #3194, Borges's ruling on Mira's third finding: stage into a
            # private O_EXCL temp file in the SAME directory as `dest`
            # (same-directory is what makes the later os.replace() atomic
            # -- a cross-filesystem os.replace() is NOT atomic), never
            # write straight to `dest`. `tempfile.mkstemp` creates it
            # O_EXCL under the hood, so this call is guaranteed to own it
            # regardless of what may or may not already sit at `dest`.
            temp_fd, temp_path_str = tempfile.mkstemp(
                dir=str(parent), prefix=f".{dest.name}.", suffix=".snapshot-tmp"
            )
            temp_path = Path(temp_path_str)
            os.close(temp_fd)  # sqlite3.connect() below opens its own handle to this path

            def _cleanup_temp() -> None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

            with self._owner_lock:
                try:
                    dest_conn = sqlite3.connect(str(temp_path))
                    aborted = False
                    try:
                        assert self._owner_conn is not None

                        def _abort_if_past_deadline(status, remaining_pages, total_pages) -> None:
                            # `sqlite3.Connection.backup()`'s `progress`
                            # callback shape: (status, remaining, pages) --
                            # `status` and the page counts are unused here;
                            # this is purely a deadline check, run between
                            # steps (see SNAPSHOT_BACKUP_STEP_PAGES's own
                            # comment for the step size).
                            if time.monotonic() >= execution_deadline:
                                raise _SnapshotDeadlineExceeded(
                                    "snapshot() exceeded its execution deadline mid-backup"
                                )

                        try:
                            self._owner_conn.backup(
                                dest_conn,
                                pages=SNAPSHOT_BACKUP_STEP_PAGES,
                                progress=_abort_if_past_deadline,
                                sleep=0,
                            )
                        except _SnapshotDeadlineExceeded:
                            aborted = True
                    finally:
                        dest_conn.close()
                    if aborted:
                        # Only the TEMP file is removed -- `dest` (whether
                        # it existed before this call or not) is never
                        # touched (Borges's #3194 amended-contract ruling,
                        # point 3: "leaves no destination file" applies to
                        # the temp staging file this call created, never
                        # to the caller's own path). This abort happens
                        # BEFORE any finalize worker is spawned, so this
                        # thread still owns the slot -- the outer
                        # `finally` below releases it.
                        _cleanup_temp()
                        return {
                            "status": "rejected",
                            "reason": "deadline_exceeded",
                            "detail": "snapshot() did not complete within its request deadline",
                            "remedy": (
                                "retry snapshot(); if it persists, the store may be too "
                                "large for the current snapshot request deadline"
                            ),
                        }
                except sqlite3.Error as exc:
                    _cleanup_temp()
                    return {
                        "status": "rejected",
                        "reason": "snapshot_failed",
                        "detail": str(exc),
                        "remedy": (
                            "retry snapshot(); if it persists, check disk space "
                            "and destination permissions"
                        ),
                    }

            # Success: `_owner_lock` is released above -- the temp file is
            # a complete, self-contained snapshot the moment backup()
            # returns, and never touches `_owner_conn` again, so nothing
            # past this point needs `_owner_lock` (#3194 round 4's own
            # finding, still true under round 5). Durability (`fsync`) and
            # the atomic swap (`os.replace`) now run on a bounded finalize
            # WORKER thread rather than inline here (#3194 round 5,
            # Borges's revised ruling -- see this method's own docstring
            # for the full protocol and why round 4's inline version left
            # the request itself unbounded). This thread hands the
            # finalize slot OFF to the worker here (`slot_owned_here =
            # False`) -- the worker releases it in its own `finally`.
            outcome = _FinalizeOutcome()
            slot_owned_here = False
            worker = threading.Thread(
                target=self._finalize_worker,
                args=(temp_path, dest, execution_deadline, outcome, self._finalize_slots),
                daemon=True,
            )
            worker.start()

            remaining = execution_deadline - time.monotonic()
            completed = outcome.event.wait(timeout=max(remaining, 0.0))

            with outcome.lock:
                if completed:
                    final_state = outcome.state
                    final_reason = outcome.reason
                elif outcome.state == "pending":
                    # The request's own timeout fired before the worker
                    # ever reached its commit-gate check -- abandon the
                    # call now, under the SAME lock the worker checks
                    # before issuing os.replace(), so the destination is
                    # guaranteed untouched (test (f)).
                    outcome.state = "abandoned"
                    final_state = "abandoned"
                    final_reason = None
                else:
                    # The worker had already moved past "pending" (into
                    # "committing", or -- a rare race -- all the way to
                    # "committed"/"aborted") by the time we took the lock,
                    # even though our own wait() timed out first.
                    final_state = outcome.state
                    final_reason = outcome.reason

            if final_state == "committed":
                return {"status": "ok", "result": {"dest_path": str(dest)}}
            if final_state == "committing":
                # R1 (module docstring / this method's own docstring): a
                # pre-deadline commit RESERVATION whose rename may land
                # after the deadline -- reported indeterminate, never
                # success; `commit` is a NEW top-level key, present ONLY
                # in this shape, so it is distinguishable from the
                # "never touched" (`"abandoned"`) shape below, which
                # carries no `commit` key at all (test (g) vs test (f)).
                return {
                    "status": "rejected",
                    "reason": "deadline_exceeded",
                    "commit": "indeterminate",
                    "detail": (
                        "snapshot() exceeded its request deadline after a commit was "
                        "reserved before the deadline; the rename may be issued and "
                        "completed after it. The destination is either the prior "
                        "file or the complete new snapshot, never a partial write"
                    ),
                    "remedy": "check dest_path directly to see which; retry snapshot() if needed",
                }
            if final_state == "abandoned":
                return {
                    "status": "rejected",
                    "reason": "deadline_exceeded",
                    "detail": "snapshot() did not complete within its request deadline",
                    "remedy": (
                        "retry snapshot(); if it persists, the store may be too "
                        "large for the current snapshot request deadline"
                    ),
                }
            # final_state == "aborted" -- the worker's own fsync/replace
            # failed (an OSError), or it found itself already abandoned/
            # past-deadline when it reached its commit-gate check.
            return {
                "status": "rejected",
                "reason": final_reason or "snapshot_failed",
                "detail": "snapshot() failed during finalize (fsync/replace)",
                "remedy": (
                    "retry snapshot(); if it persists, check disk space "
                    "and destination permissions"
                ),
            }
        finally:
            if slot_owned_here:
                self._finalize_slots.release()

    def _finalize_worker(
        self,
        temp_path: Path,
        dest: Path,
        execution_deadline: float,
        outcome: "_FinalizeOutcome",
        slots: threading.BoundedSemaphore,
    ) -> None:
        """The bounded finalize worker (#3194 round 5): runs `fsync` then
        `os.replace` OUTSIDE `_owner_lock` and outside the request thread
        entirely, on its own daemon thread, spawned by `_handle_snapshot()`
        once the backup-into-temp step has succeeded. Owns the finalize
        slot from the moment it is spawned (the caller already acquired it
        non-blocking before the backup began -- see `_handle_snapshot()`'s
        own docstring) and releases it here, in the outer `finally`, on
        EVERY exit path -- "the worker releases the slot when it exits,
        whenever the kernel lets it" (Borges's ruling, point 3).

        Commit-state protocol (Borges's ruling, point 4): `outcome.state`
        moves `"pending" -> "committing" -> "committed"` on success, or
        `"pending"/"abandoned" -> "aborted"` on any failure or on finding
        itself abandoned/past-deadline at the commit-gate check. Every
        read AND write of `outcome.state`/`outcome.reason` happens under
        `outcome.lock` -- the request thread (in `_handle_snapshot()`)
        touches the SAME object under the SAME lock, and the whole
        protocol depends on the two never observing or mutating it without
        holding that lock.

        Cleanup stays in ONE `finally`, keyed on `committed` (Borges's
        ruling, point 4's last clause) -- not an `except sqlite3.Error`/
        `except OSError` list at each failure site, which would miss
        whichever failure mode wasn't enumerated (point 5: "every error
        path cleans the temp file"). `outcome.event.set()` and the slot
        release ALSO live in this one `finally`, so they fire exactly
        once, on every exit path, regardless of which branch returned.
        """
        committed = False
        try:
            try:
                fsync_fd = os.open(str(temp_path), os.O_RDONLY)
                try:
                    os.fsync(fsync_fd)
                finally:
                    os.close(fsync_fd)
            except OSError:
                with outcome.lock:
                    outcome.state = "aborted"
                    outcome.reason = "snapshot_failed"
                return

            # The commit-point gate (Borges's ruling, point 4): checked
            # immediately after fsync, under `outcome.lock` -- the SAME
            # lock the request thread takes on its own timeout, so the
            # two can never disagree about whether the call is still
            # live. Checking the LIVE `abandoned` state here (not a
            # deadline snapshot captured at worker-start) is what
            # guarantees test (f)'s "dest never changes" property: if the
            # request thread got here first and set "abandoned", this
            # check sees it and never issues os.replace(), no matter what
            # this worker's own clock reads.
            with outcome.lock:
                if outcome.state == "abandoned" or time.monotonic() >= execution_deadline:
                    outcome.state = "aborted"
                    outcome.reason = "deadline_exceeded"
                    return
                outcome.state = "committing"

            # os.replace() runs OUTSIDE the lock -- Borges's ruling, R1:
            # no in-process primitive can check the time and rename
            # atomically, so a rename issued here may land after the
            # deadline; that residual is accepted, not closed, and is why
            # the request thread reports "indeterminate" rather than
            # "aborted" if it observes "committing" (test (g)).
            try:
                os.replace(str(temp_path), str(dest))
            except OSError:
                with outcome.lock:
                    outcome.state = "aborted"
                    outcome.reason = "snapshot_failed"
                return

            with outcome.lock:
                outcome.state = "committed"
                committed = True
        finally:
            if not committed:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
            outcome.event.set()
            slots.release()


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
