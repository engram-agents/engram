"""viz.shell — the nav-rendering machinery, and the four assembled
page-HTML SHELLS (engram-alpha#2555 PR A pure relocation, then PR B
amendment-1's nav-CSS-consolidation / static-asset / url_for rework).

No HTTP handling here -- this module's job is producing the four
ready-to-serve HTML shells (once, at import time) plus the nav-fragment
renderer server.py calls per request.

ARCHITECTURE NOTE, PR B (load-bearing for understanding why nav rendering
is split across two different TIMES): PR A baked all four pages --
including the nav ``<div class="nav">...</div>`` itself -- once, at module
import time, from four already-fully-substituted HTML strings. PR B's
``url_for()`` requirement (#2555 amendment-1, requirement 3) makes the nav
genuinely request-dependent in two ways a module-import-time bake cannot
see: (a) the ACTIVE tab is a property of which route was requested, and
(b) each tab link must carry the EFFECTIVE agent through, not some
import-time default. "Effective" and not "the raw ``?agent=`` param" is
deliberate: the agent is resolved the same way every agent-scoped API
endpoint already resolves it (explicit ``?agent=`` else the live
``DEFAULT_AGENT``, itself refreshed on a TTL by ``agents._refresh_agents_
if_stale()``) -- carrying through only a literal query param and leaving
it absent otherwise would silently reproduce the very bug this feature
exists to remove, the moment ``DEFAULT_AGENT`` changes between two page
loads in the same browsing session. So PR B splits nav rendering into
two phases:

* Import time (this module, unchanged in spirit from PR A): read each page
  template once into ``HTML``/``HEALTH_HTML``/``STATS_HTML``/``CONFIG_HTML``.
  Everything on the page EXCEPT the nav div itself is still pre-baked here,
  preserving PR A's "read once, not per-request" performance property for
  the bulk of each page. Each of these four strings still contains the
  literal sentinel ``<!--NAV_PLACEHOLDER-->`` where the nav div used to be
  baked in -- see ``_NAV_PLACEHOLDER`` below.
* Request time (``server.py``'s ``Handler.do_GET``): once the matched route
  and the request's own ``?agent=`` are both known, call ``_render_nav``
  (below) and splice the result into the page shell via a single
  ``str.replace(_NAV_PLACEHOLDER, ...)``.

Nav CSS/JS are no longer placeholder-substituted at all (PR A's
``_NAV_SCRIPT_PLACEHOLDER``/``_NAV_CSS_PLACEHOLDER`` machinery is gone):
they're real static assets served from ``/static/`` (see ``viz/static/``
and ``server.py``'s static route) and referenced from each template via a
plain ``<link rel="stylesheet" href="/static/base.css">`` /
``<script src="/static/nav.js"></script>`` tag baked directly into the
template file itself -- static, page-independent, no substitution needed.
``_NAV_SCRIPT`` is kept as an importable constant purely because several
regression tests assert against its content directly (the single-agent-mode
guard, the wrapper-display-value regression guard -- see
``test_viz_server_select_control.py``); it is NOT used to bake any page.
"""

from __future__ import annotations

from pathlib import Path

from . import urls

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _read_template(name: str) -> str:
    """Read one file from templates/, once, at import time.

    ``encoding="utf-8", newline=""`` -- reads the file exactly as stored
    (no universal-newline translation), so byte-identity with the original
    computed string constants is preserved regardless of what line-ending
    convention the checkout / OS uses. Uses ``open()`` rather than
    ``Path.read_text()`` because the latter's ``newline`` parameter is
    Python 3.13+ only (this project supports earlier 3.x).
    """
    with open(_TEMPLATES_DIR / name, encoding="utf-8", newline="") as f:
        return f.read()


def _read_static(name: str) -> str:
    """Read one file from static/, once, at import time (same newline
    handling as ``_read_template`` -- see its docstring)."""
    with open(_STATIC_DIR / name, encoding="utf-8", newline="") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Shared nav bar -- tabs, request-time renderer, and the sentinel string used
# inside each (pre-baked-minus-nav) page template.
# ---------------------------------------------------------------------------

# tab key -> label. Paths are NOT duplicated here -- url_for() (viz.urls) is
# the single source of truth for tab key -> path, so _render_nav asks it for
# every href rather than carrying its own copy.
_NAV_TABS = [
    ("graph",  "Graph View"),
    ("health", "Health Dashboard"),
    ("stats",  "Stats"),
    ("config", "Config"),
]

# Kept as an importable constant even though it no longer bakes any page
# (nav.js is served as a real static file, referenced via <script src> --
# see the module docstring) -- several regression tests assert directly
# against its content. Read from static/, not templates/, now that nav.js
# lives there.
_NAV_SCRIPT = _read_static("nav.js")

# Sentinel string left in each page shell where the nav div goes; replaced
# by server.py at REQUEST time (see the module docstring's ARCHITECTURE
# NOTE), never here at import time.
_NAV_PLACEHOLDER = "<!--NAV_PLACEHOLDER-->"

# Sentinel string left in each page shell (just before </body>) where the
# shared footer goes -- same two-phase (import-time shell, request-time
# splice) reasoning as _NAV_PLACEHOLDER above: the footer's data-provenance
# half depends on which agent's graph is being rendered, a per-request
# fact PR A's import-time bake can't see. See viz.footer's module
# docstring for the footer's own content/provenance logic; server.py's
# do_GET does the splice, exactly like it does for the nav.
_FOOTER_PLACEHOLDER = "<!--FOOTER_PLACEHOLDER-->"


def _render_nav(active_tab: str, agent: str | None = None) -> str:
    """Return the HTML fragment for the shared top nav bar, computed at
    REQUEST TIME (engram-alpha#2555 PR B amendment-1, requirement 2 + 3).

    Includes tab links (active tab bold, built via ``urls.url_for`` so each
    one carries *agent* through when given) and the global agent-selector
    dropdown. The dropdown is hidden until the shared nav script (served
    from ``/static/nav.js``) populates it (single-agent mode keeps it
    hidden permanently).

    active_tab: one of {"graph", "health", "stats", "config"}.
    agent: the EFFECTIVE agent for this request -- ``agents._resolve_agent``'s
        resolved name (explicit ``?agent=`` else the live ``DEFAULT_AGENT``,
        or the exact unregistered name if one was requested and unknown,
        never silently swapped), not the raw query param. Callers pass
        None only when there is genuinely no agent context (there always
        is one in practice, since ``_resolve_agent`` never returns empty
        unless no agents are registered at all) -- see ``urls.url_for``'s
        docstring for why None/empty are treated identically (no stray
        ``?agent=`` appended to any link) in that edge case.
    """
    links = []
    for tab_key, label in _NAV_TABS:
        href = urls.url_for(tab_key, agent=agent)
        if tab_key == active_tab:
            links.append(f'  <a href="{href}" style="font-weight:600;">{label}</a>')
        else:
            links.append(f'  <a href="{href}">{label}</a>')
    links.append(
        '  <span id="nav-agent-wrapper">'
        '<span class="nav-agent-label">Agent:</span>'
        '<select id="agent-select-nav" class="nav-agent-select"></select>'
        "</span>"
    )
    return "<div class=\"nav\">\n" + "\n".join(links) + "\n</div>"


# ---------------------------------------------------------------------------
# The four page SHELLS -- read once at import time (not per-request),
# matching PR A's module-load-time behavior for everything except the nav
# div itself. Each still contains the literal ``_NAV_PLACEHOLDER`` sentinel
# where the nav div goes -- server.py splices in the real fragment (via
# ``_render_nav``, above) per request. graph.html no longer needs a separate
# assembly step (PR A's ``_build_graph_html``): its CSS/JS are now real
# ``<link>``/``<script src>`` tags baked directly into the template file
# itself (see ``templates/graph.html``), not split-point placeholders
# requiring substitution -- so it's read the same way as the other three.
# ---------------------------------------------------------------------------

HTML = _read_template("graph.html")
HEALTH_HTML = _read_template("health.html")
STATS_HTML = _read_template("stats.html")
CONFIG_HTML = _read_template("config.html")
