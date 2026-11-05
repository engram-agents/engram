#!/usr/bin/env python3
"""fairy_post_review.py -- the reviewer fairy's post-and-read-back gate (#3853).

The engram-pr-reviewer spec used to carry this gate as a bash block. Claude
Code's worktree guard refuses any command that carries a runtime-computed
value next to ``gh``/``git``, so a worktree-isolated fairy could not run the
block and rebuilt the gate by hand. The gate now lives here and the fairy
calls it as ONE fully literal command:

    python3 <helper path> \
        --repo OWNER/REPO --pr N --expected 'LINE' --body-file PATH [--gh GH] [--timeout SECONDS]

The helper path is under the plugin root, `${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}/tools/`, with ENGRAM_HOME naming the data home.

Stdlib only, and it must run on Python 3.9 (bare ``python3`` in a fairy shell
is the system interpreter; Apple's is 3.9.6): no ``match``, no ``X | Y`` at
runtime, no 3.10+ stdlib. It imports nothing from the engram package.
``--gh`` names the gh executable (default ``gh``); tests pass a stub.
``--timeout SECONDS`` (positive int, default 120) bounds each of the two gh calls;
a timeout is exit 6 on either (the post may have landed). ``--pr`` takes at most 9
significant digits (checked before ``int()``, so every interpreter behaves alike),
and ``--expected`` must be a single line (a newline or carriage return is exit 2).

Every outcome prints exactly ONE result line on stdout and exits. Values embedded
in a line (gh's output, the body's first line, the landed first line) are collapsed
to one line (whitespace runs become one space) for printing only; the comparisons
use the raw values:

  0  READ-BACK OK: comment <id> on PR #<N>: <first line>
  2  NOT POSTED: invalid arguments: <reason>   |  body file unreadable: <reason>
  3  NOT POSTED: body file first line is [..], expected [..]
     NOT POSTED: body file has no '## Verdict' section (truncated?)
  4  READ-BACK MISMATCH ... (gh pr comment failed or printed no comment id)
  5  READ-BACK MISMATCH ... (the comment that landed fails a read-back check)
  2  NOT POSTED: helper error: <type>: <msg>   (unexpected error before any post)
  6  READ-BACK MISMATCH: helper error after the post was attempted: <type>: <msg>
     (unexpected error, or a gh timeout, once ``gh pr comment`` was invoked: the
     post may have landed)

stdout is forced to UTF-8 (the result line carries an emoji), and gh's output is
decoded with errors="replace", so neither locale nor stray bytes can suppress the
line. The one non-result output is argparse's own ``--help`` (prints usage, exit 0).

The only gh calls are one ``pr comment`` and one ``api .../issues/comments/<id>``
for the id gh printed. Never a second post, never edit/delete, never a "latest
comment" fetch: on a mismatch the parent decides, not this tool.
"""
import argparse
import json
import re
import subprocess
import sys

REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PR_RE = re.compile(r"[0-9]+")
ID_RE = re.compile(r"issuecomment-(\d+)")
MAX_PR_DIGITS = 9
DEFAULT_TIMEOUT = 120

# Set once ``gh pr comment`` is about to be invoked, so the catch-all in main()
# can tell "nothing posted" (exit 2) from "the post may have landed" (exit 6).
_STATE = {"post_attempted": False}


class _Args(argparse.ArgumentParser):
    def error(self, message):
        _finish("NOT POSTED: invalid arguments: " + message, 2)


def _finish(line, code):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    sys.exit(code)


def _flat(value):
    """Collapse a value to one line for printing (never used for comparisons)."""
    return " ".join(str(value).split())


def _positive_int(text):
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError("--timeout must be a positive integer, got [%s]" % _flat(text))
    return n


def _first_line(text):
    lines = text.splitlines()
    return lines[0] if lines else ""


def _verdict_lines(text):
    return sum(1 for ln in text.splitlines() if ln.startswith("## Verdict"))


def _mismatch(cid, printed, posted_pr, expected, posted_first, verdicts):
    return (
        "READ-BACK MISMATCH: comment [%s] (gh printed [%s]) landed on PR #%s; "
        "expected first line [%s], posted first line [%s]; "
        "'## Verdict' lines: %d" % (
            _flat(cid), _flat(printed), _flat(posted_pr), _flat(expected),
            _flat(posted_first), verdicts,
        )
    )


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # not a TextIOWrapper (or very old Python): best effort
        pass
    try:
        _run(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # one result line, whatever happens
        detail = "%s: %s" % (type(exc).__name__, exc)
        if _STATE["post_attempted"]:
            _finish(
                "READ-BACK MISMATCH: helper error after the post was attempted: " + detail, 6
            )
        _finish("NOT POSTED: helper error: " + detail, 2)


def _run(argv=None):
    ap = _Args(description="Post a reviewer comment through the gate, then read it back.")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--expected", required=True)
    ap.add_argument("--body-file", required=True, dest="body_file")
    ap.add_argument("--gh", default="gh")
    ap.add_argument("--timeout", type=_positive_int, default=DEFAULT_TIMEOUT)
    a = ap.parse_args(argv)

    # 1. Argument validation, before anything else.
    if not PR_RE.fullmatch(a.pr) or len(a.pr.lstrip("0")) == 0:
        _finish("NOT POSTED: invalid arguments: --pr must be a positive integer, got [%s]" % _flat(a.pr), 2)
    if len(a.pr.lstrip("0")) > MAX_PR_DIGITS:
        _finish(
            "NOT POSTED: invalid arguments: --pr must have at most %d significant digits"
            % MAX_PR_DIGITS,
            2,
        )
    if not REPO_RE.fullmatch(a.repo) or any(
        not c.strip(".") for c in a.repo.split("/")
    ):
        _finish("NOT POSTED: invalid arguments: --repo must look like OWNER/REPO, got [%s]" % _flat(a.repo), 2)
    if not a.expected:
        _finish("NOT POSTED: invalid arguments: --expected must be non-empty", 2)
    if "\n" in a.expected or "\r" in a.expected:
        _finish("NOT POSTED: invalid arguments: --expected must be a single line", 2)
    pr = str(int(a.pr))

    # 2. Body file.
    try:
        with open(a.body_file, "r", encoding="utf-8") as fh:
            body = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        _finish("NOT POSTED: body file unreadable: %s" % _flat(exc), 2)

    # 3. First-line gate.
    first = _first_line(body)
    if first != a.expected:
        _finish(
            "NOT POSTED: body file first line is [%s], expected [%s]"
            % (_flat(first), _flat(a.expected)),
            3,
        )

    # 4. Verdict gate.
    if _verdict_lines(body) < 1:
        _finish("NOT POSTED: body file has no '## Verdict' section (truncated?)", 3)

    # 5. Post, once.
    _STATE["post_attempted"] = True
    try:
        r = subprocess.run(
            [a.gh, "pr", "comment", pr, "-R", a.repo, "--body-file", a.body_file],
            stdout=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            timeout=a.timeout,
        )
        printed, rc = r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        _finish(
            "READ-BACK MISMATCH: helper error after the post was attempted: "
            "TimeoutExpired: gh pr comment timed out after %d s; the post may have landed"
            % a.timeout,
            6,
        )
    except OSError as exc:
        printed, rc = "could not run gh: %s" % exc, 1
    ids = ID_RE.findall(printed)
    if rc != 0 or not ids:
        _finish(_mismatch("", printed, "", a.expected, "", 0), 4)
    cid = ids[-1]

    # 6. Read back that exact comment.
    posted_pr, posted_first, verdicts = "", "", 0
    try:
        g = subprocess.run(
            [a.gh, "api", "repos/%s/issues/comments/%s" % (a.repo, cid)],
            stdout=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
            timeout=a.timeout,
        )
        if g.returncode == 0:
            data = json.loads(g.stdout)
            posted_pr = str(data["issue_url"]).rstrip("/").rsplit("/", 1)[-1]
            posted_body = data["body"]
            posted_first = _first_line(posted_body)
            verdicts = _verdict_lines(posted_body)
    except subprocess.TimeoutExpired:
        _finish(
            "READ-BACK MISMATCH: helper error after the post was attempted: "
            "TimeoutExpired: gh api read-back timed out after %d s; the post may have landed"
            % a.timeout,
            6,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass

    if posted_pr == pr and posted_first == a.expected and verdicts >= 1:
        _finish("READ-BACK OK: comment %s on PR #%s: %s" % (cid, posted_pr, _flat(posted_first)), 0)
    _finish(_mismatch(cid, printed, posted_pr, a.expected, posted_first, verdicts), 5)


if __name__ == "__main__":
    main()
