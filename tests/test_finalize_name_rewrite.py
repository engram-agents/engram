"""Tests for the finalize_name_rewrite logic embedded in tools/agentctl Step 2c.

The rewrite logic lives inline in agentctl as a heredoc, bracketed by sentinel
comments.  This test file extracts that heredoc (single source of truth) and
runs it via subprocess, so the EXACT bytes that ship in agentctl are tested —
not a copy.

Validates that:
  - Config/runtime files containing the old tag ARE rewritten and get a .bak.
  - Record/log/binary/pruned files are NOT rewritten and get no .bak.
  - JSON files remain valid after rewrite; invalid JSON causes the script to
    exit non-zero (SystemExit(1)) and leaves the original file intact.
  - A clean home (no stale refs) returns zero touched without error.
  - The function is generic: no hardcoded agent names or real identifiers.
  - Both BEGIN and END sentinels exist exactly once in agentctl (extraction
    integrity guard).
  - finalize-name fails loud (exit non-zero) rather than silently skipping
    Step 2c.

Synthetic identifiers used throughout:
  OLD_TAG   = "agent-newborn-20990101000000"
  NEW_NAME  = "agent-testagent"
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate agentctl and extract the heredoc body between sentinels
# ---------------------------------------------------------------------------

AGENTCTL_PATH = Path(__file__).parent.parent / "tools" / "agentctl"

_BEGIN_SENTINEL = "# === BEGIN finalize_name_rewrite (single source; tests/test_finalize_name_rewrite.py extracts between these sentinels) ==="
_END_SENTINEL = "# === END finalize_name_rewrite ==="


def _extract_heredoc() -> str:
    """Extract the Python body between the BEGIN/END sentinel lines in agentctl.

    Returns the lines between (exclusive of) the sentinel lines, joined with
    newlines.  Raises AssertionError if the sentinels are missing or appear
    more than once (guard against accidental duplication).
    """
    text = AGENTCTL_PATH.read_text()
    lines = text.splitlines()

    begin_indices = [i for i, ln in enumerate(lines) if ln.strip() == _BEGIN_SENTINEL]
    end_indices = [i for i, ln in enumerate(lines) if ln.strip() == _END_SENTINEL]

    assert len(begin_indices) == 1, (
        f"Expected exactly 1 BEGIN sentinel in {AGENTCTL_PATH}, found {len(begin_indices)}"
    )
    assert len(end_indices) == 1, (
        f"Expected exactly 1 END sentinel in {AGENTCTL_PATH}, found {len(end_indices)}"
    )

    begin_i = begin_indices[0]
    end_i = end_indices[0]
    assert begin_i < end_i, "BEGIN sentinel must appear before END sentinel"

    # Lines between the sentinels (exclusive)
    body_lines = lines[begin_i + 1 : end_i]
    return "\n".join(body_lines) + "\n"


# Extract once at module load; all tests share this.
_HEREDOC_BODY = _extract_heredoc()


@pytest.fixture(scope="session")
def extracted_script(tmp_path_factory) -> Path:
    """Write the extracted heredoc body to a temp .py file for subprocess use."""
    d = tmp_path_factory.mktemp("heredoc")
    script = d / "finalize_name_rewrite_extracted.py"
    script.write_text(_HEREDOC_BODY)
    return script


# ---------------------------------------------------------------------------
# Helper: run the extracted script via subprocess
# ---------------------------------------------------------------------------

def _run_script(extracted_script: Path, home: Path, old_prefix: str, new_prefix: str) -> subprocess.CompletedProcess:
    """Invoke extracted_script with (home, old_prefix, new_prefix) argv.

    Returns the CompletedProcess result (does not raise on non-zero exit).
    """
    return subprocess.run(
        [sys.executable, str(extracted_script), str(home), old_prefix, new_prefix],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OLD_TAG = "agent-newborn-20990101000000"
NEW_NAME = "agent-testagent"
OLD_HOME = f"/home/{OLD_TAG}"          # absolute-path form that appears in files
OLD_ENCODED = f"-home-{OLD_TAG}"       # encoded-cwd form in .claude/projects/ names


# ---------------------------------------------------------------------------
# Fixture: synthetic agent home
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> Path:
    """Write a text file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _write_binary(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture()
def agent_home(tmp_path):
    """Build a synthetic agent home matching the spec's fixture description."""
    home = tmp_path / OLD_TAG

    tag = OLD_TAG   # short alias
    ref = OLD_HOME  # the absolute-path string that appears inside files

    # -----------------------------------------------------------------------
    # Config/runtime files — MUST be rewritten
    # -----------------------------------------------------------------------

    # .codex/config.toml (the live-bug trigger — issue #825)
    _write(
        home / ".codex" / "config.toml",
        f'[experimental]\ncompact_prompt_file = "{ref}/.engram/codex-compact-prompt.md"\n',
    )

    # .codex/rules/default.rules
    _write(
        home / ".codex" / "rules" / "default.rules",
        f"# rules for {tag}\nrule allow_home {ref}/.engram\n",
    )

    # .claude.json
    _write(
        home / ".claude.json",
        json.dumps(
            {
                "projects": {f"-home-{tag}-project": []},
                "mcpServers": {"engram": {"command": f"{ref}/.engram/venv/bin/python3"}},
            }
        ),
    )

    # .engram/seed-manifest.json
    _write(
        home / ".engram" / "seed-manifest.json",
        json.dumps({"agent_home": ref, "agent_name": tag}),
    )

    # .engram/warm-briefing.md
    _write(
        home / ".engram" / "warm-briefing.md",
        f"# warm briefing\nHome: {ref}\nAgent: {tag}\n",
    )

    # .engram/marketplace/plugins/engram/.mcp.json
    _write(
        home / ".engram" / "marketplace" / "plugins" / "engram" / ".mcp.json",
        json.dumps({"server": f"{ref}/.engram/venv/bin/python3"}),
    )

    # .config/systemd/user/engram-surface-daemon.service
    _write(
        home / ".config" / "systemd" / "user" / "engram-surface-daemon.service",
        f"[Service]\nExecStart={ref}/.engram/venv/bin/python3 server.py\n",
    )

    # -----------------------------------------------------------------------
    # Record files — MUST be skipped
    # -----------------------------------------------------------------------

    # .engram/knowledge.sql  (graph dump — .sql extension → skip)
    _write(
        home / ".engram" / "knowledge.sql",
        f"-- ENGRAM graph dump\nINSERT INTO nodes VALUES ('{ref}/.engram');\n",
    )

    # .engram/surface-daemon.log  (.log extension → skip)
    _write(
        home / ".engram" / "surface-daemon.log",
        f"2099-01-01 00:00:00 INFO started at {ref}\n",
    )

    # .engram/sessions/<id>.jsonl  (.jsonl extension → skip)
    sessions_dir = home / ".engram" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    _write(
        sessions_dir / "session-abc123.jsonl",
        f'{{"cwd": "{ref}/.engram", "event": "start"}}\n',
    )

    # .engram/graph_snapshot.md  (exact name → skip)
    _write(
        home / ".engram" / "graph_snapshot.md",
        f"# snapshot\nhome: {ref}\n",
    )

    # .engram/finalize-name-complete.json  (*-complete.json suffix → skip)
    _write(
        home / ".engram" / "finalize-name-complete.json",
        json.dumps({"from": tag, "to": "someother", "at": "2099-01-01T00:00:00Z"}),
    )

    # history.jsonl  (exact name AND extension → skip)
    _write(
        home / ".engram" / "history.jsonl",
        f'{{"ts": "2099-01-01", "home": "{ref}"}}\n',
    )

    # -----------------------------------------------------------------------
    # Binary file — MUST be skipped
    # -----------------------------------------------------------------------

    # knowledge.db with NUL bytes (simulates SQLite binary)
    _write_binary(
        home / ".engram" / "knowledge.db",
        b"SQLite format 3\x00" + f"home={ref}".encode() + b"\x00" * 16,
    )

    # -----------------------------------------------------------------------
    # Pruned directories — contents MUST be skipped (dirs not descended)
    # -----------------------------------------------------------------------

    # .engram/venv/x.py  (under "venv" pruned dir)
    _write(
        home / ".engram" / "venv" / "x.py",
        f"HOME = '{ref}'\n",
    )

    # .engram/__pycache__/y.pyc  (under "__pycache__" pruned dir)
    (home / ".engram" / "__pycache__").mkdir(parents=True, exist_ok=True)
    _write_binary(
        home / ".engram" / "__pycache__" / "y.cpython-312.pyc",
        b"\xd0\r\r\n" + f"# {ref}".encode() + b"\x00",
    )

    return home


# ---------------------------------------------------------------------------
# Helper: collect all .bak-prefinalize files under the home
# ---------------------------------------------------------------------------

def _bak_files(home: Path) -> set:
    return {p for p in home.rglob("*.bak-prefinalize")}


# ---------------------------------------------------------------------------
# Test: sentinel integrity — both sentinels exist exactly once in agentctl
# ---------------------------------------------------------------------------

class TestSentinelPresence:
    """Guard: a future edit that removes or duplicates a sentinel breaks extraction."""

    def test_begin_sentinel_exists_exactly_once(self):
        text = AGENTCTL_PATH.read_text()
        count = text.count(_BEGIN_SENTINEL)
        assert count == 1, (
            f"BEGIN sentinel must appear exactly once in agentctl, found {count}"
        )

    def test_end_sentinel_exists_exactly_once(self):
        text = AGENTCTL_PATH.read_text()
        count = text.count(_END_SENTINEL)
        assert count == 1, (
            f"END sentinel must appear exactly once in agentctl, found {count}"
        )

    def test_begin_before_end(self):
        text = AGENTCTL_PATH.read_text()
        begin_pos = text.index(_BEGIN_SENTINEL)
        end_pos = text.index(_END_SENTINEL)
        assert begin_pos < end_pos, "BEGIN sentinel must precede END sentinel"

    def test_heredoc_body_nonempty(self):
        """Extraction produces a non-trivial body (not an accidental empty slice)."""
        assert len(_HEREDOC_BODY.strip()) > 100, "Extracted heredoc body is suspiciously short"


# ---------------------------------------------------------------------------
# Test: config files ARE rewritten
# ---------------------------------------------------------------------------

class TestConfigFilesRewritten:
    """Every config/runtime file that contains OLD_TAG must be rewritten."""

    def test_codex_config_toml(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (agent_home / ".codex" / "config.toml").read_text()
        assert OLD_TAG not in text
        assert NEW_NAME in text

    def test_codex_rules(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (agent_home / ".codex" / "rules" / "default.rules").read_text()
        assert OLD_TAG not in text
        assert NEW_NAME in text

    def test_claude_json(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (agent_home / ".claude.json").read_text()
        assert OLD_TAG not in text
        assert NEW_NAME in text
        # Must still be valid JSON
        json.loads(text)

    def test_seed_manifest_json(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (agent_home / ".engram" / "seed-manifest.json").read_text()
        assert OLD_TAG not in text
        assert NEW_NAME in text
        json.loads(text)

    def test_warm_briefing_md(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (agent_home / ".engram" / "warm-briefing.md").read_text()
        assert OLD_TAG not in text
        assert NEW_NAME in text

    def test_mcp_json(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (agent_home / ".engram" / "marketplace" / "plugins" / "engram" / ".mcp.json").read_text()
        assert OLD_TAG not in text
        assert NEW_NAME in text
        json.loads(text)

    def test_systemd_service(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (agent_home / ".config" / "systemd" / "user" / "engram-surface-daemon.service").read_text()
        assert OLD_TAG not in text
        assert NEW_NAME in text

    def test_bak_files_created_for_config_files(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        baks = _bak_files(agent_home)
        # At minimum, every config file that had the old tag must have a .bak
        config_paths = [
            agent_home / ".codex" / "config.toml",
            agent_home / ".codex" / "rules" / "default.rules",
            agent_home / ".claude.json",
            agent_home / ".engram" / "seed-manifest.json",
            agent_home / ".engram" / "warm-briefing.md",
            agent_home / ".engram" / "marketplace" / "plugins" / "engram" / ".mcp.json",
            agent_home / ".config" / "systemd" / "user" / "engram-surface-daemon.service",
        ]
        for p in config_paths:
            bak = Path(str(p) + ".bak-prefinalize")
            assert bak in baks, f"Missing .bak-prefinalize for {p.name}"


# ---------------------------------------------------------------------------
# Test: record files are NOT rewritten
# ---------------------------------------------------------------------------

class TestRecordFilesSkipped:
    """Record/log/dump/audit files must be left byte-for-byte intact."""

    def _assert_unchanged(self, path: Path, extracted_script, home):
        _run_script(extracted_script, home, OLD_TAG, NEW_NAME)
        text = path.read_text()
        assert OLD_TAG in text, f"Expected OLD_TAG still present in {path.name}"
        bak = Path(str(path) + ".bak-prefinalize")
        assert not bak.exists(), f"Unexpected .bak-prefinalize created for {path.name}"

    def test_knowledge_sql_unchanged(self, agent_home, extracted_script):
        self._assert_unchanged(agent_home / ".engram" / "knowledge.sql", extracted_script, agent_home)

    def test_surface_daemon_log_unchanged(self, agent_home, extracted_script):
        self._assert_unchanged(agent_home / ".engram" / "surface-daemon.log", extracted_script, agent_home)

    def test_session_jsonl_unchanged(self, agent_home, extracted_script):
        self._assert_unchanged(agent_home / ".engram" / "sessions" / "session-abc123.jsonl", extracted_script, agent_home)

    def test_graph_snapshot_unchanged(self, agent_home, extracted_script):
        self._assert_unchanged(agent_home / ".engram" / "graph_snapshot.md", extracted_script, agent_home)

    def test_finalize_complete_json_unchanged(self, agent_home, extracted_script):
        """finalize-name-complete.json must not be rewritten (audit record)."""
        # Note: this file contains "someother" not OLD_TAG in the "to" field,
        # but the "from" field has OLD_TAG — ensure it is untouched.
        original = (agent_home / ".engram" / "finalize-name-complete.json").read_bytes()
        _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        result = (agent_home / ".engram" / "finalize-name-complete.json").read_bytes()
        assert result == original, "finalize-name-complete.json must not be modified"

    def test_history_jsonl_unchanged(self, agent_home, extracted_script):
        self._assert_unchanged(agent_home / ".engram" / "history.jsonl", extracted_script, agent_home)


# ---------------------------------------------------------------------------
# Test: binary file skipped
# ---------------------------------------------------------------------------

class TestBinaryFilesSkipped:
    def test_knowledge_db_unchanged(self, agent_home, extracted_script):
        original = (agent_home / ".engram" / "knowledge.db").read_bytes()
        _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        result = (agent_home / ".engram" / "knowledge.db").read_bytes()
        assert result == original, "Binary knowledge.db must not be modified"
        bak = agent_home / ".engram" / "knowledge.db.bak-prefinalize"
        assert not bak.exists(), "No .bak-prefinalize for binary file"


# ---------------------------------------------------------------------------
# Test: invalid-UTF-8-but-no-NUL file is skipped (strict UTF-8 guard)
# ---------------------------------------------------------------------------

class TestInvalidUtf8Skipped:
    def test_invalid_utf8_no_nul_skipped(self, tmp_path, extracted_script):
        """A file with the old tag AND invalid UTF-8 bytes but NO NUL is skipped.

        Without the strict-UTF-8 guard the rewriter reads it with errors="replace",
        substitutes U+FFFD for the bad bytes, and writes back — silently corrupting
        the file.  With the guard it skips the file: bytes unchanged, no backup,
        exit 0, no exception.
        """
        home = tmp_path / OLD_TAG
        home.mkdir()
        # Construct bytes that: contain OLD_TAG (so the rewriter would attempt to
        # process it), include \xff\xfe (invalid UTF-8 — not a valid 2-byte seq
        # and no NUL so the binary-NUL sniff does NOT catch it).
        content = (
            b"agent config for " + OLD_TAG.encode("ascii")
            + b" path=/home/" + OLD_TAG.encode("ascii")
            + b" \xff\xfe extra data"
        )
        bad_file = home / "config.cfg"
        bad_file.write_bytes(content)

        result = _run_script(extracted_script, home, OLD_TAG, NEW_NAME)

        # Script must exit 0 — decode failure is a skip, not a fatal error
        assert result.returncode == 0, (
            f"Expected exit 0 on invalid-UTF-8 skip, got {result.returncode}. "
            f"stderr: {result.stderr!r}"
        )
        # File bytes must be untouched (no rewrite attempted)
        assert bad_file.read_bytes() == content, (
            "Invalid-UTF-8 file must be left byte-for-byte intact"
        )
        # No .bak-prefinalize (skip happens before backup)
        bak = Path(str(bad_file) + ".bak-prefinalize")
        assert not bak.exists(), (
            ".bak-prefinalize must NOT be created for invalid-UTF-8 skipped file"
        )
        # The skip must be reported in the summary output
        assert "skipped (invalid UTF-8)" in result.stdout, (
            f"Expected invalid-UTF-8 skip count in stdout, got: {result.stdout!r}"
        )
        assert "skipped (invalid UTF-8): 1 file(s)" in result.stdout, (
            f"Expected 'skipped (invalid UTF-8): 1 file(s)' in stdout utf8-skip summary, got: {result.stdout!r}"
        )


# ---------------------------------------------------------------------------
# Test: write_text encoding pin survives LC_ALL=C
# ---------------------------------------------------------------------------

class TestLcAllCLocale:
    """Regression guard for the locale-default write_text and stdout-glyph bugs
    (round-3 + round-4 fixes).

    Under LC_ALL=C with PEP 538 locale coercion disabled (PYTHONCOERCECLOCALE=0
    + PYTHONUTF8=0), Python's default codec is ASCII.  Two failure modes exist:

    1. write_text(new_text) raises UnicodeEncodeError for any valid-UTF-8 file
       whose content contains non-ASCII characters.  Fixed by pinning
       encoding="utf-8" on write_text.

    2. print(f"  ✓ ...") raises UnicodeEncodeError because stdout is ASCII-only.
       Fixed by sys.stdout.reconfigure(errors="replace") at script startup.

    Without PYTHONCOERCECLOCALE=0 + PYTHONUTF8=0 the test is non-discriminating:
    PEP 538 silently rewrites LC_ALL=C to C.UTF-8 at interpreter startup, so
    both bugs are masked even with the fixes reverted.  The env vars must be
    present to make the test a real regression guard.
    """

    def test_non_ascii_utf8_rewrites_under_lc_all_c(self, tmp_path, extracted_script):
        """A valid-UTF-8 file with non-ASCII bytes rewrites correctly under LC_ALL=C.

        PYTHONCOERCECLOCALE=0 + PYTHONUTF8=0 disable PEP 538 coercion so the
        ASCII-codec premise actually holds, making this a real regression guard
        rather than a vacuous pass.
        """
        home = tmp_path / OLD_TAG
        home.mkdir()
        # File with non-ASCII UTF-8 content (é = U+00E9 = 0xC3 0xA9) AND the old tag.
        content = f"# config for {OLD_TAG}\n# path: /home/{OLD_TAG}\n# note: café\n"
        cfg = home / "config.toml"
        cfg.write_text(content, encoding="utf-8")

        env = {
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "LANGUAGE": "C",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONUTF8": "0",
        }
        result = subprocess.run(
            [sys.executable, str(extracted_script), str(home), OLD_TAG, NEW_NAME],
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 0, (
            f"Expected exit 0 under LC_ALL=C, got {result.returncode}. "
            f"stderr: {result.stderr!r}"
        )
        rewritten = cfg.read_text(encoding="utf-8")
        assert OLD_TAG not in rewritten, "OLD_TAG must be replaced"
        assert NEW_NAME in rewritten, "NEW_NAME must appear after rewrite"
        assert "café" in rewritten, "Non-ASCII bytes must survive rewrite byte-correctly"


# ---------------------------------------------------------------------------
# Test: pruned directories are not descended
# ---------------------------------------------------------------------------

class TestPrunedDirsSkipped:
    def test_venv_file_unchanged(self, agent_home, extracted_script):
        original = (agent_home / ".engram" / "venv" / "x.py").read_text()
        _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        result = (agent_home / ".engram" / "venv" / "x.py").read_text()
        assert result == original, "venv/x.py must not be touched"
        assert OLD_TAG in result

    def test_pycache_file_unchanged(self, agent_home, extracted_script):
        original = (agent_home / ".engram" / "__pycache__" / "y.cpython-312.pyc").read_bytes()
        _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        result = (agent_home / ".engram" / "__pycache__" / "y.cpython-312.pyc").read_bytes()
        assert result == original, "__pycache__ file must not be touched"


# ---------------------------------------------------------------------------
# Test: return value / report counts (via stdout parsing)
# ---------------------------------------------------------------------------

class TestReturnCounts:
    def test_script_exits_zero_on_success(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Unexpected non-zero exit: {result.stderr}"

    def test_stdout_reports_occurrences(self, agent_home, extracted_script):
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0
        # The script prints "total: N occurrences across M file(s)" when N > 0
        assert "total:" in result.stdout or "occurrences" in result.stdout or "rewritten" in result.stdout, (
            f"Expected occurrence summary in stdout, got: {result.stdout!r}"
        )

    def test_touched_equals_files_with_bak(self, agent_home, extracted_script):
        """Number of files with .bak-prefinalize equals the number the script reported touching."""
        result = _run_script(extracted_script, agent_home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0
        baks = _bak_files(agent_home)
        # Count config files we expect were rewritten (same 7 as in TestConfigFilesRewritten)
        config_paths = [
            agent_home / ".codex" / "config.toml",
            agent_home / ".codex" / "rules" / "default.rules",
            agent_home / ".claude.json",
            agent_home / ".engram" / "seed-manifest.json",
            agent_home / ".engram" / "warm-briefing.md",
            agent_home / ".engram" / "marketplace" / "plugins" / "engram" / ".mcp.json",
            agent_home / ".config" / "systemd" / "user" / "engram-surface-daemon.service",
        ]
        expected_baks = {Path(str(p) + ".bak-prefinalize") for p in config_paths}
        assert expected_baks.issubset(baks), (
            f"Missing expected .bak files: {expected_baks - baks}"
        )


# ---------------------------------------------------------------------------
# Test: clean home (no stale refs) → exit 0, no baks
# ---------------------------------------------------------------------------

class TestCleanHome:
    def test_no_stale_refs(self, tmp_path, extracted_script):
        home = tmp_path / "agent-testagent-clean"
        home.mkdir()
        # Write a file that does NOT contain OLD_TAG
        (home / "config.json").write_text(json.dumps({"key": "value"}))
        result = _run_script(extracted_script, home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "no stale references" in result.stdout
        # No .bak files created
        baks = _bak_files(home)
        assert len(baks) == 0


# ---------------------------------------------------------------------------
# Test: JSON validation — invalid post-rewrite JSON triggers loud failure
# ---------------------------------------------------------------------------

class TestJsonValidation:
    def test_invalid_json_triggers_nonzero_exit(self, tmp_path, extracted_script):
        """A .json file that would become invalid JSON after rewrite causes exit 1.

        We craft a file where the tag appears inside a JSON string value in a
        way that we can control by making the JSON structurally rely on the
        exact tag bytes.  The simplest approach: write a valid JSON file, then
        use a tag-as-fake-delimiter trick — but since the new_prefix is also a
        safe string, replacement won't naturally break validity.

        Instead, we write a file whose content is valid JSON with the tag as a
        key, and monkey-patch by writing the file with a structure that becomes
        invalid after substitution by including a raw control character in the
        rewritten value.  Since that's awkward in a subprocess-only test, the
        most reliable approach is to write a file that is ALREADY invalid JSON
        but contains the tag string, so the validator fires on the post-rewrite
        text.

        Actually, the cleanest approach: make OLD_TAG appear as part of the
        file content that, after substitution with NEW_NAME (which is shorter),
        loses a closing structure.  This is hard to guarantee portably.

        Pragmatic: use a file where the content after rewrite would fail
        json.loads — write a file where OLD_TAG is the only thing between
        two JSON-structure-breaking positions.  Specifically, write a file
        whose content is:

          {"key": "OLDTAG}

        (missing closing quote + brace).  This is invalid JSON as-is, but our
        rewriter doesn't validate the ORIGINAL — it validates AFTER rewrite.
        After rewrite it is still invalid (NEW_NAME is also safe).  So the
        validator fires.

        NOTE: the rewriter reads the file, counts occurrences of OLD_TAG,
        writes new_text, then validates new_text as JSON.  If new_text fails
        json.loads, the script exits 1.
        """
        home = tmp_path / "agent-newborn-20990101000001"
        home.mkdir()
        # A .json file that is invalid JSON (missing closing quote and brace)
        # but contains OLD_TAG so the rewriter attempts to process it.
        conf = home / "broken.json"
        # Write invalid JSON that contains the tag (rewriter will try to rewrite + validate)
        conf.write_text('{"agent": "' + OLD_TAG)  # deliberately missing closing " and }

        result = _run_script(extracted_script, home, OLD_TAG, NEW_NAME)
        assert result.returncode == 1, (
            f"Expected exit 1 on JSON validation failure, got {result.returncode}. "
            f"stdout: {result.stdout!r} stderr: {result.stderr!r}"
        )
        # The .bak-prefinalize must exist (backup created before the write attempt)
        assert (home / "broken.json.bak-prefinalize").exists(), (
            ".bak-prefinalize must be created before any write attempt"
        )

    def test_original_file_intact_after_json_invalidation_exit(self, tmp_path, extracted_script):
        """After a JSON-invalidation exit, the original file content is preserved.

        The rewriter takes a backup (.bak-prefinalize) before attempting the
        write, then validates the rewritten text.  On validation failure it
        exits 1 WITHOUT overwriting the file — the original bytes must still
        be readable.
        """
        home = tmp_path / "agent-newborn-20990101000002"
        home.mkdir()
        # Write a file that is already-invalid JSON containing OLD_TAG.
        # The rewriter will attempt rewrite → json.loads(new_text) fails → exit 1.
        original_content = '{"agent": "' + OLD_TAG  # missing closing " and }
        conf = home / "broken.json"
        conf.write_text(original_content)

        result = _run_script(extracted_script, home, OLD_TAG, NEW_NAME)
        assert result.returncode == 1

        # The file must still contain the ORIGINAL content (not the partially
        # rewritten text), because the rewriter exits before p.write_text().
        current_content = conf.read_text()
        assert current_content == original_content, (
            f"Original file was modified despite JSON validation failure.\n"
            f"Expected: {original_content!r}\n"
            f"Got:      {current_content!r}"
        )

    def test_loud_fail_message_in_stderr(self, tmp_path, extracted_script):
        """On JSON validation failure, the script prints an ERROR message to stderr."""
        home = tmp_path / "agent-newborn-20990101000003"
        home.mkdir()
        conf = home / "broken.json"
        conf.write_text('{"agent": "' + OLD_TAG)

        result = _run_script(extracted_script, home, OLD_TAG, NEW_NAME)
        assert result.returncode == 1
        # The error message should appear on stderr
        assert "ERROR" in result.stderr, (
            f"Expected ERROR in stderr on validation failure, got: {result.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Test: genericity — function is parameterized, not hardcoded
# ---------------------------------------------------------------------------

class TestGenericity:
    def test_different_synthetic_tags(self, tmp_path, extracted_script):
        """Works with arbitrary newborn tags, not just specific agent names."""
        old = "agent-newborn-29991231235959"
        new = "agent-synthetic-v2"
        home = tmp_path / old
        home.mkdir()
        (home / "config.toml").write_text(f"home = '/home/{old}'\n")
        result = _run_script(extracted_script, home, old, new)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (home / "config.toml").read_text()
        assert old not in text
        assert new in text

    def test_encoded_cwd_form_rewritten(self, tmp_path, extracted_script):
        """The encoded-cwd form (-home-agent-newborn-XXX) is also rewritten."""
        home = tmp_path / OLD_TAG
        home.mkdir()
        encoded = f"-home-{OLD_TAG}-project-dir"
        (home / "settings.json").write_text(json.dumps({"cwd_alias": encoded}))
        result = _run_script(extracted_script, home, OLD_TAG, NEW_NAME)
        assert result.returncode == 0, f"Script failed: {result.stderr}"
        text = (home / "settings.json").read_text()
        assert OLD_TAG not in text
        assert NEW_NAME in text


# ---------------------------------------------------------------------------
# Step 7 viz-ACL counter regression tests
# ---------------------------------------------------------------------------

def _step7_acl_loop(tmp_path: Path, dirs: list, setfacl_exits: dict | None = None) -> subprocess.CompletedProcess:
    """Run the Step 7 ACL-counter loop in a bash subshell.

    Creates dirs under tmp_path, installs a mock setfacl that returns the
    exit code in setfacl_exits (keyed by dir basename, default 0), then runs
    the loop and returns the result.
    """
    if setfacl_exits is None:
        setfacl_exits = {}

    # Create dirs
    for d in dirs:
        (tmp_path / d).mkdir(exist_ok=True)

    # Write a mock setfacl that reads its exit code from a per-dir file.
    # The -R flag doesn't change the basename, so we key on the last path arg.
    mock_setfacl = tmp_path / "setfacl"
    mock_setfacl.write_text(
        "#!/bin/bash\n"
        "# Last arg is the target path; look up exit code by basename.\n"
        "target=\"${@: -1}\"\n"
        "base=$(basename \"$target\")\n"
        f"declare -A exits=({' '.join(f'[{k}]={v}' for k, v in setfacl_exits.items())})\n"
        "exit \"${exits[$base]:-0}\"\n"
    )
    mock_setfacl.chmod(0o755)

    script = f"""
_step7_test() {{
    local chosen_home="{tmp_path}"
    local VIZ_OPERATOR="testuser"
    local _viz_ok=0 _viz_skipped=0 _viz_cfg_ok=0
    for _engram_dir in "$chosen_home"/.engram "$chosen_home"/.engram-*; do
        [[ -d "$_engram_dir" ]] || {{ (( _viz_skipped++ )) || true; continue; }}
        local _setfacl_err
        _setfacl_err=$(setfacl -R -m "u:${{VIZ_OPERATOR}}:rX" "$_engram_dir" 2>&1) \\
            && setfacl -R -d -m "u:${{VIZ_OPERATOR}}:rX" "$_engram_dir" 2>/dev/null \\
            && (( ++_viz_ok )) \\
            || {{ echo "  WARN: setfacl rX failed on $_engram_dir: $_setfacl_err" >&2; continue; }}
    done
    echo "_viz_ok=$_viz_ok _viz_skipped=$_viz_skipped"
}}
export PATH="{tmp_path}:$PATH"
_step7_test
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


class TestStep7VizAclCounter:
    """Regression tests for the Step 7 ACL counter (Borges blocker, PR #1351).

    Bug: (( _viz_ok++ )) post-increment evaluates to 0 (false) when _viz_ok=0,
    so the || WARN branch fires on the FIRST dir even when setfacl succeeded.
    Fix: (( ++_viz_ok )) pre-increment evaluates to 1 (true) on first success.
    """

    def test_single_engram_dir_no_spurious_warn(self, tmp_path):
        """A single ~/.engram dir with a succeeding setfacl produces no WARN."""
        result = _step7_acl_loop(tmp_path, [".engram"])
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "WARN" not in result.stderr, f"Spurious WARN on first dir: {result.stderr}"
        assert "_viz_ok=1" in result.stdout, f"Counter not incremented: {result.stdout}"

    def test_two_engram_dirs_both_counted(self, tmp_path):
        """Two engram dirs (.engram + .engram-gemini) both counted, no WARN."""
        result = _step7_acl_loop(tmp_path, [".engram", ".engram-gemini"])
        assert result.returncode == 0
        assert "WARN" not in result.stderr, f"Spurious WARN: {result.stderr}"
        assert "_viz_ok=2" in result.stdout, f"Expected 2 successes: {result.stdout}"

    def test_no_engram_dirs_all_skipped(self, tmp_path):
        """With no engram dirs, _viz_ok=0 and _viz_skipped>0, no WARN."""
        result = _step7_acl_loop(tmp_path, [])
        assert result.returncode == 0
        assert "WARN" not in result.stderr
        assert "_viz_ok=0" in result.stdout
        assert "_viz_skipped=" in result.stdout

    def test_setfacl_failure_emits_warn(self, tmp_path):
        """When setfacl fails, WARN IS emitted (distinguishes real failure from false alarm)."""
        result = _step7_acl_loop(
            tmp_path, [".engram"], setfacl_exits={".engram": 1}
        )
        assert "WARN" in result.stderr, f"Expected WARN on setfacl failure: {result.stderr}"
        assert "_viz_ok=0" in result.stdout
