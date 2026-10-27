"""Contract for config.json type guards at the #4146 sibling sites (#4203).

Written BEFORE the fix (contract-first). config.json is hand-edited; `.get(k, d)`
covers only a MISSING key, so an explicit null / wrong type must not raise.

Contract:
  C1 (URL resolution: _status_derive._resolve_forum_url, forum_api.forum_url_from_config,
     baton-migrate main()): a `forum` that is not a dict, or a `url` that is not a
     non-blank string, is treated exactly as if absent: fall through to
     $FORUM_URL, then http://localhost:5002 (migrate: args.forum_url stays first).
     Never raises. A valid string url keeps today's behaviour.
  C2 (surgical._get_current_turn): non-dict top-level config, non-dict `memory`,
     or a `current_turn` that is not an int (bool is NOT an int) returns 0.
     A valid int is returned unchanged.
  C3 (tripwire._load_config): a non-dict top-level JSON value refuses like a
     missing or unparseable config: ONE stderr line "tripwire: config top level
     is not a JSON object (got <type name>) at <path>", exit EXIT_ERROR, nothing
     on stdout. Never returns a non-dict, never returns {} for this case.
  C4 (tripwire._infer_approver message): a present-but-non-string self_lineage is
     shown RAW with "(must be a string)"; a missing key keeps today's text.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("isolate_hook_loads_module")

TOOLS = Path(__file__).resolve().parent.parent / "src" / "engram" / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DEFAULT = "http://localhost:5002"
_ENV = "http://env.example:9"

# Bad values for a dict-valued key (`forum`, `memory`).
BAD_DICT = [None, 42, ["x"], "x", True]
BAD_DICT_IDS = ["none", "int", "list", "str", "bool"]
# Bad values for a string-valued key (`forum.url`). Blank strings count as absent.
BAD_STR = [None, 42, ["http://x"], {"a": 1}, True, "", "   "]
BAD_STR_IDS = ["none", "int", "list", "dict", "bool", "empty", "blank"]


@pytest.fixture
def env_url(monkeypatch):
    monkeypatch.setenv("FORUM_URL", _ENV)


# --- C1 site 1: _status_derive -------------------------------------------------------

@pytest.fixture
def sd(monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    return _load("_sd_4203", TOOLS / "_status_derive.py")


def _sd_resolve(sd, monkeypatch, config):
    monkeypatch.setattr(sd, "_load_config", lambda: config)
    return sd._resolve_forum_url()


@pytest.mark.parametrize("bad", BAD_STR, ids=BAD_STR_IDS)
def test_status_derive_bad_url_falls_through(sd, monkeypatch, env_url, bad):
    assert _sd_resolve(sd, monkeypatch, {"forum": {"url": bad}}) == _ENV


@pytest.mark.parametrize("bad", BAD_STR, ids=BAD_STR_IDS)
def test_status_derive_bad_url_falls_to_default(sd, monkeypatch, bad):
    monkeypatch.delenv("FORUM_URL", raising=False)
    assert _sd_resolve(sd, monkeypatch, {"forum": {"url": bad}}) == _DEFAULT


@pytest.mark.parametrize("bad", BAD_DICT, ids=BAD_DICT_IDS)
def test_status_derive_bad_forum_falls_through(sd, monkeypatch, env_url, bad):
    assert _sd_resolve(sd, monkeypatch, {"forum": bad}) == _ENV


def test_status_derive_valid_url_unchanged(sd, monkeypatch, env_url):
    assert _sd_resolve(sd, monkeypatch, {"forum": {"url": " http://a:1/ "}}) == "http://a:1"


# --- C1 site 2: forum_api ------------------------------------------------------------

@pytest.fixture
def fa(monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    return _load("_fa_4203", TOOLS / "forum_api.py")


@pytest.mark.parametrize("bad", BAD_STR, ids=BAD_STR_IDS)
def test_forum_api_bad_url_falls_through(fa, env_url, bad):
    assert fa.forum_url_from_config({"forum": {"url": bad}}) == _ENV


@pytest.mark.parametrize("bad", BAD_STR, ids=BAD_STR_IDS)
def test_forum_api_bad_url_falls_to_default(fa, monkeypatch, bad):
    monkeypatch.delenv("FORUM_URL", raising=False)
    assert fa.forum_url_from_config({"forum": {"url": bad}}) == _DEFAULT


@pytest.mark.parametrize("bad", BAD_DICT, ids=BAD_DICT_IDS)
def test_forum_api_bad_forum_falls_through(fa, env_url, bad):
    assert fa.forum_url_from_config({"forum": bad}) == _ENV


def test_forum_api_valid_url_unchanged(fa, env_url):
    assert fa.forum_url_from_config({"forum": {"url": "http://a:1/"}}) == "http://a:1/"
    assert fa.forum_url_from_config({}) == _ENV


# --- C1 site 3: baton-migrate main() -------------------------------------------------

@pytest.fixture
def migrate_url(monkeypatch, tmp_path, capsys):
    mod = _load("_mig_4203", TOOLS / "baton-migrate-to-coord-store.py")
    monkeypatch.setattr(mod, "ForumClient", lambda url: object())
    monkeypatch.setattr(mod, "migrate", lambda *a, **k: (0, 0, 0))

    def run(config, *extra):
        monkeypatch.setattr(mod, "_load_config", lambda: config)
        monkeypatch.setattr(
            sys, "argv",
            ["m", "--projects-dir", str(tmp_path), "--dry-run", *extra],
        )
        mod.main()
        for line in capsys.readouterr().out.splitlines():
            if line.strip().startswith("forum-url:"):
                return line.split("forum-url:", 1)[1].strip()
        raise AssertionError("no forum-url line printed")

    return run


@pytest.mark.parametrize("bad", BAD_STR, ids=BAD_STR_IDS)
def test_migrate_bad_url_falls_through(migrate_url, env_url, bad):
    assert migrate_url({"forum": {"url": bad}}) == _ENV


@pytest.mark.parametrize("bad", BAD_STR, ids=BAD_STR_IDS)
def test_migrate_bad_url_falls_to_default(migrate_url, monkeypatch, bad):
    monkeypatch.delenv("FORUM_URL", raising=False)
    assert migrate_url({"forum": {"url": bad}}) == _DEFAULT


@pytest.mark.parametrize("bad", BAD_DICT, ids=BAD_DICT_IDS)
def test_migrate_bad_forum_falls_through(migrate_url, env_url, bad):
    assert migrate_url({"forum": bad}) == _ENV


def test_migrate_valid_and_arg_precedence_unchanged(migrate_url, env_url):
    assert migrate_url({"forum": {"url": "http://a:1/"}}) == "http://a:1/"
    assert migrate_url({"forum": {"url": 42}}, "--forum-url", "http://arg:2") == "http://arg:2"


# --- C2: surgical._get_current_turn --------------------------------------------------

@pytest.fixture
def turn(monkeypatch, tmp_path):
    sg = _load("_sg_4203", TOOLS / "surgical.py")
    monkeypatch.setattr(sg, "DATA_DIR", tmp_path)

    def get(raw):
        (tmp_path / "config.json").write_text(json.dumps(raw))
        return sg._get_current_turn()

    return get


@pytest.mark.parametrize("bad", BAD_DICT, ids=BAD_DICT_IDS)
def test_surgical_bad_memory_returns_zero(turn, bad):
    assert turn({"memory": bad}) == 0


@pytest.mark.parametrize("bad", [None, "7", 1.5, ["x"], {"a": 1}, True, False],
                         ids=["none", "str", "float", "list", "dict", "true", "false"])
def test_surgical_bad_current_turn_returns_zero(turn, bad):
    assert turn({"memory": {"current_turn": bad}}) == 0


@pytest.mark.parametrize("bad", [[], "x", 3, None, True], ids=["list", "str", "int", "none", "bool"])
def test_surgical_non_dict_toplevel_returns_zero(turn, bad):
    assert turn(bad) == 0


def test_surgical_valid_unchanged(turn):
    assert turn({"memory": {"current_turn": 41}}) == 41
    assert turn({"memory": {}}) == 0
    assert turn({}) == 0


# --- C3 / C4: tripwire ---------------------------------------------------------------

@pytest.fixture
def tw():
    return _load("_tw_4203", TOOLS / "tripwire.py")


@pytest.mark.parametrize(
    "raw,tname",
    [("[]", "list"), ('"x"', "str"), ("3", "int"), ("true", "bool"), ("null", "NoneType")],
    ids=["list", "str", "int", "bool", "null"],
)
def test_tripwire_load_config_non_dict_refuses(tw, tmp_path, monkeypatch, capsys, raw, tname):
    p = tmp_path / "config.json"
    p.write_text(raw)
    monkeypatch.setenv("ENGRAM_CONFIG_PATH", str(p))
    with pytest.raises(SystemExit) as ei:
        tw._load_config()
    assert ei.value.code == tw.EXIT_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"tripwire: config top level is not a JSON object (got {tname}) at {p}\n"


def test_tripwire_valid_config_unchanged(tw, tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"self_lineage": "anthropic:opus", "agent_name": "zed"}))
    monkeypatch.setenv("ENGRAM_CONFIG_PATH", str(p))
    assert tw._infer_approver(tw._load_config()) == "zed"


_ABSENT = object()


@pytest.mark.parametrize(
    "value,expected",
    [
        (42, "self_lineage=42 (must be a string)"),
        (None, "self_lineage=None (must be a string)"),
        (["opus"], "self_lineage=['opus'] (must be a string)"),
        (_ABSENT, "self_lineage=''"),
    ],
    ids=["int", "none", "list", "missing"],
)
def test_tripwire_cannot_infer_message_shows_raw_value(tw, capsys, value, expected):
    cfg = {} if value is _ABSENT else {"self_lineage": value}
    with pytest.raises(SystemExit):
        tw._infer_approver(cfg)
    err = capsys.readouterr().err
    assert expected in err
    if value is _ABSENT:
        assert "must be a string" not in err


def test_tripwire_cannot_infer_message_string_lineage_unchanged(tw, capsys):
    with pytest.raises(SystemExit):
        tw._infer_approver({"self_lineage": "other:x"})
    assert "self_lineage='other:x'; supply --approver explicitly" in capsys.readouterr().err
