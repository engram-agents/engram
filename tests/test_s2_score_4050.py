"""Cells for tools/s2_score.py (engram-alpha#4050 S2) -- the scoring script for
the signed S2 pre-registration (draft 5, issue comment 6387983396; seed pinned to draft 4).

Contract-first: these cells were written before the tool. Fixtures are built by
RUNNING the real sheet builder (tools/s2_clause_sheet.py) on small invented
graphs (a copy of a real schema, rows inserted with raw SQL), then filling the
rendered sheets with invented labels. No real graph, sheet or key is ever
opened. Ids use the ``zz_`` / ``zy_`` / ``zp_`` / ``zo_`` prefixes, which are
not node-type prefixes. Expected values are computed here independently of the
tool (plain arithmetic), or are hand-computed constants with the arithmetic in
a comment.
"""

import atexit
import hashlib
import importlib.util
import itertools
import json
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TOOLS = Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"
_SCORE_PATH = _TOOLS / "s2_score.py"
_SHEET_PATH = _TOOLS / "s2_clause_sheet.py"

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_s2score_4050_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TEMPLATE: dict = {}
_COUNTER = itertools.count()
SIGNED_SEED = 3594015630
IN_WINDOW = "2026-10-15T00:00:00Z"


# --------------------------------------------------------------------------- loaders
def _load(path, label):
    name = f"{label}_{next(_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def score():
    return _load(_SCORE_PATH, "s2_score")


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


def _make_db(rows):
    path = Path(TEST_DIR) / f"db_{next(_COUNTER)}.db"
    shutil.copy(_template_db(), path)
    conn = sqlite3.connect(path)
    for r in rows:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at, quoted_text, interpretation,"
            " quote_type, is_current, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
            (r["id"], "observation_factual", f"claim text for {r['id']}", IN_WINDOW,
             f"quote text for {r['id']}", f"interpretation of {r['id']} (zz_7777 noted)",
             "hard_data", 1, json.dumps({"source_class": "external"})),
        )
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return path


# --------------------------------------------------------------------------- the fixture study
ROSTER = {"names": ["Quillon", "Marrowby"], "handles": ["quillon-bot", "marrowby-dev"]}
LABELS3 = ("CARRIED", "NOT", "UNCLEAR")


def _planted(prefix):
    return [
        dict(planted_id=f"{prefix}_9001", quote="planted quote one", interpretation="pi one",
             clauses=["pc one a", "pc one b"], kind="P1"),
        dict(planted_id=f"{prefix}_9002", quote="planted quote two", interpretation="pi two",
             clauses=["pc two a"], kind="P2"),
        dict(planted_id=f"{prefix}_9003", quote="planted quote three", interpretation="pi three",
             clauses=["pc three a", "pc three b", "pc three c"], kind="P3"),
        dict(planted_id=f"{prefix}_9004", quote="planted quote four", interpretation="pi four",
             clauses=["pc four a", "pc four b"], kind="P4"),
    ]


def _build_sheet(monkeypatch, prefix, planted_prefix, render_seed_hex=None):
    """Run the real sheet builder on an invented graph: 8 observations, 2 per
    similarity bin, 2 clauses each, plus the four planted items."""
    sheet_tool = _load(_SHEET_PATH, "s2_clause_sheet")
    rows = [dict(id=f"{prefix}_{2001 + i}") for i in range(8)]
    sims = {f"quote text for {r['id']}": 0.05 + 0.1 * i for i, r in enumerate(rows)}
    monkeypatch.setattr(sheet_tool.engram_anchoring, "quote_claim_similarity",
                        lambda q, c: (sims[q], None))
    db = _make_db(rows)
    out = Path(TEST_DIR) / f"sheet_{next(_COUNTER)}"
    assert sheet_tool.main(["select", "--db", str(db), "--out-dir", str(out),
                            "--build-commit", "abc1234"]) == 0
    claims = json.loads((out / "claims.json").read_text())
    splits = {c["node_id"]: [f"{c['node_id']} clause one", f"{c['node_id']} clause two"] for c in claims}
    (out / "splits.json").write_text(json.dumps(splits))
    (out / "planted.json").write_text(json.dumps(_planted(planted_prefix)))
    (out / "roster.json").write_text(json.dumps(ROSTER))
    if render_seed_hex:  # a different presentation order, drawn by the real tool
        key = json.loads((out / "key.json").read_text())
        key["seed_hex"] = render_seed_hex
        (out / "key.json").write_text(json.dumps(key))
    assert sheet_tool.main(["render", "--db", str(db), "--out-dir", str(out),
                            "--splits", str(out / "splits.json"), "--planted", str(out / "planted.json"),
                            "--scrub-roster", str(out / "roster.json"),
                            "--sheet-salt", "0123456789abcdef"]) == 0  # entry 15: render requires a salt
    if render_seed_hex:  # restore the signed seed in the key (the scorer pins it)
        key = json.loads((out / "key.json").read_text())
        key["seed_hex"] = sheet_tool.SEED_HEX
        (out / "key.json").write_text(json.dumps(key))
    key = json.loads((out / "key.json").read_text())
    real = sorted(i["node_id"] for i in key["items"])
    kinds = {v["id"]: v.get("kind") for v in key["render"]["items"].values()}
    pk = {p["planted_id"]: {"added_clause": {"P3": 3, "P4": 2}.get(p["kind"])} for p in _planted(planted_prefix)}
    (out / "planted-key.json").write_text(json.dumps(pk))
    return dict(out=out, key=key, real=real, kinds=kinds, planted_ids=[p["planted_id"] for p in _planted(planted_prefix)],
                num_of={v["id"]: int(n) for n, v in key["render"]["items"].items()},
                nclauses={v["id"]: v["clauses"] for v in key["render"]["items"].values()})


def _fill(sheet_text, answers, flags):
    """Return the sheet with Label/Flag lines filled from {(num, k): raw}."""
    out, num, k = [], None, None
    for line in sheet_text.split("\n"):
        m = re.match(r"^## Item (\d+)$", line)
        if m:
            num = int(m.group(1))
        m = re.match(r"^(\d+)\. ", line)
        if m:
            k = int(m.group(1))
        if line.startswith("   Label:"):
            line = f"   Label: {answers[(num, k)]}"
        elif line.startswith("   Flag (optional):"):
            line = f"   Flag (optional): {flags.get((num, k), '')}".rstrip() if flags.get((num, k)) else line
        out.append(line)
    return "\n".join(out)


def _default(sheet, rid, k):
    kind = sheet["kinds"].get(rid)
    if kind in ("P3", "P4"):
        return "NOT" if k == {"P3": 3, "P4": 2}[kind] else "CARRIED"
    return "CARRIED"


class Study:
    """Two invented sheets, two judges each, two passes; everything editable
    before ``write()``. Judge A is the same name on both sheets (as the signed
    rater columns have one shared rater)."""

    def __init__(self, monkeypatch, render_seed_hexes=(None, None)):
        self.sheets = [_build_sheet(monkeypatch, "zz", "zp", render_seed_hexes[0]),
                       _build_sheet(monkeypatch, "zy", "zo", render_seed_hexes[1])]
        self.names = ["sheet_one", "sheet_two"]
        self.judges = [("judgeA", "judgeB1"), ("judgeA", "judgeB2")]
        self.ans, self.flags, self.hooks, self.bad_hash = {}, {}, {}, set()
        self.dir = Path(TEST_DIR) / f"study_{next(_COUNTER)}"
        self.dir.mkdir()
        for si, sh in enumerate(self.sheets):
            for role in "AB":
                for p in ("pass1", "pass2"):
                    self.ans[(si, role, p)] = self._base(sh)
        self.resplit = [{"items_compared": 10, "clause_count_agreement": 0.9},
                        {"items_compared": 10, "clause_count_agreement": 0.7}]

    @staticmethod
    def _base(sh):
        d = {}
        for n, v in sh["key"]["render"]["items"].items():
            for k in range(1, v["clauses"] + 1):
                d[(int(n), k)] = _default(sh, v["id"], k)
        return d

    def clauses(self, si):
        return [(rid, k) for rid in self.sheets[si]["real"] for k in (1, 2)]

    def set(self, si, role, rid, k, raw, passes=("pass1", "pass2")):
        for p in passes:
            self.ans[(si, role, p)][(self.sheets[si]["num_of"][rid], k)] = raw

    def put(self, si, role, labels, passes=("pass1", "pass2")):
        """labels: one raw label per real clause, in clauses(si) order."""
        for (rid, k), raw in zip(self.clauses(si), labels):
            self.set(si, role, rid, k, raw, passes)

    def flag(self, si, role, rid, k, text, p="pass1"):
        self.flags.setdefault((si, role, p), {})[(self.sheets[si]["num_of"][rid], k)] = text

    def write(self):
        hashes, returns = {}, {}
        for si, sh in enumerate(self.sheets):
            for role in "AB":
                judge = self.judges[si][0 if role == "A" else 1]
                for p, fname in (("pass1", "sheet-pass1.md"), ("pass2", "sheet-pass2.md")):
                    text = _fill((sh["out"] / fname).read_text(), self.ans[(si, role, p)],
                                 self.flags.get((si, role, p), {}))
                    hook = self.hooks.get((si, role, p))
                    if hook:
                        text = hook(text)
                    path = self.dir / f"ret_{si}_{role}_{p}.md"
                    path.write_text(text)
                    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    if (si, role, p) in self.bad_hash:
                        sha = "0" * 64
                    hashes.setdefault(self.names[si], {}).setdefault(judge, {})[p] = sha
                    returns.setdefault(si, {}).setdefault(judge, {})[p] = str(path)
        (self.dir / "hashes.json").write_text(json.dumps(hashes))
        sheets_cfg = []
        for si, sh in enumerate(self.sheets):
            rp = self.dir / f"resplit_{si}.json"
            rp.write_text(json.dumps(self.resplit[si]))
            sheets_cfg.append(dict(
                name=self.names[si], key=str(sh["out"] / "key.json"),
                planted_key=str(sh["out"] / "planted-key.json"), resplit_agreement=str(rp),
                sheet_pass1=str(sh["out"] / "sheet-pass1.md"), sheet_pass2=str(sh["out"] / "sheet-pass2.md"),
                rater_A=self.judges[si][0], rater_B=self.judges[si][1], returns=returns[si]))
        (self.dir / "config.json").write_text(json.dumps(dict(sheets=sheets_cfg)))
        return self

    def run(self, score, out_name=None, extra=()):
        out = self.dir / (out_name or "out")
        rc = score.main(["--config", str(self.dir / "config.json"), "--hashes", str(self.dir / "hashes.json"),
                         "--out-dir", str(out), "--scorer-commit", "abc1234", *extra])
        return rc, out

    def report(self, score, out_name=None):
        rc, out = self.run(score, out_name)
        assert rc == 0
        return json.loads((out / "report.json").read_text(), parse_constant=_no_const)

    def ret(self, si, role, p):
        return self.dir / f"ret_{si}_{role}_{p}.md"


def _no_const(c):
    raise AssertionError(f"non-finite JSON constant {c}")


def _study(monkeypatch, **kw):
    return Study(monkeypatch, **kw)


# independent arithmetic -------------------------------------------------------
def _table(la, lb, pos):
    a = [x in pos for x in la]
    b = [x in pos for x in lb]
    return [[sum(x and y for x, y in zip(a, b)), sum(x and not y for x, y in zip(a, b))],
            [sum((not x) and y for x, y in zip(a, b)), sum((not x) and (not y) for x, y in zip(a, b))]]


def _po_kappa(t):
    (a, b), (c, d) = t
    n = a + b + c + d
    po = (a + d) / n
    pa, pb = (a + b) / n, (a + c) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return po, (po - pe) / (1 - pe)


def _bits(pos_idx, n=16, on="NOT", off="CARRIED"):
    return [on if i in pos_idx else off for i in range(n)]


# Known-answer tables (hand-computed in the comments of the cells below).
S1_A = _bits(range(0, 8))                       # A: NOT in clauses 0-7
S1_B = _bits(list(range(0, 6)) + [8])           # B: NOT in 0-5 and 8   => [[6,2],[1,7]]
S2_A = _bits(range(0, 4))                       # A: NOT in 0-3
S2_B = _bits([0, 1, 2, 4, 5])                   # B: NOT in 0-2, 4, 5   => [[3,1],[2,10]]


def _agree_study(monkeypatch):
    st = _study(monkeypatch)
    st.put(0, "A", S1_A)
    st.put(0, "B", S1_B)
    st.put(1, "A", S2_A)
    st.put(1, "B", S2_B)
    return st.write()


# =========================================================================== known answers (pure functions)
def test_k1_cohen_ac1_pabak_known_answer(score):
    # Hand-computed on the table [[18, 8], [0, 14]] (rows = column-A positive/negative,
    # cols = column-B positive/negative), n = 40:
    #   po   = (18 + 14) / 40 = 0.8
    #   pA   = (18 + 8) / 40 = 0.65 ; pB = (18 + 0) / 40 = 0.45
    #   pe   = 0.65*0.45 + 0.35*0.55 = 0.2925 + 0.1925 = 0.485
    #   kappa= (0.8 - 0.485) / (1 - 0.485) = 0.315 / 0.515 = 0.61165...   (the signed text: 0.6117)
    #   AC1  : pi = (0.65 + 0.45) / 2 = 0.55 ; pe = 2*0.55*0.45 = 0.495 ;
    #          AC1 = (0.8 - 0.495) / (1 - 0.495) = 0.305 / 0.505 = 0.60396...
    #   PABAK= 2*0.8 - 1 = 0.6
    t = (18, 8, 0, 14)
    assert score.agreement(t)[0] == pytest.approx(0.8)
    assert score.cohen_kappa(t)[0] == pytest.approx(0.315 / 0.515)
    assert round(score.cohen_kappa(t)[0], 4) == 0.6117
    assert score.gwet_ac1(t)[0] == pytest.approx(0.305 / 0.505)
    assert score.pabak(t)[0] == pytest.approx(0.6)


def test_k2_signed_text_pooled_column_swap_known_answer(score):
    # The signed change-log entry 13: each sheet [[18,8],[0,14]] pooled with the
    # columns kept = [[36,16],[0,28]] -> kappa 0.6117 ; with sheet 2's columns swapped
    # (its table transposed) the pool is [[36,8],[8,28]]... kept as the signed numbers:
    # pooled with columns fixed:  po=64/80=.8, pA=52/80=.65, pB=36/80=.45 -> same 0.6117.
    assert round(score.cohen_kappa((36, 16, 0, 28))[0], 4) == 0.6117
    # Swapping sheet 2's columns transposes its table: [[18,0],[8,14]] ; pooled = [[36,8],[8,28]]
    #   po=64/80=.8 ; pA=pB=44/80=.55 ; pe=.55*.55+.45*.45=.3025+.2025=.505 ;
    #   kappa=(.8-.505)/.495=0.5960...  (the signed text: 0.5960)
    assert round(score.cohen_kappa((36, 8, 8, 28))[0], 4) == 0.5960


def test_k3_spearman_with_ties_known_answer(score):
    # x = [1, 2, 2, 3] -> average ranks [1, 2.5, 2.5, 4]
    # y = [1, 3, 2, 2] -> average ranks [1, 4, 2.5, 2.5]
    # both rank means 2.5 ; dx = [-1.5, 0, 0, 1.5] ; dy = [-1.5, 1.5, 0, 0]
    # cov = 2.25 ; varx = vary = 4.5 ; rho = 2.25 / 4.5 = 0.5
    assert score.spearman([1, 2, 2, 3], [1, 3, 2, 2])[0] == pytest.approx(0.5)
    assert score.spearman([1, 2, 3], [3, 2, 1])[0] == pytest.approx(-1.0)


def test_k4_spearman_constant_is_undefined_with_reason(score):
    v, why = score.spearman([1, 2, 3], [5, 5, 5])
    assert v is None and why
    v, why = score.spearman([1], [1])
    assert v is None and why


# =========================================================================== contract 12: degenerate tables
@pytest.mark.parametrize("t", [(5, 0, 0, 0), (0, 0, 0, 7), (0, 0, 0, 0)])
def test_c12_degenerate_kappa_is_undefined_not_a_number(score, t):
    v, why = score.cohen_kappa(t)
    assert v is None and isinstance(why, str) and why


def test_c12_both_raters_constant_in_a_full_run(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.put(1, "A", ["CARRIED"] * 16)
    st.put(1, "B", ["CARRIED"] * 16)
    st.write()
    rep = st.report(score)  # report() parses with a hook that rejects NaN/Infinity
    k = rep["measures"]["primary"]["sheets"]["sheet_two"]["kappa"]
    assert k["value"] is None and k["undefined_reason"]
    assert rep["measures"]["primary"]["sheets"]["sheet_two"]["agreement"]["value"] == 1.0


# =========================================================================== contract 1: hash first
def test_c1_hash_mismatch_refuses_naming_file_and_scores_nothing(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    st.bad_hash.add((1, "B", "pass2"))
    st.write()
    rc, out = st.run(score)
    err = capsys.readouterr().err
    assert rc == 2
    assert str(st.ret(1, "B", "pass2")) in err
    assert not (out / "report.json").exists() and not (out / "report.md").exists()


def test_c1_all_mismatches_are_named(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    st.bad_hash.update({(0, "A", "pass1"), (1, "A", "pass2")})
    st.write()
    rc, _ = st.run(score)
    err = capsys.readouterr().err
    assert rc == 2 and str(st.ret(0, "A", "pass1")) in err and str(st.ret(1, "A", "pass2")) in err


def test_c1_missing_posted_hash_refuses(monkeypatch, score):
    st = _agree_study(monkeypatch)
    h = json.loads((st.dir / "hashes.json").read_text())
    del h["sheet_two"]["judgeB2"]["pass1"]
    (st.dir / "hashes.json").write_text(json.dumps(h))
    rc, _ = st.run(score)
    assert rc == 2


def test_c1_sheet_not_matching_key_hash_refuses(monkeypatch, score):
    st = _agree_study(monkeypatch)
    p = st.sheets[0]["out"] / "sheet-pass1.md"
    # the sheet file is the reference for clause text; it must be the one the key hashed
    p.chmod(0o644)
    p.write_text(p.read_text() + "\n")
    rc, _ = st.run(score)
    assert rc == 2


# =========================================================================== contract 2: structure
def _drop_item(num):
    def hook(text):
        parts = re.split(r"(?m)^(?=## Item \d+$)", text)
        return "".join(p for p in parts if not p.startswith(f"## Item {num}\n"))
    return hook


def test_c2_missing_item_refuses_naming_item(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    st.hooks[(0, "A", "pass1")] = _drop_item(3)
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and "item 3" in capsys.readouterr().err


def test_c2_extra_item_refuses_naming_item(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    st.hooks[(1, "B", "pass1")] = lambda t: t + "\n## Item 99\n\n**Clauses:**\n\n1. x\n   Label: CARRIED\n"
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and "item 99" in capsys.readouterr().err


def test_c2_missing_clause_refuses_naming_item(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    n = st.sheets[0]["num_of"][st.sheets[0]["real"][2]]

    def hook(text):
        pat = re.compile(rf"(## Item {n}\n.*?)(2\. [^\n]*\n   Label: [^\n]*\n   Flag \(optional\):[^\n]*\n\n)", re.S)
        return pat.sub(lambda m: m.group(1), text, count=1)
    st.hooks[(0, "A", "pass1")] = hook
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and f"item {n}" in capsys.readouterr().err


def _append_clause(num):
    def hook(text):
        parts = re.split(r"(?m)^(?=## Item \d+$)", text)
        return "".join(p + "3. sneaked in\n   Label: NOT\n   Flag (optional): \n\n"
                       if p.startswith(f"## Item {num}\n") else p for p in parts)
    return hook


def test_c2_extra_clause_refuses_naming_item(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    n = st.sheets[0]["num_of"][st.sheets[0]["real"][0]]
    st.hooks[(0, "B", "pass1")] = _append_clause(n)
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and f"item {n}" in capsys.readouterr().err


def test_c2_changed_clause_text_refuses_naming_item_and_clause(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    rid = st.sheets[1]["real"][4]
    n = st.sheets[1]["num_of"][rid]
    # node ids in clause text are masked on the sheet ("[id] clause two"), so edit within the item block
    st.hooks[(1, "A", "pass2")] = lambda t: "".join(
        b.replace("clause two", "clause TWO changed") if b.startswith(f"## Item {n}\n") else b
        for b in re.split(r"(?m)^(?=## Item \d+$)", t))
    st.write()
    rc, _ = st.run(score)
    err = capsys.readouterr().err
    assert rc == 2 and f"item {n}, clause 2" in err


def test_c2_missing_label_line_refuses(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    n = st.sheets[0]["num_of"][st.sheets[0]["real"][1]]
    st.hooks[(0, "A", "pass1")] = lambda t: re.sub(
        rf"(## Item {n}\n.*?1\. [^\n]*\n)   Label:[^\n]*\n", r"\1", t, count=1, flags=re.S)
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and f"item {n}, clause 1" in capsys.readouterr().err


def test_c2_free_text_shapes_do_not_drop_or_shift_an_answer(monkeypatch, score):
    """Lesson from #4061: a parser must not silently drop an answer. Vary the
    free-text shape: a Flag starting '(a)', a lower-case label, extra spaces,
    a stray blank line, a multi-line flag."""
    st = _study(monkeypatch)
    st.put(0, "A", S1_A, passes=("pass1",))
    st.put(0, "B", S1_B, passes=("pass1",))
    st.put(1, "A", S2_A, passes=("pass1",))
    st.put(1, "B", S2_B, passes=("pass1",))
    r0, r1 = st.sheets[0]["real"][0], st.sheets[0]["real"][3]
    st.set(0, "A", r0, 1, "  not  ", passes=("pass1",))       # lower case, padded
    st.set(0, "A", r0, 2, "Not", passes=("pass1",))
    st.flag(0, "A", r0, 1, "(a) this split looks wrong; see (b)")
    st.flag(0, "A", r1, 2, "line one of a flag\nline two, starting here\n2. looks like a clause number")

    def stray(text):
        return text.replace("\n\n## Item 2", "\n\n\n\n## Item 2", 1).replace("   Label: ", "   Label:   ", 1)
    st.hooks[(0, "A", "pass1")] = stray
    st.write()
    rep = st.report(score)
    # labels survived: item r0 clauses 1 and 2 are the first two real clauses and both NOT in S1_A
    assert rep["measures"]["primary"]["sheets"]["sheet_one"]["table"] == _table(S1_A, S1_B, {"NOT"})
    assert rep["secondaries"]["per_judge"]["sheet_one"]["judgeA"]["flag_count"]["pass1"] == 2


def test_c2_structure_checked_for_pass2_too(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    st.hooks[(1, "A", "pass2")] = _drop_item(1)
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and "item 1" in capsys.readouterr().err


# =========================================================================== contract 3: labels
def test_c3_label_case_and_spacing_accepted(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.set(0, "A", st.sheets[0]["real"][7], 2, "  carried ", passes=("pass1",))     # was CARRIED anyway
    st.set(0, "B", st.sheets[0]["real"][0], 1, "nOt", passes=("pass1",))
    st.write()
    rep = st.report(score)
    assert rep["measures"]["primary"]["sheets"]["sheet_one"]["table"] == _table(S1_A, S1_B, {"NOT"})


def test_c3_blank_label_is_unclear_and_counted_per_judge_per_pass(monkeypatch, score):
    st = _agree_study(monkeypatch)
    a, b = st.sheets[0]["real"][7], st.sheets[0]["real"][6]
    st.set(0, "A", a, 2, "", passes=("pass1",))
    st.set(0, "A", b, 1, "", passes=("pass1", "pass2"))
    st.set(0, "B", b, 2, "", passes=("pass2",))
    st.write()
    rep = st.report(score)
    dev = rep["protocol_deviations"]["sheet_one"]
    assert dev["judgeA"]["blank_labels"] == {"pass1": 2, "pass2": 1}
    assert dev["judgeB1"]["blank_labels"] == {"pass1": 0, "pass2": 1}
    # the blank was CARRIED-by-default elsewhere; scored UNCLEAR => counted in the marginals
    assert rep["marginals"]["sheet_one"]["judgeA"]["three_way"]["UNCLEAR"] == 2


def test_c3_other_token_refuses_whole_run_listing_every_bad_cell(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    r = st.sheets[0]["real"]
    st.set(0, "A", r[0], 1, "MAYBE", passes=("pass1",))
    st.set(0, "A", r[3], 2, "NOT?", passes=("pass1",))
    st.set(1, "B", st.sheets[1]["real"][5], 1, "carried-ish", passes=("pass2",))
    st.write()
    rc, out = st.run(score)
    err = capsys.readouterr().err
    assert rc == 2
    n0, n3 = st.sheets[0]["num_of"][r[0]], st.sheets[0]["num_of"][r[3]]
    n5 = st.sheets[1]["num_of"][st.sheets[1]["real"][5]]
    for needle in (f"item {n0}, clause 1", f"item {n3}, clause 2", f"item {n5}, clause 1",
                   "MAYBE", "NOT?", str(st.ret(0, "A", "pass1")), str(st.ret(1, "B", "pass2"))):
        assert needle in err
    assert not (out / "report.json").exists()


def test_c3_pass1_refuses_the_pass2_only_label(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)
    st.set(0, "A", st.sheets[0]["real"][0], 1, "NOT, STATED IN INTERPRETATION", passes=("pass1",))
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and "STATED IN INTERPRETATION" in capsys.readouterr().err


def test_c3_pass2_accepts_the_stated_label_case_and_spacing_insensitive(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.set(0, "A", st.sheets[0]["real"][0], 1, "not,  stated   in interpretation", passes=("pass2",))
    st.write()
    rep = st.report(score)
    assert rep["protocol_deviations"]["sheet_one"]["judgeA"]["pass2_deviations"] == []


# =========================================================================== contract 4: pass 1 is final
def test_c4_pass2_changes_reported_and_pass1_used(monkeypatch, score):
    st = _agree_study(monkeypatch)
    r = st.sheets[0]["real"]
    # A: a CARRIED -> NOT (clause 8, was CARRIED) and a NOT -> CARRIED (clause 0): two deviations.
    st.set(0, "A", r[4], 1, "NOT", passes=("pass2",))
    st.set(0, "A", r[0], 1, "CARRIED", passes=("pass2",))
    # B: only the allowed change NOT -> NOT, STATED IN INTERPRETATION: no deviation.
    st.set(0, "B", r[0], 2, "NOT, STATED IN INTERPRETATION", passes=("pass2",))
    st.write()
    rep = st.report(score)
    devs = rep["protocol_deviations"]["sheet_one"]
    assert len(devs["judgeA"]["pass2_deviations"]) == 2
    assert devs["judgeB1"]["pass2_deviations"] == []
    d0 = {(d["item"], d["clause"]) for d in devs["judgeA"]["pass2_deviations"]}
    assert d0 == {(st.sheets[0]["num_of"][r[4]], 1), (st.sheets[0]["num_of"][r[0]], 1)}
    assert rep["measures"]["primary"]["sheets"]["sheet_one"]["table"] == _table(S1_A, S1_B, {"NOT"})


def test_c4_pass2_unclear_to_carried_is_a_deviation(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.set(0, "A", st.sheets[0]["real"][7], 1, "UNCLEAR", passes=("pass1",))
    st.set(0, "A", st.sheets[0]["real"][7], 1, "CARRIED", passes=("pass2",))
    st.write()
    rep = st.report(score)
    assert len(rep["protocol_deviations"]["sheet_one"]["judgeA"]["pass2_deviations"]) == 1


# =========================================================================== contract 5: planted controls
def _planted_rec(rep, sheet, judge):
    return rep["planted_controls"][sheet]["judges"][judge]


def test_c5_planted_reported_before_measures(monkeypatch, score):
    rep = _agree_study(monkeypatch).report(score)
    keys = list(rep)
    assert keys.index("planted_controls") < keys.index("measures") < keys.index("secondaries")


def test_c5_clean_judges_are_pooled_and_planted_excluded_from_tables(monkeypatch, score):
    rep = _agree_study(monkeypatch).report(score)
    rec = _planted_rec(rep, "sheet_one", "judgeA")
    assert rec["pooled"] is True and rec["misses_both"] is False
    assert rec["p1_p2_labelled_not"] is False and rec["labels_all_four_not"] is False
    t = rep["measures"]["primary"]["sheets"]["sheet_one"]["table"]
    assert sum(map(sum, t)) == 16  # real clauses only; the planted items' NOTs are not in it


def test_c5a_missing_one_of_p3_p4_is_not_exclusion(monkeypatch, score):
    st = _agree_study(monkeypatch)
    p3 = st.sheets[0]["planted_ids"][2]
    st.set(0, "A", p3, 3, "CARRIED", passes=("pass1",))
    st.write()
    rec = _planted_rec(st.report(score), "sheet_one", "judgeA")
    assert rec["missed_p3"] is True and rec["missed_p4"] is False
    assert rec["misses_both"] is False and rec["pooled"] is True


@pytest.mark.parametrize("raw3,raw4", [("CARRIED", "CARRIED"), ("UNCLEAR", "CARRIED"), ("CARRIED", "UNCLEAR")])
def test_c5a_missing_both_excludes_and_unclear_counts_as_missed(monkeypatch, score, raw3, raw4):
    st = _agree_study(monkeypatch)
    pl = st.sheets[0]["planted_ids"]
    st.set(0, "B", pl[2], 3, raw3, passes=("pass1",))
    st.set(0, "B", pl[3], 2, raw4, passes=("pass1",))
    st.write()
    rep = st.report(score)
    rec = _planted_rec(rep, "sheet_one", "judgeB1")
    assert rec["misses_both"] is True and rec["pooled"] is False
    # a sheet with fewer than two pooled judges reads "not met" and its kappa is undefined
    assert rep["planted_controls"]["sheet_one"]["pooled_judges"] == 1
    assert rep["verdict"]["met"] is False and "sheet_one" in rep["verdict"]["short_sheets"]
    sheet = rep["measures"]["primary"]["sheets"]["sheet_one"]
    assert sheet["kappa"]["value"] is None and sheet["kappa"]["undefined_reason"]
    assert rep["measures"]["primary"]["pooled"]["kappa"]["value"] is None


def test_c5b_any_nonexcluded_p3_miss_uses_pass1_not_pass2(monkeypatch, score):
    st = _agree_study(monkeypatch)
    pl = st.sheets[0]["planted_ids"]
    # in pass 2 the judge would hit them, but pass 1 is final
    st.set(0, "A", pl[2], 3, "CARRIED", passes=("pass1",))
    st.set(0, "A", pl[3], 2, "CARRIED", passes=("pass1",))
    st.write()
    assert _planted_rec(st.report(score), "sheet_one", "judgeA")["misses_both"] is True


def test_c5b_a_not_on_p1_or_p2_excludes(monkeypatch, score):
    for which, k in ((0, 2), (1, 1)):
        st = _agree_study(monkeypatch)
        st.set(1, "A", st.sheets[1]["planted_ids"][which], k, "NOT", passes=("pass1",))
        st.write()
        rec = _planted_rec(st.report(score), "sheet_two", "judgeA")
        assert rec["p1_p2_labelled_not"] is True and rec["pooled"] is False


def test_c5c_all_four_not_reported_separately_and_stated_subsumed(monkeypatch, score):
    st = _agree_study(monkeypatch)
    pl = st.sheets[0]["planted_ids"]
    st.set(0, "A", pl[0], 1, "NOT", passes=("pass1",))   # one NOT clause is enough under "any"
    st.set(0, "A", pl[1], 1, "NOT", passes=("pass1",))
    st.write()
    rep = st.report(score)
    rec = _planted_rec(rep, "sheet_one", "judgeA")
    assert rec["labels_all_four_not"] is True
    assert rec["p1_p2_labelled_not"] is True and rec["pooled"] is False
    assert "subsumed" in rep["planted_controls_note"].lower()
    assert "(b)" in rep["planted_controls_note"] and "(c)" in rep["planted_controls_note"]


def test_c5_labels_all_four_not_is_false_when_three_items_are_not(monkeypatch, score):
    st = _agree_study(monkeypatch)
    rec = _planted_rec(st.report(score), "sheet_one", "judgeA")
    assert rec["labels_all_four_not"] is False  # only P3 and P4 carry NOT


def test_c5_pass2_planted_labels_do_not_count(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.set(0, "A", st.sheets[0]["planted_ids"][0], 1, "NOT", passes=("pass2",))
    st.write()
    assert _planted_rec(st.report(score), "sheet_one", "judgeA")["p1_p2_labelled_not"] is False


def test_c5_planted_key_must_match_the_sheet(monkeypatch, score):
    st = _agree_study(monkeypatch)
    p = st.sheets[0]["out"] / "planted-key.json"
    pk = json.loads(p.read_text())
    pk["zp_9003"]["added_clause"] = None  # a P3 with no added clause is not a control
    p.write_text(json.dumps(pk))
    rc, _ = st.run(score)
    assert rc == 2


# =========================================================================== contracts 6 and 7: the two collapses
def test_c6_primary_tables_known_answer_and_pooled_is_cell_sum(monkeypatch, score):
    rep = _agree_study(monkeypatch).report(score)
    prim = rep["measures"]["primary"]
    # sheet one: A NOT in 0-7, B NOT in 0-5 and 8 => [[6,2],[1,7]]
    #   n=16 po=13/16=.8125 pA=8/16=.5 pB=7/16=.4375 pe=.5*.4375+.5*.5625=.5 kappa=.3125/.5=0.625
    # sheet two: A NOT in 0-3, B NOT in 0-2,4,5 => [[3,1],[2,10]]
    #   n=16 po=13/16 pA=4/16=.25 pB=5/16=.3125 pe=.25*.3125+.75*.6875=.59375 kappa=.21875/.40625=7/13
    # pooled = cell by cell sum [[9,3],[3,17]]: n=32 po=26/32=.8125 pA=pB=12/32=.375
    #   pe=.140625+.390625=.53125 kappa=.28125/.46875=0.6 exactly
    assert prim["sheets"]["sheet_one"]["table"] == [[6, 2], [1, 7]]
    assert prim["sheets"]["sheet_two"]["table"] == [[3, 1], [2, 10]]
    assert prim["pooled"]["table"] == [[9, 3], [3, 17]]
    assert prim["sheets"]["sheet_one"]["kappa"]["value"] == pytest.approx(0.625)
    assert prim["sheets"]["sheet_two"]["kappa"]["value"] == pytest.approx(7 / 13)
    assert prim["pooled"]["kappa"]["value"] == pytest.approx(0.6)
    assert prim["pooled"]["agreement"]["value"] == pytest.approx(0.8125)
    assert prim["sheets"]["sheet_one"]["agreement"]["value"] == pytest.approx(0.8125)


def test_c6_unclear_goes_with_carried_in_the_primary(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.set(0, "B", st.sheets[0]["real"][0], 1, "UNCLEAR")   # was NOT
    st.write()
    rep = st.report(score)
    la = S1_A
    lb = list(S1_B)
    lb[0] = "UNCLEAR"
    assert rep["measures"]["primary"]["sheets"]["sheet_one"]["table"] == _table(la, lb, {"NOT"})


def test_c6_columns_are_fixed_by_config_not_by_file_order(monkeypatch, score):
    """Swapping which judge the config names as column A transposes the table.
    Both sheets are swapped together (measure 3 needs the same rater_A on both)."""
    st = _study(monkeypatch)
    st.judges = [("judgeA", "judgeB1"), ("judgeA", "judgeB1")]
    st.put(0, "A", S1_A)
    st.put(0, "B", S1_B)
    st.put(1, "A", S2_A)
    st.put(1, "B", S2_B)
    st.write()
    cfg = json.loads((st.dir / "config.json").read_text())
    for s in cfg["sheets"]:
        s["rater_A"], s["rater_B"] = s["rater_B"], s["rater_A"]
    (st.dir / "config.json").write_text(json.dumps(cfg))
    rep = st.report(score)
    assert rep["measures"]["primary"]["sheets"]["sheet_one"]["table"] == _table(S1_B, S1_A, {"NOT"})
    assert rep["measures"]["primary"]["sheets"]["sheet_two"]["table"] == _table(S2_B, S2_A, {"NOT"})
    # and the pooled table is the sum under those fixed columns
    # sheet one transposed [[6,1],[2,7]] + sheet two transposed [[3,2],[1,10]]
    assert rep["measures"]["primary"]["pooled"]["table"] == [[6 + 3, 1 + 2], [2 + 1, 7 + 10]]


def test_c6_config_rejects_same_judge_in_both_columns(monkeypatch, score):
    st = _agree_study(monkeypatch)
    cfg = json.loads((st.dir / "config.json").read_text())
    cfg["sheets"][0]["rater_B"] = cfg["sheets"][0]["rater_A"]
    (st.dir / "config.json").write_text(json.dumps(cfg))
    assert st.run(score)[0] == 2


def test_c6_rater_a_differing_across_sheets_refuses_before_any_return_is_read(monkeypatch, score, capsys):
    """Measure 3: column A is the same judge on both sheets. Refuse otherwise, naming both values."""
    st = _agree_study(monkeypatch)
    cfg = json.loads((st.dir / "config.json").read_text())
    cfg["sheets"][1]["rater_A"] = "judgeX"
    cfg["sheets"][1]["returns"]["judgeX"] = cfg["sheets"][1]["returns"].pop("judgeA")
    (st.dir / "config.json").write_text(json.dumps(cfg))
    # no return file exists: a refusal for the rater columns must come before any is read
    for si in (0, 1):
        for role in "AB":
            for p in ("pass1", "pass2"):
                st.ret(si, role, p).unlink()
    rc, out = st.run(score)
    assert rc == 2
    err = capsys.readouterr().err
    assert "judgeA" in err and "judgeX" in err and "measure 3" in err
    assert not (out / "report.json").exists() and not (out / "report.md").exists()


def test_c6_swapping_only_sheet_two_columns_refuses(monkeypatch, score, capsys):
    """The change-log entry 13 case: shared judge in column A on sheet 1, column B on sheet 2."""
    st = _agree_study(monkeypatch)
    cfg = json.loads((st.dir / "config.json").read_text())
    s1 = cfg["sheets"][1]
    s1["rater_A"], s1["rater_B"] = s1["rater_B"], s1["rater_A"]
    (st.dir / "config.json").write_text(json.dumps(cfg))
    rc, out = st.run(score)
    assert rc == 2
    err = capsys.readouterr().err
    assert "judgeA" in err and "judgeB2" in err and "measure 3" in err
    assert not (out / "report.json").exists()


def test_c6_standard_config_same_rater_a_still_scores(monkeypatch, score):
    st = _agree_study(monkeypatch)
    cfg = json.loads((st.dir / "config.json").read_text())
    assert cfg["sheets"][0]["rater_A"] == cfg["sheets"][1]["rater_A"]
    assert cfg["sheets"][0]["rater_B"] != cfg["sheets"][1]["rater_B"]  # column B may differ
    rc, out = st.run(score)
    assert rc == 0 and (out / "report.json").exists()


def test_c7_second_collapse_puts_unclear_with_not(monkeypatch, score):
    st = _study(monkeypatch)
    la = ["NOT"] * 4 + ["UNCLEAR"] * 4 + ["CARRIED"] * 8
    lb = ["NOT"] * 2 + ["UNCLEAR"] * 2 + ["NOT"] * 2 + ["CARRIED"] * 2 + ["UNCLEAR"] * 1 + ["CARRIED"] * 7
    st.put(0, "A", la)
    st.put(0, "B", lb)
    st.put(1, "A", S2_A)
    st.put(1, "B", S2_B)
    st.write()
    rep = st.report(score)
    t1 = _table(la, lb, {"NOT"})
    t2 = _table(la, lb, {"NOT", "UNCLEAR"})
    assert t1 != t2
    assert rep["measures"]["primary"]["sheets"]["sheet_one"]["table"] == t1
    s2 = rep["measures"]["second_collapse"]["sheets"]["sheet_one"]
    assert s2["table"] == t2
    po, kap = _po_kappa(t2)
    assert s2["agreement"]["value"] == pytest.approx(po) and s2["kappa"]["value"] == pytest.approx(kap)
    assert rep["measures"]["second_collapse"]["pooled"]["table"] == [
        [t2[0][0] + 3, t2[0][1] + 1], [t2[1][0] + 2, t2[1][1] + 10]]


# =========================================================================== contract 8: with every kappa
def test_c8_marginals_ac1_pabak(monkeypatch, score):
    st = _study(monkeypatch)
    la = ["NOT"] * 4 + ["UNCLEAR"] * 4 + ["CARRIED"] * 8
    st.put(0, "A", la)
    st.put(0, "B", S1_B)
    st.put(1, "A", S2_A)
    st.put(1, "B", S2_B)
    st.write()
    rep = st.report(score)
    m = rep["marginals"]["sheet_one"]["judgeA"]
    assert m["three_way"] == {"CARRIED": 8, "NOT": 4, "UNCLEAR": 4}
    assert m["primary_collapse"] == {"NOT": 4, "not_NOT": 12}
    assert m["second_collapse"] == {"NOT_or_UNCLEAR": 8, "CARRIED": 8}
    t = _table(la, S1_B, {"NOT"})
    (a, b), (c, d) = t
    po = (a + d) / 16
    pi = ((a + b) / 16 + (a + c) / 16) / 2
    ac1 = (po - 2 * pi * (1 - pi)) / (1 - 2 * pi * (1 - pi))
    s = rep["measures"]["primary"]["sheets"]["sheet_one"]
    assert s["ac1"]["value"] == pytest.approx(ac1)
    assert s["pabak"]["value"] == pytest.approx(2 * po - 1)


def test_c8_bootstrap_clusters_items_and_pools_within_each_sheet_independently(score):
    # sheet 1: 3 items of one clause each, all both-positive; sheet 2: 5 items, all both-negative.
    sheets = [[(1, 0, 0, 0)] * 3, [(0, 0, 0, 1)] * 5]
    rng = random.Random(1)
    for _ in range(50):
        per_sheet, pooled = score.resample_tables(rng, sheets)
        # each sheet draws as many observations as it has, from itself only
        assert per_sheet[0] == (3, 0, 0, 0) and per_sheet[1] == (0, 0, 0, 5)
        assert pooled == (3, 0, 0, 5)


def test_c8_bootstrap_resamples_observations_not_clauses(score):
    # two items of 3 clauses each: one all-positive, one all-negative. Resampling clauses
    # could give any n11 0..6 ; resampling observations gives only multiples of 3.
    sheets = [[(3, 0, 0, 0), (0, 0, 0, 3)], [(3, 0, 0, 0), (0, 0, 0, 3)]]
    rng = random.Random(7)
    seen = set()
    for _ in range(200):
        per_sheet, _pooled = score.resample_tables(rng, sheets)
        for t in per_sheet:
            assert t[0] % 3 == 0 and t[3] % 3 == 0 and t[0] + t[3] == 6
            seen.add(t[0])
    assert seen == {0, 3, 6}


def test_c8_resample_is_a_pure_function_of_the_seeded_rng(score):
    sheets = [[(1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0)], [(0, 0, 0, 1), (1, 0, 0, 0)]]
    a = [score.resample_tables(random.Random(SIGNED_SEED), sheets) for _ in range(3)]
    assert a[0] == a[1] == a[2]


def test_c8_percentile_interval_known_answer(score):
    vals = [float(i) for i in range(1, 101)]  # 1..100
    # linear interpolation at q*(m-1): q=.025 -> position 2.475 between 3 and 4 -> 3.475 ;
    # q=.975 -> position 96.525 between 97 and 98 -> 97.525
    lo, hi = score.percentile_interval(vals)
    assert lo == pytest.approx(3.475) and hi == pytest.approx(97.525)


def test_c8_run_records_seed_resamples_and_is_byte_identical_on_rerun(monkeypatch, score):
    st = _agree_study(monkeypatch)
    rc1, o1 = st.run(score, "out1")
    rc2, o2 = st.run(score, "out2")
    assert rc1 == rc2 == 0
    assert (o1 / "report.json").read_bytes() == (o2 / "report.json").read_bytes()
    assert (o1 / "report.md").read_bytes() == (o2 / "report.md").read_bytes()
    rep = json.loads((o1 / "report.json").read_text())
    assert rep["rng"]["seed"] == SIGNED_SEED and rep["rng"]["resamples"] == 2000
    ci = rep["measures"]["primary"]["pooled"]["ci"]["kappa"]
    assert ci["resamples"] == 2000 and ci["undefined_resamples"] + ci["defined_resamples"] == 2000
    assert ci["lo"] <= ci["hi"]
    # the point estimate sits beside the interval
    assert ci["point"] == pytest.approx(rep["measures"]["primary"]["pooled"]["kappa"]["value"])
    assert set(rep["measures"]["primary"]["pooled"]["ci"]) >= {"agreement", "kappa", "ac1", "pabak"}


def test_c8_a_different_seed_draws_differently(score):
    sheets = [[(1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)] * 2] * 2
    a = [score.resample_tables(random.Random(SIGNED_SEED), sheets)[1] for _ in range(1)]
    b = [score.resample_tables(random.Random(SIGNED_SEED + 1), sheets)[1] for _ in range(1)]
    ra, rb = random.Random(SIGNED_SEED), random.Random(SIGNED_SEED + 1)
    da = [score.resample_tables(ra, sheets)[1] for _ in range(20)]
    db = [score.resample_tables(rb, sheets)[1] for _ in range(20)]
    assert a != b or da != db


def test_c8_undefined_resamples_are_counted_and_interval_taken_over_defined(monkeypatch, score):
    st = _study(monkeypatch)
    # exactly one item has a (shared) NOT on each sheet; a resample without it has constant raters
    for si in (0, 1):
        both = ["NOT", "NOT"] + ["CARRIED"] * 14
        st.put(si, "A", both)
        st.put(si, "B", both)
    st.write()
    rep = st.report(score)
    for ci in (rep["measures"]["primary"]["sheets"]["sheet_one"]["ci"]["kappa"],
               rep["measures"]["primary"]["pooled"]["ci"]["kappa"]):
        assert ci["undefined_resamples"] > 0
        assert ci["defined_resamples"] == 2000 - ci["undefined_resamples"]
        assert ci["lo"] is not None and ci["hi"] is not None


# =========================================================================== contract 9: the criterion
def _crit(score, pooled, s1, s2):
    return score.evaluate_criterion(pooled, [s1, s2], ["sheet_one", "sheet_two"])


def test_c9_met_exactly_on_the_thresholds(score):
    # pooled [[9,3],[3,17]]: kappa 0.6 exactly, agreement 26/32; sheet tables with kappa >= .5
    out = _crit(score, (9, 3, 3, 17), (6, 2, 1, 7), (3, 1, 2, 10))
    assert out["met"] is True and out["short_sheets"] == []


def test_c9_pooled_kappa_just_below_point_six_is_not_met(score):
    out = _crit(score, (9, 3, 4, 16), (6, 2, 1, 7), (3, 1, 2, 10))   # kappa ~0.541
    assert out["met"] is False and "judges do not agree well enough" in out["verdict"]


def test_c9_failed_agreement_is_named_among_the_reasons(score):
    # po = 0.79 and kappa ~0.58: this table fails BOTH the kappa and the agreement clause, so it
    # only shows that the agreement clause is evaluated and named. The next cell isolates it.
    out = _crit(score, (40, 10, 11, 39), (6, 2, 1, 7), (3, 1, 2, 10))
    assert out["met"] is False and any("agreement" in r for r in out["reasons"])


def test_c9_agreement_clause_fails_alone(score):
    # pooled (a, b, c, d) = (399, 151, 51, 399), n = 1000. Rater one says yes on a+b = 550, rater
    # two on a+c = 450, so the marginals sit on opposite sides of 0.5 and pe = .55*.45 + .45*.55
    # = 0.495 (below 0.5). Then po = 798/1000 = 399/500 < 0.8, yet kappa = (.798-.495)/.505 = 3/5
    # exactly, so only the agreement clause fails.
    from fractions import Fraction
    a, b, c, d = 399, 151, 51, 399
    n = a + b + c + d
    po = Fraction(a + d, n)
    pe = Fraction((a + b) * (a + c) + (c + d) * (b + d), n * n)
    assert po == Fraction(399, 500) and pe == Fraction(99, 200)
    assert (po - pe) / (1 - pe) == Fraction(3, 5)
    out = _crit(score, (a, b, c, d), (6, 2, 1, 7), (3, 1, 2, 10))
    assert out["met"] is False and out["short_sheets"] == []
    assert len(out["reasons"]) == 1
    assert "agreement" in out["reasons"][0] and "kappa" not in out["reasons"][0]


def test_c9_sheet_kappa_below_half_names_that_sheet(score):
    out = _crit(score, (9, 3, 3, 17), (6, 2, 1, 7), (0, 4, 4, 8))
    assert out["met"] is False and out["short_sheets"] == ["sheet_two"]
    assert "judges do not agree well enough" in out["verdict"] and "sheet_two" in out["verdict"]


def test_c9_sheet_kappa_exactly_half_passes(score):
    # [[2,1],[1,2]]: po=4/6, pA=pB=.5, pe=.5, kappa=(.6667-.5)/.5=1/3 -> no. use [[3,1],[1,3]]: po=.75 pe=.5 kappa=.5
    out = _crit(score, (9, 3, 3, 17), (3, 1, 1, 3), (6, 2, 1, 7))
    assert out["short_sheets"] == []


def test_c9_undefined_sheet_kappa_counts_as_short(score):
    out = _crit(score, (9, 3, 3, 17), (6, 2, 1, 7), (0, 0, 0, 16))
    assert out["met"] is False and out["short_sheets"] == ["sheet_two"]


def test_c9_full_run_verdict_met_on_the_known_tables(monkeypatch, score):
    rep = _agree_study(monkeypatch).report(score)
    assert rep["verdict"]["met"] is True and rep["verdict"]["short_sheets"] == []


def test_c9_full_run_verdict_names_the_short_sheet(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.put(1, "B", _bits(range(4, 12)))   # [[0,4],[8,4]]: kappa negative on sheet two
    st.write()
    rep = st.report(score)
    assert rep["verdict"]["met"] is False
    assert "sheet_two" in rep["verdict"]["short_sheets"] and "sheet_one" not in rep["verdict"]["short_sheets"]
    assert "judges do not agree well enough" in rep["verdict"]["text"]


# =========================================================================== contract 10: secondaries
def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(x, y):
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy)


def _sec_study(monkeypatch):
    st = _study(monkeypatch)
    # sheet one: both judges NOT on clause 1 of items 0, 4, 5, 7 (by sorted id); A also NOT elsewhere
    a1 = ["CARRIED"] * 16
    b1 = ["CARRIED"] * 16
    for item in (0, 4, 5, 7):
        a1[2 * item] = b1[2 * item] = "NOT"
    a1[3] = "NOT"       # A alone: item 1 clause 2
    b1[2 * 6 + 1] = "NOT"  # B alone: item 6 clause 2
    a2 = ["CARRIED"] * 16
    b2 = ["CARRIED"] * 16
    a2[2 * 2] = b2[2 * 2] = "NOT"   # both NOT: item 2
    a2[2 * 2 + 1] = "NOT"           # A also NOT on item 2's second clause
    st.put(0, "A", a1)
    st.put(0, "B", b1)
    st.put(1, "A", a2)
    st.put(1, "B", b2)
    return st, (a1, b1, a2, b2)


def test_c10_share_of_observations_with_a_both_not_clause_per_bin(monkeypatch, score):
    st, _ = _sec_study(monkeypatch)
    rep = st.write().report(score)
    # bins by sorted id: items 0,1 -> B1 ; 2,3 -> B2 ; 4,5 -> B3 ; 6,7 -> B4
    sh1 = rep["secondaries"]["both_not_share_by_bin"]["sheet_one"]
    assert {b: (v["observations_with_both_not"], v["observations"]) for b, v in sh1.items()} == {
        "B1": (1, 2), "B2": (0, 2), "B3": (2, 2), "B4": (1, 2)}
    sh2 = rep["secondaries"]["both_not_share_by_bin"]["sheet_two"]
    assert {b: v["observations_with_both_not"] for b, v in sh2.items()} == {"B1": 0, "B2": 1, "B3": 0, "B4": 0}
    pooled = rep["secondaries"]["both_not_share_by_bin"]["pooled"]
    assert {b: (v["observations_with_both_not"], v["observations"]) for b, v in pooled.items()} == {
        "B1": (1, 4), "B2": (1, 4), "B3": (2, 4), "B4": (1, 4)}
    assert pooled["B3"]["share"]["value"] == 0.5


def test_c10_three_spearman_correlations_per_sheet_and_pooled(monkeypatch, score):
    st, (a1, b1, a2, b2) = _sec_study(monkeypatch)
    rep = st.write().report(score)
    sims = [0.05 + 0.1 * i for i in range(8)]

    def counts(a, b):
        ca = [sum(a[2 * i + j] == "NOT" for j in (0, 1)) for i in range(8)]
        cb = [sum(b[2 * i + j] == "NOT" for j in (0, 1)) for i in range(8)]
        cboth = [sum(a[2 * i + j] == "NOT" and b[2 * i + j] == "NOT" for j in (0, 1)) for i in range(8)]
        return ca, cb, cboth
    c1, c2 = counts(a1, b1), counts(a2, b2)
    sp = rep["secondaries"]["spearman"]
    for name, idx in (("A_not_count", 0), ("B_not_count", 1), ("both_not_count", 2)):
        assert sp["sheet_one"][name]["value"] == pytest.approx(_pearson(_rank(sims), _rank(c1[idx])))
        assert sp["sheet_two"][name]["value"] == pytest.approx(_pearson(_rank(sims), _rank(c2[idx])))
        pooled_x, pooled_y = sims + sims, c1[idx] + c2[idx]
        assert sp["pooled"][name]["value"] == pytest.approx(_pearson(_rank(pooled_x), _rank(pooled_y)))


def test_c10_clause_split_agreement_is_read_from_each_seats_file(monkeypatch, score):
    st = _agree_study(monkeypatch)
    rep = st.report(score)
    got = rep["secondaries"]["clause_split_agreement"]
    assert got["sheet_one"] == st.resplit[0] and got["sheet_two"] == st.resplit[1]


def test_c10_malformed_resplit_file_refuses(monkeypatch, score):
    st = _agree_study(monkeypatch)
    (st.dir / "resplit_1.json").write_text("not json")
    assert st.run(score)[0] == 2


def test_c10_pass2_rate_unclear_rate_and_flags_per_judge(monkeypatch, score):
    st = _agree_study(monkeypatch)
    r = st.sheets[0]["real"]
    # judge A, sheet one: 8 pass-1 NOTs (clauses 0-7). Mark 3 of them in pass 2.
    for rid, k in ((r[0], 1), (r[0], 2), (r[1], 1)):
        st.set(0, "A", rid, k, "NOT, STATED IN INTERPRETATION", passes=("pass2",))
    st.set(0, "A", r[7], 1, "UNCLEAR", passes=("pass1", "pass2"))
    st.set(0, "A", r[7], 2, "UNCLEAR", passes=("pass1", "pass2"))
    st.flag(0, "A", r[2], 1, "a note")
    st.flag(0, "A", r[3], 2, "another", p="pass2")
    st.flag(0, "A", st.sheets[0]["planted_ids"][0], 1, "on a planted item")   # planted: not counted
    st.write()
    rep = st.report(score)
    pj = rep["secondaries"]["per_judge"]["sheet_one"]["judgeA"]
    assert pj["pass2_rate"]["numerator"] == 3 and pj["pass2_rate"]["denominator"] == 8
    assert pj["pass2_rate"]["value"] == pytest.approx(3 / 8)
    assert pj["unclear_rate"]["value"] == pytest.approx(2 / 16)
    assert pj["flag_count"] == {"pass1": 1, "pass2": 1}
    pb = rep["secondaries"]["per_judge"]["sheet_one"]["judgeB1"]
    assert pb["flag_count"] == {"pass1": 0, "pass2": 0}
    assert pb["unclear_rate"]["value"] == 0.0


def test_c10_pass2_rate_undefined_when_no_pass1_nots(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.put(1, "A", ["CARRIED"] * 16)
    st.write()
    pj = st.report(score)["secondaries"]["per_judge"]["sheet_two"]["judgeA"]
    assert pj["pass2_rate"]["value"] is None and pj["pass2_rate"]["undefined_reason"]


# =========================================================================== contract 11: output
def test_c11_report_json_carries_every_input_hash_commit_and_seed(monkeypatch, score):
    st = _agree_study(monkeypatch)
    rep = st.report(score)
    shas = set(rep["inputs"].values())
    for path in [st.dir / "config.json", st.dir / "hashes.json", st.sheets[0]["out"] / "key.json",
                 st.sheets[1]["out"] / "planted-key.json", st.dir / "resplit_0.json",
                 st.sheets[0]["out"] / "sheet-pass1.md", st.ret(0, "A", "pass1"), st.ret(1, "B", "pass2")]:
        assert hashlib.sha256(path.read_bytes()).hexdigest() in shas
    assert rep["scorer"]["commit"] == "abc1234"
    assert rep["rng"]["seed"] == SIGNED_SEED


def test_c11_report_md_prints_item_numbers_never_node_ids(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.flag(0, "A", st.sheets[0]["real"][0], 1, "my note")
    st.write()
    rc, out = st.run(score)
    assert rc == 0
    md = (out / "report.md").read_text()
    assert md.strip()
    for sh in st.sheets:
        for rid in sh["real"] + sh["planted_ids"]:
            assert rid not in md
    assert not re.search(r"\b[a-z]{2}_\d{4}\b", md)
    assert "planted" in md.lower()


def test_c11_report_md_leads_with_the_planted_controls(monkeypatch, score):
    st = _agree_study(monkeypatch)
    rc, out = st.run(score)
    md = (out / "report.md").read_text()
    assert md.lower().index("planted") < md.lower().index("kappa")


def test_c11_refuses_to_overwrite_a_report(monkeypatch, score):
    st = _agree_study(monkeypatch)
    assert st.run(score, "same")[0] == 0
    assert st.run(score, "same")[0] == 2


def test_c11_seed_must_be_the_signed_seed_on_both_keys(monkeypatch, score):
    st = _agree_study(monkeypatch)
    kp = st.sheets[1]["out"] / "key.json"
    key = json.loads(kp.read_text())
    key["seed"] = SIGNED_SEED + 1
    kp.write_text(json.dumps(key))
    assert st.run(score)[0] == 2


def test_c11_no_report_dir_residue_on_refusal(monkeypatch, score):
    st = _agree_study(monkeypatch)
    st.bad_hash.add((0, "A", "pass1"))
    st.write()
    rc, out = st.run(score)
    assert rc == 2 and (not out.exists() or not list(out.iterdir()))


# =========================================================================== item order
def test_order_numbering_redrawn_by_the_real_tool_changes_nothing_in_the_scores(monkeypatch, score):
    base = _agree_study(monkeypatch)
    redrawn = _study(monkeypatch, render_seed_hexes=("1a2b3c4d", "0f0f0f0f"))
    for si, (a, b) in enumerate(((S1_A, S1_B), (S2_A, S2_B))):
        redrawn.put(si, "A", a)
        redrawn.put(si, "B", b)
    redrawn.write()
    # ids are the same, numbering is not
    assert [base.sheets[0]["num_of"][r] for r in base.sheets[0]["real"]] != [
        redrawn.sheets[0]["num_of"][r] for r in redrawn.sheets[0]["real"]]
    r1, r2 = base.report(score), redrawn.report(score)
    assert r1["measures"] == r2["measures"]
    assert r1["secondaries"]["both_not_share_by_bin"] == r2["secondaries"]["both_not_share_by_bin"]
    assert r1["verdict"] == r2["verdict"]


def test_order_non_monotone_key_order_changes_nothing(monkeypatch, score):
    st = _agree_study(monkeypatch)
    want = st.report(score)["measures"]
    for sh in st.sheets:
        kp = sh["out"] / "key.json"
        key = json.loads(kp.read_text())
        items = key["render"]["items"]
        key["render"]["items"] = {n: items[n] for n in sorted(items, key=lambda n: (int(n) * 7) % 13)}
        key["items"] = list(reversed(key["items"]))
        kp.write_text(json.dumps(key))
    assert list(json.loads((st.sheets[0]["out"] / "key.json").read_text())["render"]["items"]) != [
        str(i) for i in range(1, 13)]
    got = st.report(score, "out_shuffled")["measures"]
    assert got == want


# =========================================================================== round 3 (reviewer round 1)
# Item 1: the report cites draft 5.
def test_r3_report_and_docstring_cite_draft_5(monkeypatch, score):
    st = _agree_study(monkeypatch)
    rc, out = st.run(score)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    md = (out / "report.md").read_text()
    for text in (rep["preregistration"], md, score.__doc__):
        assert "6387983396" in text and "9a0afe35" in text
    # the seed is draft 4's pinned value, and the report says so
    assert "6385972287" in rep["preregistration"] and "3594015630" in rep["preregistration"]
    assert "draft 5" in rep["preregistration"]


# Item 2: a trailing judge-notes section.
NOTES_TEXT = ("the recalled line about the third item bore on one label\n"
              "Label: NOT\n"
              "1. a line shaped like a clause\n"
              "Flag (optional): a flag-shaped line\n"
              "## Item 2\n"
              "\n"
              "last line of the notes")


def _add_notes(heading="## Notes", text=NOTES_TEXT):
    return lambda t: t.rstrip("\n") + "\n\n" + heading + "\n" + text + "\n"


def _scored_parts(rep):
    return {k: rep[k] for k in ("planted_controls", "verdict", "measures", "marginals", "secondaries",
                                "protocol_deviations")}


@pytest.mark.parametrize("heading", ["## Notes", "  ## NOTES  ", "## notes"])
def test_r3_trailing_notes_score_identically_and_text_is_in_json_only(monkeypatch, score, heading):
    plain = _agree_study(monkeypatch)
    want = _scored_parts(plain.report(score))
    st = _agree_study(monkeypatch)
    st.hooks[(0, "A", "pass1")] = _add_notes(heading)
    st.hooks[(1, "B", "pass2")] = _add_notes(heading)
    st.write()
    rc, out = st.run(score)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    assert _scored_parts(rep) == want
    assert rep["notes"]["sheet_one"]["judgeA"]["pass1"] == NOTES_TEXT
    assert rep["notes"]["sheet_two"]["judgeB2"]["pass2"] == NOTES_TEXT
    assert rep["notes"]["sheet_one"]["judgeA"]["pass2"] is None
    md = (out / "report.md").read_text()
    assert "recalled line" not in md and "last line of the notes" not in md
    assert "notes: pass1 yes, pass2 no" in md       # sheet one / judgeA
    assert "notes: pass1 no, pass2 yes" in md       # sheet two / judgeB2
    assert "notes: pass1 no, pass2 no" in md        # everyone else


def test_r3_notes_with_label_like_lines_change_no_count(monkeypatch, score):
    st = _agree_study(monkeypatch)
    want = _scored_parts(st.report(score, "plain"))
    st.hooks[(0, "B", "pass1")] = _add_notes(text="Label: NOT\nLabel: NOT\nFlag (optional): x\nFlag (optional): y")
    st.write()
    got = _scored_parts(st.report(score, "with_notes"))
    assert got == want
    assert got["secondaries"]["per_judge"]["sheet_one"]["judgeB1"]["flag_count"] == {"pass1": 0, "pass2": 0}


def test_r3_notes_heading_before_the_last_item_refuses(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)

    def hook(text):
        heads = [m.start() for m in re.finditer(r"(?m)^## Item \d+$", text)]
        at = heads[-2]                       # before the second-to-last item: hides the last two
        return text[:at] + "## Notes\n\n" + text[at:]
    st.hooks[(0, "A", "pass1")] = hook
    st.write()
    rc, out = st.run(score)
    err = capsys.readouterr().err
    assert rc == 2 and "after the last clause" in err
    assert not out.exists() or not list(out.iterdir())


def test_r3_notes_heading_before_the_last_clause_of_the_last_item_refuses(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)

    def hook(text):
        last = list(re.finditer(r"(?m)^## Item \d+$", text))[-1].start()
        at = text.index("\n2. ", last) + 1   # before the last item's second (last) clause
        return text[:at] + "## Notes\n" + text[at:]
    st.hooks[(1, "A", "pass2")] = hook
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and "after the last clause" in capsys.readouterr().err


def test_r3_notes_heading_between_a_clause_and_its_label_refuses(monkeypatch, score, capsys):
    st = _agree_study(monkeypatch)

    def hook(text):
        last = list(re.finditer(r"(?m)^## Item \d+$", text))[-1].start()
        at = text.index("   Label:", text.index("\n2. ", last))   # the last clause's own Label line
        return text[:at] + "## Notes\n" + text[at:]
    st.hooks[(0, "B", "pass1")] = hook
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and "after the last clause" in capsys.readouterr().err


def test_r3_text_after_the_last_clause_with_no_notes_heading_keeps_todays_behaviour(score):
    """Not changed by round 3: with an open Flag the line continues the flag; without one it refuses."""
    base = "## Item 1\n\n**Clauses:**\n\n1. x\n   Label: NOT\n"
    got = score.parse_sheet(base + "   Flag (optional):\nstray line", "w")
    assert got[1][0]["flag"] == "stray line"
    with pytest.raises(score.Refusal):
        score.parse_sheet(base + "stray line", "w")


# Item 3: placeholder flags are not flags.
PLACEHOLDERS = ["n/a", "na", "none", "-", "no", "N/A", "  None ", "NO", "Na"]


@pytest.mark.parametrize("ph", PLACEHOLDERS)
def test_r3_placeholder_flag_does_not_count(monkeypatch, score, ph):
    st = _agree_study(monkeypatch)
    r = st.sheets[0]["real"]
    st.flag(0, "A", r[0], 1, "a real note")
    st.flag(0, "A", r[1], 1, ph)
    st.write()
    pj = st.report(score)["secondaries"]["per_judge"]["sheet_one"]["judgeA"]
    assert pj["flag_count"]["pass1"] == 1


def test_r3_flag_that_merely_starts_with_a_placeholder_still_counts(monkeypatch, score):
    st = _agree_study(monkeypatch)
    r = st.sheets[0]["real"]
    st.flag(0, "A", r[0], 1, "n/a but see item 3")
    st.flag(0, "A", r[1], 1, "none of these clauses fit")
    st.flag(0, "A", r[2], 1, "n/a\nsecond line")
    st.write()
    assert st.report(score)["secondaries"]["per_judge"]["sheet_one"]["judgeA"]["flag_count"]["pass1"] == 3


def test_r3_report_records_the_placeholder_set(monkeypatch, score):
    rep = _agree_study(monkeypatch).report(score)
    assert sorted(rep["flag_placeholders"]) == sorted(["n/a", "na", "none", "-", "no"])


# Item 4: the interval method is recorded.
def test_r3_report_records_the_interval_method(monkeypatch, score):
    rep = _agree_study(monkeypatch).report(score)
    assert rep["ci_method"] == "percentile, linear interpolation at q*(m-1) over the defined resamples"


# =========================================================================== round 4: flag safety and heading variants
def _last_flag_hook(heading="## Notes"):
    """Put the heading just before the last Flag line of the return, i.e. between the last clause's
    Label line and its Flag line."""
    def hook(text):
        at = text.rindex("   Flag (optional):")
        return text[:at] + heading + "\n" + text[at:]
    return hook


def test_r4_notes_heading_between_last_label_and_its_flag_refuses(monkeypatch, score, capsys):
    st = _study(monkeypatch)
    st.put(0, "A", S1_A)
    st.put(0, "B", S1_B)
    st.put(1, "A", S2_A)
    st.put(1, "B", S2_B)
    last = max(st.sheets[0]["real"], key=lambda r: st.sheets[0]["num_of"][r])
    st.flag(0, "A", last, 2, "split is wrong")
    st.hooks[(0, "A", "pass1")] = _last_flag_hook()
    st.write()
    rc, out = st.run(score)
    err = capsys.readouterr().err
    assert rc == 2 and "Flag" in err and "after the last clause" in err
    assert not out.exists() or not list(out.iterdir())


@pytest.mark.parametrize("heading", ["## Notes", "## Notes:", "### Notes", "# notes", "Notes:", "Notes", "  ##   NOTES :  "])
def test_r4_heading_between_label_and_flag_refuses_for_every_variant(score, heading):
    text = "## Item 1\n\n**Clauses:**\n\n1. x\n   Label: NOT\n" + heading + "\n   Flag (optional): split is wrong\n"
    with pytest.raises(score.Refusal) as ei:
        score.parse_return(text, "w")
    assert "item 1, clause 1" in str(ei.value) and "Flag" in str(ei.value)


def test_r4_notes_after_a_label_with_no_flag_line_still_accepted(score):
    text = "## Item 1\n\n**Clauses:**\n\n1. x\n   Label: NOT\n## Notes\nfree text\n"
    items, notes, _ = score.parse_return(text, "w")
    assert items[1][0]["flag"] is None and notes == "free text"


@pytest.mark.parametrize("heading", ["## Notes", "## Notes:", "### Notes", "# notes", "Notes:", "Notes"])
def test_r4_heading_variants_after_the_last_clause_are_notes(monkeypatch, score, heading):
    want = _scored_parts(_agree_study(monkeypatch).report(score))
    st = _agree_study(monkeypatch)
    st.hooks[(0, "A", "pass1")] = _add_notes(heading)
    st.write()
    rc, out = st.run(score)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    assert _scored_parts(rep) == want
    assert rep["secondaries"]["per_judge"]["sheet_one"]["judgeA"]["flag_count"]["pass1"] == 0
    assert rep["notes"]["sheet_one"]["judgeA"]["pass1"] == NOTES_TEXT


@pytest.mark.parametrize("heading", ["## Notes:", "### Notes", "# notes", "Notes:", "Notes"])
def test_r4_heading_variants_before_the_last_clause_refuse(monkeypatch, score, capsys, heading):
    st = _agree_study(monkeypatch)

    def hook(text):
        last = list(re.finditer(r"(?m)^## Item \d+$", text))[-1].start()
        at = text.index("\n2. ", last) + 1
        return text[:at] + heading + "\n" + text[at:]
    st.hooks[(1, "A", "pass2")] = hook
    st.write()
    rc, _ = st.run(score)
    assert rc == 2 and "after the last clause" in capsys.readouterr().err


def test_r4_flag_text_that_merely_contains_notes_is_still_a_flag(score):
    text = ("## Item 1\n\n**Clauses:**\n\n1. x\n   Label: NOT\n"
            "   Flag (optional): notes on the split\nnotes on the split, line two\n")
    items, notes, _ = score.parse_return(text, "w")
    assert notes is None
    assert items[1][0]["flag"] == "notes on the split\nnotes on the split, line two"


def test_r4_report_records_the_notes_heading_pattern(monkeypatch, score):
    rep = _agree_study(monkeypatch).report(score)
    assert rep["notes_heading_pattern"] == r"^#{0,6}\s*notes\s*:?$"
