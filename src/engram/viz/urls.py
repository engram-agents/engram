"""viz.urls — url_for(name, **params): the one function that builds every
internal link (engram-alpha#2555 PR B, amendment-1 requirement 3, plus
this file's own placement call from the design threads).

Deliberately a standalone module with NO dependency on ``shell`` or
``routes``: ``shell._render_nav`` calls ``url_for`` for every nav-tab link,
so the dependency has to run shell -> urls (never the reverse), or the two
modules would import each other. Keeping this module leaf-level (only
stdlib) avoids that entirely.

Route names ("graph"/"health"/"stats"/"config") match ``shell._NAV_TABS``'s
tab keys 1:1, and the paths match ``routes.PAGE_ROUTES``'s keys 1:1. No
single shared registry object binds all three together (a deliberate
choice, not an oversight: the three name/path pairs are short, and this
keeps the two page-serving modules from importing each other). A mismatch
fails LOUDLY: ``ValueError`` from ``url_for`` the first time a page is
requested with an unknown name, or an explicit ``assert`` at
``server.py``'s one ``route_name_for_path`` call site (added after a
reviewer-fairy finding on PR#2564: the docstring here originally claimed
this would fail loudly on its own, but a drifted ``None`` actually flowed
silently into ``shell._render_nav`` and rendered a nav bar with no tab
bolded — the assert is what makes the claim true).
"""

from __future__ import annotations

from urllib.parse import urlencode

# Route name -> path, for the 4 whole-page GET routes registered in
# routes.PAGE_ROUTES. Mandatory per the PR spec; the "static" pseudo-route
# below extends the registry to /static/<file> asset URLs, which is not one
# of the 4 page routes but is a useful third thing every internal link
# should be able to go through this one function for.
_ROUTE_PATHS: dict[str, str] = {
    "graph": "/",
    "health": "/health",
    "stats": "/stats",
    "config": "/config",
}


def url_for(name: str, **params) -> str:
    """Build an internal URL for route *name*.

    *name* is one of "graph"/"health"/"stats"/"config", or the special
    value "static" (which additionally requires a ``filename=`` kwarg and
    ignores any other kwargs).

    Every other kwarg becomes a query parameter, but only when its value is
    truthy — in particular ``agent=``, when given and non-empty, carries the
    EFFECTIVE agent through (the caller is expected to pass the RESOLVED
    name from ``agents._resolve_agent``, not a raw query param — see
    ``shell._render_nav``'s docstring for why: a literal-param-only carry
    silently breaks the moment ``DEFAULT_AGENT`` changes between two page
    loads, the exact "switching pages server-side silently changes which
    graph is being viewed" bug this function exists to remove). A
    falsy/None/empty-string value is dropped entirely rather than
    serialized as ``agent=`` or ``agent=None`` — the caller is expected to
    pass ``agent=None`` (not ``agent=""``) for "no agent context at all"
    (in practice this happens only if no agents are registered at all,
    since ``_resolve_agent`` otherwise always resolves to a real name),
    and both are treated the same way here regardless.
    """
    if name == "static":
        filename = params.get("filename")
        if not filename:
            raise ValueError("url_for('static', ...) requires filename=")
        return f"/static/{filename}"

    try:
        path = _ROUTE_PATHS[name]
    except KeyError:
        raise ValueError(f"Unknown route name: {name!r}") from None

    query = {k: v for k, v in params.items() if v}
    if not query:
        return path
    return f"{path}?{urlencode(query)}"


def route_name_for_path(path: str) -> str | None:
    """Reverse lookup: request path -> route name, for active-tab resolution.

    Returns None for any path that isn't one of the 4 page routes. The one
    caller (``server.py``'s ``do_GET``) only invokes this after already
    confirming ``path in routes.PAGE_ROUTES``, so a None return there would
    mean ``routes.PAGE_ROUTES`` and ``_ROUTE_PATHS`` have drifted apart —
    a bug worth surfacing loudly (an ``AssertionError``/``KeyError`` at the
    call site), not a case to silently paper over here.
    """
    for route_name, route_path in _ROUTE_PATHS.items():
        if route_path == path:
            return route_name
    return None
