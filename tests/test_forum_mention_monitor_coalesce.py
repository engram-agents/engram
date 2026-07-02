"""Integration tests for the cross-cycle coalesce window in
forum-mention-monitor.sh (#1616 / UCS rc2 F10).

Runs the REAL shell script as a subprocess against a local mock HTTP server
that serves sequential /mentions payloads, and asserts on the script's
stdout ("📣 ..." summary lines). Two testability-only env overrides make
this feasible in a fast unit-test cycle without touching the script's
default (production) behaviour:

  - FORUM_MENTION_POLL_SECONDS overrides the 30s poll cadence.
  - FORUM_MENTION_COALESCE_SECONDS overrides the 120s coalesce window
    (already a documented, spec'd override — not test-only).

Verifies (per issue #1616's "Verify" section):
  (a) 5 mentions across 5 consecutive polls -> exactly 1 summary emit.
  (b) a single mention still emits within one window.
  (c) forum-down mid-window preserves the buffer + $SEEN; recovery does not
      flood (no duplicate / doubled emit for the mention seen before the
      outage).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-mention-monitor.sh"

_FLUSH_RE = re.compile(r"^\U0001f4e3 (\d+) new @-mentions? across (\d+) threads?: (.+)$")


class _MockMentionsServer:
    """Minimal single-threaded HTTP server serving a test-controlled
    /mentions payload. `payload_fn(request_count) -> (status, body_dict)`
    is called once per GET, `request_count` starting at 1 (the monitor's
    seed poll is request #1)."""

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
                status, body = outer._payload_fn(n)
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def _mentions_payload(entries: list[tuple[int, int, str]]) -> dict:
    """entries: list of (post_id, thread_id, thread_title)."""
    return {
        "mentions": [
            {"post_id": pid, "thread_id": tid, "thread_title": title}
            for pid, tid, title in entries
        ]
    }


def _run_monitor(tmp_path: Path, port: int, poll_seconds: str, coalesce_seconds: str,
                  run_seconds: float) -> list[str]:
    """Arm the real script against the mock server for `run_seconds`, then
    SIGTERM it and return captured stdout lines."""
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    config = {"agent_name": "testagent", "forum": {"url": f"http://127.0.0.1:{port}"}}
    (home / ".engram" / "config.json").write_text(json.dumps(config))

    seen_file = tmp_path / "seen"

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["FORUM_MENTION_NO_REAP"] = "1"
    env["FORUM_MENTION_SEEN_FILE"] = str(seen_file)
    env["FORUM_MENTION_POLL_SECONDS"] = poll_seconds
    env["FORUM_MENTION_COALESCE_SECONDS"] = coalesce_seconds

    proc = subprocess.Popen(
        ["bash", str(_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    lines: list[str] = []

    def _reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    time.sleep(run_seconds)

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    reader_thread.join(timeout=2)

    return lines, seen_file


# ---------------------------------------------------------------------------
# (a) 5 mentions across 5 consecutive polls -> exactly 1 summary emit
# ---------------------------------------------------------------------------

def test_five_mentions_across_five_polls_coalesce_to_one_emit(tmp_path):
    entries_all = [
        (1, 200, "hot thread"),
        (2, 200, "hot thread"),
        (3, 200, "hot thread"),
        (4, 199, "other thread"),
        (5, 199, "other thread"),
    ]

    def payload_fn(n: int):
        # request #1 = seed (0 mentions). requests #2-6 add one mention each.
        count = max(0, min(n - 1, len(entries_all)))
        return 200, _mentions_payload(entries_all[:count])

    server = _MockMentionsServer(payload_fn)
    server.start()
    try:
        lines, seen_file = _run_monitor(
            tmp_path, server.port,
            poll_seconds="0.4", coalesce_seconds="2",
            run_seconds=6.0,
        )
    finally:
        server.stop()

    flush_lines = [l for l in lines if _FLUSH_RE.match(l)]
    assert len(flush_lines) == 1, f"expected exactly 1 summary emit, got {flush_lines!r}\nall lines: {lines!r}"
    m = _FLUSH_RE.match(flush_lines[0])
    assert m is not None
    n_mentions, n_threads = int(m.group(1)), int(m.group(2))
    assert n_mentions == 5, f"expected 5 mentions coalesced, got {n_mentions} ({flush_lines[0]!r})"
    assert n_threads == 2, f"expected 2 distinct threads, got {n_threads} ({flush_lines[0]!r})"
    assert "#200(×3)" in flush_lines[0]
    assert "#199(×2)" in flush_lines[0]

    # $SEEN advanced to include all 5 post_ids even though only one summary
    # line was ever emitted for them.
    seen_content = seen_file.read_text()
    for pid in (1, 2, 3, 4, 5):
        assert f"{pid}\t" in seen_content, f"post {pid} missing from $SEEN: {seen_content!r}"


# ---------------------------------------------------------------------------
# (b) a single mention still emits within one window
# ---------------------------------------------------------------------------

def test_single_mention_emits_within_one_window(tmp_path):
    entries_all = [(42, 55, "solo thread")]

    def payload_fn(n: int):
        count = 0 if n == 1 else 1  # appears starting at poll #2, then stays
        return 200, _mentions_payload(entries_all[:count])

    server = _MockMentionsServer(payload_fn)
    server.start()
    try:
        lines, _seen_file = _run_monitor(
            tmp_path, server.port,
            poll_seconds="0.3", coalesce_seconds="1",
            run_seconds=3.0,
        )
    finally:
        server.stop()

    flush_lines = [l for l in lines if _FLUSH_RE.match(l)]
    assert len(flush_lines) == 1, f"expected exactly 1 summary emit, got {flush_lines!r}\nall lines: {lines!r}"
    m = _FLUSH_RE.match(flush_lines[0])
    assert m is not None
    assert int(m.group(1)) == 1
    assert int(m.group(2)) == 1
    assert "#55(×1)" in flush_lines[0]
    # singular grammar
    assert "1 new @-mention across 1 thread" in flush_lines[0]


# ---------------------------------------------------------------------------
# (c) forum-down mid-window preserves the buffer + $SEEN; recovery doesn't flood
# ---------------------------------------------------------------------------

def test_forum_down_mid_window_preserves_buffer_and_seen(tmp_path):
    entries_all = [(7, 300, "resilient thread")]

    # request #1: seed (0 mentions, success).
    # request #2: 1 mention (post 7), success -> opens window, buffers it.
    # requests #3-7: forum down (HTTP 500) -> curl --fail -> skip cycle,
    #                buffer + $SEEN must survive untouched.
    # requests #8+: forum back up, SAME single mention (already in $SEEN) ->
    #               must NOT be re-buffered / re-counted -> eventual flush
    #               still reports exactly 1 mention, not 2.
    def payload_fn(n: int):
        if n == 1:
            return 200, _mentions_payload([])
        if n == 2:
            return 200, _mentions_payload(entries_all)
        if 3 <= n <= 7:
            return 500, {"error": "forum down"}
        return 200, _mentions_payload(entries_all)

    server = _MockMentionsServer(payload_fn)
    server.start()
    try:
        lines, seen_file = _run_monitor(
            tmp_path, server.port,
            poll_seconds="0.3", coalesce_seconds="3",
            run_seconds=6.0,
        )
    finally:
        server.stop()

    flush_lines = [l for l in lines if _FLUSH_RE.match(l)]
    assert len(flush_lines) == 1, (
        f"expected exactly 1 summary emit (no flood on recovery), got {flush_lines!r}\n"
        f"all lines: {lines!r}"
    )
    m = _FLUSH_RE.match(flush_lines[0])
    assert m is not None
    assert int(m.group(1)) == 1, f"buffer should hold exactly 1 mention (no duplicate on recovery): {flush_lines[0]!r}"
    assert "#300(×1)" in flush_lines[0]

    seen_content = seen_file.read_text()
    assert "7\t" in seen_content


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
