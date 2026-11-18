"""Contract tests for the ENGRAM model-family gate (#4596): the write block, the next-turn warning,
the shared reader, and the generated write-tool list.

Contract-first (CLAUDE.md "Contract-first tests"): every cell below was written BEFORE the implementation
and run red against the tree without it.  The spec is /home/agents-shared/halley/4596-gate-spec.md
(SPEC v1, section 3 names C1a..C12; the numbers below follow it).

Design principle: the harness detects and code writes the text, so a session on the wrong model never
speaks in the agent's voice.  The texts in this file are the spec's 2.1 / 2.2 / 2.3, written out here
independently of the hooks, so a drifting hook text fails loudly.

   R  shared reader model_of_call            C1a dormant                  C1b config anomalies
   C2 MATCH from the calling entry           C3 MISMATCH from the calling entry
   C4 every UNKNOWN code (unmapped a-d)      C5 polling                   C6 budget
   C7 read tools never blocked               C8 one source for the list   C9 both prefixes
   C10 next-turn warning                     C11 deployed layout          C12 mutation controls (PR body)
   Round 2 (spec section 7): C13 helper call found in its own file   C14 helper fallback   C15 missing / unreadable /
   invalid helper ids   C16 agent specs that can write pin no model   C17 the parent file is never read for a helper
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO / "src" / "engram"
HOOK_DIR = SRC_ENGRAM / "hooks" / "claude"
PRETOOL_FILE = HOOK_DIR / "engram-model-gate-pretool.py"
PROMPT_FILE = HOOK_DIR / "engram-model-gate-prompt.py"
GATELIB_FILE = HOOK_DIR / "_model_gatelib.py"
HOOKS_JSON = SRC_ENGRAM / "hooks" / "hooks.json"
SERVER_PY = SRC_ENGRAM / "server.py"
TOOL_LIST_JSON = SRC_ENGRAM / "model_gate_write_tools.json"
GENERATOR_FILE = SRC_ENGRAM / "tools" / "gen_model_gate_write_tools.py"
PREFIXES = ("mcp__engram__", "mcp__plugin_engram_engram__")

import engram_signature as sig  # noqa: E402

SELF = "anthropic:opus"
THIS_CALL = "This call"
PREVIOUS = "The previous reply in this session"


# ------------------------------------------------------------------ texts (spec section 2)

def _tail(self_lineage, home):
    return (f"Nothing was written. To continue: relaunch the session on a `{self_lineage}` model. If the "
            f"change of model family is deliberate, that is the human's decision: change `self_lineage` "
            f"in `{home}/config.json`. Reading memory still works.")


def deny_text(source, model_id, lineage, self_lineage, home):
    return (f"ENGRAM write blocked by the model-family gate. {source} came from model `{model_id}` "
            f"(family `{lineage}`); this ENGRAM memory is locked to `{self_lineage}`. "
            + _tail(self_lineage, home))


def deny_unmapped_text(source, model_id, self_lineage, home):
    return (f"ENGRAM write blocked by the model-family gate. {source} came from model `{model_id}`, "
            f"which the gate does not recognise as family `{self_lineage}`. " + _tail(self_lineage, home))


def deny_unmapped_c_text(source, model_id, self_lineage, home):
    """Branches (c) and (d) deny: remedies in the order relaunch, table row, human's lineage decision.
    (Round 9: branch (d) carries this same text; branch (a) keeps `deny_unmapped_text`.)"""
    return (f"ENGRAM write blocked by the model-family gate. {source} came from model `{model_id}`, "
            f"which the gate does not recognise as family `{self_lineage}`. Nothing was written. To continue: "
            f"relaunch the session on a `{self_lineage}` model. If `{model_id}` is your own vendor's "
            f"`{self_lineage}` model and the gate does not know it yet, add its exact id to the model table "
            f"(`_NON_CLAUDE_MODEL_TABLE` in `engram_trust.py`, a reviewed change). If the change of model "
            f"family is deliberate, that is the human's decision: change `self_lineage` in "
            f"`{home}/config.json`. Reading memory still works.")


def unverified_text(code):
    return (f"ENGRAM model-family gate: this write went through UNVERIFIED (`{code}`): the gate could "
            f"not confirm which model made the call.")


def warning_text(model_id, lineage, self_lineage):
    return (f"ENGRAM model-family gate: the last reply in this session came from model `{model_id}` "
            f"(family `{lineage}`), but this ENGRAM memory is locked to `{self_lineage}`. ENGRAM writes "
            f"are blocked while this holds. Per the identity rule: stop autonomous and loop work, and "
            f"tell the human which model this is and which family the memory is locked to. Relaunching "
            f"on `{self_lineage}`, or changing `self_lineage` in the config, is the human's decision.")


# ------------------------------------------------------------------ helpers

def asst(model, text="hello"):
    return {"type": "assistant", "message": {"role": "assistant", "model": model,
                                              "content": [{"type": "text", "text": text}]}}


def call_row(model, tool_use_id, name="mcp__engram__engram_add_observation", extra_blocks=()):
    blocks = [{"type": "text", "text": "ok"}, *extra_blocks,
              {"type": "tool_use", "id": tool_use_id, "name": name, "input": {}}]
    msg = {"role": "assistant", "content": blocks}
    if model is not None:
        msg["model"] = model
    return {"type": "assistant", "message": msg}


def result_row(tool_use_id):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": "done"}]}}


def attach(model_id, name="Some Model"):
    return {"type": "attachment", "attachment": {"type": "model",
                                                  "identity": {"modelId": model_id, "marketingName": name}}}


def turn_ctx(model):
    return {"type": "turn_context", "payload": {"model": model}}


def user(text="hi"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def write_jsonl(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def append_jsonl(path: Path, row) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
        fh.flush()


def set_lineage(h: Path, value):
    cfg = {} if value is None else {"self_lineage": value}
    (h / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("ENGRAM_HOME", str(h))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    set_lineage(h, SELF)
    return h


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hook(isolate_hook_loads):
    return _load(PRETOOL_FILE, "engram_model_gate_pretool_under_test")


@pytest.fixture()
def prompt(isolate_hook_loads):
    return _load(PROMPT_FILE, "engram_model_gate_prompt_under_test")


@pytest.fixture()
def fast(hook, monkeypatch):
    """A short poll budget, for cells whose point is the FALLBACK (the real 1.0 s is pinned in C6)."""
    monkeypatch.setattr(hook, "BUDGET_S", 0.12)
    monkeypatch.setattr(hook, "STEP_S", 0.01)
    return hook


def pay(tool="mcp__engram__engram_add_observation", tid="toolu_01", tpath=None, session="s1"):
    p = {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": tool, "tool_use_id": tid,
         "tool_input": {}}
    if tpath is not None:
        p["transcript_path"] = str(tpath)
    return p


def out(res):
    """(permissionDecision | 'allow-context', text) of a pretool decision (None result = silent allow)."""
    if res is None:
        return None, ""
    o = res["hookSpecificOutput"]
    assert o["hookEventName"] == "PreToolUse"
    if "permissionDecision" in o:
        return o["permissionDecision"], o["permissionDecisionReason"]
    return "allow-context", o["additionalContext"]


def server_tools():
    """{registered tool name: readOnlyHint value or None}, read from server.py's decorators by this
    test's OWN parse (independent of the generator)."""
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    found = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in n.decorator_list:
            if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "tool"
                    and isinstance(d.func.value, ast.Name) and d.func.value.id == "mcp"):
                continue
            kw = {k.arg: k.value for k in d.keywords}
            name = n.name
            if isinstance(kw.get("name"), ast.Constant):
                name = kw["name"].value
            ro = None
            ann = kw.get("annotations")
            if isinstance(ann, ast.Dict):
                for k, v in zip(ann.keys, ann.values):
                    if isinstance(k, ast.Constant) and k.value == "readOnlyHint" and isinstance(v, ast.Constant):
                        ro = v.value
            found[name] = ro
    return found


def run_hook(h, payload, hook_file, plugin_root=None, cwd=None, raw_stdin=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = str(h)
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload)
    return subprocess.run([sys.executable, str(hook_file)], input=stdin, text=True, capture_output=True,
                          env=env, cwd=cwd, timeout=60)


def copy_runtime(root: Path) -> None:
    """What the build puts at the plugin root: the runtime modules, the data file, tools/."""
    for f in list(SRC_ENGRAM.glob("*.py")) + list(SRC_ENGRAM.glob("*.json")):
        shutil.copy2(f, root / f.name)
    shutil.copytree(SRC_ENGRAM / "tools", root / "tools", ignore=shutil.ignore_patterns("__pycache__"))


def copy_hooks_flat(root: Path) -> None:
    (root / "hooks").mkdir(parents=True, exist_ok=True)
    for f in HOOK_DIR.glob("*.py"):
        shutil.copy2(f, root / "hooks" / f.name)


# ====================================================================== R: shared reader

def test_r01_finds_the_calling_row_and_returns_its_model(tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), call_row("claude-sonnet-5-5", "toolu_A")])
    assert sig.model_of_call(t, "toolu_A", 0.2, 0.01) == ("claude-sonnet-5-5", "calling_entry", None)


def test_r02_the_id_must_be_a_tool_use_block_of_an_assistant_row(tmp_path):
    """The bytes prefilter hits the tool_result row and a longer id; neither is the calling entry."""
    rows = [asst("claude-opus-5-5"), call_row("claude-sonnet-5-5", "toolu_10"), result_row("toolu_1")]
    t = write_jsonl(tmp_path / "t.jsonl", rows)
    mid, source, reason = sig.model_of_call(t, "toolu_1", 0.1, 0.01)
    assert (mid, source, reason) == ("claude-sonnet-5-5", "fallback_newest_row", None)  # fell back; not the _10 row


def test_r03_the_row_appended_late_is_found_by_polling(tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])
    threading.Timer(0.15, append_jsonl, (t, call_row("claude-sonnet-5-5", "toolu_B"))).start()
    assert sig.model_of_call(t, "toolu_B", 1.0, 0.01) == ("claude-sonnet-5-5", "calling_entry", None)


def test_r04_budget_expiry_falls_back_to_the_newest_model_row(tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), user()])
    t0 = time.monotonic()
    assert sig.model_of_call(t, "toolu_never", 0.2, 0.01) == ("claude-opus-5-5", "fallback_newest_row", None)
    assert 0.2 <= time.monotonic() - t0 < 0.2 + 0.4


def test_r05_a_calling_row_larger_than_the_window_cannot_be_found_and_ends_in_the_fallback(tmp_path):
    big = call_row("claude-sonnet-5-5", "toolu_BIG", extra_blocks=[{"type": "text", "text": "x" * 300_000}])
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), big])
    mid, source, reason = sig.model_of_call(t, "toolu_BIG", 0.1, 0.01)
    assert source == "fallback_newest_row" and mid is None and reason == "window_exceeded"


def test_r06_a_calling_row_with_no_usable_model_is_no_model_row_at_once(tmp_path):
    for model in (None, "<synthetic>", "  "):
        t = write_jsonl(tmp_path / "t.jsonl", [user(), call_row(model, "toolu_C")])
        t0 = time.monotonic()
        assert sig.model_of_call(t, "toolu_C", 5.0, 0.01) == (None, "calling_entry", "no_model_row")
        assert time.monotonic() - t0 < 1.0, "a found row without a model must not wait out the budget"


def test_r07_nothing_found_and_no_row_at_all_is_budget_expired_no_row(tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [user()])
    assert sig.model_of_call(t, "toolu_never", 0.1, 0.01) == (None, "fallback_newest_row", "budget_expired_no_row")


def test_r08_never_raises_and_every_failure_is_a_reason(tmp_path):
    assert sig.model_of_call(tmp_path / "nope.jsonl", "toolu_x", 0.1, 0.01) == (
        None, "fallback_newest_row", "transcript_not_found")
    assert sig.model_of_call(tmp_path, "toolu_x", 0.1, 0.01) == (None, "fallback_newest_row", "transcript_unreadable")
    assert sig.model_of_call(None, "toolu_x", 0.1, 0.01)[2] == "transcript_path_missing"
    assert sig.model_of_call("", "toolu_x", 0.1, 0.01)[2] == "transcript_path_missing"
    for junk in (123, object()):
        assert sig.model_of_call(junk, "toolu_x", 0.1, 0.01)[0] is None
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])
    assert sig.model_of_call(t, None, 0.1, 0.01) == ("claude-opus-5-5", "fallback_newest_row", None)
    assert sig.model_of_call(t, "", 0.1, 0.01) == ("claude-opus-5-5", "fallback_newest_row", None)
    mixed = write_jsonl(tmp_path / "m.jsonl", [asst("claude-opus-5-5"), turn_ctx("gpt-6.1-sol")])
    assert sig.model_of_call(mixed, "toolu_x", 0.1, 0.01) == (None, "fallback_newest_row", "mixed_harness")
    # a nonsense budget and step: no raise, no wait, the fallback answer
    assert sig.model_of_call(t, "toolu_x", "bad", None) == ("claude-opus-5-5", "fallback_newest_row", None)


def test_r09_unmapped_fallback_id_is_returned_with_its_reason(tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("mystery-1")])
    assert sig.model_of_call(t, "toolu_x", 0.1, 0.01) == ("mystery-1", "fallback_newest_row", "unmapped_model")


# -- the tail reader kept from #4613 (the window rules the fallback stands on)

def test_r10_read_model_tail_window_rules(tmp_path):
    a = asst("claude-opus-5-5")
    t = write_jsonl(tmp_path / "t.jsonl", [a, user("p" * 3000), asst("claude-sonnet-5-5")])
    full = t.stat().st_size
    assert sig.read_model_tail(t, full).model_id == "claude-sonnet-5-5"
    res = sig.read_model_tail(t, len(json.dumps(asst("claude-sonnet-5-5"))) + 10)
    assert res.model_id == "claude-sonnet-5-5"
    res = sig.read_model_tail(t, 50)
    assert res.model_id is None and res.reason == "window_exceeded"
    assert sig.read_model_tail(tmp_path / "nope", 100).reason == "transcript_not_found"
    assert sig.read_model_tail(tmp_path, 100).reason == "transcript_unreadable"


def _first_line_decides(path, total):
    first = json.dumps(asst("claude-opus-5-5")) + "\n"
    pad = total - len(first.encode()) - len(json.dumps(user("")) + "\n")
    assert pad >= 0
    path.write_text(first + json.dumps(user("p" * pad)) + "\n", encoding="utf-8")
    assert path.stat().st_size == total
    return path


def test_r11_window_boundary_exactly_the_bound_reads_the_first_line_whole(tmp_path):
    n = sig.read_model_tail(_first_line_decides(tmp_path / "a.jsonl", 4096), 4096)
    assert (n.model_id, n.reason) == ("claude-opus-5-5", None)


def test_r12_window_boundary_one_byte_over_discards_the_partial_first_line(tmp_path):
    n = sig.read_model_tail(_first_line_decides(tmp_path / "b.jsonl", 4097), 4096)
    assert (n.model_id, n.reason) == (None, "window_exceeded")


def _boundary_transcript(path, calling_plus_newest):
    """[filler][calling row, sonnet, tool_use toolu_EDGE][newest row, opus] with len(calling) + len(newest)
    == ``calling_plus_newest`` bytes, so the file size is ``len(filler) + calling_plus_newest`` and the window
    start (size - CALL_WINDOW_BYTES) falls at a chosen byte of the calling row."""
    newest = json.dumps(asst("claude-opus-5-5")) + "\n"
    def calling_of(pad):
        return json.dumps(call_row("claude-sonnet-5-5", "toolu_EDGE", extra_blocks=[{"type": "text", "text": "x" * pad}]))

    calling = calling_of(calling_plus_newest - len(calling_of(0) + "\n") - len(newest))
    path.write_text(json.dumps(user("f")) + "\n" + calling + "\n" + newest, encoding="utf-8")
    return path


def test_r15_the_calling_scan_window_edge_is_exact_through_model_of_call(tmp_path):
    """The window the CALLING-ROW scan reads (``_read_window``) is a copy of ``read_model_tail``'s, and r11/r12
    pin only the tail reader.  Here the calling row begins at byte s of the file and the window starts at
    s - 1 (the newline before it): the row is found whole.  Mutation: a window 100 bytes smaller starts inside
    the row, the row is lost, the fallback answers (opus) -> red."""
    w = sig.CALL_WINDOW_BYTES
    t = _boundary_transcript(tmp_path / "a.jsonl", w - 1)
    assert t.stat().st_size - w == len(json.dumps(user("f")) + "\n") - 1
    assert sig.model_of_call(t, "toolu_EDGE", 0.0, 0.01) == ("claude-sonnet-5-5", "calling_entry", None)


def test_r16_the_calling_scan_discards_the_first_line_of_a_truncated_window(tmp_path):
    """One byte more and the window starts exactly at the calling row's first byte: a window that does not
    start at offset 0 drops its first line (the partial-line rule), so the row is not found and the fallback
    answers.  Mutation: keep the first line -> the row is found (sonnet) -> red."""
    w = sig.CALL_WINDOW_BYTES
    t = _boundary_transcript(tmp_path / "b.jsonl", w)
    assert t.stat().st_size - w == len(json.dumps(user("f")) + "\n")
    assert sig.model_of_call(t, "toolu_EDGE", 0.0, 0.01) == ("claude-opus-5-5", "fallback_newest_row", None)


def test_r17_two_rows_with_one_tool_use_id_the_newest_wins(tmp_path):
    """A transcript can carry one id twice (a resumed or replayed turn).  The newest matching row is the
    call being made.  Mutation: scan oldest first -> the older row's model (sonnet) -> red."""
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_DUP"), user(),
                                           call_row("claude-opus-5-5", "toolu_DUP")])
    assert sig.model_of_call(t, "toolu_DUP", 0.0, 0.01) == ("claude-opus-5-5", "calling_entry", None)


def test_r13_tail_and_whole_file_parsers_agree_on_small_transcripts(tmp_path):
    cases = {
        "opus": [asst("claude-opus-5-5")],
        "attach_over_asst": [attach("claude-fable-5-2", "Fable"), asst("claude-opus-5-5")],
        "newest_asst": [asst("claude-opus-5-5"), asst("claude-sonnet-5-5")],
        "synthetic_skipped": [asst("claude-sonnet-5-5"), asst("<synthetic>")],
        "codex": [turn_ctx("gpt-6.1-sol")],
        "mixed": [asst("claude-opus-5-5"), turn_ctx("gpt-6.1-sol")],
        "unmapped": [asst("mystery-1")],
        "no_rows": [user()],
        "placeholder_only": [asst("<synthetic>")],
    }
    for name, rows in cases.items():
        t = write_jsonl(tmp_path / f"{name}.jsonl", rows)
        try:
            whole = ("ok", sig._read_model(t))
        except sig._Refusal as r:
            whole = ("refused", r.code)
        res = sig.read_model_tail(t, 1 << 20)
        tail = ("ok", (res.model_id, res.marketing_name)) if res.reason is None else ("refused", res.reason)
        if whole[0] == "refused":
            expect = {"no_usable_model": "no_model_row"}.get(whole[1], whole[1])
            assert tail == ("refused", expect), (name, whole, tail)
        else:
            assert tail == whole, (name, whole, tail)


def test_r14_unknown_refusal_code_maps_to_internal_error(tmp_path, monkeypatch):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])

    def boom(rows, transcript):
        raise sig._Refusal("model", "something new", code="brand_new_code")

    monkeypatch.setattr(sig, "_model_from_rows", boom)
    assert sig.read_model_tail(t, 1 << 20).reason == "internal_error"


# ====================================================================== C1a / C1b: config

@pytest.mark.parametrize("cfg", ["absent_file", {}, {"self_lineage": ""}, {"self_lineage": "   "},
                                 {"self_lineage": None}, {"other": 1}])
def test_c1a_dormant_is_silent_for_any_model(hook, home, tmp_path, cfg):
    cp = home / "config.json"
    cp.unlink()
    if cfg != "absent_file":
        cp.write_text(json.dumps(cfg), encoding="utf-8")
    for model in ("claude-sonnet-5-5", "claude-opus-5-5", "mystery-1"):
        t = write_jsonl(tmp_path / "t.jsonl", [call_row(model, "toolu_01")])
        assert hook.decide(pay(tpath=t), home) is None, (cfg, model)


@pytest.mark.parametrize("body", ["{not json", "", "[1, 2]", "null"])
def test_c1b_config_not_json_or_not_an_object_is_unverified_never_silent(hook, home, tmp_path, body):
    (home / "config.json").write_text(body, encoding="utf-8")
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("config_unreadable"))


def test_c1b_config_that_is_a_directory_is_unreadable(hook, home, tmp_path):
    (home / "config.json").unlink()
    (home / "config.json").mkdir()
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("config_unreadable"))


@pytest.mark.parametrize("bad", ["opus", "Anthropic:Opus", "anthropic:", ":opus", "a:b:c", 5, ["x"], True])
def test_c1b_malformed_self_lineage_is_unverified_never_silent(hook, home, tmp_path, bad):
    set_lineage(home, bad)
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("self_lineage_malformed"))


def test_c1b_hook_lineage_pattern_equals_cores():
    import engram_core
    src = GATELIB_FILE.read_text(encoding="utf-8")
    m = re.search(r"_LINEAGE_RE\s*=\s*re\.compile\(r?[\"'](.+?)[\"']\)", src)
    assert m, "the hook must hold the provider:family pattern (dormancy is decided before the runtime loads)"
    assert m.group(1) == engram_core._LINEAGE_RE.pattern


# ====================================================================== C2 / C3

@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-opus-4-8", "claude-opus-6-0[1m]",
                                   "claude-opus-4-1-20250805"])
def test_c2_match_from_the_calling_entry_is_silent_any_version(hook, home, tmp_path, model):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-sonnet-5-5"), call_row(model, "toolu_01")])
    assert hook.decide(pay(tpath=t), home) is None


def test_c3_mismatch_from_the_calling_entry_denies_with_text_2_1(hook, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), call_row("claude-sonnet-5-5", "toolu_01")])
    assert out(hook.decide(pay(tpath=t), home)) == (
        "deny", deny_text(THIS_CALL, "claude-sonnet-5-5", "anthropic:sonnet", SELF, home))


def test_c3_non_anthropic_family_both_ways(hook, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("gpt-6.1-sol", "toolu_01")])
    assert out(hook.decide(pay(tpath=t), home)) == (
        "deny", deny_text(THIS_CALL, "gpt-6.1-sol", "openai:gpt", SELF, home))
    set_lineage(home, "openai:gpt")
    assert hook.decide(pay(tpath=t), home) is None


# ====================================================================== C4: every UNKNOWN code

def test_c4_transcript_path_missing(hook, home):
    assert out(hook.decide(pay(), home)) == ("allow-context", unverified_text("transcript_path_missing"))


def test_c4_transcript_not_found(hook, home, tmp_path):
    assert out(hook.decide(pay(tpath=tmp_path / "nope.jsonl"), home)) == (
        "allow-context", unverified_text("transcript_not_found"))


def test_c4_transcript_unreadable(hook, home, tmp_path):
    assert out(hook.decide(pay(tpath=tmp_path), home)) == ("allow-context", unverified_text("transcript_unreadable"))


@pytest.mark.parametrize("model", [None, "<synthetic>"])
def test_c4_no_model_row(hook, home, tmp_path, model):
    t = write_jsonl(tmp_path / "t.jsonl", [user(), call_row(model, "toolu_01")])
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("no_model_row"))


def test_c4_budget_expired_no_row(fast, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [user()])
    assert out(fast.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("budget_expired_no_row"))


def test_c4_window_exceeded(fast, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), user("p" * 300_000)])
    assert out(fast.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("window_exceeded"))


def test_c4_internal_error_from_the_reader_and_from_a_raise(fast, home, tmp_path, monkeypatch):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])
    fast.decide(pay(tpath=t), home)  # loads the runtime
    monkeypatch.setattr(fast, "model_of_call", lambda *a, **k: (None, "fallback_newest_row", "internal_error"))
    assert out(fast.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("internal_error"))

    def boom(*a, **k):
        raise RuntimeError("forced")

    monkeypatch.setattr(fast, "model_of_call", boom)
    assert out(fast.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("internal_error"))


def test_c4_mixed_harness_denies(fast, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), turn_ctx("gpt-6.1-sol")])
    kind, text = out(fast.decide(pay(tpath=t), home))
    assert kind == "deny"
    assert text.startswith("ENGRAM write blocked by the model-family gate.")
    assert "mixed" in text and f"`{SELF}`" in text and "Nothing was written." in text
    assert text.endswith(_tail(SELF, home))


# -- unmapped_model: branches a-d, by the provider _parse_model_id reports and the family token

def _call(tmp_path, model):
    return write_jsonl(tmp_path / "t.jsonl", [call_row(model, "toolu_01")])


def test_c4_unmapped_a_known_other_provider_denies(hook, home, tmp_path):
    set_lineage(home, "openai:gpt")
    t = _call(tmp_path, "claude-opus-5 beta")  # unmapped; parser says anthropic != openai
    assert out(hook.decide(pay(tpath=t), home)) == (
        "deny", deny_unmapped_text(THIS_CALL, "claude-opus-5 beta", "openai:gpt", home))


def test_c4_unmapped_a_shared_family_word_across_providers_denies(hook, home, tmp_path):
    set_lineage(home, "deepseek:flash")
    t = _call(tmp_path, "claude-flash-9 beta")  # contains the family word `flash`, but it is Anthropic's id
    assert out(hook.decide(pay(tpath=t), home)) == (
        "deny", deny_unmapped_text(THIS_CALL, "claude-flash-9 beta", "deepseek:flash", home))


def test_c4_unmapped_b_same_provider_family_token_present_allows_unverified(hook, home, tmp_path):
    t = _call(tmp_path, "claude-opus-5 beta")
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("unmapped_model"))


def test_c4_unmapped_c_provider_not_recognised_family_token_present_first_token_foreign_denies(hook, home, tmp_path):
    """Rule U: `mystery` is not one of anthropic:opus's own first tokens, so (c) denies with the new text."""
    t = _call(tmp_path, "mystery-opus-9")
    assert out(hook.decide(pay(tpath=t), home)) == (
        "deny", deny_unmapped_c_text(THIS_CALL, "mystery-opus-9", SELF, home))


def test_c4_unmapped_c_provider_not_recognised_own_first_token_allows_unverified(hook, home, tmp_path):
    t = _call(tmp_path, "anthropic-opus-6")
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("unmapped_model"))


@pytest.mark.parametrize("model", ["mystery-9", "claude-sonnet-5 beta"])
def test_c4_unmapped_d_family_token_absent_denies(hook, home, tmp_path, model):
    t = _call(tmp_path, model)
    assert out(hook.decide(pay(tpath=t), home)) == ("deny", deny_unmapped_c_text(THIS_CALL, model, SELF, home))


@pytest.mark.parametrize("lineage,model", [("openai:gpt", "o5-mini"), (SELF, "gemini-2.5-flash")])
def test_c4_unmapped_d_deny_carries_the_three_remedies_in_order(hook, home, tmp_path, lineage, model):
    """Round 9: branch (d) (family token absent): own vendor with no family token, and a foreign id with
    none.  The exact text AND the order relaunch < table row < `self_lineage`."""
    set_lineage(home, lineage)
    t = _call(tmp_path, model)
    got = out(hook.decide(pay(tpath=t), home))
    assert got == ("deny", deny_unmapped_c_text(THIS_CALL, model, lineage, home))
    text = got[1]
    assert (text.index("relaunch the session") < text.index("add its exact id to the model table")
            < text.index("change `self_lineage` in"))


def test_c4_unmapped_a_deny_does_not_carry_the_table_row_sentence(hook, home, tmp_path):
    """Round 9: branch (a) keeps the old text; the parser knows the id is another vendor's, so a table row
    cannot be the fix."""
    set_lineage(home, "openai:gpt")
    t = _call(tmp_path, "claude-opus-5 beta")
    got = out(hook.decide(pay(tpath=t), home))
    assert got == ("deny", deny_unmapped_text(THIS_CALL, "claude-opus-5 beta", "openai:gpt", home))
    assert "add its exact id to the model table" not in got[1]


def test_c4_unmapped_family_token_is_a_whole_hyphen_token_not_a_substring(hook, home, tmp_path):
    t = _call(tmp_path, "mystery-opusx-9")
    assert out(hook.decide(pay(tpath=t), home))[0] == "deny"


def test_c4_unmapped_from_the_fallback_uses_the_previous_reply_wording(fast, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("mystery-9")])
    assert out(fast.decide(pay(tpath=t), home)) == ("deny", deny_unmapped_c_text(PREVIOUS, "mystery-9", SELF, home))
    t2 = write_jsonl(tmp_path / "t2.jsonl", [asst("mystery-opus-9")])
    assert out(fast.decide(pay(tpath=t2), home)) == (
        "deny", deny_unmapped_c_text(PREVIOUS, "mystery-opus-9", SELF, home))
    t3 = write_jsonl(tmp_path / "t3.jsonl", [asst("anthropic-opus-6")])
    assert out(fast.decide(pay(tpath=t3), home)) == ("allow-context", unverified_text("unmapped_model"))


# Rule U measured column: (lineage, id, expected).  Expected kinds: allow (silent), notice, deny_c (branch c
# text), deny_d (branch d text, the same as c since round 9), deny_mapped:<lineage> (the id maps to another lineage).
RULE_U_ROWS = [
    ("deepseek:flash", "deepseek-v4.1-flash", "allow"),
    ("deepseek:flash", "deepseek-v5-flash", "notice"),
    ("deepseek:flash", "ds-v5-flash", "deny_c"),
    ("deepseek:flash", "deepseek-v5-pro", "deny_d"),
    ("deepseek:flash", "gemini-2.5-flash", "deny_c"),
    ("deepseek:flash", "qwen3-flash", "deny_c"),
    ("deepseek:flash", "kimi-k3-flash", "deny_c"),
    ("deepseek:flash", "claude-opus-5-5", "deny_mapped:anthropic:opus"),
    ("anthropic:opus", "claude-opus-5-5", "allow"),
    ("anthropic:opus", "claude-opus-9", "allow"),
    ("anthropic:opus", "anthropic-opus-6", "notice"),
    ("anthropic:opus", "opus-6", "deny_c"),
    ("anthropic:opus", "claude-sonnet-5-5", "deny_mapped:anthropic:sonnet"),
    ("anthropic:opus", "llama-opus-70b", "deny_c"),
    ("anthropic:opus", "opus-mini", "deny_c"),
    ("anthropic:opus", "gemini-2.5-flash", "deny_d"),
    ("openai:gpt", "gpt-6.1-sol", "allow"),
    ("openai:gpt", "gpt-7", "notice"),
    ("openai:gpt", "gpt-6.5-mini", "notice"),
    ("openai:gpt", "o5-mini", "deny_d"),
    ("openai:gpt", "gpt-neox-20b", "notice"),
    ("openai:gpt", "chatgpt-4o-latest", "deny_d"),
    ("openai:gpt", "deepseek-v4.1-flash", "deny_mapped:deepseek:flash"),
    ("openai:gpt", "qwen-gpt-7b", "deny_c"),
    ("acme:whiz", "acme-whiz-1", "notice"),
    ("acme:whiz", "acme-whiz-2-mini", "notice"),
    ("acme:whiz", "whiz-1", "deny_c"),
    ("acme:whiz", "gemini-2.5-whiz", "deny_c"),
    ("acme:whiz", "claude-whiz-1", "deny_mapped:anthropic:whiz"),
]


def _rule_u_expect(kind, lineage, model, home):
    if kind == "allow":
        return None, ""
    if kind == "notice":
        return "allow-context", unverified_text("unmapped_model")
    if kind == "deny_c":
        return "deny", deny_unmapped_c_text(THIS_CALL, model, lineage, home)
    if kind == "deny_d":
        return "deny", deny_unmapped_c_text(THIS_CALL, model, lineage, home)
    mapped = kind.split(":", 1)[1]
    return "deny", deny_text(THIS_CALL, model, mapped, lineage, home)


@pytest.mark.parametrize("lineage,model,kind", RULE_U_ROWS)
def test_c4_rule_u_matches_the_measured_column(hook, home, tmp_path, lineage, model, kind):
    set_lineage(home, lineage)
    t = write_jsonl(tmp_path / "t.jsonl", [call_row(model, "toolu_01")])
    assert out(hook.decide(pay(tpath=t), home)) == _rule_u_expect(kind, lineage, model, home)


def test_c4_rule_u_column_has_29_rows():
    assert len(RULE_U_ROWS) == 29


def test_c4_rule_u_named_residual_an_own_first_token_passes_with_the_notice(hook, home, tmp_path):
    """Pins the ACCEPTED residual: `gpt-neox-20b` on openai:gpt has the family token `gpt` and the seat's own
    first token `gpt`, so it passes with the notice although it is not an OpenAI GPT.  Narrow, never silent.
    A later change to this outcome must be a deliberate one: this cell goes red with it."""
    set_lineage(home, "openai:gpt")
    t = _call(tmp_path, "gpt-neox-20b")
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("unmapped_model"))


def test_c4_rule_u_a_lineage_with_no_table_rows_trusts_only_its_provider_token(hook, home, tmp_path):
    """`acme:whiz` has no table row: own first tokens are just {acme}."""
    set_lineage(home, "acme:whiz")
    t = _call(tmp_path, "acme-whiz-1")
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("unmapped_model"))
    t2 = write_jsonl(tmp_path / "t2.jsonl", [call_row("whiz-1", "toolu_01")])
    assert out(hook.decide(pay(tpath=t2), home)) == (
        "deny", deny_unmapped_c_text(THIS_CALL, "whiz-1", "acme:whiz", home))


def test_c4_rule_u_another_lineages_table_token_is_not_own(hook, home, tmp_path):
    """`gpt-flash-x` on deepseek:flash: `flash` is the family token and the provider is not recognised, but
    the first token `gpt` belongs to openai:gpt's table ids, not to deepseek:flash's own set, so it is (c)
    foreign and denies with the branch-(c) text.  Pins that own first tokens are per lineage."""
    set_lineage(home, "deepseek:flash")
    t = _call(tmp_path, "gpt-flash-x")
    assert out(hook.decide(pay(tpath=t), home)) == (
        "deny", deny_unmapped_c_text(THIS_CALL, "gpt-flash-x", "deepseek:flash", home))


def test_c4_rule_u_branch_c_deny_text_orders_the_remedies(hook, home, tmp_path):
    """relaunch / agent definition, then the table row, then the human's `self_lineage` decision."""
    t = _call(tmp_path, "mystery-opus-9")
    main = out(hook.decide(pay(tpath=t), home))[1]
    assert (main.index("relaunch the session") < main.index("add its exact id to the model table")
            < main.index("change `self_lineage` in"))
    parent, _ = sub_layout(tmp_path, [], [call_row("mystery-opus-9", "toolu_01")])
    helper = out(hook.decide(sub_pay(parent), home))[1]
    assert (helper.index("agent definition must run") < helper.index("add its exact id to the model table")
            < helper.index("Changing `self_lineage` is the human's decision"))


def test_c4_rule_u_own_first_tokens_are_read_from_the_table_not_hard_coded(hook, home, tmp_path, monkeypatch):
    set_lineage(home, "deepseek:flash")
    t = _call(tmp_path, "zz-flash-2")
    assert out(hook.decide(pay(tpath=t), home)) == (
        "deny", deny_unmapped_c_text(THIS_CALL, "zz-flash-2", "deepseek:flash", home))
    monkeypatch.setitem(sys.modules["engram_trust"]._NON_CLAUDE_MODEL_TABLE, "zz-flash-1",
                        ("deepseek", "flash", "1"))
    assert out(hook.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("unmapped_model"))


def test_c4_every_allow_unverified_output_carries_text_2_2_with_its_code(fast, home, tmp_path):
    """The never-silent rule, over every allow code at once: output is exactly 2.2 and is no decision."""
    cases = {
        "transcript_path_missing": pay(),
        "transcript_not_found": pay(tpath=tmp_path / "nope"),
        "transcript_unreadable": pay(tpath=tmp_path),
        "budget_expired_no_row": pay(tpath=write_jsonl(tmp_path / "a.jsonl", [user()])),
        "no_model_row": pay(tpath=write_jsonl(tmp_path / "b.jsonl", [call_row(None, "toolu_01")])),
        "window_exceeded": pay(tpath=write_jsonl(tmp_path / "c.jsonl", [user("p" * 300_000)])),
    }
    for code, payload in cases.items():
        res = fast.decide(payload, home)
        assert res is not None, code
        assert set(res["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}, code
        assert res["hookSpecificOutput"]["additionalContext"] == unverified_text(code), code


# ====================================================================== C5 polling / C6 budget

def test_c5_a_row_appended_after_the_hook_starts_is_found(hook, home, tmp_path):
    """Opus before, sonnet is the call: found => deny 'This call'; a single read would see opus and allow."""
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])
    w = threading.Timer(0.25, append_jsonl, (t, call_row("claude-sonnet-5-5", "toolu_01")))
    w.start()
    try:
        res = hook.decide(pay(tpath=t), home)
    finally:
        w.join()
    assert out(res) == ("deny", deny_text(THIS_CALL, "claude-sonnet-5-5", "anthropic:sonnet", SELF, home))


def test_c5_a_row_that_never_appears_falls_back(fast, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])
    assert out(fast.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("fallback_newest_row"))


def test_c5_fallback_match_is_never_silent_and_fallback_mismatch_denies_previous_reply(fast, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), user()])
    assert out(fast.decide(pay(tpath=t), home)) == ("allow-context", unverified_text("fallback_newest_row"))
    t2 = write_jsonl(tmp_path / "t2.jsonl", [asst("claude-sonnet-5-5"), user()])
    assert out(fast.decide(pay(tpath=t2), home)) == (
        "deny", deny_text(PREVIOUS, "claude-sonnet-5-5", "anthropic:sonnet", SELF, home))


def test_c6_constants_and_the_budget_bound_when_the_row_never_appears(hook, home, tmp_path):
    assert (hook.BUDGET_S, hook.STEP_S) == (1.0, 0.02)
    margin = 0.5  # stated margin: one poll plus interpreter scheduling noise on a loaded host
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])
    hook.decide(pay(tpath=t), home)  # warm: the first call imports the runtime
    t0 = time.monotonic()
    res = hook.decide(pay(tpath=t), home)
    dt = time.monotonic() - t0
    assert out(res)[0] == "allow-context"
    assert hook.BUDGET_S <= dt < hook.BUDGET_S + margin, dt


# ====================================================================== C7 / C8 / C9: tools

def test_c8_every_server_tool_has_an_explicit_boolean_read_only_hint():
    tools = server_tools()
    assert tools, "no @mcp.tool found: the test's own parse is broken"
    bad = sorted(n for n, ro in tools.items() if not isinstance(ro, bool))
    assert bad == [], f"tools without an explicit readOnlyHint: {bad}"


def _load_generator():
    return _load(GENERATOR_FILE, "gen_model_gate_write_tools_under_test")


def test_c8_json_list_equals_the_annotations():
    tools = server_tools()
    writes = sorted(n for n, ro in tools.items() if ro is False)
    data = json.loads(TOOL_LIST_JSON.read_text(encoding="utf-8"))
    assert data["write_tools"] == writes
    assert tuple(data["prefixes"]) == PREFIXES
    assert len(writes) >= 1 and set(writes).isdisjoint(n for n, ro in tools.items() if ro is True)


def _matcher():
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    found = []
    for group in cfg["hooks"]["PreToolUse"]:
        for h in group["hooks"]:
            if "engram-model-gate-pretool.py" in h["command"]:
                found.append((group["matcher"], h))
    assert len(found) == 1, "the pretool gate must be registered exactly once"
    return found[0]


def test_c8_hooks_json_matcher_equals_the_annotations_under_both_prefixes():
    tools = server_tools()
    matcher, h = _matcher()
    rx = re.compile(matcher)
    for name, ro in tools.items():
        for p in PREFIXES:
            assert bool(rx.search(p + name)) == (ro is False), (p + name)
    # nothing else: no bare-name, other-server or built-in tool starts the hook
    for other in ("Bash", "Edit", "mcp__other__engram_add_observation", "engram_add_observation",
                  "mcp__engram__", "mcp__engram__engram_add_observation_extra_x"):
        assert not rx.search(other), other


def test_c8_generator_output_equals_the_committed_files():
    gen = _load_generator()
    data, matcher = gen.build_outputs(SERVER_PY.read_text(encoding="utf-8"))
    assert data == json.loads(TOOL_LIST_JSON.read_text(encoding="utf-8"))
    assert matcher == _matcher()[0]
    assert gen.main(["--check"]) == 0


def test_c8_an_unannotated_tool_fails_the_generator():
    gen = _load_generator()
    src = (
        "@mcp.tool(name='engram_a', annotations={'readOnlyHint': True})\ndef a(): pass\n"
        "@mcp.tool(name='engram_b', annotations={'title': 'x'})\ndef b(): pass\n")
    with pytest.raises(ValueError, match="engram_b"):
        gen.build_outputs(src)
    with pytest.raises(ValueError, match="engram_c"):
        gen.build_outputs("@mcp.tool(name='engram_c')\ndef c(): pass\n")
    with pytest.raises(ValueError, match="engram_d"):
        gen.build_outputs("@mcp.tool(name='engram_d', annotations={'readOnlyHint': 'no'})\ndef d(): pass\n")


def test_c8_a_drifted_committed_file_fails_check(tmp_path):
    gen = _load_generator()
    bad_json = tmp_path / "list.json"
    bad_json.write_text(json.dumps({"schema": 1, "prefixes": list(PREFIXES), "write_tools": ["engram_x"]}))
    assert gen.main(["--check", "--json", str(bad_json)]) != 0


def test_c7_read_tools_never_blocked_even_in_a_mismatch_session(hook, home, tmp_path):
    tools = server_tools()
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    reads = sorted(n for n, ro in tools.items() if ro is True)
    assert reads
    for name in reads:
        for p in PREFIXES:
            assert hook.decide(pay(tool=p + name, tpath=t), home) is None, p + name
    for other in ("Bash", "Read", "mcp__other__thing"):
        assert hook.decide(pay(tool=other, tpath=t), home) is None, other


def test_c9_both_prefixes_get_the_same_decision_for_every_write_tool(hook, home, tmp_path):
    tools = server_tools()
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    writes = sorted(n for n, ro in tools.items() if ro is False)
    for name in writes:
        a, b = (out(hook.decide(pay(tool=p + name, tpath=t), home)) for p in PREFIXES)
        assert a == b == ("deny", deny_text(THIS_CALL, "claude-sonnet-5-5", "anthropic:sonnet", SELF, home)), name
    # and a MATCH session writes freely under both
    t2 = write_jsonl(tmp_path / "t2.jsonl", [call_row("claude-opus-5-5", "toolu_01")])
    for p in PREFIXES:
        assert hook.decide(pay(tool=p + writes[0], tpath=t2), home) is None


def test_c9_a_missing_tool_name_is_gated_not_waved_through(hook, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    p = pay(tpath=t)
    del p["tool_name"]
    assert out(hook.decide(p, home))[0] == "deny"


# ====================================================================== C10: next-turn warning

def test_c10_mismatch_injects_text_2_3(prompt, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), asst("claude-sonnet-5-5"), user()])
    ctx = prompt.decide({"session_id": "s", "transcript_path": str(t)}, home)
    assert ctx == warning_text("claude-sonnet-5-5", "anthropic:sonnet", SELF)


@pytest.mark.parametrize("rows", [
    [asst("claude-opus-5-5")],                             # MATCH
    [asst("claude-sonnet-5-5"), asst("claude-opus-5-5")],  # newest wins: back on family
    [asst("mystery-1")],                                   # UNKNOWN: unmapped
    [user()],                                              # UNKNOWN: no model row
    [asst("claude-opus-5-5"), turn_ctx("gpt-6.1-sol")],    # UNKNOWN: mixed harness
])
def test_c10_match_and_unknown_inject_nothing(prompt, home, tmp_path, rows):
    t = write_jsonl(tmp_path / "t.jsonl", rows)
    assert prompt.decide({"session_id": "s", "transcript_path": str(t)}, home) is None


def test_c10_dormant_malformed_and_missing_transcript_inject_nothing(prompt, home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-sonnet-5-5")])
    p = {"session_id": "s", "transcript_path": str(t)}
    for val in (None, "", "opus", 5):
        set_lineage(home, val)
        assert prompt.decide(p, home) is None, val
    (home / "config.json").unlink()
    assert prompt.decide(p, home) is None
    set_lineage(home, SELF)
    assert prompt.decide({"session_id": "s"}, home) is None
    assert prompt.decide({"session_id": "s", "transcript_path": str(tmp_path / "nope")}, home) is None
    assert prompt.decide({}, home) is None


def test_c10_process_emits_the_block_through_emit_context_and_exits_zero(home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-sonnet-5-5")])
    done = run_hook(home, {"session_id": "s", "transcript_path": str(t)}, PROMPT_FILE)
    assert (done.returncode, done.stderr) == (0, "")
    o = json.loads(done.stdout)["hookSpecificOutput"]
    assert o["hookEventName"] == "UserPromptSubmit"
    assert warning_text("claude-sonnet-5-5", "anthropic:sonnet", SELF) in o["additionalContext"]
    t2 = write_jsonl(tmp_path / "t2.jsonl", [asst("claude-opus-5-5")])
    quiet = run_hook(home, {"session_id": "s", "transcript_path": str(t2)}, PROMPT_FILE)
    assert (quiet.returncode, quiet.stdout, quiet.stderr) == (0, "", "")
    bad = run_hook(home, None, PROMPT_FILE, raw_stdin="{{{")
    assert (bad.returncode, bad.stdout) == (0, "")


# ====================================================================== process shape + registration

def test_pretool_process_denies_with_json_on_stdout_and_exits_zero(home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    done = run_hook(home, pay(tpath=t), PRETOOL_FILE)
    assert (done.returncode, done.stderr) == (0, "")
    assert out(json.loads(done.stdout)) == (
        "deny", deny_text(THIS_CALL, "claude-sonnet-5-5", "anthropic:sonnet", SELF, home))
    t2 = write_jsonl(tmp_path / "t2.jsonl", [call_row("claude-opus-5-5", "toolu_01")])
    ok = run_hook(home, pay(tpath=t2), PRETOOL_FILE)
    assert (ok.returncode, ok.stdout, ok.stderr) == (0, "", "")


def test_pretool_process_survives_bad_stdin(home):
    done = run_hook(home, None, PRETOOL_FILE, raw_stdin="{{{")
    assert done.returncode == 0
    # a write tool with an unreadable payload is not waved through silently
    assert unverified_text("transcript_path_missing") in done.stdout


def test_registration_prompt_hook_in_user_prompt_submit_and_no_stop_hook():
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))

    def cmds(ev):
        return [h["command"] for g in cfg["hooks"][ev] for h in g["hooks"]]

    assert sum("engram-model-gate-prompt.py" in c for c in cmds("UserPromptSubmit")) == 1
    assert not any("engram-model-gate-stop" in c for ev in cfg["hooks"] for c in cmds(ev))
    assert not (HOOK_DIR / "engram-model-gate-stop.py").exists()


# ====================================================================== C11: deployed layout

def test_c11_flattened_layout_without_plugin_root_gates_and_warns(home, tmp_path):
    root = tmp_path / "deployed"
    root.mkdir()
    copy_runtime(root)
    copy_hooks_flat(root)
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    done = run_hook(home, pay(tpath=t), root / "hooks" / PRETOOL_FILE.name, cwd=str(tmp_path))
    assert (done.returncode, done.stderr) == (0, "")
    assert out(json.loads(done.stdout)) == (
        "deny", deny_text(THIS_CALL, "claude-sonnet-5-5", "anthropic:sonnet", SELF, home))
    other_prefix = "mcp__plugin_engram_engram__" + sorted(n for n, ro in server_tools().items() if ro is False)[0]
    ok = run_hook(home, pay(tool=other_prefix, tpath=t), root / "hooks" / PRETOOL_FILE.name, cwd=str(tmp_path))
    assert out(json.loads(ok.stdout))[0] == "deny"
    warn = run_hook(home, {"session_id": "s", "transcript_path": str(t)},
                    root / "hooks" / PROMPT_FILE.name, cwd=str(tmp_path))
    # the call-row fixture has a sonnet model row, so the warning fires from the deployed tree too
    assert warning_text("claude-sonnet-5-5", "anthropic:sonnet", SELF) in json.loads(warn.stdout)[
        "hookSpecificOutput"]["additionalContext"]


def test_c11_plugin_root_env_is_preferred(home, tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    copy_runtime(root)
    copy_hooks_flat(root)
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    done = run_hook(home, pay(tpath=t), PRETOOL_FILE, plugin_root=root)
    assert out(json.loads(done.stdout))[0] == "deny"


def test_c11_the_walk_stops_at_the_tree_root(home, tmp_path):
    """A decoy engram_signature.py ABOVE the plugin root must never be used (#3983): the gate then
    cannot read the model, and says so (allow, never silent), instead of reading through the decoy."""
    outer = tmp_path / "outer"
    root = outer / "plugin"
    root.mkdir(parents=True)
    copy_hooks_flat(root)
    shutil.copy2(SRC_ENGRAM / "engram_core.py", root / "engram_core.py")
    shutil.copy2(TOOL_LIST_JSON, root / TOOL_LIST_JSON.name)
    shutil.copy2(SRC_ENGRAM / "engram_signature.py", outer / "engram_signature.py")
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    done = run_hook(home, pay(tpath=t), root / "hooks" / PRETOOL_FILE.name, cwd=str(tmp_path))
    assert (done.returncode, done.stderr) == (0, "")
    assert out(json.loads(done.stdout)) == ("allow-context", unverified_text("internal_error"))


# -- Round 3 fold 1: an import failure is never silent (only dormancy is)

def _hook_without_helper(tmp_path):
    """The hook file alone in a directory: `_model_gatelib` (and `_hooklib`) cannot be imported."""
    d = tmp_path / "lonely"
    d.mkdir()
    shutil.copy2(PRETOOL_FILE, d / PRETOOL_FILE.name)
    return d / PRETOOL_FILE.name


def _hook_with_hooklib_only(tmp_path):
    """The hook plus `_hooklib` in a directory: `_model_gatelib` cannot be imported, `_hooklib` can (round 4)."""
    d = tmp_path / "hooklib_only"
    d.mkdir()
    shutil.copy2(PRETOOL_FILE, d / PRETOOL_FILE.name)
    shutil.copy2(HOOK_DIR / "_hooklib.py", d / "_hooklib.py")
    return d / PRETOOL_FILE.name


def test_r3_c18a_helper_module_missing_is_unverified_gate_import_failed(home, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    done = run_hook(home, pay(tpath=t), _hook_without_helper(tmp_path))
    assert done.returncode == 0
    assert out(json.loads(done.stdout)) == ("allow-context", unverified_text("gate_import_failed"))
    assert "helper import failed" in done.stderr   # the stderr line is kept


@pytest.mark.parametrize("cfg", [None, {}, {"self_lineage": ""}, {"self_lineage": "  "}, {"self_lineage": None}])
def test_r3_c18b_helper_module_missing_with_a_dormant_config_is_silent(home, tmp_path, cfg):
    if cfg is None:
        (home / "config.json").unlink()
    else:
        (home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    done = run_hook(home, pay(), _hook_with_hooklib_only(tmp_path))
    assert (done.returncode, done.stdout) == (0, "")


@pytest.mark.parametrize("body", ["{not json", "[]", '{"self_lineage": 5}'])
def test_r3_c18c_helper_module_missing_with_an_unusable_config_is_unverified(home, tmp_path, body):
    (home / "config.json").write_text(body, encoding="utf-8")
    done = run_hook(home, pay(), _hook_with_hooklib_only(tmp_path))
    assert out(json.loads(done.stdout)) == ("allow-context", unverified_text("gate_import_failed"))


# -- Round 4: no raw ENGRAM_HOME read; the import-failure path asks `_hooklib.engram_home`

def test_r4_a_helper_import_fails_hooklib_importable_dormant_config_is_silent(home, tmp_path):
    (home / "config.json").write_text("{}", encoding="utf-8")
    done = run_hook(home, pay(), _hook_with_hooklib_only(tmp_path))
    assert (done.returncode, done.stdout) == (0, "")


def test_r4_b_helper_import_fails_hooklib_importable_set_lineage_is_unverified(home, tmp_path):
    (home / "config.json").write_text(json.dumps({"self_lineage": SELF}), encoding="utf-8")
    done = run_hook(home, pay(), _hook_with_hooklib_only(tmp_path))
    assert done.returncode == 0
    assert out(json.loads(done.stdout)) == ("allow-context", unverified_text("gate_import_failed"))


def test_r4_c_both_imports_fail_is_unverified_even_with_a_dormant_config(home, tmp_path):
    (home / "config.json").write_text("{}", encoding="utf-8")
    done = run_hook(home, pay(), _hook_without_helper(tmp_path))
    assert out(json.loads(done.stdout)) == ("allow-context", unverified_text("gate_import_failed"))


def test_r4_d_relative_engram_home_on_the_import_failure_path_is_unverified(home, tmp_path):
    hook = _hook_with_hooklib_only(tmp_path)
    (tmp_path / "rel").mkdir()
    (tmp_path / "rel" / "config.json").write_text("{}", encoding="utf-8")   # dormant, if it were read
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = "rel"
    done = subprocess.run([sys.executable, str(hook)], input=json.dumps(pay()), text=True, capture_output=True,
                          env=env, cwd=str(tmp_path), timeout=60)
    assert done.returncode == 0
    assert out(json.loads(done.stdout)) == ("allow-context", unverified_text("gate_import_failed"))


# -- Round 5: the MAIN path agrees with the import-failure path on a relative ENGRAM_HOME.  `_hooklib` exits
# (SystemExit) at import on a relative home; that exit must be recorded as an import failure, not escape main().

def _run_real_hook_with_home(tmp_path, home_value):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = home_value
    return subprocess.run([sys.executable, str(PRETOOL_FILE)], input=json.dumps(pay()), text=True,
                          capture_output=True, env=env, cwd=str(tmp_path), timeout=60)


def test_r5_a_relative_engram_home_on_the_main_path_is_unverified_not_silent(tmp_path):
    (tmp_path / "rel").mkdir()
    (tmp_path / "rel" / "config.json").write_text(json.dumps({"self_lineage": SELF}), encoding="utf-8")
    done = _run_real_hook_with_home(tmp_path, "rel")
    assert done.returncode == 0
    assert out(json.loads(done.stdout)) == ("allow-context", unverified_text("gate_import_failed"))
    # round 6: ONE stderr line in total, `_hooklib`'s refusal naming the value (the #3863 rule); the hook's own
    # "helper import failed" line is not added on top, and the dormancy re-import does not repeat the refusal
    assert [ln for ln in done.stderr.splitlines() if ln.strip()] == [
        "engram hook: ENGRAM_HOME must be an absolute path, got 'rel'; doing nothing"], done.stderr


def test_r5_b_absolute_dormant_home_on_the_main_path_stays_silent(tmp_path):
    h = tmp_path / "abs"
    h.mkdir()
    (h / "config.json").write_text("{}", encoding="utf-8")
    done = _run_real_hook_with_home(tmp_path, str(h))
    assert (done.returncode, done.stdout) == (0, "")


# ====================================================================== ROUND 2: helper-agent (sub-agent) calls
# Spec section 7 (R2.1-R2.5; Kepler's rulings, Umwelt 27654).  A sub-agent's PreToolUse payload carries
# `agent_id` and `agent_type` beside the PARENT's `session_id` and `transcript_path`.  The sub-agent's own
# transcript is <dirname(transcript_path)>/<session_id>/subagents/agent-<agent_id>.jsonl and records the
# call under the sub-agent's own model.  Contracts C13-C17, written before the implementation.

AGENT_ID = "a1b2c3d4e5f6"
AGENT_TYPE = "engram:engram-dream-master"


def helper_deny_text(source, agent_type, agent_id, model_id, lineage, self_lineage):
    lead = "This call came from a" if source == THIS_CALL else "The previous reply of a"
    return (f"ENGRAM write blocked by the model-family gate. {lead} helper agent (`{agent_type}`, id "
            f"`{agent_id}`) running on model `{model_id}` (family `{lineage}`); this ENGRAM memory is locked "
            f"to `{self_lineage}`. Nothing was written. The main session's own model is not affected by this "
            f"block. To let this helper write, its agent definition must run on a `{self_lineage}` model; "
            f"that is the human's decision.")


def helper_deny_unmapped_text(source, agent_type, agent_id, model_id, self_lineage):
    lead = "This call came from a" if source == THIS_CALL else "The previous reply of a"
    return (f"ENGRAM write blocked by the model-family gate. {lead} helper agent (`{agent_type}`, id "
            f"`{agent_id}`) running on model `{model_id}`, which the gate does not recognise as family "
            f"`{self_lineage}`. Nothing was written. The main session's own model is not affected by this "
            f"block. To let this helper write, its agent definition must run on a `{self_lineage}` model; "
            f"that is the human's decision.")


def helper_deny_unmapped_c_text(source, agent_type, agent_id, model_id, self_lineage):
    lead = "This call came from a" if source == THIS_CALL else "The previous reply of a"
    return (f"ENGRAM write blocked by the model-family gate. {lead} helper agent (`{agent_type}`, id "
            f"`{agent_id}`) running on model `{model_id}`, which the gate does not recognise as family "
            f"`{self_lineage}`. Nothing was written. The main session's own model is not affected by this "
            f"block. To let this helper write, its agent definition must run on a `{self_lineage}` model. If "
            f"`{model_id}` is your own vendor's `{self_lineage}` model and the gate does not know it yet, add "
            f"its exact id to the model table (`_NON_CLAUDE_MODEL_TABLE` in `engram_trust.py`, a reviewed "
            f"change). Changing `self_lineage` is the human's decision.")


def sub_pay(tpath, session="sess-1", agent_id=AGENT_ID, agent_type=AGENT_TYPE, tid="toolu_01", **kw):
    p = pay(tid=tid, tpath=tpath, session=session)
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    p.update(kw)
    return p


def sub_layout(tmp_path, parent_rows, sub_rows, session="sess-1", agent_id=AGENT_ID):
    """(parent_transcript, subagent_transcript) in the harness layout."""
    parent = write_jsonl(tmp_path / f"{session}.jsonl", parent_rows)
    d = tmp_path / session / "subagents"
    d.mkdir(parents=True)
    sub = write_jsonl(d / f"agent-{agent_id}.jsonl", sub_rows)
    return parent, sub


# -- C13: the calling row is found in the sub-agent's own file

def test_c13_subagent_calling_row_match_is_silent(hook, home, tmp_path):
    parent, _ = sub_layout(tmp_path, [asst("claude-sonnet-5-5")],
                           [asst("claude-opus-5-5"), call_row("claude-opus-5-5", "toolu_01")])
    assert hook.decide(sub_pay(parent), home) is None


def test_c13_subagent_calling_row_mismatch_denies_with_the_helper_text(hook, home, tmp_path):
    parent, _ = sub_layout(tmp_path, [asst("claude-opus-5-5")],
                           [asst("claude-opus-5-5"), call_row("claude-haiku-5-5", "toolu_01")])
    assert out(hook.decide(sub_pay(parent), home)) == (
        "deny", helper_deny_text(THIS_CALL, AGENT_TYPE, AGENT_ID, "claude-haiku-5-5", "anthropic:haiku", SELF))


def test_c13_the_helper_text_names_the_helper_and_clears_the_main_session(hook, home, tmp_path):
    parent, _ = sub_layout(tmp_path, [], [call_row("claude-haiku-5-5", "toolu_01")])
    _, text = out(hook.decide(sub_pay(parent), home))
    assert f"`{AGENT_TYPE}`" in text and f"`{AGENT_ID}`" in text
    assert "The main session's own model is not affected by this block." in text
    assert "helper agent" in text


@pytest.mark.parametrize("bad", ["absent", None, 7, ""])
def test_c13_agent_type_missing_or_not_a_string_renders_unknown(hook, home, tmp_path, bad):
    parent, _ = sub_layout(tmp_path, [], [call_row("claude-haiku-5-5", "toolu_01")])
    p = sub_pay(parent, agent_type=None)
    if bad != "absent":
        p["agent_type"] = bad
    assert out(hook.decide(p, home)) == (
        "deny", helper_deny_text(THIS_CALL, "unknown", AGENT_ID, "claude-haiku-5-5", "anthropic:haiku", SELF))


def test_c13_a_subagent_row_appended_late_is_found_by_polling(hook, home, tmp_path):
    parent, sub = sub_layout(tmp_path, [asst("claude-opus-5-5")], [asst("claude-opus-5-5")])
    w = threading.Timer(0.25, append_jsonl, (sub, call_row("claude-haiku-5-5", "toolu_01")))
    w.start()
    try:
        res = hook.decide(sub_pay(parent), home)
    finally:
        w.join()
    assert out(res) == ("deny", helper_deny_text(THIS_CALL, AGENT_TYPE, AGENT_ID, "claude-haiku-5-5",
                                                 "anthropic:haiku", SELF))


def test_c13_empty_agent_id_is_a_main_session_call(hook, home, tmp_path):
    """Only a NON-EMPTY string agent_id makes a helper call: the parent file is read, the plain text 2.1 is used."""
    parent = write_jsonl(tmp_path / "t.jsonl", [call_row("claude-sonnet-5-5", "toolu_01")])
    assert out(hook.decide(sub_pay(parent, agent_id=""), home)) == (
        "deny", deny_text(THIS_CALL, "claude-sonnet-5-5", "anthropic:sonnet", SELF, home))


# -- R2.2: unmapped branches a-d apply to the helper's model id

@pytest.mark.parametrize("model,self_lineage,expect", [
    ("claude-opus-5 beta", "openai:gpt", "deny"),            # a: known other provider
    ("claude-flash-9 beta", "deepseek:flash", "deny"),       # a: shared family word across providers
    ("claude-opus-5 beta", SELF, "unmapped_model"),          # b
    ("mystery-opus-9", SELF, "deny_c"),                      # c: first token foreign (rule U)
    ("anthropic-opus-6", SELF, "unmapped_model"),            # c: first token is the seat's own
    ("mystery-9", SELF, "deny_c"),                           # d: same three-remedy text as c (round 9)
])
def test_r22_unmapped_branches_apply_to_the_helper_model(hook, home, tmp_path, model, self_lineage, expect):
    set_lineage(home, self_lineage)
    parent, _ = sub_layout(tmp_path, [], [call_row(model, "toolu_01")])
    got = out(hook.decide(sub_pay(parent), home))
    if expect == "deny":
        assert got == ("deny", helper_deny_unmapped_text(THIS_CALL, AGENT_TYPE, AGENT_ID, model, self_lineage))
    elif expect == "deny_c":
        assert got == ("deny", helper_deny_unmapped_c_text(THIS_CALL, AGENT_TYPE, AGENT_ID, model, self_lineage))
    else:
        assert got == ("allow-context", unverified_text("unmapped_model"))


# -- C14: the fallback reads the SUB-AGENT file, never the parent's

def test_c14_fallback_mismatch_from_the_subagent_file_denies_with_previous_reply_helper_text(fast, home, tmp_path):
    parent, _ = sub_layout(tmp_path, [asst("claude-opus-5-5")], [asst("claude-haiku-5-5"), user()])
    assert out(fast.decide(sub_pay(parent), home)) == (
        "deny", helper_deny_text(PREVIOUS, AGENT_TYPE, AGENT_ID, "claude-haiku-5-5", "anthropic:haiku", SELF))


def test_c14_fallback_match_from_the_subagent_file_is_never_silent(fast, home, tmp_path):
    parent, _ = sub_layout(tmp_path, [asst("claude-haiku-5-5")], [asst("claude-opus-5-5"), user()])
    assert out(fast.decide(sub_pay(parent), home)) == ("allow-context", unverified_text("fallback_newest_row"))


def test_c14_fallback_unmapped_helper_model_uses_the_previous_reply_wording(fast, home, tmp_path):
    parent, _ = sub_layout(tmp_path, [asst("claude-opus-5-5")], [asst("mystery-9")])
    assert out(fast.decide(sub_pay(parent), home)) == (
        "deny", helper_deny_unmapped_c_text(PREVIOUS, AGENT_TYPE, AGENT_ID, "mystery-9", SELF))


# -- C15: missing / unreadable / invalid-id sub-agent file: allow, never silent, with its code

def test_c15_missing_subagent_file(fast, home, tmp_path):
    parent = write_jsonl(tmp_path / "sess-1.jsonl", [asst("claude-haiku-5-5")])  # the parent would DENY if read
    assert out(fast.decide(sub_pay(parent), home)) == ("allow-context", unverified_text("subagent_transcript_missing"))


def test_c15_missing_subagent_directory_too(fast, home, tmp_path):
    parent = write_jsonl(tmp_path / "sess-1.jsonl", [asst("claude-haiku-5-5")])
    (tmp_path / "sess-1").write_text("a file where the session directory should be")
    assert out(fast.decide(sub_pay(parent), home)) == ("allow-context", unverified_text("subagent_transcript_missing"))


def test_c15_unreadable_subagent_file_not_a_regular_file(fast, home, tmp_path):
    parent = write_jsonl(tmp_path / "sess-1.jsonl", [asst("claude-haiku-5-5")])
    (tmp_path / "sess-1" / "subagents" / f"agent-{AGENT_ID}.jsonl").mkdir(parents=True)  # a directory
    assert out(fast.decide(sub_pay(parent), home)) == (
        "allow-context", unverified_text("subagent_transcript_unreadable"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_c15_a_fifo_subagent_file_is_unreadable_and_does_not_hang(fast, home, tmp_path):
    parent = write_jsonl(tmp_path / "sess-1.jsonl", [asst("claude-haiku-5-5")])
    d = tmp_path / "sess-1" / "subagents"
    d.mkdir(parents=True)
    os.mkfifo(d / f"agent-{AGENT_ID}.jsonl")
    assert out(fast.decide(sub_pay(parent), home)) == (
        "allow-context", unverified_text("subagent_transcript_unreadable"))


@pytest.mark.parametrize("field,bad", [
    ("agent_id", "../x"), ("agent_id", ".."), ("agent_id", "a/b"), ("agent_id", "a\\b"), ("agent_id", "a\x00b"),
    ("agent_id", "/abs"), ("agent_id", ["x"]), ("agent_id", 12),
    ("session_id", "../x"), ("session_id", ".."), ("session_id", "a/b"), ("session_id", "a\\b"),
    ("session_id", "a\x00b"), ("session_id", None), ("session_id", ""),
])
def test_c15_invalid_ids_allow_unverified_and_read_nothing(fast, home, tmp_path, field, bad):
    parent, _ = sub_layout(tmp_path, [asst("claude-haiku-5-5")], [call_row("claude-haiku-5-5", "toolu_01")])
    p = sub_pay(parent)
    p[field] = bad
    assert out(fast.decide(p, home)) == ("allow-context", unverified_text("subagent_id_invalid"))


def test_c15_model_of_call_reports_each_code_directly(tmp_path):
    parent = write_jsonl(tmp_path / "sess-1.jsonl", [asst("claude-opus-5-5")])
    assert sig.model_of_call(parent, "toolu_01", 0.05, 0.01, agent_id=AGENT_ID, session_id="sess-1") == (
        None, "fallback_newest_row", "subagent_transcript_missing")
    assert sig.model_of_call(parent, "toolu_01", 0.05, 0.01, agent_id="../x", session_id="sess-1") == (
        None, "fallback_newest_row", "subagent_id_invalid")
    assert sig.model_of_call(parent, "toolu_01", 0.05, 0.01, agent_id=AGENT_ID, session_id="a/b") == (
        None, "fallback_newest_row", "subagent_id_invalid")


# -- C17: the parent transcript is never read for a helper call

def test_c17_parent_newest_row_matches_while_the_subagent_file_mismatches_denies(fast, home, tmp_path):
    parent, _ = sub_layout(tmp_path, [asst("claude-opus-5-5"), call_row("claude-opus-5-5", "toolu_01")],
                           [asst("claude-haiku-5-5"), call_row("claude-haiku-5-5", "toolu_01")])
    kind, text = out(fast.decide(sub_pay(parent), home))
    assert kind == "deny" and "claude-haiku-5-5" in text and "helper agent" in text


def test_c17_the_parent_calling_row_is_not_used_when_the_subagent_file_lacks_it(fast, home, tmp_path):
    """The parent holds the calling row (opus, MATCH); the helper's file does not: the HELPER file's newest
    row (haiku) decides, so the answer is a deny naming the previous reply."""
    parent, _ = sub_layout(tmp_path, [call_row("claude-opus-5-5", "toolu_01")], [asst("claude-haiku-5-5")])
    assert out(fast.decide(sub_pay(parent), home)) == (
        "deny", helper_deny_text(PREVIOUS, AGENT_TYPE, AGENT_ID, "claude-haiku-5-5", "anthropic:haiku", SELF))


def test_c17_the_main_session_call_still_reads_the_parent_file(hook, home, tmp_path):
    parent, _ = sub_layout(tmp_path, [call_row("claude-sonnet-5-5", "toolu_01")], [asst("claude-opus-5-5")])
    assert out(hook.decide(pay(tpath=parent, session="sess-1"), home)) == (
        "deny", deny_text(THIS_CALL, "claude-sonnet-5-5", "anthropic:sonnet", SELF, home))


# -- C16: no shipped agent spec that can WRITE to ENGRAM pins a model (R2.4)

AGENT_SPEC_DIR = SRC_ENGRAM / "agents" / "claude"   # tiers.json ships agents/claude/<f>.md as agents/<f>.md


def _has_frontmatter(text):
    lines = text.splitlines()
    return bool(lines) and lines[0].strip() == "---"


def _frontmatter_fields(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields = {}
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", ln)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def _tool_names(tools_value):
    v = tools_value.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return {t.strip().strip("'\"") for t in v.split(",") if t.strip()}


def _write_tool_full_names():
    d = json.loads(TOOL_LIST_JSON.read_text(encoding="utf-8"))
    return {p + n for p in d["prefixes"] for n in d["write_tools"]}


def writer_specs_with_a_model(spec_dir):
    """({spec names naming an ENGRAM write tool}, [those that also carry a `model:` line])."""
    writers = _write_tool_full_names()
    named, pinned = set(), []
    for f in sorted(Path(spec_dir).glob("*.md")):
        text = f.read_text(encoding="utf-8")
        if not _has_frontmatter(text):
            continue   # not an agent spec (e.g. README.md)
        fm = _frontmatter_fields(text)
        # no `tools:` line: Claude Code gives the agent EVERY tool, ENGRAM write tools included
        if "tools" not in fm or _tool_names(fm["tools"]) & writers:
            named.add(f.stem)
            if "model" in fm:
                pinned.append(f.stem)
    return named, pinned


def test_c16_every_shipped_agent_spec_that_can_write_to_engram_has_no_model_line():
    named, pinned = writer_specs_with_a_model(AGENT_SPEC_DIR)
    assert "engram-dream-master" in named, "the check found no writer: its own parse is broken"
    assert pinned == [], (f"agent specs that name an ENGRAM write tool AND pin `model:`: {pinned}. A helper that "
                          f"writes must run on the session's model (the gate denies another family): decide first.")


def test_c16_the_shipped_set_today_is_the_dream_master_only():
    named, _ = writer_specs_with_a_model(AGENT_SPEC_DIR)
    assert named == {"engram-dream-master"}


def _plant(d, name, tools, model=None):
    lines = ["---", f"name: {name}", "description: planted", f"tools: {tools}"]
    if model:
        lines.append(f"model: {model}")
    lines += ["---", "body", ""]
    (d / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


def test_c16_a_planted_writer_with_a_model_line_is_flagged(tmp_path):
    _plant(tmp_path, "planted-writer", "Read, mcp__engram__engram_add_observation", model="sonnet")
    _plant(tmp_path, "planted-plugin-prefix", "Read, mcp__plugin_engram_engram__engram_add_edge", model="sonnet")
    _plant(tmp_path, "planted-ok", "Read, mcp__engram__engram_add_observation")
    named, pinned = writer_specs_with_a_model(tmp_path)
    assert named == {"planted-writer", "planted-plugin-prefix", "planted-ok"}
    assert pinned == ["planted-plugin-prefix", "planted-writer"]


def test_c16_matching_is_by_exact_tool_name_not_prefix(tmp_path):
    """`engram_focus` is a write tool; `engram_focus_sets` is read-only and must not be mistaken for it."""
    assert "mcp__engram__engram_focus" in _write_tool_full_names()
    assert "mcp__engram__engram_focus_sets" not in _write_tool_full_names()
    _plant(tmp_path, "reads-focus-sets", "Read, mcp__engram__engram_focus_sets, mcp__engram__engram_query",
           model="sonnet")
    _plant(tmp_path, "writes-focus", "[Read, mcp__engram__engram_focus]", model="sonnet")
    named, pinned = writer_specs_with_a_model(tmp_path)
    assert named == {"writes-focus"} and pinned == ["writes-focus"]


def test_c16_a_spec_with_frontmatter_and_no_tools_line_inherits_every_tool_so_it_is_a_writer(tmp_path):
    (tmp_path / "x.md").write_text("---\nname: x\nmodel: sonnet\n---\nbody\n", encoding="utf-8")
    (tmp_path / "y.md").write_text("---\nname: y\n---\nbody\n", encoding="utf-8")
    named, pinned = writer_specs_with_a_model(tmp_path)
    assert named == {"x", "y"} and pinned == ["x"]


def test_c16_a_file_without_frontmatter_is_not_an_agent_spec(tmp_path):
    (tmp_path / "README.md").write_text("# Agents\n\nmodel: sonnet\ntools: none\n", encoding="utf-8")
    assert writer_specs_with_a_model(tmp_path) == (set(), [])


# -- Round 7 (Kepler, crossing 27718): the write-gate hook prints its notice on a relative ENGRAM_HOME even though
# the shipped server routes refuse to start there.  Premise: "the ENGRAM MCP server never starts with a relative
# ENGRAM_HOME", pinned on both start routes so a change that lets it start turns red.
#   launcher route: tests/test_launchers_relative_engram_home_3863.py
#       ::test_L1_L5_a_relative_engram_home_is_refused_before_any_side_effect[src/engram/launch-engram-server.sh-*]
#       (exact rc 2, the refusal line on stderr, empty stdout, nothing created, no external command run, so no
#       server can start); not duplicated here.
#   direct route: the cell below.

def test_r7_a_direct_server_start_on_a_relative_engram_home_refuses_non_zero_and_serves_nothing(tmp_path):
    if importlib.util.find_spec("fastmcp") is None:
        pytest.skip("fastmcp is not importable in this interpreter; server.py cannot reach the home resolution")
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "ENGRAM_HOME": "rel/home", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, str(SRC_ENGRAM / "server.py")], input="", capture_output=True,
                          text=True, env=env, cwd=str(cwd), timeout=60)
    assert done.returncode != 0, (done.returncode, done.stderr[-400:])
    assert done.stdout == "", done.stdout[:300]
    assert "ENGRAM_HOME must be an absolute path, got 'rel/home'" in done.stderr, done.stderr[-400:]
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == ["cwd", "home"]
