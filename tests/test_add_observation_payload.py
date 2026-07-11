"""Tests for the engram_add_observation single-payload signature (ob_1499 + ls_0023).

The MCP-exposed engram_add_observation now takes one parameter:
    payload_json: str  (a JSON object with the observation fields)

This eliminates the antml-prefix multi-parameter swallow risk that the
N-parameter signature was prone to (~26% of add_observation calls were
auto-repaired by hooks/engram-toolcall-repair.py per 7-day telemetry as
of 2026-05-08). Internal callers continue to use _add_observation_impl
with named kwargs.

Coverage:
  1. Happy path — well-formed payload creates an observation.
  2. Schema enforcement — invalid JSON, non-object payload, unknown fields.
  3. Required-field validation — missing required fields are reported.
  4. Field validation — invalid quote_type / source_class still rejected.
  5. Internal helper — _add_observation_impl works with named kwargs.
  6. Client backward compatibility — EngramClient.call auto-wraps legacy
     dict form into payload_json so pre-migration callers keep working.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_payload_obs_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


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
    return server


# ── 1. Happy path ──────────────────────────────────────────────────────────


def test_payload_json_creates_observation():
    server = fresh_server()
    payload = {
        "url": "https://example.com/article",
        "title": "Example",
        "quoted_text": "X says Y",
        "interpretation": "X claims that Y is true",
        "claim": "Y is true according to X",
        "quote_type": "attributed_analysis",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert r["observation_id"].startswith("ob_"), r
    assert r.get("evidence_id", "").startswith("ev_"), r


# ── 2. Schema enforcement ──────────────────────────────────────────────────


def test_payload_json_invalid_json_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json="{not-json}"))
    assert "error" in r and "Invalid JSON" in r["error"], r


def test_payload_json_non_object_rejected():
    server = fresh_server()
    # JSON array is valid JSON but not an object → reject
    r = json.loads(server.engram_add_observation(payload_json="[1, 2, 3]"))
    assert "error" in r and "must be a JSON object" in r["error"], r


def test_payload_json_unknown_field_rejected():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data", "url": "https://x.com", "title": "T",
        "made_up_field": "value",
    })))
    assert "error" in r and "Unknown fields" in r["error"], r
    assert "made_up_field" in r["error"], r


# ── 3. Required-field validation ───────────────────────────────────────────


def test_payload_json_missing_required():
    server = fresh_server()
    # Empty payload — missing all four required fields
    r = json.loads(server.engram_add_observation(payload_json="{}"))
    assert "error" in r and "Missing required fields" in r["error"], r
    for field in ("quoted_text", "interpretation", "claim", "quote_type"):
        assert field in r["error"], f"{field} not mentioned: {r['error']}"


def test_payload_json_partial_missing():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q",
        "interpretation": "i",
        # claim + quote_type missing
        "url": "https://x.com",
        "title": "T",
    })))
    assert "error" in r and "Missing required fields" in r["error"], r
    assert "claim" in r["error"] and "quote_type" in r["error"], r["error"]


# ── 4. Field-value validation (still applies through impl) ────────────────


def test_payload_json_invalid_quote_type():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "fabricated_type",
        "url": "https://x.com", "title": "T",
    })))
    assert "error" in r and "Invalid quote_type" in r["error"], r


def test_payload_json_invalid_source_class():
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data",
        "url": "https://x.com", "title": "T",
        "source_class": "made_up_class",
    })))
    assert "error" in r and "Invalid source_class" in r["error"], r


def test_payload_json_explicit_empty_source_class_still_rejected():
    """Regression for the None-sentinel fix: an EXPLICIT source_class=""
    must still hit the VALID_SOURCE_CLASSES rejection (matching dev
    behavior) and create no node — it must NOT be silently defaulted to
    'external' the way a truly-omitted source_class is. Only true omission
    (the field absent from the payload) should trigger the ergonomic
    default; an explicit empty string is a caller error, not an omission."""
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data",
        "url": "https://x.com", "title": "T",
        "source_class": "",
    })))
    assert "error" in r and "Invalid source_class" in r["error"], r

    conn = server._get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type LIKE 'observation%'"
    ).fetchone()["c"]
    conn.close()
    assert count == 0, f"Expected no observation node created, found {count}"


def test_payload_json_missing_evidence_source():
    """Neither url nor evidence_id provided → impl returns error."""
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data",
        # No url, no evidence_id
    })))
    assert "error" in r, r
    assert "url" in r["error"] or "evidence_id" in r["error"], r["error"]


# ── 5. Internal impl callable with kwargs (in-server callers) ─────────────


def test_internal_impl_accepts_kwargs():
    server = fresh_server()
    r = json.loads(server._add_observation_impl(
        url="https://example.com/internal",
        title="Internal call",
        quoted_text="internal payload",
        interpretation="exercising the impl directly",
        claim="impl callable with kwargs",
        quote_type="attributed_analysis",
    ))
    assert r.get("status") == "created", r


# ── 6. EngramClient backward compatibility ────────────────────────────────


def test_client_call_legacy_dict_autowrapped():
    """EngramClient.call should auto-wrap legacy named-field dict for tools
    whose schema declares only payload_json. This keeps pre-migration
    callers working without each having to know about the JSON shape."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    client = EngramClient(db_dir=TEST_DIR)

    # Pass legacy named-field dict — should auto-wrap into payload_json
    r = client.call("engram_add_observation", {
        "url": "https://example.com/legacy",
        "title": "Legacy form",
        "quoted_text": "still works",
        "interpretation": "legacy callers preserved",
        "claim": "Legacy dict form auto-wrapped",
        "quote_type": "hard_data",
    })
    assert r.get("status") == "created", f"Legacy dict should auto-wrap: {r}"


def test_client_call_explicit_payload_json_passthrough():
    """When the caller already passes payload_json, don't double-wrap."""
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    from engram_client import EngramClient
    client = EngramClient(db_dir=TEST_DIR)

    r = client.call("engram_add_observation", {
        "payload_json": json.dumps({
            "url": "https://example.com/explicit",
            "title": "Explicit form",
            "quoted_text": "passes through unchanged",
            "interpretation": "no double-wrap",
            "claim": "Explicit payload_json passthrough",
            "quote_type": "hard_data",
        }),
    })
    assert r.get("status") == "created", f"Explicit payload_json should pass through: {r}"


# ── 7. source_repo field ───────────────────────────────────────────────────


def test_source_repo_accepted_as_known_field():
    """source_repo and repo_path are in _ADD_OBSERVATION_FIELDS — must not be rejected as unknown."""
    server = fresh_server()
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "quote_type": "hard_data",
        "url": "https://example.com/doc",
        "title": "T",
        "source_repo": "",
        "repo_path": "",
    })))
    assert "Unknown fields" not in r.get("error", ""), (
        f"source_repo/repo_path should be known fields, not rejected as unknown: {r}"
    )


def test_source_repo_nonexistent_dir_rejected():
    """source_repo pointing to a non-existent path returns a 'not a directory' error."""
    server = fresh_server()
    content = "The rate of Y is 42%."
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        f.write(content)
        f.close()
        tmp_path = f.name
        r = json.loads(server.engram_add_observation(payload_json=json.dumps({
            "quoted_text": content,
            "interpretation": "Known rate.",
            "claim": "Rate of Y is 42%",
            "quote_type": "hard_data",
            "url": f"file://{tmp_path}",
            "title": "Test temp file",
            "source_repo": "/nonexistent/no/such/dir",
        })))
        assert "error" in r, f"Expected error for bad source_repo, got: {r}"
        assert "not a directory" in r["error"], (
            f"Expected 'not a directory' in error message: {r['error']}"
        )
    finally:
        os.unlink(f.name)


# ── 8. session_id param (#1764) ─────────────────────────────────────────────
#
# session_id lets an agent citing the CURRENT chat pass its injected session
# id instead of hand-constructing the JSONL file:// url + title. The server
# resolves session_id -> <DATA_DIR>/sessions/<session_id>.json ->
# transcript_path, then feeds that url into the EXISTING evidence-node +
# verbatim-quote path unchanged.


def _write_session_marker(server, session_id, transcript_path, started_at="2026-07-10T12:00:00+00:00"):
    """Write a session marker file under the fresh_server()'s DATA_DIR,
    mirroring the shape written by engram-session-start-hook.py's
    write_marker(): {session_id, transcript_path, started_at, ...}."""
    sessions_dir = server.core.DATA_DIR / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "started_at": started_at,
    }
    with open(sessions_dir / f"{session_id}.json", "w") as f:
        json.dump(marker, f)


def _write_transcript(quote_line):
    """Write a standalone transcript file (outside any git repo, so
    _capture_file_version's committed-before-cite guard doesn't apply) and
    return its absolute path."""
    transcript_dir = tempfile.mkdtemp(prefix="engram_test_transcript_")
    transcript_path = os.path.join(transcript_dir, "session.jsonl")
    with open(transcript_path, "w") as f:
        f.write(json.dumps({"type": "user", "text": quote_line}) + "\n")
    return transcript_path


def test_session_id_happy_path_and_reuse():
    """session_id-only call files an ob whose evidence source_url is the
    resolved transcript; a 2nd session_id call reuses the same evidence node."""
    server = fresh_server()
    quote = "The rate of widget failures was 3.2% in Q1."
    transcript_path = _write_transcript(quote)
    session_id = "abc123-def456-happy"
    _write_session_marker(server, session_id, transcript_path)

    payload = {
        "quoted_text": quote,
        "interpretation": "Failure rate measured directly from the transcript.",
        "claim": "Widget failure rate was 3.2% in Q1.",
        "session_id": session_id,
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    ev_id_1 = r["evidence_id"]
    assert ev_id_1.startswith("ev_"), r

    conn = server._get_db()
    row = conn.execute("SELECT source_url FROM nodes WHERE id = ?", (ev_id_1,)).fetchone()
    conn.close()
    assert row["source_url"] == "file://" + transcript_path, (
        f"Evidence source_url should be the resolved transcript, got {row['source_url']!r}"
    )

    # Second session_id call, different claim -> should reuse the same
    # evidence node (dedup is by source_url, per the evidence-block refactor).
    payload2 = dict(payload)
    payload2["claim"] = "Widget failure rate confirmed at 3.2% in Q1."
    r2 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload2)))
    assert r2.get("status") == "created", r2
    assert r2["evidence_id"] == ev_id_1, (
        f"Expected evidence reuse across session_id calls, got {r2['evidence_id']} != {ev_id_1}"
    )


def test_session_id_ergonomic_defaults():
    """session_id + omitted quote_type/source_class -> filed with
    personal_communication / user_stated."""
    server = fresh_server()
    quote = "The migration finished without incident."
    transcript_path = _write_transcript(quote)
    session_id = "abc123-def456-defaults"
    _write_session_marker(server, session_id, transcript_path)

    payload = {
        "quoted_text": quote,
        "interpretation": "Migration status reported directly.",
        "claim": "The migration completed successfully.",
        "session_id": session_id,
        # quote_type and source_class both omitted
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert r.get("source_class") == "user_stated", (
        f"Expected source_class default 'user_stated' on session_id path, got {r}"
    )

    conn = server._get_db()
    row = conn.execute(
        "SELECT quote_type FROM nodes WHERE id = ?", (r["observation_id"],)
    ).fetchone()
    conn.close()
    assert row["quote_type"] == "personal_communication", (
        f"Expected quote_type default 'personal_communication' on session_id path, got {row['quote_type']!r}"
    )


def test_session_id_explicit_source_class_not_stomped():
    """session_id path + an EXPLICIT source_class must keep that value —
    the ergonomic default (user_stated) applies only on true omission, it
    must not overwrite a caller-supplied choice."""
    server = fresh_server()
    quote = "The rollback completed cleanly."
    transcript_path = _write_transcript(quote)
    session_id = "abc123-def456-explicit-sc"
    _write_session_marker(server, session_id, transcript_path)

    payload = {
        "quoted_text": quote,
        "interpretation": "Rollback status reported directly.",
        "claim": "The rollback completed without error.",
        "session_id": session_id,
        "source_class": "introspective",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    assert r.get("source_class") == "introspective", (
        f"Expected explicit source_class 'introspective' to be preserved on the "
        f"session_id path, not stomped by the ergonomic default, got {r}"
    )


def test_session_id_explicit_empty_quote_type_still_rejected():
    """Regression for the None-sentinel fix applied to quote_type (mirrors
    the source_class fix above): session_id path + an EXPLICIT
    quote_type="" must still hit the Missing-required-fields rejection and
    create no node — it must NOT be silently defaulted to
    'personal_communication' the way a truly-omitted quote_type is. Only
    true omission (the field absent from the payload) should trigger the
    ergonomic default; an explicit empty string is a caller error, not an
    omission."""
    server = fresh_server()
    quote = "The deploy rolled back automatically."
    transcript_path = _write_transcript(quote)
    session_id = "abc123-def456-explicit-empty-qt"
    _write_session_marker(server, session_id, transcript_path)

    payload = {
        "quoted_text": quote,
        "interpretation": "Deploy status reported directly.",
        "claim": "The deploy rolled back automatically.",
        "session_id": session_id,
        "quote_type": "",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" in r and "Missing required fields" in r["error"] and "quote_type" in r["error"], r

    conn = server._get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type LIKE 'observation%'"
    ).fetchone()["c"]
    conn.close()
    assert count == 0, f"Expected no observation node created, found {count}"


def test_session_id_verbatim_still_gates():
    """session_id resolves fine, but the quoted_text isn't actually in the
    transcript -> loud verbatim error, no node created."""
    server = fresh_server()
    transcript_path = _write_transcript("The actual line in the transcript.")
    session_id = "abc123-def456-verbatim"
    _write_session_marker(server, session_id, transcript_path)

    payload = {
        "quoted_text": "This exact sentence does not appear anywhere in the transcript.",
        "interpretation": "Fabricated/mismatched quote.",
        "claim": "Something was said.",
        "session_id": session_id,
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" in r, f"Expected a verbatim-quote error, got {r}"
    assert "not found" in r["error"].lower(), r["error"]

    conn = server._get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type LIKE 'observation%'"
    ).fetchone()["c"]
    conn.close()
    assert count == 0, f"Expected no observation node created, found {count}"


def test_session_id_missing_marker():
    """Unknown session_id (no marker file on disk) -> clear error, no node."""
    server = fresh_server()
    payload = {
        "quoted_text": "q",
        "interpretation": "i",
        "claim": "c",
        "session_id": "no-such-session-id-exists",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" in r, f"Expected error for missing session marker, got {r}"
    assert "session marker" in r["error"].lower() or "no session" in r["error"].lower(), r["error"]

    conn = server._get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type LIKE 'observation%'"
    ).fetchone()["c"]
    conn.close()
    assert count == 0, f"Expected no observation node created, found {count}"


def test_session_id_path_traversal_rejected():
    """session_id containing path separators is rejected by the allowlist
    validator BEFORE any filesystem access — no node created either way."""
    server = fresh_server()

    for bad_session_id in ("../../etc/passwd", "foo/bar", "..\\..\\windows"):
        payload = {
            "quoted_text": "q",
            "interpretation": "i",
            "claim": "c",
            "session_id": bad_session_id,
        }
        r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
        assert "error" in r, f"Expected rejection for session_id={bad_session_id!r}, got {r}"
        assert "invalid session_id" in r["error"].lower(), (
            f"session_id={bad_session_id!r} should be rejected by the allowlist "
            f"validator (error should start with 'Invalid session_id', not a "
            f"'marker not found' filesystem-access error): {r['error']}"
        )

    conn = server._get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type LIKE 'observation%'"
    ).fetchone()["c"]
    conn.close()
    assert count == 0, f"Expected no observation node created, found {count}"


def test_session_id_precedence_conflict_with_url():
    """session_id + url both given -> error asking for exactly one source form."""
    server = fresh_server()
    session_id = "abc123-def456-conflict"
    payload = {
        "quoted_text": "q",
        "interpretation": "i",
        "claim": "c",
        "quote_type": "hard_data",
        "url": "https://example.com/article",
        "title": "Example",
        "session_id": session_id,
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" in r, f"Expected precedence-conflict error, got {r}"
    assert "session_id" in r["error"] and (
        "url" in r["error"] or "evidence_id" in r["error"]
    ), r["error"]

    conn = server._get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM nodes WHERE type LIKE 'observation%'"
    ).fetchone()["c"]
    conn.close()
    assert count == 0, f"Expected no observation node created, found {count}"


def test_url_and_evidence_id_paths_unaffected_by_session_id():
    """Regression: an existing url+title call and an existing evidence_id
    call still behave exactly as before — quote_type still required on those
    paths (no ergonomic-default leakage from the session_id path)."""
    server = fresh_server()

    # url+title path: quote_type omitted -> still a hard required-field error.
    r = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "q", "interpretation": "i", "claim": "c",
        "url": "https://example.com/no-quote-type",
        "title": "T",
    })))
    assert "error" in r and "Missing required fields" in r["error"] and "quote_type" in r["error"], r

    # url+title path: quote_type provided -> created exactly as before.
    r2 = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "X says Y", "interpretation": "X claims that Y is true",
        "claim": "Y is true according to X",
        "quote_type": "attributed_analysis",
        "url": "https://example.com/regression-url",
        "title": "Regression URL",
    })))
    assert r2.get("status") == "created", r2
    assert r2.get("source_class") == "external", (
        f"Expected default source_class 'external' unchanged on the url path, got {r2}"
    )

    # evidence_id path: reuse the evidence node just created, quote_type
    # still required and still enforced.
    ev_id = r2["evidence_id"]
    r3 = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "X says Y", "interpretation": "Second interpretation",
        "claim": "A second claim citing the same evidence",
        "evidence_id": ev_id,
        # quote_type omitted on purpose
    })))
    assert "error" in r3 and "Missing required fields" in r3["error"] and "quote_type" in r3["error"], r3

    r4 = json.loads(server.engram_add_observation(payload_json=json.dumps({
        "quoted_text": "X says Y", "interpretation": "Second interpretation",
        "claim": "A second claim citing the same evidence",
        "evidence_id": ev_id,
        "quote_type": "hard_data",
    })))
    assert r4.get("status") == "created", r4
    assert r4["evidence_id"] == ev_id, r4
