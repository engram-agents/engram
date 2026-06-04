"""Tests for hooks/claude/engram-utility-credit-mention-stop.py.

Covers the prose-mention credit-assignment hook (alpha #177 area 4):
  - bump_utility(): Q-update formula correctness, missing IDs skipped
  - credit_mentions(): end-to-end pipeline against tmp db (no recall_window)
  - collect_turn_text(): JSONL-walk correctness

The recall_window intersection was removed in PR #214 (tautology argument:
post-cutover node IDs always originate from substrate context). Tests for
get_current_turn and recall_window_active_ids have been removed along with
those functions.

All tests are self-contained: they build minimal in-memory or tmp_path
sqlite schemas. Production knowledge.db is NEVER touched.

ENGRAM_HOME isolation (#792)
----------------------------
The hook module freezes ENGRAM_HOME-derived paths (KNOWLEDGE_DB, LOG_PATH,
and sys.path insertion) at import time. Without env isolation, every test run
that exercises main() appended to the real ~/.engram/utility-credit-mention.log.

Fix: the `isolated_engram_home` autouse fixture sets ENGRAM_HOME to a per-test
tmp_path and re-executes the hook module via its saved spec so ALL module-frozen
paths re-derive from the tmp directory. Teardown restores the original env and
re-executes again, leaving the module in its pre-test state for any non-pytest
code sharing the process.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Make the alpha repo root importable so the hook can pick up engram_ids.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Load the hook module from its hooks/claude/ path (not normally importable).
HOOK_PATH = REPO_ROOT / "hooks" / "claude" / "engram-utility-credit-mention-stop.py"

# The real ENGRAM_HOME — used only to verify the real log is NOT written.
_REAL_ENGRAM_HOME = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")


def _load_hook():
    spec = importlib.util.spec_from_file_location("engram_utility_credit_mention_stop", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["engram_utility_credit_mention_stop"] = module
    spec.loader.exec_module(module)
    return module, spec


hook, _HOOK_SPEC = _load_hook()


def _reload_hook() -> None:
    """Re-execute the hook module in place using the saved file spec.

    importlib.reload() fails for modules loaded via spec_from_file_location
    with a non-importable path (Python's _find_spec can't locate the module
    by name in sys.path). Re-executing via the saved spec achieves the same
    effect: all module-level globals (ENGRAM_HOME, KNOWLEDGE_DB, LOG_PATH,
    and the sys.path insertion) are re-evaluated from the current environment.
    The existing module object is mutated in place so callers holding a
    reference to `hook` see the updated values immediately.
    """
    _HOOK_SPEC.loader.exec_module(hook)


# --------------------------------------------------------------------------- #
# ENGRAM_HOME isolation fixture (#792)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def isolated_engram_home(tmp_path):
    """Isolate the full ENGRAM_HOME surface for every test.

    Sets ENGRAM_HOME to tmp_path and reloads the hook module so that all
    module-frozen paths (ENGRAM_HOME, KNOWLEDGE_DB, LOG_PATH, and the
    sys.path insertion) re-derive from the tmp directory rather than the
    real ~/.engram. Restores the original env and re-reloads at teardown.

    This fixture is autouse=True (function scope) so every test in this file
    runs in a hermetic sandbox with no production-path leakage.
    """
    # --- setup ---
    orig_env_value = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(tmp_path)
    _reload_hook()

    yield

    # --- teardown: restore original state ---
    if orig_env_value is None:
        os.environ.pop("ENGRAM_HOME", None)
    else:
        os.environ["ENGRAM_HOME"] = orig_env_value
    _reload_hook()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def tmp_db(tmp_path) -> str:
    """In-memory-style sqlite db with nodes table matching production schema slice."""
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, utility_score REAL)")
    conn.executemany(
        "INSERT INTO nodes (id, utility_score) VALUES (?, ?)",
        [
            ("ob_1234", 0.0),
            ("dv_0567", 0.5),
            ("ax_0001", 0.9),
            ("ob_9999", None),  # explicit NULL to test the `or 0.0` fallback
        ],
    )
    conn.commit()
    conn.close()
    return str(db_path)


# --------------------------------------------------------------------------- #
# Hermeticity proof — module paths must not point at the real ~/.engram (#792)
# --------------------------------------------------------------------------- #


def test_module_paths_hermetic():
    """After the isolated_engram_home fixture, NONE of the module-frozen paths
    should point under the real ENGRAM_HOME (~/.engram).

    Checks: hook.ENGRAM_HOME, hook.KNOWLEDGE_DB, hook.LOG_PATH.
    Also verifies that the fixture's tmp_path is the new ENGRAM_HOME.
    """
    real_home = _REAL_ENGRAM_HOME
    for attr in ("ENGRAM_HOME", "KNOWLEDGE_DB", "LOG_PATH"):
        val = getattr(hook, attr)
        assert not str(val).startswith(real_home), (
            f"hook.{attr}={val!r} still points under real ENGRAM_HOME={real_home!r} "
            f"after isolated_engram_home fixture. The reload-based isolation failed."
        )
    # The new ENGRAM_HOME must be the tmp_path injected by the fixture.
    assert hook.ENGRAM_HOME == os.environ["ENGRAM_HOME"], (
        f"hook.ENGRAM_HOME={hook.ENGRAM_HOME!r} does not match "
        f"os.environ['ENGRAM_HOME']={os.environ['ENGRAM_HOME']!r}"
    )


def test_hermeticity_real_log_not_polluted(monkeypatch, tmp_path):
    """Meta-test: running main() does NOT write to the real ENGRAM_HOME log.

    Records the real log's size (or absence) before calling main(), then
    calls main() with a synthetic Stop-hook payload that will trigger the log
    write path (mentioned_count > 0), then asserts the real log is byte-identical.

    After the isolated_engram_home fixture, hook.LOG_PATH and the _log()
    default argument both point to tmp_path, so any log write goes there
    rather than to the real ~/.engram/utility-credit-mention.log.
    """
    real_log = Path(_REAL_ENGRAM_HOME) / "utility-credit-mention.log"
    before_size = real_log.stat().st_size if real_log.exists() else -1

    # Build a tmp knowledge.db with ob_0001 so main() finds something to bump.
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, utility_score REAL)")
    conn.execute("INSERT INTO nodes (id, utility_score) VALUES (?, ?)", ("ob_0001", 0.0))
    conn.commit()
    conn.close()

    # Wrap credit_mentions to route the bump to our tmp db
    # (hook.KNOWLEDGE_DB now points to isolated_engram_home's tmp, not this test's
    # tmp_path; the wrapper ensures the right DB gets the write).
    _real_credit_mentions = hook.credit_mentions

    def _credit_mentions_with_tmp_db(last_message: str, **kwargs):
        kwargs.setdefault("db_path", str(db_path))
        return _real_credit_mentions(last_message, **kwargs)

    monkeypatch.setattr(hook, "credit_mentions", _credit_mentions_with_tmp_db)

    payload = json.dumps({
        "stop_hook_active": False,
        "last_assistant_message": "Firing the full pipeline, mentioning ob_0001.",
        "session_id": "hermetic-proof-session",
        "transcript_path": "",
    })
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    hook.main()

    after_size = real_log.stat().st_size if real_log.exists() else -1
    assert before_size == after_size, (
        f"Real ENGRAM_HOME log changed: before={before_size} bytes, after={after_size} bytes. "
        f"The isolation fixture did not fully contain log writes. "
        f"Real log path: {real_log}"
    )


# --------------------------------------------------------------------------- #
# bump_utility — Q-formula
# --------------------------------------------------------------------------- #


def test_bump_utility_q_formula_zero_baseline(tmp_db):
    # ob_1234 starts at 0.0; α=0.12 → Q_new = 0 + 0.12*(1-0) = 0.12
    updated = hook.bump_utility({"ob_1234"}, alpha=0.12, db_path=tmp_db)
    assert updated == 1
    conn = sqlite3.connect(tmp_db)
    val = conn.execute("SELECT utility_score FROM nodes WHERE id = 'ob_1234'").fetchone()[0]
    conn.close()
    assert val == pytest.approx(0.12, abs=1e-9)


def test_bump_utility_q_formula_mid_baseline(tmp_db):
    # dv_0567 starts at 0.5; α=0.12 → Q_new = 0.5 + 0.12*0.5 = 0.56
    hook.bump_utility({"dv_0567"}, alpha=0.12, db_path=tmp_db)
    conn = sqlite3.connect(tmp_db)
    val = conn.execute("SELECT utility_score FROM nodes WHERE id = 'dv_0567'").fetchone()[0]
    conn.close()
    assert val == pytest.approx(0.56, abs=1e-9)


def test_bump_utility_q_formula_near_ceiling(tmp_db):
    # ax_0001 starts at 0.9; α=0.12 → Q_new = 0.9 + 0.12*0.1 = 0.912
    hook.bump_utility({"ax_0001"}, alpha=0.12, db_path=tmp_db)
    conn = sqlite3.connect(tmp_db)
    val = conn.execute("SELECT utility_score FROM nodes WHERE id = 'ax_0001'").fetchone()[0]
    conn.close()
    assert val == pytest.approx(0.912, abs=1e-9)


def test_bump_utility_handles_null_utility_score(tmp_db):
    # ob_9999 has utility_score=NULL → coerced to 0 → Q_new = 0.12
    hook.bump_utility({"ob_9999"}, alpha=0.12, db_path=tmp_db)
    conn = sqlite3.connect(tmp_db)
    val = conn.execute("SELECT utility_score FROM nodes WHERE id = 'ob_9999'").fetchone()[0]
    conn.close()
    assert val == pytest.approx(0.12, abs=1e-9)


def test_bump_utility_missing_id_silently_skipped(tmp_db):
    # ob_0000 doesn't exist → updated count = 0, no error
    updated = hook.bump_utility({"ob_0000"}, alpha=0.12, db_path=tmp_db)
    assert updated == 0


def test_bump_utility_mixed_existing_and_missing(tmp_db):
    updated = hook.bump_utility({"ob_1234", "ob_0000", "dv_0567"}, alpha=0.12, db_path=tmp_db)
    assert updated == 2


def test_bump_utility_empty_set_noop(tmp_db):
    assert hook.bump_utility(set(), alpha=0.12, db_path=tmp_db) == 0


# --------------------------------------------------------------------------- #
# credit_mentions — end-to-end pipeline (no recall_window, PR #214)
# --------------------------------------------------------------------------- #


def test_credit_mentions_alpha_matches_server_use_alpha():
    """ALPHA_MENTION must match server.py:USE_ALPHA['mention'].

    The hook keeps ALPHA_MENTION as a local literal (to avoid importing the
    full server module at hook-fire time). This test is the cross-file
    invariant check: import server.USE_ALPHA at TEST time and assert the
    two values agree. If they diverge (either side updates without the
    other), this test fails and surfaces the drift loudly.
    """
    # The hook's _load_hook execution inserts ENGRAM_HOME (~/.engram) at
    # sys.path[0], so a plain `import server` would resolve to the deployed
    # snapshot, not the worktree under test. Re-prioritize REPO_ROOT here
    # so the assertion checks THIS branch's server.py.
    sys.path.insert(0, str(REPO_ROOT))
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server  # heavy import OK at test time, NOT at hook fire
    assert hook.ALPHA_MENTION == server.USE_ALPHA["mention"], (
        f"Hook's ALPHA_MENTION={hook.ALPHA_MENTION} drifted from "
        f"server.py:USE_ALPHA['mention']={server.USE_ALPHA['mention']}. "
        f"Update both together; the comment in the hook flags this invariant."
    )


def test_credit_mentions_bumps_all_mentioned_ids_present_in_db(tmp_db):
    """Happy path: 3 IDs mentioned, all 3 in DB → all 3 bumped by α=0.10."""
    message = "I built on ob_1234 and confirmed via dv_0567; see also ax_0001."
    stats = hook.credit_mentions(message, db_path=tmp_db, alpha=0.10)
    assert stats["mentioned_count"] == 3
    assert stats["updated_count"] == 3

    conn = sqlite3.connect(tmp_db)
    rows = dict(conn.execute("SELECT id, utility_score FROM nodes").fetchall())
    conn.close()
    # ob_1234: 0.0 → 0.10; dv_0567: 0.5 → 0.55; ax_0001: 0.9 → 0.91
    assert rows["ob_1234"] == pytest.approx(0.10, abs=1e-9)
    assert rows["dv_0567"] == pytest.approx(0.55, abs=1e-9)
    assert rows["ax_0001"] == pytest.approx(0.91, abs=1e-9)


def test_credit_mentions_silently_skips_missing_db_ids(tmp_db):
    """IDs mentioned in prose but absent from DB → no error, updated_count=0."""
    stats = hook.credit_mentions(
        "Earlier I cited ob_0000 and ob_9876 in my response.",
        db_path=tmp_db,
        alpha=0.10,
    )
    assert stats["mentioned_count"] == 2
    assert stats["updated_count"] == 0


def test_credit_mentions_no_message_noop(tmp_db):
    stats = hook.credit_mentions("", db_path=tmp_db)
    assert stats == {
        "mentioned_count": 0,
        "updated_count": 0,
    }


def test_credit_mentions_no_ids_in_message(tmp_db):
    stats = hook.credit_mentions(
        "Plain prose with no node IDs at all.",
        db_path=tmp_db,
    )
    assert stats["mentioned_count"] == 0
    assert stats["updated_count"] == 0


def test_credit_mentions_deduplicates(tmp_db):
    # Same ID mentioned twice → counted once, bumped once
    stats = hook.credit_mentions(
        "First ob_1234, then again ob_1234 mentioned.",
        db_path=tmp_db,
        alpha=0.10,
    )
    assert stats["mentioned_count"] == 1
    assert stats["updated_count"] == 1

    conn = sqlite3.connect(tmp_db)
    val = conn.execute("SELECT utility_score FROM nodes WHERE id = 'ob_1234'").fetchone()[0]
    conn.close()
    assert val == pytest.approx(0.10, abs=1e-9)  # single bump, not double


def test_credit_mentions_shape_match_but_unknown_prefix_capital(tmp_db):
    """Shape-regex false-positives with capital prefix are not matched.

    `In_1234` (capital I) doesn't match the regex (requires lowercase prefix)
    so mentioned_count=0 and nothing is bumped.
    """
    stats = hook.credit_mentions(
        "In_1234 was mentioned but it's not a real ID.",  # capital I won't match regex
        db_path=tmp_db,
    )
    # "In_1234" doesn't match (regex requires lowercase prefix)
    assert stats["mentioned_count"] == 0


def test_credit_mentions_lowercase_shape_match_misses_in_db(tmp_db):
    """Defensive case: lowercase `in_1234` matches the regex but the DB has no
    such row. bump_utility must silently skip — mentioned_count=1, updated_count=0.
    """
    stats = hook.credit_mentions(
        "Earlier I cited in_1234 in my response.",  # lowercase — DOES match regex
        db_path=tmp_db,
    )
    assert stats["mentioned_count"] == 1
    assert stats["updated_count"] == 0


# --------------------------------------------------------------------------- #
# bump_utility — rounding invariant (Suggestion F)
# --------------------------------------------------------------------------- #


def test_bump_utility_stores_rounded_to_6_decimals(tmp_db):
    """Blocker A invariant: stored value must be exactly round(q_new, 6).

    Computes the expected value independently and asserts exact equality
    (not approx). Tests that floating-point accumulation past the 7th digit
    is truncated before storage, matching server.py:_utility_reward behaviour.
    """
    # ob_1234 starts at 0.0, alpha=0.12.
    # raw Q_new = 0.0 + 0.12 * (1.0 - 0.0) = 0.12 (exact in IEEE 754)
    # round(0.12, 6) = 0.12 — for this particular value they are equal.
    # Use a baseline that creates sub-6-decimal drift to make rounding observable.
    # dv_0567 starts at 0.5, alpha=0.12 → raw = 0.5 + 0.12 * 0.5 = 0.56
    # round(0.56, 6) = 0.56  (clean)
    # ax_0001 starts at 0.9, alpha=0.12 → raw = 0.9 + 0.12 * 0.1 = 0.912
    # Use a pathological alpha to force sub-6-decimal digits.
    q_old = 1.0 / 3.0  # 0.333333...
    alpha = 1.0 / 7.0  # 0.142857...
    raw = q_old + alpha * (1.0 - q_old)
    expected = round(raw, 6)

    # Seed the DB with this exact baseline.
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO nodes (id, utility_score) VALUES (?, ?)", ("ob_frac", q_old))
    conn.commit()
    conn.close()

    hook.bump_utility({"ob_frac"}, alpha=alpha, db_path=tmp_db)

    conn = sqlite3.connect(tmp_db)
    stored = conn.execute("SELECT utility_score FROM nodes WHERE id = 'ob_frac'").fetchone()[0]
    conn.close()
    # Exact equality — not approx. This pins that round(q_new, 6) is what's stored.
    assert stored == expected


# --------------------------------------------------------------------------- #
# bump_utility — Q ∈ [0, 1] property (Suggestion G)
# --------------------------------------------------------------------------- #


def test_bump_utility_q_stays_in_unit_interval(tmp_db):
    """Property: 100 repeated Q-updates with alpha=0.12 starting from Q=0
    must keep Q in [0, 1] at every step (documents asymptote-at-1 monotonic
    behaviour).
    """
    # Seed a single node at 0.0.
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO nodes (id, utility_score) VALUES (?, ?)", ("ob_prop", 0.0))
    conn.commit()
    conn.close()

    for _ in range(100):
        hook.bump_utility({"ob_prop"}, alpha=0.12, db_path=tmp_db)
        conn = sqlite3.connect(tmp_db)
        val = conn.execute("SELECT utility_score FROM nodes WHERE id = 'ob_prop'").fetchone()[0]
        conn.close()
        assert 0.0 <= val <= 1.0, f"Q={val!r} escaped [0, 1]"


# --------------------------------------------------------------------------- #
# collect_turn_text — JSONL walk (Design Issue D)
# --------------------------------------------------------------------------- #


def _make_jsonl_line(entry_type: str, content) -> str:
    """Build a minimal JSONL entry matching Claude Code's session format."""
    return json.dumps({"type": entry_type, "message": {"content": content}})


def test_collect_turn_text_includes_mid_turn_reasoning(tmp_path):
    """JSONL with a real user message, 3 assistant text blocks (interspersed with
    tool_result user entries), and a tool_use block should collect all 3 text blocks.

    Layout (chronological):
      user (real)            ← turn boundary — stop here
      assistant text A
      user tool_result       ← NOT a turn boundary
      assistant tool_use
      user tool_result       ← NOT a turn boundary
      assistant text B
      user tool_result       ← NOT a turn boundary
      assistant text C       ← last flushed before this Stop fire
    (last_assistant_message = "text D" from stdin, not yet in JSONL)
    """
    jsonl = tmp_path / "session.jsonl"
    lines = [
        # Prior turn boundary
        _make_jsonl_line("user", "Hello, what is ob_0000?"),
        # Turn starts: first assistant text block
        _make_jsonl_line("assistant", [{"type": "text", "text": "text A mentions ob_1234"}]),
        # Tool-result user entry (not a real user message)
        _make_jsonl_line("user", [{"type": "tool_result", "content": "some result"}]),
        # Assistant with tool_use (no text content to collect)
        _make_jsonl_line("assistant", [{"type": "tool_use", "id": "tu_1", "name": "some_tool", "input": {}}]),
        # Another tool-result
        _make_jsonl_line("user", [{"type": "tool_result", "content": "result 2"}]),
        # Second assistant text block
        _make_jsonl_line("assistant", [{"type": "text", "text": "text B mentions dv_0567"}]),
        # Another tool-result
        _make_jsonl_line("user", [{"type": "tool_result", "content": "result 3"}]),
        # Third assistant text block (last flushed)
        _make_jsonl_line("assistant", [{"type": "text", "text": "text C mentions ax_0001"}]),
    ]
    jsonl.write_text("\n".join(lines) + "\n")

    result = hook.collect_turn_text(str(jsonl), "text D mentions ob_9999")

    # All 3 flushed blocks + stdin's last message must appear.
    assert "text A mentions ob_1234" in result
    assert "text B mentions dv_0567" in result
    assert "text C mentions ax_0001" in result
    assert "text D mentions ob_9999" in result
    # Prior turn's real user message content must NOT appear.
    assert "Hello, what is ob_0000?" not in result


def test_collect_turn_text_stops_at_real_user_message(tmp_path):
    """A prior turn's assistant texts that precede a real user message must NOT
    be collected — the real user message is the turn boundary.
    """
    jsonl = tmp_path / "session.jsonl"
    lines = [
        # Prior prior turn (should not be collected)
        _make_jsonl_line("assistant", [{"type": "text", "text": "old text from ob_8888"}]),
        # Real user message — turn boundary
        _make_jsonl_line("user", "Start of new turn"),
        # Current turn's assistant text
        _make_jsonl_line("assistant", [{"type": "text", "text": "current text mentions ob_1234"}]),
    ]
    jsonl.write_text("\n".join(lines) + "\n")

    result = hook.collect_turn_text(str(jsonl), "stdin ob_9999")

    assert "current text mentions ob_1234" in result
    assert "stdin ob_9999" in result
    # Prior-turn text must not bleed through.
    assert "old text from ob_8888" not in result


def test_collect_turn_text_missing_transcript_returns_last_message():
    """When transcript_path is empty/missing, falls back to last_assistant_message."""
    result = hook.collect_turn_text("", "fallback ob_1234")
    assert result == "fallback ob_1234"


def test_collect_turn_text_nonexistent_file_returns_last_message(tmp_path):
    result = hook.collect_turn_text(str(tmp_path / "nope.jsonl"), "fallback ob_5678")
    assert result == "fallback ob_5678"


# --------------------------------------------------------------------------- #
# main — stop_hook_active guard
# --------------------------------------------------------------------------- #


def test_main_stop_hook_active_guard_skips_credit_assignment(monkeypatch):
    """When the Stop-hook payload has stop_hook_active=True (the hook re-fired
    on its own write), main() must return early without calling credit_mentions.
    """
    payload = json.dumps(
        {
            "stop_hook_active": True,
            "last_assistant_message": "Cited ob_1234 in this turn.",
            "session_id": "test_session",
            "transcript_path": "",
        }
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return {"mentioned_count": 0, "updated_count": 0}

    monkeypatch.setattr(hook, "credit_mentions", _spy)

    hook.main()

    assert calls == [], f"credit_mentions called {len(calls)} times despite stop_hook_active=True"


def test_is_real_user_message_empty_list_is_not_boundary():
    """Empty content list must NOT be treated as a real user message — would
    cut off the backwards walk prematurely.
    """
    assert hook._is_real_user_message([]) is False


def test_is_real_user_message_empty_string_is_not_boundary():
    """Empty string content is not a real user message either."""
    assert hook._is_real_user_message("") is False


# --------------------------------------------------------------------------- #
# Activation smoke test — first-ever production activation (issue #783)
# --------------------------------------------------------------------------- #


def test_main_full_pipeline_bumps_utility_via_stdin(monkeypatch, tmp_path):
    """Smoke test for the full main() path: synthetic Stop-hook stdin payload
    mentioning ob_0001; assert utility_score increased by Q-update formula.

    This hook was never activated before issue #783.  This test exercises the
    complete execution path that will run in production:
      1. main() reads stdin JSON (Stop-hook payload shape).
      2. collect_turn_text() is called (no transcript → falls back to stdin msg).
      3. credit_mentions() finds ob_0001, bumps its utility_score.
      4. Q_new = Q_old + 0.10 * (1 - Q_old) = 0.0 + 0.10 * 1.0 = 0.10.

    ENGRAM_HOME is pointed at an isolated tmp_path throughout (via the
    isolated_engram_home autouse fixture); the real log is never touched.

    Note on db_path threading: credit_mentions() has `db_path=KNOWLEDGE_DB` as
    a default argument frozen at import time. After the autouse fixture's reload,
    KNOWLEDGE_DB points to the fixture's tmp_path, not this test's tmp_path.
    We wrap credit_mentions to inject this test's tmp db_path so the real bump
    logic runs while pointing at the synthetic DB.
    """
    # Build a minimal knowledge.db with a single node at utility_score=0.0.
    db_path = tmp_path / "knowledge.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, utility_score REAL)")
    conn.execute("INSERT INTO nodes (id, utility_score) VALUES (?, ?)", ("ob_0001", 0.0))
    conn.commit()
    conn.close()

    # Redirect LOG_PATH to tmp_path so main()'s _log() writes there.
    # After the autouse fixture's reload, LOG_PATH already points to a tmp dir;
    # this monkeypatch redirects it specifically to this test's tmp_path for
    # clarity, but the real ~/.engram log is not written either way.
    monkeypatch.setattr(hook, "LOG_PATH", str(tmp_path / "utility-credit-mention.log"))

    # Wrap credit_mentions to inject this test's tmp db_path, leaving all other
    # logic intact. (The default db_path after reload points to the fixture's
    # tmp_path, not this test's tmp_path — they differ.)
    _real_credit_mentions = hook.credit_mentions

    def _credit_mentions_with_tmp_db(last_message: str, **kwargs):
        # .setdefault() supplies the tmp default only when db_path is absent,
        # so a test that passes an explicit db_path has it forwarded unchanged.
        kwargs.setdefault("db_path", str(db_path))
        return _real_credit_mentions(last_message, **kwargs)

    monkeypatch.setattr(hook, "credit_mentions", _credit_mentions_with_tmp_db)

    # Construct a minimal Stop-hook stdin payload that mentions ob_0001.
    payload = json.dumps({
        "stop_hook_active": False,
        "last_assistant_message": "I built this derivation using ob_0001 as the key premise.",
        "session_id": "smoke-test-session",
        "transcript_path": "",
    })
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    hook.main()

    # Assert utility_score was bumped: Q_new = 0.0 + 0.10*(1-0.0) = 0.10
    conn = sqlite3.connect(str(db_path))
    val = conn.execute("SELECT utility_score FROM nodes WHERE id = 'ob_0001'").fetchone()[0]
    conn.close()
    assert val == pytest.approx(0.10, abs=1e-9), (
        f"Activation smoke test failed: utility_score={val!r}, expected 0.10. "
        "The full main() pipeline did not bump the utility score for the mentioned node."
    )
