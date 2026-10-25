"""Tests for engram_requote (engram-alpha#4135, part of #4050): fix an
observation's quote in place, against the SAME evidence only.

Similarity: the suite runs with ENGRAM_NO_EMBEDDINGS=1, so every cell except
the "similarity unavailable" one patches `engram_observation._quote_claim_similarity`
(the binding the module under test calls) with a deterministic lookup stub.
Nothing else in the module under test is patched, except the one cell that
forces a failure after the UPDATE (`core._log_edit`).
"""

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_requote_4135_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))
SRC_FILE = os.path.join(TEST_DIR, "requote_source.md")
ARCHIVE_DIR = os.path.join(TEST_DIR, "evidence-archive")

SOURCE_LINES = [
    "alpha beta gamma delta epsilon zeta",
    "the quick brown fox jumps over the lazy dog",
    "ALL GREEN",
    "uniqueoldtoken lives only in the first sentence here",
]

PAST = "2026-01-01T00:00:00.000Z"
FUTURE = "2999-01-01T00:00:00.000Z"


def fresh_server():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    os.makedirs(TEST_DIR)
    os.environ["ENGRAM_EVIDENCE_ARCHIVE"] = ARCHIVE_DIR
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    with open(SRC_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(SOURCE_LINES) + "\n")
    return server


def stub_similarity(monkeypatch, server, table, default=0.5):
    """Deterministic similarity: a dict lookup keyed on the quote."""
    def fake(quote, claim):
        return table.get(quote, default), None
    monkeypatch.setattr(server._observation_mod, "_quote_claim_similarity", fake)


def _add(server, quote, url=None, title="Requote test source", claim="claim text",
         **extra):
    payload = {
        "url": url or f"file://{SRC_FILE}",
        "title": title,
        "quoted_text": quote,
        "interpretation": "interp text",
        "claim": claim,
        "quote_type": "attributed_analysis",
    }
    payload.update(extra)
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r.get("status") == "created", r
    return r["observation_id"]


def _rq(server, node_id, new, reason="tighten the quote", **extra):
    payload = {"node_id": node_id, "new_quoted_text": new, "reason": reason}
    payload.update(extra)
    return json.loads(server.engram_requote(payload_json=json.dumps(payload)))


def _row(server, node_id):
    conn = server.core._get_db()
    try:
        return dict(conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())
    finally:
        conn.close()


def _set(server, node_id, **cols):
    conn = server.core._get_db()
    try:
        for k, v in cols.items():
            conn.execute(f"UPDATE nodes SET {k} = ? WHERE id = ?", (v, node_id))
        conn.commit()
    finally:
        conn.close()


def _meta(server, node_id):
    return json.loads(_row(server, node_id)["metadata"])


def _set_meta(server, node_id, **kv):
    m = _meta(server, node_id)
    m.update(kv)
    _set(server, node_id, metadata=json.dumps(m))


def _edit_actions(server, node_id=None):
    conn = server.core._get_db()
    try:
        rows = conn.execute(
            "SELECT action, node_id, details FROM edit_history WHERE action = 'requoted'"
        ).fetchall()
        return [dict(r) for r in rows if node_id is None or r["node_id"] == node_id]
    finally:
        conn.close()


def _advisory_lines(server):
    p = server.core.DATA_DIR / "anchoring_advisory.jsonl"
    return p.read_text().splitlines() if p.exists() else []


def _snapshot(server, node_id):
    """Everything a requote must NOT touch: the node row minus
    quoted_text/metadata (this includes any embedding column), the edges
    touching it, and its evidence row."""
    conn = server.core._get_db()
    try:
        node = dict(conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())
        ev_id = node["evidence_id"]
        node.pop("quoted_text")
        node.pop("metadata")
        edges = [tuple(r) for r in conn.execute(
            "SELECT * FROM edges WHERE source_id = ? OR target_id = ? ORDER BY 1,2,3",
            (node_id, node_id)).fetchall()]
        ev = conn.execute("SELECT * FROM nodes WHERE id = ?", (ev_id,)).fetchone()
        return node, edges, dict(ev) if ev else None
    finally:
        conn.close()


def _assert_unchanged(server, node_id, before_row, n_edits=0, n_adv=None):
    after = _row(server, node_id)
    assert after == before_row
    assert len(_edit_actions(server)) == n_edits
    if n_adv is not None:
        assert len(_advisory_lines(server)) == n_adv


def _write_transcript(name, rows):
    d = tempfile.mkdtemp(prefix="engram_test_tr_4135_", dir=TEST_DIR)
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def _msg(text, ts=None, role="user"):
    row = {"type": role, "message": {"role": role, "content": text}}
    if ts is not None:
        row["timestamp"] = ts
    return row


# --------------------------------------------------------------------------
# Accepted moves
# --------------------------------------------------------------------------

def test_R1_expand_on_transcript_backed_node(monkeypatch):
    server = fresh_server()
    full = "MERGED 2026-09-04T14:52:14Z 591fb1cf"
    tr = _write_transcript("session.jsonl", [_msg("opening text of the session", PAST),
                                            _msg(full, PAST)])
    nid = _add(server, full, url=f"file://{tr}")
    # A legacy short quote (it predates the write-time floor): set directly.
    _set(server, nid, quoted_text="MERGED")
    stub_similarity(monkeypatch, server, {"MERGED": 0.30, full: 0.60})
    r = _rq(server, nid, full)
    assert r.get("status") == "requoted", r
    assert r["move"] == "EXPAND"
    assert r["version_check"] == "transcript_bounded"
    assert r["bound_kind"] == "timestamp"
    assert r["similarity_before"] == 0.30 and r["similarity_after"] == 0.60
    assert "anchoring_advisory" in r
    row = _row(server, nid)
    assert row["quoted_text"] == full
    hist = json.loads(row["metadata"])["quote_history"]
    assert len(hist) == 1
    h = hist[0]
    assert h["old_quote"] == "MERGED" and h["new_quote"] == full
    assert h["move"] == "EXPAND" and h["reason"] == "tighten the quote"
    assert h["similarity_before"] == 0.30 and h["similarity_after"] == 0.60
    assert "at" in h and "by_session" in h


def test_R2_byte_identity_outside_quote_and_history(monkeypatch):
    server = fresh_server()
    nid = _add(server, "the quick brown fox")
    before_meta = _meta(server, nid)
    snap_before = _snapshot(server, nid)
    stub_similarity(monkeypatch, server, {"the quick brown fox": 0.3,
                                          "the quick brown fox jumps over the lazy dog": 0.5})
    r = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert r.get("status") == "requoted", r
    assert _snapshot(server, nid) == snap_before
    after_meta = _meta(server, nid)
    hist = after_meta.pop("quote_history")
    assert len(hist) == 1
    assert after_meta == before_meta  # every other metadata key unchanged


def test_R3_switch_to_higher_similarity(monkeypatch):
    server = fresh_server()
    nid = _add(server, "the quick brown fox jumps over the lazy dog")
    stub_similarity(monkeypatch, server, {"the quick brown fox jumps over the lazy dog": 0.2,
                                          "alpha beta gamma delta epsilon zeta": 0.9})
    r = _rq(server, nid, "alpha beta gamma delta epsilon zeta")
    assert r.get("status") == "requoted", r
    assert r["move"] == "SWITCH"
    assert r["version_check"] == "none"
    assert _row(server, nid)["quoted_text"] == "alpha beta gamma delta epsilon zeta"


def test_R4_switch_to_lower_similarity_refused(monkeypatch):
    server = fresh_server()
    nid = _add(server, "the quick brown fox jumps over the lazy dog")
    stub_similarity(monkeypatch, server, {"the quick brown fox jumps over the lazy dog": 0.8,
                                          "alpha beta gamma delta epsilon zeta": 0.1234})
    before = _row(server, nid)
    r = _rq(server, nid, "alpha beta gamma delta epsilon zeta")
    assert r["refusal"] == "similarity_decreased", r
    assert "0.8000" in r["error"] and "0.1234" in r["error"], r["error"]
    _assert_unchanged(server, nid, before)


def test_R5_expand_with_lower_similarity_refused(monkeypatch):
    server = fresh_server()
    nid = _add(server, "the quick brown fox")
    stub_similarity(monkeypatch, server, {"the quick brown fox": 0.7,
                                          "the quick brown fox jumps over the lazy dog": 0.2})
    before = _row(server, nid)
    r = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert r["refusal"] == "similarity_decreased", r
    assert "0.7000" in r["error"] and "0.2000" in r["error"], r["error"]
    _assert_unchanged(server, nid, before)


def test_R6_similarity_unavailable_real_path():
    server = fresh_server()  # no stub: the embedder is off (ENGRAM_NO_EMBEDDINGS=1)
    nid = _add(server, "the quick brown fox")
    before = _row(server, nid)
    r = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert r["refusal"] == "similarity_unavailable", r
    _assert_unchanged(server, nid, before)


# --------------------------------------------------------------------------
# Evidence refusals
# --------------------------------------------------------------------------

def test_R7_evidence_file_moved_away(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    before = _row(server, nid)
    os.rename(SRC_FILE, SRC_FILE + ".gone")
    r = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert r["refusal"] == "evidence_unretrievable", r
    assert SRC_FILE in r["error"], r["error"]
    _assert_unchanged(server, nid, before)


def test_R8_https_evidence_unretrievable(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "a quote from a web page", url="https://example.com/page",
               title="Web page")
    before = _row(server, nid)
    r = _rq(server, nid, "a quote from a web page, longer")
    assert r["refusal"] == "evidence_unretrievable", r
    assert "https://example.com/page" in r["error"], r["error"]
    _assert_unchanged(server, nid, before)


def test_R9_new_quote_not_in_source(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    before = _row(server, nid)
    r = _rq(server, nid, "a sentence the source never contained")
    assert r["refusal"] == "not_in_source", r
    _assert_unchanged(server, nid, before)


def test_R10_nine_char_non_line_quote_too_short(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    before = _row(server, nid)
    r = _rq(server, nid, "beta gamm")  # 9 chars, inside line 1, not a whole line
    assert r["refusal"] == "quote_too_short", r
    _assert_unchanged(server, nid, before)


def test_R10b_whole_line_short_quote_accepted(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    r = _rq(server, nid, "ALL GREEN")  # 9 chars but the whole source line
    assert r.get("status") == "requoted", r


# --------------------------------------------------------------------------
# Node-state refusals
# --------------------------------------------------------------------------

def test_R11_superseded_and_retracted_not_current(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    a = _add(server, "the quick brown fox")
    b = _add(server, "alpha beta gamma delta")
    _set(server, a, is_current=0)
    _set(server, b, status="retracted")
    for nid in (a, b):
        before = _row(server, nid)
        r = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
        assert r["refusal"] == "not_current", r
        _assert_unchanged(server, nid, before)


def test_R12_derivation_not_observation(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    conn = server.core._get_db()
    try:
        conn.execute(
            "INSERT INTO nodes (id, type, claim, created_at) VALUES "
            "('dv_fixture', 'derivation', 'fixture derivation', ?)",
            (server.core._now(),))
        conn.commit()
    finally:
        conn.close()
    r = _rq(server, "dv_fixture", "anything at all here")
    assert r["refusal"] == "not_observation", r
    r2 = _rq(server, "no_such_node", "anything at all here")
    assert r2["refusal"] == "not_observation", r2


def test_R13_identical_quote(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    before = _row(server, nid)
    r = _rq(server, nid, "the quick brown fox")
    assert r["refusal"] == "identical_quote", r
    _assert_unchanged(server, nid, before)


def test_R13b_identical_quote_on_https_node_is_identical_quote(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "a quote from a web page", url="https://example.com/page",
               title="Web page")
    r = _rq(server, nid, "a quote from a web page")
    assert r["refusal"] == "identical_quote", r  # the cheap check runs first


# --------------------------------------------------------------------------
# Version rule (a) git_sha
# --------------------------------------------------------------------------

def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _git_fixture(server):
    repo = os.path.join(TEST_DIR, "gitrepo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    f = os.path.join(repo, "doc.md")
    with open(f, "w") as fh:
        fh.write("oldrevision sentence about widgets and bolts\n")
    _git(repo, "add", "doc.md")
    _git(repo, "commit", "-q", "-m", "v1")
    nid = _add(server, "oldrevision sentence about widgets", url=f"file://{f}")
    with open(f, "w") as fh:
        fh.write("newrevision sentence about gadgets and nuts\n")
    _git(repo, "commit", "-q", "-am", "v2")
    return nid, f


def test_R14_git_old_revision_accepted(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid, f = _git_fixture(server)
    sha = _meta(server, nid)["git_sha"]
    r = _rq(server, nid, "sentence about widgets and bolts")  # only in v1
    assert r.get("status") == "requoted", r
    assert r["git_sha"] == sha
    assert "version_check" not in r or r["version_check"] != "none"
    assert _row(server, nid)["quoted_text"] == "sentence about widgets and bolts"


def test_R15_git_new_revision_only_is_not_in_source(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid, f = _git_fixture(server)
    before = _row(server, nid)
    r = _rq(server, nid, "sentence about gadgets and nuts")  # only in v2 / working tree
    assert r["refusal"] == "not_in_source", r
    _assert_unchanged(server, nid, before)


def test_R15b_git_revision_unreadable(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid, f = _git_fixture(server)
    _set_meta(server, nid, git_sha="0" * 40)
    before = _row(server, nid)
    r = _rq(server, nid, "sentence about widgets and bolts")
    assert r["refusal"] == "evidence_unretrievable", r
    assert "0" * 40 in r["error"], r["error"]
    _assert_unchanged(server, nid, before)


# --------------------------------------------------------------------------
# Version rule (b) content_hash, (c1) transcript bound, (c2) hash-named archive
# --------------------------------------------------------------------------

def test_R16_content_hash_without_git_sha_metadata_written_directly(monkeypatch):
    # The public path records content_hash WITHOUT git_sha only for a
    # content-addressed archive file inside a git repo; the fixture writes the
    # metadata directly instead of building that repo layout.
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    h = hashlib.sha256(open(SRC_FILE, "rb").read()).hexdigest()
    _set_meta(server, nid, content_hash=h)
    ok = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert ok.get("status") == "requoted", ok
    assert ok["content_hash"] == h
    # Now the file changes: refuse, both hashes named.
    nid2 = _add(server, "alpha beta gamma delta")
    _set_meta(server, nid2, content_hash=h)
    with open(SRC_FILE, "a") as fh:
        fh.write("an appended line\n")
    new_h = hashlib.sha256(open(SRC_FILE, "rb").read()).hexdigest()
    before = _row(server, nid2)
    r = _rq(server, nid2, "alpha beta gamma delta epsilon zeta")
    assert r["refusal"] == "evidence_changed", r
    assert h in r["error"] and new_h in r["error"], r["error"]
    _assert_unchanged(server, nid2, before, n_edits=1)


def test_R17_transcript_span_only_after_created_at(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    tr = _write_transcript("session.jsonl", [
        _msg("the earlier utterance in this session", PAST),
        _msg("a row without any stamp sits in between", None),
        _msg("a later utterance written after the observation", FUTURE),
    ])
    nid = _add(server, "the earlier utterance", url=f"file://{tr}")
    before = _row(server, nid)
    r = _rq(server, nid, "a later utterance written after the observation")
    assert r["refusal"] == "not_in_source", r
    _assert_unchanged(server, nid, before)
    ok = _rq(server, nid, "earlier utterance in this session")
    assert ok.get("status") == "requoted", ok
    assert ok["version_check"] == "transcript_bounded"
    assert ok["bound"] == before["created_at"] and ok["bound_kind"] == "timestamp"
    # the unstamped row is kept
    ok2 = _rq(server, nid, "a row without any stamp sits in between")
    assert ok2.get("status") == "requoted", ok2


def test_R17b_transcript_row_order_fallback_when_stamps_unusable(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    tr = _write_transcript("session.jsonl", [
        _msg("the earlier utterance in this session"),
        _msg("a later utterance written after the observation"),
    ])
    nid = _add(server, "the earlier utterance", url=f"file://{tr}")
    ok = _rq(server, nid, "earlier utterance in this session")
    assert ok.get("status") == "requoted", ok
    assert ok["version_check"] == "transcript_bounded" and ok["bound_kind"] == "row_order"
    before = _row(server, nid)
    r = _rq(server, nid, "a later utterance written after the observation")
    assert r["refusal"] == "not_in_source", r
    _assert_unchanged(server, nid, before, n_edits=1)


def test_R18_hash_named_archive_altered(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    body = "archived line one about widgets\narchived line two about gadgets\n"
    h = hashlib.sha256(body.encode()).hexdigest()
    path = os.path.join(ARCHIVE_DIR, f"{h}.md")
    with open(path, "w") as fh:
        fh.write(body)
    nid = _add(server, "archived line one about widgets", url=f"file://{path}")
    ok = _rq(server, nid, "archived line one")
    assert ok.get("status") == "requoted", ok
    assert ok["version_check"] == "hash_name"
    nid2 = _add(server, "archived line two about gadgets", url=f"file://{path}")
    with open(path, "w") as fh:
        fh.write(body.replace("gadgets", "gizmos") + "extra\n")
    before = _row(server, nid2)
    r = _rq(server, nid2, "archived line two")
    assert r["refusal"] == "evidence_changed", r
    _assert_unchanged(server, nid2, before, n_edits=1)


# --------------------------------------------------------------------------
# EXPAND classification on normalized forms
# --------------------------------------------------------------------------

def test_R19_expand_with_json_escape_and_curly_quote_in_stored_old(monkeypatch):
    server = fresh_server()
    new = 'He did say "hello" now and "then" walked'
    tr = _write_transcript("session.jsonl", [_msg(new, PAST)])
    nid = _add(server, "He did say", url=f"file://{tr}")
    # The stored old quote carries a JSON escape and curly quotes: set directly.
    old = 'say \\"hello\\" now and \u201cthen\u201d'
    _set(server, nid, quoted_text=old)
    stub_similarity(monkeypatch, server, {old: 0.2, new: 0.4})
    r = _rq(server, nid, new)
    assert r.get("status") == "requoted", r
    assert r["move"] == "EXPAND", r  # contained only once decoded and normalized


# --------------------------------------------------------------------------
# FTS, history, inspect, atomicity, unknown fields
# --------------------------------------------------------------------------

def _fts_hits(server, token):
    conn = server.core._get_db()
    try:
        return [r[0] for r in conn.execute(
            "SELECT n.id FROM nodes_fts f JOIN nodes n ON n.rowid = f.rowid "
            "WHERE nodes_fts MATCH ?", (token,)).fetchall()]
    finally:
        conn.close()


def test_R20_fts_follows_the_quote(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "uniqueoldtoken lives only in the first")
    assert nid in _fts_hits(server, "uniqueoldtoken")
    r = _rq(server, nid, "ALL GREEN")
    assert r.get("status") == "requoted", r
    assert nid not in _fts_hits(server, "uniqueoldtoken")
    r2 = _rq(server, nid, "alpha beta gamma delta epsilon zeta")
    assert r2.get("status") == "requoted", r2
    assert nid in _fts_hits(server, "epsilon")


def test_R21_edit_history_one_row_refusal_adds_none(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    _rq(server, nid, "no such text in the source anywhere")
    _rq(server, nid, "the quick brown fox")
    assert _edit_actions(server) == []
    advisory_before = len(_advisory_lines(server))
    ok = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert ok.get("status") == "requoted", ok
    rows = _edit_actions(server, nid)
    assert len(rows) == 1
    d = json.loads(rows[0]["details"])
    assert d.get("move") == "EXPAND" and d.get("reason") == "tighten the quote"
    assert len(_advisory_lines(server)) == advisory_before + 1  # success only


def test_R22_inspect_shows_quote_history(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    ok = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert ok.get("status") == "requoted", ok
    for view in ("recall", "deep"):
        out = server.engram_inspect(payload_json=json.dumps({"node_id": nid, "view": view}))
        assert "quote_history" in out and "tighten the quote" in out, (view, out[:500])


def test_R23_failure_after_update_is_atomic(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    before = _row(server, nid)

    def boom(*a, **k):
        raise RuntimeError("forced failure after the UPDATE")

    monkeypatch.setattr(server.core, "_log_edit", boom)
    r = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert "error" in r and "nothing was changed" in r["error"], r
    assert r.get("status") != "requoted"
    monkeypatch.undo()
    assert _row(server, nid) == before
    assert _edit_actions(server) == []


def test_R24_unknown_field_refused(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    before = _row(server, nid)
    r = _rq(server, nid, "the quick brown fox jumps over the lazy dog",
            evidence_id="ev_invented")
    assert "error" in r and "evidence_id" in r["error"], r
    assert r.get("status") != "requoted"
    _assert_unchanged(server, nid, before)


@pytest.mark.parametrize("field", ["node_id", "new_quoted_text", "reason"])
def test_R25_blank_or_missing_field_refused(monkeypatch, field):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    nid = _add(server, "the quick brown fox")
    payload = {"node_id": nid, "new_quoted_text": "the quick brown fox jumps",
               "reason": "why"}
    payload[field] = "   "
    r = json.loads(server.engram_requote(payload_json=json.dumps(payload)))
    assert "error" in r and r.get("status") != "requoted", r
    del payload[field]
    r = json.loads(server.engram_requote(payload_json=json.dumps(payload)))
    assert "error" in r and r.get("status") != "requoted", r


# --------------------------------------------------------------------------
# Round 2 (PR #4164 review): guarded write, c1 cut, cross-repo, R24 variant
# --------------------------------------------------------------------------

def _retract_args(nid):
    return json.dumps({
        "node_id": nid, "error_type": "other", "reason": "retracted inside the call",
        "replacement_absent_because": "NONE_BECAUSE_UNKNOWN",
        "replacement_absent_note": "fixture",
    })


def test_R26_concurrent_requote_inside_similarity_step_refused(monkeypatch):
    server = fresh_server()
    nid = _add(server, "the quick brown fox")
    inner_quote = "the quick brown fox jumps"
    outer_quote = "the quick brown fox jumps over the lazy dog"
    state = {"inner_done": False, "inner_result": None}

    def fake(quote, claim):
        if not state["inner_done"]:
            state["inner_done"] = True  # set first: the inner call re-enters here
            state["inner_result"] = _rq(server, nid, inner_quote, reason="inner fix")
        return 0.5, None

    monkeypatch.setattr(server._observation_mod, "_quote_claim_similarity", fake)
    outer = _rq(server, nid, outer_quote, reason="outer fix")
    assert state["inner_result"].get("status") == "requoted", state["inner_result"]
    assert outer.get("refusal") == "changed_concurrently", outer
    assert "changed" in outer["error"] and "retry" in outer["error"], outer["error"]
    row = _row(server, nid)
    assert row["quoted_text"] == inner_quote
    hist = json.loads(row["metadata"])["quote_history"]
    assert len(hist) == 1 and hist[0]["new_quote"] == inner_quote
    assert len(_edit_actions(server, nid)) == 1


def test_R26b_metadata_only_change_inside_similarity_step_refused(monkeypatch):
    # Writes the DB directly (not through a tool) to isolate ONE half of the
    # guarded UPDATE: only `metadata` changes, `quoted_text` stays as read.
    server = fresh_server()
    nid = _add(server, "the quick brown fox")
    state = {"done": False}

    def fake(quote, claim):
        if not state["done"]:
            state["done"] = True
            _set_meta(server, nid, interfering_key="written mid-call")
        return 0.5, None

    monkeypatch.setattr(server._observation_mod, "_quote_claim_similarity", fake)
    outer = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert state["done"]
    assert outer.get("refusal") == "changed_concurrently", outer
    row = _row(server, nid)
    assert row["quoted_text"] == "the quick brown fox"
    meta = json.loads(row["metadata"])
    assert meta.get("interfering_key") == "written mid-call"
    assert "quote_history" not in meta
    assert _edit_actions(server, nid) == []


def test_R26c_quoted_text_only_change_inside_similarity_step_refused(monkeypatch):
    # Writes the DB directly (not through a tool) to isolate ONE half of the
    # guarded UPDATE: only `quoted_text` changes, `metadata` stays byte-identical.
    server = fresh_server()
    nid = _add(server, "the quick brown fox")
    meta_before = _row(server, nid)["metadata"]
    interfering = "the lazy dog"
    state = {"done": False}

    def fake(quote, claim):
        if not state["done"]:
            state["done"] = True
            _set(server, nid, quoted_text=interfering)
        return 0.5, None

    monkeypatch.setattr(server._observation_mod, "_quote_claim_similarity", fake)
    outer = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert state["done"]
    assert outer.get("refusal") == "changed_concurrently", outer
    row = _row(server, nid)
    assert row["quoted_text"] == interfering
    assert row["metadata"] == meta_before
    assert _edit_actions(server, nid) == []


def test_R27_retract_inside_similarity_step_refused(monkeypatch):
    server = fresh_server()
    nid = _add(server, "the quick brown fox")
    state = {"done": False, "retract": None}

    def fake(quote, claim):
        if not state["done"]:
            state["done"] = True
            state["retract"] = json.loads(
                server.engram_retract(payload_json=_retract_args(nid)))
        return 0.5, None

    monkeypatch.setattr(server._observation_mod, "_quote_claim_similarity", fake)
    outer = _rq(server, nid, "the quick brown fox jumps over the lazy dog")
    assert "error" not in state["retract"], state["retract"]
    assert outer.get("refusal") == "changed_concurrently", outer
    row = _row(server, nid)
    assert row["status"] == "retracted"
    assert row["quoted_text"] == "the quick brown fox"
    meta = json.loads(row["metadata"])
    assert "quote_history" not in meta
    assert "retract" in json.dumps(meta).lower(), meta  # what retract wrote survives
    assert _edit_actions(server, nid) == []


def test_R28_unstamped_row_after_future_row_is_cut(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    tr = _write_transcript("session.jsonl", [
        _msg("the earlier utterance in this session", PAST),
        _msg("a later utterance written after the observation", FUTURE),
        _msg("an unstamped summary row appended much later on", None),
    ])
    nid = _add(server, "the earlier utterance", url=f"file://{tr}")
    before = _row(server, nid)
    r = _rq(server, nid, "an unstamped summary row appended much later on")
    assert r["refusal"] == "not_in_source", r
    _assert_unchanged(server, nid, before)


def test_R29_cross_repo_citation_cannot_be_requoted(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    repo = os.path.join(TEST_DIR, "srcrepo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    body = "crossrepo sentence about widgets and bolts\n"
    with open(os.path.join(repo, "doc.md"), "w") as fh:
        fh.write(body)
    _git(repo, "add", "doc.md")
    _git(repo, "commit", "-q", "-m", "v1")
    outside = os.path.join(TEST_DIR, "outside_copy.md")  # not inside any git repo
    with open(outside, "w") as fh:
        fh.write(body)
    nid = _add(server, "crossrepo sentence about widgets", url=f"file://{outside}",
               source_repo=repo, repo_path="doc.md")
    assert _meta(server, nid).get("git_sha")
    before = _row(server, nid)
    r = _rq(server, nid, "crossrepo sentence about widgets and bolts")
    assert r["refusal"] == "evidence_unretrievable", r
    assert "source_repo" in r["error"] and "repo_path" in r["error"], r["error"]
    assert "own git repo" in r["error"] and "cannot be requoted" in r["error"], r["error"]
    _assert_unchanged(server, nid, before)


def test_R24b_real_second_evidence_id_refused_and_node_unchanged(monkeypatch):
    server = fresh_server()
    stub_similarity(monkeypatch, server, {})
    second = os.path.join(TEST_DIR, "second_source.md")
    with open(second, "w") as fh:
        fh.write("a distinctive line found only in the second file\n")
    nid = _add(server, "the quick brown fox")
    other = _add(server, "a distinctive line found only in", url=f"file://{second}",
                 title="Second source")
    other_ev = _row(server, other)["evidence_id"]
    assert other_ev and other_ev != _row(server, nid)["evidence_id"]
    before = _row(server, nid)
    r = _rq(server, nid, "a distinctive line found only in the second file",
            evidence_id=other_ev)
    assert "error" in r and r.get("status") != "requoted", r
    after = _row(server, nid)
    assert after["evidence_id"] == before["evidence_id"]
    assert after["quoted_text"] == before["quoted_text"]
    assert after == before
    assert _edit_actions(server) == []
