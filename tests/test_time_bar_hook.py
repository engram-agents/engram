"""Tests for engram-time-bar-hook's time-field formatting (#721),
last-user-activity stamping (#1077 part 2), and loop-wake classification (#1592).

The formatting fix makes USER-LOCAL time prominent (weekday/date/12-hour/tz from
the user's timezone) and reduces UTC to a small parenthetical — because an agent
read the leading UTC field as the local time and confabulated 'it's 2am'. The
load-bearing cases: the local fields (esp. weekday + date) derive from local, not
UTC, so they stay correct across the UTC/local date boundary near midnight.

The stamping tests (spec case 7) verify that last-user-activity is written only
on genuine human-typed prompts and not on loop self-wakes or monitor events.

The loop-wake tests (#1592) verify that:
  - A ScheduleWakeup loop-wake prompt (CC-2.1.195 stdin shape, no promptSource)
    is correctly classified as non-human by _is_human_prompt.
  - The last-user-msg display write is gated on _is_human_prompt (Task 2b).
  - The hook's fallback marker constant equals loop_prompt.LOOP_WAKE_MARKER (SSoT guard).
"""

import importlib.util
import json as _json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HOOK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "engram", "hooks", "claude", "engram-time-bar-hook.py",
)
spec = importlib.util.spec_from_file_location("time_bar_hook", HOOK_PATH)
tbh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tbh)

# Also load loop_prompt for the SSoT guard test and fixture helpers.
_LP_PATH = Path(__file__).parent.parent / "src" / "engram" / "tools" / "loop_prompt.py"
_lp_spec = importlib.util.spec_from_file_location("loop_prompt_tbh_tests", _LP_PATH)
_lp = importlib.util.module_from_spec(_lp_spec)
_lp_spec.loader.exec_module(_lp)

pytestmark = pytest.mark.skipif(tbh.ZoneInfo is None, reason="zoneinfo unavailable")


def test_local_is_prominent_utc_is_parenthetical():
    # 02:55Z on Jun 3 == 10:55 PM EDT on Jun 2 (the exact case that confused the agent).
    now = datetime(2026, 6, 3, 2, 55, 38, tzinfo=timezone.utc)
    field = tbh._format_time_field(now, "America/New_York")
    # Leads with the local weekday + date + 12-hour time + tz.
    assert field.startswith("Tue 2026-06-02 10:55 PM EDT")
    # UTC is reduced to a small parenthetical at the end.
    assert field.endswith("(02:55Z)")


def test_weekday_and_date_come_from_local_not_utc():
    """The bug's second instance: weekday/date must be the LOCAL ones."""
    now = datetime(2026, 6, 3, 2, 55, tzinfo=timezone.utc)  # UTC: Wed Jun 3
    field = tbh._format_time_field(now, "America/New_York")  # local: Tue Jun 2
    assert "Tue" in field and "2026-06-02" in field
    assert "Wed" not in field and "2026-06-03" not in field


def test_twelve_hour_strips_leading_zero():
    # 23:34Z == 7:34 PM EDT — single-digit hour must not show a leading zero.
    now = datetime(2026, 6, 2, 23, 34, tzinfo=timezone.utc)
    field = tbh._format_time_field(now, "America/New_York")
    assert "7:34 PM EDT" in field
    assert "07:34" not in field


def test_noon_and_midnight_render_sensibly():
    noon = datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc)  # 12:00 PM EDT
    assert "12:00 PM EDT" in tbh._format_time_field(noon, "America/New_York")
    midnight = datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)  # 12:00 AM EDT (Jun 2)
    assert "12:00 AM EDT" in tbh._format_time_field(midnight, "America/New_York")


def test_pacific_still_works():
    now = datetime(2026, 6, 3, 2, 55, tzinfo=timezone.utc)  # 7:55 PM PDT Jun 2
    field = tbh._format_time_field(now, "America/Los_Angeles")
    assert field.startswith("Tue 2026-06-02 7:55 PM PDT")
    assert field.endswith("(02:55Z)")


def test_invalid_tz_falls_back_to_labelled_utc():
    now = datetime(2026, 6, 3, 2, 55, tzinfo=timezone.utc)
    field = tbh._format_time_field(now, "Not/ARealZone")
    assert "UTC (local tz unavailable)" in field
    # Does not silently present UTC as if it were local.
    assert "2026-06-03T02:55" in field


def test_dst_abbr_tracks_season():
    """tz abbreviation + offset follow DST (inherited from astimezone)."""
    # Winter → EST (UTC-5): 12:00Z == 7:00 AM EST.
    winter = tbh._format_time_field(datetime(2026, 12, 15, 12, 0, tzinfo=timezone.utc), "America/New_York")
    assert "7:00 AM EST" in winter
    # Summer → EDT (UTC-4): 12:00Z == 8:00 AM EDT.
    summer = tbh._format_time_field(datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc), "America/New_York")
    assert "8:00 AM EDT" in summer


def test_fallback_when_zoneinfo_module_absent(monkeypatch):
    """If zoneinfo isn't importable at all (ZoneInfo is None), fall back to labelled UTC."""
    monkeypatch.setattr(tbh, "ZoneInfo", None)
    field = tbh._format_time_field(datetime(2026, 6, 3, 2, 55, tzinfo=timezone.utc), "America/New_York")
    assert "UTC (local tz unavailable)" in field
    assert "2026-06-03T02:55" in field


def test_tz_none_shows_utc_with_not_configured_label():
    """tz_name=None (unconfigured) → UTC shown with actionable label, never a silent wrong coast."""
    now = datetime(2026, 6, 3, 2, 55, tzinfo=timezone.utc)
    field = tbh._format_time_field(now, None)
    assert "UTC (tz not configured)" in field
    # Weekday and date come from UTC when unconfigured (NOT from local EDT).
    assert "Wed" in field  # 2026-06-03 UTC is Wednesday
    assert "2026-06-03" in field
    # Does NOT silently show a wrong-coast timezone.
    assert "PDT" not in field
    assert "EDT" not in field


def test_get_user_tz_returns_none_when_no_config(tmp_path, monkeypatch):
    """No config.json → returns None (not a hardcoded LA default)."""
    monkeypatch.setattr(tbh, "CONFIG", tmp_path / "config.json")
    assert tbh._get_user_tz() is None


def test_get_user_tz_returns_none_when_user_block_absent(tmp_path, monkeypatch):
    """config.json present but no 'user' block → returns None."""
    import json as _json
    cfg = tmp_path / "config.json"
    cfg.write_text(_json.dumps({"agent_name": "test"}))
    monkeypatch.setattr(tbh, "CONFIG", cfg)
    assert tbh._get_user_tz() is None


def test_get_user_tz_returns_none_when_timezone_empty(tmp_path, monkeypatch):
    """config.json has user.timezone='' (empty) → returns None."""
    import json as _json
    cfg = tmp_path / "config.json"
    cfg.write_text(_json.dumps({"user": {"timezone": ""}}))
    monkeypatch.setattr(tbh, "CONFIG", cfg)
    assert tbh._get_user_tz() is None


def test_get_user_tz_returns_value_when_set(tmp_path, monkeypatch):
    """config.json has user.timezone set → returns it."""
    import json as _json
    cfg = tmp_path / "config.json"
    cfg.write_text(_json.dumps({"user": {"timezone": "America/New_York"}}))
    monkeypatch.setattr(tbh, "CONFIG", cfg)
    assert tbh._get_user_tz() == "America/New_York"


# ---------------------------------------------------------------------------
# Spec case 7 — last-user-activity stamp discriminator
# ---------------------------------------------------------------------------

class TestIsHumanPrompt:
    """_is_human_prompt correctly identifies human vs non-human payloads."""

    def test_typed_prompt_source_is_human(self):
        payload = {"promptSource": "typed", "prompt": "hello there"}
        assert tbh._is_human_prompt(payload) is True

    def test_system_prompt_source_is_not_human(self):
        """Monitor task-notification events carry promptSource=='system'."""
        payload = {"promptSource": "system", "prompt": "<task-notification>done</task-notification>"}
        assert tbh._is_human_prompt(payload) is False

    def test_no_prompt_source_task_notification_is_not_human(self):
        """No promptSource + task-notification body → not human (fallback path)."""
        payload = {"prompt": "<task-notification>wakeup</task-notification>"}
        assert tbh._is_human_prompt(payload) is False

    def test_no_prompt_source_command_message_is_not_human(self):
        """No promptSource + <command-message> body → loop self-wake → not human."""
        payload = {"prompt": "<command-message>...</command-message>"}
        assert tbh._is_human_prompt(payload) is False

    def test_no_prompt_source_command_name_is_not_human(self):
        """No promptSource + <command-name> body → not human."""
        payload = {"prompt": "<command-name>loop</command-name>"}
        assert tbh._is_human_prompt(payload) is False

    def test_no_prompt_source_local_command_is_not_human(self):
        """No promptSource + <local-command body → not human."""
        payload = {"prompt": "<local-command>run</local-command>"}
        assert tbh._is_human_prompt(payload) is False

    def test_no_prompt_source_stop_hook_feedback_is_not_human(self):
        """No promptSource + 'Stop hook feedback:' body -> not human (#2438).

        A blocking Stop-hook re-invocation (e.g. engram-loop-arm-stop-hook.py's
        own nudge) renders as a user turn starting with 'Stop hook feedback:'
        and carries promptSource=None -- must not be misclassified as a
        genuine human prompt."""
        payload = {"prompt": "Stop hook feedback:\n[Loop Arm Check: a loop marker is active and this turn shows no ScheduleWakeup call. Arm now.]"}
        assert tbh._is_human_prompt(payload) is False

    def test_no_prompt_source_plain_text_is_human(self):
        """No promptSource + plain text body → treated as human (fallback)."""
        payload = {"prompt": "What is the status of #1077?"}
        assert tbh._is_human_prompt(payload) is True

    def test_empty_prompt_body_fallback_is_not_human(self):
        """No promptSource + empty body → NOT human (positive-evidence default).

        The discriminator stamps only on positive evidence of a human prompt.
        An empty/absent body (e.g. an unreadable or empty payload) is absence of
        evidence, not evidence of a human — so it must NOT classify as human
        (which would falsely mark 'engaged'). A non-empty body with no machine
        prefix IS human.
        """
        assert tbh._is_human_prompt({}) is False
        # ...but a real, non-empty body with no machine prefix still reads human:
        assert tbh._is_human_prompt({"prompt": "fix the bug please"}) is True

    def test_unknown_prompt_source_is_not_human(self):
        """An unrecognised promptSource value (not 'typed') → not human."""
        payload = {"promptSource": "api", "prompt": "hello"}
        assert tbh._is_human_prompt(payload) is False


class TestStampUserActivity:
    """_stamp_user_activity writes the epoch stamp; errors are swallowed."""

    def test_stamp_written_on_human_prompt(self, tmp_path, monkeypatch):
        """Spec case 7a: stamp is written when _is_human_prompt is True."""
        activity_path = tmp_path / "last-user-activity"
        monkeypatch.setattr(tbh, "LAST_USER_ACTIVITY", activity_path)
        import time as _time
        epoch = _time.time()
        tbh._stamp_user_activity(epoch)
        assert activity_path.exists()
        written = int(activity_path.read_text().strip())
        assert written == int(epoch)

    def test_stamp_not_written_on_non_human_prompt(self, tmp_path, monkeypatch):
        """Spec case 7b: the gate blocks the stamp for a task-notification payload."""
        activity_path = tmp_path / "last-user-activity"
        monkeypatch.setattr(tbh, "LAST_USER_ACTIVITY", activity_path)
        import time as _time
        payload = {
            "promptSource": "system",
            "prompt": "<task-notification>monitor event</task-notification>",
        }
        # Exercise the REAL gate: the stamp fires only when _is_human_prompt is True.
        if tbh._is_human_prompt(payload):
            tbh._stamp_user_activity(_time.time())
        assert not activity_path.exists()  # gate correctly blocked the write

    def test_stamp_write_error_is_swallowed(self, tmp_path, monkeypatch):
        """Write to a path that will fail (directory, not file) → no exception raised."""
        bad_path = tmp_path / "last-user-activity"
        bad_path.mkdir()  # Make it a directory so write_text fails.
        monkeypatch.setattr(tbh, "LAST_USER_ACTIVITY", bad_path)
        import time as _time
        # Must not raise.
        tbh._stamp_user_activity(_time.time())


# ---------------------------------------------------------------------------
# #1592 — CC-2.1.195 fixture + loop-wake classification + secondary display bug
# ---------------------------------------------------------------------------

def _cc_payload(body: str) -> dict:
    """Return the EXACT CC-2.1.195 UserPromptSubmit stdin shape (no promptSource).

    In Claude Code 2.1.195, the hook payload contains ONLY these keys:
    cwd, hook_event_name, permission_mode, prompt, session_id, transcript_path.
    There is NO promptSource field — which is the root cause of #1592.
    """
    return {
        "cwd": "/home/agent-borges/engram-alpha",
        "hook_event_name": "UserPromptSubmit",
        "permission_mode": "bypassPermissions",
        "prompt": body,
        "session_id": "test-session-1592",
        "transcript_path": "/home/agent-borges/.claude/projects/test/transcript.jsonl",
    }


class TestLoopWakeClassification:
    """#1592 — loop-wake prompts must be classified as non-human in the
    CC-2.1.195 payload shape (no promptSource in stdin)."""

    def test_cc_payload_plain_human_body_is_human(self):
        """CC-2.1.195 shape + plain text body → classified as human (True)."""
        payload = _cc_payload("Fix the regression in #1592 please.")
        assert tbh._is_human_prompt(payload) is True

    def test_cc_payload_loop_wake_body_is_not_human(self):
        """CC-2.1.195 shape + format_loop_prompt body → classified as non-human (False).

        This is the core #1592 fix: ScheduleWakeup loop wakes arrive with no
        promptSource and a plain-text body.  The marker prefix ensures they
        are detected as non-human in the fallback path.
        """
        loop_body = _lp.format_loop_prompt("Loop tick — checking for new work.")
        payload = _cc_payload(loop_body)
        assert tbh._is_human_prompt(payload) is False

    def test_cc_payload_loop_wake_with_meta_is_not_human(self):
        """CC-2.1.195 shape + format_loop_prompt with meta → non-human."""
        loop_body = _lp.format_loop_prompt(
            "Curiosity loop — scanning open questions.", kind="curiosity", seq=5
        )
        payload = _cc_payload(loop_body)
        assert tbh._is_human_prompt(payload) is False

    def test_cc_payload_no_prompt_source_key(self):
        """Fixture must not carry promptSource — verify our fixture is faithful."""
        payload = _cc_payload("hello")
        assert "promptSource" not in payload

    def test_loop_wake_marker_in_non_human_prefixes(self):
        """The loop-wake marker is present in _NON_HUMAN_PREFIXES."""
        assert tbh._LOOP_WAKE_MARKER in tbh._NON_HUMAN_PREFIXES

    def test_is_loop_wake_round_trip(self):
        """is_loop_wake(format_loop_prompt(x)) is always True."""
        assert _lp.is_loop_wake(_lp.format_loop_prompt("anything"))

    def test_format_loop_prompt_idempotent(self):
        """format_loop_prompt applied twice does not double-mark."""
        once = _lp.format_loop_prompt("core message")
        twice = _lp.format_loop_prompt(once)
        assert twice == once


class TestIsHumanPromptHtmlEntityNormalization:
    """gh#1646, Layer 2 — _is_human_prompt's fallback prefix check must
    classify identically whether the loop-wake marker arrives plain or
    HTML-escaped, so an agent-construction mistake (&lt;loop-wake&gt; instead
    of real angle brackets) can't silently fall through to a false 'human'
    classification."""

    def test_escaped_loop_wake_marker_is_not_human(self):
        """The incident case: &lt;loop-wake&gt; body → non-human (False)."""
        payload = _cc_payload("&lt;loop-wake&gt;\n...core...")
        assert tbh._is_human_prompt(payload) is False

    def test_plain_loop_wake_marker_is_not_human(self):
        """Plain marker (unchanged behavior) still classifies as non-human."""
        payload = _cc_payload("<loop-wake>\n...core...")
        assert tbh._is_human_prompt(payload) is False

    def test_normal_human_prompt_is_still_human(self):
        """A normal human prompt is not misclassified as non-human."""
        payload = _cc_payload("a normal human prompt")
        assert tbh._is_human_prompt(payload) is True


class TestSSoTGuard:
    """The hook's fallback marker string equals loop_prompt.LOOP_WAKE_MARKER.

    This guard prevents the two constants from diverging silently — if one is
    updated and the other is not, the test fails and the drift is caught at
    review time rather than in production (#1592 spec requirement).
    """

    def test_fallback_equals_loop_prompt_marker(self):
        """Hook's hardcoded fallback == loop_prompt.LOOP_WAKE_MARKER (SSoT guard)."""
        assert tbh._LOOP_WAKE_MARKER_FALLBACK == _lp.LOOP_WAKE_MARKER

    def test_active_marker_equals_loop_prompt_marker(self):
        """In the test env (source tree), the import succeeds and the active
        marker also equals loop_prompt.LOOP_WAKE_MARKER."""
        # _LOOP_WAKE_MARKER is set either from the import or from the fallback.
        # In the test env the import succeeds, so both should be identical.
        assert tbh._LOOP_WAKE_MARKER == _lp.LOOP_WAKE_MARKER


class TestLastUserMsgGate:
    """Task 2b — LAST_USER_MSG write is gated on _is_human_prompt (#1592).

    The secondary display bug: the "last user msg Xm ago" display was updated
    unconditionally, so loop-wakes reset it to "this msg" on every tick.
    The fix gates the write on _is_human_prompt(payload).
    """

    def test_loop_wake_does_not_write_last_user_msg(self, tmp_path, monkeypatch):
        """A loop-wake payload leaves LAST_USER_MSG unchanged."""
        msg_path = tmp_path / "last-user-msg.json"
        monkeypatch.setattr(tbh, "LAST_USER_MSG", msg_path)

        payload = _cc_payload(_lp.format_loop_prompt("Loop tick."))
        # Simulate the gated write from main()
        if tbh._is_human_prompt(payload):
            from datetime import timezone as _tz
            utc_iso = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            msg_path.write_text(_json.dumps({"ts": utc_iso}))

        assert not msg_path.exists(), (
            "LAST_USER_MSG must not be written on a loop-wake prompt"
        )

    def test_human_prompt_writes_last_user_msg(self, tmp_path, monkeypatch):
        """A genuine human payload causes LAST_USER_MSG to be written."""
        msg_path = tmp_path / "last-user-msg.json"
        monkeypatch.setattr(tbh, "LAST_USER_MSG", msg_path)

        payload = _cc_payload("Please summarise the open issues.")
        if tbh._is_human_prompt(payload):
            from datetime import timezone as _tz
            utc_iso = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            msg_path.write_text(_json.dumps({"ts": utc_iso}))

        assert msg_path.exists(), (
            "LAST_USER_MSG must be written on a genuine human prompt"
        )
        data = _json.loads(msg_path.read_text())
        assert "ts" in data


# ---------------------------------------------------------------------------
# #1537/#1539 guard — deployed-tree resolution for loop_prompt import
# ---------------------------------------------------------------------------

class TestDeployedTreeResolution:
    """Regression guard for the #1537/#1539 class of deployed-tree path bugs.

    The plugin build flattens ``hooks/claude/*`` → ``hooks/*``, so a hook at
    ``<root>/hooks/<name>.py`` is one level shallower than its source at
    ``src/engram/hooks/claude/<name>.py``.  A fixed ``parents[N]`` calibrated
    against the deeper source layout overshoots in the deployed tree (same
    silent failure as #1539: the path resolves to *a* directory, just the wrong
    one).

    The time-bar hook uses the robust resolve pattern (``$CLAUDE_PLUGIN_ROOT/tools``
    first, then walk-parents for ``loop_prompt.py``) instead of a fixed ``parents[N]``.
    These tests exercise both paths by copying the real hook and loop_prompt into a
    temp dir that mirrors the deployed (flattened) layout, then loading the hook
    fresh so its module-level import block re-executes against that layout.

    Loading uses a unique module name per case (``importlib.util.module_from_spec``
    + ``spec.loader.exec_module``) so the top-level resolution re-runs rather than
    reusing a cached module.
    """

    _SRC_ROOT = Path(__file__).parent.parent / "src" / "engram"

    def _build_deployed_layout(self, root: Path) -> None:
        """Populate ``<root>/hooks/engram-time-bar-hook.py`` and
        ``<root>/tools/loop_prompt.py`` — the deployed (flattened) layout."""
        (root / "hooks").mkdir(parents=True)
        (root / "tools").mkdir(parents=True)
        shutil.copy(
            self._SRC_ROOT / "hooks" / "claude" / "engram-time-bar-hook.py",
            root / "hooks" / "engram-time-bar-hook.py",
        )
        shutil.copy(
            self._SRC_ROOT / "tools" / "loop_prompt.py",
            root / "tools" / "loop_prompt.py",
        )

    def _load_hook_fresh(self, hook_path: Path, module_name: str):
        """Load the hook at hook_path under a unique module_name.

        Clears ``sys.modules['loop_prompt']`` first so the hook's top-level
        import block re-resolves loop_prompt from the newly configured sys.path
        rather than reusing a module cached from an earlier load.
        """
        sys.modules.pop("loop_prompt", None)
        spec = importlib.util.spec_from_file_location(module_name, str(hook_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_case1_plugin_root_set(self, tmp_path, monkeypatch):
        """Case 1: CLAUDE_PLUGIN_ROOT=<root> → hook resolves loop_prompt via env-var path.

        When ``$CLAUDE_PLUGIN_ROOT`` is set, ``<root>/tools`` is the first
        candidate and is found before the walk-parents search even runs.
        The hook's ``_LOOP_WAKE_MARKER`` is set from the import (not the
        hardcoded fallback), and the prefix is present in ``_NON_HUMAN_PREFIXES``.
        """
        root = tmp_path / "plugin"
        self._build_deployed_layout(root)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))

        saved_syspath = sys.path[:]
        try:
            hook = self._load_hook_fresh(
                root / "hooks" / "engram-time-bar-hook.py",
                "time_bar_hook_deployed_c1",
            )
            assert hook._LOOP_WAKE_MARKER == "<loop-wake>"
            assert hook._LOOP_WAKE_MARKER in hook._NON_HUMAN_PREFIXES
        finally:
            sys.path[:] = saved_syspath
            sys.modules.pop("loop_prompt", None)

    def test_case2_plugin_root_unset(self, tmp_path, monkeypatch):
        """Case 2: CLAUDE_PLUGIN_ROOT unset → walk-parents finds <root>/tools/loop_prompt.py.

        With the env var absent, the hook walks parents of its own ``__file__``
        (``<root>/hooks/engram-time-bar-hook.py``).  ``parents[1]`` is ``<root>``
        and ``<root>/tools/loop_prompt.py`` exists → found in one hop.  A fixed
        ``parents[N]`` calibrated against the source tree (two extra levels) would
        land at the *parent* of ``<root>`` and miss the deployed tools dir.
        """
        root = tmp_path / "plugin"
        self._build_deployed_layout(root)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        saved_syspath = sys.path[:]
        try:
            hook = self._load_hook_fresh(
                root / "hooks" / "engram-time-bar-hook.py",
                "time_bar_hook_deployed_c2",
            )
            assert hook._LOOP_WAKE_MARKER == "<loop-wake>"
            assert hook._LOOP_WAKE_MARKER in hook._NON_HUMAN_PREFIXES
        finally:
            sys.path[:] = saved_syspath
            sys.modules.pop("loop_prompt", None)
