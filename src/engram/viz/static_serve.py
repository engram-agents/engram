"""viz.static_serve — shared ``/static/<filename>`` file-serving logic
(engram-alpha#2754 PR B).

Extracted out of ``viz.server``'s original ``_serve_static`` method +
``_STATIC_DIR``/``_STATIC_CONTENT_TYPES``/``_STATIC_CACHE_CONTROL`` module
constants so BOTH ``viz.server`` (the main ``:5001`` dashboard) and
``viz.workbench`` (the new ``:5021`` Curve Workbench entry point, which
moved off ``:5001`` in this same PR) serve the identical, single flat
``viz/static/`` directory through ONE code path — rather than two
hand-copied copies of the same path-traversal guard drifting apart over
time. Neither caller's behavior changes: this module is a pure relocation
of ``viz.server``'s pre-existing logic, byte-identical in its checks and
its response shape.

No HTTP server of its own here — ``serve_static`` takes whatever
``BaseHTTPRequestHandler``-shaped object the caller is already mid-request
on (anything exposing ``send_response``/``send_header``/``end_headers``/
``wfile.write``, which every stdlib ``http.server.BaseHTTPRequestHandler``
subclass does) and writes the response directly onto it, the same way the
original method did via ``self``.
"""

from __future__ import annotations

from pathlib import Path

# static/ is a FLAT directory (base.css, nav.js, graph.css, graph.js,
# graph-agent-select.js, health-detail-math.js, health-workbench.js,
# health-workbench-curve.js, and PR A's health-detail.js) -- no
# subdirectories are ever a legitimate request, which is what makes the
# path-traversal guard below simple to get right: reject any filename
# containing a path separator or '..' outright, rather than trying to
# canonicalize-and-compare against arbitrarily nested input.
_STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()

_STATIC_CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
}

# These are static assets bundled with the plugin (not per-agent dynamic
# content) -- a 1-hour cache is a reasonable default; nothing here changes
# without a plugin upgrade, which is a process restart anyway.
_STATIC_CACHE_CONTROL = "public, max-age=3600"


def serve_static(handler, filename: str) -> None:
    """Serve one file from ``viz/static/`` onto *handler*.

    *filename* is everything after ``"/static/"`` in the request path --
    NOT yet validated. *handler* is the live request-handling object
    (``self`` from the caller's ``do_GET``) whose ``send_response`` /
    ``send_header`` / ``end_headers`` / ``wfile.write`` this function calls
    directly, exactly as the original in-``Handler`` method did.

    Path-traversal guard: static/ is a flat directory, so any filename
    containing a path separator or a '..' component is rejected outright
    as 404, rather than attempting to canonicalize-and-compare (the
    flat-directory invariant makes the reject-outright form both simpler
    and strictly more conservative than that would be).
    """
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        handler.send_response(404)
        # Reviewer-fairy catch on PR-2926 (2026-09-14): Content-Length: 0 is
        # required so a keep-alive HTTP/1.1 client doesn't hang waiting for a
        # body that never comes -- see server.py's do_GET catch-all 404 for
        # the full explanation (same fix, same root cause, three call sites).
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return

    target = _STATIC_DIR / filename
    if not target.is_file():
        handler.send_response(404)
        # Reviewer-fairy catch on PR-2926 (2026-09-14): Content-Length: 0 is
        # required so a keep-alive HTTP/1.1 client doesn't hang waiting for a
        # body that never comes -- see server.py's do_GET catch-all 404 for
        # the full explanation (same fix, same root cause, three call sites).
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    # Belt-and-suspenders: confirm the resolved path is still directly
    # inside _STATIC_DIR (guards against the target itself being a
    # symlink that escapes static/, which the string-level checks above
    # don't see).
    if target.resolve().parent != _STATIC_DIR:
        handler.send_response(404)
        # Reviewer-fairy catch on PR-2926 (2026-09-14): Content-Length: 0 is
        # required so a keep-alive HTTP/1.1 client doesn't hang waiting for a
        # body that never comes -- see server.py's do_GET catch-all 404 for
        # the full explanation (same fix, same root cause, three call sites).
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return

    content_type = _STATIC_CONTENT_TYPES.get(target.suffix, "application/octet-stream")
    body = target.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", _STATIC_CACHE_CONTROL)
    handler.end_headers()
    handler.wfile.write(body)
