#!/usr/bin/env python3
"""
ENGRAM Visualization Server

Serves a D3.js force-directed graph of an ENGRAM knowledge graph in the
browser. Stdlib-only. Three operating modes:

* Auto-detect (default — no flags): scan accessible /home/* dirs + running
  user's home for .engram*/ folders; read agent_name from each config.json.
  If nothing found, falls back to single-agent default (~/.engram/knowledge.db).
  An optional --config layered on top overrides labels/default/exclude and can
  add agents that weren't auto-detected (back-compat with explicit-db entries).
* Single-agent (--db PATH): register one anonymous 'default' agent at PATH
  (a knowledge.db file or a directory containing one).
* Explicit-only (--no-autodetect [--config PATH]): skip auto-discovery; use
  only the explicit config or the single-agent default.

Usage:
    python viz_server.py                                 # auto-detect (new default)
    python viz_server.py --db /path/to/knowledge.db      # single-agent custom
    python viz_server.py --config /path/to/agents.json   # auto-detect + override
    python viz_server.py --no-autodetect --config agents.json  # explicit-only

Deployment / restart model (operator-launched, not agent-managed):
    In a multi-agent install this server is normally started by the OPERATOR
    (a privileged user with read access to every agent's ENGRAM data directory)
    via a small launch script or command — NOT by an individual agent, whose
    user typically lacks the cross-agent read access that auto-detect and
    --config need. The process is long-lived: started once, outside any agent
    session, and it keeps running across agent restarts and upgrades.

    Consequences for an agent operating on the codebase:
    * You generally CANNOT restart this server yourself — it runs under a
      different user, so `pkill`/relaunch will fail on permissions (and you
      should not try; it is shared infrastructure you do not own). Ask the
      operator to re-run their launch script instead.
    * After an upgrade that changes THIS file, the running process keeps
      executing the OLD code until the operator restarts it — the source on
      disk being new is not enough. So the correct post-upgrade action is:
      ask the operator to restart, then confirm the new code is live via
      GET /api/health (a `health_score` field means it responded).
"""

import argparse
import glob
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

_disc_log = logging.getLogger("viz_server.discover")

# config_schema lives in tools/ (sibling directory relative to this file).
# Insert the parent of tools/ into sys.path so `from tools.config_schema import ...`
# works whether this file is run from the repo root OR from ~/.engram/ (deployed).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
try:
    from tools.config_schema import annotate_schema, SCHEMA as _CONFIG_SCHEMA  # noqa: E402
    _CONFIG_SCHEMA_AVAILABLE = True
except ImportError:
    _CONFIG_SCHEMA_AVAILABLE = False
    annotate_schema = None  # type: ignore[assignment]
    _CONFIG_SCHEMA = []  # type: ignore[assignment]

try:
    from engram_stats import _compute_health_score  # noqa: E402
    _HEALTH_SCORE_AVAILABLE = True
except ImportError:
    _HEALTH_SCORE_AVAILABLE = False
    _compute_health_score = None  # type: ignore[assignment]

try:
    from engram_log_indexer import Indexer as _Indexer  # noqa: E402
    _INDEXER_AVAILABLE = True
except ImportError:
    _INDEXER_AVAILABLE = False
    _Indexer = None  # type: ignore[assignment]

# _CONFIDENCE_BEARING_TYPES — calibration SSoT, imported from engram_stats
# (promoted to a module-level constant in #1225) so /api/schema and its CI drift
# gate track the real source rather than a replica. On import failure the empty
# tuple makes the drift test fail loudly (vs a replica that would pass trivially).
try:
    from engram_stats import _CONFIDENCE_BEARING_TYPES  # noqa: E402
except ImportError:
    _CONFIDENCE_BEARING_TYPES = ()  # type: ignore[assignment]

try:
    from engram_core import (  # noqa: E402
        VALID_NODE_TYPES,
        CLAIM_BEARING_TYPES,
        VALID_RELATIONS,
        EDGE_CLASSIFICATIONS,
    )
    _SCHEMA_CORE_AVAILABLE = True
except ImportError:
    _SCHEMA_CORE_AVAILABLE = False
    VALID_NODE_TYPES = set()  # type: ignore[assignment]
    CLAIM_BEARING_TYPES = set()  # type: ignore[assignment]
    VALID_RELATIONS = set()  # type: ignore[assignment]
    EDGE_CLASSIFICATIONS = {}  # type: ignore[assignment]

try:
    from engram_query import (  # noqa: E402
        _LOGICAL_SUBSTRATE_RELATIONS,
        _CONTEXTUAL_RELATIONS,
    )
    _SCHEMA_QUERY_AVAILABLE = True
except ImportError:
    _SCHEMA_QUERY_AVAILABLE = False
    _LOGICAL_SUBSTRATE_RELATIONS = frozenset()  # type: ignore[assignment]
    _CONTEXTUAL_RELATIONS = frozenset()  # type: ignore[assignment]


def get_schema_data() -> dict:
    """Return the schema SSoT as a JSON-serialisable dict.

    Builds from engram_core / engram_query / engram_stats constants.
    No DB access required — the schema is static.  Called by the
    /api/schema endpoint and directly by the CI drift gate.
    """
    # Logical-substrate set: the 9 from engram_query plus instantiates
    # (instantiates is in VALID_RELATIONS but in neither _LOGICAL_SUBSTRATE_RELATIONS
    # nor _CONTEXTUAL_RELATIONS — treat as logical-substrate per spec §Deliverable 1).
    logical_substrate = sorted(
        set(_LOGICAL_SUBSTRATE_RELATIONS) | {"instantiates"}
    )
    contextual = sorted(_CONTEXTUAL_RELATIONS)

    relations_detail = {
        rel: {
            "cascade": bool(EDGE_CLASSIFICATIONS.get(rel, {}).get("cascade", False)),
            "provenance": bool(EDGE_CLASSIFICATIONS.get(rel, {}).get("provenance", False)),
        }
        for rel in VALID_RELATIONS
    }

    return {
        "node_types": sorted(VALID_NODE_TYPES),
        "claim_bearing_types": sorted(CLAIM_BEARING_TYPES),
        "confidence_bearing_types": sorted(_CONFIDENCE_BEARING_TYPES),
        "relations": relations_detail,
        "logical_substrate_relations": logical_substrate,
        "contextual_relations": contextual,
    }


DEFAULT_DB = str(Path.home() / ".engram" / "knowledge.db")
DEFAULT_PORT = 5001

# Agent registry — populated in main() from --config or --db.
# Maps name -> {"name": str, "label": str, "db": str (resolved abs path)}.
AGENTS: dict = {}
DEFAULT_AGENT: str = ""

# ---------------------------------------------------------------------------
# Live agent registry cache — refreshed on a TTL so newly-born agents appear
# in the UI without restarting the viz server.  Seeded by main(); refreshed by
# _refresh_agents_if_stale() at every registry read that drives the UI.
# ---------------------------------------------------------------------------

_AGENT_CACHE_TTL_SECONDS: float = 45.0
_AGENT_CACHE_TS: float = 0.0  # time.monotonic() of last successful scan; 0 → never

# Startup mode — set in main() so _refresh_agents_if_stale replays the same path.
# "single_db"         → --db explicit or --no-autodetect without --config; static.
# "config_only"       → --no-autodetect --config; reload config on TTL (no home-scan).
# "discover_merge"    → discover + --config; re-scan + merge on TTL.
# "discover_only"     → discover, no --config, agents found; re-scan on TTL.
# "discover_fallback" → discover, no --config, nothing found; retry discovery on TTL.
_AGENT_STARTUP_MODE: str = ""
_AGENT_STARTUP_CONFIG: str = ""  # --config path for modes that need it

# ---------------------------------------------------------------------------
# Config write infrastructure — per-agent backup tracking.
# Populated on first write per agent per server process: agent_name → backup path.
# ---------------------------------------------------------------------------
_CONFIG_WRITE_BACKUPS: dict = {}  # agent_name -> backup_path written this session

# ---------------------------------------------------------------------------
# Shared top-nav helper — single source of truth for the top navigation bar
# rendered on every tab page.  Called at module load to build the HTML
# constants; avoids per-tab duplication and keeps the agent selector ID/CSS
# canonical.
# ---------------------------------------------------------------------------

_NAV_TABS = [
    ("graph",  "/",        "Graph View"),
    ("health", "/health",  "Health Dashboard"),
    ("stats",  "/stats",   "Stats"),
    ("config", "/config",  "Config"),
]

# CSS rules injected into every page's <style> block.
_NAV_SHARED_CSS = """\
    .nav-agent-label { color: #7ecfff; font-size: 0.85em; margin-right: 6px; }
    .nav-agent-select { padding: 4px 8px; background: #0d1b36; border: 1px solid #1a4a8a;
                        border-radius: 4px; color: #e0e0e0; font-size: 0.85em; outline: none; }
    #nav-agent-wrapper { display: none; margin-left: auto; }"""


def _render_nav(active_tab: str) -> str:
    """Return the HTML fragment for the shared top nav bar.

    Includes tab links (active tab bold) and the global agent-selector
    dropdown.  The dropdown is hidden until the shared nav script populates
    it (single-agent mode keeps it hidden permanently).

    active_tab: one of {"graph", "health", "stats", "config"}
    """
    links = []
    for tab_key, href, label in _NAV_TABS:
        if tab_key == active_tab:
            links.append(f'  <a href="{href}" style="font-weight:600;">{label}</a>')
        else:
            links.append(f'  <a href="{href}">{label}</a>')
    links.append(
        '  <span id="nav-agent-wrapper">'
        '<span class="nav-agent-label">Agent:</span>'
        '<select id="agent-select-nav" class="nav-agent-select"></select>'
        "</span>"
    )
    return "<div class=\"nav\">\n" + "\n".join(links) + "\n</div>"


# Shared JS block injected just before </body> on every tab page.
# Handles: populating the agent selector from /api/agents, hiding it in
# single-agent mode, rewriting tab links to preserve ?agent=, and
# navigating on change.
_NAV_SCRIPT = """\
<script>
(function () {
  var sel = document.getElementById('agent-select-nav');
  if (!sel) return;
  var wrapper = document.getElementById('nav-agent-wrapper');

  fetch('/api/agents').then(function(r) { return r.json(); }).then(function(data) {
    var agents = data.agents || [];
    var defaultAgent = data.default || (agents[0] && agents[0].name);
    if (agents.length <= 1) return;  // single-agent mode: keep selector hidden

    var currentAgent = new URL(window.location.href).searchParams.get('agent');
    if (!currentAgent || !agents.find(function(a) { return a.name === currentAgent; })) {
      currentAgent = defaultAgent;
    }

    sel.innerHTML = '';
    agents.forEach(function(a) {
      var opt = document.createElement('option');
      opt.value = a.name;
      var status = a.available ? '(' + a.current_node_count + ')' : '(unavailable)';
      opt.textContent = a.label + ' ' + status;
      if (!a.available && a.error) opt.title = a.error;
      sel.appendChild(opt);
    });
    sel.value = currentAgent;
    if (wrapper) wrapper.style.display = 'inline-flex';

    // Rewrite all same-origin tab links to preserve the agent param.
    document.querySelectorAll('.nav a').forEach(function(link) {
      var href = link.getAttribute('href');
      if (!href || !href.startsWith('/')) return;
      var u = new URL(href, window.location.origin);
      u.searchParams.set('agent', currentAgent);
      link.setAttribute('href', u.toString());
    });

    sel.onchange = function() {
      var u = new URL(window.location.href);
      u.searchParams.set('agent', sel.value);
      window.location.href = u.toString();
    };
  }).catch(function() {});
}());
</script>"""

# Sentinel strings used inside each HTML constant; replaced at module load.
_NAV_PLACEHOLDER = "<!--NAV_PLACEHOLDER-->"
_NAV_SCRIPT_PLACEHOLDER = "<!--NAV_SCRIPT_PLACEHOLDER-->"
_NAV_CSS_PLACEHOLDER = "<!--NAV_CSS_PLACEHOLDER-->"


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
    #search-box, #semantic-search-box {
      display: flex;
      gap: 6px;
    }
    #search-input, #semantic-search-input {
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
    #search-input:focus, #semantic-search-input:focus { border-color: #4a90d9; }
    #search-input::placeholder, #semantic-search-input::placeholder { color: #556; }

    /* ── Semantic search results ─────────────────────────────── */
    .search-result-item {
      padding: 5px 7px;
      margin-bottom: 4px;
      background: #0d1b36;
      border: 1px solid #1a4a8a;
      border-radius: 3px;
      cursor: pointer;
      font-size: 11px;
      color: #ccd;
    }
    .search-result-item:hover { background: #1a4a8a; }
    .search-result-id { font-family: monospace; color: #aaccff; font-size: 11px; }
    .search-result-type { font-size: 9px; color: #7788aa; text-transform: uppercase; margin-left: 4px; }
    .search-result-claim { color: #99aabb; font-size: 10px; margin-top: 2px; }
    .search-result-conf { font-size: 9px; color: #6688aa; margin-top: 1px; }
    #search-input.not-found, #semantic-search-input.not-found {
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

    /* What's new since last visit (#1169 slice-2) — distinct from sg-highlight
       (which is a brightness boost + thick stroke for subgraph traversal) and
       from the pulse-ring (a one-shot animated ring on search-focus).
       new-node-glow uses a cyan outer-glow filter + thicker stroke to signal
       "created since your last visit" without clashing with node-type fill. */
    .node-circle.new-node-glow {
      stroke: #00d4ff !important;
      stroke-width: 2.5;
      filter: drop-shadow(0 0 4px #00d4ffaa);
    }
    #new-nodes-indicator {
      font-size: 11px;
      color: #00d4ff;
      margin-top: 5px;
      display: none;
    }

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
           border-bottom: 1px solid #1a4a8a; flex-shrink: 0;
           display: flex; align-items: center; }
    .nav a { color: #00d4ff; text-decoration: none; margin-right: 16px; font-size: 0.9em; }
    .nav a:hover { text-decoration: underline; }
<!--NAV_CSS_PLACEHOLDER-->

    /* ── Content row (sidebar + graph + detail) ─────────────────────── */
    #content-row { display: flex; flex: 1; overflow: hidden; }
  </style>
</head>
<body>

<!-- ── Top nav ──────────────────────────────────────────────────────── -->
<!--NAV_PLACEHOLDER-->

<!-- ── Content row ──────────────────────────────────────────────────── -->
<div id="content-row">

<!-- ── Sidebar ──────────────────────────────────────────────────────── -->
<div id="sidebar">
  <header>
    ENGRAM Visualizer
    <small id="db-path-label"></small>
  </header>

  <div class="section">
    <div id="status-line"><span id="stat-nodes">—</span></div>
    <div id="new-nodes-indicator"></div>
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
      <input type="text" id="search-input" placeholder="e.g. dv_NNNN" spellcheck="false" />
      <div class="btn" id="search-btn">Go</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Semantic search</div>
    <div id="semantic-search-box">
      <input type="text" id="semantic-search-input" placeholder="e.g. calibration uncertainty" spellcheck="false" />
      <div class="btn" id="semantic-search-btn">Search</div>
    </div>
    <div id="semantic-search-results" style="display:none;margin-top:8px;"></div>
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
    <label class="toggle">
      <input type="checkbox" id="show-full-graph-cb" />
      Show full graph
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
      // stale_replacement is dict-keyed since PR #281 (multi-cascade safety).
      // Legacy nodes may still have scalar form; handle both.
      const repl = (meta.stale_replacement && typeof meta.stale_replacement === 'object')
          ? Object.values(meta.stale_replacement).join(', ') || '?'
          : (meta.stale_replacement || '?');
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
      // Fallback dash: use the cascade/provenance heuristic based on known types
      const solidRels = new Set(['cites', 'derives_from', 'retracts', 'supersedes', 'supported_by']);
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
</script>
<!--NAV_SCRIPT_PLACEHOLDER-->
</body>
</html>
""".replace(_NAV_PLACEHOLDER, _render_nav("graph")).replace(
    _NAV_SCRIPT_PLACEHOLDER, _NAV_SCRIPT
).replace(_NAV_CSS_PLACEHOLDER, _NAV_SHARED_CSS)

# ---------------------------------------------------------------------------
# Agent registry helpers
# ---------------------------------------------------------------------------

def _own_agent_name() -> str:
    """Return the agent_name from $ENGRAM_HOME/config.json, or '' on any error.

    $ENGRAM_HOME defaults to ~/.engram when the env var is absent.  Never
    raises — callers treat a blank return as "own agent unknown, fall back".
    """
    try:
        engram_home = os.environ.get("ENGRAM_HOME") or str(Path.home() / ".engram")
        config_path = os.path.join(engram_home, "config.json")
        with open(config_path) as f:
            cfg = json.load(f)
        name = cfg.get("agent_name", "")
        return name if isinstance(name, str) else ""
    except Exception:
        return ""


def _pick_default(agents: dict) -> str:
    """Pick the default agent for auto-detect mode.

    Preference order:
    1. The launching context's own agent ($ENGRAM_HOME/config.json agent_name),
       if present among *agents*.
    2. Alphabetically first agent name (deterministic fallback).

    Returns '' when *agents* is empty (callers should handle that case).
    """
    if not agents:
        return ""
    own = _own_agent_name()
    if own and own in agents:
        return own
    return sorted(agents.keys())[0]


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


def discover_agents(scan_roots=None) -> dict:
    """Auto-discover local ENGRAM agents by scanning home directories.

    For each root in scan_roots (default: every /home/* entry plus the running
    user's own home), glob '<root>/.engram*', skip backup/baseline dirs, and
    for each candidate require both knowledge.db and config.json. Read
    agent_name from config.json (the canonical field). Register the agent under
    that name with its db path.

    Returns a dict: agent_name -> {"name": str, "label": str, "db": str}.
    Never raises — a partial result (only readable agents) is correct behavior.
    """
    if scan_roots is None:
        roots: list = []
        try:
            roots = [Path(p) for p in glob.glob("/home/*") if Path(p).is_dir()]
        except Exception as e:
            _disc_log.debug("discover_agents: /home/* glob failed: %s", e)
        try:
            own_home = Path.home()
            if own_home not in roots:
                roots.append(own_home)
        except Exception as e:
            _disc_log.debug("discover_agents: Path.home() failed: %s", e)
    else:
        roots = [Path(r) for r in scan_roots]

    agents: dict = {}

    for root in roots:
        try:
            candidates = sorted(root.glob(".engram*"))
        except Exception as e:
            _disc_log.debug("discover_agents: glob failed for root %s: %s", root, e)
            continue

        for candidate in candidates:
            try:
                name_part = candidate.name
                # Skip backup and baseline dirs.
                if name_part.endswith(".bak") or "baseline" in name_part:
                    continue
                if not candidate.is_dir():
                    continue

                db_path = candidate / "knowledge.db"
                config_path = candidate / "config.json"

                if not db_path.exists() or not config_path.exists():
                    continue

                try:
                    with open(config_path) as f:
                        cfg = json.load(f)
                except Exception as e:
                    _disc_log.debug(
                        "discover_agents: could not read config %s: %s", config_path, e
                    )
                    continue

                agent_name = cfg.get("agent_name", "")
                if not agent_name or not isinstance(agent_name, str):
                    _disc_log.debug(
                        "discover_agents: no agent_name in %s, skipping", config_path
                    )
                    continue

                if agent_name in agents:
                    _disc_log.debug(
                        "discover_agents: collision — '%s' already registered "
                        "(db=%s), skipping %s",
                        agent_name, agents[agent_name]["db"], db_path,
                    )
                    continue

                agents[agent_name] = {
                    "name": agent_name,
                    "label": agent_name,
                    "db": str(db_path),
                }
            except Exception as e:
                _disc_log.debug(
                    "discover_agents: error processing candidate %s: %s", candidate, e
                )
                continue

    return agents


def _merge_config_over_discovered(discovered: dict, config_path: str) -> tuple[dict, str]:
    """Layer an explicit config file on top of a discovered agent registry.

    Merge semantics (auto-detect is base, config is override layer):
    1. Start from 'discovered' (auto-detected agents).
    2. Apply 'exclude' list — drop named agents.
    3. For each entry in config 'agents':
       - If already discovered: override label (and db if explicitly given).
         'db' is optional for override entries — label-only override is valid.
       - If not discovered and has 'db': add the agent (back-compat).
       - If not discovered and no 'db': warn and skip.
    4. Default: config 'default' field wins; else `_pick_default` — the
       own-agent ($ENGRAM_HOME/config.json agent_name) if present among the
       merged agents, else the first agent by sorted name.

    Returns (merged_agents_dict, default_name).
    """
    with open(config_path) as f:
        cfg = json.load(f)

    # Deep-copy the inner dicts too: a shallow dict(discovered) would share the
    # inner {name,label,db} objects, so the label/db overrides below would mutate
    # the caller's discovered dict. main() doesn't reuse it today, but the
    # don't-mutate-the-caller invariant must hold for any future caller/test.
    agents = {k: dict(v) for k, v in discovered.items()}

    # Step 2: exclude
    for name in cfg.get("exclude", []):
        if name in agents:
            agents.pop(name)
        else:
            _disc_log.debug("_merge_config: exclude '%s' not in registry, ignoring", name)

    # Step 3: layer explicit entries
    for a in cfg.get("agents", []):
        name = a.get("name")
        if not name:
            continue
        if name in agents:
            # Override fields on an already-discovered entry.
            if "label" in a:
                agents[name]["label"] = a["label"]
            if "db" in a:
                agents[name]["db"] = os.path.expanduser(a["db"])
        else:
            # Not auto-detected — add if db is provided, skip otherwise.
            if "db" in a:
                agents[name] = {
                    "name": name,
                    "label": a.get("label", name),
                    "db": os.path.expanduser(a["db"]),
                }
            else:
                _disc_log.warning(
                    "_merge_config: agent '%s' not auto-detected and no db given — skipping",
                    name,
                )

    # Step 4: default
    explicit_default = cfg.get("default")
    if explicit_default:
        if explicit_default not in agents:
            raise ValueError(
                f"{config_path}: default '{explicit_default}' is not in the "
                "merged agent registry"
            )
        default = explicit_default
    else:
        default = _pick_default(agents)

    return agents, default


def _refresh_agents_if_stale(ttl_seconds: float | None = None) -> None:
    """Refresh AGENTS/DEFAULT_AGENT when the cache is older than ttl_seconds.

    Clock: time.monotonic() — patchable in tests via unittest.mock.patch.

    Modes that re-scan home directories: discover_merge, discover_only,
    discover_fallback.  Static modes (single_db) never re-scan.

    Fail-safe: on any exception, keep the last-good registry and log a warning
    so the calling request still succeeds.
    """
    global AGENTS, DEFAULT_AGENT, _AGENT_CACHE_TS

    ttl = ttl_seconds if ttl_seconds is not None else _AGENT_CACHE_TTL_SECONDS
    now = time.monotonic()
    if now - _AGENT_CACHE_TS <= ttl:
        return  # within TTL — cache hit

    if _AGENT_STARTUP_MODE in ("single_db", ""):
        # Static registry or not yet seeded by main() — nothing to refresh.
        return

    try:
        if _AGENT_STARTUP_MODE == "config_only":
            new_agents, new_default = _load_config(_AGENT_STARTUP_CONFIG)
        elif _AGENT_STARTUP_MODE == "discover_merge":
            discovered = discover_agents()
            if not discovered:
                # Empty discover in merge-mode is a transient scan failure (homes
                # briefly unreadable) — startup found agents in this mode, so an
                # empty result now is a regression, not "all agents gone". Merging
                # {} would narrow the registry to config-explicit agents and drop
                # discovered-only ones for one TTL. Keep last-good (symmetric with
                # the discover_only/fallback guard below).
                _AGENT_CACHE_TS = now
                return
            new_agents, new_default = _merge_config_over_discovered(
                discovered, _AGENT_STARTUP_CONFIG
            )
        elif _AGENT_STARTUP_MODE in ("discover_only", "discover_fallback"):
            new_agents = discover_agents()
            if new_agents:
                new_default = _pick_default(new_agents)
            else:
                # Nothing found — keep current registry, bump timestamp to avoid
                # hammering on a persistently empty scan.
                _AGENT_CACHE_TS = now
                return
        else:
            _disc_log.debug(
                "_refresh_agents_if_stale: unknown mode %r, skipping", _AGENT_STARTUP_MODE
            )
            return

        AGENTS = new_agents
        DEFAULT_AGENT = new_default
        _AGENT_CACHE_TS = now
        _disc_log.debug(
            "_refresh_agents_if_stale: refreshed registry (%d agents, default=%r)",
            len(AGENTS), DEFAULT_AGENT,
        )
    except Exception as exc:
        _disc_log.warning(
            "_refresh_agents_if_stale: refresh failed, keeping last-good registry: %s", exc
        )
        _AGENT_CACHE_TS = now  # avoid hammering on persistent failure


def _resolve_agent(query_string: str) -> tuple[str, str]:
    """Pick an agent given the query string. Returns (name, db_path).

    Falls back to DEFAULT_AGENT when ?agent is omitted. Returns
    (requested_name, "") when ?agent names something unregistered, so the
    handler can surface a clear error instead of silently swapping in the
    default.
    """
    _refresh_agents_if_stale()
    qs = parse_qs(query_string or "")
    requested = qs.get("agent", [None])[0]
    name = requested or DEFAULT_AGENT
    if name in AGENTS:
        return name, AGENTS[name]["db"]
    return name or "", ""


def get_agents_meta() -> list:
    """Per-agent status snapshot for /api/agents (drives the UI dropdown)."""
    _refresh_agents_if_stale()
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

def search_nodes(db_path: str, query: str, limit: int = 20) -> dict:
    """FTS5-based node search for the /api/search endpoint.

    Falls back to a LIKE scan when nodes_fts is unavailable (e.g. schema
    version pre-dates FTS setup). Returns at most `limit` results ordered
    by FTS rank (best match first) or by recency for the fallback path.

    Result shape per node: {id, type, claim, confidence, status}.
    """
    if not os.path.exists(db_path):
        return {"results": [], "error": f"Database not found: {db_path}"}
    if not query or not query.strip():
        return {"results": [], "error": "query is required"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        # Check whether nodes_fts exists
        fts_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
        ).fetchone() is not None

        results = []
        if fts_exists:
            # FTS5 path — use MATCH with a simple token sanitization:
            # strip non-alphanumeric chars that FTS5 doesn't handle well,
            # append * for prefix matching on the last token.
            import re as _re
            tokens = _re.sub(r"[^\w\s]", " ", query).split()
            if tokens:
                fts_expr = " ".join(tokens[:-1] + [tokens[-1] + "*"])
            else:
                fts_expr = None

            if fts_expr:
                rows = conn.execute(
                    """SELECT n.id, n.type, n.claim, n.confidence, n.status
                       FROM nodes n
                       JOIN nodes_fts f ON n.rowid = f.rowid
                       WHERE f.nodes_fts MATCH ?
                         AND n.is_current = 1
                         AND (n.status IS NULL OR n.status NOT IN ('retracted'))
                       ORDER BY rank
                       LIMIT ?""",
                    (fts_expr, limit),
                ).fetchall()
                results = [dict(r) for r in rows]

        if not results:
            # Fallback: LIKE scan on claim
            like_pat = f"%{query.replace('%', '').replace('_', '')}%"
            rows = conn.execute(
                """SELECT id, type, claim, confidence, status
                   FROM nodes
                   WHERE claim LIKE ?
                     AND is_current = 1
                     AND (status IS NULL OR status NOT IN ('retracted'))
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (like_pat, limit),
            ).fetchall()
            results = [dict(r) for r in rows]

        conn.close()
        return {"results": results, "query": query}

    except sqlite3.OperationalError as e:
        return {"results": [], "error": str(e)}
    except Exception as e:
        return {"results": [], "error": str(e)}


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
                      "intensity_hint", "nudge_source",
                      # recall summary + keywords (added for detail panel top display)
                      "recall_summary", "recall_keywords"]
        select_cols = base_cols + [c for c in extra_cols if c in col_names]
        cur.execute(f"SELECT {','.join(select_cols)} FROM nodes ORDER BY id")
        nodes = []
        for row in cur.fetchall():
            n = dict(row)
            # Ensure booleans are plain Python ints for JSON
            n["is_current"] = int(n["is_current"]) if n["is_current"] is not None else 1
            # Parse recall_keywords from JSON string to list so JS receives an array
            if "recall_keywords" in n and n["recall_keywords"]:
                try:
                    n["recall_keywords"] = json.loads(n["recall_keywords"])
                except (json.JSONDecodeError, TypeError):
                    n["recall_keywords"] = []
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
        # Use the canonical single-source-of-truth formula from engram_stats
        # (_compute_health_score) so the viz dashboard always matches
        # engram_diagnose / engram_stats. The previous reimplementation here
        # excluded only 'contradicts' from DAG-exempt relations, over-counting
        # dag-exempt edges (exemplifies/instantiates/serves/resolves) as
        # violations → full -20 penalty → wrong score (issue #1148).
        if _HEALTH_SCORE_AVAILABLE:
            data["health_score"] = _compute_health_score(conn)
        else:
            data["health_score"] = 0.0

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
    /* Calibration card — namespaced to avoid collision with existing .bar-chart */
    .calibration-bar-chart { display: flex; align-items: flex-end; gap: 3px; height: 60px; margin: 6px 0 20px; }
    .calibration-bar-chart .cal-bar { flex: 1; border-radius: 3px 3px 0 0; min-width: 16px;
                                      position: relative; transition: height 0.3s; }
    .calibration-bar-chart .cal-bar .cal-bar-label { position: absolute; bottom: -18px; left: 50%;
                                                      transform: translateX(-50%); font-size: 0.6em;
                                                      color: #888; white-space: nowrap; }
    .calibration-bar-chart .cal-bar .cal-bar-value { position: absolute; top: -14px; left: 50%;
                                                      transform: translateX(-50%); font-size: 0.65em; color: #ccc; }
    .cal-type-label { color: #7ecfff; font-size: 0.8em; margin: 10px 0 2px; font-weight: 600; }
    .cal-caption { font-size: 0.72em; color: #888; font-style: italic; margin: 8px 0 4px; }
    .cal-col-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
    .cal-col-header { color: #7ecfff; font-size: 0.78em; font-weight: 600; text-align: center;
                      border-bottom: 1px solid #0f3460; padding-bottom: 4px; margin-bottom: 4px; }
    .cal-col-row { font-size: 0.75em; text-align: center; padding: 2px 0; color: #d0d0d0; }
    .cal-col-type { font-size: 0.72em; color: #aaa; text-align: left; padding: 2px 0; }
    .nav { display: flex; align-items: center; }
<!--NAV_CSS_PLACEHOLDER-->
  </style>
</head>
<body>
  <!--NAV_PLACEHOLDER-->
  <h1>ENGRAM Health Dashboard</h1>
  <div id="loading">Loading health data...</div>
  <div id="dashboard" style="display:none;">
    <div class="grid" id="cards"></div>
  </div>

  <script>
  function _currentAgent() {
    return new URL(window.location.href).searchParams.get('agent');
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
    Promise.all([
      fetch(_withAgent('/api/health')).then(r => r.json()),
      fetch(_withAgent('/api/calibration')).then(r => r.json()).catch(() => null),
    ]).then(([data, calData]) => {
        document.getElementById('loading').style.display = 'none';
        document.getElementById('dashboard').style.display = 'block';
        if (data.error) {
          document.getElementById('cards').innerHTML = '<div class="card"><p>' + data.error + '</p></div>';
          return;
        }
        render(data);
        if (calData && !calData.error) {
          renderCalibration(calData);
        }
      })
      .catch(e => {
        document.getElementById('loading').textContent = 'Error: ' + e.message;
      });
  }

  // Agent switching is handled by the shared top-nav selector (see
  // _NAV_SCRIPT); loadHealth() reads _currentAgent() from the URL on each
  // page load, so no per-tab wiring is needed here.
  loadHealth();

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

  // HTML-escape for DB-sourced strings used in innerHTML.
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Bucket boundaries for per-type histograms (5-6 buckets).
  // Reserved for future histogram rendering — V1 uses quantile display.
  const CAL_BUCKETS = [
    {lo: 0,    hi: 0.30, label: '<0.30', color: '#f44336'},
    {lo: 0.30, hi: 0.50, label: '0.30–0.50', color: '#ff9800'},
    {lo: 0.50, hi: 0.70, label: '0.50–0.70', color: '#ffeb3b'},
    {lo: 0.70, hi: 0.85, label: '0.70–0.85', color: '#8bc34a'},
    {lo: 0.85, hi: 0.95, label: '0.85–0.95', color: '#4caf50'},
    {lo: 0.95, hi: 1.01, label: '≥0.95',    color: '#00e676'},
  ];

  function calDeltaClass(delta) {
    const abs = Math.abs(delta);
    if (abs >= 0.15) return 'danger';
    if (abs >= 0.10) return 'warn';
    return 'good';
  }

  function renderCalibration(cal) {
    const cards = document.getElementById('cards');
    if (!cards) return;

    const corpus = cal.corpus || {};
    const byType = corpus.by_type || {};
    const byQt   = corpus.by_quote_type || {};
    const byRt   = corpus.by_reasoning_type || {};
    const drift  = cal.drift_by_type || {};

    const PRIMARY_TYPES = ['observation_factual', 'derivation', 'conjecture', 'lesson'];

    // ── Per-type histograms ───────────────────────────────────────────────
    let histHtml = '';
    for (const ntype of PRIMARY_TYPES) {
      const stats = byType[ntype] || {n: 0};
      const n = stats.n || 0;
      // We don't have raw distribution, so approximate from p-quantiles:
      // use n + p25/p50/p75/p90/p95 to assign fractional counts to buckets.
      // Simpler approach for V1: render a compact stats row instead of a
      // histogram (true bucket data requires a separate query not in the
      // current API shape). Display p25/p50/p75 summary row + N.
      if (n === 0) {
        histHtml += `<div class="cal-type-label">${esc(ntype)}</div>
          <div style="color:#888;font-size:0.78em;font-style:italic;">No data</div>`;
      } else {
        const p50 = stats.p50 !== null ? stats.p50.toFixed(2) : '—';
        const p25 = stats.p25 !== null ? stats.p25.toFixed(2) : '—';
        const p75 = stats.p75 !== null ? stats.p75.toFixed(2) : '—';
        const mean = stats.mean !== null ? stats.mean.toFixed(3) : '—';
        histHtml += `<div class="cal-type-label">${esc(ntype)} <span style="color:#888;font-weight:normal;">(n=${n})</span></div>
          ${metric('p25 / p50 / p75', p25 + ' / ' + p50 + ' / ' + p75)}
          ${metric('mean', mean)}`;
      }
    }

    // ── Per-quote_type table ──────────────────────────────────────────────
    const qtEntries = Object.entries(byQt).sort((a, b) => b[1].n - a[1].n);
    let qtHtml = '';
    if (qtEntries.length === 0) {
      qtHtml = '<p class="empty" style="padding:8px 0;">No quote_type data yet.</p>';
    } else {
      qtHtml = '<table><thead><tr><th>quote_type</th><th style="text-align:right">n</th><th style="text-align:right">mean</th></tr></thead><tbody>';
      for (const [qt, s] of qtEntries) {
        qtHtml += `<tr><td>${esc(qt)}</td><td style="text-align:right">${s.n}</td><td style="text-align:right">${s.mean.toFixed(3)}</td></tr>`;
      }
      qtHtml += '</tbody></table>';
      qtHtml += '<p class="cal-caption">the structural-confidence-determination axiom — structural anchoring — see ob_NNNN for the 2D matrix design.</p>';
    }

    // ── Per-reasoning_type table ──────────────────────────────────────────
    const rtEntries = Object.entries(byRt).sort((a, b) => b[1].n - a[1].n);
    let rtHtml = '';
    if (rtEntries.length === 0) {
      rtHtml = '<p class="empty" style="padding:8px 0;">No reasoning_type data yet.</p>';
    } else {
      rtHtml = '<table><thead><tr><th>reasoning_type</th><th style="text-align:right">n</th><th style="text-align:right">mean</th></tr></thead><tbody>';
      for (const [rt, s] of rtEntries) {
        rtHtml += `<tr><td>${esc(rt)}</td><td style="text-align:right">${s.n}</td><td style="text-align:right">${s.mean.toFixed(3)}</td></tr>`;
      }
      rtHtml += '</tbody></table>';
    }

    // ── Drift indicator ───────────────────────────────────────────────────
    let driftHtml = '';
    const driftEntries = Object.entries(drift);
    if (driftEntries.length === 0) {
      driftHtml = '<p style="color:#888;font-size:0.8em;font-style:italic;">No this-turn data yet — drift available after first filing.</p>';
    } else {
      for (const [ntype, d] of driftEntries) {
        const cls = calDeltaClass(d.delta);
        const sign = d.delta >= 0 ? '+' : '';
        driftHtml += `<div class="metric">
          <span class="label">${esc(ntype)}</span>
          <span class="value ${cls}" title="this_turn p50=${d.this_turn_p50} corpus p50=${d.corpus_p50} n_this_turn=${d.n_this_turn}">
            ${d.this_turn_p50.toFixed(2)} vs ${d.corpus_p50.toFixed(2)} · delta ${sign}${d.delta.toFixed(3)}
          </span>
        </div>`;
      }
    }

    // ── Assemble card ─────────────────────────────────────────────────────
    const cardHtml = `<div class="card" style="grid-column: span 2">
      <h3>Confidence Calibration</h3>
      <h2 style="margin-top:0;margin-bottom:6px;">Per-Type Summary (corpus)</h2>
      ${histHtml}
      <h2>Per-Quote-Type (observation_factual)</h2>
      ${qtHtml}
      <h2>Per-Reasoning-Type (derivation)</h2>
      ${rtHtml}
      <h2>Drift: This Turn vs Corpus (p50 delta)</h2>
      ${driftHtml}
    </div>`;

    cards.innerHTML += cardHtml;
  }
  </script>
<!--NAV_SCRIPT_PLACEHOLDER-->
</body>
</html>""".replace(_NAV_PLACEHOLDER, _render_nav("health")).replace(
    _NAV_SCRIPT_PLACEHOLDER, _NAV_SCRIPT
).replace(_NAV_CSS_PLACEHOLDER, _NAV_SHARED_CSS)


# ---------------------------------------------------------------------------
# Stats tab — agent-stats dashboard backed by ~/.engram/logs/index.db
# Per alpha #175 (two-level logging architecture).
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
    defaults = {"tier2_max_nodes": 1000, "decay_base": 1.014}
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
            "tier2_max_nodes": int(mem.get("tier2_max_nodes", defaults["tier2_max_nodes"])),
            "decay_base": float(mem.get("decay_base", defaults["decay_base"])),
        }
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return defaults


def _resolve_config_path(agent_name: str) -> str:
    """Resolve agent_name to its config.json path. Returns empty string if not found."""
    agent_meta = AGENTS.get(agent_name)
    if not agent_meta:
        return ""
    db_path = agent_meta.get("db", "")
    if not db_path:
        return ""
    engram_home = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(engram_home, "config.json")


def get_config_data(agent_name: str) -> dict:
    """Load and annotate config.json for /api/config.

    Returns:
      {
        "agent": str,
        "config_path": str,
        "annotated": {"sections": [...]} | None,
        "error": str (only present on failure),
      }
    Note: config_raw is intentionally NOT returned — Tier C keys must not
    leak at the API layer even if they're excluded from the rendered UI.
    """
    config_path = _resolve_config_path(agent_name)
    result: dict = {"agent": agent_name, "config_path": config_path}

    if not config_path or not os.path.exists(config_path):
        result["annotated"] = None
        result["error"] = f"config.json not found at {config_path!r}"
        return result

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        result["annotated"] = None
        result["error"] = f"Failed to read config.json: {exc}"
        return result

    if _CONFIG_SCHEMA_AVAILABLE and annotate_schema is not None:
        try:
            result["annotated"] = annotate_schema(config_raw)
        except Exception as exc:
            result["annotated"] = None
            result["error"] = f"Schema annotation failed: {exc}"
    else:
        result["annotated"] = None
        result["error"] = "config_schema module not available"

    return result


# ---------------------------------------------------------------------------
# Config write infrastructure — PUT /api/config/<key>
#
# Validation derives entirely from tools/config_schema.py SCHEMA — types,
# ranges, allowed values, and restart_required are all schema-driven, not
# hardcoded lists. Only editable=True keys may be written.
#
# In-place write pattern: build content in a temp file, then copy it into
# config.json's existing inode (NOT os.rename, which would swap inodes and
# strip the file's owner+mode — GitHub #372). Backup-once-per-session per
# agent. Read-after-write verify with rollback on failure.
# ---------------------------------------------------------------------------


def _schema_index() -> dict:
    """Build key → schema_row dict for fast lookup. Returns {} if unavailable."""
    return {row["key"]: row for row in _CONFIG_SCHEMA}


def _validate_config_value(key: str, raw_value) -> tuple:
    """Validate value against schema. Returns (coerced_value, error_str|None).

    error_str is None on success. On failure, error_str describes the rejection.
    Rejects unknown keys (not in schema) and non-editable keys.
    Type coercion: string→int/float/bool is attempted when the inbound type is
    JSON-compatible (e.g. JSON int 5 for schema type "integer" is accepted;
    string "5" is also accepted for int/float for browser form tolerance).
    For list/list_of_objects types, the value must already be a list.
    """
    index = _schema_index()
    if not index:
        return None, "config_schema not available — cannot validate"
    if key not in index:
        return None, f"Unknown config key: {key!r}"
    row = index[key]
    if not row.get("editable", False):
        return None, f"Config key {key!r} is read-only"

    expected_type = row.get("type", "")
    v = raw_value

    # Type coercion + check
    if expected_type == "integer":
        try:
            v = int(v)
        except (ValueError, TypeError):
            return None, f"Key {key!r} expects integer, got {type(raw_value).__name__}: {raw_value!r}"
    elif expected_type == "float":
        try:
            v = float(v)
        except (ValueError, TypeError):
            return None, f"Key {key!r} expects float, got {type(raw_value).__name__}: {raw_value!r}"
    elif expected_type == "boolean":
        if isinstance(v, bool):
            pass
        elif isinstance(v, int):
            v = bool(v)
        elif isinstance(v, str):
            if v.lower() in ("true", "1", "yes"):
                v = True
            elif v.lower() in ("false", "0", "no"):
                v = False
            else:
                return None, f"Key {key!r} expects boolean, got string {v!r}"
        else:
            return None, f"Key {key!r} expects boolean, got {type(raw_value).__name__}: {raw_value!r}"
    elif expected_type == "string":
        if not isinstance(v, str):
            return None, f"Key {key!r} expects string, got {type(raw_value).__name__}: {raw_value!r}"
    elif expected_type in ("list", "list_of_objects"):
        if not isinstance(v, list):
            return None, f"Key {key!r} expects list, got {type(raw_value).__name__}: {raw_value!r}"
        if expected_type == "list_of_objects":
            for i, item in enumerate(v):
                if not isinstance(item, dict):
                    return None, (
                        f"Key {key!r}: item {i} must be a dict, "
                        f"got {type(item).__name__}: {item!r}"
                    )
                if not isinstance(item.get("domain"), str) or not item["domain"]:
                    return None, (
                        f"Key {key!r}: item {i} must have a non-empty string 'domain' key"
                    )
    else:
        # Unknown type in schema — accept as-is but flag
        pass

    # Range check (integer / float)
    if expected_type in ("integer", "float"):
        if "min" in row and v < row["min"]:
            return None, f"Key {key!r}: value {v} is below minimum {row['min']}"
        if "max" in row and v > row["max"]:
            return None, f"Key {key!r}: value {v} exceeds maximum {row['max']}"

    # Allowed-values check (present on some schema rows as "allowed_values")
    if "allowed_values" in row and v not in row["allowed_values"]:
        return None, (
            f"Key {key!r}: value {v!r} not in allowed values "
            f"{row['allowed_values']!r}"
        )

    # Pattern check (present on string rows that declare "pattern")
    if expected_type == "string" and "pattern" in row:
        import re
        if not re.fullmatch(row["pattern"], v):
            hint = row.get("pattern_error", f"expected format matching {row['pattern']!r}")
            return None, f"Key {key!r}: value {v!r} does not match expected format ({hint})"

    return v, None


def _set_nested(config: dict, key: str, value) -> None:
    """Set a dotted-path key in config dict, creating intermediate dicts."""
    parts = key.split(".")
    obj = config
    for part in parts[:-1]:
        if part not in obj or not isinstance(obj[part], dict):
            obj[part] = {}
        obj = obj[part]
    obj[parts[-1]] = value


def _get_nested(config: dict, key: str):
    """Get a dotted-path key from config dict. Returns _MISSING sentinel if absent."""
    parts = key.split(".")
    obj = config
    for part in parts:
        if not isinstance(obj, dict) or part not in obj:
            return _MISSING
        obj = obj[part]
    return obj


class _Missing:
    """Sentinel for absent nested keys (distinct from None)."""
    def __repr__(self):
        return "<MISSING>"


_MISSING = _Missing()


def write_config_key(agent_name: str, key: str, raw_value) -> dict:
    """Write a validated config key for the named agent.

    Returns:
      {"ok": True, "restart_required": bool, "validated_value": <value>}
      or {"ok": False, "error": "<msg>"}

    Guarantees:
    - Validation (type + range) before any disk touch.
    - Backup-once-per-session per agent.
    - In-place write (preserves config.json inode → owner + mode).
    - Read-after-write verify; rollback to backup on failure.
    - Per-agent isolation: only touches the config.json for agent_name.
    """
    # Resolve config path using the same function as the GET path.
    config_path = _resolve_config_path(agent_name)
    if not config_path:
        return {"ok": False, "error": f"Unknown agent: {agent_name!r}"}
    if not os.path.exists(config_path):
        return {"ok": False, "error": f"config.json not found at {config_path!r}"}

    # Validate before touching disk.
    validated_value, err = _validate_config_value(key, raw_value)
    if err:
        return {"ok": False, "error": err}

    # Determine restart_required from schema metadata.
    index = _schema_index()
    row = index.get(key, {})
    restart_required = bool(row.get("restart_required", False))

    # Load current config.
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Failed to read config.json: {exc}"}

    # Backup once per agent per session.
    if agent_name not in _CONFIG_WRITE_BACKUPS:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"{config_path}.bak.{ts}"
        try:
            shutil.copy2(config_path, backup_path)
            _CONFIG_WRITE_BACKUPS[agent_name] = backup_path
        except OSError as exc:
            return {"ok": False, "error": f"Failed to create backup: {exc}"}

    backup_path = _CONFIG_WRITE_BACKUPS[agent_name]

    # Apply the new value.
    _set_nested(config, key, validated_value)

    # In-place write: copy validated content into config.json's EXISTING inode
    # rather than os.rename(temp, config.json). os.rename swaps in the temp
    # file's inode, which carries the temp's owner + mode (operator UID, 0600
    # from mkstemp) — when the viz server runs as a different UID than the
    # agent that owns config.json, that strips the agent's ownership and locks
    # it out of its own config (GitHub #372). Building in a temp first keeps a
    # serialization error from truncating the live file; the copy into the
    # existing inode is not crash-atomic, so the backup + read-after-write
    # verify + rollback below cover a torn write. Second tradeoff vs os.rename
    # (which is atomic at the VFS layer): a concurrent reader can transiently
    # observe a partially-written config.json and get a JSONDecodeError. The
    # copy window is sub-millisecond for typical config sizes and config is
    # read at startup, not continuously, so this is not a real-world concern.
    config_dir = os.path.dirname(config_path)
    try:
        fd, tmp_path = _tempfile_in_dir(config_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
                f.write("\n")
            with open(tmp_path, "r", encoding="utf-8") as src, \
                    open(config_path, "w", encoding="utf-8") as dst:
                shutil.copyfileobj(src, dst)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except OSError as exc:
        # In-place commit can leave config.json torn — restore from backup.
        try:
            shutil.copyfile(backup_path, config_path)
        except OSError:
            pass
        return {"ok": False, "error": f"Write failed: {exc}"}

    # Read-after-write verify.
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            written = json.load(f)
        written_val = _get_nested(written, key)
        if isinstance(written_val, _Missing) or written_val != validated_value:
            # Verify failed — rollback from backup.
            try:
                shutil.copyfile(backup_path, config_path)
            except OSError:
                pass
            return {"ok": False, "error": "write verify failed, rolled back"}
    except (OSError, json.JSONDecodeError) as exc:
        # Can't verify — rollback.
        try:
            shutil.copyfile(backup_path, config_path)
        except OSError:
            pass
        return {"ok": False, "error": f"write verify failed (read error: {exc}), rolled back"}

    # After verify success: refresh backup so any later same-session rollback
    # lands on last-good (this written value) rather than session-start.
    try:
        shutil.copyfile(config_path, backup_path)
    except OSError:
        pass

    return {
        "ok": True,
        "restart_required": restart_required,
        "validated_value": validated_value,
    }


def _tempfile_in_dir(directory: str) -> tuple:
    """Create a named temp file in directory. Returns (fd, path).

    Uses mkstemp for atomic O_EXCL creation with no auto-delete, so
    write_config_key controls the temp's lifecycle: build content, copy it
    into config.json's existing inode, then unlink the temp.
    """
    fd, path = tempfile.mkstemp(dir=directory, prefix=".config_write_", suffix=".tmp")
    return fd, path


# ---------------------------------------------------------------------------
# Calibration data — confidence distribution across windows
# Queries replicated from server.py:_compute_confidence_distribution.
# viz_server.py is stdlib-only; importing server.py would pull in the full
# MCP server stack, so the SQL is dual-sourced here intentionally.
#
# WARNING: SQL queries in _conf_dist_for_window mirror
# server.py:_compute_confidence_distribution (around lines 6971-7103).
# If server.py's confidence aggregation logic changes, update both.
# ---------------------------------------------------------------------------

def _viz_percentile(sorted_vals: list, p: float) -> float:
    """Compute p-th percentile of a pre-sorted list. Linear interpolation.

    Mirrors server.py:_percentile exactly (same algorithm, same edge-case
    handling). Duplicated here to keep viz_server.py self-contained.
    """
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def _conf_dist_for_window(
    conn: sqlite3.Connection,
    created_at_filter: str,
    filter_args: list,
) -> dict:
    """Compute confidence distribution for a single time window.

    Returns dict with by_type, by_quote_type, by_reasoning_type,
    by_source_class sub-keys.  Empty results use empty dicts, never None.
    Mirrors server.py:_compute_confidence_distribution.

    INTENTIONAL DIVERGENCE: emits {"n": 0, ...} placeholder entries for empty
    types so the UI always has consistent rows; server.py skips them via
    continue.
    """
    _base_filter = (
        " AND is_current = 1"
        " AND (status IS NULL OR status != 'retracted')"
    )
    _CONFIDENCE_BEARING_TYPES = (
        "observation_factual",
        "derivation",
        "conjecture",
        "lesson",
        "axiom",
        "definition",
    )

    # ── by_type ──────────────────────────────────────────────────────────
    by_type: dict = {}
    for ntype in _CONFIDENCE_BEARING_TYPES:
        rows = conn.execute(
            "SELECT confidence FROM nodes WHERE type = ?"
            + _base_filter
            + created_at_filter
            + " AND confidence IS NOT NULL ORDER BY confidence ASC",
            [ntype] + filter_args,
        ).fetchall()
        confidences = [r["confidence"] for r in rows]
        if not confidences:
            # Emit explicit empty placeholder so the UI can render a row
            by_type[ntype] = {"n": 0, "mean": None, "p25": None, "p50": None,
                              "p75": None, "p90": None, "p95": None}
            continue
        n = len(confidences)
        by_type[ntype] = {
            "n": n,
            "mean": round(sum(confidences) / n, 3),
            "p25": round(_viz_percentile(confidences, 25), 2),
            "p50": round(_viz_percentile(confidences, 50), 2),
            "p75": round(_viz_percentile(confidences, 75), 2),
            "p90": round(_viz_percentile(confidences, 90), 2),
            "p95": round(_viz_percentile(confidences, 95), 2),
        }

    # ── by_quote_type ─────────────────────────────────────────────────────
    by_quote_type: dict = {}
    for row in conn.execute(
        "SELECT quote_type, COUNT(*) as n, AVG(confidence) as mean_conf"
        " FROM nodes WHERE type = 'observation_factual'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND quote_type IS NOT NULL"
        " GROUP BY quote_type",
        filter_args,
    ).fetchall():
        qt = row["quote_type"]
        if not qt:
            continue
        by_quote_type[qt] = {"n": row["n"], "mean": round(row["mean_conf"], 3)}

    # ── by_reasoning_type ────────────────────────────────────────────────
    by_reasoning_type: dict = {}
    deriv_rows = conn.execute(
        "SELECT confidence, metadata FROM nodes WHERE type = 'derivation'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND metadata IS NOT NULL",
        filter_args,
    ).fetchall()
    _rt_buckets: dict = {}
    for row in deriv_rows:
        try:
            meta = json.loads(row["metadata"])
            rtype = meta.get("reasoning_type")
        except (json.JSONDecodeError, TypeError):
            rtype = None
        if not rtype:
            rtype = "legacy_untyped"
        _rt_buckets.setdefault(rtype, []).append(row["confidence"])
    for rtype, vals in _rt_buckets.items():
        by_reasoning_type[rtype] = {
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 3),
        }

    # ── by_source_class ──────────────────────────────────────────────────
    by_source_class: dict = {}
    obs_rows = conn.execute(
        "SELECT confidence, metadata FROM nodes WHERE type = 'observation_factual'"
        + _base_filter
        + created_at_filter
        + " AND confidence IS NOT NULL AND metadata IS NOT NULL",
        filter_args,
    ).fetchall()
    _sc_buckets: dict = {}
    for row in obs_rows:
        try:
            meta = json.loads(row["metadata"])
            sc = meta.get("source_class", "external")
        except (json.JSONDecodeError, TypeError):
            sc = "external"
        _sc_buckets.setdefault(sc, []).append(row["confidence"])
    for sc, vals in _sc_buckets.items():
        by_source_class[sc] = {
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 3),
        }

    return {
        "by_type": by_type,
        "by_quote_type": by_quote_type,
        "by_reasoning_type": by_reasoning_type,
        "by_source_class": by_source_class,
    }


def get_calibration_data(db_path: str) -> dict:
    """Compute confidence calibration data across three time windows.

    Windows mirror engram_stats / engram_diagnose conventions:
      corpus       — all time (no created_at filter)
      this_turn    — last 24 h (≈ 1-turn window in engram_stats terms)
      rolling_7    — last 168 h (≈ 7-turn window)
      drift_by_type — per-type p50 delta (this_turn vs corpus, only when n≥3)

    Returns {"error": ...} on failure.  Never raises.
    """
    if not db_path or not os.path.exists(db_path):
        return {"error": f"Database not found: {db_path!r}"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return {"error": f"Could not open DB: {exc}"}
    try:
        now_utc = datetime.now(timezone.utc)

        # Corpus: all-time, no filter
        corpus = _conf_dist_for_window(conn, "", [])

        # this_turn: last 24 h (matches engram_stats mode="1-turn")
        cutoff_1t = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        this_turn = _conf_dist_for_window(conn, " AND created_at >= ?", [cutoff_1t])

        # rolling_7: last 168 h (matches engram_stats mode="7-turn")
        cutoff_7t = (now_utc - timedelta(hours=168)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rolling_7 = _conf_dist_for_window(conn, " AND created_at >= ?", [cutoff_7t])

        # Drift: this_turn p50 vs corpus p50, only when this_turn has ≥3 samples
        # (mirrors engram_diagnose gate: avoids noisy single-node drift reports)
        drift_by_type: dict = {}
        for ntype, corpus_stats in corpus["by_type"].items():
            this_stats = this_turn["by_type"].get(ntype, {})
            corpus_p50 = corpus_stats.get("p50")
            this_p50 = this_stats.get("p50")
            n_this = this_stats.get("n", 0)
            if corpus_p50 is None or this_p50 is None or n_this < 3:
                continue
            drift_by_type[ntype] = {
                "this_turn_p50": this_p50,
                "corpus_p50": corpus_p50,
                "delta": round(this_p50 - corpus_p50, 3),
                "n_this_turn": n_this,
            }

        return {
            "corpus": corpus,
            "this_turn": this_turn,
            "rolling_7": rolling_7,
            "drift_by_type": drift_by_type,
        }
    finally:
        conn.close()


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
        # Auto-bootstrap: run the indexer on-demand when index.db is absent.
        # Blocks the single-threaded HTTPServer for the indexing pass, but this
        # is a one-time cost per server lifecycle — once index.db exists, this
        # branch is never re-entered for the lifetime of the process.
        if _INDEXER_AVAILABLE and index_db_path:
            try:
                _Indexer.run_once(logs_dir=Path(index_db_path).parent)
            except Exception as exc:
                logging.warning("stats auto-run failed: %s", exc)
        if not index_db_path or not os.path.exists(index_db_path):
            indexer_path = os.path.join(_THIS_DIR, "engram_log_indexer.py")
            return {
                "empty": True,
                "reason": (
                    "No index.db at expected location yet — has the indexer run? "
                    f"Try: python3 {indexer_path}"
                ),
            }
    try:
        # mode=ro (no immutable=1): index.db uses WAL journal mode (#879) so
        # mode=ro needs -shm/-wal access; immutable=1 would skip those files
        # and produce stale reads.
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
    /* Bottom section: tall Tool Decision Context (left) beside a stacked column
       of Cohort / Recent Errors / Confidence Trends (right). align-items:start
       lets the short right-column cards pack from the top and fill the blank
       under the tall left card instead of flowing below the fold (#1225 Item 8). */
    .stats-bottom { grid-column: 1 / -1; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }
    .stats-col { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
    @media (max-width: 900px) { .stats-bottom { grid-template-columns: 1fr; } }
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
    .nav { display: flex; align-items: center; }
<!--NAV_CSS_PLACEHOLDER-->
  </style>
</head>
<body>
  <!--NAV_PLACEHOLDER-->
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
    Promise.all([
      fetch(_withAgent('/api/stats')).then(r => r.json()),
      fetch(_withAgent('/api/calibration')).then(r => r.json()).catch(() => null),
    ]).then(([data, calData]) => {
        document.getElementById('loading').style.display = 'none';
        document.getElementById('dashboard').style.display = 'block';
        if (data.error || data.empty) {
          document.getElementById('cards').innerHTML =
            '<div class="card"><p class="empty">' + (data.error || data.reason) + '</p></div>';
          return;
        }
        render(data);
        if (calData && !calData.error) {
          renderConfidenceTrends(calData);
        }
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

    // ── Bottom section (#1225 Item 8) ───────────────────────────────────
    // Tool Decision Context is tall; Cohort-Aligned is a fixed short 10-row
    // block. Lay them out as two columns — tall card on the left, a stacked
    // column of the short cards (Cohort / Recent Errors / Confidence Trends)
    // on the right — so the short cards fill the blank beside Tool Decision
    // and stay above the fold instead of flowing below it.
    let leftHtml = '';
    let rightHtml = '';

    // Left column: tool decision context (the tall one)
    if (d.tools && d.tools.length) {
      leftHtml += '<div class="card"><h3>Tool Decision Context (7d)</h3>'
        + '<table><thead><tr><th>Tool</th><th>Status</th><th style="text-align:right">N</th></tr></thead><tbody>';
      d.tools.forEach(t => {
        leftHtml += '<tr><td>' + esc(t.tool_name) + '</td><td>' + esc(t.result_status) + '</td><td style="text-align:right">' + t.n + '</td></tr>';
      });
      leftHtml += '</tbody></table></div>';
    }

    // Right column (stacked): cohort-aligned (last 10 turns)
    if (d.cohort && d.cohort.length) {
      rightHtml += '<div class="card"><h3>Cohort-Aligned View (last 10 turns)</h3>'
        + '<table><thead><tr><th>Turn</th><th style="text-align:right">Events</th><th style="text-align:right">Surface</th><th style="text-align:right">Tools</th><th style="text-align:right">Hooks</th></tr></thead><tbody>';
      d.cohort.forEach(c => {
        rightHtml += '<tr><td>' + c.turn + '</td><td style="text-align:right">' + c.events + '</td><td style="text-align:right">' + c.surface_fires + '</td><td style="text-align:right">' + c.tool_calls + '</td><td style="text-align:right">' + c.hook_fires + '</td></tr>';
      });
      rightHtml += '</tbody></table></div>';
    }

    // Right column (stacked): recent errors
    if (d.errors) {
      if (d.errors.length === 0) {
        rightHtml += '<div class="card"><h3>Recent Errors (24h)</h3><p class="empty">No errors in the last 24 hours.</p></div>';
      } else {
        rightHtml += '<div class="card"><h3>Recent Errors (24h)</h3>'
          + '<table><thead><tr><th>Time</th><th>Event</th><th>Source</th><th>Detail</th></tr></thead><tbody>';
        d.errors.forEach(e => {
          rightHtml += '<tr><td>' + esc(e.ts) + '</td><td>' + esc(e.event_type) + '</td><td>' + esc(e.tool_name || e.hook_name || '') + '</td><td>' + esc(e.detail) + '</td></tr>';
        });
        rightHtml += '</tbody></table></div>';
      }
    }

    // Confidence Trends is appended into #stats-col-right by
    // renderConfidenceTrends() once /api/calibration resolves.
    if (leftHtml) {
      // Two columns: tall left, stacked right.
      html += '<div class="stats-bottom">'
        + '<div class="stats-col stats-col-left">' + leftHtml + '</div>'
        + '<div class="stats-col stats-col-right" id="stats-col-right">' + rightHtml + '</div>'
        + '</div>';
    } else if (rightHtml) {
      // No left content (e.g. fresh install, no tool calls in 7d) — render the
      // right column full-width rather than leaving a blank 50% left column.
      // Keep id="stats-col-right" so the async Confidence Trends append lands here.
      html += '<div class="stats-col stats-col-right" id="stats-col-right" style="grid-column: 1 / -1">' + rightHtml + '</div>';
    }

    document.getElementById('cards').innerHTML = html;
  }

  function renderConfidenceTrends(cal) {
    const cards = document.getElementById('cards');
    if (!cards) return;

    const PRIMARY_TYPES = ['observation_factual', 'derivation', 'conjecture', 'lesson'];
    const windows = [
      {key: 'corpus',    label: 'All time'},
      {key: 'rolling_7', label: '7-turn'},
      {key: 'this_turn', label: '1-turn'},
    ];

    // Build header row
    let tableHtml = '<table><thead><tr>'
      + '<th>Type</th>'
      + windows.map(w => '<th style="text-align:right">' + w.label + ' p50 (n)</th>').join('')
      + '</tr></thead><tbody>';

    for (const ntype of PRIMARY_TYPES) {
      tableHtml += '<tr><td>' + esc(ntype) + '</td>';
      for (const w of windows) {
        const wdata = (cal[w.key] || {}).by_type || {};
        const stats = wdata[ntype] || {};
        if (!stats.n) {
          tableHtml += '<td style="text-align:right;color:#888;">—</td>';
        } else {
          const p50 = stats.p50 !== null && stats.p50 !== undefined ? stats.p50.toFixed(2) : '—';
          tableHtml += '<td style="text-align:right">' + p50 + ' <span style="color:#888;font-size:0.85em;">(' + stats.n + ')</span></td>';
        }
      }
      tableHtml += '</tr>';
    }
    tableHtml += '</tbody></table>';

    const cardHtml = '<div class="card"><h3>Confidence Trends</h3>'
      + '<p style="font-size:0.78em;color:#888;margin-bottom:8px;">Per-type median confidence — All time / 7-turn window / 1-turn window. Divergence indicates recent filing drift.</p>'
      + tableHtml
      + '</div>';

    // Append into the right-hand stacked column (#1225 Item 8) so Confidence
    // Trends fills the blank under Cohort/Errors; fall back to #cards if the
    // bottom section wasn't rendered (e.g. no tools/cohort/errors data).
    const rightCol = document.getElementById('stats-col-right');
    // insertAdjacentHTML('beforeend') appends without re-parsing existing
    // children (no flicker, no listener loss) — preferred over innerHTML +=.
    (rightCol || cards).insertAdjacentHTML('beforeend', cardHtml);
  }

  loadStats();
  </script>
<!--NAV_SCRIPT_PLACEHOLDER-->
</body>
</html>""".replace(_NAV_PLACEHOLDER, _render_nav("stats")).replace(
    _NAV_SCRIPT_PLACEHOLDER, _NAV_SCRIPT
).replace(_NAV_CSS_PLACEHOLDER, _NAV_SHARED_CSS)


# ---------------------------------------------------------------------------
# Config tab HTML — editable config viewer over ~/.engram/config.json
# ---------------------------------------------------------------------------

CONFIG_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>ENGRAM Config</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
           background: #1a1a2e; color: #e0e0e0; padding: 24px; }
    h1 { color: #00d4ff; font-size: 1.5em; margin-bottom: 16px; }
    .nav { margin-bottom: 16px; display: flex; align-items: center; }
    .nav a { color: #00d4ff; text-decoration: none; margin-right: 16px; font-size: 0.9em; }
    .nav a:hover { text-decoration: underline; }
<!--NAV_CSS_PLACEHOLDER-->

    /* Section cards */
    .section-card { background: #16213e; border-radius: 8px; padding: 16px;
                    border: 1px solid #0f3460; margin-bottom: 14px; }
    .section-card h3 { color: #00d4ff; font-size: 0.95em; margin-bottom: 12px; }
    .section-gated { opacity: 0.5; pointer-events: none; }
    .section-gated .gate-row { opacity: 1; pointer-events: auto; }

    /* Rows */
    .config-row { display: flex; align-items: flex-start; gap: 10px;
                  padding: 6px 0; border-bottom: 1px solid #0f3460; font-size: 0.85em; }
    .config-row:last-child { border-bottom: none; }
    .row-label { color: #aaa; min-width: 200px; flex-shrink: 0; display: flex;
                 align-items: center; gap: 4px; }
    .row-control { flex: 1; }
    .row-badges { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 2px; }

    /* Badges */
    .badge { font-size: 0.72em; padding: 1px 5px; border-radius: 3px;
             white-space: nowrap; }
    .badge-restart { background: #1a3a5c; color: #7ecfff; border: 1px solid #1a4a8a; }
    .badge-legacy  { background: #3a2a0a; color: #ffb74d; border: 1px solid #7a4500; cursor: help; }
    .badge-readonly { background: #2a2a32; color: #999; border: 1px solid #44444c; cursor: help; }

    /* Controls */
    input[type="text"], input[type="number"] {
      background: #0d1b36; border: 1px solid #1a4a8a; border-radius: 4px;
      color: #e0e0e0; padding: 3px 7px; font-size: 0.85em; width: 100%;
      outline: none; }
    input:disabled { opacity: 0.7; cursor: not-allowed; }
    input:not(:disabled):focus { border-color: #00d4ff; }
    input[type="checkbox"] { accent-color: #00d4ff; width: 15px; height: 15px; }
    input[type="checkbox"]:disabled { cursor: not-allowed; opacity: 0.7; }

    /* Editable field wrapper */
    .field-edit-wrap { display: flex; align-items: center; gap: 6px; }
    .field-edit-wrap input[type="text"],
    .field-edit-wrap input[type="number"] { flex: 1; }

    /* Save button */
    .btn-save { background: #0d3a6e; border: 1px solid #1a6aaa; border-radius: 4px;
                color: #7ecfff; cursor: pointer; font-size: 0.78em; padding: 3px 10px;
                white-space: nowrap; flex-shrink: 0; }
    .btn-save:hover { background: #1a4a8a; }
    .btn-save:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Inline field status */
    .field-status { font-size: 0.78em; margin-top: 3px; }
    .field-status.ok  { color: #66bb6a; }
    .field-status.err { color: #ef5350; }

    /* Restart-required banner */
    #restart-banner { display: none; margin-bottom: 14px; padding: 10px 14px;
                      background: #1a2a4a; border: 1px solid #1a6aaa;
                      border-radius: 6px; color: #7ecfff; font-size: 0.85em; }
    #restart-banner strong { color: #00d4ff; }

    /* System-critical soft warning (tier B + restart_required) */
    .warn-critical { font-size: 0.75em; color: #ffb74d; margin-top: 3px; }

    /* Select control (enum fields like coder_fairy_policy / reviewer_fairy_policy) */
    select.cfg-select {
      background: #0d1b36; border: 1px solid #1a4a8a; border-radius: 4px;
      color: #e0e0e0; padding: 3px 7px; font-size: 0.85em; outline: none;
      cursor: pointer; }
    select.cfg-select:disabled { opacity: 0.7; cursor: not-allowed; }

    /* List display (read-only) */
    .list-display { list-style: none; padding: 0; }
    .list-display li { background: #0d1b36; border: 1px solid #1a4a8a; border-radius: 3px;
                        padding: 2px 7px; margin-bottom: 3px; font-size: 0.82em; color: #ccc; }
    .list-display li:empty { display: none; }
    .list-empty { color: #555; font-style: italic; font-size: 0.82em; }

    /* Null value */
    .null-value { color: #555; font-style: italic; }

    /* Tooltip */
    .tooltip-icon { color: #7ecfff; cursor: help; font-size: 0.75em;
                    border: 1px solid #7ecfff; border-radius: 50%; width: 14px; height: 14px;
                    display: inline-flex; align-items: center; justify-content: center;
                    flex-shrink: 0; position: relative; }
    .tooltip-icon:hover::after {
      content: attr(data-tip);
      position: absolute;
      background: #0d1b36; border: 1px solid #1a4a8a; border-radius: 4px;
      color: #e0e0e0; font-size: 0.9em; padding: 6px 10px; max-width: 280px;
      white-space: pre-wrap; z-index: 100; pointer-events: none;
    }

    /* Pulse animation for embedding.enabled == false */
    @keyframes alertPulse {
      0%   { border-color: #1a4a8a; }
      50%  { border-color: #c62828; }
      100% { border-color: #1a4a8a; }
    }
    .alert-pulse { animation: alertPulse 1.5s ease-in-out infinite; }

    /* Advanced expander */
    .advanced-expander { margin-top: 8px; }
    .advanced-toggle { background: none; border: 1px solid #1a4a8a; border-radius: 4px;
                        color: #7ecfff; cursor: pointer; font-size: 0.85em; padding: 6px 12px;
                        display: flex; align-items: center; gap: 6px; width: 100%; text-align: left; }
    .advanced-toggle:hover { background: #0d1b36; }
    .advanced-toggle .arrow { transition: transform 0.2s; display: inline-block; }
    .advanced-toggle.open .arrow { transform: rotate(90deg); }
    .advanced-body { display: none; margin-top: 10px; }
    .advanced-body.open { display: block; }

    /* Footer */
    .config-footer { margin-top: 24px; padding: 14px 16px; background: #16213e;
                      border: 1px solid #0f3460; border-radius: 6px; color: #888;
                      font-size: 0.8em; line-height: 1.6; }
    .config-footer a { color: #7ecfff; text-decoration: none; }
    .config-footer a:hover { text-decoration: underline; }

    /* Loading / error */
    #cfg-loading { color: #888; }
    #cfg-error   { color: #f44336; }
  </style>
</head>
<body>
  <!--NAV_PLACEHOLDER-->
  <h1>ENGRAM Config</h1>

  <div id="cfg-loading">Loading config…</div>
  <div id="cfg-error" style="display:none;"></div>
  <div id="restart-banner">
    <strong>&#x27F3; Restart needed</strong> — one or more changes take effect after you restart Claude Code / next session.
  </div>
  <div id="cfg-body" style="display:none;"></div>

  <div class="config-footer">
    ENGRAM has additional configuration parameters for protocol-level
    tuning that aren&#39;t surfaced here. They live in ~/.engram/config.json
    and are documented in the project <a href="https://github.com/engram-agents/engram/blob/main/README.md" target="_blank">README</a>. Edit them only after
    reading the calibration documentation.
  </div>

  <script>
  function _currentAgent() {
    return new URL(window.location.href).searchParams.get('agent');
  }
  function _withAgent(path) {
    const a = _currentAgent();
    if (!a) return path;
    return path + (path.includes('?') ? '&' : '?') + 'agent=' + encodeURIComponent(a);
  }
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Agent switching is handled by the shared top-nav selector (_NAV_SCRIPT);
  // no per-tab selector wiring needed here.

  // ── Config write (saveField) ─────────────────────────────────────────────
  async function saveField(key, value) {
    try {
      const res = await fetch(_withAgent('/api/config/' + encodeURIComponent(key)), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: value }),
      });
      const data = await res.json();
      if (!data.ok) {
        console.error('saveField error:', data.error);
        alert('Save failed: ' + (data.error || 'unknown error'));
      }
    } catch (e) {
      console.error('saveField fetch error:', e);
      alert('Save failed: ' + e.message);
    }
  }

  // ── Rendering helpers ────────────────────────────────────────────────────

  // Returns the input element HTML for a field. When editable=true the
  // control is interactive; when editable=false it is disabled/read-only.
  function renderControl(row) {
    const v = row.value;
    const ctrl = row.control;
    const editable = row.editable !== false;  // undefined → treat as true (belt-and-suspenders)
    const alertPulse = (row.alert_when_false === true && v === false) ? ' alert-pulse' : '';
    const key = row.key;

    if (ctrl === 'checkbox') {
      const checked = v ? 'checked' : '';
      if (!editable) {
        return `<input type="checkbox" ${checked} disabled class="${alertPulse.trim()}">`;
      }
      // Editable checkbox: inline save-on-change (no explicit save button needed).
      // updateDependsOn() fires unconditionally on every change so gated rows
      // (e.g. auto_sleep_time under auto_sleep_enabled) show/hide immediately,
      // even if the PUT fails.
      return `<input type="checkbox" id="field-${esc(key)}" ${checked} class="${alertPulse.trim()}"
                onchange="updateSectionGates(); updateDependsOn(); saveField('${esc(key)}', this.checked)">`;
    }
    if (ctrl === 'text') {
      if (!editable) {
        if (v === null || v === undefined) {
          return `<span class="null-value">(not set)</span>`;
        }
        return `<input type="text" value="${esc(String(v))}" disabled class="${alertPulse.trim()}">`;
      }
      const val = (v === null || v === undefined) ? '' : esc(String(v));
      return `<div class="field-edit-wrap">
        <input type="text" id="field-${esc(key)}" value="${val}"
               class="${alertPulse.trim()}"
               onkeydown="if(event.key==='Enter')saveField('${esc(key)}',this.value)">
        <button class="btn-save" onclick="saveFieldFromInput('${esc(key)}')">Save</button>
      </div>`;
    }
    if (ctrl === 'int' || ctrl === 'float') {
      if (!editable) {
        if (v === null || v === undefined) {
          return `<span class="null-value">(not set — using code default)</span>`;
        }
        return `<input type="number" value="${esc(String(v))}" disabled class="${alertPulse.trim()}">`;
      }
      const val = (v === null || v === undefined) ? '' : esc(String(v));
      // When unset, surface the effective code default as a placeholder so the
      // box isn't a blank with no hint of what's active (config_schema `default`).
      // Scope: this lives in the int/float branch only — the sole schema field
      // with a `default` today is numeric. A future text field with a `default`
      // would need the same line added to the text branch above.
      const ph = (val === '' && row.default !== null && row.default !== undefined)
        ? ` placeholder="${esc(String(row.default))} (default)"` : '';
      const step = (ctrl === 'float') ? 'any' : '1';
      return `<div class="field-edit-wrap">
        <input type="number" id="field-${esc(key)}" value="${val}"${ph} step="${step}"
               class="${alertPulse.trim()}"
               onkeydown="if(event.key==='Enter')saveFieldFromInput('${esc(key)}')">
        <button class="btn-save" onclick="saveFieldFromInput('${esc(key)}')">Save</button>
      </div>`;
    }
    if (ctrl === 'time_hm') {
      // Parse current HH:MM value; default to 03:00 if missing.
      const raw = (v === null || v === undefined || v === '') ? '03:00' : String(v);
      const parts = raw.split(':');
      const curHH = (parts[0] || '03').padStart(2, '0');
      const curMM = (parts[1] || '00').padStart(2, '0');

      // Build hour options 00–23
      let hourOpts = '';
      for (let h = 0; h < 24; h++) {
        const hh = String(h).padStart(2, '0');
        hourOpts += `<option value="${hh}"${hh === curHH ? ' selected' : ''}>${hh}</option>`;
      }
      // Build minute options 00–59
      let minOpts = '';
      for (let m = 0; m < 60; m++) {
        const mm = String(m).padStart(2, '0');
        minOpts += `<option value="${mm}"${mm === curMM ? ' selected' : ''}>${mm}</option>`;
      }

      const disabledAttr = editable ? '' : ' disabled';
      const onChange = editable
        ? `onchange="(function(){var hSel=this.closest('.field-edit-wrap').querySelector('.time-hm-hour');var mSel=this.closest('.field-edit-wrap').querySelector('.time-hm-min');saveField('${esc(key)}',hSel.value+':'+mSel.value);}).call(this)"`
        : '';
      return `<div class="field-edit-wrap">
        <select class="time-hm-hour"${disabledAttr} ${onChange}>${hourOpts}</select>
        <span style="margin:0 2px;">:</span>
        <select class="time-hm-min"${disabledAttr} ${onChange}>${minOpts}</select>
      </div>`;
    }
    // list_display (read-only list) and list_edit (editable in v0 as read-only list — full
    // list editor is deferred; v0 shows the current values and a note)
    if (ctrl === 'list_display' || ctrl === 'list_edit') {
      if (v === null || v === undefined || (Array.isArray(v) && v.length === 0)) {
        return `<span class="list-empty">(empty)</span>`;
      }
      const items = Array.isArray(v) ? v : [v];
      return '<ul class="list-display">' + items.map(item => `<li>${esc(String(item))}</li>`).join('') + '</ul>';
    }
    if (ctrl === 'yellow_domains_edit') {
      if (v === null || v === undefined || (Array.isArray(v) && v.length === 0)) {
        return `<span class="list-empty">(empty)</span>`;
      }
      const items = Array.isArray(v) ? v : [v];
      return '<ul class="list-display">' + items.map(item => {
        const domain = esc(item.domain || item);
        const reason = item.reason ? ` — ${esc(item.reason)}` : '';
        const node = item.engram_node ? ` [${esc(item.engram_node)}]` : '';
        return `<li>${domain}${reason}${node}</li>`;
      }).join('') + '</ul>';
    }
    if (ctrl === 'select') {
      const editable = row.editable !== false;
      const opts = (row.allowed_values || []).map(opt =>
        `<option value="${esc(opt)}" ${opt === v ? 'selected' : ''}>${esc(opt)}</option>`
      ).join('');
      if (!editable) {
        return `<select disabled class="cfg-select${alertPulse}">${opts}</select>`;
      }
      const key = row.key;
      return `<select id="field-${esc(key)}" class="cfg-select${alertPulse}"
                onchange="saveField('${esc(key)}', this.value)">${opts}</select>`;
    }
    // fallback (read-only)
    if (v === null || v === undefined) return `<span class="null-value">(not set)</span>`;
    return `<input type="text" value="${esc(String(v))}" disabled>`;
  }

  function renderRow(row, isGated) {
    const editable = row.editable !== false;
    const badges = [];
    if (!editable) {
      badges.push(`<span class="badge badge-readonly" title="Managed elsewhere (set at install time / by agentctl, or read-only by design) — not editable here">&#128274; read-only</span>`);
    }
    if (row.restart_required) {
      badges.push(`<span class="badge badge-restart" title="Restart needed after this change">&#x27F3; restart needed</span>`);
    }
    if (row.uses_legacy_path) {
      const legPath = esc(row.value_source || '');
      badges.push(`<span class="badge badge-legacy" title="Currently stored at ${legPath}; will canonicalize on next migration.">&#9888; legacy key</span>`);
    }
    const badgeHtml = badges.length ? `<div class="row-badges">${badges.join('')}</div>` : '';
    const tipHtml = row.tooltip ? `<span class="tooltip-icon" data-tip="${esc(row.tooltip)}">?</span>` : '';
    const gateRowClass = row.gates_section ? ' gate-row' : '';

    // Soft warning for system-critical fields (tier B + restart_required + editable)
    const critWarnHtml = (row.tier === 'advanced' && row.restart_required && editable)
      ? `<div class="warn-critical">&#9888; Calibration-level setting — change with care.</div>`
      : '';

    // depends_on: hide row if the gate field is currently false
    // We tag the row with data-depends-on so JS can toggle visibility.
    const dependsAttr = row.depends_on ? ` data-depends-on="${esc(row.depends_on)}"` : '';

    // Status placeholder for save feedback
    const statusHtml = editable && row.control !== 'list_display' && row.control !== 'list_edit'
      && row.control !== 'yellow_domains_edit'
      ? `<div class="field-status" id="status-${esc(row.key)}"></div>`
      : '';

    return `<div class="config-row${gateRowClass}" id="row-${esc(row.key)}"${dependsAttr}>
      <div class="row-label">${esc(row.label)}${tipHtml}</div>
      <div class="row-control">${renderControl(row)}${badgeHtml}${critWarnHtml}${statusHtml}</div>
    </div>`;
  }

  function renderSection(sec) {
    const gated = sec.section_gate && !sec.gate_open;
    const bodyClass = gated ? ' section-gated' : '';
    const gateAttr = sec.section_gate ? ` data-section-gate="${esc(sec.section_gate)}"` : '';
    const rowsHtml = sec.rows.map(r => renderRow(r, gated)).join('');
    return `<div class="section-card${bodyClass}" id="sec-${esc(sec.id)}"${gateAttr}>
      <h3>${esc(sec.label)}</h3>
      ${rowsHtml}
    </div>`;
  }

  // ── Main load ────────────────────────────────────────────────────────────
  async function loadConfig() {
    document.getElementById('cfg-loading').style.display = '';
    document.getElementById('cfg-body').style.display = 'none';
    document.getElementById('cfg-error').style.display = 'none';
    try {
      const res = await fetch(_withAgent('/api/config'));
      const data = await res.json();
      document.getElementById('cfg-loading').style.display = 'none';

      if (data.error && !data.annotated) {
        document.getElementById('cfg-error').textContent = 'Error: ' + data.error;
        document.getElementById('cfg-error').style.display = '';
        return;
      }

      const body = document.getElementById('cfg-body');
      body.style.display = '';

      if (!data.annotated || !data.annotated.sections) {
        body.innerHTML = '<p style="color:#888;">No config data available.</p>';
        return;
      }

      const sections = data.annotated.sections;
      const tierA = sections.filter(s => s.tier === 'daily');
      const tierB = sections.filter(s => s.tier === 'advanced');

      let html = '';

      // Tier A sections
      tierA.forEach(sec => { html += renderSection(sec); });

      // Tier B — wrapped in advanced expander
      if (tierB.length > 0) {
        const advBody = tierB.map(sec => renderSection(sec)).join('');
        html += `<div class="advanced-expander">
          <button class="advanced-toggle" id="adv-toggle" onclick="toggleAdvanced()">
            <span class="arrow">&#9658;</span>
            Advanced (calibration-aware tuning)
          </button>
          <div class="advanced-body" id="adv-body">${advBody}</div>
        </div>`;
      }

      body.innerHTML = html;

      // Apply depends_on visibility rules and section-gate greying immediately after render
      updateGates();

      // Restore advanced expander state from localStorage
      const advExpanded = localStorage.getItem('engram_config_advanced_expanded') === 'true';
      if (advExpanded) {
        const toggle = document.getElementById('adv-toggle');
        const advBody = document.getElementById('adv-body');
        if (toggle && advBody) {
          toggle.classList.add('open');
          advBody.classList.add('open');
        }
      }
    } catch (e) {
      document.getElementById('cfg-loading').style.display = 'none';
      document.getElementById('cfg-error').textContent = 'Failed to load config: ' + e.message;
      document.getElementById('cfg-error').style.display = '';
    }
  }

  function toggleAdvanced() {
    const toggle = document.getElementById('adv-toggle');
    const body = document.getElementById('adv-body');
    if (!toggle || !body) return;
    const isOpen = toggle.classList.toggle('open');
    body.classList.toggle('open', isOpen);
    localStorage.setItem('engram_config_advanced_expanded', isOpen ? 'true' : 'false');
  }

  // ── depends_on: show/hide rows gated on a boolean field ─────────────────
  // Called after initial render and whenever a checkbox that gates_section
  // or is referenced as depends_on changes.
  function updateDependsOn() {
    document.querySelectorAll('[data-depends-on]').forEach(rowEl => {
      const gateKey = rowEl.getAttribute('data-depends-on');
      const gateInput = document.getElementById('field-' + gateKey);
      if (!gateInput) return;  // gate field not rendered — leave row as-is
      const gateValue = gateInput.checked;
      rowEl.style.display = gateValue ? '' : 'none';
    });
  }

  // ── section-gate: grey/ungrey section cards whose gate checkbox toggled ──
  // Reads the current checked state of each section's gate checkbox and
  // adds/removes .section-gated on the card live, matching the initial
  // server-side render (gated when unchecked, normal when checked).
  function updateSectionGates() {
    document.querySelectorAll('[data-section-gate]').forEach(cardEl => {
      const gateKey = cardEl.getAttribute('data-section-gate');
      const gateInput = document.getElementById('field-' + gateKey);
      if (!gateInput) return;  // gate field not rendered — leave card as-is
      if (gateInput.checked) {
        cardEl.classList.remove('section-gated');
      } else {
        cardEl.classList.add('section-gated');
      }
    });
  }

  // Convenience wrapper: call both gate-update passes together.
  function updateGates() {
    updateDependsOn();
    updateSectionGates();
  }

  // ── Field save ───────────────────────────────────────────────────────────
  // saveField(key, value) — called directly from checkbox onchange and from
  // saveFieldFromInput (text/number Save button or Enter press).
  async function saveField(key, value) {
    const statusEl = document.getElementById('status-' + key);
    const btn = document.querySelector(`[onclick="saveFieldFromInput('${key}')"]`);
    if (statusEl) { statusEl.textContent = 'Saving…'; statusEl.className = 'field-status'; }
    if (btn) btn.disabled = true;

    try {
      const res = await fetch(_withAgent('/api/config/' + encodeURIComponent(key)), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value }),
      });
      const data = await res.json();

      if (data.ok) {
        // Update displayed value to validated_value (server may coerce types)
        const inputEl = document.getElementById('field-' + key);
        if (inputEl && inputEl.type !== 'checkbox' && data.validated_value !== undefined) {
          inputEl.value = data.validated_value;
        }
        if (statusEl) {
          statusEl.textContent = 'Saved';
          statusEl.className = 'field-status ok';
          setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 3000);
        }
        // Show restart banner if required
        if (data.restart_required) {
          document.getElementById('restart-banner').style.display = '';
        }
        // Update depends_on visibility and section-gate greying in case the saved key is a gate
        updateGates();
      } else {
        const msg = data.error || 'Save failed';
        if (statusEl) {
          statusEl.textContent = msg;
          statusEl.className = 'field-status err';
        }
      }
    } catch (e) {
      if (statusEl) {
        statusEl.textContent = 'Network error: ' + e.message;
        statusEl.className = 'field-status err';
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // Called by text/number Save button (or Enter press) — reads the input value,
  // coerces numeric fields to Number, then delegates to saveField.
  function saveFieldFromInput(key) {
    const inputEl = document.getElementById('field-' + key);
    if (!inputEl) return;
    if (inputEl.type === 'number') {
      const n = Number(inputEl.value);
      if (Number.isNaN(n)) {
        const statusEl = document.getElementById('status-' + key);
        if (statusEl) {
          statusEl.textContent = 'Invalid number — please enter a numeric value.';
          statusEl.className = 'field-status err';
        }
        return;
      }
      saveField(key, n);
    } else {
      saveField(key, inputEl.value);
    }
  }

  loadConfig();
  </script>
<!--NAV_SCRIPT_PLACEHOLDER-->
</body>
</html>""".replace(_NAV_PLACEHOLDER, _render_nav("config")).replace(
    _NAV_SCRIPT_PLACEHOLDER, _NAV_SCRIPT
).replace(_NAV_CSS_PLACEHOLDER, _NAV_SHARED_CSS)


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
        if path == "/config":
            self._html(CONFIG_HTML)
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
        elif path == "/api/schema":
            # Schema is static (no DB required) — return the SSoT dict built
            # from engram_core / engram_query / engram_stats constants.
            self._json(get_schema_data())
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
                            "agent": agent_name}, status=404)
            else:
                resp = get_health_data(db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/stats":
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                index_db_path = _resolve_logs_index(agent_name)
                resp = get_stats_data(index_db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/calibration":
            if not db_path:
                self._json({"error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                resp = get_calibration_data(db_path)
                resp["agent"] = agent_name
                self._json(resp)
        elif path == "/api/config":
            resp = get_config_data(agent_name)
            self._json(resp)
        elif path == "/api/search":
            if not db_path:
                self._json({"results": [], "error": f"Unknown agent: {agent_name!r}",
                            "agent": agent_name}, status=404)
            else:
                qs = parse_qs(parsed.query or "")
                query = (qs.get("q", [None])[0] or "").strip()
                if not query:
                    self._json({"results": [], "error": "q parameter is required",
                                "agent": agent_name}, status=400)
                else:
                    resp = search_nodes(db_path, query)
                    resp["agent"] = agent_name
                    self._json(resp)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        """CORS preflight — needed for PUT from a browser on a different origin."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_PUT(self):
        """PUT /api/config/<key>?agent=<name>

        Request body: JSON {"value": <new_value>}
        Response (200 on success):
          {"ok": true, "restart_required": bool, "validated_value": <val>}
        Response (400 on validation error / unknown key):
          {"ok": false, "error": "<msg>"}
        Response (404 on path mismatch):
          {"ok": false, "error": "not found"}
        """
        parsed = urlparse(self.path)
        path = parsed.path

        # Route: /api/config/<key>  (key may be dotted, e.g. cadence.drowsiness_caution_pct)
        _PREFIX = "/api/config/"
        if not path.startswith(_PREFIX):
            self._json({"ok": False, "error": "not found"}, status=404)
            return

        key = path[len(_PREFIX):]
        if not key:
            self._json({"ok": False, "error": "key required in path"}, status=400)
            return

        # Parse request body.
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        if content_length == 0:
            self._json({"ok": False, "error": "request body required"}, status=400)
            return
        try:
            body_bytes = self.rfile.read(content_length)
            body = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError) as exc:
            self._json({"ok": False, "error": f"invalid JSON body: {exc}"}, status=400)
            return

        if "value" not in body:
            self._json({"ok": False, "error": "'value' field required in body"}, status=400)
            return

        # Resolve agent — same as GET.
        agent_name, _db_path = _resolve_agent(parsed.query)

        try:
            result = write_config_key(agent_name, key, body["value"])
        except OSError:
            raise
        except Exception as exc:
            self._json(
                {"ok": False, "error": f"internal error: {exc}"}, status=500
            )
            return
        status = 200 if result.get("ok") else 400
        self._json(result, status=status)

    def _json(self, data: dict, status: int = 200):
        """Send JSON response. status defaults to 200 (preserves the historical
        no-status callsite shape); pass an explicit status for error responses."""
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
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
                        help="Optional config JSON: override labels/default/exclude "
                             "for auto-detected agents, or add agents with explicit db. "
                             "Without --no-autodetect, auto-detect is always the base.")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="Address to bind to (default: 127.0.0.1)")
    parser.add_argument("--no-autodetect", action="store_true",
                        help="Disable auto-discovery; use only --config or --db.")
    args = parser.parse_args()

    if args.config and args.db:
        parser.error("--config and --db are mutually exclusive")

    global AGENTS, DEFAULT_AGENT, _AGENT_STARTUP_MODE, _AGENT_STARTUP_CONFIG, _AGENT_CACHE_TS

    if args.db:
        # Explicit --db always forces single-agent mode (unchanged behavior).
        db = os.path.expanduser(args.db)
        if os.path.isdir(db):
            db = os.path.join(db, "knowledge.db")
        AGENTS = {"default": {"name": "default", "label": "Default", "db": db}}
        DEFAULT_AGENT = "default"
        _AGENT_STARTUP_MODE = "single_db"
    elif args.no_autodetect:
        # Opt-out: explicit-config-only path (old behavior).
        if args.config:
            AGENTS, DEFAULT_AGENT = _load_config(args.config)
            _AGENT_STARTUP_MODE = "config_only"
            _AGENT_STARTUP_CONFIG = args.config
        else:
            db = DEFAULT_DB
            AGENTS = {"default": {"name": "default", "label": "Default", "db": db}}
            DEFAULT_AGENT = "default"
            _AGENT_STARTUP_MODE = "single_db"
    else:
        # Default path: auto-detect is the base.
        discovered = discover_agents()
        if args.config:
            # Layer explicit config on top of discovered.
            AGENTS, DEFAULT_AGENT = _merge_config_over_discovered(discovered, args.config)
            _AGENT_STARTUP_MODE = "discover_merge"
            _AGENT_STARTUP_CONFIG = args.config
        elif discovered:
            # Pure auto-detect: prefer the launching context's own agent as
            # default; fall back to alphabetically first for determinism.
            AGENTS = discovered
            DEFAULT_AGENT = _pick_default(AGENTS)
            _AGENT_STARTUP_MODE = "discover_only"
        else:
            # Nothing discovered and no config — fall back to single-agent default.
            db = DEFAULT_DB
            AGENTS = {"default": {"name": "default", "label": "Default", "db": db}}
            DEFAULT_AGENT = "default"
            _AGENT_STARTUP_MODE = "discover_fallback"

    # Seed the live-refresh cache timestamp so the first request is a cache hit.
    _AGENT_CACHE_TS = time.monotonic()

    HTTPServer.allow_reuse_address = True
    bind = args.bind or "127.0.0.1"
    server = HTTPServer((bind, args.port), Handler)
    print(f"ENGRAM Visualizer →  http://localhost:{args.port}")
    print(f"Health Dashboard  →  http://localhost:{args.port}/health")
    print(f"Config tab        →  http://localhost:{args.port}/config")
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
