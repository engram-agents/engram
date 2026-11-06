"""engram-alpha#4405 -- no root-run verb of tools/agentctl or tools/agent-bootstrap
writes, chowns, chmods, moves or installs through a path an agent can create,
replace or rename.

Contract-first: these cells were written before the implementation and watched
to fail. Everything runs unprivileged on a scratch tree. Nothing here touches
/home/agents-shared or /var/lib on the host: verb-level cells run a COPY of
tools/agentctl (the SHARED_ROOT and AGENTCTL_STATE literals rewritten to the
scratch tree) inside ``unshare -Urm``, a fake root whose own files read as uid 0.

What the static cells (G1-G5, O1) cannot see: a write through a variable that
holds a path, through a helper, or through a sourced file. The security read
covers that remainder by hand; the scan below is a search by pattern.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENTCTL = REPO / "tools" / "agentctl"
BOOTSTRAP = REPO / "tools" / "agent-bootstrap"

VERBS = {
    "spawn": "cmd_spawn",
    "finalize-name": "cmd_finalize_name",
    "share": "cmd_share",
    "unshare": "cmd_unshare",
}


def _stat_ok() -> bool:
    r = subprocess.run(["stat", "-c", "%u %a", "/"], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _userns_ok() -> bool:
    if not shutil.which("unshare"):
        return False
    r = subprocess.run(
        ["unshare", "-Urm", "bash", "-c", "mount -t tmpfs t /home && echo ok"],
        capture_output=True, text=True, timeout=30)
    return r.stdout.strip() == "ok"


def _identity_drop_ok() -> bool:
    """A one-uid sandbox cannot become the newborn. V4 stands in for
    `sudo -u <newborn>` by running the moved commands in a mount namespace where
    the planted link's target is a read-only stand-in: the newborn lacks rights on
    a root-owned target, while the fake root (the verb's own shell) keeps them."""
    if not _userns_ok():
        return False
    r = subprocess.run(
        ["unshare", "-Urm", "bash", "-c",
         "t=$(mktemp); e=$(mktemp); echo ORIG > \"$t\"; "
         "unshare -m bash -c \"mount --bind $e $t && mount -o remount,ro,bind $t && echo x > $t\" "
         "2>/dev/null && echo WROTE || echo DENIED; cat \"$t\"; rm -f \"$t\" \"$e\""],
        capture_output=True, text=True, timeout=30)
    return r.stdout.split() == ["DENIED", "ORIG"]


needs_stat = pytest.mark.skipif(not _stat_ok(), reason="needs GNU stat -c")
needs_userns = pytest.mark.skipif(not _userns_ok(), reason="no unprivileged user+mount namespace")
needs_nested = pytest.mark.skipif(not _identity_drop_ok(),
                                  reason="cannot stand in for the newborn identity (no private mount namespace)")

_TMP_BASE = tempfile.mkdtemp(prefix="agentctl-4405-")
os.chmod(_TMP_BASE, 0o755)  # a stranger uid must be able to traverse it (V4)


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    shutil.rmtree(_TMP_BASE, ignore_errors=True)


@pytest.fixture
def scratch():
    d = Path(tempfile.mkdtemp(dir=_TMP_BASE))
    d.chmod(0o755)
    return d


# --------------------------------------------------------------------------
# text helpers (static cells)
# --------------------------------------------------------------------------

def _func_body(text, name):
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}\n", text, re.S | re.M)
    assert m, f"function {name} not found"
    return m.group(1)


def _logical_lines(text):
    """Yield (lineno, line, func) for code lines: comments dropped, heredoc
    bodies dropped (the line that OPENS a heredoc is kept), backslash
    continuations joined."""
    out = []
    lines = text.split("\n")
    i = 0
    func = None
    heredoc_end = None
    while i < len(lines):
        raw = lines[i]
        lineno = i + 1
        if heredoc_end is not None:
            if raw.strip() == heredoc_end:
                heredoc_end = None
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{", raw)
        if m:
            func = m.group(1)
        end_func = raw.startswith("}")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        joined = raw
        while joined.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            joined = joined.rstrip()[:-1] + " " + lines[i].strip()
        hd = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", joined)
        if hd:
            heredoc_end = hd.group(2)
        out.append((lineno, joined, func))
        if end_func:
            func = None
        i += 1
    return out


WRITE_CMDS = frozenset({"chown", "chgrp", "chmod", "setfacl", "mv", "cp", "install",
                        "tee", "ln", "rm"})
# a root shell running a quoted string: the string is invisible to the scan, so
# the wrapper itself must be a named allow-list row
INDIRECT_CMDS = frozenset({"bash", "sh", "dash", "zsh", "eval", "xargs"})
NEWBORN_WRAPPERS = frozenset({"ab_as_newborn"})
NEWBORN_USER = re.compile(r'^"?\$\{?(chosen_user|USER_NAME)\}?"?$')
_KEEP_STR = re.compile(r'\$\{?(chosen_user|USER_NAME)\}?')
SKIP_WRAPPERS = frozenset({"env", "exec", "nice", "time", "nohup", "timeout", "builtin"})
KEYWORDS = ("then", "do", "else", "!", "if", "elif", "while", "until")
# the root-run regions of tools/agentctl (R2); tools/agent-bootstrap is scanned whole
AGENTCTL_REGIONS = frozenset({"cmd_spawn", "cmd_finalize_name", "cmd_share",
                              "cmd_unshare", "audit"})


def _blank(line, in_dq):
    """Replace the CONTENT of quoted strings with x (the quote marks stay), so a
    word inside an echo is not read as a command. A string whose whole content is
    $chosen_user / $USER_NAME is kept: the newborn-prefix test reads it. in_dq
    carries an unterminated double-quoted string to the next logical line.
    Returns (blanked, in_dq)."""
    out, buf = [], []
    q = '"' if in_dq else None
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if q is None:
            if c == "\\" and i + 1 < n:
                out.append(line[i:i + 2]); i += 2; continue
            if c in "\"'":
                q = c; buf = []
            out.append(c); i += 1
            continue
        if q == '"' and c == "\\" and i + 1 < n:
            buf.append(line[i:i + 2]); i += 2; continue
        if c == q:
            s = "".join(buf)
            out.append(s if _KEEP_STR.fullmatch(s) else "x")
            out.append(c); q = None; i += 1
            continue
        buf.append(c); i += 1
    return "".join(out), q == '"'


def _segments(blanked):
    b = re.sub(r"\[\[.*?\]\]", " x ", blanked)
    b = re.sub(r"\(\(.*?\)\)", " x ", b)
    b = re.sub(r"\$\(|`", ";", b)
    return re.split(r"&&|\|\||[;&|(){}]", b)


def _classify(tokens):
    """(command word, behind-the-newborn) of one command segment."""
    i, newborn = 0, False
    while i < len(tokens):
        t = tokens[i]
        if t in KEYWORDS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", t):
            i += 1
        elif t in NEWBORN_WRAPPERS:
            newborn = True
            i += 1
        elif t == "sudo":
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                if tokens[i] == "-u" and i + 1 < len(tokens):
                    newborn = newborn or bool(NEWBORN_USER.match(tokens[i + 1]))
                    i += 2
                else:
                    i += 1
        elif t in SKIP_WRAPPERS:
            i += 1
            while i < len(tokens) and (tokens[i].startswith("-") or tokens[i].isdigit()):
                i += 1
        else:
            return os.path.basename(t.strip("\"'")), newborn
    return None, newborn


_REDIRECT = re.compile(r"(?<![<>])(\d*|&)>>?(?!&)\s*([^\s;)|&<]*)")


def scan_root_writes(text, regions=None):
    """Every root-run write in the text: chown chgrp chmod setfacl mv cp install
    tee ln rm, or a redirect to a file, unless the command runs behind
    `sudo -u "$chosen_user"` (or ab_as_newborn). A redirect is root's even behind
    sudo -u, because the shell opens it. A root bash/sh/eval/xargs is flagged too:
    its quoted body is not read. No path token decides anything (round 3: the
    first G4 read only lines naming five variables and missed the viz setfacl
    lines). `regions` limits the scan to those function names (None: all text).
    Built to be run in Python: a `$` in a double-quoted shell pattern would
    expand. Returns [(lineno, logical line, why)]."""
    hits = []
    in_dq = False
    for lineno, line, func in _logical_lines(text):
        if regions is not None and func not in regions:
            in_dq = False
            continue
        was_dq = in_dq
        blanked, in_dq = _blank(line, in_dq)
        if was_dq and not re.sub(r'[x"\s]', "", blanked):
            continue
        why = []
        for seg in _segments(blanked):
            toks = seg.split()
            cmd, newborn = _classify(toks)
            if newborn or cmd is None:
                continue
            if cmd in WRITE_CMDS:
                why.append(f"root {cmd}")
            elif cmd in INDIRECT_CMDS:
                why.append(f"root {cmd} (quoted body unread)")
        for m in _REDIRECT.finditer(re.sub(r"\[\[.*?\]\]|\(\(.*?\)\)", " x ", blanked)):
            if m.group(2) != "/dev/null" and not m.group(2).startswith("&"):
                why.append(f"root redirect to {m.group(2)}")
        if why:
            hits.append((lineno, line.strip(), "; ".join(sorted(set(why)))))
    return hits


# Named allow-list (R2). One row per root-run write the scan sees, by EXACT logical
# line text (comments inside the line included), with one reason per row. A row
# whose line no longer exists fails the cell (stale), so the list cannot outlive
# the code. Three kinds of reason appear here:
#   CREATED  the path was created by root a moment ago in a directory no agent can
#            write, or in the newborn home before any process of that account exists
#   OPERATOR the path is one the operator named on the command line; a known
#            residual (check-then-use), declared, not a guard
#   OWN      the write goes to root's own state, or to the entry just created
_NB = "newborn home, created by useradd earlier in this script: no process of the account exists yet and /home is root-only"
ALLOW = [
    ("agentctl",
     'chgrp agents "/home/agent-$newborn_tag" 2>/dev/null ||  echo "WARN: chgrp agents /home/agent-$newborn_tag failed; the operator may not be able to traverse the new home." >&2',
     "CREATED: the argument is the newborn's home directory ITSELF, a name directly under /home, which only root can create or replace; nothing inside the newborn's tree is named"),
    ("agentctl",
     'mv -T -- "$SHARED_ROOT/health/$newborn_tag" "$SHARED_ROOT/health/$chosen"',
     "OWN: health rename; the parent is gated root-owned and sticky, the source is refused if a link, the destination if it exists, and -T makes a destination planted after the check fail instead of receiving the source"),
    ("agentctl",
     'chown -h "$chosen_user:agents" "$SHARED_ROOT/health/$chosen"',
     "OWN: -h acts on the entry, never its target; the entry was just moved by the sticky-gated rename"),
    ("agentctl",
     '( umask 022; echo "$stamp $action ${SUDO_USER:-$(id -un)}→$agent path=$path${extras:+ $extras}" >> "$AGENTCTL_STATE/share-log" )  || { echo "ERROR: $action happened but could not be logged (write to $AGENTCTL_STATE/share-log failed)." >&2; return 1; }',
     "OWN: append into the root-only state directory, re-verified by _state_dir_check immediately before"),
    ("agentctl",
     'setfacl -m "g:$user:--x" "$parent"  || { echo "ERROR: setfacl --x failed on \'$parent\'" >&2; exit 1; }',
     "OPERATOR: cmd_share parent walk, an ancestor of the operator-chosen path; same check-then-use residual as the path rows below (an ancestor an agent owns can be renamed and replaced by a link); not a guard"),
    ("agentctl",
     'setfacl -m "u:$SUDO_USER:r-x" "$parent"  || echo "WARNING: preserve u:$SUDO_USER:r-x failed on \'$parent\'" >&2',
     "OPERATOR: cmd_share parent walk, same ancestors and same residual as the row above"),
    ("agentctl",
     'setfacl -R -m "g:$user:$acl_perm" "$path"  || { echo "ERROR: setfacl $acl_perm failed on \'$path\'" >&2; exit 1; }',
     "OPERATOR: cmd_share on an operator-chosen path resolved by readlink -f; the [[ -L ]] test before it is a check-then-use gap, a known residual, not a guard"),
    ("agentctl",
     'setfacl -R -d -m "g:$user:$acl_perm" "$path"  || echo "WARNING: default ACL setfacl failed on \'$path\'" >&2',
     "OPERATOR: cmd_share default ACL: same operator-chosen path, same residual, not a guard"),
    ("agentctl",
     'setfacl -R -x "g:$user" "$path" 2>/dev/null || true',
     "OPERATOR: cmd_unshare on an operator-chosen path resolved by readlink -f; the [[ -L ]] test is a check-then-use gap, a known residual, not a guard"),
    ("agentctl",
     '[[ -d "$path" ]] && setfacl -R -d -x "g:$user" "$path" 2>/dev/null || true',
     "OPERATOR: cmd_unshare default ACL: same operator-chosen path, same residual, not a guard"),
    ("agentctl",
     'setfacl -x "g:$user" "$parent" 2>/dev/null || true',
     "OPERATOR: cmd_unshare parent GC, ancestors of the operator-chosen path; same residual as the cmd_share parent walk"),
    ("agent-bootstrap", 'chmod 0755 "$cfgdir"',
     "CREATED: $cfgdir was made by mktemp -d a few lines up (root-owned, random name, in TMPDIR, normally sticky /tmp)"),
    ("agent-bootstrap",
     'printf \'[safe]\\n\\tdirectory = "%s"\\n\' "$esc" > "$cfg" && chmod 0644 "$cfg" || rc=$?',
     "CREATED: $cfg is a new file inside the root-owned $cfgdir, which no agent can write"),
    ("agent-bootstrap", 'rm -rf -- "$cfgdir"   # success or failure: the trust entry never outlives the clone command',
     "CREATED: removes the root-owned mktemp directory made above"),
    ("agent-bootstrap", 'chmod "$HOME_MODE" "$USER_HOME"', "CREATED: " + _NB),
    ("agent-bootstrap", 'cat >"$USER_HOME/.bashrc" <<\'BASHRC\'', "CREATED: " + _NB),
    ("agent-bootstrap", 'chown "$USER_NAME:$USER_NAME" "$USER_HOME/.bashrc"', "CREATED: " + _NB),
    ("agent-bootstrap", 'cat >"$USER_HOME/.runtime/start.sh" <<\'RUN\'', "CREATED: " + _NB),
    ("agent-bootstrap", 'chmod 0755 "$USER_HOME/.runtime/start.sh"', "CREATED: " + _NB),
    ("agent-bootstrap", 'cat >"$USER_HOME/.runtime/install.sh" <<INSTALL_WRAPPER', "CREATED: " + _NB),
    ("agent-bootstrap", 'chmod 0755 "$USER_HOME/.runtime/install.sh"', "CREATED: " + _NB),
    ("agent-bootstrap", 'cat >>"$USER_HOME/.bashrc" <<\'VENV\'', "CREATED: " + _NB),
    ("agent-bootstrap", 'cp "$_src" "$USER_HOME/.local/bin/"', "CREATED: " + _NB),
    ("agent-bootstrap", 'chmod 0755 "$USER_HOME/.local/bin/claude-remote"', "CREATED: " + _NB),
    ("agent-bootstrap", 'setfacl -d -m "u:${OPERATOR}:rw" "$ENGRAM_HOME"', "CREATED: " + _NB),
    ("agent-bootstrap", 'setfacl -m "u:${OPERATOR}:rw" "$CONFIG_JSON"', "CREATED: " + _NB),
    ("agent-bootstrap", 'chgrp -R agents "$SHARED_ROOT"',
     "OWN: chgrp -R does not follow links (measured by the security seat); spec C9 keeps it"),
    ("agent-bootstrap", 'chown -h "$USER_NAME:agents" "$SHARED_ROOT/health/$NAME"',
     "CREATED: -h; the entry was made one line above by mkdir without -p (a planted name already refused), in a sticky root-owned parent"),
    ("agent-bootstrap", 'chown -R "$USER_NAME:$USER_NAME" "$USER_HOME"',
     "CREATED: " + _NB + "; a link at $USER_HOME is refused just above"),
]


def _scan_file(name, text):
    regions = AGENTCTL_REGIONS if name == "agentctl" else None
    allow = [(exact, why) for f, exact, why in ALLOW if f == name]
    unallowed, used = [], set()
    for lineno, line, why in scan_root_writes(text, regions):
        for i, (exact, _why) in enumerate(allow):
            if line == exact:
                used.add(i)
                break
        else:
            unallowed.append((lineno, line, why))
    return unallowed, [r for i, r in enumerate(allow) if i not in used]


def _g5_findings(text):
    """`chown -R` / `setfacl -R` lines: (lineno, line, func, has_L_or_H, arg)."""
    out = []
    for lineno, line, func in _logical_lines(text):
        for m in re.finditer(r"(?<![\w./-])(chown|setfacl)\s+([^|;&)]*)", line):
            toks = m.group(2).split()
            if not any(re.fullmatch(r"-[A-Za-z]*R[A-Za-z]*", t) for t in toks):
                continue
            has_lh = any(re.fullmatch(r"-[A-Za-z]*[LH][A-Za-z]*", t) for t in toks)
            toks = [t for t in toks if not t.startswith(("2>", ">"))]
            arg = toks[-1].strip("\"'") if toks else ""
            out.append((lineno, line.strip(), func, has_lh, arg))
    return out


def _g5_link_refusals_missing(text):
    """Each `-R` site's argument must be preceded, inside its function, by a
    `[[ -L <arg> ]]` test."""
    lines = text.split("\n")
    missing = []
    for lineno, line, func, _lh, arg in _g5_findings(text):
        start = 0
        for j in range(lineno - 1, -1, -1):
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{", lines[j]):
                start = j
                break
        window = "\n".join(lines[start:lineno - 1])
        if not re.search(r"\[\[\s+-L\s+[\"']?" + re.escape(arg.strip("\"'")) + r"[\"']?\s+\]\]", window):
            missing.append((lineno, line, arg))
    return missing


# --------------------------------------------------------------------------
# P -- the pure predicate, through the __state-dir-ok seam
# --------------------------------------------------------------------------

def _ok(*args):
    r = subprocess.run(["bash", str(AGENTCTL), "__state-dir-ok", *args],
                       capture_output=True, text=True, timeout=30)
    assert "Unknown subcommand" not in r.stdout, "the __state-dir-ok seam does not exist"
    return r.returncode


class TestPredicate:
    def test_P1_good_directory_accepted(self):
        assert _ok("0", "755", "1", "0", "0") == 0

    def test_P2_group_write_refused(self):
        assert _ok("0", "775", "1", "0", "0") != 0
        assert _ok("0", "2775", "1", "0", "0") != 0

    def test_P3_other_write_refused(self):
        assert _ok("0", "757", "1", "0", "0") != 0

    def test_P4_wrong_owner_refused(self):
        assert _ok("1000", "755", "1", "0", "0") != 0
        # the expected uid is an argument: the same pair passes when it matches
        assert _ok("1000", "755", "1", "0", "1000") == 0

    def test_P5_link_refused(self):
        assert _ok("0", "755", "1", "1", "0") != 0

    def test_P6_non_directory_refused(self):
        assert _ok("0", "755", "0", "0", "0") != 0

    @pytest.mark.parametrize("args", [
        ("x", "755", "1", "0", "0"), ("0", "75x", "1", "0", "0"),
        ("0", "755", "yes", "0", "0"), ("0", "755", "1", "no", "0"),
        ("0", "755", "1", "0", "root"), ("", "755", "1", "0", "0"),
        ("0", "", "1", "0", "0"), ("0", "755", "1", "0"),
    ])
    def test_P7_non_numeric_or_missing_input_refused(self, args):
        assert _ok(*args) != 0


# --------------------------------------------------------------------------
# W -- the measuring wrapper, on real scratch paths under a fake root
# --------------------------------------------------------------------------

def _funcs():
    text = AGENTCTL.read_text()
    return "\n".join(
        f"{n}() {{\n{_func_body(text, n)}}}" for n in ("_state_dir_ok", "_state_dir_check"))


def _check(path):
    script = f'{_funcs()}\n_state_dir_check "$1"'
    return subprocess.run(["unshare", "-Urm", "bash", "-c", script, "x", str(path)],
                          capture_output=True, text=True, timeout=30)


@needs_stat
@needs_userns
class TestWrapper:
    def test_W1_symlink_to_a_good_directory_refused_and_named(self, scratch):
        good = scratch / "good"
        good.mkdir(mode=0o755)
        good.chmod(0o755)
        link = scratch / "link"
        link.symlink_to(good)
        # positive control: the very directory behind the link passes
        assert _check(good).returncode == 0, _check(good).stderr
        r = _check(link)
        assert r.returncode != 0
        assert str(link) in r.stderr and "link" in r.stderr.lower()
        assert "sudo" in r.stderr  # the exact repair command

    def test_W2_regular_file_refused(self, scratch):
        (scratch / "ok").mkdir(mode=0o755)
        assert _check(scratch / "ok").returncode == 0
        f = scratch / "file"
        f.write_text("x")
        f.chmod(0o755)
        r = _check(f)
        assert r.returncode != 0 and str(f) in r.stderr
        assert "not a directory" in r.stderr

    def test_W3_missing_path_refused(self, scratch):
        (scratch / "ok").mkdir(mode=0o755)
        assert _check(scratch / "ok").returncode == 0
        r = _check(scratch / "nope")
        assert r.returncode != 0 and str(scratch / "nope") in r.stderr
        assert "sudo" in r.stderr

    def test_W4_group_writable_and_wrong_owner_named(self, scratch):
        d = scratch / "gw"
        d.mkdir()
        d.chmod(0o775)
        r = _check(d)
        assert r.returncode != 0 and "775" in r.stderr and str(d) in r.stderr
        # a directory the fake root does not own: /proc/1 is root-owned on the
        # host, so it reads as an unmapped owner here
        r = _check("/proc")
        assert r.returncode != 0 and "/proc" in r.stderr


# --------------------------------------------------------------------------
# G -- static
# --------------------------------------------------------------------------

class TestStatic:
    def test_G1_gate_follows_require_root_in_all_four_verbs(self):
        text = AGENTCTL.read_text()
        lines = text.split("\n")
        sites = []
        for i, l in enumerate(lines):
            m = re.match(r"^\s*require_root\s+[\"']?(spawn|finalize-name|share|unshare)[\"']?\s*$", l)
            if m:
                sites.append((m.group(1), i))
        assert len(sites) == 4, sites
        assert sorted(s[0] for s in sites) == sorted(VERBS)
        # both quoting forms exist in the file; the pattern must see both
        assert any('"' in lines[i] for _, i in sites) and any('"' not in lines[i] for _, i in sites)
        for verb, i in sites:
            assert lines[i + 1].strip() == "_state_dir_init || exit 1", (verb, lines[i + 1])

    def test_G1_control_a_missing_gate_is_seen(self):
        planted = 'cmd_x() {\n    require_root share\n    local user\n}\n'
        lines = planted.split("\n")
        assert lines[2].strip() != "_state_dir_init || exit 1"

    def test_G2_literal_zero_at_the_production_call(self):
        text = AGENTCTL.read_text()
        calls = [l.strip() for l in text.split("\n")
                 if re.search(r"(^|[\s;&|(])_state_dir_ok\s", l)
                 and not l.lstrip().startswith("#")
                 and not l.startswith("_state_dir_ok()")
                 and "cmd__state_dir_ok" not in l]
        # the seam passes "$@" through and is not a production call
        prod = [c for c in calls if '"$@"' not in c]
        assert len(prod) == 1, calls
        assert "_state_dir_ok" in _func_body(text, "_state_dir_check")
        toks = prod[0].split()
        idx = [i for i, t in enumerate(toks) if t == "_state_dir_ok"][0]
        args = toks[idx + 1:idx + 6]
        assert len(args) == 5 and args[4].rstrip(";") == "0", args

    def test_G3_state_constant_is_one_readonly_literal(self):
        text = AGENTCTL.read_text()
        assigns = [l for l in text.split("\n")
                   if re.match(r"^\s*(readonly\s+|declare\s+\S+\s+|local\s+|export\s+)*AGENTCTL_STATE=", l)]
        assert assigns == ['readonly AGENTCTL_STATE="/var/lib/agentctl"'], assigns
        code = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#"))
        assert not re.search(r"\$\{AGENTCTL_STATE[:#%/^,@!]", code)
        assert not re.search(r"AGENTCTL_STATE=\S*\$", code)

    @pytest.mark.parametrize("name,path", [("agentctl", AGENTCTL), ("agent-bootstrap", BOOTSTRAP)])
    def test_G4_every_root_write_in_the_root_regions_is_newborn_run_or_named(self, name, path):
        unallowed, stale = _scan_file(name, path.read_text())
        assert unallowed == [], unallowed
        assert stale == [], [r for r in stale]
        # every allow-list row carries a reason
        assert all(why.strip() for _f, _x, why in ALLOW)

    def test_G4_the_regions_exist_and_the_scan_parses_every_one_of_them(self):
        text = AGENTCTL.read_text()
        funcs = {f for _n, _l, f in _logical_lines(text)}
        assert AGENTCTL_REGIONS <= funcs, AGENTCTL_REGIONS - funcs
        # an unterminated double quote would make the scan skip real code: after the
        # last line of every region the string state must be closed
        for fn in AGENTCTL_REGIONS:
            body = "\n".join(f"{n}() {{\n{_func_body(text, n)}}}" for n in [fn])
            in_dq = False
            for _ln, line, _f in _logical_lines(body):
                _b, in_dq = _blank(line, in_dq)
            assert not in_dq, fn
        in_dq = False
        for _ln, line, _f in _logical_lines(BOOTSTRAP.read_text()):
            _b, in_dq = _blank(line, in_dq)
        assert not in_dq

    def test_G4_control_the_scan_sees_planted_lines(self):
        planted = "\n".join([
            'chown root "$SHARED_ROOT/x"',
            'echo hi > "$chosen_home/f"',
            'mv "$a" "$SHARED_ROOT/b"',
            'chmod 2775 "$SHARED_ROOT" "$SHARED_ROOT/audit"',
            'setfacl -m u:x:r "$SHARED_ROOT/y"',
            'setfacl -R -m g:a:r "$path"',
            'cat > "$chosen_home/.engram/m.json" 2>/dev/null <<JSON',
            'JSON',
            'chown "$u" "$chosen_home/m"',
            # round 3: lines that name none of the old path tokens
            'setfacl -R -m "u:${VIZ_OPERATOR}:rX" "$_engram_dir"',
            '[[ -f "$_cfg" ]] && setfacl -m "u:${VIZ_OPERATOR}:rw" "$_cfg"',
            'chmod 777 "$d"',
            'rm -f "$whatever"',
            'ln -s "$a" "$b"',
            'install -m 0644 "$a" "$b"',
            'echo x | tee "$f"',
            'bash -c "chmod 777 $d"',
            'sudo -u root chmod 777 "$d"',
            'sudo -u "$chosen_user" echo hi > "$chosen_home/f"',
        ])
        assert len(scan_root_writes(planted)) == 18
        # and it does not see these
        quiet = "\n".join([
            'sudo -u "$chosen_user" tee "$chosen_home/f" <<JSON',
            'JSON',
            '# chown root "$SHARED_ROOT/x"',
            'cat <<DONE',
            'chown root "$SHARED_ROOT/x"',
            'DONE',
            'echo "$SHARED_ROOT"',
            'echo "refusing to chown or mv it" >&2',
            'sudo -u "$chosen_user" setfacl -R -m "u:x:rX" "$_engram_dir" 2>/dev/null',
            'ab_as_newborn chmod 0755 "$d"',
            'x=$(sudo -u "$chosen_user" cp -a "$a" "$b" 2>&1)',
            'echo "multi',
            'chmod 777 inside a string "',
            'if (( a > b )); then :; fi',
            'foo 2>&1 >/dev/null',
        ])
        assert scan_root_writes(quiet) == []

    def test_G4_control_regions_limit_the_scan_and_a_planted_line_in_one_goes_red(self):
        text = AGENTCTL.read_text()
        planted = text.replace("    require_root share\n", '    require_root share\n    chmod 0777 "$d"\n', 1)
        assert planted != text
        unallowed, _ = _scan_file("agentctl", planted)
        assert [u for u in unallowed if u[1] == 'chmod 0777 "$d"'], unallowed
        # outside every region the same line is not this cell's business
        outside = text + '\nchmod 0777 "$d"\n'
        assert _scan_file("agentctl", outside)[0] == _scan_file("agentctl", text)[0]

    @pytest.mark.parametrize("needle", [
        'setfacl -R -m "u:${VIZ_OPERATOR}:rX" "$_engram_dir"',
        'setfacl -R -d -m "u:${VIZ_OPERATOR}:rX" "$_engram_dir"',
        'setfacl -m "u:${VIZ_OPERATOR}:rw" "$_cfg"',
    ])
    def test_G4_mutant_the_viz_setfacl_run_as_root_goes_red(self, needle):
        text = AGENTCTL.read_text()
        # positive control: as shipped, this very line is NOT among the findings
        assert [u for u in _scan_file("agentctl", text)[0] if needle in u[1]] == []
        assert needle in text and ('sudo -u "$chosen_user" ' + needle) in text
        mutated = text.replace('sudo -u "$chosen_user" ' + needle, needle)
        assert mutated != text, "the viz setfacl line moved: update this cell"
        unallowed, _ = _scan_file("agentctl", mutated)
        assert [u for u in unallowed if needle in u[1]], unallowed

    @pytest.mark.parametrize("name,path", [("agentctl", AGENTCTL), ("agent-bootstrap", BOOTSTRAP)])
    def test_G5_recursive_ops_never_follow_links_and_get_a_real_directory(self, name, path):
        text = path.read_text()
        sites = _g5_findings(text)
        assert sites, "the scan found no chown -R / setfacl -R at all"
        assert [s for s in sites if s[3]] == [], "a -L or -H sits beside a recursive op"
        assert _g5_link_refusals_missing(text) == []

    def test_G5_control_the_scan_sees_planted_lines(self):
        for planted in ('chown -RL a "$d"', 'setfacl -R -L -m u:x:r "$d"', 'chown -R -H a "$d"'):
            assert [s for s in _g5_findings(planted) if s[3]], planted
        assert _g5_link_refusals_missing('f() {\n    chown -R a "$d"\n}\n') != []
        assert _g5_link_refusals_missing('f() {\n    [[ -L "$d" ]] && exit 1\n    chown -R a "$d"\n}\n') == []

    def test_O1_resolve_user_precedes_any_use_of_the_state_path(self):
        text = AGENTCTL.read_text()
        for fn in ("cmd_share", "cmd_unshare"):
            body = [l for l in _func_body(text, fn).split("\n") if not l.lstrip().startswith("#")]
            ru = [i for i, l in enumerate(body) if "resolve_user" in l]
            use = [i for i, l in enumerate(body) if re.search(r"share-state|state_dir|state_file|AGENTCTL_STATE", l)
                   and "_state_dir_init" not in l]
            assert ru and use, (fn, ru, use)
            assert ru[0] < use[0], (fn, ru, use)


# --------------------------------------------------------------------------
# V -- whole verbs under the fake root
# --------------------------------------------------------------------------

STUB_ID = """#!/bin/bash
# agent-newborn-* exists after the bootstrap/usermod stub ran; agent-<chosen> after usermod.
for a in "$@"; do
  case "$a" in
    agent-newborn-*)
      [ -f "$STUB_STATE/created" ] || exit 1
      [ -f "$STUB_STATE/renamed" ] && exit 1
      for b in "$@"; do [ "$b" = "-gn" ] && { echo "$a"; exit 0; }; done
      echo "uid=1(x)"; exit 0 ;;
    agent-chosenone)
      [ -f "$STUB_STATE/renamed" ] || exit 1
      echo "uid=2(x)"; exit 0 ;;
  esac
done
exec /usr/bin/id "$@"
"""

STUB_CHOWN = """#!/bin/bash
echo "chown $*" >> "$STUB_STATE/chown.log"
exit 0
"""

STUB_NOOP = "#!/bin/bash\necho \"$(basename $0) $*\" >> \"$STUB_STATE/noop.log\"\nexit 0\n"
STUB_FAIL = "#!/bin/bash\nexit 1\n"

STUB_BOOTSTRAP = """#!/bin/bash
echo "$*" >> "$STUB_STATE/bootstrap-called"
tag="$1"
mkdir -p "/home/agent-$tag/.engram"
touch "$STUB_STATE/created"
[ -n "${STUB_BREAK_STATE:-}" ] && chmod g+w "$STUB_BREAK_STATE"
exit 0
"""

STUB_USERMOD = """#!/bin/bash
echo "usermod $*" >> "$STUB_STATE/usermod.log"
# usermod -l NEW -d NEWHOME -m OLD
new="$2"; newhome="$4"; old="$6"
mv "/home/$old" "$newhome"
touch "$STUB_STATE/renamed"
exit 0
"""

STUB_SETFACL = """#!/bin/bash
echo "setfacl $*" >> "$STUB_STATE/setfacl.log"
exit 0
"""

# sudo -u <user> [VAR=val ...] cmd args...: a one-uid sandbox cannot switch
# identity, so for the three commands this fix moved under `sudo -u` the stub runs
# them in a private mount namespace where $VICTIM (the planted links' target, a
# root-owned file the newborn has no rights on) is a read-only stand-in. That is
# the newborn's real position: it keeps its own tree and cannot write the target.
# The verb's own root shell is untouched, so a root redirect still reaches it.
STUB_SUDO = """#!/bin/bash
[ "$1" = "-u" ] || exit 99
user="$2"; shift 2
envs=()
while [[ "${1:-}" == *=* ]]; do envs+=("$1"); shift; done
echo "sudo -u $user ${envs[*]:-} $*" >> "$STUB_STATE/sudo.log"
[ "${FAIL_CP:-}" = 1 ] && [ "$1" = cp ] && exit 1
case "$1" in
  tee|mv|setfacl)
    exec env "${envs[@]}" unshare -m -- bash -c '
      if [ -n "${VICTIM_FILE:-}" ] && [ -e "$VICTIM_FILE" ]; then
        mount --bind "$STUB_STATE/empty" "$VICTIM_FILE" && mount -o remount,ro,bind "$VICTIM_FILE" || exit 98
      fi
      exec "$@"' _ "$@" ;;
  *) exec env "${envs[@]}" "$@" ;;
esac
"""

# mv that plants a link at the health rename's destination just before the real
# mv runs: a name created AFTER the verb's own checks (R1). Only the one rename
# whose last argument is <...>/health/chosenone is touched; every other mv (the
# usermod stub's, the projects loop's) passes straight through.
STUB_MV = """#!/bin/bash
last="${@: -1}"
if [ -n "${PLANT_DEST_LINK:-}" ] && [ "$(basename "$last")" = chosenone ] && [[ "$last" == */health/* ]]; then
  ln -s "$PLANT_DEST_LINK" "$last"
fi
exec /bin/mv "$@"
"""

SPAWN_WRAPPER = """set -u
mount -t tmpfs t /home
bash "$AGENTCTL_COPY" spawn --bootstrap-path "$STUB_STATE/agent-bootstrap" "$@"
rc=$?
exit $rc
"""

TAG = "newborn-20260101000000"
FIN_WRAPPER = """set -u
mount -t tmpfs t /home
home="/home/agent-$TAG"
mkdir -p "$home/.engram" "$home/.claude/projects"
printf '{}' > "$home/.engram/config.json"
if [ -n "${PLANT_MARKER:-}" ]; then
  ln -s "$VICTIM" "$home/.engram/finalize-name-complete.json"
fi
chmod -R a+rwX "$home"
touch "$STUB_STATE/created"
bash "$AGENTCTL_COPY" finalize-name "$TAG" chosenone
rc=$?
exit $rc
"""


def _rewrite_literals(text, shared, state):
    text, n = re.subn(r'^SHARED_ROOT="[^"]*"$', f'SHARED_ROOT="{shared}"', text, flags=re.M)
    assert n == 1
    text, n = re.subn(r'^readonly AGENTCTL_STATE="[^"]*"$', f'readonly AGENTCTL_STATE="{state}"',
                      text, flags=re.M)
    assert n == 1, "the AGENTCTL_STATE literal is not in the expected form"
    return text


class Bench:
    def __init__(self, root):
        self.root = root
        self.shared = root / "shared"
        self.state = root / "var-lib" / "agentctl"      # AGENTCTL_STATE
        self.stub = root / "stub"
        self.bin = self.stub / "bin"
        self.victim_dir = root / "victim"
        self.bin.mkdir(parents=True)
        self.shared.mkdir()
        (self.shared / "health").mkdir()
        self.state.parent.mkdir()
        for p in (self.stub, self.bin, self.shared, self.state.parent):
            p.chmod(0o755)
        self.stub.chmod(0o777)  # a stranger uid logs here (V4)
        copy = self.stub / "agentctl-copy"
        copy.write_text(_rewrite_literals(AGENTCTL.read_text(), self.shared, self.state))
        self.copy = copy
        for name, body in (("id", STUB_ID), ("chown", STUB_CHOWN), ("sudo", STUB_SUDO),
                           ("usermod", STUB_USERMOD), ("setfacl", STUB_SETFACL),
                           ("groupmod", STUB_NOOP), ("loginctl", STUB_NOOP),
                           ("pkill", STUB_NOOP), ("sleep", STUB_NOOP), ("pgrep", STUB_FAIL),
                           ("getfacl", STUB_NOOP), ("mv", STUB_MV)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.stub / "empty").write_text("")
        (self.stub / "agent-bootstrap").write_text(STUB_BOOTSTRAP)
        (self.stub / "agent-bootstrap").chmod(0o755)

    def good_state(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        (self.state / "share-state").mkdir(mode=0o755)
        (self.state / "share-state").chmod(0o755)

    def run(self, wrapper, *args, **env):
        e = dict(os.environ)
        e.update({"PATH": f"{self.bin}:{os.environ['PATH']}", "STUB_STATE": str(self.stub),
                  "AGENTCTL_COPY": str(self.copy), "VIZ_OPERATOR": "operator",
                  "HOME": str(self.root / "home"), "TAG": TAG})
        (self.root / "home").mkdir(exist_ok=True)
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(["unshare", "-Urm", "bash", "-c", wrapper, "wrapper", *args],
                              capture_output=True, text=True, timeout=180, env=e)

    def read(self, name):
        p = self.stub / name
        return p.read_text() if p.exists() else ""


def _second(bench):
    d = bench.root / "second"
    d.mkdir()
    d.chmod(0o755)
    return Bench(d)


@pytest.fixture
def bench(scratch):
    return Bench(scratch)


@needs_stat
@needs_userns
class TestSpawnVerb:
    def test_V1_bad_state_directory_refuses_before_the_bootstrap_runs(self, bench):
        bench.good_state()
        ok = bench.run(SPAWN_WRAPPER)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        assert bench.read("bootstrap-called")  # positive control: a good state runs it
        # now the same verb with a group-writable state directory
        b2 = _second(bench)
        b2.good_state()
        b2.state.chmod(0o775)
        r = b2.run(SPAWN_WRAPPER)
        assert r.returncode != 0
        assert str(b2.state) in r.stderr and "775" in r.stderr
        assert b2.read("bootstrap-called") == ""
        assert not (b2.state / "share-log").exists()

    def test_V1b_a_link_for_the_state_directory_refuses_before_the_bootstrap(self, bench):
        real = bench.root / "real-state"
        real.mkdir(mode=0o755)
        (real / "share-state").mkdir(mode=0o755)
        bench.state.symlink_to(real)
        r = bench.run(SPAWN_WRAPPER)
        assert r.returncode != 0 and str(bench.state) in r.stderr
        assert bench.read("bootstrap-called") == ""
        assert not (real / "share-log").exists()

    def test_V2_audit_failure_is_a_loud_nonzero_exit_that_says_the_action_happened(self, bench):
        bench.good_state()
        ok = bench.run(SPAWN_WRAPPER)
        assert ok.returncode == 0
        log = bench.state / "share-log"
        assert log.exists() and " spawn " in log.read_text()   # positive control
        b2 = _second(bench)
        b2.good_state()
        # the bootstrap stub opens the state directory to group write AFTER the gate
        r = b2.run(SPAWN_WRAPPER, STUB_BREAK_STATE=b2.state)
        assert r.returncode != 0
        assert "spawn happened but could not be logged" in r.stderr
        assert b2.read("bootstrap-called")   # the action did happen
        assert not (b2.state / "share-log").exists()

    def test_A1_fresh_host_creates_the_state_directories_0755(self, bench):
        assert not bench.state.exists()
        r = bench.run(SPAWN_WRAPPER)
        assert r.returncode == 0, r.stdout + r.stderr
        for d in (bench.state, bench.state / "share-state"):
            assert stat.S_IMODE(d.stat().st_mode) == 0o755
        log = bench.state / "share-log"
        assert log.exists() and stat.S_IMODE(log.stat().st_mode) == 0o644

    def test_A1b_group_writable_parent_refuses_and_creates_nothing(self, bench):
        bench.state.parent.chmod(0o775)
        r = bench.run(SPAWN_WRAPPER)
        assert r.returncode != 0 and str(bench.state.parent) in r.stderr
        assert not bench.state.exists() and bench.read("bootstrap-called") == ""
        # positive control: the same layout with a good parent works
        b2 = _second(bench)
        assert b2.run(SPAWN_WRAPPER).returncode == 0

    def test_A1c_linked_parent_refuses_and_creates_nothing_through_it(self, bench):
        real_parent = bench.root / "real-parent"
        real_parent.mkdir(mode=0o755)
        real_parent.chmod(0o755)
        link_parent = bench.root / "link-parent"
        link_parent.symlink_to(real_parent)
        text = _rewrite_literals(AGENTCTL.read_text(), bench.shared, link_parent / "agentctl")
        bench.copy.write_text(text)
        r = bench.run(SPAWN_WRAPPER)
        assert r.returncode != 0 and str(link_parent) in r.stderr
        assert not (real_parent / "agentctl").exists()
        assert bench.read("bootstrap-called") == ""
        # positive control: the real parent, reached directly, works
        bench.copy.write_text(_rewrite_literals(AGENTCTL.read_text(), bench.shared,
                                                real_parent / "agentctl"))
        assert bench.run(SPAWN_WRAPPER).returncode == 0
        assert (real_parent / "agentctl").is_dir()


@needs_stat
@needs_userns
class TestFinalizeVerb:
    def _ready(self, bench, sticky=True):
        bench.good_state()
        bench.shared.chmod(0o2775)
        (bench.shared / "health").chmod(0o3775 if sticky else 0o2775)

    def _go(self, bench, **env):
        return bench.run(FIN_WRAPPER, VICTIM=bench.victim_dir / "target",
                         VICTIM_FILE=bench.victim_dir / "target", **env)

    def test_V3_health_rename_control_a_real_directory_is_renamed_and_chowned_h(self, bench):
        self._ready(bench)
        (bench.shared / "health" / TAG).mkdir()
        r = self._go(bench)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (bench.shared / "health" / "chosenone").is_dir()
        assert not (bench.shared / "health" / TAG).exists()
        assert re.search(r"chown -h agent-chosenone:agents \S+/health/chosenone", bench.read("chown.log"))

    def test_V3_symlink_at_health_tag_is_refused_and_its_target_untouched(self, bench):
        self._ready(bench)
        bench.victim_dir.mkdir()
        target = bench.victim_dir / "target"
        target.mkdir()
        (target / "keep").write_text("K")
        before = target.stat().st_mtime_ns
        (bench.shared / "health" / TAG).symlink_to(target)
        r = self._go(bench)
        assert r.returncode != 0
        assert "health" in r.stderr and TAG in r.stderr
        assert (bench.shared / "health" / TAG).is_symlink()
        assert not (bench.shared / "health" / "chosenone").exists()
        assert (target / "keep").read_text() == "K" and target.stat().st_mtime_ns == before
        assert str(target) not in bench.read("chown.log")
        assert bench.read("usermod.log") == ""    # refused before the account moved

    def test_V3_existing_destination_is_refused_and_untouched(self, bench):
        self._ready(bench)
        dest = bench.shared / "health" / "chosenone"
        dest.mkdir()
        (dest / "theirs").write_text("T")
        (bench.shared / "health" / TAG).mkdir()
        (bench.shared / "health" / TAG / "mine").write_text("M")
        r = self._go(bench)
        assert r.returncode != 0 and "chosenone" in r.stderr
        assert (dest / "theirs").read_text() == "T"
        assert not (dest / TAG).exists(), "mv A B moved A inside an existing B"
        assert (bench.shared / "health" / TAG / "mine").read_text() == "M"
        assert bench.read("usermod.log") == ""

    def test_V5_destination_planted_after_the_check_never_receives_the_source(self, bench):
        # R1: the check passed (no destination yet), then a link appears at the
        # destination before the mv runs. Plain `mv A B` would move A INSIDE the
        # link's target; `mv -T` must fail instead.
        self._ready(bench)
        (bench.shared / "health" / TAG).mkdir()
        (bench.shared / "health" / TAG / "mine").write_text("M")
        ok = self._go(bench)
        # positive control, same cell: with nothing planted the very same verb
        # renamed the directory and its contents arrived at the destination
        assert ok.returncode == 0, ok.stdout + ok.stderr
        assert (bench.shared / "health" / "chosenone" / "mine").read_text() == "M"
        b2 = _second(bench)
        self._ready(b2)
        (b2.shared / "health" / TAG).mkdir()
        (b2.shared / "health" / TAG / "mine").write_text("M")
        target = b2.victim_dir / "target"
        target.mkdir(parents=True)
        (target / "keep").write_text("K")
        before = target.stat().st_mtime_ns
        r = b2.run(FIN_WRAPPER, VICTIM=target, VICTIM_FILE=target, PLANT_DEST_LINK=target)
        assert r.returncode != 0, r.stdout + r.stderr
        assert (b2.shared / "health" / "chosenone").is_symlink()      # the plant is still there
        assert sorted(p.name for p in target.iterdir()) == ["keep"], "the source landed inside the link's target"
        assert target.stat().st_mtime_ns == before
        assert (b2.shared / "health" / TAG / "mine").read_text() == "M"   # source untouched
        assert str(target) not in b2.read("chown.log")

    def test_V6_baseline_snapshot_is_copied_as_the_newborn_and_no_root_chown_follows(self, bench):
        # R3: the copy runs behind `sudo -u`, and nothing is chowned afterwards
        self._ready(bench)
        (bench.shared / "health" / TAG).mkdir()
        r = self._go(bench)
        assert r.returncode == 0, r.stdout + r.stderr
        sudo = bench.read("sudo.log")
        assert re.search(r"sudo -u agent-chosenone\s+cp -a \S+/\.engram \S+/\.engram\.first-session-baseline\.bak", sudo), sudo
        assert "first-session-baseline" not in bench.read("chown.log")
        assert "baseline snapshot taken" in r.stdout and "WARN" not in r.stderr

    def test_V6b_a_failed_copy_warns_naming_the_path_and_the_verb_continues(self, bench):
        self._ready(bench)
        (bench.shared / "health" / TAG).mkdir()
        r = self._go(bench, FAIL_CP="1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert re.search(r"WARN.*\.engram\.first-session-baseline\.bak", r.stderr), r.stderr
        assert "baseline snapshot taken" not in r.stdout
        assert "Finalize-name complete" in r.stdout          # the verb went on to its end
        assert re.search(r"cp -a", bench.read("sudo.log"))   # and the copy WAS attempted, as the newborn

    def test_V6c_an_existing_baseline_path_is_still_refused_and_nothing_is_copied(self, bench):
        self._ready(bench)
        (bench.shared / "health" / TAG).mkdir()
        wrapper = FIN_WRAPPER.replace('touch "$STUB_STATE/created"',
                                      'mkdir "$home/.engram.first-session-baseline.bak"\ntouch "$STUB_STATE/created"')
        r = bench.run(wrapper, VICTIM=bench.victim_dir / "target")
        assert r.returncode != 0 and "refusing to snapshot" in r.stderr, r.stdout + r.stderr
        assert "cp -a" not in bench.read("sudo.log")

    def test_V3_health_not_sticky_refuses_with_the_exact_repair(self, bench):
        self._ready(bench, sticky=False)
        (bench.shared / "health" / TAG).mkdir()
        r = self._go(bench)
        assert r.returncode != 0
        assert "sudo chmod 2775" not in r.stderr   # the repair adds the bit, it never drops setgid
        assert re.search(r"sudo chmod 3775 \S+/health", r.stderr), r.stderr
        assert bench.read("usermod.log") == ""

    @needs_nested
    def test_V4_planted_marker_link_leaves_its_target_untouched(self, bench):
        self._ready(bench)
        (bench.shared / "health" / TAG).mkdir()
        bench.victim_dir.mkdir()
        bench.victim_dir.chmod(0o755)
        victim = bench.victim_dir / "target"
        victim.write_text("ORIG")
        victim.chmod(0o600)
        before = victim.stat().st_mtime_ns
        r = self._go(bench, PLANT_MARKER="1")
        assert r.returncode == 0, r.stdout + r.stderr
        assert victim.read_text() == "ORIG" and victim.stat().st_mtime_ns == before
        # positive control in the same cell: the regular marker beside the link WAS written
        sudo = bench.read("sudo.log")
        assert re.search(r"sudo -u agent-chosenone .*tee \S*\.engram-finalize-pending\.json", sudo), sudo
        assert re.search(r"sudo -u agent-chosenone .*tee \S*/\.engram/finalize-name-complete\.json", sudo), sudo
        assert "chown" not in "".join(
            l for l in bench.read("chown.log").splitlines() if "finalize-name-complete.json" in l)

    @needs_nested
    def test_V4b_regular_markers_are_written_with_the_right_content(self, bench):
        self._ready(bench)
        (bench.shared / "health" / TAG).mkdir()
        wrapper = FIN_WRAPPER + 'true\n'
        wrapper = wrapper.replace(
            'rc=$?\nexit $rc',
            'rc=$?\ncp /home/agent-chosenone/.engram/finalize-name-complete.json "$STUB_STATE/complete.copy"\n'
            'cp /home/agent-chosenone/.engram-finalize-pending.json "$STUB_STATE/pending.copy"\nexit $rc')
        r = bench.run(wrapper, VICTIM=bench.victim_dir / "target")
        assert r.returncode == 0, r.stdout + r.stderr
        import json
        assert json.loads(bench.read("complete.copy"))["chosen_name"] == "chosenone"
        assert json.loads(bench.read("pending.copy"))["viz_operator"] == "operator"


# --------------------------------------------------------------------------
# S -- the sticky predicate for $SHARED_ROOT/health (C7), by function extraction
# --------------------------------------------------------------------------

def _sticky(*args):
    text = AGENTCTL.read_text()
    fn = f"_shared_dir_sticky_ok() {{\n{_func_body(text, '_shared_dir_sticky_ok')}}}"
    return subprocess.run(["bash", "-c", fn + '\n_shared_dir_sticky_ok "$@"', "x", *args],
                          capture_output=True, text=True, timeout=30).returncode


class TestStickyPredicate:
    def test_S1_root_owned_sticky_directory_accepted(self):
        assert _sticky("0", "3775", "1", "0", "0") == 0

    def test_S2_missing_sticky_bit_refused(self):
        assert _sticky("0", "2775", "1", "0", "0") != 0

    def test_S3_link_non_directory_wrong_owner_non_numeric_refused(self):
        assert _sticky("0", "3775", "1", "1", "0") != 0
        assert _sticky("0", "3775", "0", "0", "0") != 0
        assert _sticky("1000", "3775", "1", "0", "0") != 0
        assert _sticky("0", "37x5", "1", "0", "0") != 0


# --------------------------------------------------------------------------
# B -- agent-bootstrap section 6
# --------------------------------------------------------------------------

class TestBootstrap:
    def _section6(self):
        text = BOOTSTRAP.read_text()
        m = re.search(r"# -+ 6\. community shared dirs -+\n(.*?)# -+ 7\.", text, re.S)
        assert m
        return m.group(1)

    def test_B1_audit_is_gone_and_no_chmod_remains_on_an_existing_path(self):
        s = self._section6()
        code = "\n".join(l for l in s.split("\n") if not l.lstrip().startswith("#"))
        assert "audit" not in code
        assert "$SHARED_ROOT/audit" not in BOOTSTRAP.read_text()
        assert not re.search(r"(^|\s)chmod\s", code), code
        assert not re.search(r"mkdir\s+-p\s", code), code
        for path, mode in (('"$SHARED_ROOT"', "2775"), ('"$SHARED_ROOT/inter-agent"', "2775"),
                           ('"$SHARED_ROOT/health"', "3775")):
            assert re.search(rf"mkdir -m {mode} {re.escape(path)}", code), (path, code)
        assert re.search(r'mkdir -m 0750 "\$SHARED_ROOT/health/\$NAME"', code)
        assert re.search(r'chown -h "\$USER_NAME:agents" "\$SHARED_ROOT/health/\$NAME"', code)

    def _run_section6(self, scratch, plant):
        s = self._section6()
        shared = scratch / "shared"
        shared.mkdir()
        for sub in ("inter-agent", "health"):
            (shared / sub).mkdir()
        name = "ada"
        if plant == "link":
            (shared / "health" / name).symlink_to(scratch / "victim")
        elif plant == "dir":
            (shared / "health" / name).mkdir()
        (scratch / "victim").mkdir(exist_ok=True)
        (scratch / "victim").chmod(0o700)
        script = f"""set -euo pipefail
SHARED_ROOT="{shared}"; NAME={name}; USER_NAME=$(id -un)
chgrp() {{ :; }}; chown() {{ echo "chown $*" >> "{scratch}/chown.log"; }}
chmod() {{ echo "chmod $*" >> "{scratch}/chmod.log"; command chmod "$@"; }}
{s}
echo REACHED_END
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30), shared

    def test_B1_planted_health_name_is_a_loud_failure_and_nothing_is_changed(self, scratch):
        # positive control: a clean tree runs the section through
        (scratch / "a").mkdir()
        ok, shared = self._run_section6(scratch / "a", "none")
        assert "REACHED_END" in ok.stdout, ok.stderr
        assert stat.S_IMODE((shared / "health" / "ada").stat().st_mode) == 0o750
        for plant in ("link", "dir"):
            d = scratch / plant
            d.mkdir()
            r, shared = self._run_section6(d, plant)
            assert r.returncode != 0 and "REACHED_END" not in r.stdout, (plant, r.stdout)
            assert r.stderr.strip(), "a refusal must be loud"
            # R4: a named refusal, not a bare `mkdir: File exists`
            low = r.stderr.lower()
            assert "already exists" in low and "userdel -r" in r.stderr, r.stderr
            assert "plant" in low and "nothing has been changed" in low, r.stderr
            assert str(shared / "health" / "ada") in r.stderr
            assert not (d / "chmod.log").exists(), "chmod ran on an existing path"
            assert not (d / "chown.log").exists()
            assert stat.S_IMODE((d / "victim").stat().st_mode) == 0o700

    def test_B1_existing_modes_are_never_reapplied(self, scratch):
        shared = scratch / "shared"
        shared.mkdir()
        shared.chmod(0o751)
        for sub in ("inter-agent", "health"):
            (shared / sub).mkdir()
            (shared / sub).chmod(0o751)
        s = self._section6()
        script = f"""set -euo pipefail
SHARED_ROOT="{shared}"; NAME=zed; USER_NAME=$(id -un)
chgrp() {{ :; }}; chown() {{ :; }}
{s}
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        for p in (shared, shared / "inter-agent", shared / "health"):
            assert stat.S_IMODE(p.stat().st_mode) == 0o751, p


class TestPlatformComment:
    def test_the_shared_root_comment_no_longer_claims_sticky(self):
        text = AGENTCTL.read_text()
        assert "is mode 3775" not in text
        assert "mode 2775" in text
