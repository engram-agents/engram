"""Contract cells for #4149 (part): pre-dispatch-check is documented as a
citation check, to be run with the epic number too. Documentation only."""
import os
import re
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src/engram/tools/pre-dispatch-check.sh"
SKILL = REPO / "src/engram/skills/claude/engram-fairy-orchestration/SKILL.md"


def _header():
    lines = []
    for line in SCRIPT.read_text().splitlines():
        if not line.startswith("#"):
            break
        lines.append(line.lstrip("#"))
    return " ".join(" ".join(lines).split())


def _bullet():
    text = SKILL.read_text()
    start = text.index("- **Pre-spec coordination check**")
    nxt = re.search(r"\n- \*\*", text[start + 5:])
    return text[start:start + 5 + nxt.start()] if nxt else text[start:]


def test_header_has_epic_rule():
    h = _header().lower()
    assert "run it with the epic number" in h and "[part of #" in h


def test_header_states_bodies_not_searched():
    h = _header().lower()
    assert "bodies are not searched" in h
    assert "bodies are searched" not in h


def test_header_states_titles_only():
    assert "open pr title" in _header().lower()


def test_header_states_open_prs_only_limit():
    h = _header().lower()
    assert "only prs open at the moment it runs" in h
    assert "started in parallel" in h and "not seen" in h


def test_header_exit_code_line_says_cites():
    h = _header()
    assert "no open PR or in-flight baton cites the issue" in h
    assert "references the issue" not in h


def test_header_states_what_it_does_not_answer():
    assert "already doing" in _header().lower()


def test_skill_bullet_has_epic_rule():
    b = _bullet().lower()
    assert "epic" in b and "[part of #" in b


def test_skill_bullet_clear_is_about_citations():
    assert "citations" in _bullet().lower()


def test_usage_error_exit_2():
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True)
    assert r.returncode == 2


def test_symlink_mode_unchanged():
    out = subprocess.check_output(
        ["git", "ls-files", "-s", "tools/pre-dispatch-check.sh"],
        cwd=str(REPO), text=True)
    assert out.startswith("120000")


def _run_with_stub_gh(tmp_path, arg, only_for=None):
    """Run the script with a stub `gh` first on PATH that reports one open PR
    titled '... [part of #4050]' and an empty baton dir.  If `only_for` is
    set the stub returns the PR only when the search string names that
    number (a plausible-wrong stub, used by the mutation)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    batons = tmp_path / "batons"
    batons.mkdir()
    pr = ('[{"number":4147,"title":"feat(x): something [part of #4050]",'
          '"author":{"login":"someone"}}]')
    cond = ('case "$*" in *"#%s"*) echo \'%s\';; *) echo \'[]\';; esac'
            % (only_for, pr)) if only_for else "echo '%s'" % pr
    gh = bindir / "gh"
    gh.write_text("#!/usr/bin/env bash\n%s\n" % cond)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
               BATON_PROJECTS_DIR=str(batons))
    return subprocess.run(["bash", str(SCRIPT), arg], capture_output=True,
                          text=True, env=env)


def test_epic_number_finds_part_of_pr_child_number_is_clear(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    r = _run_with_stub_gh(tmp_path / "a", "4050")
    assert r.returncode == 1
    assert "PR #4147" in r.stdout and "[part of #4050]" in r.stdout
    r2 = _run_with_stub_gh(tmp_path / "b", "4148")
    assert r2.returncode == 0
    assert "CLEAR" in r2.stdout
