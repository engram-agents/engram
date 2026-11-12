#!/usr/bin/env python3
"""PreToolUse hook: block ENGRAM writes made by a model of the wrong family (#4596).

Design principle: the harness detects and code writes the text, so a session running on the wrong model
never speaks in the agent's voice.  Every message below is a fixed text filled by code.

hooks.json starts this hook only for the ENGRAM WRITE tools (a generated matcher, both prefixes
``mcp__engram__`` and ``mcp__plugin_engram_engram__``; see ``tools/gen_model_gate_write_tools.py``), so a read
tool or any other tool never pays for it.  The hook still checks the name against the same generated list
(``model_gate_write_tools.json``) and lets any other tool through silently.

Decision, in order (the PreToolUse payload carries ``tool_name``, ``tool_use_id``, ``transcript_path``):

  1. ``self_lineage`` from ``$ENGRAM_HOME/config.json``.  No config, or ``self_lineage`` absent or empty:
     DORMANT, allow, no output.  Config unreadable or not JSON: allow, NEVER SILENT (``config_unreadable``).
     ``self_lineage`` present but malformed: allow, NEVER SILENT (``self_lineage_malformed``).
  2. The model of THIS call: ``engram_signature.model_of_call`` polls the transcript (the harness starts
     this hook before the calling row is written) for ``BUDGET_S`` seconds every ``STEP_S``; on expiry it
     falls back to the newest model row, which is the PREVIOUS reply's model.  The family comes from
     ``engram_core.model_id_to_lineage`` (the one table; this file holds none).
  3. From the calling entry: MATCH allow silently; MISMATCH deny (text 2.1, "This call").
  4. From the fallback: MATCH allow, never silent (``fallback_newest_row``); MISMATCH deny (text 2.1,
     "The previous reply in this session").
  5. UNKNOWN.  An id the table does not map, judged by the provider ``engram_trust._parse_model_id`` reports
     and by the family word of ``self_lineage`` as a ``-``-separated token of the id:
       a. the parsed provider is known and is not self's provider: DENY (a family word such as ``flash``
          can be shared across providers);
       b. same provider and the family token is present: allow, never silent (``unmapped_model``);
       c. provider not recognised and the family token is present: allow, never silent (``unmapped_model``);
       d. the family token is absent: DENY.
     ``mixed_harness``: DENY.  Every other code (``no_model_row``, ``transcript_path_missing``,
     ``transcript_not_found``, ``transcript_unreadable``, ``window_exceeded``, ``budget_expired_no_row``,
     ``internal_error``): allow, NEVER SILENT, text 2.2 with the code.

An import failure of the helpers (round 3) is not silent either: ``gate_import_failed``, text 2.2, printed with
the standard library only, plus the stderr line, exit 0.  Dormancy still wins when it can be decided without
the failed import: a config that the standard library reads, with ``self_lineage`` absent or empty, stays silent.

Helper agents (round 2, R2.1-R2.3): a payload with a non-empty ``agent_id`` is a call made by a sub-agent.  The
model is read from THAT helper's own transcript (``engram_signature.model_of_call(..., agent_id=, session_id=)``:
``<dirname(transcript_path)>/<session_id>/subagents/agent-<agent_id>.jsonl``), never the parent's, with the
same poll, budget, fallback and unmapped rules.  Extra allow-never-silent codes: ``subagent_id_invalid``,
``subagent_transcript_missing``, ``subagent_transcript_unreadable``.  A deny names the helper (``agent_type``,
``agent_id``) and says the main session's own model is not affected.

"Allow, never silent" is an ``additionalContext`` block with no permission decision, so the harness's own
permission flow still applies.  A deny is ``permissionDecision: deny`` with the text as its reason.  The hook
always exits 0.

Not covered (said in the PR): writes that reach the graph outside the MCP tools (sleep and nap scripts, CLI
tools, direct database tools); harnesses other than Claude Code.
"""
import json
import os
import re
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

BUDGET_S = 1.0
STEP_S = 0.02
STDIN_MAX_BYTES = 1 << 20
WRITE_TOOLS_FILE = "model_gate_write_tools.json"
_DEFAULT_PREFIXES = ("mcp__engram__", "mcp__plugin_engram_engram__")

try:
    import _model_gatelib as _gate
    from _hooklib import engram_home_or_exit as _engram_home_or_exit
    _IMPORT_ERROR = None
except (Exception, SystemExit) as _exc:  # noqa: BLE001 - `_hooklib` exits (SystemExit) at import on a relative ENGRAM_HOME; main() reports it on stderr and still exits 0
    _IMPORT_ERROR = type(_exc).__name__

# Filled by _load_runtime() on first use: a dormant seat and a read tool never pay for the engram_core import.
_core = None
model_of_call = None
_parse_model_id = None


# ------------------------------------------------------------------ fixed texts

_SOURCE_CALL = "This call"
_SOURCE_PREVIOUS = "The previous reply in this session"


def _remedy(self_lineage, home):
    return (f"Nothing was written. To continue: relaunch the session on a `{self_lineage}` model. If the "
            f"change of model family is deliberate, that is the human's decision: change `self_lineage` "
            f"in `{home}/config.json`. Reading memory still works.")


def _deny_text(source, model_id, lineage, self_lineage, home, helper=None):
    if helper:
        return (f"ENGRAM write blocked by the model-family gate. {_helper_lead(source, helper)} running on "
                f"model `{model_id}` (family `{lineage}`); this ENGRAM memory is locked to `{self_lineage}`. "
                + _helper_remedy(self_lineage))
    return (f"ENGRAM write blocked by the model-family gate. {source} came from model `{model_id}` "
            f"(family `{lineage}`); this ENGRAM memory is locked to `{self_lineage}`. "
            + _remedy(self_lineage, home))


def _deny_unmapped_text(source, model_id, self_lineage, home, helper=None):
    if helper:
        return (f"ENGRAM write blocked by the model-family gate. {_helper_lead(source, helper)} running on "
                f"model `{model_id}`, which the gate does not recognise as family `{self_lineage}`. "
                + _helper_remedy(self_lineage))
    return (f"ENGRAM write blocked by the model-family gate. {source} came from model `{model_id}`, "
            f"which the gate does not recognise as family `{self_lineage}`. " + _remedy(self_lineage, home))


def _helper_lead(source, helper):
    """The subject of a helper-agent (sub-agent) deny: names the HELPER, so the parent session does not
    read the text as its own model being wrong.  ``helper`` is ``(agent_type, agent_id)``."""
    agent_type, agent_id = helper
    who = f"helper agent (`{agent_type}`, id `{agent_id}`)"
    return f"This call came from a {who}" if source == _SOURCE_CALL else f"The previous reply of a {who}"


def _helper_remedy(self_lineage):
    return ("Nothing was written. The main session's own model is not affected by this block. To let this "
            f"helper write, its agent definition must run on a `{self_lineage}` model; that is the human's "
            "decision.")


def _deny_mixed_text(self_lineage, home):
    return ("ENGRAM write blocked by the model-family gate. The transcript of this session mixes Claude and "
            f"Codex model records, so the gate cannot tell which model made this call; this ENGRAM memory is "
            f"locked to `{self_lineage}`. " + _remedy(self_lineage, home))


def _unverified_text(code):
    return (f"ENGRAM model-family gate: this write went through UNVERIFIED (`{code}`): the gate could not "
            f"confirm which model made the call.")


def _deny(text):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": text}}


def _unverified(code):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": _unverified_text(code)}}


# --------------------------------------------------------------- tool list

def _load_write_tools():
    """``(prefixes, set_of_bare_names)`` from the generated list, or ``(prefixes, None)`` when it cannot be
    found or read: then every call that reached the hook is gated (the matcher already chose it)."""
    try:
        d = _gate.find_dir(__file__, WRITE_TOOLS_FILE)
        if d is None:
            return _DEFAULT_PREFIXES, None
        with open(os.path.join(d, WRITE_TOOLS_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        prefixes = tuple(p for p in data.get("prefixes", ()) if isinstance(p, str)) or _DEFAULT_PREFIXES
        names = {n for n in data.get("write_tools", ()) if isinstance(n, str)}
        return prefixes, (names or None)
    except Exception:  # noqa: BLE001
        return _DEFAULT_PREFIXES, None


def _is_write_tool(tool_name):
    """False only when ``tool_name`` is known NOT to be an ENGRAM write tool."""
    if not isinstance(tool_name, str):
        return True  # cannot tell: gate it (the matcher chose this call)
    prefixes, names = _load_write_tools()
    if names is None:
        return True
    for p in prefixes:
        if tool_name.startswith(p):
            return tool_name[len(p):] in names
    return False


# ---------------------------------------------------------------- runtime

def _load_runtime():
    """Import ``engram_core`` and ``engram_signature``; True when both are usable (cached)."""
    global _core, model_of_call, _parse_model_id
    if _core is not None:
        return True
    d = _gate.find_dir(__file__, "engram_core.py", "engram_signature.py")
    if d is None:
        return False
    if d not in sys.path:
        sys.path.insert(0, d)
    import engram_core
    from engram_signature import model_of_call as _moc
    from engram_trust import _parse_model_id as _pmi
    _core = engram_core
    _parse_model_id = _pmi
    if model_of_call is None:  # (a test may have replaced it)
        model_of_call = _moc
    return True


# --------------------------------------------------------------- decision

def _unmapped(model_id, source_text, self_lineage, home, helper=None):
    """Branches a-d of the unmapped-id rule."""
    mid = re.sub(r"\[[^\]]*\]\s*$", "", model_id.strip()).strip()
    provider = (_parse_model_id(mid).get("provider") or "")
    self_provider, _, self_family = self_lineage.partition(":")
    if provider and provider != self_provider:                    # a
        return _deny(_deny_unmapped_text(source_text, model_id, self_lineage, home, helper))
    fam, toks = self_family.split("-"), mid.split("-")
    if any(toks[i:i + len(fam)] == fam for i in range(len(toks) - len(fam) + 1)):  # b, c
        return _unverified("unmapped_model")
    return _deny(_deny_unmapped_text(source_text, model_id, self_lineage, home, helper))      # d


def _decide(payload, home):
    if not _is_write_tool(payload.get("tool_name")):
        return None
    state, self_lineage = _gate.config_state(home)
    if state == _gate.DORMANT:
        return None
    if state != _gate.OK:
        return _unverified(state)
    if not _load_runtime():
        return _unverified("internal_error")
    tpath, tid = payload.get("transcript_path"), payload.get("tool_use_id")
    agent_id = payload.get("agent_id")
    helper = None
    if agent_id is None or agent_id == "":
        mid, source, reason = model_of_call(tpath, tid, BUDGET_S, STEP_S)
    else:  # a helper agent's call: its own transcript, never the parent's (R2.1)
        at = payload.get("agent_type")
        helper = (at if isinstance(at, str) and at.strip() else "unknown", agent_id)
        mid, source, reason = model_of_call(tpath, tid, BUDGET_S, STEP_S, agent_id=agent_id,
                                            session_id=payload.get("session_id"))
    source_text = _SOURCE_CALL if source == "calling_entry" else _SOURCE_PREVIOUS
    if reason == "mixed_harness":
        return _deny(_deny_mixed_text(self_lineage, home))
    if mid and reason in (None, "unmapped_model"):
        lineage = _core.model_id_to_lineage(mid)
        if not lineage:
            return _unmapped(mid, source_text, self_lineage, home, helper)
        if lineage != self_lineage:
            return _deny(_deny_text(source_text, mid, lineage, self_lineage, home, helper))
        return None if source == "calling_entry" else _unverified("fallback_newest_row")
    return _unverified(reason or "internal_error")


def decide(payload, home):
    """The hook's output object for one PreToolUse payload (None = allow, no output).  Never raises: an
    error after the gate is known to be set up becomes an allow that says ``internal_error``."""
    try:
        return _decide(payload if isinstance(payload, dict) else {}, home)
    except Exception:  # noqa: BLE001
        return _unverified("internal_error")


def _stdlib_dormant():
    """True only when ``config.json`` in the layer's ENGRAM home can be read with the standard library alone AND
    ``self_lineage`` is absent, null or blank.  Used only when an import failed.  The home comes from
    ``_hooklib.engram_home`` (a separate module from the one that failed; it refuses a relative value), imported
    here on its own: if that import fails (it also exits, ``SystemExit``, on a relative home), or the home is
    refused, nothing is judged and the answer is False.
    Same rule as ``_model_gatelib.config_state``'s dormant states, nothing else counts as dormant."""
    try:
        from _hooklib import engram_home as _home_of
        home = str(_home_of())
        try:
            with open(os.path.join(home, "config.json"), "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (FileNotFoundError, NotADirectoryError):
            return True
        if not isinstance(cfg, dict):
            return False
        val = cfg.get("self_lineage")
        return val is None or (isinstance(val, str) and not val.strip())
    except (Exception, SystemExit):  # noqa: BLE001 - importing `_hooklib` exits on a relative home; never dormant
        return False


def _emit_import_failure(skip_dormancy=False):
    """An import failure is never silent (code ``gate_import_failed``), unless the gate is dormant.  Standard
    library plus `_hooklib.engram_home` (imported on its own): it must not depend on the import that failed.  The text is ``_unverified_text``'s.
    ``skip_dormancy``: the failed import was a refusal (``SystemExit``), so the home is known to be refused and
    the answer is "not dormant" without a second `_hooklib` import, which would print the refusal line again."""
    try:
        if not skip_dormancy and _stdlib_dormant():
            return
        text = ("ENGRAM model-family gate: this write went through UNVERIFIED (`gate_import_failed`): the "
                "gate could not confirm which model made the call.")
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                            "additionalContext": text}}) + "\n")
    except Exception:  # noqa: BLE001 - a hook never raises
        pass


def main():
    try:
        raw = sys.stdin.read(STDIN_MAX_BYTES)
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        payload = {}
    if _IMPORT_ERROR:
        # A SystemExit is `_hooklib`'s refusal of a relative ENGRAM_HOME: it already wrote its one stderr line,
        # which names the value (the #3863 rule), so this hook adds no line of its own and does not re-import
        # `_hooklib` to ask about dormancy (that would print the refusal again).  Any other failure: own line.
        # On this path the shipped server routes refuse to start (pinned by tests/test_model_gate_4596.py
        # ::test_r7_a_direct_server_start_on_a_relative_engram_home_refuses_non_zero_and_serves_nothing and the
        # launcher cell in test_launchers_relative_engram_home_3863.py); the notice is printed anyway because a
        # server started with its own environment could still be live (Kepler 27718, Borges's PR amendment).
        refused = _IMPORT_ERROR == "SystemExit"
        if not refused:
            sys.stderr.write("engram model-gate: helper import failed (%s); the gate did not run\n" % _IMPORT_ERROR)
        _emit_import_failure(skip_dormancy=refused)
        return 0
    try:
        res = decide(payload, _engram_home_or_exit())
        if res is not None:
            sys.stdout.write(json.dumps(res) + "\n")
    except Exception:  # noqa: BLE001 - a hook never raises
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
