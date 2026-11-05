"""Contract tests: compact after every passing nap by default, and surface a refusal (#4350, part of #3640).

Lei, 2026-11-04: "I'd like the auto-after-nap-compaction to be a default feature and power users
can opt out." One switch, ``compaction.nap_backstop`` (absent = ON, only a literal false = OFF),
governs the backstop. These cells were written BEFORE the implementation and run red at base.

Cells:
  N1  no loop-mode.json, a passing nap -> the backstop runs the tool and a request is written
  N2  switch false, with or without a loop marker -> no tool run, no request
  N3  a refusal -> backstop-last.json has outcome refused and a non-empty reason naming the briefing check
      (a success records no reason; a stub exit 4 records the tool's first stderr line)
  N4  the surface hook's next run emits exactly one line with "Nap compaction not requested" and the
      reason; a second run emits none; a NEW refused attempt (new checkpoint_at) emits again
  N5  success -> the surface hook emits no such line
  N6  a corrupt backstop-last.json -> the surface hook emits no such line and does not raise
  N8  the line sits in the head, so an oversized body does not cut it; it is stamped only when the
      rendered context really holds it (a render that drops it leaves it for the next prompt)
  N9  the same checkpoint with a new briefing mtime surfaces again (the stamp key is the pair)
  N10 another session's refusal (record session_id differs from the prompt's) is neither shown nor stamped
  N7  (the 4012 cells that asserted "no loop marker -> no run" were updated; see that file)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_nap_compact_backstop_4012 as t4012  # noqa: E402  (its helpers drive the real Stop hook)

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

SURFACE_HOOK = t4012.HOOK_DIR / "engram-surface-hook.py"
LINE_MARKER = "Nap compaction not requested"
SECOND_HEADER = "2026-10-21T12:00:00"


# ---------------------------------------------------------------------------
# the surface hook, driven through main() (the critical-warning tests' pattern)
# ---------------------------------------------------------------------------

def _load_surface(home: Path):
    spec = importlib.util.spec_from_file_location("engram_surface_hook_4350", SURFACE_HOOK)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"ENGRAM_HOME": str(home)}):
        spec.loader.exec_module(mod)
    assert mod.ENGRAM_HOME == str(home), "the hook did not resolve against the test's ENGRAM_HOME"
    return mod


def surface_context(home: Path, *, session_id: str | None = t4012.SESSION, write_reminder: str | None = None,
                    emit_stub=None) -> str:
    """One UserPromptSubmit fire against `home`; the additionalContext it printed ('' when none).

    `session_id` goes into the stdin payload; `write_reminder` replaces the write-reminder block (a way
    to make the body big); `emit_stub` replaces emit_context (a way to make it drop everything).
    """
    hook = _load_surface(home)
    out = io.StringIO()
    payload = {"prompt": "hello, what's next?"}
    if session_id is not None:
        payload["session_id"] = session_id

    def _no_spawn(*_a, **_k):
        raise OSError("test: relaunch spawn disabled")

    with patch.object(hook, "query_daemon", return_value=None), \
         patch.object(hook, "query_fts_fallback", return_value={}), \
         patch.object(hook, "_spawn_daemon_relaunch", _no_spawn), \
         patch("sys.stdin", io.StringIO(json.dumps(payload))), \
         patch("sys.stdout", out):
        with contextlib.ExitStack() as stack:
            if write_reminder is not None:
                stack.enter_context(patch.object(hook, "check_write_reminder", return_value=write_reminder))
            if emit_stub is not None:
                stack.enter_context(patch.object(hook, "emit_context", emit_stub))
            try:
                hook.main()
            except SystemExit:
                pass
    text = out.getvalue().strip()
    if not text:
        return ""
    return json.loads(text).get("hookSpecificOutput", {}).get("additionalContext", "")


def marker_lines(context: str) -> list[str]:
    return [ln for ln in context.splitlines() if LINE_MARKER in ln]


def refused_home(tmp_path: Path, **kw) -> Path:
    """A home whose passing-nap evidence exists but whose briefing predates the checkpoint -> the tool refuses."""
    home = t4012.make_home(tmp_path, briefing_after=False, **kw)
    done = t4012.real_stop(home)
    assert done.returncode == 0, done.stderr
    assert t4012.backstop_last(home)["outcome"] == "refused"
    return home


@pytest.fixture(scope="module")
def stub_plugin() -> Path:
    import atexit
    import shutil
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="backstop4350_stub_"))
    atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
    t4012._copy_hooks(root / "hooks")
    (root / "tools").mkdir()
    (root / "tools" / "compact-request.py").write_text(t4012.STUB_TOOL, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# N1 / N2  the loop marker no longer gates; the switch still does
# ---------------------------------------------------------------------------

def test_n1_no_loop_marker_a_passing_nap_writes_a_request(tmp_path):
    home = t4012.make_home(tmp_path, loop=False)
    assert not (home / "loop-mode.json").exists()
    done = t4012.real_stop(home)
    assert done.returncode == 0, done.stderr
    req = t4012.request(home)
    assert req is not None and req["session_id"] == t4012.SESSION
    assert t4012.backstop_last(home)["outcome"] == "written"


@pytest.mark.parametrize("loop", [False, True], ids=["no_loop", "loop"])
def test_n2_switch_false_means_no_run_and_no_request(loop, tmp_path, stub_plugin):
    home = t4012.make_home(tmp_path, loop=loop)
    t4012._write_json(home / "config.json", {"compaction": {"nap_backstop": False}})
    t4012.stop(home, stub_plugin)
    assert t4012.stub_calls(home) == []
    real = t4012.real_stop(home)
    assert real.returncode == 0
    assert t4012.request(home) is None
    assert not (home / "compact" / "backstop-last.json").exists()


# ---------------------------------------------------------------------------
# N3  the reason is recorded on a refusal and only then
# ---------------------------------------------------------------------------

def test_n3_a_refusal_records_a_reason_naming_the_briefing_check(tmp_path):
    home = refused_home(tmp_path, loop=False)
    rec = t4012.backstop_last(home)
    assert rec["outcome"] == "refused" and rec["exit_code"] == 3
    assert isinstance(rec.get("reason"), str) and rec["reason"].strip()
    assert "briefing" in rec["reason"].lower()


def test_n3b_the_reason_is_the_tools_first_non_empty_stderr_line_and_capped(tmp_path, stub_plugin):
    home = t4012.make_home(tmp_path)
    (home / "stub-exit").write_text("4")
    t4012.stop(home, stub_plugin)
    rec = t4012.backstop_last(home)
    assert rec["outcome"] == "error" and rec["reason"] == "stub: refused with 4"

    long_tool = tmp_path / "long-plugin"
    t4012._copy_hooks(long_tool / "hooks")
    (long_tool / "tools").mkdir(parents=True)
    (long_tool / "tools" / "compact-request.py").write_text(
        "import sys\nsys.stderr.write('x' * 1000 + '\\n')\nsys.exit(3)\n", encoding="utf-8")
    (tmp_path / "second").mkdir()
    home2 = t4012.make_home(tmp_path / "second")
    t4012.stop(home2, long_tool)
    assert len(t4012.backstop_last(home2)["reason"]) == 300


def test_n3c_a_success_records_no_reason(tmp_path):
    home = t4012.make_home(tmp_path, loop=False)
    t4012.real_stop(home)
    assert t4012.backstop_last(home)["outcome"] == "written"
    assert "reason" not in t4012.backstop_last(home)


# ---------------------------------------------------------------------------
# N4 / N5 / N6  the surface hook
# ---------------------------------------------------------------------------

def test_n4_a_refusal_surfaces_exactly_once_and_a_new_refused_attempt_surfaces_again(tmp_path):
    home = refused_home(tmp_path, loop=False)
    reason = t4012.backstop_last(home)["reason"]

    first = marker_lines(surface_context(home))
    assert len(first) == 1, first
    assert reason in first[0]
    assert first[0].startswith("[" + LINE_MARKER) and "the engram-nap skill" in first[0]

    assert marker_lines(surface_context(home)) == [], "the same refused attempt must not surface twice"

    # a NEW refused attempt: a new checkpoint (and its backstop attempt) surfaces again
    (home / "session_log.md").write_text(f"## Nap (turn 6) — {SECOND_HEADER}\n", encoding="utf-8")
    t4012._write_json(home / "compact" / "last-checkpoint.json",
                      {"checkpoint_at": SECOND_HEADER, "session_id": t4012.SESSION, "backup_ok": True})
    # the briefing (mtime 10:00 - 2min) is still older than the new 12:00 checkpoint -> refused again
    t4012.real_stop(home)
    assert t4012.backstop_last(home)["checkpoint_at"] == SECOND_HEADER
    again = marker_lines(surface_context(home))
    assert len(again) == 1, again
    assert marker_lines(surface_context(home)) == []


def test_n5_a_success_surfaces_nothing(tmp_path):
    home = t4012.make_home(tmp_path, loop=False)
    t4012.real_stop(home)
    assert t4012.backstop_last(home)["outcome"] == "written"
    assert marker_lines(surface_context(home)) == []
    assert not (home / "compact" / "backstop-surfaced.json").exists()


@pytest.mark.parametrize("body", ["{not json", "[1, 2]", '{"outcome": "refused"}', "", '{"outcome": 7}'],
                         ids=["torn", "list", "no_pair", "empty", "bad_outcome"])
def test_n6_a_corrupt_backstop_last_surfaces_nothing_and_does_not_raise(body, tmp_path):
    home = t4012.make_home(tmp_path, loop=False)
    (home / "compact").mkdir(exist_ok=True)
    (home / "compact" / "backstop-last.json").write_text(body, encoding="utf-8")
    assert marker_lines(surface_context(home)) == []


# ---------------------------------------------------------------------------
# N8 / N9 / N10  round 1 of #4354: shown-or-not-stamped, the pair as key, session scope
# ---------------------------------------------------------------------------

def test_n8a_an_oversized_body_does_not_cut_the_refusal_line(tmp_path):
    home = refused_home(tmp_path, loop=False)
    got = marker_lines(surface_context(home, write_reminder="W" * 30000))
    assert len(got) == 1, "the refusal line was cut by the injection budget"


def test_n8b_a_render_that_drops_the_line_leaves_it_unstamped_for_the_next_prompt(tmp_path):
    home = refused_home(tmp_path, loop=False)

    def drops_everything(head, body, **kw):
        return "\u25b8 0 shown \u00b7 N cut"

    assert marker_lines(surface_context(home, emit_stub=drops_everything)) == []
    assert not (home / "compact" / "backstop-surfaced.json").exists(), "stamped a line nobody saw"
    assert len(marker_lines(surface_context(home))) == 1
    assert marker_lines(surface_context(home)) == []


def test_n9_same_checkpoint_new_briefing_mtime_surfaces_again(tmp_path):
    home = refused_home(tmp_path, loop=False)
    assert len(marker_lines(surface_context(home))) == 1
    assert marker_lines(surface_context(home)) == []
    rec = t4012.backstop_last(home)
    t4012._write_json(home / "compact" / "backstop-last.json",
                      dict(rec, briefing_mtime_ns=int(rec["briefing_mtime_ns"]) + 1_000_000_000))
    assert len(marker_lines(surface_context(home))) == 1


def test_n10_another_sessions_refusal_is_neither_shown_nor_stamped(tmp_path):
    home = refused_home(tmp_path, loop=False)
    assert t4012.backstop_last(home)["session_id"] == t4012.SESSION
    assert marker_lines(surface_context(home, session_id="some-other-session")) == []
    assert not (home / "compact" / "backstop-surfaced.json").exists()
    assert len(marker_lines(surface_context(home, session_id=t4012.SESSION))) == 1


def test_n10b_a_null_session_record_surfaces_to_any_session(tmp_path):
    home = refused_home(tmp_path, loop=False)
    rec = t4012.backstop_last(home)
    t4012._write_json(home / "compact" / "backstop-last.json", dict(rec, session_id=None))
    assert len(marker_lines(surface_context(home, session_id="some-other-session"))) == 1
