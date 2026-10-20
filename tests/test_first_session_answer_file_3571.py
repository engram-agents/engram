"""Contract-first tests for #3571: the first-session forum-url answer reaches the
Step 5.7 block as DATA (a file), never as shell source (part of #3571).

The defect: the ``# forum-url-answer`` template embedded the user's URL in a
quoted heredoc inside the agent's Bash call. The delimiter is a literal the URL
can contain, so a multi-line "URL" holding a line that is exactly ``URL_EOF``
closed the heredoc and the lines after it ran as shell. Fixing the delimiter is
the wrong layer; the answer must not pass through shell parsing at all.

The design under test: the agent writes the URL, as ONE line, to
``$ENGRAM_HOME/forum-url.answer`` with its file-writing tool (content is data).
The block (the fence whose first line is ``# forum-url-step``) reads that file
through a short Python read and nothing else, consumes it, and hands the value to
``set_forum_url.py`` as ONE quoted argument. No template fence, no environment
variable.

Two halves, and they prove different things:

  EXECUTABLE   every command is built FROM THE SKILL TEXT: the block is
               extracted from SKILL.md, the answer file is written the way the
               prose says (a plain ``open().write()``: data, no shell, which is
               what a file tool does), and the block is run as printed.
  LINT         pinned sentences in the prose. They prove the instruction stays
               WRITTEN. They cannot prove an agent obeys it: an agent that wrote
               the file with a shell command instead would defeat the prose, not
               the block (the block reads the FILE, so a shell-written file is
               still data; the exposure would be the agent's own shell command,
               which the prose forbids).

Every subprocess takes its environment from ``_hermetic_env``: HOME and
XDG_CONFIG_HOME pinned to one temp dir; inherited ENGRAM_HOME, FORUM_URL,
FORUM_ANSWER, FORUM_FORCE and CLAUDE_PLUGIN_ROOT dropped.
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "src" / "engram" / "skills" / "claude" / "engram-first-session" / "SKILL.md"
TOOL = REPO / "src" / "engram" / "tools" / "set_forum_url.py"

STEP, FORCE, CONSUME = "# forum-url-step", "# forum-url-force", "# forum-url-hint-consume"
LOCAL = "http://localhost:5002"
LAN = "http://192.168.1.50:5002"
BASE = {"mode": "single", "self_lineage": "anthropic:opus"}
INVALID_LINE = ("forum-url.answer is not a usable single-line http(s) URL: "
                "ignoring it")
SKIP_LINE = ("set_forum_url.py is not installed here "
             "(no forum components on this install): skip this step")
PLAIN_NOT_SET = "forum.url is not set (ask the question; answer (c) leaves it unset)"

_HERMETIC_HOME = tempfile.mkdtemp(prefix="first-session-answer-home-")
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


def _step57():
    m = re.search(r"^## Step 5\.7 .*?(?=^## )", _skill_text(), flags=re.MULTILINE | re.DOTALL)
    assert m, "no Step 5.7 section"
    return m.group(0)


def _fences(section=None):
    return re.findall(r"^```bash\n(.*?)^```", section or _step57(),
                      flags=re.MULTILINE | re.DOTALL)


def _fence(marker):
    found = [f for f in _fences() if marker in f]
    assert len(found) == 1, f"expected exactly one {marker!r} fence, found {len(found)}"
    return found[0]


def _prose():
    return re.sub(r"^```.*?^```", "", _step57(), flags=re.MULTILINE | re.DOTALL)


def _flat_prose():
    return re.sub(r"\s+", " ", _prose())


# ---- the harness ----------------------------------------------------------

class Seat:
    """One temp ENGRAM_HOME plus a plugin root holding (or not) the real tool."""

    def __init__(self, tmp_path, config=BASE, tool=True):
        self.tmp = tmp_path
        self.home = tmp_path / "engram-home"
        self.home.mkdir()
        self.cfg = self.home / "config.json"
        if config is not None:
            self.cfg.write_text(json.dumps(config, indent=2) + "\n")
        self.answer_path = self.home / "forum-url.answer"
        root = tmp_path / "plugin-root"
        (root / "tools").mkdir(parents=True)
        if tool:
            os.symlink(TOOL, root / "tools" / "set_forum_url.py")
        self.env = _hermetic_env(ENGRAM_HOME=str(self.home), CLAUDE_PLUGIN_ROOT=str(root))
        self.cfg_before = self.cfg.read_bytes() if config is not None else None
        self.cwd = tmp_path

    def write_answer(self, content):
        """What the file-writing tool does: bytes to a path, no shell involved."""
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        with open(self.answer_path, "wb") as f:
            f.write(data)

    def run_block(self, force=False, timeout=60):
        """The block exactly as printed (optionally preceded by the force fence)."""
        script = (_fence(FORCE) + "\n" if force else "") + _fence(STEP)
        return subprocess.run(["bash", "-c", script], env=self.env, cwd=str(self.cwd),
                              capture_output=True, text=True, timeout=timeout)

    def answer_exists(self):
        return os.path.lexists(self.answer_path)

    def url(self):
        return json.loads(self.cfg.read_text()).get("forum", {}).get("url")


def _seat_with(tmp_path, content, **kw):
    s = Seat(tmp_path, **kw)
    s.write_answer(content)
    return s


# --------------------------------------------------------------------------
# The #3571 cell: a forged heredoc delimiter is just an invalid answer
# --------------------------------------------------------------------------

class TestDefect3571:
    def test_multiline_answer_with_forged_delimiter_runs_nothing(self, tmp_path):
        sentinel = tmp_path / "SENTINEL"
        s = _seat_with(tmp_path, f"http://h\nURL_EOF\ntouch {sentinel}\n:\n")
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert not sentinel.exists(), "the answer text was run as shell (#3571)"
        assert INVALID_LINE in r.stdout
        assert "exit status" not in r.stdout, "the tool must not run on an invalid answer"
        assert s.cfg.read_bytes() == s.cfg_before
        assert not s.answer_exists(), "an invalid answer is still consumed"

    def test_forged_delimiter_with_a_command_that_would_write_the_config(self, tmp_path):
        s = _seat_with(
            tmp_path, f"{LOCAL}\nURL_EOF\nprintf PWNED > {tmp_path / 'engram-home' / 'config.json'}\n")
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert s.cfg.read_bytes() == s.cfg_before
        assert not s.answer_exists()


# --------------------------------------------------------------------------
# Valid answers
# --------------------------------------------------------------------------

class TestValidAnswer:
    def test_plain_url_is_written_and_the_answer_is_consumed(self, tmp_path):
        s = _seat_with(tmp_path, LOCAL + "\n")
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert s.url() == LOCAL, r.stdout + r.stderr
        assert "written: forum.url set to " + LOCAL in r.stdout
        assert "exit status: 0" in r.stdout
        assert not s.answer_exists()

    def test_no_trailing_newline_is_fine(self, tmp_path):
        s = _seat_with(tmp_path, LAN)
        r = s.run_block()
        assert s.url() == LAN, r.stdout + r.stderr
        assert not s.answer_exists()

    def test_other_config_keys_survive(self, tmp_path):
        s = _seat_with(tmp_path, LAN + "\n")
        s.run_block()
        assert s.url() == LAN
        assert json.loads(s.cfg.read_text())["self_lineage"] == "anthropic:opus"

    def test_file_of_exactly_the_size_cap_is_not_called_oversize(self, tmp_path):
        url = "http://" + "a" * (4096 - 7 - 1) + "\n"  # 4096 bytes including the newline
        assert len(url.encode()) == 4096
        s = _seat_with(tmp_path, url)
        r = s.run_block()
        assert INVALID_LINE not in r.stdout
        assert not s.answer_exists()

    @pytest.mark.parametrize("name,url", [
        ("dollar-paren", "http://h:5002/$(touch SENTINEL)"),
        ("backtick", "http://h:5002/`touch SENTINEL`"),
        ("quote-break", "http://h:5002/';touch SENTINEL;'"),
        ("double-quote", 'http://h:5002/"$(touch SENTINEL)"'),
        ("apostrophe", "http://h:5002/a'b"),
        ("semicolon-amp", "http://h:5002/a;touch SENTINEL&&touch SENTINEL"),
    ])
    def test_one_line_full_of_metacharacters_is_data(self, tmp_path, name, url):
        sentinel = tmp_path / "SENTINEL"
        url = url.replace("SENTINEL", str(sentinel))
        s = _seat_with(tmp_path, url + "\n")
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert not sentinel.exists(), "the URL was executed by the shell"
        # a whitespace-bearing one is invalid, the rest are stored or refused by the tool as data
        assert s.cfg.read_bytes() == s.cfg_before or s.url() == url
        assert not s.answer_exists()

    def test_apostrophe_url_round_trips_verbatim(self, tmp_path):
        url = "http://h:5002/a'b"
        s = _seat_with(tmp_path, url + "\n")
        r = s.run_block()
        assert s.url() == url, r.stdout + r.stderr

    @pytest.mark.parametrize("url,expected", [
        ("http://h/a*b", "http://h/aXb"),
    ])
    def test_value_reaches_the_tool_as_one_quoted_argument(self, tmp_path, url, expected):
        # The cwd holds a path the unquoted pattern would glob to; a quoted
        # argument reaches the tool untouched.
        s = _seat_with(tmp_path, url + "\n")
        d = tmp_path / "http:" / "h"
        d.mkdir(parents=True)
        (d / "aXb").write_text("")
        r = s.run_block()
        assert s.url() == url, r.stdout + r.stderr
        assert s.url() != expected

    def test_question_mark_reaches_the_tool_unexpanded(self, tmp_path):
        # The tool refuses a query string. Unquoted, the pattern would glob to an
        # existing path and be accepted instead.
        s = _seat_with(tmp_path, "http://h/a?b\n")
        d = tmp_path / "http:" / "h"
        d.mkdir(parents=True)
        (d / "aXb").write_text("")
        r = s.run_block()
        assert "exit status: 2" in r.stdout, r.stdout + r.stderr
        assert s.cfg.read_bytes() == s.cfg_before

    def test_different_existing_url_exits_3_consumes_and_leaves_config_alone(self, tmp_path):
        s = _seat_with(tmp_path, LOCAL + "\n", config=dict(BASE, forum={"url": LAN}))
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert "exit status: 3" in r.stdout
        assert "pass --force" in (r.stdout + r.stderr)
        assert s.cfg.read_bytes() == s.cfg_before
        assert not s.answer_exists()

    def test_force_fence_then_block_overwrites(self, tmp_path):
        s = _seat_with(tmp_path, LOCAL + "\n", config=dict(BASE, forum={"url": LAN}))
        r = s.run_block(force=True)
        assert s.url() == LOCAL, r.stdout + r.stderr
        assert "exit status: 0" in r.stdout
        assert not s.answer_exists()

    def test_force_fence_is_the_only_way_to_force(self, tmp_path):
        s = _seat_with(tmp_path, LOCAL + "\n", config=dict(BASE, forum={"url": LAN}))
        s.env["FORUM_FORCE"] = "0"
        r = s.run_block()
        assert "exit status: 3" in r.stdout
        assert s.cfg.read_bytes() == s.cfg_before

    def test_tool_refusal_consumes_the_answer(self, tmp_path):
        # passes the block's http(s) check, so it reaches the tool, which refuses the port
        s = _seat_with(tmp_path, "http://h:0\n")
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert "exit status: 2" in r.stdout
        assert "refused: URL port must be 1..65535" in r.stdout
        assert s.cfg.read_bytes() == s.cfg_before
        assert not s.answer_exists()

    def test_tool_absent_with_a_valid_answer_prints_error_and_consumes(self, tmp_path):
        s = _seat_with(tmp_path, LOCAL + "\n", tool=False)
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert "ERROR: cannot locate set_forum_url.py" in r.stdout
        assert SKIP_LINE not in r.stdout
        assert s.cfg.read_bytes() == s.cfg_before
        assert not s.answer_exists()

    def test_missing_config_reports_exit_2_and_creates_nothing(self, tmp_path):
        s = _seat_with(tmp_path, LOCAL + "\n", config=None)
        r = s.run_block()
        assert "exit status: 2" in r.stdout
        assert not s.cfg.exists()
        assert not s.answer_exists()


# --------------------------------------------------------------------------
# Invalid answers: report, run nothing, consume
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name,content", [
    ("second-non-empty-line", f"{LOCAL}\nhttp://evil.example:1\n"),
    ("second-line-no-newline", f"{LOCAL}\nx"),
    ("leading-blank-line", f"\n{LOCAL}\n"),
    ("nul-byte", f"http://h\x00:5002\n"),
    ("nul-after-newline", f"{LOCAL}\n\x00"),
    ("space-in-line", "http://h x:5002\n"),
    ("tab-in-line", "http://h\t:5002\n"),
    ("trailing-space", f"{LOCAL} \n"),
    ("carriage-return", f"{LOCAL}\r\n"),
    ("control-char", "http://h\x1b[0m:5002\n"),
    ("unicode-space", "http://h\u00a0:5002\n"),
    ("http-scheme-only", "http://\n"),
    ("https-scheme-only", "https://\n"),
    ("no-scheme", "localhost:5002\n"),
    ("ftp", "ftp://h:5002\n"),
    ("uppercase-scheme", "HTTP://h:5002\n"),
    ("empty-file", ""),
    ("only-a-newline", "\n"),
    ("not-utf8", b"http://h\xff\xfe\n"),
    ("oversize", "http://" + "a" * 4100 + "\n"),
])
def test_invalid_answer_is_reported_runs_nothing_and_is_consumed(tmp_path, name, content):
    sentinel = tmp_path / "SENTINEL"
    s = _seat_with(tmp_path, content)
    r = s.run_block()
    assert r.returncode == 0, r.stderr
    assert INVALID_LINE in r.stdout, f"{name}: {r.stdout!r}"
    assert "exit status" not in r.stdout, f"{name}: the tool must not run"
    assert s.cfg.read_bytes() == s.cfg_before
    assert not sentinel.exists()
    assert not s.answer_exists(), f"{name}: an invalid answer must still be consumed"


class TestNonRegularAnswerFile:
    def test_symlink_is_refused_target_untouched_and_link_consumed(self, tmp_path):
        target = tmp_path / "precious"
        target.write_text(LOCAL + "\n")
        s = Seat(tmp_path)
        os.symlink(target, s.answer_path)
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert INVALID_LINE in r.stdout
        assert "exit status" not in r.stdout
        assert s.cfg.read_bytes() == s.cfg_before
        assert target.read_text() == LOCAL + "\n", "the symlink target must be left alone"
        assert not s.answer_exists()

    def test_dangling_symlink_is_consumed(self, tmp_path):
        s = Seat(tmp_path)
        os.symlink(tmp_path / "nowhere", s.answer_path)
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert not s.answer_exists()

    def test_fifo_returns_promptly_and_is_invalid(self, tmp_path):
        s = Seat(tmp_path)
        os.mkfifo(s.answer_path)
        script = _fence(STEP)
        proc = subprocess.Popen(
            ["bash", "-c", script], env=s.env, cwd=str(tmp_path),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            proc.communicate()
            pytest.fail("the block blocked on a FIFO at the answer path")
        assert proc.returncode == 0, err
        assert INVALID_LINE in out
        assert "exit status" not in out
        assert s.cfg.read_bytes() == s.cfg_before
        assert not s.answer_exists()

    def test_directory_is_invalid_and_does_not_hang(self, tmp_path):
        s = Seat(tmp_path)
        s.answer_path.mkdir()
        r = s.run_block(timeout=15)
        assert r.returncode == 0, r.stderr
        assert INVALID_LINE in r.stdout
        assert "exit status" not in r.stdout
        assert s.cfg.read_bytes() == s.cfg_before


# --------------------------------------------------------------------------
# No answer: exactly today's no-answer behaviour
# --------------------------------------------------------------------------

class TestNoAnswer:
    def test_absent_file_unset_key_says_not_set(self, tmp_path):
        s = Seat(tmp_path)
        r = s.run_block()
        assert r.stdout == PLAIN_NOT_SET + "\n"
        assert s.cfg.read_bytes() == s.cfg_before

    def test_absent_file_key_set_says_already_set(self, tmp_path):
        s = Seat(tmp_path, config=dict(BASE, forum={"url": LOCAL}))
        r = s.run_block()
        assert r.stdout == f"forum.url is already set: {LOCAL} (skip the question)\n"

    def test_absent_file_tool_absent_prints_only_the_skip_line(self, tmp_path):
        s = Seat(tmp_path, tool=False)
        assert s.run_block().stdout.strip() == SKIP_LINE

    def test_forum_answer_environment_variable_is_no_answer(self, tmp_path):
        s = Seat(tmp_path)
        s.env["FORUM_ANSWER"] = LOCAL
        r = s.run_block()
        assert r.returncode == 0, r.stderr
        assert r.stdout == PLAIN_NOT_SET + "\n", "the removed environment channel was honoured"
        assert s.cfg.read_bytes() == s.cfg_before

    def test_forum_answer_environment_variable_does_not_beat_an_invalid_file(self, tmp_path):
        s = _seat_with(tmp_path, "http://h\nX\n")
        s.env["FORUM_ANSWER"] = LOCAL
        r = s.run_block()
        assert INVALID_LINE in r.stdout and "exit status" not in r.stdout
        assert s.cfg.read_bytes() == s.cfg_before

    def test_forum_url_environment_variable_is_never_used(self, tmp_path):
        s = Seat(tmp_path)
        s.env["FORUM_URL"] = "http://env.example:5002"
        r = s.run_block()
        assert r.stdout == PLAIN_NOT_SET + "\n"

    def test_answer_in_another_directory_is_not_read(self, tmp_path):
        # the file is read ONLY from $ENGRAM_HOME/forum-url.answer
        s = Seat(tmp_path)
        (tmp_path / "forum-url.answer").write_text(LOCAL + "\n")
        (tmp_path / "plugin-root" / "forum-url.answer").write_text(LOCAL + "\n")
        r = s.run_block()
        assert r.stdout == PLAIN_NOT_SET + "\n"

    def test_answer_file_is_found_under_home_dot_engram_when_engram_home_is_unset(self, tmp_path):
        fake_home = tmp_path / "home"
        (fake_home / ".engram").mkdir(parents=True)
        cfg = fake_home / ".engram" / "config.json"
        cfg.write_text(json.dumps(BASE))
        root = tmp_path / "plugin-root"
        (root / "tools").mkdir(parents=True)
        os.symlink(TOOL, root / "tools" / "set_forum_url.py")
        (fake_home / ".engram" / "forum-url.answer").write_text(LAN + "\n")
        env = _hermetic_env(HOME=str(fake_home), CLAUDE_PLUGIN_ROOT=str(root))
        r = subprocess.run(["bash", "-c", _fence(STEP)], env=env, cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert json.loads(cfg.read_text())["forum"]["url"] == LAN, r.stdout + r.stderr
        assert not (fake_home / ".engram" / "forum-url.answer").exists()


# --------------------------------------------------------------------------
# The block's own text
# --------------------------------------------------------------------------

def _code(fence):
    return "\n".join(l for l in fence.splitlines() if not l.lstrip().startswith("#"))


class TestBlockText:
    def test_block_reads_only_the_answer_file_and_never_sources_or_evals(self):
        code = _code(_fence(STEP))
        assert "forum-url.answer" in code
        assert not re.search(r"(^|[\s;&|(])(source|eval)\s", code)
        assert not re.search(r"(^|\s)\.\s+[\"$]", code)
        assert "FORUM_ANSWER" not in code

    def test_tool_call_passes_one_quoted_argument_and_merges_stderr(self):
        assert '"$PY" "$TOOL" "$ANSWER" ${FORCE:+--force} 2>&1' in _fence(STEP)

    def test_block_consumes_the_answer_file_with_rm(self):
        assert re.search(r'rm -f -- "\$ANSWER_FILE"', _fence(STEP))

    def test_tool_resolution_uses_plugin_root_pattern(self):
        assert "${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/tools/set_forum_url.py}" in _fence(STEP)


# --------------------------------------------------------------------------
# C1 lint -- nothing in Step 5.7 takes the user's text into shell source
# --------------------------------------------------------------------------

class TestC1NoTemplate:
    def test_no_answer_template_fence_remains(self):
        fences = _fences()
        assert not [f for f in fences if "# forum-url-answer" in f]
        assert sorted(f.lstrip().splitlines()[0] for f in fences) == sorted(
            [CONSUME, FORCE, STEP])

    def test_no_placeholder_or_answer_heredoc_in_any_step_fence(self):
        for f in _fences():
            assert "<URL>" not in f
            assert "URL_EOF" not in f
            assert not re.search(r"<[A-Za-z_][A-Za-z_ -]*>", re.sub(r"<<'?\w+'?", "", _code(f))), \
                "no placeholder the agent must substitute the user's input into"
            # the only heredocs are the block's own constant Python reads, quoted
            for m in re.finditer(r"<<(-?)\s*(['\"]?)(\w+)\2", f):
                assert m.group(3) == "PYEOF" and m.group(2) == "'", m.group(0)

    def test_no_forum_answer_environment_variable_anywhere_in_the_step(self):
        assert "FORUM_ANSWER" not in _step57()

    def test_force_fence_is_still_only_the_constant_export(self):
        f = _fence(FORCE)
        assert [l for l in f.splitlines() if l.strip()] == [FORCE, "export FORUM_FORCE=1"]

    def test_consume_fence_is_unchanged_and_does_not_touch_the_answer_file(self):
        code = _code(_fence(CONSUME))
        assert "forum-url.hint" in code and "forum-url.answer" not in code


# --------------------------------------------------------------------------
# C4 lint -- the prose pins the channel (cannot prove an agent obeys it)
# --------------------------------------------------------------------------

class TestC4PinnedWording:
    def test_write_the_answer_to_the_file_with_the_file_writing_tool(self):
        p = _flat_prose()
        assert "forum-url.answer" in p
        assert re.search(r"[Ww]rite the user's URL to `\$ENGRAM_HOME/forum-url\.answer`", p)
        assert "default `~/.engram/forum-url.answer`" in p
        assert "with your file-writing tool" in p

    def test_exactly_one_line(self):
        assert "as exactly one line" in _flat_prose()

    def test_never_with_a_shell_command(self):
        p = _flat_prose()
        assert "NEVER with a shell command" in p
        for word in ("echo", "printf", "cat <<", "tee"):
            assert word in p, word

    def test_more_than_one_line_or_whitespace_is_not_written(self):
        p = _flat_prose()
        assert re.search(
            r"more than one line, or contains whitespace, do not write it: tell the user "
            r"it is not a URL and ask again", p)

    def test_answer_a_writes_the_url_the_user_gave(self):
        p = _flat_prose()
        assert re.search(r"For \(a\) with a different port, write the URL the user gave", p)

    def test_the_operators_hint_is_written_only_when_the_user_chose_it(self):
        p = _flat_prose()
        assert re.search(r"For \"use it\" on the operator's hint, write the hinted URL", p)
        assert re.search(
            r"never write the hinted URL to `forum-url\.answer` unless the user chose it", p)

    def test_the_block_alone_is_run_after_writing_and_no_template_is_mentioned(self):
        p = _flat_prose()
        assert "template" not in p.lower()
        assert "forum-url-answer" not in p
        assert re.search(r"[Rr]un the block[^.]*in one Bash call", p)

    def test_the_invalid_line_is_named_and_means_nothing_ran(self):
        assert INVALID_LINE in _flat_prose()

    def test_url_is_never_taken_from_the_environment_or_hostname(self):
        p = _flat_prose()
        assert "never guessed" in p
        assert re.search(r"never taken from[^.]*`FORUM_URL`", p)


# --------------------------------------------------------------------------
# #3573 prose fold (part of #3571) -- the prose is the only protection of the
# WRITE, so its three gaps are pinned. Lint only: it proves the sentences stay
# written, not that an agent obeys them.
# --------------------------------------------------------------------------

# sha256 of each bash fence body of Step 5.7 on the base (0c5824b2); the prose
# fold must not touch a fence.
# STEP was re-pinned for #3863 6c: the fence now differs from the base ONLY in the
# `TOOL=` marketplace-fallback line, which became the canonical
# "${ENGRAM_HOME:-$HOME/.engram}" expression (shown once by the 6c acceptance run
# recorded in the PR body; the one-time test that asserted it was then deleted).
# The `PY=` venv line is the base text: the venv is ~/.engram/venv regardless of
# ENGRAM_HOME (Borges' ruling on #3863). Old STEP hash: 4609926a...6519.
FENCE_SHA256 = {
    STEP: "eb20c5ba6b228d8934de793f2a670d162e483310d8f8f43c3a534ef99d2e63c3",
    FORCE: "9c8f94608ee593c063a718e35f806dff752bf5f1964a929cb0f15591fdc68525",
    CONSUME: "45e88966b4fcd9831e0a7210f18f65c983d83cdc1de0ce4150cddc5bfbd456a6",
}


class TestProseFold3573:
    def test_c1_names_apply_patch_as_the_codex_tool_next_to_the_write_tool(self):
        assert ("(the Write tool on Claude Code, `apply_patch` on Codex)"
                in _flat_prose())

    def test_c2_leftover_answer_file_is_read_first_never_a_shell_fallback(self):
        p = _flat_prose()
        assert "already exists" in p and "a leftover from an earlier attempt" in p
        assert re.search(r"Read it first and then write the new content over it "
                         r"with your file tool", p)
        assert "that URL came from an earlier answer" in p
        assert "A shell command is never the fallback when the file tool refuses" in p

    def test_c3_no_non_shell_file_tool_means_do_not_write_choose_c(self):
        p = _flat_prose()
        assert re.search(r"no file-writing tool that is not a shell command, do NOT "
                         r"write the answer with a heredoc or any shell command", p)
        assert re.search(r"choose answer \(c\) \(no forum\) and tell the user they can "
                         r"set it later with `set_forum_url\.py <URL>`", p)

    def test_c4_the_step_fences_are_byte_identical_to_the_base(self):
        import hashlib
        got = {f.splitlines()[0]: hashlib.sha256(f.encode()).hexdigest()
               for f in _fences()}
        assert got == FENCE_SHA256
