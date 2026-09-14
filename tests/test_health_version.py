"""Tests for health/version.py — the canonical health-v2 version-identity
module (engram-alpha#2537, Lei's amendment 2)."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# #2565: append (not insert(0)) -- putting src/engram at position 0
# outranks the repo-root tools/ package for the rest of the process,
# a live instance of the tools/-package-name-clash class found while
# building #2565's durable guard (tests/conftest.py).
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "engram"))

from health import version as health_version  # noqa: E402


class TestFormatVersion:
    def test_default_formats_current_major_minor(self):
        assert health_version.format_version() == "health-v2.0"

    def test_explicit_args_override_defaults(self):
        assert health_version.format_version(2, 5) == "health-v2.5"

    def test_zero_zero(self):
        assert health_version.format_version(0, 0) == "health-v0.0"


class TestParseVersion:
    def test_round_trips_with_format(self):
        s = health_version.format_version(3, 7)
        assert health_version.parse_version(s) == (3, 7)

    def test_current_round_trips(self):
        assert health_version.parse_version(health_version.CURRENT) == (
            health_version.MAJOR, health_version.MINOR,
        )

    @pytest.mark.parametrize("bad", [
        "0.5.0-dev.20260830231757.4ae2b947",  # ENGRAM plugin-build shape
        "0.4.0-pr5",                           # the retired ENGINE_VERSION shape
        "health-v1",                            # missing minor
        "health-v1.0.0",                        # too many dots
        "health-v1.x",                          # non-digit minor
        "v1.0",                                 # missing health- prefix
        "",
        "health-v",
    ])
    def test_malformed_or_foreign_strings_raise_not_silently_default(self, bad):
        with pytest.raises(ValueError):
            health_version.parse_version(bad)


class TestCurrent:
    def test_current_matches_module_constants(self):
        assert health_version.CURRENT == health_version.format_version(
            health_version.MAJOR, health_version.MINOR
        )

    def test_current_is_health_v2_0_today(self):
        """Pinned literally, not just structurally — health-v2.0 as of
        engram-alpha#2905 PR-B's MAJOR bump (unifying taint_rate +
        stale_derivation_rate into one L0 measurement,
        claim_bearing_pending_marker, backing the new pending_premise_rate
        signal): MAJOR 1->2, MINOR resets to 0. Regardless of ENGRAM's own
        version, per the module docstring."""
        assert health_version.CURRENT == "health-v2.0"

    def test_current_never_looks_like_an_engram_build_string(self):
        """The collision this module exists to prevent: no ENGRAM-shaped
        substring (three dots, a 'dev'/build-stamp suffix) anywhere."""
        assert health_version.CURRENT.count(".") == 1
        assert "dev" not in health_version.CURRENT
