# FAISS Index Caching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the Modal RVC worker from re-reading and fully re-materializing a 221MB FAISS index from disk on every single ~480ms streaming audio block, which is the dominant cause of ~3s-per-block conversion latency (confirmed via production `[Timing]` logs) and the "part by part" broken-up call audio it produces.

**Architecture:** `RVC/infer/modules/vc/pipeline.py:310-312` (vendored, third-party) calls `faiss.read_index(file_index)` then `index.reconstruct_n(0, index.ntotal)` unconditionally on every `vc_single()`/`pipeline()` call — there is no caching layer above it. Rather than editing vendored RVC code, `modal_deploy/worker.py` monkeypatches `faiss.read_index` at container-startup time with a memoizing wrapper: first call loads + reconstructs as before and caches the result (keyed by file path); every later call — including all real per-block conversions during a live call — returns the already-loaded index instantly. The existing GPU warm-up pass in `RVCEngine.startup()` naturally pre-warms this cache before any real caller connects, so the one-time cost is paid at container boot, not mid-call.

**Tech Stack:** Python 3.10, `faiss-cpu==1.7.3`, `functools.lru_cache`, pytest (no GPU/torch required for the new test — it only exercises the caching wrapper against a real small FAISS index).

## Global Constraints

- Do not modify any file under `RVC/` (vendored third-party code) — the fix lives entirely in `modal_deploy/worker.py` plus one new test file.
- The cache must return the exact same in-memory `faiss.Index` object (and precomputed `big_npy` array) on every call for a given path — RVC's `pipeline()` always calls `index.reconstruct_n(0, index.ntotal)` (the whole index, no partial args), so a full-array cache is always correct for this codebase's actual call pattern.
- `faiss-cpu` is already a pinned dependency in `modal_deploy/requirements.txt:17` — no dependency changes needed.

---

### Task 1: Cached `faiss.read_index` wrapper with a standalone test

**Files:**
- Modify: `modal_deploy/worker.py` (add near the top, after the existing `streaming` import fallback block at line 15, before `app = modal.App("rvc-worker")` at line 18)
- Test: `modal_deploy/test_faiss_index_cache.py` (new file — pure `faiss`/`numpy`, no torch/GPU/RVC import needed, runnable in any env with `faiss-cpu` installed)

**Interfaces:**
- Produces: `_cached_read_index(path: str) -> faiss.Index` — a drop-in replacement assigned to `faiss.read_index`. Calling `index.reconstruct_n(0, index.ntotal)` on the object it returns is O(1) after the first call for that path (returns a precomputed cached array, ignores its arguments).

- [ ] **Step 1: Write the failing test**

Create `modal_deploy/test_faiss_index_cache.py`:

```python
import numpy as np
import faiss
import pytest

from worker import _cached_read_index, _install_faiss_index_cache


@pytest.fixture
def small_index_file(tmp_path):
    """A real, tiny FAISS IVF-Flat index on disk, shaped like the
    production added_*.index files (IVF + Flat quantizer)."""
    dim = 8
    vectors = np.random.RandomState(0).rand(50, dim).astype("float32")
    quantizer = faiss.IndexFlatL2(dim)
    index = faiss.IndexIVFFlat(quantizer, dim, 4)
    index.train(vectors)
    index.add(vectors)
    path = tmp_path / "test.index"
    faiss.write_index(index, str(path))
    return str(path), vectors


def test_cached_read_index_returns_same_object_on_repeat_calls(small_index_file):
    path, _ = small_index_file
    _install_faiss_index_cache()

    first = faiss.read_index(path)
    second = faiss.read_index(path)

    assert first is second


def test_cached_read_index_reconstruct_n_matches_real_reconstruction(small_index_file):
    path, vectors = small_index_file
    _install_faiss_index_cache()

    index = faiss.read_index(path)
    reconstructed = index.reconstruct_n(0, index.ntotal)

    assert reconstructed.shape == vectors.shape
    # reconstruct_n on an IVF-Flat index returns vectors in the order they
    # were added, matching the original training set exactly.
    np.testing.assert_allclose(reconstructed, vectors, rtol=1e-5)


def test_cached_reconstruct_n_ignores_disk_after_first_call(small_index_file, monkeypatch):
    path, _ = small_index_file
    _install_faiss_index_cache()

    faiss.read_index(path)  # primes the cache
    call_count = {"n": 0}
    real_open = open

    def counting_open(*args, **kwargs):
        if str(args[0]) == path:
            call_count["n"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    faiss.read_index(path)  # should be served from cache, no disk access

    assert call_count["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd modal_deploy && python -m pytest test_faiss_index_cache.py -v`
Expected: FAIL with `ImportError: cannot import name '_cached_read_index' from 'worker'` (the functions don't exist yet).

- [ ] **Step 3: Write minimal implementation**

In `modal_deploy/worker.py`, insert after line 15 (the `except ImportError: import streaming as st` fallback) and before line 18 (`app = modal.App("rvc-worker")`):

```python
import faiss
from functools import lru_cache

# RVC/infer/modules/vc/pipeline.py calls faiss.read_index(file_index) then
# index.reconstruct_n(0, index.ntotal) on EVERY vc_single() call — fine for
# the original one-shot-per-file WebUI use case, but this app calls it once
# per ~480ms streaming audio block, re-reading and re-materializing a 221MB
# index from disk every time (confirmed ~1.4-2.0s of the ~3s per-block
# latency in production [Timing] logs). Cache the loaded index and its full
# reconstruction so only the first call (the GPU warm-up pass in
# RVCEngine.startup(), before any real caller connects) pays that cost.
def _install_faiss_index_cache():
    if getattr(faiss.read_index, "_kiera_cached", False):
        return  # already installed (e.g. re-imported in a test)
    faiss.read_index = _cached_read_index


@lru_cache(maxsize=4)
def _cached_read_index(path: str) -> "faiss.Index":
    index = _real_faiss_read_index(path)
    cached_npy = index.reconstruct_n(0, index.ntotal)
    index.reconstruct_n = lambda *args, **kwargs: cached_npy
    return index


_real_faiss_read_index = faiss.read_index
_cached_read_index._kiera_cached = True
_install_faiss_index_cache()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd modal_deploy && python -m pytest test_faiss_index_cache.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add modal_deploy/worker.py modal_deploy/test_faiss_index_cache.py
git commit -m "Cache FAISS index load/reconstruct in the RVC worker (was re-reading 221MB from disk per streaming block)"
```

---

### Task 2: Deploy and verify against real per-block timing logs

**Files:**
- None (deployment + log verification only — Task 1 already contains all code changes)

**Interfaces:**
- Consumes: the deployed `rvc-worker` Modal app (`modal_deploy/worker.py` from Task 1) and its existing `[Timing]` print statements in `RVCEngine.run_conversion` (`modal_deploy/worker.py`, unchanged).

- [ ] **Step 1: Deploy the updated worker**

Run: `modal deploy modal_deploy/worker.py`
Expected: deploy succeeds; note the container(s) it reports so you can distinguish pre- and post-deploy containers in logs.

- [ ] **Step 2: Place one test call through the normal flow**

Use the existing outbound/inbound call flow (whatever you used for the 2026-07-03 16:36 test call referenced in this debugging session) so the `/ws` streaming path — not the offline `/convert` path — exercises the cache under real conditions.

- [ ] **Step 3: Pull timing logs and confirm the "npy" cost collapsed after the first block**

Run:
```bash
python -m modal app logs rvc-worker --since 15m --search "Timing"
```
Expected: the **first** `vc_single`/`npy:` line for the new container may still show ~1.4-2.0s (this is the warm-up pass in `startup()`, which primes the cache before the call connects). All **subsequent** lines from the same container during the live call should show a sharply lower `npy:` value (well under the ~1.4-2.0s baseline) and a correspondingly lower `total run_conversion` time, closer to the ~320ms real-time budget the streaming pipeline needs.

- [ ] **Step 4: Confirm the call itself sounds continuous**

Listen to the test call recording (or do it live). Expected: no more "part by part" stuttering — audio should stream continuously rather than arriving in multi-second bursts.

- [ ] **Step 5: If `npy` time is still high after the first block, stop and report — do not add a second caching layer speculatively**

If Step 3's expected drop doesn't happen, the redundant disk read/reconstruct wasn't the (sole) bottleneck — the remaining time is inherent to HuBERT feature extraction or the actual `index.search(npy, k=8)` FAISS query (both also counted inside the same `times[0]`/"npy" bucket, per `RVC/infer/modules/vc/pipeline.py:217-245`). That would need new profiling (e.g., temporarily timing `model.extract_features` and `index.search` separately) rather than another blind fix — per this project's debugging discipline, don't stack a second speculative change on top of an unconfirmed first one.

---

## Self-Review Notes

- **Spec coverage:** Task 1 implements and unit-tests the caching wrapper; Task 2 verifies it actually fixes the real symptom (choppy audio) against production evidence, and explicitly defines what "didn't work" looks like and what to do next (stop and report, not guess further) — matching this session's systematic-debugging approach.
- **No placeholders:** every step has runnable code/commands with concrete expected output.
- **Type/name consistency:** `_cached_read_index`, `_install_faiss_index_cache`, and `_real_faiss_read_index` are the only new names, used identically in the implementation (Task 1 Step 3) and the test (Task 1 Step 1).
