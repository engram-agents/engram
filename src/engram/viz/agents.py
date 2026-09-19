"""viz.agents — agent discovery, resolution, and config-merge
(engram-alpha#2555 PR A, pure relocation of viz_server.py's agent-registry
functions, byte-identical function bodies).

Owns the package-wide mutable agent registry: ``AGENTS`` and
``DEFAULT_AGENT`` are REASSIGNED (not mutated in place) by
``_refresh_agents_if_stale`` below and by ``viz.server.main`` at
startup. Every OTHER module that reads them must do so via attribute
access on this module (``import viz.agents as viz_agents`` then
``viz_agents.AGENTS`` / ``viz_agents.DEFAULT_AGENT``) -- NEVER
``from viz.agents import AGENTS``, which would bind a reference to
whatever dict object existed at import time and go stale the moment
this module reassigns its own module-level name. See data/stats.py's
module docstring for a concrete instance of this hazard, found while
splitting this file.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs

# Renamed from the original "viz_server.discover" to match this module's
# new home -- a diagnostic-only name (log filtering), not part of the
# byte-identical served-output contract this PR's acceptance gates check.
_disc_log = logging.getLogger("viz.agents.discover")

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


def configure_agents_from_args(args) -> None:
    """Resolve AGENTS/DEFAULT_AGENT/_AGENT_STARTUP_MODE/_AGENT_STARTUP_CONFIG
    from parsed CLI args, and seed _AGENT_CACHE_TS so the first live-refresh
    check is a cache hit (engram-alpha#2754 PR B).

    Extracted out of ``viz.server.main()`` (where this exact ~50-line block
    originally lived, byte-identical in behavior) so BOTH ``viz.server``
    (``:5001``) and ``viz.workbench`` (``:5021``, the new standalone Curve
    Workbench entry point this same PR adds) resolve the IDENTICAL agent
    set from the same flags -- the issue's own words: "against the same
    operator config." A hand-copied second implementation would drift the
    moment one caller's resolution logic changed and the other's didn't.

    *args* is any object exposing ``.db``, ``.config``, ``.no_autodetect``
    attributes matching this module's ``--db``/``--config``/
    ``--no-autodetect`` argparse flags (an ``argparse.Namespace`` from
    either caller's own parser satisfies this structurally -- no shared
    parser object is required). Callers remain responsible for their OWN
    ``--config``/``--db`` mutual-exclusivity validation via their own
    ``parser.error(...)`` before calling this (that check needs the
    caller's own parser instance for its usage message, so it isn't
    extracted here).

    Mutates THIS module's own AGENTS/DEFAULT_AGENT/etc. via ``global``
    assignment -- never attribute-assignment on some OTHER module object.
    This is the same hazard this module's own docstring already documents
    for every reader of AGENTS/DEFAULT_AGENT: a caller reassigns via
    ``viz_agents.AGENTS = ...`` (attribute access on the live module
    object); this function, being IN the module, reassigns via ``global``
    instead -- both forms rebind the SAME module attribute, so every other
    module's ``viz_agents.AGENTS`` read sees the update either way.
    """
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

