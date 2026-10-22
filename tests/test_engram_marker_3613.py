"""Contract tests: alpha #3613 Part 1A -- the ENGRAM meaningful-span marker, consumer side.

CONTRACT (stated in the spec; these tests were written BEFORE the code):

Marker tokens (U+27E6 / U+27E7): OPEN = "⟦ENGRAM⟧", CLOSE = "⟦/ENGRAM⟧", defined
in ``_hooklib`` as ``ENGRAM_MARKER_OPEN`` / ``ENGRAM_MARKER_CLOSE``.

``_hooklib.marker_recall_query(text) -> (state, query)``; state in
{"none", "marked", "empty"}. Rules, in order:
  1. No opener anywhere -> ("none", text), and ``query is text`` (identity).
     A lone closer without an opener is also "none".
  2. Balanced nesting first: left-to-right depth scan; top-level spans are the
     text between a depth-0 opener and the closer returning depth to 0. If the
     scan ends at depth 0 with no stray closer at depth 0, the spans are those.
  3. Greedy fallback when unbalanced: ONE span from the first opener to the
     LAST closer after it; no closer after it -> to the end of the text.
  4. Clean spans: remove every marker token inside, strip whitespace, drop
     empties.
  5. No span left -> ("empty", ""); else ("marked", "\\n".join(spans)).
  Never raises; non-str input -> ("none", input).

Consumers:
  * Surface hook (prompt route): "none" = byte-identical to today; "marked" =
    the extracted query is what reaches the recall stage (daemon, FTS fallback,
    attached packs, embed-query concat, prepend gate) and the wrapper text does
    not; "empty" = the recall stage is skipped (no daemon/FTS query, no recall
    block) while every other output (e.g. the write reminder) still prints.
  * Tripwire hook (tool route): lesson-pattern matching stays on the raw
    "tool_name + args" string; only the text handed to the in-turn recall is
    extracted. "marked" -> extracted query; "empty" -> no in-turn recall call;
    "none" -> unchanged.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import unittest.mock
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ENGRAM = PROJECT_ROOT / "src" / "engram"
HOOKS_DIR = SRC_ENGRAM / "hooks" / "claude"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import _hooklib  # noqa: E402

OPEN = "⟦ENGRAM⟧"
CLOSE = "⟦/ENGRAM⟧"

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _q(text):
    # Resolve the helper at call time so a missing function is a per-cell red.
    return _hooklib.marker_recall_query(text)


# ---------------------------------------------------------------------------
# H: the helper
# ---------------------------------------------------------------------------


def test_constants_are_the_white_square_bracket_tokens():
    assert _hooklib.ENGRAM_MARKER_OPEN == "⟦ENGRAM⟧"
    assert _hooklib.ENGRAM_MARKER_CLOSE == "⟦/ENGRAM⟧"


def test_h1_none_no_opener_returns_same_object():
    text = "plain wrapper text with no marker at all"
    state, query = _q(text)
    assert state == "none"
    assert query is text


def test_h1_none_lone_closer():
    text = f"before {CLOSE} after"
    state, query = _q(text)
    assert (state, query) == ("none", text)
    assert query is text


def test_h1_none_empty_string():
    text = ""
    state, query = _q(text)
    assert (state, query) == ("none", "")
    assert query is text


def test_h2_marked_single_span_strips_whitespace_and_wrapper():
    assert _q(f"[wrapper] {OPEN}  hello world \n{CLOSE} [tail]") == ("marked", "hello world")


def test_h2_marked_two_top_level_spans_joined_by_newline():
    text = f"w {OPEN}alpha{CLOSE} mid {OPEN}beta{CLOSE} z"
    assert _q(text) == ("marked", "alpha\nbeta")


def test_h2_marked_balanced_nested_pair_inner_tokens_removed_text_kept():
    text = f"w {OPEN}outer {OPEN}inner{CLOSE} tail{CLOSE} z"
    assert _q(text) == ("marked", "outer inner tail")


def test_h3_empty_adjacent_tokens():
    assert _q(f"{OPEN}{CLOSE}") == ("empty", "")


def test_h3_empty_whitespace_only():
    assert _q(f"wrapper {OPEN}  \n\t {CLOSE} wrapper") == ("empty", "")


def test_h3_empty_two_empty_spans():
    assert _q(f"{OPEN}{CLOSE} x {OPEN} {CLOSE}") == ("empty", "")


def test_h3_empty_span_dropped_among_nonempty():
    assert _q(f"{OPEN}{CLOSE}{OPEN}kept{CLOSE}") == ("marked", "kept")


def test_h4_greedy_extra_opener_first_opener_to_last_closer():
    # unbalanced (depth 1 at end): one span, first opener .. last closer
    assert _q(f"{OPEN} a {OPEN} b {CLOSE}") == ("marked", "a  b")


def test_h4_greedy_stray_extra_closer_goes_to_last_closer():
    # closer at depth 0 after the first opener -> greedy to the LAST closer
    assert _q(f"{OPEN} a {CLOSE} b {CLOSE}") == ("marked", "a  b")


def test_h4_greedy_stray_closer_then_second_span():
    # the balanced scan would have yielded "a" and "c"; stray closer makes it
    # unbalanced, so greedy absorbs everything to the last closer.
    assert _q(f"{OPEN}a{CLOSE}{CLOSE}x{OPEN}c{CLOSE}") == ("marked", "axc")


def test_h4_greedy_opener_without_closer_runs_to_end():
    assert _q(f"wrapper {OPEN} only the tail here") == ("marked", "only the tail here")


def test_h4_greedy_tokens_inside_quoted_payload_absorbed():
    text = f"w {OPEN}said: {OPEN}quoted{CLOSE} then more{CLOSE} z"
    # balanced -> nested; still one span with inner tokens removed
    assert _q(text) == ("marked", "said: quoted then more")


def test_h5_wrapper_text_outside_marker_not_in_query():
    text = f"WRAPPER-HEAD {OPEN}signal{CLOSE} WRAPPER-TAIL"
    state, query = _q(text)
    assert state == "marked"
    assert "WRAPPER" not in query
    assert query == "signal"


@pytest.mark.parametrize("bad", [None, 123, 4.5, b"bytes", ["a"], {"k": 1}, object()])
def test_h6_non_str_input_returns_none_and_input(bad):
    state, query = _q(bad)
    assert state == "none"
    assert query is bad


# ---------------------------------------------------------------------------
# P: surface hook (prompt route)
# ---------------------------------------------------------------------------

_SURFACE_HOOK_PATH = HOOKS_DIR / "engram-surface-hook.py"


def _load_surface_hook(engram_home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_surface_hook_marker3613", None)
    spec = importlib.util.spec_from_file_location(
        "engram_surface_hook_marker3613", str(_SURFACE_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Fixture:
    """Stub daemon + FTS + attached packs; records every query they receive."""

    def __init__(self, mod, monkeypatch, *, daemon_up=True, short_gate=False):
        self.daemon_calls = []
        self.fts_calls = []
        self.pack_calls = []
        self.nudge_calls = 0

        def _daemon(query, *a, **kw):
            self.daemon_calls.append((query, kw.get("embed_query")))
            if not daemon_up:
                return None
            return {"matched_ids": ["nd_fake1"], "match_count": 1}

        def _fts(query, *a, **kw):
            self.fts_calls.append(query)
            return {"matched_ids": ["nd_fake1"], "match_count": 1, "nodes": []}

        def _packs(query, *a, **kw):
            self.pack_calls.append(query)
            return []

        def _nudge(*a, **kw):
            self.nudge_calls += 1
            return "[RECALL-NUDGE nd_fake1]"

        monkeypatch.setattr(mod, "query_daemon", _daemon)
        monkeypatch.setattr(mod, "query_fts_fallback", _fts)
        monkeypatch.setattr(mod, "_query_attached_packs", _packs)
        monkeypatch.setattr(mod, "format_nudge", _nudge)
        monkeypatch.setattr(mod, "_get_auto_surface_config", lambda: {
            "idf_gate_enabled": False,
            "short_prompt_threshold_chars": 10_000 if short_gate else 0,
            "prev_response_tail_chars": 50,
        })
        # Record what the prepend gate scored by observing the embed query.
        monkeypatch.setattr(mod, "_read_prev_assistant_tail", lambda *a, **kw: "PREVTAIL")
        monkeypatch.setattr(mod, "check_principle_triggers_full", lambda *a, **kw: ("", []))
        monkeypatch.setattr(mod, "check_error_patterns", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "check_write_reminder", lambda: "[WRITE-REMINDER-FIRES]")
        monkeypatch.setattr(mod, "check_repair_marker", lambda: "")
        monkeypatch.setattr(mod, "check_feeling_nudge_marker", lambda: "")
        monkeypatch.setattr(mod, "check_warm_briefing", lambda count: "")
        monkeypatch.setattr(mod, "format_nap_warning", lambda count: "")
        monkeypatch.setattr(mod, "_check_mcp_write_tool_marker", lambda *a: (True, None))
        monkeypatch.setattr(mod, "_check_db_liveness", lambda *a: (True, "ok"))
        monkeypatch.setattr(mod, "_check_embedding_restore_flag", lambda *a: (True, ""))
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")
        monkeypatch.setattr(mod, "write_telemetry_record", lambda **kw: None)
        orig = sys.modules.get("context_tracker")
        sys.modules["context_tracker"] = types.SimpleNamespace(
            estimate_usage=lambda *a, **kw: None,
            format_drowsiness=lambda *a, **kw: "",
        )
        self._restore = orig

    def restore(self):
        if self._restore is not None:
            sys.modules["context_tracker"] = self._restore
        else:
            sys.modules.pop("context_tracker", None)


def _run_surface(mod, prompt):
    stdin_data = json.dumps({"prompt": prompt, "session_id": "marker-3613", "transcript_path": ""})
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with patch("sys.stdout", buf):
            try:
                mod.main()
            except SystemExit:
                pass
    return buf.getvalue()


WRAPPER_PROMPT_NONE = "Monitor event: WRAPPERWORD heartbeat tick no marker here"

# Output recorded from the BASE commit (a9fb6a6d) for WRAPPER_PROMPT_NONE with
# the fixture above (daemon up). The no-marker control must stay byte-identical.
_P1_PINNED_OUTPUT = '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\\u25b8 2 shown \\u00b7 0 cut\\n[RECALL-NUDGE nd_fake1]\\n[WRITE-REMINDER-FIRES]"}}\n'


@pytest.fixture
def surface(tmp_path, monkeypatch):
    home = tmp_path / "engram"
    home.mkdir()
    mod = _load_surface_hook(home, monkeypatch)
    fx = _Fixture(mod, monkeypatch)
    yield mod, fx
    fx.restore()


def test_p1_surface_none_output_byte_identical_to_base(surface):
    mod, fx = surface
    out = _run_surface(mod, WRAPPER_PROMPT_NONE)
    assert out == _P1_PINNED_OUTPUT
    # and the raw (stripped) prompt reached the daemon, unchanged
    assert fx.daemon_calls[0][0] == WRAPPER_PROMPT_NONE


def test_p1_surface_none_fts_fallback_receives_raw_prompt(tmp_path, monkeypatch):
    home = tmp_path / "engram"
    home.mkdir()
    mod = _load_surface_hook(home, monkeypatch)
    fx = _Fixture(mod, monkeypatch, daemon_up=False)
    try:
        _run_surface(mod, WRAPPER_PROMPT_NONE)
    finally:
        fx.restore()
    assert fx.fts_calls == [WRAPPER_PROMPT_NONE]
    assert fx.pack_calls == [WRAPPER_PROMPT_NONE]


def test_p2_surface_marked_daemon_query_has_marked_text_not_wrapper(surface):
    mod, fx = surface
    prompt = f"WRAPPERWORD notification: {OPEN}the quokka migration plan{CLOSE} WRAPPERTAIL"
    out = _run_surface(mod, prompt)
    assert fx.daemon_calls, "recall must run for a marked prompt"
    q, _embed = fx.daemon_calls[0]
    assert q == "the quokka migration plan"
    assert "WRAPPER" not in q
    assert fx.pack_calls == ["the quokka migration plan"]
    assert "[RECALL-NUDGE nd_fake1]" in out


def test_p2_surface_marked_fts_fallback_query_has_marked_text_not_wrapper(tmp_path, monkeypatch):
    home = tmp_path / "engram"
    home.mkdir()
    mod = _load_surface_hook(home, monkeypatch)
    fx = _Fixture(mod, monkeypatch, daemon_up=False)
    try:
        _run_surface(mod, f"WRAPPERWORD {OPEN}the quokka plan{CLOSE} WRAPPERTAIL")
    finally:
        fx.restore()
    assert fx.fts_calls == ["the quokka plan"]


def test_p2_surface_marked_embed_query_concat_uses_marked_text(tmp_path, monkeypatch):
    home = tmp_path / "engram"
    home.mkdir()
    mod = _load_surface_hook(home, monkeypatch)
    fx = _Fixture(mod, monkeypatch, short_gate=True)  # legacy gate: short -> prepend
    try:
        _run_surface(mod, f"WRAPPERWORD {OPEN}quokka plan{CLOSE} WRAPPERTAIL")
    finally:
        fx.restore()
    q, embed = fx.daemon_calls[0]
    assert q == "quokka plan"
    assert embed == "PREVTAIL\n\nquokka plan"
    assert "WRAPPER" not in embed


def test_p3_surface_empty_marker_skips_recall_but_other_nudges_print(surface):
    mod, fx = surface
    out = _run_surface(mod, f"WRAPPERWORD {OPEN}{CLOSE} WRAPPERTAIL")
    assert fx.daemon_calls == []
    assert fx.fts_calls == []
    assert fx.pack_calls == []
    assert fx.nudge_calls == 0
    assert "RECALL-NUDGE" not in out
    assert "[WRITE-REMINDER-FIRES]" in out


def test_p3_surface_empty_marker_does_not_emit_daemon_down_warning(tmp_path, monkeypatch):
    home = tmp_path / "engram"
    home.mkdir()
    mod = _load_surface_hook(home, monkeypatch)
    fx = _Fixture(mod, monkeypatch, daemon_up=False)
    try:
        out = _run_surface(mod, f"{OPEN} {CLOSE}")
    finally:
        fx.restore()
    assert "daemon" not in out.lower()
    assert "[WRITE-REMINDER-FIRES]" in out


# ---------------------------------------------------------------------------
# T: tripwire hook (tool route)
# ---------------------------------------------------------------------------

_TRIPWIRE_PATH = HOOKS_DIR / "engram-lesson-tripwire-hook.py"


def _load_tripwire():
    spec = importlib.util.spec_from_file_location("lesson_tripwire_hook_marker3613", _TRIPWIRE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_tripwire(hook, hook_input, *, tripwires=None, recall_mock=None):
    buf = io.StringIO()
    mock = recall_mock if recall_mock is not None else unittest.mock.Mock(return_value="")
    with (
        patch.object(hook, "load_tripwires", return_value=tripwires or []),
        patch.object(hook, "load_prevention_lessons", return_value=[]),
        patch.object(hook, "check_in_turn_recall", mock),
        patch("sys.stdin", io.StringIO(json.dumps(hook_input))),
        patch("sys.stdout", buf),
    ):
        try:
            hook.main()
        except SystemExit:
            pass
    return buf.getvalue(), mock


def _bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


_TRIPWIRES = [("danger-pattern", "Heed the danger lesson.", "ls_fake1")]


@pytest.mark.parametrize("cmd", [
    "do danger-pattern now",
    f"do {OPEN}something else{CLOSE} then danger-pattern",
    f"echo {OPEN}harmless{CLOSE}; danger-pattern",
    f"echo {OPEN}{CLOSE} danger-pattern",
])
def test_t1_tripwire_lesson_fires_regardless_of_marker(cmd):
    hook = _load_tripwire()
    out, _ = _run_tripwire(hook, _bash(cmd), tripwires=_TRIPWIRES)
    assert "Heed the danger lesson." in out


def test_t1_tripwire_pattern_only_inside_wrapper_still_fires():
    # The pattern sits OUTSIDE the marked span: matching must still see raw text.
    hook = _load_tripwire()
    out, _ = _run_tripwire(
        hook, _bash(f"danger-pattern {OPEN}benign words{CLOSE}"), tripwires=_TRIPWIRES
    )
    assert "Heed the danger lesson." in out


def test_t2_tripwire_none_passes_raw_match_target_unchanged():
    hook = _load_tripwire()
    cmd = "echo WRAPPERWORD plain command"
    _, mock = _run_tripwire(hook, _bash(cmd))
    mock.assert_called_once()
    assert mock.call_args.args[0] == cmd


def test_t2_tripwire_marked_passes_extracted_text_only():
    hook = _load_tripwire()
    cmd = f"echo WRAPPERWORD {OPEN}quokka migration{CLOSE} WRAPPERTAIL"
    _, mock = _run_tripwire(hook, _bash(cmd))
    mock.assert_called_once()
    assert mock.call_args.args[0] == "quokka migration"


def test_t2_tripwire_empty_marker_skips_in_turn_recall():
    hook = _load_tripwire()
    out, mock = _run_tripwire(hook, _bash(f"echo WRAPPERWORD {OPEN}{CLOSE}"))
    mock.assert_not_called()
    assert "in-turn recall" not in out


def test_t2_tripwire_empty_marker_still_prints_tripwire_hit():
    hook = _load_tripwire()
    out, mock = _run_tripwire(
        hook, _bash(f"danger-pattern {OPEN}{CLOSE}"), tripwires=_TRIPWIRES
    )
    mock.assert_not_called()
    assert "Heed the danger lesson." in out


# ---------------------------------------------------------------------------
# R: raw-prompt uses in the surface hook stay on the RAW prompt (M6 follow-up)
# ---------------------------------------------------------------------------
# The recall QUERY is narrowed to the marked span; everything that is not the
# recall query keeps the raw (stripped) prompt. Spies pin each use, for both a
# marked and an empty-marker prompt.

_RAW_MARKED = f"WRAPPERWORD notification: {OPEN}the quokka plan{CLOSE} WRAPPERTAIL"
_RAW_EMPTY = f"WRAPPERWORD notification: {OPEN}{CLOSE} WRAPPERTAIL"


class _Spies:
    def __init__(self, mod, monkeypatch):
        self.error_pattern_args = []
        self.fire_key_args = []
        self.ledger_queries = []
        self.emitted = []

        def _err(p, *a, **kw):
            self.error_pattern_args.append(p)
            return None

        real_key = mod.prompt_fire_key

        def _key(p):
            self.fire_key_args.append(p)
            return real_key(p)

        def _ledger(kind, session_id, fire_key, query, *a, **kw):
            self.ledger_queries.append(query)

        class _Emitter:
            @staticmethod
            def init(**kw):
                return _Emitter()

            def emit(inner, event_type=None, data=None, **kw):
                self.emitted.append((event_type, data))
                return "uid"

        monkeypatch.setattr(mod, "check_error_patterns", _err)
        monkeypatch.setattr(mod, "prompt_fire_key", _key)
        monkeypatch.setattr(mod, "write_route_ledger", _ledger)
        monkeypatch.setitem(
            sys.modules, "engram_log_emitter", types.SimpleNamespace(Emitter=_Emitter)
        )

    def surface_fire_lens(self):
        return [d["prompt_len_chars"] for t, d in self.emitted if t == "engram.surface.fire"]


@pytest.fixture
def raw_use(surface, monkeypatch):
    mod, fx = surface
    return mod, _Spies(mod, monkeypatch)


@pytest.mark.parametrize("raw", [_RAW_MARKED, _RAW_EMPTY], ids=["marked", "empty"])
def test_r_a_check_error_patterns_receives_raw_prompt(raw_use, raw):
    mod, spies = raw_use
    _run_surface(mod, raw)
    assert spies.error_pattern_args == [raw]


def test_r_b_prompt_fire_key_called_with_raw_prompt_marked(raw_use):
    mod, spies = raw_use
    _run_surface(mod, _RAW_MARKED)
    assert spies.fire_key_args == [_RAW_MARKED]


def test_r_b_prompt_fire_key_not_called_for_empty_marker(raw_use):
    # The only fire-key call site is the route-ledger write, which an empty
    # marker skips (no recall ran, nothing to record).
    mod, spies = raw_use
    _run_surface(mod, _RAW_EMPTY)
    assert spies.fire_key_args == []


def test_r_c_surface_fire_telemetry_length_is_raw_prompt_length_marked(raw_use):
    mod, spies = raw_use
    _run_surface(mod, _RAW_MARKED)
    assert spies.surface_fire_lens() == [len(_RAW_MARKED)]


def test_r_c_surface_fire_telemetry_not_emitted_for_empty_marker(raw_use):
    mod, spies = raw_use
    _run_surface(mod, _RAW_EMPTY)
    assert spies.surface_fire_lens() == []


def test_r_d_route_ledger_receives_extracted_query_marked(raw_use):
    mod, spies = raw_use
    _run_surface(mod, _RAW_MARKED)
    assert spies.ledger_queries == ["the quokka plan"]


def test_r_d_route_ledger_not_written_for_empty_marker(raw_use):
    mod, spies = raw_use
    _run_surface(mod, _RAW_EMPTY)
    assert spies.ledger_queries == []
