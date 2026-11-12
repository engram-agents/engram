"""Contract tests for tools/install.sh, the scripted fresh install (#4097, slice 4).

Written BEFORE the script. Everything the script calls is a fake: a `claude`, a `curl`, a
venv `python` whose pip does nothing, `install-local-marketplace.sh`, `bootstrap.py`,
`install_selfcheck.py`, the service scripts. The script runs under a PATH that holds only
symlinks to the few system tools it needs plus the fakes (so this seat's real `claude` can
never be reached), with a temp HOME and ENGRAM_HOME. No network, no real pip, no real Claude
Code, no real bootstrap.

Contract (from the slice-4 spec):
  * fresh home, healthy fakes -> exit 0, every step `ok`, the self-check called with
    `--phase pre-first-session`, zero prompts with --yes
  * each step's failure -> non-zero exit, a `FAIL: [step N]` line naming that step, and no
    later step runs
  * DB state: absent -> bootstrap plain; 0-node file -> FORCE_RESEED_EMPTY=1 and the line says
    so; non-empty -> not called, node count printed
  * config.json: only install_tier is written; every other key survives (multi_agent is retired, #4255)
  * CLAUDE.md: existing + no backup -> backup with the original bytes; backup present -> left
    byte-identical
  * --yes alone never runs the Claude Code installer; --install-claude-code does
  * re-run on an installed home -> bootstrap skipped, exit 0
"""

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_INSTALL_SH = REPO_ROOT / "src" / "engram" / "tools" / "install.sh"

SYSTEM_TOOLS = (
    "bash sh env git uname cp mkdir rm cat sed grep dirname basename readlink mktemp date tr "
    "ls chmod tail head touch mv sort wc id true false tee sleep ln find expr cut awk tty "
    "realpath"
).split()


def _write(path, text, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


class Sandbox:
    """A temp HOME + ENGRAM_HOME + a repo-shaped tree around the REAL install.sh."""

    def __init__(self, root, with_claude=True, with_git=True, with_venv=True, with_systemctl=True):
        self.root = root
        self.home = root / "home"
        self.engram = self.home / ".engram"
        self.claude_home = self.home / ".claude"
        self.repo = root / "repo"
        self.tools_src = self.repo / "src" / "engram" / "tools"
        self.calls = root / "calls.log"
        self.fakebin = root / "fakebin"
        self.safebin = root / "safebin"
        self.claude_state = root / "claude-state"
        self.plugin_src = root / "plugin-src"
        for d in (self.home, self.claude_state):
            d.mkdir(parents=True, exist_ok=True)
        self.calls.write_text("")
        self._build_repo()
        self._build_plugin_src()
        self._build_safebin(with_git)
        self._build_fakebin(with_claude, with_systemctl)
        if with_venv:
            self._build_venv()

    # -- construction ------------------------------------------------------------------------
    def _build_repo(self):
        self.tools_src.mkdir(parents=True)
        if REAL_INSTALL_SH.exists():
            shutil.copy(REAL_INSTALL_SH, self.tools_src / "install.sh")
        # Step 3 writes install_tier through the checkout's file guard (stdlib only), so a
        # repo-shaped tree carries it beside tools/.
        shutil.copy(REPO_ROOT / "src" / "engram" / "engram_file_guard.py", self.tools_src.parent / "engram_file_guard.py")
        (self.repo / "tools").mkdir()
        (self.repo / "tools" / "install.sh").symlink_to("../src/engram/tools/install.sh")
        (self.repo / "requirements").mkdir()
        (self.repo / "requirements" / "requirements.txt").write_text("fakepkg\n")
        (self.repo / "requirements" / "requirements-dev.txt").write_text("fakedevpkg\n")
        _write(self.tools_src / "install-local-marketplace.sh", """#!/usr/bin/env bash
echo "marketplace" >> "$CALLS_LOG"
echo "market-args $*" >> "$CALLS_LOG"
python3 -c 'import json, os; d = json.load(open(os.path.join(os.environ["ENGRAM_HOME"], "config.json"))); open(os.environ["CALLS_LOG"], "a").write("market-config tier=%s\\n" % d.get("install_tier"))'
[ "${FAKE_MARKET_RC:-0}" = "0" ] || exit "$FAKE_MARKET_RC"
dest="$ENGRAM_HOME/marketplace/plugins/engram"
rm -rf "$dest"; mkdir -p "$dest"
cp -a "$FAKE_PLUGIN_SRC/." "$dest/"
""")
        _write(self.tools_src / "install_selfcheck.py", """import os, sys
open(os.environ["CALLS_LOG"], "a").write("selfcheck " + " ".join(sys.argv[1:]) + "\\n")
print("SELFCHECK fake")
sys.exit(int(os.environ.get("FAKE_SELFCHECK_RC", "0")))
""")
        # L1 (round 3): step 1 refuses a HEAD that is not a deployable tag merged into origin/dev
        # or origin/main, using the repo's own src/build/engine/deploy_tag.py. The fixture ships
        # that one file, then commits, tags v0.0.1 and points refs/remotes/origin/dev at it: the
        # default Sandbox is a deployable-tag checkout, so only the L1 cells change that.
        eng = self.repo / "src" / "build" / "engine"
        eng.mkdir(parents=True)
        (eng / "__init__.py").write_text("")
        shutil.copy(REPO_ROOT / "src" / "build" / "engine" / "deploy_tag.py", eng / "deploy_tag.py")
        genv = {**self._git_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        for cmd in (["init", "-q", "-b", "dev"], ["add", "-A"], ["commit", "-q", "-m", "base"],
                    ["tag", "v0.0.1"], ["update-ref", "refs/remotes/origin/dev", "HEAD"]):
            subprocess.run(["git", "-C", str(self.repo), *cmd], check=True, env=genv)

    @staticmethod
    def _git_env():
        return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

    def _build_plugin_src(self):
        p = self.plugin_src
        _write(p / "bootstrap.py", """import os, pathlib, sqlite3, sys
e = os.environ
open(e["CALLS_LOG"], "a").write("bootstrap FORCE_RESEED_EMPTY=%s FORCE=%s TARGET=%s ALPHA_DIR=%s TEMPLATES=%s PYTHON_BIN=%s\\n" % (
    e.get("FORCE_RESEED_EMPTY", "unset"), e.get("FORCE", "unset"), e.get("TARGET"), e.get("ALPHA_DIR"),
    e.get("ALPHA_TEMPLATES_DIR"), e.get("PYTHON_BIN")))
import json
open(e["CALLS_LOG"], "a").write("argv-of-bootstrap " +json.dumps(sys.argv[1:]) + "\\n")
if int(e.get("FAKE_BOOTSTRAP_RC", "0")):
    print("bootstrap fake: failing")
    sys.exit(int(e["FAKE_BOOTSTRAP_RC"]))
eh = pathlib.Path(e["ENGRAM_HOME"])
con = sqlite3.connect(str(eh / "knowledge.db"))
con.execute("CREATE TABLE IF NOT EXISTS nodes (id TEXT)")
con.execute("DELETE FROM nodes")
con.executemany("INSERT INTO nodes VALUES (?)", [("n%d" % i,) for i in range(6)])
con.commit()
con.close()
(pathlib.Path(e["CLAUDE_HOME"]) / "CLAUDE.md").write_text("rendered by ENGRAM bootstrap\\n")
(eh / "first-run-pending").write_text("{}")
""", 0o644)
        (p / "templates").mkdir(parents=True, exist_ok=True)
        _write(p / "tools" / "setup-surface-daemon-service.sh", """#!/usr/bin/env bash
echo "recall" >> "$CALLS_LOG"
exit "${FAKE_RECALL_RC:-0}"
""")
        _write(p / "tools" / "operator-setup-viz.sh", """#!/usr/bin/env bash
echo "viz" >> "$CALLS_LOG"
exit "${FAKE_VIZ_RC:-0}"
""")
        _write(p / "tools" / "set_forum_url.py", """import os, sys
open(os.environ["CALLS_LOG"], "a").write("forum_url " + " ".join(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("FAKE_FORUM_RC", "0")))
""", 0o644)

    def _build_safebin(self, with_git):
        self.safebin.mkdir(parents=True, exist_ok=True)
        for name in SYSTEM_TOOLS:
            if name == "git" and not with_git:
                continue
            found = shutil.which(name)
            if found:
                (self.safebin / name).symlink_to(found)
        # python3 -> the interpreter running this test (a wrapper: a venv python symlinked
        # elsewhere would lose its pyvenv.cfg).
        _write(self.safebin / "python3", '#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)

    def _build_fakebin(self, with_claude, with_systemctl):
        self.fakebin.mkdir(parents=True, exist_ok=True)
        self.claude_script = self.root / "claude-fake.sh"
        _write(self.claude_script, """#!/bin/sh
echo "claude $*" >> "$CALLS_LOG"
st="$FAKE_CLAUDE_STATE"
case "$1" in
  --version) echo "9.9.9 (Claude Code)"; exit "${FAKE_CLAUDE_VERSION_RC:-0}" ;;
esac
if [ "$1" = "plugin" ]; then
  # FAKE_CLAUDE_PLUGIN_MODE: "already" = install/enable exit 1 (already installed/enabled) yet the
  # plugin IS enabled; "noeffect" = install/enable exit 0 yet the plugin ends up NOT enabled.
  case "$FAKE_CLAUDE_PLUGIN_MODE:$2" in
    already:install|already:enable) echo "already done" >&2; touch "$st/installed" "$st/enabled"; exit 1 ;;
    noeffect:install) touch "$st/installed"; exit 0 ;;
    noeffect:enable) exit 0 ;;
  esac
  case "$2" in
    install) [ "$FAKE_CLAUDE_FAIL" = "install" ] && { echo "install: cannot run without a session" >&2; exit 1; }
             touch "$st/installed"; exit 0 ;;
    enable)  [ "$FAKE_CLAUDE_FAIL" = "enable" ] && { echo "enable: failed" >&2; exit 1; }
             [ -e "$st/installed" ] && touch "$st/enabled"; exit 0 ;;
    list)    if [ -e "$st/enabled" ]; then echo '[{"id":"engram@engram-local","enabled":true}]'
             elif [ -e "$st/installed" ]; then echo '[{"id":"engram@engram-local","enabled":false}]'
             else echo '[]'; fi; exit 0 ;;
  esac
fi
exit 0
""")
        if with_claude:
            (self.fakebin / "claude").symlink_to(self.claude_script)
        _write(self.fakebin / "curl", """#!/bin/sh
echo "curl $*" >> "$CALLS_LOG"
[ "${FAKE_CURL_RC:-0}" = "0" ] || exit "$FAKE_CURL_RC"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
cat > "$out" <<EOF
echo "claude-installer-ran" >> "$CALLS_LOG"
mkdir -p "$HOME/.local/bin"
ln -sf "$FAKE_CLAUDE_SCRIPT" "$HOME/.local/bin/claude"
exit \\${FAKE_CLAUDE_INSTALLER_RC:-0}
EOF
""")
        if with_systemctl:
            _write(self.fakebin / "systemctl", """#!/bin/sh
if [ "$1" = "--user" ] && [ "$2" = "show-environment" ]; then exit "${FAKE_SYSTEMCTL_USER_RC:-0}"; fi
exit 0
""")
            _write(self.fakebin / "launchctl", "#!/bin/sh\nexit 0\n")

    def _build_venv(self):
        _write(self.engram / "venv" / "bin" / "python", """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  echo "pip $*" >> "$CALLS_LOG"
  echo "fake-pip-output-marker: $*"
  exit "${FAKE_PIP_RC:-0}"
fi
exec "$REAL_PY" "$@"
""")

    def use_real_bootstrap(self):
        """Replace the fake bootstrap.py by a thin wrapper that execs the repo's REAL one, with the
        real templates beside it. The installer only ASKS about CLAUDE.md; the real bootstrap makes
        the backups, so the cells about backups need it."""
        shutil.copytree(REPO_ROOT / "src" / "engram" / "templates", self.plugin_src / "templates", dirs_exist_ok=True)
        (self.plugin_src / "bootstrap.py").write_text(
            "import os, sys\n"
            "open(os.environ['CALLS_LOG'], 'a').write('bootstrap (real) ' + ' '.join(sys.argv[1:]) + '\\n')\n"
            f"os.execv(sys.executable, [sys.executable, {str(REPO_ROOT / 'src' / 'engram' / 'bootstrap.py')!r}, *sys.argv[1:]])\n"
        )
        return self

    # -- running -----------------------------------------------------------------------------
    def run(self, *args, env=None, stdin=subprocess.DEVNULL):
        e = {
            "PATH": "%s:%s" % (self.fakebin, self.safebin),
            "HOME": str(self.home),
            "ENGRAM_HOME": str(self.engram),
            "CLAUDE_HOME": str(self.claude_home),
            "TMPDIR": str(self.root),
            "CALLS_LOG": str(self.calls),
            "FAKE_PLUGIN_SRC": str(self.plugin_src),
            "FAKE_CLAUDE_STATE": str(self.claude_state),
            "FAKE_CLAUDE_SCRIPT": str(self.claude_script),
            "REAL_PY": sys.executable,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        e.update(env or {})
        proc = subprocess.run(
            ["bash", str(self.repo / "tools" / "install.sh"), *args],
            cwd=str(self.root), env=e, stdin=stdin, capture_output=True, text=True, timeout=120,
        )
        proc.all = proc.stdout + proc.stderr
        return proc

    def git(self, *args):
        genv = {**self._git_env(), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=genv,
                              capture_output=True, text=True).stdout.strip()

    def commit_on_branch(self, branch, tag=None):
        """A new commit on ``branch`` (not merged into origin/dev); optionally tagged."""
        self.git("checkout", "-q", "-b", branch)
        (self.repo / "feature.txt").write_text(branch)
        self.git("add", "feature.txt")
        self.git("commit", "-q", "-m", branch)
        if tag:
            self.git("tag", tag)
        return self.git("rev-parse", "HEAD")

    def call_lines(self):
        return self.calls.read_text().splitlines()

    def calls_starting(self, prefix):
        return [c for c in self.call_lines() if c.startswith(prefix)]

    def make_db(self, nodes):
        self.engram.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.engram / "knowledge.db"))
        con.execute("CREATE TABLE nodes (id TEXT)")
        con.executemany("INSERT INTO nodes VALUES (?)", [("n%d" % i,) for i in range(nodes)])
        con.commit()
        con.close()


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


# ── Cell 1: fresh home, healthy fakes ───────────────────────────────────────────────────────
def test_fresh_home_all_fakes_healthy_runs_every_step_ok(sb):
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    for n in range(1, 11):
        assert any(l.startswith("[step %d]" % n) and " ... ok" in l for l in r.stdout.splitlines()), \
            "step %d has no ok line:\n%s" % (n, r.all)
    assert "FAIL" not in r.all
    assert "[Y/n]" not in r.all  # zero prompts
    assert sb.calls_starting("selfcheck") == ["selfcheck --phase pre-first-session"]
    boot = sb.calls_starting("bootstrap")
    assert len(boot) == 1 and "FORCE_RESEED_EMPTY=unset" in boot[0] and "FORCE=unset" in boot[0]
    assert "TARGET=claude" in boot[0]
    assert "ALPHA_DIR=%s" % (sb.engram / "marketplace" / "plugins" / "engram") in boot[0]
    assert "TEMPLATES=%s" % (sb.engram / "marketplace" / "plugins" / "engram" / "templates") in boot[0]
    assert "PYTHON_BIN=%s" % (sb.engram / "venv" / "bin" / "python") in boot[0]
    # the order of the real work
    kinds = [c.split()[0] if not c.startswith("claude plugin") else c for c in sb.call_lines()]
    assert kinds.index("marketplace") < kinds.index("bootstrap") < kinds.index("claude plugin install engram@engram-local") \
        < kinds.index("claude plugin enable engram@engram-local") < kinds.index("recall") < kinds.index("selfcheck")
    # the final block names the next actions
    assert "claude" in r.stdout.split("[step 10]")[1] and "/login" in r.stdout


def test_cpu_torch_index_only_on_linux_x86_64(sb):
    sb.run("--yes")
    pips = sb.calls_starting("pip")
    assert len(pips) == 1 and "requirements.txt" in pips[0]
    linux_x86 = platform.system() == "Linux" and platform.machine() == "x86_64"
    assert ("--extra-index-url https://download.pytorch.org/whl/cpu" in pips[0]) == linux_x86


def test_dev_tier_also_installs_dev_requirements(sb):
    r = sb.run("--yes", "--tier", "dev")
    assert r.returncode == 0, r.all
    pips = sb.calls_starting("pip")
    assert len(pips) == 2 and "requirements-dev.txt" in pips[1]
    assert json.loads((sb.engram / "config.json").read_text())["install_tier"] == "dev"


def test_venv_created_when_absent(tmp_path):
    sb2 = Sandbox(tmp_path, with_venv=False)
    # python3 -m venv DIR is faked by a python3 wrapper that makes DIR/bin/python
    _write(sb2.safebin / "python3", """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  echo "venv-created $3" >> "$CALLS_LOG"
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then echo "pip $*" >> "$CALLS_LOG"; exit 0; fi\\nexec "$REAL_PY" "$@"\\n' > "$3/bin/python"
  chmod +x "$3/bin/python"
  exit 0
fi
exec "%s" "$@"
""" % sys.executable)
    r = sb2.run("--yes")
    assert r.returncode == 0, r.all
    assert sb2.calls_starting("venv-created") == ["venv-created %s" % (sb2.engram / "venv")]


# ── Cell 2: each step's failure names the step and stops the run ────────────────────────────
STEP_FAILURES = [
    # (step, args, env, sandbox kwargs, call-prefixes that must NOT appear)
    (1, ["--yes"], {}, {"with_git": False}, ["pip", "marketplace", "bootstrap", "claude plugin", "selfcheck"]),
    (2, ["--yes", "--install-claude-code"], {"FAKE_CURL_RC": "22"}, {"with_claude": False},
     ["pip", "marketplace", "bootstrap", "claude plugin", "selfcheck"]),
    (3, ["--yes"], {}, {"config": "{not json"}, ["pip", "marketplace", "bootstrap", "claude plugin", "selfcheck"]),
    (4, ["--yes"], {"FAKE_PIP_RC": "1"}, {}, ["marketplace", "bootstrap", "claude plugin", "selfcheck"]),
    (5, ["--yes"], {"FAKE_MARKET_RC": "3"}, {}, ["bootstrap", "claude plugin", "recall", "selfcheck"]),
    (6, ["--yes"], {"FAKE_BOOTSTRAP_RC": "1"}, {}, ["claude plugin", "recall", "selfcheck"]),
    (7, ["--yes"], {"FAKE_CLAUDE_FAIL": "install"}, {}, ["recall", "selfcheck"]),
    (8, ["--yes"], {"FAKE_RECALL_RC": "1"}, {}, ["selfcheck"]),
    (9, ["--yes"], {"FAKE_SELFCHECK_RC": "1"}, {}, []),
]


@pytest.mark.parametrize("step,args,env,kw,forbidden", STEP_FAILURES, ids=["step%d" % s[0] for s in STEP_FAILURES])
def test_each_step_failure_names_the_step_and_stops(tmp_path, step, args, env, kw, forbidden):
    kw = dict(kw)
    config = kw.pop("config", None)
    sb2 = Sandbox(tmp_path, **kw)
    if config is not None:
        sb2.engram.mkdir(parents=True, exist_ok=True)
        (sb2.engram / "config.json").write_text(config)
    r = sb2.run(*args, env=env)
    assert r.returncode != 0, r.all
    assert "FAIL: [step %d]" % step in r.stderr, r.all
    assert not any(l.startswith("[step %d]" % step) and l.rstrip().endswith("ok") for l in r.stdout.splitlines())
    for later in range(step + 1, 11):
        assert "[step %d]" % later not in r.all, "step %d ran after step %d failed:\n%s" % (later, step, r.all)
    for prefix in forbidden:
        assert not sb2.calls_starting(prefix), "%s ran after step %d failed: %s" % (prefix, step, sb2.call_lines())


def test_step7_fails_when_enable_does_not_take_effect(tmp_path):
    sb2 = Sandbox(tmp_path)
    r = sb2.run("--yes", env={"FAKE_CLAUDE_FAIL": "enable"})
    assert r.returncode != 0 and "FAIL: [step 7]" in r.stderr, r.all
    assert "enabled" in r.stderr


def test_missing_claude_without_flag_fails_with_native_installer_command_and_flag(tmp_path):
    sb2 = Sandbox(tmp_path, with_claude=False)
    r = sb2.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all
    assert "curl -fsSL https://claude.ai/install.sh | bash" in r.stderr
    assert "--install-claude-code" in r.stderr


def test_claude_that_does_not_run_fails_step_1(tmp_path):
    sb2 = Sandbox(tmp_path)
    r = sb2.run("--yes", env={"FAKE_CLAUDE_VERSION_RC": "1"})
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all


def test_git_that_cannot_read_the_checkout_gets_the_safe_directory_remedy(tmp_path):
    sb2 = Sandbox(tmp_path)
    _write(sb2.fakebin / "git", """#!/bin/sh
echo "fatal: detected dubious ownership in repository at '/some/other/users/checkout'" >&2
exit 128
""")
    r = sb2.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all
    assert "safe.directory '/some/other/users/checkout'" in r.stderr
    assert not sb2.calls_starting("pip")


def test_unwritable_claude_home_fails_step_1(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    sb2 = Sandbox(tmp_path)
    sb2.claude_home.mkdir(parents=True)
    sb2.claude_home.chmod(0o555)
    try:
        r = sb2.run("--yes")
    finally:
        sb2.claude_home.chmod(0o755)
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr and str(sb2.claude_home) in r.stderr, r.all


# ── Cell 3: knowledge.db states ─────────────────────────────────────────────────────────────
def test_db_absent_runs_bootstrap_plain(sb):
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    boot = sb.calls_starting("bootstrap")
    assert len(boot) == 1 and "FORCE_RESEED_EMPTY=unset" in boot[0]


def test_db_with_zero_nodes_runs_bootstrap_with_force_reseed_empty_and_says_so(sb):
    sb.make_db(0)
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    boot = sb.calls_starting("bootstrap")
    assert len(boot) == 1 and "FORCE_RESEED_EMPTY=1" in boot[0], boot
    assert any("0 nodes" in l and "FORCE_RESEED_EMPTY=1" in l for l in r.stdout.splitlines()), r.all


def test_inherited_force_variables_do_not_leak_into_a_plain_bootstrap(sb):
    r = sb.run("--yes", env={"FORCE": "1", "FORCE_RESEED_EMPTY": "1"})
    assert r.returncode == 0, r.all
    boot = sb.calls_starting("bootstrap")
    assert "FORCE_RESEED_EMPTY=unset" in boot[0] and "FORCE=unset" in boot[0]


def test_db_non_empty_skips_bootstrap_and_prints_the_node_count(sb):
    sb.make_db(7)
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert not sb.calls_starting("bootstrap")
    assert any("7 nodes" in l and "skipped" in l for l in r.stdout.splitlines()), r.all
    assert sb.calls_starting("selfcheck") == ["selfcheck --phase installed"]


def test_unreadable_db_fails_step_6_instead_of_guessing(sb):
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "knowledge.db").write_text("this is not a sqlite file at all, just text padding " * 4)
    r = sb.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 6]" in r.stderr, r.all
    assert not sb.calls_starting("bootstrap")


# ── Cell 4: config.json keeps every key but install_tier ────────────────────────────────────────
def test_config_with_self_lineage_and_other_keys_survives_unchanged(sb):
    sb.engram.mkdir(parents=True, exist_ok=True)
    original = {
        "self_lineage": "anthropic:opus",
        "install_tier": "essential",
        "multi_agent": True,
        "cadence": {"drowsiness_ceiling_tokens": 800000},
        "unrelated": ["a", {"b": 1}],
    }
    (sb.engram / "config.json").write_text(json.dumps(original, indent=2) + "\n")
    r = sb.run("--yes", "--tier", "convenience")
    assert r.returncode == 0, r.all
    after = json.loads((sb.engram / "config.json").read_text())
    assert after["install_tier"] == "convenience" and after["multi_agent"] is True
    for key in ("self_lineage", "cadence", "unrelated"):
        assert after[key] == original[key]
    before_lines = (json.dumps(original, indent=2) + "\n").splitlines()
    after_lines = (sb.engram / "config.json").read_text().splitlines()
    for line in before_lines:
        if "install_tier" in line or "multi_agent" in line:
            continue
        assert line in after_lines, "line changed: %r" % line
    assert list(after) == list(original)  # key order preserved


def test_config_already_matching_is_not_rewritten(sb):
    sb.engram.mkdir(parents=True, exist_ok=True)
    raw = '{"self_lineage":   "anthropic:opus",   "install_tier": "convenience", "multi_agent": false}'
    (sb.engram / "config.json").write_text(raw)
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert (sb.engram / "config.json").read_text() == raw


def test_config_created_when_absent_with_install_tier_only(sb):
    r = sb.run("--yes", "--multi-agent")  # retired flag: accepted, no effect (#4255)
    assert r.returncode == 0, r.all
    assert json.loads((sb.engram / "config.json").read_text()) == {"install_tier": "convenience"}


# ── Cell 5: CLAUDE.md backup ───────────────────────────────────────────────────────────────
def _stamped_backups(claude_home):
    return sorted(claude_home.glob("CLAUDE.md.engram-backup.*"))


def test_existing_claude_md_is_backed_up_with_its_original_bytes(sb):
    """The installer only ASKS; the real bootstrap makes the backups: the first-ever `.pre-engram`
    and a stamped copy, both holding the original bytes. The step-6 line names the file and both
    backup names."""
    sb.use_real_bootstrap()
    sb.claude_home.mkdir(parents=True)
    original = b"my own rules \xe2\x80\x94 do not lose me\r\nline two\n"
    (sb.claude_home / "CLAUDE.md").write_bytes(original)
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert (sb.claude_home / "CLAUDE.md.pre-engram").read_bytes() == original
    stamped = _stamped_backups(sb.claude_home)
    assert len(stamped) == 1 and stamped[0].read_bytes() == original
    assert (sb.claude_home / "CLAUDE.md").read_bytes() != original
    assert sb.calls_starting("argv-of-bootstrap") == [] and sb.calls_starting("bootstrap (real) --replace ")
    assert str(sb.claude_home / "CLAUDE.md") in r.stdout and str(sb.claude_home / "CLAUDE.md.pre-engram") in r.stdout


def test_the_installer_makes_no_copy_of_claude_md_itself(sb):
    """With the fake bootstrap (which copies nothing) an existing CLAUDE.md gets NO backup file:
    the only code that copies it is bootstrap's guard. A copy made by install.sh would show here."""
    sb.claude_home.mkdir(parents=True)
    (sb.claude_home / "CLAUDE.md").write_bytes(b"the user's file\n")
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert not (sb.claude_home / "CLAUDE.md.pre-engram").exists()
    assert _stamped_backups(sb.claude_home) == []


def test_existing_backup_is_left_byte_identical(sb):
    sb.use_real_bootstrap()
    sb.claude_home.mkdir(parents=True)
    current = b"ENGRAM's own rendered file from an earlier run\n"
    (sb.claude_home / "CLAUDE.md").write_bytes(current)
    users_original = b"the user's real original, backed up on the first run\n"
    (sb.claude_home / "CLAUDE.md.pre-engram").write_bytes(users_original)
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert (sb.claude_home / "CLAUDE.md.pre-engram").read_bytes() == users_original
    stamped = _stamped_backups(sb.claude_home)
    assert len(stamped) == 1 and stamped[0].read_bytes() == current, "the current file must get its own stamped backup"
    assert (sb.claude_home / "CLAUDE.md").read_bytes() not in (current, users_original)
    assert str(sb.claude_home / "CLAUDE.md.pre-engram") in r.stdout


def test_dangling_backup_link_is_left_alone_and_the_current_file_gets_a_stamped_backup(sb):
    """The backup-name checks moved from the shell to bootstrap's guard: a dangling link at
    `.pre-engram` is not written through; the stamped backup is made and the run goes on."""
    sb.use_real_bootstrap()
    sb.claude_home.mkdir(parents=True)
    original = b"the user's file, must survive\n"
    (sb.claude_home / "CLAUDE.md").write_bytes(original)
    bak = sb.claude_home / "CLAUDE.md.pre-engram"
    bak.symlink_to(sb.root / "nowhere-backup.md")
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert bak.is_symlink() and not (sb.root / "nowhere-backup.md").exists(), "the link was written through"
    stamped = _stamped_backups(sb.claude_home)
    assert len(stamped) == 1 and stamped[0].read_bytes() == original


def test_directory_at_backup_path_is_left_alone_and_the_current_file_gets_a_stamped_backup(sb):
    sb.use_real_bootstrap()
    sb.claude_home.mkdir(parents=True)
    original = b"the user's file, must survive\n"
    (sb.claude_home / "CLAUDE.md").write_bytes(original)
    bak = sb.claude_home / "CLAUDE.md.pre-engram"
    bak.mkdir()
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert bak.is_dir() and not list(bak.iterdir())
    stamped = _stamped_backups(sb.claude_home)
    assert len(stamped) == 1 and stamped[0].read_bytes() == original


def test_hard_link_at_backup_path_fails_step_6_and_nothing_is_replaced(sb):
    """bootstrap's guard refuses (the backup name is the same file as CLAUDE.md, so replacing it
    would overwrite the backup too); the installer reports it as a step-6 failure."""
    sb.use_real_bootstrap()
    sb.claude_home.mkdir(parents=True)
    original = b"the user's file, must survive\n"
    md = sb.claude_home / "CLAUDE.md"
    md.write_bytes(original)
    bak = sb.claude_home / "CLAUDE.md.pre-engram"
    os.link(md, bak)
    r = sb.run("--yes")
    assert r.returncode != 0
    assert "FAIL: [step 6]" in r.all and str(bak) in r.all and str(md) in r.all
    assert "same file" in r.all
    assert md.read_bytes() == original and bak.read_bytes() == original
    assert _stamped_backups(sb.claude_home) == []


def test_second_run_after_aborted_seed_does_not_overwrite_the_backup(sb):
    """First run (real bootstrap) saves the original to `.pre-engram`. The seed is then undone
    (graph and memory markers removed) and CLAUDE.md is edited; the second run replaces CLAUDE.md after a
    stamped backup of that edit, and `.pre-engram` is not touched."""
    sb.use_real_bootstrap()
    sb.claude_home.mkdir(parents=True)
    original = b"user original\n"
    (sb.claude_home / "CLAUDE.md").write_bytes(original)
    first = sb.run("--yes")
    assert first.returncode == 0, first.all
    assert (sb.claude_home / "CLAUDE.md.pre-engram").read_bytes() == original
    # undo the seed (graph and the files that mark an existing agent), keep CLAUDE.md
    for name in ("knowledge.db", "warm-briefing.md", "seed-manifest.json", "first-run-pending"):
        (sb.engram / name).unlink()
    edited = b"something ENGRAM wrote meanwhile\n"
    (sb.claude_home / "CLAUDE.md").write_bytes(edited)
    second = sb.run("--yes")
    assert second.returncode == 0, second.all
    assert (sb.claude_home / "CLAUDE.md.pre-engram").read_bytes() == original
    assert edited in [p.read_bytes() for p in _stamped_backups(sb.claude_home)]


def test_no_backup_is_made_when_bootstrap_is_skipped(sb):
    sb.make_db(6)
    sb.claude_home.mkdir(parents=True)
    (sb.claude_home / "CLAUDE.md").write_text("ENGRAM's own rendering on an installed seat\n")
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert not (sb.claude_home / "CLAUDE.md.pre-engram").exists()


def test_no_claude_md_means_no_backup(sb):
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert not (sb.claude_home / "CLAUDE.md.pre-engram").exists()


# ── Cell 6: --yes never implies --install-claude-code ──────────────────────────────────────
def test_yes_alone_never_runs_the_claude_code_installer(tmp_path):
    sb2 = Sandbox(tmp_path, with_claude=False)
    r = sb2.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all
    assert not sb2.calls_starting("curl")
    assert not sb2.calls_starting("claude-installer-ran")
    assert not (sb2.home / ".local" / "bin" / "claude").exists()


def test_install_claude_code_flag_runs_the_installer_then_rechecks(tmp_path):
    sb2 = Sandbox(tmp_path, with_claude=False)
    r = sb2.run("--yes", "--install-claude-code")
    assert r.returncode == 0, r.all
    curl = sb2.calls_starting("curl")
    assert len(curl) == 1 and curl[0].startswith("curl -fsSL https://claude.ai/install.sh -o ")
    assert sb2.calls_starting("claude-installer-ran")
    assert "claude --version" in sb2.call_lines()
    assert any(l.startswith("[step 2]") and " ... ok" in l for l in r.stdout.splitlines())


def test_installer_that_exits_non_zero_fails_step_2(tmp_path):
    sb2 = Sandbox(tmp_path, with_claude=False)
    r = sb2.run("--yes", "--install-claude-code", env={"FAKE_CLAUDE_INSTALLER_RC": "7"})
    assert r.returncode != 0 and "FAIL: [step 2]" in r.stderr, r.all
    assert not sb2.calls_starting("pip")


def test_flag_with_claude_already_present_does_not_run_the_installer(sb):
    r = sb.run("--yes", "--install-claude-code")
    assert r.returncode == 0, r.all
    assert not sb.calls_starting("curl")


# ── Cell 7: re-run on an installed home ────────────────────────────────────────────────────
def test_rerun_on_an_installed_home_skips_bootstrap_and_exits_zero(sb):
    first = sb.run("--yes")
    assert first.returncode == 0, first.all
    config_after_first = (sb.engram / "config.json").read_text()
    second = sb.run("--yes")
    assert second.returncode == 0, second.all
    assert len(sb.calls_starting("bootstrap")) == 1  # only the first run
    assert "6 nodes" in second.stdout and "skipped" in second.stdout
    assert (sb.engram / "config.json").read_text() == config_after_first
    assert sb.calls_starting("selfcheck") == ["selfcheck --phase pre-first-session", "selfcheck --phase installed"]


# ── Services, flags ─────────────────────────────────────────────────────────────────────────
def test_no_recall_service_skips_the_service_script(sb):
    r = sb.run("--yes", "--no-recall-service")
    assert r.returncode == 0, r.all
    assert not sb.calls_starting("recall")
    assert "not requested" in r.stdout


def test_service_manager_absent_is_a_warning_not_a_failure(tmp_path):
    sb2 = Sandbox(tmp_path, with_systemctl=False)
    r = sb2.run("--yes")
    assert r.returncode == 0, r.all
    assert "WARN: [step 8]" in r.stdout
    assert not sb2.calls_starting("recall")
    assert sb2.calls_starting("selfcheck")


@pytest.mark.skipif(platform.system() != "Linux", reason="--viz-service is Linux only")
def test_viz_service_runs_the_viz_script_on_linux(sb):
    r = sb.run("--yes", "--viz-service")
    assert r.returncode == 0, r.all
    assert sb.calls_starting("viz")


def test_forum_url_runs_set_forum_url_against_the_config(sb):
    r = sb.run("--yes", "--forum-url", "http://forum.lan:5002")
    assert r.returncode == 0, r.all
    assert sb.calls_starting("forum_url") == ["forum_url http://forum.lan:5002 --config %s" % (sb.engram / "config.json")]


def test_forum_url_refusal_fails_step_8(sb):
    r = sb.run("--yes", "--forum-url", "nonsense", env={"FAKE_FORUM_RC": "2"})
    assert r.returncode != 0 and "FAIL: [step 8]" in r.stderr, r.all
    assert not sb.calls_starting("selfcheck")


def test_bad_arguments_exit_2_and_run_nothing(sb):
    for args in (["--bogus"], ["--tier", "huge"], ["--tier"], ["--forum-url"]):
        r = sb.run(*args)
        assert r.returncode == 2, (args, r.all)
    assert sb.call_lines() == []


def test_help_exits_zero_and_lists_every_flag(sb):
    r = sb.run("--help")
    assert r.returncode == 0
    for flag in ("--tier", "--recall-service", "--no-recall-service", "--viz-service",
                 "--forum-url", "--install-claude-code", "--yes", "--help"):
        assert flag in r.stdout
    assert sb.call_lines() == []


def test_the_three_ruled_defaults_are_named_constants_near_the_top():
    head = REAL_INSTALL_SH.read_text().splitlines()[:60]
    text = "\n".join(head)
    assert "DEFAULT_INSTALL_CLAUDE_CODE=0" in text
    assert "DEFAULT_BACKUP_AND_REPLACE_CLAUDE_MD=0" in text
    assert "DEFAULT_RECALL_SERVICE=1" in text


def test_tools_install_sh_is_a_symlink_to_the_source_copy():
    link = REPO_ROOT / "tools" / "install.sh"
    assert link.is_symlink() and os.readlink(link) == "../src/engram/tools/install.sh"


def test_script_parses_under_bash_n():
    assert subprocess.run(["bash", "-n", str(REAL_INSTALL_SH)]).returncode == 0


# ═══ Round 2 (PR #4116 review): the cells below assert the round-2 contract ═══════════════════

# ── 1. Step 7 is gated on the list read-back, not on the exit codes ────────────────────────
def test_step7_passes_when_install_and_enable_exit_nonzero_but_list_shows_enabled(sb):
    r = sb.run("--yes", env={"FAKE_CLAUDE_PLUGIN_MODE": "already"})
    assert r.returncode == 0, r.all
    assert any(l.startswith("[step 7]") and " ... ok" in l for l in r.stdout.splitlines()), r.all
    assert sb.calls_starting("claude plugin list --json")


def test_step7_fails_when_install_and_enable_exit_zero_but_list_shows_not_enabled(sb):
    r = sb.run("--yes", env={"FAKE_CLAUDE_PLUGIN_MODE": "noeffect"})
    assert r.returncode != 0 and "FAIL: [step 7]" in r.stderr, r.all
    assert not any(l.startswith("[step 7]") and l.rstrip().endswith("ok") for l in r.stdout.splitlines())
    assert not sb.calls_starting("recall") and not sb.calls_starting("selfcheck")


# ── 2. A re-run without --tier keeps the stored choices ────────────────────
def _write_config(sb, **kw):
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "config.json").write_text(json.dumps(kw, indent=2) + "\n")


def test_rerun_without_flags_keeps_stored_tier_and_multi_agent_and_step5_gets_them(sb):
    _write_config(sb, install_tier="dev", multi_agent=True, self_lineage="anthropic:opus")
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    after = json.loads((sb.engram / "config.json").read_text())
    assert after["install_tier"] == "dev" and after["multi_agent"] is True
    assert after["self_lineage"] == "anthropic:opus"
    assert sb.calls_starting("market-config") == ["market-config tier=dev"]
    assert len(sb.calls_starting("pip")) == 2  # dev tier still adds requirements-dev
    assert "config: install_tier" not in r.all and "config: multi_agent" not in r.all  # nothing changed


def test_tier_flag_that_changes_a_stored_value_prints_one_change_line(sb):
    _write_config(sb, install_tier="dev", multi_agent=True)
    r = sb.run("--yes", "--tier", "essential")
    assert r.returncode == 0, r.all
    assert [l for l in r.stdout.splitlines() if l.startswith("config: install_tier")] == \
        ["config: install_tier dev -> essential"]
    assert "config: multi_agent" not in r.all  # untouched
    after = json.loads((sb.engram / "config.json").read_text())
    assert after["install_tier"] == "essential" and after["multi_agent"] is True
    assert sb.calls_starting("market-config") == ["market-config tier=essential"]


def test_single_agent_flag_no_longer_switches_a_multi_agent_seat_back(sb):
    _write_config(sb, install_tier="convenience", multi_agent=True)
    r = sb.run("--yes", "--single-agent")
    assert r.returncode == 0, r.all
    assert not [l for l in r.stdout.splitlines() if l.startswith("config: multi_agent")]
    assert json.loads((sb.engram / "config.json").read_text())["multi_agent"] is True
    assert sb.calls_starting("market-config") == ["market-config tier=convenience"]


def test_multi_agent_flag_on_a_single_agent_config_changes_nothing(sb):
    _write_config(sb, install_tier="convenience", multi_agent=False)
    r = sb.run("--yes", "--multi-agent")
    assert r.returncode == 0, r.all
    assert "config: multi_agent" not in r.all
    assert json.loads((sb.engram / "config.json").read_text())["multi_agent"] is False


def test_fresh_config_gets_the_defaults_and_prints_no_change_line(sb):
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert json.loads((sb.engram / "config.json").read_text()) == {"install_tier": "convenience"}
    assert "config: install_tier" not in r.all and "config: multi_agent" not in r.all
    assert sb.calls_starting("market-config") == ["market-config tier=convenience"]


def test_help_documents_the_keep_rule(sb):
    r = sb.run("--help")
    assert r.returncode == 0 and "--single-agent" not in r.stdout
    assert "kept" in r.stdout.lower()


# ── 3. config.json rewrite: non-ASCII survives ──────────────────────────────────────────────
def test_non_ascii_value_in_an_unrelated_key_survives_a_rewrite_as_the_same_characters(sb):
    sb.engram.mkdir(parents=True, exist_ok=True)
    value = "café — 日本"
    text = json.dumps({"install_tier": "dev", "multi_agent": False, "note": value},
                      indent=2, ensure_ascii=False) + "\n"
    (sb.engram / "config.json").write_bytes(text.encode("utf-8"))
    r = sb.run("--yes", "--tier", "essential")  # forces a rewrite
    assert r.returncode == 0, r.all
    raw = (sb.engram / "config.json").read_bytes().decode("utf-8")
    assert value in raw and "\\u" not in raw
    assert json.loads(raw)["install_tier"] == "essential"


def test_script_does_not_claim_byte_for_byte_preservation():
    assert "byte-for-byte" not in REAL_INSTALL_SH.read_text().lower()


# ── 4. Step 8: a service manager with no usable user session is a WARN ─────────────────────
@pytest.mark.skipif(platform.system() != "Linux", reason="the systemd user-session probe is Linux only")
def test_systemctl_present_but_no_user_session_is_a_warn_with_the_exact_later_command(sb):
    r = sb.run("--yes", env={"FAKE_SYSTEMCTL_USER_RC": "1"})
    assert r.returncode == 0, r.all
    warn = [l for l in r.stdout.splitlines() if l.startswith("WARN: [step 8]")]
    assert len(warn) == 1 and "recall service not installed: " in warn[0], r.all
    later = "bash %s" % (sb.engram / "marketplace" / "plugins" / "engram" / "tools" / "setup-surface-daemon-service.sh")
    assert "install it later with " + later in warn[0], warn[0]
    assert not sb.calls_starting("recall")
    assert sb.calls_starting("selfcheck")  # the run continued


@pytest.mark.skipif(platform.system() != "Linux", reason="the systemd user-session probe is Linux only")
def test_service_script_failing_after_a_passing_probe_is_a_fail_naming_no_recall_service(sb):
    r = sb.run("--yes", env={"FAKE_RECALL_RC": "1"})
    assert r.returncode != 0 and "FAIL: [step 8]" in r.stderr, r.all
    assert "--no-recall-service" in r.stderr
    assert "WARN: [step 8]" not in r.stdout
    assert sb.calls_starting("recall") and not sb.calls_starting("selfcheck")


# ── 5. Logs survive a failure ───────────────────────────────────────────────────────────────
def test_pip_failure_leaves_the_log_at_the_printed_path_with_the_pips_output(sb):
    import re
    r = sb.run("--yes", env={"FAKE_PIP_RC": "1"})
    assert r.returncode != 0 and "FAIL: [step 4]" in r.stderr, r.all
    fail_line = [l for l in r.stderr.splitlines() if l.startswith("FAIL: [step 4]")][0]
    m = re.search(r"(%s/install-logs/[^\s/]+)/pip\.log" % re.escape(str(sb.engram)), fail_line)
    assert m, fail_line  # the FAIL line itself names the surviving path
    logdir = Path(m.group(1))
    assert "fake-pip-output-marker" in (logdir / "pip.log").read_text()
    assert not list(sb.root.glob("engram-install.*"))  # the temp dir is still removed


def test_a_successful_run_leaves_no_install_logs_behind(sb):
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert not (sb.engram / "install-logs").exists()
    assert not list(sb.root.glob("engram-install.*"))


# ── 6. Preflight: a dangling CLAUDE.md symlink fails at step 1 ──────────────────────────────
def test_dangling_claude_md_symlink_fails_step_1_before_any_real_work(sb):
    sb.claude_home.mkdir(parents=True)
    (sb.claude_home / "CLAUDE.md").symlink_to(sb.root / "nowhere" / "CLAUDE.md")
    r = sb.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all
    assert str(sb.claude_home / "CLAUDE.md") in r.stderr and "move the link aside" in r.stderr.lower()
    for prefix in ("pip", "marketplace", "bootstrap", "claude plugin", "selfcheck"):
        assert not sb.calls_starting(prefix), sb.call_lines()
    assert (sb.claude_home / "CLAUDE.md").is_symlink()  # untouched


def test_live_claude_md_symlink_still_has_its_target_bytes_backed_up(sb):
    sb.use_real_bootstrap()
    sb.claude_home.mkdir(parents=True)
    target = sb.root / "dotfiles" / "CLAUDE.md"
    target.parent.mkdir()
    original = b"my dotfile rules \xe2\x80\x94 kept\n"
    target.write_bytes(original)
    (sb.claude_home / "CLAUDE.md").symlink_to(target)
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    backup = sb.claude_home / "CLAUDE.md.pre-engram"
    assert backup.read_bytes() == original and not backup.is_symlink()


def test_header_states_the_step_1_no_fetch_gap():
    """fold2 item 6 (PR #4123): step 1 never fetches, so stale origin/* refs are refused there.
    The installer's header comment is its install doc; it must say so and name the remedy."""
    head = REAL_INSTALL_SH.read_text().split("set -euo pipefail", 1)[0]
    assert "step 1 never fetches" in head
    assert "stale" in head and "origin/*" in head
    assert "--branch" in head and "shallow" in head
    assert "git fetch --tags origin" in head


# ═══ #4255 slice 1: the --multi-agent / --single-agent axis is retired ═════════════════════════
# Contract: S1.1 (both flags are accepted and ignored: same exit status, exactly one stderr line
# naming the flag given, identical config.json), S1.7 (install.sh never writes, rewrites or deletes
# `multi_agent`; the PASS line and the :643 hint drop the axis).
def _retired(flag):
    return "%s is retired and has no effect (#4255)" % flag


def _twin(tmp_path):
    return Sandbox(tmp_path / "plain"), Sandbox(tmp_path / "flagged")


@pytest.mark.parametrize("flag", ["--multi-agent", "--single-agent"])
def test_retired_flag_is_ignored_with_one_stderr_line_and_the_same_config(flag, tmp_path):
    plain, flagged = _twin(tmp_path)
    a = plain.run("--yes", env={"LC_ALL": "C"})
    b = flagged.run("--yes", flag, env={"LC_ALL": "C"})
    assert a.returncode == b.returncode == 0, a.all + b.all
    assert b.stderr.splitlines().count(_retired(flag)) == 1, b.stderr
    assert [l for l in b.stderr.splitlines() if "retired" in l] == [_retired(flag)]
    assert [l for l in a.stderr.splitlines() if "retired" in l] == []
    assert (flagged.engram / "config.json").read_bytes() == (plain.engram / "config.json").read_bytes()
    strip = lambda t: [l for l in t.splitlines() if not l.startswith("[step 1]")]
    # Step 4 prints install-logs/<UTC timestamp to the second>/pip.log, and the two runs are
    # sequential: unmasked, the cell fails whenever they straddle a second boundary (#4260 round 1).
    norm = lambda root, t: [re.sub(r"\d{8}T\d{6}Z", "TS", l.replace(str(root), "ROOT")) for l in strip(t)]
    assert norm(flagged.root, b.stdout) == norm(plain.root, a.stdout)


def test_both_flags_and_repeats_print_one_line_per_distinct_flag(tmp_path):
    sb2 = Sandbox(tmp_path / "x")
    r = sb2.run("--yes", "--multi-agent", "--single-agent", "--multi-agent")
    assert r.returncode == 0, r.all
    got = [l for l in r.stderr.splitlines() if "retired" in l]
    assert sorted(got) == sorted([_retired("--multi-agent"), _retired("--single-agent")]), r.stderr
    assert got[0] == _retired("--multi-agent")  # first-given order


@pytest.mark.parametrize("flag", ["--multi-agent", "--single-agent"])
def test_retired_flag_does_not_change_the_exit_status_of_a_refusal(flag, tmp_path):
    plain, flagged = _twin(tmp_path)
    for s in (plain, flagged):
        s.engram.mkdir(parents=True, exist_ok=True)
        (s.engram / "config.json").write_text("{not json")
    a = plain.run("--yes")
    b = flagged.run("--yes", flag)
    assert a.returncode == b.returncode != 0
    assert "FAIL: [step 3]" in a.stderr and "FAIL: [step 3]" in b.stderr
    assert b.stderr.splitlines().count(_retired(flag)) == 1
    assert (flagged.engram / "config.json").read_text() == "{not json"  # refusal leaves it byte-identical


def test_retired_flag_with_a_failing_pip_fails_the_same_way(tmp_path):
    plain, flagged = _twin(tmp_path)
    a = plain.run("--yes", env={"FAKE_PIP_RC": "1"})
    b = flagged.run("--yes", "--multi-agent", env={"FAKE_PIP_RC": "1"})
    assert a.returncode == b.returncode != 0
    assert "FAIL: [step 4]" in a.stderr and "FAIL: [step 4]" in b.stderr


def test_help_does_not_list_either_retired_flag(sb):
    r = sb.run("--help")
    assert r.returncode == 0
    assert "multi-agent" not in r.stdout.lower() and "single-agent" not in r.stdout.lower()
    assert "multi_agent" not in r.stdout
    head = REAL_INSTALL_SH.read_text().split("set -euo pipefail", 1)[0]
    assert "multi-agent" not in head and "single-agent" not in head


def test_retired_flag_from_an_empty_cwd_by_absolute_path(sb):
    r = sb.run("--yes", "--multi-agent")  # Sandbox.run already runs from sb.root, not the repo
    assert r.returncode == 0, r.all
    assert Path(sb.root) != REPO_ROOT and r.stderr.splitlines().count(_retired("--multi-agent")) == 1


@pytest.mark.parametrize("mask", [0o077, 0o002])
def test_config_file_mode_is_the_same_with_and_without_the_retired_flag(mask, tmp_path):
    plain, flagged = _twin(tmp_path)
    old = os.umask(mask)
    try:
        assert plain.run("--yes").returncode == 0
        assert flagged.run("--yes", "--multi-agent").returncode == 0
    finally:
        os.umask(old)
    mode = lambda s: (s.engram / "config.json").stat().st_mode & 0o777
    assert mode(plain) == mode(flagged) == (0o666 & ~mask)


def test_fresh_config_has_install_tier_only(sb):
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert json.loads((sb.engram / "config.json").read_text()) == {"install_tier": "convenience"}


@pytest.mark.parametrize("flags", [(), ("--multi-agent",), ("--single-agent",)])
def test_existing_multi_agent_and_mode_keys_stay_byte_identical(flags, sb):
    raw = ('{\n  "self_lineage": "anthropic:opus",\n  "install_tier": "convenience",\n'
           '  "multi_agent": true,\n  "mode": "multi",\n  "other": [1, 2]\n}\n')
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "config.json").write_text(raw)
    r = sb.run("--yes", *flags)
    assert r.returncode == 0, r.all
    assert (sb.engram / "config.json").read_text() == raw  # nothing changed, nothing rewritten


@pytest.mark.parametrize("flags", [(), ("--multi-agent",), ("--single-agent",)])
def test_a_tier_change_rewrites_the_file_but_not_the_two_keys(flags, sb):
    raw = ('{\n  "install_tier": "dev",\n  "multi_agent": true,\n  "mode": "multi",\n'
           '  "unrelated": {"a": 1}\n}\n')
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "config.json").write_text(raw)
    r = sb.run("--yes", "--tier", "essential", *flags)
    assert r.returncode == 0, r.all
    text = (sb.engram / "config.json").read_text()
    assert '  "multi_agent": true,\n' in text and '  "mode": "multi"' in text
    assert list(json.loads(text)) == ["install_tier", "multi_agent", "mode", "unrelated"]
    assert "config: install_tier dev -> essential" in r.stdout.splitlines()
    assert "config: multi_agent" not in r.all


def test_a_stored_non_bool_multi_agent_is_also_left_alone(sb):
    raw = '{"install_tier": "convenience", "multi_agent": "yes", "mode": 7}'
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "config.json").write_text(raw)
    r = sb.run("--yes", "--tier", "dev")
    assert r.returncode == 0, r.all
    after = json.loads((sb.engram / "config.json").read_text())
    assert after["multi_agent"] == "yes" and after["mode"] == 7 and after["install_tier"] == "dev"


@pytest.mark.parametrize("flags", [(), ("--multi-agent",), ("--single-agent",)])
def test_an_existing_config_without_multi_agent_is_not_rewritten_nor_given_the_key(flags, sb):
    raw = '{"self_lineage":   "anthropic:opus",   "install_tier": "convenience"}'
    sb.engram.mkdir(parents=True, exist_ok=True)
    (sb.engram / "config.json").write_text(raw)
    r = sb.run("--yes", *flags)
    assert r.returncode == 0, r.all
    assert (sb.engram / "config.json").read_text() == raw  # byte-identical: no key added, no reformat


def test_pass_line_for_step_3_does_not_print_multi_agent(sb):
    r = sb.run("--yes")
    line = [l for l in r.stdout.splitlines() if l.startswith("[step 3]") and " ... ok" in l]
    assert line and "multi_agent" not in line[0] and "install_tier=convenience" in line[0], r.stdout


def test_forum_url_hint_names_the_tier_not_the_retired_flag(sb):
    (sb.plugin_src / "tools" / "set_forum_url.py").unlink()
    r = sb.run("--yes", "--forum-url", "http://forum.lan:5002")
    assert r.returncode != 0 and "FAIL: [step 8]" in r.stderr, r.all
    assert "--multi-agent" not in r.stderr
    assert "convenience or dev" in r.stderr
