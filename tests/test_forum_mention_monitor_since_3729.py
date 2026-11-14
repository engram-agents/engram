"""Cursor + ``since`` for forum-mention-monitor.sh (#3729).

Before: every 30s poll asked for the agent's WHOLE @-mention history (no
``since``), ~758 KB per poll on the real forum, growing forever. Now the monitor
keeps a server-derived cursor next to its seen file and polls with
``since = cursor - overlap``.

Runs the REAL script against a mock server that records every request path and
answers like the real endpoint: validates ``since`` with fromisoformat (400 on a
bad one), then filters ``created_at > since`` as a plain TEXT comparison, which
is what ``db.get_mentions`` does in SQL on the stored
``%Y-%m-%dT%H:%M:%S.%fZ`` text. A ``since`` in any other shape therefore
mis-compares here exactly as it would there.

Contract cells: 1 cursor from server, 2 since with overlap (stored format,
URL-encoded), 3 no cursor -> full poll, 4 exactly-once with the overlap,
5 malformed / failed poll writes neither file, 6 empty list keeps the cursor,
7 a cursor lost mid-run does not replay history, 8 the seed re-baselines the
cursor (restart after a DB restore / clock step back), 9 a 500 whose body is a
valid, newer mentions list leaves the cursor byte-identical, 10 restart with an
existing cursor and a stale seen file re-announces nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "src" / "engram" / "tools" / "forum-mention-monitor.sh"

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
T0 = datetime(2026, 11, 14, 12, 0, 0, 123456, tzinfo=timezone.utc)


def ts(dt: datetime) -> str:
    return dt.strftime(_FMT)


def post(pid: int, created: datetime, tid: int = 100) -> dict:
    return {"post_id": pid, "thread_id": tid, "thread_title": f"t{tid}",
            "author": "someone", "kind": "at_mention", "created_at": ts(created)}


class Server:
    """Records request paths; ``posts`` is mutable; ``script(n)`` may return
    (status, raw_bytes) to override request n, or None for the normal answer;
    ``on_request(n, server)`` runs at the START of request n."""

    def __init__(self, posts, script=None, on_request=None):
        self.posts = list(posts)
        self.paths: list[str] = []
        self.script = script
        self.on_request = on_request
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                n = len(outer.paths)
                if outer.on_request:
                    outer.on_request(n, outer)
                over = outer.script(n) if outer.script else None
                if over is not None:
                    status, raw = over
                else:
                    status, raw = outer._answer(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def _answer(self, path):
        q = parse_qs(urlsplit(path).query)  # decodes '+' to ' ', like flask
        since = (q.get("since") or [""])[0].strip() or None
        if since is not None:
            try:
                datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                return 400, b'{"error":"invalid since"}'
        rows = [p for p in self.posts if since is None or p["created_at"] > since]
        rows.sort(key=lambda p: p["created_at"])
        return 200, json.dumps({"mentions": rows}).encode()

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


def run(tmp_path, srv, *, until, extra_env=None, pre_seen=None, pre_cursor=None,
        timeout=20.0):
    """Arm the real script; stop once until(srv) is true (+ a short grace).
    Returns (stdout_lines, seen_path)."""
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    (home / ".engram" / "config.json").write_text(json.dumps(
        {"agent_name": "testagent", "forum": {"url": f"http://127.0.0.1:{srv.port}"}}))
    seen = tmp_path / "seen"
    if pre_seen is not None:
        seen.write_text(pre_seen)
    if pre_cursor is not None:
        Path(str(seen) + ".cursor").write_text(pre_cursor)
    env = os.environ.copy()
    env.update({
        "HOME": str(home), "ENGRAM_HOME": str(home / ".engram"),
        "FORUM_MENTION_NO_REAP": "1", "FORUM_MENTION_SEEN_FILE": str(seen),
        "FORUM_MENTION_POLL_SECONDS": "0.3", "FORUM_MENTION_COALESCE_SECONDS": "0",
    })
    env.update(extra_env or {})
    proc = subprocess.Popen(["bash", str(_SCRIPT)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    lines: list[str] = []

    def rd():
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))

    t = threading.Thread(target=rd, daemon=True)
    t.start()
    end = time.time() + timeout
    while time.time() < end and not until(srv):
        time.sleep(0.05)
    time.sleep(1.0)  # let the cycle that answered the last request finish
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    t.join(timeout=2)
    return lines, seen


def cursor_of(seen: Path):
    c = Path(str(seen) + ".cursor")
    return c.read_text().strip() if c.exists() else None


def since_of(path: str):
    return (parse_qs(urlsplit(path).query).get("since") or [None])[0]


def announced(lines):
    return [l for l in lines if "new @-mention" in l]


# 1 -----------------------------------------------------------------------

def test_cursor_is_max_created_at_from_the_response_after_seed(tmp_path):
    srv = Server([post(1, T0 - timedelta(hours=5)), post(2, T0), post(3, T0 - timedelta(hours=1))])
    try:
        _, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 1)
    finally:
        srv.stop()
    # the mock's timestamps are in the past of any real run; a cursor taken from the
    # local clock could not equal T0
    assert cursor_of(seen) == ts(T0)


def test_cursor_is_the_max_not_the_last_and_never_moves_backwards(tmp_path):
    newer, older = T0 + timedelta(seconds=10), T0 - timedelta(seconds=100)
    srv = Server([post(1, T0)])
    srv.script = {
        # newest FIRST (out of order): the max must win, not the last item
        2: (200, json.dumps({"mentions": [post(2, newer), post(3, older)]}).encode()),
        # a response holding only OLDER mentions (stale replica, skew): no step back
        3: (200, json.dumps({"mentions": [post(3, older)]}).encode()),
    }.get
    try:
        _, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 4)
    finally:
        srv.stop()
    assert cursor_of(seen) == ts(newer)


# 2 -----------------------------------------------------------------------

def test_poll_adds_since_cursor_minus_default_overlap_in_stored_format_urlencoded(tmp_path):
    srv = Server([post(1, T0)])
    try:
        run(tmp_path, srv, until=lambda s: len(s.paths) >= 3)
    finally:
        srv.stop()
    expected = ts(T0 - timedelta(seconds=600))
    later = srv.paths[1:]
    assert later, srv.paths
    for p in later:
        assert "kind=at_mention" in p
        assert since_of(p) == expected, p
        # a literal '+' in a query string decodes to a space; the value must be encoded
        assert "since=" + quote(expected, safe="") in p, p
        assert "+" not in p.split("since=", 1)[1], p


def test_overlap_is_overridable_for_tests(tmp_path):
    srv = Server([post(1, T0)])
    try:
        run(tmp_path, srv, until=lambda s: len(s.paths) >= 2,
            extra_env={"FORUM_MENTION_SINCE_OVERLAP_SECONDS": "5"})
    finally:
        srv.stop()
    assert since_of(srv.paths[1]) == ts(T0 - timedelta(seconds=5))


# 3 -----------------------------------------------------------------------

def test_no_cursor_first_poll_is_a_full_poll_then_cursor_is_written(tmp_path):
    srv = Server([post(1, T0)])
    try:
        _, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 1)
    finally:
        srv.stop()
    assert "since" not in srv.paths[0]
    assert cursor_of(seen) == ts(T0)


def test_upgrade_seen_without_cursor_does_a_full_poll_and_seeds_cursor(tmp_path):
    srv = Server([post(1, T0)])
    try:
        _, seen = run(tmp_path, srv, pre_seen="1\tt100\n", until=lambda s: len(s.paths) >= 2)
    finally:
        srv.stop()
    assert "since" not in srv.paths[0]
    assert cursor_of(seen) == ts(T0)
    assert since_of(srv.paths[1]) == ts(T0 - timedelta(seconds=600))


# 4 -----------------------------------------------------------------------

def test_seen_mention_reappearing_inside_overlap_is_not_reannounced(tmp_path):
    srv = Server([post(1, T0)])
    try:
        lines, _ = run(tmp_path, srv, until=lambda s: len(s.paths) >= 4)
    finally:
        srv.stop()
    # polls 2..4 sent a since, and the server answered post 1 again (inside the overlap)
    assert all(since_of(p) for p in srv.paths[1:]), srv.paths
    assert announced(lines) == [], lines


def test_older_than_newest_but_inside_overlap_is_announced_exactly_once(tmp_path):
    srv = Server([post(1, T0)])

    def hook(n, s):
        if n == 3:  # late commit / skew: older than the newest seen, inside the window
            s.posts.append(post(2, T0 - timedelta(seconds=100), tid=200))

    srv.on_request = hook
    try:
        lines, _ = run(tmp_path, srv, until=lambda s: len(s.paths) >= 7)
    finally:
        srv.stop()
    got = announced(lines)
    assert len(got) == 1, lines
    assert "1 new @-mention across 1 thread: #200" in got[0], got


# 5 -----------------------------------------------------------------------

def test_malformed_or_failed_poll_writes_neither_seen_nor_cursor(tmp_path):
    later = T0 + timedelta(hours=1)
    srv = Server([post(1, T0)])
    srv.script = {
        2: (200, b"{}"),
        3: (200, b'{"error":"maintenance"}'),
        4: (200, b'{"mentions": "nope"}'),
        5: (200, b"<html>maintenance</html>"),
        6: (500, b'{"mentions":[]}'),
    }.get
    seen_path = tmp_path / "seen"
    snaps: dict = {}

    def snap():
        cur = Path(str(seen_path) + ".cursor")
        return (seen_path.read_bytes() if seen_path.exists() else None,
                cur.read_bytes() if cur.exists() else None)

    def hook(n, s):
        snaps[n] = snap()  # files as left by every cycle before request n
        if n == 7:
            s.posts.append(post(9, later))

    srv.on_request = hook
    try:
        lines, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 8)
    finally:
        srv.stop()
    base = snaps[2]  # after the good seed
    assert base[1] is not None and base[1].decode().strip() == ts(T0)
    for n in (3, 4, 5, 6, 7):  # each follows one bad answer (requests 2..6)
        assert snaps[n] == base, n
    # recovery: the next good poll still works and announces the new mention
    assert cursor_of(seen) == ts(later)
    assert len(announced(lines)) == 1, lines


# 6 -----------------------------------------------------------------------

def test_empty_list_under_since_keeps_cursor_and_snapshots_seen_empty(tmp_path):
    srv = Server([post(1, T0)])
    try:
        lines, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 3,
                          extra_env={"FORUM_MENTION_SINCE_OVERLAP_SECONDS": "0"})
        end_cursor = cursor_of(seen)
        end_seen = seen.read_text()
    finally:
        srv.stop()
    # overlap 0: since == cursor, strict '>' excludes post 1 -> an EMPTY list is the answer
    assert since_of(srv.paths[1]) == ts(T0)
    assert end_cursor == ts(T0)  # not moved back, not cleared
    assert end_seen == ""        # snapshot semantics unchanged: empty answer -> empty seen
    assert announced(lines) == []


def test_after_an_empty_answer_the_next_new_mention_is_still_announced(tmp_path):
    srv = Server([post(1, T0)])

    def hook(n, s):
        if n == 4:
            s.posts.append(post(2, T0 + timedelta(seconds=30), tid=300))

    srv.on_request = hook
    try:
        lines, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 6,
                          extra_env={"FORUM_MENTION_SINCE_OVERLAP_SECONDS": "0"})
    finally:
        srv.stop()
    got = announced(lines)
    assert len(got) == 1 and "#300" in got[0], lines
    assert cursor_of(seen) == ts(T0 + timedelta(seconds=30))


# 7 -----------------------------------------------------------------------

def test_cursor_lost_mid_run_does_not_replay_history(tmp_path):
    srv = Server([post(1, T0 - timedelta(hours=5)), post(2, T0 - timedelta(hours=4)),
                  post(3, T0)])
    seen_path = tmp_path / "seen"
    # Answer 3 carries post 3 WITHOUT a created_at, so cycle 3 cannot re-write the
    # cursor that the hook removes while request 3 is in flight: poll 4 starts
    # with no cursor, as after an outside cleanup or truncation.
    nodate = post(3, T0)
    del nodate["created_at"]
    srv.script = {3: (200, json.dumps({"mentions": [nodate]}).encode())}.get

    def hook(n, s):
        if n == 3:
            Path(str(seen_path) + ".cursor").unlink()
        if n == 4:  # poll 4 is a FULL poll: history plus one genuinely new mention
            s.posts.append(post(4, T0 + timedelta(seconds=30), tid=400))

    srv.on_request = hook
    try:
        lines, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 6)
    finally:
        srv.stop()
    assert since_of(srv.paths[3]) is None, srv.paths  # the lost-cursor poll was full
    got = announced(lines)
    assert len(got) == 1 and "1 new @-mention across 1 thread: #400" in got[0], lines
    assert cursor_of(seen) == ts(T0 + timedelta(seconds=30))


# 8 -----------------------------------------------------------------------

def test_seed_rebaselines_a_cursor_that_is_ahead_of_the_server(tmp_path):
    # a DB restore / server clock step back: the stored cursor is far in the future
    future = ts(T0 + timedelta(days=30))
    srv = Server([post(1, T0)])

    def hook(n, s):
        if n == 3:
            s.posts.append(post(2, T0 + timedelta(seconds=30), tid=500))

    srv.on_request = hook
    try:
        lines, seen = run(tmp_path, srv, pre_cursor=future + "\n",
                          until=lambda s: len(s.paths) >= 6)
    finally:
        srv.stop()
    assert since_of(srv.paths[0]) is None  # the seed is a full poll
    assert since_of(srv.paths[1]) == ts(T0 - timedelta(seconds=600)), srv.paths
    got = announced(lines)
    assert len(got) == 1 and "#500" in got[0], lines
    assert cursor_of(seen) == ts(T0 + timedelta(seconds=30))


def test_seed_sets_cursor_from_its_own_response_alone(tmp_path):
    future = ts(T0 + timedelta(days=30))
    srv = Server([post(1, T0)])
    try:
        _, seen = run(tmp_path, srv, pre_cursor=future + "\n",
                      until=lambda s: len(s.paths) >= 1)
    finally:
        srv.stop()
    assert cursor_of(seen) == ts(T0)


# 9 -----------------------------------------------------------------------

def test_a_500_carrying_a_valid_newer_mentions_list_leaves_the_cursor_untouched(tmp_path):
    newer = T0 + timedelta(hours=2)
    srv = Server([post(1, T0)])
    srv.script = {
        2: (500, json.dumps({"mentions": [post(7, newer)]}).encode()),
    }.get
    seen_path = tmp_path / "seen"
    snaps: dict = {}

    def hook(n, s):
        cur = Path(str(seen_path) + ".cursor")
        snaps[n] = (seen_path.read_bytes() if seen_path.exists() else None,
                    cur.read_bytes() if cur.exists() else None)

    srv.on_request = hook
    try:
        lines, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 4)
    finally:
        srv.stop()
    assert snaps[2][1] == ts(T0).encode() + b"\n"
    assert snaps[3] == snaps[2]  # after the 500 cycle: seen and cursor byte-identical
    assert cursor_of(seen) == ts(T0)
    assert announced(lines) == [], lines


# 10 ----------------------------------------------------------------------

def test_restart_with_existing_cursor_and_stale_seen_reannounces_nothing(tmp_path):
    srv = Server([post(1, T0 - timedelta(hours=5)), post(2, T0)])

    def hook(n, s):
        if n == 3:
            s.posts.append(post(3, T0 + timedelta(seconds=30), tid=600))

    srv.on_request = hook
    try:
        lines, seen = run(tmp_path, srv, pre_seen="2\tt100\n", pre_cursor=ts(T0) + "\n",
                          until=lambda s: len(s.paths) >= 6)
    finally:
        srv.stop()
    got = announced(lines)
    assert len(got) == 1 and "#600" in got[0], lines
    assert cursor_of(seen) == ts(T0 + timedelta(seconds=30))


# 10 ----------------------------------------------------------------------

def test_empty_seen_and_no_cursor_first_ever_mention_is_announced_once(tmp_path):
    # An agent with no mentions at all: $SEEN is empty and there is no cursor. The
    # post_id high-water filter must NOT apply (nothing to measure against), so the
    # first-ever mention is announced exactly once.
    srv = Server([])

    def hook(n, s):
        if n == 3:
            s.posts.append(post(1, T0, tid=700))

    srv.on_request = hook
    try:
        lines, seen = run(tmp_path, srv, until=lambda s: len(s.paths) >= 6)
    finally:
        srv.stop()
    got = announced(lines)
    assert len(got) == 1 and "#700" in got[0], lines
    assert cursor_of(seen) == ts(T0)


def test_empty_seed_removes_a_stale_cursor_and_a_later_mention_is_announced(tmp_path):
    # Stale cursor from before a forum DB restore (far in the future) + the restored
    # forum has zero mentions: the seed must delete the cursor, or the new mention
    # (created_at far below the cursor) would be filtered out by `since` forever.
    future = ts(T0 + timedelta(days=30))
    srv = Server([])
    cursor_after_seed = []

    def hook(n, s):
        if n == 2:  # the first steady poll: the seed has finished by now
            cursor_after_seed.append(cursor_of(tmp_path / "seen"))
        if n == 3:
            s.posts.append(post(1, T0, tid=800))

    srv.on_request = hook
    try:
        lines, seen = run(tmp_path, srv, pre_cursor=future + "\n",
                          until=lambda s: len(s.paths) >= 6)
    finally:
        srv.stop()
    assert cursor_after_seed == [None], cursor_after_seed
    assert since_of(srv.paths[1]) is None, srv.paths  # polls are full, not windowed
    got = announced(lines)
    assert len(got) == 1 and "#800" in got[0], lines
    assert cursor_of(seen) == ts(T0)


# 11 ----------------------------------------------------------------------

def test_with_a_cursor_a_late_commit_with_a_lower_post_id_is_announced_once(tmp_path):
    # With a cursor the plain not-in-$SEEN rule applies; the post_id high-water
    # filter belongs to the no-cursor poll only. Post 2 is LOWER than the newest
    # seen id (5) and its created_at is inside the 600 s overlap.
    srv = Server([post(5, T0)])

    def hook(n, s):
        if n == 3:
            s.posts.append(post(2, T0 - timedelta(seconds=100), tid=200))

    srv.on_request = hook
    try:
        lines, _ = run(tmp_path, srv, until=lambda s: len(s.paths) >= 7)
    finally:
        srv.stop()
    assert all(since_of(p) for p in srv.paths[1:]), srv.paths  # the cursor path throughout
    got = announced(lines)
    assert len(got) == 1, lines
    assert "1 new @-mention across 1 thread: #200" in got[0], got


def test_no_cursor_mid_run_a_non_integer_post_id_is_announced_once(tmp_path):
    # Cursor lost mid-run with integer ids in $SEEN: the high-water filter applies,
    # but a non-integer id is never filtered, so it is announced (once).
    srv = Server([post(5, T0 - timedelta(hours=1)), post(6, T0)])
    seen_path = tmp_path / "seen"
    nodate = post(6, T0)
    del nodate["created_at"]  # cycle 3 cannot re-write the cursor the hook removes
    srv.script = {3: (200, json.dumps({"mentions": [nodate]}).encode())}.get

    def hook(n, s):
        if n == 3:
            Path(str(seen_path) + ".cursor").unlink()
        if n == 4:  # the full poll: history (5, 6) plus one new mention with a string id
            s.posts.append(post("abc", T0 + timedelta(seconds=30), tid=900))

    srv.on_request = hook
    try:
        lines, _ = run(tmp_path, srv, until=lambda s: len(s.paths) >= 6)
    finally:
        srv.stop()
    assert since_of(srv.paths[3]) is None, srv.paths  # the lost-cursor poll was full
    got = announced(lines)
    assert len(got) == 1 and "1 new @-mention across 1 thread: #900" in got[0], lines
