"""The guard computes a merge from the bytes it reads AT WRITE TIME (engram-alpha#4444 contract B8, stage 1).

Contract-first: written before `fg.Item.new_text_fn` and `engram_claude_block` exist.

  * B8: `fg.Item` gains an optional `new_text_fn(current_text) -> str | raises MergeRefused`. It is called
    with the file's current text ("" when the file is absent). `fg.apply`'s re-plan calls it on the FRESH
    bytes and classifies, backs up and writes THAT text, so a file edited after the check-only pass is
    judged as it is at write time. Forced order, two halves: edit OUTSIDE the block after the check ->
    the edit survives and the block is written; edit INSIDE the block -> apply REFUSES per B9(b),
    nothing written. A change that makes B4 apply (a second BEGIN) -> apply refuses, nothing written.
  * A MergeRefused raised by new_text_fn during apply's re-plan surfaces as ONE REFUSED message
    (`fg.Refused`, the type bootstrap catches), never a traceback.
  * ORDER: apply re-plans FIRST and backs up AFTER, so a raise in the re-plan leaves nothing written by
    apply (a backup that bootstrap took from the first pass stays: copies only).
  * B11 lives inside merge(), so new_text_fn runs it on the bytes of apply's re-plan.
  * The same forced-order cell against the Codex config.toml item (issue 4939): the item bootstrap builds
    carries a new_text_fn too (`test_codex_config_item_keeps_an_edit_made_after_the_check_only_pass_4939`).

While `new_text_fn` or the module is missing, a cell fails with a plain AssertionError naming stage 2.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"

R_OLD = "# Rules\n\nFirst rule.\n\n## Section\n\n- item one\n- item two\n"
R_NEW = R_OLD + "\n## Added\n\nThree more lines here.\n"
TOP = "top note\n\n"
BOTTOM = "\nbottom note\n"
FLAG = "--overwrite-edited-block"
B11_SENTENCE = "this file already holds this install's ENGRAM text outside a block"


def _blk():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    try:
        import engram_claude_block as mod
    except Exception as e:
        raise AssertionError("engram_claude_block is missing: stage 2") from e
    return mod


def _fg():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import engram_file_guard as fg
    return fg


def _current(path: Path) -> str:
    """The file's text the way the contract hands it over: decoded bytes, line endings intact."""
    return path.read_bytes().decode("utf-8") if os.path.lexists(path) else ""


def _item(fg, path: Path, fn, **kw):
    """An Item carrying new_text_fn. `new_text` is the first-pass value (fn on the current bytes), so the
    cell works whether the guard computes the check-only text from the fn or from `new_text`."""
    try:
        return fg.Item(path, fn(_current(path)), new_text_fn=fn, **kw)
    except TypeError as e:
        raise AssertionError("fg.Item has no new_text_fn field: stage 2") from e


def _merge_fn(render: str):
    blk = _blk()

    def fn(cur: str) -> str:
        return blk.merge(cur if cur != "" else None, render).text
    return fn


def _names(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


def _stamped(d: Path) -> list[Path]:
    return sorted(p for p in d.iterdir() if p.name.startswith("CLAUDE.md.engram-backup."))


def _apply_refused(fg, plan, consent, **kw) -> str:
    """apply() must refuse with fg.Refused (what bootstrap.main catches) or SystemExit; anything else
    would reach the user as a traceback."""
    try:
        written = fg.apply(plan, consent, **kw)
    except fg.Refused as e:
        return str(e)
    except SystemExit as e:
        return str(e.code)
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"apply let {type(e).__name__} escape ({e}); bootstrap catches only fg.Refused, "
            f"so the user would see a traceback"
        ) from e
    raise AssertionError(f"apply did not refuse; it wrote {written}")


def _file_with_block(tmp_path: Path):
    blk = _blk()
    p = tmp_path / "CLAUDE.md"
    p.write_bytes((TOP + blk.wrap(R_OLD) + BOTTOM).encode("utf-8"))
    return p


def _edit_inside(text: str, extra: str) -> str:
    lines = text.split("\n")
    i = next(k for k, ln in enumerate(lines) if ln.startswith("<!-- engram:begin"))
    return "\n".join(lines[: i + 2] + extra.split("\n")[:-1] + lines[i + 2:])


# ----------------------------------------------------------------------------------- B8, two halves

def test_B8_a_line_edited_OUTSIDE_the_block_after_the_check_survives_and_the_block_is_written(tmp_path):
    """B8 half 1: check-only pass, then the user edits a line outside the block, then apply: the edit
    survives, the block is the new one, and the backups hold the EDITED bytes. If apply wrote the text it
    computed in the check-only pass (M13), the edit would be gone (a lost update)."""
    blk, fg = _blk(), _fg()
    p = _file_with_block(tmp_path)
    plan = fg.plan([_item(fg, p, _merge_fn(R_NEW), first_identity=True)])
    edited = (TOP.replace("top note", "top note EDITED OUTSIDE") + blk.wrap(R_OLD) + BOTTOM).encode("utf-8")
    p.write_bytes(edited)
    written = fg.apply(plan, fg.consent_for(False, [str(p)]))
    assert p in written
    assert p.read_bytes().decode("utf-8") == TOP.replace("top note", "top note EDITED OUTSIDE") + blk.wrap(R_NEW) + BOTTOM
    assert [b.read_bytes() for b in _stamped(tmp_path)] == [edited]
    assert (tmp_path / "CLAUDE.md.pre-engram").read_bytes() == edited


def test_B8_a_line_edited_INSIDE_the_block_after_the_check_makes_apply_refuse_and_write_nothing(tmp_path):
    """B8 half 2: check-only pass, then someone edits a line inside the block, then apply: apply REFUSES
    per B9(b), as ONE fg.Refused (never a raw MergeRefused traceback), the message carries the user's
    edited line and the flag, the file keeps the edited bytes, and no file appears. If apply wrote the
    stale first-pass text, the edit inside the block would be overwritten without a word."""
    blk, fg = _blk(), _fg()
    p = _file_with_block(tmp_path)
    plan = fg.plan([_item(fg, p, _merge_fn(R_NEW), first_identity=True)])
    edited = _edit_inside(p.read_bytes().decode("utf-8"), "MY OWN EDIT INSIDE\n").encode("utf-8")
    p.write_bytes(edited)
    names = _names(tmp_path)
    msg = _apply_refused(fg, plan, fg.consent_for(False, [str(p)]))
    assert "REFUSED" in msg and p.name in msg, msg
    assert FLAG in msg and "MY OWN EDIT INSIDE" in msg, msg
    assert p.read_bytes() == edited and _names(tmp_path) == names


def test_B8_a_second_BEGIN_added_after_the_check_makes_apply_refuse_in_one_line(tmp_path):
    """B8 (B4 variant): after the check, a second block is pasted in: apply refuses in ONE REFUSED line
    naming the file, writes nothing, takes no backup. If apply had written the first-pass text, ENGRAM
    would have picked one of two blocks."""
    blk, fg = _blk(), _fg()
    p = _file_with_block(tmp_path)
    plan = fg.plan([_item(fg, p, _merge_fn(R_NEW), first_identity=True)])
    doubled = p.read_bytes() + blk.wrap(R_OLD).encode("utf-8")
    p.write_bytes(doubled)
    names = _names(tmp_path)
    msg = _apply_refused(fg, plan, fg.consent_for(False, [str(p)]))
    assert "REFUSED" in msg and p.name in msg
    assert len(msg.strip().splitlines()) == 1, msg
    assert p.read_bytes() == doubled and _names(tmp_path) == names


# ------------------------------------------------------------------------------ ORDER (re-plan first)

def test_B8_ORDER_apply_replans_before_it_backs_up_so_a_refusal_leaves_nothing_behind(tmp_path):
    """B8 ORDER: with no first-pass backups, an inside edit makes apply refuse and the folder is exactly
    as it was: no `.pre-engram`, no stamped copy. If apply backed up BEFORE re-planning, a copy of the
    edited file would be left behind by a run that wrote nothing."""
    fg = _fg()
    p = _file_with_block(tmp_path)
    plan = fg.plan([_item(fg, p, _merge_fn(R_NEW), first_identity=True)])
    edited = _edit_inside(p.read_bytes().decode("utf-8"), "MY OWN EDIT INSIDE\n").encode("utf-8")
    p.write_bytes(edited)
    _apply_refused(fg, plan, fg.consent_for(False, [str(p)]))
    assert _names(tmp_path) == ["CLAUDE.md"], _names(tmp_path)


def test_B8_ORDER_a_first_pass_backup_stays_and_apply_adds_nothing_when_it_refuses(tmp_path):
    """B8 ORDER: a backup that bootstrap.main took from the FIRST pass stays (copies only), and a raise
    in apply's re-plan adds nothing to it. The folder after the refusal equals the folder after
    save_backups, and the saved bytes are the file as it was at the first pass."""
    fg = _fg()
    p = _file_with_block(tmp_path)
    original = p.read_bytes()
    consent = fg.consent_for(False, [str(p)])
    plan = fg.plan([_item(fg, p, _merge_fn(R_NEW), first_identity=True)])
    saved = fg.save_backups(plan, consent)
    names = _names(tmp_path)
    assert (tmp_path / "CLAUDE.md.pre-engram").read_bytes() == original
    edited = _edit_inside(original.decode("utf-8"), "MY OWN EDIT INSIDE\n").encode("utf-8")
    p.write_bytes(edited)
    _apply_refused(fg, plan, consent, saved=saved)
    assert _names(tmp_path) == names, "apply added or removed a file while refusing"
    assert p.read_bytes() == edited
    assert [b.read_bytes() for b in _stamped(tmp_path)] == [original]


# ------------------------------------------------------------------------- what new_text_fn is handed

def test_B8_new_text_fn_is_called_with_the_files_text_line_endings_intact(tmp_path):
    """B8 / B3: the function receives the file's current text with its CR+LF endings intact (B3 keeps the
    user's bytes, so a read that translated CRLF to LF would lose them). The recording fn sees exactly the
    bytes on disk, including the call made at apply's re-plan."""
    fg = _fg()
    p = tmp_path / "CLAUDE.md"
    raw = "a\r\nb\r\n"
    p.write_bytes(raw.encode("utf-8"))
    seen: list[str] = []

    def fn(cur: str) -> str:
        seen.append(cur)
        return cur + "added\n"

    try:
        item = fg.Item(p, raw + "added\n", new_text_fn=fn, first_identity=True)
    except TypeError as e:
        raise AssertionError("fg.Item has no new_text_fn field: stage 2") from e
    plan = fg.plan([item])
    fg.apply(plan, fg.consent_for(False, [str(p)]))
    assert seen, "new_text_fn was never called"
    assert all(s == raw for s in seen), seen
    assert p.read_bytes() == (raw + "added\n").encode("utf-8")


def test_B8_new_text_fn_is_called_with_the_empty_string_for_an_absent_file(tmp_path):
    """B8: 'called with the file's current text, or "" when the file is absent'. An absent file is
    created with exactly what fn returns for ""; a guard that passed None would break fn(str)."""
    fg = _fg()
    p = tmp_path / "CLAUDE.md"
    seen: list = []

    def fn(cur):
        seen.append(cur)
        return "created\n"

    try:
        item = fg.Item(p, "created\n", new_text_fn=fn, first_identity=True)
    except TypeError as e:
        raise AssertionError("fg.Item has no new_text_fn field: stage 2") from e
    written = fg.apply(fg.plan([item]), fg.consent_for(False, []))
    assert p in written and p.read_bytes() == b"created\n"
    assert seen and all(s == "" for s in seen), seen
    assert _names(tmp_path) == ["CLAUDE.md"], "creating an absent file takes no backup"


def test_B7_at_guard_level_a_text_equal_to_the_current_file_is_not_written_or_backed_up(tmp_path):
    """B7 at the guard: when new_text_fn returns the file's own text, apply writes nothing, takes no
    backup and leaves the modification time alone (the 'identical' state)."""
    blk, fg = _blk(), _fg()
    p = tmp_path / "CLAUDE.md"
    p.write_bytes(blk.wrap(R_NEW).encode("utf-8"))
    os.utime(p, (1_000_000_000, 1_000_000_000))
    plan = fg.plan([_item(fg, p, _merge_fn(R_NEW), first_identity=True)])
    written = fg.apply(plan, fg.consent_for(False, [str(p)]))
    assert written == []
    assert _names(tmp_path) == ["CLAUDE.md"] and p.stat().st_mtime_ns == 1_000_000_000 * 10**9


# --------------------------------------------------------------------------- B11 on the re-plan bytes

def test_B11_runs_on_the_replan_bytes(tmp_path):
    """B11 lives INSIDE merge(), so new_text_fn runs it on the bytes of apply's re-plan. Forced order: a
    check-only pass on a markerless file that does NOT hold the render text; then the user pastes the
    render text verbatim; then apply REFUSES per B11 (the contract's sentence), nothing written, no new
    backup. A guard that reused the first-pass text would append a second copy."""
    fg = _fg()
    p = tmp_path / "CLAUDE.md"
    p.write_bytes(b"my notes\n")
    plan = fg.plan([_item(fg, p, _merge_fn(R_NEW), first_identity=True)])
    pasted = b"my notes\n\n" + R_NEW.encode("utf-8")
    p.write_bytes(pasted)
    names = _names(tmp_path)
    msg = _apply_refused(fg, plan, fg.consent_for(False, [str(p)]))
    assert B11_SENTENCE in " ".join(msg.split()), msg
    assert p.read_bytes() == pasted and _names(tmp_path) == names


# ------------------------------------------------------------------ regression guard (green on dev)

def test_B8_regression_an_item_without_new_text_fn_keeps_the_fixed_text_behaviour(tmp_path):
    """B8 regression guard (green on dev): the guard change is general but an Item with a fixed text
    behaves as before: apply re-classifies the CURRENT bytes (backs them up) and writes the fixed text.
    (This is also the old stale-text shape that new_text_fn exists to avoid.)"""
    fg = _fg()
    p = tmp_path / "CLAUDE.md"
    p.write_bytes(b"old\n")
    plan = fg.plan([fg.Item(p, "fixed text\n", first_identity=True)])
    p.write_bytes(b"old\nedited after the check\n")
    fg.apply(plan, fg.consent_for(False, [str(p)]))
    assert p.read_bytes() == b"fixed text\n"
    assert [b.read_bytes() for b in _stamped(tmp_path)] == [b"old\nedited after the check\n"]


# ---------------------------------------------------------------- the Codex config.toml item (issue 4939)

# The probe builds the Codex config.toml item with bootstrap's OWN `_write_items` (the item a real run
# plans), runs the check-only pass, edits the file, then applies. A replica of the item would not see a fix
# in bootstrap.py.
_CODEX_PROBE = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import bootstrap
fg = bootstrap.fg
cfg = Path(sys.argv[2])
ids = dict(bootstrap._FIXED_SEED_IDS)
subs = bootstrap._build_substitutions(ids, quiet=True)
items = bootstrap._write_items(ids, subs, "{}", None, bootstrap._predicted_agents_render())
item = next(it for it in items if it.path == cfg)
plan = fg.plan([item])                                   # the check-only pass
edit = b'\n[plugins.added_after_the_check]\nuser_edit = true\n'   # OUTSIDE ENGRAM's identity keys
cfg.write_bytes(cfg.read_bytes() + edit)
raised = None
try:
    fg.apply(plan, fg.consent_for(False, [str(cfg)]))
except BaseException as e:  # noqa: BLE001
    raised = repr(e)
final = cfg.read_bytes().decode("utf-8")
print(json.dumps({"raised": raised, "final": final, "final_head": final[:120], "final_tail": final[-200:],
                  "backups": sorted(p.name for p in cfg.parent.iterdir() if p.name != cfg.name)}))
"""

FOREIGN_STANZA = '[plugins.someone]\nkey = "value"\n'
ADDED_STANZA = "[plugins.added_after_the_check]\nuser_edit = true\n"


def _codex_probe(tmp_path: Path) -> dict:
    engram_home = tmp_path / ".engram"
    engram_home.mkdir()
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(FOREIGN_STANZA, encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path), "ALPHA_DIR": str(REPO_ROOT), "ALPHA_TEMPLATES_DIR": str(SRC / "templates"),
        "ENGRAM_HOME": str(engram_home), "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"), "TARGET": "codex", "PYTHON_BIN": sys.executable,
        "ENGRAM_NO_EMBEDDINGS": "1",
    })
    r = subprocess.run([sys.executable, "-c", _CODEX_PROBE, str(SRC), str(cfg)],
                       env=env, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:  # a broken probe is not the contract's observable
        raise RuntimeError("the Codex probe itself failed:\n" + r.stdout + r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_codex_config_item_keeps_an_edit_made_after_the_check_only_pass_4939(tmp_path):
    """Issue 4939: an edit made to the Codex config.toml between the check-only pass and apply is present in
    the final bytes, and the foreign stanza that was in the file before the check is still there. If the item
    carried a fixed text computed before apply, apply would write that stale text and the added stanza would
    be gone (a lost update). The item is the one bootstrap builds, not a copy."""
    result = _codex_probe(tmp_path)
    final = result["final"]
    assert result["raised"] is None, result["raised"]
    assert ADDED_STANZA in final, (
        "the user's edit (the `[plugins.added_after_the_check]` stanza with `user_edit = true`, appended after "
        f"the check-only pass) was LOST by apply; backups={result['backups']}; final tail={result['final_tail']!r}"
    )
    assert FOREIGN_STANZA in final, f"the foreign stanza that was there before the check is gone: {final!r}"
    assert final.count('instructions = """') == 1, "the identity keys must appear exactly once"


def test_codex_config_second_run_is_byte_identical_and_takes_no_backup_4939(tmp_path):
    """Issue 4939 (idempotence): after a normal Codex run, a second run over the result leaves config.toml's
    bytes and modification time alone and creates no backup. The older Codex cells count the identity keys
    and the backups (test_bootstrap_codex, the 4440 file) but never compare the bytes; a new_text_fn whose text
    differed from what the first run wrote in even one character would make the second run take a backup and
    rewrite."""
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(FOREIGN_STANZA, encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path), "ALPHA_DIR": str(REPO_ROOT), "ALPHA_TEMPLATES_DIR": str(SRC / "templates"),
        "ENGRAM_HOME": str(tmp_path / ".engram"), "CLAUDE_HOME": str(tmp_path / ".claude"),
        "CODEX_HOME": str(tmp_path / ".codex"), "TARGET": "codex", "PYTHON_BIN": sys.executable,
        "ENGRAM_NO_EMBEDDINGS": "1",
    })

    def run(*args):
        (tmp_path / ".engram").mkdir(exist_ok=True)
        r = subprocess.run([sys.executable, str(SRC / "bootstrap.py"), *args], env=env, capture_output=True,
                           text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr

    run("--force")
    first = cfg.read_bytes()
    os.utime(cfg, (1_000_000_000, 1_000_000_000))
    names = _names(cfg.parent)
    import shutil
    shutil.rmtree(tmp_path / ".engram")
    run()
    assert cfg.read_bytes() == first and FOREIGN_STANZA in first.decode("utf-8")
    assert cfg.stat().st_mtime_ns == 1_000_000_000 * 10**9, "the second run rewrote config.toml"
    assert _names(cfg.parent) == names, "the second run took a backup"


def test_B8_an_unreadable_file_with_a_new_text_fn_is_a_one_line_refusal_not_an_escaping_error(tmp_path):
    """B8 / plan(): a file the guard cannot read (mode 000) must not let an OSError out of the check-only
    pass. The entry is refused with ONE REFUSED line naming the file, nothing is written. Without the catch
    the PermissionError would reach the user as a traceback before any backup or write."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file modes, so an unreadable file cannot be made here")
    fg = _fg()
    p = tmp_path / "CLAUDE.md"
    p.write_bytes(b"my rules\n")
    os.chmod(p, 0)
    try:
        try:
            item = fg.Item(p, "x\n", new_text_fn=lambda cur: cur + "y\n", first_identity=True)
        except TypeError as e:
            raise AssertionError("fg.Item has no new_text_fn field: stage 2") from e
        plan = fg.plan([item])  # must not raise
        msg = fg.refusal(plan, fg.consent_for(False, [str(p)]))
    finally:
        os.chmod(p, 0o644)
    assert msg is not None and "REFUSED" in msg and p.name in msg, msg
    assert len(msg.strip().splitlines()) == 1, msg
    assert p.read_bytes() == b"my rules\n" and _names(tmp_path) == ["CLAUDE.md"]
