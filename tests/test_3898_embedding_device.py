"""Contract tests for `embedding.device` / `embedding.cpu_threads` (#3898).

ENGRAM loaded its embedder through SentenceTransformer, which silently takes
CUDA whenever a GPU exists (about 750 MB VRAM per process, no latency gain for
single-query recall). The contract:

  * `embedding.device` in {cpu, cuda, auto}, DEFAULT cpu, applied at all FOUR
    model loads (three SentenceTransformer sites + the polarity CrossEncoder).
  * The CPU path never touches CUDA.
  * `embedding.cpu_threads` (default 4) caps torch's intra-op threads on EVERY
    path where the loaded model landed on CPU.

TEST ENVIRONMENT TRAP. The venv torch is a CPU-only build and system python
has no torch at all, so a real `torch.cuda.is_initialized()` assertion would be
VACUOUS here (always False). The "never touches CUDA" contract is therefore
tested with a SPY: fake `torch` and `sentence_transformers` modules whose CUDA
entry points record every call and (on the CPU path) raise AssertionError. The
real-torch tests below are additions, importorskip-guarded, and are
non-vacuous only on a CUDA build. No model is ever downloaded; no GPU needed.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL = "all-MiniLM-L6-v2"
ALLOWED = ("cpu", "cuda", "auto")


# ---------------------------------------------------------------------------
# Spy fakes
# ---------------------------------------------------------------------------


class _Dev(str):
    @property
    def type(self):
        return str(self).split(":")[0]


class _Vec:
    def tolist(self):
        return [0.1, 0.2, 0.3]


class _Rec:
    """Harmless stand-in that RECORDS every attribute access under its dotted
    name (into spy.cuda_calls) and returns another recorder. Nothing raises:
    the tests assert `spy.cuda_calls == []` on the cpu paths, so a wrong
    implementation goes red through the record, never through a crash that the
    loaders' generic `except` would swallow."""

    def __init__(self, spy, name):
        object.__setattr__(self, "_spy", spy)
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        full = f"{self._name}.{attr}"
        self._spy.cuda_calls.append(full)
        if full == "torch.cuda.is_available":
            return lambda *a, **k: self._spy.gpu
        if full == "torch.cuda.device_count":
            return lambda *a, **k: 1 if self._spy.gpu else 0
        return _Rec(self._spy, full)

    def __call__(self, *a, **k):
        return _Rec(self._spy, self._name + "()")


class _FakeTorch(types.ModuleType):
    """Fake torch: set/get_num_threads are real members; EVERY other attribute
    (cuda, version, backends, ...) is recorded as `torch.<name>`."""

    def __init__(self, spy):
        super().__init__("torch")
        self._spy = spy

    def __getattr__(self, attr):
        if attr.startswith("__") or attr == "_spy":
            raise AttributeError(attr)
        self._spy.cuda_calls.append(f"torch.{attr}")
        return _Rec(self._spy, f"torch.{attr}")


class Spy:
    """Records every CUDA-ish torch access, thread-cap call and model construction."""

    def __init__(self, gpu: bool = False, forbid_cuda: bool = False):
        self.gpu = gpu                  # what the fake library would pick on its own
        self.forbid_cuda = forbid_cuda  # kept for call-site readability; see _Rec (no raising)
        self.cuda_calls: list[str] = []
        self.thread_calls: list = []
        self.st_calls: list[tuple] = []
        self.ce_calls: list[tuple] = []
        self.st_fail_local_only = False
        self.no_device = False          # fake ST models report no `.device`
        self.strict = None              # {'st': Signature, 'ce': Signature} -> bind or reject
        self.rejected: list[str] = []   # constructor calls the strict signature rejected
        self.threads_now = 32

    def modules(self):
        spy = self
        torch = _FakeTorch(spy)

        def set_num_threads(n):
            spy.thread_calls.append(n)
            spy.threads_now = n

        torch.set_num_threads = set_num_threads
        torch.get_num_threads = lambda: spy.threads_now

        class SentenceTransformer:
            def __init__(self, name, **kw):
                spy.st_calls.append((name, dict(kw)))
                if spy.strict:
                    try:
                        spy.strict["st"].bind(name, **kw)
                    except TypeError as exc:
                        spy.rejected.append(f"SentenceTransformer: {exc}")
                        raise
                if kw.get("local_files_only") and spy.st_fail_local_only:
                    raise OSError("not in cache")
                if not spy.no_device:
                    dev = kw.get("device") or ("cuda:0" if spy.gpu else "cpu")
                    self.device = _Dev(dev)

            def encode(self, text, convert_to_numpy=True):
                return _Vec()

        class CrossEncoder:
            def __init__(self, name, device=None, **kw):
                spy.ce_calls.append((name, device))
                if spy.strict:
                    given = {"device": device} if device is not None else {}
                    try:
                        spy.strict["ce"].bind(name, **given, **kw)
                    except TypeError as exc:
                        spy.rejected.append(f"CrossEncoder: {exc}")
                        raise
                self.device = _Dev(device or ("cuda:0" if spy.gpu else "cpu"))

        st = types.ModuleType("sentence_transformers")
        st.SentenceTransformer = SentenceTransformer
        st.CrossEncoder = CrossEncoder
        return {"torch": torch, "sentence_transformers": st}

    @property
    def st_devices(self):
        return [kw.get("device", "<unset>") for _n, kw in self.st_calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh engram_core pointed at a temp config; helper to set the config
    and install a Spy. Nothing here touches the live ENGRAM home."""
    for key in list(sys.modules):
        if key == "server" or key.startswith("engram_"):
            monkeypatch.delitem(sys.modules, key, raising=False)
    monkeypatch.delenv("ENGRAM_NO_EMBEDDINGS", raising=False)
    # conftest / other tests may set this process-wide; the polarity load is under test
    monkeypatch.delenv("ENGRAM_NO_POLARITY", raising=False)
    import engram_core as core

    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(core, "CONFIG_PATH", cfg_path)
    # #4447: the default model is checksum-verified against the real HF cache
    # after load; these tests drive a fake SentenceTransformer and never have
    # model bytes, so the verification is stubbed out here (it has its own
    # cells in test_embedding_model_pin_4447.py).
    monkeypatch.setattr(core, "_verify_pinned_snapshot", lambda name: None)

    class E:
        pass

    e = E()
    e.core = core
    e.tmp = tmp_path

    def set_config(**embedding):
        cfg = {"embedding": {"model": MODEL, **embedding}}
        cfg_path.write_text(json.dumps(cfg))

    def install(spy):
        for name, mod in spy.modules().items():
            monkeypatch.setitem(sys.modules, name, mod)
        return spy

    e.set_config = set_config
    e.install = install
    e.write_raw = lambda d: cfg_path.write_text(json.dumps(d))
    return e


def _stderr_lines(capsys, needle):
    err = capsys.readouterr().err
    return [ln for ln in err.splitlines() if needle in ln]


# ---------------------------------------------------------------------------
# C1 default is cpu, at every site, CUDA never touched
# ---------------------------------------------------------------------------


def test_c1_default_cpu_all_four_sites_no_cuda(env):
    spy = env.install(Spy(gpu=True, forbid_cuda=True))
    env.write_raw({"embedding": {"model": MODEL}})  # no embedding.device
    core = env.core
    mgr = core.EmbeddingManager()
    spy.st_fail_local_only = True
    mgr._load_model(MODEL)            # offline-first fails -> online fallback
    spy.st_fail_local_only = False
    mgr.revive(MODEL)                 # local-only reload
    nli = core._NLIClassifier()
    assert nli._ensure_loaded("p-model") is True
    # offline attempt, online fallback, revive reload
    assert spy.st_devices == ["cpu", "cpu", "cpu"]
    assert spy.ce_calls == [("p-model", "cpu")]
    assert spy.cuda_calls == []


def test_c1_no_embedding_section_at_all(env):
    spy = env.install(Spy(gpu=True, forbid_cuda=True))
    env.write_raw({})
    mgr = env.core.EmbeddingManager()
    mgr._load_model(MODEL)
    assert spy.st_devices == ["cpu"]
    assert spy.cuda_calls == []


# ---------------------------------------------------------------------------
# C2 real entry points (recall + filing) never touch CUDA on cpu
# ---------------------------------------------------------------------------


def test_c2_recall_and_filing_paths_never_touch_cuda(env):
    spy = env.install(Spy(gpu=True, forbid_cuda=True))
    env.write_raw({
        "embedding": {"model": MODEL, "device": "cpu"},
        "polarity": {"enabled": True, "model": "p-model",
                     "threshold": 0.46, "min_similarity_for_check": 0.30},
    })
    core = env.core
    import engram_observation as obs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT, type TEXT, claim TEXT, source_title TEXT,"
        " predicted_event TEXT, recall_summary TEXT, recall_keywords TEXT)"
    )
    conn.execute("INSERT INTO nodes VALUES ('n1','observation_factual','a claim',"
                 "NULL,NULL,NULL,NULL)")

    # recall entry point (embeds the query; later DB errors are swallowed)
    core._semantic_search(conn, "some query")
    # filing entry points: store-embedding for the new node + polarity scoring
    core._compute_and_store_embedding(conn, "n1")
    obs._compute_polarity_alerts(
        "new claim", [{"id": "n1", "claim": "old claim", "similarity": 0.9}]
    )
    # the NLI model has no real forward pass in the fake; the load is what counts
    assert spy.st_calls, "the embedder must have been loaded by the entry points"
    assert spy.ce_calls == [("p-model", "cpu")]
    assert all(d == "cpu" for d in spy.st_devices)
    assert spy.cuda_calls == []


# ---------------------------------------------------------------------------
# C3 cuda
# ---------------------------------------------------------------------------


def test_c3_cuda_with_gpu_gets_cuda_and_no_thread_cap(env):
    spy = env.install(Spy(gpu=True))
    env.set_config(device="cuda")
    core = env.core
    core.EmbeddingManager()._load_model(MODEL)
    assert core._NLIClassifier()._ensure_loaded("p-model")
    assert spy.st_devices == ["cuda"]
    assert spy.ce_calls == [("p-model", "cuda")]
    assert spy.thread_calls == []


def test_c3_cuda_without_gpu_falls_back_to_cpu_with_one_notice(env, capsys):
    spy = env.install(Spy(gpu=False))
    env.set_config(device="cuda", cpu_threads=3)
    mgr = env.core.EmbeddingManager()
    mgr._load_model(MODEL)
    assert mgr._model is not None, "embeddings must not turn off"
    assert spy.st_devices == ["cpu"]
    assert spy.thread_calls == [3]
    lines = _stderr_lines(capsys, "embedding.device is cuda")
    assert lines == [
        "[engram] embedding.device is cuda but no CUDA device is visible; using cpu"
    ] or (len(lines) == 1 and
          "embedding.device is cuda but no CUDA device is visible; using cpu" in lines[0])


def test_c3_cuda_without_gpu_polarity_falls_back_with_notice(env, capsys):
    spy = env.install(Spy(gpu=False))
    env.set_config(device="cuda", cpu_threads=3)
    nli = env.core._NLIClassifier()
    assert nli._ensure_loaded("p-model") is True
    assert spy.ce_calls == [("p-model", "cpu")]
    assert spy.thread_calls == [3]
    assert len(_stderr_lines(capsys, "embedding.device is cuda")) == 1


# ---------------------------------------------------------------------------
# C4 auto = today's behaviour; thread cap on every CPU path
# ---------------------------------------------------------------------------


def test_c4_auto_passes_no_device_kwarg_and_ce_by_availability(env):
    spy = env.install(Spy(gpu=True))
    env.set_config(device="auto")
    core = env.core
    core.EmbeddingManager()._load_model(MODEL)
    core._NLIClassifier()._ensure_loaded("p-model")
    assert spy.st_devices == ["<unset>"]
    assert spy.ce_calls == [("p-model", "cuda")]
    assert spy.thread_calls == []  # landed on GPU: no cap


def test_c4_auto_no_gpu_applies_thread_cap_fake_torch(env):
    spy = env.install(Spy(gpu=False))
    env.set_config(device="auto", cpu_threads=2)
    core = env.core
    core.EmbeddingManager()._load_model(MODEL)
    assert spy.st_devices == ["<unset>"]
    assert spy.threads_now == 2 and spy.thread_calls == [2]
    spy.thread_calls.clear()
    core._NLIClassifier()._ensure_loaded("p-model")
    assert spy.ce_calls == [("p-model", "cpu")]
    assert spy.thread_calls == [2]


def test_c4_auto_no_gpu_real_torch_get_num_threads_equals_cpu_threads(env, monkeypatch):
    """Borges's wording: torch.get_num_threads() after the model loads must
    equal cpu_threads. Real torch, fake sentence_transformers (no download).
    Non-vacuous on a CUDA build with CUDA_VISIBLE_DEVICES= ; on a CPU-only
    build it still proves the real set_num_threads call happens."""
    torch = pytest.importorskip("torch")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    saved = torch.get_num_threads()
    try:
        spy = Spy(gpu=False)
        # only the sentence_transformers fake; keep REAL torch
        monkeypatch.setitem(sys.modules, "sentence_transformers",
                            spy.modules()["sentence_transformers"])
        target = 1 if saved != 1 else 2
        env.set_config(device="auto", cpu_threads=target)
        env.core.EmbeddingManager()._load_model(MODEL)
        assert torch.get_num_threads() == target
    finally:
        torch.set_num_threads(saved)


# ---------------------------------------------------------------------------
# C5 invalid values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["gpu", "CPU", "", 3, None, ["cpu"]])
def test_c5_invalid_device_loads_on_cpu_with_one_notice(env, capsys, bad):
    spy = env.install(Spy(gpu=True, forbid_cuda=True))
    env.set_config(device=bad)
    core = env.core
    mgr = core.EmbeddingManager()
    mgr._load_model(MODEL)
    nli = core._NLIClassifier()
    assert nli._ensure_loaded("p-model") is True
    assert mgr._model is not None, "embeddings must not be disabled"
    assert spy.st_devices == ["cpu"]
    assert spy.ce_calls == [("p-model", "cpu")]
    assert spy.cuda_calls == []
    lines = _stderr_lines(capsys, "embedding.device")
    # one line per load that read the setting (embedder + polarity); each names
    # the bad value and the allowed set
    assert len(lines) == 2
    for ln in lines:
        assert repr(bad) in ln
        for a in ALLOWED:
            assert a in ln


def test_c5_invalid_device_one_line_for_one_load(env, capsys):
    env.install(Spy(gpu=True, forbid_cuda=True))
    env.set_config(device="gpu")
    env.core.EmbeddingManager()._load_model(MODEL)
    assert len(_stderr_lines(capsys, "embedding.device")) == 1


def test_c5_schema_validator_reports_invalid_device(env):
    from viz.data import config as vc

    ok, err = vc._validate_config_value("embedding.device", "cpu")
    assert err is None and ok == "cpu"
    for good in ALLOWED:
        assert vc._validate_config_value("embedding.device", good)[1] is None
    for bad in ("gpu", "CPU", ""):
        assert vc._validate_config_value("embedding.device", bad)[1] is not None
    assert vc._validate_config_value("embedding.device", 3)[1] is not None
    assert vc._validate_config_value("embedding.cpu_threads", 0)[1] is not None
    assert vc._validate_config_value("embedding.cpu_threads", 4)[1] is None


@pytest.mark.parametrize("bad", [0, -1, "x", 2.5, True])
def test_c5_invalid_cpu_threads_default_four_one_notice(env, capsys, bad):
    spy = env.install(Spy(gpu=False))
    env.set_config(device="cpu", cpu_threads=bad)
    env.core.EmbeddingManager()._load_model(MODEL)
    assert spy.thread_calls == [4]
    lines = _stderr_lines(capsys, "embedding.cpu_threads")
    assert len(lines) == 1
    assert repr(bad) in lines[0]


def test_c5_valid_cpu_threads_used_and_no_notice(env, capsys):
    spy = env.install(Spy(gpu=False))
    env.set_config(device="cpu", cpu_threads=7)
    env.core.EmbeddingManager()._load_model(MODEL)
    assert spy.thread_calls == [7]
    assert isinstance(spy.thread_calls[0], int)
    assert _stderr_lines(capsys, "embedding.cpu_threads") == []


def test_cpu_threads_default_is_four_when_absent(env):
    spy = env.install(Spy(gpu=False))
    env.set_config(device="cpu")
    env.core.EmbeddingManager()._load_model(MODEL)
    assert spy.thread_calls == [4]


# ---------------------------------------------------------------------------
# C6 read at LOAD time
# ---------------------------------------------------------------------------


def test_c6_config_edit_between_loads_takes_effect(env):
    spy = env.install(Spy(gpu=True))
    mgr = env.core.EmbeddingManager()
    env.set_config(device="cpu")
    mgr._load_model(MODEL)
    env.set_config(device="cuda")
    mgr._load_model("other-model")  # model name changes -> second load
    assert spy.st_devices == ["cpu", "cuda"]


def test_c6_polarity_reads_config_per_load(env):
    spy = env.install(Spy(gpu=True))
    nli = env.core._NLIClassifier()
    env.set_config(device="cpu")
    nli._ensure_loaded("p1")
    env.set_config(device="cuda")
    nli._ensure_loaded("p2")
    assert spy.ce_calls == [("p1", "cpu"), ("p2", "cuda")]


def test_c6_thread_cap_reads_config_per_load(env):
    spy = env.install(Spy(gpu=False))
    mgr = env.core.EmbeddingManager()
    env.set_config(device="cpu", cpu_threads=2)
    mgr._load_model(MODEL)
    env.set_config(device="cpu", cpu_threads=3)
    mgr._load_model("other-model")
    assert spy.thread_calls == [2, 3]


# ---------------------------------------------------------------------------
# C7 each site individually (non-default setting so ignoring it is visible)
# ---------------------------------------------------------------------------


def test_c7_site_offline_first_load(env):
    spy = env.install(Spy(gpu=True))
    env.set_config(device="cuda")
    env.core.EmbeddingManager()._load_model(MODEL)
    assert spy.st_calls[0][1].get("local_files_only") is True
    assert spy.st_calls[0][1].get("device") == "cuda"


def test_c7_site_online_fallback_load(env):
    spy = env.install(Spy(gpu=True))
    spy.st_fail_local_only = True
    env.set_config(device="cuda")
    env.core.EmbeddingManager()._load_model(MODEL)
    assert len(spy.st_calls) == 2
    assert "local_files_only" not in spy.st_calls[1][1]
    assert spy.st_calls[1][1].get("device") == "cuda"


def test_c7_site_online_fallback_cpu_default(env):
    spy = env.install(Spy(gpu=True, forbid_cuda=True))
    spy.st_fail_local_only = True
    env.set_config()
    env.core.EmbeddingManager()._load_model(MODEL)
    assert spy.st_calls[1][1].get("device") == "cpu"
    assert spy.cuda_calls == []


def test_c7_site_revive_reload(env):
    spy = env.install(Spy(gpu=True))
    env.set_config(device="cuda")
    mgr = env.core.EmbeddingManager()
    mgr._load_model(MODEL)
    spy.st_calls.clear()
    assert mgr.revive(MODEL) is True
    assert len(spy.st_calls) == 1
    assert spy.st_calls[0][1].get("local_files_only") is True
    assert spy.st_calls[0][1].get("device") == "cuda"


def test_c7_site_revive_reload_cpu_thread_cap(env):
    spy = env.install(Spy(gpu=False))
    env.set_config(device="auto", cpu_threads=5)
    mgr = env.core.EmbeddingManager()
    mgr._load_model(MODEL)
    spy.thread_calls.clear()
    mgr.revive(MODEL)
    assert spy.thread_calls == [5]


def test_c7_site_polarity_cross_encoder(env):
    spy = env.install(Spy(gpu=True))
    env.set_config(device="cpu")
    env.core._NLIClassifier()._ensure_loaded("p-model")
    assert spy.ce_calls == [("p-model", "cpu")]


# ---------------------------------------------------------------------------
# C8 parity (GPU-gated; runs on the GPU host)
# ---------------------------------------------------------------------------


def _gpu_and_cached():
    try:
        import torch
        if not torch.cuda.is_available():
            return False
        from sentence_transformers import SentenceTransformer
        SentenceTransformer(MODEL, local_files_only=True, device="cpu")
        return True
    except Exception:
        return False


def test_c8_cpu_cuda_vector_parity():
    # Lazy skip: the check runs in the test body, never at collection time.
    if not _gpu_and_cached():
        pytest.skip("needs a CUDA device and the cached embedder model; runs on the GPU host")
    from sentence_transformers import SentenceTransformer
    import numpy as np

    sents = [f"fixed parity sentence number {i} about topic {i % 5}" for i in range(20)]
    a = SentenceTransformer(MODEL, local_files_only=True, device="cpu").encode(sents)
    b = SentenceTransformer(MODEL, local_files_only=True, device="cuda").encode(sents)
    for x, y in zip(a, b):
        cos = float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y)))
        assert cos >= 0.999999


# ---------------------------------------------------------------------------
# C9 registry + docs
# ---------------------------------------------------------------------------


def _row(key):
    from tools.config_schema import SCHEMA
    return {r["key"]: r for r in SCHEMA}[key]


def test_c9_schema_rows():
    dev = _row("embedding.device")
    assert dev["type"] == "string"
    assert dev["default"] == "cpu"
    assert list(dev["allowed_values"]) == ["cpu", "cuda", "auto"]
    assert dev["section"] == "embedding"
    thr = _row("embedding.cpu_threads")
    assert thr["type"] == "integer"
    assert thr["default"] == 4
    assert thr["min"] == 1
    assert thr["section"] == "embedding"
    # same tier convention as embedding.model
    assert dev["tier"] == thr["tier"] == _row("embedding.model")["tier"]


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("rel", [
    "README-AGENT.md",
    "src/engram/templates/config.json.example",
])
def test_c9_docs_list_both_keys(rel):
    text = open(os.path.join(REPO, rel), encoding="utf-8").read()
    assert "device" in text and "cpu_threads" in text
    if rel.endswith(".example"):
        emb = json.loads(text)["embedding"]
        assert emb["device"] == "cpu" and emb["cpu_threads"] == 4
    else:
        assert "embedding.device" in text and "embedding.cpu_threads" in text


# ---------------------------------------------------------------------------
# Review round 1 additions
# ---------------------------------------------------------------------------


def test_spy_records_version_backends_and_unknown_cuda_members():
    """Anti-vacuity for the spy itself: every route a wrong implementation might
    use to probe CUDA is recorded, not just the four named entry points."""
    spy = Spy()
    torch = spy.modules()["torch"]
    torch.version.cuda
    torch.backends.cuda
    torch.cuda.current_device
    torch.cuda.get_device_name
    torch.cuda.mem_get_info
    getattr(torch.cuda, "never_heard_of_it", None)
    assert spy.cuda_calls == [
        "torch.version", "torch.version.cuda",
        "torch.backends", "torch.backends.cuda",
        "torch.cuda", "torch.cuda.current_device",
        "torch.cuda", "torch.cuda.get_device_name",
        "torch.cuda", "torch.cuda.mem_get_info",
        "torch.cuda", "torch.cuda.never_heard_of_it",
    ]
    # the thread functions are NOT CUDA probes
    spy2 = Spy()
    t2 = spy2.modules()["torch"]
    t2.set_num_threads(2)
    t2.get_num_threads()
    assert spy2.cuda_calls == []


def test_cpu_path_records_no_cuda_access_at_all_four_sites(env):
    spy = env.install(Spy(gpu=True))
    env.set_config(device="cpu")
    core = env.core
    spy.st_fail_local_only = True
    mgr = core.EmbeddingManager()
    mgr._load_model(MODEL)
    spy.st_fail_local_only = False
    mgr.revive(MODEL)
    core._NLIClassifier()._ensure_loaded("p-model")
    assert len(spy.st_calls) == 3 and len(spy.ce_calls) == 1
    assert spy.cuda_calls == []


# D4: the cap is decided from the LOADED model's device


def test_d4_model_without_device_is_not_capped_and_does_not_raise(env):
    spy = env.install(Spy(gpu=False))
    env.core._cap_cpu_threads(types.SimpleNamespace(), 3)
    assert spy.thread_calls == []


def test_d4_loader_with_device_less_model_does_not_cap(env):
    spy = env.install(Spy(gpu=False))
    spy.no_device = True
    env.set_config(device="cpu", cpu_threads=3)
    mgr = env.core.EmbeddingManager()
    mgr._load_model(MODEL)
    assert mgr._model is not None
    assert spy.thread_calls == []


def test_d4_only_model_dot_device_cpu_is_capped(env):
    spy = env.install(Spy(gpu=False))
    m = types.SimpleNamespace(model=types.SimpleNamespace(device=_Dev("cpu")))
    env.core._cap_cpu_threads(m, 3)
    assert spy.thread_calls == [3]


def test_d4_only_model_dot_device_cuda_is_not_capped(env):
    spy = env.install(Spy(gpu=True))
    m = types.SimpleNamespace(model=types.SimpleNamespace(device=_Dev("cuda:0")))
    env.core._cap_cpu_threads(m, 3)
    assert spy.thread_calls == []


# torch import failure has its own notice


def test_cuda_with_torch_import_failure_says_so_once(env, capsys, monkeypatch):
    spy = env.install(Spy(gpu=False))
    monkeypatch.setitem(sys.modules, "torch", None)  # `import torch` -> ImportError
    env.set_config(device="cuda")
    mgr = env.core.EmbeddingManager()
    mgr._load_model(MODEL)
    assert mgr._model is not None
    assert spy.st_devices == ["cpu"]
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if "embedding.device" in ln]
    assert lines == [
        "[engram] embedding.device is cuda but torch could not be imported; using cpu"
    ]
    assert "no CUDA device" not in err


# The viz config WRITER's coercion, as documentation of CURRENT behaviour


def test_writer_validation_documents_current_coercion():
    """Documentation, not a design goal. The viz writer (`_validate_config_value`)
    shares the schema's generic integer coercion, so it is MORE LENIENT than the
    runtime path: it stores '4' as 4, 2.5 as 2 and True as 1, while the runtime
    reader (see test_c5_invalid_cpu_threads_default_four_one_notice) rejects
    those same values to the default 4. The runtime path is the stricter one."""
    from viz.data import config as vc

    v = vc._validate_config_value
    # cpu_threads: coerced
    assert v("embedding.cpu_threads", "4") == (4, None)
    assert v("embedding.cpu_threads", 2.5) == (2, None)
    assert v("embedding.cpu_threads", True) == (1, None)
    # cpu_threads: rejected
    for bad in (0, -1, None):
        assert v("embedding.cpu_threads", bad)[1] is not None
    # device: rejected
    for bad in ("CPU", "gpu", "", 3, None, "cuda:0"):
        assert v("embedding.device", bad)[1] is not None
    # device: accepted
    for good in ("cpu", "cuda", "auto"):
        assert v("embedding.device", good) == (good, None)


# ---------------------------------------------------------------------------
# Signature-faithful fakes: every load site must call the REAL constructor shape
# ---------------------------------------------------------------------------
# Older tests fake SentenceTransformer with `def ctor(model_name)`, which a new
# keyword breaks with a TypeError that the loaders' generic `except` swallows.
# The converse hazard is a keyword the REAL library would reject. These fakes
# bind every call against the library's constructor signature (from the
# installed class when importable, and always from a hand-written copy), so a
# site that passes an unknown keyword is caught without a GPU or a download.


def _faithful_st(model_name_or_path=None, modules=None, device=None, prompts=None,
                 default_prompt_name=None, cache_folder=None, trust_remote_code=False,
                 revision=None, local_files_only=False, token=None, use_auth_token=None,
                 truncate_dim=None, model_kwargs=None, tokenizer_kwargs=None,
                 config_kwargs=None, model_card_data=None, backend="torch"):
    pass


def _faithful_ce(model_name_or_path, num_labels=None, max_length=None, activation_fn=None,
                 device=None, cache_folder=None, trust_remote_code=False, revision=None,
                 local_files_only=False, token=None, model_kwargs=None, tokenizer_kwargs=None,
                 config_kwargs=None, model_card_data=None, backend="torch"):
    pass


def _real_init_signature(attr):
    try:
        import sentence_transformers
        sig = inspect.signature(getattr(sentence_transformers, attr).__init__)
    except Exception:
        return None
    return sig.replace(parameters=list(sig.parameters.values())[1:])  # drop self


def _strict_signature_sources():
    out = [("hand-written", {"st": inspect.signature(_faithful_st),
                             "ce": inspect.signature(_faithful_ce)})]
    st, ce = _real_init_signature("SentenceTransformer"), _real_init_signature("CrossEncoder")
    if st is not None and ce is not None:
        out.append(("installed-library", {"st": st, "ce": ce}))
    return out


@pytest.mark.parametrize("device", ["cpu", "cuda", "auto"])
@pytest.mark.parametrize("source", [n for n, _ in _strict_signature_sources()])
def test_all_four_sites_call_the_real_constructor_shape(env, source, device):
    sigs = dict(_strict_signature_sources())[source]
    spy = env.install(Spy(gpu=True))
    spy.strict = sigs
    env.set_config(device=device)
    core = env.core
    spy.st_fail_local_only = True
    mgr = core.EmbeddingManager()
    mgr._load_model(MODEL)              # site 1 (offline attempt) + site 2 (online fallback)
    spy.st_fail_local_only = False
    mgr.revive(MODEL)                   # site 3 (local-cache-only reload)
    ok = core._NLIClassifier()._ensure_loaded("p-model")  # site 4 (CrossEncoder)
    assert spy.rejected == [], spy.rejected
    assert ok is True and mgr._model is not None
    assert len(spy.st_calls) == 3 and len(spy.ce_calls) == 1


def test_strict_signature_fake_actually_rejects_an_unknown_keyword():
    """Anti-vacuity: the strict signatures do not accept everything."""
    for _name, sigs in _strict_signature_sources():
        with pytest.raises(TypeError):
            sigs["st"].bind("m", device="cpu", not_a_real_kwarg=1)
        with pytest.raises(TypeError):
            sigs["ce"].bind("m", device="cpu", not_a_real_kwarg=1)
        sigs["st"].bind("m", device="cpu", local_files_only=True)
        sigs["ce"].bind("m", device="cpu")
