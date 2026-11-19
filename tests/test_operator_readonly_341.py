"""#341: the operator account may READ a seat's ENGRAM files and may not WRITE any.

Cells:
  * structural guard (repo-wide, runs everywhere): no shipped file grants
    ``:rw``/``:rwx`` (or a mask with ``w``) to the operator / viz-operator.
  * mocked bootstrap 5.7 cell (runs everywhere): the issued setfacl arguments.
  * agentctl finalize-name text cell (runs everywhere).
  * real-ACL cells 1-3 (Linux only, skipped without setfacl/getfacl).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "tools" / "agent-bootstrap"
AGENTCTL = REPO / "tools" / "agentctl"

SCAN_ROOTS = ["src", "tools", "deploy"]
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
# A grant naming an operator-ish variable or literal: u:${OPERATOR}:rw, u:{viz_operator}:rwx ...
_OPERATOR_GRANT = re.compile(r"u:[^\s\"':,]*operator[^\s\"':,]*:[rwxXst-]*w", re.I)
# A mask carrying w, on a setfacl line (or within 3 lines of one).
_MASK_W = re.compile(r"\bm(?:ask)?::?[rwxXst-]*w")


def _is_text(path: Path) -> bool:
    try:
        path.read_text()
        return True
    except (UnicodeDecodeError, OSError):
        return False


def scan_text(text: str):
    """Return [(lineno, line)] for lines that grant the operator write."""
    lines = text.splitlines()
    hits = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if _OPERATOR_GRANT.search(line):
            hits.append((i + 1, line))
            continue
        if _MASK_W.search(line):
            window = "\n".join(lines[max(0, i - 3): i + 4])
            if "setfacl" in window:
                hits.append((i + 1, line))
    return hits


def scan_tree(base: Path, roots=SCAN_ROOTS):
    found = []
    for root in roots:
        top = base / root
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.is_symlink() or not _is_text(p):
                    continue
                for lineno, line in scan_text(p.read_text()):
                    found.append((str(p.relative_to(base)), lineno, line.strip()))
    return found


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
        assert scan_text(planted + "\n"), planted

    @pytest.mark.parametrize("quiet", [
        'setfacl -m "u:${OPERATOR}:rX" "$f"',
        'setfacl -R -m "u:${VIZ_OPERATOR}:rX,m::rX" "$d"',
        '# setfacl -m "u:${OPERATOR}:rw" in a comment',
        'setfacl -m "u:${AGENT}:rw" "$f"',
    ])
    def test_control_scanner_ignores_read_only_and_non_operator(self, quiet):
        assert scan_text(quiet + "\n") == [], quiet

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
