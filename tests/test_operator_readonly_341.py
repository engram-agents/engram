"""#341: the operator account may READ a seat's ENGRAM files and may not WRITE any.

Cells:
  * structural guard (repo-wide, runs everywhere): no shipped file passes
    setfacl a user entry (``u:`` or ``user:``, ``d:`` / ``default:`` too) whose
    permission carries write (a letter ``w``, or octal 2/3/6/7), or a
    ``m::`` / ``mask::`` carrying write. The grantee can be any text or any
    shell variable, so ``u:$SUDO_USER:rw`` and ``u:spt:rw`` are caught as well
    as the operator names. Group entries (``g:``) are out of scope. Roots: src,
    tools, deploy, tests/spawn, README.md, USER_GUIDE.md, docs.
    Shell and other text: backslash-continued lines are joined, then every
    logical line with ``setfacl`` is read; comment lines and echo/printf lines
    are skipped. Python: parsed with ``ast``; for each call, the constant parts
    of its arguments (``+``, f-strings, ``%``, ``.format``, lists, ``" ".join``
    folded; variables become a placeholder) are read, so docstrings, comments and
    print/logging text are never mistaken for a grant. The small ALLOW table
    lists the known synthetic grants with a reason, and fails when an entry
    stops matching.
    WHAT STILL ESCAPES (the guard is text and syntax, not execution): a grant
    assembled across separate statements or variables (``e = "u:x:rw"`` on one
    line, ``setfacl ... e`` on another); a grant built or run by a helper
    function the guard cannot see into; ``setfacl`` called through a name that is
    not the literal word (an alias, ``$SETFACL``); a grant read from a
    file or the environment at run time; and binary or non-text files.
  * mocked bootstrap 5.7 cell (runs everywhere): the issued setfacl arguments.
  * agentctl finalize-name text cell (runs everywhere).
  * real-ACL cells 1-3 (Linux only, skipped without setfacl/getfacl).
"""
import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "tools" / "agent-bootstrap"
AGENTCTL = REPO / "tools" / "agentctl"

SCAN_ROOTS = ["src", "tools", "deploy", "tests/spawn", "README.md", "USER_GUIDE.md", "docs"]
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}

# Known non-operator / synthetic write grants, keyed (relative path, text that
# must appear in the offending setfacl argument list) -> why it is allowed. The
# guard fails if an entry no longer matches anything, so this table cannot rot.
ALLOW = {
    ("tests/spawn/run_spawn_test.sh", "u:root:rw"):
        "synthetic setfacl-mechanics fixture on a mktemp dir, run as root in the "
        "spawn container; not what bootstrap 5.7 grants and not an operator",
    ("src/forum/tests/test_installer_modes_4499.py", "u:65534:rwx"):
        "forum installer test plants a foreign ACL entry (uid 65534) on a tmp dir "
        "so it can prove the installer strips it; not an operator grant",
}

# One ACL user entry: (u|user):<grantee>:<perm>. The grantee is any text
# (literal, $VAR, ${VAR}, ${VAR:-x}, $(cmd), or the VAR placeholder the Python
# folder substitutes); the perm is letters or one octal digit. A leading d: /
# default: is covered because the lookbehind only refuses word characters, $, {.
_PERM = r"([rwxXst-]+|[0-7])(?![A-Za-z0-9_])"
_USER_ENTRY = re.compile(
    r"(?<![A-Za-z0-9_${])(?:u|user):((?:\$\{[^}]*\}|\$\([^)]*\)|[^:\s,\"'])+):" + _PERM)
_MASK_ENTRY = re.compile(r"(?<![A-Za-z0-9_${])(?:m|mask):{1,2}" + _PERM)
_LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}


def _perm_writes(perm: str) -> bool:
    return perm in "2367" if perm.isdigit() else "w" in perm


def _segment_hits(segment: str) -> bool:
    for m in _USER_ENTRY.finditer(segment):
        if _perm_writes(m.group(2)):
            return True
    for m in _MASK_ENTRY.finditer(segment):
        if _perm_writes(m.group(1)):
            return True
    return False


def _setfacl_segments(text: str):
    """Each run of text from a ``setfacl`` word to the end of that command."""
    for m in re.finditer(r"setfacl", text):
        yield re.split(r"[;|&\n]", text[m.start():], maxsplit=1)[0]


def _is_text(path: Path) -> bool:
    try:
        path.read_text()
        return True
    except (UnicodeDecodeError, OSError):
        return False


def _scan_shell_like(text: str):
    """Shell and any other text: join backslash continuations, scan logical lines
    that carry ``setfacl``, skip comment lines and echo/printf commands."""
    hits = []
    physical = text.split("\n")
    i = 0
    while i < len(physical):
        start = i
        logical = physical[i]
        while logical.endswith("\\") and i + 1 < len(physical):
            i += 1
            logical = logical[:-1] + physical[i]
        i += 1
        stripped = logical.strip()
        if not stripped or stripped.startswith("#") or "setfacl" not in stripped:
            continue
        if re.match(r"(echo|printf)\b", stripped):
            continue
        code = re.sub(r"\s#.*$", "", logical)
        for seg in _setfacl_segments(code):
            if _segment_hits(seg):
                hits.append((start + 1, seg.strip()))
    return hits


def _flat(node) -> str:
    """Fold the constant text of an expression; anything dynamic becomes ``VAR``."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        return "".join(_flat(v) if isinstance(v, ast.Constant) else "VAR" for v in node.values)
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            return _flat(node.left) + _flat(node.right)
        if isinstance(node.op, ast.Mod):
            return _flat(node.left)
        return "VAR"
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return " ".join(_flat(e) for e in node.elts)
    if isinstance(node, ast.IfExp):
        return _flat(node.body) + " " + _flat(node.orelse)
    if isinstance(node, ast.Starred):
        return _flat(node.value)
    if isinstance(node, ast.Call):
        parts = []
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Constant):
            parts.append(_flat(node.func.value))
        parts += [_flat(a) for a in node.args] + [_flat(k.value) for k in node.keywords]
        return " ".join(parts)
    return "VAR"


def _scan_python(text: str):
    """Python: for every Call whose folded constant arguments mention setfacl,
    scan that text. Docstrings, comments and bare strings are not call arguments."""
    tree = ast.parse(text)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Name) and f.id == "print") or (
                isinstance(f, ast.Attribute) and f.attr in _LOG_ATTRS):
            continue
        folded = " ".join(_flat(a) for a in node.args + [k.value for k in node.keywords])
        if "setfacl" not in folded:
            continue
        for seg in _setfacl_segments(folded):
            if _segment_hits(seg):
                found[seg.strip()] = node.lineno  # inner calls are walked later, so they win
    return sorted((ln, seg) for seg, ln in found.items())


def scan_source(text: str, name: str):
    """Return [(lineno, setfacl-argument-text)] for real write grants in ``text``.

    ``name`` picks the parser: ``*.py`` (or a python shebang) goes through ``ast``;
    everything else is scanned as shell-like text. A Python file that does not
    parse falls back to the text scan."""
    is_py = name.endswith(".py") or text.startswith("#!") and "python" in text.split("\n", 1)[0]
    if is_py:
        try:
            return _scan_python(text)
        except SyntaxError:
            pass
    return _scan_shell_like(text)


def _raw_hits(base: Path, roots):
    found = []
    for root in roots:
        top = base / root
        if top.is_file():
            files = [top]
        elif top.is_dir():
            files = []
            for dirpath, dirnames, filenames in os.walk(top):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                files += [Path(dirpath) / fn for fn in filenames]
        else:
            continue
        for p in files:
            if p.is_symlink() or not _is_text(p):
                continue
            for lineno, seg in scan_source(p.read_text(), p.name):
                found.append((p.relative_to(base).as_posix(), lineno, seg))
    return found


def _allowed(hit, allow):
    return any(hit[0] == rel and snip in hit[2] for rel, snip in allow)


def scan_tree(base: Path, roots=SCAN_ROOTS, allow=ALLOW):
    return [h for h in _raw_hits(base, roots) if not _allowed(h, allow)]


def stale_allow_entries(base: Path, roots=SCAN_ROOTS, allow=ALLOW):
    raw = _raw_hits(base, roots)
    return [k for k in allow if not any(h[0] == k[0] and k[1] in h[2] for h in raw)]


# --------------------------------------------------------------------------
# Structural guard
# --------------------------------------------------------------------------

class TestStructuralGuard:
    def test_no_shipped_file_grants_operator_write(self):
        found = scan_tree(REPO)
        assert found == [], "shipped code grants the operator write (#341): " + repr(found)

    @pytest.mark.parametrize("planted", [
        'setfacl -m "u:${OPERATOR}:rw" "$f"',
        'setfacl -d -m "u:${VIZ_OPERATOR}:rwx" "$d"',
        '["setfacl", "-m", f"u:{viz_operator}:rw", path]',
        'setfacl -m "u:${OPERATOR}:rX,m::rw" "$f"',
        'setfacl -m m::rwx "$f"',
    ])
    def test_control_scanner_sees_a_planted_grant(self, planted):
        assert scan_source(planted + "\n", "x.sh"), planted

    @pytest.mark.parametrize("quiet", [
        'setfacl -m "u:${OPERATOR}:rX" "$f"',
        'setfacl -R -m "u:${VIZ_OPERATOR}:rX,m::rX" "$d"',
        '# setfacl -m "u:${OPERATOR}:rw" in a comment',
        'setfacl -m "u:$SUDO_USER:r-x" "$f"',
        'setfacl -m "u:${OP}:4" "$f"',
        'setfacl -m "g:${AGENT}:rwx" "$f"',
        'setfacl -b "$f"',
    ])
    def test_control_scanner_ignores_read_only_and_group_grants(self, quiet):
        assert scan_source(quiet + "\n", "x.sh") == [], quiet

    # ---- round 2: one cell per gap (a-e) found by the round-1 review ----

    @pytest.mark.parametrize("planted", [
        'setfacl -m "u:$SUDO_USER:rw" "$f"',
        'setfacl -m "u:$USER:rw" "$f"',
        'setfacl -m "u:${VIZ_USER}:rw" "$f"',
        'setfacl -m "u:${OP}:rwx" "$f"',
        'setfacl -m u:spt:rw "$f"',
        'setfacl -m "user:${OPERATOR}:rw" "$f"',
        'setfacl -m "d:u:${OP}:rw" "$f"',
        'setfacl -m "default:user:${OP}:rw" "$f"',
        'setfacl -m "u:${OPERATOR}:6" "$f"',
        'setfacl -m "u:${OPERATOR}:2" "$f"',
        'setfacl -m "u:${OPERATOR}:3" "$f"',
        'setfacl -m "u:${OPERATOR}:7" "$f"',
        'setfacl -m "u:${OPERATOR}:rX,u:${OP2}:rw" "$f"',
    ])
    def test_gap_a_any_grantee_any_perm_spelling(self, planted):
        assert scan_source(planted + "\n", "x.sh"), planted

    @pytest.mark.parametrize("planted", [
        'setfacl -m \\\n  "u:${OP}:rw" \\\n  "$f"',
        'setfacl \\\n  -m \\\n  "u:${OPERATOR}:rX,\\\nu:${OP}:rw" "$f"',
    ])
    def test_gap_b_shell_continuation_lines(self, planted):
        assert scan_source(planted + "\n", "x.sh"), planted

    @pytest.mark.parametrize("planted", [
        'run(["setfacl", "-m",\n     "u:" + op + ":rw", path])\n',
        'run(["setfacl", "-m", "u:" "%s" ":rw" % op, path])\n',
        'run(["setfacl", "-m",\n     f"u:{op}:" f"rw", path])\n',
        'run(["setfacl", "-m", f"u:{op}:rwx", path])\n',
        'run("setfacl -m u:{}:rw {}".format(op, path), shell=True)\n',
        'run(" ".join(["setfacl", "-m", "user:" + op + ":6", path]))\n',
    ])
    def test_gap_b_python_concatenation_and_fstrings(self, planted):
        assert scan_source(planted, "x.py"), planted

    @pytest.mark.parametrize("planted", [
        # the mask sits more than 3 physical lines after the word setfacl
        'setfacl -R \\\n  -d \\\n  -P \\\n  -n \\\n  -m "u:${OP}:rX,m::rw" "$f"',
        'setfacl -R \\\n  -d \\\n  -P \\\n  -n \\\n  -m mask::rwx "$f"',
        'setfacl \\\n\\\n  -d \\\n  -L \\\n  -m "d:m::rw" "$f"',
    ])
    def test_gap_c_mask_without_the_three_line_rule(self, planted):
        assert scan_source(planted + "\n", "x.sh"), planted

    def test_gap_c_mask_alone_is_a_hit(self):
        assert scan_source('setfacl -m m::rw "$f"\n', "x.sh")
        assert scan_source('setfacl -m mask::rwx "$f"\n', "x.sh")

    def test_gap_c_python_mask_far_from_the_setfacl_word(self):
        body = ('run(["setfacl",\n"-R",\n"-d",\n"-P",\n"-n",\n'
                '"-m", "m::rw", p])\n')
        assert scan_source(body, "x.py")

    @pytest.mark.parametrize("quiet,name", [
        ('"""Docs: setfacl -m u:viz_operator:rw is what we never do."""\n', "x.py"),
        ('# setfacl -m u:viz_operator:rw\nx = 1\n', "x.py"),
        ('print("run: setfacl -m u:viz_operator:rw f")\n', "x.py"),
        ('msg = "u:${OPERATOR_AGENT}:rwx"\n', "x.py"),
        ('echo "  setfacl -m u:viz_operator:rw (never)"\n', "x.sh"),
        ('printf "%s\\n" "setfacl -m u:${OPERATOR_AGENT}:rwx"\n', "x.sh"),
        ('  # setfacl -m "u:${OP}:rw" f\n', "x.sh"),
        ('setfacl -m "u:${OP}:rX" "$f"  # was u:${OP}:rw\n', "x.sh"),
        ('x = "u:${OPERATOR}:rw"\nrun(["setfacl", "-b", p])\n', "x.py"),
    ])
    def test_gap_d_prose_is_not_a_grant(self, quiet, name):
        assert scan_source(quiet, name) == [], quiet

    def test_gap_e_scan_roots_cover_docs_and_spawn_tests(self, tmp_path):
        for rel in ["src", "tools", "deploy", "tests/spawn", "docs/internal"]:
            (tmp_path / rel).mkdir(parents=True)
        (tmp_path / "README.md").write_text("x\n")
        (tmp_path / "USER_GUIDE.md").write_text("x\n")
        assert scan_tree(tmp_path) == []
        planted = {
            "tests/spawn/p.sh": 'setfacl -m "u:${OP}:rw" "$f"\n',
            "docs/internal/p.md": "```\nsetfacl -m u:${OP}:rw f\n```\n",
            "README.md": "setfacl -m u:${OP}:rw f\n",
            "USER_GUIDE.md": "setfacl -m u:${OP}:rw f\n",
        }
        for rel, body in planted.items():
            f = tmp_path / rel
            old = f.read_text() if f.exists() else None
            f.write_text(body)
            assert [h for h in scan_tree(tmp_path) if h[0] == rel], rel
            f.unlink() if old is None else f.write_text(old)

    def test_allow_table_entry_that_matches_nothing_fails(self, tmp_path):
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "a.sh").write_text('setfacl -m "u:root:rw" "$f"\n')
        live = {("tools/a.sh", "u:root:rw"): "synthetic fixture"}
        assert scan_tree(tmp_path, allow=live) == []
        assert stale_allow_entries(tmp_path, allow=live) == []
        (tmp_path / "tools" / "a.sh").write_text('setfacl -m "u:root:rX" "$f"\n')
        assert stale_allow_entries(tmp_path, allow=live) == [("tools/a.sh", "u:root:rw")]
        # and an allow entry never hides a grant it was not written for
        (tmp_path / "tools" / "a.sh").write_text('setfacl -m "u:other:rw" "$f"\n')
        assert scan_tree(tmp_path, allow=live)

    def test_every_allow_entry_has_a_reason_and_matches_something(self):
        assert all(isinstance(v, str) and len(v) > 10 for v in ALLOW.values())
        assert stale_allow_entries(REPO) == []

    def test_mutation_split_shell_and_concatenated_python_go_red(self, tmp_path):
        for root in SCAN_ROOTS:
            if (REPO / root).is_dir():
                dst = tmp_path / root
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(REPO / root, dst, ignore=shutil.ignore_patterns(*SKIP_DIRS))
        for rel, body in [
            ("tools/split.sh", 'setfacl -m \\\n  "u:${OP}:rw" \\\n  "$f"\n'),
            ("src/concat.py", 'run(["setfacl", "-m",\n     "u:" + who + ":rw", p])\n'),
            ("deploy/mask.sh", 'setfacl \\\n  -m "m::rwx" "$f"\n'),
        ]:
            f = tmp_path / rel
            f.parent.mkdir(exist_ok=True)
            f.write_text(body)
            assert [h for h in scan_tree(tmp_path) if h[0] == rel], rel
            f.unlink()

    def test_mutation_a_new_site_in_each_root_goes_red(self, tmp_path):
        # Copy of the real roots + one planted file per root: the guard must name it.
        for root in SCAN_ROOTS:
            if (REPO / root).is_dir():
                shutil.copytree(REPO / root, tmp_path / root, ignore=shutil.ignore_patterns(*SKIP_DIRS))
        assert scan_tree(tmp_path) == []  # baseline green on the copy
        for root, name, body in [
            ("src", "planted.py", 'run(["setfacl", "-m", f"u:{viz_operator}:rw", p])\n'),
            ("tools", "planted.sh", 'setfacl -m "u:${OPERATOR}:rwx" "$f"\n'),
            ("deploy", "planted.sh", 'setfacl -m "u:${VIZ_OPERATOR}:rX,m::rw" "$f"\n'),
        ]:
            (tmp_path / root).mkdir(exist_ok=True)
            f = tmp_path / root / name
            f.write_text(body)
            assert [h for h in scan_tree(tmp_path) if h[0] == f"{root}/{name}"], root
            f.unlink()
        assert scan_tree(tmp_path) == []


# --------------------------------------------------------------------------
# Bootstrap section 5.7
# --------------------------------------------------------------------------

def _section_57() -> str:
    text = BOOTSTRAP.read_text()
    start = text.index("# ---------- 5.7.")
    end = text.index("# ---------- 6.")
    return text[start:end]


def _run_57(tmp_path, operator, path_prefix=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    script = f'USER_HOME="{home}"\n' + _section_57()
    env = dict(os.environ, SUDO_USER=operator)
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    return home / ".engram", r


class TestBootstrapMocked:
    def _mock(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "setfacl.log"
        m = bindir / "setfacl"
        m.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
        m.chmod(0o755)
        return bindir, log

    def test_section_57_issues_only_rX_grants(self, tmp_path):
        bindir, log = self._mock(tmp_path)
        (tmp_path / "home" / ".engram").mkdir(parents=True)
        (tmp_path / "home" / ".engram" / "config.json").write_text("{}")
        engram, r = _run_57(tmp_path, "opuser", str(bindir))
        assert r.returncode == 0, r.stderr
        calls = log.read_text().splitlines()
        assert len(calls) == 2, calls  # default ACL on the dir, direct ACL on config.json
        for c in calls:
            assert "u:opuser:rX" in c, c
            assert not re.search(r":rwx?\b|m::", c), c
        assert any(c.startswith("-d ") for c in calls)
        assert any(c.endswith("config.json") for c in calls)
        assert "rw" not in r.stdout, r.stdout

    @pytest.mark.parametrize("operator", ["", "root"])
    def test_section_57_does_nothing_without_a_real_operator(self, tmp_path, operator):
        bindir, log = self._mock(tmp_path)
        (tmp_path / "home" / ".engram").mkdir(parents=True)
        engram, r = _run_57(tmp_path, operator, str(bindir))
        assert r.returncode == 0, r.stderr
        assert not log.exists(), log.read_text()

    def test_section_57_calls_are_not_recursive(self, tmp_path):
        # why a re-run cannot narrow files that already inherited rw
        bindir, log = self._mock(tmp_path)
        (tmp_path / "home" / ".engram").mkdir(parents=True)
        (tmp_path / "home" / ".engram" / "config.json").write_text("{}")
        _run_57(tmp_path, "opuser", str(bindir))
        for c in log.read_text().splitlines():
            assert not re.search(r"(^|\s)-\w*R", c), c

    def test_section_57_header_states_the_narrow_scope(self):
        text = BOOTSTRAP.read_text()
        header = text[text.index("# ---------- 5.7."):text.index("ENGRAM_HOME=\"$USER_HOME/.engram\"")]
        assert "NARROWS" not in header
        assert "narrows config.json and the home default ACL only" in " ".join(header.replace("#", " ").split())
        assert "not recursive" in header and "SUDO_USER is empty or root" in header

    def test_hook_comment_does_not_claim_every_rw_seat_is_narrowed(self):
        hook = REPO / "src/engram/hooks/claude/engram-session-start-hook.py"
        flat = " ".join(hook.read_text().replace("#", " ").split())
        assert "is NARROWED" not in flat
        assert "returns early (and removes the marker) when getfacl shows" in flat
        assert "is NOT narrowed here" in flat
        # the behaviour behind that sentence is pinned in test_session_start_hook_viz_acl.py
        # (marker removed + no setfacl call when getfacl already shows the operator entry)

    def test_section_57_text_says_read_only(self):
        sec = _section_57()
        assert not re.search(r"u:\$\{OPERATOR\}:rw", sec)
        assert "r/w" not in sec


# --------------------------------------------------------------------------
# agentctl finalize-name step 7
# --------------------------------------------------------------------------

class TestFinalizeStep7Text:
    def test_cfg_grant_is_read_only(self):
        text = AGENTCTL.read_text()
        cfg_lines = [
            l for l in text.splitlines()
            if 'setfacl -m "u:${VIZ_OPERATOR}:' in l and '"$_cfg"' in l
        ]
        assert len(cfg_lines) == 1, cfg_lines
        assert ':rX"' in cfg_lines[0] and ":rw" not in cfg_lines[0]
        assert "+ rw on" not in text
        assert "+ r on ${_viz_cfg_ok} config.json" in text


# --------------------------------------------------------------------------
# Real-ACL cells 1-3: Linux only
# --------------------------------------------------------------------------

def _pick_operator():
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name
    for cand in ("nobody", "daemon", "bin"):
        try:
            pwd.getpwnam(cand)
        except KeyError:
            continue
        if cand != me:
            return cand
    return None


def _have_acl():
    if not (shutil.which("setfacl") and shutil.which("getfacl")):
        return False
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["setfacl", "-m", f"u:{os.getuid()}:rwx", d], capture_output=True)
        return r.returncode == 0


needs_acl = pytest.mark.skipif(
    not _have_acl() or _pick_operator() is None,
    reason="needs setfacl/getfacl on a filesystem with ACLs and a second account (Linux)",
)


def _getfacl(path):
    return subprocess.run(["getfacl", "-c", str(path)], capture_output=True, text=True).stdout


def _entry(acl, prefix, op):
    for line in acl.splitlines():
        if line.startswith(f"{prefix}user:{op}:"):
            return line
    return None


def _perm(line):
    return re.match(r"[a-z:]*user:[^:]+:([rwx-]{3})", line).group(1)


def _effective(line):
    m = re.search(r"#effective:([rwx-]{3})", line)
    return m.group(1) if m else _perm(line)


def _mask(acl):
    for line in acl.splitlines():
        if line.startswith("mask::"):
            return line.split("::")[1].split()[0]
    return None


@needs_acl
class TestBootstrapRealAcl:
    def test_cell1_bootstrap_default_and_entry_are_read_only(self, tmp_path):
        op = _pick_operator()
        engram, r = _run_57(tmp_path, op)
        assert r.returncode == 0, r.stderr
        acl = _getfacl(engram)
        d = _entry(acl, "default:", op)
        assert d and _perm(d) == "r-x", acl
        m = re.search(r"^default:mask::(\S+)", acl, re.M)
        assert not m or "w" not in m.group(1), acl

    def test_cell2_new_file_inherits_read_only(self, tmp_path):
        op = _pick_operator()
        engram, r = _run_57(tmp_path, op)
        f = engram / "config.json"
        f.write_text("{}")
        os.chmod(f, 0o644)
        acl = _getfacl(f)
        e = _entry(acl, "", op)
        assert e, acl
        assert _perm(e) == "r-x" and _effective(e) == "r--", acl

    def test_cell3_existing_rw_file_is_narrowed(self, tmp_path):
        op = _pick_operator()
        engram = tmp_path / "home" / ".engram"
        engram.mkdir(parents=True)
        cfg = engram / "config.json"
        cfg.write_text("{}")
        subprocess.run(["setfacl", "-m", f"u:{op}:rw", str(cfg)], check=True)
        assert _perm(_entry(_getfacl(cfg), "", op)) == "rw-"  # pre-state really is rw
        _run_57(tmp_path, op)
        acl = _getfacl(cfg)
        e = _entry(acl, "", op)
        assert "w" not in _perm(e) and _effective(e) == "r--", acl
        assert "w" not in (_mask(acl) or ""), acl
