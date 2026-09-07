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

// Node-only export for the pure-math parity test
// (tests/test_health_workbench_curve_parity.py) -- a no-op in the
// browser, where these three names are simply globals on `window` once
// this file loads (same convention as health-detail-math.js).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    piecewiseLinear: piecewiseLinear,
    aspectScore: aspectScore,
    composeScore: composeScore,
  };
}
