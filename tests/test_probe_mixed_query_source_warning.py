"""Contract tests for the mixed-querySource WARNING in probe_eval and probe_fleet_report.

Contract-first: written before the warning existed. The report tools print ONE row per arm name
and `querySource` only as a count column, so a pooled score of a proxy arm can silently mix two
query materials (old rows: `thinking_full` keyed on narration on some seats, on thinking on
others). New data cannot mix by construction; the warning is for old inputs and for a regression.

Contract:
  W1  PROXY ARMS ONLY: thinking_full, thinking_tail, extract_model, novel_terms, narration_full.
      When such an arm's units carry MORE THAN ONE distinct non-null querySource, ONE line:
        WARNING: arm <arm> mixes querySource <s1> (<n1>) and <s2> (<n2>): its scores pool different
        query materials; group by querySource before comparing.
      sources in alphabetical order; three or more are joined with ", " and a final " and ".
      NEVER tool_input, never a route arm, never another arm name.
  W2  a unit whose querySource is None or absent is ignored; one source or zero units: no warning.
  W3  probe_eval: right after the per-arm table; fleet report: directly under section 2, per input
      first (in input order, each line led by `[seat:date] `), then pooled (led by `[pooled] `).
      Without a mixed proxy arm the report bytes are those of the tool without the warning.

Node-id-shaped ids are BUILT by `nid`, never spelled.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO_ROOT / "tools" / "probes" / "mods-recall"
PROXY_ARMS = ["thinking_full", "thinking_tail", "extract_model", "novel_terms", "narration_full"]


def nid(prefix: str, n: int) -> str:
    return f"{prefix}_{n}"


A, B = nid("ob", 6001), nid("ob", 6002)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"probe_warn_{name}", PROBE_DIR / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def evaluator():
    return _load("probe_eval")


@pytest.fixture()
def fleet():
    return _load("probe_fleet_report")


def _w(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def expected(arm: str, counts: dict) -> str:
    """The warning line, spelled from the contract (independent of the tool's own helper)."""
    parts = [f"{src} ({counts[src]})" for src in sorted(counts)]
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return (f"WARNING: arm {arm} mixes querySource {joined}: its scores pool different query "
            "materials; group by querySource before comparing.")


def build(root: Path, units: list, *, absent_key: bool = False) -> Path:
    """units: (arm, querySource-or-None) per test unit; one golden per unit, one test file per unit."""
    for i, (arm, src) in enumerate(units):
        qid = f"u{i:03d}"
        _w(root / "golden" / f"q_{qid}.json", {
            "query_id": qid, "query_text": "g",
            "ranked_candidates": [{"id": A, "grading": {"relevance": 2}}, {"id": B, "grading": {"relevance": 1}}]})
        meta = {"arm": arm, "status": "ok", "ms": 1.0, "msOverStep": 0.001, "shownIds": None}
        if not (absent_key and src is None):
            meta["querySource"] = src
        _w(root / "test" / f"{qid}.json", {
            "query_id": qid, "query_text": "q",
            "ranked_nodes": [{"id": A, "rank": 1, "grading": None, "signals": {}, "notes": ""}],
            "metadata": meta})
    _w(root / "summary.json", {"steps_rated": len(units), "steps_skipped_by_reason": {}})
    return root


def units_of(arm: str, **counts) -> list:
    out = []
    for src, n in counts.items():
        out += [(arm, None if src == "none" else src)] * n
    return out


def report(ev, root: Path) -> str:
    return ev.render_report(ev.evaluate(str(root / "golden"), str(root / "test")))


def warns(text: str) -> list:
    return [ln for ln in text.splitlines() if "WARNING:" in ln]


# ------------------------------------------------------------------ X1 no mix

@pytest.mark.parametrize("arm", PROXY_ARMS)
def test_x1_one_source_per_proxy_arm_prints_no_warning(evaluator, tmp_path, arm):
    text = report(evaluator, build(tmp_path, units_of(arm, visible=3)))
    assert warns(text) == []
    assert "WARNING" not in text


def test_x1_zero_units_and_other_arms_print_no_warning(evaluator, tmp_path):
    text = report(evaluator, build(tmp_path, units_of("arm_x", thinking=2, visible=2)
                                   + units_of("route:prompt_surface", none=2)))
    assert "WARNING" not in text  # an arm name outside the proxy list never warns, mixed or not


# ------------------------------------------------------------------ X2 mix

@pytest.mark.parametrize("arm", PROXY_ARMS)
def test_x2_two_sources_print_exactly_one_exact_line_with_counts(evaluator, tmp_path, arm):
    text = report(evaluator, build(tmp_path, units_of(arm, visible=3, thinking=2)))
    assert warns(text) == [expected(arm, {"visible": 3, "thinking": 2})]
    # sources are listed alphabetically: thinking before visible
    assert "mixes querySource thinking (2) and visible (3):" in text


@pytest.mark.parametrize("arm", PROXY_ARMS)
def test_x2_three_sources_use_the_three_way_join(evaluator, tmp_path, arm):
    text = report(evaluator, build(tmp_path, units_of(arm, visible=3, thinking=2, zzz=1)))
    assert warns(text) == [expected(arm, {"visible": 3, "thinking": 2, "zzz": 1})]
    assert "mixes querySource thinking (2), visible (3) and zzz (1):" in text


def test_x2_each_mixed_proxy_arm_gets_its_own_line_in_arm_order(evaluator, tmp_path):
    text = report(evaluator, build(tmp_path, units_of("thinking_tail", visible=1, thinking=1)
                                   + units_of("thinking_full", visible=2, thinking=1)))
    assert warns(text) == [expected("thinking_full", {"visible": 2, "thinking": 1}),
                           expected("thinking_tail", {"visible": 1, "thinking": 1})]


def test_x2_the_warning_sits_right_after_the_per_arm_table(evaluator, tmp_path):
    text = report(evaluator, build(tmp_path, units_of("thinking_full", visible=1, thinking=1)))
    lines = text.splitlines()
    w = next(i for i, ln in enumerate(lines) if "WARNING:" in ln)
    assert lines[w - 1] == "" and lines[w - 2].startswith("| thinking_full |")
    assert lines[w + 1] == "" and lines[w + 2].startswith("Latency excluded")


# ------------------------------------------------------------------ X3 tool_input never

def test_x3_tool_input_with_mixed_sources_prints_no_warning(evaluator, tmp_path):
    text = report(evaluator, build(tmp_path, units_of("tool_input", visible=3, thinking=2)))
    assert "WARNING" not in text
    assert "thinking: 2, visible: 3" in text  # the count column still shows the split


# ------------------------------------------------------------------ X4 None / absent ignored

def test_x4_none_units_are_ignored_one_real_source_means_no_warning(evaluator, tmp_path):
    text = report(evaluator, build(tmp_path, units_of("thinking_full", visible=5, none=3)))
    assert "WARNING" not in text


def test_x4_an_absent_key_is_ignored_like_none(evaluator, tmp_path):
    text = report(evaluator, build(tmp_path, units_of("thinking_full", visible=5, none=3), absent_key=True))
    assert "WARNING" not in text


def test_x4_none_never_appears_as_a_source_in_a_real_mix(evaluator, tmp_path):
    text = report(evaluator, build(tmp_path, units_of("thinking_full", visible=2, thinking=1, none=4)))
    assert warns(text) == [expected("thinking_full", {"visible": 2, "thinking": 1})]


# ------------------------------------------------------------------ W6-style byte identity

def test_x1_the_report_without_a_mixed_proxy_arm_has_no_added_line(evaluator, tmp_path):
    # the same input with the warning lines removed is the report itself when nothing is mixed
    text = report(evaluator, build(tmp_path, units_of("thinking_full", visible=3) + units_of("tool_input", thinking=1, visible=1)))
    assert [ln for ln in text.splitlines() if "WARNING" in ln] == []
    assert "\n\n\n" not in text  # no stray blank line where a warning block would sit


# ------------------------------------------------------------------ X5 fleet

def _run(fleet, tmp_path: Path, pairs) -> str:
    out = tmp_path / "fleet.md"
    argv = [str(d) for d, _ in pairs]
    for _, label in pairs:
        argv += ["--label", label]
    assert fleet.main(argv + ["--out", str(out)]) == 0
    return out.read_text()


def _section2(text: str) -> list:
    body = text[text.index("## 2. querySource split"):text.index("## 3. Signal sweep")]
    return body.splitlines()


def test_x5_inputs_that_disagree_give_no_per_input_warning_and_one_pooled_warning(fleet, tmp_path):
    a = build(tmp_path / "A", units_of("thinking_full", visible=4))
    b = build(tmp_path / "B", units_of("thinking_full", thinking=3))
    text = _run(fleet, tmp_path, [(a, "seata:2026-10-06"), (b, "seatb:2026-10-07")])
    assert warns(text) == ["[pooled] " + expected("thinking_full", {"visible": 4, "thinking": 3})]
    sec = _section2(text)
    w = next(i for i, ln in enumerate(sec) if "WARNING:" in ln)
    assert sec[w - 1] == "" and sec[w - 2].startswith("| thinking_full |")  # directly under the table


def test_x5_a_mixed_input_warns_per_input_first_in_input_order_then_pooled(fleet, tmp_path):
    a = build(tmp_path / "A", units_of("thinking_full", visible=2, thinking=1))
    b = build(tmp_path / "B", units_of("thinking_full", visible=1, thinking=3) + units_of("narration_full", visible=1, thinking=1))
    text = _run(fleet, tmp_path, [(b, "seatb:2026-10-07"), (a, "seata:2026-10-06")])
    # inputs are ordered by date: seata (A) first, seatb (B) second
    assert warns(text) == [
        "[seata:2026-10-06] " + expected("thinking_full", {"visible": 2, "thinking": 1}),
        "[seatb:2026-10-07] " + expected("narration_full", {"visible": 1, "thinking": 1}),
        "[seatb:2026-10-07] " + expected("thinking_full", {"visible": 1, "thinking": 3}),
        "[pooled] " + expected("narration_full", {"visible": 1, "thinking": 1}),
        "[pooled] " + expected("thinking_full", {"visible": 3, "thinking": 4}),
    ]


def test_x5_no_mixed_proxy_arm_means_no_warning_and_tool_input_never_warns(fleet, tmp_path):
    a = build(tmp_path / "A", units_of("thinking_full", visible=2) + units_of("tool_input", visible=1, thinking=1))
    b = build(tmp_path / "B", units_of("thinking_full", visible=1) + units_of("tool_input", visible=1, thinking=2))
    text = _run(fleet, tmp_path, [(a, "seata:2026-10-06"), (b, "seatb:2026-10-07")])
    assert "WARNING" not in text


# ------------------------------------------------------------------ X6 the two tools agree

def test_x6_probe_eval_and_the_fleet_report_agree_on_the_counts(evaluator, fleet, tmp_path):
    a = build(tmp_path / "A", units_of("thinking_full", visible=2, thinking=1) + units_of("narration_full", visible=3))
    b = build(tmp_path / "B", units_of("thinking_full", visible=1, thinking=3))
    text = _run(fleet, tmp_path, [(a, "seata:2026-10-06"), (b, "seatb:2026-10-07")])
    got = {}
    for root, label in ((a, "seata:2026-10-06"), (b, "seatb:2026-10-07")):
        eval_lines = warns(report(evaluator, root))
        fleet_lines = [ln[len(f"[{label}] "):] for ln in warns(text) if ln.startswith(f"[{label}] ")]
        assert eval_lines == fleet_lines  # the same line, byte for byte, from both tools
        got[label] = eval_lines
    assert got["seata:2026-10-06"] == [expected("thinking_full", {"visible": 2, "thinking": 1})]
    assert got["seatb:2026-10-07"] == [expected("thinking_full", {"visible": 1, "thinking": 3})]
    # the pooled counts are the sums of the per-input counts
    assert [ln for ln in warns(text) if ln.startswith("[pooled] ")] == [
        "[pooled] " + expected("thinking_full", {"visible": 3, "thinking": 4})]
