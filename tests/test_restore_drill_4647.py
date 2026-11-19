"""Contract cells for the restore drill (part 1 of #4647).

Written as the contract, then run against a stub to see them red, then against the tool (see the PR).
Cells are named test_dN_... after the contract they pin:

  D1  the live graph is untouched      D6  transcripts replayed and compared
  D2  no sqlite3 command-line program  D7  an unsafe scratch is refused (exit 2, nothing written)
  D3  row content exact (write-once)   D8  first server connect in a scratch-home subprocess
  D3b classification                   D9  the report keys
  D4  rows written after the dump      D10 --no-embed
  D5  vector completeness and sample

The real embedding model is never loaded here: `_embed_texts` is replaced by a deterministic stub.
stdlib + pytest only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "src" / "engram" / "tools" / "restore_drill.py"
PKG_ROOT = REPO / "src" / "engram"


def _load():
    name = "_restore_drill_4647"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rd = _load()

_SCHEMA_SCRIPT = "import engram_core as c; c._get_db().close()"

CLAIMS = {
    "ax_0001": "honesty is structural",
    "ob_0001": "the first observation",
    "ob_0002": "the second observation",
    "ob_0003": "the third observation",
}


def _stub_vec(text: str) -> list:
    return [len(text) / 100.0, (sum(map(ord, text)) % 97) / 97.0, 0.5]


def stub_embed(texts, scratch=None):
    return [_stub_vec(t) for t in texts]


@pytest.fixture(autouse=True)
def embedder(monkeypatch):
    monkeypatch.setattr(rd, "_embed_texts", stub_embed)


def _clean_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ENGRAM_", "CLAUDE_"))}
    env.update(ENGRAM_HOME=str(home), HOME=str(home), ENGRAM_NO_DB_GUARDS="1", PYTHONPATH=str(PKG_ROOT),
               PYTHONDONTWRITEBYTECODE="1")
    return env


def make_home(tmp_path, name="home") -> Path:
    """A live home: the server's own schema, four nodes, edges, vectors, and a committed-style dump."""
    home = tmp_path / name
    home.mkdir()
    subprocess.run([sys.executable, "-c", _SCHEMA_SCRIPT], env=_clean_env(home), check=True,
                   capture_output=True, timeout=120)
    conn = sqlite3.connect(home / "knowledge.db")
    for nid, claim in CLAIMS.items():
        typ = "axiom" if nid.startswith("ax") else "observation"
        conn.execute(
            "INSERT INTO nodes(id,type,claim,created_at,is_current,status,confidence,recall_count,embedding) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (nid, typ, claim, "2026-01-01T00:00:00Z", 1, "active", 0.85, 2, json.dumps(_stub_vec(claim))))
    conn.execute("INSERT INTO edges(source_id,target_id,relation,created_at) VALUES('ob_0002','ob_0001','supports','2026-01-02')")
    conn.commit()
    conn.close()
    dump(home)
    return home


def dump(home: Path) -> None:
    rd.engram_backup.dump_stripped(str(home / "knowledge.db"), str(home / "knowledge.sql"))


def live_exec(home: Path, sql: str, params=()) -> None:
    conn = sqlite3.connect(home / "knowledge.db")
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def drill(home, scratch, **kw):
    return rd.run_drill(home=home, scratch=scratch, source=kw.pop("source", home / "no-transcripts"), **kw)


def tree_state(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture
def home(tmp_path):
    return make_home(tmp_path)


# ---------------------------------------------------------------- D1

def _side_files(home):
    return sorted(p.name for p in home.iterdir() if p.name.startswith("knowledge.db-"))


def test_d1_live_graph_untouched_quiescent(home, tmp_path):
    db = home / "knowledge.db"
    sha = rd._sha256_file(db)
    before = tree_state(home)
    assert _side_files(home) == []
    rep = drill(home, tmp_path / "scratch", assert_quiescent=True)
    assert rep.exit_code == 0, rep.failures
    assert rd._sha256_file(db) == sha
    assert _side_files(home) == []
    assert tree_state(home) == before


def test_d1_live_graph_untouched_with_active_wal(home, tmp_path):
    writer = sqlite3.connect(home / "knowledge.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE nodes SET recall_count = 9 WHERE id = 'ob_0001'")
    writer.commit()  # in the WAL, not checkpointed: the writer stays open
    try:
        assert _side_files(home) == ["knowledge.db-shm", "knowledge.db-wal"]
        db = home / "knowledge.db"
        sha = rd._sha256_file(db)
        wal_before = (home / "knowledge.db-wal").stat()
        rep = drill(home, tmp_path / "scratch", assert_quiescent=True)
        assert rep.exit_code == 0, rep.failures
        assert rd._sha256_file(db) == sha
        assert _side_files(home) == ["knowledge.db-shm", "knowledge.db-wal"]
        assert (home / "knowledge.db-wal").stat().st_size == wal_before.st_size
        # the row only the WAL holds was read: the drill saw the committed state
        assert rep.mutable_diffs.get("nodes.recall_count") == 1
    finally:
        writer.close()


def test_d1_live_db_is_opened_read_only(home):
    conn = rd._open_live_readonly(home / "knowledge.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE nodes SET claim = 'x'")
    finally:
        conn.close()


# ---------------------------------------------------------------- D2

def test_d2_runs_without_any_sqlite3_cli(home, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert rep.nodes_restored == len(CLAIMS)


# ---------------------------------------------------------------- D3 / D3b

def test_d3_a_mutated_dump_claim_names_the_node_and_the_column(home, tmp_path):
    sql = home / "knowledge.sql"
    text = sql.read_text(encoding="utf-8")
    assert "the second observation" in text
    sql.write_text(text.replace("the second observation", "the second observatioN"), encoding="utf-8")
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    hit = [f for f in rep.failures if "ob_0002" in f and "column claim" in f]
    assert hit, rep.failures
    assert "nodes" in hit[0]


def test_d3_the_output_lists_what_was_not_compared(home, tmp_path):
    rep = drill(home, tmp_path / "scratch")
    assert "embedding" in rep.excluded["columns"]
    assert any(t.startswith("nodes_fts") for t in rep.excluded["tables"]) or rep.excluded["tables"]
    text = rd.format_report(rep)
    assert "not compared" in text and "embedding" in text


def test_d3b_a_changed_mutable_column_is_reported_not_failed(home, tmp_path):
    live_exec(home, "UPDATE nodes SET recall_count = 7 WHERE id = 'ob_0001'")
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert rep.mutable_diffs == {"nodes.recall_count": 1}
    assert "nodes.recall_count=1" in rd.format_report(rep)


def test_d3b_a_changed_claim_character_in_live_is_a_failure(home, tmp_path):
    live_exec(home, "UPDATE nodes SET claim = 'the first observatioN' WHERE id = 'ob_0001'")
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert any("ob_0001" in f and "column claim" in f for f in rep.failures), rep.failures


def test_d3b_an_unclassified_column_is_treated_as_write_once(tmp_path):
    home = tmp_path / "h2"
    home.mkdir()
    subprocess.run([sys.executable, "-c", _SCHEMA_SCRIPT], env=_clean_env(home), check=True,
                   capture_output=True, timeout=120)
    conn = sqlite3.connect(home / "knowledge.db")
    conn.execute("INSERT INTO nodes(id,type,claim,created_at,is_current,status) VALUES('ax_0001','axiom','c','2026-01-01',1,'active')")
    conn.execute("CREATE TABLE mystery(id INTEGER PRIMARY KEY, x TEXT)")
    conn.execute("INSERT INTO mystery VALUES(1, 'a')")
    conn.commit()
    conn.close()
    dump(home)
    live_exec(home, "UPDATE mystery SET x = 'b' WHERE id = 1")
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert any("mystery" in f and "column x" in f and "unclassified" in f for f in rep.failures), rep.failures
    assert rep.classification["mystery"]["unclassified"] == ["id", "x"]
    assert rd.classify("mystery", "x") == rd.UNCLASSIFIED


def test_d3b_the_classification_table_is_pinned():
    wo, mu = rd.WRITE_ONCE, rd.MUTABLE
    pinned = {
        ("nodes", "claim"): wo, ("nodes", "quoted_text"): wo, ("nodes", "id"): wo, ("nodes", "created_at"): wo,
        ("nodes", "type"): wo, ("nodes", "evidence_id"): wo,
        ("nodes", "recall_count"): mu, ("nodes", "recall_turn"): mu, ("nodes", "importance_score"): mu,
        ("nodes", "utility_score"): mu, ("nodes", "memory_status"): mu, ("nodes", "confidence"): mu,
        ("nodes", "metadata"): rd.KEYED,
        ("nodes", "status"): rd.LIFECYCLE, ("nodes", "is_current"): rd.LIFECYCLE,
        ("nodes", "superseded_by"): rd.LIFECYCLE, ("nodes", "resolved_by"): rd.LIFECYCLE,
        ("edges", "source_id"): wo, ("edges", "target_id"): wo, ("edges", "relation"): wo,
        ("edit_history", "details"): wo, ("diagnostic_history", "metrics"): mu, ("diagnostic_history", "timestamp"): wo, ("tool_timing", "duration_ms"): wo,
        ("focus_state", "active_set_name"): mu, ("sequences", "last_num"): mu,
        ("edges", "metadata"): rd.UNCLASSIFIED, ("nodes", "trust_tier"): rd.AUDIT_GATED,
        ("nodes", "source_type"): rd.AUDIT_GATED,
        ("a_table_nobody_ruled_on", "any"): rd.UNCLASSIFIED,
    }
    for (t, c), want in pinned.items():
        assert rd.classify(t, c) == want, (t, c)


def test_d3b_every_real_nodes_column_is_classified_or_listed(home, tmp_path):
    """A column added to `nodes` later lands in the unclassified list on purpose; this pins today's list so
    the addition is a visible decision."""
    rep = drill(home, tmp_path / "scratch")
    c = rep.classification["nodes"]
    assert set(c[rd.WRITE_ONCE]) >= {"claim", "quoted_text", "id", "type", "created_at"}
    assert set(c[rd.MUTABLE]) >= {"recall_count", "importance_score", "utility_score", "memory_status"}
    assert "claim" not in c[rd.UNCLASSIFIED] and "recall_count" not in c[rd.UNCLASSIFIED]
    assert {"standpoint_author_id"} <= set(c[rd.UNCLASSIFIED])
    assert {"trust_tier", "source_type"} <= set(c[rd.AUDIT_GATED])
    assert not {"trust_tier", "source_type"} & set(c[rd.UNCLASSIFIED])


# ---------------------------------------------------------------- D4

def test_d4_rows_written_after_the_dump_are_reported_not_failed(home, tmp_path):
    for nid in ("ob_0098", "ob_0099"):
        live_exec(home, "INSERT INTO nodes(id,type,claim,created_at,is_current,status) VALUES(?,?,?,?,1,'active')",
                  (nid, "observation", "written after the dump", "2026-02-01"))
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert rep.live_only == ["ob_0098", "ob_0099"]
    assert rep.nodes_restored == len(CLAIMS)


def test_d4_a_node_in_the_dump_missing_from_live_is_a_failure(home, tmp_path):
    live_exec(home, "DELETE FROM nodes WHERE id = 'ob_0003'")
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert any("ob_0003" in f and "missing from live" in f for f in rep.failures), rep.failures


# ---------------------------------------------------------------- D5

def test_d5_a_node_without_a_vector_after_the_re_embed_is_a_failure(home, tmp_path, monkeypatch):
    def lossy(texts, scratch=None):
        return [None if t == CLAIMS["ob_0002"] else _stub_vec(t) for t in texts]
    monkeypatch.setattr(rd, "_embed_texts", lossy)
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert rep.vectors_missing == ["ob_0002"]
    assert any("ob_0002" in f and "no vector" in f for f in rep.failures)


def test_d5_every_node_has_a_vector_and_the_sample_matches(home, tmp_path):
    rep = drill(home, tmp_path / "scratch", sample=3)
    assert rep.exit_code == 0, rep.failures
    assert rep.vectors_missing == [] and rep.vectors_embedded == len(CLAIMS)
    assert rep.vector_sample_checked == 3
    conn = sqlite3.connect(tmp_path / "scratch" / "knowledge.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE embedding IS NULL").fetchone()[0] == 0
    finally:
        conn.close()


def test_d5_a_sample_vector_that_differs_from_the_restored_one_is_a_failure(home, tmp_path, monkeypatch):
    calls = []

    def drifting(texts, scratch=None):
        calls.append(len(texts))
        out = [_stub_vec(t) for t in texts]
        if len(calls) > 1:  # the SAMPLE pass disagrees with what the first pass stored
            out[0] = [9.0, 9.0, 9.0]
        return out
    monkeypatch.setattr(rd, "_embed_texts", drifting)
    rep = drill(home, tmp_path / "scratch", sample=len(CLAIMS))
    assert rep.exit_code == 1
    assert any("sample re-embed differs" in f for f in rep.failures), rep.failures


def test_d5_float_noise_between_batches_is_not_a_difference(home, tmp_path, monkeypatch):
    """The real model is not bit-repeatable across batch shapes (measured: 1 of 50 sampled differed); noise of
    1e-9 must pass while a real difference (the cell above) still fails."""
    calls = []

    def noisy(texts, scratch=None):
        calls.append(len(texts))
        out = [_stub_vec(t) for t in texts]
        if len(calls) > 1:
            out = [[x + 1e-9 for x in v] for v in out]
        return out
    monkeypatch.setattr(rd, "_embed_texts", noisy)
    rep = drill(home, tmp_path / "scratch", sample=len(CLAIMS))
    assert rep.exit_code == 0, rep.failures
    assert rep.vector_sample_checked == len(CLAIMS)


def test_d5_a_stale_live_vector_is_counted_not_failed(home, tmp_path):
    live_exec(home, "UPDATE nodes SET embedding = ? WHERE id = 'ob_0001'", (json.dumps([5.0, 5.0, 5.0]),))
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert rep.live_vectors_stale == 1


# ---------------------------------------------------------------- D6

def _transcripts(tmp_path):
    src = tmp_path / "projects"
    (src / "proj").mkdir(parents=True)
    (src / "proj" / "a.jsonl").write_bytes(b'{"n":1}\n{"n":2}\n')
    (src / "proj" / "b.jsonl").write_bytes(b'{"b":1}\n')
    return src


def _archive(home, src):
    rd.at.archive_run(str(src), str(home / "transcript-archive"))


def test_d6_replayed_transcripts_equal_the_live_prefix(home, tmp_path):
    src = _transcripts(tmp_path)
    _archive(home, src)
    rep = drill(home, tmp_path / "scratch", source=src)
    assert rep.exit_code == 0, rep.failures
    assert rep.transcripts_replayed == 2
    assert rep.transcript_live_only_bytes == 0
    assert (tmp_path / "scratch" / "transcripts" / "proj" / "a.jsonl").read_bytes() == b'{"n":1}\n{"n":2}\n'


def test_d6_bytes_appended_after_the_archive_are_reported_not_failed(home, tmp_path):
    src = _transcripts(tmp_path)
    _archive(home, src)
    with open(src / "proj" / "a.jsonl", "ab") as f:
        f.write(b'{"n":3}\n')
    rep = drill(home, tmp_path / "scratch", source=src)
    assert rep.exit_code == 0, rep.failures
    assert rep.transcript_live_only_bytes == len(b'{"n":3}\n')


def test_d6_a_live_transcript_that_differs_inside_the_archived_prefix_is_a_failure(home, tmp_path):
    src = _transcripts(tmp_path)
    _archive(home, src)
    (src / "proj" / "b.jsonl").write_bytes(b'{"b":9}\n')  # same length, different bytes
    rep = drill(home, tmp_path / "scratch", source=src)
    assert rep.exit_code == 1
    assert any("proj/b.jsonl" in f and "differ" in f for f in rep.failures), rep.failures


def test_d6_a_corrupt_archive_byte_is_a_failure_not_a_traceback(home, tmp_path):
    src = _transcripts(tmp_path)
    _archive(home, src)
    arc = next((home / "transcript-archive").glob("transcripts-*.xz"))
    raw = bytearray(arc.read_bytes())
    for i in range(len(raw) // 2, len(raw) // 2 + 8):
        raw[i] ^= 0xFF
    arc.write_bytes(bytes(raw))
    rep = drill(home, tmp_path / "scratch", source=src)
    assert rep.exit_code == 1
    assert any("transcript replay failed" in f for f in rep.failures), rep.failures


def test_d6_a_deleted_middle_archive_surfaces_the_gap(home, tmp_path):
    src = _transcripts(tmp_path)
    for _ in range(3):
        with open(src / "proj" / "a.jsonl", "ab") as f:
            f.write(b'{"more":1}\n')
        _archive(home, src)
    files = sorted((home / "transcript-archive").glob("transcripts-*.xz"))
    assert len(files) == 3
    files[1].unlink()
    rep = drill(home, tmp_path / "scratch", source=src)
    assert rep.exit_code == 1
    assert any("ReplayGap" in f and "gap" in f for f in rep.failures), rep.failures


# ---------------------------------------------------------------- D7

def _refused(home, scratch):
    before = tree_state(home)
    with pytest.raises(rd.DrillRefused):
        rd.run_drill(home=home, scratch=scratch, source=home / "x")
    assert tree_state(home) == before


def test_d7_scratch_inside_the_live_home_is_refused(home):
    target = home / "scratch-in-home"
    _refused(home, target)
    assert not target.exists()


def test_d7_scratch_equal_to_the_home_is_refused(home):
    _refused(home, home)


def test_d7_a_non_empty_scratch_is_refused_and_left_alone(home, tmp_path):
    scratch = tmp_path / "busy"
    scratch.mkdir()
    (scratch / "keep.txt").write_text("mine")
    before = tree_state(scratch)
    _refused(home, scratch)
    assert tree_state(scratch) == before


def test_d7_cli_exit_codes(home, tmp_path, capsys):
    assert rd.main(["--home", str(home), "--scratch", str(home / "inside")]) == 2
    missing = tmp_path / "nohome"
    missing.mkdir()
    assert rd.main(["--home", str(missing), "--scratch", str(tmp_path / "s")]) == 2
    (home / "knowledge.sql").unlink()
    assert rd.main(["--home", str(home), "--scratch", str(tmp_path / "s2")]) == 2
    assert not (tmp_path / "s2").exists()


# ---------------------------------------------------------------- D8

def test_d8_the_connect_runs_in_a_subprocess_rooted_at_the_scratch_home(home, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_NO_DB_GUARDS", "1")  # a bypass in the parent must not ride into the subprocess
    monkeypatch.setenv("ENGRAM_BOOTSTRAP", "1")
    seen = []
    real = subprocess.run

    def spy(args, **kw):
        if isinstance(args, list) and args[:2] == [sys.executable, "-c"]:
            seen.append(kw["env"])
        return real(args, **kw)
    monkeypatch.setattr(rd.subprocess, "run", spy)
    scratch = tmp_path / "scratch"
    rep = drill(home, scratch, assert_quiescent=True)
    assert rep.exit_code == 0, rep.failures
    assert seen, "the first server connect did not run in a subprocess"
    for env in seen:
        assert env["ENGRAM_HOME"] == str(scratch) and env["HOME"] == str(scratch)
        assert "ENGRAM_NO_DB_GUARDS" not in env and "ENGRAM_BOOTSTRAP" not in env
    assert rep.schema["user_version_after"] == rep.schema["latest"]


def test_d8_a_quiescent_live_home_is_byte_and_mtime_identical_after(home, tmp_path):
    (home / "sessions").mkdir()
    (home / "sessions" / "s1.json").write_text("{}")
    before = tree_state(home)
    rep = drill(home, tmp_path / "scratch", assert_quiescent=True)
    assert rep.exit_code == 0, rep.failures
    assert rep.live_home_observed == []
    assert tree_state(home) == before


def _touch_live_during_drill(monkeypatch, home):
    def writing(texts, scratch=None):
        (home / "sessions").mkdir(exist_ok=True)
        (home / "sessions" / "during.json").write_text("{}")
        return stub_embed(texts)
    monkeypatch.setattr(rd, "_embed_texts", writing)


def test_d8_a_change_during_a_quiescent_drill_is_a_failure(home, tmp_path, monkeypatch):
    _touch_live_during_drill(monkeypatch, home)
    rep = drill(home, tmp_path / "scratch", assert_quiescent=True)
    assert rep.exit_code == 1
    assert any("live home changed" in f for f in rep.failures), rep.failures


def test_d8_a_change_on_a_live_run_is_recorded_not_attributed_and_not_failed(home, tmp_path, monkeypatch):
    _touch_live_during_drill(monkeypatch, home)
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert any("sessions/during.json" in c for c in rep.live_home_observed)
    assert "observed during drill, not attributed" in rd.format_report(rep)


def test_d8_an_old_schema_is_migrated_in_the_scratch_and_reported(home, tmp_path):
    sql = home / "knowledge.sql"
    text = sql.read_text(encoding="utf-8")
    # The fixture dump is taken at the code's latest schema version, which moves with dev: read it, never hard-code it.
    found = re.findall(r"PRAGMA user_version = (\d+);", text)
    assert len(found) == 1, found
    current = int(found[0])
    sql.write_text(text.replace(f"PRAGMA user_version = {current};", f"PRAGMA user_version = {current - 1};"),
                   encoding="utf-8")
    rep = drill(home, tmp_path / "scratch")
    assert rep.schema["latest"] == current, "the fixture dump is at the latest version, so one step back is an old schema"
    assert rep.schema["dump_user_version"] == current - 1
    assert rep.schema["user_version_after"] == rep.schema["latest"]
    assert rep.schema["migrated_in_scratch"] is True
    assert "migrated in the scratch copy" in rd.format_report(rep)


# ---------------------------------------------------------------- D9 / D10

def test_d9_the_json_report_has_the_named_keys(home, tmp_path, capsys):
    rc = rd.main(["--home", str(home), "--scratch", str(tmp_path / "scratch"), "--source", str(tmp_path / "none"),
                  "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    for key in ("nodes_restored", "rows_compared", "live_only", "vectors_missing", "live_vectors_stale",
                "transcripts_replayed", "transcript_live_only_bytes", "seconds", "failures"):
        assert key in data, key
    assert data["failures"] == [] and data["nodes_restored"] == len(CLAIMS)


def test_d9_the_plain_summary_mirrors_the_keys_and_names_the_dump_source(home, tmp_path, capsys):
    rc = rd.main(["--home", str(home), "--scratch", str(tmp_path / "scratch"), "--source", str(tmp_path / "none")])
    out = capsys.readouterr().out
    assert rc == 0
    for frag in ("nodes_restored=4", "rows_compared=", "live_only=0", "vectors_missing=0", "live_vectors_stale=0",
                 "transcripts_replayed=0", "transcript_live_only_bytes=0", "failures=0", "PASSED"):
        assert frag in out, frag
    assert "dump: working-tree" in out and "mtime" in out


def test_d9_the_git_source_names_the_commit(home, tmp_path):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-q"], ["add", "knowledge.sql"], ["commit", "-q", "-m", "dump"]):
        subprocess.run(["git", "-C", str(home), *args], check=True, capture_output=True, env=env)
    head = subprocess.run(["git", "-C", str(home), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    rep = drill(home, tmp_path / "scratch", from_git=True)
    assert rep.exit_code == 0, rep.failures
    assert rep.dump_source["kind"] == "git" and rep.dump_source["commit"] == head


def test_d10_no_embed_skips_the_model_and_says_so(home, tmp_path, monkeypatch, capsys):
    def boom(texts, scratch=None):
        raise AssertionError("the embedder must not be called under --no-embed")
    monkeypatch.setattr(rd, "_embed_texts", boom)
    rc = rd.main(["--home", str(home), "--scratch", str(tmp_path / "scratch"), "--source", str(tmp_path / "none"),
                  "--no-embed"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "embedding was skipped" in out and "embedding skipped" in out
    conn = sqlite3.connect(tmp_path / "scratch" / "knowledge.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL").fetchone()[0] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------- D11 lifecycle columns, gated by the audit record
# The one rule: a difference between the restore and live is legal ONLY if the live graph's own record explains it
# as a change made after the dump: (i) an edit_history row (or the retracted_at stamp) for that node, timestamped
# after the dump time, AND (ii) a (restored, live) pair the code can make. No record: FAIL, naming table, key, column.

T_DUMP = "2026-06-01T00:00:00+00:00"
T_BEFORE = "2026-05-01T00:00:00+00:00"
T_AFTER = "2026-07-01T00:00:00+00:00"
_MARKER_LINE = "-- engram-dumped-at:"


def _strip_marker(text: str) -> str:
    return "".join(ln for ln in text.splitlines(keepends=True) if not ln.startswith(_MARKER_LINE))


def mark_dump(home: Path, iso: str = T_DUMP) -> None:
    """Make the dump carry exactly ONE marker line, `iso` (dump_stripped already wrote one: replace it)."""
    p = home / "knowledge.sql"
    p.write_text(f"{_MARKER_LINE} {iso}\n" + _strip_marker(p.read_text(encoding="utf-8")), encoding="utf-8")


def strip_marker(home: Path) -> None:
    """An OLDER dump: no marker line at all."""
    p = home / "knowledge.sql"
    p.write_text(_strip_marker(p.read_text(encoding="utf-8")), encoding="utf-8")


def retract_sql(at):
    meta = {"retracted": True, "error_type": "wrong", "retraction_reason": "r"}
    if at:
        meta["retracted_at"] = at
    return ("UPDATE nodes SET status='retracted', is_current=0, metadata=? WHERE id='ob_0001'", (json.dumps(meta),))


def lc(rep, col):
    return rep.lifecycle.get(col, {})


def _edit(home, action, node, ts, details="{}"):
    live_exec(home, "INSERT INTO edit_history(timestamp,turn,action,node_id,node_type,details) "
                    "VALUES(?,?,?,?,?,?)", (ts, 1, action, node, "observation", details))


def _dump_with(home, sql, params=()):
    live_exec(home, sql, params)
    dump(home)
    mark_dump(home)


def _hits(rep, *needles):
    return [f for f in rep.failures if all(n in f for n in needles)]


def test_d11_retraction_with_the_retracted_at_stamp_after_the_dump_passes_and_is_counted(home, tmp_path):
    mark_dump(home)
    live_exec(home, *retract_sql(T_AFTER))
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "status")["passed"] == 1 and lc(rep, "is_current")["passed"] == 1
    assert rep.ambiguous_count == 0


def test_d11_retraction_stamped_before_the_dump_fails(home, tmp_path):
    mark_dump(home)
    live_exec(home, *retract_sql(T_BEFORE))
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert lc(rep, "status")["failed_before_dump"] == 1
    assert _hits(rep, "nodes", "ob_0001", "column status", "before the dump")


def test_d11_retraction_with_no_record_at_all_fails_naming_table_key_column(home, tmp_path):
    mark_dump(home)
    live_exec(home, *retract_sql(None))  # no retracted_at, and no edit_history row
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert lc(rep, "status")["failed_no_record"] == 1
    assert _hits(rep, "nodes", "id='ob_0001'", "column status", "no audit record")


def test_d11_retraction_recorded_only_by_an_edit_history_row_after_the_dump_passes(home, tmp_path):
    mark_dump(home)
    live_exec(home, *retract_sql(None))
    _edit(home, "retracted", "ob_0001", T_AFTER)
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures


def test_d11_a_retraction_in_the_dump_that_is_active_live_fails_even_with_a_patch_row(home, tmp_path):
    _dump_with(home, *retract_sql(T_BEFORE))  # the dump already shows ob_0001 retracted
    live_exec(home, "UPDATE nodes SET status='active', is_current=1 WHERE id='ob_0001'")
    _edit(home, "surgical_field_patch", "ob_0001", T_AFTER, json.dumps({"field": "status"}))
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert lc(rep, "status")["failed_transition"] == 1
    assert _hits(rep, "nodes", "ob_0001", "column status", "retracted", "active")


def _resolved_then_reopened(tmp_path, name, row_at):
    h = make_home(tmp_path, name)
    _dump_with(h, "UPDATE nodes SET status='resolved', resolved_by='ob_0002' WHERE id='ob_0001'")
    live_exec(h, "UPDATE nodes SET status='open', resolved_by=NULL WHERE id='ob_0001'")
    if row_at:
        _edit(h, "resolution_recomputed", "ob_0001", row_at)
    return h


def test_d11_lifecycle_transition_with_an_audit_row_after_the_dump_passes_and_is_counted(tmp_path):
    h = _resolved_then_reopened(tmp_path, "h", T_AFTER)
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "status")["passed"] == 1 and lc(rep, "resolved_by")["passed"] == 1
    assert "resolved -> open" in rep.lifecycle_transitions
    assert "_recompute_resolution_target" in rep.lifecycle_transitions["resolved -> open"]["reason"]
    assert "resolved -> open" in rd.format_report(rep)


def test_d11_lifecycle_transition_with_an_audit_row_before_the_dump_fails(tmp_path):
    h = _resolved_then_reopened(tmp_path, "h", T_BEFORE)
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    assert lc(rep, "status")["failed_before_dump"] == 1 and lc(rep, "resolved_by")["failed_before_dump"] == 1
    assert _hits(rep, "nodes", "ob_0001", "column status", "before the dump")


def test_d11_lifecycle_transition_with_no_audit_row_fails_naming_table_key_column(tmp_path):
    h = _resolved_then_reopened(tmp_path, "h", None)
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    assert _hits(rep, "nodes", "id='ob_0001'", "column status", "no audit record")
    assert _hits(rep, "nodes", "id='ob_0001'", "column resolved_by", "no audit record")
    assert lc(rep, "status")["failed_no_record"] == 1 and lc(rep, "resolved_by")["failed_no_record"] == 1


def test_d11_an_unlisted_transition_fails_even_with_an_audit_row_after_the_dump(home, tmp_path):
    mark_dump(home)
    # restored 'active' -> live 'resolved' is not a pair any code path writes; a 'resolved' row does not make it one
    live_exec(home, "UPDATE nodes SET status='resolved' WHERE id='ob_0001'")
    _edit(home, "resolved", "ob_0001", T_AFTER)
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert lc(rep, "status")["failed_transition"] == 1
    assert _hits(rep, "nodes", "ob_0001", "column status", "'active'", "'resolved'", "not a transition")


def test_d11_a_resolution_move_is_not_explained_by_another_nodes_audit_row(home, tmp_path):
    _dump_with(home, "UPDATE nodes SET status='resolved', resolved_by='ob_0003' WHERE id='ob_0002'")
    live_exec(home, "UPDATE nodes SET status='open', resolved_by=NULL WHERE id='ob_0002'")
    _edit(home, "resolution_recomputed", "ob_0003", T_AFTER)  # a row for a DIFFERENT node
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert _hits(rep, "ob_0002", "no audit record")


def test_d11_task_status_move_needs_its_status_row_after_the_dump(tmp_path):
    for name, row, want in (("yes", ("status_planned_to_done", T_AFTER), 0), ("no", None, 1),
                            ("old", ("status_planned_to_done", T_BEFORE), 1)):
        h = make_home(tmp_path, name)
        _dump_with(h, "UPDATE nodes SET status='planned' WHERE id='ob_0001'")
        live_exec(h, "UPDATE nodes SET status='done' WHERE id='ob_0001'")
        if row:
            _edit(h, row[0], "ob_0001", row[1])
        rep = drill(h, tmp_path / ("s" + name))
        assert rep.exit_code == want, (name, rep.failures)
        if not want:
            assert "planned -> done" in rep.lifecycle_transitions


def test_d11_field_patch_on_status_passes_only_with_its_own_row_naming_that_field(tmp_path):
    for name, details, want in (("status", {"field": "status"}, 0), ("claim", {"field": "claim"}, 1)):
        h = make_home(tmp_path, name)
        mark_dump(h)
        live_exec(h, "UPDATE nodes SET status='blocked' WHERE id='ob_0001'")  # active -> blocked: not a listed pair
        _edit(h, "surgical_field_patch", "ob_0001", T_AFTER, json.dumps(details))
        rep = drill(h, tmp_path / ("s" + name))
        assert rep.exit_code == want, (name, rep.failures)


def test_d11_supersede_needs_its_superseded_row_after_the_dump(tmp_path):
    for name, ts, want in (("after", T_AFTER, 0), ("before", T_BEFORE, 1), ("none", None, 1)):
        h = make_home(tmp_path, "h" + name)
        mark_dump(h)
        live_exec(h, "UPDATE nodes SET is_current=0, superseded_by='ob_0003' WHERE id='ob_0001'")
        if ts:
            _edit(h, "superseded", "ob_0001", ts)
        rep = drill(h, tmp_path / ("s" + name))
        assert rep.exit_code == want, (name, rep.failures)
        if want:
            assert _hits(rep, "nodes", "ob_0001", "column superseded_by")
        else:
            assert lc(rep, "superseded_by")["passed"] == 1 and lc(rep, "is_current")["passed"] == 1


def test_d11_backward_is_current_and_changed_superseded_by_fail_even_with_a_row(tmp_path):
    h = make_home(tmp_path, "h2")
    _dump_with(h, "UPDATE nodes SET is_current=0, superseded_by='ob_0002' WHERE id='ob_0001'")
    live_exec(h, "UPDATE nodes SET is_current=1, superseded_by='ob_0003' WHERE id='ob_0001'")
    _edit(h, "superseded", "ob_0001", T_AFTER)
    rep = drill(h, tmp_path / "s2")
    assert rep.exit_code == 1
    assert _hits(rep, "ob_0001", "column is_current")
    assert _hits(rep, "ob_0001", "column superseded_by", "ob_0002", "ob_0003")
    h3 = make_home(tmp_path, "h3")
    _dump_with(h3, "UPDATE nodes SET superseded_by='ob_0003' WHERE id='ob_0001'")
    live_exec(h3, "UPDATE nodes SET superseded_by=NULL WHERE id='ob_0001'")
    rep = drill(h3, tmp_path / "s3")
    assert rep.exit_code == 1 and any("ob_0001" in f and "column superseded_by" in f for f in rep.failures)


def test_d11_resolved_by_cleared_while_status_is_not_open_fails(tmp_path):
    h = make_home(tmp_path, "h")
    _dump_with(h, "UPDATE nodes SET status='resolved', resolved_by='ob_0002' WHERE id='ob_0001'")
    live_exec(h, "UPDATE nodes SET status='resolved', resolved_by=NULL WHERE id='ob_0001'")
    _edit(h, "resolution_recomputed", "ob_0001", T_AFTER)
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    assert any("ob_0001" in f and "column resolved_by" in f for f in rep.failures), rep.failures


def test_d11_a_writer_with_no_audit_row_is_named_and_its_difference_fails(home, tmp_path):
    mark_dump(home)
    # restored resolved_by NULL -> live a resolver, with no edit_history row: what _backfill_resolved_by writes
    live_exec(home, "UPDATE nodes SET resolved_by='ob_0002' WHERE id='ob_0001'")
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    hit = _hits(rep, "nodes", "ob_0001", "column resolved_by", "no audit record")
    assert hit and "_backfill_resolved_by" in hit[0] and "_resolve_impl" in hit[0], rep.failures
    gaps = " ".join(rd.NO_AUDIT_WRITERS)
    assert "engram_core._backfill_resolved_by" in gaps and "engram_revision._resolve_impl" in gaps
    assert "Known gaps" in rd.__doc__ and "_backfill_resolved_by" in rd.__doc__
    assert "known gap" in rd.format_report(rep).lower()


def test_d11_the_same_difference_with_a_row_after_the_dump_passes(home, tmp_path):
    mark_dump(home)
    live_exec(home, "UPDATE nodes SET resolved_by='ob_0002' WHERE id='ob_0001'")
    _edit(home, "resolution_recomputed", "ob_0001", T_AFTER)
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures


def test_d11_a_mutable_column_still_only_reported(home, tmp_path):
    mark_dump(home)
    live_exec(home, "UPDATE nodes SET confidence = 0.1 WHERE id = 'ob_0001'")
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert rep.mutable_diffs.get("nodes.confidence") == 1


# ---------------------------------------------------------------- D11b the dump time: marker, commit window, mtime

_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com"}


def _git(home, *args, date=None):
    env = {**os.environ, **_GIT_ENV}
    if date:
        env.update(GIT_COMMITTER_DATE=date, GIT_AUTHOR_DATE=date)
    subprocess.run(["git", "-C", str(home), *args], check=True, capture_output=True, env=env)


T_C1 = "2026-05-01T00:00:00+00:00"   # the previous commit of knowledge.sql
T_C2 = "2026-06-01T00:00:00+00:00"   # this commit
T_BETWEEN = "2026-05-15T00:00:00+00:00"
T_BEFORE_C1 = "2026-04-01T00:00:00+00:00"
T_AFTER_C2 = "2026-07-01T00:00:00+00:00"


def two_commit_home(tmp_path, name="h"):
    """An OLDER dump (no marker) committed twice, T_C1 then T_C2; ob_0001 is then retracted live."""
    h = make_home(tmp_path, name)
    strip_marker(h)
    _git(h, "init", "-q")
    _git(h, "add", "knowledge.sql")
    _git(h, "commit", "-q", "-m", "one", date=T_C1)
    live_exec(h, "UPDATE nodes SET confidence = 0.5 WHERE id = 'ob_0003'")
    dump(h)
    strip_marker(h)
    _git(h, "add", "knowledge.sql")
    _git(h, "commit", "-q", "-m", "two", date=T_C2)
    live_exec(h, *retract_sql(None))
    return h


def test_d11b_marker_present_the_source_is_marker_even_with_commits(tmp_path):
    h = two_commit_home(tmp_path)
    mark_dump(h, T_DUMP)
    _git(h, "add", "knowledge.sql")
    _git(h, "commit", "-q", "-m", "three", date=T_AFTER_C2)
    _edit(h, "retracted", "ob_0001", T_AFTER_C2)
    rep = drill(h, tmp_path / "s")
    assert rep.dump_time_source == "marker"
    assert rep.dump_time.startswith("2026-06-01T00:00:00") and rep.dump_time_earliest == rep.dump_time
    assert "dump_time_source: marker" in rd.format_report(rep)


def test_d11b_no_marker_and_two_commits_the_window_is_the_two_commit_times(tmp_path):
    h = two_commit_home(tmp_path)
    _edit(h, "retracted", "ob_0001", T_AFTER_C2)
    rep = drill(h, tmp_path / "s")
    assert rep.dump_time_source == "commit-window"
    assert rep.dump_time_earliest.startswith("2026-05-01T00:00:00") and rep.dump_time.startswith("2026-06-01T00:00:00")
    assert "commit-window" in rd.format_report(rep)
    rep2 = drill(h, tmp_path / "s2", from_git=True)
    assert rep2.dump_time_source == "commit-window" and rep2.dump_time_earliest.startswith("2026-05-01")


def test_d11b_a_change_after_this_commit_passes_before_the_previous_commit_fails(tmp_path):
    h = two_commit_home(tmp_path, "ha")
    _edit(h, "retracted", "ob_0001", T_AFTER_C2)
    rep = drill(h, tmp_path / "sa")
    assert rep.exit_code == 0, rep.failures
    assert rep.ambiguous_count == 0 and lc(rep, "status")["passed"] == 1
    h2 = two_commit_home(tmp_path, "hb")
    _edit(h2, "retracted", "ob_0001", T_BEFORE_C1)
    rep2 = drill(h2, tmp_path / "sb")
    assert rep2.exit_code == 1
    assert lc(rep2, "status")["failed_before_dump"] == 1 and rep2.ambiguous_count == 0


def test_d11b_a_change_between_the_two_commits_is_reported_as_ambiguous_and_exits_0(tmp_path):
    h = two_commit_home(tmp_path)
    _edit(h, "retracted", "ob_0001", T_BETWEEN)
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 0, rep.failures
    assert rep.failures == []
    assert rep.ambiguous_count == 2  # status and is_current, both explained only by that row
    assert {a["column"] for a in rep.ambiguous} == {"status", "is_current"}
    assert all(a["key"] == "ob_0001" and a["table"] == "nodes" for a in rep.ambiguous)
    out = rd.format_report(rep)
    assert "ambiguous: dump time unknown" in out and "column status" in out
    assert rep.to_json()["ambiguous_count"] == 2 and rep.to_json()["passed"] is True


def test_d11b_a_single_commit_leaves_the_earlier_bound_unknown_never_invented(tmp_path):
    h = make_home(tmp_path)
    strip_marker(h)
    _git(h, "init", "-q")
    _git(h, "add", "knowledge.sql")
    _git(h, "commit", "-q", "-m", "one", date=T_C2)
    live_exec(h, *retract_sql(None))
    _edit(h, "retracted", "ob_0001", T_BEFORE_C1)  # before the only commit: nothing says it predates the dump
    rep = drill(h, tmp_path / "s")
    assert rep.dump_time_source == "commit-window"
    assert rep.dump_time_earliest == "" and "earliest unknown" in rd.format_report(rep)
    assert rep.exit_code == 0 and rep.ambiguous_count == 2


def test_d11b_mtime_fallback_for_a_dump_with_no_repo_and_no_marker(home, tmp_path):
    strip_marker(home)
    os.utime(home / "knowledge.sql", (1_700_000_000, 1_700_000_000))
    rep = drill(home, tmp_path / "scratch")
    assert rep.dump_time_source == "mtime-fallback"
    assert rep.dump_time.startswith("2023-11-14T22:13:20")
    assert "weakest" in rd.format_report(rep)


# ---------------------------------------------------------------- D11c the marker, written by dump_stripped

def test_d11c_dump_stripped_writes_exactly_one_marker_line_first(home):
    lines = (home / "knowledge.sql").read_text(encoding="utf-8").splitlines()
    marks = [ln for ln in lines if ln.startswith(_MARKER_LINE)]
    assert len(marks) == 1 and lines[0] == marks[0]
    stamp = marks[0].split(":", 1)[1].strip()
    t = rd._parse_time(stamp)
    assert t is not None and t.utcoffset().total_seconds() == 0
    stats = rd.engram_backup.dump_stripped(str(home / "knowledge.db"), str(home / "again.sql"))
    assert stats["dumped_at"].startswith(stamp[:10])
    assert sum(1 for ln in (home / "again.sql").read_text(encoding="utf-8").splitlines()
               if ln.startswith(_MARKER_LINE)) == 1


def test_d11c_the_stamp_is_taken_before_the_backup_so_it_never_post_dates_the_data(home):
    import datetime as dt
    before = dt.datetime.now(dt.timezone.utc)
    dump(home)
    after = dt.datetime.now(dt.timezone.utc)
    first = (home / "knowledge.sql").read_text(encoding="utf-8").splitlines()[0]
    t = rd._parse_time(first.split(":", 1)[1].strip())
    assert before <= t <= after


def test_d11c_every_dump_consumer_loads_a_dump_that_carries_the_marker(home, tmp_path):
    db = str(home / "knowledge.db")
    text = (home / "knowledge.sql").read_text(encoding="utf-8")
    assert text.startswith(_MARKER_LINE)
    # restore (the sqlite3 module, as the drill and a human restore do)
    conn = sqlite3.connect(tmp_path / "r.db")
    conn.executescript(text)
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == len(CLAIMS)
    conn.close()
    # engram_backup's own readers
    assert rd.engram_backup._verify_dump_restores(str(home / "knowledge.sql"), len(CLAIMS)) == len(CLAIMS)
    assert rd.engram_backup.verify_roundtrip(db) is not None
    # the drill
    assert drill(home, tmp_path / "scratch").exit_code == 0


# ---------------------------------------------------------------- D16 requote: write-once under the same rule

def _requote_pair(tmp_path, name):
    h = make_home(tmp_path, name)
    mark_dump(h)
    live_exec(h, "UPDATE nodes SET quoted_text='a new quote' WHERE id='ob_0001'")
    return h


def test_d16_a_quoted_text_difference_with_a_requoted_row_after_the_dump_passes(tmp_path):
    h = _requote_pair(tmp_path, "h")
    _edit(h, "requoted", "ob_0001", T_AFTER)
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "quoted_text")["passed"] == 1


def test_d16_a_quoted_text_difference_with_no_audit_row_fails_naming_the_column(tmp_path):
    h = _requote_pair(tmp_path, "h")
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    assert _hits(rep, "nodes", "id='ob_0001'", "column quoted_text", "no audit record")


def test_d16_a_requoted_row_before_the_dump_does_not_explain_the_difference(tmp_path):
    h = _requote_pair(tmp_path, "h")
    _edit(h, "requoted", "ob_0001", T_BEFORE)
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    assert _hits(rep, "column quoted_text", "before the dump")


def test_d16_a_field_patch_on_quoted_text_also_records_it(tmp_path):
    h = _requote_pair(tmp_path, "h")
    _edit(h, "surgical_field_patch", "ob_0001", T_AFTER, json.dumps({"field": "quoted_text"}))
    assert drill(h, tmp_path / "s").exit_code == 0


def test_d16_requote_writes_an_audit_row_in_the_shipped_code():
    src = (PKG_ROOT / "engram_observation.py").read_text(encoding="utf-8")
    i = src.index("UPDATE nodes SET quoted_text = ?")
    assert '_log_edit(conn, "requoted"' in src[i:i + 1500]


# ---------------------------------------------------------------- D12 nodes.metadata key by key

def _meta_both(home, dump_meta, live_meta, nid="ob_0001"):
    live_exec(home, "UPDATE nodes SET metadata=? WHERE id=?", (json.dumps(dump_meta), nid))
    dump(home)
    mark_dump(home)
    live_exec(home, "UPDATE nodes SET metadata=? WHERE id=?", (json.dumps(live_meta), nid))


def test_d12_a_changed_write_once_metadata_key_fails_naming_the_key(home, tmp_path):
    _meta_both(home, {"reasoning_type": "deductive_modus_ponens"}, {"reasoning_type": "abductive_best_explanation"})
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert any("nodes" in f and "ob_0001" in f and "column metadata" in f and "reasoning_type" in f
               for f in rep.failures), rep.failures


@pytest.mark.parametrize("key", ["reasoning_class", "warrant", "built_on_contested", "content_hash", "git_sha",
                                 "source_class", "quote_verified"])
def test_d12_every_write_once_key_is_pinned(key):
    assert key in rd.WRITE_ONCE_METADATA_KEYS


def test_d12_a_changed_mutable_metadata_key_is_reported_not_failed(home, tmp_path):
    _meta_both(home, {"quote_history": []}, {"quote_history": [{"move": "x"}]})
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert rep.metadata_mutable_diffs == {"quote_history": 1}


def test_d12_an_unknown_metadata_key_is_listed_and_a_difference_on_it_fails(home, tmp_path):
    _meta_both(home, {"made_up_key_zz": 1}, {"made_up_key_zz": 2})
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert "made_up_key_zz" in rep.metadata_unknown
    assert any("ob_0001" in f and "made_up_key_zz" in f for f in rep.failures), rep.failures
    assert "made_up_key_zz" in rd.format_report(rep)


def test_d12_an_unknown_key_is_listed_even_when_nothing_differs(home, tmp_path):
    _meta_both(home, {"made_up_key_zz": 1}, {"made_up_key_zz": 1})
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert rep.metadata_unknown == {"made_up_key_zz": 1}


def test_d12_the_retraction_stamp_keys_follow_the_lifecycle_stamp_rule(home, tmp_path):
    mark_dump(home)
    live_exec(home, *retract_sql(T_AFTER))
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 0, rep.failures
    assert "retracted_at" not in rep.metadata_unknown and "retracted" not in rep.metadata_unknown
    assert rep.metadata_mutable_diffs == {}


def test_d12_a_stamp_key_present_in_the_dump_but_changed_live_fails(home, tmp_path):
    _meta_both(home, {"retracted_at": T_BEFORE}, {"retracted_at": T_AFTER})
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert any("ob_0001" in f and "retracted_at" in f for f in rep.failures), rep.failures


def test_d12_invalid_json_on_either_side_fails_naming_the_node(home, tmp_path):
    conn = sqlite3.connect(home / "knowledge.db")  # the server's own trigger refuses invalid JSON: drop it (live only)
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND sql LIKE '%valid JSON%'").fetchall():
        conn.execute(f'DROP TRIGGER "{name}"')
    conn.execute("UPDATE nodes SET metadata='{not json' WHERE id='ob_0002'")
    conn.commit()
    conn.close()
    rep = drill(home, tmp_path / "scratch")
    assert rep.exit_code == 1
    assert any("ob_0002" in f and "metadata" in f and "JSON" in f for f in rep.failures), rep.failures


# ---------------------------------------------------------------- D13 re-embed contract

def _named(model, dim_extra=0):
    def fn(texts, scratch=None):
        out = rd.EmbedVectors([_stub_vec(t) + [0.0] * dim_extra for t in texts])
        out.model = model
        return out
    return fn


def test_d13_model_name_differs_between_passes_is_a_failure(home, tmp_path, monkeypatch):
    calls = []

    def fn(texts, scratch=None):
        calls.append(1)
        return _named("model-a" if len(calls) == 1 else "model-b")(texts)
    monkeypatch.setattr(rd, "_embed_texts", fn)
    rep = drill(home, tmp_path / "scratch", sample=2)
    assert rep.exit_code == 1
    assert any("model" in f and "model-a" in f and "model-b" in f for f in rep.failures), rep.failures


def test_d13_dimension_differs_between_passes_is_a_failure(home, tmp_path, monkeypatch):
    calls = []

    def fn(texts, scratch=None):
        calls.append(1)
        return _named("m", dim_extra=0 if len(calls) == 1 else 2)(texts)
    monkeypatch.setattr(rd, "_embed_texts", fn)
    rep = drill(home, tmp_path / "scratch", sample=2)
    assert rep.exit_code == 1
    assert any("dimension" in f for f in rep.failures), rep.failures


def test_d13_the_largest_difference_and_the_tolerance_unit_are_reported(home, tmp_path, monkeypatch, capsys):
    calls = []

    def fn(texts, scratch=None):
        calls.append(1)
        v = _named("m")(texts)
        if len(calls) > 1:
            v2 = rd.EmbedVectors([[x + 4e-6 for x in vec] for vec in v])
            v2.model = "m"
            return v2
        return v
    monkeypatch.setattr(rd, "_embed_texts", fn)
    rc = rd.main(["--home", str(home), "--scratch", str(tmp_path / "scratch"), "--source", str(tmp_path / "n"),
                  "--json", "--sample", "3"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0, data["failures"]
    assert data["vector_tolerance"]["value"] == 1e-05
    assert "element" in data["vector_tolerance"]["unit"] and "absolute" in data["vector_tolerance"]["unit"]
    assert 3e-6 < data["vector_max_diff"] < 5e-6
    assert data["embedding_model"] == "m"


def test_d13_an_unnamed_model_is_not_a_match():
    assert rd.EmbedVectors([[0.0]]).model is None


# ---------------------------------------------------------------- D14 the live-home open (R1) and sqlite errors (R3)

def _listing(home):
    return sorted(p.name for p in home.iterdir())


def test_d14_wal_without_shm_is_refused_and_nothing_is_created(home, tmp_path):
    (home / "knowledge.db-wal").write_bytes(b"")
    before = tree_state(home)
    listing = _listing(home)
    with pytest.raises(rd.DrillRefused, match="has a -wal file but no -shm file: refusing to open it read-only"):
        rd.run_drill(home=home, scratch=tmp_path / "scratch", source=home / "x")
    assert tree_state(home) == before
    assert _listing(home) == listing  # no -shm appeared
    assert not (tmp_path / "scratch").exists()
    assert rd.main(["--home", str(home), "--scratch", str(tmp_path / "s2")]) == 2
    assert _listing(home) == listing


def test_d14_shm_without_wal_is_refused_and_nothing_is_created(home, tmp_path):
    (home / "knowledge.db-shm").write_bytes(b"\0" * 32768)
    before = tree_state(home)
    with pytest.raises(rd.DrillRefused, match="has a -shm file but no -wal file"):
        rd.run_drill(home=home, scratch=tmp_path / "scratch", source=home / "x")
    assert tree_state(home) == before


def test_d14_a_sqlite_error_on_the_live_side_is_exit_2_without_a_traceback(home, tmp_path, monkeypatch, capsys):
    real = rd._columns

    def flaky(conn, table):
        files = [r[2] for r in conn.execute("PRAGMA database_list").fetchall()]
        if table == "edges" and not any("scratch" in f for f in files):
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real(conn, table)
    monkeypatch.setattr(rd, "_columns", flaky)
    rc = rd.main(["--home", str(home), "--scratch", str(tmp_path / "scratch"), "--source", str(tmp_path / "n")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "Traceback" not in err and "edges" in err and "malformed" in err


# ---------------------------------------------------------------- D15 the child environment (R4)

def test_d15_the_child_env_is_an_allowlist_and_offline_flags_are_for_the_embed_step_only(monkeypatch, tmp_path):
    for k, v in {"XDG_CACHE_HOME": "/x", "HF_TOKEN": "t", "HF_ENDPOINT": "e", "TRANSFORMERS_CACHE": "/t",
                 "ENGRAMD_FOO": "1", "ENGRAM_HOME": "/live", "CLAUDE_PLUGIN_ROOT": "/p", "SOME_SECRET": "s",
                 "LC_ALL": "C", "TZ": "UTC", "HF_HOME": "/hf"}.items():
        monkeypatch.setenv(k, v)
    scratch = str(tmp_path / "s")
    embed = rd._scratch_env(scratch, embedder_off=False)
    connect = rd._scratch_env(scratch, embedder_off=True)
    for env in (embed, connect):
        assert env["ENGRAM_HOME"] == scratch and env["HOME"] == scratch
        for bad in ("XDG_CACHE_HOME", "HF_TOKEN", "HF_ENDPOINT", "TRANSFORMERS_CACHE", "ENGRAMD_FOO",
                    "CLAUDE_PLUGIN_ROOT", "SOME_SECRET"):
            assert bad not in env, bad
        assert env["LC_ALL"] == "C" and env["TZ"] == "UTC"
    assert embed["HF_HOME"] == "/hf"
    assert embed["HF_HUB_OFFLINE"] == "1" and embed["TRANSFORMERS_OFFLINE"] == "1"
    for k in ("HF_HOME", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        assert k not in connect, k
    assert connect["ENGRAM_NO_EMBEDDINGS"] == "1" and "ENGRAM_NO_EMBEDDINGS" not in embed


# ---------------------------------------------------------------- D17 skip an unchanged dump (round 4, fold 1)
# The marker makes every dump differ by one line. Both commit paths skip a commit by the git status of
# knowledge.sql, so an unchanged graph must keep the OLD file (old marker, old bytes): the old content is still
# true from the old marker time on. The paths are run for real, in a subprocess whose home is a throwaway repo.

# One process runs the three steps, so the server's own start-up bookkeeping (embedding_restore_state.updated_at is
# rewritten on every connect) does not make the graph "changed" between the two commit calls.
_COMMIT_SCRIPT = r"""
import hashlib, json, sqlite3, subprocess, sys, time
import engram_core as core
home = core.DATA_DIR
which = sys.argv[1]
conn = core._get_db() if which == "sync" else None


def commit():
    if which == "worker":
        import engram_snapshot_worker as w
        snap = home / "captured.db"
        if snap.exists():
            snap.unlink()
        s = sqlite3.connect(str(home / "knowledge.db")); d = sqlite3.connect(str(snap)); s.backup(d); d.close(); s.close()
        w._regenerate_derived_files(snap)
        res = w._commit_snapshot_files("nap", "m")
    else:
        res = core._commit_snapshot_sync(conn, "m", "nap")
    n = subprocess.run(["git", "-C", str(home), "rev-list", "--count", "HEAD", "--", "knowledge.sql"],
                       capture_output=True, text=True).stdout.strip()
    data = (home / "knowledge.sql").read_bytes()
    return {"committed": res.get("git_committed"), "sql_commits": int(n), "sha": hashlib.sha256(data).hexdigest(),
            "marker": data.decode().splitlines()[0], "has_change": b"a claim that changed" in data}


steps = [commit()]
time.sleep(0.02)
steps.append(commit())          # the same graph
other = sqlite3.connect(str(home / "knowledge.db"))
other.execute("UPDATE nodes SET claim = 'a claim that changed' WHERE id = 'ob_0003'"); other.commit(); other.close()
time.sleep(0.02)
steps.append(commit())          # a changed graph
print("RESULT " + json.dumps(steps))
"""


def _commit_env(home):
    return {**_clean_env(home), **_GIT_ENV, "GIT_TERMINAL_PROMPT": "0"}


def _result_line(p):
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads([ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")][-1][7:])


@pytest.mark.parametrize("which", ["worker", "sync"])
def test_d17_an_unchanged_graph_makes_no_new_commit_and_keeps_the_old_file(home, which):
    _git(home, "init", "-q")
    p = subprocess.run([sys.executable, "-c", _COMMIT_SCRIPT, which], env=_commit_env(home), capture_output=True,
                       text=True, timeout=300)
    first, again, changed = _result_line(p)
    assert first["committed"] is True and first["sql_commits"] == 1, first
    # the same graph: its dump differs from the committed one by the marker only
    assert again["sql_commits"] == 1, "an unchanged graph made a new knowledge.sql commit"
    assert again["sha"] == first["sha"] and again["marker"] == first["marker"], "the old file (old marker) was not kept"
    # a changed graph commits, and the new dump carries the new marker
    assert changed["sql_commits"] == 2 and changed["has_change"] and not first["has_change"]
    assert changed["marker"] > first["marker"]


def test_d17_a_dump_that_differs_only_in_the_marker_is_unchanged(tmp_path):
    bk = rd.engram_backup
    body = b"BEGIN TRANSACTION;\nINSERT INTO t VALUES(1);\nCOMMIT;\nPRAGMA user_version = 3;\n"
    old = b"-- engram-dumped-at: 2026-01-01T00:00:00.000000Z\n" + body
    new = tmp_path / "new.sql"
    new.write_bytes(b"-- engram-dumped-at: 2026-02-02T00:00:00.000000Z\n" + body)
    assert bk.dump_matches_committed(new, old) is True
    (tmp_path / "old.sql").write_bytes(old)
    assert bk.dump_matches_committed(str(new), tmp_path / "old.sql") is True  # an old file by path
    assert bk.dump_matches_committed(new, body) is True  # an older committed dump with no marker at all
    new.write_bytes(b"-- engram-dumped-at: 2026-02-02T00:00:00.000000Z\n" + body.replace(b"VALUES(1)", b"VALUES(2)"))
    assert bk.dump_matches_committed(new, old) is False  # any other change is a change
    # only the FIRST line is the marker: the same text deeper in the file is content
    new.write_bytes(b"-- engram-dumped-at: 2026-02-02T00:00:00.000000Z\n" + body + b"-- engram-dumped-at: x\n")
    assert bk.dump_matches_committed(new, old) is False
    assert bk.dump_matches_committed(new, None) is False
    assert bk.dump_matches_committed(tmp_path / "absent.sql", old) is False


# ---------------------------------------------------------------- D18 the marker is the CAPTURE time (fold 2)

T_CAPTURE = "2026-06-01T00:00:00.000000Z"
T_LIVE_RETRACT = "2026-06-01T00:00:01+00:00"  # live, after the capture, before any real dump time


def _captured_then_retracted(tmp_path, name):
    h = make_home(tmp_path, name)
    cap = h / "captured.db"
    src = sqlite3.connect(h / "knowledge.db")
    dst = sqlite3.connect(cap)
    src.backup(dst)
    dst.close()
    src.close()
    live_exec(h, *retract_sql(T_LIVE_RETRACT))
    _edit(h, "retracted", "ob_0001", T_LIVE_RETRACT)
    return h, cap


def test_d18_a_dump_stamped_with_the_capture_time_passes_where_the_dump_time_failed(tmp_path):
    h, cap = _captured_then_retracted(tmp_path, "h")
    bk = rd.engram_backup
    bk.dump_stripped(str(cap), str(h / "knowledge.sql"))  # the old behaviour: stamped now, after the retract
    rep = drill(h, tmp_path / "s1")
    assert rep.exit_code == 1 and _hits(rep, "ob_0001", "column status", "before the dump time"), rep.failures
    stats = bk.dump_stripped(str(cap), str(h / "knowledge.sql"), dumped_at=T_CAPTURE)
    assert stats["dumped_at"] == T_CAPTURE
    assert (h / "knowledge.sql").read_text(encoding="utf-8").splitlines()[0] == f"{_MARKER_LINE} {T_CAPTURE}"
    rep = drill(h, tmp_path / "s2")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "status")["passed"] == 1 and rep.dump_time_source == "marker"


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-06-01", "2026-06-01T00:00:00.000000Z\nDROP TABLE nodes;",
                                 "2026-06-01 00:00:00", 5])
def test_d18_a_malformed_capture_time_is_refused_and_the_target_is_left_alone(home, bad):
    before = (home / "knowledge.sql").read_bytes()
    with pytest.raises((ValueError, TypeError)):
        rd.engram_backup.dump_stripped(str(home / "knowledge.db"), str(home / "knowledge.sql"), dumped_at=bad)
    assert (home / "knowledge.sql").read_bytes() == before
    assert not [p for p in home.iterdir() if p.name.startswith((".engram-sql-", ".engram-bk-"))]


_CAPTURE_SCRIPT = r"""
import json, sqlite3, time
import engram_core as core
import engram_snapshot_worker as w
w.start_worker = lambda: None            # no thread: the job stays pending until the drain below
conn = core._get_db()
assert core._commit_snapshot_async(conn, "m", "nap", False) is not None   # captures, enqueues
time.sleep(0.05)
stamp = core._now()                      # a live change AFTER the capture, with its audit row
conn.execute("UPDATE nodes SET status='retracted', is_current=0, metadata=? WHERE id='ob_0001'",
             (json.dumps({"retracted": True, "error_type": "wrong", "retraction_reason": "r", "retracted_at": stamp}),))
conn.execute("INSERT INTO edit_history(timestamp,turn,action,node_id,node_type,details) VALUES(?,?,?,?,?,?)",
             (stamp, 1, "retracted", "ob_0001", "observation", "{}"))
conn.commit()
time.sleep(0.05)
w._drain_pending()                       # the worker dumps the captured copy, later than the capture
print("RESULT " + json.dumps({"retracted_at": stamp}))
"""


def test_d18_the_snapshot_worker_stamps_the_capture_time_not_the_dump_time(home, tmp_path):
    _git(home, "init", "-q")
    p = subprocess.run([sys.executable, "-c", _CAPTURE_SCRIPT], env=_commit_env(home), capture_output=True,
                       text=True, timeout=300)
    stamp = _result_line(p)["retracted_at"]
    marker = rd._parse_time((home / "knowledge.sql").read_text(encoding="utf-8").splitlines()[0].split(":", 1)[1].strip())
    assert marker < rd._parse_time(stamp), "the marker is not earlier than a change made after the capture"
    rep = drill(home, tmp_path / "s")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "status")["passed"] == 1 and rep.dump_time_source == "marker"


_JOB_SCRIPT = r"""
import json, os, sqlite3
import engram_core as core
import engram_snapshot_worker as w
home = core.DATA_DIR
CAPTURED = "2026-03-04T05:06:07.123456Z"
w.enqueue_job(7, 1, "m", "nap", captured_at=CAPTURED)
job = [j for j in w._pending_jobs() if (j.get("turn"), j.get("seq")) == (7, 1)][0]
snap = w._pending_dir() / job["snapshot"]
snap.parent.mkdir(parents=True, exist_ok=True)
s = sqlite3.connect(str(home / "knowledge.db")); d = sqlite3.connect(str(snap)); s.backup(d); d.close(); s.close()
os.utime(snap, (1000000000, 1000000000))  # 2001-09-09: clearly not the capture time
w._process_job(job)
first = (home / "knowledge.sql").read_text(encoding="utf-8").splitlines()[0]
print("RESULT " + json.dumps({"job_captured_at": job.get("captured_at"), "first": first, "expected": CAPTURED}))
"""


def test_d18_the_job_record_carries_captured_at_and_the_worker_stamps_exactly_that(home):
    """enqueue_job writes the field; the dump's marker is the job's captured_at, never the file's mtime (which this
    cell sets to a clearly different instant)."""
    _git(home, "init", "-q")
    p = subprocess.run([sys.executable, "-c", _JOB_SCRIPT], env=_commit_env(home), capture_output=True,
                       text=True, timeout=300)
    res = _result_line(p)
    assert res["job_captured_at"] == res["expected"], "enqueue_job dropped captured_at"
    assert res["first"] == _MARKER_LINE + " " + res["expected"], res["first"]


# ---------------------------------------------------------------- D19 trust_tier and source_type are audit-gated (fold 3)

def _col_pair(tmp_path, name, col, dump_value, live_value):
    h = make_home(tmp_path, name)
    live_exec(h, f"UPDATE nodes SET {col} = ? WHERE id = 'ob_0001'", (dump_value,))
    dump(h)
    mark_dump(h)
    live_exec(h, f"UPDATE nodes SET {col} = ? WHERE id = 'ob_0001'", (live_value,))
    return h


def test_d19_the_table_names_each_gated_column_and_key():
    t = rd.AUDIT_GATED_COLUMNS
    assert {("nodes", "trust_tier"), ("nodes", "source_type"), ("nodes.metadata", "trust_tier")} <= set(t)
    assert "trust_tier_set" in t[("nodes", "trust_tier")]["actions"]
    assert "self_anchor_transferred" in t[("nodes", "trust_tier")]["actions"]


@pytest.mark.parametrize("action", ["trust_tier_set", "self_anchor_transferred"])
def test_d19_trust_tier_changed_with_an_audit_row_after_the_dump_passes(tmp_path, action):
    h = _col_pair(tmp_path, "h", "trust_tier", "unknown", "our_side")
    _edit(h, action, "ob_0001", T_AFTER)
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "trust_tier")["passed"] == 1


def test_d19_trust_tier_changed_without_a_row_fails_naming_table_key_column(tmp_path):
    h = _col_pair(tmp_path, "h", "trust_tier", "unknown", "our_side")
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    assert lc(rep, "trust_tier")["failed_no_record"] == 1
    assert _hits(rep, "nodes", "id='ob_0001'", "column trust_tier", "no audit record")


def test_d19_a_row_before_the_dump_or_for_another_action_does_not_explain_trust_tier(tmp_path):
    h = _col_pair(tmp_path, "h", "trust_tier", "unknown", "our_side")
    _edit(h, "trust_tier_set", "ob_0001", T_BEFORE)
    assert _hits(drill(h, tmp_path / "s"), "column trust_tier", "before the dump")
    h2 = _col_pair(tmp_path, "h2", "trust_tier", "unknown", "our_side")
    _edit(h2, "retracted", "ob_0001", T_AFTER)  # a row for the node, but not a trust-tier move
    assert _hits(drill(h2, tmp_path / "s2"), "column trust_tier", "no audit record")


def test_d19_the_trust_tier_metadata_key_follows_the_same_rule(home, tmp_path):
    _meta_both(home, {"trust_tier": "unknown"}, {"trust_tier": "our_side"})
    rep = drill(home, tmp_path / "s")
    assert rep.exit_code == 1
    assert "trust_tier" not in rep.metadata_unknown
    assert _hits(rep, "nodes", "ob_0001", "metadata", "trust_tier", "no audit record")
    _edit(home, "trust_tier_set", "ob_0001", T_AFTER)
    rep2 = drill(home, tmp_path / "s2")
    assert rep2.exit_code == 0, rep2.failures
    assert lc(rep2, "metadata.trust_tier")["passed"] == 1


def test_d19_source_type_changed_needs_an_audit_row_after_the_dump(tmp_path):
    h = _col_pair(tmp_path, "h", "source_type", None, "web")
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    assert _hits(rep, "nodes", "id='ob_0001'", "column source_type", "no audit record")
    _edit(h, "surgical_field_patch", "ob_0001", T_AFTER, json.dumps({"field": "source_type"}))
    rep2 = drill(h, tmp_path / "s2")
    assert rep2.exit_code == 0, rep2.failures
    assert lc(rep2, "source_type")["passed"] == 1


def test_d19_the_known_gaps_name_the_two_new_no_audit_writers_by_function():
    gaps = " ".join(rd.NO_AUDIT_WRITERS)
    assert "_run_db_one_time_setup" in gaps and "_backfill_source_type" in gaps
    assert "_backfill_resolved_by" in gaps and "_resolve_impl" in gaps


# ---------------------------------------------------------------- D20 failure text (fold 4)

def test_d20_a_failing_quoted_text_message_prints_neither_text_but_its_type_and_length(tmp_path):
    h = make_home(tmp_path, "h")
    live_exec(h, "UPDATE nodes SET quoted_text = ? WHERE id = 'ob_0001'", ("the dumped secret quote text",))
    dump(h)
    mark_dump(h)
    live_exec(h, "UPDATE nodes SET quoted_text = ? WHERE id = 'ob_0001'", ("a live quote that differs",))
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    hit = _hits(rep, "ob_0001", "column quoted_text")
    assert hit
    text = " ".join(rep.failures) + rd.format_report(rep)
    assert "secret quote" not in text and "live quote that" not in text
    assert "str" in hit[0] and str(len("the dumped secret quote text")) in hit[0] and str(len("a live quote that differs")) in hit[0]
    assert "differ in length" in hit[0]


def test_d20_metadata_failures_print_the_key_and_the_value_shape_never_the_value(home, tmp_path):
    _meta_both(home, {"birthday": "1990-04-02", "made_up_key_zz": "alpha-secret"},
               {"birthday": "1991-05-03", "made_up_key_zz": "omega-secret"})
    rep = drill(home, tmp_path / "s")
    assert rep.exit_code == 1
    text = " ".join(rep.failures)
    assert _hits(rep, "ob_0001", "metadata key birthday") and _hits(rep, "ob_0001", "metadata key made_up_key_zz")
    for leaked in ("1990", "1991", "alpha-secret", "omega-secret"):
        assert leaked not in text, leaked
    hit = _hits(rep, "metadata key birthday")[0]
    assert "str" in hit and "10" in hit and "same length" in hit


def test_d20_a_withdrawal_stamp_that_changed_does_not_print_the_reason_text(home, tmp_path):
    _meta_both(home, {"retracted": True, "retraction_reason": "the private reason"},
               {"retracted": True, "retraction_reason": "another private reason"})
    rep = drill(home, tmp_path / "s")
    assert rep.exit_code == 1
    assert "private reason" not in " ".join(rep.failures)
    assert _hits(rep, "metadata key retraction_reason")


def test_d20_known_gap_strings_name_the_function_and_carry_no_source_line_numbers(home, tmp_path):
    import re
    for gap in rd.NO_AUDIT_WRITERS:
        assert not re.search(r"\.py:\d+", gap), gap
    mark_dump(home)
    live_exec(home, "UPDATE nodes SET resolved_by='ob_0002' WHERE id='ob_0001'")
    rep = drill(home, tmp_path / "s")
    assert not re.search(r"\.py:\d+", " ".join(rep.failures) + rd.format_report(rep))


def test_d20_a_resolved_by_failure_with_no_audit_row_names_the_two_no_audit_writers(home, tmp_path):
    mark_dump(home)
    live_exec(home, "UPDATE nodes SET resolved_by='ob_0002' WHERE id='ob_0001'")
    hit = _hits(drill(home, tmp_path / "s"), "column resolved_by", "no audit record")
    assert hit and "possible cause" in hit[0]
    assert "_backfill_resolved_by" in hit[0] and "_resolve_impl" in hit[0]


def test_d20_a_status_failure_on_the_resolve_path_with_no_audit_row_names_them_too(tmp_path):
    h = make_home(tmp_path, "h")
    _dump_with(h, "UPDATE nodes SET status='resolved', resolved_by='ob_0002' WHERE id='ob_0001'")
    live_exec(h, "UPDATE nodes SET status='open', resolved_by=NULL WHERE id='ob_0001'")  # no audit row at all
    hit = _hits(drill(h, tmp_path / "s"), "column status", "no audit record")
    assert hit and "_backfill_resolved_by" in hit[0] and "_resolve_impl" in hit[0]
    # a retraction is not the resolve path: its message does not carry the hint
    h2 = make_home(tmp_path, "h2")
    mark_dump(h2)
    live_exec(h2, *retract_sql(None))
    hit2 = _hits(drill(h2, tmp_path / "s2"), "column status", "no audit record")
    assert hit2 and "_backfill_resolved_by" not in hit2[0]


# ---------------------------------------------------------------- D21 cells for mutants a colleague's run left alive

def test_d21_r17_the_capture_stamp_is_read_before_the_backup_not_after(home, monkeypatch):
    """The stamp must never post-date the data it stamps: the clock is read BEFORE the hot backup. Observed at the
    backup call itself, with a clock that ticks on every read."""
    bk = rd.engram_backup
    ticks = iter(range(1, 100))
    events = []

    def clock(epoch=None):
        events.append("clock")
        return f"2026-06-01T00:00:{next(ticks):02d}.000000Z"

    class _Conn:  # delegates everything to a real connection, and notes the one call that copies the data
        def __init__(self, real):
            self._c = real

        def backup(self, target):
            events.append("backup")
            return self._c.backup(getattr(target, "_c", target))

        def __getattr__(self, name):
            return getattr(self._c, name)

    class _Sqlite3:
        def __getattr__(self, name):
            return getattr(sqlite3, name)

        def connect(self, *a, **k):
            return _Conn(sqlite3.connect(*a, **k))

    monkeypatch.setattr(bk, "format_dump_time", clock)
    monkeypatch.setattr(bk, "sqlite3", _Sqlite3())
    stats = bk.dump_stripped(str(home / "knowledge.db"), str(home / "again.sql"))
    assert events[:2] == ["clock", "backup"], events
    assert stats["dumped_at"] == "2026-06-01T00:00:01.000000Z"  # the first reading, not a later one


def _later_clock(monkeypatch, bk):
    """Every way the module can read the clock now answers a DIFFERENT, later instant, and ticks per read."""
    import datetime as dt
    reads = []

    class _Later(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            reads.append("datetime.now")
            return dt.datetime(2099, 1, 1, 0, 0, len(reads) % 60, tzinfo=dt.timezone.utc)

    monkeypatch.setattr(bk, "datetime", _Later)
    return reads


def test_d21_r17w_the_written_marker_is_the_captured_value_not_a_fresh_clock_read(home, monkeypatch):
    """The marker LINE is the value captured/passed in: a writer that re-reads the clock when it writes (stamped
    right at capture, wrong on disk) is red. The clock is moved to a different instant before the write."""
    bk = rd.engram_backup
    given = "2026-06-01T00:00:07.000000Z"
    reads = _later_clock(monkeypatch, bk)
    out = home / "given.sql"
    stats = bk.dump_stripped(str(home / "knowledge.db"), str(out), dumped_at=given)
    assert out.read_text(encoding="utf-8").splitlines()[0] == _MARKER_LINE + " " + given
    assert stats["dumped_at"] == given
    assert out.read_text(encoding="utf-8").splitlines()[0] == _MARKER_LINE + " " + stats["dumped_at"]
    assert reads == [], "a given dumped_at must not touch the clock at all"
    # dumped_at=None: the marker is the ONE clock read taken before the backup, however often the clock ticks
    ticks = iter(range(1, 100))

    def clock(epoch=None):
        return f"2026-06-01T00:00:{next(ticks):02d}.000000Z"

    monkeypatch.setattr(bk, "format_dump_time", clock)
    out2 = home / "none.sql"
    stats2 = bk.dump_stripped(str(home / "knowledge.db"), str(out2))
    first = out2.read_text(encoding="utf-8").splitlines()[0]
    assert first == _MARKER_LINE + " " + "2026-06-01T00:00:01.000000Z"
    assert stats2["dumped_at"] == "2026-06-01T00:00:01.000000Z"
    assert first == _MARKER_LINE + " " + stats2["dumped_at"]
    assert reads == [], "the writer must not read the datetime clock either"


def test_d21_r4_a_resolved_by_move_between_values_needs_a_resolution_status_live(tmp_path):
    h = make_home(tmp_path, "h")
    _dump_with(h, "UPDATE nodes SET status='resolved', resolved_by='ob_0002' WHERE id='ob_0001'")
    live_exec(h, "UPDATE nodes SET status='active', resolved_by='ob_0003' WHERE id='ob_0001'")
    _edit(h, "resolution_recomputed", "ob_0001", T_AFTER)  # a row after the dump, but the pair is not a transition
    rep = drill(h, tmp_path / "s")
    assert rep.exit_code == 1
    assert lc(rep, "resolved_by")["failed_transition"] == 1
    assert _hits(rep, "column resolved_by", "value -> other value")


def _status_move(tmp_path, name, dump_sql, live_sql, action):
    h = make_home(tmp_path, name)
    _dump_with(h, dump_sql)
    live_exec(h, live_sql)
    _edit(h, action, "ob_0001", T_AFTER)
    return drill(h, tmp_path / (name + "-s"))


def test_d21_r6_a_reopened_row_alone_explains_a_reopen(tmp_path):
    rep = _status_move(tmp_path, "h", "UPDATE nodes SET status='resolved', resolved_by='ob_0002' WHERE id='ob_0001'",
                       "UPDATE nodes SET status='open', resolved_by=NULL WHERE id='ob_0001'", "reopened")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "status")["passed"] == 1 and lc(rep, "resolved_by")["passed"] == 1


def test_d21_r7_a_resolved_row_alone_explains_a_resolution(tmp_path):
    rep = _status_move(tmp_path, "h", "UPDATE nodes SET status='open' WHERE id='ob_0001'",
                       "UPDATE nodes SET status='resolved', resolved_by='ob_0002' WHERE id='ob_0001'", "resolved")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "status")["passed"] == 1 and lc(rep, "resolved_by")["passed"] == 1


def test_d21_r1_an_audit_row_stamped_at_exactly_the_dump_time_explains_the_change(home, tmp_path):
    mark_dump(home, T_DUMP)
    live_exec(home, *retract_sql(None))
    _edit(home, "retracted", "ob_0001", T_DUMP)  # the very instant of the marker
    rep = drill(home, tmp_path / "s")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "status")["passed"] == 1 and rep.ambiguous_count == 0


def _retracted_at_only(home, stamp):
    """status and is_current agree between the restored and live copies: only metadata.retracted_at differs."""
    mark_dump(home, T_DUMP)
    live_exec(home, "UPDATE nodes SET metadata=? WHERE id='ob_0001'", (json.dumps({"retracted_at": stamp}),))


def test_d21_r9_a_retracted_at_stamp_alone_passes_after_the_dump_and_fails_before_it(home, tmp_path):
    _retracted_at_only(home, T_AFTER)
    rep = drill(home, tmp_path / "s1")
    assert rep.exit_code == 0, rep.failures
    assert lc(rep, "metadata.retracted_at")["passed"] == 1
    h2 = make_home(tmp_path, "h2")
    _retracted_at_only(h2, T_BEFORE)
    rep2 = drill(h2, tmp_path / "s2")
    assert rep2.exit_code == 1
    assert lc(rep2, "metadata.retracted_at")["failed_before_dump"] == 1
    assert _hits(rep2, "ob_0001", "column metadata.retracted_at", "before the dump time")
