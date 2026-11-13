#!/usr/bin/env python3
"""UserPromptSubmit hook: surface new forum threads and @mentions to the agent.

On every prompt, queries GET /api/threads?since=<surfaced-cursor> and injects
a brief notice into additionalContext if new threads have appeared. Advances
the surfaced cursor after firing.

Also queries GET /api/agent/<name>/inbox and filters to kind=at_mention items,
then injects a 🔔 mention line ABOVE the generic 📢 threads line when pending
@mentions exist. The inbox endpoint uses server-side per-thread read marks
(p.id > COALESCE(r.last_read_post_id, 0)), so reading a thread server-side
clears the mention automatically — no local read-cursor involved.

Also auto-publishes derived agent status to /api/agents/status on a throttled
schedule (Q1–Q4 from spec #1077 part 1): synchronous POST with timeout=
PUBLISH_TIMEOUT_SECONDS, swallow-all-errors, throttle on max(cadence_seconds,
60), always-publish-on-state-change, mid-sleep guard. The publish never
breaks the existing thread/mention surfacing.

Two-cursor state for threads (surfaced cursor is still hook-owned):
  - forum-surfaced-cursor.txt: advances on every hook fire. Drives "new since
    last prompt." After the hook fires, threads that were "new" appear in the
    surfaced window on the next prompt.
  - forum-read-cursor.txt: vestigial — no longer consumed by this hook for
    mention queries. The `cursor` subcommand stays for manual override/back-compat.

Cache: 30s file-based cache at ~/.engram/forum-hook-cache.json (under $ENGRAM_HOME) to avoid
hammering the server on rapid successive prompts. Cache stores both threads and
inbox mentions together. Stale-cache fallback on server-unreachable (never break
the hook chain if the forum is down).

Publish state: separate ~/.engram/forum-status-publish.json (under $ENGRAM_HOME) tracks last-published
status with its own throttle interval (never shares the 30s-TTL cache).

comms_surface gate (alpha#2019 slice A): config.json["comms_surface"] ("umwelt"
| "forum", resolved via _prompthooklib.get_comms_surface()) decides which lines
this hook is allowed to emit. Under "forum" (the default -- absent key), the
🔔/📢 lines above are unchanged. Under "umwelt", both are suppressed (the
umwelt plugin's own hooks already surface world state) EXCEPT a single
compact 📪 advisory line when pending ADDRESSED items (mentions) exist --
the anti-black-hole clause: a world-preferring seat must never silently miss
an addressed forum item pre-bridge (umwelt#599 slice 4 will bridge these).
An outage (or an unreadable inbox) is NOT silent under "umwelt" either: the
unknown line below is injected, so the advisory's absence is evidence of an
empty inbox only when no `⚠️ forum ...` line accompanies it. Broadcast-only
counts (new threads, no mention) are fully suppressed under "umwelt".

Unknown state (#3521 row 5; Kepler's ruling, 2026-10-06): "never block" means
this hook never blocks the prompt -- not that it may say "nothing new" when it
could not look. Each fetch is tri-state (ok / unknown / for the inbox also
`unsupported` = HTTP 404, an old server, healthy by design) and an unknown
state renders ONE bounded (<= 200 char) line carrying the reason CLASS only
(never a raw exception message; that goes to one sanitized stderr line):
  - forum unreachable, no cache  -> `⚠️ forum unreachable (<class>) ...`
  - forum unreachable, stale cache -> the stale output PLUS the same line,
    worded `showing cached counts`.
  - threads ok but inbox unreadable -> `⚠️ forum inbox unreadable (<class>) ...`;
    the cache stores `"mentions": null` (NOT `[]`) so the next prompt inside
    the TTL retries the inbox once (threads and cursor untouched).
Rate limit (deliberately unlike the baton hook, which prints its banner on
every prompt): this hook fires on every prompt for the whole length of an
outage, so each line (keys `forum-threads`, `forum-inbox`) is shown at most
once per 1800 s via the shared stamp (_hooklib.probe_notice_due /
mark_probe_notice); it reappears after the window while the condition lasts.

Silent no-op conditions:
  - forum.url not configured (agent_name alone does not count).
  - No new threads since last prompt and no pending @mentions (and no unknown
    state to report inside its rate-limit window).
  - Inbox endpoint absent (HTTP 404) -> known-empty, no line.
  - Status publish failure (forum-status-publish.json) -> silently swallowed;
    existing output unaffected.
  - comms_surface=umwelt with no pending addressed items -> fully silent
    (broadcast-only new-thread counts are not surfaced under umwelt).

Design doc: forum/spec.md
"""

import http.client
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)
# This hook's ONLY unconditional _hooklib import is emit_context (tests/test_forum_hook_unknown_state_3521.py
# pins that a _hooklib without the probe helpers still loads), so its home comes from the prompt-hook layer's
# resolver in _prompthooklib (#3863, parity-pinned), through its own guarded import. When that lib is
# missing, main() reports the failure and exits before any use of this value.
try:
    from _prompthooklib import engram_home_or_exit as _engram_home
    _HOME_LIB_OK = True
except Exception:
    _HOME_LIB_OK = False
ENGRAM_HOME = str(_engram_home()) if _HOME_LIB_OK else ""

SURFACED_CURSOR_PATH = os.path.join(ENGRAM_HOME, "forum-surfaced-cursor.txt")
# READ_CURSOR_PATH is no longer consumed by this hook for mention queries.
# Mention state is now derived from GET /api/agent/<name>/inbox, which uses
# server-side per-thread read marks — no local cursor needed.
# The `forum cursor` subcommand in tools/forum.py retains READ_CURSOR_PATH
# for manual override / back-compat only.
HOOK_CACHE_PATH = os.path.join(ENGRAM_HOME, "forum-hook-cache.json")

# Publish state file — separate from the 30s-TTL cache; its own throttle interval.
STATUS_PUBLISH_PATH = os.path.join(ENGRAM_HOME, "forum-status-publish.json")

CACHE_TTL_SECONDS = 30

_DEFAULT_FORUM_URL = "http://localhost:5002"

# hooks.json budgets this hook at 5s total. FETCH_TIMEOUT_SECONDS previously
# matched that budget exactly (5s), leaving zero margin for process startup +
# JSON parsing — on a genuinely unreachable forum the hook call itself would
# often exceed the outer budget and the harness would report a hook timeout
# instead of the intended graceful degrade-to-cache.
#
# Worst case per fire covered by THESE two constants is THREE calls, not two:
# _fetch_threads, then (only if threads succeeded) _fetch_inbox, then the
# unconditional publish POST. A prior version of this comment assumed at most
# one fetch plus the publish — true for a dead host (inbox is skipped when
# threads returns None) but false for a SLOW host: threads can succeed just
# under its own ceiling, then inbox times out at its own full ceiling, then
# publish still fires. FETCH_TIMEOUT_SECONDS covers both the threads and inbox
# call sites, so the invariant for THIS three-call class is
# 2*FETCH_TIMEOUT_SECONDS + PUBLISH_TIMEOUT_SECONDS must stay comfortably
# under 5s (colleague catch, PR #1642 review, 2026-08-03 — see
# test_worst_case_three_call_budget_under_hook_timeout for the regression
# guard on this exact sum).
#
# KNOWN GAP (gh#1906, not closed by this fix): the auto-publish block below
# also calls derive_own_status(), which unconditionally makes a FOURTH,
# uncached HTTP call (_fetch_held_batons, in _status_derive.py) on its own
# independent _FORUM_REQUEST_TIMEOUT_SECONDS=2.0 -- not derived from either
# constant here, and not covered by this module's 30s threads/mentions cache.
# The true worst case (threads + inbox + held-batons + publish) can still
# exceed 5s on a slow-but-alive host even with the constants below. Left
# open rather than shrinking these two constants further to compensate,
# since that constant is shared with tools/forum.py's unconstrained
# status-auto CLI path and squeezing these fetches much smaller trades away
# real margin for legitimately-slow (not dead) hosts. This module's own
# shared deadline below (HOOK_TOTAL_BUDGET_SECONDS) could subsume this gap
# if extended to that fourth call -- not done here, kept as a documented,
# disclosed follow-up rather than expanding this fix's diff into a second
# module (see gh#1906 for the candidate fixes).
FETCH_TIMEOUT_SECONDS = 1.5
PUBLISH_TIMEOUT_SECONDS = 1.0

# gh#3107: the worst-case arithmetic above (2*FETCH_TIMEOUT_SECONDS +
# PUBLISH_TIMEOUT_SECONDS < 5.0) rests on a premise that is FALSE for a
# server that trickles data: "a call made with timeout=T takes at most T".
# urllib/http.client re-arm the socket's read timeout on every SUCCESSFUL
# partial read, so a connection that is accepted and then dribbles bytes
# resets its own clock indefinitely -- measured: a call budgeted at 2.0s took
# 9.759s (4.84x) against a trickling peer, while the SAME timeout correctly
# bounded a silent (non-responding) peer at 2.014s. The exception in both
# cases is TimeoutError; only ELAPSED TIME distinguishes them, which is why a
# test against an unresponsive server alone (the "control" case) cannot catch
# this -- it passes on both a correct implementation and a broken one.
#
# The fix is a SHARED, wall-clock deadline for the whole hook fire, with
# EVERY socket read (not just the initial connect) capped at
# min(FETCH_TIMEOUT_SECONDS_or_PUBLISH_TIMEOUT_SECONDS, remaining-budget) --
# re-checked between reads. Capping at the REMAINING budget is the
# load-bearing element: re-checking the deadline between reads WITHOUT also
# shrinking each read's own cap toward it still overshoots by up to one
# read's full cap (measured: a between-ops-only check with a fixed 0.5s cap
# returned at 0.503s for the WRONG reason -- the fixed cap firing on a
# 0.5s-per-byte trickle, not the deadline; only a trickle FASTER than the cap
# -- 0.1s -- with the cap ALSO shrunk toward the deadline correctly aborted
# at the deadline itself, 3.003s against a 3.0s budget). Both layers are kept
# (belt-and-braces), but the cap-at-remaining is what actually enforces the
# bound; see _fetch_with_deadline's own docstring for the read loop.
#
# Scope note: this budget covers time AFTER a connection is established, not
# DNS resolution. _ipv4_only_connect's own docstring documents why this
# module pins IPv4 explicitly (an mDNS record advertising unreachable IPv6
# candidates ahead of the working address); DNS itself was separately
# measured at ~5ms on a reachable forum host, negligible against this
# budget, but that measurement says nothing about a resolver hanging while
# the host is asleep (a distinct, unmeasured case, cf. #3067's window) -- a
# hung resolve is not bounded by anything below and is named here rather
# than silently assumed away.
HOOK_TOTAL_BUDGET_SECONDS = 4.2  # matches the old arithmetic guard's own
                                 # ~0.8s-margin-under-5s ceiling: a host that
                                 # is merely slow (each call taking close to
                                 # its own FETCH/PUBLISH_TIMEOUT_SECONDS) is
                                 # unaffected: 2*1.5+1.0 = 4.0 <= 4.2. Only a
                                 # host that is anomalously slow beyond that
                                 # intended sum -- a trickle, not a wait -- is
                                 # newly bounded by this ceiling.
_READ_BLOCK_SIZE = 4096


def _remaining_budget(deadline: float) -> float:
    """Seconds left before `deadline` (a time.monotonic() value), floored at 0."""
    return max(0.0, deadline - time.monotonic())

# ---------------------------------------------------------------------------
# _status_derive import (spec §2, Q2)
#
# Resolve tools/ in BOTH topologies via the shared _hooklib helper (gh#1657,
# slice 3 of 3 -- this walk-parents logic was near-identical to the baton/
# inter-agent hooks' own copies, parameterized on a different marker file:
# _status_derive.py rather than forum_api.py, since this hook's own import
# needs a different tools/ module). _hooklib.py lives alongside this file in
# both topologies (source: hooks/claude/; deployed: hooks/), so no
# walk-parents is needed to find it -- but this repo's own test loaders
# (importlib.util.spec_from_file_location) don't auto-add a script's own
# directory to sys.path the way a real `python3 hooks/x.py` invocation does,
# so this hook adds its own directory explicitly before importing _hooklib.
# If the import fails for ANY reason, derive_own_status is set to None and the
# publish silently no-ops — the existing thread/mention surfacing is unaffected.
#
# gh#1680 slice 1: the config/agent-name/resolve-tools-dir prologue
# previously inlined here now lives in the shared _prompthooklib module
# (same directory, same sys.path bootstrap used below). _PROMPTHOOKLIB_OK
# gates main() -- if the shared module somehow fails to import, the hook
# reports the exception class (one stderr line, one injected context line;
# #3521) and exits 0 rather than crashing.
#
# engram-alpha#2863 WS2: this hook's own response-dict construction at the
# end of main() now routes through _assemble_response() below, which calls
# `_hooklib.emit_context()` (WS1, #2874) -- the shared bounded-injection
# renderer, imported unconditionally alongside it just below (same
# unconditional-import precedent as engram-surface-hook.py's own #2876
# migration: _hooklib.py / _harness.py are guaranteed sibling files in both
# build topologies, per the module docstring's #1539/#1558 note above, so
# this import doesn't need the defensive try/except _PROMPTHOOKLIB_OK gets).
#
# NAMING COLLISION worth flagging (mirrors Luria's #2876 note on
# check_principle_triggers_full): `_prompthooklib.py` already exports its
# OWN, differently-scoped `emit_context()` -- "print the additionalContext
# hookSpecificOutput envelope to stdout" for the baton/inter-agent hooks,
# with no budget/bounding logic at all (see that module's own docstring).
# This hook does NOT import that name (only the four _prompthooklib names
# below), so there is no runtime collision -- but two functions named
# `emit_context()` now exist in this directory with unrelated contracts;
# don't assume they're the same function when reading either hook.
# ---------------------------------------------------------------------------

_this_dir = str(Path(__file__).resolve().parent)
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)
from _hooklib import emit_context
from _harness import detect_harness, get_harness_budgets
# #3521 row 5: shared advisory-probe `unknown` rendering (sanitize + stderr
# line + the stamp-file rate limit). Imported separately from emit_context
# above so a failure here degrades to a plain, unlimited, bounded line --
# never to silence (see _unknown_line / _notice_due).
try:
    from _hooklib import (
        sanitize_probe_text, split_probe_reason, write_probe_unknown_stderr,
        probe_notice_due, mark_probe_notice,
    )
    _PROBE_LIB_OK = True
except Exception:
    _PROBE_LIB_OK = False
try:
    from _prompthooklib import (
        load_config_checked as _load_config_checked_impl,
        report_config_unknown as _report_config_unknown,
        get_agent_name as _get_agent_name_impl,
        get_comms_surface as _get_comms_surface_impl,
        bootstrap_tools_dir as _bootstrap_tools_dir,
        # #3628: every cursor / cache / publish-state read and temp write is
        # non-blocking and regular-file-only (the temp names are predictable:
        # <dest>.tmp). Reached through _prompthooklib so a missing _hooklib
        # helper degrades to OSError at the site, not to an import crash.
        hooklib_io as _hooklib_io,
        # #1459: every forum-sourced string is sanitised and framed as data.
        forum_field as _forum_field,
        forum_data_block as _forum_data_block,
    )
    _PROMPTHOOKLIB_OK = True
except Exception as _lib_exc:
    # #3521: remember the CLASS only (never the message); main() reports it.
    _PROMPTHOOKLIB_OK = False
    _PROMPTHOOKLIB_IMPORT_ERROR = type(_lib_exc).__name__
_tools_dir = _bootstrap_tools_dir("_status_derive.py") if _PROMPTHOOKLIB_OK else None
try:
    from _status_derive import derive_own_status, _read_loop_mode
except Exception:
    derive_own_status = None  # publish becomes a no-op; never break the hook
    _read_loop_mode = None    # type: ignore[assignment]
# #1459: the forum token has ONE source (tools/forum_api.load_forum_token), reached through
# the tools dir bootstrapped just above. Missing import -> None: requests go out unauthenticated
# and the forum's 401 surfaces through the hook's existing `http-401` unknown-state banner.
try:
    from forum_api import load_forum_token as _load_forum_token
except Exception:
    _load_forum_token = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Config helpers -- delegate to _prompthooklib (gh#1680 slice 1). Thin
# per-hook wrappers, not bare aliases: they pin ENGRAM_HOME (this hook's own
# module constant, captured once at import time above) as an explicit arg,
# preserving both (a) the exact zero-arg / single-arg call signatures every
# existing call site and test-monkeypatch already depends on, and (b) the
# "frozen at import time" ENGRAM_HOME timing the pre-extraction inline code
# had.
# ---------------------------------------------------------------------------

def _ipv4_only_connect(conn) -> None:
    """conn.connect(), forced to IPv4-only DNS resolution.

    The forum host's mDNS record advertises unreachable IPv6 candidates (a
    link-local address plus globally-scoped ones with no route on this LAN)
    ahead of the working IPv4 address in getaddrinfo's result order. stdlib
    socket.create_connection tries each candidate serially at the full
    per-attempt timeout before falling through to the next — enough,
    compounded across the dead entries, to blow this hook's outer
    UserPromptSubmit budget (observed: multi-second stalls vs. a 5s budget).
    curl doesn't hit this because it races families (Happy Eyeballs); stdlib
    has no such racing, so pin the family explicitly. See
    tools/forum_api.py's ForumClient._execute for the canonical writeup —
    duplicated here (not imported) per this hook's zero-import-back design.
    Applied here because _fetch_with_deadline drives
    http.client.HTTPConnection directly rather than urllib.
    """
    real_getaddrinfo = socket.getaddrinfo

    def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_only_getaddrinfo
    try:
        conn.connect()
    finally:
        socket.getaddrinfo = real_getaddrinfo


def _fetch_classified(
    url: str,
    deadline: float,
    per_op_cap: float,
    method: str = "GET",
    headers: Optional[dict] = None,
    body: Optional[bytes] = None,
) -> tuple:
    """The fetch core: returns `(body, None)` on a 2xx within budget, or
    `(None, "<class>: <message>")` on every failure (#3521 row 5). The class
    is one of `deadline` (shared budget spent), `timeout` (a socket timed
    out), `connect` (the connection could not be made), `http-<code>` (a
    non-2xx status) or `error` (anything else after connecting); the message
    is for the sanitized stderr line only and never reaches the injected line.

    `_fetch_with_deadline` below is the bytes-or-None view of this function
    that the publish POST and the pre-existing tests use unchanged.

    GET/POST `url`, bounded by a SHARED end-to-end deadline (gh#3107), not
    a flat per-call timeout. See HOOK_TOTAL_BUDGET_SECONDS's own comment
    above for the measured reason a flat timeout= does not bound a request
    against a server that keeps trickling data.

    Every socket READ (the initial connect included, via the timeout= passed
    to HTTPConnection's constructor) is capped at min(per_op_cap,
    remaining-budget), re-checked before each read -- so no single read, nor
    an unbounded SEQUENCE of individually-bounded reads, can outlive
    `deadline`. Capping at the remaining budget is the load-bearing element
    (see the module-level comment above HOOK_TOTAL_BUDGET_SECONDS); the
    explicit remaining<=0 check below is belt-and-braces on top of it.

    Drives http.client.HTTPConnection directly rather than urlopen: urlopen's
    response object exposes no public way to re-arm its socket's timeout
    between reads (only a private, three-levels-deep `.fp.raw._sock`), where
    HTTPConnection.sock is a first-class public attribute -- the same choice
    the fixture that validated this mechanism made, for the same reason.

    Returns the raw response body on a 2xx status within budget; None on any
    error, non-2xx status, or budget exhaustion mid-read -- matching the
    existing bare-except-return-None contract every caller here already
    relies on (a stalled/failed fetch is indistinguishable from any other
    failure to fall back to cache) -- except that the failure now carries its
    reason class for the callers that render an `unknown` state.
    """
    remaining = _remaining_budget(deadline)
    if remaining <= 0:
        return None, "deadline: shared budget already spent"
    # #1459: every forum request carries the bearer token when one is configured. A refused
    # token file (group/world-readable) is a failure class of its own, not a silent skip.
    # The header only ever goes to `url`'s own origin: this drives http.client directly, which
    # follows no redirect (a 3xx is the non-2xx failure below), so the same-origin redirect
    # policy of forum_api.forum_urlopen has nothing to guard here (pinned by
    # tests/test_forum_redirect_origin_1459.py).
    headers = dict(headers or {})
    if _load_forum_token is not None and "Authorization" not in headers:
        try:
            _tok = _load_forum_token()
        except Exception as _tok_exc:  # noqa: BLE001 - ForumTokenFileError names path + chmod 600
            return None, f"error: {_tok_exc}"
        if _tok:
            headers["Authorization"] = f"Bearer {_tok}"
    parsed = urllib.parse.urlsplit(url)
    conn_cls = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    conn = conn_cls(parsed.hostname, parsed.port, timeout=min(per_op_cap, remaining))
    stage = "connect"
    try:
        _ipv4_only_connect(conn)
        stage = "error"
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn.request(method, path, body=body, headers=headers or {})
        # gh#3143 (Mira's + Aleph's correction to this line's first draft):
        # capture the socket BEFORE getresponse(), not after. On a
        # `Connection: close` response, getresponse() sets conn.sock = None
        # -- but per CPython's own http.client source, this "effectively
        # passes the connection to the response": the response keeps
        # reading from the SAME socket object, it has not consumed the
        # body and there are real network reads still ahead of it. Re-arming
        # a captured reference to that same object keeps every read1()
        # bounded by min(per_op_cap, remaining) exactly as before; guarding
        # the re-arm on conn.sock (this line's first, wrong draft) instead
        # skips it entirely once conn.sock goes None, freezing every
        # subsequent read at whatever timeout the connection started with --
        # for a mid-body stall that is a full per_op_cap of unbounded
        # overshoot, the exact class of failure #3107 exists to prevent.
        sock = conn.sock
        resp = conn.getresponse()
        if resp.status < 200 or resp.status >= 300:
            return None, f"http-{resp.status}: HTTP {resp.status}"
        out = bytearray()
        while True:
            remaining = _remaining_budget(deadline)
            if remaining <= 0:
                return None, "deadline: shared budget spent mid-read"
            # The captured `sock` re-arms the timeout for as long as the
            # underlying fd is genuinely still open -- including mid-stall
            # on a `Connection: close` response, where the server hasn't
            # finished sending and the fd is very much alive despite
            # `conn.sock` already reading None (this is the property the
            # capture exists to preserve). But once the body has been FULLY
            # drained (the common case: a short response read to completion
            # in one prior read1() call), HTTPResponse's own EOF handling
            # closes the underlying fd for real on ITS next internal step --
            # and a `settimeout()` call against an already-closed fd raises
            # OSError ("Bad file descriptor"), not the AttributeError this
            # function used to guard against. Swallow it here rather than
            # via the function's own bare except below: at that point there
            # is nothing left to bound a timeout against, and resp.read1()
            # immediately below correctly signals EOF (b"") without one.
            try:
                sock.settimeout(min(per_op_cap, remaining))
            except OSError:
                pass
            # read1(), NOT read(): HTTPResponse.read(n) loops internally,
            # making as many underlying raw socket reads as needed to
            # accumulate n bytes (or EOF) BEFORE returning -- so the deadline
            # recheck above never runs between those internal reads, and a
            # server trickling slower than n bytes per per_op_cap window can
            # trap this call for its ENTIRE remaining length regardless of
            # settimeout() (measured: a 600-byte/0.05s-per-byte trickle ran
            # ~30s past a 2s budget under read(), because 4096 > 600 meant
            # ONE .read(4096) call never returned until EOF). read1(n) makes
            # AT MOST ONE underlying raw read per call, returning whatever
            # is currently available (possibly far fewer than n bytes) --
            # exactly the "one settimeout() = one op" pairing this loop
            # needs, and the reason the overshoot bound is one BLOCK's cap
            # (a read1() call can still return up to n bytes if that much is
            # already buffered) rather than one byte's, per #3107 review.
            chunk = resp.read1(_READ_BLOCK_SIZE)
            if not chunk:
                break
            out.extend(chunk)
        return bytes(out), None
    except Exception as exc:
        cls = "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else stage
        return None, f"{cls}: {type(exc).__name__}: {exc}"
    finally:
        conn.close()


def _fetch_with_deadline(
    url: str,
    deadline: float,
    per_op_cap: float,
    method: str = "GET",
    headers: Optional[dict] = None,
    body: Optional[bytes] = None,
) -> Optional[bytes]:
    """Bytes on a 2xx within budget, None on any failure -- the original
    contract (see _fetch_classified for the reason-carrying core)."""
    return _fetch_classified(url, deadline, per_op_cap, method, headers, body)[0]


# #3521 row 6: why the last _load_config() could not read config.json, or None
# when it could (or the file is simply absent). main() reads this right after
# _load_config() so an unreadable file is reported as UNKNOWN instead of being
# treated as "not configured". Kept as module state, not a changed return
# type, so _load_config() keeps its zero-arg dict contract for every existing
# caller and test monkeypatch.
_config_unknown_reason: Optional[str] = None


def _load_config() -> dict:
    """Load $ENGRAM_HOME/config.json. Returns {} on any failure (and records
    the reason in _config_unknown_reason when the file is present but unusable)."""
    global _config_unknown_reason
    cfg, _config_unknown_reason = _load_config_checked_impl(ENGRAM_HOME)
    return cfg or {}


def _get_agent_name(config: dict) -> str:
    """Resolve agent name from config. Returns '' if not set."""
    return _get_agent_name_impl(config, ENGRAM_HOME)


def _get_comms_surface(config: dict) -> str:
    """Resolve this agent's comms surface ('umwelt' or 'forum') from config.

    See _prompthooklib.get_comms_surface() for the full semantics
    (alpha#2019 slice A).
    """
    return _get_comms_surface_impl(config, ENGRAM_HOME)


def _get_forum_url(config: dict) -> str:
    """Resolve forum server URL from config or env. Returns default if absent."""
    forum_cfg = config.get("forum", {})
    if isinstance(forum_cfg, dict):
        url = forum_cfg.get("url", "").strip()
        if url:
            return url.rstrip("/")
    env_url = os.environ.get("FORUM_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")
    return _DEFAULT_FORUM_URL


def _is_forum_configured(config: dict) -> bool:
    """Return True if a forum is configured: ``forum.url`` is a non-empty string.

    Silent no-op gate: no ``forum.url`` -> skip the hook, so an install with no
    forum server never reports one as unreachable. ``agent_name`` alone is this
    agent's identity, not forum configuration, and does not pass the gate;
    neither does the ``FORUM_URL`` env var by itself. Total: any other shape
    (non-dict ``forum``, non-string or blank ``url``) is "not configured".
    """
    if not isinstance(config, dict):
        return False
    forum_cfg = config.get("forum")
    if not isinstance(forum_cfg, dict):
        return False
    url = forum_cfg.get("url")
    return isinstance(url, str) and bool(url.strip())


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------

def _read_cursor(path: str) -> Optional[datetime]:
    """Read a cursor file; return UTC datetime or None if absent/invalid."""
    try:
        raw = _hooklib_io().read_regular_text(path).strip()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _write_cursor(path: str, ts: datetime) -> None:
    """Write a UTC datetime to a cursor file atomically."""
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with _hooklib_io().open_regular_text_write(tmp) as f:
            f.write(ts_str + "\n")
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> Optional[dict]:
    """Load the hook cache. Returns None if absent, expired, or invalid."""
    try:
        raw = _hooklib_io().read_regular_text(HOOK_CACHE_PATH)
        data = json.loads(raw)
        # #3535: a cache that is not a JSON object, or whose `fetched_at` is not a
        # finite number (bool counts as not a number), is an INVALID cache -- the
        # same as an unparsable one -- never an AttributeError / TypeError that
        # exits the hook rc=1 with no forum signal. A non-finite stamp is invalid
        # too: NaN/Infinity would make `age > TTL` False and serve it as fresh.
        if not isinstance(data, dict):
            return None
        fetched_at = data.get("fetched_at", 0)
        if (isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float))
                or not math.isfinite(fetched_at)):
            return None
        age = time.time() - fetched_at
        if age > CACHE_TTL_SECONDS:
            return None
        return data
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _save_cache(threads: list, mentions: Optional[list],
                fetched_at: Optional[float] = None) -> None:
    """Save fetched threads and mentions to the hook cache.

    `mentions=None` is written as JSON `null`: the marker for "the inbox could
    not be read" (#3521 row 5). It is NOT the same as `[]` ("read, none
    waiting"); a reader must retry the inbox rather than serve it. A cache
    written by older code has the key absent or a list, and keeps its old
    meaning. `fetched_at` lets a mentions-only refresh keep the original
    threads fetch time, so it never extends the TTL.
    """
    dest = Path(HOOK_CACHE_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        payload = {
            "fetched_at": time.time() if fetched_at is None else fetched_at,
            "threads": threads,
            "mentions": mentions,
        }
        with _hooklib_io().open_regular_text_write(tmp) as f:
            f.write(json.dumps(payload))
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Publish state helpers (separate from the 30s cache)
# ---------------------------------------------------------------------------

def _load_publish_state() -> Optional[dict]:
    """Load the last-published status state. Returns None if absent/invalid."""
    try:
        return json.loads(_hooklib_io().read_regular_text(STATUS_PUBLISH_PATH))
    except (OSError, json.JSONDecodeError):
        return None


def _save_publish_state(state: str, activity: Optional[str],
                        queue: list, cadence: Optional[int],
                        published_at: float) -> None:
    """Save publish state atomically (same discipline as _save_cache)."""
    dest = Path(STATUS_PUBLISH_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        payload = {
            "state": state,
            "activity": activity,
            "queue": queue,
            "cadence": cadence,
            "published_at": published_at,
        }
        with _hooklib_io().open_regular_text_write(tmp) as f:
            f.write(json.dumps(payload))
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Publish decision helpers (pure — unit-testable without a live server)
# ---------------------------------------------------------------------------

def _should_publish(
    last: Optional[dict],
    current_tuple: tuple,
    now: float,
    throttle: float,
) -> bool:
    """Return True if a status publish should fire.

    Rules (spec Q3):
      (a) No last file → publish.
      (b) Status tuple changed since last publish → publish.
      (c) Unchanged AND within throttle window → skip.
      (d) Unchanged AND past throttle window → publish.

    Args:
        last:           The last-published state dict (None if no file).
        current_tuple:  (state, activity, queue_tuple, cadence) — queue must be
                        a hashable type (tuple) so equality works correctly.
        now:            Current epoch float.
        throttle:       Throttle interval in seconds.
    """
    if last is None:
        return True

    # Compare current tuple against last-published tuple.
    # queue is stored as a list in JSON; normalise both sides to tuple for equality.
    last_tuple = (
        last.get("state"),
        last.get("activity"),
        tuple(last.get("queue") or []),
        last.get("cadence"),
    )
    if current_tuple != last_tuple:
        return True

    # Unchanged — check throttle window.
    last_published_at = last.get("published_at", 0) or 0
    return (now - last_published_at) > throttle


def _apply_sleep_guard(
    state: str,
    activity: Optional[str],
    queue: list,
    cadence: Optional[int],
    last: Optional[dict],
    now: float,
) -> tuple:
    """Apply the Q4 mid-sleep guard.

    If there is no loop marker AND the last-published state was 'sleeping' AND
    we are still within the throttle window, keep ALL of the derived fields at
    their last-published 'sleeping' values (state, activity, queue, AND cadence)
    to suppress the false-wake publish. Common case self-corrects on the next
    genuine wake.

    Cadence must be restored alongside the other fields: derive_own_status with
    no loop marker returns cadence=None, but the last sleeping publish carried
    the sleep cadence (e.g. 270). If only state/activity/queue were restored,
    the cadence mismatch (None vs 270) would make _should_publish see a changed
    tuple and fire a spurious POST that DROPS expected_republish_seconds — the
    server would then fall back to its global window. (Reviewer catch, #1077 r2.)

    Returns (state, activity, queue, cadence) — possibly overridden.
    """
    if _read_loop_mode is None:
        return state, activity, queue, cadence

    loop = _read_loop_mode()
    if loop is not None:
        # Loop marker present — genuine wake. No guard needed.
        return state, activity, queue, cadence

    if last is None:
        return state, activity, queue, cadence

    if last.get("state") != "sleeping":
        return state, activity, queue, cadence

    last_published_at = last.get("published_at", 0) or 0
    last_cadence = last.get("cadence")
    throttle = max(last_cadence or 0, 60)
    if (now - last_published_at) <= throttle:
        # Within the window — keep ALL last-sleeping fields (incl. cadence).
        return "sleeping", last.get("activity"), last.get("queue") or [], last_cadence

    return state, activity, queue, cadence


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def _fetch_threads(
    forum_url: str, since: Optional[str], agent_name: str, deadline: float
) -> tuple:
    """GET /api/threads?since=<cursor>&agent=<name>.

    Returns `(threads, None)` on success (a list, possibly empty) and
    `(None, "<class>: <message>")` on any error -- the state is UNKNOWN, and
    the caller falls back to the cache AND says so (#3521 row 5).
    Timeout: min(FETCH_TIMEOUT_SECONDS, remaining budget under `deadline`) --
    see HOOK_TOTAL_BUDGET_SECONDS's module-header comment for the budget math
    (gh#3107).
    """
    params = {}
    if since:
        params["since"] = since
    if agent_name:
        params["agent"] = agent_name

    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{forum_url}/api/threads?{qs}"
    else:
        url = f"{forum_url}/api/threads"

    body, reason = _fetch_classified(
        url, deadline, FETCH_TIMEOUT_SECONDS, headers={"Accept": "application/json"}
    )
    if body is None:
        return None, reason
    try:
        data = json.loads(body.decode("utf-8"))
        threads = data.get("threads", [])
        if not isinstance(threads, list):
            raise TypeError("threads is not a list")
        return threads, None
    except Exception as exc:
        return None, f"unparsable: {type(exc).__name__}: {exc}"


def _fetch_inbox(forum_url: str, agent_name: str, deadline: float) -> tuple:
    """GET /api/agent/<name>/inbox — returns at_mention items only.

    The inbox endpoint uses server-side per-thread read marks so "reading the
    thread clears the mention" — no local cursor needed.

    Returns `(value, reason)` (#3521 row 5), three states:
      * `(items, None)`          -- read OK; items filtered to kind=at_mention
                                    (possibly empty).
      * `([], "unsupported")`    -- HTTP 404: an old server without the inbox
                                    endpoint. Known-empty by design: no line,
                                    cached as `[]`.
      * `(None, "<class>: <msg>")` -- UNKNOWN: anything else (never raises).
    Timeout: min(FETCH_TIMEOUT_SECONDS,
    remaining budget under `deadline`) — see HOOK_TOTAL_BUDGET_SECONDS's
    module-header comment (gh#3107). URL-quotes the agent name for safety.

    Response shape: {"inbox": [...], "unread_all": N}
    Item shape: {post_id, thread_id, thread_title, author, kind, created_at}
    """
    quoted_name = urllib.parse.quote(agent_name, safe="")
    url = f"{forum_url}/api/agent/{quoted_name}/inbox"

    body, reason = _fetch_classified(
        url, deadline, FETCH_TIMEOUT_SECONDS, headers={"Accept": "application/json"}
    )
    if body is None:
        if reason is not None and reason.startswith("http-404"):
            return [], "unsupported"
        return None, reason
    try:
        data = json.loads(body.decode("utf-8"))
        inbox = data.get("inbox", [])
        # Filter to at_mention only — reply_on_my_thread items are out of scope
        # for the banner (they are a different notification class).
        return [item for item in inbox if item.get("kind") == "at_mention"], None
    except Exception as exc:
        return None, f"unparsable: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

# engram-alpha#2863 WS2: mention_line becomes `head` -- the "must not miss"
# content routed through _assemble_response()/emit_context() below -- and
# thread titles are USER-CONTROLLED, unbounded content: the forum server's
# create-thread handler only .strip()s and rejects empty (src/forum/server.py,
# no max-length validation). Unlike engram-surface-hook.py's own head content
# (#2876), which is naturally short because it's built entirely from fixed
# system-health strings, this hook's head embeds real user-authored text --
# so WITHOUT a defensive per-title cap, a single pathologically long title
# could make `head` (mention_line) itself exceed the harness's head_budget
# BEFORE it ever reaches emit_context() (which only bounds the render IT
# produces, not the raw string a caller hands it). This is exactly the
# "never load content of unpredictable size" case (WS2 spec rule 2).
_MENTION_TITLE_MAX_CHARS = 150


def _format_mention_line(mentions: list) -> Optional[str]:
    """Format the 🔔 mention summary for injection into additionalContext.

    Mirrors the wording of forum status's _format_mention_line (tools/forum.py)
    but uses "mention"/"mentions" (not "post"/"posts") for noun agreement, caps
    inline display at 3, and appends the `forum read <id>` nudge.

    #1459: the first line is this hook's own count line; the thread ids, titles
    and authors (all forum-sourced) follow inside one framed data block, each
    through forum_field. A title is capped at _MENTION_TITLE_MAX_CHARS (150
    chars is a generous display width -- real titles observed in production top
    out far shorter, census: 608-char total mention line, 2026-09-12 -- and the
    worst case, 3 shown items each at the cap, stays comfortably under
    head_budget, 1,800).

    Returns None when the list is empty.
    """
    if not mentions:
        return None

    MAX_SHOWN = 3
    shown = mentions[:MAX_SHOWN]
    rest = len(mentions) - MAX_SHOWN

    entries = []
    for m in shown:
        tid = _forum_field(m.get("thread_id", "?"), 20)
        title = _forum_field(m.get("thread_title", "?"), _MENTION_TITLE_MAX_CHARS)
        author = _forum_field(m.get("author", "?"), 40)
        kind = m.get("kind", "")
        if kind == "at_mention":
            desc = f"@mention by {author}"
        else:
            desc = f"reply to your thread by {author}"
        entries.append(f'  - #{tid} "{title}" ({desc})')
    if rest > 0:
        entries.append(f"  +{rest} more")

    total = len(mentions)
    noun = "mention" if total == 1 else "mentions"
    count_line = (
        f"\U0001f514 {total} forum {noun} waiting on you"
        f" — `forum read <id>` to clear."
    )
    return "\n".join([count_line, *_forum_data_block(entries, "forum mentions")])


def _format_umwelt_advisory_line(mentions: list) -> Optional[str]:
    """Format the 📪 comms_surface=umwelt anti-black-hole advisory line.

    alpha#2019 slice A: under comms_surface=umwelt, the ordinary 🔔/📢
    hook-surface lines are suppressed (the umwelt plugin's own hooks
    already surface world state). But an ADDRESSED forum item (an
    at_mention -- a broadcast-only new-thread has no addressee) must never
    silently vanish pre-bridge (umwelt#599 slice 4), so this single
    compact line fires whenever pending addressed items exist. Returns
    None when there are none (broadcast-only counts are fully suppressed
    under umwelt -- no line at all in that case).
    """
    if not mentions:
        return None
    total = len(mentions)
    noun = "item" if total == 1 else "items"
    return (
        f"\U0001f4ea comms_surface=umwelt: {total} addressed forum {noun} exist; "
        f"the mention bridge is not yet live — `forum read <id>` to collect "
        f"(alpha#2019/umwelt#599 slice 4 will bridge these)."
    )


# ---------------------------------------------------------------------------
# Unknown-state rendering (#3521 row 5)
# ---------------------------------------------------------------------------

_HOOK_NAME = "engram-forum-prompt-hook"
_UNKNOWN_LINE_MAX = 200
_NOTICE_KEY_THREADS = "forum-threads"
_NOTICE_KEY_INBOX = "forum-inbox"


def _plain_clean(text, limit: int) -> str:
    """Fallback for sanitize_probe_text when _hooklib's helpers are missing:
    each control character -> one space, then cut. Never raises."""
    try:
        return "".join(
            " " if (ord(c) < 32 or 127 <= ord(c) <= 159) else c for c in str(text)
        )[:limit]
    except Exception:
        return "<unprintable>"[:limit]


def _reason_class(reason) -> str:
    """The reason CLASS only (`connect`, `http-500`, ...), sanitized and <= 40
    chars -- the raw message never reaches the injected line. Never raises."""
    try:
        if _PROBE_LIB_OK:
            cls, _msg = split_probe_reason(reason)
            return sanitize_probe_text(cls, 40).strip() or "unknown"
        return _plain_clean(str(reason).partition(":")[0].strip(), 40) or "unknown"
    except Exception:
        return "unknown"


def _unknown_line(probe: str, reason, cached: bool = False) -> str:
    """The ONE injected line for an unknown state, <= 200 chars. Never raises."""
    cls = _reason_class(reason)
    if probe == "inbox":
        line = f"⚠️ forum inbox unreadable ({cls}) — @mentions may be waiting"
    elif cached:
        line = (
            f"⚠️ forum unreachable ({cls}) — showing cached counts;"
            " new posts and @mentions may be missing"
        )
    else:
        line = (
            f"⚠️ forum unreachable ({cls}) — new posts and @mentions"
            " cannot be read; recheck when it is back"
        )
    return line[:_UNKNOWN_LINE_MAX]


def _unknown_notice(probe: str, key: str, reason, cached: bool = False) -> tuple:
    """Report an unknown state: ALWAYS one sanitized stderr line; returns
    `(line, key)` when the injected line is due (not shown within the last
    window), else `(None, None)`. If the shared helpers are unavailable the
    line is simply always due (a plain, bounded one-liner, never silence).
    Never raises."""
    try:
        if _PROBE_LIB_OK:
            write_probe_unknown_stderr(_HOOK_NAME, f"forum-{probe}", reason)
            due = probe_notice_due(ENGRAM_HOME, key)
        else:
            sys.stderr.write(
                f"{_HOOK_NAME}: forum-{probe} unknown: {_plain_clean(reason, 250)}"[:300] + "\n"
            )
            due = True
    except Exception:
        due = True
    if not due:
        return None, None
    try:
        return _unknown_line(probe, reason, cached), key
    except Exception:
        return "⚠️ forum state unknown", key


def _mark_notices(keys: list) -> None:
    """Start a new rate-limit window for each key. Call ONLY after the
    response carrying the lines was printed and flushed. Never raises."""
    if not _PROBE_LIB_OK:
        return
    for key in keys:
        try:
            mark_probe_notice(ENGRAM_HOME, key)
        except Exception:
            pass


def _cached_mentions(data: dict) -> Optional[list]:
    """The mentions out of a cache payload: a list, or None = UNKNOWN (the
    `"mentions": null` marker, or any non-list). A cache written by older
    code has the key ABSENT (-> []) or a list (-> as is): both keep their old
    meaning."""
    if "mentions" not in data:
        return []
    value = data["mentions"]
    if not isinstance(value, list):
        return None
    # #3535: an item that is not a JSON object (`[1, "a", null]`) would raise
    # AttributeError in the formatter. DROP such items; valid ones still render, and
    # a list with none left renders no mention line.
    return [m for m in value if isinstance(m, dict)]


def _cached_threads(data: dict) -> list:
    """The threads out of a cache payload: a list, else [] (#3535). The hook only
    counts them, so a non-list value (`"threads": 5` raised TypeError in len())
    is treated as no threads."""
    value = data.get("threads", [])
    return value if isinstance(value, list) else []


def _assemble_response(head_blocks: list, body_blocks: list) -> dict:
    """Build the UserPromptSubmit response dict from this prompt's fired
    head/body blocks, routed through the shared bounded renderer
    `_hooklib.emit_context()` (engram-alpha#2863 WS2).

    `head_blocks` / `body_blocks` are the already-rendered, non-empty block
    strings `main()` collected this prompt (see the "Build injection block"
    section below) -- this function does no filtering of its own beyond the
    "did anything fire at all" check. Split out from `main()` purely for
    testability, mirroring engram-surface-hook.py's own `_assemble_response()`
    (#2876) -- `main()`'s own behavior is otherwise unchanged.

    Census (this hook, unlike surface-hook's ~11 blocks): only 3 possible
    blocks total, across two branches main()'s own comms_surface gate keeps
    mutually exclusive within a single invocation:
      - comms_surface="forum" (default): mention_line (head) + threads_line
        (body) -- mention_line is the single actionable/addressed-item fact
        for this prompt (an @mention), threads_line the lower-priority
        broadcast-only new-threads count.
      - comms_surface="umwelt": the addressed-item advisory line (head) only
        -- conceptually the same "actionable fact" role as mention_line
        above, just this branch's own equivalent wording.
    So head is always at most one block; body is at most one block, and only
    ever fires in forum mode.

    Returns `{}` when nothing fired this prompt. In practice this call site
    never reaches that branch -- main()'s own pre-existing silent-no-op exits
    (three separate bare `sys.exit(0)` checks: forum-not-configured,
    advisory_line is None, threads-and-mention_line both absent) all run
    BEFORE this function is ever called, preserving the exact pre-migration
    behavior of emitting NO stdout at all (not even `{}`) on those paths --
    unlike engram-surface-hook.py, which always prints something. The `{}`
    branch here is defensive belt-and-suspenders only, matching the shared
    `_assemble_response()` contract established by #2876 for consistency;
    `emit_context()` is never called with two empty inputs either way.
    """
    head = "\n\n".join(head_blocks)
    body = list(body_blocks)
    if not head and not body:
        return {}

    harness = detect_harness()
    budgets = get_harness_budgets(harness)
    rendered = emit_context(
        head,
        body,
        budget=budgets["budget"],
        # DELIBERATE DEVIATION from engram-surface-hook.py's own #2876 choice
        # (target=budgets["budget"], the hard ceiling): here target is
        # budgets["head_budget"] instead. Reasoning: this hook's head
        # (mention_line, or its umwelt-mode equivalent) embeds real
        # user-authored thread titles -- genuinely unbounded content, unlike
        # surface-hook's head, which is built entirely from fixed
        # system-health strings and stays under head_budget only because
        # those three messages happen to be short today (documented there as
        # a canary, not a guarantee). Passing target=head_budget makes
        # emit_context()'s own Stage-1 line-boundary trim (see its docstring)
        # do the bounding for us, using the SAME tested mechanism the
        # tripwire hook's own local economy target relies on (_harness.py's
        # own docstring names this exact precedent: "its own soft target of
        # 1800 happens to equal claude-code's head_budget here"). This
        # hook's normal content (max delivered historically: 608 chars, per
        # the #2863 census) is nowhere near either ceiling, so no real
        # content is lost by choosing the smaller target -- it only
        # tightens the worst-case guarantee. (Also see forum_field()'s
        # own cap on individual titles -- belt-and-suspenders
        # with this target choice, not a substitute for it: together they
        # make the "head stays under head_budget" property a real
        # invariant here, not a canary.)
        target=budgets["head_budget"],
        # No single block in this hook is known to run large enough to need
        # the allow_single_overflow exception (unlike surface-hook's
        # principle_triggers) -- left at the default (False).
        allow_single_overflow=False,
        # No shared overflow-persistence helper exists yet in _hooklib.py
        # (see engram-surface-hook.py's own #2876 note) -- cut content is
        # dropped with a "M cut" count and no "-- see <path>" pointer.
        overflow_writer=None,
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": rendered,
        }
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _report_prompthooklib_import_failed() -> None:
    """#3521: a failed `_prompthooklib` import is VISIBLE, not a silent no-op.

    One class-only stderr line plus one class-only injected context line, on
    every run (deliberately no rate limit: a broken install is rare and
    severe). The lib -- and its `emit_context` -- is the thing that failed, so
    the envelope is built here with `json` alone. Each emission is separately
    guarded: this function never raises, and if a write fails the hook falls
    back to the silent exit 0 it had before.
    """
    try:
        cls = "".join(
            ch if ch.isprintable() else "?" for ch in _PROMPTHOOKLIB_IMPORT_ERROR
        )[:80]
        line = f"{_HOOK_NAME}: _prompthooklib import failed: {cls}"
    except Exception:
        return
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": line,
        }}))
        sys.stdout.flush()
    except Exception:
        pass


def main() -> None:
    # ── Prologue-lib gate (gh#1680 slice 1) ───────────────────────────────────
    # If _prompthooklib somehow failed to import, _load_config/_get_agent_name
    # are unavailable. Report the failure (class-only stderr line + injected
    # context line, #3521) and exit 0 without doing any other work. When run
    # as a script the exit is os._exit(0), not sys.exit(0): the reporter
    # swallows a failed write on a closed pipe, which leaves data buffered,
    # and the interpreter's shutdown flush would then fail and turn the exit
    # code into 120. When the module is loaded in-process (as the tests do),
    # it raises SystemExit(0) instead, because os._exit would end the host
    # process.
    if not _PROMPTHOOKLIB_OK:
        _report_prompthooklib_import_failed()
        try:
            sys.stderr.flush()
        except Exception:
            pass
        # Only a hook run as a script may skip shutdown: an in-process caller
        # (such as a test) must get a catchable SystemExit, never a killed process.
        if __name__ == "__main__":
            os._exit(0)
        sys.exit(0)

    # ── Config ────────────────────────────────────────────────────────────────
    config = _load_config()
    if _config_unknown_reason is not None:
        # #3521 row 6: a config.json that is there but unusable is UNKNOWN,
        # never "forum not configured". One stderr line + one rate-limited
        # injected line, then exit: nothing below can run without a config.
        _report_config_unknown(
            _HOOK_NAME, "config:forum-prompt-hook", ENGRAM_HOME,
            _config_unknown_reason,
            lambda text: print(json.dumps(_assemble_response([text], []))),
            "forum check skipped",
        )
        sys.exit(0)

    # ── Silent no-op gate ────────────────────────────────────────────────────
    # If forum isn't configured (no forum.url in config), skip: no output, no
    # file, no connection.
    if not _is_forum_configured(config):
        sys.exit(0)

    agent_name = _get_agent_name(config)
    forum_url = _get_forum_url(config)
    comms_surface = _get_comms_surface(config)

    # ── Read surfaced cursor ──────────────────────────────────────────────────
    surfaced_cursor = _read_cursor(SURFACED_CURSOR_PATH)
    since_str = surfaced_cursor.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if surfaced_cursor else None

    # Mention state no longer uses a local read cursor. The inbox endpoint
    # (GET /api/agent/<name>/inbox) uses server-side per-thread read marks, so
    # "reading a thread clears the mention" is handled by the server. No local
    # cursor read needed here.

    # ── Fetch threads + inbox mentions (with cache + stale-cache fallback) ────
    # gh#3107: ONE shared wall-clock deadline for every network call this hook
    # fire makes (threads, inbox, and the publish POST below) -- see
    # HOOK_TOTAL_BUDGET_SECONDS's module-header comment for why a flat
    # per-call timeout alone does not bound a trickling connection.
    _hook_deadline = time.monotonic() + HOOK_TOTAL_BUDGET_SECONDS
    threads: Optional[list] = None
    mentions: Optional[list] = None  # None = unknown (see _cached_mentions)
    threads_unknown = None   # reason string when the live threads fetch failed
    inbox_unknown = None     # reason string when the inbox could not be read
    cache_served = False     # True when a stale cache stood in for the forum

    cached = _load_cache()
    if cached is not None:
        # Cache is fresh — display it, but do NOT advance the surfaced cursor.
        # The live fetch that populated this cache already advanced the cursor
        # to its fetch time. Advancing again to `now` here would step the
        # cursor past posts made AFTER that fetch but before cache-expiry, so
        # the next live fetch's since=cursor would exclude them and they'd
        # never surface. (Twin of the unreachable-no-cache guard below.)
        threads = _cached_threads(cached)
        # Backward-compat: old cache files have no "mentions" key → [].
        # `"mentions": null` is the #3521 marker for "the inbox failed".
        mentions = _cached_mentions(cached)
        if mentions is None:
            if agent_name:
                # ONE live inbox retry, inside the shared deadline. Threads are
                # NOT refetched and the cursor is NOT touched: the cached
                # threads are still the answer for this window.
                mentions, retry_reason = _fetch_inbox(forum_url, agent_name, _hook_deadline)
                if mentions is not None:
                    # Write the resolved mentions back, keeping the ORIGINAL
                    # fetched_at so this never extends the TTL.
                    _save_cache(threads, mentions, fetched_at=cached.get("fetched_at"))
                else:
                    inbox_unknown = retry_reason
            else:
                mentions = []
    else:
        # Live fetch with since=current surfaced cursor.
        threads, threads_reason = _fetch_threads(forum_url, since_str, agent_name, _hook_deadline)
        if threads is not None:
            # Also fetch inbox mentions (advisory; failure → unknown, not a
            # blocker). Filtered to kind=at_mention inside _fetch_inbox.
            if agent_name:
                mentions, inbox_reason = _fetch_inbox(forum_url, agent_name, _hook_deadline)
                if mentions is None:
                    inbox_unknown = inbox_reason
            else:
                mentions = []
            # mentions=None is persisted as null (unknown), never as [].
            _save_cache(threads, mentions)
            # Only here did we query the server with the current cursor, so
            # only here is it safe to advance: everything up to `now` is now
            # covered by a real since=cursor query.
            now = datetime.now(timezone.utc)
            _write_cursor(SURFACED_CURSOR_PATH, now)
        else:
            # Server unreachable — stale-cache fallback (any age); do NOT
            # advance, so the next reachable query re-checks the same window.
            # The fallback is no longer silent: see unknown_blocks below.
            threads_unknown = threads_reason
            try:
                raw = _hooklib_io().read_regular_text(HOOK_CACHE_PATH)
                stale = json.loads(raw)
                threads = _cached_threads(stale)
                mentions = _cached_mentions(stale)
                cache_served = True
            except (OSError, ValueError, AttributeError):
                threads = None
                mentions = None

    # ── Build injection block ─────────────────────────────────────────────────
    # Mention line (higher priority) goes ABOVE the generic threads line.
    mention_line = _format_mention_line(mentions or [])

    # ── Auto-publish derived status (Q1–Q4, spec #1077 part 1) ───────────────
    # This block runs AFTER the fetch/format above but BEFORE the final output.
    # Its success/failure must NEVER gate the existing thread/mention output.
    try:
        if derive_own_status is not None and agent_name:
            _now = time.time()

            # Step 2: derive status from local signals.
            state, activity, queue, cadence = derive_own_status(agent_name)

            # Step 3: apply Q4 sleep guard (restores cadence too — see fn doc).
            last_pub = _load_publish_state()
            state, activity, queue, cadence = _apply_sleep_guard(
                state, activity, queue, cadence, last_pub, _now
            )

            # Step 4: compute throttle interval.
            throttle = max(cadence or 0, 60)

            # Step 5: decide whether to publish.
            current_tuple = (state, activity, tuple(queue), cadence)
            if _should_publish(last_pub, current_tuple, _now, throttle):
                # Step 6: POST to /api/agents/status with timeout=PUBLISH_TIMEOUT_SECONDS.
                # Only on success, write the publish state file.
                payload: dict = {
                    "agent": agent_name,
                    "state": state,
                    "activity": activity,
                    "queue": queue,
                }
                # cadence None → omit (server uses its global window);
                # 0 (on-call) or positive → include (mirror cmd_status_auto).
                if cadence is not None:
                    payload["expected_republish_seconds"] = cadence

                post_data = json.dumps(payload).encode("utf-8")
                # gh#3107: same shared _hook_deadline the fetches above used,
                # not a fresh full-size PUBLISH_TIMEOUT_SECONDS -- a slow
                # threads/inbox pair must correctly leave this call less
                # budget, not a flat one regardless of what already elapsed.
                publish_body = _fetch_with_deadline(
                    f"{forum_url}/api/agents/status",
                    _hook_deadline,
                    PUBLISH_TIMEOUT_SECONDS,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    body=post_data,
                )
                if publish_body is not None:
                    # POST succeeded — advance the throttle timestamp.
                    _save_publish_state(state, activity, queue, cadence, _now)
                # else: POST failed (or the shared budget was already spent)
                # — do NOT advance the timestamp so the next prompt retries.
    except Exception:
        # Whole publish block raised — swallow completely. The existing
        # thread/mention output is unaffected.
        pass

    # ── comms_surface gate (alpha#2019 slice A) ───────────────────────────────
    # Under "umwelt", the ordinary 🔔/📢 lines are suppressed (the umwelt
    # plugin's own hooks already surface world state) EXCEPT a single
    # compact advisory when pending ADDRESSED items (mentions) exist -- the
    # anti-black-hole clause: a world-preferring seat must never silently
    # miss an addressed forum item pre-bridge. Broadcast-only counts (new
    # threads, no mention) are fully suppressed under umwelt.
    #
    # engram-alpha#2863 WS2: head_blocks/body_blocks split, routed through
    # _assemble_response()/emit_context() below -- preserves the exact
    # existing branching for what fires this prompt (including the three
    # pre-existing bare `sys.exit(0)` silent-no-op exits below, untouched);
    # only WHERE the resulting string goes has changed. head_blocks carries
    # the single actionable/addressed-item fact for this prompt (an
    # @mention, or its umwelt-mode equivalent); body_blocks carries the
    # lower-priority broadcast-only new-threads count, forum mode only.
    #
    # #3521 row 5: an UNKNOWN state (forum unreachable / inbox unreadable) is
    # reported by one bounded line per window and lands in head_blocks AFTER
    # the actionable mention line; it counts as "something to report" for the
    # silent-exit checks below, in both modes.
    unknown_blocks: list = []
    notice_keys: list = []
    if threads_unknown is not None:
        line, key = _unknown_notice(
            "threads", _NOTICE_KEY_THREADS, threads_unknown, cached=cache_served)
        if line:
            unknown_blocks.append(line)
            notice_keys.append(key)
    if inbox_unknown is not None:
        line, key = _unknown_notice("inbox", _NOTICE_KEY_INBOX, inbox_unknown)
        if line:
            unknown_blocks.append(line)
            notice_keys.append(key)

    head_blocks: list = []
    body_blocks: list = []
    if comms_surface == "umwelt":
        advisory_line = _format_umwelt_advisory_line(mentions or [])
        if advisory_line is None and not unknown_blocks:
            sys.exit(0)
        head_blocks = ([advisory_line] if advisory_line else []) + unknown_blocks
    else:
        # ── Exit silently if nothing to report ──────────────────────────────
        if not threads and not mention_line and not unknown_blocks:
            sys.exit(0)

        # ── Count new threads since the surfaced cursor ─────────────────────
        new_count = len(threads) if threads else 0

        # 🔔 mention line first (if any) -- head: the actionable fact.
        if mention_line:
            head_blocks.append(mention_line)
        head_blocks.extend(unknown_blocks)

        # 📢 generic threads line (if any) -- body: broadcast-only, lower priority.
        if new_count > 0:
            post_word = "post" if new_count == 1 else "posts"
            body_blocks.append(
                f"\U0001f4e2 {new_count} new forum {post_word} since last read"
                f" — run `forum read <id>` or `forum status` to catch up."
            )

        if not head_blocks and not body_blocks:
            sys.exit(0)

    response = _assemble_response(head_blocks, body_blocks)
    if not response:
        # Defensive only -- see _assemble_response()'s own docstring on why
        # this call site never actually reaches its `{}` branch (the three
        # sys.exit(0) checks above already cover every "nothing to report"
        # case with the pre-migration silent-no-op behavior, no print at all).
        sys.exit(0)

    print(json.dumps(response))
    if notice_keys:
        # Stamp ONLY after the response was printed and flushed: a print that
        # failed must not start a rate-limit window for a line nobody saw.
        try:
            sys.stdout.flush()
            _mark_notices(notice_keys)
        except Exception:
            pass


if __name__ == "__main__":
    main()
