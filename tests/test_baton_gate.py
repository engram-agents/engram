"""Tests for tools/baton_gate.py (#2884, second half).

baton_gate.py's _parse_github_anchor / _dedupe_checks_by_latest /
_pr_ci_state / _pr_approval_state are exercised indirectly (and thoroughly)
by tests/test_baton.py, since tools/baton.py now imports those four names
directly from baton_gate.py -- patching baton.py's own module attribute (or
the global subprocess.run, which both baton.py and baton_gate.py resolve
identically at call time) exercises the shared implementation either way.
This file covers what tests/test_baton.py cannot:

  1. PoolSentinelUnresolved / _resolve_pool_sentinel's own I/O + caching,
     never exercised directly before this extraction.
  2. TestSentinelPredicateParity -- an automated equivalence check between
     tools.baton's own _pool_sentinel/_is_pool_sentinel/_is_maintainer_flip
     (left unchanged by this refactor) and baton_gate.py's independent
     copies of the same three predicates. See baton_gate.py's module
     docstring ("Scope note") for why these are two separate
     implementations rather than one shared import -- this class is the
     mitigation for that deliberate duplication: it fails loudly the
     moment the two copies diverge, rather than drifting unnoticed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).parent
_ROOT = _THIS_DIR.parent
_TOOLS_DIR = _ROOT / "tools"

# append (not insert(0)) -- mirrors src/forum/tests/test_baton_thin_client.py's
# own rationale: tools/ also holds forum.py, whose package name would shadow
# src/forum's `forum` package if inserted ahead of it for the rest of the
# pytest run.
if str(_TOOLS_DIR) not in sys.path:
    sys.path.append(str(_TOOLS_DIR))

import baton_gate  # noqa: E402
import baton as _baton  # noqa: E402  (tools/baton.py, via tools/ on sys.path)


@pytest.fixture(autouse=True)
def _reset_both_caches():
    """Both modules cache their resolved sentinel independently -- reset both
    before AND after every test so no test leaks a resolved value into the
    next (mirrors tests/test_baton.py's own per-test _POOL_SENTINEL_CACHE
    resets)."""
    _baton._POOL_SENTINEL_CACHE.clear()
    baton_gate._POOL_SENTINEL_CACHE.clear()
    yield
    _baton._POOL_SENTINEL_CACHE.clear()
    baton_gate._POOL_SENTINEL_CACHE.clear()


# ---------------------------------------------------------------------------
# _parse_github_anchor -- smoke coverage (thorough coverage already lives in
# tests/test_baton.py via the shared import; this just confirms the module
# is independently importable and correct in isolation).
# ---------------------------------------------------------------------------

class TestParseGithubAnchor:
    def test_bare_pr_number(self):
        assert baton_gate._parse_github_anchor("pr/123") == ("pr", None, "123")

    def test_repo_qualified_pr(self):
        assert baton_gate._parse_github_anchor("pr/example-org/example-repo/123") == (
            "pr", "example-org/example-repo", "123",
        )

    def test_project_anchor(self):
        assert baton_gate._parse_github_anchor("project/4") == ("project", None, "4")

    def test_empty_value(self):
        assert baton_gate._parse_github_anchor("") is None

    def test_garbage_value(self):
        assert baton_gate._parse_github_anchor("not-an-anchor") is None


# ---------------------------------------------------------------------------
# _derive_legacy_author -- the #2961-follow-up fallback for the 1,493
# pre-existing batons with no `author:` field (2026-09-16, Ariadne's
# round-2 catch on PR#2966).
# ---------------------------------------------------------------------------

class TestDeriveLegacyAuthor:
    def test_recovers_author_from_real_init_line_shape(self):
        body = (
            "\n# PR-9000 — some title\n\n## Turn log\n\n"
            "- 2026-09-16T12:00:00Z initialized → clio: project initialized by clio\n"
            "- 2026-09-16T13:00:00Z clio → ariadne: please review\n"
        )
        assert baton_gate._derive_legacy_author(body) == "clio"

    def test_takes_the_first_initialized_line_not_a_later_one(self):
        """A body should only ever carry one 'initialized ->' line (it's
        written once, at init) -- but if a hand-edited/corrupted file has
        more than one, the FIRST is the conservative choice (closest to
        the actual `init()` call), not the last."""
        body = (
            "- 2026-09-16T12:00:00Z initialized → clio: project initialized by clio\n"
            "- 2026-09-16T14:00:00Z initialized → borges: a later, spurious line\n"
        )
        assert baton_gate._derive_legacy_author(body) == "clio"

    def test_no_initialized_line_returns_empty_string(self):
        """Not an error -- identical to a missing `author:` field with no
        fallback available; the caller's `or` chain falls through to "",
        and the exemption simply cannot fire."""
        body = "- 2026-09-16T13:00:00Z clio → ariadne: please review\n"
        assert baton_gate._derive_legacy_author(body) == ""

    def test_empty_body_returns_empty_string(self):
        assert baton_gate._derive_legacy_author("") == ""
        assert baton_gate._derive_legacy_author(None) == ""

    def test_does_not_match_a_non_initialized_arrow_line(self):
        """Only the literal 'initialized -> X:' shape counts -- an
        ordinary turn-log arrow line (any later flip/claim/release) must
        NOT be mistaken for the author line, even though it shares the
        arrow syntax."""
        body = "- 2026-09-16T13:00:00Z clio → ariadne: please review\n"
        # Sanity: the arrow line above genuinely exists and is not empty --
        # this test would be vacuous if the fixture body were malformed.
        assert "→" in body
        assert baton_gate._derive_legacy_author(body) == ""


# ---------------------------------------------------------------------------
# resolve_pool_sentinel -- I/O + cache + raise, never directly tested before
# this extraction (tools/baton.py's own _pool_sentinel did the sys.exit
# instead, exercised via SystemExit in tests/test_baton.py).
# ---------------------------------------------------------------------------

class TestResolvePoolSentinel:
    def test_reads_primary_user_from_config(self, tmp_path):
        home = tmp_path / "engram"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"primary_user": "Lei Shi"}))
        assert baton_gate._resolve_pool_sentinel(str(home)) == "lei shi"

    def test_caches_after_first_resolution(self, tmp_path, monkeypatch):
        home = tmp_path / "engram"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"primary_user": "casey"}))
        first = baton_gate._resolve_pool_sentinel(str(home))
        assert first == "casey"
        # Mutate the file -- cached value must NOT change on a second call.
        (home / "config.json").write_text(json.dumps({"primary_user": "someone-else"}))
        second = baton_gate._resolve_pool_sentinel(str(home))
        assert second == "casey"

    def test_falls_back_to_env_var_when_config_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BATON_POOL_SENTINEL", "env-sentinel")
        missing_home = tmp_path / "does-not-exist"
        assert baton_gate._resolve_pool_sentinel(str(missing_home)) == "env-sentinel"

    def test_raises_when_unresolvable(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BATON_POOL_SENTINEL", raising=False)
        missing_home = tmp_path / "does-not-exist"
        with pytest.raises(baton_gate.PoolSentinelUnresolved):
            baton_gate._resolve_pool_sentinel(str(missing_home))

    def test_does_not_call_sys_exit(self, tmp_path, monkeypatch):
        """The whole point of the extraction: unlike tools/baton.py's own
        _pool_sentinel(), this must raise -- never exit the process (which
        would kill a long-running Flask server, not just refuse one
        request)."""
        monkeypatch.delenv("BATON_POOL_SENTINEL", raising=False)
        missing_home = tmp_path / "does-not-exist"
        try:
            baton_gate._resolve_pool_sentinel(str(missing_home))
        except SystemExit:
            pytest.fail("_resolve_pool_sentinel must raise, never sys.exit")
        except baton_gate.PoolSentinelUnresolved:
            pass


# ---------------------------------------------------------------------------
# TestSentinelPredicateParity -- the anti-drift check for the deliberate
# duplication (see module docstring above and baton_gate.py's own).
# ---------------------------------------------------------------------------

class TestSentinelPredicateParity:
    """For every (to_agent, current_holder, colleague_name, sentinel) input,
    tools.baton._is_maintainer_flip and baton_gate._is_maintainer_flip must
    return the identical boolean -- pinning the two independent
    implementations to each other so a future edit to one that silently
    diverges from the other fails this test rather than drifting unnoticed.
    """

    SENTINEL_VALUES = ("casey", "lei shi", "lin")
    NAMES = ("ariadne", "borges", "sol", "casey", "lei", "unknown-agent")

    def _pin_both(self, monkeypatch, sentinel_value):
        monkeypatch.setattr(_baton, "_pool_sentinel", lambda: sentinel_value)
        monkeypatch.setattr(
            baton_gate, "_resolve_pool_sentinel", lambda *a, **kw: sentinel_value
        )

    def test_is_pool_sentinel_parity(self, monkeypatch):
        for sentinel_value in self.SENTINEL_VALUES:
            self._pin_both(monkeypatch, sentinel_value)
            for name in self.NAMES:
                assert _baton._is_pool_sentinel(name) == baton_gate._is_pool_sentinel(name), (
                    f"parity break: sentinel={sentinel_value!r} name={name!r}"
                )

    def test_is_maintainer_flip_parity(self, monkeypatch):
        for sentinel_value in self.SENTINEL_VALUES:
            self._pin_both(monkeypatch, sentinel_value)
            for to_agent in self.NAMES:
                for current_holder in self.NAMES:
                    for colleague_name in (*self.NAMES, ""):
                        for author in (*self.NAMES, ""):
                            old = _baton._is_maintainer_flip(
                                to_agent, current_holder, colleague_name, author
                            )
                            new = baton_gate._is_maintainer_flip(
                                to_agent, current_holder, colleague_name, author
                            )
                            assert old == new, (
                                f"parity break: sentinel={sentinel_value!r} "
                                f"to_agent={to_agent!r} current_holder={current_holder!r} "
                                f"colleague_name={colleague_name!r} author={author!r} -- "
                                f"tools.baton={old!r} baton_gate={new!r}"
                            )

    def test_unresolved_sentinel_raises_in_baton_gate(self, monkeypatch, tmp_path):
        """baton_gate's copy must raise PoolSentinelUnresolved (not exit,
        not silently return False) when the sentinel can't be determined --
        the caller (api_project_flip) fails closed on this."""
        monkeypatch.delenv("BATON_POOL_SENTINEL", raising=False)
        monkeypatch.setattr(
            baton_gate,
            "_resolve_pool_sentinel",
            lambda *a, **kw: (_ for _ in ()).throw(
                baton_gate.PoolSentinelUnresolved("cannot determine pool sentinel")
            ),
        )
        with pytest.raises(baton_gate.PoolSentinelUnresolved):
            baton_gate._is_pool_sentinel("anyone")
        with pytest.raises(baton_gate.PoolSentinelUnresolved):
            baton_gate._is_maintainer_flip("anyone", "someone", "colleague")
