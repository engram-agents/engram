"""emit_context()'s `truncated_first` shape must honour its ceiling even
when the in-band marker is what pushes it over (engram-alpha#2899).

The defect: `_render_truncated_first()` sized the single truncated item
against `ceiling - overhead`, clamped at 0. When trailer + head + marker
alone exceeded the ceiling (Stage 1 sizes the head against the trailer
only and never reserves the marker), the clamp hid the shortfall and the
render ran past `target` (the soft ceiling) by it. The hard `budget` was
never exceeded -- a backstop clamps -- so only the soft ceiling was wrong.

Contract (written before the fix):
  C1 soft ceiling: allow_single_overflow=False -> len(out) <= target for
     every head length (uniform and ragged lines), with a path-returning
     writer AND with overflow_writer=None.
  C1b the same, swept over TARGET as well as head length (a fixed target
     fixes the trim room, so a join-accounting slip can hide behind it).
  C2b overflow-allowed with a tight budget: the in-band marker survives.
  C2 hard ceiling in the overflow-allowed shape -> len(out) <= budget.
  C3 line boundary + honesty: a re-trimmed head is a prefix of the original
     ending at a newline, and the trailer says `head trimmed`.
  C4 no change when nothing overshoots (short head: unchanged shape,
     no `head trimmed` added).
  C5 writer failure (raises / returns "") -> `…[truncated]` marker, still
     within target, never raises.
  C6 floor: target too small for trailer + marker even with an empty head
     -> no raise, within budget, head empty (target overshoot is
     unavoidable there and is deliberately NOT asserted).
"""

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).parent.parent
    / "src" / "engram" / "hooks" / "claude" / "_hooklib.py"
)
PATH = "/home/someone01/.engram/overflow/tripwire-20261011T122600Z-abcdef.txt"
RAGGED = (7, 23, 41, 3, 60, 18)


@pytest.fixture(scope="module")
def H():
    spec = importlib.util.spec_from_file_location("_hooklib_2899", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Stage 1 sizes the head against a PLACEHOLDER overflow path built from ENGRAM_HOME and the
# process id (`_placeholder_overflow_path`), so its length differs per environment: tests/conftest.py
# sets a throwaway ENGRAM_HOME, a developer shell has none, and the pid has 4-7 digits. Which
# join-accounting slips a sweep can see depends on that length, so the tests pin it (default 60) and
# the discriminating cells sweep several. The fix's contract (len <= ceiling) holds for every length.
PLACEHOLDER_LENS = (45, 60, 90)


def pin_placeholder(H, monkeypatch, n):
    monkeypatch.setattr(H, "_placeholder_overflow_path", lambda: "/" + "q" * (n - 1))


@pytest.fixture(autouse=True)
def _default_placeholder(H, monkeypatch):
    pin_placeholder(H, monkeypatch, 60)


def uniform_head(length):
    return "".join(("x" * 39 + "\n") for _ in range(length // 40 + 1))[:length]


def ragged_head(length):
    out, i = [], 0
    while sum(len(s) + 1 for s in out) < length + 1:
        out.append("r" * RAGGED[i % len(RAGGED)])
        i += 1
    return ("\n".join(out) + "\n")[:length]


HEADS = {"uniform": uniform_head, "ragged": ragged_head}


def writer(items):
    return PATH


def failing_writer(items):
    raise OSError("disk full")


def empty_writer(items):
    return ""


def run(H, head, *, target=500, budget=900, aso=False, w=writer, n=30):
    return H.emit_context(
        head, ["B" * 900] * n, budget=budget, target=target,
        allow_single_overflow=aso, overflow_writer=w,
    )


SHORT_PATH = "/h/" + "p" * 17   # 20 chars: a short overflow path, unlike the 69-char test PATH


def short_writer(items):
    return SHORT_PATH


# The trim room depends on the path length (it sits in the trailer and the marker), and two
# live hooks call emit_context(overflow_writer=None), where the worst-case trailer carries
# no path at all. A one-character slip in the join accounting is only visible for some
# (path length, head shape) pairs: it shows with no writer and with a 20-char path, not with
# the 69-char path. So the contract is exercised over all three.
WRITERS = {"path-writer": writer, "short-path-writer": short_writer, "no-writer": None}


@pytest.mark.parametrize("plen", PLACEHOLDER_LENS)
@pytest.mark.parametrize("wname", WRITERS)
@pytest.mark.parametrize("kind", HEADS)
def test_c1_soft_ceiling_every_head_length(H, kind, wname, plen, monkeypatch):
    pin_placeholder(H, monkeypatch, plen)
    bad = []
    for length in range(0, 700):
        out = run(H, HEADS[kind](length), w=WRITERS[wname])
        if len(out) > 500:
            bad.append((length, len(out)))
    assert not bad, f"over target 500 at {len(bad)} head lengths, first: {bad[:3]}"


@pytest.mark.parametrize("plen", PLACEHOLDER_LENS)
@pytest.mark.parametrize("wname", WRITERS)
@pytest.mark.parametrize("kind", HEADS)
def test_c1b_soft_ceiling_every_target(H, kind, wname, plen, monkeypatch):
    pin_placeholder(H, monkeypatch, plen)
    # C1 varies the head length at ONE target, so the trim room it exercises is a
    # single number and an off-by-one in the join accounting can hide behind
    # line-boundary alignment. Varying the target moves the room every step.
    # Floor: trailer + marker with an empty head is ~205 chars, well under 260.
    bad = []
    for length in (0, 90, 130, 200, 310, 400, 600):
        for target in range(260, 700):
            out = run(H, HEADS[kind](length), target=target, budget=900, w=WRITERS[wname])
            if len(out) > target:
                bad.append((length, target, len(out) - target))
    assert not bad, f"over target at {len(bad)} (head, target) pairs, first: {bad[:3]}"


@pytest.mark.parametrize("kind", HEADS)
def test_c2_hard_ceiling_when_single_overflow_allowed(H, kind):
    bad = []
    for length in range(0, 700):
        out = run(H, HEADS[kind](length), target=300, budget=500, aso=True)
        if len(out) > 500:
            bad.append((length, len(out)))
    assert not bad, f"over budget 500 at {len(bad)} head lengths, first: {bad[:3]}"


@pytest.mark.parametrize("kind", HEADS)
def test_c2b_overflow_allowed_keeps_the_marker_inside_the_budget(H, kind):
    # With allow_single_overflow=True the ceiling is `budget`. Before the fix a head that
    # filled the budget left no room for the in-band marker, and the hard-budget backstop
    # then cut the render INSIDE the head: the item and the marker (the pointer to the
    # overflow file) were gone entirely, not just late. The marker must survive.
    lost = []
    for length in range(0, 700):
        out = run(H, HEADS[kind](length), target=400, budget=420, aso=True)
        assert len(out) <= 420, (length, len(out))
        if not out.endswith(f"full text in {PATH}]"):
            lost.append(length)
    assert not lost, f"marker lost at {len(lost)} head lengths, first: {lost[:3]}"


@pytest.mark.parametrize("kind", HEADS)
def test_c3_head_prefix_on_line_boundary_and_trailer_says_trimmed(H, kind):
    for length in range(0, 700):
        original = HEADS[kind](length)
        out = run(H, original)
        lines = out.split("\n")
        # shape here: trailer, head lines..., item+marker (single line)
        head = "\n".join(lines[1:-1])
        assert original.startswith(head), length
        if head != original:
            assert original[len(head)] == "\n", f"mid-line cut at {length}"
            assert "head trimmed" in lines[0], f"silent head cut at {length}"


def test_c4_short_head_long_item_unchanged_shape(H):
    head = "short head\nsecond line"
    out = run(H, head)
    lines = out.split("\n")
    assert "head trimmed" not in lines[0]
    assert lines[1:3] == ["short head", "second line"]
    assert lines[-1].endswith(f"…[truncated, full text in {PATH}]")
    # the item is cut to exactly fill the ceiling when nothing else interferes
    assert len(out) == 500


@pytest.mark.parametrize("w", [failing_writer, empty_writer])
@pytest.mark.parametrize("kind", HEADS)
def test_c5_writer_failure_short_marker_within_target(H, kind, w):
    for length in range(0, 700):
        out = run(H, HEADS[kind](length), w=w)
        assert len(out) <= 500, length
        assert out.endswith("…[truncated]"), length


def test_c6_floor_no_raise_within_budget_empty_head(H):
    head = uniform_head(400)
    out = run(H, head, target=150, budget=900)
    lines = out.split("\n")
    assert len(out) <= 900
    assert "head trimmed" in lines[0]
    assert len(lines) == 2, "head must be empty at the floor"
    assert lines[1].endswith(f"…[truncated, full text in {PATH}]")


def test_c4_other_shapes_do_not_reserve_the_marker(H):
    """The fix lives inside the truncated_first render only. A call that
    never truncates an item (here: items fit, some cut) must size its head
    exactly as before -- reserving the marker in Stage 1 would trim heads
    in every shape."""
    head = uniform_head(330)
    body = ["i" * 20] * 3 + ["J" * 900] * 5
    out = H.emit_context(
        head, body, budget=900, target=500,
        allow_single_overflow=False, overflow_writer=writer,
    )
    lines = out.split("\n")
    assert "head trimmed" not in lines[0]
    assert head in out  # whole head kept, uncut
    assert "truncated" not in out
    assert len(out) <= 500
