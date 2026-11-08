"""#4514: the file store refuses a symlink at every path it reads content from.

Contract (stated first, asserted here):
- C1: ``InvalidDmPath`` is a ``ValueError``, NOT an ``OSError`` -- the DM call
  sites' ``except OSError:`` handlers mean "no thread yet", so an
  OSError-derived refusal would be swallowed and the store would fail OPEN.
- C2: the normal DM round trip is unchanged (append then read; an unwritten pair
  still reads as ``[]``).
- C3: a planted link to an outside file makes both ``read_dm_thread`` and
  ``append_dm`` raise ``InvalidDmPath`` -- and nothing is disclosed or written:
  the target's bytes are unchanged, the link survives as evidence, and no temp
  sibling appears.
- C4: a dangling link is refused the same way.
- C5: a link pointing INSIDE ``dm/`` is refused too (same rule as the baton path).
- C6: ``read_projects`` skips a planted ``projects/*.md`` link (no record, and
  its target's content never reaches a returned ``raw``).
- C7: ``recover_max_seq`` does not raise on a planted link and its target's
  ``seq:`` does not inflate the high-water mark.
- C8: ``POST /api/dm/<counterpart>`` answers a named JSON 500 when the thread
  path is not a plain file, and writes nothing.
- C9: ``recover_max_seq`` skips planted links in its ``dm/`` and ``archive/``
  loops too (same rule as ``projects/``): no raise, and neither target's
  ``seq`` inflates the high-water mark.
- C10: ``GET /api/dm/<counterpart>`` answers the same named JSON 500, and no
  byte of the link's target reaches the response.
- C11: a ``projects/`` directory that is itself a symlink to an outside directory
  is refused by ``read_projects`` (``InvalidProjectId``) -- a scan root that is
  not the store's is not a per-file anomaly to skip.
- C12: the same for the ``projects/``, ``archive/`` and ``dm/`` roots of
  ``recover_max_seq`` (``InvalidProjectId`` / ``InvalidProjectId`` /
  ``InvalidDmPath``).
- C13: ``/api/updates`` survives one poisoned DM thread: 200, the healthy
  message present, the poisoned one absent and its target's bytes nowhere in
  the response.
- C14: a FIFO at the thread path raises ``InvalidDmPath`` and the call returns
  promptly -- bounded in a subprocess so a regression to a blocking open fails
  the cell instead of hanging the suite.

C3-C7 are about NON-DISCLOSURE, which file mode does not buy: ``read_text``
follows a link before anything else happens, so the copied bytes would then be
served legitimately through the API.
"""

import os
import sqlite3

import pytest

from forum.coordination import FileStore, SeqAllocator
from forum.coordination import store_file
from forum.coordination.names import InvalidDmPath, InvalidProjectId


def _dm_dir(store):
    """The store's resolved ``dm`` directory, computed independently of the impl."""
    return store.root.resolve() / "dm"


def _thread_link(store, a="ariadne", b="sol"):
    return _dm_dir(store) / f"{'+'.join(sorted((a, b)))}.md"


def _baton_md(project_id, *, seq=None, title=None):
    lines = [
        "---",
        f"project_id: {project_id}",
        f"title: {title or project_id + ' title'}",
        "status: in-progress",
        "turn: ariadne",
        "turn_since: 2026-06-26T00:00:00Z",
        'turn_reason: "handed off"',
        "participants: [ariadne, sol]",
    ]
    if seq is not None:
        lines.append(f"seq: {seq}")
    lines += ["---", f"# {project_id}", "", "body text", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# C1: the exception type is deliberately NOT an OSError
# ---------------------------------------------------------------------------
def test_c1_invalid_dm_path_is_not_an_oserror():
    assert issubclass(InvalidDmPath, ValueError)
    assert issubclass(InvalidDmPath, OSError) is False


# ---------------------------------------------------------------------------
# C2: the normal path still works
# ---------------------------------------------------------------------------
def test_c2_normal_dm_round_trip_is_unchanged(tmp_path):
    store = FileStore(tmp_path)
    store.append_dm("ariadne", "sol", "hello", seq=1, ts="2026-06-26T00:00:00Z")
    assert [m.body for m in store.read_dm_thread("ariadne", "sol")] == ["hello"]
    assert store.read_dm_thread("ariadne", "borges") == []


# ---------------------------------------------------------------------------
# C3: planted link to an outside file -- neither disclosed nor written through
# ---------------------------------------------------------------------------
def test_c3_planted_link_to_outside_file_is_refused_without_disclosure(tmp_path):
    store = FileStore(tmp_path)
    dm_dir = _dm_dir(store)
    dm_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-secret.txt"
    secret = "OUTSIDE-SECRET-#4514"
    outside.write_text(secret, encoding="utf-8")
    os.chmod(outside, 0o600)
    link = _thread_link(store)
    os.symlink(outside, link)

    with pytest.raises(InvalidDmPath):
        store.read_dm_thread("ariadne", "sol")  # NOT []
    with pytest.raises(InvalidDmPath):
        store.append_dm("ariadne", "sol", "hi", seq=1, ts="t")

    assert outside.read_text(encoding="utf-8") == secret
    assert os.path.islink(link)
    assert list(dm_dir.glob(".*tmp*")) == []


# ---------------------------------------------------------------------------
# C4: dangling link
# ---------------------------------------------------------------------------
def test_c4_dangling_link_is_refused(tmp_path):
    store = FileStore(tmp_path)
    dm_dir = _dm_dir(store)
    dm_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / "does-not-exist.md", _thread_link(store))
    with pytest.raises(InvalidDmPath):
        store.read_dm_thread("ariadne", "sol")
    with pytest.raises(InvalidDmPath):
        store.append_dm("ariadne", "sol", "hi", seq=1, ts="t")


# ---------------------------------------------------------------------------
# C5: a link that points INSIDE dm/ is refused too
# ---------------------------------------------------------------------------
def test_c5_link_pointing_inside_dm_is_refused_too(tmp_path):
    store = FileStore(tmp_path)
    dm_dir = _dm_dir(store)
    dm_dir.mkdir(parents=True, exist_ok=True)
    real = dm_dir / "other-thread.md"
    real.write_text("not a thread", encoding="utf-8")
    os.symlink(real, _thread_link(store))
    with pytest.raises(InvalidDmPath):
        store.read_dm_thread("ariadne", "sol")
    with pytest.raises(InvalidDmPath):
        store.append_dm("ariadne", "sol", "hi", seq=1, ts="t")


# ---------------------------------------------------------------------------
# C6: the projects board scan skips a planted link
# ---------------------------------------------------------------------------
def test_c6_read_projects_skips_a_planted_link(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1"), seq=1)
    projects = store.root.resolve() / "projects"
    outside = tmp_path / "outside-baton.md"
    outside.write_text(
        _baton_md("EVIL", seq=2, title="OUTSIDE-SECRET-#4514"), encoding="utf-8"
    )
    os.symlink(outside, projects / "evil.md")

    recs = store.read_projects(active_only=False)
    assert [r.project_id for r in recs] == ["PR-1"]
    assert all("OUTSIDE-SECRET-#4514" not in r.raw for r in recs)


# ---------------------------------------------------------------------------
# C7: cursor recovery skips a planted link (no raise, no inflated seq)
# ---------------------------------------------------------------------------
def test_c7_recover_max_seq_skips_a_planted_link(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1"), seq=5)
    projects = store.root.resolve() / "projects"
    outside = tmp_path / "outside-baton.md"
    outside.write_text(_baton_md("EVIL", seq=999999), encoding="utf-8")
    os.symlink(outside, projects / "evil.md")

    assert store.recover_max_seq() == 5


# ---------------------------------------------------------------------------
# C8: the send route surfaces the refusal as named JSON, not an opaque HTML 500
# ---------------------------------------------------------------------------
def test_c8_dm_send_route_names_the_plain_file_refusal(tmp_path):
    from forum.db import init_db
    from forum.server import create_app

    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()

    store = FileStore(tmp_path)
    dm_dir = _dm_dir(store)
    dm_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-secret.txt"
    secret = "OUTSIDE-SECRET-#4514"
    outside.write_text(secret, encoding="utf-8")
    link = _thread_link(store)
    os.symlink(outside, link)

    app = create_app(db_path, audit_path)
    app.config["TESTING"] = True
    app.config["COORD_STORE"] = store
    app.config["COORD_ALLOCATOR"] = SeqAllocator()
    client = app.test_client()

    resp = client.post("/api/dm/ariadne", json={"agent": "sol", "body": "hi"})
    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload is not None
    assert payload["error"] == "dm thread path is not a plain file"
    assert outside.read_text(encoding="utf-8") == secret
    assert os.path.islink(link)
    assert list(dm_dir.glob(".*tmp*")) == []


# ---------------------------------------------------------------------------
# C9: cursor recovery skips planted links in dm/ and archive/ too
# ---------------------------------------------------------------------------
def test_c9_recover_max_seq_skips_links_in_dm_and_archive(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1"), seq=5)
    archive = store.root.resolve() / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    dm_dir = _dm_dir(store)
    dm_dir.mkdir(parents=True, exist_ok=True)

    # A baton-shaped target: its frontmatter seq: must not reach the high-water mark.
    outside_baton = tmp_path / "outside-baton.md"
    outside_baton.write_text(_baton_md("EVIL-ARCHIVE", seq=999998), encoding="utf-8")
    os.symlink(outside_baton, archive / "evil.md")

    # A DM-shaped target: its header seq= must not either.
    outside_dm = tmp_path / "outside-dm.md"
    outside_dm.write_text(
        "<!-- dm thread: evil+pair -->\n"
        "--- seq=999999 from=ariadne to=sol at=2026-06-26T00:00:00Z lines=1 ---\n"
        "body\n",
        encoding="utf-8",
    )
    os.symlink(outside_dm, dm_dir / "evil+pair.md")

    assert store.recover_max_seq() == 5


# ---------------------------------------------------------------------------
# C10: the read route surfaces the refusal the same way, and discloses nothing
# ---------------------------------------------------------------------------
def test_c10_dm_read_route_names_the_plain_file_refusal(tmp_path):
    from forum.db import init_db
    from forum.server import create_app

    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()

    store = FileStore(tmp_path)
    dm_dir = _dm_dir(store)
    dm_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-thread.md"
    secret = "OUTSIDE-SECRET-#4514"
    outside.write_text(
        "<!-- dm thread: ariadne+sol -->\n"
        "--- seq=7 from=ariadne to=sol at=2026-06-26T00:00:00Z lines=1 ---\n"
        f"{secret}\n",
        encoding="utf-8",
    )
    link = _thread_link(store)
    os.symlink(outside, link)

    app = create_app(db_path, audit_path)
    app.config["TESTING"] = True
    app.config["COORD_STORE"] = store
    app.config["COORD_ALLOCATOR"] = SeqAllocator()
    client = app.test_client()

    resp = client.get("/api/dm/ariadne?agent=sol")
    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload is not None
    assert payload["error"] == "dm thread path is not a plain file"
    assert set(payload) == {"error"}  # no messages key, so nothing was disclosed
    assert secret not in resp.get_data(as_text=True)
    assert outside.read_text(encoding="utf-8").count(secret) == 1
    assert os.path.islink(link)


# ---------------------------------------------------------------------------
# C11: a scan ROOT that is itself a symlink is refused, not scanned
# ---------------------------------------------------------------------------
def test_c11_symlinked_projects_scan_root_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside-projects"
    outside.mkdir()
    secret = "OUTSIDE-SECRET-#4514"
    (outside / "leak.md").write_text(
        _baton_md("EVIL", title=secret), encoding="utf-8"
    )
    os.symlink(outside, root / "projects")

    store = FileStore(root)
    with pytest.raises(InvalidProjectId) as excinfo:
        store.read_projects(active_only=False)
    assert secret not in str(excinfo.value)


# ---------------------------------------------------------------------------
# C12: the same for every recover_max_seq scan root
# ---------------------------------------------------------------------------
def test_c12_symlinked_recover_scan_roots_are_refused(tmp_path):
    secret = "OUTSIDE-SECRET-#4514"

    # projects/ -> outside directory
    root_p = tmp_path / "projects-root"
    root_p.mkdir()
    outside_p = tmp_path / "outside-projects-recover"
    outside_p.mkdir()
    (outside_p / "leak.md").write_text(
        _baton_md("EVIL", seq=999997, title=secret), encoding="utf-8"
    )
    os.symlink(outside_p, root_p / "projects")
    with pytest.raises(InvalidProjectId) as excinfo_p:
        FileStore(root_p).recover_max_seq()
    assert secret not in str(excinfo_p.value)

    # archive/ -> outside directory
    root_a = tmp_path / "archive-root"
    root_a.mkdir()
    outside_a = tmp_path / "outside-archive"
    outside_a.mkdir()
    (outside_a / "leak.md").write_text(
        _baton_md("EVIL", seq=999998, title=secret), encoding="utf-8"
    )
    os.symlink(outside_a, root_a / "archive")
    with pytest.raises(InvalidProjectId) as excinfo_a:
        FileStore(root_a).recover_max_seq()
    assert secret not in str(excinfo_a.value)

    # dm/ -> outside directory
    root_d = tmp_path / "dm-root"
    root_d.mkdir()
    outside_d = tmp_path / "outside-dm"
    outside_d.mkdir()
    (outside_d / "evil+pair.md").write_text(
        "<!-- dm thread: evil+pair -->\n"
        "--- seq=999999 from=ariadne to=sol at=2026-06-26T00:00:00Z lines=1 ---\n"
        f"{secret}\n",
        encoding="utf-8",
    )
    os.symlink(outside_d, root_d / "dm")
    with pytest.raises(InvalidDmPath) as excinfo_d:
        FileStore(root_d).recover_max_seq()
    assert secret not in str(excinfo_d.value)


# ---------------------------------------------------------------------------
# C13: one poisoned DM thread must not take down the whole wake feed
# ---------------------------------------------------------------------------
def test_c13_updates_feed_survives_a_poisoned_dm_thread(tmp_path):
    from forum.db import init_db
    from forum.server import create_app

    db_path = str(tmp_path / "forum.db")
    audit_path = str(tmp_path / "audit.jsonl")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()

    store = FileStore(tmp_path)
    store.append_dm("ariadne", "sol", "healthy message", seq=10, ts="2026-06-26T00:00:00Z")
    dm_dir = _dm_dir(store)
    outside = tmp_path / "outside-thread.md"
    secret = "OUTSIDE-SECRET-#4514"
    outside.write_text(
        "<!-- dm thread: borges+sol -->\n"
        "--- seq=11 from=borges to=sol at=2026-06-26T00:01:00Z lines=1 ---\n"
        f"{secret}\n",
        encoding="utf-8",
    )
    os.symlink(outside, dm_dir / "borges+sol.md")

    app = create_app(db_path, audit_path)
    app.config["TESTING"] = True
    app.config["COORD_STORE"] = store
    app.config["COORD_ALLOCATOR"] = SeqAllocator(recover=lambda: 20)
    client = app.test_client()

    resp = client.get("/api/updates?agent=sol&since=0")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert secret not in text
    bodies = [u["body"] for u in resp.get_json()["updates"] if u["kind"] == "dm"]
    assert "healthy message" in bodies
    assert not any(secret in b for b in bodies)


# ---------------------------------------------------------------------------
# C14: a FIFO at the thread path is refused promptly, never blocks
# ---------------------------------------------------------------------------
def test_c14_fifo_at_thread_path_is_refused_promptly(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    if not hasattr(os, "mkfifo"):
        pytest.skip("no os.mkfifo on this platform")

    store = FileStore(tmp_path)
    dm_dir = _dm_dir(store)
    dm_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(_thread_link(store))

    src = Path(__file__).resolve().parents[2]  # .../src
    code = (
        "from pathlib import Path\n"
        "from forum.coordination import FileStore\n"
        "from forum.coordination.names import InvalidDmPath\n"
        f"store = FileStore(Path({str(tmp_path)!r}))\n"
        "try:\n"
        "    store.read_dm_thread('ariadne', 'sol')\n"
        "except InvalidDmPath:\n"
        "    print('refused')\n"
        "else:\n"
        "    raise SystemExit('did not refuse')\n"
    )
    env = dict(os.environ, PYTHONPATH=str(src))
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("reading a FIFO at the thread path BLOCKED (no O_NONBLOCK)")
    assert r.returncode == 0, r.stderr
    assert "refused" in r.stdout


# ---------------------------------------------------------------------------
# Discrimination controls for the two structural layers.
#
# Through the public API the layers mask each other: `_dm_path` refuses before
# `_read_text_nofollow` is ever reached, so reverting either one alone leaves a
# public-API cell green. These two cells drive each layer directly, so a mutant
# that reverts one layer reddens exactly one of them and leaves the other green.
# ---------------------------------------------------------------------------
def test_dm_path_refuses_a_planted_link_directly(tmp_path):
    # The link points INSIDE dm/ on purpose: the containment check
    # (resolve().parent != dm_dir) passes for it, so only `islink` can refuse.
    store = FileStore(tmp_path)
    dm_dir = _dm_dir(store)
    dm_dir.mkdir(parents=True, exist_ok=True)
    real = dm_dir / "other-thread.md"
    real.write_text("not a thread", encoding="utf-8")
    os.symlink(real, _thread_link(store))

    with pytest.raises(InvalidDmPath):
        store._dm_path("ariadne", "sol")


def test_nofollow_helper_refuses_a_link_and_reads_a_plain_file(tmp_path):
    link = tmp_path / "link.md"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, link)
    with pytest.raises(InvalidDmPath):
        store_file._read_text_nofollow(link)

    plain = tmp_path / "plain.md"
    plain.write_text("plain bytes", encoding="utf-8")
    assert store_file._read_text_nofollow(plain) == "plain bytes"
