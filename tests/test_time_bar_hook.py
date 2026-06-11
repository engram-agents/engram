"""Tests for engram-time-bar-hook's time-field formatting (#721).

The fix makes USER-LOCAL time prominent (weekday/date/12-hour/tz from the user's
timezone) and reduces UTC to a small parenthetical — because an agent read the
leading UTC field as the local time and confabulated 'it's 2am'. The load-bearing
cases: the local fields (esp. weekday + date) derive from local, not UTC, so they
stay correct across the UTC/local date boundary near midnight.
"""

import importlib.util
import os
from datetime import datetime, timezone

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
