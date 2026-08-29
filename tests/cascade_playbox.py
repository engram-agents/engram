"""tests/cascade_playbox.py — the cascade playbox test harness (#2356).

A `with`-scoped sandbox for exercising the #2013 cascade machine
(supersede / retract / frontier / resolve) against named topologies, without
ever hand-writing the state a real cascade would produce.

WHY THIS MODULE IS NOT NAMED `engram_playbox` (C3, load-bearing). Isolation
in this suite is achieved by MODULE PURGE, not save/restore:
``tests/conftest.py::fresh_module`` deletes every ``sys.modules`` key matching
``key == "server" or key.startswith("engram_")`` before re-importing the
family (see ``tests/IMPORT_RUBRIC.md`` §R4). A playbox module named
``engram_playbox`` would have its own module-level singleton state deleted by
the very purge call it must issue on ``__enter__`` — the guard would reset
itself exactly when a second box tried to open while the first was still
live. Naming this file ``cascade_playbox.py`` (no ``engram_`` prefix) keeps
its module object, and the module-level ``_LIVE`` slot below, outside the
purge predicate. ``T2`` in ``tests/test_cascade_playbox.py`` is the proof
that this actually holds, not just the fix.

WHY ISOLATION HERE IS PURGE-THEN-RECONFIGURE, NOT SAVE/RESTORE (C2). Two
ENGRAM sandboxes cannot be live in one process at once, silently (C1) — a
write issued on behalf of an earlier holder lands in whichever data dir the
shared module globals currently point at, with no error. The dominant
convention in this suite (177 call sites, 131 files) is: purge every
``engram_*``/``server`` module, re-import, then call the fresh module's own
``_configure_paths(tmpdir)``. ``__enter__`` below follows that convention
exactly, in that order, and ``__exit__`` does NOT attempt to restore whatever
paths were configured before — a restore step would be a second, competing
mechanism layered on top of the one this suite already uses everywhere else.

WHAT IS SYNTHETIC AND WHAT IS NOT. Only the *adjudication* is synthetic: the
harness plays the part of the agent choosing `acknowledged` / `superseded` /
`retracted` for a frontier node. Every state transition — `box.node()` minting
through the canonical creation tools, `box.supersede()` / `box.retract()` /
`box.resolve()` through the real impls, `box.frontier()` through the real
read — goes through the actual cascade machine in
``src/engram/engram_revision.py``. A playbox test cannot express a state the
real machine would refuse to reach; that property is the whole reason this
harness exists instead of a hand-built fixture.

Import-pattern contract note: this file is a plain support module, not a
``test_*.py`` file, so ``tools/run_touched_tests.py``'s static selection graph
does not parse it directly (it only walks ``tests/test_*.py``). The consuming
test file, ``tests/test_cascade_playbox.py``, carries its own top-level
imports of every family module this harness touches (R1) so a change to any
of them still selects that test file.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Optional

# --- bootstrap sys.path exactly like every sibling test file (R4 / the
# established convention) so this module is importable both under pytest
# (which loads tests/conftest.py at collection time) and standalone. -------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

# `from conftest import fresh_module` inserts src/engram onto sys.path as a
# side effect of conftest.py's own module-level bootstrap — the same
# mechanism every sibling cascade test file relies on. Must run before the
# `import engram_*` lines below.
from conftest import fresh_module  # noqa: E402 — must follow sys.path setup

import engram_core  # noqa: E402
import engram_revision  # noqa: E402
import engram_observation  # noqa: E402
import engram_derive  # noqa: E402
import engram_conformance  # noqa: E402


# ---------------------------------------------------------------------------
# Handle — the API surface. Ids are reachable ONLY via Playbox.id_of().
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Handle:
    """An opaque reference to a node minted inside a playbox.

    Deliberately NOT the node id. An id in a fixture is an invitation to
    hand-build the state the playbox exists to make unreachable — so the
    only sanctioned way to get from a handle back to an id is
    ``Playbox.id_of(handle)``, which exists for error messages and
    debugging, not for fixture construction.
    """

    name: str

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Handle({self.name!r})"


class PlayboxAlreadyLiveError(Exception):
    """Raised when a second playbox is entered while one is already live.

    Two ENGRAM sandboxes cannot be live in one process at once, silently
    (C1) — nesting would corrupt whichever holder's writes land where.
    Names both the live box's topology and the attempted one so the failure
    is diagnosable from the message alone.
    """

    def __init__(self, live_topology, attempted_topology):
        self.live_topology = live_topology
        self.attempted_topology = attempted_topology
        super().__init__(
            f"A cascade playbox is already live (topology={live_topology!r}). "
            f"Cannot enter a second one (attempted topology="
            f"{attempted_topology!r}) — playboxes never nest. Exit the live "
            f"box's `with` block first."
        )


# Module-level singleton slot. Deliberately NOT inside an `engram_*` module
# (see the file docstring, C3) — a purge of the engram_* family must NOT
# reset this.
_LIVE: Optional["Playbox"] = None


# ---------------------------------------------------------------------------
# Playbox
# ---------------------------------------------------------------------------


class Playbox:
    """Context-managed cascade sandbox. See the module docstring.

    Three API groups, kept separate:
      * Build (setup only, before any event): ``node()``.
      * Drive (the real machine, never a shortcut): ``supersede()``,
        ``retract()``, ``frontier()``, ``resolve()``.
      * Assert: ``markers()``, ``edges()``, ``confidence()``,
        ``conformance()``, ``cycles()``.
    """

    def __init__(self, topology: Optional[str] = None):
        self.topology = topology
        self._tmpdir: Optional[str] = None
        self._rev = None
        self._obs = None
        self._derive = None
        self._conformance = None
        self._core = None
        self._name_to_id: dict = {}
        self._id_to_name: dict = {}
        self.handles: dict = {}

    # -- lifetime -----------------------------------------------------

    def __enter__(self) -> "Playbox":
        global _LIVE
        if _LIVE is not None:
            raise PlayboxAlreadyLiveError(_LIVE.topology, self.topology)
        _LIVE = self
        try:
            self._tmpdir = tempfile.mkdtemp(prefix="cascade_playbox_")
            os.environ.setdefault("ENGRAM_NO_EMBEDDINGS", "1")

            # C2 / IMPORT_RUBRIC §R4: purge the family, THEN configure paths.
            # Do not invent a parallel isolation path.
            self._rev = fresh_module(engram_revision)
            self._obs = importlib.import_module("engram_observation")
            self._derive = importlib.import_module("engram_derive")
            self._conformance = importlib.import_module("engram_conformance")
            self._core = sys.modules["engram_core"]

            self._core._configure_paths(self._tmpdir)
            self._core._ensure_data_dir()

            if self.topology is not None:
                self._build_topology(self.topology)
        except BaseException:
            # Entering failed after we claimed _LIVE — release it so a
            # failed box doesn't permanently wedge the module. Also remove
            # any temp dir already created: __exit__ never runs on a failed
            # __enter__ (the `with` block's context was never entered), so
            # without this the temp dir would leak on every setup failure
            # (round-1 review, #2373).
            if self._tmpdir:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
            _LIVE = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        global _LIVE
        # Release in a finally so an exception inside the `with` block still
        # clears _LIVE (T3). Drop the temp dir; do NOT restore prior paths
        # (C2) — that would be a second, competing isolation mechanism.
        try:
            if self._tmpdir:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
        finally:
            _LIVE = None
        return False  # never swallow an exception from the `with` block

    # -- handles --------------------------------------------------------

    def id_of(self, handle: Handle) -> str:
        """The only sanctioned handle -> id path. For error messages and
        debugging — never for fixture construction."""
        if not isinstance(handle, Handle):
            raise TypeError(f"id_of() expects a Handle, got {type(handle)!r}")
        try:
            return self._name_to_id[handle.name]
        except KeyError:
            raise KeyError(
                f"handle {handle.name!r} is not known to this box"
            ) from None

    def __getitem__(self, name: str) -> Handle:
        if name not in self._name_to_id:
            raise KeyError(f"no handle named {name!r} in this box")
        return Handle(name)

    def _handle_of(self, node_id) -> Optional[str]:
        return self._id_to_name.get(node_id)

    # -- Build ------------------------------------------------------------

    def node(
        self,
        name: str,
        kind: str = "observation",
        claim: Optional[str] = None,
        supports=None,
        reasoning_type: str = "deductive_modus_ponens",
        **kwargs,
    ) -> Handle:
        """Mint a node via the real canonical creation tool and return a
        Handle (never an id). ``kind`` is 'observation' or 'derivation'."""
        if name in self._name_to_id:
            raise ValueError(f"handle {name!r} already minted in this box")
        claim = claim or f"{name}: playbox claim ({self.topology or 'custom'})."

        if kind == "observation":
            payload = dict(
                url=kwargs.pop("url", f"https://example.com/playbox/{name}"),
                title=kwargs.pop("title", f"Playbox source {name}"),
                quoted_text=kwargs.pop("quoted_text", claim),
                interpretation=kwargs.pop("interpretation", f"Interpretation: {claim}"),
                claim=claim,
                quote_type=kwargs.pop("quote_type", "attributed_analysis"),
            )
            payload.update(kwargs)
            result = json.loads(self._obs._add_observation_impl(**payload))
            if "error" in result:
                raise RuntimeError(
                    f"box.node({name!r}, kind='observation') failed: {result['error']}"
                )
            node_id = result["observation_id"]

        elif kind == "derivation":
            if not supports:
                raise ValueError(
                    f"box.node({name!r}, kind='derivation') requires supports=[...]"
                )
            supporting_ids = ",".join(self.id_of(h) for h in supports)
            payload = dict(
                claim=claim,
                supporting_ids=supporting_ids,
                logical_chain=kwargs.pop("logical_chain", f"Derivation for {name}."),
                reasoning_type=reasoning_type,
            )
            payload.update(kwargs)
            result = json.loads(self._derive._derive_impl(**payload))
            if "error" in result:
                raise RuntimeError(
                    f"box.node({name!r}, kind='derivation') failed: {result['error']}"
                )
            node_id = result["derivation_id"]

        else:
            raise ValueError(
                f"unknown kind {kind!r} — box.node() supports 'observation' "
                f"and 'derivation'"
            )

        handle = Handle(name)
        self._name_to_id[name] = node_id
        self._id_to_name[node_id] = name
        self.handles[name] = handle
        return handle

    # -- Drive — the real machine, never a shortcut ------------------------

    def supersede(self, old: Handle, new: Handle, reason: str = "") -> dict:
        return json.loads(
            self._rev._supersede_impl(
                old_node_id=self.id_of(old),
                new_node_id=self.id_of(new),
                supersede_reason=reason or f"playbox supersede {old.name} -> {new.name}",
            )
        )

    def retract(self, node: Handle, reason: str = "", error_type: str = "other") -> dict:
        return json.loads(
            self._rev._retract_impl(
                node_id=self.id_of(node),
                error_type=error_type,
                reason=reason or f"playbox retract {node.name}",
            )
        )

    def frontier(self, node: Optional[Handle] = None, limit: Optional[int] = None) -> dict:
        node_id = self.id_of(node) if node is not None else None
        return json.loads(self._rev._cascade_frontier_impl(node_id=node_id, limit=limit))

    def resolve(
        self,
        node: Handle,
        kind: str,
        replacement: Optional[Handle] = None,
        note: str = "",
        error_type: str = "other",
    ) -> dict:
        replacement_id = self.id_of(replacement) if replacement is not None else ""
        return json.loads(
            self._rev._cascade_resolve_impl(
                node_id=self.id_of(node),
                resolution_kind=kind,
                replacement_id=replacement_id,
                note=note,
                error_type=error_type,
            )
        )

    # -- Assert -------------------------------------------------------------

    def markers(self, node: Handle) -> list:
        """Per-node, per-event cascade_markers rows. Ids rendered back to
        handle names wherever this box knows them."""
        node_id = self.id_of(node)
        conn = self._core._get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM cascade_markers WHERE node_id = ? ORDER BY id",
                (node_id,),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["node_handle"] = self._handle_of(d.get("node_id"))
            d["root_node_handle"] = self._handle_of(d.get("root_node"))
            d["resolution_replacement_handle"] = self._handle_of(
                d.get("resolution_replacement_id")
            )
            out.append(d)
        return out

    def edges(self, relations=None) -> list:
        """Edges filtered to ``relations`` — defaults to the DERIVED cascade
        set (``engram_core.CASCADE_RELATIONS``), never a literal list (T5)."""
        rels = tuple(relations) if relations is not None else tuple(
            sorted(self._core.CASCADE_RELATIONS)
        )
        conn = self._core._get_db()
        try:
            placeholders = ",".join("?" * len(rels)) if rels else "NULL"
            rows = conn.execute(
                f"SELECT source_id, target_id, relation FROM edges "
                f"WHERE relation IN ({placeholders}) ORDER BY id",
                rels,
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["source_handle"] = self._handle_of(d["source_id"])
            d["target_handle"] = self._handle_of(d["target_id"])
            out.append(d)
        return out

    def confidence(self, node: Handle) -> Optional[float]:
        node_id = self.id_of(node)
        conn = self._core._get_db()
        try:
            row = conn.execute(
                "SELECT confidence FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
        finally:
            conn.close()
        return row["confidence"] if row else None

    def conformance(self) -> dict:
        """The real #2300 partition-conformance flare — never a synthetic
        stand-in."""
        conn = self._core._get_db()
        try:
            return self._conformance.check_partition_conformance(conn)
        finally:
            conn.close()

    def cycles(self) -> list:
        """Any cycle of any length over the DERIVED cascade set
        (``engram_core.CASCADE_RELATIONS``). Returns a list of cycles, each a
        list of handle names (or raw ids where a handle is unknown) in
        traversal order, closing back on its own first element. An empty
        list means the cascade-relation subgraph is currently acyclic.

        This is a diagnostic DFS back-edge detector, not an exhaustive
        elementary-cycle enumerator (which is exponential in the worst
        case) — it is sufficient to answer "is there a cycle reachable from
        here", which is the question the harness exists to let a caller
        investigate. It asserts nothing itself about whether any given
        topology reaches one — see the module docstring and
        ``mutual_crossing``'s builder for why that question is deliberately
        left open here.
        """
        rels = tuple(sorted(self._core.CASCADE_RELATIONS))
        conn = self._core._get_db()
        try:
            placeholders = ",".join("?" * len(rels)) if rels else "NULL"
            rows = conn.execute(
                f"SELECT source_id, target_id FROM edges "
                f"WHERE relation IN ({placeholders})",
                rels,
            ).fetchall()
        finally:
            conn.close()

        adjacency: dict = {}
        for r in rows:
            adjacency.setdefault(r["source_id"], []).append(r["target_id"])

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict = {}
        found: list = []

        def _dfs(node_id, path):
            color[node_id] = GRAY
            path.append(node_id)
            for nxt in adjacency.get(node_id, ()):
                c = color.get(nxt, WHITE)
                if c == WHITE:
                    _dfs(nxt, path)
                elif c == GRAY:
                    idx = path.index(nxt)
                    cyc_ids = path[idx:] + [nxt]
                    found.append(
                        [self._handle_of(n) or n for n in cyc_ids]
                    )
                # BLACK: fully explored elsewhere, no new cycle through here.
            path.pop()
            color[node_id] = BLACK

        for start in list(adjacency.keys()):
            if color.get(start, WHITE) == WHITE:
                _dfs(start, [])
        return found

    # -- Named topologies ---------------------------------------------------

    def _build_topology(self, name: str) -> dict:
        builders = {
            "chain": self._build_chain,
            "diamond": self._build_diamond,
            "triple_diamond": self._build_triple_diamond,
            "nested_diamond": self._build_nested_diamond,
            "mutual_crossing": self._build_mutual_crossing,
        }
        if name not in builders:
            raise ValueError(
                f"unknown topology {name!r} — must be one of {sorted(builders)}"
            )
        return builders[name]()

    def _build_chain(self, length: int = 4) -> dict:
        """base -(derives_from)- d1 -(derives_from)- d2 -...- d{length-1}.

        ``length`` total nodes (1 observation + ``length - 1`` derivations),
        default 4.
        """
        base = self.node("base", kind="observation")
        prev = base
        out = {"base": base}
        for i in range(1, length):
            h = self.node(f"d{i}", kind="derivation", supports=[prev])
            out[f"d{i}"] = h
            prev = h
        return out

    def _build_diamond(self) -> dict:
        """base -> {left, right} -> tip (tip derives from both)."""
        base = self.node("base", kind="observation")
        left = self.node("left", kind="derivation", supports=[base])
        right = self.node("right", kind="derivation", supports=[base])
        tip = self.node("tip", kind="derivation", supports=[left, right])
        return {"base": base, "left": left, "right": right, "tip": tip}

    def _build_triple_diamond(self) -> dict:
        """base -> {a, b, c} -> tip (tip derives from all three) — a
        diamond widened to three parallel middle branches."""
        base = self.node("base", kind="observation")
        a = self.node("a", kind="derivation", supports=[base])
        b = self.node("b", kind="derivation", supports=[base])
        c = self.node("c", kind="derivation", supports=[base])
        tip = self.node("tip", kind="derivation", supports=[a, b, c])
        return {"base": base, "a": a, "b": b, "c": c, "tip": tip}

    def _build_nested_diamond(self) -> dict:
        """An ordinary diamond (base -> {left, right} -> tip) whose `right`
        branch is itself the tip of an inner diamond over the same base
        (base -> {inner_a, inner_b} -> right)."""
        base = self.node("base", kind="observation")
        left = self.node("left", kind="derivation", supports=[base])
        inner_a = self.node("inner_a", kind="derivation", supports=[base])
        inner_b = self.node("inner_b", kind="derivation", supports=[base])
        right = self.node("right", kind="derivation", supports=[inner_a, inner_b])
        tip = self.node("tip", kind="derivation", supports=[left, right])
        return {
            "base": base,
            "left": left,
            "inner_a": inner_a,
            "inner_b": inner_b,
            "right": right,
            "tip": tip,
        }

    def _build_mutual_crossing(self) -> dict:
        """Two supersedes whose replacements each sit in the OTHER's
        dependency subtree — the shape that reaches the cascade cycle
        question (#2013 §12.3 / §12.7's `acknowledged_derives_from` guard).

        Construction, through public verbs only. ``engram_supersede`` is
        type-preserving (old and new must be the same node type — verified
        empirically against ``_supersede_impl``'s own guard), so the two
        nodes being superseded must themselves be derivations, not the root
        observation:

            root              -- one shared observation, ordinary premise
            prem_a, prem_b    -- derivations, both derives_from root
            x1  derives_from prem_a
            x2  derives_from prem_b
            a_prime derives_from x2      -- prem_a's replacement sits in
                                             prem_b's/x2's subtree
            b_prime derives_from x1      -- prem_b's replacement sits in
                                             prem_a's/x1's subtree
            supersede(prem_a, a_prime)
            supersede(prem_b, b_prime)

        After construction, x1 and x2 each carry one pending cascade marker
        (from prem_a's and prem_b's supersedes respectively) and sit on the
        frontier. If x1 is later acknowledged (replacement a_prime) and x2 is
        also acknowledged (replacement b_prime), the acknowledged_derives_from
        edges written would close the cycle
        x1 -> a_prime -> x2 -> b_prime -> x1 over CASCADE_RELATIONS —
        PROVIDED the §12.7 guard doesn't refuse the second acknowledgement
        first. This builder stops BEFORE that adjudication — it does not call
        ``resolve()`` — because whether the cycle is actually reachable
        through the public API is the open question this PR exists to let a
        caller investigate, not to answer (see the module docstring and
        #2356's spec).
        """
        root = self.node("root", kind="observation")
        prem_a = self.node("prem_a", kind="derivation", supports=[root])
        prem_b = self.node("prem_b", kind="derivation", supports=[root])
        x1 = self.node("x1", kind="derivation", supports=[prem_a])
        x2 = self.node("x2", kind="derivation", supports=[prem_b])
        a_prime = self.node("a_prime", kind="derivation", supports=[x2])
        b_prime = self.node("b_prime", kind="derivation", supports=[x1])

        r1 = self.supersede(prem_a, a_prime)
        if "error" in r1:
            raise RuntimeError(
                f"mutual_crossing topology setup failed at "
                f"supersede(prem_a, a_prime): {r1['error']}"
            )
        r2 = self.supersede(prem_b, b_prime)
        if "error" in r2:
            raise RuntimeError(
                f"mutual_crossing topology setup failed at "
                f"supersede(prem_b, b_prime): {r2['error']}"
            )

        return {
            "root": root,
            "prem_a": prem_a,
            "prem_b": prem_b,
            "x1": x1,
            "x2": x2,
            "a_prime": a_prime,
            "b_prime": b_prime,
        }


def cascade_playbox(topology: Optional[str] = None) -> Playbox:
    """``with cascade_playbox(topology="diamond") as box: ...``

    Returns a not-yet-entered :class:`Playbox`; the ``with`` block IS the
    lifetime (see the module docstring). ``topology=None`` starts empty —
    build a custom fixture with ``box.node()``.
    """
    return Playbox(topology=topology)
