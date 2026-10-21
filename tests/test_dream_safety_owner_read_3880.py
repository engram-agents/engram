"""engram-alpha#3880 (U17 of #2652) -- the dream master's store reads go through the owner behind a flag.

The third `tools/` unit, and the first whose channel is a DOCUMENT that hands an agent a function taking a
connection (no `connect` appears in it). `tools/dream_master_batch.py` gains a CLI with three subcommands,
each printing ONE JSON object on stdout:

  safety-row <node_id>              {"is_current","status","superseded_by"} | null (a MISSING node, exit 0)
  coverage {recall_summary|recall_keywords}   the fraction health.metrics.recall_*_coverage(conn) computes
                                    (for every value engram_core can write; five hand-written keyword shapes
                                    diverge and are pinned: see the round-1 cells at the end of this file)
  null-embeddings --population <p>  the count health.engine._count_null_embeddings(conn, p) computes

Contract (issue #3880 plus Borges's widening comment, each cell red before the implementation):

  C1  differential: flag ON vs OFF gives byte-identical stdout and the same exit code on a fixture with a
      current, a superseded, a retracted and a missing node, for all three subcommands; and each equals what
      the library function computes today (the health functions are the oracle for the two aggregates).
  C2  failure is never a plausible value: owner down, Unavailable, a rejected template, a malformed answer and
      (flag OFF) a missing / unreadable store give a non-zero exit, EMPTY stdout, and a stderr line naming
      the class. No `null`, no `0`, no `1.0`.
  C3  owner down with the flag ON: zero direct connects to the store (counted), for every subcommand.
  C4  flag OFF: the direct path opens `mode=ro`; a planted write on that very connection fails.
  C5  spec cells: both dream agent specs name the CLI and say never to open knowledge.db, and NO call that
      passes a connection survives as an instruction in either spec (a SWEEP, so a fourth member turns it red).
  C6  the ledger carries the three channels and the method-note line.
  C7  ENGRAM_HOME: a temp home is honoured; a `~/.engram` default reads the decoy and goes red.
  C8  registry: every statement the flag-ON path sends is registered byte for byte, once, under its site.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
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

TOOL_PATH = REPO_ROOT / "tools" / "dream_master_batch.py"
FLAG_KEY = "dream_safety_reads_via_owner"
SITE_PREFIX = "tools/dream_master_batch.py:"
SPEC_DIR = REPO_ROOT / "src" / "engram" / "agents" / "claude"
SPECS = {"dream-master": SPEC_DIR / "engram-dream-master.md", "dream-fairy": SPEC_DIR / "engram-dream-fairy.md"}

SAFETY_SQL = "SELECT is_current, status, superseded_by FROM nodes WHERE id = ?"
_WS = ("\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
       "\u2009\u200a\u2028\u2029\u202f\u205f\u3000")
SUMMARY_SQL = (
    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN trim(coalesce(recall_summary, ''), '" + _WS + "') = '' "
    "THEN 1 ELSE 0 END), 0) FROM nodes WHERE is_current = 1 AND type IN (SELECT value FROM json_each(?))"
)
KEYWORDS_SQL = (
    "SELECT COUNT(*), COALESCE(SUM(CASE WHEN json_valid(recall_keywords) THEN CASE WHEN "
    "json_type(recall_keywords) = 'array' AND json_array_length(recall_keywords) > 0 THEN 0 ELSE 1 END "
    "ELSE 1 END), 0) FROM nodes WHERE is_current = 1 AND type IN (SELECT value FROM json_each(?))"
)
NULL_EMB_SQL = (
    "SELECT COUNT(*) FROM nodes WHERE is_current = 1 AND type IN (SELECT value FROM json_each(?)) "
    "AND embedding IS NULL"
)
CLAIM_TYPES = sorted(engram_core.CLAIM_BEARING_TYPES)


@pytest.fixture(autouse=True)
def _restore_imports():
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
    d = Path(tempfile.mkdtemp(prefix="dm3880-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


SUMMARIES = [None, "", "   ", "\u00a0\u3000\u2003", "\t\n", "real summary", "x", "\u200ax\u200a"]
KEYWORDS = [None, "", "[]", " [] ", '["a"]', "not json", '{"a": 1}', '"str"', "[1, 2]", "null", "[null]", "[1,]"]


def make_store(home: Path, *, claim_nodes: bool = True) -> Path:
    """A store with: a current / superseded / retracted node (the safety-row population), claim-bearing nodes
    whose summaries, keywords and embeddings exercise every branch of the two health functions for the values engram_core can write (the five shapes it
    cannot write diverge and are pinned separately, below), and the rows
    that must NOT count (a non-claim type, a non-current claim node with a NULL embedding)."""
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        ins = ("INSERT INTO nodes (id, type, claim, created_at, is_current, status, superseded_by, "
               "recall_summary, recall_keywords, embedding) VALUES (?, ?, 'c', '2026-01-01T00:00:00', ?, ?, ?, ?, ?, ?)")
        conn.execute(ins, ("ob_cur", "observation_factual", 1, "active", None, "s", '["k"]', "[0.1]"))
        conn.execute(ins, ("ob_old", "observation_factual", 0, "active", "ob_cur", "s", '["k"]', "[0.1]"))
        conn.execute(ins, ("ob_ret", "observation_factual", 0, "retracted", None, "s", '["k"]', "[0.1]"))
        if claim_nodes:
            for i in range(48):
                conn.execute(ins, (f"cb_{i:03d}", CLAIM_TYPES[i % len(CLAIM_TYPES)], 1, "active", None,
                                   SUMMARIES[i % len(SUMMARIES)], KEYWORDS[i % len(KEYWORDS)],
                                   None if i % 3 == 0 else "[0.5]"))
            conn.execute(ins, ("ls_001", "lesson", 1, "active", None, None, None, None))  # not claim-bearing
            conn.execute(ins, ("cb_old", "derivation", 0, "active", "cb_000", None, None, None))  # not current
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


@pytest.fixture
def seat(home):
    make_store(home)
    return home


def set_flag(home: Path, value=True) -> None:
    (home / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


def load_tool(home: Path, monkeypatch, path: Path = TOOL_PATH):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("dream_master_batch_3880", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cli(home, args, tool=TOOL_PATH, **extra_env):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    env.update(extra_env)
    return subprocess.run([sys.executable, str(tool), *args], capture_output=True, env=env, timeout=120)


def oracle_conn(home: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(home / "knowledge.db"))


class Opens:
    """Raise-and-record every way THIS thread could open a sqlite store (the in-process fake owner serves from
    other threads and keeps its own connections)."""

    def __init__(self):
        self.record = []
        self._thread = threading.get_ident()

    def install(self, monkeypatch, *, raising=True):
        real = {"connect": sqlite3.connect, "dbapi2": sqlite3.dbapi2.connect}

        def guard(name):
            def f(*a, **k):
                if threading.get_ident() != self._thread:
                    return real[name](*a, **k)
                self.record.append((name, str(a[0]) if a else ""))
                if raising:
                    raise AssertionError(f"direct open via {name}: {a[:1]}")
                return real[name](*a, **k)
            return f

        monkeypatch.setattr(sqlite3, "connect", guard("connect"))
        monkeypatch.setattr(sqlite3.dbapi2, "connect", guard("dbapi2"))
        return self


def _owner_answering(home, answer):
    owner = FakeOwner(home)
    owner._answer = lambda request, a=answer: (
        {"status": "ok", "result": "pong"} if request.get("method") == "ping" else a)
    return owner


def _owner_rows(home, rows):
    return _owner_answering(home, {"status": "ok", "result": rows})


# the seven invocations the differential runs: every safety-row population plus the two aggregates and the count
INVOCATIONS = {
    "safety_current": ["safety-row", "ob_cur"],
    "safety_superseded": ["safety-row", "ob_old"],
    "safety_retracted": ["safety-row", "ob_ret"],
    "safety_missing": ["safety-row", "ob_nope"],
    "coverage_summary": ["coverage", "recall_summary"],
    "coverage_keywords": ["coverage", "recall_keywords"],
    "null_embeddings": ["null-embeddings", "--population", "claim_bearing_nodes"],
}


# ---- C1: differential, and the oracle -----------------------------------------------------------

def _off_on(seat, args):
    set_flag(seat, False)
    off = cli(seat, args)
    set_flag(seat)
    with FakeOwner(seat) as owner:
        on = cli(seat, args)
    return off, on, owner


@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_c1_flag_on_and_off_give_byte_identical_stdout_and_the_same_exit_code(seat, name):
    off, on, owner = _off_on(seat, INVOCATIONS[name])
    assert off.returncode == on.returncode == 0, (off.stderr, on.stderr)
    assert off.stdout == on.stdout and off.stdout.endswith(b"\n") and off.stdout.count(b"\n") == 1
    assert off.stdout != b"" and on.stderr == off.stderr == b""
    assert any(r["method"] == "read" for r in owner.requests), "the flag-ON leg never reached the owner"


def test_c1_the_safety_row_for_each_population_is_the_expected_object(seat):
    want = {
        "safety_current": {"is_current": 1, "status": "active", "superseded_by": None},
        "safety_superseded": {"is_current": 0, "status": "active", "superseded_by": "ob_cur"},
        "safety_retracted": {"is_current": 0, "status": "retracted", "superseded_by": None},
    }
    set_flag(seat, False)
    for name, expect in want.items():
        out = cli(seat, INVOCATIONS[name]).stdout
        assert json.loads(out) == expect and list(json.loads(out)) == ["is_current", "status", "superseded_by"], out
    assert cli(seat, INVOCATIONS["safety_missing"]).stdout == b"null\n"


def test_c1_the_cli_equals_the_library_function_it_wraps(seat, monkeypatch):
    set_flag(seat, False)
    mod = load_tool(seat, monkeypatch)
    conn = oracle_conn(seat)
    for node in ("ob_cur", "ob_old", "ob_ret", "ob_nope"):
        assert json.loads(cli(seat, ["safety-row", node]).stdout) == mod.fetch_safety_row(conn, node)


@pytest.mark.parametrize("which", ["recall_summary", "recall_keywords"])
@pytest.mark.parametrize("flag", [False, True])
def test_c1_coverage_equals_the_health_function_exactly(seat, which, flag):
    import health.metrics as metrics

    expected = getattr(metrics, f"{which}_coverage")(oracle_conn(seat))
    assert 0.0 < expected < 1.0, "the fixture must exercise both covered and uncovered nodes"
    set_flag(seat, flag)
    if flag:
        with FakeOwner(seat):
            out = cli(seat, ["coverage", which]).stdout
    else:
        out = cli(seat, ["coverage", which]).stdout
    assert json.loads(out) == expected and out.decode().strip() == repr(expected), (out, expected)


@pytest.mark.parametrize("flag", [False, True])
def test_c1_null_embeddings_equals_the_health_function_exactly(seat, flag):
    import health.engine as engine

    expected = engine._count_null_embeddings(oracle_conn(seat), "claim_bearing_nodes")
    assert expected > 0
    set_flag(seat, flag)
    if flag:
        with FakeOwner(seat):
            out = cli(seat, INVOCATIONS["null_embeddings"]).stdout
    else:
        out = cli(seat, INVOCATIONS["null_embeddings"]).stdout
    assert out == f"{expected}\n".encode()


@pytest.mark.parametrize("flag", [False, True])
def test_c1_a_store_with_no_claim_bearing_nodes_reports_unavailable_not_a_number(home, flag):
    """health returns its UNAVAILABLE sentinel for an empty denominator; the CLI says so by name, never 0 or 1.0."""
    import health.metrics as metrics

    make_store(home, claim_nodes=False)
    conn = oracle_conn(home)
    conn.execute("UPDATE nodes SET type = 'lesson'")  # nothing claim-bearing is left
    conn.commit()
    assert metrics.recall_summary_coverage(conn) is metrics.UNAVAILABLE
    set_flag(home, flag)
    ctx = FakeOwner(home) if flag else None
    if ctx:
        ctx.__enter__()
    try:
        for which in ("recall_summary", "recall_keywords"):
            assert cli(home, ["coverage", which]).stdout == b'"UNAVAILABLE"\n'
        assert cli(home, INVOCATIONS["null_embeddings"]).stdout == b"0\n"  # a count of zero IS a value here
    finally:
        if ctx:
            ctx.__exit__(None, None, None)


def test_c1_control_a_wrong_column_order_owner_reddens_the_harness(seat):
    """The harness must be able to fail on the NEW side: an owner answering the safety row with status and
    superseded_by swapped is refused or differs; it never prints the same bytes."""
    class Swapped(FakeOwner):
        def _answer(self, request):
            ans = super()._answer(request)
            if request.get("method") == "read" and ans.get("status") == "ok" and ans["result"]:
                ans = {"status": "ok", "result": [[r[0], r[2], r[1]] for r in ans["result"]]}
            return ans

    set_flag(seat, False)
    off = cli(seat, INVOCATIONS["safety_superseded"])
    set_flag(seat)
    with Swapped(seat):
        on = cli(seat, INVOCATIONS["safety_superseded"])
    assert (on.returncode, on.stdout) != (off.returncode, off.stdout)


def test_c1_control_a_coverage_the_wrong_way_round_reddens_the_oracle_cell(seat):
    """An owner that answers (total, covered) instead of (total, missing) yields 1 - fraction: not the oracle."""
    class Inverted(FakeOwner):
        def _answer(self, request):
            ans = super()._answer(request)
            sql = (request.get("params") or {}).get("sql", "")
            if request.get("method") == "read" and ans.get("status") == "ok" and "COUNT(*), COALESCE" in sql:
                ans = {"status": "ok", "result": [[r[0], r[0] - r[1]] for r in ans["result"]]}
            return ans

    set_flag(seat, False)
    off = cli(seat, ["coverage", "recall_summary"])
    set_flag(seat)
    with Inverted(seat):
        on = cli(seat, ["coverage", "recall_summary"])
    assert on.stdout != off.stdout


# ---- C2: failure is never a plausible value -----------------------------------------------------

def _assert_failed(r, cls=None):
    assert r.returncode != 0, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == b"", r.stdout
    err = r.stderr.decode()
    assert err.strip(), "a failure must say so on stderr"
    if cls:
        assert cls in err, err


@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_c2_owner_down_flag_on_fails_with_empty_stdout_naming_the_class(seat, name):
    set_flag(seat)
    r = cli(seat, INVOCATIONS[name])
    _assert_failed(r, "DreamReadError")
    assert "owner" in r.stderr.decode().lower()


REFUSALS = [
    {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "unregistered_template", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "refused_at_load", "detail": "d", "remedy": "r"},
    {"status": "ok", "result": "not-a-list"},
]


@pytest.mark.parametrize("name", sorted(INVOCATIONS))
@pytest.mark.parametrize("answer", REFUSALS, ids=lambda a: a.get("reason", "malformed"))
def test_c2_every_owner_refusal_fails_empty_stdout_never_null_or_a_number(seat, name, answer):
    set_flag(seat)
    with _owner_answering(seat, answer):
        _assert_failed(cli(seat, INVOCATIONS[name]), "DreamReadError")


MALFORMED = {
    "safety-row": [
        [[None, None, None, None]],          # too wide
        [[1, "active"]],                     # too short
        [[]],                                # empty row
        [["1", "active", None]],             # is_current is text
        [[1, 5, None]],                      # status is a number
        [[1, "active", 7]],                  # superseded_by is a number
        [["x"]],                             # not three cells
        ["ob_cur"],                          # a row that is not a list
    ],
    "coverage": [
        [[]],                                # nothing in the row
        [[5]],                               # one cell
        [[5, 1, 1]],                         # three cells
        [[5, -1]],                           # negative missing
        [[5, 6]],                            # more missing than total
        [["5", 1]],                          # text count
        [[5.5, 1]],                          # float count
        [[0, 3]],                            # missing from an empty population
        [],                                  # an aggregate over zero rows still returns one row
        [[5, 1], [5, 1]],                    # two rows
    ],
    "null-embeddings": [[[]], [[-1]], [["3"]], [[1, 2]], [[2.5]], [], [[1], [1]]],
}


@pytest.mark.parametrize("name", ["safety_current", "coverage_summary", "coverage_keywords", "null_embeddings"])
def test_c2_malformed_owner_rows_fail_and_never_print_a_value(seat, name):
    args = INVOCATIONS[name]
    set_flag(seat)
    for rows in MALFORMED[args[0]]:
        with _owner_rows(seat, rows):
            _assert_failed(cli(seat, args), "DreamReadError")


ROW_GOOD = [1, "active", None]
ROW_BAD = [0, "retracted", None]


@pytest.mark.parametrize("rows", [
    [ROW_GOOD, ROW_BAD],                 # good first, conflicting second
    [ROW_BAD, ROW_GOOD],                 # bad first, good second
    [ROW_GOOD, ROW_GOOD],                # identical: cardinality is the rule, not agreement
    [ROW_GOOD, ROW_BAD, ROW_BAD],        # three
], ids=["good-then-bad", "bad-then-good", "identical", "three"])
def test_c2_security_a_selection_by_unique_id_answering_more_than_one_row_fails(seat, rows):
    """U17 security fold: more than one row for a unique node id is malformed, never a safety result."""
    set_flag(seat)
    with _owner_rows(seat, rows):
        r = cli(seat, INVOCATIONS["safety_retracted"])
    _assert_failed(r, "DreamReadError")


def test_c2_security_one_valid_row_is_unchanged(seat):
    set_flag(seat)
    with _owner_rows(seat, [ROW_GOOD]):
        r = cli(seat, INVOCATIONS["safety_current"])
    assert r.returncode == 0 and r.stderr == b"", (r.returncode, r.stderr)
    assert json.loads(r.stdout) == {"is_current": 1, "status": "active", "superseded_by": None}


def test_c2_an_empty_safety_answer_is_a_missing_node_not_a_failure(seat):
    """[] for a safety-row IS the missing-node answer (the one place null is correct)."""
    set_flag(seat)
    with _owner_rows(seat, []):
        r = cli(seat, INVOCATIONS["safety_current"])
    assert (r.returncode, r.stdout) == (0, b"null\n")


@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_c2_flag_off_a_missing_store_fails_naming_the_class(home, name):
    set_flag(home, False)
    _assert_failed(cli(home, INVOCATIONS[name]), "FileNotFoundError")


@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_c2_flag_off_an_unreadable_store_fails_naming_the_class(home, name):
    (home / "knowledge.db").write_bytes(b"this is not a sqlite file " * 100)
    _assert_failed(cli(home, INVOCATIONS[name]), "DatabaseError")


@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_c2_flag_off_a_store_without_the_nodes_table_fails_naming_the_class(home, name):
    sqlite3.connect(str(home / "knowledge.db")).execute("CREATE TABLE other (x)").connection.commit()
    _assert_failed(cli(home, INVOCATIONS[name]), "OperationalError")


def test_c2_an_unknown_population_fails_it_is_not_ignored(seat):
    for flag in (False, True):
        set_flag(seat, flag)
        with FakeOwner(seat):
            _assert_failed(cli(seat, ["null-embeddings", "--population", "all_nodes"]), "ValueError")


def test_c2_an_unknown_coverage_name_fails_empty_stdout(seat):
    _assert_failed(cli(seat, ["coverage", "embedding"]))


def test_c2_no_subcommand_or_no_node_id_fails_empty_stdout(seat):
    _assert_failed(cli(seat, []))
    _assert_failed(cli(seat, ["safety-row"]))


# ---- C3: owner down, zero direct connects ---------------------------------------------------------

@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_c3_owner_down_flag_on_opens_nothing_directly(seat, monkeypatch, capsys, name):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    rc = mod.main(INVOCATIONS[name])
    out = capsys.readouterr()
    assert rc != 0 and out.out == "" and "DreamReadError" in out.err
    assert opens.record == [], opens.record


@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_c3_owner_up_flag_on_also_opens_nothing_directly_and_reads_through_the_owner(seat, monkeypatch, capsys, name):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with FakeOwner(seat) as owner:
        rc = mod.main(INVOCATIONS[name])
    assert rc == 0, capsys.readouterr().err
    assert opens.record == []
    assert [r["method"] for r in owner.requests].count("read") == 1, "one registered statement per command"


def test_c3_flag_on_without_the_client_module_fails_and_never_reads_directly(seat, monkeypatch, capsys):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    opens = Opens().install(monkeypatch)
    with FakeOwner(seat):
        rc = mod.main(INVOCATIONS["safety_current"])
    assert rc != 0 and capsys.readouterr().out == "" and opens.record == []


def test_c3_flag_on_never_stats_the_local_store_file(seat, monkeypatch, capsys):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    real_exists = Path.exists

    def exists(self, *a, **k):
        if str(self).endswith("knowledge.db"):
            raise AssertionError(f"the tool stat the local store: {self}")
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", exists)
    with FakeOwner(seat):
        assert mod.main(INVOCATIONS["safety_current"]) == 0


@pytest.mark.parametrize("config", [None, "{not json", '{"engramd": [1]}', '"a string"', ""])
def test_c3_a_flag_that_is_absent_or_unreadable_is_off_and_reads_directly(seat, monkeypatch, capsys, config):
    if config is not None:
        (seat / "config.json").write_text(config, encoding="utf-8")
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch, raising=False)
    assert mod.main(INVOCATIONS["safety_current"]) == 0  # no owner running: must not need one
    assert len(opens.record) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "active"


@pytest.mark.parametrize("value", [False, "true", 1, None, "yes"])
def test_c3_only_the_json_boolean_true_turns_the_flag_on(seat, monkeypatch, capsys, value):
    set_flag(seat, value)
    mod = load_tool(seat, monkeypatch)
    assert mod.main(INVOCATIONS["safety_current"]) == 0  # no owner running: direct


# ---- C4: flag OFF is read-only ---------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_c4_the_direct_connection_is_read_only_a_planted_write_fails(seat, monkeypatch, capsys, name):
    set_flag(seat, False)
    mod = load_tool(seat, monkeypatch)
    seen = []
    real = sqlite3.connect

    def spy(*a, **k):
        seen.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    assert mod.main(INVOCATIONS[name]) == 0
    assert len(seen) == 1, seen
    (args, kwargs) = seen[0]
    conn = real(*args, **kwargs)  # the very connection the tool opened, reopened here (the tool closes its own)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            conn.execute("INSERT INTO nodes (id, type, claim, created_at) VALUES ('w', 'observation_factual', 'x', 'y')")
    finally:
        conn.close()
    assert oracle_conn(seat).execute("SELECT COUNT(*) FROM nodes WHERE id = 'w'").fetchone() == (0,)


# ---- C7: ENGRAM_HOME ----------------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [False, True])
def test_c7_a_temp_engram_home_is_honoured_and_the_users_dot_engram_is_never_read(seat, tmp_path, flag):
    decoy = tmp_path / "userhome"
    (decoy / ".engram").mkdir(parents=True)
    make_store(decoy / ".engram", claim_nodes=False)  # a store with different answers: ob_cur exists, no coverage
    sqlite3.connect(str(decoy / ".engram" / "knowledge.db")).execute(
        "UPDATE nodes SET status = 'DECOY' WHERE id = 'ob_cur'").connection.commit()
    set_flag(seat, flag)
    ctx = FakeOwner(seat) if flag else None
    if ctx:
        ctx.__enter__()
    try:
        r = cli(seat, INVOCATIONS["safety_current"], HOME=str(decoy))
        cov = cli(seat, INVOCATIONS["coverage_summary"], HOME=str(decoy))
    finally:
        if ctx:
            ctx.__exit__(None, None, None)
    assert json.loads(r.stdout)["status"] == "active", (r.stdout, r.stderr)
    assert cov.returncode == 0 and 0.0 < json.loads(cov.stdout) < 1.0, (cov.stdout, cov.stderr)


def test_c7_there_is_no_home_default_in_the_new_code():
    src = (REPO_ROOT / "src" / "engram" / "tools" / "dream_master_batch.py").read_text()
    assert "Path.home()" not in src and "expanduser" not in src
    assert not re.search(r"""["']~/\.engram""", src), "a hard-coded default home in the dream master CLI"
    assert 'os.environ.get("ENGRAM_HOME")' not in src, "the home has ONE resolver: _owner_read.engram_home()"


# ---- C8: the registry -----------------------------------------------------------------------------

def _registered():
    return json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())["templates"]


@pytest.mark.parametrize("sql,fn", [
    (SAFETY_SQL, "fetch_safety_row"),
    (SUMMARY_SQL, "recall_summary_counts"),
    (KEYWORDS_SQL, "recall_keywords_counts"),
    (NULL_EMB_SQL, "count_null_embeddings"),
])
def test_c8_each_statement_is_registered_once_byte_exact_under_this_tools_site(sql, fn):
    hits = [t for t in _registered() if t["sql"] == sql]
    assert len(hits) == 1, "each statement must be registered exactly once"
    assert hits[0]["key"] == hashlib.sha256(sql.encode("utf-8")).hexdigest()
    assert hits[0]["sites"] == [SITE_PREFIX + fn]
    assert sql in registered_sql()


def test_c8_the_flag_on_path_sends_exactly_the_registered_bytes_with_the_pinned_parameters(seat):
    set_flag(seat)
    sent = {}
    with FakeOwner(seat) as owner:
        for name, args in INVOCATIONS.items():
            n = len(owner.requests)
            assert cli(seat, args).returncode == 0
            reads = [r for r in owner.requests[n:] if r["method"] == "read"]
            assert len(reads) == 1, (name, owner.requests[n:])
            sent[name] = (reads[0]["params"]["sql"], reads[0]["params"]["args"])
    types = [json.dumps(CLAIM_TYPES)]
    assert sent["safety_current"] == (SAFETY_SQL, ["ob_cur"])
    assert sent["safety_missing"] == (SAFETY_SQL, ["ob_nope"])
    assert sent["coverage_summary"] == (SUMMARY_SQL, types)
    assert sent["coverage_keywords"] == (KEYWORDS_SQL, types)
    assert sent["null_embeddings"] == (NULL_EMB_SQL, types)


def test_c8_the_flag_key_is_pinned(seat, monkeypatch):
    assert load_tool(seat, monkeypatch).OWNER_FLAG_KEY == FLAG_KEY


def test_c8_the_scan_config_roots_the_three_functions():
    cfg = json.loads((REPO_ROOT / "tools" / "engramd_read_scan.config.json").read_text())
    for fn in ("fetch_safety_row", "recall_summary_counts", "recall_keywords_counts", "count_null_embeddings"):
        assert SITE_PREFIX + fn in cfg["roots"]


def test_c8_the_new_cli_is_tiered_and_its_test_is_mapped():
    tiers = json.loads((REPO_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text())
    assert {"path": "tools/dream_master_batch.py", "tier": "essential"} in tiers["mechanisms"]
    tmap = json.loads((REPO_ROOT / "src" / "engram" / "packaging" / "test-map.json").read_text())
    mapped = [e for e in tmap["mappings"] if e.get("path_prefix_or_glob") == "tools/dream_master_batch.py"]
    assert mapped and "tests/test_dream_safety_owner_read_3880.py" in mapped[0]["test_globs"]


# ---- both layouts ---------------------------------------------------------------------------------

def _flattened(tmp: Path) -> Path:
    root = tmp / "engram-plugin"
    (root / "hooks").mkdir(parents=True)
    for p in (REPO_ROOT / "src" / "engram" / "hooks" / "claude").glob("*.py"):
        shutil.copy(p, root / "hooks" / p.name)
    for p in (REPO_ROOT / "src" / "engram").glob("*.py"):
        shutil.copy(p, root / p.name)
    shutil.copytree(REPO_ROOT / "src" / "engram" / "tools", root / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO_ROOT / "src" / "engram" / "health", root / "health",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return root


@pytest.mark.parametrize("layout", ["source", "flattened"])
@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_cli_flag_on_works_in_both_layouts(layout, name, seat, tmp_path):
    set_flag(seat)
    tool = TOOL_PATH if layout == "source" else _flattened(tmp_path) / "tools" / "dream_master_batch.py"
    with FakeOwner(seat) as owner:
        r = cli(seat, INVOCATIONS[name], tool=tool)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert any(q["method"] == "read" for q in owner.requests), "the flag-ON run never reached the owner"


# ---- C5: the agent specs --------------------------------------------------------------------------------

# Every shape in which a spec hands an agent a connection-taking call or tells it to open the store itself.
SWEEP = [
    re.compile(r"\(\s*conn\s*[,)]"),              # f(conn), f(conn, x): any call passing a connection
    re.compile(r"\bsqlite3\.connect\b"),
    re.compile(r"\bfetch_safety_row\s*\(\s*conn"),
    re.compile(r"\b_count_null_embeddings\s*\(\s*conn"),
    re.compile(r"\b\w*coverage\s*\(\s*conn"),
    re.compile(r"\bcheck_fts_integrity\s*\(\s*conn"),
]


def sweep(text: str) -> list[tuple[int, str]]:
    return [(i, line.strip()) for i, line in enumerate(text.splitlines(), 1) if any(p.search(line) for p in SWEEP)]


@pytest.mark.parametrize("spec", sorted(SPECS))
def test_c5_no_call_that_passes_a_connection_survives_as_an_instruction(spec):
    assert sweep(SPECS[spec].read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("planted", [
    "current = fetch_safety_row(conn, target_id)",
    "run health.metrics.recall_keywords_coverage(conn) now",
    "x = health.engine._count_null_embeddings(conn, population)",
    "a_fourth_helper(conn)",
    "a_fifth_helper(conn, x, y)",
    "conn = sqlite3.connect('/x/knowledge.db')",
])
def test_c5_control_the_sweep_flags_each_shape_so_a_fourth_member_turns_it_red(planted):
    assert sweep(f"prose\n```python\n{planted}\n```\n") == [(3, planted)]


@pytest.mark.parametrize("spec", sorted(SPECS))
def test_c5_the_spec_names_the_cli_and_says_never_to_open_the_store(spec):
    text = SPECS[spec].read_text(encoding="utf-8")
    assert "dream_master_batch.py" in text and "safety-row" in text
    assert re.search(r"never\s+open\s+`?knowledge\.db`?\s+yourself", text, re.IGNORECASE), "the plain instruction"


def test_c5_the_dream_master_names_all_three_subcommands():
    text = SPECS["dream-master"].read_text(encoding="utf-8")
    for sub in ("safety-row", "coverage recall_summary", "coverage recall_keywords",
                "null-embeddings --population"):
        assert sub in text, sub


@pytest.mark.parametrize("spec", sorted(SPECS))
def test_c5_every_cli_command_in_a_spec_uses_the_canonical_home_expression(spec):
    """Lei's #3863 ruling: a command naming the plugin tools directory resolves the home through
    ${ENGRAM_HOME:-$HOME/.engram}, never a bare ~/.engram."""
    for i, line in enumerate(SPECS[spec].read_text(encoding="utf-8").splitlines(), 1):
        if "dream_master_batch.py" in line and re.search(r"python3?\b", line):
            assert "${ENGRAM_HOME:-$HOME/.engram}" in line and "~/.engram" not in line, (spec, i, line)


# ---- C6: the ledger -----------------------------------------------------------------------------------

LEDGER = REPO_ROOT / "docs" / "2652-migration-ledger.md"


def _section_2b() -> str:
    text = LEDGER.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^## 2b\..*?(?=^## )", text)
    assert m, "the ledger has no Section 2b (channels the connect( scan cannot see)"
    return m.group(0)


def test_c6_the_ledger_carries_one_prose_row_per_channel_behind_the_flag():
    rows = [ln for ln in _section_2b().splitlines() if re.match(r"^\|\s*2b-\d+\s*\|", ln)]
    assert len(rows) == 3, rows
    for ln, key in zip(rows, ("safety-row", "coverage", "null-embeddings")):
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        assert key in ln and "engram-dream-master.md" in ln and f"engramd.{FLAG_KEY}" in ln, ln
        assert "prose" in cells and "behind-flag" in cells, cells
    assert any("engram-dream-fairy.md" in ln for ln in rows), "the safety-row row names the fairy spec too"


def test_c6_the_method_notes_say_a_function_taking_a_connection_is_a_channel():
    text = LEDGER.read_text(encoding="utf-8")
    assert re.search(r"hands an agent a function taking a connection is a channel", text)


def test_c6_the_section_2_gate_still_counts_only_connect_lines():
    r = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "check_2652_ledger.py")],
                       capture_output=True, text=True, cwd=REPO_ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


# ---- live owner (Linux and supported Mac) ---------------------------------------------------------

@requires_linux_owner
@pytest.mark.parametrize("name", sorted(INVOCATIONS))
def test_live_real_owner_agrees_with_the_direct_path(seat, name):
    set_flag(seat, False)
    off = cli(seat, INVOCATIONS[name])
    set_flag(seat)
    owner = eo.EngramdOwner(seat, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    try:
        on = cli(seat, INVOCATIONS[name])
    finally:
        owner.stop()
    assert (on.returncode, on.stdout) == (off.returncode, off.stdout) and on.returncode == 0


# ---- round-1 fold (fairy finding 1): recall_keywords divergence, PINNED --------------------------------
#
# The SQL (json_valid / json_type / json_array_length) and health.metrics.recall_keywords_coverage
# (json.loads + isinstance list + len) agree for every value engram_core can write: it stores the column as
# `json.dumps(recall_keywords) if recall_keywords else None` (engram_core.py, the derive INSERT, the line
# `json.dumps(recall_keywords) if recall_keywords else None,`) over a validated list, so the column holds
# NULL or a standard-JSON list. Five hand-written shapes engram_core CANNOT write diverge, and they are
# pinned HERE, not fixed: nothing in a normal write path can reach them, and making SQLite parse NaN (or
# lifting its depth limit) is not available. Measured on SQLite 3.53.4 against CPython json.loads:
#
#   shape                                    health (python)       CLI (sql)
#   '[NaN]', '[Infinity]', '[-Infinity]'     present (a list)      missing (json_valid is 0)
#   '[' * 1500 + ']' * 1500                  present               missing (past SQLite's depth limit of 1000;
#                                                                   1000 deep is still present on both)
#   '["a"]' + NUL byte                       missing (json.loads   present (SQLite stops at the NUL)
#                                            raises "Extra data")
#
# A future change that alters either side, silently, turns these cells red. They are NOT in the KEYWORDS
# list above because that list feeds the exact-equality cell; mixing them in would make that cell red.

# shape -> (raw column value, health coverage on a store with that ONE node, CLI coverage on the same store)
DIVERGENT_KEYWORDS = {
    "nan": ("[NaN]", 1.0, 0.0),
    "infinity": ("[Infinity]", 1.0, 0.0),
    "negative_infinity": ("[-Infinity]", 1.0, 0.0),
    "deeper_than_sqlite_limit": ("[" * 1500 + "]" * 1500, 1.0, 0.0),
    "trailing_nul": ('["a"]\x00', 0.0, 1.0),
}
# the same nesting just inside SQLite's limit agrees: the boundary is the limit, not nesting as such
AGREEING_KEYWORDS = {"depth_1000": "[" * 1000 + "]" * 1000, "standard_list": '["a"]'}


def _one_keyword_store(home: Path, raw) -> Path:
    db = make_store(home, claim_nodes=False)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE nodes SET type = 'lesson'")  # the 3 safety-row nodes must not count
    conn.execute("INSERT INTO nodes (id, type, claim, created_at, is_current, status, recall_summary, "
                 "recall_keywords, embedding) VALUES ('cb_one', ?, 'c', '2026-01-01T00:00:00', 1, 'active', "
                 "'s', ?, '[0.5]')", (CLAIM_TYPES[0], raw))
    conn.commit()
    conn.close()
    return db


def _keywords_pair(home: Path, flag: bool):
    import health.metrics as metrics

    health = metrics.recall_keywords_coverage(oracle_conn(home))
    set_flag(home, flag)
    if flag:
        with FakeOwner(home):
            out = cli(home, ["coverage", "recall_keywords"]).stdout
    else:
        out = cli(home, ["coverage", "recall_keywords"]).stdout
    return health, json.loads(out)


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("shape", sorted(DIVERGENT_KEYWORDS))
def test_round1_keywords_shapes_engram_core_cannot_write_diverge_in_exactly_the_pinned_way(home, shape, flag):
    raw, health_expected, cli_expected = DIVERGENT_KEYWORDS[shape]
    _one_keyword_store(home, raw)
    health, cli_value = _keywords_pair(home, flag)
    assert (health, cli_value) == (health_expected, cli_expected), (shape, flag, health, cli_value)


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("shape", sorted(AGREEING_KEYWORDS))
def test_round1_keywords_control_the_boundary_shapes_that_still_agree(home, shape, flag):
    _one_keyword_store(home, AGREEING_KEYWORDS[shape])
    health, cli_value = _keywords_pair(home, flag)
    assert health == cli_value == 1.0, (shape, flag, health, cli_value)


def test_round1_keywords_combined_fixture_measured_numbers(home):
    """The base fixture (49 current claim-bearing rows, which agree) plus the five divergent rows: 54. Measured:
    health 0.3148 (17/54), CLI 0.2593 (14/54): the difference is the four present-in-python rows less the one
    missing-in-python row, 3/54, identically flag ON and OFF."""
    db = make_store(home)
    conn = sqlite3.connect(str(db))
    ins = ("INSERT INTO nodes (id, type, claim, created_at, is_current, status, recall_summary, recall_keywords, "
           "embedding) VALUES (?, ?, 'c', '2026-01-01T00:00:00', 1, 'active', 's', ?, '[0.5]')")
    for i, (raw, _h, _c) in enumerate(DIVERGENT_KEYWORDS.values()):
        conn.execute(ins, (f"dv_{i}", CLAIM_TYPES[0], raw))
    conn.commit()
    conn.close()
    for flag in (False, True):
        health, cli_value = _keywords_pair(home, flag)
        assert (round(health, 4), round(cli_value, 4)) == (0.3148, 0.2593), (flag, health, cli_value)


# ---- round-1 fold (finding 2): the bare name as an instruction -----------------------------------------

_LIBRARY_NOTE = re.compile(r"you do not call it", re.IGNORECASE)


_SENTENCE_END = re.compile(r"(?<=[.!?]\))\s+|(?<=[.!?])\s+")


def bare_name_uses(text: str, name: str = "fetch_safety_row") -> list[tuple[int, str]]:
    """Sentences that use the bare library-function name, other than the one library-note sentence saying the
    agent does not call it. The exemption is per SENTENCE (round 2): a paragraph that holds the note does not
    exempt an instruction placed beside it."""
    out, line_no = [], 1
    for para in re.split(r"\n\s*\n", text):
        pos = 0
        for sentence in _SENTENCE_END.split(para):
            start = para.find(sentence, pos)
            pos = start + len(sentence)
            if re.search(rf"\b{name}\b", sentence) and not _LIBRARY_NOTE.search(sentence):
                lines = sentence.splitlines()
                first = next(i for i, ln in enumerate(lines) if name in ln)
                out.append((line_no + para.count("\n", 0, start) + first, lines[first].strip()))
        line_no += para.count("\n") + 2
    return out


@pytest.mark.parametrize("spec", sorted(SPECS))
def test_round1_c5_the_bare_function_name_is_never_an_instruction_in_either_spec(spec):
    assert bare_name_uses(SPECS[spec].read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("planted", [
    "then verify the edge still doesn't exist via `fetch_safety_row`, then wire it",
    "Fetch current state for the SAFETY CHECK only: fetch_safety_row checks the substrate",
    "- call fetch_safety_row on the target",
])
def test_round1_c5_control_the_bare_name_as_an_instruction_turns_the_sweep_red(planted):
    assert len(bare_name_uses(f"prose\n\n{planted}\n\nmore prose\n")) == 1


def test_round1_c5_control_the_library_note_is_the_one_allowed_mention():
    note = ("(`fetch_safety_row` in `tools/dream_master_batch.py` is the library function\n"
            "the CLI wraps; you do not call it.)")
    assert bare_name_uses(f"prose\n\n{note}\n") == []
    assert len(bare_name_uses(f"prose\n\n{note}\n\nthen call fetch_safety_row\n")) == 1


@pytest.mark.parametrize("instruction", [
    "Then call fetch_safety_row on the target.",
    "Verify the edge via `fetch_safety_row` first.",
])
def test_round2_control_an_instruction_in_the_same_paragraph_as_the_library_note_is_flagged(instruction):
    note = ("(`fetch_safety_row` in `tools/dream_master_batch.py` is the library function\n"
            "the CLI wraps; you do not call it.)")
    for text in (f"prose\n\n{note} {instruction}\n", f"prose\n\n{instruction} {note}\n",
                 f"prose\n\n{note}\n{instruction}\n"):
        assert len(bare_name_uses(text)) == 1, text


def test_round1_c5_category_7_names_a_tool_that_exists_for_the_edge_check():
    line = next(ln for ln in _master_text().splitlines() if "Category 7 missing-principle-edge" in ln)
    assert "engram_inspect" in line and 'view="edges"' in line, line
    assert "no_op_already_exists" in line, "engram_add_edge is idempotent: the fallback must be named"


# ---- round-1 fold (finding 3): a failed read is its own record section, with a stop condition -----------

def _master_text() -> str:
    return SPECS["dream-master"].read_text(encoding="utf-8")


def _failed_read_rule() -> str:
    text = _master_text()
    return text[text.index("**A failed read is not a missing node.**"):text.index("**Missing-node case**")]


def test_round1_c5_the_record_format_has_its_own_heading_for_failed_safety_reads():
    text = _master_text()
    fmt = text[text.index("# Dream record — YYYY-MM-DD"):]
    assert re.search(r"(?m)^## Safety reads failed", fmt), "no section in the dream-record format"
    section = fmt.split("## Safety reads failed")[1].split("\n## ")[0]
    assert "Snapshot-diverged" not in section, "failed reads must not be filed under divergence"
    assert re.search(r"stopped", section, re.IGNORECASE), "the section carries the stop outcome"


def test_round1_c5_the_rule_files_a_failed_read_under_that_heading_not_under_divergence():
    rule = _failed_read_rule()
    assert "Safety reads failed" in rule and "Snapshot-diverged" not in rule, rule


def test_round1_c5_three_consecutive_failed_safety_reads_stop_the_execution_pass_loudly():
    rule = _failed_read_rule()
    assert re.search(r"3\s+consecutive\s+failed\s+safety\s+reads", rule), rule
    assert re.search(r"\bSTOP\b", rule) and re.search(r"do\s+not\s+describe", rule, re.IGNORECASE), rule
    assert re.search(r"successful.{0,60}reset|reset.{0,60}successful", rule, re.IGNORECASE | re.DOTALL), rule


def test_round2_the_completion_checklist_has_an_exit_for_a_stopped_execution_pass():
    text = _master_text()
    checklist = text[text.index("# Completion criteria"):text.index("# Tools you DO NOT use")]
    lines = [ln for ln in checklist.splitlines() if ln.startswith("- [ ]") and "execution pass" in ln]
    assert len(lines) == 1, lines
    assert "Safety reads failed" in lines[0] and re.search(r"3\s+consecutive", lines[0]), lines[0]
    assert re.search(r"\bcompleted\b", lines[0]) and re.search(r"\bOR\b", lines[0]), lines[0]


def test_round2_the_stop_rule_says_what_happens_after_a_stop():
    rule = _failed_read_rule()
    stop = rule[rule.index("**Stop condition"):]
    assert "coverage" in stop and "null-embeddings" in stop, stop
    assert re.search(r"once\s+each", stop), stop
    assert re.search(r"not\s+count\s+toward\s+the\s+3", stop), stop
    assert re.search(r"finish\s+the\s+night", stop), stop


# ---- round-1 fold (finding 4): the ledger tallies ------------------------------------------------------

def _template_counts():
    entries = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())["templates"]
    tools_only = [e for e in entries if e["sites"] and all(s.startswith("tools/") for s in e["sites"])]
    return len(entries) - len(tools_only), len(entries), len(tools_only)


def test_round1_c6_section_3_header_counts_equal_the_registry():
    non_tools, total, tools = _template_counts()
    m = re.search(r"(\d+) template entries; (\d+) with the ([\w-]+) `tools/` templates", LEDGER.read_text())
    assert m, "the Section 3 header sentence"
    assert (int(m.group(1)), int(m.group(2))) == (non_tools, total), (m.group(0), non_tools, total, tools)
    words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
             "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
             "nineteen": 19, "twenty": 20, "twenty-one": 21, "twenty-two": 22, "twenty-three": 23,
             "twenty-four": 24, "twenty-five": 25}
    assert words[m.group(3)] == tools


def test_round1_c6_section_1_says_three_prose_channels_are_listed_by_hand_in_2b():
    text = LEDGER.read_text()
    sec1 = text[text.index("## 1. Population"):text.index("## 2. The ledger")]
    assert re.search(r"three further prose channels.{0,200}Section 2b.{0,200}not in these tallies", sec1,
                     re.DOTALL | re.IGNORECASE), "the tally sentence"


# ---- round 2 (Sol's finding 1): the aggregate CLI must not import engram_core ---------------------

def _no_sqlite_vec_shim(tmp_path: Path) -> str:
    """A directory holding a `sqlite_vec.py` that cannot import: put first on the child's PYTHONPATH it makes
    engram_core print its `[engram] sqlite-vec unavailable` notice, as a host without the package does."""
    d = tmp_path / "shim_no_sqlite_vec"
    d.mkdir()
    (d / "sqlite_vec.py").write_text("raise ImportError('shimmed: sqlite-vec unavailable')\n", encoding="utf-8")
    return str(d)


def test_round2_the_shim_really_makes_engram_core_print_the_notice(tmp_path):
    """The control for the cell below: without it the shim could be a no-op and the cell vacuously green."""
    shim = _no_sqlite_vec_shim(tmp_path)
    src = str(TOOL_PATH.resolve().parent.parent)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH")}
    env["PYTHONPATH"] = os.pathsep.join([shim, src])
    done = subprocess.run([sys.executable, "-c", "import engram_core"], capture_output=True, env=env, timeout=120)
    assert b"sqlite-vec unavailable" in done.stderr, done.stderr


@pytest.mark.parametrize("flag_on", [False, True], ids=["flag_off", "flag_on"])
@pytest.mark.parametrize("name", ["coverage_summary", "coverage_keywords", "null_embeddings"])
def test_round2_the_aggregate_cli_is_silent_on_stderr_without_sqlite_vec(seat, tmp_path, name, flag_on):
    shim = _no_sqlite_vec_shim(tmp_path)
    set_flag(seat, False)
    reference = cli(seat, INVOCATIONS[name]).stdout
    set_flag(seat, flag_on)
    if flag_on:
        with FakeOwner(seat):
            shimmed = cli(seat, INVOCATIONS[name], PYTHONPATH=shim)
    else:
        shimmed = cli(seat, INVOCATIONS[name], PYTHONPATH=shim)
    assert shimmed.returncode == 0, shimmed.stderr
    assert shimmed.stderr == b"", shimmed.stderr
    assert shimmed.stdout == reference and reference.count(b"\n") == 1 and json.loads(reference) is not None


_NESTED_TOOL = ('"${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}'
                '/tools/dream_master_batch.py"')


def printed_commands(text: str) -> list[str]:
    """Every `python3 ...dream_master_batch.py` command, however quoted (or not): from `python3` to the tool
    name on one line, plus one closing quote if present."""
    return re.findall(r"python3\s+[^\n]*?dream_master_batch\.py[\"']?", text)


@pytest.mark.parametrize("planted", [
    "python3 ~/.engram/marketplace/plugins/engram/tools/dream_master_batch.py safety-row x",
    "python3 '${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}/tools/dream_master_batch.py' safety-row x",
    'python3 "$HOME/.engram/marketplace/plugins/engram/tools/dream_master_batch.py" safety-row x',
])
def test_round2_control_an_unquoted_or_differently_quoted_command_is_flagged(planted):
    cmds = printed_commands(f"run `{planted}` now\n")
    assert len(cmds) == 1 and cmds != ["python3 " + _NESTED_TOOL], cmds


def test_round2_control_the_canonical_nested_command_is_not_flagged():
    cmds = printed_commands(f"run `python3 {_NESTED_TOOL} safety-row x` now\n")
    assert cmds == ["python3 " + _NESTED_TOOL]


@pytest.mark.parametrize("spec", sorted(SPECS))
def test_round2_every_printed_command_honours_claude_plugin_root_with_the_canonical_fallback(spec):
    """Sibling commands (engram-upgrade, the home sweep) spell the plugin root this way; the old form pinned the
    plugin under $ENGRAM_HOME and ignored CLAUDE_PLUGIN_ROOT. Every `python3 ...dream_master_batch.py` command
    in the spec carries the nested form exactly."""
    text = SPECS[spec].read_text(encoding="utf-8")
    cmds = printed_commands(text)
    assert cmds, spec
    assert all(c == "python3 " + _NESTED_TOOL for c in cmds), cmds


@pytest.mark.parametrize("plugin_root_set", [False, True], ids=["root_unset", "root_set"])
def test_round2_the_printed_command_runs_under_bash(tmp_path, plugin_root_set):
    """Execute the command as the spec prints it. There is no store, so the CLI exits non-zero, but it must
    reach its OWN error line (an error class), not bash's or python's 'No such file'."""
    home = tmp_path / "scratch_home"
    home.mkdir()
    # with the root unset the fallback is $ENGRAM_HOME/marketplace/plugins/engram: point it at the source tree
    link = home / "marketplace" / "plugins" / "engram"
    link.parent.mkdir(parents=True)
    link.symlink_to(TOOL_PATH.resolve().parent.parent)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    if plugin_root_set:
        env["CLAUDE_PLUGIN_ROOT"] = str(TOOL_PATH.resolve().parent.parent)
        env["ENGRAM_HOME"] = str(tmp_path / "elsewhere")  # proves the root, not the home, located the tool
        (tmp_path / "elsewhere").mkdir()
    done = subprocess.run(["bash", "-c", f"python3 {_NESTED_TOOL} safety-row ob_x"], capture_output=True,
                          env=env, timeout=120)
    assert done.returncode != 0 and done.stdout == b""
    assert b"No such file" not in done.stderr and b"can't open file" not in done.stderr, done.stderr
    assert b"Error" in done.stderr, done.stderr


def test_round2_the_literal_claim_bearing_type_list_equals_engram_core(monkeypatch, seat):
    """The CLI keeps its own literal (importing engram_core prints a notice on a host without sqlite-vec); this
    pins it to the core's set so there is still no second list to drift. Importing engram_core here may print
    the notice: this cell does not compare bytes."""
    mod = load_tool(seat, monkeypatch)
    import engram_core as core

    assert isinstance(mod.CLAIM_BEARING_TYPES_SORTED, tuple)
    assert list(mod.CLAIM_BEARING_TYPES_SORTED) == sorted(core.CLAIM_BEARING_TYPES)
    assert json.loads(mod._claim_bearing_types_json()) == sorted(core.CLAIM_BEARING_TYPES)
