"""Contract-first tests for the engram-sleep SKILL.md cohort-boundary snippet (#3123).

`engram-sleep/SKILL.md` computes `PREV_SLEEP` — the previous sleep's cohort
boundary — from `~/.engram/sessions/last-sleep-success.json`. The marker's
only writer (`tools/write_sleep_marker.py`) never writes a `cohort_end_at`
key; it writes `completed_at` (among others). Before this fix, the snippet
read the wrong key, the ensuing `KeyError` was swallowed by `2>/dev/null ||
echo ""`, and `PREV_SLEEP` silently became an empty string — which then
matches every node ever created in the `created_at >= PREV_SLEEP` filter.

Two things are tested:

1. A structural doc-drift guard (`test_boundary_snippet_reads_completed_at`)
   — cheap, catches a future accidental reintroduction of `cohort_end_at`
   anywhere in the file.
2. An executable behavior test (`TestBoundarySnippetExecution`) — extracts
   the ACTUAL snippet text from SKILL.md at test time (never hand-copied,
   so the test can't silently drift from the shipped doc) and runs it via
   `bash -c` against fixture marker files in a temp $HOME, covering:
   - C1: well-formed marker → PREV_SLEEP resolves to the exact timestamp.
   - C2: marker EXISTS but is missing `completed_at` (simulated corruption
     / old format) → must fail visibly, never silently-empty-exit-0.
   - C3: no marker file at all (legitimate first-ever-sleep case per
     write_sleep_marker.py's own docstring) → PREV_SLEEP resolves to
     empty, via a DIFFERENT code path than C2's loud failure.

Pre-fix baseline (recorded, not asserted, since this file ships only once
the fix has landed): running these three against the original
`cohort_end_at`-reading snippet gives C1 RED (empty stdout instead of the
timestamp — this is the #3123 bug, captured as a failing case before the
fix), C2 RED (the original snippet ALSO collapses "missing key" into
silent-empty-exit-0, since `cohort_end_at` is absent from the C2 fixture
too — proving the discrimination control actually discriminates), and C3
green (the original snippet already handled "no marker file" correctly,
by accident of the shared `2>/dev/null || echo ""` fallback covering both
missing-file and missing-key cases identically).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest


SKILL = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "engram"
    / "skills"
    / "claude"
    / "engram-sleep"
    / "SKILL.md"
)

# Every shipped `.md` under src/engram/ — the structural doc-drift guard below
# scans all of them, not just SKILL.md, so the wrong `cohort_end_at` key name
# (the same root cause as #3123: one boundary concept, two names) can't
# silently reappear in ANY shipped doc, including siblings like
# engram-dream-master.md that merely describe the value in prose.
SRC_ENGRAM_ROOT = Path(__file__).resolve().parent.parent / "src" / "engram"

# Matches every ```bash ... ``` fenced block in the doc, non-greedy, so each
# block can be inspected independently for the two anchor substrings below.
_BASH_FENCE_RE = re.compile(r"```bash\n(.*?)\n```", re.DOTALL)


def _extract_boundary_snippet(text: str) -> str:
    """Extract the PREV_SLEEP boundary-extraction bash snippet from the
    SKILL.md text — the actual shipped snippet, not a hand-copied
    reconstruction, so this test cannot silently drift from the doc.

    Locates it by scanning every fenced ```bash block for the one that
    references both the marker filename and the PREV_SLEEP variable — the
    two substrings that must survive any future edit to this section for
    the skill to still work at all.
    """
    for match in _BASH_FENCE_RE.finditer(text):
        block = match.group(1)
        if "last-sleep-success.json" in block and "PREV_SLEEP" in block:
            return block
    raise AssertionError(
        "could not locate the PREV_SLEEP boundary-extraction snippet in "
        f"{SKILL} — has this section moved or been rewritten?"
    )


def _run_snippet(snippet: str, home: Path) -> subprocess.CompletedProcess:
    """Execute the extracted snippet under bash, with $HOME pointed at a
    fixture directory, and echo $PREV_SLEEP afterward so its resolved value
    is observable on stdout."""
    script = snippet + '\necho "$PREV_SLEEP"\n'
    env = dict(os.environ)
    env["HOME"] = str(home)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _write_marker(home: Path, payload: dict) -> None:
    sessions_dir = home / ".engram" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    with open(sessions_dir / "last-sleep-success.json", "w") as f:
        json.dump(payload, f)


# ---------------------------------------------------------------------------
# 1. Structural doc-drift guard
# ---------------------------------------------------------------------------

def test_boundary_snippet_reads_completed_at_not_cohort_end_at():
    """The boundary-extraction snippet must read `completed_at` (the key
    tools/write_sleep_marker.py actually writes), and `cohort_end_at` (a
    key that is never read or named in any shipped file) must not appear
    anywhere in ANY shipped `.md` file under `src/engram/` — not just
    SKILL.md, and not just at the one known line — so a future accidental
    reintroduction anywhere in the shipped docs is also caught."""
    for md_file in sorted(SRC_ENGRAM_ROOT.rglob("*.md")):
        md_text = md_file.read_text(encoding="utf-8")
        assert "cohort_end_at" not in md_text, (
            f"stale key name 'cohort_end_at' reappeared in {md_file}"
        )

    text = SKILL.read_text(encoding="utf-8")
    snippet = _extract_boundary_snippet(text)
    assert "completed_at" in snippet


# ---------------------------------------------------------------------------
# 2. Executable behavior tests
# ---------------------------------------------------------------------------

class TestBoundarySnippetExecution:
    def test_c1_well_formed_marker_resolves_to_completed_at(self, tmp_path):
        """C1: a well-formed marker with `completed_at` set → PREV_SLEEP
        resolves to that exact timestamp."""
        home = tmp_path / "home"
        home.mkdir()
        timestamp = "2026-01-15T03:22:41.123456+00:00"
        _write_marker(
            home,
            {
                "completed_at": timestamp,
                "turn_advanced_to": 42,
                "nodes_consolidated": 7,
                "cohort_start_at": "2026-01-14T00:00:00+00:00",
            },
        )

        text = SKILL.read_text(encoding="utf-8")
        snippet = _extract_boundary_snippet(text)
        result = _run_snippet(snippet, home)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == timestamp

    def test_c2_marker_exists_but_missing_completed_at_fails_visibly(self, tmp_path):
        """C2 (discrimination control): a marker file EXISTS but does NOT
        have a `completed_at` key (simulating corruption / an old marker
        format predating this field). The snippet must NOT silently
        produce empty output and exit 0 — it must fail visibly."""
        home = tmp_path / "home"
        home.mkdir()
        _write_marker(
            home,
            {
                # Deliberately missing `completed_at` — an old-format or
                # corrupted marker.
                "turn_advanced_to": 42,
                "nodes_consolidated": 7,
                "cohort_start_at": "2026-01-14T00:00:00+00:00",
            },
        )

        text = SKILL.read_text(encoding="utf-8")
        snippet = _extract_boundary_snippet(text)
        result = _run_snippet(snippet, home)

        # The failure must be VISIBLE: a non-zero exit code. (Silent
        # empty-output-with-exit-0 is exactly the #3123 bug shape.)
        assert result.returncode != 0, (
            "snippet must fail loudly on a marker missing 'completed_at', "
            f"got returncode=0, stdout={result.stdout!r}"
        )

    def test_c3_no_marker_file_resolves_to_empty_via_different_path(self, tmp_path):
        """C3 (legitimate empty case, regression check): no marker file at
        all — the documented first-ever-sleep case
        (write_sleep_marker.py's own docstring: 'first node in cohort if
        no prior sleep'). PREV_SLEEP must resolve to empty, and this must
        be a DIFFERENT outcome shape than C2's loud failure (exit 0, no
        stderr), not the same fallback silently reused for both."""
        home = tmp_path / "home"
        home.mkdir()
        # No .engram directory at all — the freshest possible "no prior
        # sleep" fixture.

        text = SKILL.read_text(encoding="utf-8")
        snippet = _extract_boundary_snippet(text)
        result = _run_snippet(snippet, home)

        assert result.returncode == 0, (
            "no-marker-file is the legitimate empty case and must exit "
            f"cleanly, got returncode={result.returncode}, stderr={result.stderr!r}"
        )
        assert result.stdout.strip() == ""
