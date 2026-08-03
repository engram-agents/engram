"""Tests for hooks/claude/_stophooklib.py (PR-A of the loop-arm-nudge spec --
shared Stop-hook prologue helpers).

Covers resolve_transcript_path(), read_offset_state(), write_offset_state(),
read_delta(), parse_jsonl_records(), walk_content_blocks(), and
emit_context() as pure/unit-testable functions, isolated from any real hook
via a fresh module load per test (mirrors tests/test_prompthooklib.py's
structure for the sibling _prompthooklib module).
"""

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "_stophooklib.py"
)


def _load_module():
    """Load _stophooklib as a fresh module instance, isolated per test."""
    spec = importlib.util.spec_from_file_location(
        "_stophooklib_test_instance", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_sys_path():
    """Snapshot/restore sys.path so a test's insertion doesn't leak."""
    before = list(sys.path)
    yield
    sys.path[:] = before


# ---------------------------------------------------------------------------
# resolve_transcript_path()
# ---------------------------------------------------------------------------


def test_resolve_transcript_path_prefers_existing_candidate(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    mod = _load_module()
    result = mod.resolve_transcript_path(str(transcript), session_id=None)
    assert result == str(transcript)


def test_resolve_transcript_path_falls_back_to_session_marker(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    real_transcript = tmp_path / "real.jsonl"
    real_transcript.write_text("", encoding="utf-8")
    (sessions_dir / "sess-1.json").write_text(
        json.dumps({"transcript_path": str(real_transcript)}), encoding="utf-8"
    )
    mod = _load_module()
    result = mod.resolve_transcript_path(
        None, session_id="sess-1", sessions_dir=str(sessions_dir)
    )
    assert result == str(real_transcript)


def test_resolve_transcript_path_candidate_missing_no_session_id(tmp_path):
    mod = _load_module()
    result = mod.resolve_transcript_path(
        str(tmp_path / "does-not-exist.jsonl"), session_id=None
    )
    assert result is None


def test_resolve_transcript_path_marker_missing_returns_none(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    mod = _load_module()
    result = mod.resolve_transcript_path(
        None, session_id="no-such-session", sessions_dir=str(sessions_dir)
    )
    assert result is None


def test_resolve_transcript_path_marker_corrupt_json_returns_none(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "sess-2.json").write_text("{not valid json", encoding="utf-8")
    mod = _load_module()
    result = mod.resolve_transcript_path(
        None, session_id="sess-2", sessions_dir=str(sessions_dir)
    )
    assert result is None


def test_resolve_transcript_path_marker_points_to_missing_file(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "sess-3.json").write_text(
        json.dumps({"transcript_path": str(tmp_path / "ghost.jsonl")}),
        encoding="utf-8",
    )
    mod = _load_module()
    result = mod.resolve_transcript_path(
        None, session_id="sess-3", sessions_dir=str(sessions_dir)
    )
    assert result is None


def test_resolve_transcript_path_no_candidate_no_session_id(tmp_path):
    mod = _load_module()
    result = mod.resolve_transcript_path(None, session_id=None)
    assert result is None


# ---------------------------------------------------------------------------
# read_offset_state() / write_offset_state()
# ---------------------------------------------------------------------------


def test_write_then_read_offset_state_roundtrip(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("hello world", encoding="utf-8")
    state_file = tmp_path / "state.json"
    mod = _load_module()

    mod.write_offset_state(str(state_file), str(transcript))
    stored_path, stored_offset = mod.read_offset_state(str(state_file))

    assert stored_path == str(transcript)
    assert stored_offset == len("hello world")


def test_write_offset_state_missing_transcript_writes_zero_offset(tmp_path):
    state_file = tmp_path / "state.json"
    mod = _load_module()

    mod.write_offset_state(str(state_file), str(tmp_path / "ghost.jsonl"))
    stored_path, stored_offset = mod.read_offset_state(str(state_file))

    assert stored_path == str(tmp_path / "ghost.jsonl")
    assert stored_offset == 0


def test_write_offset_state_is_atomic_tmp_replace(tmp_path):
    """After a write, no stray .tmp file is left behind."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("x", encoding="utf-8")
    state_file = tmp_path / "state.json"
    mod = _load_module()

    mod.write_offset_state(str(state_file), str(transcript))

    assert state_file.exists()
    assert not (tmp_path / "state.json.tmp").exists()


def test_read_offset_state_missing_file_raises(tmp_path):
    mod = _load_module()
    with pytest.raises(OSError):
        mod.read_offset_state(str(tmp_path / "no-such-state.json"))


def test_read_offset_state_corrupt_json_raises(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text("{not valid json", encoding="utf-8")
    mod = _load_module()
    with pytest.raises(json.JSONDecodeError):
        mod.read_offset_state(str(state_file))


def test_write_offset_state_never_raises_on_bad_path():
    """Best-effort write: an unwritable state-file path must not raise."""
    mod = _load_module()
    # A path under a nonexistent directory can never be created.
    mod.write_offset_state("/nonexistent-dir-xyz/state.json", "")
    # No exception -- that's the whole assertion.


# ---------------------------------------------------------------------------
# read_delta()
# ---------------------------------------------------------------------------


def test_read_delta_reads_exact_range(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("0123456789", encoding="utf-8")
    mod = _load_module()

    result = mod.read_delta(str(transcript), stored_offset=3, current_size=8)

    assert result == b"34567"


def test_read_delta_missing_file_raises_oserror(tmp_path):
    mod = _load_module()
    with pytest.raises(OSError):
        mod.read_delta(str(tmp_path / "ghost.jsonl"), 0, 10)


# ---------------------------------------------------------------------------
# parse_jsonl_records()
# ---------------------------------------------------------------------------


def test_parse_jsonl_records_all_valid():
    delta = (
        json.dumps({"type": "assistant", "n": 1}) + "\n"
        + json.dumps({"type": "user", "n": 2}) + "\n"
    ).encode("utf-8")
    mod = _load_module()

    records, any_record_parsed = mod.parse_jsonl_records(delta)

    assert any_record_parsed is True
    assert records == [{"type": "assistant", "n": 1}, {"type": "user", "n": 2}]


def test_parse_jsonl_records_skips_blank_lines():
    delta = b"\n\n" + json.dumps({"type": "assistant"}).encode("utf-8") + b"\n\n"
    mod = _load_module()

    records, any_record_parsed = mod.parse_jsonl_records(delta)

    assert any_record_parsed is True
    assert records == [{"type": "assistant"}]


def test_parse_jsonl_records_skips_unparseable_lines_but_keeps_valid():
    delta = (
        "{this is not json!!!\n"
        + json.dumps({"type": "assistant", "ok": True}) + "\n"
    ).encode("utf-8")
    mod = _load_module()

    records, any_record_parsed = mod.parse_jsonl_records(delta)

    assert any_record_parsed is True
    assert records == [{"type": "assistant", "ok": True}]


def test_parse_jsonl_records_all_garbage_any_record_parsed_false():
    delta = b"not json\nalso not json\n"
    mod = _load_module()

    records, any_record_parsed = mod.parse_jsonl_records(delta)

    assert any_record_parsed is False
    assert records == []


def test_parse_jsonl_records_empty_delta():
    mod = _load_module()
    records, any_record_parsed = mod.parse_jsonl_records(b"")
    assert records == []
    assert any_record_parsed is False


def test_parse_jsonl_records_non_dict_json_counts_toward_any_record_parsed():
    """A line that parses to a non-dict JSON value (e.g. a bare list) is
    dropped from `records` but still flips any_record_parsed True -- matches
    engram-stop-hook.py's pre-extraction ordering (any_record_parsed was set
    immediately after json.loads succeeded, before the isinstance(dict)
    check)."""
    delta = json.dumps([1, 2, 3]).encode("utf-8") + b"\n"
    mod = _load_module()

    records, any_record_parsed = mod.parse_jsonl_records(delta)

    assert records == []
    assert any_record_parsed is True


def test_parse_jsonl_records_decodes_bad_bytes_with_replace():
    """Malformed UTF-8 bytes must not raise -- errors='replace'. The bad
    bytes are on their own (garbage) line so they don't corrupt the
    following valid JSON line -- this test only asserts decoding doesn't
    raise and the valid line downstream still parses."""
    delta = (
        b"\xff\xfe\n"
        + json.dumps({"type": "assistant"}).encode("utf-8") + b"\n"
    )
    mod = _load_module()
    # Must not raise.
    records, any_record_parsed = mod.parse_jsonl_records(delta)
    assert any_record_parsed is True
    assert records == [{"type": "assistant"}]


# ---------------------------------------------------------------------------
# walk_content_blocks()
# ---------------------------------------------------------------------------


def test_walk_content_blocks_nonempty_string_synthesizes_text_block():
    mod = _load_module()
    result = mod.walk_content_blocks("hello")
    assert result == [{"type": "text", "text": "hello"}]


def test_walk_content_blocks_empty_string_yields_nothing():
    mod = _load_module()
    assert mod.walk_content_blocks("") == []


def test_walk_content_blocks_list_yields_dict_items_in_order():
    mod = _load_module()
    content = [
        {"type": "text", "text": "a"},
        {"type": "tool_use", "name": "Bash"},
    ]
    result = mod.walk_content_blocks(content)
    assert result == content


def test_walk_content_blocks_list_skips_non_dict_items():
    mod = _load_module()
    content = [{"type": "text", "text": "a"}, "not-a-dict", 42, None]
    result = mod.walk_content_blocks(content)
    assert result == [{"type": "text", "text": "a"}]


def test_walk_content_blocks_none_yields_nothing():
    mod = _load_module()
    assert mod.walk_content_blocks(None) == []


def test_walk_content_blocks_int_yields_nothing():
    mod = _load_module()
    assert mod.walk_content_blocks(42) == []


def test_walk_content_blocks_empty_list_yields_nothing():
    mod = _load_module()
    assert mod.walk_content_blocks([]) == []


# ---------------------------------------------------------------------------
# emit_context()
# ---------------------------------------------------------------------------


def test_emit_context_envelope_shape_uses_stop_event_name():
    """Distinct from _prompthooklib.emit_context: hookEventName is "Stop",
    not "UserPromptSubmit" -- the two are not interchangeable."""
    mod = _load_module()
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.emit_context("hello world")
    envelope = json.loads(buf.getvalue())
    assert envelope == {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": "hello world",
        }
    }


# ---------------------------------------------------------------------------
# Flattened deployed-topology trap (mandatory per _prompthooklib precedent)
# ---------------------------------------------------------------------------


def test_stophooklib_importable_in_flattened_deployed_topology(tmp_path, monkeypatch):
    """The plugin build FLATTENS hooks/claude/* -> hooks/* -- one level
    shallower than source. Recreate that layout (_stophooklib.py alone in a
    bare hooks/ dir) and confirm its exports still work correctly when
    imported at that nesting depth.
    """
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

    root = tmp_path / "engram"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "_stophooklib.py").write_text(MODULE_PATH.read_text())

    import subprocess

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("hello", encoding="utf-8")

    probe_script = (
        "import sys, io, json, contextlib\n"
        f"sys.path.insert(0, r'{root}/hooks')\n"
        "import _stophooklib as m\n"
        f"assert m.resolve_transcript_path(r'{transcript}') == r'{transcript}'\n"
        "print('resolve_ok')\n"
        "assert m.walk_content_blocks('x') == [{'type': 'text', 'text': 'x'}]\n"
        "print('walk_ok')\n"
        "records, any_parsed = m.parse_jsonl_records(b'{\"type\": \"assistant\"}\\n')\n"
        "assert any_parsed and records == [{'type': 'assistant'}]\n"
        "print('parse_ok')\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf):\n"
        "    m.emit_context('hi')\n"
        "envelope = json.loads(buf.getvalue())\n"
        "expected = {'hookSpecificOutput': {'hookEventName': 'Stop', 'additionalContext': 'hi'}}\n"
        "assert envelope == expected\n"
        "print('emit_ok')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", probe_script],
        capture_output=True, text=True,
        env={k: v for k, v in __import__("os").environ.items() if k != "CLAUDE_PLUGIN_ROOT"},
    )
    assert "resolve_ok" in r.stdout, (
        f"_stophooklib.resolve_transcript_path failed in flattened topology "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "walk_ok" in r.stdout, (
        f"_stophooklib.walk_content_blocks failed in flattened topology "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "parse_ok" in r.stdout, (
        f"_stophooklib.parse_jsonl_records failed in flattened topology "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
    assert "emit_ok" in r.stdout, (
        f"_stophooklib.emit_context envelope diverged in flattened topology "
        f"(stdout={r.stdout!r} stderr={r.stderr!r})"
    )
