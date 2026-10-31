"""engram-alpha#3881 (U18 of #2652) -- tools/anchoring_report.py (and its second caller
tools/quote_quality_survey.py) read the whole current-observation population through the shared owner-read
helper, behind engramd.anchoring_report_reads_via_owner (default OFF), in keyset pages by id, with the node id
as the LAST element of the worst-N sort key.

Contract (each cell written before the implementation and red against the unmodified tree):

  P1  every column of both templates exists in a store built by the real schema code
  P2  the templates cover every column `_row_result` and the survey's `build_records` read
  T1  both templates are registered byte for byte under the tool's site; T2 the scan config names the root
  T3  BOTH templates are ADMITTED by the production classifier (engramd_read_registry.classify_template): a plain
      unit cell, so a later change of the SQL cannot drift into a refusal only a real owner would show
  T4  the page size is one number: PAGE_ROWS, the literal in the SQL bytes and the registered JSON agree
  D1  flag OFF vs flag ON through a REAL owner at the REAL page size (200): byte-identical stdout (json with
      generated_at and db masked, and human), over the sizes 0, 1, 199, 200, 201, 400, 603, ties out of id order
      inside a page and straddling the 200 and 400 boundaries and at the end, a quote_type subset that is a
      multiple of the page size, NULL / empty / non-ASCII / long cells. No owner-reaching cell patches PAGE_ROWS.
  D2  the survey, flag OFF vs flag ON: byte-identical json, judging sheet and (masked) key
  D3  one end-to-end subprocess run at the REAL PAGE_ROWS over more than two pages
  S1  the id is the LAST sort part and ascending (end to end, and on `_worst_n` directly)
  S1e a --top cut through a group of rows tied on the first three parts keeps the LOWEST ids, ascending, and the
      bytes are identical flag OFF and flag ON through a real owner (store holds the tied ids out of id order)
  B1  no bulk reply: no reply over PAGE_ROWS rows, and N // PAGE_ROWS + 1 replies
  C1  complete paging: every id exactly once
  O1  owner down, flag ON: exit 3, empty stdout, one stderr line, ZERO connects to the store
  O1b flag ON pings the owner BEFORE any page is read: the owner's FIRST request is the ping (a page request
      follows), and a ping that fails is the run's failure (exit 3, empty stdout, one line naming the ping)
  O2  a failure on page 1, a middle page and the LAST page (refusal, or the owner dying): exit 3, empty stdout
  O3  every refusal kind: typed error, exit 3
  O4  malformed answers each raise the typed error and terminate (a repeated page included)
  O5  a real owner answering zero rows is a population of 0 and a normal report (exit 0)
  O4d a boolean `confidence` cell (true and false) is a typed error (exit 3), shown on a stub connection that hands
      the page to the tool directly: the owner client's own cell check refuses a bool first, so only a stub reaches
      the tool's check
  Q1  `--quote-type ""` means NO filter: byte-identical to no flag, flag OFF and flag ON through a real owner,
      on a store holding several quote types
  H2s the survey's store-class exit codes, flag OFF: a missing store is 1 ("DB not found"), a directory and a file
      that is not a database are 3, one stderr line, empty stdout, no sheet or key written
  K1  `_open_population_reader`'s connection is closed exactly once by anchoring_report.main and by the survey's
      main, on success and when the read fails
  R1  flag OFF is read-only; G1 the new path holds no connect call; X1 `_open_readonly` and s2_clause_sheet
      are untouched with the flag ON; H1 a relative ENGRAM_HOME is refused (exit 2); F1 --db elsewhere stays direct
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import signal
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

TOOL_PATH = REPO_ROOT / "src" / "engram" / "tools" / "anchoring_report.py"
SURVEY_PATH = REPO_ROOT / "src" / "engram" / "tools" / "quote_quality_survey.py"
S2_PATH = REPO_ROOT / "src" / "engram" / "tools" / "s2_clause_sheet.py"
FLAG_KEY = "anchoring_report_reads_via_owner"
SITE = "tools/anchoring_report.py:_fetch_population"

_COLS = "id, quote_type, confidence, claim, quoted_text, interpretation, metadata, created_at"
_TYPES = "('observation_factual', 'observation_predictive')"
SQL_ALL = f"SELECT {_COLS} FROM nodes WHERE type IN {_TYPES} AND is_current = 1 AND id > ? ORDER BY id LIMIT 200"
SQL_BY_TYPE = (f"SELECT {_COLS} FROM nodes WHERE type IN {_TYPES} AND is_current = 1 AND quote_type = ? "
               "AND id > ? ORDER BY id LIMIT 200")
COLUMNS = ("id", "quote_type", "confidence", "claim", "quoted_text", "interpretation", "metadata", "created_at")
P = 200  # the REAL page size: no owner-reaching cell patches PAGE_ROWS

#: the one place the population cells wait on a possible hang (a repeated page must raise, not loop)
HANG_SECONDS = 30


@pytest.fixture(autouse=True)
def _hang_guard():
    """Every cell ends (red, with a TimeoutError) rather than hangs: a paging loop that never advances its cursor
    must fail a cell, not wedge the run. 120 s is far above the slowest honest cell (a few seconds)."""
    def boom(*_a):
        raise TimeoutError("cell still running after 120 s (a paging loop that does not terminate?)")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(120)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


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


@contextlib.contextmanager
def deadline(seconds=HANG_SECONDS):
    def boom(*_a):
        raise TimeoutError(f"still running after {seconds} s (a loop that does not terminate?)")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ---- fixture stores, built by the real schema code ---------------------------------------------------

_TEMPLATE = {}


def _template_db() -> Path:
    if "path" not in _TEMPLATE:
        d = Path(tempfile.mkdtemp(prefix="ar-tpl-", dir="/tmp"))
        db = d / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()
        _TEMPLATE["path"] = db
    return _TEMPLATE["path"]


def new_home() -> Path:
    """A SHORT home (the owner socket lives at <home>/run/engramd.sock; AF_UNIX paths are capped)."""
    d = Path(tempfile.mkdtemp(prefix="ar-", dir="/tmp"))
    shutil.copyfile(_template_db(), d / "knowledge.db")
    return d


@pytest.fixture
def home():
    d = new_home()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def oid(i: int) -> str:
    return f"ob_{i:05d}"


LONG_CLAIM = ("a very long claim " * 1200) + " ended at 6642"


def build_rows(n: int, page: int, hard_data_count: int | None = None):
    """n current observations with ids ob_00000.. in id order, in INSERTION order (the returned list order).

    Dimensions held varied on purpose: kinds of anchoring (none / partial / none anchored), NULL and empty
    cells, non-ASCII text, one very long claim, quote_type present or NULL, three tie groups at three
    positions (inside one page, straddling the first and the second page boundary, at the very end) inserted in DESCENDING id
    order, everything else in a seeded shuffle (so no row is inserted in id order by accident)."""
    rng = random.Random(1234)
    if hard_data_count is None:
        hd = {i for i in range(n) if i % 2 == 0}
    else:
        hd = set(random.Random(5).sample(range(n), min(hard_data_count, n)))
    others = ["official_statement", "editorial", None]
    groups = {"T1": [2, 3, 4], "T2": [page - 1, page, page + 1], "T2b": [2 * page - 1, 2 * page, 2 * page + 1],
              "T3": [n - 3, n - 2, n - 1]}
    taken, tie_of = set(), {}
    for g, idxs in groups.items():
        keep = [i for i in idxs if 0 <= i < n and i not in taken]
        taken.update(keep)
        groups[g] = keep
        for i in keep:
            tie_of[i] = g
    tie_conf = {"T1": 0.91, "T2": 0.92, "T2b": 0.94, "T3": 0.93}
    rows = {}
    for i in range(n):
        k = i % 5
        if i in tie_of:
            claim, quote, interp, conf = "reached 1111 and 2222 and 3333 and 4444", "x", "ti", tie_conf[tie_of[i]]
        elif k == 0:
            claim, quote, interp, conf = "plain words only", "plain words only", "i", 0.85
        elif k == 1:
            claim, quote, interp, conf = "reached 4821 units", "no number here", "ii", 0.7
        elif k == 2:
            claim, quote, interp, conf = "reached 4821 and 9137", "only 9137 here", "iii", None
        elif k == 3:
            claim, quote, interp, conf = "reached 4821 and 9137 and 7713", "nothing", "iv", 0.95
        else:
            claim, quote, interp, conf = "naïve 日本語 5521", "日本語", None, 0.5
        if i == 6 and i not in tie_of:
            claim = LONG_CLAIM
        if i == 1 and i not in tie_of:
            quote = None          # NULL quoted_text
        if i == 8 and i not in tie_of:
            interp = ""           # empty interpretation
        qt = "hard_data" if i in hd else others[i % 3]
        rows[i] = dict(
            id=oid(i), type="observation_predictive" if i % 7 == 3 else "observation_factual",
            quote_type=qt, confidence=conf, claim=claim, quoted_text=quote, interpretation=interp,
            metadata=json.dumps({"source_class": "primary"}) if i % 4 == 0 else None,
            created_at=f"2026-01-{(i % 27) + 1:02d}T00:00:00", is_current=1)
    order = list(range(n))
    rng.shuffle(order)
    for g, idxs in groups.items():
        slots = sorted(order.index(i) for i in idxs)
        for slot, i in zip(slots, sorted(idxs, reverse=True)):
            order[slot] = i
    out = [rows[i] for i in order]
    # rows the population must NOT contain: not current, wrong type; ids sort before and after the real ones
    for nid, ntype, cur in (("aa_noise_old", "observation_factual", 0), ("zz_noise_old", "observation_factual", 0),
                            ("zz_noise_dv", "derivation", 1)):
        out.append(dict(id=nid, type=ntype, quote_type="hard_data", confidence=0.5, claim="noise 31337",
                        quoted_text="n", interpretation="n", metadata=None,
                        created_at="2026-01-01T00:00:00", is_current=cur))
    return out, groups


def populate(home: Path, rows) -> None:
    conn = sqlite3.connect(str(home / "knowledge.db"))
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, quote_type, confidence, quoted_text, interpretation, "
            "metadata, is_current) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["type"], r["claim"], r["created_at"], r["quote_type"], r["confidence"],
             r["quoted_text"], r["interpretation"], r["metadata"], r["is_current"]))
    conn.commit()
    conn.close()


def population_ids(rows, quote_type=None):
    return sorted(r["id"] for r in rows if r["is_current"] == 1
                  and r["type"] in ("observation_factual", "observation_predictive")
                  and (quote_type is None or r["quote_type"] == quote_type))


def set_flag(home: Path, value=True) -> None:
    (home / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


def load_tool(home: Path, monkeypatch, path=TOOL_PATH, name="anchoring_report_3881"):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cli(home: Path, *args: str, path=TOOL_PATH, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home if cwd is None else home.name)
    env["ENGRAM_NO_EMBEDDINGS"] = "1"
    return subprocess.run([sys.executable, str(path), *args], capture_output=True, text=True, env=env,
                          timeout=180, cwd=cwd)


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


class Recorder:
    """Counts what the tool asked the REAL owner (every registered population statement) and how many rows each
    reply carried (wraps the owner's own read handler)."""

    def __init__(self, monkeypatch):
        self.sql, self.rows = [], []
        real = eo.EngramdOwner._handle_read
        rec = self

        def recording(self_, params, deadline_):
            ans = real(self_, params, deadline_)
            if params.get("sql") in (SQL_ALL, SQL_BY_TYPE):
                rec.sql.append(params.get("sql"))
                rec.rows.append(len(ans["result"]) if isinstance(ans, dict) and ans.get("status") == "ok" else None)
            return ans

        monkeypatch.setattr(eo.EngramdOwner, "_handle_read", recording)


def run_main(mod, capsys, *argv):
    rc = mod.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def mask(text: str) -> str:
    """The ONLY normalisation: the two fields that differ run to run (a clock reading and the db path)."""
    text = re.sub(r'("generated_at": )"[^"]*"', r'\1"<generated_at>"', text)
    return re.sub(r'("db": )"[^"]*"', r'\1"<db>"', text)


def stderr_lines(text: str) -> list:
    return [ln for ln in text.splitlines() if ln.strip()]


# The runtime prints this one line on every run of an interpreter that has no sqlite-vec module; it is not the tool's output.
KNOWN_OPTIONAL_DEP_WARNING = "[engram] sqlite-vec unavailable"


def tool_error_lines(text: str) -> list:
    return [ln for ln in stderr_lines(text) if ln.startswith("ERROR:")]


def foreign_stderr_lines(text: str) -> list:
    """Every non-blank stderr line that is neither the tool's own ERROR: line nor the known optional-dependency warning."""
    return [ln for ln in stderr_lines(text)
            if not ln.startswith("ERROR:") and not ln.startswith(KNOWN_OPTIONAL_DEP_WARNING)]


def has_one_error_and_nothing_foreign(text: str) -> bool:
    return len(tool_error_lines(text)) == 1 and foreign_stderr_lines(text) == []


def assert_typed_exit_3(rc, out, err, *, needle=""):
    assert rc == 3, (rc, out, err)
    assert out == "", out
    lines = stderr_lines(err)
    assert len(lines) == 1, err
    assert "AnchoringReportOwnerReadError" in lines[0], lines[0]
    if needle:
        assert needle in lines[0], lines[0]


# ---- P1 / P2 -------------------------------------------------------------------------------------------

def test_p1_every_column_of_both_templates_exists_in_a_real_schema_store(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    finally:
        conn.close()
    assert tuple(mod._COLUMNS) == COLUMNS
    assert set(mod._COLUMNS) <= have, set(mod._COLUMNS) - have
    for sql in (mod._POPULATION_SQL_ALL, mod._POPULATION_SQL_BY_TYPE):
        assert f"SELECT {', '.join(mod._COLUMNS)} FROM nodes" in sql
        conn = sqlite3.connect(str(home / "knowledge.db"))
        try:
            by_type = "quote_type = ?" in sql
            assert sql.count("?") == (2 if by_type else 1)   # the cursor, and the quote_type: nothing else
            conn.execute(sql, (["hard_data"] if by_type else []) + [""]).fetchall()
        finally:
            conn.close()


def _subscript_names(path: Path, func: str) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func)
    return {n.slice.value for n in ast.walk(fn) if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
            and n.value.id == "row" and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)}


def test_p2_the_templates_cover_every_column_the_two_readers_use(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    used = _subscript_names(TOOL_PATH, "_row_result") | _subscript_names(SURVEY_PATH, "build_records")
    assert used, "the AST scan found nothing: the instrument is blind"
    assert {"claim", "quoted_text", "interpretation", "id", "quote_type", "confidence", "metadata",
            "created_at"} <= used
    assert used <= set(mod._COLUMNS), used - set(mod._COLUMNS)


# ---- T --------------------------------------------------------------------------------------------------

def test_t1_both_templates_are_registered_byte_for_byte_under_the_tools_site():
    data = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    for sql in (SQL_ALL, SQL_BY_TYPE):
        hits = [t for t in data["templates"] if t["sql"] == sql]
        assert len(hits) == 1, sql
        assert hits[0]["sites"] == [SITE], hits[0]["sites"]
        assert hits[0]["key"] == hashlib.sha256(sql.encode("utf-8")).hexdigest()


def test_t2_the_scan_config_names_the_function_as_a_root():
    cfg = json.loads((REPO_ROOT / "tools" / "engramd_read_scan.config.json").read_text())
    assert SITE in cfg["roots"]


def test_t3_the_module_constants_are_the_registered_bytes(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    assert mod._POPULATION_SQL_ALL == SQL_ALL
    assert mod._POPULATION_SQL_BY_TYPE == SQL_BY_TYPE
    assert mod.PAGE_ROWS == 200


TEMPLATES_JSON = REPO_ROOT / "src" / "engram" / "engramd_read_templates.json"

#: The committed templates the production loader legitimately DROPS, each with the reason it is dropped by design.
#: sql -> (the loader's refusal reason, one line why). Established by loading the committed file through
#: `load_registry` on a store that has the vocab table (what a live store has after its first IDF call) and
#: reading `.refused`: exactly one template is dropped, and it is this one, by the virtual-table rule
#: (engramd_read_registry: "templates over virtual tables (FTS5, vec0) are not registrable; use a typed read
#: operation"). It travels as the typed `df` operation instead (tests/_fake_owner.py answers it the same way).
ALLOWED_LOAD_DROPS = {
    "SELECT term, doc FROM nodes_fts_vocab WHERE term IN (SELECT value FROM json_each(?))": (
        "virtual_table_template", "FTS5 vocab virtual table: not registrable, served by the typed df operation"),
}


def _load_committed_registry(home, path=TEMPLATES_JSON):
    """The owner's own load path (engramd_owner._load_read_registry): `load_registry(path, ro_conn_on_the_store)`,
    on a store built by the real schema code. The vocab table is added the way engram_idf adds it on a live store,
    so the IDF template meets the rule that drops it by design rather than a missing table."""
    import engram_idf
    import engramd_read_registry as reg

    rw = sqlite3.connect(str(home / "knowledge.db"))
    try:
        engram_idf.ensure_vocab_table(rw)
        rw.commit()
    finally:
        rw.close()
    conn = sqlite3.connect(f"file:{home / 'knowledge.db'}?mode=ro", uri=True)
    try:
        return reg.load_registry(path, conn)
    finally:
        conn.close()


def test_t3_every_committed_template_is_admitted_by_the_production_loader_except_the_allowlist(home):
    """Not only this unit's two templates: the whole committed file, through the same `load_registry` the owner
    calls at start-up (and the four units that check their own templates use). The allowlist is explicit and
    both ways: every committed template is admitted EXCEPT exactly the allowlist, and every allowlisted entry is
    really dropped (for the stated reason), so a stale entry goes red too."""
    import engramd_read_registry as reg

    entries = json.loads(TEMPLATES_JSON.read_text())["templates"]
    assert len(entries) > 40, "the committed list looks empty: the cell is blind"
    loaded = _load_committed_registry(home)
    by_sql = {e["sql"]: e for e in entries}
    assert set(ALLOWED_LOAD_DROPS) <= set(by_sql), set(ALLOWED_LOAD_DROPS) - set(by_sql)   # no stale entry
    for sql, (reason, _why) in ALLOWED_LOAD_DROPS.items():
        assert sql in loaded.refused, f"allowlisted but admitted: {sql[:80]}"
        assert loaded.refused[sql].reason == reason, (loaded.refused[sql].reason, reason)
    unexpected = {by_sql[sql]["key"]: (by_sql[sql]["sites"], r.reason, r.detail)
                  for sql, r in loaded.refused.items() if sql not in ALLOWED_LOAD_DROPS}
    assert unexpected == {}, unexpected
    admitted = set(loaded.templates)
    assert admitted == {e["key"] for e in entries if e["sql"] not in ALLOWED_LOAD_DROPS}   # nothing silently lost


def test_t3c_this_units_two_templates_are_admitted_by_the_production_loader(home, monkeypatch):
    import engramd_read_registry as reg

    mod = load_tool(home, monkeypatch)
    loaded = _load_committed_registry(home)
    for sql in (mod._POPULATION_SQL_ALL, mod._POPULATION_SQL_BY_TYPE):
        assert sql not in loaded.refused, loaded.refused.get(sql)
        assert reg.template_key(sql) in loaded.templates


def test_t3b_the_classifier_does_refuse_the_forms_t3_guards_against(home):
    """The control for T3: it can fail. The two forms the first draft used are refused by the same call."""
    import engramd_read_registry as reg

    conn = sqlite3.connect(f"file:{home / 'knowledge.db'}?mode=ro", uri=True)
    try:
        in_form = SQL_ALL.replace(_TYPES, "(?, ?)")
        bound_limit = SQL_ALL.replace("LIMIT 200", "LIMIT ?")
        assert reg.classify_template(in_form, conn).reason == "unsafe_placeholder"
        assert reg.classify_template(bound_limit, conn).reason == "unsafe_placeholder"
    finally:
        conn.close()


def test_t4_the_page_size_is_one_number_in_the_constant_the_sql_and_the_registry(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    assert mod.PAGE_ROWS == 200
    for sql in (mod._POPULATION_SQL_ALL, mod._POPULATION_SQL_BY_TYPE):
        assert sql.endswith(f" LIMIT {mod.PAGE_ROWS}"), sql
    data = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    ours = [t["sql"] for t in data["templates"] if t["sites"] == [SITE]]
    assert sorted(ours) == sorted([mod._POPULATION_SQL_ALL, mod._POPULATION_SQL_BY_TYPE])
    assert all(sql.endswith(f" LIMIT {mod.PAGE_ROWS}") for sql in ours)


# ---- G1 ---------------------------------------------------------------------------------------------------

def _functions(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _connect_calls(fn) -> list:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Attribute) and n.func.attr == "connect")
        or (isinstance(n.func, ast.Name) and n.func.id == "connect"))]


def _called_names(fn) -> set:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            out.add(n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", ""))
    return out


def test_g1_the_new_path_holds_no_connect_call_and_main_does_not_open_readonly():
    fns = _functions(TOOL_PATH)
    for name in ("_open_population_reader", "_fetch_population", "main"):
        assert name in fns, name
        assert _connect_calls(fns[name]) == [], name
    assert "_open_readonly" not in _called_names(fns["main"])
    assert "_open_population_reader" in _called_names(fns["main"])
    # by design the file keeps exactly ONE connect, in the direct-only helper the third tool borrows
    total = [(name, len(_connect_calls(fn))) for name, fn in fns.items() if _connect_calls(fn)]
    assert total == [("_open_readonly", 1)], total
    text = TOOL_PATH.read_text(encoding="utf-8")
    assert len(re.findall(r"sqlite3\.connect\(", text)) == 1


def test_g1b_the_survey_main_does_not_open_readonly_either():
    fns = _functions(SURVEY_PATH)
    assert "_open_readonly" not in _called_names(fns["main"])
    assert "_open_population_reader" in _called_names(fns["main"])
    assert _connect_calls(fns["main"]) == []


# ---- the in-process entry, flag defaults ---------------------------------------------------------------

def test_e1_the_spec_names_the_tools_own_flag_and_error_class(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    assert issubclass(mod.AnchoringReportOwnerReadError, RuntimeError)
    spec = mod._owner_spec()
    assert spec.flag_key == FLAG_KEY
    assert spec.error_cls is mod.AnchoringReportOwnerReadError
    assert "no observations" in spec.never_empty
    assert mod.OWNER_READ_FAILED_EXIT == 3 and mod.BAD_HOME_EXIT == 2


# ---- D1: flag OFF vs flag ON through a REAL owner -------------------------------------------------------

SIZES = [0, 1, P - 1, P, P + 1, 2 * P, 3 * P + 3]   # 0, 1, 199, 200, 201, 400, 603
RUNS = {
    "json_all": ["--format", "json", "--top", "1000000"],
    "json_qt": ["--format", "json", "--top", "1000000", "--quote-type", "hard_data"],
    "human_default_top": ["--format", "human"],
}


@requires_linux_owner
@pytest.mark.parametrize("n", SIZES)
def test_d1_flag_off_and_flag_on_through_a_real_owner_print_identical_bytes(home, monkeypatch, capsys, n):
    # a quote_type subset that is itself a multiple of the page size, whenever the population allows one
    hd = 2 * P if n >= 3 * P else None
    rows, groups = build_rows(n, P, hard_data_count=hd)
    populate(home, rows)
    db = str(home / "knowledge.db")
    mod = load_tool(home, monkeypatch)
    off, on = {}, {}
    set_flag(home, False)
    for name, argv in RUNS.items():
        off[name] = run_main(mod, capsys, "--db", db, *argv)
    set_flag(home, True)
    rec = Recorder(monkeypatch)
    with real_owner(home):
        for name, argv in RUNS.items():
            on[name] = run_main(mod, capsys, "--db", db, *argv)
    for name in RUNS:
        a, b = off[name], on[name]
        assert a[0] == 0 and b[0] == 0, (name, a, b)
        assert (mask(a[1]), a[2]) == (mask(b[1]), b[2]), name
    # the ON leg really went through the owner, in registered statements only
    assert rec.sql and set(rec.sql) <= {SQL_ALL, SQL_BY_TYPE}
    # non-vacuous: the fixture reaches stdout, and the tie groups print in ascending id order
    payload = json.loads(off["json_all"][1])
    assert payload["population"] == n
    ids = [w["id"] for w in payload["worst"]]
    assert sorted(ids) == population_ids(rows)
    for g, idxs in groups.items():
        listed = [i for i in ids if i in {oid(j) for j in idxs}]
        assert listed == sorted(oid(j) for j in idxs), (g, listed)
        if len(idxs) > 1:   # contiguous: ties are adjacent in the sorted list
            pos = [ids.index(i) for i in listed]
            assert pos == list(range(pos[0], pos[0] + len(pos))), (g, pos)
    qt = json.loads(off["json_qt"][1])
    assert sorted(w["id"] for w in qt["worst"]) == population_ids(rows, "hard_data")
    if hd:
        assert qt["population"] == 2 * P  # the strict subset is itself a multiple of the page size


@requires_linux_owner
def test_d3_one_end_to_end_cli_run_at_the_real_page_size_over_more_than_two_pages(home, monkeypatch):
    n = 3 * 200 + 3
    rows, _groups = build_rows(n, 200)
    populate(home, rows)
    argv = ["--format", "json", "--top", "1000000"]
    set_flag(home, False)
    off = cli(home, *argv)
    set_flag(home, True)
    rec = Recorder(monkeypatch)
    with real_owner(home):
        on = cli(home, *argv)
    assert off.returncode == 0 and on.returncode == 0, (off.stderr, on.stderr)
    assert rec.sql == [SQL_ALL] * 4 and rec.rows == [200, 200, 200, 3], rec.rows   # the ON run used the owner
    assert mask(off.stdout) == mask(on.stdout) and off.stderr == on.stderr
    payload = json.loads(on.stdout)
    assert payload["population"] == n
    assert sorted(w["id"] for w in payload["worst"]) == population_ids(rows)
    assert len(payload["worst"]) == n


# ---- D2: the survey ----------------------------------------------------------------------------------------

@requires_linux_owner
def test_d2_the_survey_flag_off_and_flag_on_print_identical_bytes(home, monkeypatch, capsys, tmp_path):
    n = 3 * P + 3
    rows, _g = build_rows(n, P)
    populate(home, rows)
    db = str(home / "knowledge.db")
    tool = load_tool(home, monkeypatch, SURVEY_PATH, "qqs_3881")

    def run(tag):
        sheet, key = tmp_path / f"sheet_{tag}.md", tmp_path / f"key_{tag}.json"
        rc, out, err = run_main(tool, capsys, "--db", db, "--format", "json", "--judging-sheet", str(sheet),
                                "--judging-key", str(key))
        assert rc == 0, err
        err = err.replace(str(sheet), "<sheet>").replace(str(key), "<key>")
        return mask(out), err, sheet.read_text(), mask(key.read_text())

    set_flag(home, False)
    off = run("off")
    set_flag(home, True)
    rec = Recorder(monkeypatch)
    with real_owner(home):
        on = run("on")
    assert off == on
    assert rec.sql and len(rec.sql) == n // P + 1 and set(rec.sql) == {SQL_ALL}
    assert json.loads(on[0])["meta"]["population"] == n
    assert on[2].strip()  # the sheet is not empty


def test_d2b_the_survey_does_not_depend_on_fetch_order(home, monkeypatch, capsys, tmp_path):
    """The tiebreak and the id-paged read must not move the survey: it re-sorts its records by id. Shown by
    running it once on the paged read and once on the OLD read (no ORDER BY, insertion order)."""
    n = 2 * P + 4
    rows, _g = build_rows(n, P)
    populate(home, rows)
    db = str(home / "knowledge.db")
    tool = load_tool(home, monkeypatch, SURVEY_PATH, "qqs_3881b")

    def run(tag):
        sheet, key = tmp_path / f"s_{tag}.md", tmp_path / f"k_{tag}.json"
        rc, out, err = run_main(tool, capsys, "--db", db, "--format", "json", "--judging-sheet", str(sheet),
                                "--judging-key", str(key))
        assert rc == 0, err
        return mask(out), sheet.read_text(), mask(key.read_text())

    paged = run("paged")

    def legacy(conn, quote_type):
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        try:
            return c.execute("SELECT " + _COLS + " FROM nodes WHERE type IN (?, ?) AND is_current = 1",
                             ("observation_factual", "observation_predictive")).fetchall()
        finally:
            c.close()

    monkeypatch.setattr(tool, "_fetch_population", legacy)
    assert run("legacy") == paged


# ---- S1: the id is the LAST sort part and ascending ----------------------------------------------------------

def _row(nid, claim="reached 4821 units", quote="nothing", conf=0.7, qt="hard_data"):
    return dict(id=nid, type="observation_factual", quote_type=qt, confidence=conf, claim=claim, quoted_text=quote,
                interpretation="i", metadata=None, created_at="2026-01-01T00:00:00", is_current=1)


def test_s1_tied_rows_list_by_ascending_id_end_to_end(home, monkeypatch, capsys):
    populate(home, [_row("ob_00030"), _row("ob_00010"), _row("ob_00020")])  # inserted c, a, b
    mod = load_tool(home, monkeypatch)
    rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert rc == 0, err
    assert [w["id"] for w in json.loads(out)["worst"]] == ["ob_00010", "ob_00020", "ob_00030"]


def test_s1b_worst_n_orders_a_tie_by_ascending_id_whatever_the_input_order(home, monkeypatch):
    """Paging by id already hands `_worst_n` ties in id order, so only a direct call proves the key itself."""
    mod = load_tool(home, monkeypatch)

    def res(nid, unanchored=("4821",), ratio=0.0, conf=0.7):
        return {"id": nid, "quote_type": "hard_data", "confidence": conf, "hard_tokens": 1, "anchored": 0,
                "ratio": ratio, "quote_len": 1, "body_to_quote_ratio": 1.0, "unanchored": list(unanchored)}

    ids = lambda results: [w["id"] for w in mod._worst_n(results, 10)]  # noqa: E731
    assert ids([res("c"), res("a"), res("b")]) == ["a", "b", "c"]
    assert ids([res("b"), res("c"), res("a")]) == ["a", "b", "c"]
    # the id does NOT outrank the earlier parts: a larger id with the higher confidence still comes first
    assert ids([res("a", conf=0.1), res("z", conf=0.9)]) == ["z", "a"]
    # ... nor a lower ratio, nor more unanchored tokens
    assert ids([res("a", ratio=0.5), res("z", ratio=0.0)]) == ["z", "a"]
    assert ids([res("a"), res("z", unanchored=("1", "2"))]) == ["z", "a"]
    # None confidence sorts as 0.0, None ratio as 1.0: the tiebreak still applies inside those ties
    assert ids([res("m", conf=None), res("k", conf=None)]) == ["k", "m"]


def test_s1c_ties_at_each_position_come_out_ascending_when_the_store_holds_them_out_of_order(
        home, monkeypatch, capsys):
    """Three positions: inside a page, across a page boundary, at the end -- inserted descending."""
    rows, groups = build_rows(3 * P + 3, P)
    populate(home, rows)
    mod = load_tool(home, monkeypatch)
    rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json",
                            "--top", "1000000")
    assert rc == 0, err
    ids = [w["id"] for w in json.loads(out)["worst"]]
    for g, idxs in groups.items():
        assert len(idxs) == 3, g
        want = sorted(oid(i) for i in idxs)
        pos = ids.index(want[0])
        assert ids[pos:pos + 3] == want, (g, ids[pos:pos + 3])


@requires_linux_owner
def test_s1e_a_top_cut_through_a_tie_group_keeps_the_lowest_ids_flag_off_and_on(home, monkeypatch, capsys):
    """Six rows tie exactly on (unanchored count, ratio, confidence), inserted out of id order, plus one row that
    sorts before them. --top 3 cuts THROUGH the tie group: the last two slots must go to the lowest ids."""
    first = _row("ob_z", claim="reached 4821 and 9137")      # two unanchored tokens: sorts before the tie group
    tied = [_row(f"ob_{c}") for c in "cafbed"]                # inserted c, a, f, b, e, d
    populate(home, [*tied, first])
    db = str(home / "knowledge.db")
    argv = ("--db", db, "--format", "json", "--top", "3")
    mod = load_tool(home, monkeypatch)
    set_flag(home, False)
    off = run_main(mod, capsys, *argv)
    set_flag(home, True)
    with real_owner(home):
        on = run_main(mod, capsys, *argv)
    assert off[0] == 0 and on[0] == 0, (off, on)
    assert [w["id"] for w in json.loads(off[1])["worst"]] == ["ob_z", "ob_a", "ob_b"]
    assert (mask(off[1]), off[2]) == (mask(on[1]), on[2])


# ---- B1 / C1 -------------------------------------------------------------------------------------------------

class CountingReader:
    """Wraps the connection the tool reads on and counts the rows of every reply."""

    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def execute(self, sql, params=()):
        rows = self._inner.execute(sql, params).fetchall()
        self._log.append(len(rows))

        class _R:
            def fetchall(_self):
                return rows
        return _R()

    def close(self):
        self._inner.close()


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("flag", [False, True], ids=["flag_off", "flag_on"])
def test_b1_c1_no_reply_over_a_page_the_right_number_of_replies_and_every_id_once(
        home, monkeypatch, capsys, n, flag):
    rows, _g = build_rows(n, P)
    populate(home, rows)
    set_flag(home, flag)
    mod = load_tool(home, monkeypatch)
    log = []
    real_open = mod.owner_read.open_reader
    monkeypatch.setattr(mod.owner_read, "open_reader",
                        lambda spec, db, use: CountingReader(real_open(spec, db, use), log))
    db = str(home / "knowledge.db")
    ctx = FakeOwner(home) if flag else contextlib.nullcontext()
    with ctx:
        rc, out, err = run_main(mod, capsys, "--db", db, "--format", "json", "--top", "1000000")
    assert rc == 0, err
    assert max(log) <= P, log                       # no bulk reply
    assert len(log) == n // P + 1, (n, log)         # ceil(n / P), plus one for an exact multiple (and 1 for n == 0)
    assert sum(log) == n
    payload = json.loads(out)
    ids = [w["id"] for w in payload["worst"]]
    assert payload["population"] == n and len(ids) == n and sorted(ids) == population_ids(rows)  # each id once


# ---- R1 ---------------------------------------------------------------------------------------------------------

def test_r1_the_flag_off_connection_is_read_only_and_the_store_bytes_do_not_change(home, monkeypatch, capsys):
    rows, _g = build_rows(10, P)
    populate(home, rows)
    db = home / "knowledge.db"
    before = db.read_bytes()
    mod = load_tool(home, monkeypatch)
    seen = []
    real_open = mod.owner_read.open_reader

    def probing(spec, db_path, use_owner):
        conn = real_open(spec, db_path, use_owner)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE nodes SET claim = 'changed'")
        seen.append(use_owner)
        return conn

    monkeypatch.setattr(mod.owner_read, "open_reader", probing)
    rc, out, err = run_main(mod, capsys, "--db", str(db), "--format", "json")
    assert rc == 0 and seen == [False], (rc, seen, err)
    assert db.read_bytes() == before
    conn = mod._open_population_reader(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM nodes")
    finally:
        conn.close()
    r = cli(home, "--format", "json")
    assert r.returncode == 0, r.stderr
    assert db.read_bytes() == before
    assert sorted(p.name for p in home.iterdir() if p.name.startswith("knowledge.db")) == ["knowledge.db"]


# ---- fake owners ---------------------------------------------------------------------------------------------------

DIE = object()


class ScriptOwner(FakeOwner):
    """Answers every population read from `script(call_index, args)`: a list is an ok answer, a dict is the
    answer, DIE closes the connection with no reply, None falls through to the faithful stand-in."""

    def __init__(self, home, script):
        super().__init__(home)
        self.script = script
        self.pop_calls = 0

    def _scripted(self, request):
        params = request.get("params") or {}
        if request.get("method") == "read" and params.get("sql") in (SQL_ALL, SQL_BY_TYPE):
            idx = self.pop_calls
            self.pop_calls += 1
            return self.script(idx, list(params.get("args") or []))
        return None

    def _handle(self, conn):
        try:
            conn.settimeout(2)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            request = json.loads(data.decode("utf-8").strip())
            self.requests.append(request)
            answer = self._scripted(request)
            if answer is DIE:
                return
            if answer is None:
                answer = self._answer(request)
            elif isinstance(answer, list):
                answer = {"status": "ok", "result": answer}
            conn.sendall((json.dumps(answer) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            conn.close()


def good_row(i):
    return [oid(i), "hard_data", 0.5, "claim", "quote", "interp", None, "2026-01-01T00:00:00"]


REFUSAL = {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"}


# ---- O1 ---------------------------------------------------------------------------------------------------------------

def test_o1_flag_on_owner_down_exits_3_and_never_connects_to_the_store(home, monkeypatch, capsys):
    rows, _g = build_rows(10, P)
    populate(home, rows)
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    connects = []
    real_connect = sqlite3.connect

    def spy(*a, **k):
        connects.append(a)
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert_typed_exit_3(rc, out, err, needle="unavailable")
    assert f"set engramd.{FLAG_KEY} to false" in err, err
    assert "no observations" in err, err           # the never_empty sentence
    assert connects == [], connects                  # zero direct connects, counted and not inferred
    r = cli(home, "--format", "json")                # and the CLI agrees: exit 3, empty stdout, one line
    assert r.returncode == 3 and r.stdout == "" and has_one_error_and_nothing_foreign(r.stderr), (r.returncode, r.stderr)


def test_o1b_flag_on_pings_the_owner_before_any_page_is_read(home, monkeypatch, capsys):
    rows, _g = build_rows(10, P)
    populate(home, rows)
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with FakeOwner(home) as owner, deadline():
        rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert rc == 0, err
    methods = [r.get("method") for r in owner.requests]
    assert methods[0] == "ping", methods                 # the ping is the FIRST thing the owner is asked
    assert "read" in methods[1:], methods                # and a page request comes after it


def test_o1b_a_failed_ping_is_the_runs_failure_and_no_page_is_read(home, monkeypatch, capsys):
    """The down-owner message ("unavailable") is the same whether the ping or the first page hits it, so the
    ping is isolated: the client's `read` raises for the ping only, with NO owner running. Without the ping the
    first page read gets the plain "unavailable" answer instead, and the line does not name a ping."""
    rows, _g = build_rows(10, P)
    populate(home, rows)
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    client = mod.owner_read.import_runtime("engramd_client")
    real_read = client.read
    asked = []

    def read(method, params, **kw):
        asked.append(method)
        if method == "ping":
            raise OSError("scripted ping failure")
        return real_read(method, params, **kw)

    monkeypatch.setattr(client, "read", read)
    rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert rc == 3 and out == "", (rc, out, err)
    assert len(stderr_lines(err)) == 1 and "owner ping failed" in err, err
    assert asked == ["ping"], asked                      # failed at the ping: no page was asked for


# ---- O2 ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("how", ["refuse", "die"])
@pytest.mark.parametrize("fail_at", ["first", "middle", "last"])
def test_o2_a_failure_on_any_page_exits_3_with_empty_stdout(home, monkeypatch, capsys, how, fail_at):
    n = 2 * P + 3                                 # 3 fetches: 0, 1, 2
    rows, _g = build_rows(n, P)
    populate(home, rows)
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    at = {"first": 0, "middle": 1, "last": n // P}[fail_at]
    bad = REFUSAL if how == "refuse" else DIE
    owner = ScriptOwner(home, lambda idx, args: bad if idx == at else None)
    with owner, deadline():
        rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert_typed_exit_3(rc, out, err)
    assert owner.pop_calls == at + 1, owner.pop_calls     # it stopped there: no retry, no later page


# ---- O3 ---------------------------------------------------------------------------------------------------------------

REFUSALS = [
    {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "unregistered_template", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "refused_at_load", "detail": "d", "remedy": "r"},
    {"status": "ok", "result": "not-a-list"},
    {"status": "ok", "result": {"rows": []}},
]


@pytest.mark.parametrize("answer", REFUSALS, ids=lambda a: a.get("reason") or str(type(a["result"]).__name__))
def test_o3_a_refusing_owner_raises_typed_and_exits_3(home, monkeypatch, capsys, answer):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with ScriptOwner(home, lambda idx, args: answer):
        with pytest.raises(mod.AnchoringReportOwnerReadError):
            conn = mod._open_population_reader(home / "knowledge.db")
            try:
                mod._fetch_population(conn, None)
            finally:
                conn.close()
        rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"))
    assert_typed_exit_3(rc, out, err)


# ---- O4 ---------------------------------------------------------------------------------------------------------------

MALFORMED = {
    # P + 1 rows, then (were it accepted) an honest empty page: ONLY the size check can refuse this one, the
    # repeated-page check cannot (the first draft repeated the oversize page and the size check could be deleted)
    "oversize_reply": lambda idx, args: [good_row(i) for i in range(1, P + 2)] if idx == 0 else [],
    "seven_cells": lambda idx, args: [good_row(1)[:7]],
    "nine_cells": lambda idx, args: [good_row(1) + ["extra"]],
    "ids_not_increasing": lambda idx, args: [good_row(2), good_row(1)],
    "ids_equal": lambda idx, args: [good_row(1), good_row(1)],
    "first_id_not_above_cursor": lambda idx, args: (
        [good_row(i) for i in range(1, P + 1)] if idx == 0 else [good_row(i) for i in range(P, 2 * P)]),
    "repeated_page": lambda idx, args: [good_row(i) for i in range(1, P + 1)],
    "non_str_id": lambda idx, args: [[7] + good_row(1)[1:]],
    "null_id": lambda idx, args: [[None] + good_row(1)[1:]],
    "int_quote_type": lambda idx, args: [good_row(1)[:1] + [5] + good_row(1)[2:]],
    "str_confidence": lambda idx, args: [good_row(1)[:2] + ["0.5"] + good_row(1)[3:]],
    "bool_confidence": lambda idx, args: [good_row(1)[:2] + [True] + good_row(1)[3:]],
    "list_cell": lambda idx, args: [good_row(1)[:3] + [[0, 1]] + good_row(1)[4:]],
    "int_claim": lambda idx, args: [good_row(1)[:3] + [5] + good_row(1)[4:]],
    "int_created_at": lambda idx, args: [good_row(1)[:7] + [5]],
    "row_is_a_string": lambda idx, args: ["not-a-row"],
    "row_is_a_dict": lambda idx, args: [dict(zip(COLUMNS, good_row(1)))],
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_o4_a_malformed_answer_raises_typed_and_terminates(home, monkeypatch, capsys, case):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with ScriptOwner(home, MALFORMED[case]) as owner, deadline():
        with pytest.raises(mod.AnchoringReportOwnerReadError) as exc:
            conn = mod._open_population_reader(home / "knowledge.db")
            try:
                mod._fetch_population(conn, None)
            finally:
                conn.close()
        assert "engramd." + FLAG_KEY in str(exc.value)
        assert "no observations" in str(exc.value)     # the never_empty sentence
        owner.pop_calls = 0   # the scripts are keyed on the call index: the CLI run starts from page 1 again
        n_after_first = 0
        rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert_typed_exit_3(rc, out, err)
    assert owner.pop_calls - n_after_first <= 2, owner.pop_calls   # a repeated page stopped at the second fetch


def test_o4b_a_wellformed_answer_with_null_cells_is_accepted(home, monkeypatch):
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    row = [oid(1), None, None, None, None, None, None, None]
    with ScriptOwner(home, lambda idx, args: [row]):
        conn = mod._open_population_reader(home / "knowledge.db")
        try:
            got = mod._fetch_population(conn, None)
        finally:
            conn.close()
    assert [dict(r) for r in got] == [dict(zip(COLUMNS, row))]


@requires_linux_owner
def test_o4c_a_blob_cell_through_the_real_owner_is_a_typed_refusal(home, monkeypatch, capsys):
    rows, _g = build_rows(10, P)
    populate(home, rows)
    conn = sqlite3.connect(str(home / "knowledge.db"))
    conn.execute("UPDATE nodes SET interpretation = ? WHERE id = ?", (b"\x00\x01\xff", oid(4)))
    conn.commit()
    conn.close()
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with real_owner(home):
        rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert_typed_exit_3(rc, out, err, needle="unsupported_value_type")


# ---- O5 ---------------------------------------------------------------------------------------------------------------

@requires_linux_owner
def test_o5_a_real_owner_answering_zero_rows_is_a_population_of_zero_exit_0(home, monkeypatch, capsys):
    # only the noise rows: no current observation at all
    populate(home, [r for r in build_rows(3, P)[0] if r["id"].startswith(("aa_", "zz_"))])
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    rec = Recorder(monkeypatch)
    with real_owner(home):
        rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["population"] == 0 and payload["worst"] == []
    assert rec.rows == [0]


# ---- X1: the third tool is untouched, flag ON ----------------------------------------------------------------------------

@requires_linux_owner
def test_x1_open_readonly_and_s2_clause_sheet_are_untouched_with_the_flag_on(home, monkeypatch):
    rows, _g = build_rows(12, P)
    populate(home, rows)
    db = home / "knowledge.db"
    mod = load_tool(home, monkeypatch)
    s2 = load_tool(home, monkeypatch, S2_PATH, "s2_clause_sheet_3881")

    def read_all(tool):
        conn = tool._open_readonly(db)
        try:
            by_name = [r["claim"] for r in conn.execute("SELECT claim FROM nodes ORDER BY id LIMIT 3")]
            assert isinstance(conn.execute("SELECT id FROM nodes LIMIT 1").fetchone(), sqlite3.Row)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO nodes (id, type, created_at) VALUES ('x_0', 'observation_factual', 'n')")
                conn.commit()
        finally:
            conn.close()
        return by_name

    set_flag(home, False)
    off = (read_all(mod), read_all(s2))
    off_pop = s2.read_population(db, "00")
    set_flag(home, True)
    seen = []
    real_handle = eo.EngramdOwner._handle_connection
    monkeypatch.setattr(eo.EngramdOwner, "_handle_connection",
                        lambda self_, conn: (seen.append(1), real_handle(self_, conn))[1])
    with real_owner(home):
        on = (read_all(mod), read_all(s2))
        on_pop = s2.read_population(db, "00")
    assert on == off and on_pop == off_pop
    assert seen == [], f"the owner received {len(seen)} connection(s)"


# ---- F1 ---------------------------------------------------------------------------------------------------------------

def test_f1_db_naming_another_file_stays_direct_and_never_contacts_the_owner(home, monkeypatch, capsys, tmp_path):
    rows, _g = build_rows(9, P)
    populate(home, rows)
    other = tmp_path / "other.db"
    shutil.copyfile(_template_db(), other)
    conn = sqlite3.connect(str(other))
    conn.execute("INSERT INTO nodes (id, type, claim, created_at, is_current, quote_type) "
                 "VALUES ('ot_1', 'observation_factual', 'c 77', '2026-01-01T00:00:00', 1, 'hard_data')")
    conn.commit()
    conn.close()
    set_flag(home, True)
    mod = load_tool(home, monkeypatch)
    with FakeOwner(home) as owner:
        rc, out, err = run_main(mod, capsys, "--db", str(other), "--format", "json")
    assert rc == 0, err
    assert json.loads(out)["population"] == 1
    assert owner.requests == []


# ---- H1 / flag OFF store classes ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [False, True], ids=["flag_off", "flag_on"])
def test_h1_a_relative_engram_home_is_a_clean_refusal(home, flag):
    set_flag(home, flag)
    r = cli(home, "--format", "json", cwd=str(home.parent))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert r.stdout == ""
    assert "Traceback" not in r.stderr, r.stderr
    lines = stderr_lines(r.stderr)
    assert len(lines) == 1, r.stderr
    assert "ENGRAM_HOME must be an absolute path" in lines[0] and home.name in lines[0], lines[0]


def test_h1b_an_absolute_home_is_unaffected(home):
    r = cli(home, "--format", "json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["population"] == 0


def test_h2_flag_off_a_missing_store_is_exit_1_and_an_unusable_one_is_exit_3(home, tmp_path):
    r = cli(home, "--db", str(tmp_path / "absent.db"))
    assert (r.returncode, r.stdout) == (1, "") and has_one_error_and_nothing_foreign(r.stderr), r.stderr
    assert "not found" in r.stderr
    r = cli(home, "--db", str(tmp_path))                  # a directory
    assert (r.returncode, r.stdout) == (3, "") and has_one_error_and_nothing_foreign(r.stderr), (r.returncode, r.stderr)
    assert "AnchoringReportOwnerReadError" in r.stderr
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 100)
    r = cli(home, "--db", str(junk))
    assert (r.returncode, r.stdout) == (3, "") and has_one_error_and_nothing_foreign(r.stderr), (r.returncode, r.stderr)
    assert "no observations" in r.stderr


def test_h3_the_survey_and_help_name_the_flag(home):
    assert FLAG_KEY in SURVEY_PATH.read_text(encoding="utf-8")
    r = cli(home, "--help", path=SURVEY_PATH)
    assert r.returncode == 0 and FLAG_KEY in r.stdout, r.stdout[-600:]


# ---- O4d: a boolean confidence, past the owner client's own check ------------------------------------------------------

class StubConn:
    """A population connection that never touches an owner or a store: `execute(...).fetchall()` answers from
    `pages(call_index, args)`; `close()` is counted. It hands the tool rows the owner client would refuse first."""

    def __init__(self, pages):
        self.pages, self.calls, self.closes = pages, 0, 0

    def execute(self, sql, params=()):
        idx, self.calls = self.calls, self.calls + 1
        rows = self.pages(idx, list(params))
        return type("Cur", (), {"fetchall": lambda _self: rows, "fetchone": lambda _self: None})()

    def close(self):
        self.closes += 1


@pytest.mark.parametrize("value", [True, False])
def test_o4d_a_boolean_confidence_is_a_typed_error_exit_3(home, monkeypatch, capsys, value):
    mod = load_tool(home, monkeypatch)
    bad = StubConn(lambda idx, args: [good_row(1)[:2] + [value] + good_row(1)[3:]])
    monkeypatch.setattr(mod, "_open_population_reader", lambda _db: bad)
    rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert_typed_exit_3(rc, out, err, needle="confidence")
    assert bad.calls == 1
    # non-vacuous: the same stub with a number (0, an int) in that cell is a normal report
    ok = StubConn(lambda idx, args: [good_row(1)[:2] + [0] + good_row(1)[3:]] if idx == 0 else [])
    monkeypatch.setattr(mod, "_open_population_reader", lambda _db: ok)
    rc, out, err = run_main(mod, capsys, "--db", str(home / "knowledge.db"), "--format", "json")
    assert rc == 0 and json.loads(out)["population"] == 1, (rc, err)


# ---- Q1: an empty --quote-type is no filter -----------------------------------------------------------------------------

@requires_linux_owner
def test_q1_an_empty_quote_type_is_no_filter_flag_off_and_flag_on(home, monkeypatch, capsys):
    rows, _g = build_rows(60, P)
    populate(home, rows)
    db = str(home / "knowledge.db")
    assert len({r["quote_type"] for r in rows if r["is_current"] == 1}) >= 3     # filtering by '' would empty it
    mod = load_tool(home, monkeypatch)

    def both():
        a = run_main(mod, capsys, "--db", db, "--format", "json", "--top", "1000000")
        b = run_main(mod, capsys, "--db", db, "--format", "json", "--top", "1000000", "--quote-type", "")
        assert a[0] == 0 and b[0] == 0, (a, b)
        assert (mask(a[1]), a[2]) == (mask(b[1]), b[2])
        assert json.loads(b[1])["population"] == 60

    set_flag(home, False)
    both()
    set_flag(home, True)
    rec = Recorder(monkeypatch)
    with real_owner(home):
        both()
    assert rec.sql and set(rec.sql) == {SQL_ALL}, set(rec.sql)     # never the by-type statement


# ---- H2s: the survey's store-class exit codes, flag OFF -----------------------------------------------------------------

def test_h2s_the_survey_missing_store_is_exit_1_and_an_unusable_one_is_exit_3(home, tmp_path):
    sheet, key = tmp_path / "sheet.md", tmp_path / "key.json"

    def run(db):
        r = cli(home, "--db", str(db), "--judging-sheet", str(sheet), "--judging-key", str(key), path=SURVEY_PATH)
        assert r.stdout == "" and has_one_error_and_nothing_foreign(r.stderr), (r.returncode, r.stdout, r.stderr)
        assert not sheet.exists() and not key.exists()
        return r

    r = run(tmp_path / "absent.db")
    assert r.returncode == 1 and "DB not found" in r.stderr, (r.returncode, r.stderr)
    r = run(tmp_path)                                        # a directory
    assert r.returncode == 3 and "AnchoringReportOwnerReadError" in r.stderr, (r.returncode, r.stderr)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database" * 100)
    r = run(junk)
    assert r.returncode == 3 and "AnchoringReportOwnerReadError" in r.stderr, (r.returncode, r.stderr)


# ---- K1: the reader's connection is closed exactly once ------------------------------------------------------------------

class CountingConn:
    """Delegates `execute` to the real reader and counts `close()` (which it also delegates)."""

    def __init__(self, real):
        self.real, self.closes = real, 0

    def execute(self, sql, params=()):
        return self.real.execute(sql, params)

    def close(self):
        self.closes += 1
        self.real.close()


@pytest.mark.parametrize("which", ["anchoring_report", "survey"])
@pytest.mark.parametrize("case", ["ok_flag_off", "failed_read_flag_on"])
def test_k1_the_population_connection_is_closed_exactly_once(home, monkeypatch, capsys, which, case):
    rows, _g = build_rows(12, P)
    populate(home, rows)
    if which == "survey":
        mod = load_tool(home, monkeypatch, SURVEY_PATH, "qqs_3881_k1")
    else:
        mod = load_tool(home, monkeypatch)
    argv = ["--db", str(home / "knowledge.db"), "--format", "json"]
    made = []
    real_open = mod._open_population_reader

    def counting_open(db_path):
        c = CountingConn(real_open(db_path))
        made.append(c)
        return c

    monkeypatch.setattr(mod, "_open_population_reader", counting_open)
    if case == "ok_flag_off":
        set_flag(home, False)
        rc, out, err = run_main(mod, capsys, *argv)
        assert rc == 0, err
    else:
        set_flag(home, True)
        with ScriptOwner(home, MALFORMED["ids_equal"]), deadline():
            rc, out, err = run_main(mod, capsys, *argv)
        assert rc == 3 and out == "", (rc, out, err)
    assert len(made) == 1 and made[0].closes == 1, [c.closes for c in made]
