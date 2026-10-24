"""Tests for the write-time quote-length floor (engram-alpha#4134, part of
#4050): `engram_add_observation` refuses a quote under MIN_QUOTE_CHARS (10)
stripped characters unless the stripped quote is the whole source unit (a
line of a file, or the whole text of one transcript message). The check runs
after the verbatim verification and before any node (observation OR evidence)
is created. A source whose text cannot be read fails closed.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_min_quote_4134_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))
TEST_FILE = os.path.join(TEST_DIR, "min_quote_source.md")

SOURCE_LINES = [
    "alpha beta gamma delta epsilon",
    "ALL GREEN",
    "pad   abc   pad and more words here",
    "前置 日本語のテキストだよ 後置",
    "the quick brown fox jumps over",
]


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    with open(TEST_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(SOURCE_LINES) + "\n")
    return server


def _payload(quote, **overrides):
    payload = {
        "url": f"file://{TEST_FILE}",
        "title": "Min quote test source",
        "quoted_text": quote,
        "interpretation": "interp text",
        "claim": "claim text",
        "quote_type": "attributed_analysis",
    }
    payload.update(overrides)
    return payload


def _add(server, quote, **overrides):
    return json.loads(server.engram_add_observation(
        payload_json=json.dumps(_payload(quote, **overrides))))


def _node_counts(server):
    conn = server.core._get_db()
    try:
        obs = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE type LIKE 'observation%'").fetchone()[0]
        ev = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE type = 'evidence'").fetchone()[0]
        return obs, ev
    finally:
        conn.close()


def _write_session(server, session_id, messages):
    """A small transcript (standalone file, outside any git repo) + marker."""
    tdir = tempfile.mkdtemp(prefix="engram_test_transcript_4134_", dir=TEST_DIR)
    path = os.path.join(tdir, "session.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in messages:
            f.write(json.dumps(line) + "\n")
    sessions_dir = server.core.DATA_DIR / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    with open(sessions_dir / f"{session_id}.json", "w") as f:
        json.dump({"session_id": session_id, "transcript_path": path,
                   "started_at": "2026-10-24T12:00:00+00:00"}, f)
    return path


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text):
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _session_add(server, session_id, quote):
    return json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": quote, "interpretation": "interp text",
        "claim": "claim text", "session_id": session_id,
    })))


def test_C1_nine_char_fragment_refused_with_containing_line():
    server = fresh_server()
    r = _add(server, "beta gamm")  # 9 chars, inside line 1
    assert "error" in r and r.get("status") != "created", r
    err = r["error"]
    assert "10" in err and "9" in err, err
    assert SOURCE_LINES[0] in err, err
    assert "quote enough of that line (or sentence) that it tells something on its own" in err
    assert "the evidence node keeps the full context" in err
    assert _node_counts(server) == (0, 0)


def test_C2_ten_char_fragment_accepted():
    server = fresh_server()
    r = _add(server, "beta gamma")  # 10 chars
    assert r.get("status") == "created", r


def test_C3_session_whole_message_exception():
    server = fresh_server()
    _write_session(server, "sess-c3", [
        _user("Go."),
        _assistant("Understood, starting the long task now."),
    ])
    r = _session_add(server, "sess-c3", "Go.")  # 3 chars == whole message
    assert r.get("status") == "created", r


def test_C4_file_whole_line_exception():
    server = fresh_server()
    r = _add(server, "ALL GREEN")  # 9 chars == a whole line
    assert r.get("status") == "created", r


def test_C5_short_quote_not_in_source_gets_verbatim_error():
    server = fresh_server()
    r = _add(server, "zzzzzzzzz")
    assert "error" in r, r
    assert "not found" in r["error"]
    assert "verbatim substring" in r["error"]
    assert "minimum" not in r["error"] and "characters" not in r["error"].split("\n")[0]
    assert _node_counts(server) == (0, 0)


def test_C6_batch_refusal_is_per_item():
    server = fresh_server()
    obs = [
        {"quoted_text": "beta gamma", "interpretation": "i1",
         "claim": "claim one", "quote_type": "attributed_analysis"},
        {"quoted_text": "beta gamm", "interpretation": "i2",
         "claim": "claim two", "quote_type": "attributed_analysis"},
        {"quoted_text": "the quick brown fox", "interpretation": "i3",
         "claim": "claim three", "quote_type": "attributed_analysis"},
    ]
    payload = {"observations_json": json.dumps(obs), "url": f"file://{TEST_FILE}",
               "title": "Min quote batch source"}
    r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
    assert r.get("created") == 2, r
    by_index = {x["index"]: x for x in r["results"]}
    assert by_index[0].get("status") == "created", by_index[0]
    assert by_index[2].get("status") == "created", by_index[2]
    assert "error" in by_index[1] and by_index[1].get("status") != "created"
    assert "10" in by_index[1]["error"] and SOURCE_LINES[0] in by_index[1]["error"]
    assert _node_counts(server)[0] == 2


def test_C7_unreadable_https_source_fails_closed():
    server = fresh_server()
    r = _add(server, "abcde", url="https://example.com/min-quote-test")
    assert "error" in r and r.get("status") != "created", r
    err = r["error"]
    assert "not available" in err, err
    assert "or snapshot the source and cite the file:// copy" in err, err
    assert "10" in err and "5" in err
    assert _node_counts(server) == (0, 0)  # not even an evidence node


def test_C8_session_fragment_refused_shows_decoded_message():
    server = fresh_server()
    msg = 'He said "the quick brown fox"\nthen he left the room'
    _write_session(server, "sess-c8", [_user(msg), _assistant("ok")])
    r = _session_add(server, "sess-c8", "quick")  # 5 chars, fragment
    assert "error" in r and r.get("status") != "created", r
    err = r["error"]
    assert 'He said "the quick brown fox"\nthen he left the room' in err, err
    assert '\\"' not in err and "\\n" not in err, err
    assert "5" in err and "10" in err
    assert _node_counts(server) == (0, 0)


def test_C9_length_measured_on_stripped_string():
    server = fresh_server()
    r = _add(server, "   abc   ")  # 9 chars raw, 3 stripped; fragment of line 3
    assert "error" in r and r.get("status") != "created", r
    assert "3" in r["error"] and SOURCE_LINES[2] in r["error"], r["error"]


def test_C10_constant_is_ten_and_text_renders_from_it(monkeypatch):
    server = fresh_server()
    obs_mod = sys.modules["engram_observation"]
    assert obs_mod.MIN_QUOTE_CHARS == 10
    monkeypatch.setattr(obs_mod, "MIN_QUOTE_CHARS", 12)
    r = _add(server, "beta gamma")  # 10 chars: now under the patched floor
    assert "error" in r, r
    assert "12" in r["error"], r["error"]


def test_C11_characters_not_bytes():
    server = fresh_server()
    quote = "日本語のテキストだ"  # 9 characters, 27 UTF-8 bytes
    assert len(quote) == 9 and len(quote.encode("utf-8")) > 10
    r = _add(server, quote)
    assert "error" in r and r.get("status") != "created", r
    assert "9" in r["error"]


def test_C12_evidence_id_path_applies_same_rules():
    server = fresh_server()
    first = _add(server, "beta gamma")
    assert first.get("status") == "created", first
    ev_id = first["evidence_id"]
    payload = {"evidence_id": ev_id, "quoted_text": "beta gamm",
               "interpretation": "i", "claim": "another claim",
               "quote_type": "attributed_analysis"}
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" in r and r.get("status") != "created", r
    assert SOURCE_LINES[0] in r["error"]
    payload["quoted_text"] = "the quick brown"
    r2 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r2.get("status") == "created", r2


# ---- round 2 (reviewer round 1) ------------------------------------------

def _write_file(name, text):
    path = os.path.join(TEST_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_C13_zero_width_space_quote_refused_on_blank_line_file():
    server = fresh_server()
    path = _write_file("blank_line_source.md",
                       "first line with words\n\nthird line with words\n")
    for quote in ("​", "​​"):
        r = _add(server, quote, url=f"file://{path}")
        assert "error" in r and r.get("status") != "created", (quote, r)
    assert _node_counts(server) == (0, 0)


def test_C14_blank_after_normalization_unit_is_never_a_match():
    server = fresh_server()
    # a line that is only a zero-width space, and a whitespace-only line
    path = _write_file("zw_line_source.md",
                       "first line with words\n​\n   \nthird line with words\n")
    r = _add(server, "​", url=f"file://{path}")
    assert "error" in r and r.get("status") != "created", r
    # a visible single char cannot ride on those blank units
    r2 = _add(server, "Z", url=f"file://{path}")
    assert "error" in r2 and r2.get("status") != "created", r2
    assert _node_counts(server) == (0, 0)


def _realistic_transcript(server, session_id):
    return _write_session(server, session_id, [
        {"type": "queue-operation", "operation": "enqueue", "content": "qop"},
        {"type": "system", "subtype": "info", "content": "sys"},
        {"type": "summary", "summary": "smy", "leafUuid": "x"},
        _user("Yes"),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Working on it now, one moment."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "tro"}]}},
    ])


def test_C15_queue_operation_text_is_not_a_unit():
    server = fresh_server()
    _realistic_transcript(server, "sess-c15")
    r = _session_add(server, "sess-c15", "qop")
    assert "error" in r and r.get("status") != "created", r
    assert _node_counts(server) == (0, 0)


def test_C16_system_record_text_is_not_a_unit():
    server = fresh_server()
    _realistic_transcript(server, "sess-c16")
    r = _session_add(server, "sess-c16", "sys")
    assert "error" in r and r.get("status") != "created", r
    r2 = _session_add(server, "sess-c16", "smy")
    assert "error" in r2 and r2.get("status") != "created", r2


def test_C17_user_message_whole_text_accepted_in_realistic_transcript():
    server = fresh_server()
    _realistic_transcript(server, "sess-c17")
    r = _session_add(server, "sess-c17", "Yes")
    assert r.get("status") == "created", r


def test_C18_transcript_error_names_which_units_count():
    server = fresh_server()
    _realistic_transcript(server, "sess-c18")
    r = _session_add(server, "sess-c18", "tro")  # tool_result text
    assert "error" in r and r.get("status") != "created", r
    err = r["error"]
    assert "only user and assistant message text counts" in err, err
    assert "tool output and system records do not" in err, err
    assert "surrounding message text" in err, err
