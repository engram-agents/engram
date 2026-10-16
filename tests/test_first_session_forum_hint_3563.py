"""Contract-first tests for the first-session forum-url HINT (part of #3563, slice 2b).

``agentctl spawn --forum-url URL`` leaves ``~/.engram/forum-url.hint``. Step 5.7
of the first-session skill reads it ONCE as the default answer, the user still
confirms, and the hint is deleted after the ask is answered. The hint is never
sourced, never read by a gate or hook, and never makes "configured" true.

Every runnable instruction in the skill is a marked fence, and every cell here
builds its command FROM THE SKILL TEXT exactly as the prose says, then runs it:

  # forum-url-step           the block (reads the answer FILE; #3571 removed the
                             answer template, the URL is never shell text)
  # forum-url-force          only when the user asks to overwrite (before the block)
  # forum-url-hint-consume   run in its OWN Bash call after ANY answer,
                             including "no forum" and "give a different URL"

Why a fourth fence: the merged prose said that for answer (c) "you do not run
the block again", but the hint has to be consumed on (c) too, otherwise it
resurfaces on a rerun. The consume fence resolves that contradiction, and a lint
cell pins that the (c) paragraph names it.

Every subprocess takes its environment from ``_hermetic_env`` and the file is
also run under a HOME whose engram-forum.service carries ``--baton-login-map``.
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "src" / "engram" / "skills" / "claude" / "engram-first-session" / "SKILL.md"
TOOL = REPO / "src" / "engram" / "tools" / "set_forum_url.py"

STEP, FORCE, CONSUME = (
    "# forum-url-step", "# forum-url-force", "# forum-url-hint-consume")

LOCAL = "http://localhost:5002"
LAN = "http://192.168.1.50:5002"
HINTED = "http://10.9.8.7:5002"
BASE = {"mode": "single", "self_lineage": "anthropic:opus"}
PLAIN_NOT_SET = "forum.url is not set (ask the question; answer (c) leaves it unset)"
SKIP_LINE = ("set_forum_url.py is not installed here "
             "(no forum components on this install): skip this step")


def offered_line(url):
    return (f"forum.url is not set (ask the question; the operator who created "
            f"this account suggested {url}: offer it as the default)")


_HERMETIC_HOME = tempfile.mkdtemp(prefix="first-session-hint-home-")
atexit.register(shutil.rmtree, _HERMETIC_HOME, ignore_errors=True)


def _hermetic_env(**extra):
    env = dict(os.environ)
    for k in ("ENGRAM_HOME", "FORUM_URL", "FORUM_ANSWER", "FORUM_FORCE",
              "CLAUDE_PLUGIN_ROOT"):
        env.pop(k, None)
    env["HOME"] = _HERMETIC_HOME
    env["XDG_CONFIG_HOME"] = os.path.join(_HERMETIC_HOME, ".config")
    env.update(extra)
    return env


# ---- the skill text -------------------------------------------------------

def _skill_text():
    return SKILL.read_text(encoding="utf-8")


def _step57(text=None):
    text = text if text is not None else _skill_text()
    m = re.search(r"^## Step 5\.7 .*?(?=^## )", text, flags=re.MULTILINE | re.DOTALL)
    assert m, "no Step 5.7 section"
    return m.group(0)


def _fences(section):
    return re.findall(r"^```bash\n(.*?)^```", section, flags=re.MULTILINE | re.DOTALL)


def _fence(marker):
    found = [f for f in _fences(_step57()) if marker in f]
    assert len(found) == 1, f"expected exactly one {marker!r} fence, found {len(found)}"
    return found[0]


def _prose():
    return re.sub(r"^```.*?^```", "", _step57(), flags=re.MULTILINE | re.DOTALL)


# ---- the harness ----------------------------------------------------------

class Seat:
    """One temp ENGRAM_HOME plus a plugin root holding (or not) the real tool."""

    def __init__(self, tmp_path, config=BASE, hint=None, tool=True, hint_symlink_to=None):
        self.tmp = tmp_path
        self.home = tmp_path / "engram-home"
        self.home.mkdir()
        self.cfg = self.home / "config.json"
        if config is not None:
            self.cfg.write_text(json.dumps(config, indent=2) + "\n")
        self.hint = self.home / "forum-url.hint"
        if hint is not None:
            self.hint.write_text(hint)
        if hint_symlink_to is not None:
            os.symlink(hint_symlink_to, self.hint)
        root = tmp_path / "plugin-root"
        (root / "tools").mkdir(parents=True)
        if tool:
            os.symlink(TOOL, root / "tools" / "set_forum_url.py")
        self.env = _hermetic_env(ENGRAM_HOME=str(self.home), CLAUDE_PLUGIN_ROOT=str(root))
        self.cfg_before = self.cfg.read_bytes() if config is not None else None

    def _run(self, script):
        return subprocess.run(["bash", "-c", script], env=self.env, cwd=str(self.tmp),
                              capture_output=True, text=True, timeout=60)

    def block(self):
        """First run: the block alone, no answer yet."""
        return self._run(_fence(STEP))

    def answer(self, url, force=False):
        """Write the answer file the way a file tool does (data, no shell), then run
        [force fence,] the block: one call. The URL never enters the script text."""
        with open(self.home / "forum-url.answer", "wb") as f:
            f.write(url.encode("utf-8") + b"\n")
        script = ""
        if force:
            script += _fence(FORCE) + "\n"
        return self._run(script + _fence(STEP))

    def consume(self):
        """The consume fence, in its OWN bash call, as the prose says."""
        return self._run(_fence(CONSUME))

    def url(self):
        return json.loads(self.cfg.read_text()).get("forum", {}).get("url")

    def hint_exists(self):
        return self.hint.exists() or self.hint.is_symlink()


# --------------------------------------------------------------------------
# H6 / H7 -- the block reads the hint and OFFERS it; it never applies it
# --------------------------------------------------------------------------

class TestOffer:
    def test_valid_hint_no_forum_url_prints_one_line_naming_it(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        r = s.block()
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().splitlines() == [offered_line(HINTED)]

    def test_offering_writes_nothing_and_consumes_nothing(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        r = s.block()
        assert r.returncode == 0, r.stderr
        assert s.cfg.read_bytes() == s.cfg_before, "the hint must never apply itself"
        assert s.hint.read_text() == HINTED + "\n"

    def test_hint_never_applies_itself_even_with_no_config_keys_at_all(self, tmp_path):
        s = Seat(tmp_path, config={}, hint=HINTED + "\n")
        r = s.block()
        assert s.cfg.read_bytes() == s.cfg_before
        assert "forum" not in json.loads(s.cfg.read_text())

    def test_only_the_first_line_is_used(self, tmp_path):
        s = Seat(tmp_path, hint=f"{HINTED}\nhttp://evil.example:1\n")
        r = s.block()
        assert r.stdout.strip().splitlines() == [offered_line(HINTED)]
        assert "evil" not in r.stdout

    def test_surrounding_whitespace_on_the_first_line_is_stripped(self, tmp_path):
        s = Seat(tmp_path, hint=f"  {HINTED}  \r\n")
        assert s.block().stdout.strip().splitlines() == [offered_line(HINTED)]

    def test_hint_of_exactly_the_cap_is_still_offered(self, tmp_path):
        u = "http://" + "a" * (4096 - 7)
        s = Seat(tmp_path, hint=u + "\n")
        assert s.block().stdout.strip().splitlines() == [offered_line(u)]

    def test_single_character_after_the_scheme_is_offered(self, tmp_path):
        s = Seat(tmp_path, hint="http://h\n")
        assert s.block().stdout.strip().splitlines() == [offered_line("http://h")]

    def test_fifo_at_the_hint_path_returns_promptly_and_is_invalid(self, tmp_path):
        s = Seat(tmp_path)
        os.mkfifo(s.hint)
        proc = subprocess.Popen(
            ["bash", "-c", _fence(STEP)], env=s.env, cwd=str(tmp_path),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.communicate()
            pytest.fail("the block blocked on a FIFO at the hint path")
        assert proc.returncode == 0, err
        assert PLAIN_NOT_SET in out
        assert "forum-url.hint" in out and "ignoring" in out
        assert "suggested" not in out
        assert s.consume().returncode == 0 and not s.hint_exists()

    def test_https_hint_is_accepted(self, tmp_path):
        u = "https://forum.example.org:5002"
        s = Seat(tmp_path, hint=u + "\n")
        assert s.block().stdout.strip().splitlines() == [offered_line(u)]

    @pytest.mark.parametrize("name,content", [
        ("ftp", "ftp://h:5002\n"),
        ("no-scheme", "localhost:5002\n"),
        ("empty-file", ""),
        ("blank-first-line", f"\n{HINTED}\n"),
        ("garbage", "hello world\n"),
        ("inner-space", "http://h x\n"),
        ("uppercase-scheme", "HTTP://h:5002\n"),
        ("http-scheme-only", "http://\n"),
        ("https-scheme-only", "https://\n"),
        ("over-the-length-cap", "http://" + "a" * (4097 - 7 + 1) + "\n"),
        ("over-the-length-cap-no-newline", "http://" + "a" * 6000),
    ])
    def test_non_http_first_line_is_reported_not_offered(self, tmp_path, name, content):
        s = Seat(tmp_path, hint=content)
        r = s.block()
        assert r.returncode == 0, r.stderr
        assert "suggested" not in r.stdout, f"{name} was offered"
        assert PLAIN_NOT_SET in r.stdout
        assert "forum-url.hint" in r.stdout and "ignoring" in r.stdout
        assert s.cfg.read_bytes() == s.cfg_before

    def test_hint_content_is_never_executed_or_sourced(self, tmp_path):
        sentinel = tmp_path / "SENTINEL"
        s = Seat(tmp_path, hint=f"http://h:5002/$(touch {sentinel})\n")
        r = s.block()
        assert r.returncode == 0, r.stderr
        assert not sentinel.exists()
        (tmp_path / "b").mkdir()
        s2 = Seat(tmp_path / "b", hint=f"http://h:5002/`touch {sentinel}`\n")
        s2.block()
        assert not sentinel.exists()

    def test_block_text_never_sources_or_evals_the_hint(self):
        block = "\n".join(l for l in _fence(STEP).splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r"(^|[\s;&|(])(source|eval)\s", block)
        assert not re.search(r"(^|\s)\.\s+[\"$]", block)
        assert "forum-url.hint" in block

    def test_forum_url_env_is_still_never_a_default(self, tmp_path):
        s = Seat(tmp_path)
        s.env["FORUM_URL"] = "http://env.example:5002"
        r = s.block()
        assert r.stdout.strip() == PLAIN_NOT_SET
        assert s.cfg.read_bytes() == s.cfg_before


# --------------------------------------------------------------------------
# H9 -- no hint: byte-identical to the merged Step 5.7
# --------------------------------------------------------------------------

class TestAbsentHint:
    def test_not_set_is_the_plain_question_line_only(self, tmp_path):
        r = Seat(tmp_path).block()
        assert r.stdout == PLAIN_NOT_SET + "\n"

    def test_already_set_line_only(self, tmp_path):
        r = Seat(tmp_path, config=dict(BASE, forum={"url": LOCAL})).block()
        assert r.stdout == f"forum.url is already set: {LOCAL} (skip the question)\n"

    def test_tool_absent_prints_only_the_skip_line(self, tmp_path):
        r = Seat(tmp_path, tool=False).block()
        assert r.stdout.strip() == SKIP_LINE

    def test_answer_run_without_hint_is_unchanged(self, tmp_path):
        s = Seat(tmp_path)
        r = s.answer(LAN)
        assert s.url() == LAN and "exit status: 0" in r.stdout


# --------------------------------------------------------------------------
# H8 -- the hint is consumed after the ask is answered, whatever the answer
# --------------------------------------------------------------------------

class TestConsume:
    def test_use_the_hinted_url(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        assert s.block().stdout.strip().splitlines() == [offered_line(HINTED)]
        r = s.answer(HINTED)                 # the user confirmed the hinted URL
        assert s.url() == HINTED, r.stdout + r.stderr
        c = s.consume()
        assert c.returncode == 0, c.stderr
        assert not s.hint_exists()

    def test_give_a_different_url(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        s.block()
        s.answer(LAN)
        assert s.url() == LAN
        assert s.consume().returncode == 0
        assert not s.hint_exists()
        assert s.url() == LAN

    def test_no_forum_none(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        s.block()
        # answer (c): the block is NOT run again; only the consume fence runs
        c = s.consume()
        assert c.returncode == 0, c.stderr
        assert not s.hint_exists()
        assert s.cfg.read_bytes() == s.cfg_before

    def test_none_then_two_reruns_ask_the_plain_question_with_no_hint(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        s.block()
        s.consume()
        for _ in range(2):
            r = s.block()
            assert r.stdout == PLAIN_NOT_SET + "\n", r.stdout
            assert "suggested" not in r.stdout and HINTED not in r.stdout
        assert not s.hint_exists()
        assert s.cfg.read_bytes() == s.cfg_before

    def test_a_refused_answer_still_consumes_when_the_fence_runs(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        r = s.answer("http://h:0")
        assert "exit status: 2" in r.stdout
        assert s.consume().returncode == 0
        assert not s.hint_exists()

    def test_session_dies_before_the_answer_hint_stays_for_the_next_run(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        s.block()                            # asked, never answered, no consume
        r = s.block()                        # the next first-session run
        assert r.stdout.strip().splitlines() == [offered_line(HINTED)]
        assert s.hint.read_text() == HINTED + "\n"

    def test_consume_removes_the_hint_and_nothing_else(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        (s.home / "knowledge.db").write_text("DB")
        (s.home / "forum-url.hint.bak").write_text("B")
        (s.home / "boot-env").write_text("X=1\n")
        s.consume()
        assert sorted(p.name for p in s.home.iterdir()) == [
            "boot-env", "config.json", "forum-url.hint.bak", "knowledge.db"]
        assert s.cfg.read_bytes() == s.cfg_before

    def test_consume_without_a_hint_is_a_quiet_success(self, tmp_path):
        s = Seat(tmp_path)
        c = s.consume()
        assert c.returncode == 0 and c.stdout.strip() == "" and c.stderr == ""

    def test_consume_of_a_symlinked_hint_removes_the_link_not_the_target(self, tmp_path):
        target = tmp_path / "precious"
        target.write_text("KEEP\n")
        s = Seat(tmp_path, hint_symlink_to=target)
        assert s.consume().returncode == 0
        assert not s.hint_exists()
        assert target.read_text() == "KEEP\n"

    def test_consume_fence_body_is_only_a_guarded_rm_of_the_hint(self):
        body = [l for l in _fence(CONSUME).splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
        text = "\n".join(body)
        assert "forum-url.hint" in text and "rm -f --" in text
        assert not re.search(r"rm\s+(-\w*r|-\w*R|--recursive)", text)
        assert "*" not in text, "no glob: the fence deletes the hint and nothing else"


class TestStaleHint:
    def test_already_set_consumes_a_stale_hint_and_says_so(self, tmp_path):
        s = Seat(tmp_path, config=dict(BASE, forum={"url": LOCAL}), hint=HINTED + "\n")
        r = s.block()
        assert r.returncode == 0, r.stderr
        lines = r.stdout.strip().splitlines()
        assert lines[0] == f"forum.url is already set: {LOCAL} (skip the question)"
        assert lines[1] == "stale forum-url.hint removed (forum.url is already set)"
        assert not s.hint_exists()
        assert s.cfg.read_bytes() == s.cfg_before

    def test_invalid_stale_hint_is_consumed_too(self, tmp_path):
        s = Seat(tmp_path, config=dict(BASE, forum={"url": LOCAL}), hint="garbage\n")
        s.block()
        assert not s.hint_exists()

    def test_tool_absent_leaves_the_hint_and_does_not_offer_it(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n", tool=False)
        r = s.block()
        assert r.stdout.strip() == SKIP_LINE
        assert s.hint.read_text() == HINTED + "\n"
        assert "suggested" not in r.stdout and HINTED not in r.stdout


# --------------------------------------------------------------------------
# H10 -- the hint can never make the forum "configured"
# --------------------------------------------------------------------------

GATE_SCRIPT = r"""
import importlib.util, json, os, sys
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, "src", "engram", "tools"))
sys.path.insert(0, os.path.join(repo, "src", "engram"))
def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m
hooks = os.path.join(repo, "src", "engram", "hooks", "claude")
tools = os.path.join(repo, "src", "engram", "tools")
cfg = json.load(open(os.path.join(os.environ["ENGRAM_HOME"], "config.json")))
out = {}
for hook in ("engram-forum-prompt-hook", "engram-baton-prompt-hook",
             "engram-inter-agent-prompt-hook"):
    out["hook:" + hook] = load(os.path.join(hooks, hook + ".py"), hook.replace("-", "_"))._is_forum_configured(cfg)
ia = load(os.path.join(tools, "ia.py"), "h10_ia")
out["ia"] = ia._is_forum_configured()
baton = load(os.path.join(tools, "baton.py"), "h10_baton")
out["baton"] = baton._is_forum_configured()
client = load(os.path.join(repo, "src", "engram", "engram_client.py"), "h10_client")
out["engram_client"] = client.is_forum_configured()
print(json.dumps(out))
"""


class TestH10NeverConfigured:
    def test_every_real_gate_is_false_with_a_hint_and_no_forum_url(self, tmp_path):
        s = Seat(tmp_path, hint=HINTED + "\n")
        r = subprocess.run([sys.executable, "-I", "-c", GATE_SCRIPT, str(REPO)],
                           env=s.env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert set(out) == {"hook:engram-forum-prompt-hook", "hook:engram-baton-prompt-hook",
                            "hook:engram-inter-agent-prompt-hook", "ia", "baton",
                            "engram_client"}
        assert all(v is False for v in out.values()), out

    def test_gate_harness_can_say_true(self, tmp_path):
        # control: the same harness reports True when forum.url IS set
        s = Seat(tmp_path, config=dict(BASE, forum={"url": LOCAL}), hint=HINTED + "\n")
        r = subprocess.run([sys.executable, "-I", "-c", GATE_SCRIPT, str(REPO)],
                           env=s.env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert all(v is True for v in out.values()), out

    def test_no_gate_hook_or_cli_mentions_the_hint_file(self):
        hits = []
        for root in (REPO / "src" / "engram" / "hooks", REPO / "src" / "engram" / "tools"):
            for p in root.rglob("*"):
                if p.is_file() and p.suffix in {".py", ".sh", ""} \
                        and "forum-url.hint" in p.read_text(errors="ignore"):
                    hits.append(str(p.relative_to(REPO)))
        client = REPO / "src" / "engram" / "engram_client.py"
        if "forum-url.hint" in client.read_text():
            hits.append("src/engram/engram_client.py")
        assert hits == []


# --------------------------------------------------------------------------
# Pinned wording (proves the instruction stays written; nothing more)
# --------------------------------------------------------------------------

class TestPinnedWording:
    def test_four_marked_fences_and_the_consume_fence_is_unique(self):
        fences = _fences(_step57())
        assert len(fences) == 3  # #3571: the answer template fence is gone
        assert _fence(CONSUME) in fences
        assert _fence(CONSUME).lstrip().startswith(CONSUME)

    def test_offer_wording(self):
        p = _prose()
        assert "The operator who created this account suggested" in p
        assert re.search(r"Use it", p) and re.search(r"give a different URL", p)
        assert re.search(r"no forum", p, re.IGNORECASE)

    def test_confirmation_not_the_hint_decides_and_no_auto_write(self):
        p = re.sub(r"\s+", " ", _prose())
        assert "the user's confirmation, not the hint, decides" in p.lower()
        assert re.search(r"never write the hinted URL to `forum-url\.answer` unless the user", p)

    def test_consume_instruction_runs_after_any_answer_in_its_own_call(self):
        p = re.sub(r"\s+", " ", _prose())
        assert "after ANY answer" in p
        assert "forum-url-hint-consume" in p
        assert re.search(r"including[^.]*\(c\)", p) and "different URL" in p
        assert "own Bash call" in p

    def test_answer_c_paragraph_no_longer_contradicts_the_consume_rule(self):
        paras = [re.sub(r"\s+", " ", x) for x in _prose().split("\n\n")]
        c_paras = [x for x in paras if "do not run the block again" in x]
        assert c_paras, "the (c) instruction should still say the block is not re-run"
        for x in c_paras:
            assert "forum-url-hint-consume" in x, x

    def test_hint_is_never_sourced_or_read_by_anything_else(self):
        p = _prose()
        assert re.search(r"never sourced", p) or re.search(r"never (read|loaded) into", p)

    def test_tool_absent_means_hint_not_offered(self):
        p = re.sub(r"\s+", " ", _prose())
        assert re.search(r"not installed[^.]*hint|hint[^.]*not (be )?offered", p, re.I)
