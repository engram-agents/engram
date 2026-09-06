"""Tests for evidence_type and original_url capture-time passthrough on
engram_add_observation (#2606 facet 1 units 2 + 3).

Unit 2 -- evidence_type: engram-snapshot emits a closed-vocabulary
evidence_type key on every source kind; engram_add_observation gains an
optional evidence_type passthrough, stored on the evidence node's metadata.
First-capture-wins on reuse: a value already present is never overwritten,
and a caller naming a DIFFERING value surfaces as evidence_type_conflict in
the response rather than silently resolving one way or the other.

Unit 3 -- original_url: the pre-snapshot provenance pointer engram-snapshot
already emits (and, until this unit, drops on the floor) is now stored on
the evidence node's metadata too, alongside evidence_type, under the same
first-capture-wins semantics. Explicitly NOT a re-fetch pointer.

Coverage:
  1. evidence_type happy path -- first capture sets it on a freshly created
     evidence node.
  2. evidence_type CONTROL test -- two observations against the SAME
     archive/evidence with DIFFERENT evidence_type values: first-capture
     wins, second surfaces evidence_type_conflict, existing value unchanged.
     (A test that only ever writes one value cannot distinguish "first
     wins" from "last wins" -- this is the control that can fail.)
  3. evidence_type: same value on reuse is a clean no-conflict no-op.
  4. evidence_type: invalid (non-enum) value is rejected with no node
     created.
  5. original_url happy path -- first capture sets it.
  6. original_url NEGATIVE test -- an evidence node created WITHOUT a
     snapshot (no original_url passed) must not grow a spurious
     original_url key in its metadata.
  7. original_url conflict -- same first-capture-wins + conflict-surfacing
     behavior as evidence_type.
  8. Both fields together on one call.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_evidence_capture_metadata_")
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


def _write_source_file(text: str) -> str:
    """Write a standalone source file (outside any git repo, so
    _capture_file_version's committed-before-cite guard doesn't apply) and
    return its absolute path."""
    source_dir = tempfile.mkdtemp(prefix="engram_test_evidence_source_")
    source_path = os.path.join(source_dir, "source.txt")
    with open(source_path, "w") as f:
        f.write(text)
    return source_path


def _evidence_metadata(server, evidence_id):
    conn = server._get_db()
    row = conn.execute(
        "SELECT metadata FROM nodes WHERE id = ?", (evidence_id,)
    ).fetchone()
    conn.close()
    return json.loads(row["metadata"] or "{}")


def _file_url(path: str) -> str:
    return f"file://{path}"


# ── 1. evidence_type happy path ─────────────────────────────────────────


def test_evidence_type_stamped_on_fresh_evidence_node():
    server = fresh_server()
    source_path = _write_source_file("The rate of widget failures was 3.2% in Q1.\n")

    payload = {
        "quoted_text": "The rate of widget failures was 3.2% in Q1.",
        "interpretation": "Failure rate measured directly.",
        "claim": "Widget failure rate was 3.2% in Q1.",
        "quote_type": "hard_data",
        "url": _file_url(source_path),
        "title": "Archived source",
        "evidence_type": "local_file",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", f"Expected created, got {r}"
    ev_id = r["evidence_id"]

    meta = _evidence_metadata(server, ev_id)
    assert meta.get("evidence_type") == "local_file", meta
    assert "evidence_type_conflict" not in r, r


# ── 2. CONTROL: first-capture-wins, differing value surfaces conflict ──


def test_evidence_type_first_capture_wins_control():
    """Two observations cite the SAME url (same evidence node, by url
    reuse) with DIFFERENT evidence_type values. First value wins and is
    never overwritten; the second call surfaces evidence_type_conflict
    instead of silently applying its own value.

    This is the control: a test that only ever writes ONE evidence_type
    value cannot distinguish "first wins" from "last wins" -- this one
    can, because the two calls disagree.
    """
    server = fresh_server()
    source_path = _write_source_file(
        "Sentence one is here.\nSentence two follows after.\n"
    )
    url = _file_url(source_path)

    payload1 = {
        "quoted_text": "Sentence one is here.",
        "interpretation": "First fact from the source.",
        "claim": "Sentence one asserts a fact.",
        "quote_type": "hard_data",
        "url": url,
        "title": "Shared source",
        "evidence_type": "web_page",
    }
    r1 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload1)))
    assert r1.get("status") == "created", r1
    ev_id = r1["evidence_id"]
    assert _evidence_metadata(server, ev_id).get("evidence_type") == "web_page"

    payload2 = {
        "quoted_text": "Sentence two follows after.",
        "interpretation": "Second fact from the same source.",
        "claim": "Sentence two asserts a different fact.",
        "quote_type": "hard_data",
        "url": url,  # same URL -> same (reused) evidence node
        "title": "Shared source",
        "evidence_type": "pdf",  # DIFFERENT value from payload1
    }
    r2 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload2)))
    assert r2.get("status") == "created", r2
    assert r2["evidence_id"] == ev_id, "expected evidence reuse by url match"

    # First-capture wins: the conflicting second value did NOT overwrite it.
    assert _evidence_metadata(server, ev_id).get("evidence_type") == "web_page", (
        "first-captured evidence_type must survive a differing second call"
    )

    # The disagreement is surfaced, not silently dropped.
    assert r2.get("evidence_type_conflict") == {
        "existing": "web_page",
        "attempted": "pdf",
    }, r2


def test_evidence_type_same_value_on_reuse_is_clean_noop():
    """Reusing the same evidence with the SAME evidence_type value is not a
    conflict -- distinguishes 'differing value' from 'any second write'."""
    server = fresh_server()
    source_path = _write_source_file(
        "Sentence one is here.\nSentence two follows after.\n"
    )
    url = _file_url(source_path)

    payload1 = {
        "quoted_text": "Sentence one is here.",
        "interpretation": "First fact.",
        "claim": "Sentence one asserts a fact.",
        "quote_type": "hard_data",
        "url": url,
        "title": "Shared source",
        "evidence_type": "web_page",
    }
    r1 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload1)))
    ev_id = r1["evidence_id"]

    payload2 = dict(payload1)
    payload2["quoted_text"] = "Sentence two follows after."
    payload2["claim"] = "Sentence two asserts a different fact."
    r2 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload2)))

    assert "evidence_type_conflict" not in r2, r2
    assert _evidence_metadata(server, ev_id).get("evidence_type") == "web_page"


def test_evidence_type_invalid_value_rejected():
    server = fresh_server()
    source_path = _write_source_file("Some source text.\n")

    payload = {
        "quoted_text": "Some source text.",
        "interpretation": "A fact.",
        "claim": "Something is true.",
        "quote_type": "hard_data",
        "url": _file_url(source_path),
        "title": "Source",
        "evidence_type": "not_a_real_type",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert "error" in r and "Invalid evidence_type" in r["error"], r


# ── 3. original_url happy path ──────────────────────────────────────────


def test_original_url_stamped_on_fresh_evidence_node():
    server = fresh_server()
    source_path = _write_source_file("Archived text content.\n")

    payload = {
        "quoted_text": "Archived text content.",
        "interpretation": "A fact from the archived source.",
        "claim": "The archived source contains this fact.",
        "quote_type": "hard_data",
        "url": _file_url(source_path),
        "title": "Archived copy",
        "original_url": "http://localhost:5002/threads/119",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r
    ev_id = r["evidence_id"]

    meta = _evidence_metadata(server, ev_id)
    assert meta.get("original_url") == "http://localhost:5002/threads/119", meta
    assert "original_url_conflict" not in r, r


# ── 4. NEGATIVE: no snapshot -> no spurious original_url key ───────────


def test_original_url_absent_when_not_snapshotted():
    """An evidence node created WITHOUT a snapshot (no original_url passed)
    must not grow a spurious original_url key -- the field is opt-in per
    call, not a default-populated one."""
    server = fresh_server()
    source_path = _write_source_file("Ordinary unsnapshotted content.\n")

    payload = {
        "quoted_text": "Ordinary unsnapshotted content.",
        "interpretation": "A fact from an ordinary local file citation.",
        "claim": "The file contains this fact.",
        "quote_type": "hard_data",
        "url": _file_url(source_path),
        "title": "Ordinary source",
        # original_url deliberately omitted
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r
    ev_id = r["evidence_id"]

    meta = _evidence_metadata(server, ev_id)
    assert "original_url" not in meta, (
        f"evidence metadata must not grow a spurious original_url key, got: {meta}"
    )


# ── 5. original_url conflict (same first-capture-wins semantics) ───────


def test_original_url_first_capture_wins_control():
    server = fresh_server()
    source_path = _write_source_file(
        "First fact sentence.\nSecond fact sentence.\n"
    )
    url = _file_url(source_path)

    payload1 = {
        "quoted_text": "First fact sentence.",
        "interpretation": "First fact.",
        "claim": "The first fact is true.",
        "quote_type": "hard_data",
        "url": url,
        "title": "Shared source",
        "original_url": "http://localhost:5002/threads/119",
    }
    r1 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload1)))
    ev_id = r1["evidence_id"]
    assert _evidence_metadata(server, ev_id).get("original_url") == (
        "http://localhost:5002/threads/119"
    )

    payload2 = {
        "quoted_text": "Second fact sentence.",
        "interpretation": "Second fact.",
        "claim": "The second fact is true.",
        "quote_type": "hard_data",
        "url": url,
        "title": "Shared source",
        "original_url": "http://localhost:5002/threads/999",  # DIFFERENT
    }
    r2 = json.loads(server.engram_add_observation(payload_json=json.dumps(payload2)))
    assert r2["evidence_id"] == ev_id

    assert _evidence_metadata(server, ev_id).get("original_url") == (
        "http://localhost:5002/threads/119"
    ), "first-captured original_url must survive a differing second call"
    assert r2.get("original_url_conflict") == {
        "existing": "http://localhost:5002/threads/119",
        "attempted": "http://localhost:5002/threads/999",
    }, r2


# ── 6. Both fields together ─────────────────────────────────────────────


def test_evidence_type_and_original_url_together():
    server = fresh_server()
    source_path = _write_source_file("Combined-field test content.\n")

    payload = {
        "quoted_text": "Combined-field test content.",
        "interpretation": "A fact from a forum-archived source.",
        "claim": "The forum thread contains this fact.",
        "quote_type": "hard_data",
        "url": _file_url(source_path),
        "title": "Forum archive",
        "evidence_type": "forum_thread",
        "original_url": "http://localhost:5002/threads/42",
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r

    meta = _evidence_metadata(server, r["evidence_id"])
    assert meta.get("evidence_type") == "forum_thread"
    assert meta.get("original_url") == "http://localhost:5002/threads/42"
