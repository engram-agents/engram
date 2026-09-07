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
    histView: {}, // signal -> 'raw'|'rawhist'|'score'|'hist'
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
  function fmtScore(v) {
    if (v === null || v === undefined || v === "UNAVAILABLE") return "--";
    return Number(v).toFixed(3);
  }
  function fmtPct(v) {
    if (v === null || v === undefined || v === "UNAVAILABLE" || isNaN(v)) return "--";
    return (Number(v) * 100).toFixed(2) + "%";
  }
  function urlAgent(name) {
    return "?agent=" + encodeURIComponent(name);
  }
  function currentAgentFromUrl() {
    var m = /[?&]agent=([^&]+)/.exec(window.location.search);
    return m ? decodeURIComponent(m[1]) : null;
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
  function saveEdits() {
    var out = {};
    Object.keys(state.edited).forEach(function (sig) {
      if (state.edited[sig] && state.curves[sig]) out[sig] = state.curves[sig];
    });
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
  function seedCurveState() {
    var persisted = loadEdits();
    (state.model.model_defs.metrics || []).forEach(function (m) {
      var cd = curveDefFor(m);
      var shippedKnots = cd ? cd.knots.map(function (p) { return p.slice(); }) : [[0, 0], [1, 1]];
      if (persisted[m.signal]) {
        state.curves[m.signal] = {
          knots: persisted[m.signal].knots.map(function (p) { return p.slice(); }),
          importance: persisted[m.signal].importance,
        };
        state.edited[m.signal] = true;
      } else {
        state.curves[m.signal] = { knots: shippedKnots, importance: m.importance };
      }
      if (!state.histView[m.signal]) state.histView[m.signal] = "raw";
    });
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

  function curveSVG(signal) {
    var c = state.curves[signal];
    var W = 340, H = 130, px = 34, py = 10, pb = 20;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "curve" });
    var sx = function (v) { return px + v * (W - px - 8); };
    var sy = function (v) { return H - pb - v * (H - py - pb); };
    [0, 0.5, 1].forEach(function (y) {
      var l = svg("line", { x1: px, y1: sy(y), x2: W - 8, y2: sy(y), stroke: "#1f3f70" });
      s.appendChild(l);
    });
    var knots = c.knots.slice().sort(function (a, b) { return a[0] - b[0]; });
    var pts = [];
    for (var i = 0; i <= 60; i++) {
      var x = i / 60;
      pts.push(sx(x) + "," + sy(piecewiseLinear(x, knots)));
    }
    s.appendChild(svg("polyline", { points: pts.join(" "), fill: "none", stroke: "#00d4ff", "stroke-width": 2 }));
    knots.forEach(function (p) {
      s.appendChild(svg("circle", { cx: sx(p[0]), cy: sy(p[1]), r: 3.5, fill: "#1a1a2e", stroke: "#00d4ff", "stroke-width": 1.5 }));
    });
    return s;
  }

  function historySVG(signal, agent, points, mode) {
    var W = 340, H = 100, px = 30, py = 8, pb = 16;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": mode });
    if (!points.length) return s;
    var isScore = mode === "score" || mode === "hist";
    var vals = points.map(function (p) {
      var v = isScore ? scoreFor(signal, p.raw) : p.raw;
      return typeof v === "number" ? v : null;
    }).filter(function (v) { return v !== null; });
    if (!vals.length) return s;
    if (mode === "hist" || mode === "rawhist") {
      var bins = computeHistogramBins(vals);
      var mx = Math.max.apply(null, bins.counts.concat([1]));
      var nb = bins.counts.length;
      var bw = (W - px - 8) / nb;
      bins.counts.forEach(function (cnt, i) {
        var h = (cnt / mx) * (H - py - pb);
        s.appendChild(svg("rect", {
          x: px + i * bw + 1, y: H - pb - h, width: Math.max(1, bw - 2), height: h,
          fill: "#00d4ff", "fill-opacity": 0.55,
        }));
      });
      return s;
    }
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi === lo) { hi += 1; lo -= 1; }
    var sx = function (i) { return px + i * (W - px - 8) / Math.max(1, vals.length - 1); };
    var sy = function (v) { return H - pb - ((v - lo) / (hi - lo)) * (H - py - pb); };
    var linePts = vals.map(function (v, i) { return sx(i) + "," + sy(v); }).join(" ");
    s.appendChild(svg("polyline", { points: linePts, fill: "none", stroke: "#7ecfff", "stroke-width": 1.3 }));
    s.appendChild(svg("circle", { cx: sx(vals.length - 1), cy: sy(vals[vals.length - 1]), r: 3, fill: "#00d4ff" }));
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

    qs("#wb-status").textContent = snap.has_snapshot
      ? "snapshot " + (snap.provenance && snap.provenance.snapshot_id || "")
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
      head.appendChild(el("span", {
        class: "sub", html: members.length + " metric(s) declared &middot; reference: " +
          (aspect.reference_metric || "none"),
      }));
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
      seatRows += '<tr class="' + (a.name === state.agent ? "sel" : "") + '"><td>' + a.name +
        '</td><td class="num">' + (raw === null || raw === undefined || raw === "UNAVAILABLE" ? "--" : fmtPct(raw)) +
        '</td><td class="num">' + fmtScore(sc) + "</td></tr>";
    });
    var table = el("table", {
      html: "<thead><tr><th>seat</th><th style='text-align:right'>raw</th><th style='text-align:right'>score</th></tr></thead><tbody>" + seatRows + "</tbody>",
    });
    left.appendChild(table);

    var focusRaw = row && row.today ? row.today.raw : null;
    var focusScore = scoreFor(signal, focusRaw);
    var num = row && row.definition ? row.definition.numerator : null;
    var den = row && row.definition ? row.definition.denominator : null;
    var haveFocusCounts = num && den && num.value !== null && num.value !== undefined
      && den.value !== null && den.value !== undefined;
    var focus = el("div", { class: "focus" });
    focus.appendChild(el("div", { class: "who", html: state.agent + " &middot; today's checkpoint" }));
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
    var histHost = el("div", {});
    mid.appendChild(histHost);

    var seriesKey = state.agent + "|" + signal;
    var series = state.seriesByAgentSignal[seriesKey];
    var points = series && series.signals && series.signals[signal] ? series.signals[signal] : [];
    if (series && series.error) {
      mid.appendChild(el("div", { class: "sub", html: "History unavailable: " + series.error }));
    } else if (!points.length) {
      mid.appendChild(el("div", { class: "sub", html: "No recorded history points for this signal yet." }));
    } else {
      histHost.appendChild(historySVG(signal, state.agent, points, state.histView[signal]));
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
    var right = el("div", {});
    right.appendChild(el("div", { class: "lbl", html: "Curve &middot; raw &rarr; score, editable knots" }));
    var curveHost = el("div", {});
    right.appendChild(curveHost);
    curveHost.appendChild(curveSVG(signal));

    var knotsWrap = el("div", { class: "knots" });
    state.curves[signal].knots.forEach(function (p, i) {
      var label = el("label", {});
      label.appendChild(document.createTextNode("k" + i + " "));
      var xInput = el("input", { value: p[0], title: "raw x", "data-sig": signal, "data-i": i, "data-c": "0" });
      var yInput = el("input", { value: p[1], title: "score y", "data-sig": signal, "data-i": i, "data-c": "1" });
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
      knotsWrap.appendChild(label);
    });
    var addBtn = el("button", { type: "button", class: "kbtn add" });
    addBtn.textContent = "+ knot";
    addBtn.addEventListener("click", function () {
      var k = state.curves[signal].knots.sort(function (a, b) { return a[0] - b[0]; });
      var m = Math.max(0, Math.floor(k.length / 2) - 1);
      var x0 = k[m][0], y0 = k[m][1], x1 = k[m + 1][0], y1 = k[m + 1][1];
      k.splice(m + 1, 0, [+((x0 + x1) / 2).toFixed(4), +((y0 + y1) / 2).toFixed(4)]);
      markEdited(signal);
      render();
    });
    knotsWrap.appendChild(addBtn);
    right.appendChild(knotsWrap);
    right.appendChild(el("div", {
      class: "sub", style: "font-size:11px;margin-top:4px",
      html: "Between knots: linear. Outside the first/last knot: held constant (clamp-to-endpoint, ported line-for-line from health.curves.piecewise_linear -- see health-workbench-curve.js).",
    }));

    var recWrap = el("div", { class: "rec" });
    recWrap.appendChild(el("span", { class: "k", html: "importance" }));
    var impSpan = el("span", { class: "imp" });
    var impInput = el("input", {
      type: "number", min: "0", step: "1", value: state.curves[signal].importance, "data-imp": signal,
    });
    impSpan.appendChild(impInput);
    impSpan.appendChild(document.createTextNode(" within " + metric.aspect + (held ? " (currently 0: held out of composition)" : "")));
    recWrap.appendChild(impSpan);
    right.appendChild(recWrap);

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
    Object.keys(state.edited).forEach(function (sig) {
      if (state.edited[sig] && state.curves[sig]) {
        out[sig] = {
          knots: state.curves[sig].knots.map(function (p) { return [p[0], p[1]]; }),
          importance: state.curves[sig].importance,
        };
      }
    });
    return out;
  }
  function refreshExportBox() {
    qs("#wb-export-json").value = JSON.stringify(buildExportPayload(), null, 2);
  }

  // ---------------------------------------------------------------------
  // Event delegation for the dynamically-rendered knot/importance inputs.
  // ---------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    qs("#wb-aspects").addEventListener("change", function (e) {
      var t = e.target;
      if (t.dataset && t.dataset.sig) {
        var v = parseFloat(t.value);
        if (!isNaN(v)) {
          state.curves[t.dataset.sig].knots[+t.dataset.i][+t.dataset.c] = v;
          markEdited(t.dataset.sig);
          render();
        }
      } else if (t.dataset && t.dataset.imp) {
        var iv = parseFloat(t.value);
        if (!isNaN(iv)) {
          state.curves[t.dataset.imp].importance = Math.max(0, iv);
          markEdited(t.dataset.imp);
          render();
        }
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
      try { window.localStorage.removeItem(LS_KEY); } catch (e) { /* best-effort */ }
      seedCurveState();
      render();
    });

    qs("#wb-agent").addEventListener("change", function (e) {
      state.agent = e.target.value;
      var url = new URL(window.location.href);
      url.searchParams.set("agent", state.agent);
      window.history.replaceState(null, "", url.toString());
      loadForAgent().then(render);
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
