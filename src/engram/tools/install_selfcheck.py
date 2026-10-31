#!/usr/bin/env python3
"""Post-install self-check for an ENGRAM seat (#4097 gap 4; slice 2).

    install_selfcheck.py [--phase pre-first-session|installed] [--json]

One line per check:

    PASS <name>[: <detail>]
    FAIL <name>: <what is wrong> — <the remedy>
    SKIP <name>: <why the check does not apply>

then ``SELFCHECK PASS n/n`` or ``SELFCHECK FAIL k/n failed`` (n counts PASS+FAIL; a SKIP is
not in the denominator). Exit 0 only if no check FAILs. ``--json`` prints ONE JSON object
instead: ``{"phase", "result", "checks": [{"name", "status", "detail"}, ...]}``.

SKIP is only for a check that does not apply (reason stated). A check that could not be
evaluated, or that raised, is a FAIL naming the cause: it never turns into a pass by
silence.

Phases. The graph and identity checks differ by phase, because the "freshly seeded" checks
read FAIL on every healthy seat after its first session (marker deleted, names substituted,
more than six nodes):

* ``installed`` (default; what the upgrade skill and re-runs use): the graph opens read-only
  and holds >= 6 nodes, every id in ``seed-manifest.json`` exists, ``~/.claude/CLAUDE.md``
  exists with no unfilled INSTALL-time placeholder.
* ``pre-first-session``: the exact seed (3 axioms, 2 definitions, 1 goal, 2 edges), the
  ``first-run-pending`` marker armed, only first-session placeholders left in CLAUDE.md.

The strongest check, ``mcp-handshake``, starts the MCP server exactly as the plugin's
``.mcp.json`` says and sends ``initialize`` over stdio, with ``HF_HUB_OFFLINE=1`` added so an
uncached embedding model fails here instead of being quietly downloaded (which would hide a
failed pre-warm). It is NOT read-only: server startup does real work (git init of the data
dir, the reconcilers). What it must not do is change the graph (node count, edge count and
the seed ids stay as they were), and it must never disturb a live seat:

* if ``$ENGRAM_HOME/mcp-tools-ready.json`` names a PID that is alive, a live MCP server is
  already serving this home, so no second server is started (``SKIP``: that server's own
  startup is the evidence) and the marker is not touched;
* otherwise the handshake runs, and the marker is put back afterwards exactly as it was
  (same bytes and mtime if it existed, removed if it did not), because the second server
  overwrites it with the PID of a process that is dead once the check returns, and the
  session hooks read that file to judge whether the real server is running.

The handshake is strict about stdout: the server's first non-JSON line there is an immediate
FAIL, because MCP's stdio transport reserves stdout for JSON-RPC, so a server writing anything
else to it is broken even though Claude Code's client is more tolerant (it logs such a line and
carries on). Both PASS details (``model-cached``, ``mcp-handshake``) name the HuggingFace cache
directory used; the real server is started by Claude Code from its own environment, so
``HF_HOME`` must be set in the environment Claude Code inherits, not only in this shell.

Paths: ``ENGRAM_HOME`` (absolute; default ``~/.engram``; resolved by the repo's one definition,
``tools/_owner_read.engram_home()``, which refuses a relative value), ``CLAUDE_HOME`` (default
``~/.claude``).
The venv is always ``~/.engram/venv`` (under HOME, not ENGRAM_HOME). Environment knob for
tests: ``INSTALL_SELFCHECK_HANDSHAKE_TIMEOUT`` (seconds, default 60).

Stdlib only; Python 3.9+ (macOS /usr/bin/python3 is 3.9); Linux and macOS.
"""

import argparse
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

HANDSHAKE_TIMEOUT_DEFAULT = 60
MIN_PYTHON = (3, 10, 7)
PLUGIN_ID = "engram@engram-local"
MARKETPLACE_NAME = "engram-local"
CLAUDE_INSTALL_REMEDY = "install Claude Code with the native installer: curl -fsSL https://claude.ai/install.sh | bash"
FIRST_SESSION_TOKENS = frozenset(
    ["{{AGENT_NAME}}", "{{USER_NAME}}", "{{TODAY}}", "{{SELF_NODE_ID}}", "{{COUNTERPART_NAME}}"]
)
TOKEN_RE = re.compile(r"\{\{[^{}\n]+\}\}")
SEED_COUNTS = {"axiom": 3, "definition": 2, "goal": 1}
SEED_EDGES = 2
MIN_INSTALLED_NODES = 6

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class Ctx:
    """Everything a check needs, resolved once."""

    def __init__(self, phase, home, engram_home, claude_home, handshake_timeout):
        self.phase = phase
        self.home = Path(home)
        self.engram_home = Path(engram_home)
        self.claude_home = Path(claude_home)
        self.handshake_timeout = handshake_timeout
        self.venv_py = Path.home() / ".engram" / "venv" / "bin" / "python"
        self.plugin_dir = self.engram_home / "marketplace" / "plugins" / "engram"
        self.db_path = self.engram_home / "knowledge.db"
        self.claude_md = self.claude_home / "CLAUDE.md"
        self.graph_decision = None  # the graph path, decided ONCE per run (see _graph_decision)


def resolve_engram_home():
    """The ENGRAM home, from the repo's ONE definition for tools (tools/_owner_read.engram_home():
    $ENGRAM_HOME, default ~/.engram, expanded, absolute; a relative value is refused with
    ValueError). `tools` is found one directory above this file in BOTH layouts (src/engram/
    and the deployed plugin root). Tests set ENGRAM_HOME in the subprocess environment."""
    pkg_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    if pkg_root not in sys.path:
        sys.path.append(pkg_root)
    from tools import _owner_read  # noqa: E402
    return Path(_owner_read.engram_home())


def _fail(what, remedy):
    return FAIL, "%s — %s" % (what, remedy)


def _tail(text, n=3, width=300):
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[-width:]


def _run_venv(ctx, code, *args, timeout=180):
    return subprocess.run(
        [str(ctx.venv_py), "-c", code, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False,
    )


def _venv_missing(ctx):
    return _fail("venv python not found at %s" % ctx.venv_py,
                 "create it: python3 -m venv ~/.engram/venv && ~/.engram/venv/bin/python -m pip install -r requirements/requirements.txt")


# ── 1-3: the venv ────────────────────────────────────────────────────────────

def check_python(ctx):
    if not ctx.venv_py.exists():
        return _venv_missing(ctx)
    proc = _run_venv(ctx, "import sys; print('.'.join(str(p) for p in sys.version_info[:3]))")
    out = proc.stdout.strip()
    try:
        ver = tuple(int(p) for p in out.split(".")[:3])
    except ValueError:
        return _fail("venv python gave no version (rc=%s): %s" % (proc.returncode, _tail(proc.stderr) or out),
                     "recreate the venv with Python >= 3.10.7")
    if ver < MIN_PYTHON:
        return _fail("venv python is %s, need >= %s" % (out, ".".join(map(str, MIN_PYTHON))),
                     "recreate the venv with a newer python3: rm -rf ~/.engram/venv && python3 -m venv ~/.engram/venv")
    return PASS, out


def check_venv_imports(ctx):
    if not ctx.venv_py.exists():
        return _venv_missing(ctx)
    proc = _run_venv(ctx, "import fastmcp, sentence_transformers")
    if proc.returncode != 0:
        return _fail("the venv cannot import fastmcp and sentence_transformers: %s" % _tail(proc.stderr, n=1),
                     "~/.engram/venv/bin/python -m pip install -r requirements/requirements.txt")
    return PASS, ""


_MODEL_SNIPPET = """
import os
import sys
sys.path.insert(0, sys.argv[1])
import engram_core as core
import server
model = core._get_embedding_config().get("model", core.DEFAULT_EMBEDDING_MODEL)
def cache_dir():
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(var):
            return os.environ[var]
    if os.environ.get("HF_HOME"):
        return os.path.join(os.environ["HF_HOME"], "hub")
    try:
        from huggingface_hub import constants
        return str(constants.HF_HUB_CACHE)
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
print(model)
print(cache_dir())
print("CACHED" if server._embedding_cache_status(model) else "MISSING")
"""


def check_model_cached(ctx):
    if os.environ.get("ENGRAM_NO_EMBEDDINGS"):
        return SKIP, "ENGRAM_NO_EMBEDDINGS is set: this seat runs deliberately without embeddings"
    if not ctx.venv_py.exists():
        return _venv_missing(ctx)
    proc = _run_venv(ctx, _MODEL_SNIPPET, str(ctx.plugin_dir))
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode != 0 or len(lines) < 3:
        return _fail("could not evaluate the model cache (rc=%s): %s" % (proc.returncode, _tail(proc.stderr)),
                     "check the plugin tree at %s and the venv imports above" % ctx.plugin_dir)
    model, cache, verdict = lines[-3], lines[-2], lines[-1]
    if verdict != "CACHED":
        return _fail("embedding model %s is not cached (HuggingFace cache under %s)" % (model, cache),
                     "%s -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('%s')\""
                     % (ctx.venv_py, model))
    return PASS, "%s (HuggingFace cache %s)" % (model, cache)


# ── 4-5: the installed tree ──────────────────────────────────────────────────

def check_plugin_tree(ctx):
    path = ctx.plugin_dir / "plugin.json"
    if not path.is_file():
        return _fail("%s does not exist" % path, "re-run tools/install-local-marketplace.sh")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return _fail("%s does not parse: %s" % (path, exc), "re-run tools/install-local-marketplace.sh")
    if not isinstance(data, dict):
        return _fail("%s is not a JSON object" % path, "re-run tools/install-local-marketplace.sh")
    return PASS, "version %s" % data.get("version", "unknown")


def check_deployed_version(ctx):
    path = ctx.engram_home / ".deployed-version"
    if not path.is_file():
        return _fail("%s does not exist" % path, "re-run tools/install-local-marketplace.sh")
    for line in path.read_text().splitlines():
        if line.startswith("alpha_sha="):
            sha = line.split("=", 1)[1].strip()
            if sha:
                return PASS, "alpha_sha=%s" % sha
    return _fail("%s has no alpha_sha= line" % path, "re-run tools/install-local-marketplace.sh")


# ── 6-7: Claude Code's view ──────────────────────────────────────────────────

def _claude_json(*args):
    """Returns (parsed, None) or (None, (what, remedy))."""
    exe = shutil.which("claude")
    if not exe:
        return None, ("`claude` is not on PATH", CLAUDE_INSTALL_REMEDY)
    proc = subprocess.run([exe, *args, "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=90, check=False)
    if proc.returncode != 0:
        return None, ("`claude %s --json` exited rc=%s: %s" % (" ".join(args), proc.returncode, _tail(proc.stderr)),
                      "check the Claude Code install: claude --version")
    try:
        return json.loads(proc.stdout), None
    except ValueError as exc:
        return None, ("`claude %s --json` printed no JSON (%s)" % (" ".join(args), exc),
                      "update Claude Code (the --json flag is needed)")


def check_marketplace_registered(ctx):
    data, err = _claude_json("plugin", "marketplace", "list")
    if err:
        return _fail(*err)
    names = [m.get("name") for m in data if isinstance(m, dict)] if isinstance(data, list) else []
    if MARKETPLACE_NAME not in names:
        return _fail("marketplace %s is not registered (found: %s)" % (MARKETPLACE_NAME, ", ".join(map(str, names)) or "none"),
                     "claude plugin marketplace add %s" % (ctx.engram_home / "marketplace"))
    return PASS, ""


def check_plugin_enabled(ctx):
    data, err = _claude_json("plugin", "list")
    if err:
        return _fail(*err)
    plugins = [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
    mine = [p for p in plugins if p.get("id") == PLUGIN_ID]
    if not mine:
        return _fail("plugin %s is not installed" % PLUGIN_ID, "claude plugin install %s" % PLUGIN_ID)
    if mine[0].get("enabled") is not True:
        return _fail("plugin %s is installed but not enabled" % PLUGIN_ID, "claude plugin enable %s" % PLUGIN_ID)
    return PASS, ""


# ── 8: the MCP handshake ─────────────────────────────────────────────────────

def _fmt_seconds(t):
    return "%g" % t


def _hf_cache_dir(env, home):
    """The HuggingFace hub cache dir a process with environment `env` would use (stdlib only:
    the same precedence as the installer pre-warm, minus the huggingface_hub constants lookup,
    whose default this falls back to)."""
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if env.get(var):
            return env[var]
    if env.get("HF_HOME"):
        return os.path.join(env["HF_HOME"], "hub")
    return os.path.join(str(home), ".cache", "huggingface", "hub")


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=5)


MARKER_NAME = "mcp-tools-ready.json"


def _live_marker_pid(marker):
    """The PID the readiness marker names if that process is alive, else None (no marker, an
    unreadable or malformed one, or a dead PID all read as 'no live server')."""
    try:
        pid = json.loads(marker.read_bytes()).get("pid")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by another user
    except OSError:
        return None
    return pid


def _snapshot_marker(marker):
    """(bytes, atime_ns, mtime_ns) of the marker, None if it is absent."""
    try:
        data = marker.read_bytes()
        st = marker.stat()
    except FileNotFoundError:
        return None
    return data, st.st_atime_ns, st.st_mtime_ns


def _restore_marker(marker, snap):
    """Put the marker back exactly as _snapshot_marker saw it. Returns an error string or None."""
    try:
        if snap is None:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            return None
        data, atime_ns, mtime_ns = snap
        tmp = marker.with_name(marker.name + ".selfcheck-restore")
        tmp.write_bytes(data)
        os.utime(tmp, ns=(atime_ns, mtime_ns))
        os.replace(tmp, marker)
    except OSError as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    return None


def check_mcp_handshake(ctx):
    """Start the server and ask for ``initialize``, without ever disturbing a live seat (see the
    module docstring): skip when a live server owns the marker, else restore the marker."""
    marker = ctx.engram_home / MARKER_NAME
    live = _live_marker_pid(marker)
    if live is not None:
        return SKIP, "a live MCP server (pid %d) is serving this home; its own startup is the evidence" % live
    snap = _snapshot_marker(marker)
    try:
        status, detail = _handshake(ctx)
    finally:
        restore_err = _restore_marker(marker, snap)
    if restore_err:
        return _fail("the handshake could not put %s back as it was (%s)" % (marker, restore_err),
                     "check the marker by hand; a stale one makes the session hooks report the MCP server as down")
    return status, detail


def _handshake(ctx):
    mcp_path = ctx.plugin_dir / ".mcp.json"
    if not mcp_path.is_file():
        return _fail("%s does not exist" % mcp_path, "re-run tools/install-local-marketplace.sh")
    try:
        servers = json.loads(mcp_path.read_text())["mcpServers"]
        spec = servers.get("engram") or (list(servers.values())[0] if len(servers) == 1 else None)
        command = spec["command"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        return _fail("%s has no usable engram server entry (%s: %s)" % (mcp_path, type(exc).__name__, exc),
                     "re-run tools/install-local-marketplace.sh")

    root = str(ctx.plugin_dir)

    def subst(value):
        return str(value).replace("${CLAUDE_PLUGIN_ROOT}", root)

    argv = [subst(command)] + [subst(a) for a in spec.get("args", [])]
    env = dict(os.environ)
    env.update({k: subst(v) for k, v in (spec.get("env") or {}).items()})
    env["CLAUDE_PLUGIN_ROOT"] = root
    env["ENGRAM_HOME"] = str(ctx.engram_home)
    env["HF_HUB_OFFLINE"] = "1"  # an uncached model must FAIL here, never download quietly

    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "install_selfcheck", "version": "1"}},
    }
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, text=True, errors="replace", start_new_session=True)
    except OSError as exc:
        return _fail("could not start %s: %s" % (argv[0], exc), "re-run tools/install-local-marketplace.sh")

    cache_note = _hf_cache_dir(env, ctx.home)
    out_q = queue.Queue()
    err_tail = deque(maxlen=20)

    def pump_out():
        for line in proc.stdout:
            out_q.put(line)

    def pump_err():
        for line in proc.stderr:
            err_tail.append(line.rstrip("\n"))

    t_out = threading.Thread(target=pump_out, daemon=True)
    t_err = threading.Thread(target=pump_err, daemon=True)
    t_out.start()
    t_err.start()

    def stderr_tail():
        t_err.join(timeout=2)
        return _tail("\n".join(err_tail), n=4, width=400)

    try:
        try:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # the server already died; the loop below reports its exit
        deadline = time.monotonic() + ctx.handshake_timeout
        while True:
            if time.monotonic() >= deadline:
                tail = stderr_tail()
                return _fail("timeout after %ss waiting for the initialize result%s"
                             % (_fmt_seconds(ctx.handshake_timeout), (" (stderr: %s)" % tail) if tail else ""),
                             "run %s by hand and watch where it stalls" % argv[0])
            try:
                line = out_q.get(timeout=0.2)
            except queue.Empty:
                line = None
            if line is not None:
                try:
                    doc = json.loads(line)
                except ValueError:
                    return _fail("the server wrote something that is not JSON-RPC: %r" % line.strip()[:120],
                                 "run %s by hand and read its output" % argv[0])
                if not isinstance(doc, dict) or doc.get("id") != 1:
                    continue  # a notification or log-shaped message; keep waiting for our reply
                if isinstance(doc.get("result"), dict):
                    info = doc["result"].get("serverInfo") or {}
                    return PASS, "initialize answered by %s (HuggingFace cache %s)" % (info.get("name") or "server", cache_note)
                return _fail("initialize returned no result: %s" % json.dumps(doc.get("error", doc))[:200],
                             "run %s by hand and read its stderr" % argv[0])
            if proc.poll() is not None and out_q.empty():
                t_out.join(timeout=1)
                if not out_q.empty():
                    continue
                return _fail("server exited rc=%s before answering initialize: %s" % (proc.returncode, stderr_tail()),
                             "run %s by hand to reproduce; an uncached embedding model needs the pre-warm remedy"
                             % argv[0])
    finally:
        _kill_group(proc)


# ── 9: graph and identity, by phase ──────────────────────────────────────────

FLAG_KEY = "install_selfcheck_reads_via_owner"
GRAPH_CHECKS = ("seed-graph", "graph-opens", "seed-ids-present")


class SelfcheckOwnerReadError(Exception):
    """An owner read this tool needed failed. Carries the helper's own wording (what the owner answered
    and what to do about it)."""


def _owner_read():
    pkg_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    if pkg_root not in sys.path:
        sys.path.append(pkg_root)
    from tools import _owner_read  # noqa: E402
    return _owner_read


def _spec(ctx):
    return _owner_read().OwnerReadSpec(
        flag_key=FLAG_KEY, error_cls=SelfcheckOwnerReadError, home=ctx.engram_home,
        never_empty="An unreadable graph is a FAIL in this self-check, never an empty or a zero-node graph.")


def _graph_decision(ctx):
    """How THIS run reads the graph, decided once and cached on ctx, so every graph check of a run takes
    the same path (#4118). Returns {"mode": "direct"|"owner"|"refused", "label": ..., "error": ...}.

    * flag OFF (or the flag's store is not this home's): the direct `mode=ro` open, as before.
    * flag ON and the owner answers a ping: every graph read goes through the owner, none through a
      direct open.
    * flag ON and the ping is Unavailable with reason `down` (nothing is listening): the ruled fallback,
      the direct `mode=ro` open. This is the installer re-run before any session, when no owner runs.
    * flag ON and anything else (the owner is present but refuses, times out, is busy or answers garbage):
      REFUSED. The graph checks FAIL naming the owner's answer; no direct open beside a live owner.

    An owner that has not yet bound its socket (mid-startup) reads as `down`, so for that moment the
    tool takes the direct read-only path. That is inside the ruling ("no live owner") and it only reads.
    """
    if ctx.graph_decision is not None:
        return ctx.graph_decision
    ctx.graph_decision = _decide_graph_path(ctx)
    return ctx.graph_decision


def _decide_graph_path(ctx):
    direct_off = {"mode": "direct", "label": "direct (flag off)", "error": None}
    try:
        owner_read = _owner_read()
        spec = _spec(ctx)
        if not owner_read.flag_applies(spec, ctx.db_path):
            return direct_off
    except Exception as exc:  # noqa: BLE001 -- the flag's state unknown reads as OFF, as the helper does
        sys.stderr.write("engramd.%s: could not read the flag (%s)\n" % (FLAG_KEY, type(exc).__name__))
        return direct_off
    refusal_remedy = "this check never opens the store directly beside a live owner"
    try:
        client = owner_read.import_runtime("engramd_client")
        result = client.read("ping", {}, timeout=spec.per_call, socket_path=client.socket_path_for_home(spec.home))
    except (Exception, SystemExit) as exc:  # client.read does not raise by contract; belt
        return {"mode": "refused", "label": "refused (owner ping failed)", "error": (
            "engramd.%s is on but the owner could not be pinged (%s: %s)" % (FLAG_KEY, type(exc).__name__, exc),
            refusal_remedy)}
    if isinstance(result, client.Ok):
        return {"mode": "owner", "label": "owner", "error": None}
    if isinstance(result, client.Unavailable) and result.reason == "down":
        return {"mode": "direct", "label": "direct (no live owner: down)", "error": None}
    return {"mode": "refused", "label": "refused (owner unhealthy)",
            "error": (str(owner_read.failure(spec, client, result)), refusal_remedy)}


def _open_ro(ctx):
    uri = "file:%s?mode=ro" % quote(str(ctx.db_path))
    return sqlite3.connect(uri, uri=True, timeout=5)


# The graph statements. Each is a literal at its execute call so the read-template scanner registers it
# (tools/engramd_read_scan.config.json roots these two functions); both paths use the same statements.
def _read_counts(con):
    """(type_counts, edges) from one connection (direct or the owner's read shim)."""
    by_type = dict(con.execute("SELECT type, COUNT(*) FROM nodes GROUP BY type").fetchall())
    edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    return by_type, edges


def _read_present_ids(con, ids):
    """The subset of `ids` that exist as nodes (the ids travel as ONE JSON array, so the arity is fixed)."""
    rows = con.execute("SELECT id FROM nodes WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(list(ids)),))
    return {r[0] for r in rows.fetchall()}


def _read_present_ids_direct(con, ids):
    """The direct-path twin of _read_present_ids, with the statement the tool always used (one placeholder per
    id), so a flag-OFF run is unchanged and needs no JSON1 in the local SQLite. It is not a scanner root: the
    owner is never sent this statement, only the registered json_each form above."""
    ids = list(ids)
    marks = ",".join("?" * len(ids))
    return {r[0] for r in con.execute("SELECT id FROM nodes WHERE id IN (%s)" % marks, ids)}


def _read_graph(ctx):
    """Returns ((type_counts, total_nodes, edges, con), None) or (None, (what, remedy))."""
    decision = _graph_decision(ctx)
    if decision["mode"] == "refused":
        return None, decision["error"]
    con = None
    try:
        if decision["mode"] == "owner":
            con = _owner_read().open_reader(_spec(ctx), ctx.db_path, use_owner=True)
        else:
            if not ctx.db_path.is_file():
                return None, ("%s does not exist" % ctx.db_path, "run bootstrap.py (fresh install) or restore the graph")
            if decision["label"] != "direct (flag off)":
                _owner_read().check_store_path(ctx.db_path)  # a FIFO is refused here, never opened
            con = _open_ro(ctx)
        by_type, edges = _read_counts(con)
    except SelfcheckOwnerReadError as exc:
        if con is not None:
            con.close()
        return None, (str(exc), "this check never opens the store directly beside a live owner")
    except (_owner_read().StoreUnusable, FileNotFoundError) as exc:
        return None, (str(exc), "fix the store path")
    except sqlite3.Error as exc:
        if con is not None:
            con.close()
        return None, ("%s does not open read-only: %s" % (ctx.db_path, exc), "restore the graph from backup")
    return (by_type, sum(by_type.values()), edges, con), None


def check_seed_graph(ctx):
    got, err = _read_graph(ctx)
    if err:
        return _fail(*err)
    by_type, total, edges, con = got
    con.close()
    problems = []
    for typ, want in SEED_COUNTS.items():
        if by_type.get(typ, 0) != want:
            problems.append("%s=%s (want %s)" % (typ, by_type.get(typ, 0), want))
    if total != sum(SEED_COUNTS.values()):
        problems.append("nodes=%s (want %s)" % (total, sum(SEED_COUNTS.values())))
    if edges != SEED_EDGES:
        problems.append("edges=%s (want %s)" % (edges, SEED_EDGES))
    if problems:
        return _fail("the seed graph is wrong: %s" % ", ".join(problems),
                     "this check is for a seat that has not had its first session; use --phase installed otherwise")
    return PASS, "3 axioms, 2 definitions, 1 goal, 2 edges"


def check_first_run_pending(ctx):
    path = ctx.engram_home / "first-run-pending"
    if not path.is_file():
        return _fail("%s does not exist" % path,
                     "bootstrap.py arms it; if the first session already ran, use --phase installed")
    return PASS, ""


def _claude_md_tokens(ctx):
    if not ctx.claude_md.is_file():
        return None
    return sorted(set(TOKEN_RE.findall(ctx.claude_md.read_text(errors="replace"))))


def check_claude_md_rendered(ctx):
    tokens = _claude_md_tokens(ctx)
    if tokens is None:
        return _fail("%s does not exist" % ctx.claude_md, "run bootstrap.py to render it from the template")
    bad = [t for t in tokens if t not in FIRST_SESSION_TOKENS]
    if bad:
        return _fail("%s still has install-time placeholders: %s" % (ctx.claude_md, ", ".join(bad)),
                     "re-run bootstrap.py; only the first-session placeholders may remain before the first session")
    return PASS, ""


def check_graph_opens(ctx):
    got, err = _read_graph(ctx)
    if err:
        return _fail(*err)
    _, total, _, con = got
    con.close()
    if total < MIN_INSTALLED_NODES:
        return _fail("the graph has %s nodes, expected at least %s" % (total, MIN_INSTALLED_NODES),
                     "run bootstrap.py (fresh install) or restore the graph from backup")
    return PASS, "%s nodes" % total


def check_seed_ids_present(ctx):
    path = ctx.engram_home / "seed-manifest.json"
    if not path.is_file():
        return _fail("%s does not exist" % path, "run bootstrap.py, which writes it")
    try:
        ids = [v for v in json.loads(path.read_text())["seed_ids"].values() if isinstance(v, str)]
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        return _fail("%s is unreadable (%s: %s)" % (path, type(exc).__name__, exc), "re-run bootstrap.py")
    if not ids:
        return _fail("%s lists no seed ids" % path, "re-run bootstrap.py")
    got, err = _read_graph(ctx)
    if err:
        return _fail(*err)
    _, _, _, con = got
    try:
        if _graph_decision(ctx)["mode"] == "owner":
            present = _read_present_ids(con, ids)
        else:
            present = _read_present_ids_direct(con, ids)
    except SelfcheckOwnerReadError as exc:
        return _fail(str(exc), "this check never opens the store directly beside a live owner")
    except sqlite3.Error as exc:
        return _fail("%s could not be read: %s" % (ctx.db_path, exc), "restore the graph from backup")
    finally:
        con.close()
    missing = [i for i in ids if i not in present]
    if missing:
        return _fail("seed nodes missing from the graph: %s" % ", ".join(missing), "restore the graph from backup")
    return PASS, "%s seed ids" % len(ids)


def check_claude_md_present(ctx):
    tokens = _claude_md_tokens(ctx)
    if tokens is None:
        return _fail("%s does not exist" % ctx.claude_md, "run bootstrap.py to render it from the template")
    bad = [t for t in tokens if t not in FIRST_SESSION_TOKENS]
    if bad:
        return _fail("%s has unfilled install-time placeholders: %s" % (ctx.claude_md, ", ".join(bad)),
                     "re-render it: re-run bootstrap.py, or fill the placeholders by hand")
    return PASS, ""


# ── runner ───────────────────────────────────────────────────────────────────

COMMON_CHECKS = [
    ("python", check_python),
    ("venv-imports", check_venv_imports),
    ("model-cached", check_model_cached),
    ("plugin-tree", check_plugin_tree),
    ("deployed-version", check_deployed_version),
    ("marketplace-registered", check_marketplace_registered),
    ("plugin-enabled", check_plugin_enabled),
    ("mcp-handshake", check_mcp_handshake),
]
PHASE_CHECKS = {
    "pre-first-session": [
        ("seed-graph", check_seed_graph),
        ("first-run-pending", check_first_run_pending),
        ("claude-md-rendered", check_claude_md_rendered),
    ],
    "installed": [
        ("graph-opens", check_graph_opens),
        ("seed-ids-present", check_seed_ids_present),
        ("claude-md-present", check_claude_md_present),
    ],
}


def run_checks(checks, ctx):
    """Run each (name, fn) in order. A check that raises is a FAIL naming the exception."""
    results = []
    noted = False
    for name, fn in checks:
        try:
            status, detail = fn(ctx)
        except Exception as exc:  # noqa: BLE001 -- a crashing check must report, never crash the run
            status, detail = FAIL, "check crashed: %s: %s — report this as a bug in install_selfcheck.py" % (
                type(exc).__name__, exc)
        result = {"name": name, "status": status, "detail": detail}
        if name in GRAPH_CHECKS:
            label = ctx.graph_decision["label"] if ctx.graph_decision else None
            result["path"] = label
            if label and not noted:  # the first graph check of the run carries the note in its text
                noted = True
                result["detail"] = ("%s (path: %s)" % (detail, label)) if detail else "(path: %s)" % label
        results.append(result)
    return results


def format_line(result):
    status, name, detail = result["status"], result["name"], result["detail"]
    if status == PASS:
        return "PASS %s%s" % (name, (": " + detail) if detail else "")
    return "%s %s: %s" % (status, name, detail)


def summarize(results):
    failed = sum(1 for r in results if r["status"] == FAIL)
    counted = sum(1 for r in results if r["status"] in (PASS, FAIL))
    if failed:
        return "FAIL", "SELFCHECK FAIL %d/%d failed" % (failed, counted)
    return "PASS", "SELFCHECK PASS %d/%d" % (counted, counted)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Post-install self-check for an ENGRAM seat.")
    parser.add_argument("--phase", choices=sorted(PHASE_CHECKS), default="installed")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print one JSON object instead of lines")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    home = Path.home()
    try:
        timeout = float(os.environ.get("INSTALL_SELFCHECK_HANDSHAKE_TIMEOUT") or HANDSHAKE_TIMEOUT_DEFAULT)
    except ValueError:
        timeout = float(HANDSHAKE_TIMEOUT_DEFAULT)
    try:
        engram_home = resolve_engram_home()
    except (ValueError, ImportError) as exc:
        remedy = ("set ENGRAM_HOME to an absolute path, or unset it" if isinstance(exc, ValueError)
                  else "tools/_owner_read.py (the ENGRAM-home definition) is missing or broken: reinstall")
        results = [{"name": "engram-home", "status": FAIL, "detail": "%s — %s" % (exc, remedy)}]
        ctx = None
    else:
        claude_home = Path(os.path.expanduser(os.environ.get("CLAUDE_HOME") or "~/.claude"))
        ctx = Ctx(args.phase, home, engram_home, claude_home, timeout)
        results = run_checks(COMMON_CHECKS + PHASE_CHECKS[args.phase], ctx)

    result, summary = summarize(results)
    if args.as_json:
        print(json.dumps({"phase": args.phase, "result": result, "checks": results}, indent=2))
    else:
        for r in results:
            print(format_line(r))
        print(summary)
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
