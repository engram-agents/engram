"""Contract tests for the probe's `narration_full` arm and the per-arm `source` (Python side).

Contract-first: these assertions were written BEFORE convert_ratings.py and
probe_eval.py knew the arm. The mod half of the same contract (what each arm
reads, the skips, the model boundary) lives in
tools/probes/mods-recall/hooks/step.test.ts and rating.test.ts.

Contract pinned here:
  B10a an arm result carrying `source` sets the unit's metadata.querySource to it,
       whatever the step's own querySource says
  B10b the same fixture WITHOUT `source` keeps the step's value (old step files)
  B10c a step file with no `source` anywhere converts to the same units as before
       (the whole-dir byte diff against the prep tip is reported in the PR handoff;
       this cell pins the per-unit part)
  B11  `narration_full` is a known arm of the converter and of probe_eval, and the
       fleet report lists it among the arm rows in its documented order (arm name)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO_ROOT / "tools" / "probes" / "mods-recall"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_probe_mods_recall as base  # noqa: E402  (the shared fixture builders)


def _nid(n: int) -> str:
    """Node ids are built at run time, never written as literals."""
    return "ob" + "_" + f"{n:04d}"


def _load(name: str):
    path = PROBE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"probe_narr_{name}", path)
    assert spec and spec.loader
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


PILOT_BLIND = {"A": "thinking_full", "B": "narration_full", "C": "tool_input"}


def _pilot_session(tmp_path: Path, *, sources: dict | None, step_source: str = "visible") -> Path:
    """One rated step with the three pilot arms; `sources` = arm -> source recorded on the arm result."""
    root = base._session(tmp_path)
    step = base._step(
        3,
        {"thinking_full": [_nid(1)], "narration_full": [_nid(2)], "tool_input": [_nid(3)]},
        blind=PILOT_BLIND,
        querySource=step_source,
        visibleChars=50,
    )
    for arm, source in (sources or {}).items():
        step["arms"][arm]["source"] = source
    base._put_step(root, step)
    base._put_rating(root, [{"index": 3, "good": [_nid(1), _nid(2)]}])
    return root


def _meta(out: Path, arm: str) -> dict:
    return json.loads((out / "test" / f"{base.TURN}-3-{arm}.json").read_text())["metadata"]


def test_b10a_arm_source_wins_over_the_step_query_source(convert, tmp_path):
    out = tmp_path / "out"
    root = _pilot_session(tmp_path, sources={"thinking_full": "thinking", "narration_full": "visible"})
    convert.convert(root, out)
    assert _meta(out, "thinking_full")["querySource"] == "thinking"  # step says "visible"
    assert _meta(out, "narration_full")["querySource"] == "visible"
    assert _meta(out, "narration_full")["arm"] == "narration_full"
    # the step is "thinking" but the arm says it read the visible text
    out2 = tmp_path / "out2"
    root2 = _pilot_session(tmp_path / "second", sources={"narration_full": "visible"}, step_source="thinking")
    convert.convert(root2, out2)
    assert _meta(out2, "narration_full")["querySource"] == "visible"


def test_b10b_without_arm_source_the_step_value_is_kept(convert, tmp_path):
    out = tmp_path / "out"
    convert.convert(_pilot_session(tmp_path, sources=None), out)
    for arm in ("thinking_full", "narration_full", "tool_input"):
        assert _meta(out, arm)["querySource"] == "visible"


def test_b10b2_tool_input_has_no_source_and_falls_back_to_the_step(convert, tmp_path):
    out = tmp_path / "out"
    root = _pilot_session(tmp_path, sources={"thinking_full": "thinking", "narration_full": "visible"}, step_source="thinking")
    convert.convert(root, out)
    assert _meta(out, "tool_input")["querySource"] == "thinking"


def test_b10c_legacy_arms_without_source_convert_as_before(convert, tmp_path):
    root = base._source_session(tmp_path)
    out = tmp_path / "out"
    convert.convert(root, out)
    a = json.loads((out / "test" / f"{base.TURN}-3-thinking_tail.json").read_text())["metadata"]
    b = json.loads((out / "test" / f"{base.TURN}-4-thinking_full.json").read_text())["metadata"]
    assert (a["querySource"], b["querySource"]) == ("thinking", "visible")


def test_b11_narration_full_is_a_known_arm_everywhere(convert, evaluator):
    assert "narration_full" in convert.KNOWN_ARMS
    assert "narration_full" in evaluator._KNOWN_ARMS


def test_b11_eval_scores_a_narration_full_unit(convert, evaluator, tmp_path):
    out = tmp_path / "out"
    convert.convert(_pilot_session(tmp_path, sources={"narration_full": "visible"}), out)
    result = evaluator.evaluate(str(out / "golden"), str(out / "test"), None)
    assert set(result["arms"]) == {"thinking_full", "narration_full", "tool_input"}
    assert result["arms"]["narration_full"]["by_query_source"] == {"visible": 1}


def _w(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def _unit(qid: str, arm: str, ids: list[str], source: str) -> dict:
    nodes = [{"id": i, "rank": r + 1, "grading": None, "signals": {}, "notes": ""} for r, i in enumerate(ids)]
    return {"query_id": qid, "query_text": "q", "ranked_nodes": nodes,
            "metadata": {"arm": arm, "status": "ok", "ms": 10.0, "msOverStep": None,
                         "querySource": source, "shownIds": None}}


def test_b11_fleet_report_lists_narration_full_in_arm_name_order(fleet, tmp_path):
    root = tmp_path / "A"
    _w(root / "golden" / "q_s1.json", {"query_id": "s1", "query_text": "g",
                                       "ranked_candidates": [{"id": _nid(1), "grading": {"relevance": 2}}]})
    for arm, source in (("thinking_full", "thinking"), ("narration_full", "visible"), ("tool_input", "visible")):
        _w(root / "test" / f"s1-{arm}.json", _unit("s1", arm, [_nid(1)], source))
    (root / "pairs.jsonl").write_text("")
    _w(root / "summary.json", {"steps_rated": 1, "steps_skipped_by_reason": {}})
    out = tmp_path / "report.md"
    assert fleet.main([str(root), "--label", "seat:2026-10-31", "--out", str(out)]) == 0
    text = out.read_text()
    rows = [line.split("|")[1].strip() for line in text.split("### p@5")[1].split("### nDCG@10")[0].splitlines()
            if line.startswith("| ") and not line.startswith("| arm") and not line.startswith("|---")]
    assert rows == ["narration_full", "thinking_full", "tool_input"]
    # the querySource split shows the visible narration unit under "visible"
    assert "visible" in text.split("## 2.")[1].split("## 3.")[0]


LEGACY_ARMS = ["thinking_full", "thinking_tail", "extract_model", "tool_input", "novel_terms"]


def test_plugin_json_arms_default_is_the_five_legacy_arms():
    """The pilot is config only: the manifest default (the value the engine fills in)
    stays the five legacy arms, in this order. hooks/step.test.ts B1b pins
    DEFAULTS.arms to the same list, so the code default and the manifest cannot drift."""
    manifest = json.loads((PROBE_DIR / ".claude-plugin" / "plugin.json").read_text())
    default = manifest["userConfig"]["arms"]["default"]
    assert default.split(",") == LEGACY_ARMS
