"""Integration tests for #3062 — an unreachable forum must be announced,
edge-triggered, not silently indistinguishable from a quiet-but-reachable one.

Both `forum-updates-monitor.sh` and `forum-mention-monitor.sh` poll with
`curl -s --fail ... || continue` (or the #2225 `as_of`/`ids` equivalent on the
seed path). Pre-#3062, a `curl --fail` non-zero (forum unreachable) and an
ordinary quiet successful poll (nothing new) produced BYTE-IDENTICAL output —
none — so the monitor's own expiry notice could not tell a multi-hour outage
from a calm night. Measured 2026-09-22: a 7h45m outage produced ~15
consecutive `no events delivered` expiry notices, identical to a quiet night.

The fix (mirroring umwelt-bell.py's post-umwelt#637 `_ARMED_REACHABLE`
pattern) is EDGE-TRIGGERED: one line on the transition INTO unreachable, one
on the transition back OUT, never once per cycle — level-triggered was
considered and rejected in that precedent for the same reason it would be
wrong here (an 8h outage at 30s cadence would be ~960 lines).

This file does NOT touch cursor / $SEEN handling — those are covered by
test_forum_monitor_malformed_200.py (#2225) and the never-clobber discipline
established by #743. "Forum down" is simulated the same way the existing
coalesce test simulates it (`test_forum_down_mid_window_preserves_buffer_and_
seen` in test_forum_mention_monitor_coalesce.py): an HTTP status >= 400, which
`curl --fail` rejects identically to a genuine connection failure — the code
path exercised (`|| continue` / `if ! RESP=...`) is the same regardless of
WHY curl failed.

Covers the four cases in issue #3062's Tests section:
  (a) entering unreachable announces exactly once
  (b) staying unreachable across multiple cycles announces nothing further
  (c) recovery announces exactly once
  (d) an ordinary quiet-but-reachable cycle announces nothing

#3067 extends the same coverage to the SEED path, which #3062 did not reach.
Both scripts seed by retrying until the first poll that succeeds AND parses /
yields a usable watermark, and that seed loop sits ABOVE the poll loop where
#3062's announce lives — so a monitor armed while the forum was down blocked
in the seed and emitted nothing at all, indistinguishable from a healthy
monitor on a quiet channel. Measured on `origin/dev` at the #3062 merge: 0
bytes of output in 8s against a dead endpoint, with a live-forum control on the
same flags that seeded immediately. Arming during an outage is the routine
case, not the rare one — seats arm at session start, and session start is what
follows a reboot, a redeploy, or the overnight shutdown.

The seed's BLOCKING is correct and unchanged (seeding from a failed or
unparseable body is the #743 / #2225 clobber-replay); only the silence is
fixed. Four further cases per script, mirroring the shape above:
  (e) armed during an outage announces once, then announces once on the seed
      that finally succeeds — and the cursor / $SEEN stays absent until then
  (f) repeated seed failures do not repeat the announce
  (g) a first seed attempt that succeeds announces nothing at all
  (h) armed during an outage WHILE THE USER IS ENGAGED defers the announce —
      silent inside the engaged window, then exactly one line on the first
      retry after it expires. Deferred, not swallowed: the suppressed branch
      must leave the edge latch clear, or the announce is lost forever and
      #3067's defect is back by another route.
  (i) engaged across the WHOLE outage AND the recovery emits NEITHER line —
      the pairing invariant the seed's success-line asymmetry rests on. Added
      on #3111's review, which showed (e)-(h) leave this case uncovered and
      that a plausible-wrong implementation passes all twelve of them.
  (j) a MALFORMED activity stamp announces and leaves the monitor ALIVE. The
      engaged check is arithmetic on a file's contents, and under `set -u` a
      non-numeric stamp used to kill the process outright — so this PR's own
      "a monitor must not be silently useless" would have shipped a corner
      where the monitor is silently DEAD.

The seed's announce wording is deliberately NOT the poll loop's "forum
unreachable" — see _seed_unreachable_lines below for why the two sites say
different things.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_UPDATES_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-updates-monitor.sh"
_MENTION_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-mention-monitor.sh"

_POLL = "0.2"  # seconds; overrides the 30s production cadence in both scripts

_UNREACHABLE_UPDATES = "forum unreachable"
_RECOVERED_UPDATES = "forum reachable again"


# ---------------------------------------------------------------------------
# mock server: scripted per-request (status, body), repeating the LAST entry
# once the script is exhausted (so a long-running poll loop never indexes
# past the end). Modeled on test_forum_monitor_malformed_200.py's
# _MockRawServer, minus the path-awareness this file does not need.
# ---------------------------------------------------------------------------

class _MockScriptedServer:
    def __init__(self, script: list[tuple[int, str]]):
        self._script = script
        self._count = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence default request logging
                pass

            def do_GET(self):
                with outer._lock:
                    outer._count += 1
                    idx = min(outer._count - 1, len(outer._script) - 1)
                status, body = outer._script[idx]
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
    servers = []

    def _start(script: list[tuple[int, str]]):
        s = _MockScriptedServer(script)
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

    @property
    def alive(self) -> bool:
        """Whether the monitor process is still running. Needed by the
        malformed-stamp tests: an engaged-check that dies under `set -u` takes
        the whole monitor with it, and a DEAD monitor is silent in a way no
        output assertion can distinguish from a correctly quiet one."""
        return self._proc.poll() is None

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


def _home_path(tmp_path: Path) -> Path:
    """The single place the fake $HOME's location is decided, so a test can
    reach into it (see _activity_stamp) without re-deriving the path."""
    return tmp_path / "home"


def _activity_stamp(tmp_path: Path) -> Path:
    """The last-user-activity stamp inside the fake $HOME — the same path both
    scripts read for engaged-detection. A test writes it BEFORE the monitor
    starts (via the fixtures' engaged_age= argument) and may re-write it
    mid-run to simulate the engaged window expiring, which the scripts see
    because they re-read the file on every attempt."""
    return _home_path(tmp_path) / ".engram" / "last-user-activity"


def _set_activity_age(tmp_path: Path, age_seconds: float) -> None:
    """Stamp last-user-activity as `age_seconds` old. 0 = engaged right now;
    anything above the scripts' _ENGAGED_WINDOW (360) = window expired."""
    stamp = _activity_stamp(tmp_path)
    stamp.write_text(f"{int(time.time()) - int(age_seconds)}\n")


def _make_home(tmp_path: Path, port: int) -> Path:
    home = _home_path(tmp_path)
    (home / ".engram").mkdir(parents=True)
    config = {"agent_name": "testagent", "forum": {"url": f"http://127.0.0.1:{port}"}}
    (home / ".engram" / "config.json").write_text(json.dumps(config))
    return home


@pytest.fixture
def run_updates(tmp_path):
    procs = []

    def _run(port: int, poll_seconds: str = _POLL, engaged_age: float | None = None,
             stamp_text: str | None = None):
        home = _make_home(tmp_path, port)
        # Written before Popen on purpose: a stamp created after the monitor
        # starts races the first seed attempt, and the announce would already
        # have been emitted.
        if engaged_age is not None:
            _set_activity_age(tmp_path, engaged_age)
        # stamp_text writes the file VERBATIM, bypassing the int formatting in
        # _set_activity_age — the only way to hand the scripts a stamp that is
        # not a well-formed epoch. Same pre-Popen placement, same reason.
        if stamp_text is not None:
            _activity_stamp(tmp_path).write_text(stamp_text)
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
    procs = []

    def _run(port: int, poll_seconds: str = _POLL, coalesce_seconds: str = "1",
             engaged_age: float | None = None, stamp_text: str | None = None):
        home = _make_home(tmp_path, port)
        # See the updates fixture: the stamp must predate the process, and
        # stamp_text writes it verbatim so a malformed stamp can be served.
        if engaged_age is not None:
            _set_activity_age(tmp_path, engaged_age)
        if stamp_text is not None:
            _activity_stamp(tmp_path).write_text(stamp_text)
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


def _wait_for_requests(server: _MockScriptedServer, n: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.request_count >= n:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"mock server saw only {server.request_count} requests, expected >= {n}"
    )


def _unreachable_lines(lines: list[str]) -> list[str]:
    return [l for l in lines if "forum unreachable" in l]


def _recovered_lines(lines: list[str]) -> list[str]:
    return [l for l in lines if "forum reachable again" in l]


# #3067 — the SEED-path pair. DISJOINT substrings from the poll-loop pair
# above, so a test can tell WHICH loop spoke, and so the two predicates can be
# asserted independently: the seed lines match NEITHER _unreachable_lines nor
# _recovered_lines, which is what lets the armed-during-an-outage tests below
# assert "the poll loop stayed completely quiet" as `== []`.
#
# The seed deliberately does NOT say "forum unreachable" (it did in this
# change's first round, purely so _unreachable_lines would match it, and that
# wording was false). The poll loop's announce sits inside the curl-failure
# branch, so "unreachable" is exactly what it observed; the seed's failure
# predicate is broader — a reachable HTTP 200 whose body does not parse or
# carries no usable watermark fails the seed too, and the malformed-200 suite
# exists because that happens. So the seed reports what it actually knows: it
# cannot SEED. Its success line says "seeded from the forum" rather than
# "reachable again" for the same reason — on the seed path the forum was never
# reachable in the first place.
def _seed_unreachable_lines(lines: list[str]) -> list[str]:
    return [l for l in lines if "not yet seeded" in l]


def _seeded_lines(lines: list[str]) -> list[str]:
    return [l for l in lines if "seeded from the forum" in l]


# How many consecutive requests the mock fails before recovering, in the
# armed-during-an-outage tests. Every one of these is consumed by the SEED
# loop (it retries in place and the poll loop never runs), so the recovery
# entry lands on seed attempt _SEED_DOWN_ATTEMPTS + 1 — getting this
# accounting wrong is the easiest way to make one of these tests pass for the
# wrong reason. At the _POLL cadence the failing phase lasts ~1.2s, which is
# the window the cursor/$SEEN-absent invariant below is checked across.
_SEED_DOWN_ATTEMPTS = 6

# How many failed seed attempts the engaged-deferral tests wait to OBSERVE
# before concluding the announce was suppressed. Asserted via the mock's
# request count rather than a sleep, so "it said nothing" can never be
# satisfied by a monitor that never got as far as making a request.
_ENGAGED_QUIET_ATTEMPTS = 4


def _assert_absent_until_seed_succeeds(path: Path, server: _MockScriptedServer) -> None:
    """The baseline file must stay ABSENT for as long as every attempt so far
    has failed. A cursor / seen-set written from a failed seed is exactly the
    #743 / #2225 clobber-replay the seed's retry exists to prevent, so this is
    asserted continuously across the failing phase rather than once.

    Race-free in the sound direction: request_count is monotonic, so if the
    file exists at time T and the count is STILL inside the failing phase at
    T' > T, it was necessarily written before any successful request arrived.
    """
    while server.request_count <= _SEED_DOWN_ATTEMPTS:
        if path.exists():
            assert server.request_count > _SEED_DOWN_ATTEMPTS, (
                f"{path.name} was written while all {_SEED_DOWN_ATTEMPTS} seed "
                f"attempts so far had FAILED (request_count="
                f"{server.request_count}) — a baseline from a failed seed is "
                f"the #743 clobber-replay"
            )
            return
        time.sleep(0.02)


_UPDATES_QUIET = json.dumps({"updates": [], "as_of": 7095})
_UPDATES_DOWN = (500, '{"error":"forum down"}')

_MENTIONS_QUIET = json.dumps({"mentions": []})
_MENTIONS_DOWN = (500, '{"error":"forum down"}')


# ===========================================================================
# forum-updates-monitor.sh
# ===========================================================================

def test_updates_enters_stays_and_recovers_from_unreachable(serve, run_updates):
    """(a) entering unreachable announces exactly once, (b) staying
    unreachable across multiple cycles announces nothing further, (c)
    recovery announces exactly once — all three asserted on the same
    request sequence so the ORDER is pinned too, not just the counts.

    Sequence: #1 seed (up) -> #2 one ordinary quiet poll (up) -> #3-6 down
    (4 consecutive failed cycles) -> #7+ up again.
    """
    script = [
        (200, _UPDATES_QUIET),  # 1: seed
        (200, _UPDATES_QUIET),  # 2: ordinary quiet poll
        _UPDATES_DOWN,          # 3: enters unreachable
        _UPDATES_DOWN,          # 4: stays unreachable
        _UPDATES_DOWN,          # 5: stays unreachable
        _UPDATES_DOWN,          # 6: stays unreachable
        (200, _UPDATES_QUIET),  # 7: recovers
        (200, _UPDATES_QUIET),  # 8: stays reachable
    ]
    server = serve(script)
    proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "7095"), "seed never baselined"
    _wait_for_requests(server, 8)
    time.sleep(0.4)  # let the drain threads catch up on the last cycle's output

    unreachable = _unreachable_lines(proc.lines)
    recovered = _recovered_lines(proc.lines)

    assert len(unreachable) == 1, (
        f"expected exactly one 'unreachable' announce across the whole "
        f"4-cycle outage, got {unreachable!r} (full output: {proc.lines!r})"
    )
    assert len(recovered) == 1, (
        f"expected exactly one 'reachable again' announce on recovery, got "
        f"{recovered!r} (full output: {proc.lines!r})"
    )
    # order: unreachable strictly before recovered
    assert proc.lines.index(unreachable[0]) < proc.lines.index(recovered[0]), (
        f"unreachable/recovered lines out of order: {proc.lines!r}"
    )


def test_updates_ordinary_quiet_reachable_cycle_announces_nothing(serve, run_updates):
    """(d) an ordinary quiet-but-reachable cycle must announce nothing at
    all — the whole point of the fix is that reachable silence stays silent."""
    server = serve([(200, _UPDATES_QUIET)])
    proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "7095"), "seed never baselined"
    _wait_for_requests(server, 5)
    time.sleep(0.4)

    assert _unreachable_lines(proc.lines) == [], (
        f"a reachable poll cycle announced 'unreachable': {proc.lines!r}"
    )
    assert _recovered_lines(proc.lines) == [], (
        f"a reachable poll cycle (no prior outage) announced 'recovered': "
        f"{proc.lines!r}"
    )


def test_updates_armed_during_outage_announces_once_then_seeded_once(serve, run_updates):
    """(e) #3067: armed while the forum is DOWN — the seed blocks, so the
    poll-loop announce is unreachable. Assert the seed announces exactly once
    on the way in, exactly once when it finally succeeds, in that order, and
    that the cursor is written only by the successful attempt.

    Request accounting: every failing request is a SEED attempt (the poll loop
    is not running yet), so the recovery lands on attempt
    _SEED_DOWN_ATTEMPTS + 1 and the poll loop starts after that.
    """
    script = [_UPDATES_DOWN] * _SEED_DOWN_ATTEMPTS + [(200, _UPDATES_QUIET)]
    server = serve(script)
    proc, cursor = run_updates(server.port)

    assert _wait_until(lambda: _seed_unreachable_lines(proc.lines)), (
        f"armed during an outage and announced NOTHING — this is #3067's "
        f"whole defect (stdout: {proc.lines!r}, stderr: {proc.errs!r})"
    )
    _assert_absent_until_seed_succeeds(cursor, server)

    assert _wait_for_content(cursor, "7095"), (
        f"seed never baselined after the forum came back (stdout: "
        f"{proc.lines!r}, stderr: {proc.errs!r})"
    )
    _wait_for_requests(server, _SEED_DOWN_ATTEMPTS + 3)
    time.sleep(0.4)

    seed_unreachable = _seed_unreachable_lines(proc.lines)
    seeded = _seeded_lines(proc.lines)

    assert len(seed_unreachable) == 1, (
        f"expected exactly one seed 'not yet seeded' announce across a "
        f"{_SEED_DOWN_ATTEMPTS}-attempt outage, got {seed_unreachable!r} "
        f"(full output: {proc.lines!r})"
    )
    assert len(seeded) == 1, (
        f"expected exactly one 'seeded from the forum' announce on the "
        f"successful seed, got {seeded!r} (full output: {proc.lines!r})"
    )
    assert proc.lines.index(seed_unreachable[0]) < proc.lines.index(seeded[0]), (
        f"seed announce order wrong: {proc.lines!r}"
    )
    # The POLL loop never spoke: it starts in the reachable state and every
    # request it makes succeeds. Asserted as `== []` rather than
    # `== seed_unreachable` because the seed's line no longer carries the
    # "forum unreachable" substring (see _seed_unreachable_lines' note) — the
    # property under test is the same, only the string it is asserted on moved,
    # and `== []` is the stricter form of it.
    assert _unreachable_lines(proc.lines) == [], (
        f"poll-loop announce fired unexpectedly: {proc.lines!r}"
    )
    assert _recovered_lines(proc.lines) == [], (
        f"poll-loop recovery announce fired unexpectedly: {proc.lines!r}"
    )


def test_updates_repeated_seed_failures_announce_only_once(serve, run_updates):
    """(f) #3067: the seed retries on $POLL_SECONDS, so a level-triggered
    announce here would be the same ~960-lines-per-8h flood umwelt#637
    rejected. The mock repeats its last entry forever, so the forum never
    comes back: many attempts, still exactly one line."""
    server = serve([_UPDATES_DOWN])
    proc, cursor = run_updates(server.port)

    _wait_for_requests(server, 6)
    time.sleep(0.4)

    assert len(_seed_unreachable_lines(proc.lines)) == 1, (
        f"expected exactly one announce across {server.request_count} failed "
        f"seed attempts, got {proc.lines!r}"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"announced 'seeded' while the seed has never succeeded: {proc.lines!r}"
    )
    assert not cursor.exists(), "cursor written despite no successful seed"


def test_updates_first_seed_attempt_succeeds_announces_nothing(serve, run_updates):
    """(g) #3067's negative control, mirroring (d): when the first seed
    attempt succeeds — the normal arm — the healthy path stays silent."""
    server = serve([(200, _UPDATES_QUIET)])
    proc, cursor = run_updates(server.port)

    assert _wait_for_content(cursor, "7095"), "seed never baselined"
    _wait_for_requests(server, 5)
    time.sleep(0.4)

    assert _seed_unreachable_lines(proc.lines) == [], (
        f"a first-attempt-succeeds seed announced 'not yet seeded': "
        f"{proc.lines!r}"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"a first-attempt-succeeds seed announced 'seeded from the forum' — "
        f"the healthy arm must be silent: {proc.lines!r}"
    )
    assert proc.lines == [], f"healthy arm emitted output at all: {proc.lines!r}"


def test_updates_seed_announce_deferred_while_engaged_then_emitted_once(
    serve, run_updates, tmp_path
):
    """(h) #3067: engaged-window handling on the seed announce — DEFERRED, not
    swallowed.

    The poll loop's engaged semantics are skip-the-cycle-and-emit-once-the-
    window-expires, and the seed must match. That forces the suppressed branch
    NOT to set $_SEED_ANNOUNCED: a latch set for a line that was never printed
    swallows the announce FOREVER, because nothing re-offers it — the seed just
    goes on retrying in silence and the operator never learns the monitor is
    unseeded. That is #3067's own defect, reintroduced through the suppression
    path, so a test that only checks the silence is not testing deferral.

    Two phases on ONE process, so the deferral is proved rather than inferred:
      1. armed with a fresh activity stamp against a permanently-down forum —
         several failed seed attempts happen (asserted via the mock's request
         count, so the silence cannot be a monitor that never ran) and NOTHING
         is announced;
      2. the stamp is aged past the scripts' _ENGAGED_WINDOW (360s) in place.
         Both scripts re-read it on every attempt, so the next retry must
         announce — and, still edge-triggered, exactly once across the many
         attempts after that.
    Phase 2 is the half a latch-while-suppressed mutation cannot survive.
    """
    server = serve([_UPDATES_DOWN])
    proc, cursor = run_updates(server.port, engaged_age=0)

    # phase 1 — engaged: silence, across attempts that demonstrably happened.
    _wait_for_requests(server, _ENGAGED_QUIET_ATTEMPTS)
    time.sleep(0.4)
    assert _seed_unreachable_lines(proc.lines) == [], (
        f"the seed announced while the user was engaged — the announce must be "
        f"deferred to the far side of the window: {proc.lines!r}"
    )
    assert proc.lines == [], (
        f"an engaged, unseeded monitor emitted output at all: {proc.lines!r}"
    )

    # phase 2 — window expired: the deferred announce must arrive.
    _set_activity_age(tmp_path, 10_000)
    assert _wait_until(lambda: _seed_unreachable_lines(proc.lines)), (
        f"the deferred announce was SWALLOWED: the engaged window expired, the "
        f"seed kept failing, and it still said nothing. This is the "
        f"latch-while-suppressed bug (stdout: {proc.lines!r}, stderr: "
        f"{proc.errs!r})"
    )
    _wait_for_requests(server, server.request_count + 4)
    time.sleep(0.4)

    assert len(_seed_unreachable_lines(proc.lines)) == 1, (
        f"expected exactly one announce after the engaged window expired — the "
        f"deferral must not cost the edge-trigger — got "
        f"{_seed_unreachable_lines(proc.lines)!r} across "
        f"{server.request_count} attempts (full output: {proc.lines!r})"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"announced 'seeded' while the seed has never succeeded: {proc.lines!r}"
    )
    assert not cursor.exists(), "cursor written despite no successful seed"


def test_updates_engaged_through_outage_and_recovery_says_nothing(serve, run_updates):
    """(i) #3111: the PAIRING invariant the seed's success-line asymmetry rests
    on. The failure line is engaged-suppressed and the success line is not, and
    what makes that safe is that the success line is gated on the SAME latch
    that is only ever set when the warning was actually PRINTED. So an
    engaged-suppressed warning must imply a suppressed success, and an unpaired
    "now receiving" — a monitor reporting it recovered from an outage it never
    reported — is unreachable.

    That invariant was true at #3111's head but NOT test-enforced: the review
    built a plausible-wrong implementation (keep the latch, add a separate
    counter set unconditionally on every failed attempt, gate the success line
    on the counter) and all twelve tests in this file passed. The uncovered
    case is engaged across BOTH the outage AND the recovery, which is the only
    path that can produce the unpaired line: (h) keeps the forum down forever
    so it never reaches a success line, and (e)-(g) are never engaged.

    Do NOT reach for _wait_for_requests here: while engaged the poll loop
    `continue`s BEFORE its curl, so request_count stalls once the seed
    completes and the wait would fail in the harness rather than the code. The
    cursor appearing is the signal that the seed succeeded.
    """
    server = serve([_UPDATES_DOWN] * 3 + [(200, _UPDATES_QUIET)])
    proc, cursor = run_updates(server.port, engaged_age=0)

    assert _wait_for_content(cursor, "7095"), (
        f"never seeded, so the recovery this test is about never happened "
        f"(stdout: {proc.lines!r}, stderr: {proc.errs!r})"
    )
    time.sleep(1.0)

    assert _seed_unreachable_lines(proc.lines) == [], (
        f"the failure line fired while engaged: {proc.lines!r}"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"UNPAIRED 'now receiving': the warning was engaged-suppressed but the "
        f"success line fired anyway, so the operator is told the monitor "
        f"recovered from an outage they were never told about: {proc.lines!r}"
    )


def test_updates_malformed_activity_stamp_announces_and_stays_alive(
    serve, run_updates
):
    """(j) #3111: a malformed activity stamp must fail towards ANNOUNCE, and
    must not kill the monitor.

    The engaged check computes `now - $(cat stamp)` in shell arithmetic. Under
    `set -u` a non-numeric stamp makes the expansion itself fail, so the
    `|| _age=999999` fallback never runs and `_age` is left unset — measured
    on #3111: `unbound variable`, rc=1, and the process EXITS. Pre-#3067 that
    corner was harmless because the seed never consulted the engaged window:
    the monitor blocked silently but stayed ALIVE, so it delivered once the
    forum returned. Post-#3067 the seed calls the check on its first failed
    attempt, which turns "alive but silent" into "dead" — a smaller instance of
    the very defect this PR fixes.

    Both halves are asserted, and the aliveness half is the load-bearing one:
    a dead monitor is silent in a way no output assertion can distinguish from
    a correctly quiet one.
    """
    server = serve([_UPDATES_DOWN])
    proc, cursor = run_updates(server.port, stamp_text="garbage\n")

    assert _wait_until(lambda: _seed_unreachable_lines(proc.lines)), (
        f"a malformed stamp silenced the arm-time announce — the engaged check "
        f"must fail towards announcing (stdout: {proc.lines!r}, stderr: "
        f"{proc.errs!r})"
    )
    # Attempts AFTER the announce: proves the retry loop is still turning, so
    # "alive" is not merely "has not exited yet".
    _wait_for_requests(server, server.request_count + 3)
    time.sleep(0.4)

    assert proc.alive, (
        f"the monitor DIED on a malformed activity stamp (stderr: "
        f"{proc.errs!r}, stdout: {proc.lines!r})"
    )
    assert len(_seed_unreachable_lines(proc.lines)) == 1, (
        f"expected exactly one announce across {server.request_count} failed "
        f"attempts with a malformed stamp, got {proc.lines!r}"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"announced 'seeded' while the seed has never succeeded: {proc.lines!r}"
    )
    assert not cursor.exists(), "cursor written despite no successful seed"


# ===========================================================================
# forum-mention-monitor.sh
# ===========================================================================

def test_mention_enters_stays_and_recovers_from_unreachable(serve, run_mention):
    """Same three-part proof as the updates monitor, for the sibling script."""
    script = [
        (200, _MENTIONS_QUIET),  # 1: seed
        (200, _MENTIONS_QUIET),  # 2: ordinary quiet poll
        _MENTIONS_DOWN,          # 3: enters unreachable
        _MENTIONS_DOWN,          # 4: stays unreachable
        _MENTIONS_DOWN,          # 5: stays unreachable
        _MENTIONS_DOWN,          # 6: stays unreachable
        (200, _MENTIONS_QUIET),  # 7: recovers
        (200, _MENTIONS_QUIET),  # 8: stays reachable
    ]
    server = serve(script)
    proc, seen = run_mention(server.port)

    assert _wait_until(lambda: seen.is_file()), "seed never baselined $SEEN"
    _wait_for_requests(server, 8)
    time.sleep(0.4)

    unreachable = _unreachable_lines(proc.lines)
    recovered = _recovered_lines(proc.lines)

    assert len(unreachable) == 1, (
        f"expected exactly one 'unreachable' announce across the whole "
        f"4-cycle outage, got {unreachable!r} (full output: {proc.lines!r})"
    )
    assert len(recovered) == 1, (
        f"expected exactly one 'reachable again' announce on recovery, got "
        f"{recovered!r} (full output: {proc.lines!r})"
    )
    assert proc.lines.index(unreachable[0]) < proc.lines.index(recovered[0]), (
        f"unreachable/recovered lines out of order: {proc.lines!r}"
    )


def test_mention_ordinary_quiet_reachable_cycle_announces_nothing(serve, run_mention):
    """(d) for the mention monitor."""
    server = serve([(200, _MENTIONS_QUIET)])
    proc, seen = run_mention(server.port)

    assert _wait_until(lambda: seen.is_file()), "seed never baselined $SEEN"
    _wait_for_requests(server, 5)
    time.sleep(0.4)

    assert _unreachable_lines(proc.lines) == [], (
        f"a reachable poll cycle announced 'unreachable': {proc.lines!r}"
    )
    assert _recovered_lines(proc.lines) == [], (
        f"a reachable poll cycle (no prior outage) announced 'recovered': "
        f"{proc.lines!r}"
    )


def test_mention_armed_during_outage_announces_once_then_seeded_once(serve, run_mention):
    """(e) #3067 for the mention monitor. Same request accounting: every
    failing request is consumed by the SEED loop, so recovery lands on attempt
    _SEED_DOWN_ATTEMPTS + 1. $SEEN must not appear until then — a seen-set
    written from a failed seed hands the next good poll every historical
    mention as new (#743)."""
    script = [_MENTIONS_DOWN] * _SEED_DOWN_ATTEMPTS + [(200, _MENTIONS_QUIET)]
    server = serve(script)
    proc, seen = run_mention(server.port)

    assert _wait_until(lambda: _seed_unreachable_lines(proc.lines)), (
        f"armed during an outage and announced NOTHING — this is #3067's "
        f"whole defect (stdout: {proc.lines!r}, stderr: {proc.errs!r})"
    )
    _assert_absent_until_seed_succeeds(seen, server)

    assert _wait_until(lambda: seen.is_file()), (
        f"seed never baselined $SEEN after the forum came back (stdout: "
        f"{proc.lines!r}, stderr: {proc.errs!r})"
    )
    _wait_for_requests(server, _SEED_DOWN_ATTEMPTS + 3)
    time.sleep(0.4)

    seed_unreachable = _seed_unreachable_lines(proc.lines)
    seeded = _seeded_lines(proc.lines)

    assert len(seed_unreachable) == 1, (
        f"expected exactly one seed 'not yet seeded' announce across a "
        f"{_SEED_DOWN_ATTEMPTS}-attempt outage, got {seed_unreachable!r} "
        f"(full output: {proc.lines!r})"
    )
    assert len(seeded) == 1, (
        f"expected exactly one 'seeded from the forum' announce on the "
        f"successful seed, got {seeded!r} (full output: {proc.lines!r})"
    )
    assert proc.lines.index(seed_unreachable[0]) < proc.lines.index(seeded[0]), (
        f"seed announce order wrong: {proc.lines!r}"
    )
    # `== []`, not `== seed_unreachable`: same reasoning as the updates twin
    # above — the seed's line no longer carries "forum unreachable".
    assert _unreachable_lines(proc.lines) == [], (
        f"poll-loop announce fired unexpectedly: {proc.lines!r}"
    )
    assert _recovered_lines(proc.lines) == [], (
        f"poll-loop recovery announce fired unexpectedly: {proc.lines!r}"
    )


def test_mention_repeated_seed_failures_announce_only_once(serve, run_mention):
    """(f) for the mention monitor — the mock repeats its last entry forever,
    so the forum never returns: many failed attempts, still exactly one line."""
    server = serve([_MENTIONS_DOWN])
    proc, seen = run_mention(server.port)

    _wait_for_requests(server, 6)
    time.sleep(0.4)

    assert len(_seed_unreachable_lines(proc.lines)) == 1, (
        f"expected exactly one announce across {server.request_count} failed "
        f"seed attempts, got {proc.lines!r}"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"announced 'seeded' while the seed has never succeeded: {proc.lines!r}"
    )
    assert not seen.exists(), "$SEEN written despite no successful seed"


def test_mention_first_seed_attempt_succeeds_announces_nothing(serve, run_mention):
    """(g) for the mention monitor — the normal arm stays silent."""
    server = serve([(200, _MENTIONS_QUIET)])
    proc, seen = run_mention(server.port)

    assert _wait_until(lambda: seen.is_file()), "seed never baselined $SEEN"
    _wait_for_requests(server, 5)
    time.sleep(0.4)

    assert _seed_unreachable_lines(proc.lines) == [], (
        f"a first-attempt-succeeds seed announced 'not yet seeded': "
        f"{proc.lines!r}"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"a first-attempt-succeeds seed announced 'seeded from the forum' — "
        f"the healthy arm must be silent: {proc.lines!r}"
    )
    assert proc.lines == [], f"healthy arm emitted output at all: {proc.lines!r}"


def test_mention_seed_announce_deferred_while_engaged_then_emitted_once(
    serve, run_mention, tmp_path
):
    """(h) for the mention monitor — same two-phase proof, same reason: the
    suppressed branch must leave the latch clear or the announce is swallowed
    forever. See the updates twin's docstring."""
    server = serve([_MENTIONS_DOWN])
    proc, seen = run_mention(server.port, engaged_age=0)

    _wait_for_requests(server, _ENGAGED_QUIET_ATTEMPTS)
    time.sleep(0.4)
    assert _seed_unreachable_lines(proc.lines) == [], (
        f"the seed announced while the user was engaged — the announce must be "
        f"deferred to the far side of the window: {proc.lines!r}"
    )
    assert proc.lines == [], (
        f"an engaged, unseeded monitor emitted output at all: {proc.lines!r}"
    )

    _set_activity_age(tmp_path, 10_000)
    assert _wait_until(lambda: _seed_unreachable_lines(proc.lines)), (
        f"the deferred announce was SWALLOWED: the engaged window expired, the "
        f"seed kept failing, and it still said nothing. This is the "
        f"latch-while-suppressed bug (stdout: {proc.lines!r}, stderr: "
        f"{proc.errs!r})"
    )
    _wait_for_requests(server, server.request_count + 4)
    time.sleep(0.4)

    assert len(_seed_unreachable_lines(proc.lines)) == 1, (
        f"expected exactly one announce after the engaged window expired — the "
        f"deferral must not cost the edge-trigger — got "
        f"{_seed_unreachable_lines(proc.lines)!r} across "
        f"{server.request_count} attempts (full output: {proc.lines!r})"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"announced 'seeded' while the seed has never succeeded: {proc.lines!r}"
    )
    assert not seen.exists(), "$SEEN written despite no successful seed"


def test_mention_engaged_through_outage_and_recovery_says_nothing(serve, run_mention):
    """(i) for the mention monitor — same pairing invariant, same uncovered
    case, same plausible-wrong mutation reddens it. See the updates twin's
    docstring, including why _wait_for_requests must not be used here."""
    server = serve([_MENTIONS_DOWN] * 3 + [(200, _MENTIONS_QUIET)])
    proc, seen = run_mention(server.port, engaged_age=0)

    assert _wait_until(lambda: seen.is_file()), (
        f"never seeded $SEEN, so the recovery this test is about never "
        f"happened (stdout: {proc.lines!r}, stderr: {proc.errs!r})"
    )
    time.sleep(1.0)

    assert _seed_unreachable_lines(proc.lines) == [], (
        f"the failure line fired while engaged: {proc.lines!r}"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"UNPAIRED 'now receiving': the warning was engaged-suppressed but the "
        f"success line fired anyway, so the operator is told the monitor "
        f"recovered from an outage they were never told about: {proc.lines!r}"
    )


def test_mention_malformed_activity_stamp_announces_and_stays_alive(
    serve, run_mention
):
    """(j) for the mention monitor — a malformed stamp must announce and must
    not kill the process. See the updates twin's docstring for why the
    aliveness half is the load-bearing assertion."""
    server = serve([_MENTIONS_DOWN])
    proc, seen = run_mention(server.port, stamp_text="garbage\n")

    assert _wait_until(lambda: _seed_unreachable_lines(proc.lines)), (
        f"a malformed stamp silenced the arm-time announce — the engaged check "
        f"must fail towards announcing (stdout: {proc.lines!r}, stderr: "
        f"{proc.errs!r})"
    )
    _wait_for_requests(server, server.request_count + 3)
    time.sleep(0.4)

    assert proc.alive, (
        f"the monitor DIED on a malformed activity stamp (stderr: "
        f"{proc.errs!r}, stdout: {proc.lines!r})"
    )
    assert len(_seed_unreachable_lines(proc.lines)) == 1, (
        f"expected exactly one announce across {server.request_count} failed "
        f"attempts with a malformed stamp, got {proc.lines!r}"
    )
    assert _seeded_lines(proc.lines) == [], (
        f"announced 'seeded' while the seed has never succeeded: {proc.lines!r}"
    )
    assert not seen.exists(), "$SEEN written despite no successful seed"

# ===========================================================================
# #3106 — a STALLED response (headers sent, body withheld, connection never
# closed) must reach the SAME unreachable announce a curl --fail failure
# does. curl --fail only fires on a non-2xx status or a connection failure;
# a connection that is accepted and sends a valid 200 header but then never
# finishes the body passes --fail and never returns AT ALL without
# --max-time, so pre-#3106 code hangs the curl call indefinitely instead of
# skipping the cycle. This is deliberately a RAW socket server, not
# _MockScriptedServer (BaseHTTPRequestHandler's .wfile.write() sends the
# whole body in one buffered call — there is no way to make it stop midway).
# ===========================================================================

class _MockStallingServer:
    """Raw-socket server: each connection gets one scripted outcome —
    ("respond", status, body) behaves like _MockScriptedServer, or "stall"
    to send a valid 200 + Content-Length header and then withhold the body
    forever (never closes). Repeats the LAST script entry once exhausted,
    matching _MockScriptedServer's convention.
    """

    def __init__(self, script: list):
        self._script = script
        self._count = 0
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.5)  # so the accept loop can notice _stop
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._conns: list = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._count

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        self._thread.join(timeout=2)

    def _accept_loop(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        try:
            conn.settimeout(5)
            conn.recv(65536)  # drain the request line/headers; body unused
            with self._lock:
                self._count += 1
                idx = min(self._count - 1, len(self._script) - 1)
            entry = self._script[idx]
            if entry == "stall":
                body_len = 100
                header = (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {body_len}\r\n\r\n"
                ).encode()
                conn.sendall(header)
                # Send nothing further; hold the connection open until the
                # test tears the server down (stop() closes every tracked
                # conn) or the client's own --max-time gives up first.
                while not self._stop:
                    time.sleep(0.05)
            else:
                status, body = entry
                payload = body.encode()
                resp = (
                    f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
                ).encode() + payload
                conn.sendall(resp)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture
def serve_stalling():
    servers = []

    def _start(script: list):
        s = _MockStallingServer(script)
        s.start()
        servers.append(s)
        return s

    yield _start
    for s in servers:
        s.stop()


def test_mention_seed_does_not_hang_forever_on_a_stalled_response(
    serve_stalling, run_mention, monkeypatch
):
    """#3106: the seed loop must not hang indefinitely on a connection that
    sends valid headers and then withholds the body. Script: stall twice,
    then answer normally — pre-fix, the very FIRST stalled connection never
    returns (no --max-time), so the seed can never reach attempt 3 within any
    bounded test window. Post-fix, each stalled attempt is bounded by
    --max-time and the seed succeeds once the server starts answering.
    """
    server = serve_stalling(["stall", "stall", (200, _MENTIONS_QUIET)])
    # Bound the client's per-request patience well under the test's own
    # wait budget so a hang is distinguishable from "just needs more time".
    monkeypatch.setenv("FORUM_MENTION_MAX_TIME_SECONDS", "1")
    proc, seen = run_mention(server.port, poll_seconds="0.3")

    assert _wait_until(lambda: seen.is_file(), timeout=8.0), (
        f"seed never baselined $SEEN after two stalled connections — a "
        f"stalled response is hanging the seed loop instead of being "
        f"bounded by --max-time (full output: {proc.lines!r})"
    )


def test_mention_poll_announces_unreachable_on_a_stalled_response(
    serve_stalling, run_mention, monkeypatch
):
    """#3106's core claim: a stalled response must reach the SAME
    'unreachable' announce (#3065) a curl --fail failure already produces.
    Script: seed + one healthy poll, then stall — pre-fix, the stalled
    connection hangs the curl call forever and the announce is never
    reached; post-fix, --max-time bounds it, curl returns non-zero, and the
    existing announce path fires exactly as it does for a 500 response.
    """
    server = serve_stalling([(200, _MENTIONS_QUIET), (200, _MENTIONS_QUIET), "stall"])
    monkeypatch.setenv("FORUM_MENTION_MAX_TIME_SECONDS", "1")
    proc, seen = run_mention(server.port, poll_seconds="0.3")

    assert _wait_until(lambda: seen.is_file()), "seed never baselined $SEEN"
    assert _wait_until(lambda: len(_unreachable_lines(proc.lines)) >= 1, timeout=8.0), (
        f"a stalled response never produced the 'unreachable' announce — "
        f"curl is hanging past the poll cycle instead of being bounded by "
        f"--max-time (full output so far: {proc.lines!r})"
    )
if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
