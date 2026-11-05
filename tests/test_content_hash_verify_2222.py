"""Contract tests for engram-alpha#2222 (third shape only): a caller-supplied
content_hash on a committed file:// source, or on the content-addressed archive
path, must match the sha256 of the file's bytes, or the call is refused.

Contract (written before the implementation):
  C1 committed file (same-repo and cross-repo): the server always computes the
     hash. Absent -> computed. Equal (case-insensitive, whitespace-stripped)
     -> computed, lower-case. Different -> refused, both values named.
  C2 content-addressed archive path: same three cases.
  C3 end to end through engram_add_observation (url), (evidence_id) and
     engram_add_observation_batch: a mismatch writes no observation node and no
     evidence row; the correct hash is accepted and stored as computed.
  C4 refusals that fire before hashing keep their own reason (the existing
     "0"*64 tests in test_source_class.py stay green unedited).
  C5 non-file:// URLs and files outside any git repo are not touched here.
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

TEST_DIR = tempfile.mkdtemp(prefix="engram_test_content_hash_2222_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))

QUOTE = "Quote inside the committed file."
BODY = (QUOTE + "\n").encode()
COMPUTED = hashlib.sha256(BODY).hexdigest()
WRONG = "0" * 64


def fresh_client():
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    from engram_client import EngramClient
    return EngramClient(db_dir=TEST_DIR)


def _eo():
    import engram_observation
    return engram_observation


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(path):
    os.makedirs(path)
    _git(path, "init")
    _git(path, "config", "user.email", "t@t.t")
    _git(path, "config", "user.name", "t")


@pytest.fixture
def repo():
    base = os.path.realpath(tempfile.mkdtemp(prefix="engram_test_2222_repo_"))
    r = os.path.join(base, "repo")
    _init_repo(r)
    with open(os.path.join(r, "f.md"), "wb") as fh:
        fh.write(BODY)
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "add f")
    yield r
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def cross(repo):
    """source_repo = repo (committed f.md); the cited file lives outside it."""
    base = os.path.realpath(tempfile.mkdtemp(prefix="engram_test_2222_cross_"))
    cited = os.path.join(base, "cited.md")
    with open(cited, "wb") as fh:
        fh.write(BODY)
    yield {"url": f"file://{cited}", "source_repo": repo, "repo_path": "f.md"}
    shutil.rmtree(base, ignore_errors=True)


def _assert_mismatch_refusal(r, supplied):
    assert r["ok"] is False, r
    assert r["content_hash"] == ""
    err = r["error"]
    assert supplied in err, err
    assert COMPUTED in err, err
    assert "does not match" in err, err
    assert "Remember: cheating blinds you" in err, err  # _reject path


# ---- C1: committed file, same repo -------------------------------------------

def test_C1a_same_repo_no_hash_supplied_computes(repo):
    fresh_client()
    r = _eo()._capture_file_version(f"file://{repo}/f.md")
    assert r["ok"] is True, r
    assert r["content_hash"] == COMPUTED


@pytest.mark.parametrize("supplied", [COMPUTED, COMPUTED.upper(), f"  {COMPUTED.upper()}\n"])
def test_C1b_same_repo_equal_hash_accepted_and_lowercased(repo, supplied):
    fresh_client()
    r = _eo()._capture_file_version(f"file://{repo}/f.md", content_hash=supplied)
    assert r["ok"] is True, r
    assert r["content_hash"] == COMPUTED


def test_C1c_same_repo_different_hash_refused_naming_both(repo):
    fresh_client()
    r = _eo()._capture_file_version(f"file://{repo}/f.md", content_hash=WRONG)
    _assert_mismatch_refusal(r, WRONG)


# ---- C1: committed file, cross-repo ------------------------------------------

def test_C1a_cross_repo_no_hash_supplied_computes(cross):
    fresh_client()
    r = _eo()._capture_file_version(cross["url"], source_repo=cross["source_repo"],
                                    repo_path=cross["repo_path"])
    assert r["ok"] is True, r
    assert r["content_hash"] == COMPUTED


@pytest.mark.parametrize("supplied", [COMPUTED, COMPUTED.upper(), f" {COMPUTED.upper()} "])
def test_C1b_cross_repo_equal_hash_accepted_and_lowercased(cross, supplied):
    fresh_client()
    r = _eo()._capture_file_version(cross["url"], content_hash=supplied,
                                    source_repo=cross["source_repo"],
                                    repo_path=cross["repo_path"])
    assert r["ok"] is True, r
    assert r["content_hash"] == COMPUTED


def test_C1c_cross_repo_different_hash_refused_naming_both(cross):
    fresh_client()
    r = _eo()._capture_file_version(cross["url"], content_hash=WRONG,
                                    source_repo=cross["source_repo"],
                                    repo_path=cross["repo_path"])
    _assert_mismatch_refusal(r, WRONG)


# ---- C2: content-addressed archive path ---------------------------------------

@pytest.fixture
def archive_file(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    _git(str(root), "init")
    _git(str(root), "config", "user.email", "t@t.t")
    _git(str(root), "config", "user.name", "t")
    (root / ".keep").write_text("seed\n")
    _git(str(root), "add", ".keep")
    _git(str(root), "commit", "-q", "-m", "seed")
    bucket = root / COMPUTED[:2]
    bucket.mkdir()
    fp = bucket / f"{COMPUTED}.txt"
    fp.write_bytes(BODY)
    fresh_client()
    eo = _eo()
    old = eo.EVIDENCE_ARCHIVE_ROOT
    eo.EVIDENCE_ARCHIVE_ROOT = str(root)
    yield f"file://{fp}"
    eo.EVIDENCE_ARCHIVE_ROOT = old


def test_C2_archive_no_hash_supplied_returns_archive_hash(archive_file):
    r = _eo()._capture_file_version(archive_file)
    assert r["ok"] is True, r
    assert r["content_hash"] == COMPUTED


@pytest.mark.parametrize("supplied", [COMPUTED, COMPUTED.upper(), f" {COMPUTED.upper()} "])
def test_C2_archive_equal_hash_accepted_and_lowercased(archive_file, supplied):
    r = _eo()._capture_file_version(archive_file, content_hash=supplied)
    assert r["ok"] is True, r
    assert r["content_hash"] == COMPUTED


def test_C2_archive_different_hash_refused_naming_both(archive_file):
    r = _eo()._capture_file_version(archive_file, content_hash=WRONG)
    _assert_mismatch_refusal(r, WRONG)


# ---- C3: end to end, nothing written on refusal --------------------------------

def _count(sql):
    import engram_core as core
    return core._get_db().execute(sql).fetchone()[0]


def _count_nodes():
    return _count("SELECT COUNT(*) FROM nodes")


def _count_evidence():
    return _count("SELECT COUNT(*) FROM nodes WHERE type = 'evidence'")


def _payload(repo, **extra):
    p = {
        "url": f"file://{repo}/f.md",
        "title": "Committed file",
        "quoted_text": QUOTE,
        "interpretation": "Testing content_hash verification.",
        "claim": "The committed file says what it says.",
        "quote_type": "hard_data",
    }
    p.update(extra)
    return p


def _assert_refused_nothing_written(r, nodes_before, ev_before):
    assert "error" in r, r
    assert r.get("status") != "created" and "observation_id" not in r, r
    assert WRONG in r["error"] and COMPUTED in r["error"], r["error"]
    assert _count_nodes() == nodes_before, "a node row was written on refusal"
    assert _count_evidence() == ev_before, "an evidence row was written on refusal"


def _stored_hash(client, obs_id):
    node = client.call("engram_inspect", {"node_id": obs_id, "view": "deep"})["node"]
    meta = node.get("metadata", "{}")
    if isinstance(meta, str):
        meta = json.loads(meta)
    return meta.get("content_hash")


def test_C3i_url_path_mismatch_refused_nothing_written(repo):
    client = fresh_client()
    n, e = _count_nodes(), _count_evidence()
    r = client.call("engram_add_observation", _payload(repo, content_hash=WRONG))
    _assert_refused_nothing_written(r, n, e)


def test_C3i_url_path_correct_hash_stored_as_computed(repo):
    client = fresh_client()
    r = client.call("engram_add_observation", _payload(repo, content_hash=COMPUTED.upper()))
    assert r.get("status") == "created", r
    assert _stored_hash(client, r["observation_id"]) == COMPUTED


def _by_evidence_id(ev_id, **extra):
    p = {
        "evidence_id": ev_id, "quoted_text": QUOTE,
        "interpretation": "Second citation.",
        "claim": "A second claim from the same file.",
        "quote_type": "hard_data",
    }
    p.update(extra)
    return p


def test_C3ii_evidence_id_path_mismatch_refused_nothing_written(repo):
    client = fresh_client()
    first = client.call("engram_add_observation", _payload(repo))
    assert first.get("status") == "created", first
    n, e = _count_nodes(), _count_evidence()
    r = client.call("engram_add_observation",
                    _by_evidence_id(first["evidence_id"], content_hash=WRONG))
    _assert_refused_nothing_written(r, n, e)


def test_C3ii_evidence_id_path_correct_hash_stored_as_computed(repo):
    client = fresh_client()
    first = client.call("engram_add_observation", _payload(repo))
    r = client.call("engram_add_observation",
                    _by_evidence_id(first["evidence_id"], content_hash=COMPUTED))
    assert r.get("status") == "created", r
    assert _stored_hash(client, r["observation_id"]) == COMPUTED


def _batch(client, repo, **extra):
    p = {
        "observations_json": json.dumps([{
            "quoted_text": QUOTE, "interpretation": "Batch citation.",
            "claim": "A batch claim from the committed file.", "quote_type": "hard_data"}]),
        "url": f"file://{repo}/f.md", "title": "Committed file",
    }
    p.update(extra)
    return client.call("engram_add_observation_batch", p)


def test_C3iii_batch_mismatch_refused_nothing_written(repo):
    client = fresh_client()
    n, e = _count_nodes(), _count_evidence()
    r = _batch(client, repo, content_hash=WRONG)
    assert r.get("created", 0) == 0, r
    blob = json.dumps(r)
    assert WRONG in blob and COMPUTED in blob, r
    assert _count_nodes() == n, "a node row was written on refusal"
    assert _count_evidence() == e, "an evidence row was written on refusal"


def test_C3iii_batch_correct_hash_created_and_stored_as_computed(repo):
    client = fresh_client()
    r = _batch(client, repo, content_hash=COMPUTED.upper())
    assert r.get("created") == 1, r
    obs_id = r["results"][0]["observation_id"]
    assert _stored_hash(client, obs_id) == COMPUTED


# ---- C4: refusal order, git checks fire before the hash comparison -----------

def test_C4_nonexistent_git_sha_refused_before_hash_mismatch(repo):
    fresh_client()
    r = _eo()._capture_file_version(f"file://{repo}/f.md", content_hash=WRONG,
                                    git_sha="a" * 40)
    assert r["ok"] is False, r
    assert "does not exist at git revision" in r["error"], r["error"]
    assert "does not match" not in r["error"], r["error"]


def test_C4_cross_repo_directory_repo_path_refused_before_hash_mismatch(cross):
    fresh_client()
    src = cross["source_repo"]
    os.makedirs(os.path.join(src, "sub"))
    with open(os.path.join(src, "sub", "g.md"), "wb") as fh:
        fh.write(BODY)
    _git(src, "add", "-A")
    _git(src, "commit", "-m", "add sub/g.md")
    r = _eo()._capture_file_version(cross["url"], content_hash=WRONG,
                                    source_repo=src, repo_path="sub")
    assert r["ok"] is False, r
    assert "must name a file (a blob)" in r["error"], r["error"]
    assert "does not match" not in r["error"], r["error"]
