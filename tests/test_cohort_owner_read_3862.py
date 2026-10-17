"""engram-alpha#3862 (U14 of #2652) -- tools/cohort_dispatch.py reads through the owner.

The second `tools/` unit. Behind `engramd.cohort_dispatch_reads_via_owner` (default OFF) the tool's
ONE read -- `_fetch_nodes`, called by `prepare` and `verify-in` -- goes to the engramd owner instead of
opening knowledge.db. Two root-cause fixes ride along: the owner-read helper is ONE shared tools module
(U12 moved onto it), and the store path has ONE ENGRAM_HOME resolution.

Contract (issue #3862, each cell red before the implementation):

  C1  differential at the read: owner and direct give the same rows in the same order for one id, many
      ids (> 999: the variable ceiling is gone), duplicate ids, missing ids and an empty list. The
      harness is shown to FAIL by mutating the NEW side: an owner whose columns are shifted by one.
  C2  owner down with the flag ON: `_fetch_nodes` RAISES, nothing is opened directly, `prepare` and
      `verify-in` exit non-zero naming the reason. Never `[]`, never partial, never a direct fallback
      (the #3045 silent-drop class: a short answer reads as "these nodes do not exist").
  C3  store selection: an explicit --db naming another file stays direct (mode=ro) and never contacts
      the owner; a flag that cannot be read is OFF.
  C4  ENGRAM_HOME: with a temp home and no --db the tool reads THAT home's store. The skill no longer
      hard-codes the path. The statement is the registry's one admitted json_each form, registered
      under this tool's site, byte-exact.
  C5  U12 unchanged: reconcile_lesson_demotions' own test files pass with no edits (run alongside).
  C6  the direct path is read-only: a write on the connection fails.
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

TOOL_PATH = REPO_ROOT / "tools" / "cohort_dispatch.py"
FLAG_KEY = "cohort_dispatch_reads_via_owner"
SITE = "tools/cohort_dispatch.py:_fetch_nodes"
FIELDS = ("id", "type", "claim", "quoted_text", "interpretation", "recall_summary",
          "predicted_event", "resolution_timeframe")
SQL = (
    "SELECT id, type, claim, quoted_text, interpretation, recall_summary, predicted_event, "
    "resolution_timeframe FROM nodes WHERE id IN (SELECT value FROM json_each(?))"
)
SQL_SHA256 = hashlib.sha256(SQL.encode("utf-8")).hexdigest()
N_BULK = 1200  # more than SQLITE_MAX_VARIABLE_NUMBER's old 999


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
    d = Path(tempfile.mkdtemp(prefix="cd3862-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_store(home: Path) -> Path:
    old = os.environ.get("ENGRAM_HOME")
    os.environ["ENGRAM_HOME"] = str(home)
    try:
        db = home / "knowledge.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        engram_core._run_db_one_time_setup(conn)
        conn.row_factory = None
        for i in range(N_BULK):
            conn.execute(
                "INSERT INTO nodes (id, type, claim, quoted_text, interpretation, recall_summary, created_at) "
                "VALUES (?, 'observation', ?, ?, ?, ?, '2026-01-01T00:00:00')",
                (f"ob_{i:04d}", f"claim {i}", f"quote {i}" if i % 3 == 0 else None,
                 f"interp {i}" if i % 5 == 0 else None, f"summary {i}" if i % 2 == 0 else None),
            )
        # a prediction stores its content elsewhere (#1966): NULL claim, the other columns carry it
        conn.execute(
            "INSERT INTO nodes (id, type, claim, predicted_event, resolution_timeframe, created_at) "
            "VALUES ('pr_0001', 'prediction', NULL, 'it rains', 'by friday', '2026-01-01T00:00:00')")
        conn.commit()
        conn.close()
    finally:
        if old is None:
            os.environ.pop("ENGRAM_HOME", None)
        else:
            os.environ["ENGRAM_HOME"] = old
    return db


def set_flag(home: Path, value=True) -> None:
    (home / "config.json").write_text(json.dumps({"engramd": {FLAG_KEY: value}}), encoding="utf-8")


def load_tool(home: Path, monkeypatch, path: Path = TOOL_PATH):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location("cohort_dispatch_3862", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seat(home):
    make_store(home)
    return home


class Opens:
    """Raise-and-record every way THIS thread could open a sqlite store (the in-process fake owner
    serves from other threads and keeps its own connections)."""

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


def _ids(*idx):
    return [f"ob_{i:04d}" for i in idx]


CASES = {
    "one": _ids(7),
    "many_over_999": [f"ob_{i:04d}" for i in range(N_BULK)],
    "duplicates": _ids(3, 4, 3, 3),
    "missing_skipped": _ids(1) + ["ob_nope", "pr_0001", "ob_also_nope"] + _ids(2),
    "prediction": ["pr_0001"],
    "empty": [],
    "reversed_input_order": list(reversed(_ids(0, 1, 2, 3, 4, 5))),
}


# ---- C1: differential at the read -----------------------------------------------------------

def _both(seat, monkeypatch, owner_factory, ids):
    set_flag(seat, False)  # one config file, the flag value is the only difference between the legs
    direct = load_tool(seat, monkeypatch)._fetch_nodes(seat / "knowledge.db", ids)
    set_flag(seat)
    mod_on = load_tool(seat, monkeypatch)
    with owner_factory(seat) as owner:
        via_owner = mod_on._fetch_nodes(seat / "knowledge.db", ids)
    return direct, via_owner, owner


@pytest.mark.parametrize("case", sorted(CASES))
def test_c1_both_paths_return_the_same_rows_in_the_same_order(seat, monkeypatch, case):
    ids = CASES[case]
    direct, via_owner, owner = _both(seat, monkeypatch, FakeOwner, ids)
    assert via_owner == direct
    known = [i for i in ids if i.startswith(("ob_0", "ob_1", "pr_")) and i not in ("ob_nope", "ob_also_nope")]
    assert [n["id"] for n in direct] == known  # input order, missing skipped, duplicates kept
    assert all(list(n) == list(FIELDS) for n in direct)  # dicts keyed by the one column tuple
    assert any(r["method"] == "read" for r in owner.requests), "the flag-ON leg never reached the owner"


def test_c1_a_prediction_row_keeps_its_null_claim_and_its_event(seat, monkeypatch):
    direct, via_owner, _ = _both(seat, monkeypatch, FakeOwner, ["pr_0001"])
    assert direct == via_owner == [{
        "id": "pr_0001", "type": "prediction", "claim": None, "quoted_text": None, "interpretation": None,
        "recall_summary": None, "predicted_event": "it rains", "resolution_timeframe": "by friday"}]


class ShiftedOwner(FakeOwner):
    """A faulty NEW side: every row's columns shifted by one."""

    def _answer(self, request):
        ans = super()._answer(request)
        if request.get("method") == "read" and ans.get("status") == "ok":
            ans = {"status": "ok", "result": [r[1:] + r[:1] for r in ans["result"]]}
        return ans


def test_c1_control_an_owner_with_shifted_columns_is_refused_by_the_row_check(seat, monkeypatch):
    """A one-column shift moves the id out of cell 0, so the owner-row check refuses it before any mapping."""
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    with ShiftedOwner(seat):
        with pytest.raises(mod.CohortOwnerReadError):
            mod._fetch_nodes(seat / "knowledge.db", _ids(1, 2, 3))


class SwappedContentOwner(FakeOwner):
    """A faulty NEW side the row check cannot see: claim and quoted_text swapped (both are text-or-null)."""

    def _answer(self, request):
        ans = super()._answer(request)
        if request.get("method") == "read" and ans.get("status") == "ok":
            ans = {"status": "ok", "result": [[r[0], r[1], r[3], r[2], *r[4:]] for r in ans["result"]]}
        return ans


def test_c1_control_an_owner_with_swapped_content_columns_reddens_the_harness(seat, monkeypatch):
    direct, via_owner, _ = _both(seat, monkeypatch, SwappedContentOwner, _ids(0, 3, 6))  # these carry a quote
    assert via_owner != direct


# ---- C2: owner down is an error -------------------------------------------------------------

def test_c2_flag_on_owner_down_raises_never_an_empty_list_and_never_opens_the_store(seat, monkeypatch):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with pytest.raises(mod.CohortOwnerReadError) as exc:
        mod._fetch_nodes(seat / "knowledge.db", _ids(1, 2))
    assert "owner" in str(exc.value).lower()
    assert opens.record == [], opens.record


def _failing_owner(seat, answer):
    owner = FakeOwner(seat)
    owner._answer = lambda request, a=answer: (
        {"status": "ok", "result": "pong"} if request.get("method") == "ping" else a)
    return owner


@pytest.mark.parametrize("answer", [
    {"status": "rejected", "reason": "deadline_exceeded", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "unregistered_template", "detail": "d", "remedy": "r"},
    {"status": "rejected", "reason": "refused_at_load", "detail": "d", "remedy": "r"},
    {"status": "ok", "result": "not-a-list"},
])
def test_c2_every_other_owner_failure_raises_too(seat, monkeypatch, answer):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    with _failing_owner(seat, answer):
        with pytest.raises(mod.CohortOwnerReadError) as exc:
            mod._fetch_nodes(seat / "knowledge.db", _ids(1))
    assert "empty list" in str(exc.value), "the refusal says why it did not answer []"


def _rows_owner(seat, rows):
    owner = FakeOwner(seat)
    owner._answer = lambda request, r=rows: (
        {"status": "ok", "result": "pong"} if request.get("method") == "ping" else {"status": "ok", "result": r})
    return owner


GOOD = ["ob_0001", "observation", "c", None, None, None, None, None]


@pytest.mark.parametrize("rows", [
    [[None] * 8],                      # a row of nulls: no id (Mira's case: used to read as 'no such node')
    [["ob_0001"]],                     # too short (used to come back as {'id': ...})
    [[]],                              # empty row (used to be a raw IndexError)
    [GOOD + ["extra"]],                # too wide
    [["ob_9999"] + GOOD[1:]],          # an id that was never asked for
    [[7] + GOOD[1:]],                  # id is not a string
    [["ob_0001", None] + GOOD[2:]],    # no type
    [["ob_0001", "observation", 5] + GOOD[3:]],   # a non-text content cell
    ["ob_0001"],                       # a row that is not a list at all
])
def test_c2_malformed_owner_rows_raise_never_a_missing_node_result(seat, monkeypatch, rows):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    with _rows_owner(seat, rows):
        with pytest.raises(mod.CohortOwnerReadError) as exc:
            mod._fetch_nodes(seat / "knowledge.db", ["ob_0001"])
    assert "empty list" in str(exc.value)


def test_c2_well_formed_owner_rows_still_pass_the_row_check(seat, monkeypatch):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    with _rows_owner(seat, [GOOD]):
        assert mod._fetch_nodes(seat / "knowledge.db", ["ob_0001"]) == [dict(zip(FIELDS, GOOD))]


def test_c2_prepare_with_malformed_owner_rows_exits_nonzero_and_writes_no_chunk(seat):
    set_flag(seat)
    out = seat / "cohort"
    with _rows_owner(seat, [[None] * 8]):
        r = _cli(seat, ["prepare", "--ids", "ob_0001", "--out", str(out)])
    assert r.returncode != 0 and "owner" in r.stderr.lower(), (r.stdout, r.stderr)
    assert not list(out.glob("chunk-*")) if out.exists() else True


def test_c2_prepare_with_one_valid_row_and_one_null_row_does_not_ship_a_one_node_cohort(seat):
    """Mira's end-to-end case: two ids asked, the owner returns one good row and one all-null row. Used to exit 0
    with a one-node cohort and a false 'not found' warning."""
    set_flag(seat)
    out = seat / "cohort"
    with _rows_owner(seat, [GOOD, [None] * 8]):
        r = _cli(seat, ["prepare", "--ids", "ob_0001,ob_0002", "--out", str(out)])
    assert r.returncode != 0 and "owner" in r.stderr.lower(), (r.returncode, r.stdout, r.stderr)
    assert not list(out.glob("chunk-*")) if out.exists() else True


def test_c2_verify_in_with_malformed_owner_rows_names_one_db_access_failure(seat):
    set_flag(seat, False)
    out = seat / "cohort"
    assert _cli(seat, ["prepare", "--ids", "ob_0001,ob_0002", "--out", str(out)]).returncode == 0
    set_flag(seat)
    with _rows_owner(seat, [[None] * 8]):
        r = _cli(seat, ["verify-in", "--out", str(out)])
    assert r.returncode != 0
    assert [e["check"] for e in json.loads(r.stdout)["errors"]] == ["db_access"]


def test_c2_flag_on_without_the_client_module_raises_never_reads_directly(seat, monkeypatch):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    monkeypatch.setitem(sys.modules, "engramd_client", None)
    opens = Opens().install(monkeypatch)
    with FakeOwner(seat):
        with pytest.raises(mod.CohortOwnerReadError):
            mod._fetch_nodes(seat / "knowledge.db", _ids(1))
    assert opens.record == []


def _cli(seat, args, tool=TOOL_PATH, **extra_env):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(seat)
    env.update(extra_env)
    return subprocess.run([sys.executable, str(tool), *args], capture_output=True, text=True, env=env, timeout=120)


def test_c2_prepare_with_the_owner_down_exits_nonzero_naming_the_reason_and_writes_no_chunk(seat):
    set_flag(seat)
    out = seat / "cohort"
    r = _cli(seat, ["prepare", "--ids", ",".join(_ids(1, 2, 3)), "--out", str(out)])
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert "owner" in r.stderr.lower() and "unavailable" in r.stderr.lower(), r.stderr
    assert not list(out.glob("chunk-*")) if out.exists() else True


def test_c2_verify_in_with_the_owner_down_fails_naming_the_reason_not_node_by_node(seat):
    set_flag(seat, False)
    out = seat / "cohort"
    ok = _cli(seat, ["prepare", "--ids", ",".join(_ids(1, 2, 3)), "--out", str(out)])
    assert ok.returncode == 0, (ok.stdout, ok.stderr)
    set_flag(seat)
    r = _cli(seat, ["verify-in", "--out", str(out)])
    assert r.returncode != 0, (r.stdout, r.stderr)
    result = json.loads(r.stdout)
    checks = [e["check"] for e in result["errors"]]
    assert checks == ["db_access"], checks  # one named failure, NOT one "not found in DB" per node
    assert "owner" in result["errors"][0]["error"].lower()


def test_c2_prepare_and_verify_in_with_the_owner_up_succeed_via_the_owner(seat):
    set_flag(seat)
    out = seat / "cohort"
    with FakeOwner(seat) as owner:
        p = _cli(seat, ["prepare", "--ids", ",".join(_ids(1, 2, 3)), "--out", str(out)])
        assert p.returncode == 0, (p.stdout, p.stderr)
        v = _cli(seat, ["verify-in", "--out", str(out)])
        assert v.returncode == 0, (v.stdout, v.stderr)
    assert json.loads(v.stdout).get("passed") is True
    assert len([r for r in owner.requests if r["method"] == "read"]) == 2  # one per command


# ---- C3: store selection --------------------------------------------------------------------

def test_c3_an_explicit_db_keeps_the_direct_path_and_never_contacts_the_owner(seat, monkeypatch):
    set_flag(seat)
    other = seat / "other"
    other.mkdir()
    shutil.copy(seat / "knowledge.db", other / "k.db")
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch, raising=False)
    got = mod._fetch_nodes(other / "k.db", _ids(1, 2))  # no owner running: must not need one
    assert [n["id"] for n in got] == _ids(1, 2)
    assert len(opens.record) == 1 and "mode=ro" in opens.record[0][1], opens.record


@pytest.mark.parametrize("config", ["{not json", '{"engramd": [1]}', '"a string"', ""])
def test_c3_a_flag_that_cannot_be_read_is_off(seat, monkeypatch, config):
    (seat / "config.json").write_text(config, encoding="utf-8")
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch, raising=False)
    got = mod._fetch_nodes(seat / "knowledge.db", _ids(1))  # no owner running
    assert [n["id"] for n in got] == _ids(1) and len(opens.record) == 1


@pytest.mark.parametrize("value", [False, "true", 1, None, "yes"])
def test_c3_only_the_json_boolean_true_turns_the_flag_on(seat, monkeypatch, value):
    set_flag(seat, value)
    mod = load_tool(seat, monkeypatch)
    assert [n["id"] for n in mod._fetch_nodes(seat / "knowledge.db", _ids(1))] == _ids(1)


def test_c3_a_symlink_to_this_homes_store_still_uses_the_owner(seat, monkeypatch):
    set_flag(seat)
    link = seat / "store-link.db"
    link.symlink_to(seat / "knowledge.db")
    mod = load_tool(seat, monkeypatch)
    opens = Opens().install(monkeypatch)
    with FakeOwner(seat) as owner:
        got = mod._fetch_nodes(link, _ids(1))
    assert [n["id"] for n in got] == _ids(1)
    assert opens.record == [] and any(r["method"] == "read" for r in owner.requests)


def test_c3_flag_on_never_stats_the_local_store_file(seat, monkeypatch):
    """The owner's answer replaces the `db_path.exists()` precondition under the flag."""
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
        assert [n["id"] for n in mod._fetch_nodes(seat / "knowledge.db", _ids(1))] == _ids(1)


def test_c3_flag_off_a_missing_store_is_still_file_not_found(seat, monkeypatch):
    set_flag(seat, False)
    mod = load_tool(seat, monkeypatch)
    with pytest.raises(FileNotFoundError):
        mod._fetch_nodes(seat / "absent.db", _ids(1))


# ---- C3b: a flag whose state is UNKNOWN is OFF and visible (Borges's ruling on #3869, the U4d precedent) ----

def _flag_line(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if "engramd_owner_flags failed" in ln]


def test_c3b_a_failed_flags_import_reads_off_and_prints_one_class_only_line(seat, monkeypatch, capsys):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    monkeypatch.setitem(sys.modules, "engramd_owner_flags", None)  # `import` -> ImportError
    opens = Opens().install(monkeypatch, raising=False)
    assert [n["id"] for n in mod._fetch_nodes(seat / "knowledge.db", _ids(1))] == _ids(1)  # the direct path
    assert len(opens.record) == 1
    lines = _flag_line(capsys)
    assert lines == [f"engramd.{FLAG_KEY}: engramd_owner_flags failed: ModuleNotFoundError"], lines


@pytest.mark.parametrize("exc", [RuntimeError("secret detail"), SystemExit(3)])
def test_c3b_a_flag_on_that_raises_is_off_and_visible_and_never_prints_the_message(seat, monkeypatch, capsys, exc):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    flags = mod.owner_read.import_runtime("engramd_owner_flags")
    monkeypatch.setattr(flags, "flag_on", lambda *a, **k: (_ for _ in ()).throw(exc))
    assert [n["id"] for n in mod._fetch_nodes(seat / "knowledge.db", _ids(1))] == _ids(1)
    err = capsys.readouterr().err
    assert f"engramd_owner_flags failed: {type(exc).__name__}" in err and "secret detail" not in err
    assert len([ln for ln in err.splitlines() if "engramd_owner_flags failed" in ln]) == 1


@pytest.mark.parametrize("config", [None, "{not json", '{"engramd": {}}', json.dumps({"engramd": {FLAG_KEY: False}})])
def test_c3b_a_healthy_import_with_an_absent_or_malformed_or_off_config_is_silent(seat, monkeypatch, capsys, config):
    if config is not None:
        (seat / "config.json").write_text(config, encoding="utf-8")
    mod = load_tool(seat, monkeypatch)
    mod._fetch_nodes(seat / "knowledge.db", _ids(1))
    assert capsys.readouterr().err == ""


# ---- C4: ENGRAM_HOME, and the statement is the registered one -------------------------------

def test_c4_no_db_reads_the_homes_store_not_the_users_dot_engram(seat, monkeypatch, tmp_path):
    fake_user_home = tmp_path / "userhome"
    (fake_user_home / ".engram").mkdir(parents=True)  # a decoy: no store in it
    monkeypatch.setenv("HOME", str(fake_user_home))
    mod = load_tool(seat, monkeypatch)
    args = mod._build_parser().parse_args(["verify-in", "--out", str(seat)])
    assert Path(args.db) == seat / "knowledge.db"
    args = mod._build_parser().parse_args(["prepare", "--ids", "x", "--out", str(seat)])
    assert Path(args.db) == seat / "knowledge.db"


def test_c4_prepare_with_no_db_flag_reads_the_temp_home_end_to_end(seat, tmp_path):
    out = seat / "cohort"
    decoy = tmp_path / "userhome"  # the user's own ~/.engram must not be what answers
    (decoy / ".engram").mkdir(parents=True)
    r = _cli(seat, ["prepare", "--ids", ",".join(_ids(1, 2)), "--out", str(out)], HOME=str(decoy))
    assert r.returncode == 0, (r.stdout, r.stderr)
    payload = json.loads((out / "chunk-0" / "payload.json").read_text())
    assert [e["id"] for e in payload] == _ids(1, 2)


def test_c4_there_is_no_hard_coded_default_store_left():
    src = (REPO_ROOT / "src" / "engram" / "tools" / "cohort_dispatch.py").read_text()
    assert "DEFAULT_DB" not in src
    assert 'Path.home() / ".engram"' not in src


def test_c4_the_sleep_skill_does_not_hard_code_the_store_for_prepare_and_verify_in():
    skill = (REPO_ROOT / "src" / "engram" / "skills" / "claude" / "engram-sleep" / "SKILL.md").read_text()
    for cmd in ("prepare", "verify-in"):
        block = re.search(rf"tools\.cohort_dispatch {cmd}[^`]*?```", skill, re.DOTALL)
        assert block, f"{cmd} invocation not found"
        assert "--db" not in block.group(0), block.group(0)


def test_c4_the_statement_constant_is_pinned_and_registered_under_this_tools_site():
    doc = json.loads((REPO_ROOT / "src" / "engram" / "engramd_read_templates.json").read_text())
    hits = [t for t in doc["templates"] if t["sql"] == SQL]
    assert len(hits) == 1, "the statement must be registered exactly once"
    assert hits[0]["key"] == SQL_SHA256 and hits[0]["sites"] == [SITE]
    assert SQL in registered_sql()


def test_c4_the_tool_sends_exactly_the_registered_bytes_with_one_json_parameter(seat, monkeypatch):
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    ids = _ids(1, 2, 3)
    with FakeOwner(seat) as owner:
        mod._fetch_nodes(seat / "knowledge.db", ids)
    reads = [r for r in owner.requests if r["method"] == "read"]
    assert [r["params"]["sql"] for r in reads] == [SQL]
    assert reads[0]["params"]["args"] == [json.dumps(ids)]


def test_c4_the_helper_is_one_shared_module_both_tools_use():
    cd = (REPO_ROOT / "src" / "engram" / "tools" / "cohort_dispatch.py").read_text()
    rl = (REPO_ROOT / "src" / "engram" / "tools" / "reconcile_lesson_demotions.py").read_text()
    for src in (cd, rl):
        assert "_owner_read" in src
    for name in ("def _owner_ping", "def _OwnerReader", "class _OwnerReader", "def _owner_failure"):
        assert name not in cd
    helper = REPO_ROOT / "src" / "engram" / "tools" / "_owner_read.py"
    assert helper.exists() and (REPO_ROOT / "tools" / "_owner_read.py").is_symlink()
    tiers = json.loads((REPO_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text())
    entry = [m for m in tiers["mechanisms"] if m["path"] == "tools/_owner_read.py"]
    assert entry == [{"path": "tools/_owner_read.py", "tier": "essential"}]


def _flattened(tmp: Path) -> Path:
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
    tool = TOOL_PATH if layout == "source" else _flattened(tmp_path) / "tools" / "cohort_dispatch.py"
    with FakeOwner(seat) as owner:
        r = _cli(seat, ["prepare", "--ids", ",".join(_ids(1, 2)), "--out", str(seat / "cohort")], tool=tool)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert any(q["method"] == "read" for q in owner.requests), "the flag-ON run never reached the owner"


# ---- C6: the direct path is read-only -------------------------------------------------------

@pytest.mark.parametrize("flag", [False, True])
def test_c6_the_direct_connection_is_read_only_on_both_flag_values(seat, monkeypatch, flag):
    set_flag(seat, flag)
    mod = load_tool(seat, monkeypatch)
    seen = []
    real = sqlite3.connect

    def spy(*a, **k):
        seen.append((a, k))
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    other = seat / "other.db"  # an explicit other --db keeps the direct leg on BOTH flag values
    shutil.copy(seat / "knowledge.db", other)
    mod._fetch_nodes(other, _ids(1))
    assert len(seen) == 1
    (args, kwargs) = seen[0]
    conn = real(*args, **kwargs)  # the very connection the tool opened, reopened here (the tool closes its own)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            conn.execute("INSERT INTO nodes (id, type, claim, created_at) VALUES ('w', 'observation', 'x', 'y')")
    finally:
        conn.close()


# ---- live owner (Linux and supported Mac) ---------------------------------------------------

@requires_linux_owner
@pytest.mark.parametrize("case", ["one", "many_over_999", "duplicates", "missing_skipped", "empty"])
def test_live_real_owner_agrees_with_the_direct_path(seat, monkeypatch, case):
    ids = CASES[case]
    set_flag(seat, False)
    direct = load_tool(seat, monkeypatch)._fetch_nodes(seat / "knowledge.db", ids)
    set_flag(seat)
    mod = load_tool(seat, monkeypatch)
    owner = eo.EngramdOwner(seat, request_deadline=5.0)
    owner.start(install_signal_handlers=False)
    try:
        via_owner = mod._fetch_nodes(seat / "knowledge.db", ids)
    finally:
        owner.stop()
    assert via_owner == direct
