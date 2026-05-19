"""Tests for hooks/claude/engram-utility-credit-mention-stop.py.

Covers the prose-mention credit-assignment hook (alpha #177 area 4):
  - get_current_turn(): config.json memory.current_turn read
  - recall_window_active_ids(): TTL semantics, missing/corrupt file
  - bump_utility(): Q-update formula correctness, missing IDs skipped
  - credit_mentions(): end-to-end pipeline against tmp config + window + db

All tests are self-contained: they build minimal in-memory or tmp_path
sqlite schemas + JSON files. Production knowledge.db is NEVER touched.
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


def _load_hook():
    spec = importlib.util.spec_from_file_location("engram_utility_credit_mention_stop", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


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


@pytest.fixture
def tmp_config(tmp_path) -> str:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"memory": {"current_turn": 42}}))
    return str(cfg_path)


@pytest.fixture
def tmp_window(tmp_path) -> str:
    win_path = tmp_path / "recall_window.json"
    win_path.write_text(
        json.dumps(
            {
                "entries": [
                    {"ids": ["ob_1234", "dv_0567"], "turn": 42, "source": "surface"},
                    {"ids": ["ax_0001"], "turn": 41, "source": "query"},
                    {"ids": ["ob_8888"], "turn": 38, "source": "surface"},  # outside TTL=3
                ]
            }
        )
    )
    return str(win_path)


# --------------------------------------------------------------------------- #
# get_current_turn
# --------------------------------------------------------------------------- #


def test_get_current_turn_reads_memory_section(tmp_config):
    assert hook.get_current_turn(tmp_config) == 42


def test_get_current_turn_missing_file_returns_zero(tmp_path):
    assert hook.get_current_turn(str(tmp_path / "nope.json")) == 0


def test_get_current_turn_corrupt_json_returns_zero(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{ not valid json")
    assert hook.get_current_turn(str(bad)) == 0


def test_get_current_turn_missing_memory_section_returns_zero(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"other": "fields"}))
    assert hook.get_current_turn(str(cfg)) == 0


# --------------------------------------------------------------------------- #
# recall_window_active_ids
# --------------------------------------------------------------------------- #


def test_recall_window_active_ids_within_ttl(tmp_window):
    # turn=42, ttl=3 → cutoff=39. Entries at turn 42 and 41 qualify; turn 38 doesn't.
    active = hook.recall_window_active_ids(42, tmp_window, ttl=3)
    assert active == {"ob_1234", "dv_0567", "ax_0001"}


def test_recall_window_active_ids_filters_old_entries(tmp_window):
    # Move forward to turn 60 → cutoff=57. NO entries qualify.
    active = hook.recall_window_active_ids(60, tmp_window, ttl=3)
    assert active == set()


def test_recall_window_active_ids_includes_exact_cutoff_turn(tmp_path):
    # Pin the inclusive cutoff: current=42, ttl=3 → cutoff=39. An entry at
    # turn=39 is KEPT (entry.turn >= cutoff), turn=38 is dropped. Mirrors
    # server.py:1528.
    win = tmp_path / "rw.json"
    win.write_text(
        json.dumps(
            {
                "entries": [
                    {"ids": ["ob_at_cutoff"], "turn": 39, "source": "surface"},
                    {"ids": ["ob_below_cutoff"], "turn": 38, "source": "surface"},
                ]
            }
        )
    )
    active = hook.recall_window_active_ids(42, str(win), ttl=3)
    assert active == {"ob_at_cutoff"}


def test_recall_window_active_ids_missing_file(tmp_path):
    assert hook.recall_window_active_ids(10, str(tmp_path / "nope.json")) == set()


def test_recall_window_active_ids_corrupt_json(tmp_path):
    bad = tmp_path / "rw.json"
    bad.write_text("{ corrupt")
    assert hook.recall_window_active_ids(10, str(bad)) == set()


def test_recall_window_active_ids_empty_entries(tmp_path):
    empty = tmp_path / "rw.json"
    empty.write_text(json.dumps({"entries": []}))
    assert hook.recall_window_active_ids(10, str(empty)) == set()


def test_recall_window_active_ids_skips_non_string_ids(tmp_path):
    """Defensive: a malformed entry with numeric IDs shouldn't crash or leak."""
    rw = tmp_path / "rw.json"
    rw.write_text(
        json.dumps(
            {
                "entries": [
                    {"ids": ["ob_1234", 12345, None, "dv_0567"], "turn": 10, "source": "surface"},
                ]
            }
        )
    )
    assert hook.recall_window_active_ids(10, str(rw)) == {"ob_1234", "dv_0567"}


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
# credit_mentions — end-to-end pipeline
# --------------------------------------------------------------------------- #


def test_credit_mentions_happy_path(tmp_db, tmp_config, tmp_window):
    message = "I built on ob_1234 and confirmed via dv_0567; see also ax_0001."
    stats = hook.credit_mentions(
        message,
        config_path=tmp_config,
        recall_window_path=tmp_window,
        db_path=tmp_db,
        alpha=0.12,
        ttl=3,
    )
    assert stats["mentioned_count"] == 3
    assert stats["active_window_count"] == 3
    assert stats["eligible_count"] == 3
    assert stats["updated_count"] == 3

    conn = sqlite3.connect(tmp_db)
    rows = dict(conn.execute("SELECT id, utility_score FROM nodes").fetchall())
    conn.close()
    # ob_1234: 0.0 → 0.12; dv_0567: 0.5 → 0.56; ax_0001: 0.9 → 0.912
    assert rows["ob_1234"] == pytest.approx(0.12, abs=1e-9)
    assert rows["dv_0567"] == pytest.approx(0.56, abs=1e-9)
    assert rows["ax_0001"] == pytest.approx(0.912, abs=1e-9)


def test_credit_mentions_no_message_noop(tmp_db, tmp_config, tmp_window):
    stats = hook.credit_mentions("", config_path=tmp_config, recall_window_path=tmp_window, db_path=tmp_db)
    assert stats == {
        "mentioned_count": 0,
        "active_window_count": 0,
        "eligible_count": 0,
        "updated_count": 0,
    }


def test_credit_mentions_no_ids_in_message(tmp_db, tmp_config, tmp_window):
    stats = hook.credit_mentions(
        "Plain prose with no node IDs at all.",
        config_path=tmp_config,
        recall_window_path=tmp_window,
        db_path=tmp_db,
    )
    assert stats["mentioned_count"] == 0
    assert stats["updated_count"] == 0


def test_credit_mentions_mentions_not_in_window(tmp_db, tmp_config, tmp_window):
    # ob_5555 is mentioned but not in the recall window → no credit
    stats = hook.credit_mentions(
        "Some unrelated id ob_5555 in the response.",
        config_path=tmp_config,
        recall_window_path=tmp_window,
        db_path=tmp_db,
    )
    assert stats["mentioned_count"] == 1
    assert stats["active_window_count"] == 3
    assert stats["eligible_count"] == 0
    assert stats["updated_count"] == 0


def test_credit_mentions_window_outside_ttl(tmp_db, tmp_config, tmp_path):
    # All window entries are older than TTL → no credit even for mentioned IDs
    rw = tmp_path / "rw.json"
    rw.write_text(
        json.dumps(
            {"entries": [{"ids": ["ob_1234"], "turn": 10, "source": "surface"}]}
        )
    )
    stats = hook.credit_mentions(
        "Mentioning ob_1234 in this response.",
        config_path=tmp_config,  # current_turn=42, cutoff=39
        recall_window_path=str(rw),
        db_path=tmp_db,
    )
    assert stats["mentioned_count"] == 1
    assert stats["active_window_count"] == 0
    assert stats["eligible_count"] == 0
    assert stats["updated_count"] == 0


def test_credit_mentions_deduplicates(tmp_db, tmp_config, tmp_window):
    # Same ID mentioned twice → counted once, bumped once
    stats = hook.credit_mentions(
        "First ob_1234, then again ob_1234 mentioned.",
        config_path=tmp_config,
        recall_window_path=tmp_window,
        db_path=tmp_db,
    )
    assert stats["mentioned_count"] == 1
    assert stats["eligible_count"] == 1
    assert stats["updated_count"] == 1

    conn = sqlite3.connect(tmp_db)
    val = conn.execute("SELECT utility_score FROM nodes WHERE id = 'ob_1234'").fetchone()[0]
    conn.close()
    assert val == pytest.approx(0.12, abs=1e-9)  # single bump, not double


def test_credit_mentions_shape_match_but_unknown_prefix(tmp_db, tmp_config, tmp_path):
    """Shape-regex false-positives (in_1234, etc.) are caught at the DB layer.

    `in_1234` matches the regex but isn't a real node ID. recall_window
    shouldn't contain it; even if it did (defensive), bump_utility would
    skip the missing row in the DB.
    """
    rw = tmp_path / "rw.json"
    rw.write_text(json.dumps({"entries": [{"ids": ["in_1234"], "turn": 42, "source": "surface"}]}))
    stats = hook.credit_mentions(
        "In_1234 was mentioned but it's not a real ID.",  # capital I won't match regex
        config_path=tmp_config,
        recall_window_path=str(rw),
        db_path=tmp_db,
    )
    # "In_1234" doesn't match (regex requires lowercase prefix)
    assert stats["mentioned_count"] == 0


def test_credit_mentions_lowercase_shape_match_misses_in_db(tmp_db, tmp_config, tmp_path):
    """Defensive case: lowercase `in_1234` matches the regex AND is in the
    recall window, but the DB has no such row. bump_utility must silently
    skip — mentioned_count=1, eligible_count=1, updated_count=0.
    """
    rw = tmp_path / "rw.json"
    rw.write_text(json.dumps({"entries": [{"ids": ["in_1234"], "turn": 42, "source": "surface"}]}))
    stats = hook.credit_mentions(
        "Earlier I cited in_1234 in my response.",  # lowercase — DOES match regex
        config_path=tmp_config,
        recall_window_path=str(rw),
        db_path=tmp_db,
    )
    assert stats["mentioned_count"] == 1
    assert stats["eligible_count"] == 1
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
        return {"mentioned_count": 0, "active_window_count": 0, "eligible_count": 0, "updated_count": 0}

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
