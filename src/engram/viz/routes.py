"""viz.routes — the page route table as data (engram-alpha#2555 PR A).

Per the layout design on #2555: "routes.py: the route table as DATA. One
line registers a page." This is deliberately
narrow for PR A -- it covers only the four whole-page GET routes
(``/``, ``/health``, ``/stats``, ``/config``), which the original
``Handler.do_GET`` served via four hardcoded ``if path == "...": self._html(...);
return`` blocks (see viz_server.py, original lines ~5260-5271). Replacing
those four blocks with one dict lookup is the whole of this module's job.

The ``/api/*`` routes are NOT represented here -- each has bespoke
agent-resolution / status-code / error-shape logic (see the original
Handler.do_GET's second half) that doesn't reduce to a flat "path ->
constant" table without behavior risk, and PR A's mandate is pure
relocation, not a redesign of that dispatch. They stay as an elif chain in
server.py, unchanged in shape from the original. A plain dict is
deliberately used (not a dataclass / named-route registry) so this stays
the simplest structure that satisfies PR A's scope -- #2555's design notes
this table as the natural place PR B's ``url_for()`` / active-tab
amendment can later hook into, without PR A pre-building any of that.

PR B UPDATE: the values below are no longer FULLY baked pages -- each
still contains the literal ``<!--NAV_PLACEHOLDER-->`` sentinel where the
nav div goes (see ``shell.py``'s module docstring for why: the nav is now
rendered at REQUEST time, not import time, so it can carry the actual
matched route + the request's own ``?agent=`` value). ``server.py``'s
``Handler.do_GET`` is responsible for splicing in the real nav fragment
(via ``shell._render_nav`` + ``urls.route_name_for_path``) before serving
any of these strings -- this dict alone is not a servable page.
"""

from __future__ import annotations

from . import shell

# path -> page shell (nav div NOT yet spliced in -- still carries the
# literal shell._NAV_PLACEHOLDER sentinel; see shell.py's module docstring).
# Active-tab resolution for the nav splice goes through
# urls.route_name_for_path(path), not a second table here. One line per
# page, per #2555's "one line registers a page."
PAGE_ROUTES: dict[str, str] = {
    "/": shell.HTML,
    "/health": shell.HEALTH_HTML,
    "/stats": shell.STATS_HTML,
    "/config": shell.CONFIG_HTML,
}
