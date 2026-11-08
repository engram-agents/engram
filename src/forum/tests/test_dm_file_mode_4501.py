"""#4501 (writer half): DM thread files are written owner-only (no group/other bits).

Contract (stated first, asserted here):
- M1: every DM append leaves the thread file owner-only, INCLUDING an append into
  a thread that already had a 0600 file (a one-time chmod must not be the only
  thing keeping it private, and the rewrite must not widen it).
- M2: the first append into a new thread yields an owner-only file.
- M3: baton files are unchanged: exactly 0o644, on first and second write.
- M4: no window -- the temp file is already owner-only when os.replace runs.
- M5: a failing os.replace propagates, leaves no stray temp, and leaves the
  existing thread bytes unchanged.

DM cells use the bit test ``& 0o077 == 0`` (never ``== 0o600``): a default ACL can
change the raw mode, and the bit test does not go vacuous under one.
"""

import os
import stat

import pytest

from forum.coordination import FileStore
from forum.coordination import store_file


def _baton_md(project_id):
    return (
        "---\n"
        f"project_id: {project_id}\n"
        f"title: {project_id} title\n"
        "status: in-progress\n"
        "turn: ariadne\n"
        "turn_since: 2026-06-26T00:00:00Z\n"
        'turn_reason: "handed off"\n'
        "participants: [ariadne, sol]\n"
        "---\n"
        f"# {project_id}\n\nbody text\n"
    )


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def _thread_file(store):
    files = list(store.dm_dir.glob("*.md"))
    assert len(files) == 1
    return files[0]


def _baton_file(store, project_id):
    files = [
        p for p in store.root.rglob("*")
        if p.is_file() and p.name.startswith(project_id) and p.suffix == ".md"
    ]
    assert len(files) == 1, files
    return files[0]


def test_m1_append_into_a_thread_that_already_had_a_0600_file_stays_owner_only(tmp_path):
    """Append into a thread that already had a 0600 file: the rewrite keeps it private."""
    store = FileStore(tmp_path)
    store.append_dm("ariadne", "sol", "first", seq=1, ts="2026-06-26T00:00:00Z")
    p = _thread_file(store)
    os.chmod(p, 0o600)
    store.append_dm("sol", "ariadne", "second", seq=2, ts="2026-06-26T00:01:00Z")
    assert _mode(p) & 0o077 == 0
    msgs = store.read_dm_thread("ariadne", "sol")
    assert [m.body for m in msgs] == ["first", "second"]


def test_m2_first_append_into_new_thread_is_owner_only(tmp_path):
    store = FileStore(tmp_path)
    store.append_dm("ariadne", "sol", "hello", seq=1, ts="2026-06-26T00:00:00Z")
    assert _mode(_thread_file(store)) & 0o077 == 0
    assert [m.body for m in store.read_dm_thread("ariadne", "sol")] == ["hello"]


def test_m3_batons_stay_0644_on_first_and_second_write(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1"), seq=1)
    p = _baton_file(store, "PR-1")
    assert _mode(p) == 0o644
    store.write_project("PR-1", _baton_md("PR-1"), seq=2)
    assert _mode(p) == 0o644


def _spy_replace(monkeypatch):
    seen = []
    real = os.replace

    def spy(src, dst, *a, **kw):
        seen.append(_mode(src))
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(store_file.os, "replace", spy)
    return seen


def test_m4_temp_file_has_no_wide_window_at_replace_time(tmp_path, monkeypatch):
    store = FileStore(tmp_path)
    seen = _spy_replace(monkeypatch)
    store.append_dm("ariadne", "sol", "new thread", seq=1, ts="2026-06-26T00:00:00Z")
    store.append_dm("ariadne", "sol", "existing thread", seq=2, ts="2026-06-26T00:01:00Z")
    assert len(seen) == 2
    assert all(m & 0o077 == 0 for m in seen)
    # baton: proves the spy sees the temp (explicit 0o644 chmod before rename)
    seen.clear()
    store.write_project("PR-1", _baton_md("PR-1"), seq=3)
    assert seen == [0o644]


def test_m5_failing_replace_propagates_leaves_no_temp_and_keeps_bytes(tmp_path, monkeypatch):
    store = FileStore(tmp_path)
    store.append_dm("ariadne", "sol", "first", seq=1, ts="2026-06-26T00:00:00Z")
    p = _thread_file(store)
    before = p.read_bytes()

    def boom(src, dst, *a, **kw):
        raise OSError("replace failed")

    monkeypatch.setattr(store_file.os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        store.append_dm("sol", "ariadne", "second", seq=2, ts="2026-06-26T00:01:00Z")
    assert list(store.dm_dir.glob(".*tmp*")) == []
    assert p.read_bytes() == before
