"""agent-bootstrap runs tools/install.sh as the newborn (#4097, slice 5).

Contract cells B1-B8. agent-bootstrap is a root-only script that creates Linux users, so
NOTHING here runs it: the script is SOURCED in a bash subprocess with
``AGENT_BOOTSTRAP_LIB_ONLY=1`` (it stops after defining its functions) and the install phase
is driven with a fake ``sudo`` on PATH, real ``git`` against a throwaway local repo, and a fake
``tools/install.sh``. No user is created, nothing outside tmp_path is touched, no network.

  B1  no venv/pip in agent-bootstrap (the .bashrc auto-activation block is KEPT)
  B2  no code path creates a ``claude`` symlink
  B3  install.sh runs as the newborn via ``sudo -u agent-NAME -H``, with the pass-through flags,
      never --allow-untagged / --allow-branch; arg parse accepts --tier/--forum-url/--no-install
      (--multi-agent is retired: #4255 slice 4, tests/test_tools_retired_axis_4255s4.py)
  B4  the clone lands on the newest deployable tag that is an ancestor of origin/dev; none -> FAIL.
      The clone alone runs with GIT_CONFIG_GLOBAL -> a temp file holding exactly one
      safe.directory entry for the source's .git, removed after the clone (round 2)
  B5  install.sh's exit code is agent-bootstrap's; the user/home are left in place, said so
  B6  agentctl spawn passes --tier dev (and --forum-url when it was given)
  B7  finalize-name step 2d repairs the shape a native install leaves
  B8  next-steps block
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "tools" / "agent-bootstrap"
AGENTCTL = REPO / "tools" / "agentctl"
SRC = BOOTSTRAP.read_text()


def _code_lines(text: str) -> list[str]:
    """Non-comment, non-blank lines (heredoc bodies included: they are code too)."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


CODE = "\n".join(_code_lines(SRC))


# ---------------------------------------------------------------------------------------------
# B1, B2: static cells
# ---------------------------------------------------------------------------------------------
class TestB1NoVenvNoPip:
    def test_no_venv_creation_or_pip(self):
        assert "-m venv" not in CODE
        assert "venv/bin/pip" not in CODE
        assert not re.search(r"\bpip3?\b", CODE), "agent-bootstrap must not run pip itself"
        assert "VENV_CREATED" not in CODE
        assert "requirements.txt" not in CODE

    def test_bashrc_venv_activation_block_is_kept(self):
        # Decision: kept. It is guarded by [[ -f activate ]] so it is inert until install.sh
        # creates the venv, and it makes the newborn's interactive shells use that venv.
        assert 'if [[ -f "$HOME/.engram/venv/bin/activate" ]]; then' in SRC
        assert "Auto-activate ENGRAM venv" in SRC


class TestB2NoClaudeSymlink:
    def test_no_claude_symlink_and_no_foreign_claude_paths(self):
        for needle in ("/usr/local/bin/claude", "/opt/claude/bin/claude",
                       "/home/agent-*/.local/bin/claude", "CLAUDE_SOURCE"):
            assert needle not in CODE, needle
        for ln in _code_lines(SRC):
            if re.search(r"\bln\b", ln):
                assert "claude" not in ln, ln

    def test_billing_leak_guards_byte_identical(self):
        assert "exec env -u ANTHROPIC_API_KEY claude \"$@\"" in SRC
        assert "export ANTHROPIC_API_KEY" not in CODE


# ---------------------------------------------------------------------------------------------
# fixtures: sourced script + fakes
# ---------------------------------------------------------------------------------------------
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

FAKE_SUDO = """#!/bin/bash
# fake sudo: log the call, require `-u USER -H`, drop them, run the rest as the test user.
echo "sudo $*" >> "$SUDO_LOG"
[ "$1" = "-u" ] && [ "$3" = "-H" ] || { echo "fake sudo: unexpected shape: $*" >&2; exit 99; }
shift 3
exec "$@"
"""

FAKE_INSTALL = """#!/bin/bash
{
  echo "argv: $*"
  echo "HOME=$HOME"
  echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY-<unset>}"
  echo "describe=$(git -C "$(dirname "$0")/.." describe --tags --exact-match HEAD 2>&1)"
} >> "$INSTALL_LOG"
if [ "${FAKE_INSTALL_RC:-0}" != "0" ]; then
  echo "FAIL: [step 4] fake step: it broke — fix it and re-run" >&2
  exit "$FAKE_INSTALL_RC"
fi
echo "[step 10] self-check ... ok"
exit 0
"""


GIT_WRAP = """#!/bin/bash
# recording git: log argv + the GIT_CONFIG_GLOBAL this invocation saw; at clone time snapshot
# the trust file (content and modes) because agent-bootstrap removes it right after.
echo "git $* ||GCG=${GIT_CONFIG_GLOBAL-<unset>}" >> "$GIT_LOG"
if [ "$1" = clone ] && [ -n "${GIT_CLONE_SLEEP-}" ]; then
  echo up > "$GIT_LOG.clone-started"; sleep "$GIT_CLONE_SLEEP"
fi
if [ "$1" = clone ] && [ -n "${GIT_CONFIG_GLOBAL-}" ]; then
  cp "$GIT_CONFIG_GLOBAL" "$GIT_LOG.cfg"
  echo "file=$(stat -c %a "$GIT_CONFIG_GLOBAL" 2>/dev/null || stat -f %Lp "$GIT_CONFIG_GLOBAL") dir=$(stat -c %a "$(dirname "$GIT_CONFIG_GLOBAL")" 2>/dev/null || stat -f %Lp "$(dirname "$GIT_CONFIG_GLOBAL")")" > "$GIT_LOG.modes"
  echo "$GIT_CONFIG_GLOBAL" > "$GIT_LOG.path"
fi
exec REALGIT "$@"
"""


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ, **GIT_ENV)
    if date:
        env["GIT_COMMITTER_DATE"] = date
        env["GIT_AUTHOR_DATE"] = date
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env=env, check=True)
    return r.stdout.strip()


def _commit(repo: Path, name: str, date: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name, date=date)
    return _git(repo, "rev-parse", "HEAD")


def make_source(tmp_path: Path, tags: dict[str, str] | None = None, unmerged_tag: str | None = None,
                untagged_head: bool = True, installer_at: str = "c1") -> Path:
    """A local 'engram-alpha' source: branch dev with c1..c3, optional tags on c1/c2, an unmerged
    side commit carrying ``unmerged_tag``, and (default) dev's HEAD untagged — the measured
    state of the host's shared checkout."""
    src = tmp_path / "source"
    src.mkdir()
    _git(src, "init", "-q", "-b", "dev")
    (src / "tools").mkdir()
    inst = src / "tools" / "install.sh"

    def add_installer(at: str) -> None:
        # ``installer_at`` names the first commit that carries tools/install.sh (r3: a tag cut
        # before the scripted installer existed has none).
        if installer_at == at:
            inst.write_text(FAKE_INSTALL)
            inst.chmod(0o755)

    add_installer("c1")
    c1 = _commit(src, "c1", "2026-01-01T00:00:00Z")
    add_installer("c2")
    c2 = _commit(src, "c2", "2026-02-01T00:00:00Z")
    add_installer("c3")
    c3 = _commit(src, "c3", "2026-03-01T00:00:00Z")
    at = {"c1": c1, "c2": c2, "c3": c3}
    for tag, where in (tags or {}).items():
        _git(src, "tag", tag, at[where])
    if unmerged_tag:
        _git(src, "checkout", "-q", "-b", "side", c3)
        side = _commit(src, "side", "2026-04-01T00:00:00Z")
        _git(src, "tag", unmerged_tag, side)
        _git(src, "checkout", "-q", "dev")
    if not untagged_head:
        _git(src, "tag", "v9.0.0", c3)
    return src


def make_env(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sudo = bindir / "sudo"
    sudo.write_text(FAKE_SUDO)
    sudo.chmod(0o755)
    gitw = bindir / "git"
    gitw.write_text(GIT_WRAP.replace("REALGIT", shutil.which("git")))
    gitw.chmod(0o755)
    tmpd = tmp_path / "tmpd"
    tmpd.mkdir()
    home = tmp_path / "home" / "agent-alice"
    home.mkdir(parents=True)
    hermetic_home = tmp_path / "hermetic"
    hermetic_home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "ANTHROPIC_", "ENGRAM", "AGENT_"))}
    env.update(GIT_ENV)
    env.update({
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(hermetic_home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "SUDO_LOG": str(tmp_path / "sudo.log"),
        "GIT_LOG": str(tmp_path / "git.log"),
        "TMPDIR": str(tmpd),
        "INSTALL_LOG": str(tmp_path / "install.log"),
        "AGENT_BOOTSTRAP_LIB_ONLY": "1",
        "ANTHROPIC_API_KEY": "sk-should-be-scrubbed",
    })
    return env, home


def run_lib(env: dict, body: str) -> subprocess.CompletedProcess:
    """Source agent-bootstrap in lib-only mode (it must not create anything) and run ``body``."""
    script = f'set -u\nsource "{BOOTSTRAP}"\n{body}\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                          timeout=120)


def install_body(home: Path, src: Path, extra: str = "") -> str:
    return (f'USER_NAME="agent-alice"; USER_HOME="{home}"; ALPHA_SOURCE="{src}"\n'
            f'TIER=""; FORUM_URL=""\n{extra}\n'
            'rc=0; ab_clone_and_install || rc=$?; echo "RC=$rc"; echo "TAG=${CLONE_TAG:-}"')


def _rc(proc) -> int:
    m = re.search(r"^RC=(\d+)$", proc.stdout, re.M)
    assert m, proc.stdout + proc.stderr
    return int(m.group(1))


def test_lib_only_mode_creates_nothing_and_is_not_root_gated(tmp_path):
    env, home = make_env(tmp_path)
    proc = run_lib(env, 'echo LOADED')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "LOADED" in proc.stdout
    assert not (tmp_path / "sudo.log").exists()


# ---------------------------------------------------------------------------------------------
# B3: arg parse + run as newborn
# ---------------------------------------------------------------------------------------------
class TestB3RunsAsNewborn:
    def test_arg_parse_passthrough_flags(self, tmp_path):
        env, _ = make_env(tmp_path)
        proc = run_lib(env, 'ab_parse_args nb --runtime claude --tier dev '
                            '--forum-url http://f.example:5002 --no-install\n'
                            'echo "N=$NAME T=$TIER F=$FORUM_URL I=$NO_INSTALL"')
        assert proc.returncode == 0, proc.stderr
        assert "N=nb T=dev F=http://f.example:5002 I=1" in proc.stdout

    def test_arg_parse_defaults_install_on_and_flags_empty(self, tmp_path):
        env, _ = make_env(tmp_path)
        proc = run_lib(env, 'ab_parse_args nb --runtime claude\n'
                            'echo "T=[$TIER] F=[$FORUM_URL] I=$NO_INSTALL"')
        assert "T=[] F=[] I=0" in proc.stdout, proc.stderr

    def test_arg_parse_rejects_bad_tier(self, tmp_path):
        env, _ = make_env(tmp_path)
        proc = run_lib(env, 'rc=0; ( ab_parse_args nb --runtime claude --tier bogus ) || rc=$?; echo "RC=$rc"')
        assert _rc(proc) == 2
        assert "tier" in proc.stderr

    def test_install_runs_via_sudo_u_newborn_h_with_flags(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(
            home, src, 'TIER=dev; FORUM_URL=http://f.example:5002'))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        sudo_lines = (tmp_path / "sudo.log").read_text().splitlines()
        assert sudo_lines, "nothing ran through sudo"
        # EVERY privileged-shape call is `sudo -u agent-alice -H ...`; none runs as root.
        assert all(ln.startswith("sudo -u agent-alice -H ") for ln in sudo_lines), sudo_lines
        install_calls = [ln for ln in sudo_lines if "install.sh" in ln]
        assert len(install_calls) == 1, sudo_lines
        call = install_calls[0]
        assert f"HOME={home}" in call
        assert "--install-claude-code --yes" in call
        assert "--tier dev" in call and "--multi-agent" not in call
        assert "--forum-url http://f.example:5002" in call
        assert "--allow-untagged" not in call and "--allow-branch" not in call
        log = (tmp_path / "install.log").read_text()
        assert f"HOME={home}" in log
        assert "ANTHROPIC_API_KEY=<unset>" in log, "install.sh must not inherit an API key"
        assert str(home / "engram-alpha") in call

    def test_flags_absent_when_not_given(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        call = [ln for ln in (tmp_path / "sudo.log").read_text().splitlines() if "install.sh" in ln][0]
        for flag in ("--tier", "--forum-url"):
            assert flag not in call

    def test_no_override_flags_and_no_persistent_git_trust_anywhere_in_script(self):
        assert "--allow-untagged" not in CODE
        assert "--allow-branch" not in CODE
        # Round 2: a scoped, temp-file safe.directory entry is the contract, so the literal
        # "safe.directory" is no longer forbidden. What is forbidden is every PERSISTENT way to
        # write one. Discriminates: `git config --global|--system|--add safe.directory X`,
        # `-c safe.directory=`, or touching a *gitconfig file all add a line matching below.
        assert not re.search(r"\bgit\b[^\n]*\bconfig\b", CODE), "no `git config` in code"
        assert "--global" not in CODE and "--system" not in CODE
        assert not re.search(r"safe\.directory\s*=\s*\*", CODE)
        gitconfig_mentions = [ln for ln in CODE.splitlines() if "gitconfig" in ln]
        assert all(ln.strip() == 'cfg="$cfgdir/gitconfig"' for ln in gitconfig_mentions), \
            gitconfig_mentions
        # the single trust-entry writer: [safe] printf redirected to the temp file, "$cfg" only
        writers = [ln for ln in CODE.splitlines() if "[safe]" in ln]
        assert len(writers) == 1 and writers[0].strip().startswith("printf ") \
            and '> "$cfg"' in writers[0], writers
        # GIT_CONFIG_GLOBAL is set once, on the env prefix of the clone command only
        env_sets = [ln for ln in CODE.splitlines() if "GIT_CONFIG_GLOBAL" in ln]
        assert len(env_sets) == 1 and "git clone" in env_sets[0] \
            and "ab_as_newborn env" in env_sets[0], env_sets

    def test_install_not_run_as_root_in_code(self):
        # every install.sh invocation in code goes through the ab_as_newborn helper
        logical = CODE.replace("\\\n", " ")  # join backslash-continued lines
        hits = [ln for ln in logical.splitlines()
                if re.search(r"\bbash\s+\"?\$.*install\.sh", ln)]
        assert hits, "no install.sh invocation found"
        for ln in hits:
            assert "ab_as_newborn" in ln, ln


# ---------------------------------------------------------------------------------------------
# B4: the clone and the tag
# ---------------------------------------------------------------------------------------------
class TestB4CloneOnDeployableTag:
    def test_clone_lands_on_newest_tag_not_dev_head(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c1", "deploy/2026-02-01-x": "c2"},
                          unmerged_tag="v9.9.9")
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        clone = home / "engram-alpha"
        # newest by commit date among tags that are ancestors of origin/dev: the deploy tag at
        # c2, NOT the older v0.1.0, NOT the unmerged v9.9.9, NOT dev's untagged c3 head.
        assert _git(clone, "describe", "--tags", "--exact-match", "HEAD") == "deploy/2026-02-01-x"
        assert _git(clone, "rev-parse", "HEAD") == _git(src, "rev-parse", "dev~1")
        assert _git(clone, "rev-parse", "HEAD") != _git(src, "rev-parse", "dev")
        assert "describe=deploy/2026-02-01-x" in (tmp_path / "install.log").read_text()
        # clone, not hardlinks
        objs = [p for p in (clone / ".git" / "objects").rglob("*") if p.is_file()]
        assert objs and all(p.stat().st_nlink == 1 for p in objs)
        assert _git(clone, "remote", "get-url", "origin") == str(src)

    def test_v_tag_wins_a_same_date_tie(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"deploy/2026-02-01-x": "c2", "v0.2.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        assert "TAG=v0.2.0" in proc.stdout

    def test_unmerged_tag_alone_is_not_deployable(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, unmerged_tag="v9.9.9")
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) != 0
        assert "no deployable tag" in proc.stderr
        assert not (tmp_path / "install.log").exists(), "install.sh must not run without a tag"

    def test_no_tags_at_all_fails_without_falling_back_to_head(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path)
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) != 0
        assert "no deployable tag" in proc.stderr
        assert "ancestor of origin/dev" in proc.stderr
        assert not (tmp_path / "install.log").exists()
        # fold2 item 4: the same advance-local-dev remedy as the no-install.sh message
        assert "local dev" in proc.stderr
        assert f"git -C {src} checkout dev && git -C {src} pull --ff-only" in proc.stderr
        assert "detached at a tag" in proc.stderr

    def test_non_deployable_tag_pattern_ignored(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"release-1": "c2", "nightly/x": "c1"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) != 0
        assert not (tmp_path / "install.log").exists()

    # ---- round 3: the picked tag may predate tools/install.sh ----
    def test_tag_without_install_sh_fails_before_running_anything(self, tmp_path):
        env, home = make_env(tmp_path)
        # v0.1.0 at c2 is the newest deployable tag; install.sh first appears at c3 (untagged head).
        src = make_source(tmp_path, tags={"v0.1.0": "c2"}, installer_at="c3")
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) != 0, proc.stdout + proc.stderr
        assert not (tmp_path / "install.log").exists(), "install.sh must not be invoked"
        assert not [ln for ln in (tmp_path / "sudo.log").read_text().splitlines()
                    if "install.sh" in ln], "no sudo'd install.sh invocation"
        # G2: the picked tag is named, on stdout (before the check) and in the message
        assert "checked out tag v0.1.0" in proc.stdout
        assert "v0.1.0" in proc.stderr
        assert "predates the scripted installer" in proc.stderr
        # G1: three remedies, in order
        a = proc.stderr.find(f"git -C {src} fetch --tags origin")
        b = proc.stderr.find("release tag containing tools/install.sh")
        s = proc.stderr.find("--sandbox-build-ref <ref>", b)  # S1: sandbox/test rig only
        c = proc.stderr.find("--no-install", s)
        assert 0 <= a < b < s < c, proc.stderr
        assert "sandbox" in proc.stderr[s:c].lower()
        assert "NOT done by agent-bootstrap" in proc.stderr
        # fold2 item 4: the tag must also be reachable from the source's LOCAL dev/main
        assert "local dev" in proc.stderr[a:b]
        assert f"git -C {src} checkout dev && git -C {src} pull --ff-only" in proc.stderr[a:b]
        assert "detached at a tag" in proc.stderr[a:b]
        # the existing left-in-place note
        assert "left in place" in proc.stderr and "userdel -r agent-alice" in proc.stderr

    def test_tag_with_install_sh_proceeds_as_before(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"}, installer_at="c2")
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        assert "predates the scripted installer" not in proc.stderr
        assert (tmp_path / "install.log").exists()

    def test_g3_no_git_fetch_or_pull_ever_runs(self, tmp_path):
        for installer_at, expect_fail in (("c1", False), ("c3", True)):
            sub = tmp_path / installer_at
            sub.mkdir()
            env, home = make_env(sub)
            src = make_source(sub, tags={"v0.1.0": "c2"}, installer_at=installer_at)
            proc = run_lib(env, install_body(home, src))
            assert (_rc(proc) != 0) == expect_fail, proc.stdout + proc.stderr
            log = (sub / "git.log").read_text()
            assert not re.search(r"^git (-C \S+ )?(fetch|pull|ls-remote|remote update)\b", log, re.M), log

    def test_g3_static_no_network_git_verb_in_code(self):
        for ln in _code_lines(SRC):
            if ln.lstrip().startswith("echo"):
                continue  # the operator-facing remedy text names `fetch`; it is not run
            assert not re.search(r"\bgit\b.*\b(fetch|pull|ls-remote)\b|remote\s+update", ln), ln

    # ---- round 2: scoped trust file for the clone (source owned by another uid) ----
    def _gitlog(self, tmp_path):
        return (tmp_path / "git.log").read_text().splitlines()

    def test_clone_invocation_carries_git_config_global_via_env_as_newborn(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        clones = [ln for ln in self._gitlog(tmp_path) if ln.startswith("git clone ")]
        assert len(clones) == 1, clones
        assert "GCG=<unset>" not in clones[0] and "GCG=" in clones[0]
        sudo_clone = [ln for ln in (tmp_path / "sudo.log").read_text().splitlines()
                      if " git clone " in ln]
        assert len(sudo_clone) == 1 and sudo_clone[0].startswith(
            "sudo -u agent-alice -H env GIT_CONFIG_GLOBAL="), sudo_clone

    def test_trust_file_is_exactly_one_entry_for_the_source_dot_git(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        content = (tmp_path / "git.log.cfg").read_text()
        assert content == f'[safe]\n\tdirectory = "{src}/.git"\n', repr(content)  # fold2: quoted
        assert "*" not in content
        assert f'directory = "{src}"\n' not in content  # not the parent dir

    def test_trust_file_is_world_readable_in_a_traversable_dir(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        run_lib(env, install_body(home, src))
        assert (tmp_path / "git.log.modes").read_text().strip() == "file=644 dir=755"

    def test_trust_file_removed_after_a_successful_clone(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        recorded = Path((tmp_path / "git.log.path").read_text().strip())
        assert not recorded.exists() and not recorded.parent.exists()
        assert list((tmp_path / "tmpd").iterdir()) == []

    def test_trust_file_removed_after_a_failing_clone(self, tmp_path):
        env, home = make_env(tmp_path)
        proc = run_lib(env, install_body(home, tmp_path / "missing"))
        assert _rc(proc) != 0
        recorded = Path((tmp_path / "git.log.path").read_text().strip())  # clone DID run
        assert not recorded.exists() and not recorded.parent.exists()
        assert list((tmp_path / "tmpd").iterdir()) == []

    def test_post_clone_git_commands_do_not_carry_git_config_global(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        later = [ln for ln in self._gitlog(tmp_path) if not ln.startswith("git clone ")]
        # tag --list, merge-base, log, checkout, plus install.sh's own describe
        assert any(" tag --list" in ln for ln in later) and any(" checkout " in ln for ln in later), later
        assert all(ln.endswith("GCG=<unset>") for ln in later), later
        install_call = [ln for ln in (tmp_path / "sudo.log").read_text().splitlines()
                        if "install.sh" in ln][0]
        assert "GIT_CONFIG_GLOBAL" not in install_call

    def test_nothing_written_to_a_persistent_git_config(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        for base in (home, tmp_path / "hermetic"):
            assert not (base / ".gitconfig").exists(), base
            assert not (base / ".config" / "git").exists(), base

    def test_clone_failure_is_nonzero_and_names_the_source(self, tmp_path):
        env, home = make_env(tmp_path)
        proc = run_lib(env, install_body(home, tmp_path / "missing"))
        assert _rc(proc) != 0
        assert str(tmp_path / "missing") in proc.stderr
        assert not (tmp_path / "install.log").exists()


# ---------------------------------------------------------------------------------------------
# B5: exit code propagation
# ---------------------------------------------------------------------------------------------
class TestB5ExitCode:
    def test_install_failure_code_is_returned_with_fail_line_and_remedy(self, tmp_path):
        env, home = make_env(tmp_path)
        env["FAKE_INSTALL_RC"] = "7"
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 7, proc.stdout + proc.stderr
        assert "FAIL: [step 4] fake step" in proc.stderr, "install.sh's FAIL line must be shown"
        assert "exited 7" in proc.stderr
        assert "userdel -r agent-alice" in proc.stderr
        assert "left in place" in proc.stderr
        assert str(home / ".engram" / "install-logs") in proc.stderr

    def test_main_exits_with_that_code(self):
        assert re.search(r"ab_clone_and_install\s*\|\|\s*exit\s+\$\?", CODE)

    def test_no_install_skips_everything(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src, 'NO_INSTALL=1'))
        # with --no-install the main flow never calls ab_clone_and_install; main guards on it.
        assert re.search(r'\[\[ "\$NO_INSTALL" == "1" \]\]|\$NO_INSTALL" -eq 1|NO_INSTALL:-0\}" != "1"', SRC)
        assert proc.returncode == 0


# ---------------------------------------------------------------------------------------------
# B8: next-steps
# ---------------------------------------------------------------------------------------------
class TestB8NextSteps:
    def _steps(self, tmp_path, no_install):
        env, home = make_env(tmp_path)
        proc = run_lib(env, (f'NAME=nb; USER_NAME=agent-alice; USER_HOME=/home/agent-alice; RUNTIME=claude\n'
                             f'HOME_MODE=0750; ALPHA_SOURCE=/x/alpha; NO_INSTALL={no_install}\n'
                             'ab_print_next_steps'))
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def test_default_flow(self, tmp_path):
        out = self._steps(tmp_path, 0)
        assert "agentctl bash nb" in out
        assert re.search(r"\n\s*\d\.\s.*\n\s+claude\b", out) or "\n       claude\n" in out
        assert "/login" in out
        assert "finalize-name nb" in out
        assert "userdel -r agent-alice" in out
        assert ".runtime/install.sh" not in out
        assert "curl -fsSL" not in out

    def test_no_install_mentions_runtime_install(self, tmp_path):
        out = self._steps(tmp_path, 1)
        assert ".runtime/install.sh" in out


# ---------------------------------------------------------------------------------------------
# B6: agentctl spawn passes the flags (reuses the existing spawn harness)
# ---------------------------------------------------------------------------------------------
from test_agentctl_forum_hint_3563 import _spawn, _read, needs_userns  # noqa: E402


@needs_userns
class TestB6AgentctlSpawnPassThrough:
    def test_tier_dev_and_multi_agent_always_passed_no_forum_url_by_default(self, tmp_path):
        proc, state, _ = _spawn(tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        called = _read(state, "bootstrap-called")
        assert called is not None
        assert " --tier dev" in called and "--multi-agent" not in called
        assert "--forum-url" not in called
        assert "--no-install" not in called

    def test_forum_url_passed_through_when_given(self, tmp_path):
        proc, state, _ = _spawn(tmp_path, "--forum-url", "http://forum.example:5002")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        called = _read(state, "bootstrap-called")
        assert "--forum-url http://forum.example:5002" in called
        assert " --tier dev" in called and "--multi-agent" not in called


# ---------------------------------------------------------------------------------------------
# B7: finalize-name step 2d vs. the shape a native install leaves
# ---------------------------------------------------------------------------------------------
def _step_2d_block() -> str:
    text = AGENTCTL.read_text()
    start = text.index('    echo "Step 2d:')
    end = text.index("    # ACL sweep on shared dirs.")
    return text[start:end]


class TestB7FinalizeNameClaudeSymlink:
    def _run(self, tmp_path, make_link):
        home = tmp_path / "chosen"
        (home / ".local" / "bin").mkdir(parents=True)
        versions = home / ".local" / "share" / "claude" / "versions"
        versions.mkdir(parents=True)
        (versions / "2.1.0").write_text("#!/bin/sh\necho 2.1.0\n")
        (versions / "2.1.0").chmod(0o755)
        make_link(home)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "sudo").write_text('#!/bin/bash\nshift 2\nexec "$@"\n')
        (bindir / "sudo").chmod(0o755)
        script = (f'set -u\nchosen_home="{home}"; chosen_user=x\n'
                  f"step2d() {{\n{_step_2d_block()}\n}}\nstep2d\n")
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}"))
        return home, proc

    def test_native_install_shape_absolute_symlink_into_own_home_is_repaired(self, tmp_path):
        # the native installer's shape: ~/.local/bin/claude -> <home>/.local/share/claude/versions/X
        # (absolute). After `usermod -d -m` the old absolute home is gone, so it dangles.
        def link(home):
            os.symlink("/home/agent-alice/.local/share/claude/versions/2.1.0",
                       home / ".local" / "bin" / "claude")
        home, proc = self._run(tmp_path, link)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        l = home / ".local" / "bin" / "claude"
        assert os.readlink(l) == "../share/claude/versions/2.1.0"
        assert l.exists()

    def test_resolving_symlink_left_alone(self, tmp_path):
        def link(home):
            os.symlink("../share/claude/versions/2.1.0", home / ".local" / "bin" / "claude")
        home, proc = self._run(tmp_path, link)
        assert "no repair needed" in proc.stdout
        assert os.readlink(home / ".local" / "bin" / "claude") == "../share/claude/versions/2.1.0"


# ---------------------------------------------------------------------------------------------
# fold 2 (PR #4123 colleague round 1 + fairy r1): one normalised source, quoted trust value,
# INT/TERM trap, fuller remedy text, pinned `--`, linked-worktree limit
# ---------------------------------------------------------------------------------------------
import signal  # noqa: E402
import time  # noqa: E402

# Calls the preflight resolver when it exists. The first cell below pins that it exists and runs
# before useradd; the guard lets the behavioural cells go red on an assertion, not a NameError.
RESOLVE = 'NO_INSTALL=0; declare -F ab_preflight_source >/dev/null && { ab_preflight_source || exit 9; }'


def resolved_body(home: Path, source_text: str, extra: str = "") -> str:
    return (f'USER_NAME="agent-alice"; USER_HOME="{home}"; ALPHA_SOURCE="{source_text}"\n'
            f'TIER=""; FORUM_URL=""\n{extra}\n{RESOLVE}\n'
            'rc=0; ab_clone_and_install || rc=$?; echo "RC=$rc"; echo "TAG=${CLONE_TAG:-}"')


def _clone_argv(tmp_path: Path) -> list[str]:
    lines = [ln for ln in (tmp_path / "git.log").read_text().splitlines() if ln.startswith("git clone ")]
    assert len(lines) == 1, lines
    return lines[0].split(" ||GCG=")[0].split()[1:]


def _trusted(tmp_path: Path) -> str:
    return subprocess.run(["git", "config", "-f", str(tmp_path / "git.log.cfg"), "--get", "safe.directory"],
                          capture_output=True, text=True, check=True).stdout.rstrip("\n")


class TestFold2NormalisedSource:
    def _check_one_string(self, tmp_path, proc, expected_src: Path):
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        argv = _clone_argv(tmp_path)
        src_arg = argv[argv.index("--") + 1]
        assert src_arg == str(expected_src)
        assert _trusted(tmp_path) == f"{src_arg}/.git"  # one string, both uses

    def test_preflight_function_runs_before_anything_that_creates_state(self):
        assert "ab_preflight_source()" in SRC
        main = SRC[SRC.index("# ---------- arg parse ----------"):]
        # Anchor on the CALL line, not the name: the comment above the call also names the function,
        # so a bare name search stayed green with the call deleted (Sol, #4123 delta read).
        pos = main.index("\nab_preflight_source ||")
        for stateful in ("\nuseradd", "groupadd agents", "mkdir -p"):
            assert pos < main.index(stateful), stateful
        assert '${ALPHA_SOURCE%/}' not in CODE  # the old strip is gone

    def test_trailing_slash_clone_arg_plus_dot_git_equals_the_trust_entry(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, resolved_body(home, f"{src}/"))
        self._check_one_string(tmp_path, proc, src)

    def test_relative_path_from_another_cwd_is_made_absolute_before_the_clone(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        elsewhere = tmp_path / "elsewhere" / "deeper"
        elsewhere.mkdir(parents=True)
        proc = run_lib(env, resolved_body(home, "../../source", extra=f'cd "{elsewhere}"'))
        self._check_one_string(tmp_path, proc, src.resolve())
        argv = _clone_argv(tmp_path)
        assert os.path.isabs(argv[argv.index("--") + 1])

    def test_symlinked_source_uses_the_resolved_string_for_both(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        link = tmp_path / "link-to-source"
        link.symlink_to(src)
        proc = run_lib(env, resolved_body(home, str(link)))
        self._check_one_string(tmp_path, proc, src.resolve())

    def test_nonexistent_source_is_refused_in_preflight_and_no_useradd_runs(self, tmp_path):
        env, home = make_env(tmp_path)
        useradd = tmp_path / "bin" / "useradd"
        useradd.write_text('#!/bin/bash\necho "useradd $*" >> "$USERADD_LOG"\n')
        useradd.chmod(0o755)
        env["USERADD_LOG"] = str(tmp_path / "useradd.log")
        missing = tmp_path / "no" / "such" / "checkout"
        proc = run_lib(env, f'ALPHA_SOURCE="{missing}"; NO_INSTALL=0\n'
                            'rc=0; declare -F ab_preflight_source >/dev/null && { ab_preflight_source || rc=$?; }; echo "PRC=$rc"\n'
                            '[ "$rc" -eq 0 ] && useradd x\nexit 0')
        assert re.search(r"^PRC=[1-9]", proc.stdout, re.M), proc.stdout + proc.stderr
        assert str(missing) in proc.stderr
        assert not (tmp_path / "useradd.log").exists()
        assert not (tmp_path / "sudo.log").exists()

    def test_source_without_a_dot_git_directory_is_refused_linked_worktree_limit(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path)
        wt = tmp_path / "linked"
        _git(src, "worktree", "add", "-q", "--detach", str(wt))
        assert (wt / ".git").is_file()
        proc = run_lib(env, f'ALPHA_SOURCE="{wt}"; NO_INSTALL=0\n'
                            'rc=0; declare -F ab_preflight_source >/dev/null && { ab_preflight_source || rc=$?; }; echo "PRC=$rc"')
        assert re.search(r"^PRC=[1-9]", proc.stdout, re.M), proc.stdout + proc.stderr
        assert "normal repo" in proc.stderr and str(wt) in proc.stderr

    def test_usage_states_the_normal_repo_limit(self):
        head = SRC[:SRC.index("set -euo pipefail")]
        assert "must be a normal repo" in head and ".git" in head

    def test_no_install_does_not_demand_a_checkout(self, tmp_path):
        env, home = make_env(tmp_path)
        proc = run_lib(env, 'ALPHA_SOURCE=""; NO_INSTALL=1\nrc=0; ab_preflight_source || rc=$?; echo "PRC=$rc"')
        assert "PRC=0" in proc.stdout, proc.stdout + proc.stderr


class TestFold2QuotedTrustValue:
    def test_hash_in_the_source_path_survives_a_git_config_read_back(self, tmp_path):
        env, home = make_env(tmp_path)
        base = tmp_path / "a#b c"
        base.mkdir()
        src = make_source(base, tags={"v0.1.0": "c2"})
        proc = run_lib(env, resolved_body(home, str(src)))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        assert _trusted(tmp_path) == f"{src}/.git"

    def test_double_quote_and_backslash_in_the_path_are_escaped(self, tmp_path):
        env, home = make_env(tmp_path)
        base = tmp_path / 'q"uo\\te'
        base.mkdir()
        src = make_source(base, tags={"v0.1.0": "c2"})
        # the path goes into a double-quoted bash assignment: escape for bash, not for git
        as_bash = str(src).replace("\\", "\\\\").replace('"', '\\"')
        proc = run_lib(env, resolved_body(home, as_bash))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        assert _trusted(tmp_path) == f"{src}/.git"


class TestFold2SignalTrap:
    @pytest.mark.parametrize("sig,code", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
    def test_signal_during_the_clone_removes_the_trust_dir(self, tmp_path, sig, code):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        env["GIT_CLONE_SLEEP"] = "2"
        script = f'set -u\nsource "{BOOTSTRAP}"\n' + install_body(home, src)
        # POSIX: a non-interactive shell cannot trap a signal ignored at its start, so reset it in the child
        proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True,
                                preexec_fn=lambda: signal.signal(sig, signal.SIG_DFL))
        started = tmp_path / "git.log.clone-started"
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert started.exists(), "fake clone never started"
        assert list((tmp_path / "tmpd").iterdir()), "trust dir should exist while the clone runs"
        os.kill(proc.pid, sig)  # the bootstrap bash only, not its process group
        out, err = proc.communicate(timeout=60)
        assert list((tmp_path / "tmpd").iterdir()) == [], out + err
        assert proc.returncode == code, (proc.returncode, out, err)
        assert not (tmp_path / "install.log").exists(), "install.sh must not run after a signal"

    def test_trust_dir_is_created_under_tmpdir_and_gone_after(self, tmp_path):
        """#4281: BSD mktemp with no template ignores TMPDIR (macOS uses its per-user temp dir), so the trust dir
        must be made from an explicit ${TMPDIR:-/tmp} template. Mutation: bare `mktemp -d` -> red on macOS."""
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        env["GIT_CLONE_SLEEP"] = "2"
        script = f'set -u\nsource "{BOOTSTRAP}"\n' + install_body(home, src)
        proc = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True)
        started = tmp_path / "git.log.clone-started"
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert started.exists(), "fake clone never started"
        during = [p.name for p in (tmp_path / "tmpd").iterdir()]
        out, err = proc.communicate(timeout=60)
        assert len(during) == 1 and during[0].startswith("agent-bootstrap."), (during, out + err)
        assert list((tmp_path / "tmpd").iterdir()) == [], out + err

    def test_trust_dir_falls_back_to_tmp_when_tmpdir_is_unset(self, tmp_path):
        """#4281 (colleague review): the ${TMPDIR:-/tmp} fallback matters when TMPDIR is absent, e.g. under sudo,
        which resets the environment. Mutation: `${TMPDIR}` with no fallback -> red here only."""
        env, home = make_env(tmp_path)
        env.pop("TMPDIR")
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src))
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        used = (tmp_path / "git.log.path").read_text().strip()
        assert used.startswith("/tmp/agent-bootstrap."), used
        assert not Path(used).exists(), f"trust file outlived the clone: {used}"
        assert not Path(used).parent.exists(), f"trust dir outlived the clone: {Path(used).parent}"

    def test_traps_are_cleared_after_the_clone(self, tmp_path):
        env, home = make_env(tmp_path)
        src = make_source(tmp_path, tags={"v0.1.0": "c2"})
        proc = run_lib(env, install_body(home, src) + '\ntrap -p; echo TRAPS-DONE')
        assert _rc(proc) == 0, proc.stdout + proc.stderr
        after = proc.stdout.split("TAG=")[-1].split("TRAPS-DONE")[0]
        assert "cfgdir" not in after, after
