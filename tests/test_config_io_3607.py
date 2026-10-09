"""Contract-first tests for #3607: config.json writes are atomic, readers
retry and then fail loud, and node creation never stamps from an unreadable
config.

Written BEFORE the implementation.  Every test touches only a temporary
ENGRAM home / graph (``_fresh_server`` points the suite's server at a
``mkdtemp`` directory) -- never the real ``~/.engram``.

Contract
  W1  write_config_json over an existing file preserves stat.S_IMODE exactly.
  W2  ...and preserves the ACL mask (a mkstemp temp is 0600 and would zero it).
  W3  a concurrent reader never sees a partial file (zero parse errors).
  R1  read_config_json on an empty / truncated file raises ConfigUnreadableError
      naming the path after exactly ``attempts`` reads.
  R2  a file torn on the first read and valid on the second returns the dict.
  R3  an absent file reads as {}.
  N1  node creation on a torn config raises the named error and writes no row.
  N2  non-memory readers keep their pre-change final-failure behaviour.
"""

import importlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_server(tmp_dir):
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.path[0] != repo_root:
        sys.path.insert(0, repo_root)
    import server
    server._configure_paths(tmp_dir)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    os.environ["ENGRAM_NO_POLARITY"] = "1"
    return server


@pytest.fixture
def cio():
    # Other tests purge engram_* from sys.modules; import at use time so
    # monkeypatches hit the module the code under test actually uses.
    return importlib.import_module("engram_config_io")


@pytest.fixture
def no_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


# ---------------------------------------------------------------- W1 / W2 ---

@pytest.mark.parametrize("mode", [0o640, 0o670])
def test_w1_write_preserves_mode(tmp_path, cio, mode):
    p = tmp_path / "config.json"
    p.write_text("{}")
    os.chmod(p, mode)
    cio.write_config_json(p, {"a": 1, "b": [1, 2]})
    assert stat.S_IMODE(os.stat(p).st_mode) == mode
    assert json.loads(p.read_text()) == {"a": 1, "b": [1, 2]}
    assert p.read_text().endswith("\n")
    assert list(tmp_path.iterdir()) == [p], "temp file left behind"


def _group_entry(path):
    out = subprocess.run(["getfacl", "-c", "-e", str(path)], capture_output=True,
                         text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("group::"):
            return line
    return None


def test_w2_acl_mask_preserved(tmp_path, cio):
    if not (shutil.which("setfacl") and shutil.which("getfacl")):
        pytest.skip("setfacl/getfacl not available")
    r = subprocess.run(
        ["setfacl", "-d", "-m", "group::rw-,mask::rw-", str(tmp_path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot set a default ACL here: {r.stderr.strip()}")
    p = tmp_path / "config.json"
    cio.write_config_json(p, {"v": 1})
    os.chmod(p, 0o660)
    before = _group_entry(p)
    assert before is not None and "#effective:---" not in before, before
    cio.write_config_json(p, {"v": 2})
    after = _group_entry(p)
    assert after is not None
    assert "#effective:---" not in after, (before, after)
    assert after.split()[0].endswith("rw-"), (before, after)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o660


# --------------------------------------------------------------------- W3 ---

def _hammer(path, writer, n):
    """Run `writer(i)` n times while a reader thread json.loads the file in a
    tight loop; return the number of parse errors the reader saw."""
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            try:
                json.loads(path.read_text())
            except json.JSONDecodeError:
                errors.append(1)
            except FileNotFoundError:
                pass

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(n):
            writer(i)
    finally:
        stop.set()
        t.join()
    return len(errors)


def _payload(i):
    return {"memory": {"current_turn": i}, "pad": ["x" * 50] * (200 + i)}


def test_w3_atomic_writer_never_exposes_partial_file(tmp_path, cio):
    p = tmp_path / "config.json"
    cio.write_config_json(p, _payload(0))
    assert _hammer(p, lambda i: cio.write_config_json(p, _payload(i)), 500) == 0


def test_w3_harness_detects_a_nonatomic_writer(tmp_path):
    """Control for the harness itself: the OLD write_text writer is caught."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps(_payload(0)))
    errs = _hammer(p, lambda i: p.write_text(json.dumps(_payload(i), indent=2)), 500)
    assert errs > 0, "harness failed to observe the truncate-then-write window"


def test_w3_set_current_turn_never_exposes_partial_file(tmp_path):
    _fresh_server(str(tmp_path))
    import engram_core as core
    core.CONFIG_PATH.write_text(json.dumps(
        {"memory": {"current_turn": 0}, "pad": ["y" * 50] * 4000}))
    assert _hammer(core.CONFIG_PATH, lambda i: core._set_current_turn(i), 500) == 0
    assert json.loads(core.CONFIG_PATH.read_text())["memory"]["current_turn"] == 499


# --------------------------------------------------------------- R1/R2/R3 ---

@pytest.mark.parametrize("content", ["", '{"memory": {"current_tu'])
def test_r1_unreadable_raises_named_error_after_attempts(tmp_path, cio, monkeypatch, no_sleep, content):
    p = tmp_path / "config.json"
    p.write_text(content)
    reads = []
    real = Path.read_text

    def counting(self, *a, **k):
        if self == p:
            reads.append(1)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", counting)
    with pytest.raises(cio.ConfigUnreadableError) as ei:
        cio.read_config_json(p, attempts=3, delay=0.05)
    assert len(reads) == 3
    assert isinstance(ei.value, ValueError)
    assert str(p) in str(ei.value)
    assert "char" in str(ei.value)  # the parse position
    assert isinstance(ei.value.__cause__, json.JSONDecodeError)


def test_r2_torn_then_valid_returns_valid(tmp_path, cio, monkeypatch, no_sleep):
    p = tmp_path / "config.json"
    p.write_text('{"a": ')
    calls = []

    def flaky(self, *a, **k):
        calls.append(1)
        return '{"a": ' if len(calls) == 1 else '{"a": 7}'

    monkeypatch.setattr(Path, "read_text", flaky)
    assert cio.read_config_json(p) == {"a": 7}
    assert len(calls) == 2


def test_r3_absent_is_empty_dict(tmp_path, cio):
    assert cio.read_config_json(tmp_path / "nope.json") == {}


# --------------------------------------------------------------------- N1 ---

def _node_count(server):
    conn = server._get_db()
    try:
        return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        conn.close()


def test_n1_torn_config_blocks_node_creation_with_no_row(tmp_path, no_sleep):
    server = _fresh_server(str(tmp_path))
    import engram_core as core
    cio_now = importlib.import_module("engram_config_io")
    core.CONFIG_PATH.write_text(json.dumps({"memory": {"current_turn": 5}}))
    payload = json.dumps({
        "url": "https://example.com/3607", "title": "t",
        "quoted_text": "a claim about config", "interpretation": "i",
        "claim": "a claim about config (3607)", "quote_type": "attributed_analysis",
    })
    before = _node_count(server)
    core.CONFIG_PATH.write_text('{"memory": {"current_tu')
    out, raised = None, None
    try:
        out = server.engram_add_observation(payload_json=payload)
    except Exception as e:  # noqa: BLE001
        raised = e
    if raised is not None:
        assert isinstance(raised, cio_now.ConfigUnreadableError), repr(raised)
        assert str(core.CONFIG_PATH) in str(raised)
    else:
        assert '"created"' not in out and str(core.CONFIG_PATH) in out, out
    assert _node_count(server) == before, "a node row was written despite the unreadable config"


# --------------------------------------------------------------------- N2 ---

def _legacy_confidence(core):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE nodes (id TEXT, confidence REAL)")
    conn.execute("INSERT INTO nodes VALUES ('n1', 0.8)")
    return core._compute_confidence(conn, "derivation", supporting_ids=["n1"])


@pytest.mark.parametrize("reader", ["embedding", "thresholds", "confidence_legacy", "self_lineage"])
def test_n2_non_memory_readers_keep_final_failure_behaviour(tmp_path, no_sleep, reader):
    _fresh_server(str(tmp_path))
    import engram_core as core
    core.CONFIG_PATH.write_text('{"memory": ')
    if reader == "embedding":
        with pytest.raises(ValueError):          # was: JSONDecodeError escaped
            core._get_embedding_config()
    elif reader == "thresholds":                 # was: swallowed -> defaults
        got = core._get_thresholds_config()
        assert got["dedup_min_similarity"] == core.DEDUP_MIN_SIMILARITY
    elif reader == "confidence_legacy":
        with pytest.raises(ValueError):          # was: JSONDecodeError escaped
            _legacy_confidence(core)
    elif reader == "self_lineage":               # was: swallowed -> ''
        assert core._self_lineage() == ""


# -------------------------------------------------------------------- N1b ---
# Every node-creating impl INSERTs the row and then calls _stamp_new_node (which
# reads the turn) with no commit in between, so the error propagates out of an
# uncommitted transaction and nothing is persisted.  _add_evidence_impl and
# _ask_impl read the config before their INSERT.  Pin it per path.

_N1B = [
    ("engram_epistemic", "_add_conjecture_impl", {"claim": "c 3607", "basis": "b"}),
    ("engram_epistemic", "_add_definition_impl", {"term": "t3607", "definition": "d"}),
    ("engram_epistemic", "_add_axiom_impl", {"claim": "a 3607", "basis": "b"}),
    ("engram_derive", "_ask_impl", {"question": "q 3607?"}),
    ("engram_tasks", "_add_goal_impl", {"claim": "g 3607", "motivation": "m"}),
    ("engram_trust", "_add_person_impl", {"name": "P3607"}),
]


@pytest.mark.parametrize("mod,fn,kwargs", _N1B)
def test_n1b_node_creating_impls_write_no_row_on_torn_config(tmp_path, no_sleep, mod, fn, kwargs):
    server = _fresh_server(str(tmp_path))
    import engram_core as core
    impl_mod = importlib.import_module(mod)
    cio_now = importlib.import_module("engram_config_io")
    before = _node_count(server)
    core.CONFIG_PATH.write_text('{"memory": {"current_tu')
    with pytest.raises(cio_now.ConfigUnreadableError):
        getattr(impl_mod, fn)(**kwargs)
    assert _node_count(server) == before


# ------------------------------------------- fold round: new target / link ---

def test_w4_new_target_mode_matches_write_text(tmp_path, cio):
    sib = tmp_path / "sibling.json"
    sib.write_text("{}")
    expected = stat.S_IMODE(os.stat(sib).st_mode)
    p = tmp_path / "config.json"
    cio.write_config_json(p, {"v": 1})
    assert stat.S_IMODE(os.stat(p).st_mode) == expected
    assert sorted(x.name for x in tmp_path.iterdir()) == ["config.json", "sibling.json"]


def test_w4b_new_target_under_default_acl_matches_write_text(tmp_path, cio):
    if not (shutil.which("setfacl") and shutil.which("getfacl")):
        pytest.skip("setfacl/getfacl not available")
    r = subprocess.run(
        ["setfacl", "-d", "-m", "group::rw-,mask::rw-", str(tmp_path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"cannot set a default ACL here: {r.stderr.strip()}")
    sib = tmp_path / "sibling.json"
    sib.write_text("{}")
    p = tmp_path / "config.json"
    cio.write_config_json(p, {"v": 1})
    want, got = _group_entry(sib), _group_entry(p)
    assert want is not None and got is not None
    assert "#effective:---" not in got, (want, got)
    assert got.split()[0] == want.split()[0], (want, got)
    assert got.split("#effective:")[-1] == want.split("#effective:")[-1], (want, got)


def test_w7_existing_file_group_is_preserved(tmp_path, cio):
    others = [g for g in os.getgroups() if g != os.getegid()]
    if not others:
        pytest.skip("process has no supplementary group besides its egid")
    gid = others[0]
    p = tmp_path / "config.json"
    p.write_text("{}")
    os.chown(p, -1, gid)
    cio.write_config_json(p, {"v": 2})
    assert os.stat(p).st_gid == gid
    assert json.loads(p.read_text()) == {"v": 2}


def test_w5_symlinked_config_is_written_through(tmp_path, cio):
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    real = real_dir / "real.json"
    real.write_text("{}")
    os.chmod(real, 0o640)
    home = tmp_path / "home"
    home.mkdir()
    link = home / "config.json"
    link.symlink_to(real)
    cio.write_config_json(link, {"v": 9})
    assert link.is_symlink()
    assert json.loads(real.read_text()) == {"v": 9}
    assert stat.S_IMODE(os.stat(real).st_mode) == 0o640
    assert [x.name for x in home.iterdir()] == ["config.json"]
    assert [x.name for x in real_dir.iterdir()] == ["real.json"]


@pytest.mark.parametrize("failing", ["replace", "fsync"])
def test_w6_error_cleans_temp_and_leaves_target(tmp_path, cio, monkeypatch, failing):
    p = tmp_path / "config.json"
    p.write_text('{"old": true}')

    def boom(*a, **k):
        raise OSError("injected")

    monkeypatch.setattr(os, failing, boom)
    with pytest.raises(OSError, match="injected"):
        cio.write_config_json(p, {"new": True})
    monkeypatch.undo()
    assert json.loads(p.read_text()) == {"old": True}
    assert [x.name for x in tmp_path.iterdir()] == ["config.json"]
