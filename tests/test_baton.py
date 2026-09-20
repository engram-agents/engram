"""Tests for tools/baton.py — multi-agent turn-state CLI.

Test categories:
  - Smoke:          each subcommand happy path exits 0
  - Validation:     bad inputs rejected with expected exit codes / errors
  - Frontmatter:    parser handles edge cases
  - SingleAgent:    config-based mode gate → silent exit (reads) or error (writes)
  - Integration:    init → flip → mine → show → close round-trip
  - CI-state dedup: _dedupe_checks_by_latest + _pr_ci_state (#1366)
  - PoolSentinel:   sentinel resolved from config primary_user

All commands are tested via direct module calls + a mock ForumClient — no live
forum server needed and no local baton filesystem.  Subprocess is used only for
pure argparse-validation tests (those that fail before any API call).

PR1 migration note: baton.py is now 100% pure-API — no local BATON_PROJECTS_DIR
reads.  Tests that previously wrote local *.md files now use
`client.get.return_value = {"raw": _make_raw_baton(...)}` for state setup and
`client.post.call_args` for post-write assertions.  Local-FS feature tests
(malformed-file skip, tmp-file hygiene) are deleted as those code paths are gone.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

CLI_PATH = Path(__file__).parent.parent / "tools" / "baton.py"

# ---------------------------------------------------------------------------
# Module-level baton import for direct-call tests.
# Adding tools/ to sys.path covers both baton.py and forum_api.py.
# ---------------------------------------------------------------------------
# append (not insert(0)) -- neither baton.py nor forum_api.py collides with
# anything in src/, but making tools/ a sys.path *root* at position 0 would
# outrank src/forum for the rest of the pytest run, shadowing the real
# `forum` package for every test collected afterward (#2037/#2063).
_TOOLS_DIR = str(Path(__file__).parent.parent / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.append(_TOOLS_DIR)

import baton as _baton  # noqa: E402
from forum_api import ForumHttpError, ForumNetworkError  # noqa: E402

# Exit codes (mirror baton.py constants for readability in assertions)
EXIT_OK = 0
EXIT_VALIDATION = 1
EXIT_IO = 2
EXIT_STATE = 3

# Config used for all multi-agent direct-call tests
MULTI_CONFIG = {"mode": "multi", "agent_name": "borges", "primary_user": "casey"}

# Repo root, for tests that assert on shipped documentation surfaces.
REPO_ROOT = Path(__file__).parent.parent

# Phrasings that assert, UNCONDITIONALLY, that a person holds the baton --
# the #3039 defect. Shared by the refusal-message test and the
# documentation-surface test so a regression on any surface trips the same
# list (a phrase scrubbed from one surface must stay scrubbed on all of
# them). "steal" is included because that is the exact word #3039 was filed
# against. A blacklist can always be dodged by a novel phrasing; it is the
# backstop, not the guard.
CLAIM_REFUSAL_BANNED_PHRASES = (
    "steal",
    "is currently holding",
    "cannot take it from",
    "is holding this baton",
    "has this baton",
    "is the current holder",
    "it is theirs",
)

# The MIRROR-IMAGE defect, added in round 5. The list above guards one
# direction only -- asserting a person HOLDS the baton -- and rounds 2-4 all
# passed it while shipping the opposite error: telling the reader a baton
# "may in fact be free" whenever the scoped turn-log line was not a
# `claimed` one. That is wrong in the common case. `release`
# (projects.py:290, `{holder} → {sentinel}: {reason}`) and `flip` (:161,
# `{from} → {to}: {reason}`) emit TEXTUALLY IDENTICAL lines, so a baton a
# colleague genuinely holds via a flip -- the normal PR-layer case --
# matched the old second branch and was reported as possibly free. A held
# baton reported free is the same inversion #3039 was filed against, and
# reporting it as free is the direction that causes a reader to take a
# baton out from under a colleague.
#
# "handing it back" is banned as well, on a second ground: it asks the
# reader to judge whether the line "handed the baton BACK", which requires
# already knowing whether <B> is a pool marker -- the very question they
# consulted this message to answer. A circular instruction is not a weaker
# instruction, it is a non-instruction.
#
# Correct phrasings state the DISJUNCTION and stop ("either a colleague
# taking it on a flip or a return to a pool marker ... this seat cannot
# tell"). Naming the free case as one of two possibilities is fine; landing
# on it as the verdict is not.
CLAIM_REFUSAL_BANNED_FREE_PHRASES = (
    "may in fact be free",
    "is in fact free",
    "the baton is free",
    "so the baton may",
    "handing it back",
    "handing the baton back",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _authored_help_strings(parser_obj: argparse.ArgumentParser) -> dict:
    """Every string THIS REPO AUTHORS that argparse renders into
    `parser_obj`'s `--help`, collected by INTROSPECTION rather than by a
    hand-written field list.

    Keyed so that a field nobody has thought of yet -- a `usage=`, a second
    positional, a new subcommand -- changes the returned dict instead of
    slipping past a checklist. See the round-11 block in
    test_claim_disambiguation_surfaces_do_not_regress for why that property
    is the whole point.

    argparse's own `-h/--help` text is EXCLUDED: its wording belongs to
    CPython, not to this repo, so pinning it would make the suite fail on an
    interpreter upgrade. Exactly one such action must exist -- asserted, so
    the exclusion cannot silently swallow an authored second one.
    """
    fields = {
        "prog": parser_obj.prog,
        "usage": parser_obj.usage,
        "description": parser_obj.description,
        "epilog": parser_obj.epilog,
    }
    help_actions = [
        a for a in parser_obj._actions
        if isinstance(a, argparse._HelpAction)
    ]
    assert len(help_actions) == 1, (
        f"{parser_obj.prog!r} has {len(help_actions)} argparse-supplied "
        "help actions, expected exactly 1 -- the exclusion here would be "
        "dropping authored text from the enumeration"
    )
    for a in parser_obj._actions:
        if isinstance(a, argparse._HelpAction):
            continue
        # A sub-parser's `help=` one-liner is stored on the PARENT's
        # _SubParsersAction, not on the sub-parser, so reading the
        # sub-parser alone can never see it. That is precisely how round
        # 10 shipped an inversion through `baton --help`.
        if isinstance(a, argparse._SubParsersAction):
            fields["subparsers:metavar"] = a.metavar
            fields["subparsers:help"] = a.help
            for ca in a._choices_actions:
                fields[f"subcommand:{ca.dest}:metavar"] = ca.metavar
                fields[f"subcommand:{ca.dest}:help"] = ca.help
            continue
        key = "|".join(a.option_strings) or a.dest
        fields[f"arg:{key}:metavar"] = a.metavar
        fields[f"arg:{key}:help"] = a.help
    return fields


def _run_baton(engram_home: Path, *args: str, env_overrides: dict | None = None) -> tuple:
    """Run the baton CLI as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH)] + list(args),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _make_client(post_return=None, get_return=None) -> MagicMock:
    """Return a mock ForumClient with configurable post/get return values."""
    client = MagicMock()
    client.post.return_value = post_return or {"seq": 42}
    if get_return is not None:
        client.get.return_value = get_return
    return client


def _write_config(
    engram_home: Path,
    agent_name: str = "borges",
    primary_user: str = "casey",
) -> None:
    """Write a minimal config.json into engram_home with mode=multi."""
    cfg = {"mode": "multi", "agent_name": agent_name, "primary_user": primary_user}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _make_raw_baton(project_id: str, **frontmatter) -> str:
    """Return a minimal baton file as a raw markdown string (no file I/O).

    The returned string matches what GET /api/projects/<pid>["raw"] returns.
    Override any field via keyword args (e.g. status="merged", turn="ariadne").
    """
    fm = {
        "project": project_id,
        "title": f"Test {project_id}",
        "status": "in-progress",
        "turn": "borges",
        "turn_since": "2026-06-26T12:00:00Z",
        "turn_reason": "start",
        "participants": "[borges, ariadne]",
    }
    fm.update(frontmatter)
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", "## Turn log", "", "- 2026-06-26T12:00:00Z initialized", ""]
    return "\n".join(lines)


def _raw_from_init_post(post_body: dict, ts: str = "2026-09-20T12:00:00Z") -> str:
    """Render the baton the forum store WOULD persist for a given
    ``POST /api/projects`` body — the missing link that lets a test run
    ``cmd_init`` and then ``cmd_flip`` against what that init actually
    created, instead of hand-writing the intermediate baton (#3042).

    Unlike ``_make_raw_baton`` (which builds a baton from keyword
    overrides), the input here is literally ``client.post.call_args``'s
    body, so every value under test travels the real path: the CLI decides
    it, this renders it the way the server would, and the next CLI call
    reads it back.

    Mirrors, deliberately and narrowly, the two server-side rules that
    decide whether an optional field EXISTS in the frontmatter at all:
    ``server.py``'s ``author = (data.get("author") or "").strip() or None``
    and ``coordination/projects.py``'s ``if github: / if colleague: /
    if author:``. A field posted empty is therefore ABSENT here — which is
    what makes ``_derive_legacy_author``'s proxy the live path for an
    unknown invoker, and is asserted directly in
    ``test_init_with_empty_invoker_leaves_author_empty``.

    The turn-log line is the real one ``init()`` writes
    (``- {ts} initialized → {turn}: {turn_reason}``) because that line is
    the sole input to ``_derive_legacy_author``.
    

    DRIFT NOTE: the two rules mirrored here are pinned against the REAL
    server in CI by src/forum/tests/test_projects_api.py --
    `test_init_persists_author_field` (an explicit author is persisted
    verbatim) and `test_init_without_author_omits_field` (an absent author
    writes no line at all, not an empty one). If either rule changes, those
    tests fail first and loudly; this mirror is a convenience, not the
    authority. Verified present at those names 2026-09-20.
    """
    turn = (post_body.get("turn") or "").strip().lower()
    reason = post_body.get("turn_reason") or ""
    project_id = post_body["project_id"]
    title = post_body.get("title") or project_id
    participants = post_body.get("participants") or []
    if isinstance(participants, str):
        participants = [p.strip().lower() for p in participants.split(",") if p.strip()]
    fm = {
        "project": project_id,
        "title": title,
        "status": post_body.get("status") or "planning",
        "turn": turn,
        "turn_since": ts,
        "turn_reason": f'"{reason}"',
        "turn_by": '"{}"'.format((post_body.get("agent") or "").strip().lower()),
        "participants": "[" + ", ".join(participants) + "]",
    }
    for key in ("github", "colleague", "author"):
        val = (post_body.get(key) or "").strip().lower()
        if val:
            fm[key] = val
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += [
        "---",
        "",
        f"# {project_id} — {title}",
        "",
        "## Turn log",
        "",
        f"- {ts} initialized → {turn}: {reason}",
        "",
    ]
    return "\n".join(lines)


def _ts(offset_minutes: int = -5) -> str:
    """Return ISO timestamp offset from now by given minutes."""
    dt = datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


# ===========================================================================
# CATEGORY 1: Smoke tests — each subcommand happy path
# ===========================================================================

class TestSmoke:
    """Each subcommand exits 0 on happy-path input."""

    def test_help_exits_0(self, engram_home):
        rc, stdout, stderr = _run_baton(engram_home, "--help")
        assert rc == 0
        assert "init" in stdout
        assert "flip" in stdout
        assert "status" in stdout

    def test_init_exits_0(self, monkeypatch):
        """init happy path: API is called and no exception is raised."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-999",
            title="smoke test",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
            repo="engram-agents/engram-paper",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects"
        assert body["project_id"] == "PR-999"

    def test_status_empty_exits_0(self, capsys):
        """status with empty project list prints 'no active' and returns normally."""
        client = _make_client(get_return={"projects": []})
        args = argparse.Namespace()
        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "no active" in captured.out.lower() or captured.out.strip() == ""

    def test_status_with_project_exits_0(self, capsys):
        """status with one project lists it."""
        client = _make_client(get_return={"projects": [{
            "project_id": "PR-100",
            "title": "Test project",
            "status": "in-review",
            "turn": "borges",
            "turn_since": _ts(-5),
            "turn_reason": "test",
            "participants": ["borges", "ariadne"],
            "seq": 1,
        }]})
        args = argparse.Namespace()
        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-100" in captured.out

    def test_mine_exits_0(self, capsys):
        """mine with a project in turn lists it."""
        client = _make_client(get_return={"projects": [{
            "project_id": "PR-200",
            "title": "mine project",
            "status": "in-review",
            "turn": "borges",
            "turn_since": _ts(-5),
            "turn_reason": "test",
            "participants": ["borges", "ariadne"],
            "seq": 1,
        }]})
        args = argparse.Namespace()
        _baton.cmd_mine(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-200" in captured.out

    def test_show_exits_0(self, capsys):
        """show displays the raw markdown of a project."""
        raw = _make_raw_baton("PR-300", title="show test")
        client = _make_client(get_return={"raw": raw})
        args = argparse.Namespace(project_id="PR-300")
        _baton.cmd_show(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-300" in captured.out

    def test_flip_exits_0(self, monkeypatch):
        """flip happy path: API is called and no exception is raised."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-400")},
        )
        args = argparse.Namespace(
            project_id="PR-400",
            to="ariadne",
            reason="done step 1",
            force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()

    def test_close_exits_0(self, monkeypatch):
        """close happy path: API is called and no exception is raised."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-500", status="in-review")},
        )
        args = argparse.Namespace(project_id="PR-500", status="merged")
        _baton.cmd_close(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()


# ===========================================================================
# CATEGORY 2: Validation tests — bad inputs rejected
# ===========================================================================

class TestValidation:
    """Bad inputs are rejected with expected exit codes and error messages."""

    def test_init_duplicate_fails(self, capsys, monkeypatch):
        """init on existing project → EXIT_VALIDATION, already-exists error.

        #1924: this project_id is repo-qualified via --repo specifically so
        the mint-site refuse-on-unqualified-anchor path never fires here --
        without it, EXIT_VALIDATION and the project_id substring in stderr
        both coincidentally match the refuse path too, and client.post is
        never reached, silently testing nothing about duplicate-conflict
        handling (the 409 side_effect below).
        """
        # _pool_sentinel() reads the ambient $ENGRAM_HOME/config.json file, not the
        # passed config dict — mock it (as the sibling tests do) so the turn
        # validation resolves in a clean CI env instead of erroring on a missing
        # primary_user.
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client()
        client.post.side_effect = ForumHttpError(409, '{"error": "already exists"}')
        args = argparse.Namespace(
            project_id="PR-101",
            title="duplicate",
            participants="borges,ariadne",
            status="planning",
            turn=None,
            github=None,
            colleague=None,
            repo="engram-agents/engram-paper",
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_called_once()
        captured = capsys.readouterr()
        assert "already exists" in captured.err or "PR-101" in captured.err

    def test_init_invalid_project_id_fails(self, engram_home):
        """init with invalid ID (no hyphen) → exit 1, format error."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "init", "NOHYPHEN",
            "--title", "bad id",
            "--participants", "borges",
        )
        assert rc == 1
        assert "TYPE-ID" in stderr or "invalid" in stderr.lower()

    def test_init_invalid_status_fails(self, engram_home):
        """init with unknown status → argparse rejects (exit non-zero)."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "init", "PR-102",
            "--title", "bad status",
            "--participants", "borges,ariadne",
            "--status", "unknown-status",
        )
        assert rc != 0

    def test_flip_nonexistent_project_fails(self, capsys):
        """flip on non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(
            project_id="PR-NONE", to="ariadne", reason="done", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_flip_to_non_participant_fails(self, monkeypatch, capsys):
        """flip to agent not in participants → EXIT_VALIDATION, validation error."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-103", participants="[borges, ariadne]")},
        )
        args = argparse.Namespace(
            project_id="PR-103", to="mneme", reason="done", force=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not a participant" in captured.err or "participant" in captured.err

    def test_flip_to_casey_allowed(self, monkeypatch):
        """flip to 'casey' (pool sentinel) is always valid regardless of participants."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-104", participants="[borges, ariadne]")},
        )
        args = argparse.Namespace(
            project_id="PR-104", to="casey", reason="need human decision", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["to_agent"] == "casey"

    def test_flip_closed_project_fails(self, monkeypatch, capsys):
        """flip on merged project → EXIT_VALIDATION, closed error."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            get_return={"raw": _make_raw_baton("PR-105", status="merged")},
        )
        args = argparse.Namespace(
            project_id="PR-105", to="ariadne", reason="done", force=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "merged" in captured.err or "closed" in captured.err

    def test_flip_cancelled_project_fails(self, monkeypatch, capsys):
        """flip on cancelled project → EXIT_VALIDATION, closed error."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            get_return={"raw": _make_raw_baton("PR-106", status="cancelled")},
        )
        args = argparse.Namespace(
            project_id="PR-106", to="ariadne", reason="done", force=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "cancelled" in captured.err or "closed" in captured.err

    def test_show_nonexistent_fails(self, capsys):
        """show on non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_show(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_close_invalid_status_fails(self, engram_home):
        """close with status=in-review (not a close status) → argparse rejects."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "close", "PR-107", "--status", "in-review",
        )
        assert rc != 0

    def test_close_already_closed_fails(self, capsys):
        """close on already-merged project → EXIT_VALIDATION, already-closed error."""
        client = _make_client(
            get_return={"raw": _make_raw_baton("PR-108", status="merged")},
        )
        args = argparse.Namespace(project_id="PR-108", status="cancelled")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_close(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "already" in captured.err

    def test_init_turn_outside_participants_fails(self, engram_home):
        """init with --turn not in participants (and not sentinel) → exit 1."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "init", "PR-109",
            "--title", "test",
            "--participants", "borges,ariadne",
            "--turn", "mneme",
        )
        assert rc == 1
        assert "participants" in stderr or "turn" in stderr


# ===========================================================================
# CATEGORY 3: Frontmatter parsing robustness
# ===========================================================================

class TestFrontmatterParsing:
    """Parser handles edge cases gracefully."""

    def test_show_displays_full_content(self, capsys):
        """show outputs the complete file content including body."""
        raw = _make_raw_baton(
            "PR-201", title="Full content test", turn_reason="colleague-review request",
        )
        client = _make_client(get_return={"raw": raw})
        args = argparse.Namespace(project_id="PR-201")
        _baton.cmd_show(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-201" in captured.out
        assert "Full content test" in captured.out
        assert "Turn log" in captured.out

    def test_participants_brackets_parsed(self, monkeypatch):
        """Participants in [alice, bob] format are parsed correctly for flip validation."""
        raw = (
            "---\n"
            "project: PR-202\n"
            "title: Bracket test\n"
            "status: in-review\n"
            "turn: borges\n"
            "turn_since: 2026-05-28T10:00:00Z\n"
            'turn_reason: "test"\n'
            "participants: [borges, ariadne]\n"
            "---\n\n"
            "# PR-202\n\n"
            "## Turn log\n\n"
            "- 2026-05-28T10:00:00Z initialized → borges\n"
        )
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(post_return={"seq": 1}, get_return={"raw": raw})
        args = argparse.Namespace(
            project_id="PR-202", to="ariadne", reason="test flip", force=False,
        )
        # Should not raise — proves ariadne was found in bracketed participants
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["to_agent"] == "ariadne"

    def test_turn_reason_quotes_stripped_in_status(self, capsys):
        """turn_reason with surrounding quotes is displayed without double-quoting."""
        client = _make_client(get_return={"projects": [{
            "project_id": "PR-204",
            "title": "Test project",
            "status": "in-review",
            "turn": "borges",
            "turn_since": _ts(-5),
            "turn_reason": '"round-3 fix pushed"',  # raw YAML echoed with quotes
            "participants": ["borges", "ariadne"],
            "seq": 1,
        }]})
        args = argparse.Namespace()
        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "round-3 fix pushed" in captured.out
        assert '""round-3 fix pushed""' not in captured.out


# ===========================================================================
# CATEGORY 5: No-forum-configured guard (config-based)
#
# Changed from mode check to forum-configured check (#2857 step 1) —
# baton/letter/forum are symmetric for any agent once a forum is configured,
# so the gate that actually matters is "is a forum configured", not
# "mode == multi". A mode=single config WITH forum.url/agent_name set now
# succeeds identically to a multi-mode config.
# Read commands → silent exit 0 with no forum configured.
# Write commands → EXIT_STATE with "no forum configured" message.
# ===========================================================================

class TestSingleAgentGuard:
    """Forum-configured gate: no forum → silent exit (reads) or error (writes)."""

    def test_status_no_forum_configured_exits_0_silently(self, capsys):
        """cmd_status with no forum configured exits 0 silently, no API calls."""
        client = _make_client()
        args = argparse.Namespace()
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_status(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out.strip() == ""
        client.get.assert_not_called()

    def test_status_single_mode_with_forum_url_succeeds(self, capsys):
        """cmd_status with mode=single but forum.url set proceeds past the gate."""
        client = _make_client(get_return={"projects": []})
        args = argparse.Namespace()
        _baton.cmd_status(
            args,
            {"mode": "single", "forum": {"url": "http://testforum:5002"}},
            "borges",
            client,
        )
        client.get.assert_called()

    def test_mine_no_forum_configured_exits_0_silently(self, capsys):
        """cmd_mine with no forum configured exits 0 silently."""
        client = _make_client()
        args = argparse.Namespace()
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_mine(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_OK

    def test_show_no_forum_configured_exits_0_silently(self, capsys):
        """cmd_show with no forum configured exits 0 silently, no API call."""
        client = _make_client()
        args = argparse.Namespace(project_id="PR-999")
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_show(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_OK
        client.get.assert_not_called()

    def test_init_no_forum_configured_exits_state(self, capsys):
        """cmd_init with no forum configured exits EXIT_STATE with message."""
        client = _make_client()
        args = argparse.Namespace(
            project_id="PR-999", title="test", participants="borges,ariadne",
            status="planning", turn=None, github=None, colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "no forum configured" in captured.err

    def test_init_single_mode_with_agent_name_succeeds(self, capsys, monkeypatch):
        """cmd_init with mode=single but agent_name set proceeds past the gate."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client(post_return={"seq": 1})
        args = argparse.Namespace(
            project_id="PR-999", title="test", participants="borges,ariadne",
            status="planning", turn=None, github=None, colleague=None,
            repo="engram-agents/engram-paper",
        )
        _baton.cmd_init(
            args, {"mode": "single", "agent_name": "borges"}, "borges", client,
        )
        client.post.assert_called()

    def test_flip_no_forum_configured_exits_state(self, capsys):
        """cmd_flip with no forum configured exits EXIT_STATE with message."""
        client = _make_client()
        args = argparse.Namespace(
            project_id="PR-1", to="ariadne", reason="done", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "no forum configured" in captured.err

    def test_flip_missing_project_exits_1(self, capsys):
        """flip referencing non-existent project ID → EXIT_VALIDATION."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(
            project_id="PR-NONEXISTENT", to="ariadne", reason="done", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION


# ===========================================================================
# CATEGORY 6: Integration round-trip
# ===========================================================================

class TestIntegration:
    """init → flip → mine → show → close round-trip."""

    @pytest.mark.skip(
        reason="requires live forum server; multi-step state chain can't be "
               "reproduced with mocks alone (mine/status/show read server-written state). "
               "Individual write operations are covered by the other Integration tests."
    )
    def test_full_round_trip(self):
        """Complete lifecycle: init, flip twice, mine filtering, close."""

    def test_flip_updates_turn_field(self, monkeypatch):
        """After flip, the API receives to_agent=ariadne (server updates turn field)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-501")},
        )
        args = argparse.Namespace(
            project_id="PR-501", to="ariadne", reason="done step 1", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["to_agent"] == "ariadne"

    def test_flip_updates_turn_reason(self, monkeypatch):
        """After flip, the API receives the provided reason."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-502")},
        )
        args = argparse.Namespace(
            project_id="PR-502", to="ariadne", reason="round-3 fix pushed", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert "round-3 fix pushed" in body["reason"]

    def test_flip_appends_turn_log_entry(self, monkeypatch):
        """After flip, the API is called with to_agent and reason."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-503")},
        )
        args = argparse.Namespace(
            project_id="PR-503", to="ariadne", reason="step complete", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-503/flip"
        assert body["to_agent"] == "ariadne"
        assert "step complete" in body["reason"]

    def test_close_updates_status_field(self, monkeypatch):
        """After close, the API receives new_status=merged."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-504", status="in-review")},
        )
        args = argparse.Namespace(project_id="PR-504", status="merged")
        _baton.cmd_close(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "merged"

    def test_close_appends_turn_log_entry(self, monkeypatch):
        """baton close calls the API with status=merged."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-505", status="in-review")},
        )
        args = argparse.Namespace(project_id="PR-505", status="merged")
        _baton.cmd_close(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-505/status"
        assert body["new_status"] == "merged"
        assert "agent" in body

    def test_mine_filters_by_turn(self, capsys):
        """mine only shows projects where turn == invoking agent."""
        client = _make_client(get_return={"projects": [
            {
                "project_id": "PR-601", "title": "mine project", "status": "in-review",
                "turn": "borges", "turn_since": _ts(-5), "turn_reason": "test",
                "participants": ["borges", "ariadne"], "seq": 1,
            },
            {
                "project_id": "PR-602", "title": "not mine", "status": "in-review",
                "turn": "ariadne", "turn_since": _ts(-5), "turn_reason": "test",
                "participants": ["borges", "ariadne"], "seq": 2,
            },
        ]})
        args = argparse.Namespace()
        _baton.cmd_mine(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "PR-601" in captured.out
        assert "PR-602" not in captured.out

    def test_status_mine_flag_same_as_mine_subcommand(self, capsys):
        """baton status --mine and baton mine produce identical filtering."""
        # NOTE: cmd_status mutates p["turn_since"] in-place (str → datetime).
        # Use a factory to get fresh list instances so the second call sees
        # clean string values rather than already-parsed datetimes.
        def _projects():
            return [
                {
                    "project_id": "PR-603", "title": "mine", "status": "in-review",
                    "turn": "borges", "turn_since": _ts(-5), "turn_reason": "test",
                    "participants": ["borges", "ariadne"], "seq": 1,
                },
                {
                    "project_id": "PR-604", "title": "not mine", "status": "in-review",
                    "turn": "ariadne", "turn_since": _ts(-5), "turn_reason": "test",
                    "participants": ["borges", "ariadne"], "seq": 2,
                },
            ]

        client1 = _make_client(get_return={"projects": _projects()})
        args1 = argparse.Namespace(mine=True)
        _baton.cmd_status(args1, MULTI_CONFIG, "borges", client1)
        captured1 = capsys.readouterr()

        client2 = _make_client(get_return={"projects": _projects()})
        args2 = argparse.Namespace()
        _baton.cmd_mine(args2, MULTI_CONFIG, "borges", client2)
        captured2 = capsys.readouterr()

        assert "PR-603" in captured1.out and "PR-603" in captured2.out
        assert "PR-604" not in captured1.out and "PR-604" not in captured2.out

    def test_multiple_flips_accumulate_log_entries(self, monkeypatch):
        """Multiple flip API calls are each made with their distinct reasons."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        client = _make_client(post_return={"seq": 1})
        client.get.return_value = {"raw": _make_raw_baton("PR-605")}

        # First flip borges → ariadne
        args1 = argparse.Namespace(
            project_id="PR-605", to="ariadne", reason="first flip", force=False,
        )
        _baton.cmd_flip(args1, MULTI_CONFIG, "borges", client)

        # Simulate server write (ariadne now holds baton)
        client.get.return_value = {"raw": _make_raw_baton("PR-605", turn="ariadne")}

        # Second flip ariadne → borges
        args2 = argparse.Namespace(
            project_id="PR-605", to="borges", reason="second flip", force=False,
        )
        _baton.cmd_flip(args2, MULTI_CONFIG, "ariadne", client)

        assert client.post.call_count == 2
        first_body = client.post.call_args_list[0][0][1]
        second_body = client.post.call_args_list[1][0][1]
        assert first_body["reason"] == "first flip"
        assert second_body["reason"] == "second flip"

    def test_status_sorted_by_turn_since(self, capsys):
        """status output sorts by turn_since ascending (oldest first)."""
        client = _make_client(get_return={"projects": [
            {
                "project_id": "PR-702", "title": "newer", "status": "in-review",
                "turn": "borges", "turn_since": _ts(-5), "turn_reason": "test",
                "participants": ["borges", "ariadne"], "seq": 2,
            },
            {
                "project_id": "PR-701", "title": "older", "status": "in-review",
                "turn": "borges", "turn_since": _ts(-60), "turn_reason": "test",
                "participants": ["borges", "ariadne"], "seq": 1,
            },
        ]})
        args = argparse.Namespace()
        _baton.cmd_status(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        idx_701 = captured.out.find("PR-701")
        idx_702 = captured.out.find("PR-702")
        assert idx_701 < idx_702, "Older project should appear first"

    def test_init_default_turn_is_invoking_agent(self, monkeypatch):
        """init without --turn posts body with turn=borges (the invoking agent)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-801", title="default turn test",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
            repo="engram-agents/engram-paper",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["turn"] == "borges"

    def test_init_explicit_turn_override(self, monkeypatch):
        """init with --turn ariadne posts body with turn=ariadne."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-802", title="explicit turn",
            participants="borges,ariadne", status="planning",
            turn="ariadne", github=None, colleague=None,
            repo="engram-agents/engram-paper",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["turn"] == "ariadne"

    def test_init_casey_as_participant_allowed(self, monkeypatch):
        """casey can be a participant and initial turn holder — API is called."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-803", title="with casey",
            participants="borges,ariadne,casey", status="planning",
            turn="casey", github=None, colleague=None,
            repo="engram-agents/engram-paper",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()

    def test_descriptive_project_id_accepted(self, monkeypatch):
        """DESIGN-style IDs are accepted and posted to the API."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="DESIGN-trust-tier-v2", title="trust tier design",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["project_id"] == "DESIGN-trust-tier-v2"


# ===========================================================================
# CATEGORY 7: rename subcommand
# ===========================================================================

class TestRename:
    """baton rename: round-trip, validation, and audit-line tests."""

    def test_rename_round_trip(self, monkeypatch):
        """init → rename: the rename API is called with the new title."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})

        # Step 1: init (API call, no file written)
        args_init = argparse.Namespace(
            project_id="PR-901", title="original title",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
            repo="engram-agents/engram-paper",
        )
        _baton.cmd_init(args_init, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()

        # Simulate server write: update client.get for subsequent rename read
        client.get.return_value = {"raw": _make_raw_baton(
            "PR-901", title="original title", turn="borges", status="planning",
        )}

        # Step 2: rename
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        args_rename = argparse.Namespace(project_id="PR-901", title="new shiny title")
        _baton.cmd_rename(args_rename, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-901/rename"
        assert body["new_title"] == "new shiny title"

    def test_rename_audit_line_appended(self):
        """After rename, the API is called with the new title."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-902", title="before rename")},
        )
        args = argparse.Namespace(project_id="PR-902", title="after rename")
        _baton.cmd_rename(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-902/rename"
        assert body["new_title"] == "after rename"

    def test_rename_refuse_missing_project(self, capsys):
        """Rename of non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", title="foo")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_rename(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_rename_refuse_empty_title(self, engram_home):
        """Rename with empty title exits non-zero (validation fires before API)."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "rename", "PR-903", "--title", "",
        )
        assert rc != 0
        assert "empty" in stderr.lower() or "non-empty" in stderr.lower()

    def test_rename_refuse_whitespace_only_title(self, engram_home):
        """Rename with whitespace-only title exits non-zero."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "rename", "PR-904", "--title", "   ",
        )
        assert rc != 0

    def test_rename_refuse_title_with_newline(self, engram_home):
        """Rename with newline in title exits non-zero (YAML safety)."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "rename", "PR-905", "--title", "line1\nline2",
        )
        assert rc != 0
        assert "newline" in stderr.lower()

    def test_rename_refuse_title_too_long(self, engram_home):
        """Rename with title > 200 chars exits non-zero."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "rename", "PR-906", "--title", "x" * 201,
        )
        assert rc != 0
        assert "long" in stderr.lower() or "200" in stderr

    def test_rename_exact_200_char_title_accepted(self):
        """Title of exactly 200 chars is accepted and reaches the API."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-908")},
        )
        args = argparse.Namespace(project_id="PR-908", title="a" * 200)
        _baton.cmd_rename(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert len(body["new_title"]) == 200


# ===========================================================================
# CATEGORY 8: claim subcommand
# ===========================================================================

class TestClaim:
    """baton claim: happy path, state-guard refusals, and audit-line tests."""

    def test_claim_happy_path(self, monkeypatch):
        """Claim posts to the claim route with agent=borges and pool_sentinel=casey."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n == "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1001", title="pool project", turn="casey",
                participants="[borges, ariadne]", turn_reason="released",
            )},
        )
        args = argparse.Namespace(project_id="PR-1001")
        _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1001/claim"
        assert body["agent"] == "borges"
        assert body["pool_sentinel"] == "casey"

    def test_claim_refuse_already_claimed_by_other(self, monkeypatch, capsys):
        """Claim when project held by another agent → EXIT_STATE with clear message."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n == "casey")

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1002", turn="ariadne", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1002")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "ariadne" in captured.err

    def test_claim_refuse_pool_marker_message_names_seat_mismatch(self, monkeypatch, capsys):
        """#3039: when turn is a pool/maintainer marker this seat's own
        config doesn't recognize (e.g. written by a seat with a different
        primary_user), the refusal must NOT assert a person holds the
        baton ("steal") -- it must name the turn value, name THIS seat's
        own resolved sentinel so the mismatch is visible, assert neither
        "held" nor "free", hand over a remedy that works OFF the turn log,
        and point at #3039 so a reader lands on the explanation instead of
        re-deriving it. Round 7 removed the inlined turn-log reading rule
        (engram-baton/SKILL.md carries it now) and with it the conditional
        `baton flip` offer, which was the colleague case's remedy and is
        wrong for a marker this seat cannot classify. Message-text only --
        behavior (still refuses, still EXIT_STATE) is unchanged and is
        covered by test_claim_refuse_already_claimed_by_other above."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n == "casey")

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1002b", turn="lei shi", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1002b")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()

        # --- STREAM DISCIPLINE (checked FIRST, because every layer below
        # --- this one reads only stderr) --------------------------------
        # Rounds 1-3 asserted exclusively on captured.err and never once on
        # captured.out, so the entire layered stack below was silent about
        # stdout. Measured: adding
        #     print(f"hint: {current_turn} is working this one right now "
        #           "-- it is theirs until they hand it over.")
        # before the sys.exit left the pinned stderr byte-identical and the
        # suite green at 184 passed / 1 skipped -- while shipping exactly
        # the unconditional-holder claim this test exists to forbid. Note
        # "it is theirs" was ALREADY on the blacklist: the blind spot was
        # the STREAM, not the vocabulary. baton.py uses bare print() for
        # status lines elsewhere, so this is an ordinary edit, not a
        # contrived one. The refusal writes to stderr and nothing else, so
        # stdout must be empty; and the blacklist below runs over BOTH
        # streams, because a guard scoped to one stream is silent on the
        # other and the message's damage does not care which stream
        # carried it.
        assert captured.out == "", (
            "baton claim wrote to stdout; the refusal belongs on stderr "
            f"alone, and stdout is unguarded by the pins below: {captured.out!r}"
        )
        both_streams = captured.out + captured.err

        # --- POSITION-ANCHORED check (the primary one) ------------------
        # The defect #3039 fixes always lives in the message's OPENING
        # clause: that is the sentence a reader acts on, so an
        # unconditional "<name> is holding this" there does the damage no
        # matter what the later bullets say. Token-PRESENCE-anywhere
        # assertions cannot see that -- two rounds of this test were
        # defeated by mutations that rewrote the opening clause while
        # retaining every asserted token in the bullets below it. So
        # assert on the FIRST LINE specifically.
        first_line = captured.err.split("\n")[0]
        # It must be the seat-mismatch statement...
        assert "does not recognize" in first_line
        assert "lei shi" in first_line
        assert "casey" in first_line
        # ...and NOTHING ELSE. Containment alone is not enough: a mutation
        # that keeps the whole genuine sentence and merely APPENDS "lei shi
        # has this baton right now, so it is theirs until they hand it
        # over." to the same line satisfies every containment above and
        # every token below (measured: it passed 184/1-skipped). Freezing
        # the opening line is what makes that mutation fail. The
        # brittleness is deliberate and is the point: the opening clause is
        # the sentence a reader acts on, so ANY edit to it -- benign or
        # not -- must come back here and be re-justified against #3039.
        assert first_line == (
            "baton claim: this baton's turn is 'lei shi', which this seat "
            "does not recognize as its pool marker (this seat resolves "
            "'casey'). This seat cannot tell from the turn log whether "
            "'lei shi' is a colleague who holds this baton or a pool "
            "marker belonging to an install whose primary_user this seat "
            "cannot resolve -- that is engram-alpha#3039. Settle it off "
            "the log: ask 'lei shi', or check the PR or project this "
            "baton anchors. To read the log yourself, run"
        ), f"opening clause changed; re-justify against #3039: {first_line!r}"

        # --- STRUCTURAL: the turn value appears exactly where it should --
        # #3039's defect IS treating the `turn` value as a human holder, so
        # an added assertion that "<turn> holds it" has to name the turn
        # value one more time than the message legitimately does. Round 7's
        # message names it THREE times, all inside the frozen opening line
        # above: the turn statement, the held-or-pool disjunction, and the
        # off-log remedy ("ask '<turn>'"). Rounds 4-6 named it once and
        # this read `== 1`; the number is RE-DERIVED from the shipped text,
        # not loosened -- an exact count over BOTH streams still fails on
        # any extra mention, including one planted on stdout, where the
        # golden master below cannot see it.
        assert both_streams.count("lei shi") == 3, (
            "turn value mentioned an unexpected number of times -- an "
            "extra mention is likely an assertion that it names a person "
            "who holds the baton"
        )

        # --- BLACKLIST (defence in depth, not the primary check) --------
        # Run over BOTH streams (see the stream-discipline note above), and
        # over the whole message rather than one line: the conditional
        # bullets are legitimate, these phrasings are not. The list is
        # shared with test_claim_disambiguation_surfaces_do_not_regress so a
        # phrase scrubbed here stays scrubbed in --help and the docs too.
        for banned in CLAIM_REFUSAL_BANNED_PHRASES:
            assert banned not in both_streams.lower(), (
                f"refusal message asserts a holder unconditionally: {banned!r}"
            )
        # ...and the MIRROR-IMAGE blacklist (round 5). Verified to
        # discriminate: run against round 4's message (commit 1d9da1bc) this
        # loop fails on "handing it back" and "may in fact be free" -- the
        # two phrasings that reported a flip-held baton as possibly free.
        for banned in CLAIM_REFUSAL_BANNED_FREE_PHRASES:
            assert banned not in both_streams.lower(), (
                f"refusal message asserts the baton is free when this seat "
                f"cannot determine that: {banned!r}"
            )

        # --- LENGTH CEILING (round 7) -----------------------------------
        # Checked BEFORE the golden master so that a message which merely
        # GREW is reported as a length regression rather than as an opaque
        # multi-hundred-character diff. Trajectory: 955 chars (round 3,
        # flagged too long) -> 709 (round 4, trimmed) -> 1049 (round 5,
        # regrown when the second bullet was repaired, and unnoticed for
        # two rounds). It regrew because "length" was marked resolved and
        # then nothing watched it: a property fixed once and afterwards
        # unwatched drifts back past its own threshold. That is why the
        # ceiling is an assertion and not a review habit.
        #
        # HEADROOM IS FIXTURE-RELATIVE, corrected in round 9. Round 7 read
        # its own 560 chars against the 650 ceiling and called the ~90
        # chars between them headroom for a rewording. They are not the
        # same thing: the message interpolates the turn value THREE times
        # plus the seat's sentinel and the project id, so what this
        # assertion measures is
        #     526 (constant) + 3*len(turn) + len(sentinel) + len(project_id)
        # and this fixture contributes only 34 chars of names
        # ('lei shi' x3, 'casey', 'PR-1002b') -- among the shortest
        # combinations that occur. Re-measured through this same code path
        # with names that actually appear in these docs: turn
        # 'ariadne-calvino', sentinel 'aleph-menard', project
        # 'class-env-var-precedence' gives 607, leaving 43; round 8's
        # pessimistic case measured 628, leaving 22. Every char added to
        # the turn value costs THREE. So the ceiling is tighter than this
        # fixture makes it look, and most of the ~90 chars visible here
        # are not spendable on wording. Raising this number needs the same
        # re-justification as editing the text below.
        assert len(captured.err) <= 650, (
            f"refusal message is {len(captured.err)} chars, over the 650 "
            "ceiling. It grew 709 -> 1049 once already while nobody was "
            "watching. Trim it, or re-justify the ceiling against the "
            "955 -> 709 -> 1049 trajectory -- do not just raise the number."
        )

        # --- GOLDEN MASTER (the backstop that closes the last hole) -----
        # Everything above still leaves the message's later text open: two
        # mutations beat the checks above by planting a nameless,
        # non-blacklisted assertion away from the first line -- "(A
        # colleague already took this one. Do not proceed.)" as an extra
        # line, and a bullet flipped to "This baton is not free: a
        # colleague already took it" -- both passing 184/1-skipped. No
        # heuristic closes that: the damaging content is ordinary English
        # with no fixed vocabulary. Since this whole PR *is* the message
        # text, the message is pinned verbatim. Any edit to it fails here
        # BY DESIGN; the fix is never to loosen this assertion but to
        # update the expected text and re-justify the new wording
        # against #3039.
        #
        # RE-JUSTIFIED for round 7: the message stops teaching grammar.
        #
        # WHAT LEFT, AND WHY IT IS NOT COMING BACK. Rounds 3-6 inlined the
        # turn-log reading rule here as two bullets. Bullet two was the
        # site of the held-looks-free inversion in rounds 2 and 4 -- "the
        # baton may in fact be free" for any non-`claimed` scoped line,
        # which is false in the common case because release and flip emit
        # TEXTUALLY IDENTICAL log lines. Measured end-to-end through the
        # real writers: init(turn=borges) + flip(to=ariadne, "please
        # review") logs "borges → ariadne: please review" with ariadne
        # genuinely HOLDING it, while init(turn=clio) + release(holder=
        # clio, sentinel="lei shi") logs "clio → lei shi: released" with it
        # genuinely free -- same shape, opposite truth. Round 5 repaired
        # the bullet into an honest disjunction, and that repair is what
        # grew the message from 709 chars to 1049, past the 955 round 3 had
        # already flagged as too long. A reading rule is prose a reader
        # consults deliberately; an error stream arriving mid-refusal is
        # not the place for it.
        #
        # The rule is not lost: engram-baton/SKILL.md carries it, with the
        # two gaps round 6 found in it closed -- init() is enumerated
        # alongside claim/flip/release (it is the only scoped writer that
        # can emit a line for a baton parked in a pool and NEVER claimed,
        # which is #3039's own motivating case), and branch one now keys on
        # a `<reason>` of exactly `claimed` instead of asking the reader to
        # recognize <A> as a pool marker -- which this PR's own thesis says
        # a reader on another install cannot do. Do NOT restore the bullets
        # here, and do NOT restore a confident branch anywhere:
        # under-claiming is the point of #3039.
        #
        # `baton flip` is deliberately NOT offered any more. It was bullet
        # one's remedy -- correct only for the colleague case -- and this
        # message no longer claims to identify that case, so naming it
        # would invite exactly the guess #3039 was filed against.
        #
        # Still true of the new text, and the things to re-confirm on any
        # future edit: it refuses without asserting that a person holds the
        # baton; it asserts neither "held" nor "free"; it names the turn
        # value AND this seat's own resolved sentinel so the mismatch is
        # visible; it carries no form of "steal"; it points at 3039 by
        # number; and the remedy it hands over is OFF the log.
        assert captured.err == (
            "baton claim: this baton's turn is 'lei shi', which this seat "
            "does not recognize as its pool marker (this seat resolves "
            "'casey'). This seat cannot tell from the turn log whether "
            "'lei shi' is a colleague who holds this baton or a pool "
            "marker belonging to an install whose primary_user this seat "
            "cannot resolve -- that is engram-alpha#3039. Settle it off "
            "the log: ask 'lei shi', or check the PR or project this "
            "baton anchors. To read the log yourself, run\n"
            "    baton show PR-1002b\n"
            "and see the engram-baton skill for how to scope a turn-log "
            "line to the turn value.\n"
        )

        # --- Existing token assertions (kept: not harmful, insufficient) -
        # Names the turn value, without asserting theft/personhood.
        assert "lei shi" in captured.err
        assert "steal" not in captured.err.lower()
        # Names this seat's OWN resolved sentinel, so the mismatch is visible.
        assert "casey" in captured.err
        # States the indeterminacy as a disjunction and lands on NEITHER
        # arm. Round 4's "means a colleague took it" + "baton flip" pair is
        # gone with the bullets: the message no longer claims to identify
        # the colleague case, so shipping that case's remedy would be the
        # over-claim #3039 was filed against.
        assert "cannot tell" in captured.err.lower()
        assert "colleague who holds this baton" in captured.err
        assert "pool marker belonging to an install" in captured.err
        # The remedy is OFF the log -- ask the agent the turn value names,
        # or read the PR/project the baton anchors. The log cannot settle
        # it from this seat, which is the whole content of #3039.
        assert "ask 'lei shi'" in captured.err
        assert "anchors" in captured.err
        # The reading rule is still reachable, just not reprinted here: the
        # message points at `baton show` for the log itself and at the
        # engram-baton skill for how to scope a line to the turn value.
        assert "baton show" in captured.err
        assert "turn log" in captured.err.lower()
        assert "engram-baton skill" in captured.err
        # Points at the issue by number.
        assert "3039" in captured.err

    def test_claim_disambiguation_surfaces_do_not_regress(self):
        """#3039: `baton claim --help`, engram-baton/SKILL.md and CLAUDE.md
        carry the same corrected disambiguation as the refusal message, and
        none of them may drift back to asserting that a person holds the
        baton.

        Added in round 4 because all three surfaces had ZERO coverage: a
        reviewer gutted the --help description back to "Refuses to steal
        from another agent who is currently holding it" -- the exact claim
        this PR exists to remove -- and the suite stayed green at 184
        passed / 1 skipped. The refusal message was pinned verbatim while
        the three surfaces a reader is at least as likely to consult were
        unguarded.

        HARDENED in round 7, because the round-4 guard was still
        defeatable. The reviewer gutted the claim sub-parser description to
        "`<B>` owns this baton and you must wait for them to pass it" --
        every required token retained, the meaning exactly inverted -- and
        this test passed at 185/1-skipped. Planted-token controls confirm
        the instrument really does read these files, so the miss was in
        the ASSERTIONS, not the plumbing: presence-and-count checks cannot
        see a meaning inversion that preserves the tokens. Two of the
        three surfaces are now pinned verbatim by the same golden-master
        instrument that has repeatedly proved discriminating on the
        refusal message itself.

        EXTENDED in round 11, on two fronts. First, the authored `--help`
        strings are now pinned as a complete ENUMERATION rather than
        field by field: rounds 6, 8 and 10 each shipped the same inversion
        through a different unpinned field at 185/1-green, so the thing
        that had to be closed was the ability of an unanticipated field to
        exist unpinned at all. Second, SKILL.md's failure-modes row is now
        pinned verbatim too. Rounds 7 and 8 deliberately left it on the
        cheaper count-based checks -- correctly, while the verb-list
        bullet carried a second copy whose disagreement caught inversion
        #4. Round 9 cut that copy, leaving the row as the only statement
        of the rule not already pinned verbatim, and round 10 inverted it
        with no banned vocabulary while green. Long prose that changes is
        still the right description of it; requiring deliberate
        re-justification to change it is now the right price.

        Deliberately cheap and stdlib-only: build the parser in-process
        (no subprocess) and read the two docs off disk.
        """
        parser = _baton.build_parser()
        # argparse exposes no public accessor for a subparser by name;
        # _SubParsersAction has been the stable shape across CPython 3.x.
        # Kept alongside the golden master below: without it, a layout
        # change would make the pin unreachable rather than red.
        subparser_actions = [
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        ]
        assert len(subparser_actions) == 1, (
            "baton's subparser layout changed -- this test can no longer "
            "locate the claim subparser, so it would pass vacuously"
        )
        claim_parser = subparser_actions[0].choices["claim"]
        claim_help = claim_parser.description
        # The RENDERED surface, whitespace-normalized (round 9). See the
        # "rendered --help surface" block below for why both exist.
        claim_help_rendered = " ".join(claim_parser.format_help().split())

        skill_md = (
            REPO_ROOT / "src" / "engram" / "skills" / "claude"
            / "engram-baton" / "SKILL.md"
        ).read_text()
        claude_md = (REPO_ROOT / "CLAUDE.md").read_text()

        # --- SLICE CLAUDE.md TO THE BATON PASSAGE -----------------------
        # Everything below runs against the PASSAGE, not the whole file.
        # Whole-file token checks are near-vacuous here: "identical"
        # occurs 9 times in CLAUDE.md, 8 of them in passages with nothing
        # to do with batons, so `assert "identical" in claude_md` could
        # not fail no matter what happened to the baton bullet. Two
        # anchors bound the passage; each occurs exactly once in the file.
        passage_start_anchor = "- `baton claim pool-X`"
        passage_end_anchor = "\n- The claimer drives the bunch toward fix-PRs"
        start = claude_md.find(passage_start_anchor)
        end = claude_md.find(passage_end_anchor)
        assert start != -1, (
            "CLAUDE.md no longer contains the baton-claim passage's "
            f"opening anchor {passage_start_anchor!r} -- the passage moved "
            "or was reworded. Re-anchor deliberately; do not fall back to "
            "asserting against the whole file, which is what made these "
            "checks vacuous in round 6."
        )
        assert end != -1 and end > start, (
            "CLAUDE.md no longer contains the baton-claim passage's "
            f"closing anchor {passage_end_anchor!r} after the opening one"
        )
        assert claude_md.count(passage_start_anchor) == 1, (
            "the CLAUDE.md opening anchor is no longer unique; the slice "
            "below could silently bound the wrong passage"
        )
        claude_passage = claude_md[start:end]
        # The passage must stay OUTSIDE the generated team-practice
        # region: edits inside it are overwritten wholesale the next time
        # tools/sync_team_practice.py renders TEAM-PRACTICE.md, so a
        # #3039 fix parked in there would silently revert.
        generated_end = claude_md.find(
            "<!-- END team-practice: which-colleague-takes-the-seat -->"
        )
        assert generated_end != -1 and start > generated_end, (
            "the CLAUDE.md baton passage is inside (or ahead of) the "
            "generated team-practice region -- content there is rendered "
            "from the team-practice repo and any fix made here is lost on "
            "the next sync"
        )

        # --- NEGATIVE CHECKS RUN OVER THE WHOLE FILE (round 9) ----------
        # The slice above is right for the POSITIVE checks and for the
        # verbatim pin: those have to name one passage, and a whole-file
        # token check would be near-vacuous (see the note above). But
        # scoping the NEGATIVE checks to the slice means every banned
        # phrase is legal three lines past the end anchor. Measured on
        # this tip at round 8: a sibling bullet just below the end anchor,
        # in the same section, carrying "is currently holding" and "it is
        # theirs" kept the suite green at 185 passed / 1 skipped. A reader
        # of that section does not stop at the anchor, so neither does
        # this. Measured free: all 13 phrases occur 0 times in the whole
        # of CLAUDE.md today.
        #
        # If one of these ever fires on a genuinely unrelated passage,
        # narrow the PHRASE in the shared tuples -- do not re-narrow these
        # checks back to the slice, which is the hole this closes.
        for banned in CLAIM_REFUSAL_BANNED_PHRASES:
            assert banned not in claude_md.lower(), (
                f"CLAUDE.md asserts somewhere that a person holds the "
                f"baton ({banned!r}) -- the #3039 claim this PR removed. "
                "The baton passage is not the only part of this file a "
                "reader acts on."
            )
        for banned in CLAIM_REFUSAL_BANNED_FREE_PHRASES:
            assert banned not in claude_md.lower(), (
                f"CLAUDE.md tells the reader somewhere that the baton is "
                f"(or may be) free on evidence that cannot establish it "
                f"({banned!r}) -- release and flip emit identical turn-log "
                "lines. State the disjunction and stop."
            )

        # name -> (text, how many times the scoped-shape token must appear).
        # The count matters, not just presence: a whole-file `in` check is
        # satisfied by any single statement of the rule. Measured while
        # writing this test -- reverting ONLY the verb-list row to round
        # 3's "last arrow line" left the suite green at 185 passed / 1
        # skipped, because the failure-modes row still carried the token.
        # Requiring the count is what makes a one-row regression fail.
        #
        # SKILL.md's count was 2 through round 8 and is 1 from round 9. The
        # second copy was CUT, NOT LOST: the verb-list bullet carried a
        # CONDENSED restatement of the turn-log reading rule, and round 8
        # found a FOURTH held-looks-free inversion in it (the `initialized`
        # case filed under "settled", which is #3039's own motivating case
        # and is indeterminate). A standing pre-commitment made before
        # round 6 -- "if that round finds a FOURTH site of the
        # held-looks-free inversion, CUT the disambiguation guidance rather
        # than sharpen it again" -- fired, because four attempts at a short
        # form produced four inversions in four wordings across rounds 2, 4
        # and 7. The rule survives in exactly ONE place, the line-by-line
        # audited failure-modes row, and the bullet keeps the pointer to
        # it. Do NOT read this 1 as drift and restore a second copy: the
        # lowered count IS the fix. Raising it back to 2 means re-arguing
        # the pre-commitment, not repairing a regression.
        SHAPE = "<A> → <B>: <reason>"
        surfaces = {
            "baton claim --help": (claim_help, 1),
            "engram-baton/SKILL.md": (skill_md, 1),
            "CLAUDE.md baton passage": (claude_passage, 1),
        }

        for name, (text, shape_count) in surfaces.items():
            # NEGATIVE first, so that the reviewer's actual regression --
            # gutting the description to "Refuses to steal from another
            # agent who is currently holding it" -- is reported as the
            # refuted claim it is. Were it ordered AFTER the vacuity guard
            # below, the gutted text (105 chars) would trip that guard
            # instead and report "unexpectedly short" -- true, but naming
            # the wrong defect. An EMPTY surface passes this loop vacuously
            # and is caught by the vacuity guard immediately below, so
            # nothing escapes either way.
            for banned in CLAIM_REFUSAL_BANNED_PHRASES:
                assert banned not in text.lower(), (
                    f"{name} asserts unconditionally that a person holds "
                    f"the baton ({banned!r}) -- the #3039 claim this PR "
                    "removed. If this phrase is legitimate in an unrelated "
                    "passage, narrow the phrase in "
                    "CLAIM_REFUSAL_BANNED_PHRASES rather than dropping "
                    "the guard."
                )
            # ...and the mirror-image blacklist (round 5): no surface may
            # land on "free" for a line this seat cannot classify. Verified
            # to discriminate on all three surfaces: at round 4 (commit
            # 1d9da1bc) --help and SKILL.md carried "handing the baton
            # back" + "may in fact be free", and CLAUDE.md carried "handing
            # it back", so this loop failed on every one of them.
            for banned in CLAIM_REFUSAL_BANNED_FREE_PHRASES:
                assert banned not in text.lower(), (
                    f"{name} tells the reader the baton is (or may be) free "
                    f"on evidence that cannot establish it ({banned!r}) -- "
                    "release and flip emit identical turn-log lines, so a "
                    "colleague's flip-held baton matches too. State the "
                    "disjunction and stop; narrow the phrase in "
                    "CLAIM_REFUSAL_BANNED_FREE_PHRASES rather than "
                    "dropping the guard."
                )
            # Guard the instrument: an empty description, or a doc that
            # moved and now reads as "", would make every substring
            # assertion here pass vacuously -- which is the exact failure
            # mode this test was added to close.
            assert text and len(text) > 200, (
                f"{name} is empty or unexpectedly short -- the assertions "
                "here would pass vacuously"
            )
            # POSITIVE: a blacklist alone would be satisfied by a surface
            # that says nothing at all, so require the corrected rule --
            # the turn-value-scoped log line, not round 3's "last arrow
            # line", which three turn-neutral writers (reopen, anchor,
            # set_colleague) can satisfy with a non-holder's name.
            assert text.count(SHAPE) >= shape_count, (
                f"{name} names the scoped turn-log line shape "
                f"{text.count(SHAPE)} time(s), expected at least "
                f"{shape_count}; an unscoped 'last arrow line' rule "
                "reintroduces #3039's false-holder diagnosis one hop "
                "downstream. If a passage was legitimately removed, lower "
                "the count here deliberately rather than dropping it."
            )
            assert "turn_reason" in text, (
                f"{name} no longer warns off turn_reason, which reads "
                "'claimed' on every claim"
            )
            # POSITIVE for round 5: the blacklist above only forbids landing
            # on "free"; silently DELETING the second branch would satisfy
            # it while leaving the reader with no account of the
            # non-`claimed` case at all. So require each surface to state
            # WHY it cannot classify that line -- that release and flip emit
            # the identical text -- and to hand over the off-log remedy that
            # replaces the verdict. Both tokens are absent from round 4
            # (1d9da1bc), so this pair fails there.
            assert "identical" in text.lower(), (
                f"{name} no longer explains WHY a non-'claimed' scoped line "
                "is indeterminate (release and flip emit the identical "
                "text). Without the reason, a later editor reinstates a "
                "confident branch."
            )
            assert "anchors" in text.lower(), (
                f"{name} no longer gives the off-log remedy (ask <B>, or "
                "check the PR/project the baton anchors) -- without it the "
                "message reports indeterminacy and strands the reader."
            )

        # --- THE RENDERED `--help` SURFACE (round 9) --------------------
        # Everything above reads `.description`, which is a FIELD of the
        # help surface, not the surface. argparse also renders `epilog`
        # (and, on the top-level parser, each subcommand's `help=`), and
        # nothing in this suite read either. Measured on this tip at
        # round 8: adding
        #     epilog="Refuses to steal from another agent who is
        #             currently holding it"
        # to p_claim ships that sentence into `baton claim --help` and the
        # suite stayed green at 185 passed / 1 skipped -- the round-4
        # regression, re-entering through an unread field. Same dimension
        # error as the rest of this PR: checking a component of the
        # surface instead of the surface.
        #
        # Whitespace is normalized FIRST because argparse re-wraps prose to
        # the terminal width: the shipped description already renders with
        # "'<A> → <B>:\n<reason>'" split across a line break, so a raw
        # substring blacklist over format_help() would miss any phrase that
        # happens to straddle a wrap -- and the wrap points move with
        # $COLUMNS, making the miss environment-dependent. Verified: a
        # planted "is currently holding" positioned across a wrap is missed
        # by the raw check and caught by the normalized one.
        for banned in CLAIM_REFUSAL_BANNED_PHRASES:
            assert banned not in claim_help_rendered.lower(), (
                "the rendered `baton claim --help` asserts unconditionally "
                f"that a person holds the baton ({banned!r}). It is not in "
                "the pinned description, so it arrived through `epilog` or "
                "another field nothing else reads."
            )
        for banned in CLAIM_REFUSAL_BANNED_FREE_PHRASES:
            assert banned not in claim_help_rendered.lower(), (
                "the rendered `baton claim --help` tells the reader the "
                f"baton is (or may be) free on evidence that cannot "
                f"establish it ({banned!r}) -- see the note on "
                "CLAIM_REFUSAL_BANNED_FREE_PHRASES."
            )
        # Guard the instrument: if format_help() ever returned the
        # description alone (or less), the two loops above would add
        # nothing over the ones that already ran on `.description`.
        assert " ".join(claim_help.split()) in claim_help_rendered, (
            "the rendered `baton claim --help` no longer contains the "
            "description -- this block is now checking something else"
        )
        assert len(claim_help_rendered) > len(" ".join(claim_help.split())), (
            "the rendered `baton claim --help` is no larger than its "
            "description; the blacklists above are then redundant rather "
            "than a second layer"
        )
        # A blacklist cannot see a meaning inversion that uses none of its
        # vocabulary -- that is exactly what round 6 proved on the
        # description ("`<B>` owns this baton and you must wait for them to
        # pass it" retains every token and inverts the meaning), and it is
        # why the description is ALSO pinned verbatim below. The same hole
        # is open on `epilog`, which no golden master covers. Measured:
        # with the two loops above in place, that sentence as an epilog
        # still passes them. So pin the field EMPTY: nothing reads it, so
        # nothing may ship through it. If an epilog is ever wanted, add a
        # verbatim pin for it here in the same breath as adding it.
        assert claim_parser.epilog is None, (
            "p_claim grew an `epilog`, which `baton claim --help` renders "
            "to the user and no test pins verbatim. Round 6's inversion "
            "used no blacklisted word, so the blacklists above would not "
            f"catch one here either. Pin it: {claim_parser.epilog!r}"
        )

        # --- THE COMPLETE ENUMERATION OF AUTHORED `--help` STRINGS ------
        # (round 11) Rounds 6, 8 and 10 each shipped round 6's exact
        # inversion through a DIFFERENT unpinned `--help` surface, every
        # one of them green at 185 passed / 1 skipped. Round 9 is the
        # instructive one: the block immediately above correctly REFUSES a
        # blacklist for `epilog` -- "a blacklist cannot see a meaning
        # inversion that uses none of its vocabulary" -- and then, ten
        # lines later, guards the subcommand `help=` one-liner with
        # exactly that blacklist. Round 10 walked round 6's inversion
        # straight through it. The right reasoning and the wrong
        # instrument, in one comment block.
        #
        # So the fix cannot be another field. Pinning only the two fields
        # round 10 named would leave round 12 to find the fifth surface.
        # What is pinned here is the ENUMERATION ITSELF: every authored
        # string argparse renders into a `--help` surface, gathered by
        # introspection and compared as a WHOLE dict. An unanticipated
        # field -- a `usage=`, a second positional, a new subcommand --
        # changes the dict and fails HERE. That is the property a
        # per-field checklist structurally cannot have.
        #
        # The named per-field guards above are KEPT rather than folded in:
        # each carries the argument for why that particular field is
        # dangerous, which a whole-dict comparison cannot express. This
        # block closes the gap between them; it does not retire them.
        claim_fields = _authored_help_strings(claim_parser)
        # `description` is pinned verbatim by the golden master below.
        # Substitute a sentinel rather than POPPING the key: popping would
        # let a DELETED description pass this dict comparison, and the
        # deleted-surface case is exactly what the vacuity guards above
        # exist for. Replacing keeps the key's presence load-bearing.
        assert "description" in claim_fields, (
            "argparse no longer exposes `description` -- the golden master "
            "below is checking a field this enumeration cannot see"
        )
        claim_fields["description"] = (
            "<pinned verbatim by the golden master below>"
        )
        assert claim_fields == {
            'prog': 'baton claim',
            'usage': None,
            'description': '<pinned verbatim by the golden master below>',
            'epilog': None,
            'arg:project_id:metavar': 'PROJECT-ID',
            'arg:project_id:help': 'Project identifier (e.g. pool-cli-gap, DESIGN-trust-tier-v2)',
        }, (
            "the authored `--help` strings on the claim sub-parser "
            "changed. This includes the POSITIONAL's `help=`, which "
            "renders into `baton claim --help` and which round 10 shipped "
            "round 6's inversion through while green. If you ADDED a "
            "field, it is a new user-facing surface: pin its text here in "
            "the same breath, and re-justify it against #3039. If you "
            f"edited one, re-justify the new wording: {claim_fields!r}"
        )

        # The parent parser is a SECOND surface with its own authored
        # strings -- its description and epilog, plus every subcommand's
        # `help=` one-liner, all of which render into `baton --help` and
        # none of which the claim sub-parser can see. Pinning only
        # `claim`'s one-liner would leave its fourteen siblings guarded by
        # the blacklist that has now lost three times; the scope of the
        # guard being replaced was the whole top-level surface, so the
        # scope of the pin is too.
        assert _authored_help_strings(parser) == {
            'prog': 'baton',
            'usage': None,
            'description': 'Multi-agent turn-state CLI — declare whose move it is on a shared project via explicit baton files.',
            'epilog': 'Protocol docs: docs/baton-protocol.md\nProjects dir:  /home/agents-shared/forum/projects/ (manual deploy)\n',
            'subparsers:metavar': 'subcommand',
            'subparsers:help': None,
            'subcommand:init:metavar': 'init',
            'subcommand:init:help': 'Create a new project baton file',
            'subcommand:flip:metavar': 'flip',
            'subcommand:flip:help': 'Pass the baton to another participant',
            'subcommand:claim:metavar': 'claim',
            'subcommand:claim:help': 'Claim a Project-layer baton from the pool (pool → self)',
            'subcommand:release:metavar': 'release',
            'subcommand:release:help': 'Return a Project-layer baton to the pool (self → pool sentinel)',
            'subcommand:status:metavar': 'status',
            'subcommand:status:help': 'List active projects (sorted by turn_since ascending)',
            'subcommand:mine:metavar': 'mine',
            'subcommand:mine:help': "Shorthand for 'baton status --mine'",
            'subcommand:show:metavar': 'show',
            'subcommand:show:help': 'Display a project file (read-only)',
            'subcommand:close:metavar': 'close',
            'subcommand:close:help': 'Mark a project merged or cancelled',
            'subcommand:reopen:metavar': 'reopen',
            'subcommand:reopen:help': 'Flip a closed baton back to an active status (inverse of close)',
            'subcommand:gc:metavar': 'gc',
            'subcommand:gc:help': 'batch-close PR-batons whose GitHub PR is merged/closed',
            'subcommand:rename:metavar': 'rename',
            'subcommand:rename:help': 'Update the human-readable project title',
            'subcommand:anchor:metavar': 'anchor',
            'subcommand:anchor:help': 'Set or update the github: anchor on an existing baton',
            'subcommand:add-participant:metavar': 'add-participant',
            'subcommand:add-participant:help': "Add an agent to an existing baton's participants list",
            'subcommand:set-colleague:metavar': 'set-colleague',
            'subcommand:set-colleague:help': 'Set or change the colleague: field on an existing baton (#2764)',
            'subcommand:merge:metavar': 'merge',
            'subcommand:merge:help': 'Gate-checked squash merge for a PR baton',
        }, (
            "the authored `--help` strings on the top-level `baton` "
            "parser changed -- most likely a subcommand's `help=` "
            "one-liner, which renders in `baton --help` and nowhere else. "
            "Adding or renaming a subcommand is expected to fail here: "
            "pin the new one-liner deliberately. For `claim`, re-justify "
            f"it against #3039: {_authored_help_strings(parser)!r}"
        )
        # The subcommand's `help=` one-liner renders on a DIFFERENT
        # surface -- the top-level `baton --help` listing -- so reading
        # the claim sub-parser cannot see it. Measured on this tip: all 13
        # banned phrases occur 0 times in the whole rendered top-level
        # help, so this costs nothing today. Round 11 pinned those
        # one-liners verbatim in the enumeration above, which is what
        # actually guards them; this loop is kept as a cheap second layer
        # over the RENDERED surface, not as the primary instrument.
        top_level_rendered = " ".join(parser.format_help().split()).lower()
        for banned in (
            CLAIM_REFUSAL_BANNED_PHRASES + CLAIM_REFUSAL_BANNED_FREE_PHRASES
        ):
            assert banned not in top_level_rendered, (
                f"the rendered top-level `baton --help` carries {banned!r} "
                "-- most likely through a subcommand's `help=` one-liner, "
                "which is rendered there and nowhere else"
            )

        # --- GOLDEN MASTERS (round 7) -----------------------------------
        # The loop above is presence-and-count only, and round 6 proved
        # that is defeatable by a meaning inversion that keeps every
        # token: "`<B>` owns this baton and you must wait for them to pass
        # it" retains the shape token, "turn_reason", "identical" and
        # "anchors" while asserting exactly the unconditional holder claim
        # #3039 was filed against. No heuristic closes that -- the
        # damaging content is ordinary English with no fixed vocabulary --
        # so the two SHORT surfaces are pinned verbatim, the instrument
        # that has caught every planted mutation on the refusal message.
        # Editing either surface fails here BY DESIGN: update the expected
        # text and re-justify the new wording against #3039, exactly as
        # for the refusal message. Line-number citations are deliberately
        # absent from both -- they are prose a reader trusts, they go
        # stale silently, and a stale one shipped for two rounds of this
        # PR; engram-alpha#3039 is the durable pointer.
        assert claim_help == (
            'Take a Project-layer baton for yourself. Only succeeds '
            "when the current turn is the pool sentinel (the install's "
            "primary_user). Refuses when the turn is not this seat's "
            'recognized pool marker — which may be a colleague '
            'genuinely holding it, or a pool or maintainer marker '
            'written by a seat with a different primary_user '
            '(engram-alpha#3039). To see what the turn log records, '
            "run 'baton show <project-id>' and find the LAST turn log "
            "line shaped '<A> → <B>: <reason>' whose <B> -- the name "
            'just after the FIRST arrow -- is the turn value. Only '
            'such a line records who the turn was handed to: anchor, '
            'colleague and reopen lines carry arrows of their own and '
            'name no holder, and turn_reason does not discriminate '
            "(every claim writes 'claimed'). '<pool> → <B>: claimed' "
            "means a colleague took it -- use 'baton flip' with an "
            'explicit handoff agreement. ANY OTHER such line leaves '
            'this seat unable to tell which case it is: release and '
            'flip emit the identical text, so the line is either a '
            'colleague taking it on a flip or a return to a pool '
            'marker belonging to a seat whose primary_user this one '
            "cannot resolve -- that is #3039. A <reason> of 'released' "
            "is release's default but is user-settable, so it hints "
            'and never settles. Settle it off the log: ask <B>, or '
            'check the PR or project this baton anchors -- do not flip '
            'on a guess.\n'
            '\n'
            'Vocabulary distinction: claim/release are Project-layer '
            'verbs (single driver, rare passes). flip is the PR-layer '
            'verb (frequent passing through fairy → reviewer → '
            'colleague → maintainer).'
        ), (
            "the claim sub-parser description changed; re-justify the new "
            f"wording against #3039: {claim_help!r}"
        )

        assert claude_passage == "\n".join((
            '- `baton claim pool-X` — `turn:` flips to the claimer; refuses when `turn:`',
            "  is not this seat's recognized pool marker — either a colleague is holding it,",
            '  or it is a pool/maintainer marker written by a seat with a different',
            '  `primary_user` (#3039). Before assuming someone holds it, run `baton show`',
            '  and find the last turn-log line shaped `<A> → <B>: <reason>` whose `<B>` —',
            '  the name just after the FIRST arrow — is the `turn:` value; `anchor`,',
            '  `set_colleague` and `reopen` write arrows without touching `turn:`, so "the',
            '  last arrow line" can name a non-holder. `<pool> → <B>: claimed` = a',
            '  colleague took it. Any other such line is **indeterminate from your seat** —',
            '  `release` and `flip` emit the identical text, so it is either a colleague',
            '  taking it on a flip (held) or a return to a pool marker belonging to a seat',
            '  whose `primary_user` you cannot resolve (free); that is #3039. Settle it off',
            '  the log — ask `<B>`, or check the PR/project the baton anchors — and do not',
            '  flip on a guess. `turn_reason` does not discriminate (every claim writes',
            '  `"claimed"`).',
        )), (
            "the CLAUDE.md baton passage changed; re-justify the new "
            f"wording against #3039: {claude_passage!r}"
        )

        # --- THE SKILL.md FAILURE-MODES ROW, PINNED (round 11) ----------
        # Rounds 7 and 8 both tiered this row's exposure a Suggestion, on
        # the ground that SKILL.md is long prose that legitimately changes
        # and a golden master over it would be noise. That was right while
        # a SECOND copy existed -- and the two copies disagreeing is
        # literally how round 8 found inversion #4, in the condensed
        # restatement carried by the verb-list bullet.
        #
        # Round 9 CUT that copy under a standing pre-commitment. The cut
        # was correct, but it removed the cross-copy detector. Measured on
        # this tip, the rule is stated user-facing in exactly three places
        # -- the CLAUDE.md baton passage, the claim sub-parser
        # description, and this row -- and the first two are pinned
        # verbatim just above. So this row is the only statement of the
        # rule a vocabulary-free inversion can still enter through, and
        # nothing is positioned to contradict it when it does: the pinned
        # pair cannot silently disagree, but neither is compared against
        # SKILL.md. Round 10 inverted this row's case (b) using no
        # blacklisted vocabulary and stayed green at 185 passed / 1
        # skipped; a count-based check cannot see that.
        #
        # So it is pinned, and the cost is the point: this is the single
        # surviving statement of a rule that has inverted FOUR times, so
        # changing it SHOULD require deliberate re-justification rather
        # than passing as ordinary prose drift.
        skill_claim_rows = [
            ln for ln in skill_md.splitlines()
            if ln.startswith("| `baton claim:")
        ]
        assert len(skill_claim_rows) == 1, (
            "engram-baton/SKILL.md no longer has exactly one failure-modes "
            f"row for `baton claim` ({len(skill_claim_rows)} found), so the "
            "pin below would compare the wrong line -- or, at zero, pass "
            "over a surface that had been deleted outright"
        )
        assert skill_claim_rows[0] == (
            '| `baton claim: ... which this seat does not recognize as its '
            'pool marker` | Either a colleague is genuinely holding it, OR '
            'the turn is a pool/maintainer marker written by a seat with a '
            'different `primary_user` (#3039) | Run `baton show <id>` and '
            'find the **last turn-log line shaped `<A> → <B>: <reason>` '
            'whose `<B>` — the name just after the FIRST arrow — is the '
            '`turn:` value**. Only such a line records who the turn was '
            'handed to: `anchor`, `set_colleague` and `reopen` write '
            'arrows of their own without touching `turn:`, so "the last '
            'arrow line" can name an agent who does not hold the baton '
            '(the same false-holder error as #3039). Four writers can '
            'produce a scoped line — `init`, `claim`, `flip`, `release` — '
            'and you classify it by tokens you can actually read, never by '
            'deciding whether `<A>` is a pool marker: on another install '
            'you cannot, and that inability IS #3039. **(a) `<reason>` is '
            'exactly `claimed`** → `claim` wrote it; it is the only one of '
            'the four that hardcodes that word, so `<B>` took the baton '
            'from a pool and holds it. Letter them and flip only by '
            'agreement. The other three take free-text reasons, so a '
            'hand-typed `claimed` can imitate this — strong evidence, not '
            'proof. **(b) `<A>` is literally `initialized`** → `init` '
            'wrote it: the baton has carried this same `turn:` since '
            'creation and was **never claimed**, so `<B>` is only whatever '
            "the creator set — very often their own install's pool marker. "
            "That is #3039's motivating case and it is **indeterminate "
            'from your seat**. **(c) anything else** → also '
            '**indeterminate from your seat**: `release` and `flip` emit '
            'the identical text, so the line is either a colleague taking '
            'it on a flip (held) or a return to a pool marker belonging to '
            'a seat whose `primary_user` you cannot resolve (free) — that '
            "is #3039. A `<reason>` of `released` is `release`'s default "
            'but is user-settable, so it hints and never settles. In (b) '
            'and (c), settle it off the log — ask `<B>`, or check the '
            'PR/project the baton anchors — and do not flip on a guess. Do '
            '**not** judge by `turn_reason`: every claim writes '
            '`"claimed"`, so a genuinely-held baton reads there as though '
            'no handoff happened. |'
        ), (
            "the engram-baton/SKILL.md failure-modes row changed. Since "
            "round 9 cut the condensed copy, this is the only statement "
            "of the turn-log reading rule that is not already pinned "
            "verbatim, and the cross-copy disagreement that caught "
            "inversion #4 no longer exists to catch a fifth. Re-justify "
            "the new wording against #3039 before updating this pin: "
            f"{skill_claim_rows[0]!r}"
        )

        # --- THE SKILL.md VERB-LIST BULLET, PINNED (round 12 fold-in) ---
        # Round 9 cut the CONDENSED READING RULE out of this bullet, and
        # the comment above records that correctly. What it left behind is
        # not nothing: the bullet still states the refusal SEMANTICS -- a
        # `turn:` this seat does not recognize may be a colleague holding
        # it, OR a pool/maintainer marker written by a seat with a
        # different `primary_user`. That disjunction IS #3039, and until
        # now nothing pinned it. The count-based SKILL.md check above
        # counts the scoped turn-log SHAPE token, which this bullet no
        # longer carries after round 9, so it reads the failure-modes row
        # and not this line; the blacklists cannot see an inversion
        # written without their vocabulary. Measured in round 12: this
        # bullet's meaning was rewritten using none of the 13 banned
        # phrases and the suite stayed green at 185 passed / 1 skipped.
        #
        # Same instrument and same price as the failure-modes row above.
        # This bullet is the statement of the rule a reader meets FIRST,
        # in the verb list they consult to use the command at all, so
        # changing it should cost a deliberate re-justification rather
        # than passing as ordinary prose drift.
        skill_claim_verb_rows = [
            ln for ln in skill_md.splitlines()
            if ln.startswith("- **`baton claim <project-id>`**")
        ]
        assert len(skill_claim_verb_rows) == 1, (
            "engram-baton/SKILL.md no longer has exactly one verb-list "
            f"bullet for `baton claim` ({len(skill_claim_verb_rows)} "
            "found), so the pin below would compare the wrong line -- or, "
            "at zero, pass over a surface that had been deleted outright"
        )
        assert skill_claim_verb_rows[0] == (
            "- **`baton claim <project-id>`** — take ownership from the "
            "pool sentinel. Refuses when `turn:` is not this seat's "
            "recognized pool marker — which may be a colleague holding "
            "it, or a pool/maintainer marker written by a seat with a "
            "different `primary_user` (#3039). The full reading rule, "
            "with the evidence for each case, is in **Failure modes + "
            "recovery** below — the `baton claim` refusal points here "
            "rather than reprinting it."
        ), (
            "the engram-baton/SKILL.md `baton claim` verb-list bullet "
            "changed. It carries the refusal's DISJUNCTION -- colleague-"
            "held OR a foreign install's pool marker -- which no other "
            "check on this file sees, because round 9 cut the turn-log "
            "shape token out of it. Re-justify the new wording against "
            f"#3039 before updating this pin: {skill_claim_verb_rows[0]!r}"
        )

    def test_claim_refuse_invoker_not_in_participants(self, monkeypatch, capsys):
        """Claim when invoker not in participants → EXIT_VALIDATION."""
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n == "casey")

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1003", turn="casey", participants="[ariadne, mneme]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1003")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "participant" in captured.err

    def test_claim_refuse_missing_project(self, capsys):
        """Claim of non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-1004")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()


# ===========================================================================
# CATEGORY 9: release subcommand
# ===========================================================================

class TestRelease:
    """baton release: happy path, state-guard refusals, --done flag tests."""

    def test_release_happy_path(self, monkeypatch):
        """Release posts to the release route with pool_sentinel=casey."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1101", title="claimed project", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1101", done=False, reason="released")
        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1101/release"
        assert body["pool_sentinel"] == "casey"
        assert body["reason"] == "released"
        assert body["done"] is False

    def test_release_custom_reason(self, monkeypatch):
        """Release with --reason passes the provided reason in the POST body."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1102", turn="borges", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-1102", done=False, reason="stepping back for review",
        )
        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert "stepping back for review" in body["reason"]

    def test_release_refuse_non_holder(self, monkeypatch, capsys):
        """Release when invoker is not current holder → EXIT_STATE."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1103", turn="ariadne", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1103", done=False, reason="released")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_release(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "ariadne" in captured.err

    def test_release_done_appends_to_title(self, monkeypatch):
        """release --done passes done=True in POST body (server marks title)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1104", title="cli gap work", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1104", done=True, reason="released")
        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["done"] is True

    def test_release_done_idempotent(self, monkeypatch):
        """release --done on already-(done) title passes done=True (server handles it)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1105", title="cli gap work (done)", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1105", done=True, reason="released")
        _baton.cmd_release(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["done"] is True


# ===========================================================================
# CATEGORY 10: anchor subcommand + --github init flag
# ===========================================================================

import re as _re
# Mirrors production `_GITHUB_ANCHOR_RE` in src/engram/tools/baton.py.
# It had drifted: this test-local copy still only accepted the BARE
# `pr/<N>` form and rejected the repo-qualified `pr/<owner>/<repo>/<N>`
# that #1715 introduced, so a fixture using the recommended (qualified)
# form failed a pattern check that production accepts. Kept as a
# deliberate local copy rather than an import (this file asserts the
# WIRE shape independently of the module under test) -- but it has to
# actually match. See tests/test_anchor_regex_parity.py for the pinned
# cross-parser contract.
_GITHUB_ANCHOR_PATTERN = _re.compile(r"^(?:pr/(?:[\w.-]+/[\w.-]+/)?\d+|project/\d+)$")


class TestAnchor:
    """baton anchor subcommand and baton init --github flag."""

    def test_init_github_pr_writes_frontmatter(self, monkeypatch):
        """init --github passes the anchor in the API body (server writes frontmatter)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-env-home", title="env home test",
            participants="borges,ariadne", status="planning",
            turn=None, github="pr/example-org/example-repo/123", colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/example-org/example-repo/123"
        assert _GITHUB_ANCHOR_PATTERN.match(body["github"])

    def test_init_github_pr_reflected_in_show(self, monkeypatch, capsys):
        """init --github is sent to the API; baton show reflects the field via API."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        # Step 1: init — verify github field in POST body
        client = _make_client({"seq": 1})
        args_init = argparse.Namespace(
            project_id="PR-show-test", title="show test",
            participants="borges,ariadne", status="planning",
            turn=None, github="pr/example-org/example-repo/456", colleague=None,
        )
        _baton.cmd_init(args_init, MULTI_CONFIG, "borges", client)
        _, init_body = client.post.call_args[0]
        assert init_body["github"] == "pr/example-org/example-repo/456"

        # Step 2: show — API returns the server-written state including github field
        raw_with_github = (
            "---\n"
            "project: PR-show-test\n"
            "title: show test\n"
            "status: planning\n"
            "turn: borges\n"
            "turn_since: 2026-01-01T00:00:00Z\n"
            'turn_reason: "initialized"\n'
            "participants: [borges, ariadne]\n"
            "github: pr/example-org/example-repo/456\n"
            "---\n\n"
            "# PR-show-test\n\n"
            "## Turn log\n\n"
            "- 2026-01-01T00:00:00Z initialized\n"
        )
        client2 = _make_client(get_return={"raw": raw_with_github})
        args_show = argparse.Namespace(project_id="PR-show-test")
        _baton.cmd_show(args_show, MULTI_CONFIG, "borges", client2)
        captured = capsys.readouterr()
        assert "pr/example-org/example-repo/456" in captured.out

    def test_init_github_rejects_malformed_value(self, engram_home):
        """init --github with malformed value exits non-zero (validation before API)."""
        _write_config(engram_home, "borges")
        for bad_val in ("xyz", "pr/abc", "pr/", "project/", "123"):
            rc, stdout, stderr = _run_baton(
                engram_home,
                "init", "PR-bad-anchor",
                "--title", "bad anchor test",
                "--participants", "borges,ariadne",
                "--github", bad_val,
            )
            assert rc != 0, (
                f"Expected non-zero exit for --github '{bad_val}'; got rc={rc}"
            )

    # --- #1924: the mint path must not create what gc/flip/merge refuse -----

    def test_init_refuses_on_explicit_unqualified_pr_anchor(self, monkeypatch, capsys):
        """#1924: --github pr/<N> with no repo REFUSES at creation.

        #1918/#1920 made gc/flip/merge refuse-and-count an unqualified PR
        anchor; cmd_init used to mint one silently (then, for a stopgap
        period, only warn about it), so the refusal surfaced weeks later on
        whoever tried to merge. Refusing here moves that signal to the one
        moment the author can act on it -- settled as REFUSE, not warn, by
        forum #411 (2026-09-13, Kepler ruling): see the comment at the call
        site for the full governance history and the cwd-inference exclusion.
        """
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-777", title="unqualified anchor",
            participants="borges,ariadne", status="planning",
            turn=None, github="pr/777", colleague=None, repo=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        err = capsys.readouterr().err
        assert "refusing to create" in err          # names the real consequence
        assert "--repo <owner>/<repo>" in err        # first fix path
        assert "--github pr/<owner>/<repo>/777" in err  # second fix path
        assert "fix it after the fact" not in err    # no anchor exists to fix

    def test_init_refuses_on_auto_derived_bare_anchor(self, monkeypatch, capsys):
        """#1924: the SILENT path -- no --github at all, anchor auto-derived
        from a PR-<N> project_id. This is the plainest possible invocation and
        was the one that produced most unqualified anchors in practice."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-1234", title="auto derived",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None, repo=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        err = capsys.readouterr().err
        # the message must name WHY the anchor exists at all, or the caller
        # cannot tell why they are being refused over a flag they never passed
        assert "auto-derived" in err
        assert "--repo" in err

    def test_init_accepts_repo_qualified_via_repo_flag(self, monkeypatch):
        """#1924 gate does not fire when --repo qualifies the derived anchor."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-1234", title="qualified via --repo",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
            repo="example-org/example-repo",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        _, body = client.post.call_args[0]
        assert body["github"] == "pr/example-org/example-repo/1234"

    def test_init_accepts_fully_qualified_github_anchor(self, monkeypatch):
        """#1924 gate does not fire on an already-qualified --github value."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-99", title="fully qualified",
            participants="borges,ariadne", status="planning",
            turn=None, github="pr/example-org/example-repo/99",
            colleague=None, repo=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        _, body = client.post.call_args[0]
        assert body["github"] == "pr/example-org/example-repo/99"

    def test_init_non_pr_baton_is_unaffected(self, monkeypatch):
        """#1924 is scoped to anchors that RESOLVE TO A PR. A project baton
        (and any baton with no github anchor) must still init cleanly --
        widening the gate to those would break every DESIGN-* project."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        for gh in (None, "project/4"):
            client = _make_client({"seq": 1})
            args = argparse.Namespace(
                project_id="DESIGN-trust-tier", title="design baton",
                participants="borges,ariadne", status="planning",
                turn=None, github=gh, colleague=None, repo=None,
            )
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
            _, body = client.post.call_args[0]
            assert body["github"] == gh

    def test_anchor_sets_github_on_existing_baton(self, monkeypatch):
        """init → anchor: the anchor API is called with github=project/4."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})

        # Step 1: init without --github
        args_init = argparse.Namespace(
            project_id="pool-data-correctness", title="data correctness work",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
        )
        _baton.cmd_init(args_init, MULTI_CONFIG, "borges", client)
        init_body = client.post.call_args[0][1]
        assert init_body.get("github") is None

        # Simulate server write: anchor can now read the project via API
        client.get.return_value = {"raw": _make_raw_baton(
            "pool-data-correctness", title="data correctness work",
            turn="borges", status="planning",
        )}

        # Step 2: set anchor
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        args_anchor = argparse.Namespace(
            project_id="pool-data-correctness", github="project/4",
        )
        _baton.cmd_anchor(args_anchor, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/pool-data-correctness/anchor"
        github_val = body["github"]
        assert github_val == "project/4"
        assert _GITHUB_ANCHOR_PATTERN.match(github_val)

    def test_anchor_updates_existing_github_field(self):
        """anchor on a baton that already has github: field calls API with new value."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-update-anchor", github="pr/100")},
        )
        args = argparse.Namespace(project_id="PR-update-anchor", github="pr/200")
        _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["github"] == "pr/200"

    def test_anchor_missing_project_exits_nonzero(self, capsys):
        """anchor on a non-existent project exits non-zero."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", github="pr/99")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_anchor_rejects_malformed_value(self, engram_home):
        """anchor with malformed --github value exits non-zero (validation before API)."""
        _write_config(engram_home, "borges")
        for bad_val in ("xyz", "pr/abc", "github.com/org/repo", "notaformat"):
            rc, stdout, stderr = _run_baton(
                engram_home,
                "anchor", "PR-1201",
                "--github", bad_val,
            )
            assert rc != 0, (
                f"Expected non-zero exit for --github '{bad_val}'; got rc={rc}"
            )

    def test_anchor_audit_line_appended(self):
        """anchor calls the API; server appends audit line to the turn log."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-1202")},
        )
        args = argparse.Namespace(project_id="PR-1202", github="pr/77")
        _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1202/anchor"
        assert body["github"] == "pr/77"

    def test_init_without_github_does_not_write_github_field(self, monkeypatch):
        """init without --github posts no github field (or null) to the API."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-no-github", title="no anchor",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        github_val = body.get("github")
        assert github_val is None

    def test_anchor_github_project_format(self):
        """anchor with project/<N> value is accepted and posted to the API correctly."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-1204")},
        )
        args = argparse.Namespace(project_id="PR-1204", github="project/4")
        _baton.cmd_anchor(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        github_val = body["github"]
        assert github_val == "project/4"
        assert _GITHUB_ANCHOR_PATTERN.match(github_val)


# ===========================================================================
# CATEGORY 11: reopen subcommand
# ===========================================================================

class TestReopen:
    """baton reopen: happy path, state-guard refusals, audit-line, and argparse tests."""

    def test_reopen_merged_baton_to_in_progress(self):
        """Reopen a merged baton: API is called with new_status=in-progress."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1301", title="reopened project", status="merged",
                turn="borges", participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1301", status="in-progress")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1301/status"
        assert body["new_status"] == "in-progress"

    def test_reopen_cancelled_baton_to_in_progress(self):
        """Reopen a cancelled baton: API is called with new_status=in-progress."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1302", status="cancelled", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1302", status="in-progress")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "in-progress"

    def test_reopen_with_explicit_status_flag(self):
        """Reopen with --status in-review: API called with new_status=in-review."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1303", status="merged", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1303", status="in-review")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["new_status"] == "in-review"

    def test_reopen_refuses_already_active(self, capsys):
        """Reopen on already-active baton → EXIT_VALIDATION with status in message."""
        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-1304", status="in-progress", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1304", status="in-progress")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "in-progress" in captured.err
        assert "not closed" in captured.err or "already" in captured.err.lower() or "flip" in captured.err

    def test_reopen_refuses_invalid_target_status(self, engram_home):
        """Reopen with --status merged (closed status) → argparse rejects."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home,
            "reopen", "PR-1305", "--status", "merged",
        )
        assert rc != 0
        assert "invalid choice" in stderr or "merged" in stderr

    def test_reopen_with_status_planning(self):
        """Reopen with --status planning calls API with new_status=planning."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1306", status="merged", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1306", status="planning")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        _, body = client.post.call_args[0]
        assert body["new_status"] == "planning"

    def test_reopen_refuses_missing_file(self, capsys):
        """Reopen of non-existent project → EXIT_VALIDATION, not-found error."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", status="in-progress")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_reopen_appends_audit_log(self):
        """reopen API call includes a reason with the status transition (server appends)."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1306", status="merged", turn="borges",
                participants="[borges, ariadne]", turn_reason="merged successfully",
            )},
        )
        args = argparse.Namespace(project_id="PR-1306", status="in-progress")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-1306/status"
        assert "reopened" in body["reason"]
        assert "borges" in body["reason"]
        assert "merged" in body["reason"]
        assert "in-progress" in body["reason"]

    def test_reopen_output_message_style(self, capsys):
        """reopen prints a confirmation line with project ID and new status."""
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-1308", status="merged", turn="borges",
                participants="[borges, ariadne]",
            )},
        )
        args = argparse.Namespace(project_id="PR-1308", status="in-progress")
        _baton.cmd_reopen(args, MULTI_CONFIG, "borges", client)

        captured = capsys.readouterr()
        assert "PR-1308" in captured.out
        assert "in-progress" in captured.out


# ===========================================================================
# CATEGORY 13: Pool sentinel resolution (issue #606)
# ===========================================================================

class TestPoolSentinel:
    """Pool sentinel is derived from config primary_user, not hardcoded."""

    def test_sentinel_resolves_to_non_casey_primary_user(self, engram_home, monkeypatch):
        """With primary_user='alice', pool sentinel is 'alice' — claim/release use alice.

        This is the core fresh-install correctness test from issue #606:
        a non-casey primary_user install must work end-to-end.
        """
        alice_config = {"mode": "multi", "agent_name": "borges", "primary_user": "alice"}
        _write_config(engram_home, agent_name="borges", primary_user="alice")
        # _pool_sentinel() reads os.environ.get("ENGRAM_HOME") directly, not the
        # module-level constant.  Use setenv (not setattr) to redirect the config read.
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n in ("alice",))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "pool-alice-test", title="non-casey sentinel test", turn="alice",
                participants="[borges, ariadne]", status="in-progress",
            )},
        )

        # Claim: _pool_sentinel() must read config → "alice" (not "casey")
        args_claim = argparse.Namespace(project_id="pool-alice-test")
        _baton.cmd_claim(args_claim, alice_config, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["pool_sentinel"] == "alice"
        assert "casey" not in claim_body["pool_sentinel"]

        # Simulate server write (borges now holds baton)
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        client.get.return_value = {"raw": _make_raw_baton(
            "pool-alice-test", turn="borges", participants="[borges, ariadne]",
        )}
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        # Release: sentinel should still be "alice"
        args_release = argparse.Namespace(
            project_id="pool-alice-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, alice_config, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "alice"

    def test_sentinel_case_normalizes_capitalized_primary_user(self, engram_home, monkeypatch):
        """primary_user='Alice' (capitalized) in config normalizes to sentinel 'alice'.

        Regression test for PR #612 round-2: _pool_sentinel() used to return
        the raw config string, so a capitalized primary_user would silently
        fail all sentinel comparisons.  The fix applies .lower() in _pool_sentinel().
        """
        alice_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alice"}
        _write_config(engram_home, agent_name="borges", primary_user="Alice")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_is_pool_sentinel", lambda n: n in ("alice",))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "pool-alice-cap-test", title="capitalized primary_user test", turn="alice",
                participants="[borges, ariadne]", status="in-progress",
            )},
        )

        # Claim: sentinel must resolve to lowercase "alice" despite "Alice" in config
        args_claim = argparse.Namespace(project_id="pool-alice-cap-test")
        _baton.cmd_claim(args_claim, alice_config, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["pool_sentinel"] == "alice"

        # Simulate server write
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        client.get.return_value = {"raw": _make_raw_baton(
            "pool-alice-cap-test", turn="borges", participants="[borges, ariadne]",
        )}
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        # Release: sentinel still lowercase "alice"
        args_release = argparse.Namespace(
            project_id="pool-alice-cap-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, alice_config, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "alice"

    def test_sentinel_accepts_short_form_first_token(self, engram_home, monkeypatch):
        """Pool initialized with turn='alex' works when primary_user='Alex Chen'.

        Regression test for issue #1309: _pool_sentinel() returns 'alex chen',
        but pools are often initialized with just the first token ('alex').
        _is_pool_sentinel must accept both forms.
        """
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "pool-short-form-test", title="short-form sentinel test", turn="alex",
                participants="[borges, ariadne]", status="in-progress",
            )},
        )

        # Claim: _is_pool_sentinel("alex") must return True for sentinel "alex chen"
        args_claim = argparse.Namespace(project_id="pool-short-form-test")
        _baton.cmd_claim(args_claim, owner_config, "borges", client)

        _, claim_body = client.post.call_args[0]
        assert claim_body["agent"] == "borges"
        assert claim_body["pool_sentinel"] == "alex chen"

        # Simulate server write (borges holds baton after claim)
        client.reset_mock()
        client.post.return_value = {"seq": 1}
        client.get.return_value = {"raw": _make_raw_baton(
            "pool-short-form-test", turn="borges", participants="[borges, ariadne]",
        )}
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        # Release: sentinel must be full "alex chen"
        args_release = argparse.Namespace(
            project_id="pool-short-form-test", done=False, reason="released",
        )
        _baton.cmd_release(args_release, owner_config, "borges", client)

        _, release_body = client.post.call_args[0]
        assert release_body["pool_sentinel"] == "alex chen"

    def test_merge_gate2_accepts_short_form_sentinel(self, engram_home, monkeypatch, capsys):
        """baton merge Gate 2 accepts short-form sentinel 'alex' with primary_user='Alex Chen'.

        Regression test for the missed comparison site in cmd_merge (issue #1309):
        Gate 2 used raw != sentinel instead of _is_pool_sentinel, so a PR baton
        with turn='alex' (short form, written by baton flip) would block merge
        with "turn is with lei — not presented for merge."
        """
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("unknown", "no gh"))

        # Baton with short-form sentinel in turn (the bug trigger). Repo-
        # qualified github: anchor so this reaches gate 2 rather than
        # tripping the #1918 no-repo refusal gate that now sits before it;
        # the monkeypatched CI/approval fakes above make gates 3/4 no-ops
        # regardless of repo.
        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9999", title="short-form merge gate test", turn="alex",
                participants="[borges, ariadne]", status="in-progress",
                github="pr/example-org/example-repo/9999",
            )},
        )

        # --dry-run --force: skips CI (Gate 3) + approval (Gate 4); no real gh call.
        # Should pass Gate 2 without "not presented for merge" error.
        args = argparse.Namespace(pr="PR-9999", dry_run=True, force=True)
        _baton.cmd_merge(args, owner_config, "borges", client)

        captured = capsys.readouterr()
        assert "gate 2 (turn=maintainer): PASS" in captured.out

    def test_merge_gate2_accepts_named_maintainer_not_sentinel(self, engram_home, monkeypatch, capsys):
        """#2913: the actual bug. This team's real convention -- every flip
        performed this session, and CLAUDE.md's own baton lifecycle -- ends
        a PR's review at a NAMED maintainer agent (e.g. "kepler"), never
        the literal pool sentinel string. Pre-#2913, `_is_pool_sentinel`
        alone would refuse this exact, correct, real-world state."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Lei Shi"}
        _write_config(engram_home, agent_name="borges", primary_user="Lei Shi")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("unknown", "no gh"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9998", title="named-maintainer merge gate test", turn="kepler",
                participants="[clio, ariadne, kepler]", status="in-progress",
                github="pr/example-org/example-repo/9998", colleague="ariadne",
            )},
        )
        args = argparse.Namespace(pr="PR-9998", dry_run=True, force=True)
        _baton.cmd_merge(args, owner_config, "borges", client)

        captured = capsys.readouterr()
        assert "gate 2 (turn=maintainer): PASS" in captured.out
        assert "not presented for merge" not in captured.err

    def test_merge_gate2_refuses_while_turn_still_with_colleague(self, engram_home, monkeypatch, capsys):
        """Companion control: turn == the registered colleague means the PR
        is still in review, not presented for merge -- must stay refused,
        same as it always has (the fix adds an exemption for NAMED
        maintainers, it does not turn Gate 2 into a rubber stamp)."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Lei Shi"}
        _write_config(engram_home, agent_name="borges", primary_user="Lei Shi")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9997", title="colleague-holds merge gate test", turn="ariadne",
                participants="[clio, ariadne]", status="in-progress",
                github="pr/example-org/example-repo/9997", colleague="ariadne",
            )},
        )
        args = argparse.Namespace(pr="PR-9997", dry_run=True, force=True)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not presented for merge" in captured.err

    def test_merge_gate2_refuses_while_turn_back_with_author(self, engram_home, monkeypatch, capsys):
        """Companion control: turn == the PR's own author (explicit
        `author:` field or the #2966 legacy-log fallback) means a
        colleague sent it back with a blocker -- must stay refused, not be
        read as merge-ready just because it isn't the literal colleague
        field either."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Lei Shi"}
        _write_config(engram_home, agent_name="borges", primary_user="Lei Shi")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        raw = _make_raw_baton(
            "PR-9996", title="author-holds merge gate test", turn="clio",
            participants="[clio, ariadne]", status="in-progress",
            github="pr/example-org/example-repo/9996", colleague="ariadne",
        )
        # Faithful legacy shape (no explicit author: field) -- exercises
        # the #2966 turn-log fallback, the same as production batons.
        raw = raw.replace(
            "- 2026-06-26T12:00:00Z initialized",
            "- 2026-06-26T12:00:00Z initialized → clio: project initialized by clio",
        )
        client = _make_client(get_return={"raw": raw})
        args = argparse.Namespace(pr="PR-9996", dry_run=True, force=True)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not presented for merge" in captured.err

    def test_merge_gate2_refuses_empty_turn(self, engram_home, monkeypatch, capsys):
        """Edge case named in Gate 2's own comment: a baton with no `turn`
        key at all must refuse with "(unknown)", not spuriously pass by
        matching the disabled `current_holder=""` sentinel
        `_is_maintainer_flip` is called with. Built by hand rather than
        via `_make_raw_baton(turn="")`: that helper writes an unquoted
        empty value (`turn: `) which corrupts the line-based frontmatter
        parser on the FOLLOWING key (the same fragility `turn_by`/
        `turn_reason` are deliberately quoted to avoid in production
        code) -- a pre-existing test-helper limitation, not something
        this fix should route around. Omitting the key entirely is both
        the realistic shape (production writers never emit an empty
        `turn:` line) and the one that actually reaches Gate 2's
        `if not current_turn` branch cleanly."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Lei Shi"}
        _write_config(engram_home, agent_name="borges", primary_user="Lei Shi")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        raw = (
            "---\n"
            "project: PR-9995\n"
            "title: empty-turn merge gate test\n"
            "status: in-progress\n"
            "turn_since: 2026-06-26T12:00:00Z\n"
            "turn_reason: start\n"
            "participants: [clio, ariadne]\n"
            "github: pr/example-org/example-repo/9995\n"
            "colleague: ariadne\n"
            "---\n\n## Turn log\n\n- 2026-06-26T12:00:00Z initialized\n"
        )
        client = _make_client(get_return={"raw": raw})
        args = argparse.Namespace(pr="PR-9995", dry_run=True, force=True)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "(unknown)" in captured.err

    def test_merge_repo_qualified_ci_red_refuses(self, engram_home, monkeypatch, capsys):
        """#1715 round-2 (reviewer-fairy-caught blocker): cmd_merge is a
        sibling gh consumer of the same `github` anchor field, and its
        CI-green gate is checked against gh's ambient-cwd repo unless the
        anchor's own repo is threaded through -- for an irreversible
        `gh pr merge --squash`, that's the most dangerous instance of the
        whole #1715 bug class. Prove the gate actually fires against the
        ANCHOR's repo, not just that some CI check happened."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("red", "check 'pr-checks' conclusion=FAILURE")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("unknown", "not reached"))

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9300", turn="alex", github="pr/engram-agents/engram-paper/9300",
            )},
        )
        args = argparse.Namespace(pr="PR-9300", dry_run=False, force=False)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        assert ("ci", "9300", "engram-agents/engram-paper") in calls
        captured = capsys.readouterr()
        assert "CI is red" in captured.err

    def test_merge_pr_number_mismatch_refuses(self, engram_home, monkeypatch, capsys):
        """The anchor's own PR number disagreeing with the CLI-resolved
        number is a red flag worth refusing on, not silently picking one --
        this would otherwise be a way for a mismatched/stale anchor to merge
        the wrong PR entirely."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("unknown", "no gh"))

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9301", turn="alex", github="pr/engram-agents/engram-paper/9999",
            )},
        )
        args = argparse.Namespace(pr="PR-9301", dry_run=False, force=False)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        captured = capsys.readouterr()
        assert "mismatch" in captured.err

    def test_merge_calls_gh_with_repo_flag(self, engram_home, monkeypatch):
        """The actual gate-5 `gh pr merge` invocation must carry --repo --
        this is the single most consequential call in the whole #1715 bug
        class (an irreversible squash-merge)."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr, repo=None: ("green", "ok"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("fresh", "ok"))

        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9302", turn="alex", github="pr/engram-agents/engram-paper/9302",
            )},
        )
        args = argparse.Namespace(pr="PR-9302", dry_run=False, force=False)
        _baton.cmd_merge(args, owner_config, "borges", client)

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--repo" in cmd
        assert cmd[cmd.index("--repo") + 1] == "engram-agents/engram-paper"
        assert "9302" in cmd

    def test_merge_bare_anchor_refuses(self, engram_home, monkeypatch, capsys):
        """#1918: a baton with no github anchor at all refuses to merge --
        there is no default repo to guess against, and gate 5 is an
        irreversible `gh pr merge --squash`. Supersedes the old
        test_merge_bare_anchor_resolves_default_repo, which asserted the
        exact behavior this issue fixes."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("green", "ok")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr, repo=None: ("fresh", "ok"))

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-9303", turn="alex")},
        )
        args = argparse.Namespace(pr="PR-9303", dry_run=True, force=False)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        assert calls == []  # refused before any gh call was made
        client.post.assert_not_called()
        captured = capsys.readouterr()
        assert "no repo-qualified github anchor" in captured.err

    def test_merge_bare_anchor_refuses_even_with_force(self, engram_home, monkeypatch, capsys):
        """#1918: --force skips CI/approval quality gates (3-4), never the
        repo-identity gate -- it has never meant 'merge without knowing
        which repo'."""
        owner_config = {"mode": "multi", "agent_name": "borges", "primary_user": "Alex Chen"}
        _write_config(engram_home, agent_name="borges", primary_user="Alex Chen")
        monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        calls = []
        monkeypatch.setattr(
            _baton, "_pr_ci_state",
            lambda pr, repo=None: calls.append(("ci", pr, repo)) or ("green", "ok"),
        )
        monkeypatch.setattr(
            _baton, "_pr_approval_state",
            lambda pr, repo=None: calls.append(("approval", pr, repo)) or ("fresh", "ok"),
        )

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-9304", turn="alex")},
        )
        args = argparse.Namespace(pr="PR-9304", dry_run=False, force=True)
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_merge(args, owner_config, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        assert calls == []
        client.post.assert_not_called()

    def test_missing_config_fails_loud(self, capsys):
        """cmd_claim with empty config (no forum configured) exits EXIT_STATE.

        Replaces the old subprocess test that verified missing config.json +
        no BATON_POOL_SENTINEL caused a loud failure. With the forum-configured
        gate, missing/empty config → no forum.url, no agent_name → EXIT_STATE
        immediately, before any sentinel resolution is attempted.
        """
        client = _make_client()
        args = argparse.Namespace(project_id="pool-no-config")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, {}, "borges", client)
        assert exc_info.value.code == EXIT_STATE
        captured = capsys.readouterr()
        assert "no forum configured" in captured.err

    def test_missing_mode_with_forum_configured_succeeds_past_gate(self, capsys):
        """cmd_claim with mode absent but agent_name set proceeds past the gate.

        Control case for the above: an otherwise-empty config with just
        agent_name set is NOT held at the forum-configured gate — it reaches
        the project-ID validation instead (EXIT_VALIDATION, not EXIT_STATE).
        """
        client = _make_client()
        args = argparse.Namespace(project_id="badid")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_claim(args, {"agent_name": "borges"}, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "invalid project ID" in captured.err


# ===========================================================================
# CATEGORY 12: CI-state dedup helper + mergeStateStatus=CLEAN short-circuit (#1366)
# ===========================================================================

import importlib.util as _ilu
import unittest.mock as _mock


def _load_baton_module():
    spec = _ilu.spec_from_file_location("baton", CLI_PATH)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCiStateDedup:
    """Unit tests for _dedupe_checks_by_latest and the _pr_ci_state fix (#1366).

    Root cause: statusCheckRollup is scoped to the PR's head commit, but it
    carries EVERY run on that commit, not just the latest.  Two runs of one
    check can land on the same SHA, leaving a stale CANCELLED or FAILURE in
    the list beside the current SUCCESS; iterating naively found the stale
    one and falsely blocked baton flip.  Runs on superseded SHAs never enter
    the rollup at all, so there is no cross-SHA case — the fixtures below
    are same-name/different-completedAt, which is that same-SHA case.
    """

    @pytest.fixture
    def baton(self):
        return _load_baton_module()

    def test_dedupe_keeps_latest_by_completed_at(self, baton):
        checks = [
            {"name": "trap-check", "conclusion": "FAILURE", "completedAt": "2026-06-24T10:00:00Z"},
            {"name": "trap-check", "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:00:00Z"},
        ]
        result = baton._dedupe_checks_by_latest(checks)
        assert len(result) == 1
        assert result[0]["conclusion"] == "SUCCESS"

    def test_dedupe_null_completed_at_loses_to_finished(self, baton):
        # In-progress re-run (null completedAt) should not displace a finished run,
        # regardless of which ordering they appear in the list.
        checks_finished_first = [
            {"name": "ci", "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:00:00Z"},
            {"name": "ci", "conclusion": None, "completedAt": None},
        ]
        result = baton._dedupe_checks_by_latest(checks_finished_first)
        assert len(result) == 1
        assert result[0]["conclusion"] == "SUCCESS"

        checks_null_first = [
            {"name": "ci", "conclusion": None, "completedAt": None},
            {"name": "ci", "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:00:00Z"},
        ]
        result = baton._dedupe_checks_by_latest(checks_null_first)
        assert len(result) == 1
        assert result[0]["conclusion"] == "SUCCESS"

    def test_dedupe_preserves_distinct_names(self, baton):
        checks = [
            {"name": "manifest-gate", "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:00:00Z"},
            {"name": "trap-check",    "conclusion": "SUCCESS", "completedAt": "2026-06-24T12:01:00Z"},
        ]
        result = baton._dedupe_checks_by_latest(checks)
        assert len(result) == 2

    def test_pr_ci_state_clean_short_circuits(self, baton):
        # mergeStateStatus=CLEAN should return green immediately, even if the
        # rollup contains a stale FAILURE entry.
        payload = json.dumps({
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [
                {"name": "trap-check", "conclusion": "FAILURE",
                 "completedAt": "2026-06-24T10:00:00Z", "state": "COMPLETED"},
            ],
        })
        mock_result = _mock.MagicMock(returncode=0, stdout=payload, stderr="")
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_ci_state("123", repo="example-org/example-repo")
        assert state == "green"
        assert "CLEAN" in detail

    def test_pr_ci_state_stale_failure_with_latest_success_is_green(self, baton):
        # Two runs of the same check: old FAILURE, new SUCCESS.
        # Without dedup the old code returned red; with dedup it's green.
        payload = json.dumps({
            "mergeStateStatus": "UNKNOWN",
            "statusCheckRollup": [
                {"name": "trap-check", "conclusion": "FAILURE",
                 "completedAt": "2026-06-24T10:00:00Z", "state": "COMPLETED"},
                {"name": "trap-check", "conclusion": "SUCCESS",
                 "completedAt": "2026-06-24T12:00:00Z", "state": "COMPLETED"},
            ],
        })
        mock_result = _mock.MagicMock(returncode=0, stdout=payload, stderr="")
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_ci_state("123", repo="example-org/example-repo")
        assert state == "green", f"expected green, got {state!r}: {detail}"

    def test_pr_ci_state_genuine_failure_is_red(self, baton):
        payload = json.dumps({
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [
                {"name": "trap-check", "conclusion": "FAILURE",
                 "completedAt": "2026-06-24T12:00:00Z", "state": "COMPLETED"},
            ],
        })
        mock_result = _mock.MagicMock(returncode=0, stdout=payload, stderr="")
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_ci_state("123", repo="example-org/example-repo")
        assert state == "red"
        assert "trap-check" in detail


# ===========================================================================
# CATEGORY 14: add-participant subcommand (#1609)
# ===========================================================================

class TestAddParticipant:
    """baton add-participant: arg wiring, error mapping, and the flip-unblock
    end-to-end regression guard (the whole point of the verb)."""

    def test_add_participant_posts_agent_and_participant(self):
        """Happy path: POSTs to /participants with {agent, participant}."""
        client = _make_client(
            post_return={"seq": 5, "added": True},
            get_return={"raw": _make_raw_baton("PR-600")},
        )
        args = argparse.Namespace(project_id="PR-600", participant="alex")
        _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-600/participants"
        assert body["agent"] == "borges"
        assert body["participant"] == "alex"

    def test_add_participant_normalizes_participant_case(self):
        client = _make_client(
            post_return={"seq": 5, "added": True},
            get_return={"raw": _make_raw_baton("PR-601")},
        )
        args = argparse.Namespace(project_id="PR-601", participant="  ALEX ")
        _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["participant"] == "alex"

    def test_add_participant_missing_project_exits_nonzero(self, capsys):
        """add-participant on a non-existent project exits non-zero."""
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", participant="alex")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_add_participant_bad_project_id_exits_nonzero(self, engram_home):
        """add-participant with a malformed --project-id exits non-zero before any API call."""
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, "add-participant", "not a valid id!", "alex",
        )
        assert rc != 0

    def test_add_participant_server_403_maps_to_exit_validation(self, capsys):
        """Server-side NotAParticipant (agent not a current participant) → EXIT_VALIDATION."""
        client = _make_client(get_return={"raw": _make_raw_baton("PR-602")})
        client.post.side_effect = ForumHttpError(403, '{"error": "agent not a participant"}')
        args = argparse.Namespace(project_id="PR-602", participant="alex")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "participant" in captured.err.lower()

    def test_add_participant_server_confirms_already_present_prints_friendly_message(self, capsys):
        """Server signals added=False (idempotent no-op) → friendly already-present message."""
        client = _make_client(
            post_return={"seq": 3, "added": False},
            get_return={"raw": _make_raw_baton("PR-603")},
        )
        args = argparse.Namespace(project_id="PR-603", participant="ariadne")  # already a seeded participant
        _baton.cmd_add_participant(args, MULTI_CONFIG, "borges", client)

        captured = capsys.readouterr()
        assert "already a participant" in captured.out.lower()

    def test_add_participant_no_forum_configured_exits_state(self):
        """add-participant with no forum configured refuses (write command)."""
        client = _make_client()
        args = argparse.Namespace(project_id="PR-604", participant="alex")
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_add_participant(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_STATE

    def test_add_participant_single_mode_with_forum_configured_succeeds(self, capsys):
        """add-participant with mode=single but forum.url set proceeds past the gate."""
        client = _make_client(
            post_return={"seq": 4, "added": True},
            get_return={"raw": _make_raw_baton("PR-604")},
        )
        args = argparse.Namespace(project_id="PR-604", participant="alex")
        _baton.cmd_add_participant(
            args,
            {"mode": "single", "forum": {"url": "http://testforum:5002"}},
            "borges",
            client,
        )
        client.post.assert_called()

    def test_add_participant_then_flip_to_new_participant_succeeds(self, monkeypatch):
        """End-to-end regression guard: the whole point of the verb (#1609).

        Before add-participant, cmd_flip refuses ('not a participant') because
        the client-side check in cmd_flip reads the participants list from the
        baton. After add-participant, a baton read reflecting the addition
        (simulating the real server write) lets cmd_flip succeed.
        """
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_pr_ci_state", lambda pr: ("unknown", "no gh"))
        monkeypatch.setattr(_baton, "_pr_approval_state", lambda pr: ("unknown", "no gh"))

        seed_raw = _make_raw_baton("PR-605", participants="[borges, ariadne]")

        # Step 1: flip to a not-yet-participant fails client-side.
        client = _make_client(get_return={"raw": seed_raw})
        flip_args = argparse.Namespace(
            project_id="PR-605", to="dana", reason="please review", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(flip_args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

        # Step 2: add-participant succeeds (server would persist this).
        client.post.return_value = {"seq": 1, "added": True}
        add_args = argparse.Namespace(project_id="PR-605", participant="dana")
        _baton.cmd_add_participant(add_args, MULTI_CONFIG, "borges", client)

        # Step 3: simulate the server's post-add state (dana now a participant),
        # then flip to dana succeeds.
        client.get.return_value = {
            "raw": _make_raw_baton("PR-605", participants="[borges, ariadne, dana]"),
        }
        client.post.reset_mock()
        client.post.return_value = {"seq": 2}
        _baton.cmd_flip(flip_args, MULTI_CONFIG, "borges", client)
        client.post.assert_called_once()
        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-605/flip"
        assert body["to_agent"] == "dana"


# ===========================================================================
# CATEGORY 15: set-colleague subcommand (#2764)
# ===========================================================================

class TestSetColleague:
    """baton set-colleague: arg wiring, error mapping, and the init-time
    --colleague payload fix (colleague is now actually sent to the server,
    not just used to shape participants + a client-side print)."""

    def test_set_colleague_posts_agent_and_colleague(self):
        client = _make_client(
            post_return={"seq": 5, "participant_added": True},
            get_return={"raw": _make_raw_baton("PR-700")},
        )
        args = argparse.Namespace(project_id="PR-700", colleague="alex")
        _baton.cmd_set_colleague(args, MULTI_CONFIG, "borges", client)

        url, body = client.post.call_args[0]
        assert url == "/api/projects/PR-700/colleague"
        assert body["agent"] == "borges"
        assert body["colleague"] == "alex"

    def test_set_colleague_normalizes_case(self):
        client = _make_client(
            post_return={"seq": 5, "participant_added": True},
            get_return={"raw": _make_raw_baton("PR-701")},
        )
        args = argparse.Namespace(project_id="PR-701", colleague="  ALEX ")
        _baton.cmd_set_colleague(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["colleague"] == "alex"

    def test_set_colleague_missing_project_exits_nonzero(self, capsys):
        client = _make_client()
        client.get.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-NONE", colleague="alex")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_set_colleague(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower() or "PR-NONE" in captured.err

    def test_set_colleague_bad_project_id_exits_nonzero(self, engram_home):
        _write_config(engram_home, "borges")
        rc, stdout, stderr = _run_baton(
            engram_home, "set-colleague", "not a valid id!", "alex",
        )
        assert rc != 0

    def test_set_colleague_empty_colleague_exits_nonzero(self, capsys):
        client = _make_client(get_return={"raw": _make_raw_baton("PR-702")})
        args = argparse.Namespace(project_id="PR-702", colleague="   ")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_set_colleague(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_set_colleague_server_403_maps_to_exit_validation(self, capsys):
        """Server-side auth guard (agent not a participant/holder) → EXIT_VALIDATION."""
        client = _make_client(get_return={"raw": _make_raw_baton("PR-703")})
        client.post.side_effect = ForumHttpError(403, '{"error": "not authorized"}')
        args = argparse.Namespace(project_id="PR-703", colleague="alex")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_set_colleague(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        captured = capsys.readouterr()
        assert "participant" in captured.err.lower() or "holder" in captured.err.lower()

    def test_set_colleague_404_maps_to_exit_state(self, capsys):
        client = _make_client(get_return={"raw": _make_raw_baton("PR-704")})
        client.post.side_effect = ForumHttpError(404, '{"error": "not found"}')
        args = argparse.Namespace(project_id="PR-704", colleague="alex")

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_set_colleague(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_STATE

    def test_set_colleague_prints_participant_added_suffix(self, capsys):
        client = _make_client(
            post_return={"seq": 5, "participant_added": True},
            get_return={"raw": _make_raw_baton("PR-705")},
        )
        args = argparse.Namespace(project_id="PR-705", colleague="alex")
        _baton.cmd_set_colleague(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "added as participant" in captured.out.lower()

    def test_set_colleague_no_suffix_when_already_participant(self, capsys):
        client = _make_client(
            post_return={"seq": 5, "participant_added": False},
            get_return={"raw": _make_raw_baton("PR-706", participants="[borges, alex]")},
        )
        args = argparse.Namespace(project_id="PR-706", colleague="alex")
        _baton.cmd_set_colleague(args, MULTI_CONFIG, "borges", client)
        captured = capsys.readouterr()
        assert "added as participant" not in captured.out.lower()

    def test_set_colleague_no_forum_configured_exits_state(self):
        """set-colleague with no forum configured refuses (write command)."""
        client = _make_client()
        args = argparse.Namespace(project_id="PR-707", colleague="alex")
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_set_colleague(args, {"mode": "single"}, "borges", client)
        assert exc_info.value.code == EXIT_STATE

    def test_set_colleague_single_mode_with_agent_name_succeeds(self, capsys):
        """set-colleague with mode=single but agent_name set proceeds past the gate."""
        client = _make_client(
            post_return={"seq": 6, "participant_added": True},
            get_return={"raw": _make_raw_baton("PR-707")},
        )
        args = argparse.Namespace(project_id="PR-707", colleague="alex")
        _baton.cmd_set_colleague(
            args, {"mode": "single", "agent_name": "borges"}, "borges", client,
        )
        client.post.assert_called()

    def test_init_sends_colleague_to_server(self, monkeypatch):
        """#2764: --colleague at init is now actually POSTed, not dropped
        after being used client-side to shape participants + a print."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        client = _make_client(post_return={"seq": 1})
        args = argparse.Namespace(
            project_id="PR-708", title="t", participants="borges",
            turn=None, status="in-progress", github=None,
            repo="engram-agents/engram-paper",
            colleague="ariadne",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["colleague"] == "ariadne"

    def test_init_without_colleague_sends_none(self, monkeypatch):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        client = _make_client(post_return={"seq": 1})
        args = argparse.Namespace(
            project_id="PR-709", title="t", participants="borges",
            turn=None, status="in-progress", github=None,
            repo="engram-agents/engram-paper",
            colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body["colleague"] is None


# ---------------------------------------------------------------------------
# #1715: repo-qualified PR anchors (fixes PR-baton IDs colliding across repos)
# ---------------------------------------------------------------------------

class TestParseGithubAnchor:
    def test_bare_pr_number(self):
        assert _baton._parse_github_anchor("pr/22") == ("pr", None, "22")

    def test_repo_qualified_pr(self):
        assert _baton._parse_github_anchor("pr/engram-agents/engram-paper/22") == (
            "pr", "engram-agents/engram-paper", "22",
        )

    def test_project_form(self):
        assert _baton._parse_github_anchor("project/4") == ("project", None, "4")

    def test_empty_string_returns_none(self):
        assert _baton._parse_github_anchor("") is None

    def test_garbage_returns_none(self):
        assert _baton._parse_github_anchor("not-an-anchor") is None


class TestInitRepoQualification:
    def test_init_refuses_bare_anchor_auto_derived_from_pr_project_id(self, monkeypatch, capsys):
        """A PR-<N> project_id with no --github still auto-derives pr/<N>
        internally (#1715 -- every PR-baton should carry SOME anchor from
        creation, not rely on downstream code re-deriving the number from
        project_id ad hoc), but that bare, unqualified derivation is no
        longer sufficient to create the baton on its own (#1924, forum #411,
        2026-09-13 ruling): without --repo (or an already-qualified
        --github), init now REFUSES rather than minting an anchor that
        gc/flip/merge would refuse later anyway."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-9001", title="auto anchor", participants="borges,ariadne",
            status="planning", turn=None, github=None, repo=None, colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        err = capsys.readouterr().err
        assert "PR-9001" in err
        assert "--repo <owner>/<repo>" in err
        assert "--github pr/<owner>/<repo>/9001" in err

    def test_init_never_infers_repo_from_cwd_or_git_remote(self, monkeypatch, capsys):
        """#1924's ruling (forum #411, 2026-09-13) is explicit: NO cwd or
        git-remote-based repo inference of any kind, anywhere in cmd_init's
        anchor-handling path -- an earlier draft of the ruling proposed
        resolving the repo from the checkout's git `origin` remote when
        unambiguous, and that was proposed, examined, and explicitly
        withdrawn (see the #1918/#1924 comment at the anchor-handling call
        site). A cwd-inferred anchor would be confidently wrong and fully
        qualified rather than merely unresolvable, which is worse than the
        loud refusal this function raises without --repo.

        This is a control, not a vigilance check: fail the test hard the
        moment cmd_init ever shells out to `subprocess` (a git-remote lookup
        being the obvious shape such an inference would take) while minting
        a bare PR-<N> baton with no --repo."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        def _fail_if_called(*args, **kwargs):
            raise AssertionError(
                f"cmd_init must never invoke subprocess (got args={args!r}, "
                f"kwargs={kwargs!r}) -- there is no cwd/git-remote repo "
                "inference anywhere in this function, deliberately (#1918/#1924)."
            )

        monkeypatch.setattr(_baton.subprocess, "run", _fail_if_called)

        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-4242", title="no cwd inference",
            participants="borges,ariadne", status="planning",
            turn=None, github=None, repo=None, colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_init_with_repo_qualifies_anchor(self, monkeypatch):
        """--repo combines with the (auto-derived or explicit) pr/<N> anchor
        into the repo-qualified form."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-22", title="cross-repo PR", participants="luria,borges",
            status="in-progress", turn=None, github=None,
            repo="engram-agents/engram-paper", colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        _, body = client.post.call_args[0]
        assert body.get("github") == "pr/engram-agents/engram-paper/22"

    def test_init_with_repo_but_no_pr_anchor_rejected(self, monkeypatch):
        """--repo without a PR-shaped anchor (e.g. a project_id that isn't
        PR-<N> and no explicit --github) is a validation error, not a silent
        no-op -- --repo only makes sense qualifying a PR anchor."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="pool-something", title="not a PR baton",
            participants="borges,ariadne", status="planning", turn=None,
            github=None, repo="engram-agents/engram-paper", colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_init_rejects_malformed_repo_value(self, monkeypatch):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-22", title="bad repo", participants="borges,ariadne",
            status="planning", turn=None, github=None,
            repo="not-a-valid-repo-format", colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_github_anchor_regex_accepts_repo_qualified_form(self):
        assert _baton._GITHUB_ANCHOR_RE.match("pr/engram-agents/engram-paper/22")
        assert _baton._GITHUB_ANCHOR_RE.match("pr/22")
        assert _baton._GITHUB_ANCHOR_RE.match("project/4")
        assert not _baton._GITHUB_ANCHOR_RE.match("pr/only-one-segment/22")

    def test_init_conflicting_github_and_repo_rejected(self, monkeypatch):
        """Reviewer-fairy-flagged operator-error case: an already
        repo-qualified --github combined with a DIFFERENT --repo must not
        silently overwrite the anchor's real repo -- reject and ask the
        caller to reconcile, rather than guess which one is intended."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-22", title="conflicting repo",
            participants="borges,ariadne", status="planning", turn=None,
            github="pr/engram-agents/engram-paper/22",
            repo="engram-agents/some-other-repo", colleague=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()

    def test_init_agreeing_github_and_repo_accepted(self, monkeypatch):
        """The non-conflicting case (--repo restates the SAME repo the
        --github anchor already carries) must still succeed."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-22", title="agreeing repo",
            participants="borges,ariadne", status="planning", turn=None,
            github="pr/engram-agents/engram-paper/22",
            repo="engram-agents/engram-paper", colleague=None,
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)
        _, body = client.post.call_args[0]
        assert body.get("github") == "pr/engram-agents/engram-paper/22"


class TestInitAuthorIsInvoker:
    """#3042: `baton init --turn <someone-else>` recorded THAT agent as the
    baton's `author:`, not the agent who ran the command.

    Live consequence, 2026-09-19: a baton created with `--turn kepler` by a
    different initor recorded `author: kepler`. Kepler then could not hand
    it back -- the flip to the initor matched none of `_is_maintainer_flip`'s
    three exemptions (not a self-flip, not the flip to the registered
    colleague, and not the flip to the recorded "author", because the author
    on file WAS kepler himself), so it was classified as presenting-for-merge
    and demanded a colleague approval that could not exist. `--force` did not
    help either (the unqualified-anchor refusal is explicitly not
    force-skippable), and there is no verb that repairs `author:` -- the
    baton had to be cancelled and re-created.

    The fix passes the INVOKER (`agent_name`), which the same posted dict
    already carries one line above as `"agent"`. Deliberately NO
    `or initial_turn` fallback: an unknown invoker must leave the field
    EMPTY so `_derive_legacy_author`'s documented PROXY fires on read,
    rather than re-asserting the turn-holder as a confident (and wrong)
    author. That distinction is pinned by
    test_init_with_empty_invoker_leaves_author_empty below.

    Sentinel pinned via monkeypatch in every test here, for the reason
    documented on TestFlipMaintainerRoleGate: `_pool_sentinel()` resolves
    from the INVOKING SEAT's own config.json, so an unpinned test is
    seat-dependent.
    """

    def test_init_with_turn_naming_another_agent_records_invoker_as_author(
        self, monkeypatch,
    ):
        """The field-level claim: init as "borges" with `--turn ariadne`
        posts `author: borges` (the invoker) and `turn: ariadne` (the turn
        holder). On the pre-fix code the posted author was `ariadne` --
        the turn holder -- which is the whole defect."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-9400", title="turn names someone else",
            participants="borges,ariadne", status="in-progress",
            turn="ariadne", github=None,
            repo="engram-agents/engram-paper", colleague="ariadne",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body.get("turn") == "ariadne"
        assert body.get("author") == "borges", (
            "init must record the INVOKER as author, not the initial turn "
            f"holder -- got {body.get('author')!r} (#3042)"
        )

    def test_turn_holder_can_flip_the_baton_back_to_the_initor_ungated(
        self, monkeypatch, capsys,
    ):
        """The trap itself, end to end -- the reason a field-level
        assertion alone is not enough.

        Chain: `cmd_init` as "borges" with `--turn ariadne` -> the posted
        body is rendered into the baton the store would persist
        (`_raw_from_init_post`, which mirrors the server's own
        optional-field rule) -> "ariadne" flips it BACK to "borges"
        through `cmd_flip`, reading exactly that baton.

        That flip must NOT be classified as a merge-presentation flip: it
        is a return to the PR's real author. Both gh-querying helpers are
        wired to FAIL LOUDLY if called, so the test measures "the gate
        never ran" rather than merely "the exit code was 0" -- on the
        pre-fix code, `author:` reads "ariadne", the author exemption
        cannot fire for a flip TO "borges", and `_pr_ci_state` is reached.

        The registered colleague is "ariadne" (the agent DOING the flip),
        deliberately: if the colleague were "borges", the flip-to-colleague
        exemption would fire and this test would pass with or without the
        fix, measuring nothing.
        """
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        def _no_ci(pr_num, repo=None):
            raise AssertionError(
                "_pr_ci_state must not be called -- flipping a baton back to "
                "the agent who INITIALIZED it is a return-to-author, not a "
                "presentation for merge (#3042)"
            )

        def _no_approval(pr_num, repo=None):
            raise AssertionError(
                "_pr_approval_state must not be called -- see _no_ci (#3042)"
            )

        monkeypatch.setattr(_baton, "_pr_ci_state", _no_ci)
        monkeypatch.setattr(_baton, "_pr_approval_state", _no_approval)

        init_client = _make_client({"seq": 1})
        init_args = argparse.Namespace(
            project_id="PR-9401", title="borges inits, ariadne holds",
            participants="borges,ariadne", status="in-progress",
            turn="ariadne", github=None,
            repo="engram-agents/engram-paper", colleague="ariadne",
        )
        _baton.cmd_init(init_args, MULTI_CONFIG, "borges", init_client)
        _, init_body = init_client.post.call_args[0]
        capsys.readouterr()  # discard init's own stdout/stderr

        flip_client = _make_client(
            post_return={"seq": 2},
            get_return={"raw": _raw_from_init_post(init_body)},
        )
        flip_args = argparse.Namespace(
            project_id="PR-9401", to="borges",
            reason="one blocker, back to you", force=False,
        )
        _baton.cmd_flip(flip_args, MULTI_CONFIG, "ariadne", flip_client)

        flip_client.post.assert_called_once()
        assert capsys.readouterr().err.strip() == ""

    def test_turn_holder_flip_back_is_not_blocked_by_the_unforceable_gate(
        self, monkeypatch, capsys,
    ):
        """The same end-to-end chain in the shape that actually stranded
        the live baton: an UNQUALIFIED `pr/<N>` anchor (auto-derived from
        the `PR-<N>` project id when no `--repo` is given -- note this init
        path predates the refusal `TestInitRepoQualification` covers, so
        the live store holds such batons).

        A misclassified return-to-author hits the unqualified-anchor
        refusal, which is documented as NOT skippable with `--force` -- so
        the holder could not hand the baton back at all, by any invocation.
        With the author recorded correctly the flip is exempt before that
        branch is ever evaluated, and succeeds.

        `--force` is passed here precisely because it is the escape hatch
        that did NOT work; the assertion is that no escape hatch is needed.
        """
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])

        def _no_ci(pr_num, repo=None):
            raise AssertionError("_pr_ci_state must not be called (#3042)")

        monkeypatch.setattr(_baton, "_pr_ci_state", _no_ci)

        init_client = _make_client({"seq": 1})
        init_args = argparse.Namespace(
            project_id="PR-9402", title="bare anchor, stranded",
            participants="borges,ariadne", status="in-progress",
            turn="ariadne", github=None,
            repo="engram-agents/engram-paper", colleague="ariadne",
        )
        _baton.cmd_init(init_args, MULTI_CONFIG, "borges", init_client)
        _, init_body = init_client.post.call_args[0]
        capsys.readouterr()  # discard init's own stdout/stderr

        # The ANCHOR is downgraded to its bare form after the fact, because
        # today's cmd_init cannot mint one: it REFUSES a bare pr/<N> anchor,
        # auto-derived or explicit (#1924). The live store nonetheless holds
        # batons created before that refusal shipped, and they are exactly
        # the ones this bug stranded. Only the anchor is overridden --
        # `author` is still whatever cmd_init actually posted, so this test
        # measures the fix rather than a value hand-written by the test.
        raw = _raw_from_init_post(dict(init_body, github="pr/9402"))
        client = _make_client(post_return={"seq": 2}, get_return={"raw": raw})
        args = argparse.Namespace(
            project_id="PR-9402", to="borges",
            reason="one blocker, back to you", force=True,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_called_once()
        err = capsys.readouterr().err
        assert "has no repo-qualified github anchor" not in err

    def test_init_without_turn_still_records_invoker_as_author(self, monkeypatch):
        """Regression control for the normal case, which must be unchanged:
        with `--turn` omitted the initial turn DEFAULTS to the invoking
        agent, so author == invoker == turn. This test passes both before
        and after the fix, by design -- it is what proves the fix did not
        move the common path."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-9403", title="ordinary self-authored baton",
            participants="borges,ariadne", status="in-progress",
            turn=None, github=None,
            repo="engram-agents/engram-paper", colleague="ariadne",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "borges", client)

        _, body = client.post.call_args[0]
        assert body.get("turn") == "borges"
        assert body.get("author") == "borges"

    def test_init_with_empty_invoker_leaves_author_empty(self, monkeypatch):
        """No `or initial_turn` fallback: when the invoker is unknown
        (`agent_name` empty) the posted `author` must be EMPTY, not the
        turn holder.

        Empty is the value that makes the server omit the `author:` field
        entirely (`(data.get("author") or "").strip() or None`, then
        `if author:` in coordination/projects.py), which is what lets
        `_derive_legacy_author` supply its documented PROXY from the turn
        log on read. Substituting the turn holder here would write a
        confident wrong author in place of a labelled proxy -- so the
        proxy is asserted too, to show what the empty field buys."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        monkeypatch.setattr(_baton, "_POOL_SENTINEL_CACHE", [])
        client = _make_client({"seq": 1})
        args = argparse.Namespace(
            project_id="PR-9404", title="unknown invoker",
            participants="borges,ariadne", status="in-progress",
            turn="ariadne", github=None,
            repo="engram-agents/engram-paper", colleague="ariadne",
        )
        _baton.cmd_init(args, MULTI_CONFIG, "", client)

        _, body = client.post.call_args[0]
        assert body.get("author") == "", (
            "an unknown invoker must leave author EMPTY so the legacy proxy "
            f"fires -- got {body.get('author')!r} (#3042)"
        )

        # What the empty field buys: no author: line, and the proxy
        # recovered from the turn log instead.
        fields, log_body = _baton._parse_frontmatter(_raw_from_init_post(body))
        assert "author" not in fields
        assert _baton._derive_legacy_author(log_body) == "ariadne"


class TestGcRepoAware:
    """#1715: cmd_gc must never guess a repo -- it reads the stored `github`
    anchor per project (never re-derives the PR number from project_id via
    regex, which has zero repo awareness) and SKIPS any PR-baton lacking a
    parseable PR anchor rather than defaulting silently.

    Sibling coverage: src/forum/tests/test_baton_thin_client.py::TestCmdGc
    tests the same cmd_gc surface from the thin-client-migration angle
    (POST body shape, error-code mapping). #1918 review round caught these
    two files drifting independently on the same anchor-contract change --
    if you touch cmd_gc's anchor handling, check both."""

    def test_gc_uses_repo_from_github_field_not_ambient_cwd(self, monkeypatch):
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-22", "github": "pr/engram-agents/engram-paper/22"},
        ]})
        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "OPEN"
            return result

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)
        args = argparse.Namespace(dry_run=True, limit=30)
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--repo" in cmd
        assert cmd[cmd.index("--repo") + 1] == "engram-agents/engram-paper"
        assert "22" in cmd

    def test_gc_skips_unqualified_anchor_never_guesses(self, monkeypatch, capsys):
        """#1918: a bare pr/<N> anchor (no repo) is skipped-and-counted like
        a missing anchor -- there is no default repo to pass to --repo.
        Supersedes the old test_gc_defaults_unqualified_anchor_to_default_repo_explicitly,
        which asserted the exact silent-guess behavior this issue fixes."""
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-1711", "github": "pr/1711"},
        ]})
        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "MERGED"
            return result

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert captured_cmds == []  # no gh call at all -- never guessed
        captured = capsys.readouterr()
        assert "skipped 1 PR-baton" in captured.err
        assert "unqualified" in captured.err

    def test_gc_skips_pr_baton_with_no_github_anchor(self, monkeypatch, capsys):
        """A PR-<N> baton with no (or unparseable) github field is skipped,
        not guessed at -- the pre-#1715 behavior derived the PR number from
        project_id alone and queried gh with zero repo context."""
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-9999", "github": None},
        ]})
        called = []
        monkeypatch.setattr(_baton.subprocess, "run", lambda *a, **kw: called.append(a) or MagicMock(returncode=0, stdout="OPEN"))
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert called == []  # no gh call at all for the ungauged baton
        captured = capsys.readouterr()
        assert "skipped 1 PR-baton" in captured.err

    def test_gc_two_different_repos_same_number_both_handled_correctly(self, monkeypatch):
        """The exact #1715 collision shape: two PR-batons for the SAME
        number (22) in DIFFERENT (both repo-qualified, per #1918 -- a bare
        anchor is never queried at all now) repos must each query their OWN
        repo and never share a result (reviewer-fairy caught: the original
        version of this test constructed only one project, so it never
        actually exercised the collision -- it was equivalent to the
        single-project test just above it)."""
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-9200", "github": "pr/engram-agents/engram-paper/22"},
            {"project_id": "PR-9201", "github": "pr/example-org/example-repo/22"},
        ]})
        captured_cmds = []

        def _fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            repo = cmd[cmd.index("--repo") + 1]
            result = MagicMock()
            result.returncode = 0
            # Different verdicts per repo -- proves the two #22s are queried
            # (and therefore could resolve) independently, not conflated.
            result.stdout = "MERGED" if repo == "engram-agents/engram-paper" else "OPEN"
            return result

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert len(captured_cmds) == 2
        by_repo = {cmd[cmd.index("--repo") + 1]: cmd for cmd in captured_cmds}
        assert "engram-agents/engram-paper" in by_repo
        assert "example-org/example-repo" in by_repo
        # Both invocations target PR number 22, and each carries its OWN repo
        # -- confirms they are not the same call, and neither's repo bleeds
        # into the other's.
        assert "22" in by_repo["engram-agents/engram-paper"]
        assert "22" in by_repo["example-org/example-repo"]


class TestGcFailedVsSkipped:
    """#1918 defect 2: a gh query failure must be counted as `failed`, not
    `skipped` -- the old code bucketed both together, so a run's own
    summary line could read `failed 0` immediately after printing dozens of
    'gh query failed' lines, making a completely inert sweep look clean."""

    def test_gh_query_failure_counted_as_failed_not_skipped(self, monkeypatch, capsys):
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-31", "github": "pr/example-org/example-repo/31"},
        ]})
        monkeypatch.setattr(
            _baton.subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=1, stdout="", stderr="not found"),
        )
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert exc_info.value.code == EXIT_IO
        captured = capsys.readouterr()
        assert "would close 0, skipped 0, failed 1" in captured.out

    def test_pr_still_open_counted_as_skipped_not_failed(self, monkeypatch, capsys):
        """The companion case: a SUCCESSFUL gh query whose PR is simply
        still OPEN (no action needed) stays a `skipped`, not a `failed` --
        this test guards against over-correcting #1918 into flagging every
        non-close as an error."""
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-32", "github": "pr/example-org/example-repo/32"},
        ]})
        monkeypatch.setattr(
            _baton.subprocess, "run",
            lambda *a, **kw: MagicMock(returncode=0, stdout="OPEN", stderr=""),
        )
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)

        # No SystemExit -- a clean sweep with nothing to close is not a failure.
        _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        captured = capsys.readouterr()
        assert "would close 0, skipped 1, failed 0" in captured.out

    def test_mixed_run_exits_nonzero_and_reports_accurate_counts(self, monkeypatch, capsys):
        client = _make_client(get_return={"projects": [
            {"project_id": "PR-41", "github": "pr/example-org/example-repo/41"},  # fails
            {"project_id": "PR-42", "github": "pr/example-org/example-repo/42"},  # open, skipped
            {"project_id": "PR-43", "github": "pr/example-org/example-repo/43"},  # merged, closed
        ]})

        def _fake_run(cmd, **kwargs):
            pr_num = cmd[3]  # ["gh", "pr", "view", pr_num, "--repo", ...]
            if pr_num == "41":
                return MagicMock(returncode=1, stdout="", stderr="gh: not found")
            if pr_num == "42":
                return MagicMock(returncode=0, stdout="OPEN", stderr="")
            return MagicMock(returncode=0, stdout="MERGED", stderr="")

        monkeypatch.setattr(_baton.subprocess, "run", _fake_run)
        monkeypatch.setattr(_baton.shutil, "which", lambda _: "/usr/bin/gh")
        args = argparse.Namespace(dry_run=True, limit=30)

        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_gc(args, MULTI_CONFIG, "borges", client)

        assert exc_info.value.code == EXIT_IO
        captured = capsys.readouterr()
        assert "would close 1, skipped 1, failed 1" in captured.out


class TestFlipRepoQualifiedGuard:
    """#1715 reviewer-fairy-caught blocker: cmd_flip's CI-green/approval-
    freshness/colleague-approval guard used a bare `^pr/(\\d+)$` regex that
    silently failed to match a repo-qualified anchor -- meaning the ENTIRE
    guard block was skipped, with no warning, for exactly the cross-repo
    PR-batons this feature exists to support. These tests prove the guard
    fires (and the resolved repo is threaded to the gh-querying helpers)
    for a repo-qualified anchor, not just the bare form."""

    def test_flip_repo_qualified_ci_red_refuses(self, monkeypatch, capsys):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("red", "check 'pr-checks' conclusion=FAILURE")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("unknown", "not reached")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9100", github="pr/engram-agents/engram-paper/9100",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9100", to="casey", reason="ready", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        # The guard actually ran, and ran against the QUALIFIED repo, not
        # DEFAULT_GITHUB_REPO or an unresolved/None value.
        assert ("ci", "9100", "engram-agents/engram-paper") in calls
        captured = capsys.readouterr()
        assert "CI is red" in captured.err

    def test_flip_repo_qualified_ci_green_proceeds(self, monkeypatch):
        """Sanity companion: a genuinely green, fresh, approved cross-repo
        PR-baton still flips successfully -- the fix doesn't just make the
        guard fire, it must resolve correctly on the happy path too."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("green", "mergeStateStatus=CLEAN")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("fresh", "approved at current tip")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9101", github="pr/engram-agents/engram-paper/9101",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9101", to="casey", reason="ready", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        client.post.assert_called_once()
        assert ("ci", "9101", "engram-agents/engram-paper") in calls
        assert ("approval", "9101", "engram-agents/engram-paper") in calls

    def test_flip_bare_anchor_refuses_never_guesses(self, monkeypatch, capsys):
        """#1918/#2511: an unqualified bare pr/<N> anchor REFUSES the flip
        entirely (no gh call, no guessed repo, no CI/approval/colleague
        verification, and the flip itself never happens) rather than
        resolving against a default OR silently proceeding unverified.
        Supersedes the old test_flip_bare_anchor_skips_guard_never_guesses,
        which asserted the exact warn-and-proceed behavior #2511 fixes --
        that assertion in turn superseded test_flip_bare_anchor_still_
        resolves_default_repo, which asserted the original silent-guess
        behavior #1918 fixed. The 'never guesses' half of the name still
        holds; only the disposition of the ungaugeable flip changed."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("green", "mergeStateStatus=CLEAN")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("fresh", "approved at current tip")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton("PR-9102", github="pr/9102")},
        )
        args = argparse.Namespace(
            project_id="PR-9102", to="casey", reason="ready", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()  # the flip never happens
        assert calls == []  # no gh call at all -- never guessed a repo
        captured = capsys.readouterr()
        assert "has no repo-qualified github anchor" in captured.err

    def test_flip_bare_anchor_refuses_even_with_force(self, monkeypatch, capsys):
        """#2511: --force does not bypass the unqualified-anchor refusal --
        force's contract is 'skip CI/approval quality gates', never 'proceed
        without knowing which repo'. Companion to the no-force case above."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        client = _make_client(
            get_return={"raw": _make_raw_baton("PR-9102", github="pr/9102")},
        )
        args = argparse.Namespace(
            project_id="PR-9102", to="casey", reason="ready", force=True,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "borges", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        captured = capsys.readouterr()
        assert "This is not skippable with --force" in captured.err


# ===========================================================================
# CATEGORY 16: cmd_flip's maintainer-role gate broadening (#2912)
# ===========================================================================


class TestFlipMaintainerRoleGate:
    """#2912: cmd_flip's entire CI-green/approval-freshness/colleague-
    approval-exists gate block was guarded by a bare `_is_pool_sentinel(to_agent)`
    check -- true only when to_agent matches config.json's primary_user (the
    literal pool sentinel, e.g. "lei shi"). But this repo's actual, documented
    convention (CLAUDE.md, "Every PR carries a baton") is to flip a PR-baton
    to a NAMED maintainer agent ("baton flip PR-N borges", "baton flip PR-N
    ariadne") -- never to the literal sentinel. So on every flip that matches
    real practice, the ENTIRE gate -- CI check, approval-freshness check, and
    the colleague-approval-EXISTS check -- silently never ran. A fully
    CI-red, zero-approval PR could be flipped to a named maintainer with
    total silence.

    _is_maintainer_flip broadens the trigger: fires on the literal sentinel
    OR any other agent, PROVIDED it is not a self-flip and not the flip to
    the registered colleague (that flip is "please review", not "ready to
    merge" -- the colleague hasn't reviewed yet, so firing CI/approval
    checks on it would be premature).

    All tests here pin the sentinel EXPLICITLY via
    `monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")` --
    matching this file's existing convention in TestFlipRepoQualifiedGuard
    above. This matters because _pool_sentinel() resolves from the INVOKING
    SEAT's own $ENGRAM_HOME/config.json primary_user, not a fleet-wide
    constant -- two different seats running the identical command can see
    _is_pool_sentinel(to_agent) resolve differently depending on whose
    machine ran it. Relying on the ambient real config.json on whatever
    machine runs these tests would make them non-deterministic across
    seats/installs; pinning removes that dependency entirely. Tests below
    cover BOTH shapes of "presenting for merge" under the SAME pinned
    sentinel: a flip to a named maintainer agent that is NOT the sentinel
    (the shape the bug actually missed), and a flip to the literal sentinel
    value itself (the shape that already worked, kept as a side-by-side
    control under the same fixture)."""

    def test_flip_to_named_maintainer_ci_red_refuses(self, monkeypatch, capsys):
        """Sol's exact #2912 repro: fully repo-qualified anchor, CI mocked
        red, approval mocked no_approval, flip to a NAMED maintainer
        ("borges") that is NOT the pool sentinel and NOT the registered
        colleague -- must REFUSE, not silently succeed. Asserting on the CI
        mock's call count (not just the exit code) is what makes this a real
        regression test for "the gate never even ran": on the old bare
        _is_pool_sentinel(to_agent) predicate, this exact flip would never
        even call _pr_ci_state, let alone see its red verdict.

        Only _pr_ci_state is asserted called (not _pr_approval_state): the
        CI-red branch exits before the approval check is ever reached --
        same short-circuit shape as the pre-existing
        test_flip_repo_qualified_ci_red_refuses above. The approval fake is
        still wired up (matching Sol's literal repro setup) so a future
        change to the short-circuit order doesn't silently stop testing it.
        """
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("red", "check 'pr-checks' conclusion=FAILURE")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("no_approval", "no APPROVED review")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9200", github="pr/engram-agents/engram-paper/9200",
                turn="ariadne", participants="[ariadne, borges, sol]",
                colleague="sol",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9200", to="borges", reason="ready", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        # The guard actually RAN -- this is exactly what the old bare
        # _is_pool_sentinel(to_agent) predicate never did for a
        # named-maintainer flip.
        assert ("ci", "9200", "engram-agents/engram-paper") in calls
        assert ("approval", "9200", "engram-agents/engram-paper") not in calls
        captured = capsys.readouterr()
        assert captured.err.strip() != ""
        assert "CI is red" in captured.err

    def test_flip_to_literal_sentinel_ci_red_refuses(self, monkeypatch, capsys):
        """Companion control to the named-maintainer case above: a flip to
        the LITERAL pinned sentinel value itself must ALSO refuse under
        CI-red -- this is the pre-existing, already-correct behavior (same
        shape as TestFlipRepoQualifiedGuard.test_flip_repo_qualified_ci_red_refuses),
        reproduced here under the identical pinned-sentinel fixture as the
        test above it, so both shapes of "presenting for merge" are verified
        side by side under one sentinel value."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("red", "check 'pr-checks' conclusion=FAILURE")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("no_approval", "no APPROVED review")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9201", github="pr/engram-agents/engram-paper/9201",
                turn="ariadne", participants="[ariadne, borges, sol, casey]",
                colleague="sol",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9201", to="casey", reason="ready", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        assert ("ci", "9201", "engram-agents/engram-paper") in calls
        captured = capsys.readouterr()
        assert "CI is red" in captured.err

    def test_flip_to_registered_colleague_does_not_trigger_gate(self, monkeypatch, capsys):
        """Control: flip to the registered COLLEAGUE ("sol", the baton's
        colleague: field) is the review-stage "please review" signal, not a
        merge-readiness claim -- this must NOT trigger CI/approval checks,
        even with CI red and no approval mocked. A regression here would
        break the exact pattern this team uses on every PR, all day
        ("baton flip PR-N <colleague>" as the first review-request flip)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        def _fake_ci_state(pr_num, repo=None):
            raise AssertionError("_pr_ci_state must not be called for a flip to the registered colleague")

        def _fake_approval_state(pr_num, repo=None):
            raise AssertionError("_pr_approval_state must not be called for a flip to the registered colleague")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9202", github="pr/engram-agents/engram-paper/9202",
                turn="ariadne", participants="[ariadne, borges, sol]",
                colleague="sol",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9202", to="sol", reason="please review", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_called_once()

    def test_self_flip_does_not_trigger_gate(self, monkeypatch, capsys):
        """Control: flipping back to yourself (e.g. after a colleague
        requests changes) is not a merge-readiness claim and must NOT
        trigger CI/approval checks, even with CI red and no approval
        mocked."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        def _fake_ci_state(pr_num, repo=None):
            raise AssertionError("_pr_ci_state must not be called for a self-flip")

        def _fake_approval_state(pr_num, repo=None):
            raise AssertionError("_pr_approval_state must not be called for a self-flip")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9203", github="pr/engram-agents/engram-paper/9203",
                turn="ariadne", participants="[ariadne, borges, sol]",
                colleague="sol",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9203", to="ariadne", reason="more work needed", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_called_once()

    def test_flip_to_named_maintainer_ci_green_approved_succeeds(self, monkeypatch):
        """Positive case: a flip to a named maintainer ("borges") with CI
        green and a real (fresh) approval SUCCEEDS -- same shape as the
        pre-existing sentinel-flip success path
        (test_flip_repo_qualified_ci_green_proceeds), just with a named
        agent as to_agent instead of the literal sentinel."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("green", "mergeStateStatus=CLEAN")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("fresh", "approved at current tip")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            post_return={"seq": 1},
            get_return={"raw": _make_raw_baton(
                "PR-9204", github="pr/engram-agents/engram-paper/9204",
                turn="ariadne", participants="[ariadne, borges, sol]",
                colleague="sol",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9204", to="borges", reason="ready", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_called_once()
        assert ("ci", "9204", "engram-agents/engram-paper") in calls
        assert ("approval", "9204", "engram-agents/engram-paper") in calls

    def test_flip_single_agent_mode_no_colleague_field_ci_red_refuses(self, monkeypatch, capsys):
        """Single-agent-mode shape: no colleague: field registered on the
        baton at all (fields.get("colleague", "") reads empty), flip to a
        named participant that is not self, with CI red -- must still
        REFUSE. Confirms the broadened gate still fires when there's no
        colleague to distinguish against (matches CLAUDE.md's "colleague
        layer collapses when no counterpart exists" for the
        APPROVAL-existence check specifically, but CI-green must still gate
        regardless of single/multi-agent mode)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("red", "check 'pr-checks' conclusion=FAILURE")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("no_approval", "no APPROVED review")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        # No colleague= kwarg passed -- _make_raw_baton's frontmatter has no
        # "colleague" key, so fields.get("colleague", "") reads "".
        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9205", github="pr/engram-agents/engram-paper/9205",
                turn="ariadne", participants="[ariadne, borges]",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9205", to="borges", reason="ready", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        assert ("ci", "9205", "engram-agents/engram-paper") in calls
        captured = capsys.readouterr()
        assert "CI is red" in captured.err

    def test_cmd_flip_gate_fires_identically_across_different_pinned_sentinels(
        self, monkeypatch, capsys,
    ):
        """#2912 seat-independence, exercised end-to-end through cmd_flip
        (companion to TestIsMaintainerFlip's direct-call invariance test
        below): _pool_sentinel() resolves per-INSTALL from that seat's own
        $ENGRAM_HOME/config.json primary_user, not a fleet-wide constant --
        the identical command can see a different resolved sentinel
        depending on who runs it (e.g. "lei shi" on one seat, "lin" on
        another). The SAME named-maintainer flip (CI red, to a named
        maintainer that is neither the current holder nor the registered
        colleague) must refuse identically no matter which sentinel value
        is resolved -- proving the gate's firing does not depend on the
        sentinel's literal value, only on the (to_agent, current_holder,
        colleague_name) triple."""
        for sentinel_value in ("casey", "lei shi", "lin"):
            monkeypatch.setattr(_baton, "_pool_sentinel", lambda v=sentinel_value: v)
            calls = []

            def _fake_ci_state(pr_num, repo=None, _calls=calls):
                _calls.append(("ci", pr_num, repo))
                return ("red", "check 'pr-checks' conclusion=FAILURE")

            def _fake_approval_state(pr_num, repo=None, _calls=calls):
                _calls.append(("approval", pr_num, repo))
                return ("no_approval", "no APPROVED review")

            monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
            monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

            client = _make_client(
                get_return={"raw": _make_raw_baton(
                    "PR-9206", github="pr/engram-agents/engram-paper/9206",
                    turn="ariadne", participants="[ariadne, borges, sol]",
                    colleague="sol",
                )},
            )
            args = argparse.Namespace(
                project_id="PR-9206", to="borges", reason="ready", force=False,
            )
            with pytest.raises(SystemExit) as exc_info:
                _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

            assert exc_info.value.code == EXIT_VALIDATION, (
                f"expected refusal under sentinel={sentinel_value!r}"
            )
            client.post.assert_not_called()
            assert ("ci", "9206", "engram-agents/engram-paper") in calls, (
                f"expected the CI gate to have fired under sentinel={sentinel_value!r}"
            )
            captured = capsys.readouterr()
            assert "CI is red" in captured.err

    def test_flip_to_named_maintainer_bare_anchor_refuses_never_guesses(self, monkeypatch, capsys):
        """Reviewer-fairy-suggested companion to TestFlipRepoQualifiedGuard's
        test_flip_bare_anchor_refuses_never_guesses: that pre-existing test
        only exercises the unqualified-anchor refusal call site
        (`_pr_anchor_match and _is_maintainer_flip(...) and
        _parsed_anchor[1] is None`) with a flip TO the literal pool sentinel
        -- the one shape that already triggered _is_maintainer_flip under
        the OLD bare _is_pool_sentinel(to_agent) predicate. This test
        confirms the SAME unqualified-anchor refusal fires when the flip
        target is a NAMED maintainer ("borges") instead -- the shape #2912's
        broadening exists for, and a shape the unqualified-anchor branch had
        never been exercised under before this test."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        calls = []

        def _fake_ci_state(pr_num, repo=None):
            calls.append(("ci", pr_num, repo))
            return ("green", "mergeStateStatus=CLEAN")

        def _fake_approval_state(pr_num, repo=None):
            calls.append(("approval", pr_num, repo))
            return ("fresh", "approved at current tip")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)
        monkeypatch.setattr(_baton, "_pr_approval_state", _fake_approval_state)

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9207", github="pr/9207",
                turn="ariadne", participants="[ariadne, borges, sol]",
                colleague="sol",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9207", to="borges", reason="ready", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()  # the flip never happens
        assert calls == []  # no gh call at all -- never guessed a repo
        captured = capsys.readouterr()
        assert "has no repo-qualified github anchor" in captured.err


class TestFlipLegacyAuthorFallback:
    """#2961 follow-up (Ariadne's round-2 catch on PR#2966): the CLI path
    (cmd_flip) must recover the author-proxy from a legacy baton's own
    turn log, the same as the REST path's _check_flip_gate does -- this
    is the 1,493-existing-baton gap a bulk migration would otherwise have
    needed a live write to close. Uses the same pinned-sentinel fixture
    convention as TestFlipMaintainerRoleGate above."""

    def test_cmd_flip_recovers_author_from_turn_log_without_author_field(self, monkeypatch, capsys):
        """No `author:` frontmatter field at all (the pre-#2961 shape) --
        but the turn log's own first line names "clio" as the real
        author. Flipping FROM ariadne (the current holder, post-review)
        BACK TO clio must succeed ungated, exactly as it would if an
        explicit `author: clio` field were present -- CI is deliberately
        mocked red, so a false-positive "maintainer flip" classification
        would refuse this and fail the test."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        def _fake_ci_state(pr_num, repo=None):
            raise AssertionError(
                "_pr_ci_state must not be called -- the legacy-author "
                "fallback should have exempted this flip before any "
                "CI/approval check runs"
            )

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)

        raw = _make_raw_baton(
            "PR-9300", github="pr/example-org/example-repo/9300",
            turn="ariadne", participants="[clio, ariadne]",
            colleague="ariadne",
        )
        # _make_raw_baton's default body line is a synthetic stub
        # ("- ... initialized", no arrow/target) -- replace it with the
        # REAL shape init() actually writes, naming clio as the author.
        raw = raw.replace(
            "- 2026-06-26T12:00:00Z initialized",
            "- 2026-06-26T12:00:00Z initialized → clio: project initialized by clio",
        )
        client = _make_client(get_return={"raw": raw})
        args = argparse.Namespace(
            project_id="PR-9300", to="clio", reason="one blocker, back to author", force=False,
        )
        _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        client.post.assert_called_once()
        captured = capsys.readouterr()
        assert captured.err.strip() == ""

    def test_cmd_flip_with_no_initialized_line_still_gates(self, monkeypatch, capsys):
        """Companion control: a baton with no `author:` field AND no
        parseable "initialized -> X:" line at all (the synthetic stub
        _make_raw_baton produces by default) has no fallback to recover --
        the flip must still be gated exactly as it was before this fix,
        never silently exempted."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")

        def _fake_ci_state(pr_num, repo=None):
            return ("red", "check 'pr-checks' conclusion=FAILURE")

        monkeypatch.setattr(_baton, "_pr_ci_state", _fake_ci_state)

        client = _make_client(
            get_return={"raw": _make_raw_baton(
                "PR-9301", github="pr/example-org/example-repo/9301",
                turn="ariadne", participants="[clio, ariadne]",
                colleague="ariadne",
            )},
        )
        args = argparse.Namespace(
            project_id="PR-9301", to="clio", reason="one blocker, back to author", force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _baton.cmd_flip(args, MULTI_CONFIG, "ariadne", client)

        assert exc_info.value.code == EXIT_VALIDATION
        client.post.assert_not_called()
        captured = capsys.readouterr()
        assert "CI is red" in captured.err


class TestIsMaintainerFlip:
    """Direct-call unit tests for _is_maintainer_flip (#2912) -- the
    broadened trigger predicate cmd_flip now uses in place of the bare
    _is_pool_sentinel(to_agent) check. Pins the sentinel via
    monkeypatch.setattr(_baton, "_pool_sentinel", ...) rather than reading
    the ambient real config.json, for the same reason documented on
    TestFlipMaintainerRoleGate above."""

    def test_sentinel_is_maintainer_flip(self, monkeypatch):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        assert _baton._is_maintainer_flip("casey", "borges", "sol") is True

    def test_self_flip_is_not_maintainer_flip(self, monkeypatch):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        assert _baton._is_maintainer_flip("borges", "borges", "sol") is False

    def test_registered_colleague_is_not_maintainer_flip(self, monkeypatch):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        assert _baton._is_maintainer_flip("sol", "borges", "sol") is False

    def test_other_participant_is_maintainer_flip(self, monkeypatch):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        assert _baton._is_maintainer_flip("ariadne", "borges", "sol") is True

    def test_empty_colleague_name_other_participant_is_maintainer_flip(self, monkeypatch):
        """Single-agent-mode shape: colleague_name == "" (no colleague
        registered) -- a flip to any non-self participant still fires."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        assert _baton._is_maintainer_flip("ariadne", "borges", "") is True

    def test_result_invariant_across_different_pinned_sentinel_values(self, monkeypatch):
        """#2912 seat-independence: _is_maintainer_flip's boolean answer for
        a given (to_agent, current_holder, colleague_name) triple must NOT
        depend on which value _pool_sentinel() happens to resolve to.
        _pool_sentinel() resolves per-INSTALL from that seat's own
        $ENGRAM_HOME/config.json primary_user, not a fleet-wide constant --
        the exact same call can see a different resolved sentinel depending
        on who runs it (e.g. "lei shi" on one seat, "lin" on another). The
        fix is sentinel-value-independent by construction: it only ever
        compares against the RESOLVED sentinel value, current_holder, and
        colleague_name -- never a literal hardcoded string -- so this must
        hold across any sentinel value, not just one pinned choice. None of
        the sentinel values exercised below ("casey", "lei shi", "lin",
        "alex") collide with the to_agent/current_holder/colleague names
        used in the assertions ("ariadne", "borges", "sol") -- a collision
        would change which branch of _is_maintainer_flip fires and
        invalidate the comparison."""
        sentinel_values = ("casey", "lei shi", "lin", "alex")
        exercised_names = ("ariadne", "borges", "sol")
        assert not (set(sentinel_values) & set(exercised_names)), (
            "sentinel/name collision would confound the invariance check"
        )
        for sentinel_value in sentinel_values:
            monkeypatch.setattr(_baton, "_pool_sentinel", lambda v=sentinel_value: v)
            # Flip TO the resolved sentinel itself: always True, whatever
            # the sentinel's literal value happens to be this iteration.
            assert _baton._is_maintainer_flip(sentinel_value, "borges", "sol") is True
            # Named-maintainer flip (to_agent is never the sentinel, never
            # the current holder, never the colleague, in any iteration):
            # always True.
            assert _baton._is_maintainer_flip("ariadne", "borges", "sol") is True
            # Self-flip (to_agent == current_holder; neither ever equals
            # the sentinel in this loop): always False.
            assert _baton._is_maintainer_flip("borges", "borges", "sol") is False
            # Flip to the registered colleague (to_agent == colleague_name;
            # neither ever equals the sentinel in this loop): always False.
            assert _baton._is_maintainer_flip("sol", "borges", "sol") is False

    def test_flip_to_author_is_not_maintainer_flip(self, monkeypatch):
        """#2961: PR-2926's real shape -- colleague "ariadne" holds the
        baton after reviewing, finds a blocker, and flips it back to the
        PR's author "clio". to_agent (clio) is neither the current holder
        (ariadne) nor the colleague (ariadne) -- pre-#2961 this fell
        through to True (misclassified as presenting-for-merge) and
        demanded an approval that cannot exist, since the colleague is the
        one objecting. With `author="clio"` passed, it must be False."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        assert _baton._is_maintainer_flip("clio", "ariadne", "ariadne") is True  # pre-#2961 shape, no author arg
        assert _baton._is_maintainer_flip("clio", "ariadne", "ariadne", "clio") is False

    def test_missing_author_is_pre_2961_behavior(self, monkeypatch):
        """An empty/falsy `author` (a baton created before #2961, with no
        `author:` frontmatter field) must reproduce the exact pre-#2961
        answer -- the exemption simply cannot fire, not an error and not a
        different default classification."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        assert _baton._is_maintainer_flip("clio", "ariadne", "ariadne", "") is True
        assert _baton._is_maintainer_flip("ariadne", "borges", "sol", "") is True
        assert _baton._is_maintainer_flip("borges", "borges", "sol", "") is False
        assert _baton._is_maintainer_flip("sol", "borges", "sol", "") is False

    def test_author_exemption_does_not_override_sentinel_or_self_flip(self, monkeypatch):
        """The author exemption is checked LAST -- a flip to the pool
        sentinel, or a self-flip, must keep its own answer even when the
        target also happens to equal `author` (an author flipping the
        baton to the human maintainer, or to themselves, is unaffected by
        this new parameter)."""
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: "casey")
        # to_agent is both the sentinel AND (contrived) the stored author --
        # sentinel wins, still True.
        assert _baton._is_maintainer_flip("casey", "borges", "sol", "casey") is True
        # to_agent is both the current holder AND the stored author --
        # self-flip wins, still False.
        assert _baton._is_maintainer_flip("borges", "borges", "sol", "borges") is False


# ===========================================================================
# CATEGORY 15: _pr_approval_state direct-call parsing coverage (#2320)
# ===========================================================================


class TestApprovalStateParsing:
    """Direct-call unit tests for _pr_approval_state -- the companion to
    TestCiStateDedup's _pr_ci_state coverage above (_pr_approval_state is the
    approval-freshness half of the same gate; _pr_ci_state is the CI-green
    half).

    Ported from the deleted tools/test_baton_approval_guard.py (removed as an
    orphan by #2310; every OTHER reference to _pr_approval_state anywhere in
    this repo -- including elsewhere in this file -- is a monkeypatch stub
    that replaces the function outright and never asserts on what it
    actually returns, so these were the only direct-call tests, see #2320).

    Adapted for the current signature, `_pr_approval_state(pr_number, repo)`:
    `repo` is now REQUIRED (#1715/#1918 removed the `gh repo view`
    cwd-inference fallback -- a caller with no repo-qualified anchor now gets
    "unknown" rather than a silent guess), and the function makes a SINGLE
    `subprocess.run` call (the GraphQL query only) instead of the old
    repo-view-then-graphql two-call shape.
    """

    @pytest.fixture
    def baton(self):
        return _load_baton_module()

    REPO = "engram-agents/engram-paper"

    @staticmethod
    def _gql_payload(head_oid, review_nodes):
        """Build the GraphQL response envelope expected by _pr_approval_state."""
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "headRefOid": head_oid,
                        "reviews": {"nodes": review_nodes},
                    }
                }
            }
        }

    @classmethod
    def _mock_gql_result(cls, payload, returncode=0, stderr=""):
        """Build a mock subprocess.CompletedProcess-like object for the single
        `gh api graphql` call _pr_approval_state now makes."""
        return _mock.MagicMock(
            returncode=returncode,
            stdout=json.dumps(payload) if payload is not None else "",
            stderr=stderr,
        )

    # --- core verdict tests ---

    def test_state_fresh_when_approval_oid_matches_tip(self, baton):
        """APPROVED review with commit.oid == headRefOid -> fresh."""
        tip = "aabbccdd1122334455667788"
        mock_result = self._mock_gql_result(self._gql_payload(tip, [
            {"state": "APPROVED", "commit": {"oid": tip}},
        ]))
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_approval_state("1", repo=self.REPO)
        assert state == "fresh"
        assert tip[:8] in detail

    def test_state_stale_when_approval_oid_differs(self, baton):
        """APPROVED review exists but commit.oid != headRefOid (rebase case)
        -- stale even if the approval's committedDate would be OLDER than the
        tip."""
        tip = "aaaa0000111122223333"
        old_oid = "bbbb9999888877776666"
        mock_result = self._mock_gql_result(self._gql_payload(tip, [
            {"state": "APPROVED", "commit": {"oid": old_oid}},
        ]))
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_approval_state("1", repo=self.REPO)
        assert state == "stale"
        assert tip[:8] in detail
        assert old_oid[:8] in detail

    def test_state_no_approval_without_approved_reviews(self, baton):
        """Only COMMENTED / CHANGES_REQUESTED reviews -> no_approval."""
        tip = "deadbeef00001111"
        mock_result = self._mock_gql_result(self._gql_payload(tip, [
            {"state": "COMMENTED", "commit": {"oid": "old111"}},
            {"state": "CHANGES_REQUESTED", "commit": {"oid": "old222"}},
        ]))
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, _ = baton._pr_approval_state("1", repo=self.REPO)
        assert state == "no_approval"

    def test_state_dismissed_only_is_no_approval(self, baton):
        """DISMISSED reviews only -> no_approval (not stale)."""
        tip = "cafebabe12345678"
        mock_result = self._mock_gql_result(self._gql_payload(tip, [
            {"state": "DISMISSED", "commit": {"oid": "old333"}},
        ]))
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, _ = baton._pr_approval_state("1", repo=self.REPO)
        assert state == "no_approval"

    def test_state_fresh_with_commit_null_and_matching_approval(self, baton):
        """A review node with commit: null is non-matching but not an error;
        a second node that matches the tip still produces fresh."""
        tip = "1234567890abcdef"
        mock_result = self._mock_gql_result(self._gql_payload(tip, [
            {"state": "APPROVED", "commit": None},
            {"state": "APPROVED", "commit": {"oid": tip}},
        ]))
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_approval_state("1", repo=self.REPO)
        assert state == "fresh"
        assert tip[:8] in detail

    # --- gh failure ladder tests ---

    def test_state_unknown_on_gql_failure(self, baton):
        """GraphQL call failure -> unknown (with gh's stderr text)."""
        mock_result = self._mock_gql_result(
            None, returncode=1, stderr="gh: not logged in",
        )
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_approval_state("1", repo=self.REPO)
        assert state == "unknown"
        assert "not logged in" in detail

    def test_state_unknown_on_gql_malformed_json(self, baton):
        """GraphQL returns non-JSON -> unknown."""
        mock_result = _mock.MagicMock(returncode=0, stdout="not json", stderr="")
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, _ = baton._pr_approval_state("1", repo=self.REPO)
        assert state == "unknown"

    def test_state_unknown_on_missing_repo_argument(self, baton):
        """The old `gh repo view` cwd-inference call this test targeted
        (test_state_unknown_on_repo_view_failure) no longer exists -- #1715/
        #1918 made `repo` a required caller-supplied argument instead of
        something _pr_approval_state fetched itself. Repurposed to the
        validation path that replaced it: an empty `repo` argument returns
        "unknown" without invoking gh at all (baton.py:524-525)."""
        def _boom(*args, **kwargs):
            raise AssertionError("gh must not be invoked when repo is empty")
        with _mock.patch("subprocess.run", _boom):
            state, detail = baton._pr_approval_state("1", repo="")
        assert state == "unknown"
        assert "repo" in detail

    def test_state_unknown_on_empty_gql_response(self, baton):
        """GraphQL returns empty body -> unknown."""
        mock_result = _mock.MagicMock(returncode=0, stdout="", stderr="")
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, _ = baton._pr_approval_state("1", repo=self.REPO)
        assert state == "unknown"

    def test_state_non_numeric_pr_number_is_unknown(self, baton):
        """The ONLY test of the GraphQL injection guard (baton.py:520-527):
        pr_number is interpolated into the query string, so a non-numeric
        value must return unknown without invoking gh at all."""
        def _boom(*args, **kwargs):
            raise AssertionError("gh must not be invoked for a non-numeric PR number")
        with _mock.patch("subprocess.run", _boom):
            state, detail = baton._pr_approval_state('123") { id } } evil', repo=self.REPO)
        assert state == "unknown"
        assert "not numeric" in detail

    def test_state_hash_prefixed_numeric_pr_number_ok(self, baton):
        """A '#123'-shaped value is tolerated (stripped), matching CLI
        ergonomics."""
        calls = []

        def _fake_run(argv, **kwargs):
            calls.append(argv)
            return _mock.MagicMock(
                returncode=0,
                stderr="",
                stdout=(
                    '{"data": {"repository": {"pullRequest": {"headRefOid": "abc123ff",'
                    ' "reviews": {"nodes": [{"state": "APPROVED", "commit": {"oid": "abc123ff"}}]}}}}}'
                ),
            )

        with _mock.patch("subprocess.run", _fake_run):
            state, detail = baton._pr_approval_state("#123", repo=self.REPO)
        assert state == "fresh"
        assert any("123" in " ".join(c) for c in calls if c[:2] == ["gh", "api"])

    def test_state_no_approval_empty_review_list(self, baton):
        """A PR with zero reviews of any kind is no_approval (reviewer-fairy
        suggestion on the original PR: pin the empty-list path so a future
        edit can't silently turn it into unknown)."""
        mock_result = self._mock_gql_result(self._gql_payload("abc123ff", []))
        with _mock.patch("subprocess.run", return_value=mock_result):
            state, detail = baton._pr_approval_state("123", repo=self.REPO)
        assert state == "no_approval"
