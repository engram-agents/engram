"""Shared helpers of the model-family gate's two hooks (#4596): ``engram-model-gate-pretool.py`` (the write
block) and ``engram-model-gate-prompt.py`` (the next-turn warning).  Not a hook: it registers nothing.

Holds what both hooks must agree on: how ``self_lineage`` is read from ``$ENGRAM_HOME/config.json`` (and the
four states that read can end in), and how the runtime siblings (``engram_core``, ``engram_signature``, the
generated ``model_gate_write_tools.json``) are found.  Stdlib plus ``_hooklib`` only: a dormant seat must not
pay for an ``engram_core`` import just to learn it is dormant.

Siblings are found via ``$CLAUDE_PLUGIN_ROOT`` first, then ``_hooklib.bounded_parents`` from the calling file,
never above the tree root and never a fixed ``parents[N]`` (the build flattens ``hooks/claude`` to ``hooks``,
#3983).
"""
import json
import os
import re
import stat
from pathlib import Path

from _hooklib import bounded_parents

# Copy of engram_core._LINEAGE_RE (the `provider:family` shape of self_lineage), kept here only so the hooks
# can decide dormancy and malformedness WITHOUT importing engram_core.  tests/test_model_gate_4596.py pins
# the two patterns equal; if the source pattern changes, change this one too.
_LINEAGE_RE = re.compile(r"[a-z0-9_-]+:[a-z0-9._-]+")

DORMANT = "dormant"
OK = "ok"
CONFIG_UNREADABLE = "config_unreadable"
SELF_LINEAGE_MALFORMED = "self_lineage_malformed"


def config_state(home):
    """``(state, self_lineage)`` from ``<home>/config.json``.

    * ``dormant``: no config file, or ``self_lineage`` absent, null, blank (the gate is not set up);
    * ``config_unreadable``: the file exists but cannot be read, is not JSON, or is not a JSON object;
    * ``self_lineage_malformed``: present but not a string of the ``provider:family`` shape;
    * ``ok``: the stripped, valid value is returned.
    Never raises."""
    try:
        path = os.path.join(os.fspath(home), "config.json")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except (FileNotFoundError, NotADirectoryError):
            return DORMANT, ""
        except OSError:
            return CONFIG_UNREADABLE, ""
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return CONFIG_UNREADABLE, ""
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                fd = -1
                cfg = json.load(fh)
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(cfg, dict):
            return CONFIG_UNREADABLE, ""
        val = cfg.get("self_lineage")
        if val is None:
            return DORMANT, ""
        if not isinstance(val, str):
            return SELF_LINEAGE_MALFORMED, ""
        val = val.strip()
        if not val:
            return DORMANT, ""
        if not _LINEAGE_RE.fullmatch(val):
            return SELF_LINEAGE_MALFORMED, ""
        return OK, val
    except Exception:  # noqa: BLE001 - anything else is an unreadable config, never a crash
        return CONFIG_UNREADABLE, ""


def find_dir(here, *names):
    """The first directory holding ALL of ``names``: ``$CLAUDE_PLUGIN_ROOT``, then ``here``'s own tree
    (bounded, up to and including the tree root).  None when neither has them."""
    candidates = []
    env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.extend(bounded_parents(Path(here).resolve()))
    for d in candidates:
        try:
            if all((d / n).is_file() for n in names):
                return str(d)
        except OSError:
            continue
    return None
