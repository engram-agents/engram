"""Contract tests for engram-stop-hook.py's flagged read through the owner,
cells C0-C6 of U6a (#3664, part of #2652).

Written BEFORE the implementation (contract-first; project CLAUDE.md,
"Contract-first tests"). Contract: the U6a spec plus the design seat's owner-down
ruling pinned on #3664 (loud ONCE per stop chain: a `decision: block` when
`stop_hook_active` is not literally `true`, one stderr line when it is).

  C0  flag OFF is today's hook, byte for byte (stdout, nudge state file, telemetry);
      the client is never imported or called; the import is lazy.
  C1  differential harness AT THE READ: raw count, derived decision, full stdout,
      old path vs owner path on the same store; shown to FAIL by mutating the NEW side.
      Run against an in-process FAKE owner (every host) and a REAL one (Linux seat).
  C2  gate disabled (threshold <= 0): False with no owner call and no DB open.
  C3  owner unavailable / rejected / unusable / raising client / unimportable client,
      flag ON: loud once per stop chain, never False/0/"immature", never the store,
      never the nudge state.
  C4  the owner call fits inside the hook's 5 s registration (half of it).
  C5  the DEPLOYED (flattened) layout resolves both runtime modules.
  C6  the launcher's `any_owner_flag_on` covers this flag by the `*_via_owner` pattern.
  R6  a failed flag helper still reads OFF but is reported: class-only stderr line + the same
      line as Stop additionalContext, never a block (cells at the end of this file).

Every test docstring names the mutation it must turn red under.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from _owner_platform import requires_linux_owner
from _fake_owner import FakeOwner, registered_sql

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)
_HOOKS_SRC = REPO_ROOT / "src" / "engram" / "hooks" / "claude"
_HOOK_PATH = _HOOKS_SRC / "engram-stop-hook.py"

import engramd_client as ec  # noqa: E402
import engramd_owner as eo  # noqa: E402
import engramd_owner_flags as eof  # noqa: E402

FLAG_KEY = "stop_reads_via_owner"
COUNT_SQL = "SELECT count(*) FROM nodes WHERE is_current = 1"
THRESHOLD = 10
NUDGE_PREFIX = "[ENGRAM Write Check:"


# --- fixtures / helpers --------------------------------------------------------

def make_store(home: Path, current: int, stale: int = 3) -> Path:
    """knowledge.db with the REAL schema, `current` is_current=1 rows and `stale`
    is_current=0 rows (which must never count)."""
    import engram_core

    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        rows = [(f"ob_{i:04d}", "observation", f"claim {i}", "2026-01-01T00:00:00", "{}", 1)
                for i in range(current)]
        rows += [(f"ob_s{i:03d}", "observation", f"stale {i}", "2026-01-01T00:00:00", "{}", 0)
                 for i in range(stale)]
        conn.executemany(
            "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current) VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def set_config(home: Path, flag=..., threshold=THRESHOLD, raw=None) -> None:
    """flag=... means 'key absent'; flag=None deletes the whole config file."""
    cfg = home / "config.json"
    if raw is not None:
        cfg.write_text(raw)
        return
    if flag is None:
        cfg.unlink(missing_ok=True)
        return
    doc = {"cadence": {"write_nudge_node_threshold": threshold}}
    if flag is not ...:
        doc["engramd"] = {FLAG_KEY: flag}
    cfg.write_text(json.dumps(doc))


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="sho-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def load_hook(home: Path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("engram_stop_hook_3664", str(_HOOK_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Run:
    def __init__(self, stdout, stderr, exc, telemetry, state):
        self.stdout, self.stderr, self.exc, self.telemetry, self.state = stdout, stderr, exc, telemetry, state


def run_main(mod, transcript: Path, **payload_extra) -> Run:
    """Drive main() with a Stop payload. -> stdout/stderr/exception/telemetry/state-file."""
    state_file = Path(mod._NUDGE_STATE_FILE)
    telemetry = []
    payload = {"session_id": "s-3664", "transcript_path": str(transcript)}
    payload.update(payload_extra)
    out, err = io.StringIO(), io.StringIO()
    exc = None
    with patch.object(mod, "write_telemetry_record",
                      lambda manifest_id, event_type, fields=None: telemetry.append((manifest_id, event_type, fields))), \
            patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", out), patch("sys.stderr", err):
        try:
            mod.main()
        except SystemExit as e:
            exc = f"SystemExit({e.code})"
        except Exception as e:  # noqa: BLE001 -- a crash is part of the behaviour under test
            exc = f"{type(e).__name__}: {e}"
    return Run(out.getvalue(), err.getvalue(), exc, telemetry,
               state_file.read_text() if state_file.exists() else None)


@pytest.fixture
def transcript(home):
    t = home / "transcript.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 600}]}}) + "\n")
    return t


def fresh(mod):
    Path(mod._NUDGE_STATE_FILE).unlink(missing_ok=True)


def spy_store_opens(monkeypatch, mod):
    """Fail-record every attempt to open the store: sqlite3.connect on knowledge.db
    and the hook's own connect_regular_ro."""
    seen = []
    real_connect = sqlite3.connect

    def spy_connect(database, *a, **k):
        seen.append(("sqlite3.connect", str(database)))
        return real_connect(database, *a, **k)

    def spy_ro(path, *a, **k):
        seen.append(("connect_regular_ro", str(path)))
        raise AssertionError("the store was opened directly")

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    monkeypatch.setattr(mod, "connect_regular_ro", spy_ro)
    return seen


def store_opens(seen, home):
    return [s for s in seen if "knowledge.db" in s[1] or s[0] == "connect_regular_ro"]


@contextlib.contextmanager
def owner_of(kind: str, home: Path):
    if kind == "fake":
        with FakeOwner(home) as o:
            yield o
    else:
        o = eo.EngramdOwner(home, request_deadline=0.6)
        o.start(install_signal_handlers=False)
        try:
            yield o
        finally:
            o.stop()


OWNER_KINDS = [
    pytest.param("fake", id="fake-owner"),
    pytest.param("real", id="real-owner", marks=requires_linux_owner),
]


# --- the C1 harness ---------------------------------------------------------------

class _Cur:
    def __init__(self, cur, sink):
        self._cur, self._sink = cur, sink

    def fetchone(self):
        row = self._cur.fetchone()
        self._sink.append(row[0])
        return row


class _Conn:
    def __init__(self, conn, sink):
        self._conn, self._sink = conn, sink

    def execute(self, *a, **k):
        return _Cur(self._conn.execute(*a, **k), self._sink)

    def close(self):
        self._conn.close()


def old_raw_count(mod) -> int:
    """The count the OLD path really fetched (recorded off its own cursor)."""
    sink = []
    orig = mod.connect_regular_ro
    with patch.object(mod, "connect_regular_ro", lambda p, *a, **k: _Conn(orig(p, *a, **k), sink)):
        mod._maturity_muted()
    assert len(sink) == 1, sink
    return sink[0]


def harness(mod, home: Path, transcript: Path) -> dict:
    """Old path vs owner path on the SAME store -> {level: (old, new)} for the RAW count,
    the derived muted decision, and the hook's FULL stdout (+ state file)."""
    set_config(home, flag=False)
    raw_old = old_raw_count(mod)
    dec_old = mod._maturity_muted()
    fresh(mod)
    r_old = run_main(mod, transcript)
    set_config(home, flag=True)
    raw_new = mod._read_count_via_owner()
    dec_new = mod._maturity_muted()
    fresh(mod)
    r_new = run_main(mod, transcript)
    return {
        "raw": (raw_old, raw_new),
        "decision": (dec_old, dec_new),
        "stdout": ((r_old.stdout, r_old.state), (r_new.stdout, r_new.state)),
    }


def differing(levels: dict) -> set:
    return {k for k, (a, b) in levels.items() if a != b}


# (name, rows in store, expected muted decision, expected stdout non-empty)
FIXTURES = [
    ("below", THRESHOLD - 3, False, True),
    ("exactly-at", THRESHOLD, True, False),
    ("above", THRESHOLD + 3, True, False),
]


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("fx", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_c1_owner_and_direct_agree_on_raw_count_decision_and_stdout(fx, owner_kind, home, monkeypatch, transcript):
    """Three levels, three fixtures (count below / EXACTLY AT / above the threshold),
    old vs owner on the same store. Anti-vacuity: the raw count is the true number of
    is_current=1 rows (the stale rows must not count), the decision is the expected
    one, and the owner really served the request. Mutation: drop the is_current
    filter / owner count off by one / `>` for `>=` on the owner side -> red (see the
    controls below)."""
    _name, current, want_muted, want_nudge = fx
    make_store(home, current)
    mod = load_hook(home, monkeypatch)
    with owner_of(owner_kind, home) as owner:
        calls = []
        orig = ec.read
        with patch.object(ec, "read", lambda *a, **k: (calls.append(a), orig(*a, **k))[1]):
            levels = harness(mod, home, transcript)
    assert levels["raw"] == (current, current)
    assert levels["decision"] == (want_muted, want_muted)
    assert bool(levels["stdout"][0][0]) is want_nudge and bool(levels["stdout"][1][0]) is want_nudge
    assert differing(levels) == set(), levels
    assert len(calls) >= 2, "the owner was never asked: the harness would pass vacuously"


def _count_shift(delta):
    original = ec.read

    def wrapped(*a, **k):
        result = original(*a, **k)
        if isinstance(result, ec.Ok):
            return ec.Ok(result=[[result.result[0][0] + delta]], request_id=result.request_id)
        return result

    return wrapped


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("fixture_name,delta,decision_unchanged", [
    ("below", +1, True),   # T-3 -> T-2: still below -> decision AND stdout unchanged
    ("above", -1, True),   # T+3 -> T+2: still above -> decision AND stdout unchanged
    ("exactly-at", -1, False),  # T -> T-1: flips
    ("exactly-at", +1, True),   # T -> T+1: still muted
])
def test_c1_control_mutant_1_owner_count_off_by_one_turns_the_harness_red(
        fixture_name, delta, decision_unchanged, owner_kind, home, monkeypatch, transcript):
    """REQUIRED mutant 1 (the NEW side's count off by +1 / -1). The RAW level must go
    red every time. On the `below` fixture with +1 and on the `above` fixture with -1
    the boolean decision AND the hook's stdout stay IDENTICAL -- that is the case a
    boolean-only (or stdout-only) harness misses, and only the raw-count level
    catches. The OLD side is untouched. Mutation of the harness itself (compare only
    decisions/stdout) -> the `below +1` / `above -1` cells go green-instead-of-red."""
    current = dict((f[0], f[1]) for f in FIXTURES)[fixture_name]
    make_store(home, current)
    mod = load_hook(home, monkeypatch)
    with owner_of(owner_kind, home):
        clean = harness(mod, home, transcript)
        assert differing(clean) == set()
        with patch.object(ec, "read", _count_shift(delta)):
            levels = harness(mod, home, transcript)
    assert levels["raw"][0] == clean["raw"][0], "the OLD side must be untouched by a NEW-side mutation"
    assert "raw" in differing(levels), levels
    if decision_unchanged:
        assert "decision" not in differing(levels) and "stdout" not in differing(levels), levels
    else:
        assert "decision" in differing(levels), levels


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
def test_c1_control_mutant_2_gt_for_ge_at_the_threshold_turns_the_harness_red(owner_kind, home, monkeypatch, transcript):
    """REQUIRED mutant 2 (`>` for `>=` on the NEW side): only the EXACTLY-AT fixture
    can see it. Below and above stay green. Raw counts agree (the read is fine); the
    decision and the stdout differ. Mutation of the harness (drop the at-threshold
    fixture) -> this goes green-instead-of-red."""
    seen = {}
    for name, current, _m, _n in FIXTURES:
        sub = home / name
        sub.mkdir()
        make_store(sub, current)
        mod = load_hook(sub, monkeypatch)
        tr = sub / "t.jsonl"
        tr.write_text(transcript.read_text())
        with owner_of(owner_kind, sub):
            with patch.object(mod, "_maturity_muted_via_owner",
                              lambda threshold, _m=mod: _m._read_count_via_owner() > threshold):
                seen[name] = differing(harness(mod, sub, tr))
    assert seen["exactly-at"] >= {"decision", "stdout"} and "raw" not in seen["exactly-at"], seen
    assert seen["below"] == set() and seen["above"] == set(), seen


@requires_linux_owner
def test_c1_real_owner_flag_on_sends_exactly_the_registered_template(home, monkeypatch, transcript):
    """Live cell (Linux seat). Flag ON must send the hook's own SQL constant, verbatim,
    as method `read`, which is also an entry of the committed registry. Mutation: a
    hand-copied literal that drifts from the template list -> unregistered_template ->
    red."""
    make_store(home, THRESHOLD + 2)
    mod = load_hook(home, monkeypatch)
    set_config(home, flag=True)
    calls = []
    orig = ec.read
    with owner_of("real", home):
        with patch.object(ec, "read", lambda m, p, **k: (calls.append((m, p, k)), orig(m, p, **k))[1]):
            run = run_main(mod, transcript)
    assert [c[0] for c in calls] == ["read"]
    assert calls[0][1] == {"sql": COUNT_SQL, "args": []} and COUNT_SQL in registered_sql()
    assert run.stdout == ""


def _strip_report(stdout: str):
    """-> (stdout without the failed-flag-helper report, the report line or None). The
    remainder is re-serialised in key order, so it is byte-equal to a report-free run."""
    if not stdout.strip():
        return stdout, None
    obj = json.loads(stdout)
    hso = obj.pop("hookSpecificOutput", None)
    return (json.dumps(obj) + "\n" if obj else ""), (hso or {}).get("additionalContext")


# --- C0: flag OFF is today's hook ----------------------------------------------------

def _flag_off_states():
    return [
        ("no-config-file", dict(flag=None)),
        ("config-without-engramd", dict(raw=json.dumps({"cadence": {"write_nudge_node_threshold": THRESHOLD}}))),
        ("engramd-without-key", dict(raw=json.dumps({"engramd": {}, "cadence": {"write_nudge_node_threshold": THRESHOLD}}))),
        ("false", dict(flag=False)),
        ("string-true", dict(flag="true")),
        ("one", dict(flag=1)),
        ("null", dict(raw=json.dumps({"engramd": {FLAG_KEY: None}, "cadence": {"write_nudge_node_threshold": THRESHOLD}}))),
        ("malformed-json", dict(raw="{nope")),
        ("helper-module-missing", dict(flag=True)),
    ]


@pytest.mark.parametrize("state", [s[0] for s in _flag_off_states()])
@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature", "mature"])
def test_c0_flag_off_is_todays_hook_and_never_touches_the_client(state, current, home, monkeypatch, transcript):
    """GUARD (green on the base too: it pins what must not change). Every OFF
    state -- including a flag `true` whose helper module is unimportable -- gives the
    pre-change output: immature -> the exact nudge JSON, the nudge state file written
    for this transcript, telemetry `reached` then `hook.fire`; mature -> empty stdout, no
    state file. `engramd_client` is poisoned (any import raises) and its seam is
    trapped. Mutation: branch on anything but the literal `true`, import the client
    on the OFF path, or read the flag before the threshold-disabled return -> red.
    The 'null' state is the one where config says `null` (not false). R6 change: the
    `helper-module-missing` state is a failed flag helper, so it is now REPORTED (not
    silent): its outcome equals the flag-OFF outcome once `hookSpecificOutput` is stripped,
    and stderr is exactly the report line; every other state is asserted unchanged
    (no report, nothing on stderr about owner)."""
    make_store(home, current)
    set_config(home, **dict(_flag_off_states())[state])
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    mod = load_hook(home, monkeypatch)
    for name in ("_import_engramd_client", "_read_count_via_owner"):
        monkeypatch.setattr(mod, name, lambda *a, **k: pytest.fail("owner seam used with the flag OFF"), raising=False)
    missing_helper = state == "helper-module-missing"
    if missing_helper:
        monkeypatch.setattr(mod, "_REPORT_TO_AGENT", True)

        def missing():
            raise ImportError("engramd_owner_flags.py not found")
        monkeypatch.setattr(mod, "_import_owner_flags", missing, raising=False)
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    # R6: a missing helper is VISIBLE (non-blocking report); the outcome is judged with that
    # report stripped, the report itself is pinned exactly.
    stdout, report = _strip_report(run.stdout)
    # no readable config -> the hook's default threshold (300) applies
    effective = 300 if state in ("no-config-file", "malformed-json") else THRESHOLD
    if current < effective:
        assert stdout == json.dumps({"decision": "block", "reason": json.loads(stdout)["reason"]}) + "\n"
        assert json.loads(stdout)["reason"].startswith(NUDGE_PREFIX)
        assert run.state is not None and str(transcript) in run.state
        assert [t[1] for t in run.telemetry] == ["engram.telemetry.reached", "engram.hook.fire"]
    else:
        assert stdout == "" and run.state is None
        assert [t[1] for t in run.telemetry] == ["engram.telemetry.reached"]
    if missing_helper:
        assert report == _line("ImportError") and run.stderr == _line("ImportError") + "\n"
    else:
        # (the emitter may complain about a malformed config on stderr today; the hook adds nothing)
        assert report is None
        assert "engramd" not in run.stderr.lower() and "owner" not in run.stderr.lower()


def test_c0_flag_off_with_stop_hook_active_true_still_nudges(home, monkeypatch, transcript):
    """Ruling item 4: the `stop_hook_active` guard covers the owner-down path ONLY.
    Flag OFF + `stop_hook_active: true` behaves exactly as today (the nudge still
    emits). GUARD, green on the base. Mutation: apply the guard to the OFF path
    -> red."""
    make_store(home, THRESHOLD - 3)
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch)
    run = run_main(mod, transcript, stop_hook_active=True)
    assert json.loads(run.stdout)["reason"].startswith(NUDGE_PREFIX)
    assert run.state is not None


def test_c0_the_client_and_the_flag_helper_are_imported_lazily(home, monkeypatch):
    """Module top level has no side effects: no top-level import of `engramd_client`
    or `engramd_owner_flags` (AST), and neither name is bound on the module after a
    flag-OFF run. Mutation: `import engramd_client` / `import engramd_owner_flags` at
    module scope -> red. (On the base this passes vacuously: it is a guard.)"""
    tree = ast.parse(_HOOK_PATH.read_text(encoding="utf-8"))
    top = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            top |= {a.name for a in stmt.names}
        elif isinstance(stmt, ast.ImportFrom):
            top.add(stmt.module)
    assert not ({"engramd_client", "engramd_owner_flags"} & top), top
    make_store(home, THRESHOLD - 3)
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch)
    assert not hasattr(mod, "engramd_client") and not hasattr(mod, "engramd_owner_flags")


def test_c0_the_flag_is_read_only_through_flag_on_with_this_hooks_key_and_config_path(home, monkeypatch, transcript):
    """Contract: read ONLY via `engramd_owner_flags.flag_on("stop_reads_via_owner",
    CONFIG_PATH)`. A recording stand-in for `flag_on` sees exactly that key and the
    $ENGRAM_HOME/config.json path, and its answer decides the path. Mutation: a
    per-hook rule that reads config itself, or another key -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=False)  # the file says OFF; only the helper's answer may turn it ON
    mod = load_hook(home, monkeypatch)
    seen = []
    monkeypatch.setattr(eof, "flag_on", lambda key, path: (seen.append((key, str(path))), True)[1])
    with FakeOwner(home):
        run_main(mod, transcript)
    assert seen and set(seen) == {(FLAG_KEY, str(home / "config.json"))}, seen


# --- C2: gate disabled ----------------------------------------------------------------

@pytest.mark.parametrize("threshold", [0, -5])
def test_c2_gate_disabled_returns_false_with_no_owner_call_and_no_db_open(threshold, home, monkeypatch):
    """Flag ON, threshold <= 0: `_maturity_muted()` is False and neither the client
    nor the store is touched (the old path returns before the DB). GUARD, green on the
    base. Mutation: read the owner (or the DB) before the threshold check -> red."""
    db = make_store(home, THRESHOLD + 3)
    set_config(home, flag=True, threshold=threshold)
    mod = load_hook(home, monkeypatch)
    seen = spy_store_opens(monkeypatch, mod)
    monkeypatch.setattr(ec, "read", lambda *a, **k: pytest.fail("owner called with the gate disabled"))
    monkeypatch.setattr(mod, "_import_engramd_client", lambda *a, **k: pytest.fail("client imported"), raising=False)
    assert mod._maturity_muted() is False
    assert store_opens(seen, home) == [], seen


# --- C3: owner unavailable / rejected / unusable / raising / unimportable ---------------

def _raising(exc):
    def boom(*a, **k):
        raise exc
    return boom


UNUSABLE = [
    ("garbage-string", "garbage"),
    ("empty-rows", []),
    ("two-rows", [[1], [2]]),
    ("two-cells", [[1, 2]]),
    ("negative", [[-1]]),
    ("bool-cell", [[True]]),
    ("string-cell", [["5"]]),
    ("null-cell", [[None]]),
]

KINDS = (["unavailable", "rejected", "raising", "unimportable"] + [f"unusable-{n}" for n, _ in UNUSABLE])


def arrange_failure(kind, mod, home, monkeypatch):
    """Wire the owner-side failure for `kind`; return the expected-details dict."""
    info = {"kind": kind}
    if kind == "unavailable":
        info["expected"] = ec.read("read", {"sql": "x", "args": []}, timeout=mod.OWNER_READ_TIMEOUT_S,
                                   socket_path=home / "run" / "engramd.sock")
        assert isinstance(info["expected"], ec.Unavailable)
    elif kind == "rejected":
        rej = ec.Rejected(reason="unregistered_template", detail="the statement is not registered",
                          remedy="register the template and restart the owner")
        info["rejected"] = rej
        monkeypatch.setattr(ec, "read", lambda *a, **k: rej)
    elif kind == "raising":
        monkeypatch.setattr(ec, "read", _raising(ValueError("Exceeds the limit (4300 digits)")))
    elif kind == "unimportable":
        empty = home / "empty-runtime"
        empty.mkdir()
        monkeypatch.setenv("ENGRAM_RUNTIME_DIR", str(empty))
        monkeypatch.setitem(sys.modules, "engramd_client", None)
        info["empty"] = empty
    else:
        payload = dict(UNUSABLE)[kind[len("unusable-"):]]
        monkeypatch.setattr(ec, "read", lambda *a, **k: ec.Ok(result=payload))
    return info


def assert_loud_block(reason: str, info: dict, mod, home: Path) -> None:
    kind = info["kind"]
    low = reason.lower()
    assert "zero" in low, "the block must say this is not a count of zero"
    a0, h0 = reason.index("Agent steps"), reason.index("Human steps")
    assert a0 < h0, "agent steps and human steps are two separate labelled parts, agent first"
    if kind == "rejected":
        rej = info["rejected"]
        assert "refused" in low and rej.reason in reason and rej.remedy in reason
        assert "not retryable" in low
    else:
        assert "unavailable" in low
    if kind == "unavailable":
        exp = info["expected"]
        assert str(home / "run" / "engramd.sock") in reason
        assert str(mod.OWNER_READ_TIMEOUT_S) in reason
        for step in exp.agent_steps:
            assert a0 < reason.index(step) < h0, step
        for step in exp.human_steps:
            assert reason.index(step) > h0, step
    if kind == "raising":
        assert "ValueError" in reason
    if kind == "unimportable":
        assert "engramd_client" in reason and str(info["empty"]) in reason
    if kind.startswith("unusable-"):
        assert "unusable" in low


PAYLOADS = [
    pytest.param({}, id="absent"),
    pytest.param({"stop_hook_active": False}, id="false"),
    pytest.param({"stop_hook_active": "true"}, id="string-true"),
    pytest.param({"stop_hook_active": 1}, id="one"),
    pytest.param({"stop_hook_active": None}, id="null"),
]


@pytest.mark.parametrize("extra", PAYLOADS)
@pytest.mark.parametrize("kind", KINDS)
def test_c3_owner_failure_with_stop_hook_active_not_true_is_one_loud_block_and_no_state(
        kind, extra, home, monkeypatch, transcript):
    """Flag ON, `stop_hook_active` anything but the literal JSON `true`: exactly ONE
    stdout line, a `{"decision":"block","reason":...}` carrying (a) UNAVAILABLE (or
    REFUSED + 'not retryable' for a rejection), (b) 'not a count of zero', (c) the
    socket path and the bound (unavailable), (d) Agent steps and Human steps as two
    separate labelled parts. And: the store is never opened (neither `sqlite3.connect`
    nor `connect_regular_ro`), `_update_state`/`_should_suppress` are never called, the
    nudge state file is not created, no `hook.fire` telemetry, exit 0.
    Mutations: collapse the failure to `False` in `_maturity_muted` (nudge instead of
    the block) / fall back to the DB / advance the nudge state on this path / treat
    '"true"' or 1 as true -> red."""
    make_store(home, THRESHOLD + 3)  # a MATURE store: a fallback or a manufactured False is distinguishable
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    info = arrange_failure(kind, mod, home, monkeypatch)
    seen = spy_store_opens(monkeypatch, mod)
    monkeypatch.setattr(mod, "_update_state", lambda *a, **k: pytest.fail("nudge state advanced"))
    monkeypatch.setattr(mod, "_should_suppress", lambda *a, **k: pytest.fail("idle machinery consulted"))
    run = run_main(mod, transcript, **extra)
    assert run.exc is None, run.exc
    assert store_opens(seen, home) == [], seen
    assert run.state is None
    assert [t[1] for t in run.telemetry] == ["engram.telemetry.reached"]
    lines = [ln for ln in run.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, run.stdout
    out = json.loads(lines[0])
    assert set(out) == {"decision", "reason"} and out["decision"] == "block"
    assert not out["reason"].startswith(NUDGE_PREFIX)
    assert_loud_block(out["reason"], info, mod, home)


@pytest.mark.parametrize("kind", KINDS)
def test_c3_owner_failure_with_stop_hook_active_true_is_silent_but_for_one_stderr_line(
        kind, home, monkeypatch, transcript):
    """The harness re-fires the Stop hook after a block; the chain was already told.
    `stop_hook_active` exactly `true`: NO stdout (no block, no nudge), ONE stderr line
    naming the condition; still no store open, no nudge state, no idle machinery,
    exit 0. Mutation: drop the `stop_hook_active` guard (a second block -> a loop) ->
    red; emit nothing on stderr -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    info = arrange_failure(kind, mod, home, monkeypatch)
    seen = spy_store_opens(monkeypatch, mod)
    monkeypatch.setattr(mod, "_update_state", lambda *a, **k: pytest.fail("nudge state advanced"))
    monkeypatch.setattr(mod, "_should_suppress", lambda *a, **k: pytest.fail("idle machinery consulted"))
    run = run_main(mod, transcript, stop_hook_active=True)
    assert run.exc is None, run.exc
    assert run.stdout == ""
    err_lines = [ln for ln in run.stderr.splitlines() if ln.strip()]
    assert len(err_lines) == 1, run.stderr
    assert "engramd" in err_lines[0].lower() or "owner" in err_lines[0].lower()
    assert store_opens(seen, home) == [], seen
    assert run.state is None
    assert [t[1] for t in run.telemetry] == ["engram.telemetry.reached"]


def test_c3_loop_two_consecutive_stops_with_the_owner_down_give_exactly_one_block(home, monkeypatch, transcript):
    """LOOP TEST. Stop #1 (no `stop_hook_active`) -> a block; the harness continues the
    agent and fires Stop #2 with `stop_hook_active: true` -> no block. Total blocks
    across the two invocations: exactly 1 (and exactly one stderr line). Mutation: drop
    the guard -> 2 blocks -> red; drop the block on the first -> 0 -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    first = run_main(mod, transcript)
    second = run_main(mod, transcript, stop_hook_active=True)
    blocks = [ln for r in (first, second) for ln in r.stdout.splitlines()
              if ln.strip() and json.loads(ln).get("decision") == "block"]
    assert len(blocks) == 1, (first.stdout, second.stdout)
    assert first.stdout.strip() and second.stdout == ""
    assert len([ln for ln in second.stderr.splitlines() if ln.strip()]) == 1


def test_c3_owner_up_and_stop_hook_active_true_still_nudges_normally(home, monkeypatch, transcript):
    """The guard is for the owner-down path only: with a healthy owner and an
    immature graph the nudge emits whatever `stop_hook_active` says. Mutation: apply
    the guard to the whole flag-ON path -> red."""
    make_store(home, THRESHOLD - 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home):
        run = run_main(mod, transcript, stop_hook_active=True)
    assert json.loads(run.stdout)["reason"].startswith(NUDGE_PREFIX)
    assert run.state is not None


@requires_linux_owner
def test_c3_rejected_by_a_real_owner_is_loud_and_never_opens_the_store(home, monkeypatch, transcript):
    """Live cell (Linux seat): the store has no `nodes` table, so the real owner's
    registry refuses the statement. Loud REFUSED block naming the owner's own reason;
    the store is never opened by the hook. Mutation: read a rejection as 'immature' or
    fall back to the store -> red."""
    (home / "knowledge.db").write_bytes(b"")
    sqlite3.connect(str(home / "knowledge.db")).close()
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    with owner_of("real", home):
        seen = spy_store_opens(monkeypatch, mod)
        run = run_main(mod, transcript)
    assert store_opens(seen, home) == [], seen
    reason = json.loads(run.stdout)["reason"]
    assert "refused" in reason.lower() and "not retryable" in reason.lower()
    assert run.state is None


# --- C4: the timeout budget -------------------------------------------------------------

def _stop_registration_timeout() -> int:
    hooks = json.loads((REPO_ROOT / "src" / "engram" / "hooks" / "hooks.json").read_text())
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "/engram-stop-hook.py" in str(node.get("command", "")):
                found.append(node["timeout"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(hooks)
    assert len(found) == 1, found
    return found[0]


def test_c4_owner_call_gets_at_most_half_the_hook_registration(home, monkeypatch):
    """The Stop hook is registered with a 5 s timeout; interpreter start, imports and
    the transcript scan sit inside it too, so the owner call gets AT MOST half.
    Mutation: raise OWNER_READ_TIMEOUT_S above registration/2 (or to the client's 2 s
    default plus margin) -> red."""
    mod = load_hook(home, monkeypatch)
    registration = _stop_registration_timeout()
    assert registration == 5
    assert 0 < mod.OWNER_READ_TIMEOUT_S <= registration / 2


def test_c4_the_call_passes_that_bound_the_registered_template_and_method_read(home, monkeypatch):
    """Flag ON sends method `read`, params exactly {sql: the hook's constant, args: []}
    where the constant equals the committed template byte for byte, and passes
    timeout=OWNER_READ_TIMEOUT_S. Mutation: a hand-copied literal, another timeout, or
    another method -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    calls = []

    def spy(method, params, **kw):
        calls.append((method, params, kw))
        return ec.Ok(result=[[THRESHOLD + 3]])

    monkeypatch.setattr(ec, "read", spy)
    assert mod._maturity_muted() is True
    assert calls == [("read", {"sql": COUNT_SQL, "args": []}, {"timeout": mod.OWNER_READ_TIMEOUT_S})], calls
    assert mod._CURRENT_COUNT_SQL == COUNT_SQL and COUNT_SQL in registered_sql()


def test_c4_a_trickling_peer_cannot_hold_the_hook_past_its_budget(home, monkeypatch, transcript):
    """A peer that dribbles a byte every 150 ms and never finishes a line (a silent peer
    alone passes the wrong implementation). The whole hook invocation, measured, ends
    inside the owner bound plus a margin, with the loud unavailable block. Mutation:
    pass the bound to a per-socket-op timeout only, or no timeout -> overrun -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    (home / "run").mkdir(mode=0o700)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(home / "run" / "engramd.sock"))
    srv.listen(1)
    stop = threading.Event()

    def trickle():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.recv(65536)
            for _ in range(60):
                if stop.is_set():
                    break
                conn.sendall(b"x")
                time.sleep(0.15)
        except OSError:
            pass
        finally:
            conn.close()

    t = threading.Thread(target=trickle, daemon=True)
    t.start()
    mod = load_hook(home, monkeypatch)
    try:
        start = time.monotonic()
        run = run_main(mod, transcript)
        elapsed = time.monotonic() - start
    finally:
        stop.set()
        srv.close()
        t.join(timeout=2)
    assert elapsed < mod.OWNER_READ_TIMEOUT_S + 0.5, elapsed
    assert "unavailable" in json.loads(run.stdout)["reason"].lower()


# --- C5: the deployed (flattened) layout ---------------------------------------------------

def test_c5_flattened_deployed_layout_resolves_both_runtime_modules_and_reads_through_the_owner(home, tmp_path):
    """The plugin build flattens hooks/claude/* -> hooks/* and puts the runtime modules
    at the plugin root: <root>/engramd_client.py, <root>/engramd_owner_flags.py,
    <root>/hooks/{engram-stop-hook.py,_hooklib.py,_stophooklib.py}. Recreated in a tmp
    dir with no src/engram ancestor, CLAUDE_PLUGIN_ROOT and ENGRAM_RUNTIME_DIR UNSET,
    flag ON, a MATURE store, an in-process fake owner. A hook that resolved both modules
    stays silent (muted) and the owner saw exactly one `read`. A hook that could not
    resolve the flag helper reads OFF (old path: ALSO silent), so the
    owner-saw-a-read assertion is what turns this cell red for a broken FLAG
    resolver (or both). Breaking ONLY the client resolver does NOT turn this cell
    red: the correctly resolved flag helper puts the plugin root on sys.path, so
    the client is found regardless; the C3 unimportable-client cells catch a broken
    client import incidentally, and the client resolver cannot be isolated here.
    Mutation: calibrate the flag-helper resolver to the source layout (fixed
    parents[N]) -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    root = tmp_path / "engram-plugin"
    (root / "hooks").mkdir(parents=True)
    for name in ("engram-stop-hook.py", "_hooklib.py", "_stophooklib.py"):
        shutil.copy(_HOOKS_SRC / name, root / "hooks" / name)
    # #3707: engramd_client imports engram_pidfile; #3768: engram_pidfile imports engram_regular_io; the build ships all at the plugin root (tiers.json, essential).
    for name in ("engramd_client.py", "engramd_owner_flags.py", "engram_pidfile.py", "engram_regular_io.py"):
        shutil.copy(Path(_SRC_ENGRAM) / name, root / name)
    transcript = home / "t.jsonl"
    transcript.write_text("{}\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    with FakeOwner(home) as owner:
        r = subprocess.run([sys.executable, str(root / "hooks" / "engram-stop-hook.py")],
                           input=json.dumps({"session_id": "s", "transcript_path": str(transcript)}),
                           capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stdout == "", r.stdout
    assert [q["method"] for q in owner.requests] == ["read"], (owner.requests, r.stderr)


def test_c5_subprocess_owner_down_exit_code_zero_and_one_block_on_stdout(home):
    """The real process, owner down, flag ON, mature store: exit 0 and exactly one
    loud block on stdout (never a traceback, never empty); with
    `stop_hook_active: true` on stdin: exit 0, empty stdout, one stderr line.
    Mutation: let the failure escape main (exit 1) or parse the payload key wrongly
    -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    first = subprocess.run([sys.executable, str(_HOOK_PATH)], input=json.dumps({"session_id": "s"}),
                           capture_output=True, text=True, env=env, cwd=str(home))
    second = subprocess.run([sys.executable, str(_HOOK_PATH)],
                            input=json.dumps({"session_id": "s", "stop_hook_active": True}),
                            capture_output=True, text=True, env=env, cwd=str(home))
    assert first.returncode == 0 and second.returncode == 0, (first.stderr, second.stderr)
    assert json.loads(first.stdout)["decision"] == "block" and len(first.stdout.splitlines()) == 1
    assert second.stdout == ""
    assert len([ln for ln in second.stderr.splitlines() if ln.strip()]) == 1


# --- C6: the launcher's gate covers this flag ------------------------------------------------

def test_c6_only_this_flag_true_makes_any_owner_flag_on_true(home, monkeypatch):
    """With ONLY `engramd.stop_reads_via_owner` true in config.json the launcher's
    `any_owner_flag_on` is True (covered by the `*_via_owner` pattern, no helper change),
    and the key the hook reads IS that key. Mutation: rename the hook's key to one the
    pattern does not match (or a typo) -> red."""
    mod = load_hook(home, monkeypatch)
    cfg = home / "config.json"
    cfg.write_text(json.dumps({"engramd": {mod._OWNER_FLAG_KEY: True}}))
    assert mod._OWNER_FLAG_KEY == FLAG_KEY == "stop_reads_via_owner"
    assert eof.any_owner_flag_on(cfg) is True
    cfg.write_text(json.dumps({"engramd": {mod._OWNER_FLAG_KEY: False}}))
    assert eof.any_owner_flag_on(cfg) is False


# --- X1: the stop hook gates on its OWN key only ---------------------------------------------

def _no_client(monkeypatch, mod):
    """Poison every route to the owner client: import raises, the seams fail the test."""
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    for name in ("_import_engramd_client", "_read_count_via_owner"):
        monkeypatch.setattr(mod, name, lambda *a, **k: pytest.fail("owner seam used on the direct path"), raising=False)


@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature", "mature"])
def test_x1_another_owner_flag_alone_leaves_the_stop_hook_on_the_direct_path(current, home, monkeypatch, transcript):
    """CROSS-FLAG. config.json has ONLY `engramd.user_identity_reads_via_owner: true`
    (the stop key absent): the stop hook stays DIRECT -- the client is never imported or
    called, the count is read from the store, and stdout + nudge state are byte-identical
    to flag OFF (both an immature and a mature store). Mutation: gate the hook on
    `engramd_owner_flags.any_owner_flag_on(CONFIG_PATH)` instead of
    `flag_on(_OWNER_FLAG_KEY, CONFIG_PATH)` -> the hook takes the owner path (no owner
    running: a loud block / the poisoned seam) -> red; the pre-existing cells stay green."""
    make_store(home, current)
    mod = load_hook(home, monkeypatch)
    set_config(home, flag=False)
    fresh(mod)
    off = run_main(mod, transcript)
    (home / "config.json").write_text(json.dumps({
        "cadence": {"write_nudge_node_threshold": THRESHOLD},
        "engramd": {"user_identity_reads_via_owner": True},
    }))
    assert eof.any_owner_flag_on(home / "config.json") is True  # the other flag really is live
    _no_client(monkeypatch, mod)
    fresh(mod)
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert (run.stdout, run.state, run.stderr) == (off.stdout, off.state, off.stderr)
    assert bool(run.stdout) is (current < THRESHOLD)
    assert [t[1] for t in run.telemetry] == [t[1] for t in off.telemetry]


@pytest.mark.parametrize("flags,label", [
    ({FLAG_KEY: True}, "stop-only"),
    ({FLAG_KEY: True, "user_identity_reads_via_owner": True}, "both"),
])
def test_x1_the_stop_key_alone_or_with_another_selects_the_owner_path(flags, label, home, monkeypatch, transcript):
    """Inverse + third variant: the stop key true (alone, or together with the
    user-identity key) selects the OWNER path -- the fake owner sees exactly one `read`
    and a mature store is silent. Mutation: gate on the user-identity key, or ignore
    the stop key -> the owner sees no read -> red."""
    make_store(home, THRESHOLD + 3)
    (home / "config.json").write_text(json.dumps({
        "cadence": {"write_nudge_node_threshold": THRESHOLD}, "engramd": flags}))
    mod = load_hook(home, monkeypatch)
    with FakeOwner(home) as owner:
        run = run_main(mod, transcript)
    assert run.exc is None and run.stdout == "", (label, run.exc, run.stdout)
    assert [q["method"] for q in owner.requests] == ["read"], (label, owner.requests)


# --- X2: a deeply nested config.json -------------------------------------------------------------

_NESTED = "[" * 200000 + "]" * 200000


def test_x2_deeply_nested_config_reads_flag_off_and_nothing_raises(home, monkeypatch, transcript):
    """config.json is 200000 nested brackets (json raises RecursionError). The flag
    reads OFF, `_owner_reads_enabled()` and `_maturity_muted()` raise nothing, and the
    hook behaves exactly as flag OFF on an unreadable config (direct path; the client is
    never imported). Reference run: the same store with a malformed `{nope` config (the
    hook then also uses its default threshold). Mutation: drop RecursionError from the
    except clause of `engramd_owner_flags` (so it escapes `flag_on`) ->
    `_owner_reads_enabled()` raises -> red."""
    make_store(home, THRESHOLD + 3)
    mod = load_hook(home, monkeypatch)
    set_config(home, raw="{nope")
    fresh(mod)
    ref = run_main(mod, transcript)
    set_config(home, raw=_NESTED)
    _no_client(monkeypatch, mod)
    assert mod._owner_reads_enabled() is False
    assert mod._maturity_muted() is False  # default threshold 300 > 13 rows: not muted, no raise
    fresh(mod)
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert (run.stdout, run.state) == (ref.stdout, ref.state)
    assert json.loads(run.stdout)["reason"].startswith(NUDGE_PREFIX)


def test_x2_the_whole_hook_survives_a_deeply_nested_config_as_a_process(home):
    """Same file, real process (the other config reader, `_read_node_threshold`, has its
    own error handling): exit 0 within a bound, and stdout equals the stdout for a
    flag-OFF `{nope` config on the same store. Mutation: let a RecursionError escape
    either config reader into main -> nonzero exit / differing stdout -> red."""
    make_store(home, THRESHOLD + 3)
    tr = home / "t.jsonl"
    tr.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 600}]}}) + "\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    payload = json.dumps({"session_id": "s-x2", "transcript_path": str(tr)})
    outs = []
    for raw in ("{nope", _NESTED):
        (home / "config.json").write_text(raw)
        (home / ".write-nudge-last-fire.json").unlink(missing_ok=True)
        r = subprocess.run([sys.executable, str(_HOOK_PATH)], input=payload, capture_output=True,
                           text=True, env=env, cwd=str(home), timeout=60)
        assert r.returncode == 0, (raw[:8], r.returncode, r.stderr[-2000:])
        outs.append(r.stdout)
    assert outs[0] == outs[1], outs
    assert json.loads(outs[1])["reason"].startswith(NUDGE_PREFIX), outs  # not vacuous: a nudge was emitted


# --- R3: any exception from the client import, a non-Ok result, a broken flag helper -------------

def _runtime_with(home: Path, monkeypatch, name: str, source: str) -> Path:
    """An ENGRAM_RUNTIME_DIR holding ONE bad module `name`; the real module of that
    name is dropped from sys.modules for the test (restored after) and sys.path is
    restored too, so the hook's sys.path insertions cannot leak between cells."""
    rt = home / f"bad-{name[:-3]}-runtime"
    rt.mkdir()
    (rt / name).write_text(source)
    monkeypatch.setenv("ENGRAM_RUNTIME_DIR", str(rt))
    monkeypatch.delitem(sys.modules, name[:-3], raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return rt


def _loud_run(home, monkeypatch, transcript, mod, active):
    seen = spy_store_opens(monkeypatch, mod)
    monkeypatch.setattr(mod, "_update_state", lambda *a, **k: pytest.fail("nudge state advanced"))
    monkeypatch.setattr(mod, "_should_suppress", lambda *a, **k: pytest.fail("idle machinery consulted"))
    run = run_main(mod, transcript, **({"stop_hook_active": True} if active else {}))
    assert run.exc is None, run.exc
    assert store_opens(seen, home) == [], seen
    assert run.state is None
    assert [t[1] for t in run.telemetry] == ["engram.telemetry.reached"]
    return run


def _assert_exactly_one_loud_outcome(run, active):
    if active:
        assert run.stdout == ""
        err = [ln for ln in run.stderr.splitlines() if ln.strip()]
        assert len(err) == 1, run.stderr
        assert "engramd" in err[0].lower()
        return None
    lines = [ln for ln in run.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, run.stdout
    out = json.loads(lines[0])
    assert set(out) == {"decision", "reason"} and out["decision"] == "block"
    assert not out["reason"].startswith(NUDGE_PREFIX)
    assert "zero" in out["reason"].lower()
    assert out["reason"].index("Agent steps") < out["reason"].index("Human steps")
    return out["reason"]


@pytest.mark.parametrize("active", [False, True], ids=["chain-fresh", "stop-hook-active"])
@pytest.mark.parametrize("exc_name,source", [
    ("TypeError", "raise TypeError(\"unsupported operand type(s) for |: 'type' and 'type'\")\n"),
    ("SyntaxError", "def (:\n"),
    ("ValueError", "raise ValueError('boom at import')\n"),
])
def test_r3_f1_a_client_module_that_raises_any_exception_at_import_is_loud_not_a_nudge(
        exc_name, source, active, home, monkeypatch, transcript):
    """Flag ON, mature store; `engramd_client.py` raises a NON-ImportError while being
    imported (a `X | Y` union on Python < 3.10 is a TypeError). Exactly ONE loud
    outcome: a block (stop_hook_active not true) whose Cause line names the exception
    TYPE, or one stderr line and no block (exactly true); the store is never opened,
    the nudge state never advances, no nudge. Mutant: narrow
    `_read_count_via_owner`'s import handler back to `except ImportError` -> the
    exception reaches `_maturity_muted`'s blanket handler, reads as 'not muted' and
    nudges -> all six parametrizations red; the pre-existing C3 `unimportable` cells
    (an ImportError) stay green."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    _runtime_with(home, monkeypatch, "engramd_client.py", source)
    run = _loud_run(home, monkeypatch, transcript, mod, active)
    reason = _assert_exactly_one_loud_outcome(run, active)
    if reason is not None:
        assert "could not be loaded" in reason and f"{exc_name}" in reason, reason
        cause = [ln for ln in reason.splitlines() if ln.startswith("  Cause:")]
        assert len(cause) == 1 and len(cause[0]) <= 310, cause


@pytest.mark.parametrize("active", [False, True], ids=["chain-fresh", "stop-hook-active"])
@pytest.mark.parametrize("label,make", [
    ("None", lambda: None),
    ("tuple", lambda: ("ok", [[99]])),
    ("object", lambda: object()),
    ("ok-lookalike", lambda: __import__("types").SimpleNamespace(result=[[99]])),
])
def test_r3_f2_a_result_that_is_not_an_ok_is_an_unusable_answer_never_a_count(
        label, make, active, home, monkeypatch, transcript):
    """`ec.read` returns something that is none of Ok / Unavailable / Rejected (None, a
    tuple, a bare object, and an object that merely HAS a `.result` of [[99]] -- on
    a mature store 99 would silently mute). Loud 'unusable answer' naming the
    unexpected type, once per stop chain; never a count, never the nudge by accident.
    Mutant: drop the `isinstance(result, ec.Ok)` guard (read `result.result`
    unconditionally) -> None/tuple/object raise AttributeError into the blanket
    handler (a nudge) and the lookalike is read as 99 (silent): all red. Mutant 2
    (`hasattr(result, 'result')` instead of isinstance) -> only the lookalike red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    value = make()
    monkeypatch.setattr(ec, "read", lambda *a, **k: value)
    run = _loud_run(home, monkeypatch, transcript, mod, active)
    reason = _assert_exactly_one_loud_outcome(run, active)
    if reason is not None:
        assert "unusable" in reason.lower() and type(value).__name__ in reason, reason


@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature", "mature"])
@pytest.mark.parametrize("how", ["import-TypeError", "import-SyntaxError", "flag_on-OSError"])
def test_r3_f3_a_broken_flag_helper_of_any_kind_reads_flag_off_and_takes_the_direct_path(
        how, current, home, monkeypatch, transcript):
    """Config says the flag is ON, but the flag helper cannot be used: it raises a
    non-ImportError while importing, or `flag_on` itself raises OSError. Documented
    decision: the hook cannot know the flag is ON, so this reads as flag OFF -- the
    direct path, exactly like a missing helper module (NOT loud). Observable: the
    direct count read happens exactly once (store opened once through
    `connect_regular_ro`), `engramd_client` is never imported, and the outcome is the
    OFF-path outcome (immature -> the nudge + its state; mature -> silent)
    once the R6 report is stripped; stderr is exactly the one report line (class only).
    Mutant:
    narrow `_owner_reads_enabled` back to `except ImportError` (or leave `flag_on(...)`
    outside the try) -> the exception reaches the blanket handler, the count is NEVER
    read, and the hook nudges: all six cells go red on the `opens == [knowledge.db]`
    assertion (the mature ones also on the silent-stdout assertion)."""
    make_store(home, current)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    if how == "import-TypeError":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", "raise TypeError('union on old python')\n")
    elif how == "import-SyntaxError":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", "def (:\n")
    else:
        def boom(key, path):
            raise OSError("config.json unreadable")
        monkeypatch.setattr(eof, "flag_on", boom)
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    monkeypatch.setattr(mod, "_import_engramd_client",
                        lambda *a, **k: pytest.fail("client imported with the flag unreadable"))
    monkeypatch.setattr(mod, "_REPORT_TO_AGENT", True)
    opens = []
    real_ro = mod.connect_regular_ro

    def counting_ro(path, *a, **k):
        opens.append(str(path))
        return real_ro(path, *a, **k)

    monkeypatch.setattr(mod, "connect_regular_ro", counting_ro)
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert opens == [str(home / "knowledge.db")], opens
    # R6: the failure is reported (class only): stderr is exactly the one report line, and
    # the outcome is the OFF-path outcome once the report is stripped.
    cls = {"import-TypeError": "TypeError", "import-SyntaxError": "SyntaxError",
           "flag_on-OSError": "OSError"}[how]
    stdout, report = _strip_report(run.stdout)
    assert report == _line(cls) and run.stderr == _line(cls) + "\n"
    if current < THRESHOLD:
        assert json.loads(stdout)["reason"].startswith(NUDGE_PREFIX)
        assert run.state is not None
    else:
        assert stdout == "" and run.state is None


# --- R4: one net behind the specific handlers, SystemExit, `is True`, the redirect seam, long messages ---

_STR_BOOM = (
    "class StrBoom(Exception):\n"
    "    def __str__(self):\n"
    "        raise RuntimeError('str boom')\n"
)


class StrBoom(Exception):
    """An exception whose __str__ raises: any f-string / str() of it inside a handler
    blows up there, which is what pushed r3's flag-ON failures into the blanket handler."""

    def __str__(self):
        raise RuntimeError("str boom")


def _assert_generic_net_block(reason, type_name):
    # The exception the net sees is the one RAISED INSIDE the specific handler (the
    # __str__ failure, a RuntimeError), not the original StrBoom: the block names that
    # type and the generic "unexpectedly" text, and never a message.
    assert "failed unexpectedly" in reason and f"unexpected {type_name}" in reason, reason
    assert "str boom" not in reason
    assert reason.index("Agent steps") < reason.index("Human steps")


@pytest.mark.parametrize("active", [False, True], ids=["chain-fresh", "stop-hook-active"])
@pytest.mark.parametrize("site", ["import", "import-importerror", "read"])
def test_r4_g1_an_exception_with_a_raising_str_is_one_loud_outcome_never_a_nudge(
        site, active, home, monkeypatch, transcript):
    """Flag ON, mature store. The exception raised by the client at import (a plain
    Exception subclass, and an ImportError subclass) or from `ec.read` has a __str__
    that RAISES, so the specific handler's f-string raises inside the handler. The
    single net in `_maturity_muted` turns that into ONE loud outcome (a block whose text
    names the exception TYPE and never calls str() on it; or one stderr line when
    stop_hook_active is exactly true). Mutant: remove the net -> the RuntimeError
    reaches the blanket handler, reads 'not muted' and nudges -> all six red; the
    pre-existing r3/C3 cells stay green."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    if site == "import":
        _runtime_with(home, monkeypatch, "engramd_client.py", _STR_BOOM + "raise StrBoom('x')\n")
    elif site == "import-importerror":
        _runtime_with(home, monkeypatch, "engramd_client.py",
                      "class StrBoom(ImportError):\n    def __str__(self):\n        raise RuntimeError('str boom')\n"
                      "raise StrBoom('x')\n")
    else:
        monkeypatch.setattr(ec, "read", _raising(StrBoom("x")))
    run = _loud_run(home, monkeypatch, transcript, mod, active)
    reason = _assert_exactly_one_loud_outcome(run, active)
    if reason is not None and site == "import-importerror":
        # GUARD, not a net cell: the ImportError wrapper's own f-string fails first and the
        # specific "could not be loaded" handler reports that RuntimeError (with its text).
        assert "RuntimeError" in reason, reason
    elif reason is not None:
        _assert_generic_net_block(reason, "RuntimeError")


@pytest.mark.parametrize("active", [False, True], ids=["chain-fresh", "stop-hook-active"])
@pytest.mark.parametrize("missing", ["Unavailable", "Ok", "Rejected"])
def test_r4_g1_a_client_lacking_a_result_class_is_loud_not_a_nudge(
        missing, active, home, monkeypatch, transcript):
    """Flag ON, mature store. A client that imports fine but lacks one of
    `Unavailable` / `Ok` / `Rejected` -> AttributeError at `ec.<name>` inside
    `_read_count_via_owner`. Loud once (block naming AttributeError, or one stderr
    line). Mutant: remove the net -> AttributeError reaches the blanket handler ->
    nudge -> all six red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    classes = [n for n in ("Unavailable", "Ok", "Rejected") if n != missing]
    src = "".join(f"class {n}:\n    pass\n" for n in classes)
    src += "def read(*a, **k):\n    return None\n"
    _runtime_with(home, monkeypatch, "engramd_client.py", src)
    run = _loud_run(home, monkeypatch, transcript, mod, active)
    reason = _assert_exactly_one_loud_outcome(run, active)
    if reason is not None:
        assert "AttributeError" in reason, reason


@pytest.mark.parametrize("active", [False, True], ids=["chain-fresh", "stop-hook-active"])
@pytest.mark.parametrize("code", [0, 3])
@pytest.mark.parametrize("site", ["import", "read"])
def test_r4_g2_a_systemexit_from_the_client_is_an_owner_failure_never_a_silent_exit(
        site, code, active, home, monkeypatch, transcript):
    """Flag ON, mature store. The client raises SystemExit(0) / SystemExit(3) at import
    or from `ec.read`. SystemExit is a BaseException: before this fix nothing caught it
    and the hook exited silently with that code. Now: ONE loud outcome and no exit.
    Mutant: catch only `Exception` at the two call sites -> run.exc is
    'SystemExit(<code>)' -> all eight red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    if site == "import":
        _runtime_with(home, monkeypatch, "engramd_client.py", f"raise SystemExit({code})\n")
    else:
        monkeypatch.setattr(ec, "read", _raising(SystemExit(code)))
    run = _loud_run(home, monkeypatch, transcript, mod, active)
    reason = _assert_exactly_one_loud_outcome(run, active)
    if reason is not None:
        assert "SystemExit" in reason, reason


@pytest.mark.parametrize("site", ["import", "read"])
def test_r4_g2_a_keyboardinterrupt_from_the_client_still_escapes_the_hook(
        site, home, monkeypatch, transcript):
    """GUARD (green on the pre-fix code too): KeyboardInterrupt is deliberately NOT an
    owner failure; it must escape `main` (loud), not be swallowed or turned into a
    block. Mutant (mine): catch BaseException at the two call sites -> the interrupt is
    swallowed into a block -> both red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    if site == "import":
        _runtime_with(home, monkeypatch, "engramd_client.py", "raise KeyboardInterrupt\n")
    else:
        monkeypatch.setattr(ec, "read", _raising(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run_main(mod, transcript)


@pytest.mark.parametrize("value", ["yes", 1, [1], {"a": 1}, "true"], ids=repr)
def test_r4_g3_only_the_boolean_true_from_the_flag_helper_reads_on(value, home, monkeypatch, transcript):
    """The config says the flag is FALSE; a stub `flag_on` returns a truthy NON-bool
    ("yes", 1, [1], {...}, "true"). The rule is that only the JSON boolean `true` is ON,
    so `_owner_reads_enabled()` is False and the hook takes the direct path (the client
    is never imported). Mutant: return the helper's value unfiltered -> truthy -> owner
    path -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(eof, "flag_on", lambda key, path: value)
    _no_client(monkeypatch, mod)
    assert mod._owner_reads_enabled() is False
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert run.stdout == "" and run.stderr == ""  # mature store, direct path: silent


def test_r4_g3_the_boolean_true_from_the_flag_helper_reads_on(home, monkeypatch):
    """Positive control for G3 (guard, green before and after): `flag_on` returning the
    bool True -> ON even though config.json says false. Mutant (mine): `is True`
    replaced by a constant False -> red."""
    set_config(home, flag=False)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(eof, "flag_on", lambda key, path: True)
    assert mod._owner_reads_enabled() is True


def test_r4_g4_the_runtime_dir_redirect_wins_even_when_its_dir_is_already_on_sys_path(
        home, monkeypatch, transcript):
    """F5. ENGRAM_RUNTIME_DIR points at a dir holding a BAD engramd_client.py (TypeError
    at import) and NO engramd_owner_flags.py; the flag is ON via the real helper. The
    redirect dir is ALREADY on sys.path but BEHIND the real source dir (which holds a
    good client), so only an unconditional move-to-position-0 makes the redirect win:
    the Cause line shows the TypeError. Mutant: revert to
    `if runtime_dir not in sys.path: sys.path.insert(0, runtime_dir)` -> the good client
    loads from the source dir, the owner is down, and the block shows no TypeError -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    rt = _runtime_with(home, monkeypatch, "engramd_client.py", "raise TypeError('bad client from the redirect dir')\n")
    assert not (rt / "engramd_owner_flags.py").exists()
    sys.path[:] = [_SRC_ENGRAM] + [p for p in sys.path if p not in (_SRC_ENGRAM, str(rt))] + [str(rt)]
    assert sys.path.index(_SRC_ENGRAM) < sys.path.index(str(rt))
    run = _loud_run(home, monkeypatch, transcript, mod, False)
    reason = _assert_exactly_one_loud_outcome(run, False)
    assert "could not be loaded" in reason, reason
    assert "TypeError: bad client from the redirect dir" in reason, reason


_LONG_MULTILINE = "a\nb" + "A" * 5000


@pytest.mark.parametrize("site", ["import", "read"])
def test_r4_g5_the_cause_line_is_one_bounded_line_whatever_the_message(site, home, monkeypatch, transcript):
    """The client raises ValueError('a\\nb' + 'A'*5000) at import / from `ec.read`. The
    block's Cause line is ONE line (the very next line is 'Agent steps'; no part of the
    message spills onto a line of its own) and is bounded: 'Cause: ' + at most 300
    chars, and is actually truncated (>= 300 chars: not vacuous). Mutants: Cause not
    truncated to 300 -> too long -> red; Cause not whitespace-collapsed -> the
    newline splits it -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    if site == "import":
        _runtime_with(home, monkeypatch, "engramd_client.py", f"raise ValueError({_LONG_MULTILINE!r})\n")
    else:
        monkeypatch.setattr(ec, "read", _raising(ValueError(_LONG_MULTILINE)))
    run = _loud_run(home, monkeypatch, transcript, mod, False)
    reason = _assert_exactly_one_loud_outcome(run, False)
    lines = reason.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("  Cause:")]
    assert len(idx) == 1, lines
    assert lines[idx[0] + 1].startswith("  Agent steps"), lines[idx[0]:idx[0] + 3]
    cause = lines[idx[0]]
    assert "ValueError" in cause
    assert 300 <= len(cause) <= len("  Cause: ") + 300, len(cause)


@pytest.mark.parametrize("site", ["import", "read"])
def test_r4_g5_the_stderr_line_is_exactly_one_line_whatever_the_message(site, home, monkeypatch, transcript):
    """Same raise, `stop_hook_active` exactly true: the stderr output is exactly ONE
    line (one trailing newline, nothing from the message). Mutant: the stderr `short`
    carries str(exc) -> embedded newline(s) -> `count('\\n') == 1` fails -> red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    if site == "import":
        _runtime_with(home, monkeypatch, "engramd_client.py", f"raise ValueError({_LONG_MULTILINE!r})\n")
    else:
        monkeypatch.setattr(ec, "read", _raising(ValueError(_LONG_MULTILINE)))
    run = _loud_run(home, monkeypatch, transcript, mod, True)
    _assert_exactly_one_loud_outcome(run, True)
    assert run.stderr.count("\n") == 1, run.stderr
    assert len(run.stderr) < 300, len(run.stderr)


# --- R5: SystemExit at the flag helper (S1) and zero direct opens on a full flag-ON fire (Z1) ---

def _flag_off_reference(mod, home, transcript):
    """The flag-OFF run of the same store: the outcome a broken flag helper must match."""
    set_config(home, flag=False)
    fresh(mod)
    ref = run_main(mod, transcript)
    assert ref.exc is None, ref.exc
    fresh(mod)
    return ref


@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature", "mature"])
@pytest.mark.parametrize("flag", [..., True], ids=["flag-absent", "flag-present"])
@pytest.mark.parametrize("code", [0, 3])
@pytest.mark.parametrize("site", ["import", "flag_on"])
def test_r5_s1_a_systemexit_from_the_flag_helper_reads_flag_off_and_takes_the_direct_path(
        site, code, flag, current, home, monkeypatch, transcript):
    """A flag helper that raises SystemExit(0) / SystemExit(3) while being imported, or
    whose `flag_on` raises it, with the flag key ABSENT and PRESENT. SystemExit is a
    BaseException: with `except Exception` it escaped `_owner_reads_enabled` and the hook
    printed nothing on EVERY seat, flag or no flag. Now it reads as flag OFF (the
    documented direct path): the outcome (stdout and nudge state file) equals a real
    flag-OFF run of the same store, the store is opened exactly once directly, and
    `engramd_client` is never imported. R6: the outcome is compared with the report stripped
    (`hookSpecificOutput`), and stderr must be exactly the one report line naming SystemExit.
    Mutant: catch only `Exception` in `_owner_reads_enabled` -> run.exc is 'SystemExit(<code>)' -> all sixteen red."""
    make_store(home, current)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "_REPORT_TO_AGENT", True)
    ref = _flag_off_reference(mod, home, transcript)
    set_config(home, flag=flag)
    if site == "import":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", f"raise SystemExit({code})\n")
    else:
        def boom(key, path):
            raise SystemExit(code)
        monkeypatch.setattr(eof, "flag_on", boom)
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    monkeypatch.setattr(mod, "_import_engramd_client",
                        lambda *a, **k: pytest.fail("client imported with the flag unreadable"))
    opens = []
    real_ro = mod.connect_regular_ro

    def counting_ro(path, *a, **k):
        opens.append(str(path))
        return real_ro(path, *a, **k)

    monkeypatch.setattr(mod, "connect_regular_ro", counting_ro)
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert opens == [str(home / "knowledge.db")], opens
    # R6: the failure is reported (class only, SystemExit); the outcome is the flag-OFF
    # outcome once the report is stripped, and stderr is exactly the report line.
    stdout, report = _strip_report(run.stdout)
    assert (stdout, run.state) == (ref.stdout, ref.state), (run.stdout, ref.stdout)
    assert report == _line("SystemExit") and run.stderr == _line("SystemExit") + "\n"
    assert bool(stdout) is (current < THRESHOLD)  # not vacuous: the outcome is the real one


@pytest.mark.parametrize("flag", [..., True], ids=["flag-absent", "flag-present"])
@pytest.mark.parametrize("site", ["import", "flag_on"])
def test_r5_s1_a_keyboardinterrupt_from_the_flag_helper_still_escapes_the_hook(
        site, flag, home, monkeypatch, transcript):
    """GUARD (green on the base too): KeyboardInterrupt is not a flag-helper failure; it
    escapes `main`. Mutant (mine): catch BaseException in `_owner_reads_enabled` -> the
    interrupt reads as flag OFF and the hook finishes normally -> all four red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=flag)
    mod = load_hook(home, monkeypatch)
    if site == "import":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", "raise KeyboardInterrupt\n")
    else:
        def boom(key, path):
            raise KeyboardInterrupt()
        monkeypatch.setattr(eof, "flag_on", boom)
    with pytest.raises(KeyboardInterrupt):
        run_main(mod, transcript)


def _raising_openers(monkeypatch, mod):
    """Make EVERY direct store open raise, and record each attempt. The hook runs in the
    main thread; the owner (fake or real) serves from its own threads and needs the real
    sqlite3.connect, so the raising `sqlite3.connect` applies to the calling thread of the
    hook only (any other thread gets the real one, unrecorded)."""
    attempts = []
    real_connect = sqlite3.connect
    main = threading.main_thread()

    def no_connect(database, *a, **k):
        if threading.current_thread() is main:
            attempts.append(("sqlite3.connect", str(database)))
            raise AssertionError("sqlite3.connect called from the hook: " + str(database))
        return real_connect(database, *a, **k)

    def no_ro(path, *a, **k):
        attempts.append(("connect_regular_ro", str(path)))
        raise AssertionError("connect_regular_ro called: " + str(path))

    monkeypatch.setattr(sqlite3, "connect", no_connect)
    monkeypatch.setattr(mod, "connect_regular_ro", no_ro)
    return attempts


@pytest.mark.parametrize("owner_kind", OWNER_KINDS)
@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature-nudge", "mature-silent"])
def test_r5_z1_a_full_flag_on_fire_with_the_owner_up_opens_the_store_zero_times(
        current, owner_kind, home, monkeypatch, transcript):
    """Flag ON, owner UP and answering, the hook's FULL main() fire (stdin payload,
    transcript). `connect_regular_ro` (hook module) and `sqlite3.connect` (hook thread)
    are patched to RAISE, so a direct read surviving anywhere the hook calls fails the
    cell. The fire completes with the owner-derived decision (immature -> the nudge JSON
    and its state file; mature -> silent, no state) and the attempt record is EMPTY. A
    GUARD on the base (green there: the base has no direct read on the flag-ON path), so
    it is discriminated by its mutant: add a discarded direct read after the owner read
    in the flag-ON branch -> the opener raises (swallowed by the blanket handler into
    'not muted' for a mature store -> a nudge instead of silence) and the attempt record
    is non-empty -> red on both assertions."""
    make_store(home, current)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    calls = []
    orig = ec.read
    with owner_of(owner_kind, home):
        attempts = _raising_openers(monkeypatch, mod)
        with patch.object(ec, "read", lambda *a, **k: (calls.append(a), orig(*a, **k))[1]):
            run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert attempts == [], attempts
    assert len(calls) == 1, "the owner was not asked: the decision did not come from it"
    if current < THRESHOLD:
        assert json.loads(run.stdout)["reason"].startswith(NUDGE_PREFIX)
        assert run.state is not None
    else:
        assert run.stdout == "" and run.state is None


# --- R6 ---------------------------------------------------------------------------------------------
# S3: `_import_owner_flags` tries every candidate dir and reports the LAST failure (mirror of U4d).
# B: a failed flag helper reads flag OFF, is NOT a block, and is VISIBLE: one class-only stderr line
# plus the same line as `hookSpecificOutput.additionalContext` (non-blocking, #3521's shape).

SENTINEL = "SENTINEL-6f3a91"
REPORT_PREFIX = "engram-stop-hook: owner flag helper failed: "


def _line(cls: str) -> str:
    return REPORT_PREFIX + cls


def _good_copy_source() -> str:
    return Path(eof.__file__).read_text() + "\nGOOD_COPY = True\n"


def _two_candidates(home, monkeypatch, explicit_src, resolved_src):
    """Build the two import candidates of `_import_owner_flags`: an explicit
    ENGRAM_RUNTIME_DIR (`explicit_src` is the source of its engramd_owner_flags.py, or None =
    the dir exists but holds no copy) and the resolved dir (same). The resolved dir is
    injected by patching the shared resolver for THIS sentinel only. The repo's own src dir is
    taken off sys.path (else a real copy would be found behind both candidates)."""
    exp, res = home / "cand-explicit", home / "cand-resolved"
    exp.mkdir()
    res.mkdir()
    if explicit_src is not None:
        (exp / "engramd_owner_flags.py").write_text(explicit_src)
    if resolved_src is not None:
        (res / "engramd_owner_flags.py").write_text(resolved_src)
    monkeypatch.setenv("ENGRAM_RUNTIME_DIR", str(exp))
    monkeypatch.delitem(sys.modules, "engramd_owner_flags", raising=False)
    src_real = os.path.realpath(_SRC_ENGRAM)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if os.path.realpath(p) != src_real])
    hooklib = sys.modules["_hooklib"]
    orig = hooklib.resolve_runtime_dir
    monkeypatch.setattr(hooklib, "resolve_runtime_dir",
                        lambda h, s: str(res) if s == "engramd_owner_flags.py" else orig(h, s))
    return exp, res


def _used_copy_dir():
    m = sys.modules.get("engramd_owner_flags")
    return None if m is None else os.path.dirname(m.__file__)


@pytest.mark.parametrize("layout", ["bad-then-good", "good-then-bad", "empty-then-good"])
def test_r6_s3_a_bad_copy_in_one_candidate_does_not_hide_a_good_copy_in_the_next(
        layout, home, monkeypatch, transcript):
    """Two candidate dirs, flag ON, owner up (fake). `bad-then-good`: the explicit copy raises
    TypeError at import, the resolved copy is good -> the good copy is used and the flag reads
    ON: the count comes from the OWNER (zero direct opens, the owner is asked once) and nothing
    is recorded as failed. The other layouts are guards (a good first copy; an explicit dir
    with no copy). Mutant: catch only ImportError per candidate (a TypeError escapes the loop
    at once) -> `bad-then-good` reads OFF, opens the store and records TypeError: red; the two
    guards stay green."""
    make_store(home, THRESHOLD - 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    bad = "raise TypeError('bad copy')\n"
    exp_src, res_src = {"bad-then-good": (bad, _good_copy_source()),
                        "good-then-bad": (_good_copy_source(), bad),
                        "empty-then-good": (None, _good_copy_source())}[layout]
    exp, res = _two_candidates(home, monkeypatch, exp_src, res_src)
    calls = []
    orig = ec.read
    with FakeOwner(home):
        attempts = _raising_openers(monkeypatch, mod)
        with patch.object(ec, "read", lambda *a, **k: (calls.append(a), orig(*a, **k))[1]):
            run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert mod._OWNER_FLAGS_FAILURE is None
    assert _used_copy_dir() == str(exp if layout == "good-then-bad" else res)
    assert getattr(sys.modules["engramd_owner_flags"], "GOOD_COPY", False) is True
    assert attempts == [], attempts
    assert len(calls) == 1, "the owner was not asked: the flag did not read ON"
    assert json.loads(run.stdout)["reason"].startswith(NUDGE_PREFIX)
    assert "owner flag helper failed" not in run.stdout + run.stderr


def test_r6_s3_both_copies_bad_reads_off_and_records_the_class_of_the_last_failure(home, monkeypatch):
    """Explicit copy raises TypeError, resolved copy is a SyntaxError: reads OFF, and the
    recorded class is the LAST one tried (SyntaxError), not the first and not a generic
    ImportError. Mutant: `raise ImportError(...)` always after the loop (drop `raise last`)
    -> recorded 'ImportError': red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    _two_candidates(home, monkeypatch, "raise TypeError('first')\n", "def (:\n")
    assert mod._owner_reads_enabled() is False
    assert mod._OWNER_FLAGS_FAILURE == "SyntaxError"


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit-dir", "no-explicit-dir"])
def test_r6_s3_no_copy_anywhere_records_importerror(explicit, home, monkeypatch):
    """No candidate holds the module: OFF, recorded class 'ImportError' (the normalized
    'not found'). Mutant: re-raise the last ModuleNotFoundError as is -> 'ModuleNotFoundError':
    red. (That a failure OTHER than not-found names its own class is the cell above.)"""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    _two_candidates(home, monkeypatch, None, None)
    if not explicit:
        monkeypatch.delenv("ENGRAM_RUNTIME_DIR")
    assert mod._owner_reads_enabled() is False
    assert mod._OWNER_FLAGS_FAILURE == "ImportError"


def test_r6_s3_known_limit_a_systemexit_from_the_explicit_copy_propagates_and_the_good_copy_is_not_tried(
        home, monkeypatch):
    """KNOWN LIMIT, pinned on purpose (recorded by the design seat): a SystemExit from the
    explicit-dir copy is not an Exception, so it leaves the loop at once and the good resolved
    copy is never tried. It still reads OFF through `_owner_reads_enabled`'s catch and records
    'SystemExit'. If this cell goes red because the good copy WAS used, the limit was lifted:
    update the docstring of `_import_owner_flags` and this cell together."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    exp, res = _two_candidates(home, monkeypatch, "raise SystemExit(3)\n", _good_copy_source())
    assert mod._owner_reads_enabled() is False
    assert mod._OWNER_FLAGS_FAILURE == "SystemExit"
    assert _used_copy_dir() != str(res)


# ---- the visible report -------------------------------------------------------------------------

def _break_helper(site, mod, home, monkeypatch):
    """Break the flag helper at `site`; return the class name the report must carry. Every
    exception message carries SENTINEL, which must never be printed."""
    if site == "import-TypeError":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", f"raise TypeError('{SENTINEL}')\n")
        return "TypeError"
    if site == "import-SyntaxError":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", f"def {SENTINEL}(:\n")
        return "SyntaxError"
    if site == "import-SystemExit":
        _runtime_with(home, monkeypatch, "engramd_owner_flags.py", f"raise SystemExit('{SENTINEL}')\n")
        return "SystemExit"
    if site == "missing":
        monkeypatch.setitem(sys.modules, "engramd_owner_flags", None)
        return "ImportError"
    exc = OSError if site == "flag_on-OSError" else SystemExit

    def boom(key, path):
        raise exc(SENTINEL)

    monkeypatch.setattr(eof, "flag_on", boom)
    return exc.__name__


SITES = ["import-TypeError", "import-SyntaxError", "import-SystemExit",
         "flag_on-OSError", "flag_on-SystemExit", "missing"]


def _no_client_and_count_opens(monkeypatch, mod):
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    monkeypatch.setattr(mod, "_import_engramd_client",
                        lambda *a, **k: pytest.fail("client imported with the flag unreadable"))
    opens = []
    real_ro = mod.connect_regular_ro

    def counting_ro(path, *a, **k):
        opens.append(str(path))
        return real_ro(path, *a, **k)

    monkeypatch.setattr(mod, "connect_regular_ro", counting_ro)
    return opens


@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature", "mature"])
@pytest.mark.parametrize("site", SITES)
def test_r6_b_a_failed_flag_helper_is_reported_visibly_and_never_blocks_by_itself(
        site, current, home, monkeypatch, transcript):
    """Flag helper broken at each site. The hook reads flag OFF (direct open exactly once,
    client never imported, state equals the flag-OFF reference) and reports: stdout is ONE
    JSON object carrying `hookSpecificOutput == {hookEventName: Stop, additionalContext:
    <the exact line>}` and stderr is that same single line. Mature: NO `decision` key (the
    outcome without the failure was silent). Immature: the nudge block is unchanged
    (`decision`/`reason` equal the reference) AND the report rides along. Class only: the
    exception message (SENTINEL) appears nowhere. Mutants: M3 add `decision: block` to the
    report-only object -> mature cells red; M4 print the message instead of the class ->
    every cell red; M5 skip the report on the muted early return -> mature cells red,
    immature stay green."""
    make_store(home, current)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "_REPORT_TO_AGENT", True)
    ref = _flag_off_reference(mod, home, transcript)
    set_config(home, flag=True)
    cls = _break_helper(site, mod, home, monkeypatch)
    opens = _no_client_and_count_opens(monkeypatch, mod)
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert opens == [str(home / "knowledge.db")], opens
    line = _line(cls)
    assert run.stderr == line + "\n", run.stderr
    lines = [ln for ln in run.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, run.stdout
    obj = json.loads(lines[0])
    assert obj["hookSpecificOutput"] == {"hookEventName": "Stop", "additionalContext": line}
    rest = {k: v for k, v in obj.items() if k != "hookSpecificOutput"}
    if current >= THRESHOLD:
        assert ref.stdout == "" and "decision" not in obj and rest == {}, obj
    else:
        assert rest == json.loads(ref.stdout), (rest, ref.stdout)
        assert rest["decision"] == "block" and rest["reason"].startswith(NUDGE_PREFIX)
    assert run.state == ref.state
    assert SENTINEL not in run.stdout + run.stderr


@pytest.mark.parametrize("active", [False, True], ids=["chain-fresh", "stop-hook-active"])
@pytest.mark.parametrize("site", SITES)
def test_r6_b_a_broken_helper_never_produces_a_block_on_a_mature_store(
        site, active, home, monkeypatch, transcript):
    """Mature store, any broken-helper site, stop_hook_active either way: the printed object
    never has `decision` (the report alone must not block). Mutant M3 -> all red."""
    make_store(home, THRESHOLD + 3)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "_REPORT_TO_AGENT", True)
    set_config(home, flag=True)
    _break_helper(site, mod, home, monkeypatch)
    run = run_main(mod, transcript, **({"stop_hook_active": True} if active else {}))
    assert run.exc is None, run.exc
    if active:
        # Measured (Claude Code 2.1.296): a Stop hook printing additionalContext without a
        # block CONTINUES the conversation and Stop fires again with stop_hook_active=true,
        # so on an already-active chain the report is stderr-only and stdout is EMPTY.
        assert run.stdout == "", run.stdout
        assert run.stderr.strip() != ""  # not vacuous: the report is still written
        return
    obj = json.loads(run.stdout)
    assert "decision" not in obj and "reason" not in obj, obj
    assert "hookSpecificOutput" in obj  # not vacuous


@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature", "mature"])
@pytest.mark.parametrize("site", ["import-TypeError", "flag_on-OSError"])
def test_r6_b_report_to_agent_false_keeps_stdout_as_today_and_only_writes_stderr(
        site, current, home, monkeypatch, transcript):
    """`_REPORT_TO_AGENT = False`: the stderr line is still written, stdout is byte-equal to
    the flag-OFF reference (the report is agent-invisible). Mutant M6: ignore the constant
    (always add additionalContext) -> red."""
    make_store(home, current)
    mod = load_hook(home, monkeypatch)
    ref = _flag_off_reference(mod, home, transcript)
    set_config(home, flag=True)
    monkeypatch.setattr(mod, "_REPORT_TO_AGENT", False)
    cls = _break_helper(site, mod, home, monkeypatch)
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert run.stderr == _line(cls) + "\n"
    assert run.stdout == ref.stdout and run.state == ref.state


def test_r6_b_report_to_agent_ships_true_once_per_turn_end(home, monkeypatch):
    """The shipped value is True under the design seat's ruling ("once per turn end": the
    agent-visible report only while stop_hook_active is not true; see the gate cell below).
    Mutant: ship False -> red. Flipping it is a one-value decision with a stated cost: one
    extra model turn per stop on a seat whose flag helper is failing."""
    assert load_hook(home, monkeypatch)._REPORT_TO_AGENT is True


@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature", "mature"])
@pytest.mark.parametrize("site", ["import-TypeError", "flag_on-OSError"])
def test_r6_b_stop_hook_active_true_never_adds_additional_context_even_with_the_constant_on(
        site, current, home, monkeypatch, transcript):
    """Constant True + failed helper + stop_hook_active=true: stdout is byte-equal to the
    flag-OFF reference for that same payload (mature: empty; immature: the nudge block with no
    hookSpecificOutput), stderr carries the line. Mutant: drop the stop_hook_active gate in
    `_emit` -> red."""
    make_store(home, current)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "_REPORT_TO_AGENT", True)
    set_config(home, flag=False)
    fresh(mod)
    ref = run_main(mod, transcript, stop_hook_active=True)
    fresh(mod)
    set_config(home, flag=True)
    cls = _break_helper(site, mod, home, monkeypatch)
    run = run_main(mod, transcript, stop_hook_active=True)
    assert run.exc is None, run.exc
    assert run.stderr == _line(cls) + "\n"
    assert run.stdout == ref.stdout and "hookSpecificOutput" not in run.stdout
    assert bool(run.stdout) is (current < THRESHOLD)  # not vacuous


@pytest.mark.parametrize("kind", ["long", "unprintable"])
def test_r6_b_the_class_name_is_sanitised_and_capped_at_80(kind, home, monkeypatch, transcript):
    """A 100-char class name is cut to 80; a non-printable char in the name becomes `?`.
    Mutants: drop the `[:80]` / drop the sanitising -> the matching cell red."""
    make_store(home, THRESHOLD + 3)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "_REPORT_TO_AGENT", True)
    set_config(home, flag=True)
    name = ("L" * 100) if kind == "long" else "Bad\x07Name\x1b"
    exc_cls = type(name, (Exception,), {})

    def boom(key, path):
        raise exc_cls(SENTINEL)

    monkeypatch.setattr(eof, "flag_on", boom)
    run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    expected = ("L" * 80) if kind == "long" else "Bad?Name?"
    assert run.stderr == _line(expected) + "\n"
    assert json.loads(run.stdout)["hookSpecificOutput"]["additionalContext"] == _line(expected)


@pytest.mark.parametrize("current", [THRESHOLD - 3, THRESHOLD + 3], ids=["immature", "mature"])
@pytest.mark.parametrize("flag", [..., False, True], ids=["flag-absent", "flag-false", "flag-true-owner-up"])
def test_r6_b_a_healthy_helper_is_silent_and_stdout_is_today(flag, current, home, monkeypatch, transcript):
    """Flag absent / false / true with a healthy helper (and the owner up for `true`):
    nothing recorded, stdout and state equal the flag-OFF reference, no report text on
    either stream. Mutant: report unconditionally -> all red."""
    make_store(home, current)
    mod = load_hook(home, monkeypatch)
    monkeypatch.setattr(mod, "_REPORT_TO_AGENT", True)
    ref = _flag_off_reference(mod, home, transcript)
    set_config(home, flag=flag)
    if flag is True:
        with FakeOwner(home):
            run = run_main(mod, transcript)
    else:
        run = run_main(mod, transcript)
    assert run.exc is None, run.exc
    assert mod._OWNER_FLAGS_FAILURE is None
    assert (run.stdout, run.state) == (ref.stdout, ref.state)
    assert "owner flag helper failed" not in run.stdout + run.stderr
    assert "hookSpecificOutput" not in run.stdout


def test_r6_b_the_failure_record_is_reset_on_every_call(home, monkeypatch):
    """A failing call records the class; the next call with a healthy helper clears it.
    Mutant: never reset -> the stale class would be reported on a healthy run: red."""
    make_store(home, THRESHOLD + 3)
    set_config(home, flag=True)
    mod = load_hook(home, monkeypatch)
    real = eof.flag_on
    monkeypatch.setattr(eof, "flag_on", lambda k, p: (_ for _ in ()).throw(OSError("x")))
    assert mod._owner_reads_enabled() is False
    assert mod._OWNER_FLAGS_FAILURE == "OSError"
    monkeypatch.setattr(eof, "flag_on", real)
    mod._owner_reads_enabled()
    assert mod._OWNER_FLAGS_FAILURE is None
