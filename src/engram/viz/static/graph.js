// ═══════════════════════════════════════════════════════════════════
// Constants
// ═══════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════
// Schema-driven style maps (populated by fetchSchema() at init time)
// ═══════════════════════════════════════════════════════════════════
//
// NODE_STYLE[type] = {color, label}   — all 18 node types from /api/schema
// EDGE_STYLE[rel]  = {color, dash}    — all 13 relations from /api/schema
//   dash: '' (solid) if cascade||provenance is true; '4,3' (dashed) otherwise
//
// Override maps preserve the existing look for known types; any future type
// added to the schema auto-gets a deterministic generated color.

const NODE_COLOR_OVERRIDE = {
  evidence:                '#4a90d9',
  axiom:                   '#ff6f61',
  definition:              '#26c6da',
  observation_factual:     '#5cb85c',
  observation_predictive:  '#f0ad4e',
  prediction:              '#ffc107',
  conjecture:              '#ff9800',
  derivation:              '#b05ce6',
  theory:                  '#7c4dff',
  contradiction:           '#e05252',
  question:                '#17a2b8',
  goal:                    '#00e676',
  goal_tension:            '#76ff03',
  feeling_report:          '#f48fb1',
  person:                  '#ffd54f',
  lesson:                  '#80deea',
  task:                    '#a1887f',
  cornerstone:             '#ff8f00',   // deep amber — distinct from axiom/person/goal
};

const NODE_LABEL_OVERRIDE = {
  evidence:                'Evidence',
  axiom:                   'Axiom',
  definition:              'Definition',
  observation_factual:     'Obs. Factual',
  observation_predictive:  'Obs. Predictive',
  prediction:              'Prediction',
  conjecture:              'Conjecture',
  derivation:              'Derivation',
  theory:                  'Theory',
  contradiction:           'Contradiction',
  question:                'Question',
  goal:                    'Goal',
  goal_tension:            'Goal Tension',
  feeling_report:          'Feeling',
  person:                  'Person',
  lesson:                  'Lesson',
  task:                    'Task',
  cornerstone:             'Cornerstone',
};

const EDGE_COLOR_OVERRIDE = {
  cites:         '#7799bb',
  supported_by:  '#5cb85c',
  predicted_by:  '#5cb85c', // #2252 W6: mirrors supported_by -- the pairing
                            // edge split out of it, same visual family.
  contradicts:   '#e05252',
  resolves:      '#4a90d9',
  derives_from:  '#b05ce6',
  supersedes:    '#f0ad4e',
  retracts:      '#cc3333',
  // New relations: distinct colors not colliding with the 7 above
  exemplifies:   '#80cbc4',
  instantiates:  '#ce93d8',
  about:         '#ffb74d',
  serves:        '#aed581',
  tensions:      '#ff8a65',
  subtask_of:    '#90a4ae',
};

// Deterministic color generation: hash type/rel string → HSL hue (fixed S=65%,L=55%)
// Used as fallback for any schema entry not in the override maps.
function _hashStringToColor(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) & 0xffffffff;
  }
  const hue = Math.abs(h) % 360;
  return `hsl(${hue},65%,55%)`;
}

function _prettifyType(s) {
  // "observation_factual" → "Observation Factual" (title-case, underscores→spaces)
  return s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

// NODE_STYLE and EDGE_STYLE are populated by fetchSchema() before first render.
let NODE_STYLE = {};
let EDGE_STYLE = {};

// ═══════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════

let rawData = { nodes: [], edges: [] };
// In-flight guard for fetchGraph() (engram-alpha#2866 fix 3): without this,
// a fetch that takes longer than the 10s auto-refresh interval leaves the
// next scheduled tick free to start a second overlapping request against a
// server that (pre-fix-2) served one request at a time, so requests piled
// up unboundedly. A skip, not a queue -- the next tick just tries again.
let _fetchGraphInFlight = false;
// activeTypes is initialised from NODE_STYLE keys in fetchSchema() — after the
// schema endpoint resolves and NODE_STYLE is populated, the full set of known
// types (including cornerstone) is active by default.
let activeTypes = new Set();
let showSuperseded = false;
let showLabels = true;
let showTierOpacity = true;
let showFullGraph = false;
let selectedNodeId = null;
let autoRefreshInterval = null;
let highlightedNodeIds = new Set();
let highlightDepth = 2;

// What's new since last visit (#1169 slice-2).
// Computed ONCE on initial load (first fetchGraph call) and cached for the
// session. Re-renders (toggle/search/type-filter) re-use the cached set so
// the "new" designation doesn't shift under the user's feet.
// The boolean flag prevents the set from being recomputed on auto-refresh.
let newNodeIds = new Set();
let _whatsNewInitialised = false;

// Memory tier threshold. Mirrors the agent's $ENGRAM_HOME/config.json
// value (memory.tier2_max_nodes) — fetched at page load from /api/meta
// so the viz reflects the ACTUAL config, not the historical default
// (Lei caught this 2026-05-17: viz was showing many nodes as tier 3
// because TIER2_MAX=1000 was hardcoded while production config had
// tier2_max_nodes=4000). The default below is used only as a fallback
// until /api/meta resolves. Tier-1 is retired (#1220); two tiers remain:
// queryable (importance_score >= TIER2_MAX rank) vs faded.
let TIER2_MAX = 1000;

// ═══════════════════════════════════════════════════════════════════
// Legible-default constants (#1169)
// ═══════════════════════════════════════════════════════════════════

// Identity-backbone node types: always shown in legible-default mode.
// cornerstone is now a fully-styled type (in NODE_STYLE) and appears in the
// type-filter panel; BACKBONE_TYPES retains it here so it is always visible
// when showFullGraph is false (backbone bypasses the activeTypes gate).
const BACKBONE_TYPES = new Set(['cornerstone', 'axiom', 'goal', 'person']);

// How many recent nodes (by created_at DESC, excluding backbone) to include.
const LEGIBLE_RECENT_LIMIT = 60;

// If the total node count is at or below this threshold, skip the subset filter
// entirely and render all nodes (avoids awkward partial view on a sparse graph).
const LEGIBLE_SMALL_GRAPH = 80;

// ═══════════════════════════════════════════════════════════════════
// What's new since last visit (#1169 slice-2)
// ═══════════════════════════════════════════════════════════════════

// Call ONCE after the first fetchGraph() resolves with node data.
// Reads prevLastVisit from localStorage (per-agent key), builds newNodeIds
// from nodes whose created_at > prevLastVisit, then advances lastVisit to now.
//
// Per-agent key: engram-viz-lastvisit-<agentName>
// First visit (null prevLastVisit): empty set, no indicator, just sets baseline.
// Subsequent re-renders (toggle/search/type-filter) reuse the cached newNodeIds
// — _whatsNewInitialised guards against recomputing on auto-refresh.
function initWhatsNew(nodes) {
  // Per-agent localStorage key. In single-agent mode currentAgent() is null
  // (loadAgents() leaves the ?agent= param unset when there's only one agent),
  // so we fall back to '__default__' — a clearly-synthetic sentinel that won't
  // collide with a real agent name. Intentional consequence: if the install
  // later adds a second agent, that agent's visit-history starts fresh under
  // its real name (first-visit semantics: no highlights on the first multi-agent
  // visit). We deliberately do NOT carry the '__default__' history forward —
  // a one-time "everything looks new" pass on the multi-agent transition is an
  // acceptable cost versus mis-attributing one agent's history to another.
  const agentName = currentAgent() || '__default__';
  const lsKey = 'engram-viz-lastvisit-' + agentName;
  const prevLastVisit = localStorage.getItem(lsKey);  // ISO string or null

  // Compute: current nodes created AFTER the last visit timestamp.
  // ISO 8601 strings are lexicographically comparable.
  // Filter to is_current=true so superseded/retracted nodes don't inflate
  // the count (#1172: full payload includes superseded nodes, which caused
  // "1095 new" when only ~1064 current nodes exist — superseded nodes are
  // not visible in the default view and should not count as "new").
  if (prevLastVisit) {
    nodes.forEach(n => {
      if (n.is_current && n.created_at && n.created_at > prevLastVisit) {
        newNodeIds.add(n.id);
      }
    });
  }
  // else: first visit — leave newNodeIds empty (no highlights, no indicator).

  // Advance the baseline: THIS visit becomes NEXT visit's prevLastVisit.
  // Write AFTER computing so the nodes just seen don't appear new next time.
  localStorage.setItem(lsKey, new Date().toISOString());

  // Update indicator in sidebar.
  const el = document.getElementById('new-nodes-indicator');
  if (el) {
    if (newNodeIds.size > 0) {
      el.textContent = '✨ ' + newNodeIds.size + ' new since your last visit';
      el.style.display = 'block';
    } else {
      el.textContent = '';
      el.style.display = 'none';
    }
  }

  _whatsNewInitialised = true;
}

// ═══════════════════════════════════════════════════════════════════
// D3 setup
// ═══════════════════════════════════════════════════════════════════

const svg = d3.select('#graph-svg');
const container = svg.append('g').attr('class', 'zoom-container');

// Arrowhead markers — one per relation, built after fetchSchema() populates
// EDGE_STYLE so all 13 relations get markers (not just the old 7).
const defs = svg.append('defs');

function buildArrowheadMarkers() {
  // Clear any previously built markers (idempotent for schema reload).
  defs.selectAll('marker').remove();
  Object.entries(EDGE_STYLE).forEach(([rel, style]) => {
    defs.append('marker')
      .attr('id', `arrow-${rel}`)
      .attr('viewBox', '0 -4 8 8')
      .attr('refX', 0)
      .attr('refY', 0)
      .attr('markerWidth', 5)
      .attr('markerHeight', 5)
      .attr('orient', 'auto')
      .append('path')
      .attr('d', 'M0,-4L8,0L0,4')
      .attr('fill', style.color)
      .attr('opacity', 0.8);
  });
}

const linkGroup = container.append('g').attr('class', 'links');
const nodeGroup = container.append('g').attr('class', 'nodes');

// Zoom behavior
const zoom = d3.zoom()
  .scaleExtent([0.1, 5])
  .on('zoom', e => container.attr('transform', e.transform));
svg.call(zoom);

// Simulation
const simulation = d3.forceSimulation()
  .force('link', d3.forceLink().id(d => d.id).distance(90).strength(0.4))
  .force('charge', d3.forceManyBody().strength(-280))
  .force('collision', d3.forceCollide().radius(d => nodeRadius(d) + 6))
  .force('x', d3.forceX().strength(0.04))
  .force('y', d3.forceY().strength(0.04))
  .alphaDecay(0.03)
  .on('tick', ticked);

// ═══════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════

function nodeRadius(d) {
  if (d.type === 'evidence') return 11;
  if (d.type === 'axiom') return 13;
  if (d.type === 'definition') return 10;
  const c = (d.confidence != null) ? d.confidence : 0.5;
  return 7 + c * 10;
}

// Compute memory tiers for current nodes based on importance_score ranking.
// Two tiers: queryable (rank < TIER2_MAX) → tier 2, faded → tier 3.
// Tier-1 is retired (#1220).
function computeTiers(nodes) {
  const current = nodes.filter(n => n.is_current).slice();
  current.sort((a, b) => (b.importance_score || 0) - (a.importance_score || 0));
  const tierMap = {};
  current.forEach((n, i) => {
    if (i < TIER2_MAX) tierMap[n.id] = 2;
    else tierMap[n.id] = 3;
  });
  // Non-current nodes get tier 3
  nodes.filter(n => !n.is_current).forEach(n => { tierMap[n.id] = 3; });
  return tierMap;
}

function nodeOpacity(d, tierMap) {
  if (!showTierOpacity) return 1.0;
  const tier = tierMap[d.id] || 3;
  // Two tiers: queryable (2) → full opacity, faded (3) → dimmed.
  // Tier-1 is retired (#1220).
  if (tier === 2) return 1.0;
  return 0.2;
}

function nodeColor(d) {
  return (NODE_STYLE[d.type] || { color: '#888' }).color;
}

function nodeDisplayText(d) {
  if (d.type === 'evidence') return d.source_domain || d.source_title || d.id;
  if (d.type === 'definition') {
    try {
      const meta = typeof d.metadata === 'string' ? JSON.parse(d.metadata) : d.metadata;
      if (meta && meta.term) return meta.term;
    } catch(e) {}
  }
  return d.claim || d.id;
}

function truncate(s, n) {
  if (!s) return '';
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function isRetracted(d) {
  return d.status === 'retracted';
}

function isTainted(d) {
  // #2013/#2009 slice 1: tainted_by is a top-level field the /api/graph
  // payload attaches from cascade_markers now — metadata never carries it.
  return Array.isArray(d.tainted_by) && d.tainted_by.length > 0;
}

function isStale(d) {
  return Array.isArray(d.stale_by) && d.stale_by.length > 0;
}

function nodeClasses(d) {
  let cls = 'node-circle';
  if (isRetracted(d)) cls += ' retracted';
  else if (!d.is_current) cls += ' superseded';
  if (isTainted(d)) cls += ' tainted';
  if (selectedNodeId === d.id) cls += ' selected';
  // What's new since last visit (#1169 slice-2): newNodeIds is frozen after
  // initWhatsNew() and never recomputed per-render, so this is stable.
  if (newNodeIds.has(d.id)) cls += ' new-node-glow';
  return cls;
}

// ═══════════════════════════════════════════════════════════════════
// Data filtering
// ═══════════════════════════════════════════════════════════════════

// Build the legible-default node set: identity backbone + 60 most-recent others.
// Returns a Set of node IDs that should be included in legible mode.
// Does NOT apply showSuperseded or activeTypes — those are applied on top by
// filteredData() so all three controls remain composable.
function legibleNodeIds() {
  const all = rawData.nodes;
  // Small-graph no-op: if total nodes <= threshold, show everything.
  if (all.length <= LEGIBLE_SMALL_GRAPH) {
    return new Set(all.map(n => n.id));
  }

  // 1. Identity backbone -- all cornerstone/axiom/goal/person nodes.
  const backboneIds = new Set();
  all.forEach(n => {
    if (BACKBONE_TYPES.has(n.type)) backboneIds.add(n.id);
  });

  // 2. Recent activity -- 60 most-recent nodes (by created_at DESC) not already in backbone.
  const nonBackbone = all
    .filter(n => !backboneIds.has(n.id))
    .slice()
    .sort((a, b) => {
      // created_at is an ISO string; lexicographic sort works for ISO 8601.
      // Nulls sort to the end (oldest).
      const ta = a.created_at || '';
      const tb = b.created_at || '';
      return tb < ta ? -1 : tb > ta ? 1 : 0;
    })
    .slice(0, LEGIBLE_RECENT_LIMIT);

  const result = new Set(backboneIds);
  nonBackbone.forEach(n => result.add(n.id));
  return result;
}

function filteredData() {
  // In legible-default mode, restrict to the backbone+recent subset first.
  // Backbone nodes (cornerstone/axiom/goal/person) bypass the activeTypes check
  // so they are always visible in legible mode regardless of filter state.
  // The showSuperseded filter is applied uniformly to both modes.
  let nodes;
  if (!showFullGraph) {
    const allowed = legibleNodeIds();
    nodes = rawData.nodes.filter(n =>
      allowed.has(n.id) &&
      (BACKBONE_TYPES.has(n.type) || activeTypes.has(n.type)) &&
      (showSuperseded || n.is_current)
    );
  } else {
    nodes = rawData.nodes.filter(n =>
      activeTypes.has(n.type) && (showSuperseded || n.is_current)
    );
  }
  const nodeIds = new Set(nodes.map(n => n.id));
  let edges = rawData.edges
    .map(e => ({
      ...e,
      source: e.source_id,
      target: e.target_id,
    }))
    .filter(e =>
      nodeIds.has(e.source) && nodeIds.has(e.target)
    );
  return { nodes, edges };
}

// ═══════════════════════════════════════════════════════════════════
// Render
// ═══════════════════════════════════════════════════════════════════

function render() {
  const { nodes, edges } = filteredData();
  const tierMap = computeTiers(rawData.nodes);

  document.getElementById('empty-msg').style.display =
    nodes.length === 0 ? 'block' : 'none';

  // Preserve existing positions
  const posMap = new Map();
  simulation.nodes().forEach(n => posMap.set(n.id, { x: n.x, y: n.y, vx: n.vx, vy: n.vy }));
  nodes.forEach(n => {
    const p = posMap.get(n.id);
    if (p) { n.x = p.x; n.y = p.y; n.vx = p.vx; n.vy = p.vy; }
  });

  // Links
  const link = linkGroup
    .selectAll('line.link')
    .data(edges, d => `${d.source_id || (d.source && d.source.id) || d.source}→${d.target_id || (d.target && d.target.id) || d.target}→${d.relation}`)
    .join(
      enter => enter.append('line')
        .attr('class', d => `link ${d.relation}`)
        .attr('stroke', d => (EDGE_STYLE[d.relation] || { color: '#666' }).color)
        .attr('stroke-width', 1.5)
        .attr('stroke-dasharray', d => (EDGE_STYLE[d.relation] || { dash: '' }).dash || null)
        .attr('marker-end', d => `url(#arrow-${d.relation})`)
        .attr('opacity', 0)
        .call(sel => sel.transition().duration(400).attr('opacity', 1)),
      update => update
        .attr('stroke', d => (EDGE_STYLE[d.relation] || { color: '#666' }).color)
        .attr('stroke-dasharray', d => (EDGE_STYLE[d.relation] || { dash: '' }).dash || null)
        .attr('marker-end', d => `url(#arrow-${d.relation})`),
      exit => exit.transition().duration(300).attr('opacity', 0).remove()
    );

  // Nodes
  const node = nodeGroup
    .selectAll('g.node')
    .data(nodes, d => d.id)
    .join(
      enter => {
        const g = enter.append('g')
          .attr('class', 'node')
          .call(d3.drag()
            .on('start', dragStart)
            .on('drag', dragged)
            .on('end', dragEnd))
          .on('click', (event, d) => {
            event.stopPropagation();
            selectNode(d);
          })
          .on('mouseover', showTooltip)
          .on('mousemove', moveTooltip)
          .on('mouseout', hideTooltip);

        g.append('circle')
          .attr('class', d => nodeClasses(d))
          .attr('r', 0)
          .attr('fill', d => nodeColor(d))
          .attr('stroke', d => d3.color(nodeColor(d)).darker(0.8))
          .attr('opacity', d => nodeOpacity(d, tierMap))
          .call(sel => sel.transition().duration(400).attr('r', d => nodeRadius(d)));

        // Status ring for resolved/retracted/tainted nodes
        g.filter(d => d.status && d.status !== 'active' && d.status !== 'open')
          .append('circle')
          .attr('class', 'status-ring')
          .attr('r', d => nodeRadius(d) + 3)
          .attr('fill', 'none')
          .attr('stroke', d => {
            if (d.status === 'resolved' || d.status === 'confirmed' || d.status === 'supported') return '#5cb85c';
            if (d.status === 'retracted') return '#e05252';
            if (d.status === 'refuted') return '#e05252';
            if (d.status === 'inconclusive') return '#7788aa';
            if (d.status.startsWith('partially')) return '#f0ad4e';
            return '#7788aa';
          })
          .attr('stroke-width', d => d.status === 'retracted' ? 2 : 1.5)
          .attr('stroke-dasharray', d =>
            d.status.startsWith('partially') ? '3 2' :
            d.status === 'retracted' ? '2 2' :
            d.status === 'inconclusive' ? '4 3' : 'none')
          .attr('opacity', d => nodeOpacity(d, tierMap) * 0.7);

        g.append('text')
          .attr('class', 'node-label')
          .attr('dx', d => nodeRadius(d) + 4)
          .attr('dy', '0.35em')
          .text(d => truncate(nodeDisplayText(d), 22));

        return g;
      },
      update => {
        update.select('circle.node-circle')
          .attr('class', d => nodeClasses(d))
          .transition().duration(300)
          .attr('r', d => nodeRadius(d))
          .attr('fill', d => nodeColor(d))
          .attr('stroke', d => d3.color(nodeColor(d)).darker(0.8))
          .attr('opacity', d => nodeOpacity(d, tierMap));

        // Update status rings
        update.select('circle.status-ring')
          .attr('r', d => nodeRadius(d) + 3)
          .attr('opacity', d => nodeOpacity(d, tierMap) * 0.7);

        update.select('text.node-label')
          .attr('dx', d => nodeRadius(d) + 4)
          .attr('opacity', d => nodeOpacity(d, tierMap))
          .text(d => showLabels ? truncate(nodeDisplayText(d), 22) : '');

        return update;
      },
      exit => exit.transition().duration(300).attr('opacity', 0).remove()
    );

  // Label visibility
  nodeGroup.selectAll('text.node-label')
    .style('display', showLabels ? null : 'none');

  // Update simulation
  simulation.nodes(nodes);
  simulation.force('link').links(edges);

  // Gently reheat if new nodes arrived
  const wasEmpty = posMap.size === 0;
  simulation.alpha(wasEmpty ? 1 : 0.15).restart();

  // Update "Showing N of M" counter on every render so it reflects the
  // current rendered subset (#1172: was stuck at fetchGraph totals and
  // never updated when Show full graph / type filters / Show superseded toggled).
  // M (the denominator) is the size of the universe the toggles filter FROM,
  // which depends on showSuperseded: with it ON, superseded nodes are part of
  // the rendered set, so M must include them too — otherwise rendered (incl
  // superseded) can exceed a current-only total and the counter shows the
  // nonsensical "Showing 1095 of 1064" (N > M).
  if (rawData && rawData.nodes) {
    const totalUniverse = showSuperseded
      ? rawData.nodes.length
      : rawData.nodes.filter(n => n.is_current).length;
    const renderedCount = nodes.length;
    const nodeText = renderedCount === totalUniverse
      ? `${totalUniverse} nodes`
      : `Showing ${renderedCount} of ${totalUniverse} nodes`;
    document.getElementById('stat-nodes').textContent = nodeText;
  }
}

// ═══════════════════════════════════════════════════════════════════
// Tick
// ═══════════════════════════════════════════════════════════════════

function ticked() {
  linkGroup.selectAll('line.link').each(function(d) {
    const src = d.source, tgt = d.target;
    if (!src || !tgt) return;
    const dx = tgt.x - src.x;
    const dy = tgt.y - src.y;
    const dist = Math.sqrt(dx * dx + dy * dy);
    if (dist === 0) return;
    const r = nodeRadius(tgt) + 2;
    const ux = dx / dist, uy = dy / dist;
    d3.select(this)
      .attr('x1', src.x)
      .attr('y1', src.y)
      .attr('x2', tgt.x - ux * r)
      .attr('y2', tgt.y - uy * r);
  });

  nodeGroup.selectAll('g.node')
    .attr('transform', d => `translate(${d.x},${d.y})`);
}

// ═══════════════════════════════════════════════════════════════════
// Drag
// ═══════════════════════════════════════════════════════════════════

function dragStart(event, d) {
  if (!event.active) simulation.alphaTarget(0.3).restart();
  d.fx = d.x; d.fy = d.y;
}
function dragged(event, d) { d.fx = event.x; d.fy = event.y; }
function dragEnd(event, d) {
  if (!event.active) simulation.alphaTarget(0);
  d.fx = null; d.fy = null;
}

// ═══════════════════════════════════════════════════════════════════
// Tooltip
// ═══════════════════════════════════════════════════════════════════

const tooltip = document.getElementById('tooltip');

function showTooltip(event, d) {
  const tierMap = computeTiers(rawData.nodes);
  const tier = tierMap[d.id] || 3;
  const tierLabel = tier === 1 ? 'Tier 1 (working)' : tier === 2 ? 'Tier 2 (searchable)' : 'Tier 3 (archive)';
  const tierColor = tier === 1 ? '#5cb85c' : tier === 2 ? '#f0ad4e' : '#e05252';

  let lines = [`<b>${d.id}</b> <span style="color:#7788aa">${NODE_STYLE[d.type]?.label || d.type}</span>`];
  const text = nodeDisplayText(d);
  if (text !== d.id) lines.push(truncate(text, 80));
  if (d.confidence != null) lines.push(`Confidence: ${(d.confidence * 100).toFixed(0)}%`);
  if (d.importance_score != null) lines.push(`Importance: ${d.importance_score.toFixed(4)} · <span style="color:${tierColor}">${tierLabel}</span>`);
  if (d.status && d.status !== 'active' && d.status !== 'open')
    lines.push(`Status: <span style="color:${d.status === 'retracted' ? '#e05252' : '#f0ad4e'}">${d.status}</span>`);
  if (isRetracted(d)) lines.push('<span style="color:#e05252">⊘ Retracted</span>');
  else if (!d.is_current) lines.push('<span style="color:#f0ad4e">⚠ Superseded</span>');
  if (isTainted(d)) lines.push('<span style="color:#ff9800">⚠ Tainted</span>');
  if (isStale(d)) lines.push('<span style="color:#d4c742">⚠ Stale</span>');
  tooltip.innerHTML = lines.join('<br>');
  tooltip.style.opacity = '1';
}

function moveTooltip(event) {
  tooltip.style.left = (event.clientX + 14) + 'px';
  tooltip.style.top = (event.clientY - 10) + 'px';
}

function hideTooltip() { tooltip.style.opacity = '0'; }

// ═══════════════════════════════════════════════════════════════════
// Node detail panel
// ═══════════════════════════════════════════════════════════════════

function selectNode(d) {
  selectedNodeId = d.id;

  // Highlight
  nodeGroup.selectAll('circle.node-circle')
    .classed('selected', nd => nd.id === d.id);

  // Panel header
  document.getElementById('detail-id').textContent = d.id;
  const badge = document.getElementById('detail-type-badge');
  badge.textContent = NODE_STYLE[d.type]?.label || d.type;
  badge.style.background = (nodeColor(d) + '33');
  badge.style.color = nodeColor(d);
  badge.style.border = `1px solid ${nodeColor(d)}55`;

  // Build body
  const body = document.getElementById('detail-body');
  body.innerHTML = '';

  if (isRetracted(d)) {
    body.insertAdjacentHTML('beforeend', '<div class="retracted-badge">⊘ Retracted</div>');
  } else if (!d.is_current) {
    body.insertAdjacentHTML('beforeend', '<div class="superseded-badge">Superseded</div>');
  }
  if (isTainted(d)) {
    const by = d.tainted_by || [];
    body.insertAdjacentHTML('beforeend',
      `<div class="tainted-badge">⚠ Tainted by: ${by.join(', ')}</div>`);
  }
  if (isStale(d)) {
    const by = d.stale_by || [];
    // stale_replacement is a plain {root: replacement_id} object, attached
    // server-side per node (#2013/#2009 slice 1) — one entry per pending
    // stale marker's root, not the old per-downstream-node cache.
    const repl = d.stale_replacement || {};
    const replStr = by.map(r => repl[r] || '?').join(', ') || '?';
    body.insertAdjacentHTML('beforeend',
      `<div class="stale-badge">⚠ Stale — premise ${by.join(', ')} superseded by ${replStr}</div>`);
  }

  function field(label, value, mono) {
    if (!value && value !== 0) return;
    body.insertAdjacentHTML('beforeend',
      `<div class="detail-section">
        <div class="detail-label">${label}</div>
        <div class="detail-value${mono ? ' mono' : ''}">${escHtml(String(value))}</div>
      </div>`
    );
  }

  // ── Recall summary + keywords (shown at TOP, before type-specific fields) ──
  if (d.recall_summary) {
    body.insertAdjacentHTML('beforeend',
      `<div class="detail-section">
        <div class="detail-label">Summary</div>
        <div class="detail-value" style="line-height:1.5">${escHtml(d.recall_summary)}</div>
      </div>`
    );
  }
  if (Array.isArray(d.recall_keywords) && d.recall_keywords.length > 0) {
    body.insertAdjacentHTML('beforeend',
      `<div class="detail-section">
        <div class="detail-label">Keywords</div>
        <div class="detail-value" style="color:#99ccff">${d.recall_keywords.map(k => escHtml(String(k))).join(' · ')}</div>
      </div>`
    );
  }

  // Type-specific fields
  switch (d.type) {
    case 'evidence':
      field('Title', d.source_title);
      field('URL', d.source_url, true);
      field('Domain', d.source_domain);
      field('Accessed', d.source_accessed);
      if (d.content_snippet) field('Snippet', d.content_snippet);
      break;

    case 'observation_factual':
    case 'observation_predictive':
      field('Claim', d.claim);
      field('Quote type', d.quote_type);
      if (d.source_class) field('Source class', d.source_class);
      if (d.confidence != null) confBar(body, d.confidence);
      field('Quoted text', d.quoted_text);
      field('Interpretation', d.interpretation);
      if (d.predicted_event) field('Predicted event', d.predicted_event);
      if (d.resolution_timeframe) field('Timeframe', d.resolution_timeframe);
      break;

    case 'prediction':
      field('Predicted event', d.predicted_event);
      field('Status', d.status || 'active');
      if (d.confidence != null) confBar(body, d.confidence);
      field('Timeframe', d.resolution_timeframe);
      if (d.resolved_by) field('Resolved by', d.resolved_by, true);
      break;

    case 'derivation':
    case 'theory':
      field('Claim', d.claim);
      if (d.confidence != null) confBar(body, d.confidence);
      // Infer derivation mode from edges: if all edges are derives_from → chain, check for resolves edge too
      {
        const myEdges = rawData.edges.filter(e => {
          const src = e.source_id || (e.source?.id) || e.source;
          return src === d.id;
        });
        const resolvesEdge = myEdges.find(e => e.relation === 'resolves');
        if (resolvesEdge) {
          const targetId = resolvesEdge.target_id || (resolvesEdge.target?.id) || resolvesEdge.target;
          field('Resolves', targetId, true);
        }
      }
      field('Logical chain', d.logical_chain);
      break;

    case 'contradiction':
      field('Claim', d.claim);
      field('Status', d.status || 'open');
      if (d.resolved_by) field('Resolved by', d.resolved_by, true);
      break;

    case 'question':
      field('Claim', d.claim);
      field('Status', d.status || 'open');
      if (d.resolved_by) field('Resolved by', d.resolved_by, true);
      break;

    case 'axiom':
      field('Claim', d.claim);
      if (d.confidence != null) confBar(body, d.confidence);
      field('Basis', d.logical_chain);
      break;

    case 'definition':
      {
        let term = '', defn = '';
        try {
          const meta = typeof d.metadata === 'string' ? JSON.parse(d.metadata) : d.metadata;
          if (meta) { term = meta.term || ''; defn = meta.definition || ''; }
        } catch(e) {}
        field('Term', term);
        field('Definition', defn);
      }
      break;

    case 'conjecture':
      field('Claim', d.claim);
      field('Status', d.status || 'active');
      if (d.confidence != null) confBar(body, d.confidence);
      field('Basis', d.logical_chain);
      if (d.resolved_by) field('Resolved by', d.resolved_by, true);
      break;

    case 'feeling_report':
      field('Claim', d.claim);
      if (d.categorical_tag) field('Tag', d.categorical_tag);
      if (d.intensity_hint != null) field('Intensity', d.intensity_hint.toFixed(2));
      if (d.reported_state) field('Reported state', d.reported_state);
      if (d.trigger_text) field('Trigger', d.trigger_text);
      if (d.nudge_source) field('Nudge source', d.nudge_source);
      break;

    case 'goal':
      field('Claim', d.claim);
      field('Status', d.status || 'open');
      {
        const meta = (() => { try { return typeof d.metadata === 'string' ? JSON.parse(d.metadata) : (d.metadata || {}); } catch(e) { return {}; } })();
        if (meta.motivation) field('Motivation', meta.motivation);
        else if (d.logical_chain) field('Motivation', d.logical_chain);
      }
      break;

    case 'goal_tension':
      field('Claim', d.claim);
      field('Status', d.status || 'open');
      {
        // Linked goals are expressed as 'tensions' edges — surface from rawData.edges
        const tensionsEdges = rawData.edges.filter(e => {
          const src = e.source_id || (e.source?.id) || e.source;
          return src === d.id && e.relation === 'tensions';
        });
        tensionsEdges.forEach((e, i) => {
          const tgt = e.target_id || (e.target?.id) || e.target;
          field(`Goal ${i + 1}`, tgt, true);
        });
        if (d.logical_chain) field('Analysis', d.logical_chain);
      }
      break;

    case 'person':
      {
        const meta = (() => { try { return typeof d.metadata === 'string' ? JSON.parse(d.metadata) : (d.metadata || {}); } catch(e) { return {}; } })();
        field('Name', meta.name || d.claim);
        if (meta.role) field('Role', meta.role);
        if (meta.is_self) field('Self', 'yes (agent self-anchor)');
        if (meta.aliases && meta.aliases.length > 0) field('Aliases', meta.aliases.join(', '));
        if (d.logical_chain) field('Background', d.logical_chain);
        // Aboutness: count nodes that cite this person node
        const aboutCount = rawData.edges.filter(e => {
          const tgt = e.target_id || (e.target?.id) || e.target;
          return tgt === d.id && (e.relation === 'about' || e.relation === 'cites');
        }).length;
        if (aboutCount > 0) field('Referenced by', `${aboutCount} node(s)`);
      }
      break;

    case 'lesson':
      field('Claim', d.claim);
      if (d.confidence != null) confBar(body, d.confidence);
      {
        const meta = (() => { try { return typeof d.metadata === 'string' ? JSON.parse(d.metadata) : (d.metadata || {}); } catch(e) { return {}; } })();
        if (meta.scaffolding_nudge) field('Nudge', meta.scaffolding_nudge);
        // incident_count from metadata; fall back to counting exemplifies edges
        const exemplifiesEdges = rawData.edges.filter(e => {
          const tgt = e.target_id || (e.target?.id) || e.target;
          return tgt === d.id && e.relation === 'exemplifies';
        });
        const incCount = meta.incident_count != null ? meta.incident_count : exemplifiesEdges.length;
        if (incCount > 0) field('Incidents', incCount);
      }
      break;

    case 'task':
      field('Claim', d.claim);
      field('Status', d.status || 'planned');
      {
        const meta = (() => { try { return typeof d.metadata === 'string' ? JSON.parse(d.metadata) : (d.metadata || {}); } catch(e) { return {}; } })();
        if (meta.scope) field('Scope', meta.scope);
        // parent_task_id: look for subtask_of edge from this node
        const subtaskEdge = rawData.edges.find(e => {
          const src = e.source_id || (e.source?.id) || e.source;
          return src === d.id && e.relation === 'subtask_of';
        });
        if (subtaskEdge) {
          const parentId = subtaskEdge.target_id || (subtaskEdge.target?.id) || subtaskEdge.target;
          field('Parent task', parentId, true);
        }
      }
      break;

    default:
      field('Claim', d.claim);
      if (d.confidence != null) confBar(body, d.confidence);
  }

  field('Created', d.created_at);
  if (d.supersedes) field('Supersedes', d.supersedes, true);
  if (d.superseded_by) field('Superseded by', d.superseded_by, true);

  // Memory management section
  if (d.importance_score != null || d.importance_base != null) {
    const tierMap = computeTiers(rawData.nodes);
    const tier = tierMap[d.id] || 3;
    const tierLabel = tier === 1 ? 'Tier 1 (working memory)' : tier === 2 ? 'Tier 2 (searchable)' : 'Tier 3 (archive)';
    const tierColor = tier === 1 ? '#5cb85c' : tier === 2 ? '#f0ad4e' : '#e05252';

    body.insertAdjacentHTML('beforeend',
      `<div class="detail-section">
        <div class="detail-label">Memory</div>
        <div class="detail-value">
          <div style="margin-bottom:3px"><span style="color:${tierColor};font-weight:600">${tierLabel}</span></div>
          ${d.importance_base != null ? `<div>Base: ${d.importance_base.toFixed(4)}</div>` : ''}
          ${d.importance_score != null ? `<div>Score: ${d.importance_score.toFixed(4)}</div>` : ''}
          ${d.recall_turn != null ? `<div>Last recall: turn ${d.recall_turn}</div>` : ''}
          ${d.recall_count ? `<div>Recall count: ${d.recall_count}</div>` : ''}
          ${d.utility_score ? `<div>Utility: ${d.utility_score.toFixed(4)}</div>` : ''}
          ${d.memory_status && d.memory_status !== 'active' ? `<div>Status: ${d.memory_status}</div>` : ''}
        </div>
      </div>`
    );
  }

  // Connected edges — grouped by direction
  const related = rawData.edges.filter(e => {
    const src = e.source_id || (e.source?.id) || e.source;
    const tgt = e.target_id || (e.target?.id) || e.target;
    return src === d.id || tgt === d.id;
  });

  if (related.length > 0) {
    // Split into outgoing (this depends on) and incoming (depends on this)
    const outgoing = [];
    const incoming = [];
    related.forEach(e => {
      const src = e.source_id || (e.source?.id) || e.source;
      if (src === d.id) outgoing.push(e);
      else incoming.push(e);
    });

    body.insertAdjacentHTML('beforeend',
      `<div class="detail-section">
        <div class="detail-label">Connections (${related.length})</div>
        ${outgoing.length > 0 ? '<div class="direction-label" style="margin:6px 0 4px">This node depends on:</div>' : ''}
        <div id="edge-chips-out"></div>
        ${incoming.length > 0 ? '<div class="direction-label" style="margin:6px 0 4px">Depended on by:</div>' : ''}
        <div id="edge-chips-in"></div>
      </div>`
    );

    function renderChips(edges, containerId, isOutgoing) {
      const container = document.getElementById(containerId);
      edges.forEach(e => {
        const src = e.source_id || (e.source?.id) || e.source;
        const tgt = e.target_id || (e.target?.id) || e.target;
        const otherId = isOutgoing ? tgt : src;
        const color = (EDGE_STYLE[e.relation] || { color: '#888' }).color;
        const otherNode = rawData.nodes.find(n => n.id === otherId);
        const otherType = otherNode ? (NODE_STYLE[otherNode.type]?.label || otherNode.type) : '';
        const otherClaim = otherNode ? truncate(nodeDisplayText(otherNode), 60) : '';
        const otherColor = otherNode ? nodeColor(otherNode) : '#888';

        const chip = document.createElement('div');
        chip.className = 'edge-chip';
        chip.style.display = 'block';
        chip.innerHTML =
          `<div style="display:flex;align-items:center;gap:5px">
            <span style="color:${color};font-size:10px">${isOutgoing ? '→' : '←'}</span>
            <span class="relation-tag" style="color:${color}">${edgeDirectionLabel(e.relation, isOutgoing)}</span>
            <span style="font-family:monospace;color:#aaccff">${otherId}</span>
            <span style="font-size:9px;color:${otherColor};opacity:0.7">${otherType}</span>
          </div>
          ${otherClaim ? '<span class="chip-claim">' + escHtml(otherClaim) + '</span>' : ''}`;
        chip.onclick = () => {
          if (otherNode) selectNode(otherNode);
        };
        container.appendChild(chip);
      });
    }

    renderChips(outgoing, 'edge-chips-out', true);
    renderChips(incoming, 'edge-chips-in', false);
  }

  // Subgraph traversal controls
  body.insertAdjacentHTML('beforeend',
    `<div class="detail-section">
      <div class="detail-label">Subgraph Traversal</div>
      <div class="subgraph-controls">
        <div class="subgraph-btn" id="sg-upstream" title="Trace toward evidence/premises">&#x2191; Upstream</div>
        <div class="subgraph-btn" id="sg-downstream" title="Trace toward conclusions/derivations">&#x2193; Downstream</div>
        <div class="subgraph-btn" id="sg-both" title="Trace both directions">&#x2195; Both</div>
        <div class="subgraph-btn clear" id="sg-clear" title="Clear highlighting">&#x2715; Clear</div>
      </div>
      <div style="display:flex;align-items:center;gap:6px;margin-top:6px">
        <span style="font-size:10px;color:#7788aa">Depth:</span>
        <div class="depth-selector">
          <div class="depth-btn${highlightDepth===1?' active':''}" data-depth="1">1</div>
          <div class="depth-btn${highlightDepth===2?' active':''}" data-depth="2">2</div>
          <div class="depth-btn${highlightDepth===3?' active':''}" data-depth="3">3</div>
          <div class="depth-btn${highlightDepth===5?' active':''}" data-depth="5">5</div>
        </div>
      </div>
      <div class="subgraph-info" id="sg-info"></div>
    </div>`
  );

  // Wire up depth buttons
  document.querySelectorAll('.depth-btn').forEach(btn => {
    btn.onclick = () => {
      highlightDepth = parseInt(btn.dataset.depth);
      document.querySelectorAll('.depth-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      // Re-apply highlight if active
      if (highlightedNodeIds.size > 0 && selectedNodeId) {
        const activeBtn = document.querySelector('.subgraph-btn.active');
        if (activeBtn) activeBtn.click();
      }
    };
  });

  // Wire up subgraph buttons
  function setupSgBtn(btnId, direction) {
    document.getElementById(btnId).onclick = () => {
      document.querySelectorAll('.subgraph-btn').forEach(b => b.classList.remove('active'));
      document.getElementById(btnId).classList.add('active');
      const nodeIds = traverseSubgraph(d.id, direction, highlightDepth);
      applyHighlight(nodeIds);
      const info = document.getElementById('sg-info');
      if (info) info.textContent = `${nodeIds.size} nodes highlighted (depth ${highlightDepth})`;
    };
  }
  setupSgBtn('sg-upstream', 'upstream');
  setupSgBtn('sg-downstream', 'downstream');
  setupSgBtn('sg-both', 'both');
  document.getElementById('sg-clear').onclick = () => {
    document.querySelectorAll('.subgraph-btn').forEach(b => b.classList.remove('active'));
    clearHighlight();
    const info = document.getElementById('sg-info');
    if (info) info.textContent = '';
  };

  document.getElementById('detail-panel').classList.add('open');
}

function confBar(container, value) {
  container.insertAdjacentHTML('beforeend',
    `<div class="detail-section">
      <div class="detail-label">Confidence</div>
      <div class="detail-value">${(value * 100).toFixed(0)}%</div>
      <div class="conf-bar-wrap"><div class="conf-bar" style="width:${(value * 100).toFixed(1)}%"></div></div>
    </div>`
  );
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ═══════════════════════════════════════════════════════════════════
// Subgraph traversal
// ═══════════════════════════════════════════════════════════════════

function traverseSubgraph(startId, direction, maxDepth) {
  // BFS from startId following edges in the specified direction.
  // direction: 'upstream' = follow source→target (toward evidence/premises)
  //            'downstream' = follow target→source (toward conclusions)
  //            'both' = follow both directions
  const visited = new Set([startId]);
  let frontier = [startId];

  for (let depth = 0; depth < maxDepth; depth++) {
    const nextFrontier = [];
    for (const nid of frontier) {
      for (const e of rawData.edges) {
        const src = e.source_id || (e.source?.id) || e.source;
        const tgt = e.target_id || (e.target?.id) || e.target;

        if (direction === 'upstream' || direction === 'both') {
          // source DEPENDS ON target — upstream follows source→target
          if (src === nid && !visited.has(tgt)) {
            visited.add(tgt);
            nextFrontier.push(tgt);
          }
        }
        if (direction === 'downstream' || direction === 'both') {
          // downstream: who depends on this node? target←source
          if (tgt === nid && !visited.has(src)) {
            visited.add(src);
            nextFrontier.push(src);
          }
        }
      }
    }
    if (nextFrontier.length === 0) break;
    frontier = nextFrontier;
  }
  return visited;
}

function applyHighlight(nodeIds) {
  highlightedNodeIds = nodeIds;

  // Dim/highlight nodes
  nodeGroup.selectAll('g.node').each(function(d) {
    const g = d3.select(this);
    const isHighlighted = nodeIds.has(d.id);
    g.select('circle.node-circle')
      .classed('sg-dimmed', !isHighlighted)
      .classed('sg-highlight', isHighlighted);
    g.select('text.node-label')
      .classed('sg-dimmed', !isHighlighted);
    g.select('circle.status-ring')
      .classed('sg-dimmed', !isHighlighted);
  });

  // Dim/highlight edges
  linkGroup.selectAll('line.link').each(function(e) {
    const src = e.source_id || (e.source?.id) || e.source;
    const tgt = e.target_id || (e.target?.id) || e.target;
    const both = nodeIds.has(src) && nodeIds.has(tgt);
    d3.select(this).classed('sg-dimmed', !both);
  });
}

function clearHighlight() {
  highlightedNodeIds = new Set();
  nodeGroup.selectAll('circle.node-circle')
    .classed('sg-dimmed', false).classed('sg-highlight', false);
  nodeGroup.selectAll('text.node-label')
    .classed('sg-dimmed', false);
  nodeGroup.selectAll('circle.status-ring')
    .classed('sg-dimmed', false);
  linkGroup.selectAll('line.link')
    .classed('sg-dimmed', false);
}

// Edge direction semantic labels
function edgeDirectionLabel(relation, isOutgoing) {
  // In ENGRAM: source DEPENDS ON target (outgoing = this node depends on other)
  const labels = {
    derives_from: ['derives from', 'derived by'],
    cites:        ['cites', 'cited by'],
    supported_by: ['supported by', 'supports'],
    predicted_by: ['predicted by', 'predicts'], // #2252 W6: pairing edge
                                                 // split out of supported_by,
                                                 // same direction/shape.
    contradicts:  ['contradicts', 'contradicted by'],
    resolves:     ['resolves', 'resolved by'],
    supersedes:   ['supersedes', 'superseded by'],
    retracts:     ['retracts', 'retracted by'],
    tensions:     ['tensions', 'tensions'],
  };
  const pair = labels[relation] || [relation, relation];
  return isOutgoing ? pair[0] : pair[1];
}

document.getElementById('close-detail').onclick = () => {
  document.getElementById('detail-panel').classList.remove('open');
  selectedNodeId = null;
  nodeGroup.selectAll('circle.node-circle').classed('selected', false);
  clearHighlight();
};

// ═══════════════════════════════════════════════════════════════════
// Search by node ID
// ═══════════════════════════════════════════════════════════════════

function searchAndFocus(query) {
  const input = document.getElementById('search-input');
  const q = query.trim().toLowerCase();
  if (!q) return;

  // Find node in raw data
  const d = rawData.nodes.find(n => n.id.toLowerCase() === q);
  if (!d) {
    input.classList.add('not-found');
    setTimeout(() => input.classList.remove('not-found'), 600);
    return;
  }

  // If node's type is currently filtered out, re-enable it
  if (!activeTypes.has(d.type)) {
    activeTypes.add(d.type);
    buildSidebar();
    render();
  }

  // If node is superseded/retracted and we're hiding those, enable show-superseded
  if (!d.is_current && !showSuperseded) {
    showSuperseded = true;
    document.getElementById('show-superseded-cb').checked = true;
    render();
  }

  // If the node is outside the legible default subset (not backbone, not in the
  // most-recent set), switch to full-graph so it actually renders and gets x/y
  // coords from the force sim before we zoom — otherwise d.x/d.y are undefined
  // and the zoom transform becomes NaN, silently corrupting pan/zoom.
  if (!showFullGraph && !legibleNodeIds().has(d.id)) {
    showFullGraph = true;
    document.getElementById('show-full-graph-cb').checked = true;
    render();
  }

  // Select the node (opens detail panel)
  selectNode(d);

  // Center the view on the node with a smooth transition
  const svgEl = document.getElementById('graph-svg');
  const w = svgEl.clientWidth;
  const h = svgEl.clientHeight;
  const scale = 1.5;
  const tx = w / 2 - d.x * scale;
  const ty = h / 2 - d.y * scale;
  svg.transition().duration(600)
    .call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));

  // Pulse ring effect on the found node
  const nodeEl = nodeGroup.selectAll('g.node').filter(nd => nd.id === d.id);
  if (!nodeEl.empty()) {
    nodeEl.select('circle.pulse-ring').remove();  // clear previous
    nodeEl.insert('circle', ':first-child')
      .attr('class', 'pulse-ring')
      .attr('cx', 0).attr('cy', 0)
      .attr('fill', 'none')
      .attr('stroke', nodeColor(d))
      .attr('stroke-width', 2)
      .attr('r', 6)
      .attr('opacity', 0.8)
      .transition().duration(800)
      .attr('r', 24)
      .attr('opacity', 0)
      .remove();
  }

  input.value = '';
}

document.getElementById('search-btn').onclick = () => {
  searchAndFocus(document.getElementById('search-input').value);
};
document.getElementById('search-input').onkeydown = (e) => {
  if (e.key === 'Enter') searchAndFocus(e.target.value);
};

// ═══════════════════════════════════════════════════════════════════
// Semantic search — calls /api/search and renders result list
// ═══════════════════════════════════════════════════════════════════

async function runSemanticSearch(query) {
  const input = document.getElementById('semantic-search-input');
  const resultsEl = document.getElementById('semantic-search-results');
  const q = query.trim();
  if (!q) return;

  resultsEl.style.display = '';
  resultsEl.innerHTML = '<div style="color:#7788aa;font-size:11px">Searching…</div>';

  try {
    const baseUrl = withAgent('/api/search');
    const url = baseUrl + (baseUrl.includes('?') ? '&' : '?') + 'q=' + encodeURIComponent(q);
    const res = await fetch(url);
    const data = await res.json();

    if (data.error) {
      resultsEl.innerHTML = `<div style="color:#e05252;font-size:11px">${escHtml(data.error)}</div>`;
      return;
    }

    const results = data.results || [];
    if (results.length === 0) {
      resultsEl.innerHTML = '<div style="color:#7788aa;font-size:11px">No results.</div>';
      input.classList.add('not-found');
      setTimeout(() => input.classList.remove('not-found'), 600);
      return;
    }

    resultsEl.innerHTML = `<div style="font-size:10px;color:#556;margin-bottom:4px">${results.length} result(s)</div>`;
    results.forEach(r => {
      const item = document.createElement('div');
      item.className = 'search-result-item';
      const conf = r.confidence != null ? `${(r.confidence * 100).toFixed(0)}%` : '';
      item.innerHTML =
        `<div><span class="search-result-id">${escHtml(r.id)}</span>` +
        `<span class="search-result-type">${escHtml(r.type || '')}</span></div>` +
        `<div class="search-result-claim">${escHtml(truncate(r.claim || '', 80))}</div>` +
        (conf ? `<div class="search-result-conf">conf: ${conf}</div>` : '');
      item.onclick = () => {
        // Navigate to node in graph if it exists in rawData, else open detail via id-search
        const node = rawData.nodes.find(n => n.id === r.id);
        if (node) {
          searchAndFocus(r.id);
        } else {
          // Node may be filtered — try to open detail panel by searching
          document.getElementById('search-input').value = r.id;
          searchAndFocus(r.id);
        }
      };
      resultsEl.appendChild(item);
    });
  } catch (err) {
    resultsEl.innerHTML = `<div style="color:#e05252;font-size:11px">Search failed: ${escHtml(String(err))}</div>`;
  }
}

document.getElementById('semantic-search-btn').onclick = () => {
  runSemanticSearch(document.getElementById('semantic-search-input').value);
};
document.getElementById('semantic-search-input').onkeydown = (e) => {
  if (e.key === 'Enter') runSemanticSearch(e.target.value);
};

// Deselect on SVG background click — keep highlight active
svg.on('click', () => {
  document.getElementById('detail-panel').classList.remove('open');
  selectedNodeId = null;
  nodeGroup.selectAll('circle.node-circle').classed('selected', false);
  // Don't clear highlight here — user may be panning/zooming.
  // Highlight only clears when the detail panel close button is clicked.
});

// ═══════════════════════════════════════════════════════════════════
// Sidebar build
// ═══════════════════════════════════════════════════════════════════

function buildSidebar() {
  // Node type filters — built from NODE_STYLE (all 18 types from /api/schema,
  // including cornerstone which was previously missing).
  const tf = document.getElementById('type-filters');
  tf.innerHTML = '';
  Object.entries(NODE_STYLE).forEach(([key, { color, label }]) => {
    const div = document.createElement('div');
    div.className = `type-filter${activeTypes.has(key) ? '' : ' dimmed'}`;
    div.dataset.type = key;
    div.innerHTML = `
      <div class="type-dot" style="background:${color}"></div>
      <span>${label}</span>`;
    div.onclick = () => {
      if (activeTypes.has(key)) { activeTypes.delete(key); div.classList.add('dimmed'); }
      else { activeTypes.add(key); div.classList.remove('dimmed'); }
      render();
    };
    tf.appendChild(div);
  });

  // Edge legend — built from EDGE_STYLE (all 13 relations from /api/schema).
  // Each swatch shows the relation's color AND its solid/dashed line style.
  const el = document.getElementById('edge-legend');
  el.innerHTML = '';
  Object.entries(EDGE_STYLE).forEach(([rel, { color, dash }]) => {
    const isDashed = !!dash;
    el.insertAdjacentHTML('beforeend',
      `<div class="edge-legend-row">
        <div class="edge-line" style="${isDashed
          ? 'background:repeating-linear-gradient(90deg,' + color + ' 0,' + color + ' 4px,transparent 4px,transparent 7px)'
          : 'background:' + color}"></div>
        <span>${rel}</span>
      </div>`
    );
  });
}

// ═══════════════════════════════════════════════════════════════════
// Multi-agent helpers
// ═══════════════════════════════════════════════════════════════════

function currentAgent() {
  return new URL(window.location.href).searchParams.get('agent');
}

function setCurrentAgent(name) {
  const url = new URL(window.location.href);
  if (name) {
    url.searchParams.set('agent', name);
  } else {
    url.searchParams.delete('agent');
  }
  window.history.replaceState(null, '', url);
}

function withAgent(path) {
  const a = currentAgent();
  if (!a) return path;
  return path + (path.includes('?') ? '&' : '?') + 'agent=' + encodeURIComponent(a);
}

// ═══════════════════════════════════════════════════════════════════
// Schema fetch — populates NODE_STYLE and EDGE_STYLE from /api/schema
// ═══════════════════════════════════════════════════════════════════

async function fetchSchema() {
  // /api/schema is agent-agnostic (static schema data), but we pass the
  // agent param if present for consistency with the rest of the API.
  try {
    const res = await fetch(withAgent('/api/schema'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const schema = await res.json();

    // Build NODE_STYLE: one entry per node_types entry
    NODE_STYLE = {};
    (schema.node_types || []).forEach(type => {
      NODE_STYLE[type] = {
        color: NODE_COLOR_OVERRIDE[type] || _hashStringToColor(type),
        label: NODE_LABEL_OVERRIDE[type] || _prettifyType(type),
      };
    });

    // Build EDGE_STYLE: one entry per relations entry
    // dash: solid (empty) if cascade||provenance; dashed ('4,3') otherwise
    EDGE_STYLE = {};
    const relMap = schema.relations || {};
    Object.entries(relMap).forEach(([rel, info]) => {
      const isSolid = !!(info.cascade || info.provenance);
      EDGE_STYLE[rel] = {
        color: EDGE_COLOR_OVERRIDE[rel] || _hashStringToColor(rel),
        dash: isSolid ? '' : '4,3',
      };
    });

    // Initialise activeTypes from the full NODE_STYLE key set (all schema types
    // are active by default — user can deselect from the filter panel).
    activeTypes = new Set(Object.keys(NODE_STYLE));

    // Build arrowhead markers for all 13 relations.
    buildArrowheadMarkers();
  } catch (err) {
    console.warn('fetchSchema failed — falling back to override maps:', err);
    // Fallback: build minimal style from the override maps so the page still
    // renders even if /api/schema is unavailable.
    NODE_STYLE = {};
    Object.entries(NODE_COLOR_OVERRIDE).forEach(([type, color]) => {
      NODE_STYLE[type] = { color, label: NODE_LABEL_OVERRIDE[type] || _prettifyType(type) };
    });
    EDGE_STYLE = {};
    Object.entries(EDGE_COLOR_OVERRIDE).forEach(([rel, color]) => {
      // Fallback dash: hand-maintained mirror of EDGE_CLASSIFICATIONS'
      // cascade/provenance flags, used only when /api/schema is unreachable.
      // Must be kept in sync by hand with EDGE_CLASSIFICATIONS whenever the
      // cascade/provenance flags change — it is not derived dynamically on
      // purpose, since it exists precisely for when the dynamic path failed.
      const solidRels = new Set(['cites', 'derives_from', 'supported_by', 'predicted_by']); // #2252 W6: predicted_by carries provenance:True, same as supported_by
      EDGE_STYLE[rel] = { color, dash: solidRels.has(rel) ? '' : '4,3' };
    });
    activeTypes = new Set(Object.keys(NODE_STYLE));
    buildArrowheadMarkers();
  }
}

async function loadAgents() {
  // Ensure the URL carries the correct ?agent= param before the first
  // fetchGraph() call, and sync memory-tier config for the active agent.
  // The nav-bar agent selector (shared across all tabs) handles switching
  // via full-page reload; we only need to set the URL default here.
  try {
    const res = await fetch('/api/agents');
    const data = await res.json();
    const agents = data.agents || [];
    const defaultAgent = data.default || (agents[0] && agents[0].name);
    if (agents.length <= 1) {
      // Single-agent mode — no URL param or dropdown needed.
    } else {
      let current = currentAgent();
      if (!current || !agents.find(a => a.name === current)) {
        current = defaultAgent;
        setCurrentAgent(current);
      }
    }
    // Always fetch meta to populate db-path-label and sync tier threshold.
    // Different agents can have different config.json values; without this
    // re-sync, switching agents would leave the prior agent's TIER2_MAX
    // active and compute tiers wrong. Tier-1 is retired (#1220).
    fetch(withAgent('/api/meta')).then(r => r.json()).then(m => {
      document.getElementById('db-path-label').textContent = m.db_path || '';
      if (m.memory_config) {
        if (typeof m.memory_config.tier2_max_nodes === 'number') {
          TIER2_MAX = m.memory_config.tier2_max_nodes;
        }
      }
    }).catch(() => {});
  } catch (err) {
    console.warn('Failed to load agents:', err);
  }
}

// ═══════════════════════════════════════════════════════════════════
// Data fetch
// ═══════════════════════════════════════════════════════════════════

async function fetchGraph() {
  if (_fetchGraphInFlight) return;
  _fetchGraphInFlight = true;
  try {
    const res = await fetch(withAgent('/api/graph'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (data.error) {
      console.warn('Graph API error:', data.error);
      document.getElementById('empty-msg').textContent = data.error;
      return;
    }

    rawData = data;

    // What's new since last visit (#1169 slice-2): initialise ONCE on first
    // fetch so the new-set is stable across auto-refresh re-renders.
    if (!_whatsNewInitialised) {
      initWhatsNew(data.nodes);
    }

    // stat-nodes is now updated inside render() on every re-render (#1172).

    // Type counts for stats panel
    const byCurrent = {};
    data.nodes.filter(n => n.is_current).forEach(n => {
      byCurrent[n.type] = (byCurrent[n.type] || 0) + 1;
    });
    const sd = document.getElementById('stats-detail');
    sd.innerHTML = '';
    Object.entries(NODE_STYLE).forEach(([key, { label }]) => {
      const cnt = byCurrent[key] || 0;
      if (cnt === 0) return;
      sd.insertAdjacentHTML('beforeend',
        `<div class="stat-row"><span class="k">${label}</span><span class="v">${cnt}</span></div>`
      );
    });

    // Memory tier distribution
    const tierMap = computeTiers(data.nodes);
    const tierCounts = {1: 0, 2: 0, 3: 0};
    data.nodes.filter(n => n.is_current).forEach(n => {
      const t = tierMap[n.id] || 3;
      tierCounts[t]++;
    });
    sd.insertAdjacentHTML('beforeend',
      `<div style="margin-top:8px;padding-top:6px;border-top:1px solid #0f3460">
        <div class="stat-row"><span class="k" style="color:#5cb85c">Tier 1 (working)</span><span class="v">${tierCounts[1]}</span></div>
        <div class="stat-row"><span class="k" style="color:#f0ad4e">Tier 2 (search)</span><span class="v">${tierCounts[2]}</span></div>
        <div class="stat-row"><span class="k" style="color:#e05252">Tier 3 (archive)</span><span class="v">${tierCounts[3]}</span></div>
      </div>`
    );

    // Edge relation counts
    const byRelation = {};
    data.edges.forEach(e => { byRelation[e.relation] = (byRelation[e.relation] || 0) + 1; });
    if (Object.keys(byRelation).length > 0) {
      sd.insertAdjacentHTML('beforeend',
        `<div style="margin-top:8px;padding-top:6px;border-top:1px solid #0f3460">` +
        Object.entries(byRelation).map(([rel, cnt]) =>
          `<div class="stat-row"><span class="k">${rel}</span><span class="v">${cnt}</span></div>`
        ).join('') + `</div>`
      );
    }

    const ts = new Date().toLocaleTimeString();
    document.getElementById('last-refresh').textContent = `Last updated: ${ts}`;

    render();

    // Refresh detail panel if a node is selected
    if (selectedNodeId) {
      const updated = rawData.nodes.find(n => n.id === selectedNodeId);
      if (updated) selectNode(updated);
    }
  } catch (err) {
    console.error('Fetch error:', err);
    document.getElementById('last-refresh').textContent = `Error: ${err.message}`;
  } finally {
    _fetchGraphInFlight = false;
  }
}

// ═══════════════════════════════════════════════════════════════════
// Controls
// ═══════════════════════════════════════════════════════════════════

document.getElementById('show-superseded-cb').onchange = function() {
  showSuperseded = this.checked;
  render();
};

document.getElementById('show-labels-cb').onchange = function() {
  showLabels = this.checked;
  nodeGroup.selectAll('text.node-label').style('display', showLabels ? null : 'none');
};

document.getElementById('show-tier-opacity-cb').onchange = function() {
  showTierOpacity = this.checked;
  render();
};

document.getElementById('show-full-graph-cb').onchange = function() {
  showFullGraph = this.checked;
  render();
};

document.getElementById('refresh-btn').onclick = fetchGraph;

document.getElementById('auto-refresh-cb').onchange = function() {
  if (this.checked) {
    autoRefreshInterval = setInterval(fetchGraph, 10000);
  } else {
    clearInterval(autoRefreshInterval);
    autoRefreshInterval = null;
  }
};

// Set db path label (agent-aware) AND sync memory tier threshold from
// the agent's config (Lei 2026-05-17: viz tier classification was wrong
// because tier sizes were hardcoded JS constants instead of read from
// $ENGRAM_HOME/config.json). Tier-1 is retired (#1220).
fetch(withAgent('/api/meta')).then(r => r.json()).then(m => {
  document.getElementById('db-path-label').textContent = m.db_path || '';
  if (m.memory_config) {
    if (typeof m.memory_config.tier2_max_nodes === 'number') {
      TIER2_MAX = m.memory_config.tier2_max_nodes;
    }
  }
}).catch(() => {});

// Center simulation on container resize
function updateSimCenter() {
  const rect = document.getElementById('main').getBoundingClientRect();
  simulation.force('x', d3.forceX(rect.width / 2).strength(0.04));
  simulation.force('y', d3.forceY(rect.height / 2).strength(0.04));
}
window.addEventListener('resize', () => { updateSimCenter(); simulation.alpha(0.1).restart(); });

// ═══════════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════════

updateSimCenter();
// Init sequence:
// 1. Resolve the agent URL param (loadAgents) so subsequent fetches are agent-scoped.
// 2. Fetch the schema SSoT (/api/schema) to populate NODE_STYLE + EDGE_STYLE.
// 3. Build the sidebar (type filters + edge legend) from the schema-derived maps.
// 4. Fetch the graph data and start auto-refresh.
loadAgents().then(() =>
  fetchSchema().then(() => {
    buildSidebar();
    fetchGraph();
    autoRefreshInterval = setInterval(fetchGraph, 10000);
  })
);
