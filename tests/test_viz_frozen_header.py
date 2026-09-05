"""Tests for the frozen-header typography fix (engram-alpha#2555 PR C,
forum #389's final ruling, superseding the wrapper-div fallback).

The bug: nav/footer chrome font-size was set in ``em`` against an inherited
``body`` font-size that differed silently across the four pages — three left
it at the browser's implicit 16px default, graph.html pinned its own 13px —
so byte-identical ``0.9em`` text rendered at two different pixel sizes. The
fix: one shared ``body{}`` baseline in ``static/base.css``, chrome selectors
converted to absolute px (which by construction can no longer depend on
inherited context), and a static gate so a future ``body{}``/``html{}`` rule
anywhere outside ``base.css`` can only ever carry LAYOUT properties.

Covers:
  1. The static whitelist gate itself, as data (the same literal list the
     real check uses) plus a deliberately-broken fixture proving the gate
     actually fires rather than vacuously passing.
  2. Chrome-selector uniqueness: base.css is the SOLE definition of
     .nav/.nav a/.nav-agent-label/.nav-agent-select/#nav-agent-wrapper/
     .viz-footer* across every CSS/template source (the PR C sweep found
     the agent-select trio quietly duplicated 4x, dead weight PR B's own
     consolidation should have caught).
  3. No relative font-size unit survives in base.css's chrome rules.
  4. Render gate (forum #389's fallback form — no headless browser available
     on this seat, see PR body for the explicit statement of which form
     ran): live-server fetch of all 4 real routes + the two served CSS
     files, re-running gates 1-3 against what the server ACTUALLY serves,
     not just the source tree.
"""
from __future__ import annotations

import re
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import server as viz_server  # noqa: E402

_VIZ_DIR = Path(__file__).resolve().parent.parent / "src" / "engram" / "viz"
_BASE_CSS = _VIZ_DIR / "static" / "base.css"
_GRAPH_CSS = _VIZ_DIR / "static" / "graph.css"
_TEMPLATES = {
    "health": _VIZ_DIR / "templates" / "health.html",
    "stats": _VIZ_DIR / "templates" / "stats.html",
    "config": _VIZ_DIR / "templates" / "config.html",
    "graph": _VIZ_DIR / "templates" / "graph.html",
}

# The whitelist itself, as DATA (forum #389's requirement): a
# body{}/html{} rule outside base.css may contain ONLY these properties.
# A future exception is then a visible one-line diff+review, not a prose
# rule someone has to remember to consult.
_ALLOWED_BODY_HTML_PROPS = frozenset({
    "display", "flex-direction", "flex-wrap",
    "height", "min-height", "overflow", "margin", "padding",
})

# Chrome selectors that must have exactly ONE definition, in base.css.
_CHROME_SELECTORS = (
    ".nav", ".nav a", ".nav a:hover",
    ".nav-agent-label", ".nav-agent-select", "#nav-agent-wrapper",
    ".viz-footer", ".viz-footer-product", ".viz-footer-provenance",
    ".viz-footer-disclaimer",
)


def _find_rule_blocks(css_text: str, selector_pattern: str) -> list[str]:
    """Return the ``{ ... }`` body of every rule whose selector matches
    *selector_pattern* (a regex fragment, anchored per-rule). Selectors are
    matched literally (not partial-word) — e.g. ``body`` doesn't match
    ``.nav-agent-label`` — since CSS selector syntax has no word-boundary
    concept the way identifiers do; we anchor on the characters that
    delimit a selector in this codebase's hand-written CSS: start-of-line-
    or-``{``/``}``/``;`` before, whitespace-or-``{`` after.
    """
    # A rule is "<selector list> { <body> }" where <selector list> is
    # whatever precedes the first unmatched "{". We scan block-by-block
    # rather than a single top-level regex so a multi-line selector list
    # (this file has some) is handled correctly.
    css_text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
    blocks = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css_text):
        selectors = m.group(1)
        body = m.group(2)
        # split multi-selector rules "a, b { ... }" (none in this codebase
        # today, but don't silently mis-scope if one appears)
        parts = [s.strip() for s in selectors.split(",")]
        if any(re.fullmatch(re.escape(selector_pattern), p) for p in parts):
            blocks.append(body)
    return blocks


def _properties_in(rule_body: str) -> set[str]:
    """Extract bare property names ("font-size", not "font-size: 12px;")
    from a rule body."""
    return {
        decl.split(":", 1)[0].strip()
        for decl in rule_body.split(";")
        if decl.strip() and ":" in decl
    }


def _extract_inline_style(html_text: str) -> str:
    m = re.search(r"<style>(.*?)</style>", html_text, re.DOTALL)
    return m.group(1) if m else ""


def _non_base_css_sources() -> dict[str, str]:
    """Every stylesheet source EXCEPT base.css itself — the static gate's
    scope. Templates' inline <style> blocks count; base.css is excluded by
    definition (it's the one place the whitelist doesn't apply)."""
    sources = {"graph.css": _GRAPH_CSS.read_text(encoding="utf-8")}
    for name, path in _TEMPLATES.items():
        sources[f"{name}.html"] = _extract_inline_style(
            path.read_text(encoding="utf-8"))
    return sources


# ---------------------------------------------------------------------------
# 1. Static whitelist gate
# ---------------------------------------------------------------------------

class TestStaticWhitelistGate(unittest.TestCase):
    def test_every_body_html_rule_outside_base_css_is_whitelisted(self):
        for source_name, css_text in _non_base_css_sources().items():
            for selector in ("body", "html"):
                for body in _find_rule_blocks(css_text, selector):
                    props = _properties_in(body)
                    disallowed = props - _ALLOWED_BODY_HTML_PROPS
                    self.assertFalse(
                        disallowed,
                        f"{source_name}'s {selector}{{}} rule carries "
                        f"non-whitelisted propert(y/ies) {disallowed} -- "
                        f"only {sorted(_ALLOWED_BODY_HTML_PROPS)} are "
                        f"allowed outside base.css (forum #389)")

    def test_graph_css_body_rule_is_exactly_the_expected_layout_set(self):
        """Not just "no violations" — pin the actual surviving set, so a
        FUTURE addition of a new (even whitelisted) property is a visible
        diff in this test rather than silently accepted."""
        bodies = _find_rule_blocks(_GRAPH_CSS.read_text(encoding="utf-8"), "body")
        self.assertEqual(len(bodies), 1, "expected exactly one body{} rule in graph.css")
        self.assertEqual(_properties_in(bodies[0]),
                          {"display", "flex-direction", "height", "overflow"})

    def test_whitelist_gate_is_real_falsifier(self):
        """Prove the gate actually fires: a deliberately-broken fixture
        carrying a non-whitelisted property on a body{} rule outside
        base.css must be REJECTED by the same check used above. Without
        this, test_every_body_html_rule_outside_base_css_is_whitelisted
        could pass vacuously (e.g. from a selector-matching bug that finds
        no rules at all)."""
        broken_css = "body { display: flex; font-size: 20px; }"
        bodies = _find_rule_blocks(broken_css, "body")
        self.assertEqual(len(bodies), 1, "fixture setup itself is broken")
        disallowed = _properties_in(bodies[0]) - _ALLOWED_BODY_HTML_PROPS
        self.assertEqual(disallowed, {"font-size"},
                          "the whitelist check failed to flag a "
                          "deliberately-injected font-size on body{} -- "
                          "the gate above would pass even on real breakage")


# ---------------------------------------------------------------------------
# 2. Chrome-selector uniqueness (the PR C sweep's own finding)
# ---------------------------------------------------------------------------

class TestChromeSelectorUniqueness(unittest.TestCase):
    def test_chrome_selectors_defined_only_in_base_css(self):
        base_css_text = _BASE_CSS.read_text(encoding="utf-8")
        for selector in _CHROME_SELECTORS:
            in_base = _find_rule_blocks(base_css_text, selector)
            self.assertEqual(
                len(in_base), 1,
                f"expected exactly one {selector!r} rule in base.css, "
                f"found {len(in_base)}")
            for source_name, css_text in _non_base_css_sources().items():
                elsewhere = _find_rule_blocks(css_text, selector)
                self.assertEqual(
                    elsewhere, [],
                    f"{selector!r} is ALSO defined in {source_name} -- "
                    f"a duplicate declared later in the cascade silently "
                    f"shadows base.css's fix for that page (this is "
                    f"exactly how .nav-agent-label/.nav-agent-select/"
                    f"#nav-agent-wrapper survived PR B's own nav-CSS "
                    f"consolidation undetected)")


# ---------------------------------------------------------------------------
# 3. No relative font-size unit in base.css's chrome rules
# ---------------------------------------------------------------------------

class TestChromeFontSizeIsAbsolute(unittest.TestCase):
    def test_no_em_rem_percent_font_size_in_chrome_selectors(self):
        base_css_text = _BASE_CSS.read_text(encoding="utf-8")
        for selector in (".nav a", ".nav-agent-label", ".nav-agent-select",
                         ".viz-footer"):
            for body in _find_rule_blocks(base_css_text, selector):
                m = re.search(r"font-size:\s*([0-9.]+)(em|rem|%)", body)
                self.assertIsNone(
                    m, f"{selector} still has a relative font-size unit "
                       f"({m.group(0) if m else ''}) -- relative units "
                       f"are exactly the mechanism of the bug this PR "
                       f"fixes (identical text, different inherited "
                       f"context, different rendered pixels)")


# ---------------------------------------------------------------------------
# 4. Render gate — forum #389's fallback form (no headless browser on this
#    seat; see PR body for the explicit statement of which form ran).
# ---------------------------------------------------------------------------

class _LiveServerBase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path: str) -> str:
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return resp.read().decode()


class TestLiveServerRenderGate(_LiveServerBase):
    """Re-runs gates 1-3 against what the server actually serves over
    HTTP, not just the source tree on disk — the served bytes are what a
    real browser (or a real headless-browser computed-style check, when
    one is available) would actually render."""

    def test_served_base_css_is_the_sole_source_of_chrome_rules(self):
        served_base = self._get("/static/base.css")
        served_graph_css = self._get("/static/graph.css")
        for selector in _CHROME_SELECTORS:
            self.assertEqual(len(_find_rule_blocks(served_base, selector)), 1)
            self.assertEqual(_find_rule_blocks(served_graph_css, selector), [])
        for path in ("/", "/health", "/stats", "/config"):
            page_html = self._get(path)
            inline_style = _extract_inline_style(page_html)
            for selector in _CHROME_SELECTORS:
                self.assertEqual(
                    _find_rule_blocks(inline_style, selector), [],
                    f"{path}'s served page carries its own {selector!r} "
                    f"rule, which would shadow base.css's fix for that "
                    f"page specifically")

    def test_served_chrome_font_sizes_identical_across_all_four_pages(self):
        """The render-gate proper: since the fix makes chrome font-size an
        ABSOLUTE unit (px), its computed value is independent of any
        inherited body font-size by construction -- so confirming (a) the
        served CSS declares one absolute px value and (b) no OTHER RULE
        USING THE SAME SELECTOR STRING overrides it (gate above) covers the
        common case (a page re-declaring `.nav a` directly). It does NOT
        resolve full CSS cascade/specificity order: a rule with a more
        specific selector targeting the same element (e.g. `body .nav a`)
        would win the cascade over `.nav a` regardless of source order and
        reintroduce the exact relative-unit bug this PR fixes, undetected by
        this gate -- Ariadne, PR #2572 review (`_find_rule_blocks` matches on
        exact selector-string equality per selector, not resolved
        specificity). An actual headless-browser computed-style read is the
        only check that resolves specificity for real (per forum #389);
        this cascade-math approximation is a narrower, cheaper stand-in for
        the identical-selector case, not its equivalent."""
        served_base = self._get("/static/base.css")
        for selector, prop in ((".nav a", "font-size"), (".viz-footer", "font-size")):
            bodies = _find_rule_blocks(served_base, selector)
            self.assertEqual(len(bodies), 1)
            m = re.search(rf"{prop}:\s*([0-9.]+)px", bodies[0])
            self.assertIsNotNone(m, f"{selector}'s {prop} isn't an absolute px value")
        # And every page actually links base.css before any page-local
        # <style> block, which is what makes "no local override" (checked
        # above) sufficient rather than a coincidence of ordering.
        for path in ("/", "/health", "/stats", "/config"):
            page_html = self._get(path)
            base_link_pos = page_html.find('href="/static/base.css"')
            style_pos = page_html.find("<style>")
            self.assertNotEqual(base_link_pos, -1, f"{path} doesn't link base.css")
            if style_pos != -1:
                self.assertLess(
                    base_link_pos, style_pos,
                    f"{path} links base.css AFTER its own <style> block -- "
                    f"the cascade-order assumption this gate relies on "
                    f"doesn't hold for this page")


if __name__ == "__main__":
    unittest.main()
