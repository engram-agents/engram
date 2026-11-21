"""The file guard compares file BYTES for every guarded file (engram-alpha#4444, CRLF half).

A plain `fg.Item` (no new_text_fn: a config or settings-style file) holding `a\\r\\nb\\r\\n` classified against the text
`a\\nb\\n` is "differs": the old classify read the file through universal newlines and called it "identical".
Both halves are pinned, each alone is weak:
  (i)  without consent the plan refuses, nothing is written and no backup is made;
  (ii) with consent apply writes `a\\nb\\n` (LF) and the CRLF original is in the backup.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "engram"


def _fg():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    import engram_file_guard as fg
    return fg


def _names(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


def test_crlf_file_against_an_lf_text_differs_and_is_refused_without_consent(tmp_path):
    """(i) A CRLF file whose text otherwise matches reads as differing: the plan says so, the refusal names it,
    nothing is written and no backup is made. If it still read as identical (the universal-newline read), the
    plan would be clean and the file would silently stay CRLF while setup reported it as already right."""
    fg = _fg()
    f = tmp_path / "settings-like.conf"
    f.write_bytes(b"a\r\nb\r\n")
    plan = fg.plan([fg.Item(f, "a\nb\n")])
    assert plan.entries[0].state == "differs"
    msg = fg.refusal(plan, fg.NO_CONSENT)
    assert msg is not None and f.name in msg
    try:
        fg.apply(plan, fg.NO_CONSENT)
    except fg.Refused:
        pass
    else:
        raise AssertionError("apply wrote without consent")
    assert f.read_bytes() == b"a\r\nb\r\n" and _names(tmp_path) == [f.name]


def test_crlf_file_with_consent_is_rewritten_with_lf_and_the_crlf_original_is_backed_up(tmp_path):
    """(ii) With consent, apply writes the LF text and the stamped backup holds the CRLF original byte for byte.
    If the comparison still said identical, nothing would be written and no backup would exist."""
    fg = _fg()
    f = tmp_path / "settings-like.conf"
    f.write_bytes(b"a\r\nb\r\n")
    plan = fg.plan([fg.Item(f, "a\nb\n")])
    written = fg.apply(plan, fg.consent_for(False, [str(f)]))
    assert written == [f]
    assert f.read_bytes() == b"a\nb\n"
    backups = [p for p in tmp_path.iterdir() if p.name.startswith(f.name + ".engram-backup.")]
    assert [b.read_bytes() for b in backups] == [b"a\r\nb\r\n"]
