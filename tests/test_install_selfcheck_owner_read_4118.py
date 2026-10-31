"""#4118 (part of #2652) -- tools/install_selfcheck.py reads the graph through the engramd owner when a
live owner exists, and opens it read-only itself when none does, saying which path it took.

Design-seat ruling, both phases: live owner -> read through it. No live owner -> open `mode=ro` directly.
Either way print the path taken. With the flag ON and a live owner, a direct open is the red case.

Contract (each cell written before the implementation):

  a   pre-first-session, no owner: direct open, the seed check PASSes, the path is named, and the main
      file, the -wal and the -journal are byte-identical afterwards (an absent -wal reads as empty; a -shm is
      not compared)
  b   installed, flag ON, a REAL scratch owner: the graph checks PASS with `path: owner` and the tool's own
      process makes ZERO direct opens of the store (a sqlite3.connect audit hook counts them; a flag-OFF run
      of the same fixture is the positive control: the instrument does count a direct open)
  b2  the three graph statements are registered: each is answered by a real owner and equals the direct answer
  c   installed, flag ON, no owner (`down`): the ruled fallback, `path: direct (no live owner: down)`, PASS,
      nothing written; a store path that is a FIFO is refused, not hung on
  d   flag ON, an owner that answers but is not healthy (Rejected, or garbage): FAIL naming the owner's
      answer, ZERO direct opens
  d2  an owner read that fails after a good ping: FAIL, ZERO direct opens, no direct retry
  d3  every Unavailable reason but `down` (timeout, busy, busy_or_stuck, protocol, local, internal), and a
      Rejected: FAIL, ZERO direct opens; `down` is the one reason that falls back
  e2  flag OFF: the SQL the direct seed-id read executes is dev's `IN (?, ...)` statement, not json_each
  e   flag OFF: the lines are dev's lines, plus only the path note
  f   one decision per run: one ping, and every graph check reports the same path
  j   `--json` carries a per-check `path` field on the graph checks and keeps the existing keys
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_owner import FakeOwner  # noqa: E402
from _owner_platform import requires_supported_owner  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
TOOL = SRC / "tools" / "install_selfcheck.py"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import engram_core  # noqa: E402
import engramd_owner as eo  # noqa: E402

FLAG_KEY = "install_selfcheck_reads_via_owner"
SEED_NODES = [
    ("ax_0001", "axiom"), ("ax_0002", "axiom"), ("ax_0003", "axiom"),
    ("df_0001", "definition"), ("df_0002", "definition"), ("gl_0001", "goal"),
]
SEED_IDS = {"AX_HONESTY": "ax_0001", "AX_PROVENANCE": "ax_0002", "AX_DISCRETION": "ax_0003",
            "DF_ENGRAM": "df_0001", "DF_EPISTEMIC": "df_0002", "GL_HUMILITY": "gl_0001"}

NOTE_OFF = "(path: direct (flag off))"
NOTE_OWNER = "(path: owner)"
NOTE_DOWN = "(path: direct (no live owner: down))"

# The tool, run in its own process with an audit hook counting every sqlite3.connect of the store.
RUNNER = textwrap.dedent('''
    import importlib.util, json, os, sys
    from pathlib import Path
    from urllib.parse import unquote
    store = os.path.realpath(sys.argv[3])
    opens = []
    def hook(event, args):
        if event == "sqlite3.connect":
            target = str(args[0])
            if target.startswith("file:"):
                target = unquote(target[5:].split("?", 1)[0])
            if os.path.realpath(target) == store:
                opens.append(target)
    sys.addaudithook(hook)
    spec = importlib.util.spec_from_file_location("install_selfcheck_4118", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = Path(sys.argv[2])
    ctx = mod.Ctx(sys.argv[4], home, home, home / "claude", 5)
    names = sys.argv[5].split(",")
    checks = [(n, f) for n, f in mod.PHASE_CHECKS[sys.argv[4]] if n in names]
    results = mod.run_checks(checks, ctx)
    print(json.dumps({"results": results, "lines": [mod.format_line(r) for r in results], "opens": opens}))
''')

INSTALLED_GRAPH = "graph-opens,seed-ids-present"
PRE_GRAPH = "seed-graph"


def build_store(home: Path, nodes=SEED_NODES, wal=False) -> Path:
    db = home / "knowledge.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    engram_core._run_db_one_time_setup(conn)
    conn.row_factory = None
    for nid, ntype in nodes:
        conn.execute("INSERT INTO nodes (id, type, claim, created_at) VALUES (?, ?, 'a claim', '2026-01-01T00:00:00')",
                     (nid, ntype))
    conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at) VALUES "
                 "('df_0001', 'ax_0001', 'supports', '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at) VALUES "
                 "('gl_0001', 'ax_0001', 'supports', '2026-01-01T00:00:00')")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=%s" % ("WAL" if wal else "DELETE"))
    conn.close()
    (home / "seed-manifest.json").write_text(json.dumps({"seed_ids": SEED_IDS}))
    return db


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="isc-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def set_flag(home: Path, value=True):
    (home / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


def run_tool(home: Path, phase="installed", checks=INSTALLED_GRAPH, tool=TOOL):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    r = subprocess.run([sys.executable, "-c", RUNNER, str(tool), str(home), str(home / "knowledge.db"), phase, checks],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, (r.stdout, r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


def fingerprint(home: Path) -> dict:
    """sha256 of the main file, the -wal and the -journal. An absent -wal reads as an empty one: a read-only
    open of a WAL-mode store may leave an EMPTY -wal behind (and a -shm, which is not compared), which is not a
    write; any byte in either is."""
    out = {}
    for suffix in ("", "-wal", "-journal"):
        p = Path(str(home / "knowledge.db") + suffix)
        data = p.read_bytes() if p.exists() else b""
        out[suffix] = hashlib.sha256(data).hexdigest()
    return out


@contextlib.contextmanager
def real_owner(home: Path):
    owner = eo.EngramdOwner(home, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    try:
        yield owner
    finally:
        try:
            owner.stop()
        except Exception:
            pass


def by_name(out, name):
    return next(r for r in out["results"] if r["name"] == name)


class PingRejectedOwner(FakeOwner):
    def _answer(self, request):
        if request.get("method") == "ping":
            return {"status": "rejected", "reason": "contract_mismatch", "detail": "owner is older", "remedy": "restart it"}
        return super()._answer(request)


class GarbageOwner(FakeOwner):
    """Answers every request with a line that is not a protocol answer."""

    def _handle(self, conn):
        try:
            conn.settimeout(2)
            conn.recv(65536)
            conn.sendall(b"this is not json\n")
        except OSError:
            pass
        finally:
            conn.close()


class ReadFailsOwner(FakeOwner):
    """Ping is fine; every read is refused."""

    def _answer(self, request):
        if request.get("method") == "read":
            return {"status": "rejected", "reason": "read_failed", "detail": "OperationalError: disk I/O error",
                    "remedy": "see the owner log"}
        return super()._answer(request)


class PingCountingOwner(FakeOwner):
    def pings(self):
        return sum(1 for r in self.requests if r.get("method") == "ping")


# --------------------------------------------------------------------------------------- a

def test_a_pre_first_session_no_owner_opens_directly_names_the_path_and_writes_nothing(home):
    build_store(home, nodes=SEED_NODES)
    before = fingerprint(home)
    out = run_tool(home, phase="pre-first-session", checks=PRE_GRAPH)
    r = by_name(out, "seed-graph")
    assert r["status"] == "PASS", r
    assert NOTE_OFF in r["detail"], r
    assert out["opens"], "the direct path opened the store"
    assert fingerprint(home) == before


def test_a_pre_first_session_wal_store_main_file_and_wal_unchanged(home):
    build_store(home, nodes=SEED_NODES, wal=True)
    before = fingerprint(home)
    out = run_tool(home, phase="pre-first-session", checks=PRE_GRAPH)
    assert by_name(out, "seed-graph")["status"] == "PASS"
    after = fingerprint(home)
    assert after[""] == before[""] and after["-wal"] == before["-wal"], (before, after)


# --------------------------------------------------------------------------------------- b

@requires_supported_owner
def test_b_live_owner_flag_on_reads_through_the_owner_with_zero_direct_opens(home):
    build_store(home)
    set_flag(home)
    with real_owner(home):
        out = run_tool(home)
    assert by_name(out, "graph-opens")["status"] == "PASS", out
    assert NOTE_OWNER in by_name(out, "graph-opens")["detail"]
    assert by_name(out, "seed-ids-present")["status"] == "PASS", out
    assert out["opens"] == [], out["opens"]


def test_b_positive_control_the_audit_instrument_counts_a_direct_open(home):
    build_store(home)
    out = run_tool(home)  # flag off: the direct path
    assert len(out["opens"]) >= 1, "the instrument must be able to see a direct open"


@requires_supported_owner
def test_b2_the_three_graph_statements_are_registered_and_the_owner_answers_them(home):
    build_store(home)
    set_flag(home)
    import importlib.util
    import engramd_client as client
    spec = importlib.util.spec_from_file_location("install_selfcheck_4118_b2", str(TOOL))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ids = sorted(SEED_IDS.values())
    direct = sqlite3.connect(str(home / "knowledge.db"))
    try:
        want_counts = mod._read_counts(direct)
        want_ids = mod._read_present_ids_direct(direct, ids)
    finally:
        direct.close()
    with real_owner(home):
        conn = client.OwnerReadConnection(30, 10, socket_path=client.socket_path_for_home(home))
        try:
            assert mod._read_counts(conn) == want_counts
            assert mod._read_present_ids(conn, ids) == want_ids
        finally:
            conn.close()
    assert want_counts[1] == 2 and want_ids == set(ids)


# --------------------------------------------------------------------------------------- c

def test_c_flag_on_no_owner_falls_back_names_down_and_writes_nothing(home):
    build_store(home)
    set_flag(home)
    before = fingerprint(home)
    out = run_tool(home)
    assert by_name(out, "graph-opens")["status"] == "PASS", out
    assert NOTE_DOWN in by_name(out, "graph-opens")["detail"], out
    assert by_name(out, "seed-ids-present")["status"] == "PASS", out
    assert out["opens"], "the fallback opened the store directly"
    assert fingerprint(home) == before


def test_c_fallback_refuses_a_fifo_store_instead_of_hanging(home):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo")
    os.mkfifo(str(home / "knowledge.db"))
    (home / "seed-manifest.json").write_text(json.dumps({"seed_ids": SEED_IDS}))
    set_flag(home)
    out = run_tool(home)  # the subprocess timeout (120 s) is the hang detector
    r = by_name(out, "graph-opens")
    assert r["status"] == "FAIL", r
    assert out["opens"] == []


# --------------------------------------------------------------------------------------- d

def _unhealthy(owner_cls, home, needle):
    build_store(home)
    set_flag(home)
    with owner_cls(home):
        out = run_tool(home)
    for name in ("graph-opens", "seed-ids-present"):
        r = by_name(out, name)
        assert r["status"] == "FAIL", r
    assert needle in by_name(out, "graph-opens")["detail"], by_name(out, "graph-opens")
    assert out["opens"] == [], "an owner is present but unhealthy: no direct open beside it"
    return out


def test_d_owner_rejects_the_ping_fails_naming_the_answer_with_zero_direct_opens(home):
    _unhealthy(PingRejectedOwner, home, "contract_mismatch")


def test_d_owner_answers_garbage_is_not_taken_for_no_owner(home):
    out = _unhealthy(GarbageOwner, home, "protocol")
    assert "(path: direct" not in by_name(out, "graph-opens")["detail"]


def test_d2_owner_read_fails_after_a_good_ping_fails_with_no_direct_retry(home):
    out = _unhealthy(ReadFailsOwner, home, "read_failed")
    assert out["opens"] == []


# --------------------------------------------------------------------------------------- e

def test_e_flag_off_lines_are_devs_lines_plus_only_the_path_note(home):
    build_store(home)
    out = run_tool(home)
    assert out["lines"] == [
        "PASS graph-opens: 6 nodes " + NOTE_OFF,
        "PASS seed-ids-present: 6 seed ids",
    ]
    pre = Path(tempfile.mkdtemp(prefix="isc-", dir="/tmp"))
    try:
        build_store(pre)
        out = run_tool(pre, phase="pre-first-session", checks=PRE_GRAPH)
        assert out["lines"] == ["PASS seed-graph: 3 axioms, 2 definitions, 1 goal, 2 edges " + NOTE_OFF]
    finally:
        shutil.rmtree(pre, ignore_errors=True)


def test_e_flag_off_failure_wording_is_devs(home):
    build_store(home, nodes=SEED_NODES[:3])
    out = run_tool(home)
    assert by_name(out, "graph-opens")["detail"].startswith("the graph has 3 nodes, expected at least 6 — run bootstrap.py")


# --------------------------------------------------------------------------------------- f, j

def test_f_one_decision_per_run_one_ping_and_one_path_for_all_graph_checks(home):
    build_store(home)
    set_flag(home)
    with PingCountingOwner(home) as owner:
        out = run_tool(home)
        pings = owner.pings()
    assert by_name(out, "graph-opens")["status"] == "PASS", out
    assert pings == 1, "the path is decided once per run, not once per check (%d pings)" % pings
    paths = {r["path"] for r in out["results"]}
    assert paths == {"owner"}, paths


def test_j_json_carries_a_path_field_on_graph_checks_and_keeps_the_existing_keys(home):
    build_store(home)
    out = run_tool(home)
    for r in out["results"]:
        assert {"name", "status", "detail", "path"} <= set(r), r
        assert r["path"] == "direct (flag off)"


# --------------------------------------------------------------------------------------- d3, e2

_OPENS = []
_WATCH = [None]


def _audit(event, args):
    if event == "sqlite3.connect" and _WATCH[0]:
        from urllib.parse import unquote
        target = str(args[0])
        if target.startswith("file:"):
            target = unquote(target[5:].split("?", 1)[0])
        if os.path.realpath(target) == _WATCH[0]:
            _OPENS.append(target)


sys.addaudithook(_audit)


def _load_tool(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, str(TOOL))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_in_process(home, monkeypatch, ping_answer):
    """The installed graph checks, in this process, with the client's ping answering `ping_answer`."""
    import engramd_client as client
    monkeypatch.setattr(client, "read", lambda method, params, **kw: ping_answer(client))
    mod = _load_tool("install_selfcheck_4118_inproc")
    ctx = mod.Ctx("installed", home, home, home / "claude", 5)
    del _OPENS[:]
    _WATCH[0] = os.path.realpath(str(home / "knowledge.db"))
    try:
        checks = [(n, f) for n, f in mod.PHASE_CHECKS["installed"] if n in INSTALLED_GRAPH.split(",")]
        return mod.run_checks(checks, ctx), list(_OPENS)
    finally:
        _WATCH[0] = None


def _unavailable(reason):
    def make(client):
        return client.Unavailable(socket_path="/x/engramd.sock", bound=1.0, agent_steps=["retry"], human_steps=["restart"],
                                  detail="d", reason=reason)
    return make


@pytest.mark.parametrize("reason", ["timeout", "busy", "busy_or_stuck", "protocol", "local", "internal"])
def test_d3_every_unavailable_reason_but_down_fails_with_zero_direct_opens(home, monkeypatch, reason):
    build_store(home)
    set_flag(home)
    results, opens = _run_in_process(home, monkeypatch, _unavailable(reason))
    for r in results:
        assert r["status"] == "FAIL", r
    assert reason in results[0]["detail"], results[0]
    assert opens == [], "an owner that is present but %s is not 'no live owner': no direct open" % reason


def test_d3_a_rejected_ping_fails_with_zero_direct_opens(home, monkeypatch):
    build_store(home)
    set_flag(home)
    results, opens = _run_in_process(home, monkeypatch, lambda c: c.Rejected(reason="contract_mismatch", detail="old", remedy="restart"))
    assert [r["status"] for r in results] == ["FAIL", "FAIL"]
    assert opens == []


def test_d3_control_down_is_the_one_reason_that_falls_back(home, monkeypatch):
    build_store(home)
    set_flag(home)
    results, opens = _run_in_process(home, monkeypatch, _unavailable("down"))
    assert [r["status"] for r in results] == ["PASS", "PASS"], results
    assert NOTE_DOWN in results[0]["detail"] and opens


def test_e2_flag_off_direct_seed_id_read_runs_devs_in_statement_not_json_each(home, monkeypatch):
    build_store(home)
    mod = _load_tool("install_selfcheck_4118_trace")
    seen = []
    real_open = mod._open_ro

    def traced(ctx):
        con = real_open(ctx)
        con.set_trace_callback(seen.append)
        return con

    monkeypatch.setattr(mod, "_open_ro", traced)
    ctx = mod.Ctx("installed", home, home, home / "claude", 5)
    status, _ = mod.check_seed_ids_present(ctx)
    assert status == "PASS"
    ids = list(SEED_IDS.values())
    dev = "SELECT id FROM nodes WHERE id IN (%s)" % ",".join("'%s'" % i for i in ids)  # sqlite expands the bound ids
    assert dev in seen, seen
    assert not any("json_each" in q for q in seen), seen
