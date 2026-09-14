// health-detail.js -- the health dashboard's user-facing, READ-ONLY Detail
// tab (engram-alpha#2754, PR A). Forked from health-workbench.js (the
// developer-tier Curve Workbench, engram-alpha#2727/#2738), per Lei's
// ruling on #2754: "the two surfaces don't need to keep sharing the same
// layout, they can drift apart organically ... simply copy the page and
// make necessary changes in-place to implement the user-facing detail
// variation of it, customize it to fit the existing health dashboard view."
//
// This is a FORK, not a parametrized shared page -- expected drift is a
// feature. Future user-facing changes land here; developer-tool needs land
// in health-workbench.js. No shared flag, no SSoT obligation between the
// two files.
//
// Kept from the workbench (informational, per the issue's own list): raw
// L0 numerator/denominator readouts, the pinned-checkpoint hover + focus
// readouts (makeFocusLayer/drawFocusAt/createReadoutEl/updateFocusReadout/
// wireFocusEvents/unpinAllFocus/fitViewBox), the history-strip rendering
// (historySVG) incl. UNAVAILABLE-run bands, the distribution/histogram
// rendering (renderHistogramSVG), per-signal and aspect/composed score
// readouts, and the "current reference" / "hold-out metric" TEXT markers.
//
// Stripped (this view edits nothing): knot handles/drag, the per-knot "+"
// button, importance <input>s (now a plain readout), the "set as
// reference" control (the text markers it produces stay -- see above),
// export/apply/reset, the stored-vs-edited `.changed` highlight/overlay
// (nothing is ever edited here, so nothing can differ from stored),
// localStorage persistence (nothing to persist).
//
// Data sources: /api/health/v2 (today's snapshot: signal_rows, aspect_tile,
// composed, provenance -- same shape health.html's OLD flat detail-view
// already read) and /api/health/v2/series WITHOUT a &signal= filter (one
// combined fetch returning every signal's history + the composed-score
// history in one round trip -- the OLD detail-view's own fetch shape, kept
// deliberately over the workbench's N-per-signal-fetch pattern: the
// workbench doesn't display composed-score history at all, so its fetch
// shape has nothing to say about that need, and the combined endpoint the
// server already exposes is strictly cheaper). Optionally /api/health/v2/
// model -- the SHIPPED scoring model's curve/importance/aspect
// declarations, used only as an ENRICHMENT layer (aspect grouping, the
// read-only curve plot, the reference/hold-out text markers, the
// importance readout): this endpoint is gated developer-tier-only
// server-side (viz/server.py's _workbench_available()), which this file
// does NOT touch (out of scope for this PR) -- so on a convenience-tier
// build where that endpoint 404s, this file degrades GRACEFULLY to a flat,
// ungrouped signal list driven entirely by /api/health/v2 + .../series,
// which never depended on the model to begin with. See the dispatching
// report's ambiguous-decision log for the full reasoning.
//
// This page NEVER writes the substrate, the model, or any file on disk --
// there is no edit path at all, so unlike health-workbench.js there is
// nothing to export either.
//
// Entry point: window.loadDetailView(), called by health.html's own
// view-toggle hook (`if (isDetail) { loadDetailView(); }` inside
// applyView()) -- same lazy-fetch-once contract the OLD detail-view's
// loadDetailView() used (a `_loaded` flag), so toggling Normal<->Detail
// never refetches. Wrapped in an IIFE with everything else private: this
// file shares a page (health.html) with an unrelated inline <script> that
// declares its OWN globals (`render`, `metric`, `esc`, ...) for the Normal
// view -- an unscoped health-workbench.js-style helper set (`el`, `svg`,
// `render`, `fmtNum`, ...) would silently collide with those, so only the
// one entry point below crosses the boundary.

(function () {
  "use strict";

  var state = {
    agent: null,
    // {available, model_defs, curve_defs} once fetched, or a fail-honest
    // {error, reason} dict (see /api/health/v2/model's own 404 shape) when
    // this build didn't ship the developer-tier curve model -- either way,
    // `state.model && state.model.available` is the one check every helper
    // below uses to decide whether the enrichment layer is usable.
    model: null,
    snapshot: null, // /api/health/v2 payload
    series: null, // /api/health/v2/series payload (combined, no &signal=)
    // key -> 'raw'|'rawhist'|'score'|'hist' for a per-signal strip, or
    // 'score'|'hist' for the composed-score strip (key "__composed__").
    histView: {},
    // "<key>|<plotKind>" -> {pinned: true, ...} -- same R2 pin-state shape
    // health-workbench.js's state.focus uses (see that file's own comment
    // on makeFocusLayer/wireFocusEvents for the full contract).
    focus: {},
    loaded: false,
  };

  var COMPOSED_KEY = "__composed__";

  // -----------------------------------------------------------------------
  // Tiny DOM / formatting helpers (ported from health-workbench.js).
  // -----------------------------------------------------------------------
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    for (var k in (attrs || {})) {
      if (k === "class") e.className = attrs[k];
      else if (k === "html") e.innerHTML = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { e.appendChild(c); });
    return e;
  }
  function fmtNum(v) {
    if (v === null || v === undefined) return "--";
    return typeof v === "number" ? v.toLocaleString("en-US") : String(v);
  }
  function escAttr(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  function fmtScore(v) {
    if (v === null || v === undefined || v === "UNAVAILABLE") return "--";
    return Number(v).toFixed(3);
  }
  function fmtPct(v) {
    if (v === null || v === undefined || v === "UNAVAILABLE" || isNaN(v)) return "--";
    return (Number(v) * 100).toFixed(2) + "%";
  }
  // checkpointLabelHTML: same convention as health-workbench.js's own
  // helper of the same name -- formatCheckpointLabel (health-workbench-
  // curve.js) returns one plain string; the trailing " (<N> [hd] ago)"
  // suffix is located by its fixed pattern and wrapped in a muted span.
  function checkpointLabelHTML(provenance) {
    var label = formatCheckpointLabel(provenance, Date.now());
    var m = /^(.*?)( \(\d+ [hd] ago\))$/.exec(label);
    if (m) {
      return m[1] + '<span class="detail-chkpt-age">' + m[2] + "</span>";
    }
    return label;
  }

  function currentAgentFromUrl() {
    return new URL(window.location.href).searchParams.get("agent");
  }
  function withAgent(path) {
    var a = currentAgentFromUrl();
    if (!a) return path;
    return path + (path.indexOf("?") >= 0 ? "&" : "?") + "agent=" + encodeURIComponent(a);
  }
  function fetchJSON(url) {
    return fetch(url).then(function (r) { return r.json(); });
  }

  // -----------------------------------------------------------------------
  // SVG helpers (ported verbatim from health-workbench.js).
  // -----------------------------------------------------------------------
  var NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  function svgText(x, y, str, attrs) {
    var merged = { x: x, y: y };
    for (var k in (attrs || {})) merged[k] = attrs[k];
    var t = svg("text", merged);
    t.textContent = str;
    return t;
  }

  // -----------------------------------------------------------------------
  // R2 focus marker machinery -- ported verbatim from health-workbench.js
  // (see that file's own comments for the full hover/pin contract). Shared
  // by every plot kind (curve / history / histogram) below.
  // -----------------------------------------------------------------------
  function makeFocusLayer(svgEl) {
    var g = svg("g", { class: "focus-layer" });
    svgEl.appendChild(g);
    return g;
  }
  function clearFocusLayer(g) {
    while (g.firstChild) g.removeChild(g.firstChild);
  }
  function drawFocusAt(g, W, H, xScreen, yScreen) {
    clearFocusLayer(g);
    g.appendChild(svg("line", { x1: xScreen, y1: 0, x2: xScreen, y2: H, class: "detail-focus-guide" }));
    if (yScreen !== null && yScreen !== undefined) {
      g.appendChild(svg("circle", { cx: xScreen, cy: yScreen, r: 3.5, class: "detail-focus-dot" }));
    }
  }
  function createReadoutEl(container) {
    var d = document.createElement("div");
    d.className = "detail-focus-readout";
    d.style.display = "none";
    container.appendChild(d);
    return d;
  }
  function hideReadout(readoutEl) {
    if (readoutEl) readoutEl.style.display = "none";
  }
  function setReadoutLines(readoutEl, lines) {
    while (readoutEl.firstChild) readoutEl.removeChild(readoutEl.firstChild);
    lines.forEach(function (line, i) {
      if (i > 0) readoutEl.appendChild(document.createElement("br"));
      readoutEl.appendChild(document.createTextNode(line));
    });
  }
  function updateFocusReadout(readoutEl, container, svgEl, xUser, yUser, lines) {
    if (!readoutEl) return;
    setReadoutLines(readoutEl, lines);
    readoutEl.style.display = "block";
    var svgRect = svgEl.getBoundingClientRect();
    var containerRect = container.getBoundingClientRect();
    var vb = svgEl.viewBox.baseVal;
    var scaleX = vb.width ? svgRect.width / vb.width : 1;
    var scaleY = vb.height ? svgRect.height / vb.height : 1;
    var markerX = (svgRect.left - containerRect.left) + (xUser - vb.x) * scaleX;
    var markerY = (svgRect.top - containerRect.top) + (yUser - vb.y) * scaleY;
    readoutEl.style.maxWidth = Math.max(40, container.clientWidth - 8) + "px";
    var flip = markerX > container.clientWidth * 0.6;
    var left = flip ? markerX - 6 - readoutEl.offsetWidth : markerX + 6;
    var top = markerY - 6;
    left = clamp(left, 0, Math.max(0, container.clientWidth - readoutEl.offsetWidth));
    top = clamp(top, 0, Math.max(0, container.clientHeight - readoutEl.offsetHeight));
    readoutEl.style.left = left + "px";
    readoutEl.style.top = top + "px";
  }
  function svgUserPoint(svgEl, evt) {
    var rect = svgEl.getBoundingClientRect();
    var vb = svgEl.viewBox.baseVal;
    return {
      x: vb.x + ((evt.clientX - rect.left) / rect.width) * vb.width,
      y: vb.y + ((evt.clientY - rect.top) / rect.height) * vb.height,
    };
  }
  function wireFocusEvents(svgEl, focusLayer, key, readoutEl, onPoint, pinnedRedraw) {
    if (state.focus[key]) pinnedRedraw();
    svgEl.addEventListener("mousemove", function (evt) {
      if (state.focus[key]) return;
      onPoint(svgUserPoint(svgEl, evt), false);
    });
    svgEl.addEventListener("mouseleave", function () {
      if (state.focus[key]) return;
      clearFocusLayer(focusLayer);
      hideReadout(readoutEl);
    });
    svgEl.addEventListener("click", function (evt) {
      if (state.focus[key]) {
        delete state.focus[key];
        clearFocusLayer(focusLayer);
        hideReadout(readoutEl);
        return;
      }
      onPoint(svgUserPoint(svgEl, evt), true);
    });
  }
  function unpinAllFocus() {
    if (!Object.keys(state.focus).length) return;
    state.focus = {};
    render();
  }
  function fitViewBox(svgEl, pad) {
    if (!svgEl) return;
    pad = pad === undefined ? 4 : pad;
    requestAnimationFrame(function () {
      try {
        var focusLayer = svgEl.querySelector(".focus-layer");
        var prevDisplay = focusLayer ? focusLayer.style.display : null;
        if (focusLayer) focusLayer.style.display = "none";
        var bbox = svgEl.getBBox();
        if (focusLayer) focusLayer.style.display = prevDisplay || "";
        if (!bbox || !isFinite(bbox.width) || !isFinite(bbox.height)) return;
        if (bbox.width === 0 && bbox.height === 0) return;
        svgEl.setAttribute(
          "viewBox",
          (bbox.x - pad) + " " + (bbox.y - pad) + " " + (bbox.width + 2 * pad) + " " + (bbox.height + 2 * pad)
        );
      } catch (e) {
        // getBBox() can throw on a detached/non-rendered SVG -- the
        // analytic first-paint viewBox stands in that case.
      }
    });
  }

  // -----------------------------------------------------------------------
  // Model helpers -- every one of these is a no-op (empty/null) when
  // state.model isn't available, which is what drives the graceful
  // flat-list fallback throughout render() below.
  // -----------------------------------------------------------------------
  function modelAvailable() {
    return !!(state.model && state.model.available);
  }
  function metricsForAspect(aspectName) {
    if (!modelAvailable()) return [];
    return (state.model.model_defs.metrics || []).filter(function (m) {
      return m.aspect === aspectName;
    });
  }
  function curveDefForSignal(signal) {
    if (!modelAvailable()) return null;
    var m = (state.model.model_defs.metrics || []).filter(function (x) { return x.signal === signal; })[0];
    if (!m) return null;
    return (state.model.curve_defs || {})[m.curve] || null;
  }
  function metricForSignal(signal) {
    if (!modelAvailable()) return null;
    return (state.model.model_defs.metrics || []).filter(function (m) { return m.signal === signal; })[0] || null;
  }

  // scoreForSignal: the CURRENT shipped model's curve applied to `raw` --
  // there is no edited/live curve in this fork, only the one the model
  // declares, so this is a straight, non-editable port of
  // health-workbench.js's scoreFor(). Returns null when the model isn't
  // available -- callers prefer the series' own already-derived `.score`
  // field first and use this only as a fallback for points that lack one
  // (see historySVG/renderHistogramSVG below), same order health-
  // workbench.js's own historySVG already used.
  function scoreForSignal(signal, rawValue) {
    if (rawValue === null || rawValue === undefined || rawValue === "UNAVAILABLE") return null;
    var cd = curveDefForSignal(signal);
    if (!cd || !cd.knots || !cd.knots.length) return null;
    return piecewiseLinear(Number(rawValue), cd.knots);
  }

  function rowByName() {
    var out = {};
    (state.snapshot.signal_rows || []).forEach(function (r) { out[r.name] = r; });
    return out;
  }

  // aspectScoreForToday/composedScoreForToday: recompute via the shared
  // math (aspectScore/composeScore, health-workbench-curve.js) over
  // TODAY's raw values when the model is available -- otherwise fall back
  // to the server's own precomputed aspect_tile[].score / composed.value,
  // which is always present regardless of model availability (see
  // viz/data/health_v2.py's get_health_v2_data -- neither field needs
  // model_defs to be built).
  function aspectScoreForToday(aspectName) {
    if (modelAvailable()) {
      var rows = rowByName();
      var members = [];
      metricsForAspect(aspectName).forEach(function (m) {
        if (m.importance <= 0) return;
        var row = rows[m.signal];
        if (!row) return;
        var raw = row.today ? row.today.raw : null;
        var sc = scoreForSignal(m.signal, raw);
        if (sc === null) return;
        members.push({ importance: m.importance, curveScore: sc });
      });
      if (members.length) return aspectScore(members);
    }
    var tile = (state.snapshot.aspect_tile || []).filter(function (a) { return a.name === aspectName; })[0];
    // #2881: tile.score can be the string sentinel "UNAVAILABLE" (a zero-
    // eligible-members aspect -- every member held-out or without data),
    // never null, for this fallback path -- only a real number is safe to
    // feed into composeScore()'s weighted-sum math below; the string
    // otherwise survives composeScore's `!== null && !== undefined` filter
    // and poisons the reduce accumulator to NaN. The visual status badges
    // (statusClass(), the aspect tile's own "(unscored)" text) read
    // tile/row fields directly elsewhere and are untouched by this guard.
    return tile ? (typeof tile.score === "number" ? tile.score : null) : null;
  }
  function composedScoreForToday() {
    if (modelAvailable()) {
      var aspects = (state.model.model_defs.aspects || []).map(function (a) {
        return { points: a.points, score: aspectScoreForToday(a.name) };
      });
      var v = composeScore(aspects);
      if (v !== null) return v;
    }
    return state.snapshot.composed ? state.snapshot.composed.value : null;
  }

  // -----------------------------------------------------------------------
  // Status badge -- kept from the OLD (pre-fork) detail-view: /api/health/
  // v2's own `status` field (scored/held_out/measured_not_scored/
  // unavailable, server-derived in viz/data/health_v2.py's _derive_status)
  // is richer per-run information the workbench never needed (it only
  // cares about the model's static importance>0/==0 split), and needs no
  // model_defs at all -- so it survives the fork unconditionally, in both
  // the model-available and flat-fallback render paths below.
  // -----------------------------------------------------------------------
  function statusClass(status) {
    if (status === "scored") return "good";
    if (status === "unavailable") return "danger";
    if (status === "held_out" || status === "measured_not_scored") return "warn";
    return "";
  }

  // -----------------------------------------------------------------------
  // Plots -- curveSVG (read-only: no drag, no knot handles, no +/-
  // buttons), renderHistogramSVG, historySVG. Ported from health-
  // workbench.js with every editing affordance removed; the focus/hover
  // machinery above is unchanged.
  // -----------------------------------------------------------------------
  function curveSVG(container, signal, cd) {
    var domain = (cd && cd.input_domain) || [0, 1];
    var range = (cd && cd.output_range) || [0, 1];
    var knots = cd && cd.knots ? cd.knots.slice().sort(function (a, b) { return a[0] - b[0]; }) : [[0, 0], [1, 1]];
    var W = 300, H = 120, px = 40, py = 10, pb = 24;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "curve" });
    container.appendChild(s);
    var sx = function (v) { return px + linearScale(v, domain[0], domain[1], 0, W - px - 8); };
    var sy = function (v) { return H - pb - linearScale(v, range[0], range[1], 0, H - py - pb); };
    var invX = function (xScreen) { return linearScale(xScreen, px, W - 8, domain[0], domain[1]); };

    [0, 0.5, 1].forEach(function (f) {
      var yVal = range[0] + f * (range[1] - range[0]);
      s.appendChild(svg("line", { x1: px, y1: sy(yVal), x2: W - 8, y2: sy(yVal), stroke: "#0f3460" }));
    });
    computeAxisTicks(range[0], range[1], 5).forEach(function (yv) {
      s.appendChild(svgText(px - 5, sy(yv) + 3, formatAxisNumber(yv), { class: "detail-axis-label", "text-anchor": "end" }));
    });
    computeAxisTicks(domain[0], domain[1], 5).forEach(function (xv) {
      var xp = sx(xv);
      s.appendChild(svg("line", { x1: xp, y1: H - pb, x2: xp, y2: H - pb + 3, stroke: "#555" }));
      s.appendChild(svgText(xp, H - pb + 12, formatAxisNumber(xv), { class: "detail-axis-label", "text-anchor": "middle" }));
    });

    var pts = [];
    for (var i = 0; i <= 60; i++) {
      var x = domain[0] + (domain[1] - domain[0]) * (i / 60);
      pts.push(sx(x) + "," + sy(piecewiseLinear(x, knots)));
    }
    s.appendChild(svg("polyline", { points: pts.join(" "), fill: "none", stroke: "#00d4ff", "stroke-width": 2 }));
    knots.forEach(function (p) {
      s.appendChild(svg("circle", { cx: sx(p[0]), cy: sy(p[1]), r: 3, fill: "#16213e", stroke: "#00d4ff", "stroke-width": 1.5 }));
    });

    var focusLayer = makeFocusLayer(s);
    var readoutEl = createReadoutEl(container);
    var key = signal + "|curve";
    function showAt(rawX, pinned) {
      var x = clamp(rawX, domain[0], domain[1]);
      var y = piecewiseLinear(x, knots);
      if (pinned) state.focus[key] = { pinned: true, x: x };
      drawFocusAt(focusLayer, W, H, sx(x), sy(y));
      updateFocusReadout(readoutEl, container, s, sx(x), sy(y), [
        "x = " + formatAxisNumber(x),
        "y = " + fmtScore(y),
      ]);
    }
    wireFocusEvents(s, focusLayer, key, readoutEl, function (pt, pinned) {
      showAt(invX(pt.x), pinned);
    }, function () {
      showAt(state.focus[key].x, false);
    });
    return s;
  }

  function renderHistogramSVG(container, signal, points, mode) {
    var isRawHist = mode === "rawhist";
    var vals = [];
    points.forEach(function (p) {
      var raw = typeof p.raw === "number" ? p.raw : null;
      var v;
      if (isRawHist) {
        v = raw;
      } else {
        v = typeof p.score === "number" ? p.score : (raw !== null ? scoreForSignal(signal, raw) : null);
      }
      if (typeof v === "number") vals.push(v);
    });
    var W = 300, H = 120, px = 40, py = 8, pb = 42;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": mode });
    container.appendChild(s);
    if (!vals.length) return s;
    var bins = computeHistogramBins(vals);
    var total = vals.length;
    var nb = bins.counts.length;
    var mx = Math.max.apply(null, bins.counts.concat([1]));
    var bw = (W - px - 8) / nb;

    s.appendChild(svgText(px - 5, H - pb + 3, "0%", { class: "detail-axis-label", "text-anchor": "end" }));
    s.appendChild(svgText(px - 5, py + 8, formatBinPercent(mx, total), { class: "detail-axis-label", "text-anchor": "end" }));

    bins.counts.forEach(function (cnt, i) {
      var h = (cnt / mx) * (H - py - pb);
      var barX = px + i * bw + 1;
      var barW = Math.max(1, bw - 2);
      var barY = H - pb - h;
      s.appendChild(svg("rect", { x: barX, y: barY, width: barW, height: h, fill: "#00d4ff", "fill-opacity": 0.55 }));
      if (cnt > 0 && barW >= 14) {
        var tallEnough = h >= 14;
        var labelAttrs = { class: "detail-axis-label", "font-size": "9", "text-anchor": "middle" };
        var labelY;
        if (tallEnough) { labelAttrs.fill = "#0a1420"; labelY = barY + 10; }
        else { labelY = barY - 3; }
        s.appendChild(svgText(barX + barW / 2, labelY, formatBinPercent(cnt, total), labelAttrs));
      }
    });

    bins.edges.forEach(function (ev, i) {
      var xp = px + i * bw;
      s.appendChild(svg("line", { x1: xp, y1: H - pb, x2: xp, y2: H - pb + 3, stroke: "#555" }));
      var ty = H - pb + 6;
      var label = isRawHist ? fmtPct(ev) : ev.toFixed(2);
      s.appendChild(svgText(xp, ty, label, {
        class: "detail-axis-label", "font-size": "9", "text-anchor": "end",
        transform: "rotate(-45 " + xp + " " + ty + ")",
      }));
    });

    var focusLayer = makeFocusLayer(s);
    var readoutEl = createReadoutEl(container);
    var key = signal + "|" + mode;
    function showBin(idx, pinned) {
      idx = clamp(idx, 0, nb - 1);
      var cnt = bins.counts[idx];
      var lo = bins.edges[idx], hi = bins.edges[idx + 1];
      var fmtEdge = isRawHist ? fmtPct : function (v) { return v.toFixed(2); };
      if (pinned) state.focus[key] = { pinned: true, idx: idx };
      var barCenterX = px + idx * bw + bw / 2;
      var barTopY = H - pb - (cnt / mx) * (H - py - pb);
      drawFocusAt(focusLayer, W, H, barCenterX, barTopY);
      updateFocusReadout(readoutEl, container, s, barCenterX, barTopY, [
        "[" + fmtEdge(lo) + ", " + fmtEdge(hi) + ")",
        fmtNum(cnt) + " points (" + formatBinPercent(cnt, total) + ")",
      ]);
    }
    wireFocusEvents(s, focusLayer, key, readoutEl, function (pt, pinned) {
      showBin(binIndexForX(pt.x, px, bw, nb), pinned);
    }, function () {
      showBin(state.focus[key].idx, false);
    });
    return s;
  }

  function historySVG(container, signal, points, mode, inputDomain, outputRange) {
    if (mode === "hist" || mode === "rawhist") {
      return renderHistogramSVG(container, signal, points, mode);
    }
    var W = 300, H = 100, px = 38, py = 8, pb = 28;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": mode });
    container.appendChild(s);
    if (!points.length) return s;
    var isScore = mode === "score";

    var times = points.map(function (p) { return Date.parse(p.t); });
    var tMin = Math.min.apply(null, times), tMax = Math.max.apply(null, times);
    if (tMax === tMin) tMax = tMin + 1;
    var rich = points.map(function (p, i) {
      var raw = typeof p.raw === "number" ? p.raw : null;
      var scoreVal = typeof p.score === "number" ? p.score
        : (raw !== null ? scoreForSignal(signal, raw) : null);
      var v = isScore ? scoreVal : raw;
      return { t: times[i], raw: raw, score: scoreVal, v: (typeof v === "number" ? v : null) };
    });
    var vals = rich.map(function (p) { return p.v; }).filter(function (v) { return v !== null; });
    if (!vals.length) return s;
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi === lo) {
      var padded = padFlatRange(lo, isScore ? "score" : "raw", inputDomain, outputRange);
      lo = padded[0];
      hi = padded[1];
    }
    var sx = function (t) { return px + linearScale(t, tMin, tMax, 0, W - px - 8); };
    var sy = function (v) { return H - pb - linearScale(v, lo, hi, 0, H - py - pb); };

    var fmtY = isScore ? fmtScore : fmtPct;
    [lo, (lo + hi) / 2, hi].forEach(function (yv) {
      s.appendChild(svgText(px - 5, sy(yv) + 3, fmtY(yv), { class: "detail-axis-label", "text-anchor": "end" }));
    });

    var xTicksMs = computeTimeTicks(tMin, tMax, 5);
    var xLabels = computeTimeTickLabels(xTicksMs);
    var spacingPx = xTicksMs.length > 1 ? (sx(xTicksMs[1]) - sx(xTicksMs[0])) : (W - px - 8);
    var tilt = shouldTiltLabels(spacingPx, xLabels);
    xTicksMs.forEach(function (tms, i) {
      var xp = sx(tms);
      s.appendChild(svg("line", { x1: xp, y1: H - pb, x2: xp, y2: H - pb + 3, stroke: "#555" }));
      var ty = H - pb + (tilt ? 6 : 10);
      var attrs = { class: "detail-axis-label", "font-size": "9" };
      if (tilt) { attrs["text-anchor"] = "end"; attrs.transform = "rotate(-45 " + xp + " " + ty + ")"; }
      else { attrs["text-anchor"] = "middle"; }
      s.appendChild(svgText(xp, ty, xLabels[i], attrs));
    });

    var segments = [];
    var currentSeg = [];
    rich.forEach(function (p) {
      if (p.v !== null) { currentSeg.push(sx(p.t) + "," + sy(p.v)); }
      else if (currentSeg.length) { segments.push(currentSeg); currentSeg = []; }
    });
    if (currentSeg.length) segments.push(currentSeg);
    segments.forEach(function (seg) {
      if (seg.length > 1) {
        s.appendChild(svg("polyline", { points: seg.join(" "), fill: "none", stroke: "#7ecfff", "stroke-width": 1.3 }));
      }
    });

    var runs = collapseUnavailableRuns(points).filter(function (r) { return r.n >= 2; });
    var hatchId = "detail-hatch-" + signal.replace(/[^a-zA-Z0-9_-]/g, "_");
    if (runs.length) {
      var defs = svg("defs", {});
      var pattern = svg("pattern", { id: hatchId, width: 4, height: 4, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
      pattern.appendChild(svg("rect", { width: 4, height: 4, fill: "#16213e" }));
      pattern.appendChild(svg("line", { x1: 0, y1: 0, x2: 0, y2: 4, stroke: "#888", "stroke-width": 2 }));
      defs.appendChild(pattern);
      s.appendChild(defs);
    }
    var bandsScreen = runs.map(function (r) {
      var x0 = sx(r.t0), x1 = sx(r.t1);
      if (x1 < x0) { var tmp = x0; x0 = x1; x1 = tmp; }
      if (x1 - x0 < 3) { var mid = (x0 + x1) / 2; x0 = mid - 1.5; x1 = mid + 1.5; }
      return { x0: x0, x1: x1, run: r };
    });
    bandsScreen.forEach(function (b) {
      s.appendChild(svg("rect", {
        x: b.x0, y: H - pb - 3, width: b.x1 - b.x0, height: 6,
        fill: "url(#" + hatchId + ")", stroke: "#888", "stroke-width": 0.5,
        class: "detail-unavailable-band",
      }));
      if (b.x1 - b.x0 >= 60) {
        s.appendChild(svgText((b.x0 + b.x1) / 2, H - pb - 8, "no measurement · " + b.run.n + " checkpoints", {
          class: "detail-axis-label", "font-size": "8", "text-anchor": "middle",
        }));
      }
    });

    var lastNumeric = null;
    for (var i = rich.length - 1; i >= 0; i--) {
      if (rich[i].v !== null) { lastNumeric = rich[i]; break; }
    }
    if (lastNumeric) {
      s.appendChild(svg("circle", { cx: sx(lastNumeric.t), cy: sy(lastNumeric.v), r: 3, fill: "#00d4ff" }));
    }

    var focusLayer = makeFocusLayer(s);
    var readoutEl = createReadoutEl(container);
    var key = signal + "|history-" + mode;
    var xsScreenForHover = rich.map(function (p) { return p.v !== null ? sx(p.t) : null; });
    function readoutLines(p) {
      return [formatLocalDateTime(p.t), "raw " + fmtPct(p.raw), "score " + fmtScore(p.score)];
    }
    function showIdx(idx, pinned) {
      var p = rich[idx];
      if (pinned) state.focus[key] = { pinned: true, idx: idx };
      var yScreen = p.v !== null ? sy(p.v) : (H - pb);
      drawFocusAt(focusLayer, W, H, sx(p.t), yScreen);
      updateFocusReadout(readoutEl, container, s, sx(p.t), yScreen, readoutLines(p));
    }
    function findBandAt(x) {
      for (var bi = 0; bi < bandsScreen.length; bi++) {
        var b = bandsScreen[bi];
        if (x >= b.x0 && x <= b.x1) return b;
      }
      return null;
    }
    function showBand(band, pinned) {
      if (pinned) {
        state.focus[key] = {
          pinned: true, band: true, x0: band.x0, x1: band.x1,
          t0: band.run.t0, t1: band.run.t1, n: band.run.n, reasons: band.run.reasons,
        };
      }
      var midX = (band.x0 + band.x1) / 2;
      var yScreen = H - pb;
      drawFocusAt(focusLayer, W, H, midX, yScreen);
      var reasonKey = dominantReasonKey(band.run.reasons);
      updateFocusReadout(readoutEl, container, s, midX, yScreen, [
        formatLocalDateTime(band.run.t0) + " → " + formatLocalDateTime(band.run.t1),
        band.run.n + " checkpoints unavailable",
        reasonKey || "reason unknown",
      ]);
    }
    wireFocusEvents(s, focusLayer, key, readoutEl, function (pt, pinned) {
      var band = findBandAt(pt.x);
      if (band) { showBand(band, pinned); return; }
      var idx = nearestIndexByValue(xsScreenForHover, pt.x);
      if (idx >= 0) showIdx(idx, pinned);
    }, function () {
      var f = state.focus[key];
      if (f.band) {
        showBand({ x0: f.x0, x1: f.x1, run: { t0: f.t0, t1: f.t1, n: f.n, reasons: f.reasons } }, false);
      } else {
        showIdx(f.idx, false);
      }
    });
    return s;
  }

  // -----------------------------------------------------------------------
  // One Series/Raw/Histogram strip -- a dashboard-styled mode toggle
  // (reuses .view-toggle/.detail-mini-toggle-btn, health.html's own
  // existing convention) plus the two plot containers above.
  // -----------------------------------------------------------------------
  // Surfaces the /api/health/v2/series fail-honest {error, reason} text when
  // that's WHY points is empty (e.g. reason === "no_db"), instead of always
  // showing the same generic note regardless of cause -- mirrors the old
  // pre-fork detail-view's _classifySeries() behavior.
  function seriesEmptyNote() {
    if (state.series && state.series.error) {
      return state.series.reason === "no_db"
        ? "no history yet on this seat"
        : String(state.series.error);
    }
    return "no recorded history points yet";
  }

  function renderStripAndDistribution(parentEl, key, points, modes, defaultMode) {
    if (!points.length) {
      parentEl.innerHTML = '<div class="detail-series-note">' + escAttr(seriesEmptyNote()) + "</div>";
      return;
    }
    if (!state.histView[key]) state.histView[key] = defaultMode;
    var seg = el("div", { class: "view-toggle detail-mini-toggle" });
    modes.forEach(function (pair) {
      var b = el("button", {
        type: "button",
        class: "view-toggle-btn detail-mini-toggle-btn" + (state.histView[key] === pair[0] ? " active" : ""),
      });
      b.textContent = pair[1];
      b.addEventListener("click", function () { state.histView[key] = pair[0]; render(); });
      seg.appendChild(b);
    });
    parentEl.innerHTML = "";
    parentEl.appendChild(seg);
    var histHost = el("div", { class: "detail-plot-container" });
    parentEl.appendChild(histHost);

    var mode = state.histView[key];
    var signalForPlot = key === COMPOSED_KEY ? COMPOSED_KEY : key;
    var cd = key === COMPOSED_KEY ? null : curveDefForSignal(key);
    var histEl = historySVG(histHost, signalForPlot, points, mode, cd ? cd.input_domain : null, cd ? cd.output_range : null);
    fitViewBox(histEl, 4);

    var mappedKey = mode === "raw" || mode === "rawhist" ? "raw" : "score";
    var vals = points
      .map(function (p) {
        if (mappedKey === "raw") return typeof p.raw === "number" ? p.raw : null;
        return typeof p.score === "number" ? p.score
          : (typeof p.raw === "number" ? scoreForSignal(signalForPlot, p.raw) : null);
      })
      .filter(function (v) { return v !== null; });
    var stats = computeDistributionStats(vals);
    if (stats) {
      var cols = ["min", "p10", "p25", "p50", "p75", "p90", "max", "mean", "sd"];
      var fmtCol = mappedKey === "raw" ? fmtPct : fmtScore;
      var thead = "<thead><tr><th></th>" + cols.map(function (c) { return "<th style='text-align:right'>" + c + "</th>"; }).join("") + "</tr></thead>";
      var rowHtml = "<tr><td>" + (mappedKey === "raw" ? "raw" : "score") + "</td>" +
        cols.map(function (c) { return "<td class='num'>" + fmtCol(stats[c]) + "</td>"; }).join("") + "</tr>";
      parentEl.appendChild(el("table", { class: "detail-statst", html: thead + "<tbody>" + rowHtml + "</tbody>" }));
    }
  }

  // -----------------------------------------------------------------------
  // Per-signal row -- a three-column grid, per the #2868 fork ruling
  // (health_workbench.html's `.signal` shape, ported to this page's own
  // `.detail-signal-grid` class -- see health.html's own comment on that
  // rule). Column split is the issue's own explicit assignment (NOT a
  // verbatim copy of health-workbench.js's LEFT/MIDDLE/RIGHT split, which
  // puts importance/hold-out-marker content in its own RIGHT column
  // alongside its curve editor -- this fork's assignment matches that
  // placement, just without the editing controls):
  //   LEFT   -- definition/raw-counts card content (header, formula, raw
  //             counts, today's raw + score).
  //   MIDDLE -- the existing renderStripAndDistribution output (history +
  //             distribution, unchanged internals).
  //   RIGHT  -- the existing curve plot + importance readout + hold-out/
  //             current-reference text markers (unchanged content, just
  //             relocated).
  // -----------------------------------------------------------------------
  function renderSignalCard(row) {
    var signal = row.name;
    var metric = metricForSignal(signal);
    var cd = curveDefForSignal(signal);
    var held = metric ? metric.importance <= 0 : row.status === "held_out";

    var card = el("div", { class: "card detail-signal-row detail-signal-grid" + (held ? " detail-held" : "") });

    // ---- LEFT: definition + raw counts ----
    var left = el("div", {});
    left.appendChild(el("h3", {
      html: escAttr(signal) + ' <span class="detail-status-badge ' + statusClass(row.status) + '">' + escAttr(row.status) + "</span>"
        + (metric ? ' <span class="detail-aspect-tag">aspect: ' + escAttr(metric.aspect) + "</span>" : ""),
    }));

    var def = row.definition || {};
    left.appendChild(el("div", { class: "metric", html: '<span class="label">formula</span><span class="value">' + escAttr(def.formula) + "</span>" }));
    var num = def.numerator || {}, den = def.denominator || {};
    var haveCounts = num.value !== null && num.value !== undefined && den.value !== null && den.value !== undefined;
    left.appendChild(el("div", {
      class: "metric",
      html: haveCounts
        ? '<span class="label">raw counts</span><span class="value">' + fmtNum(num.value) + " / " + fmtNum(den.value) + "</span>"
        : '<span class="label">raw counts</span><span class="value">unavailable</span>',
    }));
    var today = row.today || {};
    left.appendChild(el("div", { class: "metric", html: '<span class="label">raw</span><span class="value">' + fmtPct(today.raw) + "</span>" }));
    var todayScore = typeof today.score === "number" ? today.score
      : (typeof today.raw === "number" ? scoreForSignal(signal, today.raw) : null);
    left.appendChild(el("div", { class: "metric", html: '<span class="label">score</span><span class="value">' + fmtScore(todayScore) + "</span>" }));
    card.appendChild(left);

    // ---- MIDDLE: history & distribution ----
    var mid = el("div", {});
    mid.appendChild(el("div", { class: "detail-plot-label", html: "History &amp; distribution" }));
    var stripHost = el("div", { class: "detail-strip-dist-container" });
    mid.appendChild(stripHost);
    var seriesArr = (state.series && state.series.signals && state.series.signals[signal]) || [];
    var modes = [["raw", "raw series"], ["rawhist", "raw histogram"], ["score", "score series"], ["hist", "score histogram"]];
    renderStripAndDistribution(stripHost, signal, seriesArr, modes, "raw");
    card.appendChild(mid);

    // ---- RIGHT: curve plot + importance + hold-out/current-reference ----
    var right = el("div", {});
    if (metric) {
      right.appendChild(el("div", { class: "metric", html: '<span class="label">importance</span><span class="value">' + fmtNum(metric.importance) + "</span>" }));
    }

    // Hold-out / current-reference TEXT markers -- Lei's ruling on #2754:
    // "the markers stay, the control goes." No third branch here (an
    // eligible-but-not-yet-reference metric): health-workbench.js renders
    // a "set as reference" BUTTON for that case, which is the control this
    // fork removes; there is no marker text to replace it with.
    if (modelAvailable() && metric) {
      var aspectDef = (state.model.model_defs.aspects || []).filter(function (a) { return a.name === metric.aspect; })[0];
      var refSignal = aspectDef ? aspectDef.reference_metric : null;
      if (held) {
        right.appendChild(el("div", { class: "detail-refctl", html: "hold-out metric" }));
      } else if (refSignal === signal) {
        right.appendChild(el("div", { class: "detail-refctl", html: "current reference" }));
      }
    }

    if (cd) {
      right.appendChild(el("div", { class: "detail-plot-label", html: "raw → score curve" }));
      var curveHost = el("div", { class: "detail-plot-container" });
      right.appendChild(curveHost);
      var curveEl = curveSVG(curveHost, signal, cd);
      fitViewBox(curveEl, 4);
    }
    card.appendChild(right);

    return card;
  }

  // -----------------------------------------------------------------------
  // Composed-score block -- restructured per #2881 (Lei's explicit ruling):
  // the disclaimer text that used to sit in the workbench-ported `.meaning`
  // side panel is now a slim, full-width bar ABOVE everything else; the
  // right-hand slot beside the Composed Score card (where the disclaimer
  // used to sit) now holds the history/histogram strip instead. Text
  // content of the disclaimer is unchanged -- only its container/position
  // and CSS class (detail-meaning-panel -> detail-disclaimer-bar).
  //
  // JUDGMENT CALL (flagged in the dispatching report): the exact two-column
  // width ratio for `.detail-strip-top` (score card | plot) is not
  // specified by the issue -- verify visually (see health.html's own
  // comment on that rule) rather than treating this as settled.
  // -----------------------------------------------------------------------
  function renderComposedBlock() {
    var prov = state.snapshot.provenance || {};
    var el1 = qs("#detail-composed-block");
    var composedValue = composedScoreForToday();
    el1.innerHTML = "";

    // JUDGMENT CALL (flagged in the dispatching report): the workbench's
    // `.meaning` panel disclaims EDITING ("Nothing on this page writes the
    // substrate, the model, or any file on disk") -- reworded here since
    // this read-only fork has no editing to disclaim in the first place,
    // but the underlying fact (this page never writes anything) still
    // holds and is worth keeping, per health-detail.js's own module
    // docstring ("This page NEVER writes the substrate...").
    var disclaimerBar = el("div", { class: "detail-disclaimer-bar" });
    disclaimerBar.appendChild(el("p", {
      html: "This is the same importance-weighted-mean / points-weighted composition the shipped scoring model uses, recomputed here in JS from the model&rsquo;s shipped curves and importance (see <code>/static/health-workbench-curve.js</code>) for read-only display. This page never writes the substrate, the model, or any file on disk.",
    }));
    el1.appendChild(disclaimerBar);

    var top = el("div", { class: "detail-strip-top" });

    var scoreCard = el("div", { class: "card" });
    scoreCard.appendChild(el("h3", { html: "Composed Score" }));
    scoreCard.appendChild(el("div", { class: "detail-composed-value", html: composedValue === null ? "--" : Number(composedValue).toFixed(1) }));
    scoreCard.appendChild(el("div", { class: "detail-provenance", html: checkpointLabelHTML(prov) }));
    scoreCard.appendChild(el("div", { class: "metric", html: '<span class="label">snapshot</span><span class="value">' + escAttr(prov.snapshot_id) + "</span>" }));
    scoreCard.appendChild(el("div", { class: "metric", html: '<span class="label">turn</span><span class="value">' + escAttr(prov.turn) + "</span>" }));
    scoreCard.appendChild(el("div", { class: "metric", html: '<span class="label">checkpoint_mode</span><span class="value">' + escAttr(prov.checkpoint_mode) + "</span>" }));
    scoreCard.appendChild(el("div", { class: "metric", html: '<span class="label">health_version</span><span class="value">' + escAttr(prov.health_version) + "</span>" }));
    top.appendChild(scoreCard);

    // JUDGMENT CALL (flagged in the dispatching report): the strip now
    // lives in its own `.card` for visual parity with scoreCard, rather
    // than a bare, unstyled container -- the issue names the SLOT
    // (beside Composed Score) but not the wrapper styling.
    var stripCard = el("div", { class: "card" });
    var stripHost = el("div", { class: "detail-strip-dist-container" });
    stripCard.appendChild(stripHost);
    top.appendChild(stripCard);

    el1.appendChild(top);

    var points = ((state.series && state.series.composed) || []).map(function (p) {
      return { t: p.t, score: p.value, raw: null, unreplayable: null };
    });
    renderStripAndDistribution(stripHost, COMPOSED_KEY, points, [["score", "series"], ["hist", "histogram"]], "score");
  }

  // -----------------------------------------------------------------------
  // Aspect grouping (model available) vs. flat fallback (model
  // unavailable -- e.g. a convenience-tier build that never shipped the
  // developer-tier /api/health/v2/model backend).
  // -----------------------------------------------------------------------
  function renderAspectGrouped() {
    var root = qs("#detail-aspect-sections");
    root.innerHTML = "";
    var rows = rowByName();
    (state.model.model_defs.aspects || []).forEach(function (aspect) {
      var members = metricsForAspect(aspect.name);
      var section = el("section", { class: "detail-aspect-section" });
      var aScore = aspectScoreForToday(aspect.name);

      // Aspect header -- the workbench's `.aspect-head` grid shape (name |
      // sub-text | composed sub-score badge), ported to this page's own
      // `.detail-aspect-head`/`.detail-atile` classes -- see health.html's
      // own comment on those rules. Drops only the workbench's "reset
      // aspect" button (an editing control this fork has nothing to
      // reset).
      var head = el("div", { class: "detail-aspect-head" });
      head.appendChild(el("h2", { html: escAttr(aspect.name) }));
      head.appendChild(el("div", {
        class: "detail-aspect-sub",
        html: members.length + " metric(s) declared · reference: " + (aspect.reference_metric || "none"),
      }));
      var tile = el("div", { class: "detail-atile" });
      tile.appendChild(el("span", { class: "detail-aspect-score", html: aScore === null ? "--" : Number(aScore).toFixed(3) }));
      tile.appendChild(document.createTextNode(" · "));
      tile.appendChild(el("span", { class: "detail-atile-pts", html: Number(aspect.points).toFixed(1) + " pts declared" }));
      head.appendChild(tile);
      section.appendChild(head);

      members.forEach(function (m) {
        var row = rows[m.signal];
        if (row) section.appendChild(renderSignalCard(row));
      });
      root.appendChild(section);
    });
  }

  function renderFlat() {
    var root = qs("#detail-aspect-sections");
    root.innerHTML = "";
    (state.snapshot.signal_rows || []).forEach(function (row) {
      root.appendChild(renderSignalCard(row));
    });
    var aspectTile = state.snapshot.aspect_tile || [];
    if (aspectTile.length) {
      var card = el("div", { class: "card" });
      card.appendChild(el("h3", { html: "Aspects" }));
      aspectTile.forEach(function (a) {
        var label = escAttr(a.name) + (a.unscored ? " (unscored)" : "");
        var value = fmtScore(a.score) + (a.coverage !== undefined && a.coverage !== null ? " · coverage " + fmtPct(a.coverage) : "");
        card.appendChild(el("div", { class: "metric", html: '<span class="label">' + label + '</span><span class="value">' + value + "</span>" }));
      });
      root.appendChild(card);
    }
  }

  // -----------------------------------------------------------------------
  // Top-level render.
  // -----------------------------------------------------------------------
  function render() {
    var emptyEl = qs("#detail-empty-state");
    var contentEl = qs("#detail-content");
    if (!state.snapshot || state.snapshot.has_snapshot === false) {
      emptyEl.textContent = (state.snapshot && state.snapshot.reason)
        ? state.snapshot.reason
        : "No detail-view snapshot data yet — this view is under construction.";
      emptyEl.style.display = "";
      contentEl.style.display = "none";
      return;
    }
    emptyEl.style.display = "none";
    contentEl.style.display = "";

    var noteEl = qs("#detail-scores-note");
    var seriesUnscored = state.series && state.series.scores_available === false;
    // engram-alpha#2896 item 2: name the actual version + the "run derive"
    // remedy, per #2869's own requested wording ("a one-line 'no derived
    // rows for vX — run derive' notice") -- both seriesUnscored branches
    // below carry it, not just an explanation of the condition.
    // state.series is truthy whenever seriesUnscored is true (it's the
    // left operand of the `&&` above), and health/series.py's export()
    // always returns `version` alongside `scores_available` on its one
    // success-path return statement -- so state.series.version is safe
    // here with no separate undefined guard.
    if (!modelAvailable() && seriesUnscored) {
      noteEl.textContent = "no derived rows for " + state.series.version + " — run derive. The developer-tier curve model isn't installed on this build either — showing raw values and server-reported scores only.";
      noteEl.style.display = "";
    } else if (!modelAvailable()) {
      noteEl.textContent = "the developer-tier curve model isn't installed on this build — signals are shown as a flat list without aspect grouping, curve plots, or reference markers.";
      noteEl.style.display = "";
    } else if (seriesUnscored) {
      noteEl.textContent = "no derived rows for " + state.series.version + " — run derive — showing raw values.";
      noteEl.style.display = "";
    } else {
      noteEl.style.display = "none";
    }

    renderComposedBlock();
    if (modelAvailable()) {
      renderAspectGrouped();
    } else {
      renderFlat();
    }
  }

  // -----------------------------------------------------------------------
  // Entry point -- lazy, fetch-once (mirrors the OLD detail-view's
  // `_detailLoaded` guard so Normal<->Detail toggling never refetches).
  // -----------------------------------------------------------------------
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") unpinAllFocus();
  });

  function loadDetailView() {
    if (state.loaded) return;
    state.loaded = true;
    state.agent = currentAgentFromUrl();

    var modelPromise = fetchJSON("/api/health/v2/model").then(function (resp) {
      state.model = resp;
    }).catch(function () {
      state.model = { available: false, reason: "fetch failed" };
    });
    var snapshotPromise = fetchJSON(withAgent("/api/health/v2")).then(function (resp) {
      state.snapshot = resp;
    });
    var seriesPromise = fetchJSON(withAgent("/api/health/v2/series")).then(function (resp) {
      state.series = resp;
    }).catch(function () {
      state.series = null;
    });

    Promise.all([modelPromise, snapshotPromise, seriesPromise]).then(render).catch(function (e) {
      var emptyEl = qs("#detail-empty-state");
      var contentEl = qs("#detail-content");
      emptyEl.textContent = "Error loading detail view: " + e.message;
      emptyEl.style.display = "";
      contentEl.style.display = "none";
    });
  }

  window.loadDetailView = loadDetailView;
})();
