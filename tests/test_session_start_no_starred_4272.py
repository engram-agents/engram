"""#4272: the session-start ``starred_block`` is deleted (Ariadne's ruling: delete).

Why it went. ``starred_block()`` read ``$ENGRAM_HOME/inter-agent-starred.json``. Nothing in
``src/`` or ``tools/`` ever wrote that file, ``ia`` has no star / starred verb (its
subcommands: list, read, write, mark-read, cursor, status, peers), and when the file did
exist the block printed ``ia read <filename>`` and ``(ia starred to see all)``, neither a
valid command. A dead feature that costs a read-scan root, a test file and a handbook
section is a liability, not a stub.

Contract (all written before the deletion, red on dev):
  1. SOURCE   the session-start hook defines no ``starred_block``, no ``STARRED_CAP`` /
              ``STARRED_STALE_DAYS``, and never names ``inter-agent-starred.json``.
  2. BEHAVIOUR  the REAL hook, run as a subprocess on a scratch ENGRAM_HOME that holds an
              ``inter-agent-starred.json`` with two old-format entries, injects nothing
              starred-related (no "starred", no ``ia read <``, no ``ia starred``), and its
              injected text equals the text from an identical seat WITHOUT the file.
  3. READ-SCAN  ``tools/engramd_read_scan.config.json`` registers no ``starred_block`` root
              (a root naming a deleted function fails pr-checks' read-scan coverage step).
  4. TEST MAP   ``src/engram/packaging/test-map.json`` names no ``test_starred_block.py``
              and that file does not exist.
  5. HANDBOOK   ``docs/handbook/03-hooks.md`` names no ``starred_block`` / ``STARRED_*``;
              its SessionStart section (2) does not claim a starred surface; its PostCompact
              section (5) says what ``engram-postcompact-hook.py`` really does (writes
              ``last-compact-at.json``, emits no additionalContext, still fires the
              ``engram.hook.fire`` event); ``00-inventory.md`` has no row for the deleted
              test file.
  6. NOTHING ELSE READS IT  no file under src/, tools/, tests/ or docs/ (bar this file, the
              PostCompact hook's dated history docstring, the changelog and archived docs)
              names the dead function, constants or file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "src" / "engram" / "hooks" / "claude" / "engram-session-start-hook.py"
POSTCOMPACT = REPO / "src" / "engram" / "hooks" / "claude" / "engram-postcompact-hook.py"
READ_SCAN_CFG = REPO / "tools" / "engramd_read_scan.config.json"
TEST_MAP = REPO / "src" / "engram" / "packaging" / "test-map.json"
HANDBOOK = REPO / "docs" / "handbook" / "03-hooks.md"
INVENTORY = REPO / "docs" / "handbook" / "00-inventory.md"
DELETED_TEST = "tests/test_starred_block.py"

# Built from pieces so this file's own text never names the dead symbols whole.
DEAD_NAMES = ("starred" + "_block", "STARRED" + "_CAP", "STARRED" + "_STALE", "inter-agent-" + "starred")


# ── 1. source ───────────────────────────────────────────────────────────────

def test_1_hook_defines_no_starred_block_function():
    import ast
    tree = ast.parse(HOOK.read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert DEAD_NAMES[0] not in names


def test_1_hook_defines_no_starred_constants():
    import ast
    tree = ast.parse(HOOK.read_text(encoding="utf-8"))
    assigned = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            assigned |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            assigned.add(n.target.id)
    assert not {a for a in assigned if a.startswith("STARRED")}, sorted(assigned)


def test_1_hook_never_names_the_dead_file_or_symbols():
    text = HOOK.read_text(encoding="utf-8")
    for dead in DEAD_NAMES:
        assert dead not in text, dead
    assert "_starred" not in text


# ── 2. behaviour: run the real hook ─────────────────────────────────────────

OLD_FORMAT_ENTRIES = [
    {"filename": "20260101-000000-borges-aaaa.md", "from": "borges",
     "title": "Agreement A", "note": "keep", "starred_at": "2026-01-01T00:00:00Z"},
    {"filename": "20260102-000000-ariadne-bbbb.md", "from": "ariadne",
     "title": "Agreement B", "starred_at": "2026-01-02T00:00:00Z"},
]


def _seat(tmp_path: Path, name: str, *, with_file: bool) -> Path:
    home = tmp_path / name
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"agent_name": "sol"}))
    if with_file:
        (home / ("inter-agent-" + "starred.json")).write_text(json.dumps(OLD_FORMAT_ENTRIES))
    return home


def _context(home: Path, tmp_path: Path, source: str = "startup") -> str:
    env = {**os.environ, "ENGRAM_HOME": str(home), "AGENTS_SHARED_DIR": str(tmp_path / "shared"),
           "LC_ALL": "C"}
    env.pop("ENGRAM_RUNTIME_DIR", None)
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"source": source, "session_id": "t4272"}),
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    assert "Traceback" not in r.stderr, r.stderr[-800:]
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.strip(), "the hook must still inject its other blocks"
    return ctx


def _normalise(ctx: str, home: Path) -> str:
    ctx = ctx.replace(str(home), "<HOME>")
    # the overflow file name carries a UTC stamp and a pid
    ctx = re.sub(r"\d{8}T\d{6}Z-\d+", "<STAMP>", ctx)
    return ctx


def _assert_no_starred(ctx: str) -> None:
    low = ctx.lower()
    assert "starred" not in low, ctx
    assert "ia read <" not in low and "ia read 2026" not in low, ctx
    assert "ia starred" not in low, ctx
    assert "⭐" not in ctx, ctx  # the old block's star glyph


@pytest.mark.parametrize("source", ["startup", "compact"])
def test_2_old_format_starred_file_injects_nothing_starred(tmp_path, source):
    home = _seat(tmp_path, "with", with_file=True)
    # the scratch path (pytest names it after this test) is scrubbed before the scan
    _assert_no_starred(_normalise(_context(home, tmp_path, source), home))


@pytest.mark.parametrize("source", ["startup", "compact"])
def test_2_injected_text_is_the_same_with_and_without_the_file(tmp_path, source):
    with_home = _seat(tmp_path, "with", with_file=True)
    without_home = _seat(tmp_path, "without", with_file=False)
    with_ctx = _context(with_home, tmp_path, source)
    without_ctx = _context(without_home, tmp_path, source)
    _assert_no_starred(_normalise(without_ctx, without_home))
    assert _normalise(with_ctx, with_home) == _normalise(without_ctx, without_home)


# ── 3. read-scan registration ───────────────────────────────────────────────

def test_3_read_scan_config_has_no_starred_root():
    cfg = json.loads(READ_SCAN_CFG.read_text(encoding="utf-8"))
    bad = [r for r in cfg["roots"] if DEAD_NAMES[0] in r]
    assert bad == []
    assert DEAD_NAMES[0] not in json.dumps(cfg["exclusions"])


# ── 4. test map ─────────────────────────────────────────────────────────────

def test_4_test_map_lists_no_deleted_test_file():
    assert "test_starred" + "_block" not in TEST_MAP.read_text(encoding="utf-8")


def test_4_deleted_test_file_does_not_exist():
    assert not (REPO / DELETED_TEST).exists()


# ── 5. handbook ─────────────────────────────────────────────────────────────

def _section(text: str, number: int) -> str:
    m = re.search(rf"^## \*\*{number}\. .*?(?=^## \*\*{number + 1}\. )", text, re.S | re.M)
    assert m, f"section {number} not found"
    return m.group(0)


def test_5_handbook_names_no_dead_symbols():
    text = HANDBOOK.read_text(encoding="utf-8")
    for dead in DEAD_NAMES:
        assert dead not in text, dead


def test_5_sessionstart_section_claims_no_starred_surface():
    assert "starred" not in _section(HANDBOOK.read_text(encoding="utf-8"), 2).lower()


def test_5_postcompact_section_describes_what_the_hook_does():
    sec = _section(HANDBOOK.read_text(encoding="utf-8"), 5)
    low = sec.lower()
    assert "starred" not in low
    assert "last-compact-at.json" in sec
    assert "additionalcontext" in low and ("no additionalcontext" in low
                                           or "cannot inject" in low
                                           or "does not emit" in low)
    assert "_emit_hook_fire" in sec or "engram.hook.fire" in sec
    # ground truth: the hook itself prints no context envelope
    src = POSTCOMPACT.read_text(encoding="utf-8")
    assert "hookSpecificOutput" not in src


def test_5_postcompact_section_does_not_say_a_surface_moved_to_sessionstart():
    # Only the focus list moved (#1732). The starred-letters block was never moved: it was
    # deleted as dead (#4272), so "the continuity surfaces ... moved to SessionStart" is false.
    sec = _section(HANDBOOK.read_text(encoding="utf-8"), 5)
    assert "moved to SessionStart" not in sec


def test_5_postcompact_docstring_says_the_starred_block_was_deleted():
    import ast
    doc = ast.get_docstring(ast.parse(POSTCOMPACT.read_text(encoding="utf-8"))) or ""
    assert "starred" in doc.lower(), "the dated history this cell guards is gone: drop the cell"
    assert "#4272" in doc, "the docstring says starred letters are covered at SessionStart; they are deleted"


def test_5_inventory_has_no_row_for_the_deleted_test_file():
    assert "test_starred" + "_block" not in INVENTORY.read_text(encoding="utf-8")


# ── 6. nothing else reads it ────────────────────────────────────────────────

ALLOWED_HITS = {
    "tests/test_session_start_no_starred_4272.py",          # this file
}


def test_6_no_other_file_names_the_dead_function_constants_or_file():
    pat = re.compile(r"starred_block|inter-agent-starred|STARRED_CAP|STARRED_STALE")
    hits = []
    for top in ("src", "tools", "tests", "docs"):
        for p in sorted((REPO / top).rglob("*")):
            rel = p.relative_to(REPO).as_posix()
            if (not p.is_file() or rel in ALLOWED_HITS or rel == "docs/CHANGELOG.md"
                    or rel.startswith("docs/internal/archive/") or "__pycache__" in rel
                    or p.suffix in {".pyc", ".db", ".sqlite", ".png"}):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    hits.append(f"{rel}:{i}")
    assert hits == [], hits


def test_5_handbook_line_count_for_the_postcompact_hook_is_the_real_one():
    """Luria's colleague read of #4276: the handbook said '165 lines' while the hook, after the docstring
    sentence added here, is 166. A count in prose goes stale unnoticed; if this fails after you edit
    engram-postcompact-hook.py, update the '(N lines)' in docs/handbook/03-hooks.md section 5."""
    sec = _section(HANDBOOK.read_text(encoding="utf-8"), 5)
    m = re.search(r"engram-postcompact-hook\.py \((\d+) lines\)", sec)
    assert m, "section 5 no longer states the hook's line count: drop this cell"
    assert int(m.group(1)) == len(POSTCOMPACT.read_text(encoding="utf-8").splitlines())
