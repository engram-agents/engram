"""engram-alpha#3824 (U12 of #2652) -- tools/reconcile_lesson_demotions.py reads through the owner.

The first `tools/` unit. Behind `engramd.reconcile_demotions_reads_via_owner` (default OFF) the
tool's ONE statement -- the per-ledgered-lesson `prevention_type` read -- goes to the engramd owner
instead of opening knowledge.db.

Contract (issue #3824, each cell red before the implementation):

  C1  differential at the read: both paths give the same per-id row AND the same candidate list on a
      fixture with a hot blocking lesson, a cold one, an advisory one, a superseded one, an archived
      one, a missing id and a non-JSON-metadata one (the #3485 case). The harness is shown to FAIL by
      mutating the NEW side: (a) the owner answers "advisory" for the hot blocking lesson -> read level
      AND candidate list red; (b) the owner drops `memory_status = 'active'` -> read level red, the
      candidate list CANNOT see it.
  C2  owner down is an error, never an empty list. The sleep cycle reads `[]` as "nothing to demote".
  C2b three owner-side refusals, three distinct messages (down / unregistered_template / refused_at_load):
      the remedies differ, so a catch-all "owner unavailable" fails.
  C3  zero direct opens of the store with the flag ON, empty allow-list; flag OFF still opens it once.
  C4  the template registers exactly the bytes the tool sends (sha256 pinned), under the site key
      `tools/reconcile_lesson_demotions.py:find_candidates`, in the source AND the flattened layout.
  C5  the flag is read only through engramd_owner_flags.flag_on; an explicit --db keeps the direct path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _fake_owner import FakeOwner, registered_sql
from _owner_platform import requires_linux_owner

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engramd_owner as eo  # noqa: E402

TOOL_PATH = REPO_ROOT / "tools" / "reconcile_lesson_demotions.py"
FLAG_KEY = "reconcile_demotions_reads_via_owner"
SITE = "tools/reconcile_lesson_demotions.py:find_candidates"
# The assembled bytes of the tool's one statement (implicit concatenation in find_candidates).
SQL = (
    "SELECT json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.prevention_type') "
    "FROM nodes WHERE id = ? AND type = 'lesson' AND is_current = 1 AND memory_status = 'active'"
)
SQL_SHA256 = "33b7113738f7310b25d3b9c6e109b18c3a790ba8c54879c25e8a774757703e3e"
NOW = 1_800_000_000.0
HOUR = 3600.0
DAY = 86400.0


@pytest.fixture(autouse=True)
def _restore_imports():
    """The tool imports engramd_client & co. from a runtime dir (a flattened tmp copy in some cells)."""
    mods, path = dict(sys.modules), list(sys.path)
    yield
    for name in [n for n in sys.modules if n not in mods]:
        del sys.modules[name]
    for name, mod in mods.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod
    sys.path[:] = path


@pytest.fixture
def home():
    d = Path(tempfile.mkdtemp(prefix="rd3824-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- the fixture store + ledger ---------------------------------------------------------------

LESSONS = {
    # id: (metadata, is_current, memory_status, ledger shape)
    "ls_hot": ({"prevention_type": "blocking"}, 1, "active", "hot"),
    "ls_cold": ({"prevention_type": "blocking"}, 1, "active", "cold"),
    "ls_adv": ({"prevention_type": "advisory"}, 1, "active", "hot"),
    "ls_sup": ({"prevention_type": "blocking"}, 0, "active", "hot"),
    "ls_arch": ({"prevention_type": "blocking"}, 1, "archived", "cold"),
    "ls_bad": ("{not json at all", 1, "active", "hot"),
    "ls_gone": None,  # ledgered, no row at all
}


def make_store(home: Path) -> Path:
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        # a corrupt blob must be insertable (the #3485 case)
        for trig in ("nodes_metadata_json_guard_insert", "nodes_metadata_json_guard_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        for lid, spec in LESSONS.items():
            if spec is None:
                continue
            meta, current, status, _ = spec
            conn.execute(
                "INSERT INTO nodes (id, type, claim, created_at, metadata, is_current, memory_status) "
                "VALUES (?, 'lesson', ?, '2026-01-01T00:00:00', ?, ?, ?)",
                (lid, f"claim {lid}", meta if isinstance(meta, str) else json.dumps(meta), current, status),
            )
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def make_ledger(home: Path) -> Path:
    path = home / "lesson-prevention-ledger.jsonl"
    entries = []
    for lid, spec in LESSONS.items():
        shape = "hot" if spec is None else spec[3]
        age = (1800.0, 3600.0) if shape == "hot" else (40 * DAY, 41 * DAY)
        for a in age:
            entries.append({"ts": NOW - a, "lesson_id": lid, "mode": "blocking", "override_present": True})
        entries.append({"ts": NOW - age[0], "lesson_id": lid, "mode": "auto_demoted_lease",
                        "override_count": 2, "threshold": 2, "window_days": 7})
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def set_flag(home: Path, value=True, key: str = FLAG_KEY) -> None:
    (home / "config.json").write_text(json.dumps({"engramd": {key: value}}), encoding="utf-8")


def load_tool(home: Path, monkeypatch, path: Path = TOOL_PATH):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("reconcile_lesson_demotions_3824", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seat(home, monkeypatch):
    make_store(home)
    make_ledger(home)
    return home


# ---- the recording seam ---------------------------------------------------------------------

class _Cur:
    def __init__(self, inner, rows, lid):
        self._inner, self._rows, self._lid = inner, rows, lid

    def fetchone(self):
        row = self._inner.fetchone()
        self._rows[self._lid] = None if row is None else tuple(row)
        return row


class _Spy:
    def __init__(self, inner, rows):
        self._inner, self._rows = inner, rows

    def execute(self, sql, params=()):
        assert sql == SQL, "the tool sent a different statement than the registered one"
        return _Cur(self._inner.execute(sql, params), self._rows, params[0])

    def close(self):
        return self._inner.close()


def run_find(mod, monkeypatch, **kw):
    """-> (per-id rows as the tool saw them, candidate list)."""
    rows: dict = {}
    real = mod._open_reader
    monkeypatch.setattr(mod, "_open_reader", lambda *a, **k: _Spy(real(*a, **k), rows))
    return rows, mod.find_candidates(now=NOW, **kw)


class MutatedOwner(FakeOwner):
    """A faulty NEW side: `how` picks the mutation."""

    def __init__(self, home, how):
        super().__init__(home)
        self.how = how

    def _answer(self, request):
        ans = super()._answer(request)
        params = request.get("params") or {}
        if request.get("method") != "read" or params.get("sql") != SQL:
            return ans
        if self.how == "advisory_for_hot" and params.get("args") == ["ls_hot"]:
            return {"status": "ok", "result": [["advisory"]]}
        if self.how == "drops_memory_status":
            db = self.home / "knowledge.db"
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                loose = SQL.replace(" AND memory_status = 'active'", "")
                return {"status": "ok", "result": [list(r) for r in conn.execute(loose, tuple(params["args"]))]}
            finally:
                conn.close()
        return ans


# ---- C1: differential at the read ----------------------------------------------------------

def _both_paths(seat, monkeypatch, owner_factory):
    set_flag(seat, False)  # same config file on both legs: only the flag value differs
    mod = load_tool(seat, monkeypatch)
    direct = run_find(mod, monkeypatch)
    set_flag(seat)
    mod_on = load_tool(seat, monkeypatch)
    with owner_factory(seat):
        owner = run_find(mod_on, monkeypatch)
    return direct, owner


def test_c1_both_paths_agree_per_id_and_on_the_candidate_list(seat, monkeypatch):
    (rows_d, cand_d), (rows_o, cand_o) = _both_paths(seat, monkeypatch, FakeOwner)
    assert [c["lesson_id"] for c in cand_d] == ["ls_hot"], cand_d  # the fixture really has one candidate
    assert rows_d == {  # and the per-id reads are the interesting ones
        "ls_hot": ("blocking",), "ls_cold": ("blocking",), "ls_adv": ("advisory",), "ls_sup": None,
        "ls_arch": None, "ls_bad": (None,), "ls_gone": None,
    }, rows_d
    assert rows_o == rows_d
    assert cand_o == cand_d


def test_c1_control_owner_answers_advisory_for_the_hot_lesson_reddens_both_levels(seat, monkeypatch):
    (rows_d, cand_d), (rows_o, cand_o) = _both_paths(
        seat, monkeypatch, lambda h: MutatedOwner(h, "advisory_for_hot"))
    assert rows_o != rows_d and rows_o["ls_hot"] == ("advisory",)
    assert cand_o != cand_d and cand_o == []


def test_c1_control_owner_dropping_memory_status_is_invisible_to_the_candidate_list(seat, monkeypatch):
    """The second mutation the list cannot see: an archived blocking lesson with a COLD lease reads as
    `blocking` through a loose statement, is dropped by the lease check, and the candidate lists agree.
    Only the per-id read level catches it."""
    (rows_d, cand_d), (rows_o, cand_o) = _both_paths(
        seat, monkeypatch, lambda h: MutatedOwner(h, "drops_memory_status"))
    assert cand_o == cand_d, "this mutation must NOT be visible at the list level"
    assert rows_o != rows_d and rows_o["ls_arch"] == ("blocking",) and rows_d["ls_arch"] is None


# ---- C2: owner down is an error -------------------------------------------------------------

def test_c2_flag_on_owner_down_raises_reconcile_error_never_an_empty_list(seat, monkeypatch):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    with pytest.raises(mod.ReconcileError) as exc:
        mod.find_candidates(now=NOW)
    assert "owner" in str(exc.value).lower()


def test_c2_flag_on_empty_ledger_with_the_owner_down_is_still_an_error(seat, monkeypatch):
    """Parity with today's missing-knowledge.db error: with the flag ON the owner's answer replaces the
    `db_path.exists()` precondition, so a night with nothing ledgered still cannot pass without an owner."""
    set_flag(seat)
    (seat / "lesson-prevention-ledger.jsonl").unlink()
    mod = load_tool(seat, monkeypatch)
    with pytest.raises(mod.ReconcileError):
        mod.find_candidates(now=NOW)


def test_c2_flag_on_empty_ledger_with_the_owner_up_is_an_empty_list(seat, monkeypatch):
    set_flag(seat)
    (seat / "lesson-prevention-ledger.jsonl").unlink()
    mod = load_tool(seat, monkeypatch)
    with FakeOwner(seat) as owner:
        assert mod.find_candidates(now=NOW) == []
    assert [r["method"] for r in owner.requests] == ["ping"], "no read is sent when nothing is ledgered"


def test_c2_cli_owner_down_exits_nonzero_with_the_error_shape(seat):
    set_flag(seat)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(seat)
    r = subprocess.run([sys.executable, str(TOOL_PATH), "--now", str(NOW)], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    out = json.loads(r.stdout)
    assert out["candidates"] is None and "owner" in out["error"].lower()


def _message_for(seat, monkeypatch, scenario):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    owner = FakeOwner(seat)
    if scenario == "unregistered":
        owner._registered = registered_sql() - {SQL}
    elif scenario == "refused_at_load":
        owner._answer = lambda request: (
            {"status": "ok", "result": "pong"} if request.get("method") == "ping" else
            {"status": "rejected", "reason": "refused_at_load", "detail": "no such column: memory_status",
             "remedy": "migrate the store and restart the owner"})
    if scenario == "down":
        with pytest.raises(mod.ReconcileError) as exc:
            mod.find_candidates(now=NOW)
        return str(exc.value)
    with owner:
        with pytest.raises(mod.ReconcileError) as exc:
            mod.find_candidates(now=NOW)
    return str(exc.value)


def test_c2b_three_refusals_three_distinct_remedies(seat, monkeypatch):
    down = _message_for(seat, monkeypatch, "down")
    skew = _message_for(seat, monkeypatch, "unregistered")
    schema = _message_for(seat, monkeypatch, "refused_at_load")
    assert len({down, skew, schema}) == 3
    # (i) down: start it
    assert re.search(r"\bstart the owner", down) and "unregistered_template" not in down and "refused_at_load" not in down
    # (ii) version skew: the running owner is older than the tool -> restart it on the new build
    assert "unregistered_template" in skew and "older" in skew and "restart the owner" in skew
    assert not re.search(r"\bstart the owner", skew)  # "restart the owner" is the other remedy
    # (iii) schema skew: a restart will NOT fix it
    assert "refused_at_load" in schema and "will not fix" in schema and "migrate" in schema
    assert "restart the owner on" not in schema and "older" not in schema


def test_c2b_other_owner_failures_also_raise(seat, monkeypatch):
    """Any owner answer that is not rows is an error: a refusal of another reason, or a reply that is
    not a list of rows. Never a silent `[]`."""
    set_flag(seat)
    for answer in ({"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"},
                   {"status": "ok", "result": "not-a-list"}):
        mod = load_tool(seat, monkeypatch)
        owner = FakeOwner(seat)
        owner._answer = lambda request, a=answer: (
            {"status": "ok", "result": "pong"} if request.get("method") == "ping" else a)
        with owner:
            with pytest.raises(mod.ReconcileError):
                mod.find_candidates(now=NOW)


def test_c2_flag_on_without_the_client_module_raises_never_reads_directly(seat, monkeypatch):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    monkeypatch.setitem(sys.modules, "engramd_client", None)  # `import engramd_client` -> ImportError
    with FakeOwner(seat):
        with pytest.raises(mod.ReconcileError):
            mod.find_candidates(now=NOW)


# ---- C3: zero direct opens -----------------------------------------------------------------

class Opens:
    """Raise-and-record every way THIS thread could open a sqlite store; the in-process fake owner
    serves from other threads and keeps its own connections."""

    def __init__(self):
        self.record = []
        self._thread = threading.get_ident()

    def install(self, monkeypatch):
        real = {"connect": sqlite3.connect, "dbapi2": sqlite3.dbapi2.connect}

        def guard(name):
            def f(*a, **k):
                if threading.get_ident() != self._thread:
                    return real[name](*a, **k)
                self.record.append((name, str(a[0]) if a else ""))
                raise AssertionError(f"direct open via {name}: {a[:1]}")
            return f

        monkeypatch.setattr(sqlite3, "connect", guard("connect"))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", guard("dbapi2"))
        return self


def test_c3_flag_on_a_full_run_opens_the_store_zero_times(seat, monkeypatch):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with FakeOwner(seat) as owner:
        got = mod.find_candidates(now=NOW)
    assert [c["lesson_id"] for c in got] == ["ls_hot"]
    assert opens.record == [], opens.record
    assert len([r for r in owner.requests if r["method"] == "read"]) == len(LESSONS)


def test_c3_flag_on_never_stats_the_local_store_file(seat, monkeypatch):
    """The `db_path.exists()` precondition is replaced by the owner's answer under the flag: the tool
    must not even look at the file (a seat whose owner serves a store the tool cannot see is fine)."""
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    store = str((seat / "knowledge.db").resolve())
    real_exists = Path.exists

    def exists(self, *a, **k):
        if str(self).endswith("knowledge.db") or str(self.resolve()) == store:
            raise AssertionError(f"the tool stat the local store: {self}")
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", exists)
    with FakeOwner(seat):
        assert [c["lesson_id"] for c in mod.find_candidates(now=NOW)] == ["ls_hot"]


def test_c3_control_flag_off_still_opens_the_store_exactly_once(seat, monkeypatch):
    mod = load_tool(seat, monkeypatch)
    opens = []
    real = sqlite3.connect

    def counting(*a, **k):
        opens.append(str(a[0]))
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting)
    assert [c["lesson_id"] for c in mod.find_candidates(now=NOW)] == ["ls_hot"]
    assert len(opens) == 1 and "knowledge.db" in opens[0], opens


# ---- C4: the template is exactly the bytes the tool sends ----------------------------------

def test_c4_the_statement_constant_hash_is_pinned():
    assert hashlib.sha256(SQL.encode("utf-8")).hexdigest() == SQL_SHA256


def test_c4_the_committed_template_is_registered_under_the_tool_site():
    doc = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    hits = [t for t in doc["templates"] if t["sql"] == SQL]
    assert len(hits) == 1, "the statement must be registered exactly once"
    assert hits[0]["key"] == SQL_SHA256
    assert hits[0]["sites"] == [SITE]


def test_c4_the_tool_sends_exactly_the_registered_bytes(seat, monkeypatch):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    with FakeOwner(seat) as owner:
        mod.find_candidates(now=NOW)
    sent = {r["params"]["sql"] for r in owner.requests if r["method"] == "read"}
    assert sent == {SQL}, sent
    assert hashlib.sha256(next(iter(sent)).encode()).hexdigest() == SQL_SHA256
    assert SQL in registered_sql()


def _flattened(tmp: Path) -> Path:
    """The deployed layout: runtime modules at the root, hooks/*.py flat, tools/ at the root."""
    root = tmp / "engram-plugin"
    (root / "hooks").mkdir(parents=True)
    for p in (REPO_ROOT / "src" / "engram" / "hooks" / "claude").glob("*.py"):
        shutil.copy(p, root / "hooks" / p.name)
    for p in (REPO_ROOT / "src" / "engram").glob("*.py"):
        shutil.copy(p, root / p.name)
    shutil.copytree(REPO_ROOT / "src" / "engram" / "tools", root / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return root


@pytest.mark.parametrize("layout", ["source", "flattened"])
def test_c4_cli_with_the_flag_on_works_in_both_layouts(layout, seat, tmp_path):
    set_flag(seat)
    tool = TOOL_PATH if layout == "source" else _flattened(tmp_path) / "tools" / "reconcile_lesson_demotions.py"
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(seat)
    with FakeOwner(seat) as owner:
        r = subprocess.run([sys.executable, str(tool), "--now", str(NOW)], capture_output=True, text=True,
                           env=env, timeout=60)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert [c["lesson_id"] for c in json.loads(r.stdout)["candidates"]] == ["ls_hot"]
    assert any(q["method"] == "read" for q in owner.requests), "the flag-ON run never reached the owner"


# ---- C5: the flag ------------------------------------------------------------------------

@pytest.mark.parametrize("value", [False, "true", 1, None, "yes"])
def test_c5_only_the_json_boolean_true_turns_the_flag_on(seat, monkeypatch, value):
    set_flag(seat, value)
    mod = load_tool(seat, monkeypatch)
    opens = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (opens.append(a[0]), real(*a, **k))[1])
    assert [c["lesson_id"] for c in mod.find_candidates(now=NOW)] == ["ls_hot"]  # no owner running
    assert len(opens) == 1


def test_c5_an_explicit_db_keeps_the_direct_path_even_with_the_flag_on(seat, monkeypatch):
    """The owner serves ONE store (this home's). A caller naming another --db asked for exactly that
    file; reading the home store through the owner instead would answer a different question."""
    set_flag(seat)
    other = seat / "other"
    other.mkdir()
    shutil.copy(seat / "knowledge.db", other / "k.db")
    mod = load_tool(seat, monkeypatch)
    got = mod.find_candidates(db_path=other / "k.db", now=NOW)  # no owner running: must not need one
    assert [c["lesson_id"] for c in got] == ["ls_hot"]


def test_c5_a_symlinked_db_that_resolves_to_this_homes_store_still_uses_the_owner(seat, monkeypatch):
    """`--db` is compared by REAL path, not by string: a symlink pointing at this home's store is this
    home's store, so the flag applies (a plain string compare would send it down the direct path)."""
    set_flag(seat)
    link = seat / "store-link.db"
    link.symlink_to(seat / "knowledge.db")
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with FakeOwner(seat) as owner:
        got = mod.find_candidates(db_path=link, now=NOW)
    assert [c["lesson_id"] for c in got] == ["ls_hot"]
    assert opens.record == [] and any(r["method"] == "read" for r in owner.requests)


def test_c2_a_raising_client_is_a_reconcile_error_too(seat, monkeypatch):
    """client.read never raises by contract; if it ever does, the tool still raises its own error
    (never a bare traceback, never a silent empty list)."""
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    client = mod._import_runtime("engramd_client")
    monkeypatch.setattr(client, "read", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(mod.ReconcileError) as exc:
        mod.find_candidates(now=NOW)
    assert "ping failed" in str(exc.value) and "RuntimeError" in str(exc.value)


# ---- live owner (Linux and supported Mac) --------------------------------------------------

@requires_linux_owner
def test_live_real_owner_agrees_with_the_direct_path(seat, monkeypatch):
    set_flag(seat, False)
    mod = load_tool(seat, monkeypatch)
    direct = mod.find_candidates(now=NOW)
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    owner = eo.EngramdOwner(seat, request_deadline=2.0)
    owner.start(install_signal_handlers=False)
    try:
        via_owner = mod.find_candidates(now=NOW)
    finally:
        owner.stop()
    assert via_owner == direct and [c["lesson_id"] for c in via_owner] == ["ls_hot"]
