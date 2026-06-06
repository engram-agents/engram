"""Tests for hooks/claude/engram-forum-prompt-hook.py.

Covers two sets of scenarios:

A) Surfaced-cursor advance semantics (original tests):
   1. Cache hit  -> cursor UNCHANGED.
   2. Live fetch success -> cursor ADVANCED to ~now.
   3. Unreachable, no cache -> cursor UNCHANGED.

B) Mention-line injection (new, spec §6):
   1. Mention line present when endpoint returns >=1 mention.
   2. Mention line ABSENT when endpoint returns None (old server / unreachable),
      generic-thread line unaffected.
   3. Mentions fetch is called with the READ cursor as `since`, NOT the surfaced
      cursor (the load-bearing §2 assertion).
   4. Persistence: same mention surfaces on consecutive prompts when read cursor
      has NOT advanced.
   5. Cache stores both threads + mentions; fresh-cache hit reads both without
      a second server call; OLD cache file (no `mentions` key) -> no KeyError.
   6. Both-lines ordering: mention line ABOVE generic threads line.
   7. Empty agent_name -> no mentions fetch; generic thread line still works.
   8. 404 mentions endpoint (old server) -> no mention line; generic unchanged.

Each test loads the hook as a module via importlib, monkeypatches
SURFACED_CURSOR_PATH, READ_CURSOR_PATH, and HOOK_CACHE_PATH to tmp paths,
and stubs _load_config / _fetch_threads / _fetch_mentions as needed.
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
    / "hooks" / "claude" / "engram-forum-prompt-hook.py"
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


def _make_mention(tid: str = "42", title: str = "Test Thread",
                  author: str = "luria", kind: str = "at_mention") -> dict:
    """Build a minimal mention dict matching the API schema."""
    return {
        "thread_id": tid,
        "thread_title": title,
        "author": author,
        "kind": kind,
    }


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
    """Point the hook module at tmp paths."""
    monkeypatch.setattr(hook, "SURFACED_CURSOR_PATH", str(tmp_cursor))
    monkeypatch.setattr(hook, "HOOK_CACHE_PATH", str(tmp_cache))
    if tmp_read_cursor is not None:
        monkeypatch.setattr(hook, "READ_CURSOR_PATH", str(tmp_read_cursor))


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
    """🔔 mention line appears in output when the mentions endpoint returns >=1."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    _write_cursor_file(tmp_read_cursor, datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc))

    mention = _make_mention(tid="99", title="Review my PR", author="luria")

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_mentions",
                        lambda url, name, since: [mention])

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
    """When _fetch_mentions returns None, no mention line; threads line unchanged."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent: [{"id": "1", "title": "hello"}])
    monkeypatch.setattr(hook, "_fetch_mentions", lambda url, name, since: None)

    output = _run_main(hook, capture_output=True)

    assert output.strip(), "Expected output (threads line) when threads present"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]

    # Generic threads line must be present
    assert "\U0001f4e2" in ctx, "Generic threads line (📢) must appear"
    # Mention line must NOT be present
    assert "\U0001f514" not in ctx, (
        "Mention line (🔔) must NOT appear when _fetch_mentions returns None"
    )


# ---------------------------------------------------------------------------
# B3: READ cursor is the `since` for mentions (load-bearing §2 assertion)
# ---------------------------------------------------------------------------

def test_mentions_since_is_read_cursor_not_surfaced_cursor(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """_fetch_mentions must be called with the READ cursor as `since`, not surfaced.

    This is the core §2 invariant: mentions persist every prompt until the
    agent acts on them (reads/acks), which advances the read cursor. The
    surfaced cursor advances on every hook fire and would clear the mention
    line after one prompt — wrong behavior.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    # Surfaced cursor and read cursor are DIFFERENT timestamps
    surfaced_ts = datetime(2026, 1, 2, 15, 0, 0, tzinfo=timezone.utc)
    read_ts = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    expected_read_since = read_ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    _write_cursor_file(tmp_cursor, surfaced_ts)
    _write_cursor_file(tmp_read_cursor, read_ts)

    calls = []

    def stub_fetch_mentions(url, name, since):
        calls.append(since)
        return []

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_mentions", stub_fetch_mentions)

    _run_main(hook)

    assert calls, "_fetch_mentions must have been called"
    actual_since = calls[0]
    assert actual_since == expected_read_since, (
        f"_fetch_mentions `since` must be the READ cursor ({expected_read_since!r}), "
        f"not the surfaced cursor ({surfaced_ts.strftime('%Y-%m-%dT%H:%M:%S.%fZ')!r}). "
        f"Got: {actual_since!r}"
    )


# ---------------------------------------------------------------------------
# B4: Persistence — same mention surfaces on consecutive prompts when
#     read cursor has NOT advanced
# ---------------------------------------------------------------------------

def test_mentions_persist_when_read_cursor_unchanged(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Mention line appears on both prompt-1 and prompt-2 when read cursor is static.

    The "stays in your court" property: a mention is a standing obligation
    until the agent reads the thread (which advances the read cursor).
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    read_ts = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    _write_cursor_file(tmp_read_cursor, read_ts)
    _write_cursor_file(tmp_cursor, datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc))

    mention = _make_mention(tid="7", title="Luria needs you", author="luria")

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])

    def fetch_mentions(url, name, since):
        # returns the same mention regardless of since — simulates a server
        # where the mention hasn't been read yet
        return [mention]

    monkeypatch.setattr(hook, "_fetch_mentions", fetch_mentions)

    # Prompt 1
    output1 = _run_main(hook, capture_output=True)
    # Expire the cache so prompt 2 does a live fetch again
    if tmp_cache.exists():
        data = json.loads(tmp_cache.read_text())
        data["fetched_at"] = time.time() - 60  # force expired
        tmp_cache.write_text(json.dumps(data))

    # Advance surfaced cursor (hook does this on live fetch) but NOT read cursor
    _write_cursor_file(tmp_cursor, datetime(2026, 1, 2, 1, 0, 0, tzinfo=timezone.utc))

    # Prompt 2
    output2 = _run_main(hook, capture_output=True)

    for prompt_n, output in enumerate([output1, output2], start=1):
        assert output.strip(), f"Prompt {prompt_n}: expected non-empty output"
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f514" in ctx, (
            f"Prompt {prompt_n}: mention line must persist when read cursor unchanged"
        )


# ---------------------------------------------------------------------------
# B5a: Cache stores both threads + mentions; fresh-cache prompt reads both
#      without a second server hit
# ---------------------------------------------------------------------------

def test_cache_stores_and_reads_both_threads_and_mentions(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """A fresh-cache prompt reads both threads+mentions without calling _fetch_mentions."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    _write_cursor_file(tmp_read_cursor, datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc))

    mention = _make_mention(tid="55", title="Cached mention", author="luria")
    # Write a cache file that already contains mentions
    _write_fresh_cache(tmp_cache, threads=[], mentions=[mention])

    fetch_mentions_calls = []

    def stub_fetch_mentions(url, name, since):
        fetch_mentions_calls.append((url, name, since))
        return []

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_mentions", stub_fetch_mentions)

    output = _run_main(hook, capture_output=True)

    # _fetch_mentions must NOT have been called (cache was fresh)
    assert fetch_mentions_calls == [], (
        "_fetch_mentions must not be called on a fresh-cache hit"
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
    _write_cursor_file(tmp_read_cursor, datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc))

    mention = _make_mention(tid="77", title="Stale but present", author="luria")

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
    _write_cursor_file(tmp_read_cursor, datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc))

    mention = _make_mention(tid="10", title="Needs review", author="luria")

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent: [{"id": "2", "title": "new thread"}])
    monkeypatch.setattr(hook, "_fetch_mentions",
                        lambda url, name, since: [mention])

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
    """When agent_name is empty, _fetch_mentions is never called; threads work."""
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)

    # Config that resolves to empty agent_name (no agent_name, no USER/LOGNAME env)
    monkeypatch.setattr(hook, "_load_config", lambda: {
        "forum": {"url": "http://localhost:5002"},
    })
    # Override _get_agent_name to return empty string
    monkeypatch.setattr(hook, "_get_agent_name", lambda config: "")

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    fetch_mentions_calls = []

    def stub_fetch_mentions(url, name, since):
        fetch_mentions_calls.append(name)
        return [_make_mention()]

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent: [{"id": "3", "title": "thread"}])
    monkeypatch.setattr(hook, "_fetch_mentions", stub_fetch_mentions)

    output = _run_main(hook, capture_output=True)

    assert fetch_mentions_calls == [], (
        "_fetch_mentions must NOT be called when agent_name is empty"
    )
    # Threads line must still be present
    assert output.strip(), "Expected threads output even with empty agent_name"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "\U0001f4e2" in ctx, "Generic threads line must work with empty agent_name"
    assert "\U0001f514" not in ctx, "No mention line when agent_name is empty"


# ---------------------------------------------------------------------------
# B8: 404 mentions endpoint (old server) -> silent skip; generic unchanged
#     (This is the "safe to merge before every install has #662's server" test)
#
#     Two layers:
#     (a) _fetch_mentions itself swallows a 404 HTTPError -> returns None.
#     (b) main() receiving None from _fetch_mentions -> no mention line,
#         generic thread line still works.
# ---------------------------------------------------------------------------

def test_fetch_mentions_swallows_404(monkeypatch):
    """_fetch_mentions must return None (not raise) when the server returns 404.

    This is the low-level contract: any HTTP error including 404 is swallowed
    so that a missing /api/agent/<name>/mentions endpoint on an old server is
    indistinguishable from "no mentions" from the caller's perspective.
    """
    import urllib.error
    import urllib.request

    hook = _load_hook()

    # Patch urlopen to raise HTTPError 404
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, None
        )

    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)

    result = hook._fetch_mentions("http://localhost:5002", "testbot", None)
    assert result is None, (
        "_fetch_mentions must return None on HTTPError 404 (old server)"
    )


def test_old_server_404_mentions_silent_skip_in_main(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """A 404 from the mentions endpoint results in no mention line; generic unchanged.

    Validates the PR's safety property: a hook hitting an old server
    (without #662's /api/agent/<name>/mentions endpoint) silently skips the
    mention line — it does NOT break the hook chain or suppress the threads line.
    Uses _fetch_mentions returning None (as it would after swallowing a 404).
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    _write_cursor_file(tmp_read_cursor, datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(hook, "_fetch_threads",
                        lambda url, since, agent: [{"id": "5", "title": "new thread"}])
    # Simulate what _fetch_mentions returns after swallowing a 404
    monkeypatch.setattr(hook, "_fetch_mentions", lambda url, name, since: None)

    output = _run_main(hook, capture_output=True)

    assert output.strip(), "Expected threads output even when mentions endpoint is 404"
    data = json.loads(output.strip())
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "\U0001f4e2" in ctx, "Generic threads line must work when mentions 404s"
    assert "\U0001f514" not in ctx, (
        "Mention line must be absent when mentions endpoint returns None (old server)"
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
    monkeypatch.setattr(hook, "_fetch_mentions", lambda url, name, since: [])

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


# ===========================================================================
# Part D — Mention cursor precision regression (#847)
# ===========================================================================
#
# Root cause: the forum CLI writes forum-read-cursor.txt at SECOND precision
# (e.g. "2026-06-05T16:52:54Z").  Mention created_at values carry MICROSECONDS
# (e.g. "…54.153797Z").  The old hook line:
#
#   read_cursor_str = read_cursor.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
#
# turned "…54Z" into "…54.000000Z".  String-compare on the server:
#   "…54.153797Z" > "…54.000000Z"  →  mention ranks ABOVE the cursor →
#   never clears, surfaces every prompt forever.
#
# The raw file form compares correctly: '.' (0x2E) < 'Z' (0x5A), so
# "…54Z" (no dot) sorts AFTER "…54.153797Z" (has dot), meaning the server
# correctly excludes the already-seen mention.
#
# Fix: pass the cursor string verbatim from the file; _read_cursor() serves as
# the validity gate only.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# D1 — The incident pin (regression discriminator)
#      Cursor file contains "2026-06-05T16:52:54Z" (second precision, no dot).
#      The `since` param reaching the mentions fetch MUST be byte-identical
#      to that raw string — NOT "2026-06-05T16:52:54.000000Z".
#
#      This test MUST fail against the pre-fix code (line 326 re-serialises via
#      strftime which adds ".000000") and MUST pass with the verbatim-pass fix.
# ---------------------------------------------------------------------------

def test_mention_since_cursor_passed_verbatim_second_precision(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Cursor file at second precision must reach _fetch_mentions byte-identical.

    The forum CLI writes forum-read-cursor.txt at second precision, e.g.
    "2026-06-05T16:52:54Z" (no microseconds, no dot). The hook's old
    parse→re-serialize round-trip turned this into "2026-06-05T16:52:54.000000Z",
    which compares BEFORE a mention at "…54.153797Z" on the server — so the
    mention never cleared. The fix reads the file verbatim. This test is the
    regression discriminator: it is RED against the pre-fix strftime line and
    GREEN after the verbatim-pass fix. Any re-introduction of format
    normalisation in this code path will make it red again. (issue #847)
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    # Write cursor at SECOND precision — exactly as the forum CLI does
    RAW_CURSOR = "2026-06-05T16:52:54Z"
    tmp_read_cursor.write_text(RAW_CURSOR + "\n")

    # Give the surfaced cursor a different, valid value so the hook runs
    _write_cursor_file(tmp_cursor, datetime(2026, 6, 5, 16, 52, 54, 0, tzinfo=timezone.utc))

    captured_since = []

    def stub_fetch_mentions(url, name, since):
        captured_since.append(since)
        return []

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_mentions", stub_fetch_mentions)

    _run_main(hook)

    assert captured_since, "_fetch_mentions must have been called"
    actual = captured_since[0]
    assert actual == RAW_CURSOR, (
        f"since= must be the raw file string {RAW_CURSOR!r} byte-identical; "
        f"got {actual!r}. If this is '2026-06-05T16:52:54.000000Z', the "
        f"parse→re-serialize round-trip has been re-introduced and #847 is back."
    )


# ---------------------------------------------------------------------------
# D2 — End-to-end shape: a mention at "…54.153797Z" does NOT surface when
#      the cursor file contains "…54Z" (second precision).
#
#      The server's `created_at > since` string comparison is simulated in the
#      stub: the mention's created_at "…54.153797Z" is NOT strictly greater than
#      the raw cursor "…54Z" ('.', 0x2E < 'Z', 0x5A), so the stub returns [].
#      With the verbatim fix the hook passes "…54Z" and the stub correctly
#      filters the mention out. With the old normalised "…54.000000Z" the stub
#      would return the mention (since "…54.153797Z" > "…54.000000Z").
# ---------------------------------------------------------------------------

def test_mention_at_same_second_does_not_surface_with_verbatim_cursor(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """A mention whose created_at matches the cursor second must not resurface.

    Simulates the server-side string comparison `created_at > since`:
    - mention created_at: "2026-06-05T16:52:54.153797Z"
    - cursor file (second precision): "2026-06-05T16:52:54Z"

    Raw comparison: "2026-06-05T16:52:54.153797Z" > "2026-06-05T16:52:54Z"
    is FALSE because '.' (0x2E) < 'Z' (0x5A) — the already-seen mention
    is correctly excluded when the cursor is passed verbatim.

    With the old normalised cursor "…54.000000Z", the same mention would have
    been included (since "…54.153797Z" > "…54.000000Z") and surfaced forever.

    Note: this test simulates the server-side filter in the stub because the
    test harness does not host a real forum server. The stub faithfully
    replicates the `created_at > since` string comparison the real server uses,
    which is the exact comparison documented in the issue #847 live-endpoint
    proof.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    RAW_CURSOR = "2026-06-05T16:52:54Z"
    MENTION_CREATED_AT = "2026-06-05T16:52:54.153797Z"
    tmp_read_cursor.write_text(RAW_CURSOR + "\n")
    _write_cursor_file(tmp_cursor, datetime(2026, 6, 5, 16, 52, 55, 0, tzinfo=timezone.utc))

    mention = {
        "thread_id": "100",
        "thread_title": "The incident thread",
        "author": "borges",
        "kind": "at_mention",
        "created_at": MENTION_CREATED_AT,
    }

    def stub_fetch_mentions_with_filter(url, name, since):
        # Replicate the server's `created_at > since` string comparison.
        # '.' (0x2E) < 'Z' (0x5A), so "…54.153797Z" is NOT > "…54Z" →
        # mention is excluded when `since` is the raw verbatim cursor.
        # If `since` were the normalised "…54.000000Z" the mention WOULD
        # pass (since "…54.153797Z" > "…54.000000Z").
        return [m for m in [mention] if m["created_at"] > since]

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_mentions", stub_fetch_mentions_with_filter)

    output = _run_main(hook, capture_output=True)

    # When the fix is correct, no mention surfaces (the filter returns []).
    # The hook also has no threads, so output is empty (sys.exit(0)).
    # An output with a 🔔 line means the mention leaked through — #847 is back.
    if output.strip():
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f514" not in ctx, (
            f"Mention at {MENTION_CREATED_AT!r} must NOT surface when cursor is "
            f"{RAW_CURSOR!r} (verbatim); if it does, the round-trip normalisation "
            f"re-introduced #847. since= was probably '2026-06-05T16:52:54.000000Z' "
            f"(re-serialised) rather than the raw '2026-06-05T16:52:54Z'."
        )


# ---------------------------------------------------------------------------
# D3 — Cursor file absent -> no `since` param (existing behaviour preserved)
# ---------------------------------------------------------------------------

def test_mention_since_absent_when_cursor_file_missing(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """When forum-read-cursor.txt is absent, `since` is None (no param sent).

    Ensures the validity-gate contract is unchanged: a missing cursor file
    means no since= constraint, so the mentions fetch returns all unread mentions.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    # Surfaced cursor present; read cursor file deliberately absent
    _write_cursor_file(tmp_cursor, datetime(2026, 6, 5, 16, 0, 0, 0, tzinfo=timezone.utc))
    assert not tmp_read_cursor.exists(), "Read cursor file must not exist for this test"

    captured_since = []

    def stub_fetch_mentions(url, name, since):
        captured_since.append(since)
        return []

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_mentions", stub_fetch_mentions)

    _run_main(hook)

    assert captured_since, "_fetch_mentions must have been called"
    assert captured_since[0] is None, (
        f"since= must be None when cursor file is absent; got {captured_since[0]!r}"
    )


# ---------------------------------------------------------------------------
# D4 — Cursor file with invalid ISO -> treated as absent; no `since` param
#      (validity gate retained — _read_cursor returns None for invalid content)
# ---------------------------------------------------------------------------

def test_mention_since_absent_when_cursor_file_invalid(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """Invalid cursor file is treated as absent: `since` is None, validity gate retained.

    The _read_cursor() validity check must still reject malformed content.
    A broken cursor file must not propagate garbage to the server.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    _write_cursor_file(tmp_cursor, datetime(2026, 6, 5, 16, 0, 0, 0, tzinfo=timezone.utc))
    # Write an invalid timestamp to the read cursor file
    tmp_read_cursor.write_text("not-a-valid-iso-timestamp\n")

    captured_since = []

    def stub_fetch_mentions(url, name, since):
        captured_since.append(since)
        return []

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_mentions", stub_fetch_mentions)

    _run_main(hook)

    assert captured_since, "_fetch_mentions must have been called"
    assert captured_since[0] is None, (
        f"since= must be None when cursor file is invalid; got {captured_since[0]!r}. "
        f"The validity gate (_read_cursor returning None) must still prevent garbage "
        f"from reaching the server."
    )


# ---------------------------------------------------------------------------
# Known-limitation pin (#847 follow-up layer, accepted trade — colleague round)
# ---------------------------------------------------------------------------


def test_known_limitation_newer_same_second_mention_also_skipped(
    tmp_cursor, tmp_read_cursor, tmp_cache, monkeypatch
):
    """KNOWN-LIMITATION PIN (accepted trade, colleague round 2026-06-05): a NEW
    mention created in the SAME second as the cursor but AFTER the read
    (cursor "…54Z", mention "…54.800000Z") ALSO compares below the verbatim
    cursor ('.' < 'Z') and is skipped by the hook — degraded-not-lost: the
    per-wake forum browse/status sweep still surfaces it. The loud-stuck
    failure #847 fixed was strictly worse than this silent one-second window.

    This pins the accepted behavior so any change to it is deliberate. It
    should flip exactly when the substrate-side single-precision timestamp
    normalization (#847's follow-up layer) lands — at that point this test
    goes red: delete it and close the follow-up.
    """
    hook = _load_hook()
    _patch_hook_paths(hook, monkeypatch, tmp_cursor, tmp_cache, tmp_read_cursor)
    _stub_config(hook, monkeypatch)

    RAW_CURSOR = "2026-06-05T16:52:54Z"
    NEWER_SAME_SECOND = "2026-06-05T16:52:54.800000Z"  # AFTER the read, same second
    tmp_read_cursor.write_text(RAW_CURSOR + "\n")
    _write_cursor_file(tmp_cursor, datetime(2026, 6, 5, 16, 52, 55, 0, tzinfo=timezone.utc))

    mention = {
        "thread_id": "101",
        "thread_title": "Arrived just after the read",
        "author": "ariadne",
        "kind": "at_mention",
        "created_at": NEWER_SAME_SECOND,
    }

    def stub_fetch_mentions_with_filter(url, name, since):
        # Same server-side string comparison as the incident tests.
        return [m for m in [mention] if m["created_at"] > since]

    monkeypatch.setattr(hook, "_fetch_threads", lambda url, since, agent: [])
    monkeypatch.setattr(hook, "_fetch_mentions", stub_fetch_mentions_with_filter)

    output = _run_main(hook, capture_output=True)

    # ACCEPTED-LIMITATION assertion: the genuinely-new same-second mention is
    # currently skipped (filter excludes it under the verbatim cursor).
    if output.strip():
        data = json.loads(output.strip())
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "\U0001f514" not in ctx, (
            "The newer-same-second mention surfaced — the accepted limitation "
            "has been lifted (substrate precision normalization landed?). "
            "Deliberate change required: delete this pin and close #847's "
            "follow-up layer."
        )
