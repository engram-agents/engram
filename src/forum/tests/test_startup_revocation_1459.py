"""#1459 PR A round 4 (S1): the startup fail-closed line honours revocation.

A reviewer's survivor: `has_live_token` counting revoked tokens left every cell green. Its only
caller is the startup line, so these cells drive that line: a token that was issued and then
revoked is NOT a live token, and the "no live agent token" line must print.

Opts out of the suite-wide auto-auth plumbing (``raw_auth``) like its siblings.
"""

import sqlite3

import pytest

from forum import db
from forum.db import init_db

from .test_forum_auth_1459 import owned_tmp  # noqa: F401 (a fixture)

pytestmark = pytest.mark.raw_auth


def test_startup_fail_closed_line_prints_when_the_only_token_is_revoked(owned_tmp, capsys):
    from forum import server

    conn = sqlite3.connect(str(owned_tmp / "f.db"))
    init_db(conn)
    db.issue_token(conn, "alice")
    assert db.revoke_tokens(conn, "alice") == 1
    server._report_auth_startup(conn, enable_dm_viewer=False)
    err = capsys.readouterr().err
    assert "no live agent token" in err, err


def test_startup_fail_closed_line_is_silent_while_another_token_is_live(owned_tmp, capsys):
    """The control: with one revoked and one live token the line does NOT print, so the cell above
    is not passing merely because the line always prints."""
    from forum import server

    conn = sqlite3.connect(str(owned_tmp / "f.db"))
    init_db(conn)
    db.issue_token(conn, "alice")
    db.revoke_tokens(conn, "alice")
    db.issue_token(conn, "bob")
    server._report_auth_startup(conn, enable_dm_viewer=False)
    assert "no live agent token" not in capsys.readouterr().err
