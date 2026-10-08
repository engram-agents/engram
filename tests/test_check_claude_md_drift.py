"""Tests for tools/check-claude-md-drift.py — rendered CLAUDE.md drift detector CLI.

Test categories:
  - Exit 0 (no drift): two refs rendering identically produce returncode 0, empty stdout.
  - Exit 1 (drift detected): a head commit that changes a template source produces
    returncode 1 and a unified diff on stdout. This is the mutation guard — if the
    script's drift-detection logic were broken, this test would fail.
  - Exit 2 (usage / argument errors): missing required args and --repo pointing at a
    non-git directory both produce returncode 2 via argparse parser.error().

Follows the tmp_path + subprocess.run([sys.executable, str(CLI_PATH)] + args,
capture_output=True, text=True, timeout=...) idiom used in test_ia_cli.py.

The fixture builds a minimal throwaway git repo in tmp_path containing both template
files that used to feed the render:
  src/engram/templates/template.CLAUDE.md   (the sole CLAUDE_RENDER_SOURCES entry now)
  src/engram/templates/compact-instructions.md

compact-instructions.md is kept in the fixture (rather than dropped) specifically to
back test_compact_instructions_change_exits_0 below — the positive regression guard
that a compact-instructions.md-only change is now correctly invisible to this tool
(engram-alpha#2067: it no longer feeds the Claude render; see
docs/compaction-continuity.md).

The script imports template_render from its own source tree (resolved from __file__),
so the fixture only needs the template content at the git refs — the render logic
comes from the installed worktree.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).parent.parent / "tools" / "check-claude-md-drift.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEMPLATE_CONTENT = """\
# I'm {{AGENT_NAME}}, an AI agent with ENGRAM.

<!-- Source of truth: compact-instructions.md — edit there, not in this template. This comment and the marker below are both replaced at install-time. -->
{{COMPACT_INSTRUCTIONS}}
"""

_COMPACT_CONTENT = """\
When summarizing this conversation, write in first person.
"""


def _run_drift(repo: Path, base_ref: str, head_ref: str) -> tuple[int, str, str]:
    """Invoke the drift CLI as a subprocess; return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "--repo", str(repo),
            "--base", base_ref,
            "--head", head_ref,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in the repo; raise on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}):\n{result.stderr}"
        )
    return result


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one base commit (both template files present).

    Returns the repo path. The HEAD after this call is the base commit; callers can
    mutate files and commit again to produce a head commit that differs from base.
    """
    repo = tmp_path / "fake_repo"
    repo.mkdir()

    # git init
    _git(repo, "init")
    # Configure local identity so git commit doesn't fail
    _git(repo, "config", "user.email", "test@test.invalid")
    _git(repo, "config", "user.name", "Test Runner")

    # Create src/engram/templates/
    templates_dir = repo / "src" / "engram" / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "template.CLAUDE.md").write_text(_TEMPLATE_CONTENT, encoding="utf-8")
    (templates_dir / "compact-instructions.md").write_text(_COMPACT_CONTENT, encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base commit")

    return repo


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A minimal git repo with one base commit containing both template files."""
    return _make_git_repo(tmp_path)


# ---------------------------------------------------------------------------
# Tests: exit 0 — no drift
# ---------------------------------------------------------------------------

class TestNoDrift:
    """Comparing a ref to itself (or two commits with identical renders) exits 0."""

    def test_same_ref_exits_0(self, git_repo: Path):
        """--base HEAD --head HEAD: identical renders → exit 0, empty stdout."""
        rc, stdout, stderr = _run_drift(git_repo, "HEAD", "HEAD")
        assert rc == 0, f"Expected exit 0 (no drift); stderr: {stderr}"
        assert stdout == "", f"Expected empty stdout when no drift; got: {stdout!r}"

    def test_two_identical_commits_exits_0(self, git_repo: Path, tmp_path: Path):
        """Two commits whose template content renders identically → exit 0."""
        # Record base SHA
        base_result = _git(git_repo, "rev-parse", "HEAD")
        base_sha = base_result.stdout.strip()

        # Make a second commit that changes something UNRELATED to the templates
        (git_repo / "unrelated.txt").write_text("not a template file\n", encoding="utf-8")
        _git(git_repo, "add", "unrelated.txt")
        _git(git_repo, "commit", "-m", "unrelated change")

        rc, stdout, stderr = _run_drift(git_repo, base_sha, "HEAD")
        assert rc == 0, f"Expected exit 0 (no drift); stderr: {stderr}"
        assert stdout == "", f"Expected empty stdout; got: {stdout!r}"

    def test_compact_instructions_change_exits_0(self, git_repo: Path, tmp_path: Path):
        """Changing compact-instructions.md alone → exit 0, no drift.

        Retargeted from the old `TestDriftDetected.test_compact_instructions_change_exits_1`
        (engram-alpha#2067): compact-instructions.md is no longer folded into the
        Claude render — CLAUDE_RENDER_SOURCES is now just ["template.CLAUDE.md"]
        (compact-instructions.md is Codex-only, see docs/compaction-continuity.md).
        A compact-instructions.md-only change is therefore correctly invisible to
        this tool now; this is a positive regression guard for that new invariant,
        not just an "unrelated file" case like test_two_identical_commits_exits_0.
        """
        base_result = _git(git_repo, "rev-parse", "HEAD")
        base_sha = base_result.stdout.strip()

        # Mutate the compact-instructions file (no longer part of the Claude render)
        templates_dir = git_repo / "src" / "engram" / "templates"
        new_compact = _COMPACT_CONTENT + "\nAn additional compact instruction.\n"
        (templates_dir / "compact-instructions.md").write_text(new_compact, encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "add compact instruction")

        rc, stdout, stderr = _run_drift(git_repo, base_sha, "HEAD")
        assert rc == 0, (
            f"Expected exit 0 (compact-instructions.md no longer feeds the Claude "
            f"render); got rc={rc}. stdout: {stdout!r}, stderr: {stderr!r}"
        )
        assert stdout == "", f"Expected empty stdout; got: {stdout!r}"


# ---------------------------------------------------------------------------
# Tests: exit 1 — drift detected (this is the mutation guard)
# ---------------------------------------------------------------------------

class TestDriftDetected:
    """When a head commit changes a template source, the CLI exits 1 with a diff."""

    def test_template_change_exits_1(self, git_repo: Path, tmp_path: Path):
        """Changing template.CLAUDE.md in the head commit → exit 1, diff on stdout."""
        base_result = _git(git_repo, "rev-parse", "HEAD")
        base_sha = base_result.stdout.strip()

        # Mutate the template file
        templates_dir = git_repo / "src" / "engram" / "templates"
        new_content = _TEMPLATE_CONTENT + "\n## A new section added in head\n"
        (templates_dir / "template.CLAUDE.md").write_text(new_content, encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-m", "add section to template")

        rc, stdout, stderr = _run_drift(git_repo, base_sha, "HEAD")
        assert rc == 1, (
            f"Expected exit 1 (drift detected); got rc={rc}. "
            f"stdout: {stdout!r}, stderr: {stderr!r}"
        )
        # stdout must contain a unified diff
        assert "---" in stdout or "+++" in stdout, (
            f"Expected unified diff markers in stdout; got: {stdout!r}"
        )
        # The changed text must appear in the diff
        assert "A new section added in head" in stdout, (
            f"Expected changed text in diff; got: {stdout!r}"
        )


# ---------------------------------------------------------------------------
# Tests: exit 2 — usage / argument errors
# ---------------------------------------------------------------------------

class TestUsageErrors:
    """Missing required args and invalid --repo produce exit 2 via parser.error()."""

    def test_missing_head_arg_exits_2(self, git_repo: Path):
        """Omitting --head (required arg) → exit 2."""
        proc = subprocess.run(
            [sys.executable, str(CLI_PATH), "--repo", str(git_repo), "--base", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 2, (
            f"Expected exit 2 for missing --head; got rc={proc.returncode}. "
            f"stderr: {proc.stderr!r}"
        )

    def test_missing_base_arg_exits_2(self, git_repo: Path):
        """Omitting --base (required arg) → exit 2."""
        proc = subprocess.run(
            [sys.executable, str(CLI_PATH), "--repo", str(git_repo), "--head", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 2, (
            f"Expected exit 2 for missing --base; got rc={proc.returncode}. "
            f"stderr: {proc.stderr!r}"
        )

    def test_missing_repo_arg_exits_2(self):
        """Omitting --repo (required arg) → exit 2."""
        proc = subprocess.run(
            [sys.executable, str(CLI_PATH), "--base", "HEAD", "--head", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 2, (
            f"Expected exit 2 for missing --repo; got rc={proc.returncode}. "
            f"stderr: {proc.stderr!r}"
        )

    def test_repo_not_a_git_dir_exits_2(self, tmp_path: Path):
        """--repo pointing at a plain directory (no .git) → exit 2 via parser.error()."""
        plain_dir = tmp_path / "not_a_git_repo"
        plain_dir.mkdir()
        rc, stdout, stderr = _run_drift(plain_dir, "HEAD", "HEAD")
        assert rc == 2, (
            f"Expected exit 2 for non-git --repo; got rc={rc}. "
            f"stderr: {stderr!r}"
        )


# ===========================================================================
# --live mode (engram-alpha#3604): every rendered template paragraph must be
# present in the live file; extras ignored; errors are exit 2, never "0 missing".
# Contract cells written BEFORE the implementation.
# ===========================================================================

_USER = "Dr. O'Neil"
_AGENT = "Ag\\ent(1)+$ \\n"          # regex-special + backslash chars, literal on purpose
_HOME = "/home/x/.engram"

_B_H1 = "# I'm {{AGENT_NAME}}, an AI agent."
_B_INTRO = "Intro paragraph that wraps\nacross two lines for {{USER_NAME}}."
_B_H2 = "## Section A"
_B_LIST1 = "- bullet one"
_B_LIST2 = "- bullet two\n\n  continued after a blank line"
_B_FENCE = "```bash\necho one\n\necho two\n```"
_B_FINAL = "Final paragraph about {{ENGRAM_HOME}} and `{{SELF_NODE_ID}}`."
_BLOCKS = [_B_H1, _B_INTRO, _B_H2, _B_LIST1, _B_LIST2, _B_FENCE, _B_FINAL]

_LIVE_FLAGS = [
    "--user-name", _USER,
    "--agent-name", _AGENT,
    "--self-node-id", "pn_0002",
    "--engram-home", _HOME,
]


def _sub(text: str) -> str:
    """Literal substitution, mirroring what the tool is specified to do."""
    for k, v in {
        "{{USER_NAME}}": _USER, "{{AGENT_NAME}}": _AGENT,
        "{{SELF_NODE_ID}}": "pn_0002", "{{ENGRAM_HOME}}": _HOME,
    }.items():
        text = text.replace(k, v)
    return text


def _rendered(blocks=None) -> str:
    return _sub("\n\n".join(blocks or _BLOCKS) + "\n")


def _line_of(block: str, blocks=None) -> int:
    """1-based line in the rendered template where ``block`` starts."""
    rendered = _rendered(blocks)
    first = _sub(block).splitlines()[0]
    return rendered.splitlines().index(first) + 1


def _live_repo(tmp_path: Path, template: str, multi: str | None = None) -> Path:
    repo = tmp_path / "live_repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    d = repo / "src" / "engram" / "templates"
    d.mkdir(parents=True)
    (d / "template.CLAUDE.md").write_text(template, encoding="utf-8")
    if multi is not None:
        (d / "template.CLAUDE.multi-agent.md").write_text(multi, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "live fixture")
    return repo


def _run_live(repo, live, *extra, head="HEAD", flags=None):
    args = [sys.executable, str(CLI_PATH), "--repo", str(repo), "--head", head,
            "--live", str(live)]
    args += _LIVE_FLAGS if flags is None else flags
    args += list(extra)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return proc.returncode, proc.stdout, proc.stderr


def _summary(stdout: str):
    m = re.search(r"(\d+) units checked, (\d+) missing", stdout)
    assert m, f"no summary line in: {stdout!r}"
    return int(m.group(1)), int(m.group(2))


@pytest.fixture
def lv(tmp_path: Path):
    repo = _live_repo(tmp_path, "\n\n".join(_BLOCKS) + "\n")
    live = tmp_path / "live.md"
    live.write_text(_rendered(), encoding="utf-8")
    return repo, live


class TestLiveAllPresent:
    def test_all_present_exit_0(self, lv):
        repo, live = lv
        rc, out, err = _run_live(repo, live)
        assert rc == 0, (out, err)
        assert _summary(out) == (7, 0)

    def test_summary_line_always_printed_even_when_missing(self, lv):
        repo, live = lv
        live.write_text("nothing relevant\n", encoding="utf-8")
        rc, out, _ = _run_live(repo, live)
        assert rc == 1
        assert _summary(out) == (7, 7)


class TestLivePositiveControl:
    """Can it fail: delete exactly one named paragraph -> exit 1 naming that one."""

    @pytest.mark.parametrize("victim", _BLOCKS)
    def test_deleted_paragraph_is_named_and_only_it(self, lv, victim):
        repo, live = lv
        kept = [b for b in _BLOCKS if b is not victim]
        live.write_text(_rendered(kept), encoding="utf-8")
        rc, out, err = _run_live(repo, live)
        assert rc == 1, (out, err)
        assert _summary(out) == (7, 1)
        first = _sub(victim).splitlines()[0]
        assert first in out
        assert f"line {_line_of(victim)}" in out
        for other in _BLOCKS:
            if other is not victim:
                assert _sub(other).splitlines()[0] not in out, other

    def test_long_first_line_is_truncated(self, tmp_path):
        long_para = "word " * 80
        repo = _live_repo(tmp_path, "# T\n\n" + long_para.strip() + "\n")
        live = tmp_path / "l.md"
        live.write_text("# T\n", encoding="utf-8")
        rc, out, _ = _run_live(repo, live, flags=[])
        assert rc == 1
        miss = [l for l in out.splitlines() if "line 3" in l]
        assert miss and len(miss[0]) < 200


class TestLiveExtras:
    def test_extra_and_reordered_paragraphs_ignored(self, lv):
        repo, live = lv
        extras = "\n\n".join(f"Unrelated local paragraph number {i}." for i in range(50))
        live.write_text(extras + "\n\n" + _rendered(list(reversed(_BLOCKS))) + "\n\n" + extras,
                        encoding="utf-8")
        rc, out, err = _run_live(repo, live)
        assert rc == 0, (out, err)
        assert _summary(out) == (7, 0)


class TestLiveWhitespaceWordSensitivity:
    def test_rewrap_blank_lines_trailing_spaces_pass(self, lv):
        repo, live = lv
        text = _rendered()
        text = text.replace("wraps\nacross", "wraps across")           # re-wrapped
        text = text.replace("\n\n", "\n\n\n\n")                      # extra blank lines
        text = "\n".join(l + "   " for l in text.splitlines()) + "\n"  # trailing spaces
        live.write_text(text, encoding="utf-8")
        rc, out, err = _run_live(repo, live)
        assert rc == 0, (out, err)

    def test_rewrap_alone_passes(self, lv):
        repo, live = lv
        live.write_text(_rendered().replace("wraps\nacross two", "wraps across\ntwo"),
                        encoding="utf-8")
        rc, out, err = _run_live(repo, live)
        assert rc == 0, (out, err)

    @pytest.mark.parametrize("old,new", [
        ("wraps", "wrapped"),            # a changed word
        ("Intro paragraph", "intro paragraph"),   # case only
        ("two lines for", "two lines, for"),      # punctuation only
        ("across", "across the"),                 # an added word
    ])
    def test_changed_word_is_missing(self, lv, old, new):
        repo, live = lv
        live.write_text(_rendered().replace(old, new), encoding="utf-8")
        rc, out, err = _run_live(repo, live)
        assert rc == 1, (out, err)
        assert _summary(out) == (7, 1)
        assert f"line {_line_of(_B_INTRO)}" in out

    def test_heading_level_change_is_missing(self, lv):
        repo, live = lv
        live.write_text(_rendered().replace("## Section A", "### Section A"), encoding="utf-8")
        rc, out, _ = _run_live(repo, live)
        assert rc == 1
        assert f"line {_line_of(_B_H2)}" in out


class TestLiveUnresolvedPlaceholder:
    def test_flag_not_supplied_names_placeholder(self, lv):
        repo, live = lv
        rc, out, err = _run_live(repo, live, flags=["--agent-name", "A", "--self-node-id", "pn_1",
                                                      "--engram-home", _HOME])
        assert rc == 2
        assert "USER_NAME" in out + err

    def test_unknown_placeholder_never_counted_present(self, tmp_path):
        tpl = "# T\n\nParagraph with {{X_UNKNOWN}} inside.\n"
        repo = _live_repo(tmp_path, tpl)
        live = tmp_path / "l.md"
        live.write_text(tpl, encoding="utf-8")   # live carries the literal text
        rc, out, err = _run_live(repo, live)
        assert rc == 2, (out, err)
        assert "X_UNKNOWN" in out + err
        assert "0 missing" not in out


class TestLiveErrorsNeverZeroMissing:
    def _assert_err(self, rc, out, err):
        assert rc == 2, (rc, out, err)
        assert err.strip(), "an error needs a message on stderr"
        assert "0 missing" not in out

    def test_live_file_missing(self, lv, tmp_path):
        repo, _ = lv
        self._assert_err(*_run_live(repo, tmp_path / "nope.md"))

    def test_live_is_a_directory(self, lv, tmp_path):
        repo, _ = lv
        d = tmp_path / "adir"
        d.mkdir()
        self._assert_err(*_run_live(repo, d))

    @pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
    def test_live_unreadable(self, lv):
        repo, live = lv
        live.chmod(0)
        try:
            self._assert_err(*_run_live(repo, live))
        finally:
            live.chmod(0o644)

    def test_live_not_utf8(self, lv):
        repo, live = lv
        live.write_bytes(b"\xff\xfe\x00bad")
        self._assert_err(*_run_live(repo, live))

    def test_unknown_head_ref(self, lv):
        repo, live = lv
        self._assert_err(*_run_live(repo, live, head="no-such-ref"))

    def test_empty_render(self, tmp_path):
        repo = _live_repo(tmp_path, "\n\n   \n")
        live = tmp_path / "l.md"
        live.write_text("anything\n", encoding="utf-8")
        self._assert_err(*_run_live(repo, live))

    def test_template_absent_at_ref(self, lv, tmp_path):
        repo, live = lv
        _git(repo, "rm", "-q", "src/engram/templates/template.CLAUDE.md")
        _git(repo, "commit", "-q", "-m", "drop template")
        self._assert_err(*_run_live(repo, live))


class TestLiveSubstitutionIsLiteral:
    def test_special_character_values_match(self, lv):
        repo, live = lv
        assert "\\" in _AGENT and "(" in _AGENT and "$" in _AGENT
        rc, out, err = _run_live(repo, live)   # live holds the literally substituted text
        assert rc == 0, (out, err)
        assert _summary(out) == (7, 0)


class TestLiveSeedManifest:
    def _setup(self, tmp_path, manifest):
        tpl = "# T\n\nSee {{AX_HONESTY}} and {{GL_EPISTEMIC_HUMILITY}}.\n"
        repo = _live_repo(tmp_path, tpl)
        live = tmp_path / "l.md"
        live.write_text("# T\n\nSee ax_0001 and gl_0001.\n", encoding="utf-8")
        m = tmp_path / "seed-manifest.json"
        if manifest is not None:
            m.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
        return repo, live, m

    def test_manifest_fills_seed_ids(self, tmp_path):
        repo, live, m = self._setup(tmp_path, {
            "seed_ids": {"{{AX_HONESTY}}": "ax_0001", "{{GL_EPISTEMIC_HUMILITY}}": "gl_0001"},
            "alpha_dir": "/x", "target": "claude"})
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m), flags=[])
        assert rc == 0, (out, err)

    def test_wrong_id_in_live_is_missing(self, tmp_path):
        repo, live, m = self._setup(tmp_path, {
            "seed_ids": {"{{AX_HONESTY}}": "ax_0009", "{{GL_EPISTEMIC_HUMILITY}}": "gl_0001"}})
        rc, out, _ = _run_live(repo, live, "--seed-manifest", str(m), flags=[])
        assert rc == 1

    def test_no_manifest_names_unresolved(self, tmp_path):
        repo, live, _ = self._setup(tmp_path, None)
        rc, out, err = _run_live(repo, live, flags=[])
        assert rc == 2 and "AX_HONESTY" in out + err

    @pytest.mark.parametrize("bad", ["{not json", {"alpha_dir": "x"}, {"seed_ids": ["a"]},
                                      {"seed_ids": {"{{AX_HONESTY}}": 5}}])
    def test_bad_manifest_exit_2(self, tmp_path, bad):
        repo, live, m = self._setup(tmp_path, bad)
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m), flags=[])
        assert rc == 2 and err.strip()

    def test_missing_manifest_exit_2(self, tmp_path):
        repo, live, m = self._setup(tmp_path, None)
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m), flags=[])
        assert rc == 2 and err.strip()


class TestLiveArgumentErrors:
    def test_live_with_base_exit_2(self, lv):
        repo, live = lv
        proc = subprocess.run([sys.executable, str(CLI_PATH), "--repo", str(repo), "--head", "HEAD",
                               "--base", "HEAD", "--live", str(live)] + _LIVE_FLAGS,
                              capture_output=True, text=True, timeout=15)
        assert proc.returncode == 2 and proc.stderr.strip()

    def test_neither_mode_exit_2(self, lv):
        repo, _ = lv
        proc = subprocess.run([sys.executable, str(CLI_PATH), "--repo", str(repo), "--head", "HEAD"],
                              capture_output=True, text=True, timeout=15)
        assert proc.returncode == 2

    def test_live_without_head_exit_2(self, lv):
        repo, live = lv
        proc = subprocess.run([sys.executable, str(CLI_PATH), "--repo", str(repo), "--live", str(live)],
                              capture_output=True, text=True, timeout=15)
        assert proc.returncode == 2

    def test_old_usage_message_preserved(self, lv):
        repo, _ = lv
        proc = subprocess.run([sys.executable, str(CLI_PATH), "--repo", str(repo), "--base", "HEAD"],
                              capture_output=True, text=True, timeout=15)
        assert proc.returncode == 2
        assert "the following arguments are required: --head" in proc.stderr
        proc = subprocess.run([sys.executable, str(CLI_PATH)], capture_output=True, text=True, timeout=15)
        assert "the following arguments are required: --repo, --base, --head" in proc.stderr

    def test_help_documents_live_flags(self):
        proc = subprocess.run([sys.executable, str(CLI_PATH), "--help"],
                              capture_output=True, text=True, timeout=15)
        for flag in ("--live", "--user-name", "--agent-name", "--self-node-id",
                     "--engram-home", "--seed-manifest", "--multi-agent"):
            assert flag in proc.stdout, flag


class TestLiveDriftGuard:
    """The shipped template must only use placeholders the tool can fill."""

    def test_real_template_placeholders_are_fillable(self):
        # Subprocess, not in-process import: the tool inserts src/engram into
        # sys.path, which would leak into the rest of the pytest session.
        code = (
            "import importlib.util, json, sys\n"
            "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n"
            "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
            "print(json.dumps(sorted(mod.KNOWN_PLACEHOLDERS)))\n"
        )
        proc = subprocess.run([sys.executable, "-c", code, str(CLI_PATH)],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        known = set(json.loads(proc.stdout))
        real = CLI_PATH.resolve().parent.parent / "templates" / "template.CLAUDE.md"
        used = set(re.findall(r"\{\{[A-Za-z0-9_]+\}\}", real.read_text(encoding="utf-8")))
        assert used, "template unexpectedly has no placeholders"
        unknown = used - known
        assert not unknown, f"template uses placeholders the --live tool cannot fill: {sorted(unknown)}"
        for never in ("{{COMPACT_INSTRUCTIONS}}", "{{PYTHON}}", "{{ENGRAM_HOOKS_DIR}}"):
            assert never not in known


_MA = ("## Local multi-agent rules\n\nUse the ia letter for one agent.\n\n"
       "### Reciprocal PR review\n\nMy PRs need a colleague review before merge.\n")


class TestLiveMultiAgent:
    def _mk(self, tmp_path, live_text, multi=_MA):
        repo = _live_repo(tmp_path, "\n\n".join(_BLOCKS) + "\n", multi=multi)
        live = tmp_path / "l.md"
        live.write_text(_rendered() + "\n" + live_text, encoding="utf-8")
        return repo, live

    def test_default_never_reads_multi_agent_file(self, tmp_path):
        repo, live = self._mk(tmp_path, "")
        rc, out, err = _run_live(repo, live)
        assert rc == 0, (out, err)
        assert _summary(out) == (7, 0)
        assert "multi-agent" not in out

    def test_flag_adds_paragraphs_and_reports_own_line_numbers(self, tmp_path):
        repo, live = self._mk(tmp_path, "")
        rc, out, err = _run_live(repo, live, "--multi-agent")
        assert rc == 1, (out, err)
        assert "Use the ia letter for one agent." in out
        assert "template.CLAUDE.multi-agent.md" in out
        assert "line 3" in out          # relative to the multi-agent file
        assert _summary(out)[1] == 2

    def test_same_text_under_different_heading_passes(self, tmp_path):
        live_text = ("### Internal agent coordination\n\nUse the ia letter for one agent.\n\n"
                     "My PRs need a colleague review before merge.\n")
        repo, live = self._mk(tmp_path, live_text)
        rc, out, err = _run_live(repo, live, "--multi-agent")
        assert rc == 0, (out, err)

    def test_reworded_text_is_missing(self, tmp_path):
        live_text = ("### X\n\nUse the ia letter for a single agent.\n\n"
                     "My PRs need a colleague review before merge.\n")
        repo, live = self._mk(tmp_path, live_text)
        rc, out, _ = _run_live(repo, live, "--multi-agent")
        assert rc == 1
        assert _summary(out)[1] == 1

    def test_flag_without_file_at_ref_exit_2(self, tmp_path):
        repo, live = self._mk(tmp_path, "", multi=None)
        rc, out, err = _run_live(repo, live, "--multi-agent")
        assert rc == 2 and err.strip()


# ===========================================================================
# Unit rule v2 (coordinator ruling after the first real-seat run): each LIST
# ITEM and each TABLE ROW is its own unit, so a local edit inside a list or
# table is not a false "missing" and a real miss names its own line.
# ===========================================================================

def _mini(tmp_path, template, live_text, *extra):
    repo = _live_repo(tmp_path, template)
    live = tmp_path / "mini_live.md"
    live.write_text(live_text, encoding="utf-8")
    return _run_live(repo, live, *extra, flags=[])


def _missing_lines(out):
    return [l for l in out.splitlines() if l.startswith("MISSING ")]


class TestLiveListItemUnits:
    def test_sentence_appended_to_item_in_middle_passes(self, tmp_path):
        rc, out, err = _mini(tmp_path, "# T\n\n1. one\n2. two\n3. three\n",
                             "# T\n\n1. one\n2. two plus a locally appended sentence.\n3. three\n")
        assert rc == 0, (out, err)

    def test_extra_item_inserted_in_middle_passes(self, tmp_path):
        rc, out, err = _mini(tmp_path, "# T\n\n- a\n- b\n- c\n",
                             "# T\n\n- a\n- LOCAL EXTRA\n- b\n- c\n")
        assert rc == 0, (out, err)

    def test_one_bullet_removed_names_that_bullets_own_line(self, tmp_path):
        tpl = "# T\n\n- b1\n- b2\n- b3\n- b4\n- b5\n"
        rc, out, err = _mini(tmp_path, tpl, "# T\n\n- b1\n- b2\n- b4\n- b5\n")
        assert rc == 1, (out, err)
        miss = _missing_lines(out)
        assert len(miss) == 1, out
        assert "line 5: - b3" in miss[0], miss

    def test_nested_sub_item_removed_names_the_sub_item(self, tmp_path):
        tpl = "# T\n\n- parent\n  - child one\n  - child two\n- tail\n"
        rc, out, err = _mini(tmp_path, tpl, "# T\n\n- parent\n  - child two\n- tail\n")
        assert rc == 1, (out, err)
        miss = _missing_lines(out)
        assert len(miss) == 1 and "line 4: - child one" in miss[0], out

    def test_fenced_block_under_item_stays_with_its_item(self, tmp_path):
        tpl = "- item head\n\n  ```bash\n  echo hi\n\n  echo ho\n  ```\n- tail\n"
        rc, out, err = _mini(tmp_path, tpl, tpl)
        assert rc == 0 and _summary(out) == (2, 0), (out, err)     # 2 units: not 3+
        sub = tmp_path / "b"
        sub.mkdir()
        rc, out, err = _mini(sub, tpl, tpl.replace("echo ho", "echo HO"))
        assert rc == 1
        miss = _missing_lines(out)
        assert len(miss) == 1 and "line 1: - item head" in miss[0], out

    def test_duplicate_identical_items_collapse_into_one_check(self, tmp_path):
        tpl = "# T\n\n- none\n- none\n- other\n"
        rc, out, err = _mini(tmp_path, tpl, "# T\n\n- other\n")
        assert rc == 1, (out, err)
        assert _summary(out) == (3, 1), out            # heading, one 'none', other
        assert len(_missing_lines(out)) == 1


class TestLiveTableRowUnits:
    TPL = "# T\n\n| h1 | h2 |\n|----|----|\n| a | b |\n| c | d |\n"

    def test_all_present_separator_is_not_a_unit(self, tmp_path):
        rc, out, err = _mini(tmp_path, self.TPL, self.TPL)
        assert rc == 0 and _summary(out) == (4, 0), (out, err)

    def test_missing_separator_in_live_is_not_reported(self, tmp_path):
        rc, out, err = _mini(tmp_path, self.TPL, self.TPL.replace("|----|----|\n", ""))
        assert rc == 0, (out, err)

    def test_one_row_reworded_only_that_row(self, tmp_path):
        rc, out, err = _mini(tmp_path, self.TPL, self.TPL.replace("| a | b |", "| a | X |"))
        assert rc == 1
        miss = _missing_lines(out)
        assert len(miss) == 1 and "line 5: | a | b |" in miss[0], out

    def test_deleted_row_reported_header_kept(self, tmp_path):
        rc, out, err = _mini(tmp_path, self.TPL, self.TPL.replace("| c | d |\n", ""))
        assert rc == 1
        miss = _missing_lines(out)
        assert len(miss) == 1 and "line 6: | c | d |" in miss[0], out
        assert "h1" not in out

    def test_extra_local_row_passes(self, tmp_path):
        rc, out, err = _mini(tmp_path, self.TPL, self.TPL + "| local | row |\n")
        assert rc == 0, (out, err)


class TestLiveSummaryWording:
    def test_summary_says_units(self, lv):
        repo, live = lv
        rc, out, _ = _run_live(repo, live)
        assert re.search(r"^\d+ units checked, \d+ missing$", out, re.M), out


# ===========================================================================
# Reviewer-fairy round 1 on PR #3605: boundaries, git-failure mapping, empty
# flag values, documented limits.
# ===========================================================================

class TestLiveWordBoundaries:
    """Presence is word-bounded on BOTH sides (documented rule)."""

    TPL = "# T\n\nAlways write it\n"

    def test_trailing_boundary_longer_word_is_missing(self, tmp_path):
        rc, out, _ = _mini(tmp_path, self.TPL, "# T\n\nAlways write items\n")
        assert rc == 1 and "line 3: Always write it" in out, out

    def test_exact_unit_is_present(self, tmp_path):
        rc, out, err = _mini(tmp_path, self.TPL, "# T\n\nAlways write it\n")
        assert rc == 0, (out, err)

    def test_trailing_punctuation_appended_counts_missing(self, tmp_path):
        # Documented rule: "write it." does not contain the unpunctuated unit
        # bounded by whitespace, so it is reported missing.
        rc, out, _ = _mini(tmp_path, self.TPL, "# T\n\nAlways write it.\n")
        assert rc == 1 and "line 3" in out, out

    def test_leading_boundary_prefixed_word_is_missing(self, tmp_path):
        rc, out, _ = _mini(tmp_path, self.TPL, "# T\n\nxAlways write it\n")
        assert rc == 1 and "line 3" in out, out

    def test_unit_followed_by_more_words_is_present(self, tmp_path):
        rc, out, err = _mini(tmp_path, self.TPL, "# T\n\nAlways write it now, locally.\n")
        assert rc == 0, (out, err)

    def test_heading_with_words_appended_is_present(self, tmp_path):
        rc, out, err = _mini(tmp_path, "## Foo\n", "## Foo and more\n")
        assert rc == 0, (out, err)


_GIT_FAIL_SNIPPET = """
import importlib.util, sys, argparse
from pathlib import Path
spec = importlib.util.spec_from_file_location('m', sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
def boom(*a, **k):
    print('simulated git plumbing failure', file=sys.stderr)
    raise SystemExit(3)
mod._git_show = boom
ns = argparse.Namespace(head='HEAD', live=sys.argv[3], seed_manifest=None, multi_agent=%s,
                        user_name='u', agent_name='a', self_node_id='pn_1', engram_home='/h')
sys.exit(mod.run_live(ns, Path(sys.argv[2])))
"""


class TestLiveGitFailureIsExit2:
    @pytest.mark.parametrize("multi", [False, True])
    def test_git_plumbing_failure_maps_to_2(self, lv, multi):
        repo, live = lv
        _git(repo, "show", "HEAD:src/engram/templates/template.CLAUDE.md")   # repo is sane
        proc = subprocess.run(
            [sys.executable, "-c", _GIT_FAIL_SNIPPET % multi, str(CLI_PATH), str(repo), str(live)],
            capture_output=True, text=True, timeout=30)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
        assert "missing" not in proc.stdout
        assert "error" in proc.stderr.lower()


class TestLiveEmptyFlagValues:
    FLAGS = {"--user-name": 0, "--agent-name": 2, "--self-node-id": 4, "--engram-home": 6}

    @pytest.mark.parametrize("flag", list(FLAGS))
    @pytest.mark.parametrize("value", ["", "   ", "\t"])
    def test_empty_or_blank_value_refused(self, lv, flag, value):
        repo, live = lv
        flags = list(_LIVE_FLAGS)
        flags[self.FLAGS[flag] + 1] = value
        rc, out, err = _run_live(repo, live, flags=flags)
        assert rc == 2, (rc, out, err)
        assert flag in err
        assert "0 missing" not in out


class TestLiveLimitsPinned:
    """The documented Limits sentences must be true."""

    def test_blockquote_is_an_ordinary_paragraph_unit(self, tmp_path):
        tpl = "# T\n\n> quoted para here\n\nTail para.\n"
        rc, out, _ = _mini(tmp_path, tpl, "# T\n\nTail para.\n")
        assert rc == 1 and _summary(out) == (3, 1) and "line 3: > quoted para here" in out, out

    def test_html_comment_is_an_ordinary_paragraph_unit(self, tmp_path):
        tpl = "# T\n\n<!-- a comment para -->\n\nTail para.\n"
        rc, out, _ = _mini(tmp_path, tpl, "# T\n\nTail para.\n")
        assert rc == 1 and _summary(out) == (3, 1) and "line 3: <!-- a comment para -->" in out, out

    def test_hunk_commented_out_in_live_still_counts_present(self, tmp_path):
        # absence check only: text inside an HTML comment / fence / quote is found anywhere
        tpl = "# T\n\nGuarded rule paragraph.\n"
        for live in ("# T\n\n<!-- Guarded rule paragraph. -->\n",
                     "# T\n\n```\nGuarded rule paragraph.\n```\n",
                     "# T\n\n> Guarded rule paragraph.\n"):
            sub = tmp_path / str(abs(hash(live)))
            sub.mkdir()
            rc, out, err = _mini(sub, tpl, live)
            assert rc == 0, (live, out, err)

    def test_help_states_the_limit(self):
        proc = subprocess.run([sys.executable, str(CLI_PATH), "--help"],
                              capture_output=True, text=True, timeout=15)
        assert "absence" in " ".join(proc.stdout.split())


# ===========================================================================
# Contract A (home written as ~ or absolute) and B (--seed-id) -- follow-ups
# to the live mode.  Written before the implementation.
# ===========================================================================

class TestLiveHomeTildeOrAbsolute:
    """{{ENGRAM_HOME}} may be given as ~/.engram or the absolute path (an unquoted
    ~ is expanded by the shell); a live file may write either form."""

    TPL = "# T\n\nData lives in {{ENGRAM_HOME}}/x for this seat.\n\nUnrelated plain paragraph.\n"

    def _go(self, tmp_path, monkeypatch, live_text, home_arg):
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        abs_home = str(fake_home / ".engram")
        repo = _live_repo(tmp_path, self.TPL)
        live = tmp_path / "hl.md"
        live.write_text(live_text.replace("ABS", abs_home), encoding="utf-8")
        arg = abs_home if home_arg == "ABS" else home_arg
        return _run_live(repo, live, flags=["--engram-home", arg])

    def test_live_tilde_arg_absolute(self, tmp_path, monkeypatch):
        rc, out, err = self._go(tmp_path, monkeypatch,
                                "# T\n\nData lives in ~/.engram/x for this seat.\n\nUnrelated plain paragraph.\n", "ABS")
        assert rc == 0 and _summary(out) == (3, 0), (out, err)

    def test_live_absolute_arg_tilde(self, tmp_path, monkeypatch):
        rc, out, err = self._go(tmp_path, monkeypatch,
                                "# T\n\nData lives in ABS/x for this seat.\n\nUnrelated plain paragraph.\n", "~/.engram")
        assert rc == 0 and _summary(out) == (3, 0), (out, err)

    @pytest.mark.parametrize("form", ["ABS", "~/.engram"])
    def test_same_form_as_argument_still_present(self, tmp_path, monkeypatch, form):
        live = "# T\n\nData lives in %s/x for this seat.\n\nUnrelated plain paragraph.\n" % form
        rc, out, err = self._go(tmp_path, monkeypatch, live, form)
        assert rc == 0 and _summary(out) == (3, 0), (out, err)

    @pytest.mark.parametrize("arg", ["ABS", "~/.engram"])
    def test_control_different_home_stays_missing(self, tmp_path, monkeypatch, arg):
        live = "# T\n\nData lives in /other/.engram/x for this seat.\n\nUnrelated plain paragraph.\n"
        rc, out, err = self._go(tmp_path, monkeypatch, live, arg)
        assert rc == 1 and _summary(out) == (3, 1), (out, err)
        assert "line 3:" in out

    def test_unit_without_home_is_unaffected(self, tmp_path, monkeypatch):
        live = "# T\n\nData lives in ~/.engram/x for this seat.\n"
        rc, out, err = self._go(tmp_path, monkeypatch, live, "ABS")
        assert rc == 1 and _summary(out) == (3, 1), (out, err)
        assert "Unrelated plain paragraph." in out


class TestLiveSeedIdOverride:
    TPL = "# T\n\nSee {{AX_HONESTY}} and {{AX_HONESTY_DISCRETION}}.\n"

    def _setup(self, tmp_path, ids, live_text="# T\n\nSee idone and idtwo.\n"):
        repo = _live_repo(tmp_path, self.TPL)
        live = tmp_path / "sl.md"
        live.write_text(live_text, encoding="utf-8")
        m = tmp_path / "seed-manifest.json"
        m.write_text(json.dumps({"seed_ids": ids}))
        return repo, live, m

    def test_missing_id_message_names_it_and_the_flag(self, tmp_path):
        repo, live, m = self._setup(tmp_path, {"{{AX_HONESTY}}": "idone"})
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m), flags=[])
        assert rc == 2 and out == "", (rc, out, err)
        assert "AX_HONESTY_DISCRETION" in err
        assert "--seed-id KEY=VALUE" in err

    def test_override_completes_the_run(self, tmp_path):
        repo, live, m = self._setup(tmp_path, {"{{AX_HONESTY}}": "idone"})
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m),
                                 "--seed-id", "AX_HONESTY_DISCRETION=idtwo", flags=[])
        assert rc == 0 and _summary(out) == (2, 0), (out, err)

    def test_override_by_content_exits_1(self, tmp_path):
        repo, live, m = self._setup(tmp_path, {"{{AX_HONESTY}}": "idone"})
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m),
                                 "--seed-id", "AX_HONESTY_DISCRETION=idother", flags=[])
        assert rc == 1 and _summary(out) == (2, 1), (out, err)

    def test_override_beats_manifest_value(self, tmp_path):
        repo, live, m = self._setup(
            tmp_path, {"{{AX_HONESTY}}": "idwrong", "{{AX_HONESTY_DISCRETION}}": "idtwo"})
        rc, out, _ = _run_live(repo, live, "--seed-manifest", str(m), flags=[])
        assert rc == 1                                    # manifest value is wrong for this live file
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m),
                                 "--seed-id", "AX_HONESTY=idone", flags=[])
        assert rc == 0, (out, err)

    def test_override_without_a_manifest(self, tmp_path):
        repo, live, _ = self._setup(tmp_path, {})
        rc, out, err = _run_live(repo, live, "--seed-id", "AX_HONESTY=idone",
                                 "--seed-id", "AX_HONESTY_DISCRETION=idtwo", flags=[])
        assert rc == 0, (out, err)

    def test_braced_key_form_accepted(self, tmp_path):
        repo, live, _ = self._setup(tmp_path, {})
        rc, out, err = _run_live(repo, live, "--seed-id", "{{AX_HONESTY}}=idone",
                                 "--seed-id", "AX_HONESTY_DISCRETION=idtwo", flags=[])
        assert rc == 0, (out, err)

    @pytest.mark.parametrize("bad", ["NOEQUALS", "=idone", "AX_HONESTY=", "AX_HONESTY=  ", "  =x"])
    def test_malformed_seed_id_exit_2(self, tmp_path, bad):
        repo, live, m = self._setup(tmp_path, {"{{AX_HONESTY}}": "idone",
                                               "{{AX_HONESTY_DISCRETION}}": "idtwo"})
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m), "--seed-id", bad, flags=[])
        assert rc == 2, (rc, out, err)
        assert "--seed-id" in err
        assert "0 missing" not in out

    def test_unused_key_is_harmless(self, tmp_path):
        repo, live, m = self._setup(tmp_path, {"{{AX_HONESTY}}": "idone",
                                               "{{AX_HONESTY_DISCRETION}}": "idtwo"})
        rc, out, err = _run_live(repo, live, "--seed-manifest", str(m),
                                 "--seed-id", "NOT_USED_ANYWHERE=whatever", flags=[])
        assert rc == 0, (out, err)


class TestLiveHelpWording:
    def test_help_mentions_end_append(self):
        proc = subprocess.run([sys.executable, str(CLI_PATH), "--help"],
                              capture_output=True, text=True, timeout=15)
        text = " ".join(proc.stdout.split())
        assert "appended at the END" in text
        assert "--seed-id" in text


# ===========================================================================
# Round 2: different-home controls in both directions, trailing slash,
# `=` in a seed-id value, repeated key.
# ===========================================================================

class TestLiveDifferentHomeBothDirections:
    _go = TestLiveHomeTildeOrAbsolute._go
    TPL = TestLiveHomeTildeOrAbsolute.TPL

    @pytest.mark.parametrize("arg,live_home", [
        ("/other/.engram", "~/.engram"),            # different absolute arg vs ~ live file
        ("/other/.engram", "ABS"),                  # different absolute arg vs own absolute live
        ("~/.engram", "/home/zzz/.engram"),         # ~ arg vs live file under a different absolute home
        ("~/.elsewhere", "~/.engram"),              # different ~ arg vs ~ live file
        ("~/.elsewhere", "ABS"),
    ])
    def test_different_home_is_missing(self, tmp_path, monkeypatch, arg, live_home):
        live = "# T\n\nData lives in %s/x for this seat.\n\nUnrelated plain paragraph.\n" % live_home
        rc, out, err = self._go(tmp_path, monkeypatch, live, arg)
        assert rc == 1 and _summary(out) == (3, 1), (arg, live_home, out, err)
        assert "line 3:" in out


class TestLiveHomeTrailingSlash:
    TPL = TestLiveHomeTildeOrAbsolute.TPL

    def _go(self, tmp_path, monkeypatch, live_home, arg):
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        abs_home = str(fake_home / ".engram")
        repo = _live_repo(tmp_path, self.TPL)
        live = tmp_path / "ts.md"
        live.write_text("# T\n\nData lives in %s/x for this seat.\n\nUnrelated plain paragraph.\n"
                        % live_home.replace("ABS", abs_home), encoding="utf-8")
        return _run_live(repo, live, flags=["--engram-home", arg.replace("ABS", abs_home)])

    @pytest.mark.parametrize("arg", ["ABS/", "~/.engram/", "ABS//", "~/.engram//"])
    @pytest.mark.parametrize("live_home", ["ABS", "~/.engram"])
    def test_trailing_slash_gives_zero_missing(self, tmp_path, monkeypatch, arg, live_home):
        rc, out, err = self._go(tmp_path, monkeypatch, live_home, arg)
        assert rc == 0 and _summary(out) == (3, 0), (arg, live_home, out, err)

    @pytest.mark.parametrize("arg", ["/other/.engram/", "~/.elsewhere/"])
    @pytest.mark.parametrize("live_home", ["ABS", "~/.engram"])
    def test_control_different_home_with_slash_is_missing(self, tmp_path, monkeypatch, arg, live_home):
        rc, out, err = self._go(tmp_path, monkeypatch, live_home, arg)
        assert rc == 1 and _summary(out) == (3, 1), (arg, live_home, out, err)

    def test_bare_slash_home_stays_nonempty(self, tmp_path, monkeypatch):
        # "/" must not collapse to an empty home (that would turn /x into plain x)
        rc, out, err = self._go(tmp_path, monkeypatch, "ABS", "/")
        assert rc == 1 and _summary(out) == (3, 1), (out, err)


class TestLiveSeedIdValueAndRepeat:
    TPL = "# T\n\nSee {{AX_HONESTY}} here.\n"

    def _run(self, tmp_path, live_value, *seed_args):
        repo = _live_repo(tmp_path, self.TPL)
        live = tmp_path / "eq.md"
        live.write_text("# T\n\nSee %s here.\n" % live_value, encoding="utf-8")
        extra = []
        for a in seed_args:
            extra += ["--seed-id", a]
        return _run_live(repo, live, *extra, flags=[])

    def test_value_is_everything_after_the_first_equals(self, tmp_path):
        rc, out, err = self._run(tmp_path, "x=y=z", "AX_HONESTY=x=y=z")
        assert rc == 0, (out, err)

    def test_value_after_first_equals_is_not_the_tail_only(self, tmp_path):
        # a live file carrying only the last segment must NOT satisfy KEY=x=y=z
        rc, out, err = self._run(tmp_path, "z", "AX_HONESTY=x=y=z")
        assert rc == 1, (out, err)

    def test_repeated_key_last_wins(self, tmp_path):
        rc, out, err = self._run(tmp_path, "second", "AX_HONESTY=first", "AX_HONESTY=second")
        assert rc == 0, (out, err)

    def test_repeated_key_first_does_not_win(self, tmp_path):
        rc, out, err = self._run(tmp_path, "first", "AX_HONESTY=first", "AX_HONESTY=second")
        assert rc == 1, (out, err)
