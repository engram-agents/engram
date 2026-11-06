"""#4390 phase 1: the same-batch report detector, in SHADOW (log only, prints nothing).

Contract cells C1..C12 (spec: the #4390 builder spec; design owner's comment on #4390).
A BLOCK is all tool_use items sharing one message.id; COMPLETE = every tool_use in it has a
tool_result in the transcript. Rule A is hash-fixed (rule_sha d10d33aa8babd0f2).

Synthetic JSONL fixtures only: no real transcript is read here. The acceptance run against two
real transcripts is a throwaway script, not a test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
MODULE_FILE = HOOK_DIR / "_same_batch_shadow.py"
TRIPWIRE = "engram-lesson-tripwire-hook.py"
STOP = "engram-loop-arm-stop-hook.py"  # the Stop hook that carries the call (engram-stop-hook.py does not)
ESSENTIAL_STOP = "engram-stop-hook.py"
LEDGER = "same-batch-shadow-ledger.jsonl"
RULE_SHA = "d10d33aa8babd0f2"
DISTINCT = "ZQXV-distinct-body-marker-4390"


def _load(hooks_dir: Path = HOOK_DIR):
    spec = importlib.util.spec_from_file_location("_same_batch_shadow_under_test", hooks_dir / "_same_batch_shadow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def enable(h):
    """C9 is DEFAULT OFF: every cell that expects a row enables the detector explicitly."""
    Path(h, "config.json").write_text(json.dumps({"same_batch_shadow": {"enabled": True}}))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "engram_home"
    h.mkdir()
    enable(h)
    monkeypatch.setenv("ENGRAM_HOME", str(h))
    return h


@pytest.fixture
def sbs(isolate_hook_loads):
    # the module inserts its own dir into sys.path (as every hook does); the window puts it back
    return _load()


# ----- fixture builders ---------------------------------------------------

def tu(mid, tid, name, inp):
    """One assistant line holding one tool_use (the real shape: one content item per line)."""
    return json.dumps({"type": "assistant", "message": {"id": mid, "role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})


def res(tid, err=False, text="ok"):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text, **({"is_error": True} if err else {})}]}})


def crossing(body):
    return "mcp__umwelt__send_crossing", {"payload_json": json.dumps({"body": body})}


def push():
    return "Bash", {"command": "git push origin x"}


def block(mid, calls, results=True, errs=()):
    """Lines for a block: all tool_uses, then (optionally) all results."""
    lines = [tu(mid, f"{mid}-t{i}", n, inp) for i, (n, inp) in enumerate(calls)]
    if results:
        lines += [res(f"{mid}-t{i}", err=(i in errs)) for i in range(len(calls))]
    return lines


def write(path, lines):
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def rows(home):
    p = Path(home) / LEDGER
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


GOOD = [crossing("I sent the note " + DISTINCT), push()]


# ----- C1 ------------------------------------------------------------------

def test_c1_one_row_whichever_hook_first_and_however_often(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    sbs.evaluate(str(t), "s1", "pretooluse")
    sbs.evaluate(str(t), "s1", "stop")
    sbs.evaluate(str(t), "s1", "pretooluse")
    r = rows(home)
    assert len(r) == 1 and r[0]["message_id"] == "msg_A" and r[0]["source"] == "pretooluse"


def test_c1_stop_first_then_pretooluse_still_one_row(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    sbs.evaluate(str(t), "s1", "stop")
    sbs.evaluate(str(t), "s1", "pretooluse")
    r = rows(home)
    assert len(r) == 1 and r[0]["source"] == "stop"


def test_c1_two_blocks_two_rows_and_other_session_same_mid_is_its_own_row(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD) + block("msg_B", GOOD))
    sbs.evaluate(str(t), "s1", "stop")
    sbs.evaluate(str(t), "s2", "stop")
    assert sorted((r["session_id"], r["message_id"]) for r in rows(home)) == [
        ("s1", "msg_A"), ("s1", "msg_B"), ("s2", "msg_A"), ("s2", "msg_B")]


def test_c1_blocks_split_over_lines_and_interleaved_results_are_one_block(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    # serial MCP-led shape: use, result, use, result, all one message.id
    write(t, [tu("msg_S", "a", *crossing("I sent it")), res("a"), tu("msg_S", "b", *push()), res("b")])
    sbs.evaluate(str(t), "s1", "stop")
    r = rows(home)
    assert len(r) == 1 and r[0]["calls"] == 2


# ----- C2, C3, C4 ------------------------------------------------------------

def test_c2_single_call_block_no_row(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [crossing("I sent it")]))
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []


def test_c3_no_report_call_no_row(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [push(), ("Edit", {"file_path": "/x"})]))
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []


def test_c3_report_that_is_the_only_effect_call_no_row(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [crossing("I sent it"), ("Bash", {"command": "ls"})]))
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []


def test_c3_two_report_calls_are_each_others_effect(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [crossing("I sent it"), ("mcp__umwelt__say", {"payload_json": json.dumps({"text": "x"})})]))
    sbs.evaluate(str(t), "s1", "stop")
    assert len(rows(home)) == 1


def test_c3_history_append_bash_is_a_report_call(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [("Bash", {"command": "echo 'pushed it' >> ~/.engram/history/2026-11-06.md"}), ("Edit", {})]))
    sbs.evaluate(str(t), "s1", "stop")
    assert len(rows(home)) == 1


def test_c3_mcp_effect_name_quirk_of_replay_is_kept(sbs, home, tmp_path):
    # replay.py: short(n).split('engram_')[-1] must be in EFFECT. engram_derive -> derive (effect);
    # an unrelated tool whose short name merely ENDS with an effect word is not.
    assert sbs.isfx("mcp__engram__engram_derive", {})
    assert not sbs.isfx("mcp__x__prefix_derive", {})
    assert sbs.isfx("mcp__x__send_crossing", {})
    assert not sbs.isfx("Read", {})


def test_c4_no_rule_a_word_no_row(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [crossing("will do this next"), push()]))
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []


def test_c4_rule_a_word_must_be_a_whole_word_case_insensitive(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [crossing("resent later, unasked"), push()]))
    write(tmp_path / "u.jsonl", block("msg_B", [crossing("SENT it"), push()]))
    sbs.evaluate(str(t), "s1", "stop")
    sbs.evaluate(str(tmp_path / "u.jsonl"), "s1", "stop")
    assert [r["message_id"] for r in rows(home)] == ["msg_B"]


def test_c4_rule_a_word_in_a_non_report_call_does_not_count(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [crossing("will do"), ("Bash", {"command": "git commit -m 'sent pushed filed'"})]))
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []


# ----- C5 ------------------------------------------------------------------

def test_c5_incomplete_block_no_row_then_row_once_complete(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    lines = block("msg_A", GOOD, results=False) + [res("msg_A-t0")]  # second call has no result
    write(t, lines)
    sbs.evaluate(str(t), "s1", "pretooluse")
    assert rows(home) == []
    write(t, lines + [res("msg_A-t1")])
    sbs.evaluate(str(t), "s1", "stop")
    assert len(rows(home)) == 1
    sbs.evaluate(str(t), "s1", "stop")
    assert len(rows(home)) == 1


def test_c5_partly_flushed_block_no_row(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    # only the first tool_use line is on disk; the sibling line is not yet flushed, and its result is absent
    first, second = block("msg_A", GOOD, results=False)
    write(t, [first])
    sbs.evaluate(str(t), "s1", "pretooluse")
    assert rows(home) == []
    write(t, [first, second, res("msg_A-t0"), res("msg_A-t1")])
    sbs.evaluate(str(t), "s1", "pretooluse")
    assert len(rows(home)) == 1


def test_c5_an_incomplete_block_does_not_hide_a_complete_one(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD) + block("msg_B", GOOD, results=False))
    sbs.evaluate(str(t), "s1", "pretooluse")
    assert [r["message_id"] for r in rows(home)] == ["msg_A"]


# ----- C6: a real multi-process race ----------------------------------------

_RACER = textwrap.dedent("""
    import importlib.util, os, sys, time
    sys.path.insert(0, sys.argv[1])
    spec = importlib.util.spec_from_file_location("sbs_racer", os.path.join(sys.argv[1], "_same_batch_shadow.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    # widen the critical section so overlap is certain, not a matter of luck: every append sleeps first
    _real = m._append_line
    def _slow(path, line):
        time.sleep(0.05)
        return _real(path, line)
    m._append_line = _slow
    go = sys.argv[3]
    while not os.path.exists(go):
        time.sleep(0.0005)
    m.evaluate(sys.argv[2], "race", sys.argv[4])
""")


def test_c6_exactly_once_under_concurrency(home, tmp_path, isolate_hook_loads):
    t = tmp_path / "t.jsonl"
    n_blocks, n_procs = 4, 6
    lines = []
    for i in range(n_blocks):
        lines += block(f"msg_R{i}", GOOD)
    write(t, lines)
    go = tmp_path / "go"
    env = {**os.environ, "ENGRAM_HOME": str(home)}
    procs = [subprocess.Popen([sys.executable, "-c", _RACER, str(HOOK_DIR), str(t), str(go),
                               "pretooluse" if i % 2 else "stop"], env=env) for i in range(n_procs)]
    time.sleep(0.5)  # let every racer reach the barrier
    go.write_text("go")
    for p in procs:
        assert p.wait(timeout=60) == 0
    # a racer that finds the lock held now skips (never waits); a later fire writes what is left, once
    mod = _load()
    mod.evaluate(str(t), "race", "stop")
    got = sorted(r["message_id"] for r in rows(home))
    assert got == sorted(f"msg_R{i}" for i in range(n_blocks))


# ----- C7: never raises, never alters the host hook --------------------------

def test_c7_evaluate_returns_silently_on_every_bad_input(sbs, home, tmp_path, capsys):
    good = tmp_path / "good.jsonl"
    write(good, block("msg_A", GOOD))
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json\n{\"type\":\"assistant\"\n[1,2]\n\"x\"\n123\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(good)
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    d = tmp_path / "adir"
    d.mkdir()
    for p in (tmp_path / "missing.jsonl", empty, garbage, link, fifo, d, "", None):
        assert sbs.evaluate(str(p) if p is not None else None, "s1", "stop") is None
    assert capsys.readouterr().out == ""
    assert rows(home) == []  # the symlinked transcript was refused, none of the rest holds a flagged block


def test_c7_malformed_line_between_good_lines_does_not_hide_the_block(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    lines = block("msg_A", GOOD)
    write(t, lines[:1] + ["{broken"] + lines[1:])
    sbs.evaluate(str(t), "s1", "stop")
    assert len(rows(home)) == 1


def test_c7_symlink_and_fifo_at_the_log_path_are_refused_silently(sbs, home, tmp_path, capsys):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    (home / LEDGER).symlink_to(victim)
    assert sbs.evaluate(str(t), "s1", "stop") is None
    assert victim.read_text() == "keep\n"
    (home / LEDGER).unlink()
    os.mkfifo(home / LEDGER)
    t0 = time.time()
    assert sbs.evaluate(str(t), "s1", "stop") is None  # must not hang on the reader-less FIFO
    assert time.time() - t0 < 5
    assert capsys.readouterr().out == ""


def test_c7_symlinked_dedupe_dir_is_refused(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / "same-batch-shadow").symlink_to(elsewhere)
    assert sbs.evaluate(str(t), "s1", "stop") is None
    assert list(elsewhere.iterdir()) == []
    assert rows(home) == []


def test_c7_read_only_home_is_silent(sbs, home, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    home.chmod(0o500)
    try:
        assert sbs.evaluate(str(t), "s1", "stop") is None
    finally:
        home.chmod(0o700)
    assert rows(home) == []


def test_c7_missing_home_is_not_created(sbs, tmp_path, monkeypatch):
    ghost = tmp_path / "ghost_home"
    monkeypatch.setenv("ENGRAM_HOME", str(ghost))
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    assert sbs.evaluate(str(t), "s1", "stop") is None
    assert not ghost.exists()


def test_c7_relative_engram_home_is_silent(sbs, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", "relative/home")
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    assert sbs.evaluate(str(t), "s1", "stop") is None


def _flat_layout(tmp_path, shadow_src: str | None = None) -> Path:
    """A deployed-style flat hooks/ dir (the build drops the claude/ level)."""
    d = tmp_path / ("plugin_" + str(len(list(tmp_path.glob("plugin_*")))))
    hooks = d / "hooks"
    hooks.mkdir(parents=True)
    for f in HOOK_DIR.iterdir():
        if f.is_file():
            shutil.copy2(f, hooks / f.name)
    if shadow_src is not None:
        (hooks / "_same_batch_shadow.py").write_text(shadow_src)
    return hooks


RAISING = "def evaluate(*a, **k):\n    raise RuntimeError('forced')\n"
RAISING_AT_IMPORT = "raise RuntimeError('forced at import')\n"


def _run_hook(hooks: Path, name: str, payload: dict, home: Path, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_ROOT"}
    env.update({"ENGRAM_HOME": str(home), "HOME": str(home.parent)})
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, str(hooks / name)], input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=60)
    return p


def _tripwire_payload(t, sid="s1"):
    return {"session_id": sid, "transcript_path": str(t), "hook_event_name": "PreToolUse",
            "tool_name": "Bash", "tool_input": {"command": "echo hello"}}


def _stop_payload(t, sid="s1", active=False):
    return {"session_id": sid, "transcript_path": str(t), "hook_event_name": "Stop", "stop_hook_active": active}


@pytest.mark.parametrize("hook,mk", [(TRIPWIRE, _tripwire_payload), (STOP, _stop_payload)])
@pytest.mark.parametrize("forced", [RAISING, RAISING_AT_IMPORT])
def test_c7_real_hook_output_is_identical_with_the_module_forced_to_raise(tmp_path, hook, mk, forced):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    h1, h2 = tmp_path / "home1", tmp_path / "home2"
    h1.mkdir()
    h2.mkdir()
    enable(h1)
    enable(h2)
    real = _run_hook(_flat_layout(tmp_path), hook, mk(t), h1)
    sabotaged = _run_hook(_flat_layout(tmp_path, forced), hook, mk(t), h2)
    assert real.stdout == sabotaged.stdout
    assert real.returncode == sabotaged.returncode
    assert "same-batch" not in real.stdout
    assert len(rows(h1)) == 1          # the real module logged ...
    assert rows(h2) == []              # ... the forced one did not, and the hook did not notice


@pytest.mark.parametrize("hook,mk", [(TRIPWIRE, _tripwire_payload), (STOP, _stop_payload)])
def test_c7_real_hook_with_a_bad_transcript_and_a_fifo_log_is_unchanged(tmp_path, hook, mk):
    h1, h2 = tmp_path / "home1", tmp_path / "home2"
    h1.mkdir()
    h2.mkdir()
    enable(h1)
    enable(h2)
    os.mkfifo(h1 / LEDGER)
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    real = _run_hook(_flat_layout(tmp_path), hook, mk(t), h1)
    base = _run_hook(_flat_layout(tmp_path, RAISING), hook, mk(tmp_path / "missing.jsonl"), h2)
    assert (real.stdout, real.returncode) == (base.stdout, base.returncode)


# ----- C8: bounded read -------------------------------------------------------

def test_c8_only_the_tail_is_read_and_an_early_block_is_skipped(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    pad = [json.dumps({"type": "system", "x": "p" * 1000}) for _ in range(int(sbs.SHADOW_TAIL_BYTES * 1.5 / 1000))]
    # the window's first complete line is a pad record, not part of any block, so msg_FIRST started
    # inside the window and is judged (round 3); msg_EARLY is before the window and is not.
    write(t, block("msg_EARLY", GOOD) + pad + block("msg_FIRST", GOOD) + block("msg_LATE", GOOD))
    assert t.stat().st_size > sbs.SHADOW_TAIL_BYTES * 1.4
    sbs.evaluate(str(t), "s1", "stop")
    assert [r["message_id"] for r in rows(home)] == ["msg_FIRST", "msg_LATE"]


def test_c8_a_block_that_starts_before_the_window_is_skipped_not_half_read(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    n = int(sbs.SHADOW_TAIL_BYTES * 1.2 / 1000)
    pad = [json.dumps({"type": "system", "x": "p" * 1000}) for _ in range(n)]
    straddle = block("msg_CUT", GOOD, results=False)
    # first line of the straddling block lies before the window; its second line, and the results, are inside
    tail_lines = straddle[1:] + [res("msg_CUT-t0"), res("msg_CUT-t1")] + block("msg_OK", GOOD)
    tail_bytes = len(("\n".join(tail_lines) + "\n").encode())
    gap = sbs.SHADOW_TAIL_BYTES - tail_bytes
    assert gap > 3000
    # put straddle[0] so that it ends up just before the window start
    fill = [json.dumps({"type": "system", "x": "q" * 1000}) for _ in range((gap // 1000) - 1)]
    write(t, pad + [straddle[0]] + fill + tail_lines)
    assert t.stat().st_size > sbs.SHADOW_TAIL_BYTES
    sbs.evaluate(str(t), "s1", "stop")
    ids = [r["message_id"] for r in rows(home)]
    assert "msg_CUT" not in ids and "msg_OK" in ids


def test_c8_first_partial_line_is_discarded(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    # one giant first line whose tail falls inside the window, then a good block; the giant line
    # is a tool_use of msg_G whose sibling is also inside the window.
    big = tu("msg_G", "g0", *crossing("I sent it " + "z" * (sbs.SHADOW_TAIL_BYTES)))
    lines = [big, tu("msg_G", "g1", *push()), res("g0"), res("g1")] + block("msg_OK", GOOD)
    write(t, lines)
    sbs.evaluate(str(t), "s1", "stop")
    ids = [r["message_id"] for r in rows(home)]
    assert ids == ["msg_OK"]


def test_c8_window_limit_can_be_turned_off_for_a_whole_read(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    pad = [json.dumps({"type": "system", "x": "p" * 1000}) for _ in range(int(sbs.SHADOW_TAIL_BYTES * 1.5 / 1000))]
    write(t, block("msg_EARLY", GOOD) + pad + block("msg_LATE", GOOD))
    sbs.evaluate(str(t), "s1", "stop", tail_bytes=None)
    assert sorted(r["message_id"] for r in rows(home)) == ["msg_EARLY", "msg_LATE"]


def _windowed(path, tail_lines, sbs, pre_mid=None):
    """A file larger than the window whose window starts INSIDE a long pre-record, so the first
    complete line of the window is tail_lines[0] (the pre-record's remainder is the discarded partial)."""
    tail_bytes = len(("\n".join(tail_lines) + "\n").encode())
    assert tail_bytes < sbs.SHADOW_TAIL_BYTES - 20000
    filler = "p" * (sbs.SHADOW_TAIL_BYTES - tail_bytes + 5000)
    if pre_mid is None:
        pre = json.dumps({"type": "system", "x": filler})
    else:  # the line before the window is an assistant record of message pre_mid (a genuinely cut block)
        pre = json.dumps({"type": "assistant", "message": {"id": pre_mid, "role": "assistant",
                                                             "content": [{"type": "text", "text": filler}]}})
    write(path, [pre] + tail_lines)
    assert path.stat().st_size > sbs.SHADOW_TAIL_BYTES


LONG = json.dumps({"type": "user", "message": {"role": "user", "content": "L" * 100000}})


def test_c8_round3_a_sparse_tail_one_long_line_then_one_whole_block_logs_a_row(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    _windowed(t, [LONG] + block("msg_ONLY", GOOD), sbs)
    sbs.evaluate(str(t), "s1", "stop")
    assert [r["message_id"] for r in rows(home)] == ["msg_ONLY"]


def test_c8_round3_first_complete_line_is_an_assistant_record_of_the_block_is_skipped(sbs, home, tmp_path):
    # round 4: skipped only when the line before it is an assistant record of the SAME message
    t = tmp_path / "t.jsonl"
    _windowed(t, block("msg_CUTPOSSIBLE", GOOD), sbs, pre_mid="msg_CUTPOSSIBLE")
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []


def test_c8_round3_assistant_first_line_with_a_non_message_line_before_it_is_kept(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    _windowed(t, block("msg_STARTS_INSIDE", GOOD), sbs)   # the line before is a system record
    sbs.evaluate(str(t), "s1", "stop")
    assert [r["message_id"] for r in rows(home)] == ["msg_STARTS_INSIDE"]


def test_c8_round3_first_complete_line_is_assistant_text_with_the_same_message_id_is_skipped(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    text_rec = json.dumps({"type": "assistant", "message": {"id": "msg_T", "role": "assistant",
                                                              "content": [{"type": "text", "text": "hello"}]}})
    _windowed(t, [text_rec] + block("msg_T", GOOD), sbs, pre_mid="msg_T")
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []


def test_c8_round3_first_complete_line_is_another_messages_assistant_record_keeps_the_block(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    other = json.dumps({"type": "assistant", "message": {"id": "msg_OTHER", "role": "assistant",
                                                           "content": [{"type": "text", "text": "x"}]}})
    _windowed(t, [other] + block("msg_K", GOOD), sbs)
    sbs.evaluate(str(t), "s1", "stop")
    assert [r["message_id"] for r in rows(home)] == ["msg_K"]


def test_c8_round3_first_complete_line_is_a_tool_result_keeps_the_block(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    _windowed(t, [res("orphan-from-before-the-window")] + block("msg_K", GOOD), sbs)
    sbs.evaluate(str(t), "s1", "stop")
    assert [r["message_id"] for r in rows(home)] == ["msg_K"]


def test_c8_round3_unparseable_first_complete_line_keeps_the_conservative_skip(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    _windowed(t, ["{this is not json"] + block("msg_K", GOOD), sbs)
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []


def test_c8_round3_real_tripwire_entry_sparse_tail_logs_a_row(sbs, tmp_path):
    t = tmp_path / "t.jsonl"
    _windowed(t, [LONG] + block("msg_ONLY", GOOD), sbs)
    h = tmp_path / "home"
    h.mkdir()
    enable(h)
    p = _run_hook(_flat_layout(tmp_path), TRIPWIRE, _tripwire_payload(t), h)
    assert p.returncode == 0
    assert [r["message_id"] for r in rows(h)] == ["msg_ONLY"]


# ----- round 4: the window cut inside the line BEFORE a whole block ------------------------------------

def _bytes(lines):
    return len(("\n".join(lines) + "\n").encode())


def _turn(i):
    """A plain earlier turn: an assistant text record and a user line (other message ids)."""
    return [json.dumps({"type": "assistant", "message": {"id": f"msg_plain{i}", "role": "assistant",
                                                           "content": [{"type": "text", "text": "t" * 40}]}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "u" * 60}})]


def _variant_block(kind, mid="msg_SW"):
    use0 = tu(mid, f"{mid}-t0", *GOOD[0])
    use1 = tu(mid, f"{mid}-t1", *GOOD[1])
    results = [res(f"{mid}-t0"), res(f"{mid}-t1")]
    if kind == "plain":
        return [use0, use1] + results
    if kind == "progress":
        return [use0, json.dumps({"type": "progress", "data": {"x": 1}}), use1] + results
    if kind == "thinking":
        think = json.dumps({"type": "assistant", "message": {"id": mid, "role": "assistant",
                                                              "content": [{"type": "thinking", "thinking": "hm"}]}})
        return [think, use0, use1] + results
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["plain", "progress", "thinking"])
def test_round4_sweep_every_window_size_that_cuts_inside_the_line_before_a_whole_block_keeps_it(sbs, home, tmp_path, kind):
    prev = json.dumps({"type": "user", "message": {"role": "user", "content": "R" * 700}})
    blk = _variant_block(kind)
    lines = _turn(0) + _turn(1) + _turn(2) + [prev] + blk
    t = tmp_path / "t.jsonl"
    write(t, lines)
    blk_bytes = _bytes(blk)
    lo, hi = blk_bytes, blk_bytes + len(prev.encode()) + 1 + 200
    lost = []
    sizes = list(range(lo, hi + 1))
    assert lo + len(prev.encode()) in sizes and lo in sizes   # exact boundary (lo) and the far edge are covered
    for n in sizes:
        sess = f"sw{n}"
        sbs.evaluate(str(t), sess, "stop", tail_bytes=n)
        if not any(r["session_id"] == sess and r["message_id"] == "msg_SW" for r in rows(home)):
            lost.append(n)
    assert lost == [], "whole block lost at window sizes %s..%s (%d sizes)" % (lost[:1], lost[-1:], len(lost))


def test_round4_a_block_genuinely_cut_previous_line_same_message_is_skipped(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    blk = block("msg_CUT2", GOOD)
    pre = json.dumps({"type": "system", "x": "p" * 3000})
    write(t, [pre] + blk)
    after_t0 = _bytes(blk[1:])
    for n, label in ((after_t0 + 5, "inside the first line"), (after_t0, "exactly on the boundary")):
        sbs.evaluate(str(t), "cut-" + str(n), "stop", tail_bytes=n)
        assert not any(r["message_id"] == "msg_CUT2" for r in rows(home)), label


def test_round4_previous_line_longer_than_the_back_read_bound_is_a_conservative_skip(sbs, home, tmp_path, monkeypatch):
    assert sbs.SHADOW_BACKREAD_BYTES == 1024 * 1024
    t = tmp_path / "t.jsonl"
    prev = json.dumps({"type": "user", "message": {"role": "user", "content": "R" * (sbs.SHADOW_BACKREAD_BYTES + 5000)}})
    blk = block("msg_LONGPREV", GOOD)
    write(t, [prev] + blk)
    read = []
    real = os.pread

    def spy(fd, n, off):
        b = real(fd, n, off)
        read.append(len(b))
        return b
    monkeypatch.setattr(os, "pread", spy)
    assert sbs.evaluate(str(t), "s1", "stop", tail_bytes=_bytes(blk) + 1000) is None
    assert rows(home) == []
    assert 0 < sum(read) <= sbs.SHADOW_BACKREAD_BYTES


def test_round4_no_back_read_when_the_first_complete_line_is_not_an_assistant_record(sbs, home, tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    _windowed(t, [LONG] + block("msg_ONLY", GOOD), sbs)
    calls = []
    real = os.pread
    monkeypatch.setattr(os, "pread", lambda *a: calls.append(a) or real(*a))
    sbs.evaluate(str(t), "s1", "stop")
    assert calls == []
    assert [r["message_id"] for r in rows(home)] == ["msg_ONLY"]


def test_round4_back_read_only_in_the_assistant_first_line_branch(sbs, home, tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    _windowed(t, block("msg_CP", GOOD), sbs)   # first complete line IS an assistant record
    calls = []
    real = os.pread
    monkeypatch.setattr(os, "pread", lambda *a: calls.append(a) or real(*a))
    sbs.evaluate(str(t), "s1", "stop")
    assert len(calls) >= 1


def test_round4_real_tripwire_entry_cut_inside_the_previous_line_logs_a_row(sbs, tmp_path):
    t = tmp_path / "t.jsonl"
    blk = block("msg_REAL", GOOD)
    prev = json.dumps({"type": "user", "message": {"role": "user", "content": "R" * sbs.SHADOW_TAIL_BYTES}})
    write(t, _turn(0) + [prev] + blk)
    assert _bytes(blk) < sbs.SHADOW_TAIL_BYTES < _bytes([prev] + blk)
    h = tmp_path / "home"
    h.mkdir()
    enable(h)
    p = _run_hook(_flat_layout(tmp_path), TRIPWIRE, _tripwire_payload(t), h)
    assert p.returncode == 0
    assert [r["message_id"] for r in rows(h)] == ["msg_REAL"]


def test_round4_docstring_states_the_two_bounds():
    src = MODULE_FILE.read_text()
    assert "fewer `calls`" in src and "not pruned" in src


def test_c8_window_is_256_kib(sbs):
    assert sbs.SHADOW_TAIL_BYTES == 256 * 1024


# ----- C9: config and harness --------------------------------------------------

def test_c9_disabled_by_config_means_no_read_and_no_write(sbs, home, tmp_path):
    (home / "config.json").write_text(json.dumps({"same_batch_shadow": {"enabled": False}}))
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == [] and not (home / "same-batch-shadow").exists()


@pytest.mark.parametrize("cfg", [None, "{}", '{"same_batch_shadow": {}}', '{"same_batch_shadow": {"enabled": false}}',
                                 '{"same_batch_shadow": {"enabled": "true"}}', '{"same_batch_shadow": {"enabled": 1}}',
                                 '{"same_batch_shadow": {"enabled": null}}', '{"same_batch_shadow": true}',
                                 "{not json", '{"same_batch_shadow": 7}'])
def test_c9_default_is_off_only_boolean_true_enables(sbs, home, tmp_path, monkeypatch, cfg):
    cp = home / "config.json"
    cp.unlink()
    if cfg is not None:
        cp.write_text(cfg)
    called = []
    monkeypatch.setattr(sbs, "_read_tail", lambda *a, **k: called.append(1) or ("", False, None))
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    sbs.evaluate(str(t), "s1", "stop")
    assert called == [] and rows(home) == [] and not (home / "same-batch-shadow").exists()


def test_c9_boolean_true_enables(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    sbs.evaluate(str(t), "s1", "stop")
    assert len(rows(home)) == 1


@pytest.mark.parametrize("hook,mk", [(TRIPWIRE, _tripwire_payload), (STOP, _stop_payload)])
def test_c9_absent_config_real_hook_behaves_as_without_the_code(tmp_path, hook, mk):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    h1, h2 = tmp_path / "home1", tmp_path / "home2"
    h1.mkdir()
    h2.mkdir()
    real = _run_hook(_flat_layout(tmp_path), hook, mk(t), h1)
    without = _run_hook(_flat_layout(tmp_path, RAISING), hook, mk(t), h2)
    assert (real.stdout, real.returncode) == (without.stdout, without.returncode)
    assert sorted(p.name for p in h1.iterdir()) == sorted(p.name for p in h2.iterdir())
    assert rows(h1) == [] and not (h1 / "same-batch-shadow").exists()


def test_c9_non_claude_harness_is_a_noop(sbs, home, tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    monkeypatch.setattr(sbs, "detect_harness", lambda: "codex")
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home) == []
    monkeypatch.setattr(sbs, "detect_harness", lambda: "claude-code")
    sbs.evaluate(str(t), "s1", "stop")
    assert len(rows(home)) == 1
    monkeypatch.setattr(sbs, "detect_harness", lambda: None)  # source tree: no platform.json
    sbs.evaluate(str(t), "s2", "stop")
    assert len(rows(home)) == 2


# ----- C10: row shape and privacy ------------------------------------------------

def test_c10_row_fields_only_and_no_body_or_input(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    calls = [crossing("I sent " + DISTINCT), ("Bash", {"command": "git push origin " + DISTINCT})]
    write(t, block("msg_A", calls))
    sbs.evaluate(str(t), "sess-9", "stop")
    raw = (home / LEDGER).read_text()
    assert DISTINCT not in raw
    for f in (home / "same-batch-shadow").rglob("*"):
        if f.is_file():
            assert DISTINCT not in f.read_text()
    (r,) = rows(home)
    assert set(r) == {"ts", "session_id", "message_id", "source", "rule", "rule_sha", "calls", "tools", "any_error"}
    assert r["session_id"] == "sess-9" and r["message_id"] == "msg_A" and r["source"] == "stop"
    assert r["rule"] == "A" and r["rule_sha"] == RULE_SHA and r["calls"] == 2
    assert r["tools"] == ["send_crossing", "Bash"] and r["any_error"] is False
    assert r["ts"].endswith("Z") and "T" in r["ts"]


def test_c10_any_error_true_if_any_call_errored(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD, errs=(1,)))
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home)[0]["any_error"] is True


def test_c10_tool_names_keep_only_the_last_segment(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", [("mcp__server__with__parts__send_crossing", crossing("sent")[1]), push()]))
    sbs.evaluate(str(t), "s1", "stop")
    assert rows(home)[0]["tools"] == ["send_crossing", "Bash"]


# ----- C11: paths and regular-io discipline -----------------------------------------

def test_c11_paths(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    sbs.evaluate(str(t), "s1", "stop")
    assert (home / LEDGER).is_file()
    d = home / "same-batch-shadow"
    assert d.is_dir() and any(d.iterdir())
    assert sorted(p.name for p in home.iterdir() if p.name != "config.json") == sorted([LEDGER, "same-batch-shadow"])


def test_c11_no_plain_open_in_the_module_source():
    import ast
    tree = ast.parse(MODULE_FILE.read_text())
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == "open":
                bad.append(n.lineno)
            if isinstance(f, ast.Attribute) and f.attr in ("open", "read_text", "write_text", "read_bytes", "write_bytes") \
                    and not (isinstance(f.value, ast.Name) and f.value.id == "os" and f.attr != "open"):
                bad.append(n.lineno)
    assert bad == [], "plain open/read/write on a path at lines %s" % bad


def test_c11_hard_linked_ledger_is_refused(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    other = tmp_path / "other.txt"
    other.write_text("keep\n")
    os.link(other, home / LEDGER)
    sbs.evaluate(str(t), "s1", "stop")
    assert other.read_text() == "keep\n"


def test_c11_failed_log_write_is_retried_on_a_later_fire(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    os.mkfifo(home / LEDGER)
    sbs.evaluate(str(t), "s1", "stop")
    (home / LEDGER).unlink()
    sbs.evaluate(str(t), "s1", "stop")
    assert len(rows(home)) == 1


# ----- C12: Stop re-entry ----------------------------------------------------------

def test_c12_stop_hook_active_true_returns_before_the_call_no_row_no_output(tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    h = tmp_path / "home"
    h.mkdir()
    enable(h)
    hooks = _flat_layout(tmp_path)
    again = _run_hook(hooks, STOP, _stop_payload(t, active=True), h)
    assert again.returncode == 0 and again.stdout == ""
    assert rows(h) == [] and not (h / "same-batch-shadow").exists()


def test_c12_active_fire_adds_no_duplicate_to_an_existing_row(tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    h = tmp_path / "home"
    h.mkdir()
    enable(h)
    hooks = _flat_layout(tmp_path)
    first = _run_hook(hooks, STOP, _stop_payload(t), h)
    again = _run_hook(hooks, STOP, _stop_payload(t, active=True), h)
    third = _run_hook(hooks, TRIPWIRE, _tripwire_payload(t), h)
    assert len(rows(h)) == 1
    assert again.stdout == ""
    assert "same-batch" not in first.stdout.lower() + third.stdout.lower()


def test_c12_the_stop_call_runs_on_every_stop_in_or_out_of_a_loop(tmp_path):
    # the call sits before the loop-marker gate: no loop-mode.json is present here and a row still lands
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    h = tmp_path / "home"
    h.mkdir()
    enable(h)
    assert not (h / "loop-mode.json").exists()
    _run_hook(_flat_layout(tmp_path), STOP, _stop_payload(t), h)
    assert len(rows(h)) == 1


# ----- deployed layout ---------------------------------------------------------------

def test_deployed_flat_layout_imports_and_logs_with_plugin_root_unset(tmp_path):
    hooks = _flat_layout(tmp_path)
    # a flat hooks/ with ONLY the module's real dependencies, no src/ tree anywhere above it
    only = tmp_path / "only" / "hooks"
    only.mkdir(parents=True)
    for n in ("_same_batch_shadow.py", "_hooklib.py", "_harness.py"):
        shutil.copy2(hooks / n, only / n)
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    h = tmp_path / "home"
    h.mkdir()
    enable(h)
    code = ("import sys; sys.path.insert(0, %r); import _same_batch_shadow as m; m.evaluate(%r, 's1', 'stop')"
            % (str(only), str(t)))
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH")}
    env["ENGRAM_HOME"] = str(h)
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=str(tmp_path))
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""
    assert len(rows(h)) == 1


def test_module_has_no_fixed_parents_walk():
    src = MODULE_FILE.read_text()
    assert ".parents" not in src and "parents[" not in src


# ----- hook wiring ----------------------------------------------------------------------

@pytest.mark.parametrize("hook", [TRIPWIRE, STOP])
def test_each_hook_has_exactly_one_call_site_and_it_is_fail_open(hook):
    import ast
    src = (HOOK_DIR / hook).read_text()
    tree = ast.parse(src)
    sites = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_sbs_evaluate"]
    assert len(sites) == 1
    # fail-open: the call and its import sit inside one try whose handler swallows Exception
    tries = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and any(
        isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_sbs_evaluate" for c in ast.walk(n))]
    assert tries and all(any(getattr(h.type, "id", "") == "Exception" for h in t.handlers) for t in tries)


def test_loop_arm_call_sits_after_the_payload_try_and_before_the_loop_marker_gate():
    src = (HOOK_DIR / STOP).read_text()
    i_call = src.index("_sbs_evaluate(")
    i_active = src.index('payload.get("stop_hook_active", False)')
    i_gate = src.index("if not os.path.exists(LOOP_MARKER_PATH):\n        return\n\n    # A stale")
    assert i_active < i_call < i_gate


def test_the_essential_stop_hook_does_not_carry_the_call():
    assert "_same_batch_shadow" not in (HOOK_DIR / ESSENTIAL_STOP).read_text()


def test_hooks_json_is_untouched_by_this_change():
    hj = PROJECT_ROOT / "src" / "engram" / "hooks" / "hooks.json"
    if not hj.exists():
        pytest.skip("hooks.json lives elsewhere in this layout")
    assert "same_batch" not in hj.read_text() and "same-batch" not in hj.read_text()


def test_tiered_with_the_lesson_tripwire():
    tiers = json.loads((PROJECT_ROOT / "src" / "build" / "packaging" / "tiers.json").read_text())
    entries = tiers if isinstance(tiers, list) else tiers.get("entries") or tiers.get("shippables") or tiers
    flat = json.dumps(tiers)
    assert "hooks/claude/_same_batch_shadow.py" in flat

    def find(path):
        stack = [tiers]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if x.get("path") == path:
                    return x
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    a = find("hooks/claude/_same_batch_shadow.py")
    b = find("hooks/claude/engram-lesson-tripwire-hook.py")
    # Both importers (the tripwire and the loop-arm Stop hook) are convenience, so the module ranks with
    # them. This fails if it goes back to essential (or to anything but convenience).
    c = find("hooks/claude/engram-loop-arm-stop-hook.py")
    assert a["tier"] == b["tier"] == c["tier"] == "convenience"
    assert a["dest"] == "hooks/_same_batch_shadow.py"


# ----- C13: log mode 600 ----------------------------------------------------------------

def test_c13_log_is_created_mode_600_even_under_umask_zero(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    old = os.umask(0)
    try:
        sbs.evaluate(str(t), "s1", "stop")
    finally:
        os.umask(old)
    assert (home / LEDGER).stat().st_mode & 0o777 == 0o600


def test_c13_preexisting_wider_log_is_not_widened(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    (home / LEDGER).write_text("")
    (home / LEDGER).chmod(0o640)
    sbs.evaluate(str(t), "s1", "stop")
    assert (home / LEDGER).stat().st_mode & 0o777 == 0o640 and len(rows(home)) == 1


# ----- C14: size cap ---------------------------------------------------------------------

def _fill_to_cap(home, sbs, extra=0):
    pad = b"x" * (sbs.SHADOW_LOG_CAP_BYTES + extra - 1) + b"\n"
    (home / LEDGER).write_bytes(pad)


def test_c14_cap_is_5_mib(sbs):
    assert sbs.SHADOW_LOG_CAP_BYTES == 5 * 1024 * 1024


def test_c14_at_the_cap_two_fires_write_one_cap_row_only(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    _fill_to_cap(home, sbs)
    before = (home / LEDGER).stat().st_size
    sbs.evaluate(str(t), "s1", "stop")
    sbs.evaluate(str(t), "s1", "pretooluse")
    data = (home / LEDGER).read_bytes()
    tail = data[before:].decode().splitlines()
    assert len(tail) == 1
    row = json.loads(tail[0])
    assert set(row) == {"event", "ts"} and row["event"] == "cap_reached" and row["ts"].endswith("Z")
    assert b"msg_A" not in data


def test_c14_after_the_cap_row_there_is_no_transcript_read(sbs, home, tmp_path, monkeypatch):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    _fill_to_cap(home, sbs)
    sbs.evaluate(str(t), "s1", "stop")
    called = []
    monkeypatch.setattr(sbs, "_read_tail", lambda *a, **k: called.append(1) or ("", False, None))
    size = (home / LEDGER).stat().st_size
    sbs.evaluate(str(t), "s1", "stop")
    assert called == [] and (home / LEDGER).stat().st_size == size


def test_c14_a_row_that_would_cross_the_cap_is_not_written(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    pad = b"x" * (sbs.SHADOW_LOG_CAP_BYTES - 50 - 1) + b"\n"
    (home / LEDGER).write_bytes(pad)
    sbs.evaluate(str(t), "s1", "stop")
    last = (home / LEDGER).read_bytes()[len(pad):].decode().splitlines()
    assert len(last) == 1 and json.loads(last[0])["event"] == "cap_reached"


def test_c14_below_the_cap_logs_normally(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    (home / LEDGER).write_bytes(b"x" * 1000 + b"\n")
    sbs.evaluate(str(t), "s1", "stop")
    assert json.loads((home / LEDGER).read_text().splitlines()[-1])["message_id"] == "msg_A"


def test_c14_cap_never_fails_the_hook(tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    h, h2 = tmp_path / "home", tmp_path / "home2"
    h.mkdir()
    h2.mkdir()
    enable(h)
    enable(h2)
    (h / LEDGER).write_bytes(b"x" * (5 * 1024 * 1024))
    p = _run_hook(_flat_layout(tmp_path), TRIPWIRE, _tripwire_payload(t), h)
    q = _run_hook(_flat_layout(tmp_path, RAISING), TRIPWIRE, _tripwire_payload(t), h2)
    assert p.returncode == q.returncode and p.stdout == q.stdout


# ----- the lock never adds a wait (round 2) ----------------------------------------------------

_HOLDER = textwrap.dedent("""
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    print("held", flush=True)
    time.sleep(6)
""")


def _hold_lock(home):
    d = Path(home) / "same-batch-shadow"
    d.mkdir(exist_ok=True)
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(d / "lock")], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def _state(home):
    return sorted(str(p.relative_to(home)) for p in Path(home).rglob("*") if p.name != "config.json")


def test_lock_held_by_another_process_evaluate_returns_fast_and_writes_nothing(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    before = None
    holder = _hold_lock(home)
    try:
        before = _state(home)
        t0 = time.time()
        sbs.evaluate(str(t), "s1", "stop")
        assert time.time() - t0 < 0.5
        assert rows(home) == []
        assert _state(home) == before  # no seen file, no ledger, no partial state
    finally:
        holder.kill()
        holder.wait()
    # (b) after the release a later fire writes the row, exactly once
    sbs.evaluate(str(t), "s1", "pretooluse")
    sbs.evaluate(str(t), "s1", "stop")
    assert [r["message_id"] for r in rows(home)] == ["msg_A"]


def test_lock_held_at_the_cap_writes_no_cap_row_then_one_after_release(sbs, home, tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    _fill_to_cap(home, sbs)
    size = (home / LEDGER).stat().st_size
    holder = _hold_lock(home)
    try:
        t0 = time.time()
        sbs.evaluate(str(t), "s1", "stop")
        assert time.time() - t0 < 0.5
        assert (home / LEDGER).stat().st_size == size
    finally:
        holder.kill()
        holder.wait()
    sbs.evaluate(str(t), "s1", "stop")
    sbs.evaluate(str(t), "s1", "stop")
    tail = (home / LEDGER).read_bytes()[size:].decode().splitlines()
    assert len(tail) == 1 and json.loads(tail[0])["event"] == "cap_reached"


def test_lock_held_real_hook_returns_promptly_and_output_is_unchanged(tmp_path):
    t = tmp_path / "t.jsonl"
    write(t, block("msg_A", GOOD))
    h1, h2 = tmp_path / "home1", tmp_path / "home2"
    h1.mkdir()
    h2.mkdir()
    enable(h1)
    enable(h2)
    holder = _hold_lock(h1)
    try:
        t0 = time.time()
        real = _run_hook(_flat_layout(tmp_path), TRIPWIRE, _tripwire_payload(t), h1)
        elapsed = time.time() - t0
    finally:
        holder.kill()
        holder.wait()
    base = _run_hook(_flat_layout(tmp_path, RAISING), TRIPWIRE, _tripwire_payload(t), h2)
    assert (real.stdout, real.returncode) == (base.stdout, base.returncode)
    assert elapsed < 5  # a blocked flock would sit until the holder's 6 s sleep ends
    assert rows(h1) == []


def test_module_never_takes_a_blocking_flock():
    import ast
    tree = ast.parse(MODULE_FILE.read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "flock"]
    assert calls
    for c in calls:
        assert "LOCK_NB" in ast.unparse(c), ast.unparse(c)
