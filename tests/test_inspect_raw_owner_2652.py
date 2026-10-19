"""engram-alpha#2652 (tools-7) -- tools/inspect_raw.py reads its one row through the owner-read helper.

`inspect_raw.py` is a read-only diagnostic CLI.  It used to open the LIVE store read-write
(`sqlite3.connect(DB_PATH)`, `SELECT *`).  Behind `engramd.inspect_raw_reads_via_owner` (default OFF) its
ONE read now goes to the engramd owner; with the flag OFF it opens the store `mode=ro`.  Either way the
write-capable handle is gone and the file has no connect line of its own.

Contract (each cell red before the implementation):

  P1  every column the template names exists in a store built by the real schema code
  P2  the template covers every column the tool reads (type, metadata, the stripped columns)
  T*  the statement is registered under the tool's own site, and the scan config names it as a root
  O1  flag OFF vs flag ON through a REAL EngramdOwner: byte-identical stdout, every mode, NULL row included
  O2  flag ON, owner down: exit 3, empty stdout, one typed stderr line -- never exit 1, never "not found"
  O3  the owner refuses: typed error, exit 3
  O4  the owner answers malformed (two rows / 4 cells / another id / bad cell types): typed error
  O5  a BLOB embedding through the real owner: typed error naming unsupported_value_type
  O6  a real owner answering zero rows: exit 1 and "not found" (genuine absence stays absence)
  R1  flag OFF is read-only: a write through the connection fails, the store bytes do not change
  G1  the file holds no `sqlite3.connect(` line
  H1  a RELATIVE ENGRAM_HOME is refused (alpha #3863): exit 2, empty stdout, one stderr line, no traceback
  V1  the validation-failure message carries the "Not answering 'node not found'" sentence
  F1  every owner-read helper failure passes through read_row and the CLI VERBATIM: nothing appended (round 5)
"""

from __future__ import annotations

import contextlib
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
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_owner import FakeOwner  # noqa: E402
from _owner_platform import requires_linux_owner  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ENGRAM = str(REPO_ROOT / "src" / "engram")
if _SRC_ENGRAM not in sys.path:
    sys.path.insert(0, _SRC_ENGRAM)

import engram_core  # noqa: E402
import engramd_owner as eo  # noqa: E402

TOOL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "inspect_raw.py"
FLAG_KEY = "inspect_raw_reads_via_owner"
SITE = "tools/inspect_raw.py:read_row"
ROW_SQL = "SELECT id, type, embedding, confidence_history, metadata FROM nodes WHERE id = ?"

EMBEDDING = json.dumps([round(0.125 * i - 0.5, 3) for i in range(8)])
HISTORY = json.dumps([
    {"timestamp": "2026-01-01T00:00:00", "value": 0.85, "reason": "created"},
    {"timestamp": "2026-02-01T00:00:00", "value": 0.7, "reason": "reviewed"},
])
METADATA = json.dumps({"content_hash": "abcdef0123456789abcdef0123456789", "git_sha": "1234567890abcdef",
                       "note": "x"})

#: id -> (type, embedding, confidence_history, metadata)
NODES = {
    "ob_full": ("observation_factual", EMBEDDING, HISTORY, METADATA),
    "ob_null": ("observation_factual", None, None, None),      # a fresh node: NULL embedding and history
    "ob_badjson": ("observation_factual", "not-json", "[unterminated", None),
    "ob_emptymeta": ("derivation", EMBEDDING, "[]", "{}"),
}
FIELDS = sorted(["embedding", "confidence_history", "content_hash", "git_sha", "parsed_metadata"])


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


_TEMPLATE = {}


def _template_db() -> Path:
    """One real-schema store with the fixture rows, built once by the real schema code."""
    if "path" not in _TEMPLATE:
        d = Path(tempfile.mkdtemp(prefix="ir-tpl-", dir="/tmp"))
        db = d / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        for nid, (ntype, emb, hist, meta) in NODES.items():
            conn.execute(
                "INSERT INTO nodes (id, type, claim, embedding, confidence_history, metadata, created_at) "
                "VALUES (?, ?, 'a claim', ?, ?, ?, '2026-01-01T00:00:00')",
                (nid, ntype, emb, hist, meta),
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        _TEMPLATE["path"] = db
    return _TEMPLATE["path"]


@pytest.fixture
def home():
    """A SHORT home (the owner socket lives at <home>/run/engramd.sock; AF_UNIX paths are capped)."""
    d = Path(tempfile.mkdtemp(prefix="ir-", dir="/tmp"))
    shutil.copyfile(_template_db(), d / "knowledge.db")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def set_flag(home: Path, value=True) -> None:
    (home / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


def load_tool(home: Path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("inspect_raw_2652", str(TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cli(home: Path, *args: str):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    return subprocess.run([sys.executable, str(TOOL_PATH), *args], capture_output=True, text=True,
                          env=env, timeout=120)


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


class BadRowsOwner(FakeOwner):
    """A fake owner that answers the tool's row statement with the given rows (a ping is answered ok)."""

    def __init__(self, home, rows):
        super().__init__(home)
        self._rows = rows

    def _answer(self, request):
        sql = (request.get("params") or {}).get("sql") or ""
        if request.get("method") == "read" and "FROM nodes WHERE id = ?" in sql:
            return {"status": "ok", "result": self._rows}
        return super()._answer(request)


class RefusingOwner(FakeOwner):
    def __init__(self, home, answer):
        super().__init__(home)
        self._fixed = answer

    def _answer(self, request):
        if request.get("method") == "ping":
            return {"status": "ok", "result": "pong"}
        return self._fixed


def stderr_lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln.strip()]


def assert_typed_exit_3(r, *, needle: str = ""):
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == "", r.stdout
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    assert "InspectRawOwnerReadError" in lines[0], lines[0]
    # it never claims the node is absent: the not-found line is "ERROR: Node '<id>' not found in <db>"
    assert "ERROR: Node " not in r.stderr and "not found in" not in r.stderr, r.stderr
    if needle:
        assert needle in lines[0], lines[0]


# ---- P1 / P2: the template's columns ----------------------------------------------------------

def test_p1_every_row_column_exists_in_a_real_schema_store(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    finally:
        conn.close()
    assert set(mod._ROW_COLUMNS) <= have, set(mod._ROW_COLUMNS) - have


def test_p2_the_template_covers_every_column_the_tool_reads(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    assert set(mod._ROW_COLUMNS) >= {"type", "metadata"} | set(mod._TABLE_STRIPPED_COLS)
    assert "id" in mod._ROW_COLUMNS  # the WHERE needs it and the answer is checked against it


def test_p3_the_statement_is_the_explicit_template_built_from_the_one_column_tuple(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    assert tuple(mod._ROW_COLUMNS) == ("id", "type", "embedding", "confidence_history", "metadata")
    assert mod._ROW_SQL == ROW_SQL
    assert mod._ROW_SQL == f"SELECT {', '.join(mod._ROW_COLUMNS)} FROM nodes WHERE id = ?"
    assert "*" not in mod._ROW_SQL


# ---- T: registration ---------------------------------------------------------------------------

def test_t1_the_template_is_registered_under_the_tools_own_site():
    data = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    hits = [t for t in data["templates"] if t["sql"] == ROW_SQL]
    assert len(hits) == 1, "the row statement must be registered exactly once"
    assert hits[0]["sites"] == [SITE], hits[0]["sites"]
    assert hits[0]["key"] == hashlib.sha256(ROW_SQL.encode("utf-8")).hexdigest()


def test_t2_the_scan_config_names_the_function_as_a_root():
    cfg = json.loads((REPO_ROOT / "tools" / "engramd_read_scan.config.json").read_text())
    assert SITE in cfg["roots"]


# ---- G1 ------------------------------------------------------------------------------------------

def test_g1_the_file_has_no_connect_line_of_its_own():
    text = TOOL_PATH.read_text(encoding="utf-8")
    hits = [n for n, ln in enumerate(text.splitlines(), 1) if re.search(r"sqlite3\.connect\(", ln)]
    assert hits == [], hits


# ---- the in-process entry, flag defaults ---------------------------------------------------------

def test_e1_the_spec_names_the_tools_own_flag_and_error_class(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    assert issubclass(mod.InspectRawOwnerReadError, RuntimeError)
    assert mod.OWNER_FLAG_KEY == FLAG_KEY
    spec = mod._owner_spec()
    assert spec.flag_key == FLAG_KEY
    assert spec.error_cls is mod.InspectRawOwnerReadError
    assert "not found" in spec.never_empty


def test_e2_flag_absent_reads_directly_and_never_contacts_an_owner(home, monkeypatch):
    mod = load_tool(home, monkeypatch)  # no config.json: the flag is OFF
    with FakeOwner(home) as owner:
        row = mod.read_row(home / "knowledge.db", "ob_full")
    assert row is not None and row["id"] == "ob_full"
    assert owner.requests == []


def test_e3_a_missing_store_raises_file_not_found_with_the_flag_off(tmp_path, home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    with pytest.raises(FileNotFoundError):
        mod.read_row(tmp_path / "absent.db", "ob_full")


# ---- R1: read-only on the direct path ---------------------------------------------------------------

def test_r1_the_flag_off_connection_is_read_only_and_the_store_bytes_do_not_change(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    db = home / "knowledge.db"
    before = db.read_bytes()
    seen = []
    real_open = mod.owner_read.open_reader

    def probing_open(spec, db_path, use_owner):
        conn = real_open(spec, db_path, use_owner)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE nodes SET claim = 'changed'")
        seen.append(use_owner)
        return conn

    monkeypatch.setattr(mod.owner_read, "open_reader", probing_open)
    assert mod.read_row(db, "ob_full")["id"] == "ob_full"
    assert seen == [False]
    assert db.read_bytes() == before


def test_r1b_a_full_cli_run_leaves_the_store_bytes_unchanged(home):
    db = home / "knowledge.db"
    before = db.read_bytes()
    runs = [["ob_full"], ["ob_null"], ["ob_full", "--json"]]
    runs += [["ob_full", "--field", f] for f in FIELDS] + [["ob_full", "--field", f, "--json"] for f in FIELDS]
    for args in runs:
        r = cli(home, *args)
        assert r.returncode == 0, (args, r.stderr)
    assert db.read_bytes() == before
    assert sorted(p.name for p in home.iterdir() if p.name.startswith("knowledge.db")) == ["knowledge.db"]


# ---- O1: flag OFF vs flag ON, a REAL owner -----------------------------------------------------------

def _modes(node_id):
    modes = [[node_id]]
    for f in FIELDS:
        modes += [[node_id, "--field", f], [node_id, "--field", f, "--json"]]
    return modes


@requires_linux_owner
def test_o1_flag_off_and_flag_on_through_a_real_owner_print_identical_bytes(home, monkeypatch):
    sent = []
    real_handle = eo.EngramdOwner._handle_read

    def recording(self, params, deadline):
        sent.append(params.get("sql"))
        return real_handle(self, params, deadline)

    monkeypatch.setattr(eo.EngramdOwner, "_handle_read", recording)
    off, on = {}, {}
    set_flag(home, False)
    for nid in NODES:
        for args in _modes(nid):
            off[tuple(args)] = cli(home, *args)
    set_flag(home, True)
    with real_owner(home):
        for nid in NODES:
            for args in _modes(nid):
                on[tuple(args)] = cli(home, *args)
    assert off.keys() == on.keys() and len(off) == 4 * (1 + 2 * len(FIELDS))
    # the ON leg really went through the owner: one registered row read per ON run, the flag-OFF legs none
    assert sent == [ROW_SQL] * len(on), (len(sent), len(on), set(sent))
    for key, a in off.items():
        b = on[key]
        assert (a.returncode, a.stdout, a.stderr) == (b.returncode, b.stdout, b.stderr), key
        assert a.returncode == 0 and a.stdout, (key, a.stderr)
    # the comparison is not vacuous: the fixture's content actually reaches stdout
    assert "content_hash" in off[("ob_full",)].stdout
    assert "'abcdef0123456789abcdef0123456789'" not in off[("ob_full",)].stdout  # the list view truncates
    assert json.loads(off[("ob_full", "--field", "embedding", "--json")].stdout) == json.loads(EMBEDDING)
    # the NULL row (fresh node) prints the same "null" lines in both flag states
    assert "null (not set for this node)" in off[("ob_null",)].stdout
    assert off[("ob_null", "--field", "embedding", "--json")].stdout == "null\n"
    assert on[("ob_null", "--field", "confidence_history")].stdout == (
        "Field 'confidence_history' is null (not set for node ob_null)\n")


# ---- O2: owner down -----------------------------------------------------------------------------------

def test_o2_flag_on_owner_down_exits_3_with_empty_stdout_and_one_typed_line(home):
    set_flag(home, True)
    for args in (["ob_full"], ["ob_full", "--field", "embedding"], ["ob_full", "--field", "embedding", "--json"]):
        r = cli(home, *args)
        assert_typed_exit_3(r, needle="unavailable")
        assert f"set engramd.{FLAG_KEY} to false" in r.stderr, r.stderr  # the helper's own remedy


def test_o2b_flag_on_owner_down_read_row_raises_and_never_returns_none(home, monkeypatch):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with pytest.raises(mod.InspectRawOwnerReadError) as exc:
        mod.read_row(home / "knowledge.db", "ob_full")
    assert "not found" in str(exc.value)  # the never_empty sentence says what it did NOT answer
    assert "engramd." + FLAG_KEY in str(exc.value)


# ---- O3: the owner refuses ----------------------------------------------------------------------------

REFUSALS = [
    {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "unregistered_template", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "refused_at_load", "detail": "d", "remedy": "r"},
    {"status": "ok", "result": "not-a-list"},
]


@pytest.mark.parametrize("answer", REFUSALS, ids=lambda a: a.get("reason", "ok_not_a_list"))
def test_o3_a_refusing_owner_raises_typed_in_process_and_exits_3_from_the_cli(home, monkeypatch, answer):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with RefusingOwner(home, answer):
        with pytest.raises(mod.InspectRawOwnerReadError):
            mod.read_row(home / "knowledge.db", "ob_full")
        assert_typed_exit_3(cli(home, "ob_full"))
        assert_typed_exit_3(cli(home, "ob_full", "--field", "embedding", "--json"))


# ---- O4: malformed answers ----------------------------------------------------------------------------

GOOD = ["ob_full", "observation_factual", EMBEDDING, HISTORY, METADATA]
MALFORMED = {
    "two_rows": [GOOD, GOOD],
    "four_cells": [GOOD[:4]],
    "six_cells": [GOOD + ["extra"]],
    "another_id": [["ob_other"] + GOOD[1:]],
    "non_str_id": [[7] + GOOD[1:]],
    "non_str_type": [["ob_full", 7] + GOOD[2:]],
    "null_type": [["ob_full", None] + GOOD[2:]],
    "int_embedding": [GOOD[:2] + [5] + GOOD[3:]],
    "float_history": [GOOD[:3] + [1.5, GOOD[4]]],
    "int_metadata": [GOOD[:4] + [0]],
    "case_only_id": [["OB_FULL"] + GOOD[1:]],   # the owner answered another id that differs only by case
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_o4_a_malformed_row_raises_typed_never_a_partial_dict(home, monkeypatch, case):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with BadRowsOwner(home, MALFORMED[case]):
        with pytest.raises(mod.InspectRawOwnerReadError) as exc:
            mod.read_row(home / "knowledge.db", "ob_full")
        assert "engramd." + FLAG_KEY in str(exc.value)
        assert_typed_exit_3(cli(home, "ob_full"))


def test_o4b_a_wellformed_answer_with_null_cells_is_accepted(home, monkeypatch):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with BadRowsOwner(home, [["ob_null", "observation_factual", None, None, None]]):
        row = mod.read_row(home / "knowledge.db", "ob_null")
    assert row == {"id": "ob_null", "type": "observation_factual", "embedding": None,
                   "confidence_history": None, "metadata": None}


# ---- O5: a BLOB cell ----------------------------------------------------------------------------------

@requires_linux_owner
def test_o5_a_blob_embedding_through_the_real_owner_is_a_typed_refusal(home, monkeypatch):
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("UPDATE nodes SET embedding = ? WHERE id = 'ob_full'", (b"\x00\x01\x02\xff",))
    conn.commit()
    assert conn.execute("SELECT typeof(embedding) FROM nodes WHERE id = 'ob_full'").fetchone()[0] == "blob"
    conn.close()
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with real_owner(home):
        with pytest.raises(mod.InspectRawOwnerReadError) as exc:
            mod.read_row(home / "knowledge.db", "ob_full")
        assert "unsupported_value_type" in str(exc.value)
        for args in (["ob_full"], ["ob_full", "--field", "embedding"]):
            assert_typed_exit_3(cli(home, *args), needle="unsupported_value_type")
        # every field of the BLOB row is refused (the row is not read), while another row still reads:
        # the refusal is the one row's, not the owner's
        assert cli(home, "ob_full", "--field", "git_sha").returncode == 3
        assert cli(home, "ob_null").returncode == 0


def _blob_the_embedding(home: Path) -> None:
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("UPDATE nodes SET embedding = ? WHERE id = 'ob_full'", (b"\x00\x01\x02\xff",))
    conn.commit()
    conn.close()


def test_o5b_flag_off_a_blob_row_exits_3_and_names_no_owner_remedy(home):
    """A row that does not fit is refused on BOTH flag values; with the flag already OFF there is no owner, so
    the flag-and-owner remedy would be false and must not be appended."""
    _blob_the_embedding(home)
    for args in (["ob_full"], ["ob_full", "--field", "embedding"]):
        r = cli(home, *args)
        assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
        assert r.stdout == "", r.stdout
        assert "the store answered" in r.stderr, r.stderr
        assert "InspectRawOwnerReadError" in r.stderr, r.stderr
        assert "owner is healthy" not in r.stderr, r.stderr
        assert "to false to read the store directly" not in r.stderr, r.stderr
        assert "Flag engramd." not in r.stderr, r.stderr
        assert len(stderr_lines(r.stderr)) == 1, r.stderr


def test_o5c_flag_on_a_row_that_does_not_fit_still_names_the_flag_and_the_remedy(home):
    """The flag-ON counterpart of O5b, on every platform (a fake owner answers a BLOB-shaped cell)."""
    set_flag(home, True)
    with BadRowsOwner(home, [["ob_full", "observation_factual", [0, 1], None, None]]):
        r = cli(home, "ob_full")
    assert_typed_exit_3(r)
    assert f"engramd.{FLAG_KEY}" in r.stderr, r.stderr
    assert f"retry once the owner is healthy, or set engramd.{FLAG_KEY} to false to read the store directly" \
        in r.stderr, r.stderr


@requires_linux_owner
def test_o5d_flag_on_a_blob_row_through_the_real_owner_names_the_flag_and_the_remedy(home):
    _blob_the_embedding(home)
    set_flag(home, True)
    with real_owner(home):
        r = cli(home, "ob_full")
    assert_typed_exit_3(r, needle="unsupported_value_type")
    assert f"set engramd.{FLAG_KEY} to false" in r.stderr, r.stderr


# ---- O6: not found stays not found --------------------------------------------------------------------

@requires_linux_owner
def test_o6_a_real_owner_answering_zero_rows_is_not_found_exit_1(home, monkeypatch):
    set_flag(home, True)
    with real_owner(home):
        r = cli(home, "ob_absent")
        mod = load_tool(home, monkeypatch)
        assert mod.read_row(home / "knowledge.db", "ob_absent") is None
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert r.stdout == ""
    assert "not found" in r.stderr and "ob_absent" in r.stderr
    assert "InspectRawOwnerReadError" not in r.stderr


def test_o6b_flag_off_not_found_and_a_missing_store_keep_exit_1(home, tmp_path):
    r = cli(home, "ob_absent")
    assert (r.returncode, r.stdout) == (1, "")
    assert "ERROR: Node 'ob_absent' not found in " in r.stderr
    empty = Path(tempfile.mkdtemp(prefix="ir-none-", dir="/tmp"))
    try:
        r = cli(empty, "ob_full")
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    assert (r.returncode, r.stdout) == (1, "")
    assert "ERROR: DB not found at " in r.stderr and "Set ENGRAM_HOME" in r.stderr


# ---- exit 3 is new: nothing else in the CLI may use it ------------------------------------------------

def test_x1_exit_code_3_is_only_the_owner_read_failure(home, monkeypatch):
    """Before this unit the CLI used 1 (not found, unknown field) and argparse's 2; 3 is new and means one thing."""
    mod = load_tool(home, monkeypatch)
    assert mod.OWNER_READ_FAILED_EXIT == 3
    src = TOOL_PATH.read_text(encoding="utf-8")
    exits = re.findall(r"sys\.exit\(([^)]*)\)", src)
    assert exits.count("OWNER_READ_FAILED_EXIT") == 1, exits
    assert set(exits) == {"1", "OWNER_READ_FAILED_EXIT", "BAD_HOME_EXIT", "BAD_ID_EXIT"}, exits
    assert exits.count("BAD_HOME_EXIT") == 1 and exits.count("BAD_ID_EXIT") == 1, exits
    assert mod.BAD_HOME_EXIT == 2 and mod.BAD_ID_EXIT == 2
    assert not re.search(r"\b(?:exit|_exit|SystemExit)\(\s*3\s*\)", src)


# ---- H1: a relative ENGRAM_HOME is refused cleanly (alpha #3863) --------------------------------------
# The repo rule (tools/_owner_read.py engram_home(), pinned by tests/test_relative_engram_home_refused_3863.py):
# a relative home resolves against the caller's cwd, so it is refused, never accepted.  This tool's exit 1
# means "node not found", so the refusal takes argparse's 2 (a misconfigured call, before any read).

def _cli_relative(home: Path, *args: str, flag: bool = False):
    if flag:
        set_flag(home, True)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = home.name  # RELATIVE: resolves to a real store under cwd=home.parent
    return subprocess.run([sys.executable, str(TOOL_PATH), *args], capture_output=True, text=True,
                          env=env, cwd=str(home.parent), timeout=120)


@pytest.mark.parametrize("flag", [False, True], ids=["flag_off", "flag_on"])
@pytest.mark.parametrize("args", [["ob_full"], ["ob_full", "--field", "git_sha"], ["ob_full", "--json"]])
def test_h1_a_relative_engram_home_is_a_clean_refusal_never_a_traceback_never_exit_1(home, args, flag):
    r = _cli_relative(home, *args, flag=flag)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == "", r.stdout
    assert "Traceback" not in r.stderr, r.stderr
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    assert "ENGRAM_HOME must be an absolute path" in lines[0] and home.name in lines[0], lines[0]
    assert "not found" not in lines[0], lines[0]


def test_h1b_an_absolute_engram_home_is_unaffected(home):
    """Control: the same store through an absolute home still reads (exit 0, the field on stdout)."""
    r = cli(home, "ob_full", "--field", "git_sha")
    assert (r.returncode, r.stderr) == (0, "")
    assert r.stdout.strip() != ""


# ---- V1: the validation-failure message carries the never-empty guard sentence ------------------------

NEVER_EMPTY = "Not answering 'node not found': that would read as the node not existing in the store."


@pytest.mark.parametrize("case", ["two_rows", "four_cells", "another_id"])
def test_v1_flag_on_validation_failure_message_carries_the_never_empty_sentence(home, monkeypatch, case):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with BadRowsOwner(home, MALFORMED[case]):
        with pytest.raises(mod.InspectRawOwnerReadError) as exc:
            mod.read_row(home / "knowledge.db", "ob_full")
    assert NEVER_EMPTY in str(exc.value), str(exc.value)


def test_v1b_flag_off_validation_failure_message_carries_the_never_empty_sentence(home, monkeypatch):
    _blob_the_embedding(home)
    mod = load_tool(home, monkeypatch)
    with pytest.raises(mod.InspectRawOwnerReadError) as exc:
        mod.read_row(home / "knowledge.db", "ob_full")
    assert NEVER_EMPTY in str(exc.value), str(exc.value)


# ---- F1: owner-read helper failures pass through verbatim (round 5) -------------------------------------
# Every helper failure kind already carries its own complete remedy; read_row appends nothing.  Expected text
# is built from the helper itself, so these cells check pass-through, not wording.

REMEDY = f"set engramd.{FLAG_KEY} to false"
REFUSED_AT_LOAD = {"status": "rejected", "reason": "refused_at_load", "detail": "d", "remedy": "r"}
RESPONDING_KINDS = {
    "unregistered_template": {"status": "rejected", "reason": "unregistered_template", "detail": "d", "remedy": "r"},
    "generic_rejected": {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"},
    "unusable_answer": {"status": "ok", "result": "not-a-list"},
    "refused_at_load": REFUSED_AT_LOAD,
}


def _helper_message(mod, home: Path) -> str:
    """The message the owner-read helper itself raises in the CURRENT state (ping, then the row read)."""
    spec = mod._owner_spec()
    try:
        mod.owner_read.ping(spec)
        reader = mod.owner_read.open_reader(spec, home / "knowledge.db", True)
        reader.execute(ROW_SQL, ("ob_full",)).fetchall()
    except mod.InspectRawOwnerReadError as exc:
        return str(exc)
    raise AssertionError("the helper did not fail in this state")


def _expected_line(helper_msg: str) -> str:
    return "ERROR: InspectRawOwnerReadError: " + " ".join(helper_msg.split()) + "\n"


def _assert_passthrough(stderr: str, stdout: str, code, helper_msg: str) -> None:
    assert code == 3, (code, stdout, stderr)
    assert stdout == "", stdout
    assert stderr == _expected_line(helper_msg), (stderr, _expected_line(helper_msg))


def test_f1_owner_down_passes_the_helper_message_through_verbatim(home, monkeypatch):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    helper = _helper_message(mod, home)
    r = cli(home, "ob_full")
    _assert_passthrough(r.stderr, r.stdout, r.returncode, helper)
    with pytest.raises(mod.InspectRawOwnerReadError) as exc:
        mod.read_row(home / "knowledge.db", "ob_full")
    assert str(exc.value) == helper


@pytest.mark.parametrize("kind", sorted(RESPONDING_KINDS))
def test_f1_a_refusing_owner_passes_the_helper_message_through_verbatim(home, monkeypatch, kind):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with RefusingOwner(home, RESPONDING_KINDS[kind]):
        helper = _helper_message(mod, home)
        r = cli(home, "ob_full")
        _assert_passthrough(r.stderr, r.stdout, r.returncode, helper)
        r2 = cli(home, "ob_full", "--field", "embedding", "--json")
        _assert_passthrough(r2.stderr, r2.stdout, r2.returncode, helper)
        with pytest.raises(mod.InspectRawOwnerReadError) as exc:
            mod.read_row(home / "knowledge.db", "ob_full")
        assert str(exc.value) == helper
    if kind == "refused_at_load":
        # the helper says a restart alone will not fix it and gives a migrate remedy: no flag, no "retry"
        assert "retry once the owner is healthy" not in r.stderr, r.stderr
        assert "Flag engramd." not in r.stderr, r.stderr


def _run_get_row(mod, capsys):
    with pytest.raises(SystemExit) as ex:
        mod._get_row("ob_full")
    out, err = capsys.readouterr()
    return err, out, ex.value.code


def test_f1_ping_failed_passes_the_helper_message_through_verbatim(home, monkeypatch, capsys):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    real_import = mod.owner_read.import_runtime

    class _Client:
        def __getattr__(self, name):
            return getattr(real_import("engramd_client"), name)

        def read(self, *a, **k):
            raise RuntimeError("boom")

    def fake_import(name):
        return _Client() if name == "engramd_client" else real_import(name)

    monkeypatch.setattr(mod.owner_read, "import_runtime", fake_import)
    helper = _helper_message(mod, home)
    assert "ping failed" in helper  # the provocation really is the ping-failed kind
    err, out, code = _run_get_row(mod, capsys)
    _assert_passthrough(err, out, code, helper)


def test_f1_client_not_loadable_passes_through_and_names_the_flag_false_remedy_exactly_once(
        home, monkeypatch, capsys):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    real_import = mod.owner_read.import_runtime

    def fake_import(name):
        if name == "engramd_client":
            raise ImportError("no client")
        return real_import(name)

    monkeypatch.setattr(mod.owner_read, "import_runtime", fake_import)
    helper = _helper_message(mod, home)
    assert "could not be loaded" in helper
    err, out, code = _run_get_row(mod, capsys)
    _assert_passthrough(err, out, code, helper)
    assert err.count("set the flag to false") == 1, err
    assert "retry once the owner is healthy" not in err, err


def test_f1c_flag_off_read_row_failure_names_neither_the_owner_nor_the_flag(home, monkeypatch):
    _blob_the_embedding(home)
    mod = load_tool(home, monkeypatch)
    with pytest.raises(mod.InspectRawOwnerReadError) as exc:
        mod.read_row(home / "knowledge.db", "ob_full")
    for text in ("owner", FLAG_KEY, "engramd."):
        assert text not in str(exc.value), (text, str(exc.value))


# ---- N1: a handler is as wide as its message (round 4) --------------------------------------------------
# `_get_row` used to turn ANY ValueError out of read_row into "ENGRAM_HOME must be absolute" (exit 2).  A node
# id that is not valid UTF-8 (a stray argv byte, which Python turns into a lone surrogate) makes sqlite3 raise
# UnicodeEncodeError, a ValueError, into that handler.  Such an id cannot name a node (ids are UTF-8 text), so
# it is refused BEFORE any read as a usage error (exit 2, the id named as not encodable), on both flag values,
# with no owner contact (flag ON and no owner running must NOT end in the owner-down exit 3).

BAD_ID = b"ob_\xff"


def _cli_bytes_id(home: Path, node_id: bytes, *, flag: bool):
    set_flag(home, flag)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    return subprocess.run([sys.executable, str(TOOL_PATH), node_id], capture_output=True, env=env, timeout=120)


@pytest.mark.parametrize("flag", [False, True], ids=["flag_off", "flag_on"])
def test_n1_a_non_utf8_node_id_is_a_usage_refusal_naming_the_id_never_a_home_message_never_a_traceback(home, flag):
    r = _cli_bytes_id(home, BAD_ID, flag=flag)   # flag ON: no owner is running, so any owner contact would exit 3
    err = r.stderr.decode("utf-8", "replace")
    assert r.returncode == 2, (r.returncode, r.stdout, err)
    assert r.stdout == b"", r.stdout
    assert "Traceback" not in err, err
    lines = stderr_lines(err)
    assert len(lines) == 1, err
    assert "not valid UTF-8" in lines[0] and "ob_" in lines[0], lines[0]
    for text in ("ENGRAM_HOME", "not found", "owner", "engramd"):
        assert text not in lines[0], (text, lines[0])


def test_n1b_a_valid_non_ascii_node_id_is_not_refused_as_unencodable(home):
    """Control: the refusal is for ids UTF-8 cannot encode, not for non-ASCII ids; this one reads and is not found."""
    r = _cli_bytes_id(home, "ob_é".encode("utf-8"), flag=False)
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert b"not found" in r.stderr and b"not valid UTF-8" not in r.stderr, r.stderr


def test_n2_get_row_does_not_report_an_arbitrary_value_error_as_a_relative_home(home, monkeypatch):
    """The handler's scope is the one refusal its message names: a ValueError out of the READ is not a home
    problem and must not be dressed as one (it propagates; it is never exit 2 with the home sentence)."""
    mod = load_tool(home, monkeypatch)

    def boom(*_a, **_k):
        raise ValueError("some other value error")

    monkeypatch.setattr(mod, "read_row", boom)
    with pytest.raises(ValueError, match="some other value error"):
        mod._get_row("ob_full")
