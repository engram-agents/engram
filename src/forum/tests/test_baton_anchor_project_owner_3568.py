"""#3568 F1: a project anchor carries its GitHub owner (``project/<owner>/<N>``).

Written BEFORE the implementation (contract-first). The baton prompt hook used to
default the owner of a project anchor to a team org (``BATON_GH_OWNER`` fell back to
a hardcoded name). The anchor now carries the owner instead.

Contract:
  1. ``_GITHUB_ANCHOR_RE`` accepts ``project/<N>`` and ``project/<owner>/<N>`` (owner =
     GitHub login rule, 1-39 chars, alnum or hyphen, not starting with a hyphen) and
     refuses the malformed shapes.
  2. ``_parse_github_anchor`` KEEPS ITS RETURN SHAPE for projects: ``("project", None,
     <N>)`` for BOTH forms. The owner never goes in slot 1 (that slot means
     "repo-qualified PR" to every caller that branches on the tuple).
  3. ``_project_anchor_owner`` returns the owner for the owner form, else None.
  4. Every caller that branches on the parsed tuple takes the same branch for the
     owner form as for the bare form (class sweep, one cell per caller family).
  5. CLI ``baton anchor`` stores the owner form exactly as written; a malformed owner
     form is refused with the accepted-forms text.
  6. The forum server's anchor route stores the owner form so it round-trips.

Identities and repo names are SYNTHETIC.
"""

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src/forum/tests")
# append (not insert(0)): tools/ also holds a flat forum.py that must not shadow the package.
_TOOLS = str(Path(__file__).resolve().parents[3] / "src" / "engram" / "tools")
if _TOOLS not in sys.path:
    sys.path.append(_TOOLS)
from test_baton_advance_api import World, AUTHOR, COLLEAGUE  # noqa: E402

from forum.coordination.markdown import parse_frontmatter  # noqa: E402

CFG = {"mode": "multi", "agent_name": "borges", "primary_user": "casey",
       "forum": {"url": "http://test-forum:5002"}}
OWNER = "example-org"
OWNER_39 = "a" * 39


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _gate():
    import baton_gate
    return baton_gate


# 1 -- the regex -----------------------------------------------------------
@pytest.mark.parametrize("good", [
    "project/4", f"project/{OWNER}/4", "project/a/1", f"project/{OWNER_39}/7",
    "project/a-b/12", "project/9x/3",
])
def test_regex_accepts(good):
    assert _gate()._GITHUB_ANCHOR_RE.match(good), good


@pytest.mark.parametrize("bad", [
    f"project/{OWNER}/", "project//4", "project/a/b/4", "project/-bad/4",
    f"project/{'a' * 40}/4", "project/x/" + "1" * 16, "project/", "project/x_y/4",
    "project/a.b/4", "project/x/4/", "project/x/٥",
])
def test_regex_refuses(bad):
    assert not _gate()._GITHUB_ANCHOR_RE.match(bad), bad


# 2 -- the parse tuple keeps its shape -------------------------------------
def test_parse_owner_form_equals_bare_form_and_slot1_is_none():
    g = _gate()
    assert g._parse_github_anchor(f"project/{OWNER}/4") == ("project", None, "4")
    assert g._parse_github_anchor(f"project/{OWNER}/4") == g._parse_github_anchor("project/4")


@pytest.mark.parametrize("bad", [f"project/{OWNER}/", "project//4", "project/a/b/4",
                                 "project/-bad/4", f"project/{'a' * 40}/4"])
def test_parse_malformed_owner_forms_are_none(bad):
    assert _gate()._parse_github_anchor(bad) is None


def test_accepted_forms_text_names_the_owner_form():
    g = _gate()
    assert "project/<owner>/<N>" in g.GITHUB_ANCHOR_ACCEPTED_FORMS
    assert "project/<N>" in g.GITHUB_ANCHOR_ACCEPTED_FORMS


# 3 -- the owner helper ----------------------------------------------------
def test_project_anchor_owner():
    f = _gate()._project_anchor_owner
    assert f(f"project/{OWNER}/4") == OWNER
    assert f("project/4") is None
    assert f("pr/o/r/5") is None
    assert f("pr/5") is None
    for bad in ("", None, "garbage", f"project/{OWNER}/", "project//4", "project/a/b/4",
                "project/-bad/4", f"project/{'a' * 40}/4"):
        assert f(bad) is None, bad


# 4 -- callers that branch on the parsed tuple ------------------------------
def test_caller_is_pr_shaped_same_branch_for_both_forms():
    g = _gate()
    for pid in ("PR-9001", "pool-x", "DESIGN-x"):
        assert g._is_pr_shaped(pid, f"project/{OWNER}/4") == g._is_pr_shaped(pid, "project/4")
    assert g._is_pr_shaped("PR-9001", f"project/{OWNER}/4") is False


def test_caller_binding_refusal_pr_shaped_id_refused_for_both_forms(world):
    from forum import server as S
    cfg = {"path": str(world.map_path), "admin_uids": (__import__("os").getuid(),),
           "trust_anchor": str(world.tmp_path)}
    bare = S._anchor_write_binding_refusal(world.gate, "PR-9001", True, "project/9001", cfg)
    owned = S._anchor_write_binding_refusal(world.gate, "PR-9001", True,
                                            f"project/{OWNER}/9001", cfg)
    assert bare is not None and "PR-shaped" in bare
    assert owned is not None and "PR-shaped" in owned
    assert owned == bare.replace("project/9001", f"project/{OWNER}/9001")
    # an unmapped / non-PR-shaped id is untouched for both
    assert S._anchor_write_binding_refusal(world.gate, "pool-x", False, "project/4", cfg) is None
    assert S._anchor_write_binding_refusal(world.gate, "pool-x", False,
                                           f"project/{OWNER}/4", cfg) is None


@pytest.mark.parametrize("anchor", ["project/9001", f"project/{OWNER}/9001"])
def test_caller_anchor_route_refuses_pr_shaped_id_for_both_forms(world, anchor):
    world.seed("PR-9001", phase="fairy", github=None)
    r = world.client.post("/api/projects/PR-9001/anchor",
                          json={"agent": AUTHOR, "github": anchor})
    assert r.status_code == 422, (anchor, r.get_json())
    assert "PR-shaped" in r.get_json()["error"], r.get_json()


def test_caller_reanchor_comparison_helper_same_for_both_forms(world):
    from forum import server as S
    g = world.gate
    o = f"project/{OWNER}/4"
    for new, cur in (("project/4", ""), ("project/4", "pr/4"), ("pr/example/repo/4", "project/4"),
                     ("project/4", "project/4")):
        owned_new = o if new == "project/4" else new
        owned_cur = o if cur == "project/4" else cur
        assert (S._anchor_completes(g, owned_cur, owned_new)
                == S._anchor_completes(g, cur, new)), (cur, new)
    # unqualified pr/4 completed by qualified pr; a project anchor never "completes" a PR
    assert S._anchor_completes(g, "pr/4", "pr/example/repo/4") is True
    assert S._anchor_completes(g, o, "pr/example/repo/4") is False
    assert S._anchor_completes(g, "pr/4", o) is False


def test_caller_verdict_and_off_unverified_same_for_both_forms(world):
    from forum import server as S
    o = f"project/{OWNER}/4"
    assert S._verdict_for_anchor("project/4", "project/4") is None
    assert S._verdict_for_anchor(o, o) is None
    assert S._off_unverified_pr("project/4") is False
    assert S._off_unverified_pr(o) is False


def test_caller_advance_gate_not_a_pr_baton_for_both_forms(world):
    outs = []
    for pid, anchor in (("pool-a", "project/4"), ("pool-b", f"project/{OWNER}/4")):
        world.seed(pid, phase="peer", github=anchor)
        r = world.advance(pid)
        outs.append((r.status_code, (r.get_json() or {}).get("error", "")))
    assert outs[0][0] == outs[1][0], outs
    assert "not a PR baton" in outs[0][1] and "not a PR baton" in outs[1][1], outs


def test_caller_cli_is_pr_baton_and_pr_shaped_same_for_both_forms():
    import baton as B
    for pid in ("PR-9001", "pool-x"):
        a = B._is_pr_baton(pid, {"github": f"project/{OWNER}/4"})
        b = B._is_pr_baton(pid, {"github": "project/4"})
        assert a == b is False
        assert B._is_pr_shaped(pid, f"project/{OWNER}/4") == B._is_pr_shaped(pid, "project/4")


# 5 -- CLI -----------------------------------------------------------------
def _pin_sentinel(monkeypatch):
    import baton as _baton
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
    return _baton


RAW = "---\nproject_id: pool-x\ntitle: t\nturn: borges\nstatus: planning\n---\n\nbody\n"


def test_cmd_anchor_stores_owner_form_exactly(monkeypatch):
    _baton = _pin_sentinel(monkeypatch)
    client = MagicMock()
    client.get.return_value = {"raw": RAW}
    client.post.return_value = {"seq": 1}
    _baton.cmd_anchor(argparse.Namespace(project_id="pool-x",
                                         github="project/engram-agents/4"), CFG, "borges", client)
    _, body = client.post.call_args[0]
    assert body["github"] == "project/engram-agents/4"


@pytest.mark.parametrize("bad", ["project/engram-agents/", "project//4", "project/a/b/4",
                                 "project/-bad/4"])
def test_cmd_anchor_refuses_malformed_owner_form(monkeypatch, capsys, bad):
    _baton = _pin_sentinel(monkeypatch)
    client = MagicMock()
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_anchor(argparse.Namespace(project_id="pool-x", github=bad),
                          CFG, "borges", client)
    assert ei.value.code == _baton.EXIT_VALIDATION
    client.post.assert_not_called()
    err = capsys.readouterr().err
    assert _gate().GITHUB_ANCHOR_ACCEPTED_FORMS in err, err
    assert "project/<owner>/<N>" in err


def test_cmd_init_accepts_owner_form_and_refuses_malformed(monkeypatch, capsys):
    _baton = _pin_sentinel(monkeypatch)

    def args(g):
        return argparse.Namespace(project_id="pool-x", title="t", participants="borges,ariadne",
                                  status="planning", turn=None, github=g, repo=None,
                                  colleague=None)
    client = MagicMock()
    client.post.return_value = {"seq": 1, "project_id": "pool-x"}
    _baton.cmd_init(args("project/engram-agents/4"), CFG, "borges", client)
    body = client.post.call_args[0][1]
    assert body["github"] == "project/engram-agents/4"

    client2 = MagicMock()
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_init(args("project/engram-agents/"), CFG, "borges", client2)
    assert ei.value.code == _baton.EXIT_VALIDATION
    client2.post.assert_not_called()
    assert "project/<owner>/<N>" in capsys.readouterr().err


# 6 -- server routes round-trip ---------------------------------------------
def test_server_anchor_route_stores_owner_form_and_hook_reads_same_owner(world):
    world.seed("pool-x", phase="fairy", github=None)
    r = world.client.post("/api/projects/pool-x/anchor",
                          json={"agent": AUTHOR, "github": f"project/{OWNER}/4"})
    assert r.status_code == 201, r.get_json()
    stored = parse_frontmatter(world.store.read_project("pool-x"))[0]["github"]
    # the route lowercases (GitHub logins are case-insensitive); the owner is not rewritten
    assert stored == f"project/{OWNER}/4"
    g = _gate()
    assert g._project_anchor_owner(stored) == OWNER
    assert g._parse_github_anchor(stored) == ("project", None, "4")
    # what the hook would read from the stored value
    import importlib.util
    hook_path = (Path(__file__).resolve().parents[2] / "engram" / "hooks" / "claude"
                 / "engram-baton-prompt-hook.py")
    spec = importlib.util.spec_from_file_location("baton_hook_3568", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    anchor = mod._resolve_anchor("pool-x", stored)
    assert anchor == stored
    assert mod._project_anchor_owner(anchor) == OWNER


def test_server_anchor_route_stores_as_sent_owner_segment_not_rewritten(world):
    """The /anchor route validates the lowercased form but stores the value AS SENT
    (pre-existing behaviour, identical for the bare and PR forms; the CLI lowercases
    before sending). The owner segment is neither dropped nor rewritten."""
    world.seed("pool-y", phase="fairy", github=None)
    world.seed("pool-y2", phase="fairy", github=None)
    for pid, sent in (("pool-y", "Project/Example-Org/4"), ("pool-y2", "Project/4")):
        r = world.client.post(f"/api/projects/{pid}/anchor",
                              json={"agent": AUTHOR, "github": sent})
        assert r.status_code == 201, r.get_json()
        assert parse_frontmatter(world.store.read_project(pid))[0]["github"] == sent
    stored = parse_frontmatter(world.store.read_project("pool-y"))[0]["github"]
    assert _gate()._project_anchor_owner(stored.lower()) == "example-org"


def test_server_init_route_accepts_owner_form_and_refuses_malformed(world):
    from test_baton_advance_api import SENTINEL

    def post(pid, g):
        return world.client.post("/api/projects", json={
            "agent": AUTHOR, "project_id": pid, "title": "t", "status": "in-progress",
            "turn": AUTHOR, "turn_reason": "r", "participants": [AUTHOR, COLLEAGUE, SENTINEL],
            "colleague": COLLEAGUE, "author": AUTHOR, "github": g})
    r = post("pool-z", f"project/{OWNER}/4")
    assert r.status_code == 201, r.get_json()
    assert parse_frontmatter(world.store.read_project("pool-z"))[0]["github"] == f"project/{OWNER}/4"
    r = post("pool-w", f"project/{OWNER}/")
    assert r.status_code == 422, r.get_json()
    assert world.store.read_project("pool-w") is None


def test_server_local_shape_regex_accepts_owner_form_like_the_gate():
    from forum import server as S
    g = _gate()
    for v in ("project/4", f"project/{OWNER}/4", f"project/{OWNER}/", "project//4",
              "project/a/b/4", "project/-bad/4", f"project/{'a' * 40}/4"):
        assert bool(S._ANCHOR_SHAPE_RE.fullmatch(v)) == (g._parse_github_anchor(v) is not None), v
    assert S._ACCEPTED_FORMS_FALLBACK == g.GITHUB_ANCHOR_ACCEPTED_FORMS
