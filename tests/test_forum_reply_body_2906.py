"""#2906 -- ``forum reply`` must never make a failed call look like a sent one.

Contract (written before the implementation):
  T1  ``reply 123 "hello"`` (no stdin) posts body ``hello`` and prints the success line.
  T2  ``reply 123`` with piped stdin posts the piped body (old contract).
  T3  positional body AND non-empty piped stdin -> refuse loudly, no API call.
  T4  an unrecognized argument never has its text echoed; message carries the count; exit 2.
  T5  empty body (positional "" or empty stdin) -> the existing empty-body refusal.

Stdin is faked explicitly: under pytest ``sys.stdin`` is a capture object whose
``read()`` raises and whose ``isatty()`` is False, so it cannot stand in for
either a TTY or a pipe.  Never touches a real forum: ``_api_post`` is stubbed.
"""
import importlib.util
import io
import sys
from pathlib import Path

import pytest

_FORUM_PY = Path(__file__).resolve().parents[1] / "src" / "engram" / "tools" / "forum.py"
SENTINEL = "ZZZ_SENTINEL_BODY_TEXT " * 20


class _FakeStdin(io.StringIO):
    def __init__(self, text: str, tty: bool):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self, *a, **k):
        if self._tty:
            raise AssertionError("read() on a TTY would block")
        return super().read(*a, **k)


@pytest.fixture
def forum(monkeypatch):
    monkeypatch.syspath_prepend(str(_FORUM_PY.parent))
    spec = importlib.util.spec_from_file_location("forum_cli_2906", _FORUM_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []

    def fake_post(url, payload):
        calls.append(payload)
        return {"thread_id": payload.get("thread_id") or 9, "post_id": 77}

    monkeypatch.setattr(mod, "_api_post", fake_post)
    monkeypatch.setattr(mod, "_load_config", lambda: {})
    monkeypatch.setattr(mod, "_get_agent_name", lambda cfg: "tester")
    monkeypatch.setattr(mod, "_resolve_forum_url", lambda cfg: "http://forum.invalid")
    mod._calls = calls
    return mod


def _run(forum, monkeypatch, argv, stdin_text="", tty=True):
    monkeypatch.setattr(sys, "argv", ["forum", *argv])
    monkeypatch.setattr(sys, "stdin", _FakeStdin(stdin_text, tty))
    # os.fstat(0) cannot see the fake above; model "piped" as a regular-file redirect.
    # Real fd-0 kinds (socket/FIFO/devnull) are covered in test_forum_reply_stdin_2906.py.
    monkeypatch.setattr(forum, "_stdin_kind", lambda: "tty" if tty else "file")
    with pytest.raises(SystemExit) as ei:
        forum.main()
        sys.exit(0)  # main() returning normally == exit 0
    return ei.value.code


def test_t1_positional_body(forum, monkeypatch, capsys):
    assert _run(forum, monkeypatch, ["reply", "123", "hello"], tty=True) == 0
    assert [c["body_md"] for c in forum._calls] == ["hello"]
    assert forum._calls[0]["thread_id"] == 123
    assert "forum reply: created  thread_id=123  post_id=77" in capsys.readouterr().out


def test_t2_piped_stdin_body(forum, monkeypatch, capsys):
    assert _run(forum, monkeypatch, ["reply", "123"], "hello\n", tty=False) == 0
    assert [c["body_md"] for c in forum._calls] == ["hello"]
    assert "forum reply: created  thread_id=123  post_id=77" in capsys.readouterr().out


def test_t3_both_sources_refused(forum, monkeypatch, capsys):
    code = _run(forum, monkeypatch, ["reply", "123", "a"], "b", tty=False)
    assert code == forum.EXIT_VALIDATION
    assert forum._calls == []
    err = capsys.readouterr().err
    assert "body given both as argument and on stdin; use one" in err


def test_t3b_positional_with_empty_pipe_is_fine(forum, monkeypatch):
    assert _run(forum, monkeypatch, ["reply", "123", "a"], "", tty=False) == 0
    assert [c["body_md"] for c in forum._calls] == ["a"]


@pytest.mark.parametrize("argv", [["post", "--category", "c", "--title", "t", SENTINEL],
                                  ["reply", "1", "body", SENTINEL]])
def test_t4_unrecognized_args_not_echoed(forum, monkeypatch, capsys, argv):
    code = _run(forum, monkeypatch, argv, "", tty=True)
    assert code == 2
    assert forum._calls == []
    err = capsys.readouterr().err
    assert "ZZZ_SENTINEL_BODY_TEXT" not in err
    assert "1 unexpected argument" in err


def test_t4b_count_is_of_all_extras(forum, monkeypatch, capsys):
    assert _run(forum, monkeypatch, ["reply", "1", "body", "x", "y", "z"]) == 2
    assert "3 unexpected argument" in capsys.readouterr().err


def test_t4c_other_argparse_errors_keep_own_message(forum, monkeypatch, capsys):
    assert _run(forum, monkeypatch, ["reply"]) == 2
    assert "THREAD-ID" in capsys.readouterr().err


@pytest.mark.parametrize("argv,stdin,tty", [(["reply", "1", ""], "", True),
                                            (["reply", "1", "   "], "", True),
                                            (["reply", "1"], "  \n", False)])
def test_t5_empty_body_refused(forum, monkeypatch, capsys, argv, stdin, tty):
    assert _run(forum, monkeypatch, argv, stdin, tty) == forum.EXIT_VALIDATION
    assert forum._calls == []
    assert "body is required" in capsys.readouterr().err


def test_reply_help_names_success_line(forum, monkeypatch, capsys):
    assert _run(forum, monkeypatch, ["reply", "--help"]) == 0
    out = " ".join(capsys.readouterr().out.split())  # help is line-wrapped
    assert "forum reply: created thread_id=N post_id=M" in out
    assert "BODY" in out
