"""Contract-first tests for the "forum configured" gate of the three CLI
helpers (part of #3552): ``ia.py``, ``baton.py`` and ``engram_client.py``.

The contract (G1-G5), the same one #3550 gave the forum/baton/inter-agent
prompt hooks:

  G1  "forum configured" means config["forum"] is a dict whose "url" is a
      non-empty string after .strip(). agent_name alone is identity, NOT forum
      configuration. Anything else (non-dict config, missing/non-dict forum,
      non-string/blank url, agent_name alone) is NOT configured, and the gate
      never raises.
  G2  The user-facing refusals name ``forum.url`` only (exact text pinned).
  G5  Real-subprocess behaviour: agent_name alone -> refused with that text;
      forum.url set (no agent_name) -> past the gate.

Cell classes (the dev-code red/green record is in the PR handoff):
  * (a) gate unit cells over the G1 table, for each of the three gates
  * (b) exact refusal text, real subprocesses
  * (c) forum.url-only configs get past the refusal, real subprocesses
  * (d) engram_client.is_forum_configured() reads through the module's own
        loader (ENGRAM_HOME), the way test_mode_gate_helpers.py does
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = PROJECT_ROOT / "src" / "engram" / "tools"
IA_PATH = TOOLS_DIR / "ia.py"
BATON_PATH = TOOLS_DIR / "baton.py"

# append (not insert(0)): src/engram/tools also holds forum.py; see the note in
# test_ia_cli.py (#2037/#2063).
for _p in (str(PROJECT_ROOT / "src" / "engram"), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.append(_p)

import baton as _baton  # noqa: E402
import engram_client as _client  # noqa: E402

_spec = importlib.util.spec_from_file_location("ia_mod_3552", str(IA_PATH))
_ia = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ia)

GATES = ["ia", "baton", "client"]


@pytest.fixture(params=GATES)
def gate_name(request):
    return request.param


def _gate(name, cfg, tmp_path, monkeypatch):
    """Evaluate gate ``name`` on ``cfg``; the client gate reads config.json."""
    if name == "ia":
        return _ia._is_forum_configured(cfg)
    if name == "baton":
        return _baton._is_forum_configured(cfg)
    home = tmp_path / "client_home"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("ENGRAM_HOME", str(home))
    return _client.is_forum_configured()


# ---------------------------------------------------------------------------
# (a) unit cells over the G1 table, for each gate
# ---------------------------------------------------------------------------

URL = "http://localhost:5002"

CONFIGURED = [
    {"forum": {"url": URL}},
    {"forum": {"url": "  " + URL + "  "}},
    {"agent_name": "x", "forum": {"url": URL}},
    {"agent_name": "", "forum": {"url": URL}},
    {"mode": "single", "forum": {"url": URL}},
]
UNCONFIGURED = [
    {}, {"agent_name": "x"}, {"agent_name": "x", "forum": {}},
    {"forum": {"url": ""}}, {"forum": {"url": "   "}},
    {"agent_name": "x", "forum": {"url": ""}},
    {"agent_name": "x", "forum": {"url": "   "}},
    {"mode": "multi"}, {"agent_name": 5}, {"agent_name": None},
]
# `forum` / `url` of the wrong type, with agent_name set (the old rule said
# True) and without it (the old rule raised on a non-string url).
WRONG_TYPE = (
    [{"agent_name": "x", "forum": v}
     for v in ("http://x", None, [], 5)]
    + [{"agent_name": "x", "forum": {"url": v}}
       for v in (5, None, ["http://x"], {}, True)]
    + [{"forum": {"url": v}} for v in (5, None, ["http://x"], {}, True)]
    + [{"forum": v} for v in ("http://x", None, [], 5)]
)


@pytest.mark.parametrize("cfg", CONFIGURED, ids=[json.dumps(c) for c in CONFIGURED])
def test_a_gate_true_over_g1_table(gate_name, cfg, tmp_path, monkeypatch):
    assert _gate(gate_name, cfg, tmp_path, monkeypatch) is True


@pytest.mark.parametrize("cfg", UNCONFIGURED, ids=[json.dumps(c) for c in UNCONFIGURED])
def test_a_gate_false_over_g1_table(gate_name, cfg, tmp_path, monkeypatch):
    assert _gate(gate_name, cfg, tmp_path, monkeypatch) is False


@pytest.mark.parametrize("cfg", WRONG_TYPE, ids=[json.dumps(c) for c in WRONG_TYPE])
def test_a_gate_total_over_wrong_types(gate_name, cfg, tmp_path, monkeypatch):
    assert _gate(gate_name, cfg, tmp_path, monkeypatch) is False


@pytest.mark.parametrize("cfg", [[], ["forum"], "forum", 5, 1.5, True],
                         ids=["list-empty", "list", "str", "int", "float", "bool"])
def test_a_gate_total_over_non_dict_config(gate_name, cfg, tmp_path, monkeypatch):
    """A config that is not a dict is not configured and never raises. (None is
    excluded for ia/baton: None means "load config.json" there; the client gate
    gets the JSON `null` through its loader below.)"""
    if gate_name == "client":
        pytest.skip("covered by test_d_client_non_dict_config_file")
    assert _gate(gate_name, cfg, tmp_path, monkeypatch) is False


@pytest.mark.parametrize("text", ["null", "[]", '"forum"', "5", "true"])
def test_d_client_non_dict_config_file(text, tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(text)
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    assert _client.is_forum_configured() is False


def test_d_client_reads_through_its_own_loader(tmp_path, monkeypatch):
    """No config argument: the gate reads $ENGRAM_HOME/config.json itself."""
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path))
    assert _client.is_forum_configured() is False  # no file
    (tmp_path / "config.json").write_text(json.dumps({"agent_name": "x"}))
    assert _client.is_forum_configured() is False
    (tmp_path / "config.json").write_text(json.dumps({"forum": {"url": URL}}))
    assert _client.is_forum_configured() is True
    (tmp_path / "config.json").write_text("{ not json")
    assert _client.is_forum_configured() is False  # unreadable: as today


def test_a_ia_and_baton_load_config_when_none(tmp_path, monkeypatch):
    """config=None -> the module's own loader (ia/baton), same rule."""
    (tmp_path / "config.json").write_text(json.dumps({"agent_name": "x"}))
    monkeypatch.setattr(_ia, "ENGRAM_HOME", str(tmp_path))
    monkeypatch.setattr(_baton, "ENGRAM_HOME", str(tmp_path))
    assert _ia._is_forum_configured() is False
    assert _baton._is_forum_configured() is False
    (tmp_path / "config.json").write_text(json.dumps({"forum": {"url": URL}}))
    assert _ia._is_forum_configured() is True
    assert _baton._is_forum_configured() is True


# ---------------------------------------------------------------------------
# Subprocess helpers (the shape users run)
# ---------------------------------------------------------------------------

def _dead_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


_DEAD = _dead_url()


def _home(tmp_path: Path, cfg) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if isinstance(cfg, str):
        (home / "config.json").write_text(cfg)
    elif cfg is not None:
        (home / "config.json").write_text(json.dumps(cfg))
    return home


def _run(path: Path, home: Path, *args: str):
    env = {k: v for k, v in os.environ.items()
           if k not in ("FORUM_URL", "CLAUDE_PLUGIN_ROOT", "ENGRAM_DATA_DIR")}
    env["ENGRAM_HOME"] = str(home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{PROJECT_ROOT / 'src' / 'engram'}:{PROJECT_ROOT / 'src'}"
    # Hermetic: a developer host may run a real forum on the default
    # localhost:5002; the env fallback points at a dead port instead.
    env["FORUM_URL"] = _DEAD
    return subprocess.run([sys.executable, str(path), *args], input="",
                          capture_output=True, text=True, env=env, timeout=60)


IA_REFUSAL_DETAIL = "(no forum.url set in config.json)"
IA_REFUSAL_LINES = [
    f"ia: no forum configured {IA_REFUSAL_DETAIL}; ia requires a configured forum.",
    "To enable: set forum.url in config.json and restart MCP.",
    'See: the engram-letter skill, troubleshooting table, row "ia exits 3 with no forum configured".',
]
BATON_REFUSAL_LINE = (
    "baton: no forum configured (set forum.url in config.json); "
    "baton requires a configured forum."
)

# `ia` DM commands (all gated by _check_forum_configured).
IA_DM = [("list",), ("read", "borges"), ("mark-read",), ("cursor", "--show"),
         ("peers",), ("write", "--to", "borges", "--from-stdin")]
# `baton` commands that go through _require_forum (write commands).
BATON_WRITE = [
    ("init", "PR-1", "--title", "t", "--participants", "a,b", "--turn", "a"),
    ("flip", "PR-1", "b", "why"), ("advance", "PR-1"), ("claim", "pool-x"),
    ("release", "pool-x"), ("close", "--status", "merged", "PR-1"), ("reopen", "PR-1"),
    ("gc",), ("rename", "--title", "t", "PR-1"), ("anchor", "PR-1", "--github", "pr/1"),
    ("add-participant", "PR-1", "c"), ("set-colleague", "PR-1", "c"),
    ("merge", "PR-1"),
]


# ---------------------------------------------------------------------------
# (b) exact refusal text, real subprocesses (G2, G5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", IA_DM, ids=[a[0] for a in IA_DM])
def test_b_ia_agent_name_alone_refuses_with_exact_text(args, tmp_path):
    r = _run(IA_PATH, _home(tmp_path, {"agent_name": "x"}), *args)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert r.stderr.splitlines() == IA_REFUSAL_LINES, r.stderr


@pytest.mark.parametrize("cfg", [{}, {"agent_name": "x", "forum": {}},
                                 {"forum": {"url": "  "}}],
                         ids=["empty", "forum-empty", "url-blank"])
def test_b_ia_other_unconfigured_shapes_same_text(cfg, tmp_path):
    r = _run(IA_PATH, _home(tmp_path, cfg), "list")
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert r.stderr.splitlines() == IA_REFUSAL_LINES, r.stderr


def test_b_ia_missing_and_invalid_details_unchanged(tmp_path):
    r = _run(IA_PATH, _home(tmp_path, None), "list")
    assert r.returncode == 3
    assert r.stderr.splitlines()[0] == (
        "ia: no forum configured (config.json missing); ia requires a configured forum.")
    (tmp_path / "bad").mkdir()
    r = _run(IA_PATH, _home(tmp_path / "bad", "{not valid json"), "list")
    assert r.returncode == 3
    assert r.stderr.splitlines()[0] == (
        "ia: no forum configured (config.json invalid — failed to parse); "
        "ia requires a configured forum.")
    assert r.stderr.splitlines()[1] == "To enable: set forum.url in config.json and restart MCP."


@pytest.mark.parametrize("args", BATON_WRITE, ids=[a[0] for a in BATON_WRITE])
def test_b_baton_agent_name_alone_refuses_with_exact_text(args, tmp_path):
    r = _run(BATON_PATH, _home(tmp_path, {"agent_name": "x"}), *args)
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert r.stderr.splitlines() == [BATON_REFUSAL_LINE], r.stderr


@pytest.mark.parametrize("args", [("status",), ("show", "PR-1")], ids=["status", "show"])
def test_b_baton_read_commands_stay_silent_exit_zero(args, tmp_path):
    r = _run(BATON_PATH, _home(tmp_path, {"agent_name": "x"}), *args)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), (r.stdout, r.stderr)


def test_b_baton_invalid_config_still_not_configured(tmp_path):
    r = _run(BATON_PATH, _home(tmp_path, "{not valid json"), "claim", "pool-x")
    assert r.returncode == 3
    assert r.stderr.splitlines() == [BATON_REFUSAL_LINE]


# ---------------------------------------------------------------------------
# (c) forum.url alone (no agent_name) gets past the refusal (G5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [("list",), ("peers",)], ids=["list", "peers"])
def test_c_ia_url_only_proceeds_past_gate(args, tmp_path):
    r = _run(IA_PATH, _home(tmp_path, {"forum": {"url": _DEAD}}), *args)
    assert "no forum configured" not in r.stderr, r.stderr
    assert "forum.url" not in r.stderr, r.stderr
    assert r.returncode != 3, (r.stdout, r.stderr)


@pytest.mark.parametrize("args", [("claim", "pool-x"), ("flip", "PR-1", "b", "why"),
                                  ("close", "--status", "merged", "PR-1")],
                         ids=["claim", "flip", "close"])
def test_c_baton_url_only_proceeds_past_gate(args, tmp_path):
    r = _run(BATON_PATH, _home(tmp_path, {"forum": {"url": _DEAD}}), *args)
    assert "no forum configured" not in r.stderr, r.stderr
    assert "usage:" not in r.stderr, r.stderr
    assert r.returncode == 2, (r.stdout, r.stderr)  # EXIT_IO: connect failure


def test_c_baton_url_only_reaches_the_network(tmp_path):
    """Stronger than 'no refusal text': the command actually tried the dead URL."""
    r = _run(BATON_PATH, _home(tmp_path, {"forum": {"url": _DEAD}}), "claim", "pool-x")
    assert "no forum configured" not in r.stderr
    assert r.returncode == 2, (r.stdout, r.stderr)  # EXIT_IO: connect failure


# ---------------------------------------------------------------------------
# G2 text, direct (no subprocess): the helper functions' own messages
# ---------------------------------------------------------------------------

def test_b_baton_require_forum_text_direct(capsys):
    with pytest.raises(SystemExit) as e:
        _baton._require_forum({"agent_name": "x"})
    assert e.value.code == 3
    assert capsys.readouterr().err.splitlines() == [BATON_REFUSAL_LINE]


def test_b_ia_check_forum_configured_text_direct(capsys, tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"agent_name": "x"}))
    monkeypatch.setattr(_ia, "ENGRAM_HOME", str(tmp_path))
    with pytest.raises(SystemExit) as e:
        _ia._check_forum_configured({"agent_name": "x"})
    assert e.value.code == 3
    assert capsys.readouterr().err.splitlines() == IA_REFUSAL_LINES
