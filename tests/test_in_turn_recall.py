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
  8. near-miss (gate passes, nothing new to render) still stamps cooldown
  9. concurrent invocations — atomic cooldown claim (#1709): only one caller
     fires under contention
  10. lock contention suppresses immediately, never blocks
  11. missing/failed lock setup degrades to the unlocked path (never crashes)
  12. no explicit min_idf: effective threshold = min_idf_ratio * ln(n_docs) (#1734)
  13. explicit min_idf: passed through verbatim, ignoring graph size (#1734)
  14. n_docs == 0: silent, no extract_keywords/daemon call (#1734)
  15. lock degrade stamps state["lock_degraded"]/reason + emits a stderr
      tell (#1737) -- the silent fallback now has an observable trace
  16. the degrade flag is cleared on the next call once the lock recovers
      (#1737) -- a stale flag must not outlive the condition it reports

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
    # A 'nodes' table with >=1 current row is seeded too: since #1734, the
    # default (no explicit min_idf) path computes min_idf_ratio * ln(n_docs)
    # from a live COUNT(*) query, so the table must exist and be non-empty
    # for the default-config tests to reach the gate at all. Schema matches
    # test_tripwire_and_recall_compose's own CREATE TABLE IF NOT EXISTS so
    # the two don't conflict when both touch the same DB file.
    import sqlite3
    conn = sqlite3.connect(Path(tmp) / "knowledge.db")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS nodes (
               id TEXT PRIMARY KEY, type TEXT, claim TEXT, is_current INT,
               memory_status TEXT, metadata TEXT)"""
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('seed_1', 'observation', 'seed node', 1, 'active', '{}')"
    )
    conn.commit()
    conn.close()


def _stub_idf_capture(terms):
    """Install a fake engram_idf whose extract_keywords records the min_idf
    it was called with, into the returned dict (key 'min_idf')."""
    captured = {}

    def extract_keywords(conn, text, min_idf=4.0, top_k=5):
        captured["min_idf"] = min_idf
        return [(t, 7.5) for t in terms]

    fake = types.ModuleType("engram_idf")
    fake.extract_keywords = extract_keywords
    sys.modules["engram_idf"] = fake
    return captured


def _seed_node_count(db_path, n):
    """Replace the nodes table contents with exactly n current rows."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS nodes (
               id TEXT PRIMARY KEY, type TEXT, claim TEXT, is_current INT,
               memory_status TEXT, metadata TEXT)"""
    )
    conn.execute("DELETE FROM nodes")
    for i in range(n):
        conn.execute(
            "INSERT INTO nodes VALUES (?, 'observation', 'seed', 1, 'active', '{}')",
            (f"seed_{i}",),
        )
    conn.commit()
    conn.close()


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


def test_concurrent_calls_only_one_fires(tmp_path, monkeypatch):
    """Core regression for #1709: the cooldown claim must be atomic. Under
    concurrent PreToolUse invocations, multiple callers must NOT all read
    'no recent fire' before any of them stamps last_fire_ts — exactly one
    thread wins the non-blocking flock claim and queries the daemon; every
    other concurrent caller returns "" (suppressed, not blocked)."""
    import threading

    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=0)
    _stub_idf([["novelterm"]])

    daemon_calls = {"n": 0}
    call_lock = threading.Lock()

    def counting_daemon(q, top_k=5):
        with call_lock:
            daemon_calls["n"] += 1
        return _daemon_result(["ob_1"])

    monkeypatch.setattr(hook, "_query_recall_daemon", counting_daemon)

    n_workers = 8
    barrier = threading.Barrier(n_workers)
    results: list = [None] * n_workers

    def worker(i):
        barrier.wait()  # maximize concurrent overlap
        results[i] = hook.check_in_turn_recall("concurrent novelterm call")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert daemon_calls["n"] == 1, f"expected exactly 1 daemon query under contention, got {daemon_calls['n']}"
    fired = [r for r in results if r]
    suppressed = [r for r in results if r == ""]
    assert len(fired) == 1, f"expected exactly 1 winner, got {len(fired)}: {results}"
    assert len(suppressed) == n_workers - 1

    state = json.load(open(hook.STATE_PATH))
    assert state["last_fire_ts"] > 0


def test_lock_contention_suppresses_not_blocks(tmp_path, monkeypatch):
    """A caller that cannot acquire the lock (already held elsewhere) must
    return "" promptly — suppress, never block."""
    import fcntl
    import time

    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=0)
    _stub_idf([["zeta"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    lockpath = str(hook.STATE_PATH) + ".lock"
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        result = hook.check_in_turn_recall("zeta call while locked")
        elapsed = time.monotonic() - start
        assert result == ""
        assert elapsed < 1.0, "check_in_turn_recall blocked instead of suppressing on contention"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_missing_fcntl_degrades_to_unlocked(tmp_path, monkeypatch):
    """If lock setup fails entirely (fcntl unavailable / lockfile open
    fails), the function must still run the normal (unlocked) path — never
    crash, never silently disable the feature."""
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=0)
    _stub_idf([["omega"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    def broken_open(*a, **kw):
        raise OSError("simulated: lockfile open unavailable")

    monkeypatch.setattr(hook.os, "open", broken_open)

    out = hook.check_in_turn_recall("omega call")
    assert "ENGRAM in-turn recall" in out
    assert "ob_1" in out
    state = json.load(open(hook.STATE_PATH))
    assert state["last_fire_ts"] > 0


def test_lock_degrade_stamps_state_and_emits_stderr_tell(tmp_path, monkeypatch, capsys):
    """#1737: a lock-setup failure must leave an observable trace -- both a
    stderr line (hook logs, never shown to the model) and a state-file flag
    -- instead of the previously-silent fallback to unlocked."""
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=0)
    _stub_idf([["omega"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    def broken_open(*a, **kw):
        raise PermissionError("simulated: lockfile open unavailable")

    monkeypatch.setattr(hook.os, "open", broken_open)

    out = hook.check_in_turn_recall("omega call")
    assert "ENGRAM in-turn recall" in out, "degrade must still run the unlocked path, not go silent"

    state = json.load(open(hook.STATE_PATH))
    assert state["lock_degraded"] is True
    assert "PermissionError" in state["lock_degrade_reason"]

    captured = capsys.readouterr()
    assert "lock degraded to unlocked path" in captured.err
    assert "PermissionError" in captured.err


def test_lock_degrade_flag_cleared_once_lock_recovers(tmp_path, monkeypatch):
    """The lock_degraded flag reflects only the LATEST call -- a stale flag
    from an earlier degrade must not persist once the lock is healthy
    again, or a diagnostic reading the state file would misreport a
    recovered install as still-degraded."""
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=0)
    _stub_idf([["omega"], ["zeta"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    real_open = hook.os.open

    def broken_open(*a, **kw):
        raise PermissionError("simulated: lockfile open unavailable")

    monkeypatch.setattr(hook.os, "open", broken_open)
    hook.check_in_turn_recall("omega call")
    state = json.load(open(hook.STATE_PATH))
    assert state["lock_degraded"] is True

    monkeypatch.setattr(hook.os, "open", real_open)
    hook.check_in_turn_recall("zeta call")
    state = json.load(open(hook.STATE_PATH))
    assert "lock_degraded" not in state
    assert "lock_degrade_reason" not in state


def test_lock_degrade_persists_even_when_cooldown_gate_short_circuits(tmp_path, monkeypatch):
    """Reviewer-fairy catch (PR #1742 first review): a degraded call that
    ALSO hits the cooldown gate must still persist the flag -- the original
    implementation only stamped the in-memory state dict and relied on a
    later _write_state() call that several early-return gates (cooldown
    among them) skip entirely, so the flag could silently never reach disk
    under sustained cooldown-heavy traffic."""
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=9999)
    _stub_idf([["omega"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    # Prime the cooldown with a normal (non-degraded) fire first.
    hook.check_in_turn_recall("prime call")

    def broken_open(*a, **kw):
        raise PermissionError("simulated: lockfile open unavailable")

    monkeypatch.setattr(hook.os, "open", broken_open)

    # This call degrades AND immediately hits the just-primed cooldown gate
    # (9999s) -- the old code returned "" here without ever calling
    # _write_state(), so the flag never reached disk.
    result = hook.check_in_turn_recall("degraded call under cooldown")
    assert result == "", "sanity: cooldown gate should still suppress the render"

    state = json.load(open(hook.STATE_PATH))
    assert state["lock_degraded"] is True, (
        "degrade flag must persist even when the cooldown gate short-circuits "
        "the rest of the function"
    )


def test_lock_degrade_flag_clears_even_when_cooldown_gate_short_circuits(tmp_path, monkeypatch):
    """Symmetric case: once the lock recovers, a call that ALSO hits the
    cooldown gate must still clear a stale flag from an earlier degrade --
    otherwise a recovered install can misreport as still-degraded for as
    long as calls keep short-circuiting before a write."""
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), cooldown_seconds=9999)
    _stub_idf([["omega"], ["zeta"]])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    real_open = hook.os.open

    def broken_open(*a, **kw):
        raise PermissionError("simulated: lockfile open unavailable")

    monkeypatch.setattr(hook.os, "open", broken_open)
    hook.check_in_turn_recall("degraded prime call")
    state = json.load(open(hook.STATE_PATH))
    assert state["lock_degraded"] is True

    monkeypatch.setattr(hook.os, "open", real_open)
    # Lock is healthy again, but the just-primed cooldown (9999s) will
    # short-circuit this call before it ever reaches _write_state().
    result = hook.check_in_turn_recall("recovered call under cooldown")
    assert result == "", "sanity: cooldown gate should still suppress the render"

    state = json.load(open(hook.STATE_PATH))
    assert "lock_degraded" not in state, (
        "a recovered lock must clear the stale flag even when the cooldown "
        "gate short-circuits the rest of the function"
    )


# ---------------------------------------------------------------------------
# #1734 — graph-size-relative min_idf default
# ---------------------------------------------------------------------------

def test_default_min_idf_is_ratio_of_ln_n_docs(tmp_path, monkeypatch):
    """No explicit min_idf configured: effective threshold is
    min_idf_ratio (default 0.7) * ln(n_docs), computed from a live
    COUNT(*) of current nodes -- not the old fixed 6.0 default."""
    import math

    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path))
    _seed_node_count(hook.DB_PATH, 12)
    captured = _stub_idf_capture(["novelterm"])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    hook.check_in_turn_recall("some novel query")

    expected = 0.7 * math.log(12)
    assert abs(captured["min_idf"] - expected) < 1e-6


def test_explicit_min_idf_passed_through_verbatim(tmp_path, monkeypatch):
    """An explicit min_idf in config is an absolute override honored
    verbatim, regardless of graph size -- existing configs keep exact
    pre-#1734 behavior."""
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path), min_idf=4.0)
    _seed_node_count(hook.DB_PATH, 12)
    captured = _stub_idf_capture(["novelterm"])
    monkeypatch.setattr(hook, "_query_recall_daemon", lambda q, top_k=5: _daemon_result(["ob_1"]))

    hook.check_in_turn_recall("some novel query")

    assert captured["min_idf"] == 4.0


def test_zero_docs_is_silent_no_daemon_call(tmp_path, monkeypatch):
    """n_docs == 0 (empty graph): return "" without calling extract_keywords
    or the recall daemon -- nothing to recall anyway."""
    hook = _load_hook(str(tmp_path))
    _enable(hook, str(tmp_path))
    _seed_node_count(hook.DB_PATH, 0)

    def failing_extract_keywords(conn, text, min_idf=4.0, top_k=5):
        raise AssertionError("extract_keywords must not be called when n_docs == 0")

    fake = types.ModuleType("engram_idf")
    fake.extract_keywords = failing_extract_keywords
    sys.modules["engram_idf"] = fake

    daemon_calls = {"n": 0}

    def counting_daemon(q, top_k=5):
        daemon_calls["n"] += 1
        return _daemon_result(["ob_1"])

    monkeypatch.setattr(hook, "_query_recall_daemon", counting_daemon)

    assert hook.check_in_turn_recall("some query") == ""
    assert daemon_calls["n"] == 0
