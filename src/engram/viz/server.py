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
import gzip
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import agents as viz_agents
from . import footer
from . import routes
from . import shell
from . import static_serve
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
# of PR A's baked-into-Python-string-constants shape.
#
# engram-alpha#2754 PR B: the actual path-traversal-guarded serving logic +
# _STATIC_DIR/_STATIC_CONTENT_TYPES/_STATIC_CACHE_CONTROL now live in
# viz.static_serve, shared with viz.workbench (the new :5021 Curve
# Workbench entry point this same PR adds) so both processes serve the
# same flat viz/static/ directory through ONE code path -- see that
# module for the constants/logic itself.
# ---------------------------------------------------------------------------

# R5.5 opt-in access log: a module-level path, unset (None) by default --
# behavior is UNCHANGED (Handler.log_message stays a no-op) unless the
# server is started with --access-log <path> (see main()). A plain module
# attribute (not a class-level constant) so tests can set
# `viz_server._ACCESS_LOG_PATH` directly the same way other startup state
# in this module (viz_agents.AGENTS etc.) is monkeypatched, without going
# through argparse/main().
_ACCESS_LOG_PATH: "str | None" = None


# ---------------------------------------------------------------------------
# Curve Workbench data (engram-alpha#2727) -- developer-tier only.
#
# engram-alpha#2754 PR B moved the Curve Workbench PAGE (and its dev-tier
# header link) off :5001 entirely, onto its own standalone entry point
# (viz.workbench, :5021 by default) -- see that module for the page-serving
# side of this feature, and its own docstring for the judgment call on
# whether a tier gate is still needed there (short answer: no, launching
# that module at all is itself the gate). The OLD, now-removed
# _workbench_available() gated THREE things at once (the health.html header
# link, the /health/workbench page route, and this one API endpoint); with
# the first two gone, only this endpoint's gate survives here, scoped down
# to what it alone still needs: viz.data.health_workbench's importability
# (NOT the page template's presence, which is irrelevant to an API
# response). :5001 still serves /api/health/v2/model per the issue's own
# text (only the workbench PAGE and header LINK leave :5001) -- but
# viz/data/health_workbench.py itself remains dev-tier-only
# (src/build/packaging/tiers.json), so an unconditional top-level import
# here would ImportError-crash a convenience/essential-tier :5001 process
# at import time. This lazy, function-scoped import is what the OLD
# _workbench_available() used for the same reason (see
# src/build/engine/README.md, "Known gaps" #3: "Lazy imports are invisible
# to the tier-closure check") -- lets this "convenience"-tier module
# reference a "dev"-tier-only sibling without tripping
# test_import_dep_tier_consistency's "importer and importee must ship at
# the same tier" rule, which only walks TOP-LEVEL module-scope imports.
# ---------------------------------------------------------------------------


def _health_workbench_model_available() -> bool:
    """True iff viz.data.health_workbench is importable in this process --
    i.e. this build shipped the Curve Workbench's dev-tier data module. A
    plain function (not a cached module-level constant) so tests can
    monkeypatch it directly to simulate flipping the tier gate on/off,
    without needing to fake the filesystem or reinstall a different tier."""
    try:
        from .data import health_workbench as _  # noqa: F401
    except ImportError:
        return False
    return True


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 persistent connections -- required for ThreadingHTTPServer's
    # concurrency to actually help a slow-client scenario (engram-alpha#2866
    # fix 2): the default HTTP/1.0 closes the connection after every response
    # regardless of threading. Every response MUST send an accurate
    # Content-Length (or use chunked encoding) once this is set, or a client
    # will hang waiting for data that never comes since the connection is no
    # longer closed as an end-of-response signal -- see _json()/_html() below,
    # both of which already compute Content-Length from the actual bytes
    # written on the wire (post-gzip for _json(), per fix 1).
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # R5.5: opt-in access log, off by default (unchanged behavior --
        # still suppresses BaseHTTPRequestHandler's own stderr access log
        # unless --access-log <path> was passed at startup, see main()).
        # BaseHTTPRequestHandler.log_request() (called from send_response
        # for every response on this Handler) invokes this with
        # args = (self.requestline, str(code), size) -- args[1] is the
        # status code as a string; method/path are read from self.command/
        # self.path directly rather than re-parsed out of `fmt`/args[0],
        # since both are already live attributes on this same instance at
        # call time.
        if not _ACCESS_LOG_PATH:
            return
        start = getattr(self, "_access_log_start", None)
        duration_ms = (time.monotonic() - start) * 1000.0 if start is not None else 0.0
        status = args[1] if len(args) > 1 else "-"
        remote = self.client_address[0] if self.client_address else "-"
        ts = datetime.now(timezone.utc).isoformat()
        line = f"{ts} {self.command} {self.path} {status} {duration_ms:.2f} {remote}\n"
        try:
            with open(_ACCESS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # best-effort logging only -- never fail the request over it

    def do_GET(self):
        self._access_log_start = time.monotonic()
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/static/"):
            static_serve.serve_static(self, path[len("/static/"):])
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
        elif path == "/api/health/v2/model":
            # Curve Workbench (engram-alpha#2727) -- the SHIPPED scoring
            # model's curve/importance/aspect-points declarations. NOT
            # agent-scoped data (every seat on this install runs the SAME
            # code, so there is exactly one shipped model to read) --
            # unlike every other branch in this elif chain, this one does
            # not consult db_path/agent_name at all. Dev-tier-gated: 404
            # (not the fail-honest 200 the sibling `/series` endpoint
            # uses) when this build didn't ship the Workbench's data
            # module -- see _health_workbench_model_available()'s own
            # docstring. This endpoint stays on :5001 per engram-alpha#2754
            # PR B ("only the workbench PAGE and header LINK leave :5001")
            # even though the workbench PAGE itself has moved to its own
            # standalone entry point (viz.workbench, :5021 by default),
            # which serves its OWN unconditional copy of this same data.
            if not _health_workbench_model_available():
                self._json(
                    {"error": "Curve Workbench is not available in this "
                              "install (developer tier only)",
                     "reason": "not_developer_tier"},
                    status=404,
                )
            else:
                from .data import health_workbench as viz_health_workbench_data
                self._json(viz_health_workbench_data.get_workbench_model())
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
            # Reviewer-fairy catch on PR-2926 (2026-09-14): an empty body with
            # no Content-Length was safe under the old HTTP/1.0 default-close
            # behavior (the closed connection itself signalled end-of-response)
            # but hangs a keep-alive HTTP/1.1 client indefinitely now that this
            # file sets protocol_version = "HTTP/1.1" -- reproduced live, a
            # real client's read timed out against this exact path pre-fix.
            # Content-Length: 0 is correct either way and costs nothing under
            # HTTP/1.0.
            self.send_header("Content-Length", "0")
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
        no-status callsite shape); pass an explicit status for error responses.

        If the client sends `Accept-Encoding: gzip`, the body is gzip-compressed
        and sent with `Content-Encoding: gzip` + the COMPRESSED Content-Length
        (engram-alpha#2866 fix 1 -- /api/graph ships uncompressed and can run to
        tens of MB on a large graph). A client that doesn't advertise gzip
        support gets byte-identical behavior to before this fix."""
        body = json.dumps(data, default=str).encode()
        accept_encoding = self.headers.get("Accept-Encoding", "")
        gzip_ok = "gzip" in accept_encoding
        if gzip_ok:
            body = gzip.compress(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if gzip_ok:
            self.send_header("Content-Encoding", "gzip")
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
    parser.add_argument("--access-log", default=None,
                        help="Optional path to an access-log file. When set, appends "
                             "one line per request (ISO-UTC method path status "
                             "duration_ms remote). Default: off (no access logging, "
                             "unchanged behavior).")
    args = parser.parse_args()

    if args.access_log:
        global _ACCESS_LOG_PATH
        _ACCESS_LOG_PATH = os.path.expanduser(args.access_log)

    if args.config and args.db:
        parser.error("--config and --db are mutually exclusive")

    # Resolves AGENTS/DEFAULT_AGENT/_AGENT_STARTUP_MODE/_AGENT_STARTUP_CONFIG/
    # _AGENT_CACHE_TS from these same --db/--config/--no-autodetect flags.
    # Extracted to viz.agents (engram-alpha#2754 PR B) so viz.workbench's
    # own main() resolves the IDENTICAL agent set from the same flags --
    # see that function's own docstring for the full account of why this
    # moved and the hazard it's careful about (module-attribute reassignment,
    # not a local `global`).
    viz_agents.configure_agents_from_args(args)

    # ThreadingHTTPServer (engram-alpha#2866 fix 2): plain HTTPServer is
    # single-threaded, so one slow client blocks every other request behind
    # it (measured: a 193-byte request delayed 58s behind a rate-limited
    # competitor). One thread per connection removes that head-of-line
    # block. Combined with Handler.protocol_version = "HTTP/1.1" above for
    # keep-alive.
    ThreadingHTTPServer.allow_reuse_address = True
    bind = args.bind or "127.0.0.1"
    server = ThreadingHTTPServer((bind, args.port), Handler)
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
