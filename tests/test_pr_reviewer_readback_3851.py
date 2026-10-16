"""#3851 / #3853: pin the reviewer spec's gated post-and-read-back block.

The spec has ONE bash block under "## Post, then read back": it gates
``gh pr comment`` on the body file's first line, then reads the posted comment
back. The block is extracted from the agent spec itself (``expected=`` through
the line that is exactly ``fi``) and run end to end against fixtures. No
network: a stub ``gh`` is first on PATH and records every call. Set
ENGRAM_REVIEWER_SPEC_PATH to point at a scratch copy (used for mutation
checks). Set ENGRAM_TEST_BASH to choose the shell (default: whatever ``bash``
is on PATH; use /bin/bash for stock macOS 3.2).
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

HEADING = "## Post, then read back"


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


def _extract_block() -> str:
    sec = _section(SPEC.read_text(encoding="utf-8"), HEADING)
    out, on = [], False
    for l in sec:
        if not on and l.startswith("expected="):
            on = True
        if on:
            out.append(l)
            if l == "fi":
                break
    return "\n".join(out) + ("\n" if out else "")


BLOCK = _extract_block()

PR, ROUND, SHA = "3850", "2", "e136f522"
GOOD_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round {ROUND} · head {SHA}"
STALE_FIRST = "🧚 engram-pr-reviewer · round 2 · head adb4d40f"
WRONG_ROUND_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round 1 · head {SHA}"
WRONG_HEAD_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round {ROUND} · head adb4d40f"
GOOD_URL = f"https://github.com/o/r/pull/{PR}#issuecomment-123"

STUB_GH = """#!/bin/sh
# stub gh: `pr comment` is logged to $STUB_PR_LOG and prints $STUB_PR_OUT;
# `api <path>` logs the last path component to $STUB_API_LOG and prints
# $STUB_API_JSON.
case "$1" in
  pr) echo "$*" >> "$STUB_PR_LOG"; printf '%s\\n' "$STUB_PR_OUT" ;;
  api) printf '%s\\n' "${2##*/}" >> "$STUB_API_LOG"; printf '%s' "$STUB_API_JSON" ;;
esac
"""


def _body(first: str, verdict: bool = True) -> str:
    body = first + "\n\n## Summary\nx\n"
    if verdict:
        body += "\n## Verdict\nAPPROVE\n"
    return body


def _comment(first: str, pr: str = PR, verdict: bool = True) -> str:
    return json.dumps(
        {
            "issue_url": f"https://api.github.com/repos/o/r/issues/{pr}",
            "body": _body(first, verdict),
        }
    )


def _run(tmp_path, body_text: str, url: str, posted_json: str, block: str = None):
    """Source the block with body file B holding body_text.

    Returns (stdout, number of `pr comment` calls, ids `api` was asked for).
    """
    block = BLOCK if block is None else block
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(STUB_GH, encoding="utf-8")
    gh.chmod(0o755)
    api_log = tmp_path / "api.log"
    api_log.write_text("", encoding="utf-8")
    pr_log = tmp_path / "pr.log"
    pr_log.write_text("", encoding="utf-8")
    b = tmp_path / "body.md"
    b.write_text(body_text, encoding="utf-8")
    script = f'PR={PR}; REPO=o/r; ROUND={ROUND}; SHA={SHA}; B="$BODYFILE"\n' + block
    env = dict(
        os.environ,
        PATH=f"{bindir}:{os.environ['PATH']}",
        BODYFILE=str(b),
        STUB_PR_OUT=url,
        STUB_API_JSON=posted_json,
        STUB_API_LOG=str(api_log),
        STUB_PR_LOG=str(pr_log),
    )
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)
    calls = len(pr_log.read_text(encoding="utf-8").splitlines())
    return r.stdout, calls, api_log.read_text(encoding="utf-8").split()


def test_S1_extraction_guard():
    assert BLOCK.strip(), "block not extracted"
    lines = BLOCK.splitlines()
    assert lines[0].startswith("expected=")
    assert lines[-1] == "fi"
    for needle in (
        "NOT POSTED",
        "gh pr comment",
        'case "$cid"',
        "posted_pr=",
        "READ-BACK OK",
        "READ-BACK MISMATCH",
        "grep -q '^## Verdict' \"$B\"",
    ):
        assert needle in BLOCK, needle
    headings = [
        l for l in SPEC.read_text(encoding="utf-8").splitlines() if l.startswith(HEADING)
    ]
    assert len(headings) == 1, headings


def test_R1_good(tmp_path):
    out, calls, ids = _run(tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK OK")
    assert calls == 1
    assert ids == ["123"]


def test_R2_stale_body_not_posted(tmp_path):
    out, calls, ids = _run(tmp_path, _body(STALE_FIRST), GOOD_URL, _comment(GOOD_FIRST))
    assert "NOT POSTED" in out
    assert GOOD_FIRST in out
    assert STALE_FIRST in out
    assert calls == 0
    assert ids == []


def test_R3_wrong_pr(tmp_path):
    out, _, _ = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST, pr="3846")
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert "PR #3846" in out


def test_R4_truncated(tmp_path):
    out, _, _ = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST, verdict=False)
    )
    assert out.startswith("READ-BACK MISMATCH")


def test_R5_empty_fetch(tmp_path):
    out, _, _ = _run(tmp_path, _body(GOOD_FIRST), GOOD_URL, "")
    assert "READ-BACK MISMATCH" in out
    assert "READ-BACK OK" not in out


def test_R6_same_pr_wrong_round(tmp_path):
    out, _, _ = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(WRONG_ROUND_FIRST)
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert WRONG_ROUND_FIRST in out


def test_R7_same_pr_and_round_wrong_head(tmp_path):
    out, _, _ = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(WRONG_HEAD_FIRST)
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert WRONG_HEAD_FIRST in out


def test_R8_fetches_the_id_gh_printed(tmp_path):
    out, _, ids = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL.replace("-123", "-98765"), _comment(GOOD_FIRST)
    )
    assert out.startswith("READ-BACK OK")
    assert ids == ["98765"]


def test_R9_gh_printed_nothing(tmp_path):
    out, _, ids = _run(tmp_path, _body(GOOD_FIRST), "", _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK MISMATCH")
    assert "gh printed []" in out
    assert ids == []


def test_R10_gh_printed_non_url(tmp_path):
    out, _, ids = _run(
        tmp_path, _body(GOOD_FIRST), "error: could not post", _comment(GOOD_FIRST)
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert ids == []


def test_R11_empty_body_file_not_posted(tmp_path):
    out, calls, ids = _run(tmp_path, "", GOOD_URL, _comment(GOOD_FIRST))
    assert "NOT POSTED" in out
    assert calls == 0
    assert ids == []


def test_R13_no_verdict_section_not_posted(tmp_path):
    out, calls, ids = _run(
        tmp_path, _body(GOOD_FIRST, verdict=False), GOOD_URL, _comment(GOOD_FIRST)
    )
    assert "NOT POSTED" in out
    assert "## Verdict" in out
    assert calls == 0
    assert ids == []


def test_R14_verdict_in_prose_only_not_posted(tmp_path):
    # Pins the `^` anchor: "Verdict" appears in prose, but no line starts
    # with "## Verdict" (heading lost to truncation) -> must not post.
    body = GOOD_FIRST + "\n\n## Summary\nVerdict follows below.\n"
    out, calls, ids = _run(tmp_path, body, GOOD_URL, _comment(GOOD_FIRST))
    assert "NOT POSTED" in out
    assert calls == 0
    assert ids == []


def _with_brief_line(line: str) -> str:
    """Simulate a fairy copying the brief's exact line into `expected`."""
    rest = BLOCK.split("\n", 1)[1]
    return f"expected='{line}'\n" + rest


def test_R12_brief_supplied_line(tmp_path):
    block = _with_brief_line(GOOD_FIRST)
    out, calls, _ = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), block=block
    )
    assert out.startswith("READ-BACK OK")
    assert calls == 1


def test_R12b_brief_line_one_char_off_not_posted(tmp_path):
    block = _with_brief_line(GOOD_FIRST)
    off_by_one = GOOD_FIRST[:-1] + "3"  # last char of the SHA differs
    assert off_by_one != GOOD_FIRST
    out, calls, ids = _run(
        tmp_path, _body(off_by_one), GOOD_URL, _comment(off_by_one), block=block
    )
    assert "NOT POSTED" in out
    assert calls == 0
    assert ids == []
