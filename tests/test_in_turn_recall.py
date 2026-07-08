"""Tests for in-turn ambient recall (#1690, P2 slice 1).

The check rides the lesson-tripwire PreToolUse hook (one spawn, two checks).
Coverage:
  1. disabled by default (no config) — silence
  2. enabled + novel terms + daemon hits — renders lines and updates state
  3. cooldown suppresses an immediate second fire
  4. all terms already seen (no novelty) — silence
  5. recently-rendered node IDs are excluded
  6. daemon unreachable — silence
  7. tripwire hit and recall compose (both render when both fire)

engram_idf and the daemon socket are stubbed: the gate logic is under test,
not IDF extraction or the daemon (covered by their own tests).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SRC_ENGRAM))

HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"

import pytest


@pytest.fixture(autouse=True)
def _clean_stub_idf():
    """Never leak the fake engram_idf into other tests in this process."""
    yield
    mod = sys.modules.get("engram_idf")
    if mod is not None and getattr(mod, "__file__", None) is None:
        sys.modules.pop("engram_idf", None)


def _load_hook(engram_home: str):
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = engram_home
    try:
        spec = importlib.util.spec_from_file_location(
            f"tripwire_hook_{Path(engram_home).name}", HOOK_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old


def _stub_idf(terms_by_call: list[list[str]]):
    """Install a fake engram_idf whose extract_keywords pops from the list
    (last entry repeats once exhausted).

    CONTRACT: returns (token, idf_score) PAIRS, matching the real
    engram_idf.extract_keywords — the original stub returned flat strings,
    which masked a TypeError against the real module (#1696 fairy blocker).
    """
    calls = {"n": 0}

    def extract_keywords(conn, text, min_idf=4.0, top_k=5):
        i = min(calls["n"], len(terms_by_call) - 1)
        calls["n"] += 1
        return [(t, 7.5) for t in terms_by_call[i]]

    fake = types.ModuleType("engram_idf")
    fake.extract_keywords = extract_keywords
    sys.modules["engram_idf"] = fake
    return fake


def _enable(hook, tmp: str, **overrides):
    cfg = {"auto_surface": {"in_turn_recall": {"enabled": True, **overrides}}}
    with open(Path(tmp) / "config.json", "w") as f:
        json.dump(cfg, f)
    # knowledge.db must exist for the sqlite ro-open not to fail the gate —
    # the stubbed extract_keywords never reads it, but the connect does.
    import sqlite3
    sqlite3.connect(Path(tmp) / "knowledge.db").close()


def _daemon_result(ids):
    return {
        "match_count": len(ids),
        "special_nodes": [],
        "top_claims": [
            {"id": i, "recall_summary": f"summary of {i}", "recall_keywords": ["kw"]}
            for i in ids
        ],
    }


def test_disabled_by_default(tmp_path):
    hook = _load_hook(str(tmp_path))
    assert hook.check_in_turn_recall("engram_derive some novel thing") == ""


def test_fires_and_updates_state(tmp_path, monkeypatch):
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path))
    _stub_idf([["replay", "harness"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1", "ob_2"]))

    out = hook.check_in_turn_recall("Bash python replay-bench harness run")
    assert "ENGRAM in-turn recall" in out
    assert "replay" in out and "ob_1" in out and "ob_2" in out

    state = json.load(open(hook.STATE_PATH))
    assert state["recent_ids"] == ["ob_1", "ob_2"]
    assert ["replay", "harness"] in state["recent_term_sets"]
    assert state["last_fire_ts"] > 0


def test_cooldown_suppresses_second_fire(tmp_path, monkeypatch):
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path))
    _stub_idf([["alpha"], ["beta"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    assert hook.check_in_turn_recall("first alpha call")
    # beta is novel, but the cooldown (default 60s) blocks before IDF runs
    assert hook.check_in_turn_recall("second beta call") == ""


def test_no_novel_terms_is_silent(tmp_path, monkeypatch):
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=0)
    _stub_idf([["gamma"], ["gamma"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    assert hook.check_in_turn_recall("gamma one")
    assert hook.check_in_turn_recall("gamma again") == ""


def test_recent_ids_excluded(tmp_path, monkeypatch):
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=0)
    _stub_idf([["delta"], ["epsilon"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1", "ob_2"]))

    first = hook.check_in_turn_recall("delta call")
    assert "ob_1" in first
    second = hook.check_in_turn_recall("epsilon call")
    # ob_1/ob_2 were just rendered — excluded; nothing left → silence
    assert second == ""


def test_daemon_down_is_silent(tmp_path, monkeypatch):
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path))
    _stub_idf([["zeta"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: None)
    assert hook.check_in_turn_recall("zeta call") == ""


def test_tripwire_and_recall_compose(tmp_path, monkeypatch, capsys):
    """When a tripwire matches AND the recall gate passes, both render."""
    import io
    import sqlite3

    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path))
    _stub_idf([["eta"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_9"]))

    # Seed a lesson with a situation_pattern into a minimal nodes table.
    conn = sqlite3.connect(hook.DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS nodes (
               id TEXT PRIMARY KEY, type TEXT, claim TEXT, is_current INT,
               memory_status TEXT, metadata TEXT)"""
    )
    conn.execute(
        """INSERT INTO nodes VALUES ('ls_1', 'lesson', 'Placeholder lesson claim.', 1, 'active',
           json_object('situation_pattern', 'danger-cmd',
                       'scaffolding_nudge', 'Check the guard first.'))"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "run danger-cmd now"},
        })),
    )
    try:
        hook.main()
    except SystemExit:
        pass
    out = capsys.readouterr().out
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "lesson-tripwire" in ctx
    assert "Check the guard first." in ctx
    assert "ENGRAM in-turn recall" in ctx
    assert "ob_9" in ctx


def test_near_miss_still_stamps_cooldown(tmp_path, monkeypatch):
    """When the gate passes but the daemon result is all already-rendered
    nodes (or empty), the cooldown + term-set memory must STILL be stamped —
    otherwise near-misses re-trigger the daemon on every tool call (#1696
    reviewer-fairy empirical repro: 5 unthrottled daemon hits in a row)."""
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path))  # default 60s cooldown
    _stub_idf([["theta"], ["iota"], ["kappa"]])
    daemon_calls = {"n": 0}

    def counting_daemon(q, top_k=5):
        daemon_calls["n"] += 1
        return {"match_count": 0}  # gate passes, nothing to render

    monkeypatch.setattr(hook, "_query_recall_daemon", counting_daemon)

    assert hook.check_in_turn_recall("theta call") == ""
    assert daemon_calls["n"] == 1
    state = json.load(open(hook.STATE_PATH))
    assert state["last_fire_ts"] > 0, "near-miss did not stamp the cooldown"
    assert [["theta", ]][0][0] in [t for ts in state["recent_term_sets"] for t in ts]

    # Second call with fresh novel terms: cooldown must block BEFORE the
    # daemon is queried again.
    assert hook.check_in_turn_recall("iota call") == ""
    assert daemon_calls["n"] == 1, "cooldown failed to throttle a near-miss retrigger"
