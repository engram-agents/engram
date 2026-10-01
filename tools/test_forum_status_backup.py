"""Contract tests: ``forum status`` prints the backup age (engram-alpha#3276, B8).

Written BEFORE the implementation (contract-first, #3045).

Why this half exists: the only reader of /health today is the installer, via
``curl --fail`` (HTTP code only), so a /health field on its own is as silent as
the journal was. ``forum status`` is the CLI seats already run.

Contract (B8): when /health carries a "backup" block, ``forum status`` prints one
line ``backup: last ok <age> ago`` with a visible degraded marker (the warning
sign) when ``deps.backup`` is "degraded". A /health WITHOUT a backup key prints
no backup line (old servers, unconfigured servers).

Harness: the same stubbed ``_api_get`` pattern as tools/test_forum_cli.py.
The CLI is expected to read ``<forum_url>/health`` through ``_api_get``.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.forum as forum_cli  # noqa: E402

WARN = "⚠"  # the warning sign


@pytest.fixture(autouse=True)
def _cli_env(tmp_path, monkeypatch):
    home = tmp_path / "engram"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps({"agent_name": "testbot", "forum": {"url": "http://localhost:59999"}})
    )
    forum_cli._FORUM_URL_CACHE = None
    monkeypatch.setattr(forum_cli, "ENGRAM_HOME", str(home))
    monkeypatch.setattr(forum_cli, "READ_CURSOR_PATH", str(home / "forum-read-cursor.txt"))
    yield
    forum_cli._FORUM_URL_CACHE = None


def _run_status(health_payload, extra_argv=()):
    """Run ``forum status`` with every endpoint stubbed; /health returns
    *health_payload* (None = endpoint fails the way swallow_errors does)."""

    def fake_api_get(url, params=None, swallow_errors=False):
        if url.endswith("/health"):
            return health_payload
        if "/api/agents/online" in url:
            return {"count": 0, "registered": 0}
        if "/inbox" in url:
            return {"inbox": [], "unread_all": 0}
        if "/api/threads" in url:
            return {"threads": []}
        return {}

    out = io.StringIO()
    with mock.patch.object(forum_cli, "_api_get", side_effect=fake_api_get), \
            mock.patch("sys.argv", ["forum", "status", *extra_argv]), \
            mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
        try:
            forum_cli.main()
        except SystemExit:
            pass
    return out.getvalue()


def _backup_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.strip().startswith("backup:")]


def _ok_health(age_hours: float) -> dict:
    return {
        "status": "ok",
        "deps": {"db": "ok", "backup": "ok"},
        "backup": {"last_success_at": "2026-10-01T04:00:00+00:00", "age_hours": age_hours},
    }


def _degraded_health(age_hours: float) -> dict:
    return {
        "status": "degraded",
        "deps": {"db": "ok", "backup": "degraded"},
        "backup": {"last_success_at": "2026-09-19T04:00:00+00:00", "age_hours": age_hours},
    }


class TestStatusBackupLine:

    def test_ok_backup_prints_line_without_marker(self):
        lines = _backup_lines(_run_status(_ok_health(5.0)))
        assert len(lines) == 1, f"exactly one backup line expected, got {lines}"
        assert re.match(r"^backup: last ok .+ ago", lines[0].strip())
        assert WARN not in lines[0]

    def test_degraded_backup_prints_line_with_marker(self):
        lines = _backup_lines(_run_status(_degraded_health(12 * 24.0)))
        assert len(lines) == 1, f"exactly one backup line expected, got {lines}"
        assert lines[0].strip().startswith("backup: last ok")
        assert WARN in lines[0], "degraded backup must carry a visible marker"

    def test_age_is_rendered_in_human_units(self):
        """A 12-day-old backup must read as days, not as a bare 288.0."""
        line = _backup_lines(_run_status(_degraded_health(12 * 24.0)))[0]
        assert re.search(r"\b12\s*d", line) or "12 day" in line, line

    def test_age_of_hours_scale(self):
        line = _backup_lines(_run_status(_ok_health(5.0)))[0]
        assert re.search(r"\b5\s*h", line) or "5 hour" in line, line

    def test_degraded_with_no_stamp_still_prints_marked_line(self):
        payload = {
            "status": "degraded",
            "deps": {"backup": "degraded"},
            "backup": {"last_success_at": None, "age_hours": None},
        }
        lines = _backup_lines(_run_status(payload))
        assert len(lines) == 1
        assert WARN in lines[0]

    def test_marker_only_when_degraded(self):
        ok = "\n".join(_backup_lines(_run_status(_ok_health(35.0))))
        bad = "\n".join(_backup_lines(_run_status(_degraded_health(37.0))))
        assert ok and WARN not in ok
        assert bad and WARN in bad

    # -- the cells below are GREEN on dev today: they pin existing behaviour --

    def test_no_backup_key_prints_no_backup_line(self):
        assert _backup_lines(_run_status({"status": "ok"})) == []

    def test_unreachable_health_prints_no_backup_line_and_status_still_works(self):
        out = _run_status(None)
        assert _backup_lines(out) == []
        assert "online:" in out

    def test_status_still_prints_its_existing_lines(self):
        out = _run_status(_ok_health(1.0))
        assert "agent:" in out and "unread:" in out and "online:" in out


class TestDegradedMarkerOnNonUtf8Stdout:

    def test_ascii_stdout_does_not_raise_and_still_marks_degraded(self):
        raw = io.BytesIO()
        ascii_out = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with mock.patch("sys.stdout", ascii_out):
            line = forum_cli._backup_status_line(_degraded_health(12 * 24.0))
            print(line)  # raised UnicodeEncodeError with a hard-coded U+26A0
            ascii_out.flush()
        text = raw.getvalue().decode("ascii")
        assert text.startswith("backup: last ok 12 d ago")
        assert "!" in text or WARN in text
