"""#4446 step 2b: uninstall removes THIS home's leftover files and links (sections B and C).

Contract (the #4446 thread: the maintainer rulings, the 2b scope correction, and
docs/internal/4446-install-artifacts.md rows B and C). Each removal needs its own ownership proof;
anything else is left and named:

  * C3 `$ENGRAM_HOME/tools`, C4 `$ENGRAM_HOME/hooks`, B16 `$HOME/.local/bin/engram-viz-setup`: removed
    only when a symlink whose target (resolved physically, `/` boundary) lies under this home's
    `marketplace/`. A dangling target is fine. The target itself is never deleted.
  * B15 `$CODEX_HOME/agents/<name>.toml`, only `--target codex`: (a) tree present -> the names in the
    plugin's agents dir (read before the tree goes); (b) tree absent -> `engram-*.toml` containing the
    exact line `[plugins."engram@engram-local".mcp_servers.engram]`. Regular files only.
  * B8 `$CLAUDE_HOME/plugins/cache/engram-local/`, only claude-code, only after the CLI remove, only
    when `known_marketplaces.json` is absent or parses with no `engram-local` key.
  * B3 `cleanupPeriodDays` in `settings.json`: KEPT, never written (since the installer asks the user
    about retention, 36500 may be their own choice, and removing the key would let Claude Code delete old
    transcripts). When the key is present, any value, one Kept line names it.
  * C6 the venv `$HOME/.engram/venv`: only with `--purge-venv`; refused when 2a left another home's
    unit, or when the venv is a symlink.
  * C10-C13, C15 kept and named; B9, B12-B14 named "not removed: host-CLI behaviour unmeasured";
    B17 and B18 named as shared / third-party.
  * The "Nothing to uninstall" path still runs these rows; --dry-run prints each row id and changes
    nothing.

Safety (#3519): every cell runs in a FAKE HOME with the 2a rig (recording stub launchctl / systemctl /
uname) plus stub `claude` / `codex` / `rm` first on PATH. No real host CLI, no real service manager.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from test_uninstall_services_4446 import BASH, UNINSTALL_SH, Rig, _lines, _ok

CODEX_MARKER = '[plugins."engram@engram-local".mcp_servers.engram]'

_CLAUDE_STUB = r"""#!/bin/bash
# recording claude stub: logs argv plus whether the plugin cache dir exists at call time. The
# `plugin marketplace remove` call empties known_marketplaces.json unless told not to / to fail.
S="@STATE@"
H="@HOME@"
c=ABSENT; [ -d "$H/.claude/plugins/cache/engram-local" ] && c=present
echo "$* | cache=$c" >> "$S/claude.log"
if [ "$1 $2 $3" = "plugin marketplace remove" ]; then
  [ -e "$S/claude_fails" ] && exit 1
  if [ ! -e "$S/claude_keeps_listing" ] && [ -f "$H/.claude/plugins/known_marketplaces.json" ]; then
    echo '{}' > "$H/.claude/plugins/known_marketplaces.json"
  fi
fi
exit 0
"""

_CODEX_STUB = r"""#!/bin/bash
echo "$*" >> "@STATE@/codex.log"
exit 0
"""

_RM_STUB = r"""#!/bin/bash
# logs every rm argv, then really removes (exec /bin/rm): lets a cell see HOW a link was removed.
printf '%s\n' "$*" >> "@STATE@/rm.log"
exec /bin/rm "$@"
"""


class FRig(Rig):
    def __init__(self, tmp: Path):
        super().__init__(tmp, "Darwin")
        subs = {"@STATE@": str(self.state), "@HOME@": str(self.home)}
        for name, body in (("claude", _CLAUDE_STUB), ("codex", _CODEX_STUB), ("rm", _RM_STUB)):
            for k, v in subs.items():
                body = body.replace(k, v)
            self._exe(name, body)
        self.claude_home = self.home / ".claude"
        self.codex_home = self.home / ".codex"
        self.mkt = self.eh / "marketplace"
        self.plugin_root = self.mkt / "plugins" / "engram"
        self.venv = self.home / ".engram" / "venv"
        self.cache = self.claude_home / "plugins" / "cache" / "engram-local"
        self.known = self.claude_home / "plugins" / "known_marketplaces.json"
        self.settings = self.claude_home / "settings.json"
        self.agents_dir = self.codex_home / "agents"
        self.viz_link = self.home / ".local" / "bin" / "engram-viz-setup"

    def run(self, *args: str, term_wait: str = "3", extra_env: dict | None = None, skip_cli: bool = True):
        env = {
            "HOME": str(self.home), "ENGRAM_HOME": str(self.eh),
            "CLAUDE_HOME": str(self.claude_home), "CODEX_HOME": str(self.codex_home),
            "PATH": f"{self.bin}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "ENGRAM_UNINSTALL_TERM_WAIT_S": term_wait,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if extra_env:
            env.update(extra_env)
        cmd = [BASH, str(UNINSTALL_SH)] + (["--skip-cli"] if skip_cli else []) + list(args)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)

    def nothing_to_uninstall(self) -> None:
        import shutil
        shutil.rmtree(self.mkt)
        (self.eh / ".deployed-version").unlink()

    # ---- fixtures
    def plant(self, d: Path, name: str = "planted.txt") -> Path:
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text("must survive\n")
        return f

    def link(self, where: Path, target: str | Path) -> Path:
        where.parent.mkdir(parents=True, exist_ok=True)
        where.symlink_to(target)
        return where

    def rm_log(self) -> list[str]:
        p = self.state / "rm.log"
        return p.read_text().splitlines() if p.exists() else []

    def claude_log(self) -> list[str]:
        p = self.state / "claude.log"
        return p.read_text().splitlines() if p.exists() else []

    def snapshot(self, root: Path) -> dict:
        snap = {}
        for dp, dns, fns in os.walk(root, followlinks=False):
            for n in dns + fns:
                p = Path(dp) / n
                rel = str(p.relative_to(root))
                if p.is_symlink():
                    snap[rel] = ("L", os.readlink(p))
                elif p.is_dir():
                    snap[rel] = ("D",)
                else:
                    snap[rel] = ("F", hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(p.stat().st_mode))
        return snap


@pytest.fixture
def rig(tmp_path):
    r = FRig(tmp_path)
    yield r
    r.teardown()


def _block(out: str) -> str:
    """The 'Files and links' summary block (everything after its heading)."""
    assert "Files and links" in out, out
    return out.split("Files and links", 1)[1]


def _block_lines(out: str, *needles: str) -> list[str]:
    return _lines(_block(out), *needles)


# ============================================================= C3 / C4 / B16 : links into the marketplace

LINKS = ("tools", "hooks")


def _link_path(rig: FRig, which: str) -> Path:
    return rig.eh / which


def _row(which: str) -> str:
    return {"tools": "C3", "hooks": "C4"}[which]


@pytest.mark.parametrize("which", LINKS)
def test_link_into_this_homes_marketplace_is_removed_target_untouched_by_us(rig, which):
    target = rig.plugin_root / which
    target.mkdir(parents=True)
    link = rig.link(_link_path(rig, which), target)
    out = _ok(rig.run())
    assert not link.is_symlink() and not link.exists()
    assert _block_lines(out, "Removed:", _row(which)), out


@pytest.mark.parametrize("which", LINKS)
def test_dangling_link_into_this_homes_marketplace_is_removed(rig, which):
    link = rig.link(_link_path(rig, which), rig.plugin_root / which / "does-not-exist-yet")
    assert not link.exists() and link.is_symlink()
    out = _ok(rig.run())
    assert not link.is_symlink()
    assert _block_lines(out, "Removed:", _row(which)), out


@pytest.mark.parametrize("which", LINKS)
def test_relative_link_into_marketplace_is_removed(rig, which):
    (rig.plugin_root / which).mkdir(parents=True)
    link = rig.link(_link_path(rig, which), f"marketplace/plugins/engram/{which}")
    out = _ok(rig.run())
    assert not link.is_symlink()
    assert _block_lines(out, "Removed:", _row(which)), out


@pytest.mark.parametrize("which", LINKS)
def test_dotdot_in_the_target_is_not_provable_and_is_left(rig, which):
    keep = rig.plant(rig.tmp / "outside" / which)
    link = rig.link(_link_path(rig, which), f"{rig.mkt}/../../outside/{which}")
    out = _ok(rig.run())
    assert link.is_symlink() and keep.read_text() == "must survive\n"
    assert _block_lines(out, "Left alone:", _row(which)), out


@pytest.mark.parametrize("which", LINKS)
def test_real_directory_is_left_and_named(rig, which):
    d = rig.eh / which
    keep = rig.plant(d)
    out = _ok(rig.run())
    assert d.is_dir() and not d.is_symlink() and keep.read_text() == "must survive\n"
    assert _block_lines(out, "Left alone:", _row(which), "not a symlink"), out


@pytest.mark.parametrize("which", LINKS)
def test_link_into_a_prefix_sibling_home_marketplace_is_left(rig, which):
    sib = Path(str(rig.eh) + "-work") / "marketplace" / "plugins" / "engram" / which
    keep = rig.plant(sib)
    link = rig.link(_link_path(rig, which), sib)
    out = _ok(rig.run())
    assert link.is_symlink() and keep.read_text() == "must survive\n"
    assert _block_lines(out, "Left alone:", _row(which)), out


@pytest.mark.parametrize("which", LINKS)
def test_link_into_a_sibling_dir_sharing_the_marketplace_prefix_is_left(rig, which):
    # <home>/marketplace-old shares the string "<home>/marketplace" as a prefix but is not inside it
    sib = rig.eh / "marketplace-old" / "plugins" / "engram" / which
    keep = rig.plant(sib)
    link = rig.link(_link_path(rig, which), sib)
    out = _ok(rig.run())
    assert link.is_symlink() and keep.read_text() == "must survive\n"
    assert _block_lines(out, "Left alone:", _row(which)), out


@pytest.mark.parametrize("which", LINKS)
def test_link_elsewhere_is_left_and_target_survives(rig, which):
    keep = rig.plant(rig.tmp / "checkout" / which)
    link = rig.link(_link_path(rig, which), keep.parent)
    out = _ok(rig.run())
    assert link.is_symlink() and keep.read_text() == "must survive\n"
    assert _block_lines(out, "Left alone:", _row(which)), out


@pytest.mark.parametrize("which", LINKS)
def test_link_to_the_marketplace_dir_itself_is_left(rig, which):
    link = rig.link(_link_path(rig, which), rig.mkt)
    out = _ok(rig.run())
    assert _block_lines(out, "Left alone:", _row(which)), out


def test_the_link_is_removed_as_a_link_never_followed_with_a_recursive_rm(rig):
    # Make the marketplace itself a link to a real dir elsewhere: the link's target then survives the
    # tree removal (rm -rf of a symlink removes the symlink only), so a followed delete would show.
    import shutil
    real = rig.tmp / "real-marketplace"
    shutil.move(str(rig.mkt), str(real))
    rig.mkt.symlink_to(real)
    keep = rig.plant(real / "plugins" / "engram" / "tools")
    keep_h = rig.plant(real / "plugins" / "engram" / "hooks")
    c3 = rig.link(rig.eh / "tools", rig.plugin_root / "tools")
    c4 = rig.link(rig.eh / "hooks", str(rig.plugin_root / "hooks") + "/")
    b16 = rig.link(rig.viz_link, rig.plugin_root / "tools" / "operator-setup-viz.sh")
    out = _ok(rig.run())
    assert not c3.is_symlink() and not c4.is_symlink() and not b16.is_symlink(), out
    assert keep.read_text() == "must survive\n" and keep_h.read_text() == "must survive\n"
    for link in (c3, c4, b16):
        for line in rig.rm_log():
            toks = line.split()
            flags = "".join(t for t in toks if t.startswith("-") and t != "--")
            hit = [t for t in toks if t.rstrip("/") == str(link)]
            assert not (hit and ("r" in flags or "R" in flags)), ("recursive rm on a link", line)
            assert not any(t == str(link) + "/" for t in toks), ("trailing slash on a link", line)
        assert any(str(link) in ln for ln in rig.rm_log()), (str(link), rig.rm_log())


def test_viz_link_into_this_homes_marketplace_is_removed(rig):
    target = rig.plugin_root / "tools" / "operator-setup-viz.sh"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    link = rig.link(rig.viz_link, target)
    out = _ok(rig.run())
    assert not link.is_symlink()
    assert _block_lines(out, "Removed:", "B16"), out


def test_viz_link_dangling_into_this_homes_marketplace_is_removed(rig):
    link = rig.link(rig.viz_link, rig.plugin_root / "tools" / "operator-setup-viz.sh")
    out = _ok(rig.run())
    assert not link.is_symlink()
    assert _block_lines(out, "Removed:", "B16"), out


def test_viz_link_that_is_a_regular_file_is_left(rig):
    rig.viz_link.parent.mkdir(parents=True)
    rig.viz_link.write_text("#!/bin/sh\necho mine\n")
    out = _ok(rig.run())
    assert rig.viz_link.read_text() == "#!/bin/sh\necho mine\n"
    assert _block_lines(out, "Left alone:", "B16", "not a symlink"), out


def test_viz_link_into_a_prefix_sibling_marketplace_is_left(rig):
    sib = Path(str(rig.eh) + "-work") / "marketplace" / "tools"
    keep = rig.plant(sib)
    link = rig.link(rig.viz_link, keep)
    out = _ok(rig.run())
    assert link.is_symlink() and keep.exists()
    assert _block_lines(out, "Left alone:", "B16"), out


def test_viz_link_into_a_source_checkout_is_left(rig):
    src = rig.plant(rig.tmp / "checkout" / "tools", "operator-setup-viz.sh")
    link = rig.link(rig.viz_link, src)
    out = _ok(rig.run())
    assert link.is_symlink() and src.exists()
    assert _block_lines(out, "Left alone:", "B16"), out


# ====================================================================================== B15 Codex agents

def _agent(d: Path, name: str, marker: bool = True) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    body = 'name = "x"\n' + (CODEX_MARKER + "\n" if marker else "[mcp_servers.engram]\n")
    p.write_text(body)
    return p


def _plugin_agents(rig: FRig, *names: str) -> None:
    d = rig.plugin_root / "agents"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_text('name = "x"\n[mcp_servers.engram]\n')


def test_b15_tree_present_removes_exactly_the_names_in_the_plugin_agents_dir(rig):
    _plugin_agents(rig, "engram-a.toml", "engram-b.toml", "plain-helper.toml")
    a = _agent(rig.agents_dir, "engram-a.toml")
    b = _agent(rig.agents_dir, "engram-b.toml", marker=False)
    h = _agent(rig.agents_dir, "plain-helper.toml")
    mine = _agent(rig.agents_dir, "engram-mine.toml")  # a user's own file, even carrying the marker
    other = _agent(rig.agents_dir, "other.toml")
    out = _ok(rig.run("--target", "codex"))
    assert not a.exists() and not b.exists() and not h.exists()
    assert mine.exists() and other.exists()
    assert _block_lines(out, "Removed:", "B15", "engram-a.toml"), out
    assert _block_lines(out, "Left alone:", "B15", "engram-mine.toml"), out
    assert not _block_lines(out, "other.toml"), "a non engram-* name outside the list is not even named"


def test_b15_tree_present_symlink_at_a_listed_name_is_left(rig):
    _plugin_agents(rig, "engram-a.toml")
    real = _agent(rig.tmp / "elsewhere", "engram-a.toml")
    rig.agents_dir.mkdir(parents=True)
    link = rig.link(rig.agents_dir / "engram-a.toml", real)
    out = _ok(rig.run("--target", "codex"))
    assert link.is_symlink() and real.exists()
    assert _block_lines(out, "Left alone:", "B15", "engram-a.toml", "symlink"), out


def test_b15_tree_absent_marker_file_removed_unmarked_left_symlink_left(rig):
    rig.nothing_to_uninstall()
    x = _agent(rig.agents_dir, "engram-x.toml")
    y = _agent(rig.agents_dir, "engram-y.toml", marker=False)
    w = rig.agents_dir / "engram-w.toml"
    w.write_text(f"# see {CODEX_MARKER}\n")  # the marker only inside a longer line: not the exact line
    real = _agent(rig.tmp / "elsewhere", "engram-z.toml")
    z = rig.link(rig.agents_dir / "engram-z.toml", real)
    other = _agent(rig.agents_dir, "other.toml")
    out = _ok(rig.run("--target", "codex"))
    assert "Nothing to uninstall" in out
    assert not x.exists()
    assert y.exists() and w.exists() and z.is_symlink() and real.exists() and other.exists()
    assert _block_lines(out, "Removed:", "B15", "engram-x.toml"), out
    for n in ("engram-y.toml", "engram-w.toml", "engram-z.toml"):
        assert _block_lines(out, "Left alone:", "B15", n), (n, out)


def test_b15_without_target_codex_nothing_under_codex_agents_is_touched(rig):
    _plugin_agents(rig, "engram-a.toml")
    _agent(rig.agents_dir, "engram-a.toml")
    _agent(rig.agents_dir, "engram-x.toml")
    before = rig.snapshot(rig.codex_home)
    out = _ok(rig.run())
    assert rig.snapshot(rig.codex_home) == before
    assert "B15" not in _block(out), out


# ================================================================================ B8 plugin cache directory

def _cache(rig: FRig) -> Path:
    f = rig.cache / "engram" / "1.0.0" / "server.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# cached\n")
    (rig.cache / "engram" / "1.0.0" / ".orphaned_at").write_text("1\n")
    return rig.cache


def test_b8_removed_when_known_marketplaces_is_absent(rig):
    c = _cache(rig)
    out = _ok(rig.run())
    assert not c.exists()
    assert _block_lines(out, "Removed:", "B8"), out


def test_b8_removed_when_known_marketplaces_has_no_engram_local(rig):
    c = _cache(rig)
    rig.known.write_text(json.dumps({"other-market": {"source": "x"}}))
    out = _ok(rig.run())
    assert not c.exists()
    assert rig.known.read_text() == json.dumps({"other-market": {"source": "x"}}), "B8 never edits that file"
    assert _block_lines(out, "Removed:", "B8"), out


def test_b8_left_with_reason_when_the_marketplace_is_still_listed(rig):
    c = _cache(rig)
    rig.known.write_text(json.dumps({"engram-local": {}}))
    (rig.state / "claude_keeps_listing").write_text("")  # the CLI remove "succeeds" yet leaves it listed
    out = _ok(rig.run(skip_cli=False))
    assert c.exists()
    assert _block_lines(out, "Left alone:", "B8", "still lists engram-local"), out


def test_b8_left_when_the_cli_remove_failed(rig):
    c = _cache(rig)
    rig.known.write_text(json.dumps({"engram-local": {}}))
    (rig.state / "claude_fails").write_text("")
    out = _ok(rig.run(skip_cli=False))
    assert c.exists()
    assert _block_lines(out, "Left alone:", "B8"), out


def test_b8_left_when_known_marketplaces_is_unparsable(rig):
    c = _cache(rig)
    rig.known.write_text("{ not json")
    out = _ok(rig.run())
    assert c.exists()
    assert _block_lines(out, "Left alone:", "B8", "unparsable"), out


def test_b8_left_when_known_marketplaces_is_not_an_object(rig):
    c = _cache(rig)
    rig.known.write_text("[]")
    out = _ok(rig.run())
    assert c.exists()
    assert _block_lines(out, "Left alone:", "B8"), out


def test_b8_skip_cli_with_the_marketplace_still_listed_is_left(rig):
    c = _cache(rig)
    rig.known.write_text(json.dumps({"engram-local": {}}))
    out = _ok(rig.run())  # --skip-cli
    assert c.exists()
    assert not rig.claude_log()
    assert _block_lines(out, "Left alone:", "B8", "--skip-cli"), out


def test_b8_runs_after_the_cli_remove_and_not_before(rig):
    c = _cache(rig)
    rig.known.write_text(json.dumps({"engram-local": {}}))
    out = _ok(rig.run(skip_cli=False))
    removes = [ln for ln in rig.claude_log() if ln.startswith("plugin marketplace remove engram-local")]
    assert len(removes) == 1 and "cache=present" in removes[0], rig.claude_log()
    assert not c.exists(), out  # the stub emptied known_marketplaces.json, so the cache goes afterwards


def test_b8_not_applied_to_the_codex_target(rig):
    c = _cache(rig)
    out = _ok(rig.run("--target", "codex"))
    assert c.exists()
    assert "B8" not in _block(out)


def test_b8_symlinked_cache_dir_is_left_and_its_target_survives(rig):
    keep = rig.plant(rig.tmp / "real-cache")
    rig.cache.parent.mkdir(parents=True)
    rig.cache.symlink_to(keep.parent)
    out = _ok(rig.run())
    assert rig.cache.is_symlink() and keep.exists()
    assert _block_lines(out, "Left alone:", "B8", "symlink"), out


# =================================================================================== B3 cleanupPeriodDays
# Design (the step owner, 2b round 4): since the installer asks the user about retention and offers 36500,
# a 36500 cannot be told apart from the user's own choice, and removing the key would let Claude Code
# start deleting old transcripts. So uninstall NEVER writes settings.json; it only names the key.

def _settings(rig: FRig, text: str) -> Path:
    rig.settings.parent.mkdir(parents=True, exist_ok=True)
    rig.settings.write_text(text)
    return rig.settings


def _b3_kept(out: str, *more: str) -> list:
    return _block_lines(out, "Kept:", "B3", "cleanupPeriodDays", "may be your own choice",
                        "remove it by hand", "Claude Code's default", *more)


@pytest.mark.parametrize("raw", [
    '{"cleanupPeriodDays": 36500, "k": 1}',
    '{"cleanupPeriodDays": 30, "a": 1}',
    '{"cleanupPeriodDays": "36500"}',
    '{"cleanupPeriodDays": null}',
])
def test_b3_key_present_any_value_is_kept_byte_identical_and_named(rig, raw):
    p = _settings(rig, raw)
    p.chmod(0o640)
    before = p.read_bytes()
    ino = p.stat().st_ino
    out = _ok(rig.run())
    assert p.read_bytes() == before and p.stat().st_ino == ino and stat.S_IMODE(p.stat().st_mode) == 0o640
    assert sorted(x.name for x in p.parent.iterdir()) == ["settings.json"], "no temp file left"
    assert len(_b3_kept(out)) == 1, out
    assert not _block_lines(out, "Removed:", "B3"), out


def test_b3_no_key_is_byte_identical_and_prints_no_b3_line(rig):
    p = _settings(rig, '{"a":1}')
    before = p.read_bytes()
    out = _ok(rig.run())
    assert p.read_bytes() == before
    assert "B3" not in _block(out)


@pytest.mark.parametrize("raw", ["{ nope", ""])
def test_b3_unparsable_is_byte_identical_and_named_as_unreadable(rig, raw):
    p = _settings(rig, raw)
    before = p.read_bytes()
    out = _ok(rig.run())
    assert p.read_bytes() == before
    assert _b3_kept(out, "could not be read"), out


def test_b3_dry_run_is_the_same_as_a_real_run(rig):
    p = _settings(rig, json.dumps({"cleanupPeriodDays": 36500, "k": 1}))
    before = p.read_bytes()
    out = _ok(rig.run("--dry-run"))
    assert p.read_bytes() == before
    assert len(_b3_kept(out)) == 1, out


def test_b3_missing_settings_is_nothing_to_do_and_creates_nothing(rig):
    out = _ok(rig.run())
    assert not rig.settings.exists() and not rig.claude_home.exists()
    assert "B3" not in _block(out)


def test_b3_symlinked_settings_is_never_written_and_the_key_is_still_named(rig):
    real = rig.tmp / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"cleanupPeriodDays": 36500}')
    rig.claude_home.mkdir(parents=True)
    rig.settings.symlink_to(real)
    out = _ok(rig.run())
    assert rig.settings.is_symlink() and real.read_text() == '{"cleanupPeriodDays": 36500}'
    assert len(_b3_kept(out)) == 1, out


# ============================================================================ C6 the venv, and the KEEP rows

def _venv(rig: FRig) -> Path:
    f = rig.venv / "bin" / "marker"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("v\n")
    return rig.venv


def test_c6_kept_and_named_without_the_flag(rig):
    v = _venv(rig)
    out = _ok(rig.run())
    assert v.is_dir()
    assert _block_lines(out, "Kept:", "C6", "--purge-venv"), out


def test_c6_removed_with_the_flag(rig):
    v = _venv(rig)
    out = _ok(rig.run("--purge-venv"))
    assert not v.exists()
    assert (rig.eh / "knowledge.db").read_bytes() == b"graph-bytes"
    assert _block_lines(out, "Removed:", "C6"), out


def test_c6_refused_when_another_homes_unit_was_left_alone(rig):
    v = _venv(rig)
    label = rig.surface_label()
    rig.plist(label, rig.other_eh)
    rig.load(label, rig.other_eh)
    out = _ok(rig.run("--purge-venv"))
    assert v.is_dir()
    assert _block_lines(out, "Left alone:", "C6", "another home"), out


def test_c6_refused_when_another_homes_keyed_unit_is_merely_named(rig):
    import test_uninstall_services_4446 as svc
    v = _venv(rig)
    rig.plist(f"com.engram.surface-daemon.{svc._key(rig.other_eh)}", rig.other_eh)
    out = _ok(rig.run("--purge-venv"))
    assert v.is_dir()
    assert _block_lines(out, "Left alone:", "C6"), out


def test_c6_refused_when_the_venv_is_a_symlink(rig):
    keep = rig.plant(rig.tmp / "real-venv")
    rig.venv.symlink_to(keep.parent)
    out = _ok(rig.run("--purge-venv"))
    assert rig.venv.is_symlink() and keep.exists()
    assert _block_lines(out, "Left alone:", "C6", "symlink"), out


def _path_without(rig: FRig, *drop: str) -> str:
    """A PATH whose directories are the rig's usual ones, minus the named commands (a symlink farm in the
    fake tmp). Deleting a stub is not enough on macOS: /bin/launchctl is real and would be found."""
    farm = rig.tmp / "pathfarm"
    farm.mkdir(exist_ok=True)
    for d in (rig.bin, Path(sys.executable).parent, Path("/usr/bin"), Path("/bin")):
        if not d.is_dir():
            continue
        for e in d.iterdir():
            if e.name in drop or (farm / e.name).is_symlink() or not e.is_file() or not os.access(e, os.X_OK):
                continue
            (farm / e.name).symlink_to(e)
    return str(farm)


def _linux(rig: FRig) -> None:
    rig._exe("uname", '#!/bin/bash\necho "Linux"\n')


@pytest.mark.parametrize("shape", ["mac-this-label", "mac-legacy-label", "linux-this-unit", "linux-legacy-unit"])
def test_c6_refused_when_a_unit_cannot_say_which_home_it_belongs_to(rig, shape):
    # an own-name (or legacy fixed-name) unit with no readable ENGRAM_HOME may be another home's: the
    # shared venv stays. (Pins SVC_FOREIGN=1 in both "could not read its ENGRAM_HOME" branches.)
    v = _venv(rig)
    if shape == "mac-this-label":
        rig.plist(rig.surface_label(), None)
    elif shape == "mac-legacy-label":
        rig.plist("com.engram.surface-daemon", None)
    else:
        _linux(rig)
        rig.unit(rig.surface_unit() if shape == "linux-this-unit" else "engram-surface-daemon.service", None)
    out = _ok(rig.run("--purge-venv"))
    assert v.is_dir() and (v / "bin" / "marker").exists()
    assert _block_lines(out, "Left alone:", "C6", "another home"), out
    assert _lines(out, "could not read its ENGRAM_HOME"), out


def _gone_home(rig: FRig) -> dict:
    # an ENGRAM_HOME that does not exist: the home key cannot be computed
    return {"ENGRAM_HOME": str(rig.tmp / "gone" / ".engram")}


def _refused(rig: FRig, out: str, *reason: str) -> None:
    assert rig.venv.is_dir() and (rig.venv / "bin" / "marker").exists(), out
    assert _block_lines(out, "Left alone:", "C6", "could not check other homes' service units", *reason), out
    assert not _block_lines(out, "Removed:", "C6"), out


def test_c6_refused_when_the_home_key_cannot_be_computed_even_with_another_homes_unit(rig):
    import test_uninstall_services_4446 as svc
    _venv(rig)
    rig.plist(f"com.engram.surface-daemon.{svc._key(rig.other_eh)}", rig.other_eh)
    out = _ok(rig.run("--purge-venv", extra_env=_gone_home(rig)))
    _refused(rig, out, "home key")


def test_c6_refused_when_the_home_key_cannot_be_computed_and_no_other_home_is_present(rig):
    _venv(rig)
    out = _ok(rig.run("--purge-venv", extra_env=_gone_home(rig)))
    _refused(rig, out, "home key")


def test_c6_refused_when_launchctl_is_missing_on_darwin(rig):
    _venv(rig)
    out = _ok(rig.run("--purge-venv", extra_env={"PATH": _path_without(rig, "launchctl")}))
    _refused(rig, out, "launchctl")


def test_c6_refused_when_systemctl_is_missing_on_linux(rig):
    _venv(rig)
    _linux(rig)
    out = _ok(rig.run("--purge-venv", extra_env={"PATH": _path_without(rig, "systemctl")}))
    _refused(rig, out, "systemctl")


def test_c6_refused_on_an_unsupported_os(rig):
    _venv(rig)
    rig._exe("uname", '#!/bin/bash\necho "FreeBSD"\n')
    out = _ok(rig.run("--purge-venv"))
    _refused(rig, out, "unsupported OS", "FreeBSD")


def test_c6_refusal_under_dry_run_is_printed_and_the_venv_stays(rig):
    _venv(rig)
    out = _ok(rig.run("--dry-run", "--purge-venv", extra_env=_gone_home(rig)))
    _refused(rig, out, "home key")


def _unit_dir(rig: FRig, os_name: str) -> Path:
    if os_name == "linux":
        _linux(rig)
        return rig.home / ".config" / "systemd" / "user"
    return rig.home / "Library" / "LaunchAgents"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 directory, so it cannot be made unreadable")
@pytest.mark.parametrize("os_name,mode", [("darwin", 0o000), ("linux", 0o000), ("darwin", 0o300), ("linux", 0o600)])
def test_c6_refused_when_the_unit_directory_cannot_be_listed_or_entered(rig, os_name, mode):
    # an unreadable (or unsearchable) unit dir makes the glob return its literal pattern: that reads as
    # "nothing found" without having looked. 0o300 = no read, 0o600 = no search.
    _venv(rig)
    d = _unit_dir(rig, os_name)
    d.mkdir(parents=True)
    d.chmod(mode)
    try:
        out = _ok(rig.run("--purge-venv"))
    finally:
        d.chmod(0o700)
    _refused(rig, out, f"the unit directory {d} could not be read")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search a mode-000 directory")
def test_c6_refused_when_an_ancestor_of_the_unit_directory_cannot_be_searched(rig):
    # the unit dir is not there to stat because its parent cannot be entered: absent-looking, not absent
    _venv(rig)
    d = _unit_dir(rig, "darwin")
    d.mkdir(parents=True)
    d.parent.chmod(0o000)
    try:
        out = _ok(rig.run("--purge-venv"))
    finally:
        d.parent.chmod(0o700)
    _refused(rig, out, f"the unit directory {d} could not be read")


@pytest.mark.parametrize("os_name", ["darwin", "linux"])
@pytest.mark.parametrize("shape", ["file", "dangling-symlink"])
def test_c6_refused_when_the_unit_directory_path_is_not_a_directory(rig, os_name, shape):
    _venv(rig)
    d = _unit_dir(rig, os_name)
    d.parent.mkdir(parents=True)
    if shape == "file":
        d.write_text("not a directory\n")
    else:
        d.symlink_to(rig.tmp / "nowhere")
    out = _ok(rig.run("--purge-venv"))
    _refused(rig, out, f"the unit directory {d} could not be read")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can list a mode-0300 directory, so it cannot be made unlistable")
@pytest.mark.parametrize("os_name", ["darwin", "linux"])
def test_f1_unlistable_unit_dir_still_stops_and_removes_this_homes_own_units(rig, os_name):
    # F1: the readability guard wraps only the OTHER-homes scan. This home's own keyed units are read by
    # name (search permission is enough), so a 0300 (searchable, not listable) directory must not skip them.
    v = _venv(rig)
    d = _unit_dir(rig, os_name)
    if os_name == "darwin":
        own = rig.plist(rig.surface_label(), rig.eh)
        rig.load(rig.surface_label(), rig.eh)
    else:
        own = rig.unit(rig.surface_unit(), rig.eh)
    d.chmod(0o300)
    try:
        out = _ok(rig.run("--purge-venv"))
        gone = not (d / own.name).exists()
    finally:
        d.chmod(0o700)
    if os_name == "darwin":
        assert [ln for ln in rig.log("launchctl") if ln.startswith("bootout ")], rig.log("launchctl")
    else:
        assert [ln for ln in rig.log("systemctl") if " disable --now " in f" {ln}"], rig.log("systemctl")
    assert gone, f"{own} was not removed: {out}"
    _refused(rig, out, f"the unit directory {d} could not be read")
    assert v.is_dir()


@pytest.mark.parametrize("os_name", ["darwin", "linux"])
def test_c6_absent_unit_directory_is_a_genuine_empty_scan_and_stays_complete(rig, os_name):
    v = _venv(rig)
    d = _unit_dir(rig, os_name)
    assert not d.exists() and not d.is_symlink()
    out = _ok(rig.run("--purge-venv"))
    assert not v.exists(), out
    assert _block_lines(out, "Removed:", "C6"), out


def test_c6_complete_scan_with_no_other_home_removes_and_states_the_bound(rig):
    v = _venv(rig)
    out = _ok(rig.run("--purge-venv"))
    assert not v.exists()
    assert _block_lines(out, "Removed:", "C6", "no service unit cannot be detected", "your statement"), out


def test_c6_flag_alone_does_not_touch_the_services_rules(rig):
    # the viz unit is "left, shared" but is NOT another home's: the purge may go ahead
    v = _venv(rig)
    rig.plist("com.engram.viz-server", None, logs=rig.eh)
    rig.load("com.engram.viz-server", "-")
    _ok(rig.run("--purge-venv"))
    assert not v.exists()


def test_keep_rows_stay_and_are_named_once_each(rig):
    names = {
        "C10": rig.eh / "seed-manifest.json",
        "C11": rig.eh / "first-run-pending",
        "C12": rig.eh / "codex-compact-prompt.md",
    }
    for p in names.values():
        p.write_text("x\n")
    (rig.eh / "install-logs" / "20261107T000000Z").mkdir(parents=True)
    names["C13"] = rig.eh / "install-logs"
    (rig.eh / "knowledge.db.setup.lockdir").mkdir(mode=0o700)
    (rig.eh / "knowledge.db.setup.lockdir" / "holder").write_text("1\n")
    names["C15"] = rig.eh / "knowledge.db.setup.lockdir"
    out = _ok(rig.run())
    for rid, p in names.items():
        assert p.exists(), rid
        assert len(_block_lines(out, "Kept:", rid)) == 1, (rid, out)
    assert (rig.eh / "knowledge.db.setup.lockdir" / "holder").read_text() == "1\n"


def test_unmeasured_host_cli_rows_are_named_and_left(rig):
    b9 = rig.plant(rig.claude_home / "plugins" / "data" / "engram-engram-local")
    cc = rig.codex_home / "plugins" / "cache" / "engram-local" / "engram"
    (cc / "0.1.0").mkdir(parents=True)
    (cc / "0.1.0" / ".mcp.json").write_text("{}")
    (cc / "0.1.0-dev.1.abc").symlink_to(cc / "0.1.0")
    cfg = rig.codex_home / "config.toml"
    cfg.write_text('[plugins."engram@engram-local"]\nenabled = true\n')
    out = _ok(rig.run())
    assert b9.exists() and (cc / "0.1.0" / ".mcp.json").exists() and (cc / "0.1.0-dev.1.abc").is_symlink()
    for rid in ("B9", "B12", "B13", "B14"):
        assert _block_lines(out, "Left alone:", rid, "host-CLI behaviour unmeasured"), (rid, out)


def test_shared_and_third_party_rows_are_named_untouched(rig):
    hf = rig.home / ".cache" / "huggingface" / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2"
    hf.mkdir(parents=True)
    claude_bin = rig.home / ".local" / "bin" / "claude"
    claude_bin.parent.mkdir(parents=True)
    claude_bin.write_text("#!/bin/sh\n")
    out = _ok(rig.run())
    assert hf.is_dir() and claude_bin.exists()
    assert _block_lines(out, "B17"), out
    assert _block_lines(out, "B18"), out


def test_absent_rows_print_nothing(rig):
    out = _ok(rig.run())
    block = _block(out)
    for rid in ("C3", "C4", "C6", "C10", "C11", "C12", "C13", "C15", "B3", "B8", "B9", "B12", "B13", "B14",
                "B15", "B16", "B17", "B18"):
        assert rid not in block, (rid, block)
    assert "(none found)" in block, block


# ========================================================================= the "Nothing to uninstall" path

def test_nothing_to_uninstall_path_still_runs_every_file_row(rig):
    rig.nothing_to_uninstall()
    tools = rig.link(rig.eh / "tools", rig.plugin_root / "tools")
    hooks = rig.link(rig.eh / "hooks", rig.plugin_root / "hooks")
    viz = rig.link(rig.viz_link, rig.plugin_root / "tools" / "operator-setup-viz.sh")
    agent = _agent(rig.agents_dir, "engram-x.toml")
    c = _cache(rig)
    p = _settings(rig, json.dumps({"cleanupPeriodDays": 36500, "k": 1}))
    v = _venv(rig)
    out = _ok(rig.run("--target", "codex", "--purge-venv"))
    assert "Nothing to uninstall" in out
    assert not tools.is_symlink() and not hooks.is_symlink() and not viz.is_symlink()
    assert not agent.exists() and v.exists() is False
    assert c.exists(), "B8 is a claude-code row: a codex run leaves it"
    # and the claude-code run over the same state handles B8 (removed) and B3 (kept, named)
    before = p.read_bytes()
    out = _ok(rig.run())
    assert "Nothing to uninstall" in out
    assert not c.exists()
    assert p.read_bytes() == before
    assert _block_lines(out, "Removed:", "B8"), out
    assert _block_lines(out, "Kept:", "B3"), out


# ================================================================================================= dry run

def test_dry_run_prints_every_row_and_changes_nothing_claude_target(rig):
    _plugin_agents(rig, "engram-a.toml")
    rig.link(rig.eh / "tools", rig.plugin_root / "tools")
    rig.link(rig.eh / "hooks", rig.plugin_root / "hooks")
    rig.link(rig.viz_link, rig.plugin_root / "tools" / "operator-setup-viz.sh")
    _cache(rig)
    rig.known.write_text(json.dumps({"other": {}}))
    _settings(rig, json.dumps({"cleanupPeriodDays": 36500, "k": 1}))
    _venv(rig)
    before = rig.snapshot(rig.home)
    out = _ok(rig.run("--dry-run", "--purge-venv", skip_cli=False))
    assert rig.snapshot(rig.home) == before
    assert not rig.claude_log()
    for rid in ("C3", "C4", "B16", "B8", "C6"):
        assert _block_lines(out, "dry-run", rid), (rid, out)
    assert _block_lines(out, "Kept:", "B3"), out
    assert not _block_lines(out, "Removed:"), out


def test_dry_run_prints_the_codex_agent_row_and_changes_nothing(rig):
    _plugin_agents(rig, "engram-a.toml")
    _agent(rig.agents_dir, "engram-a.toml")
    before = rig.snapshot(rig.home)
    out = _ok(rig.run("--dry-run", "--target", "codex"))
    assert rig.snapshot(rig.home) == before
    assert _block_lines(out, "dry-run", "B15", "engram-a.toml"), out


def test_dry_run_on_the_nothing_to_uninstall_path_changes_nothing(rig):
    rig.nothing_to_uninstall()
    rig.link(rig.eh / "tools", rig.plugin_root / "tools")
    _agent(rig.agents_dir, "engram-x.toml")
    _settings(rig, json.dumps({"cleanupPeriodDays": 36500}))
    before = rig.snapshot(rig.home)
    out = _ok(rig.run("--dry-run", "--target", "codex"))
    assert rig.snapshot(rig.home) == before
    assert _block_lines(out, "dry-run", "C3"), out
    assert _block_lines(out, "dry-run", "B15"), out
    out = _ok(rig.run("--dry-run"))
    assert rig.snapshot(rig.home) == before
    assert _block_lines(out, "Kept:", "B3"), out


def test_dry_run_refused_venv_is_still_named(rig):
    _venv(rig)
    rig.plist(rig.surface_label(), rig.other_eh)
    out = _ok(rig.run("--dry-run", "--purge-venv"))
    assert rig.venv.is_dir()
    assert _block_lines(out, "Left alone:", "C6"), out


# ===================================================================================== bash 3.2 and parsing

def test_usage_mentions_the_new_flag():
    r = subprocess.run([BASH, str(UNINSTALL_SH), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--purge-venv" in r.stdout
