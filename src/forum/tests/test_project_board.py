"""Tests for the project board read model and routes (Slice 1).

Covers:
1. Read model parses fixture baton files correctly.
2. Malformed file is skipped; other items still render.
3. gh-reconcile: a fixture with a PR ref flips merged→done when gh mock says merged.
4. gh-reconcile degrades gracefully when gh is unavailable (gh_unknown=True, page loads).
5. /api/board/updates?since= correctness (since-cursor filtering).
6. /api/board/updates?agent= filters to that agent's court.
7. /api/board/projects returns 200 with board/counts keys.
8. /board returns 200.
9. No code path writes to any project .md file (verified by fixture mtime check).
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from forum.board_projects import (
    _batch_gh_reconcile,
    _effective_status,
    _gh_pr_state,
    filter_updates,
    get_board_counts,
    read_project_board,
)
from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Path to fixture projects dir
# ---------------------------------------------------------------------------
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "projects"


# ---------------------------------------------------------------------------
# Flask app fixture (backed by in-memory DB + fixture projects dir)
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    """Flask app backed by a temp DB, using fixture projects dir."""
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    # Point the board reader at our fixture directory via env var.
    os.environ["BATON_PROJECTS_DIR"] = str(FIXTURES_DIR)
    yield application
    # Clean up env override after test.
    os.environ.pop("BATON_PROJECTS_DIR", None)


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helper: read_project_board from fixtures, mocking gh calls
# ---------------------------------------------------------------------------

def _board_with_gh_mock(gh_mock: dict[str, str]) -> list[dict]:
    """Call read_project_board() from FIXTURES_DIR with gh state mocked.

    Args:
        gh_mock: dict mapping pr_number string → state string.
            Any PR number not in the dict returns 'unknown'.
    """
    def _fake_batch(pr_numbers: list[str]) -> dict[str, str]:
        return {pr: gh_mock.get(pr, "unknown") for pr in pr_numbers}

    with patch("forum.board_projects._batch_gh_reconcile", side_effect=_fake_batch):
        return read_project_board(FIXTURES_DIR)


# ---------------------------------------------------------------------------
# 1. Read model: parses fixture files
# ---------------------------------------------------------------------------

class TestReadModel:
    def test_returns_list(self):
        """read_project_board returns a list."""
        items = _board_with_gh_mock({})
        assert isinstance(items, list)

    def test_parses_pr_100(self):
        """PR-100 fixture is parsed with correct fields."""
        items = _board_with_gh_mock({"100": "open"})
        pr100 = next((i for i in items if i["project"] == "PR-100"), None)
        assert pr100 is not None, "PR-100 should be in the board"
        assert pr100["title"] == "Add project board feature"
        assert pr100["status"] == "in-review"
        assert pr100["turn"] == "ariadne"
        assert pr100["kind"] == "pr"
        assert pr100["github"] == "pr/100"
        assert "ariadne" in pr100["participants"]
        assert "lei" in pr100["participants"]

    def test_parses_issue_200(self):
        """ISSUE-200 fixture is parsed as kind='issue'."""
        items = _board_with_gh_mock({})
        iss = next((i for i in items if i["project"] == "ISSUE-200"), None)
        assert iss is not None
        assert iss["kind"] == "issue"
        assert iss["github"] == ""  # no github field

    def test_all_projects_parsed(self):
        """All valid fixture files are parsed (malformed is skipped)."""
        items = _board_with_gh_mock({"100": "open", "101": "open", "102": "merged", "103": "merged"})
        project_ids = {i["project"] for i in items}
        # Valid baton files present in fixtures
        assert "PR-100" in project_ids
        assert "PR-101" in project_ids
        assert "PR-102" in project_ids
        assert "PR-103" in project_ids
        assert "ISSUE-200" in project_ids

    def test_required_fields_present(self):
        """Every item has all required fields."""
        items = _board_with_gh_mock({"100": "open"})
        required = {
            "project", "title", "kind", "status", "effective_status",
            "turn", "turn_since", "participants", "github", "gh_state", "updated_at",
            "cursor_ts",  # the /updates cursor key — guard against its removal
        }
        for item in items:
            missing = required - set(item.keys())
            assert not missing, f"Item {item['project']} missing fields: {missing}"

    def test_age_str_present(self):
        """age_str is present (may be empty string for items with no turn_since)."""
        items = _board_with_gh_mock({"100": "open"})
        for item in items:
            assert "age_str" in item


# ---------------------------------------------------------------------------
# 2. Malformed file is skipped; others render
# ---------------------------------------------------------------------------

class TestMalformedSkipped:
    def test_malformed_not_in_board(self):
        """MALFORMED.md (no frontmatter) is skipped; it never appears in board."""
        items = _board_with_gh_mock({})
        project_ids = {i["project"] for i in items}
        assert "MALFORMED" not in project_ids

    def test_other_items_still_rendered(self):
        """After skipping malformed, other items still render."""
        items = _board_with_gh_mock({"100": "open"})
        project_ids = {i["project"] for i in items}
        # At minimum PR-100 and ISSUE-200 should be present
        assert "PR-100" in project_ids
        assert "ISSUE-200" in project_ids


# ---------------------------------------------------------------------------
# 3. gh-reconcile: merged PR flips to effective_status=done
# ---------------------------------------------------------------------------

class TestGhReconcile:
    def test_pr102_reconciled_to_done(self):
        """PR-102 file says in-progress but gh says merged → effective_status=done."""
        items = _board_with_gh_mock({"102": "merged", "100": "open", "101": "open"})
        pr102 = next((i for i in items if i["project"] == "PR-102"), None)
        assert pr102 is not None
        assert pr102["status"] == "in-progress"  # file status unchanged
        assert pr102["effective_status"] == "done"  # reconciled
        assert pr102["gh_state"] == "merged"

    def test_pr103_file_merged_also_done(self):
        """PR-103 file already says merged → effective_status=done regardless of gh."""
        items = _board_with_gh_mock({"103": "open"})  # gh says open but file says merged
        pr103 = next((i for i in items if i["project"] == "PR-103"), None)
        assert pr103 is not None
        assert pr103["effective_status"] == "done"  # file status is enough

    def test_pr100_open_stays_active(self):
        """PR-100 open on gh → effective_status=in-review (no reconciliation)."""
        items = _board_with_gh_mock({"100": "open"})
        pr100 = next((i for i in items if i["project"] == "PR-100"), None)
        assert pr100 is not None
        assert pr100["effective_status"] == "in-review"

    def test_closed_pr_also_done(self):
        """A PR that is closed (not merged) also flips to done."""
        items = _board_with_gh_mock({"101": "closed"})
        pr101 = next((i for i in items if i["project"] == "PR-101"), None)
        assert pr101 is not None
        assert pr101["effective_status"] == "done"
        assert pr101["gh_state"] == "closed"


# ---------------------------------------------------------------------------
# 4. gh-reconcile degrades gracefully when gh is unavailable
# ---------------------------------------------------------------------------

class TestGhDegrade:
    def test_gh_unknown_does_not_crash(self):
        """When gh returns unknown, board still renders (no exception)."""
        def _always_unknown(pr_numbers):
            return {pr: "unknown" for pr in pr_numbers}

        with patch("forum.board_projects._batch_gh_reconcile", side_effect=_always_unknown):
            items = read_project_board(FIXTURES_DIR)

        assert isinstance(items, list)
        assert len(items) > 0

    def test_gh_unknown_sets_flag(self):
        """Items with PR refs and unknown gh state have gh_unknown=True."""
        def _always_unknown(pr_numbers):
            return {pr: "unknown" for pr in pr_numbers}

        with patch("forum.board_projects._batch_gh_reconcile", side_effect=_always_unknown):
            items = read_project_board(FIXTURES_DIR)

        # PR-100 has a github ref → should have gh_unknown=True
        pr100 = next((i for i in items if i["project"] == "PR-100"), None)
        assert pr100 is not None
        assert pr100["gh_unknown"] is True

    def test_no_gh_ref_has_no_flag(self):
        """ISSUE-200 has no github ref → gh_unknown=False."""
        items = _board_with_gh_mock({})
        iss = next((i for i in items if i["project"] == "ISSUE-200"), None)
        assert iss is not None
        assert iss["gh_unknown"] is False

    def test_batch_exception_degrades(self):
        """If _batch_gh_reconcile raises, board still returns (no 500)."""
        def _raise(*args, **kwargs):
            raise RuntimeError("gh not available")

        with patch("forum.board_projects._batch_gh_reconcile", side_effect=_raise):
            items = read_project_board(FIXTURES_DIR)

        assert isinstance(items, list)
        # Items are returned but with unknown gh state
        for item in items:
            if item["github"]:
                assert item["gh_state"] == "unknown" or item["gh_state"] == ""


# ---------------------------------------------------------------------------
# 5. filter_updates: since-cursor correctness
# ---------------------------------------------------------------------------

class TestFilterUpdates:
    def _get_items(self):
        return _board_with_gh_mock({"100": "open", "101": "open", "102": "merged"})

    def test_since_epoch_returns_all_with_cursor_ts(self):
        """since=epoch (1970-01-01T00:00:00Z) returns all items with a cursor_ts."""
        items = self._get_items()
        updates = filter_updates(items, "1970-01-01T00:00:00Z", None)
        # All fixture files have a readable mtime → all have cursor_ts
        assert len(updates) == len([i for i in items if i["cursor_ts"]])

    def test_since_future_returns_empty(self):
        """since=future timestamp returns no items."""
        items = self._get_items()
        updates = filter_updates(items, "2099-12-31T23:59:59Z", None)
        assert updates == []

    def test_since_keys_on_cursor_ts_not_turn_since(self):
        """#1445 fix: the cursor keys on cursor_ts (file mtime = visibility), NOT
        turn_since. A backdated turn_since must not hide a recently-written item
        (that backdating is exactly what caused the silent missed wake)."""
        items = [
            # turn_since (display) BEFORE `since`, but mtime (cursor) AFTER it:
            # the old turn_since-keying would silently DROP this → the bug.
            {"project": "backdated", "turn": "ariadne",
             "updated_at": "2026-06-18T00:00:00Z",
             "cursor_ts":  "2026-06-20T00:00:00Z"},
            # mtime before `since` → genuinely not changed since → excluded.
            {"project": "old", "turn": "ariadne",
             "updated_at": "2026-06-20T00:00:00Z",
             "cursor_ts":  "2026-06-18T00:00:00Z"},
        ]
        ids = {u["project"] for u in filter_updates(items, "2026-06-19T00:00:00Z", None)}
        assert "backdated" in ids   # mtime > since → included (was dropped under the bug)
        assert "old" not in ids     # mtime < since → correctly excluded

    def test_since_none_returns_all(self):
        """since=None returns all items (no time filtering)."""
        items = self._get_items()
        updates = filter_updates(items, None, None)
        assert len(updates) == len(items)


# ---------------------------------------------------------------------------
# 6. filter_updates: agent filtering
# ---------------------------------------------------------------------------

class TestFilterUpdatesAgent:
    def _get_items(self):
        return _board_with_gh_mock({"100": "open", "101": "open"})

    def test_agent_filter_returns_only_that_courts_items(self):
        """?agent=ariadne returns only items where turn=ariadne."""
        items = self._get_items()
        updates = filter_updates(items, None, "ariadne")
        for u in updates:
            assert u["turn"] == "ariadne", f"{u['project']} has turn={u['turn']}, expected ariadne"

    def test_agent_filter_excludes_other_courts(self):
        """?agent=ariadne excludes items in borges's court."""
        items = self._get_items()
        updates = filter_updates(items, None, "ariadne")
        assert not any(u["turn"] == "borges" for u in updates)

    def test_agent_filter_plus_since(self):
        """Combined agent + since: filter both dimensions."""
        items = self._get_items()
        # Only ariadne's items after 2026-06-19
        updates = filter_updates(items, "2026-06-19T00:00:00Z", "ariadne")
        for u in updates:
            assert u["turn"] == "ariadne"
        pr101 = next((u for u in updates if u["project"] == "PR-101"), None)
        assert pr101 is None  # PR-101 is borges's, not ariadne's

    def test_agent_not_in_court_returns_empty(self):
        """?agent=nobody returns empty when nobody holds the baton."""
        items = self._get_items()
        updates = filter_updates(items, None, "nobody-holds-anything")
        assert updates == []


# ---------------------------------------------------------------------------
# 7. GET /api/board/projects — JSON endpoint
# ---------------------------------------------------------------------------

class TestApiBoardProjects:
    def test_returns_200(self, client):
        resp = client.get("/api/board/projects")
        assert resp.status_code == 200

    def test_response_has_board_key(self, client):
        data = json.loads(client.get("/api/board/projects").data)
        assert "board" in data

    def test_response_has_counts_key(self, client):
        data = json.loads(client.get("/api/board/projects").data)
        assert "counts" in data

    def test_board_is_list(self, client):
        data = json.loads(client.get("/api/board/projects").data)
        assert isinstance(data["board"], list)

    def test_counts_is_dict(self, client):
        data = json.loads(client.get("/api/board/projects").data)
        assert isinstance(data["counts"], dict)

    def test_board_items_have_required_fields(self, client):
        data = json.loads(client.get("/api/board/projects").data)
        required = {
            "project", "title", "kind", "status", "effective_status",
            "turn", "turn_since", "participants", "github", "gh_state", "updated_at",
            "cursor_ts",  # the /updates cursor key — guard against its removal
        }
        for item in data["board"]:
            missing = required - set(item.keys())
            assert not missing, f"API item {item['project']} missing: {missing}"

    def test_content_type_json(self, client):
        resp = client.get("/api/board/projects")
        assert "application/json" in resp.content_type


# ---------------------------------------------------------------------------
# 8. GET /api/board/updates — updates feed
# ---------------------------------------------------------------------------

class TestApiBoardUpdates:
    def test_returns_200(self, client):
        resp = client.get("/api/board/updates?since=1970-01-01T00:00:00Z")
        assert resp.status_code == 200

    def test_response_has_updates_key(self, client):
        data = json.loads(client.get("/api/board/updates?since=1970-01-01T00:00:00Z").data)
        assert "updates" in data

    def test_response_has_now_key(self, client):
        data = json.loads(client.get("/api/board/updates?since=1970-01-01T00:00:00Z").data)
        assert "now" in data
        # now should be a valid ISO-8601 string
        from datetime import datetime
        datetime.fromisoformat(data["now"].replace("Z", "+00:00"))

    def test_since_epoch_returns_all_items(self, client):
        data = json.loads(client.get("/api/board/updates?since=1970-01-01T00:00:00Z").data)
        assert len(data["updates"]) >= 4  # at least 4 fixture files

    def test_since_future_returns_empty(self, client):
        data = json.loads(client.get("/api/board/updates?since=2099-01-01T00:00:00Z").data)
        assert data["updates"] == []

    def test_agent_filter(self, client):
        data = json.loads(
            client.get("/api/board/updates?since=1970-01-01T00:00:00Z&agent=ariadne").data
        )
        for item in data["updates"]:
            assert item["turn"] == "ariadne"

    def test_invalid_since_returns_400(self, client):
        resp = client.get("/api/board/updates?since=not-a-date")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data

    def test_no_since_returns_all(self, client):
        """Omitting since should return all items (no filtering)."""
        data = json.loads(client.get("/api/board/updates").data)
        assert "updates" in data
        assert isinstance(data["updates"], list)


# ---------------------------------------------------------------------------
# 9. GET /board — HTML page
# ---------------------------------------------------------------------------

class TestHtmlBoard:
    def test_returns_200(self, client):
        resp = client.get("/board")
        assert resp.status_code == 200

    def test_content_type_html(self, client):
        resp = client.get("/board")
        assert "text/html" in resp.content_type

    def test_page_contains_project_title(self, client):
        body = client.get("/board").data.decode("utf-8")
        # Should contain the title of at least one fixture
        assert "Add project board feature" in body or "Project" in body

    def test_page_contains_board_heading(self, client):
        body = client.get("/board").data.decode("utf-8")
        assert "board" in body.lower() or "Board" in body


# ---------------------------------------------------------------------------
# 10. No writes to project .md files
# ---------------------------------------------------------------------------

class TestNoWrites:
    def test_fixture_files_not_modified(self):
        """read_project_board() does not modify any fixture .md file."""
        # Record mtimes before
        mtimes_before = {
            f: f.stat().st_mtime
            for f in FIXTURES_DIR.glob("*.md")
        }

        # Call read_project_board with mocked gh
        _board_with_gh_mock({"100": "open", "101": "open", "102": "merged"})

        # Verify mtimes after
        for f, mtime_before in mtimes_before.items():
            mtime_after = f.stat().st_mtime
            assert mtime_after == mtime_before, (
                f"File {f.name} was modified by read_project_board()!"
            )

    def test_api_does_not_write_files(self, client):
        """GET /api/board/projects does not modify any fixture .md file."""
        mtimes_before = {
            f: f.stat().st_mtime
            for f in FIXTURES_DIR.glob("*.md")
        }

        client.get("/api/board/projects")

        for f, mtime_before in mtimes_before.items():
            mtime_after = f.stat().st_mtime
            assert mtime_after == mtime_before, (
                f"File {f.name} was modified by the API endpoint!"
            )


# ---------------------------------------------------------------------------
# 11. Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_effective_status_gh_merged_overrides_inprogress(self):
        """_effective_status: gh merged overrides in-progress file status."""
        eff, gh = _effective_status("in-progress", "pr/42", {"42": "merged"})
        assert eff == "done"
        assert gh == "merged"

    def test_effective_status_gh_closed_overrides_inreview(self):
        """_effective_status: gh closed overrides in-review file status."""
        eff, gh = _effective_status("in-review", "pr/42", {"42": "closed"})
        assert eff == "done"
        assert gh == "closed"

    def test_effective_status_file_merged_no_gh_ref(self):
        """_effective_status: file merged, no gh ref → done."""
        eff, gh = _effective_status("merged", "", {})
        assert eff == "done"
        assert gh == ""

    def test_effective_status_open_pr_passthrough(self):
        """_effective_status: open PR → file status passes through."""
        eff, gh = _effective_status("in-review", "pr/42", {"42": "open"})
        assert eff == "in-review"
        assert gh == "open"

    def test_effective_status_unknown_gh_passthrough(self):
        """_effective_status: unknown gh state → file status passes through."""
        eff, gh = _effective_status("in-progress", "pr/42", {"42": "unknown"})
        assert eff == "in-progress"
        assert gh == "unknown"

    def test_get_board_counts(self):
        """get_board_counts tallies correctly."""
        items = [
            {"effective_status": "in-review"},
            {"effective_status": "in-review"},
            {"effective_status": "in-progress"},
            {"effective_status": "done"},
        ]
        counts = get_board_counts(items)
        assert counts["in-review"] == 2
        assert counts["in-progress"] == 1
        assert counts["done"] == 1

    def test_gh_pr_state_returns_unknown_on_gh_unavailable(self):
        """_gh_pr_state returns 'unknown' when gh is not on PATH."""
        with patch("forum.board_projects.subprocess.run", side_effect=FileNotFoundError):
            result = _gh_pr_state("999")
        assert result == "unknown"

    def test_gh_pr_state_returns_unknown_on_nonzero_exit(self):
        """_gh_pr_state returns 'unknown' on non-zero gh exit code."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "not found"
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("999")
        assert result == "unknown"

    def test_gh_pr_state_parses_merged(self):
        """_gh_pr_state returns 'merged' when gh reports MERGED."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "MERGED"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42")
        assert result == "merged"

    def test_gh_pr_state_parses_closed(self):
        """_gh_pr_state returns 'closed' when gh reports CLOSED."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "CLOSED"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42")
        assert result == "closed"

    def test_gh_pr_state_parses_open(self):
        """_gh_pr_state returns 'open' when gh reports OPEN."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "OPEN"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42")
        assert result == "open"

    def test_batch_gh_reconcile_deduplicates(self):
        """One _gh_pr_state call per distinct PR; result + cache populated.

        read_project_board pre-dedupes PR numbers before calling this, so the
        input is distinct. Clear the cache before/after so this test neither
        pollutes nor is polluted by the module-level gh cache.
        """
        from forum import board_projects as bp
        bp._clear_gh_cache()
        call_log = []

        def _fake_gh_pr_state(pr_num):
            call_log.append(pr_num)
            return "open"

        try:
            with patch("forum.board_projects._gh_pr_state", side_effect=_fake_gh_pr_state):
                result = _batch_gh_reconcile(["42", "43"])
            assert result == {"42": "open", "43": "open"}
            assert sorted(call_log) == ["42", "43"]  # one call per distinct PR
        finally:
            bp._clear_gh_cache()


# ---------------------------------------------------------------------------
# gh-state TTL cache (perf: bounds gh calls under frequent /updates polling)
# ---------------------------------------------------------------------------

def test_gh_cache_suppresses_repeat_calls():
    """Second reconcile within TTL must not re-spawn gh (one call/PR/window).

    Guards the fix for the ~2s-polled /api/board/updates fan-out: without the
    cache, every poll across every loop-agent spawns a gh subprocess per PR.
    """
    from forum import board_projects as bp
    bp._clear_gh_cache()
    try:
        with patch("forum.board_projects._gh_pr_state", return_value="merged") as m:
            assert bp._batch_gh_reconcile(["1005"]) == {"1005": "merged"}
            assert bp._batch_gh_reconcile(["1005"]) == {"1005": "merged"}  # cached
            assert m.call_count == 1
    finally:
        bp._clear_gh_cache()


def test_gh_cache_does_not_cache_unknown():
    """'unknown' is not cached, so a transient gh outage self-heals next call."""
    from forum import board_projects as bp
    bp._clear_gh_cache()
    try:
        with patch("forum.board_projects._gh_pr_state", return_value="unknown") as m:
            bp._batch_gh_reconcile(["1005"])
            bp._batch_gh_reconcile(["1005"])  # not cached → re-queries
            assert m.call_count == 2
    finally:
        bp._clear_gh_cache()


# ---------------------------------------------------------------------------
# /api/board/updates cursor contract (as_of captured before read; echo-back)
# ---------------------------------------------------------------------------

def test_updates_returns_as_of_cursor(client):
    """Response carries a server-authoritative `as_of` (ISO) + `now` alias."""
    import time as _t
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    resp = client.get("/api/board/updates")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "as_of" in body and "updates" in body
    assert body["now"] == body["as_of"]  # alias for one release
    as_of = datetime.fromisoformat(body["as_of"].replace("Z", "+00:00"))
    # as_of is captured at request time (not in the future, not absurdly stale).
    assert before.replace(microsecond=0) <= as_of <= datetime.now(timezone.utc)


def test_updates_echo_cursor_excludes_prior_items(client):
    """Echoing as_of back as `since` (exclusive) returns no already-seen items."""
    first = client.get("/api/board/updates").get_json()
    cursor = first["as_of"]
    second = client.get(f"/api/board/updates?since={cursor}").get_json()
    first_ids = {u["project"] for u in first["updates"]}
    second_ids = {u["project"] for u in second["updates"]}
    # Nothing changed between the two calls → no item re-appears past the cursor.
    assert first_ids & second_ids == set()


# ---------------------------------------------------------------------------
# Presence join + grouping route param (regression guards from PR #1446 review)
# ---------------------------------------------------------------------------

def test_presence_join_reflects_published_state(client):
    """A published 'working' status must surface on cards — not silently offline.

    Regression guard for the status_state-vs-state key bug: list_board emits the
    resolved status under key 'state', and the per-card join must read that.
    """
    r = client.post("/api/agents/status", json={"agent": "ariadne", "state": "working"})
    assert r.status_code == 200
    html = client.get("/board").get_data(as_text=True)
    assert "pdot--working" in html  # would be all 'pdot--offline' under the bug


def test_presence_unpublished_agent_is_offline(client):
    """Agents with no published status render offline — no KeyError, page 200."""
    resp = client.get("/board")
    assert resp.status_code == 200
    assert "pdot--offline" in resp.get_data(as_text=True)


def test_board_group_by_param_accepted(client):
    """?group_by= is the extensibility seam; known + unknown axes both 200."""
    assert client.get("/board?group_by=status").status_code == 200
    assert client.get("/board?group_by=nonsense").status_code == 200  # falls back
