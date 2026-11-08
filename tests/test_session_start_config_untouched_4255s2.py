"""#4255 slice 2 (amended S2.4): session start no longer writes config.json.

The hook's ``update_counterparts_from_registry`` rewrote the operator's config.json
(``counterparts``) at every session start for a key nothing reads. It is retired.
Its sibling ``write_agent_registration`` stays: ``peer_topology_block`` reads that
registry.

Contract (runs the REAL hook as a subprocess):
  A. On a seat whose config.json has ``agent_name`` and whose registry holds a co-host
     peer record, config.json is BYTE-IDENTICAL after a session start (mtime too). The
     config is deliberately non-canonical (4-space indent, unsorted keys, no trailing
     newline, a stale ``counterparts`` list), so a build that merely re-dumps the same
     JSON still changes bytes.
  B. This agent's registry file IS still written, with the documented shape.
  C. A holds across: config mode 0444, umask 077 / 002, agent_name null / empty /
     non-string (#4137 guard), registry dir absent, shared root absent, an unparseable
     registry record, ENGRAM_HOME reached through a symlink.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = (Path(__file__).resolve().parents[1] / "src" / "engram" / "hooks" / "claude"
        / "engram-session-start-hook.py")
HOST = socket.gethostname()

# Non-canonical on purpose: json.dumps(json.loads(x), indent=2) + "\n" cannot reproduce it.
NONCANON = (
    '{\n'
    '    "z_last": 1,\n'
    '    "agent_name": "sol",\n'
    '    "counterparts": ["stale-peer"],\n'
    '    "a_first": {"k": [1,  2]}\n'
    '}'
)


def _peer(reg: Path, name: str = "goodpeer") -> None:
    reg.mkdir(parents=True, exist_ok=True)
    (reg / f"{name}.json").write_text(json.dumps({
        "agent_name": name, "hostname": HOST, "pid": 1,
        "registered_at": "2099-01-01T00:00:00Z",
    }))


def _seat(tmp_path: Path, cfg_text: str = NONCANON, *, peers: bool = True, shared_root: bool = True):
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.json"
    cfg.write_text(cfg_text)
    shared = tmp_path / "shared"
    if shared_root:
        shared.mkdir()
        if peers:
            _peer(shared / "agents")
    return home, cfg, shared


def _run(home: Path, shared: Path, tmp_path: Path, *, umask=None):
    env = {**os.environ, "ENGRAM_HOME": str(home), "AGENTS_SHARED_DIR": str(shared), "LC_ALL": "C"}
    env.pop("ENGRAM_RUNTIME_DIR", None)
    pre = (lambda: os.umask(umask)) if umask is not None else None
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"source": "startup", "session_id": "t4255s2"}),
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120, preexec_fn=pre,
    )
    assert "Traceback" not in r.stderr, r.stderr[-800:]
    # Never from a hook: the retired-key line must not reach a prompt's context by any route
    # (a textual scan of the sources cannot see a line built by concatenation).
    assert "#4255" not in r.stdout + r.stderr, (r.stdout + r.stderr)[-800:]
    return r


def _snap(cfg: Path):
    st = cfg.stat()
    return cfg.read_bytes(), st.st_mtime_ns


def test_A_config_bytes_and_mtime_unchanged_with_cohost_peer(tmp_path):
    home, cfg, shared = _seat(tmp_path)
    before = _snap(cfg)
    _run(home, shared, tmp_path)
    assert _snap(cfg) == before


def test_B_registry_file_is_still_written_with_its_shape(tmp_path):
    home, cfg, shared = _seat(tmp_path)
    _run(home, shared, tmp_path)
    rec = json.loads((shared / "agents" / "sol.json").read_text())
    assert set(rec) == {"agent_name", "hostname", "pid", "registered_at"}
    assert rec["agent_name"] == "sol"
    assert rec["hostname"] == HOST
    assert isinstance(rec["pid"], int) and rec["pid"] > 0
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["registered_at"])


def test_B_registry_written_even_with_no_peers(tmp_path):
    """The registration write must not depend on peers existing."""
    home, cfg, shared = _seat(tmp_path, peers=False)
    before = _snap(cfg)
    _run(home, shared, tmp_path)
    assert json.loads((shared / "agents" / "sol.json").read_text())["agent_name"] == "sol"
    assert _snap(cfg) == before


def test_C_config_mode_0444(tmp_path):
    home, cfg, shared = _seat(tmp_path)
    cfg.chmod(0o444)
    before = _snap(cfg)
    _run(home, shared, tmp_path)
    assert _snap(cfg) == before
    # #4445: SessionStart now strips group/other bits from a top-level file in the data home
    # (0444 -> 0400). The contract here is that the CONTENT is never rewritten (bytes and
    # mtime above, on a file nobody can write); the mode is the one thing that may change,
    # and only by dropping group/other bits.
    assert cfg.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("um", [0o077, 0o002])
def test_C_umask(tmp_path, um):
    home, cfg, shared = _seat(tmp_path)
    before = _snap(cfg)
    _run(home, shared, tmp_path, umask=um)
    assert _snap(cfg) == before


@pytest.mark.parametrize("name_json", ["null", '""', "42", '["x"]', '{"a": 1}', "true", '"   "'])
def test_C_bad_agent_name_guard(tmp_path, name_json):
    cfg_text = '{\n    "counterparts": ["stale"],\n    "agent_name": %s\n}' % name_json
    home, cfg, shared = _seat(tmp_path, cfg_text)
    before = _snap(cfg)
    _run(home, shared, tmp_path)
    assert _snap(cfg) == before
    # The #4137 guard: nobody is registered under a non-name. Only the pre-seeded peer file is there.
    assert sorted(p.name for p in (shared / "agents").glob("*.json")) == ["goodpeer.json"]


def test_C_registry_dir_absent(tmp_path):
    home, cfg, shared = _seat(tmp_path, peers=False)
    assert not (shared / "agents").exists()
    before = _snap(cfg)
    _run(home, shared, tmp_path)
    assert _snap(cfg) == before


def test_C_shared_root_absent(tmp_path):
    home, cfg, shared = _seat(tmp_path, shared_root=False)
    assert not shared.exists()
    before = _snap(cfg)
    _run(home, shared, tmp_path)
    assert _snap(cfg) == before
    assert not shared.exists()  # single-host install: nothing is created


def test_C_unparseable_registry_record(tmp_path):
    home, cfg, shared = _seat(tmp_path)
    (shared / "agents" / "aaa-broken.json").write_text("{invalid json {{")
    (shared / "agents" / "deep.json").write_text("[" * 100000 + "]" * 100000)
    before = _snap(cfg)
    _run(home, shared, tmp_path)
    assert _snap(cfg) == before


def test_C_symlinked_engram_home(tmp_path):
    home, cfg, shared = _seat(tmp_path)
    link = tmp_path / "homelink"
    link.symlink_to(home, target_is_directory=True)
    before = _snap(cfg)
    _run(link, shared, tmp_path)
    assert _snap(cfg) == before


@pytest.mark.parametrize("variant", ["umask077", "umask002", "mode0444", "registry_dir_absent",
                                     "unparseable_record", "symlinked_home"])
def test_B_registry_written_across_the_environment_dimensions(tmp_path, variant):
    """The same dimensions as the C cells, asserted on the registration side."""
    home, cfg, shared = _seat(tmp_path, peers=(variant != "registry_dir_absent"))
    um = {"umask077": 0o077, "umask002": 0o002}.get(variant)
    run_home = home
    if variant == "mode0444":
        cfg.chmod(0o444)
    if variant == "unparseable_record":
        (shared / "agents" / "aaa-broken.json").write_text("{invalid json {{")
    if variant == "symlinked_home":
        run_home = tmp_path / "homelink"
        run_home.symlink_to(home, target_is_directory=True)
    _run(run_home, shared, tmp_path, umask=um)
    rec = json.loads((shared / "agents" / "sol.json").read_text())
    assert rec["agent_name"] == "sol" and rec["hostname"] == HOST


def test_the_writer_and_its_scan_entry_are_gone():
    src = HOOK.read_text()
    assert "update_counterparts_from_registry" not in src
    assert 'config["counterparts"]' not in src
    scan = (Path(__file__).resolve().parents[1] / "tools" / "engramd_read_scan.config.json").read_text()
    assert "update_counterparts_from_registry" not in scan
    assert "write_agent_registration" in src and "write_agent_registration" in scan  # sibling stays
