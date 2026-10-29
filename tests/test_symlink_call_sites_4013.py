"""#4013 round 3: the `follow_symlinks=False` call site that has a test.

Removing `follow_symlinks=False` there left 837 tests green.  The cell plants a symlink at the write
path, drives the real function, and asserts the link was REPLACED by a regular file holding the new
content while the old target stayed byte-identical.  (The second site, the session-start hook's
counterparts writer, was retired by #4255 slice 2 together with its cell.)
"""

import json
from pathlib import Path

import engram_walguard


def test_write_degraded_marker_replaces_a_planted_symlink(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious, not a marker\n")
    before = victim.read_bytes()
    link = engram_walguard._marker_path(data_dir)
    link.symlink_to(victim)

    returned = engram_walguard.write_degraded_marker(data_dir, {"reason": "planted-link-test"})

    assert Path(returned) == link
    assert not link.is_symlink(), "the planted link was written through, not replaced"
    assert link.is_file()
    assert victim.read_bytes() == before
    assert json.loads(link.read_text())["reason"] == "planted-link-test"
