"""EngramdClient -- owner wire-contract client library (#2652 Phase-1 Unit 1).

Pure client library for talking to `engramd`, the future single owner of
`knowledge.db` (engram-alpha#2652). **There is no owner to talk to yet** --
U3 builds the real owner process. This unit defines both sides of the wire
protocol and ships a client that speaks it; `tests/test_engramd_client.py`
builds a small fake socket server as its own test fixture, since no real
owner exists to test against.

Full design context: docs/2652-engrammanager-design.md SS5 (Rider 2 -- what
a caller sees when the owner is down).

NAMING NOTE (read before reaching for a familiar-looking name): this module
is deliberately NOT named `engram_client.py` and its class is deliberately
NOT named `EngramClient`. `src/engram/engram_client.py` already exists and
is a completely different, unrelated, actively-used module -- a
direct-Python-API wrapper for external LLM tool-use integration
(Gemini/OpenAI/Ollama function calling against the MCP tool functions). This
module is named after the owner process it talks to, `engramd`
(engram-alpha#2652's issue body: "Owner = the existing surface daemon,
promoted... it becomes `engramd`"), and its class is `EngramdClient` (note
the trailing `d`) -- a distinct name, in a distinct file, with zero
relationship to `engram_client.py`/`EngramClient`.

## Wire protocol

Newline-delimited JSON over AF_UNIX/SOCK_STREAM -- the same framing style
already used by the existing recall-daemon socket client (see
`src/engram/hooks/claude/engram-lesson-tripwire-hook.py`,
`_query_recall_daemon`), but written fresh here: `_query_recall_daemon`
collapses every failure into `None` (the manufactured-null anti-pattern
SS5.1 of the design doc forbids), which is precisely what this module exists
not to do.

Request (client -> owner), one JSON object per line::

    {"contract_version": "1.0", "method": "<name>", "params": {...}, "request_id": "<uuid-string-or-null>"}

`contract_version` is sent on every request (not a separate handshake step).
`request_id` is null/omitted for reads, always a non-null string for writes.

Response (owner -> client), one JSON object per line -- only two wire-level
states, because "unavailable" is never something the owner sends (if it can
respond at all, it is by definition not unavailable -- that state is purely
the CLIENT's own conclusion from a transport failure or timeout)::

    {"status": "ok", "result": <any-json-value>}
    {"status": "rejected", "reason": "<short-code>", "detail": "<human-readable>", "remedy": "<what-to-do>"}

## Result types

Three states, distinct at the TYPE level -- `Ok` / `Unavailable` /
`Rejected` -- never collapsed into one error class with a discriminator
field. `Unavailable` and `Rejected` have opposite remedies (restart the
owner vs. fix the call); a caller that cannot tell them apart at the type
level will retry a permanent rejection forever.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

CONTRACT_VERSION = "1.0"  # MAJOR.MINOR -- this client's own wire-contract version.
DEFAULT_TIMEOUT = 2.0  # seconds -- matches _query_recall_daemon's sock.settimeout(2) precedent.
DEFAULT_SOCKET_NAME = "engramd.sock"  # distinct from the existing recall-daemon.sock (a different service).
# The socket's containing subdirectory, relative to $ENGRAM_HOME (#3194,
# P1-U4a -- the owner socket permission layer). $ENGRAM_HOME itself must
# stay group/ACL-readable (the viz server still reads each seat's DB
# directly there), so the socket cannot live directly in $ENGRAM_HOME --
# it needs its own dedicated, ACL-stripped, 0700 directory. Borges's
# ruling on issue #3194: keep the shipped socket FILENAME
# (DEFAULT_SOCKET_NAME) and move only its parent directory -- the property
# §2.1 of docs/2652-security-slice-design.md asks for is the directory,
# not the name.
SOCKET_DIR_NAME = "run"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MiB -- generous for a JSON control-plane response,
# small enough to bound a misbehaving owner that never completes a line (round-5 should-fix 1).


# ---------------------------------------------------------------------------
# Result types -- Ok / Unavailable / Rejected, distinct at the type level
# ---------------------------------------------------------------------------
#
# Deliberately NOT implemented on Unavailable/Rejected: __len__, __iter__,
# __eq__ against None/empty containers, or any __bool__ override. Plain
# dataclass instances are truthy by default with no code needed here; adding
# any of the above risks accidentally making an error result look like
# "empty-but-present" data to a caller who does `if result:` instead of an
# isinstance() check -- exactly the manufactured-null failure mode
# docs/2652-engrammanager-design.md SS5.1 forbids. Leave this section alone.


@dataclass(frozen=True)
class Ok:
    """The operation completed and the owner read the committed row back
    (for writes) or answered (for reads)."""

    result: Any
    request_id: str | None = None  # non-None for writes, None for reads

    @property
    def value(self) -> Any:
        return self.result


@dataclass(frozen=True)
class Unavailable:
    """The owner could not be reached, or did not answer within the bound.

    This is purely the CLIENT's own conclusion from a transport failure or
    a timeout -- the owner never sends this status itself (see module
    docstring: if it can respond at all, it is by definition not
    unavailable).
    """

    socket_path: str
    bound: float  # the timeout, in seconds, that was exceeded
    agent_steps: list[str]  # non-empty: what the agent can try itself
    human_steps: list[str]  # non-empty: what only the user can do
    detail: str = ""
    request_id: str | None = None

    @property
    def value(self) -> Any:
        raise AttributeError(
            "Unavailable has no value -- check the result's type (isinstance) "
            "before reading .value; this is not an answer."
        )


@dataclass(frozen=True)
class Rejected:
    """The owner was reached and refused the request (schema, contract
    version, validation, ...)."""

    reason: str
    detail: str
    remedy: str
    request_id: str | None = None
    # The owner's optional top-level `commit` key, verbatim. Today only
    # snapshot()'s timeout response sets it, to "indeterminate" (a commit was
    # reserved before the deadline; dest is atomically old or new). None on
    # every other rejection -- for a `deadline_exceeded` rejection that means
    # the destination was not touched. Added LAST, defaulted, so positional
    # construction and equality of existing callers are unchanged.
    commit: str | None = None

    @property
    def value(self) -> Any:
        raise AttributeError(
            "Rejected has no value -- check the result's type (isinstance) "
            "before reading .value; this is not an answer."
        )


EngramdResult = Ok | Unavailable | Rejected


# ---------------------------------------------------------------------------
# Contract-version comparison
# ---------------------------------------------------------------------------
#
# This is the SHIPPED policy -- U3's real owner process calls this on every
# incoming request (docs/2652-engrammanager-design.md SS5). It must live
# here, not only reimplemented inside a test fixture, or no possible change
# to this module could ever be caught by a version-policy test (round-5
# should-fix 2 -- before this function existed, the version comparison
# logic lived only in tests/test_engramd_client.py's own fake-server
# handler, so test 5 exercised the fake's reimplementation, never this
# module).


def check_contract_version(sent: str, owner: str = CONTRACT_VERSION) -> Rejected | None:
    """Compare a sent contract_version against the owner's own version.
    MAJOR mismatch or a malformed version string -> Rejected(reason="contract_version").
    MINOR skew (same MAJOR, different MINOR) -> None (accepted)."""
    sent_parsed = _parse_major_minor(sent)
    owner_parsed = _parse_major_minor(owner)
    if sent_parsed is None or owner_parsed is None:
        return Rejected(
            reason="contract_version",
            detail=f"malformed contract_version: sent={sent!r}, owner={owner!r}",
            remedy="restart the owner (engramd) on a compatible version",
        )
    sent_major, _sent_minor = sent_parsed
    owner_major, _owner_minor = owner_parsed
    if sent_major != owner_major:
        return Rejected(
            reason="contract_version",
            detail=f"client sent major version {sent_major}, owner is on major version {owner_major}",
            remedy="restart the owner (engramd) on a compatible version",
        )
    return None


def _parse_major_minor(version: str) -> tuple[int, int] | None:
    """Parse a MAJOR.MINOR version string. Returns None (never raises) for
    anything that doesn't parse cleanly -- missing dot, non-numeric parts,
    empty string, extra segments -- so a malformed version is a normal
    Rejected outcome from check_contract_version(), never a crash."""
    if not isinstance(version, str):
        return None
    parts = version.split(".")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Socket path resolution
# ---------------------------------------------------------------------------


def _resolve_socket_path(socket_path: Path | None) -> Path:
    """Default socket path is $ENGRAM_HOME/run/engramd.sock (#3194 -- the
    socket moved into a dedicated subdirectory so the permission layer can
    give it its own 0700, ACL-stripped directory without touching
    $ENGRAM_HOME's own mode/ACLs, which other consumers, e.g. viz, still
    need to read), falling back to ~/.engram when ENGRAM_HOME is unset --
    the same resolution idiom already used elsewhere in this codebase (see
    src/engram/hooks/claude/engram-lesson-tripwire-hook.py:77)."""
    if socket_path is not None:
        return socket_path
    engram_home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
    return Path(engram_home) / SOCKET_DIR_NAME / DEFAULT_SOCKET_NAME


# ---------------------------------------------------------------------------
# Unavailable-result construction (shared by every _call() failure branch)
# ---------------------------------------------------------------------------


def _unavailable(
    *, socket_path: str, bound: float, detail: str, request_id: str | None
) -> Unavailable:
    """Build an Unavailable result carrying concrete, actionable recovery
    guidance in the response payload itself (docs/2652-engrammanager-design.md
    SS5.5: guidance travels in the payload, not a log line, and the two kinds
    -- agent-actionable vs. human-only -- are distinguishable without parsing
    prose)."""
    agent_steps = [
        "Retry the call after a short backoff -- engramd may recover on its own.",
        f"Check whether the engramd process is running and (re)start it if not "
        f"(the owner should be listening on {socket_path}).",
    ]
    human_steps = [
        "If retries keep failing, restart your Claude Code session.",
        "Run /mcp to reconnect the MCP server, or check the engramd logs for "
        "the underlying failure.",
    ]
    return Unavailable(
        socket_path=socket_path,
        bound=bound,
        agent_steps=agent_steps,
        human_steps=human_steps,
        detail=detail,
        request_id=request_id,
    )


def _arm_timeout_or_unavailable(
    sock: socket.socket,
    *,
    deadline: float,
    socket_path: str,
    bound: float,
    request_id: str | None,
) -> Unavailable | None:
    """Recompute the remaining budget against `deadline` and arm `sock`'s
    timeout for the NEXT blocking op with that freshly-recomputed value.
    Returns an Unavailable result if the budget is already exhausted (the
    caller must stop immediately, without attempting the op); returns None
    otherwise (armed, proceed).

    Called before EVERY blocking socket operation in _call() -- connect(),
    sendall(), and each iteration of the recv() loop -- so a slowly-
    trickling owner cannot make the total call take an unbounded multiple
    of `bound` the way a single settimeout(bound) set once at entry would
    (round-5 should-fix 1; docs/2652-engrammanager-design.md SS5.1).
    Recomputing `remaining` between ops alone, without re-arming
    settimeout() with that exact value each time, would under-specify and
    can still overshoot -- both steps are required together."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _unavailable(
            socket_path=socket_path,
            bound=bound,
            detail="owner did not respond within the bound",
            request_id=request_id,
        )
    sock.settimeout(remaining)
    return None


# ---------------------------------------------------------------------------
# The one transport primitive
# ---------------------------------------------------------------------------


def _call(
    method: str,
    params: dict,
    *,
    request_id: str | None,
    timeout: float,
    socket_path: Path,
) -> EngramdResult:
    """The one place that touches the socket. Every transport failure path
    maps to Unavailable; every owner-sent {"status":"rejected",...} maps to
    Rejected; every {"status":"ok",...} maps to Ok. This is the ONLY
    function with `except` clauses around socket I/O in this module.

    `timeout` is an END-TO-END deadline for the whole call, not a
    per-socket-op budget: socket.settimeout() applies its configured value
    to EACH blocking call independently (connect/sendall/each recv), so a
    fixed settimeout(timeout) set once at the top lets a slowly-trickling
    owner make the total call take an unbounded multiple of the requested
    bound (docs/2652-engrammanager-design.md SS5.1 -- "a hang is
    indistinguishable from a slow graph"; round-5 should-fix 1). Instead,
    `deadline` is computed once, and `_arm_timeout_or_unavailable()` is
    called before EVERY blocking op to recompute the remaining budget and
    re-arm the socket's timeout with that fresh value -- see its own
    docstring for why both steps are required together.

    An unparseable owner response, or a response carrying an unrecognized
    `status` value, is classified as Unavailable (not Rejected) -- a
    deliberate choice: it is a protocol failure, and the remedy is to
    restart the owner, not to fix the call's params.
    """
    sp = str(socket_path)
    request = {
        "contract_version": CONTRACT_VERSION,
        "method": method,
        "params": params,
        "request_id": request_id,
    }
    payload = (json.dumps(request) + "\n").encode("utf-8")

    deadline = time.monotonic() + timeout
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        exhausted = _arm_timeout_or_unavailable(
            sock, deadline=deadline, socket_path=sp, bound=timeout, request_id=request_id
        )
        if exhausted is not None:
            return exhausted
        sock.connect(sp)

        exhausted = _arm_timeout_or_unavailable(
            sock, deadline=deadline, socket_path=sp, bound=timeout, request_id=request_id
        )
        if exhausted is not None:
            return exhausted
        sock.sendall(payload)

        data = b""
        while True:
            exhausted = _arm_timeout_or_unavailable(
                sock, deadline=deadline, socket_path=sp, bound=timeout, request_id=request_id
            )
            if exhausted is not None:
                return exhausted
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_RESPONSE_BYTES:
                return _unavailable(
                    socket_path=sp,
                    bound=timeout,
                    detail=f"owner response exceeded the {MAX_RESPONSE_BYTES}-byte cap without completing a line",
                    request_id=request_id,
                )
            if b"\n" in data:
                break
    except socket.timeout:
        # The bounded-wait-and-give-up path. Kept as its own clause, distinct
        # from the connection-refused/socket-missing path below, so the two
        # are separately identifiable/mutable -- see contract test 8's
        # discrimination control in tests/test_engramd_client.py.
        return _unavailable(
            socket_path=sp,
            bound=timeout,
            detail="owner did not respond within the bound",
            request_id=request_id,
        )
    except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
        # socket.timeout (== TimeoutError as of Python 3.10) is itself an
        # OSError subclass, so this clause MUST come after the timeout
        # clause above, never merged with it -- a merged `except Exception`
        # (or an OSError-first ordering) would swallow the timeout branch.
        return _unavailable(
            socket_path=sp,
            bound=timeout,
            detail=str(exc),
            request_id=request_id,
        )
    finally:
        sock.close()

    if not data:
        return _unavailable(
            socket_path=sp,
            bound=timeout,
            detail="owner closed the connection without responding",
            request_id=request_id,
        )

    try:
        response = json.loads(data.decode("utf-8").strip())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _unavailable(
            socket_path=sp,
            bound=timeout,
            detail=f"owner sent an unparseable response: {exc}",
            request_id=request_id,
        )

    status = response.get("status") if isinstance(response, dict) else None
    if status == "ok":
        return Ok(result=response.get("result"), request_id=request_id)
    if status == "rejected":
        return Rejected(
            reason=response.get("reason", ""),
            detail=response.get("detail", ""),
            remedy=response.get("remedy", ""),
            request_id=request_id,
            commit=response.get("commit"),
        )
    return _unavailable(
        socket_path=sp,
        bound=timeout,
        detail=f"owner sent an unrecognized status: {status!r}",
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Public read/write API split
# ---------------------------------------------------------------------------


def read(
    method: str,
    params: dict,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    socket_path: Path | None = None,
) -> EngramdResult:
    """No request_id concept at all -- the read path structurally cannot
    carry one, which is half of what makes 'impossible to send a write
    without a request_id' true (the other half is write()'s own guarantee
    below).

    `snapshot()` (there is no separate wrapper: it is `read("snapshot",
    {"dest_path": ...})`): an `Unavailable` result -- a dropped connection --
    must be read as INDETERMINATE, never as "nothing happened" and never as
    success. The owner stops snapshot execution `SNAPSHOT_RESPONSE_RESERVE`
    before the request deadline so its timeout responses (`Rejected` with
    reason `deadline_exceeded`, whose wire form may carry `commit:
    "indeterminate"`) normally arrive; the reserve makes a missed delivery
    rare, not impossible (scheduler stalls). The destination is atomically
    either the old file or the complete new snapshot, never partial, so
    the client-visible forms `commit: "indeterminate"` and `Unavailable`
    both mean "go look at dest_path"; neither ever means success.
    `Rejected.commit == "indeterminate"` is the typed marker for the
    committing outcome; `commit is None` on a `deadline_exceeded` rejection
    means the destination was not touched; `Unavailable` remains
    indeterminate."""
    return _call(
        method,
        params,
        request_id=None,
        timeout=timeout,
        socket_path=_resolve_socket_path(socket_path),
    )


def write(
    method: str,
    params: dict,
    *,
    request_id: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    socket_path: Path | None = None,
) -> EngramdResult:
    """Always ensures a request_id exists before constructing the wire
    message -- caller-supplied (for an explicit retry of a specific prior
    attempt) or freshly generated (uuid4, as a string) otherwise. The
    request_id used is echoed back on EVERY returned result type (Ok,
    Unavailable, AND Rejected) so a caller that got Unavailable can retry
    by passing that same id back in."""
    rid = request_id or str(uuid.uuid4())
    return _call(
        method,
        params,
        request_id=rid,
        timeout=timeout,
        socket_path=_resolve_socket_path(socket_path),
    )
