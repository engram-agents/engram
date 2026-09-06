// health-detail-math.js -- pure distribution/histogram math for the health
// detail-view's history strip + distribution panel (engram-alpha#2518,
// data-contract §4: "the page computes distribution stats/histograms
// client-side over `history`" -- no server field for this).
//
// Deliberately dependency-free and DOM-free: no `document`, no `fetch`, no
// state. That is what lets tests/test_viz_health_v2_detail_view.py run this
// file directly under `node` for a pure math unit test, in addition to the
// browser loading it via <script src="/static/health-detail-math.js"> on
// health.html (before the page's own inline <script>, so these three names
// are ordinary globals by the time the inline script's render functions call
// them).
//
// Worked examples (hand-verified by hand, not just by the Node test), so the
// math is checkable by inspection even on a seat with no `node` available --
// values = [1, 2, 3, 4, 5], n = 5:
//   percentile(sorted, 50) -> idx = ceil(0.50*5) - 1 = 3 - 1 = 2 -> sorted[2] = 3
//   percentile(sorted, 25) -> idx = ceil(0.25*5) - 1 = 2 - 1 = 1 -> sorted[1] = 2
//   percentile(sorted, 75) -> idx = ceil(0.75*5) - 1 = 4 - 1 = 3 -> sorted[3] = 4
//   percentile(sorted, 10) -> idx = ceil(0.10*5) - 1 = 1 - 1 = 0 -> sorted[0] = 1
//   percentile(sorted, 90) -> idx = ceil(0.90*5) - 1 = 5 - 1 = 4 -> sorted[4] = 5
//   mean = (1+2+3+4+5)/5 = 3
//   population variance = ((1-3)^2+(2-3)^2+(3-3)^2+(4-3)^2+(5-3)^2)/5
//                        = (4+1+0+1+4)/5 = 2
//   sd = sqrt(2) ~= 1.4142135623730951

function percentile(sortedArr, p) {
  var n = sortedArr.length;
  var idx = Math.ceil((p / 100) * n) - 1;
  if (idx < 0) idx = 0;
  if (idx > n - 1) idx = n - 1;
  return sortedArr[idx];
}

// {min, p10, p25, p50, p75, p90, max, mean, sd} over `values` (the plotted --
// non-null, non-"UNAVAILABLE" -- numbers from one point array), or `null`
// when `values` is empty. Population sd (not sample) -- a deliberate scope
// choice (flagged in the dispatching report): the values ARE the full
// population of plotted points for that strip, not a sample drawn from a
// larger one, so population sd is the more natural read; sample sd would
// only differ by the well-known n/(n-1) factor if a future caller wants it.
function computeDistributionStats(values) {
  if (!values || values.length === 0) return null;
  var sorted = values.slice().sort(function (a, b) { return a - b; });
  var n = sorted.length;
  var mean = sorted.reduce(function (a, b) { return a + b; }, 0) / n;
  var variance =
    sorted.reduce(function (a, b) { return a + Math.pow(b - mean, 2); }, 0) / n;
  return {
    min: sorted[0],
    p10: percentile(sorted, 10),
    p25: percentile(sorted, 25),
    p50: percentile(sorted, 50),
    p75: percentile(sorted, 75),
    p90: percentile(sorted, 90),
    max: sorted[n - 1],
    mean: mean,
    sd: Math.sqrt(variance),
  };
}

// Adaptive histogram over `values`: Freedman-Diaconis bin width, falling
// back to Sturges when the FD width is non-positive (a degenerate/near-
// degenerate IQR) or n < 2. Returns {edges, counts} -- `edges` has
// `counts.length + 1` entries (bin i spans [edges[i], edges[i+1])), except
// the LAST bin is closed on both ends so the max value has a bin to land in.
function computeHistogramBins(values) {
  if (!values || values.length === 0) return { edges: [], counts: [] };
  var sorted = values.slice().sort(function (a, b) { return a - b; });
  var n = sorted.length;
  var min = sorted[0];
  var max = sorted[n - 1];
  if (max === min) {
    // Degenerate: every value identical -- one visible bin holding all of
    // them (a zero-width real range has no meaningful bin count).
    return { edges: [min, min + 1], counts: [n] };
  }
  var iqr = percentile(sorted, 75) - percentile(sorted, 25);
  var binWidth = 2 * iqr * Math.pow(n, -1 / 3);
  var binCount;
  if (binWidth <= 0 || n < 2) {
    binCount = Math.ceil(Math.log2(n) + 1); // Sturges fallback
  } else {
    binCount = Math.round((max - min) / binWidth);
  }
  binCount = Math.max(5, Math.min(20, binCount));
  var edgeWidth = (max - min) / binCount;
  var edges = [];
  for (var i = 0; i <= binCount; i++) edges.push(min + i * edgeWidth);
  var counts = new Array(binCount).fill(0);
  sorted.forEach(function (v) {
    var idx = Math.floor((v - min) / edgeWidth);
    if (idx >= binCount) idx = binCount - 1; // v === max lands in the last bin
    if (idx < 0) idx = 0;
    counts[idx]++;
  });
  return { edges: edges, counts: counts };
}

// Node-only export for the pure-math unit test
// (tests/test_viz_health_v2_detail_view.py) -- a no-op in the browser, where
// these three names are simply globals on `window` once this file loads.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    percentile: percentile,
    computeDistributionStats: computeDistributionStats,
    computeHistogramBins: computeHistogramBins,
  };
}
