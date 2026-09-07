"""Tests for viz.urls.url_for()/route_name_for_path() and the request-time
nav splice + /static/ asset serving they drive (engram-alpha#2555 PR B
amendment-1, requirements 2/3/5).

Two layers, per the PR spec's "adapt PR A's methodology -- live server
diffs, not just unit tests":

1. Unit-level: url_for()/route_name_for_path() in isolation (no HTTP).
2. Live-server: spin up a real viz.server.Handler HTTPServer and fetch,
   the same pattern test_viz_server_select_control.py's
   TestSelectControlHTTPPut uses -- covers:
   - Gate 1b (nav parity, strong form) -- covered separately in
     test_viz_server_select_control.py's TestAllTabsSameNavBar +
     test_exactly_one_nav_css_rule_opener_in_viz; this file's
     TestLiveServerNavRendering re-covers it at the true HTTP level (the
     4 routes actually served, not the shell constants directly).
   - Gate 1c (link integrity): crawl every internal href/src in every
     page's live response; confirm each resolves to a known route.
   - ?agent= carry-through: set -> every internal nav link carries it;
     unset -> no stray ?agent= appended.
   - Static serving correctness: content match, Content-Type, cache
     header, path-traversal rejection, 404 on missing file.
"""
from __future__ import annotations

import re
import sys
import threading
import unittest
import unittest.mock
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import agents as viz_agents
from viz import server as viz_server
from viz import shell as viz_shell
from viz import urls as viz_urls

_STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "engram" / "viz" / "static"

_KNOWN_PAGE_PATHS = {"/", "/health", "/stats", "/config"}


# ---------------------------------------------------------------------------
# 1. Unit-level: url_for() / route_name_for_path()
# ---------------------------------------------------------------------------

class TestUrlForRoutes(unittest.TestCase):
    def test_all_four_page_routes_resolve(self):
        self.assertEqual(viz_urls.url_for("graph"), "/")
        self.assertEqual(viz_urls.url_for("health"), "/health")
        self.assertEqual(viz_urls.url_for("stats"), "/stats")
        self.assertEqual(viz_urls.url_for("config"), "/config")

    def test_unknown_route_name_raises(self):
        with self.assertRaises(ValueError):
            viz_urls.url_for("nonexistent")

    def test_static_route_requires_filename(self):
        with self.assertRaises(ValueError):
            viz_urls.url_for("static")

    def test_static_route_builds_path(self):
        self.assertEqual(viz_urls.url_for("static", filename="base.css"),
                         "/static/base.css")

    def test_agent_param_carried_through_when_given(self):
        self.assertEqual(viz_urls.url_for("health", agent="alice"),
                         "/health?agent=alice")

    def test_agent_param_omitted_when_none(self):
        self.assertEqual(viz_urls.url_for("health", agent=None), "/health")

    def test_agent_param_omitted_when_empty_string(self):
        self.assertEqual(viz_urls.url_for("health", agent=""), "/health")

    def test_graph_route_with_agent(self):
        """The graph route's bare path is "/" -- confirm the query string
        attaches correctly rather than producing "/?agent=x" malformed."""
        self.assertEqual(viz_urls.url_for("graph", agent="bob"), "/?agent=bob")


class TestRouteNameForPath(unittest.TestCase):
    def test_all_four_paths_resolve_to_names(self):
        self.assertEqual(viz_urls.route_name_for_path("/"), "graph")
        self.assertEqual(viz_urls.route_name_for_path("/health"), "health")
        self.assertEqual(viz_urls.route_name_for_path("/stats"), "stats")
        self.assertEqual(viz_urls.route_name_for_path("/config"), "config")

    def test_unknown_path_returns_none(self):
        self.assertIsNone(viz_urls.route_name_for_path("/nonexistent"))


class TestRenderNavAgentCarryThrough(unittest.TestCase):
    """shell._render_nav is the one caller of url_for() that matters for
    the page-serving behavior -- confirm the carry-through end-to-end at
    the fragment level (server-level confirmation is in
    TestLiveServerAgentCarryThrough below)."""

    def test_agent_carried_through_every_tab_link(self):
        nav = viz_shell._render_nav("health", agent="alice")
        for path in ("/", "/health", "/stats", "/config"):
            self.assertIn(f'href="{path}?agent=alice"', nav,
                          f"nav missing ?agent=alice carry-through for {path}")

    def test_no_stray_agent_param_when_omitted(self):
        nav = viz_shell._render_nav("health", agent=None)
        self.assertNotIn("agent=", nav,
                         "nav has a stray ?agent= with no agent given")


# ---------------------------------------------------------------------------
# 2. Live-server: real HTTP requests against viz.server.Handler
# ---------------------------------------------------------------------------

def _crawl_internal_refs(html: str) -> set[str]:
    """Return every href="..."/src="..." value in *html* (raw, with any
    query string intact)."""
    return set(re.findall(r'(?:href|src)="([^"]*)"', html))


class _LiveServerBase(unittest.TestCase):
    """Spin up a real viz.server.Handler HTTPServer -- same pattern as
    test_viz_server_select_control.py's TestSelectControlHTTPPut."""

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path: str) -> tuple[int, bytes, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urlopen(url, timeout=5) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})


class TestServerAssertsOnRouteTableDrift(_LiveServerBase):
    """THE FALSIFIER for server.py's route_name_for_path assert
    (reviewer-fairy finding on PR#2564): without it, a drift between
    routes.PAGE_ROUTES and urls._ROUTE_PATHS flows a None active_tab
    silently into shell._render_nav, rendering a nav bar with no tab
    bolded rather than the loud failure route_name_for_path's own
    docstring always claimed. Drives the REAL do_GET code path (a live
    request against a real HTTPServer, not a re-typed copy of the assert)
    with urls._ROUTE_PATHS patched empty, simulating exactly the drift
    the assert exists to catch."""

    def test_drifted_route_table_fails_loudly_not_silently(self):
        # Remap "health" to a DIFFERENT path than routes.PAGE_ROUTES actually
        # serves it at, rather than emptying _ROUTE_PATHS outright -- an
        # empty table would ALSO break url_for()'s own forward lookups for
        # every tab name (_render_nav calls url_for("graph"/"health"/...)
        # for each tab link), which would raise regardless of whether the
        # assert under test exists, and prove nothing about it specifically.
        # This isolates JUST route_name_for_path's reverse lookup failing
        # for "/health" while url_for's forward lookups keep succeeding --
        # confirmed by first checking route_name_for_path alone.
        drifted = dict(viz_urls._ROUTE_PATHS)
        drifted["health"] = "/health-drifted-for-this-test-only"
        with unittest.mock.patch.object(viz_urls, "_ROUTE_PATHS", drifted):
            self.assertIsNone(
                viz_urls.route_name_for_path("/health"),
                "test setup didn't actually create drift -- route_name_for_path "
                "still resolved '/health' with the remapped table",
            )
            # An AssertionError raised inside do_GET (no try/except wraps
            # it) means the handler never sends a response at all -- the
            # client sees the connection close mid-request. What matters
            # for this falsifier is only that it is NOT a clean 200 with
            # a silently-broken (no-tab-bolded) nav, which is what would
            # happen without the assert -- pin "raises something", not the
            # exact transport-level exception shape, an http.server detail.
            with self.assertRaises(Exception):
                self._get("/health")


class TestLiveServerNavRendering(_LiveServerBase):
    """Gate 1b (live-HTTP form): the nav div served by each of the 4 real
    routes is identical modulo the active-tab bold marker."""

    def _nav_div(self, html: bytes) -> str:
        text = html.decode()
        m = re.search(r'<div class="nav">.*?</div>', text, re.DOTALL)
        self.assertIsNotNone(m, "no <div class=\"nav\"> found in response")
        return m.group(0)

    def test_nav_div_identical_across_routes_modulo_bold_marker(self):
        navs = {}
        for path in sorted(_KNOWN_PAGE_PATHS):
            status, body, _ = self._get(path)
            self.assertEqual(status, 200, f"{path} did not return 200")
            navs[path] = self._nav_div(body).replace(' style="font-weight:600;"', "")
        values = list(navs.values())
        self.assertTrue(
            all(v == values[0] for v in values),
            f"nav div differs across routes (modulo bold marker): {navs!r}",
        )


class TestLiveServerAgentCarryThrough(_LiveServerBase):
    """?agent= carry-through, at the real HTTP level."""

    def test_agent_param_set_carries_to_every_nav_link(self):
        for path in sorted(_KNOWN_PAGE_PATHS):
            status, body, _ = self._get(f"{path}?agent=testagent")
            self.assertEqual(status, 200)
            text = body.decode()
            for tab_path in ("/", "/health", "/stats", "/config"):
                self.assertIn(
                    f'href="{tab_path}?agent=testagent"', text,
                    f"{path} response missing ?agent=testagent carry-through "
                    f"for nav link {tab_path}",
                )

    def test_agent_param_unset_no_stray_param(self):
        """No ?agent= on the request -> no ?agent= on any nav link, in THIS
        fixture's degenerate state specifically: _LiveServerBase never calls
        viz.server.main(), so viz_agents.AGENTS/DEFAULT_AGENT are still their
        module-level empty defaults ({}/""), and agents._resolve_agent()
        resolves an empty string for "no agent context at all" -- which
        url_for() correctly drops, same as None. This does NOT exercise the
        realistic case (a real DEFAULT_AGENT configured, as every actual
        deployment has) -- see TestLiveServerAgentCarryThroughWithRealDefault
        below for that, which is the one that would have caught the original
        bug this fix corrects (carrying through only the literal query param,
        never the resolved default -- silently reproducing the "switching
        pages changes which graph you're viewing" bug this feature exists to
        remove, the moment DEFAULT_AGENT changes between two page loads)."""
        for path in sorted(_KNOWN_PAGE_PATHS):
            status, body, _ = self._get(path)
            self.assertEqual(status, 200)
            text = body.decode()
            for tab_path in ("/", "/health", "/stats", "/config"):
                self.assertIn(f'href="{tab_path}"', text,
                              f"{path} response missing bare nav link {tab_path}")
                self.assertNotIn(f'href="{tab_path}?agent=', text,
                                 f"{path} response has a stray ?agent= on "
                                 f"nav link {tab_path} with no ?agent= set "
                                 f"on the request")


class TestLiveServerAgentCarryThroughWithRealDefault(_LiveServerBase):
    """The realistic case TestLiveServerAgentCarryThrough's fixture can't
    exercise: a real DEFAULT_AGENT configured (as every actual deployment
    has, single- or multi-agent), and a request with NO ?agent= param.
    Nav links must carry the RESOLVED default through, not omit it -- the
    falsifier for the "carry through only the literal query param" bug:
    revert server.py's do_GET page-route branch to derive agent_param from
    the raw query string instead of agents._resolve_agent(), and this test
    fails (links come back bare instead of carrying ?agent=defaultagent)."""

    def setUp(self):
        super().setUp()
        self._saved_agents = dict(viz_agents.AGENTS)
        self._saved_default = viz_agents.DEFAULT_AGENT
        viz_agents.AGENTS = {
            "defaultagent": {"name": "defaultagent", "label": "Default",
                              "db": "/nonexistent/does-not-matter.db"},
        }
        viz_agents.DEFAULT_AGENT = "defaultagent"

    def tearDown(self):
        viz_agents.AGENTS = self._saved_agents
        viz_agents.DEFAULT_AGENT = self._saved_default
        super().tearDown()

    def test_unset_agent_param_carries_resolved_default(self):
        for path in sorted(_KNOWN_PAGE_PATHS):
            status, body, _ = self._get(path)
            self.assertEqual(status, 200)
            text = body.decode()
            for tab_path in ("/", "/health", "/stats", "/config"):
                self.assertIn(
                    f'href="{tab_path}?agent=defaultagent"', text,
                    f"{path} response (no ?agent= on the request) does not "
                    f"carry the resolved DEFAULT_AGENT through to nav link "
                    f"{tab_path} -- switching tabs would silently drop the "
                    f"effective agent the moment DEFAULT_AGENT changes",
                )


class TestLiveServerLinkIntegrity(_LiveServerBase):
    """Gate 1c: crawl every internal href/src in every page's response and
    confirm each resolves to a known route (a page route, one of the 4
    static assets actually served, or an external absolute URL, which is
    out of scope for this check).

    ``_EXTRA_KNOWN_TARGETS`` (engram-alpha#2727): dev-tier-only routes that
    are not one of the 4 always-shipped ``_KNOWN_PAGE_PATHS`` (those drive
    OTHER tests in this file that fetch each path expecting 200 under a
    fixture with no registered agents -- adding a route here that 404s
    under that fixture, or that has no matching nav tab, would break those,
    not just this one). ``/health/workbench`` is a real, valid link target
    the detail view's own header conditionally emits (see server.py's
    ``_workbench_available()`` gate) -- deliberately allow-listed here,
    separately, rather than folded into ``_KNOWN_PAGE_PATHS``."""

    _EXTRA_KNOWN_TARGETS = {"/health/workbench"}

    def test_all_internal_refs_resolve(self):
        known_static = {f"/static/{p.name}" for p in _STATIC_DIR.iterdir() if p.is_file()}
        for path in sorted(_KNOWN_PAGE_PATHS):
            status, body, _ = self._get(path)
            self.assertEqual(status, 200)
            refs = _crawl_internal_refs(body.decode())
            for ref in refs:
                if ref.startswith("http://") or ref.startswith("https://"):
                    continue  # external -- out of scope for internal link integrity
                bare = ref.split("?", 1)[0]
                self.assertTrue(
                    bare in _KNOWN_PAGE_PATHS
                    or bare in known_static
                    or bare in self._EXTRA_KNOWN_TARGETS,
                    f"{path} response has an internal ref {ref!r} that "
                    f"resolves to neither a known page route nor a served "
                    f"static asset",
                )


class TestLiveServerStaticServing(_LiveServerBase):
    """Static serving correctness: content match, Content-Type, cache
    header, path-traversal rejection, 404 on missing file."""

    _CONTENT_TYPES = {".css": "text/css", ".js": "application/javascript"}

    def test_every_static_asset_content_type_and_cache_header(self):
        for f in sorted(_STATIC_DIR.iterdir()):
            if not f.is_file():
                continue
            status, body, headers = self._get(f"/static/{f.name}")
            self.assertEqual(status, 200, f"/static/{f.name} did not return 200")
            self.assertEqual(body, f.read_bytes(),
                             f"/static/{f.name} body does not match file content")
            self.assertEqual(headers.get("Content-Type"),
                             self._CONTENT_TYPES[f.suffix],
                             f"/static/{f.name} wrong Content-Type")
            self.assertEqual(headers.get("Cache-Control"),
                             "public, max-age=3600",
                             f"/static/{f.name} wrong/missing Cache-Control")

    def test_path_traversal_rejected(self):
        status, _, _ = self._get("/static/../server.py")
        self.assertEqual(status, 404, "path-traversal request was not rejected")

    def test_nonexistent_file_404s(self):
        status, _, _ = self._get("/static/nonexistent.css")
        self.assertEqual(status, 404)

    def test_path_traversal_does_not_leak_server_py(self):
        _, body, _ = self._get("/static/../server.py")
        self.assertNotIn(b"class Handler", body,
                         "path-traversal request leaked server.py content")


if __name__ == "__main__":
    unittest.main()
