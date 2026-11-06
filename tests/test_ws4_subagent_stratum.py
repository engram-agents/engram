"""Tests for the WS4 kit's opt-in sub-agent stratum (engram-alpha#2753,
the design owner's D11 ruling, contract items a to g).

Synthetic fixtures only, built in tmp_path. No real transcript, ledger or
home-directory file is read. Every id below is an obviously synthetic
fixture string (CI scrub gate: no real node ids, no real names).

Cells (numbering follows the dispatch spec):
  1. a sub-agent file where the old parsing yields zero calls now yields
     the fired call and a correct window.
  2. lookup: one match / no match / two matches / other session's dir.
  3. nested record (different agentId) stays excluded.
  4. blinding: chain block stripped; a head named after redaction flagged.
  5. header: fire id only.
  6. regression gate: top-level outputs byte-identical with and without
     --subagent-stratum; the subtree holds only the sub-agent fires.
  7. analyze.py: exploratory block, O1 only, no pooled O1, mixed input
     refused.
  8. (default path) is the existing test files passing unchanged -- run
     separately, not encoded here.
"""
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from tools.ws4 import analyze as ws4_analyze  # noqa: E402
from tools.ws4 import extract_spans as ws4_extract_spans  # noqa: E402
from tools.ws4.ledger import compute_fire_id, load_jsonl, write_jsonl  # noqa: E402
from tools.ws4.transcript import (  # noqa: E402
    build_span,
    compute_window,
    find_fired_tool_use,
    find_tripwire_attachment,
    load_transcript,
)

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

NUDGE_BLOCK = "[lesson-tripwire] Action-moment pattern matched — remember:\n  • some nudge text"
PRINCIPLES_BLOCK = (
    "[triggered principles]\n"
    "  2 of these 2 matches sit under head-fixture-1 (a short label)\n"
    "  ↑ head-fixture-1 — the head's full claim text"
)
AGENT_A = "aaaa1111"
AGENT_B = "bbbb2222"
AGENT_NESTED = "nest9999"


def _write_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _sub(rec, agent_id=AGENT_A):
    out = dict(rec)
    out["isSidechain"] = True
    out["agentId"] = agent_id
    return out


def _tool_use(tool_use_id, name, tool_input, ts, model=None):
    msg = {"role": "assistant", "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}]}
    if model:
        msg["model"] = model
    return {"type": "assistant", "timestamp": ts, "message": msg}


def _text(text, ts):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _result(tool_use_id, content, ts):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]},
    }


def _prompt(text, ts):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _attachment(tool_use_id, additional_context, ts):
    stdout = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": additional_context}})
    return {
        "type": "attachment",
        "timestamp": ts,
        "attachment": {
            "type": "hook_success",
            "hookName": "PreToolUse:Bash",
            "hookEvent": "PreToolUse",
            "toolUseID": tool_use_id,
            "stdout": stdout,
            "command": "python3 .../engram-lesson-tripwire-hook.py",
        },
    }


def _sub_agent_records(fire_id, agent_id=AGENT_A, nudge=None, model="fixture-model-x", tail_calls=2):
    """A whole sub-agent transcript: every record is isSidechain + agentId."""
    nudge = nudge if nudge is not None else NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK
    recs = [
        _prompt("do the fixture task", "2026-09-28T00:00:00Z"),
        _tool_use(fire_id, "Bash", {"command": "ls"}, "2026-09-28T00:00:01Z", model=model),
        _attachment(fire_id, nudge, "2026-09-28T00:00:01Z"),
        _result(fire_id, "ok", "2026-09-28T00:00:02Z"),
    ]
    for i in range(tail_calls):
        recs.append(_tool_use(f"{fire_id}_c{i}", "Bash", {"command": f"echo {i}"}, f"2026-09-28T00:00:0{3 + i}Z"))
        recs.append(_result(f"{fire_id}_c{i}", f"out{i}", f"2026-09-28T00:00:0{3 + i}Z"))
    recs.append(_text("all done", "2026-09-28T00:00:09Z"))
    return [_sub(r, agent_id) for r in recs]


def _main_records(fire_id, nudge=None):
    nudge = nudge if nudge is not None else NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK
    return [
        _tool_use(fire_id, "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
        _attachment(fire_id, nudge, "2026-09-28T00:00:00Z"),
        _tool_use("toolu_main_c0", "Bash", {"command": "echo main"}, "2026-09-28T00:00:01Z"),
    ]


def _row(transcript_path, tool_use_id, arm="chain", ts=1.0, heads=("head-fixture-1",)):
    return {
        "ts": ts, "ts_iso": "2026-09-28T00:00:00Z", "session_id": "s", "transcript_path": str(transcript_path),
        "tool_name": "Bash", "tool_use_id": tool_use_id, "nonce": tool_use_id, "draw_basis": "tool_use_id",
        "seed": "abc", "arm": arm, "shown_lesson_ids": ["lesson-fixture-A"], "cut_lesson_ids": [],
        "heads_by_lesson": {"lesson-fixture-A": list(heads)}, "nudge_chars": 10, "chain_chars": 10,
        "chain_rendered": True,
    }


def _add_sub_file(session_main_path, agent_id, records, agent_type="fixture:fixture-agent"):
    session_main_path = Path(session_main_path)
    sub_dir = session_main_path.parent / session_main_path.stem / "subagents"
    _write_jsonl(sub_dir / f"agent-{agent_id}.jsonl", records)
    (sub_dir / f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"agentType": agent_type, "description": "fixture", "toolUseId": "toolu_parent", "spawnDepth": 1}),
        encoding="utf-8",
    )


def _run(tmp_path, rows, out_name, stratum, sample=0):
    ledger = tmp_path / "ledger.jsonl"
    write_jsonl(ledger, rows)
    out = tmp_path / out_name
    argv = ["--date", "2026-09-28", "--out", str(out), "--ledger", str(ledger), "--sample", str(sample)]
    if stratum:
        argv.append("--subagent-stratum")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ws4_extract_spans.main(argv)
    assert rc == 0
    return out, buf.getvalue()


# --- cell 1 ----------------------------------------------------------------


def test_cell1_old_parsing_yields_zero_calls_on_a_subagent_file(tmp_path):
    path = tmp_path / "agent-aaaa1111.jsonl"
    _write_jsonl(path, _sub_agent_records("toolu_sub_fire"))
    records = load_transcript(path)
    # Main-transcript mode (the default) still excludes every sidechain record.
    assert find_fired_tool_use(records, "toolu_sub_fire", "Bash", None) is None


def test_cell1_subagent_mode_yields_the_fired_call_and_a_correct_window(tmp_path):
    path = tmp_path / "agent-aaaa1111.jsonl"
    _write_jsonl(path, _sub_agent_records("toolu_sub_fire"))
    records = load_transcript(path)
    fired = find_fired_tool_use(records, "toolu_sub_fire", "Bash", None, own_agent_id=AGENT_A)
    assert fired is not None
    assert fired.tool_use_id == "toolu_sub_fire"
    assert fired.record_idx == 1

    # K smaller than the available calls: closes on K.
    w2 = compute_window(records, fired, 1, own_agent_id=AGENT_A)
    assert (w2.close_reason, w2.calls_counted) == ("k_calls", 1)

    # K larger: the sub-agent's final text ends its turn -> the existing
    # third category (outbound_act), after both further calls.
    w6 = compute_window(records, fired, 6, own_agent_id=AGENT_A)
    assert (w6.close_reason, w6.calls_counted) == ("outbound_act", 2)
    assert w6.end_record_idx == len(records) - 1

    assert find_tripwire_attachment(records, fired, own_agent_id=AGENT_A) is not None
    assert find_tripwire_attachment(records, fired) is None  # main mode still blind to it


# --- cell 2 ----------------------------------------------------------------


def test_cell2_lookup_one_none_two_and_other_session(tmp_path):
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    main2 = proj / "sess2.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    _write_jsonl(main2, _main_records("toolu_main_fire2"))
    # sess1: unique id in one file; an id shared by two files.
    _add_sub_file(main1, AGENT_A, _sub_agent_records("toolu_one", AGENT_A))
    _add_sub_file(main1, AGENT_B, _sub_agent_records("toolu_dup", AGENT_B))
    _add_sub_file(main1, "cccc3333", _sub_agent_records("toolu_dup", "cccc3333"))
    # sess2 holds an id that sess1's row asks for: must NOT be found.
    _add_sub_file(main2, "dddd4444", _sub_agent_records("toolu_elsewhere", "dddd4444"))

    rows = [
        _row(main1, "toolu_main_fire", ts=1.0),
        _row(main1, "toolu_one", ts=2.0),
        _row(main1, "toolu_nowhere", ts=3.0),
        _row(main1, "toolu_dup", ts=4.0),
        _row(main1, "toolu_elsewhere", ts=5.0),
    ]
    fid = {r["tool_use_id"]: compute_fire_id(r) for r in rows}
    out, stdout = _run(tmp_path, rows, "out", stratum=True)

    sub = out / "subagent"
    assert [p.name for p in (sub / "spans").glob("*.md")] == [f"{fid['toolu_one']}.md"]
    unloc = {r["fire_id"]: r["reason"] for r in load_jsonl(sub / "unlocatable.jsonl")}
    assert unloc == {
        fid["toolu_nowhere"]: "call_not_located",
        fid["toolu_dup"]: "ambiguous_subagent",
        fid["toolu_elsewhere"]: "call_not_located",
    }
    # primary: the main-session fire is a span; all four others stay unlocatable there.
    assert [p.name for p in (out / "spans").glob("*.md")] == [f"{fid['toolu_main_fire']}.md"]
    assert {r["fire_id"] for r in load_jsonl(out / "unlocatable.jsonl")} == {
        fid[k] for k in ("toolu_one", "toolu_nowhere", "toolu_dup", "toolu_elsewhere")
    }
    assert "subagent stratum (exploratory):" in stdout


def test_cell2_row_without_tool_use_id_is_listed_not_looked_up(tmp_path):
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, [_text("nothing to fire", "2026-09-28T00:00:00Z")])  # no Bash call -> call_not_located
    _add_sub_file(main1, AGENT_A, _sub_agent_records("toolu_one", AGENT_A))
    row = _row(main1, None)
    row["nonce"] = "n-1"
    out, _ = _run(tmp_path, [row], "out", stratum=True)
    sub = out / "subagent"
    assert list((sub / "spans").glob("*.md")) == []
    assert [r["reason"] for r in load_jsonl(sub / "unlocatable.jsonl")] == ["call_not_located"]
    stratum = load_jsonl(sub / "stratum.jsonl")
    assert len(stratum) == 1
    assert stratum[0]["subagent_file"] is None and stratum[0]["agent_type"] is None and stratum[0]["model"] is None


# --- cell 3 ----------------------------------------------------------------


def test_cell3_nested_record_with_other_agent_id_stays_excluded(tmp_path):
    recs = _sub_agent_records("toolu_sub_fire", tail_calls=1)
    nested_push = _sub(_tool_use("toolu_nested", "Bash", {"command": "git push origin x"}, "2026-09-28T00:00:02Z"), AGENT_NESTED)
    no_agent = {"type": "assistant", "isSidechain": True, "timestamp": "2026-09-28T00:00:02Z",
                "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_noagent", "name": "Bash", "input": {"command": "gh pr create"}}]}}
    recs[3:3] = [nested_push, no_agent]  # between the fired call and the next own call
    path = tmp_path / "agent-aaaa1111.jsonl"
    _write_jsonl(path, recs)
    records = load_transcript(path)
    fired = find_fired_tool_use(records, "toolu_sub_fire", "Bash", None, own_agent_id=AGENT_A)
    window = compute_window(records, fired, 6, own_agent_id=AGENT_A)
    # the nested git push / gh pr create would close the window at call 1 if counted
    assert window.calls_counted == 1
    assert window.close_reason == "outbound_act"  # the own final text, not the nested push
    assert window.end_record_idx == len(records) - 1
    # a nested id is not findable as the acting agent's call
    assert find_fired_tool_use(records, "toolu_nested", "Bash", None, own_agent_id=AGENT_A) is None
    span = build_span(records, fired, window, "fire-x", 6, own_agent_id=AGENT_A)
    assert "git push" not in span and "gh pr create" not in span


# --- cells 4 and 5 ---------------------------------------------------------


def test_cell4_and_5_blinding_and_header(tmp_path):
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    sub_recs = _sub_agent_records("toolu_one", AGENT_A, model="fixture-model-x")
    _add_sub_file(main1, AGENT_A, sub_recs, agent_type="fixture:secret-agent-type")
    # second fire: the agent quotes the head AFTER redaction -> flagged
    leaky = _sub_agent_records("toolu_leak", AGENT_B, model="fixture-model-x")
    leaky.insert(-1, _sub(_text("I recall head-fixture-1 applies here", "2026-09-28T00:00:08Z"), AGENT_B))
    _add_sub_file(main1, AGENT_B, leaky, agent_type="fixture:secret-agent-type")

    rows = [_row(main1, "toolu_main_fire", ts=1.0), _row(main1, "toolu_one", ts=2.0), _row(main1, "toolu_leak", ts=3.0)]
    fid = {r["tool_use_id"]: compute_fire_id(r) for r in rows}
    out, _ = _run(tmp_path, rows, "out", stratum=True)
    sub = out / "subagent"

    span = (sub / "spans" / f"{fid['toolu_one']}.md").read_text(encoding="utf-8")
    assert "[triggered principles]" not in span
    assert "head-fixture-1" not in span
    assert "some nudge text" in span  # only the chain block was removed

    flags = {r["fire_id"]: r for r in load_jsonl(sub / "flags.jsonl")}
    assert flags[fid["toolu_one"]]["head_named_in_span"] is False
    assert flags[fid["toolu_leak"]]["head_named_in_span"] is True

    # cell 5: header carries the fire id only
    for p in (sub / "spans").glob("*.md"):
        text = p.read_text(encoding="utf-8")
        assert text.splitlines()[0] == f"# Fire {p.stem}"
        for forbidden in ("stratum", "secret-agent-type", "fixture-model-x", "agent-aaaa1111", "agent-bbbb2222",
                          "sidechain", AGENT_A, AGENT_B):
            assert forbidden not in text.lower(), forbidden
    # analyst-only facts live in stratum.jsonl, never in spans/
    stratum = {r["fire_id"]: r for r in load_jsonl(sub / "stratum.jsonl")}
    assert stratum[fid["toolu_one"]] == {
        "fire_id": fid["toolu_one"], "agent_type": "fixture:secret-agent-type",
        "model": "fixture-model-x", "subagent_file": "agent-aaaa1111.jsonl",
    }
    assert not list((sub / "spans").glob("*.jsonl"))
    key = load_jsonl(sub / "key.jsonl")
    assert all(set(r) == {"fire_id", "arm"} for r in key)


# --- cell 6 ----------------------------------------------------------------


def _tree_bytes(root: Path, skip=("subagent",)):
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in skip:
            continue
        if p.is_file():
            out[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_cell6_regression_gate_top_level_outputs_byte_identical(tmp_path):
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    main_recs = _main_records("toolu_main_a") + [_tool_use("toolu_main_b", "Bash", {"command": "ls"}, "2026-09-28T00:00:05Z"),
                                                 _attachment("toolu_main_b", NUDGE_BLOCK, "2026-09-28T00:00:05Z")]
    # a sidechain record in the main transcript must stay excluded in both runs
    main_recs.insert(2, _sub(_tool_use("toolu_side", "Bash", {"command": "git push"}, "2026-09-28T00:00:00Z"), AGENT_NESTED))
    _write_jsonl(main1, main_recs)
    _add_sub_file(main1, AGENT_A, _sub_agent_records("toolu_sub_a", AGENT_A))
    _add_sub_file(main1, AGENT_B, _sub_agent_records("toolu_sub_b", AGENT_B))
    rows = [
        _row(main1, "toolu_main_a", "chain", ts=1.0),
        _row(main1, "toolu_main_b", "control", ts=2.0),
        _row(main1, "toolu_sub_a", "chain", ts=3.0),
        _row(main1, "toolu_sub_b", "control", ts=4.0),
        _row(main1, "toolu_gone", "control", ts=5.0),
    ]
    fid = {r["tool_use_id"]: compute_fire_id(r) for r in rows}
    out_plain, stdout_plain = _run(tmp_path, rows, "out_plain", stratum=False)
    out_flag, stdout_flag = _run(tmp_path, rows, "out_flag", stratum=True)

    plain = _tree_bytes(out_plain)
    flagged = _tree_bytes(out_flag)
    assert plain == flagged
    assert set(plain) >= {"key.jsonl", "flags.jsonl", "unlocatable.jsonl", "sample.jsonl", "sample_summary.json"}
    assert not (out_plain / "subagent").exists()  # without the flag: no subtree at all

    # stdout: the flag run is the plain run plus a separate trailing block
    assert stdout_flag.startswith(stdout_plain)
    extra = stdout_flag[len(stdout_plain):]
    assert extra.lstrip("\n").startswith("subagent stratum (exploratory):")
    assert "stratum_rows == spans_out + unlocatable: 3 == 2 + 1" in extra

    # the subtree holds only the sub-agent fires (no main fire)
    sub = out_flag / "subagent"
    assert {p.stem for p in (sub / "spans").glob("*.md")} == {fid["toolu_sub_a"], fid["toolu_sub_b"]}
    assert {r["fire_id"] for r in load_jsonl(sub / "key.jsonl")} == {fid["toolu_sub_a"], fid["toolu_sub_b"]}
    assert {r["fire_id"] for r in load_jsonl(sub / "stratum.jsonl")} == {
        fid["toolu_sub_a"], fid["toolu_sub_b"], fid["toolu_gone"]}
    # primary denominators unchanged: the sub-agent fires stay unlocatable there
    assert {r["fire_id"] for r in load_jsonl(out_flag / "unlocatable.jsonl")} == {
        fid["toolu_sub_a"], fid["toolu_sub_b"], fid["toolu_gone"]}


def test_cell6b_sidechain_record_in_main_transcript_still_changes_nothing(tmp_path):
    """Main mode keeps its global isSidechain exclusion: a sidechain
    outbound act between the fire and the K-th call must not close the main
    window. (Guards the 'drop sidechain exclusion globally' mutant.)"""
    path = tmp_path / "main.jsonl"
    recs = [
        _tool_use("toolu_f", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
        _attachment("toolu_f", NUDGE_BLOCK, "2026-09-28T00:00:00Z"),
        _sub(_tool_use("toolu_s", "Bash", {"command": "git push"}, "2026-09-28T00:00:01Z"), AGENT_NESTED),
        _tool_use("toolu_c0", "Bash", {"command": "echo 0"}, "2026-09-28T00:00:02Z"),
        _tool_use("toolu_c1", "Bash", {"command": "echo 1"}, "2026-09-28T00:00:03Z"),
    ]
    _write_jsonl(path, recs)
    records = load_transcript(path)
    fired = find_fired_tool_use(records, "toolu_f", "Bash", None)
    window = compute_window(records, fired, 2)
    assert (window.close_reason, window.calls_counted) == ("k_calls", 2)
    span = build_span(records, fired, window, "fire-x", 2)
    assert "git push" not in span


# --- cell 7 ----------------------------------------------------------------


def _analyze_fixture(tmp_path, with_sub=True):
    """Primary: 3 chain (1 walked) + 2 control (all heeded) -> diff +1/3.
    Sub stratum: 1 chain heeded + 1 control walked -> diff -1.0.
    Pooled would be .25 - 1/3 = -0.0833..., present in neither."""
    rows, key, verdicts = [], [], []
    sub_rows, sub_key = [], []

    def add(ledger, keyl, n, arm, verdict, corr):
        r = _row(f"/x/{n}.jsonl", f"toolu_{n}", arm, ts=float(n))
        ledger.append(r)
        fid = compute_fire_id(r)
        keyl.append({"fire_id": fid, "arm": arm})
        verdicts.append({"fire_id": fid, "adjudicator": "adj", "verdict": verdict, "corrective_at": corr, "note": ""})
        return fid

    for n, arm, v, c in [(1, "chain", "walked_past", None), (2, "chain", "heeded", 1), (3, "chain", "heeded", 1),
                         (4, "control", "heeded", 1), (5, "control", "heeded", 1)]:
        add(rows, key, n, arm, v, c)
    for n, arm, v, c in [(11, "chain", "heeded", 1), (12, "control", "walked_past", None)]:
        add(sub_rows, sub_key, n, arm, v, c)

    ledger_path = tmp_path / "ledger.jsonl"
    write_jsonl(ledger_path, rows + sub_rows)
    verdicts_path = tmp_path / "verdicts.jsonl"
    write_jsonl(verdicts_path, verdicts)
    key_path = tmp_path / "out" / "key.jsonl"
    flags_path = tmp_path / "out" / "flags.jsonl"
    unloc_path = tmp_path / "out" / "unlocatable.jsonl"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(key_path, key)
    write_jsonl(flags_path, [])
    write_jsonl(unloc_path, [{"fire_id": k["fire_id"], "reason": "call_not_located"} for k in sub_key])
    sub_dir = tmp_path / "out" / "subagent"
    sub_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(sub_dir / "key.jsonl", sub_key)
    write_jsonl(sub_dir / "flags.jsonl", [])
    write_jsonl(sub_dir / "unlocatable.jsonl", [])
    write_jsonl(sub_dir / "stratum.jsonl", [
        {"fire_id": k["fire_id"], "agent_type": None, "model": None, "subagent_file": "agent-x.jsonl"} for k in sub_key])
    base = ["--seat", "S", "--ledger", str(ledger_path), "--verdicts", str(verdicts_path), "--key", str(key_path),
            "--flags", str(flags_path), "--no-sample-summary", "--unlocatable", str(unloc_path)]
    return base, sub_dir, rows, sub_rows, key, sub_key


def _analyze(argv):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            rc = ws4_analyze.main(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return rc, buf.getvalue(), err.getvalue()


def test_cell7_exploratory_block_o1_only_and_no_pooled_o1(tmp_path):
    base, sub_dir, rows, sub_rows, key, sub_key = _analyze_fixture(tmp_path)
    rc_plain, out_plain, _ = _analyze(base)
    assert rc_plain == 0
    assert "EXPLORATORY" not in out_plain  # option absent: today's report

    rc, out, err = _analyze(base + ["--subagent-dir", str(sub_dir)])
    assert rc == 0, err
    # the primary report is untouched: today's output is a strict prefix
    assert out.startswith(out_plain)
    block = out[len(out_plain):]
    assert "EXPLORATORY (added 2026-11-06 before any outcome was read): sub-agent stratum" in block
    assert "O1" in block and "K=1" in block  # O1 at K_PRIMARY plus the sweep
    assert "O2 is not reported for this stratum" in block
    assert "== O2" not in block
    assert block.count("arm_difference=-1.0") >= 1  # the stratum's own diff

    # no pooled O1 anywhere
    from tools.ws4.constants import K_PRIMARY
    seat_p = ws4_analyze.SeatInputs("S", rows, {k["fire_id"]: k for k in key}, {}, {})
    seat_s = ws4_analyze.SeatInputs("S", sub_rows, {k["fire_id"]: k for k in sub_key}, {}, {})
    # verdicts come from the real run; recompute the cross-stratum value from the same files
    verdicts = ws4_analyze.load_verdicts(tmp_path / "verdicts.jsonl")
    seat_p.verdicts = seat_s.verdicts = verdicts
    seat_p.ledger_rows = rows + sub_rows
    seat_p_only = ws4_analyze.compute_o1([seat_p], K_PRIMARY)["pooled"]["arm_difference"]
    seat_s.ledger_rows = rows + sub_rows
    sub_only = ws4_analyze.compute_o1([seat_s], K_PRIMARY)["pooled"]["arm_difference"]
    mixed = ws4_analyze.SeatInputs("S", rows + sub_rows, {**seat_p.key, **seat_s.key}, verdicts, {})
    cross = ws4_analyze.compute_o1([mixed], K_PRIMARY)["pooled"]["arm_difference"]
    assert cross not in (seat_p_only, sub_only)
    assert str(cross) not in out
    assert out.count("pooled arm_difference=") == 1  # the primary's own line only
    assert "pooled" not in block.lower()
    # the within-stratum all-seats line is labelled as such (round 2, nit 1)
    assert f"all seats, this stratum arm_difference={sub_only}" in block
    # no line in the block combines the two strata
    for line in block.splitlines():
        assert not ("primary" in line.lower() and "arm_difference" in line)


def test_cell7_mixed_inputs_exit_nonzero_naming_the_conflict(tmp_path):
    base, sub_dir, rows, sub_rows, key, sub_key = _analyze_fixture(tmp_path)

    # (a) sub-agent key rows passed as the primary key
    argv = list(base)
    argv[argv.index("--key") + 1] = str(sub_dir / "key.jsonl")
    rc, out, err = _analyze(argv + ["--subagent-dir", str(sub_dir)])
    assert rc != 0 and "EXPLORATORY" not in out
    assert "sub-agent" in err and "key" in err
    # even without --subagent-dir: a stratum file offered as primary is refused
    rc2, _, err2 = _analyze(argv)
    assert rc2 != 0 and "sub-agent" in err2

    # (b) one fire_id in both strata
    both = sub_dir.parent / "dup_sub"
    both.mkdir()
    write_jsonl(both / "key.jsonl", [key[0]])
    write_jsonl(both / "flags.jsonl", [])
    write_jsonl(both / "unlocatable.jsonl", [])
    write_jsonl(both / "stratum.jsonl", [{"fire_id": key[0]["fire_id"], "agent_type": None, "model": None, "subagent_file": None}])
    rc3, out3, err3 = _analyze(base + ["--subagent-dir", str(both)])
    assert rc3 != 0 and "EXPLORATORY" not in out3
    assert key[0]["fire_id"] in err3


# --- round 2 ---------------------------------------------------------------


def test_r2_item1_foreign_agent_id_record_is_not_a_lookup_match(tmp_path):
    """File X holds the id only in a record whose agentId is a foreign,
    nested agent's; file Y holds it under its own agentId. The lookup must
    return Y's span, not ambiguous_subagent."""
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    x_recs = _sub_agent_records("toolu_x_own", AGENT_B)
    x_recs.append(_sub(_tool_use("toolu_shared", "Bash", {"command": "ls"}, "2026-09-28T00:00:08Z"), AGENT_NESTED))
    _add_sub_file(main1, AGENT_B, x_recs)
    _add_sub_file(main1, AGENT_A, _sub_agent_records("toolu_shared", AGENT_A))
    row = _row(main1, "toolu_shared")
    fid = compute_fire_id(row)
    out, _ = _run(tmp_path, [row], "out", stratum=True)
    sub = out / "subagent"
    assert [p.name for p in (sub / "spans").glob("*.md")] == [f"{fid}.md"]
    assert load_jsonl(sub / "unlocatable.jsonl") == []
    assert [r["subagent_file"] for r in load_jsonl(sub / "stratum.jsonl")] == [f"agent-{AGENT_A}.jsonl"]


def _run_expect_refusal(tmp_path, rows, out_dir):
    ledger = tmp_path / "ledger.jsonl"
    write_jsonl(ledger, rows)
    argv = ["--date", "2026-09-28", "--out", str(out_dir), "--ledger", str(ledger), "--sample", "0",
            "--subagent-stratum"]
    err = io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        rc = ws4_extract_spans.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_r2_item2_second_run_into_same_out_is_refused_and_leaves_everything_untouched(tmp_path):
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    _add_sub_file(main1, AGENT_A, _sub_agent_records("toolu_one", AGENT_A))
    rows = [_row(main1, "toolu_main_fire", ts=1.0), _row(main1, "toolu_one", ts=2.0)]
    out, _ = _run(tmp_path, rows, "out", stratum=True)
    before = _tree_bytes(out, skip=())
    assert any(k.startswith("subagent/") for k in before)

    rc, stdout, stderr = _run_expect_refusal(tmp_path, rows, out)
    assert rc != 0
    assert str(out / "subagent") in stderr and "--out" in stderr
    assert stdout == ""  # nothing reported as done
    assert _tree_bytes(out, skip=()) == before  # subtree AND top level unchanged

    # a fresh out dir works
    fresh, _ = _run(tmp_path, rows, "out2", stratum=True)
    assert len(list((fresh / "subagent" / "spans").glob("*.md"))) == 1


def test_r2_item2_refusal_writes_nothing_even_into_a_new_out_with_a_stale_subtree(tmp_path):
    """Order: the check runs before any write, so a refused run leaves no
    half-written stratum -- not even a top-level file in an out dir that
    only holds a stale subtree."""
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    stale = tmp_path / "out" / "subagent" / "spans"
    stale.mkdir(parents=True)
    (stale / "stale.md").write_text("old span", encoding="utf-8")
    rc, _, stderr = _run_expect_refusal(tmp_path, [_row(main1, "toolu_main_fire")], tmp_path / "out")
    assert rc != 0 and "subagent" in stderr
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["subagent"]
    assert (stale / "stale.md").read_text(encoding="utf-8") == "old span"


def test_r2_item2_without_the_flag_a_second_run_is_unchanged(tmp_path):
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    rows = [_row(main1, "toolu_main_fire")]
    out, _ = _run(tmp_path, rows, "out", stratum=False)
    first = _tree_bytes(out, skip=())
    out_again, _ = _run(tmp_path, rows, "out", stratum=False)
    assert _tree_bytes(out_again, skip=()) == first


def test_r2_item3_dotdot_in_transcript_path_does_not_reach_another_sessions_dir(tmp_path):
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    main2 = proj / "sess2.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    _write_jsonl(main2, _main_records("toolu_main_fire2"))
    (proj / "sess1").mkdir(exist_ok=True)
    _add_sub_file(main2, "dddd4444", _sub_agent_records("toolu_elsewhere", "dddd4444"))
    planted = proj / "sess1" / ".." / "sess2.jsonl"  # resolves into sess2's directory
    row = _row(planted, "toolu_elsewhere")
    out, _ = _run(tmp_path, [row], "out", stratum=True)
    sub = out / "subagent"
    assert list((sub / "spans").glob("*.md")) == []
    assert [r["reason"] for r in load_jsonl(sub / "unlocatable.jsonl")] == ["call_not_located"]


def test_r2_item3_symlink_in_subagents_to_another_sessions_file_is_skipped(tmp_path):
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    main2 = proj / "sess2.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    _write_jsonl(main2, _main_records("toolu_main_fire2"))
    _add_sub_file(main2, "dddd4444", _sub_agent_records("toolu_elsewhere", "dddd4444"))
    link_dir = proj / "sess1" / "subagents"
    link_dir.mkdir(parents=True)
    # same basename, so the file's own agentId check cannot be what stops it
    (link_dir / "agent-dddd4444.jsonl").symlink_to(proj / "sess2" / "subagents" / "agent-dddd4444.jsonl")
    row = _row(main1, "toolu_elsewhere")
    out, _ = _run(tmp_path, [row], "out", stratum=True)
    sub = out / "subagent"
    assert list((sub / "spans").glob("*.md")) == []
    assert [r["reason"] for r in load_jsonl(sub / "unlocatable.jsonl")] == ["call_not_located"]


def test_r2_item3_ordinary_in_directory_file_is_still_found(tmp_path):
    """Control for the two containment cells: the guard must not reject
    honest files (a resolved path inside the resolved directory)."""
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    _add_sub_file(main1, AGENT_A, _sub_agent_records("toolu_one", AGENT_A))
    out, _ = _run(tmp_path, [_row(main1, "toolu_one")], "out", stratum=True)
    assert len(list((out / "subagent" / "spans").glob("*.md"))) == 1


# --- round 3: four behaviours pinned (cells only; the code was already right) -


def test_r3_r1_main_mode_final_text_without_later_prompt_closes_end_of_transcript(tmp_path):
    """The sub-agent final-text rule must not leak into main-transcript mode:
    a main span whose last record is the agent's final text, no later prompt,
    still closes end_of_transcript (registered-stratum bytes unchanged)."""
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    recs = [
        _tool_use("toolu_f", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z"),
        _attachment("toolu_f", NUDGE_BLOCK + "\n" + PRINCIPLES_BLOCK, "2026-09-28T00:00:00Z"),
        _result("toolu_f", "ok", "2026-09-28T00:00:01Z"),
        _tool_use("toolu_c0", "Bash", {"command": "echo 0"}, "2026-09-28T00:00:02Z"),
        _result("toolu_c0", "out0", "2026-09-28T00:00:02Z"),
        _text("all done", "2026-09-28T00:00:03Z"),
    ]
    _write_jsonl(main1, recs)
    records = load_transcript(main1)
    fired = find_fired_tool_use(records, "toolu_f", "Bash", None)
    k = ws4_extract_spans.MAX_K_SWEEP  # the CLI's default K
    window = compute_window(records, fired, k)
    assert (window.close_reason, window.calls_counted) == ("end_of_transcript", 1)
    assert window.end_record_idx == len(records) - 1

    # Through the CLI: the flag sidecar records the same close reason, with
    # and without --subagent-stratum, and the span bytes do not move.
    row = _row(main1, "toolu_f")
    fid = compute_fire_id(row)
    out_plain, _ = _run(tmp_path, [row], "out_plain", stratum=False)
    out_flag, _ = _run(tmp_path, [row], "out_flag", stratum=True)
    for out in (out_plain, out_flag):
        assert [r["close_reason"] for r in load_jsonl(out / "flags.jsonl")] == ["end_of_transcript"]
    span_plain = (out_plain / "spans" / f"{fid}.md").read_bytes()
    assert span_plain == (out_flag / "spans" / f"{fid}.md").read_bytes()
    assert span_plain == build_span(records, fired, window, fid, k).encode("utf-8")
    assert b"all done" in span_plain  # the final text is inside the span, as today


def test_r3_r2_only_call_not_located_rows_become_stratum_candidates(tmp_path):
    """A row whose main transcript is unreadable, and a row whose tripwire
    attachment is missing, must reach none of the stratum files -- even
    though a sub-agent file of the same session holds their tool_use_id."""
    proj = tmp_path / "proj"
    gone = proj / "sess_gone.jsonl"  # never written: unreadable
    noatt = proj / "sess_noatt.jsonl"
    _write_jsonl(noatt, [_tool_use("toolu_noatt", "Bash", {"command": "ls"}, "2026-09-28T00:00:00Z")])
    _add_sub_file(gone, AGENT_A, _sub_agent_records("toolu_gone", AGENT_A))
    _add_sub_file(noatt, AGENT_B, _sub_agent_records("toolu_noatt", AGENT_B))
    rows = [_row(gone, "toolu_gone", ts=1.0), _row(noatt, "toolu_noatt", ts=2.0)]
    out, _ = _run(tmp_path, rows, "out", stratum=True)

    reasons = sorted(r["reason"].split(":")[0] for r in load_jsonl(out / "unlocatable.jsonl"))
    assert reasons == ["transcript_unreadable", "tripwire_attachment_not_found"]  # the primary keeps both
    sub = out / "subagent"
    for name in ("stratum.jsonl", "key.jsonl", "unlocatable.jsonl"):
        assert load_jsonl(sub / name) == [], name
    assert list((sub / "spans").glob("*.md")) == []


def test_r3_r3_run_without_the_keyword_creates_no_subagent_dir(tmp_path):
    """run() is a public entry: its own default must be off, not just the CLI's."""
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    _add_sub_file(main1, AGENT_A, _sub_agent_records("toolu_one", AGENT_A))
    rows = [_row(main1, "toolu_main_fire", ts=1.0), _row(main1, "toolu_one", ts=2.0)]
    ledger = tmp_path / "ledger.jsonl"
    write_jsonl(ledger, rows)

    out = tmp_path / "out_run"
    report = ws4_extract_spans.run("2026-09-28", ws4_extract_spans.MAX_K_SWEEP, str(out), str(ledger), sample_n=0)
    assert not (out / "subagent").exists()
    assert "subagent" not in report

    # and its outputs are exactly the CLI-without-flag outputs
    out_cli, _ = _run(tmp_path, rows, "out_cli", stratum=False)
    assert _tree_bytes(out, skip=()) == _tree_bytes(out_cli, skip=())


def test_r3_refusal_looks_at_any_entry_not_only_spans(tmp_path):
    """A subagent/ holding only a stale stratum.jsonl (no spans/) is refused:
    exit 2, nothing written, the stale file untouched."""
    proj = tmp_path / "proj"
    main1 = proj / "sess1.jsonl"
    _write_jsonl(main1, _main_records("toolu_main_fire"))
    stale_dir = tmp_path / "out" / "subagent"
    stale_dir.mkdir(parents=True)
    (stale_dir / "stratum.jsonl").write_text('{"fire_id": "stale"}\n', encoding="utf-8")
    before = _tree_bytes(tmp_path / "out", skip=())
    rc, stdout, stderr = _run_expect_refusal(tmp_path, [_row(main1, "toolu_main_fire")], tmp_path / "out")
    assert rc == 2
    assert str(stale_dir) in stderr and "--out" in stderr
    assert stdout == ""
    assert not (stale_dir / "spans").exists()
    assert _tree_bytes(tmp_path / "out", skip=()) == before
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["subagent"]
