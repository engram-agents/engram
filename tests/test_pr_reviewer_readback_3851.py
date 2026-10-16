"""#3851: pin the reviewer spec's body-file check and post read-back.

The two bash snippets are extracted from the agent spec itself (anchored on
subsection headings and start/end lines) and run against fixtures. No network:
the read-back block runs end to end (``url=`` through ``fi``) with a stub
``gh`` first on PATH. Set ENGRAM_REVIEWER_SPEC_PATH to point at a scratch copy
(used for mutation checks). Set ENGRAM_TEST_BASH to choose the shell
(default: whatever ``bash`` is on PATH; use /bin/bash for stock macOS 3.2).
"""
import json
import os
import subprocess
from pathlib import Path

DEFAULT_SPEC = (
    Path(__file__).resolve().parent.parent
    / "src" / "engram" / "agents" / "claude" / "engram-pr-reviewer.md"
)
SPEC = Path(os.environ.get("ENGRAM_REVIEWER_SPEC_PATH", DEFAULT_SPEC))
BASH = os.environ.get("ENGRAM_TEST_BASH", "bash")

BODY_HEADING = "## Body files"
READBACK_HEADING = "## Read back what you posted"


def _section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith(heading)), None)
    if start is None:
        return []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") or lines[j].startswith("# "):
            end = j
            break
    return lines[start:end]


def _extract_readback() -> str:
    sec = _section(SPEC.read_text(encoding="utf-8"), READBACK_HEADING)
    out, on = [], False
    for l in sec:
        if not on and l.startswith("url="):
            on = True
        if on:
            out.append(l)
            if l == "fi":
                break
    return "\n".join(out) + ("\n" if out else "")


def _extract_prepost() -> str:
    sec = _section(SPEC.read_text(encoding="utf-8"), BODY_HEADING)
    out, on = [], False
    for l in sec:
        if not on and l.startswith("  expected="):
            on = True
        if on:
            out.append(l[2:] if l.startswith("  ") else l)
            if l.startswith('  [ "$(head -1'):
                break
    return "\n".join(out) + ("\n" if out else "")


READBACK = _extract_readback()
PREPOST = _extract_prepost()

PR, ROUND, SHA = "3850", "2", "e136f522"
GOOD_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round {ROUND} · head {SHA}"
STALE_FIRST = "🧚 engram-pr-reviewer · round 2 · head adb4d40f"
WRONG_ROUND_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round 1 · head {SHA}"
WRONG_HEAD_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round {ROUND} · head adb4d40f"
GOOD_URL = f"https://github.com/o/r/pull/{PR}#issuecomment-123"

STUB_GH = """#!/bin/sh
# stub gh: `pr comment` prints $STUB_PR_OUT; `api <path>` records the last
# path component to $STUB_API_LOG and prints $STUB_API_JSON.
case "$1" in
  pr) printf '%s\\n' "$STUB_PR_OUT" ;;
  api) printf '%s\\n' "${2##*/}" >> "$STUB_API_LOG"; printf '%s' "$STUB_API_JSON" ;;
esac
"""


def _run_readback(tmp_path, url: str, posted_json: str):
    """Run the whole read-back block with a stub gh first on PATH.

    Returns (stdout, ids the stub's `api` was asked for).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(STUB_GH, encoding="utf-8")
    gh.chmod(0o755)
    log = tmp_path / "api.log"
    log.write_text("", encoding="utf-8")
    script = f"PR={PR}; REPO=o/r; ROUND={ROUND}; SHA={SHA}; B=/dev/null\n" + READBACK
    env = dict(
        os.environ,
        PATH=f"{bindir}:{os.environ['PATH']}",
        STUB_PR_OUT=url,
        STUB_API_JSON=posted_json,
        STUB_API_LOG=str(log),
    )
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)
    return r.stdout, log.read_text(encoding="utf-8").split()


def _comment(first: str, pr: str = PR, verdict: bool = True) -> str:
    body = first + "\n\n## Summary\nx\n"
    if verdict:
        body += "\n## Verdict\nAPPROVE\n"
    return json.dumps(
        {"issue_url": f"https://api.github.com/repos/o/r/issues/{pr}", "body": body}
    )


def test_S1_extraction_guard():
    assert READBACK.strip(), "read-back block not extracted"
    for needle in ("url=", "cid=", 'case "$cid"', "posted_pr="):
        assert needle in READBACK, needle
    lines = READBACK.splitlines()
    assert lines[0].startswith("url=")
    assert lines[-1] == "fi"
    assert "expected=" in PREPOST


def test_R1_good(tmp_path):
    out, _ = _run_readback(tmp_path, GOOD_URL, _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK OK")


def test_R2_stale_first_line(tmp_path):
    out, _ = _run_readback(tmp_path, GOOD_URL, _comment(STALE_FIRST))
    assert out.startswith("READ-BACK MISMATCH")
    assert GOOD_FIRST in out
    assert STALE_FIRST in out


def test_R3_wrong_pr(tmp_path):
    out, _ = _run_readback(tmp_path, GOOD_URL, _comment(GOOD_FIRST, pr="3846"))
    assert out.startswith("READ-BACK MISMATCH")
    assert "PR #3846" in out


def test_R4_truncated(tmp_path):
    out, _ = _run_readback(tmp_path, GOOD_URL, _comment(GOOD_FIRST, verdict=False))
    assert out.startswith("READ-BACK MISMATCH")


def test_R5_empty_fetch(tmp_path):
    out, _ = _run_readback(tmp_path, GOOD_URL, "")
    assert "READ-BACK MISMATCH" in out
    assert "READ-BACK OK" not in out


def test_R6_same_pr_wrong_round(tmp_path):
    out, _ = _run_readback(tmp_path, GOOD_URL, _comment(WRONG_ROUND_FIRST))
    assert out.startswith("READ-BACK MISMATCH")
    assert WRONG_ROUND_FIRST in out


def test_R7_same_pr_and_round_wrong_head(tmp_path):
    out, _ = _run_readback(tmp_path, GOOD_URL, _comment(WRONG_HEAD_FIRST))
    assert out.startswith("READ-BACK MISMATCH")
    assert WRONG_HEAD_FIRST in out


def test_R8_fetches_the_id_gh_printed(tmp_path):
    out, ids = _run_readback(tmp_path, GOOD_URL, _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK OK")
    assert ids == ["123"]


def test_R9_gh_printed_nothing(tmp_path):
    out, ids = _run_readback(tmp_path, "", _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK MISMATCH")
    assert "gh printed []" in out
    assert ids == []


def test_R10_gh_printed_non_url(tmp_path):
    out, ids = _run_readback(tmp_path, "error: could not post", _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK MISMATCH")
    assert ids == []


def _run_prepost(tmp_path, first: str) -> str:
    b = tmp_path / "body.md"
    b.write_text(first + "\n\n## Verdict\nAPPROVE\n", encoding="utf-8")
    script = f'PR={PR}; ROUND={ROUND}; SHA={SHA}; B="$BODYFILE"\n' + PREPOST
    env = dict(os.environ, BODYFILE=str(b))
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)
    return r.stdout


def test_P1_prepost_stale(tmp_path):
    out = _run_prepost(tmp_path, STALE_FIRST)
    assert "NOT POSTED" in out
    assert STALE_FIRST in out
    assert GOOD_FIRST in out


def test_P2_prepost_good(tmp_path):
    assert _run_prepost(tmp_path, GOOD_FIRST) == ""
