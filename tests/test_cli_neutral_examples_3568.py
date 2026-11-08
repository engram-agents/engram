"""baton / ia help and error text a general user reads names none of our team
(engram-alpha#3568, cosmetic slice).

Contract (written BEFORE the implementation): `baton --help`, every
`baton <sub> --help`, `ia --help` and every `ia <sub> --help` contain none of
`borges`, `ariadne`, `engram-agents` or our shared directory; so do the error
messages that carry examples (the --participants, --github/anchor and --repo
validations). Examples are neutral: `alice,bob`, `owner/repo`,
`pr/owner/repo/22`. The `baton --help` epilog no longer hardcodes a projects dir.
The project board's name placeholder is `e.g. your-name`.

HERMETIC: the two real-subprocess cells run under ``_hermetic_env`` (HOME and
XDG_CONFIG_HOME pinned to a temp dir, ENGRAM_HOME pointed at an empty temp dir).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_TOOLS = _REPO / "src" / "engram" / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.append(str(_TOOLS))  # append, never insert(0): see test_baton.py (#2037/#2063)

BANNED = ("borges", "ariadne", "engram-agents", "agents-shared")

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored);
# module scope: the hook is loaded in module-scope fixtures, which a function-scope window does not cover.
pytestmark = pytest.mark.usefixtures("isolate_hook_loads_module")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test_3568", _TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def baton():
    return _load("baton")


@pytest.fixture(scope="module")
def ia():
    return _load("ia")


def _hermetic_env(tmp: Path) -> dict:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    eh = tmp / "engram_home"
    eh.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["ENGRAM_HOME"] = str(eh)
    return env


def _all_help(parser: argparse.ArgumentParser) -> dict[str, str]:
    """name -> format_help() for the parser and every (nested) subparser."""
    out = {parser.prog: parser.format_help()}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                out.update(_all_help(sub))
    return out


def _leaks(text: str) -> list[str]:
    low = text.lower()
    return [b for b in BANNED if b in low]


class TestHelpNamesNoTeam:

    def test_baton_help_surfaces_are_enumerated(self, baton):
        helps = _all_help(baton.build_parser())
        assert len(helps) >= 10, sorted(helps)  # parent + the subcommands

    def test_every_baton_help_is_free_of_our_names(self, baton):
        bad = {k: _leaks(v) for k, v in _all_help(baton.build_parser()).items() if _leaks(v)}
        assert not bad, bad

    def test_every_ia_help_is_free_of_our_names(self, ia):
        helps = _all_help(ia.build_parser())
        assert len(helps) >= 3, sorted(helps)
        bad = {k: _leaks(v) for k, v in helps.items() if _leaks(v)}
        assert not bad, bad

    def test_baton_help_uses_the_neutral_examples(self, baton):
        text = "\n".join(_all_help(baton.build_parser()).values())
        for ex in ("alice,bob", "owner/repo", "pr/owner/repo/22"):
            assert ex in text, ex

    def test_ia_help_uses_a_neutral_example(self, ia):
        text = "\n".join(_all_help(ia.build_parser()).values())
        assert "alice" in text

    def test_baton_epilog_does_not_hardcode_a_projects_dir(self, baton):
        epilog = baton.build_parser().epilog or ""
        assert "Projects dir" not in epilog and "/projects/" not in epilog, epilog

    @pytest.mark.parametrize("tool", ["baton", "ia"])
    def test_real_help_subprocess_is_free_of_our_names(self, tmp_path, tool):
        r = subprocess.run([sys.executable, str(_TOOLS / f"{tool}.py"), "--help"],
                           capture_output=True, text=True, env=_hermetic_env(tmp_path), timeout=30)
        assert r.returncode == 0, r.stdout + r.stderr
        assert not _leaks(r.stdout + r.stderr), r.stdout


_MULTI = {"mode": "multi", "agent_name": "alice", "forum": {"url": "http://test-forum:5002"},
          "primary_user": "casey"}


class TestErrorMessagesNameNoTeam:

    def _init(self, baton, monkeypatch, capsys, **over):
        monkeypatch.setattr(baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(baton, "_POOL_SENTINEL_CACHE", [])
        base = dict(project_id="PR-999", title="t", participants="alice,bob", status="planning",
                    turn=None, github=None, colleague=None, repo=None)
        base.update(over)
        with pytest.raises(SystemExit):
            baton.cmd_init(argparse.Namespace(**base), _MULTI, "alice", MagicMock())
        return capsys.readouterr().err

    def test_participants_error_carries_an_example_and_no_team_name(self, baton, monkeypatch, capsys):
        err = self._init(baton, monkeypatch, capsys, participants=",")
        assert "--participants" in err and "e.g." in err, err
        assert not _leaks(err), err

    def test_init_github_anchor_error_carries_an_example_and_no_team_name(self, baton, monkeypatch, capsys):
        err = self._init(baton, monkeypatch, capsys, github="not-an-anchor")
        assert "e.g." in err, err
        assert not _leaks(err), err

    def test_init_repo_error_carries_an_example_and_no_team_name(self, baton, monkeypatch, capsys):
        err = self._init(baton, monkeypatch, capsys, repo="not-a-repo")
        assert "owner/repo" in err and "e.g." in err, err
        assert not _leaks(err), err

    def test_anchor_error_carries_an_example_and_no_team_name(self, baton, capsys):
        client = MagicMock()
        client.get.return_value = {"raw": "---\nproject: PR-1\n---\n"}
        with pytest.raises(SystemExit):
            baton.cmd_anchor(argparse.Namespace(project_id="PR-1", github="not-an-anchor"),
                             _MULTI, "alice", client)
        err = capsys.readouterr().err
        assert "e.g." in err, err
        assert not _leaks(err), err


class TestBoardPlaceholder:

    def test_project_board_name_placeholder_is_neutral(self):
        html = (_REPO / "src" / "forum" / "templates" / "project_board.html").read_text()
        assert "e.g. lei" not in html
        # #1459: the board viewer is read-only; its pool-sentinel/actor inputs (and so the
        # placeholder this cell used to pin) are gone. No input at all means no leaking one.
        assert "sentinel-input" not in html and "<input" not in html
