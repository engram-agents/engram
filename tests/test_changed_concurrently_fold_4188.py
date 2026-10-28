"""Fold the six `changed_concurrently` refusals into one engram_core helper
(engram-alpha #4188). A no-behaviour-change refactor.

Contract:
  D1  no construction of the refusal exists under src/engram outside
      engram_core.changed_concurrently_refusal (red on the base, which has
      six: three modules' local helpers, one inline dict in each of
      engram_revision / engram_observation (evidence), and the requote call).
      D1 detects only two LITERAL forms: a dict literal with a constant
      "refusal": "changed_concurrently", and a direct
      `_requote_refusal("changed_concurrently", ...)` call. It does not detect
      other constructions (a computed key or value, json.dumps on a variable,
      etc.).
  R1  the helper itself rolls back the connection exactly once and returns the
      refusal JSON.
  S*  per call site, the string the tool returns is byte-identical to what the
      base returned for the same input. Each expected string was checked
      against a run of the BASE (295b7c97) before the fold; "<ID>" stands for
      the node id allocated in the fixture, substituted at compare time.

Wording differs between sites and is preserved (not unified):
  - trust (update_person, ...) and retract:   "Node '<ID>' changed ..."
  - tasks / epistemic helpers take a kind:    "Task|Goal|Lesson '<ID>' changed ..."
  - add_observation evidence passthrough:     "Evidence node '<ID>' changed ..."
  - requote:                                  "'<ID>' changed ... (another requote,
                                               a retract or a supersede got there first)"
"""

import ast
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "engram")

_TAIL = ("(another write got there first). Nothing was changed. "
         "Re-read the node and retry.")


def _generic(prefix):
    return json.dumps({
        "error": f"{prefix} '<ID>' changed while this call was running {_TAIL}",
        "refusal": "changed_concurrently",
    })


EXPECT_NODE = _generic("Node")
EXPECT_TASK = _generic("Task")
EXPECT_GOAL = _generic("Goal")
EXPECT_LESSON = _generic("Lesson")
EXPECT_EVIDENCE = _generic("Evidence node")
EXPECT_REQUOTE = json.dumps({
    "error": "'<ID>' changed while this call was running (another requote, a "
             "retract or a supersede got there first). Nothing was changed. "
             "Re-read the node and retry.",
    "refusal": "changed_concurrently",
})


# --------------------------------------------------------------------------
# D1: single definition
# --------------------------------------------------------------------------

def _constructions():
    """(file, enclosing function) for every place that BUILDS the refusal:
    a dict literal {"refusal": "changed_concurrently", ...} or a
    `_requote_refusal("changed_concurrently", ...)` call. Comments,
    docstrings and bare outcome strings are not matched."""
    found = []
    for fn in sorted(os.listdir(SRC)):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(SRC, fn), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        class V(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Dict(self, node):
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant) and k.value == "refusal"
                            and isinstance(v, ast.Constant)
                            and v.value == "changed_concurrently"):
                        found.append((fn, self.stack[-1] if self.stack else None))
                self.generic_visit(node)

            def visit_Call(self, node):
                if (node.args and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "changed_concurrently"
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "_requote_refusal"):
                    found.append((fn, self.stack[-1] if self.stack else None))
                self.generic_visit(node)

        V().visit(tree)
    return found


def test_D1_exactly_one_definition_in_engram_core():
    """Detects only the two LITERAL forms named in _constructions(); other
    ways of building the refusal would not be seen."""
    found = _constructions()
    assert found == [("engram_core.py", "changed_concurrently_refusal")], found


def test_R1_helper_rolls_back_exactly_once(env):
    import engram_core

    class _Conn:
        def __init__(self):
            self.rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    conn = _Conn()
    out = engram_core.changed_concurrently_refusal(conn, "Node", "node_fixture")
    assert conn.rollbacks == 1
    assert json.loads(out)["refusal"] == "changed_concurrently"


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

_RETRACT = dict(error_type="other", reason="test retraction",
                replacement_absent_because="NONE_BECAUSE_NO_SUCCESSOR")


@pytest.fixture
def env():
    d = tempfile.mkdtemp(prefix="engram_test_fold_4188_")
    os.environ["ENGRAM_EVIDENCE_ARCHIVE"] = os.path.join(d, "evidence-archive")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(d)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    server._test_dir = d
    yield server
    shutil.rmtree(d, ignore_errors=True)


def _call_raw(fn, **p):
    return fn(payload_json=json.dumps(p))


def _call(fn, **p):
    return json.loads(_call_raw(fn, **p))


def _obs(server, tag):
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


def _second_writer_sets_key(server, nid, key, value):
    conn = server.core._get_db()
    try:
        m = json.loads(conn.execute(
            "SELECT metadata FROM nodes WHERE id = ?", (nid,)).fetchone()["metadata"])
        m[key] = value
        conn.execute("UPDATE nodes SET metadata = ? WHERE id = ?", (json.dumps(m), nid))
        conn.commit()
    finally:
        conn.close()


def _interfere_on_metadata_write(monkeypatch, server, nid):
    """First core._metadata_dict_for_write(raw, nid, ...) call for `nid` runs a
    second writer (after the tool's read, before its guarded write)."""
    state = {"done": False}
    real = server.core._metadata_dict_for_write

    def interfering(*a, **k):
        if not state["done"] and len(a) > 1 and a[1] == nid:
            state["done"] = True
            _second_writer_sets_key(server, nid, "interfering_key", "mid-call")
        return real(*a, **k)

    monkeypatch.setattr(server.core, "_metadata_dict_for_write", interfering)
    return state


def _assert_bytes(raw, expected_template, nid):
    assert raw == expected_template.replace("<ID>", nid), raw


# --------------------------------------------------------------------------
# S*: per-site byte identity (green on the base AND after the fold)
# --------------------------------------------------------------------------

def test_S_trust_update_person(env, monkeypatch):
    server = env
    r = _call(server.engram_add_person, name="Zorblax Quentin")
    pn = r.get("person_id") or r.get("node_id") or r["id"]
    st = _interfere_on_metadata_write(monkeypatch, server, pn)
    raw = _call_raw(server.engram_update_person, node_id=pn,
                    updates={"relation": "granite surveyor"}, reason="test")
    assert st["done"]
    _assert_bytes(raw, EXPECT_NODE, pn)


def test_S_tasks_update_task(env, monkeypatch):
    server = env
    tid = _call(server.engram_add_task, description="Tidy the granite quarry ledger.")["task_id"]
    st = _interfere_on_metadata_write(monkeypatch, server, tid)
    raw = _call_raw(server.engram_update_task, task_id=tid, new_status="active", note="go")
    assert st["done"]
    _assert_bytes(raw, EXPECT_TASK, tid)


def test_S_tasks_deactivate_goal(env, monkeypatch):
    server = env
    gid = json.loads(server.engram_add_goal(json.dumps(
        {"claim": "Keep the quarry ledger honest.", "motivation": "testing"})))["goal_id"]
    st = _interfere_on_metadata_write(monkeypatch, server, gid)
    raw = _call_raw(server.engram_deactivate_goal, goal_id=gid, reason="pause")
    assert st["done"]
    _assert_bytes(raw, EXPECT_GOAL, gid)


def test_S_epistemic_update_lesson(env, monkeypatch):
    server = env
    ax = _call(server.engram_add_axiom, claim="Fixture premise a holds.",
               basis="test fixture basis")["axiom_id"]
    lid = _call(server.engram_add_lesson, claim="Lesson pattern a.", incident_ids=ax,
                logical_chain="Pattern derived from a.",
                scaffolding_nudge="Watch for this pattern.")["lesson_id"]
    st = _interfere_on_metadata_write(monkeypatch, server, lid)
    raw = _call_raw(server.engram_update_lesson, node_id=lid,
                    updates={"scaffolding_nudge": "Watch harder."})
    assert st["done"]
    _assert_bytes(raw, EXPECT_LESSON, lid)


def test_S_revision_retract(env, monkeypatch):
    server = env
    nid = _obs(server, "s1")
    state = {"done": False}
    real = server.core._pending_markers_for_node

    def interfering(conn, node_id):
        if not state["done"] and node_id == nid:
            state["done"] = True
            _second_writer_sets_key(server, nid, "interfering_key", "mid-call")
        return real(conn, node_id)

    monkeypatch.setattr(server.core, "_pending_markers_for_node", interfering)
    raw = _call_raw(server.engram_retract, node_id=nid, **_RETRACT)
    assert state["done"]
    _assert_bytes(raw, EXPECT_NODE, nid)


def test_S_observation_evidence_passthrough(env, monkeypatch):
    server = env
    _obs(server, "s4")
    conn = server.core._get_db()
    try:
        ev_id = conn.execute("SELECT id FROM nodes WHERE type = 'evidence'").fetchone()["id"]
    finally:
        conn.close()
    state = {"done": False}
    real = server.core._similar_existing_matches

    def interfering(*a, **k):
        if not state["done"]:
            state["done"] = True
            _second_writer_sets_key(server, ev_id, "interfering_key", "mid-call")
        return real(*a, **k)

    monkeypatch.setattr(server.core, "_similar_existing_matches", interfering)
    raw = _call_raw(
        server.engram_add_observation,
        url="https://example.com/s4", title="Src s4",
        quoted_text="s4 second verbatim quote text.",
        interpretation="s4 second interpretation text here.",
        claim="s4 granite quarry output fell in autumn.",
        quote_type="hard_data", evidence_type="web_page",
    )
    assert state["done"]
    _assert_bytes(raw, EXPECT_EVIDENCE, ev_id)


def test_S_observation_requote(env, monkeypatch):
    server = env
    src = os.path.join(server._test_dir, "requote_source.md")
    with open(src, "w", encoding="utf-8") as f:
        f.write("the quick brown fox jumps over the lazy dog\n")
    r = _call(server.engram_add_observation, url=f"file://{src}",
              title="Requote test source", quoted_text="the quick brown fox",
              interpretation="interp text", claim="claim text",
              quote_type="attributed_analysis")
    assert r.get("status") == "created", r
    nid = r["observation_id"]
    state = {"done": False}

    def fake(quote, claim):
        if not state["done"]:
            state["done"] = True
            inner = _call(server.engram_requote, node_id=nid,
                          new_quoted_text="the quick brown fox jumps", reason="inner fix")
            assert inner.get("status") == "requoted", inner
        return 0.5, None

    monkeypatch.setattr(server._observation_mod, "_quote_claim_similarity", fake)
    raw = _call_raw(server.engram_requote, node_id=nid,
                    new_quoted_text="the quick brown fox jumps over the lazy dog",
                    reason="outer fix")
    assert state["done"]
    _assert_bytes(raw, EXPECT_REQUOTE, nid)
