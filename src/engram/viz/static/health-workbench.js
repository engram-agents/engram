// health-workbench.js -- Curve Workbench page logic (engram-alpha#2727).
//
// DOM/fetch layer only -- the pure scoring math lives in
// health-workbench-curve.js (piecewiseLinear / aspectScore / composeScore,
// a line-for-line port of health/curves.py + health/engine.py, loaded
// before this file) and the distribution/histogram math is REUSED, not
// duplicated, from health-detail-math.js (computeDistributionStats /
// computeHistogramBins / percentile, same file the detail view uses).
//
// Data sources, all read-only:
//   /api/agents               -- the seat list, for the selector + the
//                                 per-seat table.
//   /api/health/v2/model      -- the SHIPPED model's curves/importance
//                                 (viz.data.health_workbench).
//   /api/health/v2?agent=X    -- X's latest snapshot (definition, raw L0
//                                 counts, today's score, aspect tile,
//                                 composed).
//   /api/health/v2/series?agent=X&signal=S -- X's history for signal S
//                                 (num/den/raw/score per checkpoint).
//
// This page NEVER writes the substrate, the model, or any file on disk.
// The only persistence is localStorage (working-state only) and the
// "Export decisions" JSON, which the user copies/downloads by hand.

(function () {
  "use strict";

  var LS_KEY = "engram-curve-workbench-edits-v1";

  var state = {
    agent: null,
    agents: [],
    model: null, // {available, model_defs, curve_defs}
    snapshotByAgent: {}, // agent -> /api/health/v2 payload
    seriesByAgentSignal: {}, // "agent|signal" -> /api/health/v2/series payload
    curves: {}, // signal -> {knots: [[x,y],...], importance: number} (LIVE, editable)
    edited: {}, // signal -> true, once touched by the user
    // signal -> {knots, importance} -- the RAW localStorage-persisted blob
    // for a signal that has NOT (yet) been loaded into the live edit state
    // above (engram-alpha#2870 fix 2). Always re-seeded from loadEdits()
    // in seedCurveState(); the page opens on the SHIPPED curve for every
    // signal regardless of what's in here -- a draft banner
    // (renderSignalRow, RIGHT column) is the only UI surface that shows
    // it, and only "load draft" ever copies it into
    // state.curves/state.edited.
    drafts: {},
    histView: {}, // signal -> 'raw'|'rawhist'|'score'|'hist'
    // signal -> reason string, set when an edit-enter-time value check
    // refuses a knot/importance edit (engram-alpha#2738 round 2); cleared
    // on the next accepted edit for that signal, or on a per-curve/full
    // reset.
    editWarnings: {},
    // "<signal>|<plotKind>" -> {pinned: true, ...plot-specific fields} --
    // the R2 focus-marker PIN state (engram-alpha#2738 round 3). Only
    // pinned markers live here; a hover-only (transient) marker never
    // writes state, so it vanishes on its own re-render/mouseleave. Survives
    // re-render because each plot function checks this map at draw time and
    // redraws its own pinned marker immediately.
    focus: {},
    // R5.3 "set as reference" -- aspect name -> currently STAGED reference
    // signal (or null); seeded from model_defs.aspects[].reference_metric
    // in seedCurveState. state.storedReferences is the as-served value,
    // used for `.changed` diffing and for the reset paths, the same
    // stored-vs-live split state.curves/storedKnots already uses for
    // per-signal knots/importance.
    references: {},
    storedReferences: {},
  };

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
  // engram-alpha#2748 item 4: this file builds most markup via raw string
  // concatenation (not the el() helper above), so an HTML-attribute value
  // sourced from server text (e.g. a snapshot's "reason" string) needs its
  // own escape before landing inside a quoted title= attribute.
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
  // checkpointLabelHTML: R5.1 -- wraps formatCheckpointLabel's plain string
  // in the muted-age span the spec asks for ("append a MUTED (<N> h/d
  // ago)"). formatCheckpointLabel deliberately returns one plain string
  // (see its own comment in health-workbench-curve.js), so the age suffix
  // is located here by its own fixed " (<N> [hd] ago)" trailing pattern
  // rather than the helper returning a structured {main, age} value --
  // JUDGMENT CALL (flagged in the dispatching report): this couples the
  // DOM layer to the pure helper's exact suffix wording, but keeps the
  // helper's contract to the single-string shape the spec asked for.
  function checkpointLabelHTML(provenance) {
    var label = formatCheckpointLabel(provenance, Date.now());
    var m = /^(.*?)( \(\d+ [hd] ago\))$/.exec(label);
    if (m) {
      return m[1] + '<span class="chkpt-age">' + m[2] + "</span>";
    }
    return label;
  }

  function urlAgent(name) {
    return "?agent=" + encodeURIComponent(name);
  }
  function currentAgentFromUrl() {
    var m = /[?&]agent=([^&]+)/.exec(window.location.search);
    return m ? decodeURIComponent(m[1]) : null;
  }

  // switchAgent: engram-alpha#2870 fix 1 -- the single path for switching
  // the whole page to a different seat's view, shared by the #wb-agent
  // <select>'s own change handler and a click on a seat's row in a
  // signal's per-seat comparison table (LEFT column). No-op when the
  // seat is already selected, so clicking the current seat's own row
  // doesn't trigger a redundant refetch. Keeps the <select>'s displayed
  // value in sync -- necessary for the click path (the browser doesn't
  // update the <select> on its own for a click elsewhere on the page);
  // harmless for the change-handler path, where the browser has already
  // updated the <select> itself.
  function switchAgent(name) {
    if (name === state.agent) return;
    state.agent = name;
    var url = new URL(window.location.href);
    url.searchParams.set("agent", state.agent);
    window.history.replaceState(null, "", url.toString());
    qs("#wb-agent").value = state.agent;
    loadForAgent().then(render);
  }

  function fetchJSON(url) {
    return fetch(url).then(function (r) { return r.json(); });
  }

  // ---------------------------------------------------------------------
  // localStorage persistence -- working state only, keyed by signal name.
  // ---------------------------------------------------------------------
  function loadEdits() {
    try {
      var raw = window.localStorage.getItem(LS_KEY);
      if (!raw) return {};
      return JSON.parse(raw) || {};
    } catch (e) {
      return {};
    }
  }
  // mergeDraftsForPersist: pure data transform (no DOM/localStorage
  // access) -- given the current edited/curves/drafts state, produce the
  // object that should be WRITTEN to localStorage (engram-alpha#2870 fix
  // 2 item 3). Two properties this exists to guarantee: (1) a signal with
  // an unloaded draft that the user never touched this session SURVIVES a
  // save triggered by editing some OTHER signal (a full-overwrite keyed
  // only off `edited` would otherwise silently erase it); (2) a signal
  // that is both an unloaded draft AND a live edit (loaded, then further
  // edited) is written from the LIVE `curves` value, never the stale
  // `drafts` copy -- the `edited` pass below runs second so it overwrites
  // the `drafts` pass for the same key. Extracted as a standalone function
  // (rather than inlined in saveEdits()) so it's directly testable under
  // plain `node`, no DOM shim needed -- see
  // tests/test_health_workbench_draft_persist.py.
  function mergeDraftsForPersist(edited, curves, drafts) {
    var out = {};
    Object.keys(drafts || {}).forEach(function (sig) {
      if (!edited || !edited[sig]) out[sig] = drafts[sig];
    });
    Object.keys(edited || {}).forEach(function (sig) {
      if (edited[sig] && curves && curves[sig]) out[sig] = curves[sig];
    });
    return out;
  }
  function saveEdits() {
    var out = mergeDraftsForPersist(state.edited, state.curves, state.drafts);
    try {
      window.localStorage.setItem(LS_KEY, JSON.stringify(out));
    } catch (e) { /* ignore quota errors -- best-effort persistence only */ }
  }

  // ---------------------------------------------------------------------
  // Model / metric helpers
  // ---------------------------------------------------------------------
  function metricsForAspect(aspectName) {
    return (state.model.model_defs.metrics || []).filter(function (m) {
      return m.aspect === aspectName;
    });
  }
  function curveDefFor(metric) {
    return (state.model.curve_defs || {})[metric.curve] || null;
  }
  function metricBySignal(signal) {
    return (state.model.model_defs.metrics || []).filter(function (m) {
      return m.signal === signal;
    })[0] || null;
  }
  function seedCurveState() {
    var persisted = loadEdits();
    // engram-alpha#2870 fix 2: the page must open on the CURRENT shipped
    // curve for every signal -- state.curves/state.edited below are ALWAYS
    // seeded from the shipped model, never from localStorage. A persisted
    // blob is instead stashed in state.drafts (unloaded, session-scoped
    // visibility only), so a stale edit from an earlier session is never
    // silently presented as the curve.
    state.drafts = {};
    (state.model.model_defs.metrics || []).forEach(function (m) {
      var cd = curveDefFor(m);
      var shippedKnots = cd ? cd.knots.map(function (p) { return p.slice(); }) : [[0, 0], [1, 1]];
      state.curves[m.signal] = { knots: shippedKnots, importance: m.importance };
      if (persisted[m.signal]) {
        state.drafts[m.signal] = {
          knots: persisted[m.signal].knots.map(function (p) { return p.slice(); }),
          importance: persisted[m.signal].importance,
        };
      }
      if (!state.histView[m.signal]) state.histView[m.signal] = "raw";
    });
    // R5.3: aspect reference metric, seeded fresh from the shipped model
    // every time (not from localStorage -- JUDGMENT CALL, flagged in the
    // dispatching report: this page's only persistence mechanism today is
    // the per-signal knots/importance blob, and adding a reference to it
    // would require a schema bump to that blob; a staged reference is
    // session-only state here, exactly like state.focus's pins are).
    (state.model.model_defs.aspects || []).forEach(function (a) {
      state.storedReferences[a.name] = a.reference_metric || null;
      state.references[a.name] = a.reference_metric || null;
    });
  }

  // resetAspect: R5.3's "reset aspect" -- restores this aspect's stored
  // reference_metric AND every one of its member metrics' stored
  // knots/importance in one shot. This is also what a per-curve "reset
  // this curve" click on the CURRENTLY-STAGED reference metric's own row
  // does (see renderSignalRow's resetBtn handler below) -- resetting the
  // metric that a "set as reference" rescale pivoted around, without also
  // un-rescaling the siblings it rescaled, would leave the aspect in an
  // inconsistent half-reset state.
  function resetAspect(aspectName) {
    state.references[aspectName] = state.storedReferences[aspectName];
    metricsForAspect(aspectName).forEach(function (m) {
      var cd = curveDefFor(m);
      var shippedKnots = cd ? cd.knots.map(function (p) { return p.slice(); }) : [[0, 0], [1, 1]];
      state.curves[m.signal] = { knots: shippedKnots, importance: m.importance };
      delete state.edited[m.signal];
      delete state.editWarnings[m.signal];
      // engram-alpha#2870 fix 2 item 4: also forget any still-unloaded
      // draft for this signal -- otherwise the merged saveEdits() below
      // would see it absent from `edited` and re-persist it right back,
      // resurrecting exactly what this reset just discarded.
      delete state.drafts[m.signal];
    });
    saveEdits();
    render();
  }

  // setReference: R5.3's "set as reference" click -- rescales every
  // member of `aspectName` (via rescaleToReference, health-workbench-
  // curve.js) so `signal`'s STAGED importance becomes exactly 1.0 and
  // every other admitted member's staged importance is uniformly
  // rescaled by the same factor (hold-outs untouched). No-ops (returns
  // without mutating state) when `signal` isn't eligible (hold-out, or
  // not found) -- rescaleToReference's own null return is the
  // eligibility signal, the same defensive backstop noted in its comment.
  function setReference(aspectName, signal) {
    var members = metricsForAspect(aspectName).map(function (m) {
      return { signal: m.signal, importance: state.curves[m.signal].importance };
    });
    var rescaled = rescaleToReference(members, signal);
    if (!rescaled) return;
    rescaled.forEach(function (r) {
      state.curves[r.signal].importance = r.importance;
      markEdited(r.signal);
      // engram-alpha#2870 fix 2 follow-up (reviewer-fairy suggestion,
      // PR #2875 round 1): symmetric with the two reset paths -- a member
      // that had a still-unloaded draft is now genuinely edited, so forget
      // the stale draft copy rather than leaving it to linger inertly in
      // state.drafts (mergeDraftsForPersist already prefers state.curves
      // for an edited signal, so this was never a live bug, but leaving a
      // dead draft behind is the same asymmetry the two reset paths were
      // fixed to avoid).
      delete state.drafts[r.signal];
    });
    state.references[aspectName] = signal;
    render();
  }

  // score for one seat's raw signal value under the CURRENT (edited) curve
  function scoreFor(signal, rawValue) {
    if (rawValue === null || rawValue === undefined || rawValue === "UNAVAILABLE") return null;
    var c = state.curves[signal];
    if (!c || !c.knots.length) return null;
    return piecewiseLinear(Number(rawValue), c.knots);
  }

  function aspectScoreFor(aspectName, agent) {
    var snap = state.snapshotByAgent[agent];
    if (!snap || !snap.has_snapshot) return null;
    var rowByName = {};
    (snap.signal_rows || []).forEach(function (r) { rowByName[r.name] = r; });
    var members = [];
    metricsForAspect(aspectName).forEach(function (m) {
      if (m.importance <= 0) return; // not admitted
      var row = rowByName[m.signal];
      if (!row) return;
      var raw = row.today ? row.today.raw : null;
      var sc = scoreFor(m.signal, raw);
      if (sc === null) return; // unavailable this run
      members.push({ importance: state.curves[m.signal].importance, curveScore: sc });
    });
    if (!members.length) return null;
    return aspectScore(members);
  }

  function composedScoreFor(agent) {
    var aspects = (state.model.model_defs.aspects || []).map(function (a) {
      return { points: a.points, score: aspectScoreFor(a.name, agent) };
    });
    return { value: composeScore(aspects), aspects: aspects };
  }

  // ---------------------------------------------------------------------
  // SVG helpers (lightweight -- no library)
  // ---------------------------------------------------------------------
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

  // curvesDiffer: array-deep-equal on knot pairs -- used both to decide
  // whether to draw the stored-curve overlay (engram-alpha#2738 round 2,
  // req 7) and, via the caller, whether a knot input gets the `.changed`
  // highlight (req 4).
  function curvesDiffer(a, b) {
    if (!a || !b) return !!a !== !!b;
    if (a.length !== b.length) return true;
    for (var i = 0; i < a.length; i++) {
      if (a[i][0] !== b[i][0] || a[i][1] !== b[i][1]) return true;
    }
    return false;
  }

  // ---------------------------------------------------------------------
  // R2 focus marker -- shared by every plot kind (curve / history / hist).
  // A plot draws into its own `<g class="focus-layer">`, appended last so
  // it paints on top. Hover (mousemove/mouseleave) draws transient state
  // that never touches `state.focus`; click toggles a PIN that is written
  // to `state.focus[key]` and therefore survives the next full re-render
  // (each plot function re-checks `state.focus[key]` at construction time
  // and redraws its pinned marker immediately -- see callers below).
  //
  // Round 4 (engram-alpha#2738 round 4, req R2b/c/d): the readout TEXT
  // moved out of the SVG entirely into an HTML overlay div
  // (`.focus-readout`, absolutely positioned over a `.plot-container`
  // wrapper) -- it was being clipped at the plot's own edges, and its
  // presence forced every plot's viewBox to stay wide enough to hold it.
  // The SVG focus layer now draws ONLY the guide line + dot; see
  // createReadoutEl/updateFocusReadout/hideReadout below.
  // ---------------------------------------------------------------------
  function makeFocusLayer(svgEl) {
    var g = svg("g", { class: "focus-layer" });
    svgEl.appendChild(g);
    return g;
  }
  function clearFocusLayer(g) {
    while (g.firstChild) g.removeChild(g.firstChild);
  }
  // drawFocusAt: vertical guide line the full plot height, plus an
  // optional dot at (xScreen, yScreen). No text -- see the HTML-overlay
  // readout helpers below.
  function drawFocusAt(g, W, H, xScreen, yScreen) {
    clearFocusLayer(g);
    g.appendChild(svg("line", { x1: xScreen, y1: 0, x2: xScreen, y2: H, class: "focus-guide" }));
    if (yScreen !== null && yScreen !== undefined) {
      g.appendChild(svg("circle", { cx: xScreen, cy: yScreen, r: 3.5, class: "focus-dot" }));
    }
  }
  // createReadoutEl: one `.focus-readout` div per plot, appended into the
  // plot's `.plot-container` wrapper AFTER the svg (so it paints on top,
  // by DOM order) -- caller is responsible for creating that wrapper with
  // `position:relative` (the `.plot-container` CSS class does this).
  function createReadoutEl(container) {
    var d = document.createElement("div");
    d.className = "focus-readout";
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
  // updateFocusReadout: position `readoutEl` (an absolutely-positioned
  // HTML div living in `container`) at the SVG user-space point
  // (xUser, yUser), converted to container-relative pixels via the
  // SVG's own live bounding box + viewBox (so it tracks whatever
  // fitViewBox most recently set, with no coupling back the other way --
  // this is what "the readout never needs fitViewBox to re-run on
  // hover" means). `max-width` = container width minus 8px (R2c);
  // flips to the left of the marker past 60% of the container's width
  // (same "near the right edge" rule R2 used for the old SVG-text
  // readout); clamped so it never leaves the container on either axis.
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
  // svgUserPoint: mouse event client coords -> SVG viewBox user-space
  // coords, via the element's live bounding box and its own viewBox (works
  // regardless of the CSS-scaled display size, since `svg{width:100%;
  // height:auto}` keeps the intrinsic aspect ratio, i.e. a uniform scale
  // factor on both axes). Reads `svgEl.viewBox.baseVal` live, so this
  // automatically tracks whatever fitViewBox() most recently set --
  // verified unchanged by the round-4 viewBox-fitting work below.
  function svgUserPoint(svgEl, evt) {
    var rect = svgEl.getBoundingClientRect();
    var vb = svgEl.viewBox.baseVal;
    return {
      x: vb.x + ((evt.clientX - rect.left) / rect.width) * vb.width,
      y: vb.y + ((evt.clientY - rect.top) / rect.height) * vb.height,
    };
  }
  // wireFocusEvents: attach the shared hover/pin/unpin behavior to one
  // plot's SVG. `onPoint(userPt)` computes and draws the marker (SVG guide
  // + dot, HTML readout) for a mouse position; `pinnedRedraw()` (called at
  // construction, before any event fires) redraws a pin already present in
  // `state.focus[key]`. `readoutEl` (round 4) is hidden alongside the SVG
  // focus layer on mouseleave/unpin.
  function wireFocusEvents(svgEl, focusLayer, key, readoutEl, onPoint, pinnedRedraw) {
    if (state.focus[key]) pinnedRedraw();
    svgEl.addEventListener("mousemove", function (evt) {
      if (state.focus[key]) return; // pinned -- hover is a no-op
      onPoint(svgUserPoint(svgEl, evt), false);
    });
    svgEl.addEventListener("mouseleave", function () {
      if (state.focus[key]) return; // pinned marker stays
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
  // Escape unpins every currently-pinned marker (a page-global shortcut,
  // not scoped to "the plot under the mouse" -- flagged as a judgment call
  // in the dispatching report: R2 says "a second click ... or Escape,
  // unpins" without saying which plot Escape targets when several are
  // pinned at once, and clearing all of them is the simplest, least
  // surprising reading).
  function unpinAllFocus() {
    if (!Object.keys(state.focus).length) return;
    state.focus = {};
    render();
  }

  // fitViewBox (round 4, R2b/c/d): after `svgEl` is appended to the live
  // DOM, resize its viewBox to fit its own rendered content (all axis
  // labels, tilted or not) plus `pad` px of margin -- this is what stops
  // tilted date labels being clipped under the stats table (R2b) and a
  // tall bar's % label being clipped above the plot (R2d, for the
  // still-drawn-above case). Deferred to `requestAnimationFrame` because
  // `getBBox()` needs the element actually laid out in the document.
  // Excludes the `.focus-layer` group from the measured bbox (hidden via
  // `display:none` for the duration of the measurement) -- the guide
  // line/dot are transient hover/pin state, not part of the plot's fixed
  // content, and including them would make the fitted viewBox jump around
  // as the mouse moves. Because the page's CSS already sets
  // `svg{width:100%;height:auto}`, growing the viewBox grows the SVG's
  // own rendered height, which pushes any DOM sibling below it (e.g. the
  // stats table) down -- no other layout change needed.
  //
  // The plot functions below still set an initial analytic `viewBox="0 0
  // W H"` (the pre-round-4 fixed dimensions) so the FIRST paint has a
  // sane guess and nothing visually jumps before this async fit lands.
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
        if (bbox.width === 0 && bbox.height === 0) return; // nothing rendered yet
        svgEl.setAttribute(
          "viewBox",
          (bbox.x - pad) + " " + (bbox.y - pad) + " " + (bbox.width + 2 * pad) + " " + (bbox.height + 2 * pad)
        );
      } catch (e) {
        // getBBox() can throw on a detached/non-rendered SVG (e.g. under
        // a headless test harness) -- the analytic first-paint viewBox
        // stands in that case, which is a safe, already-tested fallback.
      }
    });
  }

  function curveSVG(container, signal, storedKnots, inputDomain, outputRange) {
    var c = state.curves[signal];
    var domain = inputDomain || [0, 1];
    var range = outputRange || [0, 1];
    var W = 340, H = 130, px = 42, py = 10, pb = 26;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "curve" });
    container.appendChild(s);
    var sx = function (v) { return px + linearScale(v, domain[0], domain[1], 0, W - px - 8); };
    var sy = function (v) { return H - pb - linearScale(v, range[0], range[1], 0, H - py - pb); };
    var invX = function (xScreen) { return linearScale(xScreen, px, W - 8, domain[0], domain[1]); };

    // R1: the three existing horizontal guide lines, now at the output
    // range's own [lo, mid, hi] rather than a hardcoded [0, 0.5, 1].
    [0, 0.5, 1].forEach(function (f) {
      var yVal = range[0] + f * (range[1] - range[0]);
      s.appendChild(svg("line", { x1: px, y1: sy(yVal), x2: W - 8, y2: sy(yVal), stroke: "#1f3f70" }));
    });

    // R1: axis ticks + small muted numeric labels, x over input_domain, y
    // over output_range.
    computeAxisTicks(range[0], range[1], 5).forEach(function (yv) {
      s.appendChild(svgText(px - 5, sy(yv) + 3, formatAxisNumber(yv), { class: "axis-label", "text-anchor": "end" }));
    });
    computeAxisTicks(domain[0], domain[1], 5).forEach(function (xv) {
      var xp = sx(xv);
      s.appendChild(svg("line", { x1: xp, y1: H - pb, x2: xp, y2: H - pb + 3, stroke: "#8a9bb5" }));
      s.appendChild(svgText(xp, H - pb + 12, formatAxisNumber(xv), { class: "axis-label", "text-anchor": "middle" }));
    });

    var knots = c.knots.slice().sort(function (a, b) { return a[0] - b[0]; });

    // Req 7 (round 2): the version-stored curve, dashed/muted, UNDER the
    // edited curve -- only when this signal's knots actually differ from
    // what was served at page load. Drawn first so the live curve/points
    // paint over it.
    if (storedKnots && curvesDiffer(c.knots, storedKnots)) {
      var stKnots = storedKnots.slice().sort(function (a, b) { return a[0] - b[0]; });
      var stPts = [];
      for (var j = 0; j <= 60; j++) {
        var sxv = domain[0] + (domain[1] - domain[0]) * (j / 60);
        stPts.push(sx(sxv) + "," + sy(piecewiseLinear(sxv, stKnots)));
      }
      s.appendChild(svg("polyline", {
        points: stPts.join(" "), fill: "none", stroke: "#8a9bb5",
        "stroke-width": 1.5, "stroke-dasharray": "5,4", opacity: "0.85",
      }));
    }

    var pts = [];
    for (var i = 0; i <= 60; i++) {
      var x = domain[0] + (domain[1] - domain[0]) * (i / 60);
      pts.push(sx(x) + "," + sy(piecewiseLinear(x, knots)));
    }
    s.appendChild(svg("polyline", { points: pts.join(" "), fill: "none", stroke: "#00d4ff", "stroke-width": 2 }));
    knots.forEach(function (p) {
      s.appendChild(svg("circle", { cx: sx(p[0]), cy: sy(p[1]), r: 3.5, fill: "#1a1a2e", stroke: "#00d4ff", "stroke-width": 1.5 }));
    });

    // R2: focus marker -- hover shows nearest sampled x along the sweep,
    // click pins it. Round 4: readout is two lines ("x = …" / "y = …"),
    // an HTML overlay now rather than SVG text (see updateFocusReadout).
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

  // renderHistogramSVG: R5 -- histogram mode (`hist` = score histogram,
  // `rawhist` = raw histogram), ported from the fixture demo's edge-tick /
  // rotated-label approach (curve-workbench-demo-2026-09-04.html
  // ~L262-276): one tick per bin edge, labels rotated -45deg with
  // text-anchor:end so neighbours never overlap even at 20 bins. Bar
  // labels are now a PERCENTAGE of points in the bin (not a raw count),
  // shown only when the bar is wide enough to hold the label.
  function renderHistogramSVG(container, signal, points, mode) {
    var isRawHist = mode === "rawhist";
    var vals = [];
    points.forEach(function (p) {
      var raw = typeof p.raw === "number" ? p.raw : null;
      var v = isRawHist ? raw : (raw !== null ? scoreFor(signal, raw) : null);
      if (typeof v === "number") vals.push(v);
    });
    var W = 340, H = 130, px = 42, py = 8, pb = 46;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": mode });
    container.appendChild(s);
    if (!vals.length) return s;
    var bins = computeHistogramBins(vals);
    var total = vals.length;
    var nb = bins.counts.length;
    var mx = Math.max.apply(null, bins.counts.concat([1]));
    var bw = (W - px - 8) / nb;

    // R5: y axis in percent -- ticks at 0 and the tallest bin's own %.
    s.appendChild(svgText(px - 5, H - pb + 3, "0%", { class: "axis-label", "text-anchor": "end" }));
    s.appendChild(svgText(px - 5, py + 8, formatBinPercent(mx, total), { class: "axis-label", "text-anchor": "end" }));

    bins.counts.forEach(function (cnt, i) {
      var h = (cnt / mx) * (H - py - pb);
      var barX = px + i * bw + 1;
      var barW = Math.max(1, bw - 2);
      var barY = H - pb - h;
      s.appendChild(svg("rect", {
        x: barX, y: barY, width: barW, height: h,
        fill: "#00d4ff", "fill-opacity": 0.55,
      }));
      // R5: percentage label, only when the bar is wide enough (>= 14px)
      // -- otherwise the R2 hover readout is how you get the value.
      // Round 4 (R2d): when the bar is ALSO tall enough (>= 14px), draw
      // the label INSIDE the bar near its top with a contrasting
      // (dark-on-cyan) fill, instead of above it -- fitViewBox (see
      // above) already stops the above-bar case from being clipped for
      // the tallest bar, but drawing inside for tall bars avoids the
      // label ever crowding the plot's own top edge/tick.
      if (cnt > 0 && barW >= 14) {
        var tallEnough = h >= 14;
        var labelAttrs = { class: "axis-label", "font-size": "9", "text-anchor": "middle" };
        var labelY;
        if (tallEnough) {
          labelAttrs.fill = "#0a1420"; // contrasting fill against the cyan bar
          labelY = barY + 10;
        } else {
          labelY = barY - 3;
        }
        s.appendChild(svgText(barX + barW / 2, labelY, formatBinPercent(cnt, total), labelAttrs));
      }
    });

    // R5: one tick per bin edge, rotated -45, port of the demo's edge
    // formatting -- raw ratio (fmtPct) vs score (2 decimals).
    bins.edges.forEach(function (ev, i) {
      var xp = px + i * bw;
      s.appendChild(svg("line", { x1: xp, y1: H - pb, x2: xp, y2: H - pb + 3, stroke: "#8a9bb5" }));
      var ty = H - pb + 6;
      var label = isRawHist ? fmtPct(ev) : ev.toFixed(2);
      s.appendChild(svgText(xp, ty, label, {
        class: "axis-label", "font-size": "9", "text-anchor": "end",
        transform: "rotate(-45 " + xp + " " + ty + ")",
      }));
    });

    // R2: focus marker -- hover shows the bin under the cursor. Round 4:
    // readout is two lines ("[a, b)" / "n points (p%)"), HTML overlay.
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

  function historySVG(container, signal, agent, points, mode, inputDomain, outputRange) {
    if (mode === "hist" || mode === "rawhist") {
      return renderHistogramSVG(container, signal, points, mode);
    }
    var W = 340, H = 100, px = 38, py = 8, pb = 28;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": mode });
    container.appendChild(s);
    if (!points.length) return s;
    var isScore = mode === "score";

    // R3 fix: x is REAL TIME over the FULL point list (UNAVAILABLE
    // included), not index. UNAVAILABLE points draw a small hollow marker
    // on the baseline at their true x instead of vanishing.
    var times = points.map(function (p) { return Date.parse(p.t); });
    var tMin = Math.min.apply(null, times), tMax = Math.max.apply(null, times);
    if (tMax === tMin) tMax = tMin + 1;
    var rich = points.map(function (p, i) {
      var raw = typeof p.raw === "number" ? p.raw : null;
      var scoreVal = typeof p.score === "number" ? p.score
        : (raw !== null ? scoreFor(signal, raw) : null);
      var v = isScore ? scoreVal : raw;
      return { t: times[i], raw: raw, score: scoreVal, v: (typeof v === "number" ? v : null) };
    });
    var vals = rich.map(function (p) { return p.v; }).filter(function (v) { return v !== null; });
    if (!vals.length) return s;
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    // Round 4 (R2a): flat-range fallback -- an all-equal series (e.g. an
    // all-zero raw rate) no longer plots a hardcoded [-1, 1] (y ticks
    // reading -100% / 0% / 100%, nonsensical for a ratio-scale raw
    // signal). See padFlatRange's own comment for the score-vs-raw split.
    if (hi === lo) {
      var padded = padFlatRange(lo, isScore ? "score" : "raw", inputDomain, outputRange);
      lo = padded[0];
      hi = padded[1];
    }
    var sx = function (t) { return px + linearScale(t, tMin, tMax, 0, W - px - 8); };
    var sy = function (v) { return H - pb - linearScale(v, lo, hi, 0, H - py - pb); };

    // R1: y ticks at lo/mid/hi, formatted like the table (fmtPct for raw,
    // fmtScore for score).
    var fmtY = isScore ? fmtScore : fmtPct;
    [lo, (lo + hi) / 2, hi].forEach(function (yv) {
      s.appendChild(svgText(px - 5, sy(yv) + 3, fmtY(yv), { class: "axis-label", "text-anchor": "end" }));
    });

    // R3: x ticks in viewer-local time, 3-5 ticks, "MM-DD HH:mm" with a
    // "YYYY-" prefix on the first tick and on any year boundary; tilt -45
    // (like R5) when the evenly-spaced ticks would overlap.
    var xTicksMs = computeTimeTicks(tMin, tMax, 5);
    var xLabels = computeTimeTickLabels(xTicksMs);
    var spacingPx = xTicksMs.length > 1 ? (sx(xTicksMs[1]) - sx(xTicksMs[0])) : (W - px - 8);
    var tilt = shouldTiltLabels(spacingPx, xLabels);
    xTicksMs.forEach(function (tms, i) {
      var xp = sx(tms);
      s.appendChild(svg("line", { x1: xp, y1: H - pb, x2: xp, y2: H - pb + 3, stroke: "#8a9bb5" }));
      var ty = H - pb + (tilt ? 6 : 10);
      var attrs = { class: "axis-label", "font-size": "9" };
      if (tilt) {
        attrs["text-anchor"] = "end";
        attrs.transform = "rotate(-45 " + xp + " " + ty + ")";
      } else {
        attrs["text-anchor"] = "middle";
      }
      s.appendChild(svgText(xp, ty, xLabels[i], attrs));
    });

    // R5.4: line SEGMENTS, broken at every UNAVAILABLE point (run length
    // 1 or more) -- this is what makes a lone UNAVAILABLE point between
    // two numeric neighbours render as an actual gap in the line rather
    // than the old single-polyline behavior, which silently bridged
    // straight across it. A segment of exactly one point draws nothing
    // (a 1-point "line" is invisible anyway), which is also correct here.
    var segments = [];
    var currentSeg = [];
    rich.forEach(function (p) {
      if (p.v !== null) {
        currentSeg.push(sx(p.t) + "," + sy(p.v));
      } else if (currentSeg.length) {
        segments.push(currentSeg);
        currentSeg = [];
      }
    });
    if (currentSeg.length) segments.push(currentSeg);
    segments.forEach(function (seg) {
      if (seg.length > 1) {
        s.appendChild(svg("polyline", { points: seg.join(" "), fill: "none", stroke: "#7ecfff", "stroke-width": 1.3 }));
      }
    });

    // R5.4: collapse every maximal run of UNAVAILABLE points into ONE
    // muted hatched band along the baseline -- replaces the old
    // per-point hollow-circle marker (which read as a broken row of "o"
    // on a long UNAVAILABLE run; see collapseUnavailableRuns's own
    // comment in health-workbench-curve.js). A run of length 1 gets NO
    // band (only the line-segment gap above) -- JUDGMENT CALL (flagged in
    // the dispatching report): the spec's own wording draws this
    // distinction explicitly ("renders as a gap in the line with no
    // marker"), so length-1 runs are deliberately excluded from the band
    // loop below, not merely too narrow to render.
    var runs = collapseUnavailableRuns(points).filter(function (r) { return r.n >= 2; });
    var hatchId = "wb-hatch-" + signal.replace(/[^a-zA-Z0-9_-]/g, "_");
    if (runs.length) {
      var defs = svg("defs", {});
      var pattern = svg("pattern", {
        id: hatchId, width: 4, height: 4, patternUnits: "userSpaceOnUse",
        patternTransform: "rotate(45)",
      });
      pattern.appendChild(svg("rect", { width: 4, height: 4, fill: "#0f3460" }));
      pattern.appendChild(svg("line", { x1: 0, y1: 0, x2: 0, y2: 4, stroke: "#8a9bb5", "stroke-width": 2 }));
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
        fill: "url(#" + hatchId + ")", stroke: "#8a9bb5", "stroke-width": 0.5,
        class: "unavailable-band",
      }));
      if (b.x1 - b.x0 >= 60) {
        s.appendChild(svgText((b.x0 + b.x1) / 2, H - pb - 8, "no measurement · " + b.run.n + " checkpoints", {
          class: "axis-label", "font-size": "8", "text-anchor": "middle",
        }));
      }
    });

    // "latest point" highlight -- the latest point that actually has a
    // plotted value (an UNAVAILABLE tail would otherwise leave nothing to
    // highlight).
    var lastNumeric = null;
    for (var i = rich.length - 1; i >= 0; i--) {
      if (rich[i].v !== null) { lastNumeric = rich[i]; break; }
    }
    if (lastNumeric) {
      s.appendChild(svg("circle", { cx: sx(lastNumeric.t), cy: sy(lastNumeric.v), r: 3, fill: "#00d4ff" }));
    }

    // R2: focus marker -- nearest data point by x-distance in pixels.
    // Round 4: readout is THREE fixed lines (local date-time / raw /
    // score), an HTML overlay -- fmtPct/fmtScore already render "--" for
    // a null/UNAVAILABLE value.
    //
    // R5.4: UNAVAILABLE points no longer have their own per-point
    // readout (NO per-point markers, per spec) -- xsScreenForHover
    // carries `null` in place of an unavailable point's x so
    // nearestIndexByValue (which already skips null entries) never snaps
    // to one; hovering a band instead is handled by findBandAt/showBand
    // below, sharing the SAME focus key/pin slot (only one of a point or
    // a band can be focused on a given plot at a time).
    var focusLayer = makeFocusLayer(s);
    var readoutEl = createReadoutEl(container);
    var key = signal + "|history-" + mode;
    var xsScreenForHover = rich.map(function (p) { return p.v !== null ? sx(p.t) : null; });
    function readoutLines(p) {
      return [
        formatLocalDateTime(p.t),
        "raw " + fmtPct(p.raw),
        "score " + fmtScore(p.score),
      ];
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

  // ---------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------
  function render() {
    if (!state.model || !state.model.available) {
      qs("#wb-empty").textContent = state.model
        ? "Curve Workbench model data unavailable: " + (state.model.reason || "unknown reason")
        : "Loading Curve Workbench data...";
      return;
    }
    var snap = state.snapshotByAgent[state.agent];
    if (!snap) return;

    qs("#wb-status").innerHTML = snap.has_snapshot
      ? checkpointLabelHTML(snap.provenance)
      : "no snapshot for this seat yet: " + (snap.reason || "");

    if (!snap.has_snapshot) {
      qs("#wb-empty").style.display = "";
      qs("#wb-empty").textContent = "No health-v2 snapshot for " + state.agent + " yet -- " + (snap.reason || "");
      qs("#wb-aspects").innerHTML = "";
      qs("#wb-strip").style.display = "none";
      qs("#wb-export-section").style.display = "none";
      return;
    }
    qs("#wb-empty").style.display = "none";
    qs("#wb-strip").style.display = "";
    qs("#wb-export-section").style.display = "";

    var composed = composedScoreFor(state.agent);
    qs("#wb-composed").textContent = composed.value === null ? "--" : composed.value.toFixed(1);
    var pip = (state.model.model_defs.aspects || [])
      .filter(function (a, i) { return composed.aspects[i].score !== null; })
      .reduce(function (acc, a) { return acc + a.points; }, 0);
    qs("#wb-pip").textContent = pip;
    qs("#wb-bandmark").style.left = Math.max(0, Math.min(100, composed.value || 0)) + "%";

    var root = qs("#wb-aspects");
    root.innerHTML = "";
    var rowByName = {};
    (snap.signal_rows || []).forEach(function (r) { rowByName[r.name] = r; });

    (state.model.model_defs.aspects || []).forEach(function (aspect, ai) {
      var sec = el("section", { class: "aspect" });
      var members = metricsForAspect(aspect.name);
      var aScore = composed.aspects[ai].score;
      var head = el("div", { class: "aspect-head" });
      head.appendChild(el("h2", { html: aspect.name }));
      // R5.3: the reference line reads the STAGED value (state.references),
      // not the model's own aspect.reference_metric -- and gets `.changed`
      // when it differs from the stored value, same convention as the
      // knot/importance `.changed` highlighting elsewhere on this page.
      var stagedRef = state.references[aspect.name];
      var refChanged = stagedRef !== state.storedReferences[aspect.name];
      var subWrap = el("div", { class: "sub-wrap" });
      subWrap.appendChild(el("span", {
        class: "sub" + (refChanged ? " changed" : ""),
        html: members.length + " metric(s) declared &middot; reference: " +
          (stagedRef || "none"),
      }));
      var resetAspectBtn = el("button", { type: "button", class: "kbtn" });
      resetAspectBtn.textContent = "reset aspect";
      resetAspectBtn.title = "restore this aspect's stored reference metric + all member importances";
      resetAspectBtn.addEventListener("click", function () { resetAspect(aspect.name); });
      subWrap.appendChild(resetAspectBtn);
      head.appendChild(subWrap);
      var tile = el("div", { class: "atile" });
      tile.appendChild(el("span", { class: "big", html: aScore === null ? "--" : aScore.toFixed(3) }));
      tile.appendChild(el("span", {
        class: "pts",
        html: "&rarr; " + (aScore === null ? "--" : (aScore * aspect.points).toFixed(1)) + " / " + aspect.points + " pts",
      }));
      tile.appendChild(el("span", { class: "how", html: state.agent }));
      head.appendChild(tile);
      sec.appendChild(head);

      members.forEach(function (m) {
        sec.appendChild(renderSignalRow(m, rowByName[m.signal]));
      });
      root.appendChild(sec);
    });

    refreshExportBox();
  }

  function renderSignalRow(metric, row) {
    var signal = metric.signal;
    var held = metric.importance <= 0;
    var wrap = el("div", { class: "signal" + (held ? " held" : "") });
    // Hoisted up from the RIGHT column (round 4): the MIDDLE column's
    // history plot now also needs the curve's declared input_domain /
    // output_range, for the flat-range fallback (padFlatRange).
    var cd = curveDefFor(metric);

    // ---- LEFT: definition + per-seat table + raw focus block ----
    var left = el("div", {});
    left.appendChild(el("div", {
      class: "sig-name",
      html: signal + ' <span class="pill' + (held ? " held" : "") + '">' +
        (held ? "measured &middot; not scored" : "aspect: " + metric.aspect) + "</span>",
    }));
    if (row && row.definition) {
      left.appendChild(el("div", { class: "def", html: row.definition.formula || "" }));
      var num = row.definition.numerator, den = row.definition.denominator;
      var haveCounts = num && den && num.value !== null && num.value !== undefined
        && den.value !== null && den.value !== undefined;
      left.appendChild(el("div", {
        class: "mono", style: "font-size:12px;color:var(--muted)",
        html: haveCounts
          ? 'raw = <span class="frac"><span>' + fmtNum(num.value) + "</span><span>" + fmtNum(den.value) + "</span></span>"
          : "raw counts unavailable in this series/snapshot payload",
      }));
    } else {
      left.appendChild(el("div", { class: "def sub", html: "No definition available for this signal in the current snapshot." }));
    }

    var seatRows = "";
    state.agents.forEach(function (a) {
      var s = state.snapshotByAgent[a.name];
      var r = s && s.has_snapshot ? (s.signal_rows || []).filter(function (x) { return x.name === signal; })[0] : null;
      var raw = r && r.today ? r.today.raw : null;
      var sc = scoreFor(signal, raw);
      // engram-alpha#2748 item 4: no-snapshot seats render a bare "--" with
      // no way to tell "no snapshot yet" from "not readable from this
      // process" (relevant on a multi-UID host) -- carry whatever reason
      // the /api/health/v2 payload exposes for THIS seat (s.reason, same
      // field the current-agent path already surfaces at L882/L886 above)
      // as a title= attribute on the cell, so a hover reveals which case
      // it is instead of both reading identically.
      var noSnapTitle = (!s || !s.has_snapshot)
        ? ' title="' + escAttr((s && s.reason) || "no snapshot for this seat") + '"'
        : "";
      seatRows += '<tr class="seat-row' + (a.name === state.agent ? " sel" : "") +
        '" data-agent="' + escAttr(a.name) + '"><td>' + escAttr(a.name) +
        '</td><td class="num"' + noSnapTitle + '>' +
        (raw === null || raw === undefined || raw === "UNAVAILABLE" ? "--" : fmtPct(raw)) +
        '</td><td class="num">' + fmtScore(sc) + "</td></tr>";
    });
    var table = el("table", {
      html: "<thead><tr><th>seat</th><th style='text-align:right'>raw</th><th style='text-align:right'>score</th></tr></thead><tbody>" + seatRows + "</tbody>",
    });
    // engram-alpha#2870 fix 1: one delegated click listener per render
    // (not per-row) -- clicking any seat's row switches the whole page to
    // that seat, same as picking it in the #wb-agent <select>. Re-created
    // fresh on every render() call, consistent with every other
    // button/control in this file.
    table.addEventListener("click", function (e) {
      var tr = e.target.closest("tr[data-agent]");
      if (tr) switchAgent(tr.getAttribute("data-agent"));
    });
    left.appendChild(table);

    var focusRaw = row && row.today ? row.today.raw : null;
    var focusScore = scoreFor(signal, focusRaw);
    var num = row && row.definition ? row.definition.numerator : null;
    var den = row && row.definition ? row.definition.denominator : null;
    var haveFocusCounts = num && den && num.value !== null && num.value !== undefined
      && den.value !== null && den.value !== undefined;
    var focus = el("div", { class: "focus" });
    var focusSnap = state.snapshotByAgent[state.agent];
    focus.appendChild(el("div", {
      class: "who",
      html: state.agent + " &middot; " + checkpointLabelHTML(focusSnap && focusSnap.provenance),
    }));
    focus.appendChild(el("div", {
      class: "raw",
      html: haveFocusCounts
        ? fmtNum(num.value) + " / " + fmtNum(den.value) + "<small>" + (num.label || "") + " / " + (den.label || "") + "</small>"
        : "unavailable<small>numerator/denominator not carried by this snapshot</small>",
    }));
    focus.appendChild(el("div", { class: "sc", html: "<small>score</small>" + fmtScore(focusScore) }));
    left.appendChild(focus);

    // ---- MIDDLE: history + distribution ----
    var mid = el("div", {});
    mid.appendChild(el("div", { class: "lbl", html: "History &amp; distribution &middot; " + state.agent }));
    var seg = el("span", { class: "seg" });
    [["raw", "raw over time"], ["rawhist", "raw histogram"], ["score", "score over time"], ["hist", "score histogram"]].forEach(function (pair) {
      var b = el("button", { type: "button", class: state.histView[signal] === pair[0] ? "on" : "" });
      b.textContent = pair[1];
      b.addEventListener("click", function () { state.histView[signal] = pair[0]; render(); });
      seg.appendChild(b);
    });
    mid.appendChild(seg);
    var histHost = el("div", { class: "plot-container" });
    mid.appendChild(histHost);

    var seriesKey = state.agent + "|" + signal;
    var series = state.seriesByAgentSignal[seriesKey];
    var points = series && series.signals && series.signals[signal] ? series.signals[signal] : [];
    if (series && series.error) {
      mid.appendChild(el("div", { class: "sub", html: "History unavailable: " + series.error }));
    } else if (!points.length) {
      mid.appendChild(el("div", { class: "sub", html: "No recorded history points for this signal yet." }));
    } else {
      // engram-alpha#2896 item 1: raw points can exist alongside
      // scores_available === false (the raw-L0-fallback case #2869/#2891
      // shipped, for a CURRENT version that has never been through
      // `derive`) -- surface that explicitly instead of silently plotting
      // a scoreless curve with no explanation. Not gated on !points.length
      // above -- this is exactly the case where points DO exist. Same
      // phrasing pattern as health-detail.js's #detail-scores-note (#2896
      // item 2) so a reader sees consistent language on both surfaces.
      if (series.scores_available === false) {
        mid.appendChild(el("div", {
          class: "sub",
          html: "no derived rows for " + series.version + " -- run derive -- showing raw values.",
        }));
      }
      var histEl = historySVG(histHost, signal, state.agent, points, state.histView[signal],
        cd ? cd.input_domain : null, cd ? cd.output_range : null);
      fitViewBox(histEl, 4);
      var rawVals = points.map(function (p) { return typeof p.raw === "number" ? p.raw : null; }).filter(function (v) { return v !== null; });
      var scoreVals = rawVals.map(function (v) { return scoreFor(signal, v); }).filter(function (v) { return v !== null; });
      var rStats = computeDistributionStats(rawVals);
      var sStats = computeDistributionStats(scoreVals);
      var cols = ["min", "p10", "p25", "p50", "p75", "p90", "max", "mean", "sd"];
      if (rStats && sStats) {
        var thead = "<thead><tr><th></th>" + cols.map(function (c) { return "<th style='text-align:right'>" + c + "</th>"; }).join("") + "</tr></thead>";
        var rowRaw = "<tr><td>raw</td>" + cols.map(function (c) { return "<td class='num'>" + fmtPct(rStats[c]) + "</td>"; }).join("") + "</tr>";
        var rowScore = "<tr><td>score</td>" + cols.map(function (c) { return "<td class='num'>" + sStats[c].toFixed(3) + "</td>"; }).join("") + "</tr>";
        mid.appendChild(el("table", { class: "statst", html: thead + "<tbody>" + rowRaw + rowScore + "</tbody>" }));
      }
    }

    // ---- RIGHT: curve editor + importance + decisions ----
    var storedKnots = cd ? cd.knots : null;
    var storedImportance = metric.importance;

    var right = el("div", {});
    right.appendChild(el("div", { class: "lbl", html: "Curve &middot; raw &rarr; score, editable knots" }));

    // engram-alpha#2870 fix 2 item 2: an UNLOADED draft (present in
    // state.drafts, not yet copied into state.curves/state.edited) is
    // surfaced here instead of silently applied -- once loaded it becomes
    // an ordinary edit and this banner's job is done (the existing
    // `.changed` highlighting + "reset this curve" button already cover
    // it from there).
    if (state.drafts[signal] && !state.edited[signal]) {
      var draftBanner = el("div", { class: "draft-banner" });
      draftBanner.appendChild(document.createTextNode(
        "local draft from an earlier session (not shown) "
      ));
      var loadDraftBtn = el("button", { type: "button", class: "kbtn" });
      loadDraftBtn.textContent = "load draft";
      loadDraftBtn.addEventListener("click", function () {
        state.curves[signal] = {
          knots: state.drafts[signal].knots.map(function (p) { return p.slice(); }),
          importance: state.drafts[signal].importance,
        };
        state.edited[signal] = true;
        saveEdits();
        render();
      });
      draftBanner.appendChild(loadDraftBtn);
      draftBanner.appendChild(document.createTextNode(" "));
      var discardDraftBtn = el("button", { type: "button", class: "kbtn" });
      discardDraftBtn.textContent = "discard";
      discardDraftBtn.addEventListener("click", function () {
        delete state.drafts[signal];
        saveEdits();
        render();
      });
      draftBanner.appendChild(discardDraftBtn);
      right.appendChild(draftBanner);
    }

    var curveHost = el("div", { class: "plot-container" });
    right.appendChild(curveHost);
    var curveEl = curveSVG(curveHost, signal, storedKnots, cd ? cd.input_domain : null, cd ? cd.output_range : null);
    fitViewBox(curveEl, 4);

    // R1 (round 4): a per-knot "+" replaces the old single global "+
    // knot" control -- inserting after knot i via insertMidKnot() is
    // curve-preserving (the new knot sits exactly on the curve as
    // already edited), unlike the old global button's fixed midpoint
    // insertion, which could visibly kink the curve. Runs through
    // validateKnots like any other edit.
    var knotsWrap = el("div", { class: "knots" });
    state.curves[signal].knots.forEach(function (p, i) {
      var label = el("label", {});
      label.appendChild(document.createTextNode("k" + i + " "));
      var storedP = storedKnots && storedKnots[i];
      var xChanged = !storedP || p[0] !== storedP[0];
      var yChanged = !storedP || p[1] !== storedP[1];
      var xInput = el("input", {
        value: p[0], title: "raw x", "data-sig": signal, "data-i": i, "data-c": "0",
        class: xChanged ? "changed" : "",
      });
      var yInput = el("input", {
        value: p[1], title: "score y", "data-sig": signal, "data-i": i, "data-c": "1",
        class: yChanged ? "changed" : "",
      });
      label.appendChild(xInput);
      label.appendChild(document.createTextNode(" "));
      label.appendChild(yInput);
      var delBtn = el("button", {
        type: "button", class: "kbtn", title: "remove this knot",
      });
      delBtn.textContent = "×";
      if (state.curves[signal].knots.length <= 2) delBtn.disabled = true;
      delBtn.addEventListener("click", function () {
        var k = state.curves[signal].knots;
        if (k.length > 2) { k.splice(i, 1); markEdited(signal); render(); }
      });
      label.appendChild(delBtn);
      var domainForInsert = cd ? cd.input_domain : null;
      var candidateInsert = insertMidKnot(state.curves[signal].knots, i, domainForInsert);
      if (candidateInsert) {
        var plusBtn = el("button", {
          type: "button", class: "kbtn add", title: "insert a knot here (curve-preserving)",
        });
        plusBtn.textContent = "+";
        plusBtn.addEventListener("click", function () {
          var cur = insertMidKnot(state.curves[signal].knots, i, domainForInsert);
          if (!cur) return; // domain/edit state moved under us -- no-op rather than stale insert
          var vresult = validateKnots(cur, domainForInsert, cd ? cd.output_range : null);
          if (!vresult.ok) {
            state.editWarnings[signal] = vresult.reason;
            render();
            return;
          }
          delete state.editWarnings[signal];
          state.curves[signal].knots = cur;
          markEdited(signal);
          render();
        });
        label.appendChild(plusBtn);
      }
      knotsWrap.appendChild(label);
    });
    right.appendChild(knotsWrap);
    right.appendChild(el("div", {
      class: "sub", style: "font-size:11px;margin-top:4px",
      html: "Between knots: linear. Outside the first/last knot: held constant (clamp-to-endpoint, ported line-for-line from health.curves.piecewise_linear -- see health-workbench-curve.js). x must stay inside "
        + (cd ? "[" + cd.input_domain[0] + ", " + cd.input_domain[1] + "]" : "the declared input domain")
        + ", y inside "
        + (cd ? "[" + cd.output_range[0] + ", " + cd.output_range[1] + "]" : "the declared output range")
        + " -- refused and reverted at entry otherwise.",
    }));

    var recWrap = el("div", { class: "rec" });
    recWrap.appendChild(el("span", { class: "k", html: "importance" }));
    var impSpan = el("span", { class: "imp" });
    var impChanged = state.curves[signal].importance !== storedImportance;
    var isCurrentRefForImp = state.references[metric.aspect] === signal;
    var impAttrs = {
      type: "number", step: "1", value: state.curves[signal].importance, "data-imp": signal,
      class: impChanged ? "changed" : "",
    };
    if (held) { impAttrs.disabled = "disabled"; }
    if (isCurrentRefForImp) {
      // R5.3: the reference metric's importance is read-only WHILE it is
      // the reference -- it's derived (always 1.0 by rescaleToReference's
      // own construction), not independently tunable; "reset this curve"
      // (via resetAspect, above) is how to un-set it.
      impAttrs.readonly = "readonly";
      impAttrs.title = "reference metric is the unit";
    }
    var impInput = el("input", impAttrs);
    impSpan.appendChild(impInput);
    impSpan.appendChild(document.createTextNode(" within " + metric.aspect));
    if (held) {
      impSpan.appendChild(el("span", {
        class: "hold-lock",
        html: "locked &middot; hold-out (stored importance 0 -- dream-eligibility state, not a tuning factor)",
      }));
    }
    recWrap.appendChild(impSpan);
    right.appendChild(recWrap);

    // R5.3: "set as reference" -- one of three renders per row (button /
    // "hold-out metric" / "current reference"). Eligibility is `!held`
    // (importance > 0) AND not already the aspect's currently-staged
    // reference.
    var isCurrentRef = state.references[metric.aspect] === signal;
    var refCtl = el("div", { class: "refctl" });
    if (held) {
      refCtl.textContent = "hold-out metric";
    } else if (isCurrentRef) {
      refCtl.textContent = "current reference";
    } else {
      var refBtn = el("button", { type: "button", class: "kbtn" });
      refBtn.textContent = "set as reference";
      refBtn.title = "rescale this aspect's importances so this metric's importance is 1.0, aspect score unchanged";
      refBtn.addEventListener("click", function () { setReference(metric.aspect, signal); });
      refCtl.appendChild(refBtn);
    }
    right.appendChild(refCtl);

    var resetRow = el("div", { class: "resetcurve" });
    var resetBtn = el("button", { type: "button", class: "kbtn" });
    resetBtn.textContent = "reset this curve";
    resetBtn.title = "discard edits (knots + importance) on this one metric only";
    resetBtn.addEventListener("click", function () {
      // R5.3: resetting the row that is CURRENTLY STAGED as the aspect's
      // reference must undo the whole rescale it caused, not just this
      // one signal's own knots/importance -- see resetAspect's own
      // comment for why a partial reset here would be inconsistent.
      if (state.references[metric.aspect] === signal) {
        resetAspect(metric.aspect);
        return;
      }
      state.curves[signal] = {
        knots: storedKnots ? storedKnots.map(function (p) { return p.slice(); }) : [[0, 0], [1, 1]],
        importance: storedImportance,
      };
      delete state.edited[signal];
      delete state.editWarnings[signal];
      // engram-alpha#2870 fix 2 item 4: see resetAspect's own comment --
      // without this, the merged saveEdits() below would re-persist a
      // still-unloaded draft for this signal right back into localStorage.
      delete state.drafts[signal];
      saveEdits();
      render();
    });
    resetRow.appendChild(resetBtn);
    right.appendChild(resetRow);

    if (state.editWarnings[signal]) {
      right.appendChild(el("div", { class: "warn-msg", html: state.editWarnings[signal] }));
    }

    right.appendChild(el("div", {
      class: "verdict",
      html: '<span class="pill held">untested</span> No known-bad-day anchors are recorded anywhere in this data model yet -- the incident test this row will eventually run (does a known-bad day score visibly below its neighbours?) has nothing to check against. Shown as a reserved slot, not fabricated.',
    }));

    wrap.appendChild(left);
    wrap.appendChild(mid);
    wrap.appendChild(right);
    return wrap;
  }

  function markEdited(signal) {
    state.edited[signal] = true;
    saveEdits();
  }

  // ---------------------------------------------------------------------
  // Export decisions
  // ---------------------------------------------------------------------
  function buildExportPayload() {
    var out = {};
    // engram-alpha#2748 item 3: per-signal entries nest under "signals",
    // mirroring health/series.py's export() shape ({aspects, composed,
    // replay_decl_hash, scores_available, signals, version} -- aspects and
    // signals as siblings) instead of sitting at the top level beside
    // "aspects" -- a signal literally named "aspects" (or any future
    // top-level block name) would otherwise be silently clobbered.
    var signalsOut = {};
    Object.keys(state.edited).forEach(function (sig) {
      if (state.edited[sig] && state.curves[sig]) {
        signalsOut[sig] = {
          knots: state.curves[sig].knots.map(function (p) { return [p[0], p[1]]; }),
          importance: state.curves[sig].importance,
        };
      }
    });
    if (Object.keys(signalsOut).length) out.signals = signalsOut;
    // R5.3: top-level "aspects" block, sibling of "signals" above -- one
    // entry per aspect whose STAGED reference differs from stored. Every
    // metric rescaled by "set as reference" is separately captured as its
    // own per-signal entry above (via markEdited), so this block carries
    // only the reference-CHOICE itself.
    var aspectsOut = {};
    (state.model && state.model.model_defs && state.model.model_defs.aspects || []).forEach(function (a) {
      var staged = state.references[a.name];
      var stored = state.storedReferences[a.name];
      if (staged !== stored) {
        aspectsOut[a.name] = { reference_metric: staged || null };
      }
    });
    if (Object.keys(aspectsOut).length) out.aspects = aspectsOut;
    return out;
  }
  function refreshExportBox() {
    qs("#wb-export-json").value = JSON.stringify(buildExportPayload(), null, 2);
  }

  // ---------------------------------------------------------------------
  // Event delegation for the dynamically-rendered knot/importance inputs.
  // ---------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") unpinAllFocus();
    });
    qs("#wb-aspects").addEventListener("change", function (e) {
      var t = e.target;
      if (t.dataset && t.dataset.sig) {
        var sig = t.dataset.sig;
        var v = parseFloat(t.value);
        if (isNaN(v)) { render(); return; } // not a number -- revert the DOM to state
        var candidate = state.curves[sig].knots.map(function (p) { return p.slice(); });
        candidate[+t.dataset.i][+t.dataset.c] = v;
        var metric = metricBySignal(sig);
        var cd = metric ? curveDefFor(metric) : null;
        var result = validateKnots(candidate, cd ? cd.input_domain : null, cd ? cd.output_range : null);
        if (!result.ok) {
          state.editWarnings[sig] = result.reason;
          render(); // re-render from unchanged state -- reverts the field
          return;
        }
        delete state.editWarnings[sig];
        state.curves[sig].knots = candidate;
        markEdited(sig);
        render();
      } else if (t.dataset && t.dataset.imp) {
        var impSig = t.dataset.imp;
        var iv = parseFloat(t.value);
        if (isNaN(iv)) { render(); return; }
        var impMetric = metricBySignal(impSig);
        var storedImp = impMetric ? impMetric.importance : 0;
        var impResult = validateImportance(iv, storedImp);
        if (!impResult.ok) {
          state.editWarnings[impSig] = impResult.reason;
          render();
          return;
        }
        delete state.editWarnings[impSig];
        state.curves[impSig].importance = iv;
        markEdited(impSig);
        render();
      }
    });

    qs("#wb-export-refresh").addEventListener("click", refreshExportBox);
    qs("#wb-export-download").addEventListener("click", function () {
      var blob = new Blob([JSON.stringify(buildExportPayload(), null, 2)], { type: "application/json" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "curve-workbench-decisions.json";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    });
    qs("#wb-export-copy").addEventListener("click", function () {
      refreshExportBox();
      var box = qs("#wb-export-json");
      box.select();
      try { document.execCommand("copy"); } catch (e) { /* clipboard API may be unavailable */ }
    });
    qs("#wb-export-reset").addEventListener("click", function () {
      if (!window.confirm("Reset every edited curve/importance back to the shipped model?")) return;
      state.edited = {};
      state.editWarnings = {};
      try { window.localStorage.removeItem(LS_KEY); } catch (e) { /* best-effort */ }
      seedCurveState();
      render();
    });

    qs("#wb-agent").addEventListener("change", function (e) {
      switchAgent(e.target.value);
    });

    init();
  });

  function loadForAgent() {
    var agent = state.agent;
    var signalNames = (state.model.model_defs.metrics || []).map(function (m) { return m.signal; });
    var snapPromise = fetchJSON("/api/health/v2" + urlAgent(agent)).then(function (data) {
      state.snapshotByAgent[agent] = data;
    }).catch(function (e) {
      console.warn("workbench: failed to load snapshot for seat " + agent, e);
    });
    var seatPromises = state.agents.map(function (a) {
      if (state.snapshotByAgent[a.name]) return Promise.resolve();
      return fetchJSON("/api/health/v2" + urlAgent(a.name)).then(function (data) {
        state.snapshotByAgent[a.name] = data;
      }).catch(function (e) {
        console.warn("workbench: failed to load snapshot for seat " + a.name, e);
      });
    });
    var seriesPromises = signalNames.map(function (sig) {
      var key = agent + "|" + sig;
      if (state.seriesByAgentSignal[key]) return Promise.resolve();
      return fetchJSON("/api/health/v2/series?agent=" + encodeURIComponent(agent) + "&signal=" + encodeURIComponent(sig))
        .then(function (data) { state.seriesByAgentSignal[key] = data; })
        .catch(function (e) {
          console.warn("workbench: failed to load series for signal " + sig, e);
        });
    });
    return Promise.all([snapPromise].concat(seatPromises).concat(seriesPromises));
  }

  function init() {
    var urlAgentName = currentAgentFromUrl();
    fetchJSON("/api/health/v2/model").then(function (modelResp) {
      state.model = modelResp;
      if (!modelResp.available) { render(); return; }
      seedCurveState();
      return fetchJSON("/api/agents").then(function (agentsResp) {
        state.agents = agentsResp.agents || [];
        state.agent = urlAgentName || agentsResp.default || (state.agents[0] && state.agents[0].name);
        var sel = qs("#wb-agent");
        sel.innerHTML = state.agents.map(function (a) {
          return '<option value="' + a.name + '"' + (a.name === state.agent ? " selected" : "") + ">" + (a.label || a.name) + "</option>";
        }).join("");
        return loadForAgent();
      }).then(render);
    });
  }
})();
