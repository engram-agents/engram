#!/usr/bin/env python3
"""fairy_report.py — extract a fairy's verbatim <result> block from a
session transcript (#2760).

## Why

A dispatched fairy's own `.output` file lives under the harness scratchpad
(`/tmp/claude-<uid>/-<project-slug>/<session-id>/tasks/<task-id>.output`),
which does not survive a host reboot. This project's convention is "a fairy
round that isn't transcribed to the PR didn't happen" -- but by the time a
merge-seat notices a round was never transcribed, the artifact backing a
LATE transcription may already be gone, while the round is still fully
remembered.

The recovery route: the full `<task-notification>...<result>...</result>
...</task-notification>` envelope is ALSO embedded in the session
transcript at `~/.claude/projects/<project-slug>/<session-id>.jsonl`, which
lives under `$HOME`, not `/tmp` -- it survives a reboot. This tool automates
that extraction (originally done by hand, once, under merge pressure, per
issue #2760's own account) so recovery is cheap and repeatable instead of
"invent ad-hoc Python under merge pressure."

## Usage

    ~/.engram/venv/bin/python3 tools/fairy_report.py --task <task-id> [--session <id>]
    ~/.engram/venv/bin/python3 tools/fairy_report.py --grep <pattern> [--session <id>]

`--task <task-id>` extracts the verbatim text between `<result>` and
`</result>` for that task's notification and prints ONLY that text to
stdout -- nothing else -- so the output pipes directly into
`gh pr comment --body-file -`. `--grep <pattern>` is the discovery mode for
when the exact task-id isn't known: lists every matching task-notification
found (task-id, session file, summary, a short snippet), one per line,
without extracting a full result (re-run with `--task` on the id you want).

`--session <id>` scopes the search to that one session's transcript
(resolved by globbing `~/.claude/projects/*/<id>.jsonl`, since this tool
doesn't reimplement the harness's cwd-to-slug encoding). Omit it to search
every session transcript under `~/.claude/projects/` -- the right default
when recovering a round from an unknown or forgotten session.

## Contract

Read-only over `~/.claude/projects/*.jsonl`. No writes, ever. Checkable
output: `--task` either prints the report and exits 0, or prints nothing to
stdout and exits 1 with a clear reason on stderr -- never a silent partial
success. Dev-tier (T3): a recovery tool for this repo's own review
discipline, not something an end-user install needs.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import NamedTuple, Optional

_RESULT_OPEN = "<result>"
_RESULT_CLOSE = "</result>"
_TASK_NOTIFICATION_OPEN = "<task-notification>"
_TASK_NOTIFICATION_CLOSE = "</task-notification>"
_TASK_ID_OPEN = "<task-id>"


class TaskNotification(NamedTuple):
    task_id: str
    summary: str
    status: str
    result: Optional[str]  # None if the envelope has no <result> block
    session_file: str
    line_no: int  # 1-indexed, for a human to jump to it


def _resolve_session_files(session: Optional[str]) -> list[str]:
    """Session transcript paths to search, in a stable (sorted) order."""
    projects_dir = Path("~/.claude/projects").expanduser()
    if session:
        pattern = str(projects_dir / "*" / f"{session}.jsonl")
    else:
        pattern = str(projects_dir / "*" / "*.jsonl")
    return sorted(glob.glob(pattern))


def _between(text: str, open_tag: str, close_tag: str) -> Optional[str]:
    """First `open_tag`, first `close_tag` after it. Safe for the short,
    machine-generated header fields (task-id/summary/status), which never
    legitimately contain nested tag-like substrings -- NOT used for
    `<result>` (see `_extract_result` below for why)."""
    start = text.find(open_tag)
    if start == -1:
        return None
    start += len(open_tag)
    end = text.find(close_tag, start)
    if end == -1:
        return None
    return text[start:end]


def _unescape_observed_entities(text: str) -> str:
    """Restore the small, fixed set of HTML entities empirically observed
    in stored transcript result text (`&lt;`, `&gt;`, `&amp;` -- checked
    live against a real transcript; no other entity has been seen there).

    Deliberately narrower than `html.unescape()` -- round-2 reviewer-fairy
    suggestion on this PR: Python's `html.unescape()` follows HTML5's full
    legacy table of ~2000+ semicolon-optional named entities, so it can
    mangle ordinary prose that happens to contain one of those names
    immediately followed by more text with no space or semicolon (e.g. a
    literal, never-escaped "Fish & Chips &amp Co" -- note the missing
    semicolon after `amp` -- silently becomes "Fish & Chips & Co"). Since
    this tool only needs to reverse a specific, known escaping step (not
    decode arbitrary HTML), it unescapes exactly the three entities that
    step produces and nothing else.

    Order matters: `&lt;`/`&gt;` first, `&amp;` last -- `&amp;` is itself
    the escape for a literal `&`, so decoding it first could make a
    downstream `&lt;`/`&gt;` replacement fire on text that never
    represented one (e.g. a stored "&amp;lt;" -- a literal ampersand
    followed by the plain text "lt;" -- decoding `&amp;` first yields
    "&lt;", which the next pass would incorrectly decode again into "<").
    """
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _extract_result(envelope: str) -> Optional[str]:
    """The `<result>` block's content, robust against the result text
    itself containing the literal substring `</result>`.

    Reviewer-fairy finding on this tool's own PR (#2766, round 1): a naive
    first-`<result>`/first-`</result>` search silently TRUNCATES whenever
    the fairy's own report discusses `</result>` as literal text -- highly
    plausible for exactly this tool's use case (fairy reports routinely
    quote tag/code syntax verbatim, including this very envelope's own
    tags). Reproduced live by the reviewer before this fix.

    Strategy: `<result>` is reliably followed by machine-generated,
    structurally rigid sibling content -- `<usage><subagent_tokens>` when
    present, or the envelope's own closing `</task-notification>`
    otherwise -- and the genuine occurrence of either is ALWAYS the
    rightmost one in the envelope (each is machine-appended exactly once,
    strictly after `<result>...</result>` closes; nothing free-form comes
    after them). So both anchors are searched with `rfind` -- the last
    match, not the first -- which makes the search immune to the anchor
    substring appearing anywhere earlier as literal prose inside the
    result text itself, however many times. Only THEN is a trailing
    `</result>` trimmed off the slice this already bounds -- trimming a
    known, fixed-length suffix off an independently-determined boundary,
    never using that suffix to FIND the boundary in the first place (which
    is what round 1's original bug did, and is invertible regardless of
    what precedes it -- a result whose own final characters happen to
    spell exactly `</result>` was suspected as a residual gap in an
    earlier draft of this comment, then round-2 reviewer-fairy tested it
    directly and confirmed it round-trips correctly; not a real gap).

    Round 1 found the naive first-`</result>` search truncates on a
    literal `</result>` in report prose. Round 2 found the round-1 FIX
    reproduced the identical bug at the `<usage>` anchor, because that
    anchor was searched with `find` (forward) instead of `rfind` --
    demonstrated live with text resembling this very docstring. Both
    anchors now use `rfind` for exactly the reason given above.

    Round 3 found that `rfind` alone is only correct when `envelope`
    genuinely holds ONE task-notification -- if the caller's raw text
    field concatenates two, `rfind` for `<usage>` would find the SECOND
    envelope's tag and bleed its content into the first envelope's
    "result". The caller (`_iter_task_notifications`, via
    `_iter_envelopes`) now delimits each envelope's outer boundary FIRST,
    so `_extract_result` only ever receives one genuine envelope's text --
    the "rightmost occurrence is genuine" invariant this function relies
    on then holds structurally, not just for the single-envelope case it
    was tested against.
    """
    start = envelope.find(_RESULT_OPEN)
    if start == -1:
        return None
    start += len(_RESULT_OPEN)

    # rfind, not find: round-2 reviewer-fairy finding on this same PR --
    # a FORWARD (first-occurrence) search for the usage anchor reproduces
    # the identical bug class round 1 flagged, just relocated to a
    # different string, because a result that discusses this exact anchor
    # as prose (plausible for a report about this very extraction logic --
    # demonstrated live by the round-2 reviewer using text resembling this
    # PR's own commit message) would match on the prose occurrence instead
    # of the genuine one. The genuine `<usage>` block is always the LAST
    # occurrence before `</task-notification>` (it's machine-appended once,
    # after `<result>` closes) -- rfind finds that one, matching the same
    # search-from-the-end reasoning already applied to the
    # `</task-notification>` fallback below.
    end = envelope.rfind("<usage><subagent_tokens>", start)
    if end == -1:
        end = envelope.rfind("</task-notification>", start)
    if end == -1:
        # No usage block AND no closing tag found -- envelope is truncated
        # (e.g. a reboot cut the transcript mid-write). Take everything to
        # the end of the string rather than returning None: a partial
        # result recovered is still more useful than nothing, and the
        # caller can judge from context whether it looks cut off.
        end = len(envelope)

    result = envelope[start:end]
    # The envelope's own formatting inserts exactly one newline between
    # `</result>` and whatever sibling tag follows it -- strip that ONE
    # newline first (order matters: it sits AFTER `</result>` in the raw
    # slice), then strip the `</result>` tag itself. A report that
    # genuinely ends in blank lines keeps them; only this one structural
    # newline is trimmed.
    if result.endswith("\n"):
        result = result[:-1]
    if result.endswith(_RESULT_CLOSE):
        result = result[: -len(_RESULT_CLOSE)]
    return _unescape_observed_entities(result)


def _extract_task_notification(envelope: str) -> Optional[dict]:
    """Parse one `<task-notification>...</task-notification>` envelope's
    task-id / summary / status / result out of its plain-text (not real
    XML) body. Returns None if `task_id` can't be found -- a malformed or
    unrelated envelope, not a "no result" case (see the result=None
    distinction below for that).
    """
    task_id = _between(envelope, _TASK_ID_OPEN, "</task-id>")
    if task_id is None:
        return None
    summary = _between(envelope, "<summary>", "</summary>") or ""
    status = _between(envelope, "<status>", "</status>") or ""
    # A result-less envelope is real and distinct from "not found at all"
    # (e.g. a task that failed before producing output) -- None, not "".
    result = _extract_result(envelope)
    return {"task_id": task_id, "summary": summary, "status": status, "result": result}


def _iter_envelopes(text: str):
    """Yield each properly-bounded `<task-notification>...</task-notification>`
    substring found in `text`, in order -- zero, one, or many.

    Round-3 reviewer-fairy finding on this PR (#2766): a record's raw text
    field is not guaranteed to hold exactly one envelope. The prior code
    treated the whole field as one envelope and searched INNER anchors
    (`<usage>...`) across it with `rfind` -- if the field genuinely held
    two concatenated envelopes, `rfind` would find the SECOND envelope's
    `<usage>` tag and use it as the boundary for the FIRST envelope's
    result, bleeding the second envelope's tags/text into the first's
    recovered output. Demonstrated live by the reviewer.

    Fix: delimit each envelope's OUTER boundary FIRST, before any inner-tag
    extraction runs, so `_extract_result`'s `rfind` calls only ever see one
    genuine envelope's text -- the "genuine anchor is always rightmost"
    invariant they rely on then holds structurally, not just usually.

    An initial version of this function reasoned that
    `<task-notification>`/`</task-notification>` are long, unusual compound
    tag names a fairy's own report would have no reason to quote as prose,
    and used a plain forward `find` for the close. That reasoning was
    falsified by this very PR's own history: this exact tag name IS what
    three straight rounds of fairy reports about this file have discussed
    verbatim (this docstring is doing it right now) -- caught by testing
    the claim directly, not by a fourth review round finding it. A plain
    forward `find` for the close is exactly as vulnerable to prose
    collision as the inner-anchor searches this file's history has already
    fixed twice.

    So each CANDIDATE close is validated before being accepted: a genuine
    structural close is followed by nothing but whitespace (end of the
    field) or the next envelope's own opening tag -- prose that merely
    discusses `</task-notification>` is followed by more prose. A
    candidate that fails this check is skipped and the search continues
    forward for the next occurrence. This is a heuristic, not a proof --
    prose that happens to end a result immediately after mentioning the
    closing tag, with nothing else following, cannot be distinguished from
    a genuine close by this or any purely textual signal; accepted as a
    documented residual limitation of an informally-structured (not real
    XML, no escaping mechanism) format, not silently ignored.

    Round-4 reviewer-fairy finding: "followed by the next envelope's own
    opening tag" is not itself sufficient -- it only checked that the text
    right after the close STARTS WITH the open-tag substring, which prose
    can satisfy without a real envelope following. A single, well-formed
    envelope's own `<result>` prose that literally quotes BOTH tags
    back-to-back (`</task-notification>\n<task-notification>\n`) followed
    by more ordinary prose -- exactly the sentence shape this file's own
    docstrings write repeatedly -- passed the old check and split one
    genuine envelope into a truncated first piece and a second piece with
    no `<task-id>`, silently dropping the tail. Demonstrated live by the
    reviewer with a minimal single-envelope repro (no second envelope
    needed).

    Fix: an open-tag candidate only counts if what immediately follows it
    (past leading whitespace) is itself `<task-id>` -- the one thing every
    genuine envelope has machine-appended right after its own open tag,
    every time, with nothing free-form between them (mirrored exactly by
    `_envelope()` in this file's own tests). Prose that merely quotes the
    open tag as a literal string has no reason to be followed by that exact
    next tag too, so this rejects the round-4 repro while still accepting
    two genuinely concatenated real envelopes (round 3's case). It narrows,
    but does not close, the same residual gap already documented above:
    round-5 reviewer-fairy confirmed the bare 9-character substring
    `<task-id>` alone -- not a full fake header, just that one literal
    string right after the fake open tag -- is already enough to pass;
    accepted as a further instance of the same documented limitation
    (no purely textual signal can rule out an increasingly elaborate
    forgery of this informally-structured, unescaped format), not silently
    ignored.
    """
    pos = 0
    while True:
        start = text.find(_TASK_NOTIFICATION_OPEN, pos)
        if start == -1:
            return
        search_from = start
        while True:
            close_at = text.find(_TASK_NOTIFICATION_CLOSE, search_from)
            if close_at == -1:
                # No close found anywhere after this open -- truncated tail
                # (e.g. a reboot cut the transcript mid-write). Yield the
                # unbounded remainder once, then stop.
                yield text[start:]
                return
            end = close_at + len(_TASK_NOTIFICATION_CLOSE)
            following = text[end:].lstrip()
            is_genuine_close = following == "" or (
                following.startswith(_TASK_NOTIFICATION_OPEN)
                and following[len(_TASK_NOTIFICATION_OPEN):].lstrip().startswith(_TASK_ID_OPEN)
            )
            if is_genuine_close:
                yield text[start:end]
                pos = end
                break
            # This candidate is followed by more text that isn't the next
            # envelope -- almost certainly prose quoting the tag, not the
            # genuine close. Keep looking forward from just past it.
            search_from = end


def _iter_task_notifications(session_file: str):
    """Yield (line_no, parsed-dict-or-None) for every task-notification
    envelope found in one session's JSONL -- including multiple envelopes
    within a single record's text field (see `_iter_envelopes`). A line may
    also hold the SAME envelope multiple times across different record
    types (queue-operation enqueue/remove, attachment) -- the harness's own
    bookkeeping, not a reason to skip lines; callers dedupe by (task_id,
    result) if needed.

    Never raises on a malformed line -- one corrupt JSONL row must not
    abort a scan that's trying to recover something the reboot already
    half-destroyed.
    """
    try:
        with open(session_file, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw_line in enumerate(f, start=1):
                if _TASK_NOTIFICATION_OPEN not in raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue
                field_text = _find_envelope_text(record)
                if field_text is None:
                    continue
                for envelope in _iter_envelopes(field_text):
                    parsed = _extract_task_notification(envelope)
                    if parsed is not None:
                        yield line_no, parsed
    except OSError:
        return


def _find_envelope_text(record: dict) -> Optional[str]:
    """The task-notification text lives under different keys depending on
    the JSONL record's `type` (queue-operation: "content"; attachment:
    attachment["prompt"]) -- check both rather than assuming one shape,
    since this format is undocumented and observed empirically, not from a
    stable schema this tool can rely on staying fixed.
    """
    content = record.get("content")
    if isinstance(content, str) and _TASK_NOTIFICATION_OPEN in content:
        return content
    attachment = record.get("attachment")
    if isinstance(attachment, dict):
        prompt = attachment.get("prompt")
        if isinstance(prompt, str) and _TASK_NOTIFICATION_OPEN in prompt:
            return prompt
    return None


def find_by_task_id(task_id: str, session: Optional[str] = None) -> Optional[TaskNotification]:
    """The LATEST matching notification for `task_id` across the searched
    session file(s) -- "latest" meaning the last one encountered in a
    stable file-then-line scan order, since a resumed/re-notified task (the
    harness's own documented behavior: "the same task-id may notify more
    than once") can legitimately produce several envelopes, and the most
    recent is the one worth trusting.
    """
    best: Optional[TaskNotification] = None
    for session_file in _resolve_session_files(session):
        for line_no, parsed in _iter_task_notifications(session_file):
            if parsed["task_id"] != task_id:
                continue
            best = TaskNotification(
                task_id=parsed["task_id"],
                summary=parsed["summary"],
                status=parsed["status"],
                result=parsed["result"],
                session_file=session_file,
                line_no=line_no,
            )
    return best


def grep_task_notifications(pattern: str, session: Optional[str] = None) -> list[TaskNotification]:
    """Every DISTINCT (by task_id) task-notification whose summary or
    result text contains `pattern` (case-insensitive substring match --
    deliberately not a regex, so a caller doesn't need to escape PR titles
    or issue numbers that happen to contain regex metacharacters), across
    the searched session file(s). Keeps the LATEST envelope per task_id,
    same reasoning as find_by_task_id.
    """
    needle = pattern.lower()
    seen: dict[str, TaskNotification] = {}
    for session_file in _resolve_session_files(session):
        for line_no, parsed in _iter_task_notifications(session_file):
            haystack = (parsed["summary"] + " " + (parsed["result"] or "")).lower()
            if needle not in haystack:
                continue
            seen[parsed["task_id"]] = TaskNotification(
                task_id=parsed["task_id"],
                summary=parsed["summary"],
                status=parsed["status"],
                result=parsed["result"],
                session_file=session_file,
                line_no=line_no,
            )
    return list(seen.values())


def _snippet(text: str, length: int = 100) -> str:
    text = " ".join(text.split())  # collapse newlines/whitespace for a one-line preview
    return text[:length] + ("…" if len(text) > length else "")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--task", metavar="TASK-ID", help="extract this task's <result> block")
    mode.add_argument("--grep", metavar="PATTERN", help="list task-notifications matching PATTERN (discovery mode)")
    parser.add_argument(
        "--session",
        metavar="SESSION-ID",
        default=None,
        help="scope the search to one session transcript (default: search all sessions)",
    )
    args = parser.parse_args(argv)

    session_files = _resolve_session_files(args.session)
    if not session_files:
        scope = f"session {args.session}" if args.session else "any session"
        print(f"fairy_report: no transcript found for {scope} under ~/.claude/projects/", file=sys.stderr)
        return 1

    if args.task:
        found = find_by_task_id(args.task, args.session)
        if found is None:
            print(f"fairy_report: no task-notification found for task-id {args.task!r}", file=sys.stderr)
            return 1
        if found.result is None:
            print(
                f"fairy_report: task-notification for {args.task!r} found "
                f"(status={found.status!r}, {found.session_file}:{found.line_no}) but carries no "
                f"<result> block -- the task may have errored before producing output",
                file=sys.stderr,
            )
            return 1
        print(found.result)
        return 0

    matches = grep_task_notifications(args.grep, args.session)
    if not matches:
        print(f"fairy_report: no task-notifications match {args.grep!r}", file=sys.stderr)
        return 1
    for m in sorted(matches, key=lambda t: (t.session_file, t.line_no)):
        preview = _snippet(m.result) if m.result else "(no result block)"
        print(f"{m.task_id}\t{m.session_file}:{m.line_no}\t{m.summary}\t{preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
