#!/usr/bin/env python3
"""
viz.server — ENGRAM Visualization Server entry point
(engram-alpha#2555 PR A, pure relocation of viz_server.py's ``Handler``
class + ``main()``, byte-identical served-output contract).

Serves a D3.js force-directed graph of an ENGRAM knowledge graph in the
browser. Stdlib-only. Three operating modes:

* Auto-detect (default — no flags): scan accessible /home/* dirs + running
  user's home for .engram*/ folders; read agent_name from each config.json.
  If nothing found, falls back to single-agent default (~/.engram/knowledge.db).
  An optional --config layered on top overrides labels/default/exclude and can
  add agents that weren't auto-detected (back-compat with explicit-db entries).
* Single-agent (--db PATH): register one anonymous 'default' agent at PATH
  (a knowledge.db file or a directory containing one).
* Explicit-only (--no-autodetect [--config PATH]): skip auto-discovery; use
  only the explicit config or the single-agent default.

Usage (module invocation — see the INVOCATION NOTE below for why this is
the shape this file expects, not direct script execution):
    python -m viz.server                                 # auto-detect (new default)
    python -m viz.server --db /path/to/knowledge.db      # single-agent custom
    python -m viz.server --config /path/to/agents.json   # auto-detect + override
    python -m viz.server --no-autodetect --config agents.json  # explicit-only

INVOCATION NOTE (engram-alpha#2555 PR A): the original ``viz_server.py`` was
a single flat file with no relative imports, so it could be run directly
(``python viz_server.py``). This module imports its siblings
(``shell``, ``routes``, ``agents``, ``data.*``) via RELATIVE imports (the
same convention every other module in this package uses — see
``viz/agents.py``'s own docstring for why), which only resolve when this
file is imported as part of the ``viz`` package — i.e. run as
``python -m viz.server`` (or ``python -m engram.viz.server`` from a source
checkout where ``engram`` is itself an importable top-level package), NEVER
as a bare ``python .../viz/server.py`` script. The deployed plugin bundle
ships as a FLAT tree (``viz/``, ``engram_stats.py``, ``engram_core.py``,
``tools/`` etc. all siblings directly under the plugin root — there is no
``engram/`` wrapper package there, confirmed by grep: nothing in this
codebase imports anything as ``engram.<x>``), so the deployed invocation is
``python -m viz.server`` with the plugin root as the working directory
(``-m`` adds the CURRENT WORKING DIRECTORY to ``sys.path``, which is what
makes the ``viz`` package resolvable) — see ``tools/operator-setup-viz.sh``
and the systemd/launchd templates, which already set
``WorkingDirectory``/cwd to the plugin root for exactly this reason.

Deployment / restart model (operator-launched, not agent-managed):
    In a multi-agent install this server is normally started by the OPERATOR
    (a privileged user with read access to every agent's ENGRAM data directory)
    via a small launch script or command — NOT by an individual agent, whose
    user typically lacks the cross-agent read access that auto-detect and
    --config need. The process is long-lived: started once, outside any agent
    session, and it keeps running across agent restarts and upgrades.

    Consequences for an agent operating on the codebase:
    * You generally CANNOT restart this server yourself — it runs under a
      different user, so `pkill`/relaunch will fail on permissions (and you
      should not try; it is shared infrastructure you do not own). Ask the
      operator to re-run their launch script instead.
    * After an upgrade that changes THIS file (or any of its sibling
      modules), the running process keeps executing the OLD code until the
      operator restarts it — the source on disk being new is not enough. So
      the correct post-upgrade action is: ask the operator to restart, then
      confirm the new code is live via GET /api/health (a `health_score`
      field means it responded).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import agents as viz_agents
from . import footer
from . import routes
from . import shell
from . import urls as viz_urls
from .data import calibration as viz_calibration_data
from .data import config as viz_config_data
from .data import graph as viz_graph_data
from .data import health as viz_health_data
from .data import health_v2 as viz_health_v2_data
from .data import schema as viz_schema_data
from .data import stats as viz_stats_data

# ---------------------------------------------------------------------------
# Static assets (engram-alpha#2555 PR B amendment-1, requirement 5): CSS/JS
# served from one real /static/<filename> route with cache headers, instead
# of PR A's baked-into-Python-string-constants shape. static/ is a FLAT
# directory (base.css, nav.js, graph.css, graph.js,
# graph-agent-select.js) -- no subdirectories are ever a legitimate request,
# which is what makes the path-traversal guard below simple to get right:
# reject any filename containing a path separator or '..' outright, rather
# than trying to canonicalize-and-compare against arbitrarily nested input.
# ---------------------------------------------------------------------------

_STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()

_STATIC_CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
}

# These are static assets bundled with the plugin (not per-agent dynamic
# content) -- a 1-hour cache is a reasonable default; nothing here changes
# without a plugin upgrade, which is a process restart anyway (see the
# INVOCATION NOTE above on why a changed source file needs an operator
# restart to take effect).
_STATIC_CACHE_CONTROL = "public, max-age=3600"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        if path == "/api/agents":
            self._json({"agents": viz_agents.get_agents_meta(),
                        "default": viz_agents.DEFAULT_AGENT})
            return

        if path in routes.PAGE_ROUTES:
            # Nav rendering happens HERE, at request time, not at import
            # time in shell.py (engram-alpha#2555 PR B amendment-1,
            # requirement 2 + 3) -- the active tab is a property of the
            # matched route, and each nav link must carry the EFFECTIVE
            # agent through via url_for(), not some import-time default.
            # See shell.py's module docstring for the full two-phase
            # (import-time shell + request-time nav splice) architecture.
            #
            # RESOLVED agent, not the raw ?agent= param -- deliberate fix
            # over the coder-fairy's first cut, which carried through only
            # an EXPLICIT ?agent= and left it absent otherwise, banking on
            # every page independently re-resolving to the SAME default.
            # That holds only while DEFAULT_AGENT is stable across the two
            # requests; agents.py's own _refresh_agents_if_stale() means it
            # is NOT always stable (a TTL-driven re-discovery can promote a
            # different agent to "first alphabetically" between two page
            # loads in the same session) -- exactly the "switching pages
            # silently changes which graph you're looking at" confusion
            # this whole feature exists to remove. _resolve_agent() already
            # does the right fallback (explicit ?agent= else DEFAULT_AGENT,
            # or the exact requested name unresolved-and-unmodified if it
            # names an unregistered agent, never silently swapped) -- reuse
            # its result rather than re-deriving a narrower one here.
            agent_param, page_db_path = viz_agents._resolve_agent(parsed.query)
            active_tab = viz_urls.route_name_for_path(path)
            # route_name_for_path's own docstring says a None here would
            # mean routes.PAGE_ROUTES and urls._ROUTE_PATHS have drifted
            # apart, "a bug worth surfacing loudly" -- but nothing DID
            # surface it loudly until this assert (reviewer-fairy finding,
            # PR#2564): without it, None flowed silently into _render_nav
            # and rendered a nav bar with no tab bolded, not the loud
            # failure the docstring promised. `path in routes.PAGE_ROUTES`
            # was already checked one line above (the `if` this block is
            # inside), so a None here can only mean the two tables drifted.
            assert active_tab is not None, (
                f"routes.PAGE_ROUTES and urls._ROUTE_PATHS have drifted: "
                f"{path!r} is a known page route but url_for's registry "
                f"doesn't know its route name"
            )
            nav_html = shell._render_nav(active_tab, agent=agent_param)
            # Footer's data-provenance half needs the SELECTED agent's own
            # data directory (the parent of its knowledge.db), not the
            # process's default -- same resolved-agent value the nav just
            # used, per the per-selected-agent ruling on forum #389.
            data_dir = os.path.dirname(page_db_path) if page_db_path else None
            footer_html = footer.render_footer(data_dir)
            page = (
                routes.PAGE_ROUTES[path]
                .replace(shell._NAV_PLACEHOLDER, nav_html)
                .replace(shell._FOOTER_PLACEHOLDER, footer_html)
            )
            self._html(page)
            return

        # Endpoints below are agent-scoped.
        agent_name, db_path = viz_agents._resolve_agent(parsed.query)

        if path == "/api/graph":
            if not db_path:
                self._json({"nodes": [], "edges": [],
                            "error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name})
            else:
                resp = viz_graph_data.get_graph(db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/schema":
            # Schema is static (no DB required) — return the SSoT dict built
            # from engram_core / engram_query / engram_stats constants.
            self._json(viz_schema_data.get_schema_data())
        elif path == "/api/meta":
            meta_resp = {"agent": agent_name,
                         "db_path": db_path,
                         "default_agent": viz_agents.DEFAULT_AGENT,
                         "label": viz_agents.AGENTS.get(agent_name, {}).get("label", agent_name)}
            # Include agent's memory-config (tier sizes, decay base) so the
            # front-end tier-classification uses the actual config values,
            # not viz-side hardcoded defaults. Lei flagged 2026-05-17 that
            # viz was showing nodes as tier 3 because TIER2_MAX=1000 was
            # hardcoded while config has tier2_max_nodes=4000.
            meta_resp["memory_config"] = viz_config_data._resolve_agent_config(agent_name)
            self._json(meta_resp)
        elif path == "/api/health":
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                resp = viz_health_data.get_health_data(db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/stats":
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                index_db_path = viz_stats_data._resolve_logs_index(agent_name)
                resp = viz_stats_data.get_stats_data(index_db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/health/v2":
            # health-v2 SSoT panel (engram-alpha#2518) -- backend loader
            # only; the detail-view frontend that consumes this endpoint is
            # a separate, not-yet-landed PR. Single-agent only (no /fleet
            # here -- reading OTHER agents' snapshot directories needs a
            # separate cross-UID permission design, out of scope for this
            # slice).
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                resp = viz_health_v2_data.get_health_v2_data(agent_name)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/health/v2/series":
            # health-v2 SERIES panel (engram-alpha#2575 PR C1) -- reads
            # `series.db`'s own `export()` (health.series). Always 200, even
            # for a fail-honest `{error, reason}` dict -- same convention as
            # the sibling `/api/health/v2` endpoint above (the error IS the
            # payload, not an HTTP-level failure); an unknown agent is the
            # one case that still gets a real 404, matching every other
            # per-agent endpoint on this handler.
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                qs = parse_qs(parsed.query or "")
                version = (qs.get("version", [None])[0]) or None
                signal = (qs.get("signal", [None])[0]) or None
                resp = viz_health_v2_data.get_health_v2_series(agent_name, version, signal)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/calibration":
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                resp = viz_calibration_data.get_calibration_data(db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/config":
            resp = viz_config_data.get_config_data(agent_name)
            self._json(resp)
        elif path == "/api/search":
            if not db_path:
                self._json({"results": [], "error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                qs = parse_qs(parsed.query or "")
                query = (qs.get("q", [None])[0] or "").strip()
                if not query:
                    self._json({"results": [], "error": "q parameter is required",
                                "agent": agent_name}, status=400)
                else:
                    resp = viz_graph_data.search_nodes(db_path, query)
                    resp["agent"] = agent_name
                    self._json(resp)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        """CORS preflight — needed for PUT from a browser on a different origin."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_PUT(self):
        """PUT /api/config/<key>?agent=<name>

        Request body: JSON {"value": <new_value>}
        Response (200 on success):
          {"ok": true, "restart_required": bool, "validated_value": <val>}
        Response (400 on validation error / unknown key):
          {"ok": false, "error": "<msg>"}
        Response (404 on path mismatch):
          {"ok": false, "error": "not found"}
        """
        parsed = urlparse(self.path)
        path = parsed.path

        # Route: /api/config/<key>  (key may be dotted, e.g. cadence.drowsiness_caution_pct)
        _PREFIX = "/api/config/"
        if not path.startswith(_PREFIX):
            self._json({"ok": False, "error": "not found"}, status=404)
            return

        key = path[len(_PREFIX):]
        if not key:
            self._json({"ok": False, "error": "key required in path"}, status=400)
            return

        # Parse request body.
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        if content_length == 0:
            self._json({"ok": False, "error": "request body required"}, status=400)
            return
        try:
            body_bytes = self.rfile.read(content_length)
            body = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError) as exc:
            self._json({"ok": False, "error": f"invalid JSON body: {exc}"}, status=400)
            return

        if "value" not in body:
            self._json({"ok": False, "error": "'value' field required in body"}, status=400)
            return

        # Resolve agent — same as GET.
        agent_name, _db_path = viz_agents._resolve_agent(parsed.query)

        try:
            result = viz_config_data.write_config_key(agent_name, key, body["value"])
        except OSError:
            raise
        except Exception as exc:
            self._json(
                {"ok": False, "error": f"internal error: {exc}"}, status=500
            )
            return
        status = 200 if result.get("ok") else 400
        self._json(result, status=status)

    def _json(self, data: dict, status: int = 200):
        """Send JSON response. status defaults to 200 (preserves the historical
        no-status callsite shape); pass an explicit status for error responses."""
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, filename: str):
        """Serve one file from viz/static/ (engram-alpha#2555 PR B
        amendment-1, requirement 5). *filename* is everything after
        "/static/" in the request path -- NOT yet validated.

        Path-traversal guard: static/ is a flat directory, so any filename
        containing a path separator or a '..' component is rejected
        outright as 404, rather than attempting to canonicalize-and-compare
        (the flat-directory invariant makes the reject-outright form both
        simpler and strictly more conservative than that would be).
        """
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            self.send_response(404)
            self.end_headers()
            return

        target = _STATIC_DIR / filename
        if not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        # Belt-and-suspenders: confirm the resolved path is still directly
        # inside _STATIC_DIR (guards against the target itself being a
        # symlink that escapes static/, which the string-level checks above
        # don't see).
        if target.resolve().parent != _STATIC_DIR:
            self.send_response(404)
            self.end_headers()
            return

        content_type = _STATIC_CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", _STATIC_CACHE_CONTROL)
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ENGRAM visualization server — D3 force-directed graph for knowledge.db"
    )
    parser.add_argument("--port", type=int, default=viz_agents.DEFAULT_PORT,
                        help=f"Port to listen on (default: {viz_agents.DEFAULT_PORT})")
    parser.add_argument("--db", default=None,
                        help="Single-agent mode: register one anonymous "
                             "'default' agent at PATH (a knowledge.db file or "
                             f"a directory containing one). Default: {viz_agents.DEFAULT_DB}")
    parser.add_argument("--config", default=None,
                        help="Optional config JSON: override labels/default/exclude "
                             "for auto-detected agents, or add agents with explicit db. "
                             "Without --no-autodetect, auto-detect is always the base.")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="Address to bind to (default: 127.0.0.1)")
    parser.add_argument("--no-autodetect", action="store_true",
                        help="Disable auto-discovery; use only --config or --db.")
    args = parser.parse_args()

    if args.config and args.db:
        parser.error("--config and --db are mutually exclusive")

    # AGENTS/DEFAULT_AGENT/_AGENT_STARTUP_MODE/_AGENT_STARTUP_CONFIG/
    # _AGENT_CACHE_TS all live in viz.agents and are REASSIGNED there (not
    # mutated in place) — so this function sets them via attribute
    # assignment on the live viz_agents module object, never a local
    # `global` (that would only rebind a name in THIS module, not the one
    # every other module reads through `viz_agents.<name>` attribute
    # access). See viz/agents.py's own module docstring for the full
    # account of this hazard.
    if args.db:
        # Explicit --db always forces single-agent mode (unchanged behavior).
        db = os.path.expanduser(args.db)
        if os.path.isdir(db):
            db = os.path.join(db, "knowledge.db")
        viz_agents.AGENTS = {"default": {"name": "default", "label": "Default", "db": db}}
        viz_agents.DEFAULT_AGENT = "default"
        viz_agents._AGENT_STARTUP_MODE = "single_db"
    elif args.no_autodetect:
        # Opt-out: explicit-config-only path (old behavior).
        if args.config:
            viz_agents.AGENTS, viz_agents.DEFAULT_AGENT = viz_agents._load_config(args.config)
            viz_agents._AGENT_STARTUP_MODE = "config_only"
            viz_agents._AGENT_STARTUP_CONFIG = args.config
        else:
            db = viz_agents.DEFAULT_DB
            viz_agents.AGENTS = {"default": {"name": "default", "label": "Default", "db": db}}
            viz_agents.DEFAULT_AGENT = "default"
            viz_agents._AGENT_STARTUP_MODE = "single_db"
    else:
        # Default path: auto-detect is the base.
        discovered = viz_agents.discover_agents()
        if args.config:
            # Layer explicit config on top of discovered.
            viz_agents.AGENTS, viz_agents.DEFAULT_AGENT = viz_agents._merge_config_over_discovered(
                discovered, args.config
            )
            viz_agents._AGENT_STARTUP_MODE = "discover_merge"
            viz_agents._AGENT_STARTUP_CONFIG = args.config
        elif discovered:
            # Pure auto-detect: prefer the launching context's own agent as
            # default; fall back to alphabetically first for determinism.
            viz_agents.AGENTS = discovered
            viz_agents.DEFAULT_AGENT = viz_agents._pick_default(viz_agents.AGENTS)
            viz_agents._AGENT_STARTUP_MODE = "discover_only"
        else:
            # Nothing discovered and no config — fall back to single-agent default.
            db = viz_agents.DEFAULT_DB
            viz_agents.AGENTS = {"default": {"name": "default", "label": "Default", "db": db}}
            viz_agents.DEFAULT_AGENT = "default"
            viz_agents._AGENT_STARTUP_MODE = "discover_fallback"

    # Seed the live-refresh cache timestamp so the first request is a cache hit.
    viz_agents._AGENT_CACHE_TS = time.monotonic()

    HTTPServer.allow_reuse_address = True
    bind = args.bind or "127.0.0.1"
    server = HTTPServer((bind, args.port), Handler)
    print(f"ENGRAM Visualizer →  http://localhost:{args.port}")
    print(f"Health Dashboard  →  http://localhost:{args.port}/health")
    print(f"Config tab        →  http://localhost:{args.port}/config")
    print("Agents registered:")
    for name, info in viz_agents.AGENTS.items():
        marker = " (default)" if name == viz_agents.DEFAULT_AGENT else ""
        exists = "" if os.path.exists(info["db"]) else " [MISSING]"
        print(f"   - {name}{marker}: {info['db']}{exists}")
    print("Press Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
