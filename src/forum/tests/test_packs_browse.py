"""Tests for #651 slice (b) — pack browse pages + pack: references in posts.

Coverage:
  HTTP:
    - GET /packs returns 200 and lists a published fixture pack.
    - GET /packs/<id> returns 200, renders README content, XSS sanitized.
    - GET /packs/<unknown-id> returns 404.

  Render (forum/render.py PACK_ID_RE + _apply_pack_refs):
    - pack:<valid-slug> in post body becomes a link to /packs/<id>.
    - Invalid / malformed tokens (path traversal, uppercase, spaces) do NOT
      become links.
    - pack: token inside an inline code block does NOT render as a link
      (mirrors citation-chip behavior — SKIP_TAGS includes <code>).

Fixture strategy: reuses _make_pack_tarball + _upload_pack from test_packs.py
(same module, same repo root path gymnastics), avoiding duplication.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from forum.db import init_db
from forum.render import render_post_body
from forum.server import create_app

# Import fixture helpers from test_packs — single source of truth for pack
# tarball construction; no duplication of _MINIMAL_SQL or _make_pack_tarball.
from forum.tests.test_packs import _make_pack_tarball, _upload_pack


# ---------------------------------------------------------------------------
# README content for detail-page tests.
# Includes a <script> tag that MUST NOT survive the sanitization pipeline.
# ---------------------------------------------------------------------------
_README_WITH_SCRIPT = """\
# Test Pack

This pack contains test knowledge nodes.

<script>alert('xss')</script>

## Notes

A normal paragraph without any dangerous content.
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    packs_dir = str(tmp_path / "packs")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    application = create_app(db_path, audit_path, packs_dir=packs_dir)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def published_pack_id(client):
    """Publish a minimal pack and return its id."""
    tarball = _make_pack_tarball()
    resp = _upload_pack(client, tarball)
    assert resp.status_code == 201, resp.data.decode()
    return json.loads(resp.data)["pack_id"]


@pytest.fixture
def published_pack_id_with_readme(client):
    """Publish a pack whose README contains a <script> tag."""
    # include_readme=False so the tarball has exactly one README member; avoids
    # relying on undocumented last-wins getmember behavior for duplicate names.
    tarball = _make_pack_tarball(include_readme=False, extra_files={"README.md": _README_WITH_SCRIPT})
    resp = _upload_pack(client, tarball)
    assert resp.status_code == 201, resp.data.decode()
    return json.loads(resp.data)["pack_id"]


# ---------------------------------------------------------------------------
# HTTP: /packs index
# ---------------------------------------------------------------------------

class TestPacksIndex:
    def test_index_200(self, client):
        resp = client.get("/packs")
        assert resp.status_code == 200

    def test_index_empty_shows_empty_state(self, client):
        resp = client.get("/packs")
        assert resp.status_code == 200
        # The empty-state message appears when registry has no packs.
        assert b"no packs published yet" in resp.data

    def test_index_shows_published_pack(self, client, published_pack_id):
        resp = client.get("/packs")
        assert resp.status_code == 200
        assert published_pack_id.encode() in resp.data

    def test_index_links_to_detail_page(self, client, published_pack_id):
        resp = client.get("/packs")
        assert resp.status_code == 200
        expected_href = f"/packs/{published_pack_id}".encode()
        assert expected_href in resp.data

    def test_index_shows_author(self, client, published_pack_id):
        """The author (from the published pack) appears on the index page."""
        resp = client.get("/packs")
        assert resp.status_code == 200
        # The default agent used by _upload_pack is "agent-a".
        assert b"agent-a" in resp.data


# ---------------------------------------------------------------------------
# HTTP: /packs/<id> detail
# ---------------------------------------------------------------------------

class TestPackDetail:
    def test_detail_200(self, client, published_pack_id):
        resp = client.get(f"/packs/{published_pack_id}")
        assert resp.status_code == 200

    def test_detail_shows_pack_id(self, client, published_pack_id):
        resp = client.get(f"/packs/{published_pack_id}")
        assert published_pack_id.encode() in resp.data

    def test_detail_shows_author(self, client, published_pack_id):
        resp = client.get(f"/packs/{published_pack_id}")
        assert b"agent-a" in resp.data

    def test_detail_contains_download_link(self, client, published_pack_id):
        resp = client.get(f"/packs/{published_pack_id}")
        assert resp.status_code == 200
        expected = f"/api/packs/{published_pack_id}/download".encode()
        assert expected in resp.data

    def test_detail_readme_content_rendered(self, client, published_pack_id_with_readme):
        """The README section is rendered — normal text appears in the output."""
        resp = client.get(f"/packs/{published_pack_id_with_readme}")
        assert resp.status_code == 200
        # "A normal paragraph" from _README_WITH_SCRIPT should appear.
        assert b"A normal paragraph" in resp.data

    def test_detail_readme_script_sanitized(self, client, published_pack_id_with_readme):
        """<script> tag in README does NOT survive as a live script element.

        markdown-it-py with html=False escapes raw HTML to text, so the
        <script> tag becomes &lt;script&gt;...&lt;/script&gt; (safe escaped
        text) rather than a real executable tag.  The security property is:
        no live <script> element injected from the README content.

        We verify via BeautifulSoup DOM parse, not raw HTML string search
        (which would fire on escaped text that is safe).
        """
        from bs4 import BeautifulSoup
        resp = client.get(f"/packs/{published_pack_id_with_readme}")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        # Count script tags: only the page's own keyboard-shortcut <script>
        # at the bottom should be present — NOT any script injected from
        # the README content.
        script_tags = soup.find_all("script")
        for tag in script_tags:
            # No script tag should contain the README's alert payload.
            assert "alert('xss')" not in (tag.string or ""), (
                f"README XSS payload found in live <script> tag: {tag!r}"
            )

    def test_detail_unknown_id_404(self, client):
        resp = client.get("/packs/no-such-pack-v1")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Render: pack: references in post bodies
# ---------------------------------------------------------------------------

class TestPackRefRender:
    def test_valid_slug_becomes_link(self):
        """pack:<valid-slug> in body text → <a href="/packs/<id>">."""
        out = render_post_body("See pack:agent-a-epistemics-v1 for details.")
        assert 'href="/packs/agent-a-epistemics-v1"' in out

    def test_link_text_is_pack_id(self):
        """The link text should be 'pack:<id>'."""
        out = render_post_body("pack:agent-b-history-v2 is relevant here.")
        assert "pack:agent-b-history-v2" in out
        assert 'href="/packs/agent-b-history-v2"' in out

    def test_pack_ref_has_pack_ref_class(self):
        """The emitted <a> carries class='pack-ref'."""
        out = render_post_body("pack:agent-a-test-v1")
        assert 'class="pack-ref"' in out

    def test_path_traversal_not_linked(self):
        """pack:../etc/passwd must NOT become a link (slash is not in slug charset)."""
        out = render_post_body("pack:../etc/passwd")
        assert 'href="/packs/' not in out

    def test_uppercase_not_linked(self):
        """pack:UPPER-CASE-V1 must NOT become a link (uppercase outside slug charset)."""
        out = render_post_body("pack:UPPER-CASE-V1")
        assert 'href="/packs/' not in out

    def test_space_in_id_not_linked(self):
        """pack:id with spaces must NOT become a link."""
        out = render_post_body("pack:some thing v1")
        assert 'href="/packs/' not in out

    def test_no_version_suffix_not_linked(self):
        """A token without -v<N> suffix does not match the slug pattern."""
        out = render_post_body("pack:no-version-here")
        assert 'href="/packs/' not in out

    def test_pack_ref_in_code_not_linked(self):
        """pack:<id> inside inline code does NOT become a link."""
        out = render_post_body("`pack:agent-a-test-v1`")
        # Should be inside a <code> element without a link.
        assert 'href="/packs/agent-a-test-v1"' not in out
        # The text itself may still appear verbatim in <code>.
        assert "pack:agent-a-test-v1" in out

    def test_multiple_pack_refs(self):
        """Multiple pack: references in one body are all linked."""
        out = render_post_body(
            "Compare pack:agent-a-alpha-v1 with pack:agent-b-beta-v2."
        )
        assert 'href="/packs/agent-a-alpha-v1"' in out
        assert 'href="/packs/agent-b-beta-v2"' in out

    def test_pack_ref_coexists_with_citation_chip(self):
        """pack: reference and ENGRAM citation chip both render in the same body."""
        out = render_post_body("See OB 0001 and also pack:agent-a-epistemics-v1.")
        assert '<span class="citation citation--ob">OB 0001</span>' in out
        assert 'href="/packs/agent-a-epistemics-v1"' in out
