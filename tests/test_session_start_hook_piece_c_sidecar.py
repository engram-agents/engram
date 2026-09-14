"""SessionStart context-budget fix (#2859): Piece C moves to a sidecar file +
the substrate-health/cheap-block group moves ahead of Piece C's pointer.

Kepler measured that the Claude Code harness persists any hook output above
~6,000-11,000 characters to a `tool-results/hook-*` file and shows only a
~2,000-char preview inline. Piece C's full per-repo commit listing used to be
returned inline and appended directly to additionalContext — on a real graph
with real commit history this pushed everything emitted AFTER it (fairy-policy
lines, forum monitor arm-instructions, peer topology, and eventually the
substrate-health warning, which used to run dead-last in main()) past that
boundary, where a truncating reader never sees it.

This file covers the four cases named in the spec (cohort-specs-2026-09-12/
2859-sessionstart-context-budget.md):
  1. A fixture repo with 60 commits (> PIECE_C_MAX_LINES) renders the SHORT
     pointer form, writes the full listing to a sidecar file, and the
     must-not-miss group (active session / session-start-reading / fairy
     policy lines) lands within the first ~1,800 characters.
  2. A fixture repo with zero commits in the window: no sidecar file, no
     pointer line (matches the old ""-return behavior).
  3. A forced-unhealthy substrate-health condition renders its warning block
     within the first ~1,800 characters, not at the tail.
  4. A sidecar-write failure degrades piece_c_git_log() to "" rather than
     raising or emitting a broken pointer.

Complements tests/test_session_start_hook_block_order.py (which locks the
pre-existing #1819/#2150 ordering invariant this PR does not touch) and
tests/test_session_start_hook_mcp_health.py (unit coverage of
_check_mcp_health itself).
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOOK_PATH = _REPO_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"

# Roughly the harness head-preview boundary Kepler measured (~2,000 chars);
# the spec asks for "first ~1,800 characters" as a safety margin under it.
_HEAD_PREVIEW_CHARS = 1800

_CANNED_CONF_ALL = {
    "by_type": {
        "observation_factual": {"n": 20, "mean": 0.72, "p25": 0.65, "p50": 0.72, "p75": 0.80, "p90": 0.88, "p95": 0.92},
    },
    "by_quote_type": {},
    "by_reasoning_type": {},
    "by_source_class": {},
}
_CANNED_CONF_7D = {
    "by_type": {},
    "by_quote_type": {},
    "by_reasoning_type": {},
    "by_source_class": {},
}


def _load_hook_module(engram_home: Path, piece_c_repos: str | None = None) -> object:
    """Load the session-start hook fresh, with ENGRAM_HOME (and, optionally,
    ENGRAM_PIECE_C_REPOS) set BEFORE exec — PIECE_C_REPOS is computed at
    module-load time from these env vars (see the hook's own module-level
    construction, ~line 1951), so they must be set prior to exec_module."""
    os.environ["ENGRAM_HOME"] = str(engram_home)
    if piece_c_repos is not None:
        os.environ["ENGRAM_PIECE_C_REPOS"] = piece_c_repos
    else:
        os.environ.pop("ENGRAM_PIECE_C_REPOS", None)

    stub_ct = MagicMock()
    stub_ct.write_baseline = MagicMock(return_value=None)
    sys.modules["context_tracker"] = stub_ct

    for mod_name in list(sys.modules.keys()):
        if mod_name in ("engram-session-start-hook", "engram_session_start_hook"):
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location("engram_session_start_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_mock_engram_client():
    mock_client = MagicMock()

    def _call_side_effect(tool_name, arguments=None):
        if tool_name != "engram_stats":
            return {}
        try:
            params = json.loads((arguments or {}).get("payload_json", "{}"))
        except (json.JSONDecodeError, AttributeError):
            params = {}
        if params.get("mode") == "7-turn":
            return {"confidence": _CANNED_CONF_7D}
        return {
            "confidence": _CANNED_CONF_ALL,
            "memory": {"current_turn": 1, "tier_sizes": {}},
        }

    mock_client.call.side_effect = _call_side_effect
    return mock_client


def _run_hook_main(module, hook_input: dict) -> dict:
    stdin_data = json.dumps(hook_input)
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with redirect_stdout(buf):
            module.main()
    return json.loads(buf.getvalue().strip())


def _make_fixture_repo(repo_path: Path, num_commits: int) -> None:
    """A throwaway git repo with `num_commits` empty commits, all within
    piece_c_git_log()'s default 2-day window (they're created "now")."""
    repo_path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Piece C Fixture",
            "GIT_AUTHOR_EMAIL": "piece-c-fixture@example.com",
            "GIT_COMMITTER_NAME": "Piece C Fixture",
            "GIT_COMMITTER_EMAIL": "piece-c-fixture@example.com",
        }
    )
    subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "Piece C Fixture"], cwd=repo_path, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "piece-c-fixture@example.com"], cwd=repo_path, check=True, env=env)
    for i in range(num_commits):
        subprocess.run(
            ["git", "commit", "--allow-empty", "-q", "-m", f"fixture commit {i}"],
            cwd=repo_path,
            check=True,
            env=env,
        )


def _make_empty_fixture_repo(repo_path: Path) -> None:
    """A git repo with NO commits at all — the zero-commits-in-window case."""
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)


@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram"
    home.mkdir()
    (home / "sessions").mkdir()
    # A minimal (but present) config.json: fairy_policy_block() only fires
    # when config.json exists at all (its open() would otherwise raise
    # FileNotFoundError -> caught -> ""). Default "auto"/"auto"/"you"
    # values are enough to exercise the "must-not-miss group precedes Piece
    # C" assertion below without needing to configure anything else.
    (home / "config.json").write_text("{}", encoding="utf-8")
    return home


# ── 1. Fixture-repo test: large repo -> short pointer + sidecar file ───────


def test_piece_c_large_repo_renders_short_pointer_and_writes_sidecar(tmp_path, engram_home):
    fixture_repo = tmp_path / "fixture-repo-large"
    _make_fixture_repo(fixture_repo, 60)

    mod = _load_hook_module(engram_home, piece_c_repos=str(fixture_repo))
    # Neutralize two blocks that are real-environment-dependent and NOT what
    # this test is about, so the "minimal-or-mocked graph" total-length
    # assertion below measures Piece C's own fix rather than ambient noise:
    #   - substrate health: this dev seat's own MCP/backup/viz state would
    #     otherwise leak into the render (e.g. no MCP server process running
    #     under pytest); test 3 below covers substrate-health rendering
    #     directly and deliberately.
    #   - tool_capability_index_block: reads a real, checked-in generated
    #     artifact (tools/engram-generate-tool-capability-index.py's output)
    #     that is large (~10KB) and unrelated to Piece C — out of scope here.
    mod._check_mcp_health = lambda engram_home: (True, None)
    mod._check_backup_staleness = lambda engram_home: (True, None)
    mod._check_viz_server_health = lambda engram_home: ("up", None)
    mod.tool_capability_index_block = lambda: ""
    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        result = _run_hook_main(
            mod,
            {
                "session_id": "piece-c-large",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            },
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]

    # -- Short pointer form, not the old full per-commit inline listing. --
    expected_sidecar = engram_home / "sessions" / "piece-c-large-piece-c.txt"
    assert "written to" in ctx, f"Piece C pointer missing entirely: {ctx!r}"
    assert str(expected_sidecar) in ctx, (
        f"pointer does not name the expected sidecar path {expected_sidecar}: {ctx!r}"
    )
    assert "60 commit(s)" in ctx, f"pointer's commit count wrong: {ctx!r}"
    assert "1 repo(s)" in ctx, f"pointer's repo count wrong: {ctx!r}"
    # The OLD inline shape rendered "  <reponame>:\n    <sha> <subject>" lines
    # directly into additionalContext; none of that per-commit detail should
    # appear inline anymore now that it lives in the sidecar only.
    assert "fixture commit 59" not in ctx, (
        f"old inline per-commit listing leaked into additionalContext: {ctx!r}"
    )

    # -- Sidecar file exists and carries the full commit listing. --
    assert expected_sidecar.exists(), f"sidecar file not written at {expected_sidecar}"
    sidecar_text = expected_sidecar.read_text(encoding="utf-8")
    assert "fixture-repo-large" in sidecar_text
    # git log lists newest-first, and PIECE_C_MAX_LINES=20 caps the rendered
    # lines to the 20 most recent — "fixture commit 59" is the newest (last
    # commit created), so it's the one guaranteed to survive the cap.
    assert "fixture commit 59" in sidecar_text
    # PIECE_C_MAX_LINES=20 caps the per-repo rendered lines; the omitted-count
    # note is the same shape the old inline block used to carry.
    assert "older commits omitted" in sidecar_text

    # -- The must-not-miss group (step 3 of the spec) precedes Piece C's
    #    pointer and lands within the harness's head-preview window. --
    must_not_miss = [
        "[Active session:",
        "[Session start reading —",
        "[Coder fairy policy:",
        "[Reviewer fairy policy:",
    ]
    head = ctx[:_HEAD_PREVIEW_CHARS]
    for needle in must_not_miss:
        assert needle in head, (
            f"must-not-miss line {needle!r} not found in first "
            f"{_HEAD_PREVIEW_CHARS} chars of additionalContext: {head!r}"
        )

    # All of the above land before Piece C's own (short) pointer line.
    pointer_at = ctx.find("written to")
    for needle in must_not_miss:
        assert ctx.find(needle) < pointer_at, (
            f"{needle!r} must precede Piece C's pointer line per the #2859 "
            f"reorder; found at {ctx.find(needle)}, pointer at {pointer_at}"
        )

    # -- Scoped claim (per the spec's own caveat): with this fixture's small,
    #    mocked graph, total emission is well under the ~6,000-char harness
    #    threshold. This says nothing about a large real graph's focus_block()
    #    output, which is explicitly out of scope for this PR. --
    assert len(ctx) < 6000, (
        f"expected total emission under 6,000 chars for this minimal fixture, "
        f"got {len(ctx)}"
    )


# ── 2. Empty-Piece-C case: zero commits in window ──────────────────────────


def test_piece_c_zero_commits_writes_no_sidecar_and_no_pointer(tmp_path, engram_home):
    fixture_repo = tmp_path / "fixture-repo-empty"
    _make_empty_fixture_repo(fixture_repo)

    mod = _load_hook_module(engram_home, piece_c_repos=str(fixture_repo))
    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        result = _run_hook_main(
            mod,
            {
                "session_id": "piece-c-empty",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            },
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]

    assert "Piece C" not in ctx, f"unexpected Piece C content for a zero-commit repo: {ctx!r}"
    assert "written to" not in ctx

    sessions_dir = engram_home / "sessions"
    sidecar_candidates = list(sessions_dir.glob("*-piece-c.txt"))
    assert sidecar_candidates == [], (
        f"no sidecar file should be written for the zero-commits case, found: {sidecar_candidates}"
    )


def test_piece_c_git_log_returns_empty_string_directly_on_zero_commits(tmp_path, engram_home):
    """Unit-level companion to the integration test above, directly on
    piece_c_git_log() — matches today's `""`-return behavior for the
    zero-commits case (spec's explicit "keep that behavior" instruction)."""
    fixture_repo = tmp_path / "fixture-repo-empty-unit"
    _make_empty_fixture_repo(fixture_repo)
    mod = _load_hook_module(engram_home, piece_c_repos=str(fixture_repo))

    assert mod.piece_c_git_log("some-session-id") == ""
    sessions_dir = engram_home / "sessions"
    assert list(sessions_dir.glob("*-piece-c.txt")) == []


# ── 3. Substrate-health-early test ─────────────────────────────────────────


def test_substrate_health_warning_renders_within_head_preview(tmp_path, engram_home):
    mod = _load_hook_module(engram_home)
    mod._check_mcp_health = lambda engram_home: (False, "forced-unhealthy for test")

    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        result = _run_hook_main(
            mod,
            {
                "session_id": "substrate-health-early",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            },
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]

    warning_at = ctx.find("⚠️  ENGRAM substrate health:")
    assert warning_at != -1, f"substrate-health warning block missing entirely: {ctx!r}"
    assert warning_at < _HEAD_PREVIEW_CHARS, (
        f"substrate-health warning must render within the first "
        f"{_HEAD_PREVIEW_CHARS} characters (#2859 — it used to run dead-last "
        f"in main()); found at {warning_at}. ctx={ctx!r}"
    )
    assert "MCP server: OFFLINE (forced-unhealthy for test)" in ctx
    assert "Tell the user immediately" in ctx


def test_substrate_health_warning_precedes_startup_only_blocks(tmp_path, engram_home):
    """The substrate-health block fires BEFORE the source=='startup'-only
    group (sleep status / fairy policy / .../ Piece C), matching item 3's
    position ahead of item 4 in the spec's ordered list."""
    mod = _load_hook_module(engram_home)
    mod._check_mcp_health = lambda engram_home: (False, "forced-unhealthy for test")

    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        result = _run_hook_main(
            mod,
            {
                "session_id": "substrate-health-order",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "startup",
            },
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]

    warning_at = ctx.find("⚠️  ENGRAM substrate health:")
    fairy_at = ctx.find("[Coder fairy policy:")
    assert warning_at != -1 and fairy_at != -1, f"missing block(s) entirely: {ctx!r}"
    assert warning_at < fairy_at, (
        f"substrate-health block must precede the startup-only cheap-block "
        f"group (item 3 before item 4); warning at {warning_at}, fairy "
        f"policy at {fairy_at}."
    )


def test_substrate_health_is_unconditional_across_sources(tmp_path, engram_home):
    """The move preserves the pre-existing behavior that substrate health
    fires for every `source`, not just startup (it sits outside the
    source=='startup' branch both before and after this PR)."""
    mod = _load_hook_module(engram_home)
    mod._check_mcp_health = lambda engram_home: (False, "forced-unhealthy for test")

    mock_client_class = MagicMock(return_value=_make_mock_engram_client())
    with patch.dict("sys.modules", {"engram_client": MagicMock(EngramClient=mock_client_class)}):
        result = _run_hook_main(
            mod,
            {
                "session_id": "substrate-health-compact",
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "source": "compact",
            },
        )
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "⚠️  ENGRAM substrate health:" in ctx
    assert "MCP server: OFFLINE (forced-unhealthy for test)" in ctx


# ── 4. Sidecar-write-failure fallback ──────────────────────────────────────


def test_piece_c_sidecar_write_failure_degrades_to_empty_string(tmp_path, engram_home):
    fixture_repo = tmp_path / "fixture-repo-write-fail"
    _make_fixture_repo(fixture_repo, 3)
    mod = _load_hook_module(engram_home, piece_c_repos=str(fixture_repo))

    # Mock the file write itself (spec's own phrasing) to raise, e.g. a
    # permission error — os.makedirs succeeds (sessions/ already exists),
    # but the open(..., "w") call that writes the sidecar body fails.
    mod.open = MagicMock(side_effect=PermissionError("disk full"))
    try:
        result = mod.piece_c_git_log("write-fail-session")
    finally:
        del mod.open  # don't leak the module-level override past this test

    assert result == "", (
        f"a sidecar-write failure must degrade piece_c_git_log() to '' "
        f"(no Piece C content, never a broken pointer); got: {result!r}"
    )
    sessions_dir = engram_home / "sessions"
    assert list(sessions_dir.glob("*-piece-c.txt")) == [], (
        "no partial/broken sidecar file should be left behind on write failure"
    )


def test_piece_c_sidecar_write_failure_never_raises(tmp_path, engram_home):
    """Escape-valve discipline: a sidecar-write failure must never propagate
    as an exception out of piece_c_git_log() (which would crash main())."""
    fixture_repo = tmp_path / "fixture-repo-write-fail-2"
    _make_fixture_repo(fixture_repo, 3)
    mod = _load_hook_module(engram_home, piece_c_repos=str(fixture_repo))

    mod.open = MagicMock(side_effect=OSError("simulated disk full"))
    try:
        # Must not raise.
        result = mod.piece_c_git_log("write-fail-session-2")
    finally:
        del mod.open

    assert result == ""
