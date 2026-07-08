"""Tests for the project board read model and routes.

#1608: repointed off the pre-cutover BATON_PROJECTS_DIR/*.md glob (dead since
the 2026-06-27 UCS cutover — see board_projects.py's module docstring) onto
the live coordination store (the same `CoordinationStore.read_projects()`
`GET /api/projects` already serves). Fixtures are now seeded into a real
`FileStore` via the `forum.coordination` writer-fns instead of static .md
fixture files.

Covers:
1. Read model reads store-backed ProjectRecords correctly.
2. A record with no parseable frontmatter is skipped; other items still render.
3. gh-reconcile: a fixture with a PR ref flips merged→done when gh mock says merged.
4. gh-reconcile degrades gracefully when gh is unavailable (gh_unknown=True, page loads).
5. /api/board/updates?since=<seq> correctness (seq-cursor filtering, #1608).
6. /api/board/updates?agent= filters to that agent's court.
7. /api/board/projects returns 200 with board/counts keys; 503 without a store.
8. /board returns 200; degrades to an empty board (200, never 500/503) without a store.
9. No code path writes to the coordination store.
"""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from forum.board_projects import (
    DEFAULT_GITHUB_REPO,
    _batch_gh_reconcile,
    _effective_status,
    _gh_pr_state,
    filter_updates,
    get_board_counts,
    read_project_board,
)
from forum.coordination import FileStore, SeqAllocator, init as _init
from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Store fixture — seeds a FileStore with the same scenarios the retired
# fixtures/projects/*.md set covered (same project ids, statuses, github refs).
# ---------------------------------------------------------------------------

def _seed_store(store: FileStore, allocator: SeqAllocator) -> None:
    _init(store, allocator, "PR-100",
          title="Add project board feature", status="in-review", turn="ariadne",
          participants=["borges", "ariadne", "lei"],
          turn_reason="reviewer-fairy converged; colleague fresh-eye requested",
          github="pr/100", ts="2026-06-20T10:00:00Z")
    _init(store, allocator, "PR-101",
          title="Fix baton flip guard", status="in-progress", turn="borges",
          participants=["borges", "ariadne"], turn_reason="claimed from pool",
          github="pr/101", ts="2026-06-18T14:30:00Z")
    _init(store, allocator, "PR-102",
          title="Already merged PR that baton file still says in-progress",
          status="in-progress", turn="lei",
          participants=["kepler", "borges", "lei"], turn_reason="presented for merge",
          github="pr/102", ts="2026-06-15T08:00:00Z")
    _init(store, allocator, "PR-103",
          title="Merged PR with file status already merged", status="merged",
          turn="lei", participants=["ariadne", "lei"], turn_reason="merged",
          github="pr/103", ts="2026-06-10T08:00:00Z")
    _init(store, allocator, "ISSUE-200",
          title="Forum search indexing latency", status="planning", turn="lei",
          participants=["borges", "ariadne", "lei"],
          turn_reason="design discussion needed", ts="2026-06-21T12:00:00Z")
    # Malformed: no frontmatter at all — written directly (bypassing init(), which
    # always emits well-formed frontmatter), the store-backed equivalent of the
    # retired MALFORMED.md fixture.
    (store.projects_dir / "MALFORMED.md").write_text(
        "# This is a plain markdown file with no frontmatter\n\n"
        "It should be skipped gracefully by the board parser without causing errors.\n"
    )


@pytest.fixture
def store(tmp_path) -> FileStore:
    s = FileStore(tmp_path / "coord")
    alloc = SeqAllocator(recover=s.recover_max_seq)
    _seed_store(s, alloc)
    return s


@pytest.fixture
def allocator(store) -> SeqAllocator:
    # Recovers from the seeded on-disk high-water-mark, mirroring what a
    # freshly-started server process's allocator would see (matches the
    # store/allocator pairing convention in test_projects_api.py).
    return SeqAllocator(recover=store.recover_max_seq)


# ---------------------------------------------------------------------------
# Flask app fixture (backed by an in-memory DB + the seeded FileStore)
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path, store, allocator):
    """Flask app backed by a temp DB, wired to the seeded coordination store."""
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    application.config["COORD_STORE"] = store
    application.config["COORD_ALLOCATOR"] = allocator
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helper: read_project_board from the seeded store, mocking gh calls
# ---------------------------------------------------------------------------

def _board_with_gh_mock(store, gh_mock: dict) -> list:
    """Call read_project_board(store) with gh state mocked.

    Args:
        gh_mock: dict mapping pr_number string → state string (bare number,
            not repo-qualified -- #1715 threaded (repo, pr_number) tuples
            through the real reconciliation, but every fixture in this file
            uses an unqualified pr/<N> anchor, which always resolves to
            DEFAULT_GITHUB_REPO, so this helper still takes bare pr_number
            keys for readability and translates to the real tuple keys).
            Any PR number not in the dict returns 'unknown'.
    """
    def _fake_batch(pr_refs):
        return {(repo, num): gh_mock.get(num, "unknown") for repo, num in pr_refs}

    with patch("forum.board_projects._batch_gh_reconcile", side_effect=_fake_batch):
        return read_project_board(store)


# ---------------------------------------------------------------------------
# 1. Read model: reads store-backed records
# ---------------------------------------------------------------------------

class TestReadModel:
    def test_returns_list(self, store):
        """read_project_board returns a list."""
        items = _board_with_gh_mock(store, {})
        assert isinstance(items, list)

    def test_returns_empty_list_when_store_is_none(self):
        """A None store (COORD_STORE not configured) degrades to []."""
        assert read_project_board(None) == []

    def test_parses_pr_100(self, store):
        """PR-100 fixture is parsed with correct fields."""
        items = _board_with_gh_mock(store, {"100": "open"})
        pr100 = next((i for i in items if i["project"] == "PR-100"), None)
        assert pr100 is not None, "PR-100 should be in the board"
        assert pr100["title"] == "Add project board feature"
        assert pr100["status"] == "in-review"
        assert pr100["turn"] == "ariadne"
        assert pr100["kind"] == "pr"
        assert pr100["github"] == "pr/100"
        assert "ariadne" in pr100["participants"]
        assert "lei" in pr100["participants"]
        assert isinstance(pr100["seq"], int) and pr100["seq"] > 0

    def test_parses_issue_200(self, store):
        """ISSUE-200 fixture is parsed as kind='issue'."""
        items = _board_with_gh_mock(store, {})
        iss = next((i for i in items if i["project"] == "ISSUE-200"), None)
        assert iss is not None
        assert iss["kind"] == "issue"
        assert iss["github"] == ""  # no github field

    def test_all_projects_parsed(self, store):
        """All valid fixture records are parsed (malformed is skipped)."""
        items = _board_with_gh_mock(store, {"100": "open", "101": "open", "102": "merged", "103": "merged"})
        project_ids = {i["project"] for i in items}
        assert "PR-100" in project_ids
        assert "PR-101" in project_ids
        assert "PR-102" in project_ids
        assert "PR-103" in project_ids
        assert "ISSUE-200" in project_ids

    def test_required_fields_present(self, store):
        """Every item has all required fields."""
        items = _board_with_gh_mock(store, {"100": "open"})
        required = {
            "project", "title", "kind", "status", "effective_status",
            "turn", "turn_since", "participants", "github", "gh_state", "updated_at",
            "seq",  # the /updates cursor key post-#1608 — guard against its removal
        }
        for item in items:
            missing = required - set(item.keys())
            assert not missing, f"Item {item['project']} missing fields: {missing}"

    def test_age_str_present(self, store):
        """age_str is present (may be empty string for items with no turn_since)."""
        items = _board_with_gh_mock(store, {"100": "open"})
        for item in items:
            assert "age_str" in item


# ---------------------------------------------------------------------------
# 2. Malformed record is skipped; others render
# ---------------------------------------------------------------------------

class TestMalformedSkipped:
    def test_malformed_not_in_board(self, store):
        """A record with no frontmatter is skipped; it never appears in board."""
        items = _board_with_gh_mock(store, {})
        project_ids = {i["project"] for i in items}
        assert "MALFORMED" not in project_ids

    def test_other_items_still_rendered(self, store):
        """After skipping malformed, other items still render."""
        items = _board_with_gh_mock(store, {"100": "open"})
        project_ids = {i["project"] for i in items}
        assert "PR-100" in project_ids
        assert "ISSUE-200" in project_ids


# ---------------------------------------------------------------------------
# 3. gh-reconcile: merged PR flips to effective_status=done
# ---------------------------------------------------------------------------

class TestGhReconcile:
    def test_pr102_reconciled_to_done(self, store):
        """PR-102 file says in-progress but gh says merged → effective_status=done."""
        items = _board_with_gh_mock(store, {"102": "merged", "100": "open", "101": "open"})
        pr102 = next((i for i in items if i["project"] == "PR-102"), None)
        assert pr102 is not None
        assert pr102["status"] == "in-progress"  # file status unchanged
        assert pr102["effective_status"] == "done"  # reconciled
        assert pr102["gh_state"] == "merged"

    def test_pr103_file_merged_also_done(self, store):
        """PR-103 file already says merged → effective_status=done regardless of gh."""
        items = _board_with_gh_mock(store, {"103": "open"})  # gh says open but file says merged
        pr103 = next((i for i in items if i["project"] == "PR-103"), None)
        assert pr103 is not None
        assert pr103["effective_status"] == "done"  # file status is enough

    def test_pr100_open_stays_active(self, store):
        """PR-100 open on gh → effective_status=in-review (no reconciliation)."""
        items = _board_with_gh_mock(store, {"100": "open"})
        pr100 = next((i for i in items if i["project"] == "PR-100"), None)
        assert pr100 is not None
        assert pr100["effective_status"] == "in-review"

    def test_closed_pr_also_done(self, store):
        """A PR that is closed (not merged) also flips to done."""
        items = _board_with_gh_mock(store, {"101": "closed"})
        pr101 = next((i for i in items if i["project"] == "PR-101"), None)
        assert pr101 is not None
        assert pr101["effective_status"] == "done"
        assert pr101["gh_state"] == "closed"


# ---------------------------------------------------------------------------
# 4. gh-reconcile degrades gracefully when gh is unavailable
# ---------------------------------------------------------------------------

class TestGhDegrade:
    def test_gh_unknown_does_not_crash(self, store):
        """When gh returns unknown, board still renders (no exception)."""
        def _always_unknown(pr_refs):
            return {ref: "unknown" for ref in pr_refs}

        with patch("forum.board_projects._batch_gh_reconcile", side_effect=_always_unknown):
            items = read_project_board(store)

        assert isinstance(items, list)
        assert len(items) > 0

    def test_gh_unknown_sets_flag(self, store):
        """Items with PR refs and unknown gh state have gh_unknown=True."""
        def _always_unknown(pr_refs):
            return {ref: "unknown" for ref in pr_refs}

        with patch("forum.board_projects._batch_gh_reconcile", side_effect=_always_unknown):
            items = read_project_board(store)

        pr100 = next((i for i in items if i["project"] == "PR-100"), None)
        assert pr100 is not None
        assert pr100["gh_unknown"] is True

    def test_no_gh_ref_has_no_flag(self, store):
        """ISSUE-200 has no github ref → gh_unknown=False."""
        items = _board_with_gh_mock(store, {})
        iss = next((i for i in items if i["project"] == "ISSUE-200"), None)
        assert iss is not None
        assert iss["gh_unknown"] is False

    def test_batch_exception_degrades(self, store):
        """If _batch_gh_reconcile raises, board still returns (no 500)."""
        def _raise(*args, **kwargs):
            raise RuntimeError("gh not available")

        with patch("forum.board_projects._batch_gh_reconcile", side_effect=_raise):
            items = read_project_board(store)

        assert isinstance(items, list)
        for item in items:
            if item["github"]:
                assert item["gh_state"] == "unknown" or item["gh_state"] == ""


# ---------------------------------------------------------------------------
# 5. filter_updates: seq-cursor correctness (#1608)
# ---------------------------------------------------------------------------

class TestFilterUpdates:
    def _get_items(self, store):
        return _board_with_gh_mock(store, {"100": "open", "101": "open", "102": "merged"})

    def test_since_zero_returns_all(self, store):
        """since=0 (below every real seq) returns all items."""
        items = self._get_items(store)
        updates = filter_updates(items, 0, None)
        assert len(updates) == len(items)

    def test_since_far_future_seq_returns_empty(self, store):
        """since beyond every item's seq returns no items."""
        items = self._get_items(store)
        updates = filter_updates(items, 999999, None)
        assert updates == []

    def test_since_excludes_at_or_below_only(self, store):
        """since is EXCLUSIVE: an item at exactly `since` is excluded, one above is included."""
        items = self._get_items(store)
        seqs = sorted(i["seq"] for i in items)
        mid = seqs[len(seqs) // 2]
        updates = filter_updates(items, mid, None)
        assert all(u["seq"] > mid for u in updates)
        assert not any(i["seq"] == mid and i in updates for i in items)

    def test_since_keys_on_seq_not_turn_since(self):
        """#1608: the cursor keys on `seq` — the store's monotonic, co-atomically
        assigned mutation counter — NOT turn_since (a writer-stamped display value
        that can be backdated). A backdated turn_since must not hide a later-committed
        item (mirrors the #1445 regression this replaces the mtime-cursor version of)."""
        items = [
            # turn_since (display) BEFORE the other item's, but seq (cursor) AFTER it:
            # a turn_since-keyed cursor would silently drop this — the #1445-class bug.
            {"project": "backdated", "turn": "ariadne",
             "turn_since": "2026-06-18T00:00:00Z", "seq": 5},
            {"project": "old", "turn": "ariadne",
             "turn_since": "2026-06-20T00:00:00Z", "seq": 2},
        ]
        ids = {u["project"] for u in filter_updates(items, 3, None)}
        assert "backdated" in ids   # seq 5 > since 3 → included
        assert "old" not in ids     # seq 2 <= since 3 → correctly excluded

    def test_since_none_returns_all(self, store):
        """since=None returns all items (no since-filtering)."""
        items = self._get_items(store)
        updates = filter_updates(items, None, None)
        assert len(updates) == len(items)


# ---------------------------------------------------------------------------
# 6. filter_updates: agent filtering
# ---------------------------------------------------------------------------

class TestFilterUpdatesAgent:
    def _get_items(self, store):
        return _board_with_gh_mock(store, {"100": "open", "101": "open"})

    def test_agent_filter_returns_only_that_courts_items(self, store):
        """?agent=ariadne returns only items where turn=ariadne."""
        items = self._get_items(store)
        updates = filter_updates(items, None, "ariadne")
        for u in updates:
            assert u["turn"] == "ariadne", f"{u['project']} has turn={u['turn']}, expected ariadne"

    def test_agent_filter_excludes_other_courts(self, store):
        """?agent=ariadne excludes items in borges's court."""
        items = self._get_items(store)
        updates = filter_updates(items, None, "ariadne")
        assert not any(u["turn"] == "borges" for u in updates)

    def test_agent_filter_plus_since(self, store):
        """Combined agent + since: filter both dimensions."""
        items = self._get_items(store)
        updates = filter_updates(items, 0, "ariadne")
        for u in updates:
            assert u["turn"] == "ariadne"
        assert not any(u["project"] == "PR-101" for u in updates)  # PR-101 is borges's

    def test_agent_not_in_court_returns_empty(self, store):
        """?agent=nobody returns empty when nobody holds the baton."""
        items = self._get_items(store)
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
            "seq",  # the /updates cursor key post-#1608 — guard against its removal
        }
        for item in data["board"]:
            missing = required - set(item.keys())
            assert not missing, f"API item {item['project']} missing: {missing}"

    def test_content_type_json(self, client):
        resp = client.get("/api/board/projects")
        assert "application/json" in resp.content_type

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.get("/api/board/projects").status_code == 503


# ---------------------------------------------------------------------------
# 8. GET /api/board/updates — updates feed (seq cursor, #1608)
# ---------------------------------------------------------------------------

class TestApiBoardUpdates:
    def test_returns_200(self, client):
        resp = client.get("/api/board/updates?since=0")
        assert resp.status_code == 200

    def test_response_has_updates_key(self, client):
        data = json.loads(client.get("/api/board/updates?since=0").data)
        assert "updates" in data

    def test_response_has_as_of_int_key(self, client):
        data = json.loads(client.get("/api/board/updates?since=0").data)
        assert "as_of" in data
        assert isinstance(data["as_of"], int)

    def test_now_alias_retired(self, client):
        """The deprecated `now` ISO-alias is retired (#1608) — the cursor's type
        itself changed in this PR, so completing the announced deprecation here
        avoids a second breaking change later."""
        data = json.loads(client.get("/api/board/updates?since=0").data)
        assert "now" not in data

    def test_since_zero_returns_all_items(self, client):
        data = json.loads(client.get("/api/board/updates?since=0").data)
        assert len(data["updates"]) >= 4  # at least 4 fixture records

    def test_since_far_future_seq_returns_empty(self, client):
        data = json.loads(client.get("/api/board/updates?since=999999").data)
        assert data["updates"] == []

    def test_agent_filter(self, client):
        data = json.loads(
            client.get("/api/board/updates?since=0&agent=ariadne").data
        )
        for item in data["updates"]:
            assert item["turn"] == "ariadne"

    def test_invalid_since_returns_400(self, client):
        resp = client.get("/api/board/updates?since=not-a-date")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data

    def test_negative_since_clamps_to_zero(self, client):
        """Negative since is clamped to 0 (mirrors /api/updates), not rejected."""
        resp = client.get("/api/board/updates?since=-5")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["updates"]) >= 4

    def test_no_since_returns_all(self, client):
        """Omitting since defaults to 0 (no filtering)."""
        data = json.loads(client.get("/api/board/updates").data)
        assert "updates" in data
        assert isinstance(data["updates"], list)
        assert len(data["updates"]) >= 4

    def test_503_without_store(self, tmp_path):
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        assert c.get("/api/board/updates").status_code == 503


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
        assert "Add project board feature" in body or "Project" in body

    def test_page_contains_board_heading(self, client):
        body = client.get("/board").data.decode("utf-8")
        assert "board" in body.lower() or "Board" in body

    def test_degrades_to_empty_board_without_store(self, tmp_path):
        """/board never 500s/503s — an unconfigured store renders an empty board."""
        db_path = str(tmp_path / "f.db")
        audit = str(tmp_path / "a.jsonl")
        conn = sqlite3.connect(db_path)
        init_db(conn)
        conn.close()
        app = create_app(db_path, audit)
        app.config["TESTING"] = True
        c = app.test_client()
        resp = c.get("/board")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 10. No writes to the coordination store
# ---------------------------------------------------------------------------

class TestNoWrites:
    def test_store_files_not_modified(self, store):
        """read_project_board() does not modify any project file in the store."""
        mtimes_before = {
            f: f.stat().st_mtime
            for f in store.projects_dir.glob("*.md")
        }

        _board_with_gh_mock(store, {"100": "open", "101": "open", "102": "merged"})

        for f, mtime_before in mtimes_before.items():
            mtime_after = f.stat().st_mtime
            assert mtime_after == mtime_before, (
                f"File {f.name} was modified by read_project_board()!"
            )

    def test_api_does_not_write_files(self, client, store):
        mtimes_before = {
            f: f.stat().st_mtime
            for f in store.projects_dir.glob("*.md")
        }

        client.get("/api/board/projects")

        for f, mtime_before in mtimes_before.items():
            mtime_after = f.stat().st_mtime
            assert mtime_after == mtime_before, (
                f"File {f.name} was modified by the API endpoint!"
            )


# ---------------------------------------------------------------------------
# 11. Unit tests for helper functions (unchanged by #1608 — pure logic)
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_effective_status_gh_merged_overrides_inprogress(self):
        """_effective_status: gh merged overrides in-progress file status."""
        eff, gh = _effective_status("in-progress", "pr/42", {(DEFAULT_GITHUB_REPO, "42"): "merged"})
        assert eff == "done"
        assert gh == "merged"

    def test_effective_status_gh_closed_overrides_inreview(self):
        """_effective_status: gh closed overrides in-review file status."""
        eff, gh = _effective_status("in-review", "pr/42", {(DEFAULT_GITHUB_REPO, "42"): "closed"})
        assert eff == "done"
        assert gh == "closed"

    def test_effective_status_file_merged_no_gh_ref(self):
        """_effective_status: file merged, no gh ref → done."""
        eff, gh = _effective_status("merged", "", {})
        assert eff == "done"
        assert gh == ""

    def test_effective_status_open_pr_passthrough(self):
        """_effective_status: open PR → file status passes through."""
        eff, gh = _effective_status("in-review", "pr/42", {(DEFAULT_GITHUB_REPO, "42"): "open"})
        assert eff == "in-review"
        assert gh == "open"

    def test_effective_status_unknown_gh_passthrough(self):
        """_effective_status: unknown gh state → file status passes through."""
        eff, gh = _effective_status("in-progress", "pr/42", {(DEFAULT_GITHUB_REPO, "42"): "unknown"})
        assert eff == "in-progress"
        assert gh == "unknown"

    def test_effective_status_repo_qualified_anchor(self):
        """_effective_status: a repo-qualified anchor (#1715) resolves against
        that repo's key in gh_states, not DEFAULT_GITHUB_REPO."""
        eff, gh = _effective_status(
            "in-progress", "pr/engram-agents/engram-paper/22",
            {("engram-agents/engram-paper", "22"): "merged"},
        )
        assert eff == "done"
        assert gh == "merged"

    def test_effective_status_repo_qualified_does_not_collide_with_default(self):
        """A repo-qualified anchor must NOT be satisfied by a DEFAULT_GITHUB_REPO
        entry for the same bare number -- this is the exact #1715 collision
        shape (engram-paper PR-22 must never read engram-alpha's PR #22)."""
        eff, gh = _effective_status(
            "in-progress", "pr/engram-agents/engram-paper/22",
            {(DEFAULT_GITHUB_REPO, "22"): "merged"},  # wrong repo's entry
        )
        assert eff == "in-progress"  # not reconciled -- falls to 'unknown'
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
            result = _gh_pr_state("999", DEFAULT_GITHUB_REPO)
        assert result == "unknown"

    def test_gh_pr_state_returns_unknown_on_nonzero_exit(self):
        """_gh_pr_state returns 'unknown' on non-zero gh exit code."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "not found"
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("999", DEFAULT_GITHUB_REPO)
        assert result == "unknown"

    def test_gh_pr_state_parses_merged(self):
        """_gh_pr_state returns 'merged' when gh reports MERGED."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "MERGED"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42", DEFAULT_GITHUB_REPO)
        assert result == "merged"

    def test_gh_pr_state_parses_closed(self):
        """_gh_pr_state returns 'closed' when gh reports CLOSED."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "CLOSED"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42", DEFAULT_GITHUB_REPO)
        assert result == "closed"

    def test_gh_pr_state_parses_open(self):
        """_gh_pr_state returns 'open' when gh reports OPEN."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "OPEN"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42", DEFAULT_GITHUB_REPO)
        assert result == "open"

    def test_batch_gh_reconcile_deduplicates(self):
        """One _gh_pr_state call per distinct (repo, PR); result + cache populated.

        read_project_board pre-dedupes PR refs before calling this, so the
        input is distinct. Clear the cache before/after so this test neither
        pollutes nor is polluted by the module-level gh cache.
        """
        from forum import board_projects as bp
        bp._clear_gh_cache()
        call_log = []

        def _fake_gh_pr_state(pr_num, repo):
            call_log.append((repo, pr_num))
            return "open"

        try:
            with patch("forum.board_projects._gh_pr_state", side_effect=_fake_gh_pr_state):
                result = _batch_gh_reconcile([(DEFAULT_GITHUB_REPO, "42"), (DEFAULT_GITHUB_REPO, "43")])
            assert result == {(DEFAULT_GITHUB_REPO, "42"): "open", (DEFAULT_GITHUB_REPO, "43"): "open"}
            assert sorted(call_log) == [(DEFAULT_GITHUB_REPO, "42"), (DEFAULT_GITHUB_REPO, "43")]
        finally:
            bp._clear_gh_cache()

    def test_batch_gh_reconcile_same_number_different_repo_does_not_collide(self):
        """#1715's core regression: two different repos' PR of the SAME
        number must resolve independently, never share a cache entry or a
        gh query result."""
        from forum import board_projects as bp
        bp._clear_gh_cache()

        def _fake_gh_pr_state(pr_num, repo):
            return "merged" if repo == DEFAULT_GITHUB_REPO else "open"

        try:
            with patch("forum.board_projects._gh_pr_state", side_effect=_fake_gh_pr_state):
                result = _batch_gh_reconcile([
                    (DEFAULT_GITHUB_REPO, "22"),
                    ("engram-agents/engram-paper", "22"),
                ])
            assert result[(DEFAULT_GITHUB_REPO, "22")] == "merged"
            assert result[("engram-agents/engram-paper", "22")] == "open"
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
    ref = (DEFAULT_GITHUB_REPO, "1005")
    try:
        with patch("forum.board_projects._gh_pr_state", return_value="merged") as m:
            assert bp._batch_gh_reconcile([ref]) == {ref: "merged"}
            assert bp._batch_gh_reconcile([ref]) == {ref: "merged"}  # cached
            assert m.call_count == 1
    finally:
        bp._clear_gh_cache()


def test_gh_cache_does_not_cache_unknown():
    """'unknown' is not cached, so a transient gh outage self-heals next call."""
    from forum import board_projects as bp
    bp._clear_gh_cache()
    ref = (DEFAULT_GITHUB_REPO, "1005")
    try:
        with patch("forum.board_projects._gh_pr_state", return_value="unknown") as m:
            bp._batch_gh_reconcile([ref])
            bp._batch_gh_reconcile([ref])  # not cached → re-queries
            assert m.call_count == 2
    finally:
        bp._clear_gh_cache()


# ---------------------------------------------------------------------------
# /api/board/updates cursor contract (as_of captured before read; echo-back)
# ---------------------------------------------------------------------------

def test_updates_returns_as_of_seq_cursor(client, allocator):
    """Response carries a server-authoritative int `as_of` (the seq watermark)."""
    resp = client.get("/api/board/updates")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "as_of" in body and "updates" in body
    assert isinstance(body["as_of"], int)
    # as_of is the committed watermark at read time — must not exceed the
    # allocator's current value (never names an in-flight/future write).
    assert body["as_of"] <= allocator.current()


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
