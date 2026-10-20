"""Contract tests for the lineage-transparent signature (engram-alpha#3450).

Written BEFORE the implementation (contract-first, CLAUDE.md).  The contract:
one function ``engram_signature.build_signature`` shared by the ``engram_sign``
MCP tool and ``tools/engram-sign.py``; refuse-and-say-so on ANY unreadable
field (never a placeholder); read-only; no network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

import engram_core  # noqa: E402
import engram_signature as sig  # noqa: E402

CLI = REPO_ROOT / "src" / "engram" / "tools" / "engram-sign.py"

VERSION = "0.5.2-dev.20261004230859.dc232478d"
HAPPY_LINE = (
    f"\U0001fa90 Kepler Somnium · Fable 5.1 (claude-fable-5-1) · "
    f"ENGRAM {VERSION} · Lei Shi's agent"
)
ID_ONLY_LINE = (
    f"\U0001fa90 Kepler Somnium · claude-fable-5-1 · "
    f"ENGRAM {VERSION} · Lei Shi's agent"
)

CONFIG = {
    "signature_emoji": "\U0001fa90",
    "display_name": "Kepler Somnium",
    "primary_user": "Lei Shi",
    "self_lineage": "anthropic:fable",
}


def _model_row(model_id="claude-fable-5-1", name="Fable 5.1"):
    return {"attachment": {"type": "model", "identity": {
        "modelId": model_id, "marketingName": name,
        "knowledgeCutoff": "June 2026"}}}


def _asst_row(model_id="claude-fable-5-1"):
    return {"type": "assistant", "message": {"model": model_id, "content": []}}


def _write_jsonl(path: Path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


_SID_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
            "ANTHROPIC_SESSION_ID")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _SID_ENV + ("CLAUDE_PLUGIN_ROOT",):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def env(tmp_path):
    """A complete valid fixture: data_dir, plugin_root, transcript."""
    data = tmp_path / "data"
    (data / "sessions").mkdir(parents=True)
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(json.dumps({"version": VERSION}))
    (plugin / "server.py").write_text("")
    tr = tmp_path / "t.jsonl"
    _write_jsonl(tr, [
        {"type": "user", "message": {"content": "hi"}},
        _asst_row(),
        _model_row(),
        _asst_row(),
    ])
    (data / "config.json").write_text(json.dumps(CONFIG))
    marker = {"session_id": "s1", "transcript_path": str(tr),
              "session_started_at": "2026-10-06T12:15:02.867379+00:00"}
    (data / "sessions" / "s1.json").write_text(json.dumps(marker))
    return type("Env", (), dict(data=data, plugin=plugin, transcript=tr,
                                tmp=tmp_path))


def _set_config(env, **overrides):
    cfg = dict(CONFIG)
    for k, v in overrides.items():
        if v is _DEL:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    (env.data / "config.json").write_text(json.dumps(cfg))


_DEL = object()


def _build(env, **kw):
    # There is no marker-guessing fallback: every call names its session.
    # Default to the fixture's "s1"; pass session_id=None to test resolution.
    kw.setdefault("session_id", "s1")
    return sig.build_signature(env.data, plugin_root=env.plugin, **kw)


def _cli(env, *extra, sid="s1", extra_env=None):
    e = {k: v for k, v in os.environ.items()
         if k not in _SID_ENV + ("CLAUDE_PLUGIN_ROOT",)}
    e.update(extra_env or {})
    sid_args = ["--session-id", sid] if sid else []
    return subprocess.run(
        [sys.executable, str(CLI), "--data-dir", str(env.data),
         "--plugin-root", str(env.plugin), *sid_args, *extra],
        capture_output=True, text=True, env=e)


def _tree(d: Path):
    return sorted((str(p.relative_to(d)), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in d.rglob("*") if p.is_file())


# --------------------------------------------------------------- happy path

def test_happy_path_byte_exact(env):
    r = _build(env)
    assert r.ok is True
    assert r.line == HAPPY_LINE
    assert r.field is None and r.reason is None


def test_cli_stdout_equals_library_line(env):
    r = _build(env)
    p = _cli(env)
    assert p.returncode == 0, p.stderr
    assert p.stdout.rstrip("\n") == r.line == HAPPY_LINE


def test_session_id_param_selects_marker(env):
    other = env.tmp / "o.jsonl"
    _write_jsonl(other, [_model_row("claude-fable-5-0", "Fable 5.0")])
    (env.data / "sessions" / "s2.json").write_text(json.dumps({
        "session_id": "s2", "transcript_path": str(other),
        "session_started_at": "2020-01-01T00:00:00+00:00"}))
    r = _build(env, session_id="s2")
    assert r.ok and "Fable 5.0 (claude-fable-5-0)" in r.line


@pytest.mark.parametrize("name", list(_SID_ENV))
def test_env_session_id_used(env, monkeypatch, name):
    other = env.tmp / "o.jsonl"
    _write_jsonl(other, [_model_row("claude-fable-5-0", "Fable 5.0")])
    (env.data / "sessions" / "s2.json").write_text(json.dumps({
        "session_id": "s2", "transcript_path": str(other),
        "session_started_at": "2020-01-01T00:00:00+00:00"}))
    monkeypatch.setenv(name, "s2")
    assert "claude-fable-5-0" in _build(env, session_id=None).line
    p = _cli(env, sid=None, extra_env={name: "s2"})
    assert p.returncode == 0, p.stderr
    assert "claude-fable-5-0" in p.stdout


def test_env_name_order(env, monkeypatch):
    # CLAUDE_CODE_SESSION_ID -> CLAUDE_SESSION_ID -> ANTHROPIC_SESSION_ID
    for n, var, mid in (("a", "CLAUDE_CODE_SESSION_ID", "claude-fable-5-7"),
                        ("b", "CLAUDE_SESSION_ID", "claude-fable-5-8"),
                        ("c", "ANTHROPIC_SESSION_ID", "claude-fable-5-9")):
        tr = env.tmp / f"{n}.jsonl"
        _write_jsonl(tr, [_model_row(mid, n)])
        (env.data / "sessions" / f"{n}.json").write_text(json.dumps(
            {"session_id": n, "transcript_path": str(tr)}))
        monkeypatch.setenv(var, n)
    assert "(claude-fable-5-7)" in _build(env, session_id=None).line
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert "(claude-fable-5-8)" in _build(env, session_id=None).line
    monkeypatch.delenv("CLAUDE_SESSION_ID")
    assert "(claude-fable-5-9)" in _build(env, session_id=None).line


def _two_sessions(env):
    """Borges's fixture: opus seat, A=sonnet (older), B=opus (newer)."""
    _set_config(env, self_lineage="anthropic:opus")
    ta, tb = env.tmp / "a.jsonl", env.tmp / "b.jsonl"
    _write_jsonl(ta, [_model_row("claude-sonnet-5-5", "Sonnet 5.5")])
    _write_jsonl(tb, [_model_row("claude-opus-5-5", "Opus 5.5")])
    for sid, tp, ts in (("A", ta, "2026-10-06T12:00:00+00:00"),
                        ("B", tb, "2026-10-06T12:30:00+00:00")):
        (env.data / "sessions" / f"{sid}.json").write_text(json.dumps(
            {"session_id": sid, "transcript_path": str(tp),
             "session_started_at": ts}))
    (env.data / "sessions" / "s1.json").unlink()


def test_two_sessions_env_follows_caller_not_newest(env, monkeypatch):
    _two_sessions(env)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "A")
    r = _build(env, session_id=None)
    assert r.ok is False and r.field == "model_family"
    assert "anthropic:sonnet" in r.reason and "anthropic:opus" in r.reason
    p = _cli(env, sid=None, extra_env={"CLAUDE_CODE_SESSION_ID": "A"})
    assert p.returncode == 2 and p.stdout == ""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "B")
    r = _build(env, session_id=None)
    assert r.ok and "Opus 5.5 (claude-opus-5-5)" in r.line


def test_two_sessions_no_id_refuses_never_guesses(env):
    _two_sessions(env)
    r = _build(env, session_id=None)
    assert r.ok is False and r.line is None and r.field == "session_marker"
    assert "CLAUDE_CODE_SESSION_ID" in r.reason and "session_id" in r.reason
    p = _cli(env, sid=None)
    assert p.returncode == 2 and p.stdout == ""
    assert "session_marker" in p.stderr and "CLAUDE_CODE_SESSION_ID" in p.stderr


def test_two_sessions_payload_beats_env(env, monkeypatch):
    _two_sessions(env)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "A")
    r = _build(env, session_id="B")
    assert r.ok and "claude-opus-5-5" in r.line


def test_blank_session_id_falls_through_to_env(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    assert _build(env, session_id="  ").line == HAPPY_LINE


def test_newest_model_attachment_row_wins(env):
    _write_jsonl(env.transcript, [_model_row("claude-fable-5-0", "Fable 5.0"),
                                  _model_row("claude-fable-5-1", "Fable 5.1")])
    assert _build(env).line == HAPPY_LINE


# ------------------------------------------------------ fallback + ordering

def test_fallback_assistant_message_model_id_only(env):
    _write_jsonl(env.transcript, [{"type": "user", "message": {"content": "x"}},
                                  _asst_row()])
    r = _build(env)
    assert r.ok and r.line == ID_ONLY_LINE


def test_fallback_skips_synthetic_newest_row(env):
    _write_jsonl(env.transcript, [{"type": "user", "message": {"content": "x"}},
                                  _asst_row("claude-fable-5-1"),
                                  _asst_row("<synthetic>")])
    r = _build(env)
    assert r.ok and r.line == ID_ONLY_LINE


def test_attachment_preferred_over_newer_assistant_row(env):
    # attachment row FIRST, assistant row after: attachment must still win
    _write_jsonl(env.transcript, [_model_row(), _asst_row("claude-fable-5-1")])
    assert _build(env).line == HAPPY_LINE


# --------------------------------------------------------------- version

def _write_stamp(env, text):
    (env.data / ".deployed-version").write_text(text)


def test_version_tagged_clean_adds_alpha_tag(env):
    _write_stamp(env, "alpha_sha=abc\nalpha_tag=deploy/2026-10-04-x\n"
                      "alpha_dirty=clean\n")
    assert _build(env).line == HAPPY_LINE.replace(
        VERSION, f"{VERSION} (deploy/2026-10-04-x)")


def test_version_dirty_no_suffix(env):
    _write_stamp(env, "alpha_tag=deploy/x\nalpha_dirty=dirty\n")
    assert _build(env).line == HAPPY_LINE


def test_version_empty_tag_no_suffix(env):
    _write_stamp(env, "alpha_tag=\nalpha_dirty=clean\n")
    assert _build(env).line == HAPPY_LINE


def test_deployed_version_absent_is_ok(env):
    assert not (env.data / ".deployed-version").exists()
    r = _build(env)
    assert r.ok and r.line == HAPPY_LINE


# ---------------------------------------------------------------- refusals

def _refused(env, field, fragment=None):
    r = _build(env)
    assert r.ok is False
    assert r.line is None
    assert r.field == field
    assert r.reason
    if fragment:
        assert fragment in r.reason
    p = _cli(env)
    assert p.returncode == 2
    assert p.stdout == ""
    assert field in p.stderr
    return r


@pytest.mark.parametrize("field", ["signature_emoji", "display_name",
                                   "primary_user", "self_lineage"])
@pytest.mark.parametrize("how", ["missing", "empty"])
def test_config_field_refusals(env, field, how):
    _set_config(env, **{field: _DEL if how == "missing" else "  "})
    _refused(env, field, "config.json")


def test_display_name_null_refuses(env):
    _set_config(env, display_name=None)
    _refused(env, "display_name")


def test_no_session_marker(env):
    for p in (env.data / "sessions").glob("*.json"):
        p.unlink()
    _refused(env, "session_marker")


def test_no_session_id_at_all_refuses(env):
    r = _build(env, session_id=None)
    assert r.ok is False and r.field == "session_marker" and r.line is None
    p = _cli(env, sid=None)
    assert p.returncode == 2 and p.stdout == ""
    assert "session_marker" in p.stderr


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", ".."])
def test_invalid_session_id_refuses(env, bad):
    r = _build(env, session_id=bad)
    assert r.ok is False and r.field == "session_marker"


def test_marker_without_transcript_path(env):
    (env.data / "sessions" / "s1.json").write_text(json.dumps({
        "session_id": "s1", "session_started_at": "2026-10-06T12:15:02+00:00"}))
    _refused(env, "transcript_path")


def test_transcript_unreadable(env):
    env.transcript.unlink()
    _refused(env, "transcript", str(env.transcript))


def test_transcript_with_no_model_rows(env):
    _write_jsonl(env.transcript, [{"type": "user", "message": {"content": "x"}}])
    _refused(env, "model")


def test_plugin_version_file_missing(env):
    (env.plugin / "plugin.json").unlink()
    _refused(env, "version", str(env.plugin / "plugin.json"))


@pytest.mark.parametrize("body", ['{"version": ""}', '{}', 'not json'])
def test_plugin_version_empty_or_malformed(env, body):
    (env.plugin / "plugin.json").write_text(body)
    _refused(env, "version")


def test_version_helper_raising_refuses(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(sig, "_read_plugin_version", boom)
    r = _build(env)
    assert r.ok is False and r.field == "version" and r.line is None


def test_family_mismatch_names_both(env):
    _write_jsonl(env.transcript, [_model_row("claude-opus-5-5", "Opus 5.5")])
    r = _refused(env, "model_family")
    assert "anthropic:opus" in r.reason and "anthropic:fable" in r.reason


def test_family_check_reads_config_self_lineage_ok(env):
    # self_lineage is varied: an opus seat on an opus transcript signs.
    _set_config(env, self_lineage="anthropic:opus")
    _write_jsonl(env.transcript, [_model_row("claude-opus-5-5", "Opus 5.5")])
    r = _build(env)
    assert r.ok is True
    assert "Opus 5.5 (claude-opus-5-5)" in r.line


def test_family_check_reads_config_self_lineage_refuses(env):
    # an opus seat on the (default) fable transcript is refused.
    _set_config(env, self_lineage="anthropic:opus")
    r = _refused(env, "model_family")
    assert "anthropic:fable" in r.reason and "anthropic:opus" in r.reason


def test_unmappable_model_id_refuses(env):
    _write_jsonl(env.transcript, [_model_row("gpt-9", "GPT 9")])
    r = _refused(env, "model_family")
    assert "gpt-9" in r.reason
    assert "does not map to a provider:family lineage" in r.reason


# ------------------------------------------------- plugin-root discovery

def test_plugin_root_from_env(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env.plugin))
    assert sig.build_signature(env.data, session_id="s1").line == HAPPY_LINE


def test_plugin_root_discovery_failure_names_path(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env.tmp / "nowhere"))
    r = sig.build_signature(env.data, session_id="s1")
    assert r.ok is False and r.field == "version"
    assert "plugin.json" in r.reason


# ------------------------------------------------------ read-only, no net

def test_read_only(env):
    before = _tree(env.data)
    assert _build(env).ok
    _cli(env)
    assert _tree(env.data) == before


def test_no_network(env, monkeypatch):
    import socket

    def deny(*a, **k):
        raise AssertionError("network touched")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    assert _build(env).ok


# ------------------------------------------------------- model_id_to_lineage

@pytest.mark.parametrize("mid,expected", [
    ("claude-fable-5-1", "anthropic:fable"),
    ("claude-opus-4-7[1m]", "anthropic:opus"),
    ("claude-haiku-4-5-20251001", "anthropic:haiku"),
    ("gpt-9", ""),
])
def test_model_id_to_lineage(mid, expected):
    assert engram_core.model_id_to_lineage(mid) == expected


# #3922: explicit exact-id table of supported non-Claude models; the success
# control lives in the SAME table as the refuse rows.
@pytest.mark.parametrize("mid,expected", [
    ("gpt-6.1-sol", "openai:gpt"),
    ("gpt-6.1-sol ", "openai:gpt"),         # trimmed -> accepted
    ("gpt-6.1-sol[1m]", "openai:gpt"),      # existing [...] suffix strip
    ("gpt-9999", ""),
    ("o5-preview", ""),
    ("gpt-6.1-soltypo", ""),
    ("GPT-6.1-SOL", ""),
    ("", ""),
    ("   ", ""),
    (None, ""),
    (61, ""),
])
def test_model_id_to_lineage_non_claude_table(mid, expected):
    assert engram_core.model_id_to_lineage(mid) == expected


# ---------------------------------------------------------------- MCP tool

def _fresh_server(td):
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(td)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def test_mcp_tool_ok_and_cli_parity(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env.plugin))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")  # server-process env
    server = _fresh_server(str(env.data))
    out = json.loads(server.engram_sign("{}"))
    assert out["status"] == "ok"
    assert out["line"] == HAPPY_LINE == _cli(env).stdout.rstrip("\n")
    assert out["parts"]["model_id"] == "claude-fable-5-1"
    assert json.loads(server.engram_sign(json.dumps({"session_id": "s1"})))["line"] == HAPPY_LINE
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    out = json.loads(server.engram_sign("{}"))
    assert out["status"] == "refused" and out["field"] == "session_marker"


def test_mcp_tool_refused_shape(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env.plugin))
    _set_config(env, signature_emoji=_DEL)
    server = _fresh_server(str(env.data))
    out = json.loads(server.engram_sign("{}"))
    assert out["status"] == "refused"
    assert out["field"] == "signature_emoji"
    assert out["reason"] and "line" not in out


def test_mcp_tool_bad_payload(env):
    server = _fresh_server(str(env.data))
    out = json.loads(server.engram_sign("not json"))
    assert "error" in out
