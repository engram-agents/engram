"""Tests for tools/tripwire.py — falsifiability-grade checker for GitHub PR approvals.

All tests are hermetic: no real gh calls, no real config files outside tmp_path.

Coverage:
  1.  test_falsifiable_via_commit             — commit by approver → exit 0, cited
  2.  test_falsifiable_via_line_comment       — line-anchored comment → exit 0
  3.  test_say_so_bare_lgtm                   — no commits, no line comments → exit 1
  4.  test_say_so_review_body_not_counted     — comment with line=null not counted → exit 1
  5.  test_config_inference_opus              — self_lineage=opus → agent_name used
  6.  test_config_inference_sonnet            — self_lineage=sonnet + peer → peer used
  7.  test_config_inference_sonnet_no_peer    — self_lineage=sonnet, no peer → error exit 2
  8.  test_explicit_approver_overrides_config — --approver wins over config
  9.  test_json_output_falsifiable            — --json with traces → grade: falsifiable
  10. test_json_output_say_so                 — --json no traces → grade: say-so
  11. test_no_approved_review_from_approver   — all traces by others → say-so exit 1
  12. test_gh_api_failure                     — subprocess non-zero → error exit 2
  13. test_falsifiable_via_original_line_fallback — line=None, original_line set → exit 0
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.tripwire as tripwire  # noqa: E402

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

REPO = "test-owner/test-repo"
PR = 42
APPROVER = "borges"

# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------


def _gh_result(payload, returncode: int = 0, stderr: str = ""):
    """Build a mock subprocess.CompletedProcess-like object."""
    m = mock.Mock()
    m.returncode = returncode
    m.stdout = json.dumps(payload) if payload is not None else ""
    m.stderr = stderr
    return m


def _make_commit(
    login: str,
    sha: str = "abc1234567890abcdef",
    message: str = "fix: do something useful",
    date: str = "2026-06-01T10:00:00Z",
) -> dict:
    """Build a commit entry matching the gh pr view --json commits shape.

    Real shape (empirically verified): .authors (array), .messageHeadline,
    .authoredDate, .oid — NOT .author/.committer/.commit nesting.
    """
    return {
        "oid": sha,
        "authors": [{"login": login, "name": "Agent", "email": "agent@example.com"}],
        "messageHeadline": message,
        "messageBody": "",
        "authoredDate": date,
        "committedDate": date,
    }


def _make_line_comment(
    login: str,
    path: str = "src/server.py",
    line: int = 42,
) -> dict:
    """Build a line-anchored review comment from gh api .../pulls/{PR}/comments."""
    return {
        "id": 100,
        "user": {"login": login},
        "path": path,
        "line": line,
        "original_line": line,
        "body": "this looks suspicious",
        "diff_hunk": "@@ -40,6 +40,7 @@",
    }


def _make_null_line_comment(login: str) -> dict:
    """Build a review comment where line and original_line are both null.

    These come from the /comments endpoint but are NOT line-anchored (no diff
    context selected).  tripwire must NOT count them as traces.
    """
    return {
        "id": 101,
        "user": {"login": login},
        "path": None,
        "line": None,
        "original_line": None,
        "body": "LGTM overall, looks good to me",
    }


def _patch_two_calls(monkeypatch, commits: list, comments: list) -> None:
    """Mock subprocess.run for the 2 gh calls tripwire makes.

    Call 1: gh pr view <PR> --repo owner/repo --json commits
    Call 2: gh api repos/owner/repo/pulls/<PR>/comments
    """
    results = [
        _gh_result({"commits": commits}),
        _gh_result(comments),
    ]
    monkeypatch.setattr(
        tripwire.subprocess, "run", mock.Mock(side_effect=results)
    )


# ---------------------------------------------------------------------------
# Invocation helper
# ---------------------------------------------------------------------------


def _make_args(
    repo: str = REPO,
    pr: int = PR,
    approver: str | None = None,
    json_output: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        command="check",
        repo=repo,
        pr=pr,
        approver=approver,
        json=json_output,
    )


def _invoke_check(args: argparse.Namespace) -> tuple[int, str, str]:
    """Call cmd_check, capturing stdout/stderr and the exit code."""
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exit_code = 0
    with mock.patch("sys.stdout", stdout_buf), mock.patch("sys.stderr", stderr_buf):
        try:
            exit_code = tripwire.cmd_check(args)
        except SystemExit as exc:
            exit_code = int(exc.code) if exc.code is not None else 0
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Falsifiable via commit
# ---------------------------------------------------------------------------


def test_falsifiable_via_commit(monkeypatch):
    """Approver has a commit on the PR branch → exit 0, commit cited in output."""
    _patch_two_calls(
        monkeypatch,
        commits=[_make_commit(APPROVER)],
        comments=[],
    )
    code, out, err = _invoke_check(_make_args(approver=APPROVER))
    assert code == tripwire.EXIT_OK, f"expected 0, got {code}; stderr={err!r}"
    assert "FALSIFIABLE" in out
    assert "commit" in out.lower()
    # SHA prefix from the mock oid "abc1234567890abcdef" → "abc1234"
    assert "abc1234" in out


# ---------------------------------------------------------------------------
# 2. Falsifiable via line-anchored comment
# ---------------------------------------------------------------------------


def test_falsifiable_via_line_comment(monkeypatch):
    """Approver has a line-anchored comment → exit 0, comment cited in output."""
    _patch_two_calls(
        monkeypatch,
        commits=[],
        comments=[_make_line_comment(APPROVER, path="src/server.py", line=42)],
    )
    code, out, err = _invoke_check(_make_args(approver=APPROVER))
    assert code == tripwire.EXIT_OK, f"expected 0, got {code}; stderr={err!r}"
    assert "FALSIFIABLE" in out
    assert "line-anchored" in out.lower()
    assert "src/server.py" in out
    assert "42" in out


# ---------------------------------------------------------------------------
# 3. Say-so: bare LGTM (no commit, no line-anchored comment)
# ---------------------------------------------------------------------------


def test_say_so_bare_lgtm(monkeypatch):
    """Approver submitted only a bare LGTM review body — no commit, no line comment.

    Review bodies come from gh api .../reviews and are NOT checked by tripwire.
    Both trace endpoints return nothing for the approver → say-so exit 1.
    """
    _patch_two_calls(monkeypatch, commits=[], comments=[])
    code, out, err = _invoke_check(_make_args(approver=APPROVER))
    assert code == tripwire.EXIT_SAY_SO, f"expected 1, got {code}; stderr={err!r}"
    assert "SAY-SO" in out
    assert APPROVER in out
    # Output should mention what was checked and found nothing
    assert "NONE" in out


# ---------------------------------------------------------------------------
# 4. Say-so: review body with null line is NOT a trace
# ---------------------------------------------------------------------------


def test_say_so_review_body_not_counted(monkeypatch):
    """Comment in /comments with line=null and original_line=null is not line-anchored.

    Even if the comment body contains review text, it does not count as a trace.
    """
    _patch_two_calls(
        monkeypatch,
        commits=[],
        comments=[_make_null_line_comment(APPROVER)],
    )
    code, out, err = _invoke_check(_make_args(approver=APPROVER))
    assert code == tripwire.EXIT_SAY_SO, f"expected 1, got {code}; stderr={err!r}"
    assert "SAY-SO" in out


# ---------------------------------------------------------------------------
# 5. Config inference: opus → agent_name
# ---------------------------------------------------------------------------


def test_config_inference_opus(monkeypatch, tmp_path):
    """self_lineage=opus → approver inferred as agent_name (self-check mode)."""
    config = {"self_lineage": "anthropic:opus", "agent_name": "ariadne"}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    monkeypatch.setenv("ENGRAM_CONFIG_PATH", str(config_file))

    _patch_two_calls(
        monkeypatch,
        commits=[_make_commit("ariadne")],
        comments=[],
    )
    code, out, err = _invoke_check(_make_args(approver=None))
    assert code == tripwire.EXIT_OK, f"expected 0, got {code}; err={err!r}"
    # Output must reference the inferred approver "ariadne"
    assert "ariadne" in out


# ---------------------------------------------------------------------------
# 6. Config inference: sonnet + peer → peer value
# ---------------------------------------------------------------------------


def test_config_inference_sonnet(monkeypatch, tmp_path):
    """self_lineage=sonnet + peer=ariadne → approver inferred as ariadne."""
    config = {"self_lineage": "anthropic:sonnet", "peer": "ariadne"}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    monkeypatch.setenv("ENGRAM_CONFIG_PATH", str(config_file))

    _patch_two_calls(
        monkeypatch,
        commits=[_make_commit("ariadne")],
        comments=[],
    )
    code, out, err = _invoke_check(_make_args(approver=None))
    assert code == tripwire.EXIT_OK, f"expected 0, got {code}; err={err!r}"
    assert "ariadne" in out


# ---------------------------------------------------------------------------
# 7. Config inference: sonnet but no peer field → error
# ---------------------------------------------------------------------------


def test_config_inference_sonnet_no_peer(monkeypatch, tmp_path):
    """self_lineage=sonnet with no 'peer' field → actionable error, exit 2."""
    config = {"self_lineage": "anthropic:sonnet", "agent_name": "sol"}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    monkeypatch.setenv("ENGRAM_CONFIG_PATH", str(config_file))

    code, out, err = _invoke_check(_make_args(approver=None))
    assert code == tripwire.EXIT_ERROR, f"expected 2, got {code}"
    # Error message must mention 'peer' so the user knows what to fix or supply
    assert "peer" in err.lower()
    # And point to --approver as the workaround
    assert "--approver" in err


# ---------------------------------------------------------------------------
# 8. Explicit --approver overrides config
# ---------------------------------------------------------------------------


def test_explicit_approver_overrides_config(monkeypatch, tmp_path):
    """--approver CLI arg takes precedence; config is not read at all."""
    # Config would infer "borges" via agent_name (opus)
    config = {"self_lineage": "anthropic:opus", "agent_name": "borges"}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    monkeypatch.setenv("ENGRAM_CONFIG_PATH", str(config_file))

    # Explicit approver is "ariadne"
    _patch_two_calls(
        monkeypatch,
        commits=[_make_commit("ariadne")],
        comments=[],
    )
    code, out, err = _invoke_check(_make_args(approver="ariadne"))
    assert code == tripwire.EXIT_OK, f"expected 0, got {code}; err={err!r}"
    # Output names the explicitly-supplied approver, not the config-derived one
    assert "ariadne" in out
    assert "borges" not in out


# ---------------------------------------------------------------------------
# 9. JSON output: falsifiable
# ---------------------------------------------------------------------------


def test_json_output_falsifiable(monkeypatch):
    """--json flag with traces present → valid JSON with grade: falsifiable."""
    _patch_two_calls(
        monkeypatch,
        commits=[_make_commit(APPROVER, sha="deadbeef1234567890",
                              message="fix: handle edge case")],
        comments=[],
    )
    code, out, err = _invoke_check(_make_args(approver=APPROVER, json_output=True))
    assert code == tripwire.EXIT_OK, f"expected 0, got {code}"
    data = json.loads(out)
    assert data["grade"] == "falsifiable"
    assert data["approver"] == APPROVER
    assert data["repo"] == REPO
    assert data["pr"] == PR
    assert len(data["traces"]) >= 1
    trace = data["traces"][0]
    assert trace["type"] == "commit"
    assert trace["sha"] == "deadbeef"[:7]
    assert "fix: handle edge case" in trace["message"]


# ---------------------------------------------------------------------------
# 10. JSON output: say-so
# ---------------------------------------------------------------------------


def test_json_output_say_so(monkeypatch):
    """--json flag with no traces → valid JSON with grade: say-so, traces: []."""
    _patch_two_calls(monkeypatch, commits=[], comments=[])
    code, out, err = _invoke_check(_make_args(approver=APPROVER, json_output=True))
    assert code == tripwire.EXIT_SAY_SO, f"expected 1, got {code}"
    data = json.loads(out)
    assert data["grade"] == "say-so"
    assert data["approver"] == APPROVER
    assert data["repo"] == REPO
    assert data["pr"] == PR
    assert data["traces"] == []


# ---------------------------------------------------------------------------
# 11. Approver has no trace at all (not even reviewed)
# ---------------------------------------------------------------------------


def test_no_approved_review_from_approver(monkeypatch):
    """All commits and comments belong to a different person → say-so exit 1."""
    OTHER = "ariadne"
    _patch_two_calls(
        monkeypatch,
        commits=[_make_commit(OTHER)],
        comments=[_make_line_comment(OTHER)],
    )
    code, out, err = _invoke_check(_make_args(approver=APPROVER))
    assert code == tripwire.EXIT_SAY_SO, f"expected 1, got {code}; stderr={err!r}"
    assert "SAY-SO" in out
    # The FLAG message names the approver being checked
    assert APPROVER in out


# ---------------------------------------------------------------------------
# 13. Falsifiable via original_line fallback (line=None, original_line set)
# ---------------------------------------------------------------------------


def test_falsifiable_via_original_line_fallback(monkeypatch):
    """Comment with line=None but original_line set → counts as line-anchored trace."""
    comment = {
        "id": 200,
        "user": {"login": APPROVER},
        "path": "src/main.py",
        "line": None,
        "original_line": 77,
        "body": "nit: rename this variable",
    }
    _patch_two_calls(monkeypatch, commits=[], comments=[comment])
    code, out, err = _invoke_check(_make_args(approver=APPROVER))
    assert code == tripwire.EXIT_OK, f"expected 0, got {code}; stderr={err!r}"
    assert "FALSIFIABLE" in out
    assert "src/main.py" in out
    assert "77" in out


# ---------------------------------------------------------------------------
# 12. gh API failure → informative error, exit 2
# ---------------------------------------------------------------------------


def test_gh_api_failure(monkeypatch):
    """First gh call returns non-zero → informative error printed, exit 2."""
    fail_result = _gh_result(None, returncode=1, stderr="gh: could not authenticate")
    monkeypatch.setattr(
        tripwire.subprocess, "run",
        mock.Mock(return_value=fail_result),
    )
    code, out, err = _invoke_check(_make_args(approver=APPROVER))
    assert code == tripwire.EXIT_ERROR, f"expected 2, got {code}"
    assert "could not authenticate" in err
