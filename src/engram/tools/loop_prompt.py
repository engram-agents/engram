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

CLI — re-arm delay (#3005, #4007):

    delay=$(python3 loop_prompt.py --rearm-delay --wake-kind other)

Prints the integer seconds to pass to ``ScheduleWakeup``.  As of #4007 the
sliding-window contract is in effect: every wake re-arms at the full
``cadence_seconds``; ``next_due_at`` is never written.

Stdlib-only — no third-party deps.  Mirrors the style of tools/presence.py and
tools/_status_derive.py.
"""

import argparse
import html
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

# ---------------------------------------------------------------------------
# SSoT — the single source of truth for the marker string.  The hook imports
# this directly; the fallback constant in the hook must equal this value.
# ---------------------------------------------------------------------------

LOOP_WAKE_MARKER: str = "<loop-wake>"

# The ENGRAM meaningful-span marker (#3613). A loop-wake has no meaningful text
# to recall on, so a FRESH prompt ends with the EMPTY marker (open immediately
# followed by close), which tells the recall hooks to skip recall for the
# message.  U+27E6 / U+27E7 white square brackets, the real characters.
ENGRAM_MARKER_OPEN: str = "\u27e6ENGRAM\u27e7"
ENGRAM_MARKER_CLOSE: str = "\u27e6/ENGRAM\u27e7"
ENGRAM_EMPTY_MARKER: str = ENGRAM_MARKER_OPEN + ENGRAM_MARKER_CLOSE

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
        ⟦ENGRAM⟧⟦/ENGRAM⟧                 ← empty marker, ALWAYS the last line

    The empty ENGRAM marker (#3613) tells the recall hooks a loop-wake has no
    meaningful text to recall on.  It is added only for FRESH input, and only
    when the core does not already contain the open token anywhere (so a core
    that carries its own marker is never given a second one).

    Contract when ``core.lstrip()`` already starts with the marker (i.e. the
    result of a previous ``format_loop_prompt`` call) — the marker itself is
    never added or duplicated again (and no ENGRAM marker is added either — the
    input is taken as already formatted), but what happens to meta differs:

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
    if ENGRAM_MARKER_OPEN not in core:
        parts.append(ENGRAM_EMPTY_MARKER)
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
# Preserve-deadline re-arm (#3005)
#
# The defect: `engram-loop` re-armed `ScheduleWakeup` at the FULL cadence on
# every wake.  Because `ScheduleWakeup` REPLACES the pending wake, a Monitor
# expiry notice (which arrives faster than the declared cadence) pushed the due
# moment out before it could ever come due — the heartbeat never fired.
#
# The fix: a wake that is NOT the heartbeat re-arms for the REMAINING time to
# the original due moment (`next_due_at`, stamped into the marker once and
# preserved thereafter).  The helper below is the SINGLE WRITER of that field,
# so no caller hand-types a timestamp.
# ---------------------------------------------------------------------------

# The marker's canonical UTC timestamp shape — the same convention the
# `activated` field already uses (`date -u +%Y-%m-%dT%H:%M:%SZ`).  Do not
# invent a second shape.
_TS_FORMAT: str = "%Y-%m-%dT%H:%M:%SZ"

# The smallest heartbeat cadence the config field may set: a configured
# `loop.heartbeat_cadence_seconds` below this is rejected with one stderr line and
# the default is used (see configured_cadence_seconds).
_MIN_DELAY_SECONDS: int = 60

# The TEAM default heartbeat cadence (15 minutes, Lei 2026-10-23, replacing his
# 2026-08-21 30-minute default).  Used ONLY when the config field
# ``loop.heartbeat_cadence_seconds`` (see ``configured_cadence_seconds``) is
# absent or invalid -- callers wanting "the cadence" ask the resolver, not this.
DEFAULT_CADENCE_SECONDS: int = 900

CADENCE_CONFIG_FIELD: str = "loop.heartbeat_cadence_seconds"

def default_marker_path() -> str:
    """Default marker path: ``loop-mode.json`` under the ENGRAM home (Step 0 of the engram-loop
    skill owns the absent case).

    Resolved at CALL time through the ONE shared resolver, ``tools/_owner_read.engram_home()``
    (#3863). The import is lazy and only the CLI reaches it: the hooks import this module as a bare
    ``loop_prompt`` (the tools dir on sys.path, no ``tools`` package) for ``LOOP_WAKE_MARKER`` and
    must not gain a sys.path edit or an import that can fail. The parent of this file's directory
    is the package root in BOTH layouts (src/engram/ and the deployed plugin root).
    """
    pkg_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    from tools import _owner_read

    return str(_owner_read.engram_home() / "loop-mode.json")


def configured_cadence_seconds() -> int:
    """The heartbeat cadence from ``loop.heartbeat_cadence_seconds`` in ``$ENGRAM_HOME/config.json``.

    Absent file / ``loop`` / key -> ``DEFAULT_CADENCE_SECONDS``, silently (the
    legitimate unconfigured state).  A PRESENT-but-invalid value (not a positive
    int; ``bool`` is not an int here), a non-object ``loop``, or an unreadable /
    unparseable config.json (RecursionError included) -> the default AND exactly one
    stderr line naming the field (or the file).  A value under ``_MIN_DELAY_SECONDS`` is
    invalid too (the field and the minimum are named).  Never raises.  The home comes from the one shared
    resolver (#3863), resolved at call time like ``default_marker_path``.
    """
    pkg_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    try:
        from tools import _owner_read

        path = _owner_read.engram_home() / "config.json"
    except Exception as exc:  # e.g. a relative ENGRAM_HOME: refuse loudly, keep the loop alive
        sys.stderr.write(
            f"loop_prompt: invalid_config: cannot locate config.json ({exc}) -- using the "
            f"default heartbeat cadence {DEFAULT_CADENCE_SECONDS}s.\n"
        )
        return DEFAULT_CADENCE_SECONDS
    try:
        with open(path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except FileNotFoundError:
        return DEFAULT_CADENCE_SECONDS
    except (OSError, ValueError, RecursionError) as exc:  # JSONDecodeError, UnicodeDecodeError, absurd nesting
        sys.stderr.write(
            f"loop_prompt: invalid_config: cannot read {path} ({type(exc).__name__}) -- using "
            f"the default heartbeat cadence {DEFAULT_CADENCE_SECONDS}s.\n"
        )
        return DEFAULT_CADENCE_SECONDS
    if not isinstance(config, dict):
        sys.stderr.write(
            f"loop_prompt: invalid_config: {path} is not a JSON object -- using the default "
            f"heartbeat cadence {DEFAULT_CADENCE_SECONDS}s.\n"
        )
        return DEFAULT_CADENCE_SECONDS
    if "loop" not in config:
        return DEFAULT_CADENCE_SECONDS
    loop = config["loop"]
    if not isinstance(loop, dict):
        sys.stderr.write(
            f"loop_prompt: invalid_cadence_config: {CADENCE_CONFIG_FIELD}: `loop` is {loop!r} "
            f"(type {type(loop).__name__}), not an object in {path} -- using the default "
            f"{DEFAULT_CADENCE_SECONDS}s.\n"
        )
        return DEFAULT_CADENCE_SECONDS
    if "heartbeat_cadence_seconds" not in loop:
        return DEFAULT_CADENCE_SECONDS
    value = loop["heartbeat_cadence_seconds"]
    if not _is_valid_cadence(value):
        sys.stderr.write(
            f"loop_prompt: invalid_cadence_config: {CADENCE_CONFIG_FIELD} is {value!r} "
            f"(type {type(value).__name__}) in {path} -- using the default "
            f"{DEFAULT_CADENCE_SECONDS}s; set it to a positive integer.\n"
        )
        return DEFAULT_CADENCE_SECONDS
    if value < _MIN_DELAY_SECONDS:
        sys.stderr.write(
            f"loop_prompt: invalid_cadence_config: {CADENCE_CONFIG_FIELD} is {value!r} "
            f"in {path}, below the minimum {_MIN_DELAY_SECONDS}s -- using the default "
            f"{DEFAULT_CADENCE_SECONDS}s.\n"
        )
        return DEFAULT_CADENCE_SECONDS
    return value


@dataclass(frozen=True)
class RearmDecision:
    """The outcome of one re-arm decision.

    ``delay_seconds`` is what to pass to ``ScheduleWakeup``.
    ``next_due_at`` is the ISO-8601 UTC due moment ("YYYY-MM-DDTHH:MM:SSZ") —
    preserved byte-identically when the deadline is kept.
    ``write_next_due_at`` is True iff the helper must stamp the marker.
    ``reason`` is ``"sliding_window"`` for all wakes (#4007).
    """

    delay_seconds: int
    next_due_at: str
    write_next_due_at: bool
    reason: str


def _format_ts(moment: datetime) -> str:
    """Render a tz-aware datetime in the marker's canonical UTC shape."""
    return moment.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _is_valid_cadence(cadence: object) -> bool:
    """True iff *cadence* is a positive ``int`` that is not a ``bool``.

    ``bool`` is excluded explicitly: ``True``/``False`` are ``int`` subclasses
    in Python, so an ``isinstance(cadence, int)`` test alone would read
    ``True`` as the cadence ``1``.
    """
    return (
        isinstance(cadence, int)
        and not isinstance(cadence, bool)
        and cadence > 0
    )


def _cadence_notice(marker: object) -> Optional[str]:
    """Loud-line detail for a PRESENT-but-invalid ``cadence_seconds``, else None.

    Returns a short human-readable descriptor (the offending value and its
    type) when the marker CARRIES ``cadence_seconds`` and it is not a positive
    int; ``None`` when the field is absent (the legitimate pre-upgrade state),
    when the marker is not a mapping, or when the value is valid.  The CLI
    prints this loudly — a present-but-invalid cadence must never be a SILENT
    fallback to the default (the #3005 silent-fallback shape, on the INPUT
    side).
    """
    if isinstance(marker, Mapping) and "cadence_seconds" in marker:
        cadence = marker.get("cadence_seconds")
        if not _is_valid_cadence(cadence):
            return f"cadence_seconds is {cadence!r} (type {type(cadence).__name__})"
    return None


def _resolve_cadence(marker: object, default_cadence: int) -> int:
    """The marker's ``cadence_seconds`` if it is a positive int, else default.

    Anything missing, non-int (including bool and float), or ``<= 0`` falls
    back to ``default_cadence`` (R6).  See ``_cadence_notice`` for the LOUD
    stderr line the CLI emits for a present-but-invalid value.
    """
    if isinstance(marker, Mapping):
        cadence = marker.get("cadence_seconds")
        if _is_valid_cadence(cadence):
            return cadence
    return default_cadence


def _parse_due(raw: object) -> Optional[datetime]:
    """Parse ``next_due_at``; return a tz-aware datetime, or None if invalid.

    Invalid means: not a non-empty ``str``, unparseable as ISO-8601, or naive
    (no UTC offset — a zone-less value must never be guessed).
    """
    if not isinstance(raw, str) or raw == "":
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def compute_rearm(
    marker: Optional[Mapping],
    *,
    now: datetime,
    is_heartbeat: bool,
    default_cadence: int = DEFAULT_CADENCE_SECONDS,
) -> RearmDecision:
    """Return the delay for the next ScheduleWakeup arm.

    Sliding-window contract (#4007): every wake re-arms at the full cadence_seconds
    regardless of wake kind. `next_due_at` is never written.

    The `is_heartbeat` parameter is retained for call-site backward compatibility
    but is ignored — the return value is identical for both values.

    ``now`` MUST be timezone-aware (a naive one raises ``ValueError`` — fail
    loud, never guess).  This function MUST NOT read the clock itself: ``now``
    is passed in, which is what makes every cell of the table testable.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("compute_rearm: `now` must be timezone-aware")

    cadence = _resolve_cadence(marker, default_cadence)

    return RearmDecision(
        delay_seconds=cadence,
        next_due_at=_format_ts(now + timedelta(seconds=cadence)),
        write_next_due_at=False,
        reason="sliding_window",
    )


# ---------------------------------------------------------------------------
# CLI entrypoint — compose as: echo "core msg" | python -m engram.tools.loop_prompt
# ---------------------------------------------------------------------------

def _load_marker_file(path: str) -> dict:
    """Read and parse the marker file, or exit 2 with a named message.

    An ABSENT file is owned by Step 0 of the loop skill, so it is an error
    here; a PRESENT but unparseable file is a real error too.  Neither case
    guesses a cadence.
    """
    if not os.path.exists(path):
        sys.stderr.write(f"loop_prompt: marker file not found: {path}\n")
        sys.exit(2)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        sys.stderr.write(
            f"loop_prompt: marker file is not valid JSON: {path} ({exc})\n"
        )
        sys.exit(2)
    if not isinstance(data, dict):
        sys.stderr.write(f"loop_prompt: marker file is not a JSON object: {path}\n")
        sys.exit(2)
    return data


def _write_next_due_at(path: str, marker: dict, next_due_at: str) -> None:
    """Patch ONLY ``next_due_at`` into the marker, atomically.

    A temp file in the SAME directory + ``os.replace``, so a crash mid-write
    never leaves a truncated marker.  Every other key (and its order) is
    preserved — a wholesale rewrite would silently drop ``next_due_at`` and
    reintroduce the #3005 defect.
    """
    payload = dict(marker)
    payload["next_due_at"] = next_due_at
    # The shared mode- and group-preserving writer (#4013).  Core modules sit at the package root
    # in BOTH layouts (src/engram/ and the deployed plugin root); imported lazily, as in
    # default_marker_path, so importing this module (the hooks do) never gains a failure point.
    pkg_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    from engram_config_io import atomic_write_text

    atomic_write_text(
        path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        follow_symlinks=False,
    )


def _rearm_main(args: argparse.Namespace) -> int:
    """The ``--rearm-delay`` path: print the delay, stamp the marker if needed."""
    marker_path = os.path.expanduser(args.marker or default_marker_path())
    marker = _load_marker_file(marker_path)

    # A PRESENT-but-invalid `cadence_seconds` is a silent-fallback hazard, so
    # it gets its own loud reason token — distinct from the `next_due_at`
    # reasons below, which describe a different field.
    cadence_notice = _cadence_notice(marker)
    # Fallback source for a marker with no valid cadence: the configured value
    # (config field, team default 900).  Resolved only when needed, so a marker
    # that carries its own cadence never triggers a config read or its loud lines.
    fallback = (
        DEFAULT_CADENCE_SECONDS
        if _is_valid_cadence(marker.get("cadence_seconds") if isinstance(marker, Mapping) else None)
        else configured_cadence_seconds()
    )
    if cadence_notice is not None:
        sys.stderr.write(
            f"loop_prompt: invalid_cadence: {cadence_notice} in "
            f"{marker_path} — using the default cadence "
            f"{fallback}s; set cadence_seconds to a positive "
            f"integer.\n"
        )

    decision = compute_rearm(
        marker,
        now=datetime.now(timezone.utc),
        is_heartbeat=(args.wake_kind == "heartbeat"),
        default_cadence=fallback,
    )

    # stdout carries the integer and NOTHING else, so `delay=$(...)` composes.
    sys.stdout.write(f"{decision.delay_seconds}\n")
    return 0


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Wrap a loop-wake prompt with the SSoT marker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Read the core message from stdin, write the marked prompt to stdout.\n"
            "Example:\n"
            "  echo 'Checking for new work.' | python loop_prompt.py --kind working\n"
            "\n"
            "Or compute a sliding-window re-arm delay (#4007):\n"
            "  python loop_prompt.py --rearm-delay --wake-kind other\n"
        ),
    )
    parser.add_argument(
        "--kind",
        metavar="KIND",
        default=None,
        help="Loop kind string embedded in the machine-readable meta line (e.g. 'working').",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--rearm-delay",
        dest="rearm_delay",
        action="store_true",
        help=(
            "Print the integer seconds to pass to ScheduleWakeup for this wake "
            "(sliding-window re-arm, #4007)."
        ),
    )
    parser.add_argument(
        "--wake-kind",
        dest="wake_kind",
        choices=("heartbeat", "other"),
        default="other",
        help=(
            "Whether this wake is the loop heartbeat or an 'other' wake "
            "(Monitor notice, DM, baton flip, human prompt). Default: other."
        ),
    )
    parser.add_argument(
        "--marker",
        metavar="PATH",
        default=None,
        help="Loop marker path (default: loop-mode.json in the ENGRAM home, "
             "i.e. $ENGRAM_HOME/loop-mode.json; resolved only on --rearm-delay).",
    )
    mode.add_argument(
        "--cadence",
        action="store_true",
        help="Print the configured heartbeat cadence in seconds as a bare integer "
             "(config field loop.heartbeat_cadence_seconds, team default 900).",
    )
    args = parser.parse_args()

    if args.cadence:
        sys.stdout.write(f"{configured_cadence_seconds()}\n")
        sys.exit(0)

    if args.rearm_delay:
        sys.exit(_rearm_main(args))

    core = sys.stdin.read()
    meta: dict = {}
    if args.kind:
        meta["kind"] = args.kind

    print(format_loop_prompt(core, **meta), end="")


if __name__ == "__main__":
    _main()
