"""health.version — the single canonical home for health-v2's own version
identity (engram-alpha#2537, Lei's amendment 2: "please make it very
differentiate from our Engram versioning, otherwise it's very confusing").

## The collision this module exists to close

Measured live (Ariadne, on her own seat, same snapshot JSON):

    ENGRAM plugin build     0.5.0-dev.20260830231757.4ae2b947
    health ENGINE_VERSION   0.4.0-pr5      <- same dotted shape, reads ONE
                                              MINOR BEHIND the plugin build

Both strings appear together in the same snapshot. A reader has every
reason to think ``0.4.0-pr5`` is a stale ENGRAM build rather than a
deliberately-independent health-spec version. There was also a third,
unrelated token (``SCHEMA_VERSION``) whose name didn't say what it
versioned.

## The fix: one token, positional major.minor, a different shape entirely

``health-v<MAJOR>.<MINOR>`` — e.g. ``health-v1.0``. Three things make
confusion structurally hard rather than merely unlikely: the literal
``health-v`` prefix, TWO numbers where ENGRAM's has three plus a build
stamp, and a different clock (this only moves on health-spec changes, never
on an ENGRAM release).

- **MAJOR = the L0 bare-data set's identity.** Bumps when a measurement is
  added, removed, or redefined. Snapshots across a MAJOR boundary are not
  fully reconstructible (see the L0 hash gate in
  ``tests/test_health_measurements.py``, which makes this bump
  self-enforcing rather than remembered — the exact gap that let
  ``CLAIM_BEARING_TYPES`` change on 2026-08-26 under a commit titled about
  confidence, with nothing to catch it).
- **MINOR = everything derived** — formulas, curves, importances, aspect
  points, the empty-denominator/unavailability policy. Snapshots are FULLY
  reconstructible across any MINOR change; that retro-applicability is this
  whole refactor's point.

Starts at ``health-v1.0`` regardless of ENGRAM's own version, and never
tracks it — a coincidence of two independent counters landing on the same
digits some day is a coincidence, not a re-synchronization to chase.

## Positional, not named — the one trade worth stating (Borges, #2537)

``health-v1.0`` requires the reader to know slot 1 is data and slot 2 is
spec. The unambiguous alternative is a named form like
``health-data1-spec0``, which nobody enjoys typing. Positional wins because
major/minor semver is already the most widely understood versioning
convention there is — the trade is real, not free, and this is the
deliberate choice, not an oversight.

## Scope (slice 1, #2537) — the token exists; the retirement doesn't, yet

This PR is the L0 measurement layer, purely additive. It mints this module
and points ``health/measurements.py``'s own L0-identity stamp at
``MAJOR`` here (replacing what would otherwise have been a throwaway local
constant — Ariadne's build-lead call: "do not ship the identifier we are
retiring, because anything written in the window carries it"). It does
**not** touch ``health.config.EngineConfig``'s existing
``schema_version``/``engine_version`` fields, or ``health/engine.py``'s
``CONFIG`` construction — retiring those two fields into this one token is
real, separate blast radius (a change to the top-level snapshot's own
schema, not to the purely-additive L0 layer) and stays out of scope here.
"""

from __future__ import annotations

MAJOR = 2
"""Identity of the current L0 (bare-data) measurement set. Bump this
whenever a measurement in ``health.measurements.MEASUREMENTS`` is added,
removed, or redefined — enforced mechanically by the L0 hash gate
(``tests/test_health_measurements.py``), not left to memory. Bumped to 2
by engram-alpha#2905 PR-B: the L0 set itself changed — two measurements
retired (``derivations_pending_supersede_marker``,
``claim_bearing_pending_retract_marker``) and one added
(``claim_bearing_pending_marker``, unifying both into a single DISTINCT
count over claim-bearing nodes carrying EITHER a pending retract OR a
pending supersede cascade marker), which is exactly the add/remove/
redefine class this counter exists to track — a MAJOR bump, not MINOR,
because the bare-data set's own identity changed, not just what is
derived from it. MINOR resets to 0 on any MAJOR bump (see MINOR's own
docstring)."""

MINOR = 0
"""Identity of everything DERIVED from L0 — formulas, curves, importances,
aspect points, the unavailability policy. Started at 0 (slice 1 introduced
no derived-layer changes); bumped to 1 by engram-alpha#2788 (curve knots
moved to a versioned data file, curves.json, and reshaped to Lei's
placeholder identity/``1 - x`` curves) — a curve reshape is exactly the
class of change this counter exists to track, per the derived-layer hash
gate in tests/test_health_measurements.py. Bumped to 2 by engram-alpha#2905
PR-A (the 2026-09-13 Lei+Kepler curves session): two new curves added to
curves.json (``badness_pending_premise``, ``goodness_coverage``) and six
metric-binding changes in health/model.py's ``MODEL_SPEC`` — three curve
rebinds (``taint_rate``/``stale_derivation_rate`` -> ``badness_pending_
premise``, and ``embedding_coverage``/``recall_summary_coverage``/
``recall_keywords_coverage`` -> ``goodness_coverage``), one importance
hold-out (``open_contradiction_rate`` -> 0.0), and two importance reweights
(``recall_summary_coverage`` -> 0.5, ``recall_keywords_coverage`` ->
0.25). RESET to 0 by engram-alpha#2905 PR-B's MAJOR bump (see MAJOR's own
docstring) — a MAJOR bump always resets MINOR (confirmed against
``tests/test_health_version_gate.py``'s own ``classify_delta`` test cases,
e.g. its "skips a major"/(1,0)->(3,0) case family): the L0 set changing
means everything derived from it starts a fresh identity too, even though
PR-B's own derived-layer declarations (signals/model bindings) ALSO
changed in the same commit (the taint_rate/stale_derivation_rate ->
pending_premise_rate signal and metric-binding unification) — that
derived-layer change does not get its own separate MINOR increment because
it is subsumed by the MAJOR bump's reset, not layered on top of it."""


def format_version(major: int = MAJOR, minor: int = MINOR) -> str:
    """Render ``(major, minor)`` as the canonical ``health-v<M>.<N>``
    string. Defaults to the CURRENT module-level version; explicit
    arguments let a caller format a DIFFERENT (e.g. historical, from an old
    snapshot) major/minor pair through the same one parser/formatter pair
    this module owns — "one stored fact, one parser" (Borges, #2537), never
    a second ad-hoc string-builder growing elsewhere."""
    return f"health-v{major}.{minor}"


def parse_version(version: str) -> tuple[int, int]:
    """Parse a ``health-v<M>.<N>`` string back into ``(major, minor)``.

    Raises ``ValueError`` (not a silent ``None``/default) on anything that
    doesn't match the exact canonical shape — a malformed or foreign
    version string (e.g. an ENGRAM plugin-build string, which is exactly
    the confusion this module exists to prevent) must fail loudly here,
    not be coerced into a plausible-looking ``(0, 0)``.
    """
    prefix = "health-v"
    if not version.startswith(prefix):
        raise ValueError(
            f"not a health.version string (missing {prefix!r} prefix): {version!r}"
        )
    rest = version[len(prefix):]
    parts = rest.split(".")
    if len(parts) != 2:
        raise ValueError(
            f"not a health.version string (expected exactly one '.' after "
            f"{prefix!r}, got {len(parts) - 1}): {version!r}"
        )
    major_str, minor_str = parts
    if not (major_str.isdigit() and minor_str.isdigit()):
        raise ValueError(
            f"not a health.version string (major/minor must be plain "
            f"non-negative integers): {version!r}"
        )
    return int(major_str), int(minor_str)


CURRENT = format_version()
"""The current module's version as a formatted string — ``"health-v1.0"``
today. Import this (not a hand-formatted string) at every call site that
needs the current version literally, so a future MAJOR/MINOR bump here is
the only edit required anywhere in the codebase."""


# ---------------------------------------------------------------------------
# Module-layout prefix canonicalization (engram-alpha#3122, round 2)
# ---------------------------------------------------------------------------
#
# A `health.*` function's `__module__` (and, by extension, any dotted
# `health.*` name derived from it -- a serialized signal_fn/scoring_fn path,
# a declared-blob leaf string) depends on how the `health` package was
# imported: `engram.health.metrics` from a `src/`-tree checkout (top-level
# package `engram` on sys.path), `health.metrics` in the deployed plugin
# (the build flattens `src/engram/` away -- see this repo's CLAUDE.md
# "Plugin build restructures code paths"). This was previously two
# independent copies of the same rule -- `health/engine.py`'s
# `_dotted_name` (operating on a MODULE name) and `health/series.py`'s
# `_normalize_layout_prefix` (operating on an arbitrary dotted STRING value
# inside a recursively-walked JSON structure) -- each with its own copy of
# the prefix constants. `canonical_health_dotted` below is the ONE shared
# implementation both now route through.
_SRC_TREE_HEALTH_PREFIX = "engram.health."
_CANONICAL_HEALTH_PREFIX = "health."


def canonical_health_dotted(name: str) -> str:
    """Canonicalize a dotted ``health.*`` name to the deployed-layout form,
    collapsing the two import-layout variants engram-alpha#3122 diagnosed.

    Subsumes both call shapes this rule has: a full
    ``f"{fn.__module__}.{fn.__qualname__}"`` dotted name (``health/
    engine.py``'s ``_dotted_name`` -- a leading ``engram.health.`` prefix on
    the module carries through into the joined string, so stripping it
    there is equivalent to stripping it here), and an arbitrary leaf string
    value from a recursively-walked JSON structure (``health/series.py``'s
    ``_normalize_layout_prefix``, which may hand this a bare module name
    with no qualname suffix at all).

    - ``name`` starting with the literal ``"engram.health."`` prefix: strip
      the leading ``"engram."``, so ``"engram.health.metrics.x"`` ->
      ``"health.metrics.x"``.
    - ``name`` exactly equal to the bare module name ``"engram.health"``
      (no trailing dot, no qualname suffix) -> ``"health"`` -- the edge case
      ``_dotted_name`` used to special-case separately (``module ==
      "engram.health"``), kept here so this one function subsumes it too.
    - Anything else (already canonical, or unrelated to ``health.*`` at
      all) is returned unchanged.
    """
    if name.startswith(_SRC_TREE_HEALTH_PREFIX):
        return _CANONICAL_HEALTH_PREFIX + name[len(_SRC_TREE_HEALTH_PREFIX):]
    if name == "engram.health":
        return "health"
    return name
