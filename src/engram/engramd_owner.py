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
  (Historical U3 scope. #3822 / U11 added the owner's first write, the typed
  `use_reward` op: see "The owner's first write" below `_use_reward_param_refusal`
  and docs/2652-migration-ledger.md section 6. Still no generic write verb.)
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

A pidfile path that exists and is not a regular file (FIFO, symlink,
directory) is refused the same way (#3768): `OwnerPidfileNotRegular`, and
`main()` returns `EXIT_PATH_NOT_REGULAR` (4), distinct from 1 and from
`EXIT_DISPLACED` (3). A store path that is not a regular file is refused with
the same code (`OwnerStorePathNotRegular`): retrying cannot fix a planted path.

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

import copy
import errno
import hashlib
import itertools
import json
import logging
import math
import os
import platform
import queue
import re
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
from collections import OrderedDict
from pathlib import Path

import engram_pidfile
import engram_moment_cursor as emc
import engram_restamp as er
import engram_walguard
import engramd_client as ec
import engramd_read_registry as rr
from engram_regular_io import NotRegularFile, describe_file_kind, read_regular_text, write_pidfile

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# The pidfile name and the argv marker live in the client (#3707: the client
# reads the pidfile on a Darwin refused connect() and never imports the owner);
# aliased here so existing owner code and tests keep their names.
DEFAULT_PID_NAME = ec.DEFAULT_PID_NAME
_PIDFILE_PROGRAM_MARKER = ec.PIDFILE_PROGRAM_MARKER

# #3240 item 2: exit status of an owner that shut down on a DISPLACED
# substrate (its -wal/-shm fds no longer match the files on disk). Distinct
# from 0 (healthy) and 1 (OwnerAlreadyRunning) so a supervisor can tell a
# displaced exit from a clean one. main() ends the process with os._exit(
# EXIT_DISPLACED) on this path -- see main() for why finalization is skipped.
EXIT_DISPLACED = 3

# #3768, #3753: exit status of an owner that REFUSED TO START because its pidfile path
# or its store path exists and is not a regular file (FIFO, symlink, directory,
# socket, device, ...). Retrying cannot fix a planted path, so the unit does not
# restart it. For the pidfile it is its own code, not the surface daemon's
# run-without-pidfile fallback: the owner's pidfile carries liveness, so an owner
# without one reads as down while it holds the socket. main() returns it (nothing
# was bound or left behind).
EXIT_PATH_NOT_REGULAR = 4
# #3753: exit status of an owner that refused to start because the store is BEHIND the
# schema this code was written against (no knowledge.db, or PRAGMA user_version below
# engram_core.LATEST_SCHEMA_VERSION). Distinct from 0/1/3/4 so a supervisor and the log can
# tell it from "another owner holds the seat" (1), "displaced" (3) and "a pidfile or store
# path is not a regular file" (4). Non-zero, so Restart=on-failure / launchd retry it: the usual cause is the
# first migration not having run yet. It is 5, NOT 2: CPython itself exits 2 when it cannot
# open the script it was asked to run (`python /nonexistent/x.py`, i.e. a deleted
# ExecStart), so a unit's exit table would read that as "store behind schema". A refusal
# code must not share a value with the interpreter's own failure codes (1 and 2); 1 stays
# only as OwnerAlreadyRunning's historic code (main() returns the literal).
EXIT_STORE_BEHIND_SCHEMA = 5

# #3240 item 2: methods that never touch the store and so are exempt from the
# displacement self-check. Every OTHER registered method is guarded by
# default (a new store method that forgets the guard fails safe).
NO_STORE_METHODS = frozenset({"ping"})

# #3240 item 2: connections of a displaced owner are parked here for the life
# of the process and never closed or cleared. Merely dropping the references
# does NOT avoid a clean close in CPython: an unreferenced sqlite3.Connection
# is closed by GC, and interpreter finalization closes the rest -- and the
# last close of a stale holder checkpoints an orphan WAL over newer data
# (see engram_walguard's docs for the harm). A displaced process is meant to
# EXIT (main() -> os._exit(EXIT_DISPLACED)); calling start() again in the same
# process after a displaced stop is unsupported -- the parked fds make a fresh
# store at the same path read as displaced. Restart the process instead.
_DISPLACED_CONNECTIONS_KEEPALIVE: list = []
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
#
# #3707 round 3: the value now lives in engramd_client (the client checks it
# before connecting, so it is the single source); this is an alias.
MAX_REQUEST_BYTES = ec.MAX_REQUEST_BYTES

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

# The part of `read`'s request deadline held back for DELIVERING its response
# (U4d, #3197) -- the same reason as SNAPSHOT_RESPONSE_RESERVE above: the send
# gate in _handle_connection() drops a response composed after the REQUEST
# deadline, so a read that overruns must stop SOONER or its own
# `deadline_exceeded` rejection never reaches the client (it would read as
# Unavailable). Same value, deliberately NOT re-measured for `read`: the
# rejection it must deliver is a ~200-byte wire dict, smaller than the snapshot
# timeout shapes the 0.25 s was measured against (80x margin there), and the
# abort point is one progress-handler tick (PROGRESS_HANDLER_INSTRUCTION_COUNT
# VM instructions, microseconds), not a backup step. Read at CALL time so a
# test can monkeypatch it.
READ_RESPONSE_RESERVE = 0.25  # seconds

# #3665: the typed `df` method (design seat ruling, crossing 9958). DF_TERMS_MAX
# bounds one request: 1000 is the IDF hook's real batch size
# (engram_idf.IDF_BATCH_TERMS on U6b, where a cell pins the equality), so a
# full hook batch is exactly one request. DF_TERM_MAX_BYTES caps one term in
# UTF-8 BYTES. Measured 2026-10-12: the longest term in a real vocab
# (~/.engram/knowledge.db under $ENGRAM_HOME, nodes_fts_vocab, 15536 terms) is 64 bytes (a sha256
# hex token); engram_idf.tokenize (`[a-z]{2,}[a-z0-9_]*`) has NO upper bound on
# token length, so the cap is a chosen bound: next power of two >= 2x the
# measured max is 128, floored at 256.
# DF_SQL is the ONE fixed statement `df` executes -- byte-identical to
# engram_idf.IDF_BATCH_SQL, with the terms bound as a single JSON-list
# parameter; no client text ever becomes SQL. DF_VOCAB_DDL_SQL reads the vocab
# table's DDL so only the `row` fts5vocab variant is served. Both run on
# `_client_read_conn` under its authorizer floor.
DF_TERMS_MAX = 1000
DF_TERM_MAX_BYTES = 256
DF_SQL = "SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))"
DF_VOCAB_DDL_SQL = "SELECT sql FROM sqlite_master WHERE name = 'nodes_fts_vocab'"
# fts5vocab(<table>, <type>) with the identifier / type optionally quoted any of
# the ways SQLite accepts; the type is captured and compared case-insensitively.
_DF_VOCAB_VARIANT_RE = re.compile(
    r"""USING\s+fts5vocab\s*\(\s*(?:"[^"]*"|'[^']*'|\[[^\]]*\]|`[^`]*`|[\w$]+)\s*,\s*["'\[`]?\s*(\w+)\s*["'\]`]?\s*\)""",
    re.IGNORECASE,
)
# SQL comments are removed from the stored DDL before the pattern above runs, so
# a commented-out `USING fts5vocab(..., row)` cannot be read as the module clause.
# A comment marker inside a quoted identifier would truncate the DDL, which can
# only fail CLOSED (no match -> unsupported_vocab_variant), never open.
_DF_SQL_COMMENT_RE = re.compile(r"/\*.*?(?:\*/|\Z)|--[^\n]*", re.DOTALL)
# The ONLY statements `_ClientReadGate.execute_typed` will prepare (#3665 F2).
_TYPED_STATEMENTS = frozenset({DF_SQL, DF_VOCAB_DDL_SQL})

# Owner-side bound on a client `read` RESULT, as serialised on the wire (U4d
# round 2, S3). The client refuses any response over ec.MAX_RESPONSE_BYTES
# (10 MiB) and reads that as Unavailable -- which tells the agent to restart a
# healthy owner for what is a deterministic, never-going-to-succeed oversize.
# So the owner refuses first, as `Rejected(result_too_large)`: 8 MiB, a fixed
# 2 MiB below the client's cap, far more than the envelope (`{"status": "ok",
# "result": ...}` + request_id + framing newline, a few hundred bytes) can add.
# It is measured row by row on the exact JSON the send path emits (json.dumps
# defaults), so the check also stops the work: at most this bound plus one row
# is ever serialised, which keeps serialisation inside the request deadline
# for any result the owner will actually send.
MAX_READ_RESPONSE_BYTES = 8 * 1024 * 1024

# #3740 Cut A: the typed `chain` method (design seat ruling, issue comment
# 6133024506). It runs `engram_core.generalization_chain` ITSELF on the
# client-read connection (no second implementation of the walk) and returns its
# dict. CHAIN_NODE_ID_MAX_BYTES caps the start id in UTF-8 BYTES: real node ids
# are a short prefix and a counter (a dozen bytes or so), and 256 is the same chosen
# ceiling as DF_TERM_MAX_BYTES. CHAIN_MAX_DEPTH_CAP is the CLAMP ceiling for
# `max_depth` and equals the default of generalization_chain's signature (a cell
# pins the equality through `inspect`): the hook never asks for more, and a
# larger request is served at the cap, not refused. CHAIN_RESULT_MAX_BYTES is the
# bound `read` applies to its serialised reply, reused rather than invented
# twice: `claim` is unbounded by design, so an oversized chain is refused by
# NAME (`chain_result_too_large`), never truncated. Read at CALL time, so a test
# can monkeypatch it. The cap is enforced AFTER the reply is built: peak memory for one
# request is bounded by the store's size (the walk reads `claim` unbounded), not by this cap.
CHAIN_NODE_ID_MAX_BYTES = 256
CHAIN_MAX_DEPTH_CAP = 4
CHAIN_RESULT_MAX_BYTES = MAX_READ_RESPONSE_BYTES

# #3741: the typed `confidence_by_type` method. At most this many modes per request (the
# four window modes of engram_stats); a longer list is REFUSED by name, never clamped
# (docs/2652-security-slice-design.md section 3.6, typed-op input bounds).
CBT_MAX_MODES = 4
# The mode names (equal to engram_stats.STATS_MODES; a cell pins the equality). Held here so
# validation, which runs before any runtime import, needs no import.
CBT_MODES = ("all", "1-turn", "7-turn", "30-turn")

# How often the LISTENING socket's accept() wakes up to check the shutdown
# flag. Short enough that .stop() returns quickly in tests (this process's
# accept loop has nothing else to do between wakeups, unlike
# engram-surface-daemon.py's heavier 5.0s, which also runs an idle watchdog
# and an indexer tick this module has neither of).
ACCEPT_POLL_INTERVAL = 0.2  # seconds

# Connection handler threads (#3678, U7). Starting a fresh thread for every accepted
# connection cost ~0.066 ms of each owner round trip (the U7 stage profile). A finished
# handler now waits for the next connection instead of exiting; the accept loop hands a
# connection to an IDLE handler when one exists and starts a new one only when none is
# idle. Capacity is never below spawn-per-connection: a connection is never queued
# behind a busy handler, so clients that hold their connections (until the request
# deadline drops them) cannot starve a fresh client of a handler. Idle handlers beyond
# IDLE_WORKER_CAP exit after IDLE_WORKER_TIMEOUT seconds idle, so a burst leaves at most
# the cap behind; stop() releases every idle handler.
WORKER_THREAD_PREFIX = "engramd-worker"
IDLE_WORKER_CAP = 4
IDLE_WORKER_TIMEOUT = 60.0  # seconds

# Peer-credential refusals (#3845). After the typed rejection the owner does
# shutdown(SHUT_WR) and DRAINS the refused peer's request (never parsing it) before
# closing: closing with unread bytes in the receive queue makes a client that reads to
# EOF get ConnectionResetError after the (delivered) rejection. The drain is bounded by
# min(REFUSAL_DRAIN_BUDGET, the remaining request deadline) and by max_request_bytes.
# A refused peer that sends nothing is therefore held up to this budget instead of
# being closed at once. Each refusal logs one WARNING, rate-limited per peer uid with a
# lazy window (no timer): PEER_REFUSAL_LOG_WINDOW seconds, at most
# PEER_REFUSAL_LOG_MAX_UIDS uids tracked (oldest evicted). At most MAX_REFUSAL_DRAINS
# drains run at once, GLOBAL across peers, so a flood of silent foreign connections
# cannot cost one held handler thread each. A refusal that finds no free slot sends the
# rejection and closes at once -- no shutdown, no drain, no hold -- so only an
# overflowing read-to-EOF send-first client loses the clean EOF (it gets
# ConnectionResetError after the delivered rejection). The log line is written either way.
REFUSAL_DRAIN_BUDGET = 0.5  # seconds
MAX_REFUSAL_DRAINS = 8
PEER_REFUSAL_LOG_WINDOW = 60  # seconds
PEER_REFUSAL_LOG_MAX_UIDS = 64

# Accept backlog (#3680). listen(5) let a burst of ~6+ simultaneous hook processes
# overflow the kernel's accept queue: the client's connect() failed at once with EAGAIN
# although the owner was healthy. The kernel caps this at net.core.somaxconn (Linux) /
# the platform limit, so SOMAXCONN is "as large as the platform allows", not a number
# picked by hand. It is NOT the concurrency control (there is none, by design: see
# above); it only absorbs a burst of clients starting together.
OWNER_LISTEN_BACKLOG = socket.SOMAXCONN

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

# ---------------------------------------------------------------------------
# Darwin arm (#3666, design doc §2.1, §2.2, §2.5). Everything below is used
# only when `_owner_arm() == "darwin"` (#3708: `platform.system()` is the one
# discriminator at every site); the Linux arm above is unchanged.
# ---------------------------------------------------------------------------

# Darwin xucred{cr_version, cr_uid, cr_ngroups, cr_groups[16]} -- LOCAL_PEERCRED's
# wire shape. NATIVE alignment ("@") and a literal 76 are both load-bearing:
# calcsize("@IIh16I") is 76, calcsize("=IIh16I") is 74, and a length pinned to
# calcsize() would agree with the wrong ("=") format (design doc §2.2).
DARWIN_PEERCRED_STRUCT_FORMAT = "@IIh16I"
DARWIN_PEERCRED_STRUCT_SIZE = 76
# cr_version of the xucred layout this parser understands (XUCRED_VERSION).
DARWIN_XUCRED_VERSION = 0
# getsockopt() level for AF_LOCAL options. Python exports neither SOL_LOCAL nor
# LOCAL_PEERPID, so this is a literal (measured: design doc P2). It is NOT
# SOL_SOCKET: LOCAL_PEERCRED at SOL_SOCKET succeeds and returns 4 zero bytes.
SOL_LOCAL = 0

# The Darwin socket-path limit (103 usable bytes, measured) lives with the
# shared #3696 contract: engramd_client.DARWIN_SOCKET_PATH_MAX_BYTES.

# Prerequisites of the Darwin permission layer, as named module constants so a
# test can point one at a missing path and exercise the real existence check.
# `chmod` is called by ABSOLUTE path, never found through PATH: a gnubin-first
# PATH would resolve to GNU chmod (which may lack -N). `/dev/fd/<n>` is how the
# strip and the read-back address the already-open run/ fd.
DARWIN_CHMOD = "/bin/chmod"
DARWIN_DEV_FD = "/dev/fd"

# Folders macOS TCC guards by default for an unprivileged process, relative to
# the user's home. THIS IS A DOCUMENTED HEURISTIC PREFIX LIST, NOT A TCC QUERY:
# asking TCC (or probing the folder) would itself raise a consent dialog nobody
# can answer on a launchd-run owner. The list is the three folders; it does not
# follow symlinks (resolving one would touch the protected folder) and is not
# claimed complete.
DARWIN_TCC_PROTECTED_HOME_FOLDERS = ("Documents", "Desktop", "Downloads")


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
    the deployment would believe it has one. This build implements the
    Linux arm (#3194) and the Darwin arm (#3666: LOCAL_PEERCRED, `/bin/chmod
    -N` on the run/ fd). A seat on any other platform, or one missing an
    arm's prerequisite (Linux: `setfacl` or `getfacl` -- #3698: the strip is
    read back -- or SO_PEERCRED; Darwin: LOCAL_PEERCRED, `/bin/chmod`,
    `/dev/fd`), refuses to start via this exception rather than starting
    with a silently-unenforced permission layer. So does a strip command
    that exits non-zero (both arms) or, on Darwin, does not finish within
    its timeout; what the READ-BACK finds is OwnerAclNotStripped."""


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


class OwnerPidfileNotRegular(RuntimeError):
    """Raised by EngramdOwner.start() when the pidfile path
    (<ENGRAM_HOME>/engramd.pid) exists and is not a regular file (#3768): a
    FIFO (a write-open would wait for a reader forever), a symlink (a write
    would follow it and truncate its target), a directory, or anything else.
    Raised before the socket is bound when the path is already wrong, and
    after cleaning up the socket and store connections when it turns wrong
    between that check and the write. A distinct exception, with its own exit
    code (EXIT_PATH_NOT_REGULAR), because the owner cannot fall back to
    running without a pidfile: the pidfile carries its liveness, and an owner
    without one reads as down while it holds the socket."""


class OwnerSocketPathTooLong(RuntimeError):
    """Raised by EngramdOwner.start() when the socket path
    (<ENGRAM_HOME>/run/engramd.sock) is longer in BYTES than an AF_UNIX
    address allows (#3696; the Darwin limit since #3666 -- both are
    `engramd_client.SOCKET_PATH_MAX_BYTES`). Raised before any filesystem
    mutation, so neither engram_home nor run/ is created. The message is
    `engramd_client.socket_path_too_long_reason()`'s text, shared with the
    client's Unavailable detail."""


class OwnerHomeTccProtected(RuntimeError):
    """Raised by EngramdOwner.start() on Darwin when $ENGRAM_HOME sits under a
    folder macOS TCC guards by default (#3666, design doc §2.5). A launchd-run
    owner there would block on a consent dialog nobody can answer, so it
    refuses instead. A heuristic prefix list, not a TCC query -- see
    DARWIN_TCC_PROTECTED_HOME_FOLDERS."""


class OwnerPeerCredentialsMalformed(RuntimeError):
    """Raised by the Darwin peer check when the kernel's LOCAL_PEERCRED answer is
    not the xucred this parser understands (length other than exactly 76, or
    cr_version other than 0). Fail closed: the worker drops the connection and
    the client reads it as Unavailable. A SHORT buffer does not reach this
    class: `struct.unpack_from` raises `struct.error` first, as on Linux."""


class OwnerAclNotStripped(RuntimeError):
    """Raised by EngramdOwner.start() when the run/ directory's ACL, read back
    through the same fd the `setfacl -b` strip used, still carries an entry
    beyond the base user::/group::/other:: triple (a named user or group, a
    mask::, any default: line) -- or when that read-back itself cannot be
    completed (getfacl failed, timed out or printed nothing parseable), because
    an unverifiable strip is the silent-gap shape (#3698, design doc §2.2).
    Darwin (#3666) raises it for the same three outcomes of its own read-back
    (`acl_get_fd_np` through the strip's fd): any entry left after `chmod -N`,
    a read-back that cannot complete, and a mode other than 0700.
    Raised before the stale-socket check and the bind, so no socket exists.
    `home/` and `run/` are left behind, owned by this uid, set to mode 0700
    (unless the Darwin mode check is what refused) and carrying whatever
    ACL the strip failed to remove; the next
    start() re-hardens `run/` on every start, so nothing needs cleaning up."""


class OwnerPythonTooOld(RuntimeError):
    """Raised by EngramdOwner.start() when the running Python's sqlite3
    lacks `Connection.setlimit` / `SQLITE_LIMIT_LENGTH` (#3349). The owner
    bounds client-read result size with them; without them it must refuse
    to start with this named reason, never run an unbounded read
    connection."""


class OwnerStoreBehindSchema(RuntimeError):
    """Raised by EngramdOwner.start() when the store is behind the schema this
    code expects (#3753): knowledge.db does not exist, cannot be read as a
    SQLite file, or its `PRAGMA user_version` is below
    `engram_core.LATEST_SCHEMA_VERSION`. (A store path that is not a regular
    file is a different refusal, OwnerStorePathNotRegular.) Started anyway, the owner would
    classify its read templates against the old schema, drop every one that
    needs a newer table, and keep them dropped for its whole lifetime. The
    message names the version found and the version required (or, when the
    version cannot be read, whether the store is locked/busy, corrupt, or the
    check's own import failed). Raised before anything is opened read-write
    and before run/ exists, so a refusal creates no knowledge.db and no run/.
    One qualification: a WAL-mode store (the real one) may gain
    `knowledge.db-wal` / `knowledge.db-shm` sidecar files, because SQLite
    creates them for any connection to a WAL database, `mode=ro` included
    (measured: -wal 0 bytes, -shm a 32 KiB wal-index, the database file
    unchanged)."""


class OwnerStorePathNotRegular(RuntimeError):
    """Raised when the store path (<ENGRAM_HOME>/knowledge.db) exists and is not
    a regular file (#3753): a FIFO (a read-only open would wait for a writer
    forever), a directory, a socket or a device. A symlink is followed, so one
    to a regular file is fine and one to a FIFO is refused. A separate class
    from OwnerStoreBehindSchema (not a subclass, so neither handler can shadow
    the other): main() maps it to EXIT_PATH_NOT_REGULAR, which the unit does not
    restart, because retrying cannot fix a planted path."""


def _file_kind(mode: int) -> str:
    for name, test in (("fifo", stat.S_ISFIFO), ("directory", stat.S_ISDIR),
                       ("socket", stat.S_ISSOCK), ("character device", stat.S_ISCHR),
                       ("block device", stat.S_ISBLK)):
        if test(mode):
            return name
    return "other non-regular file"


def _refuse_non_regular_store(store_path) -> None:
    """Raise OwnerStorePathNotRegular if `store_path` exists and is not a regular file (#3753,
    #3785). The ONE store-path predicate: `_connect_store_ro` (start) and
    `check_start_refusals` (`--preflight`) both call it.

    `os.stat` FOLLOWS symlinks and does not block on a FIFO: a symlink to a regular file is
    accepted, a symlink to a FIFO is refused. A missing or unstattable path passes (it is not
    a blocking object; the caller reports it). Pure: one stat, no mutation."""
    try:
        st = os.stat(Path(store_path).resolve())
    except OSError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise OwnerStorePathNotRegular(
            f"the store path {store_path} is not a regular file (it is a {_file_kind(st.st_mode)}); "
            f"refusing to open it: a read-only open of a FIFO would block forever"
        )


def _connect_store_ro(store_path) -> "sqlite3.Connection":
    """A `mode=ro` URI connection to the store: never creates the file, never
    writes. The one place the owner opens the store read-only (the start-time
    schema gate and the read-template classification both go through it).

    Refuses (OwnerStorePathNotRegular, via `_refuse_non_regular_store`) anything that is
    not a regular file BEFORE connecting: a read-only open (O_RDONLY) of a FIFO blocks
    until a writer appears, so the check must not be left to sqlite (every earlier store
    open here was read-write, which fails at once on a FIFO). A missing path is left to
    the caller's own 'no knowledge.db' branch (sqlite then fails fast on it)."""
    _refuse_non_regular_store(store_path)
    return sqlite3.connect(Path(store_path).resolve().as_uri() + "?mode=ro", uri=True)


def _require_store_schema_current(store_path) -> None:
    """Refuse (OwnerStoreBehindSchema) unless `store_path` exists and its
    `PRAGMA user_version` is >= `engram_core.LATEST_SCHEMA_VERSION` (#3753).

    Reads the version on a `mode=ro` URI connection, never a read-write one,
    so a missing file is not created as a side effect (a WAL-mode store may
    still gain -wal/-shm sidecars (no row data) from the probe; `immutable=1` is NOT
    used because a version bump still in the WAL would then read stale). `>=` passes: an older
    owner on a newer store is the upgrade-restart case. The required version
    is read from engram_core at CALL time (module attribute, not a copy), so
    the migration code and this gate share one constant."""
    # The one place start() pays for the engram_core import (_engram_core avoids it until
    # the first typed op). A missing dependency must refuse cleanly, not traceback: it is
    # reported as OwnerStoreBehindSchema, so main() gives it the same retryable exit code.
    # A distinct class was judged not worth a second exit code for an install fault the
    # supervisor's bounded retry handles the same way.
    try:
        import engram_core
    except ImportError as exc:
        raise OwnerStoreBehindSchema(
            f"cannot check the store's schema version: import of engram_core failed "
            f"({type(exc).__name__}: {exc}); the owner's runtime is incomplete"
        ) from exc

    required = engram_core.LATEST_SCHEMA_VERSION
    try:
        present = Path(store_path).exists()
    except OSError as exc:
        raise OwnerStoreBehindSchema(
            f"cannot stat the store at {store_path} ({type(exc).__name__}: {exc}); "
            f"requires schema version {required}"
        ) from exc
    if not present:
        raise OwnerStoreBehindSchema(
            f"no knowledge.db at {store_path}: the store has not been created or migrated yet "
            f"(this owner requires schema version {required}); run the migration "
            f"(any ENGRAM session start does) and the supervisor will retry"
        )
    conn = None
    try:
        conn = _connect_store_ro(store_path)
        found = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            # Retryable and NOT evidence the store is behind: a migration may hold the lock.
            raise OwnerStoreBehindSchema(
                f"the store at {store_path} is locked/busy ({exc}); a migration may be running, "
                f"so its schema version could not be read; the supervisor will retry "
                f"(this owner requires schema version {required})"
            ) from exc
        raise OwnerStoreBehindSchema(
            f"cannot open the store at {store_path} to read its schema version "
            f"({type(exc).__name__}: {exc}); this owner requires schema version {required}"
        ) from exc
    except sqlite3.Error as exc:
        raise OwnerStoreBehindSchema(
            f"the store at {store_path} is not a valid, intact SQLite database "
            f"({type(exc).__name__}: {exc}); its schema version cannot be read "
            f"(this owner requires schema version {required})"
        ) from exc
    finally:
        if conn is not None:
            conn.close()
    if found < required:
        raise OwnerStoreBehindSchema(
            f"the store at {store_path} is at schema version {found}, but this owner requires "
            f"{required}: it would classify its read templates against the old schema and drop "
            f"those needing newer tables; migrate the store, then start the owner"
        )


def _require_sqlite_setlimit(conn_cls=None, module=None) -> None:
    """Fail closed unless sqlite3 offers `Connection.setlimit` and
    `SQLITE_LIMIT_LENGTH`.

    #3349: both are "Added in version 3.11" (Python sqlite3 docs); on 3.10
    the `setlimit` call in start() would raise AttributeError mid-start,
    after files and connections were already opened. Checked by FEATURE,
    not by sys.version_info. `conn_cls` / `module` are injection seams for
    tests (a C type cannot be delattr'd); they default to the real
    sqlite3 at call time.
    """
    if module is None:
        module = sqlite3
    if conn_cls is None:
        conn_cls = module.Connection
    if not hasattr(conn_cls, "setlimit") or not hasattr(module, "SQLITE_LIMIT_LENGTH"):
        raise OwnerPythonTooOld(
            "engramd owner requires Python 3.11+ (sqlite3.Connection.setlimit and "
            "SQLITE_LIMIT_LENGTH, which bound client-read result size, were added "
            f"in 3.11); running Python {sys.version.split()[0]}. Refusing to start "
            "rather than serve reads without the size bound (#3349)."
        )


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

# Read-form pragmas the client-read floor admits. `user_version` is the
# harmless schema-version probe. `data_version` (#3665, U6b) was added as a
# MEASURED widening: SQLite's fts5vocab virtual table runs
# `PRAGMA data_version` internally on every scan, so the typed `df` method
# (a document-frequency lookup over nodes_fts_vocab) cannot run on this
# connection without it. Rulings: design seat (#3665 comment 6124428822) --
# typed owner method `df`, and allow `data_version` in READ form only;
# security seat (#3665 comment 6124451743) -- no hold; measured on Linux /
# SQLite 3.45.1, `data_version` is the only new pragma and adds zero new
# action codes. Residual, accepted: a coarse write-timing oracle -- an int
# counter that changes when ANOTHER connection commits, carrying no store
# content. Any further name needs the same measurement discipline as the
# baseline above (measure what fires, record it, get the security seat's
# read); it is not a free edit. The write form stays denied by the
# `arg2 is not None` guard in `_client_read_authorizer`.
CLIENT_READ_PRAGMA_ALLOWLIST = frozenset({"user_version", "data_version"})


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
# ~/.engram/knowledge.db ($ENGRAM_HOME) (3,324 rows in `nodes`, per the fairy spec) has
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


# ---------------------------------------------------------------------------
# Read-template registry wiring (U4c, #3196). Resolved and loaded in start(),
# BEFORE the socket is bound; the owner gains NO socket operation from it.
# ---------------------------------------------------------------------------

READ_TEMPLATES_FILENAME = "engramd_read_templates.json"


def _find_read_templates_path() -> "Path | None":
    """Locate the committed template list WITHOUT assuming the source
    layout (project CLAUDE.md, "Plugin build restructures code paths"):
    the file BESIDE this module first, then `$CLAUDE_PLUGIN_ROOT`, then the
    nearest ancestor of this module that actually contains the file -- never
    a fixed `parents[N]`. The data file ships beside the module in both the
    source and the flattened plugin layouts, so a stale CLAUDE_PLUGIN_ROOT
    must not override it. None if it cannot be found (the registry is then
    EMPTY: everything refused)."""
    sibling = Path(__file__).resolve().with_name(READ_TEMPLATES_FILENAME)
    if sibling.is_file():
        return sibling
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        cand = Path(root) / READ_TEMPLATES_FILENAME
        if cand.is_file():
            return cand
    here = Path(__file__).resolve()
    for d in here.parents:
        cand = d / READ_TEMPLATES_FILENAME
        if cand.is_file():
            return cand
    return None


def _read_install_tier(engram_home: Path) -> "str | None":
    """`install_tier` from `<ENGRAM_HOME>/config.json`; None if absent or
    unreadable (which `resolve_dynamic_flag` treats as not-dev). Read through
    `read_regular_text` (#3838): `start()` runs this before the owner serves, and
    a FIFO at config.json would block a plain `read_text` until a writer appeared;
    a non-regular file is an OSError here, i.e. None."""
    try:
        cfg = json.loads(read_regular_text(engram_home / "config.json", encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tier = cfg.get("install_tier") if isinstance(cfg, dict) else None
    return tier if isinstance(tier, str) else None


# ---------------------------------------------------------------------------
# Serialize-once read replies (#3678, U7 cut 2). Each row is JSON-encoded ONCE;
# the response-size bound is counted on those fragments and the reply is
# assembled from the SAME fragments, so the bytes checked are the bytes sent.
# `_assemble_read_ok` reproduces json.dumps's default separators (", " and
# ": ") and ensure_ascii exactly: the wire output is byte-identical to
# json.dumps({"status": "ok", "result": rows}).
# ---------------------------------------------------------------------------

_READ_OK_PREFIX = '{"status": "ok", "result": ['
_READ_OK_SUFFIX = ']}'
_READ_OK_EMPTY_LEN = len('{"status": "ok", "result": []}')


def _assemble_read_ok(fragments: list) -> str:
    return _READ_OK_PREFIX + ", ".join(fragments) + _READ_OK_SUFFIX


class _RenderedReadOk(dict):
    """The `ok` reply of `read`, as the dict callers have always seen, carrying
    the exact wire text built from the size-checked fragments. `_handle_connection`
    sends `wire_text` instead of serializing the dict again. Do not mutate the
    dict after construction: `wire_text` is a copy and would not follow."""

    def __init__(self, rows: list, wire_text: str) -> None:
        super().__init__(status="ok", result=rows)
        self.wire_text = wire_text


def _render_read_rows(outcome) -> "tuple[dict, int]":
    """-> (reply, counted bytes). `reply` is a `_RenderedReadOk`, or the same
    rejection dict as before (unsupported cell type; over MAX_READ_RESPONSE_BYTES).
    The count keeps the old per-row allowance (+2 per row for the separator)."""
    rows, fragments = [], []
    wire_bytes = _READ_OK_EMPTY_LEN
    for row in outcome:
        cells = list(row)
        for cell in cells:
            if cell is not None and type(cell) not in (int, float, str):
                return {
                    "status": "rejected",
                    "reason": "unsupported_value_type",
                    "detail": f"a result cell has type {type(cell).__name__}; only None/int/float/str are served",
                    "remedy": "select a text/number projection of the column, or extend the read contract deliberately",
                }, wire_bytes
        fragment = json.dumps(cells)
        wire_bytes += len(fragment) + 2
        if wire_bytes > MAX_READ_RESPONSE_BYTES:
            return {
                "status": "rejected",
                "reason": "result_too_large",
                "detail": f"the result is over {MAX_READ_RESPONSE_BYTES} bytes serialised (stopped counting at {wire_bytes}); nothing was sent",
                "remedy": "this is a property of the statement and the data, not an outage -- do NOT restart the owner; narrow the statement (LIMIT / fewer columns) or ask for a paged read",
            }, wire_bytes
        rows.append(cells)
        fragments.append(fragment)
    return _RenderedReadOk(rows, _assemble_read_ok(fragments)), wire_bytes


def _df_rejection(reason: str, detail: str, remedy: str) -> dict:
    return {"status": "rejected", "reason": reason, "detail": detail, "remedy": remedy}


def _df_param_refusal(params) -> "dict | None":
    """Validate a `df` request; None = valid, else the wire rejection. EACH
    violation has its own named reason. Check order (the first violated wins):
    shape, list, empty, too many, then per element in index order: not-str,
    empty, too long (UTF-8 bytes). Touches no connection."""
    if not isinstance(params, dict) or set(params) != {"terms"}:
        return _df_rejection(
            "df_params_shape",
            "df params must be a dict with exactly the key 'terms'",
            "send {'terms': [<str>, ...]} and no other key",
        )
    terms = params["terms"]
    if not isinstance(terms, list):
        return _df_rejection(
            "df_terms_not_list", f"'terms' must be a list, got {type(terms).__name__}", "send 'terms' as a list of strings"
        )
    if not terms:
        return _df_rejection("df_terms_empty", "'terms' is an empty list", "send at least one term")
    if len(terms) > DF_TERMS_MAX:
        return _df_rejection(
            "df_terms_too_many",
            f"'terms' has {len(terms)} entries; the maximum is {DF_TERMS_MAX}",
            f"split the batch into requests of at most {DF_TERMS_MAX} terms",
        )
    for i, term in enumerate(terms):
        if not isinstance(term, str):
            return _df_rejection(
                "df_term_not_str", f"terms[{i}] is {type(term).__name__}, not a string", "send every term as a string"
            )
        if term == "":
            return _df_rejection("df_term_empty", f"terms[{i}] is the empty string", "drop empty terms before sending")
        if len(term.encode("utf-8", "surrogatepass")) > DF_TERM_MAX_BYTES:
            return _df_rejection(
                "df_term_too_long",
                f"terms[{i}] exceeds {DF_TERM_MAX_BYTES} UTF-8 bytes",
                f"terms are capped at {DF_TERM_MAX_BYTES} bytes; drop or shorten the term",
            )
    return None


def _chain_param_refusal(params) -> "dict | None":
    """Validate a `chain` request; None = valid, else the wire rejection. EACH
    violation has its own named reason. Check order (the first violated wins):
    shape, node_id type, node_id empty, node_id too long (UTF-8 BYTES, counted
    with surrogatepass so a lone surrogate is measured rather than raising),
    node_id not encodable (a lone surrogate cannot be bound as a SQL parameter),
    max_depth type (bool is not an int), max_depth negative. A `max_depth` above
    CHAIN_MAX_DEPTH_CAP is NOT a violation: it is clamped by the handler.
    Touches no connection; the node id is never echoed into a detail."""
    if not isinstance(params, dict) or set(params) != {"node_id", "max_depth"}:
        return _df_rejection(
            "chain_params_shape",
            "chain params must be a dict with exactly the keys 'node_id' and 'max_depth'",
            "send {'node_id': <str>, 'max_depth': <int>} and no other key",
        )
    node_id, max_depth = params["node_id"], params["max_depth"]
    if not isinstance(node_id, str):
        return _df_rejection(
            "chain_node_id_not_str", f"'node_id' must be a string, got {type(node_id).__name__}", "send 'node_id' as a string"
        )
    if node_id == "":
        return _df_rejection("chain_node_id_empty", "'node_id' is the empty string", "send a node id")
    if len(node_id.encode("utf-8", "surrogatepass")) > CHAIN_NODE_ID_MAX_BYTES:
        return _df_rejection(
            "chain_node_id_too_long",
            f"'node_id' exceeds {CHAIN_NODE_ID_MAX_BYTES} UTF-8 bytes",
            f"node ids are capped at {CHAIN_NODE_ID_MAX_BYTES} bytes; send the real id",
        )
    try:
        node_id.encode("utf-8")
    except UnicodeEncodeError:
        return _df_rejection(
            "chain_node_id_not_utf8",
            "'node_id' contains a lone surrogate, which cannot be encoded as UTF-8",
            "send a node id that is valid text",
        )
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        return _df_rejection(
            "chain_max_depth_not_int", f"'max_depth' must be an integer, got {type(max_depth).__name__}", "send 'max_depth' as an int"
        )
    if max_depth < 0:
        return _df_rejection("chain_max_depth_negative", "'max_depth' is negative", "send 0 or a positive depth")
    return None


def _cbt_param_refusal(params) -> "dict | None":
    """Validate a `confidence_by_type` request; None = valid, else the wire rejection. EACH
    violation has its own named reason; nothing the caller sent is echoed. Check order (the
    first violated wins): shape (a dict with exactly the key 'modes'), modes not a list, modes
    empty, modes longer than CBT_MAX_MODES (refused, never clamped: section 3.6); then ONE pass
    over the elements in list order, each checked not-str -> unknown -> duplicate-of-an-earlier-
    element (the element's type is checked BEFORE its membership test, so a list / dict element
    cannot raise). Touches no connection."""
    if not isinstance(params, dict) or set(params) != {"modes"}:
        return _df_rejection(
            "cbt_params_shape",
            "confidence_by_type params must be a dict with exactly the key 'modes'",
            "send {'modes': [<str>, ...]} and no other key",
        )
    modes = params["modes"]
    if not isinstance(modes, list):
        return _df_rejection(
            "cbt_modes_not_list", f"'modes' must be a list, got {type(modes).__name__}", "send 'modes' as a list of strings"
        )
    if not modes:
        return _df_rejection("cbt_modes_empty", "'modes' is empty", "send at least one mode")
    if len(modes) > CBT_MAX_MODES:
        return _df_rejection(
            "cbt_modes_too_many",
            f"'modes' has more than {CBT_MAX_MODES} entries",
            f"send at most {CBT_MAX_MODES} modes; the request is refused, not shortened",
        )
    # CBT_MODES, not engram_stats.STATS_MODES: validation must not import the runtime.
    seen: list = []
    for i, mode in enumerate(modes):
        if not isinstance(mode, str):
            return _df_rejection(
                "cbt_mode_not_str", f"modes[{i}] is {type(mode).__name__}, not a string", "send every mode as a string"
            )
        if mode not in CBT_MODES:
            return _df_rejection(
                "cbt_mode_unknown", f"modes[{i}] is not a known mode", f"known modes: {', '.join(CBT_MODES)}"
            )
        if mode in seen:
            return _df_rejection("cbt_mode_duplicate", f"modes[{i}] repeats an earlier entry", "send each mode once")
        seen.append(mode)
    return None


def _engram_core(home):
    """Import `engram_core` LAZILY, on the first `chain` / `confidence_by_type` request. It is
    large, and the owner's startup must not pay for it. Measured: importing it opens no
    database, creates no file and starts no thread (its one process-level effect is
    `os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "1")`); cells pin the first two. It resolves
    from this module's own directory, where the plugin build puts it beside this file.

    `home` is REQUIRED (#3741, Borges Q1): the owner's own ENGRAM home. engram_core resolves
    its paths from the process env at import; the owner's home may differ from that, so every
    call re-points the core at `home` with `engram_core._configure_paths(home)` -- module
    globals only, no database opened, nothing created -- and core (`_get_current_turn` reads
    CONFIG_PATH) and owner share one home by construction. (Re-pointing on every call, not
    only the first, keeps a process that hosts a second owner on another home correct; it is
    idempotent.) `home=None` is a TypeError: an optional home would silently read the
    process-env home's config.json, re-opening the unconfigured path this requirement closes.
    ImportError propagates to the handler, which names it."""
    if home is None:
        raise TypeError("_engram_core requires the owner's home (home=None would read the process-env home)")
    import engram_core

    engram_core._configure_paths(home)
    return engram_core


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

    def __init__(
        self, conn: sqlite3.Connection, registry: "rr.Registry | None" = None, *, home: "Path"
    ) -> None:
        self._conn = conn
        # #3741: the owner's home, handed to `_engram_core(home)` by the typed ops that run
        # engram_core code. Required (keyword-only): a gate without a home is a TypeError.
        self._home = home
        # U4c (#3196): the read-template registry. None means the EMPTY
        # registry -- a gate built without one refuses everything; it never
        # defaults to permissive.
        self._registry = registry if registry is not None else rr.EMPTY_REGISTRY

    def execute(self, sql: str, params: tuple = (), *, deadline: float):
        """Registry lookup FIRST (R1-R3, R8), then the existing deadline
        path. A refused request is RETURNED as an `rr.Rejected` -- never
        raised, and nothing is prepared on the connection (R2). An admitted
        statement runs the deadline path unchanged; its authorizer and
        resource bounds remain the second, independent layer (R9)."""
        refusal = self._registry.check(sql, params)
        if refusal is not None:
            return refusal
        try:
            return self._execute_admitted(sql, params, deadline=deadline)
        except sqlite3.OperationalError as exc:
            # The split is by SQLite's own error code. SQLITE_ERROR (1) is a
            # statement-level error from a bad parameter (e.g. malformed JSON
            # into json_each(?)): the CLIENT's fault, so it is returned. The
            # deadline abort (SQLITE_INTERRUPT, 9) and the resource-limit abort
            # (sqlite3.DataError / SQLITE_TOOBIG, not an OperationalError) are
            # the OWNER's controls and propagate to the dispatcher. No
            # sqlite_errorcode (Python < 3.11): not convertible -- fail loud.
            if getattr(exc, "sqlite_errorcode", None) != 1:
                raise
            logging.getLogger("engramd_owner").warning(
                "admitted read failed at execution (template %s): %s: %s",
                rr.template_key(sql), type(exc).__name__, exc)
            return rr.Rejected(
                reason="read_failed",
                detail=f"{type(exc).__name__}: {exc}"[:200],
                remedy="the statement was admitted but failed at execution; if the parameters "
                "are valid this may be an owner-side fault (schema drift), see the owner log",
            )

    def execute_typed(self, sql: str, params: tuple = (), *, deadline: float) -> list:
        """Entry point for a TYPED owner method's fixed statement (#3665 `df`):
        skips the read-template registry and goes straight to
        `_execute_admitted`. Sound because the registry exists to vet
        CLIENT-SUPPLIED SQL text, and here none can exist: `sql` must be a
        member of the module-level `_TYPED_STATEMENTS` (enforced here, not
        merely documented -- any other string raises ValueError before anything
        is prepared), parameters are bound, and the authorizer floor on this
        connection (the second, independent layer) still applies to everything
        that is prepared. Still holds only the client-read connection, so the
        routing guarantee is unchanged. SQLite errors propagate to the caller's
        `_read_failed` / deadline mapping."""
        if sql not in _TYPED_STATEMENTS:
            raise ValueError(f"execute_typed: not a typed statement: {sql[:60]}")
        return self._execute_admitted(sql, params, deadline=deadline)

    def run_chain(self, node_id: str, max_depth: int, *, deadline: float) -> dict:
        """Entry point for the typed `chain` method (#3740): run
        `engram_core.generalization_chain(<this connection>, node_id, max_depth)` with the
        request deadline armed, and return its dict. It is NOT `execute_typed` and does NOT
        touch `_TYPED_STATEMENTS`: the walk's statements are fixed code inside engram_core,
        not text from the client, so there is no statement string for the gate to vet, and
        the set stays exactly `df`'s two. What still holds: this gate has only the
        client-read connection (routing), the permanent authorizer floor applies to every
        statement the walk prepares (a denied one surfaces as a SQLite error), and the
        progress handler stops the walk at `deadline`, exactly as `_execute_admitted` does.
        The caller has already validated and clamped `node_id` / `max_depth`. SQLite errors
        and ImportError propagate to the caller's mapping.

        Why one deadline covers the whole walk: the progress handler's VM-step counter
        accumulates across the walk's ~9 REUSED cached statements (the connection's statement
        cache keeps them across the walk's hops), so the handler fires on the walk's total work,
        not once per statement. A future change that builds per-call SQL text in engram_core
        (new text per hop, a cache miss each time) would weaken that, and the deadline with it;
        re-measure before making such a change.

        What the deadline does NOT reach: the final `cycle_detected` DFS inside
        `generalization_chain` issues zero SQL, so the progress handler cannot fire in it, by
        construction. Its cost is O(V+E) of the reached subgraph (about 40k dict operations at
        20k fan-in, sub-millisecond). A future change that moves work into that DFS would void
        the deadline guarantee silently; re-measure before making one.

        `engram_core.generalization_chain` itself refuses a negative or non-int depth (ValueError);
        the owner op's own named refusal (`chain_max_depth_negative` / `chain_max_depth_not_int`)
        runs first, so that error is never reached from a client request."""
        core = _engram_core(self._home)
        _arm_read_deadline(self._conn, deadline)
        try:
            return core.generalization_chain(self._conn, node_id, max_depth)
        finally:
            # same slate-clearing as _execute_admitted: no stale deadline for the next call
            self._conn.set_progress_handler(None, 0)

    def run_confidence_by_type(self, modes: list, *, deadline: float) -> dict:
        """Entry point for the typed `confidence_by_type` method (#3741): the by_type confidence
        distribution for each mode, computed by `engram_stats.confidence_by_type` (the SAME code
        `engram_stats` runs, so the two are equal by construction) on THIS client-read connection
        with the request deadline armed, plus the owner home's `current_turn`. Returns
        `{"current_turn": int, "by_mode": {mode: {"by_type": ...}}}`.

        Like `run_chain` it is NOT `execute_typed` and `_TYPED_STATEMENTS` is not widened: the
        statements are fixed code in engram_stats, the client sends only mode NAMES (already
        validated by `_cbt_param_refusal`), and the authorizer floor on this connection applies to
        everything prepared. Read-only: no write, no tool_timing, no migration, no `_get_db`.
        SQLite errors and ImportError propagate to the caller's mapping."""
        core = _engram_core(self._home)
        import engram_stats  # after _engram_core(home): the core must be pointed at the home first

        _arm_read_deadline(self._conn, deadline)
        try:
            by_mode = engram_stats.confidence_by_type(self._conn, modes, engram_stats._now_utc())
            return {"current_turn": int(core._get_current_turn()), "by_mode": by_mode}
        finally:
            self._conn.set_progress_handler(None, 0)

    def _execute_admitted(self, sql: str, params: tuple, *, deadline: float) -> list:
        """The deadline-armed execute path, reached only for a statement the
        registry admitted. Reached from `EngramdOwner._handle_read()` (the
        client-facing `read` method, U4d #3197) via `execute()`."""
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


def _darwin_unsupported_reason() -> str | None:
    """The Darwin branch of `platform_unsupported_reason()` (#3666, design doc
    §2.5): None only when all three prerequisites hold; otherwise a refusal
    naming the FIRST missing one. Each is read from its own primitive (the
    `socket` attribute, the two module path constants) so a test can remove
    exactly one -- never by patching `platform.system` alone (#3375)."""
    if not hasattr(socket, "LOCAL_PEERCRED"):
        return (
            "refusing to start: this Python/OS build has no "
            "socket.LOCAL_PEERCRED -- the Darwin peer-credential check "
            "(design doc §2.2) cannot run here, and starting without it "
            "would be a silent permission-layer gap"
        )
    if not os.path.exists(DARWIN_CHMOD):
        return (
            f"refusing to start: {DARWIN_CHMOD} is required to strip "
            f"inherited ACLs on the socket's run/ directory (`chmod -N`, "
            f"design doc §2.1) and was not found; it is called by absolute "
            f"path, never through PATH"
        )
    if not os.path.isdir(DARWIN_DEV_FD):
        return (
            f"refusing to start: {DARWIN_DEV_FD} is not a directory -- the "
            f"run/ strip and its ACL read-back address the open directory fd "
            f"as {DARWIN_DEV_FD}/<fd> (design doc §2.5)"
        )
    return None


def _darwin_acl_entries_via_fd(fd: int) -> list[str]:
    """The ACL entries on the object behind `fd`, read THROUGH that fd with
    libc's `acl_get_fd_np` (#3666, design doc §2.5's open read-back item).
    Returns one text line per entry; `[]` means no ACL.

    Measured on macOS 26.6.2: with no ACL, `acl_get_fd_np` returns NULL with
    errno ENOENT (that is the documented "no ACL", not a failure); with an
    entry it returns the acl_t, whose `acl_to_text` lists it. (`ls -led
    /dev/fd/<fd>` was tried first and shows NO entry for a directory that
    carries one, so it cannot serve as the read-back.) Any other failure, or a
    libc without these symbols, raises OwnerAclNotStripped: an ACL we
    could not read is not an ACL we may call absent."""
    import ctypes  # lazy: Darwin-only, keeps the Linux import path untouched

    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        get_fd = libc.acl_get_fd_np
        get_fd.restype = ctypes.c_void_p
        get_fd.argtypes = [ctypes.c_int, ctypes.c_int]
        to_text = libc.acl_to_text
        to_text.restype = ctypes.c_void_p
        to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        free = libc.acl_free
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:
        raise OwnerAclNotStripped(
            f"refusing to start: cannot read back the ACL on run/ (libc "
            f"acl_get_fd_np unavailable: {exc}); an unverified strip is not "
            f"accepted"
        ) from exc

    ACL_TYPE_EXTENDED = 0x00000100  # <sys/acl.h>
    ctypes.set_errno(0)
    acl = get_fd(fd, ACL_TYPE_EXTENDED)
    if not acl:
        err = ctypes.get_errno()
        if err == errno.ENOENT:
            return []
        raise OwnerAclNotStripped(
            f"refusing to start: ACL read-back on run/ failed "
            f"(acl_get_fd_np errno {err}: {os.strerror(err)}); an unverified "
            f"strip is not accepted"
        )
    try:
        length = ctypes.c_ssize_t()
        text_ptr = to_text(acl, ctypes.byref(length))
        if not text_ptr:
            err = ctypes.get_errno()
            raise OwnerAclNotStripped(
                f"refusing to start: ACL read-back on run/ found an ACL it "
                f"could not render (acl_to_text errno {err}: "
                f"{os.strerror(err)}); an unverified strip is not accepted"
            )
        try:
            text = ctypes.string_at(text_ptr).decode("utf-8", "replace")
        finally:
            free(text_ptr)
    finally:
        free(acl)
    # "!#acl 1" header, then one "kind:guid:name:id:allow|deny:perms" per entry.
    return [ln for ln in text.splitlines() if ln and not ln.startswith("!#acl")]


def _owner_arm() -> str | None:
    """Which permission arm this seat runs (#3708): "linux", "darwin", or None
    for any other platform. The decision now lives in the client, as
    `ec.platform_arm()` (#3712), keyed on `platform.system()` ALONE: the client
    also needs the platform (its socket-path limit) and must never import the
    owner, so the one source sits on the side of the import direction both can
    reach. This stays a name here because the platform gate, the TCC check,
    `_harden_run_dir` and the peer check all ask it, and tests patch it. The
    `hasattr(socket, ...)` prerequisite checks do not choose an arm; they ask
    whether the chosen arm's tool exists."""
    return ec.platform_arm()


def platform_unsupported_reason() -> str | None:
    """Why this seat cannot run the owner, or None when it can (#3194, #3292).

    Linux: four checks, in this order, first failure wins: (1) not Linux,
    (2) no `setfacl` on PATH, (3) no `getfacl` on PATH (the strip's
    read-back, #3698), (4) no `socket.SO_PEERCRED`.
    Darwin (#3666): `_darwin_unsupported_reason()` -- `socket.LOCAL_PEERCRED`,
    `/bin/chmod` and `/dev/fd`, each named when missing. Any other platform
    refuses.
    `EngramdOwner._check_platform_support()` raises `OwnerPermissionUnsupported`
    with the returned string, and the test suite's `requires_linux_owner`
    marker skips on `is not None`, so the refusal and the skip share this one
    definition."""
    arm = _owner_arm()
    if arm == "darwin":
        return _darwin_unsupported_reason()
    if arm != "linux":
        return (
            f"refusing to start: the socket permission layer (ACL "
            f"strip on run/, SO_PEERCRED peer check) is only "
            f"implemented for Linux and Darwin in this build; this seat "
            f"reports platform.system() == {platform.system()!r}"
        )
    if shutil.which("setfacl") is None:
        return (
            "refusing to start: `setfacl` is required to strip "
            "inherited ACLs on the socket's run/ directory (design "
            "doc §2.1) and was not found on PATH"
        )
    if shutil.which("getfacl") is None:
        return (
            "refusing to start: `getfacl` is required to read back the "
            "ACL strip on the socket's run/ directory (design doc §2.2: "
            "an unverified strip is a silent gap) and was not found on PATH"
        )
    if not hasattr(socket, "SO_PEERCRED"):
        return (
            "refusing to start: this Python/OS build has no "
            "socket.SO_PEERCRED -- the peer-credential check (design "
            "doc §2.2) cannot run here, and starting without it would "
            "be a silent permission-layer gap"
        )
    return None


_ACL_LEFTOVER_LIST_MAX = 10
_ACL_BASE_RE = re.compile(r"^(user|group|other)::\S*(\s+#.*)?$")


def _read_acl_entries(fd: int, run_dir=None) -> list[str]:
    """The ACL entries of the directory open as `fd`, one string per line,
    blank and `#` comment lines dropped (#3698). Reads through
    /proc/self/fd/<fd> -- the SAME fd the strip used -- never a path, so a
    swap of run/ between strip and read-back cannot report a clean ACL for a
    different directory. A getfacl failure (non-zero exit, timeout, missing
    binary) or output with no entry at all raises OwnerAclNotStripped with its
    own reason: an unverifiable strip refuses. stderr is NOT a failure signal
    (getfacl always warns about the leading '/')."""
    where = str(run_dir) if run_dir is not None else f"/proc/self/fd/{fd}"
    try:
        proc = subprocess.run(
            ["getfacl", "-c", "-n", f"/proc/self/fd/{fd}"],
            capture_output=True,
            text=True,
            timeout=30,
            pass_fds=[fd],
        )
    except subprocess.TimeoutExpired as exc:
        raise OwnerAclNotStripped(
            f"refusing to start: `getfacl` on {where} timed out after "
            f"{exc.timeout}s -- the ACL strip could not be verified"
        ) from exc
    except OSError as exc:
        raise OwnerAclNotStripped(
            f"refusing to start: `getfacl` could not be run on {where} "
            f"({exc}) -- the ACL strip could not be verified"
        ) from exc
    if proc.returncode != 0:
        raise OwnerAclNotStripped(
            f"refusing to start: `getfacl` on {where} failed (exit "
            f"{proc.returncode}): {proc.stderr.strip()} -- the ACL strip "
            f"could not be verified"
        )
    entries = [
        ln.strip()
        for ln in proc.stdout.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not entries:
        raise OwnerAclNotStripped(
            f"refusing to start: `getfacl` on {where} printed no ACL entries "
            f"(unparseable output) -- the ACL strip could not be verified"
        )
    return entries


def _acl_leftovers(entries: list[str]) -> list[str]:
    """The entries that make a stripped ACL NOT clean (#3698): everything but
    exactly one each of user::, group::, other:: (any permission bits). A
    named entry, mask::, any default: line or an unrecognised line is listed
    verbatim; a missing or duplicated base entry is listed as a note."""
    leftovers: list[str] = []
    seen: dict[str, int] = {"user": 0, "group": 0, "other": 0}
    for ln in entries:
        m = _ACL_BASE_RE.match(ln)
        if m is None:
            leftovers.append(ln)
        else:
            seen[m.group(1)] += 1
            if seen[m.group(1)] > 1:
                leftovers.append(ln)
    for kind, n in seen.items():
        if n == 0:
            leftovers.append(f"<missing base entry {kind}::>")
    return leftovers


# ---------------------------------------------------------------------------
# The owner's first write: the typed `use_reward` op (#3822, U11 of #2652, Stage A)
# ---------------------------------------------------------------------------
#
# docs/2652-engrammanager-design.md SS3.1 (typed writes, never a generic write_sql) and SS5.4
# (read-back acknowledgement + idempotency key). The client supplies ONLY {node_ids, action,
# request_id}: no alpha, no config path, no value. alpha is owner-side
# (`engram_restamp.USE_ALPHA[action]`); the memory config is the OWNER'S OWN
# `ENGRAM_HOME/config.json`, read fresh on every call.

# Every method in the table carries a kind. The write kinds are a DECLARED, CLOSED set: one constant, pinned
# by tests/test_engramd_use_reward_3822.py to exactly the four below. An unclassified method is
# default-deny at dispatch.
METHOD_KINDS = frozenset({"read", "write"})
# the 2nd write: #3856; the 3rd and 4th: #3879 (the regenerate-embeddings tool's `fill_embeddings` / `finish_embedding_regen`)
WRITE_METHODS = frozenset({"use_reward", "advance_moment_cursor", "fill_embeddings", "finish_embedding_regen"})
# Writes whose handler also receives the WIRE envelope's `request_id` (as a third argument): their params are a
# closed key set that cannot carry one (`fill_embeddings`: exactly {model, items}). use_reward carries its own
# request_id inside params and is not in this set.
WIRE_REQUEST_ID_METHODS = frozenset({"fill_embeddings"})

# The closed action enum (today one action). Anything else is refused before the store is touched.
USE_REWARD_ACTIONS = frozenset({"mention"})
USE_REWARD_MAX_IDS = 500  # per call; enforced before any SQL (an over-cap call opens no transaction)
USE_REWARD_NODE_ID_MAX_BYTES = 256
USE_REWARD_REQUEST_ID_MAX_BYTES = 128  # a uuid4 string is 36
# In-memory, bounded (LRU) dedupe store. KNOWN BOUND: it dies with the process, so an owner restart between
# COMMIT and ack followed by a client retry of the same request_id bumps a second time (a small monotone
# over-credit, not corruption); and past this cap the oldest request_id is forgotten. Read at call time.
USE_REWARD_DEDUPE_CAP = 1024
# The TOTAL time one use_reward may spend WAITING (seconds): the `_owner_lock` acquire PLUS SQLite's busy wait
# for a competing direct writer share this one budget (the busy timeout for the op is the REMAINING budget).
# When it runs out the op answers the typed, DETERMINATE `busy` with nothing committed. It is pinned by a test to
# be strictly less than `engramd_client.OWNER_WRITE_CLIENT_TIMEOUT - OWNER_WRITE_REPLY_RESERVE`, so that answer
# arrives before the client gives up. Read at call time.
OWNER_WRITE_LOCK_WAIT = 1.0
USE_REWARD_MONOTONE_FLOOR = 0.99  # ids at or above this utility are exempt from the monotone read-back invariant
USE_REWARD_BUSY_RETRY_SLEEP = 0.02  # seconds between attempts while a direct writer holds the store's write lock

# The triggers on `nodes` in the real schema, MEASURED (7), pinned against sqlite_master by a test: a newly
# added trigger fails that cell loudly. None of them fires on UPDATE of utility_score / importance_score /
# recall_turn (each is AFTER INSERT / AFTER DELETE / BEFORE UPDATE OF metadata / AFTER UPDATE OF status,
# is_current, claim-and-text columns), so the set whose statements the op admits BY NAME is empty.
USE_REWARD_NODES_TRIGGERS_PINNED = frozenset({
    "nodes_fts_insert", "nodes_fts_delete", "nodes_retract_remove_from_fts", "nodes_fts_supersede_remove",
    "nodes_fts_rewrite_resync", "nodes_metadata_json_guard_insert", "nodes_metadata_json_guard_update",
})
USE_REWARD_ADMITTED_TRIGGERS: frozenset = frozenset()

# Column-exact allowlist, MEASURED from one real use_reward on the real schema (the recording authorizer's
# tuples are pinned in the test): reads of nodes.id / utility_score / importance_base / recall_count by the
# shared function, plus importance_score / recall_turn by the read-back; updates of exactly the three columns.
USE_REWARD_READ_COLUMNS = frozenset({
    "id", "utility_score", "importance_base", "recall_count", "importance_score", "recall_turn",
})
USE_REWARD_UPDATE_COLUMNS = frozenset({"utility_score", "importance_score", "recall_turn"})
USE_REWARD_SAVEPOINT = "engram_use_restamp"  # the shared function's own SAVEPOINT name


def _use_reward_allows(action_code, arg1, arg2, db_name, trigger_or_view) -> bool:
    """The op-scoped allowlist (pure): True iff this authorizer call is one use_reward needs. Everything else
    (INSERT, DELETE, other columns / tables / attached dbs, PRAGMA, DDL, functions, other savepoints) is False.
    A statement originating in a trigger or view is admitted only if that trigger is NAMED in
    USE_REWARD_ADMITTED_TRIGGERS (empty today: none fires), and is then held to the same column rules."""
    if trigger_or_view is not None and trigger_or_view not in USE_REWARD_ADMITTED_TRIGGERS:
        return False
    if action_code == sqlite3.SQLITE_SELECT:
        return True
    if action_code == sqlite3.SQLITE_READ:
        return db_name == "main" and arg1 == "nodes" and arg2 in USE_REWARD_READ_COLUMNS
    if action_code == sqlite3.SQLITE_UPDATE:
        return db_name == "main" and arg1 == "nodes" and arg2 in USE_REWARD_UPDATE_COLUMNS
    if action_code == sqlite3.SQLITE_TRANSACTION:
        return arg1 in ("BEGIN", "COMMIT", "ROLLBACK")
    if action_code == sqlite3.SQLITE_SAVEPOINT:
        return arg1 in ("BEGIN", "RELEASE", "ROLLBACK") and arg2 == USE_REWARD_SAVEPOINT
    return False


class _UseRewardGuard:
    """Per-op state for the authorizer: the denials seen, and the two phases in which a transaction COMMIT /
    ROLLBACK is the handler's own (a COMMIT issued from inside the shared function would be a partial commit)."""

    def __init__(self, allows=None, vec_allows=None) -> None:
        # `vec_allows`: an optional extra pure predicate consulted ONLY while `vec_phase` is open (the fill op's vec
        # mirror, #3879): the admissions that are legitimate inside the mirror and nowhere else.
        self._vec_allows = vec_allows
        self.vec_phase = False
        # The op's pure allowlist: `_use_reward_allows` by default, `_moment_cursor_allows` for the second write
        # (#3856). One guard, one set of COMMIT / ROLLBACK / busy-PRAGMA phases, two allowlists.
        self._allows = allows if allows is not None else _use_reward_allows
        self.denials: list = []
        self.allow_commit = False
        self.allow_rollback = False
        self.allow_busy_pragma = False  # the handler's own busy_timeout restore, even if the permissive restore failed

    def reset(self) -> None:
        """A fresh attempt (after a busy rollback): no denials carried over, the handler's phases closed."""
        self.denials.clear()
        self.allow_commit = False
        self.allow_rollback = False

    def authorizer(self, action_code, arg1, arg2, db_name, trigger_or_view):
        if self.allow_busy_pragma and action_code == sqlite3.SQLITE_PRAGMA and arg1 == "busy_timeout":
            return sqlite3.SQLITE_OK
        if action_code == sqlite3.SQLITE_TRANSACTION and (
            (arg1 == "COMMIT" and not self.allow_commit) or (arg1 == "ROLLBACK" and not self.allow_rollback)
        ):
            self.denials.append((action_code, arg1, arg2, db_name, trigger_or_view))
            return sqlite3.SQLITE_DENY
        if self._allows(action_code, arg1, arg2, db_name, trigger_or_view):
            return sqlite3.SQLITE_OK
        if self.vec_phase and self._vec_allows is not None and self._vec_allows(
                action_code, arg1, arg2, db_name, trigger_or_view):
            return sqlite3.SQLITE_OK
        self.denials.append((action_code, arg1, arg2, db_name, trigger_or_view))
        return sqlite3.SQLITE_DENY


# ---------------------------------------------------------------------------
# The second write: the typed `advance_moment_cursor` op (#3856, U13 of #2652, phase 2)
# ---------------------------------------------------------------------------
#
# The shared function `engram_moment_cursor.advance_moment_cursor` advances `metadata.special_moment_cursor` in ONE
# compare-and-set UPDATE. NO request_id and no dedupe store: a replay is harmless (a retry after COMMIT finds
# from + 1 and answers `stale`). The client supplies ONLY {person_id, from_cursor}; the owner computes the target.
MOMENT_CURSOR_PERSON_ID_MAX_BYTES = 256

# What U11's pinned property ("no column the op writes is one a trigger fires on") CANNOT be here: the metadata
# JSON guard trigger fires on EVERY metadata update. So the property is a NAMED allow-set: every trigger on `nodes`
# that can fire on `UPDATE nodes SET metadata` must be in it (pinned against sqlite_master by a test). A new trigger
# on `metadata` is NOT admitted by editing this set to make a cell pass; it is reported to the design seat.
MOMENT_CURSOR_ADMITTED_TRIGGERS = frozenset({"nodes_metadata_json_guard_update"})

# Column-exact allowlist, MEASURED (authorizer-recorded) over real runs of the shared function on the real schema
# (applied / stale / not_person / not_object and the read-back SELECT): reads of nodes.id / type / is_current /
# metadata; ONE UPDATE target, nodes.metadata; the four json functions (json_valid arrives through the guard
# trigger's WHEN clause and body; RAISE(ABORT) itself needs no authorization code).
MOMENT_CURSOR_READ_COLUMNS = frozenset({"id", "type", "is_current", "metadata"})
MOMENT_CURSOR_UPDATE_COLUMNS = frozenset({"metadata"})
MOMENT_CURSOR_FUNCTIONS = frozenset({"json_set", "json_type", "json_extract", "json_valid"})


def _moment_cursor_allows(action_code, arg1, arg2, db_name, trigger_or_view) -> bool:
    """The op-scoped allowlist (pure) for `advance_moment_cursor`: True iff this authorizer call is one the op
    needs. Everything else (INSERT, DELETE, other columns / tables / attached dbs, PRAGMA, DDL, other functions,
    SAVEPOINT) is False. A statement originating in a trigger is admitted only if that trigger is NAMED in
    MOMENT_CURSOR_ADMITTED_TRIGGERS (the metadata JSON guard), and is then held to the same rules."""
    if trigger_or_view is not None and trigger_or_view not in MOMENT_CURSOR_ADMITTED_TRIGGERS:
        return False
    if action_code == sqlite3.SQLITE_SELECT:
        return True
    if action_code == sqlite3.SQLITE_READ:
        return db_name == "main" and arg1 == "nodes" and arg2 in MOMENT_CURSOR_READ_COLUMNS
    if action_code == sqlite3.SQLITE_UPDATE:
        return db_name == "main" and arg1 == "nodes" and arg2 in MOMENT_CURSOR_UPDATE_COLUMNS
    if action_code == sqlite3.SQLITE_FUNCTION:
        return arg2 in MOMENT_CURSOR_FUNCTIONS
    if action_code == sqlite3.SQLITE_TRANSACTION:
        return arg1 in ("BEGIN", "COMMIT", "ROLLBACK")
    return False


def _moment_cursor_param_refusal(params) -> "dict | None":
    """Validate an `advance_moment_cursor` request; None = valid, else the wire rejection. EACH violation has its
    own named reason, checked in this order: (1) not a dict with exactly the keys 'person_id' and 'from_cursor'
    (an extra key, `request_id` included, or a missing one); (2) person_id: not a str, empty, over
    MOMENT_CURSOR_PERSON_ID_MAX_BYTES UTF-8 bytes (counted with surrogatepass), a lone surrogate (cannot be
    bound); (3) from_cursor: not an int (a bool, float, str, null, list, dict), outside [-(2**53), 2**53).
    Touches no connection, so a refused call executes no statement."""
    p = "advance_moment_cursor_"
    if not isinstance(params, dict) or set(params) != {"person_id", "from_cursor"}:
        return _df_rejection(
            p + "params_shape",
            "advance_moment_cursor params must be a dict with exactly the keys 'person_id' and 'from_cursor'",
            "send exactly {person_id, from_cursor}; there is no request_id and no target value")
    person_id, from_cursor = params["person_id"], params["from_cursor"]
    if not isinstance(person_id, str):
        return _df_rejection(p + "person_id_not_str", f"'person_id' must be a string, got {type(person_id).__name__}",
                             "send 'person_id' as a string")
    if person_id == "":
        return _df_rejection(p + "person_id_empty", "'person_id' is the empty string", "send a real person id")
    if len(person_id.encode("utf-8", "surrogatepass")) > MOMENT_CURSOR_PERSON_ID_MAX_BYTES:
        return _df_rejection(p + "person_id_too_long", f"'person_id' exceeds {MOMENT_CURSOR_PERSON_ID_MAX_BYTES} UTF-8 bytes",
                             f"ids are capped at {MOMENT_CURSOR_PERSON_ID_MAX_BYTES} bytes; send the real id")
    try:
        person_id.encode("utf-8")
    except UnicodeEncodeError:
        return _df_rejection(p + "person_id_not_utf8", "'person_id' contains a lone surrogate, which cannot be bound as a SQL parameter",
                             "send a well-formed string")
    if isinstance(from_cursor, bool) or not isinstance(from_cursor, int):
        return _df_rejection(p + "from_cursor_not_int", f"'from_cursor' must be an integer, got {type(from_cursor).__name__}",
                             "send 'from_cursor' as the integer cursor you read")
    if not (-(2 ** 53) <= from_cursor < 2 ** 53):
        return _df_rejection(p + "from_cursor_out_of_range", "'from_cursor' is outside [-(2**53), 2**53)",
                             "send the cursor you read; values outside the JSON-safe range are refused")
    return None


# ---------------------------------------------------------------------------
# The third and fourth writes: `fill_embeddings` and `finish_embedding_regen` (#3879, U16 of #2652)
# ---------------------------------------------------------------------------
#
# The regenerate-embeddings tool keeps computing vectors (no owner path loads a model); the owner gets the WRITES.
# `fill_embeddings` is a validated compare-and-set of `nodes.embedding` for up to 50 nodes (+ a reported best-effort
# `vec_nodes` mirror); `finish_embedding_regen` rebuilds the FTS index on the owner's connection and runs today's
# clear-or-finalize restore-flag branch through the SAME engram_core functions with `respect_lease=True`.
FILL_EMBEDDINGS_MAX_ITEMS = 50  # per call; enforced before any SQL
# The store's embedding dimension. engram_core has no single constant for it (the 384 is a literal in the vec0 DDL
# `embedding float[384]` and in the vec mirror's `len(vector) == 384`): a test ties this constant to those literals.
FILL_EMBEDDINGS_DIM = 384
FILL_EMBEDDINGS_VEC_SAVEPOINT = "engram_fill_vec"  # the vec mirror's own SAVEPOINT: a vec failure never poisons the fill

# MEASURED (authorizer-recorded over real runs on the real schema, which carries the seven pinned `nodes` triggers):
# NO trigger or view fires on `UPDATE nodes SET embedding` (every trigger on `nodes` is on metadata / status /
# is_current / claim-and-text columns), and none fires during the FTS rebuild or the restore-flag writes (the finish op
# never writes `nodes`). So the set of triggers whose statements either op admits BY NAME is empty, pinned against
# sqlite_master (USE_REWARD_NODES_TRIGGERS_PINNED) by a test, with a positive control that a claim UPDATE does fire one.
FILL_EMBEDDINGS_ADMITTED_TRIGGERS: frozenset = frozenset()
FINISH_EMBEDDING_REGEN_ADMITTED_TRIGGERS: frozenset = frozenset()

# fill: column-exact allowlist, MEASURED: reads of nodes.id / is_current / embedding, ONE update target nodes.embedding.
FILL_EMBEDDINGS_READ_COLUMNS = frozenset({"id", "is_current", "embedding"})
FILL_EMBEDDINGS_UPDATE_COLUMNS = frozenset({"embedding"})
# The vec0 table and its shadow tables. NOT measurable on a Python whose sqlite3 cannot load an extension (the macOS
# framework Python): the names are vec0's documented shadow set for one TEXT-keyed vector column. A name missing here
# is a denial INSIDE the vec savepoint, which degrades to `vec_mirrored: false` (reported, the nodes writes stand)
# rather than failing the fill.
_VEC_TABLE_NAME = re.compile(
    r"^vec_nodes(_(chunks|rowids|info|auxiliary|vector_chunks\d{2}|metadatachunks\d{2}|metadatatext\d{2}))?$")

# finish: MEASURED over the clear branch, the finalize branch (set and clear), a missing nodes_fts (the restore case: the
# CREATE VIRTUAL TABLE and its shadow tables), a stale under-indexed nodes_fts (the DROP + CREATE repair) and a missing
# embedding_restore_state table (the core function's CREATE TABLE IF NOT EXISTS).
FINISH_NODES_READ_COLUMNS = frozenset({
    "ROWID", "id", "type", "claim", "quoted_text", "interpretation", "trigger_text", "recall_summary", "recall_keywords",
    "status", "is_current", "embedding", "source_title", "predicted_event",
})
FINISH_RESTORE_STATE_READ_COLUMNS = frozenset({"singleton_key", "not_fully_restored", "null_count", "cause", "updated_at"})
FINISH_RESTORE_STATE_UPDATE_COLUMNS = frozenset({"not_fully_restored", "null_count", "cause", "updated_at"})
FINISH_FUNCTIONS = frozenset({"coalesce", "count"})
FINISH_PRAGMAS = frozenset({"user_version", "data_version"})


def _is_vec_table(name) -> bool:
    return isinstance(name, str) and _VEC_TABLE_NAME.match(name) is not None


def _is_fts_table(name) -> bool:
    """The `nodes_fts` family: the virtual table, its shadow tables (`nodes_fts_data` / `_idx` / `_docsize` / `_config`),
    the optional `nodes_fts_vocab`, and the shadow tables' auto-indexes."""
    return isinstance(name, str) and (
        name == "nodes_fts" or name.startswith("nodes_fts_") or name.startswith("sqlite_autoindex_nodes_fts_"))


def _fill_embeddings_allows(action_code, arg1, arg2, db_name, trigger_or_view) -> bool:
    """The op-scoped allowlist (pure) for `fill_embeddings`: True iff this authorizer call is one the op needs.
    Everything else (any other column / table / attached db, INSERT / DELETE on `nodes`, DDL, PRAGMA, other functions,
    other savepoints) is False. A statement originating in a trigger or view is admitted only if that trigger is
    NAMED in FILL_EMBEDDINGS_ADMITTED_TRIGGERS (empty: none fires). The vec mirror's statements run inside the op's
    own savepoint and may touch only the vec0 table and its shadow tables."""
    if trigger_or_view is not None and trigger_or_view not in FILL_EMBEDDINGS_ADMITTED_TRIGGERS:
        return False
    if action_code == sqlite3.SQLITE_SELECT:
        return True
    if action_code == sqlite3.SQLITE_READ:
        return db_name == "main" and (
            (arg1 == "nodes" and arg2 in FILL_EMBEDDINGS_READ_COLUMNS) or _is_vec_table(arg1))
    if action_code == sqlite3.SQLITE_UPDATE:
        return db_name == "main" and (
            (arg1 == "nodes" and arg2 in FILL_EMBEDDINGS_UPDATE_COLUMNS) or _is_vec_table(arg1))
    if action_code in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
        return db_name == "main" and _is_vec_table(arg1)
    if action_code == sqlite3.SQLITE_TRANSACTION:
        return arg1 in ("BEGIN", "COMMIT", "ROLLBACK")
    if action_code == sqlite3.SQLITE_SAVEPOINT:
        return arg1 in ("BEGIN", "RELEASE", "ROLLBACK") and arg2 == FILL_EMBEDDINGS_VEC_SAVEPOINT
    return False


def _fill_vec_phase_allows(action_code, arg1, arg2, db_name, trigger_or_view) -> bool:
    """The admissions that hold ONLY while the vec mirror runs (the guard's `vec_phase`; the guard consults this after
    `_fill_embeddings_allows` has said no). MEASURED by Borges on Linux (PR #3900, SQLite 3.53.0, sqlite_vec 0.1.9) as
    the complete set of authorizer calls outside the `vec_nodes` name family made by the mirror's exact sequence
    (SAVEPOINT, DELETE, INSERT, ROLLBACK TO, RELEASE) on `vec0(node_id TEXT PRIMARY KEY, embedding float[384])`:
      - SQLITE_READ on table `x`, column `node_id`: vec0's constructor declares its schema under the name `x`.
      - SQLITE_FUNCTION `max`.
    Nothing else: not column `embedding` of `x` (not requested in the census; this Mac cannot load vec0, so it was
    not measured here and is left out), not another function (`load_extension` included), not a trigger-originated
    statement. A request in the census that is missing here degrades the mirror to `vec_mirrored: false`."""
    if trigger_or_view is not None:
        return False
    if action_code == sqlite3.SQLITE_READ:
        return db_name == "main" and arg1 == "x" and arg2 == "node_id"
    if action_code == sqlite3.SQLITE_FUNCTION:
        return arg2 == "max"
    return False


def _finish_embedding_regen_allows(action_code, arg1, arg2, db_name, trigger_or_view) -> bool:
    """The op-scoped allowlist (pure) for `finish_embedding_regen`. The op never writes `nodes`; it may read the
    columns the restore-flag recount reads, write the FTS family and the restore-state row, and run the DDL the FTS
    rebuild and the restore-state table's lazy create need (all confined to those names). A trigger-originated
    statement is admitted only if NAMED in FINISH_EMBEDDING_REGEN_ADMITTED_TRIGGERS (empty)."""
    if trigger_or_view is not None and trigger_or_view not in FINISH_EMBEDDING_REGEN_ADMITTED_TRIGGERS:
        return False
    if action_code == sqlite3.SQLITE_SELECT:
        return True
    if action_code == sqlite3.SQLITE_READ:
        if _is_fts_table(arg1):  # `count(*)` over a shadow table reports db_name None and an empty column
            return db_name in ("main", None)
        return db_name == "main" and (
            (arg1 == "nodes" and arg2 in FINISH_NODES_READ_COLUMNS)
            or (arg1 == "embedding_restore_state" and arg2 in FINISH_RESTORE_STATE_READ_COLUMNS)
            or arg1 == "sqlite_master")
    if action_code == sqlite3.SQLITE_UPDATE:
        return db_name == "main" and (
            (arg1 == "embedding_restore_state" and arg2 in FINISH_RESTORE_STATE_UPDATE_COLUMNS) or arg1 == "sqlite_master")
    if action_code == sqlite3.SQLITE_INSERT:
        return db_name == "main" and (arg1 in ("embedding_restore_state", "sqlite_master") or _is_fts_table(arg1))
    if action_code == sqlite3.SQLITE_DELETE:
        return db_name == "main" and (arg1 == "sqlite_master" or _is_fts_table(arg1))
    if action_code == sqlite3.SQLITE_CREATE_TABLE:
        return db_name == "main" and (arg1 == "embedding_restore_state" or _is_fts_table(arg1))
    if action_code in (sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE):
        return db_name == "main" and _is_fts_table(arg1)
    if action_code in (sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE):
        return db_name == "main" and arg1 in ("nodes_fts", "nodes_fts_vocab") and arg2 in ("fts5", "fts5vocab")
    if action_code == sqlite3.SQLITE_PRAGMA:
        return (arg1 == "table_info" and arg2 in ("nodes", "nodes_fts")) or arg1 in FINISH_PRAGMAS
    if action_code == sqlite3.SQLITE_FUNCTION:
        return arg2 in FINISH_FUNCTIONS
    if action_code == sqlite3.SQLITE_TRANSACTION:
        return arg1 in ("BEGIN", "COMMIT", "ROLLBACK")
    return False


def _fill_rejection(token: str, detail: str, remedy: str, reason: str = "invalid_params") -> dict:
    """A typed refusal. `reason` is the closed class (`invalid_params` | `bad_dimension` | `non_finite` |
    `model_mismatch`); the detail BEGINS with a stable machine token naming the exact violation, so every violation is
    its own refusal even inside the `invalid_params` class. Nothing the caller sent is echoed."""
    return _df_rejection(reason, f"{token}: {detail}", remedy)


def _fill_embeddings_param_refusal(params) -> "tuple[dict | None, list]":
    """Validate a `fill_embeddings` request over ALL items; -> (refusal | None, items), where items is the list of
    `(node_id, vector_as_list_of_float)` in call order. A refusal means NOTHING may be written. Check order (the first
    violated wins): (1) params is a dict with exactly the keys {model, items}; (2) model is a str; (3) items is a list,
    non-empty, at most FILL_EMBEDDINGS_MAX_ITEMS; then per item in order: (4) a dict with exactly {node_id, vector};
    (5) node_id: a str, non-empty, at most USE_REWARD_NODE_ID_MAX_BYTES UTF-8 bytes (counted with surrogatepass), no
    lone surrogate; (6) vector: a list of exactly FILL_EMBEDDINGS_DIM elements (`bad_dimension`), each an int or a float
    and NOT a bool (`invalid_params`), each finite after conversion to float (`non_finite`: NaN, +-Infinity, or an int
    too large to be a float). `model_mismatch` needs the store's config and is checked by the handler. Ints are
    normalised to float so the stored text is what the embedder's floats serialise to. Touches no connection."""
    p = "fill_embeddings params"
    if not isinstance(params, dict) or set(params) != {"model", "items"}:
        return _fill_rejection("params_shape", f"{p} must be a dict with exactly the keys 'model' and 'items'",
                               "send exactly {model, items}; the request_id travels on the wire, not in params"), []
    if not isinstance(params["model"], str):
        return _fill_rejection("model_not_str", "'model' must be a string", "send the store's embedding model name as a string"), []
    items = params["items"]
    if not isinstance(items, list):
        return _fill_rejection("items_not_list", f"'items' must be a list, got {type(items).__name__}", "send 'items' as a list"), []
    if not items:
        return _fill_rejection("items_empty", "'items' is empty", "send at least one item"), []
    if len(items) > FILL_EMBEDDINGS_MAX_ITEMS:
        return _fill_rejection("items_too_many", f"'items' has more than {FILL_EMBEDDINGS_MAX_ITEMS} entries",
                               f"send at most {FILL_EMBEDDINGS_MAX_ITEMS} items per call; split the batch"), []
    out: list = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"node_id", "vector"}:
            return _fill_rejection("item_shape", f"items[{i}] must be a dict with exactly the keys 'node_id' and 'vector'",
                                   "send each item as {node_id, vector}"), []
        nid, vector = item["node_id"], item["vector"]
        if not isinstance(nid, str):
            return _fill_rejection("node_id_not_str", f"items[{i}].node_id must be a string", "send every node id as a string"), []
        if nid == "":
            return _fill_rejection("node_id_empty", f"items[{i}].node_id is the empty string", "send real node ids"), []
        if len(nid.encode("utf-8", "surrogatepass")) > USE_REWARD_NODE_ID_MAX_BYTES:
            return _fill_rejection("node_id_too_long", f"items[{i}].node_id exceeds {USE_REWARD_NODE_ID_MAX_BYTES} UTF-8 bytes",
                                   f"node ids are capped at {USE_REWARD_NODE_ID_MAX_BYTES} bytes; send the real id"), []
        try:
            nid.encode("utf-8")
        except UnicodeEncodeError:
            return _fill_rejection("node_id_not_utf8", f"items[{i}].node_id contains a lone surrogate, which cannot be bound",
                                   "send node ids that are valid text"), []
        if not isinstance(vector, list):
            return _fill_rejection("vector_not_list", f"items[{i}].vector must be a list of numbers", "send 'vector' as a list"), []
        if len(vector) != FILL_EMBEDDINGS_DIM:
            return _fill_rejection(
                "bad_dimension", f"items[{i}].vector must have exactly {FILL_EMBEDDINGS_DIM} elements",
                f"send vectors of the store's dimension ({FILL_EMBEDDINGS_DIM}); the owner does not trust a client-supplied one",
                reason="bad_dimension"), []
        floats: list = []
        for j, x in enumerate(vector):
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                return _fill_rejection("vector_element_type", f"items[{i}].vector[{j}] must be an int or a float (not a bool)",
                                       "send every element as a finite number"), []
            try:
                f = float(x)
            except OverflowError:
                f = math.inf  # an int too large to be a float: not a finite value
            if not math.isfinite(f):
                return _fill_rejection(
                    "non_finite", f"items[{i}].vector[{j}] is not a finite number (NaN, Infinity or an int too large for a float)",
                    "send finite numbers only; json.dumps emits NaN / Infinity by default, so a client must not pass them",
                    reason="non_finite"), []
            floats.append(f)
        out.append((nid, floats))
    return None, out


def _fill_request_id_refusal(request_id) -> "dict | None":
    """The wire envelope's request_id (the idempotency key): a non-empty str of at most USE_REWARD_REQUEST_ID_MAX_BYTES
    UTF-8 bytes (a uuid4 string fits). A write always carries one on the wire (`engramd_client.write` generates it)."""
    if (not isinstance(request_id, str) or request_id == ""
            or len(request_id.encode("utf-8", "surrogatepass")) > USE_REWARD_REQUEST_ID_MAX_BYTES):
        return _fill_rejection("request_id_invalid", "the write's request_id must be a non-empty string of at most "
                               f"{USE_REWARD_REQUEST_ID_MAX_BYTES} UTF-8 bytes",
                               "send the write through engramd_client.write, which always supplies one")
    return None


def _fill_embeddings_fingerprint(model: str, ids_and_texts) -> bytes:
    """SHA-256 over (model, ids in order, sha256 of each vector's stored text): the idempotency payload identity.
    The dedupe store keeps this 32-byte digest per request_id, never the vectors."""
    body = [model, [[nid, hashlib.sha256(text.encode("utf-8")).hexdigest()] for nid, text in ids_and_texts]]
    return hashlib.sha256(json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("ascii")).digest()


def _finish_embedding_regen_param_refusal(params) -> "dict | None":
    """Validate a `finish_embedding_regen` request: exactly {processed, embedded, skipped, failed, embedder_available},
    the four counts non-negative ints (not bools) and `embedder_available` a real bool (not an int). None = valid.
    Touches no connection."""
    keys = ("processed", "embedded", "skipped", "failed")
    if not isinstance(params, dict) or set(params) != set(keys) | {"embedder_available"}:
        return _fill_rejection("params_shape", "finish_embedding_regen params must be a dict with exactly the keys "
                               "'processed', 'embedded', 'skipped', 'failed' and 'embedder_available'",
                               "send exactly the four counts of the pass and the tool's own embedder probe; there is "
                               "no request_id and no path")
    if not isinstance(params["embedder_available"], bool):
        return _fill_rejection("embedder_available_not_bool", "'embedder_available' must be a bool",
                               "send the result of the tool's own `_embedder.is_available()` as true or false")
    for k in keys:
        v = params[k]
        if isinstance(v, bool) or not isinstance(v, int):
            return _fill_rejection("count_not_int", f"'{k}' must be an integer", "send each count as a non-negative int")
        if v < 0:
            return _fill_rejection("count_negative", f"'{k}' is negative", "send each count as a non-negative int")
    return None


def _method_kind_refusal(method: str, kind) -> "dict | None":
    """Default-deny dispatch gate (8a / 8a-default). None = the method may run."""
    if not isinstance(kind, str) or kind not in METHOD_KINDS:
        return _df_rejection(
            "method_kind_missing",
            f"method {method!r} has no kind: every method must be declared 'read' or 'write'",
            "this is an owner build defect, not a caller error: the method is not client-reachable until it is classified",
        )
    if (kind == "write") != (method in WRITE_METHODS):
        return _df_rejection(
            "method_kind_mismatch",
            f"method {method!r} is declared {kind!r}, which disagrees with the closed write set",
            "owner build defect: update WRITE_METHODS and the kind together",
        )
    return None


def _use_reward_fingerprint(node_ids, action: str) -> bytes:
    """SHA-256 of a canonical encoding of the payload (ids in order, action). The dedupe store keeps this 32-byte
    digest per request_id, never the ids themselves (500 ids x 256 bytes would be ~128 KB per entry)."""
    return hashlib.sha256(json.dumps([list(node_ids), action], separators=(",", ":"), ensure_ascii=True).encode("ascii")).digest()


def _displaced_rejection(exc) -> dict:
    """The one wire form of a confirmed substrate displacement (used by `_dispatch` and by the in-lock re-check)."""
    return {
        "status": "rejected",
        "reason": "substrate_displaced",
        "detail": str(exc),
        "remedy": exc.remedy,
    }


def _use_reward_busy(detail: str, remedy: "str | None" = None) -> dict:
    """The determinate `busy` refusal: the wait budget ran out BEFORE the write got the lock (or the commit could
    take it), so nothing was committed and the op was rolled back. Not recorded for dedupe: a retry runs.
    Shared by every owner write (`remedy` overrides the use_reward wording for an op with no request_id)."""
    return _df_rejection(
        "busy", f"{detail}; nothing was committed",
        remedy or "retry once with the SAME request_id (nothing was written, so the retry runs fresh)")


def _write_abort(conn: sqlite3.Connection, guard: "_UseRewardGuard", op: str) -> None:
    """Roll the op's transaction back (the guard's rollback phase is the handler's own)."""
    guard.allow_rollback = True
    try:
        conn.rollback()
    except sqlite3.Error:
        logging.getLogger("engramd_owner").error("%s rollback failed", op, exc_info=True)


def _write_denied(guard: "_UseRewardGuard", shared: str) -> dict:
    """The one wire form of 'the op-scoped authorizer denied a statement' (the whole op was rolled back)."""
    d = guard.denials[0]
    return _df_rejection(
        "write_denied",
        f"the op-scoped authorizer denied a statement (action code {d[0]}, {d[1]!r}, {d[2]!r}); the whole op was rolled back",
        f"owner build defect: the shared {shared} executed a statement outside the closed allowlist")


def _write_sqlite_error(exc: sqlite3.Error, op: str, busy_remedy: "str | None", failed_remedy: str) -> dict:
    """A sqlite3.Error BEFORE the commit: a locked/busy store is the typed determinate `busy` (carrying `_busy` so the
    handler's retry loop retries it); anything else is `<op>_write_failed`. Both are rolled back already."""
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return {**_use_reward_busy("the store stayed locked by another writer for the whole lock-wait budget", busy_remedy),
                "_busy": True}
    return _df_rejection(f"{op}_write_failed", f"the store write failed ({type(exc).__name__}); the op was rolled back",
                         failed_remedy)


def _use_reward_param_refusal(params) -> "dict | None":
    """Validate a `use_reward` request; None = valid, else the wire rejection. EACH violation has its own
    named reason; nothing the caller sent is echoed. Check order (the first violated wins): (1) shape: not a
    dict, any key outside {node_ids, action, request_id}, or node_ids / action absent; (2) request_id absent;
    (3) node_ids: not a list, empty, longer than USE_REWARD_MAX_IDS, then per element in order: not a str,
    empty, over USE_REWARD_NODE_ID_MAX_BYTES UTF-8 bytes (counted with surrogatepass), a lone surrogate (cannot
    be bound); (4) action: not a str, not in USE_REWARD_ACTIONS; (5) request_id: not a str, empty, over
    USE_REWARD_REQUEST_ID_MAX_BYTES. Touches no connection, so a refused call opens no transaction."""
    if not isinstance(params, dict) or not set(params) <= {"node_ids", "action", "request_id"} or not {"node_ids", "action"} <= set(params):
        return _df_rejection(
            "use_reward_params_shape",
            "use_reward params must be a dict with exactly the keys 'node_ids', 'action' and 'request_id'",
            "send {'node_ids': [<str>, ...], 'action': 'mention', 'request_id': <str>} and no other key (no alpha, no value, no path)",
        )
    if "request_id" not in params:
        return _df_rejection("use_reward_request_id_missing", "'request_id' is missing", "send a client-generated request_id")
    node_ids, action, request_id = params["node_ids"], params["action"], params["request_id"]
    if not isinstance(node_ids, list):
        return _df_rejection("use_reward_node_ids_not_list", f"'node_ids' must be a list, got {type(node_ids).__name__}", "send 'node_ids' as a list of strings")
    if not node_ids:
        return _df_rejection("use_reward_node_ids_empty", "'node_ids' is empty", "send at least one node id")
    if len(node_ids) > USE_REWARD_MAX_IDS:
        return _df_rejection(
            "use_reward_node_ids_too_many", f"'node_ids' has more than {USE_REWARD_MAX_IDS} entries",
            f"send at most {USE_REWARD_MAX_IDS} ids per call; split the batch")
    for i, nid in enumerate(node_ids):
        if not isinstance(nid, str):
            return _df_rejection("use_reward_node_id_not_str", f"node_ids[{i}] must be a string, got {type(nid).__name__}", "send every node id as a string")
        if nid == "":
            return _df_rejection("use_reward_node_id_empty", f"node_ids[{i}] is the empty string", "send real node ids")
        if len(nid.encode("utf-8", "surrogatepass")) > USE_REWARD_NODE_ID_MAX_BYTES:
            return _df_rejection(
                "use_reward_node_id_too_long", f"node_ids[{i}] exceeds {USE_REWARD_NODE_ID_MAX_BYTES} UTF-8 bytes",
                f"node ids are capped at {USE_REWARD_NODE_ID_MAX_BYTES} bytes; send the real id")
        try:
            nid.encode("utf-8")
        except UnicodeEncodeError:
            return _df_rejection(
                "use_reward_node_id_not_utf8", f"node_ids[{i}] contains a lone surrogate, which cannot be bound as a SQL parameter",
                "send node ids that are valid text")
    if not isinstance(action, str):
        return _df_rejection("use_reward_action_not_str", f"'action' must be a string, got {type(action).__name__}", "send 'action' as a string")
    if action not in USE_REWARD_ACTIONS:
        return _df_rejection(
            "use_reward_action_unknown", "'action' is not in the closed action set",
            f"supported actions: {', '.join(sorted(USE_REWARD_ACTIONS))}")
    if not isinstance(request_id, str):
        return _df_rejection("use_reward_request_id_not_str", f"'request_id' must be a string, got {type(request_id).__name__}", "send a client-generated request_id string")
    if request_id == "":
        return _df_rejection("use_reward_request_id_empty", "'request_id' is the empty string", "send a non-empty client-generated request_id")
    if len(request_id.encode("utf-8", "surrogatepass")) > USE_REWARD_REQUEST_ID_MAX_BYTES:
        return _df_rejection(
            "use_reward_request_id_too_long", f"'request_id' exceeds {USE_REWARD_REQUEST_ID_MAX_BYTES} UTF-8 bytes",
            f"request ids are capped at {USE_REWARD_REQUEST_ID_MAX_BYTES} bytes; a uuid4 string fits")
    return None


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
        idle_worker_cap: int = IDLE_WORKER_CAP,
        idle_worker_timeout: float = IDLE_WORKER_TIMEOUT,
        listen_backlog: int = OWNER_LISTEN_BACKLOG,
    ) -> None:
        self.engram_home = _resolve_engram_home(engram_home)
        # The socket's parent directory (self.socket_path.parent) is the
        # `run/` directory this unit hardens -- see start()'s directory
        # setup below (#3194).
        self.socket_path = ec.socket_path_for_home(self.engram_home)
        self.pid_path = ec.pid_path_for_home(self.engram_home)
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
        self.idle_worker_cap = idle_worker_cap
        self.idle_worker_timeout = idle_worker_timeout
        # A plain attribute so a test can use a tiny backlog to hit "backlog full" (#3680).
        self.listen_backlog = listen_backlog
        # Handler hand-off (#3678): `_idle` counts handlers waiting on `_jobs`. Both are
        # changed only under `_handoff_lock`, so a handler retiring on its idle timeout and
        # the accept loop handing it a connection cannot strand that connection.
        self._handoff_lock = threading.Lock()
        self._idle = 0
        self._jobs: "queue.SimpleQueue" = queue.SimpleQueue()
        self._worker_seq = itertools.count(1)
        # Peer-refusal log limiter (#3845): peer uid -> [window_start, suppressed].
        self._peer_refusal_lock = threading.Lock()
        self._peer_refusal_log: dict = {}
        # Refusal drains in flight (#3845); the lock is never held across I/O.
        self._refusal_drains_lock = threading.Lock()
        self._refusal_drains = 0

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
        # Serialises `read` on the ONE shared `_client_read_conn` (U4d). The
        # gate arms a per-call progress_handler on that connection and clears
        # it in a `finally`; two request threads interleaving would clear each
        # other's deadline mid-statement. Deliberately separate from
        # `_owner_lock`: a long snapshot holds that one, and a read must not
        # queue behind it (nor may a read ever take the owner-internal path).
        self._client_read_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._bound_socket = False
        self._accept_thread: threading.Thread | None = None
        # #3194 round 5: bounds concurrent finalize (fsync + os.replace)
        # work across every snapshot() call this owner serves -- shared
        # for this owner's whole lifetime, unlike _FinalizeOutcome (which
        # is per-call). See SNAPSHOT_FINALIZE_SLOTS's own comment and
        # _handle_snapshot()'s docstring for the full protocol.
        self._finalize_slots = threading.BoundedSemaphore(SNAPSHOT_FINALIZE_SLOTS)
        # #3240 item 2: set once engram_walguard.assert_not_displaced has
        # confirmed this owner's -wal/-shm fds are displaced. From then on
        # _cleanup() must NOT .close() either connection (a clean close of
        # a stale holder checkpoints an orphan WAL over data other
        # connections have since written -- see engram_walguard's docs).
        self._displaced = False
        # Method table: every KNOWN method. _dispatch() guards each one not
        # in NO_STORE_METHODS, so registering a method here is all a new
        # store-touching operation needs to be displacement-guarded.
        # Entries late-bind through `self` (lambda) rather than capturing
        # bound methods, so a replaced `self._handle_snapshot` (tests do
        # this) is still what gets called.
        self._methods = {
            "ping": lambda params, deadline: self._handle_ping(params, deadline),
            "snapshot": lambda params, deadline: self._handle_snapshot(params, deadline),
            "read": lambda params, deadline: self._handle_read(params, deadline),
            "df": lambda params, deadline: self._handle_df(params, deadline),
            "chain": lambda params, deadline: self._handle_chain(params, deadline),
            "confidence_by_type": lambda params, deadline: self._handle_confidence_by_type(params, deadline),
            "use_reward": lambda params, deadline: self._handle_use_reward(params, deadline),
            "advance_moment_cursor": lambda params, deadline: self._handle_advance_moment_cursor(params, deadline),
            # #3879. `fill_embeddings` is in WIRE_REQUEST_ID_METHODS: `_dispatch` passes it the envelope's request_id.
            "fill_embeddings": lambda params, deadline, request_id=None: self._handle_fill_embeddings(params, deadline, request_id),
            "finish_embedding_regen": lambda params, deadline: self._handle_finish_embedding_regen(params, deadline),
        }
        # Every method carries a kind (8a). Kept ALONGSIDE `_methods` (tests replace handlers there). A method in
        # `_methods` with no entry here is default-deny at dispatch (8a-default), never treated as a read.
        # `snapshot` is `read`: it copies the store out and never writes into it.
        self._method_kinds = {
            "ping": "read", "snapshot": "read", "read": "read", "df": "read", "chain": "read",
            "confidence_by_type": "read", "use_reward": "write", "advance_moment_cursor": "write",
            "fill_embeddings": "write", "finish_embedding_regen": "write",
        }
        # use_reward's dedupe store: request_id -> {"fp": (ids, action), "response": <first wire dict>}.
        # In-memory and LRU-bounded by USE_REWARD_DEDUPE_CAP; guarded by `_owner_lock`.
        self._use_reward_seen: "OrderedDict[str, dict]" = OrderedDict()
        # fill_embeddings' own dedupe store (a separate namespace: the same request_id string may appear on two
        # different ops): request_id -> {"fp": sha256 digest, "response": <first wire dict>}. Same bound and the same
        # KNOWN LIMIT as use_reward's (dies with the process; the oldest entry is forgotten past the cap).
        self._fill_embeddings_seen: "OrderedDict[str, dict]" = OrderedDict()
        # The vec mirror's capability, probed ONCE per owner connection on the first fill: None = not tried yet,
        # False = this process cannot load vec0 (so every fill acks `vec_mirrored: false`), else the serializer.
        self._fill_vec_state = None

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
        Raises OwnerPythonTooOld if the running Python's sqlite3 lacks
        `Connection.setlimit` (Python < 3.11) -- before anything is created
        (#3349). Raises OwnerSocketPathTooLong if the socket path exceeds
        the AF_UNIX byte limit -- before engram_home or run/ is created
        (#3696; per-platform limit since #3666). Raises OwnerAclNotStripped
        if the run/ ACL read back through the strip's fd still carries any
        entry beyond the base triple, or cannot be read back (#3698; on
        Darwin also if run/ is not mode 0700 after the strip, #3666) --
        before the stale-socket check and the bind. On Darwin, raises
        OwnerHomeTccProtected if engram_home is under ~/Documents,
        ~/Desktop or ~/Downloads -- before anything under it is read or
        created (#3666).
        Raises OwnerPidfileNotRegular if the pidfile path exists and is
        not a regular file (#3768) -- before the stale-socket check and the
        bind in the common case; a path that turns non-regular between that
        check and the pidfile write is refused there, after cleaning up.
        Raises OwnerStoreBehindSchema if knowledge.db does
        not exist, cannot be read, or has `PRAGMA user_version` below
        `engram_core.LATEST_SCHEMA_VERSION` (#3753) -- before anything is
        created or opened read-write, so the refusal creates no knowledge.db
        and no run/. Raises OwnerStorePathNotRegular if the store path is not
        a regular file (FIFO, directory, socket), checked before any open, since
        a read-only open of a FIFO would block (a WAL-mode store may gain -wal/-shm sidecars (no row data) from
        the read-only probe).

        `install_signal_handlers=False` (tests only -- see
        tests/test_engramd_owner.py's owner_running fixture) skips
        registering SIGTERM/SIGINT handlers, since signal.signal() is
        process-global and installing handlers repeatedly across a test
        session would clobber pytest's own signal handling. The cleanup
        path this skips is otherwise identical: the real handler
        (_handle_signal) just calls .stop(), the same method tests call
        directly.
        """
        # The refusals that precede any filesystem mutation (platform support,
        # socket-path length, Darwin TCC home, a non-regular pidfile), in one
        # place so `--preflight` runs exactly these and cannot drift from them
        # (#3709).
        self.check_start_refusals()

        # #3349: refuse on a Python without sqlite3 setlimit (< 3.11)
        # before ANY file, connection, socket or thread exists.
        _require_sqlite_setlimit()

        # R4 (#3196): the dynamic-template escape hatch is resolved FIRST,
        # before any file, connection or socket is touched -- a flag set on
        # a non-dev install raises DynamicTemplatesRefused with nothing
        # to clean up.
        dynamic_templates = rr.resolve_dynamic_flag(
            os.environ, _read_install_tier(self.engram_home)
        )

        # #3753: refuse a store that is behind this code's schema (or absent) BEFORE the
        # first filesystem mutation (the mkdir below) and so before any connect() to it:
        # a read-only probe, so a refusal creates no knowledge.db and no run/ (a WAL-mode
        # store may gain -wal/-shm sidecars (no row data): SQLite makes them even for mode=ro).
        # It sits after every pure refusal above (platform, path length, TCC, setlimit,
        # dynamic flag), which touch nothing, and ahead of the live-owner check below:
        # a second launch against a behind store is told about the store, not the seat.
        _require_store_schema_current(self.store_path)

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
        # Fresh connections, fresh verdict: a previous run's displacement
        # says nothing about the connections opened here.
        self._displaced = False
        self._owner_conn = sqlite3.connect(str(self.store_path), check_same_thread=False)
        self._fill_vec_state = None  # a fresh connection: the vec0 probe (#3879) starts over
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
        # U4c (#3196): the read-template registry, loaded and RE-CLASSIFIED
        # against the live store's schema (R12) BEFORE the socket binds.
        # Any load problem yields an EMPTY registry (everything refused),
        # never a permissive one.
        self._client_read_gate = _ClientReadGate(
            self._client_read_conn, self._load_read_registry(dynamic_templates), home=self.engram_home
        )

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
        self._server.listen(self.listen_backlog)  # OWNER_LISTEN_BACKLOG (#3680)
        self._server.settimeout(self.accept_poll_interval)

        self._write_pidfile()

        if install_signal_handlers:
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)

        self._shutdown.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    @staticmethod
    def _kind_of_mode(mode: int) -> str:
        return describe_file_kind(mode)

    def _pidfile_refusal_text(self, mode: int | None, nlink: int | None = None) -> str:
        if mode is not None and stat.S_ISREG(mode) and nlink is not None and nlink != 1:
            what = f"has {nlink} hard links, not a single regular file"
        else:
            kind = self._kind_of_mode(mode) if mode is not None else "non-regular file"
            what = f"is a {kind}, not a regular file"
        return (
            f"refusing to start: the pidfile path {self.pid_path} {what}; "
            f"nothing was bound. Remove it and start again"
        )

    def _write_pidfile(self) -> None:
        """Write this process's pid to the pidfile (#3768). The write
        sequence (O_NOFOLLOW, O_NONBLOCK, no O_TRUNC, fstat regular-and-
        single-link, only then ftruncate) is the shared
        `engram_regular_io.write_pidfile` (#3773), also used by the collab
        tools; this write path REFUSES symlinks on purpose, unlike the
        read-side liveness check in `engram_pidfile.recorded_live_pid`.

        A refusal (`NotRegularFile`) becomes OwnerPidfileNotRegular after
        releasing what start() already acquired (the socket and both store
        connections; the accept thread does not exist yet). Any other
        OSError propagates unchanged."""
        try:
            write_pidfile(self.pid_path, str(os.getpid()).encode("ascii"))
        except NotRegularFile as exc:
            self._cleanup()
            raise OwnerPidfileNotRegular(self._pidfile_refusal_text(exc.st_mode, exc.nlink)) from exc

    def _load_read_registry(self, dynamic: bool) -> "rr.Registry":
        """Load the committed template list, classifying each template
        against a DEDICATED short-lived read-only connection to the live
        store (the audit installs an authorizer, so it must never run on
        `_owner_conn` or `_client_read_conn`)."""
        # An unfindable file is handed to the loader as a nonexistent path:
        # it logs once and returns the EMPTY registry (R12).
        path = _find_read_templates_path() or (self.engram_home / (READ_TEMPLATES_FILENAME + ".not-found"))
        schema_conn = None
        loaded = False
        try:
            schema_conn = _connect_store_ro(self.store_path)
            registry = rr.load_registry(path, schema_conn, dynamic=dynamic)
            loaded = True
            return registry
        except Exception as exc:  # fail closed: an unloadable registry is an empty one
            logging.getLogger("engramd_owner").warning(
                "read-template registry load failed (%s: %s); registry is EMPTY",
                type(exc).__name__, exc)
            return rr.EMPTY_REGISTRY
        finally:
            if schema_conn is not None and (not dynamic or not loaded):
                schema_conn.close()

    def check_start_refusals(self) -> None:
        """The refusals `start()` makes before it reads or creates anything,
        in `start()`'s order; raises the same exceptions `start()` raises.
        Pure: no mutation, and the only filesystem accesses are the one `lstat`
        of the pidfile in check 4 and the one `stat` of the store in check 5
        (each check below is documented pure).
        `main()`'s `--preflight` calls exactly this, so the
        installer's up-front check (#3709) is the owner's own logic and a
        refusal that would respawn under a supervisor is caught before
        anything is registered.

        1. `_check_platform_support()`.
        2. #3696: an over-long socket path is refused by name BEFORE the first
           filesystem mutation, so a refusal leaves nothing behind. Byte
           length, shared reason text with the client; the limit is
           per-platform (Darwin's 103 since #3666).
        3. #3666 (Darwin only, no-op elsewhere): pure -- no filesystem access --
           and so also before the first read under engram_home and the first
           mutation (mkdir).
        4. #3768: the pidfile path, when it exists, must be a regular file with
           one link: a FIFO, symlink, directory or multiply-hard-linked file is
           refused by name (`OwnerPidfileNotRegular`, exit 4 from `main()`)
           before anything is bound. Here, and not only in `start()`, because
           launchd has no per-exit-code retry exclusion: the installer's
           `--preflight` must refuse what would otherwise respawn every
           throttle interval forever (#3709). One `lstat`, which does not
           follow, so a symlink counts; a missing path (or home) is the normal
           fresh start and passes, which is what makes this safe before mkdir.
           The race between this check and the write is closed in
           `_write_pidfile`, which keeps its own checks.
        5. #3785: the store path (<ENGRAM_HOME>/knowledge.db), when it exists,
           must be a regular file: a FIFO, directory, socket or device is
           refused by name (`OwnerStorePathNotRegular`, exit 4 from `main()`),
           for the same no-per-exit-code-retry reason as check 4. Runs even
           when the pidfile is absent. One `stat` (follows symlinks, does not
           block on a FIFO) through `_refuse_non_regular_store`, the very
           helper `_connect_store_ro` uses; a missing store or home passes.

        The installer runs `--preflight` with the INSTALLER'S environment, not
        launchd's (#3780). That is safe today because every Darwin-arm
        predicate here is stdlib or uses an absolute path. A future
        PATH-dependent predicate would diverge silently: the preflight would
        pass while the launchd-run owner refused with exit 1 every 60 s
        (ThrottleInterval). So the predicates in this method must stay
        independent of the environment."""
        self._check_platform_support()
        too_long = ec.socket_path_too_long_reason(self.socket_path)
        if too_long is not None:
            raise OwnerSocketPathTooLong(too_long)
        self._check_darwin_home_not_tcc_protected()
        try:
            pid_stat = os.lstat(self.pid_path)
        except FileNotFoundError:
            pid_stat = None  # fresh start: fall through to check 5, do not return
        # A hard-linked regular file is refused too: writing it would rewrite
        # another name's content.
        if pid_stat is not None and (not stat.S_ISREG(pid_stat.st_mode) or pid_stat.st_nlink > 1):
            raise OwnerPidfileNotRegular(
                self._pidfile_refusal_text(pid_stat.st_mode, pid_stat.st_nlink))
        _refuse_non_regular_store(self.store_path)

    def _check_platform_support(self) -> None:
        """Refuse to start rather than silently degrade the permission
        layer on a platform/toolchain this build cannot secure (#3194,
        design doc §2.2's posture -- "the fallback on an unrecognised
        platform is to REFUSE TO START, never to skip the check" --
        extended here to the ACL-strip requirement of §2.1). This build
        implements the Linux arm (#3194) and the Darwin arm (#3666), chosen
        by `_owner_arm()` (#3708). Any other platform, a Linux seat missing
        `setfacl`, `getfacl` or socket.SO_PEERCRED, or a Darwin seat missing
        socket.LOCAL_PEERCRED, `/bin/chmod` or `/dev/fd` refuses here rather
        than starting with a silently-unenforced permission layer.

        The checks live in the module-level
        `platform_unsupported_reason()` (#3292) -- the one definition the
        test suite's `requires_linux_owner` marker also keys on, so the
        refusal and the skip cannot disagree."""
        reason = platform_unsupported_reason()
        if reason is not None:
            raise OwnerPermissionUnsupported(reason)

    def _check_darwin_home_not_tcc_protected(self) -> None:
        """Darwin only (#3666, design doc §2.5): refuse an ENGRAM_HOME under
        ~/Documents, ~/Desktop or ~/Downloads. A heuristic prefix list, NOT a
        TCC query (see DARWIN_TCC_PROTECTED_HOME_FOLDERS): probing those
        folders would itself raise a consent dialog. Compared lexically and
        case-insensitively (the default volume is case-insensitive), with no
        filesystem access and no symlink resolution -- resolving a path inside
        a protected folder is exactly the access this check exists to avoid."""
        if _owner_arm() != "darwin":
            return
        home_parts = [p.casefold() for p in Path.home().parts]
        parts = [p.casefold() for p in self.engram_home.parts]
        for folder in DARWIN_TCC_PROTECTED_HOME_FOLDERS:
            prefix = home_parts + [folder.casefold()]
            if parts[: len(prefix)] == prefix:
                raise OwnerHomeTccProtected(
                    f"refusing to start: ENGRAM_HOME {self.engram_home} is "
                    f"under ~/{folder}, a folder macOS TCC protects by "
                    f"default; an owner started without a user at the screen "
                    f"would block forever on a consent dialog nobody can "
                    f"answer. Grant the owner's python Full Disk Access, or "
                    f"move ENGRAM_HOME out of ~/Documents, ~/Desktop and "
                    f"~/Downloads. (This is a heuristic folder list, not a "
                    f"TCC query -- probing would raise the dialog.)"
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
            if _owner_arm() == "darwin":
                # #3666: the Darwin strip + fd-based read-back; the finally
                # below still closes fd. The Linux setfacl arm is untouched.
                self._strip_run_dir_acl_darwin(fd, run_dir)
                return
            try:
                proc = subprocess.run(
                    ["setfacl", "-b", f"/proc/self/fd/{fd}"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=30,
                    pass_fds=[fd],
                )
            except subprocess.TimeoutExpired as exc:
                # Parity with the Darwin `chmod -N` refusal (#3708): a hung
                # strip is a named refusal, not a bare traceback.
                raise OwnerPermissionUnsupported(
                    f"refusing to start: `setfacl -b` on {run_dir} did not "
                    f"finish within {exc.timeout}s"
                ) from exc
            if proc.returncode != 0:
                raise OwnerPermissionUnsupported(
                    f"refusing to start: `setfacl -b` on {run_dir} (via "
                    f"its open fd, never by re-resolving the path) "
                    f"failed (exit {proc.returncode}): {proc.stderr.strip()}"
                )
            # #3698: trust the strip only after reading it back, through the
            # SAME fd (never the path), before the fd is closed.
            leftovers = _acl_leftovers(_read_acl_entries(fd, run_dir))
            if leftovers:
                shown = leftovers[:_ACL_LEFTOVER_LIST_MAX]
                rest = len(leftovers) - len(shown)
                more = f" (and {rest} more)" if rest else ""
                raise OwnerAclNotStripped(
                    f"refusing to start: {run_dir} still carries leftover ACL "
                    f"entries after `setfacl -b` (read back via its open fd): "
                    f"{'; '.join(shown)}{more}"
                )
        finally:
            os.close(fd)

    def _strip_run_dir_acl_darwin(self, fd: int, run_dir: Path) -> None:
        """Darwin arm of the `run/` ACL strip (#3666, design doc §2.1 + §2.5).

        `/bin/chmod -N /dev/fd/<fd>` with the fd passed via `pass_fds`: the
        strip acts on the fd's TARGET, not on whatever the path names now
        (swap control measured in §2.5). Then the ACL is READ BACK through the
        same fd -- never the path, so a swap between strip and read-back
        cannot show a clean read for a different directory -- and any entry
        left over refuses the start. `chmod -N` exiting 0 is not evidence the
        strip worked; nothing on the Linux arm re-reads the ACL either (#3698)."""
        try:
            proc = subprocess.run(
                [DARWIN_CHMOD, "-N", f"{DARWIN_DEV_FD}/{fd}"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                pass_fds=[fd],
            )
        except subprocess.TimeoutExpired as exc:
            # A hung strip is a refusal with a named reason, not a bare
            # traceback out of _harden_run_dir (still fail-closed either way).
            raise OwnerPermissionUnsupported(
                f"refusing to start: `chmod -N` on {run_dir} did not finish "
                f"within {exc.timeout}s"
            ) from exc
        if proc.returncode != 0:
            raise OwnerPermissionUnsupported(
                f"refusing to start: `chmod -N` on {run_dir} (via its open "
                f"fd, never by re-resolving the path) failed (exit "
                f"{proc.returncode}): {proc.stderr.strip()}"
            )
        leftover = _darwin_acl_entries_via_fd(fd)
        if leftover:
            raise OwnerAclNotStripped(
                f"refusing to start: ACL read-back on {run_dir} (through the "
                f"same open fd) still shows {len(leftover)} entr"
                f"{'y' if len(leftover) == 1 else 'ies'} after `chmod -N` "
                f"reported success: {'; '.join(leftover)}"
            )
        # The security argument rests on the MODE as well as the ACL: read it
        # back through the same fd too. Measured on macOS 26.6.2: `chmod -N`
        # leaves mode bits alone (0700 stays 0700, 0755 stays 0755), so this
        # holds today; it refuses if a strip ever widens the mode.
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != RUN_DIR_MODE:
            raise OwnerAclNotStripped(
                f"refusing to start: {run_dir} reads mode {oct(mode)} after "
                f"the ACL strip, expected {oct(RUN_DIR_MODE)}"
            )

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
        # Release every idle handler (#3678); a busy one exits after its request, when it
        # sees _shutdown in _next_connection(). The tokens go in under the same lock that
        # zeroes `_idle`: a handler whose idle timeout fires in between then finds a token
        # waiting and takes it, rather than decrementing `_idle` below zero and leaving a
        # stray token behind.
        with self._handoff_lock:
            idle, self._idle = self._idle, 0
            for _ in range(idle):
                self._jobs.put(None)
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
        #
        # #3768: this read decides whether to UNLINK, so it refuses symlinks
        # and non-regular files (O_NOFOLLOW, O_NONBLOCK, then fstat): a FIFO
        # must not hang it and must not be unlinked, a symlink is not ours.
        still_ours = False
        not_regular_kind = None
        hard_links = None
        try:
            fd = os.open(self.pid_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                not_regular_kind = "symlink"
            else:
                # No pidfile, or unreadable -- nothing to protect, proceed
                # with removing it (matches the surface daemon's identical
                # fallback) -- unless the path is something other than a
                # regular file that open() merely failed on (e.g. a
                # socket-type file: ENXIO on Linux, EOPNOTSUPP on macOS),
                # which is not ours to unlink.
                still_ours = True
                try:
                    lmode = os.lstat(self.pid_path).st_mode
                except OSError:
                    pass
                else:
                    if not stat.S_ISREG(lmode):
                        still_ours = False
                        not_regular_kind = self._kind_of_mode(lmode)
        else:
            try:
                fst = os.fstat(fd)
                mode = fst.st_mode
                if not stat.S_ISREG(mode):
                    not_regular_kind = self._kind_of_mode(mode)
                elif fst.st_nlink > 1:
                    # hard-linked: another name shares this inode, not ours
                    hard_links = fst.st_nlink
                else:
                    try:
                        with os.fdopen(fd, closefd=False) as f:
                            still_ours = int(f.read().strip()) == os.getpid()
                    except (OSError, ValueError):
                        # garbage/unreadable content: same fallback as above
                        still_ours = True
            finally:
                os.close(fd)
        if not_regular_kind is not None:
            logging.getLogger("engramd_owner").warning(
                "pidfile %s is a %s, not a regular file; leaving it alone",
                self.pid_path, not_regular_kind)
        if hard_links is not None:
            logging.getLogger("engramd_owner").warning(
                "pidfile %s has %d hard links; leaving it alone",
                self.pid_path, hard_links)
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
            if not self._displaced and self._owner_conn is not None:
                # #3240 item 3: an owner displaced but never asked for a
                # store operation must not take the clean-close path just
                # because no request tripped the guard. Check once here.
                try:
                    engram_walguard.assert_not_displaced(str(self.store_path))
                except engram_walguard.SubstrateDisplaced as exc:
                    self._displaced = True
                    print(
                        f"[engramd] substrate displaced detected at shutdown: {exc}",
                        file=sys.stderr,
                    )
            if self._displaced:
                # #3240 item 2: displaced substrate -- NEVER close. Park the
                # connections in the module-level keepalive list: dropping
                # the references would let GC (or interpreter finalization)
                # close them, and the last close checkpoints an orphan WAL
                # over newer data. main() then ends the process with
                # os._exit(EXIT_DISPLACED) so finalization cannot run.
                for conn in (self._owner_conn, self._client_read_conn):
                    if conn is not None:
                        _DISPLACED_CONNECTIONS_KEEPALIVE.append(conn)
                self._owner_conn = None
                self._client_read_conn = None
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
        """Delegates to engram_pidfile.recorded_pid_is_alive() -- the single
        source shared with engram-surface-daemon.py (#3240 item 1): a
        recorded pid counts as alive only if that process could have
        written the pidfile (not merely any live process that reused the
        pid). False for a missing/unreadable/garbage pidfile."""
        return engram_pidfile.recorded_pid_is_alive(
            self.pid_path, program_marker=_PIDFILE_PROGRAM_MARKER
        )

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
            self._dispatch_connection(conn)

    def _dispatch_connection(self, conn: socket.socket) -> None:
        """Give `conn` to an idle handler if there is one, else start a new handler.
        Never queues a connection behind a busy handler (#3678)."""
        with self._handoff_lock:
            if self._idle > 0:
                self._idle -= 1
                self._jobs.put(conn)
                return
        threading.Thread(
            target=self._worker, args=(conn,),
            name=f"{WORKER_THREAD_PREFIX}-{next(self._worker_seq)}", daemon=True,
        ).start()

    def _worker(self, conn: "socket.socket | None") -> None:
        """Serve `conn`, then wait for the next connection; exit on stop() or when idle
        past `idle_worker_timeout` while more than `idle_worker_cap` handlers are idle."""
        while conn is not None:
            try:
                self._handle_connection(conn)
            except Exception:  # _handle_connection maps its own failures; keep the handler alive
                logging.getLogger("engramd_owner").error(
                    "unexpected exception in a connection handler", exc_info=True)
            conn = self._next_connection()

    def _next_connection(self) -> "socket.socket | None":
        with self._handoff_lock:
            if self._shutdown.is_set():
                return None
            self._idle += 1
        while True:
            try:
                return self._jobs.get(timeout=self.idle_worker_timeout)  # None = stop()
            except queue.Empty:
                pass
            with self._handoff_lock:
                if not self._jobs.empty():
                    # Handed a connection between the timeout and taking the lock: the
                    # accept loop already counted this handler as busy.
                    return self._jobs.get_nowait()
                if self._shutdown.is_set() or self._idle > self.idle_worker_cap:
                    self._idle -= 1
                    return None

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
        design doc §2.2/§5 test 2).

        The arm is `_owner_arm()` (#3708), not the presence of an attribute:
        darwin runs the LOCAL_PEERCRED check (#3666); linux runs this
        SO_PEERCRED check and REFUSES if `socket.SO_PEERCRED` is absent --
        never falling through to the Darwin check; any other platform
        REFUSES. The check never no-ops."""
        arm = _owner_arm()
        if arm == "darwin":
            return self._check_peer_credentials_darwin(conn)
        if arm != "linux":
            raise OwnerPermissionUnsupported(
                f"refusing to check peer credentials: no peer-credential "
                f"check exists for platform.system() == {platform.system()!r}"
            )
        if not hasattr(socket, "SO_PEERCRED"):
            raise OwnerPermissionUnsupported(
                "refusing to check peer credentials: this Linux Python/OS "
                "build has no socket.SO_PEERCRED"
            )
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

    def _refused_peer_uids(self, conn: socket.socket) -> tuple:
        """(peer_uid, owner_uid) for the refusal log (#3845). Re-reads the kernel
        credentials, so `_check_peer_credentials` keeps returning only the wire dict.
        peer_uid is None if the re-read fails; this never raises."""
        own_uid = os.getuid()
        try:
            if _owner_arm() == "darwin":
                raw = conn.getsockopt(SOL_LOCAL, socket.LOCAL_PEERCRED, DARWIN_PEERCRED_STRUCT_SIZE)
                return struct.unpack_from(DARWIN_PEERCRED_STRUCT_FORMAT, raw)[1], own_uid
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEERCRED_STRUCT_SIZE)
            return struct.unpack_from(PEERCRED_STRUCT_FORMAT, raw)[1], own_uid
        except Exception:
            return None, own_uid

    def _log_peer_refusal(self, conn: socket.socket, rejection: dict) -> None:
        """One bounded WARNING per refusal window per peer uid (#3845). Names the peer
        uid, the owner uid and the reason; never any request bytes. The window is lazy
        (no timer): the first refusal after a window expires carries "N suppressed in
        the previous window". A burst followed by silence therefore never prints its
        count."""
        peer_uid, own_uid = self._refused_peer_uids(conn)
        reason = rejection.get("reason")
        now = time.monotonic()
        with self._peer_refusal_lock:
            entry = self._peer_refusal_log.get(peer_uid)
            if entry is not None and now - entry[0] < PEER_REFUSAL_LOG_WINDOW:
                entry[1] += 1
                return
            carried = entry[1] if entry is not None else 0
            self._peer_refusal_log.pop(peer_uid, None)  # re-insert last: order = window start
            while len(self._peer_refusal_log) >= PEER_REFUSAL_LOG_MAX_UIDS:
                del self._peer_refusal_log[next(iter(self._peer_refusal_log))]
            self._peer_refusal_log[peer_uid] = [now, 0]
        tail = f"; {carried} suppressed in the previous window" if carried else ""
        try:
            logging.getLogger("engramd_owner").warning(
                "peer refused: uid %s (owner uid %s), reason %s; further refusals from uid %s "
                "suppressed for %ds%s",
                peer_uid, own_uid, reason, peer_uid, PEER_REFUSAL_LOG_WINDOW, tail)
        except Exception:
            pass  # a raising logging.Filter must never cost the peer its typed rejection or the drain

    def _take_refusal_drain_slot(self) -> bool:
        """Non-blocking: True if a drain slot was free (and is now taken)."""
        with self._refusal_drains_lock:
            if self._refusal_drains >= MAX_REFUSAL_DRAINS:
                return False
            self._refusal_drains += 1
            return True

    def _release_refusal_drain_slot(self) -> None:
        with self._refusal_drains_lock:
            self._refusal_drains -= 1

    def _drain_refused_request(self, conn: socket.socket, deadline: float) -> None:
        """After the typed rejection: shutdown(SHUT_WR), then read and DISCARD the
        refused peer's request until EOF, the drain budget, or max_request_bytes (#3845).
        Never parses what it reads."""
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            return
        budget = min(REFUSAL_DRAIN_BUDGET, deadline - time.monotonic())
        if budget <= 0:
            return
        end = time.monotonic() + budget
        consumed = 0
        while consumed < self.max_request_bytes:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            conn.settimeout(remaining)
            try:
                chunk = conn.recv(min(65536, self.max_request_bytes - consumed))
            except OSError:
                return  # includes socket.timeout
            if not chunk:
                return  # EOF
            consumed += len(chunk)

    def _check_peer_credentials_darwin(self, conn: socket.socket) -> dict | None:
        """Darwin arm of the peer check (#3666, design doc §2.2 + §2.5): same
        return contract as `_check_peer_credentials()` -- a §2.3 rejection wire
        dict for an unauthorised uid, None to proceed.

        `getsockopt(SOL_LOCAL=0, LOCAL_PEERCRED, 76)`, parsed with
        `struct.unpack_from("@IIh16I")` -- NEVER a slice, so a short buffer
        (measured: an 8-byte request returns 8 bytes with no error; the wrong
        LEVEL returns 4 zero bytes with no error) raises `struct.error`
        instead of decoding to uid 0. Then the buffer must be EXACTLY 76 bytes
        and `cr_version` must be 0, else `OwnerPeerCredentialsMalformed`
        (fail closed; `cr_version == 0` is §2.5's added requirement)."""
        if not hasattr(socket, "LOCAL_PEERCRED"):
            raise OwnerPermissionUnsupported(
                "refusing to check peer credentials: this platform has "
                "neither socket.SO_PEERCRED nor socket.LOCAL_PEERCRED"
            )
        raw = conn.getsockopt(SOL_LOCAL, socket.LOCAL_PEERCRED, DARWIN_PEERCRED_STRUCT_SIZE)
        cr_version, peer_uid, _ngroups, *_groups = struct.unpack_from(
            DARWIN_PEERCRED_STRUCT_FORMAT, raw
        )
        if len(raw) != DARWIN_PEERCRED_STRUCT_SIZE:
            raise OwnerPeerCredentialsMalformed(
                f"LOCAL_PEERCRED returned {len(raw)} bytes, expected exactly "
                f"{DARWIN_PEERCRED_STRUCT_SIZE}"
            )
        if cr_version != DARWIN_XUCRED_VERSION:
            raise OwnerPeerCredentialsMalformed(
                f"LOCAL_PEERCRED returned xucred cr_version {cr_version}, "
                f"this parser understands only {DARWIN_XUCRED_VERSION}"
            )
        own_uid = os.getuid()
        # uid mismatch, then §2.2's uid-0 clause (defence in depth: it changes
        # no outcome given the mismatch rule, see _check_peer_credentials()).
        if peer_uid != own_uid or (peer_uid == 0 and own_uid != 0):
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
                self._log_peer_refusal(conn, rejection)
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
                    else:
                        if self._take_refusal_drain_slot():
                            try:
                                self._drain_refused_request(conn, deadline)
                            finally:
                                self._release_refusal_drain_slot()
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
            try:
                response = self._dispatch(request, deadline)
            except Exception as exc:
                # Catch-all for an UNEXPECTED exception in any method (the
                # deadline/limit aborts and every other deliberate refusal are
                # mapped INSIDE the handlers and returned, so they never reach
                # here). Without it the thread died silently, the client saw a
                # closed socket, read it as Unavailable and told the agent to
                # restart a healthy owner. `Exception`, not BaseException:
                # SystemExit / KeyboardInterrupt still propagate.
                logging.getLogger("engramd_owner").error(
                    "unexpected exception in method %r", request.get("method"), exc_info=True)
                response = {
                    "status": "rejected",
                    "reason": "internal_error",
                    "detail": f"{type(exc).__name__}: {exc}"[:200],
                    "remedy": "owner bug (an unexpected exception inside the owner, not an outage): see the owner log",
                }
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
            # A `read` reply arrives pre-rendered from its size-checked fragments
            # (#3678 cut 2); everything else is serialized here as before.
            text = getattr(response, "wire_text", None)
            if text is None:
                text = json.dumps(response)
            try:
                conn.sendall((text + "\n").encode("utf-8"))
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

        handler = self._methods.get(method) if isinstance(method, str) else None
        if handler is not None:
            kind_refusal = _method_kind_refusal(method, self._method_kinds.get(method))
            if kind_refusal is not None:
                return kind_refusal
            if method not in NO_STORE_METHODS:
                # #3240 item 2: the owner's own displacement self-check,
                # for EVERY known store-touching method (guarded by
                # default; unknown methods never reach here, so they cost
                # no /proc scan). detection_unavailable is NOT
                # displacement -- assert_not_displaced returns quietly.
                try:
                    engram_walguard.assert_not_displaced(str(self.store_path))
                except engram_walguard.SubstrateDisplaced as exc:
                    self._displaced = True
                    return _displaced_rejection(exc)
            if method in WIRE_REQUEST_ID_METHODS:
                # #3879: a write whose params cannot carry a request_id gets the envelope's (the idempotency key).
                return handler(params, deadline, request.get("request_id"))
            return handler(params, deadline)

        return {
            "status": "rejected",
            "reason": "unknown_operation",
            "detail": f"no such operation: {method!r}",
            "remedy": (
                "check the method name against the owner's supported "
                f"operations (this build: {', '.join(self._methods)})"
            ),
        }

    def _handle_ping(self, params: dict, deadline: float) -> dict:
        return {
            "status": "ok",
            "result": {
                "contract_version": self.contract_version,
                "store_path": str(self.store_path),
            },
        }

    # -- the owner's first write (#3822) ---------------------------------------

    def _use_reward_memory_config(self) -> dict:
        """The `memory` block of THIS owner's own `ENGRAM_HOME/config.json`, read FRESH on every call (never cached
        at start: `current_turn` moves at sleep). PARITY with the direct path (the hook reads the same file through
        the same function): an absent file or one without a `memory` block yields the defaults (turn 0, not an
        error); a torn / unparseable file, a FIFO or a directory raises, which the shared `use_reward` reports as
        `restamp_error` with the bump standing."""
        return er.read_memory_config(self.engram_home / "config.json")

    def _use_reward_read_utility(self, conn: sqlite3.Connection, ids) -> dict:
        """{id: utility_score or 0.0} for each of `ids` that has a row (NULL counts as 0.0, as the Q-update does)."""
        out: dict = {}
        for nid in ids:
            r = conn.execute("SELECT utility_score FROM nodes WHERE id = ?", (nid,)).fetchone()
            if r is not None:
                out[nid] = r[0] if r[0] is not None else 0.0
        return out

    def _use_reward_read_rows(self, conn: sqlite3.Connection, ids) -> dict:
        """{id: {utility_score, importance_score, recall_turn}} for each of `ids` that has a row, in the order
        given. Used twice per op: inside the transaction (the EXPECTED values) and again after COMMIT (the ACTUAL
        values, SS5.4 read-back). Both reads run on the owner connection under the op's allowlist."""
        rows: dict = {}
        for nid in ids:
            r = conn.execute(
                "SELECT utility_score, importance_score, recall_turn FROM nodes WHERE id = ?", (nid,)
            ).fetchone()
            if r is not None:
                rows[nid] = {"utility_score": r[0], "importance_score": r[1], "recall_turn": r[2]}
        return rows

    def _handle_use_reward(self, params: dict, deadline: float) -> dict:
        """The typed `use_reward` write (kind `write`). Wire: params exactly {node_ids, action, request_id}.

        Order: (1) validate, touching no connection (`_use_reward_param_refusal`); (2) take `_owner_lock`, waiting
        at most OWNER_WRITE_LOCK_WAIT (`busy` otherwise); the SQLite busy wait is the REMAINING part of that one budget; (3) UNDER THAT LOCK, dedupe check,
        execution and recording, so two concurrent calls with one request_id cannot both bump; (4) install the
        op-scoped restrictive authorizer on `_owner_conn` BEFORE the transaction opens and restore the permissive
        placeholder in a `finally` on every exit path; (5) run the SHARED `engram_restamp.use_reward` (bump THEN
        restamp, the restamp in its own SAVEPOINT) in ONE transaction; read the touched columns (EXPECTED); COMMIT;
        re-read them from the committed store (ACTUAL); `ok` only if every compare matches exactly.

        Outcomes: `ok` (ack fields: updated, bumped, restamped, restamp_error, rows, duplicate); `write_denied`
        (the authorizer denied a statement: the whole op is rolled back, no partial bump -- including a denial the
        shared function's restamp guard would otherwise absorb as `restamp_error`); `busy` (the one wait budget ran out: nothing committed, rolled back, determinate) /
        `use_reward_write_failed` (any other sqlite3.Error, rolled back); `readback_mismatch` (COMMITTED but unverified:
        the commit cannot be undone at that point, so it is reported loudly, naming the id(s), and recorded so a
        replay re-reports instead of re-bumping). A bump committed with a failed restamp is an `ok` ack with
        `restamp_error` set, never a silent success. Rejections before the commit are not recorded in the dedupe
        store, so a retry of the same request_id runs."""
        refusal = _use_reward_param_refusal(params)
        if refusal is not None:
            return refusal
        node_ids: list = list(params["node_ids"])
        action: str = params["action"]
        request_id: str = params["request_id"]
        fingerprint = _use_reward_fingerprint(node_ids, action)

        def record(resp: dict) -> None:
            """Remember a COMMITTED outcome (ok or committed_unverified) so a same-id retry replays it."""
            self._use_reward_seen[request_id] = {"fp": fingerprint, "response": copy.deepcopy(resp)}
            self._use_reward_seen.move_to_end(request_id)
            while len(self._use_reward_seen) > USE_REWARD_DEDUPE_CAP:
                self._use_reward_seen.popitem(last=False)

        def dedupe_check() -> "dict | None":
            prior = self._use_reward_seen.get(request_id)
            if prior is None:
                return None
            self._use_reward_seen.move_to_end(request_id)
            if prior["fp"] != fingerprint:
                return _df_rejection(
                    "use_reward_request_id_reused", "this request_id was already used for a different payload",
                    "generate a fresh request_id per distinct operation; reuse one only to retry the SAME call")
            replay = copy.deepcopy(prior["response"])
            if replay.get("status") == "ok":
                replay["result"]["duplicate"] = True
            return replay

        return self._owner_write(
            "use_reward", deadline, _UseRewardGuard,
            lambda conn, guard: self._use_reward_execute(conn, guard, node_ids, action, record),
            intercept=dedupe_check, after=record)

    def _owner_write(self, op: str, deadline: float, make_guard, attempt, *, intercept=None, after=None,
                     busy_remedy: "str | None" = None) -> dict:
        """The shared scaffold of every owner write (`WRITE_METHODS`); `_handle_use_reward` and
        `_handle_advance_moment_cursor` both run through it (one definition of the wait budget, the lock, the
        displacement re-check, the open-transaction refusal, the busy-timeout switch and the authorizer install /
        restore). `op` names the op in the typed refusals (`<op>_owner_stopping`, `<op>_txn_open`).

        Order: take `_owner_lock`, waiting at most OWNER_WRITE_LOCK_WAIT (`busy` otherwise; the SQLite busy wait is
        the REMAINING part of that one budget); UNDER THAT LOCK, the optional `intercept()` (use_reward's dedupe
        check: a non-None answer is returned as is), the in-lock displacement re-check, the open-transaction
        refusal; then install the op-scoped restrictive authorizer (`make_guard()`) BEFORE the transaction opens
        and `attempt(conn, guard)` it, retrying a `_busy` attempt every USE_REWARD_BUSY_RETRY_SLEEP until the
        budget is spent; authorizer and busy_timeout are restored in a `finally` on every exit path. A response
        carrying `_committed: True` is one whose COMMIT happened: the marker is stripped and `after(response)`
        runs under the lock (use_reward records it for dedupe)."""
        wait_started = time.monotonic()
        budget = max(0.0, min(OWNER_WRITE_LOCK_WAIT, deadline - wait_started))
        if not self._owner_lock.acquire(timeout=budget):
            return _use_reward_busy("the owner's write lock was not free within the lock-wait budget", busy_remedy)
        try:
            conn = self._owner_conn
            if conn is None or self._displaced:
                return _df_rejection(f"{op}_owner_stopping", "the owner is stopping or its store handle is gone",
                                     "retry once the owner is running again")
            if intercept is not None:
                answered = intercept()
                if answered is not None:
                    return answered

            # The dispatch-time displacement check can be stale by the whole lock wait: re-check here, under the lock,
            # before anything is executed, and handle it exactly as `_dispatch` does.
            try:
                engram_walguard.assert_not_displaced(str(self.store_path))
            except engram_walguard.SubstrateDisplaced as exc:
                self._displaced = True
                return _displaced_rejection(exc)
            # Never start on top of someone else's open transaction on the shared connection (every user holds
            # `_owner_lock`, so this means a leaked transaction): nothing is executed, and it is left alone.
            if conn.in_transaction:
                return _df_rejection(
                    f"{op}_txn_open", "the owner's store connection already has a transaction open",
                    "owner defect: a previous user of the connection left a transaction open; see the owner log")
            # SQLite's OWN busy wait is switched OFF for the op (busy_timeout = 0) and the wait is done here, as a
            # retry loop against the one budget. MEASURED (macOS, SQLite 3.53.4, Python 3.12): SQLite's busy
            # handler overshoots its timeout badly (busy_timeout = 1000 ms waited 2.4-2.6 s; 300 ms waited ~1.2 s;
            # the same for connect(timeout=)), so it cannot enforce a TOTAL bound that a client deadline sits on.
            # `PRAGMA busy_timeout` is the only way to change it after connect (python's sqlite3 has no setter) and
            # the op authorizer denies PRAGMA, so it runs HERE, while the permissive authorizer is still installed;
            # the previous value is restored in the same `finally` as the authorizer, after the permissive one is
            # back. A busy attempt rolled back completely, so retrying the whole transaction is safe.
            give_up_at = wait_started + budget
            prev_busy_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

            # ONE try/finally opens here, before the first thing that changes the connection, and restores BOTH
            # changes on every exit (authorizer first, then the PRAGMA), each in its own inner try so one failing
            # cannot skip the other.
            guard = None
            try:
                conn.execute("PRAGMA busy_timeout = 0")
                guard = make_guard()
                conn.set_authorizer(guard.authorizer)
                while True:
                    guard.reset()
                    response = attempt(conn, guard)
                    if not response.get("_busy"):
                        break
                    left = give_up_at - time.monotonic()
                    if left <= 0:
                        break
                    time.sleep(min(USE_REWARD_BUSY_RETRY_SLEEP, left))
                response.pop("_busy", None)
            finally:
                try:
                    if conn.in_transaction:  # belt and braces: on ANY exit the connection is left transaction-free
                        if guard is not None:
                            guard.allow_rollback = True
                        conn.rollback()
                except Exception:
                    logging.getLogger("engramd_owner").error("%s final rollback failed", op, exc_info=True)
                try:
                    conn.set_authorizer(_owner_internal_authorizer)
                except Exception:
                    logging.getLogger("engramd_owner").error("%s authorizer restore failed", op, exc_info=True)
                try:
                    if guard is not None:
                        guard.allow_busy_pragma = True  # only matters if the permissive restore above failed
                    conn.execute(f"PRAGMA busy_timeout = {int(prev_busy_ms)}")
                except Exception:
                    logging.getLogger("engramd_owner").error("%s busy_timeout restore failed", op, exc_info=True)
            if response.pop("_committed", False) and after is not None:
                after(response)
            return response
        finally:
            self._owner_lock.release()

    def _use_reward_execute(self, conn: sqlite3.Connection, guard: "_UseRewardGuard", node_ids: list, action: str, record) -> dict:
        """The transaction body of `_handle_use_reward`; runs with the op allowlist installed and `_owner_lock` held.
        A response carrying `_committed: True` is one whose COMMIT happened (the caller records it for dedupe and
        strips the marker)."""
        def abort() -> None:
            _write_abort(conn, guard, "use_reward")

        def denied() -> dict:
            return _write_denied(guard, "use_reward")

        unique_ids = list(dict.fromkeys(node_ids))
        try:
            # The write transaction is opened up front, so the pre-op reads below are INSIDE it and a competing
            # direct writer is met here (busy) rather than half way through.
            conn.execute("BEGIN IMMEDIATE")
            # Independent of the shared function: each id's utility BEFORE the call (NULL = 0.0). Used only by the
            # monotone invariant after the commit; it does not recompute the Q-update.
            pre = self._use_reward_read_utility(conn, unique_ids)
            result = er.use_reward(conn, node_ids, action, memory_config=self._use_reward_memory_config)
            if guard.denials:
                abort()
                return denied()
            expected = self._use_reward_read_rows(conn, unique_ids)
            guard.allow_commit = True
            conn.commit()
            guard.allow_commit = False
        except BaseException as exc:
            guard.allow_commit = False
            abort()
            if not isinstance(exc, Exception):
                raise  # SystemExit / GeneratorExit / injected async exceptions: rolled back, then propagated
            if guard.denials:
                return denied()
            if isinstance(exc, sqlite3.Error):
                return _write_sqlite_error(
                    exc, "use_reward", None,
                    "retry with the SAME request_id; if it persists check the store's disk and permissions")
            raise
        # COMMITTED. From here the write cannot be undone.
        try:
            actual = self._use_reward_read_rows(conn, unique_ids)
            bad = [nid for nid in dict.fromkeys([*expected, *actual]) if expected.get(nid) != actual.get(nid)]
            # The independent monotone invariant: a shared function that CLAIMS an update (bumped > 0) must have moved
            # every id whose pre-op utility was below 0.99 strictly upward. (>= 0.99 is exempt: 6-dp rounding near 1.0
            # may legitimately leave the value unchanged; the parity cells own that region.)
            stalled = []
            if result.bumped > 0:
                stalled = [
                    nid for nid, row in actual.items()
                    if pre.get(nid, 0.0) < USE_REWARD_MONOTONE_FLOOR and (row["utility_score"] or 0.0) <= pre.get(nid, 0.0)
                ]
            if bad or stalled:
                parts = []
                if bad:
                    parts.append(f"the committed row differs from the in-transaction one for id(s) {bad}")
                if stalled:
                    parts.append(f"write did not land for id(s) {stalled} (utility_score did not move up from its pre-op value)")
                return {**_df_rejection(
                    "readback_mismatch",
                    "COMMITTED but UNVERIFIED: " + "; ".join(parts) + "; the commit cannot be undone",
                    "do not retry with a new request_id (it would bump again); inspect the named nodes"),
                    "commit": "committed_unverified", "_committed": True}
            return {
                "status": "ok",
                "result": {
                    "updated": list(expected),
                    "bumped": result.bumped,
                    "restamped": result.restamped,
                    "restamp_error": result.restamp_error,
                    "rows": actual,
                    "duplicate": False,
                },
                "_committed": True,
            }
        except BaseException as exc:
            # ANY failure after the commit (a verification bug, MemoryError, a sqlite error on the re-read, SystemExit):
            # the commit stands, so the outcome is committed-but-UNVERIFIED, never a bare internal error and never a
            # silent retry-bumps-again. Recorded BEFORE anything propagates; the caller's finally still restores the
            # authorizer / busy_timeout and releases the lock.
            unverified = {**_df_rejection(
                "readback_mismatch",
                f"COMMITTED but UNVERIFIED: the post-commit verification failed ({type(exc).__name__}); the commit stands and cannot be undone",
                "do not retry with a new request_id (it would bump again); retry with the SAME request_id to replay this outcome, and inspect the nodes"),
                "commit": "committed_unverified"}
            record(unverified)
            if not isinstance(exc, Exception):
                raise
            return {**unverified, "_committed": True}

    # -- the owner's second write (#3856) --------------------------------------

    def _handle_advance_moment_cursor(self, params: dict, deadline: float) -> dict:
        """The typed `advance_moment_cursor` write (kind `write`). Wire: params exactly {person_id, from_cursor}; no
        request_id (the shared compare-and-set makes a replay harmless: a retry after COMMIT finds from + 1 and
        answers `stale` with it).

        Order: validate, touching no connection (`_moment_cursor_param_refusal`); then the shared `_owner_write`
        scaffold (the same wait budget, lock, displacement re-check, open-transaction refusal and busy-timeout
        switch as use_reward) with the op's own restrictive authorizer (`_moment_cursor_allows`); BEGIN IMMEDIATE,
        the SHARED `engram_moment_cursor.advance_moment_cursor`, COMMIT only when it applied (ROLLBACK otherwise:
        there is nothing to write), then the read-back (design 5.4) from the committed store.

        Outcomes: `ok` with {applied, cursor, reason} (an `applied: False` answer, `stale` / `not_person` /
        `not_object`, is a typed ANSWER, not a refusal); `write_denied`; `busy` (nothing committed, determinate);
        `advance_moment_cursor_write_failed` (rolled back); `readback_mismatch` (COMMITTED but unverified)."""
        refusal = _moment_cursor_param_refusal(params)
        if refusal is not None:
            return refusal
        person_id: str = params["person_id"]
        from_cursor: int = params["from_cursor"]
        return self._owner_write(
            "advance_moment_cursor", deadline, lambda: _UseRewardGuard(allows=_moment_cursor_allows),
            lambda conn, guard: self._advance_moment_cursor_execute(conn, guard, person_id, from_cursor),
            busy_remedy="retry once with the same from_cursor (nothing was written, so the retry runs fresh)")

    def _moment_cursor_readback(self, conn: sqlite3.Connection, person_id: str):
        """The person's cursor under the ONE cursor rule, read from the store (None if there is no current person
        row). Runs after COMMIT under the op allowlist; a test replaces it to perturb the post-commit value."""
        row = conn.execute(
            "SELECT " + emc.CURSOR_SQL_EXPR + " FROM nodes WHERE id = :id AND type = 'person' AND is_current = 1",
            {"id": person_id},
        ).fetchone()
        return None if row is None else row[0]

    def _advance_moment_cursor_execute(self, conn: sqlite3.Connection, guard: "_UseRewardGuard", person_id: str,
                                       from_cursor: int) -> dict:
        """The transaction body of `_handle_advance_moment_cursor`; runs with the op allowlist installed and
        `_owner_lock` held."""
        op = "advance_moment_cursor"
        try:
            # Opened up front so a competing direct writer is met here (busy) rather than half way through.
            conn.execute("BEGIN IMMEDIATE")
            result = emc.advance_moment_cursor(conn, person_id, from_cursor)
            if guard.denials:
                _write_abort(conn, guard, op)
                return _write_denied(guard, op)
            if result["applied"]:
                guard.allow_commit = True
                conn.commit()
                guard.allow_commit = False
            else:
                _write_abort(conn, guard, op)  # nothing to write: stale / not_person / not_object
        except BaseException as exc:
            guard.allow_commit = False
            _write_abort(conn, guard, op)
            if not isinstance(exc, Exception):
                raise  # SystemExit / GeneratorExit / injected async exceptions: rolled back, then propagated
            if guard.denials:
                return _write_denied(guard, op)
            if isinstance(exc, sqlite3.Error):
                return _write_sqlite_error(
                    exc, op, "retry once with the same from_cursor (nothing was written, so the retry runs fresh)",
                    "retry once with the same from_cursor; if it persists check the store's disk and permissions")
            raise
        if not result["applied"]:
            return {"status": "ok", "result": dict(result)}
        # COMMITTED. From here the write cannot be undone: every failure below is committed-but-UNVERIFIED.
        try:
            # The committed value is read under the ONE cursor rule, so the comparison is against that rule applied
            # to from + 1 too: at from = 2**53 - 1 the stored 2**53 is outside the window and reads as 0 (measured by
            # the range-edge cell), and that is a verified commit, not a mismatch.
            expected = emc.read_cursor(from_cursor + 1)
            actual = self._moment_cursor_readback(conn, person_id)
            if actual != expected:
                return {**_df_rejection(
                    "readback_mismatch",
                    f"COMMITTED but UNVERIFIED: the committed cursor reads {actual!r}, expected {expected}; the commit cannot be undone",
                    "do not blindly repeat; read the person's current cursor and decide (another writer may have advanced it)"),
                    "commit": "committed_unverified"}
            return {"status": "ok", "result": dict(result)}
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return {**_df_rejection(
                "readback_mismatch",
                f"COMMITTED but UNVERIFIED: the post-commit verification failed ({type(exc).__name__}); the commit stands and cannot be undone",
                "do not blindly repeat; read the person's current cursor and decide"),
                "commit": "committed_unverified"}

    # -- the owner's third and fourth writes (#3879) ---------------------------------------------

    def _handle_fill_embeddings(self, params: dict, deadline: float, request_id=None) -> dict:
        """The typed `fill_embeddings` write (kind `write`). Wire: params exactly {model, items: [{node_id, vector}]},
        at most FILL_EMBEDDINGS_MAX_ITEMS items; `request_id` is the envelope's (see WIRE_REQUEST_ID_METHODS).

        Order: (1) validate EVERY item, touching no connection (`_fill_embeddings_param_refusal`): one bad item rejects
        the whole call with nothing written; (2) the request_id; (3) the model, against the OWNER'S OWN home config read
        through the same function the server uses (`engram_core._get_embedding_config`, the tool's exact expression):
        `model_mismatch` if it differs, so two vector spaces can never mix; (4) the shared `_owner_write` scaffold (the
        one wait budget, lock, displacement re-check, open-transaction refusal, busy-timeout switch) with this op's
        restrictive authorizer; UNDER THE LOCK a dedupe check by request_id (a replay answers the stored outcome with
        `duplicate: True`; the same id with a different payload is refused); BEGIN IMMEDIATE, the per-item
        compare-and-set `UPDATE nodes SET embedding = ? WHERE id = ? AND is_current = 1 AND embedding IS NULL`, the
        classification of every item, the best-effort vec mirror of the CHANGED rows only (own savepoint), COMMIT, then
        the read-back of every changed row.

        Outcomes: `ok` {filled, skipped_not_null, skipped_not_current_or_missing, vec_mirrored}; the four validation
        refusals; `write_denied`; `busy` (nothing committed); `fill_embeddings_write_failed` (rolled back);
        `readback_mismatch` (COMMITTED but unverified, recorded so a replay re-reports instead of re-writing)."""
        refusal, items = _fill_embeddings_param_refusal(params)
        if refusal is not None:
            return refusal
        refusal = _fill_request_id_refusal(request_id)
        if refusal is not None:
            return refusal
        model: str = params["model"]
        try:
            core = _engram_core(self.engram_home)
            store_model = core._get_embedding_config().get("model", core.DEFAULT_EMBEDDING_MODEL)
        except Exception as exc:
            return _fill_rejection(
                "model_config_unreadable", f"the store's embedding model could not be determined ({type(exc).__name__})",
                "fix the owner home's config.json (or the owner's runtime); nothing was written", reason="model_config_unreadable")
        if not isinstance(store_model, str) or model != store_model:
            return _fill_rejection(
                "model_mismatch", "'model' is not the store's configured embedding model",
                "embed with the model named in the owner home's config.json; two vector spaces must never mix",
                reason="model_mismatch")
        texts = [json.dumps(vector) for _nid, vector in items]
        work = [(nid, text, vector) for (nid, vector), text in zip(items, texts)]
        fingerprint = _fill_embeddings_fingerprint(model, [(nid, text) for nid, text, _v in work])

        def record(resp: dict) -> None:
            """Remember a COMMITTED outcome (ok or committed_unverified) so a same-id retry replays it."""
            self._fill_embeddings_seen[request_id] = {"fp": fingerprint, "response": copy.deepcopy(resp)}
            self._fill_embeddings_seen.move_to_end(request_id)
            while len(self._fill_embeddings_seen) > USE_REWARD_DEDUPE_CAP:
                self._fill_embeddings_seen.popitem(last=False)

        def dedupe_check() -> "dict | None":
            prior = self._fill_embeddings_seen.get(request_id)
            if prior is None:
                return None
            self._fill_embeddings_seen.move_to_end(request_id)
            if prior["fp"] != fingerprint:
                return _fill_rejection(
                    "request_id_reused", "this request_id was already used for a different payload",
                    "generate a fresh request_id per distinct operation; reuse one only to retry the SAME call")
            replay = copy.deepcopy(prior["response"])
            if replay.get("status") == "ok":
                replay["result"]["duplicate"] = True
            return replay

        return self._owner_write(
            "fill_embeddings", deadline, lambda: _UseRewardGuard(allows=_fill_embeddings_allows, vec_allows=_fill_vec_phase_allows),
            lambda conn, guard: self._fill_embeddings_execute(conn, guard, work, record),
            intercept=dedupe_check, after=record)

    def _fill_vec_ready(self, conn: sqlite3.Connection):
        """The vec mirror's serializer (`sqlite_vec.serialize_float32`, what the direct path uses) if THIS connection
        can write `vec_nodes`, else None. Probed once per owner connection: import `sqlite_vec` and load vec0 onto the
        owner's connection (the one place this process loads an extension; the loader switches loading back off). A
        Python whose sqlite3 cannot enable extension loading (the macOS framework build) or an install without
        sqlite-vec yields None for the process lifetime, and every fill then acks `vec_mirrored: false`. A test
        replaces this method."""
        if self._fill_vec_state is None:
            try:
                import sqlite_vec
                conn.enable_load_extension(True)  # absent on a Python built without it: AttributeError -> unavailable
                try:
                    sqlite_vec.load(conn)
                finally:
                    conn.enable_load_extension(False)
                self._fill_vec_state = sqlite_vec.serialize_float32
            except Exception as exc:
                logging.getLogger("engramd_owner").info("fill_embeddings: vec0 unavailable on the owner connection (%s)",
                                                        type(exc).__name__)
                self._fill_vec_state = False
        return self._fill_vec_state or None

    def _fill_embeddings_readback(self, conn: sqlite3.Connection, ids) -> dict:
        """{id: stored embedding text (None if NULL or the row is gone)} for each of `ids`, read from the committed
        store (design 5.4). Runs after COMMIT under the op allowlist; a test replaces it to perturb the post-commit value."""
        out: dict = {}
        for nid in ids:
            row = conn.execute("SELECT embedding FROM nodes WHERE id = ?", (nid,)).fetchone()
            out[nid] = None if row is None else row[0]
        return out

    def _fill_vec_mirror(self, conn: sqlite3.Connection, guard: "_UseRewardGuard", changed, serialize) -> bool:
        """Mirror the CHANGED rows into `vec_nodes` (DELETE + INSERT per row, as the direct path does) inside the op's
        own SAVEPOINT. True iff every row was mirrored. ANY failure (the vec0 module or table missing, a locked
        table, a serializer error, or an authorizer denial of a vec statement) rolls back to the savepoint, drops the
        denials recorded since it opened (a vec denial degrades the mirror, it does not fail the fill) and returns
        False: the `nodes` writes stand and the ack says `vec_mirrored: false` (today's direct path swallows this)."""
        n_denied_before = len(guard.denials)
        sp = FILL_EMBEDDINGS_VEC_SAVEPOINT
        conn.execute(f"SAVEPOINT {sp}")
        guard.vec_phase = True  # opens AFTER the savepoint statement and closes in the finally: only the mirror's own statements
        try:
            for nid, _text, vector in changed:
                conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (nid,))
                conn.execute("INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)", (nid, serialize(vector)))
            conn.execute(f"RELEASE {sp}")
            return True
        except Exception as exc:
            logging.getLogger("engramd_owner").warning(
                "fill_embeddings: vec mirror failed (%s: %s); the embeddings are written, vec_nodes is not", type(exc).__name__, exc)
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
            del guard.denials[n_denied_before:]
            return False
        finally:
            guard.vec_phase = False

    def _fill_embeddings_execute(self, conn: sqlite3.Connection, guard: "_UseRewardGuard", work: list, record) -> dict:
        """The transaction body of `_handle_fill_embeddings`; runs with the op allowlist installed and `_owner_lock`
        held. `work` is [(node_id, stored text, vector)]. A response carrying `_committed: True` is one whose COMMIT
        happened (the scaffold records it for dedupe and strips the marker)."""
        op = "fill_embeddings"
        changed: list = []
        not_null = missing = 0
        mirrored = False
        try:
            serialize = self._fill_vec_ready(conn)  # a Python-level probe: no SQL, before the transaction opens
            # Opened up front so a competing direct writer is met here (busy) rather than half way through.
            conn.execute("BEGIN IMMEDIATE")
            for nid, text, vector in work:
                cur = conn.execute(
                    "UPDATE nodes SET embedding = ? WHERE id = ? AND is_current = 1 AND embedding IS NULL", (text, nid))
                if cur.rowcount == 1:
                    changed.append((nid, text, vector))
                    continue
                row = conn.execute("SELECT is_current, embedding IS NULL FROM nodes WHERE id = ?", (nid,)).fetchone()
                if row is not None and row[0] == 1 and row[1] == 0:
                    not_null += 1  # current, and the CAS refused it: something already holds an embedding
                else:
                    missing += 1  # no such row, or not current
            mirrored = serialize is not None
            if serialize is not None and changed:
                mirrored = self._fill_vec_mirror(conn, guard, changed, serialize)
            if guard.denials:
                _write_abort(conn, guard, op)
                return _write_denied(guard, op)
            guard.allow_commit = True
            conn.commit()
            guard.allow_commit = False
        except BaseException as exc:
            guard.allow_commit = False
            _write_abort(conn, guard, op)
            if not isinstance(exc, Exception):
                raise  # SystemExit / GeneratorExit / injected async exceptions: rolled back, then propagated
            if guard.denials:
                return _write_denied(guard, op)
            if isinstance(exc, sqlite3.Error):
                return _write_sqlite_error(
                    exc, op, "retry once with the SAME request_id (nothing was written, so the retry runs fresh)",
                    "retry with the SAME request_id; if it persists check the store's disk and permissions")
            raise
        # COMMITTED. From here the write cannot be undone: every failure below is committed-but-UNVERIFIED.
        try:
            ids = [nid for nid, _t, _v in changed]
            actual = self._fill_embeddings_readback(conn, ids) if ids else {}
            bad = [nid for nid, text, _v in changed if actual.get(nid) != text]
            if bad:
                resp = {**_df_rejection(
                    "readback_mismatch",
                    f"COMMITTED but UNVERIFIED: the committed embedding differs from the one written for id(s) {bad}; the commit cannot be undone",
                    "do not retry with a new request_id; retry with the SAME request_id to replay this outcome, and inspect the named nodes"),
                    "commit": "committed_unverified", "_committed": True}
                return resp
            return {
                "status": "ok",
                "result": {
                    "filled": len(changed),
                    "skipped_not_null": not_null,
                    "skipped_not_current_or_missing": missing,
                    "vec_mirrored": bool(mirrored),
                },
                "_committed": True,
            }
        except BaseException as exc:
            unverified = {**_df_rejection(
                "readback_mismatch",
                f"COMMITTED but UNVERIFIED: the post-commit verification failed ({type(exc).__name__}); the commit stands and cannot be undone",
                "do not retry with a new request_id; retry with the SAME request_id to replay this outcome, and inspect the nodes"),
                "commit": "committed_unverified"}
            record(unverified)
            if not isinstance(exc, Exception):
                raise
            return {**unverified, "_committed": True}

    def _handle_finish_embedding_regen(self, params: dict, deadline: float) -> dict:
        """The typed `finish_embedding_regen` write (kind `write`). Wire: params exactly {processed, embedded, skipped,
        failed, embedder_available}: four non-negative ints and a real bool. `embedder_available` is the TOOL's probe
        of the embedder it just used; the owner passes it to the core finalize and NEVER probes an embedder itself
        (that would import sentence_transformers / torch into this process, and could pick a different cause than the
        direct path). No request_id: a replay re-runs an idempotent rebuild and flag write.

        Validate (touching no connection); import `engram_core` / `engram_backup` lazily (they are large / only this op
        needs them); then the shared `_owner_write` scaffold with this op's authorizer and ONE transaction: the FTS
        rebuild on the owner's connection (`engram_backup.rebuild_fts_index_on_conn`, the one body the path-taking
        function also runs), then exactly today's tool-tail branch through the SAME core functions with
        `respect_lease=True` (no second copy of the lease logic): ALWAYS `_finalize_embedding_restore_flag`, whose fresh
        skip-aware recount clears the flag when no embeddable NULL remains and sets it otherwise. The owner never
        branches on the client's `processed` (a stale or lying `processed == 0` must not clear a flag over real
        NULLs; in every honest case the tool's own count 0 means recount 0, so this equals the direct path). The core function's own COMMIT is the transaction's commit
        (the only one the op allows). Read-back: the FTS doc count equals the count of indexable `nodes` rows, and the
        `embedding_restore_state` row is what the branch can have written.

        Outcomes: `ok` {fts_rows, restore_state}; `write_denied`; `busy`; `finish_embedding_regen_write_failed` (the
        whole op rolled back, the FTS rebuild included); `readback_mismatch` (COMMITTED but unverified)."""
        refusal = _finish_embedding_regen_param_refusal(params)
        if refusal is not None:
            return refusal
        counts = {k: params[k] for k in ("processed", "embedded", "skipped", "failed")}
        try:
            core = _engram_core(self.engram_home)
            import engram_backup
        except ImportError as exc:
            return _df_rejection(
                "finish_embedding_regen_runtime_unavailable", f"the owner's runtime lacks a module this op needs ({type(exc).__name__})",
                "owner install fault: the owner's runtime is incomplete")
        return self._owner_write(
            "finish_embedding_regen", deadline, lambda: _UseRewardGuard(allows=_finish_embedding_regen_allows),
            lambda conn, guard: self._finish_embedding_regen_execute(
                conn, guard, counts, core, engram_backup, embedder_available=params["embedder_available"]),
            busy_remedy="retry once (nothing was written, so the retry runs fresh)")

    def _finish_embedding_regen_readback(self, conn: sqlite3.Connection) -> dict:
        """{fts_rows, state} read from the committed store: the FTS index's document count (None if there is no
        `nodes_fts`) and the `embedding_restore_state` row (None if absent). A test replaces it to perturb either."""
        has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts'").fetchone()
        fts_rows = conn.execute("SELECT COUNT(*) FROM nodes_fts_docsize").fetchone()[0] if has_fts else None
        row = conn.execute(
            "SELECT not_fully_restored, null_count, cause, updated_at FROM embedding_restore_state WHERE singleton_key = 1"
        ).fetchone()
        return {"fts_rows": fts_rows, "state": None if row is None else tuple(row)}

    @staticmethod
    def _restore_state_is_what_the_branch_wrote(pre, actual) -> bool:
        """The restore row after the branch is one the branch can have written: a fresh foreign lease left it
        EXACTLY as it was; else the cleared shape; else the set shape with a real cause (the finalize recount decides
        which; the client's `processed` never does)."""
        if actual is None:
            return False
        if pre is not None and pre[2] == "regeneration-running" and tuple(actual) == tuple(pre):
            return True  # respect_lease: a fresh lease held by someone else was not overwritten
        cleared = actual[0] == 0 and actual[1] == 0 and actual[2] is None
        return cleared or (actual[0] == 1 and actual[1] > 0 and actual[2] in ("per-node-failures", "embedder-unavailable"))

    def _finish_embedding_regen_execute(self, conn: sqlite3.Connection, guard: "_UseRewardGuard", counts: dict, core, backup,
                                        *, embedder_available: bool) -> dict:
        """The transaction body of `_handle_finish_embedding_regen`; runs with the op allowlist installed and
        `_owner_lock` held. `embedder_available` is the tool's probe, handed to the core finalize as given."""
        op = "finish_embedding_regen"
        try:
            conn.execute("BEGIN IMMEDIATE")
            backup.rebuild_fts_index_on_conn(conn, str(self.store_path))
            # EXPECTED fts rows: an independent count of the rows the index must hold (the shared partition predicate).
            expected_fts = None
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts'").fetchone():
                node_cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
                _exclude, indexable = backup._fts_row_predicates(node_cols)
                expected_fts = conn.execute(
                    "SELECT COUNT(*) FROM nodes" + (f" WHERE {indexable}" if indexable else "")).fetchone()[0]
            core._ensure_embedding_restore_state_table(conn)
            pre = conn.execute(
                "SELECT not_fully_restored, null_count, cause, updated_at FROM embedding_restore_state WHERE singleton_key = 1"
            ).fetchone()
            # The core function COMMITS (its own write path): that commit is this transaction's, the FTS rebuild included.
            guard.allow_commit = True
            try:
                # ALWAYS the recount path: `processed` is the client's claim and decides nothing here. The core finalize
                # clears when its fresh skip-aware recount is 0 and sets the flag otherwise (cause from the tool's probe).
                core._finalize_embedding_restore_flag(
                    conn, dict(counts), respect_lease=True, embedder_available=embedder_available)
            finally:
                guard.allow_commit = False
            if guard.denials:
                _write_abort(conn, guard, op)
                return _write_denied(guard, op)
            if conn.in_transaction:
                guard.allow_commit = True
                try:
                    conn.commit()
                finally:
                    guard.allow_commit = False
        except BaseException as exc:
            guard.allow_commit = False
            _write_abort(conn, guard, op)
            if not isinstance(exc, Exception):
                raise  # SystemExit / GeneratorExit / injected async exceptions: rolled back, then propagated
            if guard.denials:
                return _write_denied(guard, op)
            if isinstance(exc, sqlite3.Error):
                return _write_sqlite_error(
                    exc, op, "retry once (nothing was written, so the retry runs fresh)",
                    "retry once; if it persists check the store's disk and permissions")
            raise
        # COMMITTED. From here the write cannot be undone: every failure below is committed-but-UNVERIFIED.
        try:
            actual = self._finish_embedding_regen_readback(conn)
            problems = []
            if actual["fts_rows"] != expected_fts:
                problems.append(f"the FTS index holds {actual['fts_rows']} documents, expected {expected_fts}")
            if not self._restore_state_is_what_the_branch_wrote(pre, actual["state"]):
                problems.append("the embedding_restore_state row is not what the branch wrote")
            if problems:
                return {**_df_rejection(
                    "readback_mismatch", "COMMITTED but UNVERIFIED: " + "; ".join(problems) + "; the commit cannot be undone",
                    "inspect the FTS index and the restore-state row; re-running the op is safe (a rebuild and a flag write)"),
                    "commit": "committed_unverified"}
            state = actual["state"]
            return {"status": "ok", "result": {"fts_rows": actual["fts_rows"] or 0, "restore_state": None if state is None else state[2]}}
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return {**_df_rejection(
                "readback_mismatch",
                f"COMMITTED but UNVERIFIED: the post-commit verification failed ({type(exc).__name__}); the commit stands and cannot be undone",
                "inspect the FTS index and the restore-state row; re-running the op is safe"),
                "commit": "committed_unverified"}

    def _read_deadline_rejection(self, detail: str) -> dict:
        return {
            "status": "rejected",
            "reason": "deadline_exceeded",
            "detail": detail,
            "remedy": "retry the read; if it keeps overrunning the statement or the store is too slow for the request deadline",
        }

    def _handle_read(self, params: dict, deadline: float) -> dict:
        """The client-facing `read` method (U4d, #3197; contract 1.1).

        Wire: params = {"sql": <str>, "args": <list>} (D1: the client sends the
        SQL literal itself, which the committed read-template list is matched
        against byte-for-byte); reply `ok` carries `result` = the rows as JSON lists in
        SQLite's own order (D2). Cell types None/int/float/str only: a bytes
        cell is `rejected` / `unsupported_value_type`, never encoded or dropped.

        Routing (test 22): the ONLY object this touches is
        `self._client_read_gate`, which holds only `_client_read_conn`. Never
        `_owner_conn`, never `_owner_lock`.

        Deadline: execution stops READ_RESPONSE_RESERVE before the request
        deadline so the `deadline_exceeded` rejection can still be delivered;
        the wait for the shared read connection is bounded by the same instant.
        """
        gate = self._client_read_gate
        if gate is None:
            return {
                "status": "rejected",
                "reason": "owner_stopping",
                "detail": "the owner is shutting down; its read path is closed",
                "remedy": "retry once the owner has restarted",
            }
        sql = params.get("sql")
        args = params.get("args", [])
        if isinstance(args, list):
            args = tuple(args)
        # A non-list `args` is passed through as-is: the template list's own check
        # refuses it as `invalid_parameter` (one source of refusal reasons).
        failure, outcome = self._run_client_read(
            deadline, lambda execution_deadline: gate.execute(sql, args, deadline=execution_deadline)
        )
        if failure is not None:
            return failure
        if isinstance(outcome, rr.Rejected):
            return outcome.as_wire()
        # Serialize once (#3678 cut 2): sized and sent from the same fragments.
        reply, _counted = _render_read_rows(outcome)
        return reply

    def _run_client_read(self, deadline: float, run) -> "tuple[dict | None, object]":
        """The lock / deadline / error tail shared by `read` and `df`.
        -> (failure_reply, outcome): `failure_reply` is a rejection dict (no time
        left, deadline abort, or a SQLite error) and `outcome` is then None;
        otherwise `failure_reply` is None and `outcome` is whatever
        `run(execution_deadline)` returned, computed under
        `self._client_read_lock`."""
        execution_deadline = deadline - READ_RESPONSE_RESERVE
        remaining = execution_deadline - time.monotonic()
        if remaining <= 0 or not self._client_read_lock.acquire(timeout=remaining):
            return self._read_deadline_rejection(
                "no execution time was left inside the request deadline (after reserving time to deliver the response)"
            ), None
        try:
            try:
                return None, run(execution_deadline)
            except sqlite3.OperationalError as exc:
                # sqlite3.SQLITE_INTERRUPT and exc.sqlite_errorcode are Python
                # 3.11 additions (3.10.7+ is supported): no bare access here.
                # 9 is SQLITE_INTERRUPT; "interrupted" is the < 3.11 fallback.
                if getattr(exc, "sqlite_errorcode", None) == getattr(sqlite3, "SQLITE_INTERRUPT", 9) \
                        or str(exc) == "interrupted":
                    return self._read_deadline_rejection("the read was aborted at the request deadline"), None
                return self._read_failed(exc), None
            except sqlite3.Error as exc:  # incl. DataError: a cell over SQLITE_LIMIT_LENGTH
                return self._read_failed(exc), None
        finally:
            self._client_read_lock.release()

    def _handle_df(self, params: dict, deadline: float) -> dict:
        """The typed `df` method (U6b, #3665): document frequencies for a list of
        terms from `nodes_fts_vocab`. Wire: params = {"terms": [<str>, ...]}
        (1..DF_TERMS_MAX, no other key; each violation has its own named
        reason, see `_df_param_refusal`); reply `ok` carries `result` = rows
        `[term, doc]` for each term PRESENT in the vocabulary (absent terms
        contribute no row; row order is not part of the contract).

        Order: (1) validate -- nothing is prepared on the store connection for a
        refused request; (2) only then touch `self._client_read_gate`, the same
        client-read-only route as `read` (never `_owner_conn` / `_owner_lock`);
        (3) the vocab table must be the `row` fts5vocab variant; (4) run the
        fixed DF_SQL with the terms bound as ONE JSON-list parameter."""
        refusal = _df_param_refusal(params)
        if refusal is not None:
            return refusal
        terms = params["terms"]
        gate = self._client_read_gate
        if gate is None:
            return {
                "status": "rejected",
                "reason": "owner_stopping",
                "detail": "the owner is shutting down; its read path is closed",
                "remedy": "retry once the owner has restarted",
            }

        def run(execution_deadline: float):
            ddl = gate.execute_typed(DF_VOCAB_DDL_SQL, (), deadline=execution_deadline)
            if not ddl:
                # Never created here: the client-read authorizer denies CREATE,
                # and the owner does not build the vocab table on a read path.
                return {
                    "status": "rejected",
                    "reason": "vocab_table_absent",
                    "detail": "nodes_fts_vocab is not in this store's sqlite_master",
                    "remedy": "the vocab table is created by the IDF writer path (engram_idf.ensure_vocab_table); the owner-connection shim treats every term as absent from the vocabulary and returns no rows, so IDF scoring yields no keywords until it exists",
                }
            match = _DF_VOCAB_VARIANT_RE.search(_DF_SQL_COMMENT_RE.sub(" ", ddl[0][0] or ""))
            if match is None or match.group(1).lower() != "row":
                return {
                    "status": "rejected",
                    "reason": "unsupported_vocab_variant",
                    "detail": "nodes_fts_vocab is not an fts5vocab(..., 'row') table; df needs the row shape (term, doc, cnt)",
                    "remedy": "the col and instance variants return different column shapes and row semantics; recreate the vocab table as the row variant",
                }
            return gate.execute_typed(DF_SQL, (json.dumps(terms),), deadline=execution_deadline)

        failure, outcome = self._run_client_read(deadline, run)
        if failure is not None:
            return failure
        if isinstance(outcome, dict):
            return outcome
        reply, _counted = _render_read_rows(outcome)
        return reply

    def _handle_chain(self, params: dict, deadline: float) -> dict:
        """The typed `chain` method (#3740 Cut A): the generalization chain of one node as
        ONE request, instead of a hop-by-hop walk of 12-14 round trips. Wire: params =
        {"node_id": <str>, "max_depth": <int>} (exactly these keys; each violation has its
        own named reason, see `_chain_param_refusal`); reply `ok` carries `result` = the dict
        `engram_core.generalization_chain` returned, JSON round-tripped (keys are strings).
        A `max_depth` above CHAIN_MAX_DEPTH_CAP is CLAMPED to it, not rejected.

        This runs engram_core's OWN function on the client-read connection; it does not
        re-implement the walk, so the owner and direct paths are equal by construction.
        The client sends no SQL: `node_id` is bound as a parameter of statements fixed in
        engram_core, and `_TYPED_STATEMENTS` is not widened (see `_ClientReadGate.run_chain`).

        Order: (1) validate -- nothing is prepared on the store connection for a refused
        request; (2) only then touch `self._client_read_gate`, the same client-read-only
        route as `read` / `df` (never `_owner_conn` / `_owner_lock`), through the shared
        lock / deadline / error tail; (3) bound the serialised reply by
        CHAIN_RESULT_MAX_BYTES -- `claim` is unbounded by design, so an oversized chain is a
        named `chain_result_too_large`, never a truncated chain. A store without the tables
        the walk reads is a named `read_failed`, as for any SQLite error; an owner whose
        runtime lacks engram_core answers `chain_runtime_unavailable`."""
        refusal = _chain_param_refusal(params)
        if refusal is not None:
            return refusal
        node_id = params["node_id"]
        depth = min(params["max_depth"], CHAIN_MAX_DEPTH_CAP)
        gate = self._client_read_gate
        if gate is None:
            return {
                "status": "rejected",
                "reason": "owner_stopping",
                "detail": "the owner is shutting down; its read path is closed",
                "remedy": "retry once the owner has restarted",
            }

        # Import engram_core HERE: after validation, before `_client_read_lock`. A cold import
        # (tens of ms) must not hold the lock that `read` / `df` wait on, nor eat the walk's
        # deadline; a failed import acquires nothing. `run_chain` calls the accessor again,
        # which is then a `sys.modules` hit.
        try:
            _engram_core(self.engram_home)
        except ImportError as exc:
            return self._chain_runtime_unavailable(exc)

        def run(execution_deadline: float):
            return gate.run_chain(node_id, depth, deadline=execution_deadline)

        try:
            failure, outcome = self._run_client_read(deadline, run)
        except ImportError as exc:  # engram_core vanished between the import above and the walk
            return self._chain_runtime_unavailable(exc)
        if failure is not None:
            return failure
        # Serialise once: the bound is counted on, and the reply sent as, the SAME text.
        result_text = json.dumps(outcome)
        wire_text = '{"status": "ok", "result": ' + result_text + "}"
        if len(wire_text) > CHAIN_RESULT_MAX_BYTES:
            return {
                "status": "rejected",
                "reason": "chain_result_too_large",
                "detail": f"the chain is {len(wire_text)} bytes serialised; the maximum is {CHAIN_RESULT_MAX_BYTES}; nothing was sent",
                "remedy": "this is a property of the data (an unbounded `claim` on a head), not an outage -- do NOT restart the owner; the chain cannot be served through the owner",
            }
        return _RenderedReadOk(outcome, wire_text)

    @staticmethod
    def _chain_runtime_unavailable(exc: Exception) -> dict:
        logging.getLogger("engramd_owner").warning("chain: engram_core could not be imported: %s", exc)
        return {
            "status": "rejected",
            "reason": "chain_runtime_unavailable",
            "detail": f"the owner could not import engram_core: {type(exc).__name__}",
            "remedy": "the owner's runtime is incomplete (engram_core.py must sit beside engramd_owner.py); reinstall the plugin and restart the owner",
        }

    def _handle_confidence_by_type(self, params: dict, deadline: float) -> dict:
        """The typed `confidence_by_type` method (#3741): the by_type confidence distribution for
        up to CBT_MAX_MODES windows as ONE request, for the session-start calibration block.
        Wire: params = {"modes": [<str>, ...]} (exactly that key; each violation has its own
        named reason, see `_cbt_param_refusal`; an over-long list is refused, never clamped,
        section 3.6); reply `ok` carries `result` = {"current_turn": int, "by_mode": {mode:
        {"by_type": {type: {n, mean, p25, p50, p75, p90, p95}}}}}.

        Runs engram_stats' OWN `confidence_by_type` on the client-read connection (see
        `_ClientReadGate.run_confidence_by_type`); it is read-only -- no write, no tool_timing, no
        migration.

        Order (as `chain`): (1) validate -- nothing is prepared for a refused request; (2) the
        lazy runtime import, after validation and BEFORE `_client_read_lock` (a cold import must
        not hold the lock `read` / `df` wait on; a failed import acquires nothing) -- an owner
        whose runtime lacks the modules answers `cbt_runtime_unavailable`; (3) the shared
        lock / deadline / error tail."""
        refusal = _cbt_param_refusal(params)
        if refusal is not None:
            return refusal
        modes = list(params["modes"])
        gate = self._client_read_gate
        if gate is None:
            return {
                "status": "rejected",
                "reason": "owner_stopping",
                "detail": "the owner is shutting down; its read path is closed",
                "remedy": "retry once the owner has restarted",
            }
        try:
            _engram_core(self.engram_home)
            import engram_stats  # noqa: F401 -- warm the import outside the lock
        except ImportError as exc:
            return self._cbt_runtime_unavailable(exc)

        def run(execution_deadline: float):
            return gate.run_confidence_by_type(modes, deadline=execution_deadline)

        try:
            failure, outcome = self._run_client_read(deadline, run)
        except ImportError as exc:  # the runtime vanished between the import above and the read
            return self._cbt_runtime_unavailable(exc)
        if failure is not None:
            return failure
        return {"status": "ok", "result": outcome}

    @staticmethod
    def _cbt_runtime_unavailable(exc: Exception) -> dict:
        logging.getLogger("engramd_owner").warning("confidence_by_type: runtime could not be imported: %s", exc)
        return {
            "status": "rejected",
            "reason": "cbt_runtime_unavailable",
            "detail": f"the owner could not import its stats runtime: {type(exc).__name__}",
            "remedy": "the owner's runtime is incomplete (engram_core.py and engram_stats.py must sit beside engramd_owner.py); reinstall the plugin and restart the owner",
        }

    def _read_failed(self, exc: Exception) -> dict:
        logging.getLogger("engramd_owner").warning(
            "client read failed: %s: %s", type(exc).__name__, exc)
        return {
            "status": "rejected",
            "reason": "read_failed",
            "detail": f"{type(exc).__name__}: {exc}"[:200],
            "remedy": "the statement was admitted but could not complete (an over-limit value, a locked or damaged store); see the owner log",
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
        # PREMISE (#3240 item 3): os.access() here tests the OWNER's rights,
        # which equals the CALLER's rights only because the peer-credential
        # gate (_check_peer_credentials) admits solely callers whose uid is
        # the owner's. Admitting any cross-uid caller requires replacing
        # this with a check against the CALLER's rights. Pinned by
        # tests/test_engramd_owner_displacement_3240.py::
        # test_snapshot_dest_rights_premise_peer_uid_equals_owner.
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


# The ONE exception -> exit-code table `main()` uses, for `--preflight` and for `start()` alike
# (#3785, Borges's ruling on PR #3776): two hand-written tables had drifted (--preflight gave a
# planted pidfile 1, start() gave it 4). Looked up with isinstance, in order; none of these
# classes subclasses another (all are direct RuntimeError subclasses), so the order carries no
# meaning today, but a future subclass must be listed BEFORE its base. `--preflight` only ever
# raises the classes `check_start_refusals` raises; OwnerAlreadyRunning, OwnerAclNotStripped and
# OwnerStoreBehindSchema are start()-only.
# Owner* classes deliberately absent (OwnerStalePathUnsafe, OwnerPeerCredentialsMalformed,
# OwnerPythonTooOld) are not mapped and propagate.
_REFUSAL_EXIT: tuple[tuple[type[RuntimeError], int], ...] = (
    (OwnerAlreadyRunning, 1),
    (OwnerSocketPathTooLong, 1),
    (OwnerHomeTccProtected, 1),
    (OwnerAclNotStripped, 1),
    (OwnerPermissionUnsupported, 1),
    (OwnerPidfileNotRegular, EXIT_PATH_NOT_REGULAR),  # retrying cannot fix a planted path
    (OwnerStorePathNotRegular, EXIT_PATH_NOT_REGULAR),
    # Its own code (not 1, not os._exit): non-zero, so the supervisor retries while the first
    # migration finishes.
    (OwnerStoreBehindSchema, EXIT_STORE_BEHIND_SCHEMA),
)


def _refusal_exit_code(exc: BaseException) -> int | None:
    """The exit code `_REFUSAL_EXIT` gives `exc`, or None if it is not a mapped refusal."""
    for cls, code in _REFUSAL_EXIT:
        if isinstance(exc, cls):
            return code
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    owner = EngramdOwner()
    refusals = tuple(cls for cls, _code in _REFUSAL_EXIT)
    if "--preflight" in args:
        # #3709: run only the refusals start() makes before it touches anything
        # (ENGRAM_HOME from the environment). Mutates nothing; silent exit 0
        # when start() would get past them, else the owner's own reason on
        # stderr and the code `_REFUSAL_EXIT` gives it (the same one start() gives).
        try:
            owner.check_start_refusals()
        except refusals as exc:
            print(f"[engramd] {exc}", file=sys.stderr)
            return _refusal_exit_code(exc)
        except NotADirectoryError as exc:
            # #3780: the pidfile path sits under something that is not a directory (ENGRAM_HOME is
            # a regular file, or a path component is). The `lstat` in check 4 raised ENOTDIR and it
            # escaped as a traceback (still exit 1). One line, same style, same exit code (1).
            print(f"[engramd] refusing to start: a path the preflight checks under ENGRAM_HOME has a "
                  f"component that is not a directory (e.g. the pidfile path {owner.pid_path}; "
                  f"{exc.strerror or 'ENOTDIR'}); nothing was bound. Fix ENGRAM_HOME and start again",
                  file=sys.stderr)
            return 1
        return 0
    try:
        owner.start(install_signal_handlers=True)
    except refusals as exc:
        print(f"[engramd] {exc}", file=sys.stderr)
        return _refusal_exit_code(exc)
    print(f"[engramd] listening on {owner.socket_path} (pid {os.getpid()})", file=sys.stderr)
    owner.wait_forever()
    # Reading _displaced right after wait_forever() is safe because stop()
    # (and so _cleanup(), which can set the flag) runs in the signal handler
    # on THIS main thread before wait_forever() returns. If stop() is ever
    # called from another thread, wait for its cleanup to finish before this
    # read, or finalization could close displaced connections.
    if owner._displaced:
        # #3240 item 2: skip interpreter finalization. The displaced
        # connections are parked in _DISPLACED_CONNECTIONS_KEEPALIVE, but
        # finalization would still close them (and the last close
        # checkpoints an orphan WAL over newer data). Socket and pidfile
        # were already removed by _cleanup().
        print(
            f"[engramd] exiting WITHOUT closing the store connections: the "
            f"substrate was displaced (exit {EXIT_DISPLACED}); restart the "
            f"owner against the current store",
            file=sys.stderr,
        )
        sys.stderr.flush()
        sys.stdout.flush()
        os._exit(EXIT_DISPLACED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
