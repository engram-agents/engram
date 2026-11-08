"""#4518 part 2: a REFUSED store entry is VISIBLE, not silently absent.

Part 1 (`c0a65aed`) made the board scan refuse a bad entry instead of hanging or
raising. That bought availability but created a silence: a refused baton simply
did not appear, so a reader could not tell "no such work item" from "one was
refused". Luria's phrasing is the contract: a vanished baton must read as "one
entry refused", not as nothing.

The additive API is ``scan_projects(*, active_only=True) -> ProjectScan``, whose
``refused`` counts entries the scan DECLINED to read. ``read_projects`` stays a
thin ``.records`` view of it, behaviour unchanged.

Cells:
- C1: a FIFO in ``projects/`` -> ``refused == 1``; healthy records still returned.
- C2: a symlink in ``projects/`` -> ``refused == 1``.
- C3 (control): a clean store -> ``refused == 0`` and every record present.
  Without it, an implementation that always returns 1 would pass C1 and C2.
- C4: a closed-status baton under ``active_only=True`` -> filtered from
  ``records`` but ``refused == 0`` (filtering is not refusal).
- C5 (control, the cell that matters most): ``read_projects()`` equals a
  hand-built fixture -- the wrapper regression guard. It passes on the base
  commit too, which is what makes it a control rather than new-API coverage.
- C6: two planted entries -> ``refused == 2``, so the count moves PER entry and
  is not a boolean dressed as an integer.
- C7 (route): ``GET /api/board/projects`` -> 200, includes ``refused == 1`` with
  a FIFO planted and the healthy baton still in ``board``; the store is scanned
  EXACTLY ONCE, so the count and the list are from the same scan.
- C8 (route, control): a clean store -> ``refused == 0`` and the baton in
  ``board``.
- C9 (HTML board): ``GET /board`` renders the refused chip (one line) when an
  entry was refused, AND scans exactly once -- the human surface is pinned the
  same way the JSON one is.
- C10 (all-refused): a store whose ONLY entry is refused -> JSON
  ``refused == 1`` with ``board == []``, and the HTML chip still present. This
  is the case where a broken template guard would show "0 items" in silence.
- C11 (HTML control): a clean store renders NO refused chip (pins the guard's
  other direction).
- C12: a mode-000 REGULAR file is an unreadable entry -> ``refused == 1`` (the
  "unreadable" half of the contract; ``islink or not isfile`` would pass it).
- C13: ``FileStore.scan_projects is not CoordinationStore.scan_projects`` -- a
  refactor cannot silently inherit the ABC's ``refused=0``.
- C14: an entry that VANISHES mid-scan (a routine archive rename race) is
  skipped WITHOUT counting -- a vanished baton is not a refusal.
- C15: a scan failure reports ``refused: null`` + ``scan_ok: false`` (UNKNOWN),
  never a clean store's ``0``.

RED-FIRST HONESTY: C1-C4 and C6-C15 exercise NEW API (``scan_projects`` / the
``refused`` field / the route key / the sentinel) that does not exist on the
base commit, so a classic red-first demonstration is not available for them --
they could not even run. They are additive-API coverage, demonstrated to
discriminate by the count moving per planted entry (C1/C2 = 1, C6 = 2) and
reading 0 on the clean control (C3, C8, C11). C5 IS meaningful on base and
passes there and after; it is reported as a control.

A ``read_text()`` on a FIFO blocks inside ``open()`` until a writer appears. The
store's scan opens ``O_NONBLOCK`` (part 1), so it does not block -- but every
cell touching a FIFO still runs under a SIGALRM budget, because a regression
would HANG the suite rather than fail it.
"""

import contextlib
import json
import os
import re
import signal
import sqlite3

import pytest

from forum.coordination import FileStore, ProjectRecord
from forum.db import init_db
from forum.server import create_app

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


def _baton_md(
    project_id,
    *,
    title=None,
    status="in-progress",
    turn="ariadne",
    turn_since="2026-06-26T00:00:00Z",
    turn_reason="handed off",
    participants="ariadne, sol",
    seq=None,
    github="",
    turn_by="",
):
    lines = [
        "---",
        f"project_id: {project_id}",
        f"title: {title or project_id + ' title'}",
        f"status: {status}",
        f"turn: {turn}",
        f"turn_since: {turn_since}",
        f"turn_reason: {turn_reason}",
        f"participants: [{participants}]",
    ]
    if seq is not None:
        lines.append(f"seq: {seq}")
    if github:
        lines.append(f"github: {github}")
    if turn_by:
        lines.append(f"turn_by: {turn_by}")
    lines += ["---", f"# {project_id}", "", "body text", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# C1: a FIFO is counted as one refusal; healthy records still come back
# ---------------------------------------------------------------------------
def test_c1_fifo_is_counted_as_one_refusal(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    projects = tmp_path / "projects"
    _mkfifo(projects / "thread.md")

    with _no_block_budget():
        scan = store.scan_projects(active_only=False)

    assert scan.refused == 1
    # In-cell control: the healthy baton beside the FIFO is still served, so
    # this cannot pass against a scan that refuses everything.
    assert [r.project_id for r in scan.records] == ["PR-1"]


# ---------------------------------------------------------------------------
# C2: a symlink is counted as one refusal
# ---------------------------------------------------------------------------
def test_c2_symlink_is_counted_as_one_refusal(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    projects = tmp_path / "projects"
    outside = tmp_path / "outside.md"
    outside.write_text(_baton_md("EVIL", seq=999), encoding="utf-8")
    os.symlink(outside, projects / "evil.md")

    scan = store.scan_projects(active_only=False)

    assert scan.refused == 1
    # In-cell control: the healthy baton is still read; the link's target never
    # contributes a record (the #4517 non-disclosure behaviour).
    assert [r.project_id for r in scan.records] == ["PR-1"]


# ---------------------------------------------------------------------------
# C3 (control): a clean store reports ZERO refusals and every record
# ---------------------------------------------------------------------------
def test_c3_clean_store_reports_zero_refusals(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    store.write_project("PR-2", _baton_md("PR-2", seq=2), seq=2)

    scan = store.scan_projects(active_only=False)

    # The control that rules out "always returns 1": C1 and C2 are meaningless
    # without this cell.
    assert scan.refused == 0
    assert {r.project_id for r in scan.records} == {"PR-1", "PR-2"}


# ---------------------------------------------------------------------------
# C4: active_only filtering is NOT a refusal
# ---------------------------------------------------------------------------
def test_c4_closed_status_filter_is_not_a_refusal(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    store.write_project(
        "PR-DONE", _baton_md("PR-DONE", status="merged", seq=2), seq=2
    )

    active = store.scan_projects(active_only=True)
    everything = store.scan_projects(active_only=False)

    # Filtered out of the active list, but NOT counted as a refusal: normal
    # filtering and a declined entry are different things, and conflating them
    # would make the number meaningless.
    assert [r.project_id for r in active.records] == ["PR-1"]
    assert active.refused == 0
    assert {r.project_id for r in everything.records} == {"PR-1", "PR-DONE"}
    assert everything.refused == 0


# ---------------------------------------------------------------------------
# C5 (control): read_projects equality against a hand-built fixture
# ---------------------------------------------------------------------------
def test_c5_read_projects_equality_against_hand_built_fixture(tmp_path):
    # Write the batons straight to disk so `raw` is byte-for-byte the fixture
    # text (write_project would re-embed the seq line).
    projects = tmp_path / "projects"
    projects.mkdir(parents=True)
    text_closed = _baton_md(
        "PR-3", status="merged", turn_since="2026-06-25T00:00:00Z", seq=1
    )
    text_a = _baton_md(
        "PR-1",
        seq=2,
        github="project/7",
        # Quoted on disk (the module writes them quoted); expected values are
        # the UNQUOTED parse, so this exercises the unquote path for both.
        turn_reason='"handed off"',
        turn_by='"ariadne"',
    )
    text_b = _baton_md(
        "PR-2", title="Second baton", turn_since="2026-06-27T00:00:00Z", seq=3
    )
    (projects / "PR-3.md").write_text(text_closed, encoding="utf-8")
    (projects / "PR-1.md").write_text(text_a, encoding="utf-8")
    (projects / "PR-2.md").write_text(text_b, encoding="utf-8")

    store = FileStore(tmp_path)

    # Ordering is by turn_since ascending: PR-3 (06-25), PR-1 (06-26), PR-2.
    expected_closed = ProjectRecord(
        project_id="PR-3",
        title="PR-3 title",
        status="merged",
        turn="ariadne",
        turn_since="2026-06-25T00:00:00Z",
        turn_reason="handed off",
        participants=("ariadne", "sol"),
        seq=1,
        raw=text_closed,
    )
    expected_a = ProjectRecord(
        project_id="PR-1",
        title="PR-1 title",
        status="in-progress",
        turn="ariadne",
        turn_since="2026-06-26T00:00:00Z",
        turn_reason="handed off",
        participants=("ariadne", "sol"),
        seq=2,
        raw=text_a,
        github="project/7",
        turn_by="ariadne",
    )
    expected_b = ProjectRecord(
        project_id="PR-2",
        title="Second baton",
        status="in-progress",
        turn="ariadne",
        turn_since="2026-06-27T00:00:00Z",
        turn_reason="handed off",
        participants=("ariadne", "sol"),
        seq=3,
        raw=text_b,
    )

    # Full equality, including `raw`, order and every parsed field (including
    # the quoted turn_reason/turn_by unquote path): the wrapper must return
    # exactly what the pre-#4518 read_projects returned.
    assert store.read_projects(active_only=False) == [
        expected_closed,
        expected_a,
        expected_b,
    ]
    # And the default (active_only=True) filtering path: same records minus the
    # closed one, same order.
    assert store.read_projects() == [expected_a, expected_b]


# ---------------------------------------------------------------------------
# C6: the count moves PER planted entry (two refusals, not a boolean)
# ---------------------------------------------------------------------------
def test_c6_count_moves_per_planted_entry(tmp_path):
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    projects = tmp_path / "projects"
    _mkfifo(projects / "fifo.md")
    outside = tmp_path / "outside.md"
    outside.write_text(_baton_md("EVIL", seq=999), encoding="utf-8")
    os.symlink(outside, projects / "link.md")

    with _no_block_budget():
        scan = store.scan_projects(active_only=False)

    # Two declined entries -> 2. A "there was at least one bad entry" boolean
    # would read 1 here, so this is the cell that pins the count as a count.
    assert scan.refused == 2
    assert [r.project_id for r in scan.records] == ["PR-1"]


# ---------------------------------------------------------------------------
# Route cells: the user-visible surface carries the count
# ---------------------------------------------------------------------------
def _board_client(tmp_path, store):
    """A test client for ``GET /api/board/projects`` wired to ``store``.

    Mirrors test_project_board.py's ``app`` fixture. The suite's autouse auth
    fixture wraps ``Flask.test_client``, so the client carries the #1459 token.
    """
    db_path = str(tmp_path / "forum-route.db")
    sqlite3.connect(db_path).close()
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(tmp_path / "audit-route.jsonl"), enable_board_viewer=True)
    app.config["TESTING"] = True
    app.config["COORD_STORE"] = store
    return app.test_client()


def _counting_scan(store, monkeypatch):
    """Wrap ``store.scan_projects`` to record calls; returns the call log.

    The board list and its refusal count must come from ONE scan, so this pins
    the count to the list it annotates on BOTH surfaces.
    """
    calls = []
    real_scan = store.scan_projects

    def counting_scan(*, active_only=True):
        calls.append(active_only)
        return real_scan(active_only=active_only)

    monkeypatch.setattr(store, "scan_projects", counting_scan)
    return calls


def test_c7_board_route_reports_one_refusal_from_the_same_scan(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "coord")
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    _mkfifo(store.projects_dir / "thread.md")

    calls = _counting_scan(store, monkeypatch)
    client = _board_client(tmp_path, store)
    with _no_block_budget():
        resp = client.get("/api/board/projects")

    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "refused" in data
    assert data["refused"] == 1
    assert data["scan_ok"] is True
    # In-cell control: the healthy baton is still served beside the refused one.
    assert [item["project"] for item in data["board"]] == ["PR-1"]
    # The "same scan" assertion: exactly one scan served both keys.
    assert calls == [False]


def test_c8_board_route_reports_zero_refusals_on_a_clean_store(tmp_path):
    store = FileStore(tmp_path / "coord")
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)

    client = _board_client(tmp_path, store)
    resp = client.get("/api/board/projects")

    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "refused" in data
    assert data["refused"] == 0            # a clean store is a REAL zero
    assert data["scan_ok"] is True
    assert [item["project"] for item in data["board"]] == ["PR-1"]


def test_c9_html_board_renders_the_refused_line(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "coord")
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    _mkfifo(store.projects_dir / "thread.md")

    calls = _counting_scan(store, monkeypatch)
    client = _board_client(tmp_path, store)
    with _no_block_budget():
        resp = client.get("/board")

    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The human surface carries the same accounting: one refused entry renders
    # as a visible count chip, not silence.
    assert re.search(
        r'<b>1</b>\s*<span class="pboard__count-label">refused</span>', body
    )
    # In-cell control: the healthy baton is still on the rendered board.
    assert "PR-1" in body
    # F3: the human surface is pinned to ONE scan too -- dropping `records=`
    # from the /board route would make this read [False, False].
    assert calls == [False]


def test_c10_all_refused_store_reports_and_renders_the_refusal(tmp_path):
    # The highest-stakes case: NOTHING renders except a refused entry. If the
    # template guard goes wrong, the page shows "0 items" with exactly the
    # silence this PR exists to end.
    store = FileStore(tmp_path / "coord")
    store.projects_dir.mkdir(parents=True, exist_ok=True)
    _mkfifo(store.projects_dir / "thread.md")

    client = _board_client(tmp_path, store)
    with _no_block_budget():
        data = json.loads(client.get("/api/board/projects").data)
        body = client.get("/board").get_data(as_text=True)

    assert data["refused"] == 1
    assert data["board"] == []
    assert re.search(
        r'<b>1</b>\s*<span class="pboard__count-label">refused</span>', body
    )


def test_c11_clean_store_html_has_no_refused_chip(tmp_path):
    # Pins the OTHER direction of the template guard: a clean store must not
    # claim a refusal.
    store = FileStore(tmp_path / "coord")
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)

    client = _board_client(tmp_path, store)
    body = client.get("/board").get_data(as_text=True)

    assert "PR-1" in body
    assert 'pboard__count-label">refused' not in body


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root (or a non-POSIX platform) can read a mode-000 file",
)
def test_c12_mode_000_regular_file_is_counted_as_refused(tmp_path):
    # The "unreadable entry" half of the contract. A plausible-wrong counting
    # implementation (`islink or not isfile`) drops this with refused == 0.
    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    secret = store.projects_dir / "secret.md"
    secret.write_text(_baton_md("SECRET", seq=2), encoding="utf-8")
    os.chmod(secret, 0o000)
    try:
        scan = store.scan_projects(active_only=False)
    finally:
        os.chmod(secret, 0o644)

    assert scan.refused == 1
    assert [r.project_id for r in scan.records] == ["PR-1"]


def test_c13_filestore_overrides_the_abc_scan_default():
    # The ABC's default returns refused=0. FileStore must NOT inherit it -- a
    # refactor that deletes the override would silently reintroduce the silence.
    from forum.coordination import CoordinationStore

    assert FileStore.scan_projects is not CoordinationStore.scan_projects


def test_c14_a_vanished_entry_is_not_counted_as_refused(tmp_path, monkeypatch):
    # archive_project RENAMES a baton out of projects/, so a board poll racing a
    # routine archive must read as "nothing there", not "one refused".
    from forum.coordination import store_file

    store = FileStore(tmp_path)
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    doomed = store.projects_dir / "PR-DOOMED.md"
    doomed.write_text(_baton_md("PR-DOOMED", seq=2), encoding="utf-8")

    real_scan_text = store_file._scan_text

    def racing_scan_text(path):
        if path.name == "PR-DOOMED.md" and path.exists():
            path.unlink()  # a routine archive rename wins the race
        return real_scan_text(path)

    monkeypatch.setattr(store_file, "_scan_text", racing_scan_text)

    scan = store.scan_projects(active_only=False)

    # ENOENT maps to the sentinel, and the scan skips it WITHOUT counting.
    assert store_file._scan_text(doomed) is store_file._VANISHED
    assert scan.refused == 0
    assert [r.project_id for r in scan.records] == ["PR-1"]


def test_c15_scan_failure_reports_unknown_not_zero(tmp_path, monkeypatch):
    # F1: a failed scan is UNKNOWN. It must not be byte-identical to a clean
    # store's refused=0.
    store = FileStore(tmp_path / "coord")
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)

    def boom(*, active_only=True):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(store, "scan_projects", boom)

    client = _board_client(tmp_path, store)
    resp = client.get("/api/board/projects")

    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["refused"] is None          # UNKNOWN, not a clean-store 0
    assert data["scan_ok"] is False
    # The pre-#4518 fail-soft is preserved: a broken store renders 200, not 500.
    assert data["board"] == []
