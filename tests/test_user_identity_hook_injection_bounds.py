"""Tests: engram-alpha#2863 WS2 -- engram-user-identity-hook.py's migration
onto `_hooklib.emit_context()`.

Companion to `tests/test_hook_injection_bounds.py` (WS1's own gate for the
helper + harness-adapter module themselves) and modeled directly on
`tests/test_surface_hook_injection_bounds.py` (the WS2 worked reference,
PR #2876) -- this file exercises the CALLER side: `main()`'s new head/body
split and `_assemble_response()`, wired end-to-end through a real `main()`
invocation.

Unlike engram-surface-hook.py, this hook has no stack of independently-
firing optional blocks to force on at once -- it fires exactly ONE identity
card per prompt. Its own "worst case" is a person node whose free-text
`role` metadata (no length cap enforced at write time -- see
`_assemble_response()`'s own docstring) is pathologically large, which is
the scenario these tests exercise instead.

Three scenarios, mirroring the WS2 spec's own list:
  1. Worst-case oversized Role/Profile metadata -- TestWorstCaseOversizedMetadata
  2. Common-case sanity (typical short metadata) -- TestCommonCaseSanity
  3. Nothing fires (no match, no sticky) -- TestNothingFires
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-user-identity-hook.py"
_HARNESS_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "_harness.py"

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_identity_hook(engram_home, monkeypatch):
    """Import the hook as a module against a scratch ENGRAM_HOME
    (hyphenated filename, not importable via normal import -- same pattern
    as test_surface_hook_injection_bounds.py's _load_surface_hook)."""
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_user_identity_hook_bounds_pt", None)
    spec = importlib.util.spec_from_file_location(
        "engram_user_identity_hook_bounds_pt", str(_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_harness_budgets(harness: str = "claude-code"):
    """Return get_harness_budgets(harness) for a fresh _harness.py load.

    Pinned to "claude-code" by default rather than calling detect_harness()
    for real, same reasoning as test_surface_hook_injection_bounds.py's own
    helper: this dev/source checkout has no baked platform.json.
    """
    spec = importlib.util.spec_from_file_location("_harness_bounds_pt_identity", _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.get_harness_budgets(harness)


def _make_person_db(db_path, person_id: str, name: str, role: str, description: str) -> None:
    """Minimal knowledge.db with one current person node -- same shape as
    tests/test_codex_hook_envelopes.py's own TestUserIdentityHookEnvelope
    fixture."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT,
            claim TEXT,
            logical_chain TEXT,
            metadata TEXT,
            is_current INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        "INSERT INTO nodes (id, type, claim, logical_chain, metadata, is_current) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (
            person_id,
            "person",
            f"{name} is a person",
            description,
            json.dumps({"name": name, "aliases": [], "role": role}),
        ),
    )
    conn.commit()
    conn.close()


def _run_main(mod, prompt: str) -> dict:
    """Drive main() end-to-end via stdin/stdout patching (same pattern as
    test_surface_hook_injection_bounds.py's _run_main)."""
    stdin_data = json.dumps({
        "prompt": prompt,
        "session_id": "bounds-test",
        "transcript_path": "",
    })
    buf = io.StringIO()
    with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin_data.encode()))):
        with patch("sys.stdout", buf):
            try:
                mod.main()
            except SystemExit:
                pass
    return json.loads(buf.getvalue().strip())


class TestWorstCaseOversizedMetadata:
    """A person node whose `role` metadata is pathologically large (no
    length cap enforced at write time -- see engram_add_person in
    server.py) must still render a bounded additionalContext block."""

    def test_oversized_role_never_exceeds_hard_budget_and_head_stays_under_head_budget(
        self, tmp_path, monkeypatch
    ):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        _make_person_db(
            engram_home / "knowledge.db",
            person_id="pn_0099",
            name="Alex",
            role="R" * 20_000,
            description="d" * 500,
        )
        mod = _load_identity_hook(engram_home, monkeypatch)
        budgets = _load_harness_budgets("claude-code")
        # Pin to claude-code: this dev checkout has no baked platform.json,
        # so main()'s own real detect_harness() call would otherwise return
        # None and silently use the UNMEASURED_HARNESS_FLOOR.
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        # Spy on emit_context so we can assert on the actual `head` string
        # main() constructed BEFORE it was passed in -- same load-bearing
        # property test_surface_hook_injection_bounds.py asserts: the final
        # rendered output's early portion could coincidentally look short
        # due to trailer placement even if head-construction has a latent
        # bug, so this must be asserted on `head` directly.
        real_emit_context = mod.emit_context
        captured = {}

        def _spy_emit_context(head, body, **kwargs):
            captured["head"] = head
            captured["body"] = list(body)
            return real_emit_context(head, body, **kwargs)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        output = _run_main(mod, prompt="Alex: hello there")

        assert output, "expected a non-empty response given a matched speaker prefix"
        ctx = output["hookSpecificOutput"]["additionalContext"]

        # The hard invariant: never exceed the harness's hard budget, no
        # matter how oversized the free-text Role metadata is.
        assert len(ctx) <= budgets["budget"], (
            f"additionalContext ({len(ctx)} chars) exceeded the hard budget "
            f"({budgets['budget']}) with a pathologically large Role field"
        )

        # Trailer leads.
        assert ctx.startswith("▸ "), f"expected the trailer to lead; got: {ctx[:80]!r}"
        assert " shown · " in ctx.split("\n", 1)[0]

        # The load-bearing property: `head` itself, BEFORE emit_context ever
        # saw it, must already be <= head_budget -- this hook's head is
        # just the identity-switch line built from a short name + person_id,
        # so it should already come in far under head_budget regardless of
        # how oversized the Role/Profile body items are.
        assert "head" in captured, "emit_context was never called -- nothing fired?"
        assert len(captured["head"]) <= budgets["head_budget"], (
            f"head ({len(captured['head'])} chars) exceeded head_budget "
            f"({budgets['head_budget']}) -- main()'s head-line construction "
            f"needs its own defensive truncation; this is NOT an "
            f"emit_context() bug."
        )


class TestCommonCaseSanity:
    """The typical prompt: a short name/role/profile -- output unchanged
    in shape from the pre-migration render, zero cut."""

    def test_typical_metadata_renders_full_zero_cut(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        _make_person_db(
            engram_home / "knowledge.db",
            person_id="pn_0042",
            name="Jordan",
            role="collaborator",
            description="A colleague working on the release.",
        )
        mod = _load_identity_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        output = _run_main(mod, prompt="Jordan: what's next")

        assert output, "expected a non-empty response -- the speaker prefix matched"
        ctx = output["hookSpecificOutput"]["additionalContext"]
        trailer = ctx.split("\n", 1)[0]
        assert "0 cut" in trailer, f"expected 0 cut for well-under-budget metadata; got: {trailer!r}"
        assert "Jordan" in ctx
        assert "collaborator" in ctx
        assert "A colleague working on the release." in ctx


class TestOversizedNameNeverDropsHeadWhole:
    """Regression test for engram-alpha#2887's colleague review (Ariadne):
    `head` is a single f-string line with no internal `\\n`, so
    `_hooklib._trim_to_line_boundary()` returns "" -- drops the head WHOLE,
    not truncated -- for any prefix that exceeds the harness's head budget
    and has no internal newline to cut at. Person names carry no length cap
    at write time (see engram_add_person in server.py); Ariadne measured
    the exact boundary on this hook's real budgets: a name of 1721 chars
    survives, 1722+ chars silently drops the entire identity line ("an
    identity card with the identity removed").

    This exercises the STICKY-session path (`main()`'s no-prefix branch),
    because `detect_speaker()`'s own prefix regex (`\\w[\\w\\s]{0,30}?`)
    caps the matched prefix at ~31 chars -- an oversized name can only ever
    reach `head` via the sticky-context JSON file (`current_user.json`),
    written once by an earlier matched turn and read back verbatim on every
    later prompt with no re-validation of its length.
    """

    def test_oversized_sticky_name_still_renders_identity_line_truncated(
        self, tmp_path, monkeypatch
    ):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        # A normal short-named person, just to clear main()'s `if not
        # people:` early-exit gate -- the oversized name below comes from
        # the sticky JSON file, not this DB row.
        _make_person_db(
            engram_home / "knowledge.db",
            person_id="pn_0001",
            name="Jordan",
            role="collaborator",
            description="A colleague working on the release.",
        )
        long_name = "A" * 2000  # comfortably past both the ~1722-char drop
        # boundary and the hook's own truncation length.
        sticky = {
            "person_id": "pn_0099",
            "name": long_name,
            "role": "collaborator",
            "description": "d" * 50,
            "matched_prefix": long_name,
        }
        (engram_home / "current_user.json").write_text(json.dumps(sticky))

        mod = _load_identity_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        output = _run_main(mod, prompt="hello there, no prefix here")

        assert output, "expected a non-empty response from sticky context"
        ctx = output["hookSpecificOutput"]["additionalContext"]

        # The regression this test guards: the identity line must be
        # PRESENT, not silently dropped whole.
        assert "ENGRAM User Identity" in ctx, (
            f"identity line was dropped entirely for an oversized sticky "
            f"name -- got: {ctx!r}"
        )
        assert "pn_0099" in ctx

        # The name must be visibly truncated (marker present, full 2000
        # chars absent) -- not silently cut with no indication.
        assert "…" in ctx, "expected an ellipsis marking the truncated name"
        assert long_name not in ctx, "the full 2000-char name must not appear untruncated"
        assert mod._MAX_HEAD_NAME_LEN < 1722, (
            "truncation length must stay comfortably under the measured "
            "~1722-char drop boundary"
        )


class TestNothingFires:
    """No speaker match and no sticky context -- the pre-migration `{}`
    early exit, preserved exactly."""

    def test_no_match_no_sticky_prints_empty_object(self, tmp_path, monkeypatch):
        engram_home = tmp_path / "engram"
        engram_home.mkdir()
        # No knowledge.db at all -- get_person_nodes() returns [] and main()
        # exits via the `if not people:` early return, well before
        # _assemble_response() is ever reached.
        mod = _load_identity_hook(engram_home, monkeypatch)
        monkeypatch.setattr(mod, "detect_harness", lambda: "claude-code")

        called = {"emit_context": False}
        real_emit_context = mod.emit_context

        def _spy_emit_context(*a, **kw):
            called["emit_context"] = True
            return real_emit_context(*a, **kw)

        monkeypatch.setattr(mod, "emit_context", _spy_emit_context)

        output = _run_main(mod, prompt="hello world")

        assert output == {}, f"expected an empty object when nothing fires; got {output!r}"
        assert not called["emit_context"], "emit_context() must not be called when no people exist"
