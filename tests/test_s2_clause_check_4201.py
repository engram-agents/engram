"""Cells for tools/s2_clause_check.py (engram-alpha#4201, part of #4050 S2 next step):
phase 1 -- the clause-check harness, baselines B0/B1/B2 and the deterministic check M1.

Contract-first: these cells were written before the tool. Fixtures are invented
(synthetic quotes, clauses, labels, ids); no real claim, sheet, key or graph is
ever opened. Study files are hand-written minimal sheets/keys/returns in the
shape s2_score reads. Node-id-shaped tokens are assembled at runtime, never
written as literals. Expected values are computed here independently of the
tool (plain arithmetic or brute force), or are hand-computed constants.
"""

import atexit
import copy
import hashlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TOOLS = Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"
_CHECK_PATH = _TOOLS / "s2_clause_check.py"

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_s2check_4201_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_COUNTER = itertools.count()
SIGNED_SEED = 3594015630


def _load(path, label):
    spec = importlib.util.spec_from_file_location(f"{label}_{next(_COUNTER)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cc(monkeypatch):
    mod = _load(_CHECK_PATH, "s2_clause_check")
    # no embedder in the cells: the real similarity function must never be reached
    monkeypatch.setattr(mod.engram_anchoring, "quote_claim_similarity",
                        lambda q, c: (None, "test: no embedder"))
    # the cells run from whatever tree the suite is in, which may be dirty: freeze's clean-tree check is
    # stubbed here and restored by the cells that exercise it (round 3)
    mod._orig_require_clean_tree = getattr(mod, "_require_clean_tree", None)
    monkeypatch.setattr(mod, "_require_clean_tree", lambda paths: None, raising=False)
    return mod


@pytest.fixture
def anchoring():
    import engram_anchoring
    return engram_anchoring


NODE_SHAPED = "ab" + "_" + "4242"  # node-id-shaped, assembled at runtime


# =========================================================================== M1
def test_m1_clause_fully_contained_is_stated(cc):
    r = cc.check_m1("Quillon merged the branch after the review finished", "Quillon merged the branch", 1.0)
    assert r["verdict"] == "STATED"
    assert r["missing_type"] is None
    assert r["missing"] == []


def test_m1_missing_date_is_typed_date(cc):
    r = cc.check_m1("Quillon merged the branch after review", "Quillon merged the branch on 2026-10-01", 0.0)
    assert r["verdict"] == "NOT"
    assert r["missing_type"] == "date"
    assert "2026-10-01" in r["missing"]


def test_m1_missing_time_is_typed_date(cc):
    r = cc.check_m1("the job ran overnight", "the job ran at 14:30", 0.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "date")


def test_m1_missing_number_is_typed_count(cc):
    r = cc.check_m1("the suite ran many cells", "the suite ran 4812 cells", 0.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "count")
    assert "4812" in r["missing"]


def test_m1_missing_issue_ref_is_typed_ref(cc):
    r = cc.check_m1("the fix landed in the tracker", "the fix landed in #123", 0.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "ref")


def test_m1_missing_hex_sha_is_typed_ref(cc):
    r = cc.check_m1("the fix landed in a commit", "the fix landed in deadbeef1", 0.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "ref")


def test_m1_missing_node_id_shaped_token_is_typed_ref(cc):
    r = cc.check_m1("the note was cited", f"the note {NODE_SHAPED} was cited", 0.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "ref")


def test_m1_missing_capitalized_name_is_typed_actor(cc):
    r = cc.check_m1("the branch was merged after review", "the branch was merged by Quillon after review", 1.0)
    assert r["verdict"] == "NOT"
    assert r["missing_type"] == "actor"
    assert "quillon" in r["missing"]


def test_m1_missing_lowercase_content_word_is_typed_other(cc):
    r = cc.check_m1("the branch was reviewed", "the branch was merged", 1.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "other")


def test_m1_type_priority_date_over_count_over_ref_over_actor(cc):
    quote = "the branch was merged"
    both = cc.check_m1(quote, "Quillon merged #123 with 4812 cells on 2026-10-01", 0.0)
    assert both["missing_type"] == "date"
    no_date = cc.check_m1(quote, "Quillon merged #123 with 4812 cells", 0.0)
    assert no_date["missing_type"] == "count"
    no_count = cc.check_m1(quote, "Quillon merged #123", 0.0)
    assert no_count["missing_type"] == "ref"
    actor_only = cc.check_m1(quote, "the branch was merged by Quillon", 1.0)   # not sentence-initial
    assert actor_only["missing_type"] == "actor"


def test_m1_never_emits_relation(cc):
    quote = "the branch was merged"
    clauses = ["Quillon merged #123", "merged 4812 on 2026-10-01", "the branch stalled", "branch merged by Marrowby",
               "nothing alike here at all"]
    seen = {cc.check_m1(quote, c, 1.0)["missing_type"] for c in clauses}
    assert seen <= {"actor", "date", "count", "ref", "other"}
    assert "relation" in cc.check_m1.__doc__  # the docstring says it is reserved for phase 2


def test_m1_stemmed_match_counts_as_present(cc):
    # clause 'merged' and quote 'merges' reduce to the same stem
    # round 3: 'merged' (-> merg) and 'merges' (-> merge) no longer collide; 'process'/'processes' do
    r = cc.check_m1("she processes the branches", "process the branch", 1.0)
    assert r["verdict"] == "STATED"


def test_m1_zero_content_word_clause_with_all_hard_tokens_is_stated(cc):
    clause = "On 2026-10-01 in #123"  # 'on' 'in' too short; no content word
    r = cc.check_m1("the thing happened 2026-10-01 under #123", clause, 1.0)
    assert r["verdict"] == "STATED"


def test_m1_zero_content_word_clause_with_missing_hard_token_is_not(cc):
    r = cc.check_m1("the thing happened under #123", "On 2026-10-01 in #123", 0.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "date")


def test_m1_normalization_case_markdown_nfkc_whitespace(cc):
    quote = "**Quillon**   MERGED\n the `branch`"
    r = cc.check_m1(quote, "quillon merged the branch", 1.0)
    assert r["verdict"] == "STATED"
    full_width = "ＭERGED the branch"  # NFKC folds the full-width M
    assert cc.check_m1(full_width, "merged the branch", 1.0)["verdict"] == "STATED"


def test_m1_theta_boundary_is_inclusive(cc):
    clause = "alpha bravo delta gamma"
    quote = "alpha bravo delta zebra"  # 3 of 4 stems present: share 0.75
    assert cc.check_m1(quote, clause, 0.75)["verdict"] == "STATED"
    assert cc.check_m1(quote, clause, 0.76)["verdict"] == "NOT"


def test_m1_stopwords_do_not_count_as_content(cc):
    # 'the', 'was', 'after' are stopwords: only 'branch' counts
    assert cc.check_m1("a branch exists", "the branch was there after", 1.0)["verdict"] == "STATED"


def test_m1_stemmer_rules(cc):
    stem = cc.stem
    assert stem("merged") == "merg"
    assert stem("merges") == "merg"    # round 3: 'merges' falls to the 's' rule (-> merge); round 5 final-e drop -> merg
    assert stem("running") == "runn"
    assert stem("cats") == "cat"
    assert stem("bed") == "bed"      # remainder would be < 3 chars: unchanged
    assert stem("sees") == "see"     # 'es' would leave 'se' (< 3 chars), so the 's' rule applies
    assert stem("branch") == "branch"


# =========================================================================== baselines
def test_b0_always_not(cc):
    assert cc.check_b0("anything", "anything")["verdict"] == "NOT"
    assert cc.check_b0("", "")["verdict"] == "NOT"


@pytest.mark.parametrize("quote,clause", [
    ("Quillon wrote 4812 cells on 2026-10-01", "ran 4812 cells on 2026-10-01"),       # all anchored
    ("Quillon wrote 4812 cells", "ran 4812 cells on 2026-10-01"),                     # one unanchored
    ("nothing numeric here", "no hard tokens in this clause"),                        # nothing to anchor
])
def test_b1_matches_anchor_exactly(cc, anchoring, quote, clause):
    want_unanchored = anchoring.anchor(clause, quote).unanchored
    r = cc.check_b1(quote, clause)
    assert r["verdict"] == ("NOT" if want_unanchored else "STATED")
    assert r["unanchored"] == want_unanchored


def test_b2_cutoff_boundary_is_stated_and_none_is_unavailable(cc):
    sims = {"q1": 0.50, "q2": 0.49, "q3": None}
    fn = lambda q, c: sims[q]  # noqa: E731
    assert cc.check_b2("q1", "c", 0.50, sim_fn=fn)["verdict"] == "STATED"     # >= is STATED
    assert cc.check_b2("q2", "c", 0.50, sim_fn=fn)["verdict"] == "NOT"
    r = cc.check_b2("q3", "c", 0.50, sim_fn=fn)
    assert r["verdict"] == "unavailable"
    assert r["similarity"] is None


def test_b2_accepts_the_real_functions_tuple_shape(cc):
    assert cc.check_b2("q", "c", 0.5, sim_fn=lambda q, c: (0.7, None))["verdict"] == "STATED"
    assert cc.check_b2("q", "c", 0.5, sim_fn=lambda q, c: (None, "embedder unavailable"))["verdict"] == "unavailable"


# =========================================================================== tuning
def _scored(score, consensus):
    return {"score": score, "consensus": consensus}


def _predict_score(rec, value):
    return "STATED" if rec["score"] >= value else "NOT"


def _kappa_ref(pairs):
    """Independent Cohen kappa on [(method, truth)] with STATED/NOT, or None if undefined."""
    n = len(pairs)
    a = sum(1 for m, t in pairs if m == "STATED" and t == "STATED")
    b = sum(1 for m, t in pairs if m == "STATED" and t == "NOT")
    c = sum(1 for m, t in pairs if m == "NOT" and t == "STATED")
    d = sum(1 for m, t in pairs if m == "NOT" and t == "NOT")
    po = (a + d) / n
    pm, pt = (a + b) / n, (a + c) / n
    pe = pm * pt + (1 - pm) * (1 - pt)
    return None if pe == 1 else (po - pe) / (1 - pe)


def test_grid_is_0_to_1_step_001(cc):
    g = cc.tuning_grid()
    assert len(g) == 101 and g[0] == 0.0 and g[1] == 0.01 and g[-1] == 1.0


def test_tuning_picks_the_kappa_maximizing_value(cc):
    dev = [_scored(0.10, "NOT"), _scored(0.20, "NOT"), _scored(0.55, "NOT"),
           _scored(0.50, "STATED"), _scored(0.70, "STATED"), _scored(0.80, "STATED"),
           _scored(0.30, "STATED")]
    best = None
    for v in cc.tuning_grid():
        k = _kappa_ref([(_predict_score(r, v), r["consensus"]) for r in dev])
        if k is not None and (best is None or k > best[1] + 1e-12):
            best = (v, k)
    got = cc.tune(dev, _predict_score)
    assert got["value"] == best[0]
    assert got["kappa"] == pytest.approx(best[1])


def test_tuning_tie_picks_the_smallest_value(cc):
    # any cutoff in (0.30, 0.60] separates perfectly; the smallest grid value is 0.31
    dev = [_scored(0.1, "NOT"), _scored(0.2, "NOT"), _scored(0.3, "NOT"),
           _scored(0.6, "STATED"), _scored(0.7, "STATED"), _scored(0.8, "STATED")]
    got = cc.tune(dev, _predict_score)
    assert got["value"] == 0.31
    assert got["kappa"] == pytest.approx(1.0)


def test_tuning_uses_agreed_clauses_only_and_skips_unavailable(cc):
    dev = [_scored(0.1, "NOT"), _scored(0.9, "STATED"),
           _scored(0.9, "BAND"), _scored(0.1, "BAND"),            # band clauses are not tuned on
           {"score": None, "consensus": "NOT"}]                    # unavailable prediction is skipped

    def predict(rec, v):
        return None if rec["score"] is None else _predict_score(rec, v)
    got = cc.tune(dev, predict)
    assert got["value"] == 0.11
    assert got["n"] == 2


# =========================================================================== consensus
def test_consensus_rules(cc):
    assert cc.consensus_of("NOT", "NOT") == ("NOT", None)
    assert cc.consensus_of("CARRIED", "UNCLEAR") == ("STATED", None)
    assert cc.consensus_of("CARRIED", "CARRIED") == ("STATED", None)
    assert cc.consensus_of("NOT", "CARRIED") == ("BAND", "A")
    assert cc.consensus_of("UNCLEAR", "NOT") == ("BAND", "B")


# =========================================================================== synthetic study files
JA, JB1, JB2 = "judgeA", "judgeB1", "judgeB2"

# (id, quote, [(clause, labelA, labelB)])
SHEET_ONE = [
    ("s1-a", "Quillon merged the branch after review on 2026-10-01",
     [("Quillon merged the branch", "CARRIED", "CARRIED"), ("The branch merged on 2026-10-02", "NOT", "NOT")]),
    ("s1-b", "the suite ran 4812 cells and all passed",
     [("the suite ran 4812 cells", "CARRIED", "UNCLEAR"), ("Marrowby approved the cells", "NOT", "CARRIED")]),
    ("s1-c", "a short quote about queues",
     [("the queue was drained", "NOT", "NOT"), ("queues exist", "CARRIED", "CARRIED")]),
]
SHEET_TWO = [
    ("s2-a", "the fix landed in #123 last week",
     [("the fix landed in #123", "CARRIED", "CARRIED"), ("the fix landed in #124", "NOT", "NOT")]),
    ("s2-b", "bravo delta were mentioned together",
     [("alpha bravo delta gamma", "NOT", "CARRIED"), ("bravo delta mentioned", "CARRIED", "CARRIED")]),
    ("s2-c", "nothing relevant at all",
     [("the cache was cleared", "NOT", "NOT"), ("the cache was warm", "NOT", "CARRIED")]),
]
PLANTED_ITEMS = [  # planted items carry labels that would corrupt the numbers if they were not excluded
    ("plant-P1", "planted quote one", [("pc one a", "NOT", "NOT"), ("pc one b", "NOT", "NOT")], "P1", None),
    ("plant-P2", "planted quote two", [("pc two a", "NOT", "NOT")], "P2", None),
    ("plant-P3", "planted quote three", [("pc three a", "NOT", "NOT"), ("pc three b", "NOT", "NOT")], "P3", 2),
    ("plant-P4", "planted quote four", [("pc four a", "NOT", "NOT"), ("pc four b", "NOT", "NOT")], "P4", 1),
]


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sheet_text(items, labels, pass2):
    parts = ["# Clause-check sheet\n\nsynthetic header\n\n"]
    for num, (iid, quote, clauses) in enumerate(items, 1):
        parts.append(f"## Item {num}\n\n**Quote:**\n" + "\n".join("> " + ln for ln in quote.split("\n")) + "\n\n")
        if pass2:
            parts.append("**Interpretation (the author's own text, not evidence):**\n> synthetic interpretation\n\n")
        parts.append("**Clauses:**\n\n")
        for k, c in enumerate(clauses, 1):
            parts.append(f"{k}. {c}\n   Label: {labels.get((iid, k), '')}\n   Flag (optional): \n\n")
    return "".join(parts)


def _write_study(real_sheets, judges=((JA, JB1), (JA, JB2)), tweak_labels=None):
    """Hand-write two sheets (keys, rendered sheets, planted keys, returns, config, hashes)."""
    root = Path(TEST_DIR) / f"study_{next(_COUNTER)}"
    root.mkdir()
    names = ["sheet_one", "sheet_two"]
    hashes, cfg_sheets, info = {}, [], []
    for si, real in enumerate(real_sheets):
        items = [(i, q, [c[0] for c in cl]) for i, q, cl in real]
        planted = [(i, q, [c[0] for c in cl]) for i, q, cl, _k, _a in PLANTED_ITEMS]
        order = items[:1] + planted[:2] + items[1:] + planted[2:]   # planted interleaved
        sheet1 = _sheet_text(order, {}, False)
        sheet2 = _sheet_text(order, {}, True)
        d = root / f"sheet{si}"
        d.mkdir()
        (d / "sheet-pass1.md").write_text(sheet1)
        (d / "sheet-pass2.md").write_text(sheet2)
        render_items = {}
        for num, (iid, _q, cls) in enumerate(order, 1):
            entry = {"id": iid, "clauses": len(cls), "order_hash": "0" * 64}
            for pid, _pq, _pc, kind, _added in PLANTED_ITEMS:
                if pid == iid:
                    entry["kind"] = kind
            render_items[str(num)] = entry
        key = {"seed": SIGNED_SEED,
               "render": {"items": render_items,
                          "sha256": {"sheet-pass1.md": _sha(sheet1), "sheet-pass2.md": _sha(sheet2)}},
               "items": [{"node_id": i, "bin": "B1", "similarity": 0.1} for i, _q, _c in real]}
        (d / "key.json").write_text(json.dumps(key))
        (d / "planted-key.json").write_text(json.dumps(
            {pid: {"added_clause": added} for pid, _q, _c, _k, added in PLANTED_ITEMS}))
        (d / "resplit.json").write_text(json.dumps({"items_compared": 3}))
        returns = {}
        for role, judge in (("A", judges[si][0]), ("B", judges[si][1])):
            labels = {}
            for iid, _q, cl in real:
                for k, (_c, la, lb) in enumerate(cl, 1):
                    labels[(iid, k)] = la if role == "A" else lb
            for pid, _pq, pcl, _kind, _added in PLANTED_ITEMS:
                for k, (_c, la, lb) in enumerate(pcl, 1):
                    labels[(pid, k)] = la if role == "A" else lb
            if tweak_labels:
                tweak_labels(si, role, labels)
            for p, text0 in (("pass1", False), ("pass2", True)):
                text = _sheet_text(order, labels, text0)
                path = d / f"ret_{role}_{p}.md"
                path.write_text(text)
                returns.setdefault(judge, {})[p] = str(path)
                hashes.setdefault(names[si], {}).setdefault(judge, {})[p] = _sha(text)
        cfg_sheets.append(dict(name=names[si], key=str(d / "key.json"), planted_key=str(d / "planted-key.json"),
                               resplit_agreement=str(d / "resplit.json"),
                               sheet_pass1=str(d / "sheet-pass1.md"), sheet_pass2=str(d / "sheet-pass2.md"),
                               rater_A=judges[si][0], rater_B=judges[si][1], returns=returns))
    (root / "config.json").write_text(json.dumps(dict(sheets=cfg_sheets)))
    (root / "hashes.json").write_text(json.dumps(hashes))
    return root


def test_load_study_builds_records_and_excludes_planted(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    sheets = cc.load_study(root / "config.json", root / "hashes.json")
    assert [s["name"] for s in sheets] == ["sheet_one", "sheet_two"]
    for sh, real in zip(sheets, (SHEET_ONE, SHEET_TWO)):
        assert len(sh["records"]) == sum(len(cl) for _i, _q, cl in real)   # no planted clause among them
        texts = {r["clause"] for r in sh["records"]}
        assert not any(t.startswith("pc ") for t in texts)
    r0 = sheets[0]["records"]
    by = {(r["clause"]): r for r in r0}
    assert by["Quillon merged the branch"]["consensus"] == "STATED"
    assert by["The branch merged on 2026-10-02"]["consensus"] == "NOT"
    assert by["the queue was drained"]["consensus"] == "NOT"
    band = by["Marrowby approved the cells"]
    assert (band["consensus"], band["band_column"]) == ("BAND", "A")
    assert by["Quillon merged the branch"]["quote"] == "Quillon merged the branch after review on 2026-10-01"


def test_load_study_refuses_a_return_whose_hash_differs(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    hashes = json.loads((root / "hashes.json").read_text())
    hashes["sheet_one"][JA]["pass1"] = "0" * 64
    (root / "hashes.json").write_text(json.dumps(hashes))
    with pytest.raises(cc.Refusal):
        cc.load_study(root / "config.json", root / "hashes.json")


# =========================================================================== evaluation
def _sheet(cc, name, specs):
    """specs: [(quote, clause, labelA, labelB)] -> one observation (item) per clause."""
    return {"name": name, "records": [cc.make_record(i, 1, q, c, a, b) for i, (q, c, a, b) in enumerate(specs, 1)]}


def _share_specs(threshold_j, copies=2):
    """Clauses of four distinct content words with j of them in the quote (share j/4).
    Both judges say STATED iff j >= threshold_j."""
    words = ["alpha", "bravo", "delta", "gamma"]
    specs = []
    for _ in range(copies):
        for j in range(5):
            quote = " ".join(words[:j] + ["zebra"])
            lab = "CARRIED" if j >= threshold_j else "NOT"
            specs.append((quote, " ".join(words), lab, lab))
    return specs


def _stub_sim(quote, clause):
    words = ["alpha", "bravo", "delta", "gamma"]
    return sum(1 for w in words if w in quote) / 4


def test_oracle_method_gets_kappa_one_and_stated_precision_recall_one(cc):
    sheet = _sheet(cc, "t", _share_specs(3))
    # inject a band clause so the oracle is judged on agreed clauses only
    sheet["records"].append(cc.make_record(99, 1, "x", "y", "NOT", "CARRIED"))

    def oracle(rec, value):
        return rec["consensus"] if rec["consensus"] in ("NOT", "STATED") else "NOT"
    res = cc.evaluate_method(sheet["records"], oracle, None)
    assert res["kappa"]["value"] == pytest.approx(1.0)
    assert res["stated_precision"]["value"] == pytest.approx(1.0)
    assert res["stated_recall"]["value"] == pytest.approx(1.0)
    assert res["band_split"]["NOT"] == 1 and res["band_split"]["STATED"] == 0
    for stat in ("kappa", "stated_precision", "stated_recall"):
        ci = res[stat]["ci"]
        assert ci["resamples"] == 2000
        assert ci["lo"] is not None and ci["lo"] <= res[stat]["value"] <= ci["hi"] + 1e-12


def test_b0_has_zero_stated_recall_and_undefined_precision_with_reason(cc):
    sheet = _sheet(cc, "t", _share_specs(3))
    res = cc.evaluate_method(sheet["records"], lambda r, v: cc.check_b0(r["quote"], r["clause"])["verdict"], None)
    assert res["stated_recall"]["value"] == 0.0
    assert res["stated_precision"]["value"] is None
    assert "no clause" in res["stated_precision"]["undefined_reason"]
    assert res["kappa"]["value"] == 0.0   # defined: always-NOT agrees with consensus exactly at chance


def test_evaluation_reports_kappa_against_each_column_alone(cc):
    # column A says NOT on 2 of 4, column B on 1 of 4; the method says NOT on the same 2 as A
    specs = [("q", "c1", "NOT", "NOT"), ("q", "c2", "NOT", "CARRIED"), ("q", "c3", "CARRIED", "CARRIED"),
             ("q", "c4", "CARRIED", "CARRIED")]
    sheet = _sheet(cc, "t", specs)
    method = {"c1": "NOT", "c2": "NOT", "c3": "STATED", "c4": "STATED"}
    res = cc.evaluate_method(sheet["records"], lambda r, v: method[r["clause"]], None)
    assert res["kappa_vs_column_A"]["value"] == pytest.approx(1.0)
    # vs B: table (method NOT & B NOT, ...) = (1, 1, 0, 2); po=.75, pm=.5, pb=.25, pe=.5*.25+.5*.75=.5 -> kappa=.5
    assert res["kappa_vs_column_B"]["value"] == pytest.approx(0.5)


def test_unavailable_predictions_are_excluded_and_counted(cc):
    sheet = _sheet(cc, "t", _share_specs(3))
    n = len(sheet["records"])
    res = cc.evaluate_method(sheet["records"],
                             lambda r, v: None if r["clause"] and r["item"] % 2 == 0 else r["consensus"], None)
    assert res["unavailable"] == sum(1 for r in sheet["records"] if r["item"] % 2 == 0)
    assert res["n_clauses_scored"] == n - res["unavailable"]


def test_bootstrap_is_reproducible(cc):
    sheet = _sheet(cc, "t", _share_specs(3))
    f = lambda r, v: "STATED" if "alpha" in r["quote"] else "NOT"  # noqa: E731
    a = cc.evaluate_method(sheet["records"], f, None)
    b = cc.evaluate_method(sheet["records"], f, None)
    assert a == b


def _folds(report):
    return {f["fold"]: f for f in report["folds"]}


def test_folds_swap_and_tuning_uses_only_the_dev_sheet(cc):
    s0 = _sheet(cc, "one", _share_specs(3))      # perfect separation for theta in (0.50, 0.75] -> 0.51
    s1 = _sheet(cc, "two", _share_specs(2))      # perfect separation for theta in (0.25, 0.50] -> 0.26
    rep = cc.build_report([s0, s1], sim_fn=_stub_sim)
    f = _folds(rep)
    assert (f[1]["tune_sheet"], f[1]["test_sheet"]) == ("one", "two")
    assert (f[2]["tune_sheet"], f[2]["test_sheet"]) == ("two", "one")
    assert f[1]["tuned"]["M1"]["value"] == 0.51
    assert f[2]["tuned"]["M1"]["value"] == 0.26
    assert f[1]["tuned"]["B2"]["value"] == 0.51
    assert f[2]["tuned"]["B2"]["value"] == 0.26

    # mutate the TEST sheet's labels in fold 1: the tuned value must not move ...
    s1_mut = _sheet(cc, "two", _share_specs(4))
    rep2 = cc.build_report([s0, s1_mut], sim_fn=_stub_sim)
    g = _folds(rep2)
    assert g[1]["tuned"]["M1"]["value"] == f[1]["tuned"]["M1"]["value"]
    assert g[1]["tuned"]["B2"]["value"] == f[1]["tuned"]["B2"]["value"]
    # ... while the same mutation, now on the DEV sheet of fold 2, does move it (the control can fire)
    assert g[2]["tuned"]["M1"]["value"] != f[2]["tuned"]["M1"]["value"]


def test_fold_results_are_scored_on_the_test_sheet_with_the_dev_value(cc):
    s0 = _sheet(cc, "one", _share_specs(3))
    s1 = _sheet(cc, "two", _share_specs(2))
    rep = cc.build_report([s0, s1], sim_fn=_stub_sim)
    f1 = _folds(rep)[1]
    # theta 0.51 tuned on 'one' applied to 'two' (labels STATED iff j >= 2): j=2 (share .5) is called NOT
    # -> clauses with j=2 are mistakes; independent count: 2 copies of j=2 are STATED truth, called NOT.
    m1 = f1["test"]["methods"]["M1"]
    assert m1["agreed_table"] == [[4, 0], [2, 4]]   # [[TP, FP], [FN, TN]], STATED positive: j=3,4 TP (4), j=2 FN (2), j=0,1 TN (4)
    assert set(f1["test"]["methods"]) == {"B0", "B1", "B2", "M1"}


def test_b2_unavailable_is_counted_in_the_report_not_guessed(cc):
    s0 = _sheet(cc, "one", _share_specs(3))
    s1 = _sheet(cc, "two", _share_specs(2))
    calls = []

    def sim(q, c):
        calls.append(1)
        return None if q.startswith("alpha bravo delta gamma") else _stub_sim(q, c)  # j == 4 unavailable
    rep = cc.build_report([s0, s1], sim_fn=sim)
    b2 = _folds(rep)[1]["test"]["methods"]["B2"]
    assert b2["unavailable"] == 2  # two copies of j=4 on the test sheet
    assert b2["n_clauses_scored"] == 8


def test_report_has_no_unguarded_nan(cc):
    s0 = _sheet(cc, "one", _share_specs(3))
    s1 = _sheet(cc, "two", _share_specs(3))
    rep = cc.build_report([s0, s1], sim_fn=_stub_sim)
    json.dumps(rep, allow_nan=False)


# =========================================================================== freeze / run
def _freeze_and_study(cc, tmp_name="x", fold=1, exclude=None, prior=None, root=None):
    root = root or _write_study([SHEET_ONE, SHEET_TWO])
    manifest = root / f"FREEZE{next(_COUNTER)}.json"
    argv = ["freeze", "--out", str(manifest), "--commit", "abc1234", "--fold", str(fold)]
    if exclude:
        argv += ["--exclude", str(exclude)]
    if prior:
        argv += ["--prior-report", str(prior)]
    assert cc.main(argv) == 0
    return root, manifest


def _run(cc, root, manifest, out_name="out", commit="abc1234", fold=1, exclude=None):
    out = root / out_name
    argv = ["run", "--config", str(root / "config.json"), "--hashes", str(root / "hashes.json"),
            "--freeze", str(manifest), "--out-dir", str(out), "--commit", commit, "--fold", str(fold)]
    if exclude:
        argv += ["--exclude", str(exclude)]
    return cc.main(argv), out


def test_freeze_manifest_contents(cc):
    root, manifest = _freeze_and_study(cc)
    m = json.loads(manifest.read_text())
    assert m["code_sha256"] == hashlib.sha256(_CHECK_PATH.read_bytes()).hexdigest()
    assert m["stopwords_sha256"] == hashlib.sha256("\n".join(sorted(cc.STOPWORDS)).encode("utf-8")).hexdigest()
    assert m["stemmer_suffixes"] == ["ing", "ed", "es", "s"]
    assert m["tuning_grid"] == {"start": 0.0, "stop": 1.0, "step": 0.01}
    assert m["tie_rule"] == "smallest"
    assert m["engram_alpha_commit"] == "abc1234"
    assert m["fold"] == 1


def test_freeze_does_not_overwrite(cc):
    root, manifest = _freeze_and_study(cc)
    before = manifest.read_text()
    assert cc.main(["freeze", "--out", str(manifest), "--commit", "other", "--fold", "1"]) == 2
    assert manifest.read_text() == before


def test_run_with_a_matching_freeze_writes_the_report(cc):
    root, manifest = _freeze_and_study(cc)
    rc, out = _run(cc, root, manifest)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text(), parse_constant=lambda c: pytest.fail(c))
    assert (out / "report.md").read_text().strip()
    assert rep["engram_alpha_commit"] == "abc1234"
    assert [f["fold"] for f in rep["folds"]] == [1]      # run evaluates only the fold it was asked for
    assert rep["freeze"]["code_sha256"] == json.loads(manifest.read_text())["code_sha256"]
    # planted clauses are in no number: 6 real clauses per sheet, none from the planted items
    for fold in rep["folds"]:
        assert sum(fold["test"]["consensus_counts"].values()) == 6


@pytest.mark.parametrize("field", ["code_sha256", "stopwords_sha256"])
def test_run_refuses_when_a_hash_differs_and_writes_nothing(cc, field):
    root, manifest = _freeze_and_study(cc)
    m = json.loads(manifest.read_text())
    m[field] = "0" * 64
    bad = root / "BAD.json"
    bad.write_text(json.dumps(m))
    rc, out = _run(cc, root, bad)
    assert rc == 2
    assert not out.exists()


@pytest.mark.parametrize("field,value", [("stemmer_suffixes", ["ing", "ed", "s"]),
                                         ("tuning_grid", {"start": 0.0, "stop": 1.0, "step": 0.05}),
                                         ("tie_rule", "largest")])
def test_run_refuses_when_a_parameter_differs(cc, field, value):
    root, manifest = _freeze_and_study(cc)
    m = json.loads(manifest.read_text())
    m[field] = value
    bad = root / "BAD.json"
    bad.write_text(json.dumps(m))
    rc, out = _run(cc, root, bad)
    assert rc == 2 and not out.exists()


def test_run_refuses_when_the_commit_differs_and_writes_nothing(cc):
    root, manifest = _freeze_and_study(cc)
    rc, out = _run(cc, root, manifest, commit="def5678")
    assert rc == 2
    assert not out.exists()


def test_run_refuses_to_overwrite_an_existing_report(cc):
    root, manifest = _freeze_and_study(cc)
    out = root / "out"
    out.mkdir()
    (out / "report.json").write_text("precious")
    rc, _ = _run(cc, root, manifest)
    assert rc == 2
    assert (out / "report.json").read_text() == "precious"
    assert not (out / "report.md").exists()


def test_run_refuses_on_a_return_hash_mismatch_and_writes_nothing(cc):
    root, manifest = _freeze_and_study(cc)
    hashes = json.loads((root / "hashes.json").read_text())
    hashes["sheet_two"][JB2]["pass2"] = "f" * 64
    (root / "hashes.json").write_text(json.dumps(hashes))
    rc, out = _run(cc, root, manifest)
    assert rc == 2 and not out.exists()


# =========================================================================== advise
def test_advise_lists_only_not_clauses_and_has_no_score(cc):
    quote = "Quillon merged the branch after review"
    clauses = ["Quillon merged the branch", "the branch merged on 2026-10-01", "Marrowby reviewed it"]
    out = cc.advise(quote, clauses, 1.0)
    assert out["of"] == 3
    assert out["not_stated"] == len(out["clauses"]) == 2
    assert [c["n"] for c in out["clauses"]] == [2, 3]
    assert out["clauses"][0] == {"n": 2, "text": clauses[1], "missing_type": "date"}
    assert out["clauses"][1]["text"] == clauses[2]
    assert out["hint"] == "widen the quote, or move the fact to the interpretation"
    assert set(out) == {"not_stated", "of", "clauses", "hint"}   # no node-level score or pass/fail field


def test_advise_with_everything_stated(cc):
    out = cc.advise("alpha bravo", ["alpha", "bravo"], 1.0)
    assert (out["not_stated"], out["of"], out["clauses"]) == (0, 2, [])


# =========================================================================== round 2 (design-owner tightenings)
# ---- (iii) the line as frozen text
LINE_SENTENCE = ("CARRIED only where the quoted text itself states every substantive element of the clause. "
                 "Where a clause added a relation, a date, a count or a cause the quote does not contain, "
                 "I labelled NOT even when the quote states the neighbouring half.")


def _line_sha(line):
    return hashlib.sha256(json.dumps(line, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def test_line_has_the_verbatim_sentence_its_source_and_one_listed_addition(cc):
    line = cc.LINE
    assert set(line) == {"sentence", "source", "additions"}          # exactly two parts, plus the source tag
    assert line["sentence"] == LINE_SENTENCE
    assert line["source"] == "#4050 comment 6397593890"
    assert line["additions"] == [{"element": "actor", "attribution": "Aleph, #4201"}]


def test_m1_docstring_cites_the_line(cc):
    assert "LINE" in cc.check_m1.__doc__


def test_manifest_records_the_line_hash(cc):
    root, manifest = _freeze_and_study(cc)
    assert json.loads(manifest.read_text())["line_sha256"] == _line_sha(cc.LINE)


def test_run_refuses_when_the_line_hash_differs_and_writes_nothing(cc):
    root, manifest = _freeze_and_study(cc)
    m = json.loads(manifest.read_text())
    m["line_sha256"] = "0" * 64                  # only the line differs; the code hash still matches
    bad = root / "BAD_LINE.json"
    bad.write_text(json.dumps(m))
    rc, out = _run(cc, root, bad)
    assert rc == 2 and not out.exists()


# ---- (ii) per-fold freeze
def _fold1_report(cc, root):
    _r, man = _freeze_and_study(cc, root=root)
    rc, out = _run(cc, root, man, out_name="fold1_out")
    assert rc == 0
    return out / "report.json"


def test_freeze_requires_a_fold_and_records_it(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    with pytest.raises(SystemExit):
        cc.main(["freeze", "--out", str(root / "F.json"), "--commit", "abc1234"])
    with pytest.raises(SystemExit):
        cc.main(["freeze", "--out", str(root / "F.json"), "--commit", "abc1234", "--fold", "3"])
    _r, man = _freeze_and_study(cc, root=root, fold=1)
    assert json.loads(man.read_text())["fold"] == 1


def test_run_requires_a_fold(cc):
    root, manifest = _freeze_and_study(cc)
    with pytest.raises(SystemExit):
        cc.main(["run", "--config", str(root / "config.json"), "--hashes", str(root / "hashes.json"),
                 "--freeze", str(manifest), "--out-dir", str(root / "o"), "--commit", "abc1234"])


def test_run_fold_1_with_a_fold_2_manifest_refuses_and_the_reverse(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    prior = _fold1_report(cc, root)
    _r, man2 = _freeze_and_study(cc, root=root, fold=2, prior=prior)
    rc, out = _run(cc, root, man2, out_name="wrong1", fold=1)
    assert rc == 2 and not out.exists()
    _r, man1 = _freeze_and_study(cc, root=root, fold=1)
    rc, out = _run(cc, root, man1, out_name="wrong2", fold=2)
    assert rc == 2 and not out.exists()


def test_fold_2_freeze_without_a_prior_report_refuses_and_writes_nothing(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    man = root / "F2.json"
    assert cc.main(["freeze", "--out", str(man), "--commit", "abc1234", "--fold", "2"]) == 2
    assert not man.exists()


def test_fold_2_manifest_records_the_prior_report_hash_and_run_reports_the_fold(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    prior = _fold1_report(cc, root)
    _r, man2 = _freeze_and_study(cc, root=root, fold=2, prior=prior)
    m = json.loads(man2.read_text())
    assert m["fold"] == 2
    assert m["prior_report_sha256"] == hashlib.sha256(prior.read_bytes()).hexdigest()
    rc, out = _run(cc, root, man2, out_name="fold2_out", fold=2)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    assert rep["fold"] == 2
    assert [f["fold"] for f in rep["folds"]] == [2]                   # only the fold it was asked for
    assert (rep["folds"][0]["tune_sheet"], rep["folds"][0]["test_sheet"]) == ("sheet_two", "sheet_one")
    assert rep["freeze_sha256"] == hashlib.sha256(man2.read_bytes()).hexdigest()
    assert rep["freeze"]["prior_report_sha256"] == m["prior_report_sha256"]


def test_run_fold_2_refuses_when_the_manifest_has_no_prior_report_hash(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    prior = _fold1_report(cc, root)
    _r, man2 = _freeze_and_study(cc, root=root, fold=2, prior=prior)
    m = json.loads(man2.read_text())
    del m["prior_report_sha256"]
    bad = root / "BAD_NOPRIOR.json"
    bad.write_text(json.dumps(m))
    rc, out = _run(cc, root, bad, out_name="noprior", fold=2)
    assert rc == 2 and not out.exists()


def test_run_fold_1_report_states_the_fold_and_the_manifest_hash(cc):
    root, manifest = _freeze_and_study(cc)
    rc, out = _run(cc, root, manifest)
    rep = json.loads((out / "report.json").read_text())
    assert rep["fold"] == 1
    assert rep["freeze_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert (rep["folds"][0]["tune_sheet"], rep["folds"][0]["test_sheet"]) == ("sheet_one", "sheet_two")
    assert "fold 1" in (out / "report.md").read_text().lower()


# ---- (i) exclusions
def _write_excl(root, entries, name=None):
    p = root / (name or f"excl_{next(_COUNTER)}.json")
    p.write_text(json.dumps({"exclude": entries}))
    return p


# sheet_two presentation numbers: item 1 = s2-a (2 clauses), item 4 = s2-b (2), item 5 = s2-c (2)
EXCL_OK = [{"sheet": "sheet_two", "item": 1, "clauses": [2]},
           {"sheet": "sheet_two", "item": 4, "clauses": "all"},
           {"sheet": "sheet_one", "item": 1, "clauses": [1]}]      # names the DEV sheet of fold 1


def test_exclusion_removes_only_test_fold_clauses_and_reports_counts(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    _r, man0 = _freeze_and_study(cc, root=root)
    rc, out0 = _run(cc, root, man0, out_name="base")
    base = json.loads((out0 / "report.json").read_text())["folds"][0]
    assert base["test"]["consensus_counts"] == {"NOT": 2, "STATED": 2, "BAND": 2}

    excl = _write_excl(root, EXCL_OK)
    _r, man = _freeze_and_study(cc, root=root, exclude=excl)
    rc, out = _run(cc, root, man, out_name="with_excl", exclude=excl)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    fold = rep["folds"][0]
    # test sheet lost item 1 clause 2 (NOT) and item 4 both clauses (BAND, STATED)
    assert fold["test"]["consensus_counts"] == {"NOT": 1, "STATED": 1, "BAND": 1}
    # the dev sheet is untouched: the tuned values equal the no-exclusion run's
    assert fold["tuned"] == base["tuned"]
    ex = fold["exclusion"]
    assert ex["sha256"] == hashlib.sha256(excl.read_bytes()).hexdigest()
    assert [(e["sheet"], e["item"], e["removed"]) for e in ex["entries"]] == \
        [("sheet_two", 1, 1), ("sheet_two", 4, 2), ("sheet_one", 1, 0)]
    assert ex["removed_total"] == 3
    assert rep["freeze"]["exclusion_sha256"] == ex["sha256"]


def test_exclusion_is_never_applied_to_a_dev_fold(cc):
    s0 = _sheet(cc, "one", _share_specs(3))     # items 1..10, j = (item-1) % 5; tuned theta 0.51
    s1 = _sheet(cc, "two", _share_specs(2))
    plain = {f["fold"]: f for f in cc.build_report([s0, s1], sim_fn=_stub_sim)["folds"]}
    # j=3 clauses of sheet 'one' (items 4 and 9): were they dropped from tuning, theta would move to 0.76
    entries = [{"sheet": "one", "item": 4, "clauses": "all"}, {"sheet": "one", "item": 9, "clauses": "all"}]
    rep = cc.build_report([s0, s1], sim_fn=_stub_sim, exclusions=entries, exclusion_sha256="e" * 64)
    f = {x["fold"]: x for x in rep["folds"]}
    assert f[1]["tuned"] == plain[1]["tuned"]                      # sheet 'one' is DEV in fold 1: untouched
    assert f[1]["tuned"]["M1"]["value"] == 0.51
    assert [e["removed"] for e in f[1]["exclusion"]["entries"]] == [0, 0]
    assert f[1]["test"]["consensus_counts"] == plain[1]["test"]["consensus_counts"]
    # in fold 2 'one' is the TEST sheet: the same entries now remove its clauses
    assert [e["removed"] for e in f[2]["exclusion"]["entries"]] == [1, 1]
    assert sum(f[2]["test"]["consensus_counts"].values()) == sum(plain[2]["test"]["consensus_counts"].values()) - 2
    assert f[2]["tuned"] == plain[2]["tuned"]


@pytest.mark.parametrize("entries", [
    [{"sheet": "sheet_two", "item": 99, "clauses": "all"}],            # no such item
    [{"sheet": "sheet_two", "item": 1, "clauses": [3]}],               # no such clause
    [{"sheet": "sheet_two", "item": 2, "clauses": "all"}],             # item 2 is a planted item, not a real one
    [{"sheet": "sheet_nine", "item": 1, "clauses": "all"}],            # no such sheet
    [{"sheet": "sheet_two", "item": 1, "clauses": [2]}, {"sheet": "sheet_two", "item": 5, "clauses": [7]}],
])
def test_exclusion_naming_something_absent_refuses_and_writes_nothing(cc, entries):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    excl = _write_excl(root, entries)
    _r, man = _freeze_and_study(cc, root=root, exclude=excl)
    rc, out = _run(cc, root, man, exclude=excl)
    assert rc == 2 and not out.exists()


@pytest.mark.parametrize("body", ["not json", json.dumps([1]), json.dumps({"exclude": "x"}),
                                  json.dumps({"exclude": [{"sheet": "sheet_two", "item": "1", "clauses": "all"}]}),
                                  json.dumps({"exclude": [{"sheet": "sheet_two", "item": 1, "clauses": "some"}]})])
def test_malformed_exclusion_file_refuses(cc, body):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    excl = root / "bad_excl.json"
    excl.write_text(body)
    assert cc.main(["freeze", "--out", str(root / "F.json"), "--commit", "abc1234", "--fold", "1",
                    "--exclude", str(excl)]) == 2


def test_run_refuses_when_the_exclusion_hash_differs_and_writes_nothing(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    frozen = _write_excl(root, [{"sheet": "sheet_two", "item": 1, "clauses": [2]}])
    other = _write_excl(root, [{"sheet": "sheet_two", "item": 1, "clauses": [1]}])
    _r, man = _freeze_and_study(cc, root=root, exclude=frozen)
    rc, out = _run(cc, root, man, out_name="o1", exclude=other)
    assert rc == 2 and not out.exists()
    rc, out = _run(cc, root, man, out_name="o2")                      # frozen with a list, run with none
    assert rc == 2 and not out.exists()
    _r, man_none = _freeze_and_study(cc, root=root)                   # frozen with none, run with a list
    rc, out = _run(cc, root, man_none, out_name="o3", exclude=frozen)
    assert rc == 2 and not out.exists()


# ---- flagged item 5: mask-token visibility
def test_report_counts_clauses_and_quotes_containing_the_mask_token(cc):
    s0 = {"name": "one", "records": [
        cc.make_record(1, 1, "saw [id] merge", "[id] merged", "CARRIED", "CARRIED"),
        cc.make_record(1, 2, "saw [id] merge", "it merged", "CARRIED", "CARRIED"),
        cc.make_record(2, 1, "a plain quote", "a plain clause", "NOT", "NOT")]}
    s1 = {"name": "two", "records": [cc.make_record(1, 1, "nothing", "nothing", "CARRIED", "CARRIED")]}
    rep = cc.build_report([s0, s1], sim_fn=_stub_sim)
    mc = rep["mask_counts"]
    assert mc["token"] == "[id]"
    assert mc["sheets"]["one"] == {"clauses": 3, "clauses_with_token": 1, "quotes": 2, "quotes_with_token": 1}
    assert mc["sheets"]["two"] == {"clauses": 1, "clauses_with_token": 0, "quotes": 1, "quotes_with_token": 0}


def test_run_report_carries_the_mask_counts(cc):
    root, manifest = _freeze_and_study(cc)
    rc, out = _run(cc, root, manifest)
    rep = json.loads((out / "report.json").read_text())
    assert set(rep["mask_counts"]["sheets"]) == {"sheet_one", "sheet_two"}
    assert rep["mask_counts"]["sheets"]["sheet_one"]["clauses"] == 6
    assert "[id]" in (out / "report.md").read_text()


# ---- flagged item 6: sentence-initial capital is not an actor
def test_sentence_initial_capital_is_not_typed_actor(cc):
    r = cc.check_m1("the branch was merged", "Reviewed branch merged", 1.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "other")      # 'reviewed' is absent, only capitalized by position


def test_sentence_initial_capital_that_recurs_capitalized_later_is_still_actor(cc):
    r = cc.check_m1("the branch was merged", "Quillon merged the branch and Quillon left", 1.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "actor")
    assert "quillon" in r["missing"]


# =========================================================================== round 3
# ---- 1. hard-token matching in M1 is token-boundary, not substring
def test_m1_number_404_is_not_present_in_4040(cc):
    r = cc.check_m1("error 4040 returned", "error 404 returned", 0.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "count")
    assert "404" in r["missing"]


def test_m1_issue_ref_123_is_not_present_in_1234(cc):
    r = cc.check_m1("see #1234 for details", "fixes #123", 0.0)
    assert (r["verdict"], r["missing_type"]) == ("NOT", "ref")
    assert "#123" in r["missing"]


def test_m1_percent_token_3_5_is_not_present_in_13_5(cc, monkeypatch):
    # the real tokenizer never emits a '%' token, so the carve-out is exercised through the matcher
    # directly and through check_m1 with the tokenizer stubbed to emit one
    assert cc._token_present(cc.normalize("grew 13.5% in 2025"), "3.5%") is False
    assert cc._token_present(cc.normalize("grew 3.5% in 2025"), "3.5%") is True
    monkeypatch.setattr(cc, "_clause_hard_tokens", lambda clause: [("3.5%", "number")])
    assert cc.check_m1("grew 13.5% in 2025", "grew 3.5%", 0.0)["verdict"] == "NOT"
    assert cc.check_m1("grew 3.5% in 2025", "grew 3.5%", 0.0)["verdict"] == "STATED"


def test_m1_hex_sha_is_present_when_bounded_by_punctuation(cc):
    r = cc.check_m1("fixed as 9ef92058.", "fixed in 9ef92058", 0.0)
    assert r["verdict"] == "STATED"
    assert cc.check_m1("fixed as 9ef920581", "fixed in 9ef92058", 0.0)["verdict"] == "NOT"   # longer run of word chars


@pytest.mark.parametrize("quote,token,present", [
    ("see #123,", "#123", True),
    ("see #1234", "#123", False),
    ("see 123", "#123", False),               # a leading '#' must match literally
    ("a#123", "#123", False),                 # glued to a word character on the left
    ("4040", "404", False),
    ("x 404", "404", True),
    ("404", "404", True),                      # start and end of the quote
    ("(404)", "404", True),
    ("a_404", "404", False),                  # '_' is a word character
    ("v 2026-10-01 ok", "2026-10-01", True),
    ("2026-10-011", "2026-10-01", False),
    ("rate 3.5% now", "3.5%", True),
    ("rate 3.5 now", "3.5%", False),          # a trailing '%' must match literally
    # round 4 (S1): for a digit-led token, '.' or ',' between two digits is part of a number, not a boundary
    ("error 4.404 seen", "404", False),       # preceded by digit-dot
    ("got 404.5 back", "404", False),         # followed by dot-digit
    ("saw 5,404 rows", "404", False),         # preceded by digit-comma
    ("saw 5,121 rows", "121", False),
    ("error 404.", "404", True),              # sentence-final period: no digit after it
    ("404, then", "404", True),               # comma, then a space
])
def test_token_boundary_matcher(cc, quote, token, present):
    assert cc._token_present(quote, token) is present


def test_m1_decimal_and_thousands_separators_are_not_boundaries(cc):
    # round 4 (S1), through check_m1: a clause number found inside a different quote number
    assert cc.check_m1("error 4.404 seen", "error 404", 0.0)["verdict"] == "NOT"
    assert cc.check_m1("saw 5,121 rows", "121 rows", 0.0)["verdict"] == "NOT"
    assert cc.check_m1("it said error 404.", "error 404", 0.0)["verdict"] == "STATED"


def test_b1_and_m1_differ_on_404_vs_4040(cc):
    quote, clause = "error 4040 returned", "error 404 returned"
    assert cc.check_b1(quote, clause)["verdict"] == "STATED"       # B1 keeps anchor()'s substring behaviour
    assert cc.check_m1(quote, clause, 0.0)["verdict"] == "NOT"
    assert "substring" in cc.check_b1.__doc__


# ---- 2. the freeze covers the dependencies
DEP_FIELDS = {"engram_anchoring_sha256": "engram_anchoring", "s2_score_sha256": "_s2"}


def _dep_path(cc, attr):
    return Path(getattr(cc, attr).__file__)


def test_manifest_pins_the_dependencies_from_the_running_modules(cc):
    root, manifest = _freeze_and_study(cc)
    m = json.loads(manifest.read_text())
    for field, attr in DEP_FIELDS.items():
        assert m[field] == hashlib.sha256(_dep_path(cc, attr).read_bytes()).hexdigest()


@pytest.mark.parametrize("field", sorted(DEP_FIELDS))
def test_run_refuses_when_a_dependency_hash_differs_in_the_manifest(cc, field):
    root, manifest = _freeze_and_study(cc)
    m = json.loads(manifest.read_text())
    m[field] = "0" * 64
    bad = root / "BAD_DEP.json"
    bad.write_text(json.dumps(m))
    rc, out = _run(cc, root, bad)
    assert rc == 2 and not out.exists()


@pytest.mark.parametrize("attr", sorted(DEP_FIELDS.values()))
def test_run_refuses_when_the_dependency_code_changed_after_the_freeze(cc, monkeypatch, attr):
    root, manifest = _freeze_and_study(cc)
    altered = root / f"altered_{attr}.py"
    altered.write_bytes(_dep_path(cc, attr).read_bytes() + b"\n# changed after the freeze\n")
    monkeypatch.setattr(getattr(cc, attr), "__file__", str(altered))
    rc, out = _run(cc, root, manifest)
    assert rc == 2 and not out.exists()


def _fixture_repo(cc):
    """A throwaway git repo holding copies of the tool and its two dependencies, committed clean."""
    repo = Path(TEST_DIR) / f"repo_{next(_COUNTER)}"
    repo.mkdir()
    paths = {"tool": repo / "tool.py", "engram_anchoring": repo / "anchoring.py", "_s2": repo / "score.py"}
    paths["tool"].write_bytes(_CHECK_PATH.read_bytes())
    for attr in ("engram_anchoring", "_s2"):
        paths[attr].write_bytes(_dep_path(cc, attr).read_bytes())
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    for argv in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "fixture"]):
        subprocess.run(git + argv, cwd=repo, check=True, capture_output=True)
    return repo, paths


def _point_at(cc, monkeypatch, paths):
    monkeypatch.setattr(cc, "__file__", str(paths["tool"]))
    for attr in ("engram_anchoring", "_s2"):
        monkeypatch.setattr(getattr(cc, attr), "__file__", str(paths[attr]))
    monkeypatch.setattr(cc, "_require_clean_tree", cc._orig_require_clean_tree)


def test_freeze_succeeds_in_a_clean_git_tree(cc, monkeypatch):
    repo, paths = _fixture_repo(cc)
    _point_at(cc, monkeypatch, paths)
    assert cc.main(["freeze", "--out", str(repo.parent / "clean_F.json"), "--commit", "abc1234", "--fold", "1"]) == 0


@pytest.mark.parametrize("which", ["tool", "engram_anchoring", "_s2"])
def test_freeze_refuses_when_one_of_the_three_files_is_dirty(cc, monkeypatch, which):
    repo, paths = _fixture_repo(cc)
    paths[which].write_bytes(paths[which].read_bytes() + b"\n# uncommitted edit\n")
    _point_at(cc, monkeypatch, paths)
    out = repo.parent / f"dirty_F_{which}.json"
    assert cc.main(["freeze", "--out", str(out), "--commit", "abc1234", "--fold", "1"]) == 2
    assert not out.exists()


def test_freeze_refuses_when_the_tool_is_not_inside_a_git_work_tree(cc, monkeypatch):
    plain = Path(TEST_DIR) / f"plain_{next(_COUNTER)}"
    plain.mkdir()
    paths = {"tool": plain / "tool.py", "engram_anchoring": plain / "anchoring.py", "_s2": plain / "score.py"}
    paths["tool"].write_bytes(_CHECK_PATH.read_bytes())
    for attr in ("engram_anchoring", "_s2"):
        paths[attr].write_bytes(_dep_path(cc, attr).read_bytes())
    _point_at(cc, monkeypatch, paths)
    out = plain / "F.json"
    assert cc.main(["freeze", "--out", str(out), "--commit", "abc1234", "--fold", "1"]) == 2
    assert not out.exists()


# ---- 3. fold-2 freeze compares the code across folds
HASH_FIELDS = ["code_sha256", "engram_anchoring_sha256", "s2_score_sha256", "line_sha256", "stopwords_sha256",
               "stemmer_suffixes", "tuning_grid", "embedding_model"]


def _tampered_prior(root, prior, mutate, name):
    rep = json.loads(Path(prior).read_text())
    mutate(rep)
    p = root / name
    p.write_text(json.dumps(rep))
    return p


def test_fold_1_report_carries_the_manifest_hash_fields(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    rep = json.loads(_fold1_report(cc, root).read_text())
    for f in HASH_FIELDS:
        assert f in rep["freeze"]


def test_fold_2_freeze_with_unchanged_code_records_false(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    prior = _fold1_report(cc, root)
    _r, man2 = _freeze_and_study(cc, root=root, fold=2, prior=prior)
    m = json.loads(man2.read_text())
    assert m["code_changed_since_fold1"] is False
    assert m["changed_fields_since_fold1"] == []
    rc, out = _run(cc, root, man2, out_name="f2_same", fold=2)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    assert rep["code_changed_since_fold1"] is False and rep["changed_fields_since_fold1"] == []
    head = "\n".join((out / "report.md").read_text().splitlines()[:5]).lower()
    assert "code changed since fold 1: false" in head


@pytest.mark.parametrize("field,value", [("code_sha256", "1" * 64), ("engram_anchoring_sha256", "2" * 64),
                                         ("s2_score_sha256", "3" * 64), ("line_sha256", "4" * 64),
                                         ("stopwords_sha256", "5" * 64), ("stemmer_suffixes", ["ing", "s"]),
                                         ("tuning_grid", {"start": 0.0, "stop": 1.0, "step": 0.05}),
                                         ("embedding_model", "synthetic-model-prior")])
def test_fold_2_freeze_records_true_and_names_the_one_changed_field(cc, field, value):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    prior = _tampered_prior(root, _fold1_report(cc, root), lambda r: r["freeze"].__setitem__(field, value),
                            f"prior_{field}.json")
    _r, man2 = _freeze_and_study(cc, root=root, fold=2, prior=prior)
    m = json.loads(man2.read_text())
    assert m["code_changed_since_fold1"] is True
    assert m["changed_fields_since_fold1"] == [field]
    rc, out = _run(cc, root, man2, out_name=f"f2_{field}", fold=2)      # visibility, not refusal
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    assert rep["code_changed_since_fold1"] is True and rep["changed_fields_since_fold1"] == [field]
    head = "\n".join((out / "report.md").read_text().splitlines()[:5]).lower()
    assert "code changed since fold 1: true" in head and field in head


@pytest.mark.parametrize("mutate", [
    lambda r: r.pop("freeze"),
    lambda r: r["freeze"].pop("code_sha256"),
    lambda r: r["freeze"].pop("s2_score_sha256"),
    lambda r: r["freeze"].pop("tuning_grid"),
])
def test_fold_2_freeze_refuses_when_the_prior_report_lacks_hash_fields(cc, mutate):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    prior = _tampered_prior(root, _fold1_report(cc, root), mutate, f"prior_bad_{next(_COUNTER)}.json")
    man = root / f"F2_{next(_COUNTER)}.json"
    assert cc.main(["freeze", "--out", str(man), "--commit", "abc1234", "--fold", "2",
                    "--prior-report", str(prior)]) == 2
    assert not man.exists()


# ---- 4. a missing quote refuses
def _drop_quote(root, sheet_idx, item_num):
    d = root / f"sheet{sheet_idx}"
    p = d / "sheet-pass1.md"
    text = p.read_text()
    new, n = re.subn(rf"(## Item {item_num}\n\n\*\*Quote:\*\*\n)(?:>[^\n]*\n)+", r"\1", text)
    assert n == 1
    p.write_text(new)
    key = json.loads((d / "key.json").read_text())
    key["render"]["sha256"]["sheet-pass1.md"] = _sha(new)      # keep the sheet's hash valid: only the quote is gone
    (d / "key.json").write_text(json.dumps(key))


def test_load_study_refuses_a_real_item_with_no_quote_lines_naming_sheet_and_item(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    _drop_quote(root, 1, 4)           # sheet_two, presentation item 4 (a real item)
    with pytest.raises(cc.Refusal) as ei:
        cc.load_study(root / "config.json", root / "hashes.json")
    msg = str(ei.value)
    assert "sheet_two" in msg and "4" in msg and "quote" in msg.lower()


def test_run_refuses_and_writes_nothing_when_a_quote_is_missing(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    _drop_quote(root, 0, 1)
    _r, man = _freeze_and_study(cc, root=root)
    rc, out = _run(cc, root, man)
    assert rc == 2 and not out.exists()


# ---- 5. stemmer rules
@pytest.mark.parametrize("word,want", [
    ("process", "process"), ("processes", "process"), ("classes", "class"), ("class", "class"),
    ("status", "status"), ("bus", "bus"), ("analysis", "analysis"),
    ("boxes", "box"), ("buzzes", "buzz"), ("churches", "church"), ("wishes", "wish"),
    ("merged", "merg"), ("merging", "merg"), ("merges", "merg"),
    ("running", "runn"), ("cats", "cat"), ("bed", "bed"), ("sees", "see"), ("branch", "branch"),
])
def test_stemmer_round_3_outputs(cc, word, want):
    assert cc.stem(word) == want


# round 4 (S2): -ses/-zes/-ces/-ges plurals meet their singular; the guard keeps the old outcomes
@pytest.mark.parametrize("word,want", [
    ("cases", "cas"), ("releases", "releas"), ("sizes", "siz"), ("pages", "pag"),   # round 5: final e dropped too
    ("services", "servic"),
    ("buses", "bus"), ("statuses", "status"),     # base ends us: the es rule still applies
])
def test_stemmer_round_4_outputs(cc, word, want):
    assert cc.stem(word) == want


@pytest.mark.parametrize("plural,singular", [
    ("cases", "case"), ("releases", "release"), ("sizes", "size"), ("pages", "page"),
    ("services", "service"), ("classes", "class"), ("processes", "process"),
])
def test_stemmer_plural_and_singular_collide(cc, plural, singular):
    assert cc.stem(plural) == cc.stem(singular)


# round 5 (S2): one final step in stem(): a result ending in 'e' drops it when >= MIN_STEM chars remain
@pytest.mark.parametrize("forms", [
    ("merge", "merged", "merges", "merging"), ("case", "cases"), ("release", "releases"), ("size", "sizes"),
    ("page", "pages"), ("service", "services"), ("cache", "caches"), ("house", "houses"),
    ("class", "classes"), ("process", "processes"), ("test", "tests"), ("note", "notes"),
    ("state", "stated", "states"),
])
def test_stemmer_round_5_word_families_collide(cc, forms):
    assert len({cc.stem(w) for w in forms}) == 1, {w: cc.stem(w) for w in forms}


def test_stemmer_caches_and_cache_now_collide(cc):
    # was the round-4 residual (caches -> cach vs cache); the round-5 final-e drop makes them meet.
    assert cc.stem("caches") == cc.stem("cache") == "cach"


def test_stemmer_houses_and_house_now_collide(cc):
    assert cc.stem("houses") == cc.stem("house") == "hous"


def test_stemmer_final_e_drop_keeps_min_stem(cc):
    assert cc.stem("use") == "use"      # dropping e would leave 2 chars < MIN_STEM
    assert cc.stem("the") == "the"
    assert cc.stem("sees") == "see"
    assert cc.stem("rule") == "rul"     # 4 chars -> 3 left: dropped


def test_stemmer_known_residual_lenses_vs_lens_does_not_collide(cc):
    # documented residual: lenses -> lens (the 'us'/'is'-style guard does not apply, es->s then e drop), but lens -> len.
    assert cc.stem("lenses") == "lens"
    assert cc.stem("lens") == "len"
    assert cc.stem("lenses") != cc.stem("lens")


def test_process_and_processes_collide(cc):
    assert cc.stem("process") == cc.stem("processes")


# =========================================================================== #4201 phase 1b
# ---- 5. freeze verification happens before any label is read
# The functions that read returns or labels, patched (recording) in the order cells below: load_study
# (the entry cmd_run calls), and the s2_score loaders it delegates to -- _load_sheet_inputs (opens the
# sheets and return paths), parse_return and _labels_of (parse the judges' labels) -- plus build_report
# (consumes the labels). If verify_freeze is moved after load_study, a bad manifest reaches these first.
_LABEL_READERS = [("cc", "load_study"), ("cc", "build_report"), ("_s2", "_load_sheet_inputs"),
                  ("_s2", "parse_return"), ("_s2", "_labels_of")]


def _record_label_readers(cc, monkeypatch):
    calls = []
    for owner, name in _LABEL_READERS:
        mod = cc if owner == "cc" else cc._s2
        orig = getattr(mod, name)

        def wrapper(*a, _orig=orig, _name=name, **k):
            calls.append(_name)
            return _orig(*a, **k)

        monkeypatch.setattr(mod, name, wrapper)
    return calls


def test_order_bad_code_hash_refuses_before_any_label_is_read(cc, monkeypatch):
    root, manifest = _freeze_and_study(cc)
    m = json.loads(manifest.read_text())
    m["code_sha256"] = "0" * 64
    bad = root / "BAD_ORDER_CODE.json"
    bad.write_text(json.dumps(m))
    calls = _record_label_readers(cc, monkeypatch)
    rc, out = _run(cc, root, bad)
    assert rc == 2 and not out.exists()
    assert calls == []


def test_order_wrong_fold_refuses_before_any_label_is_read(cc, monkeypatch):
    root, manifest = _freeze_and_study(cc, fold=1)
    calls = _record_label_readers(cc, monkeypatch)
    rc, out = _run(cc, root, manifest, fold=2)         # a fold-1 manifest, run asked for fold 2
    assert rc == 2 and not out.exists()
    assert calls == []


def test_order_positive_control_a_valid_manifest_reads_the_labels_once(cc, monkeypatch):
    root, manifest = _freeze_and_study(cc)
    calls = _record_label_readers(cc, monkeypatch)
    rc, out = _run(cc, root, manifest)
    assert rc == 0 and out.exists()
    assert calls.count("load_study") == 1
    assert calls.count("build_report") == 1


# ---- 6. the embedding model is in the freeze, pinned at run, and reported
def _set_model(cc, monkeypatch, model):
    """Make the seat's embedding config resolve to ``model`` (None: no "model" key at all)."""
    cfg = {} if model is None else {"model": model}
    monkeypatch.setattr(cc.engram_anchoring.core, "_get_embedding_config", lambda: dict(cfg))


def test_embedding_model_helper_resolves_as_quote_claim_similarity_does(cc, monkeypatch):
    core = cc.engram_anchoring.core
    _set_model(cc, monkeypatch, "synthetic-model-a")
    assert cc._embedding_model() == "synthetic-model-a"
    _set_model(cc, monkeypatch, None)
    assert cc._embedding_model() == core.DEFAULT_EMBEDDING_MODEL


def test_manifest_records_the_embedding_model(cc, monkeypatch):
    _set_model(cc, monkeypatch, "synthetic-model-a")
    root, manifest = _freeze_and_study(cc)
    assert json.loads(manifest.read_text())["embedding_model"] == "synthetic-model-a"


def test_run_refuses_when_the_seats_embedding_model_differs_and_writes_nothing(cc, monkeypatch, capsys):
    _set_model(cc, monkeypatch, "synthetic-model-a")
    root, manifest = _freeze_and_study(cc)
    _set_model(cc, monkeypatch, "synthetic-model-b")
    calls = _record_label_readers(cc, monkeypatch)
    rc, out = _run(cc, root, manifest)
    assert rc == 2 and not out.exists()
    assert "embedding_model" in capsys.readouterr().err
    assert calls == []


def test_run_report_carries_the_embedding_model(cc, monkeypatch):
    _set_model(cc, monkeypatch, "synthetic-model-a")
    root, manifest = _freeze_and_study(cc)
    rc, out = _run(cc, root, manifest)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    assert rep["embedding_model"] == "synthetic-model-a"
    head = "\n".join((out / "report.md").read_text().splitlines()[:6])
    assert "synthetic-model-a" in head
