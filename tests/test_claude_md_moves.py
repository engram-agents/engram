"""Nothing-lost gate for the CLAUDE.md prune (#3322).

CLAUDE.md states rules; dated measurement history lives in docs/. Every block
moved out of CLAUDE.md is listed in tools/claude_md_moves.json with its first and
last sentence. tools/claude_md_moves_check.py asserts each of those appears
exactly once in the named doc and zero times in CLAUDE.md.

The real-repo test runs the check on the checked-in mapping. The fixture tests
are the discriminating controls: each plausible-wrong state (sentence still in
CLAUDE.md, duplicated in the doc, absent from the doc, malformed entry) must
fail, so a check that always passes cannot hide behind the real-repo test.
"""

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "claude_md_moves_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("claude_md_moves_check", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture(tmp_path, claude, doc, moves):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "CLAUDE.md").write_text(claude, encoding="utf-8")
    (tmp_path / "docs" / "MOVED.md").write_text(doc, encoding="utf-8")
    (tmp_path / "tools" / "claude_md_moves.json").write_text(
        json.dumps({"moves": moves}), encoding="utf-8"
    )
    return tmp_path


def _move(first="Alpha sentence here.", last="Omega sentence here."):
    return {
        "from_section": "Some section",
        "to_doc": "docs/MOVED.md",
        "first_sentence": first,
        "last_sentence": last,
    }


def test_real_repo_mapping_is_satisfied():
    mod = _load()
    assert mod.check(REPO) == []


def test_real_repo_mapping_is_nonempty():
    data = json.loads((REPO / "tools" / "claude_md_moves.json").read_text(encoding="utf-8"))
    assert len(data["moves"]) > 0


def test_clean_fixture_passes(tmp_path):
    root = _fixture(
        tmp_path,
        "kept rule\n",
        "Alpha sentence here. body Omega sentence here.\n",
        [_move()],
    )
    assert _load().check(root) == []


def test_sentence_still_in_claude_md_fails(tmp_path):
    root = _fixture(
        tmp_path,
        "kept rule Alpha sentence here.\n",
        "Alpha sentence here. Omega sentence here.\n",
        [_move()],
    )
    errs = _load().check(root)
    assert errs and any("CLAUDE.md" in e for e in errs)


def test_sentence_duplicated_in_doc_fails(tmp_path):
    root = _fixture(
        tmp_path,
        "kept\n",
        "Alpha sentence here. x Alpha sentence here. Omega sentence here.\n",
        [_move()],
    )
    errs = _load().check(root)
    assert errs and any("2 times" in e for e in errs)


def test_sentence_absent_from_doc_fails(tmp_path):
    root = _fixture(tmp_path, "kept\n", "Alpha sentence here.\n", [_move()])
    errs = _load().check(root)
    assert errs and any("0 times" in e for e in errs)


def test_missing_doc_fails(tmp_path):
    root = _fixture(tmp_path, "kept\n", "x\n", [dict(_move(), to_doc="docs/NOPE.md")])
    assert _load().check(root)


def test_empty_sentence_fails(tmp_path):
    root = _fixture(tmp_path, "kept\n", "Alpha sentence here.\n", [_move(first="")])
    assert _load().check(root)


def test_missing_key_fails(tmp_path):
    bad = _move()
    del bad["last_sentence"]
    root = _fixture(tmp_path, "kept\n", "Alpha sentence here.\n", [bad])
    assert _load().check(root)


def test_missing_mapping_file_fails(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    assert _load().check(tmp_path)
