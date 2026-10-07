"""Contract-first tests for the agentctl spawn forum-url hint (part of #3563, slice 2b).

``agentctl spawn --forum-url URL`` leaves ONE line in the newborn's
``~/.engram/forum-url.hint``. The first-session skill offers it as the default
answer to its forum question (the user still confirms). ``boot-env`` is
operator-authored and is never written here. agentctl runs as ROOT, so nothing
derived from the URL is sourced, evaluated or executed, and the file is
written without ever following a symlink or overwriting an existing path.

Three layers, from the most direct to the most end-to-end:

  U  ``agentctl __forum-hint-url-ok`` -- the early shape check (H2).
  D  ``_write_forum_hint`` itself -- the marked region of agentctl is EXTRACTED
     into a temp script (no agentctl verb exposes the writer: a root script must
     not carry a write-capable test verb) and the function is called against a
     temp tree as the current (non-root) user: happy path, exists, symlink,
     wrong owner, failure branches, and a simulated create-between-check-and-
     publish race (a bash function named ``ln`` defined before the call shadows
     the command and creates the target at the exact moment of the publish).
  F  the whole ``spawn`` verb, run in a throwaway user+mount namespace
     (``unshare -Urm``: root inside the namespace only, ``/home`` replaced by a
     tmpfs, so no real user or real home is touched) against a copy of agentctl
     whose SHARED_ROOT line points at a temp dir. ``id``, ``stat -c %U`` on the
     newborn's ``.engram`` and ``chown`` are stubbed on PATH because the newborn
     user does not exist; ``agent-bootstrap`` is a stub that creates
     ``/home/agent-<tag>/.engram``. The F cells skip (and the skip is reported)
     where unprivileged user namespaces are unavailable.

Not covered by any cell here: the real ``agent-bootstrap``, real ``useradd``,
a real root uid, real ``chgrp agents``, and a real ``finalize-name`` run. See
the handoff.

Every subprocess takes its environment from ``_hermetic_env`` (HOME and
XDG_CONFIG_HOME pinned to a temp dir, inherited ENGRAM_HOME / FORUM_URL /
FORUM_ANSWER / FORUM_FORCE / CLAUDE_PLUGIN_ROOT dropped). The whole file is also
run under a HOME whose engram-forum.service carries ``--baton-login-map``.
"""

import atexit
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AGENTCTL = REPO / "tools" / "agentctl"

_HERMETIC_HOME = tempfile.mkdtemp(prefix="agentctl-hint-home-")
atexit.register(shutil.rmtree, _HERMETIC_HOME, ignore_errors=True)

URL = "http://192.168.1.50:5002"
ME = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
MYGROUP = subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip()


def _hermetic_env(**extra):
    env = dict(os.environ)
    for k in ("ENGRAM_HOME", "FORUM_URL", "FORUM_ANSWER", "FORUM_FORCE",
              "CLAUDE_PLUGIN_ROOT"):
        env.pop(k, None)
    env["HOME"] = _HERMETIC_HOME
    env["XDG_CONFIG_HOME"] = os.path.join(_HERMETIC_HOME, ".config")
    env.update(extra)
    return env


def _agentctl(*args, env=None, prefix=None):
    cmd = (prefix or []) + ["bash", str(AGENTCTL), *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                       env=env or _hermetic_env())
    # An internal verb that does not exist exits non-zero too; a refusal cell
    # must never pass because the verb is missing.
    assert "Unknown subcommand" not in r.stdout, f"verb {args[0]} does not exist"
    return r


# --------------------------------------------------------------------------
# U -- the shape check (H2)
# --------------------------------------------------------------------------

GOOD_URLS = [
    "http://localhost:5002",
    "https://forum.example.com:5002/x?y=1&z=2".replace("&", "%26"),
    "http://192.168.1.50:5002",
    "http://h", "https://h",
    "http://" + "a" * (2048 - 7),             # exactly 2048 bytes
]

BAD_URLS = [
    ("empty", ""),
    ("no-scheme", "localhost:5002"),
    ("ftp", "ftp://h:5002"),
    ("uppercase-scheme", "HTTP://h:5002"),
    ("http-scheme-only", "http://"),
    ("https-scheme-only", "https://"),
    ("leading-space", " http://h:5002"),
    ("space", "http://h :5002"),
    ("tab", "http://h\t:5002"),
    ("newline", "http://h:5002\nhttp://evil"),
    ("carriage-return", "http://h:5002\r"),
    ("control-char", "http://h\x01:5002"),
    ("del", "http://h\x7f:5002"),
    ("credentials", "http://user:pw@h:5002"),
    ("too-long", "http://" + "a" * (2048 - 7 + 1)),
    ("multibyte-over-limit-in-bytes", "http://" + "é" * 1021),  # 1028 chars, 2049 bytes
    ("backtick", "http://h/`id`"),
    ("dollar", "http://h/$HOME"),
    ("backslash", "http://h/a\\b"),
    ("double-quote", 'http://h/a"b'),
    ("single-quote", "http://h/a'b"),
    ("semicolon", "http://h/a;b"),
    ("ampersand", "http://h/a&b"),
    ("pipe", "http://h/a|b"),
    ("less", "http://h/a<b"),
    ("greater", "http://h/a>b"),
    ("open-paren", "http://h/a(b"),
    ("close-paren", "http://h/a)b"),
    ("open-brace", "http://h/a{b"),
    ("close-brace", "http://h/a}b"),
]


class TestUShapeCheck:
    @pytest.mark.parametrize("url", GOOD_URLS)
    def test_good_urls_pass(self, url):
        r = _agentctl("__forum-hint-url-ok", url)
        assert r.returncode == 0, r.stdout + r.stderr

    @pytest.mark.parametrize("name,url", BAD_URLS)
    def test_bad_urls_are_refused_with_a_reason(self, name, url):
        r = _agentctl("__forum-hint-url-ok", url)
        assert r.returncode != 0, f"{name} was accepted"
        assert "forum-url" in (r.stdout + r.stderr), "a refusal must say why, naming the flag"


# --------------------------------------------------------------------------
# D -- the writer, run directly against a temp tree
# --------------------------------------------------------------------------

def _tree(tmp_path):
    home = tmp_path / "home"
    (home / ".engram").mkdir(parents=True)
    return home, home / ".engram" / "forum-url.hint"


REGION_BEGIN = "# ---- spawn --forum-url hint (#3563) ----"
REGION_END = "cmd_spawn() {"


def _region():
    """The hint helpers, exactly as shipped: from their marker up to cmd_spawn."""
    text = AGENTCTL.read_text()
    assert text.count(REGION_BEGIN) == 1 and text.count("\n" + REGION_END) == 1
    a = text.index(REGION_BEGIN)
    b = text.index("\n" + REGION_END, a)
    return text[a:b] + "\n"


def _write_hint(home, url=URL, user=ME, group=MYGROUP, umask=None, pre=""):
    """Source the extracted region in a temp script (same shell options as
    agentctl) and call _write_forum_hint. ``pre`` is bash run AFTER the region
    is defined and BEFORE the call (used to shadow ``ln`` or ``chown``)."""
    script = ["set -euo pipefail"]
    if umask is not None:
        script.append(f"umask {umask}")
    script += [_region(), pre, '_write_forum_hint "$@"']
    d = Path(home).parent
    f = d / "writer-under-test.sh"
    f.write_text("\n".join(script) + "\n")
    return subprocess.run(["bash", str(f), str(home), user, group, url],
                          capture_output=True, text=True, timeout=60,
                          env=_hermetic_env())


def _entries(d):
    return sorted(p.name for p in Path(d).iterdir())


class TestDWriter:
    def test_happy_path_exact_content_mode_owner_and_no_temp_left(self, tmp_path):
        home, hint = _tree(tmp_path)
        r = _write_hint(home)
        assert r.returncode == 0, r.stdout + r.stderr
        assert hint.read_bytes() == (URL + "\n").encode()
        st = hint.lstat()
        assert stat.S_ISREG(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o600
        assert st.st_uid == os.getuid()
        assert st.st_nlink == 1
        assert _entries(hint.parent) == ["forum-url.hint"]

    def test_mode_is_0600_even_under_a_permissive_umask(self, tmp_path):
        home, hint = _tree(tmp_path)
        r = _write_hint(home, umask="000")
        assert r.returncode == 0, r.stdout + r.stderr
        assert stat.S_IMODE(hint.lstat().st_mode) == 0o600

    def test_writer_itself_refuses_an_invalid_url(self, tmp_path):
        home, hint = _tree(tmp_path)
        r = _write_hint(home, url="http://h/a;b")
        assert r.returncode != 0
        assert "forum-url" in r.stderr
        assert _entries(hint.parent) == []

    def test_existing_hint_is_never_overwritten(self, tmp_path):
        home, hint = _tree(tmp_path)
        hint.write_text("OLD\n")
        r = _write_hint(home)
        assert r.returncode != 0
        assert "forum-url.hint" in r.stderr and "WARN" in r.stderr
        assert hint.read_text() == "OLD\n"
        assert _entries(hint.parent) == ["forum-url.hint"]

    def test_symlinked_target_is_not_followed(self, tmp_path):
        home, hint = _tree(tmp_path)
        victim = tmp_path / "victim"
        victim.write_text("VICTIM\n")
        os.symlink(victim, hint)
        r = _write_hint(home)
        assert r.returncode != 0 and "WARN" in r.stderr
        assert victim.read_text() == "VICTIM\n"
        assert os.readlink(hint) == str(victim)
        assert _entries(hint.parent) == ["forum-url.hint"]

    def test_dangling_symlink_target_is_not_followed_or_created_through(self, tmp_path):
        home, hint = _tree(tmp_path)
        ghost = tmp_path / "ghost"
        os.symlink(ghost, hint)
        r = _write_hint(home)
        assert r.returncode != 0 and "WARN" in r.stderr
        assert not ghost.exists() and not ghost.is_symlink()
        assert _entries(hint.parent) == ["forum-url.hint"]

    def test_symlinked_engram_dir_is_refused(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        real = tmp_path / "elsewhere"
        real.mkdir()
        os.symlink(real, home / ".engram")
        r = _write_hint(home)
        assert r.returncode != 0
        assert "WARN" in r.stderr
        assert _entries(real) == []

    def test_symlinked_home_is_refused_even_with_a_real_owned_engram(self, tmp_path):
        # home itself is a symlink to a real directory holding a real .engram
        # owned by the caller: only the home check can refuse.
        target = tmp_path / "real-home"
        (target / ".engram").mkdir(parents=True)
        home = tmp_path / "home-link"
        os.symlink(target, home)
        r = _write_hint(home)
        assert r.returncode == 1, r.stdout + r.stderr
        assert "WARN" in r.stderr and "not a real directory" in r.stderr
        assert _entries(target / ".engram") == [], "no hint and no temp name may be left"
        assert _entries(target) == [".engram"]

    def test_missing_engram_dir_is_refused_and_not_created(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        r = _write_hint(home)
        assert r.returncode != 0 and "WARN" in r.stderr
        assert not (home / ".engram").exists()

    def test_engram_dir_not_owned_by_the_newborn_user_is_refused(self, tmp_path):
        home, hint = _tree(tmp_path)
        # chown is shadowed to succeed, so ONLY the owner check can refuse: a
        # real chown to another user would fail for a non-root caller anyway.
        r = _write_hint(home, user="nobody", pre="chown() { return 0; }")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "WARN" in r.stderr and "not owned by nobody" in r.stderr
        assert _entries(hint.parent) == [], "no file and no temp name may be left"

    def test_unresolvable_group_publishes_nothing(self, tmp_path):
        home, hint = _tree(tmp_path)
        r = _write_hint(home, group="no-such-group-xyz")
        assert r.returncode != 0
        assert "WARN" in r.stderr
        assert _entries(hint.parent) == [], "a half-owned file must never be published"

    def test_target_created_between_check_and_publish_survives(self, tmp_path):
        home, hint = _tree(tmp_path)
        # a function named ln shadows the command: the target appears with
        # RACED content at the exact moment of the publish, then the real ln
        # runs and must fail rather than replace it.
        pre = 'ln() { printf "RACED\\n" > "${@: -1}"; command ln "$@"; }'
        r = _write_hint(home, pre=pre)
        assert r.returncode == 1 and "WARN" in r.stderr, r.stdout + r.stderr
        assert hint.read_text() == "RACED\n"
        assert _entries(hint.parent) == ["forum-url.hint"], "temp name must be cleaned up"


class TestDNeverExecutes:
    def test_writer_source_does_not_source_eval_or_mv_n(self):
        text = AGENTCTL.read_text()
        m = re.search(r"^_write_forum_hint\(\) \{\n(.*?)^\}\n", text, re.S | re.M)
        assert m, "_write_forum_hint function not found"
        body = "\n".join(l for l in m.group(1).splitlines()
                         if not l.lstrip().startswith("#"))
        assert not re.search(r"(^|[\s;&|(])(source|eval)\s", body), body
        assert not re.search(r"(^|\s)\.\s+[\"$]", body), body
        assert "mv -n" not in body and not re.search(r"\bmv\b", body), \
            "publish with link(2), not mv"
        assert re.search(r"\bln\b", body)

    def test_no_test_seam_function_lives_in_the_root_script(self):
        text = AGENTCTL.read_text()
        assert "_forum_hint_before_publish" not in text


# Every `__` verb is dispatched with NO require_root and is documented as a pure
# check ("no sudo, no real users, no side effects"). A side-effecting one would be
# a root primitive wherever sudo allows `sudo agentctl <anything>`. This set is
# pinned, so adding a verb fails here and forces a review.
PURE_INTERNAL_VERBS = {
    "__codex-managed-session", "__expand-session-args", "__session-prologue",
    "__require-viz-operator", "__read-viz-operator-config",
    "__viz-operator-conf-provenance-ok",
    "__viz-operator-config-value-if-provenance-ok",
    "__resolve-viz-operator-from-config", "__viz-operator-charset-ok",
    "__validate-chosen-name", "__as-self-check", "__parse-session-flags",
    "__detect-platform-as-self", "__forum-hint-url-ok",
}


class TestInternalVerbAllowlist:
    def test_dispatcher_internal_verbs_equal_the_pinned_pure_set(self):
        text = AGENTCTL.read_text()
        verbs = set(re.findall(r"^    (__[a-z][a-z0-9-]*)\)", text, flags=re.M))
        assert verbs == PURE_INTERNAL_VERBS, (
            f"added: {sorted(verbs - PURE_INTERNAL_VERBS)}, "
            f"removed: {sorted(PURE_INTERNAL_VERBS - verbs)}; a new __ verb runs "
            "with no require_root: review that it has no side effect, then pin it")


# --------------------------------------------------------------------------
# F -- the whole spawn verb in a throwaway namespace
# --------------------------------------------------------------------------

def _userns_ok():
    if not shutil.which("unshare"):
        return False
    r = subprocess.run(
        ["unshare", "-Urm", "bash", "-c", "mount -t tmpfs t /home && echo ok"],
        capture_output=True, text=True, timeout=30)
    return r.stdout.strip() == "ok"


_USERNS = _userns_ok()
needs_userns = pytest.mark.skipif(
    not _USERNS, reason="unprivileged user+mount namespaces unavailable here")

STUB_ID = """#!/bin/bash
# id stub: agent-newborn-* exists only after the bootstrap stub ran.
for a in "$@"; do
  case "$a" in
    agent-newborn-*)
      [ -f "$STUB_STATE/created" ] || exit 1
      for b in "$@"; do [ "$b" = "-gn" ] && { echo "$a"; exit 0; }; done
      echo "uid=1(x)"; exit 0 ;;
  esac
done
exec /usr/bin/id "$@"
"""

STUB_STAT = """#!/bin/bash
# stat stub: `stat -c %U <newborn>/.engram` reports STUB_OWNER (default: the
# newborn user), because the namespace has no such user. Everything else is real.
last="${@: -1}"
case " $* " in
  *%U*)
    case "$last" in
      */.engram)
        user="$(basename "$(dirname "$last")")"
        echo "${STUB_OWNER:-$user}"; exit 0 ;;
    esac ;;
esac
exec /usr/bin/stat "$@"
"""

STUB_CHOWN = """#!/bin/bash
echo "chown $*" >> "$STUB_STATE/chown.log"
exit 0
"""

STUB_BOOTSTRAP = """#!/bin/bash
# agent-bootstrap stub: records its call and creates the newborn home + .engram.
echo "$*" >> "$STUB_STATE/bootstrap-called"
tag="$1"
mkdir -p "/home/agent-$tag/.engram"
[ -n "${STUB_PRECREATE:-}" ] && printf 'PRE\\n' > "/home/agent-$tag/.engram/forum-url.hint"
touch "$STUB_STATE/created"
exit 0
"""

WRAPPER = """set -u
mount -t tmpfs t /home
bash "$AGENTCTL_COPY" spawn --bootstrap-path "$STUB_STATE/agent-bootstrap" "$@"
rc=$?
for d in /home/agent-*; do
  [ -d "$d" ] || continue
  ls -A "$d/.engram" > "$STUB_STATE/listing" 2>/dev/null || true
  if [ -e "$d/.engram/forum-url.hint" ] || [ -L "$d/.engram/forum-url.hint" ]; then
    cp "$d/.engram/forum-url.hint" "$STUB_STATE/hint.copy"
    stat -c '%a' "$d/.engram/forum-url.hint" > "$STUB_STATE/hint.mode"
  fi
done
ls -A /home > "$STUB_STATE/homes" 2>/dev/null || true
exit $rc
"""


def _make_copy(tmp_path, source_text):
    state = tmp_path / "state"
    shared = tmp_path / "shared"
    bindir = state / "bin"
    bindir.mkdir(parents=True)
    shared.mkdir()
    new, n = re.subn(r'^SHARED_ROOT="[^"]*"$', f'SHARED_ROOT="{shared}"',
                     source_text, flags=re.M)
    assert n == 1, "expected exactly one SHARED_ROOT assignment"
    copy = state / "agentctl-copy"
    copy.write_text(new)
    for name, body in (("id", STUB_ID), ("stat", STUB_STAT), ("chown", STUB_CHOWN)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    (state / "agent-bootstrap").write_text(STUB_BOOTSTRAP)
    (state / "agent-bootstrap").chmod(0o755)
    return state, shared, copy, bindir


def _spawn(tmp_path, *args, owner=None, precreate=False, source_text=None):
    assert not str(tmp_path).startswith("/home/"), "tmp must not live under /home"
    state, shared, copy, bindir = _make_copy(
        tmp_path, source_text if source_text is not None else AGENTCTL.read_text())
    extra = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_STATE": str(state),
        "AGENTCTL_COPY": str(copy),
        "VIZ_OPERATOR": "operator",
    }
    if owner:
        extra["STUB_OWNER"] = owner
    if precreate:
        extra["STUB_PRECREATE"] = "1"
    proc = subprocess.run(
        ["unshare", "-Urm", "bash", "-c", WRAPPER, "wrapper", *args],
        capture_output=True, text=True, timeout=120, env=_hermetic_env(**extra))
    log = shared / "audit" / "share-log"
    audit = log.read_text() if log.exists() else ""
    return proc, state, audit


def _tag(proc):
    m = re.search(r"Newborn name:\s+(agent-newborn-\d+)", proc.stdout)
    assert m, proc.stdout + proc.stderr
    return m.group(1)


def _read(state, name):
    p = state / name
    return p.read_text() if p.exists() else None


# The whole stdout of a spawn WITHOUT --forum-url on today's dev, with the
# timestamped newborn name normalised. H1: it must not change by one byte.
GOLDEN_NO_FLAG = '''== agentctl spawn ==
  Newborn name:   agent-newborn-TS
  Runtime:        claude
  Bootstrap:      BOOTSTRAP
  Alpha source:   (auto-detected by agent-bootstrap)


== Spawn complete ==
The newborn is at agent-newborn-TS. To complete the birth:

  1. Enter the newborn's bash login shell:
       agentctl bash newborn-TS

  2. Inside that shell, install Claude Code in the new agent's account.
       (Follow the current Claude Code install instructions for the platform.)

  3. In the first session, ask the agent to install ENGRAM from the
     plugin marketplace:
       claude                 # At the first prompt, ask the agent to install
                              # ENGRAM from the plugin marketplace, then exit.
                              # On the next session start (after ENGRAM installs),
                              # the SessionStart hook fires engram-first-session
                              # → agent generates 3 candidate names → you pick.

  4. From the operator's shell (NOT inside the newborn shell), finalize the rename:
       sudo agentctl finalize-name newborn-TS <chosen-name>

To abandon a newborn before finalize-name (simple cleanup):
  sudo userdel -r agent-newborn-TS
'''


def _normalised(proc, state):
    out = proc.stdout
    tag = _tag(proc)
    ts = tag.removeprefix("agent-newborn-")
    out = out.replace(ts, "TS").replace(str(state / "agent-bootstrap"), "BOOTSTRAP")
    return out


@needs_userns
class TestFWholeSpawn:
    def test_no_flag_is_byte_for_byte_today(self, tmp_path):
        proc, state, audit = _spawn(tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _normalised(proc, state) == GOLDEN_NO_FLAG
        assert not re.search(r"forum|hint", proc.stdout + proc.stderr, re.I)
        assert _read(state, "hint.copy") is None
        assert "forum-url.hint" not in (_read(state, "listing") or "")
        assert _read(state, "chown.log") is None

    def test_flag_writes_one_line_hint_and_adds_exactly_one_stdout_line(self, tmp_path):
        proc, state, audit = _spawn(tmp_path, "--forum-url", URL)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        tag = _tag(proc)
        assert _read(state, "hint.copy") == URL + "\n"
        assert _read(state, "hint.mode").strip() == "600"
        assert f"{tag}:{tag}" in _read(state, "chown.log")
        norm = _normalised(proc, state)
        assert norm.startswith(GOLDEN_NO_FLAG.rstrip("\n"))
        extra = norm[len(GOLDEN_NO_FLAG.rstrip("\n")):].strip("\n").splitlines()
        assert len(extra) == 1, extra
        assert "forum-url.hint" in extra[0] and "/home/agent-newborn-TS/.engram" in extra[0]
        assert "default" in extra[0] and "first-session" in extra[0]
        assert URL not in proc.stdout

    def test_equals_form_is_the_same(self, tmp_path):
        proc, state, audit = _spawn(tmp_path, f"--forum-url={URL}")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _read(state, "hint.copy") == URL + "\n"

    def test_audit_records_forum_hint_yes_and_never_the_url(self, tmp_path):
        proc, state, audit = _spawn(tmp_path, "--forum-url", URL)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "forum_hint=yes" in audit
        assert "192.168" not in audit and "http" not in audit and URL not in audit

    def test_audit_line_without_the_flag_is_byte_identical_to_dev(self, tmp_path):
        proc, state, audit = _spawn(tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # dev format: "<stamp> spawn <user>→<tag> path=newborn runtime=claude"
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ spawn [^ ]+→newborn-\d+ "
            r"path=newborn runtime=claude\n", audit), repr(audit)

    def test_audit_line_with_flag_appends_forum_hint_after_runtime(self, tmp_path):
        proc, state, audit = _spawn(tmp_path, "--forum-url", URL)
        assert audit.endswith(" path=newborn runtime=claude forum_hint=yes\n"), repr(audit)

    def test_second_forum_url_is_an_error_before_any_side_effect(self, tmp_path):
        for args in (("--forum-url", URL, "--forum-url", URL),
                     ("--forum-url", URL, f"--forum-url={URL}")):
            sub = tmp_path / str(len(args)) / args[2][:5].strip("-")
            sub.mkdir(parents=True)
            proc, state, audit = _spawn(sub, *args)
            assert proc.returncode == 2, proc.stdout + proc.stderr
            assert "--forum-url" in proc.stderr and "more than once" in proc.stderr
            assert _read(state, "bootstrap-called") is None
            assert _read(state, "created") is None
            assert audit == ""

    def test_missing_value_is_an_error_before_any_side_effect(self, tmp_path):
        proc, state, audit = _spawn(tmp_path, "--forum-url")
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "ERROR: --forum-url" in proc.stderr and "value" in proc.stderr
        assert _read(state, "bootstrap-called") is None

    @pytest.mark.parametrize("name,url", [
        ("ftp", "ftp://h:5002"), ("credentials", "http://u:p@h:5002"),
        ("space", "http://h :5002"), ("semicolon", "http://h/a;b"),
        ("dollar", "http://h/$(id)"), ("empty", ""),
        ("http-scheme-only", "http://"), ("https-scheme-only", "https://"),
    ])
    def test_bad_url_refused_before_the_user_is_created(self, tmp_path, name, url):
        proc, state, audit = _spawn(tmp_path, "--forum-url", url)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "ERROR: --forum-url" in proc.stderr
        assert _read(state, "bootstrap-called") is None, "the user was created first"
        assert _read(state, "created") is None
        assert not (_read(state, "homes") or "").strip()
        assert audit == ""

    def test_hint_write_failure_is_a_warning_and_never_fails_the_spawn(self, tmp_path):
        proc, state, audit = _spawn(tmp_path, "--forum-url", URL, owner="somebody-else")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "== Spawn complete ==" in proc.stdout
        assert "WARN" in proc.stderr and "forum-url.hint" in proc.stderr
        assert _read(state, "hint.copy") is None
        assert audit.endswith(" runtime=claude forum_hint=no\n"), repr(audit)
        assert _read(state, "bootstrap-called") is not None
        # no NEXT-block hint line when nothing was written
        assert not re.search(r"hint was left|first-session will offer", proc.stdout)

    def test_preexisting_hint_survives_a_spawn_with_the_flag(self, tmp_path):
        proc, state, audit = _spawn(tmp_path, "--forum-url", URL, precreate=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _read(state, "hint.copy") == "PRE\n"
        assert "WARN" in proc.stderr
        assert "forum_hint=no" in audit

    def test_other_unknown_flags_are_still_rejected(self, tmp_path):
        proc, state, audit = _spawn(tmp_path, "--forum-ur", URL)
        assert proc.returncode == 2
        assert _read(state, "bootstrap-called") is None


# --------------------------------------------------------------------------
# H5 -- finalize-name: the hint lives inside ~/.engram, which the rename walks
# --------------------------------------------------------------------------

_BEGIN = "# === BEGIN finalize_name_rewrite"
_END = "# === END finalize_name_rewrite ==="


def _rewrite_script(tmp_path):
    lines = AGENTCTL.read_text().splitlines()
    b = next(i for i, l in enumerate(lines) if l.strip().startswith(_BEGIN))
    e = next(i for i, l in enumerate(lines) if l.strip() == _END)
    p = tmp_path / "rewrite.py"
    p.write_text("\n".join(lines[b + 1:e]) + "\n")
    return p


class TestH5Finalize:
    def test_hint_survives_the_path_rewrite_byte_for_byte(self, tmp_path):
        old, new = "agent-newborn-20990101000000", "agent-ada"
        home = tmp_path / old
        eng = home / ".engram"
        eng.mkdir(parents=True)
        (eng / "forum-url.hint").write_text(URL + "\n")
        (eng / "forum-url.hint").chmod(0o600)
        (eng / "seed-manifest.json").write_text(f'{{"home": "/home/{old}"}}\n')
        before_mode = stat.S_IMODE((eng / "forum-url.hint").stat().st_mode)
        r = subprocess.run(
            [sys.executable, "-I", str(_rewrite_script(tmp_path)), str(home), old, new],
            capture_output=True, text=True, timeout=60, env=_hermetic_env())
        assert r.returncode == 0, r.stdout + r.stderr
        assert (eng / "forum-url.hint").read_bytes() == (URL + "\n").encode()
        assert stat.S_IMODE((eng / "forum-url.hint").stat().st_mode) == before_mode
        # the walk did visit this directory: the neighbour WAS rewritten
        assert new in (eng / "seed-manifest.json").read_text()
        assert not any(p.name.startswith("forum-url.hint.") for p in eng.iterdir())


def test_hermetic_env_pins_home_and_drops_inherited_variables(monkeypatch):
    for k in ("ENGRAM_HOME", "FORUM_URL", "FORUM_ANSWER", "FORUM_FORCE",
              "CLAUDE_PLUGIN_ROOT"):
        monkeypatch.setenv(k, "/leak")
    monkeypatch.setenv("HOME", "/nonexistent/realhome")
    env = _hermetic_env()
    for k in ("ENGRAM_HOME", "FORUM_URL", "FORUM_ANSWER", "FORUM_FORCE",
              "CLAUDE_PLUGIN_ROOT"):
        assert k not in env
    assert env["HOME"] == _HERMETIC_HOME
    assert env["XDG_CONFIG_HOME"].startswith(_HERMETIC_HOME)
