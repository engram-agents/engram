"""engram-alpha#4616 -- tools/model_switch_read.py reads the graph through the engramd owner path (the mentor
seat's ruling, 2026-11-11: "through the owner read path, not the allowlist"), never by a direct open of the
seat's own store.

Contract (each cell written before the implementation):

  T1  the page statement is registered byte for byte under the site tools/model_switch_read.py:fetch_nodes, the
      scan config names that root, and the production loader ADMITS it (a plain unit cell); T4 the page size is
      one number (PAGE_ROWS, the literal in the SQL, the registered JSON)
  G1  the tool holds no `sqlite3.connect(` of its own (the 2652 ledger's whole point)
  D1  SAME DRAW BOTH WAYS: for one graph and one seed, `cards.md` and `key.json` are byte-identical between
      (a) a direct read of a copy of the store (a --db that is not the seat's own store; no owner exists) and
      (b) the owner path (the default db of ENGRAM_HOME, through a fake owner AND through a REAL owner), over
      graphs of 0, 199, 200, 201 and 450 nodes (page boundaries)
  B1  the owner path pings first and then reads page by page: N // 200 + 1 read requests, none over 200 rows
  O1  OWNER NOT RUNNING: exit 1, empty stdout, ONE stderr line `model_switch_read: <reason>` that names no flag
      for a direct read, nothing written to --out, and ZERO sqlite3.connect calls (a spy), for the default db and
      for a --db that names the home's store by another path (a symlink)
  O2  an owner that refuses the read (unregistered_template) stops the tool the same way
  O3  a page that does not fit (repeated page, a bool, a short row, an over-long page) or a page that fails
      after page 1 raises a typed error / Refusal; the pages read so far are never drawn from
  L1  no own id and no own text on stdout or stderr on the owner path, on success
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
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

TOOL = Path(os.environ.get("MODEL_SWITCH_READ_TOOL") or REPO_ROOT / "src" / "engram" / "tools" / "model_switch_read.py")
SITE = "tools/model_switch_read.py:fetch_nodes"
SQL = ("SELECT id, type, claim, created_at, quoted_text, interpretation, is_current, metadata "
       "FROM nodes WHERE id > ? ORDER BY id LIMIT 200")
TEMPLATES_JSON = REPO_ROOT / "src" / "engram" / "engramd_read_templates.json"
SCAN_CONFIG = REPO_ROOT / "tools" / "engramd_read_scan.config.json"
P = 200

_TEMPLATE = {}


def _template_db() -> Path:
    if "path" not in _TEMPLATE:
        d = Path(tempfile.mkdtemp(prefix="msr-tpl-", dir="/tmp"))
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


def new_home(tag="msr") -> Path:
    """A SHORT home (the owner socket lives at <home>/run/engramd.sock; AF_UNIX paths are capped)."""
    d = Path(tempfile.mkdtemp(prefix=tag + "-", dir="/tmp"))
    shutil.copyfile(_template_db(), d / "knowledge.db")
    return d


@pytest.fixture
def home():
    d = new_home()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


TYPES = ["observation_factual", "lesson", "cornerstone", "feeling_report", "evidence", "person", "derivation",
         "axiom", "definition", "goal"]


def populate(home: Path, n: int) -> list:
    """n nodes with ids that sort out of insertion order, spread over 60 days, every type the draw looks at,
    NULL / non-ASCII / multi-line cells, some not current, some introspective.  Returns the (id, claim) pairs."""
    conn = sqlite3.connect(str(home / "knowledge.db"))
    out = []
    for i in range(n):
        nid = f"{TYPES[i % len(TYPES)][:2]}_{(i * 7919) % 100003:06d}_{i}"
        claim = f"claim-{i} word{i * 31 % 977} {'naïve 日本語' if i % 9 == 0 else 'plain'}"
        if i % 13 == 0:
            claim += "\nsecond line"
        quote = None if i % 5 == 0 else f"quote-{i} text{i * 17 % 701}"
        interp = "" if i % 11 == 0 else f"interp-{i} note{i * 13 % 389}"
        meta = json.dumps({"source_class": "introspective"}) if i % 3 == 0 else (None if i % 4 == 0 else "{}")
        created = f"2026-0{1 + (i * 7) % 2}-{1 + (i * 5) % 27:02d}T{i % 24:02d}:00:00+00:00"
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, quoted_text, interpretation, metadata, is_current) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (nid, TYPES[i % len(TYPES)], claim, created, quote, interp, meta, 0 if i % 17 == 0 else 1))
        out.append((nid, claim))
    conn.commit()
    conn.close()
    return out


def write_decoys(path: Path) -> Path:
    path.write_text(json.dumps([{"claim": f"decoy claim {i}", "quote": f"decoy quote {i}", "interpretation": "d"}
                                for i in range(4)]), encoding="utf-8")
    return path


def cli(home: Path, *args, path=TOOL):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(home)
    return subprocess.run([sys.executable, str(path), *map(str, args)], capture_output=True, text=True, env=env,
                          timeout=120)


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


def draw_direct(home: Path, tmp: Path, seed="seed-1"):
    """(a) a direct read: a COPY of the store at another path, so the tool takes the helper's mode=ro open; no
    owner exists for it."""
    copy = tmp / "copy"
    copy.mkdir()
    shutil.copyfile(home / "knowledge.db", copy / "knowledge.db")
    out = tmp / "out-direct"
    r = cli(home, "draw", "--seed", seed, "--decoys", write_decoys(tmp / "d1.json"), "--out", out,
            "--db", copy / "knowledge.db", "--allow-short")
    assert r.returncode == 0, r.stderr
    return out, r


def draw_owner(home: Path, tmp: Path, seed="seed-1"):
    """(b) the owner path: the default db of ENGRAM_HOME, no --db."""
    out = tmp / "out-owner"
    r = cli(home, "draw", "--seed", seed, "--decoys", write_decoys(tmp / "d2.json"), "--out", out, "--allow-short")
    assert r.returncode == 0, r.stderr
    return out, r


def load_tool(home: Path, monkeypatch, name="msr_4616"):
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("ENGRAM_RUNTIME_DIR", raising=False)
    spec = importlib.util.spec_from_file_location(name, str(TOOL))
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TOOL.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(TOOL.parent))
    return mod


# ---- T / G ---------------------------------------------------------------------------------------------


def test_t1_the_page_statement_is_registered_byte_for_byte_under_the_site():
    entries = [e for e in json.loads(TEMPLATES_JSON.read_text())["templates"] if e["sql"] == SQL]
    assert len(entries) == 1
    assert entries[0]["sites"] == [SITE]
    assert SITE in json.loads(SCAN_CONFIG.read_text())["roots"]


def test_t1_the_production_loader_admits_it(home):
    import engram_idf
    import engramd_read_registry as reg

    rw = sqlite3.connect(str(home / "knowledge.db"))
    engram_idf.ensure_vocab_table(rw)
    rw.commit()
    rw.close()
    conn = sqlite3.connect(f"file:{home / 'knowledge.db'}?mode=ro", uri=True)
    try:
        loaded = reg.load_registry(TEMPLATES_JSON, conn)
    finally:
        conn.close()
    assert SQL not in loaded.refused, loaded.refused.get(SQL)


def test_t4_the_module_constant_and_the_page_size_are_the_registered_bytes(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    assert mod._PAGE_SQL == SQL
    assert mod.PAGE_ROWS == P


def test_g1_the_tool_holds_no_connect_call():
    import ast

    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "connect"]
    assert calls == []
    assert "sqlite3.connect(" not in TOOL.read_text(encoding="utf-8").replace("`sqlite3.connect(`", "")


# ---- D1 ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 199, 200, 201, 450])
def test_d1_same_draw_through_a_fake_owner_and_a_direct_read(n, home, tmp_path):
    if n:
        populate(home, n)
    if n == 0:
        # an empty graph draws nothing: both ways refuse with the same line
        copy = tmp_path / "copy"
        copy.mkdir()
        shutil.copyfile(home / "knowledge.db", copy / "knowledge.db")
        rd = cli(home, "draw", "--seed", "s", "--decoys", write_decoys(tmp_path / "d.json"), "--out",
                 tmp_path / "o1", "--db", copy / "knowledge.db")
        with FakeOwner(home):
            ro = cli(home, "draw", "--seed", "s", "--decoys", tmp_path / "d.json", "--out", tmp_path / "o2")
        assert (rd.returncode, rd.stdout, rd.stderr) == (ro.returncode, ro.stdout, ro.stderr)
        assert rd.returncode == 1 and rd.stderr.startswith("model_switch_read: no own candidates")
        return
    out_d, _ = draw_direct(home, tmp_path)
    with FakeOwner(home):
        out_o, _ = draw_owner(home, tmp_path)
    assert (out_o / "cards.md").read_bytes() == (out_d / "cards.md").read_bytes()
    assert (out_o / "key.json").read_bytes() == (out_d / "key.json").read_bytes()
    key = json.loads((out_o / "key.json").read_text())
    assert sum(1 for v in key["items"].values() if v["source"] == "own") > 0


@requires_linux_owner
def test_d1_same_draw_through_a_REAL_owner_and_a_direct_read(home, tmp_path):
    populate(home, 450)
    out_d, _ = draw_direct(home, tmp_path)
    with real_owner(home):
        out_o, r = draw_owner(home, tmp_path)
    assert (out_o / "cards.md").read_bytes() == (out_d / "cards.md").read_bytes()
    assert (out_o / "key.json").read_bytes() == (out_d / "key.json").read_bytes()
    assert r.stderr == ""


def test_d1_the_key_maps_items_to_the_same_sources_both_ways_for_another_seed(home, tmp_path):
    populate(home, 450)
    out_d, _ = draw_direct(home, tmp_path, seed="another")
    with FakeOwner(home):
        out_o, _ = draw_owner(home, tmp_path, seed="another")
    kd = json.loads((out_d / "key.json").read_text())["items"]
    ko = json.loads((out_o / "key.json").read_text())["items"]
    assert kd == ko and len(kd) == 4 + sum(1 for v in kd.values() if v["source"] == "own")


# ---- B1 / L1 -------------------------------------------------------------------------------------------


def test_b1_ping_first_then_one_read_per_page(home, tmp_path):
    populate(home, 450)
    with FakeOwner(home) as owner:
        draw_owner(home, tmp_path)
    methods = [r["method"] for r in owner.requests]
    assert methods[0] == "ping"
    reads = [r for r in owner.requests if r["method"] == "read"]
    assert len(reads) == 450 // P + 1 and len(methods) == 1 + len(reads)
    assert all(r["params"]["sql"] == SQL for r in reads)
    assert [r["params"]["args"] for r in reads][0] == [""]


def test_l1_no_own_id_and_no_own_text_on_stdout_or_stderr_on_the_owner_path(home, tmp_path):
    rows = populate(home, 450)
    with FakeOwner(home):
        out, r = draw_owner(home, tmp_path)
    seen = r.stdout + r.stderr
    for nid, claim in rows:
        assert nid not in seen
        assert claim.splitlines()[0] not in seen


# ---- O1 / O2 -------------------------------------------------------------------------------------------


def _one_line_refusal(r):
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert r.stdout == ""
    assert r.stderr.count("\n") == 1 and r.stderr.startswith("model_switch_read: "), r.stderr
    assert "to false" not in r.stderr and "read the store directly." not in r.stderr, r.stderr
    assert "never reads the seat's store directly" in r.stderr


def test_o1_owner_not_running_stops_with_one_line_and_writes_nothing(home, tmp_path):
    populate(home, 450)
    out = tmp_path / "out"
    r = cli(home, "draw", "--seed", "s", "--decoys", write_decoys(tmp_path / "d.json"), "--out", out)
    _one_line_refusal(r)
    assert "owner" in r.stderr
    assert not out.exists()


def test_o1_a_db_naming_the_homes_store_by_another_path_is_still_the_owner_path(home, tmp_path):
    populate(home, 450)
    link = tmp_path / "link.db"
    link.symlink_to(home / "knowledge.db")
    out = tmp_path / "out"
    r = cli(home, "draw", "--seed", "s", "--decoys", write_decoys(tmp_path / "d.json"), "--out", out, "--db", link)
    _one_line_refusal(r)
    assert not out.exists()


def test_o1_zero_connects_when_the_owner_is_down(home, tmp_path, monkeypatch, capsys):
    populate(home, 450)
    mod = load_tool(home, monkeypatch)
    connects = []
    real = sqlite3.connect

    def spy(*a, **k):
        connects.append(a)
        return real(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy)
    rc = mod.main(["draw", "--seed", "s", "--decoys", str(write_decoys(tmp_path / "d.json")), "--out",
                   str(tmp_path / "out")])
    out, err = capsys.readouterr()
    assert rc == 1 and out == "" and err.startswith("model_switch_read: ")
    assert connects == []


def test_o1_a_dying_or_unreachable_owner_after_ping_stops_the_run(home, tmp_path, monkeypatch, capsys):
    """The owner answers the ping and then every read is refused (a fake with an empty registry): the draw stops."""
    populate(home, 450)
    with FakeOwner(home) as owner:
        owner._registered = set()
        r = cli(home, "draw", "--seed", "s", "--decoys", write_decoys(tmp_path / "d.json"), "--out",
                tmp_path / "out")
    _one_line_refusal(r)
    assert "unregistered_template" in r.stderr
    assert not (tmp_path / "out").exists()


# ---- O3 ------------------------------------------------------------------------------------------------


class StubConn:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, list(params)))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return type("Cur", (), {"fetchall": lambda s: page})()


def row(i, **kw):
    cells = [f"id_{i:05d}", "lesson", "c", "2026-01-01T00:00:00+00:00", None, None, 1, None]
    for k, v in kw.items():
        cells[("id", "type", "claim", "created_at", "quoted_text", "interpretation", "is_current", "metadata").index(k)] = v
    return cells


def full_page(start):
    return [row(i) for i in range(start, start + P)]


def test_o3_malformed_answers_raise_and_terminate(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    for pages in (
        [full_page(0), full_page(0)],                          # a repeated page
        [[row(0, is_current=True)]],                           # a bool where an integer belongs
        [[row(0)[:5]]],                                        # a short row
        [full_page(0) + [row(999)]],                           # an over-long page
        [[row(0, claim=5)]],                                   # a non-text cell
        [{"not": "a list"}],                                   # not rows at all
        [[row(1), row(0)]],                                    # ids not increasing
    ):
        with pytest.raises(mod.OwnerReadError):
            mod.fetch_nodes(StubConn(pages))


def test_o3_a_failure_after_page_one_discards_everything(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    with pytest.raises(mod.Refusal):
        mod.fetch_nodes(StubConn([full_page(0), sqlite3.OperationalError("boom")]))


def test_o3_a_page_stream_is_complete_and_cursor_advances(home, monkeypatch):
    mod = load_tool(home, monkeypatch)
    conn = StubConn([full_page(0), full_page(P), [row(2 * P)]])
    nodes = mod.fetch_nodes(conn)
    assert [n["id"] for n in nodes] == [f"id_{i:05d}" for i in range(2 * P + 1)]
    assert [c[1] for c in conn.calls] == [[""], [f"id_{P - 1:05d}"], [f"id_{2 * P - 1:05d}"]]
