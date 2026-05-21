#!/usr/bin/env python3
"""
ENGRAM Visualization Server

Serves a D3.js force-directed graph of an ENGRAM knowledge graph in the
browser. Stdlib-only. Two operating modes:

* Single-agent (--db PATH or default ~/.engram/knowledge.db):
  one anonymous "default" agent.
* Multi-agent (--config agents.json): a registered set of agents,
  each with its own DB; UI dropdown switches between them, all API
  endpoints accept ?agent=<name>.

Usage:
    python viz_server.py                                 # single-agent default
    python viz_server.py --db /path/to/knowledge.db      # single-agent custom
    python viz_server.py --config /path/to/agents.json   # multi-agent
"""

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DEFAULT_DB = str(Path.home() / ".engram" / "knowledge.db")
DEFAULT_PORT = 5001

# Agent registry — populated in main() from --config or --db.
# Maps name -> {"name": str, "label": str, "db": str (resolved abs path)}.
AGENTS: dict = {}
DEFAULT_AGENT: str = ""

# ---------------------------------------------------------------------------
# HTML / JS / CSS  (embedded so the server is a single file)
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>KG Memory Visualizer</title>
  <script src="https://d3js.org/d3.v7.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      display: flex;
      flex-direction: column;
      height: 100vh;
      background: #1a1a2e;
      color: #e0e0e0;
      font-family: 'Segoe UI', system-ui, sans-serif;
      font-size: 13px;
      overflow: hidden;
    }

    /* ── Sidebar ────────────────────────────────────────────────────── */
    #sidebar {
      width: 260px;
      min-width: 260px;
      background: #16213e;
      border-right: 1px solid #0f3460;
      display: flex;
      flex-direction: column;
      overflow-y: auto;
      padding: 0 0 12px 0;
    }

    #sidebar header {
      background: #0f3460;
      padding: 14px 16px;
      font-size: 15px;
      font-weight: 600;
      letter-spacing: 0.4px;
      color: #e0e0ff;
      border-bottom: 1px solid #1a4a8a;
    }

    #sidebar header small {
      display: block;
      font-size: 10px;
      font-weight: 400;
      color: #99aabb;
      margin-top: 3px;
      word-break: break-all;
    }

    .section {
      padding: 10px 14px;
      border-bottom: 1px solid #0f3460;
    }

    .section-title {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 1px;
      color: #7788aa;
      margin-bottom: 8px;
    }

    #status-line {
      font-size: 11px;
      color: #7788aa;
      margin-bottom: 6px;
    }

    #status-line span { color: #aaccff; }

    .btn {
      display: inline-block;
      padding: 5px 12px;
      background: #0f3460;
      border: 1px solid #1a4a8a;
      border-radius: 4px;
      color: #cce0ff;
      cursor: pointer;
      font-size: 12px;
      user-select: none;
    }
    .btn:hover { background: #1a4a8a; }
    .btn:active { background: #245090; }

    label.toggle {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
      cursor: pointer;
      user-select: none;
      color: #ccd;
    }

    input[type=checkbox] { accent-color: #4a90d9; }

    /* ── Search box ─────────────────────────────────────────── */
    #search-box {
      display: flex;
      gap: 6px;
    }
    #search-input {
      flex: 1;
      padding: 5px 8px;
      background: #0d1b36;
      border: 1px solid #1a4a8a;
      border-radius: 4px;
      color: #e0e0e0;
      font-size: 12px;
      font-family: 'Consolas', 'Menlo', monospace;
      outline: none;
    }
    #search-input:focus { border-color: #4a90d9; }
    #search-input::placeholder { color: #556; }
    #search-input.not-found {
      border-color: #e05252;
      animation: shake 0.3s ease;
    }
    @keyframes shake {
      0%, 100% { transform: translateX(0); }
      25% { transform: translateX(-4px); }
      75% { transform: translateX(4px); }
    }
    @keyframes pulse-ring {
      0% { r: 6; opacity: 0.8; }
      100% { r: 24; opacity: 0; }
    }

    .type-filter {
      display: flex;
      align-items: center;
      gap: 7px;
      margin-bottom: 5px;
      cursor: pointer;
      user-select: none;
    }

    .type-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      flex-shrink: 0;
    }

    .type-filter.dimmed { opacity: 0.35; }

    /* ── Stats ──────────────────────────────────────────────────────── */
    .stat-row {
      display: flex;
      justify-content: space-between;
      margin-bottom: 4px;
      font-size: 12px;
    }
    .stat-row .k { color: #99aabb; }
    .stat-row .v { color: #cce0ff; font-weight: 600; }

    /* ── Edge legend ────────────────────────────────────────────────── */
    .edge-legend-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 5px;
      font-size: 12px;
      color: #aab;
    }
    .edge-line {
      width: 28px;
      height: 2px;
      border-radius: 1px;
      flex-shrink: 0;
    }

    /* ── Main graph area ────────────────────────────────────────────── */
    #main {
      flex: 1;
      position: relative;
      overflow: hidden;
    }

    #graph-svg {
      width: 100%;
      height: 100%;
      display: block;
    }

    /* Empty state */
    #empty-msg {
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      text-align: center;
      color: #556;
      font-size: 15px;
      pointer-events: none;
    }

    /* Tooltip */
    #tooltip {
      position: fixed;
      background: #0f3460ee;
      border: 1px solid #1a4a8a;
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 12px;
      color: #dde;
      max-width: 280px;
      pointer-events: none;
      opacity: 0;
      transition: opacity 0.15s;
      z-index: 100;
      line-height: 1.5;
    }

    /* ── Detail panel ───────────────────────────────────────────────── */
    /* Collapses to 0 width when no node is selected so it doesn't reserve
       layout space; expands to 400px when .open is added on node click.
       Pre-fix: width:400px + transform:translateX(100%) hid the panel
       visually but the flex slot still consumed 400px from #main. */
    #detail-panel {
      width: 0;
      min-width: 0;
      background: #16213e;
      border-left: 1px solid #0f3460;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      transition: width 0.25s ease, min-width 0.25s ease;
    }

    #detail-panel.open {
      width: 400px;
      min-width: 400px;
    }

    #detail-header {
      background: #0f3460;
      padding: 12px 14px;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      border-bottom: 1px solid #1a4a8a;
    }

    #detail-header .node-id {
      font-size: 12px;
      font-family: monospace;
      color: #aaccff;
    }

    #detail-header .node-type-badge {
      font-size: 10px;
      border-radius: 3px;
      padding: 1px 6px;
      margin-top: 3px;
      display: inline-block;
    }

    #close-detail {
      cursor: pointer;
      font-size: 18px;
      color: #667;
      padding: 0 4px;
      line-height: 1;
    }
    #close-detail:hover { color: #aac; }

    #detail-body {
      flex: 1;
      overflow-y: auto;
      padding: 12px 14px;
    }

    .detail-section {
      margin-bottom: 14px;
    }

    .detail-label {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: #7788aa;
      margin-bottom: 4px;
    }

    .detail-value {
      color: #dde;
      line-height: 1.5;
      word-break: break-word;
    }

    .detail-value.mono { font-family: monospace; font-size: 11px; }

    .conf-bar-wrap {
      background: #0f3460;
      border-radius: 3px;
      height: 6px;
      margin-top: 4px;
    }
    .conf-bar {
      height: 100%;
      border-radius: 3px;
      background: linear-gradient(90deg, #4a90d9, #5cb85c);
    }

    .edge-chip {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      background: #0f3460;
      border: 1px solid #1a4a8a;
      border-radius: 3px;
      padding: 3px 7px;
      margin: 2px 3px 2px 0;
      font-size: 11px;
      cursor: pointer;
      color: #ccd;
    }
    .edge-chip:hover { background: #1a4a8a; }
    .edge-chip .chip-claim {
      font-size: 10px;
      color: #889;
      display: block;
      margin-top: 2px;
      line-height: 1.3;
    }

    .relation-tag {
      font-size: 9px;
      color: #99aabb;
      text-transform: uppercase;
    }

    .direction-label {
      font-size: 9px;
      color: #7788aa;
      font-style: italic;
    }

    /* ── Subgraph traversal controls ───────────────────────────────── */
    .subgraph-controls {
      display: flex;
      gap: 6px;
      align-items: center;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .subgraph-btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 5px 10px;
      background: #0d1b36;
      border: 1px solid #1a4a8a;
      border-radius: 4px;
      color: #aaccff;
      cursor: pointer;
      font-size: 11px;
      user-select: none;
    }
    .subgraph-btn:hover { background: #1a4a8a; border-color: #4a90d9; }
    .subgraph-btn.active { background: #1a4a8a; border-color: #4a90d9; color: #fff; }
    .subgraph-btn.clear { border-color: #e05252; color: #e09090; }
    .subgraph-btn.clear:hover { background: #3a1010; }

    .depth-selector {
      display: inline-flex;
      gap: 2px;
      margin-left: 4px;
    }
    .depth-btn {
      width: 22px;
      height: 22px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #0d1b36;
      border: 1px solid #1a4a8a;
      border-radius: 3px;
      color: #aaccff;
      cursor: pointer;
      font-size: 11px;
      font-weight: 600;
    }
    .depth-btn:hover { background: #1a4a8a; }
    .depth-btn.active { background: #4a90d9; color: #fff; border-color: #4a90d9; }

    .subgraph-info {
      font-size: 10px;
      color: #7788aa;
      margin-top: 6px;
    }

    /* Highlighting state — applied when subgraph traversal is active */
    .node-circle.sg-dimmed { opacity: 0.08 !important; }
    .node-label.sg-dimmed { opacity: 0.05 !important; }
    .link.sg-dimmed { opacity: 0.04 !important; }
    .node-circle.sg-highlight { filter: brightness(1.3); stroke-width: 2.5; }
    .status-ring.sg-dimmed { opacity: 0.05 !important; }

    .superseded-badge {
      background: #3a2000;
      color: #f0ad4e;
      border: 1px solid #f0ad4e44;
      border-radius: 3px;
      padding: 2px 7px;
      font-size: 10px;
      display: inline-block;
      margin-bottom: 8px;
    }

    .retracted-badge {
      background: #3a0000;
      color: #e05252;
      border: 1px solid #e0525244;
      border-radius: 3px;
      padding: 2px 7px;
      font-size: 10px;
      display: inline-block;
      margin-bottom: 8px;
    }

    .tainted-badge {
      background: #3a2200;
      color: #ff9800;
      border: 1px solid #ff980044;
      border-radius: 3px;
      padding: 2px 7px;
      font-size: 10px;
      display: inline-block;
      margin-bottom: 8px;
    }

    .stale-badge {
      background: #2a2a00;
      color: #d4c742;
      border: 1px solid #d4c74244;
      border-radius: 3px;
      padding: 2px 7px;
      font-size: 10px;
      display: inline-block;
      margin-bottom: 8px;
    }

    /* SVG node / edge styles (set via D3) */
    .node-circle { cursor: pointer; stroke-width: 1.5; }
    .node-circle.superseded { opacity: 0.3; stroke-dasharray: 4 2; }
    .node-circle.retracted { opacity: 0.2; stroke-dasharray: 2 2; stroke: #e05252 !important; }
    .node-circle.tainted { stroke: #ff9800 !important; stroke-width: 2; stroke-dasharray: 3 2; }
    .node-circle.selected { stroke-width: 3; filter: brightness(1.3); }
    .node-label { font-size: 10px; fill: #ccd; pointer-events: none; }
    .status-ring { pointer-events: none; }
    .link { stroke-opacity: 0.65; }
    .link.supersedes { stroke-dasharray: 5 3; }
    .link.resolves { stroke-dasharray: 6 3; }
    .link.retracts { stroke-dasharray: 3 3; }

    /* ── Top nav ────────────────────────────────────────────────────── */
    .nav { margin-bottom: 0; padding: 8px 16px; background: #0f3460;
           border-bottom: 1px solid #1a4a8a; flex-shrink: 0; }
    .nav a { color: #00d4ff; text-decoration: none; margin-right: 16px; font-size: 0.9em; }
    .nav a:hover { text-decoration: underline; }

    /* ── Content row (sidebar + graph + detail) ─────────────────────── */
    #content-row { display: flex; flex: 1; overflow: hidden; }
  </style>
</head>
<body>

<!-- ── Top nav ──────────────────────────────────────────────────────── -->
<div class="nav">
  <a href="/" style="font-weight:600;">Graph View</a>
  <a href="/health">Health Dashboard</a>
  <a href="/stats">Stats</a>
</div>

<!-- ── Content row ──────────────────────────────────────────────────── -->
<div id="content-row">

<!-- ── Sidebar ──────────────────────────────────────────────────────── -->
<div id="sidebar">
  <header>
    ENGRAM Visualizer
    <small id="db-path-label"></small>
  </header>

  <div class="section" id="agent-section" style="display:none;">
    <div class="section-title">Agent</div>
    <select id="agent-select" style="width:100%;padding:5px 8px;background:#0d1b36;border:1px solid #1a4a8a;border-radius:4px;color:#e0e0e0;font-size:12px;outline:none;"></select>
  </div>

  <div class="section">
    <div id="status-line">Nodes: <span id="stat-nodes">—</span> &nbsp; Edges: <span id="stat-edges">—</span></div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <div class="btn" id="refresh-btn">↻ Refresh</div>
      <label class="toggle" style="margin:0">
        <input type="checkbox" id="auto-refresh-cb" checked />
        Auto (10s)
      </label>
    </div>
    <div id="last-refresh" style="font-size:10px;color:#556;margin-top:6px;"></div>
  </div>

  <div class="section">
    <div class="section-title">Find node</div>
    <div id="search-box">
      <input type="text" id="search-input" placeholder="e.g. dv_0130" spellcheck="false" />
      <div class="btn" id="search-btn">Go</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Options</div>
    <label class="toggle">
      <input type="checkbox" id="show-superseded-cb" />
      Show superseded / retracted
    </label>
    <label class="toggle">
      <input type="checkbox" id="show-labels-cb" checked />
      Show labels
    </label>
    <label class="toggle">
      <input type="checkbox" id="show-tier-opacity-cb" checked />
      Tier opacity
    </label>
  </div>

  <div class="section">
    <div class="section-title">Node types</div>
    <div id="type-filters"></div>
  </div>

  <div class="section">
    <div class="section-title">Edge relations</div>
    <div id="edge-legend"></div>
  </div>

  <div class="section" id="stats-section">
    <div class="section-title">Graph stats</div>
    <div id="stats-detail"></div>
  </div>
</div>

<!-- ── Graph ────────────────────────────────────────────────────────── -->
<div id="main">
  <div id="empty-msg">No data yet — start the MCP server and add some nodes.</div>
  <svg id="graph-svg"></svg>
</div>

<!-- ── Detail panel ──────────────────────────────────────────────────── -->
<div id="detail-panel">
  <div id="detail-header">
    <div>
      <div class="node-id" id="detail-id"></div>
      <span class="node-type-badge" id="detail-type-badge"></span>
    </div>
    <div id="close-detail">✕</div>
  </div>
  <div id="detail-body"></div>
</div>

</div><!-- /#content-row -->

<!-- ── Tooltip ───────────────────────────────────────────────────────── -->
<div id="tooltip"></div>

<script>
// ═══════════════════════════════════════════════════════════════════
// Constants
// ═══════════════════════════════════════════════════════════════════

const NODE_TYPES = {
  evidence:                { color: '#4a90d9', label: 'Evidence' },
  axiom:                   { color: '#ff6f61', label: 'Axiom' },
  definition:              { color: '#26c6da', label: 'Definition' },
  observation_factual:     { color: '#5cb85c', label: 'Obs. Factual' },
  observation_predictive:  { color: '#f0ad4e', label: 'Obs. Predictive' },
  prediction:              { color: '#ffc107', label: 'Prediction' },
  conjecture:              { color: '#ff9800', label: 'Conjecture' },
  derivation:              { color: '#b05ce6', label: 'Derivation' },
  theory:                  { color: '#7c4dff', label: 'Theory' },
  contradiction:           { color: '#e05252', label: 'Contradiction' },
  question:                { color: '#17a2b8', label: 'Question' },
  goal:                    { color: '#00e676', label: 'Goal' },
  goal_tension:            { color: '#76ff03', label: 'Goal Tension' },
  feeling_report:          { color: '#f48fb1', label: 'Feeling' },
  person:                  { color: '#ffd54f', label: 'Person' },
  lesson:                  { color: '#80deea', label: 'Lesson' },
  task:                    { color: '#a1887f', label: 'Task' },
};

const EDGE_COLORS = {
  cites:         '#7799bb',
  supports:      '#5cb85c',
  contradicts:   '#e05252',
  resolves:      '#4a90d9',
  derives_from:  '#b05ce6',
  supersedes:    '#f0ad4e',
  retracts:      '#cc3333',
};

// ═══════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════

let rawData = { nodes: [], edges: [] };
let activeTypes = new Set(Object.keys(NODE_TYPES));
let showSuperseded = false;
let showLabels = true;
let showTierOpacity = true;
let selectedNodeId = null;
let autoRefreshInterval = null;
let highlightedNodeIds = new Set();
let highlightDepth = 2;

// Memory tier thresholds. These mirror the agent's $ENGRAM_HOME/config.json
// values (memory.tier1_max_nodes / tier2_max_nodes) — fetched at page load
// from /api/meta so the viz reflects the ACTUAL config, not the historical
// default that diverged silently (Lei caught this 2026-05-17: viz was
// showing many nodes as tier 3 because TIER2_MAX=1000 was hardcoded while
// production config had tier2_max_nodes=4000). The defaults below are used
// only as a fallback until /api/meta resolves; the call site near line 1705
// overrides these from the meta response.
let TIER1_MAX = 200;
let TIER2_MAX = 1000;

// ═══════════════════════════════════════════════════════════════════
// D3 setup
// ═══════════════════════════════════════════════════════════════════

const svg = d3.select('#graph-svg');
const container = svg.append('g').attr('class', 'zoom-container');

// Arrowhead markers (one per relation)
const defs = svg.append('defs');
Object.entries(EDGE_COLORS).forEach(([rel, color]) => {
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
    .attr('fill', color)
    .attr('opacity', 0.8);
});

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

// Compute memory tiers for current nodes based on importance_score ranking
function computeTiers(nodes) {
  const current = nodes.filter(n => n.is_current).slice();
  current.sort((a, b) => (b.importance_score || 0) - (a.importance_score || 0));
  const tierMap = {};
  current.forEach((n, i) => {
    if (i < TIER1_MAX) tierMap[n.id] = 1;
    else if (i < TIER2_MAX) tierMap[n.id] = 2;
    else tierMap[n.id] = 3;
  });
  // Non-current nodes get tier 3
  nodes.filter(n => !n.is_current).forEach(n => { tierMap[n.id] = 3; });
  return tierMap;
}

function nodeOpacity(d, tierMap) {
  if (!showTierOpacity) return 1.0;
  const tier = tierMap[d.id] || 3;
  if (tier === 1) return 1.0;
  if (tier === 2) return 0.55;
  return 0.2;
}

function nodeColor(d) {
  return (NODE_TYPES[d.type] || { color: '#888' }).color;
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
  if (!d.metadata) return false;
  try {
    const meta = typeof d.metadata === 'string' ? JSON.parse(d.metadata) : d.metadata;
    return meta && Array.isArray(meta.tainted_by) && meta.tainted_by.length > 0;
  } catch(e) { return false; }
}

function isStale(d) {
  if (!d.metadata) return false;
  try {
    const meta = typeof d.metadata === 'string' ? JSON.parse(d.metadata) : d.metadata;
    return meta && Array.isArray(meta.stale_by) && meta.stale_by.length > 0;
  } catch(e) { return false; }
}

function nodeClasses(d) {
  let cls = 'node-circle';
  if (isRetracted(d)) cls += ' retracted';
  else if (!d.is_current) cls += ' superseded';
  if (isTainted(d)) cls += ' tainted';
  if (selectedNodeId === d.id) cls += ' selected';
  return cls;
}

// ═══════════════════════════════════════════════════════════════════
// Data filtering
// ═══════════════════════════════════════════════════════════════════

function filteredData() {
  let nodes = rawData.nodes.filter(n =>
    activeTypes.has(n.type) && (showSuperseded || n.is_current)
  );
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
        .attr('stroke', d => EDGE_COLORS[d.relation] || '#666')
        .attr('stroke-width', 1.5)
        .attr('marker-end', d => `url(#arrow-${d.relation})`)
        .attr('opacity', 0)
        .call(sel => sel.transition().duration(400).attr('opacity', 1)),
      update => update
        .attr('stroke', d => EDGE_COLORS[d.relation] || '#666')
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

  let lines = [`<b>${d.id}</b> <span style="color:#7788aa">${NODE_TYPES[d.type]?.label || d.type}</span>`];
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
  badge.textContent = NODE_TYPES[d.type]?.label || d.type;
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
    try {
      const meta = typeof d.metadata === 'string' ? JSON.parse(d.metadata) : d.metadata;
      const by = meta.tainted_by || [];
      body.insertAdjacentHTML('beforeend',
        `<div class="tainted-badge">⚠ Tainted by: ${by.join(', ')}</div>`);
    } catch(e) {
      body.insertAdjacentHTML('beforeend', '<div class="tainted-badge">⚠ Tainted</div>');
    }
  }
  if (isStale(d)) {
    try {
      const meta = typeof d.metadata === 'string' ? JSON.parse(d.metadata) : d.metadata;
      const by = meta.stale_by || [];
      const repl = meta.stale_replacement || '?';
      body.insertAdjacentHTML('beforeend',
        `<div class="stale-badge">⚠ Stale — premise ${by.join(', ')} superseded by ${repl}</div>`);
    } catch(e) {
      body.insertAdjacentHTML('beforeend', '<div class="stale-badge">⚠ Stale</div>');
    }
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
        const color = EDGE_COLORS[e.relation] || '#888';
        const otherNode = rawData.nodes.find(n => n.id === otherId);
        const otherType = otherNode ? (NODE_TYPES[otherNode.type]?.label || otherNode.type) : '';
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
    supports:     ['supports', 'supported by'],
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
  // Node type filters
  const tf = document.getElementById('type-filters');
  tf.innerHTML = '';
  Object.entries(NODE_TYPES).forEach(([key, { color, label }]) => {
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

  // Edge legend
  const el = document.getElementById('edge-legend');
  el.innerHTML = '';
  Object.entries(EDGE_COLORS).forEach(([rel, color]) => {
    const isDashed = rel === 'supersedes' || rel === 'retracts';
    el.insertAdjacentHTML('beforeend',
      `<div class="edge-legend-row">
        <div class="edge-line" style="background:${color};${isDashed ? 'background:repeating-linear-gradient(90deg,'+color+' 0,'+color+' 5px,transparent 5px,transparent 8px)' : ''}"></div>
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

async function loadAgents() {
  try {
    const res = await fetch('/api/agents');
    const data = await res.json();
    const agents = data.agents || [];
    const defaultAgent = data.default || (agents[0] && agents[0].name);
    if (agents.length <= 1) {
      // Single-agent mode — keep dropdown hidden, no URL param needed.
      return;
    }
    const sel = document.getElementById('agent-select');
    sel.innerHTML = '';
    agents.forEach(a => {
      const opt = document.createElement('option');
      opt.value = a.name;
      const status = a.available ? `(${a.current_node_count})` : '(unavailable)';
      opt.textContent = `${a.label} ${status}`;
      if (!a.available && a.error) opt.title = a.error;
      sel.appendChild(opt);
    });
    let current = currentAgent();
    if (!current || !agents.find(a => a.name === current)) {
      current = defaultAgent;
      setCurrentAgent(current);
    }
    sel.value = current;
    document.getElementById('agent-section').style.display = '';
    sel.onchange = () => {
      setCurrentAgent(sel.value);
      fetchGraph();
      // Refresh the db-path-label AND re-sync memory tier thresholds
      // for the newly-selected agent. Different agents can have different
      // config.json values; without this re-sync, switching agents in
      // multi-agent mode would leave the prior agent's TIER1_MAX/TIER2_MAX
      // active and compute tiers wrong. Round-4 fairy caught this gap.
      fetch(withAgent('/api/meta')).then(r => r.json()).then(m => {
        document.getElementById('db-path-label').textContent = m.db_path || '';
        if (m.memory_config) {
          if (typeof m.memory_config.tier1_max_nodes === 'number') {
            TIER1_MAX = m.memory_config.tier1_max_nodes;
          }
          if (typeof m.memory_config.tier2_max_nodes === 'number') {
            TIER2_MAX = m.memory_config.tier2_max_nodes;
          }
        }
      }).catch(() => {});
    };
  } catch (err) {
    console.warn('Failed to load agents:', err);
  }
}

// ═══════════════════════════════════════════════════════════════════
// Data fetch
// ═══════════════════════════════════════════════════════════════════

async function fetchGraph() {
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

    // Update stats bar
    const current = data.nodes.filter(n => n.is_current).length;
    document.getElementById('stat-nodes').textContent = `${current} (${data.nodes.length} total)`;
    document.getElementById('stat-edges').textContent = data.edges.length;

    // Type counts for stats panel
    const byCurrent = {};
    data.nodes.filter(n => n.is_current).forEach(n => {
      byCurrent[n.type] = (byCurrent[n.type] || 0) + 1;
    });
    const sd = document.getElementById('stats-detail');
    sd.innerHTML = '';
    Object.entries(NODE_TYPES).forEach(([key, { label }]) => {
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

document.getElementById('refresh-btn').onclick = fetchGraph;

document.getElementById('auto-refresh-cb').onchange = function() {
  if (this.checked) {
    autoRefreshInterval = setInterval(fetchGraph, 10000);
  } else {
    clearInterval(autoRefreshInterval);
    autoRefreshInterval = null;
  }
};

// Set db path label (agent-aware) AND sync memory tier thresholds from
// the agent's config (Lei 2026-05-17: viz tier classification was wrong
// because tier sizes were hardcoded JS constants instead of read from
// $ENGRAM_HOME/config.json).
fetch(withAgent('/api/meta')).then(r => r.json()).then(m => {
  document.getElementById('db-path-label').textContent = m.db_path || '';
  if (m.memory_config) {
    if (typeof m.memory_config.tier1_max_nodes === 'number') {
      TIER1_MAX = m.memory_config.tier1_max_nodes;
    }
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

buildSidebar();
updateSimCenter();
// Resolve the agent dropdown first (sets URL param if needed) so the very
// first /api/graph fetch is already scoped to the right agent.
loadAgents().then(() => {
  fetchGraph();
  autoRefreshInterval = setInterval(fetchGraph, 10000);
});
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Agent registry helpers
# ---------------------------------------------------------------------------

def _load_config(path: str) -> tuple[dict, str]:
    """Parse a multi-agent config JSON. Returns (agents_dict, default_name).

    Schema (see active-work/viz-server-multi-agent-2026-05-04.md):
      {"agents": [{"name": str, "db": str, "label": str (optional)}, ...],
       "default": str (optional — first agent if omitted)}
    """
    with open(path) as f:
        cfg = json.load(f)
    raw_agents = cfg.get("agents", [])
    if not raw_agents:
        raise ValueError(f"{path}: 'agents' list is empty or missing")
    agents = {}
    for a in raw_agents:
        if "name" not in a or "db" not in a:
            raise ValueError(f"{path}: agent missing required keys 'name' or 'db': {a!r}")
        name = a["name"]
        agents[name] = {
            "name": name,
            "label": a.get("label", name),
            "db": os.path.expanduser(a["db"]),
        }
    default = cfg.get("default") or next(iter(agents))
    if default not in agents:
        raise ValueError(f"{path}: default '{default}' is not in the agents list")
    return agents, default


def _resolve_agent(query_string: str) -> tuple[str, str]:
    """Pick an agent given the query string. Returns (name, db_path).

    Falls back to DEFAULT_AGENT when ?agent is omitted. Returns
    (requested_name, "") when ?agent names something unregistered, so the
    handler can surface a clear error instead of silently swapping in the
    default.
    """
    qs = parse_qs(query_string or "")
    requested = qs.get("agent", [None])[0]
    name = requested or DEFAULT_AGENT
    if name in AGENTS:
        return name, AGENTS[name]["db"]
    return name or "", ""


def get_agents_meta() -> list:
    """Per-agent status snapshot for /api/agents (drives the UI dropdown)."""
    out = []
    for name, info in AGENTS.items():
        entry = {"name": name, "label": info.get("label", name), "db": info["db"]}
        if not os.path.exists(info["db"]):
            entry["available"] = False
            entry["error"] = "Database file not found"
            entry["current_node_count"] = 0
        else:
            try:
                conn = sqlite3.connect(f"file:{info['db']}?mode=ro", uri=True)
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE is_current = 1"
                ).fetchone()[0]
                conn.close()
                entry["available"] = True
                entry["current_node_count"] = cnt
            except sqlite3.OperationalError as e:
                entry["available"] = False
                entry["error"] = str(e)
                entry["current_node_count"] = 0
            except Exception as e:
                entry["available"] = False
                entry["error"] = str(e)
                entry["current_node_count"] = 0
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def get_graph(db_path: str) -> dict:
    if not os.path.exists(db_path):
        return {"nodes": [], "edges": [], "error": f"Database not found: {db_path}"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # All nodes — dynamically select available columns to handle
        # both old (pre-memory) and new schemas
        col_names = {r[1] for r in cur.execute("PRAGMA table_info(nodes)").fetchall()}
        base_cols = [
            "id", "type", "claim", "created_at",
            "source_url", "source_title", "source_domain", "source_accessed", "content_snippet",
            "evidence_id", "quoted_text", "interpretation", "quote_type",
            "predicted_event", "resolution_timeframe", "status", "resolved_by",
            "logical_chain", "confidence", "confidence_history", "is_current",
            "supersedes", "superseded_by", "metadata",
        ]
        # Memory-management and semantic columns (added in later schema versions)
        extra_cols = ["importance_base", "importance_score", "recall_turn",
                      "recall_count", "memory_status", "source_class", "utility_score",
                      # feeling_report-specific columns
                      "reported_state", "trigger_text", "categorical_tag",
                      "intensity_hint", "nudge_source"]
        select_cols = base_cols + [c for c in extra_cols if c in col_names]
        cur.execute(f"SELECT {','.join(select_cols)} FROM nodes ORDER BY id")
        nodes = []
        for row in cur.fetchall():
            n = dict(row)
            # Ensure booleans are plain Python ints for JSON
            n["is_current"] = int(n["is_current"]) if n["is_current"] is not None else 1
            nodes.append(n)

        # All edges
        cur.execute("""
            SELECT source_id, target_id, relation, created_at
            FROM edges
            ORDER BY id
        """)
        edges = [
            {"source_id": r["source_id"], "target_id": r["target_id"],
             "relation": r["relation"], "created_at": r["created_at"]}
            for r in cur.fetchall()
        ]

        conn.close()
        return {"nodes": nodes, "edges": edges}

    except sqlite3.OperationalError as e:
        # Table may not exist yet
        if "no such table" in str(e):
            return {"nodes": [], "edges": [], "error": "Schema not initialised yet — start the MCP server first."}
        return {"nodes": [], "edges": [], "error": str(e)}
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)}


def get_health_data(db_path: str) -> dict:
    """Compute live health metrics + trend from diagnostic_history."""
    if not os.path.exists(db_path):
        return {"error": f"Database not found: {db_path}"}
    try:
        # Read-only via mode=ro; multi-agent peer-DB safety is enforced by
        # OS file permissions (owner UID writes; group=agents reads), not by
        # this URI flag. The previous `immutable=1` was dropped (#184) because
        # it caused the reader to ignore -wal entirely → silent staleness.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        data: dict = {}

        # ── Live metrics ─────────────────────────────────────────────
        # Node counts by type
        type_rows = conn.execute(
            """SELECT type, COUNT(*) as total, SUM(is_current) as current_count
               FROM nodes GROUP BY type"""
        ).fetchall()
        type_counts = {}
        total_nodes = 0
        total_current = 0
        for r in type_rows:
            type_counts[r["type"]] = {"total": r["total"], "current": r["current_count"]}
            total_nodes += r["total"]
            total_current += r["current_count"]
        data["node_counts"] = type_counts
        data["total_nodes"] = total_nodes
        data["total_current"] = total_current

        # Edge counts
        edge_rows = conn.execute(
            "SELECT relation, COUNT(*) as c FROM edges GROUP BY relation"
        ).fetchall()
        data["edge_counts"] = {r["relation"]: r["c"] for r in edge_rows}
        data["total_edges"] = sum(r["c"] for r in edge_rows)

        # Confidence distribution
        conf_buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0,
                        "0.6-0.8": 0, "0.8-1.0": 0}
        for cr in conn.execute(
            "SELECT confidence FROM nodes WHERE is_current = 1 AND confidence IS NOT NULL"
        ).fetchall():
            c = cr["confidence"]
            if c < 0.2: conf_buckets["0.0-0.2"] += 1
            elif c < 0.4: conf_buckets["0.2-0.4"] += 1
            elif c < 0.6: conf_buckets["0.4-0.6"] += 1
            elif c < 0.8: conf_buckets["0.6-0.8"] += 1
            else: conf_buckets["0.8-1.0"] += 1
        data["confidence_distribution"] = conf_buckets

        # Issue counts
        data["tainted"] = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND metadata LIKE '%\"tainted_by\"%'"
        ).fetchone()["c"]
        data["stale"] = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND metadata LIKE '%\"stale_by\"%'"
        ).fetchone()["c"]
        data["retracted"] = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE status = 'retracted'"
        ).fetchone()["c"]
        data["orphans"] = conn.execute("""
            SELECT COUNT(*) as c FROM nodes n WHERE n.is_current = 1
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.source_id = n.id)
              AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.target_id = n.id)
        """).fetchone()["c"]

        # Memory tiers (read config for turn)
        config_path = Path(db_path).parent / "config.json"
        current_turn = 0
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text())
                current_turn = int(cfg.get("memory", {}).get("current_turn", 0))
            except Exception:
                pass
        data["current_turn"] = current_turn

        # Embedding coverage
        with_emb = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND embedding IS NOT NULL"
        ).fetchone()["c"]
        data["embedding_pct"] = round(with_emb / total_current * 100, 1) if total_current else 0

        # Feeling reports
        data["feeling_reports"] = conn.execute(
            "SELECT COUNT(*) as c FROM nodes WHERE type = 'feeling_report' AND is_current = 1"
        ).fetchone()["c"]

        # Resolution rates
        for ntype, label in [("question", "questions"), ("conjecture", "conjectures")]:
            total = conn.execute("SELECT COUNT(*) as c FROM nodes WHERE type = ?", (ntype,)).fetchone()["c"]
            resolved = conn.execute(
                "SELECT COUNT(*) as c FROM nodes WHERE type = ? AND status NOT IN ('open', 'active')",
                (ntype,)
            ).fetchone()["c"]
            data[f"{label}_total"] = total
            data[f"{label}_resolved"] = resolved

        # ── Edit history (last 7 days, grouped by day) ───────────────
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        try:
            daily_edits = conn.execute("""
                SELECT SUBSTR(timestamp, 1, 10) as day, action, COUNT(*) as c
                FROM edit_history WHERE timestamp > ?
                GROUP BY day, action ORDER BY day
            """, (week_ago,)).fetchall()
            edit_by_day: dict = {}
            for r in daily_edits:
                d = r["day"]
                if d not in edit_by_day:
                    edit_by_day[d] = {}
                edit_by_day[d][r["action"]] = r["c"]
            data["edit_by_day"] = edit_by_day
        except sqlite3.OperationalError:
            data["edit_by_day"] = {}

        # ── Diagnostic history trend ─────────────────────────────────
        try:
            snapshots = conn.execute(
                "SELECT turn, timestamp, metrics FROM diagnostic_history ORDER BY id DESC LIMIT 20"
            ).fetchall()
            trend = []
            for s in reversed(list(snapshots)):
                m = json.loads(s["metrics"])
                trend.append({
                    "turn": s["turn"],
                    "timestamp": s["timestamp"][:16],
                    "health_score": m.get("health_score", 0),
                    "total_nodes": m.get("structure", {}).get("total_current", 0),
                })
            data["health_trend"] = trend
        except sqlite3.OperationalError:
            data["health_trend"] = []

        # ── Compute live health score ────────────────────────────────
        score = 100.0
        dag_v = conn.execute("""
            SELECT COUNT(*) as c FROM edges e
            JOIN nodes s ON e.source_id = s.id JOIN nodes t ON e.target_id = t.id
            WHERE s.created_at < t.created_at AND e.relation NOT IN ('contradicts')
        """).fetchone()["c"]
        score -= min(dag_v * 5, 20)
        score -= min(data["tainted"] * 3, 15)
        score -= min(data["stale"] * 2, 10)
        score -= min(data["orphans"], 10)
        score -= min((100 - data["embedding_pct"]) * 0.1, 10)
        if total_nodes > 0 and data["retracted"] / total_nodes > 0.1:
            score -= min((data["retracted"] / total_nodes - 0.1) * 100, 15)
        data["health_score"] = round(max(score, 0), 1)

        conn.close()
        return data

    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {"error": "Schema not initialised yet"}
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Health dashboard HTML
# ---------------------------------------------------------------------------

HEALTH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ENGRAM Health Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
           background: #1a1a2e; color: #e0e0e0; padding: 24px; }
    h1 { color: #00d4ff; font-size: 1.5em; margin-bottom: 16px; }
    h2 { color: #7ecfff; font-size: 1.1em; margin: 16px 0 8px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
    .card { background: #16213e; border-radius: 8px; padding: 16px; border: 1px solid #0f3460; }
    .card h3 { color: #00d4ff; font-size: 0.95em; margin-bottom: 10px; }
    .metric { display: flex; justify-content: space-between; padding: 4px 0;
              border-bottom: 1px solid #0f3460; font-size: 0.85em; }
    .metric:last-child { border-bottom: none; }
    .metric .label { color: #aaa; }
    .metric .value { color: #fff; font-weight: 600; }
    .metric .value.warn { color: #ff9800; }
    .metric .value.danger { color: #f44336; }
    .metric .value.good { color: #4caf50; }
    .gauge { width: 120px; height: 120px; margin: 0 auto 12px; position: relative; }
    .gauge canvas { width: 120px; height: 120px; }
    .gauge .score { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
                    font-size: 1.8em; font-weight: 700; }
    .bar-chart { display: flex; align-items: flex-end; gap: 4px; height: 80px; margin-top: 8px; }
    .bar-chart .bar { flex: 1; background: #00d4ff; border-radius: 3px 3px 0 0;
                      min-width: 20px; position: relative; transition: height 0.3s; }
    .bar-chart .bar .bar-label { position: absolute; bottom: -18px; left: 50%;
                                  transform: translateX(-50%); font-size: 0.65em; color: #888; white-space: nowrap; }
    .bar-chart .bar .bar-value { position: absolute; top: -16px; left: 50%;
                                  transform: translateX(-50%); font-size: 0.7em; color: #ccc; }
    .trend { margin-top: 8px; }
    .trend svg { width: 100%; height: 60px; }
    .trend path { fill: none; stroke: #00d4ff; stroke-width: 2; }
    .trend circle { fill: #00d4ff; }
    .nav { margin-bottom: 16px; }
    .nav a { color: #00d4ff; text-decoration: none; margin-right: 16px; font-size: 0.9em; }
    .nav a:hover { text-decoration: underline; }
    .timeline { margin-top: 8px; }
    .timeline .day { display: flex; align-items: center; gap: 8px; padding: 3px 0;
                     font-size: 0.8em; border-bottom: 1px solid #0f3460; }
    .timeline .day .date { color: #888; min-width: 80px; }
    .timeline .day .counts { display: flex; gap: 6px; flex-wrap: wrap; }
    .timeline .day .counts span { padding: 1px 6px; border-radius: 3px; font-size: 0.75em; }
    .tag-created { background: #1b5e20; color: #a5d6a7; }
    .tag-superseded { background: #4a148c; color: #ce93d8; }
    .tag-retracted { background: #b71c1c; color: #ef9a9a; }
    .tag-resolved { background: #0d47a1; color: #90caf9; }
    .tag-other { background: #37474f; color: #b0bec5; }
    #loading { text-align: center; padding: 40px; color: #888; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/">Graph View</a>
    <a href="/health" style="font-weight:600;">Health Dashboard</a>
    <a href="/stats">Stats</a>
    <span id="agent-switcher" style="display:none;margin-left:auto;float:right;">
      <span style="color:#7ecfff;font-size:0.85em;margin-right:6px;">Agent:</span>
      <select id="agent-select-health" style="padding:4px 8px;background:#0d1b36;border:1px solid #1a4a8a;border-radius:4px;color:#e0e0e0;font-size:0.85em;outline:none;"></select>
    </span>
  </div>
  <h1>ENGRAM Health Dashboard</h1>
  <div id="loading">Loading health data...</div>
  <div id="dashboard" style="display:none;">
    <div class="grid" id="cards"></div>
  </div>

  <script>
  function _currentAgent() {
    return new URL(window.location.href).searchParams.get('agent');
  }
  function _setCurrentAgent(name) {
    const url = new URL(window.location.href);
    if (name) url.searchParams.set('agent', name);
    else url.searchParams.delete('agent');
    window.history.replaceState(null, '', url);
  }
  function _withAgent(path) {
    const a = _currentAgent();
    if (!a) return path;
    return path + (path.includes('?') ? '&' : '?') + 'agent=' + encodeURIComponent(a);
  }

  function loadHealth() {
    document.getElementById('loading').style.display = '';
    document.getElementById('dashboard').style.display = 'none';
    document.getElementById('cards').innerHTML = '';
    fetch(_withAgent('/api/health'))
      .then(r => r.json())
      .then(data => {
        document.getElementById('loading').style.display = 'none';
        document.getElementById('dashboard').style.display = 'block';
        if (data.error) {
          document.getElementById('cards').innerHTML = '<div class="card"><p>' + data.error + '</p></div>';
          return;
        }
        render(data);
      })
      .catch(e => {
        document.getElementById('loading').textContent = 'Error: ' + e.message;
      });
  }

  // Wire agent dropdown if multi-agent.
  fetch('/api/agents').then(r => r.json()).then(data => {
    const agents = data.agents || [];
    const defaultAgent = data.default || (agents[0] && agents[0].name);
    if (agents.length <= 1) {
      loadHealth();
      return;
    }
    const sel = document.getElementById('agent-select-health');
    agents.forEach(a => {
      const opt = document.createElement('option');
      opt.value = a.name;
      const status = a.available ? `(${a.current_node_count})` : '(unavailable)';
      opt.textContent = `${a.label} ${status}`;
      if (!a.available && a.error) opt.title = a.error;
      sel.appendChild(opt);
    });
    let current = _currentAgent();
    if (!current || !agents.find(a => a.name === current)) {
      current = defaultAgent;
      _setCurrentAgent(current);
    }
    sel.value = current;
    document.getElementById('agent-switcher').style.display = '';
    sel.onchange = () => { _setCurrentAgent(sel.value); loadHealth(); };
    loadHealth();
  }).catch(() => loadHealth());

  function render(d) {
    const cards = document.getElementById('cards');
    let html = '';

    // Health score gauge
    html += `<div class="card" style="text-align:center">
      <h3>Health Score</h3>
      <div class="gauge"><canvas id="gauge" width="120" height="120"></canvas>
        <div class="score" style="color:${scoreColor(d.health_score)}">${d.health_score}</div>
      </div>
      <div style="font-size:0.8em; color:#888">Turn ${d.current_turn}</div>
    </div>`;

    // Overview
    html += `<div class="card"><h3>Overview</h3>
      ${metric('Current nodes', d.total_current)}
      ${metric('Total (incl. superseded)', d.total_nodes)}
      ${metric('Edges', d.total_edges)}
      ${metric('Embedding coverage', d.embedding_pct + '%', d.embedding_pct < 80 ? 'warn' : 'good')}
      ${metric('Feeling reports', d.feeling_reports)}
    </div>`;

    // Issues
    const issues = d.tainted + d.stale + d.retracted + d.orphans;
    html += `<div class="card"><h3>Issues</h3>
      ${metric('Tainted nodes', d.tainted, d.tainted > 0 ? 'danger' : 'good')}
      ${metric('Stale nodes', d.stale, d.stale > 0 ? 'warn' : 'good')}
      ${metric('Retracted nodes', d.retracted, d.retracted > 10 ? 'warn' : '')}
      ${metric('Orphan nodes', d.orphans, d.orphans > 5 ? 'warn' : '')}
      ${metric('Total issues', issues, issues > 10 ? 'warn' : 'good')}
    </div>`;

    // Resolution
    html += `<div class="card"><h3>Resolution Rates</h3>
      ${metric('Questions', d.questions_resolved + '/' + d.questions_total + ' (' + pct(d.questions_resolved, d.questions_total) + ')')}
      ${metric('Conjectures', d.conjectures_resolved + '/' + d.conjectures_total + ' (' + pct(d.conjectures_resolved, d.conjectures_total) + ')')}
    </div>`;

    // Confidence distribution
    const confDist = d.confidence_distribution || {};
    const confMax = Math.max(...Object.values(confDist), 1);
    html += `<div class="card"><h3>Confidence Distribution</h3>
      <div class="bar-chart">
        ${Object.entries(confDist).map(([k,v]) =>
          `<div class="bar" style="height:${Math.max(v/confMax*80,2)}px; background:${confBarColor(k)}">
            <span class="bar-value">${v}</span><span class="bar-label">${k}</span></div>`
        ).join('')}
      </div>
    </div>`;

    // Node types
    const nc = d.node_counts || {};
    html += `<div class="card"><h3>Node Types</h3>
      ${Object.entries(nc).sort((a,b) => b[1].current - a[1].current).map(([t,c]) =>
        metric(t, c.current + (c.total > c.current ? ' (+' + (c.total - c.current) + ' old)' : ''))
      ).join('')}
    </div>`;

    // Edge types
    const ec = d.edge_counts || {};
    html += `<div class="card"><h3>Edge Types</h3>
      ${Object.entries(ec).sort((a,b) => b[1] - a[1]).map(([r,c]) => metric(r, c)).join('')}
    </div>`;

    // Edit timeline (last 7 days)
    const ebd = d.edit_by_day || {};
    const days = Object.keys(ebd).sort();
    if (days.length > 0) {
      html += `<div class="card" style="grid-column: span 2"><h3>Edit Activity (Last 7 Days)</h3>
        <div class="timeline">
          ${days.map(day => {
            const acts = ebd[day];
            return `<div class="day"><span class="date">${day}</span><div class="counts">
              ${Object.entries(acts).map(([a,c]) =>
                `<span class="tag-${tagClass(a)}">${a}: ${c}</span>`).join('')}
            </div></div>`;
          }).join('')}
        </div>
      </div>`;
    }

    // Health trend
    const trend = d.health_trend || [];
    if (trend.length > 1) {
      html += `<div class="card" style="grid-column: span 2"><h3>Health Score Trend</h3>
        <div class="trend"><svg id="trend-svg" viewBox="0 0 600 60"></svg></div>
      </div>`;
    }

    cards.innerHTML = html;

    // Draw gauge
    drawGauge(d.health_score);

    // Draw trend line
    if (trend.length > 1) drawTrend(trend);
  }

  function metric(label, value, cls) {
    return `<div class="metric"><span class="label">${label}</span><span class="value ${cls||''}">${value}</span></div>`;
  }
  function pct(a, b) { return b > 0 ? Math.round(a/b*100) + '%' : '0%'; }
  function scoreColor(s) { return s >= 80 ? '#4caf50' : s >= 60 ? '#ff9800' : '#f44336'; }
  function confBarColor(k) {
    const m = {'0.0-0.2':'#f44336','0.2-0.4':'#ff9800','0.4-0.6':'#ffeb3b','0.6-0.8':'#8bc34a','0.8-1.0':'#4caf50'};
    return m[k] || '#00d4ff';
  }
  function tagClass(a) {
    if (a === 'created') return 'created';
    if (a === 'superseded' || a === 'stale_flagged') return 'superseded';
    if (a === 'retracted' || a === 'tainted') return 'retracted';
    if (a === 'resolved' || a === 'reopened') return 'resolved';
    return 'other';
  }

  function drawGauge(score) {
    const c = document.getElementById('gauge');
    if (!c) return;
    const ctx = c.getContext('2d');
    const cx = 60, cy = 60, r = 50;
    // Background arc
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0.75 * Math.PI, 2.25 * Math.PI);
    ctx.strokeStyle = '#0f3460';
    ctx.lineWidth = 10;
    ctx.lineCap = 'round';
    ctx.stroke();
    // Score arc
    const angle = 0.75 * Math.PI + (score / 100) * 1.5 * Math.PI;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0.75 * Math.PI, angle);
    ctx.strokeStyle = scoreColor(score);
    ctx.lineWidth = 10;
    ctx.lineCap = 'round';
    ctx.stroke();
  }

  function drawTrend(trend) {
    const svg = document.getElementById('trend-svg');
    if (!svg || trend.length < 2) return;
    const w = 600, h = 60, pad = 10;
    const scores = trend.map(t => t.health_score);
    const mn = Math.min(...scores) - 5, mx = Math.max(...scores) + 5;
    const xScale = i => pad + i / (trend.length - 1) * (w - 2 * pad);
    const yScale = v => h - pad - (v - mn) / (mx - mn) * (h - 2 * pad);
    const pts = trend.map((t, i) => `${xScale(i)},${yScale(t.health_score)}`);
    svg.innerHTML = `<path d="M${pts.join(' L')}"/>` +
      trend.map((t, i) =>
        `<circle cx="${xScale(i)}" cy="${yScale(t.health_score)}" r="3"><title>Turn ${t.turn}: ${t.health_score}</title></circle>`
      ).join('');
  }
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Stats tab — agent-stats dashboard backed by ~/.engram/logs/index.db
# Per alpha #175 + ~/borges-desk/projects/engram-logging/PHASE4-STATS-TAB.md.
# Privacy: queries ONLY filter WHERE level = 1 (stats-only); L2 events are
# in the index but never surfaced through this dashboard.
# ---------------------------------------------------------------------------

def _resolve_agent_config(agent_name: str) -> dict:
    """Resolve agent's memory config from $ENGRAM_HOME/config.json.

    Returns the relevant viz-tunable fields with their config-or-default values.
    Defaults match server.py:_get_memory_config (lines 1108-1113). Returns
    defaults if config.json is missing/malformed — never raises.

    Used by /api/meta so the front-end tier-classification reads CONFIG values
    (e.g. tier2_max=4000 in production) rather than viz-side hardcoded defaults
    (tier2=1000 in the original JS, which mis-classified ~3000 nodes as tier 3).
    """
    defaults = {"tier1_max_nodes": 200, "tier2_max_nodes": 1000, "decay_base": 1.014}
    agent_meta = AGENTS.get(agent_name)
    if not agent_meta:
        return defaults
    db_path = agent_meta.get("db", "")
    if not db_path:
        return defaults
    engram_home = os.path.dirname(os.path.abspath(db_path))
    config_path = os.path.join(engram_home, "config.json")
    if not os.path.exists(config_path):
        return defaults
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        mem = config.get("memory", {})
        return {
            "tier1_max_nodes": int(mem.get("tier1_max_nodes", defaults["tier1_max_nodes"])),
            "tier2_max_nodes": int(mem.get("tier2_max_nodes", defaults["tier2_max_nodes"])),
            "decay_base": float(mem.get("decay_base", defaults["decay_base"])),
        }
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return defaults


def _resolve_logs_index(agent_name: str) -> str:
    """Resolve agent_name to its index.db path. Returns empty string if unknown."""
    agent_meta = AGENTS.get(agent_name)
    if not agent_meta:
        return ""
    # AGENTS metadata stores the agent's knowledge-db path under key "db"
    # (per viz_server.py line 33 module-level comment + lines 1758, 1778, 1786,
    # 2746). The logs index.db lives at $ENGRAM_HOME/logs/index.db where
    # $ENGRAM_HOME is the parent of the knowledge.db file.
    db_path = agent_meta.get("db", "")
    if not db_path:
        return ""
    engram_home = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(engram_home, "logs", "index.db")


def get_stats_data(index_db_path: str) -> dict:
    """Run the 5 stats panels against the index.db. Returns a dict the
    Stats HTML page renders. Empty-data states are explicit so the UI can
    render a 'No events yet' placeholder rather than a blank card.
    """
    if not index_db_path or not os.path.exists(index_db_path):
        return {"empty": True, "reason": "No index.db at expected location yet — has the indexer run? Try: python3 ~/.engram/engram_log_indexer.py"}
    try:
        # mode=ro defensively; engram_log_indexer.py uses DELETE journal mode
        # today so no WAL-staleness risk exists, but if it ever switches to WAL,
        # mode=ro is correct AND we still need to NOT carry immutable=1 (#184).
        conn = sqlite3.connect(f"file:{index_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"empty": True, "reason": f"Could not open index.db: {exc}"}
    try:
        # Confirm events table exists (indexer hasn't run if it doesn't)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        )
        if not cur.fetchone():
            return {"empty": True, "reason": "events table not found in index.db — has the indexer run?"}
        # Total L1 events (cardinality check)
        cur = conn.execute("SELECT COUNT(*) FROM events WHERE level = 1")
        total = cur.fetchone()[0]
        if total == 0:
            return {"empty": True, "reason": "Index has no L1 events yet — start using ENGRAM to generate metrics."}

        out: dict = {"total_events": total}

        # Panel 1: auto-surface health (last 7 days)
        cur = conn.execute(
            """SELECT COUNT(*) AS fires,
                      MIN(daemon_latency_ms) AS min_ms,
                      AVG(daemon_latency_ms) AS avg_ms,
                      MAX(daemon_latency_ms) AS max_ms,
                      SUM(fallback_to_fts) AS fallback_count
               FROM events
               WHERE event_type = 'engram.surface.fire'
                 AND level = 1
                 AND ts > datetime('now', '-7 days')"""
        )
        row = cur.fetchone()
        out["surface"] = {
            "fires_7d": row["fires"] or 0,
            "latency_min_ms": row["min_ms"],
            "latency_avg_ms": round(row["avg_ms"], 1) if row["avg_ms"] is not None else None,
            "latency_max_ms": row["max_ms"],
            "fallback_count_7d": row["fallback_count"] or 0,
            "fallback_pct": (100.0 * (row["fallback_count"] or 0) / row["fires"]) if row["fires"] else 0.0,
        }

        # Panel 2: hook fire profile (last 7 days, by hook_name)
        cur = conn.execute(
            """SELECT hook_name,
                      COUNT(*) AS fires,
                      AVG(hook_duration_ms) AS avg_duration_ms,
                      SUM(CASE WHEN hook_exit_code != 0 THEN 1 ELSE 0 END) AS error_count
               FROM events
               WHERE event_type = 'engram.hook.fire'
                 AND level = 1
                 AND ts > datetime('now', '-7 days')
                 AND hook_name IS NOT NULL
               GROUP BY hook_name
               ORDER BY fires DESC"""
        )
        out["hooks"] = [
            {
                "hook_name": r["hook_name"],
                "fires_7d": r["fires"],
                "avg_duration_ms": round(r["avg_duration_ms"], 1) if r["avg_duration_ms"] is not None else None,
                "error_count_7d": r["error_count"],
            }
            for r in cur.fetchall()
        ]

        # Panel 3: tool decision context (last 7 days)
        cur = conn.execute(
            """SELECT tool_name, result_status, COUNT(*) AS n
               FROM events
               WHERE event_type = 'engram.tool.engram_call'
                 AND level = 1
                 AND ts > datetime('now', '-7 days')
                 AND tool_name IS NOT NULL
               GROUP BY tool_name, result_status
               ORDER BY tool_name, n DESC"""
        )
        out["tools"] = [
            {"tool_name": r["tool_name"], "result_status": r["result_status"], "n": r["n"]}
            for r in cur.fetchall()
        ]

        # Panel 4: cohort-aligned (last 10 turns).
        # Both the outer query AND the subquery must filter WHERE level = 1
        # for the privacy invariant. Round-1 fairy caught the missing level
        # filter on the subquery (would shift the window anchor if a future
        # L2 event happened to carry a higher turn than L1 events).
        cur = conn.execute(
            """SELECT turn, COUNT(*) AS events,
                      SUM(CASE WHEN event_type = 'engram.surface.fire' THEN 1 ELSE 0 END) AS surface_fires,
                      SUM(CASE WHEN event_type = 'engram.tool.engram_call' THEN 1 ELSE 0 END) AS tool_calls,
                      SUM(CASE WHEN event_type = 'engram.hook.fire' THEN 1 ELSE 0 END) AS hook_fires
               FROM events
               WHERE level = 1
                 AND turn > (SELECT COALESCE(MAX(turn), 0) - 10 FROM events WHERE level = 1)
               GROUP BY turn
               ORDER BY turn DESC"""
        )
        out["cohort"] = [
            {
                "turn": r["turn"],
                "events": r["events"],
                "surface_fires": r["surface_fires"],
                "tool_calls": r["tool_calls"],
                "hook_fires": r["hook_fires"],
            }
            for r in cur.fetchall()
        ]

        # Panel 5: recent errors (last 24h, capped at 25)
        cur = conn.execute(
            """SELECT ts, event_type, tool_name, hook_name, result_status, hook_exit_code
               FROM events
               WHERE level = 1
                 AND ts > datetime('now', '-1 day')
                 AND (
                   (event_type = 'engram.tool.engram_call' AND result_status IN ('error', 'blocked_tainted', 'blocked_stale', 'rejected'))
                   OR (event_type = 'engram.hook.fire' AND hook_exit_code IS NOT NULL AND hook_exit_code != 0)
                 )
               ORDER BY ts DESC
               LIMIT 25"""
        )
        out["errors"] = [
            {
                "ts": r["ts"],
                "event_type": r["event_type"],
                "tool_name": r["tool_name"],
                "hook_name": r["hook_name"],
                "detail": r["result_status"] if r["event_type"] == "engram.tool.engram_call" else f"exit_code={r['hook_exit_code']}",
            }
            for r in cur.fetchall()
        ]

        return out
    finally:
        conn.close()


STATS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ENGRAM Stats Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
           background: #1a1a2e; color: #e0e0e0; padding: 24px; }
    h1 { color: #00d4ff; font-size: 1.5em; margin-bottom: 16px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; }
    .card { background: #16213e; border-radius: 8px; padding: 16px; border: 1px solid #0f3460; }
    .card h3 { color: #00d4ff; font-size: 0.95em; margin-bottom: 10px; }
    .metric { display: flex; justify-content: space-between; padding: 4px 0;
              border-bottom: 1px solid #0f3460; font-size: 0.85em; }
    .metric:last-child { border-bottom: none; }
    .metric .label { color: #aaa; }
    .metric .value { color: #fff; font-weight: 600; }
    .metric .value.warn { color: #ff9800; }
    .metric .value.good { color: #4caf50; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
    th { color: #7ecfff; text-align: left; padding: 4px 8px 4px 0; border-bottom: 1px solid #0f3460; }
    td { padding: 3px 8px 3px 0; border-bottom: 1px solid #0f3460; color: #d0d0d0; }
    .empty { color: #888; text-align: center; padding: 24px; font-style: italic; }
    .nav { margin-bottom: 16px; }
    .nav a { color: #00d4ff; text-decoration: none; margin-right: 16px; font-size: 0.9em; }
    .nav a:hover { text-decoration: underline; }
    .privacy-note { color: #888; font-size: 0.75em; margin-top: 24px; text-align: center; font-style: italic; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/">Graph View</a>
    <a href="/health">Health Dashboard</a>
    <a href="/stats" style="font-weight:600;">Stats</a>
  </div>
  <h1>ENGRAM Stats Dashboard <span style="color:#7ecfff;font-size:0.7em;">(last 7 days unless noted)</span></h1>
  <div id="loading">Loading stats…</div>
  <div id="dashboard" style="display:none;">
    <div class="grid" id="cards"></div>
    <p class="privacy-note">Stats tab queries only L1 (stats-only) events. L2 (content) events are indexed but never surfaced through this dashboard.</p>
  </div>

  <script>
  function _withAgent(path) {
    const a = new URL(window.location.href).searchParams.get('agent');
    if (!a) return path;
    return path + (path.includes('?') ? '&' : '?') + 'agent=' + encodeURIComponent(a);
  }
  function fmtMs(v) { return (v === null || v === undefined) ? '—' : v + 'ms'; }
  function fmtN(v) { return (v === null || v === undefined) ? '—' : v; }
  // HTML-escape DB-sourced strings before innerHTML concatenation. The
  // indexer accepts arbitrary JSONL from ~/.engram/logs/sessions/; while
  // emitter-produced fields are well-controlled, externally crafted event
  // files could land arbitrary content in hook_name/tool_name/result_status/
  // event_type/ts/detail and into the dashboard via innerHTML. Per round-1
  // fairy review: enforce the "database-sourced strings are not HTML" invariant.
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function loadStats() {
    fetch(_withAgent('/api/stats'))
      .then(r => r.json())
      .then(data => {
        document.getElementById('loading').style.display = 'none';
        document.getElementById('dashboard').style.display = 'block';
        if (data.error || data.empty) {
          document.getElementById('cards').innerHTML =
            '<div class="card"><p class="empty">' + (data.error || data.reason) + '</p></div>';
          return;
        }
        render(data);
      })
      .catch(err => {
        document.getElementById('loading').innerHTML = 'Error: ' + err;
      });
  }

  function render(d) {
    let html = '';
    // Total events
    html += '<div class="card"><h3>Total L1 Events Indexed</h3>'
         + '<div class="metric"><span class="label">All-time</span>'
         + '<span class="value">' + d.total_events.toLocaleString() + '</span></div></div>';

    // Panel 1: auto-surface health
    if (d.surface) {
      const s = d.surface;
      const fallbackCls = s.fallback_pct > 5 ? 'warn' : 'good';
      html += '<div class="card"><h3>Auto-Surface Health</h3>'
        + '<div class="metric"><span class="label">Fires (7d)</span><span class="value">' + fmtN(s.fires_7d) + '</span></div>'
        + '<div class="metric"><span class="label">Latency min / avg / max</span><span class="value">' + fmtMs(s.latency_min_ms) + ' / ' + fmtMs(s.latency_avg_ms) + ' / ' + fmtMs(s.latency_max_ms) + '</span></div>'
        + '<div class="metric"><span class="label">FTS fallback rate</span><span class="value ' + fallbackCls + '">' + s.fallback_pct.toFixed(1) + '% (' + s.fallback_count_7d + ')</span></div>'
        + '</div>';
    }

    // Panel 2: hook fire profile
    if (d.hooks && d.hooks.length) {
      html += '<div class="card" style="grid-column: span 2"><h3>Hook Fire Profile (7d)</h3>'
        + '<table><thead><tr><th>Hook</th><th style="text-align:right">Fires</th><th style="text-align:right">Avg dur</th><th style="text-align:right">Errors</th></tr></thead><tbody>';
      d.hooks.forEach(h => {
        const errCls = h.error_count_7d > 0 ? 'warn' : '';
        html += '<tr><td>' + esc(h.hook_name) + '</td><td style="text-align:right">' + h.fires_7d + '</td><td style="text-align:right">' + fmtMs(h.avg_duration_ms) + '</td><td style="text-align:right" class="' + errCls + '">' + h.error_count_7d + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }

    // Panel 3: tool decision context
    if (d.tools && d.tools.length) {
      html += '<div class="card" style="grid-column: span 2"><h3>Tool Decision Context (7d)</h3>'
        + '<table><thead><tr><th>Tool</th><th>Status</th><th style="text-align:right">N</th></tr></thead><tbody>';
      d.tools.forEach(t => {
        html += '<tr><td>' + esc(t.tool_name) + '</td><td>' + esc(t.result_status) + '</td><td style="text-align:right">' + t.n + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }

    // Panel 4: cohort-aligned (last 10 turns)
    if (d.cohort && d.cohort.length) {
      html += '<div class="card" style="grid-column: span 2"><h3>Cohort-Aligned View (last 10 turns)</h3>'
        + '<table><thead><tr><th>Turn</th><th style="text-align:right">Events</th><th style="text-align:right">Surface</th><th style="text-align:right">Tools</th><th style="text-align:right">Hooks</th></tr></thead><tbody>';
      d.cohort.forEach(c => {
        html += '<tr><td>' + c.turn + '</td><td style="text-align:right">' + c.events + '</td><td style="text-align:right">' + c.surface_fires + '</td><td style="text-align:right">' + c.tool_calls + '</td><td style="text-align:right">' + c.hook_fires + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }

    // Panel 5: recent errors
    if (d.errors) {
      if (d.errors.length === 0) {
        html += '<div class="card"><h3>Recent Errors (24h)</h3><p class="empty">No errors in the last 24 hours.</p></div>';
      } else {
        html += '<div class="card" style="grid-column: span 2"><h3>Recent Errors (24h)</h3>'
          + '<table><thead><tr><th>Time</th><th>Event</th><th>Source</th><th>Detail</th></tr></thead><tbody>';
        d.errors.forEach(e => {
          html += '<tr><td>' + esc(e.ts) + '</td><td>' + esc(e.event_type) + '</td><td>' + esc(e.tool_name || e.hook_name || '') + '</td><td>' + esc(e.detail) + '</td></tr>';
        });
        html += '</tbody></table></div>';
      }
    }

    document.getElementById('cards').innerHTML = html;
  }

  loadStats();
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/agents":
            self._json({"agents": get_agents_meta(), "default": DEFAULT_AGENT})
            return

        if path == "/health":
            self._html(HEALTH_HTML)
            return
        if path == "/stats":
            self._html(STATS_HTML)
            return
        if path == "/":
            self._html(HTML)
            return

        # Endpoints below are agent-scoped.
        agent_name, db_path = _resolve_agent(parsed.query)

        if path == "/api/graph":
            if not db_path:
                self._json({"nodes": [], "edges": [],
                            "error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name})
            else:
                resp = get_graph(db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/meta":
            meta_resp = {"agent": agent_name,
                         "db_path": db_path,
                         "default_agent": DEFAULT_AGENT,
                         "label": AGENTS.get(agent_name, {}).get("label", agent_name)}
            # Include agent's memory-config (tier sizes, decay base) so the
            # front-end tier-classification uses the actual config values,
            # not viz-side hardcoded defaults. Lei flagged 2026-05-17 that
            # viz was showing nodes as tier 3 because TIER2_MAX=1000 was
            # hardcoded while config has tier2_max_nodes=4000.
            meta_resp["memory_config"] = _resolve_agent_config(agent_name)
            self._json(meta_resp)
        elif path == "/api/health":
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name})
            else:
                resp = get_health_data(db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/stats":
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name})
            else:
                index_db_path = _resolve_logs_index(agent_name)
                resp = get_stats_data(index_db_path)
                resp["agent"] = agent_name
                self._json(resp)
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data: dict):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="ENGRAM visualization server — D3 force-directed graph for knowledge.db"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--db", default=None,
                        help="Single-agent mode: register one anonymous "
                             "'default' agent at PATH (a knowledge.db file or "
                             f"a directory containing one). Default: {DEFAULT_DB}")
    parser.add_argument("--config", default=None,
                        help="Multi-agent mode: JSON config registering several "
                             "agents (see agents.json.example)")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="Address to bind to (default: 127.0.0.1)")
    args = parser.parse_args()

    if args.config and args.db:
        parser.error("--config and --db are mutually exclusive")

    global AGENTS, DEFAULT_AGENT
    if args.config:
        AGENTS, DEFAULT_AGENT = _load_config(args.config)
    else:
        db = os.path.expanduser(args.db) if args.db else DEFAULT_DB
        # Accept either a directory or a direct .db file path
        if os.path.isdir(db):
            db = os.path.join(db, "knowledge.db")
        AGENTS = {"default": {"name": "default", "label": "Default", "db": db}}
        DEFAULT_AGENT = "default"

    HTTPServer.allow_reuse_address = True
    bind = args.bind or "127.0.0.1"
    server = HTTPServer((bind, args.port), Handler)
    print(f"ENGRAM Visualizer →  http://localhost:{args.port}")
    print(f"Health Dashboard  →  http://localhost:{args.port}/health")
    print("Agents registered:")
    for name, info in AGENTS.items():
        marker = " (default)" if name == DEFAULT_AGENT else ""
        exists = "" if os.path.exists(info["db"]) else " [MISSING]"
        print(f"   - {name}{marker}: {info['db']}{exists}")
    print("Press Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
