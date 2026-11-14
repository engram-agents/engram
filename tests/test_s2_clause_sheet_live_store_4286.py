"""Cells for the live-store refusal in tools/s2_clause_sheet.py (engram-alpha#4286).

Contract: the tool only ever reads COPIES. A ``--db`` that is the live store
(``engram_home() / "knowledge.db"``, or a symlink or hard link to it) is refused
before any read, for every subcommand. A copy anywhere else, including inside the
ENGRAM_HOME, is accepted. A relative ENGRAM_HOME makes the check unable to run, so
it refuses. On a copy, the outputs stay byte-identical (the study's sheets are
frozen by hash).

Every cell runs in a scratch ENGRAM_HOME set through the environment, on a tiny
hand-built ``nodes`` table. No real home and no real graph is touched. Similarity
is stubbed, as in the other s2 cells.
"""

import hashlib
import importlib.util
import itertools
import json
import os
import sqlite3
from pathlib import Path

import pytest

_TOOL_PATH = (Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"
              / "s2_clause_sheet.py")
_COUNTER = itertools.count()
IN_WINDOW = "2026-10-15T00:00:00Z"
SIMS = (0.1, 0.3, 0.5, 0.7)  # one per bin B1..B4
SALT = "0123456789abcdef"
BUILD = "abc1234"
ROSTER = {"names": ["Quillon", "Marrowby"], "handles": ["quillon-bot"]}
PLANTED = [dict(planted_id=f"zp_900{n}", quote=f"planted quote {n}", interpretation="planted interp",
                clauses=[f"pc {n}"], kind=f"P{n}") for n in (1, 2, 3, 4)]


def _load_tool():
    spec = importlib.util.spec_from_file_location(f"s2_live_store_{next(_COUNTER)}", _TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows():
    return [dict(id=f"zz_{b * 100 + i:04d}", sim=SIMS[b]) for b in range(4) for i in range(1, 13)]


def _write_db(path: Path, rows) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT, claim TEXT, created_at TEXT,"
                 " quoted_text TEXT, interpretation TEXT, quote_type TEXT, is_current INTEGER, metadata TEXT)")
    for r in rows:
        conn.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?)",
                     (r["id"], "observation_factual", f"claim text for {r['id']}", IN_WINDOW,
                      f"quote text for {r['id']}", f"interpretation of {r['id']}", "hard_data", 1,
                      json.dumps({"source_class": "external"})))
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return path


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A scratch ENGRAM_HOME holding the 'live' store, a tool with similarity stubbed and
    ``_open_readonly`` wrapped to record every call."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    rows = _rows()
    live = _write_db(home / "knowledge.db", rows)
    tool = _load_tool()
    sims = {f"quote text for {r['id']}": r["sim"] for r in rows}
    monkeypatch.setattr(tool.engram_anchoring, "quote_claim_similarity",
                        lambda quote, claim: (sims[quote], None))
    calls = []
    real_open = tool._open_readonly

    def recording_open(db_path):
        calls.append(str(db_path))
        return real_open(db_path)
    monkeypatch.setattr(tool, "_open_readonly", recording_open)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    copy = _write_db(scratch / "copy.db", rows)
    return dict(tool=tool, home=home, live=live, copy=copy, scratch=scratch, calls=calls, tmp=tmp_path)


def _snapshot(d: Path):
    if not d.exists():
        return None
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


def _argv(e, cmd, db, out):
    """argv for `cmd` against `db`, with `out` as the out-dir. Support files live beside, not in, `out`."""
    base = [cmd, "--db", str(db), "--out-dir", str(out)]
    if cmd == "select":
        return base + ["--build-commit", BUILD]
    if cmd == "swap":
        return base + ["--node", "zz_0001", "--reason", "r"]
    support = e["tmp"] / "support"
    support.mkdir(exist_ok=True)
    (support / "roster.json").write_text(json.dumps(ROSTER))
    (support / "planted.json").write_text(json.dumps(PLANTED))
    key = json.loads((out / "key.json").read_text()) if (out / "key.json").exists() else {"items": []}
    (support / "splits.json").write_text(json.dumps(
        {i["node_id"]: [f"{i['node_id']} one", f"{i['node_id']} two"] for i in key["items"]}))
    return base + ["--splits", str(support / "splits.json"), "--planted", str(support / "planted.json"),
                   "--scrub-roster", str(support / "roster.json"), "--sheet-salt", SALT]


def _prepare(e, cmd):
    """The out-dir the command is run against: empty for select, selected-from-the-copy for swap/render."""
    out = e["tmp"] / f"out_{next(_COUNTER)}"
    if cmd != "select":
        assert e["tool"].main(_argv(e, "select", e["copy"], out)) == 0
        e["calls"].clear()
    return out


def _refused_cleanly(e, cmd, db, capsys):
    out = _prepare(e, cmd)
    before = _snapshot(out)
    capsys.readouterr()
    rc = e["tool"].main(_argv(e, cmd, db, out))
    err = capsys.readouterr().err
    assert rc == 2
    assert err.count("\n") == 1 and err.startswith("refused: "), err
    assert "not found" not in err
    assert "backup" in err and "scratch" in err  # the recipe
    assert e["calls"] == []  # no read happened
    assert _snapshot(out) == before  # nothing written (select: out-dir never created)
    return err


CMDS = ("select", "swap", "render")


@pytest.mark.parametrize("cmd", CMDS)
def test_live_store_refused_before_any_read(env, capsys, cmd):
    err = _refused_cleanly(env, cmd, env["live"], capsys)
    assert str(env["live"]) in err and "live store" in err


@pytest.mark.parametrize("cmd", CMDS)
def test_symlink_to_live_store_refused(env, capsys, cmd):
    link = env["scratch"] / "link.db"
    link.symlink_to(env["live"])
    err = _refused_cleanly(env, cmd, link, capsys)
    assert str(link) in err and "live store" in err


@pytest.mark.parametrize("cmd", CMDS)
def test_hard_link_to_live_store_refused(env, capsys, cmd):
    hard = env["scratch"] / "hard.db"
    os.link(env["live"], hard)
    err = _refused_cleanly(env, cmd, hard, capsys)
    assert str(hard) in err and "live store" in err


@pytest.mark.parametrize("cmd", CMDS)
def test_copy_inside_engram_home_accepted(env, capsys, cmd):
    inside = env["home"] / "research"
    inside.mkdir()
    db = _write_db(inside / "copy.db", _rows())
    out = _prepare(env, cmd)
    assert env["tool"].main(_argv(env, cmd, db, out)) == 0
    assert env["calls"] == [str(db)]  # reached the normal path and read the copy


@pytest.mark.parametrize("cmd", CMDS)
def test_relative_engram_home_refuses(env, monkeypatch, capsys, cmd):
    out = _prepare(env, cmd)
    monkeypatch.setenv("ENGRAM_HOME", "relative/home")
    capsys.readouterr()
    rc = env["tool"].main(_argv(env, cmd, env["copy"], out))
    err = capsys.readouterr().err
    assert rc == 2
    assert err.count("\n") == 1 and err.startswith("refused: ")
    assert "ENGRAM_HOME must be an absolute path" in err
    assert env["calls"] == []


@pytest.mark.parametrize("cmd", CMDS)
def test_missing_db_is_not_a_live_store_match(env, capsys, cmd):
    """A --db that does not exist falls through to today's behaviour (no 'live store' refusal)."""
    out = _prepare(env, cmd)
    rc = None
    try:
        rc = env["tool"].main(_argv(env, cmd, env["scratch"] / "absent.db", out))
    except sqlite3.OperationalError:
        pass  # today's behaviour: the read-only open fails
    assert "live store" not in capsys.readouterr().err
    assert rc != 0


def test_missing_live_store_is_not_a_match(env, capsys):
    env["live"].unlink()
    out = _prepare(env, "select")
    assert env["tool"].main(_argv(env, "select", env["copy"], out)) == 0


# ---- C4: byte-identity on a copy, against pins captured from the UNCHANGED tool ----
# Pinned expected values: sha256 of each output file, computed by running the tool BEFORE the change.
GOLDEN_SELECT = {
    "claims.json": "c21bb18851821f76ab7b7f204a801c153a554e02e1231027ba633b17cb8d3086",
    "screen.md": "91f9c9f00c4b2af07340d2ad94f58313aa8047d6930bfa7d0697fcd16ffebdb8",
    "key.json": "615bc2a8be0ad29b71218c53988e905d11a361d8c5722daf8f1a8516e3eba19f",
}
GOLDEN_RENDER = {
    "claims.json": "c21bb18851821f76ab7b7f204a801c153a554e02e1231027ba633b17cb8d3086",
    "screen.md": "91f9c9f00c4b2af07340d2ad94f58313aa8047d6930bfa7d0697fcd16ffebdb8",
    "key.json": "60728f5bfa54da2ab4af28e610381c699f5375e11a6053af740fc15edc82f360",
    "sheet-pass1.md": "927b71a44f5fc036186da520518902e077917bc4e798d471106f282956789266",
    "sheet-pass2.md": "6196c3100443d7309fb81cc4f59785eb8fe44604a85f6d1c71956dbddca60b1c",
}


def _hashes(out: Path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir())}


def _byte_run(env):
    out = env["tmp"] / "golden_out"
    assert env["tool"].main(_argv(env, "select", env["copy"], out)) == 0
    select_hashes = _hashes(out)
    assert env["tool"].main(_argv(env, "render", env["copy"], out)) == 0
    return select_hashes, _hashes(out)


def test_outputs_byte_identical_on_a_copy(env):
    select_hashes, render_hashes = _byte_run(env)
    if os.environ.get("S2_4286_PRINT_PINS"):
        print(json.dumps({"select": select_hashes, "render": render_hashes}, indent=2))
    assert select_hashes == GOLDEN_SELECT
    assert render_hashes == GOLDEN_RENDER
