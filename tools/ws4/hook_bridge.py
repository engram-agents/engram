"""Bridge into the lesson-tripwire hook's own matching primitives.

The O2 "transfer" computation (tools/ws4/analyze.py) must use the EXACT same
situation-matching machinery the hook used at fire time -- never a second,
independently written copy that could silently drift from it (spec:
ws4-adjudication-kit.md, "O2 situation" paragraph. "Import build_match_target
from src/engram/hooks/claude/engram-lesson-tripwire-hook.py by path
(importlib), do not reimplement it.").

This module only ever READS the hook file (via importlib, by path) and never
modifies it -- the DO-NOT list explicitly allows the import while forbidding
any edit to the hook itself.
"""
from __future__ import annotations

import importlib.util
import re
import sqlite3
from pathlib import Path

_HOOK_MODULE_NAME = "ws4_lesson_tripwire_hook_bridge"
_hook_module_cache = None


def _default_hook_path() -> Path:
    # tools/ws4/hook_bridge.py -> repo root is two parents up from tools/ws4/.
    repo_root = Path(__file__).resolve().parent.parent.parent
    return repo_root / "src" / "engram" / "hooks" / "claude" / "engram-lesson-tripwire-hook.py"


def load_hook_module(hook_path: "Path | None" = None):
    """Import the hook module by file path (importlib) -- never re-implement
    build_match_target or the situation_pattern query. Memoized per process.

    Escape valve: raises loudly (FileNotFoundError / AttributeError) if the
    hook file is missing or doesn't expose the two symbols this kit relies
    on -- per the spec's own escape valve ("If ... the hook import doesn't
    match this spec, STOP and report. Don't improvise."), a caller hitting
    this exception should stop and report rather than fall back to a
    reimplementation.
    """
    global _hook_module_cache
    if _hook_module_cache is not None and hook_path is None:
        return _hook_module_cache
    path = hook_path or _default_hook_path()
    if not path.exists():
        raise FileNotFoundError(
            f"WS4 adjudication kit escape valve: lesson-tripwire hook not "
            f"found at {path}. Do not reimplement build_match_target -- "
            f"stop and report."
        )
    spec = importlib.util.spec_from_file_location(_HOOK_MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "build_match_target"):
        raise AttributeError(
            "WS4 adjudication kit escape valve: hook module has no "
            "build_match_target -- stop and report, do not reimplement."
        )
    if not hasattr(module, "_QUERY"):
        raise AttributeError(
            "WS4 adjudication kit escape valve: hook module has no _QUERY "
            "-- stop and report, do not reimplement the situation_pattern "
            "lookup."
        )
    if hook_path is None:
        _hook_module_cache = module
    return module


def build_match_target(hook_input: dict, hook_path: "Path | None" = None) -> str:
    """Thin pass-through to the hook's own build_match_target (imported by
    path, never reimplemented)."""
    module = load_hook_module(hook_path)
    return module.build_match_target(hook_input)


def load_situation_patterns(db_path, hook_path: "Path | None" = None) -> dict:
    """id -> situation_pattern, read via the hook's OWN _QUERY (same JSON
    path the hook's _QUERY uses -- spec's O2 paragraph), opened read-only.

    Returns {} on any DB problem (missing file, locked, malformed) -- this
    kit is advisory/offline tooling, not the hook itself, so a DB problem is
    reported by the caller's own denominators rather than raised here.
    """
    module = load_hook_module(hook_path)
    query = module._QUERY
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(query).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    # _QUERY's SELECT order is (id, nudge, pattern) -- see the hook module.
    return {row[0]: row[2] for row in rows if row[2]}


def situation_pattern_matches(pattern: "str | None", match_target: str) -> bool:
    """The same one-line match test check_command() in the hook uses
    (re.search(pattern, match_target, re.IGNORECASE)) -- reused directly
    here rather than routed through check_command() itself, since
    check_command()'s own contract is "return hits for a list of
    tripwires", not "does this one pattern match this one target"; this is
    the minimal shared primitive, not a second copy of any decision logic.
    A malformed pattern (re.error) is treated as no-match, matching the
    hook's own check_command() behavior ("malformed pattern -- skip
    silently")."""
    if not pattern:
        return False
    try:
        return re.search(pattern, match_target, re.IGNORECASE) is not None
    except re.error:
        return False
