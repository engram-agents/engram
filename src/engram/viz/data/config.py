"""viz.data.config — config read/write data assembly for /api/config and
PUT /api/config/<key> (engram-alpha#2555 PR A, pure relocation of
viz_server.py's config functions, byte-identical function bodies except
one deliberate AGENTS-reference fix, noted below).

No HTTP, no HTML -- unit-testable in isolation.

CROSS-MODULE NOTE (same hazard as data/stats.py, see that module's own
docstring for the full account): ``_resolve_agent_config`` and
``_resolve_config_path`` read the package-wide ``AGENTS`` registry,
which lives in ``viz.agents`` and is REASSIGNED there — so this module
uses ``viz_agents.AGENTS`` (attribute access), never a bound
``from viz.agents import AGENTS``.

sys.path NOTE (project CLAUDE.md's documented hazard class: "any code
that resolves a sibling/companion path at runtime MUST NOT assume the
source layout" — a fixed ``Path(__file__).resolve().parents[N]``
calibrated against one nesting depth silently overshoots at another).
The original ``viz_server.py`` sat directly under ``src/engram/`` (one
level from ``tools/``'s parent); this module sits at
``src/engram/viz/data/`` (two levels deeper). A fixed ``parents[N]``
copied from the original file's depth would resolve to the WRONG
directory. Uses a walk-parents search instead — the same pattern
``engram-baton-prompt-hook.py``/``engram-forum-prompt-hook.py`` already
establish for this exact class of problem — so it is correct at either
nesting depth without needing to know which one it's running at.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import agents as viz_agents


def _find_tools_parent(start: Path) -> Path | None:
    """Walk upward from *start* to find the nearest ancestor directory
    that itself contains a ``tools/config_schema.py`` file — the
    directory that needs to go on sys.path for
    ``from tools.config_schema import ...`` to resolve. Works whether
    this module is running from the source tree
    (``src/engram/viz/data/config.py``, where the target is
    ``src/engram/``) or a built/deployed plugin tree
    (``plugin_root/viz/data/config.py``, where the target is
    ``plugin_root/``) — both have ``tools/config_schema.py`` at the
    SAME relative depth from this file (three ``..`` up), but computing
    that depth by counting is exactly the "fixed parents[N]" mistake
    this function exists to avoid if the tree is ever restructured
    again. Returns None if not found, OR if every candidate directory is
    unreadable -- Path.is_file() raises OSError (not False) in that
    case, and this function runs at bare module-import time, so the
    walk must never raise (same never-raises shape as
    _hooklib.resolve_runtime_dir, engram-alpha#2671). Guards PER
    CANDIDATE, not the whole walk in one try/except: if the nearest
    candidate happens to be the unreadable one, a whole-walk guard
    would abort before ever checking a further, normally-readable
    ancestor -- discarding a candidate that would have resolved
    (engram-alpha#2671 review round). Caller degrades gracefully
    either way, same as the original file's own try/except ImportError
    contract.
    """
    for candidate in [start, *start.parents]:
        try:
            found = (candidate / "tools" / "config_schema.py").is_file()
        except OSError:
            continue
        if found:
            return candidate
    return None


_TOOLS_PARENT = _find_tools_parent(Path(__file__).resolve())
if _TOOLS_PARENT is not None and str(_TOOLS_PARENT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_PARENT))
try:
    from tools.config_schema import annotate_schema, SCHEMA as _CONFIG_SCHEMA  # noqa: E402
    _CONFIG_SCHEMA_AVAILABLE = True
except ImportError:
    _CONFIG_SCHEMA_AVAILABLE = False
    annotate_schema = None  # type: ignore[assignment]
    _CONFIG_SCHEMA = []  # type: ignore[assignment]


# Populated on first write per agent per server process: agent_name -> backup
# path. MUTATED in place (never reassigned), so — unlike AGENTS/DEFAULT_AGENT
# — this is safe as an ordinary module-level dict with no cross-module
# attribute-access requirement; nothing outside this module reads it anyway.
_CONFIG_WRITE_BACKUPS: dict = {}

# Serializes write_config_key()'s read-modify-write-verify sequence.
# ThreadingHTTPServer (server.py) gives one thread per connection, so two
# concurrent PUT /api/config/<key> requests can otherwise interleave their
# load-mutate-write-verify steps and silently lose one update or leave
# config.json torn (engram-alpha#2926 round-1 review). Module-level so any
# future in-process caller (e.g. workbench.py) inherits the same guarantee.
_WRITE_LOCK = threading.Lock()


def _schema_index() -> dict:
    """Build key → schema_row dict for fast lookup. Returns {} if unavailable."""
    return {row["key"]: row for row in _CONFIG_SCHEMA}


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
    agent_meta = viz_agents.AGENTS.get(agent_name)
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
    agent_meta = viz_agents.AGENTS.get(agent_name)
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
    - The load-mutate-write-verify sequence is serialized in-process by
      ``_WRITE_LOCK``, so two concurrent PUTs handled by ThreadingHTTPServer
      cannot interleave and lose an update or tear the file. This does NOT
      cover a SEPARATE PROCESS editing config.json directly (e.g. a human
      hand-editing the file, or a second server instance) — that path is
      unserialized, as it always was.
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

    # Everything from here through the post-verify backup refresh touches
    # shared on-disk state (config.json, _CONFIG_WRITE_BACKUPS) and must be
    # serialized against concurrent callers (see _WRITE_LOCK's docstring).
    with _WRITE_LOCK:
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

        # In-place write: copy validated content into config.json's EXISTING
        # inode rather than os.rename(temp, config.json). os.rename swaps in
        # the temp file's inode, which carries the temp's owner + mode
        # (operator UID, 0600 from mkstemp) — when the viz server runs as a
        # different UID than the agent that owns config.json, that strips the
        # agent's ownership and locks it out of its own config (GitHub #372).
        # Building in a temp first keeps a serialization error from
        # truncating the live file; the copy into the existing inode is not
        # crash-atomic, so the backup + read-after-write verify + rollback
        # below cover a torn write. Second tradeoff vs os.rename (which is
        # atomic at the VFS layer): a concurrent reader can transiently
        # observe a partially-written config.json and get a JSONDecodeError.
        # The copy window is sub-millisecond for typical config sizes and
        # config is read at startup, not continuously, so this is not a
        # real-world concern.
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

        # After verify success: refresh backup so any later same-session
        # rollback lands on last-good (this written value) rather than
        # session-start.
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
