#!/usr/bin/env python3
"""UserPromptSubmit hook: warn the next turn when the last reply came from a model of the wrong family (#4596).

Design principle: the harness detects and code writes the text, so a session on the wrong model never speaks
in the agent's voice.  The block below is the fixed text 2.3, filled by code and rendered through
``_hooklib.emit_context()`` (bounded, trailer first).

Reads the newest model row of the transcript (``engram_signature.read_model_tail``, last 256 KiB) and maps
it with ``engram_core.model_id_to_lineage`` (the one table).  MISMATCH (mapped, and not ``self_lineage``):
inject the block.  MATCH, dormant (no ``self_lineage``), malformed or unreadable config, or UNKNOWN (no model
row, unmapped id, mixed harness, unreadable transcript): print nothing.  The write block
(``engram-model-gate-pretool.py``) is what stops the writes and says so, never silently, when it cannot tell.

Known gap: the first wrong-family turn has nothing to read before it, so it gets no warning; the write block
covers its writes.  The hook always exits 0.
"""
import json
import os
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

TAIL_BYTES = 262144
STDIN_MAX_BYTES = 1 << 20

try:
    import _model_gatelib as _gate
    from _hooklib import emit_context as _emit_context, engram_home_or_exit as _engram_home_or_exit
    from _harness import detect_harness, get_harness_budgets
    _IMPORT_ERROR = None
except Exception as _exc:  # noqa: BLE001 - main() reports it on stderr and still exits 0
    _IMPORT_ERROR = type(_exc).__name__

_core = None
read_model_tail = None


def _warning_text(model_id, lineage, self_lineage):
    return (f"ENGRAM model-family gate: the last reply in this session came from model `{model_id}` "
            f"(family `{lineage}`), but this ENGRAM memory is locked to `{self_lineage}`. ENGRAM writes "
            f"are blocked while this holds. Per the identity rule: stop autonomous and loop work, and "
            f"tell the human which model this is and which family the memory is locked to. Relaunching "
            f"on `{self_lineage}`, or changing `self_lineage` in the config, is the human's decision.")


def _load_runtime():
    global _core, read_model_tail
    if _core is not None:
        return True
    d = _gate.find_dir(__file__, "engram_core.py", "engram_signature.py")
    if d is None:
        return False
    if d not in sys.path:
        sys.path.insert(0, d)
    import engram_core
    from engram_signature import read_model_tail as _rmt
    _core = engram_core
    if read_model_tail is None:
        read_model_tail = _rmt
    return True


def decide(payload, home):
    """The warning text, or None.  Never raises."""
    try:
        state, self_lineage = _gate.config_state(home)
        if state != _gate.OK:
            return None
        tpath = payload.get("transcript_path") if isinstance(payload, dict) else None
        if not isinstance(tpath, str) or not tpath.strip():
            return None
        if not _load_runtime():
            return None
        res = read_model_tail(tpath, TAIL_BYTES)
        if res.reason is not None or not res.model_id:
            return None
        lineage = _core.model_id_to_lineage(res.model_id)
        if not lineage or lineage == self_lineage:
            return None
        return _warning_text(res.model_id, lineage, self_lineage)
    except Exception:  # noqa: BLE001
        return None


def main():
    try:
        raw = sys.stdin.read(STDIN_MAX_BYTES)
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        payload = {}
    if _IMPORT_ERROR:
        sys.stderr.write("engram model-gate: helper import failed (%s); the warning did not run\n" % _IMPORT_ERROR)
        return 0
    try:
        text = decide(payload, _engram_home_or_exit())
        if text is not None:
            budget = get_harness_budgets(detect_harness())["budget"]
            block = _emit_context(text, [], budget=budget, target=budget)
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": block}}) + "\n")
    except Exception:  # noqa: BLE001 - a hook never raises
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
