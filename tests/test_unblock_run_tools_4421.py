"""Contract cells for #4421's tool surface, packaging and prose (spec C1, D, E).

Written BEFORE the implementation (contract-first; project CLAUDE.md).

* the two new MCP tools take ONE `payload_json` string parameter, reject unknown fields (a payload `session_id` among
  them: the session key comes from the server's environment, never from the agent), are listed in the capability
  index, never use the word "focus", and are NOT named in the dream master's tools list;
* the frontier docstrings no longer promise "no session state";
* the sleep skill calls `engram_cascade_end_unblock({})` BEFORE the dream master is spawned, and says why;
* the resolve-cascade skill, docs/cascade-machine.md and the changelog carry the new fields;
* the client module resolves in the deployed FLAT layout, with CLAUDE_PLUGIN_ROOT unset.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "engram"
SERVER = SRC / "server.py"
NEW_TOOLS = ("engram_cascade_start_unblock", "engram_cascade_end_unblock")


def _server_funcs() -> dict:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_the_new_tools_take_a_single_payload_json_string_parameter(name):
    fn = _server_funcs().get(name)
    assert fn is not None, f"{name} is not defined in server.py"
    args = [a.arg for a in fn.args.args]
    assert args == ["payload_json"]
    assert ast.unparse(fn.args.args[0].annotation) == "str"


def _fresh_server():
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    os.environ.setdefault("ENGRAM_NO_DB_GUARDS", "1")
    import server
    return server


def test_unknown_payload_fields_are_rejected_for_every_run_entry_point_including_session_id():
    srv = _fresh_server()
    for fn, extra in (
        (srv.engram_cascade_start_unblock, {"node_ids": ["dv_1"], "session_id": "s"}),
        (srv.engram_cascade_end_unblock, {"session_id": "s"}),
        (srv.engram_cascade_frontier, {"session_id": "s"}),
        (srv.engram_cascade_resolve, {"node_id": "dv_1", "resolution_kind": "retracted", "session_id": "s"}),
    ):
        r = json.loads(fn(json.dumps(extra)))
        assert "error" in r and "Unknown fields" in r["error"] and "session_id" in r["error"], (fn.__name__, r)


def test_the_new_tools_are_in_the_capability_index_with_a_one_line_purpose_and_no_word_focus():
    index = (SRC / "tool_capability_index.txt").read_text(encoding="utf-8")
    lines = {ln.split(":", 1)[0]: ln for ln in index.splitlines() if ln.strip()}
    for name in NEW_TOOLS:
        assert name in lines, name
        assert len(lines[name].split(":", 1)[1].strip()) > 20
        assert "focus" not in lines[name].lower(), lines[name]
        assert "focus" not in name


def test_the_new_tools_are_not_named_in_the_dream_masters_spec():
    spec = (SRC / "agents" / "claude" / "engram-dream-master.md").read_text(encoding="utf-8")
    for name in NEW_TOOLS:
        assert name not in spec


def test_the_frontier_docstrings_no_longer_promise_no_session_state():
    srv_text = SERVER.read_text(encoding="utf-8")
    rev_text = (SRC / "engram_revision.py").read_text(encoding="utf-8")
    assert "no session state" not in srv_text
    frontier_doc = ast.get_docstring(_server_funcs()["engram_cascade_frontier"])
    assert "unblock_run" in frontier_doc and "session state" in frontier_doc
    tree = ast.parse(rev_text)
    impl = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_cascade_frontier_impl")
    doc = ast.get_docstring(impl)
    assert "no session state" not in doc
    assert "unblock_run" in doc and "pure function of marker state" in doc


def test_the_sleep_skill_ends_the_run_before_the_dream_master_is_spawned_and_says_why():
    text = (SRC / "skills" / "claude" / "engram-sleep" / "SKILL.md").read_text(encoding="utf-8")
    step8 = text.split("### Step 8 — Spawn the dream master", 1)[1].split("### Step 9", 1)[0]
    call = step8.find("engram_cascade_end_unblock({})")
    spawn = step8.find('subagent_type="engram-dream-master"')
    assert call != -1 and spawn != -1 and call < spawn
    assert "stale run" in step8 and "next" in step8[call - 400: call + 600]


def test_the_resolve_cascade_skill_and_the_design_doc_carry_the_run():
    skill = (SRC / "skills" / "claude" / "engram-resolve-cascade" / "SKILL.md").read_text(encoding="utf-8")
    assert "engram_cascade_end_unblock" in skill and "unblock run" in skill.lower()
    assert "engram_cascade_start_unblock" in skill and "`done`" in skill
    doc = (REPO / "docs" / "cascade-machine.md").read_text(encoding="utf-8")
    for needle in ("unblock_run", "eligible_shapes", "next_step", "unblock-runs.json", "roots"):
        assert needle in doc, needle
    log = (REPO / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = log.split("## [Unreleased]", 1)[1].split("\n## [v", 1)[0]
    assert "4421" in unreleased and "unblock" in unreleased.lower()


def test_the_client_module_is_tiered_essential_exactly_once():
    manifest = json.loads((REPO / "src" / "build" / "packaging" / "tiers.json").read_text(encoding="utf-8"))
    entries = [e for e in manifest["shippables"] if e.get("path") == "engram_unblock_run.py"] \
        if "shippables" in manifest else []
    if not entries:
        flat = json.dumps(manifest)
        assert flat.count('"engram_unblock_run.py"') == 1
    else:
        assert len(entries) == 1 and entries[0]["tier"] == "essential"


def test_the_client_module_resolves_in_the_flattened_layout_with_plugin_root_unset(tmp_path):
    """CLAUDE.md 'Plugin build restructures code paths': the plugin root holds the runtime modules side by side."""
    flat = tmp_path / "plugin"
    flat.mkdir()
    for name in ("engram_unblock_run.py", "engramd_client.py", "engram_pidfile.py", "engram_regular_io.py",
                 "engram_home_resolver.py", "engram_signature.py"):
        shutil.copy2(SRC / name, flat / name)
    other = tmp_path / "elsewhere"
    other.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "PYTHONPATH", "ENGRAM_HOME")}
    env["CLAUDE_CODE_SESSION_ID"] = "flat-sess"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import engram_unblock_run as u; "
            "print(u.__file__); print(u.session_key())")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(flat)], cwd=other, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split()
    assert Path(lines[0]).parent == flat and lines[1] == "flat-sess"
