"""install.sh round 3 (#4097, slice 5): --sandbox-untagged (S1), the step-1 HEAD guard (L1), the
venv location (L2) and the macOS python remedy (L4).

Reuses the slice-4 Sandbox (every dependency faked). Its repo is a deployable-tag checkout by
default (commit tagged v0.0.1, refs/remotes/origin/dev at it). The fake
install-local-marketplace.sh logs its argv as a ``market-args`` line.

Contract (written before the code):
  S1  --sandbox-untagged forwards BOTH --allow-untagged and --allow-branch to step 5 and prints
      ``SANDBOX BUILD (not a release): <git rev-parse HEAD>`` in step 1 and in the final block;
      without it (including --yes and every other flag, and any env var) neither is forwarded
      and no banner prints.
  L1  step 1 refuses a HEAD that install-local-marketplace.sh's step-5 guard would refuse, using
      the same rule (src/build/engine/deploy_tag.py): FAIL at step 1 naming the HEAD and the
      remedy, before venv/pip. Skipped with --sandbox-untagged.
  L2  the venv is $HOME/.engram/venv even when ENGRAM_HOME is another absolute path.
  L4  on Darwin the python-version FAIL remedy names ``brew install python@3.12``; Linux does not.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from test_install_sh_4097 import REPO_ROOT, Sandbox, _write  # noqa: E402


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path)


def _market_args(sb):
    lines = sb.calls_starting("market-args")
    assert len(lines) == 1, lines
    return lines[0].split()[1:]


# ── S1 ──────────────────────────────────────────────────────────────────────────────────────
def test_sandbox_untagged_forwards_both_allow_flags_and_prints_the_banner_twice(sb):
    sha = sb.commit_on_branch("feature")  # untagged feature branch: only the flag lets it through
    r = sb.run("--yes", "--sandbox-untagged")
    assert r.returncode == 0, r.all
    assert _market_args(sb) == ["--allow-untagged", "--allow-branch"]
    banner = "SANDBOX BUILD (not a release): %s" % sha
    assert r.stdout.count(banner) == 2, r.all
    # once in step 1 (before its ok line), once in the final block (after step 10)
    assert r.stdout.index(banner) < r.stdout.index("[step 1] preflight ... ok")
    assert r.stdout.rindex(banner) > r.stdout.index("[step 10]")


def test_without_the_flag_neither_allow_flag_is_forwarded_and_no_banner(sb):
    r = sb.run("--yes")
    assert r.returncode == 0, r.all
    assert _market_args(sb) == []
    assert "SANDBOX BUILD" not in r.all


def test_yes_and_other_flags_alone_never_imply_sandbox(sb):
    r = sb.run("--yes", "--install-claude-code", "--multi-agent", "--tier", "dev")
    assert r.returncode == 0, r.all
    assert _market_args(sb) == []
    assert "SANDBOX BUILD" not in r.all


def test_env_var_cannot_turn_sandbox_on(sb):
    r = sb.run("--yes", env={"SANDBOX_UNTAGGED": "1", "ENGRAM_SANDBOX_UNTAGGED": "1"})
    assert _market_args(sb) == [] and "SANDBOX BUILD" not in r.all


def test_help_documents_sandbox_untagged(sb):
    r = sb.run("--help")
    assert "--sandbox-untagged" in r.stdout and "not a release" in r.stdout


# ── L1 ──────────────────────────────────────────────────────────────────────────────────────
def test_untagged_feature_branch_head_fails_at_step_1_before_pip(sb):
    sha = sb.commit_on_branch("feature")
    r = sb.run("--yes")
    assert r.returncode != 0, r.all
    assert "FAIL: [step 1]" in r.stderr, r.all
    assert sha[:7] in r.stderr or sha in r.stderr, "the FAIL line must name the HEAD"
    assert "deployable tag" in r.stderr and "--sandbox-untagged" in r.stderr
    assert sb.calls_starting("pip") == [] and sb.calls_starting("marketplace") == []
    assert "[step 2]" not in r.all


def test_same_head_with_sandbox_untagged_proceeds(sb):
    sb.commit_on_branch("feature")
    r = sb.run("--yes", "--sandbox-untagged")
    assert r.returncode == 0, r.all
    assert sb.calls_starting("pip") and sb.calls_starting("marketplace")


def test_deployable_tag_head_proceeds(sb):
    r = sb.run("--yes")  # the default fixture: v0.0.1 merged into origin/dev
    assert r.returncode == 0, r.all
    assert "FAIL" not in r.all


def test_tag_on_an_unmerged_commit_is_refused(sb):
    sb.commit_on_branch("feature", tag="v9.9.9")  # tag exists, commit not in origin/dev
    r = sb.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all


def test_untagged_dev_head_is_refused_like_the_step_5_tag_guard(sb):
    # branch 'dev' passes the #794 branch guard but the #2586 tag guard still refuses it
    sb.git("commit", "-q", "--allow-empty", "-m", "dev-untagged")
    r = sb.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all


def test_step_1_decision_agrees_with_the_step_5_rule_on_every_fixture(tmp_path):
    """install.sh step 1 and install-local-marketplace.sh's guard share deploy_tag.deployable_tag_at;
    pin that on four fixtures by comparing install.sh's verdict to that function's on the same repo."""
    sys.path.insert(0, str(REPO_ROOT))
    from src.build.engine.deploy_tag import deployable_tag_at

    def build(n, how):
        root = tmp_path / str(n)
        root.mkdir()
        s = Sandbox(root)
        if how == "feature":
            s.commit_on_branch("feature")
        elif how == "unmerged-tag":
            s.commit_on_branch("feature", tag="v9.9.9")
        elif how == "dev-untagged":
            s.git("commit", "-q", "--allow-empty", "-m", "x")
        return s

    for n, how in enumerate(["tagged", "feature", "unmerged-tag", "dev-untagged"]):
        s = build(n, how)
        expect_ok = deployable_tag_at(str(s.repo)) is not None
        r = s.run("--yes")
        assert (r.returncode == 0) == expect_ok, (how, r.all)
        assert ("FAIL: [step 1]" in r.stderr) == (not expect_ok), (how, r.all)


# ── L2 ──────────────────────────────────────────────────────────────────────────────────────
def test_venv_is_created_under_home_even_when_engram_home_differs(tmp_path):
    sb2 = Sandbox(tmp_path, with_venv=False)
    other = tmp_path / "elsewhere" / "engram-data"
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
    r = sb2.run("--yes", env={"ENGRAM_HOME": str(other)})
    assert r.returncode == 0, r.all
    assert sb2.calls_starting("venv-created") == ["venv-created %s" % (sb2.home / ".engram" / "venv")]
    assert not (other / "venv").exists()


# ── L4 ──────────────────────────────────────────────────────────────────────────────────────
def _old_python(sb2):
    # a python3 that fails the >= 3.10.7 check and reports an old version
    _write(sb2.safebin / "python3", """#!/bin/sh
case "$*" in
  *"version_info[:3] >= (3, 10, 7)"*) exit 1 ;;
  "--version") echo "Python 3.9.2"; exit 0 ;;
esac
exec "%s" "$@"
""" % sys.executable)


def _fake_uname(sb2, system):
    real = sb2.safebin / "uname"
    target = real.resolve()
    real.unlink()
    _write(sb2.fakebin / "uname", """#!/bin/sh
if [ "$1" = "-s" ]; then echo "%s"; exit 0; fi
exec "%s" "$@"
""" % (system, target))


def test_darwin_python_remedy_names_brew_python_3_12(tmp_path):
    sb2 = Sandbox(tmp_path)
    _old_python(sb2)
    _fake_uname(sb2, "Darwin")
    r = sb2.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all
    assert "older than" in r.stderr
    assert "brew install python@3.12" in r.stderr


def test_darwin_python_remedy_makes_an_unversioned_python3_resolve_to_the_new_interpreter(tmp_path):
    """#4630: `brew install python@3.12` alone is a dead end. A versioned Homebrew formula
    links `python3.12` but no unversioned `python3` into the Homebrew bin directory (measured
    on one Mac); the unversioned names live in `$(brew --prefix python@3.12)/libexec/bin`. The
    preflight runs `python3`, so the printed remedy must also put that directory first on PATH.
    The remedy is run here, as printed, against a fake `brew`; the real behaviour needs a Mac."""
    import re
    import subprocess
    sb2 = Sandbox(tmp_path)
    _old_python(sb2)
    _fake_uname(sb2, "Darwin")
    r = sb2.run("--yes")
    assert r.returncode != 0 and "FAIL: [step 1]" in r.stderr, r.all
    assert "brew install python@3.12" in r.stderr
    m = re.search(r'export PATH="[^"\n]*"', r.stderr)
    assert m, "the Darwin remedy must print an `export PATH=\"...\"` command:\n" + r.stderr
    prefix = tmp_path / "cellar" / "python@3.12"
    (prefix / "libexec" / "bin").mkdir(parents=True)
    new_py = prefix / "libexec" / "bin" / "python3"
    new_py.write_text("#!/bin/sh\necho new\n")
    new_py.chmod(0o755)
    fakebrew = tmp_path / "fakebrew"
    fakebrew.mkdir()
    brew = fakebrew / "brew"
    brew.write_text('#!/bin/sh\n[ "$1" = "--prefix" ] && [ "$2" = "python@3.12" ] && echo "%s" && exit 0\nexit 1\n' % prefix)
    brew.chmod(0o755)
    old = tmp_path / "oldbin"
    old.mkdir()
    old_py = old / "python3"
    old_py.write_text("#!/bin/sh\necho old\n")
    old_py.chmod(0o755)
    out = subprocess.run(
        ["/bin/bash", "-c", m.group(0) + '; command -v python3'],
        env={"PATH": "%s:%s:/usr/bin:/bin" % (fakebrew, old), "HOME": str(tmp_path)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    assert out.stdout.strip() == str(new_py), (out.stdout, out.stderr, m.group(0))


def test_linux_python_remedy_does_not_mention_brew(tmp_path):
    sb2 = Sandbox(tmp_path)
    _old_python(sb2)
    _fake_uname(sb2, "Linux")
    r = sb2.run("--yes")
    assert r.returncode != 0 and "older than" in r.stderr
    assert "brew" not in r.stderr
    assert "install Python 3.10.7 or newer and re-run" in r.stderr
