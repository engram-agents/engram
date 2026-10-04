"""Contract tests for tools/probes/mods-recall — the converter and the eval.

Contract-first (CLAUDE.md "Contract-first tests", spec part of #3362): these
assertions were written BEFORE convert_ratings.py / probe_eval.py existed.

All fixtures are hand-built step / rating JSON in ``tmp_path``; nothing under
``~/.engram`` is read or written. Node ids like ``ob_1234`` are fine here
(tests/ is repo-only); they never appear in tools/probes/** source.

Contracts pinned:
  1. one TEST RRU per (step, arm); summary counts match; an extra rating for
     a missing step shows up as ratings_without_step == 1
  2. an unrated step -> no golden, steps_unrated == 1, exit 0
  3. relevance mapping exact: good->2, noise->0, surfaced-unmarked->1;
     unsurfaced-good -> present, relevance 2, notes == "unsurfaced"
  4. an unknown blind letter -> SystemExit naming turnId, index, letter
  5. a malformed step file -> SystemExit naming the file; nothing written
  6. probe_eval: golden-aligned arm nDCG@10 == 1.0, reversed arm strictly lower;
     pair table counts match
  7. latency: msOverStep None when stepMs missing; medians/p90 over present
     values only; the report prints the excluded count
  8. control: swapping noise->1 makes the mapping assertion (test 3) fail
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = REPO_ROOT / "tools" / "probes" / "mods-recall"


def _load(name: str):
    """Import a script from the hyphenated plugin folder by file path."""
    path = PROBE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"probe_{name}", path)
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


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------

TURN = "aaaaaaaa-1111-2222-3333-444444444444"
TURN8 = "aaaaaaaa"
Q8 = "t" + TURN8  # query ids / golden names lead with a constant "t"
BLIND = {"A": "thinking_tail", "B": "thinking_full"}


def _cand(node_id: str, rank: int, **extra) -> dict:
    row = {
        "id": node_id,
        "type": "observation",
        "claim": f"claim of {node_id}",
        "rank": rank,
        "composite_score": 1.0 - rank / 100,
        "similarity": 0.5,
        "match_type": "semantic",
        "armsHit": ["thinking_tail"],
        "inContext": False,
        "principle": False,
    }
    row.update(extra)
    return row


def _arm(name: str, ids: list[str], *, ms: float = 100.0, step_ms=1000.0, **extra) -> dict:
    row = {
        "arm": name,
        "query": f"query for {name}",
        "candidates": [_cand(i, r + 1) for r, i in enumerate(ids)],
        "ms": ms,
        "msOverStep": (ms / step_ms) if step_ms else None,
    }
    row.update(extra)
    return row


def _step(index: int, arms: dict[str, list[str]], *, turn: str = TURN,
          step_ms=1000.0, blind: dict | None = None, **extra) -> dict:
    row = {
        "turnId": turn,
        "index": index,
        "model": "test-model",
        "stopReason": "tool_use",
        "stepMs": step_ms,
        "messagesScanned": 3,
        "engram_server": "engram",
        "armBlind": blind if blind is not None else BLIND,
        "arms": {name: _arm(name, ids, step_ms=step_ms) for name, ids in arms.items()},
        "ts": 1,
    }
    row.update(extra)
    return row


def _write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


def _session(tmp_path: Path) -> Path:
    root = tmp_path / "session"
    (root / "steps").mkdir(parents=True)
    return root


def _put_step(root: Path, step: dict) -> Path:
    return _write(root / "steps" / f"{step['turnId']}-{step['index']:03d}.json", step)


def _put_request(root: Path, turn: str = TURN, shown: dict | None = None) -> Path:
    """The request file the mod writes at turn end; by default it shows every
    candidate id of every step file of that turn (so nothing is "unshown")."""
    if shown is None:
        shown = {}
        for path in sorted((root / "steps").glob("*.json")):
            try:
                step = json.loads(path.read_text())
            except ValueError:
                continue
            if step.get("turnId") != turn or not step.get("arms"):
                continue
            blind = step.get("armBlind") or {}
            shown[str(step["index"])] = {
                letter: [c["id"] for c in step["arms"].get(arm, {}).get("candidates", [])]
                for letter, arm in blind.items()
            }
    return _write(root / "requests" / f"{turn}.json",
                  {"turnId": turn, "shown": shown, "chars": 100, "omittedSteps": 0})


def _put_rating(root: Path, steps: list[dict], *, turn: str = TURN, ts: int = 10,
                request: bool = True) -> Path:
    if request and not (root / "requests" / f"{turn}.json").exists():
        _put_request(root, turn)
    return _write(
        root / "ratings" / f"{turn}-{ts}.json",
        {"ts": ts, "sessionId": "s", "turnId": turn, "steps": steps, "ratedBy": "test-model"},
    )


def _standard_session(tmp_path: Path) -> Path:
    """Two steps with arms (2 arms each), step 3 rated, step 4 unrated, plus
    one rating for a step (9) that has no step file at all."""
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001", "ob_0002", "ob_0003"],
                              "thinking_full": ["ob_0002", "ob_0004"]}))
    _put_step(root, _step(4, {"thinking_tail": ["ob_0005"], "thinking_full": ["ob_0006"]}))
    _put_rating(root, [
        {"index": 3, "good": ["ob_0001"], "noise": ["ob_0004"]},
        {"index": 9, "good": ["ob_0099"], "noise": []},
    ])
    return root


def _golden_by_id(out: Path, name: str) -> dict:
    data = json.loads((out / "golden" / name).read_text())
    return {c["id"]: c for c in data["ranked_candidates"]}


# ---------------------------------------------------------------------------
# 1. one TEST RRU per (step, arm); summary counts; ratings_without_step
# ---------------------------------------------------------------------------


def test_one_test_rru_per_step_arm_and_summary_counts(convert, tmp_path):
    root = _standard_session(tmp_path)
    out = tmp_path / "out"
    summary = convert.convert(root, out)

    assert summary["steps_total"] == 2
    assert summary["steps_with_arms"] == 2
    assert summary["steps_rated"] == 1
    assert summary["steps_unrated"] == 1
    assert summary["ratings_without_step"] == 1
    assert summary["test_rrus_written"] == 4  # 2 steps x 2 arms
    assert summary["goldens_written"] == 1
    assert set(summary) == {
        "steps_total", "steps_with_arms", "steps_rated", "steps_unrated",
        "ratings_without_step", "test_rrus_written", "goldens_written",
    }

    files = sorted(p.name for p in (out / "test").glob("*.json"))
    assert files == sorted([
        f"{TURN}-3-thinking_tail.json", f"{TURN}-3-thinking_full.json",
        f"{TURN}-4-thinking_tail.json", f"{TURN}-4-thinking_full.json",
    ])
    rru = json.loads((out / "test" / f"{TURN}-3-thinking_tail.json").read_text())
    assert rru["query_id"] == f"{Q8}-s3"
    assert rru["query_text"] == "query for thinking_tail"
    assert [n["id"] for n in rru["ranked_nodes"]] == ["ob_0001", "ob_0002", "ob_0003"]
    assert [n["rank"] for n in rru["ranked_nodes"]] == [1, 2, 3]
    assert rru["ranked_nodes"][0]["signals"]["composite_score"] is not None
    assert rru["metadata"]["arm"] == "thinking_tail"
    assert rru["metadata"]["armLetter"] == "A"
    assert rru["metadata"]["stepMs"] == 1000.0
    assert rru["metadata"]["msOverStep"] == pytest.approx(0.1)
    assert rru["metadata"]["model"] == "test-model"


def test_summary_identities_hold_on_a_skipped_step(convert, tmp_path):
    root = _standard_session(tmp_path)
    _put_step(root, {"turnId": TURN, "index": 5, "skipped": "short-thinking",
                     "armBlind": BLIND, "ts": 1})
    summary = convert.convert(root, tmp_path / "out")
    assert summary["steps_total"] == 3
    assert summary["steps_with_arms"] == 2  # the skipped row has no arms
    assert summary["steps_with_arms"] == summary["steps_rated"] + summary["steps_unrated"]


# ---------------------------------------------------------------------------
# 2. unrated step -> no golden, steps_unrated == 1, exit 0
# ---------------------------------------------------------------------------


def test_unrated_step_has_no_golden_and_exits_zero(convert, tmp_path):
    root = _session(tmp_path)
    _put_step(root, _step(4, {"thinking_tail": ["ob_0005"], "thinking_full": ["ob_0006"]}))
    out = tmp_path / "out"
    assert convert.main([str(root), "--out", str(out)]) == 0
    assert list((out / "golden").glob("*.json")) == []
    summary = convert.convert(root, tmp_path / "out2")
    assert summary["steps_unrated"] == 1
    assert summary["steps_rated"] == 0
    assert summary["goldens_written"] == 0


# ---------------------------------------------------------------------------
# 3. relevance mapping exact (+ 8. the control that keeps it honest)
# ---------------------------------------------------------------------------


def _assert_relevance_mapping(convert, tmp_path: Path) -> None:
    """The mapping contract; also the thing the control mutates."""
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001", "ob_0002", "ob_0003"],
                              "thinking_full": ["ob_0002", "ob_0004"]}))
    _put_rating(root, [{"index": 3, "good": ["ob_0001", "ob_0077"], "noise": ["ob_0004"]}])
    out = tmp_path / "out"
    convert.convert(root, out)
    golden = _golden_by_id(out, f"q_{Q8}-s3.json")
    assert golden["ob_0001"]["grading"]["relevance"] == 2  # good
    assert golden["ob_0004"]["grading"]["relevance"] == 0  # noise
    assert golden["ob_0002"]["grading"]["relevance"] == 1  # surfaced, unmarked
    assert golden["ob_0003"]["grading"]["relevance"] == 1  # surfaced, unmarked
    # good but no arm surfaced it -> kept, flagged
    assert golden["ob_0077"]["grading"]["relevance"] == 2
    assert golden["ob_0077"]["notes"] == "unsurfaced"
    assert golden["ob_0001"].get("notes", "") != "unsurfaced"
    # the union of arm candidates plus the unsurfaced rated id, nothing else
    assert set(golden) == {"ob_0001", "ob_0002", "ob_0003", "ob_0004", "ob_0077"}


def test_relevance_mapping_exact(convert, tmp_path):
    _assert_relevance_mapping(convert, tmp_path)


def test_control_noise_mapped_to_one_is_caught_by_the_mapping_test(convert, tmp_path, monkeypatch):
    """Control: swap noise->1 and the mapping assertion MUST fail."""
    monkeypatch.setitem(convert.RELEVANCE, "noise", 1)
    with pytest.raises(AssertionError):
        _assert_relevance_mapping(convert, tmp_path)


def test_golden_loads_through_the_repo_loader(convert, tmp_path):
    from tools.recall_harness.golden_loader import load_golden_set

    root = _standard_session(tmp_path)
    out = tmp_path / "out"
    convert.convert(root, out)
    loaded = load_golden_set(str(out / "golden"))
    assert list(loaded) == [f"{Q8}-s3"]
    assert loaded[f"{Q8}-s3"].relevant_ids == {"ob_0001"}


# ---------------------------------------------------------------------------
# 4. unknown blind letter -> SystemExit naming turnId, index, letter
# ---------------------------------------------------------------------------


def test_unknown_blind_letter_exits_nonzero_naming_turn_index_letter(convert, tmp_path):
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001"], "thinking_full": ["ob_0002"]}))
    _put_rating(root, [{"index": 3, "good": ["ob_0001"], "pair": {"Z": "better"}}])
    with pytest.raises(SystemExit) as exc:
        convert.convert(root, tmp_path / "out")
    assert exc.value.code not in (0, None)
    message = str(exc.value)
    assert TURN in message and "3" in message and "Z" in message


def test_pairs_are_unblinded_through_armblind(convert, tmp_path):
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001"], "thinking_full": ["ob_0002"]}))
    _put_rating(root, [{"index": 3, "good": ["ob_0001"], "pair": {"A": "better"}}])
    out = tmp_path / "out"
    convert.convert(root, out)
    rows = [json.loads(line) for line in (out / "pairs.jsonl").read_text().splitlines()]
    assert rows == [{"turnId": TURN, "index": 3, "armA": "thinking_tail",
                     "armB": "thinking_full", "verdict": "better"}]


# ---------------------------------------------------------------------------
# 5. malformed step file -> non-zero naming the file; nothing partial written
# ---------------------------------------------------------------------------


def test_malformed_step_file_exits_naming_it_and_writes_nothing(convert, tmp_path):
    root = _standard_session(tmp_path)
    bad = root / "steps" / f"{TURN}-007.json"
    bad.write_text("{ this is not json")
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        convert.convert(root, out)
    assert exc.value.code not in (0, None)
    assert bad.name in str(exc.value)
    # nothing partial left behind claimed as complete
    leftovers = [p for p in out.rglob("*") if p.is_file()] if out.exists() else []
    assert leftovers == []


def test_refuses_to_overwrite_existing_output_without_flag(convert, tmp_path):
    root = _standard_session(tmp_path)
    out = tmp_path / "out"
    convert.convert(root, out)
    with pytest.raises(SystemExit):
        convert.convert(root, out)
    assert convert.convert(root, out, overwrite=True)["test_rrus_written"] == 4


# ---------------------------------------------------------------------------
# 6. probe_eval over a 2-arm fixture
# ---------------------------------------------------------------------------


def _eval_fixture(tmp_path: Path):
    gdir, tdir = tmp_path / "golden", tmp_path / "test"
    gdir.mkdir()
    tdir.mkdir()
    _write(gdir / "q_aaaaaaaa-s3.json", {
        "query_id": "aaaaaaaa-s3", "query_text": "q",
        "ranked_candidates": [
            {"id": "ob_0001", "grading": {"relevance": 2}},
            {"id": "ob_0002", "grading": {"relevance": 2}},
            {"id": "ob_0003", "grading": {"relevance": 1}},
            {"id": "ob_0004", "grading": {"relevance": 0}},
        ],
    })
    # a rated step no arm retrieved anything useful for: its good id is a miss
    _write(gdir / "q_cccccccc-s5.json", {
        "query_id": "cccccccc-s5", "query_text": "q",
        "ranked_candidates": [
            {"id": "ob_0077", "grading": {"relevance": 2}, "notes": "unsurfaced"},
            {"id": "ob_0078", "grading": {"relevance": 1}},
        ],
    })

    def rru(arm: str, ids: list[str], ms=100.0, step_ms=1000.0, qid="aaaaaaaa-s3", **flags):
        return {
            "query_id": qid, "query_text": "q",
            "ranked_nodes": [
                {"id": i, "rank": r + 1, "grading": None,
                 "signals": {"inContext": flags.get("in_context", False),
                             "principle": flags.get("principle", False)}, "notes": ""}
                for r, i in enumerate(ids)
            ],
            "metadata": {"arm": arm, "armLetter": "A", "ms": ms, "stepMs": step_ms,
                         "msOverStep": (ms / step_ms) if step_ms else None,
                         "model": "m", "sessionId": "s"},
        }

    _write(tdir / "x.json", rru("arm_x", ["ob_0001", "ob_0002", "ob_0003", "ob_0004"]))
    _write(tdir / "y.json", rru("arm_y", ["ob_0004", "ob_0003", "ob_0002", "ob_0001"]))
    pairs = tmp_path / "pairs.jsonl"
    pairs.write_text("\n".join(json.dumps(r) for r in [
        {"turnId": TURN, "index": 3, "armA": "arm_x", "armB": "arm_y", "verdict": "better"},
        {"turnId": TURN, "index": 4, "armA": "arm_x", "armB": "arm_y", "verdict": "better"},
        {"turnId": TURN, "index": 5, "armA": "arm_x", "armB": "arm_y", "verdict": "worse"},
    ]) + "\n")
    return gdir, tdir, pairs, rru


def test_eval_ranks_golden_aligned_arm_above_reversed(evaluator, tmp_path):
    gdir, tdir, pairs, _ = _eval_fixture(tmp_path)
    result = evaluator.evaluate(str(gdir), str(tdir), str(pairs))
    arms = result["arms"]
    assert arms["arm_x"]["ndcg_at_k_10"] == 1.0
    assert arms["arm_y"]["ndcg_at_k_10"] < arms["arm_x"]["ndcg_at_k_10"]
    assert arms["arm_x"]["n_steps"] == 1 and arms["arm_y"]["n_steps"] == 1
    assert result["pairs"][("arm_x", "arm_y")] == {"better": 2, "same": 0, "worse": 1}
    # golden-relevance-2 id nobody surfaced is an arm miss
    assert result["misses"] == {"cccccccc-s5": ["ob_0077"]}


def test_eval_report_writes_one_row_per_arm_pairs_and_misses(evaluator, tmp_path):
    gdir, tdir, pairs, _ = _eval_fixture(tmp_path)
    out = tmp_path / "report.md"
    assert evaluator.main(["--golden", str(gdir), "--test", str(tdir),
                           "--pairs", str(pairs), "--out", str(out)]) == 0
    text = out.read_text()
    assert "| arm_x |" in text and "| arm_y |" in text
    assert "arm_x" in text and "better" in text
    assert "ob_0077" in text


def test_eval_unrated_step_is_reported_not_scored(evaluator, tmp_path):
    gdir, tdir, pairs, rru = _eval_fixture(tmp_path)
    _write(tdir / "z.json", rru("arm_x", ["ob_0009"], qid="bbbbbbbb-s1"))
    result = evaluator.evaluate(str(gdir), str(tdir), str(pairs))
    assert result["unrated"] == ["bbbbbbbb-s1"]
    assert result["arms"]["arm_x"]["n_steps"] == 1  # the unrated one is not scored


def test_eval_fails_loudly_when_a_golden_file_is_silently_skippable(evaluator, tmp_path):
    """golden_loader drops names with '_v'; the eval must not let a golden
    vanish unannounced."""
    gdir, tdir, pairs, _ = _eval_fixture(tmp_path)
    _write(gdir / "q_aaaaaaaa-s9_v2.json", {
        "query_id": "aaaaaaaa-s9", "query_text": "q",
        "ranked_candidates": [{"id": "ob_0001", "grading": {"relevance": 2}}]})
    with pytest.raises(SystemExit) as exc:
        evaluator.evaluate(str(gdir), str(tdir), str(pairs))
    assert "q_aaaaaaaa-s9_v2.json" in str(exc.value)


# ---------------------------------------------------------------------------
# 7. latency handling
# ---------------------------------------------------------------------------


def test_msoverstep_is_null_when_stepms_missing(convert, tmp_path):
    root = _session(tmp_path)
    step = _step(3, {"thinking_tail": ["ob_0001"], "thinking_full": ["ob_0002"]}, step_ms=None)
    del step["stepMs"]
    _put_step(root, step)
    out = tmp_path / "out"
    convert.convert(root, out)
    rru = json.loads((out / "test" / f"{TURN}-3-thinking_tail.json").read_text())
    assert rru["metadata"]["msOverStep"] is None
    assert rru["metadata"]["stepMs"] is None
    assert rru["metadata"]["ms"] == 100.0  # ms itself is still real
    # stepMs == 0 is also "unknown", never a fake 0 ratio
    root2 = tmp_path / "s2"
    (root2 / "steps").mkdir(parents=True)
    zero = _step(3, {"thinking_tail": ["ob_0001"]}, step_ms=0)
    _put_step(root2, zero)
    convert.convert(root2, tmp_path / "out2")
    rru0 = json.loads((tmp_path / "out2" / "test" / f"{TURN}-3-thinking_tail.json").read_text())
    assert rru0["metadata"]["msOverStep"] is None


def test_latency_stats_use_present_values_only_and_report_exclusions(evaluator, tmp_path):
    gdir, tdir, pairs, rru = _eval_fixture(tmp_path)
    # three more arm_x RRUs on the SAME rated step id with varied latency;
    # one has no stepMs (msOverStep None).
    for n, (ms, step_ms) in enumerate([(200.0, 1000.0), (300.0, None), (400.0, 1000.0)]):
        _write(tdir / f"extra{n}.json", rru("arm_x", ["ob_0001"], ms=ms, step_ms=step_ms))
    result = evaluator.evaluate(str(gdir), str(tdir), str(pairs))
    x = result["arms"]["arm_x"]
    # ms present for all 4 arm_x RRUs: 100,200,300,400
    assert x["median_ms"] == 250.0
    assert x["p90_ms"] == 400.0
    # msOverStep present for 3 of 4: 0.1, 0.2, 0.4 -> median 0.2
    assert x["median_ms_over_step"] == pytest.approx(0.2)
    assert x["ms_over_step_excluded"] == 1
    out = tmp_path / "report.md"
    evaluator.main(["--golden", str(gdir), "--test", str(tdir), "--out", str(out)])
    assert "excluded" in out.read_text()


def test_main_without_pairs_still_writes_report(evaluator, tmp_path):
    gdir, tdir, _, _ = _eval_fixture(tmp_path)
    out = tmp_path / "r.md"
    assert evaluator.main(["--golden", str(gdir), "--test", str(tdir), "--out", str(out)]) == 0
    assert out.exists()


# ---------------------------------------------------------------------------
# golden filename hazard: a turnId starting with "v"
# ---------------------------------------------------------------------------


def test_turn_id_starting_with_v_round_trips_through_convert_and_eval(convert, evaluator, tmp_path):
    turn = "vvvv1234-1111-2222-3333-444444444444"
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001", "ob_0002"],
                              "thinking_full": ["ob_0002", "ob_0003"]}, turn=turn))
    _put_rating(root, [{"index": 3, "good": ["ob_0001"]}], turn=turn)
    out = tmp_path / "out"
    summary = convert.convert(root, out)
    assert summary["goldens_written"] == 1
    names = [p.name for p in (out / "golden").glob("*.json")]
    assert names == ["q_tvvvv1234-s3.json"]
    result = evaluator.evaluate(str(out / "golden"), str(out / "test"), None)
    assert result["n_goldens"] == 1
    assert result["unrated"] == []
    assert result["arms"]["thinking_tail"]["n_steps"] == 1


# ---------------------------------------------------------------------------
# unshown candidates (the rater only sees what the request showed)
# ---------------------------------------------------------------------------


def _unshown_session(tmp_path: Path) -> Path:
    """arm X (thinking_tail) surfaces 5 ids; the request showed only the top 3."""
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001", "ob_0002", "ob_0003", "ob_0004", "ob_0005"],
                              "thinking_full": ["ob_0001", "ob_0006"]}))
    _put_request(root, shown={"3": {"A": ["ob_0001", "ob_0002", "ob_0003"], "B": ["ob_0001"]}})
    # ob_0004 (rank 4, unshown) is rated good anyway, as if read from the files
    _put_rating(root, [{"index": 3, "good": ["ob_0001", "ob_0004"]}], request=False)
    return root


def test_unshown_surfaced_ids_are_marked_in_the_golden_and_relevance_stays_one(convert, tmp_path):
    root = _unshown_session(tmp_path)
    out = tmp_path / "out"
    convert.convert(root, out)
    golden = _golden_by_id(out, f"q_{Q8}-s3.json")
    for unshown in ("ob_0005", "ob_0006"):  # surfaced, never shown, unrated
        assert golden[unshown]["notes"] == "unshown"
        assert golden[unshown]["grading"]["relevance"] == 1
    assert golden["ob_0002"].get("notes", "") == ""  # shown, neutral
    assert golden["ob_0001"].get("notes", "") == ""
    # a rated id keeps its rating; unshown does not override a rating the agent gave
    assert golden["ob_0004"]["grading"]["relevance"] == 2
    assert golden["ob_0004"].get("notes", "") != "unsurfaced"


def test_test_rru_carries_the_ids_the_request_showed(convert, tmp_path):
    root = _unshown_session(tmp_path)
    out = tmp_path / "out"
    convert.convert(root, out)
    rru = json.loads((out / "test" / f"{TURN}-3-thinking_tail.json").read_text())
    assert rru["metadata"]["shownIds"] == ["ob_0001", "ob_0002", "ob_0003"]


def test_missing_requests_file_for_a_rated_turn_exits_naming_the_turn(convert, tmp_path):
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001"], "thinking_full": ["ob_0002"]}))
    _put_rating(root, [{"index": 3, "good": ["ob_0001"]}], request=False)
    with pytest.raises(SystemExit) as exc:
        convert.convert(root, tmp_path / "out")
    assert exc.value.code not in (0, None)
    assert TURN in str(exc.value) and "request" in str(exc.value)
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").rglob("*.json"))


def test_unrated_turn_needs_no_requests_file(convert, tmp_path):
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001"]}))
    assert convert.convert(root, tmp_path / "out")["steps_unrated"] == 1


def test_eval_reports_unshown_rate_and_shown_only_truncates(convert, evaluator, tmp_path):
    root = _unshown_session(tmp_path)
    out = tmp_path / "out"
    convert.convert(root, out)
    full = evaluator.evaluate(str(out / "golden"), str(out / "test"), None)
    # thinking_tail surfaced 5 ids, 2 of them marked unshown (ob_0005 and... ob_0004 is rated, so 1)
    tail = full["arms"]["thinking_tail"]
    assert tail["unshown_rate"] == pytest.approx(1 / 5)
    assert full["arms"]["thinking_full"]["unshown_rate"] == pytest.approx(1 / 2)
    assert full["shown_only"] is False

    shown = evaluator.evaluate(str(out / "golden"), str(out / "test"), None, shown_only=True)
    assert shown["shown_only"] is True
    # ob_0004 (good, rank 4) was never shown: --shown-only drops it from the ranking
    assert shown["arms"]["thinking_tail"]["recall_at_k_10"] < full["arms"]["thinking_tail"]["recall_at_k_10"]
    report = tmp_path / "r.md"
    assert evaluator.main(["--golden", str(out / "golden"), "--test", str(out / "test"),
                           "--shown-only", "--out", str(report)]) == 0
    text = report.read_text()
    assert "shown-only" in text and "unshown" in text


# ---------------------------------------------------------------------------
# pair keys: A and B only
# ---------------------------------------------------------------------------


def test_pair_key_other_than_a_or_b_exits_naming_turn_index_letter(convert, tmp_path):
    root = _session(tmp_path)
    blind = {"A": "thinking_tail", "B": "thinking_full", "C": "tool_input"}
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001"], "thinking_full": ["ob_0002"],
                              "tool_input": ["ob_0003"]}, blind=blind))
    _put_rating(root, [{"index": 3, "good": ["ob_0001"], "pair": {"C": "better"}}])
    with pytest.raises(SystemExit) as exc:
        convert.convert(root, tmp_path / "out")
    assert exc.value.code not in (0, None)
    message = str(exc.value)
    assert TURN in message and "3" in message and "'C'" in message
    # never a silent C-vs-A: no output was written
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").rglob("*.json"))


def test_pair_key_b_is_accepted_and_compares_against_a(convert, tmp_path):
    root = _session(tmp_path)
    _put_step(root, _step(3, {"thinking_tail": ["ob_0001"], "thinking_full": ["ob_0002"]}))
    _put_rating(root, [{"index": 3, "good": ["ob_0001"], "pair": {"B": "worse"}}])
    out = tmp_path / "out"
    convert.convert(root, out)
    rows = [json.loads(line) for line in (out / "pairs.jsonl").read_text().splitlines()]
    assert rows == [{"turnId": TURN, "index": 3, "armA": "thinking_full",
                     "armB": "thinking_tail", "verdict": "worse"}]


# ---------------------------------------------------------------------------
# novel_terms.py: which graph arm E reads
# ---------------------------------------------------------------------------


def test_novel_terms_default_db_follows_engram_home(tmp_path):
    import sqlite3
    import subprocess

    home = tmp_path / "seat-home"
    home.mkdir()
    db = home / "knowledge.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (id TEXT, is_current INTEGER)")
    conn.commit()
    conn.close()
    env = {**os.environ, "ENGRAM_HOME": str(home)}
    run = subprocess.run(
        [sys.executable, str(PROBE_DIR / "novel_terms.py"), "--state", str(tmp_path / "seen.json")],
        input="some thinking text", capture_output=True, text=True, env=env, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["db"] == str(db)
    assert out["terms"] == [] and out["novel"] == []  # empty graph: nothing to extract


def test_novel_terms_default_db_without_engram_home_is_dot_engram(monkeypatch):
    nt = _load("novel_terms")
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    assert nt.default_db() == Path.home() / ".engram" / "knowledge.db"
    monkeypatch.setenv("ENGRAM_HOME", "/somewhere/else")
    assert nt.default_db() == Path("/somewhere/else") / "knowledge.db"


# ---------------------------------------------------------------------------
# input validation BEFORE any write (path traversal via ids / arm names)
# ---------------------------------------------------------------------------


def _tree(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")}


def _assert_dies_naming(convert, tmp_path: Path, root: Path, file_name: str, field: str) -> None:
    before = _tree(tmp_path)
    with pytest.raises(SystemExit) as exc:
        convert.convert(root, tmp_path / "out")
    assert exc.value.code not in (0, None)
    message = str(exc.value)
    assert file_name in message and field in message
    # nothing written anywhere under the tmp parent, inside or outside --out
    assert _tree(tmp_path) == before


def test_step_turn_id_with_traversal_is_refused_before_any_write(convert, tmp_path):
    root = _session(tmp_path)
    bad = _write(root / "steps" / "evil-000.json",
                 _step(0, {"tool_input": ["ob_0001"]}, turn="../../escape"))
    _assert_dies_naming(convert, tmp_path, root, bad.name, "turnId")


def test_rating_turn_id_with_traversal_is_refused_before_any_write(convert, tmp_path):
    root = _standard_session(tmp_path)
    bad = _write(root / "ratings" / "evil-1.json",
                 {"ts": 1, "sessionId": "s", "turnId": "../x", "steps": [{"index": 3, "good": []}]})
    _assert_dies_naming(convert, tmp_path, root, bad.name, "turnId")


def test_request_turn_id_with_traversal_is_refused_before_any_write(convert, tmp_path):
    root = _standard_session(tmp_path)
    bad = _write(root / "requests" / "evil.json", {"turnId": "../x", "shown": {}})
    _assert_dies_naming(convert, tmp_path, root, bad.name, "turnId")


def test_arm_name_with_traversal_is_refused_before_any_write(convert, tmp_path):
    root = _session(tmp_path)
    step = _step(0, {"thinking_tail": ["ob_0001"]})
    step["arms"]["../../evil"] = _arm("../../evil", ["ob_0002"])
    bad = _put_step(root, step)
    _assert_dies_naming(convert, tmp_path, root, bad.name, "arm")


def test_unknown_arm_negative_index_and_bad_blind_letter_are_refused(convert, tmp_path):
    cases = [
        (lambda st: st["arms"].__setitem__("not_an_arm", _arm("not_an_arm", ["ob_0001"])), "arm"),
        (lambda st: st.__setitem__("index", -1), "index"),
        (lambda st: st.__setitem__("armBlind", {"a": "thinking_tail", "B": "thinking_full"}), "armBlind"),
        (lambda st: st.__setitem__("armBlind", {"A": "../evil", "B": "thinking_full"}), "armBlind"),
    ]
    for n, (mutate, field) in enumerate(cases):
        sub = tmp_path / f"case{n}"
        sub.mkdir()
        root = _session(sub)
        step = _step(0, {"thinking_tail": ["ob_0001"], "thinking_full": ["ob_0002"]})
        mutate(step)
        bad = _write(root / "steps" / "x.json", step)
        _assert_dies_naming(convert, sub, root, bad.name, field)


def test_valid_session_still_converts_control(convert, tmp_path):
    root = _standard_session(tmp_path)
    summary = convert.convert(root, tmp_path / "out")
    assert summary["test_rrus_written"] == 4 and summary["goldens_written"] == 1


def test_eval_rejects_a_test_rru_the_converter_would_have_refused(evaluator, tmp_path):
    gdir, tdir, _, rru = _eval_fixture(tmp_path)
    bad = rru("../../evil", ["ob_0001"])
    _write(tdir / "bad_arm.json", bad)
    with pytest.raises(SystemExit) as exc:
        evaluator.evaluate(str(gdir), str(tdir), None)
    assert "bad_arm.json" in str(exc.value) and "arm" in str(exc.value)
    (tdir / "bad_arm.json").unlink()
    _write(tdir / "bad_q.json", rru("arm_x", ["ob_0001"], qid="../../q"))
    with pytest.raises(SystemExit) as exc:
        evaluator.evaluate(str(gdir), str(tdir), None)
    assert "bad_q.json" in str(exc.value) and "query_id" in str(exc.value)


# ---------------------------------------------------------------------------
# output permissions: files 0600, dirs 0700, a shared --out is refused
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are not meaningful here")


def _modes(root: Path):
    return [(p, p.stat().st_mode & 0o777, p.is_dir()) for p in [root, *root.rglob("*")]]


@posix_only
def test_converter_outputs_are_0600_files_in_0700_dirs(convert, tmp_path):
    old = os.umask(0o022)
    try:
        out = tmp_path / "out"
        convert.convert(_standard_session(tmp_path), out)
    finally:
        os.umask(old)
    entries = _modes(out)
    assert len(entries) > 5
    for path, mode, is_dir in entries:
        assert mode == (0o700 if is_dir else 0o600), f"{path} is {oct(mode)}"


@posix_only
def test_preexisting_shared_out_is_refused_naming_path_and_mode(convert, tmp_path):
    root = _standard_session(tmp_path)
    out = tmp_path / "shared"
    out.mkdir()
    os.chmod(out, 0o755)
    with pytest.raises(SystemExit) as exc:
        convert.convert(root, out)
    assert str(out) in str(exc.value) and "0o755" in str(exc.value)
    assert list(out.iterdir()) == []


@posix_only
def test_overwrite_rerun_keeps_modes(convert, tmp_path):
    root = _standard_session(tmp_path)
    out = tmp_path / "out"
    convert.convert(root, out)
    convert.convert(root, out, overwrite=True)
    for path, mode, is_dir in _modes(out):
        assert mode == (0o700 if is_dir else 0o600), f"{path} is {oct(mode)}"


@posix_only
def test_eval_report_is_0600(evaluator, tmp_path):
    gdir, tdir, pairs, _ = _eval_fixture(tmp_path)
    report = tmp_path / "report.md"
    old = os.umask(0o022)
    try:
        evaluator.main(["--golden", str(gdir), "--test", str(tdir), "--out", str(report)])
        evaluator.main(["--golden", str(gdir), "--test", str(tdir), "--out", str(report)])  # replace
    finally:
        os.umask(old)
    assert report.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# trailing-newline bypass: "$" matches before a final newline; checks must fullmatch
# ---------------------------------------------------------------------------

NL = chr(10)


def test_step_turn_id_with_trailing_newline_is_refused(convert, tmp_path):
    root = _session(tmp_path)
    bad = _write(root / "steps" / "nl-000.json", _step(0, {"tool_input": ["ob_0001"]}, turn="abc" + NL))
    _assert_dies_naming(convert, tmp_path, root, bad.name, "turnId")


def test_rating_turn_id_with_trailing_newline_is_refused(convert, tmp_path):
    root = _standard_session(tmp_path)
    bad = _write(root / "ratings" / "nl-1.json",
                 {"ts": 1, "sessionId": "s", "turnId": "abc" + NL, "steps": [{"index": 3, "good": []}]})
    _assert_dies_naming(convert, tmp_path, root, bad.name, "turnId")


def test_blind_letter_with_trailing_newline_is_refused(convert, tmp_path):
    root = _session(tmp_path)
    step = _step(0, {"thinking_tail": ["ob_0001"], "thinking_full": ["ob_0002"]})
    step["armBlind"] = {"A" + NL: "thinking_tail", "B": "thinking_full"}
    bad = _write(root / "steps" / "x.json", step)
    _assert_dies_naming(convert, tmp_path, root, bad.name, "armBlind")


def test_eval_rejects_trailing_newline_ids(evaluator, tmp_path):
    gdir, tdir, _, rru = _eval_fixture(tmp_path)
    _write(tdir / "nl_q.json", rru("arm_x", ["ob_0001"], qid="abc" + NL))
    with pytest.raises(SystemExit) as exc:
        evaluator.evaluate(str(gdir), str(tdir), None)
    assert "nl_q.json" in str(exc.value)
    (tdir / "nl_q.json").unlink()
    _write(tdir / "nl_arm.json", rru("arm_x" + NL, ["ob_0001"]))
    with pytest.raises(SystemExit) as exc:
        evaluator.evaluate(str(gdir), str(tdir), None)
    assert "nl_arm.json" in str(exc.value)
