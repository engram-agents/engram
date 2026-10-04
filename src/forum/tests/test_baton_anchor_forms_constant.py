"""#3390: /anchor and ``cmd_anchor`` take the accepted-forms text from the shared constant.

Written BEFORE the implementation (contract-first). #3384 put the wording of the
accepted ``github`` anchor forms in ``baton_gate.GITHUB_ANCHOR_ACCEPTED_FORMS`` and
moved ``POST /api/projects`` and ``baton init --github`` onto it. Two refusals still
carried their own hand-copied list ("Expected pr/<N>, pr/<owner>/<repo>/<N>, or
project/<N>"): the server's ``POST /api/projects/<id>/anchor`` and the CLI's
``baton anchor --github``. Wording only; what is accepted or refused is unchanged.

Contract:
  1. /anchor with a junk anchor on an existing baton -> 422 whose text carries the
     constant and NOT the old "Expected pr/" phrase; nothing is written.
  2. ``cmd_anchor`` with a junk --github -> EXIT_VALIDATION before any GET/POST;
     stderr carries the constant and NOT the old phrase.
  3. parity: all four refusal surfaces carry the same constant (one parametrised test).
  4. valid anchors are still accepted on /anchor and ``cmd_anchor`` (regression pins).

The message STARTS are unchanged: "<route> <pid>: invalid github anchor ..." and
"baton anchor: invalid --github value ...".

Environment-independent: the pool sentinel is pinned by ``World`` / monkeypatch.
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
from test_baton_advance_api import World, SENTINEL, AUTHOR, COLLEAGUE, REPO  # noqa: E402

OLD_PHRASE = "Expected pr/"
CFG = {"mode": "multi", "agent_name": "borges", "primary_user": "casey"}
RAW = "---\nproject_id: pool-x\ntitle: t\nturn: borges\nstatus: planning\n---\n\nbody\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def _forms():
    from baton_gate import GITHUB_ANCHOR_ACCEPTED_FORMS
    return GITHUB_ANCHOR_ACCEPTED_FORMS


def _pin_sentinel(monkeypatch):
    import baton as _baton
    monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
    monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
    return _baton


def _server_anchor_error(world, bad):
    world.seed("PR-9001", phase="fairy", github=None)
    r = world.client.post("/api/projects/PR-9001/anchor", json={"agent": AUTHOR, "github": bad})
    assert r.status_code == 422, r.get_json()
    return r.get_json()["error"]


def _cli_anchor_error(monkeypatch, capsys, bad):
    _baton = _pin_sentinel(monkeypatch)
    client = MagicMock()
    args = argparse.Namespace(project_id="DESIGN-x", github=bad)
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_anchor(args, CFG, "borges", client)
    assert ei.value.code == _baton.EXIT_VALIDATION
    client.post.assert_not_called()
    client.get.assert_not_called()
    return capsys.readouterr().err


def _server_init_error(world, bad):
    r = world.client.post("/api/projects", json={
        "agent": AUTHOR, "project_id": "DESIGN-x", "title": "t", "status": "in-progress",
        "turn": AUTHOR, "turn_reason": "r", "participants": [AUTHOR, COLLEAGUE, SENTINEL],
        "colleague": COLLEAGUE, "author": AUTHOR, "github": bad})
    assert r.status_code == 422, r.get_json()
    return r.get_json()["error"]


def _cli_init_error(monkeypatch, capsys, bad):
    _baton = _pin_sentinel(monkeypatch)
    client = MagicMock()
    args = argparse.Namespace(project_id="DESIGN-x", title="t", participants="borges,ariadne",
                              status="planning", turn=None, github=bad, repo=None, colleague=None)
    with pytest.raises(SystemExit) as ei:
        _baton.cmd_init(args, CFG, "borges", client)
    assert ei.value.code == _baton.EXIT_VALIDATION
    client.post.assert_not_called()
    return capsys.readouterr().err


# 1 -----------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["garbage", "pr/", "xyz/5", "project/"])
def test_anchor_route_refusal_carries_the_constant_not_the_old_list(world, bad):
    err = _server_anchor_error(world, bad)
    assert "invalid github anchor" in err, err      # message start unchanged
    assert repr(bad) in err, err
    assert _forms() in err, err
    assert OLD_PHRASE not in err, err


# 2 -----------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["garbage", "pr/", "xyz/5", "project/"])
def test_cmd_anchor_refusal_carries_the_constant_not_the_old_list(monkeypatch, capsys, bad):
    err = _cli_anchor_error(monkeypatch, capsys, bad)
    assert err.startswith(f"baton anchor: invalid --github value '{bad}'"), err
    assert _forms() in err, err
    assert OLD_PHRASE not in err, err


# 3 -----------------------------------------------------------------------
@pytest.mark.parametrize("surface", ["POST /api/projects", "/anchor", "cmd_init", "cmd_anchor"])
def test_all_four_refusal_surfaces_carry_the_same_constant(world, monkeypatch, capsys, surface):
    if surface == "POST /api/projects":
        err = _server_init_error(world, "garbage")
    elif surface == "/anchor":
        err = _server_anchor_error(world, "garbage")
    elif surface == "cmd_init":
        err = _cli_init_error(monkeypatch, capsys, "garbage")
    else:
        err = _cli_anchor_error(monkeypatch, capsys, "garbage")
    assert _forms() in err, (surface, err)
    assert OLD_PHRASE not in err, (surface, err)


# 4 regression pins --------------------------------------------------------
@pytest.mark.parametrize("good", [f"pr/{REPO}/9001", f"PR/{REPO}/9001"])
def test_anchor_route_still_accepts_a_valid_anchor(world, good):
    world.seed("PR-9001", phase="fairy", github=None)
    r = world.client.post("/api/projects/PR-9001/anchor", json={"agent": AUTHOR, "github": good})
    assert r.status_code == 201, r.get_json()      # /anchor answers 201 on a written anchor


@pytest.mark.parametrize("good", ["pr/490", "pr/o/r/22", "project/4"])
def test_cmd_anchor_still_accepts_a_valid_anchor(monkeypatch, good):
    _baton = _pin_sentinel(monkeypatch)
    client = MagicMock()
    client.get.return_value = {"raw": RAW}
    client.post.return_value = {"seq": 1}
    _baton.cmd_anchor(argparse.Namespace(project_id="pool-x", github=good), CFG, "borges", client)
    _, body = client.post.call_args[0]
    assert body["github"] == good
