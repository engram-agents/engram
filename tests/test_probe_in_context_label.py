"""Contract tests for the rater's `in_context` mark in tools/probes/mods-recall.

Contract-first: these assertions were written BEFORE the label existed in convert_ratings.py,
probe_eval.py and probe_fleet_report.py. Every expected number is hand-computed in the comment
beside it from the fixture literals, never read off the tool.

NAME COLLISION, stated once: candidates carry an AUTOMATIC boolean `inContext` (hooks/step.ts)
and recompute_in_context.py writes a sidecar for it. The `in_context` tested here is a RATER's
mark in the ratings file. The two are independent; nothing here touches the automatic one.

Node-id-shaped ids are BUILT by `nid`, never spelled (the repo scans for literal ids).

Contract:
  C6  converter: an `in_context` id gets relevance grade 0; the golden's context records
      `inContextIds` (sorted) and `labelsOffered` true ONLY when the ratings step has the
      `in_context` key (even empty); an id in two of good / noise / in_context dies naming turn,
      step, ids and the two lists, before anything is written; a ratings file without the key
      converts to the SAME golden bytes as before the label existed.
  C7  probe_eval: p@5 counts an in_context id as NOT good; per arm the share = shown ids marked
      in_context / shown ids, pooled over steps whose golden offered the label; old goldens and
      a zero denominator read None (printed as an em dash, never 0).
  C8  probe_fleet_report: the share table exists only when some input offers the label.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO_ROOT / "tools" / "probes" / "mods-recall"
DASH = "—"


def nid(prefix: str, n: int) -> str:
    return f"{prefix}_{n}"


A, B, C, D, E, F = (nid("ob", 5000 + i) for i in range(1, 7))
X, Y, Z = (nid("ob", 5100 + i) for i in range(1, 4))


def _load(name: str):
    path = PROBE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"probe_label_{name}", path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def convert():
    return _load("convert_ratings")


@pytest.fixture()
def evaluator():
    return _load("probe_eval")


@pytest.fixture()
def fleet():
    return _load("probe_fleet_report")


def _w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


# --------------------------------------------------------------------------- C6 converter

TURN = "bbbbbbbb-1111-2222-3333-444444444444"
Q = "tbbbbbbbb-s3"  # the converter's query id for TURN, step 3
BLIND = {"A": "thinking_tail", "B": "thinking_full"}


def _cand(node_id: str, rank: int) -> dict:
    return {"id": node_id, "type": "observation", "claim": "c", "rank": rank, "composite_score": 0.5,
            "similarity": 0.5, "match_type": "semantic", "armsHit": [], "inContext": False, "principle": False}


def _session(tmp_path: Path, rating_step: dict) -> Path:
    """One step, arms thinking_tail [A,B,C] and thinking_full [B,D]; the request showed all of it."""
    root = tmp_path / "session"
    arms = {
        "thinking_tail": {"arm": "thinking_tail", "query": "q", "candidates": [_cand(i, r + 1) for r, i in enumerate([A, B, C])],
                          "ms": 10.0, "msOverStep": 0.01},
        "thinking_full": {"arm": "thinking_full", "query": "q", "candidates": [_cand(i, r + 1) for r, i in enumerate([B, D])],
                          "ms": 10.0, "msOverStep": 0.01},
    }
    _w(root / "steps" / f"{TURN}-003.json", {"turnId": TURN, "index": 3, "model": "m", "stopReason": "tool_use",
                                              "stepMs": 1000.0, "messagesScanned": 3, "engram_server": "engram",
                                              "armBlind": BLIND, "arms": arms, "ts": 1})
    _w(root / "requests" / f"{TURN}.json", {"turnId": TURN, "chars": 100, "omittedSteps": 0,
                                           "shown": {"3": {"A": [A, B, C], "B": [B, D]}}})
    _w(root / "ratings" / f"{TURN}-10.json", {"ts": 10, "sessionId": "s", "turnId": TURN,
                                              "steps": [rating_step], "ratedBy": "m"})
    return root


def _golden(out: Path) -> dict:
    return json.loads((out / "golden" / f"q_{Q}.json").read_text())


def _grades(golden: dict) -> dict:
    return {c["id"]: c["grading"]["relevance"] for c in golden["ranked_candidates"]}


def test_c6_in_context_ids_get_grade_zero_and_the_golden_records_them(convert, tmp_path):
    root = _session(tmp_path, {"index": 3, "good": [A], "noise": [D], "in_context": [B]})
    out = tmp_path / "out"
    convert.convert(root, out)
    golden = _golden(out)
    # A good -> 2; B in_context -> 0; D noise -> 0; C shown, unmarked -> 1
    assert _grades(golden) == {A: 2, B: 0, C: 1, D: 0}
    assert golden["context"]["inContextIds"] == [B]
    assert golden["context"]["labelsOffered"] is True
    assert convert.RELEVANCE["in_context"] == 0


def test_c6_in_context_ids_are_stored_sorted_and_an_unsurfaced_one_is_kept_at_grade_zero(convert, tmp_path):
    extra = nid("ob", 5999)  # never surfaced by any arm
    root = _session(tmp_path, {"index": 3, "in_context": [D, extra, B]})
    out = tmp_path / "out"
    convert.convert(root, out)
    golden = _golden(out)
    assert golden["context"]["inContextIds"] == sorted([D, extra, B])
    rows = {c["id"]: c for c in golden["ranked_candidates"]}
    assert rows[extra]["grading"]["relevance"] == 0 and rows[extra]["notes"] == "unsurfaced"
    assert rows[B]["grading"]["relevance"] == 0 and rows[D]["grading"]["relevance"] == 0


def test_c6_an_empty_in_context_key_still_means_the_label_was_offered(convert, tmp_path):
    root = _session(tmp_path, {"index": 3, "good": [A], "noise": [], "in_context": []})
    out = tmp_path / "out"
    convert.convert(root, out)
    assert _golden(out)["context"]["labelsOffered"] is True
    assert _golden(out)["context"]["inContextIds"] == []


def test_c6_an_in_context_id_rated_from_the_files_is_not_recorded_as_unshown(convert, tmp_path):
    # the request showed thinking_tail [A, B] and thinking_full [B]; C (tail) and D (full) were surfaced, never shown.
    # C is marked in_context (rated from the files: the rater saw it anyway) -> no "unshown" note, grade 0.
    # D is unmarked -> the control: it IS recorded as unshown, so the exemption is what keeps C clean.
    root = _session(tmp_path, {"index": 3, "in_context": [C]})
    _w(root / "requests" / f"{TURN}.json", {"turnId": TURN, "chars": 100, "omittedSteps": 0,
                                           "shown": {"3": {"A": [A, B], "B": [B]}}})
    out = tmp_path / "out"
    convert.convert(root, out)
    rows = {c["id"]: c for c in _golden(out)["ranked_candidates"]}
    assert rows[D]["notes"] == "unshown"
    assert rows[C].get("notes", "") == ""
    assert rows[C]["grading"]["relevance"] == 0


# the golden of a ratings step WITHOUT the key, as the converter wrote it before the label existed:
# copied from the output of the unmodified converter on this very fixture (ids built, never spelled).
def _old_golden_text() -> str:
    return json.dumps({
        "query_id": Q, "query_text": f"probe step {Q} (agent-rated)", "primary_category": "probe",
        "source": "probe-agent-rated",
        "context": {"turnId": TURN, "shown": {"A": [A, B, C], "B": [B, D]}},
        "ranked_candidates": [{"id": A, "grading": {"relevance": 2}}, {"id": B, "grading": {"relevance": 1}},
                              {"id": C, "grading": {"relevance": 1}}, {"id": D, "grading": {"relevance": 0}}]})


def test_c6_a_ratings_step_without_the_key_converts_to_the_old_golden_bytes(convert, tmp_path):
    root = _session(tmp_path, {"index": 3, "good": [A], "noise": [D]})
    out = tmp_path / "out"
    convert.convert(root, out)
    text = (out / "golden" / f"q_{Q}.json").read_text()
    assert text == _old_golden_text()
    ctx = json.loads(text)["context"]
    assert "labelsOffered" not in ctx and "inContextIds" not in ctx


@pytest.mark.parametrize("marks, lists", [
    ({"good": [A], "in_context": [A]}, ("good", "in_context")),
    ({"noise": [B], "in_context": [B]}, ("noise", "in_context")),
    ({"good": [C], "noise": [C]}, ("good", "noise")),
    ({"good": [D], "noise": [D], "in_context": [D]}, ("good", "noise")),  # all three: the first pair found names it
])
def test_c6_an_id_in_two_lists_dies_naming_turn_step_ids_and_lists_and_writes_nothing(convert, tmp_path, marks, lists):
    root = _session(tmp_path, {"index": 3, **marks})
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        convert.convert(root, out)
    msg = str(exc.value)
    shared = sorted(set.intersection(*(set(v) for v in marks.values())) if len(marks) > 1 else set())
    assert TURN in msg and "step 3" in msg and str(shared) in msg
    assert f"both {lists[0]} and {lists[1]}" in msg
    assert not out.exists() or not list(out.rglob("*.json"))  # validated before anything is written


# --------------------------------------------------------------------------- C7 probe_eval


def _g(gdir: Path, qid: str, rows: dict, *, offered: bool | None = None, in_context: list | None = None) -> None:
    obj = {"query_id": qid, "query_text": "g",
           "ranked_candidates": [{"id": i, "grading": {"relevance": rel}} for i, rel in rows.items()]}
    # every golden carries the real {turnId, shown} context; a pre-label one has nothing more
    obj["context"] = {"turnId": "t", "shown": None}
    if offered:
        obj["context"].update({"inContextIds": sorted(in_context or []), "labelsOffered": True})
    _w(gdir / f"q_{qid}.json", obj)


def _t(tdir: Path, name: str, qid: str, arm: str, ids: list, shown: list | None) -> None:
    _w(tdir / f"{name}.json", {
        "query_id": qid, "query_text": "q",
        "ranked_nodes": [{"id": i, "rank": r + 1, "grading": None, "signals": {}, "notes": ""} for r, i in enumerate(ids)],
        "metadata": {"arm": arm, "status": "ok", "ms": 1.0, "msOverStep": 0.001, "querySource": "thinking", "shownIds": shown}})


def test_c7_the_same_id_changes_p_at_5_when_it_is_in_context_instead_of_good(convert, evaluator, tmp_path):
    scores = {}
    for name, step in (("good", {"index": 3, "good": [A]}), ("ctx", {"index": 3, "in_context": [A]})):
        root = _session(tmp_path / name, step)
        out = tmp_path / name / "out"
        convert.convert(root, out)
        scores[name] = evaluator.evaluate(str(out / "golden"), str(out / "test"))["arms"]["thinking_tail"]["precision_at_k_5"]
        grade_a = _grades(json.loads((out / "golden" / f"q_{Q}.json").read_text()))[A]
        assert grade_a == (2 if name == "good" else 0)
    # thinking_tail ranks [A, B, C]; only A is rated. A good (grade 2): 1 relevant of 5 -> 1/5 = 0.2.
    # A in_context (grade 0): no grade >= 2 in the top 5 -> 0/5 = 0.0
    assert scores["good"] == pytest.approx(0.2)
    assert scores["ctx"] == pytest.approx(0.0)


def _share_fixture(tmp_path: Path):
    """arm_x over three steps of goldens, plus an arm that only ever has an old golden.

    s1 (offers): in_context {B, C}; arm_x ranking [A,B,C,D,E,F], shown [A,B,C,D] -> 2 in_context of 4 shown
        (all-candidates would read 2/6, so a share over every candidate is caught)
    s2 (offers): in_context {X};    arm_x ranking [X,Y,Z], shown [X,Y,Z]          -> 1 of 3
    s3 (OLD, no labelsOffered): arm_x ranking [A,B], shown [A,B]; B is graded 0 -> EXCLUDED (an old golden says nothing)
    pooled arm_x = (2 + 1) / (4 + 3) = 3/7 = 0.428571..., n = 7
    arm_old: ranks only the OLD step s3 -> no offering step -> share None (not 0), n 0
    arm_empty: ranks s4 (offers, in_context {A}) but shown [] -> zero denominator -> None, n 0
    """
    gdir, tdir = tmp_path / "golden", tmp_path / "test"
    _g(gdir, "s1", {A: 2, B: 0, C: 0, D: 1, E: 1, F: 1}, offered=True, in_context=[B, C])
    _g(gdir, "s2", {X: 2, Y: 1, Z: 1}, offered=True, in_context=[X])
    _g(gdir, "s3", {A: 2, B: 0})
    _g(gdir, "s4", {A: 0, B: 1}, offered=True, in_context=[A])
    _t(tdir, "x1", "s1", "arm_x", [A, B, C, D, E, F], [A, B, C, D])
    _t(tdir, "x2", "s2", "arm_x", [X, Y, Z], [X, Y, Z])
    _t(tdir, "x3", "s3", "arm_x", [A, B], [A, B])
    _t(tdir, "o3", "s3", "arm_old", [A, B], [A, B])
    _t(tdir, "e4", "s4", "arm_empty", [A, B], [])
    return gdir, tdir


def test_c7_in_context_share_is_over_shown_ids_pooled_over_offering_steps(evaluator, tmp_path):
    gdir, tdir = _share_fixture(tmp_path)
    arms = evaluator.evaluate(str(gdir), str(tdir))["arms"]
    assert arms["arm_x"]["rater_in_context_share"] == pytest.approx(3 / 7)
    assert arms["arm_x"]["rater_in_context_n"] == 7


def test_c7_the_share_also_reports_how_many_offering_steps_contributed(evaluator, tmp_path):
    gdir, tdir = _share_fixture(tmp_path)
    arms = evaluator.evaluate(str(gdir), str(tdir))["arms"]
    assert arms["arm_x"]["rater_in_context_steps"] == 2  # s1 and s2 offer; the old golden s3 does not count
    assert arms["arm_old"]["rater_in_context_steps"] == 0
    assert arms["arm_empty"]["rater_in_context_steps"] == 1  # it ranks an offering step, even if nothing was shown


def test_c7_an_old_golden_reads_none_never_zero_and_a_zero_denominator_reads_none(evaluator, tmp_path):
    gdir, tdir = _share_fixture(tmp_path)
    arms = evaluator.evaluate(str(gdir), str(tdir))["arms"]
    assert arms["arm_old"]["rater_in_context_share"] is None
    assert arms["arm_old"]["rater_in_context_n"] == 0
    assert arms["arm_empty"]["rater_in_context_share"] is None
    assert arms["arm_empty"]["rater_in_context_n"] == 0


def test_c7_the_report_prints_the_share_with_its_n_and_an_em_dash_for_none(evaluator, tmp_path):
    gdir, tdir = _share_fixture(tmp_path)
    text = evaluator.render_report(evaluator.evaluate(str(gdir), str(tdir)))
    lines = {ln.split("|")[1].strip(): [c.strip() for c in ln.split("|")[2:-1]] for ln in text.splitlines()
             if ln.startswith("| arm_") and ln.count("|") == 4}
    assert lines["arm_x"] == ["0.429", "7"]
    assert lines["arm_old"] == [DASH, "0"]
    assert lines["arm_empty"] == [DASH, "0"]


def test_c7_the_shown_only_mode_gives_the_same_share(evaluator, tmp_path):
    gdir, tdir = _share_fixture(tmp_path)
    shown = evaluator.evaluate(str(gdir), str(tdir), shown_only=True)["arms"]
    assert shown["arm_x"]["rater_in_context_share"] == pytest.approx(3 / 7)


def test_c7_with_no_offering_golden_the_report_has_no_share_section(evaluator, tmp_path):
    gdir, tdir = tmp_path / "golden", tmp_path / "test"
    _g(gdir, "s3", {A: 2, B: 0})
    _t(tdir, "x3", "s3", "arm_x", [A, B], [A, B])
    result = evaluator.evaluate(str(gdir), str(tdir))
    assert result["labels_offered"] is False
    assert "in_context share" not in evaluator.render_report(result)


# --------------------------------------------------------------------------- C8 fleet report


def _fleet_dir(root: Path, prefix: str, steps: int, offered: bool, in_context_by_step: list | None = None) -> Path:
    for i in range(1, steps + 1):
        qid = f"{prefix}{i}"
        ctx = (in_context_by_step or [[]] * steps)[i - 1]
        _g(root / "golden", qid, {A: 2, B: 1, C: 1}, offered=offered, in_context=ctx)
        _t(root / "test", f"{qid}-x", qid, "arm_x", [A, B, C], [A, B, C])
        if offered:
            _t(root / "test", f"{qid}-y", qid, "arm_y", [A], [A])
    _w(root / "summary.json", {"steps_rated": steps, "steps_skipped_by_reason": {}})
    return root


def _run(fleet, tmp_path: Path, pairs) -> str:
    out = tmp_path / "fleet.md"
    argv = [str(d) for d, _ in pairs]
    for _, label in pairs:
        argv += ["--label", label]
    assert fleet.main(argv + ["--out", str(out)]) == 0
    return out.read_text()


HEADING = "in_context share per arm per seat:date (cell = share (n shown ids, n steps))"


def test_c8_the_share_table_appears_when_an_input_offers_the_label(fleet, tmp_path):
    # L (offers): 4 steps, arm_x shown [A,B,C] each step = 12 shown ids; in_context per step {B},{B,C},{},{} -> 1+2+0+0 = 3
    #     arm_x share = 3/12 = .250 (12 ids over 4 steps: parentheses, the gate counts steps); arm_y shows [A] each step = 4 ids, none in_context -> 0/4 = .000 (n 4 < 10 -> parentheses)
    # O (old): 2 steps, no labelsOffered -> every cell is the em dash
    lab = _fleet_dir(tmp_path / "L", "l", 4, True, [[B], [B, C], [], []])
    old = _fleet_dir(tmp_path / "O", "o", 2, False)
    text = _run(fleet, tmp_path, [(lab, "kepler:2026-10-06"), (old, "borges:2026-10-07")])
    assert HEADING in text
    section = text[text.index(HEADING):]
    rows = {ln.split("|")[1].strip(): [c.strip() for c in ln.split("|")[2:-1]] for ln in section.splitlines()
            if ln.startswith("| arm_")}
    assert rows["arm_x"] == ["(.250) (12 ids, 4 steps)", DASH]  # 12 ids but 4 steps < 10: thin evidence
    assert rows["arm_y"] == ["(.000) (4 ids, 4 steps)", "-"]  # the old input has no arm_y at all: the usual '-', not an offered-label dash
    assert text.index(HEADING) > text.index("## 5. Caveats")


def test_c8_without_an_offering_input_the_report_has_no_share_table_at_all(fleet, tmp_path):
    old1 = _fleet_dir(tmp_path / "O1", "p", 2, False)
    old2 = _fleet_dir(tmp_path / "O2", "q", 3, False)
    text = _run(fleet, tmp_path, [(old1, "kepler:2026-10-06"), (old2, "borges:2026-10-07")])
    assert "in_context share" not in text


def _thin_dir(root: Path, steps: int, ids_per_step: int) -> Path:
    """arm_x over `steps` offering steps, each showing `ids_per_step` ids, the first in_context every step."""
    ids = [nid("ob", 5200 + i) for i in range(ids_per_step)]
    for i in range(1, steps + 1):
        qid = f"u{i}"
        _g(root / "golden", qid, {k: 1 for k in ids}, offered=True, in_context=ids[:1])
        _t(root / "test", f"{qid}-x", qid, "arm_x", ids, ids)
    _w(root / "summary.json", {"steps_rated": steps, "steps_skipped_by_reason": {}})
    return root


def _share_cell(text: str) -> str:
    section = text[text.index(HEADING):]
    rows = {ln.split("|")[1].strip(): [c.strip() for c in ln.split("|")[2:-1]] for ln in section.splitlines()
            if ln.startswith("| arm_")}
    return rows["arm_x"][0]


def test_c8_twelve_shown_ids_over_two_steps_is_parenthesised(fleet, tmp_path):
    thin = _thin_dir(tmp_path / "T", 2, 6)  # 12 shown ids >= 10, but only 2 steps
    assert _share_cell(_run(fleet, tmp_path, [(thin, "kepler:2026-10-06")])) == "(.167) (12 ids, 2 steps)"


def test_c8_the_same_twelve_ids_over_ten_or_more_steps_is_plain(fleet, tmp_path):
    wide = _thin_dir(tmp_path / "W", 12, 1)  # 12 shown ids over 12 steps
    assert _share_cell(_run(fleet, tmp_path, [(wide, "kepler:2026-10-06")])) == "1.000 (12 ids, 12 steps)"
