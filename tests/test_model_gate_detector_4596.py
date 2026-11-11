"""Contract tests for the model-family detector, slice 1 of #4596.

Contract-first (CLAUDE.md "Contract-first tests"): every cell below was written BEFORE the
implementation and run red at base.

Design principle: the harness detects and code writes the text, so a session running on the wrong
model never speaks in the agent's voice.  This slice RECORDS only (no blocking, no stdout, no notice).

Cells (numbers follow the spec at /home/agents-shared/halley/4596-detector-spec.md):
   1 MATCH writes nothing                 2 MISMATCH at the first reply        3 mid-session change, newest wins
   4 back to MATCH writes nothing         5 attachment row beats assistant     6 synthetic / blank ids skipped
   7 non-Anthropic family both ways       8 each UNKNOWN reason, one cell each 9 malformed lines skipped
  10 dormancy (unset / malformed)        11 schema                            12 append-only
  13 change-only                         14 hard cap, one visible line        15 process: nothing on stdout, exit 0
  16 deployed layout, no plugin root     17 tail parser agrees with _read_model
  18 existing signature tests (run unchanged, not re-stated here)             19 time budget
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC_ENGRAM = REPO / "src" / "engram"
HOOK_DIR = SRC_ENGRAM / "hooks" / "claude"
HOOK_FILE = HOOK_DIR / "engram-model-gate-stop.py"

import engram_signature as sig  # noqa: E402

SCHEMA_KEYS = {"schema", "at", "session_id", "outcome", "reason", "model_id", "measured_lineage",
               "self_lineage", "transcript_path"}
UNKNOWN_REASONS = {"transcript_path_missing", "transcript_not_found", "transcript_unreadable", "no_model_row",
                   "window_exceeded", "unmapped_model", "mixed_harness", "internal_error", "records_cap_reached"}


# ------------------------------------------------------------------ helpers

def asst(model, text="hello"):
    return {"type": "assistant", "message": {"role": "assistant", "model": model,
                                              "content": [{"type": "text", "text": text}]}}


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


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("ENGRAM_HOME", str(h))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    set_lineage(h, "anthropic:opus")
    return h


def set_lineage(h: Path, value):
    cfg = {} if value is None else {"self_lineage": value}
    (h / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def load_hook():
    spec = importlib.util.spec_from_file_location("engram_model_gate_stop_under_test", HOOK_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def hook(isolate_hook_loads):
    """The hook module, loaded inside the shared sys.path window (#4029): the hook inserts its own
    directory and, on first use, the runtime directory."""
    return load_hook()


def records(h: Path):
    p = h / "model-gate" / "records.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def stop(hook, h: Path, transcript, session="s1"):
    payload = {"session_id": session}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return hook.detect(payload, h)


def copy_runtime(root: Path) -> None:
    """The runtime modules the build puts at the plugin root (engram_core imports tools/)."""
    for f in SRC_ENGRAM.glob("*.py"):
        shutil.copy2(f, root / f.name)
    shutil.copytree(SRC_ENGRAM / "tools", root / "tools", ignore=shutil.ignore_patterns("__pycache__"))


def run_hook(h: Path, payload, hook_file=HOOK_FILE, plugin_root=None, cwd=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = str(h)
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return subprocess.run([sys.executable, str(hook_file)], input=json.dumps(payload), text=True,
                          capture_output=True, env=env, cwd=cwd, timeout=60)


# ------------------------------------------------------- 1-7 detector outcomes

def test_c01_match_writes_nothing(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [user(), asst("claude-opus-5-5")])
    stop(hook, home, t)
    assert records(home) == []


def test_c02_mismatch_at_first_reply(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [user(), asst("claude-fable-5-2")])
    stop(hook, home, t)
    (r,) = records(home)
    assert r["outcome"] == "MISMATCH" and r["reason"] is None
    assert r["measured_lineage"] == "anthropic:fable" and r["model_id"] == "claude-fable-5-2"
    assert r["self_lineage"] == "anthropic:opus"


def test_c03_mid_session_change_newest_wins(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), asst("claude-opus-5-5"),
                                           asst("claude-sonnet-5-5")])
    stop(hook, home, t)
    (r,) = records(home)
    assert r["outcome"] == "MISMATCH" and r["measured_lineage"] == "anthropic:sonnet"


def test_c04_back_to_match_writes_no_new_record(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-sonnet-5-5")])
    stop(hook, home, t)
    assert len(records(home)) == 1
    write_jsonl(t, [asst("claude-sonnet-5-5"), asst("claude-opus-5-5")])
    stop(hook, home, t)
    assert len(records(home)) == 1
    # a different session, so change-only cannot be what keeps the file quiet: the newest row (opus) is a MATCH
    stop(hook, home, t, session="other")
    assert len(records(home)) == 1


def test_c05_attachment_row_beats_assistant_row(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [attach("claude-fable-5-2"), asst("claude-opus-5-5")])
    stop(hook, home, t)
    (r,) = records(home)
    assert r["outcome"] == "MISMATCH" and r["measured_lineage"] == "anthropic:fable"


@pytest.mark.parametrize("junk", ["<synthetic>", "", "   ", None])
def test_c06_synthetic_and_blank_ids_fall_through(home, hook, tmp_path, junk):
    # older real id is sonnet (mismatch); the newer junk id must not decide, nor mask it as unknown
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-sonnet-5-5"), asst(junk)])
    stop(hook, home, t)
    (r,) = records(home)
    assert r["outcome"] == "MISMATCH" and r["model_id"] == "claude-sonnet-5-5"


def test_c07_non_anthropic_family_both_ways(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("gpt-6.1-sol")])
    stop(hook, home, t)
    (r,) = records(home)
    assert r["outcome"] == "MISMATCH" and r["measured_lineage"] == "openai:gpt"
    other = tmp_path / "home2"
    other.mkdir()
    set_lineage(other, "openai:gpt")
    stop(hook, other, t)
    assert records(other) == []


# ------------------------------------------------------------ 8 UNKNOWN reasons

def only_unknown(home, reason):
    (r,) = records(home)
    assert r["outcome"] == "UNKNOWN" and r["reason"] == reason, r
    assert r["measured_lineage"] is None or reason == "unmapped_model"
    return r


def test_c08_transcript_path_missing(home, hook):
    stop(hook, home, None)
    r = only_unknown(home, "transcript_path_missing")
    assert r["transcript_path"] is None and r["model_id"] is None


def test_c08_transcript_not_found(home, hook, tmp_path):
    stop(hook, home, tmp_path / "nope.jsonl")
    only_unknown(home, "transcript_not_found")


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores mode 000")
def test_c08_transcript_unreadable(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])
    t.chmod(0)
    try:
        stop(hook, home, t)
    finally:
        t.chmod(0o644)
    only_unknown(home, "transcript_unreadable")


def test_c08_no_model_row(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [user("only a user row so far")])
    stop(hook, home, t)
    only_unknown(home, "no_model_row")


def test_c08_window_exceeded_newest_line_larger_than_the_window(home, hook, tmp_path):
    big = user("x" * (hook.TAIL_BYTES + 1000))
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), big])
    stop(hook, home, t)
    only_unknown(home, "window_exceeded")


def test_c08_window_exceeded_no_model_row_in_a_full_window(home, hook, tmp_path):
    rows = [asst("claude-opus-5-5")] + [user("y" * 1000) for _ in range(400)]  # > 262144 bytes of user rows
    t = write_jsonl(tmp_path / "t.jsonl", rows)
    assert t.stat().st_size > hook.TAIL_BYTES
    stop(hook, home, t)
    only_unknown(home, "window_exceeded")


def test_c08_unmapped_model(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("mystery-model-1")])
    stop(hook, home, t)
    only_unknown(home, "unmapped_model")


def test_c08_unmapped_claude_prefixed_model(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-")])
    stop(hook, home, t)
    r = only_unknown(home, "unmapped_model")
    assert r["model_id"] == "claude-"


def test_c08_mixed_harness(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5"), turn_ctx("gpt-6.1-sol")])
    stop(hook, home, t)
    only_unknown(home, "mixed_harness")


def test_c08_internal_error(home, hook, tmp_path, monkeypatch):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])

    def boom(*a, **k):
        raise RuntimeError("forced")

    monkeypatch.setattr(hook, "read_model_tail", boom)
    stop(hook, home, t)
    only_unknown(home, "internal_error")


def test_c08_unknown_never_collapses_into_match(home, hook, tmp_path):
    # the same transcript shape a MATCH would have, minus the model: must not read as a match
    t = write_jsonl(tmp_path / "t.jsonl", [user()])
    stop(hook, home, t)
    assert len(records(home)) == 1


# -------------------------------------------------------- 9 malformed lines

def test_c09_malformed_lines_are_skipped(home, hook, tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps(asst("claude-sonnet-5-5")) + "\n" + "{not json\n" + "[1,2]\n" + "\n" + "\x00\x01garbage\n",
                 encoding="utf-8")
    stop(hook, home, t)
    (r,) = records(home)
    assert r["outcome"] == "MISMATCH" and r["model_id"] == "claude-sonnet-5-5"


# ----------------------------------------------------------------- 10 dormancy

@pytest.mark.parametrize("value", [None, "", "   ", "opus", "Anthropic:Opus", "anthropic:", ":opus", 7, ["a:b"]])
def test_c10_dormant_writes_nothing_and_creates_nothing(home, hook, tmp_path, value):
    set_lineage(home, value)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    stop(hook, home, t)
    stop(hook, home, None)
    assert not (home / "model-gate").exists()


def test_c10_dormant_when_config_is_missing_or_unparseable(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    (home / "config.json").write_text("{broken", encoding="utf-8")
    stop(hook, home, t)
    (home / "config.json").unlink()
    stop(hook, home, t)
    assert not (home / "model-gate").exists()


def test_c10_dormancy_is_decided_before_the_runtime_is_loaded(home, hook, tmp_path, monkeypatch):
    """Round 2 (S1): a malformed or unset self_lineage must not even touch the runtime modules."""
    calls = []
    monkeypatch.setattr(hook, "_load_runtime", lambda: calls.append(1) or False)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    for value in ("Anthropic:Opus", "opus", None):
        set_lineage(home, value)
        stop(hook, home, t)
    assert calls == [] and not (home / "model-gate").exists()


def test_c10_malformed_lineage_with_unloadable_runtime_writes_nothing(home, tmp_path):
    """Round 2 (S1): real process, hooks beside NO runtime modules, malformed self_lineage -> nothing written."""
    root = tmp_path / "bare"
    (root / "hooks").mkdir(parents=True)
    for f in HOOK_DIR.glob("*.py"):
        shutil.copy2(f, root / "hooks" / f.name)
    set_lineage(home, "Not A Lineage")
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    done = run_hook(home, {"session_id": "b1", "transcript_path": str(t)},
                    hook_file=root / "hooks" / HOOK_FILE.name, cwd=str(tmp_path))
    assert (done.returncode, done.stdout) == (0, "")
    assert not (home / "model-gate").exists()
    # and a VALID lineage with the same unloadable runtime still records internal_error (the case S2 names)
    set_lineage(home, "anthropic:opus")
    done = run_hook(home, {"session_id": "b2", "transcript_path": str(t)},
                    hook_file=root / "hooks" / HOOK_FILE.name, cwd=str(tmp_path))
    assert done.returncode == 0 and done.stdout == ""
    assert [(r["outcome"], r["reason"]) for r in records(home)] == [("UNKNOWN", "internal_error")]


# ------------------------------------------------------------ 11-14 record file

def test_c11_schema(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    before = datetime.now(timezone.utc)
    stop(hook, home, t, session="sess-x")
    after = datetime.now(timezone.utc)
    (r,) = records(home)
    assert set(r) == SCHEMA_KEYS
    assert r["schema"] == 1 and r["session_id"] == "sess-x" and r["transcript_path"] == str(t)
    at = datetime.fromisoformat(r["at"])
    assert at.tzinfo is not None and at.utcoffset().total_seconds() == 0
    assert before.replace(microsecond=0) <= at <= after


def test_c11_unknown_record_has_the_same_keys(home, hook):
    stop(hook, home, None)
    (r,) = records(home)
    assert set(r) == SCHEMA_KEYS and r["reason"] in UNKNOWN_REASONS


def test_c12_append_only_keeps_existing_bytes_as_prefix(home, hook, tmp_path):
    d = home / "model-gate"
    d.mkdir()
    existing = b'{"schema": 1, "legacy": true}\nnot even json\n'
    (d / "records.jsonl").write_bytes(existing)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    stop(hook, home, t)
    after = (d / "records.jsonl").read_bytes()
    assert after.startswith(existing) and len(after) > len(existing) and after.endswith(b"\n")


def test_c13_change_only_per_session(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    stop(hook, home, t, session="A")
    stop(hook, home, t, session="A")
    assert len(records(home)) == 1
    stop(hook, home, t, session="B")
    assert [r["session_id"] for r in records(home)] == ["A", "B"]


def test_c13_a_changed_outcome_or_model_writes_again(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    stop(hook, home, t)
    write_jsonl(t, [asst("claude-fable-5-2"), asst("claude-sonnet-5-5")])
    stop(hook, home, t)          # model_id changed
    stop(hook, home, None)       # outcome/reason changed (UNKNOWN)
    stop(hook, home, None)       # unchanged
    assert [(r["outcome"], r["reason"], r["model_id"]) for r in records(home)] == [
        ("MISMATCH", None, "claude-fable-5-2"), ("MISMATCH", None, "claude-sonnet-5-5"),
        ("UNKNOWN", "transcript_path_missing", None)]


def test_c13_change_only_compares_the_reason_alone(home, hook, tmp_path):
    # Same session, same outcome (UNKNOWN), same null model_id; only the reason differs -> two lines.
    t = tmp_path / "t.jsonl"
    t.write_bytes(b"x" * (hook.TAIL_BYTES + 10) + b"\n")  # one huge line, no model row: window_exceeded
    stop(hook, home, t)
    write_jsonl(t, [user()])                                 # small file, no assistant row: no_model_row
    stop(hook, home, t)
    stop(hook, home, t)                                      # unchanged: still no third line
    assert [(r["outcome"], r["reason"], r["model_id"]) for r in records(home)] == [
        ("UNKNOWN", "window_exceeded", None), ("UNKNOWN", "no_model_row", None)]


def test_c10_hook_lineage_pattern_equals_cores(hook):
    # The hook decides dormancy before loading the runtime (by design), so it carries its own copy of
    # engram_core._LINEAGE_RE.  Nothing else ties the two copies together: this does.
    import engram_core
    assert hook._LINEAGE_RE.pattern == engram_core._LINEAGE_RE.pattern


@pytest.mark.parametrize("value", ["anthropic:opus", "Anthropic:opus", "anth.ropic:opus", "anthropic:opus.5",
                                   "anthropic:", ":opus", "a b:c", "anthropic:opus:5", "openai:gpt-6_1"])
def test_c10_hook_dormancy_agrees_with_core_on_the_lineage_shape(home, hook, tmp_path, value):
    import engram_core
    set_lineage(home, value)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])  # a model no listed lineage matches
    wrote = stop(hook, home, t) is not None
    assert wrote == bool(engram_core._LINEAGE_RE.fullmatch(value))
    assert bool(records(home)) == wrote


def test_c13_change_only_finds_the_record_past_other_sessions(home, hook, tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    stop(hook, home, t, session="A")
    for s in ("B", "C", "D"):
        stop(hook, home, t, session=s)
    stop(hook, home, t, session="A")  # newest record FOR A is still the same -> skipped
    assert len(records(home)) == 4


def test_c14_cap_writes_exactly_one_visible_line_then_nothing(home, hook, tmp_path):
    d = home / "model-gate"
    d.mkdir()
    filler = (json.dumps({"schema": 1, "filler": "x" * 200}) + "\n").encode()
    blob = filler * (hook.RECORDS_MAX_BYTES // len(filler) + 1)
    (d / "records.jsonl").write_bytes(blob)
    assert (d / "records.jsonl").stat().st_size >= hook.RECORDS_MAX_BYTES
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    stop(hook, home, t, session="A")
    stop(hook, home, t, session="B")
    stop(hook, home, None, session="C")
    lines = (d / "records.jsonl").read_bytes()[len(blob):].decode().splitlines()
    assert len(lines) == 1
    r = json.loads(lines[0])
    assert r["outcome"] == "UNKNOWN" and r["reason"] == "records_cap_reached" and set(r) == SCHEMA_KEYS



def _filler_records(total, line_len=1024):
    """Valid JSON-lines filler of exactly ``total`` bytes (lines of ``line_len``, the last one the remainder)."""
    def mk(n):
        base = len(json.dumps({"schema": 1, "filler": ""}).encode()) + 1
        return (json.dumps({"schema": 1, "filler": "x" * (n - base)}) + "\n").encode()
    full, rem = divmod(total, line_len)
    blob = mk(line_len) * full + (mk(rem) if rem else b"")
    assert len(blob) == total
    return blob


def test_c14_cap_boundary_exactly_at_the_cap_is_capped(home, hook, tmp_path):
    """Round 2 (S4b): 'at or over'.  Kills the mutant ``size > RECORDS_MAX_BYTES``."""
    d = home / "model-gate"
    d.mkdir()
    blob = _filler_records(hook.RECORDS_MAX_BYTES)
    (d / "records.jsonl").write_bytes(blob)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    stop(hook, home, t)
    (r,) = [json.loads(x) for x in (d / "records.jsonl").read_bytes()[len(blob):].decode().splitlines()]
    assert r["reason"] == "records_cap_reached"


def test_c14_cap_boundary_one_byte_under_gets_a_normal_record(home, hook, tmp_path):
    d = home / "model-gate"
    d.mkdir()
    blob = _filler_records(hook.RECORDS_MAX_BYTES - 1)
    (d / "records.jsonl").write_bytes(blob)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    stop(hook, home, t)
    (r,) = [json.loads(x) for x in (d / "records.jsonl").read_bytes()[len(blob):].decode().splitlines()]
    assert (r["outcome"], r["reason"], r["model_id"]) == ("MISMATCH", None, "claude-fable-5-2")


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_c14_linked_records_file_is_refused_and_the_target_untouched(home, tmp_path, kind):
    """Round 2 (S5): nothing is written through a link; the hook still exits 0 with no stdout."""
    d = home / "model-gate"
    d.mkdir()
    target = tmp_path / "victim.txt"
    target.write_bytes(b"precious\n")
    link = d / "records.jsonl"
    if kind == "symlink":
        link.symlink_to(target)
    else:
        os.link(target, link)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    done = run_hook(home, {"session_id": "L", "transcript_path": str(t)})
    assert (done.returncode, done.stdout) == (0, "")
    assert target.read_bytes() == b"precious\n"
    assert link.read_bytes() == b"precious\n"


# ------------------------------------------------------------ 15 hook process

def test_c15_process_is_silent_and_exits_zero_on_every_outcome(home, tmp_path):
    match = write_jsonl(tmp_path / "m.jsonl", [asst("claude-opus-5-5")])
    mismatch = write_jsonl(tmp_path / "x.jsonl", [asst("claude-fable-5-2")])
    cases = [
        ("MATCH", {"session_id": "p1", "transcript_path": str(match)}, 0),
        ("MISMATCH", {"session_id": "p2", "transcript_path": str(mismatch)}, 1),
        ("UNKNOWN", {"session_id": "p3"}, 1),
    ]
    for name, payload, n in cases:
        done = run_hook(home, payload)
        assert (done.returncode, done.stdout, done.stderr) == (0, "", ""), (name, done)
    assert [r["outcome"] for r in records(home)] == ["MISMATCH", "UNKNOWN"]
    # dormancy
    other = tmp_path / "dormant"
    other.mkdir()
    set_lineage(other, None)
    done = run_hook(other, {"session_id": "p4", "transcript_path": str(mismatch)})
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert not (other / "model-gate").exists()


@pytest.mark.parametrize("stdin", ["", "not json", "[1]", "null"])
def test_c15_process_survives_bad_stdin(home, stdin):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env["ENGRAM_HOME"] = str(home)
    done = subprocess.run([sys.executable, str(HOOK_FILE)], input=stdin, text=True, capture_output=True, env=env,
                          timeout=60)
    assert (done.returncode, done.stdout) == (0, "")


# ------------------------------------------------------- 16 deployed layout

def test_c16_flattened_layout_without_plugin_root_resolves_engram_core(home, tmp_path):
    root = tmp_path / "deployed"
    (root / "hooks").mkdir(parents=True)
    copy_runtime(root)
    for f in HOOK_DIR.glob("*.py"):
        shutil.copy2(f, root / "hooks" / f.name)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    done = run_hook(home, {"session_id": "d1", "transcript_path": str(t)},
                    hook_file=root / "hooks" / HOOK_FILE.name, cwd=str(tmp_path))
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    (r,) = records(home)
    assert r["outcome"] == "MISMATCH" and r["measured_lineage"] == "anthropic:fable"


def test_c16_the_walk_stops_at_the_tree_root(home, tmp_path):
    """A decoy engram_signature.py/engram_core.py ABOVE the plugin root must never be used (#3983)."""
    outer = tmp_path / "outer"
    root = outer / "plugin"
    (root / "hooks").mkdir(parents=True)
    for f in HOOK_DIR.glob("*.py"):
        shutil.copy2(f, root / "hooks" / f.name)
    # the plugin root holds engram_core.py (the tree-root marker) but NOT engram_signature.py;
    # the decoy above it does.  A bounded walk finds nothing -> internal_error, never the decoy.
    shutil.copy2(SRC_ENGRAM / "engram_core.py", root / "engram_core.py")
    shutil.copy2(SRC_ENGRAM / "engram_signature.py", outer / "engram_signature.py")
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    done = run_hook(home, {"session_id": "d2", "transcript_path": str(t)},
                    hook_file=root / "hooks" / HOOK_FILE.name, cwd=str(tmp_path))
    assert (done.returncode, done.stdout) == (0, "")
    recs = records(home)
    assert all(r["outcome"] != "MISMATCH" for r in recs), "the decoy above the tree root was used"
    assert [(r["outcome"], r["reason"]) for r in recs] == [("UNKNOWN", "internal_error")]


def test_c16_plugin_root_env_is_preferred(home, tmp_path):
    root = tmp_path / "plugin"
    (root / "hooks").mkdir(parents=True)
    copy_runtime(root)
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-fable-5-2")])
    done = run_hook(home, {"session_id": "d3", "transcript_path": str(t)}, plugin_root=root)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert [r["outcome"] for r in records(home)] == ["MISMATCH"]


# ------------------------------------------------- 17 shared parser agreement

def _agree_cases(tmp_path):
    big_junk = [user("z" * 500) for _ in range(5)]
    return {
        "opus": [asst("claude-opus-5-5")],
        "attach_over_asst": [attach("claude-fable-5-2", "Fable"), asst("claude-opus-5-5")],
        "newest_asst": [asst("claude-opus-5-5"), asst("claude-sonnet-5-5")],
        "synthetic_skipped": [asst("claude-sonnet-5-5"), asst("<synthetic>")],
        "codex": [turn_ctx("gpt-6.1-sol")],
        "mixed": [asst("claude-opus-5-5"), turn_ctx("gpt-6.1-sol")],
        "unmapped": [asst("mystery-1")],
        "no_rows": [user()],
        "placeholder_only": [asst("<synthetic>")],
        "codex_no_model": [turn_ctx("")],
        "trailing_junk": [asst("claude-opus-5-5")] + big_junk,
    }


def test_c17_tail_and_whole_file_parsers_agree_on_small_transcripts(tmp_path):
    for name, rows in _agree_cases(tmp_path).items():
        t = write_jsonl(tmp_path / f"{name}.jsonl", rows)
        try:
            whole = ("ok", sig._read_model(t))
        except sig._Refusal as r:
            whole = ("refused", r.code)
        res = sig.read_model_tail(t, 1 << 20)
        tail = ("ok", (res.model_id, res.marketing_name)) if res.reason is None else ("refused", res.reason)
        if whole[0] == "refused":
            # the two spellings of "no usable model" (none / placeholder-only) are one hook reason
            expect = {"no_usable_model": "no_model_row"}.get(whole[1], whole[1])
            assert tail == ("refused", expect), (name, whole, tail)
        else:
            assert tail == whole, (name, whole, tail)


def test_c17_read_model_tail_window_rules(tmp_path):
    a = asst("claude-opus-5-5")
    t = write_jsonl(tmp_path / "t.jsonl", [a, user("p" * 3000), asst("claude-sonnet-5-5")])
    full = t.stat().st_size
    # whole file inside the window
    assert sig.read_model_tail(t, full).model_id == "claude-sonnet-5-5"
    # window starts mid-line: the partial first line is discarded, the last line still decides
    res = sig.read_model_tail(t, len(json.dumps(asst("claude-sonnet-5-5"))) + 10)
    assert res.model_id == "claude-sonnet-5-5"
    # window holds only a partial line -> window_exceeded, not an older row's id
    res = sig.read_model_tail(t, 50)
    assert res.model_id is None and res.reason == "window_exceeded"
    # a missing file and a directory
    assert sig.read_model_tail(tmp_path / "nope", 100).reason == "transcript_not_found"
    assert sig.read_model_tail(tmp_path, 100).reason == "transcript_unreadable"


def test_c17_read_model_whole_file_is_unchanged_in_shape(tmp_path):
    t = write_jsonl(tmp_path / "t.jsonl", [attach("claude-fable-5-2", " Fable 5.2 ")])
    assert sig._read_model(t) == ("claude-fable-5-2", "Fable 5.2")
    with pytest.raises(sig._Refusal) as e:
        sig._read_model(write_jsonl(tmp_path / "u.jsonl", [user()]))
    assert e.value.field == "model"



def _first_line_decides(path, total):
    """A transcript of exactly ``total`` bytes whose only model row is the FIRST line (opus)."""
    first = json.dumps(asst("claude-opus-5-5")) + "\n"
    pad = total - len(first.encode()) - len(json.dumps(user("")) + "\n")
    assert pad >= 0
    rest = json.dumps(user("p" * pad)) + "\n"
    path.write_text(first + rest, encoding="utf-8")
    assert path.stat().st_size == total
    return path


def test_c17_window_boundary_exactly_the_bound_reads_the_first_line_whole(tmp_path):
    """Round 2 (S4a).  Kills the mutant 'start the window one byte late'."""
    n = sig.read_model_tail(_first_line_decides(tmp_path / "a.jsonl", 4096), 4096)
    assert (n.model_id, n.reason) == ("claude-opus-5-5", None)


def test_c17_window_boundary_one_byte_over_discards_the_partial_first_line(tmp_path):
    n = sig.read_model_tail(_first_line_decides(tmp_path / "b.jsonl", 4097), 4096)
    assert (n.model_id, n.reason) == (None, "window_exceeded")


def test_c17_hook_window_boundary_at_TAIL_BYTES(home, hook, tmp_path):
    ta = _first_line_decides(tmp_path / "a.jsonl", hook.TAIL_BYTES)
    stop(hook, home, ta, session="A")          # opus decides at exactly the bound -> MATCH, nothing written
    assert records(home) == []
    tb = _first_line_decides(tmp_path / "b.jsonl", hook.TAIL_BYTES + 1)
    stop(hook, home, tb, session="B")          # one over: the first line is partial and discarded
    (r,) = records(home)
    assert (r["outcome"], r["reason"]) == ("UNKNOWN", "window_exceeded")


def test_c17_unknown_refusal_code_maps_to_internal_error(tmp_path, monkeypatch):
    """Round 2 (S3): a refusal whose code the tail reader does not know is not 'no_model_row'."""
    t = write_jsonl(tmp_path / "t.jsonl", [asst("claude-opus-5-5")])

    def boom(rows, transcript):
        raise sig._Refusal("model", "something new", code="brand_new_code")

    monkeypatch.setattr(sig, "_model_from_rows", boom)
    assert sig.read_model_tail(t, 1 << 20).reason == "internal_error"
    monkeypatch.setattr(sig, "_model_from_rows",
                        lambda rows, transcript: (_ for _ in ()).throw(sig._Refusal("model", "no code")))
    assert sig.read_model_tail(t, 1 << 20).reason == "internal_error"


# ----------------------------------------------------------------- 19 budget

@pytest.fixture(scope="module")
def big_transcript(tmp_path_factory):
    d = tmp_path_factory.mktemp("big")
    p = d / "big.jsonl"
    line = (json.dumps(asst("claude-opus-5-5", "w" * 900)) + "\n").encode()
    target = 50 * 1024 * 1024
    with open(p, "wb") as f:
        n = 0
        while n < target:
            f.write(line)
            n += len(line)
        f.write((json.dumps(asst("claude-fable-5-2", "last")) + "\n").encode())
    assert p.stat().st_size >= target
    return p


def test_c19_time_budget_in_process(home, hook, big_transcript, capsys):
    # Scope of the number: tail read, parse, map, the records scan and the append / change-only skip.  The
    # records file is pre-filled so the 64 KiB scan window is full (records of OTHER sessions, so every timed
    # call still scans the whole window and then appends or skips).
    rdir = home / "model-gate"
    rdir.mkdir()
    filler = (json.dumps({"schema": 1, "at": "2026-01-01T00:00:00+00:00", "session_id": "other",
                          "outcome": "UNKNOWN", "reason": "no_model_row", "model_id": None,
                          "measured_lineage": None, "self_lineage": "anthropic:opus",
                          "transcript_path": "/x" * 100}) + "\n").encode()
    (rdir / "records.jsonl").write_bytes(filler * (3 * hook.RECORDS_SCAN_BYTES // len(filler)))
    assert (rdir / "records.jsonl").stat().st_size > hook.RECORDS_SCAN_BYTES
    assert hook.TIME_BUDGET_MS == 100
    stop(hook, home, big_transcript, session="warm")  # untimed: first call imports the runtime modules
    cold_free = []
    for i in range(20):
        t0 = time.perf_counter()
        stop(hook, home, big_transcript, session="budget")  # i=0 appends; 1..19 change-only
        cold_free.append((time.perf_counter() - t0) * 1000)
    med, mx = statistics.median(cold_free), max(cold_free)
    with capsys.disabled():
        print(f"\n[4596 budget] 50MB transcript, 20 runs: median {med:.2f} ms, max {mx:.2f} ms "
              f"(budget {hook.TIME_BUDGET_MS} ms, max bound {3 * hook.TIME_BUDGET_MS} ms)")
    assert med < hook.TIME_BUDGET_MS
    assert mx < 3 * hook.TIME_BUDGET_MS


def test_c19_subprocess_wall_time(home, big_transcript, capsys):
    t0 = time.perf_counter()
    done = run_hook(home, {"session_id": "wall", "transcript_path": str(big_transcript)})
    wall = time.perf_counter() - t0
    with capsys.disabled():
        print(f"\n[4596 budget] real hook subprocess on the 50MB transcript: wall {wall * 1000:.0f} ms")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert wall < 2.0
    assert [r["session_id"] for r in records(home)] == ["wall"]
