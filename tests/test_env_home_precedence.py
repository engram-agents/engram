"""Tests for ENGRAM_HOME-preferred reader precedence (issue #489, PR 1).

Verifies the canonical reader form:
    os.environ.get("ENGRAM_HOME") or os.environ.get("ENGRAM_DATA_DIR") or <DEFAULT>

Coverage:
  1. engram_client._load_engram_config: ENGRAM_HOME wins over ENGRAM_DATA_DIR.
  2. engram_client._load_engram_config: ENGRAM_DATA_DIR used when ENGRAM_HOME unset.
  3. engram_client._load_engram_config: default used when neither var set.
  4. server.py DATA_DIR: ENGRAM_HOME wins over ENGRAM_DATA_DIR.
  5. server.py DATA_DIR: ENGRAM_DATA_DIR used when ENGRAM_HOME unset.
  6. server.py DATA_DIR: default (~/.engram) used when neither var set.
  7. tools/inspect_raw.py DATA_DIR: ENGRAM_HOME wins over ENGRAM_DATA_DIR.
  8. tools/inspect_raw.py DATA_DIR: ENGRAM_DATA_DIR used when ENGRAM_HOME unset.
  9. tools/inspect_raw.py DATA_DIR: default (~/.engram) used when neither var set.

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
    """Explicitly remove both vars so no leaked global can contaminate."""
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    monkeypatch.delenv("ENGRAM_DATA_DIR", raising=False)


def _reload_server(monkeypatch: pytest.MonkeyPatch, tmp_dir: str) -> object:
    """Reload server module under patched env, then call _configure_paths."""
    # Ensure no embeddings overhead in tests
    monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")
    if "server" in sys.modules:
        del sys.modules["server"]
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

    def test_home_wins_over_data_dir(self, monkeypatch, tmp_path):
        """ENGRAM_HOME set and ENGRAM_DATA_DIR set to a different path →
        config from ENGRAM_HOME is returned (HOME wins)."""
        _clean_env(monkeypatch)
        home_dir = tmp_path / "home"
        data_dir = tmp_path / "data"
        home_dir.mkdir()
        data_dir.mkdir()
        # Write a distinguishing config in each dir
        (home_dir / "config.json").write_text(json.dumps({"_source": "home"}))
        (data_dir / "config.json").write_text(json.dumps({"_source": "data"}))

        monkeypatch.setenv("ENGRAM_HOME", str(home_dir))
        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))

        import engram_client
        cfg = engram_client._load_engram_config()
        assert cfg.get("_source") == "home", (
            f"Expected config from ENGRAM_HOME; got: {cfg}"
        )

    def test_data_dir_fallback_when_home_unset(self, monkeypatch, tmp_path):
        """ENGRAM_HOME unset, ENGRAM_DATA_DIR set → config from ENGRAM_DATA_DIR."""
        _clean_env(monkeypatch)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.json").write_text(json.dumps({"_source": "data"}))

        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))
        # ENGRAM_HOME is explicitly unset (done by _clean_env above)

        import engram_client
        cfg = engram_client._load_engram_config()
        assert cfg.get("_source") == "data", (
            f"Expected config from ENGRAM_DATA_DIR; got: {cfg}"
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

    def test_home_wins_over_data_dir(self, monkeypatch, tmp_path):
        """ENGRAM_HOME set and ENGRAM_DATA_DIR set to a different path →
        DATA_DIR resolves to ENGRAM_HOME value (HOME wins)."""
        _clean_env(monkeypatch)
        home_dir = tmp_path / "home"
        data_dir = tmp_path / "data"
        home_dir.mkdir()
        data_dir.mkdir()

        monkeypatch.setenv("ENGRAM_HOME", str(home_dir))
        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))
        monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")

        if "server" in sys.modules:
            del sys.modules["server"]
        import server  # type: ignore[import]
        # DATA_DIR is set at import time from the env
        assert server.DATA_DIR == Path(home_dir), (
            f"Expected DATA_DIR={home_dir}; got {server.DATA_DIR}"
        )

    def test_data_dir_fallback_when_home_unset(self, monkeypatch, tmp_path):
        """ENGRAM_HOME unset, ENGRAM_DATA_DIR set → DATA_DIR is ENGRAM_DATA_DIR."""
        _clean_env(monkeypatch)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))
        monkeypatch.setenv("ENGRAM_NO_EMBEDDINGS", "1")

        if "server" in sys.modules:
            del sys.modules["server"]
        import server  # type: ignore[import]
        assert server.DATA_DIR == Path(data_dir), (
            f"Expected DATA_DIR={data_dir}; got {server.DATA_DIR}"
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

        if "server" in sys.modules:
            del sys.modules["server"]
        import server  # type: ignore[import]
        assert server.DATA_DIR == Path(tmp_path / "default_engram"), (
            f"Expected DATA_DIR to use expanduser default; got {server.DATA_DIR}"
        )


# ---------------------------------------------------------------------------
# 7-9. tools/inspect_raw.py DATA_DIR
# ---------------------------------------------------------------------------

class TestInspectRawDataDirResolution:
    """tools/inspect_raw.py DATA_DIR resolves at module import time."""

    def test_home_wins_over_data_dir(self, monkeypatch, tmp_path):
        """ENGRAM_HOME set and ENGRAM_DATA_DIR set to a different path →
        DATA_DIR resolves to ENGRAM_HOME value (HOME wins)."""
        _clean_env(monkeypatch)
        home_dir = tmp_path / "home"
        data_dir = tmp_path / "data"
        home_dir.mkdir()
        data_dir.mkdir()

        monkeypatch.setenv("ENGRAM_HOME", str(home_dir))
        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))

        inspect_raw = _reload_inspect_raw(monkeypatch)
        assert inspect_raw.DATA_DIR == Path(home_dir), (
            f"Expected DATA_DIR={home_dir}; got {inspect_raw.DATA_DIR}"
        )

    def test_data_dir_fallback_when_home_unset(self, monkeypatch, tmp_path):
        """ENGRAM_HOME unset, ENGRAM_DATA_DIR set → DATA_DIR is ENGRAM_DATA_DIR."""
        _clean_env(monkeypatch)
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        monkeypatch.setenv("ENGRAM_DATA_DIR", str(data_dir))

        inspect_raw = _reload_inspect_raw(monkeypatch)
        assert inspect_raw.DATA_DIR == Path(data_dir), (
            f"Expected DATA_DIR={data_dir}; got {inspect_raw.DATA_DIR}"
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
