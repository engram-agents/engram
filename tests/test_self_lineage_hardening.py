"""Regression tests for self_lineage validation hardening (#960).

Three scenarios from Mira's #750 post-P4 concern:

  1. Malformed self_lineage config value → _self_lineage() fails closed
     (returns '') AND emits a warning once (caplog assertion).
  2. Valid value ('anthropic:opus') → passes through unchanged.
  3. Empty value → passes (empty is allowed — feature-dark mode).

MUTATION CHECK (ls_0014 — green-is-not-ground-truth):
  Each test that asserts the fail-closed path is verified non-vacuous: see
  inline comments explaining what would break if the guard were removed.
  The mutation check was done by temporarily removing the `if val and not
  _LINEAGE_RE.fullmatch(val): return ""` branch and confirming test_malformed
  returns the raw value instead of '', causing the assertion to fail. Guard
  restored.

Import pattern:
  Uses ``fresh_module(engram_core)`` (conftest.py fixture) to reload
  engram_core between tests, resetting the _self_lineage_warned sentinel so
  each test gets a clean once-per-process state.  Follows Rule A of the
  seam gate: no ``from engram_core import ...``.
"""

import importlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest

# conftest.py adds REPO_ROOT to sys.path.
import engram_core


def _fresh_core():
    """Reload engram_core (and purge server + all engram_* family) so the
    once-per-process _self_lineage_warned sentinel resets between tests.

    Follows the conftest.fresh_module pattern: purge + importlib.import_module.
    Returns the freshly loaded engram_core module.
    """
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    return importlib.import_module("engram_core")


# ---------------------------------------------------------------------------
# Test 1: malformed value → fail-closed + warning once
# ---------------------------------------------------------------------------

def test_malformed_self_lineage_fails_closed_and_warns(caplog, tmp_path):
    """_self_lineage() must return '' and emit a warning for a bad config.

    MUTATION CHECK: if the guard `if val and not _LINEAGE_RE.fullmatch(val):
    return ""` were absent, _self_lineage() would return the raw malformed
    value 'not a valid lineage!!' instead of ''.  The assertion
    ``assert result == ""`` would then fail (got 'not a valid lineage!!'),
    confirming this test is non-vacuous.
    """
    core = _fresh_core()
    config = {"self_lineage": "not a valid lineage!!"}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    core._configure_paths(str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="engram_core"):
        result = core._self_lineage()

    assert result == "", (
        f"Expected '' (fail-closed) for malformed self_lineage, got {result!r}"
    )
    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "not a valid lineage!!" in m and "provider:family" in m
        for m in warning_messages
    ), (
        f"Expected warning naming malformed value and expected shape, "
        f"got records: {warning_messages}"
    )


def test_malformed_self_lineage_warns_only_once(caplog, tmp_path):
    """The warning must fire at most once per process (once-per-process sentinel).

    Calls _self_lineage() twice with the same malformed config; expects exactly
    one warning record from the second and subsequent calls being suppressed.
    """
    core = _fresh_core()
    config = {"self_lineage": "bad::value"}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    core._configure_paths(str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="engram_core"):
        core._self_lineage()
        core._self_lineage()
        core._self_lineage()

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING
                       and "provider:family" in r.message]
    assert len(warning_records) == 1, (
        f"Expected exactly 1 warning for repeated malformed calls, "
        f"got {len(warning_records)}: {[r.message for r in warning_records]}"
    )


# ---------------------------------------------------------------------------
# Test 2: valid value → passes through unchanged
# ---------------------------------------------------------------------------

def test_valid_self_lineage_passthrough(tmp_path):
    """_self_lineage() must return the value unchanged for 'anthropic:opus'."""
    core = _fresh_core()
    config = {"self_lineage": "anthropic:opus"}
    (tmp_path / "config.json").write_text(json.dumps(config))
    core._configure_paths(str(tmp_path))

    result = core._self_lineage()
    assert result == "anthropic:opus", (
        f"Expected 'anthropic:opus' passthrough, got {result!r}"
    )


def test_valid_self_lineage_other_forms(tmp_path):
    """Additional valid provider:family forms pass through unchanged.

    Valid forms per _LINEAGE_RE = r'[a-z0-9_-]+:[a-z0-9._-]+':
      - provider side: lowercase letters, digits, underscore, hyphen (NO dot)
      - family side: lowercase letters, digits, dot, underscore, hyphen
    """
    core = _fresh_core()
    # All values below satisfy [a-z0-9_-]+:[a-z0-9._-]+
    for value in ("openai:gpt", "google:gemini", "x-ai:grok", "provider:family.v2"):
        (tmp_path / "config.json").write_text(json.dumps({"self_lineage": value}))
        core._configure_paths(str(tmp_path))
        # Reset the sentinel between iterations since _fresh_core was not called again
        core._self_lineage_warned = False
        result = core._self_lineage()
        assert result == value, (
            f"Expected passthrough for {value!r}, got {result!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: empty value → allowed (feature-dark mode)
# ---------------------------------------------------------------------------

def test_empty_self_lineage_allowed(tmp_path):
    """_self_lineage() must return '' for an empty or absent config value.

    Empty = feature dark (safe). Must NOT emit a warning.
    """
    core = _fresh_core()

    # Absent key
    (tmp_path / "config.json").write_text(json.dumps({}))
    core._configure_paths(str(tmp_path))
    result = core._self_lineage()
    assert result == "", f"Expected '' for absent key, got {result!r}"

    # Explicitly empty string
    core._self_lineage_warned = False
    (tmp_path / "config.json").write_text(json.dumps({"self_lineage": ""}))
    result = core._self_lineage()
    assert result == "", f"Expected '' for empty string, got {result!r}"

    # Whitespace-only (stripped to empty)
    core._self_lineage_warned = False
    (tmp_path / "config.json").write_text(json.dumps({"self_lineage": "  "}))
    result = core._self_lineage()
    assert result == "", f"Expected '' for whitespace-only, got {result!r}"


# ---------------------------------------------------------------------------
# SSoT check: engram_observation.py uses core._LINEAGE_RE, not a local copy
# ---------------------------------------------------------------------------

def test_observation_lineage_gate_uses_core_ssot():
    """engram_observation.py must NOT contain an inline regex literal for
    lineage validation — it must use core._LINEAGE_RE (SSoT, #960 change 1).

    This is a static grep-gate: if the inline literal
    ``r"[a-z0-9_-]+:[a-z0-9._-]+"`` reappears in the validation block,
    the de-duplication was reverted.
    """
    repo_root = Path(__file__).parent.parent
    obs_path = repo_root / "src" / "engram" / "engram_observation.py"
    content = obs_path.read_text(encoding="utf-8")

    # The inline fullmatch with a literal regex string must no longer appear.
    # The SSoT form is: core._LINEAGE_RE.fullmatch(...)
    assert 're.fullmatch(\n        r"[a-z0-9_-]+:[a-z0-9._-]+"' not in content, (
        "Inline regex literal found in engram_observation.py — SSoT violated"
    )
    assert 're.fullmatch(\n    r"[a-z0-9_-]+:[a-z0-9._-]+"' not in content, (
        "Inline regex literal found in engram_observation.py — SSoT violated"
    )
    # The SSoT form must be present.
    assert "core._LINEAGE_RE.fullmatch" in content, (
        "core._LINEAGE_RE.fullmatch not found in engram_observation.py — "
        "SSoT reference missing"
    )
