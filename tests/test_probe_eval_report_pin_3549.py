"""Byte-identity pin for probe_eval's reports (engram-alpha#3549, D1).

The fleet report re-uses probe_eval by import and the issue forbids any change to
the existing report. The expected strings below were computed from probe_eval.py
BEFORE any #3549 work (probe_eval.py is not modified by #3549 at all) and pasted
in as literals. The fixture is a synthetic converter output dir generated from
literals here.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROBE_DIR = Path(__file__).resolve().parent.parent / "tools" / "probes" / "mods-recall"


@pytest.fixture()
def evaluator():
    spec = importlib.util.spec_from_file_location("probe_eval_pin3549", PROBE_DIR / "probe_eval.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _w(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _rru(qid, arm, ids, ms=None, status="ok", source="thinking", shown=None, sims=None):
    return {
        "query_id": qid, "query_text": "q",
        "ranked_nodes": [{"id": i, "rank": r + 1, "grading": None,
                          "signals": ({"similarity": sims[r]} if sims else {"inContext": r == 0}),
                          "notes": ""} for r, i in enumerate(ids)],
        "metadata": {"arm": arm, "status": status, "ms": ms,
                     "msOverStep": (ms / 1000.0) if ms is not None else None,
                     "querySource": source, "shownIds": shown},
    }


def _build(root: Path) -> Path:
    for qid in ("t1-s1", "t1-s2"):
        _w(root / "golden" / f"q_{qid}.json", {
            "query_id": qid, "query_text": "g",
            "ranked_candidates": [
                {"id": "ob_0001", "grading": {"relevance": 2}},
                {"id": "ob_0002", "grading": {"relevance": 2}},
                {"id": "ob_0003", "grading": {"relevance": 1}, "notes": "unshown"},
                {"id": "ob_0004", "grading": {"relevance": 0}},
            ]})
    t = root / "test"
    _w(t / "a.json", _rru("t1-s1", "arm_x", ["ob_0001", "ob_0002", "ob_0003", "ob_0004"], ms=120.0))
    _w(t / "b.json", _rru("t1-s2", "arm_x", ["ob_0001", "ob_0004"], ms=80.0, source="visible"))
    _w(t / "c.json", _rru("t1-s1", "arm_y", ["ob_0004", "ob_0003", "ob_0002", "ob_0001"], ms=300.0,
                          shown=["ob_0004", "ob_0002"]))
    _w(t / "d.json", _rru("t1-s2", "arm_y", ["ob_0003"], ms=None, source="visible", shown=["ob_0003"]))
    _w(t / "e.json", _rru("t1-s2", "arm_z", ["ob_0001"], ms=50.0, status="error"))
    _w(t / "f.json", _rru("t1-s1", "route:prompt_surface", ["ob_0001", "ob_0002", "ob_0003"],
                          sims=[0.9, 0.5, None]))
    _w(t / "g.json", _rru("t1-s9", "arm_x", ["ob_0001"], ms=10.0))  # no golden: unrated
    (root / "pairs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"armA": "arm_x", "armB": "arm_y", "verdict": "better"},
        {"armA": "arm_x", "armB": "arm_y", "verdict": "worse"},
    ]))
    _w(root / "summary.json", {"steps_skipped_by_reason": {"terminal": 2, "short": 1}})
    return root


FULL = "# Probe eval\n\nmode: full (every surfaced id scored; unshown ids count as neutral)\n\ngoldens: 2 · test RRUs: 7 · unrated steps (not scored): 1\nsteps skipped by reason: terminal 2 · short 1\n\n## Per arm\n\nin-context flag: ledger/hook value\n\n| arm | n steps | p@5 | r@10 | nDCG@10 | MRR | median ms | p90 ms | median msOverStep | unshown | steps by querySource | inContext (top-5) | principle (top-5) |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|\n| arm_x | 2 | 0.300 | 0.750 | 0.766 | 1.000 | 100 | 120 | 0.100 | 0.167 | thinking: 1, visible: 1 | 0.333 | 0.000 |\n| arm_y | 2 | 0.200 | 0.500 | 0.464 | 0.167 | 300 | 300 | 0.300 | 0.400 | thinking: 1, visible: 1 | 0.400 | 0.000 |\n| arm_z | 0 | - | - | - | - | - | - | - | - | - | - | - |\n| route:prompt_surface | 1 | 0.400 | 1.000 | 1.000 | 1.000 | - | - | - | 0.333 | thinking: 1 | - | 0.000 |\n\nLatency excluded (value absent): arm_x: ms 0, msOverStep 0, arm_y: ms 1, msOverStep 1, arm_z: ms 0, msOverStep 0, route:prompt_surface: ms 1, msOverStep 1\n\nArm runs excluded (skipped or errored): arm_z: 1\n\n## Pair verdicts (armA vs armB, from armA's side)\n\n| armA | armB | better | same | worse |\n|---|---|---|---|---|\n| arm_x | arm_y | 1 | 0 | 1 |\n\n## Arm misses (golden relevance-2 ids no arm surfaced)\n\n- t1-s2: ob_0002\n\n## Unrated steps (no golden; not scored)\n\n- t1-s9\n"
SHOWN = "# Probe eval\n\nmode: shown-only (each ranking truncated to the ids the request showed)\n\ngoldens: 2 · test RRUs: 7 · unrated steps (not scored): 1\nsteps skipped by reason: terminal 2 · short 1\n\n## Per arm\n\nin-context flag: ledger/hook value\n\n| arm | n steps | p@5 | r@10 | nDCG@10 | MRR | median ms | p90 ms | median msOverStep | unshown | steps by querySource | inContext (top-5) | principle (top-5) |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|\n| arm_x | 2 | 0.300 | 0.750 | 0.766 | 1.000 | 100 | 120 | 0.100 | 0.167 | thinking: 1, visible: 1 | 0.333 | 0.000 |\n| arm_y | 2 | 0.100 | 0.250 | 0.301 | 0.250 | 300 | 300 | 0.300 | 0.333 | thinking: 1, visible: 1 | 0.667 | 0.000 |\n| arm_z | 0 | - | - | - | - | - | - | - | - | - | - | - |\n| route:prompt_surface | 1 | 0.400 | 1.000 | 1.000 | 1.000 | - | - | - | 0.333 | thinking: 1 | - | 0.000 |\n\nLatency excluded (value absent): arm_x: ms 0, msOverStep 0, arm_y: ms 1, msOverStep 1, arm_z: ms 0, msOverStep 0, route:prompt_surface: ms 1, msOverStep 1\n\nArm runs excluded (skipped or errored): arm_z: 1\n\n## Pair verdicts (armA vs armB, from armA's side)\n\n| armA | armB | better | same | worse |\n|---|---|---|---|---|\n| arm_x | arm_y | 1 | 0 | 1 |\n\n## Arm misses (golden relevance-2 ids no arm surfaced)\n\n- t1-s2: ob_0002\n\n## Unrated steps (no golden; not scored)\n\n- t1-s9\n"
SWEEP = "# Probe signal sweep\n\nEach route's recorded candidates re-ranked offline; p@5 against the same goldens (unrated candidates count as not relevant). A signal null on a candidate is 0 for it and counted in null_signal_rows.\n\n## route:prompt_surface\n\n| setting | n steps | p@5 | null_signal_rows |\n|---|---|---|---|\n| as recorded | 1 | 0.400 | 0 |\n| floor 0.30 | 1 | 0.400 | 1 |\n| floor 0.35 | 1 | 0.400 | 1 |\n| floor 0.40 | 1 | 0.400 | 1 |\n| floor 0.45 | 1 | 0.400 | 1 |\n| floor 0.50 | 1 | 0.400 | 1 |\n| floor 0.55 | 1 | 0.200 | 1 |\n| floor 0.60 | 1 | 0.200 | 1 |\n| floor 0.65 | 1 | 0.200 | 1 |\n| floor 0.70 | 1 | 0.200 | 1 |\n\n"


def _render(ev, root, shown_only):
    return ev.render_report(ev.evaluate(str(root / "golden"), str(root / "test"),
                                        str(root / "pairs.jsonl"), shown_only=shown_only))


def test_full_report_is_byte_identical(evaluator, tmp_path):
    assert _render(evaluator, _build(tmp_path), False) == FULL


def test_shown_only_report_is_byte_identical(evaluator, tmp_path):
    assert _render(evaluator, _build(tmp_path), True) == SHOWN


def test_signal_sweep_report_is_byte_identical(evaluator, tmp_path):
    root = _build(tmp_path)
    got = evaluator.render_sweep(evaluator.signal_sweep(str(root / "golden"), str(root / "test")))
    assert got == SWEEP
