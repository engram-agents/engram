"""#4139: the loop-arm nudge never names a marker field the marker lacks.

Boot-charge-written markers carry no `loop_prompt` field, yet the nudge told
the agent to "use this file's own `loop_prompt` field". Contract:
  C1  marker has a non-empty str `loop_prompt`  -> today's text, byte-identical.
  C2  absent / null / "" / non-string           -> no mention of the field as
      present; points at tools/loop_prompt.py instead.
  C3  marker unparseable                        -> C2 text, rc 0, no raise.
Harness reused from tests/test_loop_arm_stop_hook.py.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.test_loop_arm_stop_hook import (
    HOOK_PATH,
    _append_lines,
    _make_stop_payload,
    _run_hook,
)

pytestmark = pytest.mark.usefixtures("isolate_hook_loads")

_OLD_SENTENCE = "Use this file's own `loop_prompt` field verbatim for the ScheduleWakeup prompt"
_NEW_SENTENCE = "This marker has no `loop_prompt` field: build the prompt with `python3 "
# #4223 round 2 (Luria item 1): --default-stub for a heartbeat loop, framed as an example; any
# other kind builds the prompt from one line saying what that wake does.
_STUB_FLAG = " --default-stub`"
_OTHER_KIND_EXAMPLE = "e.g. `echo '<one line saying what this wake does>' | python3 "


def _run_nudge(tmp_path: Path, marker_text: str) -> tuple[str, int]:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "loop-mode.json").write_text(marker_text, encoding="utf-8")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    _run_hook(_make_stop_payload(str(transcript)), home)
    _append_lines(
        transcript,
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "prose, no arm"}]}}, separators=(",", ":")),
    )
    return _run_hook(_make_stop_payload(str(transcript)), home)


def _reason(stdout: str) -> str:
    parsed = json.loads(stdout)
    assert parsed["decision"] == "block"
    return parsed["reason"]


def _hook_nudge_text() -> str:
    spec = importlib.util.spec_from_file_location("_loop_arm_4139", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._NUDGE_TEXT


# sha256 of the pre-#4139 _NUDGE_TEXT, read off origin/dev's hook (last changed
# in 58621a81; 1016 chars). Pins C1's byte-identity to the OLD text, not to whatever
# the constant now says: an edit to _NUDGE_HEAD/_NUDGE_TAIL turns this red.
_PRE_4139_NUDGE_SHA256 = "c3ff994ea8788b7c20f32e415940081997ba9ffa716e490d1551a0fb51a632e9"


def test_c1_constant_is_byte_identical_to_pre_4139_text():
    import hashlib
    assert hashlib.sha256(_hook_nudge_text().encode("utf-8")).hexdigest() == _PRE_4139_NUDGE_SHA256


def test_c1_nonempty_loop_prompt_emits_old_text_exactly(tmp_path):
    marker = json.dumps({"kind": "working", "pacer": "scheduleWakeup",
                         "loop_prompt": "<loop-wake> stub"})
    stdout, rc = _run_nudge(tmp_path, marker)
    assert rc == 0
    reason = _reason(stdout)
    assert reason == _hook_nudge_text()
    assert _OLD_SENTENCE in reason


@pytest.mark.parametrize("extra", [
    pytest.param({}, id="absent"),
    pytest.param({"loop_prompt": None}, id="null"),
    pytest.param({"loop_prompt": ""}, id="empty"),
    pytest.param({"loop_prompt": 42}, id="int"),
])
def test_c2_missing_or_bad_loop_prompt_points_at_helper(tmp_path, extra):
    marker = json.dumps({"kind": "working", "pacer": "scheduleWakeup", **extra})
    stdout, rc = _run_nudge(tmp_path, marker)
    assert rc == 0
    reason = _reason(stdout)
    assert "Loop Arm Check" in reason
    assert "this file's own `loop_prompt` field" not in reason
    assert _NEW_SENTENCE in reason
    assert _STUB_FLAG in reason and _OTHER_KIND_EXAMPLE in reason
    assert "Heartbeat wake" not in reason
    assert "loop_prompt.py" in reason
    # rest of the nudge stays identical
    assert "batch `ScheduleWakeup(delay=cadence_seconds)` and `CronList`" in reason
    assert "The CronList result is your verify step" in reason
    assert "(engram-loop Step 2 item 3, #2098)" in reason


def test_c3_unparseable_marker_behaves_as_c2(tmp_path):
    # NOTE (reviewer round 1): end to end, a corrupt marker never reaches the
    # nudge -- the pacer gate fails closed (silence) first -- so this half pins
    # the gate, not C3, and passes with or without #4139. The direct selector
    # call below is the discriminating C3 check.
    stdout, rc = _run_nudge(tmp_path, "{not json")
    assert rc == 0
    assert stdout == ""  # pacer gate: unparseable marker -> silence, no raise


def test_c3_selector_on_unreadable_marker_is_c2_and_never_raises(tmp_path):
    spec = importlib.util.spec_from_file_location("_loop_arm_4139b", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    for path in (str(bad), str(tmp_path / "missing.json")):
        text = mod._nudge_text_for_marker(path)
        assert "this file's own `loop_prompt` field" not in text
        assert _NEW_SENTENCE in text
        assert _STUB_FLAG in text and _OTHER_KIND_EXAMPLE in text
