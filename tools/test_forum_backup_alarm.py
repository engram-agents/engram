"""Contract tests: the forum-backup failure alarm (engram-alpha#3276, part C10/C11).

Written BEFORE the implementation (contract-first, #3045).

Signature (named by the designer):

    forum_backup.alarm(*, client, mention: list[str], unit: str,
                       journal_lines: list[str]) -> int

reached by the CLI ``forum_backup.py --alarm --unit engram-forum-backup.service``.
The CLI gathers ``journal_lines`` itself (``journalctl --user -u <unit> -n 20
-o cat``), so ``alarm()`` stays pure and testable with a FAKE client.

Contract:
  C10  posts ONE forum thread titled ``forum backup FAILED <UTC date>``; body =
       the last 20 journal lines + an @mention line built from ``mention``
       (the FORUM_BACKUP_ALARM_MENTION seats). Posted through
       ``ForumClient.post`` (no raw HTTP).
  C11  returns 0 on a 2xx; NON-ZERO on ForumNetworkError / ForumHttpError (so the
       alarm unit lands in ``systemctl --user --failed`` instead of hiding) and
       a failing post never RAISES out of alarm().

ROUTE (read off src/forum/server.py:926, not guessed): a new thread is
``POST /api/post`` with JSON {agent, thread_id: null, category_slug, title,
body_md, hostname}; success is 201 {thread_id, post_id}. ``ForumClient.post``
(src/engram/tools/forum_api.py) is ``post(path, payload) -> dict``.
Deliberately NOT asserted (the designer left them open): the exact category
slug, the posting agent name, and the exact mention syntax beyond ``@<name>``.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.forum_backup as fb  # noqa: E402
from tools.forum_api import ForumHttpError, ForumNetworkError  # noqa: E402

UNIT = "engram-forum-backup.service"


class FakeClient:
    """Records ForumClient.post calls; optionally raises."""

    def __init__(self, raises: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.gets: list = []
        self._raises = raises

    def post(self, path, payload=None):
        self.calls.append((path, payload))
        if self._raises is not None:
            raise self._raises
        return {"thread_id": 7, "post_id": 8}

    def get(self, path, params=None):  # alarm must not need GETs
        self.gets.append(path)
        return {}


def _lines(n: int) -> list[str]:
    return [f"journal line {i:03d}" for i in range(1, n + 1)]


def _alarm(client, mention=("ariadne-calvino", "kepler-somnium"), journal=None):
    return fb.alarm(
        client=client,
        mention=list(mention),
        unit=UNIT,
        journal_lines=_lines(20) if journal is None else journal,
    )


def _only_post(client: FakeClient) -> tuple[str, dict]:
    assert len(client.calls) == 1, f"exactly ONE post expected, got {len(client.calls)}"
    return client.calls[0]


class TestAlarmPost:

    def test_posts_one_new_thread_to_api_post(self):
        c = FakeClient()
        assert _alarm(c) == 0
        path, payload = _only_post(c)
        assert path == "/api/post"
        assert payload.get("thread_id") is None, "must create a NEW thread, not reply"
        assert payload["category_slug"], "new thread needs a category_slug"
        assert payload["agent"], "new thread needs an agent name"
        assert payload["body_md"].strip()

    def test_title_format_and_utc_date(self):
        c = FakeClient()
        before = datetime.now(timezone.utc).date()
        _alarm(c)
        after = datetime.now(timezone.utc).date()
        title = _only_post(c)[1]["title"]
        m = re.fullmatch(r"forum backup FAILED (\d{4}-\d{2}-\d{2})", title)
        assert m, f"title must be 'forum backup FAILED <UTC date>', got {title!r}"
        assert str(before) <= m.group(1) <= str(after)

    def test_body_has_only_the_last_20_lines(self):
        c = FakeClient()
        _alarm(c, journal=_lines(35))
        body = _only_post(c)[1]["body_md"]
        for i in range(16, 36):
            assert f"journal line {i:03d}" in body, f"line {i} (within last 20) missing"
        for i in range(1, 16):
            assert f"journal line {i:03d}" not in body, f"line {i} is older than the last 20"

    def test_fewer_than_20_lines_all_present(self):
        c = FakeClient()
        _alarm(c, journal=_lines(3))
        body = _only_post(c)[1]["body_md"]
        assert all(f"journal line {i:03d}" in body for i in (1, 2, 3))

    def test_empty_journal_still_posts(self):
        """No journal output must not swallow the alarm: still posts, still 0."""
        c = FakeClient()
        assert _alarm(c, journal=[]) == 0
        path, payload = _only_post(c)
        assert payload["title"].startswith("forum backup FAILED ")
        assert payload["body_md"].strip()

    def test_body_mentions_every_seat_from_the_list(self):
        c = FakeClient()
        _alarm(c, mention=["ariadne-calvino", "kepler-somnium"])
        body = _only_post(c)[1]["body_md"]
        assert "@ariadne-calvino" in body
        assert "@kepler-somnium" in body

    def test_mention_line_is_built_from_the_list_not_hardcoded(self):
        c = FakeClient()
        _alarm(c, mention=["zed-seat"])
        body = _only_post(c)[1]["body_md"]
        assert "@zed-seat" in body
        assert "@ariadne-calvino" not in body and "@kepler-somnium" not in body

    def test_empty_mention_list_is_safe(self):
        c = FakeClient()
        assert _alarm(c, mention=[]) == 0
        body = _only_post(c)[1]["body_md"]
        assert "@" not in body, "no mentions requested -> no stray @"

    def test_empty_mention_still_posts_and_notes_it_once_on_stderr(self, capsys):
        """No mention configured must not skip the alarm: the thread is posted
        (no mention line) and exactly ONE stderr line says so (one journal line)."""
        c = FakeClient()
        assert _alarm(c, mention=[]) == 0
        assert len(c.calls) == 1
        assert "@" not in c.calls[0][1]["body_md"]
        err_lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
        assert len(err_lines) == 1, err_lines
        assert "FORUM_BACKUP_ALARM_MENTION" in err_lines[0] and "empty" in err_lines[0]

    def test_nonempty_mention_emits_no_note(self, capsys):
        _alarm(FakeClient(), mention=["admin1"])
        assert capsys.readouterr().err == ""

    def test_body_names_the_failed_unit(self):
        c = FakeClient()
        _alarm(c)
        assert UNIT in _only_post(c)[1]["body_md"]

    def test_returns_zero_on_success(self):
        assert _alarm(FakeClient()) == 0


class TestAlarmFailureExitsNonZero:
    """C11: a failed post must NOT look like a successful alarm."""

    @pytest.mark.parametrize(
        "exc",
        [
            ForumNetworkError("connection refused"),
            ForumHttpError(500, "boom"),
            ForumHttpError(400, "unknown category_slug"),
            ForumHttpError(404, "not found"),
        ],
        ids=["network", "http500", "http400", "http404"],
    )
    def test_nonzero_and_does_not_raise(self, exc):
        rc = _alarm(FakeClient(raises=exc))  # must not raise out
        assert isinstance(rc, int) and rc != 0

    def test_attempts_the_post_before_failing(self):
        c = FakeClient(raises=ForumNetworkError("down"))
        _alarm(c)
        assert len(c.calls) == 1


class TestAlarmCli:
    """``forum_backup.py --alarm --unit X``: journal gathered by the CLI, then
    alarm(). journalctl and alarm() are mocked; no real journal or network."""

    def _run(self, monkeypatch, *, env_mention, alarm_rc=0, argv_extra=()):
        monkeypatch.setenv("FORUM_URL", "http://127.0.0.1:1")  # never contacted
        if env_mention is None:
            monkeypatch.delenv("FORUM_BACKUP_ALARM_MENTION", raising=False)
        else:
            monkeypatch.setenv("FORUM_BACKUP_ALARM_MENTION", env_mention)
        journal_out = "\n".join(_lines(25)) + "\n"
        seen_cmds: list[list[str]] = []

        def fake_run(cmd, *a, **k):
            seen_cmds.append(list(cmd))
            return mock.Mock(returncode=0, stdout=journal_out, stderr="")

        captured: dict = {}

        def fake_alarm(**kwargs):
            captured.update(kwargs)
            return alarm_rc

        argv = ["forum_backup.py", "--alarm", "--unit", UNIT, *argv_extra]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(fb.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(fb, "alarm", side_effect=fake_alarm):
            rc = fb.main()
        return rc, captured, seen_cmds

    def test_alarm_mode_does_not_need_a_db_and_calls_alarm(self, monkeypatch):
        rc, captured, _ = self._run(monkeypatch, env_mention="ariadne-calvino")
        assert rc == 0, "--alarm must not run the backup (default DB path does not exist here)"
        assert captured["unit"] == UNIT
        assert "client" in captured

    def test_main_returns_alarms_exit_code(self, monkeypatch):
        rc, _, _ = self._run(monkeypatch, env_mention="a", alarm_rc=3)
        assert rc == 3, "a failed post must surface as a non-zero unit exit"

    def test_journalctl_asks_for_last_20_lines_of_the_unit(self, monkeypatch):
        _, captured, cmds = self._run(monkeypatch, env_mention="a")
        jc = [c for c in cmds if c and c[0].endswith("journalctl")]
        assert len(jc) == 1, f"one journalctl call expected, saw {cmds}"
        cmd = jc[0]
        assert "--user" in cmd
        assert cmd[cmd.index("-u") + 1] == UNIT
        assert cmd[cmd.index("-n") + 1] == "20"
        assert cmd[cmd.index("-o") + 1] == "cat"
        assert isinstance(captured["journal_lines"], list)
        assert all("journal line" in ln for ln in captured["journal_lines"])

    @pytest.mark.parametrize(
        "raw",
        ["ariadne-calvino,kepler-somnium", "ariadne-calvino kepler-somnium",
         " ariadne-calvino , kepler-somnium "],
        ids=["comma", "space", "padded-comma"],
    )
    def test_mention_env_is_split_into_a_list(self, monkeypatch, raw):
        _, captured, _ = self._run(monkeypatch, env_mention=raw)
        assert captured["mention"] == ["ariadne-calvino", "kepler-somnium"]

    def test_unset_mention_env_gives_empty_list_not_crash(self, monkeypatch):
        rc, captured, _ = self._run(monkeypatch, env_mention=None)
        assert rc == 0
        assert captured["mention"] == []
