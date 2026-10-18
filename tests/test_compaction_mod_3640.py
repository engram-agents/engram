"""Source-level contract cells for tools/mods/engram-compaction (part of #3640).

The behavioural cells of the mod live in its *.test.ts files and run under
`claude plugin test tools/mods/engram-compaction`. The plugin-test environment has no
file system of its own to read the mod's source with, so the structural contracts that
are about the SOURCE are pinned here:

  M11 hook budget: no mod source calls `$.model` or `$.process`
  M9  fail-open: every `on(...)` registration carries a `.catch` that replays `next(e)`
  M12 the manifest switch is `enabled`, a boolean, default TRUE (Lei, 2026-10-18); off registers nothing
  M13 `session.compact` is registered WITHOUT a matcher (the harness leaves `trigger`
      undefined in a test; the trigger is checked inside the hook)
  M14 the replace/beside choice is ONE exported constant, default beside
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "tools" / "mods" / "engram-compaction"
HOOKS = MOD / "hooks"


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(HOOKS.glob("*.ts")) if ".test." not in p.name and p.name != "kit.ts"}


def test_m11_no_model_or_process_calls():
    srcs = _sources()
    assert srcs, "no mod sources found"
    for name, text in srcs.items():
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(("//", "*", "/*")))
        assert "$.model" not in code, name
        assert "$.process" not in code, name


def test_m9_every_registration_has_a_catch_replaying_next():
    text = (HOOKS / "register.ts").read_text(encoding="utf-8")
    registrations = re.findall(r"^\s*on\(", text, flags=re.MULTILINE)
    catches = re.findall(r"\.catch\(\(\$, e, next\) => next\(e\)\)", text)
    assert len(registrations) == 2
    assert len(catches) == len(registrations)


def test_m12_manifest_switch_defaults_on():
    manifest = json.loads((MOD / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    enabled = manifest["userConfig"]["enabled"]
    assert enabled["type"] == "boolean"
    assert enabled["default"] is True


def test_m12_docs_and_manifest_do_not_still_say_default_off():
    """The switch's default and the words that describe it move together."""
    manifest = (MOD / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    readme = (MOD / "README.md").read_text(encoding="utf-8")
    register = (HOOKS / "register.ts").read_text(encoding="utf-8")
    assert "Default off" not in manifest
    assert "Default OFF" not in readme and "(default false)" not in readme
    assert "Off by default" not in register


def test_m12_off_registers_nothing():
    text = (HOOKS / "register.ts").read_text(encoding="utf-8")
    guard = text.index("options.enabled !== true")
    first_on = text.index("on(")
    assert guard < first_on
    assert re.search(r"options\.enabled !== true\)\s*return", text)


def test_m13_session_compact_has_no_matcher():
    text = (HOOKS / "register.ts").read_text(encoding="utf-8")
    assert re.search(r"on\('session\.compact', \(\$, e, next\)", text)
    assert "trigger:" not in text.split("on('session.compact'", 1)[1].split("\n", 1)[0]


def test_m14_shape_is_one_named_constant_defaulting_to_beside():
    text = (HOOKS / "compaction.ts").read_text(encoding="utf-8")
    assert len(re.findall(r"export const COMPACT_SHAPE: Shape = 'beside'", text)) == 1
    assert "COMPACT_SHAPE" in (HOOKS / "register.ts").read_text(encoding="utf-8")


def test_modules_listed_in_hooks_json_exist():
    spec = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    assert spec["modules"] == ["./register.ts"]
    assert (HOOKS / "register.ts").is_file()
