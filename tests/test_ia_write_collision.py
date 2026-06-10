"""Regression test for same-second ia write filename collision (issue #994).

Two `ia write` calls in the same second must produce two distinct files with
both bodies intact — not one file with the second clobbing the first.

The fix uses os.link instead of os.rename for the atomic publish step, with
a numeric suffix bump (-2, -3, …) on collision.

Mutation check anchor: these tests FAIL against the old os.rename behavior
because os.rename silently overwrites the destination, leaving only one file.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Path to the CLI and tools module
CLI_PATH = Path(__file__).parent.parent / "tools" / "ia.py"
TOOLS_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers (mirrors patterns from test_ia_cli.py)
# ---------------------------------------------------------------------------

def _run_ia(
    engram_home: Path,
    inter_agent_dir: Path,
    *args: str,
    stdin_text: str = "",
    env_overrides: dict | None = None,
) -> tuple:
    """Run the ia CLI as a subprocess; return (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    env["ENGRAM_HOME"] = str(engram_home)
    env["INTER_AGENT_DIR"] = str(inter_agent_dir)
    if env_overrides:
        env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, str(CLI_PATH)] + list(args),
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write_config(engram_home: Path, mode: str = "multi", agent_name: str = "ariadne") -> None:
    cfg = {"mode": mode, "agent_name": agent_name, "counterparts": []}
    (engram_home / "config.json").write_text(json.dumps(cfg))


def _make_letter_content(agent_name: str, ts_str: str, body: str) -> str:
    return (
        "---\n"
        f"from: {agent_name}\n"
        "to: borges\n"
        f"timestamp: {ts_str}\n"
        "---\n\n"
        f"{body}\n"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engram_home(tmp_path: Path) -> Path:
    home = tmp_path / "engram_home"
    home.mkdir()
    return home


@pytest.fixture
def inter_agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / "inter-agent"
    d.mkdir()
    return d


# ===========================================================================
# Core collision regression tests
# ===========================================================================

class TestWriteCollision:
    """Two same-second ia writes produce two distinct files, both bodies intact."""

    def test_same_second_produces_two_distinct_files(self, engram_home, inter_agent_dir):
        """Two ia write calls with a frozen (identical) timestamp produce two .md files.

        MUTATION CHECK: this test FAILS against the old os.rename behavior because
        os.rename silently replaces the first file with the second, leaving only one
        .md file with the second body content.
        """
        _write_config(engram_home)

        # Import the module and patch datetime.now so both calls get the same timestamp.
        # We patch at the tools.ia module level since ia.py uses `datetime.now(timezone.utc)`.
        import importlib
        import types
        # Reload tools.ia as a fresh module to avoid cross-test state
        if "tools.ia" in sys.modules:
            del sys.modules["tools.ia"]
        if "tools" in sys.modules:
            # Don't delete the package if it's a namespace package, just reload ia
            pass

        sys.path.insert(0, str(TOOLS_ROOT))
        import tools.ia as ia_module

        frozen_dt = datetime(2026, 6, 9, 10, 0, 0, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_dt

        body_1 = "First letter body — must survive."
        body_2 = "Second letter body — must not clobber the first."

        ts_str = frozen_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        content_1 = _make_letter_content("ariadne", ts_str, body_1)
        content_2 = _make_letter_content("ariadne", ts_str, body_2)

        config = {"mode": "multi", "agent_name": "ariadne"}

        with patch.object(ia_module, "datetime", _FrozenDatetime):
            # Fake args for cmd_write — minimal Namespace
            import argparse

            def _make_write_args(body_content: str, stdin_content: str) -> None:
                args = argparse.Namespace(
                    to="borges",
                    re=None,
                    reply=None,
                    title=None,
                    body_file=None,
                    from_stdin=True,
                )
                # Redirect stdin
                import io
                old_stdin = sys.stdin
                sys.stdin = io.StringIO(stdin_content)
                try:
                    ia_module.cmd_write(args, config, "ariadne")
                finally:
                    sys.stdin = old_stdin

            # Patch INTER_AGENT_DIR so cmd_write uses our tmp dir
            old_inter = ia_module.INTER_AGENT_DIR
            ia_module.INTER_AGENT_DIR = str(inter_agent_dir)
            try:
                _make_write_args(body_1, content_1)
                _make_write_args(body_2, content_2)
            finally:
                ia_module.INTER_AGENT_DIR = old_inter

        md_files = sorted(inter_agent_dir.glob("*.md"))
        assert len(md_files) == 2, (
            f"Expected 2 distinct .md files after two same-second writes; "
            f"got {len(md_files)}: {[f.name for f in md_files]}. "
            f"COLLISION BUG: if len==1, the second write clobbered the first."
        )

        all_content = "".join(f.read_text() for f in md_files)
        assert body_1 in all_content, (
            f"First letter body not found in written files — was clobbered. "
            f"Files: {[f.name for f in md_files]}"
        )
        assert body_2 in all_content, (
            f"Second letter body not found in written files. "
            f"Files: {[f.name for f in md_files]}"
        )

    def test_collision_suffix_scheme(self, engram_home, inter_agent_dir):
        """First file keeps clean name; second collision gets -2 suffix.

        Verifies the specific suffix scheme: <ts>_<agent>.md, <ts>_<agent>-2.md.
        """
        _write_config(engram_home)

        import importlib
        if "tools.ia" in sys.modules:
            del sys.modules["tools.ia"]
        sys.path.insert(0, str(TOOLS_ROOT))
        import tools.ia as ia_module

        frozen_dt = datetime(2026, 6, 9, 11, 30, 0, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_dt

        ts_str = frozen_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        content_1 = _make_letter_content("ariadne", ts_str, "Body one.")
        content_2 = _make_letter_content("ariadne", ts_str, "Body two.")
        config = {"mode": "multi", "agent_name": "ariadne"}

        import io
        import argparse

        with patch.object(ia_module, "datetime", _FrozenDatetime):
            old_inter = ia_module.INTER_AGENT_DIR
            ia_module.INTER_AGENT_DIR = str(inter_agent_dir)
            try:
                for content in (content_1, content_2):
                    args = argparse.Namespace(
                        to="borges", re=None, reply=None, title=None,
                        body_file=None, from_stdin=True,
                    )
                    old_stdin = sys.stdin
                    sys.stdin = io.StringIO(content)
                    try:
                        ia_module.cmd_write(args, config, "ariadne")
                    finally:
                        sys.stdin = old_stdin
            finally:
                ia_module.INTER_AGENT_DIR = old_inter

        md_files = sorted(inter_agent_dir.glob("*.md"))
        assert len(md_files) == 2, f"Expected 2 files; got {[f.name for f in md_files]}"

        # Expected names based on frozen timestamp
        ts_dashes = frozen_dt.strftime("%Y-%m-%dT%H-%M-%SZ")
        clean_name = f"{ts_dashes}_ariadne.md"
        suffixed_name = f"{ts_dashes}_ariadne-2.md"

        names = {f.name for f in md_files}
        assert clean_name in names, (
            f"Expected clean name '{clean_name}' in {names}"
        )
        assert suffixed_name in names, (
            f"Expected suffixed name '{suffixed_name}' in {names}"
        )

    def test_success_message_reflects_actual_name(
        self, engram_home, inter_agent_dir, capsys
    ):
        """The printed 'sent → <name>' message must show the ACTUAL written filename.

        On collision, the message must show the suffixed name, not the pre-collision
        candidate.
        """
        _write_config(engram_home)

        import importlib
        if "tools.ia" in sys.modules:
            del sys.modules["tools.ia"]
        sys.path.insert(0, str(TOOLS_ROOT))
        import tools.ia as ia_module

        frozen_dt = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        ts_str = frozen_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_dashes = frozen_dt.strftime("%Y-%m-%dT%H-%M-%SZ")

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_dt

        config = {"mode": "multi", "agent_name": "ariadne"}
        import io
        import argparse

        with patch.object(ia_module, "datetime", _FrozenDatetime):
            old_inter = ia_module.INTER_AGENT_DIR
            ia_module.INTER_AGENT_DIR = str(inter_agent_dir)
            try:
                for body_text in ("First body.", "Second body (collision)."):
                    content = _make_letter_content("ariadne", ts_str, body_text)
                    args = argparse.Namespace(
                        to="borges", re=None, reply=None, title=None,
                        body_file=None, from_stdin=True,
                    )
                    old_stdin = sys.stdin
                    sys.stdin = io.StringIO(content)
                    try:
                        ia_module.cmd_write(args, config, "ariadne")
                    finally:
                        sys.stdin = old_stdin
            finally:
                ia_module.INTER_AGENT_DIR = old_inter

        # The second write's output (captured by capsys) must mention the -2 name.
        captured = capsys.readouterr()
        # Both writes printed to stdout — captured.out has both lines
        lines = [ln for ln in captured.out.splitlines() if "sent" in ln]
        assert len(lines) == 2, f"Expected 2 'sent →' lines; got {lines}"

        clean_name = f"{ts_dashes}_ariadne.md"
        suffixed_name = f"{ts_dashes}_ariadne-2.md"

        # First write → clean name
        assert clean_name in lines[0], (
            f"First write should report clean name '{clean_name}'; got: {lines[0]!r}"
        )
        # Second write → suffixed name (NOT the pre-collision clean name)
        assert suffixed_name in lines[1], (
            f"Second write should report suffixed name '{suffixed_name}'; got: {lines[1]!r}"
        )
        assert clean_name not in lines[1], (
            f"Second write should NOT report pre-collision name '{clean_name}' "
            f"when a collision occurred; got: {lines[1]!r}"
        )

    def test_no_tmp_file_remains_after_collision(self, engram_home, inter_agent_dir):
        """After two same-second writes, no .tmp file remains in inter-agent/."""
        _write_config(engram_home)

        import importlib
        if "tools.ia" in sys.modules:
            del sys.modules["tools.ia"]
        sys.path.insert(0, str(TOOLS_ROOT))
        import tools.ia as ia_module

        frozen_dt = datetime(2026, 6, 9, 13, 0, 0, tzinfo=timezone.utc)
        ts_str = frozen_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_dt

        config = {"mode": "multi", "agent_name": "ariadne"}
        import io
        import argparse

        with patch.object(ia_module, "datetime", _FrozenDatetime):
            old_inter = ia_module.INTER_AGENT_DIR
            ia_module.INTER_AGENT_DIR = str(inter_agent_dir)
            try:
                for body_text in ("Body A.", "Body B."):
                    content = _make_letter_content("ariadne", ts_str, body_text)
                    args = argparse.Namespace(
                        to="borges", re=None, reply=None, title=None,
                        body_file=None, from_stdin=True,
                    )
                    old_stdin = sys.stdin
                    sys.stdin = io.StringIO(content)
                    try:
                        ia_module.cmd_write(args, config, "ariadne")
                    finally:
                        sys.stdin = old_stdin
            finally:
                ia_module.INTER_AGENT_DIR = old_inter

        tmp_files = list(inter_agent_dir.glob("*.tmp"))
        assert tmp_files == [], f"No .tmp files should remain; got {tmp_files}"


# ===========================================================================
# Consumer round-trip tests
# ===========================================================================

class TestSuffixedFileConsumers:
    """Confirm that all consumers of letter filenames tolerate -2 suffix names."""

    def test_glob_picks_up_suffixed_file(self, inter_agent_dir):
        """glob('*.md') picks up <ts>_<agent>-2.md files."""
        # Write a suffixed file directly
        suffixed = inter_agent_dir / "2026-06-09T10-00-00Z_ariadne-2.md"
        suffixed.write_text(
            "---\nfrom: ariadne\nto: borges\ntimestamp: 2026-06-09T10:00:00Z\n---\n\nBody.\n"
        )
        md_files = list(inter_agent_dir.glob("*.md"))
        names = [f.name for f in md_files]
        assert "2026-06-09T10-00-00Z_ariadne-2.md" in names, (
            f"glob should pick up suffixed file; got {names}"
        )

    def test_scan_letters_parses_suffixed_file(self, inter_agent_dir):
        """_scan_letters parses a -2 suffixed letter using its frontmatter timestamp."""
        sys.path.insert(0, str(TOOLS_ROOT))
        from tools.ia import _scan_letters

        suffixed = inter_agent_dir / "2026-06-09T10-00-00Z_ariadne-2.md"
        suffixed.write_text(
            "---\nfrom: ariadne\nto: borges\ntimestamp: 2026-06-09T10:00:00Z\n---\n\nSuffixed body.\n"
        )
        letters, skipped = _scan_letters("borges", inter_agent_dir)
        assert skipped == 0, f"No letters should be skipped; skipped={skipped}"
        assert len(letters) == 1, f"Expected 1 letter; got {len(letters)}"
        assert letters[0]["filename"] == "2026-06-09T10-00-00Z_ariadne-2.md"
        assert letters[0]["from"] == "ariadne"

    def test_re_field_roundtrip_with_suffixed_name(self, engram_home, inter_agent_dir):
        """A reply letter with re: pointing at a -2 suffixed file passes validation.

        Confirms the re: existence check in _validate_frontmatter accepts suffixed names.
        """
        _write_config(engram_home)

        # Create the suffixed source letter
        source_filename = "2026-06-09T10-00-00Z_borges-2.md"
        source_path = inter_agent_dir / source_filename
        source_path.write_text(
            "---\nfrom: borges\nto: ariadne\ntimestamp: 2026-06-09T10:00:00Z\n---\n\nSource.\n"
        )

        # Attempt to write a reply referencing the suffixed letter via --re
        now = datetime.now(timezone.utc)
        ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        reply_content = (
            "---\n"
            "from: ariadne\n"
            "to: borges\n"
            f"timestamp: {ts_str}\n"
            f"re: {source_filename}\n"
            "---\n\n"
            "Reply body.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "write", "--to", "borges", "--from-stdin",
            stdin_text=reply_content,
        )
        assert rc == 0, (
            f"Reply referencing a -2 suffixed source letter should be accepted; "
            f"stderr: {stderr}"
        )
        assert "sent" in stdout

    def test_replied_scan_matches_suffixed_re_field(self, inter_agent_dir):
        """_scan_replied_filenames correctly matches re: pointing at a suffixed file.

        When an outgoing letter has re: <ts>_<agent>-2.md, the replied set should
        contain that filename, and the corresponding incoming letter's filename
        (also -2 suffix) should be in the replied set.
        """
        sys.path.insert(0, str(TOOLS_ROOT))
        from tools.ia import _scan_replied_filenames

        incoming_suffixed = "2026-06-09T10-00-00Z_borges-2.md"
        outgoing_filename = "2026-06-09T11-00-00Z_ariadne.md"

        # Create an incoming letter with a -2 suffix
        (inter_agent_dir / incoming_suffixed).write_text(
            "---\nfrom: borges\nto: ariadne\ntimestamp: 2026-06-09T10:00:00Z\n---\n\nIncoming.\n"
        )
        # Create an outgoing reply that references the -2 suffixed letter
        (inter_agent_dir / outgoing_filename).write_text(
            f"---\nfrom: ariadne\nto: borges\ntimestamp: 2026-06-09T11:00:00Z\nre: {incoming_suffixed}\n---\n\nReply.\n"
        )

        replied = _scan_replied_filenames("ariadne", inter_agent_dir)
        assert incoming_suffixed in replied, (
            f"replied set should contain suffixed filename '{incoming_suffixed}'; "
            f"got {replied}"
        )

    def test_cursor_detector_does_not_trip_on_suffixed_filename(self):
        """The filename-format cursor detector regex does NOT falsely match -2 suffixed names
        when those names are not used as cursor values.

        The regex r'T\\d{2}-\\d{2}-\\d{2}' IS present in filenames like
        '2026-06-09T10-00-00Z_ariadne-2.md', but this regex is ONLY applied to
        cursor *values* (timestamps passed to --set or --up-to), never to filenames.
        This test documents the design invariant: filenames are never passed to
        _parse_iso_timestamp.
        """
        import re
        sys.path.insert(0, str(TOOLS_ROOT))
        from tools.ia import _parse_iso_timestamp

        # The cursor detector regex matches the filename-format portion in a suffixed name.
        # This confirms why filenames MUST NOT be passed to _parse_iso_timestamp.
        suffixed_name = "2026-06-09T10-00-00Z_ariadne-2.md"
        assert re.search(r"T\d{2}-\d{2}-\d{2}", suffixed_name), (
            "The detector regex matches a suffixed filename — confirming it would "
            "reject it if mistakenly passed as a cursor value."
        )

        # Confirm _parse_iso_timestamp would reject it (validating the existing guard)
        with pytest.raises(SystemExit) as exc_info:
            _parse_iso_timestamp(suffixed_name, context="test")
        assert exc_info.value.code == 1, (
            "A filename passed to _parse_iso_timestamp should exit 1 "
            "(cursor-format detector fires)"
        )

        # Confirm a real ISO timestamp (what cursors actually store) parses fine
        valid_cursor = "2026-06-09T10:00:00Z"
        dt = _parse_iso_timestamp(valid_cursor, context="test")
        assert dt is not None

    def test_ia_read_suffixed_filename_directly(self, engram_home, inter_agent_dir):
        """ia read <suffixed-filename> works — reads the file by name, advances cursor."""
        _write_config(engram_home)

        suffixed = inter_agent_dir / "2026-06-09T10-00-00Z_borges-2.md"
        suffixed.write_text(
            "---\nfrom: borges\nto: ariadne\ntimestamp: 2026-06-09T10:00:00Z\n---\n\n# Suffixed letter\n\nBody of suffixed letter.\n"
        )
        rc, stdout, stderr = _run_ia(
            engram_home, inter_agent_dir,
            "read", "2026-06-09T10-00-00Z_borges-2.md",
        )
        assert rc == 0, f"ia read on a -2 suffixed file should work; stderr: {stderr}"
        assert "Suffixed letter" in stdout or "Body of suffixed letter" in stdout
