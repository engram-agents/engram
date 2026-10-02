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
import html
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

    Contract when ``core.lstrip()`` already starts with the marker (i.e. the
    result of a previous ``format_loop_prompt`` call) — the marker itself is
    never added or duplicated again, but what happens to meta differs:

    - No meta kwargs given: the result is the lstripped input, byte-for-byte
      (today's / original behaviour, trailing whitespace included).
    - Meta kwargs given: the result carries exactly one meta line, built the
      same way as the fresh-input case above. If line 2 of the input is
      already a ``<loop-meta ...>`` line, it is REPLACED by the new one (old
      and new keys are never merged); otherwise the new meta line is
      INSERTED right after the marker line. Everything else — the rest of
      the input, including its trailing whitespace/newline — is preserved
      byte-for-byte (gh#3309).

    Parameters
    ----------
    core:
        The human-readable loop message.  Typically the loop's current intent
        or status.
    **meta:
        Optional machine-readable metadata (e.g. ``kind="working"``,
        ``seq=3``).  Embedded as JSON on the second line when present — even
        when the input is already marked (see contract above).
    """
    stripped_core = core.lstrip()

    # Idempotency guard: already starts with the marker — don't double-add.
    if stripped_core.startswith(LOOP_WAKE_MARKER):
        if not meta:
            return stripped_core

        meta_line = f"{_META_PREFIX}{json.dumps(meta, separators=(',', ':'))}{_META_SUFFIX}"

        if "\n" not in stripped_core:
            # Bare marker, no newline at all — nothing else to preserve.
            return stripped_core + "\n" + meta_line

        marker_line, after_marker = stripped_core.split("\n", 1)

        if "\n" in after_marker:
            second_line, rest = after_marker.split("\n", 1)
        else:
            second_line, rest = after_marker, None

        if second_line.startswith(_META_PREFIX):
            # Replace the existing meta line in place — never merge old and
            # new keys.
            if rest is None:
                return marker_line + "\n" + meta_line
            return marker_line + "\n" + meta_line + "\n" + rest

        # No existing meta line — insert the new one right after the marker
        # line, preserving everything that followed the marker byte-for-byte.
        return marker_line + "\n" + meta_line + "\n" + after_marker

    parts = [LOOP_WAKE_MARKER]
    if meta:
        parts.append(f"{_META_PREFIX}{json.dumps(meta, separators=(',', ':'))}{_META_SUFFIX}")
    parts.append(core)
    return "\n".join(parts)


def is_loop_wake(body: str) -> bool:
    """Return True if ``body`` starts with the loop-wake marker.

    Strips leading whitespace before checking, so slight prompt indentation or
    leading newlines don't defeat the detection.

    Also HTML-unescapes ``body`` before checking, so a marker that was
    mangled into its HTML-escaped form (e.g. an agent hand-constructing
    ``&lt;loop-wake&gt;`` instead of real angle brackets when composing a
    ``ScheduleWakeup`` prompt) still classifies identically to the plain
    marker. This is effect-layer robustness: an agent-construction error in
    how the marker gets embedded can't silently defeat detection and get
    misclassified as a human prompt (gh#1649).

    Parameters
    ----------
    body:
        The raw prompt body string (e.g. ``payload.get("prompt", "")``).
    """
    return html.unescape(body).lstrip().startswith(LOOP_WAKE_MARKER)


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
