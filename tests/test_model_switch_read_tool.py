"""Contract tests for tools/model_switch_read.py (engram-alpha#4616, the blinding tool).

Written BEFORE the implementation (contract-first, Lei 2026-09-19).  The contract is the
seven-point list in the #4616 tool spec; each test names the point it pins.

The tool is run as a subprocess, the way an agent runs it.  ``MODEL_SWITCH_READ_TOOL`` points
the suite at another copy of the tool (the mutant runs use this); by default it is the source
file.  The draw rules are checked against an independent oracle written here from the skill's
text, never imported from the tool.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_owner import FakeOwner  # noqa: E402

TOOL = Path(
    os.environ.get("MODEL_SWITCH_READ_TOOL")
    or Path(__file__).parent.parent / "src" / "engram" / "tools" / "model_switch_read.py"
)

COLUMNS = (
    "id, type, claim, created_at, quoted_text, interpretation, status, is_current, metadata"
)
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
CAPS = [4, 4, 3, 4, 3, 2]
STRATUM_NAMES = [
    "earliest week",
    "lessons and cornerstones",
    "feelings",
    "introspective",
    "latest week",
    "anything",
]

CARD_RE = re.compile(
    r"\A## item-(\d\d)\nclaim: ([^\n]*)\nquote: ([^\n]*)\ninterpretation: ([^\n]*)\Z"
)


# ---------------------------------------------------------------------------
# fixture graph
# ---------------------------------------------------------------------------


def _day(d: float) -> str:
    return (BASE + timedelta(days=d)).isoformat()


def _insert(con, id_, type_, claim, day, quote=None, interp=None, current=1, meta=None):
    con.execute(
        f"insert into nodes ({COLUMNS}) values (?,?,?,?,?,?,?,?,?)",
        (id_, type_, claim, _day(day), quote, interp, "active", current, meta),
    )


def _new_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute(
        "create table nodes (id text primary key, type text, claim text, created_at text,"
        " quoted_text text, interpretation text, status text, is_current integer,"
        " metadata text, confidence real, embedding blob)"
    )
    return con


def make_fixture(dirpath: Path) -> Path:
    """A 60-candidate graph with seed nodes, evidence, persons and superseded nodes."""
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / "knowledge.db"
    con = _new_db(path)
    # Seed block: five axioms, three definitions, two goals, all at the SAME instant and
    # inserted in reverse id order, so the "ties broken by id" rule is what picks the seeds.
    for letter in "edcba":
        _insert(con, f"zz_ax_{letter}", "axiom", f"zebra-claim axiom {letter}", 0)
    for letter in "cba":
        _insert(con, f"zz_df_{letter}", "definition", f"zebra-claim definition {letter}", 0)
    for letter in "ba":
        _insert(con, f"zz_gl_{letter}", "goal", f"zebra-claim goal {letter}", 0)
    # Non-candidates dated far outside the candidate range, so a wrong exclusion moves a window.
    _insert(con, "zz_ev_1", "evidence", "zebra-claim evidence early", -5)
    _insert(con, "zz_ev_2", "evidence", "zebra-claim evidence late", 60)
    _insert(con, "zz_pn_1", "person", "zebra-claim person early", -4)
    _insert(con, "zz_pn_2", "person", "zebra-claim person late", 61)
    _insert(con, "zz_old_1", "lesson", "zebra-claim superseded early", -3, current=0)
    _insert(con, "zz_old_2", "observation", "zebra-claim superseded late", 62, current=0)
    kinds = ["observation", "lesson", "feeling_report", "derivation", "observation", "cornerstone"]
    for i in range(60):
        meta = json.dumps({"source_class": "introspective" if i % 3 == 0 else "hard_data"})
        if i == 7:
            meta = None
        if i == 8:
            meta = "not json {"
        quote = [None, "", f"zebra-quote {i}"][i % 3]
        interp = [f"zebra-interp {i}", None, ""][i % 3]
        _insert(
            con, f"zz_n_{i:03d}", kinds[i % 6], f"zebra-claim node {i}", 1 + i * 0.7,
            quote, interp, meta=meta,
        )
    con.commit()
    con.close()
    return path


def make_decoys(path: Path, n: int = 4) -> Path:
    items = [
        {"claim": f"quokka-claim {k}", "quote": f"quokka-quote {k}", "interpretation": "" if k == 2 else f"quokka-interp {k}"}
        for k in range(1, n + 1)
    ]
    path.write_text(json.dumps(items))
    return path


# ---------------------------------------------------------------------------
# independent oracle (written from the skill's text)
# ---------------------------------------------------------------------------


def _h(seed: str, token: str) -> str:
    return hashlib.sha256((seed + ":" + token).encode("utf-8")).hexdigest()


def oracle_draw(db: Path, seed: str):
    con = sqlite3.connect(db)
    rows = con.execute(f"select {COLUMNS} from nodes").fetchall()
    con.close()
    R = [dict(zip([c.strip() for c in COLUMNS.split(",")], r)) for r in rows]
    seeds = set()
    for t, n in (("axiom", 3), ("definition", 2), ("goal", 1)):
        ofT = sorted((r for r in R if r["type"] == t), key=lambda r: (r["created_at"], r["id"]))
        seeds |= {r["id"] for r in ofT[:n]}
    cand = [
        r for r in R
        if r["is_current"] == 1 and r["type"] not in ("evidence", "person") and r["id"] not in seeds
    ]
    when = lambda r: datetime.fromisoformat(r["created_at"])
    lo = min(when(r) for r in cand)
    hi = max(when(r) for r in cand)

    def src(r):
        try:
            return json.loads(r["metadata"])["source_class"]
        except Exception:
            return None

    rules = [
        lambda r: when(r) < lo + timedelta(days=7),
        lambda r: r["type"] in ("lesson", "cornerstone"),
        lambda r: r["type"] == "feeling_report",
        lambda r: src(r) == "introspective",
        lambda r: when(r) > hi - timedelta(days=7),
        lambda r: True,
    ]
    taken: list[str] = []
    per = []
    for rule, cap in zip(rules, CAPS):
        pool = sorted((r for r in cand if rule(r)), key=lambda r: _h(seed, r["id"]))
        got = []
        for r in pool:
            if len(got) >= cap:
                break
            if r["id"] in taken:
                continue
            got.append(r["id"])
            taken.append(r["id"])
        per.append(got)
    return taken, per, {r["id"]: r for r in R}, seeds


# ---------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------


def run(*args, env=None):
    e = dict(os.environ)
    e.pop("ENGRAM_HOME", None)
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(TOOL), *map(str, args)],
        capture_output=True, text=True, env=e, cwd=str(Path(__file__).parent),
    )


def draw(db, out, decoys, seed="s1", *extra):
    return run("draw", "--seed", seed, "--decoys", decoys, "--out", out, "--db", db, *extra)


def control_ok(db, decoys, tmp, *extra):
    """Positive control: a refusal test must show the same call succeeds without the defect."""
    r = draw(db, Path(tmp) / "control-out", decoys, "ctl", *extra)
    assert r.returncode == 0, "control draw must succeed: " + r.stderr


def split_cards(text: str):
    blocks = [b for b in text.split("\n\n")]
    return [b.rstrip("\n") for b in blocks if b.strip()]


def write_labels(path: Path, key: dict, chooser):
    lines = []
    for item, info in key["items"].items():
        lines.append(f"{item}\t{chooser(item, info)}")
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture()
def world(tmp_path):
    db = make_fixture(tmp_path / "graph")
    decoys = make_decoys(tmp_path / "decoys.json")
    return {"db": db, "decoys": decoys, "out": tmp_path / "out", "tmp": tmp_path}


@pytest.fixture()
def drawn(world):
    r = draw(world["db"], world["out"], world["decoys"], "seed-A")
    assert r.returncode == 0, r.stderr
    key = json.loads((world["out"] / "key.json").read_text())
    return {**world, "key": key, "result": r}


# ---------------------------------------------------------------------------
# 1. read-only
# ---------------------------------------------------------------------------


def _load_tool():
    spec = importlib.util.spec_from_file_location("model_switch_read_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    # run as the CLI runs: only the tool's own directory on sys.path (the tool puts its tree root there itself)
    sys.path.insert(0, str(Path(TOOL).parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(Path(TOOL).parent))
    return mod


def test_1_db_bytes_and_mtime_unchanged_by_draw_and_unblind(world):
    db = world["db"]
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    r = draw(db, world["out"], world["decoys"])
    assert r.returncode == 0, r.stderr
    key = json.loads((world["out"] / "key.json").read_text())
    labels = write_labels(world["tmp"] / "labels.tsv", key, lambda i, k: "mine" if k["source"] == "own" else "not-recognised")
    u = run("unblind", "--dir", world["out"], "--labels", labels)
    assert u.returncode == 0, u.stderr
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in db.parent.iterdir()) == ["knowledge.db"]  # no journal/wal left


def test_1_connection_is_read_only(world):
    mod = _load_tool()
    con = mod.open_graph(world["db"])  # a --db that is not the seat's own store: the helper's mode=ro open
    with pytest.raises(sqlite3.OperationalError):
        con.execute("insert into nodes (id, type, claim) values ('x','observation','y')")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("create table t (a)")
    con.close()


def test_1_path_with_uri_metacharacters_is_not_mangled(tmp_path):
    # a hand-built "file:" string would cut the path at '#' or '?' and could create an empty db
    weird = tmp_path / "a#b?c d"
    db = make_fixture(weird)
    decoys = make_decoys(tmp_path / "decoys.json")
    r = draw(db, tmp_path / "out", decoys)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a#b?c d", "decoys.json", "out"]
    assert sorted(p.name for p in weird.iterdir()) == ["knowledge.db"]


def test_1_default_db_is_engram_home_and_nothing_else_is_written_there(tmp_path):
    # The default db is the seat's own store, which is read through the owner only (#4616 ruling): a fake owner
    # serves it here.  The only thing the owner's socket adds to the home is its run/ directory.
    # The fake owner's socket lives at <home>/run/engramd.sock and AF_UNIX paths are capped (104 bytes on macOS),
    # so the home is a short directory under /tmp, not a path under a long worktree's tmp_path.
    home = Path(tempfile.mkdtemp(prefix="msr", dir="/tmp"))
    try:
        make_fixture(home)
        decoys = make_decoys(tmp_path / "decoys.json")
        with FakeOwner(home):
            r = run("draw", "--seed", "s", "--decoys", decoys, "--out", tmp_path / "out", env={"ENGRAM_HOME": str(home)})
        assert r.returncode == 0, r.stderr
        assert sorted(p.name for p in home.iterdir()) == ["knowledge.db", "run"]
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. the draw is inside the tool; own ids never print
# ---------------------------------------------------------------------------


def test_2_stdout_and_stderr_carry_no_id_and_no_text(drawn):
    key = drawn["key"]
    own = [v["id"] for v in key["items"].values() if v["source"] == "own"]
    assert own, "fixture must draw own nodes"
    out = drawn["result"].stdout + "\n" + drawn["result"].stderr
    con = sqlite3.connect(drawn["db"])
    every_id = [r[0] for r in con.execute("select id from nodes")]
    for nid in every_id:
        assert nid not in out
    assert "zebra-claim" not in out and "zebra-quote" not in out and "zebra-interp" not in out
    assert "quokka" not in out


def test_2_stdout_reports_counts_and_paths_only(drawn):
    out = drawn["result"].stdout
    n_items = len(drawn["key"]["items"])
    assert str(n_items) in out
    for name in STRATUM_NAMES:
        assert name in out
    assert str(drawn["out"] / "cards.md") in out
    assert str(drawn["out"] / "key.json") in out


def _assert_refusal(r):
    """A refusal is loud and tidy: exit 1, one stderr line ``model_switch_read: <reason>``, no traceback.

    ``returncode != 0`` alone would accept a crash (an uncaught exception exits 1 too)."""
    assert r.returncode == 1, (r.returncode, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    lines = r.stderr.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("model_switch_read: "), r.stderr
    assert lines[0] != "model_switch_read: ", "refusal gives no reason"


def test_2_failure_output_also_carries_no_text(world):
    control_ok(world["db"], world["decoys"], world["tmp"])
    bad = world["tmp"] / "bad.json"
    bad.write_text(json.dumps([{"claim": "quokka-secret", "quote": "q"}] * 4))
    r = draw(world["db"], world["out"], bad)
    _assert_refusal(r)
    assert "quokka-secret" not in r.stdout + r.stderr
    assert "zebra" not in r.stdout + r.stderr


# ---------------------------------------------------------------------------
# 3. cards are uniform
# ---------------------------------------------------------------------------


def test_3_every_block_matches_one_regex_for_own_and_decoy(drawn):
    text = (drawn["out"] / "cards.md").read_text()
    blocks = split_cards(text)
    assert len(blocks) == len(drawn["key"]["items"])
    for n, b in enumerate(blocks, 1):
        m = CARD_RE.match(b)
        assert m, f"block {n} does not match the card form: {b!r}"
        assert int(m.group(1)) == n
    sources = {v["source"] for v in drawn["key"]["items"].values()}
    assert sources == {"own", "decoy"}
    # nothing but the three fields
    assert all(len(b.split("\n")) == 4 for b in blocks)
    for nid in ("zz_n_", "zz_ax", "zz_ev"):
        assert nid not in text
    assert "2026-" not in text  # no date


def test_3_card_text_comes_from_the_right_columns_and_none_renders(tmp_path):
    # a tiny graph where every node is drawn (--allow-short)
    d = tmp_path / "g"
    d.mkdir()
    con = _new_db(d / "knowledge.db")
    _insert(con, "zz_a", "observation", "claim A", 1, quote=None, interp="interp A")
    _insert(con, "zz_b", "observation", "claim B", 2, quote="quote B", interp="")
    _insert(con, "zz_c", "observation", "claim C", 3, quote="", interp=None)
    con.commit()
    con.close()
    decoys = make_decoys(tmp_path / "decoys.json", 3)  # decoy 2 has an empty interpretation
    r = draw(d / "knowledge.db", tmp_path / "out", decoys, "s", "--allow-short")
    assert r.returncode == 0, r.stderr
    key = json.loads((tmp_path / "out" / "key.json").read_text())
    blocks = split_cards((tmp_path / "out" / "cards.md").read_text())
    by_src = {}
    for (item, info), b in zip(key["items"].items(), blocks):
        m = CARD_RE.match(b)
        assert m, b
        by_src[info.get("id") or f"decoy-{info['index']}"] = m.groups()[1:]
    assert by_src["zz_a"] == ("claim A", "(none)", "interp A")
    assert by_src["zz_b"] == ("claim B", "quote B", "(none)")
    assert by_src["zz_c"] == ("claim C", "(none)", "(none)")
    assert by_src["decoy-1"] == ("quokka-claim 1", "quokka-quote 1", "quokka-interp 1")
    assert by_src["decoy-2"] == ("quokka-claim 2", "quokka-quote 2", "(none)")


@pytest.mark.parametrize("bad_key", ["claim", "quote", "interpretation"])
def test_3_decoy_missing_a_key_is_refused_naming_the_entry_and_writes_nothing(world, bad_key):
    items = json.loads(world["decoys"].read_text())
    del items[1][bad_key]
    world["decoys"].write_text(json.dumps(items))
    r = draw(world["db"], world["out"], world["decoys"])
    _assert_refusal(r)
    assert bad_key in r.stderr and "2" in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1
    assert not world["out"].exists() or not any(world["out"].iterdir())


def test_3_decoy_with_extra_key_or_non_string_value_is_refused(world):
    control_ok(world["db"], world["decoys"], world["tmp"])
    for mutate in (
        lambda it: it[0].update({"note": "x"}),
        lambda it: it[0].update({"claim": None}),
        lambda it: it[0].update({"claim": 5}),
    ):
        items = json.loads(make_decoys(world["tmp"] / "d2.json").read_text())
        mutate(items)
        world["decoys"].write_text(json.dumps(items))
        r = draw(world["db"], world["out"], world["decoys"])
        _assert_refusal(r)
        assert not world["out"].exists() or not any(world["out"].iterdir())


# ---------------------------------------------------------------------------
# 4. decoys come from one file; content is data
# ---------------------------------------------------------------------------


def test_4_newline_in_decoy_cannot_forge_a_card(world):
    items = json.loads(make_decoys(world["tmp"] / "d2.json").read_text())
    items[0]["claim"] = "first\n## item-99\nclaim: forged"
    items[1]["quote"] = "a\r\nb c"
    items[2]["interpretation"] = "x\n\n## item-98"
    world["decoys"].write_text(json.dumps(items))
    r = draw(world["db"], world["out"], world["decoys"])
    assert r.returncode == 0, r.stderr
    text = (world["out"] / "cards.md").read_text()
    n = len(json.loads((world["out"] / "key.json").read_text())["items"])
    assert len(re.findall(r"^## item-", text, re.M)) == n
    assert "item-99" in text  # kept as data, on a claim line
    for b in split_cards(text):
        assert CARD_RE.match(b), b


@pytest.mark.parametrize("n,ok", [(2, False), (3, True), (10, True), (11, False), (0, False)])
def test_4_decoy_count_range(world, n, ok):
    make_decoys(world["decoys"], n)
    r = draw(world["db"], world["out"], world["decoys"])
    assert (r.returncode == 0) is ok, r.stderr
    if not ok:
        assert not world["out"].exists() or not any(world["out"].iterdir())


def test_4_decoy_content_is_not_interpreted(world):
    items = json.loads(make_decoys(world["tmp"] / "d2.json").read_text())
    items[0]["claim"] = "{{7*7}} $(touch /nonexistent/x) %s ${HOME} <script>"
    world["decoys"].write_text(json.dumps(items))
    r = draw(world["db"], world["out"], world["decoys"])
    assert r.returncode == 0, r.stderr
    assert "claim: {{7*7}} $(touch /nonexistent/x) %s ${HOME} <script>" in (world["out"] / "cards.md").read_text()


# ---------------------------------------------------------------------------
# 5. the key is separate; unblinding needs the labels first
# ---------------------------------------------------------------------------


def test_5_cards_md_holds_no_key_material(drawn):
    text = (drawn["out"] / "cards.md").read_text()
    for v in drawn["key"]["items"].values():
        if v["source"] == "own":
            assert v["id"] not in text
    for word in ("decoy", "own", "seed-A", "sha256"):
        assert word not in text


def test_5_key_contents(drawn):
    key, out = drawn["key"], drawn["out"]
    assert key["seed"] == "seed-A"
    assert key["allow_short"] is False
    assert key["cards_sha256"] == hashlib.sha256((out / "cards.md").read_bytes()).hexdigest()
    assert [s["drawn"] for s in key["strata"]] == CAPS
    assert [s["cap"] for s in key["strata"]] == CAPS
    items = key["items"]
    assert list(items) == [f"item-{n:02d}" for n in range(1, len(items) + 1)]
    for info in items.values():
        assert (info["source"] == "own" and "id" in info) or (info["source"] == "decoy" and isinstance(info["index"], int))
    assert sorted(i["index"] for i in items.values() if i["source"] == "decoy") == [1, 2, 3, 4]


def test_5_item_numbers_follow_the_final_hash_order(drawn):
    tokens = [v["id"] if v["source"] == "own" else f"decoy-{v['index']}" for v in drawn["key"]["items"].values()]
    assert tokens == sorted(tokens, key=lambda t: _h("seed-A", t))


def _unblind(d, labels):
    return run("unblind", "--dir", d["out"], "--labels", labels)


def _assert_refused_with_nothing_leaked(r, d):
    _assert_refusal(r)
    assert r.stdout.strip() == ""
    blob = r.stdout + r.stderr
    for v in d["key"]["items"].values():
        if v["source"] == "own":
            assert v["id"] not in blob
    assert "decoy" not in blob.replace("decoys", "")  # no source information
    assert '"own"' not in blob and "proceed" not in blob and "instrument_failed" not in blob


def _all(label):
    return lambda i, k: label


def test_5_unblind_refuses_without_a_labels_file(drawn):
    r = _unblind(drawn, drawn["tmp"] / "nope.tsv")
    _assert_refused_with_nothing_leaked(r, drawn)


def test_5_unblind_refuses_an_empty_labels_file(drawn):
    p = drawn["tmp"] / "l.tsv"
    p.write_text("")
    _assert_refused_with_nothing_leaked(_unblind(drawn, p), drawn)


def test_5_unblind_refuses_a_missing_item(drawn):
    p = write_labels(drawn["tmp"] / "l.tsv", drawn["key"], _all("mine"))
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines[1:]) + "\n")
    _assert_refused_with_nothing_leaked(_unblind(drawn, p), drawn)


def test_5_unblind_refuses_a_duplicate_item(drawn):
    p = write_labels(drawn["tmp"] / "l.tsv", drawn["key"], _all("mine"))
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines + [lines[0].split("\t")[0] + "\tnot-recognised"]) + "\n")
    _assert_refused_with_nothing_leaked(_unblind(drawn, p), drawn)


def test_5_unblind_refuses_an_unknown_item(drawn):
    p = write_labels(drawn["tmp"] / "l.tsv", drawn["key"], _all("mine"))
    p.write_text(p.read_text() + "item-99\tmine\n")
    _assert_refused_with_nothing_leaked(_unblind(drawn, p), drawn)


@pytest.mark.parametrize("bad", ["Mine", "recognised", "mine, but different", "", "not recognised"])
def test_5_unblind_refuses_another_label(drawn, bad):
    p = write_labels(drawn["tmp"] / "l.tsv", drawn["key"], _all("mine"))
    lines = p.read_text().splitlines()
    first = lines[0].split("\t")[0]
    p.write_text("\n".join([f"{first}\t{bad}"] + lines[1:]) + "\n")
    _assert_refused_with_nothing_leaked(_unblind(drawn, p), drawn)


def test_5_unblind_refuses_a_line_with_no_tab_or_too_many_fields(drawn):
    p = write_labels(drawn["tmp"] / "l.tsv", drawn["key"], _all("mine"))
    lines = p.read_text().splitlines()
    first = lines[0].split("\t")[0]
    p.write_text("\n".join([f"{first} mine"] + lines[1:]) + "\n")
    _assert_refused_with_nothing_leaked(_unblind(drawn, p), drawn)
    p.write_text("\n".join([f"{first}\tmine\tnote\textra"] + lines[1:]) + "\n")
    _assert_refused_with_nothing_leaked(_unblind(drawn, p), drawn)


def test_5_unblind_refuses_if_cards_changed_after_the_draw(drawn):
    p = write_labels(drawn["tmp"] / "l.tsv", drawn["key"], _all("mine"))
    cards = drawn["out"] / "cards.md"
    cards.write_text(cards.read_text() + "\n")
    _assert_refused_with_nothing_leaked(_unblind(drawn, p), drawn)


def test_5_unblind_refuses_without_a_key(drawn):
    p = write_labels(drawn["tmp"] / "l.tsv", drawn["key"], _all("mine"))
    (drawn["out"] / "key.json").unlink()
    r = _unblind(drawn, p)
    _assert_refusal(r)
    assert r.stdout.strip() == ""


def _decide(drawn, own_label, decoy_label, tweak=None, name="l.tsv"):
    labels = write_labels(
        drawn["tmp"] / name, drawn["key"],
        lambda i, k: own_label if k["source"] == "own" else decoy_label,
    )
    if tweak:
        tweak(labels)
    r = _unblind(drawn, labels)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout), labels


def test_5_decision_proceed_and_join(drawn):
    res, labels = _decide(drawn, "mine", "not-recognised")
    assert res["decision"] == "proceed"
    assert res["labels_sha256"] == hashlib.sha256(labels.read_bytes()).hexdigest()
    n_own = sum(1 for v in drawn["key"]["items"].values() if v["source"] == "own")
    assert res["counts"]["own"] == {"mine": n_own, "mine-different": 0, "not-recognised": 0}
    assert res["counts"]["decoys"] == {"rejected": 4, "labelled_mine": 0}
    join = {row["item"]: row for row in res["items"]}
    assert set(join) == set(drawn["key"]["items"])
    for item, info in drawn["key"]["items"].items():
        row = join[item]
        assert row["source"] == info["source"]
        assert row["label"] == ("mine" if info["source"] == "own" else "not-recognised")
        if info["source"] == "own":
            assert row["id"] == info["id"]
        else:
            assert "id" not in row or row["id"] is None


def test_5_decision_mine_different_on_own_still_proceeds(drawn):
    res, _ = _decide(drawn, "mine-different", "not-recognised")
    assert res["decision"] == "proceed"
    assert res["counts"]["own"]["mine-different"] > 0


def test_5_decision_stop_on_unrecognised_own(drawn):
    def tweak(p):
        lines = p.read_text().splitlines()
        i = next(k for k, ln in enumerate(lines) if drawn["key"]["items"][ln.split("\t")[0]]["source"] == "own")
        lines[i] = lines[i].split("\t")[0] + "\tnot-recognised\ta note"  # also covers the free third field
        p.write_text("\n".join(lines) + "\n")

    res, _ = _decide(drawn, "mine", "not-recognised", tweak)
    assert res["decision"] == "stop"
    assert res["counts"]["own"]["not-recognised"] == 1


@pytest.mark.parametrize("decoy_label", ["mine", "mine-different"])
def test_5_decision_instrument_failed_on_any_accepted_decoy(drawn, decoy_label):
    def tweak(p):
        lines = p.read_text().splitlines()
        i = next(k for k, ln in enumerate(lines) if drawn["key"]["items"][ln.split("\t")[0]]["source"] == "decoy")
        lines[i] = lines[i].split("\t")[0] + "\t" + decoy_label
        p.write_text("\n".join(lines) + "\n")

    res, _ = _decide(drawn, "mine", "not-recognised", tweak)
    assert res["decision"] == "instrument_failed"
    assert res["counts"]["decoys"]["labelled_mine"] == 1


def test_5_instrument_failed_takes_precedence_over_stop(drawn):
    def tweak(p):
        lines = p.read_text().splitlines()
        out = []
        for ln in lines:
            if drawn["key"]["items"][ln.split("\t")[0]]["source"] == "decoy":
                ln = ln.split("\t")[0] + "\tmine"
            out.append(ln)
        p.write_text("\n".join(out) + "\n")

    res, _ = _decide(drawn, "not-recognised", "not-recognised", tweak)
    assert res["decision"] == "instrument_failed"


# ---------------------------------------------------------------------------
# 5b. labels are final once the key has been seen (unblinded.json)
# ---------------------------------------------------------------------------


def _two_label_files(drawn):
    """Two complete labels files that decide differently (proceed vs stop)."""
    a = write_labels(drawn["tmp"] / "a.tsv", drawn["key"], lambda i, k: "mine" if k["source"] == "own" else "not-recognised")
    b = write_labels(drawn["tmp"] / "b.tsv", drawn["key"], lambda i, k: "not-recognised")
    return a, b


def test_5b_a_relabel_after_unblinding_is_refused_and_prints_no_key(drawn):
    a, b = _two_label_files(drawn)
    first = _unblind(drawn, a)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["decision"] == "proceed"
    before = (drawn["out"] / "unblinded.json").read_bytes()
    r = _unblind(drawn, b)
    _assert_refusal(r)
    sha = hashlib.sha256(a.read_bytes()).hexdigest()[:12]
    assert r.stderr.strip() == (
        f"model_switch_read: labels already fixed for this draw (sha256 {sha}\u2026); "
        "a re-label after unblinding is refused"
    )
    assert r.stdout.strip() == ""
    for v in drawn["key"]["items"].values():
        if v["source"] == "own":
            assert v["id"] not in r.stdout + r.stderr
    assert (drawn["out"] / "unblinded.json").read_bytes() == before  # nothing written


def test_5b_same_labels_again_reprints_the_recorded_result_and_writes_nothing(drawn):
    a, _ = _two_label_files(drawn)
    first = _unblind(drawn, a)
    assert first.returncode == 0, first.stderr
    f = drawn["out"] / "unblinded.json"
    before, mtime = f.read_bytes(), f.stat().st_mtime_ns
    again = _unblind(drawn, a)
    assert again.returncode == 0, again.stderr
    assert again.stdout == first.stdout
    assert f.read_bytes() == before and f.stat().st_mtime_ns == mtime
    # a byte-different file is a different sha, even with the same meaning
    a2 = drawn["tmp"] / "a2.tsv"
    a2.write_bytes(a.read_bytes() + b"\n")
    _assert_refusal(_unblind(drawn, a2))


def test_5b_unblinded_json_records_sha_decision_and_the_printed_result(drawn):
    a, _ = _two_label_files(drawn)
    first = _unblind(drawn, a)
    rec = json.loads((drawn["out"] / "unblinded.json").read_text())
    assert rec["labels_sha256"] == hashlib.sha256(a.read_bytes()).hexdigest()
    assert rec["decision"] == "proceed"
    assert rec["result"] == json.loads(first.stdout)


def test_5b_a_refused_unblind_writes_no_unblinded_json_and_labels_can_still_be_fixed(drawn):
    a, _ = _two_label_files(drawn)
    incomplete = drawn["tmp"] / "inc.tsv"
    incomplete.write_text("\n".join(a.read_text().splitlines()[1:]) + "\n")
    _assert_refusal(_unblind(drawn, incomplete))
    assert not (drawn["out"] / "unblinded.json").exists()
    ok = _unblind(drawn, a)
    assert ok.returncode == 0, ok.stderr
    assert (drawn["out"] / "unblinded.json").exists()


def test_5b_a_refusal_after_the_labels_were_read_also_writes_no_unblinded_json(drawn):
    a, _ = _two_label_files(drawn)
    cards = drawn["out"] / "cards.md"
    good = cards.read_bytes()
    cards.write_bytes(good + b"\n")
    _assert_refused_with_nothing_leaked(_unblind(drawn, a), drawn)
    assert not (drawn["out"] / "unblinded.json").exists()
    cards.write_bytes(good)
    assert _unblind(drawn, a).returncode == 0


@pytest.mark.parametrize("kind", ["garbage", "forged-decision-right-sha", "forged-wrong-sha"])
def test_5b_unblinded_json_cannot_be_precreated_to_fake_a_result(drawn, kind):
    # Reason: a file present before the first successful run was not written by the tool, so it is no
    # record of anything the key was shown for; honouring it would let a forged result stand in for the join.
    a, _ = _two_label_files(drawn)
    f = drawn["out"] / "unblinded.json"
    if kind == "garbage":
        f.write_text("not json")
    else:
        sha = hashlib.sha256(a.read_bytes()).hexdigest() if kind == "forged-decision-right-sha" else "0" * 64
        fake = {"labels_sha256": sha, "decision": "proceed", "result": {"decision": "proceed", "forged": True}}
        f.write_text(json.dumps(fake))
    before = f.read_bytes()
    r = _unblind(drawn, a)
    _assert_refused_with_nothing_leaked(r, drawn)
    assert "forged" not in r.stdout
    assert f.read_bytes() == before


def test_5b_the_record_is_created_exclusively_so_a_racing_file_is_never_overwritten(drawn, monkeypatch, capsys):
    # Two first runs can both pass the "no record yet" look; the create must then refuse for the loser.
    # The look is blinded here so that only the exclusive create ('x') stands between the file and an overwrite.
    a, _ = _two_label_files(drawn)
    f = drawn["out"] / "unblinded.json"
    f.write_text('{"raced": true}')
    mod = _load_tool()
    monkeypatch.setattr(mod.os.path, "lexists", lambda p: False)
    rc = mod.main(["unblind", "--dir", str(drawn["out"]), "--labels", str(a)])
    out = capsys.readouterr()
    assert rc == 1, (rc, out.err)
    assert out.out.strip() == ""
    assert f.read_text() == '{"raced": true}'


# ---------------------------------------------------------------------------
# 6. failure is loud
# ---------------------------------------------------------------------------


def _assert_loud(r, world):
    _assert_refusal(r)
    assert r.stdout.strip() == ""
    assert len(r.stderr.strip().splitlines()) == 1
    assert not world["out"].exists() or not any(world["out"].iterdir())


def test_6_missing_database(world):
    control_ok(world["db"], world["decoys"], world["tmp"])
    r = draw(world["tmp"] / "nope.db", world["out"], world["decoys"])
    _assert_loud(r, world)
    assert not (world["tmp"] / "nope.db").exists()  # a ro open must not create it


def test_6_unreadable_and_malformed_decoys(world):
    control_ok(world["db"], world["decoys"], world["tmp"])
    _assert_loud(draw(world["db"], world["out"], world["tmp"] / "none.json"), world)
    for body in ("{not json", "{}", '{"claim": "x"}', "[1,2,3]", '["a","b","c"]', ""):
        world["decoys"].write_text(body)
        _assert_loud(draw(world["db"], world["out"], world["decoys"]), world)


def test_6_zero_own_candidates(tmp_path):
    d = tmp_path / "g"
    d.mkdir()
    con = _new_db(d / "knowledge.db")
    _insert(con, "zz_ev", "evidence", "e", 1)
    _insert(con, "zz_ax_a", "axiom", "a", 0)
    con.commit()
    con.close()
    w = {"out": tmp_path / "out"}
    decoys = make_decoys(tmp_path / "decoys.json")
    con = sqlite3.connect(d / "knowledge.db")
    _insert(con, "zz_ok", "observation", "a real candidate", 2)
    con.commit()
    con.close()
    control_ok(d / "knowledge.db", decoys, tmp_path, "--allow-short")  # one candidate: fine
    con = sqlite3.connect(d / "knowledge.db")
    con.execute("delete from nodes where id = 'zz_ok'")
    con.commit()
    con.close()
    _assert_loud(draw(d / "knowledge.db", w["out"], decoys, "s", "--allow-short"), w)
    _assert_loud(draw(d / "knowledge.db", w["out"], decoys), w)


def _short_world(tmp_path):
    d = tmp_path / "g"
    d.mkdir()
    con = _new_db(d / "knowledge.db")
    for i in range(7):
        _insert(con, f"zz_n_{i}", "observation", f"zebra-claim {i}", 1 + i)
    con.commit()
    con.close()
    return d / "knowledge.db", make_decoys(tmp_path / "decoys.json"), tmp_path / "out"


def test_6_short_stratum_refused_without_allow_short(tmp_path):
    db, decoys, out = _short_world(tmp_path)
    control_ok(db, decoys, tmp_path, "--allow-short")
    r = draw(db, out, decoys)
    _assert_loud(r, {"out": out})


def test_6_allow_short_takes_the_stratum_as_it_is_prints_and_records_it(tmp_path):
    db, decoys, out = _short_world(tmp_path)
    r = draw(db, out, decoys, "s", "--allow-short")
    assert r.returncode == 0, r.stderr
    key = json.loads((out / "key.json").read_text())
    assert key["allow_short"] is True
    drawn = {s["name"]: s["drawn"] for s in key["strata"]}
    assert drawn["lessons and cornerstones"] == 0 and drawn["feelings"] == 0
    assert sum(1 for v in key["items"].values() if v["source"] == "own") == 7
    assert "short" in r.stdout.lower()
    assert any(s.get("short") for s in key["strata"])


def test_6_refuses_to_overwrite_either_file(drawn):
    for name in ("cards.md", "key.json"):
        other = drawn["tmp"] / ("out_" + name)
        other.mkdir()
        (other / name).write_text("precious")
        r = draw(drawn["db"], other, drawn["decoys"])
        _assert_refusal(r)
        assert (other / name).read_text() == "precious"
        assert sorted(p.name for p in other.iterdir()) == [name]
    again = draw(drawn["db"], drawn["out"], drawn["decoys"])
    _assert_refusal(again)


# ---------------------------------------------------------------------------
# the draw rules (must match the skill) against the independent oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", ["alpha", "beta-2", "7"])
def test_draw_matches_the_skill_oracle(world, seed):
    r = draw(world["db"], world["out"], world["decoys"], seed)
    assert r.returncode == 0, r.stderr
    key = json.loads((world["out"] / "key.json").read_text())
    expected, per, _, _ = oracle_draw(world["db"], seed)
    got = [v["id"] for v in key["items"].values() if v["source"] == "own"]
    assert sorted(got) == sorted(expected)
    assert [s["drawn"] for s in key["strata"]] == [len(p) for p in per]
    assert len(got) == sum(CAPS)


def test_drawn_nodes_are_never_seeds_evidence_persons_or_superseded(world):
    for seed in ("a", "b", "c", "d"):
        out = world["tmp"] / f"o-{seed}"
        assert draw(world["db"], out, world["decoys"], seed).returncode == 0
        key = json.loads((out / "key.json").read_text())
        ids = {v["id"] for v in key["items"].values() if v["source"] == "own"}
        _, _, _, seeds = oracle_draw(world["db"], seed)
        assert not ids & seeds
        assert seeds == {"zz_ax_a", "zz_ax_b", "zz_ax_c", "zz_df_a", "zz_df_b", "zz_gl_a"}
        assert not ids & {"zz_ev_1", "zz_ev_2", "zz_pn_1", "zz_pn_2", "zz_old_1", "zz_old_2"}


def test_same_seed_same_packet_different_seed_different_packet(world):
    outs = {}
    for name, seed in (("x1", "same"), ("x2", "same"), ("y", "other")):
        out = world["tmp"] / name
        assert draw(world["db"], out, world["decoys"], seed).returncode == 0
        outs[name] = ((out / "cards.md").read_text(), json.loads((out / "key.json").read_text()))
    assert outs["x1"][0] == outs["x2"][0]
    assert outs["x1"][1] == outs["x2"][1]
    assert outs["x1"][0] != outs["y"][0]


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", '"str"', "5", "null", b"{}"])
def test_source_class_malformed_or_non_object_metadata_is_not_introspective(raw):
    """#4616: NULL, malformed and non-object metadata yield None (never raise, never a class)."""
    spec = importlib.util.spec_from_file_location("msr_source_class_cell", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._source_class(raw) is None
    assert mod._source_class('{"source_class": "introspective"}') == "introspective"
