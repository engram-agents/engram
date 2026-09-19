"""Per-harness hook-injection budgets (engram-alpha#2863 WS1).

Lei ruled (2026-09-12, quoted on the issue): every ENGRAM hook's
`additionalContext` injection must be (1) bounded under a named per-harness
budget, (2) never load content of unpredictable size, (3) carry its
highest-value summary in the first ~2,000 characters, because the harness
silently truncates the TAIL of an oversized injection and different
harnesses (Codex, DeepSeek) have different ceilings.

This module is the harness-adapter half of the fix: one dict per harness,
selected by `get_harness_budgets(harness)`. The companion helper,
`_hooklib.emit_context()`, is the mechanism half -- it renders a hook's
content within the numbers this module returns. See that function's
docstring for the render contract; this module is only the numbers table
plus lookup.

Design spec: Borges's comment on the issue (2026-09-12T14:02:48Z, S2) +
Kepler's Amendment 2 (2026-09-12T14:31:26Z) -- read those two comments in
full before changing this file; this module is a direct transcription of
that already-approved design, not a re-derivation of it.

WHY `head_budget` IS STILL A PER-HARNESS NUMBER, EVEN THOUGH `emit_context()`
NO LONGER TAKES A `head_budget` PARAMETER (Kepler's Amendment 1 renamed the
per-hook soft ceiling to `target`, scoped `allow_single_overflow` to it, and
made `budget` the only harness-sourced number in emit_context()'s own
signature): `head_budget` here is NOT the same concept as a hook's `target`.
It is the harness's own PHYSICAL preview-truncation boundary (Borges's S2:
"the cut is min(2,000 chars, last complete line under ~2,048 bytes)") --
a fact about how a given harness renders a PreToolUse/UserPromptSubmit hook's
additionalContext, independent of what any individual hook chooses as its
own economy target. It is not passed into emit_context() (Part B of the WS1
spec is explicit there is no third parameter); it is exposed here so a
caller choosing its own `target` can pick one that is safely <= the current
harness's head_budget when it wants the "trailer+head survives the inline
preview" guarantee -- exactly what the tripwire hook does when it folds onto
the helper (its own soft target of 1800 happens to equal claude-code's
head_budget here, per Borges's own margin analysis; that equality is by
design, not a coincidence emit_context() enforces automatically).

MEASURED vs FLOOR (Kepler's Amendment 2): only `claude-code` has measured
constants today. Codex/DeepSeek must NEVER inherit claude-code's numbers --
`get_harness_budgets()` returns `UNMEASURED_HARNESS_FLOOR` with
`measured: False` for anything not in `HARNESS_BUDGETS`, so a caller can
name the deficit out loud (Kepler's suggested wording: a trailer/head suffix
like "(unmeasured harness floor)") instead of silently trusting an
unmeasured number.
"""

from __future__ import annotations

import json
import os

# claude-code: budget = 9,500 sits 134 chars under the lowest measured
# persist threshold (band 9,634 < t <= 10,184 chars, four seats -- Borges
# S2). head_budget = 1,800 clears both halves of the harness's own cut rule
# (<= 2,000 chars, and measured byte-expansion ratios keep it under 2,048
# bytes too). Do NOT add codex/deepseek entries here with guessed numbers --
# Aleph's off-host probe (gated on his owner's word) is the only path to a
# measured entry for either; until then they fall through to
# UNMEASURED_HARNESS_FLOOR below.
HARNESS_BUDGETS = {
    "claude-code": {"budget": 9_500, "head_budget": 1_800},
}

# Conservative numeric floor for any harness without a measured entry
# (Kepler's Amendment 2 -- "an unset value is untestable and fails silent at
# import"). Well under claude-code's own numbers on both axes so a caller
# using this floor is never accidentally as generous as a measured harness.
UNMEASURED_HARNESS_FLOOR = {"budget": 4_000, "head_budget": 1_200}


def get_harness_budgets(harness: str) -> dict:
    """Return {"budget": int, "head_budget": int, "measured": bool} for the
    given harness name.

    `harness` is matched case-insensitively, with surrounding whitespace
    stripped (defensive -- callers derive this string from config/env/a
    baked platform.json, per detect_harness() below, and none of those
    sources is guaranteed to hand back an exact-cased "claude-code").

    Falls back to UNMEASURED_HARNESS_FLOOR (measured=False) for any harness
    not in HARNESS_BUDGETS -- NEVER silently inherits claude-code's numbers
    for an unmeasured harness (Amendment 2). Never raises: an empty string,
    None-coerced-to-empty, or any other unrecognized value all resolve to
    the floor rather than raising or KeyError-ing.
    """
    try:
        key = (harness or "").strip().lower()
    except Exception:
        key = ""
    entry = HARNESS_BUDGETS.get(key)
    if entry is not None:
        return {**entry, "measured": True}
    return {**UNMEASURED_HARNESS_FLOOR, "measured": False}


def detect_harness() -> "str | None":
    """Return the baked runtime platform name, or None when unavailable.

    Mirrors the existing cross-harness detection convention already used
    elsewhere in this repo (`_runtime_platform()` in
    engram-session-start-hook.py and engram-loop-arm-stop-hook.py) rather
    than inventing a new one: plugin builds bake a `platform.json` at the
    runtime root (`{"platform": "claude-code" | "codex" | ...}`,
    src/build/engine/build.py's bake_platform_json()); source-tree/dev
    execution generally has no platform.json, so this returns None there.

    This file (`_harness.py`) lives in the SAME directory as `_hooklib.py`
    and the hooks that call it, in both build topologies (source:
    `hooks/claude/`; deployed: `hooks/` -- the plugin build flattens the
    `claude/` level away, per this repo's CLAUDE.md), so the runtime root
    is one dirname() up from this file's own location, exactly as the two
    existing copies of this probe compute it.

    Best-effort / advisory, like both existing copies: any read/parse
    failure (missing file, malformed JSON, non-string platform field)
    returns None rather than raising. The result is a plain string (e.g.
    "claude-code", "codex"), lowercased+stripped, ready to pass straight
    into get_harness_budgets() above.
    """
    try:
        hook_dir = os.path.dirname(os.path.abspath(__file__))
        runtime_dir = os.path.dirname(hook_dir)
        platform_path = os.path.join(runtime_dir, "platform.json")
        with open(platform_path, encoding="utf-8") as f:
            data = json.load(f)
        platform = data.get("platform")
        return platform.strip().lower() if isinstance(platform, str) else None
    except Exception:
        return None
