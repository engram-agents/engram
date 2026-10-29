"""Contract tests for #4255 slice 1: the build engine retires the --multi-agent axis.

Written BEFORE the implementation (contract-first). The contract is the slice-1 text of #4255,
items S1.1 (build side), S1.2, S1.3, S1.4, S1.5, S1.6. The installer cells (S1.1 install.sh, S1.7,
S1.8) live beside the existing installer cells in test_install_sh_4097.py and
test_install_local_marketplace.py.

  S1.1  `--multi-agent` is accepted and ignored: same exit status, exactly one stderr line
        `--multi-agent is retired and has no effect (#4255)`, same built tree; `--help` stops
        listing it.
  S1.2  the shipped file set per tier does not change (checked against the pure tier predicate
        over tiers.json, computed here, not by the engine).
  S1.3  select_shippables & co lose the parameter; tiers.json loses its doc key; an entry that
        carries `multi_agent` is REFUSED, the message names the path.
  S1.4  .engram-build-manifest.json has no `multi_agent` key (both writers) and the build log
        never prints the axis.
  S1.5  flags-match-config compares tier only.
  S1.6  marketplace-rebuilt reads neither config key.
"""

import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_PLUGIN_SH = REPO_ROOT / "tools" / "build-plugin.sh"
TIERS_JSON = REPO_ROOT / "src" / "build" / "packaging" / "tiers.json"

TIER_RANK = {"essential": 0, "convenience": 1, "dev": 2}
RETIRED = "--multi-agent is retired and has no effect (#4255)"
# Paths every build ships that are not manifest `mechanisms` entries (build inputs).
BUILD_INPUT_PATHS = {"hooks/hooks.json", "packaging/mcp.json", "platform.json", "plugin.json"}
_TS = re.compile(rb"-dev\.\d{14}\.")


def _env(**extra):
    env = {**os.environ, "LC_ALL": "C"}
    env.update(extra)
    return env


def _via_sh(out, *args, cwd=None, env=None):
    return subprocess.run(
        ["bash", str(BUILD_PLUGIN_SH), "--allow-branch", "--allow-untagged", "--output", str(out), *args],
        cwd=str(cwd or REPO_ROOT), env=env or _env(), capture_output=True, text=True, timeout=180,
    )


def _via_cli(out, *args):
    return subprocess.run(
        [sys.executable, "-m", "tools.engine.cli", "build", "--allow-branch", "--allow-untagged",
         "--output", str(out), *args],
        cwd=str(REPO_ROOT), env=_env(), capture_output=True, text=True, timeout=180,
    )


def _tree(root):
    """relative path -> sha256, with the per-build timestamp in the version stamp masked."""
    out = {}
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            out[os.path.relpath(p, root)] = hashlib.sha256(_TS.sub(b"-dev.TS.", Path(p).read_bytes())).hexdigest()
    return out


def _retired_lines(stderr):
    return [l for l in stderr.splitlines() if "retired" in l]


@pytest.fixture(scope="module")
def conv_flagless(tmp_path_factory):
    out = tmp_path_factory.mktemp("flagless") / "plugin"
    r = _via_sh(out, "--tier", "convenience")
    assert r.returncode == 0, r.stderr
    return out, r


# ── S1.1: the flag is accepted and ignored ──────────────────────────────────────────────────
@pytest.mark.parametrize("runner", [_via_sh, _via_cli], ids=["build-plugin.sh", "cli-build"])
def test_flag_is_accepted_ignored_and_announced_exactly_once(runner, conv_flagless, tmp_path):
    base_out, base = conv_flagless
    out = tmp_path / "plugin"
    r = runner(out, "--tier", "convenience", "--multi-agent")
    assert r.returncode == base.returncode == 0, r.stderr
    assert r.stderr.splitlines().count(RETIRED) == 1, r.stderr
    assert len(_retired_lines(r.stderr)) == 1, r.stderr
    assert _tree(out) == _tree(base_out)


def test_flagless_run_prints_no_retirement_line(conv_flagless):
    assert _retired_lines(conv_flagless[1].stderr) == []


def test_flag_given_twice_prints_one_line_per_distinct_flag(tmp_path):
    r = _via_sh(tmp_path / "plugin", "--tier", "essential", "--multi-agent", "--multi-agent")
    assert r.returncode == 0, r.stderr
    assert r.stderr.splitlines().count(RETIRED) == 1, r.stderr


def test_flag_does_not_change_the_exit_status_of_a_failing_run(tmp_path):
    bad = _via_sh(tmp_path / "a", "--tier", "bogus")
    badf = _via_sh(tmp_path / "b", "--tier", "bogus", "--multi-agent")
    assert bad.returncode == badf.returncode != 0
    assert badf.stderr.splitlines().count(RETIRED) == 1


@pytest.mark.parametrize("args", [("--help",), ("-h",)])
def test_help_no_longer_lists_the_flag(args, tmp_path):
    for runner in (_via_sh, _via_cli):
        r = runner(tmp_path / "x", *args)
        assert r.returncode == 0
        assert "multi-agent" not in (r.stdout + r.stderr).lower()


def test_build_plugin_sh_header_does_not_document_the_flag():
    assert "multi-agent" not in BUILD_PLUGIN_SH.resolve().read_text().lower()


def test_flag_from_an_empty_cwd_by_absolute_path(conv_flagless, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    base_out, _ = conv_flagless
    out = tmp_path / "plugin"
    r = _via_sh(out, "--tier", "convenience", "--multi-agent", cwd=empty)
    assert r.returncode == 0, r.stderr
    assert r.stderr.splitlines().count(RETIRED) == 1
    assert _tree(out) == _tree(base_out)


def test_flag_through_a_symlinked_output_root_and_tmpdir_with_trailing_slash(conv_flagless, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    base_out, _ = conv_flagless
    r = _via_sh(link / "plugin", "--tier", "convenience", "--multi-agent", env=_env(TMPDIR=str(tmp_path) + "/"))
    assert r.returncode == 0, r.stderr
    assert r.stderr.splitlines().count(RETIRED) == 1
    assert _tree(real / "plugin") == _tree(base_out)


# ── S1.2: the shipped set per tier is the pure tier predicate (+ fixed build inputs) ───────────
@pytest.mark.parametrize("tier", ["essential", "convenience", "dev"])
def test_shipped_paths_equal_the_tier_predicate_over_tiers_json(tier, tmp_path):
    manifest = json.loads(TIERS_JSON.read_text())
    expected = {e["path"] for e in manifest["mechanisms"] if TIER_RANK[e["tier"]] <= TIER_RANK[tier]}
    r = _via_sh(tmp_path / "plugin", "--tier", tier, "--multi-agent")
    assert r.returncode == 0, r.stderr
    shipped = json.loads((tmp_path / "plugin" / ".engram-build-manifest.json").read_text())["shipped_paths"]
    assert len(shipped) == len(set(shipped))
    assert set(shipped) == expected | BUILD_INPUT_PATHS


# ── S1.3: the axis cannot come back by accident ─────────────────────────────────────────────
def test_selection_functions_lost_the_parameter():
    from tools.engine import build, manifest
    assert list(inspect.signature(manifest.select_shippables).parameters) == ["manifest", "tier"]
    assert list(inspect.signature(build.select_shippables_with_identity).parameters) == [
        "manifest", "tier", "identity"]
    assert "multi_agent" not in inspect.signature(build.build_plugin).parameters
    assert "multi_agent" not in inspect.signature(build.write_build_manifest).parameters


def test_tiers_json_has_no_multi_agent_doc_key_and_no_entry_carries_one():
    assert "multi_agent" not in TIERS_JSON.read_text()


def _fixture_manifest(tmp_path, bucket, key_value):
    m = {
        "schema_version": 1, "depth_tiers": ["essential", "convenience", "dev"],
        "default_tier": "convenience",
        "mechanisms": [{"path": "ok.py", "tier": "essential"}],
        "excluded": [], "repo_only": [], "build_inputs": [],
    }
    m[bucket].append({"path": "sneaky/path.py", "tier": "dev", "reason": "x", "multi_agent": key_value})
    p = tmp_path / "tiers.json"
    p.write_text(json.dumps(m))
    return p


@pytest.mark.parametrize("bucket", ["mechanisms", "excluded", "repo_only", "build_inputs"])
@pytest.mark.parametrize("value", [True, False])
def test_manifest_gate_refuses_an_entry_carrying_multi_agent_and_names_its_path(bucket, value, tmp_path):
    from tools.engine.manifest import load_manifest
    with pytest.raises(ValueError) as exc:
        load_manifest(str(_fixture_manifest(tmp_path, bucket, value)))
    assert "sneaky/path.py" in str(exc.value)
    assert "multi_agent" in str(exc.value)


def test_manifest_gate_accepts_a_clean_fixture(tmp_path):
    from tools.engine.manifest import load_manifest
    p = _fixture_manifest(tmp_path, "mechanisms", True)
    m = json.loads(p.read_text())
    del m["mechanisms"][1]["multi_agent"]
    p.write_text(json.dumps(m))
    assert len(load_manifest(str(p))["mechanisms"]) == 2


# ── S1.4: no multi_agent in the build manifest or in the log ───────────────────────────────
def test_built_manifest_and_build_log_do_not_mention_the_axis(conv_flagless):
    out, r = conv_flagless
    raw = (out / ".engram-build-manifest.json").read_text()
    assert "multi_agent" not in raw
    assert set(json.loads(raw)) == {"tier", "target", "identity", "shipped_paths"}
    # The file listing still names template.CLAUDE.multi-agent.md (slice 3): look for the axis
    # as a setting (`multi-agent: yes`, `multi_agent=true`), not as a word.
    assert not re.search(r"multi[-_]agent\s*[:=]|--multi-agent", r.stdout + r.stderr, re.I)


def test_write_build_manifest_does_not_write_the_key(tmp_path):
    from tools.engine.build import write_build_manifest
    write_build_manifest(str(tmp_path), "dev", ["a"])
    raw = (tmp_path / ".engram-build-manifest.json").read_text()
    assert "multi_agent" not in raw
    assert json.loads(raw) == {"tier": "dev", "target": "claude-code", "identity": "self", "shipped_paths": ["a"]}


# ── S1.5: flags-match-config compares tier only ──────────────────────────────────────────────
def _flags_ctx(tmp_path, config, built):
    home = tmp_path / "home"
    root = tmp_path / "built"
    home.mkdir()
    root.mkdir()
    (home / "config.json").write_text(json.dumps(config))
    (root / ".engram-build-manifest.json").write_text(json.dumps(built))
    return {"engram_home": str(home), "source_dir": str(tmp_path), "_built_plugin_root": str(root)}


NEW_BUILT = {"tier": "dev", "target": "claude-code", "identity": "self", "shipped_paths": []}


@pytest.mark.parametrize("config,built,passes", [
    ({"install_tier": "dev", "multi_agent": True}, NEW_BUILT, True),
    ({"install_tier": "dev", "mode": "multi"}, NEW_BUILT, True),
    ({"install_tier": "dev"}, {**NEW_BUILT, "multi_agent": True}, True),
    ({"install_tier": "convenience", "multi_agent": True}, NEW_BUILT, False),
], ids=["multi_agent-key", "mode-multi", "old-manifest-field-ignored", "tier-differs"])
def test_flags_match_config_table(config, built, passes, tmp_path):
    from tools.engine.flows import _check_flags_match_config
    ctx = _flags_ctx(tmp_path, config, built)
    assert _check_flags_match_config(ctx) is passes
    if not passes:
        detail = ctx["_last_failure_detail"]
        assert "tier=dev" in detail and "tier=convenience" in detail
        assert "multi_agent" not in detail and "multi-agent" not in detail


# ── S1.6: marketplace-rebuilt reads neither key ──────────────────────────────────────────────
@pytest.mark.parametrize("extra", [{}, {"multi_agent": True}, {"mode": "multi"}, {"multi_agent": "garbage"}],
                         ids=["flagless", "multi_agent", "mode-multi", "wrong-typed"])
def test_marketplace_rebuilt_hands_build_plugin_the_same_arguments_whatever_the_keys(extra, tmp_path, monkeypatch):
    from tools.engine import build, flows
    src = tmp_path / "src"
    (src / "src" / "build" / "packaging").mkdir(parents=True)
    (src / "src" / "build" / "packaging" / "tiers.json").write_text(json.dumps({
        "schema_version": 1, "depth_tiers": ["essential", "convenience", "dev"],
        "default_tier": "convenience", "mechanisms": []}))
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"install_tier": "dev", **extra}))
    seen = {}
    monkeypatch.setattr(build, "build_plugin", lambda **kw: seen.update(kw))
    monkeypatch.setattr(flows, "refresh_and_check_deployable_tag_at", lambda _d: "v0.0.0")
    monkeypatch.setattr(flows, "_current_branch", lambda _d: "dev")
    flows._apply_marketplace_rebuilt({"source_dir": str(src), "engram_home": str(home)})
    assert "multi_agent" not in seen
    assert seen["tier"] == "dev"
    assert sorted(seen) == ["manifest", "plugin_root", "repo_root", "tier"]
