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
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])

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
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: None)

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

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_inbox",
                        lambda url, name: [mention])

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
                        lambda url, since, agent: [{"id": "1", "title": "hello"}])
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name: None)

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

    def fetch_inbox_dynamic(url, name):
        return list(inbox_items)

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
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
    monkeypatch.setattr(hook2, "_fetch_threads", lambda url, since, agent: [])
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

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])

    def fetch_inbox(url, name):
        # Returns the same mention regardless — simulates a server where the
        # thread has not been marked read yet.
        return [mention]

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

    def stub_fetch_inbox(url, name):
        fetch_inbox_calls.append((url, name))
        return []

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
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
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: None)

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
                        lambda url, since, agent: [{"id": "2", "title": "new thread"}])
    monkeypatch.setattr(hook, "_fetch_inbox",
                        lambda url, name: [mention])

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

    def stub_fetch_inbox(url, name):
        fetch_inbox_calls.append(name)
        return [_make_inbox_item()]

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent: [{"id": "3", "title": "thread"}])
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
    """_fetch_inbox must return None (not raise) when the server returns 404.

    This is the low-level contract: any HTTP error including 404 is swallowed
    so that a missing /api/agent/<name>/inbox endpoint on an old server is
    indistinguishable from "no mentions" from the caller's perspective.
    """
    hook = _load_hook()

    # Patch urlopen to raise HTTPError 404
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, None
        )

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)

    result = hook._fetch_inbox("http://localhost:5002", "testbot")
    assert result is None, (
        "_fetch_inbox must return None on HTTPError 404 (old server)"
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
                        lambda url, since, agent: [{"id": "5", "title": "new thread"}])
    # Simulate what _fetch_inbox returns after swallowing a 404
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name: None)

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

    class FakeResponse:
        def read(self):
            return json.dumps(inbox_response).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_urlopen(req, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)

    result = hook._fetch_inbox("http://localhost:5002", "testbot")
    assert result is not None, "_fetch_inbox must succeed"
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
    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name: [])

    output = _run_main(hook, capture_output=True)

    # No at_mention items → no banner, no output (exit 0)
    if output.strip():
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f514" not in ctx, (
            "reply_on_my_thread items must NOT appear in the 🔔 mention banner"
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

    def stub_fetch_threads(url, since, agent):
        captured_since.append(since)
        # Simulate a post that arrived at T:00.000500Z (same second, after cursor)
        return [{"id": "99", "title": "Same-second post"}]

    monkeypatch.setattr(hook, "_fetch_threads", stub_fetch_threads)
    monkeypatch.setattr(hook, "_fetch_inbox", lambda url, name: [])

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

    Recreate the deployed layout (hooks/<name>.py with a SIBLING tools/ AND
    SIBLING _hooklib.py / _prompthooklib.py) with CLAUDE_PLUGIN_ROOT unset
    and assert that _status_derive resolves: a resolved import leaves
    derive_own_status as a callable; a broken import leaves it None.
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
    (root / "hooks" / "engram-forum-prompt-hook.py").write_text(hook_src.read_text())
    (root / "hooks" / "_hooklib.py").write_text(hooklib_src.read_text())
    (root / "hooks" / "_prompthooklib.py").write_text(prompthooklib_src.read_text())
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
