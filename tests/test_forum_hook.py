"""Tests for hooks/claude/engram-forum-prompt-hook.py.

Covers three sets of scenarios:

A) Surfaced-cursor advance semantics (original tests):
   1. Cache hit  -> cursor UNCHANGED.
   2. Live fetch success -> cursor ADVANCED to ~now.
   3. Unreachable, no cache -> cursor UNCHANGED.

B) Mention-line injection (inbox endpoint, post-#907):
   1. Mention line present when _fetch_inbox returns >=1 at_mention item.
   2. Mention line ABSENT when _fetch_inbox returns None (old server / unreachable),
      generic-thread line unaffected.
   3. (#907 contract) Mention in inbox appears in banner; when the server marks
      the thread read (simulated: inbox no longer returns it), the banner clears.
      Banner is a pure function of server read-state — no local cursor involved.
   4. Persistence: same mention surfaces on consecutive prompts when inbox
      still returns it (server has not yet marked the thread read).
   5. Cache stores both threads + mentions; fresh-cache hit reads both without
      a second server call; OLD cache file (no `mentions` key) -> no KeyError.
   6. Both-lines ordering: mention line ABOVE generic threads line.
   7. Empty agent_name -> no inbox fetch; generic thread line still works.
   8. 404 inbox endpoint (old server) -> no mention line; generic unchanged.
   9. kind=reply_on_my_thread items from the inbox are NOT surfaced in the banner.

C) Surfaced-cursor precision regression (#751):
   1. Same-second post not excluded by cursor.

Each test loads the hook as a module via importlib, monkeypatches
SURFACED_CURSOR_PATH and HOOK_CACHE_PATH to tmp paths, and stubs
_load_config / _fetch_threads / _fetch_inbox as needed.
main() is called directly (catching SystemExit); then cursors / output
are inspected.
"""

import importlib.util
import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import call, patch

import pytest

HOOK_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "engram-forum-prompt-hook.py"
)


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_hook():
    """Load the forum prompt hook as a Python module (fresh import each call)."""
    spec = importlib.util.spec_from_file_location(
        "engram_forum_prompt_hook", HOOK_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_cursor_file(path: Path, ts: datetime) -> None:
    """Write a UTC datetime to a cursor file in the hook's expected format."""
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    path.write_text(ts_str + "\n")


def _read_cursor_bytes(path: Path) -> bytes:
    """Read a cursor file as raw bytes for byte-for-byte comparison."""
    return path.read_bytes()


def _write_fresh_cache(cache_path: Path, threads: list, mentions: list = None) -> None:
    """Write a fresh (not expired) cache file at cache_path."""
    payload = {"fetched_at": time.time(), "threads": threads}
    if mentions is not None:
        payload["mentions"] = mentions
    cache_path.write_text(json.dumps(payload))


def _make_inbox_item(tid: str = "42", title: str = "Test Thread",
                     author: str = "luria", kind: str = "at_mention") -> dict:
    """Build a minimal inbox item dict matching the API schema.

    The inbox endpoint returns items with post_id/thread_id/thread_title/author/
    kind/created_at; kind is 'at_mention' or 'reply_on_my_thread'.
    """
    return {
        "thread_id": tid,
        "thread_title": title,
        "author": author,
        "kind": kind,
        "post_id": 1,
        "created_at": "2026-01-01T12:00:00Z",
    }


# Back-compat alias used in tests that pre-date the inbox migration.
_make_mention = _make_inbox_item


def _run_main(hook, capture_output: bool = False):
    """Run hook.main(), catching SystemExit. Returns captured stdout if requested."""
    if capture_output:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                hook.main()
        except SystemExit:
            pass
        return buf.getvalue()
    else:
        try:
            hook.main()
        except SystemExit:
            pass
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cursor(tmp_path: Path) -> Path:
    return tmp_path / "forum-surfaced-cursor.txt"


@pytest.fixture
def tmp_read_cursor(tmp_path: Path) -> Path:
    return tmp_path / "forum-read-cursor.txt"


@pytest.fixture
def tmp_cache(tmp_path: Path) -> Path:
    return tmp_path / "forum-hook-cache.json"


def _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor=None):
    """Point the hook module at tmp paths.

    tmp_read_cursor is accepted for backward compat but is a no-op —
    the hook no longer reads READ_CURSOR_PATH for mention queries.
    """
    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))


def _stub_config(hook, monkeypatch, agent_name: str = "testbot"):
    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": agent_name,
        "forum": {"url": "http://localhost:5002"},
    })


# ===========================================================================
# Part A — Surfaced-cursor advance semantics (original tests, preserved)
# ===========================================================================

# ---------------------------------------------------------------------------
# Scenario A1: Cache hit -> cursor UNCHANGED
# ---------------------------------------------------------------------------

def test_cache_hit_cursor_unchanged(tmp_cursor, tmp_cache, monkeypatch):
    """On a cache hit, the surfaced cursor must not advance.

    The live fetch that populated the cache already advanced the cursor to
    its fetch time. Advancing again to `now` here would step past posts that
    arrived after the cache was populated but before it expired.
    """
    hook = _load_hook()

    # Point the module at our tmp paths
    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))

    # Stub _load_config to return a configured dict (avoids touching disk)
    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": "testbot",
        "forum": {"url": "http://localhost:5002"},
    })

    # Write a known cursor value
    known_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_cursor_file(tmp_cursor, known_ts)
    cursor_before = _read_cursor_bytes(tmp_cursor)

    # Seed a fresh cache (age 0 — well within 30s TTL)
    _write_fresh_cache(tmp_cache, [])

    # Run main(); it will hit the cache branch and call sys.exit(0) because
    # threads is empty — catch that.
    try:
        hook.main()
    except SystemExit:
        pass

    # Cursor must be byte-for-byte unchanged
    cursor_after = _read_cursor_bytes(tmp_cursor)
    assert cursor_after == cursor_before, (
        f"Cursor must be unchanged on a cache hit; "
        f"before={cursor_before!r}, after={cursor_after!r}"
    )


# ---------------------------------------------------------------------------
# Scenario A2: Live fetch success -> cursor ADVANCED
# ---------------------------------------------------------------------------

def test_live_fetch_success_cursor_advanced(tmp_cursor, tmp_cache, monkeypatch):
    """On a successful live fetch, the surfaced cursor must advance to ~now."""
    hook = _load_hook()

    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))

    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": "testbot",
        "forum": {"url": "http://localhost:5002"},
    })

    # Write a known cursor value in the past
    old_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_cursor_file(tmp_cursor, old_ts)

    # No cache file -> forces the live-fetch branch
    assert not tmp_cache.exists()

    # Stub _fetch_threads to return a non-None empty list (success, no threads)
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))

    before_call = datetime.now(timezone.utc)

    # main() will call sys.exit(0) because threads list is empty
    try:
        hook.main()
    except SystemExit:
        pass

    after_call = datetime.now(timezone.utc)

    # Cursor must exist and have advanced past old_ts
    assert tmp_cursor.exists(), "Cursor file must exist after live fetch"
    raw = tmp_cursor.read_text().strip()
    new_ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assert new_ts > old_ts, (
        f"Cursor must advance on live fetch success; old={old_ts}, new={new_ts}"
    )
    # Sanity: the new cursor is within the window of the call
    assert new_ts >= before_call.replace(microsecond=0) or new_ts >= before_call, (
        f"Cursor must be ~now after live fetch; before_call={before_call}, new={new_ts}"
    )
    _ = after_call  # referenced for documentation, not strictly needed in assertion


# ---------------------------------------------------------------------------
# Scenario A3: Unreachable, no cache -> cursor UNCHANGED
# ---------------------------------------------------------------------------

def test_unreachable_no_cache_cursor_unchanged(tmp_cursor, tmp_cache, monkeypatch):
    """When server is unreachable and no cache exists, cursor must not advance.

    Leaving the cursor in place ensures the next successful query re-checks
    the same window and doesn't miss posts made during the outage.
    """
    hook = _load_hook()

    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))

    monkeypatch.setattr(hook, "_load_config", lambda: {
        "agent_name": "testbot",
        "forum": {"url": "http://localhost:5002"},
    })

    # Write a known cursor value
    known_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    _write_cursor_file(tmp_cursor, known_ts)
    cursor_before = _read_cursor_bytes(tmp_cursor)

    # No cache file, and _fetch_threads returns None (server unreachable)
    assert not tmp_cache.exists()
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: (None, "connect: ConnectionRefusedError: refused"))

    # main() exits silently (threads=None -> no-op)
    try:
        hook.main()
    except SystemExit:
        pass

    # Cursor must be byte-for-byte unchanged
    cursor_after = _read_cursor_bytes(tmp_cursor)
    assert cursor_after == cursor_before, (
        f"Cursor must be unchanged when server is unreachable and no cache exists; "
        f"before={cursor_before!r}, after={cursor_after!r}"
    )


# ===========================================================================
# Part B — Mention-line injection (new tests, spec §6)
# ===========================================================================

# ---------------------------------------------------------------------------
# B1: Mention line present when endpoint returns >=1 mention
# ---------------------------------------------------------------------------

def test_mention_line_present_when_mentions_returned(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """🔔 mention line appears in output when _fetch_inbox returns >=1 at_mention item."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    mention = _make_inbox_item(tid="99", title="Review my PR", author="luria")

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
    monkeypatch.setattr(hook, "_fetch_inbox",
                        lambda url, name, deadline: ([mention], None))

    output = _run_main(hook, capture_output=True)

    assert output.strip(), "Expected non-empty output when mentions are present"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "\U0001f514" in ctx, "Mention line (🔔) must appear in output"
    assert "luria" in ctx
    assert "#99" in ctx
    assert "Review my PR" in ctx
    assert "forum read" in ctx, "Nudge to `forum read <id>` must appear"


# ---------------------------------------------------------------------------
# B2: Mention line ABSENT when endpoint returns None; generic line unaffected
# ---------------------------------------------------------------------------

def test_mention_line_absent_on_none_generic_unaffected(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """When _fetch_inbox returns None, no mention line; threads line unchanged."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent, deadline: ([{"id": "1", "title": "hello"}], None))
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: (None, "http-500: HTTP 500"))

    output = _run_main(hook, capture_output=True)

    assert output.strip(), "Expected output (threads line) when threads present"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]

    # Generic threads line must be present
    assert "\U0001f4e2" in ctx, "Generic threads line (📢) must appear"
    # Mention line must NOT be present
    assert "\U0001f514" not in ctx, (
        "Mention line (🔔) must NOT appear when _fetch_inbox returns None"
    )


# ---------------------------------------------------------------------------
# B3: #907 contract — banner is a pure function of server read-state
#     A mention present in the inbox appears in the banner; after the server
#     marks the thread read (simulated: inbox no longer returns the item),
#     the banner clears. No local cursor is involved.
# ---------------------------------------------------------------------------

def test_907_banner_clears_when_inbox_no_longer_returns_mention(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """#907 regression contract: banner is driven entirely by the server inbox.

    Prompt 1: inbox returns a mention → 🔔 banner appears.
    Prompt 2: server marks the thread read (simulated: inbox returns []) → 🔔 clears.

    This is the core invariant fixed by #907: the banner must be a pure function
    of the server's read-state, not a local cursor that can drift out of sync.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    mention = _make_inbox_item(tid="55", title="Please review", author="ariadne")
    inbox_items = [mention]

    def fetch_inbox_dynamic(url, name, deadline):
        return list(inbox_items), None

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
    monkeypatch.setattr(hook, "_fetch_inbox", fetch_inbox_dynamic)

    # Prompt 1: mention present in inbox → banner appears
    output1 = _run_main(hook, capture_output=True)
    assert output1.strip(), "Prompt 1: expected non-empty output"
    data1 = json.loads(output1.strip())
    ctx1 = data1["hookSpecificOutput"]["additionalContext"]
    assert "\U0001f514" in ctx1, "Prompt 1: mention line must appear when inbox returns it"

    # Expire the cache so prompt 2 does a live fetch
    if tmp_cache.exists():
        cache_data = json.loads(tmp_cache.read_text())
        cache_data["fetched_at"] = time.time() - 60
        tmp_cache.write_text(json.dumps(cache_data))

    # Server marks the thread read — inbox now returns []
    inbox_items.clear()

    # Reload hook for a clean module state
    hook2 = _load_hook()
    _patch_hook_paths(hook2, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook2, monkeypatch)
    monkeypatch.setattr(hook2, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
    monkeypatch.setattr(hook2, "_fetch_inbox", fetch_inbox_dynamic)

    # Prompt 2: inbox is empty → banner must clear (exit 0, no output)
    output2 = _run_main(hook2, capture_output=True)
    if output2.strip():
        data2 = json.loads(output2.strip())
        ctx2 = data2["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f514" not in ctx2, (
            "Prompt 2: banner must clear when inbox returns [] "
            "(server has marked the thread read — #907 contract)"
        )


# ---------------------------------------------------------------------------
# B4: Persistence — same mention surfaces on consecutive prompts when
#     read cursor has NOT advanced
# ---------------------------------------------------------------------------

def test_mentions_persist_when_inbox_still_returns_item(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Mention line appears on both prompt-1 and prompt-2 when inbox still returns it.

    The "stays in your court" property: a mention persists until the server
    marks the thread read (i.e., until the agent runs `forum read <id>`).
    The hook sees the same mention from the inbox on consecutive prompts.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc))

    mention = _make_inbox_item(tid="7", title="A colleague needs you", author="colleague-a")

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))

    def fetch_inbox(url, name, deadline):
        # Returns the same mention regardless — simulates a server where the
        # thread has not been marked read yet.
        return [mention], None

    monkeypatch.setattr(hook, "_fetch_inbox", fetch_inbox)

    # Prompt 1
    output1 = _run_main(hook, capture_output=True)
    # Expire the cache so prompt 2 does a live fetch again
    if tmp_cache.exists():
        cache_data = json.loads(tmp_cache.read_text())
        cache_data["fetched_at"] = time.time() - 60  # force expired
        tmp_cache.write_text(json.dumps(cache_data))

    # Advance surfaced cursor (hook does this on live fetch)
    _write_cursor_file(tmp_cursor, datetime(2026, 1, 2, 1, 0, 0, tzinfo=timezone.utc))

    # Prompt 2
    output2 = _run_main(hook, capture_output=True)

    for prompt_n, output in enumerate([output1, output2], start=1):
        assert output.strip(), f"Prompt {prompt_n}: expected non-empty output"
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f514" in ctx, (
            f"Prompt {prompt_n}: mention line must persist while inbox still returns it"
        )


# ---------------------------------------------------------------------------
# B5a: Cache stores both threads + mentions; fresh-cache prompt reads both
#      without a second server hit
# ---------------------------------------------------------------------------

def test_cache_stores_and_reads_both_threads_and_mentions(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """A fresh-cache prompt reads both threads+mentions without calling _fetch_inbox."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    mention = _make_inbox_item(tid="55", title="Cached mention", author="luria")
    # Write a cache file that already contains mentions
    _write_fresh_cache(tmp_cache, threads=[], mentions=[mention])

    fetch_inbox_calls = []

    def stub_fetch_inbox(url, name, deadline):
        fetch_inbox_calls.append((url, name))
        return [], None

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
    monkeypatch.setattr(hook, "_fetch_inbox", stub_fetch_inbox)

    output = _run_main(hook, capture_output=True)

    # _fetch_inbox must NOT have been called (cache was fresh)
    assert fetch_inbox_calls == [], (
        "_fetch_inbox must not be called on a fresh-cache hit"
    )
    # But the mention line from cache must still appear
    assert output.strip(), "Expected output from cached mention"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "\U0001f514" in ctx, "Cached mention line must appear from fresh cache"
    assert "Cached mention" in ctx


# ---------------------------------------------------------------------------
# B5b: OLD cache file (no `mentions` key) -> no KeyError; mention line absent
# ---------------------------------------------------------------------------

def test_old_cache_no_mentions_key_no_keyerror(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """An old cache file without a `mentions` key must not raise KeyError.

    The hook defaults to [] via cache.get('mentions', []) for backward compat.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    # Old-style cache: only threads, no mentions key
    old_payload = {"fetched_at": time.time(), "threads": [{"id": "1"}]}
    tmp_cache.write_text(json.dumps(old_payload))

    # Should not raise; mention line should simply be absent
    output = _run_main(hook, capture_output=True)

    if output.strip():
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f514" not in ctx, (
            "Mention line must be absent when old cache has no `mentions` key"
        )
    # reaching here without exception is also a pass (thread only -> exit 0 is fine
    # if threads list has no renderable entries in this hook's logic)


# ---------------------------------------------------------------------------
# B5c: STALE cache with mentions -> mention line still renders from stale cache
# ---------------------------------------------------------------------------

def test_stale_cache_fallback_renders_mentions(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Stale-cache fallback (server unreachable) renders mention line when
    the stale cache contains mentions.

    The stale-fallback path (``stale.get('mentions', [])``) is exercised
    when _fetch_threads returns None AND a cache file exists but is expired.
    This test is the symmetric counterpart to B5a (fresh-cache with mentions)
    and B5b (old cache without mentions key).
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    mention = _make_inbox_item(tid="77", title="Stale but present", author="luria")

    # Write an EXPIRED cache (fetched_at far in the past) that contains a mention.
    # _load_cache() will return None (expired), forcing the live-fetch path.
    # _fetch_threads returns None (server unreachable), triggering the stale fallback.
    stale_payload = {
        "fetched_at": time.time() - 3600,  # 1 hour old — well past 30s TTL
        "threads": [],
        "mentions": [mention],
    }
    tmp_cache.write_text(json.dumps(stale_payload))

    # Server unreachable: _fetch_threads returns None
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: (None, "connect: ConnectionRefusedError: refused"))

    output = _run_main(hook, capture_output=True)

    assert output.strip(), "Expected non-empty output: mention from stale cache"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "\U0001f514" in ctx, (
        "Mention line (🔔) must render from stale-cache fallback when cache has mentions"
    )
    assert "luria" in ctx, "Mention author must appear from stale cache"
    assert "#77" in ctx, "Mention thread ID must appear from stale cache"
    assert "Stale but present" in ctx, "Mention thread title must appear from stale cache"


# ---------------------------------------------------------------------------
# B6: Both-lines ordering — mention line ABOVE generic threads line
# ---------------------------------------------------------------------------

def test_both_lines_ordering_mention_above_generic(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """When both mention and thread lines are present, 🔔 appears above 📢."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    mention = _make_inbox_item(tid="10", title="Needs review", author="luria")

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent, deadline: ([{"id": "2", "title": "new thread"}], None))
    monkeypatch.setattr(hook, "_fetch_inbox",
                        lambda url, name, deadline: ([mention], None))

    output = _run_main(hook, capture_output=True)
    assert output.strip(), "Expected non-empty output"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]

    # Both lines must be present
    assert "\U0001f514" in ctx, "Mention line (🔔) must be present"
    assert "\U0001f4e2" in ctx, "Generic threads line (📢) must be present"

    # 🔔 must come before 📢
    bell_pos = ctx.index("\U0001f514")
    megaphone_pos = ctx.index("\U0001f4e2")
    assert bell_pos < megaphone_pos, (
        f"Mention line (🔔 at {bell_pos}) must appear ABOVE "
        f"generic threads line (📢 at {megaphone_pos})"
    )


# ---------------------------------------------------------------------------
# B7: Empty agent_name -> no mentions fetch; generic thread line still works
# ---------------------------------------------------------------------------

def test_empty_agent_name_no_mentions_fetch_generic_works(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """When agent_name is empty, _fetch_inbox is never called; threads work."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)

    # Config that resolves to empty agent_name (no agent_name, no USER/LOGNAME env)
    monkeypatch.setattr(hook, "_load_config", lambda: {
        "forum": {"url": "http://localhost:5002"},
    })
    # Override _get_agent_name to return empty string
    monkeypatch.setattr(hook, "_get_agent_name", lambda config: "")

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    fetch_inbox_calls = []

    def stub_fetch_inbox(url, name, deadline):
        fetch_inbox_calls.append(name)
        return [_make_inbox_item()], None

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent, deadline: ([{"id": "3", "title": "thread"}], None))
    monkeypatch.setattr(hook, "_fetch_inbox", stub_fetch_inbox)

    output = _run_main(hook, capture_output=True)

    assert fetch_inbox_calls == [], (
        "_fetch_inbox must NOT be called when agent_name is empty"
    )
    # Threads line must still be present
    assert output.strip(), "Expected threads output even with empty agent_name"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "\U0001f4e2" in ctx, "Generic threads line must work with empty agent_name"
    assert "\U0001f514" not in ctx, "No mention line when agent_name is empty"


# ---------------------------------------------------------------------------
# B8: 404 inbox endpoint (old server) -> silent skip; generic unchanged
#     (This is the "safe to merge before every install has the inbox endpoint" test)
#
#     Two layers:
#     (a) _fetch_inbox itself swallows a 404 HTTPError -> returns None.
#     (b) main() receiving None from _fetch_inbox -> no mention line,
#         generic thread line still works.
# ---------------------------------------------------------------------------

def test_fetch_inbox_swallows_404(monkeypatch):
    """_fetch_inbox must not raise on a 404 and must report it as the
    `unsupported` state: `([], "unsupported")`.

    #3521 row 5: a missing /api/agent/<name>/inbox endpoint on an old server is
    healthy by design -- known-empty, no line, cached as `[]` -- and, unlike
    every OTHER failure (which is now `(None, reason)` = unknown), it IS
    "no mentions" from the caller's perspective.

    gh#3107: _fetch_inbox now drives http.client.HTTPConnection (via
    _fetch_with_deadline), not urlopen -- mocked at the new layer. A non-2xx
    status is read off resp.status directly (http.client does not raise on
    it the way urlopen raises HTTPError), so this fake's status=404 exercises
    the same swallow path via a different mechanism.
    """
    hook = _load_hook()

    class FakeResponse:
        status = 404
        def read(self, n=-1):
            return b""

    class FakeConn:
        # `sock = None`: the fetch captures conn.sock before getresponse().
        # Without the attribute that line raised AttributeError and the old
        # swallow-all handler returned None -- this test passed WITHOUT ever
        # reaching the 404 status check it is named for (found by #3521 row 5,
        # whose classified fetch reports that AttributeError as an unknown).
        sock = None
        def __init__(self, host, port, timeout=None):
            pass
        def connect(self):
            pass
        def request(self, method, path, body=None, headers=None):
            pass
        def getresponse(self):
            return FakeResponse()
        def close(self):
            pass

    monkeypatch.setattr(hook.http.client, "HTTPConnection", FakeConn)

    result = hook._fetch_inbox(
        "http://localhost:5002", "testbot", hook.time.monotonic() + 5.0
    )
    assert result == ([], "unsupported"), (
        "_fetch_inbox must report a 404 status (old server) as unsupported"
    )


def test_old_server_404_inbox_silent_skip_in_main(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """A 404 from the inbox endpoint results in no mention line; generic unchanged.

    Validates the safety property: a hook hitting an old server (without the
    /api/agent/<name>/inbox endpoint) silently skips the mention line — it does
    NOT break the hook chain or suppress the threads line.
    Uses _fetch_inbox returning None (as it would after swallowing a 404).
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent, deadline: ([{"id": "5", "title": "new thread"}], None))
    # Simulate what _fetch_inbox returns after swallowing a 404
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([], "unsupported"))

    output = _run_main(hook, capture_output=True)

    assert output.strip(), "Expected threads output even when inbox endpoint is 404"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "\U0001f4e2" in ctx, "Generic threads line must work when inbox 404s"
    assert "\U0001f514" not in ctx, (
        "Mention line must be absent when inbox endpoint returns None (old server)"
    )


# ---------------------------------------------------------------------------
# B9: kind=reply_on_my_thread items NOT surfaced in the banner
#     Two layers:
#     (a) _fetch_inbox itself filters to at_mention only before returning.
#     (b) When inbox contains ONLY reply_on_my_thread items, the banner is
#         absent — _fetch_inbox returns [] and no 🔔 line is emitted.
# ---------------------------------------------------------------------------

def test_fetch_inbox_filters_reply_on_my_thread(monkeypatch):
    """_fetch_inbox must return only at_mention items, not reply_on_my_thread.

    This tests the filtering layer inside _fetch_inbox: when the server inbox
    response contains both kinds, only at_mention items reach the caller.

    gh#3107: mocked at the http.client.HTTPConnection layer _fetch_inbox now
    drives (via _fetch_with_deadline), not urlopen.
    """
    hook = _load_hook()

    # Build a fake server response with both kinds
    inbox_response = {
        "inbox": [
            {"thread_id": "10", "thread_title": "Mention", "author": "a",
             "kind": "at_mention", "post_id": 1, "created_at": "2026-01-01T12:00:00Z"},
            {"thread_id": "11", "thread_title": "Reply", "author": "b",
             "kind": "reply_on_my_thread", "post_id": 2, "created_at": "2026-01-01T12:00:01Z"},
        ],
        "unread_all": 2,
    }
    body = json.dumps(inbox_response).encode("utf-8")

    class FakeResponse:
        status = 200
        def __init__(self, body):
            self._chunks = [body, b""]
        def read1(self, n=-1):
            return self._chunks.pop(0)

    class FakeConn:
        def __init__(self, host, port, timeout=None):
            pass
        def connect(self):
            pass
        def request(self, method, path, body=None, headers=None):
            pass
        def getresponse(self):
            return FakeResponse(body)
        def close(self):
            pass
        @property
        def sock(self):
            class _FakeSock:
                def settimeout(self, t):
                    pass
            return _FakeSock()

    monkeypatch.setattr(hook.http.client, "HTTPConnection", FakeConn)

    result = hook._fetch_inbox(
        "http://localhost:5002", "testbot", hook.time.monotonic() + 5.0
    )
    result, reason = result
    assert result is not None and reason is None, "_fetch_inbox must succeed"
    assert len(result) == 1, (
        f"_fetch_inbox must return only at_mention items; got {result!r}"
    )
    assert result[0]["kind"] == "at_mention", (
        "Only at_mention items must be returned by _fetch_inbox"
    )
    assert result[0]["thread_id"] == "10"


def test_reply_on_my_thread_only_inbox_no_banner(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """When inbox contains only reply_on_my_thread items, the 🔔 banner is absent.

    _fetch_inbox filters to at_mention; when the inbox has no at_mention items,
    _fetch_inbox returns [] and the hook emits no mention banner.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    # _fetch_inbox returns [] because all inbox items are reply_on_my_thread
    # (the real _fetch_inbox filters; this simulates that filtered result)
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([], None))

    output = _run_main(hook, capture_output=True)

    # No at_mention items → no banner, no output (exit 0)
    if output.strip():
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f514" not in ctx, (
            "reply_on_my_thread items must NOT appear in the 🔔 mention banner"
        )


# ---------------------------------------------------------------------------
# B10: Worst-case three-call budget regression (PR #1642 colleague review)
#      Worst case per fire is threads + inbox + publish, not threads-or-inbox
#      + publish. FETCH_TIMEOUT_SECONDS covers BOTH the threads and inbox call
#      sites, so the real invariant is 2*FETCH + PUBLISH, not FETCH + PUBLISH.
# ---------------------------------------------------------------------------

def test_worst_case_three_call_budget_under_hook_timeout():
    """Borges's PR #1642 review catch: a slow-but-succeeding threads fetch
    followed by a timed-out inbox fetch, followed by the unconditional publish
    POST, is three calls in one hook fire — not the two the original comment
    assumed. This regression-guards the actual sum against the 5s hooks.json
    budget, so a future constant bump can't silently reintroduce the gap.

    Scope note (gh#1906): this guards the three calls owned by THIS module's
    two constants. A fourth, uncached call (_fetch_held_batons, via
    derive_own_status, on its own independent _FORUM_REQUEST_TIMEOUT_SECONDS
    in _status_derive.py) is a separate, still-open gap — not asserted here.
    See the KNOWN GAP comment above FETCH_TIMEOUT_SECONDS in the hook module.

    gh#3107: this arithmetic is a CONSTANT-BUMP GUARD, kept as exactly that —
    it catches someone raising FETCH/PUBLISH_TIMEOUT_SECONDS without checking
    the sum. It does NOT itself prove the hook is bounded in wall-clock time,
    because it never opens a socket: "a call made with timeout=T takes at
    most T" is FALSE for a server that trickles data (see
    test_shared_deadline_bounds_a_trickling_fetch below for the real,
    socket-based contract this constant-sum guard cannot provide).
    """
    hook = _load_hook()
    worst_case = 2 * hook.FETCH_TIMEOUT_SECONDS + hook.PUBLISH_TIMEOUT_SECONDS
    assert worst_case < 5.0, (
        f"Worst-case three-call budget ({worst_case}s) must stay under the "
        f"5s hooks.json timeout, or a slow-succeeding threads fetch followed "
        f"by a timed-out inbox fetch (publish still firing) can blow the "
        f"outer hook budget"
    )
    assert worst_case <= 4.2, (
        f"Worst case ({worst_case}s) should leave at least ~0.8s margin under "
        f"the 5s budget for interpreter startup + JSON parsing, matching the "
        f"margin the original (two-call) design already relied on"
    )


# ---------------------------------------------------------------------------
# B11: gh#3107 — the shared deadline must actually bound a TRICKLING server,
# not just a silent (non-responding) one. Methodology validated by Aleph
# (forum #430, 2026-09-25): a trickle SLOWER than the per-op cap only proves
# the cap fires, not the deadline (every read individually times out first);
# the trickle here is deliberately FASTER than FETCH_TIMEOUT_SECONDS so every
# individual read succeeds and only ACCUMULATED elapsed time can trip
# anything -- isolating the deadline-derivation logic from the per-op cap.
# ---------------------------------------------------------------------------

class _TricklingServer:
    """Raw-socket server: sends a valid 200 + Content-Length header, then
    sends ONE BYTE at a time at `gap` seconds apart, for `nbytes_before_stall`
    bytes, then STOPS SENDING ENTIRELY (holds the connection open, no more
    data, no close) -- so whichever read is in flight when the shared
    deadline is reached genuinely BLOCKS rather than succeeding quickly.
    `content_length` (>= nbytes_before_stall) is what the header advertises,
    so the client never sees EOF from the stall alone.

    This genuine-stall tail is the part that actually discriminates
    "cap every read at the REMAINING budget" from "re-check between reads
    with a FIXED cap" (Aleph's own measured distinction, forum #430): a
    steady trickle that never stalls lets the between-op check alone catch
    things quickly regardless of which cap is used (measured: ~0.05s
    overshoot either way, not discriminating) -- the fixed-cap weakness only
    shows up when a read is truly blocked and the cap's SIZE (not just its
    presence) determines how long that block lasts.

    Raw socket, not http.server.BaseHTTPRequestHandler, because a buffered
    handler's .wfile.write() sends its whole payload in one call -- there is
    no way to make it stall mid-body. Same wall this file's own #3106
    sibling test (test_forum_monitor_unreachable_edge.py's
    _MockStallingServer) hit and solved the same way -- independent
    convergence on raw sockets is itself mild evidence it's the only way.
    """

    def __init__(
        self,
        nbytes_before_stall: int,
        gap: float,
        content_length: int = 10000,
        closing: bool = False,
    ):
        self._nbytes_before_stall = nbytes_before_stall
        self._gap = gap
        self._content_length = content_length
        self._closing = closing
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(10)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            conn.recv(4096)
            close_header = "Connection: close\r\n" if self._closing else ""
            conn.sendall(
                f"HTTP/1.1 200 OK\r\n{close_header}"
                f"Content-Length: {self._content_length}\r\n\r\n".encode()
            )
            for _ in range(self._nbytes_before_stall):
                if self._stop.is_set():
                    return
                conn.sendall(b"x")
                time.sleep(self._gap)
            # Genuine stall: send nothing further, hold the connection open.
            while not self._stop.is_set():
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def test_shared_deadline_bounds_a_trickling_fetch():
    """gh#3107's real contract: _fetch_threads, driven through the shared
    deadline, must abort near HOOK_TOTAL_BUDGET_SECONDS -- even when a read
    is genuinely BLOCKED (no data arriving) right as the deadline passes.

    Server: a brief fast trickle (2 bytes at 0.02s each -- faster than
    per_op_cap, so those early reads succeed quickly and prove the deadline
    isn't merely the per-op cap firing on a slow trickle, Aleph's own first
    probe's mistake per forum #430), THEN A GENUINE STALL (no more data,
    ever). Whichever read is in flight when the deadline is reached is
    therefore truly blocked, which is the part that actually discriminates
    "cap every read at the REMAINING budget" from "re-check between reads
    with a FIXED cap": measured directly (see the mutation-control
    verification in the PR body) -- a steady trickle that never stalls lets
    a fixed-cap implementation look correct (its overshoot bounded by the
    trickle's own fast gap, not by the cap), because no read ever actually
    blocks for close to the cap's full duration. Only a genuine stall forces
    a read to block long enough for the cap's SIZE (shrunk vs. fixed) to
    matter.

    Written before the deadline mechanism existed in this file, this test
    must fail against flat-per-call-timeout code (elapsed runs until the
    test's own patience/thread teardown, unbounded by the budget) and pass
    once every read is capped at min(per_op_cap, remaining-budget).
    """
    hook = _load_hook()
    per_op_cap = 0.5
    budget = 0.25  # SMALLER than per_op_cap once the brief trickle elapses,
                   # so the stalled read's timeout MUST shrink below
                   # per_op_cap to land near budget -- the exact regime
                   # where "cap at remaining" and "fixed cap" diverge (see
                   # this test's own docstring and the PR body's mutation
                   # measurement for the numbers that justify this choice).
    server = _TricklingServer(nbytes_before_stall=2, gap=0.02)
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/api/threads"
        deadline = time.monotonic() + budget
        t0 = time.monotonic()
        result = hook._fetch_with_deadline(url, deadline, per_op_cap)
        elapsed = time.monotonic() - t0
    finally:
        server.stop()

    assert result is None, (
        f"a permanently-stalled body must never complete -- got {result!r}"
    )
    assert elapsed < budget + 0.15, (
        f"elapsed {elapsed:.2f}s overshot the {budget}s shared deadline by "
        f"more than a small jitter margin -- a per-op cap that is NOT "
        f"shrunk toward the remaining budget would instead wait its full "
        f"{per_op_cap}s cap on the read blocked at the deadline (measured: "
        f"~{budget + per_op_cap - 0.04:.2f}s for that mutation, well past "
        f"this bound), which is the exact distinction Aleph's measurement "
        f"drew between 'cap at remaining' and 're-check between ops' alone"
    )
    assert elapsed > budget - 0.1, (
        f"elapsed {elapsed:.2f}s returned suspiciously early relative to "
        f"the {budget}s budget"
    )


def test_shared_deadline_checked_between_reads_not_just_between_calls():
    """A SEPARATE property from the stall test above: with a CONTINUOUS fast
    trickle (never stalling, faster than per_op_cap) spanning far longer than
    the budget, the deadline must still be re-checked frequently enough to
    catch it -- not just once per logical fetch call.

    This is what actually discriminates HTTPResponse.read(n) (loops
    internally, making as many underlying raw reads as needed to fill n
    bytes before EVER returning to the caller -- so a deadline recheck
    between separate .read() calls never gets a chance to run while one call
    is trapped looping) from .read1(n) (at most one raw read per call,
    letting the deadline recheck fire after every individual socket op).
    Measured self-caught bug (see PR body / ob_1064): with plain .read(4096)
    against a 600-byte/0.05s-per-byte trickle and a 2.0s budget, elapsed was
    34.49s -- one .read(4096) call ran to completion (~30s) because 4096 far
    exceeds what trickles in per recv(), so the between-CALLS deadline check
    never ran until the whole body had arrived.

    The genuine-stall test above cannot catch this: with only ~2 trickled
    bytes before a permanent stall, there is only ever ONE logical read
    operation regardless of read() vs read1(), so both forms compute the
    same single settimeout() value up front. Only a long CONTINUOUS trickle
    -- many small chunks, each its own opportunity for the deadline check to
    fire between them -- exercises the difference.
    """
    hook = _load_hook()
    per_op_cap = 0.5  # trickle gap (0.05s) is FASTER than this cap, so every
                       # individual chunk arrives well within its own
                       # settimeout() -- isolating the between-reads-check
                       # property from the cap-shrinking property the other
                       # test covers.
    budget = 2.0
    server = _TricklingServer(nbytes_before_stall=600, gap=0.05, content_length=600)
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/api/threads"
        deadline = time.monotonic() + budget
        t0 = time.monotonic()
        result = hook._fetch_with_deadline(url, deadline, per_op_cap)
        elapsed = time.monotonic() - t0
    finally:
        server.stop()

    assert result is None, (
        f"a trickling body 30s long against a {budget}s budget must never "
        f"complete -- got {result!r}"
    )
    assert elapsed < budget + 0.6, (
        f"elapsed {elapsed:.2f}s should stay close to the {budget}s shared "
        f"deadline -- .read(n) (which loops internally accumulating up "
        f"to n bytes before returning) would instead run close to the "
        f"trickle's own 30s length (measured: 34.49s for that mutation), "
        f"because the between-CALLS deadline check never gets a chance to "
        f"fire while one .read(4096) call is trapped looping internally"
    )


class _ClosingResponseServer:
    """Raw-socket server: sends a valid 200 with `Connection: close` and a
    real JSON body, then closes its end of the socket immediately after the
    last byte — the actual shape a real server (measured live against this
    repo's own forum, engram-alpha#3143) uses, not a mocked keep-alive
    connection. This is the shape that makes CPython's http.client null out
    `HTTPConnection.sock` before the caller's own read loop ever runs: once
    `getresponse()`'s internal `begin()` sees `Connection: close` and a
    fully-satisfiable Content-Length, it marks the response `will_close` and
    tears down the connection's socket right there, well before the caller
    gets a chance to read the body.

    Raw socket, not http.server.BaseHTTPRequestHandler, for the same reason
    _TricklingServer above is raw: a buffered handler doesn't reproduce a
    server actually closing its end of the wire the instant it's done, which
    is exactly the condition under test.
    """

    def __init__(self, body: bytes):
        self._body = body
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(10)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            conn.recv(4096)
            header = (
                f"HTTP/1.1 200 OK\r\nConnection: close\r\n"
                f"Content-Length: {len(self._body)}\r\n\r\n"
            ).encode()
            conn.sendall(header + self._body)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def test_fetch_with_deadline_handles_a_real_closing_response(monkeypatch):
    """gh#3143: a real `Connection: close` response (measured live against
    this repo's own forum server) leaves `HTTPConnection.sock` as `None`
    immediately after `getresponse()` returns -- BEFORE `_fetch_with_deadline`'s
    read loop ever runs. The loop's unconditional `conn.sock.settimeout(...)`
    at the top of every iteration then raises `AttributeError: 'NoneType'
    object has no attribute 'settimeout'`, caught by the function's own bare
    `except Exception: return None` -- silently returning None exactly like
    any genuine failure, indistinguishable to every caller from a real
    timeout or a dead server.

    Written before the fix landed: this test must FAIL (result is None,
    where it should be the real body) against the code as shipped in #3110,
    and PASS once the settimeout() call is guarded on `conn.sock is not
    None`. See this test file's own mutation-control convention (the
    _TricklingServer tests above) -- the mutation here is simply reverting
    the guard, which the PR body verifies separately reddens exactly this
    test and no other.
    """
    hook = _load_hook()
    body = b'{"threads": [{"id": 431, "title": "gh3143 regression body"}]}'
    server = _ClosingResponseServer(body=body)
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/api/threads"
        deadline = time.monotonic() + 5.0
        result = hook._fetch_with_deadline(url, deadline, per_op_cap=2.0)
    finally:
        server.stop()

    assert result == body, (
        f"expected the real Connection:close body to come through intact, "
        f"got {result!r} -- a None here means the AttributeError on "
        f"conn.sock.settimeout(None-check-missing) was silently swallowed "
        f"by the bare except, exactly gh#3143's reported symptom"
    )


def test_fetch_with_deadline_handles_a_zero_length_closing_response(monkeypatch):
    """Narrower sibling of the test above, isolating the SPECIFIC shape that
    makes CPython null the socket synchronously inside getresponse() itself
    (measured directly: a zero-length body triggers this even before the
    read loop's first iteration, not merely after the last chunk) -- the
    most immediate trigger for the reported AttributeError, and a real shape
    a forum endpoint can return (e.g. a 200 with an empty JSON array
    serialized to zero bytes is not this repo's actual behavior, but a
    HEAD-shaped or 204-adjacent empty-body 200 is a legitimate HTTP
    response this client must not crash on).
    """
    hook = _load_hook()
    server = _ClosingResponseServer(body=b"")
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/api/threads"
        deadline = time.monotonic() + 5.0
        result = hook._fetch_with_deadline(url, deadline, per_op_cap=2.0)
    finally:
        server.stop()

    assert result == b"", (
        f"a zero-length Connection:close body is a valid empty success "
        f"response, not a failure -- got {result!r}"
    )


def test_closing_response_mid_body_stall_still_bounded_by_remaining_budget():
    """gh#3143 round 2 (Aleph's + Mira's colleague-review correction): the
    first fix guarded `conn.sock.settimeout(...)` on `conn.sock is not None`
    -- which made the two tests above pass, but is wrong in a way neither of
    them could catch, because neither has a body still arriving after
    `getresponse()` returns. On a `Connection: close` response, `conn.sock`
    goes `None` immediately (per CPython's own http.client source: this
    "effectively passes the connection to the response", not "the body is
    already fully buffered") -- the response keeps reading from the SAME
    socket object, with real network reads still ahead of it. Guarding on
    `conn.sock` skips the re-arm entirely from that point on, freezing every
    subsequent read1() at whatever timeout the connection was constructed
    with -- so a mid-body stall on a Connection:close response can overshoot
    the shared deadline by up to one full `per_op_cap`, the exact class of
    unbounded-block #3107 exists to prevent, just gated behind a different
    trigger (any Connection:close response, not merely a slow trickle).

    Measured directly against the guarded form (Aleph, PR review on
    883e5ac9): per_op_cap=1.5, budget=3.0, a fast trickle draining most of
    the budget then a genuine (indefinite) stall -- guarded elapsed 4.376s
    (overshoot +1.376s); capturing the socket before getresponse() and
    re-arming that captured reference instead: 3.003s (overshoot +0.003s).
    This test reproduces that shape at smaller, CI-friendly numbers, tuned
    (per this file's own test_shared_deadline_bounds_a_trickling_fetch
    docstring on why this matters) so the remaining budget at the moment of
    the stall is meaningfully SMALLER than per_op_cap -- otherwise a frozen
    fixed-at-construction cap and a correctly-shrinking one land close
    enough together that jitter alone can make a wrong fix look right (the
    exact trap that test's own docstring names, and that an earlier,
    looser-toleranced version of THIS test fell into: it passed under both
    the correct fix and the wrong guarded one).

    Written to redden against the guarded fix and pass once the socket is
    captured BEFORE getresponse() and that captured reference is re-armed
    unconditionally (no `is not None` check needed, since the object itself
    never becomes None -- only the `conn.sock` attribute pointing at it
    does).
    """
    hook = _load_hook()
    per_op_cap = 0.5
    budget = 1.0
    # Trickle 16 bytes at 0.05s/byte (~0.8s) -- draining budget down to
    # ~0.2s remaining, well under per_op_cap=0.5s -- THEN a genuine
    # indefinite stall (the server's built-in behavior once nbytes_before_
    # stall is exhausted: hold the connection open, send nothing further).
    # Correct fix: the between-reads recheck shrinks the next settimeout()
    # toward the ~0.2s actually remaining, aborting near budget (~1.0s
    # total). Guarded/frozen fix: the connection's timeout stays at its
    # CONSTRUCTION-time value (min(per_op_cap, budget) = 0.5s, computed
    # before any bytes were read), so the stalled read -- starting at
    # ~0.8s -- doesn't time out until ~0.8+0.5 = 1.3s, a clearly
    # distinguishable ~0.3s past this test's tolerance.
    server = _TricklingServer(nbytes_before_stall=16, gap=0.05, closing=True)
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/api/threads"
        deadline = time.monotonic() + budget
        t0 = time.monotonic()
        result = hook._fetch_with_deadline(url, deadline, per_op_cap)
        elapsed = time.monotonic() - t0
    finally:
        server.stop()

    assert result is None, (
        f"a stalled-then-abandoned Connection:close body must never "
        f"complete -- got {result!r}"
    )
    assert elapsed < budget + 0.15, (
        f"elapsed {elapsed:.2f}s overshot the {budget}s shared deadline by "
        f"more than a small jitter margin -- a settimeout() call frozen at "
        f"connection-construction time (guarded on conn.sock, which goes "
        f"None immediately on a Connection:close response) would instead "
        f"run close to the full {per_op_cap}s per-op cap regardless of how "
        f"little budget remained, which is exactly what capturing the "
        f"socket before getresponse() and re-arming THAT reference fixes"
    )


# ---------------------------------------------------------------------------
# C1: Same-second cursor precision regression (#751)
#     Cursor written at T:00.000000Z must NOT exclude a post at T:00.000500Z.
# ---------------------------------------------------------------------------

def test_same_second_post_not_excluded_by_cursor(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Same-second post is surfaced even when cursor was written in the same second.

    Regression for #751: a coarse seconds-only cursor '...T12:00:00Z' sorts
    AFTER a same-second post '...T12:00:00.500000Z' (Z=0x5A > .=0x2E), so the
    server's `created_at > since` filter excluded the post.  The fix writes
    cursors with fixed-width 6-digit microseconds so a cursor at
    '...T12:00:00.000000Z' correctly precedes '...T12:00:00.500000Z'.

    This test verifies that the `since_str` produced by the hook (which mirrors
    what the hook passes to _fetch_threads) uses the microsecond format.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    # Cursor at the start of a second (zero microseconds)
    cursor_ts = datetime(2026, 1, 1, 12, 0, 0, 0, tzinfo=timezone.utc)
    _write_cursor_file(tmp_cursor, cursor_ts)

    # Capture the `since` value that the hook passes to _fetch_threads
    captured_since = []

    def stub_fetch_threads(url, since, agent, deadline):
        captured_since.append(since)
        # Simulate a post that arrived at T:00.000500Z (same second, after cursor)
        return [{"id": "99", "title": "Same-second post"}], None

    monkeypatch.setattr(hook, "_fetch_threads", stub_fetch_threads)
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([], None))

    output = _run_main(hook, capture_output=True)

    assert captured_since, "_fetch_threads must have been called"
    since_value = captured_since[0]

    # Must use fixed-width microseconds format
    assert since_value is not None, "since_str must not be None when cursor exists"
    assert "." in since_value, (
        f"since_str must contain '.' (microseconds separator); got: {since_value!r} — "
        f"seconds-only format would lexically sort AFTER same-second posts"
    )
    assert since_value == "2026-01-01T12:00:00.000000Z", (
        f"since_str must use fixed-width 6-digit microseconds; got: {since_value!r}"
    )

    # The post must surface in the output
    assert output.strip(), "Same-second post must produce output"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "new forum post" in ctx or "new forum posts" in ctx, (
        "Same-second post must appear in thread-count line"
    )




# ---------------------------------------------------------------------------
# E) comms_surface gate (alpha#2019 slice A)
#
#   1. forum mode (explicit comms_surface="forum") -> emissions byte-identical
#      to the absent-key default (both pass through _stub_config unchanged).
#   2. umwelt mode, mention-pending -> the 📪 advisory line (shape asserted).
#   3. umwelt mode, broadcast-only (threads present, no mentions) -> no
#      emission at all (fully suppressed).
#   4. The advisory names the count and the `forum read <id>` remedy.
# ---------------------------------------------------------------------------


def _stub_config_with_comms_surface(hook, monkeypatch, comms_surface, agent_name="testbot"):
    cfg = {
        "agent_name": agent_name,
        "forum": {"url": "http://localhost:5002"},
    }
    if comms_surface is not None:
        cfg["comms_surface"] = comms_surface
    monkeypatch.setattr(hook, "_load_config", lambda: cfg)


def test_forum_mode_explicit_matches_absent_key_default(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Explicit comms_surface='forum' produces byte-identical output to the
    absent-key default -- the forum-mode code path must be untouched by the
    #2019 slice A gate."""
    mention = _make_inbox_item(tid="21", title="Forum-mode parity", author="luria")

    def _run(comms_surface):
        hook = _load_hook()
        _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
        _stub_config_with_comms_surface(hook, monkeypatch, comms_surface)
        _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(
            hook, "_fetch_threads",
            lambda url, since, agent, deadline: ([{"id": "2", "title": "new thread"}], None),
        )
        monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([mention], None))
        return _run_main(hook, capture_output=True)

    output_absent = _run(None)
    output_explicit_forum = _run("forum")

    assert output_absent.strip(), "Expected non-empty output (absent-key default)"
    assert output_explicit_forum.strip(), "Expected non-empty output (explicit forum)"
    data_absent = json.loads(output_absent.strip())
    data_explicit = json.loads(output_explicit_forum.strip())
    assert data_absent == data_explicit, (
        "explicit comms_surface='forum' must be byte-identical to the "
        "absent-key default"
    )


def test_forum_mode_parity_holds_in_silent_state(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Parity's second state (#2116 review): with nothing to report, explicit
    comms_surface='forum' and the absent-key default are both silent -- the
    parity claim covers the empty path empirically, not just by code-reading."""
    def _run(comms_surface):
        hook = _load_hook()
        _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
        _stub_config_with_comms_surface(hook, monkeypatch, comms_surface)
        _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
        monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([], None))
        return _run_main(hook, capture_output=True)

    output_absent = _run(None)
    output_explicit_forum = _run("forum")

    assert output_absent.strip() == "", "absent-key default must be silent with nothing to report"
    assert output_explicit_forum.strip() == "", "explicit forum must be silent with nothing to report"


def test_umwelt_mode_mention_pending_emits_advisory(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Under comms_surface=umwelt, a pending @mention emits the 📪 advisory
    line instead of the ordinary 🔔/📢 lines."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config_with_comms_surface(hook, monkeypatch, "umwelt")

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    mention = _make_inbox_item(tid="33", title="World-preferring seat", author="borges")

    monkeypatch.setattr(
        hook, "_fetch_threads",
        lambda url, since, agent, deadline: ([{"id": "9", "title": "some new thread"}], None),
    )
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([mention], None))

    output = _run_main(hook, capture_output=True)

    assert output.strip(), "Expected the umwelt advisory line to fire"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]

    # The ordinary 🔔/📢 lines must NOT appear under umwelt.
    assert "\U0001f514" not in ctx, "🔔 mention line must be suppressed under umwelt"
    assert "\U0001f4e2" not in ctx, "📢 threads line must be suppressed under umwelt"

    # The 📪 advisory line must appear, naming the count and the remedy.
    assert "\U0001f4ea" in ctx, "📪 advisory line must appear under umwelt"
    assert "comms_surface=umwelt" in ctx
    assert "1 addressed forum item" in ctx, "Advisory must name the pending count"
    assert "forum read <id>" in ctx, "Advisory must name the `forum read <id>` remedy"
    assert "not yet live" in ctx


def test_umwelt_mode_broadcast_only_no_emission(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Under comms_surface=umwelt, a broadcast-only new-thread count (no
    pending mention) produces NO emission at all -- fully suppressed."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config_with_comms_surface(hook, monkeypatch, "umwelt")

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(
        hook, "_fetch_threads",
        lambda url, since, agent, deadline: ([{"id": "9", "title": "broadcast-only thread"}], None),
    )
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: ([], None))

    output = _run_main(hook, capture_output=True)

    assert output.strip() == "", (
        "Broadcast-only counts must be fully suppressed under comms_surface=umwelt"
    )


def test_umwelt_mode_advisory_pluralizes_count(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """The advisory line's count noun pluralizes correctly for N>1."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config_with_comms_surface(hook, monkeypatch, "umwelt")

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    mentions = [
        _make_inbox_item(tid="1", title="First", author="a"),
        _make_inbox_item(tid="2", title="Second", author="b"),
    ]

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent, deadline: ([], None))
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name, deadline: (mentions, None))

    output = _run_main(hook, capture_output=True)

    assert output.strip(), "Expected the umwelt advisory line to fire"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "2 addressed forum items" in ctx


# ---------------------------------------------------------------------------
# D) Flattened-topology regression (#1540)
# ---------------------------------------------------------------------------

import os
import subprocess
import sys


def test_tools_resolution_in_flattened_deployed_topology(tmp_path):
    """Regression — parity with #1539's walk-all-parents fix for the baton hook.

    The plugin build FLATTENS hooks/claude/ -> hooks/, so the deployed forum
    hook sits one level shallower than source. The old ``parents[2]/tools``
    fallback would overshoot in this topology (CPR-unset case). Walk-all-parents
    is robust to both depths.

    Updated for gh#1657 slice 3: the hook now resolves tools/ via the shared
    hooks/claude/_hooklib.py (extracted in slice 1, already re-pointed for
    baton/inter-agent in slices 1-2) rather than its own inline walk-parents
    copy, so _hooklib.py must also be present as a sibling in the recreated
    flattened layout for the import to succeed.

    Updated again for gh#1680 slice 1: config/agent-name resolution also now
    delegates to the shared _prompthooklib module (same directory), so that
    must be present as a sibling too. Extended assertions cover (a) tools/
    resolution (unchanged), (b) config loads, (c) agent name resolves --
    this hook has no local _emit_context to check (see _prompthooklib.py's
    module docstring for why that helper was NOT extracted into forum's
    call site).

    Updated again for engram-alpha#2863 WS2: this hook now imports
    `_hooklib.emit_context` and `_harness.{detect_harness,get_harness_budgets}`
    unconditionally at module load (see the hook's own prologue comment), so
    `_harness.py` must also be present as a sibling in the recreated
    flattened layout or the whole module import raises ModuleNotFoundError
    before any of this test's probes even run (same incidental-regression
    class as engram-surface-hook.py's own #2876 fix to
    tests/test_hook_emitter_path_resolution.py's `_SHARED_SIBLINGS`).

    Recreate the deployed layout (hooks/<name>.py with a SIBLING tools/ AND
    SIBLING _hooklib.py / _prompthooklib.py / _harness.py) with
    CLAUDE_PLUGIN_ROOT unset and assert that _status_derive resolves: a
    resolved import leaves derive_own_status as a callable; a broken import
    leaves it None.
    """
    root = tmp_path / "engram"
    (root / "hooks").mkdir(parents=True)
    (root / "tools").mkdir()
    hook_src = HOOK_PATH
    hooklib_src = (
        Path(__file__).parent.parent
        / "src" / "engram" / "hooks" / "claude" / "_hooklib.py"
    )
    prompthooklib_src = (
        Path(__file__).parent.parent
        / "src" / "engram" / "hooks" / "claude" / "_prompthooklib.py"
    )
    harness_src = (
        Path(__file__).parent.parent
        / "src" / "engram" / "hooks" / "claude" / "_harness.py"
    )
    (root / "hooks" / "engram-forum-prompt-hook.py").write_text(hook_src.read_text())
    (root / "hooks" / "_hooklib.py").write_text(hooklib_src.read_text())
    (root / "hooks" / "_prompthooklib.py").write_text(prompthooklib_src.read_text())
    (root / "hooks" / "_harness.py").write_text(harness_src.read_text())
    # Stub _status_derive.py so the import can succeed.
    (root / "tools" / "_status_derive.py").write_text(
        "def derive_own_status(agent_name): return None, None, None, None\n"
        "def _read_loop_mode(): return None\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agent_name": "luria"}))

    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = str(home)
    probe_script = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('hook', "
        f"  r'{root}/hooks/engram-forum-prompt-hook.py')\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "print('resolved' if mod.derive_own_status is not None else 'broken')\n"
        "cfg = mod._load_config()\n"
        "print('config_ok' if cfg.get('agent_name') == 'luria' else f'config_broken:{cfg!r}')\n"
        "name = mod._get_agent_name(cfg)\n"
        "print('name_ok' if name == 'luria' else f'name_broken:{name!r}')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe_script],
        capture_output=True, text=True, env=env,
    )
    assert "resolved" in r.stdout, (
        "deployed-topology hook failed to resolve _status_derive via walk-parents "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "config_ok" in r.stdout, (
        f"deployed-topology hook failed to load config via _prompthooklib "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "name_ok" in r.stdout, (
        f"deployed-topology hook failed to resolve agent name via _prompthooklib "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
