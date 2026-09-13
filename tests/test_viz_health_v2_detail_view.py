"""Tests for the health-v2 DETAIL VIEW's Curve-Workbench-fork slice
(engram-alpha#2754 PR A -- read-only fork of health-workbench.js).

Scope: this is the frontend slice that CONSUMES `/api/health/v2`,
`/api/health/v2/series`, and (optionally, as an enrichment layer)
`/api/health/v2/model` -- all three endpoints and their backend loaders
(`viz/data/health_v2.py`, `viz/data/health_workbench.py`, `health/series.py`)
are already built and covered by `tests/test_viz_health_v2.py` /
`tests/test_viz_health_workbench.py`; nothing here re-tests them.

Before #2754, the Detail view's render logic lived entirely INSIDE
health.html's inline `<script>` (plain string-built HTML, no DOM API), which
let this file extract and run `renderHistoryStrip` etc. directly under
`node`. #2754 PR A forked the Curve Workbench (health-workbench.js) instead,
which builds its DOM via the real `document.createElementNS`/`.appendChild`
API -- exactly like health-workbench.js's own render logic, which
`tests/test_viz_health_workbench.py`'s own module docstring explicitly
excludes from node-executed testing for that reason ("the page's client-side
JS render logic" is "out of scope for THIS file", covered only by the pure-
math parity tests + static string assertions). health-detail.js followed the same convention through #2754 PR A: its
DOM-touching functions were covered only by static string/structure
assertions on the served page + the served static file, never by
extracting-and-running them under plain `node` (no jsdom on this seat) --
the pure math it calls (piecewiseLinear, collapseUnavailableRuns,
computeDistributionStats, ...) is covered, unchanged, by
tests/test_health_workbench_curve_parity.py, tests/test_health_workbench_
round5_parity.py, and the TestDistributionMathViaNode class below.

engram-alpha#2868 (the three-column-grid fork fix) is the ONE exception to
that convention, and deliberately so: the defect it fixes (everything
stacked in one column instead of the workbench's three-panel grid) is
exactly the kind of layout-SHAPE bug that a string/structure assertion on
source text cannot see -- the pre-fix source already contained every
correct string a naive check would look for, just arranged in the wrong
DOM shape. TestDetailViewRealDOMStructure below evaluates health-detail.js
(and, as a control, health-workbench.js) in a hand-rolled `vm`-based DOM
shim -- no jsdom dependency added -- and inspects the ACTUAL constructed
element tree. This is narrowly scoped to the one class of bug static
assertions structurally cannot catch; it is not a general reversal of the
no-jsdom convention, and new tests for this file should still default to
the static-assertion style above unless they're testing DOM shape.

Same `_LiveServerBase` convention as `tests/test_viz_health_view_toggle.py`
and `tests/test_viz_health_v2.py` (real `HTTPServer` thread + `urlopen`, no
headless browser available on this seat).
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import threading
import unittest
from collections import namedtuple
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viz import server as viz_server  # noqa: E402

_MATH_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-detail-math.js"
)
_DETAIL_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-detail.js"
)
_CURVE_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-workbench-curve.js"
)
_WORKBENCH_JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "viz"
    / "static"
    / "health-workbench.js"
)

_NODE_AVAILABLE = shutil.which("node") is not None

# Minimal, hand-rolled DOM shim + fixture-driven harness used by
# TestDetailViewRealDOMStructure below -- see that class's own docstring for
# why this exists (no jsdom on this seat, and static string assertions on
# source can't tell "the DOM is a single column" from "the DOM is three
# columns"). Placeholders (__MATH_PATH__ etc.) are substituted with
# json.dumps'd absolute paths at test-run time -- see _run_harness().
_DOM_HARNESS_JS = r"""
(function () {
  "use strict";
  var fs = require("fs");
  var vm = require("vm");

  // ---- Minimal fake DOM -----------------------------------------------
  var idMap = {};

  function FakeElement(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this._className = "";
    this._innerHTML = "";
    this._textContent = undefined;
    this.disabled = false;
    this.value = undefined;
  }
  FakeElement.prototype.setAttribute = function (k, v) {
    v = String(v);
    this.attributes[k] = v;
    if (k === "id") idMap[v] = this;
    if (k === "class") this._className = v;
    if (k.indexOf("data-") === 0) {
      var camel = k.slice(5).replace(/-([a-z0-9])/g, function (m, c) { return c.toUpperCase(); });
      this.dataset[camel] = v;
    }
    if (k === "disabled") this.disabled = true;
  };
  FakeElement.prototype.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
  };
  FakeElement.prototype.appendChild = function (c) {
    c.parentNode = this;
    this.children.push(c);
    return c;
  };
  FakeElement.prototype.addEventListener = function () {};
  FakeElement.prototype.removeEventListener = function () {};
  FakeElement.prototype.select = function () {};
  FakeElement.prototype.querySelector = function (sel) { return domQuerySelector(sel); };
  Object.defineProperty(FakeElement.prototype, "className", {
    get: function () { return this._className; },
    set: function (v) { this._className = v; this.attributes["class"] = v; },
  });
  Object.defineProperty(FakeElement.prototype, "innerHTML", {
    get: function () { return this._innerHTML; },
    set: function (v) { this._innerHTML = v; this.children = []; },
  });
  Object.defineProperty(FakeElement.prototype, "textContent", {
    get: function () {
      if (this._textContent !== undefined) return this._textContent;
      if (this.children.length === 0) return this._innerHTML || "";
      return this.children.map(function (c) { return c.textContent; }).join("");
    },
    set: function (v) { this._textContent = v; this.children = []; },
  });

  function domQuerySelector(sel) {
    if (sel.charAt(0) === "#") return idMap[sel.slice(1)] || null;
    return null; // only #id selectors are used anywhere in these two files
  }

  var fakeDocument = {
    _listeners: {},
    createElement: function (tag) { return new FakeElement(tag); },
    createElementNS: function (ns, tag) { return new FakeElement(tag); },
    querySelector: function (sel) { return domQuerySelector(sel); },
    addEventListener: function (type, cb) {
      (this._listeners[type] = this._listeners[type] || []).push(cb);
    },
    createTextNode: function (text) {
      var e = new FakeElement("#text");
      e._textContent = String(text);
      return e;
    },
    _dispatch: function (type) {
      (this._listeners[type] || []).slice().forEach(function (cb) { cb({}); });
    },
  };

  global.document = fakeDocument;
  global.window = { location: { href: "http://localhost/health", search: "" } };
  global.requestAnimationFrame = function () {}; // never invokes its callback -- see class docstring
  global.confirm = function () { return false; };

  // ---- Traversal helpers (self-inclusive) ------------------------------
  function forEachNode(root, fn) {
    if (!root) return;
    fn(root);
    (root.children || []).forEach(function (c) { forEachNode(c, fn); });
  }
  function hasClassToken(n, token) {
    return !!(n._className && n._className.split(/\s+/).indexOf(token) >= 0);
  }
  function findFirst(root, predicate) {
    var found = null;
    forEachNode(root, function (n) { if (!found && predicate(n)) found = n; });
    return found;
  }
  function countAll(root, predicate) {
    var c = 0;
    forEachNode(root, function (n) { if (predicate(n)) c++; });
    return c;
  }
  function findTag(root, tag) { return findFirst(root, function (n) { return n.tagName === tag; }); }
  function findClassToken(root, token) { return findFirst(root, function (n) { return hasClassToken(n, token); }); }
  // findAspectSectionByName: locate the `.detail-aspect-section` whose <h2>
  // textContent matches `name` -- used by the #2881 regression test below to
  // pull the SPECIFIC `zero_eligible_aspect` tile's rendered score text
  // (rather than just "the first one", which test_detail_aspect_head_is_
  // three_cell_grid already covers structurally).
  function findAspectSectionByName(root, name) {
    return findFirst(root, function (n) {
      if (!hasClassToken(n, "detail-aspect-section")) return false;
      var h2 = findTag(n, "H2");
      return !!(h2 && String(h2.textContent) === name);
    });
  }

  // ---- Editing-control probe --------------------------------------------
  var EXPORT_IDS = ["wb-export-section", "wb-export-json", "wb-export-refresh", "wb-export-download", "wb-export-copy", "wb-export-reset"];
  var TEXT_MARKERS = ["reset aspect", "reset this curve", "set as reference"];
  function probeEditingControls(root) {
    var hits = [];
    forEachNode(root, function (n) {
      if (Object.prototype.hasOwnProperty.call(n.attributes, "data-sig")) hits.push("data-sig::" + n.attributes["data-sig"]);
      if (Object.prototype.hasOwnProperty.call(n.attributes, "data-imp")) hits.push("data-imp::" + n.attributes["data-imp"]);
      if (n._className && /\bkbtn\b/.test(n._className)) hits.push("kbtn::" + n._className);
      var idAttr = n.attributes["id"];
      if (idAttr && EXPORT_IDS.indexOf(idAttr) >= 0) hits.push("id=" + idAttr + "::" + idAttr);
      if (n.tagName === "INPUT") hits.push("tag=INPUT::(input)");
      var txt = (n._textContent !== undefined ? String(n._textContent) : "").trim();
      if (txt && TEXT_MARKERS.indexOf(txt) >= 0) hits.push("text=" + txt + "::" + txt);
    });
    return hits;
  }

  // ---- Fixture data -------------------------------------------------
  // Five metrics across three aspects -- deliberately includes ALL FOUR
  // per-row branches health-detail.js/health-workbench.js distinguish:
  // (a) the aspect's current reference (quote_verification_rate),
  // (b) a non-held, non-reference metric -- the ONLY case that renders the
  //     workbench's "set as reference" BUTTON (git_versioning_rate),
  // (c) a held-out metric (url_resolvability_rate),
  // (d) a single-member aspect where the lone metric is its own reference
  //     (claim_bearing_rate),
  // plus (e) `zero_eligible_aspect` (#2881 regression fixture, ADDED by
  // this PR): a THIRD aspect whose only member is held-out (importance 0),
  // matching the real `provenance_integrity` bug's shape on a live seat --
  // zero ADMITTED (importance>0) members with data, so
  // aspectScoreForToday()'s model-recompute path finds `members.length ===
  // 0` and falls through to the raw aspect_tile fallback. This is a
  // DIFFERENT shape from (c)/`provenance_integrity` above, which has TWO
  // admitted, scored siblings alongside its held-out member and never
  // reaches the fallback branch at all -- that's precisely why 34 pre-
  // #2881 tests (including #2872's own held-out-aspect-member regression)
  // never caught this bug: none of them left an aspect with ZERO eligible
  // members. `zero_eligible_aspect`'s aspect_tile entry carries the same
  // string sentinel `"UNAVAILABLE"` the real server payload does (verified
  // via `curl /api/health/v2?agent=kepler` at spec-writing time) -- not
  // `null` -- which is exactly the value `typeof tile.score === "number"`
  // must reject.
  var CURVE_DEFS = { curve_pass_rate: { knots: [[0, 0], [0.5, 0.5], [1, 1]], input_domain: [0, 1], output_range: [0, 1] } };
  var METRICS = [
    { signal: "quote_verification_rate", aspect: "provenance_integrity", curve: "curve_pass_rate", importance: 1 },
    { signal: "git_versioning_rate", aspect: "provenance_integrity", curve: "curve_pass_rate", importance: 1 },
    { signal: "url_resolvability_rate", aspect: "provenance_integrity", curve: "curve_pass_rate", importance: 0 },
    { signal: "claim_bearing_rate", aspect: "claim_bearing_current", curve: "curve_pass_rate", importance: 2 },
    { signal: "zero_eligible_signal", aspect: "zero_eligible_aspect", curve: "curve_pass_rate", importance: 0 },
  ];
  var ASPECTS = [
    { name: "provenance_integrity", points: 40, reference_metric: "quote_verification_rate" },
    { name: "claim_bearing_current", points: 60, reference_metric: "claim_bearing_rate" },
    { name: "zero_eligible_aspect", points: 20, reference_metric: null },
  ];
  var MODEL_FIXTURE = { available: true, model_defs: { metrics: METRICS, aspects: ASPECTS }, curve_defs: CURVE_DEFS };
  var AGENTS_FIXTURE = { agents: [{ name: "sol", label: "Sol" }], default: "sol" };

  function signalRow(name, raw, score, status) {
    return {
      name: name,
      status: status,
      definition: {
        formula: name + "_verified / " + name + "_total",
        numerator: { key: name + "_num", label: name + "_num", value: 80 },
        denominator: { key: name + "_den", label: name + "_den", value: 100 },
      },
      today: { raw: raw, score: score, curve: null },
    };
  }
  var SNAPSHOT_FIXTURE = {
    has_snapshot: true,
    provenance: {
      snapshot_id: "snap-0001", turn: 42, checkpoint_mode: "manual",
      written_at: "2026-09-12T00:00:00Z", health_version: "v2",
    },
    signal_rows: [
      signalRow("quote_verification_rate", 0.8, 0.8, "scored"),
      signalRow("git_versioning_rate", 0.7, 0.7, "scored"),
      signalRow("url_resolvability_rate", 0.6, null, "held_out"),
      signalRow("claim_bearing_rate", 0.9, 0.9, "scored"),
      signalRow("zero_eligible_signal", 0.5, null, "held_out"),
    ],
    aspect_tile: [
      { name: "provenance_integrity", score: 0.8, unscored: false },
      { name: "claim_bearing_current", score: 0.9, unscored: false },
      // #2881 regression fixture -- the real server shape for an aspect
      // with zero eligible (importance>0, has-data) members: `score` is
      // the STRING sentinel "UNAVAILABLE", not `null`, with `unscored:
      // true`. See this file's own comment on `zero_eligible_aspect` above.
      { name: "zero_eligible_aspect", score: "UNAVAILABLE", unscored: true },
    ],
    composed: { value: 86.0, composed_coverage: 1.0, unscored_aspects: [] },
  };
  function seriesPoints(base) {
    return [
      { t: "2026-09-10T00:00:00Z", raw: base - 0.05, score: base - 0.05, unreplayable: null },
      { t: "2026-09-11T00:00:00Z", raw: base, score: base, unreplayable: null },
    ];
  }
  var ALL_SIGNALS = {
    quote_verification_rate: seriesPoints(0.8),
    git_versioning_rate: seriesPoints(0.7),
    url_resolvability_rate: seriesPoints(0.6),
    claim_bearing_rate: seriesPoints(0.9),
    zero_eligible_signal: seriesPoints(0.5),
  };
  var COMPOSED_SERIES = [
    { t: "2026-09-10T00:00:00Z", value: 82.0 },
    { t: "2026-09-11T00:00:00Z", value: 86.0 },
  ];
  var COMBINED_SERIES_FIXTURE = { signals: ALL_SIGNALS, composed: COMPOSED_SERIES, scores_available: true };
  function seriesForSignal(sig) {
    var out = {};
    out[sig] = ALL_SIGNALS[sig] || [];
    return { signals: out, composed: COMPOSED_SERIES, scores_available: true };
  }

  global.fetch = function (url) {
    var path = url.split("?")[0];
    var data;
    if (path === "/api/health/v2/model") data = MODEL_FIXTURE;
    else if (path === "/api/agents") data = AGENTS_FIXTURE;
    else if (path === "/api/health/v2/series") {
      var m = /signal=([^&]+)/.exec(url);
      data = m ? seriesForSignal(decodeURIComponent(m[1])) : COMBINED_SERIES_FIXTURE;
    } else if (path === "/api/health/v2") data = SNAPSHOT_FIXTURE;
    else return Promise.reject(new Error("unmocked fetch url: " + url));
    return Promise.resolve({ json: function () { return Promise.resolve(data); } });
  };

  function evalFile(path) {
    var code = fs.readFileSync(path, "utf8");
    vm.runInThisContext(code, { filename: path });
  }
  function flush(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms || 80); });
  }
  function makeContainer(id, tag) {
    var e = new FakeElement(tag || "div");
    e.setAttribute("id", id);
    return e;
  }

  async function main() {
    // ---- Detail side ----------------------------------------------------
    var detailComposedBlock = makeContainer("detail-composed-block");
    var detailAspectSections = makeContainer("detail-aspect-sections");
    makeContainer("detail-empty-state");
    makeContainer("detail-content");
    makeContainer("detail-scores-note");

    evalFile(__MATH_PATH__);
    evalFile(__CURVE_PATH__);
    evalFile(__DETAIL_PATH__);

    window.loadDetailView();
    await flush(100);

    var detailRoot = new FakeElement("div");
    detailRoot.appendChild(detailComposedBlock);
    detailRoot.appendChild(detailAspectSections);

    var signalGridEl = findClassToken(detailRoot, "detail-signal-grid");
    var stripTopEl = findClassToken(detailRoot, "detail-strip-top");
    var aspectHeadEl = findClassToken(detailRoot, "detail-aspect-head");

    var detailReport = {
      signalCardCount: countAll(detailRoot, function (n) { return hasClassToken(n, "detail-signal-row"); }),
      composedBlockRendered: detailComposedBlock.children.length > 0,
      aspectSectionCount: countAll(detailRoot, function (n) { return hasClassToken(n, "detail-aspect-section"); }),
      editingHits: probeEditingControls(detailRoot),
      firstSignalGrid: signalGridEl ? {
        className: signalGridEl._className,
        childCount: signalGridEl.children.length,
        leftHasH3: !!(signalGridEl.children[0] && findTag(signalGridEl.children[0], "H3")),
        midHasStrip: !!(signalGridEl.children[1] && findClassToken(signalGridEl.children[1], "detail-strip-dist-container")),
        rightHasCurveOrImportance: !!(signalGridEl.children[2] && (
          findClassToken(signalGridEl.children[2], "detail-plot-container") ||
          findClassToken(signalGridEl.children[2], "metric")
        )),
      } : null,
      composedStripTop: stripTopEl ? { childCount: stripTopEl.children.length } : null,
      composedHasDistStrip: !!findClassToken(detailComposedBlock, "detail-strip-dist-container"),
      // #2881: the disclaimer bar must be a DIRECT child of
      // #detail-composed-block, appended before `.detail-strip-top` --
      // verified by index position among detailComposedBlock's own
      // immediate children (not a deep search, which couldn't tell
      // "sibling before" from "nested inside").
      composedHasDisclaimerBar: (function () {
        return detailComposedBlock.children.some(function (c) { return hasClassToken(c, "detail-disclaimer-bar"); });
      })(),
      disclaimerBarBeforeStripTop: (function () {
        var disclaimerIdx = -1, stripTopIdx = -1;
        detailComposedBlock.children.forEach(function (c, i) {
          if (hasClassToken(c, "detail-disclaimer-bar")) disclaimerIdx = i;
          if (hasClassToken(c, "detail-strip-top")) stripTopIdx = i;
        });
        return disclaimerIdx >= 0 && stripTopIdx >= 0 && disclaimerIdx < stripTopIdx;
      })(),
      // Borges's #2872 post-merge finding: the fixture already carries a
      // held-out (importance:0) metric inside provenance_integrity, and
      // the code guards it twice (aspectScoreForToday's `importance <= 0`
      // skip, composedScoreForToday's null-check) -- but nothing asserted
      // the RENDERED text is a finite number rather than "NaN"/"--". Pull
      // the actual .detail-composed-value element's text so the Python
      // side can pin it.
      composedValueText: (function () {
        var el = findClassToken(detailComposedBlock, "detail-composed-value");
        return el ? String(el.textContent) : null;
      })(),
      firstAspectHead: aspectHeadEl ? { childCount: aspectHeadEl.children.length } : null,
      // #2881 regression: the SPECIFIC zero-eligible-members aspect's own
      // rendered `.detail-aspect-score` text (renderAspectGrouped's
      // `aScore === null ? "--" : Number(aScore).toFixed(3)`) -- pre-fix,
      // aspectScoreForToday() returns the raw string "UNAVAILABLE" for this
      // aspect, and `Number("UNAVAILABLE").toFixed(3)` renders the literal
      // text "NaN"; post-fix it renders "--".
      zeroEligibleAspectScoreText: (function () {
        var section = findAspectSectionByName(detailAspectSections, "zero_eligible_aspect");
        if (!section) return null;
        var scoreEl = findClassToken(section, "detail-aspect-score");
        return scoreEl ? String(scoreEl.textContent) : null;
      })(),
    };

    // ---- Workbench side (control) ---------------------------------------
    makeContainer("wb-agent", "select");
    makeContainer("wb-status", "span");
    makeContainer("wb-strip", "section");
    makeContainer("wb-composed", "span");
    makeContainer("wb-pip", "span");
    makeContainer("wb-bandmark", "i");
    makeContainer("wb-empty", "div");
    var wbAspects = makeContainer("wb-aspects", "div");
    var wbExportSection = makeContainer("wb-export-section", "section");
    wbExportSection.appendChild(makeContainer("wb-export-json", "textarea"));
    wbExportSection.appendChild(makeContainer("wb-export-refresh", "button"));
    wbExportSection.appendChild(makeContainer("wb-export-download", "button"));
    wbExportSection.appendChild(makeContainer("wb-export-copy", "button"));
    wbExportSection.appendChild(makeContainer("wb-export-reset", "button"));

    evalFile(__WORKBENCH_PATH__);
    document._dispatch("DOMContentLoaded");
    await flush(150);

    var wbRoot = new FakeElement("div");
    wbRoot.appendChild(wbAspects);
    wbRoot.appendChild(wbExportSection);

    var wbSignalRow = findClassToken(wbRoot, "signal");
    var workbenchReport = {
      signalRowCount: countAll(wbRoot, function (n) { return hasClassToken(n, "signal"); }),
      editingHits: probeEditingControls(wbRoot),
      firstSignalRow: wbSignalRow ? { childCount: wbSignalRow.children.length } : null,
    };

    process.stdout.write(JSON.stringify({ detail: detailReport, workbench: workbenchReport }));
  }

  main().catch(function (e) {
    process.stderr.write("HARNESS ERROR: " + (e && e.stack || e) + "\n");
    process.exit(1);
  });
})();
"""

# `.status`/`.headers` on an `http.client.HTTPResponse` are populated
# up-front and remain readable after close; `.read()` needs the still-open
# socket -- so the body must be drained INSIDE the `with` block, before the
# context manager's `__exit__` closes it. This tuple carries all three back
# out already-materialized, so callers can't accidentally call `.read()`
# on an already-closed response (which silently returns b"", not an error).
_Response = namedtuple("_Response", ["status", "headers", "body"])


class _LiveServerBase(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), viz_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()

    def _get(self, path: str) -> _Response:
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
            return _Response(resp.status, resp.headers, resp.read())

    def _get_text(self, path: str) -> str:
        return self._get(path).body.decode()


class TestDetailScriptIncludesPresentAndOrdered(_LiveServerBase):
    """health.html must load health-detail-math.js, then health-workbench-
    curve.js (the shared pure-math library, per #2754's "share only the
    pure math library, fork the page" ruling), then health-detail.js -- in
    that order, and all three BEFORE the page's own inline <script> (which
    synchronously calls the view-toggle's `applyView()`, whose Detail
    branch calls `loadDetailView()` -- defined in health-detail.js -- at
    page-load time when the initial view already resolves to 'detail')."""

    def test_three_script_tags_present_in_order(self):
        body = self._get_text("/health")
        idx_math = body.index('<script src="/static/health-detail-math.js"></script>')
        idx_curve = body.index('<script src="/static/health-workbench-curve.js"></script>')
        idx_detail = body.index('<script src="/static/health-detail.js"></script>')
        idx_inline = body.index("<script>")
        self.assertLess(idx_math, idx_curve)
        self.assertLess(idx_curve, idx_detail)
        self.assertLess(idx_detail, idx_inline)

    def test_detail_js_served_with_js_content_type(self):
        resp = self._get("/static/health-detail.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/javascript")

    def test_curve_js_served_with_js_content_type(self):
        """Sibling regression guard for the tiers.json retag this PR makes
        (dev -> convenience for health-workbench-curve.js): the static
        route itself doesn't consult tiers.json (that's a BUILD-time
        filter, not a request-time gate), so this only confirms the file is
        still served correctly from source -- the tier retag's actual
        purpose (surviving a --tier convenience build) is a packaging-time
        property, not something a live-server test can exercise."""
        resp = self._get("/static/health-workbench-curve.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/javascript")


class TestDetailJsFileContract(unittest.TestCase):
    """Static assertions on health-detail.js's own source -- the entry
    point contract, and that every editing affordance named in the issue's
    strip-list is actually gone."""

    def setUp(self):
        self.src = _DETAIL_JS_PATH.read_text(encoding="utf-8")

    def test_file_exists(self):
        self.assertTrue(_DETAIL_JS_PATH.is_file(), f"missing {_DETAIL_JS_PATH}")

    def test_exposes_global_entry_point(self):
        self.assertIn("window.loadDetailView", self.src)

    def test_wrapped_in_iife_no_leaked_globals(self):
        """health.html's inline <script> declares its OWN `render`/`el`-
        shaped globals for the Normal view -- an unscoped health-workbench.
        js-style helper set would silently collide. Confirm the fork stays
        wrapped -- stripping the leading `//`-comment block first (the
        module docstring), since the IIFE itself is the first STATEMENT,
        not necessarily the first line of the file."""
        body_without_leading_comments = re.sub(r"^(\s*//.*\n)+", "", self.src)
        self.assertRegex(body_without_leading_comments.strip(), r"^\(function \(\) \{")
        self.assertRegex(self.src.strip(), r"\}\)\(\);\s*$")

    def test_no_knot_drag_or_insert_ui(self):
        for gone in ("insertMidKnot", "delBtn", "plusBtn", "kbtn add"):
            self.assertNotIn(gone, self.src, f"editing affordance {gone!r} should be stripped")

    def test_no_importance_input_element(self):
        # The workbench's editable importance <input> -- replaced here by a
        # plain read-only <div class="metric"> readout (see
        # renderSignalCard).
        self.assertNotIn('"data-imp"', self.src)
        self.assertNotIn("validateImportance", self.src)

    def test_no_export_apply_reset(self):
        for gone in ("buildExportPayload", "refreshExportBox", "wb-export", "saveEdits", "loadEdits"):
            self.assertNotIn(gone, self.src, f"export/persistence affordance {gone!r} should be stripped")

    def test_no_set_as_reference_control_but_markers_survive(self):
        # No functional "set as reference" control (the button/click-handler
        # this fork removes) -- a comment MAY still mention the phrase
        # (documenting what health-workbench.js's own control looked like),
        # so this checks for the functional markers, not the bare string.
        self.assertNotIn("setReference(", self.src)
        self.assertNotIn("refBtn", self.src)
        self.assertIn("hold-out metric", self.src)
        self.assertIn("current reference", self.src)

    def test_no_changed_highlight_overlay(self):
        # curvesDiffer / the stored-vs-edited dashed overlay -- moot here
        # since nothing is ever edited.
        self.assertNotIn("curvesDiffer", self.src)
        self.assertNotIn('"changed"', self.src)

    def test_reuses_shared_math_functions_by_name(self):
        # These are NOT redeclared in health-detail.js -- they're globals
        # from health-workbench-curve.js, loaded first (see the script-
        # order test above). Referencing them by name here confirms the
        # fork didn't accidentally duplicate the math instead of sharing
        # it, per the issue's "share only the pure math library" ruling.
        for name in (
            "piecewiseLinear", "aspectScore", "composeScore",
            "collapseUnavailableRuns", "dominantReasonKey",
            "formatCheckpointLabel", "computeAxisTicks", "clamp",
        ):
            self.assertIn(name, self.src)

    def test_gracefully_degrades_when_model_unavailable(self):
        self.assertIn("modelAvailable", self.src)
        self.assertIn("renderFlat", self.src)

    def test_unscored_notice_names_version_and_remedy_in_both_branches(self):
        """engram-alpha#2896 item 2: #detail-scores-note must state the
        actual version + the "run derive" remedy, per #2869's own
        requested wording ("a one-line 'no derived rows for vX — run
        derive' notice") -- not just an explanation of the condition. Both
        the standalone `seriesUnscored` branch and the combined
        `!modelAvailable() && seriesUnscored` branch must carry it -- the
        old single-string version only had one."""
        # Old stale wording must not survive the fix.
        self.assertNotIn("score history unavailable for this model version", self.src)

        occurrences = self.src.count("no derived rows for")
        self.assertEqual(
            occurrences, 2,
            "expected the phrase in both the standalone and the combined "
            "seriesUnscored branches",
        )
        self.assertIn("run derive", self.src)
        self.assertIn("state.series.version", self.src)

        combined_idx = self.src.index("!modelAvailable() && seriesUnscored")
        standalone_idx = self.src.index("} else if (seriesUnscored) {")
        self.assertGreater(standalone_idx, combined_idx)

        combined_notice_idx = self.src.index("no derived rows for", combined_idx)
        standalone_notice_idx = self.src.index("no derived rows for", standalone_idx)

        # Each branch's notice sits close to its own `state.series.version`
        # interpolation -- genuinely inside that branch, not a stray
        # occurrence elsewhere in the file.
        combined_version_idx = self.src.index("state.series.version", combined_notice_idx)
        self.assertLess(combined_version_idx - combined_notice_idx, 100)

        standalone_version_idx = self.src.index("state.series.version", standalone_notice_idx)
        self.assertLess(standalone_version_idx - standalone_notice_idx, 100)

        # The combined branch must still carry the developer-tier-model
        # clause alongside the new version/remedy text -- not dropped.
        combined_end = self.src.index(";", combined_notice_idx)
        combined_text = self.src[combined_idx:combined_end]
        self.assertIn("developer-tier curve model isn't installed", combined_text)
        self.assertIn("showing raw values and server-reported scores only", combined_text)

        # The standalone branch still conveys "showing raw values".
        standalone_end = self.src.index(";", standalone_notice_idx)
        standalone_text = self.src[standalone_idx:standalone_end]
        self.assertIn("showing raw values", standalone_text)


class TestDetailViewMarkup(_LiveServerBase):
    def test_detail_content_containers_present(self):
        body = self._get_text("/health")
        for elem_id in (
            "detail-empty-state",
            "detail-content",
            "detail-scores-note",
            "detail-composed-block",
            "detail-aspect-sections",
        ):
            self.assertIn(f'id="{elem_id}"', body)

    def test_old_flat_containers_removed(self):
        """The pre-fork flat-list containers (#2518) are gone -- replaced
        by #detail-aspect-sections, which renders either aspect-grouped
        (model available) or flat (model unavailable) content at runtime,
        per health-detail.js's own render()/renderFlat()/
        renderAspectGrouped()."""
        body = self._get_text("/health")
        self.assertNotIn('id="detail-signal-rows"', body)
        self.assertNotIn('id="detail-aspect-tiles"', body)

    def test_detail_content_hidden_by_default(self):
        body = self._get_text("/health")
        m = re.search(r'<div id="detail-content"([^>]*)>', body)
        self.assertIsNotNone(m)
        self.assertIn("display:none", m.group(1).replace(" ", ""))

    def test_empty_state_message_still_present(self):
        """The pre-existing slice-1 placeholder text must survive as the
        served (pre-JS) markup -- test_viz_health_view_toggle.py already
        asserts this too; re-asserted here since this slice touches the
        same div."""
        body = self._get_text("/health")
        self.assertIn("No detail-view snapshot data yet", body)

    def test_apply_view_hooks_load_detail_view(self):
        """applyView()'s detail branch must still call loadDetailView() --
        now the global entry point health-detail.js exposes -- the hook
        that covers both the initial-page-load-in-detail case and a later
        Normal->Detail toggle click. Unchanged by this PR (out of scope:
        the spec explicitly keeps this hook's trigger condition untouched,
        only what it calls)."""
        body = self._get_text("/health")
        m = re.search(r"function applyView\(view, persist\)\s*\{(.*?)\n  \}", body, re.DOTALL)
        self.assertIsNotNone(m, "couldn't find applyView()")
        self.assertIn("loadDetailView()", m.group(1))


class TestMathFileServedAndReferenced(_LiveServerBase):
    def test_health_page_references_math_static_file(self):
        body = self._get_text("/health")
        self.assertIn('<script src="/static/health-detail-math.js"></script>', body)

    def test_math_static_file_served_with_js_content_type(self):
        resp = self._get("/static/health-detail-math.js")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Type"), "application/javascript")
        body = resp.body.decode()
        self.assertIn("function computeDistributionStats", body)
        self.assertIn("function computeHistogramBins", body)


# ---------------------------------------------------------------------------
# Real-DOM-execution control (engram-alpha#2868): the module docstring above
# and TestDetailJsFileContract both explain that this file's own convention
# is STATIC STRING assertions on source, never executing the render
# functions under a real DOM -- which is exactly why 62 pre-#2868 tests never
# caught that renderSignalCard/renderComposedBlock/renderAspectGrouped were
# still emitting a single stacked column: nothing ever called them and
# inspected the resulting tree, only grepped for identifier strings in the
# source text. This class closes that specific gap with a minimal,
# hand-rolled DOM shim (no jsdom/npm dependency -- deliberately, matching
# this repo's "no jsdom on this seat" convention cited above) that actually
# evaluates health-detail.js (and, as the acceptance criterion's own control,
# health-workbench.js) via Node's `vm.runInThisContext`, drives their real
# entry points (`window.loadDetailView()` / the workbench's `DOMContentLoaded`
# -> `init()` chain) against realistic fixture data through a stubbed
# `fetch`, and inspects the REAL resulting element tree -- not source text.
#
# Scope note: this shim implements only what these two files' RENDER-TIME
# code path touches (createElement/createElementNS, setAttribute/dataset,
# className/textContent/innerHTML, appendChild, a `#id`-only querySelector,
# and no-op event wiring). It deliberately does NOT implement SVG geometry
# (`getBBox`/`getBoundingClientRect`/`viewBox.baseVal`) -- every call site
# that needs those lives inside `fitViewBox`'s `requestAnimationFrame`
# callback or a mouse-event handler, neither of which this test invokes (the
# shim's `requestAnimationFrame` is a no-op that never runs its callback), so
# the structural assertions below never depend on them. No interactive
# testing (clicks/hovers) is performed or claimed.
@unittest.skipUnless(_NODE_AVAILABLE, "node not available on this seat")
class TestDetailViewRealDOMStructure(unittest.TestCase):
    """Drives health-detail.js's real render functions (via loadDetailView())
    against fixture data shaped like the real /api/health/v2(+/series,
    +/model) payloads, and asserts on the ACTUAL constructed DOM tree:

    1. None of the workbench's editing-control identifiers (enumerated from
       health-workbench.js's own source below, per the issue's own
       instruction not to guess them) appear anywhere in the Detail
       render's tree.
    2. The per-signal row, the composed-score block, and the aspect header
       are actually built as multi-column grid containers with the expected
       child elements -- not merely that the CSS class names exist in the
       stylesheet.

    Control (same acceptance criterion, other half): the identical probe
    run against health-workbench.js's OWN real render (driven through its
    actual `DOMContentLoaded` -> `init()` entry point, the same fixture
    shape) DOES find every one of those identifiers -- proving the probe
    itself is capable of finding them, so the negative result on the Detail
    side isn't vacuous (a probe that finds nothing anywhere would pass on
    an empty page for a boring reason)."""

    _EDITING_MARKERS = [
        "data-sig",  # knot x/y <input>s
        "data-imp",  # importance <input>
        "kbtn",  # every workbench edit-button class (delete/+/reset/reference)
        "id=wb-export-section",
        "id=wb-export-json",
        "id=wb-export-refresh",
        "id=wb-export-download",
        "id=wb-export-copy",
        "id=wb-export-reset",
        "text=reset aspect",
        "text=reset this curve",
        "text=set as reference",
        "tag=INPUT",
    ]

    def _run_harness(self):
        script = _DOM_HARNESS_JS
        script = script.replace("__MATH_PATH__", json.dumps(str(_MATH_JS_PATH)))
        script = script.replace("__CURVE_PATH__", json.dumps(str(_CURVE_JS_PATH)))
        script = script.replace("__DETAIL_PATH__", json.dumps(str(_DETAIL_JS_PATH)))
        script = script.replace("__WORKBENCH_PATH__", json.dumps(str(_WORKBENCH_JS_PATH)))
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            result.returncode, 0,
            f"DOM-harness node script failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"DOM-harness produced non-JSON stdout:\n{result.stdout}\nSTDERR: {result.stderr}")

    def setUp(self):
        self.report = self._run_harness()

    def test_detail_render_actually_ran(self):
        """Sanity check that the harness genuinely drove renderSignalCard/
        renderAspectGrouped/renderComposedBlock with real-shaped data, not
        an empty page -- the required control for the negative assertions
        below to mean anything at all."""
        d = self.report["detail"]
        self.assertGreaterEqual(d["signalCardCount"], 2, "expected >=2 rendered signal cards")
        self.assertTrue(d["composedBlockRendered"])
        self.assertGreaterEqual(d["aspectSectionCount"], 2, "expected >=2 rendered aspect sections")

    def test_detail_render_contains_no_editing_controls(self):
        d = self.report["detail"]
        self.assertEqual(
            d["editingHits"], [],
            f"Detail render must contain NONE of the editing-control identifiers, found: {d['editingHits']}",
        )

    def test_workbench_render_contains_editing_controls_control(self):
        """The control half of the acceptance criterion: the SAME probe,
        run against health-workbench.js's own real render, must find every
        identifier enumerated in _EDITING_MARKERS -- proving the probe is
        capable of finding them (so the Detail-side negative result is
        meaningful, not a probe that never matches anything)."""
        w = self.report["workbench"]
        self.assertGreaterEqual(w["signalRowCount"], 2, "expected >=2 rendered workbench signal rows")
        found_kinds = {hit.split("::", 1)[0] for hit in w["editingHits"]}
        expected_kinds = set(self._EDITING_MARKERS)
        missing = expected_kinds - found_kinds
        self.assertEqual(
            missing, set(),
            f"control failed -- probe did not find these editing markers in the "
            f"workbench's own real render (probe or fixture bug): {missing}",
        )

    def test_detail_signal_row_is_three_column_grid(self):
        """Structural check, not a CSS-class-string check: the DOM node
        health-detail.js actually appends for one signal_rows entry has the
        `detail-signal-grid` class AND exactly 3 direct child elements
        (left/middle/right), with content markers in each."""
        d = self.report["detail"]
        row = d["firstSignalGrid"]
        self.assertIsNotNone(row, "no .detail-signal-grid element found in the rendered tree")
        self.assertIn("detail-signal-grid", row["className"])
        self.assertEqual(row["childCount"], 3, f"expected 3 grid columns, got {row['childCount']}")
        self.assertTrue(row["leftHasH3"], "left column should contain the signal's <h3> header")
        self.assertTrue(row["midHasStrip"], "middle column should contain the strip/distribution container")
        self.assertTrue(row["rightHasCurveOrImportance"], "right column should contain the curve plot or importance readout")

    def test_detail_composed_block_is_two_column_strip(self):
        """#2881 restructuring: the right-hand column now holds the history/
        histogram strip (was the disclaimer/meaning text pre-#2881) -- the
        disclaimer moved to its own full-width bar above this row (see
        composedHasDisclaimerBar below). Still 2 columns; the CONTENT of
        the second column changed, not the column count."""
        d = self.report["detail"]
        top = d["composedStripTop"]
        self.assertIsNotNone(top, "no .detail-strip-top element found under #detail-composed-block")
        self.assertEqual(top["childCount"], 2, f"expected 2 columns (score, strip), got {top['childCount']}")
        self.assertTrue(d["composedHasDistStrip"], "a history/distribution strip should follow the top strip")

    def test_detail_composed_block_has_full_width_disclaimer_bar_above(self):
        """#2881: the disclaimer text is now a standalone full-width bar
        (`.detail-disclaimer-bar`) that is a DIRECT child of
        #detail-composed-block, appended BEFORE the `.detail-strip-top`
        two-column row -- not nested inside either column."""
        d = self.report["detail"]
        self.assertTrue(d["composedHasDisclaimerBar"], "no .detail-disclaimer-bar element found under #detail-composed-block")
        self.assertTrue(d["disclaimerBarBeforeStripTop"], "disclaimer bar must precede the score/strip row, not follow or nest inside it")

    def test_detail_aspect_head_is_three_cell_grid(self):
        d = self.report["detail"]
        head = d["firstAspectHead"]
        self.assertIsNotNone(head, "no .detail-aspect-head element found in the rendered tree")
        self.assertEqual(head["childCount"], 3, f"expected 3 cells (name, sub, tile), got {head['childCount']}")

    def test_composed_score_finite_with_held_out_aspect_member(self):
        """#2872 post-merge finding (Borges, PR #2872): the fixture's
        `provenance_integrity` aspect already carries a held-out metric
        (url_resolvability_rate, importance:0) alongside two admitted ones,
        but nothing previously asserted the RENDERED .detail-composed-value
        text is a finite number rather than "NaN" or "--" -- 34 tests
        passed without a single one naming this outcome.

        Post-#2881 note: MODEL_FIXTURE now ALSO carries `zero_eligible_
        aspect` (added for test_composed_score_finite_with_zero_eligible_
        aspect below), which shares this same render and this same
        `composedValueText` field -- so this test is no longer isolated to
        JUST the held-out-member case described below; without the #2881
        fix it would also fail via the zero-eligible-members path. That
        doesn't invalidate what follows -- provenance_integrity's own
        arithmetic still doesn't discriminate the guard, which is exactly
        why the dedicated test below was added rather than relying on this
        one to cover both shapes.

        Round-1 reviewer-fairy correction on this PR (verified, kept):
        this test pins TODAY's render as finite on THIS fixture; it does
        NOT exercise or discriminate the weighting-guard logic
        (aspectScoreForToday's `importance <= 0` skip, composedScoreForToday
        's null-check, aspectScore's `total > 0` check) at all. Measured by
        disabling all three simultaneously against the real source: still
        35/35 green, still a finite render. The reason is arithmetic, not
        guard interaction -- provenance_integrity always retains two
        importance:1 metrics with concrete raw values, so the held-out
        metric's own weighted contribution is exactly zero regardless of
        whether any guard fires; none of the three guards' trigger
        conditions (null raw, zero total importance) actually occur on
        this fixture. A genuine guard-discriminating test would need a
        fixture where an aspect's ADMITTED importance total is itself
        driven to zero (e.g. a fully-held-out aspect) -- NOT built here at
        the time of the original comment; #2881 below closes exactly that
        gap (see test_composed_score_finite_with_zero_eligible_aspect and
        the `zero_eligible_aspect` fixture entry).

        The fixture's `available: true` model makes composedScoreForToday()
        take the live compose-from-aspects path (not the flat snapshot.
        composed.value fallback), so this is a real computation, not a
        hardcoded fixture number -- that much holds."""
        text = self.report["detail"]["composedValueText"]
        self.assertIsNotNone(text, "no .detail-composed-value element found")
        self.assertNotIn("NaN", text, "composed score rendered NaN with a held-out aspect member present")
        self.assertNotEqual(text, "--", "composed score rendered the null placeholder, not a computed value")
        try:
            value = float(text)
        except ValueError:
            self.fail(f"composed score text {text!r} is not parseable as a finite number")
        self.assertTrue(math.isfinite(value), f"composed score {value} is not finite")

    def test_composed_score_finite_with_zero_eligible_aspect(self):
        """#2881 regression -- the genuinely guard-discriminating case the
        previous test's docstring named as a documented gap: `MODEL_FIXTURE`
        now includes `zero_eligible_aspect`, a THIRD aspect whose only
        member (`zero_eligible_signal`) is held-out (importance 0), so
        aspectScoreForToday()'s model-recompute path finds `members.length
        === 0` and falls through to the raw `aspect_tile` fallback, whose
        `score` field is the STRING sentinel "UNAVAILABLE" -- matching the
        real `provenance_integrity` bug's shape on a live seat (verified via
        `curl /api/health/v2?agent=kepler` at spec-writing time), which
        `provenance_integrity`'s own fixture entry (two admitted, scored
        siblings alongside its held-out member) never reaches.

        Pre-fix (`return tile ? tile.score : null;`), this string survives
        composeScore()'s `!== null && !== undefined` filter, `"UNAVAILABLE"
        * 100.0` evaluates to NaN, and the poisoned accumulator renders
        both `.detail-composed-value` AND this aspect's own
        `.detail-aspect-score` as the literal text "NaN". Post-fix
        (`typeof tile.score === "number" ? tile.score : null`), the aspect
        is correctly excluded from the compose-from-aspects weighted sum
        (rendering "--" for the excluded aspect's own score) while the
        composed value stays finite from the two remaining aspects."""
        d = self.report["detail"]
        composed_text = d["composedValueText"]
        self.assertIsNotNone(composed_text, "no .detail-composed-value element found")
        self.assertNotIn("NaN", composed_text, "composed score rendered NaN with a zero-eligible-members aspect present")
        try:
            value = float(composed_text)
        except ValueError:
            self.fail(f"composed score text {composed_text!r} is not parseable as a finite number")
        self.assertTrue(math.isfinite(value), f"composed score {value} is not finite")

        aspect_text = d["zeroEligibleAspectScoreText"]
        self.assertIsNotNone(aspect_text, "no .detail-aspect-score element found for zero_eligible_aspect")
        self.assertNotIn("NaN", aspect_text, "zero_eligible_aspect's own tile rendered NaN instead of the null placeholder")
        self.assertEqual(aspect_text, "--", "zero_eligible_aspect (zero eligible members) should render the null placeholder")

    def test_workbench_signal_row_is_three_column_grid_control(self):
        """Control for the grid-structure checks above: the workbench's own
        `.signal` row (unmodified by this PR) is independently confirmed to
        still be a 3-child grid container by the SAME traversal code,
        establishing the traversal itself is sound."""
        w = self.report["workbench"]
        row = w["firstSignalRow"]
        self.assertIsNotNone(row, "no .signal element found in the rendered workbench tree")
        self.assertEqual(row["childCount"], 3, f"expected 3 grid columns (left/mid/right), got {row['childCount']}")


@unittest.skipUnless(_NODE_AVAILABLE, "node not available on this seat")
class TestDistributionMathViaNode(unittest.TestCase):
    """Genuine executed-JS coverage for the pure math: requires the actual
    `health-detail-math.js` file under `node` and asserts on known
    inputs -> known outputs (percentile nearest-rank, population sd,
    Freedman-Diaconis with Sturges fallback). Unchanged by #2754 -- this
    file's own math is untouched, only its consumers (health-detail.js
    instead of the old inline renderDistributionPanel) changed."""

    def _run_node(self, script: str):
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(
            result.returncode, 0,
            f"node script failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}",
        )
        return json.loads(result.stdout)

    def test_math_js_file_exists(self):
        self.assertTrue(_MATH_JS_PATH.is_file(), f"missing {_MATH_JS_PATH}")

    def test_distribution_stats_known_inputs(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        console.log(JSON.stringify(m.computeDistributionStats([1,2,3,4,5])));
        """
        stats = self._run_node(script)
        self.assertEqual(stats["min"], 1)
        self.assertEqual(stats["p10"], 1)
        self.assertEqual(stats["p25"], 2)
        self.assertEqual(stats["p50"], 3)
        self.assertEqual(stats["p75"], 4)
        self.assertEqual(stats["p90"], 5)
        self.assertEqual(stats["max"], 5)
        self.assertEqual(stats["mean"], 3)
        self.assertAlmostEqual(stats["sd"], 1.4142135623730951, places=9)

    def test_distribution_stats_empty_returns_null(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        console.log(JSON.stringify(m.computeDistributionStats([]) === null));
        """
        self.assertTrue(self._run_node(script))

    def test_histogram_bins_known_inputs(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        console.log(JSON.stringify(m.computeHistogramBins([1,2,3,4,5])));
        """
        hist = self._run_node(script)
        self.assertEqual(len(hist["counts"]), 5)
        self.assertEqual(sum(hist["counts"]), 5)
        self.assertEqual(len(hist["edges"]), len(hist["counts"]) + 1)
        self.assertAlmostEqual(hist["edges"][0], 1)
        self.assertAlmostEqual(hist["edges"][-1], 5)

    def test_histogram_bins_degenerate_single_value_repeated(self):
        """max === min (a constant series) -- must not divide by zero, and
        must clamp to a single visible bin holding every point."""
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        console.log(JSON.stringify(m.computeHistogramBins([7,7,7])));
        """
        hist = self._run_node(script)
        self.assertEqual(hist["counts"], [3])
        self.assertEqual(len(hist["edges"]), 2)

    def test_histogram_bins_clamped_to_5_and_20(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        const wide = Array.from({{length: 500}}, (_, i) => i);
        const b = m.computeHistogramBins(wide);
        console.log(JSON.stringify({{n: b.counts.length}}));
        """
        result = self._run_node(script)
        self.assertGreaterEqual(result["n"], 5)
        self.assertLessEqual(result["n"], 20)

    def test_percentile_nearest_rank_indices(self):
        script = f"""
        const m = require({json.dumps(str(_MATH_JS_PATH))});
        const sorted = [10, 20, 30, 40, 50];
        console.log(JSON.stringify({{
          p10: m.percentile(sorted, 10),
          p50: m.percentile(sorted, 50),
          p90: m.percentile(sorted, 90),
        }}));
        """
        result = self._run_node(script)
        self.assertEqual(result["p10"], 10)
        self.assertEqual(result["p50"], 30)
        self.assertEqual(result["p90"], 50)


if __name__ == "__main__":
    unittest.main()
