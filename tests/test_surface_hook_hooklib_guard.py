"""Regression test for gh#2035's follow-up: engram-surface-hook.py's 5
atomic_write_json call sites must NOT crash when _hooklib is absent
(_HOOKLIB_OK == False).

Context: this hook explicitly codes for a missing _hooklib import (a
guarded `try: from _hooklib import ...; _HOOKLIB_OK = True; except
Exception: _HOOKLIB_OK = False`) and its own docstring promises graceful
degradation "including a failed _hooklib import." A prior commit on this
PR added atomic_write_json to that guarded import and called it UNGUARDED
at 5 sites -- NameError, exit 1, empty stdout on every prompt when
_hooklib.py is missing (reproduced directly by Ariadne's colleague
review). None of the sites' existing exception handlers catch NameError
(they only catch FileNotFoundError / json.JSONDecodeError / OSError, or
have no try/except at all).

Each test below forces _HOOKLIB_OK = False AND deletes atomic_write_json
from the module namespace (so a call to it would raise the exact
NameError Ariadne saw, rather than silently succeeding because the real
_hooklib happens to be importable in this dev checkout) -- this precisely
exercises the _HOOKLIB_OK == False branch added at each site, not just any
exception path.

All tests set ENGRAM_HOME to a pytest tmp_path BEFORE importing the hook
module (mirrors test_principle_triggers_registry.py's §5
_load_surface_hook helper) so no test ever touches a real ~/.engram
directory.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_SURFACE_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-surface-hook.py"

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_surface_hook_without_hooklib(engram_home, monkeypatch):
    """Import the surface hook against a scratch ENGRAM_HOME, then simulate
    an absent _hooklib import: _HOOKLIB_OK = False and atomic_write_json
    removed from the module namespace entirely, so any unguarded call site
    would raise NameError exactly as it did before this fix."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_surface_hook_hooklib_guard_test", None)
    spec = importlib.util.spec_from_file_location(
        "engram_surface_hook_hooklib_guard_test", str(_SURFACE_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._HOOKLIB_OK = False
    if hasattr(mod, "atomic_write_json"):
        delattr(mod, "atomic_write_json")
    return mod


def test_write_surface_ledger_falls_back_without_hooklib(tmp_path, monkeypatch):
    """Site 1: _write_surface_ledger must not raise / silently swallow a
    NameError when _hooklib is absent -- the ledger must actually persist
    via the plain-write fallback."""
    mod = _load_surface_hook_without_hooklib(tmp_path, monkeypatch)
    ledger = {"s1": [["ob_0001"]]}
    mod._write_surface_ledger(ledger)

    path = mod._surface_ledger_path()
    with open(path, "r", encoding="utf-8") as f:
        written = json.load(f)
    assert written == ledger, (
        f"expected the ledger fallback write to persist the given content; got {written}"
    )


def test_write_prompt_counter_falls_back_without_hooklib(tmp_path, monkeypatch):
    """Site 2 -- the exact site Ariadne reproduced the crash on (no
    try/except at all, called unguarded from main())."""
    mod = _load_surface_hook_without_hooklib(tmp_path, monkeypatch)
    state = {"prompts_since_compaction": 7, "last_reset": ""}

    mod.write_prompt_counter(state)  # must not raise NameError

    with open(mod.COUNTER_PATH, "r") as f:
        written = json.load(f)
    assert written == state


def test_check_write_reminder_falls_back_without_hooklib(tmp_path, monkeypatch):
    """Site 3: a pending write-reminder marker must still be cleared (and
    the tripwire text returned) via the plain-write fallback."""
    mod = _load_surface_hook_without_hooklib(tmp_path, monkeypatch)
    with open(mod.WRITE_REMINDER_PATH, "w") as f:
        json.dump({"pending": True}, f)

    result = mod.check_write_reminder()

    assert "ENGRAM Write Check" in result
    with open(mod.WRITE_REMINDER_PATH, "r") as f:
        after = json.load(f)
    assert after["pending"] is False, (
        "the pending marker must be cleared even when _hooklib is absent, "
        "or this reminder will repeat every prompt"
    )


def test_check_repair_marker_falls_back_without_hooklib(tmp_path, monkeypatch):
    """Site 4: same shape as site 3, for the tool-call repair marker."""
    mod = _load_surface_hook_without_hooklib(tmp_path, monkeypatch)
    with open(mod.REPAIR_MARKER_PATH, "w") as f:
        json.dump({"pending": True, "tool_name": "engram_add_observation", "repairs": []}, f)

    result = mod.check_repair_marker()

    assert "Tool-Call Repair" in result
    with open(mod.REPAIR_MARKER_PATH, "r") as f:
        after = json.load(f)
    assert after["pending"] is False


def test_check_principle_triggers_falls_back_without_hooklib(tmp_path, monkeypatch):
    """Site 5: PRINCIPLE_TRIGGER_STATE_PATH's write-back is the one site
    that was already atomic (hand-rolled tmp+os.replace) before this PR --
    its fallback must reproduce THAT idiom, not degrade to a plain write.
    Exercised end-to-end via the public check_principle_triggers wrapper,
    same pattern as test_principle_triggers_registry.py's §5 coverage."""
    mod = _load_surface_hook_without_hooklib(tmp_path, monkeypatch)
    registry = {
        "ob_hooklib_guard": {
            "principle_id": "ls_hooklib_guard",
            "kind": "lesson",
            "claim": "Test lesson claim.",
            "nudge": "Do the corrective action.",
        }
    }
    with open(mod.PRINCIPLE_TRIGGERS_PATH, "w") as f:
        json.dump(registry, f)

    # Must not raise NameError -- this is what a real prompt would do on an
    # install where _hooklib.py is genuinely absent.
    result = mod.check_principle_triggers(["ob_hooklib_guard"], prompt_count=5)

    assert "ls_hooklib_guard" in result
    with open(mod.PRINCIPLE_TRIGGER_STATE_PATH, "r") as f:
        state = json.load(f)
    assert state["ls_hooklib_guard"]["fires"] == 1, (
        f"expected the trigger-state fallback write (tmp+os.replace) to persist "
        f"the fire count; got {state}"
    )
