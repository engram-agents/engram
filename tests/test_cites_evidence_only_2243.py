"""`cites` carries observation -> evidence ONLY (#2243, Lei's ruling 2026-10-06).

Before this change ten context writers minted a `cites` edge for a `context_ids` /
`implements_ids` reference whose target is NOT an evidence node (plus the
observation writer: eleven `INSERT ... 'cites'` literals in all). Those now write
`about` (a non-provenance, non-DAG relation), and the one place that writes
relation `cites` (`engram_core._insert_cites_edge`) refuses a non-`ev_` target.

Contract (written before the implementation; see the PR for the red run):

  C1  per minting site: `context_ids` (or `implements_ids`) naming a NON-evidence
      node -> an `about` edge to it and NO `cites` edge; the tool response keeps
      its keys.
  C2  same call naming an `ev_` node -> `about` as well: `cites` needs an
      OBSERVATION source AND an `ev_` target (Kepler/Lei strict form), and no
      context_ids site has an observation source.
  C3  `_insert_cites_edge` refuses a non-`ev_` target (ValueError naming `about`
      and the target id) and a non-observation source (ValueError stating
      "cites carries observation->evidence only" and the source id), nothing
      written either way; it accepts observation -> `ev_`.
  C4  the observation add path still writes exactly one `cites` edge to its
      evidence node.
  C5  a `pn_` (person) context target gets an `about` edge. DECISION, pinned on
      purpose: `about` already means recall-routing aboutness for person targets
      (it is migrated when a person node is superseded, engram_revision.py), and
      the old context `cites` -> person was annotation-only.
  C6  the add_edge verb refuses relation `cites` (pre-existing behaviour, pinned).
  C7  drift guard: no raw `INSERT INTO edges ... 'cites'` literal under
      `src/engram/` outside `_insert_cites_edge`, plus a planted-literal positive
      control proving the scanner can fail.
  C8  the docstrings / tool descriptions that said "Creates 'cites' edges" for
      context_ids now say `about`.

Legacy loose `cites` edges already stored in a graph are NOT touched here (the
bulk retype is a separate step of #2243).
"""

import ast
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import tempfile

import pytest

SRC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "src" / "engram"


# --------------------------------------------------------------------------
# fixture: a fresh server over a scratch data dir (same shape as
# test_non_object_metadata_write_sweep_3413.py)
# --------------------------------------------------------------------------

@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_cites_2243_")
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
    return r


def _definition(server):
    r = _call(server.engram_add_definition, term="quarry", definition="A place where stone is cut.")
    assert r.get("status") == "created", r
    return r["definition_id"]


def _edges(conn, source_id, target_id):
    return sorted(
        row[0] for row in conn.execute(
            "SELECT relation FROM edges WHERE source_id = ? AND target_id = ?",
            (source_id, target_id)).fetchall())


# --------------------------------------------------------------------------
# the ten context-minting sites (the eleventh `cites` literal, the observation
# writer, is C4): name -> maker(server, ctx) -> (response, new_id)
# `ctx` is the comma-separated id string for the context reference.
# `seed` is a fresh observation id usable as a premise / incident.
# --------------------------------------------------------------------------

def _mk_axiom(server, ctx, seed):
    r = _call(server.engram_add_axiom, claim="Shortcuts erode integrity.",
              basis="Foundational.", context_ids=ctx)
    return r, r.get("axiom_id")


def _mk_definition(server, ctx, seed):
    r = _call(server.engram_add_definition, term="ledger",
              definition="A book of record.", context_ids=ctx)
    return r, r.get("definition_id")


def _mk_conjecture(server, ctx, seed):
    r = _call(server.engram_add_conjecture, claim="Seasonality drives quarry output.",
              basis="One season of data.", context_ids=ctx)
    return r, r.get("conjecture_id")


def _mk_lesson(server, ctx, seed):
    r = _call(
        server.engram_add_lesson,
        claim="When a quarry report arrives, check the season first.",
        incident_ids=seed,
        scaffolding_nudge="Check the season before reading the quarry figure.",
        logical_chain="The single quarry incident shows seasonality confounds output.",
        situation_pattern="quarry output report received",
        context_ids=ctx,
    )
    return r, r.get("lesson_id")


def _mk_ask(server, ctx, seed):
    r = _call(server.engram_ask, question="Why did quarry output rise?", context_ids=ctx)
    return r, r.get("question_id")


def _mk_goal(server, ctx, seed):
    r = _call(server.engram_add_goal, claim="Measure reasoning change, not accumulation.",
              motivation="Wire work to this goal.", context_ids=ctx)
    return r, r.get("goal_id")


def _mk_task_implements(server, ctx, seed):
    r = _call(server.engram_add_task, description="Check the quarry season table",
              implements_ids=ctx)
    return r, r.get("task_id")


def _mk_feeling(server, ctx, seed):
    r = _call(server.engram_report_feeling, reported_state="Recognition of familiar terrain.",
              trigger="Re-encountering a quarry report.", context_ids=ctx)
    return r, r.get("feeling_id")


def _mk_person(server, ctx, seed):
    r = _call(server.engram_add_person, name="Quarry Foreman", relation="colleague",
              context_ids=ctx)
    return r, r.get("person_id")


def _mk_derive(server, ctx, seed):
    r = _call(
        server.engram_derive,
        claim="Quarry output is seasonal.",
        supporting_ids=seed,
        logical_chain="The observation shows a spring rise; spring recurs; so output is seasonal.",
        reasoning_type="inductive_analogy",
        context_ids=ctx,
    )
    return r, r.get("derivation_id")


SITES = {
    "engram_epistemic._add_axiom_impl": _mk_axiom,
    "engram_epistemic._add_definition_impl": _mk_definition,
    "engram_epistemic._add_conjecture_impl": _mk_conjecture,
    "engram_epistemic._add_lesson_impl": _mk_lesson,
    "engram_derive._ask_impl": _mk_ask,
    "engram_tasks._add_goal_impl": _mk_goal,
    "engram_tasks._add_task_impl(implements_ids)": _mk_task_implements,
    "engram_tasks._report_feeling_impl": _mk_feeling,
    "engram_trust._add_person_impl": _mk_person,
    "engram_core._create_derivation": _mk_derive,
}

# The response keys each site returned BEFORE this change (captured from the
# pre-change code with a non-evidence `ob_` context target; the key set must
# not move: only the edge relation changes).
EXPECTED_KEYS = {
    "engram_core._create_derivation": ["claim", "confidence", "context_nodes", "derivation_id", "logical_chain_preview", "reasoning_class", "reasoning_type", "status", "structure_warnings", "supporting_nodes", "utility_bumped_count"],
    "engram_derive._ask_impl": ["context_nodes", "message", "question", "question_id", "status"],
    "engram_epistemic._add_axiom_impl": ["axiom_id", "claim", "confidence", "context_nodes", "status"],
    "engram_epistemic._add_conjecture_impl": ["claim", "confidence", "conjecture_id", "context_nodes", "status"],
    "engram_epistemic._add_definition_impl": ["context_nodes", "definition", "definition_id", "status", "term"],
    "engram_epistemic._add_lesson_impl": ["claim", "context_nodes", "importance_anchored", "incident_nodes", "lesson_id", "prevention_type", "reasoning_class", "reasoning_type", "scaffolding_nudge", "status", "tripwire_priority"],
    "engram_tasks._add_goal_impl": ["claim", "context_nodes", "goal_id", "motivation", "status"],
    "engram_tasks._add_task_impl(implements_ids)": ["description", "implements", "importance_base", "scope", "status", "task_id", "task_status"],
    "engram_tasks._report_feeling_impl": ["categorical_tag", "claim", "context_fingerprint", "context_nodes", "feeling_id", "intensity_hint", "no_confidence", "nudge_source", "status"],
    "engram_trust._add_person_impl": ["aliases", "context_nodes", "name", "person_id", "relation", "status"],
}


def _run_site(env, site, target_kind):
    server, conn = env
    seed_obs = _obs(server, "seed")
    seed = seed_obs["observation_id"]
    if target_kind == "ev":
        target = seed_obs["evidence_id"]
    elif target_kind == "df":
        target = _definition(server)
    else:
        target = _obs(server, "ctx")["observation_id"]
    resp, new_id = SITES[site](server, target, seed)
    assert new_id, f"{site}: creation failed: {resp}"
    return conn, resp, new_id, target


@pytest.mark.parametrize("site", sorted(SITES))
def test_c1_non_evidence_context_target_gets_about_not_cites(env, site):
    conn, resp, new_id, target = _run_site(env, site, "ob")
    assert target.startswith("ob_")
    rels = _edges(conn, new_id, target)
    assert "cites" not in rels, (site, rels)
    assert "about" in rels, (site, rels)
    assert "error" not in resp, resp


@pytest.mark.parametrize("site", sorted(SITES))
def test_c1_definition_target_gets_about_not_cites(env, site):
    conn, resp, new_id, target = _run_site(env, site, "df")
    assert target.startswith("df_")
    rels = _edges(conn, new_id, target)
    assert "cites" not in rels, (site, rels)
    assert "about" in rels, (site, rels)


@pytest.mark.parametrize("site", sorted(SITES))
def test_c1_response_keys_unchanged(env, site):
    conn, resp, new_id, target = _run_site(env, site, "ob")
    assert sorted(resp.keys()) == EXPECTED_KEYS[site], (site, sorted(resp.keys()))


@pytest.mark.parametrize("site", sorted(SITES))
def test_c2_evidence_context_target_from_non_observation_source_is_about(env, site):
    """Strict form: `cites` is observation -> evidence ONLY. None of the ten
    context sites has an observation source, so even an `ev_` context target
    gets `about`."""
    conn, resp, new_id, target = _run_site(env, site, "ev")
    assert target.startswith("ev_")
    assert _edges(conn, new_id, target) == ["about"], site


def test_c2_no_context_site_has_an_observation_source():
    """The cell above is exhaustive only while this holds: every context site
    mints a non-observation node type."""
    assert not any("observation" in name for name in SITES), sorted(SITES)


def test_c1_report_feeling_keeps_cited_ids_list(env):
    """engram_report_feeling keeps its `context_nodes` return key (the
    `cited_ids` list inside the impl) naming the referenced node."""
    conn, resp, new_id, target = _run_site(env, "engram_tasks._report_feeling_impl", "ob")
    assert target in resp.get("context_nodes", []), resp


def test_c1_create_derivation_keeps_ctx_used(env):
    conn, resp, new_id, target = _run_site(env, "engram_core._create_derivation", "ob")
    assert target in resp.get("context_nodes", []), resp


# C5 -- person target. DECISION (pinned on purpose): `about` already means
# recall-routing aboutness for person targets (it is migrated when a person node
# is superseded, engram_revision.py), and the old context `cites` -> person edge
# was annotation-only. So a `pn_` context target gets `about`.
def test_c5_person_context_target_gets_about(env):
    server, conn = env
    p = _call(server.engram_add_person, name="Self Anchor", relation="self", is_self=True)
    pn = p.get("person_id")
    assert pn and pn.startswith("pn_"), p
    r = _call(server.engram_add_axiom, claim="Honesty is structural.", basis="Axiom.",
              context_ids=pn)
    assert _edges(conn, r["axiom_id"], pn) == ["about"], r


# --------------------------------------------------------------------------
# C3 -- the guard
# --------------------------------------------------------------------------

def _two_nodes(env):
    server, conn = env
    seed = _obs(server, "seed")
    other = _definition(server)
    return conn, seed["observation_id"], seed["evidence_id"], other


def test_c3_insert_cites_edge_refuses_non_evidence_target(env):
    import engram_core
    conn, ob_id, ev_id, df_id = _two_nodes(env)
    before = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    with pytest.raises(ValueError) as ei:
        engram_core._insert_cites_edge(conn, ob_id, df_id, "2026-10-06T00:00:00Z")
    msg = str(ei.value)
    assert "about" in msg and df_id in msg, msg
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == before
    assert _edges(conn, ob_id, df_id) == []


def test_c3_insert_cites_edge_refuses_non_observation_source(env):
    import engram_core
    conn, ob_id, ev_id, df_id = _two_nodes(env)
    before = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    with pytest.raises(ValueError) as ei:
        engram_core._insert_cites_edge(conn, df_id, ev_id, "2026-10-06T00:00:00Z")
    msg = str(ei.value)
    assert "observation->evidence only" in msg and df_id in msg, msg
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == before
    assert _edges(conn, df_id, ev_id) == []


def test_c3_insert_cites_edge_refuses_missing_source_row(env):
    import engram_core
    conn, ob_id, ev_id, df_id = _two_nodes(env)
    with pytest.raises(ValueError) as ei:
        engram_core._insert_cites_edge(conn, "ob_9999", ev_id, "2026-10-06T00:00:00Z")
    assert "ob_9999" in str(ei.value)


def test_c3_insert_cites_edge_accepts_observation_to_evidence(env):
    import engram_core
    server, conn = env
    first = _obs(server, "first")
    second = _obs(server, "second")
    engram_core._insert_cites_edge(
        conn, second["observation_id"], first["evidence_id"], "2026-10-06T00:00:00Z")
    assert _edges(conn, second["observation_id"], first["evidence_id"]) == ["cites"]


def test_c3_insert_context_edge_cites_only_for_observation_source_and_ev_target(env):
    import engram_core
    server, conn = env
    first = _obs(server, "first")
    second = _obs(server, "second")
    df = _definition(server)
    now = "2026-10-06T00:00:00Z"
    ob2, ev1, ob1 = second["observation_id"], first["evidence_id"], first["observation_id"]
    engram_core._insert_context_edge(conn, ob2, ev1, now)   # observation -> ev_  : cites
    engram_core._insert_context_edge(conn, df, ev1, now)    # non-observation -> ev_ : about
    engram_core._insert_context_edge(conn, ob2, ob1, now)   # observation -> non-ev : about
    engram_core._insert_context_edge(conn, ev1, ob1, now)   # evidence source -> non-ev : about
    assert _edges(conn, ob2, ev1) == ["cites"]
    assert _edges(conn, df, ev1) == ["about"]
    assert _edges(conn, ob2, ob1) == ["about"]
    assert _edges(conn, ev1, ob1) == ["about"]


def test_c3_duplicate_context_edge_still_raises_integrity_error(env):
    """Callers keep their `except sqlite3.IntegrityError: pass` wrapper."""
    import engram_core
    conn, ob_id, ev_id, df_id = _two_nodes(env)
    engram_core._insert_context_edge(conn, df_id, ob_id, "2026-10-06T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        engram_core._insert_context_edge(conn, df_id, ob_id, "2026-10-06T00:00:00Z")


def _predictive_source(env):
    """A node of type observation_predictive (re-typed from a fresh
    observation), plus an evidence id and a non-evidence target."""
    server, conn = env
    seed = _obs(server, "pred")
    df = _definition(server)
    other_ev = _obs(server, "other")["evidence_id"]
    conn.execute("UPDATE nodes SET type = 'observation_predictive' WHERE id = ?",
                 (seed["observation_id"],))
    conn.commit()
    return conn, seed["observation_id"], other_ev, df


def test_c3_insert_cites_edge_accepts_observation_predictive_source(env):
    import engram_core
    conn, pred, ev, df = _predictive_source(env)
    engram_core._insert_cites_edge(conn, pred, ev, "2026-10-06T00:00:00Z")
    rows = conn.execute(
        "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'cites' AND target_id = ?",
        (pred, ev)).fetchall()
    assert len(rows) == 1


def test_c3_insert_context_edge_routes_observation_predictive_source(env):
    import engram_core
    conn, pred, ev, df = _predictive_source(env)
    now = "2026-10-06T00:00:00Z"
    engram_core._insert_context_edge(conn, pred, ev, now)
    engram_core._insert_context_edge(conn, pred, df, now)
    assert _edges(conn, pred, ev) == ["cites"]
    assert _edges(conn, pred, df) == ["about"]


# --------------------------------------------------------------------------
# C4 -- the observation path is unchanged
# --------------------------------------------------------------------------

def test_c4_observation_writes_exactly_one_cites_edge_to_its_evidence(env):
    server, conn = env
    r = _obs(server, "c4")
    rows = conn.execute(
        "SELECT target_id FROM edges WHERE source_id = ? AND relation = 'cites'",
        (r["observation_id"],)).fetchall()
    assert [row[0] for row in rows] == [r["evidence_id"]]
    assert r["evidence_id"].startswith("ev_")


# --------------------------------------------------------------------------
# C6 -- add_edge refuses `cites` (pre-existing behaviour, pinned)
# --------------------------------------------------------------------------

def test_c6_add_edge_verb_refuses_cites(env):
    server, conn = env
    seed = _obs(server, "seed")["observation_id"]
    df = _definition(server)
    r = _call(server.engram_add_edge, source_id=seed, target_id=df, relation="cites")
    assert "error" in r and "not addable via engram_add_edge" in r["error"], r
    assert _edges(conn, seed, df) == []


# --------------------------------------------------------------------------
# C7 -- drift guard (modelled on test_metadata_parse_drift_guard_3413.py)
# --------------------------------------------------------------------------

# (relative path under src/engram, enclosing function) -> one-line reason.
# `_insert_cites_edge` is the single writer; everything else must route
# through it (directly or via `_insert_context_edge`).
CITES_WRITER = ("engram_core.py", "_insert_cites_edge")
ALLOW_LIST = {
    ("tools/surgical.py", "edge_add"):
        "dev tooling (T3): DB-surgery edge_add takes the relation as a CALLER-SUPPLIED variable, "
        "not a 'cites' literal; it is a deliberate operator override and is not routed",
}

# `INSERT ... INTO edges ... 'cites'` in one statement. A cites write is flagged
# when a string constant holding an INSERT into edges also holds the `cites`
# literal. (A variable relation is out of the scanner's reach; see the module
# docstring of the PR: the add_edge verb and surgical.py are the two such sites.)
_INSERT_EDGES = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+edges\b", re.I)
_CITES_LITERAL = re.compile(r"'cites'|\"cites\"", re.I)


def _enclosing(tree):
    """Yield (call_or_const_node, enclosing function name) for every string
    constant in the tree."""
    stack = []

    def visit(node):
        pushed = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if pushed:
            stack.append(node.name)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, (stack[-1] if stack else "<module>")
        for child in ast.iter_child_nodes(node):
            yield from visit(child)
        if pushed:
            stack.pop()

    yield from visit(tree)


def find_raw_cites_inserts(root):
    """Scan every `.py` under `root` for a raw cites INSERT literal. Returns a
    sorted list of (relative path, enclosing function, lineno)."""
    root = pathlib.Path(root)
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the real tree always parses
            continue
        # Adjacent string literals are one Constant after parsing, so a
        # multi-line "INSERT ..." "VALUES (?, ?, 'cites', ?)" is a single hit.
        for const, fn in _enclosing(tree):
            s = const.value
            if _INSERT_EDGES.search(s) and _CITES_LITERAL.search(s):
                found.append((rel, fn, const.lineno))
    return found


def test_c7_no_raw_cites_insert_outside_the_single_writer():
    hits = find_raw_cites_inserts(SRC_ROOT)
    offenders = [
        (rel, fn, ln) for rel, fn, ln in hits
        if (rel, fn) != CITES_WRITER and (rel, fn) not in ALLOW_LIST
    ]
    assert offenders == [], (
        "raw `INSERT INTO edges ... 'cites'` outside engram_core._insert_cites_edge "
        f"(route through _insert_cites_edge / _insert_context_edge): {offenders}")


def test_c7_the_single_writer_exists_and_is_the_only_hit():
    hits = find_raw_cites_inserts(SRC_ROOT)
    writer_hits = [h for h in hits if (h[0], h[1]) == CITES_WRITER]
    assert len(writer_hits) == 1, hits


def test_c7_allow_list_has_no_stale_entries():
    """An allow-list entry whose site no longer matches the scanner is stale
    (the surgical entry above is a variable-relation site the literal scan does
    not flag, so it is checked for existence of the function instead)."""
    for (rel, fn), reason in ALLOW_LIST.items():
        assert reason
        text = (SRC_ROOT / rel).read_text(encoding="utf-8")
        assert f"def {fn}" in text, (rel, fn)


def test_c7_planted_literal_positive_control(tmp_path):
    """The scanner can fail: a synthetic source with a raw cites insert is
    reported, both single-line and implicit-concatenation shapes."""
    (tmp_path / "planted.py").write_text(
        "def loose_writer(conn, a, b, now):\n"
        "    conn.execute(\n"
        "        \"INSERT INTO edges (source_id, target_id, relation, created_at) \"\n"
        "        \"VALUES (?, ?, 'cites', ?)\",\n"
        "        (a, b, now),\n"
        "    )\n"
        "def single_line(conn, a, b, now):\n"
        "    conn.execute(\"INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)\", (a, b, now))\n"
        "def innocent(conn, a, b, now):\n"
        "    conn.execute(\"INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'about', ?)\", (a, b, now))\n",
        encoding="utf-8",
    )
    # A file named like a real writer module and a nested one: the scanner
    # must not skip a module by name or depth.
    (tmp_path / "engram_trust.py").write_text(
        "def person_writer(conn, a, b, now):\n"
        "    conn.execute(\"INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)\", (a, b, now))\n",
        encoding="utf-8",
    )
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "nested.py").write_text(
        "def nested(conn, a, b, now):\n"
        "    conn.execute(\"INSERT INTO edges (source_id, target_id, relation, created_at) VALUES (?, ?, 'cites', ?)\", (a, b, now))\n",
        encoding="utf-8",
    )
    hits = find_raw_cites_inserts(tmp_path)
    assert sorted((rel, fn) for rel, fn, _ in hits) == [
        ("engram_trust.py", "person_writer"),
        ("planted.py", "loose_writer"),
        ("planted.py", "single_line"),
        ("tools/nested.py", "nested"),
    ], hits


# --------------------------------------------------------------------------
# C8 -- docs say `about`
# --------------------------------------------------------------------------

# Prose that tells an agent a context reference creates `cites`. Scans every
# `.py` under src/engram plus the capability index. Only PROSE is matched
# (docstrings, comments, tool descriptions), never the code that writes the
# relation. `cites` is legitimately named elsewhere (observation->evidence
# docs, relation tables), so the pattern is deliberately narrow: "creates /
# create / wires ... cites edge(s)", "implements_ids (cites edges)", a
# `context_ids` clause followed by "'cites' edges", "<node> cites axiom ...
# [via context_ids]".
STALE_CITES_PROSE = [
    re.compile(r"creates?\s+'?cites'?\s+edges?", re.I),
    re.compile(r"wires\s+[^\n]{0,40}\+\s*cites\s+edges?", re.I),
    re.compile(r"implements_ids\s*\(cites\s+edges?\)", re.I),
    re.compile(r"context_ids[^\n]{0,80}'cites'\s+(?:edges?|targets?)", re.I),
    re.compile(r"'cites'\s+edges?\s+for\s+definitions", re.I),
    re.compile(r"optional\s+'cites'\s+targets", re.I),
    re.compile(r"cites\s+axiom\s*\([^)]*\)\s*\[via\s+context_ids\]", re.I),
    re.compile(r"context_ids\s*(?:→|->)\s*'cites'", re.I),
    re.compile(r"#\s*Create\s+cites\s+edges", re.I),
]


def find_stale_cites_prose(root):
    root = pathlib.Path(root)
    files = sorted(root.rglob("*.py")) + sorted(root.glob("tool_capability_index.txt"))
    hits = []
    for path in files:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(rx.search(line) for rx in STALE_CITES_PROSE):
                hits.append((path.relative_to(root).as_posix(), i, line.strip()[:100]))
    return hits


def test_c8_no_stale_context_creates_cites_prose_anywhere_in_src():
    assert find_stale_cites_prose(SRC_ROOT) == []


def test_c8_planted_string_positive_control(tmp_path):
    (tmp_path / "planted.py").write_text(
        '"""context_ids: Optional node IDs. Creates cites edges."""\n'
        "# implements_ids (cites edges) and goal_id\n"
        "# Create cites edges for context\n"
        "# observation cites evidence (fine)\n",
        encoding="utf-8")
    (tmp_path / "tool_capability_index.txt").write_text(
        "x: context_ids (comma-separated 'cites' edges for definitions etc.)\n",
        encoding="utf-8")
    hits = find_stale_cites_prose(tmp_path)
    assert sorted((f, ln) for f, ln, _ in hits) == [
        ("planted.py", 1), ("planted.py", 2), ("planted.py", 3),
        ("tool_capability_index.txt", 1)], hits
