"""viz.data.health_v2 -- the health-v2 SSoT loader for /api/health/v2
(engram-alpha#2518; per the issue's ownership split, this module builds
``signal_row.definition``/``today``/``provenance`` and
``aspect_tile``/``composed`` from a stored snapshot; the detail-view
frontend that CONSUMES this endpoint is a separate PR, tracked on #2518 --
this module and the ``/api/health/v2`` route are backend-only).

Reads the LATEST health-score snapshot JSON file written by
``health.engine.write_snapshot`` under
``$ENGRAM_HOME/health-snapshots/<turn>-<checkpoint_mode>-<timestamp>.json``
and assembles the shape a future detail-view front-end will consume.

Deliberately does NOT import anything from ``src/engram/health/`` -- this
module treats the snapshot as a plain JSON file on disk (the health engine
package is a different layer; viz just reads whatever JSON shape lands on
disk). All snapshot-shape knowledge here is a documented CONTRACT with
``health/engine.py``'s ``build_snapshot``/``write_snapshot`` and
``health/signals.py``'s ``signal_defs_jsonable`` -- re-derived by reading
those modules' source, never imported.

**One deliberate exception (engram-alpha#2575 PR C1): ``get_health_v2_series``
imports ``health.series`` lazily.** ``series.db`` is ``health.series``'s OWN
store and ``export()`` is that module's own query over it -- duplicating
that SQL here would be exactly the anti-pattern this docstring's "no
health/ imports" rule exists to avoid (re-deriving another module's
contract instead of calling it). Every OTHER function in this file keeps
the plain-JSON-file contract described above unchanged.

No HTTP, no HTML -- unit-testable in isolation, same convention as this
package's sibling loaders (``stats.py``, ``health.py``, ``calibration.py``).

CROSS-MODULE NOTE (mirrors ``stats.py``'s own docstring note): this module
reads the package-wide ``AGENTS`` registry via ``viz_agents.AGENTS``
(attribute access on the module object), never a bound
``from viz.agents import AGENTS`` -- the latter would capture a reference
to the dict object that existed at import time and go stale the moment
``viz.agents._refresh_agents_if_stale()`` reassigns the module-level name
to a new dict.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from .. import agents as viz_agents

# Matches health.engine.write_snapshot's own filename shape exactly:
#   filename = f"{turn}-{checkpoint_mode}-{ts}.json"
#   ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
# `turn` is NOT zero-padded (a plain int()), so lexical/string sort can
# misorder e.g. "10-..." before "9-..." -- callers of
# `_latest_snapshot_filename` below must sort by the PARSED integer, never
# by filename string-sort. The middle group is deliberately `[^-]+` rather
# than a hardcoded "nap|pre_sleep|post_dream" alternation, so a filename
# still matches if that checkpoint_mode enum grows -- none of today's three
# values contain a literal "-", so the 3-way split is exact today either way.
_SNAPSHOT_FILENAME_RE = re.compile(r"^(\d+)-([^-]+)-(\d{8}T\d{6}Z)\.json$")


def _resolve_health_snapshots_dir(agent_name: str) -> str:
    """Resolve agent_name to its health-snapshots directory. Returns empty
    string if the agent is unknown -- same convention as
    ``stats._resolve_logs_index``."""
    agent_meta = viz_agents.AGENTS.get(agent_name)
    if not agent_meta:
        return ""
    # AGENTS metadata stores the agent's knowledge-db path under key "db"
    # (see viz/data/stats.py's identical comment for the citation). The
    # health-snapshots dir lives at $ENGRAM_HOME/health-snapshots, where
    # $ENGRAM_HOME is the parent of the knowledge.db file -- same layout
    # convention health/engine.py's own `_snapshot_dir()` uses
    # (`core.DATA_DIR / "health-snapshots"`).
    db_path = agent_meta.get("db", "")
    if not db_path:
        return ""
    engram_home = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(engram_home, "health-snapshots")


def _latest_snapshot_filename(snapshots_dir: str) -> "str | None":
    """Pick the highest-turn-number snapshot filename in *snapshots_dir*,
    ignoring anything that doesn't match ``_SNAPSHOT_FILENAME_RE`` -- this
    excludes the fixed-name dream projection
    (``health-snapshot-dream.json``) and any ``.error`` sentinel sibling,
    neither of which is turn-numbered. Sorts by a TOTAL, deterministic
    ordering key -- ``(turn, ts_token, mtime_ns, name)`` -- so no two
    distinct files can ever tie (round-3.6 fix, engram-alpha#2940
    reviewer-fairy round on Mira's own colleague review; identical fix
    landed in this function's sibling copy in ``health/engine.py`` -- see
    that copy's docstring for the full reasoning behind each key
    component):

    1. Parsed integer ``turn`` (turn only advances at sleep -- same-turn
       checkpoints are the normal case, not an edge case).
    2. The filename's own ``ts_token`` (second-resolution, sorts correctly
       as a plain string) -- but two DIFFERENT-mode files (e.g. ``nap`` and
       ``pre_sleep``) can genuinely share both turn and this token if
       written within the same wall-clock second.
    3. The file's own ``mtime_ns`` (nanosecond resolution) -- breaks that
       tie by reading real write-order off the filesystem. A file that
       fails to ``stat()`` (e.g. a race with deletion) sorts as ``-1``
       rather than raising, so it never wins a tie it can't prove it
       belongs in.
    4. The filename string itself -- a purely mechanical final tiebreaker
       guaranteeing a TOTAL ordering; never fires in practice.

    Returns ``None`` if the directory doesn't exist or has no matching
    files."""
    try:
        names = os.listdir(snapshots_dir)
    except OSError:
        return None
    best_name = None
    best_key = None
    for name in names:
        m = _SNAPSHOT_FILENAME_RE.match(name)
        if not m:
            continue
        turn = int(m.group(1))
        ts_token = m.group(3)
        try:
            mtime_ns = os.stat(os.path.join(snapshots_dir, name)).st_mtime_ns
        except OSError:
            mtime_ns = -1  # vanished between listdir() and stat() -- never wins a tie
        key = (turn, ts_token, mtime_ns, name)
        if best_key is None or key > best_key:
            best_key = key
            best_name = name
    return best_name


def _written_at(snapshot_path: str, filename: str) -> str:
    """Best-effort ISO-8601 write timestamp. Prefers parsing the filename's
    own timestamp component -- the exact ``%Y%m%dT%H%M%SZ`` format
    ``health.engine.write_snapshot`` stamps
    (``ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())``) -- and falls
    back to the file's own mtime (``os.path.getmtime``, ISO-formatted UTC)
    when the filename doesn't parse (e.g. a hand-placed fixture with a
    nonstandard name). Returns "" if even the mtime fallback fails."""
    m = _SNAPSHOT_FILENAME_RE.match(filename)
    if m:
        ts_token = m.group(3)
        try:
            dt = datetime.strptime(ts_token, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass  # fall through to the mtime fallback below
    try:
        return datetime.fromtimestamp(
            os.path.getmtime(snapshot_path), tz=timezone.utc
        ).isoformat()
    except OSError:
        return ""


def _metric_to_aspect_map(config: dict) -> dict:
    """``{metric_name: aspect_name}`` from the snapshot's own ``config``
    block (``health.engine._config_to_dict``'s output) -- structural
    membership only (which aspect a metric is nested under), which is
    reliable regardless of the ``scored``/``state_valued`` fields on the
    same block being vestigial for real composition (see
    ``_derive_status``'s docstring for why those two fields are NOT used
    here)."""
    mapping: dict = {}
    for aspect in (config.get("aspects") or []):
        aspect_name = aspect.get("name")
        for metric in (aspect.get("metrics") or []):
            metric_name = metric.get("name")
            if metric_name:
                mapping[metric_name] = aspect_name
    return mapping


def _derive_status(
    name: str, signals: dict, scores: dict, metric_to_aspect: dict
) -> str:
    """One of "scored"/"measured_not_scored"/"held_out"/"unavailable" for
    signal *name*.

    JUDGMENT CALL (flagged in the dispatching report): the snapshot's
    ``config`` block's per-metric ``scored`` field is EXPLICITLY vestigial
    for real composition as of #1991 PR5 (see health/engine.py's own module
    docstring -- "scored``/``state_valued`` are now VESTIGIAL for real
    composition") -- the live production ``ScoringModel`` uses a per-metric
    ``importance`` float that is NOT itself serialized into the snapshot at
    all. So this derivation does NOT read ``config[...]["scored"]`` as an
    importance proxy (it is uniformly True for all 9 metrics in the current
    CONFIG, which would make every signal read as "scored"). Instead it
    reconstructs the same four states purely from fields that DO reflect the
    real ScoringModel's behavior on THIS snapshot:

      1. "unavailable" -- ``signals[name] == "UNAVAILABLE"`` (the L1 signal
         itself declined this run -- empty denominator or a failed
         environment gate).
      2. "held_out" -- the signal's aspect (found via ``metric_to_aspect``,
         which IS reliable -- aspect nesting is structural, untouched by the
         vestigial-field issue above) appears in
         ``scores["unscored_aspects"]``. An aspect lands there when its OWN
         ``aspect_score`` is UNAVAILABLE -- either it has zero admitted
         (importance > 0) metrics declared at all (``provenance_integrity``
         under the shipped v1 table), or every one of its admitted metrics
         is unavailable this run. Since step 1 already confirmed THIS
         signal's own raw value is available, an aspect-level UNAVAILABLE at
         this point can only be the STRUCTURAL zero-admitted case (a
         same-aspect metric with importance > 0 and an available raw value
         would have kept the aspect scored) -- so this correctly identifies
         a metric with importance == 0 in a wholly-diagnostic aspect.
      3. "measured_not_scored" -- the aspect IS scored (not in
         ``unscored_aspects``), but *this* signal's name is absent from
         ``scores["aspect_coverage"][aspect]["metrics_in_play"]`` -- i.e. it
         was available but not admitted into the aspect's weighted mean
         (importance == 0 within an otherwise-scored aspect, e.g.
         ``giant_component_fraction`` under ``retrievability`` today).
      4. "scored" -- otherwise (present in ``metrics_in_play``).

    This reproduces the real importance-driven states exactly for the
    shipped v1 model without needing importance itself in the snapshot,
    because "admitted and available" is exactly what ``metrics_in_play``
    already reports per SCORED aspect, and "zero admitted" is exactly what
    membership in ``unscored_aspects`` reports per aspect as a whole.

    CAVEAT (found in colleague review, verified against ``model.py``'s
    actual v1 bindings rather than left as a hypothetical): step 2's
    inference -- "this signal is available, so its aspect landing in
    ``unscored_aspects`` can only mean zero ADMITTED metrics" -- would be
    WRONG for an aspect that has multiple admitted (importance>0) metrics
    with INDEPENDENTLY-failable denominators/gates *and* a diagnostic
    (importance==0) metric, if enough of the admitted ones went unavailable
    at once while the diagnostic one stayed available: that aspect would
    correctly land in ``unscored_aspects`` (all admitted metrics
    unavailable), but the diagnostic metric would then be mislabeled
    "held_out" (implying zero admitted metrics structurally) rather than
    "measured_not_scored" (its true state, unrelated to the co-incidental
    unavailability of its aspect-mates). Checked whether this actually
    fires today: it does not. Every current aspect with a mixed
    admitted+diagnostic membership (``retrievability``: embedding_coverage/
    recall_summary_coverage/recall_keywords_coverage admitted,
    giant_component_fraction diagnostic) has all its admitted-vs-diagnostic
    metrics sharing the SAME denominator (``claim_bearing_current``), so a
    denominator-zero that unavails the admitted ones also unavails the
    diagnostic one -- which step 1 catches directly, never reaching this
    branch. ``epistemic_integrity``'s three metrics are independently
    failable but has no diagnostic metric to mislabel.
    ``provenance_integrity`` is already fully zero-admitted (not a mixed
    case). So this derivation is correct for the SHIPPED v1 model, but its
    correctness is a property of the model's current shape, not a
    structural invariant this function enforces -- a future aspect adding a
    diagnostic metric alongside independently-failable admitted ones would
    silently break it. No fix applied here: doing so would need per-metric
    importance serialized into the snapshot -- now available as of
    engram-alpha#2575 PR C0's ``model_defs`` blob (``metrics[].importance``)
    -- flagging for whoever wires this function to read it.
    """
    if signals.get(name) == "UNAVAILABLE":
        return "unavailable"

    aspect_name = metric_to_aspect.get(name)
    unscored_aspects = scores.get("unscored_aspects") or []
    if aspect_name is not None and aspect_name in unscored_aspects:
        return "held_out"

    aspect_coverage = scores.get("aspect_coverage") or {}
    metrics_in_play = []
    if aspect_name is not None:
        metrics_in_play = (aspect_coverage.get(aspect_name) or {}).get(
            "metrics_in_play"
        ) or []
    if name in metrics_in_play:
        return "scored"
    return "measured_not_scored"


def _polarity_for_form(form) -> str:
    """"increasing" for a "rate" signal, "decreasing" for "one_minus_rate",
    else "unknown" -- read purely off the signal's own declared ``form``,
    never from a name-based guess (a teammate's explicit correction: do NOT
    assume every "one_minus_rate" row is a hardcoded "coverage" signal)."""
    if form == "rate":
        return "increasing"
    if form == "one_minus_rate":
        return "decreasing"
    return "unknown"


def _unavailable_policy_text(gates: list) -> str:
    """Human-readable unavailable-policy string for one signal's
    definition block."""
    text = "denominator == 0 -> UNAVAILABLE"
    if gates:
        gate_words = " and ".join(gates)
        text += f"; also requires {gate_words} to be true"
    return text


def _signal_row(name: str, snapshot: dict, metric_to_aspect: dict) -> dict:
    signal_defs = snapshot.get("signal_defs") or {}
    signals = snapshot.get("signals") or {}
    scores = snapshot.get("scores") or {}
    measurements = snapshot.get("measurements") or {}
    metrics_scores = scores.get("metrics") or {}

    d = signal_defs.get(name) or {}
    num_key = d.get("numerator")
    den_key = d.get("denominator")
    gates = d.get("gates") or []
    form = d.get("form")

    return {
        "name": name,
        "status": _derive_status(name, signals, scores, metric_to_aspect),
        "definition": {
            "formula": d.get("formula"),
            "numerator": {
                "key": num_key,
                # "label" == the raw L0 key itself, deliberately -- a
                # teammate explicitly said not to build a per-signal label
                # map here; real human-readable labels land on
                # MeasurementDef in a separate issue (#2580), not per-signal
                # here.
                "label": num_key,
                "value": measurements.get(num_key) if num_key else None,
            },
            "denominator": {
                "key": den_key,
                "label": den_key,
                "value": measurements.get(den_key) if den_key else None,
            },
            "polarity": _polarity_for_form(form),
            "unavailable_policy": _unavailable_policy_text(gates),
            "environment_gates": [
                {"key": g, "value": measurements.get(g)} for g in gates
            ],
        },
        "today": {
            # The L1 pre-curve value (health/engine.py's build_snapshot:
            # `signals[metric_name] = _to_jsonable(raw)`, BEFORE
            # `curve.curve_fn(raw)` produces the "score" below) -- distinct
            # from both the raw L0 counts (`measurements`) and the curved
            # score. May be the string "UNAVAILABLE", same passthrough
            # convention `scores["metrics"]` already uses.
            "raw": signals.get(name),
            "score": metrics_scores.get(name),
            # STUBBED. #2578 merged (curve_defs is a real, declared field
            # in the snapshot now: {curve_name: {knots, input_domain,
            # monotone_direction, output_range, observed_range}}) -- but
            # curve_defs is keyed by CURVE name, not signal name. The
            # per-signal mapping now EXISTS as of engram-alpha#2575 PR C0's
            # ``model_defs`` blob (``metrics[].curve``, keyed by signal) --
            # flagged on #2518 rather than worked around here; wiring this
            # field to read ``model_defs`` is left for whoever picks this
            # up.
            "curve": None,
        },
    }


def get_health_v2_data(agent_name: str) -> dict:
    """Load the latest health-v2 snapshot for *agent_name* and assemble the
    detail-view payload. Never raises -- always returns a dict, with
    ``has_snapshot: False`` + a ``reason`` string for every empty/error
    case."""
    snapshots_dir = _resolve_health_snapshots_dir(agent_name)
    if not snapshots_dir:
        return {"has_snapshot": False, "reason": f"Unknown agent: {agent_name!r}"}

    filename = _latest_snapshot_filename(snapshots_dir)
    if filename is None:
        if not os.path.isdir(snapshots_dir):
            return {
                "has_snapshot": False,
                "reason": "No health-snapshots directory yet -- has a checkpoint ever run?",
            }
        return {
            "has_snapshot": False,
            "reason": "health-snapshots directory exists but has no snapshot files yet",
        }

    snapshot_path = os.path.join(snapshots_dir, filename)
    try:
        with open(snapshot_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "has_snapshot": False,
            "reason": f"Failed to read/parse snapshot {filename!r}: {exc}",
        }

    # DELIBERATE, not an oversight (#2587): this guard refuses even when a
    # perfectly usable current-format snapshot exists at a LOWER turn number
    # nearby -- `_latest_snapshot_filename` only ever picks the highest-turn
    # file, so it never sees that older, usable snapshot to fall back to.
    # Falling back would show STALE data as CURRENT, which is exactly the
    # defect this whole health-v2 panel exists to prevent. A higher-turn
    # old-format file sitting beside a lower-turn current-format one means a
    # STALE WRITER (a pre-upgrade MCP server process still running) wrote it
    # after this seat had already upgraded -- which is precisely what the
    # reason message below already tells the reader to fix (rebuild +
    # reconnect the MCP server retires the stale writer). Do not turn this
    # refusal into a fallback to the older snapshot.
    if "measurements" not in snapshot:
        return {
            "has_snapshot": False,
            "reason": (
                f"Snapshot {filename!r} is an old-format (pre-health-v2) "
                "snapshot -- it is missing the 'measurements' key. Rebuild "
                "the plugin build and reconnect the MCP server to write a "
                "current-format snapshot."
            ),
        }

    scores = snapshot.get("scores") or {}
    config = snapshot.get("config") or {}
    signal_defs = snapshot.get("signal_defs") or {}
    metric_to_aspect = _metric_to_aspect_map(config)

    snapshot_id = filename[:-len(".json")] if filename.endswith(".json") else filename

    provenance = {
        "snapshot_id": snapshot_id,
        "turn": snapshot.get("turn"),
        "checkpoint_mode": snapshot.get("checkpoint_mode"),
        "written_at": _written_at(snapshot_path, filename),
        "health_version": snapshot.get("health_version"),
        "raw_schema_version": snapshot.get("raw_schema_version"),
        "model_version": snapshot.get("model_version"),
        "anchor": snapshot.get("anchor"),
    }

    signal_rows = [
        _signal_row(name, snapshot, metric_to_aspect) for name in signal_defs
    ]

    aspects_scores = scores.get("aspects") or {}
    unscored_aspects = scores.get("unscored_aspects") or []
    aspect_coverage = scores.get("aspect_coverage") or {}
    aspect_tile = []
    for aspect_name, score in aspects_scores.items():
        entry = {"name": aspect_name, "score": score}
        # "unscored"/"coverage" are additive -- both read straight off
        # existing snapshot fields with no invented shape (JUDGMENT CALL,
        # flagged in the dispatching report); "points" was deliberately
        # left off -- see that report's ambiguous-decision log.
        entry["unscored"] = aspect_name in unscored_aspects
        cov = aspect_coverage.get(aspect_name)
        if cov is not None:
            entry["coverage"] = cov.get("coverage")
        aspect_tile.append(entry)

    composed = {
        "value": scores.get("composed"),
        "points_in_play": scores.get("points_in_play"),
        "composed_coverage": scores.get("composed_coverage"),
        "unscored_aspects": unscored_aspects,
    }

    return {
        "has_snapshot": True,
        "provenance": provenance,
        "signal_rows": signal_rows,
        "aspect_tile": aspect_tile,
        "composed": composed,
    }


def get_health_v2_series(agent_name: str, version: "str | None" = None, signal: "str | None" = None) -> dict:
    """The ``/api/health/v2/series`` payload (engram-alpha#2575 PR C1) --
    resolves *agent_name*'s ``series.db`` (same directory convention as
    ``_resolve_health_snapshots_dir`` above -- ``series.db`` lives beside
    the ``*.json`` snapshot files, under ``$ENGRAM_HOME/health-snapshots``)
    and calls ``health.series.export(db_path, version, signal)`` -- see
    this module's own docstring for why THIS ONE function is allowed to
    import ``health.series`` (unlike every other function here, which
    treats a snapshot as a plain JSON file).

    Never raises: an unknown agent, a missing ``series.db``, or a failed
    ``health.series`` import are each their own fail-honest ``{error,
    reason}`` dict (never an exception) -- same convention ``export``
    itself already uses (``no_db``/``version_absent``/``signal_unknown``/
    ``unreadable``); this function adds exactly one more reason,
    ``series_module_unavailable``, for the import failing."""
    snapshots_dir = _resolve_health_snapshots_dir(agent_name)
    if not snapshots_dir:
        return {"error": f"Unknown agent: {agent_name!r}", "reason": "no_db"}

    db_path = os.path.join(snapshots_dir, "series.db")
    if not os.path.exists(db_path):
        return {
            "error": "no series store for this agent yet",
            "reason": "no_db",
            "db_path": db_path,
        }

    try:
        # `health` resolves as a top-level package whenever this process's
        # cwd (production: the plugin root, per viz/server.py's own
        # INVOCATION NOTE) or `sys.path` (tests: conftest.py inserts
        # `src/engram`) already makes it importable -- no extra path
        # surgery needed here, matching every other lazy `from health
        # import ...` in this codebase.
        from health import series as health_series
    except Exception as exc:
        return {"error": str(exc), "reason": "series_module_unavailable"}

    return health_series.export(db_path, version, signal)
