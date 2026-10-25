"""Contract tests for the A6 contended arm running client PROCESSES, not threads (part of #3740).

Written BEFORE the change (contract-first). The finding: `measure()` ran the contended arm as two threads
of one process, so both paths shared one GIL; for the tripwire hook that hid the owner's real contended
cost (threads 1.10x, separate processes 1.42x, measured 2026-10-25). Real hook fires are processes.

The contract, for the function-level hooks (user-identity, stop, tripwire, surface); the session-start arm
(fresh processes already) is untouched:

  C1  `_contended_phase(fn, reset, n, clients, timeout_s)` forks `clients` child PROCESSES; none runs in the
      parent; each runs its timed loop in its main thread.
  C2  exactly `clients * n` samples, or RuntimeError naming the child (with its traceback tail when it has
      one) when a child exits non-zero, dies on a signal, or writes no result. Never a short list.
  C3  all children finish their untimed preparation before any starts timing (start barrier); they overlap.
  C4  `_TripwireFire` / `_SurfaceFire` get a process-unique tag via `rebind_for_process()`, called in the
      child before `prepare()`: STATE/LEDGER/PREVENTION_LEDGER paths, session id and probe dir differ.
  C5  fork guard: more than one thread in the parent, or an active store-open counting window -> RuntimeError
      before anything is forked.
  C6  per hook the phases run baseline, owner, owner, baseline; each phase is `clients` x ceil(n/2) fires;
      a path's row pools its two phases (n=300 -> 600 samples, as 2 threads x 300 before).
  C7  a child ends with os._exit (no parent atexit, never returns into measure()), writes nothing to fd 1;
      results go through a per-phase temp dir the parent creates and removes.
  C8  the parent waits at most `timeout_s`; on expiry it SIGKILLs and reaps every child and raises.
  C9  the report text says processes, and the result carries `"contended_clients": "process"`.

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import atexit
import gc
import importlib.util
import json
import math
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

from _a6_isolated import a6_fork_default_deny, isolated  # noqa: F401  (autouse: fork default-deny, #4163)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_PATH = REPO_ROOT / "tools" / "engramd_a6_latency.py"

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="the contended arm forks")


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("engramd_a6_latency_procs_3740", TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _lines(d: Path):
    out = []
    for p in sorted(d.glob("*.jsonl")):
        out += [json.loads(x) for x in p.read_text().splitlines() if x]
    return out


class Recorder:
    """A fire that logs what each CHILD did to a file per pid (the parent can read it back)."""

    def __init__(self, log: Path, sleep: float = 0.0, slow_prepare: float = 0.0) -> None:
        self.log, self.sleep, self.slow_prepare = log, sleep, slow_prepare

    def _w(self, **kw) -> None:
        with open(self.log / f"{os.getpid()}.jsonl", "a") as f:
            f.write(json.dumps(dict(kw, pid=os.getpid(), t=time.monotonic(),
                                    main=threading.current_thread() is threading.main_thread())) + "\n")

    def prepare(self) -> None:
        try:   # exactly one child (the first to create the marker) is slow
            os.close(os.open(self.log / "slow-marker", os.O_CREAT | os.O_EXCL))
            time.sleep(self.slow_prepare)
        except FileExistsError:
            pass
        self._w(ev="prepared")

    def reset(self) -> None:
        self._w(ev="reset")

    def __call__(self):
        self._w(ev="fire")
        if self.sleep:
            time.sleep(self.sleep)
        self._w(ev="fired")


# ---- C1 ------------------------------------------------------------------------------------------

@isolated
def test_c1_the_clients_are_distinct_child_processes_in_their_main_thread(tool, tmp_path):
    """Mutation: run the clients as threads of the parent (or one client in the parent) -> the pids
    collapse onto the parent's / each other's -> red."""
    rec = Recorder(tmp_path)
    samples = tool._contended_phase(rec, rec.reset, 4, 2)
    fires = [e for e in _lines(tmp_path) if e["ev"] == "fire"]
    pids = {e["pid"] for e in fires}
    assert len(pids) == 2 and os.getpid() not in pids
    assert all(e["main"] for e in fires)
    assert len(samples) == 8


# ---- C2 ------------------------------------------------------------------------------------------

@isolated
def test_c2_returns_exactly_clients_times_n_floats(tool, tmp_path):
    """Mutation: return fewer / pooled-with-something-else samples -> red."""
    rec = Recorder(tmp_path)
    for clients, n in ((2, 5), (3, 2)):
        out = tool._contended_phase(rec, None, n, clients)
        assert len(out) == clients * n and all(isinstance(x, float) and x >= 0 for x in out)


class _Boom(Recorder):
    """The child that gets the marker first raises on its third timed fire."""

    def __call__(self):
        super().__call__()
        n = sum(1 for e in _lines(self.log) if e["ev"] == "fire" and e["pid"] == os.getpid())
        if n == 4:   # 1 untimed + 3rd timed
            try:
                os.close(os.open(self.log / "boom-marker", os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                return
            raise ValueError("boom-marker-text")


@isolated
def test_c2_a_child_that_raises_fails_the_phase_naming_it_with_its_traceback(tool, tmp_path):
    """Mutation: silently drop the failed child's samples (pool the others) -> no RuntimeError -> red."""
    with pytest.raises(RuntimeError) as exc:
        tool._contended_phase(_Boom(tmp_path), None, 6, 2)
    msg = str(exc.value)
    assert "boom-marker-text" in msg and "ValueError" in msg
    assert "client" in msg and "pid" in msg


class _Killed(Recorder):
    def __call__(self):
        super().__call__()
        if sum(1 for e in _lines(self.log) if e["ev"] == "fire" and e["pid"] == os.getpid()) == 3:
            try:
                os.close(os.open(self.log / "kill-marker", os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                return
            os.kill(os.getpid(), signal.SIGKILL)


@isolated
def test_c2_a_child_killed_by_a_signal_fails_the_phase(tool, tmp_path):
    """Mutation: treat a signal death as 'no samples, carry on' -> red."""
    with pytest.raises(RuntimeError, match=r"(?i)signal|SIGKILL"):
        tool._contended_phase(_Killed(tmp_path), None, 6, 2)


class _SilentExit(Recorder):
    def __call__(self):
        super().__call__()
        if sum(1 for e in _lines(self.log) if e["ev"] == "fire" and e["pid"] == os.getpid()) == 3:
            try:
                os.close(os.open(self.log / "exit-marker", os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                return
            os._exit(0)   # exits 0, but owes its result file


@isolated
def test_c2_a_child_that_exits_zero_without_a_result_fails_the_phase(tool, tmp_path):
    """Mutation: judge a child by its exit code alone -> a clean-looking exit with no result passes -> red."""
    with pytest.raises(RuntimeError, match=r"(?i)no result"):
        tool._contended_phase(_SilentExit(tmp_path), None, 6, 2)


# ---- C3 ------------------------------------------------------------------------------------------

@isolated
def test_c3_children_prepare_before_any_times_and_then_overlap(tool, tmp_path):
    """One child prepares slowly (0.4 s). Barrier: no child's first timed fire precedes the slowest
    child's prepared stamp. Overlap: the children's timed windows intersect, in the fire-side stamps
    and in the spans the phase reports. Mutation: run the children one after another (waitpid each
    before forking the next) -> the second child prepares after the first finished timing and the
    windows are disjoint -> red."""
    rec = Recorder(tmp_path, sleep=0.02, slow_prepare=0.4)
    spans: list = []
    tool._contended_phase(rec, rec.reset, 5, 2, spans=spans)
    ev = _lines(tmp_path)
    pids = sorted({e["pid"] for e in ev})
    assert len(pids) == 2
    prepared = {e["pid"]: e["t"] for e in ev if e["ev"] == "prepared"}
    # timed fires = every fire after each child's first (untimed) one
    timed = {p: [e for e in ev if e["pid"] == p and e["ev"] in ("fire", "fired")][2:] for p in pids}
    first_timed = min(t[0]["t"] for t in timed.values())
    assert first_timed >= max(prepared.values())
    start = {p: timed[p][0]["t"] for p in pids}
    end = {p: timed[p][-1]["t"] for p in pids}
    assert max(start.values()) < min(end.values())
    assert len(spans) == 2 and max(s[0] for s in spans) < min(s[1] for s in spans)


# ---- C4 ------------------------------------------------------------------------------------------

def _fake_tripwire(tool, tmp_path, monkeypatch, log):
    home = tmp_path / "home"
    home.mkdir()
    noop = types.SimpleNamespace(set_target=lambda t: None, clear_target=lambda: None)
    shared = types.SimpleNamespace(home=home, hook_path=Path("hook.py"), exclusion=False,
                                   probe_root=tmp_path / "probes", stdin=noop, stdout=noop, stderr=noop,
                                   recall_reached=[], probe_seen=[])
    fire = tool._TripwireFire(shared, "owner", tmp_path / "config.json")

    def main():
        m = fire.prepare()
        with open(log / f"{os.getpid()}.json", "w") as f:
            json.dump({"pid": os.getpid(), "paths": [str(m.STATE_PATH), str(m.LEDGER_PATH),
                                                     str(m.PREVENTION_LEDGER_PATH)],
                       "sid": fire._local.sid, "probe": str(fire._local.probe)}, f)

    monkeypatch.setattr(tool, "load_hook", lambda home, path, name: types.SimpleNamespace(main=main))
    return fire


def _identity_of(log: Path):
    return [json.loads(p.read_text()) for p in sorted(log.glob("*.json"))]


@isolated
def test_c4_tripwire_children_have_process_unique_state_session_and_probe(tool, tmp_path, monkeypatch):
    """Mutation: build the child tag WITHOUT the pid (a constant per path) -> the children share
    STATE/LEDGER/PREVENTION paths, session id and probe dir -> red."""
    log = tmp_path / "log"
    log.mkdir()
    fire = _fake_tripwire(tool, tmp_path, monkeypatch, log)
    fire.prepare()   # the parent's own binding (the warm-up did this)
    parent = {"paths": [str(fire.prepare().STATE_PATH), str(fire.prepare().LEDGER_PATH),
                        str(fire.prepare().PREVENTION_LEDGER_PATH)], "sid": fire._local.sid,
              "probe": str(fire._local.probe)}
    tool._contended_phase(fire, fire.reset, 2, 2)
    ids = _identity_of(log)
    assert len(ids) == 2
    for key in ("sid", "probe"):
        assert len({i[key] for i in ids} | {parent[key]}) == 3
    for slot in range(3):
        assert len({i["paths"][slot] for i in ids} | {parent["paths"][slot]}) == 3
    assert all(str(i["pid"]) in i["sid"] for i in ids)


@isolated
def test_c4_surface_children_have_process_unique_sessions(tool, tmp_path, monkeypatch):
    """Mutation: build the child tag WITHOUT the pid -> the children share one session id -> red."""
    log = tmp_path / "log"
    log.mkdir()
    noop = types.SimpleNamespace(set_target=lambda t: None, clear_target=lambda: None)
    shared = types.SimpleNamespace(home=tmp_path, hook_path=Path("hook.py"), stdin=noop, stdout=noop, stderr=noop,
                                   recall_reached=[], clear_fire_state=lambda: None)
    fire = tool._SurfaceFire(shared, "owner", tmp_path / "config.json")

    def main():
        fire.prepare()
        sid = json.loads(fire._local.payload)["session_id"]
        (log / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "sid": sid}))

    monkeypatch.setattr(tool, "load_hook", lambda home, path, name: types.SimpleNamespace(
        main=main, query_daemon=None, CONFIG_PATH=None))
    monkeypatch.setattr(tool, "_require_surface_flag", lambda mod, path: None)
    fire.prepare()
    parent_sid = json.loads(fire._local.payload)["session_id"]
    tool._contended_phase(fire, fire.reset, 2, 2)
    ids = _identity_of(log)
    assert len(ids) == 2 and len({i["sid"] for i in ids} | {parent_sid}) == 3
    assert all(str(i["pid"]) in i["sid"] for i in ids)


def test_c4_the_classes_carry_the_rebind_method_and_the_plain_binders_do_not(tool):
    """Mutation: drop the method from either class -> red. The plain binders (stop, user-identity) write
    no per-fire files (read-only SELECTs / owner reads), so they carry none."""
    assert callable(getattr(tool._TripwireFire, "rebind_for_process", None))
    assert callable(getattr(tool._SurfaceFire, "rebind_for_process", None))


# ---- C5 ------------------------------------------------------------------------------------------

def test_c5_a_multithreaded_parent_is_refused_before_anything_forks(tool, tmp_path):
    """Mutation: drop the guard -> a child is forked from a multi-threaded parent -> red."""
    stop = threading.Event()
    t = threading.Thread(target=stop.wait)
    t.start()
    try:
        rec = Recorder(tmp_path)
        with pytest.raises(RuntimeError, match=r"(?i)thread"):
            tool._contended_phase(rec, None, 2, 2)
        assert _lines(tmp_path) == []
    finally:
        stop.set()
        t.join()


@isolated
def test_c5_an_active_store_open_counting_window_is_refused_before_anything_forks(tool, tmp_path):
    """Mutation: drop the window check -> children's opens would escape the count -> red."""
    db = tmp_path / "knowledge.db"
    sqlite3.connect(str(db)).close()
    rec = Recorder(tmp_path)
    with tool._counting_store_opens(db):
        with pytest.raises(RuntimeError, match=r"(?i)counting|window"):
            tool._contended_phase(rec, None, 2, 2)
    assert _lines(tmp_path) == []


# ---- C6 ------------------------------------------------------------------------------------------

def test_c6_phases_alternate_baseline_owner_owner_baseline_and_pool_per_path(tool, monkeypatch):
    """n=300 -> 150 fires per client per phase -> 600 samples per path; n=5 -> 3 per client -> 12.
    Mutation: run baseline, baseline, owner, owner (or one phase per path) -> red."""
    calls = []

    def fake_phase(fn, reset, n, clients, timeout_s=None, spans=None):
        calls.append((fn.__name__, reset.__name__, n, clients))
        if spans is not None:
            spans.extend([(0.0, 1.0)] * clients)
        return [1.0] * (clients * n)

    def baseline():
        pass

    def owner():
        pass

    def rb():
        pass

    def ro():
        pass

    monkeypatch.setattr(tool, "_contended_phase", fake_phase)
    for n in (300, 5):
        calls.clear()
        rows, _overlap = tool._contended_rows(baseline, owner, {id(baseline): rb, id(owner): ro}, n)
        assert [c[0] for c in calls] == ["baseline", "owner", "owner", "baseline"]
        assert [c[1] for c in calls] == ["rb", "ro", "ro", "rb"]
        assert all(c[2] == math.ceil(n / 2) and c[3] == 2 for c in calls)
        assert [(r["arm"], r["path"], r["n"]) for r in rows] == [
            ("contended", "baseline", 4 * math.ceil(n / 2)), ("contended", "owner", 4 * math.ceil(n / 2))]
    assert rows[0]["n"] == 12


# ---- C7 ------------------------------------------------------------------------------------------

class _Noisy(Recorder):
    def __call__(self):
        print("stray stdout line")
        sys.stdout.flush()
        os.write(1, b"raw fd 1 bytes\n")
        super().__call__()


@isolated
def test_c7_children_never_write_to_fd_1(tool, tmp_path, capfd):
    """measure_in_layout parses the layout child's LAST stdout line as JSON. Mutation: let the child keep
    the inherited fd 1 -> stray lines reach the parent's stdout -> red."""
    tool._contended_phase(_Noisy(tmp_path), None, 2, 2)
    assert capfd.readouterr().out == ""


@isolated
def test_c7_children_skip_the_parents_atexit_and_the_temp_dir_is_removed(tool, tmp_path, monkeypatch):
    """Mutation: end the child with sys.exit / return (running the parent's atexit handlers) -> the marker
    appears; leave the per-phase temp dir behind (success or failure) -> red."""
    marker = tmp_path / "atexit-ran"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    parent_pid = os.getpid()

    def handler():
        if os.getpid() != parent_pid:
            marker.write_text("child ran the parent's atexit")

    atexit.register(handler)
    try:
        tool._contended_phase(Recorder(tmp_path), None, 2, 2)
        with pytest.raises(RuntimeError):
            tool._contended_phase(_Boom(tmp_path), None, 6, 2)
    finally:
        atexit.unregister(handler)
    assert not marker.exists()
    assert list(scratch.iterdir()) == []


# ---- C8 ------------------------------------------------------------------------------------------

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@isolated
@pytest.mark.parametrize("where", ["timed", "prepare"])
def test_c8_a_hung_child_is_killed_and_reaped_after_timeout_s(tool, tmp_path, where):
    """Hang in a timed fire, or in the untimed preparation (before the barrier). Mutation: wait without a
    bound -> the test hangs / outlives the limit; kill without waitpid -> a zombie answers kill(pid, 0)
    -> red."""
    class Hang(Recorder):
        def prepare(self):
            super().prepare()
            if where == "prepare":
                time.sleep(60)

        def __call__(self):
            super().__call__()
            if where == "timed" and sum(1 for e in _lines(self.log)
                                        if e["ev"] == "fire" and e["pid"] == os.getpid()) == 2:
                time.sleep(60)

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match=r"(?i)timed out|timeout"):
        tool._contended_phase(Hang(tmp_path), None, 3, 2, timeout_s=1.5)
    assert time.monotonic() - t0 < 15
    pids = {e["pid"] for e in _lines(tmp_path)}
    assert len(pids) == 2 and not any(_alive(p) for p in pids)


# ---- C9 ------------------------------------------------------------------------------------------

def _result(hook, **extra):
    rows = [{"arm": a, "path": p, "n": 20, "p50_ms": 1.0 if p == "baseline" else 0.9, "p99_ms": 2.0,
             "mean_ms": 1.0, "min_ms": 0.5}
            for a in ("sequential", "contended", "cold-cache") for p in ("baseline", "owner")]
    base = {"rows": rows, "n": 20, "warmup": 5, "hook": hook, "answers_match": True, "answer_size": 10,
            "answer_count": 3, "people_count": 3, "node_count": 300, "db_bytes": 1000, "owner_pid": 1,
            "owner_cmdline": "engramd_owner", "journal_mode": "wal", "cold_cache_evicts": True,
            "env": {"platform": "p", "cpus": 2, "python": "3", "sqlite": "3"},
            "exclusion": False, "probe_dir_live": True, "recall_reached": True, "stdout_match": True,
            "opens_flag_on": 0, "opens_flag_off": 5, "open_violations": [], "remainder_opens_flag_on": 0,
            "layout": "installed", "layout_root": "/r", "parent_walk_calls": 0}
    base.update(extra)
    return base


@pytest.mark.parametrize("hook", ["user-identity", "stop", "tripwire", "surface"])
def test_c9_function_level_report_text_says_processes(tool, hook):
    """Mutation: leave the old 'threads share one GIL' / '2 threads x N' wording -> red."""
    table = tool.format_table(_result(hook, contended_clients="process"))
    assert "thread" not in table.lower()
    assert "GIL" not in table
    assert "2 client processes" in table


def test_c9_hook_notes_say_per_process_and_the_docstring_has_no_client_threads(tool):
    """Mutation: keep 'per thread' in the tripwire / surface notes or 'client threads' in the module
    docstring -> red."""
    for hook in ("tripwire", "surface"):
        table = tool.format_table(_result(hook, contended_clients="process"))
        assert "separate hook module per process" in table and "per thread" not in table
    assert "two client threads at once" not in tool.__doc__ and "affects both paths equally" not in tool.__doc__


def test_c9_measure_stamps_the_key_and_the_row_holds_the_exact_sample_count(tool, monkeypatch):
    """End to end against the in-process fake owner is covered in test_engramd_a6_latency.py (its threads
    are exempt from the fork guard); this drives measure() with the phase stubbed: the key is on the result and
    each contended row is the 4 x ceil(n/2) the phases returned. Mutation: omit the key, or fall back to
    the old thread loop for a function-level hook -> red."""
    seen = []

    def fake_phase(fn, reset, n, clients, timeout_s=None, spans=None):
        seen.append(n)
        if spans is not None:
            spans.extend([(0.0, 1.0)] * clients)
        return [1.0] * (clients * n)

    monkeypatch.setattr(tool, "_contended_phase", fake_phase)
    monkeypatch.setattr(tool, "launch_owner", lambda home: types.SimpleNamespace(
        proc=types.SimpleNamespace(pid=os.getpid(), poll=lambda: None), home=home, stop=lambda: None))
    monkeypatch.setattr(tool, "_fadvise_cold", lambda db: None)
    monkeypatch.setattr(tool, "_bind_user_identity", lambda home: (lambda: 1, lambda: 1, True, 1,
                                                                    {"people_match": True, "people_count": 1}))
    result = tool.measure(n=7, nodes=50, warmup=1)
    assert result["contended_clients"] == "process"
    assert seen == [4, 4, 4, 4]
    contended = {r["path"]: r["n"] for r in result["rows"] if r["arm"] == "contended"}
    assert contended == {"baseline": 16, "owner": 16}
    assert result["contended_overlap"] == {"baseline": 1.0, "owner": 1.0}


# ---- round 2 R1: the per-thread fork-guard exemption -----------------------------------------------

class _Parked:
    """A live helper thread, optionally exempting ITSELF from the fork guard first."""

    def __init__(self, tool, exempt: bool, name: str) -> None:
        self.stop = threading.Event()
        self.up = threading.Event()

        def run():
            if exempt:
                tool.exempt_this_thread_from_fork_guard()
            self.up.set()
            self.stop.wait()

        self.thread = threading.Thread(target=run, name=name)
        self.thread.start()
        assert self.up.wait(5)

    def close(self) -> None:
        self.stop.set()
        self.thread.join()


@isolated
def test_r1_an_exempted_live_thread_does_not_trip_the_guard(tool, tmp_path):
    """Mutation: ignore the exemption (count every live thread) -> the guard raises -> red."""
    t = _Parked(tool, True, "trusted-helper")
    try:
        tool._contended_fork_guard()
        # and a real phase forks fine with the trusted thread alive
        assert len(tool._contended_phase(Recorder(tmp_path), None, 2, 2)) == 4
    finally:
        t.close()


def test_r1_a_non_exempted_live_thread_still_trips_the_guard_and_is_named(tool):
    """Mutation: exempt every thread (guard off) -> no raise -> red."""
    t = _Parked(tool, False, "stranger-thread")
    try:
        with pytest.raises(RuntimeError, match=r"stranger-thread"):
            tool._contended_fork_guard()
    finally:
        t.close()


def test_r1_the_exemption_is_per_thread_not_process_wide(tool):
    """Exempting thread A must not let thread B pass. Mutation: make the exemption process-wide (a module
    flag instead of a per-thread registry) -> B passes -> red."""
    a = _Parked(tool, True, "exempt-a")
    b = _Parked(tool, False, "plain-b")
    try:
        with pytest.raises(RuntimeError) as exc:
            tool._contended_fork_guard()
        assert "plain-b" in str(exc.value) and "exempt-a" not in str(exc.value)
    finally:
        a.close()
        b.close()


def test_r1_the_exemption_dies_with_the_thread(tool):
    """A finished exempt thread leaves nothing a later thread could match (a registry keyed on reusable
    thread idents would let a new thread that reuses the ident through). Mutation: key the registry on
    `threading.get_ident()` -> an ident-reusing stranger passes -> red when the ident is reused."""
    for _ in range(20):
        a = _Parked(tool, True, "gone")
        a.close()
    t = _Parked(tool, False, "late-stranger")
    try:
        with pytest.raises(RuntimeError, match="late-stranger"):
            tool._contended_fork_guard()
    finally:
        t.close()


def test_r1_the_registry_holds_threads_weakly(tool):
    """The exemption registry must not keep a finished thread alive: exempt a thread, join it, drop every
    reference, collect, and the registry no longer holds it. Mutation: a plain `set` for the WeakSet keeps the
    Thread object (a strong reference) -> the registry still holds it -> red. (The other r1 tests cannot see
    this: a dead thread is not in threading.enumerate(), so it cannot trip the guard either way.)"""
    gc.collect()   # earlier tests' finished threads may still await collection; count from a settled registry
    before = len(tool._FORK_EXEMPT)
    t = _Parked(tool, True, "weak-probe")
    assert len(tool._FORK_EXEMPT) == before + 1          # registered while alive
    t.close()
    del t
    gc.collect()
    assert len(tool._FORK_EXEMPT) == before


# ---- round 2 R2: surface children keep their per-fire state apart ----------------------------------

def _surface_pair(tool, tmp_path, monkeypatch):
    """Two _SurfaceFire objects standing in for two children (their process tags set the way
    rebind_for_process would set them from two pids), on ONE shared home, with a fake hook module whose
    main() writes the state files the real hook writes per fire: the counter and the surfaced-recency
    ledger (through the module's own `_surface_ledger_path()`)."""
    home = tmp_path / "home"
    home.mkdir()
    noop = types.SimpleNamespace(set_target=lambda t: None, clear_target=lambda: None)
    shared = tool._SurfaceShared(home, Path("hook.py"))
    shared.stdin = shared.stdout = shared.stderr = noop
    (home / "knowledge.db").write_text("store")
    (home / "config.json").write_text("{}")
    shared.snapshot()

    def fake_load(h, path, name):
        mod = types.SimpleNamespace(
            query_daemon=None, CONFIG_PATH=None, COUNTER_PATH=str(home / "prompt-counter.json"),
            WRITE_REMINDER_PATH=str(home / "write-reminder.json"),
            _surface_ledger_path=lambda: str(home / "surface-ledger.json"))

        def main():
            for p in (mod.COUNTER_PATH, mod.WRITE_REMINDER_PATH, mod._surface_ledger_path()):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(name)
        mod.main = main
        return mod

    monkeypatch.setattr(tool, "load_hook", fake_load)
    monkeypatch.setattr(tool, "_require_surface_flag", lambda mod, path: None)
    fires = []
    for tag in ("p111", "p222"):
        f = tool._SurfaceFire(shared, "owner", tmp_path / "config.json")
        f._process_tag = tag
        fires.append(f)
    return home, shared, fires


def _state_files(fire):
    mod = fire.prepare()
    return [Path(mod.COUNTER_PATH), Path(mod.WRITE_REMINDER_PATH), Path(mod._surface_ledger_path())]


def test_r2_two_surface_children_have_disjoint_state_files(tool, tmp_path, monkeypatch):
    """Mutation: leave the hook's state paths pointing at the shared home -> the children share them -> red."""
    home, shared, (a, b) = _surface_pair(tool, tmp_path, monkeypatch)
    pa, pb = _state_files(a), _state_files(b)
    assert set(map(str, pa)).isdisjoint(map(str, pb))
    for p in pa + pb:
        assert str(p).startswith(str(home))
    # each of the THREE redirected state files (counter, write-reminder marker, ledger) lands in its own child's
    # state dir, named by the child's tag. Mutation: delete the WRITE_REMINDER_PATH (or COUNTER_PATH, or ledger)
    # redirect -> that file stays at the home root, shared -> red.
    for fire, files, tag in ((a, pa, "p111"), (b, pb, "p222")):
        assert sorted(p.name for p in files) == ["prompt-counter.json", "surface-ledger.json", "write-reminder.json"]
        for p in files:
            assert p.parent == home / "a6-client-state" / f"owner-{tag}", p


def test_r2_one_surface_childs_reset_leaves_its_siblings_state_untouched(tool, tmp_path, monkeypatch):
    """Mutation: revert `reset` to the home-wide clear -> A's reset deletes B's files -> red."""
    home, shared, (a, b) = _surface_pair(tool, tmp_path, monkeypatch)
    a(), b()
    pa, pb = _state_files(a), _state_files(b)
    assert all(p.is_file() for p in pa + pb)
    a.reset()
    assert not any(p.exists() for p in pa), "a child's reset must clear its OWN state"
    assert all(p.is_file() for p in pb), "a child's reset must not touch a sibling's state"
    assert (home / "knowledge.db").is_file() and (home / "config.json").is_file()
    b.reset()
    assert not any(p.exists() for p in pb)


def test_r2_the_parents_own_fires_still_clear_home_wide(tool, tmp_path, monkeypatch):
    """The sequential / cold-cache arms (no process tag) keep the old behaviour. Mutation: scope the
    parent's reset to a state dir too -> a stray hook file survives -> red."""
    home, shared, (a, b) = _surface_pair(tool, tmp_path, monkeypatch)
    parent = tool._SurfaceFire(shared, "baseline", tmp_path / "config.json")
    (home / "stray-fire-file.json").write_text("x")
    parent.reset()
    assert not (home / "stray-fire-file.json").exists()
    assert (home / "knowledge.db").is_file()


# ---- round 3 R7: the pooled-count check is per path ------------------------------------------------

def test_r7_an_unbalanced_phase_order_pools_per_path_without_a_false_owes(tool, monkeypatch):
    """Order baseline, owner, owner: the baseline path owes 1 phase's worth, the owner path 2. Mutation:
    compare every path against the BASELINE path's phase count (the round-2 form) -> the owner path is
    reported as 'owes' although it holds exactly what it ran -> red."""
    monkeypatch.setattr(tool, "CONTENDED_PHASE_ORDER", ("baseline", "owner", "owner"))

    def fake_phase(fn, reset, n, clients, timeout_s=None, spans=None):
        spans.extend([(0.0, 1.0)] * clients)
        return [1.0] * (clients * n)

    monkeypatch.setattr(tool, "_contended_phase", fake_phase)

    def baseline():
        pass

    def owner():
        pass

    rows, overlap = tool._contended_rows(baseline, owner, {id(baseline): None, id(owner): None}, 6)
    assert [(r["path"], r["n"]) for r in rows] == [("baseline", 6), ("owner", 12)]
    assert overlap == {"baseline": 1.0, "owner": 1.0}


def test_r7_a_short_pool_still_raises_owes(tool, monkeypatch):
    """The check stays: a phase that returns a short list (not possible through _contended_phase, but the
    pooled count is its own last line) is a loud error. Mutation: drop the check -> red."""
    monkeypatch.setattr(tool, "_contended_phase",
                        lambda fn, reset, n, clients, timeout_s=None, spans=None: spans.extend([(0.0, 1.0)] * clients) or [1.0])

    def baseline():
        pass

    def owner():
        pass

    with pytest.raises(RuntimeError, match="owes"):
        tool._contended_rows(baseline, owner, {id(baseline): None, id(owner): None}, 6)


# ---- round 3 R8: the module docstring bounds its claim ----------------------------------------------

def test_r8_the_docstring_scopes_the_missing_key_to_function_level_hooks(tool):
    """Session-start results carry no `contended_clients` key either and were never threaded. Mutation: revert
    to 'results without a `contended_clients` key are from that arm' -> red."""
    doc = " ".join(tool.__doc__.split())
    assert "function-level hook results without a `contended_clients` key" in doc


# ---- round 2 R3 / R5: report notes ------------------------------------------------------------------

def test_r3_r5_notes_state_the_shared_tripwire_files_and_the_recall_flag_scope(tool):
    """Mutation: drop either sentence -> red."""
    trip = tool.format_table(_result("tripwire", contended_clients="process"))
    assert "shared across clients on purpose" in trip
    for hook in ("tripwire", "surface"):
        table = tool.format_table(_result(hook, contended_clients="process"))
        assert "recall/probe sanity flags cover the warm-up, sequential and cold-cache fires only." in table


# ---- round 2 R4: overlap is reported, never gated ---------------------------------------------------

def test_r4_spans_overlap_means_every_pair_overlaps(tool):
    """Mutation: compare only the first two spans, or let touching spans count -> red."""
    assert tool._spans_overlap([(0.0, 2.0), (1.0, 3.0)])
    assert tool._spans_overlap([(0.0, 5.0), (1.0, 3.0), (2.0, 4.0)])
    assert not tool._spans_overlap([(0.0, 1.0), (1.0, 2.0)])          # touching is not overlapping
    assert not tool._spans_overlap([(0.0, 2.0), (1.0, 3.0), (2.5, 4.0)])   # third misses the first
    assert not tool._spans_overlap([])


def test_r4_contended_rows_report_overlap_per_path_and_never_raise(tool, monkeypatch):
    """Baseline phases: overlap, disjoint. Owner phases: disjoint, overlap. -> 0.5 each, no exception.
    Mutation: raise on non-overlap, or drop the key -> red."""
    plan = iter([[(0.0, 2.0), (1.0, 3.0)],    # baseline phase 1: overlap
                 [(0.0, 1.0), (2.0, 3.0)],    # owner phase 1: disjoint
                 [(0.0, 2.0), (1.0, 3.0)],    # owner phase 2: overlap
                 [(0.0, 1.0), (2.0, 3.0)]])   # baseline phase 2: disjoint

    def fake_phase(fn, reset, n, clients, timeout_s=None, spans=None):
        spans.extend(next(plan))
        return [1.0] * (clients * n)

    monkeypatch.setattr(tool, "_contended_phase", fake_phase)

    def baseline():
        pass

    def owner():
        pass

    rows, overlap = tool._contended_rows(baseline, owner, {id(baseline): None, id(owner): None}, 6)
    assert overlap == {"baseline": 0.5, "owner": 0.5}
    assert [r["n"] for r in rows] == [12, 12]


def test_r4_the_table_prints_overlap_only_when_a_phase_did_not_overlap(tool):
    """Mutation: print nothing, or always print -> red."""
    ok = tool.format_table(_result("tripwire", contended_clients="process",
                                   contended_overlap={"baseline": 1.0, "owner": 1.0}))
    bad = tool.format_table(_result("tripwire", contended_clients="process",
                                    contended_overlap={"baseline": 1.0, "owner": 0.5}))
    assert "did not overlap" not in ok
    assert "did not overlap" in bad and "owner 0.50" in bad
    assert "did not overlap" not in tool.format_table(_result("tripwire"))   # an old result has no key


# ---- round 3 R9: the build is a per-seat axis, not a constant -----------------------------------------

def _sqlite_result(hook, build, **extra):
    r = _result(hook, contended_clients="process", **extra)
    r["env"] = dict(r["env"], sqlite=build)
    return r


@pytest.mark.parametrize("hook", ["user-identity", "stop", "tripwire", "surface", "session-start"])
def test_r9_every_table_names_its_own_build_and_the_per_seat_rule(tool, hook):
    """Mutation: hard-code a build, say 'production build', or drop the factual scope sentence from any hook's
    table -> red. R10 mutations: flip clause on every hook -> the four non-tripwire params red; flip clause
    never -> the tripwire param red."""
    for build in ("3.45.1", "3.99.1"):
        extra = {}
        if hook == "session-start":
            extra = dict(opens_flag_on=0, calibration_opens_on=0, opens_flag_off=1, calibration_opens_off=1,
                         open_violations=[], stdout_match=True, readers_reached_off=[], readers_reached_on=[])
        table = " ".join(tool.format_table(_sqlite_result(hook, build, **extra)).split())
        assert f"SQLite {build}" in table
        for other in {"3.45.1", "3.99.1", "3.53.0"} - {build}:
            assert other not in table
        assert "production build" not in table.lower() and "expected" not in table.lower().replace("expected_", "")
        assert "only for a seat whose venv links that build" in table
        # the flip rule is Kepler's ruling for ONE flag (R10): tripwire output only, naming that flag
        if hook == "tripwire":
            assert "For `engramd.lesson_tripwire_reads_via_owner`: a seat flips only when" in table
            assert "No fleet-wide flip" in table and "Kepler, #3740, 2026-10-25" in table
        else:
            assert "flips only" not in table and "Kepler" not in table
            assert "fleet-wide flip" not in table and "lesson_tripwire_reads_via_owner" not in table


@pytest.mark.parametrize("exclusion", [True, False])
def test_r9_the_tripwire_names_the_exclusion_setting_it_used(tool, exclusion):
    """The tripwire's verdict scope names the exclusion setting. Mutation: drop the setting from the scope
    sentence -> the OTHER setting's word is not the one printed -> red."""
    table = " ".join(tool.format_table(_sqlite_result("tripwire", "3.99.1", exclusion=exclusion)).split())
    word = "ON" if exclusion else "OFF"
    assert f"SQLite 3.99.1 with exclusion {word}" in table
    assert "runs that exclusion setting" in table and "its OWN exclusion setting" in table


def test_r9_a_non_tripwire_table_does_not_mention_an_exclusion_setting_in_the_scope(tool):
    """Mutation: print the exclusion clause for every hook -> red."""
    table = " ".join(tool.format_table(_sqlite_result("surface", "3.99.1")).split())
    assert "runs that exclusion setting" not in table


def test_r9_the_tool_source_names_no_fixed_build_and_no_production_build():
    """Mutation: put a build literal or the phrase back anywhere in the tool -> red."""
    import re
    src = TOOL_PATH.read_text()
    assert "SURFACE_EXPECTED_SQLITE" not in src
    assert "production build" not in src.lower()
    assert not re.search(r"(?i)sqlite[ _-]*(version|build)?\W{0,4}3\.\d+\.\d+|expected\W{0,3}3\.\d+\.\d+", src), \
        "a hard-coded SQLite build in the tool"


# ---- round 5 R11/R12: only the installed block decides; the flip clause quotes the written ruling ---------

_SS_EXTRA = dict(opens_flag_on=0, calibration_opens_on=0, opens_flag_off=1, calibration_opens_off=1,
                 open_violations=[], stdout_match=True, readers_reached_off=[], readers_reached_on=[])
_ALL_HOOKS = ["user-identity", "stop", "tripwire", "surface", "session-start"]


def _layout_result(hook, layout, build="3.99.1", **extra):
    return _sqlite_result(hook, build, layout=layout, **(_SS_EXTRA if hook == "session-start" else {}), **extra)


@pytest.mark.parametrize("hook", _ALL_HOOKS)
def test_r11_the_source_block_states_the_build_but_decides_for_no_seat(tool, hook):
    """A source-checkout cell is 'a development-time number, NOT production': it decides for no seat and carries
    no flip clause, even for the tripwire. Mutation: print 'decides' under source too -> red."""
    table = " ".join(tool.format_table(_layout_result(hook, "source")).split())
    assert "This run: SQLite 3.99.1" in table
    for banned in ("decides only for", "flips only", "Kepler", "fleet-wide"):
        assert banned not in table


@pytest.mark.parametrize("hook", _ALL_HOOKS)
def test_r11_the_installed_block_decides_only_for_a_seat_on_that_build(tool, hook):
    """Mutation: drop the clause from the installed block -> red."""
    table = " ".join(tool.format_table(_layout_result(hook, "installed")).split())
    assert "decides only for a seat whose venv links that build" in table


def test_r11_the_rendered_source_block_and_installed_block_differ_as_ruled(tool):
    """End to end through format_layouts (the per-layout rendering that prints the headings), tripwire."""
    out = tool.format_layouts([_layout_result("tripwire", "installed"), _layout_result("tripwire", "source")])
    inst, src = out.split("### SOURCE checkout")
    inst, src = " ".join(inst.split()), " ".join(src.split())
    assert "decides only for" in inst and "For `engramd.lesson_tripwire_reads_via_owner`" in inst
    for banned in ("decides only for", "flips only", "Kepler"):
        assert banned not in src
    assert "This run: SQLite 3.99.1 with exclusion OFF." in src


def test_r12_the_flip_clause_quotes_the_written_ruling_not_an_overlap_cell(tool):
    """Kepler's written clause (b): the process-level contended arm, measured on that build and reported beside
    the gate cell, production overlap frequency still unmeasured. Mutation: restore 'overlap cell' -> red."""
    rule = " ".join(tool.TRIPWIRE_FLIP_RULE.split())
    assert "reported beside" in rule and "unmeasured" in rule and "process-level contended arm" in rule
    assert "overlap cell" not in rule
    assert "No fleet-wide flip" in rule and "default stays OFF" in rule and "Kepler, #3740, 2026-10-25" in rule
    table = " ".join(tool.format_table(_layout_result("tripwire", "installed")).split())
    assert "reported beside" in table and "unmeasured" in table and "overlap cell" not in table


def test_r12_no_output_text_in_the_tool_says_overlap_cell():
    """The phrase is gone from the tool source entirely. Mutation: put it back anywhere -> red."""
    assert "overlap cell" not in TOOL_PATH.read_text()


def test_r12_contended_overlap_is_documented_as_not_the_production_overlap_frequency(tool):
    """Where the key is printed (the NOTE) and where it is documented (_contended_rows). Mutation: drop the
    clause from either -> red."""
    res = _layout_result("tripwire", "installed", contended_overlap={"baseline": 1.0, "owner": 0.5})
    note = " ".join(tool.format_table(res).split())
    assert "NOT the production overlap frequency" in note
    assert "NOT the production overlap frequency" in " ".join(tool._contended_rows.__doc__.split())
