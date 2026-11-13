"""#3673: get_primary_user() in engram-user-identity-hook.py reads an unusable
config.json (non-object top level, or nested deeply enough to raise
RecursionError) as "no primary user" (""), like a missing or malformed file."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOOK_PATH = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude" / "engram-user-identity-hook.py"

# #4029: in-process hook loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _load_hook(engram_home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", str(engram_home))
    sys.modules.pop("engram_user_identity_hook_primary_3673", None)
    spec = importlib.util.spec_from_file_location(
        "engram_user_identity_hook_primary_3673", str(_HOOK_PATH)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _primary(tmp_path, monkeypatch, text):
    if text is not None:
        (tmp_path / "config.json").write_text(text)
    return _load_hook(tmp_path, monkeypatch).get_primary_user()


@pytest.mark.parametrize("text", ["[]", "[1, 2]", '"Lin"', "42", "null", "true"])
def test_non_object_top_level_reads_as_no_primary_user(tmp_path, monkeypatch, text):
    assert _primary(tmp_path, monkeypatch, text) == ""


def test_deeply_nested_config_reads_as_no_primary_user(tmp_path, monkeypatch):
    n = 200000
    assert _primary(tmp_path, monkeypatch, "[" * n + "]" * n) == ""


def test_normal_dict_is_lowercased(tmp_path, monkeypatch):
    assert _primary(tmp_path, monkeypatch, '{"primary_user": "Lin"}') == "lin"


def test_null_primary_user_is_empty(tmp_path, monkeypatch):
    assert _primary(tmp_path, monkeypatch, '{"primary_user": null}') == ""


def test_missing_file_is_empty(tmp_path, monkeypatch):
    assert _primary(tmp_path, monkeypatch, None) == ""


def test_malformed_json_is_empty(tmp_path, monkeypatch):
    assert _primary(tmp_path, monkeypatch, "{not json") == ""
