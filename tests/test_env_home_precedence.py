"""Tests for ENGRAM_HOME as the single canonical data-dir env var.

ENGRAM_DATA_DIR was removed (unified into ENGRAM_HOME, 2026-06-01); the canonical
reader form is now:
    os.environ.get("ENGRAM_HOME") or <DEFAULT>

These tests are the **unification regression guard**: they set a *stray*
ENGRAM_DATA_DIR and assert it is IGNORED, so the removed fallback cannot creep
back in. (Originally an ENGRAM_HOME-vs-ENGRAM_DATA_DIR precedence test, #489.)

Coverage (× 3 consumers: engram_client._load_engram_config, server.py DATA_DIR,
tools/inspect_raw.py DATA_DIR):
  1. ENGRAM_HOME set + a stray ENGRAM_DATA_DIR set → resolves to ENGRAM_HOME
     (the stray ENGRAM_DATA_DIR has no effect).
  2. ENGRAM_HOME unset + a stray ENGRAM_DATA_DIR set → resolves to the ~/.engram
     DEFAULT, NOT to ENGRAM_DATA_DIR (proves the fallback is gone).
  3. Neither var set → resolves to the ~/.engram default.

Each test explicitly clears both vars at the start so a leaked global from another
test module cannot contaminate results.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove ENGRAM_HOME and any stray ENGRAM_DATA_DIR so no leaked global
    contaminates results."""
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    monkeypatch.delenv("ENGRAM_DATA_DIR", raising=False)


def _reload_server(monkeypatch: pytest.MonkeyPatch, tmp_dir: str) -> object:
    """Reload server module under patched env, then call _configure_paths."""
    # Ensure no embeddings overhead in tests
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            del sys.modules[key]
    import server  # type: ignore[import]
    server._configure_paths(tmp_dir)
    server._ensure_data_dir()
    return server


def _reload_inspect_raw(monkeypatch: pytest.MonkeyPatch) -> object:
    """Reload tools/inspect_raw module under currently-patched env."""
    # inspect_raw reads DATA_DIR at module level; must reload after env set
    key = "tools.inspect_raw"
    # Also try plain "inspect_raw" key since path-based imports may use either
    for k in list(sys.modules.keys()):
        if "inspect_raw" in k:
            del sys.modules[k]
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import inspect_raw  # type: ignore[import]
    return inspect_raw


# ---------------------------------------------------------------------------
# 1-3. engram_client._load_engram_config
# ---------------------------------------------------------------------------

class TestEngramClientConfigResolution:
    """engram_client._load_engram_config reads ENGRAM_HOME to find config.json."""

    def test_home_used_stray_data_dir_ignored(self, monkeypatch, tmp_path):
        """ENGRAM_HOME set + a stray ENGRAM_DATA_DIR set to a different path →
        config from ENGRAM_HOME is returned; the stray ENGRAM_DATA_DIR is ignored."""
        _clean_env(monkeypatch)
        home_dir = tmp_path / "home"
        data_dir = tmp_path / "data"
        home_dir.mkdir()
        data_dir.mkdir()
        # Write a distinguishing config in each dir
        (home_dir / "config.json").write_text(json.dumps({"_source": "home"}))
        (data_dir / "config.json").write_text(json.dumps({"_source": "data"}))

        monkeypatch.setenv("ENGRAM_HOME", str(home_dir))
        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))  # must be IGNORED

        import engram_client
        cfg = engram_client._load_engram_config()
        assert cfg.get("_source") == "home", (
            f"Expected config from ENGRAM_HOME (stray ENGRAM_DATA_DIR ignored); got: {cfg}"
        )

    def test_data_dir_ignored_when_home_unset(self, monkeypatch, tmp_path):
        """ENGRAM_HOME unset + a stray ENGRAM_DATA_DIR set → resolution IGNORES
        ENGRAM_DATA_DIR and falls through to the ~/.engram default (no config
        there → {}). Guards against the removed DATA_DIR fallback creeping back."""
        _clean_env(monkeypatch)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.json").write_text(json.dumps({"_source": "data"}))
        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))  # must be IGNORED

        # Point the ~/.engram default at an empty dir (no config.json)
        no_config_dir = tmp_path / "empty_home"
        no_config_dir.mkdir()
        original_expanduser = os.path.expanduser

        def fake_expanduser(p):
            if p == "~/.engram":
                return str(no_config_dir)
            return original_expanduser(p)

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        import engram_client
        cfg = engram_client._load_engram_config()
        assert cfg == {}, (
            f"Expected {{}} (default used, stray ENGRAM_DATA_DIR ignored); got: {cfg}"
        )

    def test_default_when_neither_set(self, monkeypatch, tmp_path):
        """Neither ENGRAM_HOME nor ENGRAM_DATA_DIR set → _load_config returns {}
        (the module default is ~/.engram; no config there in CI, so returns {})."""
        _clean_env(monkeypatch)
        # We can't assert a specific path here since ~/.engram may or may not exist,
        # but we CAN verify that _load_config returns a dict (not an exception) and
        # that the resolved path uses the expanduser default.
        import engram_client
        # Patch expanduser to a controlled tmp dir with no config.json
        no_config_dir = tmp_path / "empty_home"
        no_config_dir.mkdir()
        original_expanduser = os.path.expanduser

        def fake_expanduser(p):
            if p == "~/.engram":
                return str(no_config_dir)
            return original_expanduser(p)

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        cfg = engram_client._load_engram_config()
        assert cfg == {}, (
            f"Expected empty dict when no config.json at default path; got: {cfg}"
        )


# ---------------------------------------------------------------------------
# 4-6. server.py DATA_DIR
# ---------------------------------------------------------------------------

class TestServerDataDirResolution:
    """server.DATA_DIR resolves from env vars at module import time."""

    def test_home_used_stray_data_dir_ignored(self, monkeypatch, tmp_path):
        """ENGRAM_HOME set + a stray ENGRAM_DATA_DIR set to a different path →
        DATA_DIR resolves to ENGRAM_HOME; the stray ENGRAM_DATA_DIR is ignored."""
        _clean_env(monkeypatch)
        home_dir = tmp_path / "home"
        data_dir = tmp_path / "data"
        home_dir.mkdir()
        data_dir.mkdir()

        monkeypatch.setenv("ENGRAM_HOME", str(home_dir))
        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))  # must be IGNORED
        monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")

        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        import server  # type: ignore[import]
        # DATA_DIR is set at import time from the env
        assert server.DATA_DIR == Path(home_dir), (
            f"Expected DATA_DIR={home_dir} (stray ENGRAM_DATA_DIR ignored); got {server.DATA_DIR}"
        )

    def test_data_dir_ignored_when_home_unset(self, monkeypatch, tmp_path):
        """ENGRAM_HOME unset + a stray ENGRAM_DATA_DIR set → DATA_DIR resolves to
        the ~/.engram default, NOT to ENGRAM_DATA_DIR (fallback removed)."""
        _clean_env(monkeypatch)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))  # must be IGNORED
        monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
        original_expanduser = os.path.expanduser

        def fake_expanduser(p):
            if p == "~/.engram":
                return str(tmp_path / "default_engram")
            return original_expanduser(p)

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        import server  # type: ignore[import]
        assert server.DATA_DIR == Path(tmp_path / "default_engram"), (
            f"Expected DATA_DIR=default (stray ENGRAM_DATA_DIR ignored); got {server.DATA_DIR}"
        )

    def test_default_when_neither_set(self, monkeypatch, tmp_path):
        """Neither env var set → DATA_DIR is the expanduser default (~/.engram)."""
        _clean_env(monkeypatch)
        monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
        original_expanduser = os.path.expanduser

        def fake_expanduser(p):
            if p == "~/.engram":
                return str(tmp_path / "default_engram")
            return original_expanduser(p)

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        for key in list(sys.modules):
            if key == "server" or key.startswith("engram_"):
                del sys.modules[key]
        import server  # type: ignore[import]
        assert server.DATA_DIR == Path(tmp_path / "default_engram"), (
            f"Expected DATA_DIR to use expanduser default; got {server.DATA_DIR}"
        )


# ---------------------------------------------------------------------------
# 7-9. tools/inspect_raw.py DATA_DIR
# ---------------------------------------------------------------------------

class TestInspectRawDataDirResolution:
    """tools/inspect_raw.py DATA_DIR resolves at module import time."""

    def test_home_used_stray_data_dir_ignored(self, monkeypatch, tmp_path):
        """ENGRAM_HOME set + a stray ENGRAM_DATA_DIR set to a different path →
        DATA_DIR resolves to ENGRAM_HOME; the stray ENGRAM_DATA_DIR is ignored."""
        _clean_env(monkeypatch)
        home_dir = tmp_path / "home"
        data_dir = tmp_path / "data"
        home_dir.mkdir()
        data_dir.mkdir()

        monkeypatch.setenv("ENGRAM_HOME", str(home_dir))
        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))  # must be IGNORED

        inspect_raw = _reload_inspect_raw(monkeypatch)
        assert inspect_raw.DATA_DIR == Path(home_dir), (
            f"Expected DATA_DIR={home_dir} (stray ENGRAM_DATA_DIR ignored); got {inspect_raw.DATA_DIR}"
        )

    def test_data_dir_ignored_when_home_unset(self, monkeypatch, tmp_path):
        """ENGRAM_HOME unset + a stray ENGRAM_DATA_DIR set → DATA_DIR resolves to
        the ~/.engram default, NOT to ENGRAM_DATA_DIR (fallback removed)."""
        _clean_env(monkeypatch)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))  # must be IGNORED
        original_expanduser = os.path.expanduser

        def fake_expanduser(p):
            if p == "~/.engram":
                return str(tmp_path / "default_engram")
            return original_expanduser(p)

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        inspect_raw = _reload_inspect_raw(monkeypatch)
        assert inspect_raw.DATA_DIR == Path(tmp_path / "default_engram"), (
            f"Expected DATA_DIR=default (stray ENGRAM_DATA_DIR ignored); got {inspect_raw.DATA_DIR}"
        )

    def test_default_when_neither_set(self, monkeypatch, tmp_path):
        """Neither env var set → DATA_DIR is the expanduser default (~/.engram)."""
        _clean_env(monkeypatch)
        original_expanduser = os.path.expanduser

        def fake_expanduser(p):
            if p == "~/.engram":
                return str(tmp_path / "default_engram")
            return original_expanduser(p)

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        inspect_raw = _reload_inspect_raw(monkeypatch)
        assert inspect_raw.DATA_DIR == Path(tmp_path / "default_engram"), (
            f"Expected DATA_DIR to use expanduser default; got {inspect_raw.DATA_DIR}"
        )
