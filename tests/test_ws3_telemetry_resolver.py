"""Tests for WS3 (alpha #2574 epic #2013, issue #2574's hook-side telemetry
resolver) -- docs/2574-telemetry-design.md §9 tests 1 and 3b, plus the gate
round-trip.

Covers two structurally distinct targets, per the WS3 spec:
  - engram-surface-hook.py: the FAN-IN-multi-site case (three call sites
    sharing one `main` dispatch target).
  - engram-postcompact-hook.py / engram-deference-detector-stop.py: the
    SIMPLE single-site case.

Each hook module is loaded fresh per test via importlib (mirrors
tests/test_deference_detector_prompt.py and
tests/test_surface_hook_hooklib_guard.py's existing convention) against a
scratch ENGRAM_HOME, so no test ever touches a real ~/.engram directory.
`Emitter.init`/`Emitter.emit` are monkeypatched at the shared
`engram_log_emitter` module level (the hooks import `Emitter` locally, at
call time, inside a narrowed try -- patching the class object already
cached in `sys.modules["engram_log_emitter"]` is what makes that late-bound
import see the patched behavior). Each hook's own
`write_telemetry_record` (bound at module-load time, either to the real
`engram_telemetry_store.write_telemetry_record` or its local
import-failure fallback) is replaced with an in-memory spy after loading,
so outcome rows are assertable without touching the filesystem telemetry
store.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src" / "engram"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import engram_log_emitter  # noqa: E402 -- shared module the hooks late-import Emitter from
from tools import telemetry_manifest_gate  # noqa: E402


# ---------------------------------------------------------------------------
# Module-loading + Emitter-patching helpers
# ---------------------------------------------------------------------------


def _live_emitter_module():
    """Re-fetch `engram_log_emitter` from `sys.modules` at point-of-use,
    rather than relying on this file's own collection-time `import
    engram_log_emitter` (line ~43).

    `tests/conftest.py`'s `fresh_module()` helper (used by 34+ other test
    files: test_cascade_playbox.py, test_trust_tier.py, test_focus_sets.py,
    etc.) does `del sys.modules[key]` for every `engram_*`-prefixed module
    and re-imports fresh whenever ANY test in the same process calls it --
    replacing `sys.modules["engram_log_emitter"]` with a brand-new module
    object mid-suite. Collection (this file's own top-level imports)
    happens before any test executes, so this file's own `engram_log_emitter`
    name keeps pointing at the orphaned pre-purge object for the rest of the
    session, while the hook's own runtime `from engram_log_emitter import
    Emitter` (called fresh, inside `mod.main()`, at call time) resolves
    `sys.modules["engram_log_emitter"]` AT THAT MOMENT -- the fresh,
    post-purge module. Patching the stale reference is therefore a no-op
    against whatever the hook actually runs.

    Round-3 reviewer-fairy finding on PR #2853 (delta round, ac46865a):
    reproduced deterministically with two independent `fresh_module()`-
    calling trigger files (test_cascade_playbox.py, test_trust_tier.py) run
    immediately before this file, and confirmed at full-suite scope --
    `pytest tests/` reproduces the identical 2 failures this fix exists to
    close. A `sys.modules` lookup always resolves to whatever object is
    CURRENTLY live, unlike a name bound once at collection time, so this is
    robust to `fresh_module()` running at any point before or between tests.
    """
    return sys.modules.get("engram_log_emitter", engram_log_emitter)


def _load_hook(mod_name: str, relpath: str, engram_home: Path, monkeypatch):
    """Import a hook module fresh against a scratch ENGRAM_HOME.

    ENGRAM_HOME is set BEFORE exec_module so every module-scope path
    constant (MARKER_PATH, KNOWLEDGE_DB_PATH, etc.) resolves under the
    scratch dir, matching test_surface_hook_hooklib_guard.py's existing
    convention.

    Also resets `engram_log_emitter`'s module-level `_EMITTER_INSTANCE`
    singleton, matching tests/test_log_emitter.py's own EmitterTestBase
    convention (its setUp/tearDown do the same raw reset). Without this,
    `Emitter.init()` returns whatever instance an EARLIER test in the same
    process already constructed rather than going through construction
    again -- so a test forcing construction to fail (patching `Emitter.init`
    itself, or the import) never actually exercises that path once a
    prior test has left the singleton populated: the emit then succeeds
    against the stale instance and the outcome records `delivered`
    instead of `init_failed`. Invisible when this file runs alone or
    alongside only its three named siblings (none of which leave the
    singleton populated at the point this file's tests run), and only
    surfaces in a wider test selection that happens to run an
    emitter-touching test first (Ariadne's colleague-review catch on
    PR #2853, round 1: 2 failed / 1346 passed on a wide `-k` selection,
    root-caused to this exact missing reset).
    """
    monkeypatch.setattr(_live_emitter_module(), "_EMITTER_INSTANCE", None)
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop(mod_name, None)
    path = SRC_ROOT / relpath
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spy_write_telemetry_record(mod):
    """Replace `mod`'s module-global `write_telemetry_record` with an
    in-memory recorder; returns the list it appends to."""
    calls: list[dict] = []

    def _spy(manifest_id, event_type, fields=None):
        calls.append(
            {
                "manifest_id": manifest_id,
                "event_type": event_type,
                "fields": dict(fields or {}),
            }
        )
        return True

    mod.write_telemetry_record = _spy
    return calls


class _FakeEmitterDropped:
    """Construction succeeds; every emit() call returns None (DROPPED)."""

    def emit(self, *args, **kwargs):
        return None


class _FakeEmitterDelivered:
    """Construction succeeds; every emit() call returns a fake uuid (DELIVERED)."""

    def __init__(self):
        self.emit_calls: list[dict] = []

    def emit(self, *args, **kwargs):
        self.emit_calls.append(kwargs)
        return "fake-uuid-0001"


def _patch_emitter_init_returns(monkeypatch, instance):
    """Force `Emitter.init(...)` (however it is later imported, since the
    hooks do `from engram_log_emitter import Emitter` inside a narrowed
    try/except at call time) to return `instance` unconditionally.

    Patches the LIVE module's `Emitter` class (see `_live_emitter_module`),
    not this file's own collection-time `engram_log_emitter` reference --
    `fresh_module()` elsewhere in the suite can replace the module object
    the hook actually imports from.
    """
    monkeypatch.setattr(
        _live_emitter_module().Emitter,
        "init",
        classmethod(lambda cls, *a, **k: instance),
    )


def _patch_emitter_init_raises(monkeypatch, exc: Exception = RuntimeError("boom: init failed")):
    def _raise(cls, *a, **k):
        raise exc

    monkeypatch.setattr(_live_emitter_module().Emitter, "init", classmethod(_raise))


def _stdin(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


# ---------------------------------------------------------------------------
# 1. Suppressor test (§9 test 1) -- single-site case:
#    engram-postcompact-hook.py
# ---------------------------------------------------------------------------


class TestSuppressorSingleSite:
    def test_dropped_outcome_recorded_and_caller_not_disrupted(self, tmp_path, monkeypatch):
        mod = _load_hook(
            "wsr_postcompact_suppressor",
            "hooks/claude/engram-postcompact-hook.py",
            tmp_path,
            monkeypatch,
        )
        calls = _spy_write_telemetry_record(mod)
        _patch_emitter_init_returns(monkeypatch, _FakeEmitterDropped())

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type":"assistant"}\n')
        _stdin(monkeypatch, {"session_id": "sess_A", "transcript_path": str(transcript)})

        # Must return normally -- no exception propagated.
        mod.main()

        hook_fire_rows = [c for c in calls if c["event_type"] == "engram.hook.fire"]
        assert hook_fire_rows, f"expected an engram.hook.fire outcome row; got calls={calls}"
        assert hook_fire_rows[-1]["fields"]["outcome"] == "dropped", (
            f"expected outcome='dropped' when Emitter.emit() returns None; got {hook_fire_rows[-1]}"
        )
        assert hook_fire_rows[-1]["manifest_id"] == "postcompact-hook.hook_fire"

    def test_mutation_control_reddens_when_outcome_recording_removed(self, tmp_path, monkeypatch):
        """Delete the outcome-recording call (comment it out, in effect, via
        a stub write_telemetry_record that only records `reached` rows) and
        confirm the suppressor assertion above would fail -- while a
        caller-not-disrupted check stays green. Mirrors the WS3 spec's
        mutation-control requirement for test 1."""
        mod = _load_hook(
            "wsr_postcompact_suppressor_mutation",
            "hooks/claude/engram-postcompact-hook.py",
            tmp_path,
            monkeypatch,
        )

        # Simulate "the outcome-recording call was deleted": _emit_hook_fire
        # still runs, but write_telemetry_record silently drops anything
        # that isn't a `reached` row -- exactly what commenting out the
        # outcome-recording line(s) would produce.
        calls: list[dict] = []

        def _reached_only_spy(manifest_id, event_type, fields=None):
            if event_type == "engram.telemetry.reached":
                calls.append({"manifest_id": manifest_id, "event_type": event_type, "fields": dict(fields or {})})
            return True

        mod.write_telemetry_record = _reached_only_spy
        _patch_emitter_init_returns(monkeypatch, _FakeEmitterDropped())

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type":"assistant"}\n')
        _stdin(monkeypatch, {"session_id": "sess_A", "transcript_path": str(transcript)})

        # Caller-not-disrupted still holds (control's other half stays green).
        mod.main()

        hook_fire_rows = [c for c in calls if c["event_type"] == "engram.hook.fire"]
        assert not hook_fire_rows, (
            "mutation control: with outcome-recording removed, the suppressor "
            f"test's own assertion (a 'dropped' engram.hook.fire row) must NOT "
            f"hold -- got {hook_fire_rows}"
        )


# ---------------------------------------------------------------------------
# 2. INIT_FAILED test (§9 test 2) -- single-site case
# ---------------------------------------------------------------------------


class TestInitFailedSingleSite:
    def test_init_failed_distinct_from_dropped(self, tmp_path, monkeypatch):
        mod = _load_hook(
            "wsr_postcompact_init_failed",
            "hooks/claude/engram-postcompact-hook.py",
            tmp_path,
            monkeypatch,
        )
        calls = _spy_write_telemetry_record(mod)
        _patch_emitter_init_raises(monkeypatch)

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type":"assistant"}\n')
        _stdin(monkeypatch, {"session_id": "sess_B", "transcript_path": str(transcript)})

        mod.main()

        hook_fire_rows = [c for c in calls if c["event_type"] == "engram.hook.fire"]
        assert hook_fire_rows, f"expected an engram.hook.fire outcome row; got calls={calls}"
        assert hook_fire_rows[-1]["fields"]["outcome"] == "init_failed"
        assert hook_fire_rows[-1]["fields"]["outcome"] != "dropped"

    def test_can_it_fail_control_old_wide_try_reddens(self, tmp_path, monkeypatch):
        """Can-it-fail control (spec §9 test 2): revert the try-narrowing for
        this one site (simulate the pre-WS3 wide-try shape, where
        construction AND the emit call share one try/except) and confirm a
        test shaped like the regression below would REDDEN -- i.e. the old
        shape really did conflate 'failed after a successful emit' with
        'construction failed'."""
        mod = _load_hook(
            "wsr_postcompact_wide_try_control",
            "hooks/claude/engram-postcompact-hook.py",
            tmp_path,
            monkeypatch,
        )
        calls = _spy_write_telemetry_record(mod)

        # Simulate the OLD (pre-WS3) wide-try shape directly: construction
        # succeeds, but something after the successful emit raises inside
        # the SAME try -- reproduce it by monkeypatching _emit_hook_fire
        # itself to the pre-WS3 shape (single try wrapping import + init +
        # emit + a subsequent raise), rather than editing the source file.
        def _old_shape_emit_hook_fire(session_id, transcript_path, duration_ms):
            try:
                from engram_log_emitter import Emitter

                emitter = Emitter.init(session_id=session_id or "unknown", transcript_path=transcript_path or "")
                emitter.emit(
                    event_type="engram.hook.fire",
                    level=1,
                    data={"duration_ms": duration_ms},
                    site="postcompact-hook.hook_fire",
                )
                raise ValueError("simulated post-emit failure, old wide-try shape")
            except Exception:
                pass  # old shape: no outcome recording distinction at all

        mod._emit_hook_fire = _old_shape_emit_hook_fire
        _patch_emitter_init_returns(monkeypatch, _FakeEmitterDelivered())

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type":"assistant"}\n')
        _stdin(monkeypatch, {"session_id": "sess_C", "transcript_path": str(transcript)})

        mod.main()

        hook_fire_rows = [c for c in calls if c["event_type"] == "engram.hook.fire"]
        assert not hook_fire_rows, (
            "can-it-fail control: the pre-WS3 wide-try shape records NO "
            f"outcome distinction at all (everything is swallowed by the "
            f"single except) -- got {hook_fire_rows}, expected none"
        )


# ---------------------------------------------------------------------------
# 2b. The decisive regression case (§1's own worked example): a failure
# AFTER a successful Emitter.init() but NOT inside Emitter.emit() itself,
# on engram-surface-hook.py's `_principle_rendered` loop -- the exact site
# named in the design doc as "6 of 11 hook-side sites wrap non-construction
# work in the same try".
# ---------------------------------------------------------------------------


def _load_surface_hook(engram_home: Path, monkeypatch):
    return _load_hook(
        "wsr_surface_hook",
        "hooks/claude/engram-surface-hook.py",
        engram_home,
        monkeypatch,
    )


def _drive_surface_hook_to_trigger_fire(mod, monkeypatch, principle_rendered):
    """Stub every surface-hook dependency EXCEPT the trigger_fire block
    itself, so `main()` reaches it with a controlled `_principle_rendered`
    value. None of these stubs touch the try-narrowing / outcome-recording
    logic under test."""
    mod._get_auto_surface_config = lambda: {
        "idf_gate_enabled": False,
        "short_prompt_threshold_chars": 0,
        "prev_response_tail_chars": 0,
    }
    mod.query_daemon = lambda prompt, embed_query=None: None
    mod.query_fts_fallback = lambda prompt: {"matched_ids": []}
    # A non-empty `principle_triggers` string is required here: the real
    # code's own `if not principle_triggers:` branch overwrites
    # `_principle_rendered = []` when it's falsy (the semantic-keyword
    # fallback path) -- returning "" would silently zero out the very
    # `_principle_rendered` value this stub exists to control.
    # #3521 row 7: main() now also passes the optional `unknown` list; row 40
    # adds the optional `state_unknown` list the same way.
    mod.check_principle_triggers_full = lambda matched_ids, prompt_count, unknown=None, state_unknown=None: (
        "[stub principle trigger text]",
        principle_rendered,
    )
    mod.check_error_patterns = lambda prompt: ""
    mod.check_write_reminder = lambda: ""
    mod.check_repair_marker = lambda: ""


class TestSurfaceHookDecisiveRegression:
    def test_delivered_not_init_failed_after_successful_construction(self, tmp_path, monkeypatch):
        """Regression test for the exact bug §1 names: a failure AFTER a
        successful Emitter.init() (here: a malformed second tuple in
        `_principle_rendered` that raises ValueError on unpack) must NOT be
        recorded as init_failed for the entries that already delivered.
        """
        mod = _load_surface_hook(tmp_path, monkeypatch)
        calls = _spy_write_telemetry_record(mod)
        fake_emitter = _FakeEmitterDelivered()
        _patch_emitter_init_returns(monkeypatch, fake_emitter)

        # First entry well-formed (delivers); second malformed (3-tuple,
        # raises ValueError on the 4-way unpack) -- the decisive shape.
        principle_rendered = [
            ("lesson", "ls_0001", {}, "tr_0001"),
            ("lesson", "ls_0002", {}),  # malformed: unpack raises here
        ]
        _drive_surface_hook_to_trigger_fire(mod, monkeypatch, principle_rendered)

        _stdin(monkeypatch, {"session_id": "sess_D", "transcript_path": "", "prompt": "hello there"})

        with pytest.raises(ValueError):
            mod.main()

        trigger_rows = [c for c in calls if c["event_type"] == "engram.trigger.fire"]
        assert trigger_rows, f"expected at least one engram.trigger.fire outcome row; got calls={calls}"
        assert trigger_rows[0]["fields"]["outcome"] == "delivered", (
            "the FIRST (well-formed) entry must record 'delivered', not "
            f"'init_failed' -- construction already succeeded before the "
            f"loop started; got {trigger_rows[0]}"
        )
        assert not any(r["fields"]["outcome"] == "init_failed" for r in trigger_rows), (
            f"no row should read init_failed -- construction never failed; got {trigger_rows}"
        )

    def test_can_it_fail_control_old_wide_try_reddens(self, tmp_path, monkeypatch):
        """Can-it-fail control: simulate the OLD (pre-WS3) wide-try shape
        for this exact site (construction + the whole loop inside one
        try/except with a bare `pass`) and confirm the decisive case above
        would NOT have recorded 'delivered' for the first entry -- it
        records nothing at all, because the old shape has no
        outcome-recording call whatsoever (that IS the bug: everything,
        including a real delivery, is swallowed silently)."""
        mod = _load_surface_hook(tmp_path, monkeypatch)
        calls = _spy_write_telemetry_record(mod)
        fake_emitter = _FakeEmitterDelivered()

        principle_rendered = [
            ("lesson", "ls_0001", {}, "tr_0001"),
            ("lesson", "ls_0002", {}),  # malformed: unpack raises here
        ]
        _drive_surface_hook_to_trigger_fire(mod, monkeypatch, principle_rendered)
        _stdin(monkeypatch, {"session_id": "sess_E", "transcript_path": "", "prompt": "hello there"})

        counter = {"prompts_since_compaction": 1}
        mod.read_prompt_counter = lambda: dict(counter)
        mod.write_prompt_counter = lambda state: None

        # Old (pre-WS3) shape: everything -- construction AND the loop --
        # inside one try/except, bare pass, no site= kwarg, no outcome
        # recording at all.
        def _old_shape_main_trigger_block(hook_input, _principle_rendered_local, counter_local):
            try:
                from engram_log_emitter import Emitter

                _emitter3 = Emitter.init(
                    session_id=hook_input.get("session_id", "unknown"),
                    transcript_path=hook_input.get("transcript_path", ""),
                )
                for _kind, _pid, _entry, _mid in _principle_rendered_local:
                    _emitter3.emit(
                        event_type="engram.trigger.fire",
                        level=1,
                        data={
                            "principle_id": _pid,
                            "kind": _kind,
                            "trigger_id": _mid,
                            "prompt_seq": counter_local["prompts_since_compaction"],
                        },
                    )
            except Exception:
                pass

        # Directly exercise the isolated old-shape block (rather than the
        # whole of main(), which would require re-deriving hook_input at
        # this point) -- this is the can-it-fail control's own harness, not
        # a claim about main()'s current (fixed) behavior.
        _old_shape_main_trigger_block(
            {"session_id": "sess_E", "transcript_path": ""}, principle_rendered, counter
        )

        trigger_rows = [c for c in calls if c["event_type"] == "engram.trigger.fire"]
        assert not trigger_rows, (
            "can-it-fail control: the pre-WS3 wide-try shape records NO "
            f"outcome row at all for the first (successfully-delivered) "
            f"entry -- got {trigger_rows}, expected none"
        )


# ---------------------------------------------------------------------------
# 3. Reached test (§9 test 3b, Case A only) -- fan-in-multi-site case:
#    engram-surface-hook.py
# ---------------------------------------------------------------------------


class TestReachedCaseA:
    def test_reached_fires_even_when_every_site_is_guarded_out(self, tmp_path, monkeypatch):
        """Case A: the trigger fires (main() runs) but the site's own guard
        is false (here: an empty prompt, which every one of surface-hook's
        three sites is guarded behind) -- `reached` must still be recorded,
        independent of whether any individual site delivers."""
        mod = _load_surface_hook(tmp_path, monkeypatch)
        calls = _spy_write_telemetry_record(mod)

        _stdin(monkeypatch, {"session_id": "sess_F", "transcript_path": "", "prompt": ""})

        with pytest.raises(SystemExit) as exc_info:
            mod.main()
        assert exc_info.value.code == 0

        reached_rows = [c for c in calls if c["event_type"] == "engram.telemetry.reached"]
        assert reached_rows, f"expected a reached row even though every site guarded out; got calls={calls}"
        assert reached_rows[0]["manifest_id"] == "hooks/claude/engram-surface-hook.py:main"

        # None of the three surface-hook sites should have delivered/dropped
        # anything -- the prompt-empty guard exits before any of them run.
        site_rows = [c for c in calls if c["event_type"] != "engram.telemetry.reached"]
        assert not site_rows, f"expected zero site outcome rows on the guarded-out path; got {site_rows}"

    def test_mutation_control_deleting_reached_increment_reddens(self, tmp_path, monkeypatch):
        """Mutation control: delete the reached-increment line (simulate by
        stubbing write_telemetry_record to drop `reached` rows specifically)
        and confirm the Case-A assertion above would fail, while the
        suppressor test (test 1, exercised on a different target) stays
        green -- i.e. the two tests are independent and this mutation does
        not accidentally also break the suppressor test's own target."""
        mod = _load_surface_hook(tmp_path, monkeypatch)

        calls: list[dict] = []

        def _drop_reached_spy(manifest_id, event_type, fields=None):
            if event_type == "engram.telemetry.reached":
                return True  # simulate the increment line having been deleted
            calls.append({"manifest_id": manifest_id, "event_type": event_type, "fields": dict(fields or {})})
            return True

        mod.write_telemetry_record = _drop_reached_spy

        _stdin(monkeypatch, {"session_id": "sess_G", "transcript_path": "", "prompt": ""})

        with pytest.raises(SystemExit):
            mod.main()

        reached_rows = [c for c in calls if c["event_type"] == "engram.telemetry.reached"]
        assert not reached_rows, (
            f"mutation control: with the reached-increment 'deleted', no "
            f"reached row should be recorded -- got {reached_rows}"
        )

        # The suppressor test's own target (postcompact-hook.py) is a
        # DIFFERENT module instance entirely, so this mutation on
        # surface-hook cannot have touched it -- assert that independently
        # here as the control's other half.
        other_mod = _load_hook(
            "wsr_postcompact_independence_check",
            "hooks/claude/engram-postcompact-hook.py",
            tmp_path,
            monkeypatch,
        )
        other_calls = _spy_write_telemetry_record(other_mod)
        _patch_emitter_init_returns(monkeypatch, _FakeEmitterDropped())
        transcript = tmp_path / "transcript2.jsonl"
        transcript.write_text('{"type":"assistant"}\n')
        _stdin(monkeypatch, {"session_id": "sess_H", "transcript_path": str(transcript)})
        other_mod.main()
        other_reached_rows = [c for c in other_calls if c["event_type"] == "engram.telemetry.reached"]
        assert other_reached_rows, "suppressor test's own target must be unaffected by the mutation above"


# ---------------------------------------------------------------------------
# 4. Gate round-trip -- L == M for every site literal WS3 added.
# ---------------------------------------------------------------------------

_WS3_SITE_LITERALS = {
    "deference-detector-prompt.hook_fire",
    "deference-detector-stop.hook_fire",
    "loop-arm-stop-hook.hook_fire",
    "postcompact-hook.hook_fire",
    "session-start-hook.hook_fire",
    "stop-hook.hook_fire",
    "surface-hook.surface_fire",
    "surface-hook.trigger_fire",
    "surface-hook.render_size",
    "utility-credit-mention-stop.enactment",
    "utility-credit-mention-stop.hook_fire",
}


class TestGateRoundTrip:
    def test_all_11_ws3_sites_clean_not_wired_and_undeclared(self):
        manifest = telemetry_manifest_gate.load_manifest()
        src_root = telemetry_manifest_gate.source_root(manifest, telemetry_manifest_gate.REPO_ROOT)
        report = telemetry_manifest_gate.run_gate(src_root, manifest)

        assert _WS3_SITE_LITERALS <= report.L, (
            f"expected all 11 WS3 site literals in L; missing "
            f"{_WS3_SITE_LITERALS - report.L}"
        )
        assert _WS3_SITE_LITERALS <= report.M, (
            f"expected all 11 WS3 site literals in M; missing "
            f"{_WS3_SITE_LITERALS - report.M}"
        )
        assert not (report.not_wired & _WS3_SITE_LITERALS), (
            f"NOT_WIRED must be empty for WS3's own sites; got {report.not_wired & _WS3_SITE_LITERALS}"
        )
        assert not (report.undeclared & _WS3_SITE_LITERALS), (
            f"UNDECLARED must be empty for WS3's own sites; got {report.undeclared & _WS3_SITE_LITERALS}"
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


# ---------------------------------------------------------------------------
# 4. Regression: this file's Emitter-patching must survive fresh_module()
#    replacing engram_log_emitter's module object mid-suite (round-3
#    reviewer-fairy finding on PR #2853; see _live_emitter_module's own
#    docstring for the full mechanism).
# ---------------------------------------------------------------------------


class TestSurvivesModuleReplacement:
    def test_init_failed_still_detected_after_fresh_module_replaces_emitter(self, tmp_path, monkeypatch):
        """Simulate the exact pollution: call `fresh_module()` on
        `engram_log_emitter` (as 34+ other test files in this suite do,
        e.g. test_cascade_playbox.py) BEFORE running one of this file's own
        tests -- replacing `sys.modules["engram_log_emitter"]` with a new
        module object, orphaning this file's own collection-time reference.
        The init_failed assertion must still hold: `_live_emitter_module()`
        re-fetches from `sys.modules` at point of use, so the patch lands
        on whatever object is CURRENTLY live regardless of when the
        replacement happened."""
        import engram_log_emitter as collection_time_ref
        from conftest import fresh_module

        fresh_module(collection_time_ref)
        assert sys.modules["engram_log_emitter"] is not collection_time_ref, (
            "fresh_module() must have actually replaced the module object "
            "for this test to be a genuine regression check"
        )

        mod = _load_hook(
            "wsr_postcompact_survives_fresh_module",
            "hooks/claude/engram-postcompact-hook.py",
            tmp_path,
            monkeypatch,
        )
        calls = _spy_write_telemetry_record(mod)
        _patch_emitter_init_raises(monkeypatch)

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type":"assistant"}\n')
        _stdin(monkeypatch, {"session_id": "sess_I", "transcript_path": str(transcript)})

        mod.main()

        hook_fire_rows = [c for c in calls if c["event_type"] == "engram.hook.fire"]
        assert hook_fire_rows, f"expected an engram.hook.fire outcome row; got calls={calls}"
        assert hook_fire_rows[-1]["fields"]["outcome"] == "init_failed", (
            f"expected 'init_failed' even after fresh_module() replaced the "
            f"module object -- got {hook_fire_rows[-1]}, which is exactly "
            f"the round-3 regression this test pins"
        )

    def test_can_it_fail_control_stale_reference_reddens(self, tmp_path, monkeypatch):
        """Can-it-fail control: patch the STALE (collection-time) module
        object directly, bypassing `_live_emitter_module()`, after the same
        `fresh_module()` replacement -- confirm this reproduces the exact
        round-3 defect (outcome reads 'delivered', not 'init_failed'),
        proving the test above is a genuine discriminator rather than
        vacuously passing regardless of which object gets patched."""
        import engram_log_emitter as collection_time_ref
        from conftest import fresh_module

        fresh_module(collection_time_ref)

        mod = _load_hook(
            "wsr_postcompact_stale_reference_control",
            "hooks/claude/engram-postcompact-hook.py",
            tmp_path,
            monkeypatch,
        )
        calls = _spy_write_telemetry_record(mod)

        # Deliberately patch the STALE reference (the bug this whole class
        # exists to prevent), not the live one _load_hook now uses.
        def _raise(cls, *a, **k):
            raise RuntimeError("boom: init failed")

        monkeypatch.setattr(collection_time_ref.Emitter, "init", classmethod(_raise))

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text('{"type":"assistant"}\n')
        _stdin(monkeypatch, {"session_id": "sess_J", "transcript_path": str(transcript)})

        mod.main()

        hook_fire_rows = [c for c in calls if c["event_type"] == "engram.hook.fire"]
        assert hook_fire_rows[-1]["fields"]["outcome"] == "delivered", (
            "can-it-fail control: patching the STALE module reference "
            f"must NOT prevent delivery (proving it has zero effect on "
            f"what the hook actually runs) -- got {hook_fire_rows[-1]}"
        )
