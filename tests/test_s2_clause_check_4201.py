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
import builtins
import copy
import hashlib
import importlib.util
import io
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


# =========================================================================== #4201 phase 3a
# The deterministic clause splitter (B), its measurement harness (split-eval) and the end-to-end mode
# (run --clauses split). Design-owner ruling: #4201 comment 6424929510. Synthetic fixtures only.

# ---- splitter: rule (a), sentences
def test_splitter_version_and_manifest_pin(cc):
    assert cc.SPLITTER_VERSION == "B1"
    root, manifest = _freeze_and_study(cc)
    assert json.loads(manifest.read_text())["splitter_version"] == "B1"


def test_rule_a_sentence_boundaries_split(cc):
    assert cc.split_claim("The suite ran. The build passed.") == ["The suite ran", "The build passed"]
    assert cc.split_claim("Did the suite run? The build passed!") == ["Did the suite run", "The build passed"]
    assert cc.split_claim("Quillon merged the branch; Marrowby approved it") == [
        "Quillon merged the branch", "Marrowby approved it"]


def test_rule_a_a_number_is_not_a_boundary(cc):
    assert cc.split_claim("the rate was 3.5 percent in the run") == ["the rate was 3.5 percent in the run"]
    assert cc.split_claim("The run took 3.5 hours. It passed.") == ["The run took 3.5 hours", "It passed"]


@pytest.mark.parametrize("text", [
    "reviewers (e.g. Quillon) approved it",
    "it holds, i.e. the cache is warm",
    "the cache vs. the queue was slower",
    "see cf. the earlier note for details",
])
def test_rule_a_an_abbreviation_is_not_a_boundary(cc, text):
    assert cc.split_claim(text) == [text]


def test_rule_a_a_code_span_is_not_split(cc):
    assert cc.split_claim("`a. b; c` was logged") == ["`a. b; c` was logged"]
    assert cc.split_claim("the line `x. y` ran. Then it stopped.") == ["the line `x. y` ran", "Then it stopped"]


# ---- splitter: rule (b), coordination
def test_rule_b_verb_on_both_sides_splits(cc):
    assert cc.split_claim("Quillon merged the branch as hotfix and Marrowby approved it") == [
        "Quillon merged the branch as hotfix", "Marrowby approved it"]
    assert cc.split_claim("the suite passed, and the build failed") == ["the suite passed", "the build failed"]
    assert cc.split_claim("the suite passed but the build failed") == ["the suite passed", "the build failed"]


def test_rule_b_hard_token_on_both_sides_splits(cc):
    assert cc.split_claim("build 4812 and ticket #123") == ["build 4812", "ticket #123"]


def test_rule_b_no_verb_or_hard_token_on_one_side_does_not_split(cc):
    assert cc.split_claim("the queue and the cache were drained") == ["the queue and the cache were drained"]
    assert cc.split_claim("tests ran against build 4812 and docs") == ["tests ran against build 4812 and docs"]
    assert cc.split_claim("bread and butter") == ["bread and butter"]


def test_rule_b_a_connector_inside_a_code_span_is_not_split(cc):
    assert cc.split_claim("the line `x was set and y was read` ran") == ["the line `x was set and y was read` ran"]


def test_rule_b_a_list_keeps_a_verbless_item_with_its_neighbour(cc):
    # the middle piece has no verb or hard token, so it merges into the piece before it
    assert cc.split_claim("Quillon ran the suite and the cache and Marrowby approved it") == [
        "Quillon ran the suite and the cache", "Marrowby approved it"]


# ---- splitter: rule (c), trailing qualifier clauses
@pytest.mark.parametrize("intro", ["after", "because", "so that", "which"])
def test_rule_c_qualifier_splits_when_both_sides_have_three_content_stems(cc, intro):
    left = "Quillon merged the hotfix branch"
    right = f"{intro} reviewers finished careful inspection"
    assert cc.split_claim(f"{left} {right}") == [left, right]


@pytest.mark.parametrize("intro", ["after", "because", "so that", "which"])
def test_rule_c_a_short_side_blocks_the_split(cc, intro):
    short_right = f"Quillon merged the hotfix branch {intro} review"
    short_left = f"merged {intro} reviewers finished careful inspection"
    assert cc.split_claim(short_right) == [short_right]
    assert cc.split_claim(short_left) == [short_left]


# ---- splitter: rule (d), strip and drop empties
def test_rule_d_strips_punctuation_and_drops_empty_pieces(cc):
    assert cc.split_claim("  ...The suite ran,  ") == ["The suite ran"]
    assert cc.split_claim("The suite ran.; ; The build passed.") == ["The suite ran", "The build passed"]
    assert cc.split_claim("") == []
    assert cc.split_claim("...") == []


def test_split_claim_is_deterministic_and_makes_no_call(cc, monkeypatch):
    # no model, no network: the embedder is stubbed away in the fixture and must stay unreached
    text = "Quillon merged the hotfix branch after reviewers finished careful inspection. Marrowby approved it"
    first = cc.split_claim(text)
    assert cc.split_claim(text) == first
    assert len(first) == 3


# ---- claims input
def _add_claims(root, per_sheet, drop_key_for=None):
    """Write a claims.json beside each sheet and point the config at it (the list shape s2_clause_sheet
    `select` writes: [{"node_id", "claim"}])."""
    cfg = json.loads((root / "config.json").read_text())
    for si, (sh, claims) in enumerate(zip(cfg["sheets"], per_sheet)):
        p = root / f"sheet{si}" / "claims.json"
        p.write_text(json.dumps([{"node_id": i, "claim": c} for i, c in claims.items()]))
        if drop_key_for != si:
            sh["claims"] = str(p)
    (root / "config.json").write_text(json.dumps(cfg))


E2E_CLAIMS_ONE = {"s1-a": "Quillon merged the branch. The branch merged on 2026-10-02",
                  "s1-b": "the suite ran 4812 cells and Marrowby approved the cells",
                  "s1-c": "the queue was drained"}
E2E_CLAIMS_TWO = {"s2-a": "the fix landed in #123. the fix landed in #124",
                  "s2-b": "bravo delta were mentioned together",
                  "s2-c": "the cache was cleared. zebra yak"}


def _e2e_root():
    root = _write_study([SHEET_ONE, SHEET_TWO])
    _add_claims(root, [E2E_CLAIMS_ONE, E2E_CLAIMS_TWO])
    return root


THETA = 0.46        # the shipped value: the gated end-to-end column evaluates M1 here and never tunes it


def _freeze_split(cc, root, fold=1, exclude=None, prior=None, theta=THETA):
    manifest = root / f"FREEZE_SPLIT{next(_COUNTER)}.json"
    argv = ["freeze", "--out", str(manifest), "--commit", "abc1234", "--fold", str(fold),
            "--clauses", "split", "--config", str(root / "config.json")]
    if theta is not None:
        argv += ["--theta", str(theta)]
    if exclude:
        argv += ["--exclude", str(exclude)]
    if prior:
        argv += ["--prior-report", str(prior)]
    assert cc.main(argv) == 0
    return manifest


def _run_split(cc, root, manifest, out_name="out_split", fold=1, exclude=None, clauses="split", theta=THETA):
    out = root / out_name
    argv = ["run", "--config", str(root / "config.json"), "--hashes", str(root / "hashes.json"),
            "--freeze", str(manifest), "--out-dir", str(out), "--commit", "abc1234", "--fold", str(fold),
            "--clauses", clauses]
    if theta is not None and clauses == "split":
        argv += ["--theta", str(theta)]
    if exclude:
        argv += ["--exclude", str(exclude)]
    return cc.main(argv), out


def test_load_claims_joins_by_node_id_and_reports_the_sha(cc):
    root = _e2e_root()
    sheets = cc.load_study(root / "config.json", root / "hashes.json")
    claims = cc.load_claims(root / "config.json", sheets)
    assert claims["sheet_one"] == E2E_CLAIMS_ONE
    assert claims["sheet_two"] == E2E_CLAIMS_TWO


def test_load_claims_refuses_a_sheet_with_no_claims_path_and_names_it(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    _add_claims(root, [E2E_CLAIMS_ONE, E2E_CLAIMS_TWO], drop_key_for=1)
    sheets = cc.load_study(root / "config.json", root / "hashes.json")
    with pytest.raises(cc.Refusal, match="sheet_two"):
        cc.load_claims(root / "config.json", sheets)


def test_load_claims_refuses_missing_and_extra_ids_and_lists_them(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    short = dict(E2E_CLAIMS_ONE)
    del short["s1-b"]
    short["zz-extra"] = "an invented node"
    _add_claims(root, [short, E2E_CLAIMS_TWO])
    sheets = cc.load_study(root / "config.json", root / "hashes.json")
    with pytest.raises(cc.Refusal) as ei:
        cc.load_claims(root / "config.json", sheets)
    msg = str(ei.value)
    assert "sheet_one" in msg and "s1-b" in msg and "zz-extra" in msg


def test_load_claims_refuses_a_planted_id_and_a_duplicate(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    with_planted = dict(E2E_CLAIMS_ONE)
    with_planted["plant-P1"] = "planted claims are not claims of real items"
    _add_claims(root, [with_planted, E2E_CLAIMS_TWO])
    sheets = cc.load_study(root / "config.json", root / "hashes.json")
    with pytest.raises(cc.Refusal, match="plant-P1"):
        cc.load_claims(root / "config.json", sheets)
    root2 = _write_study([SHEET_ONE, SHEET_TWO])
    _add_claims(root2, [E2E_CLAIMS_ONE, E2E_CLAIMS_TWO])
    p = root2 / "sheet0" / "claims.json"
    rows = json.loads(p.read_text())
    rows.append({"node_id": "s1-a", "claim": "a second claim for the same node"})
    p.write_text(json.dumps(rows))
    sheets2 = cc.load_study(root2 / "config.json", root2 / "hashes.json")
    with pytest.raises(cc.Refusal, match="s1-a"):
        cc.load_claims(root2 / "config.json", sheets2)


# ---- split-eval: a synthetic two-sheet fixture with known answers
SEED_HEX = "d638538e"
# kind -> (claim, frozen clauses); matched = the counts agree
_SE_KINDS = {
    "two": ("Alpha ran. Bravo ran.", ["Alpha ran", "Bravo ran"]),          # agree, jaccard 1, 1
    "one": ("Delta ran", ["Delta ran"]),                                    # agree, jaccard 1
    "mis": ("Echo ran", ["Echo ran", "Foxtrot ran"]),                       # count disagrees
    "near": ("Delta ran quickly", ["Delta ran"]),                           # agree, jaccard 2/3
    "far": ("Golf hotel india", ["Juliet kilo lima"]),                      # agree, jaccard 0
}
_SE_AGREE = {"two", "one", "near", "far"}
_SE_J = {"two": [1.0, 1.0], "one": [1.0], "near": [2 / 3], "far": [0.0]}
_SE_KINDS_ONE = ["two", "one", "mis", "near", "far", "two", "one", "mis", "two", "one", "near", "mis"]
_SE_KINDS_TWO = ["mis", "mis", "one", "far", "two", "near", "mis", "one", "far", "two", "one", "two"]


def _se_ids(prefix):
    return [f"{prefix}{i:02d}" for i in range(1, 13)]


def _se_study(kinds_one=None, kinds_two=None):
    kinds = [kinds_one or _SE_KINDS_ONE, kinds_two or _SE_KINDS_TWO]
    real, claims = [], []
    for prefix, ks in zip(("qa", "qb"), kinds):
        items, cl = [], {}
        for iid, k in zip(_se_ids(prefix), ks):
            claim, frozen = _SE_KINDS[k]
            items.append((iid, "a synthetic quote", [(c, "NOT", "NOT") for c in frozen]))
            cl[iid] = claim
        real.append(items)
        claims.append(cl)
    root = _write_study(real)
    _add_claims(root, claims)
    return root, kinds


def _first_ten(ids):
    """Independent re-derivation of the stage-1 selection: the first 10 by sha256(seed_hex || id)."""
    return sorted(ids, key=lambda i: hashlib.sha256((SEED_HEX + i).encode("utf-8")).hexdigest())[:10]


def _se_item_number(root, si, iid):
    key = json.loads((root / f"sheet{si}" / "key.json").read_text())
    return next(int(n) for n, v in key["render"]["items"].items() if v["id"] == iid)


def _split_eval(cc, root, bar, out_name="se_out"):
    out = root / out_name
    rc = cc.main(["split-eval", "--config", str(root / "config.json"), "--bar", json.dumps(bar),
                  "--out-dir", str(out)])
    return rc, out


def _se_expected(kinds, ids):
    agree = [k in _SE_AGREE for k in kinds]
    ft = set(_first_ten(ids))
    ft_agree = [a for a, i in zip(agree, ids) if i in ft]
    js = [j for k in kinds if k in _SE_AGREE for j in _SE_J[k]]
    return {"n": len(ids), "agree": sum(agree), "rate": sum(agree) / len(ids),
            "ft_n": len(ft_agree), "ft_agree": sum(ft_agree), "ft_rate": sum(ft_agree) / len(ft_agree),
            "pairs": len(js), "mean_j": sum(js) / len(js), "share": sum(1 for j in js if j >= 0.5) / len(js)}


def test_split_eval_count_agreement_and_boundary_jaccard_match_the_known_answers(cc):
    root, kinds = _se_study()
    rc, out = _split_eval(cc, root, {"sheet_one": 0.0, "sheet_two": 0.0})
    assert rc == 0
    rep = json.loads((out / "report-split.json").read_text())
    for name, ks, prefix in (("sheet_one", kinds[0], "qa"), ("sheet_two", kinds[1], "qb")):
        want = _se_expected(ks, _se_ids(prefix))
        got = rep["sheets"][name]
        assert got["n_items"] == want["n"] == 12
        assert got["count_agree"] == want["agree"]
        assert got["count_agree_rate"] == pytest.approx(want["rate"])
        assert got["boundary"]["n_pairs"] == want["pairs"]
        assert got["boundary"]["mean_jaccard"] == pytest.approx(want["mean_j"])
        assert got["boundary"]["share_ge_0_5"] == pytest.approx(want["share"])
    pooled_agree = sum(_se_expected(k, _se_ids(p))["agree"] for k, p in ((kinds[0], "qa"), (kinds[1], "qb")))
    assert rep["pooled"]["count_agree"] == pooled_agree
    assert rep["pooled"]["n_items"] == 24
    assert rep["pooled"]["count_agree_rate"] == pytest.approx(pooled_agree / 24)
    assert (out / "report-split.md").read_text().strip()


def test_split_eval_first_ten_uses_the_sort_rule(cc):
    root, kinds = _se_study()
    ids_one = _se_ids("qa")
    assert _first_ten(ids_one) != ids_one[:10]           # the control: the sort rule is not id order
    rc, out = _split_eval(cc, root, {"sheet_one": 0.0, "sheet_two": 0.0})
    rep = json.loads((out / "report-split.json").read_text())
    for si, (name, ks, prefix) in enumerate((("sheet_one", kinds[0], "qa"), ("sheet_two", kinds[1], "qb"))):
        want = _se_expected(ks, _se_ids(prefix))
        ft = rep["sheets"][name]["first_ten"]
        assert ft["items"] == sorted(_se_item_number(root, si, i) for i in _first_ten(_se_ids(prefix)))
        assert ft["n"] == want["ft_n"] == 10
        assert ft["count_agree"] == want["ft_agree"]
        assert ft["count_agree_rate"] == pytest.approx(want["ft_rate"])
    # at least one sheet's first-ten rate differs from its all-items rate, so a wrong selection would show
    assert any(rep["sheets"][n]["first_ten"]["count_agree_rate"] != rep["sheets"][n]["count_agree_rate"]
               for n in ("sheet_one", "sheet_two"))


def test_split_eval_sort_hash_is_the_sheet_builders(cc):
    spec = importlib.util.spec_from_file_location(f"s2_clause_sheet_{next(_COUNTER)}", _TOOLS / "s2_clause_sheet.py")
    sheet_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sheet_mod)
    for ident in ("qa01", "qb07", "zz-9"):
        assert cc._sort_hash(SEED_HEX, ident) == sheet_mod.sort_hash(SEED_HEX, ident)


def test_split_eval_bar_passes_and_fails_per_sheet(cc):
    root, kinds = _se_study()
    r1 = _se_expected(kinds[0], _se_ids("qa"))["rate"]
    r2 = _se_expected(kinds[1], _se_ids("qb"))["rate"]
    rc, out = _split_eval(cc, root, {"sheet_one": r1, "sheet_two": r2}, "se_pass")     # at the bar passes
    assert rc == 0
    rep = json.loads((out / "report-split.json").read_text())
    assert rep["sheets"]["sheet_one"]["pass"] is True and rep["sheets"]["sheet_two"]["pass"] is True
    assert rep["verdict"]["pass"] is True
    rc, out = _split_eval(cc, root, {"sheet_one": r1, "sheet_two": r2 + 0.01}, "se_fail")
    rep = json.loads((out / "report-split.json").read_text())
    assert rep["sheets"]["sheet_one"]["pass"] is True and rep["sheets"]["sheet_two"]["pass"] is False
    assert rep["verdict"]["pass"] is False
    assert rep["bar"] == {"sheet_one": r1, "sheet_two": r2 + 0.01}
    assert "FAIL" in (out / "report-split.md").read_text()


@pytest.mark.parametrize("bar", [{"sheet_one": 0.5}, {"sheet_one": 0.5, "sheet_two": 0.6, "extra": 0.1},
                                 {"sheet_one": "high", "sheet_two": 0.6}, {"sheet_one": 1.5, "sheet_two": 0.6},
                                 [0.5, 0.6]])
def test_split_eval_refuses_a_malformed_bar_and_writes_nothing(cc, bar):
    root, _kinds = _se_study()
    rc, out = _split_eval(cc, root, bar)
    assert rc == 2 and not out.exists()


def test_split_eval_refuses_when_a_sheet_has_no_claims_and_does_not_overwrite(cc, capsys):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    _add_claims(root, [E2E_CLAIMS_ONE, E2E_CLAIMS_TWO], drop_key_for=0)
    rc, out = _split_eval(cc, root, {"sheet_one": 0.5, "sheet_two": 0.5})
    assert rc == 2 and not out.exists()
    assert "sheet_one" in capsys.readouterr().err
    root2, _k = _se_study()
    rc, out2 = _split_eval(cc, root2, {"sheet_one": 0.0, "sheet_two": 0.0})
    assert rc == 0
    before = (out2 / "report-split.json").read_text()
    rc, _ = _split_eval(cc, root2, {"sheet_one": 0.9, "sheet_two": 0.9})
    assert rc == 2
    assert (out2 / "report-split.json").read_text() == before


def _record_opened_paths(monkeypatch):
    """Record every path opened through the ways the tool reads files (builtins.open, io.open, Path.open,
    Path.read_bytes, Path.read_text), resolved, so a return is recognised by its full path and not by how a
    fixture happens to name it."""
    opened = []
    real_open, real_io_open = builtins.open, io.open
    real_popen, real_rb, real_rt = Path.open, Path.read_bytes, Path.read_text

    def note(p):
        if isinstance(p, (str, bytes, os.PathLike)):
            opened.append(os.path.realpath(os.fsdecode(p)))

    def w_open(f, *a, **k):
        note(f)
        return real_open(f, *a, **k)

    def w_io_open(f, *a, **k):
        note(f)
        return real_io_open(f, *a, **k)

    def w_popen(self, *a, **k):
        note(self)
        return real_popen(self, *a, **k)

    def w_rb(self):
        note(self)
        return real_rb(self)

    def w_rt(self, *a, **k):
        note(self)
        return real_rt(self, *a, **k)

    monkeypatch.setattr(builtins, "open", w_open)
    monkeypatch.setattr(io, "open", w_io_open)
    monkeypatch.setattr(Path, "open", w_popen)
    monkeypatch.setattr(Path, "read_bytes", w_rb)
    monkeypatch.setattr(Path, "read_text", w_rt)
    return opened


def _return_paths_in(root):
    cfg = json.loads((root / "config.json").read_text())
    paths = [p for sh in cfg["sheets"] for judge in sh["returns"].values() for p in judge.values()]
    assert len(paths) == 8 and all(os.path.isabs(p) for p in paths)
    return {os.path.realpath(p) for p in paths}


def test_split_eval_never_calls_the_label_loaders_or_reads_a_return(cc, monkeypatch):
    root, _kinds = _se_study()
    returns = _return_paths_in(root)                  # every return the config lists, by full path
    calls = []
    for owner, name in (("cc", "load_study"), ("cc", "build_report"), ("_s2", "parse_return"), ("_s2", "_labels_of"),
                        ("_s2", "_check_structure")):
        mod = cc if owner == "cc" else cc._s2
        orig = getattr(mod, name)

        def wrapper(*a, _orig=orig, _name=name, **k):
            calls.append(_name)
            return _orig(*a, **k)
        monkeypatch.setattr(mod, name, wrapper)
    opened = _record_opened_paths(monkeypatch)
    rc, out = _split_eval(cc, root, {"sheet_one": 0.0, "sheet_two": 0.0})
    assert rc == 0
    assert calls == []
    assert any(p.endswith("key.json") for p in opened)         # the spy can see reads (positive control)
    assert not (returns & set(opened))                         # no judge's return was opened, by full path


def test_the_open_spy_sees_a_return_read_under_any_name(cc, monkeypatch):
    # positive control for the structural spy: a return read through each reader is recorded by full path,
    # whatever the file is called, so "no return opened" above cannot be an artefact of the fixture's naming
    root, _kinds = _se_study()
    returns = sorted(_return_paths_in(root))
    opened = _record_opened_paths(monkeypatch)
    Path(returns[0]).read_bytes()
    Path(returns[1]).read_text()
    with open(returns[2]) as fh:
        fh.read()
    with Path(returns[3]).open() as fh:
        fh.read()
    assert set(returns[:4]) <= set(opened)


# ---- end to end: label mapping
def _frozen_recs(cc, specs):
    """specs: [(clause, labelA, labelB)] for one item -> records with k = 1.."""
    return [cc.make_record(1, k, "a quote", c, a, b) for k, (c, a, b) in enumerate(specs, 1)]


def test_map_split_clauses_inherits_the_label_of_the_highest_jaccard_clause(cc):
    frozen = _frozen_recs(cc, [("alpha bravo delta", "CARRIED", "CARRIED"), ("gamma kilo lima", "NOT", "NOT")])
    mapped, none = cc.map_split_clauses(1, "a quote", ["alpha bravo", "gamma kilo"], frozen)
    assert none == 0
    assert [(r["clause"], r["consensus"], r["mapped_ks"]) for r in mapped] == [
        ("alpha bravo", "STATED", [1]), ("gamma kilo", "NOT", [2])]
    assert mapped[0]["a"] == "CARRIED" and mapped[0]["b"] == "CARRIED"
    # the best clause wins even when a weaker one overlaps too
    mapped2, _ = cc.map_split_clauses(1, "q", ["alpha bravo gamma"], frozen)
    assert mapped2[0]["mapped_ks"] == [1] and mapped2[0]["consensus"] == "STATED"


def test_map_split_clauses_a_tie_is_not(cc):
    # 'alpha gamma' overlaps clause 1 and clause 2 by one word each (jaccard .25 and .25)
    tie_mixed = _frozen_recs(cc, [("alpha bravo delta", "CARRIED", "CARRIED"), ("gamma kilo lima", "NOT", "NOT")])
    mapped, none = cc.map_split_clauses(1, "q", ["alpha gamma"], tie_mixed)
    assert none == 0
    assert mapped[0]["consensus"] == "NOT" and mapped[0]["mapped_ks"] == [1, 2]
    tie_stated = _frozen_recs(cc, [("alpha bravo delta", "CARRIED", "CARRIED"), ("gamma kilo lima", "CARRIED", "CARRIED")])
    mapped2, _ = cc.map_split_clauses(1, "q", ["alpha gamma"], tie_stated)
    assert mapped2[0]["consensus"] == "NOT"                # a tie is NOT even between two STATED clauses


def test_map_split_clauses_zero_jaccard_is_none_and_is_not_a_record(cc):
    frozen = _frozen_recs(cc, [("alpha bravo delta", "CARRIED", "CARRIED")])
    mapped, none = cc.map_split_clauses(1, "q", ["alpha bravo", "zebra yak", "papa quebec"], frozen)
    assert none == 2
    assert [r["clause"] for r in mapped] == ["alpha bravo"]


# ---- end to end: run --clauses split
def test_run_split_reports_mapping_rates_and_none_counts(cc):
    root = _e2e_root()
    man = _freeze_split(cc, root)
    rc, out = _run_split(cc, root, man)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    mp = rep["end_to_end"]["mapping"]
    assert mp["sheet_one"] == {"split_clauses": 5, "mapped": 5, "none": 0, "mapping_rate": 1.0, "ties_total": 0,
                               "ties_stated_stated": 0, "frozen_clauses": 6, "frozen_unmapped": 1,
                               "items_empty_split": 0, "claims_over_cap": 0}
    assert mp["sheet_two"] == {"split_clauses": 5, "mapped": 4, "none": 1, "mapping_rate": 0.8, "ties_total": 2,
                               "ties_stated_stated": 0, "frozen_clauses": 6, "frozen_unmapped": 2,
                               "items_empty_split": 0, "claims_over_cap": 0}
    assert rep["clauses_mode"] == "split" and rep["splitter_version"] == "B1"


def test_run_split_scores_on_the_split_clauses_and_leaves_none_out(cc):
    root = _e2e_root()
    man = _freeze_split(cc, root)
    rc, out = _run_split(cc, root, man)
    rep = json.loads((out / "report.json").read_text())
    e2e = rep["end_to_end"]["gated"]["folds"][0]
    assert (e2e["tune_sheet"], e2e["test_sheet"]) == ("sheet_one", "sheet_two")
    # sheet_two split clauses and the frozen clause each maps to (computed by hand in this cell):
    #   s2-a: both split clauses have the same content stems as BOTH frozen clauses (#123 / #124 are not
    #   words), so each is a tie -> NOT     s2-b: 'bravo delta were mentioned together' -> c2 STATED
    #   s2-c: 'the cache was cleared' -> c1 NOT, 'zebra yak' -> NONE (left out)
    assert e2e["test"]["consensus_counts"] == {"NOT": 3, "STATED": 1, "BAND": 0}
    frozen = rep["folds"][0]                              # the frozen-split numbers sit beside them
    assert frozen["test"]["consensus_counts"] == {"NOT": 2, "STATED": 2, "BAND": 2}
    assert set(e2e["test"]["methods"]) == {"B0", "B1", "B2", "M1"}
    md = (out / "report.md").read_text()
    assert "End-to-end" in md
    assert md.index("## Fold 1") < md.index("End-to-end")
    assert "mapping rate" in md.lower()


def test_run_split_exclusion_applies_by_mapping(cc):
    root = _e2e_root()
    # sheet_two item 4 is s2-b; its one split clause maps to frozen clause 2, which the list excludes
    excl = _write_excl(root, [{"sheet": "sheet_two", "item": 4, "clauses": [2]}])
    man = _freeze_split(cc, root, exclude=excl)
    rc, out = _run_split(cc, root, man, exclude=excl)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    e2e = rep["end_to_end"]["gated"]["folds"][0]
    assert e2e["exclusion"]["removed_total"] == 1
    assert e2e["test"]["consensus_counts"] == {"NOT": 3, "STATED": 0, "BAND": 0}
    assert rep["folds"][0]["exclusion"]["removed_total"] == 1       # the frozen clause is removed there too


def test_run_split_a_none_clause_is_never_removed_by_an_exclusion(cc):
    root = _e2e_root()
    excl = _write_excl(root, [{"sheet": "sheet_two", "item": 5, "clauses": "all"}])   # s2-c, two split clauses
    man = _freeze_split(cc, root, exclude=excl)
    rc, out = _run_split(cc, root, man, exclude=excl)
    rep = json.loads((out / "report.json").read_text())
    e2e = rep["end_to_end"]["gated"]["folds"][0]
    assert e2e["exclusion"]["removed_total"] == 1                    # only the mapped clause; NONE was never in
    assert rep["end_to_end"]["mapping"]["sheet_two"]["none"] == 1


# ---- end to end: the manifest pins the mode, the splitter and the claims
def test_split_manifest_records_mode_splitter_and_claims_sha(cc):
    root = _e2e_root()
    m = json.loads(_freeze_split(cc, root).read_text())
    assert m["clauses_mode"] == "split" and m["splitter_version"] == "B1"
    want = {n: hashlib.sha256((root / f"sheet{i}" / "claims.json").read_bytes()).hexdigest()
            for i, n in enumerate(("sheet_one", "sheet_two"))}
    assert m["claims_sha256"] == want
    frozen_root, frozen_man = _freeze_and_study(cc)
    fm = json.loads(frozen_man.read_text())
    assert (fm["clauses_mode"], fm["splitter_version"], fm["claims_sha256"]) == ("frozen", "B1", None)


def test_run_split_reports_the_claims_sha_in_inputs(cc):
    root = _e2e_root()
    rc, out = _run_split(cc, root, _freeze_split(cc, root))
    rep = json.loads((out / "report.json").read_text())
    for i, n in enumerate(("sheet_one", "sheet_two")):
        assert rep["inputs"][f"{n}/claims"] == hashlib.sha256((root / f"sheet{i}" / "claims.json").read_bytes()).hexdigest()
    assert rep["freeze"]["clauses_mode"] == "split"


@pytest.mark.parametrize("field,value", [("splitter_version", "B0"), ("clauses_mode", "frozen"),
                                         ("claims_sha256", {"sheet_one": "0" * 64, "sheet_two": "1" * 64})])
def test_run_split_refuses_when_a_pinned_field_differs_before_any_label_is_read(cc, monkeypatch, field, value):
    root = _e2e_root()
    man = _freeze_split(cc, root)
    m = json.loads(man.read_text())
    m[field] = value
    bad = root / f"BAD_{field}.json"
    bad.write_text(json.dumps(m))
    calls = _record_label_readers(cc, monkeypatch)
    rc, out = _run_split(cc, root, bad)
    assert rc == 2 and not out.exists()
    assert calls == []


def test_run_split_refuses_when_the_claims_changed_after_the_freeze(cc):
    root = _e2e_root()
    man = _freeze_split(cc, root)
    p = root / "sheet0" / "claims.json"
    rows = json.loads(p.read_text())
    rows[0]["claim"] = rows[0]["claim"] + " and more"
    p.write_text(json.dumps(rows))
    rc, out = _run_split(cc, root, man)
    assert rc == 2 and not out.exists()


def test_run_refuses_a_mode_mismatch_in_both_directions(cc):
    root = _e2e_root()
    split_man = _freeze_split(cc, root)
    rc, out = _run_split(cc, root, split_man, out_name="o1", clauses="frozen")
    assert rc == 2 and not out.exists()
    _r, frozen_man = _freeze_and_study(cc, root=root)
    rc, out = _run_split(cc, root, frozen_man, out_name="o2", clauses="split")
    assert rc == 2 and not out.exists()


def test_run_split_with_a_sheet_lacking_claims_refuses_naming_the_sheet(cc, capsys):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    _add_claims(root, [E2E_CLAIMS_ONE, E2E_CLAIMS_TWO], drop_key_for=1)
    argv = ["freeze", "--out", str(root / "F.json"), "--commit", "abc1234", "--fold", "1", "--clauses", "split",
            "--config", str(root / "config.json"), "--theta", str(THETA)]
    assert cc.main(argv) == 2
    assert "sheet_two" in capsys.readouterr().err
    assert not (root / "F.json").exists()


def test_freeze_split_without_a_config_refuses(cc):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    assert cc.main(["freeze", "--out", str(root / "F.json"), "--commit", "abc1234", "--fold", "1",
                    "--clauses", "split"]) == 2
    assert not (root / "F.json").exists()


def test_fold_2_split_freeze_compares_the_mode_and_claims(cc):
    root = _e2e_root()
    man1 = _freeze_split(cc, root)
    rc, out1 = _run_split(cc, root, man1, out_name="f1")
    assert rc == 0
    man2 = _freeze_split(cc, root, fold=2, prior=out1 / "report.json")
    m2 = json.loads(man2.read_text())
    assert m2["code_changed_since_fold1"] is False and m2["changed_fields_since_fold1"] == []
    rc, out2 = _run_split(cc, root, man2, out_name="f2", fold=2)
    assert rc == 0


# ---- round 2: the gated column is a fixed theta; the method check re-tunes beside it
def _spy_tune(cc, monkeypatch):
    """Record every call to tune() as (predict function name, number of dev records)."""
    calls = []
    orig = cc.tune

    def spy(dev_records, predict):
        calls.append((predict.__name__, len(dev_records)))
        return orig(dev_records, predict)
    monkeypatch.setattr(cc, "tune", spy)
    return calls


def _eval_sheets_of(cc, root):
    sheets = cc.load_study(root / "config.json", root / "hashes.json")
    eval_sheets, _stats = cc.split_sheets(sheets, cc.load_claims(root / "config.json", sheets))
    return sheets, eval_sheets


def test_the_gated_column_never_tunes_m1_and_the_method_check_does(cc, monkeypatch):
    root = _e2e_root()
    sheets, eval_sheets = _eval_sheets_of(cc, root)
    calls = _spy_tune(cc, monkeypatch)
    rep = cc.build_report(sheets, fold=1, eval_sheets=eval_sheets, fixed_theta=0.5)
    names = [c[0] for c in calls]
    # fold 1 tunes on sheet_one's split clauses: M1 once (the method check only), B2 once (the baseline)
    assert names.count("p_m1") == 1 and names.count("p_b2") == 1
    assert calls[names.index("p_m1")][1] == len(eval_sheets[0]["records"])
    gated = rep["folds"][0]
    assert gated["tuned"]["M1"]["value"] == 0.5 and gated["tuned"]["M1"]["fixed"] is True
    assert gated["test"]["methods"]["M1"]["parameter_value"] == 0.5
    mc = rep["method_check_folds"][0]
    assert set(mc["test"]["methods"]) == {"M1"}
    assert mc["test"]["methods"]["M1"]["parameter_value"] == mc["tuned"]["M1"]["value"]


def test_the_method_check_value_differs_from_the_fixed_theta_when_the_dev_fold_says_so(cc):
    # a control for the cell above: the two columns really are different columns on this fixture
    root = _e2e_root()
    sheets, eval_sheets = _eval_sheets_of(cc, root)
    rep = cc.build_report(sheets, fold=1, eval_sheets=eval_sheets, fixed_theta=0.99)
    assert rep["folds"][0]["tuned"]["M1"]["value"] == 0.99
    assert rep["method_check_folds"][0]["tuned"]["M1"]["value"] != 0.99


def test_run_split_reports_both_columns_and_the_retuned_vs_fixed_line(cc, monkeypatch):
    root = _e2e_root()
    man = _freeze_split(cc, root)
    calls = _spy_tune(cc, monkeypatch)
    rc, out = _run_split(cc, root, man)
    assert rc == 0
    names = [c[0] for c in calls]
    assert names.count("p_m1") == 2 and names.count("p_b2") == 2      # the frozen pass + (method check | gated B2)
    rep = json.loads((out / "report.json").read_text())
    e = rep["end_to_end"]
    assert e["gated"]["column"] == "fixed_theta" and e["gated"]["theta"] == THETA
    assert e["method_check"]["gated"] is False
    g, m = e["gated"]["folds"][0], e["method_check"]["folds"][0]
    assert g["tuned"]["M1"]["value"] == THETA and g["test"]["methods"]["M1"]["parameter_value"] == THETA
    assert set(g["test"]["methods"]) == {"B0", "B1", "B2", "M1"} and set(m["test"]["methods"]) == {"M1"}
    md = (out / "report.md").read_text()
    gi, mi = md.index("## End-to-end, GATED column"), md.index("## End-to-end, method check (UNGATED)")
    assert md.index("## Fold 1") < gi < mi
    assert f"re-tuned theta per fold vs fixed: {m['tuned']['M1']['value']} vs {THETA}" in md


def test_split_report_labels_the_frozen_section_and_the_gated_heading(cc):
    root = _e2e_root()
    man = _freeze_split(cc, root)
    rc, out = _run_split(cc, root, man)
    assert rc == 0
    md = (out / "report.md").read_text()
    label = md.index("## Frozen human-split clauses (reference only, NOT the gate")
    assert label < md.index("## Fold 1")
    gated = [ln for ln in md.splitlines() if ln.startswith("## End-to-end, GATED column")]
    assert len(gated) == 1 and "GATED" in gated[0] and f"theta fixed at {THETA}" in gated[0]
    # frozen mode: no such label
    froot, fman = _freeze_and_study(cc)
    rc, fout = _run(cc, froot, fman)
    assert rc == 0
    assert "reference only" not in (fout / "report.md").read_text()


def test_run_split_theta_is_required_and_refused_in_frozen_mode(cc):
    root = _e2e_root()
    man = _freeze_split(cc, root)
    rc, out = _run_split(cc, root, man, theta=None)
    assert rc == 2 and not out.exists()
    assert cc.main(["freeze", "--out", str(root / "F_NT.json"), "--commit", "abc1234", "--fold", "1",
                    "--clauses", "split", "--config", str(root / "config.json")]) == 2
    assert not (root / "F_NT.json").exists()
    # frozen mode has no theta to fix: giving one is refused rather than ignored
    _r, fman = _freeze_and_study(cc, root=root)
    assert cc.main(["run", "--config", str(root / "config.json"), "--hashes", str(root / "hashes.json"),
                    "--freeze", str(fman), "--out-dir", str(root / "o_ft"), "--commit", "abc1234", "--fold", "1",
                    "--theta", "0.46"]) == 2
    assert not (root / "o_ft").exists()
    assert cc.main(["freeze", "--out", str(root / "F_FT.json"), "--commit", "abc1234", "--fold", "1",
                    "--theta", "0.46"]) == 2
    assert not (root / "F_FT.json").exists()


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "nan", "inf"])
def test_theta_outside_zero_one_is_refused(cc, bad):
    root = _e2e_root()
    assert cc.main(["freeze", "--out", str(root / "F_BAD.json"), "--commit", "abc1234", "--fold", "1",
                    "--clauses", "split", "--config", str(root / "config.json"), "--theta", bad]) == 2
    assert not (root / "F_BAD.json").exists()


def test_split_manifest_records_fixed_theta_and_the_gate_column(cc):
    root = _e2e_root()
    m = json.loads(_freeze_split(cc, root).read_text())
    assert m["fixed_theta"] == THETA and m["gate_column"] == "fixed_theta"
    _r, frozen_man = _freeze_and_study(cc)
    fm = json.loads(frozen_man.read_text())
    assert (fm["fixed_theta"], fm["gate_column"]) == (None, None)
    rc, out = _run_split(cc, root, _freeze_split(cc, root))
    rep = json.loads((out / "report.json").read_text())
    assert rep["freeze"]["fixed_theta"] == THETA and rep["freeze"]["gate_column"] == "fixed_theta"


def test_run_split_refuses_a_theta_that_differs_from_the_manifest_before_any_label_is_read(cc, monkeypatch):
    root = _e2e_root()
    man = _freeze_split(cc, root)                                  # frozen at 0.46
    calls = _record_label_readers(cc, monkeypatch)
    rc, out = _run_split(cc, root, man, theta=0.5)
    assert rc == 2 and not out.exists()
    assert calls == []
    # positive control: the same freeze with the matching theta does read the labels
    rc, out = _run_split(cc, root, man, theta=THETA, out_name="ok_theta")
    assert rc == 0 and calls.count("load_study") == 1


def test_run_split_refuses_a_manifest_whose_fixed_theta_was_edited(cc, monkeypatch):
    root = _e2e_root()
    man = _freeze_split(cc, root)
    m = json.loads(man.read_text())
    m["fixed_theta"] = 0.5                                          # the manifest was edited, not the command
    bad = root / "BAD_THETA.json"
    bad.write_text(json.dumps(m))
    calls = _record_label_readers(cc, monkeypatch)
    rc, out = _run_split(cc, root, bad)
    assert rc == 2 and not out.exists() and calls == []


def test_fold_2_split_freeze_flags_a_changed_fixed_theta(cc):
    root = _e2e_root()
    man1 = _freeze_split(cc, root)
    rc, out1 = _run_split(cc, root, man1, out_name="f1t")
    assert rc == 0
    man2 = _freeze_split(cc, root, fold=2, prior=out1 / "report.json", theta=0.5)
    m2 = json.loads(man2.read_text())
    assert m2["code_changed_since_fold1"] is True and m2["changed_fields_since_fold1"] == ["fixed_theta"]
    rc, out2 = _run_split(cc, root, man2, out_name="f2t", fold=2, theta=0.5)
    assert rc == 0
    assert json.loads((out2 / "report.json").read_text())["changed_fields_since_fold1"] == ["fixed_theta"]


# ---- round 2: tie, coverage and exclusion-through-a-tie reporting
def _hand_sheet(cc, name, items):
    """items: {item number: (node id, [(clause, labelA, labelB)])} -> a sheet shaped like load_study's."""
    recs = []
    for n, (_iid, cls) in items.items():
        recs += [cc.make_record(n, k, "a quote", c, a, b) for k, (c, a, b) in enumerate(cls, 1)]
    return {"name": name, "records": recs, "ids": {n: iid for n, (iid, _c) in items.items()}}


def test_a_stated_stated_tie_counts_in_both_tie_numbers_and_a_not_stated_tie_only_in_total(cc):
    both = _hand_sheet(cc, "sx", {
        1: ("n1", [("alpha bravo delta", "CARRIED", "CARRIED"), ("gamma kilo lima", "CARRIED", "CARRIED")]),
        2: ("n2", [("mike november oscar", "CARRIED", "CARRIED"), ("papa quebec romeo", "NOT", "NOT")])})
    _evs, stats = cc.split_sheets([both], {"sx": {"n1": "alpha gamma", "n2": "mike papa"}})
    assert stats["sx"]["ties_total"] == 2 and stats["sx"]["ties_stated_stated"] == 1
    only_mixed = _hand_sheet(cc, "sy", {2: ("n2", [("mike november oscar", "CARRIED", "CARRIED"),
                                                   ("papa quebec romeo", "NOT", "NOT")])})
    _evs, st2 = cc.split_sheets([only_mixed], {"sy": {"n2": "mike papa"}})
    assert st2["sy"]["ties_total"] == 1 and st2["sy"]["ties_stated_stated"] == 0
    only_ss = _hand_sheet(cc, "sz", {1: ("n1", [("alpha bravo delta", "CARRIED", "CARRIED"),
                                                ("gamma kilo lima", "CARRIED", "CARRIED")])})
    _evs, st3 = cc.split_sheets([only_ss], {"sz": {"n1": "alpha gamma"}})
    assert st3["sz"]["ties_total"] == 1 and st3["sz"]["ties_stated_stated"] == 1


def test_a_tie_where_the_judges_disagree_is_not_a_stated_stated_tie(cc):
    # consensus BAND (one judge NOT) is not STATED: the rule did not move a STATED consensus label away
    sheet = _hand_sheet(cc, "sx", {1: ("n1", [("alpha bravo delta", "CARRIED", "CARRIED"),
                                              ("gamma kilo lima", "CARRIED", "NOT")])})
    _evs, stats = cc.split_sheets([sheet], {"sx": {"n1": "alpha gamma"}})
    assert stats["sx"]["ties_total"] == 1 and stats["sx"]["ties_stated_stated"] == 0


def test_coverage_counts_frozen_clauses_no_split_clause_maps_to_and_empty_splits(cc):
    sheet = _hand_sheet(cc, "sx", {
        1: ("n1", [("alpha bravo delta", "CARRIED", "CARRIED"), ("gamma kilo lima", "NOT", "NOT")]),
        2: ("n2", [("mike november oscar", "CARRIED", "CARRIED")]),
        3: ("n3", [("papa quebec romeo", "NOT", "NOT"), ("sierra tango uniform", "NOT", "NOT")])})
    # item 1: one split clause covers clause 1 only (clause 2 is never chosen); item 2 maps; item 3 splits to nothing
    claims = {"sx": {"n1": "alpha bravo", "n2": "mike november", "n3": "."}}
    assert cc.split_claim(".") == []
    _evs, stats = cc.split_sheets([sheet], claims)
    st = stats["sx"]
    assert st["frozen_clauses"] == 5
    assert st["frozen_unmapped"] == 3          # item 1 clause 2, item 3 clauses 1 and 2
    assert st["items_empty_split"] == 1
    assert st["split_clauses"] == 2 and st["mapped"] == 2


def test_a_frozen_clause_chosen_only_through_a_tie_is_not_unmapped(cc):
    sheet = _hand_sheet(cc, "sx", {1: ("n1", [("alpha bravo delta", "CARRIED", "CARRIED"),
                                              ("gamma kilo lima", "NOT", "NOT")])})
    _evs, stats = cc.split_sheets([sheet], {"sx": {"n1": "alpha gamma"}})
    assert stats["sx"]["frozen_unmapped"] == 0


def test_run_split_report_md_shows_ties_and_coverage_beside_the_mapping_rate(cc):
    root = _e2e_root()
    rc, out = _run_split(cc, root, _freeze_split(cc, root))
    md = (out / "report.md").read_text()
    line = next(ln for ln in md.splitlines() if ln.startswith("- sheet_two: split clauses"))
    for part in ("ties total 2", "STATED-STATED ties 0", "frozen clauses never mapped to 2 of 6",
                 "items with an empty split 0"):
        assert part in line


def test_exclusion_removes_a_split_clause_tied_to_an_excluded_clause_that_is_not_the_lowest(cc):
    frozen = _frozen_recs(cc, [("alpha bravo delta", "CARRIED", "CARRIED"), ("gamma kilo lima", "CARRIED", "CARRIED")])
    mapped, _none = cc.map_split_clauses(1, "q", ["alpha gamma"], frozen)
    assert mapped[0]["mapped_ks"] == [1, 2] and mapped[0]["k"] == 1       # tied; the lowest is clause 1
    sheet = {"name": "sx", "records": mapped}
    kept, rows = cc.apply_exclusions_mapped(sheet, [{"sheet": "sx", "item": 1, "clauses": [2]}])
    assert kept == [] and rows[0]["removed"] == 1                         # clause 2 is excluded, not the lowest
    kept1, _ = cc.apply_exclusions_mapped(sheet, [{"sheet": "sx", "item": 1, "clauses": [1]}])
    assert kept1 == []                                                     # and the lowest too
    kept3, _ = cc.apply_exclusions_mapped(sheet, [{"sheet": "sx", "item": 1, "clauses": [3]}])
    assert len(kept3) == 1                                                 # an unrelated clause removes nothing


# ---- frozen mode is unchanged
# Computed from the tool as merged in #4204 (2e5ff406) on the same synthetic fixture, before this change:
# sha256 of json.dumps(report["folds"], sort_keys=True), and of the report.md text before "## Inputs"
# without its "engram-alpha commit:" line (the commit, freeze hash and seat model are not fold content).
GOLDEN_FROZEN_FOLDS_SHA = "ce0c1e6c7d4971719da183ba32aedcbddf8b45c0c18ed31966ec1d21f88e94f7"
GOLDEN_FROZEN_MD_SHA = "2209a9eb47ddea7ae7fd4e5310053b5b0ad61a4ebea40c52e4c41636701bba78"
GOLDEN_FROZEN_REPORT_KEYS = ['ci_method', 'columns', 'embedding_model', 'engram_alpha_commit', 'fold', 'folds', 'freeze',
                             'freeze_sha256', 'inputs', 'mask_counts', 'note', 'report_version', 'resamples', 'seed',
                             'sheet_order', 'tool_version']


@pytest.mark.parametrize("explicit", [False, True])
def test_frozen_mode_output_is_byte_identical_to_dev(cc, explicit):
    root = _write_study([SHEET_ONE, SHEET_TWO])
    _r, man = _freeze_and_study(cc, root=root)
    if explicit:
        rc, out = _run_split(cc, root, man, clauses="frozen")
    else:
        rc, out = _run(cc, root, man)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    assert sorted(rep) == GOLDEN_FROZEN_REPORT_KEYS
    assert hashlib.sha256(json.dumps(rep["folds"], sort_keys=True).encode()).hexdigest() == GOLDEN_FROZEN_FOLDS_SHA
    md = (out / "report.md").read_text()
    lines = [ln for ln in md.split("## Inputs")[0].splitlines() if not ln.startswith("engram-alpha commit:")]
    assert hashlib.sha256("\n".join(lines).encode()).hexdigest() == GOLDEN_FROZEN_MD_SHA


# ---- advise
def test_advise_splits_the_claim_when_no_clauses_are_given(cc):
    quote = "Quillon merged the branch after review"
    claim = "Quillon merged the branch. Marrowby reviewed it"
    out = cc.advise(quote, theta=1.0, claim=claim)
    assert out["of"] == 2
    assert [c["text"] for c in out["clauses"]] == ["Marrowby reviewed it"]
    assert out["clauses"][0]["n"] == 2
    assert cc.advise(quote, None, 1.0, claim) == out
    given = cc.advise(quote, ["Quillon merged the branch"], 1.0, claim)       # explicit clauses win
    assert given["of"] == 1 and given["not_stated"] == 0


def test_advise_needs_clauses_or_a_claim(cc):
    with pytest.raises(ValueError):
        cc.advise("a quote", theta=1.0)
    with pytest.raises(ValueError):
        cc.advise("a quote", ["a clause"])        # theta has no silent default


# =========================================================================== round 4 (#4216 colleague findings)
# Finding 1: jaccard of two EMPTY stem sets is 0.0, so a stopword-only split clause maps to NONE.
# Finding 2: split_claim is bounded (SPLIT_MAX_CHARS, reported as claims_over_cap), rule (c) is iterative,
# rule (b) is linear, and below the cap the output is exactly what it was.

def test_jaccard_of_two_empty_stem_sets_is_zero_not_one(cc):
    assert cc.jaccard(set(), set()) == 0.0
    assert cc.jaccard({"alpha"}, set()) == 0.0 and cc.jaccard(set(), {"alpha"}) == 0.0
    assert cc.jaccard({"alpha", "bravo"}, {"alpha", "delta"}) == pytest.approx(1 / 3)
    assert cc.jaccard({"alpha"}, {"alpha"}) == 1.0


def test_a_stopword_only_split_clause_maps_to_none_even_beside_a_stopword_only_frozen_clause(cc):
    # both clause texts are all stopwords, so both stem sets are empty: no evidence of overlap, so NONE,
    # never a label inherited from the frozen clause that happens to be empty too
    assert cc._stem_set("that were this") == set() == cc._stem_set("the was and")
    frozen = _frozen_recs(cc, [("the was and", "CARRIED", "CARRIED"), ("alpha bravo delta", "NOT", "NOT")])
    mapped, none = cc.map_split_clauses(1, "q", ["that were this"], frozen)
    assert mapped == [] and none == 1


def test_a_stopword_only_claim_is_counted_as_none_in_the_mapping_stats_and_leaves_no_record(cc):
    sheet = _hand_sheet(cc, "sx", {1: ("n1", [("the was and", "CARRIED", "CARRIED"),
                                              ("alpha bravo delta", "NOT", "NOT")])})
    assert cc.split_claim("that were this") == ["that were this"]
    evs, stats = cc.split_sheets([sheet], {"sx": {"n1": "that were this"}})
    st = stats["sx"]
    assert (st["split_clauses"], st["mapped"], st["none"], st["mapping_rate"]) == (1, 0, 1, 0.0)
    assert evs[0]["records"] == []
    assert st["frozen_unmapped"] == 2


# ---- the input cap
def test_split_max_chars_is_a_documented_constant(cc):
    assert cc.SPLIT_MAX_CHARS == 20000
    assert "SPLIT_MAX_CHARS" in cc.split_claim.__doc__


def test_a_claim_over_the_cap_is_not_split_and_one_at_the_cap_is(cc):
    unit = "The suite ran. "
    at_cap = (unit * (cc.SPLIT_MAX_CHARS // len(unit) + 1))[:cc.SPLIT_MAX_CHARS]
    assert len(at_cap) == cc.SPLIT_MAX_CHARS
    assert len(cc.split_claim(at_cap)) > 1                                  # at the cap: split as ever
    over = at_cap + "x"
    assert len(over) == cc.SPLIT_MAX_CHARS + 1
    assert cc.split_claim(over) == [over.strip(cc.SPLIT_STRIP)]              # over it: one piece, rule (d) only


def test_an_over_cap_claim_is_still_stripped_and_a_blank_one_is_empty(cc, monkeypatch):
    monkeypatch.setattr(cc, "SPLIT_MAX_CHARS", 20)
    assert cc.split_claim("  The suite ran. The build passed. ") == ["The suite ran. The build passed"]
    assert cc.split_claim(" " * 30) == []


def test_over_the_cap_is_counted_in_the_mapping_stats(cc, monkeypatch):
    monkeypatch.setattr(cc, "SPLIT_MAX_CHARS", 30)
    root = _e2e_root()
    want = {"sheet_one": sum(len(c) > 30 for c in E2E_CLAIMS_ONE.values()),
            "sheet_two": sum(len(c) > 30 for c in E2E_CLAIMS_TWO.values())}
    assert want["sheet_one"] > 0 and want["sheet_two"] > 0
    man = _freeze_split(cc, root)
    rc, out = _run_split(cc, root, man)
    assert rc == 0
    rep = json.loads((out / "report.json").read_text())
    got = {n: st["claims_over_cap"] for n, st in rep["end_to_end"]["mapping"].items()}
    assert got == want
    md = (out / "report.md").read_text()
    line = next(ln for ln in md.splitlines() if ln.startswith("- sheet_two: split clauses"))
    assert f"claims over the split cap {want['sheet_two']}" in line


def test_the_study_shaped_fixture_has_no_claim_over_the_cap(cc):
    root = _e2e_root()
    rc, out = _run_split(cc, root, _freeze_split(cc, root))
    rep = json.loads((out / "report.json").read_text())
    assert [st["claims_over_cap"] for st in rep["end_to_end"]["mapping"].values()] == [0, 0]


def test_split_eval_reports_claims_over_cap_per_sheet_and_pooled(cc, monkeypatch):
    monkeypatch.setattr(cc, "SPLIT_MAX_CHARS", 15)
    root, kinds = _se_study()
    rc, out = _split_eval(cc, root, {"sheet_one": 0.0, "sheet_two": 0.0})
    assert rc == 0
    rep = json.loads((out / "report-split.json").read_text())
    want = [sum(len(_SE_KINDS[k][0]) > 15 for k in ks) for ks in kinds]
    assert min(want) > 0
    assert [rep["sheets"]["sheet_one"]["claims_over_cap"], rep["sheets"]["sheet_two"]["claims_over_cap"]] == want
    assert rep["pooled"]["claims_over_cap"] == sum(want)
    assert "claims over the split cap" in (out / "report-split.md").read_text()


def test_split_eval_reports_zero_over_cap_on_the_ordinary_fixture(cc):
    root, _kinds = _se_study()
    rc, out = _split_eval(cc, root, {"sheet_one": 0.0, "sheet_two": 0.0})
    rep = json.loads((out / "report-split.json").read_text())
    assert rep["sheets"]["sheet_one"]["claims_over_cap"] == 0 == rep["pooled"]["claims_over_cap"]


# ---- robustness: no recursion, no quadratic blow-up (the cap is lifted so the loops themselves are tested)
def test_a_chain_of_a_thousand_qualifiers_returns_without_recursion_error(cc, monkeypatch):
    monkeypatch.setattr(cc, "SPLIT_MAX_CHARS", 10 ** 7, raising=False)    # raising=False: red for the right reason
    claim = "Quillon merged the hotfix branch" + " after alpha bravo delta" * 1000
    pieces = cc.split_claim(claim)                       # RecursionError before the rule became iterative
    assert len(pieces) == 1001
    assert pieces[0] == "Quillon merged the hotfix branch"
    assert set(pieces[1:]) == {"after alpha bravo delta"}


def test_thirty_thousand_verbless_and_characters_split_in_well_under_a_second(cc, monkeypatch):
    import time
    monkeypatch.setattr(cc, "SPLIT_MAX_CHARS", 10 ** 7, raising=False)
    claim = "cache and " * 3000                          # 30,000 chars, no verb-like word and no hard token
    t0 = time.perf_counter()
    pieces = cc.split_claim(claim)
    took = time.perf_counter() - t0
    assert len(claim) == 30000 and len(pieces) == 1      # nothing can stand as a clause, so no cut
    assert took < 1.0, f"took {took:.2f}s"               # ~5 s before; a generous bound (it runs in ~0.1 s)


# ---- the output under the cap is exactly what it was: an independent copy of the round-3 splitter (B1)
def _ref_in_spans(pos, spans):
    return any(a <= pos < b for a, b in spans)


def _ref_split_claim(cc, text):
    """The splitter as it stood at 40ecb3fb (recursive rule (c), left side rescanned in rule (b)), copied
    here so the cell compares today's code with it. Uses only unchanged helpers from the tool."""
    spans_of = lambda t: [(m.start(), m.end()) for m in re.finditer(r"`[^`]*`", t)]

    def ends_abbr(t, dot):
        head = t[: dot + 1].lower()
        for ab in cc.SPLIT_ABBREVIATIONS:
            if head.endswith(ab):
                before = head[: len(head) - len(ab)]
                if not before or not before[-1].isalnum():
                    return True
        return False

    def sentences(t):
        spans, pieces, start = spans_of(t), [], 0
        for m in cc.SPLIT_BREAK_RE.finditer(t):
            i = m.start()
            if _ref_in_spans(i, spans) or (t[i] == "." and ends_abbr(t, i)):
                continue
            pieces.append(t[start:i])
            start = m.end()
        pieces.append(t[start:])
        return pieces

    def like(side):
        for w in cc._WORD_RE.findall(cc.normalize(side)):
            if w in cc.SPLIT_VERB_FORMS or cc.stem(w) in cc._SPLIT_VERB_STEMS:
                return True
        return bool(cc._hard_tokens_with_class(side))

    def coordination(sentence):
        spans = spans_of(sentence)
        cuts = [(m.start(), m.end()) for m in cc.SPLIT_COORD_RE.finditer(sentence)
                if not _ref_in_spans(m.start(), spans)]
        pieces, piece_start = [], 0
        for idx, (a, b) in enumerate(cuts):
            right_end = cuts[idx + 1][0] if idx + 1 < len(cuts) else len(sentence)
            if like(sentence[piece_start:a]) and like(sentence[b:right_end]):
                pieces.append(sentence[piece_start:a])
                piece_start = b
        pieces.append(sentence[piece_start:])
        return pieces

    def n_stems(t):
        return len(cc._content_stems(cc.normalize(t)))

    def qualifiers(piece):
        spans = spans_of(piece)
        at = sorted(m.start() for intro in cc.SPLIT_QUALIFIERS
                    for m in re.finditer(re.escape(intro), piece) if not _ref_in_spans(m.start(), spans))
        for pos in at:
            left, right = piece[:pos], piece[pos + 1:]
            if n_stems(left) >= cc.SPLIT_MIN_STEMS and n_stems(right) >= cc.SPLIT_MIN_STEMS:
                return [left] + qualifiers(right)
        return [piece]

    out = []
    for sentence in sentences(text or ""):
        for piece in coordination(sentence):
            out.extend(qualifiers(piece))
    return [p for p in (x.strip(cc.SPLIT_STRIP) for x in out) if p]


# every input the splitter cells above feed it, plus the claims of the e2e and split-eval fixtures
_PIN_FIXED = [
    "The suite ran. The build passed.", "Did the suite run? The build passed!",
    "Quillon merged the branch; Marrowby approved it", "the rate was 3.5 percent in the run",
    "The run took 3.5 hours. It passed.", "reviewers (e.g. Quillon) approved it",
    "it holds, i.e. the cache is warm", "the cache vs. the queue was slower",
    "see cf. the earlier note for details", "`a. b; c` was logged", "the line `x. y` ran. Then it stopped.",
    "Quillon merged the branch as hotfix and Marrowby approved it", "the suite passed, and the build failed",
    "the suite passed but the build failed", "build 4812 and ticket #123",
    "the queue and the cache were drained", "tests ran against build 4812 and docs", "bread and butter",
    "the line `x was set and y was read` ran", "Quillon ran the suite and the cache and Marrowby approved it",
    "Quillon merged the hotfix branch after reviewers finished careful inspection",
    "Quillon merged the hotfix branch because reviewers finished careful inspection",
    "Quillon merged the hotfix branch so that reviewers finished careful inspection",
    "Quillon merged the hotfix branch which reviewers finished careful inspection",
    "Quillon merged the hotfix branch after review", "merged after reviewers finished careful inspection",
    "  ...The suite ran,  ", "The suite ran.; ; The build passed.", "", "...", ".", "   ",
    "Quillon merged the hotfix branch after reviewers finished careful inspection. Marrowby approved it",
    "approx. three hosts ran it, and e.g. two of them passed", "x and y", "a, and b, but c",
    *E2E_CLAIMS_ONE.values(), *E2E_CLAIMS_TWO.values(), *(c for c, _f in _SE_KINDS.values()),
    "`unclosed code span and the suite ran",
    "the suite ran `a and b` and the build passed after Marrowby reviewed the change",
    "Alpha ran and Bravo ran and Charlie ran after Delta finished the careful audit because Echo asked",
]

_PIN_WORDS = ("Quillon Marrowby the hotfix branch suite build cache queue reviewers inspection careful change "
              "merged approved ran passed failed drained cleared landed reported was were is has bread butter "
              "alpha bravo delta zebra yak 4812 #123 2026-10-02 " + NODE_SHAPED + " `code and span` `x. y` e.g. i.e. vs. 3.5 "
              "and and but after because which").split()
_PIN_MARKS = (" ", " ", " ", " ", ", ", ", and ", " and ", " but ", ". ", "; ", "! ", "? ", " after ",
              " because ", " so that ", " which ", ",", "\n")


def _generated_claims(n=240, seed=4216):
    import random
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        parts = []
        for _w in range(rng.randint(1, 60)):
            parts.append(rng.choice(_PIN_WORDS))
            parts.append(rng.choice(_PIN_MARKS))
        out.append("".join(parts))
    return out


def test_split_claim_output_under_the_cap_is_identical_to_the_round_3_splitter(cc):
    corpus = _PIN_FIXED + _generated_claims()
    assert len(corpus) >= 260 and all(len(t) <= cc.SPLIT_MAX_CHARS for t in corpus)
    assert any(len(cc.split_claim(t)) > 2 for t in corpus)      # the corpus does exercise the rules
    for text in corpus:
        assert cc.split_claim(text) == _ref_split_claim(cc, text), text


def test_the_pin_catches_a_wrong_rule_b(cc, monkeypatch):
    # control: a splitter that kept only the sentence-level cut would differ from the reference on this corpus
    monkeypatch.setattr(cc, "_split_coordination", lambda sentence: [sentence])
    assert any(cc.split_claim(t) != _ref_split_claim(cc, t) for t in _PIN_FIXED)
