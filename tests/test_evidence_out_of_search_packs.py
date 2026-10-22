"""Round 2 of the evidence-out-of-search ruling (Lei 2026-10-08): attached-pack
FTS in the surface hook is a search path, so it excludes evidence too -- using
the ONE definition (engram_search_rules), not a hand-copied literal.

  PACK       TestPackFtsExcludesEvidence   (_query_attached_packs)
  SINGLE     TestSingleDefinition          (core re-exports the module's objects)
  DEPLOYED   TestDeployedLayout            (flattened plugin layout, no
                                            CLAUDE_PLUGIN_ROOT, resolves the module)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_surface_hook_attached_packs import (  # noqa: E402
    _build_pack_db, _load_hook_module, _write_config,
)

HOOKS_SRC = PROJECT_ROOT / "src" / "engram" / "hooks" / "claude"
RUNTIME_SRC = PROJECT_ROOT / "src" / "engram"

_ROWS = [
    {"id": "xx_ev01", "type": "evidence", "claim": "epistemology knowledge"},
    {"id": "xx_ob01", "type": "observation_factual",
     "claim": "epistemology studies the nature of knowledge and belief"},
]

import pytest  # noqa: E402
# #4029: in-process hook/tools loads run inside this window (sys.path / sys.modules restored).
pytestmark = pytest.mark.usefixtures("isolate_hook_loads")


class TestPackFtsExcludesEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram_pack_ev_test_")
        self.hook = _load_hook_module()
        self.hook.ENGRAM_HOME = self.tmp
        self.hook.CONFIG_PATH = os.path.join(self.hook.ENGRAM_HOME, "config.json")  # #3827: CONFIG_PATH is the hook's one config source
        pack_dir = os.path.join(self.tmp, "lib1")
        os.makedirs(pack_dir)
        _build_pack_db(os.path.join(pack_dir, "knowledge.db"), _ROWS)
        _write_config(self.tmp, {
            "attached_packs": [{"id": "lib1", "path": pack_dir, "enabled": True}]
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pack_search_returns_only_the_non_evidence_row(self):
        res = self.hook._query_attached_packs("epistemology knowledge", quota=5)
        self.assertEqual([r["id"] for r in res], ["xx_ob01"])

    def test_evidence_does_not_consume_the_quota(self):
        """quota=1 and the evidence row is the better bm25 match: the single
        slot must still go to the searchable row."""
        res = self.hook._query_attached_packs("epistemology knowledge", quota=1)
        self.assertEqual([r["id"] for r in res], ["xx_ob01"])


class TestSingleDefinition(unittest.TestCase):
    def test_core_reexports_the_modules_objects(self):
        import engram_core
        import engram_search_rules as rules
        self.assertIs(engram_core.SEARCH_EXCLUDED_TYPES, rules.SEARCH_EXCLUDED_TYPES)
        self.assertEqual(engram_core._not_search_excluded_sql("n"),
                         rules.not_search_excluded_sql("n"))
        self.assertEqual(engram_core._searchable_where_sql(),
                         rules.searchable_where_sql())

    def test_module_is_stdlib_only(self):
        src = (RUNTIME_SRC / "engram_search_rules.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.startswith(("import ", "from ")):
                self.assertTrue(line.startswith("from __future__"), line)


class _DeployedBase(unittest.TestCase):
    """Flattened deployed layout: <root>/hooks/<hook>.py with the runtime
    modules as siblings of hooks/ at the plugin root; CLAUDE_PLUGIN_ROOT and
    ENGRAM_RUNTIME_DIR UNSET unless a test sets it."""

    COPY_RULES = True  # False: engram_search_rules.py is absent from the tree

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="engram_pack_deployed_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "plugin_root"
        hooks = self.root / "hooks"
        hooks.mkdir(parents=True)
        for name in ("_hooklib.py", "_harness.py", "engram-surface-hook.py"):
            shutil.copy(HOOKS_SRC / name, hooks / name)
        names = ["engram_log_emitter.py"]
        if self.COPY_RULES:
            names.append("engram_search_rules.py")
        for name in names:
            src = RUNTIME_SRC / name
            if src.exists():  # absent pre-implementation: the test then fails on the import
                shutil.copy(src, self.root / name)
        (self.root / "engram_client.py").write_text(
            "class EngramClient:\n    def __init__(self,*a,**k): pass\n"
            "    def call(self,*a,**k): return {}\n")
        self.home = self.tmp / "engram_home"
        self.home.mkdir()
        pack_dir = self.tmp / "pack"
        pack_dir.mkdir()
        _build_pack_db(str(pack_dir / "knowledge.db"), _ROWS)
        _write_config(str(self.home), {
            "attached_packs": [{"id": "lib1", "path": str(pack_dir), "enabled": True}]
        })

    def _run_query(self, runtime_dir=None):
        """Load the flattened hook in a fresh interpreter and run the pack
        query. Returns (ids, module_file_or_None, stderr)."""
        script = (
            "import importlib.util, json, sys\n"
            f"p = {str(self.root / 'hooks' / 'engram-surface-hook.py')!r}\n"
            "spec = importlib.util.spec_from_file_location('hook', p)\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "res = m._query_attached_packs('epistemology knowledge', quota=5)\n"
            "r = sys.modules.get('engram_search_rules')\n"
            "print(json.dumps({'ids': [x['id'] for x in res],\n"
            "                  'mod': getattr(r, '__file__', None)}))\n"
        )
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_PLUGIN_ROOT", "ENGRAM_RUNTIME_DIR", "PYTHONPATH")}
        env["ENGRAM_HOME"] = str(self.home)
        if runtime_dir is not None:
            env["ENGRAM_RUNTIME_DIR"] = str(runtime_dir)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, env=env, timeout=30, cwd=str(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        return out["ids"], out["mod"], proc.stderr


class TestDeployedLayout(_DeployedBase):
    """CLAUDE_PLUGIN_ROOT and ENGRAM_RUNTIME_DIR UNSET (rung 2 of
    resolve_runtime_dir must find the module on its own)."""

    def test_flattened_hook_resolves_module_and_filters_pack(self):
        ids, mod, _ = self._run_query()
        self.assertEqual(ids, ["xx_ob01"])
        self.assertEqual(os.path.realpath(mod),
                         os.path.realpath(self.root / "engram_search_rules.py"))


class TestImportFallback(_DeployedBase):
    def test_runtime_dir_without_module_falls_back_to_parent_walk(self):
        """ENGRAM_RUNTIME_DIR is returned unchecked by resolve_runtime_dir, so
        it can name a dir that lacks the module; the parent walk must still
        find it and the pack results come back filtered."""
        empty = self.tmp / "empty_runtime"
        empty.mkdir()
        ids, mod, _ = self._run_query(runtime_dir=empty)
        self.assertEqual(ids, ["xx_ob01"])
        self.assertEqual(os.path.realpath(mod),
                         os.path.realpath(self.root / "engram_search_rules.py"))


class TestModuleAbsentEverywhere(_DeployedBase):
    COPY_RULES = False

    def test_fails_closed_and_logs_one_visible_line(self):
        ids, mod, err = self._run_query()
        self.assertEqual(ids, [])  # fail closed: no pack results, evidence or not
        self.assertIsNone(mod)
        lines = [ln for ln in err.splitlines()
                 if ln.startswith("engram-surface-hook: engram_search_rules unavailable")]
        self.assertEqual(len(lines), 1, err)
        self.assertIn("fail", lines[0].lower())


if __name__ == "__main__":
    unittest.main()
