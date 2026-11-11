"""Contract-first tests for the ``forum.url`` writer (part of #3557, slice 1).

Since #3550 / #3559 "forum configured" means ``config.json`` ``forum.url`` is a
non-empty string, and nothing wrote it. ``src/engram/tools/set_forum_url.py``
is the one shared, idempotent writer; ``install-forum-service.sh`` is its first
caller (for the admin seat's own config).

The contract (S1-S9):

  S1  URL validity: str; non-empty after strip; scheme http/https only;
      non-empty hostname; port (if any) 1..65535; no whitespace or control
      characters; NO userinfo. Written value = stripped input minus ONE
      trailing "/". Anything else: refused, exit 2, file byte-identical.
  S2  Config: missing -> refused exit 2 and NO file created; invalid JSON ->
      refused exit 2, bytes untouched; top level not an object -> exit 2.
  S3  Existing forum.url: equal after strip -> unchanged, exit 0, file NOT
      rewritten (bytes and mtime identical); different non-empty -> refused
      exit 3 without --force, written with --force; blank/whitespace/absent
      -> written.
  S4  forum key: absent or null -> {"url": v}; dict -> set only url; any other
      type -> refused exit 2 without --force, replaced with --force.
  S5  Preservation: every other top-level key, every other key inside forum,
      and key ORDER; output is json.dump(indent=2) + trailing newline.
  S6  Atomic: temp file in the same directory, fsync, os.replace; mode kept;
      on any failure the original is byte-identical and no temp file is left;
      a symlinked config is written through to its target, link kept.
  S7  Scope: only forum.url is ever written.
  S8  Installer step: port 5002/5099 -> http://localhost:<port>; a different
      pre-existing url is kept and the install still succeeds; --dry-run
      writes nothing and says what it would do; a missing admin config does
      not fail the install.
  S9  Round trip: the written value, read through the real
      forum_api.forum_url_from_config, is what the real hooks' and CLIs'
      _is_forum_configured treat as configured.

Every cell is red on a dev without the module (it does not exist). The shell
step of S8 cannot run the real installer (systemctl, venv, group); the
installer's marked step block is extracted and run on its own, and the real
script's --dry-run covers the dry-run output.
"""

import atexit
import importlib.util
import json
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
TOOL = REPO / "src" / "engram" / "tools" / "set_forum_url.py"
INSTALLER = REPO / "src" / "forum" / "deploy" / "install-forum-service.sh"
FORUM_API = REPO / "src" / "engram" / "tools" / "forum_api.py"
FORUM_HOOK = REPO / "src" / "engram" / "hooks" / "claude" / "engram-forum-prompt-hook.py"
IA = REPO / "src" / "engram" / "tools" / "ia.py"

URL = "http://localhost:5002"


# One empty HOME for every subprocess cell. The installer reads
# $HOME/.config/systemd/user/engram-forum.service and refuses when it already
# carries a baton login map, so a subprocess that inherits the caller's HOME
# passes on every seat except a forum-admin seat. Every subprocess in this file
# gets its environment from _hermetic_env, which pins HOME and XDG_CONFIG_HOME
# here and drops any inherited ENGRAM_HOME.
_HERMETIC_HOME = tempfile.mkdtemp(prefix="set-forum-url-home-")
atexit.register(shutil.rmtree, _HERMETIC_HOME, ignore_errors=True)

# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


def _hermetic_env(**extra):
    env = dict(os.environ)
    env.pop("ENGRAM_HOME", None)
    env["HOME"] = _HERMETIC_HOME
    env["XDG_CONFIG_HOME"] = os.path.join(_HERMETIC_HOME, ".config")
    env.update(extra)
    return env


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _tool():
    return _load(TOOL, "set_forum_url_under_test")


def _write(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def _cfg(tmp_path, data):
    return _write(tmp_path / "config.json", data)


def _cli(args, env_home=None):
    # Without the module, `python missing.py` itself exits 2; that is not the
    # tool refusing. Require the file so a refusal cell cannot pass by absence.
    assert TOOL.is_file(), "set_forum_url.py does not exist"
    extra = {"ENGRAM_HOME": str(env_home)} if env_home is not None else {}
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, env=_hermetic_env(**extra))


def _no_temp_left(directory, keep, with_backups=False):
    """The directory holds exactly `keep`. With `with_backups=True`, stamped backups
    (`<name>.engram-backup.<UTC>`) are allowed too: a write that changes the file saves the old
    bytes there first. Anything else is a leftover. A refused run passes no `with_backups`, so a
    backup it should not have made still fails."""
    names = sorted(p.name for p in directory.iterdir() if with_backups is False or ".engram-backup." not in p.name)
    return names == sorted(keep)


# ---------------------------------------------------------------- S1 ----

BAD_URLS = [
    "localhost:5002",
    "ftp://h:5002",
    "file:///etc/passwd",
    "",
    "   ",
    "http://",
    "http://:5002",
    "http://h:0",
    "http://h:70000",
    "http://h:5002/ x",
    "http://u:p@h",
    "http://u@h:5002",
    "http://h:5002\nx",
    "http://h\t:5002",
    "http://h:5002\x00",
    "http://h:5002?x=1",
    "http://h:5002?",
    "http://h:5002#f",
    "http://h:5002#",
    "http://h:5002/api?x=1",
    "http://h:5002/api#f",
    # S1d: more than one trailing slash
    "http://h:5002//",
    "http://h:5002///",
    "http://h:5002/api//",
    # S1c: host plausibility
    "http://h\\x",
    "http://h%20x",
    "http://-",
    "http://a..b",
    "http://-a.b",
    "http://a-.b",
    "http://" + "a" * 64,
    "http://" + ".".join(["a" * 63] * 3 + ["a" * 62]),
    "http://" + "h" * 5000,
    "http://h:",
    "http://[::1",
    "http://[zz]:5002",
    "http://[::1]:",
    # DEL (0x7f) in the path, where the host check cannot be what refuses it
    "http://h:5002/a\x7fb",
]


class TestS1UrlValidity:
    @pytest.mark.parametrize("bad", BAD_URLS, ids=lambda s: repr(s)[:30])
    def test_bad_url_refused_file_identical(self, tmp_path, bad):
        p = _cfg(tmp_path, {"agent_name": "x", "forum": {"url": ""}})
        before = p.read_bytes()
        r = _tool().set_forum_url(p, bad)
        assert r.status == "refused" and r.exit_code == 2, r
        assert p.read_bytes() == before
        assert _no_temp_left(tmp_path, ["config.json"])

    @pytest.mark.parametrize("bad", BAD_URLS, ids=lambda s: repr(s)[:30])
    def test_bad_url_cli_exit_2(self, tmp_path, bad):
        if "\x00" in bad:
            pytest.skip("a NUL byte cannot be passed in argv; covered by the function cell")
        p = _cfg(tmp_path, {})
        before = p.read_bytes()
        r = _cli([bad, "--config", str(p)])
        assert r.returncode == 2, (r.stdout, r.stderr)
        assert p.read_bytes() == before

    def test_non_string_refused(self, tmp_path):
        p = _cfg(tmp_path, {})
        for bad in (None, 5002, ["http://h"], b"http://h"):
            r = _tool().set_forum_url(p, bad)
            assert r.status == "refused" and r.exit_code == 2

    def test_missing_cli_argument_exit_2(self, tmp_path):
        p = _cfg(tmp_path, {})
        r = _cli(["--config", str(p)])
        assert r.returncode == 2

    @pytest.mark.parametrize("good,stored", [
        ("http://localhost:5002", "http://localhost:5002"),
        ("https://forum.example.org", "https://forum.example.org"),
        ("  http://10.0.0.5:5002  ", "http://10.0.0.5:5002"),
        ("http://h:5002/", "http://h:5002"),
        ("HTTP://H:1", "HTTP://H:1"),
        ("http://h:65535", "http://h:65535"),
        ("http://h:5002/api", "http://h:5002/api"),
        ("http://h:5002/api/", "http://h:5002/api"),
        ("http://localhost", "http://localhost"),
        ("http://h", "http://h"),
        ("http://a.b-c.d", "http://a.b-c.d"),
        ("http://192.168.1.10:5002", "http://192.168.1.10:5002"),
        ("http://[::1]", "http://[::1]"),
        ("http://[fe80::1]:5002", "http://[fe80::1]:5002"),
        ("HTTP://H:5002", "HTTP://H:5002"),
        ("http://" + "a" * 63, "http://" + "a" * 63),
        ("http://" + ".".join(["a" * 63] * 3 + ["a" * 61]),
         "http://" + ".".join(["a" * 63] * 3 + ["a" * 61])),
        ("http://my_host:5002", "http://my_host:5002"),
        ("http://h:5002/", "http://h:5002"),
    ])
    def test_good_url_written_stripped_one_slash(self, tmp_path, good, stored):
        p = _cfg(tmp_path, {})
        r = _tool().set_forum_url(p, good)
        assert r.status == "written" and r.exit_code == 0, r
        assert json.loads(p.read_text())["forum"]["url"] == stored


# ---------------------------------------------------------------- S2 ----

class TestS1cHostProblemDirect:
    """``_host_problem`` is a pure function of the netloc string, so its IPv6
    clauses are tested directly.

    Why direct: measured on Python 3.12.3 (the interpreter in ~/.engram/venv),
    ``urlsplit("http://[zz]:5002")`` and ``urlsplit("http://[::1]x")`` already
    raise ValueError, and ``validate_url`` turns that into a refusal BEFORE
    ``_host_problem`` runs. So through the CLI on this interpreter those clauses
    are unreachable. The installer calls plain ``python3``, which may be an
    older Python that does not validate bracketed hosts; there the tool's own
    check is the only guard. (``http://[::1]:`` is the one case urlsplit lets
    through here, so its CLI cell does reach the tool's own check.)
    """

    @pytest.mark.parametrize("netloc", [
        "[zz]", "[zz]:5002", "[::1", "[::1]x", "[::1]:", "[::1]]",
        "[1:2:3]", "[:::1]",
    ])
    def test_refused(self, netloc):
        assert _tool()._host_problem(netloc), netloc

    @pytest.mark.parametrize("netloc", [
        "[::1]", "[::1]:5002", "[fe80::1]", "[2001:db8::1]:443", "localhost:5002",
    ])
    def test_accepted(self, netloc):
        assert _tool()._host_problem(netloc) is None, netloc

    def test_port_digits_are_not_this_functions_job(self):
        # urlsplit(...).port rejects "[::1]:5002x"; _host_problem does not look
        # at port digits, so it returns None here. Pinned so the split is explicit.
        assert _tool()._host_problem("[::1]:5002x") is None

    def test_validate_url_refuses_bad_ipv6_whichever_layer_catches_it(self):
        from urllib.parse import urlsplit
        mod = _tool()
        try:
            urlsplit("http://[zz]:5002").port
            urlsplit_rejects = False
        except ValueError:
            urlsplit_rejects = True
        # Not interpreter-dependent: refused either way. The message names which
        # layer did it, for a reader who wonders why the tool's check looks dead.
        value, why = mod.validate_url("http://[zz]:5002")
        assert value is None, (
            f"python {sys.version.split()[0]}: urlsplit rejects bad IPv6 = "
            f"{urlsplit_rejects}; reason = {why}")


class TestS2Config:
    def test_missing_config_refused_no_file(self, tmp_path):
        p = tmp_path / "config.json"
        r = _tool().set_forum_url(p, URL)
        assert r.status == "refused" and r.exit_code == 2
        assert list(tmp_path.iterdir()) == []

    def test_missing_config_cli(self, tmp_path):
        p = tmp_path / "config.json"
        r = _cli([URL, "--config", str(p)])
        assert r.returncode == 2
        assert not p.exists()

    @pytest.mark.parametrize("raw", [b"{not json", b"", b"\xff\xfe"])
    def test_corrupt_config_untouched(self, tmp_path, raw):
        p = tmp_path / "config.json"
        p.write_bytes(raw)
        r = _tool().set_forum_url(p, URL)
        assert r.status == "refused" and r.exit_code == 2
        assert p.read_bytes() == raw
        assert _no_temp_left(tmp_path, ["config.json"])

    @pytest.mark.parametrize("data", [[], [1], "s", 3, None, True])
    def test_non_object_top_level(self, tmp_path, data):
        p = tmp_path / "config.json"
        p.write_text(json.dumps(data))
        before = p.read_bytes()
        r = _tool().set_forum_url(p, URL)
        assert r.status == "refused" and r.exit_code == 2
        assert p.read_bytes() == before

    def test_green_control_valid_config_written(self, tmp_path):
        p = _cfg(tmp_path, {"agent_name": "a"})
        assert _tool().set_forum_url(p, URL).status == "written"

    def test_cli_default_path_from_engram_home(self, tmp_path):
        _write(tmp_path / "config.json", {"agent_name": "a"})
        r = _cli([URL], env_home=tmp_path)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert json.loads((tmp_path / "config.json").read_text())["forum"] == {"url": URL}


# ---------------------------------------------------------------- S3 ----

class TestS3Existing:
    def test_equal_unchanged_not_rewritten(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"url": URL}})
        os.utime(p, (1_000_000_000, 1_000_000_000))
        before, mt = p.read_bytes(), p.stat().st_mtime_ns
        r = _tool().set_forum_url(p, URL)
        assert r.status == "unchanged" and r.exit_code == 0
        assert p.read_bytes() == before and p.stat().st_mtime_ns == mt

    def test_equal_with_whitespace_in_file_unchanged(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"url": "  " + URL + " \n"}})
        os.utime(p, (1_000_000_000, 1_000_000_000))
        before, mt = p.read_bytes(), p.stat().st_mtime_ns
        r = _tool().set_forum_url(p, URL)
        assert r.status == "unchanged" and r.exit_code == 0
        assert p.read_bytes() == before and p.stat().st_mtime_ns == mt

    @pytest.mark.parametrize("existing", ["http://a:5002/", "  http://a:5002/ \n"])
    def test_existing_with_one_trailing_slash_is_unchanged(self, tmp_path, existing):
        p = _cfg(tmp_path, {"forum": {"url": existing}})
        os.utime(p, (1_000_000_000, 1_000_000_000))
        before, mt = p.read_bytes(), p.stat().st_mtime_ns
        r = _tool().set_forum_url(p, "http://a:5002")
        assert r.status == "unchanged" and r.exit_code == 0, r
        assert p.read_bytes() == before and p.stat().st_mtime_ns == mt

    @pytest.mark.parametrize("existing", ["http://a:5002//", "http://a:5003/",
                                          "http://b:5002/"])
    def test_existing_slash_variants_that_differ_stay_different(self, tmp_path, existing):
        p = _cfg(tmp_path, {"forum": {"url": existing}})
        before = p.read_bytes()
        r = _tool().set_forum_url(p, "http://a:5002")
        assert r.status == "refused" and r.exit_code == 3, r
        assert p.read_bytes() == before

    def test_unchanged_cli_exit_0(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"url": URL}})
        assert _cli([URL, "--config", str(p)]).returncode == 0

    def test_different_refused_exit_3(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"url": "http://other:5002"}})
        before = p.read_bytes()
        r = _tool().set_forum_url(p, URL)
        assert r.status == "refused" and r.exit_code == 3
        assert p.read_bytes() == before

    def test_different_cli_exit_3_then_force(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"url": "http://other:5002"}})
        assert _cli([URL, "--config", str(p)]).returncode == 3
        assert _cli([URL, "--config", str(p), "--force"]).returncode == 0
        assert json.loads(p.read_text())["forum"]["url"] == URL

    def test_different_with_force_written(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"url": "http://other:5002"}})
        r = _tool().set_forum_url(p, URL, force=True)
        assert r.status == "written" and r.exit_code == 0

    @pytest.mark.parametrize("existing", ["", "   ", "\n\t"])
    def test_blank_written(self, tmp_path, existing):
        p = _cfg(tmp_path, {"forum": {"url": existing}})
        r = _tool().set_forum_url(p, URL)
        assert r.status == "written"
        assert json.loads(p.read_text())["forum"]["url"] == URL

    def test_absent_url_key_written(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"other": 1}})
        assert _tool().set_forum_url(p, URL).status == "written"

    def test_non_string_existing_url_is_not_a_url(self, tmp_path):
        # a non-string url is not a "non-empty string": treated as blank
        p = _cfg(tmp_path, {"forum": {"url": 5}})
        assert _tool().set_forum_url(p, URL).status == "written"


# ---------------------------------------------------------------- S4 ----

class TestS4ForumKey:
    def test_absent(self, tmp_path):
        p = _cfg(tmp_path, {"a": 1})
        assert _tool().set_forum_url(p, URL).status == "written"
        assert json.loads(p.read_text())["forum"] == {"url": URL}

    def test_null(self, tmp_path):
        p = _cfg(tmp_path, {"forum": None})
        assert _tool().set_forum_url(p, URL).status == "written"
        assert json.loads(p.read_text())["forum"] == {"url": URL}

    def test_dict_with_other_keys(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"poll": 30, "url": "", "z": [1]}})
        assert _tool().set_forum_url(p, URL).status == "written"
        assert json.loads(p.read_text())["forum"] == {"poll": 30, "url": URL, "z": [1]}

    @pytest.mark.parametrize("bad", ["s", ["a"], 5, True, 0, False, 1.5],
                             ids=repr)
    def test_other_type_refused_without_force(self, tmp_path, bad):
        p = _cfg(tmp_path, {"forum": bad, "k": 1})
        before = p.read_bytes()
        r = _tool().set_forum_url(p, URL)
        assert r.status == "refused" and r.exit_code == 2
        assert p.read_bytes() == before

    @pytest.mark.parametrize("bad", ["s", ["a"], 5, True], ids=repr)
    def test_other_type_replaced_with_force(self, tmp_path, bad):
        p = _cfg(tmp_path, {"forum": bad, "k": 1})
        r = _tool().set_forum_url(p, URL, force=True)
        assert r.status == "written"
        assert json.loads(p.read_text())["forum"] == {"url": URL}
        assert json.loads(p.read_text())["k"] == 1


# ---------------------------------------------------------------- S5 ----

class TestS5Preservation:
    def test_other_keys_order_and_format(self, tmp_path):
        data = {"zeta": 1, "agent_name": "n", "forum": {"b": 2, "url": "", "a": 1},
                "mode": "m", "alpha": {"deep": [1, {"x": None}]}, "multi_agent": True}
        p = _cfg(tmp_path, data)
        assert _tool().set_forum_url(p, URL).status == "written"
        expected = dict(data)
        expected["forum"] = {"b": 2, "url": URL, "a": 1}
        assert p.read_text() == json.dumps(expected, indent=2) + "\n"
        assert list(json.loads(p.read_text())) == list(data)
        assert list(json.loads(p.read_text())["forum"]) == ["b", "url", "a"]

    def test_other_forum_keys_kept(self, tmp_path):
        p = _cfg(tmp_path, {"forum": {"keep": "me", "n": 1}})
        _tool().set_forum_url(p, URL)
        f = json.loads(p.read_text())["forum"]
        assert f["keep"] == "me" and f["n"] == 1 and f["url"] == URL

    def test_non_ascii_sibling_key_survives_a_rewrite(self, tmp_path):
        # The writer keeps json.dump's default (ASCII escapes), the same as
        # install-local-marketplace.sh; the value must round-trip unchanged.
        p = _cfg(tmp_path, {"agent_name": "né", "forum": {}})
        _tool().set_forum_url(p, URL)
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["agent_name"] == "né" and d["forum"] == {"url": URL}

    def test_differently_formatted_file_is_reformatted(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text('{"a":1,"b":{"c":2}}')
        _tool().set_forum_url(p, URL)
        assert p.read_text() == json.dumps(
            {"a": 1, "b": {"c": 2}, "forum": {"url": URL}}, indent=2) + "\n"


# ---------------------------------------------------------------- S6 ----

class TestS6Atomic:
    def test_replace_failure_leaves_original_and_no_temp(self, tmp_path, monkeypatch):
        p = _cfg(tmp_path, {"agent_name": "n"})
        before = p.read_bytes()
        mod = _tool()

        def boom(*a, **k):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(mod.os, "replace", boom)
        r = mod.set_forum_url(p, URL)
        assert r.status != "written", r
        assert p.read_bytes() == before
        assert _no_temp_left(tmp_path, ["config.json"], with_backups=True)

    def test_mode_preserved(self, tmp_path):
        p = _cfg(tmp_path, {})
        os.chmod(p, 0o640)
        _tool().set_forum_url(p, URL)
        assert stat.S_IMODE(p.stat().st_mode) == 0o640

    def test_no_temp_file_after_success(self, tmp_path):
        p = _cfg(tmp_path, {})
        _tool().set_forum_url(p, URL)
        assert _no_temp_left(tmp_path, ["config.json"], with_backups=True)

    def test_temp_is_in_same_directory(self, tmp_path, monkeypatch):
        p = _cfg(tmp_path, {})
        mod = _tool()
        seen = {}
        real = os.replace

        def spy(src, dst):
            seen["src"], seen["dst"] = Path(src), Path(dst)
            return real(src, dst)

        monkeypatch.setattr(mod.os, "replace", spy)
        mod.set_forum_url(p, URL)
        assert seen["src"].parent == seen["dst"].parent == tmp_path

    def test_symlink_written_through_link_kept(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        target = _write(real_dir / "cfg-target.json", {"agent_name": "n"})
        link_dir = tmp_path / "home"
        link_dir.mkdir()
        link = link_dir / "config.json"
        link.symlink_to(target)
        r = _tool().set_forum_url(link, URL)
        assert r.status == "written"
        assert link.is_symlink() and os.readlink(link) == str(target)
        assert json.loads(target.read_text())["forum"] == {"url": URL}
        assert _no_temp_left(link_dir, ["config.json"], with_backups=True)
        assert _no_temp_left(real_dir, ["cfg-target.json"], with_backups=True)


# ---------------------------------------------------------------- S7 ----

class TestS7Scope:
    def test_identity_keys_untouched(self, tmp_path):
        data = {"agent_name": "sol", "mode": "single", "multi_agent": False,
                "comms_surface": "forum", "forum": {}}
        p = _cfg(tmp_path, data)
        _tool().set_forum_url(p, URL)
        d = json.loads(p.read_text())
        for k in ("agent_name", "mode", "multi_agent", "comms_surface"):
            assert d[k] == data[k]
        assert set(d) == set(data)
        assert d["forum"] == {"url": URL}


# ---------------------------------------------------------------- S9 ----

class TestS9RoundTrip:
    def test_written_value_round_trips_through_real_consumers(self, tmp_path):
        fa = _load(FORUM_API, "forum_api_s9")
        hook = _load(FORUM_HOOK, "forum_hook_s9")
        for given, stored in (("http://localhost:5002", "http://localhost:5002"),
                              ("http://10.1.2.3:5099/", "http://10.1.2.3:5099")):
            p = _cfg(tmp_path, {"agent_name": "n"})
            assert _tool().set_forum_url(p, given).status == "written"
            cfg = json.loads(p.read_text())
            assert fa.forum_url_from_config(cfg) == stored
            assert hook._is_forum_configured(cfg) is True
            # the client base is the same with or without the stripped slash
            assert fa.ForumClient(fa.forum_url_from_config(cfg))._base == stored

    def test_ia_cli_gate_agrees(self, tmp_path):
        ia = _load(IA, "ia_s9")
        p = _cfg(tmp_path, {})
        assert ia._is_forum_configured({}) is False
        _tool().set_forum_url(p, URL)
        assert ia._is_forum_configured(json.loads(p.read_text())) is True


# ---------------------------------------------------------------- S8 ----

_STEP_RE = re.compile(r"^# >>> forum-url-step\n(.*?)^# <<< forum-url-step$",
                      re.S | re.M)


def _step_block():
    m = _STEP_RE.search(INSTALLER.read_text())
    assert m, "install-forum-service.sh has no '# >>> forum-url-step' block"
    return m.group(1)


def _run_step(tmp_path, *, port="5002", dry=0, home=None):
    home = home if home is not None else tmp_path
    script = (
        "set -euo pipefail\n"
        f'SRC="{REPO}"\nPORT="{port}"\nDRY_RUN={dry}\n'
        f'export ENGRAM_HOME="{home}"\n'
        + _step_block()
        + "\necho STEP-DONE\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env=_hermetic_env())


def _real_dry_run(tmp_path, engram_home, *, port):
    return subprocess.run(
        ["bash", str(INSTALLER), "--src", str(REPO), "--service-dir",
         str(tmp_path / "forum"), "--group", "agents", "--port", port,
         "--dry-run"], capture_output=True, text=True,
        env=_hermetic_env(ENGRAM_HOME=str(engram_home)))


class TestS8Installer:
    @pytest.mark.parametrize("port", ["5002", "5099"])
    def test_step_writes_localhost_port(self, tmp_path, port):
        _write(tmp_path / "config.json", {"agent_name": "n", "mode": "m"})
        r = _run_step(tmp_path, port=port)
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "STEP-DONE" in r.stdout
        d = json.loads((tmp_path / "config.json").read_text())
        assert d["forum"]["url"] == f"http://localhost:{port}"
        assert d["agent_name"] == "n" and d["mode"] == "m"

    def test_step_reports_tool_result_and_other_seat_commands(self, tmp_path):
        _write(tmp_path / "config.json", {})
        r = _run_step(tmp_path)
        out = r.stdout + r.stderr
        assert "written" in out
        assert "set_forum_url.py http://localhost:5002" in out
        assert "http://<this-host-address>:5002" in out

    def test_preexisting_different_url_kept_install_still_succeeds(self, tmp_path):
        _write(tmp_path / "config.json", {"forum": {"url": "http://other:5002"}})
        before = (tmp_path / "config.json").read_bytes()
        r = _run_step(tmp_path)
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "STEP-DONE" in r.stdout
        assert (tmp_path / "config.json").read_bytes() == before
        assert "WARN" in out
        assert "--force" in out and "set_forum_url.py" in out

    def test_missing_admin_config_does_not_fail(self, tmp_path):
        r = _run_step(tmp_path)
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "STEP-DONE" in r.stdout
        assert "WARN" in out
        assert not (tmp_path / "config.json").exists()

    def test_step_dry_run_writes_nothing_and_says_so(self, tmp_path):
        _write(tmp_path / "config.json", {"agent_name": "n"})
        before = (tmp_path / "config.json").read_bytes()
        r = _run_step(tmp_path, dry=1)
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert (tmp_path / "config.json").read_bytes() == before
        assert "[dry-run]" in out and "set_forum_url.py" in out

    def test_real_script_dry_run_prints_step_and_writes_nothing(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        _write(home / "config.json", {"agent_name": "n"})
        before = (home / "config.json").read_bytes()
        r = _real_dry_run(tmp_path, home, port="5099")
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert (home / "config.json").read_bytes() == before
        assert "[dry-run]" in out and "set_forum_url.py" in out
        assert "http://localhost:5099" in out
        assert "http://<this-host-address>:5099" in out

    def test_dry_run_is_hermetic_against_the_callers_unit_file(self, tmp_path, monkeypatch):
        # The installer reads $HOME/.config/systemd/user/engram-forum.service and
        # REFUSES when it already carries a baton login map. On a forum-admin
        # seat that file exists, so a cell that inherits HOME fails there only.
        real_home = tmp_path / "real-home"
        unit_dir = real_home / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        unit = unit_dir / "engram-forum.service"
        unit.write_text("[Service]\nExecStart=/x/python -m forum.server "
                        "--baton-login-map /etc/x/map.json\n")
        unit_before = unit.read_bytes()
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        engram_home = tmp_path / "eh"
        engram_home.mkdir()
        _write(engram_home / "config.json", {"agent_name": "n"})
        r = _real_dry_run(tmp_path, engram_home, port="5099")
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "[dry-run]" in out and "set_forum_url.py" in out
        assert unit.read_bytes() == unit_before

    def test_step_runs_after_the_post_install_gates(self):
        text = INSTALLER.read_text()
        assert text.index("# >>> forum-url-step") > text.index("verify-only passed")
        assert text.index("# >>> forum-url-step") < text.index('echo "==> Done."')

    def test_installer_still_parses(self):
        r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True,
                           env=_hermetic_env())
        assert r.returncode == 0, r.stderr

    def test_printed_commands_survive_a_path_with_a_space(self, tmp_path):
        src = tmp_path / "my src"
        tool_dir = src / "src" / "engram" / "tools"
        tool_dir.mkdir(parents=True)
        (tool_dir / "set_forum_url.py").write_text(TOOL.read_text())
        # #3863 step 3b: the tool resolves the home through its sibling tools/_owner_read.py, so a checkout-shaped
        # tools/ dir carries it (and the package marker) beside the tool.
        for sibling in ("__init__.py", "_owner_read.py"):
            (tool_dir / sibling).write_text((TOOL.parent / sibling).read_text())
        # The write goes through the file guard, which sits beside tools/ in a checkout.
        (tool_dir.parent / "engram_file_guard.py").write_text((TOOL.parent.parent / "engram_file_guard.py").read_text())
        _write(tmp_path / "config.json", {"forum": {"url": "http://other:5002"}})
        before = (tmp_path / "config.json").read_bytes()
        script = (
            "set -euo pipefail\n"
            f'SRC="{src}"\nPORT=5002\nDRY_RUN=0\nexport ENGRAM_HOME="{tmp_path}"\n'
            + _step_block()
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env=_hermetic_env())
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        cmds = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("python3 ")]
        cmds += [ln.split("THIS host:", 1)[1].strip() for ln in out.splitlines()
                 if "THIS host:" in ln]
        assert len(cmds) == 2, out
        for cmd in cmds:
            h = subprocess.run(["bash", "-c", cmd + " --help"], capture_output=True,
                               text=True, env=_hermetic_env())
            assert h.returncode == 0 and "usage" in h.stdout.lower(), (cmd, h.stdout, h.stderr)
        manual = subprocess.run(["bash", "-c", cmds[0]], capture_output=True, text=True,
                                env=_hermetic_env(ENGRAM_HOME=str(tmp_path)))
        assert manual.returncode == 3, (cmds[0], manual.stdout, manual.stderr)
        assert (tmp_path / "config.json").read_bytes() == before
