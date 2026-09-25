#!/usr/bin/env python3
"""enumerate_armed_monitors.py -- List Monitor-tool task_ids armed this session.

engram-alpha#3049: engram-sleep's Phase A Step 3.5 only tears down live
Monitors (Umwelt bell, forum-mention, forum-updates) when a
``~/.engram/loop-mode.json`` marker exists. A session that armed Monitors ad
hoc -- e.g. in response to "arm loop and enter Umwelt" without ever running
the full ``/loop`` entry that writes the marker -- leaves those Monitors
running through a sleep cycle undetected, because Step 3.5's own text reads
"skip entirely if no marker exists" and there is nothing else that
enumerates them: ``TaskStop`` takes a ``task_id`` and nothing lists armed
ones, and ``CronList`` covers cron jobs, not Monitors (Ariadne, forum #423).

The one enumeration source that works is the session transcript: every
``Monitor`` arm writes a ``tool_use`` block (name=="Monitor") followed by a
``tool_result`` block whose content starts with ``Monitor started (task
<id>``, and ``SessionStart`` already injects ``transcript_path`` into
context.

**Two self-contamination modes, both measured live by Ariadne on forum #423,
and why parsing by record+tool-name (not grepping raw text) avoids both:**

1. An ENGRAM node whose ``quoted_text`` cites a prior arm line reads as an
   arm under a plain text grep, because the phrase "Monitor started (task"
   appears in that record too -- but that record's own tool call is
   ``engram_inspect`` (or whatever tool produced it), never a ``tool_use``
   block with ``name == "Monitor"``. Filtering on the tool_use block's own
   ``name`` field, not on substring content anywhere in the transcript,
   structurally excludes this: the phrase has to originate from a REAL
   Monitor arm's own tool_result, matched back to its own tool_use by
   ``tool_use_id``.
2. A prior run's own printed output lands in the transcript as a Bash
   tool_result, and grepping the transcript again matches that output.
   Parsing by record means this script's own output is never itself a
   ``tool_use`` block named ``Monitor`` -- it's ordinary stdout attached to
   whatever invoked it -- so it can never appear as a false hit on a second
   run.

Stale ids are harmless over-collection: most arms from earlier in a long
session have already expired (Monitor's own 30-min or session-length cap),
and ``TaskStop`` against an expired id simply fails -- so this script
over-collects deliberately rather than trying to determine liveness itself.
Liveness is not this script's job; see the fd-target cross-check in the
engram-sleep skill's Step 3.5 for a way to confirm teardown worked
afterward, which this script also does not attempt (it has no access to
`/proc` or a live process table by design -- it only reads the transcript).

**"Harmless" is a correctness claim, not a cost claim -- the two diverge on
long sessions.** Measured by Aleph on a real ~46MB three-day transcript
(PR-3050 colleague review): 255 unique task_ids returned against 2 actually-
live Monitors. Each id costs one `TaskStop` call, so over-collection scales
with SESSION length, not with how many Monitors are actually live --
worth knowing given engram-sleep is the one routine on this fleet with a
measured budget-blowout history (an unchecked Phase B orchestration once
cost 6.39M tokens in 25 minutes). Not fixed here: capping the scan would
trade away the completeness this tool exists for (the same completeness
that, per Aleph's own report, caught a real live Monitor his own seat's
confirm-first policy had missed) -- left as a known, disclosed tradeoff
rather than a silent one.

This tool also enumerates only what THIS SESSION'S transcript can see --
"no Monitors found" means "none armed by this session," not "none live on
this host." A Monitor from a different session, a pruned worktree, or an
orphaned process is invisible to it by construction.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ARM_TASK_ID_RE = re.compile(r"Monitor started \(task (\S+?)[,)]")


def _tool_result_text(content) -> str:
    """Flatten a `tool_result` block's own inner `content` field to text.

    Claude Code's JSONL `tool_result.content` appears in production as
    EITHER a bare string OR a list of typed block dicts -- the same
    ambiguity `_stophooklib.walk_content_blocks()` normalizes for the
    Stop-hook family (src/engram/hooks/claude/_stophooklib.py). This tool
    is intentionally stdlib-only with no cross-package import, so it
    re-implements the same two-shape handling locally rather than reaching
    across src/engram/hooks/ -- semantics match, dependency does not.

    A prior version of this function hard-required a bare string and
    silently returned nothing for list-shaped content, which meant a
    Monitor arm whose own tool_result happened to be list-shaped was never
    enumerated at all -- the exact "Monitor escapes teardown" failure this
    whole tool exists to prevent, just via a different code path
    (engram-alpha#3050 review round 1).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


def find_armed_monitor_task_ids(transcript_path: str | Path) -> list[str]:
    """Return every Monitor task_id armed in this transcript, oldest first.

    Parses each JSONL line as its own record. Only a ``tool_use`` block
    whose ``name`` is exactly ``"Monitor"`` is treated as an arm; only the
    ``tool_result`` block whose ``tool_use_id`` matches that arm's own
    ``id`` is read for the task_id. No substring search over raw text ever
    runs -- see the module docstring for why that is the load-bearing
    property, not a stylistic preference.

    Duplicate ids (a task_id re-armed under the same id never happens in
    practice, but a session can restart mid-transcript on rare harness
    paths) are de-duplicated while preserving first-seen order, so a
    consumer that TaskStops each id once does not double-call.
    """
    pending_monitor_tool_use_ids: set[str] = set()
    seen: dict[str, None] = {}  # ordered set: dict preserves insertion order

    path = Path(transcript_path)
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue

                if (
                    block.get("type") == "tool_use"
                    and block.get("name") == "Monitor"
                    and isinstance(block.get("id"), str)
                ):
                    pending_monitor_tool_use_ids.add(block["id"])
                    continue

                if (
                    block.get("type") == "tool_result"
                    and block.get("tool_use_id") in pending_monitor_tool_use_ids
                ):
                    result_text = _tool_result_text(block.get("content"))
                    match = _ARM_TASK_ID_RE.search(result_text)
                    if match:
                        seen.setdefault(match.group(1), None)
                    # Consumed (matched or not) -- a Monitor tool_use only
                    # ever gets one tool_result, so this id is done either way.
                    pending_monitor_tool_use_ids.discard(block["tool_use_id"])

    return list(seen.keys())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "transcript_path",
        help="Path to the current session's .jsonl transcript "
        "(SessionStart injects this as transcript_path).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON array instead of one id per line.",
    )
    args = parser.parse_args(argv)

    task_ids = find_armed_monitor_task_ids(args.transcript_path)

    if args.json:
        print(json.dumps(task_ids))
    else:
        for task_id in task_ids:
            print(task_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
