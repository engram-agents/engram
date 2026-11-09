"""#4527: the remaining store readers report a REFUSED entry instead of hiding it.

#4526 (``54a7eef9``) made ``FileStore.scan_projects(*, active_only=...) ->
ProjectScan`` the ONE scan that both lists project records and counts the entries
it declined to read, and taught the board JSON surface (``GET
/api/board/projects``) to carry ``refused`` + ``scan_ok``. Three other surfaces
still call ``read_projects()`` and therefore cannot tell a clean store from one
with a refused entry. This file pins the contract on those three:

- S1 ``GET /api/projects`` — gains ``refused``/``scan_ok``, honours its existing
  ``active_only`` query param, and the ``?agent=`` filter narrows the LIST only:
  a refused entry has no participants, so the COUNT is never narrowed by agent.
- S2 ``GET /api/board/updates`` — same pair on the envelope; the list is the
  records from the SAME scan (``read_project_board(store, records=records)``).
- S3 ``build_updates`` — same pair on the dict it returns; its baton half reads
  records from the same scan.

The pair has two ENVELOPE states: a successful scan is ``refused: <int >= 0>,
scan_ok: true``; ``refused: null, scan_ok: false`` means NO project scan backed
the envelope — for S3 the only such case is a ``kinds`` narrowing that excluded
the baton half.

A FAILED scan is not an envelope state on S1 and S3 (fairy round 1, PR #4533):

- S1 ``GET /api/projects`` returns HTTP 500. Its consumer shows "coordination
  state unavailable" only on an HTTP error, so a 200 with an empty list would
  render as "you have no batons".
- S3 ``build_updates`` raises. Its consumer (``tools/forum-updates-monitor.sh``)
  advances its cursor on any 200 carrying a usable ``as_of``, so a degraded
  envelope would move the cursor past baton events that were never read; a
  non-200 leaves the cursor where it was.
- S2 ``GET /api/board/updates`` keeps the fail-soft it had BEFORE this branch
  (200 with ``null``/``false``). That exposure predates this work and is not
  widened here.

S4 (``_refuse_existing_id``'s documented fail-open) has no count; its behaviour
is pinned by C8: ``init`` still succeeds beside a refused entry.

Cells:
- C1: clean store -> ``refused == 0, scan_ok == True`` on all four surfaces
  (S1, S2, S3, BOARD) and each list is served unchanged.
- C2: one planted symlink -> ``refused == 1`` on all four and each list is
  unchanged (the refused entry's target never contributes a record).
- C3: S2 keeps its pre-existing fail-soft: a raising scan is a 200 with UNKNOWN
  ``null``/``false`` and an empty item list (``FileStore.read_projects`` IS the
  scan, so there is nothing left to fall back to).
- C3a: S3 with the baton kind RAISES on a scan failure — never a dict.
- C3b: S1 returns HTTP 500 on a scan failure — never a 200 with an empty list —
  and the body carries neither ``refused`` nor ``scan_ok``.
- C3c: S2's fail-soft READ path: when the scan fails once but ``read_projects``
  survives, the list is served rather than blanked.
- C4: S1 with ``?agent=borges`` (not a participant) -> the list is empty but the
  count is STILL 1: the count is not narrowed by the filter.
- C5: S1 with ``?active_only=false`` -> closed items are included, and the scan
  itself was called with ``active_only=False`` (the parameterisation pin).
- C6: exactly ONE ``scan_projects`` call per served request on the SUCCESS path
  on S1, S2, S3 — a count can never annotate a list read at a different
  moment. A fail-soft request may take two (C3c re-scans).
- C7: all four surfaces expose both keys with the SAME types in the clean state
  (int/bool); the two board JSON routes that still return an envelope on a scan
  failure (S2, BOARD) pin the failure state (NoneType/bool). The four are
  enumerated by name in ``_four_surfaces`` — a fifth surface must extend that
  list, or its key pair goes unchecked here.
- C8: with a refused entry present, ``init`` still succeeds (the deliberate
  fail-open of ``_refuse_existing_id``: one planted file must not block every
  ``init``).
- C9: S3 with ``kinds=("dm",)`` -> ``null``/``false``: the baton half never
  scanned, so the pair is UNKNOWN, never a "clean store" ``0`` the read never
  observed.

RED-FIRST HONESTY: C1-C5, C7, C9 and C3c touch the new keys/behaviour and could
not pass on the base commit (``KeyError`` on the response dict), so a classic
red-first demonstration is unavailable there; discrimination is shown by the
count moving between C1 (0) and C2 (1), and by the envelope states in C3/C7.
C6 and C8 pass on the base commit and are controls: C6 because
``read_projects`` is already the thin ``scan_projects`` wrapper (#4526), and C8
because the fail-open is the behaviour that already exists. C3a and C3b also
pass on the base commit, deliberately: they pin the fail-loud behaviour the
base had and this branch's first round lost.
"""

import json
import os
import sqlite3

import pytest

from forum.coordination import FileStore, SeqAllocator, build_updates
from forum.coordination import init as _init
from forum.db import init_db
from forum.server import create_app


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
    lines += ["---", f"# {project_id}", "", "body text", ""]
    return "\n".join(lines)


def _seeded_store(tmp_path):
    """A FileStore holding one healthy baton (``PR-1``) plus its allocator."""
    store = FileStore(tmp_path / "coord")
    store.write_project("PR-1", _baton_md("PR-1", seq=1), seq=1)
    allocator = SeqAllocator(recover=store.recover_max_seq)
    return store, allocator


def _plant_symlink(tmp_path, store):
    """One entry the scan declines to read: a symlink pointing outside the store."""
    outside = tmp_path / "outside.md"
    outside.write_text(_baton_md("EVIL", seq=999), encoding="utf-8")
    os.symlink(outside, store.projects_dir / "evil.md")


def _client(tmp_path, store, allocator):
    """A Flask test client over ``store``/``allocator``.

    The suite's autouse auth fixture wraps ``Flask.test_client``, so the client
    carries the #1459 token for the identity each request names.
    """
    db_path = str(tmp_path / "forum.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(db_path, str(tmp_path / "audit.jsonl"))
    app.config["TESTING"] = True
    app.config["COORD_STORE"] = store
    app.config["COORD_ALLOCATOR"] = allocator
    return app.test_client()


def _counting_scan(store, monkeypatch):
    """Wrap ``store.scan_projects`` to record each call's ``active_only`` value.

    The list and its refusal count must come from ONE scan, so this pins the
    count to the list it annotates on every surface (#4526's pattern).
    """
    calls = []
    real_scan = store.scan_projects

    def counting_scan(*, active_only=True):
        calls.append(active_only)
        return real_scan(active_only=active_only)

    monkeypatch.setattr(store, "scan_projects", counting_scan)
    return calls


def _four_surfaces(client, store, allocator, agent="ariadne"):
    """The four response envelopes, enumerated BY NAME.

    S1 and S2 are routes, S3 the builder, and ``BOARD`` the board JSON surface
    #4526 shipped (named here so the drift assertion compares every surface that
    carries the pair, not only the three this PR added). A fifth surface must
    extend this list; nothing discovers it automatically.
    """
    return {
        "S1": json.loads(client.get("/api/projects").data),
        "S2": json.loads(client.get("/api/board/updates").data),
        "S3": build_updates(store, allocator, agent),
        "BOARD": json.loads(client.get("/api/board/projects").data),
    }


# ---------------------------------------------------------------------------
# C1 (control): a clean store is a REAL zero on all four surfaces
# ---------------------------------------------------------------------------
def test_c1_clean_store_reports_zero_on_all_four_surfaces(tmp_path):
    store, allocator = _seeded_store(tmp_path)
    client = _client(tmp_path, store, allocator)

    envelopes = _four_surfaces(client, store, allocator)

    for name, data in envelopes.items():
        assert "refused" in data, name
        assert "scan_ok" in data, name
        assert data["refused"] == 0, name          # never 1 "just in case"
        assert data["scan_ok"] is True, name

    # In-cell control: the healthy baton is still served on each surface.
    assert [p["project_id"] for p in envelopes["S1"]["projects"]] == ["PR-1"]
    assert {i["project"] for i in envelopes["S2"]["updates"]} == {"PR-1"}
    assert {u["project_id"] for u in envelopes["S3"]["updates"]} == {"PR-1"}
    assert {b["project"] for b in envelopes["BOARD"]["board"]} == {"PR-1"}


# ---------------------------------------------------------------------------
# C2: a planted refusal is counted on all four surfaces; the list is unchanged
# ---------------------------------------------------------------------------
def test_c2_planted_symlink_is_counted_on_all_four_surfaces(tmp_path):
    store, allocator = _seeded_store(tmp_path)
    _plant_symlink(tmp_path, store)
    client = _client(tmp_path, store, allocator)

    envelopes = _four_surfaces(client, store, allocator)

    for name, data in envelopes.items():
        assert data["refused"] == 1, name
        assert data["scan_ok"] is True, name

    # The refused entry contributes no record: the list is exactly what it was.
    assert [p["project_id"] for p in envelopes["S1"]["projects"]] == ["PR-1"]
    assert {i["project"] for i in envelopes["S2"]["updates"]} == {"PR-1"}
    assert {u["project_id"] for u in envelopes["S3"]["updates"]} == {"PR-1"}
    assert {b["project"] for b in envelopes["BOARD"]["board"]} == {"PR-1"}


# ---------------------------------------------------------------------------
# C3: S2 keeps the fail-soft it had BEFORE this branch (UNKNOWN, 200)
# ---------------------------------------------------------------------------
def test_c3_board_updates_keeps_its_pre_existing_fail_soft(tmp_path, monkeypatch):
    store, allocator = _seeded_store(tmp_path)

    def boom(*, active_only=True):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(store, "scan_projects", boom)
    client = _client(tmp_path, store, allocator)

    resp = client.get("/api/board/updates")

    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["refused"] is None       # UNKNOWN, not a clean-store 0
    assert data["scan_ok"] is False
    # `FileStore.read_projects` IS the scan, so the fallback read fails too.
    assert data["updates"] == []


# ---------------------------------------------------------------------------
# C3a: S3 feeds a CURSOR consumer, so a failed scan RAISES -- no degraded 200
# ---------------------------------------------------------------------------
def test_c3a_build_updates_raises_on_a_failed_scan(tmp_path, monkeypatch):
    store, allocator = _seeded_store(tmp_path)

    def boom(*, active_only=True):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(store, "scan_projects", boom)

    # tools/forum-updates-monitor.sh advances its cursor on any 200 carrying a
    # usable watermark, so a degraded envelope would move the cursor past baton
    # events that were never read. The failure must escape, not return a dict.
    with pytest.raises(RuntimeError, match="scan exploded"):
        build_updates(store, allocator, "ariadne", kinds=("baton",))


# ---------------------------------------------------------------------------
# C3b: S1 fails LOUD -- a 500, never a 200 with an empty list
# ---------------------------------------------------------------------------
def test_c3b_projects_route_returns_500_on_a_failed_scan(tmp_path, monkeypatch):
    store, allocator = _seeded_store(tmp_path)

    def boom(*, active_only=True):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(store, "scan_projects", boom)
    client = _client(tmp_path, store, allocator)
    # Let the exception become Flask's 500 instead of propagating through the
    # test client (testing mode re-raises by default).
    client.application.config["PROPAGATE_EXCEPTIONS"] = False

    resp = client.get("/api/projects")

    # The consumer shows "coordination state unavailable" only on an HTTP
    # error; a 200 with [] would render as "you have no batons".
    assert resp.status_code == 500
    # No envelope: neither key exists on the error body.
    data = resp.get_json(silent=True)
    assert data is None or ("refused" not in data and "scan_ok" not in data)
    assert b"refused" not in resp.data
    assert b"scan_ok" not in resp.data


# ---------------------------------------------------------------------------
# C3c: S2's fail-soft READ path still serves the list when the scan fails once
# ---------------------------------------------------------------------------
def test_c3c_fail_soft_read_still_serves_the_list_on_s2(tmp_path, monkeypatch):
    # S2's `_scan_board(fail_soft=True)` returns (None, None), then
    # `read_project_board(records=None)` performs its own read, which succeeds
    # if only the first scan call failed. This is the pre-existing board
    # fail-soft -- NOT a contract S1 or S3 carry (C3a/C3b).
    store, allocator = _seeded_store(tmp_path)
    client = _client(tmp_path, store, allocator)
    real_scan = store.scan_projects
    state = {"armed": True}

    def flaky(*, active_only=True):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("scan exploded")
        return real_scan(active_only=active_only)

    monkeypatch.setattr(store, "scan_projects", flaky)

    resp = client.get("/api/board/updates")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["refused"] is None
    assert data["scan_ok"] is False
    assert {i["project"] for i in data["updates"]} == {"PR-1"}


# ---------------------------------------------------------------------------
# C4: S1's ?agent= narrows the list, NEVER the count
# ---------------------------------------------------------------------------
def test_c4_agent_filter_does_not_narrow_the_refused_count(tmp_path):
    store, allocator = _seeded_store(tmp_path)  # participants: ariadne, sol
    _plant_symlink(tmp_path, store)
    client = _client(tmp_path, store, allocator)

    data = json.loads(client.get("/api/projects?agent=borges").data)

    # borges is not a participant, so the LIST is empty...
    assert data["projects"] == []
    # ...but a refused entry has no participants to filter on, so the COUNT is
    # unchanged. Reading 0 here would be "0 refused among my items", which the
    # number cannot support.
    assert data["refused"] == 1
    assert data["scan_ok"] is True


# ---------------------------------------------------------------------------
# C5: S1 honours active_only (the _scan_board parameterisation)
# ---------------------------------------------------------------------------
def test_c5_active_only_false_includes_closed_items(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "coord")
    store.write_project(
        "PR-1", _baton_md("PR-1", seq=1, turn_since="2026-06-26T00:00:00Z"), seq=1
    )
    store.write_project(
        "PR-DONE",
        _baton_md("PR-DONE", status="merged", seq=2, turn_since="2026-06-27T00:00:00Z"),
        seq=2,
    )
    allocator = SeqAllocator(recover=store.recover_max_seq)
    client = _client(tmp_path, store, allocator)
    calls = _counting_scan(store, monkeypatch)

    active = json.loads(client.get("/api/projects").data)
    everything = json.loads(client.get("/api/projects?active_only=false").data)

    assert [p["project_id"] for p in active["projects"]] == ["PR-1"]
    assert {p["project_id"] for p in everything["projects"]} == {"PR-1", "PR-DONE"}
    # Filtering is not refusal: closed items are filtered, never counted.
    assert active["refused"] == 0
    assert everything["refused"] == 0
    # The scan itself was parameterised, not hardcoded to active_only=False.
    assert calls == [True, False]


# ---------------------------------------------------------------------------
# C6: exactly ONE scan per served request on the SUCCESS path (S1, S2, S3)
# ---------------------------------------------------------------------------
def test_c6_exactly_one_scan_per_served_request_on_success(tmp_path, monkeypatch):
    # Success path only: a FAIL-SOFT request may take two scans (C3c re-scans
    # after `_scan_board` returns (None, None)).
    store, allocator = _seeded_store(tmp_path)
    client = _client(tmp_path, store, allocator)
    calls = _counting_scan(store, monkeypatch)

    client.get("/api/projects")
    assert calls == [True]        # honours the route's default active_only

    calls.clear()
    client.get("/api/board/updates")
    assert calls == [False]       # read_project_board renders active_only=False

    calls.clear()
    build_updates(store, allocator, "ariadne")
    assert calls == [True]        # build_updates' baton half reads active records


# ---------------------------------------------------------------------------
# C7: the key pair cannot drift between surfaces (types pinned in both states)
# ---------------------------------------------------------------------------
def test_c7_key_pair_same_types_on_every_surface(tmp_path, monkeypatch):
    store, allocator = _seeded_store(tmp_path)
    client = _client(tmp_path, store, allocator)

    clean = _four_surfaces(client, store, allocator)
    for name, data in clean.items():
        # `type(...) is int` rather than isinstance: bool is an int subclass, so
        # isinstance(True, int) would pass against a boolean dressed as a count.
        assert type(data["refused"]) is int, name
        assert type(data["scan_ok"]) is bool, name
        assert data["scan_ok"] is True, name

    def boom(*, active_only=True):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(store, "scan_projects", boom)
    # Only the two board JSON routes still return an envelope on a scan failure:
    # S1 is a 500 (C3b) and S3 raises (C3a).
    failed = {
        "S2": json.loads(client.get("/api/board/updates").data),
        "BOARD": json.loads(client.get("/api/board/projects").data),
    }
    for name, data in failed.items():
        assert data["refused"] is None, name
        assert type(data["scan_ok"]) is bool, name
        assert data["scan_ok"] is False, name


# ---------------------------------------------------------------------------
# C9: S3 with the baton kind narrowed away is UNKNOWN, not a fake clean zero
# ---------------------------------------------------------------------------
def test_c9_kinds_without_baton_reports_unknown_not_zero(tmp_path):
    store, allocator = _seeded_store(tmp_path)

    out = build_updates(store, allocator, "ariadne", kinds=("dm",))

    # No project scan ran, so there is no list for a count to describe: UNKNOWN,
    # never a "clean store" 0 that would be a claim the read never made.
    assert out["refused"] is None
    assert out["scan_ok"] is False


# ---------------------------------------------------------------------------
# C8: S4's deliberate fail-open — a refused entry never blocks `init`
# ---------------------------------------------------------------------------
def test_c8_init_still_succeeds_beside_a_refused_entry(tmp_path):
    store, allocator = _seeded_store(tmp_path)
    _plant_symlink(tmp_path, store)
    # The refusal is real...
    assert store.scan_projects(active_only=False).refused == 1

    # ...and `init` still succeeds: `_refuse_existing_id` can only compare ids
    # it can read, and raising on a refused entry would let one planted file
    # block every `init` (the availability class #4518 exists to prevent).
    seq = _init(
        store,
        allocator,
        "PR-2",
        title="second",
        status="in-progress",
        turn="ariadne",
        participants=["ariadne", "sol"],
        turn_reason="start",
        transition_phase="fairy",
    )

    assert isinstance(seq, int) and seq > 1
    assert store.read_project("PR-2") is not None
