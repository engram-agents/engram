"""loop_prompt — SSoT for the loop-wake prompt marker.

Autonomous loop wakes (ScheduleWakeup) must be distinguishable from genuine
human-typed prompts so hooks can classify them correctly.  This module owns the
single source of truth for the marker string and the formatting / detection
helpers that guarantee every loop wake carries it.

The marker is ALWAYS the first non-whitespace token in a loop-wake prompt — that
invariant is what makes prefix-based detection in the time-bar hook reliable.

Usage:

    from loop_prompt import format_loop_prompt, is_loop_wake, LOOP_WAKE_MARKER

    # Arm a ScheduleWakeup:
    prompt = format_loop_prompt("Loop tick — checking for new work.", kind="working")

    # Classify in a hook:
    if is_loop_wake(payload.get("prompt", "")):
        ...  # not a human prompt

CLI (compose with shell tools):

    echo "Loop tick — checking new work." | python -m engram.tools.loop_prompt

Stdlib-only — no third-party deps.  Mirrors the style of tools/presence.py and
tools/_status_derive.py.
"""

import argparse
import json
import sys

# ---------------------------------------------------------------------------
# SSoT — the single source of truth for the marker string.  The hook imports
# this directly; the fallback constant in the hook must equal this value.
# ---------------------------------------------------------------------------

LOOP_WAKE_MARKER: str = "<loop-wake>"

# The machine-readable meta-line prefix (prefix only; the JSON payload follows).
_META_PREFIX: str = "<loop-meta "
_META_SUFFIX: str = ">"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def format_loop_prompt(core: str, **meta) -> str:
    """Return a loop-wake prompt with the marker guaranteed on the first line.

    The marker is always the first non-whitespace token, making prefix-based
    detection in the time-bar hook reliable regardless of the core content.

    Layout::

        <loop-wake>
        <loop-meta {"kind": "working"}>   ← only when meta kwargs are given
        <core message here>

    Idempotent: if ``core`` already starts with the marker (i.e. the result of
    a previous ``format_loop_prompt`` call), the marker is not added again.

    Parameters
    ----------
    core:
        The human-readable loop message.  Typically the loop's current intent
        or status.
    **meta:
        Optional machine-readable metadata (e.g. ``kind="working"``,
        ``seq=3``).  Embedded as JSON on the second line when present.
    """
    stripped_core = core.lstrip()

    # Idempotency guard: already starts with the marker — don't double-add.
    if stripped_core.startswith(LOOP_WAKE_MARKER):
        return core

    parts = [LOOP_WAKE_MARKER]
    if meta:
        parts.append(f"{_META_PREFIX}{json.dumps(meta, separators=(',', ':'))}{_META_SUFFIX}")
    parts.append(core)
    return "\n".join(parts)


def is_loop_wake(body: str) -> bool:
    """Return True if ``body`` starts with the loop-wake marker.

    Strips leading whitespace before checking, so slight prompt indentation or
    leading newlines don't defeat the detection.

    Parameters
    ----------
    body:
        The raw prompt body string (e.g. ``payload.get("prompt", "")``).
    """
    return body.lstrip().startswith(LOOP_WAKE_MARKER)


# ---------------------------------------------------------------------------
# CLI entrypoint — compose as: echo "core msg" | python -m engram.tools.loop_prompt
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Wrap a loop-wake prompt with the SSoT marker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Read the core message from stdin, write the marked prompt to stdout.\n"
            "Example:\n"
            "  echo 'Checking for new work.' | python loop_prompt.py --kind working"
        ),
    )
    parser.add_argument(
        "--kind",
        metavar="KIND",
        default=None,
        help="Loop kind string embedded in the machine-readable meta line (e.g. 'working').",
    )
    args = parser.parse_args()

    core = sys.stdin.read()
    meta: dict = {}
    if args.kind:
        meta["kind"] = args.kind

    print(format_loop_prompt(core, **meta), end="")


if __name__ == "__main__":
    _main()
