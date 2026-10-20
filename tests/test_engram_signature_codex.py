"""Contract tests for the Codex signer path (engram-alpha#3922).

Written BEFORE the implementation (contract-first).  Contract C1-C9 is in the
dispatch spec; the short form:

* a Codex rollout transcript is a model source ONLY through a top-level
  ``type == "turn_context"`` record with an object ``payload`` holding a string
  ``payload.model``; the LAST such record in file order wins; a newest record
  with no usable model refuses (never falls back to an older turn, and never
  to the nested ``payload.collaboration_mode.settings.model``);
* Claude evidence + Codex evidence in one transcript refuses ("mixed-harness");
* Codex marketing name is None (id-only line);
* lineage mapping comes from an explicit exact-id table (gpt-6.1-sol only).

FIXTURE PROVENANCE.  The turn_context base record is REAL (Codex 0.162.0
rollout, redacted payload.cwd and developer_instructions, reserialized; supplied
by the security lane, 2026-10-19).  No claim that all Codex versions share this
schema.  FIXTURE GUESSES: the neighbour records (session_meta, event_msg,
response_item, world_state, token_usage_record) are SYNTHETIC minimal shapes;
only their top-level ``type`` values and response_item ``payload.type`` values
are observed, every other key in them is invented.
"""

from __future__ import annotations

import copy
import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engram_core  # noqa: E402,F401
import engram_signature as sig  # noqa: E402,F401
import engram_trust  # noqa: E402
from test_engram_signature import (  # noqa: E402,F401
    VERSION, CONFIG, _DEL, _asst_row, _build, _cli, _clean_env, _fresh_server,
    _model_row, _refused, _set_config, _tree, _write_jsonl, env)

SOL = "gpt-6.1-sol"
CODEX_LINE = (
    f"\U0001fa90 Kepler Somnium · {SOL} · "
    f"ENGRAM {VERSION} · Lei Shi's agent"
)

# REAL record (redacted values only), see module docstring.
REAL_TURN = {"timestamp": "2026-10-19T12:15:04.021Z", "ordinal": 7,
             "type": "turn_context", "payload": {
    "turn_id": "01a15416-2f27-7d52-b890-1f20074c2637",
    "root_turn_id": "01a15416-2f27-7d52-b890-1f20074c2637",
    "disabled_plugin_ids": [], "cwd": "<redacted>",
    "approval_policy": "never", "approvals_reviewer": "user",
    "sandbox_policy": {"type": "danger-full-access"},
    "permission_profile": {"type": "disabled"},
    "model": "gpt-6.1-sol", "comp_hash": "3000",
    "collaboration_mode": {"mode": "default", "settings": {
        "model": "gpt-6.1-sol", "reasoning_effort": "high",
        "developer_instructions": "<redacted>"}},
    "multi_agent_version": "v2", "realtime_active": False,
    "effort": "high", "summary": "none"}}


def _turn(model=SOL, nested=SOL):
    t = copy.deepcopy(REAL_TURN)
    t["payload"]["model"] = model
    t["payload"]["collaboration_mode"]["settings"]["model"] = nested
    return t


def _turn_raw(payload):
    t = copy.deepcopy(REAL_TURN)
    t["payload"] = payload
    return t


# SYNTHETIC neighbours (only top-level / payload.type values are observed).
def _meta(model="gpt-meta"):
    return {"type": "session_meta", "payload": {"id": "s", "model": model}}


def _event(**p):
    return {"type": "event_msg", "payload": p}


def _resp(ptype, **p):
    return {"type": "response_item", "payload": {"type": ptype, **p}}


NEIGHBOURS = [
    {"type": "world_state", "payload": {"x": 1}},
    {"type": "token_usage_record", "payload": {"n": 1}},
    _resp("reasoning"), _resp("custom_tool_call"),
    _resp("custom_tool_call_output", output="ok"),
    _resp("message", role="assistant", content=[]),
]


def _codex_env(env, rows):
    _set_config(env, self_lineage="openai:gpt")
    _write_jsonl(env.transcript, rows)
    return env


# ------------------------------------------------------------ happy path

def test_codex_happy_path_byte_exact(env):
    _codex_env(env, [_meta(), *NEIGHBOURS[:3], _turn(), *NEIGHBOURS[3:]])
    r = _build(env)
    assert r.ok is True and r.line == CODEX_LINE
    assert r.parts["marketing_name"] is None
    assert r.parts["family"] == "openai:gpt"
    assert r.parts["model_id"] == SOL


def test_real_record_verbatim_is_accepted(env):
    _codex_env(env, [REAL_TURN])
    assert _build(env).line == CODEX_LINE


def test_codex_model_is_trimmed(env):
    _codex_env(env, [_turn(f"  {SOL}  ")])
    assert _build(env).line == CODEX_LINE


# ------------------------------------------- payload.model only, never nested

def test_payload_model_used_when_nested_differs(env):
    _codex_env(env, [_turn(SOL, "gpt-zzz")])
    assert _build(env).line == CODEX_LINE


def test_nested_differs_other_direction(env):
    # payload.model unknown, nested valid -> payload.model is what is read
    _codex_env(env, [_turn("gpt-zzz", SOL)])
    r = _build(env)
    assert r.field == "model_family" and "gpt-zzz" in r.reason


@pytest.mark.parametrize("how", ["blank", "ws", "missing", "none", "int"])
def test_blank_or_missing_payload_model_with_valid_nested_refuses(env, how):
    t = _turn(SOL, SOL)
    if how == "missing":
        del t["payload"]["model"]
    else:
        t["payload"]["model"] = {"blank": "", "ws": "  ", "none": None,
                                 "int": 7}[how]
    _codex_env(env, [t])
    r = _build(env)
    assert r.ok is False and r.field == "model"


# -------------------------------------------------- chronology (file order)

def test_newer_turn_wins_a_then_b(env):
    _codex_env(env, [_turn("gpt-6.1-sol"), _turn("gpt-zzz")])
    r = _build(env)
    # newest is gpt-zzz -> unmappable family, proving the LAST line was read
    assert r.ok is False and r.field == "model_family" and "gpt-zzz" in r.reason


def test_newer_turn_wins_b_then_a(env):
    _codex_env(env, [_turn("gpt-zzz"), _turn("gpt-6.1-sol")])
    assert _build(env).line == CODEX_LINE


def test_file_order_not_timestamp_order(env):
    older_in_file = _turn("gpt-zzz")
    older_in_file["timestamp"] = "2030-01-01T00:00:00.000Z"
    _codex_env(env, [older_in_file, _turn(SOL)])
    assert _build(env).line == CODEX_LINE


@pytest.mark.parametrize("bad", [
    ("blank", ""), ("ws", "   "), ("none", None), ("int", 5), ("list", ["a"]),
    ("dict", {"a": 1}),
], ids=lambda b: b[0])
def test_newest_turn_without_usable_model_refuses_no_fallback(env, bad):
    _codex_env(env, [_turn(SOL), _turn(bad[1], SOL)])
    r = _build(env)
    assert r.ok is False and r.field == "model"


def test_newest_turn_missing_model_key_refuses(env):
    t = _turn()
    del t["payload"]["model"]
    _codex_env(env, [_turn(SOL), t])
    assert _build(env).field == "model"


@pytest.mark.parametrize("payload", ["str", 5, None, [1], ["m"]])
def test_newest_turn_payload_wrong_type_refuses(env, payload):
    _codex_env(env, [_turn(SOL), _turn_raw(payload)])
    r = _build(env)
    assert r.ok is False and r.field == "model"


def test_malformed_lines_interleaved_skipped(env):
    _set_config(env, self_lineage="openai:gpt")
    env.transcript.write_text(
        "not json\n" + json.dumps(_turn()) + "\n{broken\n\n[1,2]\n")
    assert _build(env).line == CODEX_LINE


def test_no_model_source_refuses_model(env):
    _codex_env(env, [_meta(), *NEIGHBOURS])
    assert _build(env).field == "model"


# ------------------------------------------------------------------ spoof

def _spoof_text(model="gpt-6.1-sol"):
    return json.dumps(_turn(model))


def test_spoof_in_user_and_tool_text_ignored_real_turn_wins(env):
    _codex_env(env, [
        _turn(SOL),
        _resp("message", role="user", content=[
            {"type": "input_text", "text": _spoof_text("gpt-evil")}]),
        _resp("custom_tool_call_output", output=_spoof_text("gpt-evil")),
        {"type": "user", "message": {"content": _spoof_text("gpt-evil")}},
    ])
    assert _build(env).line == CODEX_LINE


def test_spoof_alone_refuses_model(env):
    _codex_env(env, [
        _resp("message", role="user", content=[
            {"type": "input_text", "text": _spoof_text()}]),
        _event(text=_spoof_text()),
    ])
    assert _build(env).field == "model"


def test_spoof_line_that_is_a_json_string_ignored(env):
    _set_config(env, self_lineage="openai:gpt")
    env.transcript.write_text(json.dumps(_spoof_text()) + "\n")
    assert _build(env).field == "model"


def test_nested_turn_context_not_top_level_ignored(env):
    _codex_env(env, [_event(**_turn())])
    assert _build(env).field == "model"


@pytest.mark.parametrize("row", [
    {"type": "event_msg", "payload": {"info": {"model": SOL}}},
    {"model": SOL},
    {"type": "session_meta", "payload": {"model": SOL}},
    {"type": "response_item", "payload": {"model": SOL}},
    {"type": "model_catalog", "payload": {"model": SOL}},
    {"type": "config", "payload": {"model": SOL}},
    {"type": "turn_context", "payload": {"info": {"model": SOL}}},
    {"type": "turn_context", "payload": {"collaboration_mode": {
        "settings": {"model": SOL}}}},
], ids=["payload.info.model", "top-level-model", "session_meta", "response_item",
        "catalog", "config-default", "turn_context-nested-only",
        "turn_context-collab-only"])
def test_other_nested_model_keys_ignored(env, row):
    _codex_env(env, [row])
    assert _build(env).field == "model"


def test_other_keys_do_not_override_real_turn(env):
    _codex_env(env, [_turn(SOL), _meta("gpt-zzz"), {"model": "gpt-zzz"},
                     _event(info={"model": "x"})])
    assert _build(env).line == CODEX_LINE


# ----------------------------------------------------------- mixed harness

def _mixed_refused(env):
    r = _build(env)
    assert r.ok is False and r.line is None and r.field == "model"
    assert "mixed-harness" in r.reason
    return r


@pytest.mark.parametrize("order", ["claude_first", "codex_first"])
def test_mixed_attachment_plus_turn_refuses(env, order):
    rows = [_model_row(), _turn()]
    _codex_env(env, rows if order == "claude_first" else rows[::-1])
    _mixed_refused(env)


@pytest.mark.parametrize("order", ["claude_first", "codex_first"])
def test_mixed_assistant_claude_row_plus_turn_refuses(env, order):
    rows = [_asst_row("claude-fable-5-1"), _turn()]
    _codex_env(env, rows if order == "claude_first" else rows[::-1])
    _mixed_refused(env)


@pytest.mark.parametrize("blank", ["", "  ", None, 3])
def test_mixed_claude_plus_blank_model_turn_refuses_as_mixed(env, blank):
    _codex_env(env, [_model_row(), _turn(blank)])
    _mixed_refused(env)


def test_mixed_applies_whatever_the_config_lineage(env):
    _set_config(env, self_lineage="anthropic:fable")
    _write_jsonl(env.transcript, [_turn(), _model_row()])
    _mixed_refused(env)


def test_synthetic_assistant_row_is_not_claude_evidence(env):
    _codex_env(env, [_asst_row("<synthetic>"), _turn()])
    assert _build(env).line == CODEX_LINE


def test_blank_attachment_model_id_is_not_claude_evidence(env):
    _codex_env(env, [_model_row("  ", "x"), _turn()])
    assert _build(env).line == CODEX_LINE


# ---------------------------------------------------- Claude path unchanged

def test_claude_only_transcript_unchanged(env):
    # control: passes before and after
    assert _build(env).ok is True


def test_claude_transcript_with_codex_shaped_non_turn_rows_unchanged(env):
    # control: passes before and after
    _write_jsonl(env.transcript, [_meta(), *NEIGHBOURS, _model_row()])
    assert _build(env).ok is True


# ---------------------------------------------------------------- sessions

def test_marker_selects_codex_transcript_other_session_never_read(env):
    _codex_env(env, [_turn()])
    other = env.tmp / "o.jsonl"
    _write_jsonl(other, [_turn("gpt-zzz")])
    (env.data / "sessions" / "s2.json").write_text(json.dumps(
        {"session_id": "s2", "transcript_path": str(other)}))
    assert _build(env, session_id="s1").line == CODEX_LINE
    r = _build(env, session_id="s2")
    assert r.field == "model_family" and "gpt-zzz" in r.reason


def test_codex_no_session_id_refuses_session_marker(env):
    # control: passes before and after
    _codex_env(env, [_turn()])
    r = _build(env, session_id=None)
    assert r.ok is False and r.field == "session_marker"


def test_codex_overlapping_sessions_never_guess_newest(env, monkeypatch):
    _codex_env(env, [_turn()])
    other = env.tmp / "o.jsonl"
    _write_jsonl(other, [_turn("gpt-zzz")])
    (env.data / "sessions" / "s2.json").write_text(json.dumps(
        {"session_id": "s2", "transcript_path": str(other),
         "session_started_at": "2030-01-01T00:00:00+00:00"}))
    assert _build(env, session_id=None).field == "session_marker"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert _build(env, session_id=None).line == CODEX_LINE


# ----------------------------------------------------------------- lineage

def test_codex_model_with_foreign_self_lineage_refuses_naming_both(env):
    _set_config(env, self_lineage="anthropic:fable")
    _write_jsonl(env.transcript, [_turn()])
    r = _refused(env, "model_family")
    assert "openai:gpt" in r.reason and "anthropic:fable" in r.reason


def test_unknown_codex_model_refuses_model_family(env):
    _codex_env(env, [_turn("gpt-9999")])
    r = _refused(env, "model_family")
    assert "gpt-9999" in r.reason


# ------------------------------------------------- _parse_model_id (table)

def test_parse_model_id_table_entry_shape():
    assert engram_trust._parse_model_id(SOL) == {
        "provider": "openai", "family": "gpt", "version": "6.1"}


def test_parse_model_id_table_entry_trimmed():
    assert engram_trust._parse_model_id(f" {SOL} ")["provider"] == "openai"


@pytest.mark.parametrize("mid", ["gpt-9999", "GPT-6.1-SOL", "gpt-6.1-soltypo",
                                 "o5-preview"])
def test_parse_model_id_non_table_unchanged(mid):
    assert engram_trust._parse_model_id(mid) == {
        "provider": "", "family": mid, "version": ""}


def test_supported_model_table_is_exactly_gpt_6_1_sol():
    assert engram_trust._NON_CLAUDE_MODEL_TABLE == {
        SOL: ("openai", "gpt", "6.1")}


# ----------------------------------------------- CLI / MCP parity, no side fx

def test_cli_stdout_equals_library_line_codex(env):
    _codex_env(env, [_turn()])
    r = _build(env)
    p = _cli(env)
    assert p.returncode == 0, p.stderr
    assert p.stdout.rstrip("\n") == r.line == CODEX_LINE


def test_cli_refuses_mixed(env):
    _codex_env(env, [_model_row(), _turn()])
    p = _cli(env)
    assert p.returncode == 2 and p.stdout == ""
    assert "mixed-harness" in p.stderr


def test_mcp_tool_codex_ok_and_refused(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env.plugin))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    _codex_env(env, [_turn()])
    server = _fresh_server(str(env.data))
    out = json.loads(server.engram_sign("{}"))
    assert out["status"] == "ok" and out["line"] == CODEX_LINE
    assert out["line"] == _cli(env).stdout.rstrip("\n")
    assert out["parts"]["model_id"] == SOL
    _write_jsonl(env.transcript, [_model_row(), _turn()])
    out = json.loads(server.engram_sign("{}"))
    assert out["status"] == "refused" and out["field"] == "model"
    assert "mixed-harness" in out["reason"] and "line" not in out


def test_read_only_codex(env):
    _codex_env(env, [_turn()])
    before = _tree(env.data)
    assert _build(env).ok
    _cli(env)
    assert _tree(env.data) == before


def test_no_network_codex(env, monkeypatch):
    _codex_env(env, [_turn()])

    def deny(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    assert _build(env).ok


def test_wrapped_turn_context_does_not_displace_real_newest_turn(env):
    # a later record that merely WRAPS a turn_context-shaped payload is not the
    # newest turn: the real top-level turn_context before it still wins.
    _codex_env(env, [_turn(SOL), _event(**_turn("gpt-zzz"))])
    assert _build(env).line == CODEX_LINE
