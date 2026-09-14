#!/usr/bin/env python3
"""
viz.workbench — the Curve Workbench's standalone entry point
(engram-alpha#2754 PR B).

The Curve Workbench (engram-alpha#2727) -- an editable dev-tool for
picking health-v2 curve knots and per-metric importance against real
history -- used to be a route on the main ``:5001`` dashboard
(``viz.server``), gated by a request-time tier check
(``_workbench_available()``). Per issue #2754's ruling ("a new port
sounds good" / "Port, not file"), the Workbench moves to its OWN
standalone HTTP server process on its own port (``:5021`` by default),
serving ONLY: the editable page itself, the three static JS files it
needs, and the four read-only APIs it depends on
(``/api/health/v2``, ``/api/health/v2/series``, ``/api/health/v2/model``,
``/api/agents``) -- all against the SAME operator config ``viz.server``
would use (``--db``/``--config``/``--no-autodetect``, resolved through
the identical ``viz.agents.configure_agents_from_args`` helper both
entry points call).

``:5001`` drops the ``/health/workbench`` route and the header link
entirely (see ``viz.server``'s own history for what was removed); this
module is a genuinely separate process, not a mode flag on the old one.
Same-origin (this page only ever talks to ITS OWN port's APIs), so no
CORS change and no new security surface versus the old in-process route.

JUDGMENT CALL 1 (tier gate) -- flagged for the parent's review: the OLD
``_workbench_available()`` existed because a SHARED, always-running
``:5001`` process had to re-check, on every request, whether THIS
particular build shipped the dev-tier files (checking file-presence was
the only observable proxy for "did this install ship the dev tier" --
see that function's own removed docstring / server.py's history for the
full reasoning, which still holds). This module's situation is
different in kind, not just degree: launching ``python -m viz.workbench``
at all is itself an OPERATOR's deliberate choice, made once, out-of-band
from any browser request -- there is no request-time branching left to
protect, because there is no other route on this process a tier-gate
could be defending. So no PER-REQUEST gate is needed here. What IS kept,
per this file's own lean toward "some defensive check is cheap": a
STARTUP-time (module-import-time) check that this build actually shipped
the dev-tier files this module needs (below) -- fails loud with a clear
operator-facing message before any socket is ever bound, rather than
letting a bare ``ImportError`` traceback (or, worse, a half-started
server) be the first sign something's wrong. This is strictly less
machinery than the old per-request gate, not a re-implementation of it.

JUDGMENT CALL 2 (route shape) -- the page is served at ``/`` (root), not
``/health/workbench``: this whole process IS the Workbench now, so
nothing else competes for ``/`` on this port. No ``/health/workbench``
alias/redirect is provided for old bookmarks (a nice-to-have the issue
doesn't require; skipped as not worth the extra route on a brand-new
entry point nobody has bookmarked yet).

JUDGMENT CALL 3 (default port) -- ``5021``, per the issue's own text.
No live precedent for a ":5021 pattern from rounds 1-5" was found
anywhere in this codebase's source or git history (grepped both); per
the issue's own fallback guidance, ``5021`` is used as a bare default
constant.

Usage::

    python -m viz.workbench                                   # auto-detect
    python -m viz.workbench --port 5021 --db /path/to/knowledge.db
    python -m viz.workbench --config /path/to/agents.json

A systemd user unit ships for this module as of #2894/#2907
(``engram-workbench-user.service``), installed via
``tools/operator-setup-viz.sh`` on Linux the same way ``viz.server``'s own
unit is -- Lei asked for one explicitly ("A user service sounds good, find
someone build it please"), superseding the earlier "no systemd unit
needed" call made for #2754. The module still ships at DEV tier only (see
``tiers.json``): the installer's workbench gate is a separate, non-fatal
check, so the unit is skipped (not an install failure) on a build that
doesn't carry the dev-tier files, and the module can still be run in the
foreground directly (``python -m viz.workbench``) either way.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import agents as viz_agents
from . import footer
from . import shell
from . import static_serve
from .data import health_v2 as viz_health_v2_data

# ---------------------------------------------------------------------------
# Startup-time dev-tier check (see JUDGMENT CALL 1 above). ALL FIVE files
# the page route (``/``) depends on -- the page template, the backend
# data module, and the three ``<script>`` tags the template itself loads
# (``health-workbench.js`` / ``health-workbench-curve.js`` /
# ``health-detail-math.js``) -- are
# required for this process to do anything meaningful, checked together,
# at IMPORT time, so a convenience/essential-tier build (or a build whose
# manifest ships only part of the set) fails loud and early (before any
# argparse, before any socket bind) rather than with a bare traceback --
# or, worse, a half-started server whose page 404s on its own <script>
# tags -- the first time a request actually needs a missing piece
# (engram-alpha#2735: the OLD viz.server._workbench_available() checked
# only the template + data module, not the two scripts the page also
# loads, so a build shipping half the set silently passed its gate).
#
# Unlike viz.server's OLD _workbench_available() (removed in #2754), this
# import is a plain TOP-LEVEL one, not a lazy function-scoped one --
# deliberately: this module itself ships at "dev" tier in
# src/build/packaging/tiers.json, the SAME tier as
# viz/data/health_workbench.py, so test_import_dep_tier_consistency's
# "importer and importee must ship at the same tier" rule is satisfied
# directly, with no need for the lazy-import trick that hid the OLD
# ("convenience"-tier server.py importing a "dev"-tier sibling) mismatch
# from that same static checker.
# ---------------------------------------------------------------------------

_WORKBENCH_TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "templates" / "health_workbench.html"
)
_WORKBENCH_JS_PATH = static_serve._STATIC_DIR / "health-workbench.js"
_WORKBENCH_CURVE_JS_PATH = static_serve._STATIC_DIR / "health-workbench-curve.js"
_WORKBENCH_DETAIL_MATH_JS_PATH = static_serve._STATIC_DIR / "health-detail-math.js"


def _missing_workbench_deps(
    template_path=None,
    js_path=None,
    curve_js_path=None,
    detail_math_js_path=None,
    import_data_module=None,
):
    """Names of the Curve Workbench's dependencies that are NOT present /
    importable -- the five files the ``/`` page route depends on: the
    page template, the three ``<script>`` tags it loads, and the backend
    data module (``viz/data/health_workbench.py``). Empty list = every
    dependency is present, gate open.

    A plain, parameterized function -- not folded directly into
    module-import-time code -- so tests can mutate each dependency
    independently (pass an alternate path for one of the four file
    checks, or a raising ``import_data_module`` callable for the fifth)
    and call this directly, without needing to fake the filesystem or
    re-import this module. This preserves the same testability the OLD,
    now-removed ``viz.server._workbench_available()`` had ("a plain
    function, not a cached module-level constant, exactly so tests can
    [flip it] without needing to fake the filesystem") in the closest
    equivalent shape for a startup-time check.

    Defaults (when called with no arguments -- what module-import time
    below actually does) resolve to the real module-level path constants
    and the real import of ``.data.health_workbench``.
    """
    template_path = _WORKBENCH_TEMPLATE_PATH if template_path is None else template_path
    js_path = _WORKBENCH_JS_PATH if js_path is None else js_path
    curve_js_path = _WORKBENCH_CURVE_JS_PATH if curve_js_path is None else curve_js_path
    detail_math_js_path = (
        _WORKBENCH_DETAIL_MATH_JS_PATH if detail_math_js_path is None else detail_math_js_path
    )

    missing = []
    if not template_path.is_file():
        missing.append(str(template_path))
    if not js_path.is_file():
        missing.append(str(js_path))
    if not curve_js_path.is_file():
        missing.append(str(curve_js_path))
    if not detail_math_js_path.is_file():
        missing.append(str(detail_math_js_path))

    if import_data_module is None:
        def import_data_module():
            from .data import health_workbench as _  # noqa: F401
    try:
        import_data_module()
    except ImportError as exc:
        missing.append(f"viz/data/health_workbench.py (not importable: {exc})")

    return missing


_MISSING_WORKBENCH_DEPS = _missing_workbench_deps()
if _MISSING_WORKBENCH_DEPS:  # pragma: no cover -- exercised only on a
    # convenience/essential-tier build, or a dev-tier build whose manifest
    # shipped only part of the set, that doesn't ship all five files this
    # process needs.
    raise SystemExit(
        "viz.workbench requires the Curve Workbench's dev-tier files, "
        "which this install does not ship or cannot import: "
        f"{'; '.join(_MISSING_WORKBENCH_DEPS)}. Build/install with the "
        "'dev' tier to use this entry point."
    )

# Real import for actual use by the Handler below -- guaranteed to
# succeed at this point since _missing_workbench_deps() above already
# confirmed it imports cleanly (or this module would already have exited).
from .data import health_workbench as viz_health_workbench_data


# ---------------------------------------------------------------------------
# R5.2-equivalent cache-busting stamp for the workbench's two own script
# tags (health-workbench-curve.js / health-workbench.js) -- moved
# wholesale from viz.server (engram-alpha#2555/#2727 R5.2), which no
# longer serves any page that references those two tags once this PR
# lands. Computed ONCE here, at import time, never per-request -- same
# reasoning as the original: a same-hour redeploy is otherwise invisible
# to a browser holding static_serve's 1h Cache-Control.
# ---------------------------------------------------------------------------

def _compute_static_version_stamp() -> str:
    """Prefers the git short SHA of the tree this file runs from (`git -C
    <this dir> rev-parse --short HEAD` -- git itself walks up from that
    directory to find the enclosing repo, so this resolves correctly
    whether or not `viz/` itself is a repo root). Falls back to the max
    mtime of the two workbench script files (as an integer string) when
    git isn't resolvable at all (e.g. an installed plugin tree with no
    `.git` anywhere above it).
    """
    import subprocess

    src_root = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "-C", str(src_root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        candidates = [
            static_serve._STATIC_DIR / "health-workbench-curve.js",
            static_serve._STATIC_DIR / "health-workbench.js",
        ]
        mtimes = [p.stat().st_mtime for p in candidates if p.is_file()]
        if mtimes:
            return str(int(max(mtimes)))
    except OSError:
        pass
    return "0"


_STATIC_VERSION_STAMP = _compute_static_version_stamp()

_DEFAULT_PORT = 5021

# engram-alpha#2767: viz.server's R5.5 opt-in access log, mirrored here for
# parity -- an operator debugging THIS process had no equivalent to
# viz.server's opt-in request log. Same shape: unset (None) by default, so
# behavior is UNCHANGED (Handler.log_message stays a no-op) unless
# --access-log <path> is passed at startup (see main()).
_ACCESS_LOG_PATH: "str | None" = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Mirrors viz.server.Handler.log_message exactly (engram-alpha#2767)
        # -- see that method's own docstring for why args[1]/self.command/
        # self.path are read directly rather than re-parsed out of fmt.
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

        if path == "/":
            self._serve_page(parsed.query)
            return

        # Endpoints below are agent-scoped.
        agent_name, db_path = viz_agents._resolve_agent(parsed.query)

        if path == "/api/health/v2":
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                resp = viz_health_v2_data.get_health_v2_data(agent_name)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/health/v2/series":
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
            # NOT agent-scoped (every seat on this install runs the SAME
            # code, so there is exactly one shipped model to read) --
            # same as viz.server's sibling copy of this endpoint. No tier
            # gate here: this process only runs at all because the
            # dev-tier files (including this data module) are present --
            # see the startup-time check at module-import time above.
            self._json(viz_health_workbench_data.get_workbench_model())
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_page(self, query: str):
        """GET /?agent=<seat> -- the Curve Workbench page itself.

        Preserves the one load-bearing fail-honest case from the OLD
        ``viz.server._serve_workbench_page`` (removed in this PR): an
        unknown/unregistered ``?agent=`` 404s with the same
        ``{"error": ..., "agent": ...}`` shape every agent-scoped
        endpoint on this handler uses -- this page has nothing meaningful
        to render for an agent with no data. The OLD method's tier-gate
        branch is NOT carried over (moot here -- see JUDGMENT CALL 1 in
        this module's own docstring), and its nav-bar splice is DROPPED,
        not carried over either: ``health_workbench.html``'s own docstring
        confirms the nav placeholder was already removed from this
        template in round 2, making that ``.replace()`` call a
        pre-existing no-op even in the OLD code.
        """
        agent_param, page_db_path = viz_agents._resolve_agent(query)
        if not page_db_path:
            self._json(
                {"error": f"Unknown agent: {agent_param!r}", "agent": agent_param},
                status=404,
            )
            return

        data_dir = os.path.dirname(page_db_path) if page_db_path else None
        footer_html = footer.render_footer(data_dir)
        with open(_WORKBENCH_TEMPLATE_PATH, encoding="utf-8") as f:
            template = f.read()
        # Cache-busting: rewrite the two workbench script tags to carry
        # `?v=<stamp>` (computed once at import time -- see
        # _compute_static_version_stamp above) so a same-hour redeploy is
        # visible to a browser holding static_serve's 1h Cache-Control.
        # health-detail-math.js (the third script tag on this page) is
        # deliberately NOT rewritten -- same scope this cache-busting
        # mechanism always had (the two workbench-specific files only).
        page = (
            template
            .replace(shell._FOOTER_PLACEHOLDER, footer_html)
            .replace(
                'src="/static/health-workbench-curve.js"',
                f'src="/static/health-workbench-curve.js?v={_STATIC_VERSION_STAMP}"',
            )
            .replace(
                'src="/static/health-workbench.js"',
                f'src="/static/health-workbench.js?v={_STATIC_VERSION_STAMP}"',
            )
        )
        self._html(page)

    def _json(self, data: dict, status: int = 200):
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ENGRAM Curve Workbench — standalone developer-tier "
                     "entry point for editing health-v2 curves/importance "
                     "(engram-alpha#2754). No systemd unit needed -- run "
                     "this yourself when you want it."
    )
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT,
                        help=f"Port to listen on (default: {_DEFAULT_PORT})")
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
                             "unchanged behavior). Mirrors viz.server's own flag "
                             "(engram-alpha#2767).")
    args = parser.parse_args()

    if args.access_log:
        global _ACCESS_LOG_PATH
        _ACCESS_LOG_PATH = os.path.expanduser(args.access_log)

    if args.config and args.db:
        parser.error("--config and --db are mutually exclusive")

    # Resolves AGENTS/DEFAULT_AGENT/etc from the SAME flags viz.server's own
    # main() uses, via the SAME shared helper -- "against the same operator
    # config" per the issue's own text.
    viz_agents.configure_agents_from_args(args)

    HTTPServer.allow_reuse_address = True
    bind = args.bind or "127.0.0.1"
    server = HTTPServer((bind, args.port), Handler)
    print(f"ENGRAM Curve Workbench →  http://localhost:{args.port}")
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
