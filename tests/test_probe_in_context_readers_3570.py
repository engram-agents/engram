"""Contract tests for engram-alpha#3570: the route arms' in-context rate, and the offline sidecar.

Written BEFORE the implementation (contract-first). Every fixture is built from literals here and
every expected number is hand-computed in the comment next to it.

C1  convert_ratings._route_rru adds metadata.ledgerTs (the ledger line's ts, int ms, else null).
C2  probe_eval in_context_rate: signals["inContext"] else signals["in_context"]; denominator = the
    top-5 candidates whose flag is KNOWN; None when none is known.
C3  evaluate(..., in_context_sidecar=DIR) / --in-context-sidecar DIR: route RRUs join the sidecar by
    (route, ledgerTs), candidates by id; an unmatched route step is unknown, never the hook value.
C4  probe_fleet_report Section 6 + --in-context-sidecar SEAT:DATE=DIR + one caveat line per input.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROBE_DIR = Path(__file__).resolve().parent.parent / "tools" / "probes" / "mods-recall"


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, PROBE_DIR / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ev():
    return _load("probe_eval_t3570", "probe_eval.py")


@pytest.fixture()
def fleet():
    return _load("probe_fleet_report_t3570", "probe_fleet_report.py")


@pytest.fixture()
def convert():
    return _load("convert_ratings_t3570", "convert_ratings.py")


def _w(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _golden(root: Path, qid: str) -> None:
    _w(root / "golden" / f"q_{qid}.json", {
        "query_id": qid, "query_text": "g",
        "ranked_candidates": [{"id": "ob_0001", "grading": {"relevance": 2}}]})


IDS5 = ["ob_0001", "ob_0002", "ob_0003", "ob_0004", "ob_0005"]


def _rru(qid, arm, ids, signals_of, **meta):
    """signals_of(i) -> the signals dict of the i-th candidate."""
    return {"query_id": qid, "query_text": "q",
            "ranked_nodes": [{"id": c, "rank": i + 1, "grading": None, "signals": signals_of(i), "notes": ""}
                             for i, c in enumerate(ids)],
            "metadata": {"arm": arm, "status": "ok", "ms": None, "msOverStep": None,
                         "querySource": None, "shownIds": None, **meta}}


def _flags(values, key="in_context"):
    return lambda i: ({key: values[i]} if i < len(values) and values[i] != "absent" else {})


def _one_route(root: Path, qid: str, ids, signals_of, **meta) -> None:
    _golden(root, qid)
    _w(root / "test" / f"{qid}.json", _rru(qid, "route:prompt_surface", ids, signals_of, **meta))


def _rate(ev, root, arm="route:prompt_surface", **kw):
    return ev.evaluate(str(root / "golden"), str(root / "test"), **kw)["arms"][arm]["in_context_rate"]


# --------------------------------------------------------------------------- C1 converter
def _entry(ts_present=True, ts=1000):
    line = {"route": "tool_recall", "session_id": "s", "fire_key": "FK", "query": "lq",
            "candidates": [{"id": "ob_0001", "similarity": 0.5, "in_context": True, "in_context_why": "shown"}],
            "missing_signals": [], "shown": ["ob_0001"], "cut": {}, "source": "test"}
    if ts_present:
        line["ts"] = ts
    return {"route": "tool_recall", "letter": "T", "fire_key": "FK", "line": line}


def _route_rru(convert, entry):
    return convert._route_rru(entry, "q1", {"stepMs": 1000.0, "model": "m", "sessionId": "ss"}, Path("sess"))


def test_c1_route_rru_carries_ledger_ts_and_nothing_else_changes(convert):
    rru = _route_rru(convert, _entry(ts=1000))
    # the converter output before #3570 plus exactly one key, metadata.ledgerTs
    assert rru == {
        "query_id": "q1", "query_text": "lq",
        "ranked_nodes": [{"id": "ob_0001", "rank": 1, "grading": None,
                          "signals": {"similarity": 0.5, "in_context": True, "in_context_why": "shown"},
                          "notes": ""}],
        "metadata": {"arm": "route:tool_recall", "armLetter": "T", "status": "ok", "ms": None,
                     "msOverStep": None, "stepMs": 1000.0, "model": "m", "querySource": None,
                     "visibleChars": None, "envelope": None, "sessionId": "s", "shownIds": ["ob_0001"],
                     "missing_signals": [], "fire_key": "FK", "unmatched": False, "cut": {},
                     "source": "test", "ledgerTs": 1000}}


def test_c1_ledger_ts_is_null_when_absent_or_not_a_number(convert):
    assert _route_rru(convert, _entry(ts_present=False))["metadata"]["ledgerTs"] is None
    assert _route_rru(convert, _entry(ts="soon"))["metadata"]["ledgerTs"] is None
    assert _route_rru(convert, _entry(ts=True))["metadata"]["ledgerTs"] is None
    assert _route_rru(convert, _entry(ts=1000.0))["metadata"]["ledgerTs"] == 1000


# --------------------------------------------------------------------------- C2 key + known-only
def test_c2_route_flag_is_read_from_snake_case_over_known_candidates_only(ev, tmp_path):
    # top-5 in_context [true, true, false, null, null] -> known 3, true 2 -> 2/3 = .667.
    # A sixth candidate (true) is outside the top-5 and must not count.
    _one_route(tmp_path, "s1", IDS5 + ["ob_0006"], _flags([True, True, False, None, None, True]))
    assert _rate(ev, tmp_path) == pytest.approx(2 / 3)


def test_c2_all_null_or_absent_is_unknown_and_renders_a_dash(ev, tmp_path):
    _one_route(tmp_path, "s1", IDS5, _flags([None] * 5))
    _one_route(tmp_path, "s2", IDS5, _flags(["absent"] * 5))
    result = ev.evaluate(str(tmp_path / "golden"), str(tmp_path / "test"))
    assert result["arms"]["route:prompt_surface"]["in_context_rate"] is None
    row = [ln for ln in ev.render_report(result).splitlines() if ln.startswith("| route:prompt_surface |")][0]
    assert row.split(" | ")[-2] == "-"  # the inContext (top-5) cell


def test_c2_camel_case_wins_when_both_names_are_present(ev, tmp_path):
    # inContext false on all five, in_context true on all five -> inContext wins -> 0/5 = 0.0, not 1.0
    _one_route(tmp_path, "s1", IDS5, lambda i: {"inContext": False, "in_context": True})
    assert _rate(ev, tmp_path) == 0.0


def test_c2_an_arm_row_is_unchanged(ev, tmp_path):
    # arm candidates carry a bool each: [true, false, false] -> 1/3 (denominator is every candidate)
    _golden(tmp_path, "s1")
    _w(tmp_path / "test" / "a.json", _rru("s1", "arm_x", IDS5[:3], _flags([True, False, False], "inContext")))
    assert _rate(ev, tmp_path, arm="arm_x") == pytest.approx(1 / 3)


# --------------------------------------------------------------------------- C3 sidecar
FK_SHARED = "FKshared"


def _sidecar_row(ts, fire_key, flags_by_id, route="prompt_surface", order=None):
    ids = order or list(flags_by_id)
    return {"ts": ts, "route": route, "fire_key": fire_key, "boundary_index": None, "boundary_ts": None,
            "candidates": [{"id": i, "in_context_offline": flags_by_id[i], "in_context_offline_why": "x",
                            "in_context_hook": True, "in_context_hook_why": "shown"} for i in ids]}


def _two_true(extra=None):
    """3 of the top-5 false: ob_0001, ob_0002 true."""
    return {"ob_0001": True, "ob_0002": True, "ob_0003": False, "ob_0004": False, "ob_0005": False, **(extra or {})}


def build_big(root: Path) -> Path:
    """Route arm of 10 steps; the hook says every top-5 candidate is in context (50 of 50 -> 1.000).
    Sidecar rows ts 1001..1010 (also fire_keys F1..F8, and FKshared on 1009 and 1010).
      steps 1-6   ts 1001-1006  sidecar 2 true of 5            -> 2 each = 12
      step 1      sidecar lists ob_0006 FIRST (true), so a rank-based lookup would see 3 true there
      step 7      NO ledgerTs (null), fire_key F7, a sidecar row with fire_key F7 exists (ts 1007)  -> unmatched
      step 8      ledgerTs 9999 (no sidecar row), fire_key F8 exists on row ts 1008                 -> unmatched
      step 9      ts 1009, fire_key FKshared, sidecar 2 true                                        -> 2
      step 10     ts 1010, fire_key FKshared, sidecar 4 true (ob_0005 false only)                   -> 4
    sidecar rate = (12 + 2 + 4) / (8 steps * 5) = 18/40 = .450 ; matched 8, unmatched 2
    hook rate    = 50/50 = 1.000 (n 10)
    """
    rows = []
    for i in range(1, 11):
        qid = f"s{i:02d}"
        _golden(root, qid)
        ts = {7: None, 8: 9999}.get(i, 1000 + i)
        fk = FK_SHARED if i in (9, 10) else f"F{i}"
        _w(root / "test" / f"{qid}.json",
           _rru(qid, "route:prompt_surface", IDS5, lambda _i: {"in_context": True, "similarity": 0.5},
                fire_key=fk, ledgerTs=ts))
        if i == 1:
            rows.append(_sidecar_row(1001, "F1", _two_true({"ob_0006": True}),
                                     order=["ob_0006", "ob_0001", "ob_0002", "ob_0003", "ob_0004", "ob_0005"]))
        elif i == 10:
            rows.append(_sidecar_row(1010, FK_SHARED, {**_two_true(), "ob_0003": True, "ob_0004": True}))
        else:
            rows.append(_sidecar_row(1000 + i, fk, _two_true()))
    # an arm step that must not be touched by the sidecar: inContext [true, false, false] -> .333
    _w(root / "test" / "arm.json", _rru("s01", "arm_x", IDS5[:3], _flags([True, False, False], "inContext")))
    side = root / "side"
    side.mkdir()
    (side / "prompt_surface.in_context_offline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return side


def _evaluate_big(ev, root, sidecar=True):
    return ev.evaluate(str(root / "golden"), str(root / "test"),
                       in_context_sidecar=str(root / "side") if sidecar else None)


def test_c3_hook_source_when_no_sidecar_is_given(ev, tmp_path):
    build_big(tmp_path)
    res = _evaluate_big(ev, tmp_path, sidecar=False)
    assert res["in_context_source"] == "hook"
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] == 1.0   # 50/50
    assert res["arms"]["route:prompt_surface"]["in_context_n"] == 10


def test_c3_sidecar_rate_joins_by_route_and_ts_and_candidates_by_id(ev, tmp_path):
    build_big(tmp_path)
    res = _evaluate_big(ev, tmp_path)
    assert res["in_context_source"] == "offline sidecar"
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] == pytest.approx(18 / 40)  # .450
    assert res["arms"]["route:prompt_surface"]["in_context_n"] == 8


def test_c3_unmatched_steps_are_unknown_not_the_hook_value_and_are_counted(ev, tmp_path):
    build_big(tmp_path)
    res = _evaluate_big(ev, tmp_path)
    # steps 7 (no ledgerTs) and 8 (ts 9999 not in the sidecar): 2 unmatched, 8 matched.
    # Had they fallen back to the hook (5 true each) the rate would be (18+10)/50 = .560.
    assert (res["sidecar_matched"], res["sidecar_unmatched"]) == (8, 2)
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] != pytest.approx(28 / 50)


def test_c3_same_fire_key_different_ts_joins_by_ts(ev, tmp_path):
    build_big(tmp_path)
    side = tmp_path / "side" / "prompt_surface.in_context_offline.jsonl"
    # keep only the two FKshared rows: step 9 -> ts 1009 (2 true), step 10 -> ts 1010 (4 true).
    # by ts: (2 + 4) / 10 = .600 ; a fire_key join would give both steps the same row: .400 or .800.
    keep = [ln for ln in side.read_text().splitlines() if FK_SHARED in ln]
    assert len(keep) == 2
    side.write_text("\n".join(keep) + "\n")
    res = _evaluate_big(ev, tmp_path)
    assert (res["sidecar_matched"], res["sidecar_unmatched"]) == (2, 8)
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] == pytest.approx(6 / 10)


def test_c3_arm_rows_are_untouched_by_the_sidecar(ev, tmp_path):
    build_big(tmp_path)
    for sidecar in (False, True):
        assert _evaluate_big(ev, tmp_path, sidecar)["arms"]["arm_x"]["in_context_rate"] == pytest.approx(1 / 3)


def test_c3_a_candidate_id_absent_from_the_sidecar_row_or_null_there_is_unknown(ev, tmp_path):
    _one_route(tmp_path, "s1", IDS5, lambda i: {"in_context": True}, ledgerTs=5)
    side = tmp_path / "side"
    side.mkdir()
    # row knows ob_0001 true, ob_0002 false, ob_0003 null; ob_0004/ob_0005 absent -> known 2, true 1 -> .500
    (side / "prompt_surface.in_context_offline.jsonl").write_text(json.dumps(
        _sidecar_row(5, "k", {"ob_0001": True, "ob_0002": False, "ob_0003": None})) + "\n")
    assert _rate(ev, tmp_path, in_context_sidecar=str(side)) == pytest.approx(0.5)


def test_c3_report_line_is_the_first_thing_after_per_arm_and_only_with_route_rows(ev, tmp_path):
    build_big(tmp_path)
    text = ev.render_report(_evaluate_big(ev, tmp_path))
    lines = text.splitlines()
    at = lines.index("## Per arm")
    assert lines[at + 1] == "" and lines[at + 2] == "in-context flag: offline sidecar (matched 8 of 10 route steps)"
    assert lines[at + 3] == "" and lines[at + 4].startswith("| arm |")
    hook = ev.render_report(_evaluate_big(ev, tmp_path, sidecar=False)).splitlines()
    assert hook[hook.index("## Per arm") + 2] == "in-context flag: ledger/hook value"
    # no route row -> no line at all
    other = tmp_path / "other"
    _golden(other, "s1")
    _w(other / "test" / "a.json", _rru("s1", "arm_x", IDS5[:3], _flags([True, False, False], "inContext")))
    assert "in-context flag" not in ev.render_report(ev.evaluate(str(other / "golden"), str(other / "test")))


def test_c3_the_route_cell_in_the_table_is_the_sidecar_rate(ev, tmp_path):
    build_big(tmp_path)
    row = [ln for ln in ev.render_report(_evaluate_big(ev, tmp_path)).splitlines()
           if ln.startswith("| route:prompt_surface |")][0]
    assert row.split(" | ")[-2] == "0.450"


def _cli(ev, root, *extra):
    return ev.main(["--golden", str(root / "golden"), "--test", str(root / "test"),
                    "--out", str(root / "rep.md"), *extra])


def test_c3_cli_flag_reads_the_sidecar_dir(ev, tmp_path, capsys):
    build_big(tmp_path)
    assert _cli(ev, tmp_path, "--in-context-sidecar", str(tmp_path / "side")) == 0
    assert "in-context flag: offline sidecar (matched 8 of 10 route steps)" in (tmp_path / "rep.md").read_text()


@pytest.mark.parametrize("content, why", [
    ("{not json\n", "not json"),
    (json.dumps({"route": "prompt_surface", "candidates": []}) + "\n", "no ts"),
    (json.dumps({"ts": 1, "candidates": []}) + "\n", "no route"),
    (json.dumps({"ts": 1, "route": "prompt_surface"}) + "\n", "no candidates"),
    (json.dumps({"ts": 1, "route": "prompt_surface", "candidates": "x"}) + "\n", "candidates not a list"),
])
def test_c3_wrong_sidecar_shapes_exit_nonzero_naming_the_file(ev, tmp_path, content, why):
    build_big(tmp_path)
    (tmp_path / "side" / "prompt_surface.in_context_offline.jsonl").write_text(content)
    with pytest.raises(SystemExit) as err:
        _evaluate_big(ev, tmp_path)
    assert err.value.code not in (0, None), why
    assert "prompt_surface.in_context_offline.jsonl" in str(err.value.code), why


def test_c3_a_missing_route_file_is_a_warning_and_all_unmatched_not_a_crash(ev, tmp_path, capsys):
    build_big(tmp_path)
    (tmp_path / "side" / "prompt_surface.in_context_offline.jsonl").unlink()
    res = _evaluate_big(ev, tmp_path)
    assert (res["sidecar_matched"], res["sidecar_unmatched"]) == (0, 10)
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] is None
    assert "prompt_surface.in_context_offline.jsonl" in capsys.readouterr().err


def test_c3_a_sidecar_dir_that_does_not_exist_is_an_error_naming_it(ev, tmp_path):
    build_big(tmp_path)
    with pytest.raises(SystemExit) as err:
        ev.evaluate(str(tmp_path / "golden"), str(tmp_path / "test"), in_context_sidecar=str(tmp_path / "nope"))
    assert str(tmp_path / "nope") in str(err.value.code)


# --------------------------------------------------------------------------- C4 fleet
def build_small(root: Path) -> Path:
    """Hook only: 3 route steps, in_context [true, false, false, false, false] -> 3/15 = .200 (n 3)."""
    for i in range(1, 4):
        qid = f"b{i}"
        _golden(root, qid)
        _w(root / "test" / f"{qid}.json",
           _rru(qid, "route:prompt_surface", IDS5, _flags([True, False, False, False, False]),
                ledgerTs=2000 + i))
    _w(root / "summary.json", {"steps_rated": 3})
    return root


KEP, BOR = "kepler:2026-10-06", "borges:2026-10-07"


def _fleet(fleet, tmp_path, *extra):
    a = tmp_path / "A"
    build_big(a)
    _w(a / "summary.json", {"steps_rated": 10})
    b = build_small(tmp_path / "B")
    out = tmp_path / "fleet.md"
    argv = [str(a), str(b), "--label", KEP, "--label", BOR, "--out", str(out), *extra]
    assert fleet.main(argv) == 0
    return out.read_text()


def _sec6(text):
    start = text.index("## 6.")
    return text[start:]


def test_c4_section6_after_caveats_with_hand_computed_cells(fleet, tmp_path):
    text = _fleet(fleet, tmp_path, "--in-context-sidecar", f"{KEP}={tmp_path / 'A' / 'side'}")
    assert text.index("## 5.") < text.index("## 6. In-context rate (top-5 of each step)")
    s6 = _sec6(text)
    # kepler: sidecar rate 18/40 = .450 over 8 steps (n < 10 -> parenthesised); borges: hook 3/15 = .200 over 3 steps
    assert "| route:prompt_surface | (.450) (8) | (.200) (3) |" in s6
    # arm_x only exists in kepler: 1/3 over 1 step (arm row, inContext)
    assert "| arm_x | (.333) (1) | - |" in s6


def test_c4_hook_cell_with_ten_steps_is_not_parenthesised(fleet, tmp_path):
    text = _fleet(fleet, tmp_path)
    assert "| route:prompt_surface | 1.000 (10) | (.200) (3) |" in _sec6(text)


def test_c4_each_column_names_its_flag_source_and_counts(fleet, tmp_path):
    s6 = _sec6(_fleet(fleet, tmp_path, "--in-context-sidecar", f"{KEP}={tmp_path / 'A' / 'side'}"))
    assert f"- {KEP}: flag source: offline sidecar (route rows: matched 8, unmatched 2; arm rows: inContext)" in s6
    assert f"- {BOR}: flag source: ledger/hook value (route rows) · inContext (arm rows)" in s6


def test_c4_caveats_gain_one_flag_source_line_per_input(fleet, tmp_path):
    text = _fleet(fleet, tmp_path, "--in-context-sidecar", f"{KEP}={tmp_path / 'A' / 'side'}")
    cv = text[text.index("## 5."):text.index("## 6.")]
    assert f"  - in-context flag, {KEP}: offline sidecar (matched 8 of 10 route steps)" in cv
    assert f"  - in-context flag, {BOR}: ledger/hook value" in cv
    assert cv.count("in-context flag,") == 2


def test_c4_sections_1_to_4_do_not_depend_on_the_sidecar(fleet, tmp_path):
    t1 = _fleet(fleet, tmp_path / "x")
    t2 = _fleet(fleet, tmp_path / "y", "--in-context-sidecar", f"{KEP}={tmp_path / 'y' / 'A' / 'side'}")
    assert t1[:t1.index("## 5.")] == t2[:t2.index("## 5.")]


def test_c4_section6_is_omitted_silently_with_the_reason_on_stderr_when_nothing_is_known(fleet, tmp_path, capsys):
    for i in range(1, 3):
        _golden(tmp_path / "N", f"n{i}")
        _w(tmp_path / "N" / "test" / f"n{i}.json", _rru(f"n{i}", "route:prompt_surface", IDS5, lambda _i: {}))
    _w(tmp_path / "N" / "summary.json", {"steps_rated": 2})
    out = tmp_path / "f.md"
    assert fleet.main([str(tmp_path / "N"), "--label", KEP, "--out", str(out)]) == 0
    text = out.read_text()
    assert "## 6." not in text and "## 5." in text
    assert "no known in-context flag" in capsys.readouterr().err


def test_c4_sidecar_label_must_be_one_of_the_labels_and_the_error_names_it(fleet, tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    build_big(a)
    build_small(b)
    with pytest.raises(SystemExit) as err:
        fleet.main([str(a), str(b), "--label", KEP, "--label", BOR, "--out", str(tmp_path / "f.md"),
                    "--in-context-sidecar", f"ghost:2026-01-01={tmp_path / 'A' / 'side'}"])
    assert "ghost:2026-01-01" in str(err.value.code)
    with pytest.raises(SystemExit) as err:
        fleet.main([str(a), str(b), "--label", KEP, "--label", BOR, "--out", str(tmp_path / "f.md"),
                    "--in-context-sidecar", "no-equals-sign"])
    assert "no-equals-sign" in str(err.value.code)


def test_c1_c3_a_converter_made_route_rru_joins_the_sidecar_by_its_ledger_ts(convert, ev, tmp_path):
    # converter -> eval end to end on literals: the ledger line's ts 77 must reach the join
    entry = _entry(ts=77)
    entry["line"]["candidates"] = [{"id": i, "in_context": True} for i in IDS5[:2]]
    rru = _route_rru(convert, entry)
    rru["metadata"]["arm"] = "route:prompt_surface"
    _golden(tmp_path, "q1")
    _w(tmp_path / "test" / "r.json", rru)
    side = tmp_path / "side"
    side.mkdir()
    # offline: ob_0001 true, ob_0002 false -> 1/2 = .500 (the hook said 2/2 = 1.0)
    (side / "prompt_surface.in_context_offline.jsonl").write_text(
        json.dumps(_sidecar_row(77, "FK", {"ob_0001": True, "ob_0002": False})) + "\n")
    res = ev.evaluate(str(tmp_path / "golden"), str(tmp_path / "test"), in_context_sidecar=str(side))
    assert (res["sidecar_matched"], res["arms"]["route:prompt_surface"]["in_context_rate"]) == (1, 0.5)


# --------------------------------------------------------------------------- C3 join-key hardening (review round 1)
def _steps(root: Path, specs) -> None:
    """specs: [(qid, route, ledgerTs)], each a top-5 route step whose hook flags are all true."""
    for qid, route, ts in specs:
        _golden(root, qid)
        _w(root / "test" / f"{qid}.json",
           _rru(qid, f"route:{route}", IDS5, lambda _i: {"in_context": True}, ledgerTs=ts))


def _write_sidecar(root: Path, route: str, rows) -> Path:
    side = root / "side"
    side.mkdir(exist_ok=True)
    (side / f"{route}.in_context_offline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return side


ALL_T = dict.fromkeys(IDS5, True)
ALL_F = dict.fromkeys(IDS5, False)


def _eval_side(ev, root, side):
    return ev.evaluate(str(root / "golden"), str(root / "test"), in_context_sidecar=str(side))


def test_f1_a_duplicate_route_ts_is_unmatched_not_last_or_first_wins(ev, tmp_path):
    # a: ts 1, one row, 2 true of 5.  b: ts 2 twice, all-true then all-false (different flags).
    # b unmatched: rate = 2/5 = .400, matched 1, unmatched 1.
    # last-wins would give b 0 true: (2+0)/10 = .200, matched 2. first-wins: (2+5)/10 = .700.
    _steps(tmp_path, [("a", "prompt_surface", 1), ("b", "prompt_surface", 2)])
    side = _write_sidecar(tmp_path, "prompt_surface", [
        _sidecar_row(1, "ka", _two_true()), _sidecar_row(2, "kb", ALL_T), _sidecar_row(2, "kb2", ALL_F)])
    res = _eval_side(ev, tmp_path, side)
    assert (res["sidecar_matched"], res["sidecar_unmatched"]) == (1, 1)
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] == pytest.approx(0.4)


def test_f1_a_triple_duplicate_route_ts_is_unmatched(ev, tmp_path):
    # ts 2 three times: all-true, all-false, all-true. last-wins and first-wins both see 5 true:
    # (2+5)/10 = .700 matched 2. Correct: b unmatched -> 2/5 = .400, matched 1.
    _steps(tmp_path, [("a", "prompt_surface", 1), ("b", "prompt_surface", 2)])
    side = _write_sidecar(tmp_path, "prompt_surface", [
        _sidecar_row(1, "ka", _two_true()), _sidecar_row(2, "k1", ALL_T), _sidecar_row(2, "k2", ALL_F),
        _sidecar_row(2, "k3", ALL_T)])
    res = _eval_side(ev, tmp_path, side)
    assert (res["sidecar_matched"], res["sidecar_unmatched"]) == (1, 1)
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] == pytest.approx(0.4)


def test_f2_the_route_is_part_of_the_join_key(ev, tmp_path):
    # two routes, the SAME ts 7. prompt_surface file: all true (5/5 = 1.000); tool_recall file: 1 true (1/5 = .200).
    # each route's rate comes from its own file; a route-blind lookup gives one route the other's row.
    _steps(tmp_path, [("p", "prompt_surface", 7), ("t", "tool_recall", 7)])
    _write_sidecar(tmp_path, "prompt_surface", [_sidecar_row(7, "k", ALL_T, route="prompt_surface")])
    side = _write_sidecar(tmp_path, "tool_recall", [
        _sidecar_row(7, "k", {**ALL_F, "ob_0001": True}, route="tool_recall")])
    arms = _eval_side(ev, tmp_path, side)["arms"]
    assert arms["route:prompt_surface"]["in_context_rate"] == pytest.approx(1.0)
    assert arms["route:tool_recall"]["in_context_rate"] == pytest.approx(0.2)


def test_f3_a_whole_number_float_ts_in_the_sidecar_matches_an_int_ledger_ts(ev, tmp_path):
    # step ledgerTs 1790849100000 (int); sidecar ts 1790849100000.0: matches -> 2/5 = .400, matched 1.
    _steps(tmp_path, [("a", "prompt_surface", 1790849100000)])
    side = _write_sidecar(tmp_path, "prompt_surface", [_sidecar_row(1790849100000.0, "k", _two_true())])
    res = _eval_side(ev, tmp_path, side)
    assert (res["sidecar_matched"], res["sidecar_unmatched"]) == (1, 0)
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] == pytest.approx(0.4)


def test_f4_a_whole_number_float_ledger_ts_matches_an_int_sidecar_ts(ev, tmp_path):
    # RRU ledgerTs 1790849100000.0 (hand-written), sidecar ts int: matches -> .400, matched 1.
    _steps(tmp_path, [("a", "prompt_surface", 1790849100000.0)])
    side = _write_sidecar(tmp_path, "prompt_surface", [_sidecar_row(1790849100000, "k", _two_true())])
    res = _eval_side(ev, tmp_path, side)
    assert (res["sidecar_matched"], res["sidecar_unmatched"]) == (1, 0)
    assert res["arms"]["route:prompt_surface"]["in_context_rate"] == pytest.approx(0.4)


@pytest.mark.parametrize("ts", [1790849100000.5, True])
def test_f4_a_non_integral_float_or_bool_ledger_ts_stays_unmatched(ev, tmp_path, ts):
    # sidecar rows exist at ts 1 (True == 1 in Python) and at 1790849100000; neither may join a step
    # whose ledgerTs is True or 1790849100000.5.
    _steps(tmp_path, [("a", "prompt_surface", ts)])
    side = _write_sidecar(tmp_path, "prompt_surface", [_sidecar_row(1, "k", ALL_T),
                                                        _sidecar_row(1790849100000, "k2", ALL_T)])
    res = _eval_side(ev, tmp_path, side)
    assert (res["sidecar_matched"], res["sidecar_unmatched"]) == (0, 1)
