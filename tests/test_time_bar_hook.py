"""Tests for engram-time-bar-hook's time-field formatting (#721) and
last-user-activity stamping (#1077 part 2).

The formatting fix makes USER-LOCAL time prominent (weekday/date/12-hour/tz from
the user's timezone) and reduces UTC to a small parenthetical — because an agent
read the leading UTC field as the local time and confabulated 'it's 2am'. The
load-bearing cases: the local fields (esp. weekday + date) derive from local, not
UTC, so they stay correct across the UTC/local date boundary near midnight.

The stamping tests (spec case 7) verify that last-user-activity is written only
on genuine human-typed prompts and not on loop self-wakes or monitor events.
"""

import importlib.util
import os
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
