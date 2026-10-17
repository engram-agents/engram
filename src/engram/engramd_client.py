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

import errno
import json
import os
import platform
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import engram_pidfile

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# MAJOR.MINOR -- this client's own wire-contract version (the owner reuses it:
# engramd_owner takes ec.CONTRACT_VERSION, so there is exactly one copy).
# "1.1" (U4d, #3197): the owner gained the client-facing `read` method -- an
# added method is a MINOR change (docs/2652-engrammanager-design.md SS5.3).
# "1.2" (U11, #3822): the owner gained its first WRITE, the typed `use_reward` method -- again an added
# method, so MINOR (`check_contract_version` tolerates minor skew; an older owner answers a
# `use_reward` call with Rejected(unknown_operation), which the caller must treat as loud).
# "1.3" (U13, #3856): the owner gained its second WRITE, the typed `advance_moment_cursor` method -- again an
# added method, so MINOR (an older owner answers it with Rejected(unknown_operation), which the caller must treat
# as loud). It takes no request_id: the compare-and-set makes a replay harmless.
CONTRACT_VERSION = "1.3"
# The `use_reward` write (#3822): the timeout a caller passes, and the slice of it reserved for the reply to
# travel. The owner's total waiting budget (engramd_owner.OWNER_WRITE_LOCK_WAIT, 1.0 s) must stay strictly below
# OWNER_WRITE_CLIENT_TIMEOUT - OWNER_WRITE_REPLY_RESERVE (1.5 s), so a `busy` refusal ARRIVES before the client
# gives up; a test pins the inequality across the two modules. 2.0 s is DEFAULT_TIMEOUT; the 0.5 s reserve is a
# judgement (loopback AF_UNIX reply plus a scheduler hiccup), not a measurement.
OWNER_WRITE_CLIENT_TIMEOUT = 2.0
OWNER_WRITE_REPLY_RESERVE = 0.5
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
# The owner's pidfile name (relative to $ENGRAM_HOME) and the argv substring of
# a live owner (`python .../engramd_owner.py`, `python -m engramd_owner`), the
# program-identity guard in engram_pidfile (#3240). They live here, not in the
# owner, because the client reads the pidfile on a Darwin refused connect()
# (#3707) and the client must never import the owner; the owner aliases them.
DEFAULT_PID_NAME = "engramd.pid"
PIDFILE_PROGRAM_MARKER = "engramd_owner"
# How long a caller should retry a refused / busy owner before treating it as
# stuck (#3707). A JUDGEMENT, not a measurement: 5x the 2 s per-call
# DEFAULT_TIMEOUT. There is no data on how long a full accept backlog takes to
# drain. One constant so the Linux busy remedy and the Darwin busy_or_stuck
# remedy say the same number.
OWNER_BUSY_RETRY_SECONDS = 10
# The largest `timeout` a call accepts (#3717). A judgement with headroom, not a
# measurement: callers pass 2-2.5 s today (DEFAULT_TIMEOUT; the stop hook's
# OWNER_READ_TIMEOUT_S), and the platform's settimeout overflows near 9.2e9 s, so
# one hour is far above any real use and far below the overflow. A larger finite
# value is refused as Rejected(invalid_timeout) before any I/O.
MAX_CALL_TIMEOUT_SECONDS = 3600
# Longest AF_UNIX socket path, in BYTES, that bind()/connect() accept (#3696).
# Linux: sun_path is 108 bytes including the NUL, so 107 usable. MEASURED on a
# Linux 6.6 seat (a 107-byte path binds; a 108-byte path raises "AF_UNIX path
# too long").
LINUX_SOCKET_PATH_MAX_BYTES = 107
# Darwin (#3666): sun_path is 104 per the platform header, but the usable
# length is 103. MEASURED on macOS 26.6.2 / Python 3.12.10 (a 103-byte path
# binds; a 104-byte path raises "AF_UNIX path too long").
DARWIN_SOCKET_PATH_MAX_BYTES = 103


def platform_arm() -> str | None:
    """Which platform arm this seat is on (#3712): "linux", "darwin", or None
    for any other platform. Keyed on `platform.system()` ALONE -- the single
    source of the platform decision, which lives in engram_pidfile.platform_arm()
    (#3707: the client imports that module, so the source moved there). This
    delegates, so the owner's `_owner_arm()` -> here -> engram_pidfile chain has
    exactly one body."""
    return engram_pidfile.platform_arm()


# The limit on THIS seat -- the one value socket_path_too_long_reason() and
# so both the owner's refusal and the client's Unavailable detail use.
# Computed once at import from platform_arm(); any non-darwin arm (including
# None) keeps the Linux limit, as before.
SOCKET_PATH_MAX_BYTES = (
    DARWIN_SOCKET_PATH_MAX_BYTES if platform_arm() == "darwin" else LINUX_SOCKET_PATH_MAX_BYTES
)
# Cap on one INCOMING request line, in bytes, newline included (#3707 round 3).
# The single source: the owner takes it from here (engramd_owner aliases it) and
# the client checks the encoded request against it BEFORE connecting, so an
# oversize request is a Rejected the caller can act on, never a broken pipe
# discovered at sendall(). The owner counts every byte it has received on the
# connection and drops (without replying) once that total is > this cap; the
# client sends exactly `payload` (the JSON line + "\n"), so `len(payload) > cap`
# is the same comparison. Requests are small control-plane JSON, so 1 MiB is
# generous headroom that still bounds a runaway or hostile peer.
MAX_REQUEST_BYTES = 1 * 1024 * 1024  # 1 MiB
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
    # Why the owner was unavailable, as a code a caller can act on without
    # parsing prose (#3707), by MEANING:
    #   "down"          no owner listening (ENOENT/ENOTSOCK/refused, no live owner;
    #                   socket path over the AF_UNIX limit); restart it.
    #   "timeout"       no valid reply within the bound; the owner may be alive.
    #   "protocol"      the owner was reached, but the exchange failed without a valid
    #                   answer (response over the cap, empty, unparseable, an
    #                   unrecognized status, or an OSError after connect()).
    #   "local"         this client process could not create a socket (EMFILE /
    #                   ENFILE / ENOBUFS); nothing is known about the owner.
    #   "internal"      the last-resort guard (#3717): an unexpected exception inside
    #                   the client. A client defect to report; nothing is known about
    #                   the owner. Tests assert no generated input reaches it.
    #   "busy"          Linux full accept backlog; owner healthy, retry.
    #   "busy_or_stuck" Darwin refused connect() with a live owner pid; retry,
    #                   then stop it and restart.
    # Added LAST, defaulted, so positional construction and equality
    # of existing callers are unchanged (the Rejected.commit pattern).
    reason: str | None = None
    # The live owner's pid when reason == "busy_or_stuck" and the pidfile was
    # found; None on every other path.
    owner_pid: int | None = None

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
    return socket_path_for_home(Path(engram_home))


def socket_path_for_home(home: Path) -> Path:
    """<home>/run/engramd.sock -- the one place the owner socket path is
    composed (the owner uses this too)."""
    return Path(home) / SOCKET_DIR_NAME / DEFAULT_SOCKET_NAME


def pid_path_for_home(home: Path) -> Path:
    """<home>/engramd.pid -- the one place the owner pidfile path is composed
    (the owner uses this too)."""
    return Path(home) / DEFAULT_PID_NAME


def _pid_path_for_socket(socket_path: Path) -> Path | None:
    """The pidfile for an owner socket at the standard layout
    (<home>/run/engramd.sock), else None (location unknown). An explicit
    `socket_path` that is not BOTH inside a directory named SOCKET_DIR_NAME AND
    named DEFAULT_SOCKET_NAME (e.g. /var/run/foo.sock) has no knowable home, so
    no pidfile is guessed."""
    socket_path = Path(socket_path)
    if socket_path.parent.name != SOCKET_DIR_NAME or socket_path.name != DEFAULT_SOCKET_NAME:
        return None
    return socket_path.parent.parent / DEFAULT_PID_NAME


def socket_path_too_long_reason(socket_path: Path | str) -> str | None:
    """None when `socket_path` fits an AF_UNIX address; otherwise the reason
    text (byte length, limit, remedy). The ONE source of that text: the owner
    raises it as OwnerSocketPathTooLong and the client returns it as the
    Unavailable detail, so the two cannot disagree (#3696). Length is in
    BYTES (os.fsencode), as the kernel counts, never characters."""
    n = len(os.fsencode(socket_path))
    if n <= SOCKET_PATH_MAX_BYTES:
        return None
    return (
        f"engramd socket path is {n} bytes, over the {SOCKET_PATH_MAX_BYTES}-byte "
        f"AF_UNIX limit: {socket_path}. Shorten ENGRAM_HOME (the socket lives at "
        f"<ENGRAM_HOME>/{SOCKET_DIR_NAME}/{DEFAULT_SOCKET_NAME}, which adds "
        f"{len('/' + SOCKET_DIR_NAME + '/' + DEFAULT_SOCKET_NAME)} bytes)."
    )


# ---------------------------------------------------------------------------
# Unavailable-result construction (shared by every _call() failure branch)
# ---------------------------------------------------------------------------


# The remedy by reason (#3707 round 3, design-seat ruling: the remedy text
# follows the code). ONE table, (agent_steps, human_steps) templates per reason,
# filled with {socket_path} and {n} (OWNER_BUSY_RETRY_SECONDS) at build time.
#   down      no owner listening: the restart step is right here and ONLY here.
#   timeout   no valid reply within the bound; a timeout was measured on a live,
#             SIGSTOPped owner, so the escalation is stop-then-restart, never a
#             bare restart.
#   protocol  the owner was reached and the exchange failed. No claim about the
#             owner's state and no restart step: a post-connect EPIPE/ECONNRESET
#             is also what a crashed owner looks like, so the retry discriminates
#             (a crashed owner turns the retry into `down`, which restarts).
#   local     the CLIENT process could not create a socket (EMFILE / ENFILE /
#             ENOBUFS, #3717). Nothing is known about the owner: no claim that it
#             is up or down, and no restart-the-owner step.
# `busy` and `busy_or_stuck` keep their own builders (the latter has pid
# variants); `_BUILDER_REASONS` names them so the reason set stays complete.
_REMEDY_STEPS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "down": (
        (
            "Retry the call after a short backoff -- engramd may recover on its own.",
            "Check whether the engramd process is running and (re)start it if not "
            "(the owner should be listening on {socket_path}).",
        ),
        (
            "If retries keep failing, restart your Claude Code session.",
            "Run /mcp to reconnect the MCP server, or check the engramd logs for "
            "the underlying failure.",
        ),
    ),
    "timeout": (
        (
            "Retry the call after a short backoff.",
            "If calls keep timing out for more than {n} seconds, the owner may be "
            "stuck. Check whether the engramd process is running; if it is running "
            "but not answering, stop it, then restart it.",
        ),
        (
            "If calls keep timing out for more than {n} seconds, the owner may be "
            "stuck: if the engramd process is running but not answering, stop it, "
            "then restart it. Check the engramd logs for what is holding it.",
        ),
    ),
    "protocol": (
        (
            "The owner was reached, but the exchange failed without a valid answer. "
            "Retry once. If the retry returns `down`, follow its steps. If it fails "
            "as `protocol` again, check the engramd logs for why the owner dropped "
            "this request.",
        ),
        ("Check the engramd logs for why the owner dropped this request.",),
    ),
    "local": (
        (
            "This client's process could not open a socket (the detail gives the "
            "errno: out of file descriptors or kernel buffers). Nothing is known "
            "about the owner. Free resources in this process (close leaked sockets "
            "or files) or restart this client process, then retry. Do not restart "
            "the owner for this.",
        ),
        (
            "Restart the Claude Code session (this client process ran out of file "
            "descriptors or kernel buffers); the owner's state is unknown and "
            "restarting it will not help.",
        ),
    ),
    # #3717: the last-resort guard in _call(). A CLIENT defect: an exception no
    # mapped clause expected. It says nothing about the owner and carries no
    # restart step; it is a signal to report, not a state to recover from.
    "internal": (
        (
            "This client hit an unexpected internal error (the detail names the "
            "exception). It is a defect in the client, not a statement about the "
            "owner: nothing is known about the owner's state. Report it with the "
            "detail text; retrying the same call may not help.",
        ),
        (
            "Report this to the engram maintainers with the detail text (an "
            "unexpected exception inside the engramd client). It says nothing "
            "about the owner, and retrying may not help.",
        ),
    ),
}
_BUILDER_REASONS = ("busy", "busy_or_stuck")
# Every value Unavailable.reason can take (the table rows plus the builders').
UNAVAILABLE_REASONS = tuple(_REMEDY_STEPS) + _BUILDER_REASONS


def _unavailable(
    *, socket_path: str, bound: float, detail: str, request_id: str | None, reason: str
) -> Unavailable:
    """Build an Unavailable result carrying concrete, actionable recovery
    guidance in the response payload itself (docs/2652-engrammanager-design.md
    SS5.5: guidance travels in the payload, not a log line, and the two kinds
    -- agent-actionable vs. human-only -- are distinguishable without parsing
    prose). `reason` is REQUIRED (no default): it is set by what the site
    MEANS -- "down", "timeout", "protocol", "local" or "internal" (see
    Unavailable.reason) -- so a new call site cannot silently inherit the wrong
    code (#3707).

    The steps come from `_REMEDY_STEPS`, keyed by `reason` (the remedy follows
    the code); a reason with no row raises KeyError here, at construction."""
    agent_templates, human_templates = _REMEDY_STEPS[reason]
    fields = {"socket_path": socket_path, "n": OWNER_BUSY_RETRY_SECONDS}
    return Unavailable(
        socket_path=socket_path,
        bound=bound,
        agent_steps=[t.format(**fields) for t in agent_templates],
        human_steps=[t.format(**fields) for t in human_templates],
        detail=detail,
        request_id=request_id,
        reason=reason,
    )


def _busy_unavailable(
    *, socket_path: str, bound: float, detail: str, request_id: str | None
) -> Unavailable:
    """The owner IS up but its accept backlog is full (#3680). Still an Unavailable
    (§5.5's two-kind shape), but the remedy is "retry", never "restart the owner":
    the owner is healthy, so restarting it would drop every other client's connection.

    The claim this message makes is bounded: it holds because the call site maps
    only EAGAIN/EWOULDBLOCK raised by connect() on this client's AF_UNIX socket,
    which on Linux means a full accept backlog. Do not route any other errno or
    any other step here; a reassuring "do NOT restart" on a real outage is the
    costly failure direction."""
    return Unavailable(
        socket_path=socket_path,
        bound=bound,
        agent_steps=[
            "The owner is up but busy (its accept backlog is full). Retry the call "
            "after a short backoff.",
            "Do NOT restart the owner for this: it is healthy and will serve the "
            "retry once the burst drains.",
        ],
        human_steps=[
            f"If retries keep failing for more than {OWNER_BUSY_RETRY_SECONDS} seconds, "
            "check the engramd logs for what is holding the owner.",
        ],
        detail=f"owner busy (accept backlog full): {detail}",
        request_id=request_id,
        reason="busy",
    )


def _busy_or_stuck_unavailable(
    *,
    socket_path: str,
    bound: float,
    detail: str,
    request_id: str | None,
    owner_pid: int | None,
) -> Unavailable:
    """Darwin: connect() was REFUSED and the pidfile names a live owner (#3707).
    On Darwin a full accept backlog, a stale socket file and a bound-but-never-
    listening socket all give ECONNREFUSED, so a busy owner and a live owner
    that is not accepting look identical to the client; the pid check only
    separates "an owner exists" from "no owner". So the remedy leads with retry
    (a wrong restart drops every other client; a wrong retry costs one caller a
    few seconds) and carries the escalation: after OWNER_BUSY_RETRY_SECONDS, stop
    the named pid, then restart. Never "restart" alone, and never "do not
    restart" (the owner is not known to be healthy).

    `owner_pid` None means the pidfile location is unknown (an explicit socket
    path outside the standard layout); the remedy then says so."""
    n = OWNER_BUSY_RETRY_SECONDS
    if owner_pid is not None:
        who = f"pid {owner_pid}"
        agent_steps = [
            f"The owner process ({who}) exists but refused the connection: it is "
            f"busy (accept backlog full) or not accepting connections. Retry the call "
            f"after a short backoff, for up to {n} seconds.",
            f"If it is still refused after {n} seconds, owner {who} is not accepting "
            f"connections. First confirm {who} is still the engramd owner (its command "
            f"line contains {PIDFILE_PROGRAM_MARKER}, e.g. `ps -p {owner_pid} -o command=`): "
            f"the pid was checked when the call failed, and it may have been reused since. "
            f"If it is, stop {who}, then restart the owner. If it is not, the owner has "
            f"exited: do not stop that pid; restart the owner.",
        ]
        human_steps = [
            f"Retry for up to {n} seconds. If the connection is still refused, owner "
            f"{who} is not accepting connections. Confirm {who} is still the engramd "
            f"owner (`ps -p {owner_pid} -o command=` shows {PIDFILE_PROGRAM_MARKER}) "
            f"before stopping it, then restart the owner; if it is not, do not stop it. "
            "Check the engramd logs for what is holding it.",
        ]
        detail_text = f"owner busy or stuck (connection refused, owner alive) pid {owner_pid}: {detail}"
    else:
        agent_steps = [
            "An owner process may exist but refused the connection (the pidfile location "
            "is unknown for this socket path, so its pid is unknown): it is busy "
            "or not accepting connections. Retry the call after a short backoff, for up "
            f"to {n} seconds.",
            f"If it is still refused after {n} seconds, the owner's pid is unknown: find "
            "and stop the running owner process before any restart.",
        ]
        human_steps = [
            f"Retry for up to {n} seconds. If the connection is still refused, the owner's "
            "pid is unknown: find and stop the running owner process, then restart it. "
            "Check the engramd logs for what is holding it.",
        ]
        detail_text = f"owner busy or stuck (connection refused, owner pid unknown): {detail}"
    return Unavailable(
        socket_path=socket_path,
        bound=bound,
        agent_steps=agent_steps,
        human_steps=human_steps,
        detail=detail_text,
        request_id=request_id,
        reason="busy_or_stuck",
        owner_pid=owner_pid,
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
            reason="timeout",
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
    """The one place that touches the socket. NEVER RAISES (#3717): it returns Ok,
    Unavailable or Rejected on every path, by construction. Every transport
    failure maps to Unavailable (seven reason codes: down, timeout, protocol,
    busy, busy_or_stuck, local, internal); a request that cannot be serialized,
    or a `timeout` that is not a finite number, maps to Rejected
    (request_not_serializable / invalid_timeout) before any socket exists; every
    owner-sent {"status":"rejected",...} maps to Rejected; every
    {"status":"ok",...} maps to Ok. This is the ONLY function with `except`
    clauses around socket I/O in this module.

    The bound, enumerated by EXCEPTION per statement (Python docs for each call),
    not by input. Each exception lands on a mapped result below; any exception
    NOT listed lands on the last-resort guard, `Unavailable(reason="internal")`,
    which is a SIGNAL (a client defect to report), not a mapping, and which the
    generative cell in tests/test_engramd_client_never_raises_3717.py asserts no
    generated input ever reaches.
      timeout check (None, str, bool, NaN, +-inf,
        finite > MAX_CALL_TIMEOUT_SECONDS)              -> Rejected(invalid_timeout)
      json.dumps(request): TypeError, ValueError (also
        NaN/inf via allow_nan=False, circular reference,
        int over the digit limit), RecursionError       -> Rejected(request_not_serializable)
      .encode("utf-8")                                  -> cannot raise (ensure_ascii)
      len(payload) > MAX_REQUEST_BYTES                  -> Rejected(request_too_large)
      socket_path_too_long_reason                       -> Unavailable(down) when too long;
        a str path with a lone surrogate raises UnicodeEncodeError in os.fsencode
        -> `internal` (a caller defect; paths from the environment use
        surrogateescape and never reach it)
      time.monotonic() + timeout                        -> finite numeric timeouts only (checked
        above); zero/negative clamped to 0 for the arithmetic -> Unavailable(timeout)
      socket.socket(): OSError (EMFILE/ENFILE/ENOBUFS)  -> Unavailable(local)
      sock.settimeout / connect: socket.timeout         -> Unavailable(timeout)
        EAGAIN/EWOULDBLOCK on connect                   -> Unavailable(busy)
        ECONNREFUSED on Darwin + live pidfile           -> Unavailable(busy_or_stuck)
        any other OSError before connect returned       -> Unavailable(down)
      sendall / recv: socket.timeout                    -> Unavailable(timeout)
        any other OSError after connect returned        -> Unavailable(protocol)
      response over MAX_RESPONSE_BYTES, no newline,
        or closed with no data                          -> Unavailable(protocol)
      json.loads: ValueError (JSONDecodeError, and an
        int over the digit limit), UnicodeDecodeError
        (non-UTF-8), RecursionError                     -> Unavailable(protocol)
      unknown / non-dict status                         -> Unavailable(protocol)
    Out of bounds, deliberately: KeyboardInterrupt and SystemExit (BaseException;
    the guard catches `Exception` only, so they propagate). Also outside this
    function, and so outside the guard: `Path.home()` RuntimeError in
    `_resolve_socket_path` (read()/write() call it before `_call`) when no home
    directory can be determined.

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
    deliberate choice: it is a protocol failure (reason="protocol"), not a
    problem with the call's params. Its steps make no claim about the owner's
    state and carry no restart: retry once, and a crashed owner turns the retry
    into `down` (#3707).
    """
    # #3717 last-resort guard. `Exception`, never BaseException. The socket is
    # closed by _call_mapped's own `finally`, so this path cannot leak it.
    try:
        return _call_mapped(
            method, params, request_id=request_id, timeout=timeout, socket_path=socket_path
        )
    except Exception as exc:  # noqa: BLE001 -- the guard is the point
        try:
            detail = f"{type(exc).__name__}: {exc}"
        except Exception:  # noqa: BLE001 -- an exception whose __str__ itself raises
            detail = type(exc).__name__
        bound = 0.0
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            try:
                bound = float(timeout)
            except OverflowError:
                bound = 0.0
        return _unavailable(
            socket_path=str(socket_path), bound=bound,
            detail=f"unexpected internal error: {detail}", request_id=request_id,
            reason="internal",
        )


def _call_mapped(
    method: str,
    params: dict,
    *,
    request_id: str | None,
    timeout: float,
    socket_path: Path,
) -> EngramdResult:
    """`_call`'s body: every known exception is mapped here (see `_call`'s table).
    Call `_call`, never this, so the last-resort guard applies."""
    # #3717: a timeout that is not a finite real number would raise further down
    # (None: TypeError; NaN: ValueError; inf: OverflowError), and so would a finite
    # one too large for the platform (settimeout overflows near 9.2e9 s). Refuse
    # both before any I/O; the cap is MAX_CALL_TIMEOUT_SECONDS. bool is excluded
    # (True would silently mean 1 second). Written with comparisons only:
    # math.isfinite(10**400) itself raises OverflowError, while 10**400 > 3600 does not.
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout != timeout
        or timeout in (float("inf"), float("-inf"))
        or timeout > MAX_CALL_TIMEOUT_SECONDS
    ):
        return Rejected(
            reason="invalid_timeout",
            detail=f"{timeout!r} ({type(timeout).__name__})",
            remedy=f"pass a finite number of seconds, at most {MAX_CALL_TIMEOUT_SECONDS}",
            request_id=request_id,
        )

    sp = str(socket_path)
    request = {
        "contract_version": CONTRACT_VERSION,
        "method": method,
        "params": params,
        "request_id": request_id,
    }
    # #3717: a request that cannot be serialized (a set, bytes, datetime or object
    # in params; a circular structure; NaN / Infinity, which json would otherwise
    # encode to non-JSON tokens) is the CALLER's error, decidable before any I/O.
    # Refuse it as Rejected here so _call never raises; this runs BEFORE the size
    # check because the size cannot be measured until serialization succeeds.
    try:
        payload = (json.dumps(request, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        return Rejected(
            reason="request_not_serializable",
            detail=f"{type(exc).__name__}: {exc}",
            remedy=(
                "make the params JSON-serializable: no sets, bytes, datetimes, "
                "objects, NaN/Infinity or circular references"
            ),
            request_id=request_id,
        )

    # #3707 round 3: an oversize request is the CALLER's error and the client can
    # know it before sending. Refuse it here as Rejected (the owner's availability
    # is not in question), never discover it as a broken pipe after connect().
    # Same comparison the owner makes (bytes received, newline included, > cap),
    # exact for an owner on the default cap; an owner built with a smaller
    # max_request_bytes (tests do this) can still drop a request this lets through.
    if len(payload) > MAX_REQUEST_BYTES:
        return Rejected(
            reason="request_too_large",
            detail=f"request is {len(payload)} bytes, over the {MAX_REQUEST_BYTES}-byte cap",
            remedy="shrink the request or split it into smaller requests",
            request_id=request_id,
        )

    # #3696: an over-long path can never connect; say why (same text as the
    # owner's refusal) before any socket exists.
    too_long = socket_path_too_long_reason(socket_path)
    if too_long is not None:
        return _unavailable(
            socket_path=sp, bound=timeout, detail=too_long, request_id=request_id,
            reason="down",
        )

    # #3717: a zero or negative timeout means "already out of budget" (immediate
    # Unavailable(timeout), as ruled). Clamp it to 0 for the deadline arithmetic
    # only, so a huge negative int (-10**400) cannot overflow `monotonic() + x`;
    # `bound` still reports the caller's value.
    deadline = time.monotonic() + (timeout if timeout > 0 else 0)
    # #3717: creating the socket can fail in THIS process (EMFILE / ENFILE /
    # ENOBUFS). That says nothing about the owner, so it is `local`, never `down`.
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        return _unavailable(
            socket_path=sp, bound=timeout, detail=str(exc), request_id=request_id,
            reason="local",
        )
    connected = False  # set once connect() returns: later OSErrors are `protocol`
    try:
        exhausted = _arm_timeout_or_unavailable(
            sock, deadline=deadline, socket_path=sp, bound=timeout, request_id=request_id
        )
        if exhausted is not None:
            return exhausted
        try:
            sock.connect(sp)
        except OSError as exc:
            # Only the CONNECT step: an AF_UNIX connect() against a full accept
            # backlog fails at once with EAGAIN (#3680) -- the owner is up and busy,
            # not down. EAGAIN from sendall()/recv() is not that, so it keeps the
            # generic path below. Anything else (ENOENT, ECONNREFUSED, the timeout)
            # is re-raised to the clauses below, unchanged.
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return _busy_unavailable(
                    socket_path=sp, bound=timeout, detail=str(exc), request_id=request_id
                )
            # Darwin only (#3707): a full backlog fails with ECONNREFUSED there, the
            # same errno as "owner gone". A pidfile naming a LIVE owner means an owner
            # exists and refused: busy_or_stuck. Pidfile missing / dead / reused ->
            # re-raise, so the down Unavailable below results. Pidfile location
            # unknown (explicit non-standard socket path) leans to busy_or_stuck
            # without a pid, never to "restart". Never on ENOENT, ENOTSOCK, a
            # timeout or any later step, or on Linux.
            if exc.errno == errno.ECONNREFUSED and platform_arm() == "darwin":
                pid_path = _pid_path_for_socket(socket_path)
                if pid_path is None:
                    return _busy_or_stuck_unavailable(
                        socket_path=sp, bound=timeout, detail=str(exc),
                        request_id=request_id, owner_pid=None,
                    )
                live_pid = engram_pidfile.recorded_live_pid(
                    pid_path, program_marker=PIDFILE_PROGRAM_MARKER
                )
                if live_pid is not None:
                    return _busy_or_stuck_unavailable(
                        socket_path=sp, bound=timeout, detail=str(exc),
                        request_id=request_id, owner_pid=live_pid,
                    )
            raise
        connected = True

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
                    reason="protocol",
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
            reason="timeout",
        )
    except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
        # socket.timeout (== TimeoutError as of Python 3.10) is itself an
        # OSError subclass, so this clause MUST come after the timeout
        # clause above, never merged with it -- a merged `except Exception`
        # (or an OSError-first ordering) would swallow the timeout branch.
        #
        # Split by step (#3707 round 3): before connect() returned, no owner was
        # reached -> `down`. After it, the owner WAS reached and the exchange
        # failed (EPIPE / ECONNRESET from sendall() or recv(), e.g. a dropped
        # request) -> `protocol`; `down` would be asserted about an owner that
        # answered the connect.
        return _unavailable(
            socket_path=sp,
            bound=timeout,
            detail=str(exc),
            request_id=request_id,
            reason="protocol" if connected else "down",
        )
    finally:
        sock.close()

    if not data:
        return _unavailable(
            socket_path=sp,
            bound=timeout,
            detail="owner closed the connection without responding",
            request_id=request_id,
            reason="protocol",
        )

    try:
        response = json.loads(data.decode("utf-8").strip())
    # ValueError, not just JSONDecodeError (its subclass): an integer over Python's
    # int-from-str digit limit raises a plain ValueError (#3717 round-2 review).
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        return _unavailable(
            socket_path=sp,
            bound=timeout,
            detail=f"owner sent an unparseable response: {exc}",
            request_id=request_id,
            reason="protocol",
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
        reason="protocol",
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
    """A WRITE that comes back `Unavailable` (reason `timeout` or `protocol`) is INDETERMINATE: the owner may have
    committed before the reply was lost, so it is never "failed". The one remedy is ONE retry with the
    SAME request_id (an owner-side dedupe store answers it with the stored outcome, `duplicate=True`, without a
    second bump); the id is echoed on every result type for exactly this. A `Rejected` is determinate only up to the COMMIT:
    before it nothing was committed; `readback_mismatch` (reason, `commit == "committed_unverified"`) is the one
    refusal that comes AFTER it (any failure while verifying a committed write lands there too), and a retry with
    the SAME request_id replays it without a second bump.

    Always ensures a request_id exists before constructing the wire
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


# ---------------------------------------------------------------------------
# Read-only connection shim (#3665 U6b stage 2b)
# ---------------------------------------------------------------------------
#
# ONE client-side shape for code written against `conn.execute(sql, params)
# .fetchone()/.fetchall()` (engram_core.generalization_chain,
# engram_idf.extract_keywords) to read through the owner. Every non-Ok result
# raises OwnerReadError; there is NO fallback to a direct open of knowledge.db
# and no database-library import here. The SQL is sent literally: the owner still
# matches each statement byte for byte against its registry.

_monotonic = time.monotonic  # module-level so tests can drive the budget clock


class OwnerReadError(Exception):
    """An owner read did not produce rows. `.kind` is one of
    unavailable / rejected / unusable_answer / budget_exhausted /
    client_failure; `.result` is the exact Unavailable/Rejected object (else
    None). str() is one short line and never carries SQL text or row data."""

    def __init__(self, kind, result=None):
        self.kind = kind
        self.result = result
        text = "owner read failed: " + kind
        if kind == "rejected" and isinstance(result, Rejected):
            text += " (" + " ".join(str(result.reason).split()) + ")"
        super().__init__(text)


def _cells_ok(row):
    if not isinstance(row, (list, tuple)):
        return False
    for cell in row:
        if cell is None:
            continue
        if isinstance(cell, bool) or not isinstance(cell, (int, float, str)):
            return False
    return True


class _OwnerCursor:
    """fetchone()/fetchall() over tuples, consuming. Deliberately minimal:
    no iteration, rowcount or description."""

    __slots__ = ("_rows", "_pos")

    def __init__(self, rows):
        self._rows = rows
        self._pos = 0

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        rest = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rest

    def __iter__(self):
        raise TypeError(
            "the owner read cursor does not support iteration; use fetchone()/fetchall()"
        )


def _idf_df_terms(sql, params):
    """-> the term list when (sql, params) is the IDF batch statement with exactly
    one str parameter that is a JSON list of str; else None (ordinary `read`
    route -- the owner then rules on it; this raises nothing of its own).
    `engram_idf` is imported lazily to keep the client import-light."""
    try:
        from engram_idf import IDF_BATCH_SQL
    except Exception:
        return None
    if sql != IDF_BATCH_SQL or len(params) != 1 or not isinstance(params[0], str):
        return None
    try:
        terms = json.loads(params[0])
    except ValueError:
        return None
    if isinstance(terms, list) and all(isinstance(t, str) for t in terms):
        return terms
    return None


# #3665: the owner's typed `df` op refuses an empty term, a term over
# DF_TERM_MAX_BYTES UTF-8 bytes and an empty list by name, while the IDF
# tokenizer (`[a-z]{2,}[a-z0-9_]*`) has no length bound. The shim therefore
# drops such terms before sending (they cannot score) and sends no request at
# all when nothing remains. Literal copies of engramd_owner.DF_TERM_MAX_BYTES /
# DF_TERMS_MAX (the client stays import-light); a cell pins the equality.
# Parity limit: a vocabulary term longer than 256 bytes would be scored on the
# direct path but not through the owner.
_DF_TERM_MAX_BYTES = 256
_DF_TERMS_MAX = 1000


class OwnerReadConnection:
    """Read-only connection shim over the owner's `read` method.

    `total_budget` is the whole-fire seconds for ALL reads together; each
    execute() uses min(per_call_timeout or total_budget, remaining). `.calls`
    counts owner round trips attempted (a budget refusal is not one)."""

    __slots__ = ("_total", "_per_call", "_socket_path", "_start", "_closed", "calls")

    def __init__(self, total_budget, per_call_timeout=None, *, socket_path=None):
        self._total = total_budget
        self._per_call = per_call_timeout
        self._socket_path = socket_path
        self._start = _monotonic()
        self._closed = False
        self.calls = 0

    def execute(self, sql, params=()):
        if self._closed:
            raise RuntimeError("closed")
        if not isinstance(sql, str):
            raise TypeError("sql must be a str")
        if not isinstance(params, (tuple, list)):
            raise TypeError("params must be a tuple or list")
        remaining = self._total - (_monotonic() - self._start)
        if remaining <= 0:
            raise OwnerReadError("budget_exhausted")
        method, payload = "read", {"sql": sql, "args": list(params)}
        df_terms = _idf_df_terms(sql, params)
        if df_terms is not None and len(df_terms) <= _DF_TERMS_MAX:
            # #3665: the IDF batch statement is not a registrable read template
            # (virtual table); the owner serves it as the typed `df` method.
            # More than the ceiling stays on the ordinary route (the owner rules).
            df_terms = [
                t for t in df_terms if t and len(t.encode("utf-8", "surrogatepass")) <= _DF_TERM_MAX_BYTES
            ]
            if not df_terms:
                return _OwnerCursor([])  # nothing can score: no round trip, no `.calls`
            method, payload = "df", {"terms": df_terms}
        timeout = min(self._per_call or self._total, remaining)
        self.calls += 1
        try:
            result = read(
                method,
                payload,
                timeout=timeout,
                socket_path=self._socket_path,
            )
        except (Exception, SystemExit) as exc:
            raise OwnerReadError("client_failure") from exc
        if isinstance(result, Ok):
            rows = result.result
            if not isinstance(rows, list) or not all(_cells_ok(r) for r in rows):
                raise OwnerReadError("unusable_answer")
            return _OwnerCursor([tuple(r) for r in rows])
        if isinstance(result, Unavailable):
            raise OwnerReadError("unavailable", result)
        if isinstance(result, Rejected):
            if method == "df" and result.reason == "vocab_table_absent":
                # The vocab table not existing is a fact about the graph, not an
                # owner failure: every term is absent from the vocabulary, so the
                # df answer is the empty result (the direct path's observable is
                # the same: no keywords). Every other rejection still raises.
                return _OwnerCursor([])
            raise OwnerReadError("rejected", result)
        raise OwnerReadError("unusable_answer")

    def generalization_chain(self, node_id, max_depth=4):
        """The generalization chain of `node_id` as ONE owner round trip (#3740 Cut A):
        sends the typed `chain` method, whose owner runs `engram_core.generalization_chain`
        itself, and returns that function's dict (JSON-decoded). The same budget, timeout and
        `.calls` accounting as `execute`: closed -> RuntimeError, budget exhausted ->
        OwnerReadError("budget_exhausted") (nothing sent, nothing counted), otherwise one
        counted round trip. A non-dict answer is OwnerReadError("unusable_answer");
        Unavailable / Rejected / a client crash map exactly as in `execute`. Deliberately
        thin: nothing is validated here (an over-length node id is sent and the owner refuses
        it by name), and `max_depth`'s default is a literal copy of the owner's
        CHAIN_MAX_DEPTH_CAP and of the function's own default (a cell pins both)."""
        if self._closed:
            raise RuntimeError("closed")
        remaining = self._total - (_monotonic() - self._start)
        if remaining <= 0:
            raise OwnerReadError("budget_exhausted")
        timeout = min(self._per_call or self._total, remaining)
        self.calls += 1
        try:
            result = read(
                "chain",
                {"node_id": node_id, "max_depth": max_depth},
                timeout=timeout,
                socket_path=self._socket_path,
            )
        except (Exception, SystemExit) as exc:
            raise OwnerReadError("client_failure") from exc
        if isinstance(result, Ok):
            if not isinstance(result.result, dict):
                raise OwnerReadError("unusable_answer")
            return result.result
        if isinstance(result, Unavailable):
            raise OwnerReadError("unavailable", result)
        if isinstance(result, Rejected):
            raise OwnerReadError("rejected", result)
        raise OwnerReadError("unusable_answer")

    def confidence_by_type(self, modes):
        """The by_type confidence distribution for each of `modes` as ONE owner round trip
        (#3741): sends the typed `confidence_by_type` method and returns the owner's dict
        `{"current_turn": int, "by_mode": {mode: {"by_type": ...}}}` (JSON-decoded). Mirrors
        `generalization_chain` exactly: closed -> RuntimeError, budget exhausted ->
        OwnerReadError("budget_exhausted") (nothing sent, nothing counted), otherwise one counted
        round trip; a non-dict answer is OwnerReadError("unusable_answer"); Unavailable /
        Rejected (`.result` attached) / a client crash map as in `execute`. Deliberately thin:
        nothing is validated here, the owner refuses a bad `modes` by name."""
        if self._closed:
            raise RuntimeError("closed")
        remaining = self._total - (_monotonic() - self._start)
        if remaining <= 0:
            raise OwnerReadError("budget_exhausted")
        timeout = min(self._per_call or self._total, remaining)
        self.calls += 1
        try:
            result = read(
                "confidence_by_type",
                {"modes": modes},
                timeout=timeout,
                socket_path=self._socket_path,
            )
        except (Exception, SystemExit) as exc:
            raise OwnerReadError("client_failure") from exc
        if isinstance(result, Ok):
            if not isinstance(result.result, dict):
                raise OwnerReadError("unusable_answer")
            return result.result
        if isinstance(result, Unavailable):
            raise OwnerReadError("unavailable", result)
        if isinstance(result, Rejected):
            raise OwnerReadError("rejected", result)
        raise OwnerReadError("unusable_answer")

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False
