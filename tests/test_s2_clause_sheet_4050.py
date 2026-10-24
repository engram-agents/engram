"""Cells for tools/s2_clause_sheet.py (engram-alpha#4050 S2) -- the clause-check
sheet builder for the signed S2 pre-registration (draft 5).

Contract-first: these cells were written before the tool. Every cell builds its
own small graph on a copy of a REAL schema (created once through the real
server) and inserts rows with raw SQL; no real graph is ever opened. Similarity
is stubbed by patching ``engram_anchoring.quote_claim_similarity`` (keyed by
quote text). Fixture ids use the ``zz_`` / ``zq_`` prefixes, which are not
node-type prefixes.
"""

import atexit
import hashlib
import importlib.util
import itertools
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TOOL_PATH = (Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"
              / "s2_clause_sheet.py")

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_s2_4050_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TEMPLATE: dict = {}
_COUNTER = itertools.count()

SEED = "d638538e"
IN_WINDOW = "2026-10-15T00:00:00Z"


# --------------------------------------------------------------------------- fixtures
def _template_db() -> Path:
    if "path" in _TEMPLATE:
        return _TEMPLATE["path"]
    home = Path(TEST_DIR) / "template_home"
    shutil.rmtree(home, ignore_errors=True)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_DB_GUARDS"] = "1"
    import server
    server._configure_paths(str(home))
    server._ensure_data_dir()
    conn = server._get_db()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    _TEMPLATE["path"] = home / "knowledge.db"
    return _TEMPLATE["path"]


def _row(id, sim, **kw):  # noqa: A002 -- the keyword is the row's "id" key
    node_id = id
    r = dict(id=node_id, sim=sim, created=IN_WINDOW, type="observation_factual",
             source_class="external", current=1, quote_type="hard_data",
             interp=f"interpretation of {node_id}")
    r.update(kw)
    r.setdefault("quote", f"quote text for {node_id}")
    r.setdefault("claim", f"claim text for {node_id}")
    return r


def _make_db(rows):
    path = Path(TEST_DIR) / f"db_{next(_COUNTER)}.db"
    shutil.copy(_template_db(), path)
    conn = sqlite3.connect(path)
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, quoted_text, interpretation,"
            " quote_type, is_current, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            (r["id"], r["type"], r["claim"], r["created"], r["quote"], r["interp"],
             r["quote_type"], r["current"], json.dumps({"source_class": r["source_class"]})),
        )
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return path


def _load_tool():
    name = f"s2_clause_sheet_{next(_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, _TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tool(monkeypatch):
    return _load_tool()


def _stub(monkeypatch, tool, rows):
    sims = {r["quote"]: r["sim"] for r in rows}

    def fake(quote, claim):
        v = sims[quote]
        return (None, "embedder_unavailable") if v is None else (v, None)
    monkeypatch.setattr(tool.engram_anchoring, "quote_claim_similarity", fake)


def _setup(monkeypatch, tool, rows):
    rows = [_row(**r) if isinstance(r, dict) else r for r in rows]
    _stub(monkeypatch, tool, rows)
    return _make_db(rows)


def _out():
    d = Path(TEST_DIR) / f"out_{next(_COUNTER)}"
    return d


def _select(tool, db, out):
    return tool.main(["select", "--db", str(db), "--out-dir", str(out),
                      "--seed-hex", SEED, "--build-commit", "abc1234"])


def _key(out):
    return json.loads((out / "key.json").read_text())


def _claims(out):
    return json.loads((out / "claims.json").read_text())


def _h(i):
    return hashlib.sha256((SEED + i).encode("utf-8")).hexdigest()


def _rows(prefix, n, sim, **kw):
    return [dict(id=f"{prefix}_{2000 + i}", sim=sim, **kw) for i in range(1, n + 1)]


# --------------------------------------------------------------------------- S1
def test_s1_sort_rule(monkeypatch, tool):
    rows = _rows("zq", 14, 0.1)
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    ids = [r["id"] for r in rows]
    expected = sorted(ids, key=_h)[:10]
    got = [c["node_id"] for c in _claims(out)]
    assert got == expected
    # Literal pins, computed outside this test with an independent hashlib call, so a
    # test that recomputes with the same bug cannot pass.
    assert got[0] == "zq_2009"
    assert got[1] == "zq_2004"
    key = _key(out)
    assert [i["node_id"] for i in key["items"]] == expected
    assert key["items"][0]["sort_hash"] == _h("zq_2009")
    assert key["seed_hex"] == SEED and key["seed"] == int(SEED, 16)
    assert set(_claims(out)[0]) == {"node_id", "claim"}


# --------------------------------------------------------------------------- S2
def test_s2_window_edges(monkeypatch, tool):
    rows = [
        dict(id="zq_3001", sim=0.1, created="2026-10-10T15:00:00Z"),   # start: in
        dict(id="zq_3002", sim=0.1, created="2026-10-24T15:00:00Z"),   # end: out
        dict(id="zq_3003", sim=0.1, created="2026-10-24T14:59:59Z"),   # a second before: in
        dict(id="zq_3004", sim=0.1, created="2026-10-10T14:59:59Z"),   # a second before start: out
    ]
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    assert {c["node_id"] for c in _claims(out)} == {"zq_3001", "zq_3003"}
    assert _key(out)["window"] == ["2026-10-10T15:00:00Z", "2026-10-24T15:00:00Z"]


# --------------------------------------------------------------------------- S3
def test_s3_exclusions(monkeypatch, tool):
    rows = [
        dict(id="zq_3101", sim=0.1),
        dict(id="zq_3102", sim=0.1, source_class="user_stated"),
        dict(id="zq_3103", sim=0.1, type="observation_predictive"),
        dict(id="zq_3104", sim=0.1, current=0),
    ]
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    assert [c["node_id"] for c in _claims(out)] == ["zq_3101"]


# --------------------------------------------------------------------------- S4
def test_s4_null_similarity_excluded_and_counted(monkeypatch, tool):
    rows = [dict(id="zq_3201", sim=0.1), dict(id="zq_3202", sim=None), dict(id="zq_3203", sim=None)]
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    assert [c["node_id"] for c in _claims(out)] == ["zq_3201"]
    assert _key(out)["null_similarity_count"] == 2


# --------------------------------------------------------------------------- S5
def test_s5_bin_edges(monkeypatch, tool):
    rows = [dict(id="zq_3301", sim=0.2), dict(id="zq_3302", sim=0.6),
            dict(id="zq_3303", sim=1.0), dict(id="zq_3304", sim=-0.1),
            dict(id="zq_3305", sim=0.4), dict(id="zq_3306", sim=0.0)]
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    bins = {i["node_id"]: i["bin"] for i in _key(out)["items"]}
    assert bins == {"zq_3301": "B2", "zq_3302": "B4", "zq_3303": "B4",
                    "zq_3304": "B1", "zq_3305": "B3", "zq_3306": "B1"}
    assert _key(out)["population_per_bin"] == {"B1": 2, "B2": 1, "B3": 1, "B4": 2}


# --------------------------------------------------------------------------- S6
def test_s6_shortfall(monkeypatch, tool):
    rows = _rows("zq", 3, 0.5) + _rows("zr", 12, 0.7)
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    key = _key(out)
    per_bin = {}
    for i in key["items"]:
        per_bin.setdefault(i["bin"], []).append(i["node_id"])
    assert len(per_bin["B3"]) == 3 and len(per_bin["B4"]) == 10
    assert key["shortfalls"] == {"B1": 10, "B2": 10, "B3": 7}


# --------------------------------------------------------------------------- S7
def test_s7_swap_next_in_same_bin_and_exhaustion(monkeypatch, tool):
    rows = _rows("zq", 14, 0.1) + _rows("zr", 12, 0.5)
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    removed = "zq_2009"
    assert tool.main(["swap", "--db", str(db), "--out-dir", str(out),
                      "--node", removed, "--reason", "private matter"]) == 0
    key = _key(out)
    # 11th in the B1 order, pinned literally (independent hashlib computation)
    assert key["swaps"] == [{"removed": removed, "added": "zq_2003", "bin": "B1",
                             "reason": "private matter"}]
    ids = {c["node_id"] for c in _claims(out)}
    assert "zq_2003" in ids and removed not in ids
    assert len(ids) == 20
    assert "private matter" not in (out / "claims.json").read_text()
    # B1 has 14 candidates: 3 more swaps exhaust the bin, the 4th refuses
    for victim in ("zq_2004", "zq_2002", "zq_2006"):
        assert tool.main(["swap", "--db", str(db), "--out-dir", str(out),
                          "--node", victim, "--reason", "r"]) == 0
    before = (out / "key.json").read_bytes()
    assert tool.main(["swap", "--db", str(db), "--out-dir", str(out),
                      "--node", "zq_2011", "--reason", "r"]) != 0
    assert (out / "key.json").read_bytes() == before


# --------------------------------------------------------------------------- render helpers
def _built(monkeypatch, tool, interp_by_id=None, n=3):
    rows = []
    for r in _rows("zq", n, 0.1):
        if interp_by_id and r["id"] in interp_by_id:
            r["interp"] = interp_by_id[r["id"]]
        rows.append(r)
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    return db, out


def _splits(out, path=None):
    sp = {c["node_id"]: [f"{c['node_id']} clause one", f"{c['node_id']} clause two"]
          for c in _claims(out)}
    path = path or (out.parent / f"splits_{next(_COUNTER)}.json")
    path.write_text(json.dumps(sp))
    return path


def _planted(interp="planted interp"):
    return [
        dict(planted_id="zp_9001", quote="planted quote one", interpretation=interp,
             clauses=["pc one"], kind="P1"),
        dict(planted_id="zp_9002", quote="planted quote two", interpretation=interp,
             clauses=["pc two"], kind="P2"),
        dict(planted_id="zp_9003", quote="planted quote three", interpretation=interp,
             clauses=["pc three", "pc three b"], kind="P3"),
        dict(planted_id="zp_9004", quote="planted quote four", interpretation=interp,
             clauses=["pc four", "pc four b"], kind="P4"),
    ]


# An invented roster: no real person, agent or handle appears in this file.
ROSTER = {"names": ["Quillon", "Marrowby", "Tesselby"],
          "handles": ["quillon-bot", "marrowby-dev"]}
_DEFAULT = object()


def _write_roster(out, roster):
    p = out.parent / f"roster_{next(_COUNTER)}.json"
    p.write_text(roster if isinstance(roster, str) else json.dumps(roster))
    return p


def _render(tool, db, out, splits, planted=None, draft=False, roster=_DEFAULT):
    argv = ["render", "--db", str(db), "--out-dir", str(out), "--splits", str(splits)]
    if roster is _DEFAULT:
        roster = ROSTER
    if roster is not None:
        argv += ["--scrub-roster", str(_write_roster(out, roster))]
    if draft:
        argv.append("--draft")
    if planted is not None:
        p = out.parent / f"planted_{next(_COUNTER)}.json"
        p.write_text(json.dumps(planted))
        argv += ["--planted", str(p)]
    return tool.main(argv)


# --------------------------------------------------------------------------- S8
def test_s8_render_refuses_bad_splits(monkeypatch, tool):
    db, out = _built(monkeypatch, tool)
    good = json.loads(_splits(out).read_text())
    missing = dict(good)
    missing.pop(next(iter(missing)))
    p1 = out.parent / f"s_{next(_COUNTER)}.json"
    p1.write_text(json.dumps(missing))
    assert _render(tool, db, out, p1, _planted()) != 0
    extra = dict(good)
    extra["zq_9999"] = ["x"]
    p2 = out.parent / f"s_{next(_COUNTER)}.json"
    p2.write_text(json.dumps(extra))
    assert _render(tool, db, out, p2, _planted()) != 0
    assert not (out / "sheet-pass1.md").exists() and not (out / "sheet-pass2.md").exists()


# --------------------------------------------------------------------------- S9
def test_s9_pass1_hides_everything_pass2_shows_scrubbed_interp(monkeypatch, tool):
    token = "DISTINCTIVETOKEN"
    interp = f"{token} said Quillon about it"
    db, out = _built(monkeypatch, tool, {"zq_2001": interp, "zq_2002": interp, "zq_2003": interp})
    assert _render(tool, db, out, _splits(out), _planted(interp=interp)) == 0
    p1 = (out / "sheet-pass1.md").read_text()
    p2 = (out / "sheet-pass2.md").read_text()
    for text in (p1, p2):
        assert "zq_200" not in text and "zp_900" not in text
        assert "hard_data" not in text
        assert "B1" not in text
        assert "0.1" not in text
    assert token not in p1 and "said" not in p1 and "interpretation of" not in p1
    assert "Quillon" not in p2 and "[name]" in p2
    assert token in p2
    # pass 1 carries quote, clauses, and the label / flag lines
    assert "> quote text for" in p1 and "clause one" in p1
    assert "Label: " in p1 and "Flag (optional): " in p1
    assert "> planted quote one" in p1
    assert "planted interp" not in p1


# --------------------------------------------------------------------------- S10
def test_s10_pass2_scrub(monkeypatch, tool):
    interp = ("see zz_1234 where Quillon (quillon-bot) agreed; Marrowby and marrowby-dev "
              "and tesselby too; Marrowbyton stays")
    db, out = _built(monkeypatch, tool, {"zq_2001": interp})
    assert _render(tool, db, out, _splits(out), _planted()) == 0
    p2 = (out / "sheet-pass2.md").read_text()
    assert ("see [node] where [name] ([handle]) agreed; [name] and [handle] and [name] too; "
            "Marrowbyton stays") in p2
    for leaked in ("zz_1234", "Quillon", "quillon-bot", "marrowby-dev", "tesselby"):
        assert leaked not in p2
    scrub = _key(out)["scrub"]
    assert scrub["node_id_regex"] == "[a-z]{2}_\\d{4}"
    assert scrub["order"] == ["node_id", "handles", "names"]
    assert scrub["names_placeholder"] == "[name]" and scrub["handles_placeholder"] == "[handle]"
    assert scrub["names_count"] == 3 and scrub["handles_count"] == 2


# --------------------------------------------------------------------------- S10b (roster)
def test_s10b_render_without_roster_refuses(monkeypatch, tool):
    db, out = _built(monkeypatch, tool)
    assert _render(tool, db, out, _splits(out), _planted(), roster=None) == 2
    assert not (out / "sheet-pass1.md").exists() and not (out / "sheet-pass2.md").exists()


@pytest.mark.parametrize("bad", [
    {"names": [], "handles": ["quillon-bot"]},
    {"names": ["Quillon"], "handles": []},
    {"names": ["Quillon"]},
    {"names": "Quillon", "handles": ["quillon-bot"]},
    {"names": ["Quillon", ""], "handles": ["quillon-bot"]},
    ["Quillon"],
    "{not json",
])
def test_s10c_malformed_or_empty_roster_refuses(monkeypatch, tool, bad):
    db, out = _built(monkeypatch, tool)
    assert _render(tool, db, out, _splits(out), _planted(), roster=bad) == 2
    assert not (out / "sheet-pass1.md").exists()


def test_s10d_missing_roster_file_refuses(monkeypatch, tool):
    db, out = _built(monkeypatch, tool)
    argv = ["render", "--db", str(db), "--out-dir", str(out), "--splits", str(_splits(out)),
            "--scrub-roster", str(out.parent / "no_such_roster.json")]
    assert tool.main(argv) == 2


def test_s10e_key_has_roster_sha_and_counts_but_no_names(monkeypatch, tool):
    db, out = _built(monkeypatch, tool)
    assert _render(tool, db, out, _splits(out), _planted()) == 0
    raw = (out / "key.json").read_text()
    for word in ROSTER["names"] + ROSTER["handles"]:
        assert word.lower() not in raw.lower()
    scrub = json.loads(raw)["scrub"]
    roster_files = sorted(out.parent.glob("roster_*.json"), key=lambda q: q.stat().st_mtime)
    assert scrub["roster_sha256"] == hashlib.sha256(roster_files[-1].read_bytes()).hexdigest()
    assert (scrub["names_count"], scrub["handles_count"]) == (3, 2)
    assert "names" not in scrub and "handles" not in scrub
    assert "names_regex" not in scrub and "handles_regex" not in scrub



# --------------------------------------------------------------------------- S11
def _oh(i):
    """Presentation hash, recomputed here with a literal salt, independent of the tool's helper."""
    return hashlib.sha256((SEED + "order" + i).encode("utf-8")).hexdigest()


# Removed-test: tests/test_s2_clause_sheet_4050.py::test_s11_item_order_follows_sort_rule — renamed to test_s11_item_order_follows_presentation_rule; draft 5 replaced the selection key with the salted presentation key for item order.
def test_s11_item_order_follows_presentation_rule(monkeypatch, tool):
    db, out = _built(monkeypatch, tool, n=4)
    assert _render(tool, db, out, _splits(out), _planted()) == 0
    ids = [c["node_id"] for c in _claims(out)] + ["zp_9001", "zp_9002", "zp_9003", "zp_9004"]
    expected = sorted(ids, key=_oh)
    key = _key(out)
    order = [key["render"]["items"][str(n)]["id"] for n in range(1, len(ids) + 1)]
    assert order == expected
    assert key["render"]["items"][str(expected.index("zp_9003") + 1)]["kind"] == "P3"
    assert key["render"]["items"][str(expected.index("zp_9003") + 1)]["clauses"] == 2
    p1 = (out / "sheet-pass1.md").read_text()
    assert f"## Item {len(ids)}" in p1
    # the planted quote appears at its sorted position
    pos = p1.index("planted quote three")
    n_at = expected.index("zp_9003") + 1
    assert p1.index(f"## Item {n_at}") < pos
    for f, name in (("sheet-pass1.md", "pass1"), ("sheet-pass2.md", "pass2")):
        assert key["render"]["sha256"][f] == hashlib.sha256((out / f).read_bytes()).hexdigest()


# --------------------------------------------------------------------------- S12
def test_s12_no_overwrite(monkeypatch, tool):
    db, out = _built(monkeypatch, tool)
    claims_before = (out / "claims.json").read_bytes()
    key_before = (out / "key.json").read_bytes()
    assert _select(tool, db, out) != 0
    assert (out / "claims.json").read_bytes() == claims_before
    assert (out / "key.json").read_bytes() == key_before
    # sheets are never overwritten either
    sp = _splits(out)
    assert _render(tool, db, out, sp, _planted()) == 0
    first = (out / "sheet-pass1.md").read_bytes()
    assert _render(tool, db, out, sp, _planted()) != 0
    assert (out / "sheet-pass1.md").read_bytes() == first


# --------------------------------------------------------------------------- S13
def test_s13_db_bytes_unchanged(monkeypatch, tool):
    rows = _rows("zq", 14, 0.1) + _rows("zr", 4, 0.9)
    db = _setup(monkeypatch, tool, rows)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    siblings_before = sorted(p.name for p in db.parent.glob(db.name + "*"))
    out = _out()
    assert _select(tool, db, out) == 0
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in db.parent.glob(db.name + "*")) == siblings_before


# --------------------------------------------------------------------------- round 2 (PR 4140 reviewer round 1)
def _stderr(capsys):
    return capsys.readouterr().err


def _screen_rows():
    return [dict(id=f"zq_{2000 + i}", sim=0.1, quote=f"QUOTETOKEN{i} words", claim=f"CLAIMTOKEN{i} words",
                 interp=f"INTERPTOKEN{i} words") for i in range(1, 15)]


# S14: the privacy screen file shows what the judges see
def test_s14_screen_md_has_quote_claim_interpretation(monkeypatch, tool):
    db = _setup(monkeypatch, tool, _screen_rows())
    out = _out()
    assert _select(tool, db, out) == 0
    scr = out / "screen.md"
    assert scr.exists() and (scr.stat().st_mode & 0o777) == 0o600
    text = scr.read_text()
    ids = [c["node_id"] for c in _claims(out)]
    assert len(ids) == 10
    last = -1
    for nid in ids:  # selection order, every selected item present
        n = int(nid.split("_")[1]) - 2000
        for tok in (nid, f"QUOTETOKEN{n} ", f"CLAIMTOKEN{n} ", f"INTERPTOKEN{n} "):
            assert tok in text
        at = text.index(nid)
        assert at > last
        last = at
    assert "screen.md" not in (out / "key.json").read_text()
    assert "QUOTETOKEN" not in (out / "claims.json").read_text()


def test_s14b_swap_refreshes_screen_md(monkeypatch, tool):
    db = _setup(monkeypatch, tool, _screen_rows())
    out = _out()
    assert _select(tool, db, out) == 0
    removed = _claims(out)[0]["node_id"]
    assert tool.main(["swap", "--db", str(db), "--out-dir", str(out),
                      "--node", removed, "--reason", "r"]) == 0
    added = _key(out)["swaps"][0]["added"]
    text = (out / "screen.md").read_text()
    n_add = int(added.split("_")[1]) - 2000
    assert removed not in text
    assert added in text and f"QUOTETOKEN{n_add} " in text and f"INTERPTOKEN{n_add} " in text
    assert ((out / "screen.md").stat().st_mode & 0o777) == 0o600
    # the added item sits where the removed one was
    assert [c["node_id"] for c in _claims(out)][0] == added
    assert text.index(added) < text.index(_claims(out)[1]["node_id"])


def test_s14c_screen_md_not_overwritten(monkeypatch, tool):
    db = _setup(monkeypatch, tool, _screen_rows())
    out = _out()
    assert _select(tool, db, out) == 0
    before = (out / "screen.md").read_bytes()
    assert _select(tool, db, out) != 0
    assert (out / "screen.md").read_bytes() == before


# S15: swap after render is refused
def test_s15_swap_after_render_refused(monkeypatch, tool, capsys):
    db = _setup(monkeypatch, tool, _rows("zq", 14, 0.1))
    out = _out()
    assert _select(tool, db, out) == 0
    assert _render(tool, db, out, _splits(out), _planted()) == 0
    key_b, claims_b = (out / "key.json").read_bytes(), (out / "claims.json").read_bytes()
    screen_b = (out / "screen.md").read_bytes()
    capsys.readouterr()
    rc = tool.main(["swap", "--db", str(db), "--out-dir", str(out),
                    "--node", _claims(out)[0]["node_id"], "--reason", "r"])
    assert rc == 2
    assert _stderr(capsys).startswith("refused:")
    assert (out / "key.json").read_bytes() == key_b
    assert (out / "claims.json").read_bytes() == claims_b
    assert (out / "screen.md").read_bytes() == screen_b


def test_s15b_swap_after_draft_render_also_refused(monkeypatch, tool):
    db = _setup(monkeypatch, tool, _rows("zq", 14, 0.1))
    out = _out()
    assert _select(tool, db, out) == 0
    assert _render(tool, db, out, _splits(out), draft=True) == 0
    key_b = (out / "key.json").read_bytes()
    assert tool.main(["swap", "--db", str(db), "--out-dir", str(out),
                      "--node", _claims(out)[0]["node_id"], "--reason", "r"]) == 2
    assert (out / "key.json").read_bytes() == key_b


# S16: planted controls are required for the final sheet
def test_s16_missing_planted_refuses_without_draft(monkeypatch, tool, capsys):
    db, out = _built(monkeypatch, tool)
    capsys.readouterr()
    assert _render(tool, db, out, _splits(out)) == 2
    assert _stderr(capsys).startswith("refused:")
    assert sorted(p.name for p in out.glob("sheet-*")) == []


def test_s16b_draft_then_final(monkeypatch, tool):
    db, out = _built(monkeypatch, tool)
    sp = _splits(out)
    assert _render(tool, db, out, sp, draft=True) == 0
    assert sorted(p.name for p in out.glob("sheet-*")) == ["sheet-pass1.DRAFT.md", "sheet-pass2.DRAFT.md"]
    assert _render(tool, db, out, sp, _planted()) == 0
    assert (out / "sheet-pass1.md").exists() and (out / "sheet-pass2.md").exists()
    assert (out / "sheet-pass1.DRAFT.md").exists()
    key = _key(out)
    assert key["render"]["sha256"]["sheet-pass1.md"] == hashlib.sha256(
        (out / "sheet-pass1.md").read_bytes()).hexdigest()
    # a draft is not overwritten either
    assert _render(tool, db, out, sp, draft=True) != 0


def _bad_planted_refuses(monkeypatch, tool, planted):
    db, out = _built(monkeypatch, tool)
    assert _render(tool, db, out, _splits(out), planted) == 2
    assert not list(out.glob("sheet-*"))


def test_s16c_three_planted_refuse(monkeypatch, tool):
    _bad_planted_refuses(monkeypatch, tool, _planted()[:3])


def test_s16d_duplicate_kind_refuses(monkeypatch, tool):
    p = _planted()
    p[3]["kind"] = "P3"
    _bad_planted_refuses(monkeypatch, tool, p)


def test_s16e_five_planted_refuse(monkeypatch, tool):
    p = _planted() + [dict(planted_id="zp_9005", quote="q", interpretation="i", clauses=["c"], kind="P1")]
    _bad_planted_refuses(monkeypatch, tool, p)


def test_s16f_empty_quote_or_clauses_refuse(monkeypatch, tool):
    p = _planted()
    p[0]["quote"] = "  "
    _bad_planted_refuses(monkeypatch, tool, p)
    p = _planted()
    p[1]["clauses"] = []
    _bad_planted_refuses(monkeypatch, tool, p)


def test_s16g_invalid_planted_refuses_even_for_draft(monkeypatch, tool):
    db, out = _built(monkeypatch, tool)
    assert _render(tool, db, out, _splits(out), _planted()[:3], draft=True) == 2
    assert not list(out.glob("sheet-*"))


# S17: the real stored timestamp format
def test_s17_real_timestamp_format_at_window_edges(monkeypatch, tool):
    rows = [
        dict(id="zq_3401", sim=0.1, created="2026-10-10T15:00:00+00:00"),         # start: in
        dict(id="zq_3402", sim=0.1, created="2026-10-24T15:00:00+00:00"),         # end: out
        dict(id="zq_3403", sim=0.1, created="2026-10-24T14:59:59.999999+00:00"),  # a microsecond before end: in
        dict(id="zq_3404", sim=0.1, created="2026-10-10T14:59:59.999999+00:00"),  # a microsecond before start: out
        dict(id="zq_3405", sim=0.1, created="2026-10-24T14:59:59.123456+00:00"),  # the store's own shape: in
        dict(id="zq_3406", sim=0.1, created="2026-10-24T15:00:00.000001+00:00"),  # a microsecond after end: out
        dict(id="zq_3407", sim=0.1, created="2026-10-24T15:59:59+01:00"),         # 14:59:59Z: in
        dict(id="zq_3408", sim=0.1, created="2026-10-24T16:00:00+01:00"),         # 15:00:00Z: out
    ]
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    assert {c["node_id"] for c in _claims(out)} == {"zq_3401", "zq_3403", "zq_3405", "zq_3407"}
    assert _key(out)["unparsable_created_at_count"] == 0


def test_s17b_naive_timestamps_are_utc(monkeypatch, tool):
    rows = [
        dict(id="zq_3501", sim=0.1, created="2026-10-24T14:59:59.123456"),  # naive, in
        dict(id="zq_3502", sim=0.1, created="2026-10-24T15:00:00"),         # naive, at end: out
        dict(id="zq_3503", sim=0.1, created="2026-10-10T15:00:00"),         # naive, at start: in
        dict(id="zq_3504", sim=0.1, created="2026-10-10T14:59:59.999999"),  # naive, before start: out
    ]
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    assert {c["node_id"] for c in _claims(out)} == {"zq_3501", "zq_3503"}
    assert _key(out)["unparsable_created_at_count"] == 0


# S18: the signed seed is pinned
def test_s18_seed_pinned(monkeypatch, tool, capsys):
    assert tool.SEED_HEX == "d638538e"
    db = _setup(monkeypatch, tool, _rows("zq", 14, 0.1))
    out = _out()
    capsys.readouterr()
    rc = tool.main(["select", "--db", str(db), "--out-dir", str(out),
                    "--seed-hex", "d638538f", "--build-commit", "abc1234"])
    assert rc == 2
    assert _stderr(capsys).startswith("refused:")
    assert not out.exists()


def test_s18b_seed_defaults_to_signed_value(monkeypatch, tool):
    rows = _rows("zq", 14, 0.1)
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert tool.main(["select", "--db", str(db), "--out-dir", str(out), "--build-commit", "abc1234"]) == 0
    assert _key(out)["seed_hex"] == "d638538e"
    assert [c["node_id"] for c in _claims(out)] == sorted([r["id"] for r in rows], key=_h)[:10]


# --------------------------------------------------------------------------- S19 (draft 5, change-log entry 14)
# Presentation order: a separately salted key, so planted items do not sit at the end.
def _order_fixture(monkeypatch, tool):
    """40 real (10 per bin) + 4 planted whose ids all sort AFTER every real id under the selection key."""
    sims = (0.1, 0.3, 0.5, 0.7)
    rows = [dict(id=f"zz_{1 + i:04d}", sim=sims[i // 10]) for i in range(40)]
    real = [r["id"] for r in rows]
    top = max(_h(i) for i in real)
    planted = _planted()
    for p in planted:
        k = 0
        while True:  # deterministic search: first suffix whose selection hash beats every real hash
            cand = f"planted-{p['kind']}-{k}"
            if _h(cand) > top:
                break
            k += 1
        p["planted_id"] = cand
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    return db, out, real, planted


def test_s19a_planted_not_all_last_and_numbering_is_salted_sort(monkeypatch, tool):
    db, out, real, planted = _order_fixture(monkeypatch, tool)
    assert sorted(c["node_id"] for c in _claims(out)) == sorted(real)
    pids = [p["planted_id"] for p in planted]
    allids = real + pids
    # precondition: under the OLD (selection) rule all four planted items would be the last four
    old = sorted(allids, key=_h)
    assert set(old[-4:]) == set(pids)
    assert _render(tool, db, out, _splits(out), planted) == 0
    items = _key(out)["render"]["items"]
    order = [items[str(n)]["id"] for n in range(1, 45)]
    assert order == sorted(allids, key=_oh)
    assert set(order[-4:]) != set(pids)
    assert order != old


def test_s19b_salted_key_differs_from_selection_key(monkeypatch, tool):
    db, out, real, planted = _order_fixture(monkeypatch, tool)
    for i in real + [p["planted_id"] for p in planted]:
        assert _oh(i) != _h(i)


def test_s19c_select_output_unchanged(monkeypatch, tool):
    sims = (0.1, 0.3, 0.5, 0.7)
    rows = [dict(id=f"zz_{100 + i:04d}", sim=sims[b]) for b in range(4) for i in range(b * 20, b * 20 + 14)]
    db = _setup(monkeypatch, tool, rows)
    out = _out()
    assert _select(tool, db, out) == 0
    golden = []  # computed here from the selection rule alone: 10 smallest sha256(seed_hex || id) per bin
    for b in range(4):
        ids = [r["id"] for r in rows if r["sim"] == sims[b]]
        golden += [(i, f"B{b + 1}", _h(i)) for i in sorted(ids, key=_h)[:10]]
    assert [c["node_id"] for c in _claims(out)] == [g[0] for g in golden]
    assert [(i["node_id"], i["bin"], i["sort_hash"]) for i in _key(out)["items"]] == golden


def test_s19d_key_records_presentation_rule(monkeypatch, tool):
    db, out, real, planted = _order_fixture(monkeypatch, tool)
    assert _render(tool, db, out, _splits(out), planted) == 0
    render = _key(out)["render"]
    assert render["order_key"] == "sha256(seed_hex || 'order' || id)"
    assert render["order_salt"] == "order"
    assert len(render["items"]) == 44
    for n, entry in render["items"].items():
        assert entry["order_hash"] == _oh(entry["id"])
    hashes = [render["items"][str(n)]["order_hash"] for n in range(1, 45)]
    assert hashes == sorted(hashes)
    assert tool.ORDER_SALT == "order" and tool.SEED_HEX == SEED


# --------------------------------------------------------------------------- S20: notes instruction
NOTES_SENTENCE = ("When you have labelled every clause, you may add a final line `## Notes` at the very end "
                  "and write free notes below it. Use it to note any node that a hook injected into your "
                  "context and that bore on a label. Nothing below that line is read as a label or flag. "
                  "A line like that anywhere before the last clause stops the return from being scored, "
                  "so write notes only at the very end.")


def test_s20a_headers_carry_notes_instruction(tool):
    for header in (tool._HEADER1, tool._HEADER2):
        assert NOTES_SENTENCE in header
        assert "`## Notes`" in header and "injected" in header


def test_s20b_draft_and_final_renders_carry_it_without_a_notes_line(monkeypatch, tool):
    import re
    # The scorer's literal Notes-heading pattern (PR #4152 head 27d99850), not imported.
    scorer_notes = re.compile(r"^#{0,6}\s*notes\s*:?$", re.IGNORECASE)
    for planted_line in ("## Notes", "Notes:", "# notes"):
        assert scorer_notes.match(planted_line.strip()), planted_line
    db, out = _built(monkeypatch, tool)
    sp = _splits(out)
    assert _render(tool, db, out, sp, draft=True) == 0
    assert _render(tool, db, out, sp, _planted()) == 0
    names = ["sheet-pass1.DRAFT.md", "sheet-pass2.DRAFT.md", "sheet-pass1.md", "sheet-pass2.md"]
    for name in names:
        text = (out / name).read_text()
        assert NOTES_SENTENCE in text, name
        assert "injected" in text and "`## Notes`" in text, name
        assert not re.search(r"^## Notes", text, re.IGNORECASE | re.MULTILINE), name
        hits = [ln for ln in text.splitlines() if scorer_notes.match(ln.strip())]
        assert hits == [], (name, hits)
