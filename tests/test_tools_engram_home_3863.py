"""The tools/ sweep of #3863 (PR 3a): every tool default under the ENGRAM home follows ENGRAM_HOME.

Lei's ruling (2026-10-17): the deployment uses ENGRAM_HOME wherever possible, since the install
allows a custom engram folder. The ONE resolver is `tools/_owner_read.engram_home()` (U14, #3869);
these tools must reach it, not carry their own `~/.engram`.

Contract (the same for every cell in TABLE):
  * ENGRAM_HOME unset or empty -> the default is exactly the path it was before: <HOME>/.engram/<leaf>.
  * ENGRAM_HOME set (a custom dir, with a space AND a single quote) -> the default is <ENGRAM_HOME>/<leaf>.
  * an explicit override (tripwire's ENGRAM_CONFIG_PATH) keeps priority over the home.

The group guard (`test_no_code_position_hardcoded_home`) scans the CODE files' AST (docstrings and
comments excluded) so a per-line revert is caught here, not only by the file-level home-path gate.

stdlib + pytest only (it runs in pr-checks step 7c-3's carve-out).
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "src" / "engram" / "tools"

# A custom home with a space AND a single quote: the shapes that break a spliced-in shell/python literal.
CUSTOM_NAME = "my engram's home"


def _load(fname: str):
    """Fresh import of tools/<fname> under the CURRENT env (module-level defaults evaluate now)."""
    path = TOOLS / fname
    name = "_t3863_" + re.sub(r"\W", "_", fname)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


def _expand(p) -> str:
    return os.path.expanduser(str(p))


# ---- the accessors: each returns {label: path-as-the-tool-would-use-it} ----------------------------

def _backup_claude_projects(m):
    return {"archive": _expand(m._parse_args([]).archive_dir)}


def _backup_session_logs(m):
    return {"archive": _expand(m._parse_args([]).archive_dir)}


def _compute_task_time(m):
    return {"sessions": m.SESSIONS_DIR}


def _dream_lottery(m):
    return {
        "state": m.DEFAULT_STATE_PATH,
        "db": m.DEFAULT_DB_PATH,
        "history": m.DEFAULT_HISTORY_PATH,
        "parser_state": m._build_parser().parse_args(["status"]).state,
    }


def _loop_prompt(m):
    return {"marker": _expand(m.default_marker_path())}


def _monitor_supervisor(m):
    a = m.build_argparser().parse_args([])
    return {
        "pidfile": a.pidfile,
        "status": a.status_file,
        "const_pid": m.DEFAULT_PIDFILE,
        "const_status": m.DEFAULT_STATUS_FILE,
    }


def _tripwire(m):
    return {"config": str(m._get_config_path())}


def _migrate(fname):
    """The three migrate_* tools build their parser inside main(): read the --db default off the
    'Database not found' error line main() prints for a missing db."""

    def get(m):
        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        old = sys.argv
        sys.argv = [fname]
        try:
            with redirect_stderr(buf), pytest.raises(SystemExit):
                m.main()
        finally:
            sys.argv = old
        line = [l for l in buf.getvalue().splitlines() if "Database not found at" in l][0]
        return {"db": line.split("Database not found at ", 1)[1].strip()}

    return get


# (fname, accessor, {label: leaf under the home})
TABLE = [
    ("backup_claude_projects.py", _backup_claude_projects, {"archive": "claude-projects-archive"}),
    ("backup_session_logs.py", _backup_session_logs, {"archive": "session-logs-archive"}),
    ("compute_task_time.py", _compute_task_time, {"sessions": "sessions"}),
    (
        "dream_lottery.py",
        _dream_lottery,
        {
            "state": "dream-lottery.json",
            "db": "knowledge.db",
            "history": "dream-lottery-history.jsonl",
            "parser_state": "dream-lottery.json",
        },
    ),
    ("loop_prompt.py", _loop_prompt, {"marker": "loop-mode.json"}),
    (
        "migrate_person_nodes.py",
        _migrate("migrate_person_nodes.py"),
        {"db": "knowledge.db"},
    ),
    (
        "migrate_prediction_pairing_edges.py",
        _migrate("migrate_prediction_pairing_edges.py"),
        {"db": "knowledge.db"},
    ),
    (
        "migrate_stranded_supported_by.py",
        _migrate("migrate_stranded_supported_by.py"),
        {"db": "knowledge.db"},
    ),
    (
        "monitor-supervisor.py",
        _monitor_supervisor,
        {
            "pidfile": ".monitor-supervisor.pid",
            "status": ".monitor-supervisor-status.json",
            "const_pid": ".monitor-supervisor.pid",
            "const_status": ".monitor-supervisor-status.json",
        },
    ),
    ("tripwire.py", _tripwire, {"config": "config.json"}),
]
IDS = [t[0] for t in TABLE]


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ENGRAM_CONFIG_PATH", raising=False)
    return home


@pytest.mark.parametrize("fname,accessor,leaves", TABLE, ids=IDS)
def test_unset_default_is_the_old_path(fname, accessor, leaves, fake_home, monkeypatch):
    monkeypatch.delenv("ENGRAM_HOME", raising=False)
    got = accessor(_load(fname))
    assert set(got) == set(leaves)
    for label, leaf in leaves.items():
        assert got[label] == str(fake_home / ".engram" / leaf), (label, got[label])


@pytest.mark.parametrize("fname,accessor,leaves", TABLE, ids=IDS)
def test_empty_env_counts_as_unset(fname, accessor, leaves, fake_home, monkeypatch):
    monkeypatch.setenv("ENGRAM_HOME", "")
    got = accessor(_load(fname))
    for label, leaf in leaves.items():
        assert got[label] == str(fake_home / ".engram" / leaf), (label, got[label])


@pytest.mark.parametrize("fname,accessor,leaves", TABLE, ids=IDS)
def test_custom_home_is_followed(fname, accessor, leaves, fake_home, tmp_path, monkeypatch):
    custom = tmp_path / CUSTOM_NAME
    monkeypatch.setenv("ENGRAM_HOME", str(custom))
    got = accessor(_load(fname))
    for label, leaf in leaves.items():
        assert got[label] == str(custom / leaf), (label, got[label])
        assert str(fake_home / ".engram") not in got[label]


def test_tripwire_config_path_override_keeps_priority(fake_home, tmp_path, monkeypatch):
    """ENGRAM_CONFIG_PATH wins over both the default home and a custom ENGRAM_HOME."""
    explicit = tmp_path / "explicit-config.json"
    monkeypatch.setenv("ENGRAM_CONFIG_PATH", str(explicit))
    for home in (None, str(tmp_path / CUSTOM_NAME)):
        if home is None:
            monkeypatch.delenv("ENGRAM_HOME", raising=False)
        else:
            monkeypatch.setenv("ENGRAM_HOME", home)
        assert str(_load("tripwire.py")._get_config_path()) == str(explicit)


def test_loop_prompt_cli_reads_the_custom_home(tmp_path):
    """Behaviour, not just a constant: the CLI with no --marker looks under ENGRAM_HOME."""
    custom = tmp_path / CUSTOM_NAME
    custom.mkdir()
    env = {**os.environ, "ENGRAM_HOME": str(custom), "HOME": str(tmp_path / "nohome")}
    r = subprocess.run(
        [sys.executable, str(TOOLS / "loop_prompt.py"), "--rearm-delay"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert str(custom / "loop-mode.json") in r.stderr + r.stdout, (r.stdout, r.stderr)


def test_monitor_supervisor_check_and_start_share_one_pidfile(fake_home, tmp_path, monkeypatch):
    """--check and the start path both default to the same module constant, so under one
    ENGRAM_HOME they cannot disagree about where the pidfile is."""
    monkeypatch.setenv("ENGRAM_HOME", str(tmp_path / CUSTOM_NAME))
    m = _load("monitor-supervisor.py")
    a = m.build_argparser().parse_args([])
    assert a.pidfile == m.DEFAULT_PIDFILE
    assert Path(a.pidfile).parent == Path(a.status_file).parent == tmp_path / CUSTOM_NAME


# ---- the group guard --------------------------------------------------------------------------------

CODE_FILES = [t[0] for t in TABLE]

_HARD = [
    re.compile(r"~/\.engram(?![\w-])"),
    re.compile(r"Path\.home\(\)\s*/\s*[\"']\.engram(?![\w-])"),
    re.compile(r"expanduser\(\s*[\"']~/\.engram"),
]


def _code_strings(tree: ast.AST):
    """Every string constant in a code position: docstrings excluded."""
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value


@pytest.mark.parametrize("fname", CODE_FILES)
def test_no_code_position_hardcoded_home(fname):
    src = (TOOLS / fname).read_text()
    tree = ast.parse(src)
    bad = [
        (ln, s.strip()[:80])
        for ln, s in _code_strings(tree)
        if any(p.search(s) for p in _HARD)
    ]
    # `Path.home() / ".engram"` is an expression, not a string: scan the source of every non-docstring
    # line (comments dropped) for the expression spellings too.
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    for i, line in enumerate(src.splitlines(), 1):
        if i in doc_lines:
            continue
        code = line.split("#", 1)[0]
        if _HARD[1].search(code) or _HARD[2].search(code):
            bad.append((i, code.strip()[:80]))
    assert not bad, f"{fname}: hard-coded ENGRAM home in code position: {bad}"


def test_every_code_file_reaches_the_one_resolver():
    for fname in CODE_FILES:
        text = (TOOLS / fname).read_text()
        assert "_owner_read" in text or "ENGRAM_HOME" in text, fname


# ---- design-seat amendment (R1/R2): a line that names the default home also names ENGRAM_HOME -------

PROSE_ONLY_FILES = ["config_schema.py", "enumerate_armed_monitors.py", "fairy_report.py"]
# Borges' ruling on #3863: the venv lives at ~/.engram/venv REGARDLESS of ENGRAM_HOME, so the
# HOME-relative venv spelling is the rule, not a leftover; it needs no ENGRAM_HOME on its line.
_VENV_HOME = re.compile(r"(?:~|\$HOME)/\.engram/venv(?![\w-])")
_HOME_MENTION = re.compile(r"(?:~|\$HOME|\$\{HOME\})/\.engram(?![\w-])|Path\.home\(\)\s*/\s*[\"']\.engram(?![\w-])")


@pytest.mark.parametrize("fname", CODE_FILES + PROSE_ONLY_FILES)
def test_every_line_naming_the_home_names_engram_home(fname):
    bad = [
        (i, line.strip()[:90])
        for i, line in enumerate((TOOLS / fname).read_text().splitlines(), 1)
        if _HOME_MENTION.search(_VENV_HOME.sub("", line)) and "ENGRAM_HOME" not in line and "engram_home()" not in line
    ]
    assert not bad, f"{fname}: a line names the default home without ENGRAM_HOME: {bad}"


@pytest.mark.parametrize("fname", PROSE_ONLY_FILES)
def test_prose_only_files_have_no_code_position_home(fname):
    tree = ast.parse((TOOLS / fname).read_text())
    assert not [s for _, s in _code_strings(tree) if any(p.search(s) for p in _HARD)]
