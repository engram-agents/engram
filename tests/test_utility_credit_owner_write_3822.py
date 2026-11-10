"""Contract cells for U11 Stage B (#3822, #2652): the utility-credit Stop hook's write goes through the
owner's typed `use_reward` op behind `engramd.utility_credit_writes_via_owner`.

Written BEFORE the implementation (contract-first; project CLAUDE.md). The contract:

  * flag OFF (default; anything but the JSON boolean true): today's code path, unchanged, and the owner client is
    never imported or called.
  * flag ON: `bump_utility` never opens the store (no sqlite3.connect on the store path, so no WAL pragma) and does not
    need engram_restamp. It sends `write("use_reward", {"node_ids", "action": "mention", "request_id"}, request_id=rid,
    timeout=<bound>)` with `rid` a fresh uuid4 string per fire: exactly those three keys, never an alpha, a value or a
    config path. It returns the COUNT bumped (`result["bumped"]`). An ack's `restamp_error` is logged `restamp failed: <err>`.
  * explicit alpha (or restamp=False) with the flag ON: refused loudly, nothing written.
  * outcome classes, each logged in DIFFERENT words, and NEVER a fall back to the store:
      DETERMINATE not-applied   Rejected(busy)                 -> `bump_utility failed: owner busy (not applied)`
                                other Rejected                 -> `bump_utility failed: owner rejected: <reason>`
                                Rejected(unknown_operation)    -> the contract-skew line (restart / upgrade the owner)
                                Unavailable(down|local|...)    -> `bump_utility failed: owner unavailable (reason=<code>)`
      COMMITTED, unverified     Rejected(readback_mismatch)    -> `bump_utility committed but unverified: ...`
      INDETERMINATE             Unavailable(timeout|protocol)  -> retry ONCE with the SAME request_id; the owner's dedupe
                                answers duplicate=true (no second bump); twice indeterminate ->
                                `bump_utility indeterminate: owner did not answer (may have been applied once)`
                                (never the word "failed"). Determinate outcomes are never retried.
  * the whole owner write (both attempts) fits inside the Stop hook's registered timeout (hooks.json).

Real-owner cells run on this Mac (scratch homes under tmp ONLY; the live ~/.engram and its socket are never touched:
asserted by every fixture).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import pytest

from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)
HOOK_PATH = REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-utility-credit-mention-stop.py"
HOOKS_JSON = REPO_ROOT / "src" / "engram" / "hooks" / "hooks.json"
# The unmodified hook (before Stage B) is VENDORED, not read from the repository history: the flag-OFF identity cells below
# are the migration proof "flag OFF == the unmodified hook: same store, same log, same count", and a proof that skips in a
# fresh or shallow clone is no proof. BASE_COMMIT is the commit the fixture was taken from (checked by one provenance cell).
# These cells and the fixture are a MIGRATION PROOF: retire both together when the flag is flipped on by default or removed.
BASE_COMMIT = "91da3142"
BASE_HOOK_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "utility_credit_hook_before_3822_stage_b.py.txt"

import engram_core  # noqa: E402
import engram_restamp as er  # noqa: E402
import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402

THREE = ("utility_score", "importance_score", "recall_turn")
CONFIG_TURN = 7
FLAG = "utility_credit_writes_via_owner"

# Ids that the hook's regex (2 letters, underscore, >=4 digits) finds in prose.
TEXT = "credit ob_0001 and ob_0002 then again ob_0002, plus ob_0099 (no row), ob_0004 and ob_0005."
MENTIONED = ["ob_0001", "ob_0002", "ob_0099", "ob_0004", "ob_0005"]  # first-occurrence order, deduped

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


# --- scratch fixtures -------------------------------------------------------------------------


def _assert_scratch(home: Path) -> None:
    live = (Path.home() / ".engram").resolve()
    h = Path(home).resolve()
    assert h != live and live not in h.parents, h
    assert str(h).startswith(str(Path(tempfile.gettempdir()).resolve())) or "/var/folders/" in str(h) or str(h).startswith("/tmp"), h
    sock = ec.socket_path_for_home(h)
    assert (Path.home() / ".engram").resolve() not in Path(sock).resolve().parents


def make_store(home: Path, *, flag=True, memory: bool = True, raw_config: str | None = None) -> Path:
    """Real-schema store: ob_0001 NULL utility, ob_0002 mid, ob_0004 Q~1.0, ob_0005 low, ob_0006 never targeted.
    config.json: the memory block (current_turn=CONFIG_TURN) plus the flag (`...` = key absent)."""
    _assert_scratch(home)
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        rows = [
            ("ob_0001", None, 0.8, 0, 1, 0.5),
            ("ob_0002", 0.5, 0.6, 3, 2, 0.4),
            ("ob_0004", 0.9999996, 0.9, 1, 3, 0.3),
            ("ob_0005", 0.2, None, 0, 1, 0.9),
            ("ob_0006", 0.33, 0.7, 5, 4, 0.6),
        ]
        for nid, u, ib, rc, rt, isc in rows:
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status,"
                " utility_score, importance_base, recall_count, recall_turn, importance_score)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (nid, "observation_factual", f"claim of {nid}", "2026-01-01T00:00:00", "{}", 1, "active",
                 u, ib, rc, rt, isc))
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    write_config(home, flag=flag, memory=memory, raw=raw_config)
    return db


def write_config(home: Path, *, flag=True, memory: bool = True, raw: str | None = None) -> None:
    if raw is not None:
        (home / "config.json").write_text(raw)
        return
    cfg: dict = {}
    if memory:
        cfg["memory"] = {"decay_base": 1.014, "current_turn": CONFIG_TURN, "tier2_max_nodes": 1000}
    if flag is not ...:
        cfg["engramd"] = {FLAG: flag}
    (home / "config.json").write_text(json.dumps(cfg))


def three(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT id, utility_score, importance_score, recall_turn FROM nodes ORDER BY id")}
    finally:
        conn.close()


def all_rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="eu3-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def start_owner(home: Path):
    o = eo.EngramdOwner(home)
    o.start(install_signal_handlers=False)
    return o


def load_hook(home: Path, monkeypatch, path: Path = HOOK_PATH, *, runtime: bool = False):
    """The hook module executed with ENGRAM_HOME=<scratch home> (its paths freeze at import)."""
    _assert_scratch(home)
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    if runtime:
        monkeypatch.setenv("ENGRAM_RUNTIME_DIR", _SRC_ENGRAM)
    else:
        monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    name = f"eu3_hook_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    assert Path(mod.ENGRAM_HOME).resolve() == home.resolve()
    return mod


def log_lines(hook) -> list[str]:
    p = Path(hook.LOG_PATH)
    return p.read_text().splitlines() if p.exists() else []


def bump_lines(hook) -> list[str]:
    return [re.sub(r"^\[[^\]]*\] ", "", ln) for ln in log_lines(hook)
            if "bump_utility" in ln or "restamp failed" in ln]


class StoreOpenCounter:
    """Counts sqlite3.connect calls (sqlite3.connect AND sqlite3.dbapi2.connect) made from the MAIN thread whose target
    resolves to `store`. The in-process owner runs in its own threads, so its connections do not count; other files do not
    count and cannot hide a store open."""

    def __init__(self, monkeypatch, store: Path):
        self.store = os.path.realpath(store)
        self.hits: list[str] = []
        real = sqlite3.connect
        counter = self

        def counting(database, *a, **k):
            try:
                raw = os.fspath(database)
                if isinstance(raw, str) and raw.startswith("file:"):
                    # a URI open (e.g. the hook's non-creating mode=rw open, #4107) names the same file
                    raw = urllib.parse.unquote(urllib.parse.urlsplit(raw).path)
                target = os.path.realpath(raw)
            except TypeError:
                target = ""
            if target.startswith(counter.store) and threading.current_thread() is threading.main_thread():
                counter.hits.append(target)
            return real(database, *a, **k)

        monkeypatch.setattr(sqlite3, "connect", counting)
        monkeypatch.setattr(sqlite3.dbapi2, "connect", counting)


def run_main(hook, monkeypatch, text=TEXT):
    payload = json.dumps({"last_assistant_message": text, "session_id": "s-3822"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    return hook.main()


# --- the differential harness (a) ------------------------------------------------------------------------


def differential(tmp_root: Path, monkeypatch, *, config_kw=None, mutate=None):
    """The same fixture through the flag-OFF direct path and the flag-ON owner path. Returns (diffs, off, on) where
    diffs names every (node, column) or count that differs."""
    config_kw = config_kw or {}
    ha, hb = tmp_root / "off", tmp_root / "on"
    ha.mkdir()
    hb.mkdir()
    dba = make_store(ha, flag=False, **config_kw)
    dbb = make_store(hb, flag=True, **config_kw)
    hook_off = load_hook(ha, monkeypatch)
    off_stats = hook_off.credit_mentions(TEXT)
    o = start_owner(hb)
    served = []
    real_method = o._methods["use_reward"]
    o._methods["use_reward"] = lambda params, deadline: (served.append(params["request_id"]), real_method(params, deadline))[1]
    try:
        if mutate:
            mutate(o, monkeypatch)
        hook_on = load_hook(hb, monkeypatch)
        on_stats = hook_on.credit_mentions(TEXT)
    finally:
        o.stop()
    a, b = three(dba), three(dbb)
    diffs = []
    for nid in sorted(set(a) | set(b)):
        for i, col in enumerate(THREE):
            if a.get(nid, (None,) * 3)[i] != b.get(nid, (None,) * 3)[i]:
                diffs.append((nid, col))
    if off_stats != on_stats:
        diffs.append(("stats", (off_stats, on_stats)))
    return diffs, (a, off_stats, hook_off), (b, on_stats, hook_on), served


@requires_linux_owner
def test_a_differential_flag_on_owner_path_equals_flag_off_direct_path_on_all_three_columns(home, monkeypatch):
    diffs, (a, off_stats, _), (b, on_stats, hook_on), served = differential(home, monkeypatch)
    assert len(served) == 1                             # the ON side really went through the owner
    assert diffs == []
    # the fixtures really exercised the four cases and moved all three columns
    assert off_stats == on_stats == {"mentioned_count": 5, "updated_count": 4}
    assert b["ob_0001"][0] == 0.1                       # NULL utility
    assert b["ob_0002"][0] == 0.55                      # duplicate id bumped once
    assert b["ob_0004"][0] == 1.0                       # Q ~ 1.0
    assert "ob_0099" not in b                           # missing id: no row created
    for nid in ("ob_0001", "ob_0002", "ob_0004", "ob_0005"):
        assert b[nid][2] == CONFIG_TURN                 # recall_turn restamped
    assert b["ob_0006"] == (0.33, 0.6, 4)               # untargeted node untouched
    assert bump_lines(hook_on) == []


@requires_linux_owner
def test_a_differential_with_an_absent_memory_block_stamps_turn_zero_on_both_paths(home, monkeypatch):
    diffs, _, (b, _, _), served = differential(home, monkeypatch, config_kw={"memory": False})
    assert len(served) == 1
    assert diffs == []
    assert b["ob_0001"][2] == 0


@requires_linux_owner
def test_a_the_harness_can_fail_an_owner_path_that_drops_the_restamp_half_is_seen(home, monkeypatch):
    """Mutate the NEW side: the owner op runs a bump-only variant (what a bump-only op would do). The same harness must go
    red on importance_score and recall_turn - and NOT on utility_score."""
    def drop_restamp(owner, mp):
        def bump_only(conn, ids, action, **kw):
            upd = er.bump_utility(conn, ids, er.USE_ALPHA[action])
            return _BumpOnly(len(upd))
        mp.setattr(eo, "er", _ErProxy(bump_only))   # the OWNER's view only; the hook's direct path is untouched

    diffs, _, _, served = differential(home, monkeypatch, mutate=drop_restamp)
    assert len(served) == 1
    cols = {d[1] for d in diffs if d[0] != "stats"}
    assert "recall_turn" in cols and "importance_score" in cols, diffs
    assert "utility_score" not in cols, diffs


class _ErProxy:
    def __init__(self, use_reward):
        self.use_reward = use_reward

    def __getattr__(self, name):
        return getattr(er, name)


class _BumpOnly:
    def __init__(self, n):
        self.bumped, self.restamped, self.restamp_error = n, 0, None


# --- (b) zero opens with the flag ON, one full fire -----------------------------------------------------


@requires_linux_owner
def test_b_flag_on_a_full_fire_opens_the_store_zero_times(home, monkeypatch):
    db = make_store(home, flag=True)
    o = start_owner(home)
    try:
        hook = load_hook(home, monkeypatch)
        counter = StoreOpenCounter(monkeypatch, db)
        run_main(hook, monkeypatch)
    finally:
        o.stop()
    assert counter.hits == []                           # empty allow-list
    assert three(db)["ob_0001"][0] == 0.1               # and the bump really happened (through the owner)


@requires_linux_owner
def test_b_control_the_counter_sees_the_direct_path_flag_off(home, monkeypatch):
    """The counter is not vacuous: the flag-OFF path opens the store (and so reaches the WAL pragma)."""
    db = make_store(home, flag=False)
    hook = load_hook(home, monkeypatch)
    counter = StoreOpenCounter(monkeypatch, db)
    run_main(hook, monkeypatch)
    assert len(counter.hits) >= 1


# --- (c) owner down --------------------------------------------------------------------------------------


def test_c_owner_down_is_a_loud_loss_no_store_open_and_enactment_detection_still_runs(home, monkeypatch):
    db = make_store(home, flag=True)                    # no owner started: no socket
    hook = load_hook(home, monkeypatch)
    before = all_rows(db)
    seen = []
    monkeypatch.setattr(hook, "_check_enactments", lambda ids, **k: seen.append(list(ids)))
    counter = StoreOpenCounter(monkeypatch, db)
    stats = hook.credit_mentions(TEXT)
    assert counter.hits == []
    assert stats == {"mentioned_count": 5, "updated_count": 0}
    assert seen == [MENTIONED]                          # enactment detection ran, independent of the bump
    lines = bump_lines(hook)
    assert len(lines) == 1 and lines[0].startswith("bump_utility failed: owner unavailable"), lines
    assert "reason=down" in lines[0]
    assert all_rows(db) == before                       # nothing written, no fallback


def test_c_owner_down_main_exit_and_stdout_unchanged(home, monkeypatch, capsys):
    make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)
    assert run_main(hook, monkeypatch) is None
    out = capsys.readouterr()
    assert out.out == ""


def test_c_an_enactment_failure_and_a_bump_failure_stay_independent(home, monkeypatch):
    make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)

    def boom(ids, **k):
        raise RuntimeError("enactment broke")
    monkeypatch.setattr(hook, "_check_enactments", boom)
    hook.credit_mentions(TEXT)
    text = "\n".join(bump_lines(hook))
    assert "enactment check failed" in "\n".join(log_lines(hook))
    assert "bump_utility failed: owner unavailable" in text


def test_c_an_unimportable_client_is_a_loud_loss_not_a_store_open(home, monkeypatch):
    db = make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)

    def no_client():
        raise ImportError("no engramd_client here")
    monkeypatch.setattr(hook, "_import_engramd_client", no_client)
    counter = StoreOpenCounter(monkeypatch, db)
    stats = hook.credit_mentions(TEXT)
    assert counter.hits == [] and stats["updated_count"] == 0
    assert any(l.startswith("bump_utility failed: owner client unavailable") and "ImportError" in l for l in bump_lines(hook))


def test_c_a_failing_flag_helper_reads_off_and_is_recorded_not_silent(home, monkeypatch):
    make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)

    def bad_helper():
        raise OSError("helper exploded")
    monkeypatch.setattr(hook, "_import_owner_flags", bad_helper)
    stats = hook.credit_mentions(TEXT)
    assert stats["updated_count"] == 4                  # direct path (flag unknown reads OFF)
    lines = bump_lines(hook)
    assert any("owner flag helper failed" in l and "OSError" in l and "helper exploded" not in l for l in lines), lines


# --- (d) skew ----------------------------------------------------------------------------------------------


@requires_linux_owner
def test_d_skew_an_owner_without_use_reward_gets_the_specific_line_and_no_write(home, monkeypatch):
    db = make_store(home, flag=True)
    o = start_owner(home)
    try:
        del o._methods["use_reward"]
        o._method_kinds.pop("use_reward", None)
        hook = load_hook(home, monkeypatch)
        before = all_rows(db)
        counter = StoreOpenCounter(monkeypatch, db)
        stats = hook.credit_mentions(TEXT)
    finally:
        o.stop()
    assert counter.hits == [] and all_rows(db) == before and stats["updated_count"] == 0
    lines = bump_lines(hook)
    assert len(lines) == 1, lines
    assert lines[0].startswith("bump_utility failed: owner rejected: unknown_operation"), lines
    assert "contract skew" in lines[0] and "restart" in lines[0] and "upgrade" in lines[0]
    # the generic rejected line differs from the skew line
    assert lines[0] != "bump_utility failed: owner rejected: unknown_operation"


# --- fake-client helpers ---------------------------------------------------------------------------------


def _unavail(reason, rid=None):
    return ec.Unavailable(socket_path="/x", bound=1.0, agent_steps=["a"], human_steps=["h"],
                          detail="owner prose that must never reach the log", request_id=rid, reason=reason)


def _rej(reason, rid=None, commit=None):
    return ec.Rejected(reason=reason, detail="owner prose that must never reach the log", remedy="r", request_id=rid,
                       commit=commit)


def _current(obj):
    """Rebind a scripted client result to the CURRENT classes. `importlib.reload(ec)` (done by
    tests/test_engramd_platform_arm_3712.py, legitimately) re-executes engramd_client in place, so `ec.Ok` /
    `ec.Rejected` / `ec.Unavailable` become new class objects, while results built at COLLECTION time (parametrize
    argument lists, module constants) are instances of the old ones and fail the hook's `isinstance` checks. A frozen
    dataclass instance whose class is named Ok / Rejected / Unavailable but is not the current `ec.<Name>` is rebuilt
    from its fields with the current class (every field kept: commit, request_id, steps, ...); anything else
    (strings, None, already-current objects) is returned unchanged."""
    import dataclasses
    name = type(obj).__name__
    if name in ("Ok", "Rejected", "Unavailable") and dataclasses.is_dataclass(obj):
        cls = getattr(ec, name)
        if not isinstance(obj, cls):
            return cls(**{f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)})
    return obj


class FakeWrite:
    """Replaces ec.write: records (method, params, request_id, timeout), advances a fake clock by each timeout it
    'waits', answers from a script of results (callables get the call record)."""

    def __init__(self, script, clock=None):
        self.script, self.calls, self.clock = list(script), [], clock

    def __call__(self, method, params, *, request_id=None, timeout=None, socket_path=None):
        self.calls.append({"method": method, "params": params, "request_id": request_id, "timeout": timeout})
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        res = _current(step(self.calls[-1]) if callable(step) else step)
        if isinstance(res, ec.Unavailable) and res.reason in ("timeout", "protocol") and self.clock is not None:
            self.clock[0] += timeout
        return res


def _ok(bumped=4, restamp_error=None, duplicate=False):
    return ec.Ok(result={"updated": [], "bumped": bumped, "restamped": bumped, "restamp_error": restamp_error,
                         "rows": {}, "duplicate": duplicate}, request_id="x")


def with_fake(home, monkeypatch, script, *, clock=None):
    db = make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)
    fake = FakeWrite(script, clock)
    monkeypatch.setattr(ec, "write", fake)
    return hook, fake, db


# --- (e) readback_mismatch, restamp_error ---------------------------------------------------------------


def test_e_readback_mismatch_says_committed_but_unverified_and_not_to_repeat_blindly(home, monkeypatch):
    hook, fake, db = with_fake(home, monkeypatch, [_rej("readback_mismatch", commit="committed_unverified")])
    before = all_rows(db)
    stats = hook.credit_mentions(TEXT)
    assert len(fake.calls) == 1                         # committed: never retried here, never a fallback
    assert stats["updated_count"] == 0 and all_rows(db) == before
    (line,) = bump_lines(hook)
    assert "committed but unverified" in line and "may have been applied once" in line
    assert "blindly" in line and "owner prose" not in line
    assert not line.startswith("bump_utility failed: owner rejected: readback_mismatch")


@requires_linux_owner
@pytest.mark.parametrize("kind", ["torn", "directory"])
def test_e_restamp_error_is_logged_as_today_the_bump_counts_and_matches_the_direct_path(home, monkeypatch, kind):
    ha, hb = home / "off", home / "on"
    ha.mkdir(), hb.mkdir()
    dba, dbb = make_store(ha, flag=False), make_store(hb, flag=True)
    for h in (ha, hb):
        cfg = h / "config.json"
        cfg.unlink()
        if kind == "torn":
            cfg.write_text('{"memory": {"current_turn": 3, ')
        else:
            cfg.mkdir()
    # the flag must still be ON for hb: a config that is torn/directory reads OFF through flag_on, so
    # pin the flag by patching only the flag read; the OWNER's own config read is what is under test.
    hook_off = load_hook(ha, monkeypatch)
    s_off = hook_off.credit_mentions(TEXT)
    o = start_owner(hb)
    try:
        hook_on = load_hook(hb, monkeypatch)
        monkeypatch.setattr(hook_on, "_owner_writes_enabled", lambda: True)
        s_on = hook_on.credit_mentions(TEXT)
    finally:
        o.stop()
    assert s_off == s_on == {"mentioned_count": 5, "updated_count": 4}
    assert three(dba) == three(dbb)
    # the message names the config path (each side's own scratch home): normalise that one difference
    off_l = [l.replace(str(ha), "<H>") for l in bump_lines(hook_off) if l.startswith("restamp failed:")]
    on_l = [l.replace(str(hb), "<H>") for l in bump_lines(hook_on) if l.startswith("restamp failed:")]
    assert len(off_l) == len(on_l) == 1 and off_l == on_l


def test_e_restamp_error_in_an_ack_is_logged_and_the_bump_still_counts(home, monkeypatch):
    hook, fake, _ = with_fake(home, monkeypatch, [_ok(bumped=3, restamp_error="config: unreadable")])
    stats = hook.credit_mentions(TEXT)
    assert stats["updated_count"] == 3
    assert bump_lines(hook) == ["restamp failed: config: unreadable"]


# --- (f) wire params --------------------------------------------------------------------------------------


UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@requires_linux_owner
def test_f_the_wire_params_are_exactly_node_ids_action_request_id_and_the_rid_is_one_uuid4(home, monkeypatch):
    make_store(home, flag=True)
    o = start_owner(home)
    try:
        hook = load_hook(home, monkeypatch)
        real = ec.write
        calls = []

        def spy(method, params, **k):
            calls.append((method, json.loads(json.dumps(params)), k))
            return real(method, params, **k)
        monkeypatch.setattr(ec, "write", spy)
        hook.credit_mentions(TEXT)
        hook.credit_mentions("again ob_0005")
    finally:
        o.stop()
    assert len(calls) == 2
    (m1, p1, k1), (m2, p2, k2) = calls
    assert m1 == "use_reward"
    assert set(p1) == {"node_ids", "action", "request_id"}                  # no alpha, value, config path
    assert p1["node_ids"] == MENTIONED and p1["action"] == "mention"
    assert UUID4.match(p1["request_id"]) and k1["request_id"] == p1["request_id"]   # same id in params and envelope
    assert UUID4.match(p2["request_id"]) and p2["request_id"] != p1["request_id"]    # fresh per fire
    assert isinstance(k1["timeout"], float) and 0 < k1["timeout"]
    assert p2["node_ids"] == ["ob_0005"]


# --- (g) explicit alpha / restamp=False refused ------------------------------------------------------------


@requires_linux_owner
def test_g_an_explicit_alpha_with_the_flag_on_is_refused_loudly_and_writes_nothing(home, monkeypatch):
    db = make_store(home, flag=True)
    o = start_owner(home)
    try:
        hook = load_hook(home, monkeypatch)
        spy = FakeWrite([_ok()])
        monkeypatch.setattr(ec, "write", spy)
        before = all_rows(db)
        counter = StoreOpenCounter(monkeypatch, db)
        with pytest.raises(ValueError, match="alpha"):
            hook.bump_utility(MENTIONED, 0.3, str(db), restamp=True)
        stats = hook.credit_mentions(TEXT, alpha=0.3)
    finally:
        o.stop()
    assert spy.calls == [] and counter.hits == [] and all_rows(db) == before
    assert stats["updated_count"] == 0
    (line,) = bump_lines(hook)
    assert line.startswith("bump_utility failed:") and "alpha" in line


def test_g_restamp_false_with_the_flag_on_is_refused_too(home, monkeypatch):
    db = make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)
    spy = FakeWrite([_ok()])
    monkeypatch.setattr(ec, "write", spy)
    with pytest.raises(ValueError, match="restamp"):
        hook.bump_utility(MENTIONED, None, str(db), restamp=False)
    assert spy.calls == []


def test_g_flag_off_an_explicit_alpha_is_honoured_as_today(home, monkeypatch):
    db = make_store(home, flag=False)
    hook = load_hook(home, monkeypatch)
    assert hook.bump_utility(["ob_0002"], 0.5, str(db), restamp=True, config_path=hook.CONFIG_PATH) == 1
    assert three(db)["ob_0002"][0] == 0.75


# --- (h) flag OFF: byte-identical, owner client never touched ----------------------------------------------


def _base_hook_source() -> str:
    """The vendored unmodified hook. A missing fixture is a hard failure, never a skip."""
    assert BASE_HOOK_FIXTURE.is_file(), f"the vendored baseline hook is missing: {BASE_HOOK_FIXTURE}"
    return BASE_HOOK_FIXTURE.read_bytes().decode("utf-8")


def test_h_the_vendored_baseline_is_the_real_unmodified_hook():
    """Provenance only (NOT the safety proof): the fixture equals the hook at BASE_COMMIT and holds no trace of the flag.
    Skips only when that commit is not in this clone."""
    text = _base_hook_source()
    assert "utility_credit_writes_via_owner" not in text and "_owner_use_reward" not in text
    r = subprocess.run(["git", "show", f"{BASE_COMMIT}:src/engram/hooks/claude/engram-utility-credit-mention-stop.py"],
                       cwd=REPO_ROOT, capture_output=True)
    if r.returncode != 0:
        pytest.skip(f"commit {BASE_COMMIT} is not available in this clone (shallow checkout): provenance not checked")
    assert r.stdout.decode("utf-8") == text


@pytest.mark.parametrize("state", ["absent", "false", "none", "string-true", "one", "no-engramd-config"])
def test_h_flag_off_equals_the_unmodified_hook_store_log_and_count(state, tmp_path, monkeypatch):
    src = _base_hook_source()
    flag = {"absent": ..., "false": False, "none": None, "string-true": "true", "one": 1, "no-engramd-config": ...}[state]
    results = []
    for label in ("old", "new"):
        h = tmp_path / label
        h.mkdir()
        db = make_store(h, flag=flag, raw_config=None)
        if state == "no-engramd-config":
            (h / "config.json").unlink()
        if label == "old":
            # the unmodified hook, placed beside the real _hooklib so its imports resolve
            hook_dir = HOOK_PATH.parent
            shutil.copytree(hook_dir, tmp_path / "old_hook", ignore=shutil.ignore_patterns("__pycache__"))
            p = tmp_path / "old_hook" / "unmodified.py"
            p.write_text(src)
            hook = load_hook(h, monkeypatch, p, runtime=True)
        else:
            hook = load_hook(h, monkeypatch, runtime=True)
        stats = hook.credit_mentions(TEXT)
        results.append((three(db), all_rows(db), stats, [re.sub(r"^\[[^\]]*\] ", "", l) for l in log_lines(hook)]))
    assert results[0] == results[1]


def test_h_flag_off_never_imports_or_calls_the_owner_client(home, monkeypatch):
    make_store(home, flag=False)
    hook = load_hook(home, monkeypatch)
    spy = FakeWrite([_ok()])
    monkeypatch.setattr(ec, "write", spy)

    def tripwire():
        raise AssertionError("the owner client was imported with the flag OFF")
    monkeypatch.setattr(hook, "_import_engramd_client", tripwire)
    assert hook.credit_mentions(TEXT)["updated_count"] == 4
    assert spy.calls == []


# --- (i) there is no fall back to the store on any non-Ok outcome --------------------------------------------


@pytest.mark.parametrize("res", [
    _unavail("down"), _unavail("local"), _unavail("busy"), _unavail("busy_or_stuck"), _unavail("internal"),
    _unavail(None), _rej("busy"), _rej("write_denied"), _rej("unknown_operation"),
    _rej("readback_mismatch", commit="committed_unverified"), _rej("use_reward_owner_busy"),
    _unavail("timeout"), _unavail("protocol"), "garbage", None, ec.Ok(result="not-a-dict"),
    ec.Ok(result={"bumped": "four"}),
], ids=lambda r: getattr(r, "reason", None) or repr(r)[:30])
def test_i_no_outcome_other_than_a_good_ack_ever_opens_the_store_or_changes_it(home, monkeypatch, res):
    clock = [0.0]
    db = make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(ec, "write", FakeWrite([res], clock))
    before = all_rows(db)
    counter = StoreOpenCounter(monkeypatch, db)
    stats = hook.credit_mentions(TEXT)
    assert counter.hits == [] and all_rows(db) == before and stats["updated_count"] == 0
    assert bump_lines(hook), "silent loss"


# --- classification and the retry rule ---------------------------------------------------------------------


def test_busy_rejection_is_determinate_one_call_no_retry_and_its_own_words(home, monkeypatch):
    hook, fake, _ = with_fake(home, monkeypatch, [_rej("busy")])
    hook.credit_mentions(TEXT)
    assert len(fake.calls) == 1
    assert bump_lines(hook) == ["bump_utility failed: owner busy (not applied)"]


def test_results_built_before_a_reload_of_the_client_still_classify_as_typed_results(home, monkeypatch):
    """REGRESSION (CI gate 2, #3859): `tests/test_engramd_platform_arm_3712.py` re-executes engramd_client IN PLACE
    (`importlib.reload(ec)`), so `ec.Rejected` / `ec.Unavailable` / `ec.Ok` become NEW class objects. Scripted results
    built at COLLECTION time (parametrize argument lists, module constants) are then instances of the OLD classes and
    the hook's `isinstance(result, ec.Rejected)` is False: 'unusable answer', no retry. The fake owner rebinds each
    result it returns to the CURRENT classes. Mutation: drop the rebind -> both flows below go red."""
    import importlib
    busy, timeout = _rej("busy"), _unavail("timeout")        # built from the classes as they are NOW
    try:
        importlib.reload(ec)                                 # exactly what platform_arm does inside its test
        assert not isinstance(busy, ec.Rejected) and not isinstance(timeout, ec.Unavailable)     # the precondition
        hook, fake, _ = with_fake(home, monkeypatch, [busy])
        hook.credit_mentions(TEXT)
        assert len(fake.calls) == 1
        assert bump_lines(hook) == ["bump_utility failed: owner busy (not applied)"]
        clock = [0.0]
        (home / "b").mkdir()
        hook, fake, _ = with_fake(home / "b", monkeypatch, [timeout, busy], clock=clock)
        monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
        hook.credit_mentions(TEXT)
        assert len(fake.calls) == 2 and fake.calls[0]["request_id"] == fake.calls[1]["request_id"]
        assert bump_lines(hook) == [RETRY_LINE.format(code="busy")]
    finally:
        importlib.reload(ec)


@pytest.mark.parametrize("res,frag", [
    (_rej("write_denied"), "bump_utility failed: owner rejected: write_denied"),
    (_rej("use_reward_owner_busy"), "bump_utility failed: owner rejected: use_reward_owner_busy"),
    (_unavail("down"), "bump_utility failed: owner unavailable (reason=down)"),
    (_unavail("local"), "bump_utility failed: owner unavailable (reason=local)"),
])
def test_other_determinate_outcomes_are_never_retried(home, monkeypatch, res, frag):
    hook, fake, _ = with_fake(home, monkeypatch, [res])
    hook.credit_mentions(TEXT)
    assert len(fake.calls) == 1
    assert bump_lines(hook) == [frag]


def test_owner_prose_never_reaches_the_log(home, monkeypatch):
    hook, _, _ = with_fake(home, monkeypatch, [_rej("write_denied")])
    hook.credit_mentions(TEXT)
    assert "owner prose" not in Path(hook.LOG_PATH).read_text()
    (home / "b").mkdir()
    hook2, _, _ = with_fake(home / "b", monkeypatch, [_rej("weird reason with spaces\nand newline")])
    hook2.credit_mentions(TEXT)
    assert all("\n" not in l for l in bump_lines(hook2)) and "spaces" not in "".join(bump_lines(hook2))


@pytest.mark.parametrize("reason", ["timeout", "protocol"])
def test_indeterminate_twice_retries_once_with_the_same_request_id_and_never_says_failed(home, monkeypatch, reason):
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch, [_unavail(reason)], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    stats = hook.credit_mentions(TEXT)
    assert len(fake.calls) == 2
    assert fake.calls[0]["request_id"] == fake.calls[1]["request_id"]
    assert fake.calls[0]["params"]["request_id"] == fake.calls[1]["params"]["request_id"] == fake.calls[0]["request_id"]
    assert stats["updated_count"] == 0
    lines = bump_lines(hook)
    assert lines == ["bump_utility indeterminate: owner did not answer (may have been applied once)"]
    assert "failed" not in "\n".join(lines)


def test_indeterminate_then_the_stored_outcome_is_the_normal_result(home, monkeypatch):
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch,
                              [_unavail("timeout"), _ok(bumped=4, restamp_error="config: x", duplicate=True)], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    stats = hook.credit_mentions(TEXT)
    assert len(fake.calls) == 2 and stats["updated_count"] == 4
    assert bump_lines(hook) == ["restamp failed: config: x"]


RETRY_LINE = "bump_utility indeterminate: {code}; the first attempt may have been applied once"


@pytest.mark.parametrize("first,second,code", [
    (_unavail("timeout"), _rej("busy"), "busy"),                                   # (a) the review's finding
    (_unavail("protocol"), _unavail("down"), "unavailable_down"),                  # (b)
    (_unavail("timeout"), _rej("write_denied"), "write_denied"),
    (_unavail("timeout"), _rej("unknown_operation"), "unknown_operation"),
    (_unavail("protocol"), _unavail("local"), "unavailable_local"),
    (_unavail("timeout"), _unavail(None), "unavailable_unknown"),
    (_unavail("timeout"), "garbage", "unusable_answer"),
    (_unavail("timeout"), None, "unusable_answer"),
    (_unavail("timeout"), ec.Ok(result="not-a-dict"), "unusable_answer"),
], ids=lambda v: getattr(v, "reason", None) or str(v)[:24])
def test_after_an_indeterminate_first_attempt_every_non_ok_retry_outcome_is_indeterminate_never_not_applied(
        home, monkeypatch, first, second, code):
    clock = [0.0]
    hook, fake, db = with_fake(home, monkeypatch, [first, second], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    before = all_rows(db)
    stats = hook.credit_mentions(TEXT)
    assert len(fake.calls) == 2 and fake.calls[0]["request_id"] == fake.calls[1]["request_id"]
    assert stats["updated_count"] == 0 and all_rows(db) == before
    lines = bump_lines(hook)
    assert lines == [RETRY_LINE.format(code=code)], lines
    assert "not applied" not in lines[0] and "failed" not in lines[0]


def test_after_an_indeterminate_first_attempt_readback_mismatch_on_the_retry_stays_committed_but_unverified(home, monkeypatch):
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch,
                              [_unavail("timeout"), _rej("readback_mismatch", commit="committed_unverified")], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    hook.credit_mentions(TEXT)
    (line,) = bump_lines(hook)
    assert "committed but unverified" in line and "not applied" not in line


def test_c_a_first_attempt_busy_is_determinate_still_not_applied_and_never_retried(home, monkeypatch):
    hook, fake, _ = with_fake(home, monkeypatch, [_rej("busy"), _ok()])
    hook.credit_mentions(TEXT)
    assert len(fake.calls) == 1
    assert bump_lines(hook) == ["bump_utility failed: owner busy (not applied)"]


@requires_linux_owner
def test_d_real_owner_first_attempt_times_out_after_commit_then_the_retry_gets_busy_and_the_line_is_indeterminate(home, monkeypatch):
    """REAL owner. Attempt 1 (client timeout 0.3 s) COMMITS, then the owner keeps its lock for 1.8 s while the reply is
    lost. The retry (same request_id, 2.5 s client timeout so the typed answer can arrive) waits the owner's 1.0 s lock
    budget and gets the typed Rejected(busy). The bump WAS applied (once), so the line must not say 'not applied'.
    The ordering is fixed by the owner's hard 1.0 s lock-wait bound vs the 1.8 s hold, not by a race."""
    db = make_store(home, flag=True)
    o = start_owner(home)
    try:
        real_method = o._methods["use_reward"]
        n = [0]

        def commit_then_hold(params, deadline):
            r = real_method(params, deadline)
            n[0] += 1
            if n[0] == 1:
                with o._owner_lock:
                    time.sleep(1.8)
            return r
        o._methods["use_reward"] = commit_then_hold
        monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", 0.3)
        hook = load_hook(home, monkeypatch)
        real_write = ec.write
        calls = []

        def spy(method, params, **k):
            if calls:                                    # the retry only: let the typed answer arrive
                k = dict(k, timeout=2.5)
            res = real_write(method, params, **k)
            calls.append((params["request_id"], res))
            return res
        monkeypatch.setattr(ec, "write", spy)
        stats = hook.credit_mentions(TEXT)
        time.sleep(0.6)                                  # let the owner's first handler finish before stopping it
    finally:
        o.stop()
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert isinstance(calls[0][1], ec.Unavailable) and calls[0][1].reason == "timeout"
    assert isinstance(calls[1][1], ec.Rejected) and calls[1][1].reason == "busy"
    assert stats["updated_count"] == 0
    assert bump_lines(hook) == [RETRY_LINE.format(code="busy")]
    cols = three(db)
    assert cols["ob_0001"][0] == 0.1 and cols["ob_0002"][0] == 0.55       # applied exactly once


@requires_linux_owner
def test_reply_lost_after_commit_the_single_retry_gets_the_stored_outcome_and_the_node_is_bumped_exactly_once(home, monkeypatch):
    """The owner COMMITS, then its reply arrives after the client timeout. The hook's one retry (same request_id) is
    answered from the dedupe store (duplicate=true): bumped once, count right."""
    db = make_store(home, flag=True)
    o = start_owner(home)
    try:
        real_method = o._methods["use_reward"]
        n = [0]

        def slow_after_commit(params, deadline):
            r = real_method(params, deadline)
            n[0] += 1
            if n[0] == 1:
                time.sleep(0.7)
            return r
        o._methods["use_reward"] = slow_after_commit
        monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", 0.4, raising=False)
        hook = load_hook(home, monkeypatch)
        real_write = ec.write
        calls = []

        def spy(method, params, **k):
            res = real_write(method, params, **k)
            calls.append((params["request_id"], k.get("request_id"), res))
            return res
        monkeypatch.setattr(ec, "write", spy)
        stats = hook.credit_mentions(TEXT)
    finally:
        o.stop()
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == calls[0][1] == calls[1][1]
    assert isinstance(calls[0][2], ec.Unavailable) and calls[0][2].reason == "timeout"
    assert isinstance(calls[1][2], ec.Ok) and calls[1][2].result["duplicate"] is True
    assert stats["updated_count"] == 4
    cols = three(db)
    assert cols["ob_0001"][0] == 0.1 and cols["ob_0002"][0] == 0.55          # bumped exactly once
    assert cols["ob_0002"][2] == CONFIG_TURN
    assert bump_lines(hook) == []


@requires_linux_owner
def test_a_retry_with_a_NEW_request_id_would_double_bump_this_cell_pins_the_same_id(home, monkeypatch):
    """The discriminating partner of the cell above: the store value after a lost reply is 0.1 only if the retry reuses the
    id. (A hook that regenerated the id would leave ob_0001 at 0.1 + 0.1*0.9 = 0.19.)"""
    db = make_store(home, flag=True)
    o = start_owner(home)
    try:
        real_method = o._methods["use_reward"]
        n = [0]

        def slow(params, deadline):
            r = real_method(params, deadline)
            n[0] += 1
            if n[0] == 1:
                time.sleep(0.7)
            return r
        o._methods["use_reward"] = slow
        monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", 0.4, raising=False)
        hook = load_hook(home, monkeypatch)
        hook.credit_mentions(TEXT)
    finally:
        o.stop()
    assert n[0] == 2                                    # the owner handled two requests (the second a replay)
    assert three(db)["ob_0001"][0] == 0.1 != 0.19


# --- the timeout arithmetic -------------------------------------------------------------------------------


def _stop_hook_timeout() -> float:
    cfg = json.loads(HOOKS_JSON.read_text())
    found = []

    def walk(x):
        if isinstance(x, dict):
            if "engram-utility-credit-mention-stop.py" in str(x.get("command", "")):
                found.append(x["timeout"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(cfg)
    assert len(found) == 1
    return float(found[0])


def test_the_registered_stop_hook_timeout_is_five_seconds():
    assert _stop_hook_timeout() == 5.0


def test_the_owner_write_budget_fits_inside_the_registered_stop_hook_timeout(home, monkeypatch):
    make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)
    # both attempts together, with a floor left for interpreter start, telemetry, enactment and the emitter
    assert hook.OWNER_WRITE_BUDGET_S + hook.OWNER_WRITE_RESERVE_S <= _stop_hook_timeout()
    assert hook.OWNER_WRITE_RESERVE_S >= 1.0


@pytest.mark.parametrize("first", [0.5, 2.0, 10.0])
def test_two_attempts_never_wait_longer_than_the_budget_whatever_the_client_constant(home, monkeypatch, first):
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch, [_unavail("timeout")], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", first, raising=False)
    hook.credit_mentions(TEXT)
    waited = sum(c["timeout"] for c in fake.calls)
    assert waited <= hook.OWNER_WRITE_BUDGET_S + 1e-9
    assert all(0 < c["timeout"] <= first for c in fake.calls)
    assert len(fake.calls) <= 2


def test_the_hook_passes_the_clients_named_timeout_when_it_exists(home, monkeypatch):
    hook, fake, _ = with_fake(home, monkeypatch, [_ok()])
    monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", 1.25, raising=False)
    hook.credit_mentions(TEXT)
    assert fake.calls[0]["timeout"] == 1.25


def test_the_hook_uses_the_clients_own_constant_not_a_copy_of_the_number(home, monkeypatch):
    """Unpatched, the timeout the hook sends is exactly `engramd_client.OWNER_WRITE_CLIENT_TIMEOUT` (2.0 s), and the hook source
    holds no local timeout constant. Mutation: hard-code another value in the hook -> red here and in the patched-constant cell."""
    hook, fake, _ = with_fake(home, monkeypatch, [_ok()])
    hook.credit_mentions(TEXT)
    assert fake.calls[0]["timeout"] == ec.OWNER_WRITE_CLIENT_TIMEOUT == 2.0
    assert not hasattr(hook, "_OWNER_WRITE_CLIENT_TIMEOUT_FALLBACK")


def test_worst_case_total_wait_with_the_real_constants_is_the_budget_inside_the_stop_timeout(home, monkeypatch):
    """First attempt waits the whole 2.0 s and times out; the retry gets 3.5 - 2.0 = 1.5 s; total 3.5 s; 1.5 s remain of 5."""
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch, [_unavail("timeout")], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    hook.credit_mentions(TEXT)
    assert [c["timeout"] for c in fake.calls] == [2.0, 1.5]
    assert sum(c["timeout"] for c in fake.calls) == 3.5 == hook.OWNER_WRITE_BUDGET_S
    assert _stop_hook_timeout() - 3.5 == hook.OWNER_WRITE_RESERVE_S == 1.5


@requires_linux_owner
def test_a_real_owner_whose_write_lock_is_held_past_the_budget_answers_busy_nothing_bumped_no_retry(home, monkeypatch):
    """An external writer holds the store's write lock for longer than the owner's lock wait (1.0 s): the REAL owner answers the
    typed Rejected(busy). The hook logs the determinate busy line, makes ONE call (no retry), opens nothing, bumps nothing."""
    db = make_store(home, flag=True)
    o = start_owner(home)
    holder = sqlite3.connect(str(db), timeout=5.0, isolation_level=None)
    try:
        hook = load_hook(home, monkeypatch)
        real = ec.write
        calls = []

        def spy(method, params, **k):
            res = real(method, params, **k)
            calls.append(res)
            return res
        monkeypatch.setattr(ec, "write", spy)
        before = all_rows(db)
        holder.execute("BEGIN IMMEDIATE")
        counter = StoreOpenCounter(monkeypatch, db)
        stats = hook.credit_mentions(TEXT)
        hits = list(counter.hits)                       # before the test's own reads below
        holder.execute("ROLLBACK")
        assert all_rows(db) == before
    finally:
        holder.close()
        o.stop()
    assert len(calls) == 1 and isinstance(calls[0], ec.Rejected) and calls[0].reason == "busy"
    assert hits == [] and stats["updated_count"] == 0
    assert bump_lines(hook) == ["bump_utility failed: owner busy (not applied)"]


# --- the deployed FLAT layout --------------------------------------------------------------------------------

_DRIVER = """
import sys, json, os
sys.path.insert(0, sys.argv[1])
import importlib.util
spec = importlib.util.spec_from_file_location("h", sys.argv[2])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
stats = m.credit_mentions(sys.argv[3])
print(json.dumps({"stats": stats, "client_imported": "engramd_client" in sys.modules,
                  "restamp_imported": "engram_restamp" in sys.modules}))
"""


def _flat_root(tmp: Path, *, without=()) -> Path:
    root = tmp / "plugin"
    (root / "hooks").mkdir(parents=True)
    for p in HOOK_PATH.parent.glob("*.py"):
        shutil.copy(p, root / "hooks" / p.name)
    for p in Path(_SRC_ENGRAM).glob("*.py"):
        if p.name not in without:
            shutil.copy(p, root / p.name)
    shutil.copytree(REPO_ROOT / "tools", root / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _run_flat(root: Path, home: Path, tmp: Path):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "ENGRAM_RUNTIME_DIR")}
    env["ENGRAM_HOME"] = str(home)
    other = tmp / "elsewhere"
    other.mkdir(exist_ok=True)
    r = subprocess.run([sys.executable, "-I", "-c", _DRIVER, str(root / "hooks"),
                        str(root / "hooks" / "engram-utility-credit-mention-stop.py"), TEXT],
                       cwd=other, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@requires_linux_owner
def test_flat_layout_flag_on_resolves_the_client_beside_the_hooks_and_needs_no_engram_restamp(home, tmp_path):
    db = make_store(home, flag=True)
    root = _flat_root(tmp_path, without=("engram_restamp.py",))   # the hook cannot import engram_restamp at all
    o = start_owner(home)
    try:
        out = _run_flat(root, home, tmp_path)
    finally:
        o.stop()
    assert out["stats"] == {"mentioned_count": 5, "updated_count": 4}
    assert out["client_imported"] is True and out["restamp_imported"] is False
    assert three(db)["ob_0001"][0] == 0.1


@requires_linux_owner
def test_flat_layout_flag_off_never_imports_the_client_and_uses_the_direct_path(home, tmp_path):
    db = make_store(home, flag=False)
    root = _flat_root(tmp_path)
    out = _run_flat(root, home, tmp_path)
    assert out["stats"] == {"mentioned_count": 5, "updated_count": 4}
    assert out["client_imported"] is False and out["restamp_imported"] is True
    assert three(db)["ob_0001"][0] == 0.1


# --- reviewer round 1 folds (PR #3847) ---------------------------------------------------------------------------


def test_fold1_an_owner_supplied_restamp_error_is_one_bounded_line_and_the_count_is_still_returned(home, monkeypatch):
    nasty = "config: boom\nsecond line\r\nthird\t\x1b[31m" + "x" * 5000
    hook, fake, _ = with_fake(home, monkeypatch, [_ok(bumped=3, restamp_error=nasty)])
    stats = hook.credit_mentions(TEXT)
    assert stats["updated_count"] == 3
    raw = Path(hook.LOG_PATH).read_text()
    assert raw.count("\n") == 1                                     # exactly one log line, no embedded newline
    (line,) = bump_lines(hook)
    assert line.startswith("restamp failed: config: boom second line")
    assert len(line) <= len("restamp failed: ") + hook.OWNER_RESTAMP_ERROR_MAX + len("...")
    assert all(ch.isprintable() for ch in line)


def test_fold1_a_short_restamp_error_is_logged_verbatim(home, monkeypatch):
    hook, _, _ = with_fake(home, monkeypatch, [_ok(bumped=2, restamp_error="config: IsADirectoryError: x")])
    hook.credit_mentions(TEXT)
    assert bump_lines(hook) == ["restamp failed: config: IsADirectoryError: x"]


def _flag_helper_raising(exc):
    def helper():
        raise exc
    return helper


def test_fold2_a_flag_helper_systemexit_reads_off_is_recorded_by_class_and_the_direct_path_is_used(home, monkeypatch):
    db = make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)
    monkeypatch.setattr(hook, "_import_owner_flags", _flag_helper_raising(SystemExit(3)))
    spy = FakeWrite([_ok()])
    monkeypatch.setattr(ec, "write", spy)
    stats = hook.credit_mentions(TEXT)
    assert stats["updated_count"] == 4 and spy.calls == []           # the direct path ran
    assert three(db)["ob_0001"][0] == 0.1
    lines = bump_lines(hook)
    assert any("owner flag helper failed (SystemExit)" in l for l in lines), lines


def test_fold2_a_flag_helper_keyboardinterrupt_propagates_it_is_not_swallowed(home, monkeypatch):
    make_store(home, flag=True)
    hook = load_hook(home, monkeypatch)
    monkeypatch.setattr(hook, "_import_owner_flags", _flag_helper_raising(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        hook.credit_mentions(TEXT)


def test_fold3_an_internal_client_error_on_the_first_attempt_is_indeterminate_and_not_retried(home, monkeypatch):
    hook, fake, db = with_fake(home, monkeypatch, [_unavail("internal")])
    before = all_rows(db)
    stats = hook.credit_mentions(TEXT)
    assert len(fake.calls) == 1                                     # internal is neither timeout nor protocol: no retry
    assert stats["updated_count"] == 0 and all_rows(db) == before
    assert bump_lines(hook) == [
        "bump_utility indeterminate: owner client error (internal); the request may have been sent"]


@pytest.mark.parametrize("reason,line", [
    ("down", "bump_utility failed: owner unavailable (reason=down)"),
    ("local", "bump_utility failed: owner unavailable (reason=local)"),
])
def test_fold3_down_and_local_keep_their_determinate_wording(home, monkeypatch, reason, line):
    hook, fake, _ = with_fake(home, monkeypatch, [_unavail(reason)])
    hook.credit_mentions(TEXT)
    assert len(fake.calls) == 1 and bump_lines(hook) == [line]


def test_fold3_internal_on_the_retry_stays_in_the_after_indeterminate_family(home, monkeypatch):
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch, [_unavail("timeout"), _unavail("internal")], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    hook.credit_mentions(TEXT)
    assert len(fake.calls) == 2
    assert bump_lines(hook) == [RETRY_LINE.format(code="unavailable_internal")]


def test_fold4_a_retry_is_skipped_when_less_than_the_minimum_budget_is_left(home, monkeypatch):
    """Attempt 1 times out after consuming 3.4 s of the 3.5 s budget: 0.1 s is left, below OWNER_RETRY_MIN_S (0.25 s). The
    retry is skipped (ONE ec.write call) and the outcome is the 'did not answer' indeterminate line. Mutation: guard `< 0`
    instead of `< OWNER_RETRY_MIN_S` -> a second call with a 0.1 s timeout -> red."""
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch, [_unavail("timeout")], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", 3.4)
    hook.credit_mentions(TEXT)
    assert hook.OWNER_WRITE_BUDGET_S - 3.4 < hook.OWNER_RETRY_MIN_S
    assert len(fake.calls) == 1 and fake.calls[0]["timeout"] == 3.4
    assert bump_lines(hook) == ["bump_utility indeterminate: owner did not answer (may have been applied once)"]


def test_fold4_a_retry_still_runs_when_exactly_the_minimum_budget_is_left(home, monkeypatch):
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch, [_unavail("timeout"), _ok()], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(ec, "OWNER_WRITE_CLIENT_TIMEOUT", hook.OWNER_WRITE_BUDGET_S - hook.OWNER_RETRY_MIN_S)
    hook.credit_mentions(TEXT)
    assert len(fake.calls) == 2 and fake.calls[1]["timeout"] == hook.OWNER_RETRY_MIN_S


# --- review fold 3: a bool is not a count -------------------------------------------------------------------------


def test_fold3_an_ok_whose_bumped_is_a_bool_is_an_unusable_acknowledgement_first_attempt(home, monkeypatch):
    hook, fake, db = with_fake(home, monkeypatch, [ec.Ok(result={"bumped": True, "restamp_error": None}, request_id="x")])
    stats = hook.credit_mentions(TEXT)
    assert len(fake.calls) == 1 and stats["updated_count"] == 0       # True must not be counted as 1
    (line,) = bump_lines(hook)
    assert line.startswith("bump_utility failed: owner returned an unusable acknowledgement")


def test_fold3_an_ok_whose_bumped_is_a_bool_after_an_indeterminate_first_attempt_is_indeterminate(home, monkeypatch):
    clock = [0.0]
    hook, fake, _ = with_fake(home, monkeypatch,
                              [_unavail("timeout"), ec.Ok(result={"bumped": True}, request_id="x")], clock=clock)
    monkeypatch.setattr(hook, "_monotonic", lambda: clock[0])
    hook.credit_mentions(TEXT)
    assert len(fake.calls) == 2
    assert bump_lines(hook) == [RETRY_LINE.format(code="unusable_answer")]
