"""#3851 / #3853: pin the reviewer's gated post-and-read-back helper.

The reviewer spec's "## Post, then read back" section holds ONE literal helper
call (``python3 ~/.engram/marketplace/plugins/engram/tools/fairy_post_review.py
--repo ... --pr ... --expected ... --body-file ...``); the gate itself (first
line, ``## Verdict`` section, one ``gh pr comment``, read-back by the printed
comment id) lives in ``src/engram/tools/fairy_post_review.py``. This file pins
(a) the spec section's shape (one heading, one literal helper line, no ``$``),
(b) the helper's behaviour, run end to end as a subprocess against fixtures
with a stub ``gh`` passed via ``--gh`` (no network; every call is logged,
including full argv), including the exit codes, and (c) that the helper is
stdlib-only and parses under the Python 3.9 grammar. Set
ENGRAM_REVIEWER_SPEC_PATH to point at a scratch copy of the spec, and
ENGRAM_FAIRY_POST_REVIEW_PATH at a scratch copy of the helper (both used for
mutation checks).
"""
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPEC = ROOT / "src" / "engram" / "agents" / "claude" / "engram-pr-reviewer.md"
SPEC = Path(os.environ.get("ENGRAM_REVIEWER_SPEC_PATH", DEFAULT_SPEC))
HELPER = Path(
    os.environ.get(
        "ENGRAM_FAIRY_POST_REVIEW_PATH",
        ROOT / "src" / "engram" / "tools" / "fairy_post_review.py",
    )
)

HEADING = "## Post, then read back"
HELPER_PREFIX = "python3 <helper path> "


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


PR, ROUND, SHA = "3850", "2", "e136f522"
GOOD_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round {ROUND} · head {SHA}"
STALE_FIRST = "🧚 engram-pr-reviewer · round 2 · head adb4d40f"
WRONG_ROUND_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round 1 · head {SHA}"
WRONG_HEAD_FIRST = f"🧚 engram-pr-reviewer · PR #{PR} · round {ROUND} · head adb4d40f"
GOOD_URL = f"https://github.com/o/r/pull/{PR}#issuecomment-123"

STUB_GH = """#!/bin/sh
# stub gh: every call's full argv goes to $STUB_ALL_LOG. `pr comment` is also
# logged to $STUB_PR_LOG, prints $STUB_PR_OUT and exits $STUB_PR_RC (default 0);
# `api <path>` logs the last path component to $STUB_API_LOG and prints
# $STUB_API_JSON.
echo "$*" >> "$STUB_ALL_LOG"
case "$1" in
  pr) echo "$*" >> "$STUB_PR_LOG"; [ -n "$STUB_PR_SLEEP" ] && exec sleep "$STUB_PR_SLEEP"; printf '%s\\n' "$STUB_PR_OUT"; exit "${STUB_PR_RC:-0}" ;;
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


# (cell label, stub-gh argv log TEXT) for every cell in this module (R20). Text,
# not paths: under `tmp_path_retention_policy=failed` a passing test's tmp dir is
# deleted at teardown, so a path kept for a later cell would be gone (#4392).
ALL_LOG_TEXTS = []


def _run(
    tmp_path,
    body_text,
    url,
    posted_json,
    expected=GOOD_FIRST,
    pr=PR,
    repo="o/r",
    pr_rc=0,
    body_path=None,
    env_extra=None,
    extra_args=None,
):
    """Run the helper with body file holding body_text (or at body_path).

    Returns (stdout, number of `pr comment` calls, ids `api` was asked for,
    exit code).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(STUB_GH, encoding="utf-8")
    gh.chmod(0o755)
    logs = {n: tmp_path / (n + ".log") for n in ("api", "pr", "all")}
    for lg in logs.values():
        lg.write_text("", encoding="utf-8")
    if body_path is None:
        b = tmp_path / "body.md"
        b.write_text(body_text, encoding="utf-8")
    else:
        b = body_path
    env = dict(
        os.environ,
        STUB_PR_OUT=url,
        STUB_PR_RC=str(pr_rc),
        STUB_API_JSON=posted_json,
        STUB_API_LOG=str(logs["api"]),
        STUB_PR_LOG=str(logs["pr"]),
        STUB_ALL_LOG=str(logs["all"]),
    )
    env.update(env_extra or {})
    cmd = [
        sys.executable, str(HELPER),
        "--repo", repo, "--pr", str(pr), "--expected", expected,
        "--body-file", str(b), "--gh", str(gh),
    ] + list(extra_args or [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    finally:
        # After the helper has run, so the capture holds what it logged; in a
        # finally so a cell whose helper call raises is still swept. Plain
        # read_text: the file was created above, so a missing one must raise.
        ALL_LOG_TEXTS.append(
            (f"{tmp_path.parent.name}/{tmp_path.name}", logs["all"].read_text(encoding="utf-8"))
        )
    calls = len(logs["pr"].read_text(encoding="utf-8").splitlines())
    return (
        r.stdout,
        calls,
        logs["api"].read_text(encoding="utf-8").split(),
        r.returncode,
    )


def _all_calls(tmp_path):
    return len((tmp_path / "all.log").read_text(encoding="utf-8").splitlines())


def test_S1_extraction_guard():
    text = SPEC.read_text(encoding="utf-8")
    headings = [l for l in text.splitlines() if l.startswith(HEADING)]
    assert len(headings) == 1, headings
    sec = _section(text, HEADING)
    helper_lines = [l for l in sec if l.startswith(HELPER_PREFIX)]
    assert len(helper_lines) == 1, helper_lines
    line = helper_lines[0]
    assert "$" not in line, line
    for needle in ("--repo", "--pr", "--expected", "--body-file"):
        assert needle in line, needle
    # the default path is stated on one line that names ENGRAM_HOME (home-path gate)
    defaults = [l for l in sec if "ENGRAM_HOME" in l and "fairy_post_review.py" in l]
    assert defaults, "no line names ENGRAM_HOME together with fairy_post_review.py"


def test_R1_good(tmp_path):
    out, calls, ids, rc = _run(tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK OK")
    assert calls == 1
    assert ids == ["123"]
    assert rc == 0
    assert len(out.splitlines()) == 1


def test_R2_stale_body_not_posted(tmp_path):
    out, calls, ids, rc = _run(tmp_path, _body(STALE_FIRST), GOOD_URL, _comment(GOOD_FIRST))
    assert "NOT POSTED" in out
    assert GOOD_FIRST in out
    assert STALE_FIRST in out
    assert calls == 0
    assert ids == []
    assert rc == 3
    assert _all_calls(tmp_path) == 0


def test_R3_wrong_pr(tmp_path):
    out, _, _, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST, pr="3846")
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert "PR #3846" in out
    assert rc == 5


def test_R4_truncated(tmp_path):
    out, _, _, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST, verdict=False)
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert rc == 5


def test_R5_empty_fetch(tmp_path):
    out, _, _, rc = _run(tmp_path, _body(GOOD_FIRST), GOOD_URL, "")
    assert "READ-BACK MISMATCH" in out
    assert "READ-BACK OK" not in out
    assert rc == 5


def test_R6_same_pr_wrong_round(tmp_path):
    out, _, _, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(WRONG_ROUND_FIRST)
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert WRONG_ROUND_FIRST in out
    assert rc == 5


def test_R7_same_pr_and_round_wrong_head(tmp_path):
    out, _, _, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(WRONG_HEAD_FIRST)
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert WRONG_HEAD_FIRST in out
    assert rc == 5


def test_R8_fetches_the_id_gh_printed(tmp_path):
    out, _, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL.replace("-123", "-98765"), _comment(GOOD_FIRST)
    )
    assert out.startswith("READ-BACK OK")
    assert ids == ["98765"]
    assert rc == 0


def test_R9_gh_printed_nothing(tmp_path):
    out, _, ids, rc = _run(tmp_path, _body(GOOD_FIRST), "", _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK MISMATCH")
    assert "gh printed []" in out
    assert ids == []
    assert rc == 4


def test_R10_gh_printed_non_url(tmp_path):
    out, _, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), "error: could not post", _comment(GOOD_FIRST)
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert ids == []
    assert rc == 4


def test_R11_empty_body_file_not_posted(tmp_path):
    out, calls, ids, rc = _run(tmp_path, "", GOOD_URL, _comment(GOOD_FIRST))
    assert "NOT POSTED" in out
    assert calls == 0
    assert ids == []
    assert rc == 3


def test_R13_no_verdict_section_not_posted(tmp_path):
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST, verdict=False), GOOD_URL, _comment(GOOD_FIRST)
    )
    assert "NOT POSTED" in out
    assert "## Verdict" in out
    assert calls == 0
    assert ids == []
    assert rc == 3


def test_R14_verdict_in_prose_only_not_posted(tmp_path):
    # Pins the line-start anchor: "Verdict" appears in prose, but no line starts
    # with "## Verdict" (heading lost to truncation) -> must not post.
    body = GOOD_FIRST + "\n\n## Summary\nVerdict follows below.\n"
    out, calls, ids, rc = _run(tmp_path, body, GOOD_URL, _comment(GOOD_FIRST))
    assert "NOT POSTED" in out
    assert calls == 0
    assert ids == []
    assert rc == 3


def test_R12_brief_supplied_line(tmp_path):
    # The brief supplies the exact line; the helper compares the file to it.
    out, calls, _, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), expected=GOOD_FIRST
    )
    assert out.startswith("READ-BACK OK")
    assert calls == 1
    assert rc == 0


def test_R12b_brief_line_one_char_off_not_posted(tmp_path):
    off_by_one = GOOD_FIRST[:-1] + "3"  # last char of the SHA differs
    assert off_by_one != GOOD_FIRST
    out, calls, ids, rc = _run(
        tmp_path, _body(off_by_one), GOOD_URL, _comment(off_by_one), expected=GOOD_FIRST
    )
    assert "NOT POSTED" in out
    assert calls == 0
    assert ids == []
    assert rc == 3


def test_R15_invalid_arguments(tmp_path):
    cases = [
        {"pr": "0"},
        {"pr": "abc"},
        {"repo": "o"},
        {"repo": "o/r/x"},
        {"repo": "../.."},
        {"repo": "./r"},
        {"repo": "o/.."},
        {"expected": ""},
    ]
    for i, kw in enumerate(cases):
        d = tmp_path / f"c{i}"
        d.mkdir()
        out, calls, ids, rc = _run(
            d, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), **kw
        )
        assert out.startswith("NOT POSTED: invalid arguments"), (kw, out)
        assert rc == 2, kw
        assert calls == 0 and ids == [], kw
        assert _all_calls(d) == 0, kw


def test_R15b_dotted_repo_components_still_valid(tmp_path):
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), repo="o.org/r.repo"
    )
    assert out.startswith("READ-BACK OK"), out
    assert calls == 1 and rc == 0


def test_R16_unreadable_body_file(tmp_path):
    out, calls, ids, rc = _run(
        tmp_path, "", GOOD_URL, _comment(GOOD_FIRST), body_path=tmp_path / "nope.md"
    )
    assert out.startswith("NOT POSTED: body file unreadable")
    assert rc == 2
    assert calls == 0 and ids == []
    assert _all_calls(tmp_path) == 0


def test_R17_gh_pr_comment_fails(tmp_path):
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), "boom: rate limited", _comment(GOOD_FIRST), pr_rc=1
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert "boom: rate limited" in out
    assert rc == 4
    assert calls == 1  # one attempt, no retry
    assert ids == []
    assert _all_calls(tmp_path) == 1


def test_R18_unparseable_json(tmp_path):
    out, calls, ids, rc = _run(tmp_path, _body(GOOD_FIRST), GOOD_URL, "{not json")
    assert out.startswith("READ-BACK MISMATCH")
    assert "READ-BACK OK" not in out
    assert rc == 5
    assert calls == 1 and ids == ["123"]


def test_R19_helper_is_py39_stdlib_only():
    src = HELPER.read_text(encoding="utf-8")
    tree = ast.parse(src, feature_version=(3, 9))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            mods.add((node.module or "").split(".")[0])
    assert mods, "no imports found"
    # Exact literal set: equality, so any new import fails loudly (this works on 3.9).
    assert mods == {"argparse", "json", "re", "subprocess", "sys"}, sorted(mods)
    if hasattr(sys, "stdlib_module_names"):  # 3.10+ only
        not_stdlib = sorted(m for m in mods if m not in sys.stdlib_module_names)
        assert not_stdlib == [], not_stdlib
    assert "engram" not in " ".join(mods)


def test_R20_never_edit_or_delete(tmp_path):
    # Two runs of its own (a clean post and a failed post), so it holds alone
    # or under xdist; when the whole module runs in one process it also sweeps
    # every earlier cell's stub-gh argv log. None may carry an edit/delete verb.
    for name, kw in (("ok", {}), ("bad", {"pr_rc": 1})):
        d = tmp_path / name
        d.mkdir()
        _run(d, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), **kw)
    assert len(ALL_LOG_TEXTS) >= 2
    seen = 0
    for label, text in ALL_LOG_TEXTS:
        for line in text.splitlines():
            seen += 1
            words = line.split()
            assert words[0] in ("pr", "api"), (label, line)
            assert "edit" not in words and "delete" not in words, (label, line)
            assert "DELETE" not in words and "PATCH" not in words, (label, line)
            if words[0] == "pr":
                assert words[1] == "comment", (label, line)
    assert seen >= 1


# --- round 1 of #4372: no leniency on "longer" values; one result line always ---


def test_R21_longer_first_line_not_posted(tmp_path):
    # Brief gave the short SHA, the body carries a longer one: a prefix match
    # would post it. Equality must refuse, with zero gh calls.
    longer = GOOD_FIRST + "abcdef0123"
    out, calls, ids, rc = _run(tmp_path, _body(longer), GOOD_URL, _comment(longer))
    assert "NOT POSTED" in out
    assert calls == 0 and ids == []
    assert rc == 3
    assert _all_calls(tmp_path) == 0


def test_R22_landed_first_line_longer_is_mismatch(tmp_path):
    longer = GOOD_FIRST + " and more"
    out, _, _, rc = _run(tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(longer))
    assert out.startswith("READ-BACK MISMATCH")
    assert "READ-BACK OK" not in out
    assert rc == 5


def test_R23_gh_nonzero_with_valid_url_is_failure(tmp_path):
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), pr_rc=1
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert "READ-BACK OK" not in out
    assert rc == 4
    assert calls == 1 and ids == []


def test_R24_landed_on_pr_with_same_trailing_digits(tmp_path):
    out, _, _, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST, pr="1" + PR)
    )
    assert out.startswith("READ-BACK MISMATCH")
    assert "READ-BACK OK" not in out
    assert rc == 5


def test_R25_verdict_mid_line_only_not_posted_and_read_back(tmp_path):
    # R14 covers prose that merely contains the word "Verdict"; this pins the
    # literal "## Verdict" appearing mid-line, on both the gate and read-back.
    mid = GOOD_FIRST + "\n\n## Summary\nsee ## Verdict below\n"
    out, calls, ids, rc = _run(tmp_path, mid, GOOD_URL, _comment(GOOD_FIRST))
    assert "NOT POSTED" in out
    assert calls == 0 and ids == []
    assert rc == 3
    landed = json.dumps(
        {"issue_url": f"https://api.github.com/repos/o/r/issues/{PR}", "body": mid}
    )
    d = tmp_path / "rb"
    d.mkdir()
    out, _, _, rc = _run(d, _body(GOOD_FIRST), GOOD_URL, landed)
    assert out.startswith("READ-BACK MISMATCH")
    assert rc == 5


def test_R26_last_comment_id_wins(tmp_path):
    url = "see https://github.com/o/r/pull/9#issuecomment-9 then " + GOOD_URL.replace("-123", "-55")
    out, _, ids, rc = _run(tmp_path, _body(GOOD_FIRST), url, _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK OK")
    assert ids == ["55"]
    assert rc == 0


def test_R27_ascii_stdout_still_prints_the_line(tmp_path):
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST),
        env_extra={"PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"},
    )
    assert out.startswith("READ-BACK OK") and GOOD_FIRST in out
    assert rc == 0 and calls == 1


def test_R28_non_utf8_gh_output_does_not_crash(tmp_path):
    url = os.fsdecode(b"\xff\xfe ") + GOOD_URL
    out, calls, ids, rc = _run(tmp_path, _body(GOOD_FIRST), url, _comment(GOOD_FIRST))
    assert out.startswith("READ-BACK OK"), out
    assert rc == 0 and calls == 1 and ids == ["123"]


def _shadow(tmp_path, name, src):
    d = tmp_path / "shadow"
    d.mkdir(exist_ok=True)
    (d / (name + ".py")).write_text(src, encoding="utf-8")
    return {"PYTHONPATH": str(d)}


def test_R29_unexpected_error_after_post_is_exit_6(tmp_path):
    env = _shadow(tmp_path, "json", "def loads(*a, **k):\n    raise RuntimeError('boom')\n")
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), env_extra=env
    )
    assert out.startswith("READ-BACK MISMATCH: helper error after the post was attempted")
    assert "RuntimeError: boom" in out
    assert len(out.splitlines()) == 1
    assert rc == 6
    assert calls == 1


def test_R30_unexpected_error_before_post_is_exit_2(tmp_path):
    env = _shadow(
        tmp_path, "argparse",
        "class ArgumentParser:\n    def __init__(self, *a, **k):\n        raise RuntimeError('boom')\n",
    )
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), env_extra=env
    )
    assert out.startswith("NOT POSTED: helper error: RuntimeError: boom")
    assert len(out.splitlines()) == 1
    assert rc == 2
    assert calls == 0 and _all_calls(tmp_path) == 0


def test_R31_multiline_gh_output_still_one_result_line(tmp_path):
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), "error line one\nerror line two\nerror line three",
        _comment(GOOD_FIRST), pr_rc=1,
    )
    assert rc == 4 and calls == 1 and ids == []
    assert out.startswith("READ-BACK MISMATCH")
    assert out.endswith("\n") and len(out.splitlines()) == 1, repr(out)
    assert "error line one error line two error line three" in out


def test_R32_newline_in_expected_is_refused(tmp_path):
    for i, exp in enumerate((GOOD_FIRST + "\nsecond", GOOD_FIRST + "\rsecond")):
        d = tmp_path / f"c{i}"
        d.mkdir()
        out, calls, ids, rc = _run(d, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), expected=exp)
        assert out == "NOT POSTED: invalid arguments: --expected must be a single line\n", repr(out)
        assert rc == 2 and calls == 0 and ids == []
        assert _all_calls(d) == 0


def test_R33_pr_digit_cap(tmp_path):
    for i, pr in enumerate(("1234567890", "9" * 5000, "0" + "1234567890")):
        d = tmp_path / f"c{i}"
        d.mkdir()
        out, calls, ids, rc = _run(d, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), pr=pr)
        assert out.startswith("NOT POSTED: invalid arguments"), (pr[:12], out[:120])
        assert len(out.splitlines()) == 1 and len(out) < 300
        assert rc == 2 and calls == 0 and ids == []
        assert _all_calls(d) == 0
    # The boundary: nine significant digits (leading zeros ignored) still post.
    for i, (arg, real) in enumerate((("999999999", "999999999"), ("0000000007", "7"))):
        d = tmp_path / f"ok{i}"
        d.mkdir()
        out, calls, ids, rc = _run(
            d, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST, pr=real), pr=arg
        )
        assert out.startswith("READ-BACK OK") and rc == 0 and calls == 1, (arg, out)


def test_R34_gh_timeout_is_exit_6(tmp_path):
    out, calls, ids, rc = _run(
        tmp_path, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST),
        env_extra={"STUB_PR_SLEEP": "30"}, extra_args=["--timeout", "1"],
    )
    assert rc == 6 and calls == 1 and ids == []
    assert out.startswith("READ-BACK MISMATCH: helper error after the post was attempted")
    assert "TimeoutExpired" in out and len(out.splitlines()) == 1


def test_R34b_timeout_must_be_a_positive_int(tmp_path):
    for i, t in enumerate(("0", "-3", "abc")):
        d = tmp_path / f"c{i}"
        d.mkdir()
        out, calls, ids, rc = _run(
            d, _body(GOOD_FIRST), GOOD_URL, _comment(GOOD_FIRST), extra_args=["--timeout", t]
        )
        assert out.startswith("NOT POSTED: invalid arguments"), (t, out)
        assert rc == 2 and calls == 0 and _all_calls(d) == 0
