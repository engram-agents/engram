"""viz.shell — the nav-rendering + placeholder-substitution machinery, and
the four assembled page-HTML constants (engram-alpha#2555 PR A, pure
relocation of viz_server.py's ``_NAV_TABS``/``_render_nav``/nav-placeholder
constants + the four ``HTML``/``HEALTH_HTML``/``STATS_HTML``/``CONFIG_HTML``
build chains).

No HTTP handling here -- this module's whole job is producing the four
ready-to-serve HTML strings, once, at import time.

TEMPLATE-BAKING NOTE (load-bearing for understanding why the substitution
chain below is mostly a no-op on today's templates, and why it stays
anyway): the original ``viz_server.py`` built each page HTML constant with
a raw string literal *containing* the sentinel placeholders
(``<!--NAV_PLACEHOLDER-->`` etc.), immediately followed by
``.replace(...).replace(...).replace(...)`` in the SAME expression -- so
the value bound to ``HTML``/``HEALTH_HTML``/``STATS_HTML``/``CONFIG_HTML``
at runtime was already POST-substitution; the placeholders never survived
past module-import time in the original file either. The prior commit on
this branch (``bdba3af2``) extracted ``templates/{health,stats,config}.html``
from those ACTUAL RUNTIME VALUES (verified byte-for-byte against the
unmodified original module), so those three template files already have
their nav baked in for their own active tab and contain none of the three
NAV_* sentinel strings. ``templates/graph.html`` is the one exception: its
source giant string ALSO had two artificial ``{{GRAPH_CSS}}``/``{{GRAPH_JS}}``/
``{{GRAPH_JS_AGENT_SELECT}}`` split-points invented for extraction (not
present in the original at all), so it genuinely needs a substitution pass
before it's usable -- see ``_build_graph_html`` below.

Because of this, calling the SAME nav-substitution chain here
(``.replace(_NAV_PLACEHOLDER, ...).replace(_NAV_SCRIPT_PLACEHOLDER,
...).replace(_NAV_CSS_PLACEHOLDER, ...)``) on all four templates is a
verified no-op for three of them today -- kept anyway, rather than special-
cased away, because (a) it reproduces the original's own code shape
exactly, satisfying the pure-relocation contract literally rather than by
argument, (b) ``_render_nav``/``_NAV_TABS`` remain real, working, IMPORTABLE
building blocks here -- the natural home #2555's design flags for PR B's
nav-CSS-consolidation amendment to hook into, and (c) it costs nothing: a
``str.replace`` call that finds no match returns the original string
object-identically cheap. Verified byte-for-byte against the ORIGINAL
module's real runtime ``HTML``/``HEALTH_HTML``/``STATS_HTML``/``CONFIG_HTML``
values (imported the unmodified viz_server.py, diffed the actual strings --
not a visual check), including a reassembly of ``templates/graph.html`` +
``graph.css`` + ``graph.js`` + ``graph-agent-select.js`` via the
``{{GRAPH_*}}`` placeholders.
"""

from __future__ import annotations

from pathlib import Path

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


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


# ---------------------------------------------------------------------------
# Shared nav bar -- tabs, renderer, shared CSS/JS, and the sentinel strings
# used inside the (pre-baked) page templates.
# ---------------------------------------------------------------------------

_NAV_TABS = [
    ("graph",  "/",        "Graph View"),
    ("health", "/health",  "Health Dashboard"),
    ("stats",  "/stats",   "Stats"),
    ("config", "/config",  "Config"),
]

# CSS rules injected into every page's <style> block.
_NAV_SHARED_CSS = _read_template("base.css")


def _render_nav(active_tab: str) -> str:
    """Return the HTML fragment for the shared top nav bar.

    Includes tab links (active tab bold) and the global agent-selector
    dropdown.  The dropdown is hidden until the shared nav script populates
    it (single-agent mode keeps it hidden permanently).

    active_tab: one of {"graph", "health", "stats", "config"}
    """
    links = []
    for tab_key, href, label in _NAV_TABS:
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


# Shared JS block injected just before </body> on every tab page.
# Handles: populating the agent selector from /api/agents, hiding it in
# single-agent mode, rewriting tab links to preserve ?agent=, and
# navigating on change.
# nav.js already carries its own <script>...</script> wrapper tags (it was
# extracted from the ORIGINAL ``_NAV_SCRIPT`` literal, which included them
# -- verified byte-for-byte in bdba3af2), so no extra wrapping here.
_NAV_SCRIPT = _read_template("nav.js")

# Sentinel strings used inside each HTML constant; replaced at module load.
_NAV_PLACEHOLDER = "<!--NAV_PLACEHOLDER-->"
_NAV_SCRIPT_PLACEHOLDER = "<!--NAV_SCRIPT_PLACEHOLDER-->"
_NAV_CSS_PLACEHOLDER = "<!--NAV_CSS_PLACEHOLDER-->"


def _substitute_nav(page: str, active_tab: str) -> str:
    """Apply the original three-step nav substitution chain to *page*.

    A verified no-op on today's pre-baked templates (see module docstring)
    -- kept so this is the one place the chain lives, matching the
    original file's own per-page code shape.
    """
    return (
        page.replace(_NAV_PLACEHOLDER, _render_nav(active_tab))
        .replace(_NAV_SCRIPT_PLACEHOLDER, _NAV_SCRIPT)
        .replace(_NAV_CSS_PLACEHOLDER, _NAV_SHARED_CSS)
    )


# ---------------------------------------------------------------------------
# Graph page -- the one template still carrying real split-point
# placeholders ({{GRAPH_CSS}}, {{GRAPH_JS}}, {{GRAPH_JS_AGENT_SELECT}}),
# invented at extraction time (bdba3af2) to break the single giant page
# string into four files.  Substituted here to reconstruct the original.
# ---------------------------------------------------------------------------

def _build_graph_html() -> str:
    page = _read_template("graph.html")
    page = page.replace("{{GRAPH_CSS}}", _read_template("graph.css"))
    page = page.replace("{{GRAPH_JS}}", _read_template("graph.js"))
    page = page.replace(
        "{{GRAPH_JS_AGENT_SELECT}}", _read_template("graph-agent-select.js")
    )
    return _substitute_nav(page, "graph")


# ---------------------------------------------------------------------------
# The four assembled page-HTML constants -- read once at import time (not
# per-request), matching the original file's module-load-time behavior.
# ---------------------------------------------------------------------------

HTML = _build_graph_html()
HEALTH_HTML = _substitute_nav(_read_template("health.html"), "health")
STATS_HTML = _substitute_nav(_read_template("stats.html"), "stats")
CONFIG_HTML = _substitute_nav(_read_template("config.html"), "config")
