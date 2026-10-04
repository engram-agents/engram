"""#3366: the SERVER refuses to create a PR-shaped baton that resolves no anchor.

Written BEFORE the implementation (contract-first). The CLI half shipped in
#3365 (``baton init`` fail-closed); this is the authority behind it: a client
that POSTs /api/projects directly (or the one-shot migration tool) used to be
able to create a PR-shaped, anchorless baton stamped ``fairy`` that nothing can
verify at the peer->merge gate (the #2916 dead end).

Contract (no bypass flag; fires at creation only; nothing stored on refusal):
  1. PR-shaped id (anchor-first ``gate._is_pr_shaped``) + no github -> 422 whose
     text names BOTH routes (``github: pr/<owner>/<repo>/<N>`` for a PR;
     ``github: project/<N>`` or a non-PR-shaped id for a bunch); nothing stored.
  2. the same id + a pr/<owner>/<repo>/<N> anchor -> 201, phase ``fairy``.
  3. the same id + ``project/<N>`` -> 201, phase ``pool`` (a bunch).
  4. non-PR-shaped ids (``DESIGN-x``, ``pool-x``) with no github -> 201 unchanged.
  5. ``UMWELT-PR-5`` with no github -> 422 (server independent of CLI derivation).
  6. odd-case shapes the classifier treats as PR (``umwelt-pr-5``, ``PR-12abc``)
     with no github -> 422; with an explicit pr/ anchor -> 201 (the server does
     not re-derive an anchor from the id).
  7. a client cannot opt out: ``transition_phase`` / any extra field does not
     bypass the refusal.

Identities and repo names are SYNTHETIC.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src/forum/tests")
# append (not insert(0)): tools/ also holds a flat forum.py that must not shadow the package.
_TOOLS = str(Path(__file__).resolve().parents[3] / "src" / "engram" / "tools")
if _TOOLS not in sys.path:
    sys.path.append(_TOOLS)
from test_baton_advance_api import World, SENTINEL, AUTHOR, COLLEAGUE, REPO  # noqa: E402

from forum.coordination.markdown import parse_frontmatter  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _post(world, pid, github=None, **extra):
    body = {
        "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
        "turn": AUTHOR, "turn_reason": "r",
        "participants": [AUTHOR, COLLEAGUE, SENTINEL],
        "colleague": COLLEAGUE, "author": AUTHOR,
    }
    if github is not None:
        body["github"] = github
    body.update(extra)
    return world.client.post("/api/projects", json=body)


def _nothing_created(world, pid):
    assert world.store.read_project(pid) is None
    assert world.client.get(f"/api/projects/{pid}").status_code == 404


def _phase(world, pid):
    return parse_frontmatter(world.store.read_project(pid))[0]["phase"]


# 1 -----------------------------------------------------------------------
def test_pr_shaped_without_anchor_is_422_naming_both_routes_and_stores_nothing(world):
    r = _post(world, "PR-autopull2")
    assert r.status_code == 422, r.get_json()
    err = r.get_json()["error"]
    assert "PR-autopull2" in err
    assert world.gate.PR_ANCHOR_TWO_ROUTE_HINT in err      # both routes, shared wording
    assert "pr/<owner>/<repo>/<N>" in err                  # route 1: it IS a PR
    assert "project/<N>" in err                            # route 2: a bunch ...
    assert "non-PR-shaped" in err                          # ... or a non-PR id
    _nothing_created(world, "PR-autopull2")


def test_refusal_leaves_the_id_free_for_a_correct_init(world):
    assert _post(world, "PR-autopull2").status_code == 422
    _nothing_created(world, "PR-autopull2")
    assert _post(world, "PR-autopull2", f"pr/{REPO}/12").status_code == 201


# 2 -----------------------------------------------------------------------
def test_pr_shaped_with_pr_anchor_is_201_fairy(world):
    r = _post(world, "PR-autopull2", f"pr/{REPO}/12")
    assert r.status_code == 201, r.get_json()
    assert _phase(world, "PR-autopull2") == "fairy"


# 3 -----------------------------------------------------------------------
def test_pr_shaped_with_project_anchor_is_201_pool_bunch(world):
    r = _post(world, "PR-autopull2", "project/7")
    assert r.status_code == 201, r.get_json()
    assert _phase(world, "PR-autopull2") == "pool"


# 4 -----------------------------------------------------------------------
@pytest.mark.parametrize("pid", ["DESIGN-x", "pool-x", "POOL-PR-3"])
def test_non_pr_shaped_without_anchor_unchanged(world, pid):
    r = _post(world, pid)
    assert r.status_code == 201, r.get_json()
    assert _phase(world, pid) == "pool"


# 5 -----------------------------------------------------------------------
def test_prefixed_pr_id_without_anchor_is_422_server_does_not_derive(world):
    r = _post(world, "UMWELT-PR-5")
    assert r.status_code == 422, r.get_json()
    _nothing_created(world, "UMWELT-PR-5")


# 6 -----------------------------------------------------------------------
@pytest.mark.parametrize("pid,num", [("umwelt-pr-5", 5), ("PR-12abc", 12)])
def test_odd_pr_shapes_without_anchor_422_with_anchor_201(world, pid, num):
    r = _post(world, pid)
    assert r.status_code == 422, r.get_json()
    _nothing_created(world, pid)
    r2 = _post(world, pid, f"pr/{REPO}/{num}")
    assert r2.status_code == 201, r2.get_json()
    assert _phase(world, pid) == "fairy"


# 7 -----------------------------------------------------------------------
@pytest.mark.parametrize("extra", [
    {"transition_phase": "fairy"}, {"transition_phase": "pool"},
    {"skip_anchor_check": True}, {"force": True}, {"phase": "fairy"},
])
def test_no_caller_settable_bypass(world, extra):
    r = _post(world, "PR-autopull2", **extra)
    assert r.status_code == 422, r.get_json()
    _nothing_created(world, "PR-autopull2")


# ---------------------------------------------------------------------------
# Reviewer round 1 (#3374)
# ---------------------------------------------------------------------------
PR_SHAPED_IDS = ["PR-autopull2", "PR-12abc", "UMWELT-PR-5", "umwelt-pr-5", "PR-5"]


# 1 BLOCKER: a non-empty but UNPARSEABLE github is "no resolved anchor" too ----
@pytest.mark.parametrize("bad", ["garbage", "pr/", "pr/o/r/", "pr/o/r", "project/", "xyz/5"])
@pytest.mark.parametrize("tp", [None, "fairy"])
@pytest.mark.parametrize("pid", PR_SHAPED_IDS)
def test_unparseable_github_on_pr_shaped_id_is_422_nothing_stored(world, pid, tp, bad):
    extra = {} if tp is None else {"transition_phase": tp}
    r = _post(world, pid, bad, **extra)
    assert r.status_code == 422, (pid, bad, tp, r.get_json())
    err = r.get_json()["error"]
    assert world.gate.PR_ANCHOR_TWO_ROUTE_HINT in err
    assert repr(bad) in err                      # names the unparseable value
    _nothing_created(world, pid)


def test_unparseable_value_echo_is_truncated(world):
    junk = "x" * 200
    r = _post(world, "PR-autopull2", junk)
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert junk not in err and "x" * 60 in err and "x" * 61 not in err
    _nothing_created(world, "PR-autopull2")


@pytest.mark.parametrize("anchor", ["PR/O/R/5", "pr/o/r/5", f"pr/{REPO}/5"])
def test_parseable_anchors_still_201(world, anchor):
    # 'PR/O/R/5' is lower-cased by the server before parsing (measured: parses).
    # an id whose prefix is unmapped, so only the NEW rule (not the repo binding) is in play
    r = _post(world, "umwelt-pr-5", anchor)
    assert r.status_code == 201, (anchor, r.get_json())
    assert _phase(world, "umwelt-pr-5") == "fairy"


def test_unqualified_pr_n_is_not_this_rules_business(world):
    # 'pr/5' parses (unqualified); the pre-existing D2 qualification path decides it.
    r = _post(world, "PR-5", "pr/5")
    assert r.status_code in (201, 422)
    if r.status_code == 422:
        assert world.gate.PR_ANCHOR_TWO_ROUTE_HINT not in r.get_json()["error"]


def test_junk_github_on_non_pr_shaped_id_keeps_current_behaviour(world):
    """PINS pre-existing behaviour (to be filed separately, NOT changed here): a
    non-PR-shaped id with an unparseable github is created as a bunch, junk stored."""
    r = _post(world, "DESIGN-x", "garbage")
    assert r.status_code == 201, r.get_json()
    assert _phase(world, "DESIGN-x") == "pool"


# 4c whitespace / null ------------------------------------------------------------
@pytest.mark.parametrize("val", ["   ", "\t", None])
@pytest.mark.parametrize("pid", PR_SHAPED_IDS)
def test_blank_or_null_github_is_422(world, pid, val):
    body = {
        "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
        "turn": AUTHOR, "turn_reason": "r", "participants": [AUTHOR, COLLEAGUE],
        "github": val}
    r = world.client.post("/api/projects", json=body)
    assert r.status_code == 422, (pid, val, r.get_json())
    _nothing_created(world, pid)


# 4d ordering: the anchor refusal comes BEFORE the transition_phase allow-list ----
@pytest.mark.parametrize("tp", ["merge", "bogus", "peer"])
def test_anchor_refusal_precedes_the_phase_allow_list(world, tp):
    r = _post(world, "PR-autopull2", transition_phase=tp)
    assert r.status_code == 422
    err = r.get_json()["error"]
    assert world.gate.PR_ANCHOR_TWO_ROUTE_HINT in err, err
    assert "allowed here" not in err
    _nothing_created(world, "PR-autopull2")


# 3 gate module unavailable: fail closed for a PR-ish id --------------------------
@pytest.fixture
def no_gate():
    from forum import server as server_mod
    saved = list(server_mod._BATON_GATE_MODULE_CACHE)
    server_mod._BATON_GATE_MODULE_CACHE[:] = [None]
    yield
    server_mod._BATON_GATE_MODULE_CACHE[:] = saved


@pytest.mark.parametrize("tp", ["fairy", "pool"])
@pytest.mark.parametrize("pid", PR_SHAPED_IDS)
@pytest.mark.parametrize("gh", [None, "garbage", "pr/"])
def test_gate_unavailable_anchorless_pr_ish_id_is_422(world, no_gate, pid, tp, gh):
    r = _post(world, pid, gh, transition_phase=tp)
    assert r.status_code == 422, (pid, tp, gh, r.get_json())
    _nothing_created(world, pid)


def test_gate_unavailable_valid_anchor_and_non_pr_ids_unaffected(world, no_gate):
    assert _post(world, "PR-5", f"pr/{REPO}/5", transition_phase="fairy").status_code == 201
    assert _post(world, "PR-6", "project/7", transition_phase="pool").status_code == 201
    assert _post(world, "DESIGN-x", transition_phase="pool").status_code == 201


# Clio's pins: the gate-unavailable text and the pool- exclusion --------------------
def test_gate_unavailable_refusal_carries_the_shared_hint_byte_for_byte(world, no_gate):
    from baton_gate import PR_ANCHOR_TWO_ROUTE_HINT as HINT  # the real constant, not world.gate
    r = _post(world, "PR-autopull2", transition_phase="fairy")
    assert r.status_code == 422, r.get_json()
    assert HINT in r.get_json()["error"]
    _nothing_created(world, "PR-autopull2")


def test_gate_unavailable_pool_prefixed_id_is_not_refused_but_pr_id_is(world, no_gate):
    r = _post(world, "pool-PR-3", transition_phase="pool")   # a bunch by the gate's own rule
    assert r.status_code == 201, r.get_json()
    r = _post(world, "PR-autopull", transition_phase="fairy")
    assert r.status_code == 422, r.get_json()


# 2 byte-identical hint (Kepler): ONE constant, both surfaces ----------------------
def test_two_route_hint_is_one_constant_in_both_surfaces(world, capsys, monkeypatch):
    tools = Path(__file__).resolve().parents[3] / "src" / "engram" / "tools"
    if str(tools) not in sys.path:
        sys.path.append(str(tools))
    import argparse
    from unittest.mock import MagicMock
    import baton as _baton
    from baton_gate import PR_ANCHOR_TWO_ROUTE_HINT as HINT

    # Deterministic pool sentinel (do NOT rely on the host's ~/.engram/config.json):
    # the same two patches tests/test_baton.py's cmd_init tests use.
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

    assert HINT and "pr/<owner>/<repo>/<N>" in HINT and "project/<N>" in HINT
    # server surface
    r = _post(world, "PR-autopull2")
    assert r.status_code == 422
    assert HINT in r.get_json()["error"]
    # CLI surface, same input: assert the REFUSAL itself fired, not merely some exit
    args = argparse.Namespace(project_id="PR-autopull2", title="t",
                              participants="borges,ariadne", status="planning",
                              turn=None, github=None, repo=None, colleague=None)
    client = MagicMock()
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_init(args, {"mode": "multi", "agent_name": "borges", "primary_user": "casey"},
                        "borges", client)
    assert ei.value.code == _baton.EXIT_VALIDATION
    client.post.assert_not_called()
    err = capsys.readouterr().err
    assert err.startswith("baton init: refusing to create 'PR-autopull2'"), err
    assert HINT in err
