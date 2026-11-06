"""#4395 Q2: every row the lesson-tripwire hook writes to its three ledgers
(tripwire-fire-ledger.jsonl, lesson-prevention-ledger.jsonl,
in-turn-recall-ledger.jsonl) carries `agent_id` / `agent_type` exactly as the
PreToolUse payload gives them, and records their ABSENCE as absent (no key --
never null, "main" or ""). transcript_path / session_id stay as given.

The payloads under tests/fixtures/hook_payloads_4395/ are REAL captures
(macOS, Claude Code 2.1.306); they are the hook's stdin here, not hand-written.
Readers of the ledgers are out of scope and untouched.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.append(str(SRC_ENGRAM))  # #2565: append, not insert(0)

from engram_idf import JUNK_STOPLIST as _REAL_JUNK_STOPLIST  # noqa: E402

HOOK_PATH = SRC_ENGRAM / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hook_payloads_4395"

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

SUB = "pretooluse_subagent_sub_one.json"
MAIN = "pretooluse_main_main_one.json"
MAIN_TWO = "pretooluse_main_main_two.json"

LEDGERS = {
    "ws4": "tripwire-fire-ledger.jsonl",
    "prevention": "lesson-prevention-ledger.jsonl",
    "recall": "in-turn-recall-ledger.jsonl",
}


def _load_hook(engram_home: str):
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = engram_home
    try:
        spec = importlib.util.spec_from_file_location(
            f"tripwire_hook_4395_{Path(engram_home).parent.name}", HOOK_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old


def _stub_idf():
    def extract_keywords(conn, text, min_idf=4.0, top_k=5, stopwords=None, n_docs=None):
        return [("replay", 7.5), ("harness", 7.5)]
    fake = types.ModuleType("engram_idf")
    fake.extract_keywords = extract_keywords
    fake.STOPWORDS = frozenset()
    fake.JUNK_STOPLIST = _REAL_JUNK_STOPLIST
    sys.modules["engram_idf"] = fake


@pytest.fixture(autouse=True)
def _clean_stub_idf():
    yield
    mod = sys.modules.get("engram_idf")
    if mod is not None and getattr(mod, "__file__", None) is None:
        sys.modules.pop("engram_idf", None)


def _arm_home(home: Path) -> None:
    """Scratch ENGRAM_HOME in which all three ledgers get a row: WS4 active,
    in-turn recall enabled, a nodes table for the recall gate."""
    (home / "config.json").write_text(json.dumps({
        "experiments": {"ws4_chain_ab": {
            "enabled": True, "start": "2020-01-01T00:00:00Z",
            "end": "2099-01-01T00:00:00Z", "seed": "seed-4395"}},
        "auto_surface": {"in_turn_recall": {"enabled": True}},
    }))
    conn = sqlite3.connect(home / "knowledge.db")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS nodes (
               id TEXT PRIMARY KEY, type TEXT, claim TEXT, is_current INT,
               memory_status TEXT, metadata TEXT, recall_summary TEXT)""")
    conn.execute("INSERT INTO nodes (id, type, claim, is_current, memory_status, metadata) "
                 "VALUES ('seed_1', 'observation', 'seed node', 1, 'active', '{}')")
    conn.commit()
    conn.close()


def _run(tmp_path, fixture, monkeypatch, *, payload_text=None, patches=None):
    """Run main() in process with the fixture file as stdin, in a fresh
    scratch ENGRAM_HOME. Returns (stdout, exit_code, {ledger_name: [rows]})."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    _arm_home(home)
    hook = _load_hook(str(home))
    _stub_idf()
    text = payload_text if payload_text is not None else (FIXTURES / fixture).read_text()
    monkeypatch.setattr(hook, "load_tripwires",
                        lambda *a, **k: [(r"echo", "Nudge: look before you echo.", "ls_4395")])
    monkeypatch.setattr(hook, "load_prevention_lessons",
                        lambda *a, **k: [{"id": "ls_shadow", "pattern": "echo",
                                          "prevention_type": "shadow"}])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: {
        "match_count": 1, "special_nodes": [],
        "top_claims": [{"id": "ob_1", "recall_summary": "s", "recall_keywords": ["kw"]}]})
    for name, val in (patches or {}).items():
        monkeypatch.setattr(hook, name, val)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    monkeypatch.setattr(sys, "stdout", out)
    code = 0
    try:
        hook.main()
    except SystemExit as e:
        code = e.code or 0
    rows = {}
    for name, fname in LEDGERS.items():
        p = home / fname
        rows[name] = ([json.loads(l) for l in p.read_text().splitlines() if l.strip()]
                      if p.exists() else [])
    return out.getvalue(), code, rows


@pytest.mark.parametrize("ledger", list(LEDGERS))
def test_subagent_payload_rows_carry_agent_fields(tmp_path, monkeypatch, ledger):
    payload = json.loads((FIXTURES / SUB).read_text())
    _, _, rows = _run(tmp_path, SUB, monkeypatch)
    assert rows[ledger], f"no row written to {LEDGERS[ledger]}"
    for row in rows[ledger]:
        assert row["agent_id"] == "a4091a71f52524e76"
        assert row["agent_type"] == "probe"
        assert row["session_id"] == payload["session_id"]
        if ledger != "prevention":  # the prevention row has never carried transcript_path
            assert row["transcript_path"] == payload["transcript_path"]


@pytest.mark.parametrize("fixture", [MAIN, MAIN_TWO])
@pytest.mark.parametrize("ledger", list(LEDGERS))
def test_main_payload_rows_have_no_agent_keys(tmp_path, monkeypatch, ledger, fixture):
    _, _, rows = _run(tmp_path, fixture, monkeypatch)
    assert rows[ledger], f"no row written to {LEDGERS[ledger]}"
    for row in rows[ledger]:
        assert "agent_id" not in row
        assert "agent_type" not in row


def test_stdout_and_exit_unchanged_and_fields_not_in_stdout(tmp_path, monkeypatch):
    out_sub, code_sub, _ = _run(tmp_path / "a", SUB, monkeypatch)
    out_main, code_main, _ = _run(tmp_path / "b", MAIN, monkeypatch)
    assert code_sub == code_main
    assert out_sub, "expected the nudge on stdout"
    assert "a4091a71f52524e76" not in out_sub and "probe" not in out_sub
    assert "agent_id" not in out_sub and "agent_type" not in out_sub
    # With the stubs used here nothing input-specific reaches the rendered text,
    # so the two real payloads must give byte-identical stdout.
    assert out_sub == out_main


def test_ws4_fallback_row_carries_agent_fields(tmp_path, monkeypatch):
    def boom(**kw):
        raise RuntimeError("row construction failed")
    _, _, rows = _run(tmp_path, SUB, monkeypatch, patches={"_build_ws4_ledger_entry": boom})
    (row,) = rows["ws4"]
    assert "error" in row and "chain_rendered" not in row  # really the minimal fallback row
    assert row["agent_id"] == "a4091a71f52524e76" and row["agent_type"] == "probe"


@pytest.mark.parametrize("fixture,expect", [(SUB, True), (MAIN, False)])
def test_lease_demotion_prevention_row(tmp_path, monkeypatch, fixture, expect):
    deny = {"demoted": True, "lesson_id": "ls_b", "override_count": 3,
            "threshold": 3, "window_days": 7}
    _, _, rows = _run(tmp_path, fixture, monkeypatch,
                      patches={"check_blocking_deny": lambda *a, **k: deny})
    lease = [r for r in rows["prevention"] if r.get("mode") == "auto_demoted_lease"]
    assert len(lease) == 1
    if expect:
        assert lease[0]["agent_id"] == "a4091a71f52524e76" and lease[0]["agent_type"] == "probe"
    else:
        assert "agent_id" not in lease[0] and "agent_type" not in lease[0]


@pytest.mark.parametrize("bad", [None, "", 7, ["x"], {"a": 1}])
def test_malformed_agent_values_are_recorded_as_absent(tmp_path, monkeypatch, bad):
    """Non-string / empty values are malformed: the row records them as
    absent (no key), never as null, "" or a non-string that would read as
    an agent id."""
    payload = json.loads((FIXTURES / SUB).read_text())
    payload["agent_id"] = bad
    payload["agent_type"] = bad
    _, _, rows = _run(tmp_path, SUB, monkeypatch, payload_text=json.dumps(payload))
    for ledger in LEDGERS:
        assert rows[ledger]
        for row in rows[ledger]:
            assert "agent_id" not in row and "agent_type" not in row


def test_only_agent_type_present_is_recorded_alone(tmp_path, monkeypatch):
    payload = json.loads((FIXTURES / SUB).read_text())
    del payload["agent_id"]
    _, _, rows = _run(tmp_path, SUB, monkeypatch, payload_text=json.dumps(payload))
    for ledger in LEDGERS:
        assert rows[ledger]
        for row in rows[ledger]:
            assert "agent_id" not in row and row["agent_type"] == "probe"


# --- round 2 (PR #4402 review): two behaviours no cell above pinned -----------

def _recall_rows(tmp_path, monkeypatch, fixture):
    """Call check_in_turn_recall DIRECTLY with the real payload as hook_input and
    NO agent_fields argument, so the in-function fallback derives the fields."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    _arm_home(home)
    hook = _load_hook(str(home))
    _stub_idf()
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: {
        "match_count": 1, "special_nodes": [],
        "top_claims": [{"id": "ob_1", "recall_summary": "s", "recall_keywords": ["kw"]}]})
    payload = json.loads((FIXTURES / fixture).read_text())
    hook.check_in_turn_recall("replay harness check", payload)
    p = home / LEDGERS["recall"]
    return payload, ([json.loads(l) for l in p.read_text().splitlines() if l.strip()]
                     if p.exists() else [])


def test_in_turn_fallback_derives_fields_from_subagent_hook_input(tmp_path, monkeypatch):
    payload, rows = _recall_rows(tmp_path, monkeypatch, SUB)
    assert rows, "no row written to in-turn-recall-ledger.jsonl"
    for row in rows:
        assert row["agent_id"] == "a4091a71f52524e76"
        assert row["agent_type"] == "probe"
        assert row["transcript_path"] == payload["transcript_path"]


@pytest.mark.parametrize("fixture", [MAIN, MAIN_TWO])
def test_in_turn_fallback_main_payload_has_no_agent_keys(tmp_path, monkeypatch, fixture):
    _, rows = _recall_rows(tmp_path, monkeypatch, fixture)
    assert rows, "no row written to in-turn-recall-ledger.jsonl"
    for row in rows:
        assert "agent_id" not in row and "agent_type" not in row


# A harness budget so small that the tripwire block always exceeds it. That is
# the hook's own condition (len(parts) > budget - reserve) for deferring the WS4
# row into _ws4_rows until the pass settles; nothing else is stubbed.
_TINY_BUDGET = {"budget": 1, "head_budget": 1, "measured": True}


@pytest.mark.parametrize("fixture,expect", [(SUB, True), (MAIN, False), (MAIN_TWO, False)])
def test_deferred_ws4_row_carries_fields(tmp_path, monkeypatch, fixture, expect):
    _, _, rows = _run(tmp_path, fixture, monkeypatch,
                      patches={"get_harness_budgets": lambda *a, **k: dict(_TINY_BUDGET)})
    (row,) = rows["ws4"]
    assert "chain_rendered" in row  # the full row, not the minimal fallback
    if expect:
        assert row["agent_id"] == "a4091a71f52524e76"
        assert row["agent_type"] == "probe"
    else:
        assert "agent_id" not in row and "agent_type" not in row
