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

# #1918/#1919: there is no default repo any more, so fixtures carry a
# repo-qualified anchor (pr/<owner>/<repo>/<N>) and name its repo explicitly.
_REPO = "example-org/example-repo"


# ---------------------------------------------------------------------------
# Store fixture — seeds a FileStore with the same scenarios the retired
# fixtures/projects/*.md set covered (same project ids, statuses, github refs).
# ---------------------------------------------------------------------------

def _seed_store(store: FileStore, allocator: SeqAllocator) -> None:
    _init(store, allocator, "PR-100",
          title="Add project board feature", status="in-review", turn="ariadne",
          participants=["borges", "ariadne", "lei"],
          turn_reason="reviewer-fairy converged; colleague fresh-eye requested",
          github="pr/example-org/example-repo/100", ts="2026-06-20T10:00:00Z")
    _init(store, allocator, "PR-101",
          title="Fix baton flip guard", status="in-progress", turn="borges",
          participants=["borges", "ariadne"], turn_reason="claimed from pool",
          github="pr/example-org/example-repo/101", ts="2026-06-18T14:30:00Z")
    _init(store, allocator, "PR-102",
          title="Already merged PR that baton file still says in-progress",
          status="in-progress", turn="lei",
          participants=["kepler", "borges", "lei"], turn_reason="presented for merge",
          github="pr/example-org/example-repo/102", ts="2026-06-15T08:00:00Z")
    _init(store, allocator, "PR-103",
          title="Merged PR with file status already merged", status="merged",
          turn="lei", participants=["ariadne", "lei"], turn_reason="merged",
          github="pr/example-org/example-repo/103", ts="2026-06-10T08:00:00Z")
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
    application = create_app(db_path, audit_path, enable_board_viewer=True)
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
            through the real reconciliation, and every seeded fixture in
            this file uses a pr/<_REPO>/<N> anchor, so this helper still
            takes bare pr_number keys for readability and translates to the
            real tuple keys).
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
        assert pr100["github"] == "pr/example-org/example-repo/100"
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
        """PR-103 file already says merged → effective_status=done regardless of gh.

        #2007: PR-103's ref is skipped from the resolve set entirely (file
        status already file-done), so the {"103": "open"} mock value is never
        even consulted -- gh_state reports 'skipped', not the mocked 'open'.
        """
        items = _board_with_gh_mock(store, {"103": "open"})  # gh says open but file says merged
        pr103 = next((i for i in items if i["project"] == "PR-103"), None)
        assert pr103 is not None
        assert pr103["effective_status"] == "done"  # file status is enough
        assert pr103["gh_state"] == "skipped"

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
# 3b. #2007: gh reconciliation is skipped for already-file-done records
# ---------------------------------------------------------------------------

class TestSkipReconcileForFileDone:
    """#2007: resolving gh for a record whose file status is already in
    _FILE_DONE_STATUSES can never change its effective_status (see
    _effective_status's file_done branch) -- so those refs are never added
    to the resolve set at all. These tests prove that guarantee directly
    (gh is never actually invoked for such a ref), not just that the
    resulting effective_status looks right -- a test that only checked the
    latter would pass even if the skip never happened, since the file
    status alone already forces 'done'.
    """

    def test_file_done_record_never_calls_gh(self, store):
        """PR-103 (file status already 'merged') must never trigger a real gh
        lookup -- even one that WOULD flip effective_status away from 'done'
        if it were honored. This is the load-bearing safety guarantee.
        """
        from forum import board_projects as bp
        bp._clear_gh_cache()

        def _fake_gh_pr_state(pr_num, repo):
            # Deliberately returns a state that would flip PR-103 off 'done'
            # were it ever consulted for PR-103's ref -- proving the skip
            # actually happens structurally, not just that the result
            # happens to look right.
            return "open"

        try:
            with patch(
                "forum.board_projects._gh_pr_state", side_effect=_fake_gh_pr_state
            ) as mock_gh:
                items = read_project_board(store)
        finally:
            bp._clear_gh_cache()

        pr103 = next((i for i in items if i["project"] == "PR-103"), None)
        assert pr103 is not None
        assert pr103["effective_status"] == "done"
        assert pr103["gh_state"] == "skipped"

        called_pr_nums = {call.args[0] for call in mock_gh.call_args_list}
        assert "103" not in called_pr_nums, (
            "gh must never be queried for an already-file-done record"
        )

    def test_file_done_reports_skipped_not_unknown(self, store):
        """A file-done record's gh_state is 'skipped', not 'unknown' -- and
        gh_unknown stays False (no 'something's wrong' warning for a
        deliberate, structural skip)."""
        items = _board_with_gh_mock(store, {"103": "merged"})
        pr103 = next((i for i in items if i["project"] == "PR-103"), None)
        assert pr103 is not None
        assert pr103["gh_state"] == "skipped"
        assert pr103["gh_unknown"] is False

    def test_shared_ref_active_gets_real_state_done_also_reports_it(self, tmp_path):
        """Two records share one PR ref: one active ('in-review', drives the
        resolve), one file-done ('merged', would be skipped alone). The
        shared ref DOES get resolved (driven by the active record) -- so the
        file-done record reports the REAL resolved gh_state, never 'skipped'.

        Judgment call (not fully dictated by the spec): when a ref IS
        resolved this call (because some other record sharing it is active),
        the file-done record reports that real state rather than always
        reporting 'skipped' for itself individually. Chosen because it's what
        the "was this ref present in gh_states this call" discriminator
        naturally produces without extra per-record bookkeeping, and because
        it's strictly more informative (ground truth is already in hand) --
        flagged here for review since a per-record 'skipped' would also have
        been defensible.
        """
        store = FileStore(tmp_path / "coord")
        alloc = SeqAllocator(recover=store.recover_max_seq)
        _init(
            store, alloc, "PR-500", title="Active PR sharing a ref",
            status="in-review", turn="ariadne", participants=["ariadne"],
            turn_reason="x", github="pr/example-org/example-repo/999", ts="2026-06-20T10:00:00Z",
        )
        _init(
            store, alloc, "PR-501", title="Done PR, same ref",
            status="merged", turn="lei", participants=["lei"],
            turn_reason="merged", github="pr/example-org/example-repo/999", ts="2026-06-10T08:00:00Z",
        )

        def _fake_batch(pr_refs):
            return {ref: "closed" for ref in pr_refs}

        with patch(
            "forum.board_projects._batch_gh_reconcile", side_effect=_fake_batch
        ) as mock_batch:
            items = read_project_board(store)

        # The shared ref was passed to _batch_gh_reconcile exactly once
        # (deduped), driven by PR-500's active status.
        called_refs = mock_batch.call_args[0][0]
        assert (_REPO, "999") in called_refs
        assert len(called_refs) == 1

        pr500 = next(i for i in items if i["project"] == "PR-500")
        pr501 = next(i for i in items if i["project"] == "PR-501")
        assert pr500["gh_state"] == "closed"
        assert pr500["effective_status"] == "done"  # closed -> done
        assert pr501["gh_state"] == "closed"  # real resolved state, not 'skipped'
        assert pr501["effective_status"] == "done"  # already file-done anyway

    def test_call_count_excludes_file_done_refs(self, tmp_path):
        """N records seeded: M file-done with distinct refs, (N-M) active with
        distinct refs. gh is queried exactly (N-M) times, never N times.
        Regression guard against silently reverting to resolving everything.
        """
        from forum import board_projects as bp
        bp._clear_gh_cache()

        store = FileStore(tmp_path / "coord")
        alloc = SeqAllocator(recover=store.recover_max_seq)

        n_done = 5
        n_active = 3
        for i in range(n_done):
            _init(
                store, alloc, f"PR-DONE-{i}", title=f"done {i}", status="merged",
                turn="lei", participants=["lei"], turn_reason="merged",
                github=f"pr/example-org/example-repo/{9000 + i}", ts="2026-06-01T00:00:00Z",
            )
        for i in range(n_active):
            _init(
                store, alloc, f"PR-ACTIVE-{i}", title=f"active {i}",
                status="in-review", turn="ariadne", participants=["ariadne"],
                turn_reason="x", github=f"pr/example-org/example-repo/{8000 + i}", ts="2026-06-01T00:00:00Z",
            )

        call_log = []

        def _fake_gh_pr_state(pr_num, repo):
            call_log.append((repo, pr_num))
            return "open"

        try:
            with patch(
                "forum.board_projects._gh_pr_state", side_effect=_fake_gh_pr_state
            ):
                items = read_project_board(store)
        finally:
            bp._clear_gh_cache()

        assert len(call_log) == n_active
        assert len(items) == n_done + n_active
        done_items = [i for i in items if i["project"].startswith("PR-DONE-")]
        assert all(i["gh_state"] == "skipped" for i in done_items)


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
        """If _batch_gh_reconcile raises, board still returns (no 500).

        #2007: _batch_gh_reconcile is only invoked with the non-file-done
        refs (PR-103's file status is already 'merged', so its ref is never
        passed to the raising mock at all) — a raised exception there degrades
        those active refs to 'unknown'; PR-103 independently reports
        'skipped' since gh reconciliation was never attempted for it.
        """
        def _raise(*args, **kwargs):
            raise RuntimeError("gh not available")

        with patch("forum.board_projects._batch_gh_reconcile", side_effect=_raise):
            items = read_project_board(store)

        assert isinstance(items, list)
        for item in items:
            if item["github"]:
                assert item["gh_state"] in ("unknown", "skipped", "")


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
        app = create_app(db_path, audit, enable_board_viewer=True)
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
        app = create_app(db_path, audit, enable_board_viewer=True)
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
        app = create_app(db_path, audit, enable_board_viewer=True)
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
        eff, gh = _effective_status("in-progress", "pr/example-org/example-repo/42", {(_REPO, "42"): "merged"})
        assert eff == "done"
        assert gh == "merged"

    def test_effective_status_gh_closed_overrides_inreview(self):
        """_effective_status: gh closed overrides in-review file status."""
        eff, gh = _effective_status("in-review", "pr/example-org/example-repo/42", {(_REPO, "42"): "closed"})
        assert eff == "done"
        assert gh == "closed"

    def test_effective_status_file_merged_no_gh_ref(self):
        """_effective_status: file merged, no gh ref → done."""
        eff, gh = _effective_status("merged", "", {})
        assert eff == "done"
        assert gh == ""

    def test_effective_status_open_pr_passthrough(self):
        """_effective_status: open PR → file status passes through."""
        eff, gh = _effective_status("in-review", "pr/example-org/example-repo/42", {(_REPO, "42"): "open"})
        assert eff == "in-review"
        assert gh == "open"

    def test_effective_status_unknown_gh_passthrough(self):
        """_effective_status: unknown gh state → file status passes through."""
        eff, gh = _effective_status("in-progress", "pr/example-org/example-repo/42", {(_REPO, "42"): "unknown"})
        assert eff == "in-progress"
        assert gh == "unknown"

    def test_effective_status_repo_qualified_anchor(self):
        """_effective_status: a repo-qualified anchor (#1715) resolves against
        that repo's key in gh_states, not some other repo's."""
        eff, gh = _effective_status(
            "in-progress", "pr/engram-agents/engram-paper/22",
            {("engram-agents/engram-paper", "22"): "merged"},
        )
        assert eff == "done"
        assert gh == "merged"

    def test_effective_status_repo_qualified_does_not_collide_with_default(self):
        """A repo-qualified anchor must NOT be satisfied by another repo's
        entry for the same bare number -- this is the exact #1715 collision
        shape (engram-paper PR-22 must never read engram-alpha's PR #22)."""
        eff, gh = _effective_status(
            "in-progress", "pr/engram-agents/engram-paper/22",
            {(_REPO, "22"): "merged"},  # wrong repo's entry
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
            result = _gh_pr_state("999", _REPO)
        assert result == "unknown"

    def test_gh_pr_state_returns_unknown_on_nonzero_exit(self):
        """_gh_pr_state returns 'unknown' on non-zero gh exit code."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "not found"
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("999", _REPO)
        assert result == "unknown"

    def test_gh_pr_state_parses_merged(self):
        """_gh_pr_state returns 'merged' when gh reports MERGED."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "MERGED"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42", _REPO)
        assert result == "merged"

    def test_gh_pr_state_parses_closed(self):
        """_gh_pr_state returns 'closed' when gh reports CLOSED."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "CLOSED"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42", _REPO)
        assert result == "closed"

    def test_gh_pr_state_parses_open(self):
        """_gh_pr_state returns 'open' when gh reports OPEN."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"state": "OPEN"})
        with patch("forum.board_projects.subprocess.run", return_value=mock_result):
            result = _gh_pr_state("42", _REPO)
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
                result = _batch_gh_reconcile([(_REPO, "42"), (_REPO, "43")])
            assert result == {(_REPO, "42"): "open", (_REPO, "43"): "open"}
            assert sorted(call_log) == [(_REPO, "42"), (_REPO, "43")]
        finally:
            bp._clear_gh_cache()

    def test_batch_gh_reconcile_same_number_different_repo_does_not_collide(self):
        """#1715's core regression: two different repos' PR of the SAME
        number must resolve independently, never share a cache entry or a
        gh query result."""
        from forum import board_projects as bp
        bp._clear_gh_cache()

        def _fake_gh_pr_state(pr_num, repo):
            return "merged" if repo == _REPO else "open"

        try:
            with patch("forum.board_projects._gh_pr_state", side_effect=_fake_gh_pr_state):
                result = _batch_gh_reconcile([
                    (_REPO, "22"),
                    ("engram-agents/engram-paper", "22"),
                ])
            assert result[(_REPO, "22")] == "merged"
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
    ref = (_REPO, "1005")
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
    ref = (_REPO, "1005")
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


# ---------------------------------------------------------------------------
# #1918 class (#1919): NO default repo for a bare pr/<N> anchor on the board.
# ---------------------------------------------------------------------------

class TestNoDefaultRepoBareAnchor:
    """A bare pr/<N> anchor gets no gh lookup (whatever
    $ENGRAM_DEFAULT_GITHUB_REPO says) and keeps its stored status, flagged
    gh_state='unqualified' / gh_unknown=True. A qualified anchor still
    reconciles exactly as before."""

    QUALIFIED = "example-org/example-repo"

    def _store(self, tmp_path):
        s = FileStore(tmp_path / "coord")
        alloc = SeqAllocator(recover=s.recover_max_seq)
        _init(s, alloc, "PR-7001", title="bare", status="in-progress", turn="borges",
              participants=["borges"], turn_reason="x", github="pr/7001",
              ts="2026-06-20T10:00:00Z")
        _init(s, alloc, "PR-7002", title="qualified", status="in-progress", turn="borges",
              participants=["borges"], turn_reason="x",
              github=f"pr/{self.QUALIFIED}/7002", ts="2026-06-20T10:00:00Z")
        return s

    def test_c4_bare_anchor_no_gh_lookup_keeps_stored_status(self, tmp_path):
        from forum import board_projects as bp
        bp._clear_gh_cache()
        try:
            with patch("forum.board_projects._gh_pr_state", return_value="merged") as m:
                items = read_project_board(self._store(tmp_path))
        finally:
            bp._clear_gh_cache()
        looked_up = [(c.args[1], c.args[0]) for c in m.call_args_list]
        assert looked_up == [(self.QUALIFIED, "7002")], looked_up
        bare = next(i for i in items if i["project"] == "PR-7001")
        assert bare["effective_status"] == "in-progress"  # stored status, NOT 'done'
        assert bare["gh_state"] == "unqualified"
        assert bare["gh_unknown"] is True

    def test_c4_qualified_anchor_still_reconciles(self, tmp_path):
        from forum import board_projects as bp
        bp._clear_gh_cache()
        try:
            with patch("forum.board_projects._gh_pr_state", return_value="merged"):
                items = read_project_board(self._store(tmp_path))
        finally:
            bp._clear_gh_cache()
        q = next(i for i in items if i["project"] == "PR-7002")
        assert q["effective_status"] == "done"
        assert q["gh_state"] == "merged"
        assert q["gh_unknown"] is False

    def test_c4_effective_status_bare_ignores_any_gh_states_entry(self):
        # Even if some entry exists for the bare number under ANY repo key,
        # a bare anchor must not be reconciled from it.
        eff, gh = _effective_status(
            "in-progress", "pr/42", {(self.QUALIFIED, "42"): "merged"},
        )
        assert (eff, gh) == ("in-progress", "unqualified")
        eff, gh = _effective_status("merged", "pr/42", {})
        assert eff == "done"

    def test_c4_bare_anchor_already_terminal_stored_status_is_done_unqualified(self):
        # A bare anchor whose STORED status is already terminal (merged /
        # cancelled) is done regardless of gh; it is still flagged
        # 'unqualified' (pins the measured behaviour, #1919 round 2).
        for stored in ("merged", "cancelled"):
            assert _effective_status(stored, "pr/42", {}) == ("done", "unqualified"), stored
        # A gh_states entry under any repo key changes nothing.
        assert _effective_status(
            "cancelled", "pr/42", {(self.QUALIFIED, "42"): "open"},
        ) == ("done", "unqualified")
