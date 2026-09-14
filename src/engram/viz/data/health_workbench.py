"""viz.data.health_workbench -- backend data assembly for the Curve
Workbench (engram-alpha#2727), developer tier only.

Exposes the SHIPPED scoring model's curve/importance/aspect-points
declarations (``health.model`` / ``health.curves`` / ``health.engine.MODEL``)
so the workbench page can seed its editable curve editors from the REAL
model ``health.engine.write_snapshot`` uses -- never a hand-copied
re-derivation.

Deliberately separate from ``viz.data.health_v2`` (which treats a stored
snapshot as a plain JSON file and does NOT import anything from
``src/engram/health/``, per that module's own docstring) -- THIS module's
whole job is reading ``health/curves.py``'s and ``health/model.py``'s live
Python objects directly (read-only: nothing here constructs, mutates, or
writes a ``ScoringModel`` or any file -- it only calls the two existing
``*_jsonable`` functions those modules already ship). This is necessary
because the model's curve/importance declarations are NOT (yet) serialized
into the health-v2 snapshot itself (``viz/data/health_v2.py``'s own
``"today.curve": None  # STUBBED`` note) -- extending that endpoint's own
contract to add them is explicitly out of scope for #2727 (its own
dispatching brief: "do not extend the API in this PR" for anything the
existing series/snapshot contract doesn't already carry). Reading the
live ``health.engine.MODEL`` singleton directly, via its own two existing
``*_jsonable`` exporters, is the same data ``write_snapshot`` embeds into
every snapshot's own ``model_defs``/``curve_defs`` blocks (health/engine.py
lines ~1511-1522) -- just resolved from the running module instead of a
stored file, because the workbench always needs the CURRENT shipped model
(the latest snapshot on disk may be older than the running code, or may
not exist at all yet for a fresh install).

No HTTP, no HTML -- unit-testable in isolation, same convention as this
package's sibling loaders (``health_v2.py``, ``stats.py``, ``config.py``).
"""

from __future__ import annotations


def get_workbench_model() -> dict:
    """The shipped ``ScoringModel``'s aspect/metric/curve declarations, or
    a fail-honest ``{"available": False, "reason": ...}`` dict if
    ``health.engine``/``health.model``/``health.curves`` are not importable
    in this process (never raises -- same "always a dict, fail-honest on
    error" convention as ``viz.data.health_v2.get_health_v2_data``).

    Shape when available::

        {"available": True,
         "model_defs": health.model.model_defs_jsonable(MODEL),
         "curve_defs": health.curves.curve_defs_jsonable(MODEL.curves)}

    ``model_defs["metrics"]`` carries, per metric: ``signal``, ``aspect``,
    ``curve`` (a curve NAME, keying into ``curve_defs``), ``importance``.
    ``model_defs["aspects"]`` carries, per aspect: ``name``, ``points``,
    ``reference_metric``. ``curve_defs`` carries, per curve name: ``knots``,
    ``input_domain``, ``monotone_direction``, ``output_range``,
    ``observed_range``. See ``health.model.model_defs_jsonable`` /
    ``health.curves.curve_defs_jsonable``'s own docstrings for the full
    field list -- this function does not reshape either.
    """
    # `stage` is an explicit flag, set at each point in this function where
    # a distinct failure could occur, rather than inferred after the fact
    # from which locals happen to be bound (engram-alpha#2735): the OLD
    # `"health_engine" not in locals()` discriminator mislabeled an import
    # failure of `health.model` (the THIRD of the three imports below) as
    # an export failure, because by the time that import statement runs,
    # `health_curves` and `health_engine` -- the two imports before it --
    # are ALREADY bound in locals(), so the locals()-based check read
    # False even though nothing had exported yet. Since this is a
    # never-raises function, `reason` is the entire diagnostic surface for
    # whoever's debugging why the workbench is unavailable, so it must
    # name the actual failing stage, not a guess reconstructed from
    # incidental binding state.
    stage = "model_module_unavailable"
    try:
        # Lazy import -- same convention as viz.data.health_v2's own
        # "one deliberate exception" import of health.series (see that
        # module's docstring): `health` resolves as a top-level package
        # whenever this process's cwd (production: the plugin root, per
        # viz/server.py's own INVOCATION NOTE) or sys.path (tests:
        # conftest.py inserts src/engram) already makes it importable --
        # no extra path surgery needed here. Function-scoped (not
        # module-level) so this module stays importable, and this whole
        # feature stays gateable, even in a build/environment where
        # health/ isn't on the path at all -- see viz.workbench's
        # startup-time dependency check for the caller-side half of that
        # gate (viz.server's old `_workbench_available()`, which this
        # comment used to cite, was removed by engram-alpha#2754).
        from health import curves as health_curves
        from health import engine as health_engine
        from health import model as health_model

        # All three imports above succeeded -- any failure from here on
        # is a genuine EXPORT failure (the *_jsonable() calls), not an
        # import failure.
        stage = "model_export_failed"
        model = health_engine.MODEL
        model_defs = health_model.model_defs_jsonable(model)
        curve_defs = health_curves.curve_defs_jsonable(model.curves)
    except Exception as exc:
        # Single guard over both the imports AND the two *_jsonable() calls
        # -- the docstring's "never raises" guarantee has to hold end-to-end,
        # not just across the import boundary: a future shape change in
        # health.model or health.curves could make model_defs_jsonable /
        # curve_defs_jsonable raise too, and that must fail-honest the same
        # way an import failure does, not bubble up as a 500.
        return {"available": False, "reason": f"{stage}: {exc}"}

    return {
        "available": True,
        "model_defs": model_defs,
        "curve_defs": curve_defs,
    }
