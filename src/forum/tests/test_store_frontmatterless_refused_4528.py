"""#4528: a ``projects/`` file whose frontmatter block does not PARSE is REFUSED.

Before this, ``scan_projects`` treated "no parseable frontmatter" as a valid
empty record: it emitted a ``ProjectRecord`` with every field empty and
``project_id`` falling back to the filename stem, and ``refused`` stayed 0. The
board's render then skipped that record via a ``startswith("---")`` branch --
so the item was invisible, but the store's own count said nothing was wrong.

Two silences are closed here:

- the scan REFUSES an unparseable file: ``refused`` increments and no record is
  emitted (so the existing refused-count chip reports it, #4518 part 2);
- ``_refuse_existing_id``'s filename-based second check counts only a file whose
  frontmatter PARSES, so ``init`` of an unparseable id is the repair path (it
  replaces the file) instead of a platform-dependent refusal.

The predicate is the store's own notion of parseable, ``frontmatter_parses(raw)``
(``_FRONTMATTER_RE`` under the hood) -- NOT ``startswith("---")`` and NOT "the
parsed field dict is empty". The distinction matters because a VALID EMPTY block
requires a blank line between the fences (the regex demands a newline before the
closing ``---``):

    "---\\n\\n---\\n# body\\n"   parses   -> fields {} but NOT refused   (C4)
    "---\\n---\\n# body\\n"      no parse -> refused                      (C4b)
    "---\\ntitle: x\\n"         no parse -> refused                      (C4c)

Cells:
- C1: a projects/ file with no frontmatter at all -> refused == 1, absent.
- C2: end-to-end ``GET /api/board/projects`` -> count reported, item absent.
- C3 (control): a clean store -> refused == 0 and the item renders.
- C4: a valid EMPTY block (note the blank line) is NOT refused and DOES appear.
- C4b: the degenerate block ``---\\n---\\n`` (no blank line) IS refused.
- C4c: an unclosed block ``---\\ntitle: x\\n`` IS refused.
- C5a: ``init`` of the exact id of an unparseable file SUCCEEDS (repair path)
  and the file is replaced by a well-formed baton.
- C5d: ``init`` of the exact id of an UNCLOSED block (``---\\ntitle: x\\n``)
  SUCCEEDS too -- the one gate fixture where ``startswith("---")`` and the real
  predicate DISAGREE, so this cell is what catches a gate that reaches for the
  heuristic (the degenerate ``---\\n---\\n`` block is another member of the
  unparseable class; C4b pins it on the scan side).
- C5b (control): ``init`` of the exact id of a well-formed file still raises.
- C5c (control): a case-variant ``init`` beside a well-formed file still raises
  -- pins the #3432 property, which must survive this change.
- C6: two unparseable files -> refused == 2 (a count, not a boolean).
"""

import json
import sqlite3

import pytest

from forum.coordination import (
    FileStore,
    ProjectAlreadyExists,
    SeqAllocator,
    init as _init,
)
from forum.db import init_db
from forum.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _seed_well_formed(store, project_id="PR-1"):
    """Create one well-formed baton through the writer; return its allocator."""
    allocator = SeqAllocator(recover=store.recover_max_seq)
    _init(
        store,
        allocator,
        project_id,
        title="healthy baton",
        status="in-progress",
        turn="ariadne",
        participants=["ariadne", "borges"],
        turn_reason="claimed from pool",
        ts="2026-06-26T00:00:00Z",
    )
    return allocator


def _init_kwargs():
    return dict(
        title="created",
        status="in-progress",
        turn="ariadne",
        participants=["ariadne"],
        turn_reason="created",
        ts="2026-06-27T00:00:00Z",
    )


def _board_client(tmp_path, store):
    """A test client for ``GET /api/board/projects`` wired to ``store``.

    Mirrors the sibling #4518 route cells (and test_project_board.py). The
    suite's autouse auth fixture wraps ``Flask.test_client``.
    """
    db_path = str(tmp_path / "forum-4528.db")
    sqlite3.connect(db_path).close()
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.close()
    app = create_app(
        db_path, str(tmp_path / "audit-4528.jsonl"), enable_board_viewer=True
    )
    app.config["TESTING"] = True
    app.config["COORD_STORE"] = store
    return app.test_client()


# ---------------------------------------------------------------------------
# C1: a no-frontmatter file is one refusal and no record
# ---------------------------------------------------------------------------
def test_c1_no_frontmatter_file_is_refused_and_absent(tmp_path):
    store = FileStore(tmp_path)
    _seed_well_formed(store)
    (store.projects_dir / "JUNK.md").write_text(
        "# This is a plain markdown file with no frontmatter\n\nbody\n",
        encoding="utf-8",
    )

    scan = store.scan_projects(active_only=False)

    assert scan.refused == 1
    # No ProjectRecord at all for the unparseable file -- not merely an
    # all-empty one.
    assert "JUNK" not in {r.project_id for r in scan.records}
    # In-cell control: the healthy baton beside it is still served.
    assert [r.project_id for r in scan.records] == ["PR-1"]


# ---------------------------------------------------------------------------
# C2: the user-visible JSON surface carries the count and omits the item
# ---------------------------------------------------------------------------
def test_c2_board_route_reports_count_and_omits_item(tmp_path):
    store = FileStore(tmp_path / "coord")
    _seed_well_formed(store)
    (store.projects_dir / "JUNK.md").write_text("# no frontmatter\n", encoding="utf-8")

    client = _board_client(tmp_path, store)
    resp = client.get("/api/board/projects")

    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["refused"] == 1
    assert data["scan_ok"] is True
    # The unparseable item is absent, and the healthy one still renders.
    assert [item["project"] for item in data["board"]] == ["PR-1"]


# ---------------------------------------------------------------------------
# C3 (control): a clean store is a REAL zero and items are unaffected
# ---------------------------------------------------------------------------
def test_c3_clean_store_reports_zero_refusals(tmp_path):
    store = FileStore(tmp_path)
    _seed_well_formed(store)
    _seed_well_formed(store, "PR-2")

    scan = store.scan_projects(active_only=False)

    # Without this, an implementation that refuses everything would pass C1/C2.
    assert scan.refused == 0
    assert {r.project_id for r in scan.records} == {"PR-1", "PR-2"}


# ---------------------------------------------------------------------------
# C4: a VALID EMPTY block (blank line between the fences) parses -> not refused
# ---------------------------------------------------------------------------
def test_c4_valid_empty_block_is_not_refused_and_appears(tmp_path):
    store = FileStore(tmp_path)
    store.projects_dir.mkdir(parents=True, exist_ok=True)
    # NOTE the blank line: the regex requires a newline before the closing
    # fence, so this is the well-formed empty block. Fields parse to {}.
    (store.projects_dir / "EMPTY.md").write_text(
        "---\n\n---\n# body\n", encoding="utf-8"
    )

    scan = store.scan_projects(active_only=False)

    assert scan.refused == 0
    assert [r.project_id for r in scan.records] == ["EMPTY"]


# ---------------------------------------------------------------------------
# C4b: the degenerate block with NO blank line does not parse -> refused
# ---------------------------------------------------------------------------
def test_c4b_degenerate_block_without_blank_line_is_refused(tmp_path):
    store = FileStore(tmp_path)
    store.projects_dir.mkdir(parents=True, exist_ok=True)
    (store.projects_dir / "DEGENERATE.md").write_text(
        "---\n---\n# body\n", encoding="utf-8"
    )

    scan = store.scan_projects(active_only=False)

    assert scan.refused == 1
    assert "DEGENERATE" not in {r.project_id for r in scan.records}


# ---------------------------------------------------------------------------
# C4c: an unclosed block does not parse -> refused
# ---------------------------------------------------------------------------
def test_c4c_unclosed_block_is_refused(tmp_path):
    store = FileStore(tmp_path)
    store.projects_dir.mkdir(parents=True, exist_ok=True)
    (store.projects_dir / "UNCLOSED.md").write_text(
        "---\ntitle: x\n", encoding="utf-8"
    )

    scan = store.scan_projects(active_only=False)

    assert scan.refused == 1
    assert "UNCLOSED" not in {r.project_id for r in scan.records}


# ---------------------------------------------------------------------------
# C5a: init of the exact id of an unparseable file is the REPAIR path
# ---------------------------------------------------------------------------
def test_c5a_init_of_unparseable_id_succeeds_and_repairs(tmp_path):
    store = FileStore(tmp_path)
    store.projects_dir.mkdir(parents=True, exist_ok=True)
    target = store.projects_dir / "BROKEN.md"
    target.write_text("# no frontmatter here\n", encoding="utf-8")

    allocator = SeqAllocator(recover=store.recover_max_seq)
    seq = _init(store, allocator, "BROKEN", **_init_kwargs())

    assert isinstance(seq, int)
    # The unparseable file was replaced by a well-formed baton ...
    replaced = target.read_text(encoding="utf-8")
    assert replaced.startswith("---\n")
    scan = store.scan_projects(active_only=False)
    assert scan.refused == 0
    assert [r.project_id for r in scan.records] == ["BROKEN"]


# ---------------------------------------------------------------------------
# C5d: init of the exact id of an UNCLOSED-block file is the repair path too
# ---------------------------------------------------------------------------
def test_c5d_init_of_unclosed_block_id_succeeds_and_repairs(tmp_path):
    store = FileStore(tmp_path)
    store.projects_dir.mkdir(parents=True, exist_ok=True)
    target = store.projects_dir / "UNCLOSED-INIT.md"
    # Opens a block and never closes it. This is the one gate fixture where
    # `raw.lstrip().startswith("---")` and the real predicate DISAGREE: the
    # heuristic calls this a baton worth reserving, the store's parse says it
    # is not a readable item at all. A gate that reaches for the heuristic
    # refuses this init; the repair path must allow it. (The degenerate
    # `---\n---\n` block is another member of the unparseable class; C4b pins
    # it on the scan side.)
    target.write_text("---\ntitle: x\n", encoding="utf-8")

    allocator = SeqAllocator(recover=store.recover_max_seq)
    seq = _init(store, allocator, "UNCLOSED-INIT", **_init_kwargs())

    assert isinstance(seq, int)
    # The unparseable file was REPLACED by a well-formed baton -- the content
    # assertions discriminate a real write from the untouched original, which
    # carries neither field (it also starts with "---\n", so a prefix check
    # alone would not).
    replaced = target.read_text(encoding="utf-8")
    assert "project: UNCLOSED-INIT" in replaced
    assert "## Turn log" in replaced
    scan = store.scan_projects(active_only=False)
    assert scan.refused == 0
    assert [r.project_id for r in scan.records] == ["UNCLOSED-INIT"]


# ---------------------------------------------------------------------------
# C5b (control): init of a well-formed file's exact id still raises
# ---------------------------------------------------------------------------
def test_c5b_init_of_well_formed_id_still_raises(tmp_path):
    store = FileStore(tmp_path)
    allocator = _seed_well_formed(store)

    with pytest.raises(ProjectAlreadyExists) as exc:
        _init(store, allocator, "PR-1", **_init_kwargs())

    assert exc.value.existing_ids == ["PR-1"]


# ---------------------------------------------------------------------------
# C5c (control): a case-variant init beside a well-formed file still raises
# ---------------------------------------------------------------------------
def test_c5c_case_variant_init_beside_well_formed_file_still_raises(tmp_path):
    store = FileStore(tmp_path)
    allocator = _seed_well_formed(store)

    with pytest.raises(ProjectAlreadyExists) as exc:
        _init(store, allocator, "pr-1", **_init_kwargs())

    # #3432's property: the EXISTING spelling is named, and nothing is created.
    assert exc.value.existing_ids == ["PR-1"]
    assert sorted(p.name for p in store.projects_dir.glob("*.md")) == ["PR-1.md"]


# ---------------------------------------------------------------------------
# C6: the count moves PER unparseable entry
# ---------------------------------------------------------------------------
def test_c6_two_unparseable_files_count_two(tmp_path):
    store = FileStore(tmp_path)
    store.projects_dir.mkdir(parents=True, exist_ok=True)
    (store.projects_dir / "A.md").write_text("# no frontmatter\n", encoding="utf-8")
    (store.projects_dir / "B.md").write_text(
        "---\n---\n# degenerate\n", encoding="utf-8"
    )

    scan = store.scan_projects(active_only=False)

    # Two declined entries -> 2, not a boolean dressed as an integer.
    assert scan.refused == 2
    assert scan.records == []
