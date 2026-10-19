"""Integration tests for #2225 — neither forum monitor may move state past a
window it could not read, on an HTTP-200 response.

`curl --fail` only rejects HTTP >= 400, so a 200 carrying a maintenance page, a
truncated body or an empty string reaches the scripts' JSON helpers intact.
Both helpers swallowed the parse exception and emitted a well-formed "nothing
here" value, which no caller could tell apart from a genuinely quiet poll:

  - forum-updates-monitor.sh  as_of() -> 0   -> the `-ge` guard declined the
    cursor write on the POLL path, and the SEED path wrote a baseline of 0 —
    after which the next poll asks since=0 and emit() replays the entire
    dm+baton backlog, one printed line per update (emit() has no aggregation).
  - forum-mention-monitor.sh  ids()   -> no lines -> `$SEEN.now` empty and the
    unconditional `mv` overwrote `$SEEN` empty, so the next GOOD poll marked
    every historical mention new. Its SEED had the same shape.

The fix is an out-of-band signal — EXIT STATUS, not a sentinel value that could
collide with real data — which every caller treats exactly as it treats a
non-zero `curl --fail`: skip the cycle, preserve state.

THE TWO HELPERS ANSWER DIFFERENT QUESTIONS, and that asymmetry is the subtlest
thing these tests pin down:

  - `ids()` asks "did this body REPORT a mentions list?". `mentions` IS the
    watermark analogue: `{"mentions":[]}` is a reported zero and still
    rewrites `$SEEN`, while `{}`, an error envelope, a top-level array or a
    non-list `mentions` never reported at all and must preserve it. `{}` in
    particular must mean the same thing to both helpers — a changeset whose
    whole thesis is this boundary cannot read one body two opposite ways.
  - `as_of()` asks the strictly stronger "did I obtain a USABLE WATERMARK?".
    A clean `{}`, a null `as_of` and a JSON array all parse perfectly and still
    never say where the window ends. On the POLL path that distinction is
    harmless — the `-ge` monotonicity guard already declined those bodies — but
    the SEED has no guard to lean on, so `0` would simply become the baseline
    and the next poll would replay everything. The helper answers for the
    stricter caller.
    A watermark of 0 that the feed actually SERVED is still valid: a fresh
    forum honestly reports `as_of: 0`, and a truthiness test there would hang
    the seed forever on a healthy feed. Absence is rejected; zero is not.

Two halves, and the second is as load-bearing as the first:

  * a window we could not read must PRESERVE state (the bug), and
  * ordinary quiet operation must keep working (the regression guard) — the
    monitor keeps polling, an advanced cursor is never rewound, `$SEEN` is
    still rewritten on an honestly-empty mentions list. An over-broad fix that
    reads "no data" as "no reading" breaks normal operation, which is worse
    than the bug it fixes.

`as_of()` additionally rejects a BOOLEAN and a NEGATIVE watermark. Neither is
pedantry: `bool` is an `int` subclass in Python, so `"as_of": false` passes an
`is None` check and an `int()` call and arrives as 0 — the same seed flood
through a door that looks like a real number; and a negative `since` "simply
admits everything <= as_of" per src/forum/coordination/updates.py.

Everything here drives the REAL scripts as subprocesses against a mock HTTP
server that serves RAW bytes (the coalesce test's mock json.dumps()es its
payload and so cannot express an unparseable body at all).

REPRODUCING THE PRE-FIX FAILURES — the method matters, state it with the
number. These tests need `$FORUM_UPDATES_POLL_SECONDS`, which does not exist
before this change, so running them against a literal clean `dev` times out
six of them on the 30s production cadence and reports a count that is real but
unattributable. To see only genuine defect failures, back-port the two
`POLL_SECONDS` lines in forum-updates-monitor.sh (the seed-retry sleep and the
poll-loop sleep) onto the old tree and nothing else.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_UPDATES_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-updates-monitor.sh"
_MENTION_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-mention-monitor.sh"

# #3613: the summary ends with the EMPTY ENGRAM marker (no span).
_FLUSH_RE = re.compile(r"^\U0001f4e3 (\d+) new @-mentions? across .* \u27e6ENGRAM\u27e7\u27e6/ENGRAM\u27e7$")

# Bodies that pass `curl --fail` with a 200 and do NOT parse as JSON.
_UNPARSEABLE = [
    pytest.param("<html>maintenance</html>", id="html-maintenance-page"),
    pytest.param("", id="empty-body"),
    pytest.param('{"updates":[{"seq":1,', id="truncated-json"),
]

_POLL = "0.2"  # seconds; overrides the 30s production cadence in both scripts


# ---------------------------------------------------------------------------
# mock server: serves RAW bytes so a test can express a non-JSON 200
# ---------------------------------------------------------------------------

class _MockRawServer:
    """`payload_fn(request_count, path) -> (status, body_str)`, called once per
    GET with `request_count` starting at 1 (each monitor's seed poll is #1)."""

    def __init__(self, payload_fn):
        self._payload_fn = payload_fn
        self._count = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence default request logging
                pass

            def do_GET(self):
                with outer._lock:
                    outer._count += 1
                    n = outer._count
                status, body = outer._payload_fn(n, self.path)
                payload = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._count

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def serve():
    """Start a mock raw server for the duration of one test."""
    servers = []

    def _start(payload_fn):
        s = _MockRawServer(payload_fn)
        s.start()
        servers.append(s)
        return s

    yield _start
    for s in servers:
        s.stop()


# ---------------------------------------------------------------------------
# running the real scripts
# ---------------------------------------------------------------------------

class _MonitorProc:
    """A running monitor subprocess whose stdout AND stderr are drained on
    threads, so a test can interleave assertions with the script's own poll
    cycles. stderr matters here: "the loop survived this body" is half of what
    the quiet-operation guards assert, and a python traceback from a helper
    would otherwise be invisible."""

    def __init__(self, argv, env):
        self.lines: list[str] = []
        self.errs: list[str] = []
        self._proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        self._readers = [
            threading.Thread(target=self._drain, args=(self._proc.stdout, self.lines),
                             daemon=True),
            threading.Thread(target=self._drain, args=(self._proc.stderr, self.errs),
                             daemon=True),
        ]
        for t in self._readers:
            t.start()

    @staticmethod
    def _drain(stream, sink):
        assert stream is not None
        for line in stream:
            sink.append(line.rstrip("\n"))

    def stop(self):
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        for t in self._readers:
            t.join(timeout=2)
        return self.lines


def _make_home(tmp_path: Path, port: int) -> Path:
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    config = {"agent_name": "testagent", "forum": {"url": f"http://127.0.0.1:{port}"}}
    (home / ".engram" / "config.json").write_text(json.dumps(config))
    return home


@pytest.fixture
def run_updates(tmp_path):
    """Arm the real forum-updates-monitor.sh; returns (proc, cursor_file)."""
    procs = []

    def _run(port: int, poll_seconds: str = _POLL):
        home = _make_home(tmp_path, port)
        cursor = tmp_path / "cursor"
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["ENGRAM_HOME"] = str(home / ".engram")
        env["FORUM_UPDATES_NO_REAP"] = "1"
        env["FORUM_UPDATES_CURSOR_FILE"] = str(cursor)
        env["FORUM_UPDATES_POLL_SECONDS"] = poll_seconds
        proc = _MonitorProc(["bash", str(_UPDATES_SCRIPT)], env)
        procs.append(proc)
        return proc, cursor

    yield _run
    for p in procs:
        p.stop()


@pytest.fixture
def run_mention(tmp_path):
    """Arm the real forum-mention-monitor.sh; returns (proc, seen_file)."""
    procs = []

    def _run(port: int, poll_seconds: str = _POLL, coalesce_seconds: str = "1"):
        home = _make_home(tmp_path, port)
        seen = tmp_path / "seen"
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["ENGRAM_HOME"] = str(home / ".engram")
        env["FORUM_MENTION_NO_REAP"] = "1"
        env["FORUM_MENTION_SEEN_FILE"] = str(seen)
        env["FORUM_MENTION_POLL_SECONDS"] = poll_seconds
        env["FORUM_MENTION_COALESCE_SECONDS"] = coalesce_seconds
        proc = _MonitorProc(["bash", str(_MENTION_SCRIPT)], env)
        procs.append(proc)
        return proc, seen

    yield _run
    for p in procs:
        p.stop()


def _wait_until(pred, timeout: float = 5.0) -> bool:
    """Poll `pred` until true or the deadline passes.

    Everything these tests read is written by a live shell subprocess with
    plain `>` redirects, so a bare exists()-check races the truncate-then-write
    and can observe an empty or half-written file. Waiting on the PREDICATE
    (not on the file's existence) is what makes these assertions stable.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


def _wait_for_content(path: Path, expected: str, timeout: float = 5.0) -> bool:
    return _wait_until(lambda: path.is_file() and path.read_text().strip() == expected,
                       timeout)


def _no_stale_scratch(seen: Path, timeout: float = 3.0) -> bool:
    """True if `$SEEN.now` is observed ABSENT at some point within `timeout`.

    Both the seed retry and a skipped poll cycle create the scratch file with
    a `>` redirect BEFORE the helper inside the pipeline can fail, so it
    exists transiently on every single retry — an instantaneous exists()
    check races that window and fails at random. What the scripts actually
    promise is that no scratch file is LEFT BEHIND for a later cycle to pick
    up, i.e. that it does not persist. A genuinely stale file is never
    absent, so this still catches the failure it is here to catch.
    """
    return _wait_until(lambda: not Path(str(seen) + ".now").exists(), timeout)


def _peek(path: Path) -> str:
    """Best-effort current contents, for assertion messages."""
    try:
        return repr(path.read_text())
    except OSError:
        return "<absent>"


def _wait_for_requests(server: _MockRawServer, n: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.request_count >= n:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"mock server saw only {server.request_count} requests, expected >= {n}"
    )


def _mentions(entries) -> str:
    """entries: iterable of (post_id, thread_id, thread_title)."""
    return json.dumps(
        {
            "mentions": [
                {"post_id": pid, "thread_id": tid, "thread_title": title}
                for pid, tid, title in entries
            ]
        }
    )


def _since_of(path: str) -> int:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    return int((qs.get("since") or ["0"])[0])


# ===========================================================================
# forum-updates-monitor.sh
# ===========================================================================

# --- regression guard: ordinary quiet operation must not break --------------

@pytest.mark.parametrize(
    "body",
    [
        pytest.param('{"updates":[],"as_of":7095}', id="int-as_of"),
        pytest.param('{"updates":[],"as_of":"7095"}', id="stringified-as_of"),
    ],
)
def test_updates_empty_updates_list_with_a_watermark_still_seeds(
    serve, run_updates, body
):
    """An empty `updates` list is a QUIET poll, not a failed one. What makes it
    usable is the WATERMARK, which is present here — so the cursor must be
    seeded at it, whether it arrives as an int or a string."""
    server = serve(lambda n, path: (200, body))
    _proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "7095"), (
        f"cursor was never seeded at the served watermark from {body!r}; "
        f"cursor is {_peek(cursor)}"
    )


def test_updates_seed_accepts_a_watermark_of_zero_on_a_fresh_feed(serve, run_updates):
    """0 IS A VALID WATERMARK, and this test exists to stop anyone "fixing"
    the no-watermark rejection with a truthiness / `-gt 0` test.

    A brand-new forum with nothing in the feed genuinely serves `as_of: 0`. A
    guard that rejected it would spin the seed forever on a perfectly healthy
    feed — a hang, not a loud failure. So: seed at 0, then advance normally
    once the feed actually has something in it."""
    update = json.dumps(
        {"as_of": 5, "updates": [{"seq": 1, "kind": "dm", "sender": "a", "body": "hi"}]}
    )

    def payload_fn(n: int, path: str):
        if n <= 5:
            return 200, '{"updates":[],"as_of":0}'   # fresh feed, honestly empty
        return 200, (update if _since_of(path) < 5 else '{"updates":[],"as_of":5}')

    server = serve(payload_fn)
    proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "0"), (
        "a served as_of of 0 is a real baseline and must seed the cursor; the "
        f"seed rejected it (cursor is {_peek(cursor)}) — a truthiness guard "
        "would hang here forever on a healthy empty feed"
    )
    assert _wait_for_content(cursor, "5", timeout=8.0), (
        f"cursor never advanced off the zero baseline; cursor is {_peek(cursor)}"
    )
    assert any("new DM from a" in l for l in proc.lines), (
        f"the genuinely-new update was never emitted: {proc.lines!r}"
    )


# Bodies that PARSE cleanly but hand back no usable watermark. Distinct from
# _UNPARSEABLE: nothing is wrong with the JSON, it just never says where the
# window ends. The poll path can shrug at these (the -ge guard already
# declined them); the seed path cannot.
_NO_WATERMARK = [
    pytest.param("{}", id="empty-object"),
    pytest.param('{"updates":[]}', id="no-as_of-key"),
    pytest.param('{"updates":[],"as_of":null}', id="null-as_of"),
    pytest.param('{"as_of":"abc"}', id="non-coercible-as_of"),
    pytest.param("[1,2,3]", id="json-array"),
    # bool is an int SUBCLASS in Python, so `false` survives both an `is None`
    # check and an int() call and lands as 0 — the site-3 flood through a door
    # that looks like a valid watermark. `true` lands as 1, which is worse in
    # a quieter way: a plausible-looking cursor that replays nearly everything.
    pytest.param('{"as_of":false}', id="false-bool"),
    pytest.param('{"as_of":true}', id="true-bool"),
    # A negative `since` "simply admits everything <= as_of" per
    # src/forum/coordination/updates.py — a full replay wearing a real number.
    pytest.param('{"as_of":-5}', id="negative"),
    # Above bash's comparable range. The failure is the quietest one in this
    # file: the seed has no numeric test so it writes the value; the poll's
    # `[ <big> -ge $_cur ]` exits 2 and that is swallowed by the existing
    # `2>/dev/null`; and every later REAL watermark then fails the same
    # comparison against the seeded value. Permanently deaf, no error anywhere.
    pytest.param('{"as_of":1e29}', id="overflow-1e29"),
    pytest.param('{"as_of":9223372036854775808}', id="overflow-2pow63"),
]


@pytest.mark.parametrize("body", _NO_WATERMARK)
def test_updates_poll_survives_bodies_with_no_usable_watermark(
    serve, run_updates, body
):
    """Ordinary quiet operation, asserted on the OBSERVABLE rather than on the
    helper's exit code: a body with no watermark must not crash the poll cycle,
    must not rewind an already-advanced cursor, and must not stop the monitor
    polling.

    This is the guard the over-broad fix breaks. It deliberately does not care
    whether as_of() reports success or failure for these bodies — only that the
    monitor keeps running and the cursor keeps its ground."""

    def payload_fn(n: int, path: str):
        if n == 1:
            return 200, '{"updates":[],"as_of":7095}'  # seed at a real watermark
        return 200, body

    server = serve(payload_fn)
    proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "7095"), (
        f"seed never baselined; cursor is {_peek(cursor)}"
    )
    seen = server.request_count
    _wait_for_requests(server, seen + 4)   # the loop is still polling
    time.sleep(0.3)

    assert cursor.read_text().strip() == "7095", (
        f"{body!r} rewound the cursor to {_peek(cursor)} — an advanced cursor "
        "must never move backwards"
    )
    if body.lstrip().startswith("{"):
        # OUT OF SCOPE, and pre-existing on dev: emit() guards only json.load,
        # then calls d.get() unguarded — so a body that parses to a NON-dict
        # (the json-array case below) makes emit() print an AttributeError
        # traceback to stderr on every cycle. The loop survives it (emit's
        # status is never checked and the cursor assertions above still hold),
        # so it is noise rather than a state defect, and it is not #2225's to
        # fix. Asserting a clean stderr only for object-shaped bodies keeps
        # this test honest about what it covers instead of quietly passing.
        assert not any("Traceback" in e for e in proc.errs), (
            f"the poll cycle crashed on {body!r}: {proc.errs!r}"
        )


# --- the bug: no usable watermark must never become the SEED baseline -------

@pytest.mark.parametrize("body", _NO_WATERMARK)
def test_updates_seed_refuses_to_baseline_without_a_watermark(
    serve, run_updates, body
):
    """Site 3 again, by the other door — and the case the original spec missed.

    These bodies parse, so a parsed-vs-unparseable predicate lets them through;
    as_of() then reports 0 and the SEED takes it as the baseline. Unlike the
    poll path there is no monotonicity guard to decline it, so the next poll
    asks since=0 and emit() walks the whole backlog. Never having been told
    where the window ends is not the same as being told it ends at 0."""
    server = serve(lambda n, path: (200, body))
    _proc, cursor = run_updates(server.port)

    _wait_for_requests(server, 3)  # the seed has retried at least twice
    assert not cursor.exists(), (
        f"seed baselined at {_peek(cursor)} from {body!r}, which never served a "
        "watermark. Asserting the cursor was never written AT ALL — not merely "
        "that it is not 0 — is what catches `as_of: -5` (a negative since "
        "admits everything) and `as_of: true` (which lands as 1); both are "
        "replays wearing a number that would pass a != 0 test."
    )


# --- narrowing "did arming replay the backlog?" (#3067) ---------------------
# The two end-to-end seed tests below used to assert `proc.lines == []`, using
# "no output at all" as a proxy for "no replay". That stopped being equivalent
# when the seed gained an arm-time announce (#3067): these tests deliberately
# serve bodies the seed MUST refuse, so the seed reports that it cannot seed
# yet — correct behaviour on this path, and not a replay.
#
# The replacement is a POSITIVE predicate rather than a filter that subtracts
# the announce: it looks for the backlogged updates themselves. Every entry
# these tests serve is kind "dm" with a distinct sender, and emit() prints the
# sender on each DM line ("new DM from agentN: msg N"), so a seed that
# baselined at 0 and replayed the (0, 7095] window shows up here as one
# matching line per backlogged update — five, not zero. The regression these
# tests exist to catch therefore still reddens them.
#
# Derived from ONE source rather than kept in sync by hand with the backlog
# built inline in the tests below. #3111's review measured that a desync does
# not merely weaken this filter, it VACATES it: with the senders left at
# agent1..agent5 while a test's backlog moved to a disjoint range, the #743
# clobber-replay regression came back GREEN (`1 passed`) — a vacuous filter
# matches nothing, and "nothing matched" is exactly what the assertion wants
# to see. The near-miss is the part worth recording: a `range(10, 15)` desync
# still reddened, but only because "agent1" is a SUBSTRING of "agent10", so
# the test was protected by an accident of substring matching rather than by
# construction. Both payloads below are now built from _BACKLOG_UPDATES, so
# the range cannot drift away from the senders the filter looks for.
_BACKLOG_SEQS = tuple(range(1, 6))
_BACKLOG_UPDATES = [
    {"seq": i, "kind": "dm", "sender": f"agent{i}", "body": f"msg {i}"}
    for i in _BACKLOG_SEQS
]
_BACKLOG_SENDERS = tuple(u["sender"] for u in _BACKLOG_UPDATES)


def _replay_lines(lines: list[str]) -> list[str]:
    """Lines reporting one of the backlogged updates as newly delivered."""
    return [l for l in lines if any(s in l for s in _BACKLOG_SENDERS)]


def test_updates_seed_retries_past_a_watermarkless_body_and_does_not_replay(
    serve, run_updates
):
    """The same thing end to end, asserted on the observable harm: requests
    #1-2 parse but carry no watermark; from #3 the feed answers honestly and a
    since=0 request gets the full backlog exactly as the real feed would.
    Arming must replay none of it."""
    backlog = json.dumps({"as_of": 7095, "updates": _BACKLOG_UPDATES})
    quiet = '{"updates":[],"as_of":7095}'

    def payload_fn(n: int, path: str):
        if n <= 2:
            return 200, '{"updates":[]}'   # parses; no watermark
        return 200, (backlog if _since_of(path) < 7095 else quiet)

    server = serve(payload_fn)
    proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "7095"), (
        f"seed did not baseline at the live watermark; cursor is {_peek(cursor)}"
    )
    _wait_for_requests(server, 6)
    time.sleep(0.4)

    # Narrowed from `proc.lines == []` — see _replay_lines above.
    assert _replay_lines(proc.lines) == [], (
        "arming replayed the backlog — the seed baselined at 0 from a body "
        f"that never served a watermark: {proc.lines!r}"
    )


# --- the bug: failed-to-parse must preserve state --------------------------

@pytest.mark.parametrize("bad_body", _UNPARSEABLE)
def test_updates_seed_refuses_to_baseline_on_malformed_200(
    serve, run_updates, bad_body
):
    """Site 3 — the SEED path, and the most severe of the three.

    A malformed 200 passes `curl --fail`, so pre-fix the seed baselined the
    cursor at 0. Nothing about that is quiet: the very next poll asks since=0
    and emit() prints one line per backlogged update. The seed must keep
    retrying instead of arming with a replay-everything baseline."""
    server = serve(lambda n, path: (200, bad_body))
    _proc, cursor = run_updates(server.port)

    _wait_for_requests(server, 3)  # the seed has retried at least twice
    assert not cursor.exists(), (
        f"seed baselined at {_peek(cursor)} from a body it could not parse "
        f"({bad_body!r}) — the next poll would replay the whole backlog"
    )


def test_updates_seed_retries_past_malformed_200_and_does_not_replay_backlog(
    serve, run_updates
):
    """Site 3, end to end: the observable harm is the replay, so assert on it.

    Requests #1-2 are malformed 200s; from #3 the server answers honestly, and
    a `since=0` request gets the full backlog exactly as the real feed would.
    Pre-fix the seed took the malformed #1, wrote 0, and the next poll's
    since=0 dumped all five updates. Post-fix the seed retries, baselines at
    the live watermark, and nothing is replayed."""
    backlog = json.dumps({"as_of": 7095, "updates": _BACKLOG_UPDATES})
    quiet = '{"updates":[],"as_of":7095}'

    def payload_fn(n: int, path: str):
        if n <= 2:
            return 200, "<html>maintenance</html>"
        # Honest cursor feed: (since, as_of] — since=0 replays everything.
        return 200, (backlog if _since_of(path) < 7095 else quiet)

    server = serve(payload_fn)
    proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "7095"), (
        f"seed did not baseline at the live watermark; cursor is {_peek(cursor)}"
    )
    _wait_for_requests(server, 6)
    time.sleep(0.4)

    assert cursor.read_text().strip() == "7095"
    # Narrowed from `proc.lines == []` — see _replay_lines above.
    assert _replay_lines(proc.lines) == [], (
        "arming replayed the backlog — the seed baselined at 0 from a "
        f"malformed 200: {proc.lines!r}"
    )


@pytest.mark.parametrize("bad_body", _UNPARSEABLE)
def test_updates_poll_leaves_cursor_untouched_on_malformed_200(
    serve, run_updates, bad_body
):
    """Site 1 — the POLL path. With the cursor at 0, pre-fix as_of() returned 0
    and `0 -ge 0` let the write through, so an unreadable window still rewrote
    the cursor. Content and mtime must both be untouched: the cursor write is
    the record of a window we actually read."""

    def payload_fn(n: int, path: str):
        if n == 1:
            return 200, '{"updates":[],"as_of":0}'  # parses; seeds cursor at 0
        return 200, bad_body

    server = serve(payload_fn)
    _proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "0"), (
        f"seed never baselined the cursor at 0; cursor is {_peek(cursor)}"
    )
    _wait_for_requests(server, 2)
    time.sleep(0.3)
    before_bytes = cursor.read_bytes()
    before_mtime = cursor.stat().st_mtime_ns

    _wait_for_requests(server, server.request_count + 3)
    time.sleep(0.3)

    assert cursor.read_bytes() == before_bytes
    assert cursor.stat().st_mtime_ns == before_mtime, (
        f"cursor was rewritten from a body that did not parse ({bad_body!r})"
    )


# ===========================================================================
# forum-mention-monitor.sh
# ===========================================================================

# --- regression guard: parsed-and-empty still rewrites $SEEN ---------------

@pytest.mark.parametrize(
    "empty_body",
    [
        pytest.param('{"mentions":[]}', id="empty-mentions-list"),
    ],
)
def test_mention_reported_empty_still_rewrites_seen(serve, run_mention, empty_body):
    """`{"mentions":[]}` is the ONE body that must still rewrite `$SEEN`: the
    forum parsed our request and REPORTED an answer, and the answer is none.
    `$SEEN` is a snapshot of the current mentions, so a quiet poll legitimately
    empties it, and skipping that write would be the over-broad fix.

    `{}` used to be parametrized here and has MOVED to the preserve-guard
    below. It is not a reported zero — it is a body that never reported at
    all, and reading it as authoritative-zero here while as_of() reads the
    same body as never-learned in the sibling script would have this changeset
    answering one question two opposite ways."""
    seeded = _mentions([(1, 10, "a"), (2, 10, "a")])

    # Requests 1-2 carry the mentions (a wider window for the baseline read
    # than a single seed poll gives); everything after is the quiet body.
    def payload_fn(n: int, path: str):
        return 200, (seeded if n <= 2 else empty_body)

    server = serve(payload_fn)
    _proc, seen = run_mention(server.port)

    assert _wait_until(lambda: "1\t" in seen.read_text()), (
        f"seen-set was never seeded from the mentions payload; got {_peek(seen)}"
    )
    assert _wait_until(lambda: seen.read_text() == ""), (
        f"{empty_body!r} parses, so the quiet-poll rewrite of $SEEN must still "
        f"happen; $SEEN is {_peek(seen)}"
    )
    # ...and keeps happening: a skipped cycle would freeze $SEEN entirely.
    mtime = seen.stat().st_mtime_ns
    assert _wait_until(lambda: seen.stat().st_mtime_ns != mtime), (
        f"$SEEN stopped being rewritten on a parseable body ({empty_body!r}) — "
        "parsed-and-empty was misclassified as failed-to-parse"
    )


def test_updates_accepts_the_largest_comparable_watermark(serve, run_updates):
    """2**63 - 1 IS a valid watermark and must stay one.

    The ceiling exists because it is exactly the domain bash's `[ -ge ]` can
    evaluate — the poll loop is the consumer, and a watermark our own
    comparison cannot evaluate is not usable. That reasoning makes the bound
    INCLUSIVE at 2**63 - 1, and this test exists so nobody later "tightens" it
    into rejecting legitimate large watermarks. Measured directly:
    `[ 9223372036854775807 -ge 0 ]` succeeds; `[ 9223372036854775808 -ge 0 ]`
    exits 2, which is why 2**63 sits in _NO_WATERMARK above."""
    biggest = 2**63 - 1
    server = serve(lambda n, path: (200, '{"updates":[],"as_of":%d}' % biggest))
    _proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, str(biggest)), (
        f"the largest bash-comparable watermark ({biggest}) must seed the "
        f"cursor; got {_peek(cursor)} — the ceiling has been tightened past "
        "the range it was derived from"
    )


# Bodies that PARSE cleanly but never REPORT a mentions list. The mention-side
# twin of _NO_WATERMARK: nothing is wrong with the JSON, it just does not
# answer the question asked. `{}` lives HERE and not under the rewrite-guard —
# a missing "mentions" key has not reported zero mentions, it has failed to
# report, and as_of() reads the identical body the identical way.
_NO_MENTIONS_LIST = [
    pytest.param("{}", id="empty-object"),
    pytest.param('{"error":"maintenance"}', id="error-envelope"),
    pytest.param("[]", id="json-array"),
    pytest.param("null", id="json-null"),
    pytest.param('{"mentions":"garbage"}', id="mentions-not-a-list"),
]


# --- the bug: a body we could not READ must preserve $SEEN -----------------

@pytest.mark.parametrize("bad_body", _UNPARSEABLE)
def test_mention_poll_preserves_seen_on_malformed_200(serve, run_mention, bad_body):
    """Site 2 — the POLL path. Pre-fix, ids() printed nothing on an unparseable
    body, `$SEEN.now` was empty and the unconditional `mv` overwrote `$SEEN`
    empty; the next good poll would then mark every historical mention new.
    `$SEEN` must be byte-identical, and no `$SEEN.now` scratch file may be left
    behind for a later cycle to pick up."""
    seeded = _mentions([(1, 10, "a"), (2, 10, "a"), (3, 11, "b")])

    # Requests 1-2 are good (seed + one honest poll), so the baseline read
    # below is not racing a single 0.2s window; the malformed run starts at 3.
    def payload_fn(n: int, path: str):
        return 200, (seeded if n <= 2 else bad_body)

    server = serve(payload_fn)
    _proc, seen = run_mention(server.port)

    assert _wait_until(lambda: b"3\t" in seen.read_bytes()), (
        f"seen-set was never seeded from the mentions payload; got {_peek(seen)}"
    )
    before = seen.read_bytes()

    _wait_for_requests(server, server.request_count + 3)
    time.sleep(0.3)

    assert seen.read_bytes() == before, (
        f"$SEEN was clobbered by a body that did not parse ({bad_body!r})"
    )
    assert _no_stale_scratch(seen), (
        "a stale $SEEN.now was left behind by a skipped cycle"
    )


def test_mention_malformed_200_then_recovery_does_not_replay_history(
    serve, run_mention
):
    """The consequence the preserved `$SEEN` prevents, asserted directly: a
    malformed 200 followed by a good poll must not announce the already-seen
    mentions as new."""
    seeded = _mentions([(1, 10, "a"), (2, 10, "a"), (3, 11, "b")])

    def payload_fn(n: int, path: str):
        if n == 1:
            return 200, seeded
        if n in (2, 3):
            return 200, "<html>maintenance</html>"
        return 200, seeded

    server = serve(payload_fn)
    proc, _seen = run_mention(server.port)

    _wait_for_requests(server, 8)
    time.sleep(1.5)  # past the 1s coalesce window, so any burst would have flushed

    flushes = [l for l in proc.lines if _FLUSH_RE.match(l)]
    assert flushes == [], (
        f"recovery replayed already-seen mentions as new: {flushes!r}"
    )


# --- the mention monitor's own SEED path (the twin of Site 3) --------------

@pytest.mark.parametrize("bad_body", _UNPARSEABLE)
def test_mention_seed_does_not_baseline_empty_on_malformed_200(
    serve, run_mention, bad_body
):
    """The mention monitor's SEED has the same shape as the updates monitor's
    Site 3: it takes the first --fail-clean poll unconditionally. A malformed
    200 there yields no id lines, so the baseline is seeded EMPTY and the first
    good poll announces every historical mention as new — the arm-time arrival
    of the same clobber-replay the poll path guards against.

    Requests #1-2 are malformed; from #3 the forum answers honestly with three
    mentions that were already there before this monitor armed. Arming must not
    announce them."""

    seeded = _mentions([(1, 10, "a"), (2, 10, "a"), (3, 11, "b")])

    def payload_fn(n: int, path: str):
        return 200, (bad_body if n <= 2 else seeded)

    server = serve(payload_fn)
    proc, seen = run_mention(server.port)

    assert _wait_until(lambda: "3\t" in seen.read_text(), timeout=8.0), (
        f"seed never baselined from a parseable body; $SEEN is {_peek(seen)}"
    )
    time.sleep(1.5)  # past the 1s coalesce window, so any burst would have flushed

    flushes = [l for l in proc.lines if _FLUSH_RE.match(l)]
    assert flushes == [], (
        "arming announced pre-existing mentions as new — the seed baselined "
        f"empty from a malformed 200: {flushes!r}"
    )
    assert _no_stale_scratch(seen), (
        "a stale $SEEN.now was left behind by a retrying seed"
    )


@pytest.mark.parametrize("body", _NO_MENTIONS_LIST)
def test_mention_poll_preserves_seen_without_a_mentions_list(
    serve, run_mention, body
):
    """B2, the poll path. These bodies parse, so a parse-only predicate lets
    them through, ids() prints nothing, and the unconditional `mv` overwrites
    `$SEEN` empty — after which the next good poll marks every historical
    mention new. Identical consequence to an unparseable body; identical
    treatment required.

    `{}` is the load-bearing member: the issue's own table names it, and it is
    the body this changeset would otherwise have read as authoritative-zero
    here while reading it as never-learned in forum-updates-monitor.sh."""
    seeded = _mentions([(1, 10, "a"), (2, 10, "a"), (3, 11, "b")])

    def payload_fn(n: int, path: str):
        return 200, (seeded if n <= 2 else body)

    server = serve(payload_fn)
    _proc, seen = run_mention(server.port)

    assert _wait_until(lambda: b"3\t" in seen.read_bytes()), (
        f"seen-set was never seeded from the mentions payload; got {_peek(seen)}"
    )
    before = seen.read_bytes()

    _wait_for_requests(server, server.request_count + 3)
    time.sleep(0.3)

    assert seen.read_bytes() == before, (
        f"$SEEN was clobbered by {body!r}, which never reported a mentions list"
    )
    assert _no_stale_scratch(seen), (
        "a stale $SEEN.now was left behind by a skipped cycle"
    )


@pytest.mark.parametrize("body", _NO_MENTIONS_LIST)
def test_mention_seed_refuses_to_baseline_without_a_mentions_list(
    serve, run_mention, body
):
    """B2 at the seed. Same shape as the updates monitor's seed guard: a body
    that never reported cannot become the baseline, or arming announces every
    pre-existing mention as new. The seed must still be retrying, so `$SEEN`
    must not exist at all."""
    server = serve(lambda n, path: (200, body))
    _proc, seen = run_mention(server.port)

    _wait_for_requests(server, 3)  # the seed has retried at least twice
    assert not seen.exists(), (
        f"seed baselined $SEEN as {_peek(seen)} from {body!r}, which never "
        "reported a mentions list — the first good poll would then announce "
        "the entire history as new"
    )
    assert _no_stale_scratch(seen), (
        "a stale $SEEN.now was left behind by a retrying seed"
    )


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
