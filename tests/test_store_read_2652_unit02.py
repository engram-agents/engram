"""#2652 unit 02 (part of #2652) -- tools/store_read.py: two typed reads of the live store (the person-node
fairy's uniform-sample id read, ledger row 1; first-session's seed-empty axiom count, row 42), through the engramd
owner behind `engramd.store_read_reads_via_owner` (default OFF), else a `mode=ro` open.

Contract (each cell written before the tool existed):

  C1  `ids --since <iso>` prints exactly the rows the row-1 SELECT selects, sorted: retracted, non-current,
      other types and rows before --since are absent
  C2  `ids --since ""` prints the whole qualifying population
  C3  `axiom-count` prints the axiom count (a store with 0 axioms and one with 3)
  C4  flag OFF: stderr names `path=direct-ro`, and the connection is `mode=ro` (a write through it fails)
  C5  flag ON, a fake owner answering: stderr names `path=owner`, the result comes from the owner (the owner saw
      the registered read), and the tool makes ZERO direct opens of the store
  C6  flag ON, ping Unavailable with reason `down`: falls back to `path=direct-ro (...)`, naming the reason
  C6b flag ON, the ping fails any other way (a rejection, garbage, every Unavailable reason but `down`): non-zero
      exit, `refused`, and ZERO direct opens of the store
  C7  a missing or non-store --db exits non-zero and names its class
  C8  neither document holds `sqlite3.connect(` any more, and both name the tool
  C9  both SELECTs are registered templates in the committed engramd_read_templates.json, sited at
      tools/store_read.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_owner import FakeOwner  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "engram"
TOOL = SRC / "tools" / "store_read.py"
TEMPLATES = SRC / "engramd_read_templates.json"
PERSON_DOC = SRC / "agents" / "claude" / "engram-person-node-fairy.md"
FIRST_SESSION_DOC = SRC / "skills" / "claude" / "engram-first-session" / "SKILL.md"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FLAG_KEY = "store_read_reads_via_owner"

IDS_SQL = (
    "SELECT id FROM nodes WHERE is_current = 1 AND status != 'retracted' "
    "AND type IN ('observation_factual', 'observation_predictive') AND created_at >= ?"
)
AXIOM_SQL = "SELECT COUNT(*) FROM nodes WHERE type='axiom'"

# (id, type, is_current, status, created_at)
ROWS = [
    ("obs_0001", "observation_factual", 1, "active", "2026-01-01T00:00:00"),
    ("obs_0002", "observation_predictive", 1, "active", "2026-03-01T00:00:00"),
    ("obs_0003", "observation_factual", 1, "active", "2026-06-01T00:00:00"),
    ("obs_0004", "observation_factual", 1, "retracted", "2026-06-02T00:00:00"),     # retracted
    ("obs_0005", "observation_factual", 0, "active", "2026-06-03T00:00:00"),        # not current
    ("drv_0001", "derivation", 1, "active", "2026-06-04T00:00:00"),                 # other type
    ("obs_0000", "observation_predictive", 1, "active", "2025-12-31T00:00:00"),     # earliest, sorts first
]
QUALIFYING = ["obs_0000", "obs_0001", "obs_0002", "obs_0003"]
SINCE = "2026-02-01T00:00:00"
QUALIFYING_SINCE = ["obs_0002", "obs_0003"]


def build_store(home: Path, axioms: int = 3, rows=ROWS) -> Path:
    """The columns the two reads touch, and nothing else (no sqlite_vec, no engram_core)."""
    db = home / "knowledge.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL, claim TEXT, "
                "is_current INTEGER DEFAULT 1, status TEXT DEFAULT 'active', created_at TEXT)")
    for r in rows:
        con.execute("INSERT INTO nodes (id, type, claim, is_current, status, created_at) VALUES (?, ?, 'c', ?, ?, ?)",
                    (r[0], r[1], r[2], r[3], r[4]))
    for i in range(axioms):
        con.execute("INSERT INTO nodes (id, type, claim, created_at) VALUES (?, 'axiom', 'a', '2026-01-01T00:00:00')",
                    ("ax_%04d" % (i + 1),))
    con.commit()
    con.close()
    return db


@pytest.fixture
def home(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="sr-", dir="/tmp")).resolve()
    monkeypatch.setenv("ENGRAM_HOME", str(d))
    monkeypatch.setenv("HOME", str(d))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def set_flag(home: Path, value=True):
    (home / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


# Every sqlite3.connect of the watched store made by the thread that is running the tool (the fake owner's own
# handler threads connect too and are not the tool).
_OPENS = []
_WATCH = {"store": None, "thread": None}


def _audit(event, args):
    if event == "sqlite3.connect" and _WATCH["store"] and threading.get_ident() == _WATCH["thread"]:
        target = str(args[0])
        bare = target[5:].split("?", 1)[0] if target.startswith("file:") else target
        from urllib.parse import unquote
        if os.path.realpath(unquote(bare)) == _WATCH["store"]:
            _OPENS.append(target)


sys.addaudithook(_audit)


def load_tool():
    spec = importlib.util.spec_from_file_location("store_read_2652_unit02", str(TOOL))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(home: Path, argv, capsys):
    """(exit code, stdout, stderr, direct opens of home/knowledge.db) of one in-process tool run."""
    mod = load_tool()
    del _OPENS[:]
    _WATCH["store"] = os.path.realpath(str(home / "knowledge.db"))
    _WATCH["thread"] = threading.get_ident()
    capsys.readouterr()
    try:
        try:
            rc = mod.main(list(argv))
        except SystemExit as exc:
            rc = exc.code
    finally:
        _WATCH["store"] = None
    out = capsys.readouterr()
    return (0 if rc is None else rc), out.out, out.err, list(_OPENS)


def stderr_lines(err):
    return [ln for ln in err.splitlines() if ln.startswith("store_read:")]


# ------------------------------------------------------------------------------------------ C1, C2, C3

def test_c1_ids_returns_exactly_the_selected_rows_sorted(home, capsys):
    build_store(home)
    rc, out, err, _ = run(home, ["ids", "--since", SINCE], capsys)
    assert rc == 0, err
    assert out.splitlines() == QUALIFYING_SINCE
    # the oracle: the row-1 SELECT run directly, sorted
    con = sqlite3.connect(str(home / "knowledge.db"))
    assert out.splitlines() == sorted(r[0] for r in con.execute(IDS_SQL, (SINCE,)))
    con.close()


def test_c2_ids_with_empty_since_is_the_whole_qualifying_population(home, capsys):
    build_store(home)
    rc, out, err, _ = run(home, ["ids", "--since", ""], capsys)
    assert rc == 0, err
    assert out.splitlines() == QUALIFYING


@pytest.mark.parametrize("axioms", [0, 3])
def test_c3_axiom_count_prints_the_fixtures_count(home, capsys, axioms):
    build_store(home, axioms=axioms)
    rc, out, err, _ = run(home, ["axiom-count"], capsys)
    assert rc == 0, err
    assert out.strip() == str(axioms)


# ------------------------------------------------------------------------------------------ C4

def test_c4_flag_off_reads_direct_ro_and_the_connection_cannot_write(home, capsys):
    db = build_store(home)
    rc, out, err, opens = run(home, ["ids", "--since", ""], capsys)
    assert rc == 0, err
    lines = stderr_lines(err)
    assert len(lines) == 1 and lines[0].startswith("store_read: path=direct-ro"), err
    assert len(opens) == 1 and "mode=ro" in opens[0], opens
    # the tool's own opener refuses a write
    mod = load_tool()
    con = mod._open_ro(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE scratch (x)")
        with pytest.raises(sqlite3.OperationalError):
            con.execute("UPDATE nodes SET claim = 'x'")
    finally:
        con.close()


def test_c4_axiom_count_flag_off_is_direct_ro_too(home, capsys):
    build_store(home)
    rc, out, err, opens = run(home, ["axiom-count"], capsys)
    assert rc == 0 and out.strip() == "3"
    assert stderr_lines(err)[0].startswith("store_read: path=direct-ro")
    assert len(opens) == 1 and "mode=ro" in opens[0]


# ------------------------------------------------------------------------------------------ C5

def test_c5_flag_on_with_an_owner_reads_through_the_owner_with_zero_direct_opens(home, capsys):
    build_store(home)
    set_flag(home)
    with FakeOwner(home) as owner:
        rc, out, err, opens = run(home, ["ids", "--since", SINCE], capsys)
        reads = [r for r in owner.requests if r.get("method") == "read"]
    assert rc == 0, err
    assert out.splitlines() == QUALIFYING_SINCE
    lines = stderr_lines(err)
    assert lines == ["store_read: path=owner"], err
    assert [r["params"]["sql"] for r in reads] == [IDS_SQL]
    assert [r["params"]["args"] for r in reads] == [[SINCE]]
    assert opens == [], "flag ON and a live owner: no direct open of the store"


def test_c5_axiom_count_through_the_owner(home, capsys):
    build_store(home, axioms=3)
    set_flag(home)
    with FakeOwner(home) as owner:
        rc, out, err, opens = run(home, ["axiom-count"], capsys)
        reads = [r for r in owner.requests if r.get("method") == "read"]
    assert rc == 0 and out.strip() == "3", err
    assert stderr_lines(err) == ["store_read: path=owner"]
    assert [r["params"]["sql"] for r in reads] == [AXIOM_SQL]
    assert opens == []


# ------------------------------------------------------------------------------------------ C6, C6b

def test_c6_flag_on_ping_down_falls_back_to_direct_ro_and_names_the_reason(home, capsys):
    build_store(home)
    set_flag(home)  # no owner is listening: the ping is Unavailable, reason `down`
    rc, out, err, opens = run(home, ["ids", "--since", ""], capsys)
    assert rc == 0, err
    assert out.splitlines() == QUALIFYING
    lines = stderr_lines(err)
    assert len(lines) == 1 and lines[0].startswith("store_read: path=direct-ro (") and "down" in lines[0], err
    assert len(opens) == 1


def _unavailable(reason):
    def make(client):
        return client.Unavailable(socket_path="/x/engramd.sock", bound=1.0, agent_steps=["retry"],
                                  human_steps=["restart"], detail="d", reason=reason)
    return make


def _in_proc_ping(monkeypatch, answer):
    import engramd_client as client
    monkeypatch.setattr(client, "read", lambda method, params, **kw: answer(client))


@pytest.mark.parametrize("reason", ["path_too_long", "timeout", "busy", "busy_or_stuck", "protocol", "local", "internal"])
def test_c6b_every_unavailable_reason_but_down_is_refused_with_zero_direct_opens(home, capsys, monkeypatch, reason):
    build_store(home)
    set_flag(home)
    _in_proc_ping(monkeypatch, _unavailable(reason))
    rc, out, err, opens = run(home, ["ids", "--since", ""], capsys)
    assert rc == 3 and out == "", (rc, out)
    lines = stderr_lines(err)
    assert len(lines) == 1 and lines[0].startswith("store_read: refused (") and reason in lines[0], err
    assert opens == [], "an owner that is present but %s is not 'no live owner': no direct open" % reason


def test_c6b_a_rejected_ping_is_refused_with_zero_direct_opens(home, capsys, monkeypatch):
    build_store(home)
    set_flag(home)
    _in_proc_ping(monkeypatch, lambda c: c.Rejected(reason="contract_mismatch", detail="old", remedy="restart it"))
    rc, out, err, opens = run(home, ["axiom-count"], capsys)
    assert rc == 3 and out == ""
    assert stderr_lines(err)[0].startswith("store_read: refused (") and "contract_mismatch" in err
    assert opens == []


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


def test_c6b_a_malformed_owner_answer_is_refused_with_zero_direct_opens(home, capsys):
    build_store(home)
    set_flag(home)
    with GarbageOwner(home):
        rc, out, err, opens = run(home, ["ids", "--since", ""], capsys)
    assert rc == 3 and out == ""
    assert stderr_lines(err)[0].startswith("store_read: refused (")
    assert opens == []


def test_c6b_the_store_path_is_never_checked_or_opened_when_refused(home, capsys, monkeypatch):
    """A spy on the path precheck: a refused run never reaches the direct path at all."""
    build_store(home)
    set_flag(home)
    _in_proc_ping(monkeypatch, _unavailable("timeout"))
    import tools._owner_read as owner_read
    seen = []
    real = owner_read.check_store_path
    monkeypatch.setattr(owner_read, "check_store_path", lambda p: (seen.append(p), real(p))[1])
    rc, _, err, opens = run(home, ["ids", "--since", ""], capsys)
    assert rc == 3 and seen == [] and opens == []


# ------------------------------------------------------------------------------------------ C7

def test_c7_a_missing_db_exits_nonzero_naming_the_class(home, capsys):
    rc, out, err, opens = run(home, ["axiom-count", "--db", str(home / "nope.db")], capsys)
    assert rc == 1 and out == ""
    assert "FileNotFoundError" in err and not (home / "nope.db").exists() and opens == []


def test_c7_a_directory_is_not_a_store(home, capsys):
    d = home / "adir"
    d.mkdir()
    rc, out, err, _ = run(home, ["ids", "--since", "", "--db", str(d)], capsys)
    assert rc == 1 and out == "" and "StoreNotAStore" in err


def test_c7_a_file_that_is_not_a_database_exits_nonzero_naming_the_class(home, capsys):
    bad = home / "bad.db"
    bad.write_text("this is not a database, just text, long enough to not be an empty file" * 4)
    rc, out, err, _ = run(home, ["axiom-count", "--db", str(bad)], capsys)
    assert rc == 1 and out == "" and "DatabaseError" in err


def test_c7_a_relative_engram_home_exits_2_and_opens_nothing(home, capsys, monkeypatch):
    """The documents teach exit 2 for a bad ENGRAM_HOME; pin it (a relative value is refused)."""
    build_store(home)
    monkeypatch.setenv("ENGRAM_HOME", "relative/engram-home")
    rc, out, err, opens = run(home, ["axiom-count"], capsys)
    assert rc == 2 and out == "" and opens == [], (rc, err)
    assert "ENGRAM_HOME must be an absolute path" in err


@pytest.mark.parametrize("answer", [[(True,)], [(False,)], [("3",)], [(3, 4)], [], [(3,), (4,)]])
def test_the_count_reader_refuses_any_answer_that_is_not_one_integer(answer):
    """A bool is an int in Python, so a `True` row would print as 1 without the guard."""
    class _Con:
        def execute(self, sql, params=()):
            class _Cur:
                def fetchall(_self):
                    return answer
            return _Cur()
    with pytest.raises(ValueError):
        load_tool().read_axiom_count(_Con())


def test_the_explicit_db_is_accepted(home, capsys):
    other = home / "other"
    other.mkdir()
    db = build_store(other, axioms=2)
    rc, out, err, _ = run(home, ["axiom-count", "--db", str(db)], capsys)
    assert rc == 0 and out.strip() == "2", err


def test_there_is_no_free_sql_subcommand():
    mod = load_tool()
    with pytest.raises(SystemExit):
        mod.main(["sql", "SELECT 1"])


# ------------------------------------------------------------------------------------------ C8, C9

def test_c8_neither_document_opens_the_store_itself_any_more_and_both_name_the_tool():
    for doc in (PERSON_DOC, FIRST_SESSION_DOC):
        text = doc.read_text(encoding="utf-8")
        assert "sqlite3.connect(" not in text, doc
        assert "store_read.py" in text, doc
    assert "store_read.py ids" in PERSON_DOC.read_text(encoding="utf-8")
    # the doc quotes the tool path, so the subcommand follows a closing quote
    assert 'store_read.py" axiom-count' in FIRST_SESSION_DOC.read_text(encoding="utf-8")


def test_c9_both_selects_are_registered_templates_sited_at_the_tool():
    templates = json.loads(TEMPLATES.read_text())["templates"]
    by_sql = {t["sql"]: t for t in templates}
    for sql in (IDS_SQL, AXIOM_SQL):
        assert sql in by_sql, "not registered: %s" % sql
        assert by_sql[sql]["sites"] and all(s.startswith("tools/store_read.py:") for s in by_sql[sql]["sites"]), by_sql[sql]
