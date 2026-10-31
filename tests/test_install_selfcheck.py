"""Contract tests for tools/install_selfcheck.py (#4097 gap 4, slice 2).

Written before the tool. The contract (slice 2 spec, Part B):

* one line per check: ``PASS <name>`` / ``FAIL <name>: <what> -- <remedy>`` /
  ``SKIP <name>: <why>``; then ``SELFCHECK PASS n/n`` or ``SELFCHECK FAIL k/n failed``;
  exit 0 iff no FAIL; ``--json`` prints ONE object instead;
* a check that raises is a FAIL naming the exception, never a crash;
* every check has a cell where it PASSes on a good fixture and a cell where it FAILs
  on the matching broken fixture, and the FAIL line names the right check;
* the handshake starts the server the way the plugin's .mcp.json says, with
  HF_HUB_OFFLINE=1 added, tells a timeout from an exit, and does not change the
  graph (node count, edge count, seed ids).

No network, no real model, no real Claude Code: the venv python is a wrapper around
the test interpreter, the plugin tree carries a fake server/core, ``claude`` and the
MCP server are small scripts.
"""

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "install_selfcheck.py"

ALL_INSTALLED = [
    "python", "venv-imports", "model-cached", "plugin-tree", "deployed-version",
    "marketplace-registered", "plugin-enabled", "mcp-handshake",
    "graph-opens", "seed-ids-present", "claude-md-present",
]
ALL_PRE = [
    "python", "venv-imports", "model-cached", "plugin-tree", "deployed-version",
    "marketplace-registered", "plugin-enabled", "mcp-handshake",
    "seed-graph", "first-run-pending", "claude-md-rendered",
]

SEED_IDS = {
    "{{AX_HONESTY}}": "ax_0001",
    "{{AX_HONESTY_DISCRETION}}": "ax_0003",
    "{{AX_PROVENANCE}}": "ax_0002",
    "{{DF_ENGRAM}}": "df_0001",
    "{{DF_EPISTEMIC_IDENTITY}}": "df_0002",
    "{{GL_EPISTEMIC_HUMILITY}}": "gl_0001",
}
SEED_NODES = [
    ("ax_0001", "axiom"), ("ax_0002", "axiom"), ("ax_0003", "axiom"),
    ("df_0001", "definition"), ("df_0002", "definition"), ("gl_0001", "goal"),
]

FAKE_SERVER_PY = '''\
import os
def _embedding_cache_status(model_name):
    root = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    return os.path.exists(os.path.join(root, "cached-" + model_name))
'''
FAKE_CORE_PY = '''\
DEFAULT_EMBEDDING_MODEL = "fake-model"
def _get_embedding_config():
    return {"model": DEFAULT_EMBEDDING_MODEL}
'''

# A fake MCP server: reads one JSON-RPC line, records what it saw, answers by MODE.
FAKE_LAUNCH_SH = '''\
#!/usr/bin/env bash
echo "HF_HUB_OFFLINE=${HF_HUB_OFFLINE-unset}" > "$ENGRAM_HOME/handshake-env-seen"
echo "ROOT=${CLAUDE_PLUGIN_ROOT-unset}" >> "$ENGRAM_HOME/handshake-env-seen"
echo "ENGRAM_HOME=${ENGRAM_HOME-unset}" >> "$ENGRAM_HOME/handshake-env-seen"
# like the real server (server.py, before mcp.run()): leave the readiness marker naming THIS pid
echo '{"pid": '$$', "fake": true}' > "$ENGRAM_HOME/mcp-tools-ready.json"
case "$(cat "$ENGRAM_HOME/fake-server-mode")" in
  exit1)
    echo "[engram] FATAL: embedding model 'fake-model' is not cached locally" >&2
    exit 1 ;;
  hang)
    sleep 30 ;;
  garbage)
    read -r line
    echo "this is not json"
    sleep 30 ;;
  error)
    read -r line
    echo '{"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"nope"}}'
    sleep 30 ;;
  needs-offline)
    if [ "${HF_HUB_OFFLINE-}" != "1" ]; then
      echo "[engram] would download the model" >&2
      exit 1
    fi
    read -r line
    echo '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"engram","version":"0"}}}'
    sleep 30 ;;
  chatty)
    read -r line
    while true; do
      echo '{"jsonrpc":"2.0","id":99,"method":"notifications/foreign"}'
      sleep 0.1
    done ;;
  badbytes)
    printf 'bad \\377 byte\\n' >&2
    echo "[engram] FATAL: after the bad byte" >&2
    exit 1 ;;
  *)
    # a long-running CHILD (different command line from this script); its pid is recorded
    # so a test can prove the whole process group was killed, not just this shell
    sleep 30 &
    echo $! > "$ENGRAM_HOME/server-child-pid"
    read -r line
    echo '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"engram","version":"0"}}}'
    wait ;;
esac
'''


def _exe(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# Variables the tool (or the processes it starts) reads from the environment. HOME, HF_HOME, ENGRAM_HOME,
# PATH and the handshake timeout are pinned by Seat.env(); the rest are dropped so the host cannot leak in.
_TOOL_ENV_VARS = (
    "ENGRAM_HOME", "CLAUDE_HOME", "HF_HUB_OFFLINE", "ENGRAM_NO_EMBEDDINGS", "ENGRAM_NO_POLARITY",
    "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
)


class Seat:
    """A healthy fake seat under tmp_path; tests break one thing at a time."""

    def __init__(self, tmp_path, *, phase="installed"):
        self.tmp = tmp_path
        self.phase = phase
        self.home = tmp_path / "home"
        self.engram = self.home / ".engram"
        self.claude_home = self.home / ".claude"
        self.plugin = self.engram / "marketplace" / "plugins" / "engram"
        self.venv_py = self.engram / "venv" / "bin" / "python"
        self.db = self.engram / "knowledge.db"
        self.fakebin = tmp_path / "fakebin"
        self.fakemods = tmp_path / "fakemods"
        self.hf = tmp_path / "hf"
        self.market_json = tmp_path / "marketplaces.json"
        self.plugins_json = tmp_path / "plugins.json"
        self.build()

    def build(self):
        self.home.mkdir(parents=True)
        self.engram.mkdir(parents=True)
        self.claude_home.mkdir(parents=True)
        # venv python: the test interpreter, plus stub fastmcp / sentence_transformers
        self.fakemods.mkdir()
        (self.fakemods / "fastmcp.py").write_text("")
        (self.fakemods / "sentence_transformers.py").write_text("")
        self.install_venv_python(stubs=True)
        # plugin tree
        self.plugin.mkdir(parents=True)
        (self.plugin / "plugin.json").write_text(json.dumps({"name": "engram", "version": "9.9.9"}))
        (self.plugin / "server.py").write_text(FAKE_SERVER_PY)
        (self.plugin / "engram_core.py").write_text(FAKE_CORE_PY)
        _exe(self.plugin / "fake-launch.sh", FAKE_LAUNCH_SH)
        (self.plugin / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"engram": {"command": "${CLAUDE_PLUGIN_ROOT}/fake-launch.sh", "args": []}}}))
        self.set_server_mode("ok")
        # model cache (HF_HOME is what the fake predicate reads)
        self.hf.mkdir()
        (self.hf / "cached-fake-model").write_text("")
        # deployed-version, seed manifest, marker
        (self.engram / ".deployed-version").write_text("alpha_sha=abc1234\nalpha_branch=HEAD\n")
        (self.engram / "seed-manifest.json").write_text(json.dumps({"seed_ids": SEED_IDS}))
        (self.engram / "first-run-pending").write_text("{}")
        self.write_db(SEED_NODES, edges=2)
        if self.phase == "installed":
            self.write_db(SEED_NODES + [("ob_0001", "observation"), ("ob_0002", "observation")], edges=3)
        (self.claude_home / "CLAUDE.md").write_text("# Me\n{{AGENT_NAME}} works with {{USER_NAME}} on {{TODAY}}.\n")
        # fake claude
        self.fakebin.mkdir()
        self.set_claude(marketplaces=[{"name": "engram-local", "source": "directory"}],
                        plugins=[{"id": "engram@engram-local", "enabled": True}])

    def install_venv_python(self, *, stubs):
        """The seat's fake venv python, hermetic: the real interpreter run with -I -S
        (isolated, no site, PYTHONPATH ignored), so it never sees the test interpreter's
        site-packages. The only place fastmcp / sentence_transformers can come from is the
        fixture's fakemods directory (stubs=True), whichever interpreter runs pytest."""
        shim = self.tmp / "venv-shim.py"
        shim.write_text(
            "import sys\n"
            f"MODS = {str(self.fakemods) if stubs else ''!r}\n"
            "args = sys.argv[1:]\n"
            "assert args and args[0] == '-c', args\n"
            "if MODS:\n"
            "    sys.path.insert(0, MODS)\n"
            "sys.argv = ['-c'] + args[2:]\n"
            "exec(compile(args[1], '<string>', 'exec'), {'__name__': '__main__'})\n")
        _exe(self.venv_py, f'#!/usr/bin/env bash\nexec "{sys.executable}" -I -S "{shim}" "$@"\n')

    def set_server_mode(self, mode):
        (self.engram / "fake-server-mode").write_text(mode)

    def write_db(self, nodes, edges):
        if self.db.exists():
            self.db.unlink()
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, type TEXT NOT NULL)")
        con.execute("CREATE TABLE edges (source_id TEXT, target_id TEXT)")
        con.executemany("INSERT INTO nodes VALUES (?, ?)", nodes)
        con.executemany("INSERT INTO edges VALUES (?, ?)", [("a%d" % i, "b%d" % i) for i in range(edges)])
        con.commit()
        con.close()

    def set_claude(self, *, marketplaces, plugins, rc=0):
        self.market_json.write_text(json.dumps(marketplaces))
        self.plugins_json.write_text(json.dumps(plugins))
        _exe(self.fakebin / "claude",
             "#!/usr/bin/env bash\n"
             f'if [ "{rc}" != "0" ]; then echo "claude: boom" >&2; exit {rc}; fi\n'
             'if [ "$1 $2 $3 $4" = "plugin marketplace list --json" ]; then cat "%s"; exit 0; fi\n'
             'if [ "$1 $2 $3" = "plugin list --json" ]; then cat "%s"; exit 0; fi\n'
             'echo "unexpected: $*" >&2; exit 2\n' % (self.market_json, self.plugins_json))

    def remove_claude(self):
        (self.fakebin / "claude").unlink()

    def counts(self):
        con = sqlite3.connect(self.db)
        nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        ids = sorted(r[0] for r in con.execute("SELECT id FROM nodes WHERE id IN (%s)" % ",".join("?" * 6),
                                               [i for i, _ in SEED_NODES]))
        con.close()
        return nodes, edges, ids

    def env(self, **extra):
        # Drop every inherited variable install_selfcheck.py reads, so a whole-suite run (other test
        # modules set ENGRAM_NO_EMBEDDINGS at import) behaves like a lone run; cells that want one pass it.
        env = {k: v for k, v in os.environ.items() if k not in _TOOL_ENV_VARS}
        env.update({
            "HOME": str(self.home),
            "ENGRAM_HOME": str(self.engram),
            "HF_HOME": str(self.hf),
            "PATH": f"{self.fakebin}{os.pathsep}{os.environ['PATH']}",
            "INSTALL_SELFCHECK_HANDSHAKE_TIMEOUT": "3",
        })
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run(self, *args, _timeout=120, **env_extra):
        return subprocess.run(
            [sys.executable, str(TOOL), *args], env=self.env(**env_extra),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=_timeout, check=False,
        )


@pytest.fixture
def seat(tmp_path):
    return Seat(tmp_path)


@pytest.fixture
def pre_seat(tmp_path):
    return Seat(tmp_path, phase="pre-first-session")


def _lines(result):
    return result.stdout.splitlines()


def _line_for(result, name):
    hits = [ln for ln in _lines(result)
            if ln[:5] in ("PASS ", "FAIL ", "SKIP ") and ln[5:].split(":")[0] == name]
    assert len(hits) == 1, (name, result.stdout, result.stderr)
    return hits[0]


def assert_only_fails(result, *names):
    """Exactly `names` FAIL (and they carry the check name); every other check PASSes."""
    for ln in _lines(result):
        if ln.startswith("FAIL "):
            assert ln.split(":")[0].split(" ")[1] in names, ln
    for name in names:
        assert _line_for(result, name).startswith("FAIL "), result.stdout
    assert result.returncode == 1, (result.stdout, result.stderr)


# ── the good seat ────────────────────────────────────────────────────────────

def test_healthy_installed_seat_passes_every_check_in_order(seat):
    r = seat.run()
    assert r.returncode == 0, (r.stdout, r.stderr)
    lines = _lines(r)
    names = [ln.split(" ", 1)[1].split(":")[0] for ln in lines if ln.startswith("PASS ")]
    assert names == ALL_INSTALLED
    assert lines[-1] == "SELFCHECK PASS 11/11"
    assert not any(ln.startswith(("FAIL", "SKIP")) for ln in lines)


def test_healthy_pre_first_session_seat_passes_every_check_in_order(pre_seat):
    r = pre_seat.run("--phase", "pre-first-session")
    assert r.returncode == 0, (r.stdout, r.stderr)
    names = [ln.split(" ", 1)[1].split(":")[0] for ln in _lines(r) if ln.startswith("PASS ")]
    assert names == ALL_PRE
    assert _lines(r)[-1] == "SELFCHECK PASS 11/11"


def test_default_phase_is_installed(seat):
    r = seat.run()
    assert _line_for(r, "graph-opens").startswith("PASS ")
    assert "seed-graph" not in r.stdout


# ── 1 python ─────────────────────────────────────────────────────────────────

def test_python_fails_when_the_venv_python_is_missing(seat):
    seat.venv_py.unlink()
    r = seat.run()
    assert _line_for(r, "python").startswith("FAIL python:")
    assert "venv" in _line_for(r, "python")
    assert r.returncode == 1


def test_python_fails_when_the_venv_python_is_too_old(seat):
    _exe(seat.venv_py, '#!/usr/bin/env bash\necho 3.9.18\n')
    r = seat.run()
    line = _line_for(r, "python")
    assert line.startswith("FAIL python:") and "3.9.18" in line and "3.10.7" in line


# ── 2 venv-imports ───────────────────────────────────────────────────────────

def test_venv_imports_fails_when_fastmcp_is_missing(seat):
    (seat.fakemods / "fastmcp.py").unlink()
    r = seat.run()
    line = _line_for(r, "venv-imports")
    assert line.startswith("FAIL venv-imports:") and "fastmcp" in line


def test_venv_imports_fails_when_sentence_transformers_is_missing(seat):
    (seat.fakemods / "sentence_transformers.py").unlink()
    r = seat.run()
    line = _line_for(r, "venv-imports")
    assert line.startswith("FAIL venv-imports:") and "sentence_transformers" in line


def test_venv_imports_runs_in_the_venv_not_the_callers_python(seat):
    """A venv python with NO stubs, while the caller's own python can import them
    (PYTHONPATH carries the stubs): the check must still FAIL, so it ran in the venv."""
    seat.install_venv_python(stubs=False)
    # the stubs go on the CALLER's sys.path only (not into the environment the venv inherits)
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys, runpy; sys.path.insert(0, sys.argv[1]); sys.argv = [sys.argv[2]]; "
         "runpy.run_path(sys.argv[0], run_name='__main__')",
         str(seat.fakemods), str(TOOL)],
        env=seat.env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert _line_for(r, "venv-imports").startswith("FAIL venv-imports:")


# ── 3 model-cached ───────────────────────────────────────────────────────────

def test_model_cached_fails_when_the_model_is_not_in_the_cache(seat):
    (seat.hf / "cached-fake-model").unlink()
    r = seat.run()
    line = _line_for(r, "model-cached")
    assert line.startswith("FAIL model-cached:") and "fake-model" in line
    assert "SentenceTransformer" in line  # the remedy


def test_model_cached_is_evaluated_under_this_users_home_cache(seat):
    """No HF_HOME: the predicate looks under $HOME/.cache/huggingface; a marker there passes."""
    (seat.hf / "cached-fake-model").unlink()
    cache = seat.home / ".cache" / "huggingface"
    cache.mkdir(parents=True)
    (cache / "cached-fake-model").write_text("")
    env = seat.env()
    env.pop("HF_HOME")
    r = subprocess.run([sys.executable, str(TOOL)], env=env, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert _line_for(r, "model-cached").startswith("PASS ")


def test_model_cached_pass_detail_names_the_cache_directory_hf_home_hub(seat):
    line = _line_for(seat.run(), "model-cached")
    assert line.startswith("PASS ") and "fake-model" in line
    assert str(seat.hf / "hub") in line


def test_model_cached_cache_directory_honours_hf_hub_cache_over_hf_home(seat):
    hub = seat.tmp / "explicit-hub"
    line = _line_for(seat.run(HF_HUB_CACHE=hub), "model-cached")
    assert str(hub) in line and str(seat.hf / "hub") not in line


def test_model_cached_fail_message_names_the_same_cache_directory(seat):
    (seat.hf / "cached-fake-model").unlink()
    hub = seat.tmp / "explicit-hub"
    line = _line_for(seat.run(HF_HUB_CACHE=hub), "model-cached")
    assert line.startswith("FAIL model-cached:") and str(hub) in line


def test_model_cached_is_skipped_with_a_reason_when_embeddings_are_disabled(seat):
    (seat.hf / "cached-fake-model").unlink()
    r = seat.run(ENGRAM_NO_EMBEDDINGS="1")
    line = _line_for(r, "model-cached")
    assert line.startswith("SKIP model-cached:") and "ENGRAM_NO_EMBEDDINGS" in line


# ── 4 plugin-tree ────────────────────────────────────────────────────────────

def test_plugin_tree_reports_the_version(seat):
    assert "9.9.9" in _line_for(seat.run(), "plugin-tree")


def test_plugin_tree_fails_when_plugin_json_is_missing(seat):
    (seat.plugin / "plugin.json").unlink()
    r = seat.run()
    assert _line_for(r, "plugin-tree").startswith("FAIL plugin-tree:")


def test_plugin_tree_fails_when_plugin_json_does_not_parse(seat):
    (seat.plugin / "plugin.json").write_text("{not json")
    r = seat.run()
    assert _line_for(r, "plugin-tree").startswith("FAIL plugin-tree:")


# ── 5 deployed-version ───────────────────────────────────────────────────────

def test_deployed_version_fails_when_the_file_is_missing(seat):
    (seat.engram / ".deployed-version").unlink()
    assert _line_for(seat.run(), "deployed-version").startswith("FAIL deployed-version:")


def test_deployed_version_fails_when_alpha_sha_is_absent(seat):
    (seat.engram / ".deployed-version").write_text("alpha_branch=HEAD\n")
    line = _line_for(seat.run(), "deployed-version")
    assert line.startswith("FAIL deployed-version:") and "alpha_sha" in line


# ── 6 marketplace-registered ─────────────────────────────────────────────────

def test_marketplace_registered_fails_when_engram_local_is_not_listed(seat):
    seat.set_claude(marketplaces=[{"name": "other"}], plugins=[{"id": "engram@engram-local", "enabled": True}])
    line = _line_for(seat.run(), "marketplace-registered")
    assert line.startswith("FAIL marketplace-registered:") and "engram-local" in line


def test_marketplace_registered_fails_with_the_native_installer_remedy_when_claude_is_absent(seat):
    seat.remove_claude()
    env = seat.env(PATH=str(seat.fakebin))  # no claude anywhere on PATH
    r = subprocess.run([sys.executable, str(TOOL)], env=env, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    line = _line_for(r, "marketplace-registered")
    assert line.startswith("FAIL marketplace-registered:") and "claude.ai/install.sh" in line
    assert _line_for(r, "plugin-enabled").startswith("FAIL plugin-enabled:")


def test_marketplace_registered_fails_when_claude_exits_nonzero(seat):
    seat.set_claude(marketplaces=[], plugins=[], rc=3)
    assert _line_for(seat.run(), "marketplace-registered").startswith("FAIL marketplace-registered:")


# ── 7 plugin-enabled ─────────────────────────────────────────────────────────

def test_plugin_enabled_fails_when_the_plugin_is_not_installed(seat):
    seat.set_claude(marketplaces=[{"name": "engram-local"}], plugins=[{"id": "umwelt@umwelt-local", "enabled": True}])
    line = _line_for(seat.run(), "plugin-enabled")
    assert line.startswith("FAIL plugin-enabled:") and "engram@engram-local" in line


def test_plugin_enabled_fails_when_the_plugin_is_disabled(seat):
    seat.set_claude(marketplaces=[{"name": "engram-local"}], plugins=[{"id": "engram@engram-local", "enabled": False}])
    line = _line_for(seat.run(), "plugin-enabled")
    assert line.startswith("FAIL plugin-enabled:") and "enabled" in line


# ── 8 mcp-handshake ──────────────────────────────────────────────────────────

def test_handshake_passes_on_a_well_formed_result(seat):
    assert _line_for(seat.run(), "mcp-handshake").startswith("PASS ")


def test_handshake_pass_detail_names_the_cache_directory_the_child_used(seat):
    line = _line_for(seat.run(), "mcp-handshake")
    assert line.startswith("PASS ") and str(seat.hf / "hub") in line
    hub = seat.tmp / "explicit-hub"
    line = _line_for(seat.run(HF_HUB_CACHE=hub), "mcp-handshake")
    assert str(hub) in line and str(seat.hf / "hub") not in line


def test_handshake_starts_the_server_as_mcp_json_says_with_plugin_root_substituted(seat):
    seat.run()
    seen = (seat.engram / "handshake-env-seen").read_text()
    assert f"ROOT={seat.plugin}" in seen


def test_handshake_adds_hf_hub_offline_to_the_servers_environment(seat):
    seat.run()
    assert "HF_HUB_OFFLINE=1" in (seat.engram / "handshake-env-seen").read_text()


def test_handshake_under_offline_fails_loudly_for_a_server_that_would_download(seat):
    """needs-offline: the fake server only starts when HF_HUB_OFFLINE=1, so PASS here
    proves the variable reaches it; the converse (an uncached model failing) is the
    exit-1 cell below plus the real-seat measurement in the PR."""
    seat.set_server_mode("needs-offline")
    assert _line_for(seat.run(), "mcp-handshake").startswith("PASS ")


def test_handshake_fails_with_the_exit_code_and_stderr_tail_when_the_server_exits(seat):
    seat.set_server_mode("exit1")
    line = _line_for(seat.run(), "mcp-handshake")
    assert line.startswith("FAIL mcp-handshake:")
    assert "server exited rc=1" in line and "FATAL" in line
    assert "timeout" not in line


def test_handshake_fails_with_a_timeout_message_when_the_server_never_answers(seat):
    seat.set_server_mode("hang")
    line = _line_for(seat.run(INSTALL_SELFCHECK_HANDSHAKE_TIMEOUT="2"), "mcp-handshake")
    assert line.startswith("FAIL mcp-handshake:")
    assert "timeout after 2s" in line
    assert "exited" not in line


def test_handshake_timeout_default_is_sixty_seconds():
    spec = importlib.util.spec_from_file_location("install_selfcheck_mod", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.HANDSHAKE_TIMEOUT_DEFAULT == 60


@pytest.mark.parametrize("mode", ["garbage", "error"])
def test_handshake_fails_when_the_reply_is_not_a_well_formed_result(seat, mode):
    seat.set_server_mode(mode)
    line = _line_for(seat.run(), "mcp-handshake")
    assert line.startswith("FAIL mcp-handshake:")


def test_handshake_fails_when_mcp_json_is_missing(seat):
    (seat.plugin / ".mcp.json").unlink()
    assert _line_for(seat.run(), "mcp-handshake").startswith("FAIL mcp-handshake:")


def test_handshake_leaves_the_server_process_dead(seat):
    r = seat.run()
    assert _line_for(r, "mcp-handshake").startswith("PASS ")
    # the fake server sleeps 30 s after answering; the check must have killed it
    pid_probe = subprocess.run(["pgrep", "-f", str(seat.plugin / "fake-launch.sh")],
                               stdout=subprocess.PIPE, text=True, check=False)
    assert pid_probe.stdout.strip() == ""


def _pid_is_dead(pid):
    """Dead, or a zombie awaiting reaping (a killed orphan can sit as Z for a moment)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat_text = Path("/proc/%d/stat" % pid).read_text()
    except OSError:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                               text=True, check=False).stdout.strip()
        return state == "" or state.startswith("Z")
    return stat_text.rsplit(")", 1)[1].split()[0] == "Z"


def test_handshake_kills_the_whole_process_group_including_the_servers_child(seat):
    """The fake launcher backgrounds a long-running child and records its pid. Killing only
    the launcher (proc.terminate/kill) leaves that child alive; only a group kill ends it."""
    assert _line_for(seat.run(), "mcp-handshake").startswith("PASS ")
    child = int((seat.engram / "server-child-pid").read_text().strip())
    deadline = time.monotonic() + 3
    while not _pid_is_dead(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert _pid_is_dead(child), "the server's child process survived the handshake check"
    finally:
        try:
            os.kill(child, 9)
        except OSError:
            pass


def test_handshake_timeout_is_not_postponed_by_foreign_id_lines(seat):
    """A server that emits a foreign-id JSON line every 0.1 s and never answers must still
    hit the timeout: the deadline is checked before each read, not only on an idle queue."""
    seat.set_server_mode("chatty")
    started = time.monotonic()
    try:
        r = seat.run(INSTALL_SELFCHECK_HANDSHAKE_TIMEOUT="2", _timeout=40)
    except subprocess.TimeoutExpired:
        pytest.fail("the check never gave up on a server that streams foreign-id lines")
    line = _line_for(r, "mcp-handshake")
    assert line.startswith("FAIL mcp-handshake:") and "timeout after 2s" in line, line
    assert time.monotonic() - started < 2 + 12  # timeout + generous slack for the other checks


def test_handshake_survives_undecodable_bytes_on_the_servers_stderr(seat):
    """errors=replace on the pump threads: a bad byte must not silently kill the reader and
    drop the lines after it (which would hide the cause of the exit)."""
    seat.set_server_mode("badbytes")
    line = _line_for(seat.run(), "mcp-handshake")
    assert line.startswith("FAIL mcp-handshake:") and "server exited rc=1" in line
    assert "after the bad byte" in line


def test_handshake_does_not_change_node_count_edge_count_or_seed_ids(seat):
    before = seat.counts()
    r = seat.run()
    assert _line_for(r, "mcp-handshake").startswith("PASS ")
    assert seat.counts() == before


def test_handshake_passes_the_checked_home_to_the_server_as_engram_home(seat):
    """The child must see the home this run CHECKED, in the form the run resolved it. The caller's
    own ENGRAM_HOME is spelled with a `..` here, so a handshake that merely inherited the
    caller's environment (instead of setting ENGRAM_HOME itself) would hand the server the
    unresolved spelling."""
    spelled = "%s/../.engram" % seat.engram
    assert spelled != str(seat.engram)
    r = seat.run(ENGRAM_HOME=spelled)
    assert _line_for(r, "mcp-handshake").startswith("PASS "), r.stdout
    seen = (seat.engram / "handshake-env-seen").read_text().splitlines()
    assert "ENGRAM_HOME=%s" % seat.engram in seen, seen


# ── 8b the handshake never disturbs a live seat ──────────────────────────────

MARKER_BYTES = b'{"pid": %d,   "started":"2026-10-23T00:00:00Z"}\n\n  '


def _plant_marker(seat, pid):
    marker = seat.engram / "mcp-tools-ready.json"
    data = MARKER_BYTES % pid
    marker.write_bytes(data)
    return marker, data


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_handshake_skips_when_the_marker_names_a_live_server(seat):
    marker, data = _plant_marker(seat, os.getpid())  # this test process: certainly alive
    r = seat.run()
    assert _line_for(r, "mcp-handshake") == (
        "SKIP mcp-handshake: a live MCP server (pid %d) is serving this home; "
        "its own startup is the evidence" % os.getpid())
    assert not (seat.engram / "handshake-env-seen").exists(), "a second server was started"
    assert marker.read_bytes() == data
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_handshake_runs_and_restores_the_marker_byte_identical_when_its_pid_is_dead(seat):
    marker, data = _plant_marker(seat, _dead_pid())
    stamp = marker.stat().st_mtime_ns
    r = seat.run()
    assert _line_for(r, "mcp-handshake").startswith("PASS "), r.stdout
    assert (seat.engram / "handshake-env-seen").exists(), "the handshake did not run"
    assert marker.read_bytes() == data
    assert marker.stat().st_mtime_ns == stamp


def test_handshake_leaves_no_marker_behind_when_there_was_none(seat):
    marker = seat.engram / "mcp-tools-ready.json"
    assert not marker.exists()
    r = seat.run()
    assert _line_for(r, "mcp-handshake").startswith("PASS "), r.stdout
    assert (seat.engram / "handshake-env-seen").exists(), "the handshake did not run"
    assert not marker.exists()


def test_handshake_restores_an_unparseable_marker_too(seat):
    marker = seat.engram / "mcp-tools-ready.json"
    marker.write_bytes(b"not json at all \xff\n")
    r = seat.run()
    assert _line_for(r, "mcp-handshake").startswith("PASS "), r.stdout
    assert marker.read_bytes() == b"not json at all \xff\n"


def test_handshake_restores_the_marker_when_the_server_fails_too(seat):
    marker, data = _plant_marker(seat, _dead_pid())
    seat.set_server_mode("exit1")
    r = seat.run()
    assert _line_for(r, "mcp-handshake").startswith("FAIL mcp-handshake:"), r.stdout
    assert marker.read_bytes() == data


# ── 9 phase-dependent graph / identity checks ────────────────────────────────

# installed phase

def test_graph_opens_fails_when_knowledge_db_is_missing(seat):
    seat.db.unlink()
    assert _line_for(seat.run(), "graph-opens").startswith("FAIL graph-opens:")


def test_graph_opens_fails_when_the_graph_has_fewer_than_six_nodes(seat):
    seat.write_db(SEED_NODES[:5], edges=2)
    line = _line_for(seat.run(), "graph-opens")
    assert line.startswith("FAIL graph-opens:") and "5" in line


def test_graph_opens_fails_when_the_file_is_not_a_database(seat):
    seat.db.write_text("this is not sqlite")
    assert _line_for(seat.run(), "graph-opens").startswith("FAIL graph-opens:")


def test_graph_is_opened_read_only(seat):
    """A read-only directory and file must not be touched: mtime and size unchanged."""
    before = (seat.db.stat().st_mtime_ns, seat.db.stat().st_size)
    r = seat.run()
    assert _line_for(r, "graph-opens").startswith("PASS ")  # the check really ran
    assert (seat.db.stat().st_mtime_ns, seat.db.stat().st_size) == before
    assert not list(seat.engram.glob("knowledge.db-*"))


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_graph_connection_cannot_write(seat):
    """Not only 'the file is unchanged after a run': the connection the checks hold must be
    unable to write at all (mode=ro), so a future check cannot alter the user's graph."""
    mod = _load_tool("install_selfcheck_mod_ro")
    ctx = mod.Ctx("installed", seat.home, seat.engram, seat.claude_home, 3)
    con = mod._open_ro(ctx)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("INSERT INTO nodes VALUES ('zz_0001', 'observation')")
    finally:
        con.close()
    assert seat.counts()[0] == len(SEED_NODES) + 2


def test_graph_checks_pass_on_a_read_only_file_in_a_read_only_directory(seat):
    seat.db.chmod(0o444)
    seat.engram.chmod(0o555)
    try:
        r = seat.run()
    finally:
        seat.engram.chmod(0o755)
        seat.db.chmod(0o644)
    for name in ("graph-opens", "seed-ids-present"):
        assert _line_for(r, name).startswith("PASS "), r.stdout


def test_seed_ids_present_fails_when_a_seed_node_is_gone(seat):
    seat.write_db([n for n in SEED_NODES if n[0] != "df_0002"] + [("ob_1", "observation"), ("ob_2", "observation")], edges=2)
    line = _line_for(seat.run(), "seed-ids-present")
    assert line.startswith("FAIL seed-ids-present:") and "df_0002" in line


def test_seed_ids_present_fails_when_the_manifest_is_missing(seat):
    (seat.engram / "seed-manifest.json").unlink()
    assert _line_for(seat.run(), "seed-ids-present").startswith("FAIL seed-ids-present:")


def test_seed_ids_present_fails_when_the_manifest_lists_no_ids(seat):
    (seat.engram / "seed-manifest.json").write_text(json.dumps({"seed_ids": {}}))
    assert _line_for(seat.run(), "seed-ids-present").startswith("FAIL seed-ids-present:")


def test_claude_md_present_fails_when_the_file_is_missing(seat):
    (seat.claude_home / "CLAUDE.md").unlink()
    assert _line_for(seat.run(), "claude-md-present").startswith("FAIL claude-md-present:")


def test_claude_md_present_fails_on_an_unfilled_install_time_placeholder(seat):
    (seat.claude_home / "CLAUDE.md").write_text("see {{AX_HONESTY}} and {{ENGRAM_HOME}}\n")
    line = _line_for(seat.run(), "claude-md-present")
    assert line.startswith("FAIL claude-md-present:") and "{{AX_HONESTY}}" in line


def test_claude_md_present_passes_when_first_session_tokens_remain_or_are_filled(seat):
    (seat.claude_home / "CLAUDE.md").write_text("Quill and {{COUNTERPART_NAME}} {{SELF_NODE_ID}}\n")
    assert _line_for(seat.run(), "claude-md-present").startswith("PASS ")


# pre-first-session phase

def test_seed_graph_fails_on_a_wrong_axiom_count(pre_seat):
    pre_seat.write_db([n for n in SEED_NODES if n[0] != "ax_0003"], edges=2)
    line = _line_for(pre_seat.run("--phase", "pre-first-session"), "seed-graph")
    assert line.startswith("FAIL seed-graph:") and "axiom" in line


def test_seed_graph_fails_on_a_wrong_edge_count(pre_seat):
    pre_seat.write_db(SEED_NODES, edges=3)
    line = _line_for(pre_seat.run("--phase", "pre-first-session"), "seed-graph")
    assert line.startswith("FAIL seed-graph:") and "edge" in line


def test_seed_graph_fails_on_an_empty_database(pre_seat):
    pre_seat.write_db([], edges=0)
    assert _line_for(pre_seat.run("--phase", "pre-first-session"), "seed-graph").startswith("FAIL seed-graph:")


def test_first_run_pending_fails_when_the_marker_is_gone(pre_seat):
    (pre_seat.engram / "first-run-pending").unlink()
    line = _line_for(pre_seat.run("--phase", "pre-first-session"), "first-run-pending")
    assert line.startswith("FAIL first-run-pending:")


def test_claude_md_rendered_fails_on_a_token_outside_the_first_session_set(pre_seat):
    (pre_seat.claude_home / "CLAUDE.md").write_text("{{AGENT_NAME}} {{PYTHON}}\n")
    line = _line_for(pre_seat.run("--phase", "pre-first-session"), "claude-md-rendered")
    assert line.startswith("FAIL claude-md-rendered:") and "{{PYTHON}}" in line


def test_claude_md_rendered_fails_when_the_file_is_missing(pre_seat):
    (pre_seat.claude_home / "CLAUDE.md").unlink()
    assert _line_for(pre_seat.run("--phase", "pre-first-session"), "claude-md-rendered").startswith("FAIL ")


def test_pre_first_session_checks_fail_on_a_healthy_installed_seat_which_is_why_phase_exists(seat):
    """The reason for two phases: the pre-first-session checks read FAIL on a working seat
    after its first session (marker gone, more than six nodes)."""
    (seat.engram / "first-run-pending").unlink()
    r = seat.run("--phase", "pre-first-session")
    assert _line_for(r, "first-run-pending").startswith("FAIL ")
    assert _line_for(r, "seed-graph").startswith("FAIL ")
    assert seat.run().returncode == 0  # while the default phase passes


# ── runner contract ──────────────────────────────────────────────────────────

def test_summary_counts_failures_and_exit_code_is_one(seat):
    (seat.engram / ".deployed-version").unlink()
    seat.set_claude(marketplaces=[], plugins=[])
    r = seat.run()
    assert _lines(r)[-1] == "SELFCHECK FAIL 3/11 failed"
    assert_only_fails(r, "deployed-version", "marketplace-registered", "plugin-enabled")


def test_one_failing_check_still_fails_the_run(seat):
    (seat.engram / ".deployed-version").unlink()
    r = seat.run()
    assert r.returncode == 1
    assert _lines(r)[-1] == "SELFCHECK FAIL 1/11 failed"


def test_skip_is_not_counted_in_the_denominator(seat):
    r = seat.run(ENGRAM_NO_EMBEDDINGS="1")
    assert _lines(r)[-1] == "SELFCHECK PASS 10/10"
    assert r.returncode == 0


def test_every_fail_line_has_what_and_remedy(seat):
    (seat.engram / ".deployed-version").unlink()
    line = _line_for(seat.run(), "deployed-version")
    body = line.split(":", 1)[1]
    assert " — " in body or " -- " in body


def test_a_check_that_raises_is_reported_as_fail_naming_the_exception_not_a_crash(seat):
    spec = importlib.util.spec_from_file_location("install_selfcheck_mod2", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def boom(ctx):
        raise RuntimeError("kaboom")

    results = mod.run_checks([("exploding", boom), ("fine", lambda ctx: ("PASS", "ok"))], ctx=None)
    by_name = {r["name"]: r for r in results}
    assert by_name["exploding"]["status"] == "FAIL"
    assert "RuntimeError" in by_name["exploding"]["detail"] and "kaboom" in by_name["exploding"]["detail"]
    assert by_name["fine"]["status"] == "PASS"


def test_relative_engram_home_is_refused(seat):
    r = seat.run(ENGRAM_HOME="relative/path")
    assert r.returncode == 1
    assert "FAIL engram-home:" in r.stdout and "absolute" in r.stdout


def test_json_output_is_one_object_with_name_status_detail(seat):
    (seat.engram / ".deployed-version").unlink()
    r = seat.run("--json")
    assert r.returncode == 1
    doc = json.loads(r.stdout)  # ONE object, nothing else on stdout
    assert doc["phase"] == "installed"
    assert doc["result"] == "FAIL"
    assert [c["name"] for c in doc["checks"]] == ALL_INSTALLED
    for c in doc["checks"]:
        # the three graph checks also carry the additive "path" field (#4118); no other check does
        assert set(c) - {"path"} == {"name", "status", "detail"}
        assert ("path" in c) == (c["name"] in ("graph-opens", "seed-ids-present"))
        assert c["status"] in ("PASS", "FAIL", "SKIP")
    bad = [c for c in doc["checks"] if c["status"] == "FAIL"]
    assert [c["name"] for c in bad] == ["deployed-version"]


def test_json_output_on_a_healthy_seat_exits_zero(seat):
    r = seat.run("--json")
    assert r.returncode == 0
    assert json.loads(r.stdout)["result"] == "PASS"


def test_unknown_phase_is_rejected(seat):
    r = seat.run("--phase", "bogus")
    assert r.returncode == 2
    assert "invalid choice" in r.stderr
