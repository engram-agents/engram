"""#3568 F1: the baton prompt hook takes a project anchor's GitHub owner from the anchor.

Written BEFORE the implementation (contract-first). Owner resolution for a project
anchor: owner in the anchor -> else ``BATON_GH_OWNER`` if set and non-empty -> else NO
``gh`` call and one short line saying the anchor has no owner. There is no hardcoded
team default.

Cells: (a) owner-in-anchor beats env, (b) bare + env -> env owner, (c) bare + no env ->
no gh call + notice line, (d) source pin: no hardcoded org literal in the hook,
(e) the mirrored project regex follows the same owner rule as baton_gate.
Identities are SYNTHETIC.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_baton_hook import (  # noqa: E402
    HOOK_PATH, _call_main, _load_hook_module, _make_project_dict, _parse_ctx, _write_config,
)

OWNER = "engram-agents"
GH_JSON = json.dumps({"items": [{"status": "Done"}, {"status": "Done"}, {"status": "Todo"}]})


@pytest.fixture
def engram_home(tmp_path):
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


def _mod(engram_home, monkeypatch, env_owner=None):
    monkeypatch.delenv("BATON_GH_OWNER", raising=False)
    if env_owner is not None:
        monkeypatch.setenv("BATON_GH_OWNER", env_owner)
    _write_config(engram_home)
    return _load_hook_module(engram_home)


def _run(mod, anchor):
    """Render main() for one in-court project-anchored baton; return (ctx, gh argvs)."""
    projects = [_make_project_dict("pool-x", turn="borges", status="in-progress", github=anchor)]
    with patch.object(mod.shutil, "which", return_value="/usr/bin/gh"):
        with patch.object(mod, "subprocess") as sub:
            sub.run.return_value = type("CP", (), {"returncode": 0, "stdout": GH_JSON,
                                                   "stderr": ""})()
            stdout, _, _ = _call_main(mod, projects=projects, gh_ok=True)
    gh_calls = [c.args[0] for c in sub.run.call_args_list if c.args and c.args[0][:1] == ["gh"]]
    return _parse_ctx(stdout), gh_calls


def test_a_owner_in_anchor_wins_over_env(engram_home, monkeypatch):
    mod = _mod(engram_home, monkeypatch, env_owner="other")
    ctx, calls = _run(mod, f"project/{OWNER}/4")
    assert len(calls) == 1, calls
    argv = calls[0]
    assert argv[argv.index("--owner") + 1] == OWNER, argv
    assert argv[:4] == ["gh", "project", "item-list", "4"], argv
    assert "2 Done" in ctx, ctx


def test_b_bare_anchor_uses_env_owner(engram_home, monkeypatch):
    mod = _mod(engram_home, monkeypatch, env_owner="org2")
    ctx, calls = _run(mod, "project/4")
    assert len(calls) == 1, calls
    argv = calls[0]
    assert argv[argv.index("--owner") + 1] == "org2", argv
    assert "2 Done" in ctx, ctx


def test_c_bare_anchor_no_env_makes_no_gh_call_and_says_so(engram_home, monkeypatch):
    mod = _mod(engram_home, monkeypatch, env_owner=None)
    ctx, calls = _run(mod, "project/4")
    assert calls == [], calls
    assert "project anchor has no owner" in ctx, ctx
    assert "project/<owner>/<N>" in ctx, ctx


def test_c2_empty_env_counts_as_unset(engram_home, monkeypatch):
    mod = _mod(engram_home, monkeypatch, env_owner="")
    ctx, calls = _run(mod, "project/4")
    assert calls == [], calls
    assert "project anchor has no owner" in ctx, ctx


@pytest.mark.parametrize("blank", ["   ", "\t", " \t \n"], ids=["spaces", "tab", "mixed"])
def test_c2b_whitespace_only_env_counts_as_unset(engram_home, monkeypatch, blank):
    """A whitespace-only $BATON_GH_OWNER is unset too: no gh call (an `--owner "   "`
    would be a bogus query), and the same notice line."""
    mod = _mod(engram_home, monkeypatch, env_owner=blank)
    ctx, calls = _run(mod, "project/4")
    assert calls == [], calls
    assert "project anchor has no owner" in ctx, ctx


def test_c2c_env_owner_is_stripped_before_use(engram_home, monkeypatch):
    mod = _mod(engram_home, monkeypatch, env_owner="  org2 \n")
    ctx, calls = _run(mod, "project/4")
    assert len(calls) == 1, calls
    argv = calls[0]
    assert argv[argv.index("--owner") + 1] == "org2", argv


def test_c3_no_owner_notice_is_not_cached_as_a_status(engram_home, monkeypatch):
    mod = _mod(engram_home, monkeypatch, env_owner=None)
    cache = {}
    assert "no owner" in mod._get_live_status("project/4", cache)
    assert cache == {}, cache


def test_d_source_has_no_hardcoded_org_default():
    text = HOOK_PATH.read_text(encoding="utf-8")
    assert '"engram-agents"' not in text
    assert "'engram-agents'" not in text


@pytest.mark.parametrize("anchor,owner", [
    ("project/4", None), (f"project/{OWNER}/4", OWNER), ("project/a/1", "a"),
    ("project/" + "a" * 39 + "/9", "a" * 39),
    ("pr/o/r/5", None), ("project/engram-agents/", None), ("project//4", None),
    ("project/a/b/4", None), ("project/-bad/4", None), (f"project/{'a' * 40}/4", None),
    ("project/x/" + "1" * 16, None),
])
def test_e_mirrored_owner_parse_matches_gate(engram_home, monkeypatch, anchor, owner):
    mod = _mod(engram_home, monkeypatch)
    sys.path.append(str(Path(__file__).resolve().parents[1] / "src" / "engram" / "tools"))
    import baton_gate
    assert mod._project_anchor_owner(anchor) == owner
    assert baton_gate._project_anchor_owner(anchor) == owner
