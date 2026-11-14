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
import shutil
import sqlite3
import subprocess
import sys
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
        ("nodes", "utility_score"): mu, ("nodes", "memory_status"): mu, ("nodes", "metadata"): mu,
        ("nodes", "status"): mu, ("nodes", "is_current"): mu, ("nodes", "superseded_by"): mu,
        ("edges", "source_id"): wo, ("edges", "target_id"): wo, ("edges", "relation"): wo,
        ("edit_history", "details"): wo, ("diagnostic_history", "metrics"): mu, ("diagnostic_history", "timestamp"): wo, ("tool_timing", "duration_ms"): wo,
        ("focus_state", "active_set_name"): mu, ("sequences", "last_num"): mu,
        ("edges", "metadata"): rd.UNCLASSIFIED, ("nodes", "trust_tier"): rd.UNCLASSIFIED,
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
    assert {"trust_tier", "standpoint_author_id", "source_type"} <= set(c[rd.UNCLASSIFIED])


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
    assert "PRAGMA user_version = 10;" in text
    sql.write_text(text.replace("PRAGMA user_version = 10;", "PRAGMA user_version = 9;"), encoding="utf-8")
    rep = drill(home, tmp_path / "scratch")
    assert rep.schema["dump_user_version"] == 9
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
