"""Tests for tools/anchoring_report.py (engram-alpha#2602 facet 1) --
the read-only CLI report over the anchoring measurement.

Runs the tool as a real subprocess against a fixture DB built via a real
fresh_server() + engram_add_observation() (never a hand-rolled schema, to
avoid schema drift between this test and the real nodes table).
"""

import atexit
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_anchoring_report_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

_TOOL_PATH = Path(__file__).resolve().parent.parent / "src" / "engram" / "tools" / "anchoring_report.py"


def _fresh_server():
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


def _write_obs(server, *, quote_type, claim, quoted_text):
    payload = {
        "url": f"https://example.com/{quote_type}-{abs(hash(claim))}",
        "title": "Fixture source",
        "quoted_text": quoted_text,
        "interpretation": "fixture interpretation",
        "claim": claim,
        "quote_type": quote_type,
    }
    r = json.loads(server.engram_add_observation(payload_json=json.dumps(payload)))
    assert r["status"] == "created", r
    return r["observation_id"]


def _build_fixture_db() -> Path:
    server = _fresh_server()
    # Well-anchored hard_data node.
    _write_obs(
        server, quote_type="hard_data",
        claim="merged as ob_1111", quoted_text="the merge landed as ob_1111 today",
    )
    # Poorly-anchored hard_data node -- the worst-N target.
    _write_obs(
        server, quote_type="hard_data",
        claim="counted 42000 events across ob_2222", quoted_text="a short unrelated quote",
    )
    # A personal_communication node with an unanchored number (no hint,
    # but still measured).
    _write_obs(
        server, quote_type="personal_communication",
        claim="mentioned 500 things", quoted_text="a conversation about other stuff",
    )
    return Path(server.core.DATA_DIR) / "knowledge.db"


def _run_tool(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_TOOL_PATH), *extra_args],
        capture_output=True, text=True, timeout=30,
    )


def test_population_count_printed_human_format():
    db_path = _build_fixture_db()
    proc = _run_tool("--db", str(db_path))
    assert proc.returncode == 0, proc.stderr
    assert "Population" in proc.stdout
    assert "3" in proc.stdout.splitlines()[0]


def test_mode_ro_write_attempt_raises():
    db_path = _build_fixture_db()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        raised = False
        try:
            conn.execute("INSERT INTO nodes (id, type, created_at) VALUES ('x_0000', 'observation_factual', 'now')")
            conn.commit()
        except sqlite3.OperationalError:
            raised = True
        assert raised, "a write on a mode=ro connection must raise"
    finally:
        conn.close()


def test_format_json_keys_present():
    db_path = _build_fixture_db()
    proc = _run_tool("--db", str(db_path), "--format", "json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    for key in ("population", "by_quote_type", "worst", "similarity_computed", "db", "generated_at"):
        assert key in payload, f"missing {key!r}"
    assert payload["population"] == 3
    assert "hard_data" in payload["by_quote_type"]
    assert payload["similarity_computed"] is False  # --similarity not passed


def test_worst_n_surfaces_the_poorly_anchored_node_first():
    db_path = _build_fixture_db()
    proc = _run_tool("--db", str(db_path), "--format", "json", "--top", "5")
    payload = json.loads(proc.stdout)
    assert payload["worst"], "expected at least one worst-list entry"
    top = payload["worst"][0]
    assert top["quote_type"] == "hard_data"
    assert set(top["unanchored"]) & {"ob_2222", "2222", "42000"}


def test_quote_type_filter_restricts_population():
    db_path = _build_fixture_db()
    proc = _run_tool("--db", str(db_path), "--format", "json", "--quote-type", "personal_communication")
    payload = json.loads(proc.stdout)
    assert payload["population"] == 1
    assert list(payload["by_quote_type"].keys()) == ["personal_communication"]


def test_similarity_disabled_reports_false_and_null_columns():
    db_path = _build_fixture_db()
    proc = _run_tool("--db", str(db_path), "--format", "json", "--similarity")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["similarity_computed"] is False
    for stats in payload["by_quote_type"].values():
        assert stats.get("median_similarity") is None
    assert "similarity unavailable" in proc.stderr
