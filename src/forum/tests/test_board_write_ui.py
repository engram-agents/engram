"""Tests for UCS Slice C — board write-button UI affordances.

Scope: DOM structure checks on the Jinja-rendered /board page.
JS behaviour (click → POST flow) is not testable in a pure Jinja render test;
those paths are covered by test_projects_api.py (route contract).

What we verify here:
  1. Identity bar elements are present in the HTML (actor-input, sentinel-input, actor-save).
  2. Card articles expose the data-* attributes JS needs (data-pid, data-turn,
     data-participants, data-terminal).
  3. All four action buttons render with correct data-action / data-project-id
     attributes — so the JS event-delegation contract is satisfied.
  4. The actions container has display:none in the static HTML, confirming
     buttons are hidden until JS sets an identity.
  5. Key JS strings are present in the page (localStorage key, API endpoints).
"""

import sqlite3

import pytest

from forum.coordination import FileStore, SeqAllocator, init as _init
from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixtures — #1608: board cards are now seeded into a real FileStore (the
# live coordination store), not read from a BATON_PROJECTS_DIR fixture dir
# (that glob is dead since the 2026-06-27 UCS cutover — see
# board_projects.py's module docstring). Same project ids/fields the retired
# fixtures/projects/*.md set used, so every DOM assertion below still holds.
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    """Flask app backed by a temp DB + a seeded coordination store (for board cards)."""
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()

    store = FileStore(tmp_path / "coord")
    allocator = SeqAllocator(recover=store.recover_max_seq)
    _init(store, allocator, "PR-100",
          title="Add project board feature", status="in-review", turn="ariadne",
          participants=["borges", "ariadne", "lei"],
          turn_reason="reviewer-fairy converged; colleague fresh-eye requested",
          github="pr/100", ts="2026-06-20T10:00:00Z")
    _init(store, allocator, "ISSUE-200",
          title="Forum search indexing latency", status="planning", turn="lei",
          participants=["borges", "ariadne", "lei"],
          turn_reason="design discussion needed", ts="2026-06-21T12:00:00Z")

    application = create_app(db_path, audit_path)
    application.config["TESTING"] = True
    application.config["COORD_STORE"] = store
    application.config["COORD_ALLOCATOR"] = allocator
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def board_html(client):
    """Decoded HTML body of GET /board — used by most tests."""
    return client.get("/board").get_data(as_text=True)


# ---------------------------------------------------------------------------
# 1. Identity bar elements
# ---------------------------------------------------------------------------

class TestIdentityBar:
    def test_actor_input_present(self, board_html):
        """Identity bar renders an actor name input."""
        assert 'id="actor-input"' in board_html

    def test_sentinel_input_present(self, board_html):
        """Identity bar renders a pool-sentinel input."""
        assert 'id="sentinel-input"' in board_html

    def test_save_button_present(self, board_html):
        """Identity bar renders the Set button."""
        assert 'id="actor-save"' in board_html

    def test_indicator_element_present(self, board_html):
        """Active-identity indicator span is in the page."""
        assert 'id="actor-indicator"' in board_html

    def test_error_element_present(self, board_html):
        """Inline error span is in the page."""
        assert 'id="actor-error"' in board_html


# ---------------------------------------------------------------------------
# 2. Card data attributes
# ---------------------------------------------------------------------------

class TestCardDataAttributes:
    def test_data_pid_present(self, board_html):
        """Cards expose data-pid with the project id (e.g. PR-100 from fixture)."""
        assert 'data-pid="PR-100"' in board_html

    def test_data_turn_present(self, board_html):
        """Cards expose data-turn for JS button-visibility logic."""
        # PR-100 fixture has turn=ariadne
        assert 'data-turn="ariadne"' in board_html

    def test_data_participants_present(self, board_html):
        """Cards expose data-participants (comma-separated) for flip-target select."""
        assert 'data-participants=' in board_html

    def test_data_terminal_present(self, board_html):
        """Cards expose data-terminal (0 or 1) for close/reopen dispatch."""
        assert 'data-terminal="0"' in board_html or 'data-terminal="1"' in board_html

    def test_data_pid_matches_fixture(self, board_html):
        """At least two fixture project IDs appear as data-pid values."""
        assert 'data-pid="PR-100"' in board_html
        assert 'data-pid="ISSUE-200"' in board_html


# ---------------------------------------------------------------------------
# 3. Action button data attributes
# ---------------------------------------------------------------------------

class TestActionButtonAttributes:
    def test_flip_button_data_action(self, board_html):
        """Flip button carries data-action='flip'."""
        assert 'data-action="flip"' in board_html

    def test_claim_button_data_action(self, board_html):
        """Claim button carries data-action='claim'."""
        assert 'data-action="claim"' in board_html

    def test_release_button_data_action(self, board_html):
        """Release button carries data-action='release'."""
        assert 'data-action="release"' in board_html

    def test_status_button_data_action(self, board_html):
        """Status button carries data-action='status'."""
        assert 'data-action="status"' in board_html

    def test_flip_button_has_project_id(self, board_html):
        """Flip button for PR-100 has data-project-id='PR-100'."""
        # Both data-action="flip" and data-project-id="PR-100" must appear in the page.
        assert 'data-action="flip"' in board_html
        assert 'data-project-id="PR-100"' in board_html

    def test_status_button_has_direction(self, board_html):
        """Status buttons carry data-direction (close or reopen)."""
        assert 'data-direction="close"' in board_html or 'data-direction="reopen"' in board_html

    def test_non_terminal_card_has_close_direction(self, board_html):
        """PR-100 (in-review, non-terminal) should have a close status button."""
        # PR-100 is in-review — not terminal → renders direction=close
        assert 'data-direction="close"' in board_html


# ---------------------------------------------------------------------------
# 4. Buttons hidden by default (no identity set)
# ---------------------------------------------------------------------------

class TestButtonsHiddenByDefault:
    def test_actions_container_display_none(self, board_html):
        """pcard__actions container has display:none in raw HTML (JS shows it)."""
        assert 'pcard__actions' in board_html
        # The container should carry inline style="display:none;" before JS runs
        assert 'display:none' in board_html

    def test_page_renders_200(self, client):
        """Board page still returns 200 with no identity set (read-only unaffected)."""
        resp = client.get("/board")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. JavaScript presence checks
# ---------------------------------------------------------------------------

class TestJavaScriptPresent:
    def test_localstorage_key_in_page(self, board_html):
        """The JS localStorage key 'board_actor' is present in the page source."""
        assert 'board_actor' in board_html

    def test_board_sentinel_key_in_page(self, board_html):
        """The sentinel localStorage key 'board_sentinel' is present."""
        assert 'board_sentinel' in board_html

    def test_api_write_endpoint_referenced(self, board_html):
        """The JS references the write API path prefix."""
        assert '/api/projects/' in board_html

    def test_reconcile_via_location_reload(self, board_html):
        """After a successful write the JS reconciles by reloading the page
        (the /board route re-reads projects/*.md on every GET), not by a
        separate board fetch."""
        assert 'location.reload()' in board_html

    def test_confirm_call_in_js(self, board_html):
        """Each write action is guarded by a confirm() call (Borges security condition)."""
        assert 'confirm(' in board_html

    def test_no_blank_reason_invitation(self, board_html):
        """The /flip and /status routes reject an empty reason (400); the JS must
        not invite a blank one. Regression guard for the round-1 reviewer blockers
        (the old 'leave blank for none' hint produced a guaranteed 400)."""
        assert 'leave blank for none' not in board_html

    def test_status_guards_active_set_boundary(self, board_html):
        """handleStatus validates the typed status against the server's active set
        so a mistyped value can't silently flip the close↔reopen dispatch."""
        assert 'ACTIVE_STATUSES' in board_html
        assert "'planning'" in board_html and "'in-review'" in board_html

    def test_per_button_confirm_coverage(self, board_html):
        """PER-BUTTON write-confirm coverage. The bare `assert 'confirm(' in
        board_html` check above is axis-blind on completeness (Borges's colleague
        catch): it stays green if a future change drops the confirm() from ONE
        button, because some OTHER button's confirm keeps the string present.
        Assert each state-changing action's DISTINCT confirm message instead, so
        removing any single button's guard fails this test."""
        per_button_guards = [
            "confirm('Act as ' + actor + ' — flip '",
            "confirm('Act as ' + actor + ' — claim '",
            "confirm('Act as ' + actor + ' — release '",
            "confirm('Act as ' + actor + ' — set '",
        ]
        for guard in per_button_guards:
            assert guard in board_html, f"missing per-button write-confirm guard: {guard}"

    def test_script_tag_present(self, board_html):
        """A <script> block is present in the board page."""
        assert '<script>' in board_html
