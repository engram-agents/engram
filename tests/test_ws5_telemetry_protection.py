"""Tests for WS5 (alpha #2574 epic #2013, issue #2574's server-side
telemetry protection + resolver) -- docs/internal/2574-telemetry-design.md §9 tests 1
and 3b, plus the gate round-trip. Server-side twin of
tests/test_ws3_telemetry_resolver.py: same discipline, structurally
different half -- fan-out present, no per-call emitter construction (see
that file's own module docstring, and docs/internal/2574-telemetry-design.md §10, for
why these are two separate workstreams).

Covers the 9 server-side ``emit_if_initialized()`` sites (engram_derive.py,
engram_observation.py, engram_query.py, engram_revision.py) and their
dispatch targets -- the 9 ``@mcp.tool``-decorated functions in server.py --
end-to-end through server.py's own ``payload_json`` wrapper. That is the one
call surface that exercises BOTH halves of this workstream in a single
invocation: site-level outcome recording (in the family module a given site
lives in) and target-level ``reached`` writes (in server.py, the dispatch
target's own first line).

This file legitimately ``import server`` (tests/IMPORT_RUBRIC.md R2) to reach
the ``reached`` writes, which live in server.py's own dispatch targets, not
in the family modules the emit sites themselves live in -- added to
``server_import_allowlist`` in src/engram/packaging/test-map.json (this file
tests telemetry placed INSIDE the payload_json wrapper's contract, not the
wrapper's own contract, so the ``test_*_payload.py`` naming convention does
not fit).
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "engram"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Top-level imports declare this file's dependency edges for
# run_touched_tests.py's static import graph (IMPORT_RUBRIC.md R1) -- this
# file exercises code in all four family modules (query/observation directly
# patched below; derive/revision exercised transitively via the fan-out/
# fan-in reached tests). The bound names themselves are NOT used for
# patching after any fresh_server() call -- see `_live()`'s own docstring.
import engram_derive  # noqa: E402,F401
import engram_observation  # noqa: E402,F401
import engram_query  # noqa: E402,F401
import engram_revision  # noqa: E402,F401
from tools import telemetry_manifest_gate  # noqa: E402


TEST_DIR = tempfile.mkdtemp(prefix="engram_test_ws5_")
atexit.register(lambda: shutil.rmtree(TEST_DIR, ignore_errors=True))


def fresh_server():
    """Purge + reimport server + every engram_* module (same convention as
    tests/test_derive_payload.py, tests/test_query_payload.py, etc. --
    80+ files in this suite). This also resets engram_log_emitter's
    `_EMITTER_INSTANCE` singleton to None as a side effect of the reimport
    (server.py's own top-level `from engram_log_emitter import Emitter,
    emit_if_initialized` re-executes against a brand-new module object), so
    no test here needs tests/test_ws3_telemetry_resolver.py's manual
    singleton-reset trick -- that trick is needed there because hooks are
    loaded via importlib.spec_from_file_location, which bypasses the normal
    sys.modules re-import path this helper relies on.
    """
    if os.path.exists(TEST_DIR):
        shutil.rmtree(TEST_DIR)
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server
    server._configure_paths(TEST_DIR)
    server._ensure_data_dir()
    os.environ["ENGRAM_NO_EMBEDDINGS"] = "1"
    return server


def _live(mod_name: str):
    """Re-fetch a repo module from sys.modules AT POINT OF USE, never via
    this file's own collection-time top-level import binding.

    `fresh_server()` (this file, and every *_payload.py file in this suite)
    purges every `engram_*`-prefixed module from sys.modules and lets
    `import server` transitively reimport fresh copies. This file's own
    top-level `import engram_query` (etc., above) exists ONLY to declare the
    dependency edge for the static import graph (IMPORT_RUBRIC.md R1) --
    using that bound name directly to patch something AFTER any
    `fresh_server()` call would silently patch an orphaned module object
    server.py has already moved on from. Identical bug class to
    tests/test_ws3_telemetry_resolver.py's `_live_emitter_module()`, one
    layer up: there it is `engram_log_emitter` going stale after a hook's own
    importlib-based reload; here it is the family modules going stale after
    `fresh_server()`'s sys.modules purge.
    """
    return sys.modules[mod_name]


def _spy(store: list):
    def _fn(manifest_id, event_type, fields=None):
        store.append({
            "manifest_id": manifest_id,
            "event_type": event_type,
            "fields": dict(fields or {}),
        })
        return True

    return _fn


def _make_always_raises():
    """A write_telemetry_record replacement that always raises, plus a
    mutable call counter -- so a protection test can confirm the raising
    path was actually exercised (otherwise the assertion "caller returned
    normally" would pass vacuously without the try/except ever firing)."""
    counter = {"n": 0}

    def _fn(manifest_id, event_type, fields=None):
        counter["n"] += 1
        raise RuntimeError("boom: simulated write_telemetry_record failure")

    return _fn, counter


# ---------------------------------------------------------------------------
# 1. Suppressor test (§9 test 1), adapted for NO_EMITTER and DROPPED both
#    being reachable server-side (unlike WS3's hook side, which never sees
#    NO_EMITTER as a *steady* state).
# ---------------------------------------------------------------------------


class TestSuppressor:
    def test_no_emitter_outcome_and_caller_returns_normally(self, monkeypatch):
        """No Emitter.init() call at all -- the permanent pre-Part-1 state
        for every one of these 9 sites (docs/internal/2574-telemetry-design.md §0.1)."""
        server = fresh_server()
        eq = _live("engram_query")
        calls: list = []
        monkeypatch.setattr(eq, "write_telemetry_record", _spy(calls))

        r = json.loads(server.engram_query(payload_json=json.dumps({"query": "hello"})))
        assert "error" not in r, r  # caller returned normally

        rows = [c for c in calls if c["manifest_id"] == "server.engram_query"]
        assert rows, f"expected an outcome row for server.engram_query; got {calls}"
        assert rows[-1]["fields"]["outcome"] == "no_emitter", rows[-1]

    def test_dropped_outcome_distinct_from_no_emitter(self, monkeypatch):
        """A REAL emitter exists (Emitter.get() is not None) but its own
        emit() fails -- DROPPED, not NO_EMITTER. Conflating these two is the
        round-2 defect §9 test 1 exists to pin (design doc §5's three-valued
        outcome model)."""
        server = fresh_server()
        eq = _live("engram_query")
        elm = _live("engram_log_emitter")
        calls: list = []
        monkeypatch.setattr(eq, "write_telemetry_record", _spy(calls))

        elm.Emitter.init(session_id="t", transcript_path="", logs_dir=Path(TEST_DIR) / "logs")
        monkeypatch.setattr(elm.Emitter, "emit", lambda self, *a, **k: None)

        r = json.loads(server.engram_query(payload_json=json.dumps({"query": "hello"})))
        assert "error" not in r, r

        rows = [c for c in calls if c["manifest_id"] == "server.engram_query"]
        assert rows, f"expected an outcome row; got {calls}"
        assert rows[-1]["fields"]["outcome"] == "dropped", rows[-1]

    def test_mutation_control_reddens_when_outcome_recording_removed(self, monkeypatch):
        """Can-it-fail control: simulate the outcome-recording call being
        deleted (a spy that only records `reached` rows, discarding
        engram.tool.engram_call rows -- the same observable shape as the
        call not being there at all) and confirm the suppressor assertion
        above does NOT hold, while the caller-not-disrupted half stays
        green."""
        server = fresh_server()
        eq = _live("engram_query")
        calls: list = []

        def _reached_only(manifest_id, event_type, fields=None):
            if event_type == "engram.telemetry.reached":
                calls.append({"manifest_id": manifest_id, "event_type": event_type, "fields": dict(fields or {})})
            return True

        monkeypatch.setattr(eq, "write_telemetry_record", _reached_only)

        r = json.loads(server.engram_query(payload_json=json.dumps({"query": "hello"})))
        assert "error" not in r, r  # caller-not-disrupted control stays green

        rows = [
            c for c in calls
            if c["manifest_id"] == "server.engram_query" and c["event_type"] == "engram.tool.engram_call"
        ]
        assert not rows, (
            f"mutation control: outcome row must NOT exist once outcome-recording "
            f"is effectively removed; got {rows}"
        )


# ---------------------------------------------------------------------------
# 2. Protection test (§10's two shapes): force a failure in the workstream's
#    OWN new code (the outcome-recording call) and confirm the MCP tool call
#    still returns normally -- never in emit_if_initialized() itself (which
#    never raises per its own contract) or in the pre-existing surrounding
#    try/finally (which this workstream deliberately does not widen; see
#    each site's own in-source comment for the reasoning).
# ---------------------------------------------------------------------------


class TestProtection:
    def test_try_finally_no_except_site_survives_outcome_recording_failure(self, monkeypatch):
        """engram_query.py:2921 (server.engram_query) sat inside a
        pre-existing try/finally with NO except clause
        (docs/internal/2574-telemetry-design.md §0.1) -- this workstream's own new,
        narrowly-scoped try/except around the new code is what must catch
        this raise, not the pre-existing try/finally (which this workstream
        deliberately never widens)."""
        server = fresh_server()
        eq = _live("engram_query")
        raiser, counter = _make_always_raises()
        monkeypatch.setattr(eq, "write_telemetry_record", raiser)

        r = json.loads(server.engram_query(payload_json=json.dumps({"query": "hello"})))
        assert "error" not in r, (
            f"MCP tool call must return normally even when the new "
            f"outcome-recording call raises; got {r}"
        )
        assert counter["n"] >= 1, (
            "the raising write_telemetry_record was never invoked -- this "
            "test would pass vacuously without exercising the protection at all"
        )

    def test_no_try_at_all_site_survives_outcome_recording_failure(self, monkeypatch):
        """engram_observation.py:2053 (server.engram_add_observation_batch)
        had NO surrounding try at all before this workstream
        (docs/internal/2574-telemetry-design.md §0.1) -- a brand-new try wraps it."""
        server = fresh_server()
        eo = _live("engram_observation")
        raiser, counter = _make_always_raises()
        monkeypatch.setattr(eo, "write_telemetry_record", raiser)

        test_file = os.path.join(TEST_DIR, "ws5_batch_source.md")
        with open(test_file, "w") as f:
            f.write("A quoted snippet for the WS5 protection test.\n")

        payload = {
            "observations_json": json.dumps([{
                "quoted_text": "A quoted snippet for the WS5 protection test.",
                "interpretation": "test interpretation",
                "claim": "A test claim for the WS5 protection test.",
                "quote_type": "attributed_analysis",
            }]),
            "url": f"file://{test_file}",
            "title": "WS5 protection test source",
        }
        r = json.loads(server.engram_add_observation_batch(payload_json=json.dumps(payload)))
        assert "error" not in r, (
            f"MCP tool call must return normally even when the new "
            f"outcome-recording call raises (no-try-at-all site); got {r}"
        )
        assert r.get("status") == "batch_complete", r
        assert counter["n"] >= 1, (
            "the raising write_telemetry_record was never invoked -- this "
            "test would pass vacuously without exercising the protection at all"
        )


# ---------------------------------------------------------------------------
# 3. Fan-out reached test: one fan-out SITE (engram_revision.py:2543, inside
#    _supersede_impl) resolves to TWO dispatch targets
#    (server.py:engram_supersede, server.py:engram_cascade_resolve) per the
#    gate's own derived dispatch-target sets. `reached` is placed at each
#    TARGET's own entry -- unconditional, before argument validation -- so
#    it does not depend on the site's own code running at all; calling each
#    target once independently is what naturally produces "one reached row
#    per target" (the union-over-targets semantics falls out of placement,
#    not special-case logic -- docs/internal/2574-telemetry-design.md §1/§10).
# ---------------------------------------------------------------------------


class TestFanOutReached:
    def test_two_distinct_reached_rows_one_per_target(self, monkeypatch):
        server = fresh_server()
        calls: list = []
        monkeypatch.setattr(server, "write_telemetry_record", _spy(calls))

        # Malformed payload_json: `reached` fires before _parse_payload
        # runs, so a parse failure still exercises it -- "directly exercise"
        # per this workstream's own spec, rather than building full
        # supersede/cascade-resolve preconditions.
        json.loads(server.engram_supersede(payload_json="not json"))
        json.loads(server.engram_cascade_resolve(payload_json="not json"))

        reached = [c for c in calls if c["event_type"] == "engram.telemetry.reached"]
        manifest_ids = {c["manifest_id"] for c in reached}
        assert manifest_ids == {"server.py:engram_supersede", "server.py:engram_cascade_resolve"}, (
            f"expected exactly these two distinct target keys; got {manifest_ids}"
        )
        assert len(reached) == 2, f"expected exactly 2 reached rows; got {reached}"

    def test_mutation_control_reddens_when_one_targets_increment_removed(self, monkeypatch):
        """Can-it-fail control: with server.py:engram_cascade_resolve's own
        `reached` write suppressed (simulating that increment being
        deleted), only ONE distinct target key must remain -- proving the
        two-row assertion above is not vacuously satisfied by some other
        mechanism."""
        server = fresh_server()
        calls: list = []

        def _suppress_cascade_resolve(manifest_id, event_type, fields=None):
            if manifest_id == "server.py:engram_cascade_resolve":
                return True  # silently dropped, as if the increment were gone
            calls.append({"manifest_id": manifest_id, "event_type": event_type, "fields": dict(fields or {})})
            return True

        monkeypatch.setattr(server, "write_telemetry_record", _suppress_cascade_resolve)

        json.loads(server.engram_supersede(payload_json="not json"))
        json.loads(server.engram_cascade_resolve(payload_json="not json"))

        reached = [c for c in calls if c["event_type"] == "engram.telemetry.reached"]
        manifest_ids = {c["manifest_id"] for c in reached}
        assert manifest_ids == {"server.py:engram_supersede"}, (
            f"mutation control: expected only the un-suppressed target key; got {manifest_ids}"
        )


# ---------------------------------------------------------------------------
# 4. Fan-in reached test: engram_add_observation_batch is reached from TWO
#    sites per the gate's own derived dispatch-target sets
#    (engram_observation.py:1897, shared with engram_add_observation; and
#    :2053, its own dedicated site) -- but `reached` is placed at the
#    TARGET's entry, not per site, so it cannot and need not distinguish
#    which site conceptually contributed (that IS the union-over-targets
#    point). What's testable from the target side is that repeated
#    invocations accumulate correctly rather than deduping or interfering.
# ---------------------------------------------------------------------------


class TestFanInReached:
    def test_accumulates_across_invocations(self, monkeypatch):
        server = fresh_server()
        calls: list = []
        monkeypatch.setattr(server, "write_telemetry_record", _spy(calls))

        json.loads(server.engram_add_observation_batch(payload_json="not json"))
        json.loads(server.engram_add_observation_batch(payload_json="not json"))

        reached = [
            c for c in calls
            if c["event_type"] == "engram.telemetry.reached"
            and c["manifest_id"] == "server.py:engram_add_observation_batch"
        ]
        assert len(reached) == 2, f"expected 2 accumulated reached rows; got {reached}"


# ---------------------------------------------------------------------------
# 5. Gate round-trip (mirrors tests/test_ws3_telemetry_resolver.py's
#    TestGateRoundTrip pattern exactly, over this workstream's own 9 site
#    literals).
# ---------------------------------------------------------------------------


_WS5_SITE_LITERALS = {
    "server.engram_derive.blocked",
    "server.engram_derive.created",
    "server.engram_contradict",
    "server.engram_add_observation",
    "server.engram_add_observation_batch",
    "server.engram_query",
    "server.engram_query_pattern",
    "server.engram_supersede",
    "server.engram_retract",
}


class TestGateRoundTrip:
    def test_all_9_ws5_sites_clean_not_wired_and_undeclared(self):
        manifest = telemetry_manifest_gate.load_manifest()
        src_root = telemetry_manifest_gate.source_root(manifest, telemetry_manifest_gate.REPO_ROOT)
        report = telemetry_manifest_gate.run_gate(src_root, manifest)

        assert _WS5_SITE_LITERALS <= report.L, (
            f"expected all 9 WS5 site literals in L; missing {_WS5_SITE_LITERALS - report.L}"
        )
        assert _WS5_SITE_LITERALS <= report.M, (
            f"expected all 9 WS5 site literals in M; missing {_WS5_SITE_LITERALS - report.M}"
        )
        assert not (report.not_wired & _WS5_SITE_LITERALS), (
            f"NOT_WIRED must be empty for WS5's own sites; got {report.not_wired & _WS5_SITE_LITERALS}"
        )
        assert not (report.undeclared & _WS5_SITE_LITERALS), (
            f"UNDECLARED must be empty for WS5's own sites; got {report.undeclared & _WS5_SITE_LITERALS}"
        )

    def test_gate_check_ok_on_real_tree(self):
        manifest = telemetry_manifest_gate.load_manifest()
        src_root = telemetry_manifest_gate.source_root(manifest, telemetry_manifest_gate.REPO_ROOT)
        report = telemetry_manifest_gate.run_gate(src_root, manifest)
        assert telemetry_manifest_gate.check_ok(report), (
            f"expected check_ok()==True on the real tree; "
            f"not_wired={report.not_wired} undeclared={report.undeclared} "
            f"unresolved_dynamic_hits={report.unresolved_dynamic_hits} "
            f"zero_emit_violations={report.zero_emit_violations}"
        )
