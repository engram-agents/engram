"""Tests for the /board viewer: a READ-ONLY operator viewer (#1459).

Scope: DOM structure checks on the Jinja-rendered /board page.

Browser JS cannot hold a bearer token safely, so the page carries NO write controls: no identity
bar, no flip/claim/release/status buttons, no form, and no script that POSTs /api/projects/*.
Writes go through the token-bearing CLI (baton). The write routes themselves are covered by
test_projects_api.py (route contract) and the auth guard by test_forum_auth_1459*.py.

What we verify here:
  1. The page renders the baton list, with the data-* attributes on each card.
  2. The page contains no write control and no write target.
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

    application = create_app(db_path, audit_path, enable_board_viewer=True)
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
# 3. Read-only: no write control, no write target (#1459)
# ---------------------------------------------------------------------------

WRITE_MARKERS = [
    "/api/projects",        # the POST target of every former write button
    "<button",              # flip / claim / release / close / reopen / Set
    'method="post"',        # no form posts from this page (the nav search form is a GET)
    "data-action=",         # the old event-delegation contract
    'id="actor-',           # the identity bar
    "localStorage",         # the actor / sentinel persistence
    "fetch(",               # any browser-side request
    "XMLHttpRequest",
    "pcard__actions",       # the action container
]


# Removed-test: src/forum/tests/test_board_write_ui.py::TestActionButtonAttributes::test_claim_button_data_action — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestActionButtonAttributes::test_flip_button_data_action — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestActionButtonAttributes::test_flip_button_has_project_id — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestActionButtonAttributes::test_non_terminal_card_has_close_direction — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestActionButtonAttributes::test_release_button_data_action — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestActionButtonAttributes::test_status_button_data_action — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestActionButtonAttributes::test_status_button_has_direction — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestButtonsHiddenByDefault::test_actions_container_display_none — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestButtonsHiddenByDefault::test_page_renders_200 — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestIdentityBar::test_actor_input_present — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestIdentityBar::test_error_element_present — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestIdentityBar::test_indicator_element_present — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestIdentityBar::test_save_button_present — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestIdentityBar::test_sentinel_input_present — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_api_write_endpoint_referenced — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_board_sentinel_key_in_page — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_confirm_call_in_js — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_localstorage_key_in_page — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_no_blank_reason_invitation — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_per_button_confirm_coverage — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_reconcile_via_location_reload — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_script_tag_present — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
# Removed-test: src/forum/tests/test_board_write_ui.py::TestJavaScriptPresent::test_status_guards_active_set_boundary — the /board viewer is read-only now (#1459): its write controls were removed, replaced by the TestBoardIsReadOnly cells below
class TestBoardIsReadOnly:
    @pytest.mark.parametrize("marker", WRITE_MARKERS)
    def test_page_has_no_write_control_or_target(self, board_html, marker):
        assert marker not in board_html, f"/board still carries {marker!r}"

    def test_page_still_renders_the_baton_list(self, client, board_html):
        assert client.get("/board").status_code == 200
        assert 'data-pid="PR-100"' in board_html and 'data-pid="ISSUE-200"' in board_html
        assert "Add project board feature" in board_html

    def test_page_says_it_is_read_only(self, board_html):
        assert "Read-only operator viewer" in board_html
        assert "token-bearing CLI" in board_html
