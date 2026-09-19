"""health.measurements — the L0 measurement layer (engram-alpha#2537,
PR1 of 3 of the health-v2 SSoT refactor; #2537 is itself part of #1991).

Design-of-record: engram-alpha#2537 (Borges, design intent; Lei's charge
quoted verbatim at the top), Ariadne's builder's-read comment (five seams),
Borges's amendment (accepts four seams, corrects one count, generalizes two
into one rule), and Ariadne's post-brief amendment DM (the ``raw_schema_
version`` stamp moves into this slice; the acceptance criterion's two halves
must be paired; both sides of an equality check must read one connection).

Lei's charge, quoted in #2537: "I want the snapshot to keep all the raw
signals and their raw graph counts ... the first hand raw data where each
signal COMPUTE from ... Raw data are measured directly from graph, that's
the bare bone of our whole health system ... The snapshot should keep the
bare data, and then all the other things are COMPUTED based on a given
version of the health spec, fully derived from bare data."

Scope of THIS module: the L0 measurement layer only — PURELY ADDITIVE.
``build_snapshot`` gains a new ``measurements`` block; nothing about
``signals``/``scores``/``aspects``/``composed`` changes in this slice. Signal
functions in ``health/metrics.py`` are UNTOUCHED — they keep computing
their own SQL exactly as they do today. Slice 2 (#2537, not this PR) makes
signal functions formulas over these measurements instead of independent SQL;
slice 3 wires read-time derivation and the two version axes. Do not extend
this module's scope to either of those — see #2537's own "do not start PR2
inside PR1" line.

## Vocabulary (pinned by the design, #2537's own note — do not vary it)

L0 = ``measurements`` (this module). L1 = ``signals``. L2 = ``scores``.
L3 = aspect scores. L4 = ``composed``.

## The boundary rule this module exists to enforce

Every L0 measurement is a plain, unconditional read off the graph — a COUNT,
a boolean environment fact, or (for the one case below) a resolved list.
**Unavailability is L1, decided from L0** — never decided here. A
zero-population graph simply measures 0; nothing in this module ever returns
health.config.UNAVAILABLE. (This is the L0/L1 line's whole point: draw
it here and a formula change downstream stays a minor, retro-applicable
bump.)

**The empty-denominator policy is the one place a decomposition can get
every non-zero count right and still be wrong.** Whoever writes L1's
formulas over these measurements (slice 2) has to reproduce each signal's
own empty_denominator policy exactly, not approximate it with a convenient
placeholder — a synthetic test fixture built with every denominator
non-zero (the natural first instinct, and this PR's own acceptance fixture
does exactly that on purpose, for reproducibility) will never exercise this
branch at all, so getting it wrong costs nothing on that fixture. Only a
real graph, or a fixture deliberately built with an empty population,
catches it. See tests/test_health_measurements.py's PR body
citation for a real instance of exactly this mistake, caught on a live
graph run — not a hypothetical warning.

## The remote-definition rule (Borges's generalization, closing the class)

    Every L0 measurement stores the resolved value of any definition input
    that lives outside the health engine's own files.

AST-enumerated (Borges, #2537 comment) as closed for today's code — exactly
three live cross-module symbols in ``health/metrics.py`` (the
PRE-EXISTING signal-function file, not this module): ``engram_core.
CLAIM_BEARING_TYPES`` (5 call sites there, backing 6 of the 13 L0
measurements below — the shared ``claim_bearing_current`` denominator plus
its 5 numerator siblings) and ``engram_core._embedder`` / ``engram_core.
_get_embedding_config`` (1 call site, one conjunction). Concretely, two
consequences beyond the 13 integers a naive reading of the L0 set would
produce:

- ``claim_bearing_types_resolved`` — the sorted CLAIM_BEARING_TYPES list
  itself, resolved at measurement time. Without it, a future edit to that
  constant (it already moved once — ``lesson`` dropped 2026-08-26, commit
  7c1ba579, under a commit whose subject named "confidence" and nothing
  about health) silently changes what six of the thirteen counts *count*
  while every L0 name stays identical — the redefinition would be invisible.
  Storing the resolved list makes a redefinition a stored-value MISMATCH,
  loud, instead of a fact nobody watches for.
- ``embedder_installed_available`` / ``embedder_config_enabled`` — TWO
  booleans, not the collapsed single ``embedder_available`` the #2537 issue
  body's literal table names. The live gate
  (``core._embedder.is_available() and core._get_embedding_config().get(
  "enabled", True)``) is an install fact AND an operator choice; one boolean
  says the gate was shut, not which conjunct shut it (accepted amendment #1,
  #2537 comment thread). Two independently-readable facts, read the exact
  way ``health.metrics.embedding_coverage``'s own gate already reads
  them.

So the stored set is 13 ints + 2 embedder booleans + 1 resolved type list +
1 version stamp = 17 keys (16 measurements + the version stamp), not the
issue body's literal "15 items" — the count moved when the remote-
definition amendment was accepted, and again when engram-alpha#2905 PR-B
unified two numerators into one; the issue
text itself was never updated to match (flagged to Ariadne in the reply
DM that accompanies this PR).

## raw_schema_version (Ariadne's post-brief amendment; Lei's amendment 2 made it the real token)

A bare int, stamped into the ``measurements`` block at the exact turn this
block first exists — NOT the full versioning machinery (graceful
degradation on a MAJOR mismatch), which is slice 3's job. Borges's
reasoning, relayed verbatim: "a measurements block with no version
identifier is exactly the un-reconstructable artifact this whole change
exists to prevent" — shipping this block without an identifier would be a
smaller copy of the defect being fixed, open for however long elapses before
slice 3 lands (a plugin rebuild, not a slice boundary, is what actually opens
that window).

**The value stamped is ``health.version.MAJOR``, not a local constant.**
Amendment 2 (Lei, via Borges/Ariadne) minted ``health/version.py`` as the
ONE canonical home for health-v2's own version identity — retiring the
confusable ``ENGINE_VERSION``/``SCHEMA_VERSION`` pair into a single
``health-v<MAJOR>.<MINOR>`` token. This module imports ``MAJOR`` from
there rather than defining its own throwaway constant, so the stamp is
already the right token instead of something slice 3 has to rename later
("do not ship the identifier we are retiring, because anything written in
the window carries it" — Ariadne's build-lead call). See
``health/version.py``'s own docstring for the full versioning design;
retiring ``health.config.EngineConfig``'s existing
``schema_version``/``engine_version`` fields into that token is NOT this
PR's job (real, separate blast radius on the top-level snapshot schema —
stays out of scope for the purely-additive L0 layer).

**The L0 hash gate** (Borges's placement call, amendment 2: "it belongs
with the definition of the set it pins") lives in this module's own test
file, ``tests/test_health_measurements.py``. It pins a hash over the
canonical L0 set — every ``MeasurementDef``'s ``name`` + ``population``,
sorted by name — so that adding, removing, or redefining a measurement
(including a silent redefinition via a remote constant like
``CLAIM_BEARING_TYPES`` — the exact 2026-08-26 failure amendment 1 already
named) fails the test until ``health.version.MAJOR`` is bumped and the
hash re-pinned IN THE SAME COMMIT. Makes the MAJOR-bump rule self-enforcing
rather than a discipline someone has to remember.

**This guarantee depends on each measurement declaring its remote
constant's RESOLVED VALUE via ``definition_inputs``, never a string
naming the symbol — caught as a real blocker in this PR's own review**
(Ariadne, PR#2541, two rounds: her first-draft fix inlined the resolved
value into ``population`` prose, which worked for ``CLAIM_BEARING_TYPES``
specifically but fixed exactly one constant by hand; Borges's correction,
relayed by her, is the general form below — a measurement declares what
it depends on, and the next constant is covered by construction, not
vigilance). See ``MeasurementDef``'s own docstring for the full
``definition_inputs`` design and its embedder-boolean bound, and
``_resolve_claim_bearing_types()`` below for the concrete resolution (see
its own docstring for why a module-level resolution is safe here
specifically), and ``tests/test_health_measurements.py::TestL0HashGate::
test_definition_inputs_resolves_the_constants_value_not_just_its_name``
for the regression test simulating 2026-08-26 directly.

## Registry mechanism (this PR's own call, "deliberately yours" per the brief)

Mirrors ``health.model.SIGNALS`` / ``CURVES`` exactly: a plain
name -> object mapping, NOT a DSL, NOT an "operator + population" expression
parser. ``MeasurementDef`` wraps one ``compute_fn``; ``MEASUREMENTS`` collects
every one; ``compute_all_measurements(conn)`` is the single entry point
``build_snapshot`` calls. Chosen for the same reason Kepler's clarification 2
gave one layer up: nothing in this PR's scope justifies a expression
language, and building one here would be exactly the kind of unrequested
generality the design doc's own restraint (§ "framework serves maintenance,
never a formality") argues against.

House rule (mirrors health/metrics.py): access shared state ONLY via
``import engram_core as core``, lazily inside each function that needs it —
never a module-level import. See that module's docstring for the full
cross-test-contamination reasoning; identical here, same test-suite fixture
pattern (``fresh_server()``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Union

from . import model as _health_model
from .signals import SIGNAL_DEFS
from .version import MAJOR as RAW_SCHEMA_VERSION

MeasurementValue = Union[int, bool, "tuple[str, ...]"]


@dataclass(frozen=True)
class MeasurementDef:
    """One registered L0 measurement — a name, a human-readable population/
    predicate description, its resolved code-constant dependencies (if
    any), and the function that reads it directly off the graph.

    ``population`` is PROSE — a concise, literal restatement of the
    population/predicate the function's own docstring already describes
    (never longer than a sentence). It is NOT the hash gate's input for
    remote-definition detection (see ``definition_inputs`` below) — mixing
    "what this measures, in words" with "the resolved value of an outside
    constant it depends on" makes ``population`` half declaration and half
    measurement, against this module's own boundary rule (Borges's
    correction on PR#2541, relayed by Ariadne, replacing an earlier,
    narrower fix that inlined the resolved value into ``population``
    itself — worse because it fixes exactly one constant by hand and the
    next measurement leaning on a different one gets nothing).

    ``definition_inputs`` — DATA, hashed by the L0 gate
    (``tests/test_health_measurements.py``) alongside ``name`` — is the
    resolved VALUE of every code constant this measurement's definition
    depends on, declared explicitly, as a tuple. Empty (``()``) for a
    measurement with no remote-definition dependency. THE RULE: "declare
    what you depend on," not "remember to inline this particular
    constant" — a future measurement that leans on a different outside
    constant is covered by construction, not by someone remembering the
    2026-08-26 incident. **Hash code constants only — deterministic,
    source-code-defined, identical at a given commit regardless of
    machine.** NEVER a per-seat/per-run ENVIRONMENT fact (e.g. the
    embedder booleans below): folding an environment fact in would make
    the pinned hash unpinnable — CI and a contributor's laptop would
    legitimately disagree, and the gate would fail for the wrong reason
    until someone deleted it. The falsifier for this bound: computing
    ``canonical_l0_hash()`` on two different seats at the same commit must
    produce the SAME digest — if it doesn't, an environment fact was
    smuggled in.

    ``label`` — a short display noun phrase for this measurement, used by
    viz/API consumers that need a human-readable name for a raw L0 key
    (e.g. the #2518 detail view's numerator/denominator rows) without
    building their own key->name table. PRESENTATION ONLY, not measurement
    IDENTITY: ``canonical_l0_hash`` below deliberately does not range over
    it (see that function's own docstring) — relabeling a measurement for
    display never bumps ``health.version.MAJOR``.

    ``replay`` — one of ``"graph"`` / ``"stripped"`` / ``"environment"``
    (design doc §3, D2), declaring how ``health.series.replay_commit`` (PR B)
    must treat this measurement when replaying a historical ``knowledge.sql``
    dump rather than a live graph:

    - ``"graph"`` (the default, 13 of today's 16 measurements) — a plain
      read off the graph; replay runs ``compute_fn`` against the restored
      dump exactly as it would against a live connection. On
      ``sqlite3.OperationalError`` (the dump predates a table/column this
      measurement needs) replay records ``null`` with a ``schema:<message>``
      reason and class ``discovered`` — a fact learned only by trying, never
      predictable from the declaration alone.
    - ``"stripped"`` (``claim_bearing_null_embedding``) — the dump never
      carries this data at all (``engram_backup.dump_stripped`` nulls
      ``embedding`` before every dump), so a graph-mode replay would not
      fail loudly — it would silently read 100% null and lie. Replay must
      never call ``compute_fn``; it records ``null`` with reason
      ``stripped_from_dump``, class ``declared`` (predictable in advance,
      from the declaration alone, without running anything).
    - ``"environment"`` (the two embedder booleans) — an install/operator
      fact about THIS seat's environment right now, not a fact about the
      graph at any past commit; history of an install fact is not a
      property of the graph replay is reconstructing. Replay must never
      call ``compute_fn``; it records ``null`` with reason
      ``environment_fact``, class ``declared``.

    Load-bearing (D2, restated from the design doc so it is not missed by
    reading this field in isolation): ``replay`` is L0 *metadata*, exactly
    like ``label`` above — it is **outside** ``canonical_l0_hash`` (see that
    function's own docstring: it hashes only ``name``/``population``/
    ``definition_inputs``) and flipping it (e.g. ``claim_bearing_null_
    embedding`` moving from ``stripped`` to ``graph`` once dumps start
    carrying embeddings) never bumps ``health.version.MAJOR``. That is
    exactly why D2 fingerprints the *declarations themselves* into
    ``replay_decl_hash`` (part of ``l0_points``'s primary key for
    ``git_replay`` rows, per ``health.series``) — the axis that tracks a
    ``replay`` flip is ``replay_decl_hash``, not the MAJOR version. Without
    that fingerprint a flip would leave every historical replay row saying
    ``null``/``stripped_from_dump`` with nothing ever re-computing them: no
    MAJOR bump fires (correctly — ``replay`` isn't hashed), and rows are
    never mutated (correctly — that's the never-mutate rule), so a
    permanent hole indistinguishable from a genuinely-measured absence is
    exactly the failure D2 exists to prevent.

    See module docstring for why this is a plain mapping and not a DSL."""

    name: str
    population: str
    compute_fn: Callable[[sqlite3.Connection], "MeasurementValue"]
    definition_inputs: "tuple[object, ...]" = ()
    label: str = ""
    replay: str = "graph"


def _resolve_claim_bearing_types() -> "tuple[str, ...]":
    """The sorted CLAIM_BEARING_TYPES VALUE, as a tuple — e.g.
    ``("axiom", "conjecture", "derivation", "observation_factual",
    "observation_predictive", "theory")`` — for every claim-bearing-scoped
    measurement's ``definition_inputs``.

    NAMES-VS-RESOLVES BUG THIS FUNCTION FIXES (Ariadne's blocker finding,
    PR#2541, Borges's better fix): a measurement's DEFINITION must depend
    on this constant's resolved VALUE, not a string naming the symbol —
    naming the symbol is byte-identical before and after an edit to the
    constant (exactly the 2026-08-26 incident this whole hash gate exists
    to catch), so the hash would stay unchanged and the gate would sail
    through the exact case it's for. Resolving the value here, and hashing
    it via ``definition_inputs`` (not folded into ``population`` prose —
    see ``MeasurementDef``'s own docstring for why that first-draft fix was
    worse), means an edit to the constant changes this tuple, changes the
    hash, and fails the gate.

    DELIBERATE EXCEPTION to this module's "lazy import, never module-level"
    house rule (see module docstring) — and why it's safe here specifically:
    that rule protects against MUTABLE per-test-fixture state
    (``DATA_DIR``, the embedder's live config) going stale across a
    ``fresh_server()`` reload. ``CLAIM_BEARING_TYPES`` is neither — it's a
    literal, source-code-defined constant that cannot change within one
    running process regardless of how many times ``engram_core`` gets
    reloaded, so resolving it once at this module's own import time (via
    the module-level call below) carries none of that risk. Called ONCE,
    at import, not lazily per-measurement — the registry itself is built
    once at import time too, so there's no benefit to re-resolving per call.
    """
    import engram_core as core  # see docstring: safe as a one-time import here

    return tuple(sorted(core.CLAIM_BEARING_TYPES))


_CLAIM_BEARING_TYPES_RESOLVED = _resolve_claim_bearing_types()


# ---------------------------------------------------------------------------
# Denominators (populations), all `is_current = 1` — mirrors the five
# populations health/metrics.py's nine signal functions already read;
# this module measures the COUNT, not the rate.
# ---------------------------------------------------------------------------


def m_derivations_current(conn: sqlite3.Connection) -> int:
    """Count of current derivation nodes. Denominator for
    ``stale_derivation_rate``."""
    return conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE type = 'derivation' AND is_current = 1"
    ).fetchone()["c"]


def m_claim_bearing_current(conn: sqlite3.Connection) -> int:
    """Count of current claim-bearing nodes (``engram_core.
    CLAIM_BEARING_TYPES``). Shared denominator for five signals:
    ``taint_rate``, ``recall_summary_coverage``, ``recall_keywords_
    coverage``, ``embedding_coverage``, ``giant_component_fraction``."""
    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    return conn.execute(
        f"SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchone()["c"]


def m_contradictions_current(conn: sqlite3.Connection) -> int:
    """Count of current contradiction nodes. Denominator for
    ``open_contradiction_rate``."""
    return conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE type = 'contradiction' AND is_current = 1"
    ).fetchone()["c"]


def m_observations_current(conn: sqlite3.Connection) -> int:
    """Count of current observation nodes (``observation_factual`` +
    ``observation_predictive``). Denominator for
    ``uncited_observation_rate``."""
    return conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE is_current = 1"
        " AND type IN ('observation_factual', 'observation_predictive')"
    ).fetchone()["c"]


def m_evidence_current(conn: sqlite3.Connection) -> int:
    """Count of current evidence nodes. Denominator for
    ``dangling_evidence_rate``."""
    return conn.execute(
        "SELECT COUNT(*) as c FROM nodes WHERE type = 'evidence' AND is_current = 1"
    ).fetchone()["c"]


# ---------------------------------------------------------------------------
# Numerators
# ---------------------------------------------------------------------------


def m_claim_bearing_pending_marker(conn: sqlite3.Connection) -> int:
    """DISTINCT current claim-bearing nodes carrying ANY pending cascade
    marker (root_op IN ('retract', 'supersede')) -- a node carrying both
    marker kinds counts once. Numerator for ``pending_premise_rate``
    (engram-alpha#2905 PR-B), unifying the prior separate taint_rate /
    stale_derivation_rate numerators: post-cascade-machine, a pending
    retract and a pending supersede marker do the same thing to a node's
    usability as a premise -- block it, regardless of cause."""
    import engram_core as core  # lazy -- match this module's existing convention

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    return conn.execute(
        f"""SELECT COUNT(DISTINCT n.id) as c
            FROM nodes n
            JOIN cascade_markers m ON m.node_id = n.id
            WHERE n.is_current = 1 AND n.type IN ({placeholders})
              AND m.root_op IN ('retract', 'supersede') AND m.status = 'pending'""",
        claim_bearing_types,
    ).fetchone()["c"]


def m_contradictions_open(conn: sqlite3.Connection) -> int:
    """Count of current contradiction nodes with
    ``status IN ('active', 'partially_resolved')``. Numerator for
    ``open_contradiction_rate``."""
    return conn.execute(
        """SELECT COUNT(*) as c FROM nodes
           WHERE type = 'contradiction' AND is_current = 1
             AND status IN ('active', 'partially_resolved')"""
    ).fetchone()["c"]


def m_observations_without_evidence_id(conn: sqlite3.Connection) -> int:
    """Count of current observations with ``evidence_id IS NULL``.
    Numerator for ``uncited_observation_rate``."""
    return conn.execute(
        """SELECT COUNT(*) as c FROM nodes
           WHERE is_current = 1
             AND type IN ('observation_factual', 'observation_predictive')
             AND evidence_id IS NULL"""
    ).fetchone()["c"]


def m_evidence_uncited(conn: sqlite3.Connection) -> int:
    """Count of current evidence nodes cited by NO current observation.
    Numerator for ``dangling_evidence_rate``.

    PERMANENT-BY-DESIGN, NOT EXPENSIVE-BY-DESIGN (PR#2541's colleague review
    predicted this query would run only once after slice 2; corrected by
    PR#2556's own colleague review, Ariadne, once slice 2 landed and
    measured it; both the "will disappear" and "so it's an accepted cost"
    framings that followed were themselves wrong — see below). PR#2541
    predicted slice 2 would make this query run once instead of twice,
    because ``dangling_evidence_rate`` would become "a formula over this
    measurement instead of its own SQL." Slice 2 DID make it a formula
    (``health.signals.dangling_evidence_rate`` is exactly that) — but the
    SSoT verification control it also added
    (``health.engine._verify_l1_against_live_signal_fns``) calls
    ``health.metrics.dangling_evidence_rate(conn)`` — the ORIGINAL,
    unformulized SQL — on every single build, as the independent oracle the
    whole design depends on. So the second query never went away; it moved
    from "the formula's own implementation" to "the thing verifying the
    formula is correct," and runs on every build rather than only
    transiently during slice 1. That part is permanent and correct as
    designed.

    What is NOT permanent is the ~1.3s/build cost first measured for it
    (engram-alpha#2556 thread, Ariadne, 4,597-node graph): Kepler traced it
    to a missing ``ANALYZE`` — grepping ``src/engram`` (non-test) found zero
    calls to ``ANALYZE`` or ``PRAGMA optimize`` in any shipped code path, and
    ``sqlite_stat1`` was confirmed absent on the two seats actually measured
    (Kepler's 1,940-node graph; Ariadne's 4,597-node graph) — so the query
    planner had no statistics on either and fell back to a full
    ``idx_nodes_current`` scan per evidence node instead of the existing
    ``idx_nodes_evidence_id``. Ariadne independently confirmed on her own
    (largest measured, un-``ANALYZE``'d at the time) seat: **758x** on this
    query alone (1187.8ms → 1.6ms) and **45x** on the whole
    L0-measure-then-verify path (2749.8ms → 60.8ms), same result counts
    before/after. Fixed as engram-alpha#2559 (Kepler): a composite index
    ``nodes(evidence_id, is_current)`` makes the correct query plan
    deterministic regardless of statistics freshness (the fix does not
    depend on ``ANALYZE`` ever running), plus a one-time ``ANALYZE`` seed
    (only when ``sqlite_stat1`` does not yet exist) followed by the
    existing ``PRAGMA optimize`` at checkpoint close (nap / ``advance_turn``)
    that together keep ``sqlite_stat1`` current fleet-wide for every other
    query on the connection, not just this one — the seed is what actually
    delivers that fleet-wide claim; see the checkpoint-close comment in
    ``engram_lifecycle._checkpoint_internal`` (search ``#2685``) for the
    measured, process-library-linkage-dependent reason ``PRAGMA optimize``
    alone was found not to be sufficient on every seat. Kepler ruled the design
    question itself (keep the SSoT
    control always-on, unchanged) BEFORE the ANALYZE cause was confirmed,
    on the grounds that a sometimes-on control is a weaker control and the
    cost sits at the nap/``advance_turn`` checkpoint, invisible to a user
    regardless of price — so the ruling never depended on #2559 landing.
    What #2559 changes is only how expensive "always-on" turns out to
    actually be: the price is gone by construction once it lands — ~1.3s/
    build before, ~30ms after, on the seats measured."""
    return conn.execute(
        """SELECT COUNT(*) as c FROM nodes n
           WHERE n.type = 'evidence' AND n.is_current = 1
             AND NOT EXISTS (
                 SELECT 1 FROM nodes o
                 WHERE o.evidence_id = n.id AND o.is_current = 1
             )"""
    ).fetchone()["c"]


def m_claim_bearing_without_recall_summary(conn: sqlite3.Connection) -> int:
    """Count of current claim-bearing nodes with a NULL/whitespace-only
    ``recall_summary``. Numerator for ``recall_summary_coverage``.

    Measurement definition (Ariadne's seam #3): presence is decided by
    reading the column directly (non-NULL AND non-whitespace after
    ``.strip()``), never the ``summary_missing`` flag — see
    ``health.metrics.recall_summary_coverage``'s own docstring for
    why this is the correct read and not just a convenient one."""
    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    rows = conn.execute(
        f"SELECT recall_summary FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchall()
    return sum(
        1 for r in rows
        if r["recall_summary"] is None or not r["recall_summary"].strip()
    )


def m_claim_bearing_without_recall_keywords(conn: sqlite3.Connection) -> int:
    """Count of current claim-bearing nodes with no non-empty
    ``recall_keywords`` list. Numerator for ``recall_keywords_coverage``.

    Measurement definition (Ariadne's seam #3): JSON-parses; missing,
    non-list, or malformed all count as absent — mirrors
    ``engram_core._reflect_keywords``'s own read pattern, per
    ``health.metrics.recall_keywords_coverage``."""
    import json

    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    rows = conn.execute(
        f"SELECT recall_keywords FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchall()

    missing = 0
    for r in rows:
        raw = r["recall_keywords"]
        if not raw:
            missing += 1
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            missing += 1
            continue
        if not isinstance(parsed, list) or len(parsed) == 0:
            missing += 1
    return missing


def m_claim_bearing_null_embedding(conn: sqlite3.Connection) -> int:
    """Count of current claim-bearing nodes with ``embedding IS NULL``.
    Numerator for ``embedding_coverage``.

    Deliberately UNCONDITIONAL — unlike ``health.metrics.
    embedding_coverage``, this measurement does NOT check the embedder
    availability gate; that gate is an environment fact
    (``embedder_installed_available`` / ``embedder_config_enabled`` below),
    and L1 (not L0) decides what a NULL-embedding count means in light of
    it. Counting NULLs is well-defined regardless of whether the embedder
    is installed."""
    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    return conn.execute(
        f"SELECT COUNT(*) as c FROM nodes WHERE is_current = 1 AND type IN ({placeholders})"
        " AND embedding IS NULL",
        claim_bearing_types,
    ).fetchone()["c"]


def m_claim_bearing_giant_component_size(conn: sqlite3.Connection) -> int:
    """Size (node count) of the largest connected component in the INDUCED
    subgraph on current claim-bearing nodes — an edge counts only if BOTH
    endpoints are current claim-bearing (design doc's "claim-bearing
    projection"). Numerator for ``giant_component_fraction``.

    A MEASUREMENT, not a formula (#2537's own boundary rule): the BFS traversal
    is how this is read off the graph; ``1 - size/total`` is L1 arithmetic on
    top of it. Adapted from ``health.metrics.giant_component_fraction``
    — same plain adjacency-dict + BFS-queue approach, no networkx/numpy.

    Unconditional: on zero claim-bearing nodes the loop below never runs and
    this returns 0, a well-defined (if trivial) measurement — L1 decides
    UNAVAILABLE from the paired ``claim_bearing_current`` denominator, not
    this function."""
    import engram_core as core  # lazy — see module docstring

    claim_bearing_types = tuple(sorted(core.CLAIM_BEARING_TYPES))
    placeholders = ",".join("?" * len(claim_bearing_types))
    node_rows = conn.execute(
        f"SELECT id FROM nodes WHERE is_current = 1 AND type IN ({placeholders})",
        claim_bearing_types,
    ).fetchall()
    claim_bearing_ids = {r["id"] for r in node_rows}
    if not claim_bearing_ids:
        return 0

    edge_rows = conn.execute("SELECT source_id, target_id FROM edges").fetchall()
    adj: dict = {}
    for e in edge_rows:
        s, t = e["source_id"], e["target_id"]
        if s in claim_bearing_ids and t in claim_bearing_ids:
            adj.setdefault(s, set()).add(t)
            adj.setdefault(t, set()).add(s)

    visited: set = set()
    largest = 0
    for nid in claim_bearing_ids:
        if nid in visited:
            continue
        size = 0
        queue = [nid]
        visited.add(nid)
        while queue:
            c = queue.pop(0)
            size += 1
            for nb in adj.get(c, ()):
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)
        largest = max(largest, size)

    return largest


# ---------------------------------------------------------------------------
# Environment facts + remote-definition resolutions (the closed 3-symbol
# surface, Borges's AST enumeration).
# ---------------------------------------------------------------------------


def m_embedder_installed_available(conn: sqlite3.Connection) -> bool:
    """``core._embedder.is_available()`` — the install fact half of the
    embedder gate. ``conn`` unused (an environment read, not a graph read);
    kept in the signature so this fits ``MeasurementDef.compute_fn``'s
    uniform shape."""
    import engram_core as core  # lazy — see module docstring

    return bool(core._embedder.is_available())


def m_embedder_config_enabled(conn: sqlite3.Connection) -> bool:
    """``core._get_embedding_config().get("enabled", True)`` — the operator-
    choice half of the embedder gate. ``conn`` unused, same reason as
    ``m_embedder_installed_available``."""
    import engram_core as core  # lazy — see module docstring

    return bool(core._get_embedding_config().get("enabled", True))


def m_claim_bearing_types_resolved(conn: sqlite3.Connection) -> "tuple[str, ...]":
    """The resolved, sorted ``engram_core.CLAIM_BEARING_TYPES`` set at
    measurement time. ``conn`` unused, same reason as the embedder facts
    above.

    This is the remote-definition rule's answer for the constant that moved
    once already (``lesson`` dropped 2026-08-26, commit 7c1ba579) and backs
    six of the thirteen numeric measurements (the shared denominator
    ``claim_bearing_current`` plus its 5 numerator siblings) — storing it
    makes a future
    redefinition a loud stored-value mismatch instead of a silent population
    change under an unchanged L0 name. Stored as a tuple here (frozen,
    hashable, matches the type this function's callers already sort into);
    ``compute_all_measurements`` converts it to a plain ``list[str]`` for
    JSON-safety at the serialization boundary, matching how every other
    MeasurementValue in this module is already a native JSON type."""
    import engram_core as core  # lazy — see module docstring

    return tuple(sorted(core.CLAIM_BEARING_TYPES))


# ---------------------------------------------------------------------------
# Registry — plain name -> MeasurementDef mapping (see module docstring).
# ---------------------------------------------------------------------------

MEASUREMENTS: "dict[str, MeasurementDef]" = {
    d.name: d
    for d in (
        MeasurementDef(
            "derivations_current",
            "current derivations",
            m_derivations_current,
            label="current derivations",
        ),
        MeasurementDef(
            "claim_bearing_current",
            "current claim-bearing nodes (engram_core.CLAIM_BEARING_TYPES)",
            m_claim_bearing_current,
            definition_inputs=_CLAIM_BEARING_TYPES_RESOLVED,
            label="current claim-bearing nodes",
        ),
        MeasurementDef(
            "contradictions_current",
            "current contradiction nodes",
            m_contradictions_current,
            label="current contradictions",
        ),
        MeasurementDef(
            "observations_current",
            "current observation_factual + observation_predictive nodes",
            m_observations_current,
            label="current observations",
        ),
        MeasurementDef(
            "evidence_current",
            "current evidence nodes",
            m_evidence_current,
            label="current evidence",
        ),
        MeasurementDef(
            "claim_bearing_pending_marker",
            "current claim-bearing nodes with a pending retract OR supersede cascade marker",
            m_claim_bearing_pending_marker,
            definition_inputs=_CLAIM_BEARING_TYPES_RESOLVED,
            label="claim-bearing nodes pending any cascade marker",
        ),
        MeasurementDef(
            "contradictions_open",
            "current contradictions with status IN (active, partially_resolved)",
            m_contradictions_open,
            label="open contradictions",
        ),
        MeasurementDef(
            "observations_without_evidence_id",
            "current observations with evidence_id IS NULL",
            m_observations_without_evidence_id,
            label="observations without an evidence ID",
        ),
        MeasurementDef(
            "evidence_uncited",
            "current evidence cited by no current observation",
            m_evidence_uncited,
            label="uncited evidence",
        ),
        MeasurementDef(
            "claim_bearing_without_recall_summary",
            "current claim-bearing nodes with recall_summary NULL or whitespace-only",
            m_claim_bearing_without_recall_summary,
            definition_inputs=_CLAIM_BEARING_TYPES_RESOLVED,
            label="claim-bearing nodes without a recall summary",
        ),
        MeasurementDef(
            "claim_bearing_without_recall_keywords",
            "current claim-bearing nodes with no non-empty recall_keywords list",
            m_claim_bearing_without_recall_keywords,
            definition_inputs=_CLAIM_BEARING_TYPES_RESOLVED,
            label="claim-bearing nodes without recall keywords",
        ),
        MeasurementDef(
            "claim_bearing_null_embedding",
            "current claim-bearing nodes with embedding IS NULL",
            m_claim_bearing_null_embedding,
            definition_inputs=_CLAIM_BEARING_TYPES_RESOLVED,
            label="claim-bearing nodes with no embedding",
            replay="stripped",
        ),
        MeasurementDef(
            "claim_bearing_giant_component_size",
            "largest connected component, induced subgraph on current claim-bearing nodes",
            m_claim_bearing_giant_component_size,
            definition_inputs=_CLAIM_BEARING_TYPES_RESOLVED,
            label="largest claim-bearing component size",
        ),
        MeasurementDef(
            "embedder_installed_available",
            "engram_core._embedder.is_available()",
            m_embedder_installed_available,
            label="embedder installed and available",
            replay="environment",
        ),
        MeasurementDef(
            "embedder_config_enabled",
            "engram_core._get_embedding_config().get('enabled', True)",
            m_embedder_config_enabled,
            label="embedder enabled in config",
            replay="environment",
        ),
        MeasurementDef(
            "claim_bearing_types_resolved",
            "sorted(engram_core.CLAIM_BEARING_TYPES)",
            m_claim_bearing_types_resolved,
            definition_inputs=_CLAIM_BEARING_TYPES_RESOLVED,
            label="resolved claim-bearing types",
        ),
    )
}


def compute_all_measurements(conn: sqlite3.Connection) -> "dict[str, object]":
    """Run every registered measurement against ``conn`` and return a
    JSON-safe ``{name: value}`` dict, plus the ``raw_schema_version`` stamp.

    The single entry point ``build_snapshot`` calls. Every value is already
    a native JSON type (``int``, ``bool``, or ``list[str]`` — tuples are
    converted here, at the one place this module hands data to a caller that
    will ``json.dumps`` it) except that ``bool`` is also technically ``int``
    in Python; kept as real ``bool`` values (not coerced to 0/1) so a reader
    of the stored snapshot sees ``true``/``false``, matching every other
    boolean field this engine already stores (e.g. ``config``'s own fields).
    """
    result: "dict[str, object]" = {"raw_schema_version": RAW_SCHEMA_VERSION}
    for name, mdef in MEASUREMENTS.items():
        value = mdef.compute_fn(conn)
        if isinstance(value, tuple):
            value = list(value)
        result[name] = value
    return result


def canonical_l0_hash() -> str:
    """SHA-256 hex digest over the canonical L0 set — every registered
    measurement's ``(name, population, definition_inputs)`` triple, sorted
    by name, as a stable JSON encoding (sorted keys, no whitespace, so the
    hash never drifts on a harmless reformat).

    The L0 hash gate (``tests/test_health_measurements.py``) pins this
    value; the two live together on purpose (Borges, amendment 2: "it
    belongs with the definition of the set it pins"). Change a
    measurement's declared ``definition_inputs`` (including via an edit to
    a remote code constant one of them resolves — see ``MeasurementDef``'s
    own docstring), or add/remove a measurement, and this hash changes —
    the gate then fails until ``health.version.MAJOR`` is bumped and the
    pinned hash updated IN THE SAME COMMIT, which is what makes the
    MAJOR-bump rule mechanically enforced rather than a discipline someone
    has to remember (the exact gap that let ``CLAIM_BEARING_TYPES`` change
    on 2026-08-26 under a commit titled about confidence, unnoticed).
    ``population`` (prose) also contributes, so a documentation-only
    rewrite of what a measurement claims to describe is caught too — cheap
    insurance, not the load-bearing half.

    Deliberately does NOT hash ``compute_fn`` itself — a function object
    has no stable, meaningful serialization, and the design's own boundary
    (§ "The boundary rule this module exists to enforce") is that L0's
    IDENTITY is its declared name + population + resolved definition
    inputs, not its implementation. A pure refactor of a measurement's SQL
    that doesn't change what it measures should not force a MAJOR bump.

    Deliberately does NOT hash the two embedder booleans' values — see
    ``MeasurementDef``'s own docstring for the environment-fact bound this
    would violate. Their ``definition_inputs`` are ``()`` for exactly this
    reason.
    """
    import json
    import hashlib

    canonical = sorted(
        (d.name, d.population, list(d.definition_inputs))
        for d in MEASUREMENTS.values()
    )
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def canonical_derived_hash() -> str:
    """SHA-256 hex digest over the canonical DERIVED-layer declaration set
    (engram-alpha#2563 Gap 2): the declared scoring model ABOVE L0/L1 --
    aspects, metric bindings, curve specs (incl. their now-declared
    ``knots``, #2576/#2578), and signal definitions -- sorted and stably
    JSON-encoded, same shape as ``canonical_l0_hash`` above.

    This is the second of the gate's two hashes. ``required_severity()``
    (``tests/test_health_version_gate.py``) computes MAJOR from a change to
    THIS function's sibling above (the L0 set — what gets measured); it
    computes MINOR from a change to THIS hash (the layers built on top of
    L0 — how a measured signal turns into a curve-shaped, aspect-weighted
    score). Before this function existed, a derived-only change (a curve
    reshaped, an importance reweighted, a metric's aspect reassigned) had
    NO detectable fingerprint at all, so ``required_severity`` could only
    ever return ``MAJOR`` or ``NONE`` — a real MINOR-worthy change was
    invisible to the gate and could land with no bump, silently.

    Hashed, from ``health.model.MODEL_SPEC`` (the unvalidated module-level
    spec — see that module's own docstring for why unvalidated is correct
    here; this hash is over DECLARATION, not over a validated-against-
    config object) and ``health.signals.SIGNAL_DEFS``:

    - ``aspects``  — every ``AspectSpec``: ``(name, points,
      reference_metric)``.
    - ``metrics``  — every ``MetricBinding``: ``(signal, aspect, curve,
      importance, detector, additional_detector, repair, dive_target)``,
      where a ``DetectorRef`` (``detector``/``additional_detector``)
      contributes its own ``(name, population)`` pair, or ``None``.
    - ``curves``   — every ``CurveSpec`` in ``MODEL_SPEC.curves``, keyed by
      curve name: ``(knots, monotone_direction, input_domain, output_range,
      observed_range)``. Deliberately does NOT hash ``curve_fn`` — same
      boundary as L0's ``compute_fn`` exclusion above: ``curve_fn`` is
      DERIVED (``__post_init__``) from ``knots``, so hashing both would be
      redundant, and a function object has no stable serialization anyway.
      ``knots`` is what makes a curve's actual shape visible to this hash
      (engram-alpha#2576 closed the gap where a curve's knots could
      silently reshape underneath a byte-identical dataclass) —
      ``curve_fn.keywords`` is never introspected anywhere, per that
      module's own docstring; ``knots`` is read directly instead.
    - ``signals``  — every ``SignalDef`` in ``SIGNAL_DEFS``, keyed by name:
      ``(numerator, denominator, form, gates, doc)``. ``doc`` (prose) also
      contributes, same "cheap insurance" reasoning as L0's ``population``
      field above.

    Deliberately does NOT hash ``MODEL_SPEC.signals`` (the dict of live
    signal-computing CALLABLES health/engine.py builds) — that dict's
    values have no stable serialization, same L0 boundary rule, and its
    declared source (``SIGNAL_DEFS``, hashed above) is what actually
    changes when a signal's arithmetic changes; the callables are compiled
    FROM that data by ``health/signals.py``, not an independent fact about
    it.

    STATED BOUND (#2563's own unit spec explicitly asks for this to be
    named, not left implicit): this hash covers what is DECLARED --
    aspects, metrics, curves, signal formulas. It does NOT cover
    ``health/engine.py``'s own AGGREGATION arithmetic -- ``_score_aspect``,
    ``compose_aspect_scores``, ``score_signals`` and the weighted-mean-
    with-renormalization rule that turns declared metric/aspect/curve data
    into the composed L3->L4 score. That is genuine CODE, not data (unlike
    everything hashed above), so a change to it (e.g. a different
    renormalization rule) would be MINOR-worthy and is currently invisible
    to this hash -- the same class of residual Borges/Ariadne identified
    and closed for the curve-knots/signal-declaration layer on 2026-09-04/
    05 (#2576/#2578/#2577), not yet closed for the aggregation layer.
    """
    import json
    import hashlib

    def _detector_tuple(ref):
        return None if ref is None else (ref.name, ref.population)

    aspects = sorted(
        (a.name, a.points, a.reference_metric)
        for a in _health_model.MODEL_SPEC.aspects
    )
    metrics = sorted(
        (
            m.signal,
            m.aspect,
            m.curve,
            m.importance,
            _detector_tuple(m.detector),
            _detector_tuple(m.additional_detector),
            m.repair,
            m.dive_target,
        )
        for m in _health_model.MODEL_SPEC.metrics
    )
    curves = sorted(
        (
            name,
            [list(k) for k in spec.knots],
            spec.monotone_direction,
            list(spec.input_domain) if spec.input_domain is not None else None,
            list(spec.output_range),
            list(spec.observed_range) if spec.observed_range is not None else None,
        )
        for name, spec in _health_model.MODEL_SPEC.curves.items()
    )
    signals = sorted(
        (d.name, d.numerator, d.denominator, d.form, list(d.gates), d.doc)
        for d in SIGNAL_DEFS.values()
    )
    canonical = {
        "aspects": aspects,
        "metrics": metrics,
        "curves": curves,
        "signals": signals,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def replay_declarations() -> "dict[str, str]":
    """``{measurement_name: replay}`` for every registered L0 measurement,
    sorted by name — the declared-replay-behaviour table ``health.series``
    (PR B) reads to decide, per measurement, whether a historical replay may
    call ``compute_fn`` at all (see ``MeasurementDef.replay``'s own
    docstring for the three values and what each means). PRESENTATION/
    CONFIGURATION only for THIS module — see ``replay_decl_hash`` below for
    the fingerprint that makes a change to this table detectable downstream
    without a MAJOR bump."""
    return dict(sorted((name, mdef.replay) for name, mdef in MEASUREMENTS.items()))


def replay_decl_hash() -> str:
    """SHA-256 hex digest over the canonical JSON encoding (sorted keys, no
    whitespace) of the sorted ``(name, replay)`` pairs from
    ``replay_declarations()`` — the fingerprint D2 folds into ``health.
    series``'s ``l0_points.replay_decl_hash`` column (part of the primary
    key for ``git_replay`` rows).

    WHY A CONTENT FINGERPRINT AND NOT A HAND-BUMPED GENERATION INT (D2):
    ``replay`` is deliberately unhashed by ``canonical_l0_hash`` (see
    ``MeasurementDef.replay``'s own docstring) — a flip never bumps
    ``health.version.MAJOR``, and ``l0_points`` rows are never mutated once
    inserted (the store's own never-mutate rule). Put those two facts
    together and a ``replay`` flip with no fingerprint at all would leave
    every historical replay row permanently saying ``null``/``stripped_
    from_dump`` (or ``environment_fact``) with nothing ever re-computing
    them — a hole indistinguishable from a genuinely-measured absence, which
    is the one thing declaring ``replay`` in the first place was supposed to
    avoid. A HAND-BUMPED int (e.g. a module-level ``REPLAY_GENERATION = 2``
    someone increments on every flip) would fix that, but only if the
    person making the flip remembers to bump it — exactly the kind of
    vigilance-dependent discipline this whole design (the L0 hash gate
    itself, ``canonical_l0_hash``) exists to replace with something
    mechanical. A CONTENT fingerprint over the declarations themselves
    changes automatically, by construction, the moment any declaration
    changes — there is no separate step to forget. The cost is that it says
    nothing about ORDER (two totally different sets of declarations could
    theoretically hash to different values with no meaningful "generation"
    relationship between them), but ``health.series`` never needs an
    ordering here — it only needs to detect "the current declarations
    differ from what a stored ``git_replay`` row was computed under," which
    a content hash answers directly, and gap-fills the difference through
    the same regular mechanism as any other missing commit (D2 again — a
    ``replay`` flip becomes a ``reason='gap_fill'`` backfill, not a special
    case)."""
    import hashlib
    import json as _json

    pairs = sorted(replay_declarations().items())
    blob = _json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def measurement_labels_jsonable() -> "dict[str, str]":
    """``{measurement_name: label}`` for every registered L0 measurement —
    the display-name table ``build_snapshot`` exports alongside the raw
    ``measurements`` values (as ``measurement_labels``), so a viz/API
    consumer building the #2518 detail view's numerator/denominator rows
    reads the label straight off the snapshot instead of maintaining its
    own key->name table. PRESENTATION ONLY — see ``MeasurementDef.label``'s
    own docstring for why this never affects ``canonical_l0_hash``."""
    return {name: mdef.label for name, mdef in MEASUREMENTS.items()}
