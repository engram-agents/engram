"""RED-FIRST contract cells for #3741: the session-start calibration block reads through the owner.

Spec: the #3741 contract (cells C1..C11), Borges's ruling and his revised item 4 on the issue, and
`docs/internal/2652-security-slice-design.md` section 3.6 (typed-op input bounds: REFUSE, never clamp).

The calibration block of `engram-session-start-hook.py` (`EngramClient().call("engram_stats", ...)`)
moves, behind U9's flag `engramd.session_start_reads_via_owner`, to a new typed owner op
`confidence_by_type {modes}`. Shared code in `engram_stats.py` makes the op and `engram_stats` equal by
construction. Flag OFF gets a read-only `user_version > 0` guard before the client is constructed.

Every cell here is RED on the tree it was written against (U9 head 2a061c8b) for the reason the spec
names -- the thing is MISSING -- except the guard cells that pin EXISTING behaviour (C1b, C10 and the
control legs), which are GREEN now and must stay green. New symbols are looked up lazily, inside each
test, through helpers that `pytest.fail` with a named message: a collection error would redden the
whole file for the wrong reason.

Helpers are imported from U9's `test_session_start_owner_read_3735.py` as they are; nothing there is
refactored. The one deliberate edit to that file is the flip of its
`test_a2_characterization_...` cell into its post-#3741 form.

Mutation table (each must turn the named cells red, the rest green):
  M1 flag ON keeps the old EngramClient path        -> C6, C5 red; C1 green
  M2 the op builds its own window                   -> C1 or C2 red
  M3 drop the flag-OFF guard                        -> C8 (OFF leg), C9 red
  M4 clamp an over-long list to 4                   -> C3 too_many red
  M5 membership test before the isinstance(str)     -> C3 not_str red (TypeError)
  M6 fall back to EngramClient on owner failure     -> C7 red
  M7 the op records tool_timing                     -> C5 red
  M8 _confidence_by_type reads r["confidence"]      -> C1 (owner legs) red
  M9 drop the _configure_paths call                 -> C11 red
  M10 silence unknown_operation (skip the record)   -> C7b(i) red
  M11 decide the wording by message text            -> C7b(iii) red
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_session_start_owner_read_3735 as u9
from test_session_start_owner_read_3735 import (  # noqa: F401 -- fixtures and helpers reused as they are
    BLOCK_MARKERS,
    FLAG_KEY,
    NOTICE_PHRASE,
    OWNER_STDERR_RE,
    SQL_FOCUS,
    AttrOpens,
    FakeOwner,
    Script,
    _rejected,
    _restore_imports,
    _unavailable,
    attributed_to_six,
    build_empty_store,
    build_store,
    flattened_root,
    full_fire_setup,
    home,
    load_ss,
    owner_of,
    run_hook_subprocess,
    run_main,
    seed_marker,
    set_flag,
    set_sleep_marker,
    stub_modules,
)
from _owner_platform import requires_linux_owner

import engram_core  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

REPO_ROOT = u9.REPO_ROOT
SRC_ENGRAM = u9._SRC_ENGRAM

MODES4 = ["all", "1-turn", "7-turn", "30-turn"]
# microseconds on purpose: the cutoff string must TRUNCATE them (the cutoff's own second, cell C2)
NOW_PIN = datetime(2026, 10, 13, 12, 0, 30, 500000, tzinfo=timezone.utc)
CAL_MARK = "[ENGRAM Calibration"


# =========================================================================================
# lazy lookups of the symbols #3741 adds (a missing one is a NAMED failure, never a collect error)
# =========================================================================================

def stats_mod():
    import engram_stats
    return engram_stats


def stats_attr(name: str):
    mod = stats_mod()
    if not hasattr(mod, name):
        pytest.fail(f"engram_stats.{name} is missing: the #3741 shared code is not implemented yet")
    return getattr(mod, name)


def pin_clock(monkeypatch, now: datetime) -> None:
    """The ONE clock both paths share: `engram_stats._now_utc` (spec C-A.2)."""
    stats_attr("_now_utc")
    monkeypatch.setattr(stats_mod(), "_now_utc", lambda: now)


@pytest.fixture(autouse=True)
def _core_paths_restored():
    """The owner (after #3741) and the reference leg both call `engram_core._configure_paths`, which
    sets module globals of this very process: put them back so no other test file inherits them."""
    before = engram_core.DATA_DIR
    yield
    engram_core._configure_paths(before)


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    """The reference leg builds a fresh store through `engram_stats` -> `_get_db`, whose one-time setup
    asks the embedder whether sentence-transformers is importable. Calibration never uses embeddings,
    and importing that stack crashed the pr-checks runner (SIGSEGV in C2's gate leg, run 38435680040).
    Set for every cell here; owner and hook subprocesses inherit it."""
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")


# =========================================================================================
# the fixture store
# =========================================================================================

def _iso(base: datetime, hours_ago: float) -> str:
    return (base - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# (id, type, confidence, hours before `base`, status, is_current)
ROWS = [
    ("of_1", "observation_factual", 0.95, 1, "active", 1),     # in every window
    ("of_2", "observation_factual", 0.85, 10, "active", 1),    # 1-turn and wider
    ("of_3", "observation_factual", 0.70, 48, "active", 1),    # 7-turn and wider
    ("of_4", "observation_factual", 0.40, 400, "active", 1),   # 30-turn and 'all'
    ("of_5", "observation_factual", 0.35, 2000, "active", 1),  # outside every window ('all' only)
    ("dv_1", "derivation", 0.75, 2, "active", 1),
    ("dv_2", "derivation", 0.55, 100, "active", 1),
    ("cj_1", "conjecture", 0.40, 3, "active", 1),
    ("ax_1", "axiom", 1.0, 3000, "active", 1),                 # outside every window
    # excluded by the shared filter, inside the windows
    ("of_ret", "observation_factual", 0.99, 1, "retracted", 1),
    ("dv_sup", "derivation", 0.11, 1, "active", 0),            # superseded (is_current = 0)
    ("of_old_ret", "observation_factual", 0.01, 2000, "retracted", 1),
]
# (lesson / definition: no row at all = the empty types -- a lesson's confidence is cleared by the store
#  anyway, so C2 uses `definition` for its extra nodes; of_null below = NULL confidence)


# Nodes just INSIDE each window's edge under the pinned clock (the window's own hours minus half an
# hour). A narrowing of a window by an hour (24 -> 23, 168 -> 167, 720 -> 719) crosses exactly one of
# them, so C1 / C1b see it. Confidence-bearing types that reach `by_type` (a lesson would not: the store
# clears a lesson's confidence). Seeded by `pinned_store` only: `live_store` keeps its far-from-boundary
# promise for the real-clock hook cells.
EDGE_ROWS = [
    ("of_edge24", "observation_factual", 0.60, 23.5, "active", 1),    # 1-turn and wider
    ("dv_edge168", "derivation", 0.65, 167.5, "active", 1),           # 7-turn and wider
    ("cj_edge720", "conjecture", 0.50, 719.5, "active", 1),           # 30-turn and 'all'
]


def seed_confidence_nodes(db: Path, base: datetime, extra=()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        for nid, ntype, conf, hours, status, cur in list(ROWS) + list(extra):
            created = hours if isinstance(hours, str) else _iso(base, hours)
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, confidence, status, is_current) "
                "VALUES (?,?,?,?,?,?,?)", (nid, ntype, f"claim {nid}", created, conf, status, cur))
        conn.execute("INSERT INTO nodes (id, type, claim, created_at, confidence) VALUES "
                     "('of_null','observation_factual','null confidence',?,NULL)", (_iso(base, 1),))
        conn.commit()
    finally:
        conn.close()


def write_config(home: Path, *, flag=None, turn=None) -> None:
    cfg: dict = {}
    if flag is not None:
        cfg["engramd"] = {FLAG_KEY: flag}
    if turn is not None:
        cfg["memory"] = {"current_turn": turn}
    (home / "config.json").write_text(json.dumps(cfg))


def pinned_store(home: Path, *, turn=17, extra=()) -> Path:
    """An empty real-schema store plus the fixture nodes, dated relative to NOW_PIN."""
    db = build_empty_store(home)
    seed_confidence_nodes(db, NOW_PIN, list(EDGE_ROWS) + list(extra))
    write_config(home, turn=turn)
    return db


def live_store(home: Path, *, flag, turn=41) -> Path:
    """A U9 store (everything the six functions read) plus fixture nodes dated relative to the real
    clock (the hook reads the real clock; every node sits far from every window boundary)."""
    db = build_store(home)
    seed_confidence_nodes(db, datetime.now(timezone.utc))
    write_config(home, flag=flag, turn=turn)
    return db


def tool_timing_rows(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        try:
            return conn.execute("SELECT count(*) FROM tool_timing").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def reference_by_type(db: Path, tmp: Path, modes) -> dict:
    """`engram_stats(mode=m, sections=["confidence"]).confidence.by_type`, on a COPY of the store
    (the reference path opens through `_get_db`, which migrates and may write)."""
    stats = stats_mod()
    refhome = Path(tempfile.mkdtemp(prefix="ref-", dir=str(tmp)))
    src = sqlite3.connect(str(db))
    dst = sqlite3.connect(str(refhome / "knowledge.db"))
    src.backup(dst)
    dst.close()
    src.close()
    if (db.parent / "config.json").exists():
        shutil.copy(db.parent / "config.json", refhome / "config.json")
    engram_core._configure_paths(refhome)
    return {m: json.loads(stats._stats_impl(m, ["confidence"]))["confidence"]["by_type"] for m in modes}


# =========================================================================================
# asking an owner
# =========================================================================================

def ask(home: Path, method: str, params: dict, timeout: float = 5.0):
    return ec.read(method, params, timeout=timeout, socket_path=ec.socket_path_for_home(home))


def ok_result(res):
    if not isinstance(res, ec.Ok):
        pytest.fail(f"the owner did not answer ok: {res!r}")
    return res.result


OWNER_SCRIPT = r'''
import json, sys
from pathlib import Path
home, reqs_path, out_path, pin = sys.argv[1:5]
import engramd_owner as eo, engramd_client as ec
if pin:
    from datetime import datetime
    import engram_core, engram_stats
    _now = datetime.fromisoformat(pin)
    engram_stats._now_utc = lambda: _now
owner = eo.EngramdOwner(Path(home), request_deadline=8.0)
owner.start(install_signal_handlers=False)
out = []
try:
    for method, params in json.load(open(reqs_path)):
        res = ec.read(method, params, timeout=8.0, socket_path=owner.socket_path)
        out.append({"type": type(res).__name__, "result": getattr(res, "result", None),
                    "reason": getattr(res, "reason", None), "detail": getattr(res, "detail", None)})
finally:
    owner.stop()
json.dump(out, open(out_path, "w"))
'''


def owner_subprocess(home: Path, tmp: Path, requests, *, env_home=None, pin=None) -> list:
    """A REAL owner in a fresh interpreter, serving `requests` ([method, params] pairs); the process
    env `ENGRAM_HOME` is `env_home` (default: the owner's own home). `pin`: an ISO instant the child
    installs as `engram_stats._now_utc` before the owner starts."""
    script, reqs, out = tmp / "owner3741.py", tmp / "reqs3741.json", tmp / "out3741.json"
    script.write_text(OWNER_SCRIPT)
    reqs.write_text(json.dumps(requests))
    out.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR")}
    env["ENGRAM_HOME"] = str(env_home or home)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), SRC_ENGRAM])
    r = subprocess.run([sys.executable, str(script), str(home), str(reqs), str(out), pin or ""],
                       capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=120)
    assert out.exists(), (r.returncode, r.stdout[-2000:], r.stderr[-2000:])
    return json.loads(out.read_text())


def child_ok(reply: dict):
    if reply["type"] != "Ok":
        pytest.fail(f"the owner did not answer ok: {reply}")
    return reply["result"]


# =========================================================================================
# C1 -- the op equals engram_stats, all four modes, three legs
# =========================================================================================

LEGS = [
    pytest.param("owner-inproc", id="real-owner-in-process", marks=requires_linux_owner),
    pytest.param("owner-subprocess", id="real-owner-subprocess", marks=requires_linux_owner),
    pytest.param("gate-conn", id="gate-connection"),
]


def op_by_mode(leg, home, db, tmp, monkeypatch, modes=MODES4):
    """-> the op's reply `result` for `modes` on `leg`. owner-inproc / owner-subprocess go over the
    wire; gate-conn runs the shared function on a connection shaped like the owner's client-read
    connection (NO row_factory, the authorizer floor installed)."""
    if leg == "owner-inproc":
        pin_clock(monkeypatch, NOW_PIN)
        with owner_of("real", home):
            return ok_result(ask(home, "confidence_by_type", {"modes": list(modes)}))
    if leg == "owner-subprocess":
        rep = owner_subprocess(home, tmp, [["confidence_by_type", {"modes": list(modes)}]],
                               pin=NOW_PIN.isoformat())
        return child_ok(rep[0])
    fn = stats_attr("confidence_by_type")
    copy = tmp / "gate-copy.db"
    src, dst = sqlite3.connect(str(db)), sqlite3.connect(str(copy))
    src.backup(dst)
    dst.close()
    src.close()
    conn = sqlite3.connect(str(copy), check_same_thread=False)
    conn.set_authorizer(eo._client_read_authorizer)
    try:
        assert conn.row_factory is None
        return {"by_mode": fn(conn, list(modes), NOW_PIN)}
    finally:
        conn.close()


@pytest.mark.parametrize("leg", LEGS)
def test_c1_the_op_equals_engram_stats_for_every_mode(leg, home, tmp_path, monkeypatch):
    """C1. `by_mode[m].by_type` == `engram_stats(mode=m, sections=["confidence"]).confidence.by_type`
    for all four modes, with ONE pinned clock. The fixture holds an empty type, a retracted node, a
    superseded (is_current = 0) node, a NULL-confidence node and nodes inside and outside every
    window. The in-process owner and the subprocess owner both read through a connection with no
    `row_factory`; `gate-connection` is the shared function on such a connection under the owner's
    authorizer floor.
    Red today: the op / `engram_stats.confidence_by_type` / `_now_utc` do not exist.
    Mutation: M2 (own window), M8 (r["confidence"]) -> red."""
    db = pinned_store(home)
    pin_clock(monkeypatch, NOW_PIN)
    reference = reference_by_type(db, tmp_path, MODES4)
    assert reference["all"], "fixture sanity: the reference must have data"
    assert "lesson" not in reference["all"] and "definition" not in reference["all"]  # the empty types
    # window sanity: the fixture really distinguishes the modes
    assert len({json.dumps(reference[m], sort_keys=True) for m in MODES4}) == 4, reference
    # the retracted / superseded / NULL nodes are not in the numbers
    assert reference["all"]["observation_factual"]["n"] == 6
    assert reference["all"]["derivation"]["n"] == 3
    # each edge node sits inside exactly the windows it should (a 1-hour narrowing would drop it)
    assert reference["1-turn"]["observation_factual"]["n"] == 3
    assert reference["7-turn"]["derivation"]["n"] == 3
    assert reference["30-turn"]["conjecture"]["n"] == 2
    got = op_by_mode(leg, home, db, tmp_path, monkeypatch)
    by_mode = got["by_mode"]
    assert set(by_mode) == set(MODES4)
    for m in MODES4:
        assert set(by_mode[m]) == {"by_type"}, (m, by_mode[m])
        assert by_mode[m]["by_type"] == reference[m], (leg, m, by_mode[m]["by_type"], reference[m])
    if leg != "gate-conn":
        assert got["current_turn"] == 17


# =========================================================================================
# C1b -- engram_stats is unchanged by the extraction (GREEN now: guards the refactor)
# =========================================================================================

# Captured from origin/dev (511195b3, the tree BEFORE any #3741 code: no `_window_for_mode`, no
# `confidence_by_type`) in a detached worktree: `engram_stats._stats_impl` over `pinned_store()` (the
# ROWS fixture plus EDGE_ROWS) with the clock pinned to NOW_PIN by patching `engram_stats.datetime`.
# Regenerated this way when EDGE_ROWS were added -- never from the #3741 code.
GOLDEN_STATS = {
    'all': (
        '{"confidence": {"by_type": {"observation_factual": {"n": 6, "mean": 0.642, "p25": 0.45, "p50": 0.65, "p75": 0.81, "p90": 0.9, "p95": 0.92}, "derivation": {"n": 3, "mean": 0.65, "p25": 0.6, "p50": 0.65, "p75": 0.7, "p90": 0.73, "p95": 0.74}, "conjecture": {"n": 2, "mean": 0.45, "p25": 0.43, "p50": 0.45, "p75": 0.47, "p90": 0.49, "p95": 0.49}, "axiom": {"n": 1, "mean": 1.0, "p25": 1.0, "p50": 1.0, "p75": 1.0, "p90": 1.0, "p95": 1.0}}, "by_quote_type": {}, "by_reasoning_type": {"legacy_untyped": {"n": 3, "mean": 0.65}}, "by_source_class": {"external": {"n": 6, "mean": 0.642}}}}'
    ),
    '1-turn': (
        '{"window": {"mode": "1-turn", "approx_hours": 24, "cutoff_iso": "2026-10-12T12:00:30Z"}, "confidence": {"by_type": {"observation_factual": {"n": 3, "mean": 0.8, "p25": 0.72, "p50": 0.85, "p75": 0.9, "p90": 0.93, "p95": 0.94}, "derivation": {"n": 1, "mean": 0.75, "p25": 0.75, "p50": 0.75, "p75": 0.75, "p90": 0.75, "p95": 0.75}, "conjecture": {"n": 1, "mean": 0.4, "p25": 0.4, "p50": 0.4, "p75": 0.4, "p90": 0.4, "p95": 0.4}}, "by_quote_type": {}, "by_reasoning_type": {"legacy_untyped": {"n": 1, "mean": 0.75}}, "by_source_class": {"external": {"n": 3, "mean": 0.8}}}}'
    ),
    '7-turn': (
        '{"window": {"mode": "7-turn", "approx_hours": 168, "cutoff_iso": "2026-10-06T12:00:30Z"}, "confidence": {"by_type": {"observation_factual": {"n": 4, "mean": 0.775, "p25": 0.67, "p50": 0.77, "p75": 0.88, "p90": 0.92, "p95": 0.93}, "derivation": {"n": 3, "mean": 0.65, "p25": 0.6, "p50": 0.65, "p75": 0.7, "p90": 0.73, "p95": 0.74}, "conjecture": {"n": 1, "mean": 0.4, "p25": 0.4, "p50": 0.4, "p75": 0.4, "p90": 0.4, "p95": 0.4}}, "by_quote_type": {}, "by_reasoning_type": {"legacy_untyped": {"n": 3, "mean": 0.65}}, "by_source_class": {"external": {"n": 4, "mean": 0.775}}}}'
    ),
    '30-turn': (
        '{"window": {"mode": "30-turn", "approx_hours": 720, "cutoff_iso": "2026-09-13T12:00:30Z"}, "confidence": {"by_type": {"observation_factual": {"n": 5, "mean": 0.7, "p25": 0.6, "p50": 0.7, "p75": 0.85, "p90": 0.91, "p95": 0.93}, "derivation": {"n": 3, "mean": 0.65, "p25": 0.6, "p50": 0.65, "p75": 0.7, "p90": 0.73, "p95": 0.74}, "conjecture": {"n": 2, "mean": 0.45, "p25": 0.43, "p50": 0.45, "p75": 0.47, "p90": 0.49, "p95": 0.49}}, "by_quote_type": {}, "by_reasoning_type": {"legacy_untyped": {"n": 3, "mean": 0.65}}, "by_source_class": {"external": {"n": 5, "mean": 0.7}}}}'
    ),
}


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW_PIN if tz is None else NOW_PIN.astimezone(tz)


@pytest.mark.parametrize("mode", MODES4)
def test_c1b_engram_stats_output_is_identical_before_and_after_the_extraction(mode, home, tmp_path, monkeypatch):
    """C1b. `engram_stats(mode, sections=["confidence"])` returns byte-for-byte the output this tree
    produced BEFORE the extraction, for every valid mode, on the same fixture with a pinned clock.
    The clock is pinned by replacing the module's `datetime` name with a fixed subclass: that works
    on the current code (`datetime.now(timezone.utc)` inline in `_stats_impl`) AND after the
    refactor (`_now_utc` calls `datetime.now(timezone.utc)`), so the golden stays meaningful and the
    cell is green before and after. GREEN now.
    Mutation: any change to the by_type loop, the cutoff format or the hours table -> red."""
    db = pinned_store(home)
    stats = stats_mod()
    monkeypatch.setattr(stats, "datetime", _FixedDatetime)
    refhome = Path(tempfile.mkdtemp(prefix="ref-", dir=str(tmp_path)))
    src, dst = sqlite3.connect(str(db)), sqlite3.connect(str(refhome / "knowledge.db"))
    src.backup(dst)
    dst.close()
    src.close()
    shutil.copy(home / "config.json", refhome / "config.json")
    engram_core._configure_paths(refhome)
    got = stats._stats_impl(mode, ["confidence"])
    assert got == GOLDEN_STATS[mode], got


# =========================================================================================
# C2 -- the cutoff's own second
# =========================================================================================

C2_EXTRA = [
    # the 1-turn cutoff is NOW_PIN - 24 h = 2026-10-12T12:00:30.5 -> '2026-10-12T12:00:30Z'
    ("df_c2a", "definition", 0.20, "2026-10-12T12:00:30.123456+00:00", "active", 1),   # cutoff's second: EXCLUDED
    ("df_c2b", "definition", 0.30, "2026-10-12T12:00:30+00:00", "active", 1),          # cutoff's second: EXCLUDED
    ("df_c2c", "definition", 0.90, "2026-10-12T12:00:31.000001+00:00", "active", 1),   # next second: INCLUDED (control)
]


@pytest.mark.parametrize("leg", LEGS)
def test_c2_a_node_in_the_cutoffs_own_second_is_excluded_on_both_paths(leg, home, tmp_path, monkeypatch):
    """C2. The 1-turn cutoff string is '2026-10-12T12:00:30Z' and is compared as a STRING. A node at
    '...:30.123456+00:00' and one at '...:30+00:00' both compare below it and are EXCLUDED; the node
    at '...:31.000001+00:00' compares above and is INCLUDED. This pins today's behaviour on both
    paths (it does not decide it): the definition type is empty in the fixture otherwise, so the
    1-turn definition bucket is exactly the control node.
    Red today: the op does not exist. Mutation: M2 (other cutoff format / own window) -> red."""
    db = pinned_store(home, extra=C2_EXTRA)
    pin_clock(monkeypatch, NOW_PIN)
    reference = reference_by_type(db, tmp_path, ["1-turn"])["1-turn"]
    one = {"n": 1, "mean": 0.9, "p25": 0.9, "p50": 0.9, "p75": 0.9, "p90": 0.9, "p95": 0.9}
    assert reference["definition"] == one, reference["definition"]  # engram_stats path: the two are excluded
    got = op_by_mode(leg, home, db, tmp_path, monkeypatch, modes=["1-turn", "all"])
    assert got["by_mode"]["1-turn"]["by_type"]["definition"] == one, got["by_mode"]["1-turn"]["by_type"].get("definition")
    assert got["by_mode"]["all"]["by_type"]["definition"]["n"] == 3  # control: all three exist


C2B_EXTRA = [
    ("df_c2d", "definition", 0.40, "2026-10-12T12:00:30Z", "active", 1),               # EQUAL to the cutoff: INCLUDED
    ("df_c2c", "definition", 0.90, "2026-10-12T12:00:31.000001+00:00", "active", 1),   # next second: INCLUDED (control)
]


@pytest.mark.parametrize("leg", LEGS)
def test_c2b_a_node_whose_created_at_equals_the_cutoff_is_included_on_both_paths(leg, home, tmp_path, monkeypatch):
    """C2b (follow-up to the colleague read of #3759). The window filter is `created_at >= cutoff`;
    no other cell has a row whose `created_at` EQUALS the cutoff string, so changing `>=` to `>`
    survived. Real rows are written `...+00:00` and never equal a `...Z` cutoff, so this pins the
    operator itself, on both paths: a row stamped exactly '2026-10-12T12:00:30Z' is in the 1-turn
    window. Mutation: `>=` -> `>` in `_window_for_mode` -> the 1-turn definition bucket drops to n=1 -> red."""
    db = pinned_store(home, extra=C2B_EXTRA)
    pin_clock(monkeypatch, NOW_PIN)
    reference = reference_by_type(db, tmp_path, ["1-turn"])["1-turn"]
    assert reference["definition"]["n"] == 2, reference["definition"]
    got = op_by_mode(leg, home, db, tmp_path, monkeypatch, modes=["1-turn"])
    assert got["by_mode"]["1-turn"]["by_type"]["definition"]["n"] == 2, got["by_mode"]["1-turn"]["by_type"].get("definition")


# =========================================================================================
# C3 -- validation table
# =========================================================================================

SENT = "SENTINEL-3741-echo"
REFUSALS = [
    ("cbt_params_shape", {}),
    ("cbt_params_shape", {"modes": ["all"], "extra": 1}),
    ("cbt_params_shape", {"mode": ["all"]}),
    ("cbt_params_shape", []),
    ("cbt_params_shape", "all"),
    ("cbt_params_shape", None),
    # first-violation-wins ORDER (spec C-B: shape, not_list, empty, too_many, not_str, unknown, duplicate)
    ("cbt_params_shape", {"modes": "all", "extra": 1}),                      # shape before not_list
    ("cbt_modes_too_many", {"modes": ["all", 7, "x", "y", "z"]}),            # length before element type
    ("cbt_modes_too_many", {"modes": ["all", "all", "all", "all", "all"]}),  # length before duplicate
    ("cbt_mode_not_str", {"modes": [7, "bogus"]}),                           # a non-str first: not_str
    ("cbt_mode_not_str", {"modes": [1, 1]}),                                 # not_str, not duplicate
    ("cbt_mode_unknown", {"modes": ["bogus", "bogus"]}),                     # unknown, not duplicate
    # element-order rule (spec C-B): ONE pass in list order, each element not_str -> unknown -> duplicate
    ("cbt_mode_unknown", {"modes": ["bogus", 7]}),                           # the unknown comes first
    ("cbt_mode_duplicate", {"modes": ["all", "all", 7]}),                    # the duplicate comes first
    ("cbt_modes_not_list", {"modes": "all"}),
    ("cbt_modes_not_list", {"modes": {"all": 1}}),
    ("cbt_modes_not_list", {"modes": None}),
    ("cbt_modes_not_list", {"modes": 7}),
    ("cbt_modes_empty", {"modes": []}),
    ("cbt_modes_too_many", {"modes": ["all", "1-turn", "7-turn", "30-turn", "all"]}),
    ("cbt_modes_too_many", {"modes": ["x"] * 5}),
    ("cbt_mode_not_str", {"modes": ["all", 7]}),
    ("cbt_mode_not_str", {"modes": [["all"]]}),
    ("cbt_mode_not_str", {"modes": [{"a": 1}]}),
    ("cbt_mode_not_str", {"modes": [None]}),
    ("cbt_mode_not_str", {"modes": [True]}),
    ("cbt_mode_unknown", {"modes": ["bogus"]}),
    ("cbt_mode_unknown", {"modes": ["ALL"]}),
    ("cbt_mode_unknown", {"modes": [""]}),
    ("cbt_mode_unknown", {"modes": [SENT]}),
    ("cbt_mode_duplicate", {"modes": ["all", "all"]}),
    ("cbt_mode_duplicate", {"modes": ["7-turn", "1-turn", "7-turn"]}),
]


@pytest.fixture(scope="module")
def table_owner():
    """ONE real owner (fixture store) for the whole table, with a statement counter on the
    client-read connection (the connection every typed op prepares on)."""
    d = Path(tempfile.mkdtemp(prefix="cbt3-"))
    try:
        pinned_store(d)
        with owner_of("real", d) as o:
            seen: list = []
            o._client_read_conn.set_trace_callback(seen.append)
            yield d, o, seen
    finally:
        shutil.rmtree(d, ignore_errors=True)


@requires_linux_owner
@pytest.mark.parametrize("reason,params", REFUSALS, ids=[f"{r}-{i}" for i, (r, _p) in enumerate(REFUSALS)])
def test_c3_every_invalid_request_is_refused_by_name_before_any_statement(reason, params, table_owner):
    """C3. Each violation has its own name, nothing is echoed, and NOTHING is prepared on the store
    connection (trace-callback statement count 0). An over-long list is REFUSED, never clamped
    (section 3.6). The element type is checked before membership (a list / dict element must not
    raise inside the owner).
    Red today: every one of these answers `unknown_operation`.
    Mutation: M4 (clamp to 4) -> too_many red; M5 (membership before isinstance) -> not_str red."""
    d, _o, seen = table_owner
    before = len(seen)
    res = ask(d, "confidence_by_type", params)
    assert isinstance(res, ec.Rejected), res
    assert res.reason == reason, (res.reason, res.detail)
    assert SENT not in repr(res), "the refusal must not echo the caller's text"
    assert len(seen) == before, seen[before:]


@requires_linux_owner
def test_c3_control_a_valid_request_is_served_and_does_prepare_statements(table_owner):
    """C3 success control: the same wire, valid modes -> ok with the documented shape, and the trace
    counter DOES move (so a count of 0 in the refusal rows is a measurement, not a dead instrument).
    Red today: `unknown_operation`."""
    d, _o, seen = table_owner
    before = len(seen)
    result = ok_result(ask(d, "confidence_by_type", {"modes": ["all", "7-turn"]}))
    assert set(result) == {"current_turn", "by_mode"}, result
    assert set(result["by_mode"]) == {"all", "7-turn"}
    assert len(seen) > before


# =========================================================================================
# C4 -- _window_for_mode is the only place a mode becomes SQL
# =========================================================================================

@pytest.mark.parametrize("bad", ["bogus", 7, None, ["all"], {"a": 1}, True], ids=repr)
def test_c4_window_for_mode_raises_valueerror_for_anything_but_a_known_mode(bad):
    """C4. `_window_for_mode(mode, now)` raises ValueError (not KeyError / TypeError) on a unknown
    string, an int, None, a list, a dict and a bool.
    Red today: the helper does not exist (the old code died with KeyError / TypeError deep in
    `_stats_impl`)."""
    fn = stats_attr("_window_for_mode")
    with pytest.raises(ValueError):
        fn(bad, NOW_PIN)


def test_c4_control_valid_modes_become_the_known_filter_and_cutoff():
    """C4 control: 'all' -> no filter; '7-turn' -> the string-compared cutoff in the unchanged format."""
    fn = stats_attr("_window_for_mode")
    assert fn("all", NOW_PIN) == ("", [])
    assert fn("7-turn", NOW_PIN) == (" AND created_at >= ?", ["2026-10-06T12:00:30Z"])


# =========================================================================================
# C5 / C6 -- flag ON: read-only, zero opens, nothing imported
# =========================================================================================

SPY_LINE = 'calls.append([method, (params or {}).get("sql") if method == "read" else None])'
SPY_LINE_WITH_PARAMS = 'calls.append([method, (params or {}).get("sql") if method == "read" else None, params])'
_MODS = u9.RUNNER.replace('json.dump({"rc": rc,', 'json.dump({"modules": sorted(sys.modules), "rc": rc,')
assert _MODS != u9.RUNNER, "U9's RUNNER changed shape: the sys.modules dump could not be spliced in"
# the spy also records each request's PARAMS (third element), so a cell can pin what the hook asked
MODS_RUNNER = _MODS.replace(SPY_LINE, SPY_LINE_WITH_PARAMS)
assert MODS_RUNNER != _MODS, "U9's RUNNER changed shape: the params-recording spy could not be spliced in"


ORACLE_SCRIPT = r'''
import importlib.util, json, sys, types
root, out_path = sys.argv[1], sys.argv[2]
sys.modules["context_tracker"] = types.SimpleNamespace(write_baseline=lambda **k: None)
sys.path.insert(0, root)
spec = importlib.util.spec_from_file_location("ss_hook_oracle", root + "/hooks/engram-session-start-hook.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
from engram_client import EngramClient
c = EngramClient()
a = c.call("engram_stats", {"payload_json": json.dumps({"sections": ["confidence", "memory"]})})
d = c.call("engram_stats", {"payload_json": json.dumps({"mode": "7-turn", "sections": ["confidence"]})})
turn = a.get("memory", {}).get("current_turn", 0)
block = mod.format_calibration_block(a.get("confidence", {}), d.get("confidence", {}), turn)
json.dump({"block": block, "turn": turn}, open(out_path, "w"))
'''


def oracle_calibration_block(home: Path, tmp: Path) -> str:
    """The calibration block the CURRENT code renders for `home`'s store: the same two `engram_stats`
    calls the hook's EngramClient path makes, then the hook module's own `format_calibration_block`.
    No git history: it runs the checked-out runtime in a fresh interpreter (EngramClient purges and
    re-imports `engram_*` modules, so not in this process) on a backup COPY of the store (the client
    writes `tool_timing`), with the home's config.json copied beside it for `current_turn`."""
    base = Path(tempfile.mkdtemp(prefix="calref-", dir=str(tmp)))
    refhome = base / "home"
    refhome.mkdir()
    src, dst = sqlite3.connect(str(home / "knowledge.db")), sqlite3.connect(str(refhome / "knowledge.db"))
    src.backup(dst)
    dst.close()
    src.close()
    if (home / "config.json").exists():
        shutil.copy(home / "config.json", refhome / "config.json")
    root = flattened_root(base / "root")
    script, out = base / "oracle3741.py", base / "oracle-out3741.json"
    script.write_text(ORACLE_SCRIPT)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH", "ENGRAM_SESSION_ROLE")}
    env["ENGRAM_HOME"] = str(refhome)
    env["HOME"] = str(base / "subhome")
    (base / "subhome").mkdir()
    r = subprocess.run([sys.executable, str(script), str(root), str(out)], capture_output=True, text=True,
                       env=env, cwd=str(base), timeout=120)
    assert out.exists(), (r.returncode, r.stdout[-2000:], r.stderr[-2000:])
    return json.loads(out.read_text())["block"]


def _fresh_data_version(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("PRAGMA data_version").fetchone()[0]
    finally:
        conn.close()


def _digest(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


@requires_linux_owner
def test_c5_a_flag_on_fire_with_a_real_owner_writes_nothing_to_the_store(home, tmp_path):
    """C5. Flag ON, a real owner up, the REAL calibration block (not stubbed), the deployed layout:
    the store's main-file bytes, a fresh connection's `PRAGMA data_version` and the `tool_timing`
    row count are all unchanged by the fire, AND the calibration block was rendered (the header is in
    the context: an implementation that silently renders nothing cannot pass).
    Red today: the block runs `engram_stats` through the client, which writes 2 `tool_timing` rows.
    Mutation: M1 (old path kept) and M7 (the op records tool_timing) -> red."""
    db = live_store(home, flag=True)
    seed_marker(home)
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    with owner_of("real", home):
        before = (_digest(db), _fresh_data_version(db), tool_timing_rows(db))
        out = run_hook_subprocess(hook, home, tmp_path, stub_calibration=False)
        assert out["rc"] == 0, out
        after = (_digest(db), _fresh_data_version(db), tool_timing_rows(db))
    assert CAL_MARK in out["ctx"], (out["ctx"], out["stderr"])  # non-vacuity: the block WAS rendered
    assert after[2] == before[2], ("tool_timing rows", before[2], after[2])
    assert after[1] == before[1], ("data_version", before[1], after[1])
    assert after[0] == before[0], "main-file bytes changed"


@requires_linux_owner
def test_c6_a_flag_on_full_fire_in_the_deployed_layout_opens_nothing_and_imports_no_runtime(
        home, tmp_path, monkeypatch):
    """C6. Flag ON, a real owner up, the real calibration block, the flattened deployed layout: ZERO
    store opens from any frame, the calibration block IS rendered (served by the owner: this is the
    anti-vacuity half), and `server`, `engram_core`, `engram_client` are absent from the hook
    process's `sys.modules`. It also pins WHAT the hook asks and renders: exactly one
    `confidence_by_type` request whose params are exactly `{"modes": ["all", "7-turn"]}` (a spy on the
    client's `read` records the params), and a rendered block equal to the block the current code
    renders for the same store (`oracle_calibration_block`: swapped (7d, all) arguments or a wrong
    mode change the text). (Flips U9's `test_a2_characterization_...`, see that file.)
    Red today: calibration still opens the store directly through EngramClient.
    Mutation: M1 -> red."""
    live_store(home, flag=True, turn=41)
    seed_marker(home)
    expected = oracle_calibration_block(home, tmp_path)
    assert CAL_MARK in expected and "turn 41" in expected, expected  # the oracle is not vacuous
    assert "All claims   (N=   9)" in expected and "7-turn rolling: N=6" in expected, expected  # literals: a formatter regression cannot pass by mirrored breakage
    monkeypatch.setattr(u9, "RUNNER", MODS_RUNNER)
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    with owner_of("real", home):
        out = run_hook_subprocess(hook, home, tmp_path, stub_calibration=False, spy_read=True,
                                  runtime_dir=root)
    assert out["rc"] == 0, out
    assert out["opens"] == [], out["opens"]
    for name in ("server", "engram_core", "engram_client"):
        assert name not in out["modules"], name
    assert "[ENGRAM Calibration: graph distribution at turn 41]" in out["ctx"], (out["ctx"], out["stderr"])
    asked = [c for c in out["calls"] if c[0] == "confidence_by_type"]
    assert len(asked) == 1, out["calls"]
    assert asked[0][2] == {"modes": ["all", "7-turn"]}, asked[0]
    assert _calibration_block(out["ctx"]) == _calibration_block(expected), (out["ctx"], expected)


# =========================================================================================
# C7 / C7b -- the owner fails: nothing falls back, every failure is reported
# =========================================================================================

class _PoisonClientModule(types.ModuleType):
    """Stands in for `engram_client`: ANY attribute access (`from engram_client import EngramClient`)
    is recorded, so a fallback to the old path is observable."""

    def __init__(self):
        super().__init__("engram_client")
        self.__dict__["touched"] = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        self.__dict__["touched"].append(name)
        raise ImportError(f"engram_client.{name} reached: the fallback path ran (#3741)")


def _fire(home, monkeypatch, *, owner=None, script_handler=None, cal_exc=None, pre=None):
    """A full in-process flag-ON fire with the six reads served by `owner` (default FakeOwner), the
    calibration read optionally failing (`cal_exc` raised from the client method, or a Script), and
    the old client replaced by a poison module. -> (run, opens, poison)."""
    poison = _PoisonClientModule()
    monkeypatch.setitem(sys.modules, "engram_client", poison)
    mod = full_fire_setup(home, monkeypatch)
    seed_confidence_nodes(home / "knowledge.db", datetime.now(timezone.utc))
    write_config(home, flag=True, turn=41)
    monkeypatch.setattr(mod, "OWNER_COLD_START_WAIT_S", 0)
    if cal_exc is not None:
        def _raise(self, modes, *a, **k):
            raise cal_exc
        monkeypatch.setattr(ec.OwnerReadConnection, "confidence_by_type", _raise, raising=False)
    if script_handler is not None:
        Script(monkeypatch, script_handler)
    if pre is not None:
        pre(mod)
    opens = AttrOpens(mod, home, raises=False).install(monkeypatch)
    with (owner or FakeOwner(home)):
        run = run_main(mod, home)
    return run, opens, poison


def _six_delivered(ctx: str) -> None:
    for key in ("focus", "saved", "checkpoint", "sleep", "backup"):
        assert BLOCK_MARKERS[key] in ctx, (key, ctx)


def _assert_calibration_silent(run, opens, poison):
    assert run.exc is None, run.exc
    assert CAL_MARK not in run.ctx, run.ctx
    assert poison.touched == [], f"the old EngramClient path was reached: {poison.touched}"
    assert opens.record == [], opens.record
    _six_delivered(run.ctx)


def test_c7a_the_owner_unavailable_for_calibration_renders_nothing_and_does_not_fall_back(home, monkeypatch):
    """C7(a). The calibration read gets `Unavailable` (the six reads are served): no calibration
    block, no `engram_client` import, zero opens, the six blocks delivered.
    Red today: the hook goes straight to the EngramClient path.
    Mutation: M6 (fallback on owner failure) -> red."""
    def h(method, sql, n):
        return _unavailable("down", home) if method == "confidence_by_type" else None
    _assert_calibration_silent(*_fire(home, monkeypatch, script_handler=h))


def test_c7b_an_older_owner_answering_unknown_operation_renders_nothing_and_does_not_fall_back(home, monkeypatch):
    """C7(b). An owner that predates the op (the fake owner answers `unknown_operation` to any
    method it does not know): no block, no fallback, zero opens, the six delivered.
    Also pins the seam real owner reply -> client mapping -> health line: the single health line
    names `unknown_operation` with the restart-the-owner remedy (same helper as C7b(i)) and
    `calibration` is a recorded site, with the stderr line carrying kind and site.
    Red today: falls into the EngramClient path."""
    run, opens, poison = _fire(home, monkeypatch)
    _assert_calibration_silent(run, opens, poison)
    _assert_unknown_operation_health_line(_health_line(run))
    assert OWNER_STDERR_RE.findall(run.stderr) == [("rejected", "calibration")], run.stderr


def test_c7c_an_exhausted_budget_renders_nothing_and_does_not_fall_back(home, monkeypatch):
    """C7(c). The client refuses the calibration read with `budget_exhausted`: no block, no fallback,
    zero opens, the six delivered. Red today: falls into the EngramClient path."""
    _assert_calibration_silent(*_fire(home, monkeypatch, cal_exc=ec.OwnerReadError("budget_exhausted")))


def _health_line(run) -> str:
    lines = [ln for ln in run.ctx.split("\n") if NOTICE_PHRASE in ln]
    assert len(lines) == 1, (len(lines), run.ctx)
    return lines[0]


def _assert_unknown_operation_health_line(line: str) -> None:
    """Shared by C7b(i) and the older-owner C7(b) cell so the two wordings cannot drift."""
    assert "calibration" in line.lower(), line
    assert "unknown_operation" in line, line
    assert "predates confidence_by_type" in line and "restart the owner" in line.lower(), line


UNKNOWN_OP = ec.Rejected(reason="unknown_operation", detail="no such operation: 'confidence_by_type'",
                         remedy="check the method name")


def test_c7b_i_the_typed_unknown_operation_failure_is_recorded_and_gets_its_own_remedy(home, monkeypatch):
    """C7b(i). The client raises the exact typed `OwnerReadError("rejected", Rejected(reason=
    "unknown_operation"))`: the site "calibration" joins `degraded` (it is named in the single
    health line's missing list), the stderr line carries kind and site, and the health line names
    `unknown_operation` with the restart-the-owner remedy ("owner predates confidence_by_type;
    restart the owner"), decided by the TYPE.
    Red today: the hook never asks the owner for calibration, so nothing is recorded.
    Mutation: M10 (skip the record) -> red."""
    run, opens, poison = _fire(home, monkeypatch, cal_exc=ec.OwnerReadError("rejected", UNKNOWN_OP))
    line = _health_line(run)
    _assert_unknown_operation_health_line(line)
    assert OWNER_STDERR_RE.findall(run.stderr) == [("rejected", "calibration")], run.stderr
    assert poison.touched == [] and opens.record == []


@pytest.mark.parametrize("kind,reason", [("unavailable", "timeout"), ("rejected", "deadline_exceeded"),
                                         ("budget_exhausted", None)])
def test_c7b_ii_controls_other_failures_are_recorded_with_their_own_wording(kind, reason, home, monkeypatch):
    """C7b(ii) controls: `unavailable` and `rejected` with another reason are recorded (own site,
    own kind / reason), and neither gets the unknown_operation wording. Red today: not recorded."""
    if kind == "budget_exhausted":
        result = None  # the client refuses locally; there is no owner reply to carry
    elif kind == "unavailable":
        result = _unavailable(reason, home)
    else:
        result = ec.Rejected(reason=reason, detail="slow", remedy="retry")
    run, _opens, _poison = _fire(home, monkeypatch, cal_exc=ec.OwnerReadError(kind, result))
    line = _health_line(run)
    assert "calibration" in line.lower() and kind in line, line
    if reason is not None:
        assert reason in line, line
    assert "predates confidence_by_type" not in line and "unknown_operation" not in line, line
    assert OWNER_STDERR_RE.findall(run.stderr) == [(kind, "calibration")], run.stderr


def test_c7b_iii_a_message_text_lookalike_does_not_get_the_unknown_operation_wording(home, monkeypatch):
    """C7b(iii). A DIFFERENT kind (`client_failure`) whose `str()` contains the words `unknown_operation`
    is recorded as what it is: the type decides the wording, never the message text.
    Red today: not recorded at all. Mutation: M11 (decide by message text) -> red."""
    exc = ec.OwnerReadError("client_failure")
    exc.args = ("owner read failed: unknown_operation (lookalike)",)
    assert "unknown_operation" in str(exc)
    run, _opens, _poison = _fire(home, monkeypatch, cal_exc=exc)
    line = _health_line(run)
    assert "calibration" in line.lower() and "client_failure" in line, line
    assert "predates confidence_by_type" not in line and "restart the owner" not in line.lower(), line
    assert OWNER_STDERR_RE.findall(run.stderr) == [("client_failure", "calibration")], run.stderr


def test_c7b_iv_an_earlier_failure_stays_the_first_failure_calibration_is_only_a_site(home, monkeypatch):
    """C7b(iv). One of the six fails first (the focus read is rejected); calibration then fails with
    `unknown_operation`. First-wins: the single health line and the stderr line describe the focus
    failure; calibration is recorded as a degraded site only (named in the missing list), and the
    unknown_operation reason does NOT replace the first failure.
    Red today: calibration is never recorded."""
    def h(method, sql, n):
        return _rejected() if (method == "read" and sql == SQL_FOCUS) else None
    run, _opens, _poison = _fire(home, monkeypatch, script_handler=h,
                                 cal_exc=ec.OwnerReadError("rejected", UNKNOWN_OP))
    line = _health_line(run)
    assert "failed at focus_block" in line and "reason: unregistered_template" in line, line
    assert "reason: unknown_operation" not in line, line
    assert "focus list" in line and "calibration" in line.lower(), line
    assert OWNER_STDERR_RE.findall(run.stderr) == [("rejected", "focus_block")], run.stderr


_STATS_ROW = {"n": 1, "mean": 0.9, "p25": 0.9, "p50": 0.9, "p75": 0.9, "p90": 0.9, "p95": 0.9}


def _good_reply():
    return {"current_turn": 41, "by_mode": {"all": {"by_type": {"observation_factual": dict(_STATS_ROW)}},
                                            "7-turn": {"by_type": {"observation_factual": dict(_STATS_ROW)}}}}


def _ok_cal_reply(reply):
    def h(method, sql, n):
        return ec.Ok(result=reply) if method == "confidence_by_type" else None
    return h


def test_c15_control_a_well_formed_ok_reply_through_the_same_seam_renders_the_block(home, monkeypatch):
    """C15 control. The scripted-Ok seam the malformed cells below use, fed a WELL-FORMED reply,
    renders the calibration block and records no failure: the malformed cells are silent because of
    the reply's shape, not because the seam is blind."""
    run, opens, poison = _fire(home, monkeypatch, script_handler=_ok_cal_reply(_good_reply()))
    assert run.exc is None, run.exc
    assert CAL_MARK in run.ctx, run.ctx
    assert NOTICE_PHRASE not in run.ctx, run.ctx
    assert poison.touched == [] and opens.record == [], (poison.touched, opens.record)


def _bad_replies():
    no_by_mode = _good_reply()
    del no_by_mode["by_mode"]
    no_7 = _good_reply()
    del no_7["by_mode"]["7-turn"]
    str_turn = _good_reply()
    str_turn["current_turn"] = "41"
    # bool is an int subclass: the hook refuses it explicitly (follow-up to the colleague read of #3759)
    bool_turn = _good_reply()
    bool_turn["current_turn"] = True
    list_by_type = _good_reply()
    list_by_type["by_mode"]["all"]["by_type"] = []
    # Per-type values: the formatter reads n and p25/p50/p75/p95 of each value (reviewer round 1).
    int_value = _good_reply()
    int_value["by_mode"]["all"]["by_type"]["derivation"] = 5
    no_p50 = _good_reply()
    del no_p50["by_mode"]["7-turn"]["by_type"]["observation_factual"]["p50"]
    str_n = _good_reply()
    str_n["by_mode"]["all"]["by_type"]["observation_factual"]["n"] = "1"
    # The formatter prints n with `:4d`, so a float n -- even a whole one -- raises ValueError
    # (reviewer round 2); quantiles print with `:.2f`, where an int is fine.
    float_n = _good_reply()
    float_n["by_mode"]["all"]["by_type"]["observation_factual"]["n"] = 3.5
    whole_float_n = _good_reply()
    whole_float_n["by_mode"]["7-turn"]["by_type"]["observation_factual"]["n"] = 3.0
    return [("missing_by_mode", no_by_mode), ("missing_7_turn", no_7),
            ("current_turn_a_string", str_turn), ("current_turn_a_bool", bool_turn),
            ("by_type_a_list", list_by_type),
            ("by_type_value_an_int", int_value), ("by_type_value_missing_p50", no_p50),
            ("by_type_value_n_a_string", str_n), ("by_type_value_n_a_float", float_n),
            ("by_type_value_n_a_whole_float", whole_float_n)]


@pytest.mark.parametrize("label,reply", _bad_replies(), ids=[b[0] for b in _bad_replies()])
def test_c15_a_malformed_ok_reply_is_recorded_as_unusable_answer_and_renders_nothing(label, reply, home, monkeypatch):
    """C15. An `ok` `confidence_by_type` reply whose SHAPE is wrong (a dict with an int `current_turn`
    and a `by_mode` dict holding both requested modes, each a dict with a dict `by_type` whose values are
    dicts of numeric n/p25/p50/p75/p95, is the only usable one) is an owner failure like any other: recorded as `OwnerReadError("unusable_answer")`
    at site "calibration" (the single health line names it, the stderr line carries kind and site),
    no block, no EngramClient, zero opens, the six delivered. Never a bare KeyError / TypeError
    swallowed by the outer try with no health line.
    Mutation: drop the shape check in the hook -> red; drop only the per-type value check -> the
    last three rows red."""
    run, opens, poison = _fire(home, monkeypatch, script_handler=_ok_cal_reply(reply))
    _assert_calibration_silent(run, opens, poison)
    line = _health_line(run)
    assert "calibration" in line.lower() and "unusable_answer" in line, line
    assert OWNER_STDERR_RE.findall(run.stderr) == [("unusable_answer", "calibration")], run.stderr


def test_c17_the_core_loader_and_the_read_gate_require_a_home(tmp_path):
    """C17 (reviewer round 1, the spec's call: `home` is REQUIRED). `_engram_core(None)` raises
    TypeError rather than silently reading the process-env home's config; a `_ClientReadGate` built
    without `home` is a TypeError at construction (so no gate can reach the core unconfigured).
    Mutation: restore the `home=None` path in either -> red."""
    with pytest.raises(TypeError, match="home"):
        eo._engram_core(None)
    with pytest.raises(TypeError):
        eo._ClientReadGate(object())
    assert eo._ClientReadGate(object(), home=tmp_path)._home == tmp_path


def test_c16_the_owners_literal_mode_list_equals_the_runtimes(home):
    """C16. The owner validates `modes` against a literal `CBT_MODES` so validation never imports the
    runtime; this cell is the only thing tying that literal to `engram_stats.STATS_MODES` (order
    included) and `CBT_MAX_MODES` to its length.
    Mutation: add a mode to either list -> red."""
    stats = stats_mod()
    assert tuple(eo.CBT_MODES) == tuple(stats.STATS_MODES), (eo.CBT_MODES, stats.STATS_MODES)
    assert eo.CBT_MAX_MODES == len(stats.STATS_MODES), (eo.CBT_MAX_MODES, len(stats.STATS_MODES))


# =========================================================================================
# C8 / C9 / C10 -- flag OFF: the user_version guard
# =========================================================================================

@pytest.mark.parametrize("flag", [None, True], ids=["flag-off", "flag-on"])
def test_c8_a_full_fire_on_an_empty_home_leaves_no_store_behind(flag, home, tmp_path):
    """C8. The real calibration block, a home with NO knowledge.db, flag OFF and flag ON (U9's Fix B
    already stops the six from creating it): no store file afterwards and no non-read-only open.
    Red today: the calibration block builds the store (OFF: always; ON: no owner, it still goes
    direct). Mutation: M3 (drop the flag-OFF guard) -> the OFF leg red."""
    set_flag(home, flag)
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    out = run_hook_subprocess(hook, home, tmp_path, stub_calibration=False)
    assert out["rc"] == 0, out
    assert out["store_exists"] is False, (out["store_size"], out["opens"])
    assert [o for o in out["opens"] if not o["ro"]] == [], out["opens"]


def test_c9_a_zero_byte_store_stays_zero_bytes_and_gets_no_calibration_block(home, tmp_path):
    """C9. A 0-byte `knowledge.db`, flag OFF, the real calibration block: after the fire the file is
    still 0 bytes, its `user_version` is 0, and no calibration block was rendered.
    Red today: the block builds the schema in the empty file. Mutation: M3 -> red."""
    db = home / "knowledge.db"
    db.write_bytes(b"")
    set_flag(home, None)
    root = flattened_root(tmp_path)
    hook = root / "hooks" / "engram-session-start-hook.py"
    out = run_hook_subprocess(hook, home, tmp_path, stub_calibration=False)
    assert out["rc"] == 0, out
    assert CAL_MARK not in out["ctx"], out["ctx"]
    assert db.stat().st_size == 0, db.stat().st_size
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()


def _calibration_block(ctx: str) -> str:
    lines = ctx.split("\n")
    i = next((k for k, ln in enumerate(lines) if ln.startswith(CAL_MARK)), None)
    assert i is not None, ctx
    j = i + 1
    while j < len(lines) and lines[j].startswith("  "):
        j += 1
    return "\n".join(lines[i:j])


def test_c10_flag_off_on_an_existing_store_is_dev_for_dev_and_still_writes_two_timing_rows(home, tmp_path):
    """C10. Flag OFF on an existing store: the calibration block text equals the block the CURRENT
    runtime renders for the same store (`oracle_calibration_block`: the same two `engram_stats` calls
    through EngramClient, then the hook module's own `format_calibration_block`; no git history, so it
    cannot skip on a shallow clone), and `tool_timing` still grows by exactly 2 (the writes stay until
    the flag retires). GREEN now: it guards that the flag-OFF path stays identical, writes included."""
    db = live_store(home, flag=None, turn=23)
    seed_marker(home)
    expected = oracle_calibration_block(home, tmp_path)  # on a backup copy, before the fire
    assert CAL_MARK in expected and "turn 23" in expected, expected  # the oracle is not vacuous
    assert "All claims   (N=   9)" in expected and "7-turn rolling: N=6" in expected, expected  # literals: a formatter regression cannot pass by mirrored breakage
    before = tool_timing_rows(db)
    root = flattened_root(tmp_path)
    out = run_hook_subprocess(root / "hooks" / "engram-session-start-hook.py", home, tmp_path,
                              stub_calibration=False)
    assert out["rc"] == 0, out
    assert _calibration_block(out["ctx"]) == _calibration_block(expected)
    assert tool_timing_rows(db) - before == 2, (before, tool_timing_rows(db))


# =========================================================================================
# C11 -- the owner reads ITS home's config, not the process env's
# =========================================================================================

@requires_linux_owner
def test_c11_the_op_reports_the_owners_home_turn_not_the_process_envs(home, tmp_path):
    """C11, DISCRIMINATING. A real owner (fresh interpreter) on home A (config turn 41) while the
    process env `ENGRAM_HOME` points at a DIFFERENT home B (config turn 7): the op reports 41. With
    `_configure_paths` not called, `engram_core` reads B and reports 7.
    Red today: the op does not exist. Mutation: M9 (drop `_configure_paths`) -> red."""
    home_a = home
    pinned_store(home_a, turn=41)
    home_b = Path(tempfile.mkdtemp(prefix="home-b-", dir=str(tmp_path)))
    write_config(home_b, turn=7)
    rep = owner_subprocess(home_a, tmp_path, [["confidence_by_type", {"modes": ["all"]}]], env_home=home_b)
    result = child_ok(rep[0])
    assert result["current_turn"] == 41, result


IMPORT_PROBE = r'''
import json, os, sys
from pathlib import Path
home = Path(sys.argv[1])
before = sorted(os.listdir(home))
events = []
def _audit(ev, args):
    if ev == "sqlite3.connect":
        events.append(["connect", str(args[0])])
    elif ev == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        p = os.fsdecode(args[0])
        if p.startswith(str(home)):
            events.append(["open", p])
sys.addaudithook(_audit)
import engramd_owner as eo
fn = getattr(eo, "_engram_core", None)
if fn is None:
    json.dump({"missing": True}, open(sys.argv[2], "w")); raise SystemExit(0)
core = fn(home)
json.dump({"missing": False, "events": events, "before": before, "after": sorted(os.listdir(home)),
           "data_dir": str(core.DATA_DIR)}, open(sys.argv[2], "w"))
'''


def test_c11b_the_first_engram_core_import_opens_and_creates_nothing(home, tmp_path):
    """C11b (from the spec's C-B paragraph, not a table row). `engramd_owner._engram_core(home)` --
    the owner's ONE lazy-import site -- calls `engram_core._configure_paths(home)` on its first
    import; that is module globals only: no database opened (no sqlite3.connect, no open under the
    home) and nothing created in the home. Also pins the shared home: `core.DATA_DIR` is the owner's
    home. Red today: `_engram_core()` does not yet take the owner's home (it is called here as
    `_engram_core(home)`), so it never configures the paths."""
    script, out = tmp_path / "probe3741.py", tmp_path / "probe-out3741.json"
    script.write_text(IMPORT_PROBE)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR")}
    env["ENGRAM_HOME"] = str(tmp_path / "elsewhere")
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), SRC_ENGRAM])
    r = subprocess.run([sys.executable, str(script), str(home), str(out)], capture_output=True, text=True,
                       env=env, cwd=str(REPO_ROOT), timeout=60)
    assert out.exists(), (r.returncode, r.stderr[-2000:])
    rep = json.loads(out.read_text())
    if rep["missing"]:
        pytest.fail("engramd_owner._engram_core is missing: the #3741 lazy-import site is not implemented yet")
    assert rep["events"] == [], rep["events"]
    assert rep["after"] == rep["before"], (rep["before"], rep["after"])
    assert rep["data_dir"] == str(home)


@requires_linux_owner
def test_c12_the_unknown_operation_remedy_lists_the_real_operations(home):
    """C12 (from the spec's C-B paragraph, not a table row). The stale text "(this build: ping,
    snapshot, read)" is replaced by the real list: every key of the owner's method table, which
    includes `confidence_by_type`.
    Red today: the text omits `df` and the new op."""
    build_empty_store(home)
    with owner_of("real", home) as o:
        res = ask(home, "no_such_op_3741", {})
        keys = sorted(o._methods)
    assert isinstance(res, ec.Rejected) and res.reason == "unknown_operation", res
    for k in keys:
        assert k in res.remedy, (k, res.remedy)
    assert "confidence_by_type" in keys and "confidence_by_type" in res.remedy, (keys, res.remedy)


@requires_linux_owner
def test_c13_the_owner_answers_cbt_runtime_unavailable_when_the_runtime_import_fails(home, monkeypatch):
    """C13 (spec C-B: "Runtime import lazy (after validation, before the client-read lock) ...; failure
    -> `cbt_runtime_unavailable`"). The owner's one lazy-import site `_engram_core(home)` is replaced
    by a function raising ImportError (accepting any call shape, so an implementation calling
    `_engram_core(home)` or `_engram_core(home=...)` both reach the intended path); a VALID request
    is then refused by name, a typed `Rejected`, and the refusal echoes neither the exception text
    nor the caller's modes.
    Red today: the op is unknown (`unknown_operation`), not `cbt_runtime_unavailable`."""

    def _boom(*a, **k):
        raise ImportError("SENTINEL-3741-import-text")

    monkeypatch.setattr(eo, "_engram_core", _boom)
    build_empty_store(home)
    with owner_of("real", home):
        res = ask(home, "confidence_by_type", {"modes": ["all"]})
    assert isinstance(res, ec.Rejected), res
    assert res.reason == "cbt_runtime_unavailable", (res.reason, res.detail)
    assert "SENTINEL-3741-import-text" not in repr(res)


# =========================================================================================
# the client method (spec C-C): OwnerReadConnection.confidence_by_type
# =========================================================================================

class _StubRead:
    """Stands in for `ec.read`: records (method, params, kwargs), returns / raises `answer`."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list = []

    def __call__(self, method, params, **k):
        self.calls.append((method, params, k))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def _client_method():
    fn = getattr(ec.OwnerReadConnection, "confidence_by_type", None)
    if fn is None:
        pytest.fail("OwnerReadConnection.confidence_by_type is missing: the #3741 client method is not "
                    "implemented yet")
    return fn


GOOD_ANSWER = {"current_turn": 3, "by_mode": {"all": {"by_type": {}}}}


def test_c14_client_sends_the_typed_op_returns_the_dict_and_counts_one_round_trip(monkeypatch):
    """C14a (spec C-C: thin, same `.calls` as the other typed methods). One call sends method
    `confidence_by_type` with params exactly `{"modes": [...]}`, returns the owner's dict as it is, and
    `.calls` goes up by exactly 1 per round trip (0 -> 1 -> 2).
    Red today: the method does not exist."""
    _client_method()
    stub = _StubRead(ec.Ok(result=GOOD_ANSWER))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(2.0)
    assert conn.calls == 0
    assert conn.confidence_by_type(["all", "7-turn"]) == GOOD_ANSWER
    assert conn.calls == 1
    conn.confidence_by_type(["all"])
    assert conn.calls == 2
    assert [(m, p) for m, p, _k in stub.calls] == [
        ("confidence_by_type", {"modes": ["all", "7-turn"]}), ("confidence_by_type", {"modes": ["all"]})]


# Named `_stub_*`, not `_unavailable`: a module-level `_unavailable` would shadow the
# two-argument helper imported from the U9 cells above (C7a / C7b(ii) call it).
def _stub_unavailable():
    return ec.Unavailable(socket_path="/nope.sock", bound=2.0, agent_steps=["a"], human_steps=["h"])


def _stub_rejected():
    return ec.Rejected(reason="cbt_mode_unknown", detail="d", remedy="r")


@pytest.mark.parametrize("make,kind", [(_stub_unavailable, "unavailable"), (_stub_rejected, "rejected")],
                         ids=["unavailable", "rejected"])
def test_c14_client_unavailable_and_rejected_map_to_owner_read_error_with_the_result_attached(
        make, kind, monkeypatch):
    """C14d (spec C-C: same error mapping as generalization_chain / execute). `read` answering
    Unavailable -> `OwnerReadError("unavailable")`, answering Rejected -> `OwnerReadError("rejected")`;
    in both `.result` IS the very object `read` returned (the hook decides `unknown_operation` by
    `isinstance(result, Rejected)` and `result.reason`, never by message text), and it counts a round trip.
    Red today: the method does not exist."""
    _client_method()
    answer = make()
    monkeypatch.setattr(ec, "read", _StubRead(answer))
    conn = ec.OwnerReadConnection(2.0)
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.confidence_by_type(["all"])
    assert ei.value.kind == kind and ei.value.result is answer
    if kind == "rejected":
        assert ei.value.result.reason == "cbt_mode_unknown"
    assert conn.calls == 1


def test_c14_client_a_raising_read_is_a_client_failure(monkeypatch):
    """C14e. `read` raising -> `OwnerReadError("client_failure")` (cause chained, no result attached).
    Red today: the method does not exist."""
    _client_method()
    boom = RuntimeError("boom")
    monkeypatch.setattr(ec, "read", _StubRead(boom))
    conn = ec.OwnerReadConnection(2.0)
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.confidence_by_type(["all"])
    assert ei.value.kind == "client_failure" and ei.value.result is None
    assert ei.value.__cause__ is boom
    assert conn.calls == 1


@pytest.mark.parametrize("answer", ["x", [["a"]], [], None, 5, [1, 2]], ids=repr)
def test_c14_client_a_non_dict_ok_answer_is_unusable(answer, monkeypatch):
    """C14b. An ok answer that is not a dict -> `OwnerReadError("unusable_answer")`, no result attached.
    Red today: the method does not exist."""
    _client_method()
    monkeypatch.setattr(ec, "read", _StubRead(ec.Ok(result=answer)))
    with pytest.raises(ec.OwnerReadError) as ei:
        ec.OwnerReadConnection(2.0).confidence_by_type(["all"])
    assert ei.value.kind == "unusable_answer" and ei.value.result is None


def test_c14_client_an_exhausted_budget_refuses_without_calling_the_owner(monkeypatch):
    """C14c. Once the whole-fire budget is spent the method raises `OwnerReadError("budget_exhausted")`
    with NOTHING sent, and `.calls` is unchanged (a refusal is not a round trip).
    Red today: the method does not exist."""
    _client_method()
    now = [100.0]
    monkeypatch.setattr(ec, "_monotonic", lambda: now[0])
    stub = _StubRead(ec.Ok(result=GOOD_ANSWER))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(1.0)
    conn.confidence_by_type(["all"])
    assert conn.calls == 1 and len(stub.calls) == 1
    now[0] += 1.0  # remaining exactly 0
    with pytest.raises(ec.OwnerReadError) as ei:
        conn.confidence_by_type(["all"])
    assert ei.value.kind == "budget_exhausted" and ei.value.result is None
    assert len(stub.calls) == 1 and conn.calls == 1


def test_c14_client_closed_is_a_runtime_error_before_the_budget_check_and_counts_nothing(monkeypatch):
    """C14f (template: test_engramd_client_chain_3740 closed cell). A closed connection raises
    RuntimeError even when the budget is also spent (closed wins), sends nothing, counts nothing.
    Red today: the method does not exist."""
    _client_method()
    now = [100.0]
    monkeypatch.setattr(ec, "_monotonic", lambda: now[0])
    stub = _StubRead(ec.Ok(result=GOOD_ANSWER))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(1.0)
    conn.close()
    now[0] += 5.0  # the budget is gone too: closed wins
    with pytest.raises(RuntimeError):
        conn.confidence_by_type(["all"])
    assert stub.calls == [] and conn.calls == 0


def test_c14_client_passes_min_of_per_call_and_remaining_as_the_timeout_kwarg(monkeypatch):
    """C14g (template: the accounting cell). `read` gets `timeout=min(per_call or total, remaining)`:
    0.6 on the first call, then 0.3 after 0.7 s of the 1.0 s budget is gone.
    Red today: the method does not exist."""
    _client_method()
    now = [100.0]
    monkeypatch.setattr(ec, "_monotonic", lambda: now[0])
    stub = _StubRead(ec.Ok(result=GOOD_ANSWER))
    monkeypatch.setattr(ec, "read", stub)
    conn = ec.OwnerReadConnection(1.0, 0.6)
    conn.confidence_by_type(["all"])
    assert stub.calls[-1][2]["timeout"] == pytest.approx(0.6)
    now[0] += 0.7
    conn.confidence_by_type(["all"])
    assert stub.calls[-1][2]["timeout"] == pytest.approx(0.3)
    assert conn.calls == 2


def test_c14_client_a_failed_round_trip_is_still_counted(monkeypatch):
    """C14h (template: test_a_failed_round_trip_is_still_counted). Rejected, a raising read and a
    non-dict ok answer each raise `OwnerReadError` AND count one round trip in `.calls`.
    Red today: the method does not exist."""
    _client_method()
    for outcome in (_rejected(), ValueError("boom"), ec.Ok(result="x")):
        stub = _StubRead(outcome)
        monkeypatch.setattr(ec, "read", stub)
        conn = ec.OwnerReadConnection(2.0)
        with pytest.raises(ec.OwnerReadError):
            conn.confidence_by_type(["all"])
        assert conn.calls == 1 and len(stub.calls) == 1


def test_c14_client_the_context_manager_closes_the_route(monkeypatch):
    """C14i (template: the context-manager cell). Leaving the `with` block closes the connection:
    the next call raises RuntimeError. Red today: the method does not exist."""
    _client_method()
    monkeypatch.setattr(ec, "read", _StubRead(ec.Ok(result=GOOD_ANSWER)))
    with ec.OwnerReadConnection(2.0) as conn:
        conn.confidence_by_type(["all"])
    with pytest.raises(RuntimeError):
        conn.confidence_by_type(["all"])
