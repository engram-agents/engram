"""#4474: U+FE18 (PRESENTATION FORM FOR VERTICAL RIGHT WHITE LENTICULAR BRAKCET) is a
closing bracket whose Unicode name misspells BRACKET, so the name rule let it through
while its partner U+FE17 was deleted. Every forum_field copy (the PR B library function and
the two monitor-script copies) must treat it as a BRACKET-named character.

Cells: behaviour per copy; a sweep over Ps/Pe code points pinning what the predicate lets
through. Oracles are this file's own literals, nothing is imported from a classifier.
"""
import importlib.util
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent / "src" / "engram"
LIB_PATH = ROOT / "hooks" / "claude" / "_prompthooklib.py"
UPDATES = ROOT / "tools" / "forum-updates-monitor.sh"
MENTION = ROOT / "tools" / "forum-mention-monitor.sh"

FE17, FE18 = "︗", "︘"

# Ps/Pe code points (0x20..0x2FFFF) the predicate lets through, pinned to Unicode 15.0.0:
# the unidata_version of the CPython 3.12 this repo runs on (the plugin venv and the system
# python both report 15.0.0), the version the sources' "163 code points" counts are stated
# for. Other versions get only the version-independent property check below.
PINNED_VERSION = "15.0.0"
PINNED_SURVIVORS = [int(x, 16) for x in """
0028 0029 0F3A 0F3B 0F3C 0F3D 169B 169C 201A 201E 207D 207E 208D 208E 2308 2309 230A 230B
2768 2769 276A 276B 27C5 27C6 27EE 27EF 2985 2986 29D8 29D9 29DA 29DB 2E28 2E29 2E42 2E59
2E5A 2E5B 2E5C 301D 301E 301F FD3E FD3F FE35 FE36 FE59 FE5A FF08 FF09 FF5F FF60
""".split()]  # 52: the issue's 53 minus U+FE18


def _load_lib():
    sys.path.insert(0, str(LIB_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location("_prompthooklib_4474", LIB_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(LIB_PATH.parent))


def _helper_source(script):
    s = script.read_text()
    i = s.index("import unicodedata\n# #1459 PR A")
    end = "if len(s) > cap else s\n"
    return s[i:s.index(end, i) + len(end)]


def _run_helper(script, text):
    code = _helper_source(script) + "\nimport sys\nsys.stdout.write(forum_field(sys.stdin.read(), 10**7))\n"
    r = subprocess.run([sys.executable, "-c", code], input=text, capture_output=True, text=True,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stderr
    return r.stdout


COPIES = {
    "lib": lambda t: _load_lib().forum_field(t, cap=10**7),
    "updates": lambda t: _run_helper(UPDATES, t),
    "mention": lambda t: _run_helper(MENTION, t),
}


@pytest.mark.parametrize("copy", list(COPIES))
def test_fe17_and_fe18_are_both_deleted_parens_kept(copy):
    ff = COPIES[copy]
    assert ff(f"a{FE17}b{FE18}c") == "abc"
    assert ff(f"a{FE18}b") == "ab"
    assert ff("f(x) and (y)") == "f(x) and (y)"


@pytest.mark.parametrize("copy", list(COPIES))
def test_sweep_ps_pe_survivors_are_pinned(copy):
    chars = [chr(cp) for cp in range(0x20, 0x30000) if unicodedata.category(chr(cp)) in ("Ps", "Pe")]
    # U+FFFD marks an ANGLE-named Ps/Pe character that was replaced, not let through
    survivors = [c for c in COPIES[copy]("".join(chars)) if c != "�"]
    # version-independent: no survivor is BRACKET-named or misspelled BRAKCET
    bad = [hex(ord(c)) for c in survivors
           if "BRACKET" in unicodedata.name(c, "") or "BRAKCET" in unicodedata.name(c, "")]
    assert bad == [], bad
    assert FE18 not in survivors
    if unicodedata.unidata_version == PINNED_VERSION:
        assert [ord(c) for c in survivors] == PINNED_SURVIVORS, [hex(ord(c)) for c in survivors]
