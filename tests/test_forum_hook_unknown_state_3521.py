"""Contract-first tests for the forum prompt hook's `unknown` state
(part of #3521, row 5).

The contract (Kepler's ruling, 2026-10-06): "never block" means the hook must
not block the prompt; it never meant the hook may report "nothing new" when it
could not look. An advisory result is tri-state -- healthy / not-ok / unknown --
and `unknown` renders ONE bounded line.

Row 5 of the inventory, in this hook (engram-forum-prompt-hook.py):

  * forum unreachable and no cache  -> was silent exit 0; now ONE line (K2)
  * forum unreachable, stale cache  -> was a silent stale serve; now the stale
    output PLUS a line saying the counts are cached (K3)
  * threads ok, inbox failed        -> was cached as `"mentions": []` for 30 s;
    now cached as `"mentions": null` (unknown) and retried once (K4) + a line (K5)
  * inbox HTTP 404 (old server)     -> `unsupported`: known-empty, NO line, cached
  * umwelt mode                     -> the same unknown handling (K6)
  * rate limit                      -> `forum-threads` / `forum-inbox` keys, 1800 s (K7)

Cell classes (the dev-code red/green record is part of the PR handoff):
  * healthy / control cells -- GREEN on unmodified origin/dev;
  * unknown cells           -- RED on unmodified origin/dev (it says nothing);
  * shape cells             -- RED on dev by construction (the `(value, reason)`
    return shape of `_fetch_threads` / `_fetch_inbox` does not exist there).

Subprocess cells drive the REAL hook against a LOCAL http.server; in-process
cells load the hook as a module (same pattern as tests/test_forum_hook.py).
"""

import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
HOOK_PATH = HOOK_DIR / "engram-forum-prompt-hook.py"
STAMP_NAME = "probe-unknown-notice.json"
CACHE_NAME = "forum-hook-cache.json"
CURSOR_NAME = "forum-surfaced-cursor.txt"
AGENT = "testbot"

THREADS_LINE_2 = (
    "\U0001f4e2 2 new forum posts since last read"
    " — run `forum read <id>` or `forum status` to catch up."
)

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


# ---------------------------------------------------------------------------
# A local forum: records requests, serves configurable (status, body) per path
# ---------------------------------------------------------------------------

class _Forum:
    def __init__(self):
        self.threads = (200, json.dumps({"threads": []}))
        self.inbox = (200, json.dumps({"inbox": [], "unread_all": 0}))
        self.hits = {"threads": 0, "inbox": 0}
        self.lock = threading.Lock()
        forum = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/api/threads"):
                    with forum.lock:
                        forum.hits["threads"] += 1
                    return self._send(*forum.threads)
                if "/inbox" in self.path:
                    with forum.lock:
                        forum.hits["inbox"] += 1
                    return self._send(*forum.inbox)
                return self._send(404, "{}")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                return self._send(200, "{}")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self._t.start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def forum():
    f = _Forum()
    yield f
    f.stop()


def _dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def _mention_inbox(tid="99", title="Review my PR", author="luria"):
    return (200, json.dumps({"inbox": [{
        "thread_id": tid, "thread_title": title, "author": author,
        "kind": "at_mention", "post_id": 1, "created_at": "2026-01-01T12:00:00Z",
    }], "unread_all": 1}))


def _threads_body(n):
    return (200, json.dumps({"threads": [{"id": str(i), "title": f"t{i}"} for i in range(n)]}))


# ---------------------------------------------------------------------------
# Subprocess driver
# ---------------------------------------------------------------------------

def _home(tmp_path: Path, url: str, *, umwelt: bool = False) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir(exist_ok=True)
    cfg = {"agent_name": AGENT, "forum": {"url": url}}
    if umwelt:
        cfg["comms_surface"] = "umwelt"
    (home / "config.json").write_text(json.dumps(cfg))
    return home


def _run(home: Path, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ("FORUM_URL", "CLAUDE_PLUGIN_ROOT")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{PROJECT_ROOT / 'src' / 'engram'}:{PROJECT_ROOT / 'src'}"
    env.update(extra_env or {})
    p = subprocess.run(
        [sys.executable, str(HOOK_PATH)], input="{}", capture_output=True, text=True,
        env=env, timeout=60,
    )
    return p.returncode, p.stdout, p.stderr


def _ctx(stdout: str) -> str:
    return json.loads(stdout.strip())["hookSpecificOutput"]["additionalContext"]


def _warn(ctx: str) -> str:
    """The ONE injected unknown line (the shared renderer may add its own
    `shown/cut` header line, which is not ours). Asserts there is exactly one."""
    lines = [ln for ln in ctx.splitlines() if ln.startswith("\u26a0")]
    assert len(lines) == 1, ctx
    return lines[0]


def _stale_cache(home: Path, threads, mentions="ABSENT", age=3600):
    payload = {"fetched_at": time.time() - age, "threads": threads}
    if mentions != "ABSENT":
        payload["mentions"] = mentions
    (home / CACHE_NAME).write_text(json.dumps(payload))


def _read_cache(home: Path) -> dict:
    return json.loads((home / CACHE_NAME).read_text())


def _write_stamp(home: Path, key: str, ts: float):
    (home / STAMP_NAME).write_text(json.dumps({"shown_at": {key: ts}}))


# ===========================================================================
# (i) forum unreachable, no cache -> K2 line, exit 0 (+ control)
# ===========================================================================

def test_i_unreachable_no_cache_emits_one_bounded_line(tmp_path):
    home = _home(tmp_path, _dead_url())
    rc, out, err = _run(home)
    assert rc == 0
    assert out.strip(), "an unreachable forum with no cache must not be silent"
    line = _warn(_ctx(out))
    assert "forum unreachable" in line and "(connect)" in line, line
    assert "cannot be read" in line
    assert len(line) <= 200
    assert len(out.strip().splitlines()) == 1
    assert len(err.strip().splitlines()) == 1, err
    assert err.startswith("engram-forum-prompt-hook: forum-threads unknown: connect:"), err


def test_i_control_healthy_empty_forum_prints_nothing(tmp_path, forum):
    home = _home(tmp_path, forum.url)
    rc, out, err = _run(home)
    assert (rc, out, err) == (0, "", "")


def test_i_unreachable_leaves_cursor_untouched(tmp_path):
    home = _home(tmp_path, _dead_url())
    (home / CURSOR_NAME).write_text("2026-01-01T12:00:00.000000Z\n")
    before = (home / CURSOR_NAME).read_bytes()
    _run(home)
    assert (home / CURSOR_NAME).read_bytes() == before


# ===========================================================================
# (ii) threads 200 + inbox 500 -> unknown cached, one retry, no cursor move
# ===========================================================================

def test_ii_inbox_failure_is_not_cached_as_empty_mentions(tmp_path, forum):
    forum.inbox = (500, "boom")
    home = _home(tmp_path, forum.url)
    rc, out, err = _run(home)
    assert rc == 0
    assert _read_cache(home)["mentions"] is None, "a failed inbox fetch must be marked unknown"
    line = _warn(_ctx(out))
    assert "forum inbox unreadable (http-500)" in line and "@mentions may be waiting" in line
    assert len(line) <= 200


def test_ii_next_run_retries_inbox_once_and_shows_the_mention(tmp_path, forum):
    forum.inbox = (500, "boom")
    forum.threads = _threads_body(0)
    home = _home(tmp_path, forum.url)
    _run(home)
    cursor_before = (home / CURSOR_NAME).read_bytes()
    assert (forum.hits["threads"], forum.hits["inbox"]) == (1, 1)
    forum.inbox = _mention_inbox()
    rc, out, _err = _run(home)
    assert rc == 0
    assert "\U0001f514 1 forum mention waiting on you" in _ctx(out)
    assert (forum.hits["threads"], forum.hits["inbox"]) == (1, 2), "retry must fetch the inbox only"
    assert (home / CURSOR_NAME).read_bytes() == cursor_before, "the retry must not move the cursor"


def test_ii_retry_success_is_written_back_without_extending_the_ttl(tmp_path, forum):
    forum.inbox = (500, "boom")
    home = _home(tmp_path, forum.url)
    _run(home)
    fetched_at = _read_cache(home)["fetched_at"]
    forum.inbox = _mention_inbox()
    _run(home)
    cache = _read_cache(home)
    assert isinstance(cache["mentions"], list) and len(cache["mentions"]) == 1
    assert cache["fetched_at"] == fetched_at
    _run(home)
    assert forum.hits["inbox"] == 2, "once resolved, the cache serves the mentions again"


def test_ii_control_inbox_ok_from_the_start_is_not_refetched(tmp_path, forum):
    forum.inbox = _mention_inbox()
    home = _home(tmp_path, forum.url)
    rc1, out1, _ = _run(home)
    rc2, out2, err2 = _run(home)
    assert (rc1, rc2) == (0, 0)
    assert forum.hits["inbox"] == 1 and forum.hits["threads"] == 1
    assert "\U0001f514" in _ctx(out2) and err2 == ""
    assert _read_cache(home)["mentions"] != None  # noqa: E711 -- a list, never the marker


def test_ii_retry_that_also_fails_renders_the_inbox_line(tmp_path, forum):
    forum.inbox = (500, "boom")
    home = _home(tmp_path, forum.url)
    _run(home)
    (home / STAMP_NAME).unlink()  # the first run's window must not hide the second line
    rc, out, err = _run(home)
    assert rc == 0 and forum.hits["inbox"] == 2
    assert "forum inbox unreadable (http-500)" in _ctx(out)
    assert err.startswith("engram-forum-prompt-hook: forum-inbox unknown: http-500:"), err


# ===========================================================================
# (iii) threads 200 + inbox 404 -> unsupported: no line, cached known-empty
# ===========================================================================

def test_iii_inbox_404_is_known_empty_no_line_cached_empty(tmp_path, forum):
    forum.inbox = (404, "{}")
    forum.threads = _threads_body(2)
    home = _home(tmp_path, forum.url)
    rc, out, err = _run(home)
    assert (rc, err) == (0, "")
    assert json.loads(out.strip()) == {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\u25b8 1 shown \u00b7 0 cut\n" + THREADS_LINE_2}}
    assert _read_cache(home)["mentions"] == []
    _run(home)
    assert forum.hits["inbox"] == 1, "a known-empty cache must not be refetched"


@pytest.mark.parametrize("status", [401, 403, 410])
def test_iii_other_4xx_inbox_status_is_unreadable_not_unsupported(tmp_path, forum, status):
    # Only 404 means "old server, no inbox". Any other 4xx is an inbox we
    # could not read: it must render the unknown line and cache unknown.
    forum.inbox = (status, "nope")
    home = _home(tmp_path, forum.url)
    rc, out, err = _run(home)
    assert rc == 0
    line = _warn(_ctx(out))
    assert f"forum inbox unreadable (http-{status})" in line and "@mentions may be waiting" in line
    assert _read_cache(home)["mentions"] is None
    assert err.startswith(f"engram-forum-prompt-hook: forum-inbox unknown: http-{status}"), err
    hook = _load_hook()
    items, reason = hook._fetch_inbox(forum.url, AGENT, time.monotonic() + 3.0)
    assert items is None and reason.startswith(f"http-{status}")


def test_iii_fetch_helpers_state_shapes(forum, tmp_path):
    hook = _load_hook()
    dl = lambda: time.monotonic() + 3.0  # noqa: E731
    forum.inbox = (404, "{}")
    assert hook._fetch_inbox(forum.url, AGENT, dl()) == ([], "unsupported")
    forum.inbox = (200, json.dumps({"inbox": []}))
    assert hook._fetch_inbox(forum.url, AGENT, dl()) == ([], None)
    forum.inbox = _mention_inbox()
    items, reason = hook._fetch_inbox(forum.url, AGENT, dl())
    assert reason is None and len(items) == 1
    forum.inbox = (503, "x")
    items, reason = hook._fetch_inbox(forum.url, AGENT, dl())
    assert items is None and reason.startswith("http-503")
    forum.threads = (200, json.dumps({"threads": []}))
    assert hook._fetch_threads(forum.url, None, AGENT, dl()) == ([], None)
    forum.threads = (404, "{}")
    t, reason = hook._fetch_threads(forum.url, None, AGENT, dl())
    assert t is None and reason.startswith("http-404"), "404 on THREADS is unknown, not unsupported"
    forum.threads = (200, "not json")
    t, reason = hook._fetch_threads(forum.url, None, AGENT, dl())
    assert t is None and reason.startswith("unparsable")
    t, reason = hook._fetch_threads(_dead_url(), None, AGENT, dl())
    assert t is None and reason.startswith("connect")
    t, reason = hook._fetch_threads(forum.url, None, AGENT, time.monotonic() - 1)
    assert t is None and reason.startswith("deadline")


# ===========================================================================
# (iv) inbox invalid JSON -> unknown line
# ===========================================================================

def test_iv_inbox_invalid_json_renders_the_unknown_line(tmp_path, forum):
    forum.inbox = (200, "<html>not json</html>")
    home = _home(tmp_path, forum.url)
    rc, out, err = _run(home)
    assert rc == 0
    assert "forum inbox unreadable (unparsable)" in _ctx(out)
    assert _read_cache(home)["mentions"] is None


# ===========================================================================
# (v) threads fail + stale cache -> stale output + the cached line
# ===========================================================================

def test_v_stale_cache_is_served_with_a_cached_counts_line(tmp_path):
    home = _home(tmp_path, _dead_url())
    _stale_cache(home, [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}], mentions=[])
    rc, out, err = _run(home)
    assert rc == 0
    resp = json.loads(out.strip())["hookSpecificOutput"]["additionalContext"]
    assert THREADS_LINE_2 in resp, "the existing stale output stays"
    assert "forum unreachable (connect)" in resp and "showing cached counts" in resp
    assert err.startswith("engram-forum-prompt-hook: forum-threads unknown: connect:")


def test_v_stale_cache_with_mention_keeps_the_mention_line(tmp_path):
    home = _home(tmp_path, _dead_url())
    _stale_cache(home, [], mentions=[{
        "thread_id": "7", "thread_title": "T", "author": "luria", "kind": "at_mention"}])
    rc, out, _ = _run(home)
    ctx = _ctx(out)
    assert rc == 0 and "\U0001f514 1 forum mention" in ctx and "showing cached counts" in ctx


# ===========================================================================
# (vi) umwelt mode
# ===========================================================================

def test_vi_umwelt_unknown_inbox_is_a_line_not_silence(tmp_path, forum):
    forum.inbox = (500, "boom")
    home = _home(tmp_path, forum.url, umwelt=True)
    rc, out, _ = _run(home)
    assert rc == 0
    assert "forum inbox unreadable (http-500)" in _ctx(out)


def test_vi_umwelt_unreachable_no_cache_is_a_line_not_silence(tmp_path):
    home = _home(tmp_path, _dead_url(), umwelt=True)
    rc, out, _ = _run(home)
    assert rc == 0 and "forum unreachable (connect)" in _ctx(out)


def test_vi_control_umwelt_healthy_empty_inbox_stays_silent(tmp_path, forum):
    home = _home(tmp_path, forum.url, umwelt=True)
    assert _run(home) == (0, "", "")


def test_vi_control_umwelt_mention_still_renders_the_advisory(tmp_path, forum):
    forum.inbox = _mention_inbox()
    home = _home(tmp_path, forum.url, umwelt=True)
    rc, out, err = _run(home)
    assert rc == 0 and err == "" and "comms_surface=umwelt: 1 addressed forum item" in _ctx(out)


def test_vi_umwelt_advisory_and_unknown_line_appear_together_advisory_first(tmp_path):
    # A stale cache holding an addressed item (advisory) + an unreachable
    # forum (unknown line): both must be in the one emitted context, with the
    # advisory ahead of the unknown line (head_blocks = advisory + unknown).
    home = _home(tmp_path, _dead_url(), umwelt=True)
    _stale_cache(home, [], mentions=[{
        "thread_id": "7", "thread_title": "T", "author": "luria", "kind": "at_mention"}])
    rc, out, _err = _run(home)
    ctx = _ctx(out)
    assert rc == 0
    advisory = "comms_surface=umwelt: 1 addressed forum item"
    assert advisory in ctx, ctx
    assert "forum unreachable (connect)" in ctx, ctx
    assert ctx.index(advisory) < ctx.index("forum unreachable"), ctx


# ===========================================================================
# (vii) rate limit
# ===========================================================================

def test_vii_second_prompt_inside_the_window_prints_no_second_line(tmp_path):
    home = _home(tmp_path, _dead_url())
    rc1, out1, _ = _run(home)
    rc2, out2, err2 = _run(home)
    assert rc1 == 0 and out1.strip()
    assert (rc2, out2) == (0, ""), "inside the window the line is not repeated"
    assert err2.strip(), "the stderr line is not rate-limited"


def test_vii_line_reappears_after_the_window(tmp_path):
    home = _home(tmp_path, _dead_url())
    _run(home)
    _write_stamp(home, "forum-threads", time.time() - 1801)
    rc, out, _ = _run(home)
    assert rc == 0 and "forum unreachable" in _ctx(out)


@pytest.mark.parametrize("stamp", [
    None,                                                   # absent
    "{not json",                                            # corrupt
    json.dumps(["wrong", "shape"]),                         # wrong-shaped
    json.dumps({"shown_at": {"forum-threads": time.time() + 10**6}}),  # future-dated
    json.dumps({"shown_at": {"forum-threads": "yesterday"}}),
], ids=["absent", "corrupt", "wrong-shape", "future", "non-numeric"])
def test_vii_bad_stamp_shows_the_line_and_never_raises(tmp_path, stamp):
    home = _home(tmp_path, _dead_url())
    if stamp is not None:
        (home / STAMP_NAME).write_text(stamp)
    rc, out, _ = _run(home)
    assert rc == 0 and "forum unreachable" in _ctx(out)


def test_vii_unwritable_stamp_shows_every_time_and_exits_zero(tmp_path):
    home = _home(tmp_path, _dead_url())
    (home / STAMP_NAME).mkdir()  # a directory where the stamp file belongs
    for _ in range(2):
        rc, out, _ = _run(home)
        assert rc == 0 and "forum unreachable" in _ctx(out)


def test_vii_keys_are_separate_for_threads_and_inbox(tmp_path, forum):
    forum.inbox = (500, "boom")
    home = _home(tmp_path, forum.url)
    _write_stamp(home, "forum-threads", time.time())  # a threads notice just shown
    rc, out, _ = _run(home)
    assert rc == 0 and "forum inbox unreadable" in _ctx(out)
    stamp = json.loads((home / STAMP_NAME).read_text())["shown_at"]
    assert set(stamp) == {"forum-threads", "forum-inbox"}


def test_vii_inbox_line_rate_limited_but_retry_continues(tmp_path, forum):
    forum.inbox = (500, "boom")
    home = _home(tmp_path, forum.url)
    _run(home)
    rc, out, _ = _run(home)
    assert rc == 0 and out == "", "second prompt inside the window: no second inbox line"
    assert forum.hits["inbox"] == 2, "...but the inbox is still retried"


def test_vii_stamp_written_only_after_the_response_was_printed(tmp_path, monkeypatch):
    hook = _load_hook()
    _patch(hook, monkeypatch, tmp_path, _dead_url())

    class _Broken(io.StringIO):
        def write(self, s):
            raise OSError("stdout closed")

    monkeypatch.setattr(sys, "stdout", _Broken())
    with contextlib.suppress(BaseException):
        hook.main()
    assert not (tmp_path / STAMP_NAME).exists(), "a print that failed must leave no stamp"
    monkeypatch.undo()
    # control: the same run with a working stdout does write it
    hook = _load_hook()
    _patch(hook, monkeypatch, tmp_path, _dead_url())
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with contextlib.suppress(SystemExit):
        hook.main()
    assert "forum unreachable" in buf.getvalue()
    assert "forum-threads" in json.loads((tmp_path / STAMP_NAME).read_text())["shown_at"]


def test_vii_stamp_not_written_when_the_flush_fails(tmp_path, monkeypatch):
    # The print itself succeeds here; only flush() raises. The stamp must be
    # written strictly AFTER a successful flush, so a line that was buffered
    # but never delivered does not start a rate-limit window.
    hook = _load_hook()
    _patch(hook, monkeypatch, tmp_path, _dead_url())

    class _FlushFails(io.StringIO):
        def flush(self):
            raise OSError("flush failed: broken pipe")

    buf = _FlushFails()
    monkeypatch.setattr(sys, "stdout", buf)
    with contextlib.suppress(SystemExit):
        hook.main()
    assert "forum unreachable" in buf.getvalue(), "the print itself must have succeeded"
    assert not (tmp_path / STAMP_NAME).exists(), "an unflushed line must leave no stamp"


# ===========================================================================
# (viii) old-format cache files keep their meaning
# ===========================================================================

def test_viii_old_cache_without_mentions_key_means_empty_no_refetch(tmp_path, forum):
    home = _home(tmp_path, forum.url)
    (home / CACHE_NAME).write_text(json.dumps(
        {"fetched_at": time.time(), "threads": [{"id": "1", "title": "a"}]}))
    rc, out, err = _run(home)
    assert (rc, err) == (0, "")
    assert "\U0001f4e2 1 new forum post" in _ctx(out) and "\U0001f514" not in _ctx(out)
    assert forum.hits == {"threads": 0, "inbox": 0}


def test_viii_old_cache_with_a_mentions_list_is_served_as_is(tmp_path, forum):
    home = _home(tmp_path, forum.url)
    (home / CACHE_NAME).write_text(json.dumps({
        "fetched_at": time.time(), "threads": [],
        "mentions": [{"thread_id": "5", "thread_title": "X", "author": "luria", "kind": "at_mention"}],
    }))
    rc, out, err = _run(home)
    assert (rc, err) == (0, "") and "\U0001f514 1 forum mention" in _ctx(out)
    assert forum.hits == {"threads": 0, "inbox": 0}


def test_viii_old_cache_with_empty_mentions_list_is_empty_not_unknown(tmp_path, forum):
    home = _home(tmp_path, forum.url)
    (home / CACHE_NAME).write_text(json.dumps(
        {"fetched_at": time.time(), "threads": [], "mentions": []}))
    assert _run(home) == (0, "", "")
    assert forum.hits == {"threads": 0, "inbox": 0}


# ===========================================================================
# K8: healthy paths are byte-identical to dev
# ===========================================================================

def test_k8_new_posts_only_is_byte_identical(tmp_path, forum):
    forum.threads = _threads_body(2)
    home = _home(tmp_path, forum.url)
    rc, out, err = _run(home)
    assert (rc, err) == (0, "")
    assert out == json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\u25b8 1 shown \u00b7 0 cut\n" + THREADS_LINE_2}}) + "\n"


def test_k8_mentions_present_is_byte_identical(tmp_path, forum):
    forum.inbox = _mention_inbox()
    forum.threads = _threads_body(2)
    home = _home(tmp_path, forum.url)
    rc, out, err = _run(home)
    mention = ('\U0001f514 1 forum mention waiting on you: #99 "Review my PR" '
               '(@mention by luria) — `forum read <id>` to clear.')
    assert (rc, err) == (0, "")
    ctx = _ctx(out)
    assert mention in ctx and THREADS_LINE_2 in ctx and ctx.index(mention) < ctx.index(THREADS_LINE_2)
    assert "⚠" not in ctx


# ===========================================================================
# (ix) sanitization + bounds (in-process: the reason text is injected)
# ===========================================================================

def _load_hook(name="forum_hook_3521"):
    if str(HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(HOOK_DIR))
    spec = importlib.util.spec_from_file_location(name, str(HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch(hook, monkeypatch, home: Path, url: str, *, umwelt=False, agent=AGENT):
    monkeypatch.setattr(hook, "ENGRAM_HOME", str(home))
    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(home / CURSOR_NAME))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(home / CACHE_NAME))
    monkeypatch.setattr(hook, "STATUS_PUBLISH_PATH", str(home / "forum-status-publish.json"))
    monkeypatch.setattr(hook, "derive_own_status", None)
    cfg = {"agent_name": agent, "forum": {"url": url}}
    if umwelt:
        cfg["comms_surface"] = "umwelt"
    monkeypatch.setattr(hook, "_load_config", lambda: cfg)
    monkeypatch.setattr(hook, "_get_comms_surface", lambda c: "umwelt" if umwelt else "forum")


def _main_capture(hook):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with contextlib.suppress(SystemExit):
            hook.main()
    return out.getvalue(), err.getvalue()


HOSTILE = "boom\r\nINJECTED\x00\x1b[31m" + ("A" * 5000)


@pytest.mark.parametrize("which", ["threads", "inbox"])
def test_ix_hostile_reason_is_sanitized_capped_and_never_injected(tmp_path, monkeypatch, which):
    hook = _load_hook()
    _patch(hook, monkeypatch, tmp_path, "http://127.0.0.1:1")
    if which == "threads":
        monkeypatch.setattr(hook, "_fetch_threads", lambda *a: (None, "connect: " + HOSTILE))
    else:
        monkeypatch.setattr(hook, "_fetch_threads", lambda *a: ([], None))
        monkeypatch.setattr(hook, "_fetch_inbox", lambda *a: (None, "unparsable: " + HOSTILE))
    out, err = _main_capture(hook)
    ctx = _ctx(out)
    line = _warn(ctx)
    assert "AAAA" not in ctx and "INJECTED" not in ctx and "\x1b" not in ctx and "\x00" not in ctx
    assert len(line) <= 200
    err_lines = err.splitlines()
    assert len(err_lines) == 1 and len(err_lines[0]) <= 300
    assert not any(ord(c) < 32 for c in err_lines[0])


@pytest.mark.parametrize("probe,cached", [("threads", False), ("threads", True), ("inbox", False)],
                         ids=["unreachable", "cached", "inbox"])
@pytest.mark.parametrize("reason", [
    "connect: " + HOSTILE,
    "http-500: " + "B" * 5000,                      # a long message, no control chars
    ("C" * 5000),                                   # no colon: a 5000-char CLASS
    "connect:    line separators \x85 " + "D" * 300,
], ids=["hostile", "long-message", "long-class", "unicode-separators"])
def test_ix_unknown_line_itself_is_one_bounded_class_only_line(probe, cached, reason):
    # Unit-level on purpose: through main() the shared renderer trims the head
    # at its first line break, which would mask a raw-message leak.
    hook = _load_hook()
    line = hook._unknown_line(probe, reason, cached)
    assert len(line) <= 200
    assert len(line.splitlines()) == 1 and not any(ord(c) < 32 for c in line)
    assert "INJECTED" not in line and "BBBB" not in line and "DDDD" not in line
    assert "C" * 41 not in line, "the class is capped at 40 chars"
    assert line.startswith("⚠️ forum ")


def test_ix_long_message_without_newlines_never_reaches_the_injected_context(tmp_path, monkeypatch):
    hook = _load_hook()
    _patch(hook, monkeypatch, tmp_path, "http://127.0.0.1:1")
    monkeypatch.setattr(hook, "_fetch_threads", lambda *a: (None, "connect: " + "A" * 5000))
    out, _err = _main_capture(hook)
    assert "AAAA" not in _ctx(out)


def test_ix_class_without_colon_and_hostile_class_are_bounded(tmp_path, monkeypatch):
    hook = _load_hook()
    _patch(hook, monkeypatch, tmp_path, "http://127.0.0.1:1")
    monkeypatch.setattr(hook, "_fetch_threads", lambda *a: (None, "X\n" * 3000))
    out, err = _main_capture(hook)
    line = _warn(_ctx(out))
    assert len(line) <= 200 and len(err.splitlines()) == 1


# ===========================================================================
# (x) degradation: never silent, never raises
# ===========================================================================

def test_x_probe_helpers_missing_degrades_to_a_plain_bounded_line(tmp_path, monkeypatch):
    real = _load_hook("forum_hook_3521_real")
    fake = types.ModuleType("_hooklib")
    fake.emit_context = real.emit_context  # the one unconditional import
    monkeypatch.setitem(sys.modules, "_hooklib", fake)
    hook = _load_hook("forum_hook_3521_nohelpers")
    assert hook._PROBE_LIB_OK is False
    _patch(hook, monkeypatch, tmp_path, "http://127.0.0.1:1")
    monkeypatch.setattr(hook, "_fetch_threads", lambda *a: (None, "connect: " + HOSTILE))
    for _ in range(2):  # no rate limit without the helpers: never silent
        out, err = _main_capture(hook)
        ctx = _ctx(out)
        line = _warn(ctx)
        assert "forum unreachable" in line and len(line) <= 200
        assert "AAAA" not in ctx and "INJECTED" not in ctx
        assert len(err.splitlines()) == 1 and len(err.splitlines()[0]) <= 300
    assert not (tmp_path / STAMP_NAME).exists()


def test_x_hook_exits_zero_when_the_probe_helpers_raise(tmp_path, monkeypatch):
    hook = _load_hook()
    _patch(hook, monkeypatch, tmp_path, "http://127.0.0.1:1")
    monkeypatch.setattr(hook, "_fetch_threads", lambda *a: (None, "connect: x"))

    def boom(*a, **k):
        raise RuntimeError("helper exploded")

    for name in ("probe_notice_due", "mark_probe_notice", "write_probe_unknown_stderr"):
        monkeypatch.setattr(hook, name, boom)
    out, _err = _main_capture(hook)
    assert "forum unreachable" in _ctx(out), "a failing helper degrades to the plain line, not silence"
