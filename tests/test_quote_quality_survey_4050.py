"""Cells for tools/quote_quality_survey.py (engram-alpha#4050 S1) -- the read-only
quote-quality survey, the blind judging export, and the pre-registered scorer.

Every cell builds its own graph on a copy of a REAL schema (created once through
the real server, never a hand-rolled CREATE TABLE) and inserts rows with raw SQL.
Similarity is stubbed deterministically by patching
``engram_anchoring.quote_claim_similarity`` (keyed by quote text, so no
similarity value ever has to be planted inside text a judge will read).
"""

import ast
import atexit
import importlib.util
import itertools
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TOOL_PATH = (Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"
              / "quote_quality_survey.py")
_ANCHORING_TOOL = _TOOL_PATH.with_name("anchoring_report.py")

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_qqs_4050_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TEMPLATE: dict = {}
_COUNTER = itertools.count()

SIM_BINS = ["[0,0.2)", "[0.2,0.4)", "[0.4,0.6)", "[0.6,0.8)", "[0.8,1]"]
LEN_BINS = ["<40", "40-79", "80-159", "160-319", ">=320"]


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


def _make_db(rows: list[dict]) -> Path:
    """rows: dicts with id, quote_type, quote, interp, claim, created, source_class,
    type (default observation_factual), current (default 1)."""
    path = Path(TEST_DIR) / f"db_{next(_COUNTER)}.db"
    shutil.copy(_template_db(), path)
    conn = sqlite3.connect(path)
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, quoted_text, interpretation,"
            " quote_type, is_current, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                r["id"], r.get("type", "observation_factual"), r["claim"],
                r.get("created", "2026-09-15T00:00:00"), r["quote"], r.get("interp", "an interpretation"),
                r["quote_type"], r.get("current", 1),
                json.dumps({"source_class": r.get("source_class", "external")}),
            ),
        )
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return path


def _load_tool():
    name = f"quote_quality_survey_{next(_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, _TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub(sims: dict):
    def fake(quoted_text, other):
        if quoted_text == other:
            return 1.0, None
        return sims.get(quoted_text, 0.1), None
    return fake


def _run_main(tool, argv, sims=None, capsys=None):
    fake = _stub(sims or {})
    tool.engram_anchoring.quote_claim_similarity = fake
    rc = tool.main(argv)
    out = capsys.readouterr().out if capsys else ""
    return rc, out


def _planted_graph(introspective_every=None):
    """60 nodes: 12 per similarity bin, plus 10 terse hard_data nodes. Returns (rows, sims)."""
    rows, sims = [], {}
    n = 0
    for b in range(5):
        for i in range(12):
            n += 1
            quote = f"quote number {n} about subject {n}"
            sc = "introspective" if introspective_every and n % introspective_every == 0 else "external"
            rows.append(dict(id=f"zq_{1000 + n}", quote_type="attributed_analysis", quote=quote,
                             claim=f"claim number {n}", interp=f"interp number {n}",
                             source_class=sc, created=f"2026-0{1 + b}-10T00:00:00"))
            sims[quote] = 0.02 + 0.2 * b + 0.012 * i
    for i in range(10):
        n += 1
        quote = f"exit code {i} run {n} passed"
        rows.append(dict(id=f"zq_{1000 + n}", quote_type="hard_data", quote=quote,
                         claim=f"claim number {n}", interp=f"interp number {n}"))
        sims[quote] = 0.5
    return rows, sims


def _sample(tool, db, tmp, sims, capsys, extra=(), seed="7"):
    sheet, key = tmp / "sheet.md", tmp / "key.json"
    rc, out = _run_main(tool, ["--db", str(db), "--format", "json", "--judging-sheet", str(sheet),
                                "--judging-key", str(key), "--seed", seed, *extra], sims, capsys)
    assert rc == 0
    return sheet, key, out


def _tmp():
    d = Path(TEST_DIR) / f"t_{next(_COUNTER)}"
    d.mkdir()
    return d


# --------------------------------------------------------------------------- C1
def test_c1_read_only_bytes_mtime_and_ro_open():
    db = _make_db([dict(id="zq_1", quote_type="hard_data", quote="rc=0", claim="c")])
    before_bytes, before_mtime = db.read_bytes(), db.stat().st_mtime_ns
    env = dict(os.environ, ENGRAM_NO_EMBEDDINGS="1")
    proc = subprocess.run([sys.executable, str(_TOOL_PATH), "--db", str(db), "--format", "json"],
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert db.read_bytes() == before_bytes
    assert db.stat().st_mtime_ns == before_mtime
    tool = _load_tool()
    conn = tool._open_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO nodes (id, type, created_at) VALUES ('x_0', 'observation_factual', 'n')")
            conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- C2
def test_c2_single_home_no_own_similarity_code():
    tree = ast.parse(_TOOL_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert "cosine" not in node.name.lower(), node.name
            assert node.name != "quote_claim_similarity"
            assert "embed" not in node.name.lower(), node.name
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "engram_anchoring" for a in node.names):
            imported = True
        if isinstance(node, ast.ImportFrom) and node.module == "engram_anchoring":
            imported = True
    assert imported


# --------------------------------------------------------------------------- C3
def _three_nodes():
    a_quote = "the merge landed and every check passed on the first attempt of the day, all of them ok"
    rows = [
        dict(id="zq_1", quote_type="official_statement", quote=a_quote, claim=a_quote,
             created="2026-08-02T00:00:00"),
        dict(id="zq_2", quote_type="attributed_analysis", quote="an unrelated remark about the weather today",
             claim="tax filing deadline moved", created="2026-08-20T00:00:00"),
        dict(id="zq_3", quote_type="hard_data", quote="rc=0 ok.", claim="tests ran",
             created="2026-09-01T00:00:00"),
    ]
    assert len(a_quote) in range(80, 160)
    assert len(rows[1]["quote"]) in range(40, 80)
    assert len(rows[2]["quote"]) == 8
    return rows


def test_c3_planted_nodes_bins_lengths_terse(capsys):
    tool = _load_tool()
    db = _make_db(_three_nodes())
    rc, out = _run_main(tool, ["--db", str(db), "--format", "json"], {}, capsys)
    assert rc == 0
    payload = json.loads(out)
    allr = payload["strata"]["all"]
    assert allr["n"] == 3
    assert allr["sim_quote_claim_bins"] == dict(zip(SIM_BINS, [2, 0, 0, 0, 1]))
    assert allr["length_bins"] == dict(zip(LEN_BINS, [1, 1, 1, 0, 0]))
    assert payload["strata"]["terse_measurement=true"]["n"] == 1
    assert payload["strata"]["terse_measurement=false"]["n"] == 2
    assert payload["meta"]["null_similarity"]["sim_quote_claim"]["count"] == 0
    assert payload["meta"]["population"] == 3
    assert payload["by_created_month"]["2026-08"]["n"] == 2
    assert payload["by_created_month"]["2026-08"]["median_sim_quote_claim"] == pytest.approx(0.55)


def test_c3_null_similarity_is_counted_never_zero(capsys):
    tool = _load_tool()
    db = _make_db(_three_nodes())
    tool.engram_anchoring.quote_claim_similarity = lambda q, o: (None, "embedder unavailable")
    assert tool.main(["--db", str(db), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    nulls = payload["meta"]["null_similarity"]
    assert nulls["sim_quote_claim"]["count"] == 3
    assert nulls["sim_quote_interp"]["count"] == 3
    assert nulls["sim_quote_claim"]["reasons"] == {"embedder unavailable": 3}
    allr = payload["strata"]["all"]
    assert sum(allr["sim_quote_claim_bins"].values()) == 0
    assert allr["deciles"]["sim_quote_claim"] is None
    assert payload["by_created_month"]["2026-08"]["median_sim_quote_claim"] is None


# --------------------------------------------------------------------------- C4
_TERSE_CASES = [
    ("hard_data", "all 12 checks passed in the nightly run", True),
    ("hard_data", "x" * 120 + " passed", True),
    ("hard_data", "x" * 199 + "1", False),
    ("attributed_analysis", "rc=0", False),
    ("hard_data", "a short remark with nothing countable", False),
    ("hard_data", "z" * 159 + "7", True),     # exactly 160 chars
    ("hard_data", "z" * 160 + "7", False),    # 161 chars
    ("hard_data", "the job failed", True),
    ("hard_data", "exit seen", True),
    ("hard_data", "up by percent %", True),
]


@pytest.mark.parametrize("qt,quote,expected", _TERSE_CASES)
def test_c4_terse_rule(qt, quote, expected):
    tool = _load_tool()
    assert tool.is_terse_measurement(qt, quote) is expected


def test_c4_rule_is_one_module_constant():
    tool = _load_tool()
    assert tool.TERSE_MEASUREMENT_RULE.max_chars == 160
    assert tool.TERSE_MEASUREMENT_RULE.quote_type == "hard_data"
    assert "pre-registered" in _TOOL_PATH.read_text().lower()


# engram-alpha#4085: _source_class tells absent / malformed / empty-object apart.
_SOURCE_CLASS_CASES = [
    (None, "external"),
    ("", "external"),
    ("{}", "external"),
    ("{ }", "external"),
    ("not json", "unknown"),
    ("[1,2]", "unknown"),
    ("5", "unknown"),
    ('{"source_class": "user_stated"}', "user_stated"),
    ('{"source_class": ""}', "external"),
    ('{"other": 1}', "external"),
    # JSON whitespace is only space, tab, LF, CR. Unicode whitespace makes
    # json.loads fail, so these stay malformed ("unknown").
    ("\xa0{}", "unknown"),
    ("{}\xa0", "unknown"),
    ("{} ", "unknown"),
    ("{} ", "external"),
    ("{\n}", "external"),
    ("\t{ }\r\n", "external"),
]


@pytest.mark.parametrize("raw,expected", _SOURCE_CLASS_CASES)
def test_source_class_three_way_4085(raw, expected):
    tool = _load_tool()
    assert tool._source_class(raw) == expected


# --------------------------------------------------------------------------- C5
def test_c5_blind_sheet(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    sheet, key, _ = _sample(tool, db, tmp, sims, capsys)
    text = sheet.read_text()
    keyd = json.loads(key.read_text())
    items = keyd["items"]
    assert len(items) == 30  # 5 per bin x 5 bins + 5 terse
    assert sorted(int(k) for k in items) == list(range(1, 31))
    for it in items.values():
        assert it["node_id"] not in text
    assert not re.search(r"zq_\d", text)
    assert not re.search(r"\d\.\d", text), "no similarity-like value in the sheet"
    for label in SIM_BINS + ["terse", "hard_data", "attributed_analysis", "official_statement",
                              "quote_type", "sim_"]:
        assert label not in text, label
    for k, it in items.items():
        assert {"node_id", "bin", "sim_quote_claim", "sim_quote_interp", "quote_type", "terse"} <= set(it)
    # same seed -> same sheet; different seed -> different order
    sheet2, key2, _ = _sample(tool, db, _tmp(), sims, capsys)
    assert sheet2.read_text() == text
    sheet3, key3, _ = _sample(tool, db, _tmp(), sims, capsys, seed="8")
    order = lambda kp: [json.loads(kp.read_text())["items"][str(i)]["node_id"] for i in range(1, 31)]
    assert order(key3) != order(key)
    # without replacement across the whole draw
    ids = [it["node_id"] for it in items.values()]
    assert len(set(ids)) == len(ids)


def test_c5_small_bin_is_reported(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    # shrink the top bin to 2 nodes
    top = [r for r in rows if sims[r["quote"]] >= 0.8][2:]
    rows = [r for r in rows if r not in top]
    db = _make_db(rows)
    sheet, key, _ = _sample(tool, db, _tmp(), sims, capsys)
    keyd = json.loads(key.read_text())
    assert keyd["meta"]["shortfalls"]["[0.8,1]"] == {"requested": 5, "drawn": 2}


# --------------------------------------------------------------------------- C6
def _filled(tool, items, answers_a, answers_b=None):
    """Render a blind sheet for `items`, fill every (a) with answers_a[item_number]."""
    body = tool.render_sheet(items)
    for num, ans in answers_a.items():
        pat = re.compile(rf"(## Item {num}\n.*?\(a\)[^\n]*?Answer:)[^\n]*", re.S)
        body = pat.sub(lambda m: m.group(1) + f"  {ans}", body, count=1)
    return body


def _synthetic(tool, no_shares, terse_yes, top_yes=None, answered=None, per_bin=5):
    """`per_bin` items per bin; no_shares[b] = how many are 'no'. answered={label: k} leaves all
    but the first k items of that bin blank. Returns (key, sheet_text)."""
    answered = answered or {}
    items, key_items, answers, num = [], {}, {}, 0
    for b, label in enumerate(SIM_BINS + ["terse"]):
        for i in range(per_bin):
            num += 1
            items.append(dict(number=num, quote=f"q{num}", interp="i", claim="c"))
            key_items[str(num)] = dict(node_id=f"zq_{num}", bin=label if label != "terse" else None,
                                       stratum=label, sim_quote_claim=0.1, sim_quote_interp=0.1,
                                       quote_type="x", terse=label == "terse")
            if i >= answered.get(label, per_bin):
                continue
            if label == "terse":
                answers[num] = "YES" if i < terse_yes else "no"
            else:
                n_no = no_shares[b]
                if i < n_no:
                    answers[num] = "no"
                elif top_yes is not None and b == 4 and i < n_no + top_yes:
                    answers[num] = " Yes "
                else:
                    answers[num] = "partly"
    return {"meta": {"swaps": 0, "seed": 1}, "items": key_items}, _filled(tool, items, answers)


def _score(tool, key, text, tmp, capsys):
    kp, sp = tmp / "k.json", tmp / "f.md"
    kp.write_text(json.dumps(key))
    sp.write_text(text)
    assert tool.main(["--score-sheet", str(sp), "--judging-key", str(kp), "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_c6_separates_monotone_true(capsys):
    tool = _load_tool()
    key, text = _synthetic(tool, [4, 3, 2, 1, 0], terse_yes=5)
    res = _score(tool, key, text, _tmp(), capsys)
    v = res["verdicts"]["SEPARATES"]
    assert v["value"] is True
    assert v["no_share_lowest"] == pytest.approx(0.8) and v["no_share_highest"] == pytest.approx(0.0)
    assert res["per_bin"]["[0,0.2)"]["no"] == pytest.approx(0.8)


def test_c6_separates_false_with_middle_bump(capsys):
    tool = _load_tool()
    key, text = _synthetic(tool, [4, 1, 3, 1, 0], terse_yes=5)
    v = _score(tool, key, text, _tmp(), capsys)["verdicts"]["SEPARATES"]
    assert v["value"] is False
    assert v["monotonic"] is False
    assert v["no_share_lowest"] - v["no_share_highest"] >= 0.4


def test_c6_separates_false_when_gap_small(capsys):
    tool = _load_tool()
    key, text = _synthetic(tool, [2, 2, 1, 1, 1], terse_yes=5)
    assert _score(tool, key, text, _tmp(), capsys)["verdicts"]["SEPARATES"]["value"] is False


def test_c6_exempt(capsys):
    tool = _load_tool()
    key, text = _synthetic(tool, [4, 3, 2, 1, 0], terse_yes=5, top_yes=3)
    res = _score(tool, key, text, _tmp(), capsys)
    ex = res["verdicts"]["EXEMPT"]
    assert ex["value"] is True
    assert ex["terse_yes"] == pytest.approx(1.0) and ex["top_bin_yes"] == pytest.approx(0.6)
    key, text = _synthetic(tool, [4, 3, 2, 1, 0], terse_yes=2, top_yes=3)
    assert _score(tool, key, text, _tmp(), capsys)["verdicts"]["EXEMPT"]["value"] is False


# --------------------------------------------------------------------------- C7
def test_c7_population_parity_with_anchoring_report():
    rows, _ = _planted_graph()
    rows.append(dict(id="zq_9001", quote_type="hard_data", quote="old", claim="c", current=0))
    rows.append(dict(id="zq_9002", quote_type="hard_data", quote="pred", claim="c", type="observation_predictive"))
    rows.append(dict(id="zq_9003", quote_type="hard_data", quote="deriv", claim="c", type="derivation"))
    db = _make_db(rows)
    env = dict(os.environ, ENGRAM_NO_EMBEDDINGS="1")
    a = subprocess.run([sys.executable, str(_ANCHORING_TOOL), "--db", str(db), "--format", "json"],
                       capture_output=True, text=True, timeout=60, env=env)
    b = subprocess.run([sys.executable, str(_TOOL_PATH), "--db", str(db), "--format", "json"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert a.returncode == 0 and b.returncode == 0, (a.stderr, b.stderr)
    pa, pb = json.loads(a.stdout)["population"], json.loads(b.stdout)
    assert pb["meta"]["population"] == pa == 71


# --------------------------------------------------------------------------- C8
def test_c8_excluded_class_never_sampled_but_still_counted(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph(introspective_every=2)
    db = _make_db(rows)
    excluded_ids = {r["id"] for r in rows if r.get("source_class") == "introspective"}
    assert len(excluded_ids) == 30
    sheet, key, out = _sample(tool, db, _tmp(), sims, capsys, extra=["--exclude-source-class", "introspective"])
    payload = json.loads(out)
    keyd = json.loads(key.read_text())
    sampled = {it["node_id"] for it in keyd["items"].values()}
    assert sampled and not (sampled & excluded_ids)
    assert payload["strata"]["all"]["n"] == 70
    cell = payload["strata"]["quote_type=attributed_analysis|source_class=introspective"]
    assert cell["n"] == 30
    assert payload["meta"]["excluded_source_classes"] == ["introspective"]
    assert payload["meta"]["excluded_from_sample_pool"] == 30
    # without the flag, the same graph does sample them (the control: the exclusion is what removes them)
    sheet2, key2, _ = _sample(tool, db, _tmp(), sims, capsys)
    sampled2 = {it["node_id"] for it in json.loads(key2.read_text())["items"].values()}
    assert sampled2 & excluded_ids


# --------------------------------------------------------------------------- C9
def test_c9_swap_keeps_bin_changes_node_bumps_count_deterministic(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    sheet, key, _ = _sample(tool, db, tmp, sims, capsys)
    before = json.loads(key.read_text())
    sheet_before = sheet.read_text()

    def do_swap(workdir, items):
        s, k = workdir / "sheet.md", workdir / "key.json"
        shutil.copy(sheet, s)
        shutil.copy(key, k)
        argv = ["--db", str(db), "--judging-sheet", str(s), "--judging-key", str(k), "--seed", "7"]
        for it in items:
            argv += ["--swap", str(it)]
        rc, _out = _run_main(tool, argv, sims, capsys)
        assert rc == 0
        return s.read_text(), json.loads(k.read_text())

    text1, key1 = do_swap(_tmp(), [3])
    text2, key2 = do_swap(_tmp(), [3])
    assert text1 == text2 and key1 == key2          # deterministic
    assert key1["meta"]["swaps"] == 1 and before["meta"]["swaps"] == 0
    old, new = before["items"]["3"], key1["items"]["3"]
    assert new["node_id"] != old["node_id"]
    assert new["bin"] == old["bin"] and new["stratum"] == old["stratum"]
    for k in before["items"]:
        if k != "3":
            assert key1["items"][k] == before["items"][k]
    assert text1 != sheet_before
    assert old["node_id"] not in text1 and new["node_id"] not in text1
    # never reuses a node already drawn, including swapped-out ones
    text3, key3 = do_swap(_tmp(), [3, 3, 3])
    assert key3["meta"]["swaps"] == 3
    drawn = [it["node_id"] for it in before["items"].values()]
    assert key3["items"]["3"]["node_id"] not in drawn
    all_now = [it["node_id"] for it in key3["items"].values()]
    assert len(set(all_now)) == len(all_now)


def test_c9_swap_terse_item_stays_terse(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    sheet, key, _ = _sample(tool, db, tmp, sims, capsys)
    before = json.loads(key.read_text())
    num = next(k for k, v in before["items"].items() if v["stratum"] == "terse")
    rc, _ = _run_main(tool, ["--db", str(db), "--judging-sheet", str(sheet), "--judging-key", str(key),
                              "--seed", "7", "--swap", num], sims, capsys)
    assert rc == 0
    after = json.loads(key.read_text())
    assert after["items"][num]["stratum"] == "terse" and after["items"][num]["terse"] is True
    assert after["items"][num]["node_id"] != before["items"][num]["node_id"]


# --------------------------------------------------------------------------- C10
def test_c10_sheet_masks_id_shaped_tokens_text_only(capsys):
    tool = _load_tool()
    quote = "the run log says ob_1234 and dv_0042 were closed"
    row = dict(id="zq_7001", quote_type="attributed_analysis", quote=quote,
               claim="closed dv_0042 and ob_1234 today", interp="see ob_1234 for context")
    db = _make_db([row])
    # positive control: the planted strings really are in the DB text the tool reads
    conn = sqlite3.connect(db)
    stored = " ".join(" ".join(r) for r in conn.execute("SELECT quoted_text, claim, interpretation FROM nodes"))
    conn.close()
    assert "ob_1234" in stored and "dv_0042" in stored
    sheet, key, out = _sample(tool, db, _tmp(), {quote: 0.1}, capsys)
    text = sheet.read_text()
    assert "ob_1234" not in text and "dv_0042" not in text
    assert "[id]" in text
    assert "ids are masked" in text.splitlines()[0].lower()
    keyd = json.loads(key.read_text())
    assert keyd["meta"]["masked_id_tokens"] == 5
    assert keyd["items"]["1"]["node_id"] == "zq_7001"
    # distributions are computed from the unmasked text: the claim's two tokens anchor in the quote
    with_sheet = json.loads(out)
    rc, out2 = _run_main(tool, ["--db", str(db), "--format", "json"], {quote: 0.1}, capsys)
    plain = json.loads(out2)
    assert with_sheet["strata"] == plain["strata"]
    assert plain["strata"]["all"]["deciles"]["anchoring_ratio"] == [1.0] * 9


# --------------------------------------------------------------------------- C6 round 2
def _sep(tool, capsys, no_shares, **kw):
    key, text = _synthetic(tool, no_shares, terse_yes=kw.pop("terse_yes", 5), **kw)
    return _score(tool, key, text, _tmp(), capsys)["verdicts"]


def test_c6_separates_exact_boundary_040_true(capsys):
    tool = _load_tool()
    # 40/100 'no' in the lowest bin, 0 in the highest: gap exactly 0.40
    v = _sep(tool, capsys, [40, 30, 20, 10, 0], per_bin=100)["SEPARATES"]
    assert v["value"] is True


def test_c6_separates_exact_boundary_040_true_with_float_noise(capsys):
    tool = _load_tool()
    # 0.6 - 0.2 is 0.39999999999999997 in floating point; the rule is still met
    v = _sep(tool, capsys, [3, 3, 2, 2, 1])["SEPARATES"]
    assert v["value"] is True


def test_c6_separates_just_below_boundary_039_false(capsys):
    tool = _load_tool()
    v = _sep(tool, capsys, [39, 30, 20, 10, 0], per_bin=100)["SEPARATES"]
    assert v["value"] is False


@pytest.mark.parametrize("answered", [
    {"[0,0.2)": 0},      # empty lowest bin
    {"[0.8,1]": 0},      # empty top bin
    {"[0,0.2)": 2},      # partial: 2 answered in the lowest bin
    {"[0.8,1]": 2},
])
def test_c6_empty_or_thin_endpoint_is_undetermined(capsys, answered):
    tool = _load_tool()
    v = _sep(tool, capsys, [4, 3, 2, 1, 0], answered=answered)
    sep = v["SEPARATES"]
    assert sep["value"] == "UNDETERMINED"
    assert sep["reason"] and "fewer than" in sep["reason"]
    assert sep["answered_per_bin"]["[0,0.2)"] == answered.get("[0,0.2)", 5)
    assert sep["answered_per_bin"]["[0.8,1]"] == answered.get("[0.8,1]", 5)
    assert tool.MIN_ANSWERED == 3


def test_c6_full_sheet_still_true_false_not_undetermined(capsys):
    tool = _load_tool()
    assert _sep(tool, capsys, [4, 3, 2, 1, 0])["SEPARATES"]["value"] is True
    assert _sep(tool, capsys, [4, 1, 3, 1, 0])["SEPARATES"]["value"] is False


def test_c6_thin_middle_bin_is_reported_but_does_not_block(capsys):
    tool = _load_tool()
    sep = _sep(tool, capsys, [4, 3, 0, 0, 0], answered={"[0.4,0.6)": 1})["SEPARATES"]
    assert sep["value"] is True
    assert sep["thin_middle_bins"] == ["[0.4,0.6)"]
    assert "middle" in sep["note"].lower()


@pytest.mark.parametrize("kwargs", [
    dict(terse_yes=5, answered={"terse": 2}),
    dict(terse_yes=5, answered={"terse": 0}),
    dict(terse_yes=5, answered={"[0.8,1]": 0}),
    dict(terse_yes=5, answered={"[0.8,1]": 2}),
])
def test_c6_exempt_undetermined_when_terse_or_top_bin_thin(capsys, kwargs):
    tool = _load_tool()
    ex = _sep(tool, capsys, [4, 3, 2, 1, 0], top_yes=3, **kwargs)["EXEMPT"]
    assert ex["value"] == "UNDETERMINED"
    assert "fewer than" in ex["reason"]


def test_c6_exempt_top_bin_is_the_registered_one_not_the_highest_answered(capsys):
    tool = _load_tool()
    ex = _sep(tool, capsys, [4, 3, 2, 1, 0], top_yes=3, answered={"[0.8,1]": 0})["EXEMPT"]
    assert ex["value"] == "UNDETERMINED" and ex.get("top_bin_yes") is None


def test_c6_exempt_exact_equality_is_true(capsys):
    tool = _load_tool()
    # terse yes 3/5 == top bin yes 3/5
    ex = _sep(tool, capsys, [4, 3, 2, 1, 0], top_yes=3, terse_yes=3)["EXEMPT"]
    assert ex["value"] is True


# --------------------------------------------------------------------------- C11 / R4
def test_c11_mask_covers_five_plus_digit_ids(capsys):
    tool = _load_tool()
    quote = "ids ob_10000 and xx_123456 and ob_0001 appear"
    db = _make_db([dict(id="zq_7002", quote_type="attributed_analysis", quote=quote,
                        claim="a claim", interp="an interpretation")])
    conn = sqlite3.connect(db)
    stored = conn.execute("SELECT quoted_text FROM nodes").fetchone()[0]
    conn.close()
    assert "ob_10000" in stored and "xx_123456" in stored
    sheet, key, _ = _sample(tool, db, _tmp(), {quote: 0.1}, capsys)
    text = sheet.read_text()
    assert "ob_10000" not in text and "xx_123456" not in text and "ob_0001" not in text
    assert json.loads(key.read_text())["meta"]["masked_id_tokens"] == 3


def test_r4_same_path_for_sheet_and_key_is_refused(capsys):
    tool = _load_tool()
    db = _make_db([dict(id="zq_7003", quote_type="hard_data", quote="rc=0", claim="c")])
    same = _tmp() / "both.out"
    with pytest.raises(SystemExit) as exc:
        tool.main(["--db", str(db), "--judging-sheet", str(same), "--judging-key", str(same)])
    assert exc.value.code != 0
    assert not same.exists()


def test_r4_c1_read_only_with_embeddings_available_stubbed(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    before_bytes, before_mtime = db.read_bytes(), db.stat().st_mtime_ns
    rc, out = _run_main(tool, ["--db", str(db), "--format", "json", "--judging-sheet", str(_tmp() / "s.md"),
                               "--judging-key", str(_tmp() / "k.json")], sims, capsys)
    assert rc == 0
    assert json.loads(out)["meta"]["null_similarity"]["sim_quote_claim"]["count"] == 0
    assert db.read_bytes() == before_bytes and db.stat().st_mtime_ns == before_mtime


# --------------------------------------------------------------------------- round 3
_QA = "(a) Does the quote by itself carry the claim? yes / partly / no -- Answer:"
_QB = "(b) Is it a verbatim measurement line? yes / no -- Answer:"


def _item_block(num, a_line, b_line, extra=""):
    return f"## Item {num}\n\n**Quote:**\n> q\n\n{a_line}\n\n{b_line}\n\n(c) Note: x\n{extra}\n"


def test_b1_note_line_starting_with_a_does_not_erase_the_answer():
    tool = _load_tool()
    # the reviewer's repro: a judge's own note line begins with "(a)"
    block = (f"## Item 1\n{_QA} partly\n{_QB} yes\n(c) Note: borderline.\n"
             "(a) would be yes under a looser reading\n(b) not a measurement at all\n")
    parsed = tool.parse_filled_sheet(block)
    assert parsed[1]["a"] == "partly" and parsed[1]["b"] == "yes"


def test_b1_first_template_line_wins_when_the_template_is_repeated():
    tool = _load_tool()
    block = _item_block(1, f"{_QA} no", f"{_QB} yes", extra=f"{_QA} yes\n")
    assert tool.parse_filled_sheet(block)[1]["a"] == "no"


def _two_item_key(items=(1, 2)):
    return {"meta": {"swaps": 0}, "items": {
        str(n): dict(node_id=f"zq_{n}", bin="[0,0.2)", stratum="[0,0.2)", sim_quote_claim=0.1,
                     sim_quote_interp=0.1, quote_type="x", terse=False) for n in items}}


def _score_text(tool, text, capsys, fmt="json", expect_rc=0, items=(1, 2)):
    tmp = _tmp()
    kp, sp = tmp / "k.json", tmp / "f.md"
    kp.write_text(json.dumps(_two_item_key(items)))
    sp.write_text(text)
    rc = tool.main(["--score-sheet", str(sp), "--judging-key", str(kp), "--format", fmt])
    assert rc == expect_rc
    return capsys.readouterr().out


def test_b1_garbage_answer_is_unparsed_loudly(capsys):
    tool = _load_tool()
    text = _item_block(1, f"{_QA} maybe", f"{_QB} yes") + _item_block(2, f"{_QA} no", f"{_QB} no")
    res = json.loads(_score_text(tool, text, capsys, expect_rc=3))
    assert res["n_unparsed"] == 1 and res["unparsed_items"] == [1]
    assert res["per_bin"]["[0,0.2)"]["n_answered"] == 1  # the garbage answer is not counted


def test_b1_garbage_in_b_is_unparsed_too(capsys):
    tool = _load_tool()
    text = _item_block(1, f"{_QA} yes", f"{_QB} partly") + _item_block(2, f"{_QA} no", f"{_QB} no")
    res = json.loads(_score_text(tool, text, capsys, expect_rc=3))
    assert res["unparsed_items"] == [1]


def test_b1_blank_answer_is_unanswered_not_unparsed(capsys):
    tool = _load_tool()
    text = _item_block(1, f"{_QA} ", f"{_QB} ") + _item_block(2, f"{_QA} no", f"{_QB} no")
    res = json.loads(_score_text(tool, text, capsys))
    assert res["n_unparsed"] == 0 and res["unparsed_items"] == []
    assert res["per_bin"]["[0,0.2)"]["n_answered"] == 1


def test_b1_item_without_a_template_line_is_unparsed(capsys):
    tool = _load_tool()
    text = (_item_block(1, f"{_QA} no", f"{_QB} no")
            + "## Item 2\n(a) my own line, the template line was deleted\n(b) same\n")
    res = json.loads(_score_text(tool, text, capsys, expect_rc=3))
    assert res["unparsed_items"] == [2]


def test_b1_key_item_missing_from_the_sheet_is_unparsed(capsys):
    tool = _load_tool()
    res = json.loads(_score_text(tool, _item_block(1, f"{_QA} no", f"{_QB} no"), capsys, expect_rc=3))
    assert res["unparsed_items"] == [2]


def test_b1_human_summary_prints_unparsed_items(capsys):
    tool = _load_tool()
    text = _item_block(1, f"{_QA} maybe", f"{_QB} yes") + _item_block(2, f"{_QA} no", f"{_QB} no")
    out = _score_text(tool, text, capsys, fmt="human", expect_rc=3)
    assert "n_unparsed=1" in out and "[1]" in out


def test_b1_template_constants_match_what_the_sheet_writer_emits():
    tool = _load_tool()
    sheet = tool.render_sheet([dict(number=1, quote="q", interp="i", claim="c")])
    assert _QA in sheet and _QB in sheet


# S1
def test_s1_sheet_is_readable_and_key_is_private_under_umask_022(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    old = os.umask(0o022)
    try:
        sheet, key, _ = _sample(tool, db, _tmp(), sims, capsys)
    finally:
        os.umask(old)
    assert sheet.stat().st_mode & 0o044 == 0o044, "judge's OS user must be able to read the sheet"
    assert sheet.stat().st_mode & 0o777 != 0o600
    assert key.stat().st_mode & 0o777 == 0o600


def test_s1_key_is_created_with_os_open_0600_no_chmod_window(capsys, monkeypatch):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    calls = []
    real_open = os.open

    def recorder(path, flags, mode=0o777, *a, **kw):
        calls.append((str(path), flags, mode))
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", recorder)
    tmp = _tmp()
    sheet, key, _ = _sample(tool, db, tmp, sims, capsys)
    # #4380: the strict open never passes O_TRUNC at open (the truncate is an ftruncate on the
    # checked fd), so a hard-linked key is refused before anything is truncated.
    wanted = os.O_WRONLY | os.O_CREAT
    key_calls = [c for c in calls if c[0] == str(key)]
    assert key_calls and key_calls[0][1] & wanted == wanted and key_calls[0][2] == 0o600
    assert key_calls[0][1] & os.O_TRUNC == 0 and key_calls[0][1] & os.O_NOFOLLOW
    assert not any(c[0] == str(sheet) and c[2] == 0o600 for c in calls)


# S2
def test_s2_privacy_notice_on_stderr_and_in_meta(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tool.engram_anchoring.quote_claim_similarity = _stub(sims)
    tmp = _tmp()
    assert tool.main(["--db", str(db), "--format", "json", "--judging-sheet", str(tmp / "s.md"),
                      "--judging-key", str(tmp / "k.json")]) == 0
    cap = capsys.readouterr()
    line = ("Read the sheet before sending it to another seat: --exclude-source-class does not "
            "screen personal strings (emails, addresses, names).")
    assert line in cap.err
    assert json.loads(cap.out)["meta"]["privacy_notice"] == line


# --------------------------------------------------------------------------- round 4
def test_d1_duplicate_item_header_is_unparsed_and_adds_nothing(capsys):
    tool = _load_tool()
    text = (_item_block(1, f"{_QA} no", f"{_QB} no") + _item_block(2, f"{_QA} no", f"{_QB} no")
            + _item_block(3, f"{_QA} yes", f"{_QB} yes") + _item_block(3, f"{_QA} no", f"{_QB} no"))
    res = json.loads(_score_text(tool, text, capsys, expect_rc=3, items=(1, 2, 3)))
    assert res["n_unparsed"] == 1 and res["unparsed_items"] == [3]
    assert "duplicate item header" in json.dumps(res["unparsed_fields"]["3"])
    b = res["per_bin"]["[0,0.2)"]
    assert b["n_answered"] == 2 and b["no"] == 1.0 and b["yes"] == 0.0


def test_d1_duplicate_with_blank_second_block_does_not_erase_first_silently(capsys):
    tool = _load_tool()
    text = (_item_block(1, f"{_QA} partly", f"{_QB} yes") + _item_block(1, f"{_QA} ", f"{_QB} ")
            + _item_block(2, f"{_QA} no", f"{_QB} no"))
    res = json.loads(_score_text(tool, text, capsys, expect_rc=3))
    assert res["unparsed_items"] == [1]


@pytest.mark.parametrize("answer", ["yes / no", "no idea", "yes?", "probably yes", "yes or no", "maybe"])
def test_d2_hedged_or_compound_answers_are_unparsed(capsys, answer):
    tool = _load_tool()
    text = _item_block(1, f"{_QA} {answer}", f"{_QB} yes") + _item_block(2, f"{_QA} no", f"{_QB} no")
    res = json.loads(_score_text(tool, text, capsys, expect_rc=3))
    assert res["unparsed_items"] == [1]
    assert res["unparsed_fields"]["1"]["a"] == "not one allowed answer"
    assert res["per_bin"]["[0,0.2)"]["n_answered"] == 1


@pytest.mark.parametrize("answer,expected", [
    ("**Partly**.", "partly"), ("  YES  ", "yes"), ("_no_", "no"), ("no.", "no"), ("*Yes*!", "yes"),
])
def test_d2_emphasis_case_and_trailing_punctuation_are_tolerated(answer, expected):
    tool = _load_tool()
    parsed = tool.parse_filled_sheet(_item_block(1, f"{_QA} {answer}", f"{_QB} yes"))
    assert parsed[1]["a"] == expected and parsed[1]["unparsed"] == []


def test_d2_b_rejects_partly_and_compound():
    tool = _load_tool()
    for ans in ("partly", "yes / no"):
        parsed = tool.parse_filled_sheet(_item_block(1, f"{_QA} no", f"{_QB} {ans}"))
        assert parsed[1]["b"] is None and parsed[1]["unparsed"] == ["b"]


_REPRO = (f"## Item 1\n{_QA} partly\n{_QB} yes\n(c) Note: borderline.\n"
          "(a) would be yes under a looser reading\n")


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_d3_crlf_and_cr_sheets_parse_like_lf(newline):
    tool = _load_tool()
    lf = tool.parse_filled_sheet(_REPRO)
    assert lf[1]["a"] == "partly" and lf[1]["b"] == "yes"
    assert tool.parse_filled_sheet(_REPRO.replace("\n", newline)) == lf


def test_d4_warning_precedes_verdicts_and_names_the_unread_field(capsys):
    tool = _load_tool()
    text = _item_block(1, f"{_QA} yes", f"{_QB} partly") + _item_block(2, f"{_QA} no", f"{_QB} no")
    out = _score_text(tool, text, capsys, fmt="human", expect_rc=3)
    assert out.index("WARNING:") < out.index("SEPARATES")
    assert out.index("WARNING:") < out.index("EXEMPT")
    assert "(b) not read; (a) counted" in out
    assert "SEPARATES" in out and "EXEMPT" in out, "verdicts are still printed"


def test_d4_exit_codes(capsys):
    tool = _load_tool()
    clean = _item_block(1, f"{_QA} no", f"{_QB} no") + _item_block(2, f"{_QA} yes", f"{_QB} yes")
    out = _score_text(tool, clean, capsys, fmt="human", expect_rc=0)
    assert "WARNING" not in out
    dirty = _item_block(1, f"{_QA} no", f"{_QB} no") + _item_block(2, f"{_QA} perhaps", f"{_QB} yes")
    out = _score_text(tool, dirty, capsys, fmt="json", expect_rc=3)
    assert json.loads(out)["verdicts"], "verdicts are still in the JSON"


# --------------------------------------------------------------------------- P: sheet parts
_PART_BYTES = 1500  # planted items are a few hundred bytes each: a few items per part
# sha256 of the 30-item sheet that _planted_graph + seed 7 produced before --sheet-part-bytes existed
_SHEET_SHA256_BEFORE_PARTS = "78426c8b7ed4c33e32a04e9e5ef06047bf8140af0cec4ec1a35348899c43e4be"


def _items_section(text):
    """Everything from the first item header on (the sheet's own header stripped)."""
    return text[text.index("## Item "):]


def _part_files(tmp, stem="sheet"):
    def k_of(p):
        return int(re.search(r"\.part(\d+)of\d+\.md$", p.name).group(1))
    return sorted(tmp.glob(f"{stem}.part*of*.md"), key=k_of)


def _item_numbers(text):
    return [int(n) for n in re.findall(r"^## Item (\d+)[ \t]*$", text, re.M)]


def _sample_parts(tool, capsys, b=_PART_BYTES):
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    sheet, key, out = _sample(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", str(b)))
    return tmp, sheet, key, _part_files(tmp), out


def _fill_text(text, answers):
    """Fill every (a) and (b) answer for the item numbers present in `text`."""
    def fill(m):
        a, b = answers[int(m.group(1))]
        blk = m.group(0).replace(_QA + " ", _QA + f" {a}", 1)
        return blk.replace(_QB + " ", _QB + f" {b}", 1)
    return re.sub(r"^## Item (\d+)\n.*?(?=^## Item |\Z)", fill, text, flags=re.S | re.M)


def _answers_for(numbers):
    cyc_a, cyc_b = ("yes", "partly", "no", "no", "partly"), ("yes", "no")
    return {n: (cyc_a[n % 5], cyc_b[n % 2]) for n in numbers}


def _score_files(tool, key_path, texts, capsys, tmp):
    paths = []
    for i, t in enumerate(texts):
        p = tmp / f"filled_{i}.md"
        p.write_text(t)
        paths.append(str(p))
    rc = tool.main(["--score-sheet", *paths, "--judging-key", str(key_path), "--format", "json"])
    return rc, json.loads(capsys.readouterr().out)


def test_p1_parts_concatenate_to_the_full_sheets_items_numbers_unchanged(capsys):
    tool = _load_tool()
    tmp, sheet, key, parts, _ = _sample_parts(tool, capsys)
    assert len(parts) > 1
    full_text = sheet.read_text()
    texts = [p.read_text() for p in parts]
    assert "\n".join(_items_section(t) for t in texts) == _items_section(full_text)
    assert [n for t in texts for n in _item_numbers(t)] == list(range(1, 31))
    head = full_text[:full_text.index("## Item ")]
    n = len(parts)
    for k, t in enumerate(texts, start=1):
        assert head.rstrip("\n") in t[:t.index("## Item ")], "the sheet's own instructions are in every part"
        nums = _item_numbers(t)
        assert f"Part {k} of {n}: items {nums[0]}-{nums[-1]} of 30." in t
    assert [p.name for p in parts] == [f"sheet.part{k}of{n}.md" for k in range(1, n + 1)]


def test_p2_every_part_within_the_byte_bound_and_ranges_are_contiguous(capsys):
    tool = _load_tool()
    tmp, sheet, key, parts, _ = _sample_parts(tool, capsys)
    assert len(parts) > 1
    prev_last = 0
    for p in parts:
        assert len(p.read_bytes()) <= _PART_BYTES
        nums = _item_numbers(p.read_text())
        assert nums[0] == prev_last + 1 and nums == list(range(nums[0], nums[-1] + 1))
        prev_last = nums[-1]
    assert prev_last == 30


def test_p3_an_item_larger_than_the_bound_is_refused_exit_2_and_names_it(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    for i, r in enumerate(rows[:12]):  # the whole lowest bin: some of them are in any 30-item draw
        old_quote = r["quote"]
        r["quote"] = "x" * 3000 + f" big quote {i}"
        sims[r["quote"]] = sims.pop(old_quote)
    db = _make_db(rows)
    # the same draw with no bound: find the oversized item's number and size
    sheet, key, _ = _sample(tool, db, _tmp(), sims, capsys)
    # an item's own bytes: its header line to its last line, without the "\n" that joins items
    sizes = {}
    for m in re.finditer(r"^## Item (\d+)\n.*?(?=^## Item |\Z)", sheet.read_text(), flags=re.S | re.M):
        blk = m.group(0)
        sizes[int(m.group(1))] = len((blk[:-1] if blk.endswith("\n\n") else blk).encode())
    big_nums = [n for n, s in sizes.items() if s > 3000]
    assert big_nums, "an oversized node must be in the 30-item draw (seed 7)"
    capsys.readouterr()
    tmp2 = _tmp()
    with pytest.raises(SystemExit) as exc:
        _sample(tool, db, tmp2, sims, capsys, extra=("--sheet-part-bytes", "2000"))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert f"item {big_nums[0]} " in err and str(sizes[big_nums[0]]) in err
    assert _part_files(tmp2) == []


def test_p4_scoring_filled_parts_equals_scoring_the_filled_full_sheet(capsys):
    tool = _load_tool()
    tmp, sheet, key, parts, _ = _sample_parts(tool, capsys)
    ans = _answers_for(range(1, 31))
    rc_full, full = _score_files(tool, key, [_fill_text(sheet.read_text(), ans)], capsys, _tmp())
    rc_parts, byparts = _score_files(tool, key, [_fill_text(p.read_text(), ans) for p in parts],
                                     capsys, _tmp())
    assert rc_full == rc_parts == 0
    assert full["verdicts"] == byparts["verdicts"]
    assert full["per_bin"] == byparts["per_bin"]
    assert full["n_unparsed"] == 0 and any(s["n_answered"] for s in full["per_bin"].values())


def test_p5_item_in_two_parts_is_a_duplicate_and_item_in_no_part_is_missing(capsys):
    tool = _load_tool()
    tmp, sheet, key, parts, _ = _sample_parts(tool, capsys)
    ans = _answers_for(range(1, 31))
    texts = [_fill_text(p.read_text(), ans) for p in parts]
    # duplicate: item 1 (first part) also appears at the end of the second part
    item1 = re.search(r"^## Item 1\n.*?(?=^## Item 2\n)", texts[0], re.S | re.M).group(0)
    dup = [texts[0], texts[1] + "\n" + item1, *texts[2:]]
    rc, res = _score_files(tool, key, dup, capsys, _tmp())
    assert rc == 3 and res["n_unparsed"] == 1 and res["unparsed_items"] == [1]
    assert res["unparsed_fields"]["1"] == {"a": "duplicate item header", "b": "duplicate item header"}
    # missing: item 2 cut out of the part that holds it
    cut = re.sub(r"^## Item 2\n.*?(?=^## Item 3\n)", "", texts[0], flags=re.S | re.M)
    assert "## Item 2\n" not in cut
    rc, res = _score_files(tool, key, [cut, *texts[1:]], capsys, _tmp())
    assert rc == 3 and res["n_unparsed"] == 1 and res["unparsed_items"] == [2]
    assert res["unparsed_fields"]["2"] == {"a": "item missing from the sheet",
                                           "b": "item missing from the sheet"}


def test_p6_parts_have_the_sheets_mode_and_the_key_stays_0600(capsys):
    tool = _load_tool()
    old = os.umask(0o022)
    try:
        tmp, sheet, key, parts, _ = _sample_parts(tool, capsys)
    finally:
        os.umask(old)
    assert parts
    mode = lambda p: p.stat().st_mode & 0o777
    assert all(mode(p) == mode(sheet) for p in parts)
    assert mode(sheet) & 0o044 == 0o044 and mode(sheet) != 0o600
    assert mode(key) == 0o600
    assert len(list(tmp.glob("*.json"))) == 1, "one private key file"


def test_p7_without_the_flag_the_output_is_byte_identical_and_no_parts(capsys):
    import hashlib
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    sheet, key, out = _sample(tool, db, tmp, sims, capsys)
    assert hashlib.sha256(sheet.read_bytes()).hexdigest() == _SHEET_SHA256_BEFORE_PARTS
    assert _part_files(tmp) == []
    meta = json.loads(out)["meta"]
    assert "sheet_parts" not in meta and "sheet_part_bytes" not in meta
    # and the flag leaves the full sheet itself untouched
    _, sheet2, _, parts2, _ = _sample_parts(tool, capsys)
    assert sheet2.read_bytes() == sheet.read_bytes() and parts2


def test_p8_export_reports_sheet_parts_in_meta_and_notice(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    tool.engram_anchoring.quote_claim_similarity = _stub(sims)
    assert tool.main(["--db", str(db), "--format", "json", "--judging-sheet", str(tmp / "sheet.md"),
                      "--judging-key", str(tmp / "key.json"), "--sheet-part-bytes", str(_PART_BYTES)]) == 0
    cap = capsys.readouterr()
    parts = _part_files(tmp)
    meta = json.loads(cap.out)["meta"]
    assert meta["sheet_parts"] == len(parts) > 1 and meta["sheet_part_bytes"] == _PART_BYTES
    err = cap.err
    assert f"sheet_parts={len(parts)}" in err and f"sheet_part_bytes={_PART_BYTES}" in err


# --------------------------------------------------------------------------- P: stale parts, multi-byte
def _export_raw(tool, db, tmp, sims, capsys, extra=(), swap=(), sheet_name="sheet.md"):
    """Run main directly so stderr survives; returns (rc, captured)."""
    tool.engram_anchoring.quote_claim_similarity = _stub(sims)
    argv = ["--db", str(db), "--format", "json", "--judging-sheet", str(tmp / sheet_name),
            "--judging-key", str(tmp / "key.json"), "--seed", "7", *extra]
    for n in swap:
        argv += ["--swap", str(n)]
    rc = tool.main(argv)
    return rc, capsys.readouterr()


def _all_parts(tmp):
    return sorted(p.name for p in tmp.glob("*.part*of*"))


def test_p9_rerun_with_fewer_parts_leaves_only_the_new_parts(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    rc, _ = _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", "1000"))
    first = _part_files(tmp)
    assert rc == 0 and len(first) > 3
    rc, cap = _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", "10000000"))
    assert rc == 0
    assert _all_parts(tmp) == ["sheet.part1of1.md"]
    assert f"removed {len(first)} stale sheet part file" in cap.err


def test_p10_swap_without_the_flag_removes_every_old_part(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", str(_PART_BYTES)))
    old = _part_files(tmp)
    assert len(old) > 1
    rc, cap = _export_raw(tool, db, tmp, sims, capsys, swap=(1,))
    assert rc == 0
    assert _all_parts(tmp) == []
    assert f"removed {len(old)} stale sheet part file" in cap.err


def test_p11_a_refused_run_writes_and_deletes_nothing(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", str(_PART_BYTES)))
    before = {p.name: p.read_bytes() for p in tmp.iterdir()}
    assert len(_part_files(tmp)) > 1
    with pytest.raises(SystemExit) as exc:
        _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", "100"))
    assert exc.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", "100"), swap=(1,))
    assert exc.value.code == 2
    assert {p.name: p.read_bytes() for p in tmp.iterdir()} == before


def test_p12_only_exact_part_names_of_this_sheet_are_removed(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    keep = ["sheet.part1of2.md.bak", "other.part1of2.md", "sheet.part1of2.txt", "sheet.partXof2.md",
            "sheet.part1of.md", "xsheet.part1of2.md", "sheet.part1of2.md.md"]
    for name in keep:
        (tmp / name).write_text("keep me")
    (tmp / "sheet.part1of2.md").write_text("stale")
    (tmp / "sheet.part07of12.md").write_text("stale")
    rc, cap = _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", "10000000"))
    assert rc == 0
    for name in keep:
        assert (tmp / name).read_text() == "keep me", name
    assert not (tmp / "sheet.part07of12.md").exists()
    assert (tmp / "sheet.part1of1.md").is_file() and not (tmp / "sheet.part1of2.md").exists()
    assert "removed 2 stale sheet part file" in cap.err


def test_p13_a_directory_with_a_part_name_is_left_alone(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    (tmp / "sheet.part1of2.md").mkdir()
    (tmp / "sheet.part1of2.md" / "inner.txt").write_text("keep me")
    rc, _ = _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", "10000000"))
    assert rc == 0 and (tmp / "sheet.part1of2.md" / "inner.txt").read_text() == "keep me"


def test_p14_a_symlink_with_a_part_name_is_unlinked_not_followed(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    target = tmp / "precious.txt"
    target.write_text("KEEP")
    # a stale link, and a link sitting exactly where the new part will be written
    (tmp / "sheet.part1of1.md").symlink_to(target)
    (tmp / "sheet.part3of9.md").symlink_to(tmp / "no-such-target")  # dangling
    rc, cap = _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", "10000000"))
    assert rc == 0
    assert target.read_text() == "KEEP", "writing the new part must not go through the old link"
    assert not (tmp / "sheet.part1of1.md").is_symlink()
    assert not os.path.lexists(tmp / "sheet.part3of9.md")
    assert "removed 2 stale sheet part file" in cap.err


def test_p15_multibyte_parts_are_within_the_bound_in_encoded_bytes(capsys):
    tool = _load_tool()
    rows, sims = _planted_graph()
    mb = "é中文🙂" * 90  # 360 characters, 1080 bytes: bytes and characters differ by 3x
    for i, r in enumerate(rows):
        if r["quote_type"] == "hard_data":
            continue
        old = r["quote"]
        r["quote"] = f"{old} {mb}"
        sims[r["quote"]] = sims.pop(old)
    db = _make_db(rows)
    tmp = _tmp()
    b = 3000
    rc, _ = _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", str(b)))
    assert rc == 0
    parts = _part_files(tmp)
    assert len(parts) > 3
    for p in parts:
        assert len(p.read_bytes()) <= b, p.name
        assert len(p.read_text(encoding="utf-8")) < len(p.read_bytes()), "the part really is multi-byte"
    full = (tmp / "sheet.md").read_text(encoding="utf-8")
    assert "\n".join(_items_section(p.read_text(encoding="utf-8")) for p in parts) == _items_section(full)


@pytest.mark.parametrize("sheet_name,decoys,stale", [
    ("a.b.md", ["aXb.part1of2.md", "a.b.part1of2.mdX", "a.b.part1of2Xmd", "a.b.part1of2.txt"],
     "a.b.part1of2.md"),
    ("x+y.md", ["xxy.part1of2.md", "xy.part1of2.md", "x+y.part1of2.mdd"], "x+y.part1of2.md"),
    ("s(1).md", ["s1.part1of2.md"], "s(1).part1of2.md"),
])
def test_p16_regex_metacharacters_in_the_sheet_name_never_delete_a_non_part(
        capsys, sheet_name, decoys, stale):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    for name in decoys:
        (tmp / name).write_text("keep me")
    (tmp / stale).write_text("stale")
    rc, cap = _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", "10000000"),
                          sheet_name=sheet_name)
    assert rc == 0
    for name in decoys:
        assert (tmp / name).read_text() == "keep me", name
    assert not (tmp / stale).exists()
    assert "removed 1 stale sheet part file" in cap.err


def _snapshot(tmp):
    """name -> (kind, bytes-or-link-target, mode) for every entry, never following links."""
    snap = {}
    for p in sorted(tmp.iterdir()):
        st = p.lstat()
        if p.is_symlink():
            snap[p.name] = ("link", os.readlink(p), None)
        else:
            snap[p.name] = ("file", p.read_bytes(), st.st_mode & 0o777)
    return snap


@pytest.mark.parametrize("which", ["sheet", "key"])
@pytest.mark.parametrize("mode", ["export", "export-parts", "swap"])
def test_p17_a_symlink_at_the_sheet_or_key_path_is_refused_exit_2_and_nothing_changes(capsys, which, mode):
    tool = _load_tool()
    rows, sims = _planted_graph()
    db = _make_db(rows)
    tmp = _tmp()
    link = tmp / ("sheet.md" if which == "sheet" else "key.json")
    if mode == "swap":
        _export_raw(tool, db, tmp, sims, capsys, extra=("--sheet-part-bytes", str(_PART_BYTES)))
        real = tmp / "elsewhere.dat"
        link.rename(real)
    else:
        real = tmp / "precious.txt"
        real.write_text("KEEP")
        if mode == "export-parts":
            (tmp / "sheet.part1of8.md").write_text("an old part")
    real.chmod(0o640)  # the key writer used to chmod the link TARGET to 0600
    link.symlink_to(real)
    before = _snapshot(tmp)
    extra = ("--sheet-part-bytes", str(_PART_BYTES)) if mode != "export" else ()
    with pytest.raises(SystemExit) as exc:
        _export_raw(tool, db, tmp, sims, capsys, extra=extra, swap=(1,) if mode == "swap" else ())
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert str(link) in err and "symlink" in err
    assert _snapshot(tmp) == before, "a refused run writes and deletes nothing, and the target is untouched"
    assert link.is_symlink()
