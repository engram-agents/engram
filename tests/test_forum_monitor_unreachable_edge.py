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
"""

from __future__ import annotations

import json
import os
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
    procs = []

    def _run(port: int, poll_seconds: str = _POLL):
        home = _make_home(tmp_path, port)
        cursor = tmp_path / "cursor"
        env = os.environ.copy()
        env["HOME"] = str(home)
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

    def _run(port: int, poll_seconds: str = _POLL, coalesce_seconds: str = "1"):
        home = _make_home(tmp_path, port)
        seen = tmp_path / "seen"
        env = os.environ.copy()
        env["HOME"] = str(home)
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


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
