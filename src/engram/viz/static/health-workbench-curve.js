// health-workbench-curve.js -- pure scoring math for the Curve Workbench
// (engram-alpha#2727), split out from health-workbench.js the same way
// health-detail-math.js is split from the detail-view's own render code:
// dependency-free and DOM-free (no `document`, no `fetch`, no state), so
// this file alone can run under `node` for a pure parity unit test
// (tests/test_health_workbench_curve_parity.py) that checks every function
// here against its Python original, byte-for-byte on the same inputs.
//
// piecewiseLinear() is a DELIBERATE, LINE-FOR-LINE port of
// health/curves.py's piecewise_linear(rate, *, knots) -- same clamp-to-
// endpoint behavior, same degenerate-zero-width-segment rule, same
// single-knot (xs[0] == xs[-1]) collapse to a flat curve. Do NOT
// "simplify" this port -- the parity test compares it against the real
// Python function on knot sets chosen specifically to exercise every one
// of these branches (see that test's own module docstring).
//
// aspectScore() mirrors health/engine.py's _score_aspect: "one aspect's
// score: Σ (importance_i / Σ importance_available) × curve_i(signal_i)
// over its CURRENTLY AVAILABLE admitted metrics only". composeScore()
// mirrors that same file's compose_aspect_scores: "composed = Σ over
// SCORED aspects (a) of (points_a / points_in_play) × aspect_score_a ×
// 100". Cited by function name so a future reader can re-verify this port
// against the current engine.py source rather than trusting this comment.

function piecewiseLinear(rate, knots) {
  if (!knots || knots.length === 0) {
    throw new Error("piecewiseLinear requires at least one (x, y) knot");
  }
  var xs = knots.map(function (k) { return k[0]; });
  var ys = knots.map(function (k) { return k[1]; });

  // Clamp: rate at or below the first knot's x -> first knot's y; at or
  // above the last knot's x -> last knot's y. Also correctly handles the
  // single-knot case (xs[0] === xs[xs.length - 1]): every rate clamps to
  // that one knot's y, i.e. a flat curve. Mirrors piecewise_linear's own
  // two clamp branches exactly.
  if (rate <= xs[0]) return ys[0];
  if (rate >= xs[xs.length - 1]) return ys[ys.length - 1];

  for (var i = 0; i < knots.length - 1; i++) {
    var x0 = xs[i], y0 = ys[i], x1 = xs[i + 1], y1 = ys[i + 1];
    if (x0 <= rate && rate <= x1) {
      if (x1 === x0) {
        // Degenerate (zero-width) segment: no meaningful slope -- return
        // the segment's own y rather than dividing by zero.
        return y0;
      }
      var t = (rate - x0) / (x1 - x0);
      return y0 + t * (y1 - y0);
    }
  }
  // Unreachable given the two clamp branches above -- kept as an explicit
  // fallback, mirroring piecewise_linear's own "invariant guard rather
  // than a silent fallthrough" comment.
  return ys[ys.length - 1];
}

// members: [{importance: number, curveScore: number}] -- the CURRENTLY
// AVAILABLE admitted metrics for one aspect (the caller has already
// excluded any importance<=0 / unavailable metric, same precondition
// _score_aspect's own `available` list enforces). Returns null for
// UNAVAILABLE (nothing to sum over), matching _score_aspect's own
// UNAVAILABLE-when-total-importance-is-zero rule.
function aspectScore(members) {
  var total = members.reduce(function (acc, m) { return acc + m.importance; }, 0);
  if (!(total > 0)) return null;
  return members.reduce(function (acc, m) {
    return acc + (m.importance / total) * m.curveScore;
  }, 0);
}

// aspects: [{points: number, score: number|null}] -- score is null for an
// aspect held out of composition (UNAVAILABLE this run or zero admitted
// metrics declared). Returns null when points_in_play <= 0 (every aspect
// held out), matching compose_aspect_scores's own UNAVAILABLE rule.
function composeScore(aspects) {
  var scored = aspects.filter(function (a) { return a.score !== null && a.score !== undefined; });
  var pointsInPlay = scored.reduce(function (acc, a) { return acc + a.points; }, 0);
  if (!(pointsInPlay > 0)) return null;
  return scored.reduce(function (acc, a) {
    return acc + (a.points / pointsInPlay) * a.score * 100.0;
  }, 0);
}

// ---------------------------------------------------------------------
// Value checks at edit-enter time (engram-alpha#2738, round 2 -- Lei's
// first-use feedback on PR #2730). Both functions are pure ({ok, reason}
// return, no DOM/state) so they parity-test against Python the same way
// piecewiseLinear does -- see
// tests/test_health_workbench_edit_checks_parity.py.
//
// validateImportance mirrors a workbench-specific business rule, NOT a
// literal health.curves/health.model check: MetricBinding itself only
// requires importance >= 0 (health/model.py's own admission check permits
// a diagnostic-only metric to be declared importance=0.0). The editable
// RANGE here -- (0, +inf), with 0 reserved and locked in either direction
// -- is Lei's rule from the issue ("the hold-out is a very special state
// ... not purely a mathematical tuning factor"), layered on top of that
// admission check, not derived from it.
// ---------------------------------------------------------------------
function validateImportance(newValue, storedValue) {
  if (typeof newValue !== "number" || isNaN(newValue)) {
    return { ok: false, reason: "importance must be a number" };
  }
  if (storedValue === 0) {
    return {
      ok: false,
      reason: "importance is locked: this metric's stored importance is " +
        "0 (hold-out) -- a hold-out is not switchable in this editor",
    };
  }
  if (newValue < 0) {
    return { ok: false, reason: "importance must be > 0 (negative refused)" };
  }
  if (newValue === 0) {
    return {
      ok: false,
      reason: "0 is reserved for hold-out and cannot be set on a " +
        "non-zero metric here",
    };
  }
  return { ok: true, reason: null };
}

// validateKnots mirrors health/curves.py's CurveSpec.__post_init__ for
// the structural rules (non-empty, sorted ascending by x) exactly, and
// additionally enforces per-knot bounds against the curve's declared
// input_domain / output_range -- an editor-specific rule requested by the
// issue ("x inside input_domain and y inside output_range"), NOT itself
// part of CurveSpec.__post_init__ (that method only validates that an
// EXPLICITLY-declared input_domain equals the knots' own derived
// (knots[0][0], knots[-1][0]) span; it never bounds-checks output_range
// against knot y values at all). Given sortedness is already enforced,
// checking every knot's x against [domainLo, domainHi] is equivalent to
// CurveSpec's own endpoint-equality check whenever the edited knot set's
// first/last x are AT the domain edges (the common case this editor
// produces); the per-knot form is used here because it also accepts an
// interior domain-preserving edit that shifts an endpoint slightly
// without literally re-touching the edge value, which is friendlier UX
// than requiring exact endpoint round-tripping on every edit.
//
// This looser interior-edit acceptance cannot reach CurveSpec.__post_init__
// today ONLY because buildExportPayload omits input_domain/output_range
// from the export entirely (see health-workbench.js) -- there is no
// explicit input_domain on load for __post_init__'s equality check to
// disagree with. A future consumer that reintroduces an explicit
// input_domain on load (e.g. a round-trip import) would reopen the gap
// between this editor's per-knot bounds check and CurveSpec's stricter
// endpoint-equality rule.
function validateKnots(knots, inputDomain, outputRange) {
  if (!knots || knots.length === 0) {
    return { ok: false, reason: "knots must contain at least one (x, y) pair" };
  }
  var xs = knots.map(function (k) { return k[0]; });
  for (var i = 0; i < xs.length - 1; i++) {
    if (xs[i] > xs[i + 1]) {
      return { ok: false, reason: "knots must be sorted ascending by x" };
    }
  }
  if (inputDomain) {
    var lo = inputDomain[0], hi = inputDomain[1];
    for (i = 0; i < knots.length; i++) {
      var x = knots[i][0];
      if (x < lo || x > hi) {
        return {
          ok: false,
          reason: "knot x=" + x + " lies outside the declared input " +
            "domain [" + lo + ", " + hi + "]",
        };
      }
    }
  }
  if (outputRange) {
    var ylo = outputRange[0], yhi = outputRange[1];
    for (i = 0; i < knots.length; i++) {
      var y = knots[i][1];
      if (y < ylo || y > yhi) {
        return {
          ok: false,
          reason: "knot y=" + y + " lies outside the declared output " +
            "range [" + ylo + ", " + yhi + "]",
        };
      }
    }
  }
  return { ok: true, reason: null };
}

// ---------------------------------------------------------------------
// Axis / focus-marker pure helpers (engram-alpha#2738, round 3 -- axis
// ticks, hover/pin focus marker, the real-time history x-axis fix, and
// histogram tick/percentage formatting). All DOM-free and state-free, same
// discipline as the functions above, so they parity-test the same way
// (tests/test_health_workbench_axis_helpers_parity.py) even though most of
// them have no Python original to compare against -- the test asserts
// pure-JS behavior directly, the way it would if there WERE a Python
// twin, rather than skipping verification just because there isn't one.
// ---------------------------------------------------------------------

// clamp(v, lo, hi) -- the one helper vanilla JS has no builtin for.
function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

// computeAxisTicks(lo, hi, desiredCount): `desiredCount` (default 5,
// clamped to [3, 5] per R1's "3-5 nice ticks incl. both ends") evenly
// spaced ticks spanning [lo, hi] inclusive of both endpoints. Deliberately
// EVENLY spaced rather than "nice round number" ticks (a d3-ticks-style
// algorithm) -- flagged as a judgment call in the dispatching report: the
// curve/output domains here are small, arbitrary, caller-declared ranges
// (not auto-scaled from unbounded data), so round-number ticks buy little
// and even spacing is simpler to reason about and test deterministically.
function computeAxisTicks(lo, hi, desiredCount) {
  var n = Math.max(3, Math.min(5, desiredCount || 5)) - 1;
  if (hi <= lo) return [lo];
  var ticks = [];
  for (var i = 0; i <= n; i++) {
    ticks.push(lo + (hi - lo) * (i / n));
  }
  return ticks;
}

// formatAxisNumber(v): compact numeric label for a curve-plot axis tick
// (raw x / score y) -- not the table's fmtPct/fmtScore (those are
// display-layer functions living in health-workbench.js and calibrated to
// the table's own columns); this is a smaller, purely-numeric label for
// the plot margin. Strips trailing zeros so "0", "0.5", "1" render short.
function formatAxisNumber(v) {
  if (Math.abs(v) < 1e-9) return "0";
  var s = v.toFixed(3);
  s = s.replace(/0+$/, "").replace(/\.$/, "");
  return s === "" || s === "-" ? "0" : s;
}

// linearScale(v, d0, d1, r0, r1): map v from domain [d0, d1] to range
// [r0, r1]. Calling it with domain/range swapped inverts the mapping --
// e.g. linearScale(screenX, r0, r1, d0, d1) recovers the data value under
// a pixel position. Degenerate (d1 === d0) domain maps everything to r0,
// matching the existing sy()/sx() degenerate-span guards elsewhere in this
// codebase (e.g. historySVG's `if (hi === lo) { hi += 1; lo -= 1; }`).
function linearScale(v, d0, d1, r0, r1) {
  if (d1 === d0) return r0;
  return r0 + ((v - d0) / (d1 - d0)) * (r1 - r0);
}

// nearestIndexByValue(values, target): index of the entry in `values`
// (numbers, `null` entries skipped) closest to `target`. Used for both the
// curve-plot "nearest sampled x" case (values = the sweep's own x's) and
// the history-plot "nearest data point by x-distance in pixels" case
// (values = each point's pixel x). Returns -1 for an all-null/empty input.
function nearestIndexByValue(values, target) {
  var bestIdx = -1;
  var bestDist = Infinity;
  for (var i = 0; i < values.length; i++) {
    var v = values[i];
    if (v === null || v === undefined) continue;
    var d = Math.abs(v - target);
    if (d < bestDist) {
      bestDist = d;
      bestIdx = i;
    }
  }
  return bestIdx;
}

// computeTimeTicks(tMin, tMax, desiredCount): evenly spaced tick
// TIMESTAMPS (ms since epoch) spanning [tMin, tMax] inclusive of both
// ends -- the time-domain twin of computeAxisTicks, same even-spacing
// judgment call (R3 asks for "3-5 ticks", not specifically nice
// round-time ticks).
function computeTimeTicks(tMin, tMax, desiredCount) {
  var n = Math.max(3, Math.min(5, desiredCount || 5)) - 1;
  if (tMax <= tMin) return [tMin];
  var ticks = [];
  for (var i = 0; i <= n; i++) {
    ticks.push(tMin + (tMax - tMin) * (i / n));
  }
  return ticks;
}

// formatTimeTickLabel(ms, withYear): viewer-LOCAL "MM-DD HH:mm", prefixed
// "YYYY-" when `withYear` is true. Uses the platform's local Date fields
// (this is intentionally viewer-local, per R3 -- it will format
// differently under `node` in a different TZ than in a browser, which is
// expected and is why the parity test only asserts the STRUCTURE of the
// output, not a fixed string, for any input that depends on local TZ --
// see that test's own comment for the one input it pins exactly).
function formatTimeTickLabel(ms, withYear) {
  var d = new Date(ms);
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  var core = pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " +
    pad(d.getHours()) + ":" + pad(d.getMinutes());
  return withYear ? d.getFullYear() + "-" + core : core;
}

// formatLocalDateTime(ms): full local "YYYY-MM-DD HH:mm" -- used by the
// R2 history-plot readout (distinct from the tick labels above, which
// only carry the year conditionally).
function formatLocalDateTime(ms) {
  var d = new Date(ms);
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
    " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

// computeTimeTickLabels(ticksMs): one label per tick from
// formatTimeTickLabel, prefixing the year on the FIRST tick and whenever
// the calendar year changes from the previous tick (R3's exact rule).
function computeTimeTickLabels(ticksMs) {
  var labels = [];
  var lastYear = null;
  ticksMs.forEach(function (ms, i) {
    var y = new Date(ms).getFullYear();
    var withYear = i === 0 || y !== lastYear;
    labels.push(formatTimeTickLabel(ms, withYear));
    lastYear = y;
  });
  return labels;
}

// shouldTiltLabels(spacingPx, labels, charPx, minGapPx): true when evenly
// spaced tick labels would overlap at the given pixel spacing -- measuring
// label width as `charPx` px/char (R3 says "~6px/char at font-size 9") and
// requiring `minGapPx` of daylight between adjacent labels. `charPx` and
// `minGapPx` both default to 6, per R3's own text ("tilt when spacing <
// width+6").
function shouldTiltLabels(spacingPx, labels, charPx, minGapPx) {
  charPx = charPx || 6;
  minGapPx = minGapPx === undefined ? 6 : minGapPx;
  var maxLen = 0;
  for (var i = 0; i < labels.length; i++) {
    maxLen = Math.max(maxLen, labels[i].length);
  }
  var width = maxLen * charPx;
  return spacingPx < width + minGapPx;
}

// formatBinPercent(count, total): a histogram bin's share of `total`
// points as a percentage string -- integer precision, or one decimal
// place when under 10% (R5's exact rule, so a thin sub-10% bin still
// shows a non-zero-looking label). "0%" for an empty/zero total rather
// than NaN.
function formatBinPercent(count, total) {
  if (!total) return "0%";
  var pct = (count / total) * 100;
  return pct < 10 ? pct.toFixed(1) + "%" : Math.round(pct) + "%";
}

// binIndexForX(mouseX, px0, binWidth, nBins): which histogram bin index a
// mouse x-position (in the same SVG user-unit space as `px0`/`binWidth`)
// falls under, clamped to a valid bin index (so a cursor past either edge
// still resolves to the nearest real bin rather than -1/nBins).
function binIndexForX(mouseX, px0, binWidth, nBins) {
  if (nBins <= 0) return -1;
  var idx = Math.floor((mouseX - px0) / binWidth);
  return clamp(idx, 0, nBins - 1);
}

// ---------------------------------------------------------------------
// Round-4 pure helpers (engram-alpha#2738 round 4 -- Lei's post-round-3
// feedback: curve-preserving per-knot "+", the all-flat history-plot
// fallback, and the unified overflow fix). Same DOM-free/state-free
// discipline as the helpers above; parity-tested in
// tests/test_health_workbench_round4_parity.py the same way the round-3
// axis helpers are (contract tests, no Python original to compare
// against).
// ---------------------------------------------------------------------

// insertMidKnot(knots, i, inputDomain): insert ONE new knot immediately
// after knot index `i`, chosen so the CURVE ITSELF is unchanged by the
// insertion (curve-preserving) -- this is what makes the per-knot "+"
// button in the editor safe to click without altering anything already
// tuned.
//
// - Interior (i is not the last index): new x is the midpoint of
//   knots[i].x and knots[i+1].x; new y is piecewiseLinear(midX, knots) --
//   i.e. the CURRENT curve's own value at that x, so the new knot sits
//   exactly on the existing segment.
// - Last knot (i === knots.length - 1): only meaningful if the curve's
//   input_domain extends past the last knot's x (the clamp-to-endpoint
//   region) -- new x is the midpoint of knots[i].x and the domain's high
//   edge, new y is the last knot's own y (the clamp value), so the curve
//   is unchanged there too. Returns `null` (no insertion) when
//   `inputDomain` is missing, or the last knot's x already sits at (or
//   past) the domain's high edge -- this is also the signal the caller
//   uses to decide whether to show the "+" button at all after the last
//   row (per the round-4 spec: "otherwise omit the button").
//
// Does not mutate `knots` -- returns a new array (or null).
function insertMidKnot(knots, i, inputDomain) {
  if (!knots || i < 0 || i >= knots.length) return null;
  var newKnots = knots.map(function (k) { return k.slice(); });
  var isLast = i === knots.length - 1;
  if (isLast) {
    if (!inputDomain) return null;
    var xLast = knots[i][0], yLast = knots[i][1], domainHi = inputDomain[1];
    if (!(xLast < domainHi)) return null;
    newKnots.push([(xLast + domainHi) / 2, yLast]);
    return newKnots;
  }
  var x0 = knots[i][0], x1 = knots[i + 1][0];
  var midX = (x0 + x1) / 2;
  var midY = piecewiseLinear(midX, knots);
  newKnots.splice(i + 1, 0, [midX, midY]);
  return newKnots;
}

// padFlatRange(v, mode, domain, outputRange) -> [lo, hi]: the flat-range
// (all values equal `v`) fallback for a history plot's own y-axis span,
// replacing the old `hi += 1; lo -= 1` (which plotted an all-zero raw
// rate as [-1, 1] -- y ticks reading -100% / 0% / 100%, nonsensical for a
// ratio-scale raw signal).
//
// - mode === "score": the score axis's own bounds are already known
//   (the curve's declared output_range, default [0,1] when not
//   supplied) -- just use that whole range rather than an arbitrary pad
//   around one value, since a constant score plotted against its own
//   full possible range is the more informative fallback than a tight
//   window around it.
// - raw mode: pad by a fixed ratio-scale amount (0.05, i.e. 5 points on
//   a 0-1 ratio) on both sides of `v`, clamped to the domain's own edges
//   so the fallback never suggests a raw value outside what's actually
//   possible. When the domain itself isn't known, clamp only the low
//   side to 0 (a ratio-scale raw signal is never negative) -- there's no
//   known ceiling to clamp the high side against.
function padFlatRange(v, mode, domain, outputRange) {
  if (mode === "score") {
    return outputRange ? [outputRange[0], outputRange[1]] : [0, 1];
  }
  var pad = 0.05;
  if (domain) {
    return [Math.max(domain[0], v - pad), Math.min(domain[1], v + pad)];
  }
  return [Math.max(0, v - pad), v + pad];
}

// ---------------------------------------------------------------------
// Round-5 pure helpers (engram-alpha#2738 round 5 -- the "latest
// checkpoint" relabel, the per-metric "set as reference" rescale, and the
// UNAVAILABLE-run collapse). Same DOM-free/state-free discipline as the
// helpers above; parity-tested in tests/test_health_workbench_round5_parity.py.
// ---------------------------------------------------------------------

// formatCheckpointLabel(provenance, nowMs) -- the served snapshot's own
// identity, replacing the old "today's checkpoint" wording (R5.1): the
// served snapshot is whatever the LATEST recorded checkpoint happens to
// be, which may be hours or days old, and "today" was actively misleading
// about that. `provenance` is the /api/health/v2 payload's own
// `provenance` object ({snapshot_id, turn, checkpoint_mode, written_at}).
// Returns "no snapshot" when provenance (or its written_at) is missing/
// unparseable -- the caller's own !snap.has_snapshot branch already
// handles the "nothing to show at all" case with its own message, so this
// fallback string is a defensive backstop, not the primary UX for that
// case. The "<N> h ago" / "<N> d ago" suffix is appended to the SAME
// string (not a separate return value) per this function's contract; a
// caller wanting to style the suffix differently (this page does, via a
// muted span) locates it with the trailing " (<N> [hd] ago)" pattern
// rather than this function returning a structured value -- see
// health-workbench.js's own comment at the call site for why.
function formatCheckpointLabel(provenance, nowMs) {
  if (!provenance || !provenance.written_at) return "no snapshot";
  var writtenMs = Date.parse(provenance.written_at);
  if (isNaN(writtenMs)) return "no snapshot";
  var local = formatLocalDateTime(writtenMs);
  var mode = provenance.checkpoint_mode || "unknown";
  var turn = (provenance.turn === null || provenance.turn === undefined) ? "?" : provenance.turn;
  var label = "latest checkpoint · " + local + " (" + mode + ", turn " + turn + ")";
  var ageMs = nowMs - writtenMs;
  var HOUR = 3600000, DAY = 86400000;
  if (ageMs > 7 * DAY) {
    label += " (" + Math.round(ageMs / DAY) + " d ago)";
  } else if (ageMs > DAY) {
    label += " (" + Math.round(ageMs / HOUR) + " h ago)";
  }
  return label;
}

// rescaleToReference(members, refSignal) -> members'|null -- R5.3's "set
// as reference" rescale. `members` is the FULL per-aspect member list for
// ONE aspect ([{signal, importance, curveScore}, ...], including
// held-out (importance===0) metrics); `refSignal` is the signal being
// promoted to reference. Returns `null` when `refSignal` isn't found in
// `members` or its importance isn't > 0 (a hold-out can never become the
// reference -- the caller's own eligibility check already excludes this,
// this is the defensive backstop for a stale/racing call).
//
// k = 1 / importance_of_ref; every non-hold-out member's importance is
// multiplied by k -- because aspectScore() divides each member's
// importance by the aspect's OWN total importance
// (Sigma importance_available), a uniform rescale of every admitted
// member cancels out of that ratio and leaves the aspect's score (and
// therefore the composed score) UNCHANGED. Hold-outs (importance === 0)
// are passed through unchanged (0 * k is still 0, but this branch avoids
// relying on that identity so a hold-out's importance is provably never
// touched). The reference member's own new importance is set to the
// literal value 1.0 (not `importance * k`, which is only ALGEBRAICALLY 1
// -- floating point multiplication of 1/x by x is not guaranteed to
// return exactly 1.0), so "the ref's importance is exactly 1.0" holds by
// construction, not by luck of the float math.
function rescaleToReference(members, refSignal) {
  if (!members) return null;
  var ref = null;
  for (var i = 0; i < members.length; i++) {
    if (members[i].signal === refSignal) { ref = members[i]; break; }
  }
  if (!ref || !(ref.importance > 0)) return null;
  var k = 1 / ref.importance;
  return members.map(function (m) {
    if (m.importance === 0) {
      return { signal: m.signal, importance: 0, curveScore: m.curveScore };
    }
    if (m.signal === refSignal) {
      return { signal: m.signal, importance: 1.0, curveScore: m.curveScore };
    }
    return { signal: m.signal, importance: m.importance * k, curveScore: m.curveScore };
  });
}

// collapseUnavailableRuns(points) -> [{t0, t1, n, reasons}] -- R5.4's
// UNAVAILABLE-run collapse. `points` is the RAW /api/health/v2/series
// point list (in list/time order), each shaped
// `{t, raw, score, unreplayable: {<measurement>: {class, reason}}|null,
// ...}`. A point counts as UNAVAILABLE when either its `raw` or its
// `score` is the literal string "UNAVAILABLE" (the API's own sentinel --
// see this file's module docstring). Returns one entry per MAXIMAL run of
// consecutive UNAVAILABLE points (by list order, i.e. by time since the
// caller's own point list is time-ordered): `t0`/`t1` are the run's first/
// last point's own `t`, `n` is the run's length, and `reasons` is a
// `{"<class>: <reason>": count}` tally built from every point in the run
// that carries an `unreplayable` object (a point with `unreplayable: null`
// contributes nothing to the tally but still counts toward `n` -- an
// UNAVAILABLE point with no recorded reason is still UNAVAILABLE). The
// caller picks the "dominant" reason from this tally (highest count) --
// deliberately not this function's job, since "dominant" is a display
// choice, not a fact about the run itself.
function collapseUnavailableRuns(points) {
  function isUnavailable(p) {
    return p.raw === "UNAVAILABLE" || p.score === "UNAVAILABLE";
  }
  var runs = [];
  var i = 0;
  var n = points.length;
  while (i < n) {
    if (!isUnavailable(points[i])) { i++; continue; }
    var start = i;
    var reasons = {};
    while (i < n && isUnavailable(points[i])) {
      var p = points[i];
      if (p.unreplayable) {
        Object.keys(p.unreplayable).forEach(function (measurement) {
          var info = p.unreplayable[measurement];
          var cls = (info && info["class"]) || "unknown";
          var reason = (info && info.reason) || "unknown";
          var reasonKey = cls + ": " + reason;
          reasons[reasonKey] = (reasons[reasonKey] || 0) + 1;
        });
      }
      i++;
    }
    var end = i - 1;
    runs.push({ t0: points[start].t, t1: points[end].t, n: end - start + 1, reasons: reasons });
  }
  return runs;
}

// dominantReasonKey(reasons) -- pick the highest-count key from a
// collapseUnavailableRuns() run's own `reasons` tally, `null` for an
// empty tally. A tiny, DOM-free display helper (not itself part of
// collapseUnavailableRuns's contract, per that function's own comment on
// why "dominant" is the caller's job) -- kept here rather than in
// health-workbench.js so it parity-tests the same way.
function dominantReasonKey(reasons) {
  var best = null, bestCount = -1;
  Object.keys(reasons || {}).forEach(function (k) {
    if (reasons[k] > bestCount) { bestCount = reasons[k]; best = k; }
  });
  return best;
}

// Node-only export for the pure-math parity test
// (tests/test_health_workbench_curve_parity.py,
// tests/test_health_workbench_edit_checks_parity.py,
// tests/test_health_workbench_axis_helpers_parity.py) -- a no-op in the
// browser, where these names are simply globals on `window` once this
// file loads (same convention as health-detail-math.js).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    piecewiseLinear: piecewiseLinear,
    aspectScore: aspectScore,
    composeScore: composeScore,
    validateImportance: validateImportance,
    validateKnots: validateKnots,
    clamp: clamp,
    computeAxisTicks: computeAxisTicks,
    formatAxisNumber: formatAxisNumber,
    linearScale: linearScale,
    nearestIndexByValue: nearestIndexByValue,
    computeTimeTicks: computeTimeTicks,
    formatTimeTickLabel: formatTimeTickLabel,
    formatLocalDateTime: formatLocalDateTime,
    computeTimeTickLabels: computeTimeTickLabels,
    shouldTiltLabels: shouldTiltLabels,
    formatBinPercent: formatBinPercent,
    binIndexForX: binIndexForX,
    insertMidKnot: insertMidKnot,
    padFlatRange: padFlatRange,
    formatCheckpointLabel: formatCheckpointLabel,
    rescaleToReference: rescaleToReference,
    collapseUnavailableRuns: collapseUnavailableRuns,
    dominantReasonKey: dominantReasonKey,
  };
}
