"""#4255 slice 2: the runtime config keys multi_agent, mode and counterparts retire.

Contract (S2.1 to S2.3):

  S2.1  No runtime code reads them: engram_client has neither helper, neither
        default-config writer emits any of the three keys, `ia status` neither
        reads nor prints `mode`, the engram_stats version block has no
        multi_agent from EITHER source (build manifest or config.json).
  S2.2  An existing key in config.json is ignored, logged ONCE per process at MCP
        server startup (server._warn_retired_config_keys, called from the
        `__main__` block), never an error, never in a hook, and LEFT IN THE FILE.
  S2.3  first-session Step 7 branches on `id -un` matching
        ^agent-newborn-[0-9]{14}$, not on is_multi_agent_mode() or a `mode` field.

Each cell is written to redden on a plausible-wrong build (see the mutant list
in the PR handoff), not only on the pre-change code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"

RETIRED = ("multi_agent", "mode", "counterparts")
LINE_PREFIX = "config.json: retired key(s) "
LINE_SUFFIX = " ignored (#4255)"


def _line(names: str) -> str:
    return f"{LINE_PREFIX}{names}{LINE_SUFFIX}"


def _py(code: str, home: Path, *, cwd=None, env_extra=None, umask=None):
    """Run `code` in a fresh interpreter against ENGRAM_HOME=home; return the CompletedProcess."""
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "PYTHONPATH")}
    env.update({"ENGRAM_HOME": str(home), "ENGRAM_NO_EMBEDDINGS": "1", "LC_ALL": "C"})
    if env_extra:
        env.update(env_extra)
    prelude = f"import sys; sys.path.insert(0, {str(SRC)!r})\n"
    if umask is not None:
        prelude += f"import os; os.umask({umask})\n"
    return subprocess.run(
        [sys.executable, "-c", prelude + code],
        capture_output=True, text=True, env=env, cwd=str(cwd) if cwd else None, timeout=60,
    )


def _retired_lines(stderr: str) -> list[str]:
    return [ln for ln in stderr.splitlines() if "retired key(s)" in ln]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# S2.1  no runtime reader / writer
# ---------------------------------------------------------------------------

def test_engram_client_has_neither_helper():
    import engram_client
    assert not hasattr(engram_client, "is_multi_agent_mode")
    assert not hasattr(engram_client, "get_counterparts")


def test_engram_client_keeps_is_forum_configured():
    """Control: the surviving helper is still there (a mutant that deleted the module's helpers wholesale)."""
    import engram_client
    assert hasattr(engram_client, "is_forum_configured")


def test_fresh_install_default_config_has_none_of_the_keys(tmp_path):
    home = tmp_path / "h"
    r = _py("import engram_core as c, json; c._configure_paths(%r); c._ensure_data_dir();"
            " print(json.dumps(json.load(open(c.CONFIG_PATH))))" % str(home), home)
    assert r.returncode == 0, r.stderr
    cfg = json.loads(r.stdout.strip().splitlines()[-1])
    assert cfg, "control: the default config must not be empty"
    assert "schema_version" in cfg  # control: this is the real default block
    for k in RETIRED:
        assert k not in cfg, k


def test_sandbox_default_config_has_none_of_the_keys(tmp_path):
    home = tmp_path / "h"
    sb = tmp_path / "sb"
    r = _py("import engram_core as c, json\n"
            f"with c.engram_sandbox(data_dir={str(sb)!r}) as d:\n"
            "    print(json.dumps(json.load(open(d / 'config.json'))))\n", home)
    assert r.returncode == 0, r.stderr
    cfg = json.loads(r.stdout.strip().splitlines()[-1])
    assert "trust_pool" in cfg  # control: the sandbox block was written
    for k in RETIRED:
        assert k not in cfg, k


def _run_ia_status(tmp_path, cfg, fmt):
    home = tmp_path / "ia"
    home.mkdir()
    (home / "config.json").write_text(json.dumps(cfg))
    env = os.environ.copy()
    env.update({"ENGRAM_HOME": str(home), "FORUM_URL": "http://localhost:1", "LC_ALL": "C"})
    p = subprocess.run(
        [sys.executable, str(SRC / "tools" / "ia.py"), "status", "--format", fmt],
        capture_output=True, text=True, env=env, timeout=30,
    )
    return p


@pytest.mark.parametrize("mode_val", ["multi", "single"])
def test_ia_status_json_has_no_mode(tmp_path, mode_val):
    p = _run_ia_status(tmp_path, {"mode": mode_val, "agent_name": "ariadne"}, "json")
    assert p.returncode == 0, p.stderr
    data = json.loads(p.stdout)
    assert data["agent"] == "ariadne"          # control: status still reports the rest
    assert "read_cursor" in data
    assert "mode" not in data


@pytest.mark.parametrize("mode_val", ["multi", "single"])
def test_ia_status_text_has_no_mode(tmp_path, mode_val):
    p = _run_ia_status(tmp_path, {"mode": mode_val, "agent_name": "ariadne"}, "human")
    assert p.returncode == 0, p.stderr
    assert "ariadne" in p.stdout               # control
    assert "forum:" in p.stdout
    assert not re.search(r"(?m)^mode:", p.stdout)
    assert mode_val not in p.stdout


def test_ia_status_help_does_not_advertise_mode():
    p = subprocess.run([sys.executable, str(SRC / "tools" / "ia.py"), "status", "--help"],
                       capture_output=True, text=True, timeout=30)
    top = subprocess.run([sys.executable, str(SRC / "tools" / "ia.py"), "--help"],
                         capture_output=True, text=True, timeout=30)
    assert "forum" in p.stdout                  # control: help rendered
    assert not re.search(r"\bmode\b", p.stdout)
    status_line = [ln for ln in top.stdout.splitlines() if ln.strip().startswith("status")]
    assert status_line and "mode" not in " ".join(status_line)


def test_viz_config_schema_has_no_mode_field():
    import importlib.util
    spec = importlib.util.spec_from_file_location("config_schema_4255", SRC / "tools" / "config_schema.py")
    config_schema = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_schema)
    keys = [f["key"] for f in config_schema.SCHEMA] if hasattr(config_schema, "SCHEMA") else None
    assert keys is not None and "agent_name" in keys  # control
    assert "mode" not in keys


@pytest.fixture
def stats_mods():
    import test_engram_stats_mode as t  # same harness as the existing version-block cells
    return t._make_fresh_mods()


def test_stats_version_block_has_no_multi_agent_from_either_source(stats_mods):
    stats, _, _, _, core_mod = stats_mods
    (core_mod.DATA_DIR / ".engram-build-manifest.json").write_text(
        json.dumps({"tier": "convenience", "multi_agent": True, "shipped_at": "2026-10-29T00:00:00Z"}))
    core_mod.CONFIG_PATH.write_text(json.dumps({"agent_name": "t-agent", "multi_agent": True}))
    ver = json.loads(stats._stats_impl(sections=["version"]))["version"]
    assert ver.get("tier") == "convenience"     # controls: both sources WERE read
    assert ver.get("agent_name") == "t-agent"
    assert "multi_agent" not in ver


def test_stats_version_block_no_multi_agent_from_config_alone(stats_mods):
    """Separate cell: a mutant that dropped only the manifest source must still go red here."""
    stats, _, _, _, core_mod = stats_mods
    (core_mod.DATA_DIR / ".engram-build-manifest.json").unlink(missing_ok=True)
    core_mod.CONFIG_PATH.write_text(json.dumps({"agent_name": "t-agent", "multi_agent": True}))
    ver = json.loads(stats._stats_impl(sections=["version"]))["version"]
    assert ver.get("agent_name") == "t-agent"
    assert "multi_agent" not in ver


def test_stats_version_block_no_multi_agent_from_manifest_alone(stats_mods):
    stats, _, _, _, core_mod = stats_mods
    (core_mod.DATA_DIR / ".engram-build-manifest.json").write_text(
        json.dumps({"tier": "dev", "multi_agent": True}))
    core_mod.CONFIG_PATH.write_text(json.dumps({"agent_name": "t-agent"}))
    ver = json.loads(stats._stats_impl(sections=["version"]))["version"]
    assert ver.get("tier") == "dev"
    assert "multi_agent" not in ver


def test_server_stats_docstring_drops_multi_agent():
    text = (SRC / "server.py").read_text()
    i = text.index("version: live build identity")
    assert "multi_agent" not in text[i:i + 600]


def test_no_runtime_reader_names_remain_in_src_or_tools():
    """git-grep style: only the slice-5 docstring in _prompthooklib may still name the helper."""
    hits = []
    for base in (SRC, REPO_ROOT / "tools"):
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in (".py", ".sh", ".md", ".json") or "tests" in p.parts:
                continue
            if p.resolve() != p and p.is_symlink():
                continue
            try:
                t = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for m in re.finditer(r"is_multi_agent_mode|get_counterparts", t):
                hits.append(p.relative_to(REPO_ROOT).as_posix())
    assert set(hits) <= {"src/engram/hooks/claude/_prompthooklib.py"}, sorted(set(hits))


# ---------------------------------------------------------------------------
# S2.2  startup line
# ---------------------------------------------------------------------------

ALL3 = {"multi_agent": True, "mode": "multi", "counterparts": ["ariadne"], "agent_name": "x"}
CALL_ONCE = "import server; server._warn_retired_config_keys()\n"


def _home_with(tmp_path, cfg_text):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(cfg_text)
    return home


def test_all_three_keys_one_line_naming_all_three(tmp_path):
    home = _home_with(tmp_path, json.dumps(ALL3))
    r = _py(CALL_ONCE, home)
    assert r.returncode == 0, r.stderr
    assert _retired_lines(r.stderr) == [_line("multi_agent, mode, counterparts")]


def test_no_keys_no_line(tmp_path):
    home = _home_with(tmp_path, json.dumps({"agent_name": "x", "forum": {"url": "http://f"}}))
    r = _py(CALL_ONCE, home)
    assert r.returncode == 0, r.stderr
    assert _retired_lines(r.stderr) == []
    assert "#4255" not in r.stderr


@pytest.mark.parametrize("key,val", [("multi_agent", True), ("mode", "single"), ("counterparts", [])])
def test_one_key_only_names_only_it(tmp_path, key, val):
    home = _home_with(tmp_path, json.dumps({key: val, "agent_name": "x"}))
    r = _py(CALL_ONCE, home)
    assert r.returncode == 0, r.stderr
    assert _retired_lines(r.stderr) == [_line(key)]


def test_two_keys_name_exactly_those_two_in_canonical_order(tmp_path):
    home = _home_with(tmp_path, json.dumps({"counterparts": [], "multi_agent": False}))
    r = _py(CALL_ONCE, home)
    assert _retired_lines(r.stderr) == [_line("multi_agent, counterparts")]


@pytest.mark.parametrize("cfg", [
    {"multi_agent": "yes"},
    {"counterparts": "x"},
    {"mode": None},
    {"multi_agent": "yes", "counterparts": "x", "mode": None},
    {"multi_agent": {"a": 1}, "mode": 7, "counterparts": 3.5},
])
def test_non_boolean_values_same_single_line_no_exception(tmp_path, cfg):
    home = _home_with(tmp_path, json.dumps(cfg))
    r = _py(CALL_ONCE, home)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    names = ", ".join(k for k in RETIRED if k in cfg)
    assert _retired_lines(r.stderr) == [_line(names)]


def test_keys_remain_in_file_byte_identical_and_file_not_touched(tmp_path):
    home = _home_with(tmp_path, json.dumps(ALL3, indent=1) + "\n")
    cfgp = home / "config.json"
    before, mt_before = _sha(cfgp), cfgp.stat().st_mtime_ns
    names_before = set(os.listdir(home))
    r = _py(CALL_ONCE, home, umask=0o077)
    assert _retired_lines(r.stderr)  # control: the check really ran
    assert _sha(cfgp) == before
    assert cfgp.stat().st_mtime_ns == mt_before
    assert set(os.listdir(home)) == names_before  # no temp/backup file left behind


def test_once_per_process_not_once_per_config_read(tmp_path):
    home = _home_with(tmp_path, json.dumps(ALL3))
    code = (
        "import server, engram_core as c, engram_client\n"
        "for _ in range(4):\n"
        "    c._config_io.read_config_json(c.CONFIG_PATH)\n"
        "    engram_client._load_engram_config()\n"
        "    engram_client.is_forum_configured()\n"
        "    c._get_embedding_config()\n"
        "server._warn_retired_config_keys()\n"
        "server._warn_retired_config_keys()\n"
        "server._warn_retired_config_keys()\n"
        "for _ in range(4):\n"
        "    c._config_io.read_config_json(c.CONFIG_PATH)\n"
    )
    r = _py(code, home)
    assert r.returncode == 0, r.stderr
    assert len(_retired_lines(r.stderr)) == 1, r.stderr


def test_two_processes_one_line_each(tmp_path):
    home = _home_with(tmp_path, json.dumps(ALL3))
    for _ in range(2):
        r = _py(CALL_ONCE + CALL_ONCE, home)
        assert len(_retired_lines(r.stderr)) == 1


def test_importing_server_alone_emits_nothing(tmp_path):
    """Import is not startup: the line belongs to the `__main__` path only (once per server process)."""
    home = _home_with(tmp_path, json.dumps(ALL3))
    r = _py("import server\n", home)
    assert r.returncode == 0, r.stderr
    assert _retired_lines(r.stderr) == []


def test_main_block_calls_the_check_before_the_embedding_gate():
    text = (SRC / "server.py").read_text()
    # Anchor on the real block (line-start), not the prose mentions of the guard higher up.
    main = text[text.rindex('\nif __name__ == "__main__":\n'):]
    call = "\n    _warn_retired_config_keys()\n"
    gate = "\n    _embedding_startup_gate()\n"
    assert call in main and gate in main
    assert main.index(call) < main.index(gate)


def test_no_hook_or_other_runtime_module_emits_the_line():
    """Never in a hook: a hook's stderr/context reaches every prompt."""
    offenders = []
    for p in SRC.rglob("*.py"):
        if p.name == "server.py" and p.parent == SRC:
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        if "retired key(s)" in t or "(#4255)" in t and "ignored" in t:
            offenders.append(p.relative_to(REPO_ROOT).as_posix())
    assert offenders == []


# Environment dimensions: pre-existing tolerated states must not become a crash.
@pytest.mark.parametrize("label,content", [
    ("empty", ""),
    ("invalid_json", "{ not json"),
    ("list_toplevel", "[1, 2, 3]"),
    ("string_toplevel", '"mode"'),
    ("null_toplevel", "null"),
])
def test_unusable_config_is_silent_not_a_crash(tmp_path, label, content):
    home = _home_with(tmp_path, content)
    cfgp = home / "config.json"
    before = _sha(cfgp)
    r = _py(CALL_ONCE, home)
    assert r.returncode == 0, (label, r.stderr)
    assert "Traceback" not in r.stderr
    assert _retired_lines(r.stderr) == []
    assert _sha(cfgp) == before


def test_unreadable_config_is_silent_not_a_crash(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads mode-000 files")
    home = _home_with(tmp_path, json.dumps(ALL3))
    cfgp = home / "config.json"
    cfgp.chmod(0)
    try:
        r = _py(CALL_ONCE, home)
    finally:
        cfgp.chmod(0o600)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert _retired_lines(r.stderr) == []


def test_directory_at_config_path_is_silent(tmp_path):
    home = tmp_path / "home"
    (home / "config.json").mkdir(parents=True)
    r = _py(CALL_ONCE, home)
    assert r.returncode == 0, r.stderr
    assert _retired_lines(r.stderr) == []


def test_absent_config_is_silent_and_not_created(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = _py(CALL_ONCE, home)
    assert r.returncode == 0, r.stderr
    assert _retired_lines(r.stderr) == []
    assert not (home / "config.json").exists()


def test_cwd_elsewhere_and_umask_002_do_not_matter(tmp_path):
    home = _home_with(tmp_path, json.dumps(ALL3))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    before = _sha(home / "config.json")
    r = _py(CALL_ONCE, home, cwd=elsewhere, umask=0o002)
    assert _retired_lines(r.stderr) == [_line("multi_agent, mode, counterparts")]
    assert _sha(home / "config.json") == before
    assert list(elsewhere.iterdir()) == []


def test_symlinked_engram_home_reads_the_same_file(tmp_path):
    real = _home_with(tmp_path, json.dumps({"mode": "multi"}))
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    r = _py(CALL_ONCE, link)
    assert _retired_lines(r.stderr) == [_line("mode")]


def test_engram_home_unset_reads_home_dot_engram(tmp_path):
    """Unset ENGRAM_HOME: the check reads $HOME/.engram/config.json, the file the server reads."""
    fake_home = tmp_path / "fakehome"
    (fake_home / ".engram").mkdir(parents=True)
    (fake_home / ".engram" / "config.json").write_text(json.dumps({"counterparts": []}))
    env = {k: v for k, v in os.environ.items() if k not in ("ENGRAM_HOME", "PYTHONPATH")}
    env.update({"HOME": str(fake_home), "ENGRAM_NO_EMBEDDINGS": "1", "LC_ALL": "C"})
    r = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SRC)!r})\n" + CALL_ONCE],
        capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert _retired_lines(r.stderr) == [_line("counterparts")]


# ---------------------------------------------------------------------------
# S2.3  first-session Step 7
# ---------------------------------------------------------------------------

SKILL = SRC / "skills" / "claude" / "engram-first-session" / "SKILL.md"
NEWBORN_RE = r"^agent-newborn-[0-9]{14}$"


def _step7() -> str:
    t = SKILL.read_text(encoding="utf-8")
    i = t.index("## Step 7 — Tell the user what's next")
    j = t.index("## Step 7.5", i)
    return t[i:j]


def test_step7_branches_on_uid_pattern_and_id_un():
    s = _step7()
    assert NEWBORN_RE in s
    assert "id -un" in s


def test_step7_names_neither_helper_nor_mode_field():
    s = _step7()
    assert "is_multi_agent_mode" not in s
    assert not re.search(r"`mode`", s)
    assert not re.search(r"\bmode field\b", s)


def test_step7_headings():
    s = _step7()
    assert "### Newborn agent (uid not yet finalized)" in s
    assert "### Named agent" in s
    assert "### Multi-agent mode" not in s
    assert "### Single-agent mode" not in s


def test_step7_finalize_name_text_under_newborn_heading_only():
    s = _step7()
    nb = s.index("### Newborn agent (uid not yet finalized)")
    named = s.index("### Named agent")
    assert nb < named
    assert "agentctl finalize-name" in s[nb:named]
    assert "agentctl finalize-name" not in s[named:]
    assert "Stop here" in s[named:]


@pytest.mark.parametrize("uid,ok", [
    ("agent-newborn-20261029121600", True),
    ("agent-ariadne", False),
    ("agent-newborn-2026", False),
    ("newborn-20261029121600", False),
    ("agent-newborn-20261029121600\n", False),   # re.match + $ would accept this; the skill's grep -E per line does not matter, the anchored class must be explicit
    ("agent-newborn-202610291216000", False),    # 15 digits
    ("agent-newborn-2026102912160", False),      # 13 digits
    ("agent-newborn-20261029121600x", False),
    ("xagent-newborn-20261029121600", False),
    ("agent-newborn-2026102912160a", False),
])
def test_newborn_uid_regex(uid, ok):
    # The pattern documented in the skill, extracted from the skill text itself (not re-typed here).
    pat = re.search(r"\^agent-newborn-\[0-9\]\{14\}\$", _step7()).group(0)
    assert pat == NEWBORN_RE
    # fullmatch semantics via \Z so a trailing newline cannot slip past `$`.
    assert (re.match(pat[:-1] + r"\Z", uid) is not None) is ok
    # What the shell sees: grep -E on the single-line output of `id -un`.
    g = subprocess.run(["grep", "-E", pat], input=uid, capture_output=True, text=True)
    if "\n" not in uid:
        assert (g.returncode == 0) is ok


def test_viz_config_schema_has_no_counterparts_field():
    """Amended S2.4: the viz `counterparts` display field retires with the writer."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("config_schema_4255b", SRC / "tools" / "config_schema.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    keys = [f["key"] for f in cs.SCHEMA]
    assert "agent_name" in keys  # control
    assert "counterparts" not in keys
