"""#4447 slice A: D3 is served locally from an exact vendored version.

The old tag loaded a FLOATING remote script (d3js.org/d3.v7.min.js) with no
integrity check. A subresource-integrity hash on a floating URL breaks when d3
ships a new 7.x, so the file is vendored at an exact version instead.
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pytest

from viz import static_serve

_VIZ = Path(static_serve.__file__).resolve().parent
_TEMPLATES = _VIZ / "templates"
_STATIC = _VIZ / "static"

D3_NAME = "d3.v7.9.2.min.js"
D3_SHA256 = "912111098ea99503a6f4967361d2bbdc9dbf58cf9c678c8e27e5b02d1a07f0c1"
D3_BYTES = 280622

_TAG = re.compile(r"<(script|link)\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR = re.compile(r"""\b(src|href)\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _html_files():
    return sorted(list(_TEMPLATES.glob("*.html")) + list(_STATIC.glob("*.html")))


def _remote_tags():
    out = []
    for f in _html_files():
        for m in _TAG.finditer(f.read_text(encoding="utf-8")):
            tag = m.group(0)
            am = _ATTR.search(tag)
            if am and re.match(r"(https?:)?//", am.group(2), re.IGNORECASE):
                out.append((f.name, tag))
    return out


class _Handler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers[k] = v

    def end_headers(self):
        pass


def test_a_every_remote_script_or_link_has_integrity():
    assert _html_files(), "scan found no html files; the cell would be vacuous"
    missing = [
        (name, tag) for name, tag in _remote_tags()
        if not re.search(r"\bintegrity\s*=", tag, re.IGNORECASE)
    ]
    assert not missing, f"remote resources without integrity=: {missing}"


def test_b_graph_html_d3_is_local_and_exists():
    html = (_TEMPLATES / "graph.html").read_text(encoding="utf-8")
    srcs = [m.group(1) for m in re.finditer(
        r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']*d3[^"']*)["']""", html, re.IGNORECASE)]
    assert srcs == [f"/static/{D3_NAME}"], srcs
    assert (_STATIC / D3_NAME).is_file()


def test_c_vendored_file_is_the_verified_bytes():
    data = (_STATIC / D3_NAME).read_bytes()
    assert len(data) == D3_BYTES
    assert hashlib.sha256(data).hexdigest() == D3_SHA256
    assert "v7.9.2" in data.split(b"\n", 1)[0].decode()


@pytest.mark.parametrize("name", [D3_NAME, "d3.LICENSE.txt"])
def test_d_served_through_static_serve(name):
    h = _Handler()
    static_serve.serve_static(h, name)
    assert h.status == 200
    assert len(h.wfile.getvalue()) > 0
    if name.endswith(".js"):
        assert "javascript" in h.headers["Content-Type"]
    else:
        assert b"Permission to use, copy, modify" in h.wfile.getvalue()
        assert h.headers["Content-Type"] == "text/plain; charset=utf-8"


def test_e_graph_js_uses_global_d3_and_d3_loads_first():
    assert re.search(r"\bd3\.", (_STATIC / "graph.js").read_text(encoding="utf-8"))
    html = (_TEMPLATES / "graph.html").read_text(encoding="utf-8")
    assert html.index(f"/static/{D3_NAME}") < html.index("/static/graph.js")
