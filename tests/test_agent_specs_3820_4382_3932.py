"""#3820, #4382, #3932: dream-master / dream-fairy spec text contracts.

Contract (written before the spec edits):

* #3820: the dream-master stale-engine check must not open with a bare
  ``${CLAUDE_PLUGIN_ROOT:?...}`` guard (the var is unset in a sub-agent's Bash). It resolves
  the plugin root with the fallback form ``${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/
  marketplace/plugins/engram}`` for both the guard and the ``sys.path`` insert, and still fails
  loudly when the resolved directory lacks ``health/engine.py``.
* #4382: the embeddings repair command resolves its tool path through the same fallback form;
  the venv python stays ``"$HOME/.engram/venv/bin/python3"`` (the venv is fixed by design).
* #3932: the dream-fairy invariant covers ``engram_query`` (``refresh_recall=False``) and
  ``engram_get_subgraph`` (``dream_mode=True``) as well as ``engram_inspect``
  (``dream_mode=True``).
"""

from __future__ import annotations

import re
from pathlib import Path

AGENTS = Path(__file__).resolve().parent.parent / "src" / "engram" / "agents" / "claude"
MASTER = (AGENTS / "engram-dream-master.md").read_text(encoding="utf-8")
FAIRY = (AGENTS / "engram-dream-fairy.md").read_text(encoding="utf-8")

ROOT_FALLBACK = (
    "${CLAUDE_PLUGIN_ROOT:-${ENGRAM_HOME:-$HOME/.engram}/marketplace/plugins/engram}"
)


def _stale_engine_block() -> str:
    # Located by the Python import the block exists to run, NOT by the guard line, so deleting
    # the loud-failure line reddens the loud-failure cell for the right reason.
    i = MASTER.index("from health import engine as he")
    start = MASTER.rfind("```bash", 0, i)
    end = MASTER.index("```", start + 7)
    return MASTER[start:end]


def test_stale_engine_check_has_no_bare_required_guard():
    assert "${CLAUDE_PLUGIN_ROOT:?" not in MASTER


def test_stale_engine_check_uses_fallback_for_guard_and_sys_path():
    block = _stale_engine_block()
    assert ROOT_FALLBACK in block
    # sys.path insert must use the resolved root, not the raw env var
    m = re.search(r"sys\.path\.insert\(0, '([^']*)'\)", block)
    assert m, block
    assert "$CLAUDE_PLUGIN_ROOT" not in m.group(1) or ROOT_FALLBACK in m.group(1)
    assert m.group(1) != "$CLAUDE_PLUGIN_ROOT"


def test_stale_engine_check_still_fails_loud_on_missing_engine():
    block = _stale_engine_block()
    # the guard tests the resolved root's health/engine.py and the failure branch exits non-zero
    assert re.search(
        r'\[ -f "?\$PLUGIN_ROOT/health/engine\.py"? \] \|\| \{[^}]*\bexit 1\b[^}]*\}', block
    ), block
    assert "|| true" not in block


def test_regenerate_embeddings_command_honours_engram_home():
    lines = [l for l in MASTER.splitlines() if "engram-regenerate-embeddings.py" in l and "run `" in l]
    assert lines, "repair command line not found"
    for l in lines:
        assert "~/.engram/marketplace" not in l
        assert f'"{ROOT_FALLBACK}/tools/engram-regenerate-embeddings.py"' in l
        assert '"$HOME/.engram/venv/bin/python3"' in l


def test_no_executable_literal_default_marketplace_path_in_master():
    for l in MASTER.splitlines():
        if "~/.engram/marketplace/plugins" in l and "tools/" in l and "`" in l:
            raise AssertionError(l[:200])


def _invariant() -> str:
    i = FAIRY.index("## Invariants")
    return FAIRY[i : i + 2500]


def _pairs(inv: str, flag: str, tool: str) -> bool:
    return re.search(rf"`{re.escape(flag)}`\s+on\s+every\s+`{tool}`\s+call", inv) is not None


def test_fairy_recall_refresh_rule_pins_each_flag_to_its_tool():
    inv = _invariant()
    assert _pairs(inv, "dream_mode=True", "engram_inspect")
    assert _pairs(inv, "dream_mode=True", "engram_get_subgraph")
    assert _pairs(inv, "refresh_recall=False", "engram_query")
    # the swapped pairings must not appear
    assert not _pairs(inv, "refresh_recall=False", "engram_get_subgraph")
    assert not _pairs(inv, "refresh_recall=False", "engram_inspect")
    assert not _pairs(inv, "dream_mode=True", "engram_query")


def test_fairy_rule_says_query_pattern_never_refreshes():
    inv = _invariant()
    assert re.search(r"`engram_query_pattern`\s+never\s+refreshes\s+recall", inv)
    assert not re.search(r"`engram_query_pattern`\s+always\s+refreshes", inv)
