"""#3045 — `cohort_dispatch validate` must reconcile returned ids against sent ids.

Contract-first trial case (CLAUDE.md "Contract-first tests"). Every item
returned in these tests is individually VALID per `validate_summary_entry`, so
the tests isolate id-set reconciliation from per-item quality.

Contract, per chunk (sent = ids in chunk-N/payload.json, returned = node_ids
in the parsed agent_output):
  1. dropped (sent, not returned)  -> failure, error "dropped: ..." -> retried
  2. unexpected (returned, not sent) -> never clean/final/retry; reported in
     summary `unexpected` + loud stderr block. Unexpected-only: exit 0 with
     final_payload.json, no retry (exit 1 <=> retry_payload.json written)
  3. duplicate (returned K>1 times) -> ONE failure "returned K times in chunk-N"
  4. whole-run invariant: every sent id in exactly one of clean/failures, and
     nothing else is clean; violation -> exit 2 (a validate bug, not a fairy error)
  5. summary gains `dropped` (count); existing keys unchanged
  6. no readable payload for a chunk -> stderr warning, that chunk's
     reconciliation skipped (never reported as reconciled) -- ad-hoc mode only
  7. manifest mode: sent ids come from the manifest payload_path; an unusable
     payload -> exit 2 naming the chunk, before any output is written
  8. stale final_payload.json / retry_payload.json are deleted at start, so a
     file present afterwards was written by this run
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.cohort_dispatch import main as dispatch_main  # noqa: E402


def _ids(n: int, prefix: str = "ob_") -> list[str]:
    return [f"{prefix}{i:04d}" for i in range(1, n + 1)]


def _valid_item(node_id: str) -> dict:
    return {
        "node_id": node_id,
        "recall_summary": f"Short valid summary for {node_id}.",
        "recall_keywords": ["test-kw", "batch-summary", "recall"],
    }


def _write_chunk(out_dir: Path, index: int, sent: list[str], returned_items: list[dict],
                 *, write_payload: bool = True) -> Path:
    chunk_dir = out_dir / f"chunk-{index}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    if write_payload:
        (chunk_dir / "payload.json").write_text(json.dumps(
            [{"id": nid, "type": "observation_factual", "claim": f"claim {nid}"} for nid in sent]
        ))
    (chunk_dir / "agent_output.json").write_text(json.dumps({"items": returned_items}))
    return chunk_dir


def _write_manifest(out_dir: Path, chunk_dirs: list[Path]) -> None:
    (out_dir / "manifest.json").write_text(json.dumps({
        "total_nodes": 0,
        "total_chunks": len(chunk_dirs),
        "chunk_size": 15,
        "out_dir": str(out_dir),
        "attempt_count": 0,
        "chunks": [
            {"chunk_index": i, "node_count": 0, "node_ids": [], "chunk_dir": str(c),
             "payload_path": str(c / "payload.json")}
            for i, c in enumerate(chunk_dirs)
        ],
    }))


def _validate(out_dir: Path, capsys) -> tuple[int, dict, str]:
    code = dispatch_main(["validate", "--out", str(out_dir)])
    cap = capsys.readouterr()
    summary = json.loads(cap.out) if cap.out.strip() else {}
    return code, summary, cap.err


def _load(out_dir: Path, name: str):
    p = out_dir / name
    return json.loads(p.read_text()) if p.exists() else None


def _setup_one_chunk(tmp_path: Path, sent: list[str], returned_items: list[dict]) -> Path:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    chunk = _write_chunk(out_dir, 0, sent, returned_items)
    _write_manifest(out_dir, [chunk])
    return out_dir


# R1 - the issue's case: 15 sent, 14 valid returned (one dropped mid-list)
def test_r1_dropped_id_is_a_failure(tmp_path, capsys):
    sent = _ids(15)
    dropped = sent[7]
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent if n != dropped])

    code, summary, err = _validate(out_dir, capsys)

    assert code == 1
    failures = _load(out_dir, "failures.json")
    assert [f["node_id"] for f in failures] == [dropped]
    assert failures[0]["error"] == "dropped: sent in chunk-0 payload, not returned by the fairy"
    assert failures[0]["payload"]["id"] == dropped
    assert len(_load(out_dir, "clean_items.json")) == 14
    assert summary["clean"] == 14
    assert summary["failures"] == 1
    assert summary["dropped"] == 1
    assert summary["clean"] + summary["failures"] == len(sent)


# R2 - the retry path carries the dropped node end to end
def test_r2_dropped_id_flows_through_retry_and_incorporate(tmp_path, capsys):
    sent = _ids(15)
    dropped = sent[7]
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent if n != dropped])
    assert _validate(out_dir, capsys)[0] == 1

    retry_payload = _load(out_dir, "retry_payload.json")
    assert [e["id"] for e in retry_payload] == [dropped]
    assert retry_payload[0]["previous_error"].startswith("dropped:")

    retry_output = tmp_path / "retry_output.json"
    retry_output.write_text(json.dumps({"items": [_valid_item(dropped)]}))
    code = dispatch_main(["incorporate", "--retry-output", str(retry_output), "--out", str(out_dir)])
    capsys.readouterr()

    assert code == 0
    final = _load(out_dir, "final_payload.json")
    assert dropped in {s["node_id"] for s in final["summaries"]}
    assert len(final["summaries"]) == 15
    unfixable = _load(out_dir, "unfixable.json") or []
    assert dropped not in {u["node_id"] for u in unfixable}
    assert not (out_dir / "retry_payload.json").exists()


# R3 - an id that was never sent, nothing else wrong: reconciliation is complete
# (nothing sent is missing), the invented item is discarded loudly, nothing to
# retry -> exit 0 with final_payload.json (skill contract: exit 1 <=> retry written)
def test_r3_unexpected_only_is_discarded_loudly_and_exits_0(tmp_path, capsys):
    sent = _ids(3)
    invented = "ob_9999"
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent] + [_valid_item(invented)])

    code, summary, err = _validate(out_dir, capsys)

    assert code == 0
    assert summary["unexpected"] == [{"node_id": invented, "chunk": "chunk-0"}]
    assert invented in err and "chunk-0" in err
    assert {c["node_id"] for c in _load(out_dir, "clean_items.json")} == set(sent)
    final = _load(out_dir, "final_payload.json")
    assert {s["node_id"] for s in final["summaries"]} == set(sent)
    assert not (out_dir / "retry_payload.json").exists()
    assert _load(out_dir, "failures.json") == []


# R3b - failures + unexpected together: exit 1, retry holds only the failures,
# the unexpected id is excluded from every output file and still listed
def test_r3b_failures_plus_unexpected(tmp_path, capsys):
    sent = _ids(4)
    dropped = sent[2]
    invented = "ob_9999"
    returned = [_valid_item(n) for n in sent if n != dropped] + [_valid_item(invented)]
    out_dir = _setup_one_chunk(tmp_path, sent, returned)

    code, summary, err = _validate(out_dir, capsys)

    assert code == 1
    assert summary["unexpected"] == [{"node_id": invented, "chunk": "chunk-0"}]
    assert invented in err
    assert [e["id"] for e in _load(out_dir, "retry_payload.json")] == [dropped]
    assert not (out_dir / "final_payload.json").exists()
    for name in ("clean_items.json", "failures.json", "retry_payload.json"):
        assert invented not in json.dumps(_load(out_dir, name)), name


# R4 - duplicate
def test_r4_duplicate_id_is_one_failure_and_not_clean(tmp_path, capsys):
    sent = _ids(3)
    dup = sent[1]
    items = [_valid_item(sent[0]), _valid_item(dup), _valid_item(dup), _valid_item(sent[2])]
    out_dir = _setup_one_chunk(tmp_path, sent, items)

    code, summary, err = _validate(out_dir, capsys)

    assert code == 1
    failures = _load(out_dir, "failures.json")
    assert [f["node_id"] for f in failures] == [dup]
    assert "returned 2 times" in failures[0]["error"]
    assert dup not in {c["node_id"] for c in _load(out_dir, "clean_items.json")}
    assert summary["clean"] + summary["failures"] == len(sent)
    assert [e["id"] for e in _load(out_dir, "retry_payload.json")] == [dup]


# R5 - control: an all-correct chunk
def test_r5_all_correct_chunk_is_clean(tmp_path, capsys):
    sent = _ids(15)
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent])

    code, summary, err = _validate(out_dir, capsys)

    assert code == 0
    assert _load(out_dir, "failures.json") == []
    assert summary["clean"] == 15
    assert summary["failures"] == 0
    assert summary["dropped"] == 0
    assert summary["unexpected"] == []
    assert len(_load(out_dir, "final_payload.json")["summaries"]) == 15


# R6 - the invariant helper must actually refuse inconsistent input
def test_r6_invariant_check_refuses_inconsistent_input():
    from tools.cohort_dispatch import ReconciliationInvariantError, _assert_reconciled

    sent = ["a", "b", "c"]
    _assert_reconciled(sent, ["a", "b"], ["c"])  # consistent: no raise

    with pytest.raises(ReconciliationInvariantError) as ei:
        _assert_reconciled(sent, ["a"], ["c"])  # b is in neither
    assert "b" in str(ei.value)
    with pytest.raises(ReconciliationInvariantError) as ei:
        _assert_reconciled(sent, ["a", "b", "c"], ["c"])  # c in both
    assert "c" in str(ei.value)
    with pytest.raises(ReconciliationInvariantError) as ei:
        _assert_reconciled(sent, ["a", "b", "zzz"], ["c"])  # zzz clean but never sent
    assert "zzz" in str(ei.value)


# Contract 4 wiring: a violation inside validate exits 2 and writes nothing
def test_r6b_validate_exits_2_when_invariant_fails(tmp_path, capsys, monkeypatch):
    import tools.cohort_dispatch as cd

    def _boom(sent, clean, failed):
        raise cd.ReconciliationInvariantError("forced")

    monkeypatch.setattr(cd, "_assert_reconciled", _boom)
    sent = _ids(2)
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent])
    code = dispatch_main(["validate", "--out", str(out_dir)])
    capsys.readouterr()
    assert code == 2
    assert not (out_dir / "clean_items.json").exists()


# Contract 6: no readable payload -> warn, skip that chunk only, never claim reconciled
def test_no_payload_chunk_warns_and_skips_only_that_chunk(tmp_path, capsys):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    sent0 = _ids(3)
    _write_chunk(out_dir, 0, sent0, [_valid_item(n) for n in sent0[:2]])  # one dropped
    _write_chunk(out_dir, 1, [], [_valid_item("ob_5001")], write_payload=False)

    code, summary, err = _validate(out_dir, capsys)  # no manifest: ad-hoc path

    assert code == 1
    assert "chunk-1" in err and "reconcil" in err.lower()
    assert summary["dropped"] == 1  # chunk-0 still reconciled
    assert {c["node_id"] for c in _load(out_dir, "clean_items.json")} == {sent0[0], sent0[1], "ob_5001"}


# R7 - an item with a missing / non-string node_id is unexpected as <unknown>;
# the id it was meant to carry is dropped and therefore retried
@pytest.mark.parametrize("bad_id", [None, 123])
def test_r7_missing_or_nonstring_node_id_is_unknown_and_sent_id_dropped(tmp_path, capsys, bad_id):
    sent = _ids(3)
    intended = sent[1]
    bad = _valid_item(intended)
    if bad_id is None:
        del bad["node_id"]
    else:
        bad["node_id"] = bad_id
    returned = [_valid_item(sent[0]), bad, _valid_item(sent[2])]
    out_dir = _setup_one_chunk(tmp_path, sent, returned)

    code, summary, err = _validate(out_dir, capsys)

    assert code == 1
    assert summary["unexpected"] == [{"node_id": "<unknown>", "chunk": "chunk-0"}]
    assert [f["node_id"] for f in _load(out_dir, "failures.json")] == [intended]
    assert [e["id"] for e in _load(out_dir, "retry_payload.json")] == [intended]


_OUTPUT_FILES = ("clean_items.json", "failures.json", "final_payload.json", "retry_payload.json")


# R8 - manifest mode: a chunk's manifest payload file is missing -> pipeline bug,
# exit 2 naming the chunk, no outputs written
def test_r8_manifest_payload_missing_exits_2(tmp_path, capsys):
    sent = _ids(2)
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent])
    (out_dir / "chunk-0" / "payload.json").unlink()

    code, summary, err = _validate(out_dir, capsys)

    assert code == 2
    assert "chunk-0" in err
    for name in _OUTPUT_FILES:
        assert not (out_dir / name).exists(), name


# R8b - same for an unreadable / non-list payload in manifest mode
@pytest.mark.parametrize("content", ["{not json", json.dumps({"id": "ob_0001"})])
def test_r8b_manifest_payload_unreadable_or_not_list_exits_2(tmp_path, capsys, content):
    sent = _ids(2)
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent])
    (out_dir / "chunk-0" / "payload.json").write_text(content)

    code, summary, err = _validate(out_dir, capsys)

    assert code == 2
    assert "chunk-0" in err
    for name in _OUTPUT_FILES:
        assert not (out_dir / name).exists(), name


# R8c - manifest mode + unparseable agent_output + unreadable payload -> exit 2, no traceback
def test_r8c_manifest_parse_error_branch_with_bad_payload_exits_2(tmp_path, capsys):
    sent = _ids(2)
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent])
    (out_dir / "chunk-0" / "agent_output.json").write_text("this is not json")
    (out_dir / "chunk-0" / "payload.json").write_text("{not json")

    code, summary, err = _validate(out_dir, capsys)

    assert code == 2
    assert "chunk-0" in err


# R8d - ad-hoc mode: parse-error branch with an unreadable payload warns, no traceback
def test_r8d_adhoc_parse_error_branch_with_bad_payload_warns(tmp_path, capsys):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    chunk = _write_chunk(out_dir, 0, _ids(2), [])
    (chunk / "agent_output.json").write_text("this is not json")
    (chunk / "payload.json").write_text("{not json")

    code, summary, err = _validate(out_dir, capsys)  # no manifest

    assert code == 0
    assert "chunk-0" in err and "warning" in err.lower()


# R9 - manifest mode: a payload entry without a string id -> exit 2
@pytest.mark.parametrize("entry", [{"claim": "no id here"}, {"id": 7}])
def test_r9_manifest_payload_entry_without_string_id_exits_2(tmp_path, capsys, entry):
    sent = _ids(2)
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent])
    (out_dir / "chunk-0" / "payload.json").write_text(json.dumps([{"id": sent[0]}, entry]))

    code, summary, err = _validate(out_dir, capsys)

    assert code == 2
    assert "chunk-0" in err
    for name in _OUTPUT_FILES:
        assert not (out_dir / name).exists(), name


# R10 - an empty chunk payload with empty output is a clean run
def test_r10_empty_chunk_is_clean(tmp_path, capsys):
    out_dir = _setup_one_chunk(tmp_path, [], [])

    code, summary, err = _validate(out_dir, capsys)

    assert code == 0
    assert summary["clean"] == 0 and summary["failures"] == 0
    assert _load(out_dir, "final_payload.json")["summaries"] == []
    assert not (out_dir / "retry_payload.json").exists()


_STALE = {"stale": True}


def _seed_stale(out_dir: Path) -> None:
    (out_dir / "final_payload.json").write_text(json.dumps(_STALE))
    (out_dir / "retry_payload.json").write_text(json.dumps([_STALE]))


# R12 - a file present after validate was always written by THIS run: a stale
# final_payload.json must not survive a validate that exits 2
def test_r12_stale_final_payload_is_deleted_when_validate_exits_2(tmp_path, capsys):
    sent = _ids(2)
    out_dir = _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent])
    (out_dir / "chunk-0" / "payload.json").unlink()  # forces exit 2
    _seed_stale(out_dir)

    code, _summary, _err = _validate(out_dir, capsys)

    assert code == 2
    assert not (out_dir / "final_payload.json").exists()
    assert not (out_dir / "retry_payload.json").exists()


def _scenario_clean(tmp_path):
    sent = _ids(3)
    return _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent])


def _scenario_unexpected_only(tmp_path):
    sent = _ids(3)
    return _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent] + [_valid_item("ob_9999")])


def _scenario_dropped(tmp_path):
    sent = _ids(3)
    return _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent[:2]])


def _scenario_dropped_plus_unexpected(tmp_path):
    sent = _ids(3)
    return _setup_one_chunk(tmp_path, sent, [_valid_item(n) for n in sent[:2]] + [_valid_item("ob_9999")])


def _scenario_missing_payload(tmp_path):
    out_dir = _scenario_clean(tmp_path)
    (out_dir / "chunk-0" / "payload.json").unlink()
    return out_dir


def _scenario_manifest_not_json(tmp_path):
    out_dir = _scenario_clean(tmp_path)
    (out_dir / "manifest.json").write_text("{not json")
    return out_dir


def _scenario_manifest_chunk_missing_payload_path(tmp_path):
    out_dir = _scenario_clean(tmp_path)
    manifest = json.loads((out_dir / "manifest.json").read_text())
    for c in manifest["chunks"]:
        del c["payload_path"]
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    return out_dir


_SCENARIOS = [
    ("clean", _scenario_clean, 0),
    ("unexpected_only", _scenario_unexpected_only, 0),
    ("dropped", _scenario_dropped, 1),
    ("dropped_plus_unexpected", _scenario_dropped_plus_unexpected, 1),
    ("missing_manifest_payload", _scenario_missing_payload, 2),
    # Uncaught exceptions must map to 2, never Python's default 1, which the
    # skill reads as "retry_payload.json written" (exit 1 <=> retry written).
    ("manifest_not_json", _scenario_manifest_not_json, 2),
    ("manifest_chunk_missing_payload_path", _scenario_manifest_chunk_missing_payload_path, 2),
]


# R11 - the file-state contract the engram-sleep skill relies on, per exit code:
#   0 => final_payload.json exists, no retry_payload.json
#   1 => retry_payload.json exists (and no final_payload.json)
#   2 => neither was written by this run
# Run both fresh and with stale copies of both files seeded beforehand.
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale_seed"])
@pytest.mark.parametrize("name,build,expected", _SCENARIOS, ids=[s[0] for s in _SCENARIOS])
def test_r11_exit_code_file_state_contract(tmp_path, capsys, name, build, expected, stale):
    out_dir = build(tmp_path)
    if stale:
        _seed_stale(out_dir)

    code, _summary, _err = _validate(out_dir, capsys)

    assert code == expected
    final, retry = out_dir / "final_payload.json", out_dir / "retry_payload.json"
    if expected == 0:
        assert final.exists() and not retry.exists()
        assert _load(out_dir, "final_payload.json") != _STALE
    elif expected == 1:
        assert retry.exists() and not final.exists()
        assert _load(out_dir, "retry_payload.json") != [_STALE]
    else:
        assert not final.exists() and not retry.exists()


# R14 - an UNEXPECTED exception exits 2 (never Python's default 1), with a
# one-line error naming the exception type plus the traceback on stderr
def test_r14_unparseable_manifest_exits_2_naming_the_exception(tmp_path, capsys):
    out_dir = _scenario_manifest_not_json(tmp_path)
    _seed_stale(out_dir)

    code, _summary, err = _validate(out_dir, capsys)

    assert code == 2
    assert not (out_dir / "final_payload.json").exists()
    assert not (out_dir / "retry_payload.json").exists()
    assert "error: validate failed unexpectedly: JSONDecodeError" in err
    assert "Traceback" in err


def test_r14b_manifest_chunk_missing_payload_path_exits_2(tmp_path, capsys):
    out_dir = _scenario_manifest_chunk_missing_payload_path(tmp_path)

    code, _summary, err = _validate(out_dir, capsys)

    assert code == 2
    assert not (out_dir / "final_payload.json").exists()
    assert not (out_dir / "retry_payload.json").exists()
    assert "error: validate failed unexpectedly: KeyError" in err


# R15 - incorporate has the same exposure (unparseable failures.json,
# malformed failure entry) and the skill reads its exit codes the same way
def _incorporate(out_dir: Path, retry_output: Path, capsys) -> tuple[int, str]:
    code = dispatch_main(["incorporate", "--retry-output", str(retry_output),
                          "--out", str(out_dir)])
    return code, capsys.readouterr().err


@pytest.mark.parametrize("failures_text", ["{not json", json.dumps([{"no_node_id": 1}])],
                         ids=["not_json", "entry_without_node_id"])
def test_r15_incorporate_unexpected_exception_exits_2(tmp_path, capsys, failures_text):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "failures.json").write_text(failures_text)
    (out_dir / "clean_items.json").write_text("[]")
    retry_output = tmp_path / "retry_output.json"
    retry_output.write_text(json.dumps({"items": []}))

    code, err = _incorporate(out_dir, retry_output, capsys)

    assert code == 2
    assert "error: incorporate failed unexpectedly:" in err


# R13 - the skill must handle validate/incorporate exit 2 visibly, and Step 7.5's
# "final_payload.json absent" guard must point back to it (never read as
# "substrate not deployed").
_SKILL = Path(__file__).resolve().parent.parent / "src/engram/skills/claude/engram-sleep/SKILL.md"


def _skill_sections() -> tuple[str, str]:
    text = _SKILL.read_text()
    v0 = text.index("After all batch-summary fairy outputs are written, run validate")
    s75 = text.index("### Step 7.5")
    s8 = text.index("### Step 8")
    return text[v0:s75], text[s75:s8]


def test_r13_skill_has_explicit_exit_2_branch():
    import re

    validate_section, step75 = _skill_sections()
    assert re.search(
        r"Exit 2 = pipeline/validate error: STOP the batch-summary step, "
        r"record the error text verbatim in the dream record, and do NOT run Step 7\.5",
        validate_section,
    ), "validate section lacks the explicit exit-2 stop branch"
    # ...at the validate step AND inside the retry loop (incorporate's exit codes)
    loop = validate_section[validate_section.index("**Loop"):]
    assert re.search(r"exit(?: code)? 2", loop, re.I) and "STOP" in loop, (
        "retry loop does not interpret exit 2"
    )
    # Step 7.5's absent-file guard points back to the exit-2 branch
    assert re.search(r"exit 2", step75, re.I)
    assert "substrate not deployed" in step75
