"""Snapshot rendering of context edges after #2243 (cites -> about).

`_generate_snapshot` printed a lesson's `Context:` line from `cites` edges
only, and a task's edge lines from `cites` / `subtask_of` only. Since #2243
writes `about` for a non-evidence context reference, NEW lessons and tasks
would silently lose those lines. Contract:

  1. a new lesson with a non-evidence `context_ids` shows `Context: <id>`;
  2. tasks are not rendered by `_generate_snapshot` at all (`type_order` omits
     "task"; the task branch is dead code), so there is no task line to lose;
     pinned as a finding;
  3. a LEGACY lesson with a raw `cites` edge still renders `Context:`;
  4. a lesson with an `about` edge to a PERSON node shows that id in `Context:`.
     Pinned on purpose: `about` is now the context relation (Lei's ruling,
     2026-10-06), so a person anchor may appear in a lesson's Context line.
"""

import json
import os
import shutil
import sys
import tempfile

import pytest


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_snapshot_ctx_2243_")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    from engram_core import _get_db
    conn = _get_db()
    yield server, conn
    shutil.rmtree(d, ignore_errors=True)


def _call(fn, **p):
    return json.loads(fn(payload_json=json.dumps(p)))


def _obs(server, tag="seed"):
    r = _call(
        server.engram_add_observation,
        url=f"https://example.com/{tag}", title=f"Src {tag}",
        quoted_text=f"{tag} verbatim quote text.",
        interpretation=f"{tag} interpretation text here.",
        claim=f"{tag} granite quarry output rose in spring.",
        quote_type="hard_data",
    )
    assert "observation_id" in r, r
    return r["observation_id"]


def _definition(server):
    r = _call(server.engram_add_definition, term="quarry", definition="A place where stone is cut.")
    return r["definition_id"]


def _lesson(server, incident, **extra):
    r = _call(
        server.engram_add_lesson,
        claim="When a quarry report arrives, check the season first.",
        incident_ids=incident,
        scaffolding_nudge="Check the season before reading the quarry figure.",
        logical_chain="The single quarry incident shows seasonality confounds output.",
        situation_pattern="quarry output report received",
        **extra,
    )
    assert "lesson_id" in r, r
    return r["lesson_id"]


def _task(server, **extra):
    r = _call(server.engram_add_task, description="Check the quarry season table", **extra)
    assert "task_id" in r, r
    return r["task_id"]


def _snapshot(conn):
    import engram_core
    return engram_core._generate_snapshot(conn)


def _block(snapshot, node_id):
    """The snapshot lines of one node: from its `### <id>` header to the blank line."""
    lines = snapshot.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"### {node_id} "))
    out = []
    for l in lines[start:]:
        if out and not l.strip():
            break
        out.append(l)
    return out


def _raw_edge(conn, src, tgt, rel):
    conn.execute("INSERT INTO edges (source_id, target_id, relation, created_at) "
                 "VALUES (?, ?, ?, datetime('now'))", (src, tgt, rel))
    conn.commit()


def test_c1_new_lesson_context_line_shows_non_evidence_context(env):
    server, conn = env
    df = _definition(server)
    ls = _lesson(server, _obs(server), context_ids=df)
    assert f"Context: {df}" in _block(_snapshot(conn), ls)


def test_c2_task_nodes_are_not_rendered_in_the_snapshot_at_all(env):
    """FINDING (not a contract of #2243): `_generate_snapshot`'s `type_order`
    omits "task", so the task branch (and its `cites`/`subtask_of` query) is
    unreachable and a task's edge lines never appear in a snapshot, before or
    after this change. Pinned so that the day a task is added to `type_order`
    this cell goes red and the task edge lines get their own cells."""
    server, conn = env
    df = _definition(server)
    tk = _task(server, implements_ids=df)
    snap = _snapshot(conn)
    assert f"### {tk} " not in snap


def test_c3_legacy_lesson_cites_edge_still_renders_context(env):
    server, conn = env
    df = _definition(server)
    ls = _lesson(server, _obs(server))
    _raw_edge(conn, ls, df, "cites")
    assert f"Context: {df}" in _block(_snapshot(conn), ls)


def test_c4_lesson_about_person_shows_person_in_context(env):
    server, conn = env
    p = _call(server.engram_add_person, name="Self Anchor", relation="self", is_self=True)
    pn = p["person_id"]
    ls = _lesson(server, _obs(server))
    _raw_edge(conn, ls, pn, "about")
    assert f"Context: {pn}" in _block(_snapshot(conn), ls)


def _derive(server, supporting, **extra):
    r = _call(server.engram_derive, claim="Quarry output is seasonal.",
              supporting_ids=supporting,
              logical_chain="The observation shows a spring rise; spring recurs; so output is seasonal.",
              reasoning_type="inductive_analogy", **extra)
    assert "derivation_id" in r, r
    return r["derivation_id"]


def _derives_from_line(snapshot, node_id):
    return next((l for l in _block(snapshot, node_id) if l.startswith("Derives from:")), "")


def test_derivation_context_id_is_not_listed_under_derives_from(env):
    """DELIBERATE: context is not a premise. Before #2243 a derivation's
    `context_ids` was a `cites` edge, and the snapshot's `Derives from:` query
    unions 'cites', so the context id showed there. It is `about` now and is
    not shown: the label asserts completeness of PREMISES. The premise
    (supporting id) must still show."""
    server, conn = env
    premise = _obs(server)
    df = _definition(server)
    dv = _derive(server, premise, context_ids=df)
    line = _derives_from_line(_snapshot(conn), dv)
    assert premise in line, line
    assert df not in line, line


def test_legacy_derivation_cites_row_still_listed_under_derives_from(env):
    """Existing union behaviour kept: a raw legacy `cites` row to a non-ev
    node still renders under `Derives from:`, so nothing that rendered before
    disappears."""
    server, conn = env
    premise = _obs(server)
    df = _definition(server)
    dv = _derive(server, premise)
    _raw_edge(conn, dv, df, "cites")
    line = _derives_from_line(_snapshot(conn), dv)
    assert premise in line and df in line, line
