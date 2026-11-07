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
covers that remainder by hand; the scan below is a search by pattern. G4's own
limits are stated on ``scan_root_writes``.

The share/unshare cells (TestShareVerb) run the REAL setfacl and getfacl on the
scratch tree under the fake root, with the grantee's group stood in for by the
namespace's one mapped gid. They cannot reproduce: a real second uid (so the
"swapped link" is a stand-in that swaps on the first recursive call, not a racing
process), an unreadable directory (a fake root reads every directory, so the
partial-removal cell uses a stand-in that prints the message setfacl printed for
one on a real run), a real `at` (a fake queues and prints `job 7`), or the
installed path of the tool.
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


def _acl_ok() -> bool:
    """The share/unshare cells run the real setfacl and getfacl on the scratch tree
    (a filesystem without ACL support, or no acl package, skips them, reported)."""
    if not (shutil.which("setfacl") and shutil.which("getfacl")):
        return False
    d = tempfile.mkdtemp()
    try:
        r = subprocess.run(["setfacl", "-m", f"u:{os.getuid()}:rwx", d], capture_output=True)
        g = subprocess.run(["getfacl", "-p", d], capture_output=True, text=True)
        return r.returncode == 0 and re.search(r"^user:[^:]+:rwx", g.stdout, re.M) is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)


needs_acl = pytest.mark.skipif(not _acl_ok(), reason="needs setfacl/getfacl and an ACL-capable /tmp")
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
                        "tee", "ln", "rm", "mkdir"})
# a root shell running a quoted string: the string is invisible to the scan, so
# the wrapper itself must be a named allow-list row
INDIRECT_CMDS = frozenset({"bash", "sh", "dash", "zsh", "eval", "xargs"})
NEWBORN_WRAPPERS = frozenset({"ab_as_newborn"})
NEWBORN_USER = re.compile(r'^"?\$\{?(chosen_user|USER_NAME|newborn_user)\}?"?$')
_KEEP_STR = re.compile(r'\$\{?(chosen_user|USER_NAME|newborn_user)\}?')
SKIP_WRAPPERS = frozenset({"env", "exec", "nice", "time", "nohup", "timeout", "builtin"})
KEYWORDS = ("then", "do", "else", "!", "if", "elif", "while", "until")
# the root-run regions of tools/agentctl (R2); tools/agent-bootstrap is scanned whole
AGENTCTL_REGIONS = frozenset({"cmd_spawn", "cmd_finalize_name", "cmd_share",
                              "cmd_unshare", "audit", "_write_forum_hint",
                              "_state_dir_init"})


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
    tee ln rm mkdir, or a redirect to a file, unless the command runs behind
    `sudo -u "$chosen_user"` / `"$USER_NAME"` / `"$newborn_user"` (or ab_as_newborn).
    A redirect is root's even behind sudo -u, because the shell opens it. A root
    bash/sh/eval/xargs is flagged too: its quoted body is not read. No path token
    decides anything (round 3: the first G4 read only lines naming five variables
    and missed the viz setfacl lines). `regions` limits the scan to those function
    names (None: all text). Built to be run in Python: a `$` in a double-quoted
    shell pattern would expand. Returns [(lineno, logical line, why)].

    LIMITS, stated so a green G4 is not read as more than it is. It does NOT see:
      * a command outside the list above that writes or creates: touch, mktemp,
        dd, truncate, sed -i, git, tar, rsync, useradd/usermod/groupmod/userdel,
        systemctl, a python/perl/awk body (the state-file writer in cmd_share is a
        python heredoc), or `at`/`atrm` queue changes;
      * a command named by a variable, or run through a function defined elsewhere;
      * anything outside AGENTCTL_REGIONS in tools/agentctl (agent-bootstrap is
        scanned whole);
      * which PATH an argument names: a row says why a line is acceptable, the scan
        never reads the argument;
      * a command inside a quoted string handed to bash/sh/eval (flagged, not read).
    mkdir is no longer among the limits (it was, until the #4405 follow-up)."""
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
     '( umask 022; echo "$stamp $action ${SUDO_USER:-$(id -un)}→$agent path=$path${extras:+ $extras}" >> "$AGENTCTL_STATE/share-log" )  || { echo "ERROR: $action happened but could not be logged (write to $AGENTCTL_STATE/share-log failed). Exit code $EXIT_ACTION_UNLOGGED: the action is DONE; do not repeat it, repair the state directory and add the log line by hand." >&2; return 1; }',
     "OWN: append into the root-only state directory, re-verified by _state_dir_check immediately before"),
    ("agentctl",
     'mkdir -m 0755 "$d" || { echo "ERROR: could not create $d" >&2; return 1; }',
     "OWN: _state_dir_init creates the root-only state directories: no -p, so a name that appeared meanwhile is a loud EEXIST and never a followed path; only when the name is absent; the parent was just verified root-owned, non-link and without group or other write, and the result is re-checked by _state_dir_check right after"),
    ("agentctl",
     'setfacl -m "g:$user:--x" "$parent"  || { echo "ERROR: setfacl --x failed on \'$parent\'" >&2; _ttl_job_undo "$at_job"; exit 1; }',
     "OPERATOR: cmd_share parent walk, an ancestor of the operator-chosen path. NOT closed by -P or by the read-back (those cover the leaf): a swapped MIDDLE component stays open and this PR does not close it (a known residual, declared, not a guard)"),
    ("agentctl",
     'setfacl -m "u:$SUDO_USER:r-x" "$parent"  || echo "WARNING: preserve u:$SUDO_USER:r-x failed on \'$parent\'" >&2',
     "OPERATOR: cmd_share parent walk, same ancestors and same residual as the row above: a swapped MIDDLE component stays open and this PR does not close it"),
    ("agentctl",
     'setfacl -R -P -m "g:$user:$acl_perm" "$path"  || { echo "ERROR: setfacl $acl_perm failed on \'$path\'" >&2; _ttl_job_undo "$at_job"; exit 1; }',
     "OPERATOR: cmd_share on an operator-chosen path resolved by readlink -f. The LEAF is closed by -P (a link swapped in at the argument takes no entry) plus the getfacl -p read-back on $path itself, which refuses when the entry is absent; a trailing slash is refused before this line. A swapped MIDDLE component stays open and this PR does not close it"),
    ("agentctl",
     'setfacl -R -P -d -m "g:$user:$acl_perm" "$path"  || { echo "ERROR: default ACL setfacl failed on \'$path\'" >&2; _ttl_job_undo "$at_job"; exit 1; }',
     "OPERATOR: cmd_share default ACL, same operator-chosen path: the leaf is closed by -P plus the read-back of the default entry; a swapped MIDDLE component stays open and this PR does not close it"),
    ("agentctl",
     'setfacl -R -P -n -x "g:$user" "$path" || rm_rc=1',
     "OPERATOR: cmd_unshare on an operator-chosen path resolved by readlink -f (the fallback strips a trailing slash). The LEAF is closed by -P plus the read-back that the entry is gone; -n keeps the mask; a partial failure is reported, never swallowed. A swapped MIDDLE component stays open and this PR does not close it"),
    ("agentctl",
     'setfacl -R -P -d -n -x "g:$user" "$path" || rm_rc=1',
     "OPERATOR: cmd_unshare default ACL: same operator-chosen path, same leaf closure (-P plus read-back), -n, loud partial failure; a swapped MIDDLE component stays open and this PR does not close it"),
    ("agentctl",
     'setfacl -n -x "g:$user" "$parent" 2>/dev/null || true',
     "OPERATOR: cmd_unshare parent GC, ancestors of the operator-chosen path (skipped when an older share record exists); -n keeps the mask. The same MIDDLE-component residual as the cmd_share parent walk: stays open and this PR does not close it"),
    ("agent-bootstrap", 'chmod 0755 "$cfgdir"',
     "CREATED: $cfgdir was made by mktemp -d a few lines up (root-owned, random name, in TMPDIR, normally sticky /tmp)"),
    ("agent-bootstrap",
     'printf \'[safe]\\n\\tdirectory = "%s"\\n\' "$esc" > "$cfg" && chmod 0644 "$cfg" || rc=$?',
     "CREATED: $cfg is a new file inside the root-owned $cfgdir, which no agent can write"),
    ("agent-bootstrap", 'rm -rf -- "$cfgdir"   # success or failure: the trust entry never outlives the clone command',
     "CREATED: removes the root-owned mktemp directory made above"),
    ("agent-bootstrap", 'mkdir -p "$USER_HOME/.runtime"', "CREATED: " + _NB),
    ("agent-bootstrap", 'mkdir -p "$USER_HOME/.local/bin"', "CREATED: " + _NB),
    ("agent-bootstrap", 'mkdir -p "$ENGRAM_HOME"',
     "CREATED on a fresh spawn: " + _NB + ". Residual, not a guard: this script also runs against a home that already exists, where "
     "an account with a live process could have replaced a component of $USER_HOME/.engram by a link; mkdir -p then creates "
     "only a MISSING leaf (it never changes an existing entry and writes no data), and the setfacl rows below carry the same "
     "re-bootstrap residual"),
    ("agent-bootstrap",
     '[[ -e "$SHARED_ROOT" || -L "$SHARED_ROOT" ]] || mkdir -m 2775 "$SHARED_ROOT"',
     "CREATED: only when the name is absent, no -p (a name that appears after the test is a loud EEXIST, never a followed path), "
     "mode set at creation and never re-applied; the parent is /home, which only root can write"),
    ("agent-bootstrap",
     '[[ -e "$SHARED_ROOT/inter-agent" || -L "$SHARED_ROOT/inter-agent" ]] || mkdir -m 2775 "$SHARED_ROOT/inter-agent"',
     "CREATED: only when absent, no -p, mode at creation only; mkdir does not follow a link at its final component, so a planted name is an EEXIST in a directory agents can write"),
    ("agent-bootstrap",
     '[[ -e "$SHARED_ROOT/health" || -L "$SHARED_ROOT/health" ]] || mkdir -m 3775 "$SHARED_ROOT/health"',
     "CREATED: only when absent, no -p, mode at creation only; same reasoning as inter-agent above"),
    ("agent-bootstrap", 'mkdir -m 0750 "$SHARED_ROOT/health/$NAME"',
     "CREATED: no -p; a planted name was refused loudly just above and a name planted after that is an EEXIST; the parent is the sticky root-owned health directory, in which one account cannot replace another's entry"),
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
    def test_G1_the_unlogged_action_exit_value_is_used_by_no_other_exit(self):
        text = AGENTCTL.read_text()
        m = re.search(r"^readonly EXIT_ACTION_UNLOGGED=(\d+)$", text, re.M)
        assert m, "the constant is not in its expected form"
        value = m.group(1)
        code = [l for _n, l, _f in _logical_lines(text)]
        literal = [l for l in code if re.search(rf"\b(exit|return)\s+[\"']?{value}[\"']?(?![\w$])", l)]
        assert literal == [], f"a literal exit {value} bypasses the constant: {literal}"
        # and no other exit in the file uses that value
        others = {x for l in code for x in re.findall(r"\bexit\s+(\d+)\b", l)}
        assert value not in others
        assert int(value) not in (0, 1, 2, 3) and others, (value, others)
        # the constant is what the four verbs exit with (spawn, finalize-name, share x2, unshare x3)
        assert sum('exit "$EXIT_ACTION_UNLOGGED"' in l for l in code) >= 6

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
            'mkdir -p "$d"',
            'rm -f "$whatever"',
            'ln -s "$a" "$b"',
            'install -m 0644 "$a" "$b"',
            'echo x | tee "$f"',
            'bash -c "chmod 777 $d"',
            'sudo -u root chmod 777 "$d"',
            'sudo -u "$chosen_user" echo hi > "$chosen_home/f"',
        ])
        assert len(scan_root_writes(planted)) == 19
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
            'sudo -u "$newborn_user" mkdir "$d"',
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

    def test_G4_control_mkdir_is_a_root_write_and_a_newborn_mkdir_is_not(self):
        assert [h[2] for h in scan_root_writes('mkdir -p "$d"')] == ["root mkdir"]
        assert scan_root_writes('sudo -u "$newborn_user" mkdir "$d"') == []

    def test_G4_the_forum_hint_writer_is_a_root_region_and_its_writes_are_all_newborn_run(self):
        text = AGENTCTL.read_text()
        assert "_write_forum_hint" in AGENTCTL_REGIONS
        body = _func_body(text, "_write_forum_hint")
        for cmd in ("tee", "chmod", "ln", "rm"):
            assert f'sudo -u "$newborn_user" {cmd}' in body, cmd
        assert [h for h in scan_root_writes(text, {"_write_forum_hint"})] == []

    def test_G4_mutant_a_root_chmod_planted_in_the_forum_hint_writer_goes_red(self):
        text = AGENTCTL.read_text()
        assert _scan_file("agentctl", text)[0] == []          # positive control: clean as shipped
        old = '    local tmp="" why=""\n'
        assert text.count(old) == 1
        planted = text.replace(old, old + '    chmod 0600 "$target"\n', 1)
        unallowed, _ = _scan_file("agentctl", planted)
        assert [u for u in unallowed if u[1] == 'chmod 0600 "$target"'], unallowed

    @pytest.mark.parametrize("cmd", ["tee", "chmod", "ln", "rm"])
    def test_G4_mutant_a_forum_hint_write_turned_root_goes_red(self, cmd):
        text = AGENTCTL.read_text()
        assert _scan_file("agentctl", text)[0] == []
        needle = f'sudo -u "$newborn_user" {cmd}'
        assert needle in text
        mutated = text.replace(needle, cmd, 1)
        assert mutated != text
        unallowed, _ = _scan_file("agentctl", mutated)
        assert [u for u in unallowed if re.search(rf"\b{cmd}\b", u[1])], (cmd, unallowed)

    @pytest.mark.parametrize("name,exact", [(f, x) for f, x, _w in ALLOW
                                            if "mkdir" in x and "setfacl" not in x])
    def test_G4_mutant_each_mkdir_row_rewritten_to_another_root_write_goes_red(self, name, exact):
        path = AGENTCTL if name == "agentctl" else BOOTSTRAP
        text = path.read_text()
        assert _scan_file(name, text)[0] == [] and _scan_file(name, text)[1] == []   # control: clean
        assert text.count(exact) == 1, exact
        mutated = text.replace(exact, exact.replace("mkdir", "chmod 0777", 1))
        unallowed, stale = _scan_file(name, mutated)
        assert unallowed and stale, "the row bound by exact line text did not notice a different root write"

    def test_F8_the_queued_job_runs_a_fixed_tool_path_never_dollar_zero(self):
        text = AGENTCTL.read_text()
        assigns = [l for l in text.split("\n") if re.match(r"^\s*(readonly\s+)?AGENTCTL_TOOL=", l)]
        assert assigns == ['readonly AGENTCTL_TOOL="/usr/local/bin/agentctl"'], assigns
        body = "\n".join(l for l in _func_body(text, "cmd_share").split("\n")
                         if not l.lstrip().startswith("#"))
        assert '"$AGENTCTL_TOOL"' in body
        assert "$0" not in body and "${0" not in body

    def test_F1_F9_every_recursive_setfacl_has_P_and_every_removal_has_n(self):
        text = AGENTCTL.read_text()
        for fn in ("cmd_share", "cmd_unshare"):
            for _ln, line, func in _logical_lines("\n".join([f"{fn}() {{", _func_body(text, fn), "}"])):
                for m in re.finditer(r"(?<![\w./-])setfacl\s+([^|;&)]*)", line):
                    toks = m.group(1).split()
                    if any(t == "-R" for t in toks):
                        assert "-P" in toks, (fn, line)
                    if "-x" in toks and fn == "cmd_unshare":
                        assert "-n" in toks, (fn, line)
        # control: the scan of a planted pair sees both omissions
        planted = 'f() {\n    setfacl -R -m g:a:r "$p"\n    setfacl -R -P -x g:a "$p"\n}\n'
        bad = []
        for _ln, line, _f in _logical_lines(planted):
            for m in re.finditer(r"(?<![\w./-])setfacl\s+([^|;&)]*)", line):
                toks = m.group(1).split()
                if "-R" in toks and "-P" not in toks:
                    bad.append("P")
                if "-x" in toks and "-n" not in toks:
                    bad.append("n")
        assert bad == ["P", "n"]

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
[ -n "${STUB_BREAK_STATE:-}" ] && chmod g+w "$STUB_BREAK_STATE"
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


def _rewrite_literals(text, shared, state, tool=None):
    text, n = re.subn(r'^SHARED_ROOT="[^"]*"$', f'SHARED_ROOT="{shared}"', text, flags=re.M)
    assert n == 1
    text, n = re.subn(r'^readonly AGENTCTL_STATE="[^"]*"$', f'readonly AGENTCTL_STATE="{state}"',
                      text, flags=re.M)
    assert n == 1, "the AGENTCTL_STATE literal is not in the expected form"
    if tool is not None:
        text, n = re.subn(r'^readonly AGENTCTL_TOOL="[^"]*"$', f'readonly AGENTCTL_TOOL="{tool}"',
                          text, flags=re.M)
        assert n == 1, "the AGENTCTL_TOOL literal is not in the expected form"
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
        assert r.returncode == 1          # a failed gate is exit 1, never the exit 8 of an unlogged action
        assert str(b2.state) in r.stderr and "775" in r.stderr
        assert "chmod" in r.stderr        # the state directory itself keeps its concrete repair
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
        assert r.returncode == 8          # F3/G1: the action happened, the log write failed
        assert "spawn happened but could not be logged" in r.stderr
        assert "exit code 8" in r.stderr.lower()
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
        assert r.returncode == 1 and str(bench.state.parent) in r.stderr
        assert not bench.state.exists() and bench.read("bootstrap-called") == ""
        # positive control: the same layout with a good parent works
        b2 = _second(bench)
        assert b2.run(SPAWN_WRAPPER).returncode == 0

    @pytest.mark.parametrize("shape", ["group-writable", "link", "missing"])
    def test_F4_the_parent_is_a_system_directory_and_its_message_offers_no_chmod_or_chown(self, bench, shape):
        # F4: a refusal about /var/lib must say "investigate", never hand the operator a
        # command that rewrites a system directory. The state directory itself keeps
        # its concrete repair (V1 asserts chmod there).
        parent = bench.state.parent
        if shape == "group-writable":
            parent.chmod(0o775)
        elif shape == "link":
            real = bench.root / "real-parent"
            real.mkdir(mode=0o755)
            real.chmod(0o755)
            parent.rmdir()
            parent.symlink_to(real)
        else:
            parent.rmdir()
        r = bench.run(SPAWN_WRAPPER)
        assert r.returncode == 1 and str(parent) in r.stderr, r.stderr
        low = r.stderr.lower()
        assert "investigate" in low and "system directory" in low, r.stderr
        for word in ("chmod", "chown", "install -d", "sudo rm", "sudo mv"):
            assert word not in low, (word, r.stderr)
        assert bench.read("bootstrap-called") == ""

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

    def test_V2_finalize_name_audit_failure_is_exit_8_and_a_failed_gate_is_exit_1(self, bench):
        self._ready(bench)
        (bench.shared / "health" / TAG).mkdir()
        r = self._go(bench, STUB_BREAK_STATE=bench.state)
        assert r.returncode == 8, r.stdout + r.stderr
        assert "finalize-name happened but could not be logged" in r.stderr
        assert "exit code 8" in r.stderr.lower()
        assert bench.read("usermod.log"), "the rename did happen"
        # a failed gate stays exit 1, before anything moves
        b2 = _second(bench)
        self._ready(b2)
        b2.state.chmod(0o775)
        (b2.shared / "health" / TAG).mkdir()
        r2 = b2.run(FIN_WRAPPER, VICTIM=b2.victim_dir / "target", VICTIM_FILE=b2.victim_dir / "target")
        assert r2.returncode == 1, r2.stdout + r2.stderr
        assert b2.read("usermod.log") == ""

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
# SH -- share / unshare under the fake root, with the REAL setfacl and getfacl
# --------------------------------------------------------------------------
# The sandbox maps one uid and one gid, so the grantee's group is stood in for by
# gid 0 (the fake root's own gid, the only gid the namespace knows). The wrappers
# below rename agent-grantee <-> 0 on the way in and out and leave every ACL
# ancestor outside the scratch tree alone (they belong to the host's root); all
# else is the real tool. Two stand-ins ACT on the call, to put the verb in a
# position a one-process bench cannot reach by chance:
#   SWAP_AT/SWAP_TO   on the first recursive call, the named directory is replaced by
#                     a link to SWAP_TO: the race the leaf cannot rule out by test
#   SETFACL_*         NOOP (exit 0, applied nothing; _DEFAULT / _ACCESS: only the -d / only
#                     the non-d recursive calls), FAIL, PARTIAL (applied it, then
#                     the error setfacl prints for a directory it could not list --
#                     measured on a real unreadable directory by the security seat;
#                     a fake root can read every directory, so it cannot be reproduced)
STUB_ID_SHARE = """#!/bin/bash
for a in "$@"; do [ "$a" = agent-grantee ] && exit 0; done
exec /usr/bin/id "$@"
"""

STUB_SETFACL_SH = """#!/bin/bash
echo "setfacl $*" >> "$STUB_STATE/setfacl.log"
last="${@: -1}"
case "$last" in "$BENCH_ROOT"/*) ;; *) exit 0 ;; esac
rec=0; dfl=0; rm=0
for a in "$@"; do
  case "$a" in -R) rec=1 ;; -d) dfl=1 ;; -x) rm=1 ;; esac
done
[ -n "${BREAK_STATE:-}" ] && chmod g+w "$BREAK_STATE"
if [ "$rec" = 1 ] && [ -n "${SWAP_AT:-}" ] && [ ! -e "$STUB_STATE/swapped" ]; then
  touch "$STUB_STATE/swapped"
  /bin/mv "$SWAP_AT" "$SWAP_AT.moved" && ln -s "$SWAP_TO" "$SWAP_AT"
fi
if [ "$rec" = 1 ]; then
  [ "${SETFACL_NOOP:-}" = 1 ] && exit 0
  [ "${SETFACL_NOOP_DEFAULT:-}" = 1 ] && [ "$dfl" = 1 ] && exit 0
  [ "${SETFACL_NOOP_ACCESS:-}" = 1 ] && [ "$dfl" = 0 ] && exit 0
  [ "${SETFACL_FAIL:-}" = 1 ] && { echo "setfacl: simulated failure" >&2; exit 1; }
  [ "${SETFACL_FAIL_DEFAULT:-}" = 1 ] && [ "$dfl" = 1 ] && { echo "setfacl: simulated failure" >&2; exit 1; }
fi
if [ "$rec" = 0 ] && [ "$rm" = 0 ] && [ "${SETFACL_FAIL_ANCESTOR:-}" = 1 ]; then
  case "$*" in *g:agent-grantee:--x*) echo "setfacl: simulated failure" >&2; exit 1 ;; esac
fi
args=(); for a in "$@"; do args+=("${a//agent-grantee/0}"); done
want=0
case "${SETFACL_PARTIAL:-}" in
  1) want=1 ;;
  access) [ "$dfl" = 0 ] && want=1 ;;
  default) [ "$dfl" = 1 ] && want=1 ;;
esac
if [ "$want" = 1 ] && [ "$rec" = 1 ] && [ "$rm" = 1 ]; then
  /usr/bin/setfacl "${args[@]}"
  echo "setfacl: $last/blocked: Permission denied" >&2
  exit 1
fi
exec /usr/bin/setfacl "${args[@]}"
"""

STUB_GETFACL_SH = """#!/bin/bash
/usr/bin/getfacl "$@" | sed -e 's/group:root:/group:agent-grantee:/'
exit ${PIPESTATUS[0]}
"""

STUB_AT = """#!/bin/bash
body="$(cat)"
echo "at $*" >> "$STUB_STATE/at.calls"
echo "setfacl_calls_before=$([ -f "$STUB_STATE/setfacl.log" ] && wc -l < "$STUB_STATE/setfacl.log" || echo 0)" >> "$STUB_STATE/at.calls"
if [ "${AT_MODE:-accept}" = reject ]; then
  echo "syntax error. Last token seen: h" >&2
  echo "Garbled time" >&2
  exit 1
fi
echo "$body" >> "$STUB_STATE/at.queue"
echo "warning: commands will be executed using /bin/sh" >&2
echo "job 7 at Thu Jan  1 00:00:00 2026" >&2
exit 0
"""

STUB_ATRM = """#!/bin/bash
echo "atrm $*" >> "$STUB_STATE/atrm.log"
[ "${ATRM_FAIL:-}" = 1 ] && exit 1
exit 0
"""

STUB_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$STUB_STATE/systemctl.calls"
[ "${ATD_MODE:-active}" = inactive ] && exit 3
exit 0
"""

STUB_READLINK = """#!/bin/bash
[ "${READLINK_FAIL:-}" = 1 ] && exit 1
[ -n "${READLINK_SLASH:-}" ] && [ "$1" = -f ] && { echo "${2}/"; exit 0; }
exec /usr/bin/readlink "$@"
"""

SHARE_WRAPPER = 'bash "$AGENTCTL_COPY" "$@"\n'


class ShareBench(Bench):
    def __init__(self, root):
        super().__init__(root)
        self.tool = self.stub / "agentctl-fixed"       # the fixed install path, rewritten into the copy
        self.tool.write_text("#!/bin/bash\nexit 0\n")
        self.tool.chmod(0o755)
        self.copy.write_text(_rewrite_literals(AGENTCTL.read_text(), self.shared, self.state,
                                               tool=self.tool))
        for name, body in (("id", STUB_ID_SHARE), ("setfacl", STUB_SETFACL_SH),
                           ("getfacl", STUB_GETFACL_SH), ("at", STUB_AT), ("atrm", STUB_ATRM),
                           ("readlink", STUB_READLINK), ("systemctl", STUB_SYSTEMCTL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.data = root / "data"
        self.data.mkdir()
        self.data.chmod(0o755)
        self.good_state()

    def tree(self, name):
        d = self.data / name
        d.mkdir()
        d.chmod(0o755)
        (d / "child").write_text("c")
        (d / "sub").mkdir()
        (d / "sub" / "leaf").write_text("l")
        return d

    def verb(self, *args, **env):
        env.setdefault("BENCH_ROOT", self.root)
        return self.run(SHARE_WRAPPER, *args, **env)

    def no_at_path(self):
        """A PATH holding every real tool and every stub EXCEPT at/atrm: `command -v at`
        must fail while the rest of the verb still runs."""
        return self.path_without("at", "atq", "atrm", "batch", dirname="noat-bin")

    def path_without(self, *names, dirname="without-bin"):
        """A PATH holding every real tool and every stub except the named ones."""
        d = self.root / dirname
        d.mkdir()
        skip = set(names)
        for src in (self.bin, Path("/usr/bin"), Path("/bin")):
            for f in src.iterdir():
                if f.name in skip or (d / f.name).exists():
                    continue
                (d / f.name).symlink_to(f.resolve() if src != self.bin else f)
        return str(d)


def _second_sh(bench):
    d = bench.root / "second"
    d.mkdir()
    d.chmod(0o755)
    return ShareBench(d)


@pytest.fixture
def sb(scratch):
    return ShareBench(scratch)


def _named(path):
    """Every NAMED group entry of <path> itself (access and default), read by the real
    getfacl from outside the namespace: what the grantee's gid actually holds there."""
    r = subprocess.run(["/usr/bin/getfacl", "-p", "--", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return [l.split("\t")[0] for l in r.stdout.splitlines()
            if re.match(r"^(default:)?group:[^:]+:", l)]


def _acl_text(path):
    r = subprocess.run(["/usr/bin/getfacl", "-p", "--", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _acl_set(args, path):
    """Plant an ACL fact as the (unprivileged) owner, outside the namespace."""
    r = subprocess.run(["/usr/bin/setfacl", *args, "--", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _access(entries):
    return [e for e in entries if e.startswith("group:")]


def _default(entries):
    return [e for e in entries if e.startswith("default:group:")]


def _record(bench):
    return bench.state / "share-state" / "grantee.json"


@needs_stat
@needs_userns
@needs_acl
class TestShareVerb:
    # ---- F1: -P with a read-back (leaf closed; a swapped MIDDLE component stays open) ----
    def test_F1_a_real_directory_takes_the_acl_and_a_link_swapped_in_leaves_the_target_clean(self, sb):
        d = sb.tree("real")
        r = sb.verb("share", "grantee", d)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Shared" in r.stdout
        # control: the real directory, its children and its default entry all took it
        for p in (d, d / "child", d / "sub", d / "sub" / "leaf"):
            assert _access(_named(p)), p
        assert _default(_named(d)) and _default(_named(d / "sub"))
        assert _record(sb).exists()

        b2 = _second_sh(sb)
        d2 = b2.tree("real")
        t = b2.tree("target")
        r2 = b2.verb("share", "grantee", d2, SWAP_AT=d2, SWAP_TO=t)
        assert r2.returncode != 0, r2.stdout + r2.stderr
        assert str(d2) in r2.stderr and "Shared" not in r2.stdout
        for p in (t, t / "child", t / "sub", t / "sub" / "leaf"):
            assert _named(p) == [], (p, _named(p))      # the target's own entries, access
        assert _default(_named(t)) == [] and _default(_named(t / "sub")) == []
        assert not _record(b2).exists()
        assert not (b2.state / "share-log").exists()

    def test_F1_a_setfacl_that_exits_0_and_applied_nothing_is_refused_by_the_read_back(self, sb):
        d = sb.tree("a")
        r = sb.verb("share", "grantee", d, SETFACL_NOOP="1")
        assert r.returncode != 0 and str(d) in r.stderr, r.stdout + r.stderr
        assert "Shared" not in r.stdout
        assert not _record(sb).exists() and not (sb.state / "share-log").exists()
        # the default entry has its own read-back: the access entry lands, the default does not
        b2 = _second_sh(sb)
        d2 = b2.tree("a")
        r2 = b2.verb("share", "grantee", d2, SETFACL_NOOP_DEFAULT="1")
        assert r2.returncode != 0 and str(d2) in r2.stderr and "default" in r2.stderr.lower(), r2.stderr
        assert "Shared" not in r2.stdout
        assert not _record(b2).exists() and not (b2.state / "share-log").exists()
        # and the access entry has its own: the default lands, the access entry does not
        b4 = ShareBench(_mk_root(b2, "four"))
        d4 = b4.tree("a")
        r4 = b4.verb("share", "grantee", d4, SETFACL_NOOP_ACCESS="1")
        assert r4.returncode != 0 and str(d4) in r4.stderr and "does not carry g:" in r4.stderr, r4.stderr
        assert "Shared" not in r4.stdout
        assert not _record(b4).exists() and not (b4.state / "share-log").exists()
        # control: neither stand-in, the same verb shares
        b3 = _second_sh(b2)
        assert b3.verb("share", "grantee", b3.tree("a")).returncode == 0

    def test_F1b_a_path_ending_in_a_slash_is_refused_and_the_targets_entry_stays_clean(self, sb):
        # setfacl 2.3.2 -R -P on a LINK WITH A TRAILING SLASH puts the entry on the target
        # (measured by the filer). readlink -f never prints one, so a stand-in does.
        t = sb.tree("target")
        link = sb.data / "link"
        link.symlink_to(t)
        r = sb.verb("share", "grantee", link, READLINK_SLASH="1")
        assert r.returncode != 0, r.stdout + r.stderr
        assert "Shared" not in r.stdout and "trailing slash" in r.stderr.lower()
        assert _named(t) == [] and _named(t / "child") == [], "the entry went through link/"
        assert not _record(sb).exists()
        # control: without the stand-in the same link is resolved to the target and shared
        b2 = _second_sh(sb)
        t2 = b2.tree("target")
        link2 = b2.data / "link"
        link2.symlink_to(t2)
        r2 = b2.verb("share", "grantee", link2)
        assert r2.returncode == 0, r2.stderr
        assert _access(_named(t2))

    # ---- F8: a share with --ttl has a scheduled removal or does not happen ----
    def test_F8_an_accepted_time_queues_a_job_for_the_fixed_tool_before_the_grant(self, sb):
        d = sb.tree("a")
        r = sb.verb("share", "grantee", d, "--ttl", "2hours")
        assert r.returncode == 0, r.stdout + r.stderr
        queue = sb.read("at.queue").strip().splitlines()
        assert queue == [f"'{sb.tool}' unshare 'grantee' '{d}'"], queue     # the fixed path, never $0
        assert str(sb.copy) not in sb.read("at.queue")
        assert "now + 2hours" in sb.read("at.calls")
        assert "scheduled" in r.stdout
        import json
        assert json.loads(_record(sb).read_text())[0]["ttl"] == "2hours"
        assert "ttl=2hours" in (sb.state / "share-log").read_text()
        # the job was queued BEFORE the first ACL change of any kind
        assert "setfacl_calls_before=0" in sb.read("at.calls")
        assert sb.read("setfacl.log")
        # control: no --ttl, no at call, ttl recorded as none
        b2 = _second_sh(sb)
        r2 = b2.verb("share", "grantee", b2.tree("a"))
        assert r2.returncode == 0 and b2.read("at.calls") == ""
        assert json.loads(_record(b2).read_text())[0]["ttl"] is None

    def test_F8_a_rejected_time_is_a_loud_refusal_and_nothing_is_granted_or_logged(self, sb):
        # control in the same cell: an accepted time on a sibling bench grants and logs
        ok = sb.verb("share", "grantee", sb.tree("ok"), "--ttl", "2hours")
        assert ok.returncode == 0 and _record(sb).exists()
        b2 = _second_sh(sb)
        d = b2.tree("a")
        r = b2.verb("share", "grantee", d, "--ttl", "2hours", AT_MODE="reject")
        assert r.returncode != 0, r.stdout + r.stderr
        assert "Garbled time" in r.stderr              # at's own error is shown, not discarded
        assert "hours" in r.stderr and "days" in r.stderr   # the accepted forms are named
        assert "Shared" not in r.stdout and "scheduled" not in r.stdout
        assert _named(d) == [] and _named(d.parent) == []
        assert b2.read("setfacl.log") == "", "an ACL call ran before the job was queued"
        assert not _record(b2).exists() and not (b2.state / "share-log").exists()
        assert b2.read("at.queue") == ""

    def test_F8_a_missing_at_is_a_loud_refusal_naming_the_forms_and_nothing_is_granted(self, sb):
        d = sb.tree("a")
        r = sb.verb("share", "grantee", d, "--ttl", "2hours", PATH=sb.no_at_path())
        assert r.returncode != 0, r.stdout + r.stderr
        assert "'at'" in r.stderr and "hours" in r.stderr and "days" in r.stderr, r.stderr
        assert "Shared" not in r.stdout
        assert _named(d) == [] and sb.read("setfacl.log") == ""
        assert not _record(sb).exists() and not (sb.state / "share-log").exists()
        # control: the same PATH without --ttl needs no at and shares
        b2 = _second_sh(sb)
        assert b2.verb("share", "grantee", b2.tree("a"), PATH=b2.no_at_path()).returncode == 0

    def test_F8_a_grant_that_fails_after_the_job_was_queued_removes_the_job_or_says_it_remains(self, sb):
        d = sb.tree("a")
        r = sb.verb("share", "grantee", d, "--ttl", "2hours", SETFACL_FAIL="1")
        assert r.returncode != 0, r.stdout + r.stderr
        assert "atrm 7" in sb.read("atrm.log")                  # the job was removed
        assert not _record(sb).exists() and not (sb.state / "share-log").exists()
        b2 = _second_sh(sb)
        r2 = b2.verb("share", "grantee", b2.tree("a"), "--ttl", "2hours", SETFACL_FAIL="1", ATRM_FAIL="1")
        assert r2.returncode != 0
        assert re.search(r"job 7.*remain", r2.stderr, re.S), r2.stderr   # it says the job remains
        assert not _record(b2).exists()

    def test_F8_a_ttl_share_whose_read_back_refuses_does_not_leave_a_queued_job_unmentioned(self, sb):
        d = sb.tree("a")
        r = sb.verb("share", "grantee", d, "--ttl", "2hours", SETFACL_NOOP="1")
        assert r.returncode != 0
        assert "atrm 7" in sb.read("atrm.log")
        assert not _record(sb).exists()

    # ---- H1 (#4410 review B1): the at job line is POSIX single-quoted, never printf %q ----
    # dash has no $'...': a %q word holding a control character and an apostrophe is read as
    # `$` plus a plain quoted string, and the text after \' runs as code (as root, from at).
    PLANTED_NAMES = ["\t'; touch MARK; #", "it's", "q'; touch MARK; #",
                     "nl\n'; touch MARK; #", "$(touch MARK)"]

    @staticmethod
    def _run_job(shell, job, cwd, rec):
        env = dict(os.environ, REC=str(rec))
        return subprocess.run([shell, "-c", job], cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=60)

    @pytest.mark.parametrize("idx", range(5))
    def test_H1_the_generated_at_job_runs_a_planted_name_as_data_under_every_sh(self, sb, scratch, idx):
        name = self.PLANTED_NAMES[idx]
        d = sb.data / name
        d.mkdir()
        d.chmod(0o755)
        # the recording stub stands in for the fixed tool path the job runs
        sb.tool.write_text('#!/bin/sh\nprintf \'%s\\0\' "$@" > "$REC"\n')
        sb.tool.chmod(0o755)
        r = sb.verb("share", "grantee", d, "--ttl", "2hours")
        assert r.returncode == 0, r.stdout + r.stderr
        job = sb.read("at.queue")
        assert job.strip(), "the fake at received no job text"
        shells = ["/bin/sh"]
        if shutil.which("dash"):
            shells.append("dash")          # by name, in addition to whatever /bin/sh is
        else:
            import warnings
            warnings.warn("dash not found: %q positive control NOT exercised on this host; "
                          "a green run here is not evidence the injection was refused")
        for shell in shells:
            work = Path(tempfile.mkdtemp(dir=scratch))
            rec = work / "rec"
            res = self._run_job(shell, job, work, rec)
            assert res.returncode == 0, (shell, repr(name), res.stdout + res.stderr)
            assert not (work / "MARK").exists(), (shell, repr(name), "the name ran as code")
            got = rec.read_bytes().split(b"\0")[:-1]
            assert got == [b"unshare", b"grantee", os.fsencode(os.path.realpath(d))], (shell, got)

    @pytest.mark.skipif(shutil.which("dash") is None, reason="dash not found: %q positive control NOT exercised on this host")
    def test_H1_control_the_same_line_built_with_percent_q_does_create_the_marker_under_dash(self, sb, scratch):
        name = self.PLANTED_NAMES[0]
        work = Path(tempfile.mkdtemp(dir=scratch))
        stub = work / "tool"
        stub.write_text('#!/bin/sh\nexit 0\n')
        stub.chmod(0o755)
        job = subprocess.run(["bash", "-c", 'printf "%q %q %q %q\\n" "$1" unshare agent-x "$2"', "x", str(stub), name],
                             capture_output=True, text=True, check=True).stdout
        self._run_job("dash", job, work, work / "rec")
        assert (work / "MARK").exists(), "the %q control did not reproduce the injection on this dash"

    # ---- F3/G1: exit 8 for "the action happened, the log write failed"; a failed gate stays 1 ----
    def test_F3_share_and_unshare_exit_3_when_the_log_write_fails_and_1_at_the_gate(self, sb):
        d = sb.tree("a")
        r = sb.verb("share", "grantee", d, BREAK_STATE=sb.state)
        assert r.returncode == 8, r.stdout + r.stderr
        assert "share happened but could not be logged" in r.stderr
        assert "exit code 8" in r.stderr.lower()
        assert _access(_named(d)), "the action did happen"
        assert "Shared" not in r.stdout
        # unshare: share cleanly first, then break the state directory
        b2 = _second_sh(sb)
        d2 = b2.tree("a")
        assert b2.verb("share", "grantee", d2).returncode == 0
        r2 = b2.verb("unshare", "grantee", d2, BREAK_STATE=b2.state)
        assert r2.returncode == 8, r2.stdout + r2.stderr
        assert "unshare happened but could not be logged" in r2.stderr
        assert _named(d2) == [], "the removal did happen"
        # a failed gate stays exit 1, whichever verb
        for verb in ("share", "unshare"):
            b3 = _second_sh(b2) if verb == "share" else ShareBench(_mk(b2, "g2"))
            b3.state.chmod(0o775)
            r3 = b3.verb(verb, "grantee", b3.tree("a"))
            assert r3.returncode == 1, (verb, r3.stdout + r3.stderr)

    def test_F3_agentctl_help_states_the_exit_code_near_the_verbs(self, sb):
        r = sb.verb("help")
        assert r.returncode == 0
        low = r.stdout.lower()
        i = low.index("exit 8")
        assert i > low.index("finalize-name <newborn-id>"), "exit 8 must follow the verb list"
        window = low[max(0, i - 300):i + 500]
        assert "log" in window and "spawn" in window and "finalize-name" in window
        assert "share" in window and "unshare" in window

    # ---- F5: share-status names the old location ----
    def test_F5_share_status_names_the_old_records_as_where_to_look(self, sb):
        r = sb.verb("share-status")
        assert r.returncode == 0, r.stderr
        assert f"{sb.shared}/share-state/<agent>.json" in r.stdout
        assert "may still be in force" in r.stdout

    # ---- unshare ----
    def _shared(self, sb, name="a"):
        d = sb.tree(name)
        assert sb.verb("share", "grantee", d).returncode == 0
        return d

    def test_F1_unshare_a_link_swapped_in_leaves_the_targets_entries_and_is_refused(self, sb):
        d = self._shared(sb, "real")
        r = sb.verb("unshare", "grantee", d)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Unshared" in r.stdout
        for p in (d, d / "child", d / "sub", d / "sub" / "leaf"):      # control: the real one is cleaned
            assert _named(p) == [], (p, _named(p))
        b2 = _second_sh(sb)
        d2 = self._shared(b2, "real")
        t = self._shared(b2, "target")
        before = {p: _named(p) for p in (t, t / "child", t / "sub")}
        assert _access(before[t]) and _default(before[t])
        r2 = b2.verb("unshare", "grantee", d2, SWAP_AT=d2, SWAP_TO=t)
        assert r2.returncode != 0, r2.stdout + r2.stderr
        assert "Unshared" not in r2.stdout and str(d2) in r2.stderr
        for p, e in before.items():
            assert _named(p) == e, (p, _named(p))        # the target's own entries, access and default
        assert _record(b2).exists()

    def test_F1_unshare_read_back_refuses_when_the_entry_is_still_there(self, sb):
        d = self._shared(sb)
        r = sb.verb("unshare", "grantee", d, SETFACL_NOOP="1")
        assert r.returncode != 0 and str(d) in r.stderr, r.stdout + r.stderr
        assert "Unshared" not in r.stdout
        import json
        assert json.loads(_record(sb).read_text()), "the record must stay while the entry does"
        b2 = _second_sh(sb)
        d2 = self._shared(b2)
        r2 = b2.verb("unshare", "grantee", d2, SETFACL_NOOP_DEFAULT="1")
        assert r2.returncode != 0 and "default" in r2.stderr.lower(), r2.stderr
        assert "Unshared" not in r2.stdout
        b3 = _second_sh(b2)
        d3 = self._shared(b3)
        r3 = b3.verb("unshare", "grantee", d3, SETFACL_NOOP_ACCESS="1")
        assert r3.returncode != 0 and "still carries g:" in r3.stderr and str(d3) in r3.stderr, r3.stderr
        assert "Unshared" not in r3.stdout

    def test_F1b_unshare_a_raw_path_ending_in_a_slash_never_removes_through_the_link(self, sb):
        t = self._shared(sb, "target")
        link = sb.data / "link"
        link.symlink_to(t)
        before = _named(t)
        assert _access(before)
        # readlink failing is the branch that keeps the operator's raw path
        r = sb.verb("unshare", "grantee", f"{link}/", READLINK_FAIL="1")
        assert r.returncode != 0, r.stdout + r.stderr
        assert "Unshared" not in r.stdout
        assert _named(t) == before and _named(t / "child") != [], "the entry was removed through link/"

    def test_F2_an_old_share_record_skips_the_ancestor_clean_up_and_warns_both_ways(self, sb):
        for kind in ("file", "dangling-link"):
            b = _second_sh(sb) if kind == "dangling-link" else sb
            d = self._shared(b)
            assert _access(_named(b.data)), "the share put a traversal entry on the ancestor"
            old = b.shared / "share-state"
            old.mkdir()
            if kind == "file":
                (old / "grantee.json").write_text("{not json at all")    # nothing is read from it
            else:
                os.symlink(b.root / "nowhere", old / "grantee.json")
            r = b.verb("unshare", "grantee", d)
            assert r.returncode == 0, (kind, r.stdout + r.stderr)
            assert _named(d) == [], "the target's own ACL is still removed"
            assert _access(_named(b.data)), (kind, "the ancestor entry was cleaned despite the old record")
            assert "WARN" in r.stderr and "getfacl" in r.stderr and "older shares" in r.stderr, r.stderr
            assert "Unshared" in r.stdout
        # control: no old record, the ancestor entry is cleaned and there is no warning
        b3 = ShareBench(_mk_root(sb, "ctl"))
        d3 = self._shared(b3)
        r3 = b3.verb("unshare", "grantee", d3)
        assert r3.returncode == 0
        assert _named(b3.data) == [] and "older shares" not in r3.stderr

    # ---- F9: removal keeps the mask; a partial removal is loud ----
    @pytest.mark.parametrize("which", ["access", "default", "ancestor"])
    def test_F9_a_removal_never_recalculates_the_mask(self, sb, which):
        d = self._shared(sb)
        if which == "access":
            f = d / "child"
            f.chmod(0o600)                               # the mask becomes ---
            probe, key = f, "mask::"
        elif which == "default":
            _acl_set(["-d", "-m", "m::---"], d)
            probe, key = d, "default:mask::"
        else:
            _acl_set(["-m", "m::---"], sb.data)
            probe, key = sb.data, "mask::"
        def mask(p):
            return [l for l in _acl_text(p).splitlines() if l.startswith(key)]
        before = mask(probe)
        assert before == [key + "---"], before          # the planted narrow mask
        mode0 = os.stat(probe).st_mode
        r = sb.verb("unshare", "grantee", d)
        assert r.returncode == 0, r.stdout + r.stderr
        assert mask(probe) == before, (which, mask(probe))
        assert os.stat(probe).st_mode == mode0, (which, oct(os.stat(probe).st_mode))
        if which == "ancestor":
            assert _access(_named(sb.data)) == [], "the clean-up of this ancestor did run"

    @pytest.mark.parametrize("which", ["1", "access", "default"])
    def test_F9_a_partial_removal_is_non_zero_named_and_never_says_unshared(self, sb, which):
        # "access" / "default": only that one of the two recursive removals fails, so a
        # swallowed exit status on either call has its own observable
        d = self._shared(sb)
        r = sb.verb("unshare", "grantee", d, SETFACL_PARTIAL=which)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "Unshared" not in r.stdout
        assert "partial" in r.stderr.lower() and str(d) in r.stderr
        assert "blocked" in r.stderr, "setfacl's own message is passed through"
        import json
        assert json.loads(_record(sb).read_text()), "a half-removed share keeps its record"
        assert "unshare-partial" in (sb.state / "share-log").read_text()
        # control: the same verb without the stand-in is a clean unshare
        b2 = _second_sh(sb)
        d2 = self._shared(b2)
        assert b2.verb("unshare", "grantee", d2).returncode == 0

    # ---- Round 2 ----
    # G3: every _ttl_job_undo call site has a behaviour cell of its own. Each case fails the
    # grant at ONE place after the job was queued; the cell reddens if THAT call is removed.
    @pytest.mark.parametrize("site,env,says", [
        ("ancestor --x", {"SETFACL_FAIL_ANCESTOR": "1"}, "setfacl --x failed"),
        ("access setfacl", {"SETFACL_FAIL": "1"}, "failed on"),
        ("default setfacl", {"SETFACL_FAIL_DEFAULT": "1"}, "default ACL setfacl failed"),
        ("access read-back", {"SETFACL_NOOP_ACCESS": "1"}, "does not carry g:"),
        ("default read-back", {"SETFACL_NOOP_DEFAULT": "1"}, "no default entry"),
    ])
    def test_G3_every_undo_site_takes_the_queued_job_back_out(self, sb, site, env, says):
        d = sb.tree("a")
        r = sb.verb("share", "grantee", d, "--ttl", "2hours", **env)
        assert r.returncode != 0 and says in r.stderr, (site, r.stdout + r.stderr)
        assert "Shared" not in r.stdout
        assert "atrm 7" in sb.read("atrm.log"), (site, "the queued job was not taken back out")
        assert "was removed" in r.stderr
        assert not _record(sb).exists() and not (sb.state / "share-log").exists()
        # the other branch of the undo, at the same site: atrm fails, the job is said to remain
        b2 = _second_sh(sb)
        r2 = b2.verb("share", "grantee", b2.tree("a"), "--ttl", "2hours", ATRM_FAIL="1", **env)
        assert r2.returncode != 0 and re.search(r"job 7.*remains", r2.stderr, re.S), (site, r2.stderr)
        # control: with none of the failures the same share is complete and no job is removed
        b3 = _second_sh(b2)
        assert b3.verb("share", "grantee", b3.tree("a"), "--ttl", "2hours").returncode == 0
        assert b3.read("atrm.log") == ""

    # G4b: a closed grammar for --ttl, before at is called
    @pytest.mark.parametrize("ttl", ["0minutes", "00hours", "2h", "2 hours", "-1days", "1hour+1day",
                                     "2hours; touch x", "2hours\nnow", "now", "2HOURS", "1.5hours", ""])
    def test_G4b_a_ttl_outside_the_grammar_is_refused_before_at_is_called(self, sb, ttl):
        ok = sb.verb("share", "grantee", sb.tree("ok"), "--ttl", "2hours")      # control, same cell
        assert ok.returncode == 0 and "now + 2hours" in sb.read("at.calls")
        b2 = _second_sh(sb)
        d = b2.tree("a")
        r = b2.verb("share", "grantee", d, "--ttl", ttl)
        assert r.returncode != 0, (repr(ttl), r.stdout + r.stderr)
        assert b2.read("at.calls") == "", (repr(ttl), "at was called with a ttl outside the grammar")
        assert "hours" in r.stderr and "days" in r.stderr and "weeks" in r.stderr, r.stderr
        assert "Shared" not in r.stdout and _named(d) == [] and b2.read("setfacl.log") == ""
        assert not _record(b2).exists() and not (b2.state / "share-log").exists()

    @pytest.mark.parametrize("ttl", ["1minute", "30minutes", "2hours", "1hour", "3days", "1week", "2weeks"])
    def test_G4b_every_form_of_the_grammar_reaches_at_verbatim(self, sb, ttl):
        r = sb.verb("share", "grantee", sb.tree("a"), "--ttl", ttl)
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"now + {ttl}" in sb.read("at.calls")

    def test_G4b_an_atd_that_is_not_active_refuses_the_ttl_share_and_grants_nothing(self, sb):
        ok = sb.verb("share", "grantee", sb.tree("ok"), "--ttl", "2hours")      # control: active
        assert ok.returncode == 0 and "is-active" in sb.read("systemctl.calls")
        assert "atd" in sb.read("systemctl.calls")
        b2 = _second_sh(sb)
        d = b2.tree("a")
        r = b2.verb("share", "grantee", d, "--ttl", "2hours", ATD_MODE="inactive")
        assert r.returncode != 0 and "atd" in r.stderr, r.stdout + r.stderr
        assert b2.read("at.calls") == "" and b2.read("setfacl.log") == ""
        assert "Shared" not in r.stdout and _named(d) == []
        assert not _record(b2).exists() and not (b2.state / "share-log").exists()
        # no --ttl: atd is not consulted and the share goes through
        b3 = _second_sh(b2)
        r3 = b3.verb("share", "grantee", b3.tree("a"), ATD_MODE="inactive")
        assert r3.returncode == 0 and b3.read("systemctl.calls") == ""
        # no systemctl on the host: the check is skipped, at is still the arbiter
        b4 = _second_sh(b3)
        r4 = b4.verb("share", "grantee", b4.tree("a"), "--ttl", "2hours",
                     PATH=b4.path_without("systemctl"), ATD_MODE="inactive")
        assert r4.returncode == 0, r4.stdout + r4.stderr
        assert "now + 2hours" in b4.read("at.calls") and b4.read("systemctl.calls") == ""

    # G2: unshare's state-record rewrite after the removal
    @pytest.mark.parametrize("record", ["{not json", '[{"mode": "rw"}]'])
    def test_G2_a_state_record_that_cannot_be_rewritten_exits_8_and_says_the_removal_happened(self, sb, record):
        d = self._shared(sb)
        _record(sb).write_text(record)           # first rewrite breaks, or (second param) the read of REMAINING
        r = sb.verb("unshare", "grantee", d)
        assert r.returncode == 8, r.stdout + r.stderr
        assert _named(d) == [], "the removal did happen"
        assert "removal" in r.stderr and "happened" in r.stderr and "8" in r.stderr, r.stderr
        assert "Unshared" not in r.stdout
        # control: a well-formed record unshares and exits 0
        b2 = _second_sh(sb)
        d2 = self._shared(b2)
        r2 = b2.verb("unshare", "grantee", d2)
        assert r2.returncode == 0 and "Unshared" in r2.stdout

    # G1: a refusal before the action is never the unlogged-action code
    def test_G1_a_refusal_before_the_action_is_not_exit_8(self, sb):
        r = sb.verb("share", "grantee", sb.tree("a"), "--mode", "bogus")
        assert r.returncode == 2
        r2 = sb.verb("share", "grantee", sb.data / "no-such-dir")
        assert r2.returncode == 1
        r3 = sb.verb("spawn", VIZ_OPERATOR="")
        assert r3.returncode not in (0, 8), r3.stdout + r3.stderr
        assert "happened" not in r3.stderr

    # G5b: a link at $path after the removals is a refusal, with a positive control
    def test_G5b_unshare_refuses_a_link_at_the_path_even_when_its_target_has_no_entry(self, sb):
        d = self._shared(sb, "real")
        bare = sb.tree("bare")                       # a target that never had the entry
        assert _named(bare) == []
        r = sb.verb("unshare", "grantee", d, SWAP_AT=d, SWAP_TO=bare)
        assert r.returncode != 0, r.stdout + r.stderr
        assert "Unshared" not in r.stdout and str(d) in r.stderr and "symbolic link" in r.stderr
        assert _record(sb).exists() and json_len(_record(sb)) == 1, "the record must stay"
        assert _named(bare) == [] and _named(bare / "child") == []
        # control: no swap, the same verb unshares and drops the record
        b2 = _second_sh(sb)
        d2 = self._shared(b2, "real")
        r2 = b2.verb("unshare", "grantee", d2)
        assert r2.returncode == 0 and "Unshared" in r2.stdout
        assert json_len(_record(b2)) == 0


def json_len(path):
    import json
    return len(json.loads(path.read_text()))


def _mk_root(bench, name):
    d = bench.root / name
    d.mkdir()
    d.chmod(0o755)
    return d


def _mk(bench, name):
    return _mk_root(bench, name)


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
