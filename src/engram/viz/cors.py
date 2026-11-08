"""viz.cors -- the ONE same-origin policy for viz.server and viz.workbench
(engram-alpha#4441, part of #4439).

Both HTTP handlers mix in :class:`SameOriginGuard`. Three rules live here and nowhere else:

* NO WILDCARD, ever. A response carries ``Access-Control-Allow-Origin`` only as the exact
  request ``Origin`` echoed back (with ``Vary: Origin``), and only when that Origin is in the
  allowed set A. ``Access-Control-Allow-Credentials`` is never sent.
* A = {http://127.0.0.1:<port>, http://localhost:<port>} + the configured ``viz.allowed_origins``.
  Every comparison is EXACT string equality (or, for a Referer, equality or ``A_element + "/"``
  as a string prefix): no case folding, no trailing-slash tolerance, no URL parsing. Parsing is
  what lets ``http://evil@127.0.0.1:<port>`` or ``127.0.0.1:<port>.evil.example`` through.
* The Host header must name the origin the request was addressed to (DNS-rebinding defence):
  H = {127.0.0.1:<port>, localhost:<port>} + the authority part of each configured origin.
  The check runs in ``parse_request``, i.e. once, before ANY ``do_<VERB>``, for every verb and
  every path kind. It checks the Host HEADER, not the request-target: an absolute-form
  ``GET http://evil.example/`` with a valid Host is served (no browser sends one, so it is not a
  rebinding path). ``[::1]`` is refused on purpose: this server can only bind IPv4 addresses (stock
  ``ThreadingHTTPServer`` is AF_INET; ``--bind ::`` fails with gaierror), so that name never reaches it.

``?agent=`` stays a TRUSTED selector: a PUT that proves same origin may write any agent's config. It is a
capability granted to the viz page, not an authorization check (this server has no identity to check it
against); it is safe only while the page at the admitted origin is ours (see #4459).

``viz.allowed_origins`` is read ONCE at start-up from the serving process's own config
($ENGRAM_HOME/config.json), never from a ``?agent=`` agent's config: an agent must not be able
to widen the allowlist of the viz server that serves it. Entries are LITERAL origins, never
patterns. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re

try:  # flat runtime module: installed plugin / src/engram on sys.path (same pair viz/agents.py uses)
    from engram_home_resolver import engram_home_or_none
except ImportError:  # source checkout with only `src` on PYTHONPATH
    from engram.engram_home_resolver import engram_home_or_none

# scheme://host[:port], http or https, a non-empty host, nothing else (no path, query, fragment,
# userinfo, trailing slash or '*'). fullmatch: a trailing newline must not slip past `$`.
_ORIGIN_ENTRY_RE = re.compile(
    r"(?:http|https)://(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?"
)

PREFLIGHT_METHODS = "GET, PUT, OPTIONS"
PREFLIGHT_HEADERS = "Content-Type, X-Engram-Viz"
_HOST_ERROR_MAX = 200

_CONFIGURED_ORIGINS: list = []


def is_valid_origin_entry(entry) -> bool:
    """True iff *entry* is a str that is exactly ``scheme://host[:port]`` (http/https)."""
    return isinstance(entry, str) and _ORIGIN_ENTRY_RE.fullmatch(entry) is not None


def set_configured_origins(origins) -> list:
    """Install the configured origins (invalid entries are dropped, order kept, duplicates removed)."""
    global _CONFIGURED_ORIGINS
    seen: list = []
    for o in origins or []:
        if is_valid_origin_entry(o) and o not in seen:
            seen.append(o)
    _CONFIGURED_ORIGINS = seen
    return list(seen)


def configured_origins() -> list:
    return list(_CONFIGURED_ORIGINS)


def load_configured_origins() -> list:
    """Read ``viz.allowed_origins`` from the serving process's own config and install it.

    Called once by each entry point's ``main()``. Never raises: a missing or malformed file, or
    a value that is not a list, gives ``[]``. Entries that fail :func:`is_valid_origin_entry`
    (a hand-edited file bypasses the write-time validation) are dropped.
    """
    origins: list = []
    try:
        home = engram_home_or_none()
        if home is not None:
            with open(os.path.join(str(home), "config.json"), "r", encoding="utf-8") as f:
                doc = json.load(f)
            viz = doc.get("viz") if isinstance(doc, dict) else None
            value = viz.get("allowed_origins") if isinstance(viz, dict) else None
            if isinstance(value, list):
                origins = value
    except Exception:
        origins = []
    return set_configured_origins(origins)


def allowed_origins(port: int) -> list:
    return [f"http://127.0.0.1:{port}", f"http://localhost:{port}"] + list(_CONFIGURED_ORIGINS)


def allowed_hosts(port: int) -> list:
    return [f"127.0.0.1:{port}", f"localhost:{port}"] + [o.split("://", 1)[1] for o in _CONFIGURED_ORIGINS]


def send_refusal(handler, status: int, message: str) -> None:
    """A JSON ``{"ok": false, "error": message}`` refusal. Never any Access-Control-* header,
    always a Content-Length (the viz handler speaks HTTP/1.1; the workbench's HTTP/1.0 is delimited by the
    close), and the connection is closed: the request body (if any) was deliberately not read."""
    body = json.dumps({"ok": False, "error": message}).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


class SameOriginGuard:
    """Mixin for a ``BaseHTTPRequestHandler`` subclass (listed BEFORE it in the bases)."""

    def _cors_port(self) -> int:
        return self.server.server_address[1]

    # -- the Host rule: one place, before any do_<VERB> -------------------------------------
    def _host_refusal(self):
        """None when exactly one Host header names this server, else the 403 message."""
        hosts = self.headers.get_all("Host")
        if hosts is not None and len(hosts) == 1 and hosts[0] in allowed_hosts(self._cors_port()):
            return None
        value = "(missing)" if hosts is None else hosts[0][:_HOST_ERROR_MAX]
        return f"Host not allowed: {value}; add it to viz.allowed_origins"

    def parse_request(self):
        if not super().parse_request():
            return False
        message = self._host_refusal()
        if message is not None:
            send_refusal(self, 403, message)
            return False
        return True

    def handle_expect_100(self):
        """``super().parse_request()`` answers ``Expect: 100-continue`` BEFORE the Host check above and
        before any ``do_<VERB>``, which would invite the body of a request we are about to refuse. So the
        Host rule and a PUT's same-origin proofs run here first; a refused request gets its 403, never a 100."""
        message = self._host_refusal()
        if message is None and self.command == "PUT":
            message = self.put_refusal_reason()
        if message is not None:
            send_refusal(self, 403, message)
            return False
        return super().handle_expect_100()

    # -- Origin ------------------------------------------------------------------------------
    def cors_allowed_origin(self):
        """The request Origin if there is exactly one Origin header and it is in A, else None."""
        origins = self.headers.get_all("Origin")
        if origins is not None and len(origins) == 1 and origins[0] in allowed_origins(self._cors_port()):
            return origins[0]
        return None

    def send_cors_headers(self) -> None:
        """Echo an allowed Origin (never '*'); nothing at all for a foreign, odd or absent one."""
        origin = self.cors_allowed_origin()
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def handle_preflight(self) -> None:
        origins = self.headers.get_all("Origin")
        if origins is None:  # not a CORS preflight: answer as a plain OPTIONS, with no CORS headers
            self.send_response(204)
            self.end_headers()
            return
        origin = self.cors_allowed_origin()
        if origin is None:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", PREFLIGHT_METHODS)
        self.send_header("Access-Control-Allow-Headers", PREFLIGHT_HEADERS)
        self.send_header("Vary", "Origin")
        self.end_headers()

    # -- PUT proves same origin --------------------------------------------------------------
    def put_refusal_reason(self):
        """None when the PUT proves same origin, else the reason it is refused. Reads headers only.

        ``Sec-Fetch-Site`` is a browser-declared header: it adds nothing against a non-browser client
        that sets it. The ``X-Engram-Viz`` plus ``Origin`` pair is the load-bearing half; the clause is
        defence in depth on the browser path (it also covers a PUT that arrives with no Origin).
        """
        if self.headers.get_all("X-Engram-Viz") != ["1"]:
            return "X-Engram-Viz: 1 header required"
        sfs = self.headers.get_all("Sec-Fetch-Site")
        if sfs is not None and sfs != ["same-origin"]:
            return "Sec-Fetch-Site must be same-origin"
        allowed = allowed_origins(self._cors_port())
        origins = self.headers.get_all("Origin")
        if origins is not None:
            if len(origins) != 1 or origins[0] not in allowed:
                return "Origin not allowed"
            return None
        referers = self.headers.get_all("Referer")
        if referers is None or len(referers) != 1:
            return "Origin or Referer of this server required"
        ref = referers[0]
        if any(ref == a or ref.startswith(a + "/") for a in allowed):
            return None
        return "Referer not allowed"
