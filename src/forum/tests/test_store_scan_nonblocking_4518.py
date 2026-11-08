"""#4518 part 1: the store's SCAN reads never block on a planted FIFO.

Contract (stated first, asserted here):

- C1: a FIFO named ``*.md`` in ``projects/`` makes ``read_projects`` return, and
  the FIFO is excluded from the board.
- C2: a FIFO in ``archive/`` makes ``recover_max_seq`` return; the FIFO cannot
  affect the high-water mark, and a regular archived file with a seq ABOVE the
  projects seq is still read and reaches the mark (the in-cell control that
  rules out a skip-everything archive loop; verified by mutation, see the
  commit).
- C3: a FIFO in ``dm/`` makes ``recover_max_seq`` return; a healthy thread's seq
  is still recovered.
- C4 (control): a REGULAR file with a real ``seq:`` beside the FIFO in
  ``projects/`` is still read and contributes its seq. Without this control,
  C1-C3 pass against an implementation that skips EVERY entry.
- C5 (control): a symlink in ``projects/`` is still skipped -- the #4517
  behaviour -- so this fix does not regress it.
- C6: ``read_dm_thread`` pointed at a FIFO still raises ``InvalidDmPath`` -- the
  DM contract is unchanged.
- C7 (control): ``_read_text_nofollow``'s two DM messages are distinct and
  pinned: a symlink says "not a plain file", a FIFO says "not a regular file".
  This is a CONTROL, not red-first -- it passes on the base commit too, and
  exists because contract 1's byte-for-byte message preservation was otherwise
  asserted by nothing.
- C8: ``read_project`` (a single-project read, not a scan) also never blocks on
  a FIFO: it returns ``None`` under the budget, while a regular file at another
  id still returns its content in the same cell (the control that rules out an
  implementation returning ``None`` for everything).
- C9: a non-UTF-8 ``*.md`` is INCLUDED lossily, NOT excluded -- its entry is
  present and its ASCII frontmatter ``seq`` is recovered. Dropping it from a
  high-water mark would reissue sequence numbers over a live baton (see B1),
  so the display/integrity policy replaces the byte rather than skipping the
  entry. A regular file beside it is still read (same in-cell control rule).
- B1: a baton with ``seq: 9`` and a 0xff byte in its BODY still yields
  ``recover_max_seq() == 9`` (the integrity half: under-counting reissues) and
  one row from ``read_projects()`` (the vanishing half).
- F2: ``build_updates`` skips one corrupt DM thread instead of raising, with a
  healthy thread beside it still present (in-cell control); the explicit
  ``read_dm_thread`` stays strict and loud -- the asymmetry is deliberate.

A ``read_text()`` on a FIFO blocks inside ``open()`` until a writer appears.
Every cell that touches a FIFO therefore runs under a SIGALRM budget: a blocked
read would otherwise HANG the suite, and a hanging test is worse than a failing
one -- it tells you nothing and costs the whole run. The budget turns a block
into a named failure instead.
"""

import contextlib
import os
import signal

import pytest

from forum.coordination import FileStore
from forum.coordination import store_file
from forum.coordination.names import InvalidDmPath

_FIFO_BUDGET_S = 5


class _Blocked(Exception):
    """Raised by the SIGALRM budget: the read blocked instead of returning."""


@contextlib.contextmanager
def _no_block_budget(seconds=_FIFO_BUDGET_S):
    """Fail (never hang) if the guarded body blocks on a FIFO open."""
    if not hasattr(signal, "SIGALRM"):
        pytest.skip("no SIGALRM on this platform; cannot bound a blocking read")

    def _fire(signum, frame):
        raise _Blocked(f"read blocked for more than {seconds}s (a FIFO open blocked)")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _mkfifo(path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no os.mkfifo on this platform")
    os.mkfifo(path)


def _baton_md(project_id, *, seq=None):
    lines = [
        "---",
        f"project_id: {project_id}",
        f"title: {project_id} title",
        "status: in-progress",
        "turn: ariadne",
        "turn_since: 2026-06-26T00:00:00Z",
        "participants: [ariadne, sol]",
    ]
    if seq is not None:
        lines.append(f"seq: {seq}")
    lines += ["---", f"# {project_id}", "", "body text", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# C1: the projects board scan returns with a FIFO present and skips it
# ---------------------------------------------------------------------------
def test_c1_read_projects_skips_a_fifo_in_projects(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    projects = tmp_path / "projects"
    _mkfifo(projects / "thread.md")

    with _no_block_budget():
        recs = store.read_projects(active_only=False)

    assert [r.project_id for r in recs] == ["PR-1"]


# ---------------------------------------------------------------------------
# C2: cursor recovery returns with a FIFO in archive/; the mark is unaffected
# ---------------------------------------------------------------------------
def test_c2_recover_max_seq_skips_a_fifo_in_archive(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=5), seq=5)
    archive = tmp_path / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    # In-cell control, mirroring C3: a REGULAR archived file with a seq ABOVE
    # the projects seq must reach the mark. Without it this cell cannot tell a
    # working archive loop from a skip-everything one -- measured: mutating the
    # archive loop body to `continue` drops this cell's `hi` from 12 to 5.
    (archive / "OLD-1.md").write_text(_baton_md("OLD-1", seq=12), encoding="utf-8")
    _mkfifo(archive / "thread.md")

    with _no_block_budget():
        hi = store.recover_max_seq()

    assert hi == 12


# ---------------------------------------------------------------------------
# C3: cursor recovery returns with a FIFO in dm/; a healthy thread still counts
# ---------------------------------------------------------------------------
def test_c3_recover_max_seq_skips_a_fifo_in_dm(tmp_path):
    store = FileStore(tmp_path)
    store.append_dm("ariadne", "sol", "hello", seq=9, ts="2026-06-26T00:00:00Z")
    dm_dir = tmp_path / "dm"
    _mkfifo(dm_dir / "evil+pair.md")

    with _no_block_budget():
        hi = store.recover_max_seq()

    assert hi == 9


# ---------------------------------------------------------------------------
# C4 (control): a regular file beside the FIFO is still read and contributes
# ---------------------------------------------------------------------------
def test_c4_regular_file_beside_a_fifo_is_still_read(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=7), seq=7)
    projects = tmp_path / "projects"
    _mkfifo(projects / "thread.md")

    with _no_block_budget():
        recs = store.read_projects(active_only=False)
        hi = store.recover_max_seq()

    # Not "everything was skipped": the real file's content and seq both land.
    assert [r.project_id for r in recs] == ["PR-1"]
    assert recs[0].seq == 7
    assert hi == 7


# ---------------------------------------------------------------------------
# C5 (control): a symlink in projects/ is STILL skipped (#4517 not regressed)
# ---------------------------------------------------------------------------
def test_c5_symlink_in_projects_is_still_skipped(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    projects = tmp_path / "projects"
    outside = tmp_path / "outside-baton.md"
    outside.write_text(_baton_md("EVIL", seq=999), encoding="utf-8")
    os.symlink(outside, projects / "evil.md")

    recs = store.read_projects(active_only=False)
    assert [r.project_id for r in recs] == ["PR-1"]


# ---------------------------------------------------------------------------
# C6: the DM read contract is unchanged -- a FIFO raises InvalidDmPath, promptly
# ---------------------------------------------------------------------------
def test_c6_read_dm_thread_on_a_fifo_still_raises(tmp_path):
    store = FileStore(tmp_path)
    dm_dir = tmp_path / "dm"
    dm_dir.mkdir(parents=True, exist_ok=True)
    _mkfifo(dm_dir / "ariadne+sol.md")

    with _no_block_budget():
        with pytest.raises(InvalidDmPath):
            store.read_dm_thread("ariadne", "sol")


# ---------------------------------------------------------------------------
# C7 (control): the two DM refusal messages are distinct and pinned
# ---------------------------------------------------------------------------
def test_c7_nofollow_refusal_messages_are_pinned(tmp_path):
    # Contract 1: the two messages are preserved byte-for-byte. Nothing asserted
    # them before, so a refactor could have collapsed them silently. This is a
    # CONTROL -- it passes on the base commit -- not a red-first cell.
    link = tmp_path / "link.md"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, link)
    with pytest.raises(InvalidDmPath, match="dm thread path is not a plain file"):
        store_file._read_text_nofollow(link)

    # A FIFO takes the other message; bounded, since a regression to a blocking
    # open at this boundary would hang rather than fail.
    fifo = tmp_path / "thread.md"
    _mkfifo(fifo)
    with _no_block_budget():
        with pytest.raises(InvalidDmPath, match="dm thread path is not a regular file"):
            store_file._read_text_nofollow(fifo)


# ---------------------------------------------------------------------------
# C8: a single-project read (read_project) never blocks on a FIFO either
# ---------------------------------------------------------------------------
def test_c8_read_project_skips_a_fifo_without_blocking(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    projects = tmp_path / "projects"
    _mkfifo(projects / "FIFO-9.md")

    with _no_block_budget():
        fifo_result = store.read_project("FIFO-9")
        regular_result = store.read_project("PR-1")

    assert fifo_result is None
    # Control in the SAME cell: a regular file at another id still returns its
    # content, so this cannot pass against "return None for everything".
    assert regular_result is not None
    assert "project_id: PR-1" in regular_result


# ---------------------------------------------------------------------------
# C9: a non-UTF-8 *.md is INCLUDED lossily, not excluded (see the policy note)
# ---------------------------------------------------------------------------
def test_c9_invalid_utf8_entry_is_included_lossily_and_seq_recovered(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=3), seq=3)
    projects = tmp_path / "projects"
    # A real baton with a bad byte in the BODY: the ASCII frontmatter (and its
    # seq) is intact. Under the display/integrity policy the bad byte is
    # REPLACED, not treated as a reason to drop the entry -- dropping it would
    # hide the seq from the high-water mark and let a fresh allocator reissue
    # sequence numbers over a live baton (B1).
    (projects / "garbage.md").write_bytes(
        _baton_md("GARBAGE", seq=4).encode("utf-8") + b"\xff"
    )

    # read_projects: the undecodable entry is PRESENT (lossily), not excluded.
    recs = store.read_projects(active_only=False)
    assert {r.project_id for r in recs} == {"PR-1", "GARBAGE"}
    # recover_max_seq: its ASCII seq is recovered, so the mark is above the
    # healthy file's 3 -- this is the integrity half.
    assert store.recover_max_seq() == 4
    # read_project: that entry renders lossily rather than returning None.
    assert store.read_project("garbage") is not None
    # Control in the SAME cell: a regular file at another id still returns text.
    assert store.read_project("PR-1") is not None
    assert "project_id: PR-1" in store.read_project("PR-1")


# ---------------------------------------------------------------------------
# B1: a corrupt BODY byte must not hide the seq or make the baton vanish
# ---------------------------------------------------------------------------
def test_b1_corrupt_body_does_not_hide_seq_or_vanish_baton(tmp_path):
    store = FileStore(tmp_path)
    projects = tmp_path / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    # seq 9 in ASCII frontmatter; one 0xff byte appended to the body.
    (projects / "PR-1.md").write_bytes(
        _baton_md("PR-1", seq=9).encode("utf-8") + b"\xff"
    )

    # The integrity half: under-counting here would reissue sequence 1 over a
    # live baton (a fresh allocator recovering 0 starts there).
    assert store.recover_max_seq() == 9
    # The vanishing half: the board must still list the baton, not drop it.
    recs = store.read_projects()
    assert len(recs) == 1
    assert recs[0].project_id == "PR-1"


# ---------------------------------------------------------------------------
# F2: the wake feed skips a corrupt DM thread; the explicit read stays loud
# ---------------------------------------------------------------------------
def test_f2_build_updates_skips_a_corrupt_dm_thread(tmp_path):
    from forum.coordination import SeqAllocator
    from forum.coordination.updates import build_updates

    store = FileStore(tmp_path)
    # A healthy thread addressed TO the agent (recipient is the relevance key).
    store.append_dm("sol", "ariadne", "healthy", seq=5, ts="2026-06-26T00:00:00Z")
    dm_dir = tmp_path / "dm"
    # A corrupt thread the agent is also party to; its body has a bad byte.
    (dm_dir / "ariadne+borges.md").write_bytes(
        b"<!-- dm thread: ariadne+borges -->\n"
        b"--- seq=9 from=ariadne to=borges at=2026-06-26T00:00:00Z lines=1 ---\n"
        b"\xff corrupt body\n"
    )

    # Must NOT raise: the feed degrades by skipping the corrupt thread.
    out = build_updates(
        store, SeqAllocator(recover=lambda: 100), "ariadne",
        since=0, kinds=("dm",), now_fn=lambda: "2026-06-26T20:00:00Z",
    )

    bodies = [u["body"] for u in out["updates"] if u["kind"] == "dm"]
    # In-cell control: the healthy thread beside it is still served.
    assert "healthy" in bodies
    assert len(bodies) == 1
    # And the explicit read remains strict and loud, by design.
    with pytest.raises(UnicodeDecodeError):
        store.read_dm_thread("ariadne", "borges")



