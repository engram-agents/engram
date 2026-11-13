"""#4686 — the two hooks' non-human prompt vocabularies must not drift.

The deference detector and the time-bar hook each keep their OWN inline
``_NON_HUMAN_PREFIXES`` tuple, on purpose: engram-time-bar-hook.py's header says
its constants are inline because it must still run when a sibling lib cannot
import, and #4686 does not reverse that. What was missing was anything
comparing the two lists, so five real machine-sent prompt shapes (task
notifications, slash-command blocks, local-command blocks, and the
cross-session peer preamble) were treated as human messages by the detector.
Three earlier members of the same class were fixed one at a time (#2427,
#2438, #2451); this file closes the class: a member added to one tuple and not
the other reddens the equality pin.

The behaviour table feeds LITERAL prompt strings, never the constant: a table
generated from ``_NON_HUMAN_PREFIXES`` would move with the defect and prove
only that the code agrees with itself.
"""

import importlib.util
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).parent.parent / "src" / "engram" / "hooks" / "claude"


def _load(filename: str, name: str):
    """Load a hook module by path, the way the sibling hook tests do."""
    spec = importlib.util.spec_from_file_location(name, _HOOKS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


deference = _load("engram-deference-detector-prompt.py", "nonhuman_vocab_deference_4686")
time_bar = _load("engram-time-bar-hook.py", "nonhuman_vocab_time_bar_4686")


def test_non_human_prefix_tuples_are_equal():
    """Both inline vocabularies must be identical tuples (same order, same members).

    This is the cell that would have failed at every point in the last three
    incidents: nothing compared the two lists.
    """
    d = deference._NON_HUMAN_PREFIXES
    t = time_bar._NON_HUMAN_PREFIXES
    only_d = sorted(set(d) - set(t))
    only_t = sorted(set(t) - set(d))
    assert d == t, (
        "non-human prompt vocabularies drifted between the deference detector "
        f"and the time-bar hook; only in deference: {only_d!r}; only in time-bar: {only_t!r}"
    )


# Literal inputs from #4686's table. Do NOT build these from _NON_HUMAN_PREFIXES.
_BEHAVIOUR_TABLE = [
    # the five shapes the detector previously missed
    ("<task-notification><task-id>x</task-id></task-notification>", True),
    ("<command-name>/foo</command-name>", True),
    ("<command-message>bar</command-message>", True),
    ("<local-command>ls</local-command>", True),
    ("Another Claude session sent a message: hi", True),
    # regressions: already classified before this change
    ("Stop hook feedback: ...", True),
    ("Loop continuation. Read ...", True),
    # the three shapes that used to be per-shape branches in _is_cron_prompt
    # rather than vocabulary members (#4719 round 2) — machine-sent, and now
    # tuple members like every other shape.
    ("Loop continuation. Read the marker.", True),
    ("<<autonomous-loop>>", True),
    ("<<autonomous-loop-dynamic>>", True),
    # mid-text negative: CONTAINS an envelope but does not START with it.
    # This row is what distinguishes a prefix rule from a containment rule —
    # a human sentence quoting a tag must stay human.
    ("I saw <task-notification> in the log, what does it mean?", False),
    # success control INSIDE the same table: a detector that answered True to
    # everything would satisfy every row above.
    ("what did you work on today?", False),
]


@pytest.mark.parametrize(("prompt", "expected"), _BEHAVIOUR_TABLE)
def test_is_cron_prompt_classifies_literal_prompt(prompt, expected, tmp_path):
    """_is_cron_prompt's vocabulary, asserted on literal strings."""
    # Nonexistent marker file: the stored-loop_prompt branch swallows OSError,
    # so each row exercises the prefix logic alone.
    loop_marker = str(tmp_path / "loop-mode.json")
    assert deference._is_cron_prompt(prompt, loop_marker) is expected
