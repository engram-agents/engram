"""Contract tests for tools/probes/mods-recall/probe_fleet_report.py (engram-alpha#3549).

Contract-first (CLAUDE.md "Contract-first tests"): these assertions were written
BEFORE probe_fleet_report.py existed. Every expected number is hand-computed in
the comment beside it from the fixture literals below, never read off the tool.

Fixtures are two synthetic converter output dirs (golden/, test/, pairs.jsonl,
summary.json), built from literals. NO real probe data was used (it lives on
another host).

Fixture A  (seat kepler, 2026-10-06), 10 steps a-s01..a-s10
  golden every step: ob_0001=2, ob_0002=2, ob_0005=2, ob_0003=0
      ideal DCG (grades 2,2,2,0) = 2/log2(2) + 2/log2(3) + 2/log2(4) = 2 + 1.2618595 + 1 = 4.2618595
  querySource: steps 1-6 "thinking", steps 7-10 "visible" (same for every arm)
  arm_x : [ob_0001, ob_0002, ob_0005]   ms = 10*i (10..100)
          p@5 = 3/5 = .600 ; nDCG = 4.2618595/4.2618595 = 1.000 ; median ms = (50+60)/2 = 55
  arm_y : [ob_0003, ob_0001]            ms = 200
          p@5 = 1/5 = .200 ; DCG = 0 + 2/log2(3) = 1.2618595 ; nDCG = 1.2618595/4.2618595 = .296 ; median ms 200
  route:prompt_surface : [ob_0001 sim .9, ob_0003 sim .6, ob_0002 sim .4]   (querySource null, ms null)
          p@5 = 2/5 = .400 ; DCG = 2 + 0 + 2/log2(4) = 3 ; nDCG = 3/4.2618595 = .704
          sweep p@5: recorded .400 ; floors .30 .35 .40 keep all 3 -> .400 ;
                     floors .45 .. .70 keep ob_0001 (and ob_0003 up to .60, rel 0, no hit) -> 1/5 = .200
          null similarities: 0
  pairs: arm_x/arm_y better 3, same 1, worse 1 ; arm_x/route:prompt_surface worse 2
  summary.json: steps_rated 10, no rater key

Fixture B  (seat borges, 2026-10-07), 4 steps b-s1..b-s4
  golden every step: ob_0001=2, ob_0002=0     ideal DCG = 2
  querySource "visible" every step
  arm_x : [ob_0001, ob_0002]  ms 40       p@5 = 1/5 = .200 ; nDCG = 2/2 = 1.000 ; median ms 40
  arm_z : status "error" on step b-s1 only -> n 0, 1 arm run excluded by status
  route:prompt_surface : [ob_0001 sim .8, ob_0002 sim null]
          p@5 = 1/5 = .200 ; nDCG = 1.000 ; one null similarity per step -> 4 null rows in every floor setting
  pairs: arm_x/arm_y better 1
  summary.json: steps_rated 4, no rater key

Pooled (hand):
  pairs  arm_x/arm_y        better 3+1 = 4, same 1, worse 1, n 6
         arm_x/route:...    worse 2, n 2
  sweep  as recorded: (10*.400 + 4*.200)/14 = 4.8/14 = .343, n 14, inputs pooled 2, excluded 0, null rows 0
         floors .30 .35 .40: only A pooled -> .400, n 10, pooled 1, excluded inputs 1, null rows excluded 4
         floors .45 .. .70 : only A pooled -> .200, n 10, pooled 1, excluded inputs 1, null rows excluded 4
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO_ROOT / "tools" / "probes" / "mods-recall"


@pytest.fixture()
def fleet():
    spec = importlib.util.spec_from_file_location("probe_fleet_report_t3549", PROBE_DIR / "probe_fleet_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _w(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _rru(qid, arm, cands, ms=None, status="ok", source="thinking", route=False):
    nodes = []
    for r, c in enumerate(cands):
        node_id, sim = c if isinstance(c, tuple) else (c, None)
        signals = {"similarity": sim} if route else {}
        nodes.append({"id": node_id, "rank": r + 1, "grading": None, "signals": signals, "notes": ""})
    return {"query_id": qid, "query_text": "q", "ranked_nodes": nodes,
            "metadata": {"arm": arm, "status": status, "ms": ms,
                         "msOverStep": None, "querySource": source, "shownIds": None}}


def _golden(root: Path, qid: str, rows: dict) -> None:
    _w(root / "golden" / f"q_{qid}.json", {
        "query_id": qid, "query_text": "g",
        "ranked_candidates": [{"id": i, "grading": {"relevance": rel}} for i, rel in rows.items()]})


def _pairs(root: Path, rows) -> None:
    (root / "pairs.jsonl").write_text("".join(
        json.dumps({"armA": a, "armB": b, "verdict": v}) + "\n" for a, b, v in rows))


def build_a(root: Path) -> Path:
    for i in range(1, 11):
        qid, src = f"a-s{i:02d}", ("thinking" if i <= 6 else "visible")
        _golden(root, qid, {"ob_0001": 2, "ob_0002": 2, "ob_0005": 2, "ob_0003": 0})
        t = root / "test"
        _w(t / f"{qid}-x.json", _rru(qid, "arm_x", ["ob_0001", "ob_0002", "ob_0005"], ms=10.0 * i, source=src))
        _w(t / f"{qid}-y.json", _rru(qid, "arm_y", ["ob_0003", "ob_0001"], ms=200.0, source=src))
        _w(t / f"{qid}-r.json", _rru(qid, "route:prompt_surface",
                                     [("ob_0001", 0.9), ("ob_0003", 0.6), ("ob_0002", 0.4)],
                                     source=None, route=True))
    _pairs(root, [("arm_x", "arm_y", "better")] * 3 + [("arm_x", "arm_y", "same"), ("arm_x", "arm_y", "worse")]
           + [("arm_x", "route:prompt_surface", "worse")] * 2)
    _w(root / "summary.json", {"steps_rated": 10, "steps_skipped_by_reason": {}})
    return root


def build_b(root: Path) -> Path:
    for i in range(1, 5):
        qid = f"b-s{i}"
        _golden(root, qid, {"ob_0001": 2, "ob_0002": 0})
        t = root / "test"
        _w(t / f"{qid}-x.json", _rru(qid, "arm_x", ["ob_0001", "ob_0002"], ms=40.0, source="visible"))
        _w(t / f"{qid}-r.json", _rru(qid, "route:prompt_surface", [("ob_0001", 0.8), ("ob_0002", None)],
                                     source=None, route=True))
    _w(root / "test" / "b-s1-z.json", _rru("b-s1", "arm_z", ["ob_0001"], ms=5.0, status="error", source="visible"))
    _pairs(root, [("arm_x", "arm_y", "better")])
    _w(root / "summary.json", {"steps_rated": 4, "steps_skipped_by_reason": {}})
    return root


@pytest.fixture()
def dirs(tmp_path):
    return build_a(tmp_path / "A"), build_b(tmp_path / "B")


def run(fleet, tmp_path, dir_label_pairs, extra=()):
    out = tmp_path / "report.md"
    argv = [str(d) for d, _ in dir_label_pairs]
    for _, label in dir_label_pairs:
        if label is not None:
            argv += ["--label", label]
    assert fleet.main(argv + ["--out", str(out), *extra]) == 0
    return out.read_text()


KEPLER, BORGES = "kepler:2026-10-06", "borges:2026-10-07"


@pytest.fixture()
def report(fleet, dirs, tmp_path):
    a, b = dirs
    return run(fleet, tmp_path, [(a, KEPLER), (b, BORGES)])


# ----------------------------------------------------------------- section order
def test_sections_appear_in_the_issues_order(report):
    heads = ["## 1.", "## 2.", "## 3.", "## 4.", "## 5."]
    pos = [report.index(h) for h in heads]
    assert pos == sorted(pos)
    assert report.index("### p@5") < report.index("### nDCG@10") < report.index("### median ms") < pos[1]


# ----------------------------------------------------------------- 1. tables
def test_p_at_5_table_exact_cells(report):
    assert "| arm | kepler:2026-10-06 | borges:2026-10-07 |" in report
    assert "| arm_x | .600 (10) | (.200) (4) |" in report          # 3/5 over 10 steps; 1/5 over 4 steps (n<10)
    assert "| arm_y | .200 (10) | - |" in report                    # 1/5 over 10; absent in B
    assert "| arm_z | - | - |" in report                           # B: its only run errored; value absent
    assert "| route:prompt_surface | .400 (10) | (.200) (4) |" in report  # 2/5 ; 1/5


def test_ndcg_table_exact_cells(report):
    nd = report[report.index("### nDCG@10"):report.index("### median ms")]
    assert "| arm_x | 1.000 (10) | (1.000) (4) |" in nd           # ideal ranking ; 2/2
    assert "| arm_y | .296 (10) | - |" in nd                      # 1.2618595/4.2618595
    assert "| route:prompt_surface | .704 (10) | (1.000) (4) |" in nd  # 3/4.2618595 ; 2/2


def test_median_ms_table_exact_cells(report):
    ms = report[report.index("### median ms"):report.index("## 2.")]
    assert "| arm_x | 55 (10) | (40) (4) |" in ms                 # median of 10..100 = 55 ; constant 40
    assert "| arm_y | 200 (10) | - |" in ms
    assert "| route:prompt_surface | - | - |" in ms               # routes carry no ms


def test_small_n_cell_is_parenthesised_and_excluded_from_best_arm_line(report):
    # B has only n<10 cells, so no arm may be named best there (mutation: drop the exclusion -> arm_x named)
    assert "best p@5 per column: kepler:2026-10-06 arm_x .600 (10) · borges:2026-10-07 none (no arm with n >= 10)" in report
    assert "best nDCG@10 per column: kepler:2026-10-06 arm_x 1.000 (10) · borges:2026-10-07 none (no arm with n >= 10)" in report


def test_columns_are_ordered_by_date_then_seat_not_input_order(fleet, dirs, tmp_path):
    a, b = dirs
    # input order deliberately reversed; same date, seats kepler/zed, plus a later date
    text = run(fleet, tmp_path, [(b, "borges:2026-10-07"), (a, "zed:2026-10-06"), (a, "kepler:2026-10-06")])
    assert "| arm | kepler:2026-10-06 | zed:2026-10-06 | borges:2026-10-07 |" in text


def test_output_is_byte_identical_in_any_input_order(fleet, dirs, tmp_path):
    a, b = dirs
    texts = set()
    for perm in itertools.permutations([(a, KEPLER), (b, BORGES)]):
        texts.add(run(fleet, tmp_path, list(perm)))
    assert len(texts) == 1


# ----------------------------------------------------------------- 2. querySource
def test_query_source_split_sits_after_the_tables_per_arm_and_column(report):
    qs = report[report.index("## 2."):report.index("## 3.")]
    assert "| arm_x | thinking: 6, visible: 4 | visible: 4 |" in qs
    assert "| arm_y | thinking: 6, visible: 4 | - |" in qs
    assert "| route:prompt_surface | none: 10 | none: 4 |" in qs


# ----------------------------------------------------------------- 3. sweep
def test_sweep_pools_non_null_inputs_and_counts_the_excluded_null_rows(report):
    sw = report[report.index("## 3."):report.index("## 4.")]
    assert "route:prompt_surface" in sw
    # columns: setting | pooled p@5 | n steps pooled | inputs pooled | inputs excluded (null signals) | null rows excluded
    # round 1: 3b's as-recorded row pools the SAME inputs as its floors (A only), see the 3a/3b cells below
    assert "| as recorded | .400 | 10 | 1 | 1 | 4 |" in sw
    for floor in ("0.30", "0.35", "0.40"):
        assert f"| floor {floor} | .400 | 10 | 1 | 1 | 4 |" in sw
    for floor in ("0.45", "0.50", "0.55", "0.60", "0.65", "0.70"):
        assert f"| floor {floor} | .200 | 10 | 1 | 1 | 4 |" in sw


# ----------------------------------------------------------------- 4. pairs
def test_pair_verdicts_are_pooled_with_n(report):
    pv = report[report.index("## 4."):report.index("## 5.")]
    assert "| arm_x | arm_y | 4 | 1 | 1 | 6 |" in pv
    assert "| arm_x | route:prompt_surface | 0 | 0 | 2 | 2 |" in pv


def test_pairs_section_is_skipped_and_says_so_when_no_input_has_pairs(fleet, dirs, tmp_path):
    a, b = dirs
    os.unlink(a / "pairs.jsonl")
    os.unlink(b / "pairs.jsonl")
    text = run(fleet, tmp_path, [(a, KEPLER), (b, BORGES)])
    pv = text[text.index("## 4."):text.index("## 5.")]
    assert "skipped: no input has pairs.jsonl" in pv
    assert "| armA |" not in pv


# ----------------------------------------------------------------- 5. caveats
def test_caveats_are_rendered_from_each_inputs_data(report):
    cv = report[report.index("## 5."):]
    assert ("- kepler:2026-10-06 — date 2026-10-06 · steps rated 10 · goldens 10 · "
            "arms: arm_x, arm_y, route:prompt_surface · arm runs excluded by status: none · "
            "rater provenance not recorded in this input") in cv
    assert ("- borges:2026-10-07 — date 2026-10-07 · steps rated 4 · goldens 4 · "
            "arms: arm_x, arm_z, route:prompt_surface · arm runs excluded by status: arm_z 1 · "
            "rater provenance not recorded in this input") in cv


def test_rater_is_rendered_only_when_the_summary_records_it(fleet, dirs, tmp_path):
    a, b = dirs
    s = json.loads((a / "summary.json").read_text())
    s["rater"] = "self"
    _w(a / "summary.json", s)
    text = run(fleet, tmp_path, [(a, KEPLER), (b, BORGES)])
    cv = text[text.index("## 5."):]
    assert "rater: self" in cv
    assert cv.count("rater provenance not recorded in this input") == 1  # only B


# ----------------------------------------------------------------- D3 labels / errors
def test_missing_label_without_metadata_names_the_dir_and_writes_no_report(fleet, dirs, tmp_path):
    a, b = dirs
    out = tmp_path / "report.md"
    with pytest.raises(SystemExit) as exc:
        fleet.main([str(a), str(b), "--label", KEPLER, "--out", str(out)])
    assert str(exc.value) not in ("0", "") and str(b) in str(exc.value)
    assert not out.exists()


def test_label_is_not_needed_when_the_dirs_summary_carries_seat_and_date(fleet, dirs, tmp_path):
    a, b = dirs
    s = json.loads((b / "summary.json").read_text())
    s.update({"seat": "borges", "date": "2026-10-07"})
    _w(b / "summary.json", s)
    text = run(fleet, tmp_path, [(a, KEPLER), (b, None)])
    assert "| arm | kepler:2026-10-06 | borges:2026-10-07 |" in text


@pytest.mark.parametrize("bad", ["kepler", "kepler:10-06", "kepler:2026-13-01", ":2026-10-06", "kepler:20261006"])
def test_malformed_label_is_an_error_naming_the_dir(fleet, dirs, tmp_path, bad):
    a, _ = dirs
    out = tmp_path / "report.md"
    with pytest.raises(SystemExit) as exc:
        fleet.main([str(a), "--label", bad, "--out", str(out)])
    assert str(a) in str(exc.value)
    assert not out.exists()


def test_duplicate_seat_date_pair_is_an_error_naming_the_dir(fleet, dirs, tmp_path):
    a, b = dirs
    out = tmp_path / "report.md"
    with pytest.raises(SystemExit) as exc:
        fleet.main([str(a), str(b), "--label", KEPLER, "--label", KEPLER, "--out", str(out)])
    assert str(b) in str(exc.value) and KEPLER in str(exc.value)
    assert not out.exists()


def test_more_labels_than_dirs_is_an_error(fleet, dirs, tmp_path):
    a, _ = dirs
    out = tmp_path / "report.md"
    with pytest.raises(SystemExit):
        fleet.main([str(a), "--label", KEPLER, "--label", BORGES, "--out", str(out)])
    assert not out.exists()


def test_unreadable_empty_or_goldenless_dir_is_an_error_naming_it(fleet, dirs, tmp_path):
    a, _ = dirs
    out = tmp_path / "report.md"
    missing = tmp_path / "nope"
    empty = tmp_path / "empty"
    empty.mkdir()
    nogold = tmp_path / "nogold"
    (nogold / "golden").mkdir(parents=True)
    _w(nogold / "test" / "t.json", _rru("x", "arm_x", ["ob_0001"]))
    for bad in (missing, empty, nogold):
        with pytest.raises(SystemExit) as exc:
            fleet.main([str(a), str(bad), "--label", KEPLER, "--label", BORGES, "--out", str(out)])
        assert str(bad) in str(exc.value)
        assert not out.exists()


# ----------------------------------------------------------------- misc contract
def test_report_file_is_private_0600(fleet, dirs, tmp_path):
    a, b = dirs
    run(fleet, tmp_path, [(a, KEPLER), (b, BORGES)])
    assert stat.S_IMODE((tmp_path / "report.md").stat().st_mode) == 0o600


def test_scoring_is_imported_not_copied():
    src = (PROBE_DIR / "probe_fleet_report.py").read_text()
    # the p@5 / nDCG field NAMES are read from probe_eval's result; metric machinery must not appear
    for forbidden in ("apply_metric", "METRICS_TO_APPLY", "log2", "import math", "def _percentile", "def _p_at_5"):
        assert forbidden not in src, forbidden


# ============================================================================ round 1 (reviewer fairy)
# F1/F2: Section 3 renders two tables per route, each naming its population.
#   3a pooled over EVERY input (null similarity counts as 0 on a floor, as probe_eval does).
#   3b pooled over inputs with NO null similarity on the route, the same inputs in every row.
def _sec3(text: str) -> str:
    return text[text.index("## 3."):text.index("## 4.")]


def _tab(sec3: str, which: str) -> str:
    """The text of sub-table 3a or 3b (from its header sentence to the next header)."""
    start = sec3.index(f"#### {which}.")
    rest = sec3[start + 1:]
    nxt = [rest.index(m) for m in ("\n#### ", "\n### ") if m in rest]
    return sec3[start:start + 1 + (min(nxt) if nxt else len(rest))]


def test_section3_never_runs_shown_only_and_says_so(fleet, dirs, tmp_path):
    a, b = dirs
    for extra in ((), ("--shown-only",)):
        text = run(fleet, tmp_path, [(a, KEPLER), (b, BORGES)], extra)
        sec = _sec3(text)
        assert "never runs with --shown-only" in sec[:sec.index("### route:")]
    assert "mode: shown-only" in text  # the mode line at the top of the report stays


def test_each_route_has_a_3a_and_a_3b_table_with_named_populations(report):
    sec = _sec3(report)
    t3a, t3b = _tab(sec, "3a"), _tab(sec, "3b")
    assert sec.index("#### 3a.") < sec.index("#### 3b.")
    assert "EVERY input" in t3a.splitlines()[0]
    assert "| setting | pooled p@5 | n steps pooled | inputs pooled | null rows counted |" in t3a
    assert "NO null similarity" in t3b.splitlines()[0]
    assert "same inputs in every row" in t3b.splitlines()[0]
    assert "| setting | pooled p@5 | n steps pooled | inputs pooled | inputs excluded (null signals) | null rows excluded |" in t3b


def test_3a_pools_every_input_weighted_by_steps_and_counts_null_rows(report):
    # hand: A route p@5 .4 over 10 steps, B route .2 over 4. as recorded (10*.4+4*.2)/14 = 4.8/14 = .343, nulls 0
    #   floors .30 .35 .40: A .4, B .2 (ob_0001 sim .8 kept, null ob_0002 -> 0, dropped) -> 4.8/14 = .343, B's 4 null rows
    #   floors .45 .. .70 : A .2, B .2 -> (2+.8)/14 = .200
    t = _tab(_sec3(report), "3a")
    assert "| as recorded | .343 | 14 | 2 | 0 |" in t
    for floor in ("0.30", "0.35", "0.40"):
        assert f"| floor {floor} | .343 | 14 | 2 | 4 |" in t
    for floor in ("0.45", "0.50", "0.55", "0.60", "0.65", "0.70"):
        assert f"| floor {floor} | .200 | 14 | 2 | 4 |" in t


def test_3b_as_recorded_is_pooled_over_the_same_inputs_as_its_floors(report):
    # hand: only A has no null similarity -> as recorded is A alone: .400 over 10 steps, 1 input pooled,
    # B excluded (1) with its 4 null rows shown, exactly like the floor rows.
    t = _tab(_sec3(report), "3b")
    assert "| as recorded | .400 | 10 | 1 | 1 | 4 |" in t
    assert "| floor 0.30 | .400 | 10 | 1 | 1 | 4 |" in t
    assert "| floor 0.70 | .200 | 10 | 1 | 1 | 4 |" in t


# ---- F2: the reviewer's reproduction, fixtures S (one null similarity) and T (none) ----
# Both: 10 route steps, golden {ob_0001: 2, ob_0002: 0, ob_0003: 0}, candidates
#   ob_0001 (sim .9, rel 2) / ob_0002 (sim .8, rel 0) / ob_0003 (rel 0, sim .5; null on ONE step of S).
# Per step p@5 = 1 relevant / 5 = .200 in every setting (ob_0001 sim .9 survives every floor <= .70 and is the
# only relevant candidate), so every pooled value is .200.
#   3a (every input, 20 steps): every setting .200 | 20 | 2 ; null rows counted: as recorded 0 (probe_eval
#      reports 0 for the unfloored row), every floor 1 (S's single null candidate, T has none).
#   3b (inputs with no null similarity): only T pools -> .200 | 10 | 1 ; S is 1 excluded input with its 1 null row,
#      in every row including as recorded.
#   property: 3b's as-recorded (value, n steps, inputs pooled) equals T run ALONE.
def build_st(root: Path, qprefix: str, null_step: bool) -> Path:
    for i in range(1, 11):
        qid = f"{qprefix}{i:02d}"
        _golden(root, qid, {"ob_0001": 2, "ob_0002": 0, "ob_0003": 0})
        sim3 = None if (null_step and i == 4) else 0.5
        _w(root / "test" / f"{qid}-r.json", _rru(qid, "route:prompt_surface",
                                                 [("ob_0001", 0.9), ("ob_0002", 0.8), ("ob_0003", sim3)],
                                                 source=None, route=True))
    _w(root / "summary.json", {"steps_rated": 10, "steps_skipped_by_reason": {}})
    return root


@pytest.fixture()
def st_report(fleet, tmp_path):
    s = build_st(tmp_path / "S", "s", True)
    t = build_st(tmp_path / "T", "t", False)
    return run(fleet, tmp_path, [(s, "sseat:2026-10-06"), (t, "tseat:2026-10-07")]), s, t


FLOORS = ("0.30", "0.35", "0.40", "0.45", "0.50", "0.55", "0.60", "0.65", "0.70")


def test_st_3a_pools_all_twenty_steps_for_every_setting(st_report):
    t = _tab(_sec3(st_report[0]), "3a")
    assert "| as recorded | .200 | 20 | 2 | 0 |" in t
    for f in FLOORS:
        assert f"| floor {f} | .200 | 20 | 2 | 1 |" in t


def test_st_3b_pools_T_only_and_shows_S_as_one_excluded_input_with_its_null_row(st_report):
    t = _tab(_sec3(st_report[0]), "3b")
    assert "| as recorded | .200 | 10 | 1 | 1 | 1 |" in t
    for f in FLOORS:
        assert f"| floor {f} | .200 | 10 | 1 | 1 | 1 |" in t


def test_st_3b_as_recorded_equals_T_run_alone(fleet, st_report, tmp_path):
    _, s, t = st_report
    alone = run(fleet, tmp_path, [(t, "tseat:2026-10-07")])
    solo = [ln for ln in _tab(_sec3(alone), "3b").splitlines() if ln.startswith("| as recorded")]
    both = [ln for ln in _tab(_sec3(st_report[0]), "3b").splitlines() if ln.startswith("| as recorded")]
    assert solo and both
    assert solo[0].split("|")[2:5] == both[0].split("|")[2:5]  # pooled p@5, n steps, inputs pooled
    assert solo[0].split("|")[2:5] == [" .200 ", " 10 ", " 1 "]


def test_3b_says_so_in_one_line_when_no_input_qualifies(fleet, tmp_path):
    s = build_st(tmp_path / "S", "s", True)
    text = run(fleet, tmp_path, [(s, "sseat:2026-10-06")])
    t3b = _tab(_sec3(text), "3b")
    assert "no input qualifies: every input has a null similarity on this route (1 excluded, 1 null rows)" in t3b
    assert "| as recorded" not in t3b
    assert "| as recorded | .200 | 10 | 1 | 0 |" in _tab(_sec3(text), "3a")  # 3a still pools it


# ---- F3: the three surviving behaviours + the boundary, behaviourally ----
def build_custom(root: Path, qprefix: str, n_steps: int, arms) -> Path:
    """arms: [(arm, candidates, step numbers it runs on, ms function of the step number)]; golden
    {ob_0001: 2, ob_0002: 2} on every step."""
    for i in range(1, n_steps + 1):
        qid = f"{qprefix}{i:02d}"
        _golden(root, qid, {"ob_0001": 2, "ob_0002": 2})
        for arm, cands, steps, msf in arms:
            if i in steps:
                _w(root / "test" / f"{qid}-{arm.replace(':', '_')}.json", _rru(qid, arm, cands, ms=msf(i)))
    _w(root / "summary.json", {"steps_rated": n_steps, "steps_skipped_by_reason": {}})
    return root


def test_median_ms_n_counts_steps_that_have_a_latency(fleet, tmp_path):
    # arm_m on 10 steps returning [ob_0001] -> p@5 = 1/5 = .200 over n 10; ms present on steps 1..7 only
    # (10,20,..,70 -> median 40), None on steps 8..10 -> the ms cell is over 7 steps, hence parenthesised.
    d = build_custom(tmp_path / "M", "m", 10, [("arm_m", ["ob_0001"], range(1, 11),
                                                 lambda i: 10.0 * i if i <= 7 else None)])
    text = run(fleet, tmp_path, [(d, KEPLER)])
    assert "| arm_m | .200 (10) |" in text[text.index("### p@5"):text.index("### nDCG@10")]
    assert "| arm_m | (40) (7) |" in text[text.index("### median ms"):text.index("## 2.")]


def test_best_arm_ties_name_every_winner(fleet, tmp_path):
    d = build_custom(tmp_path / "T2", "t", 10, [("arm_p", ["ob_0001"], range(1, 11), lambda i: 1.0),
                                                 ("arm_q", ["ob_0001"], range(1, 11), lambda i: 1.0)])
    text = run(fleet, tmp_path, [(d, KEPLER)])
    assert "best p@5 per column: kepler:2026-10-06 arm_p .200 (10), arm_q .200 (10)" in text


def test_the_n_boundary_is_behavioural_9_is_parenthesised_10_and_11_are_plain_and_can_win(fleet, tmp_path):
    ds = [build_custom(tmp_path / f"N{n}", f"n{n}-", n, [("arm_n", ["ob_0001"], range(1, n + 1), lambda i: 1.0)])
          for n in (9, 10, 11)]
    text = run(fleet, tmp_path, [(ds[0], "s:2026-10-01"), (ds[1], "s:2026-10-02"), (ds[2], "s:2026-10-03")])
    p5 = text[text.index("### p@5"):text.index("### nDCG@10")]
    assert "| arm_n | (.200) (9) | .200 (10) | .200 (11) |" in p5
    assert ("best p@5 per column: s:2026-10-01 none (no arm with n >= 10) · "
            "s:2026-10-02 arm_n .200 (10) · s:2026-10-03 arm_n .200 (11)") in text


def test_a_parenthesised_arm_with_a_higher_value_is_not_named_best(fleet, tmp_path):
    # arm_lo on 10 steps [ob_0001] -> .200 (10); arm_hi on 5 steps [ob_0001, ob_0002] -> 2/5 = .400 (5)
    d = build_custom(tmp_path / "H", "h", 10, [("arm_lo", ["ob_0001"], range(1, 11), lambda i: 1.0),
                                                ("arm_hi", ["ob_0001", "ob_0002"], range(1, 6), lambda i: 1.0)])
    text = run(fleet, tmp_path, [(d, KEPLER)])
    assert "| arm_hi | (.400) (5) |" in text
    best = [ln for ln in text.splitlines() if ln.startswith("best p@5")][0]
    assert best == "best p@5 per column: kepler:2026-10-06 arm_lo .200 (10)"


# ---- F4: nits ----
def test_readme_example_uses_the_venv_python_and_says_why():
    readme = (PROBE_DIR / "README.md").read_text()
    sec = readme[readme.index("## Fleet report across seats and days"):]
    assert "~/.engram/venv/bin/python probe_fleet_report.py" in sec
    assert "fastmcp" in sec and "python3 probe_fleet_report.py" not in sec


def test_out_into_a_nonexistent_directory_exits_nonzero_naming_it_without_a_traceback(dirs, tmp_path):
    import subprocess
    a, _ = dirs
    target = tmp_path / "no_such_dir" / "r.md"
    proc = subprocess.run([sys.executable, str(PROBE_DIR / "probe_fleet_report.py"), str(a), "--label", KEPLER,
                           "--out", str(target)], capture_output=True, text=True)
    assert proc.returncode != 0
    assert str(tmp_path / "no_such_dir") in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not target.exists() and not target.parent.exists()


def test_label_overriding_a_different_summary_seat_date_is_said_in_that_inputs_caveats_line(fleet, dirs, tmp_path):
    a, b = dirs
    s = json.loads((a / "summary.json").read_text())
    s.update({"seat": "borges", "date": "2026-10-07"})
    _w(a / "summary.json", s)
    text = run(fleet, tmp_path, [(a, KEPLER), (b, BORGES)])
    cv = text[text.index("## 5."):]
    line_a = [ln for ln in cv.splitlines() if ln.startswith("- kepler:2026-10-06")][0]
    line_b = [ln for ln in cv.splitlines() if ln.startswith("- borges:2026-10-07")][0]
    assert "label overrides summary seat/date (summary: borges:2026-10-07)" in line_a
    assert "label overrides" not in line_b


def test_no_override_note_when_the_label_agrees_with_the_summary(fleet, dirs, tmp_path):
    a, b = dirs
    s = json.loads((a / "summary.json").read_text())
    s.update({"seat": "kepler", "date": "2026-10-06"})
    _w(a / "summary.json", s)
    assert "label overrides" not in run(fleet, tmp_path, [(a, KEPLER), (b, BORGES)])
