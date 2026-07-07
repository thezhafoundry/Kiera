---
title: TensorRT migration (RMVPE re-enable + phased latency plan)
type: issue
status: open
sources: [decisions-log, subsystem-notes, active-backlog]
updated: 2026-07-06
---

**Status as of 2026-07-06: three review rounds complete; implementation is now credible
and lives entirely in the UNCOMMITTED working tree** (all 2026-07-05 commits were
reverted). Remaining before rollout: 9 open review findings (hardening, not design), the
Task 9 GPU verification chain (probe → exports → engine build with its ≤400 ms gate →
offline A/B listen test), and the user-run deploy.

## Review arc (2026-07-05 → 2026-07-06)

- **Round 1** (landed-then-reverted commits): whole migration in one commit, Phase 1
  buffer shipped ahead of its gate, and a commit re-enabling the pitch auto-detection
  that was reverted as unreliable on 2026-07-03 — followed by two "Reverted" commits.
- **Round 2**: found the TRT path dead on arrival for three independent reasons — the
  HuBERT export was broken on both branches, `fastapi_app` deployed on an image without
  TensorRT libs, and `BLOCK_MS` had been cut to 500 while the static engine shapes still
  assumed 1400 ms blocks (zero-filling half of every block). Plus a 2.0 s playout cap
  smaller than the 5 s reconnect flush (guaranteed speech loss on WS reconnect).
- **Round 3**: all round-2 blockers fixed — block geometry restored with a cross-reference
  comment, new `modal_defs.py` single-source image module (correct build-layer ordering,
  pinned ORT 1.19.0, complete CUDA library paths), HuBERT export repaired with parity gate,
  hard provider verification, fatal latency gate. All local tests pass. Two genuine
  TensorRT discoveries surfaced: **ONNX random ops don't compile under TRT** (hence
  deterministic shims edited into the *vendored* `models_onnx.py` — see the commit trap in
  [[active-backlog]]) and a **Myelin FP16 compiler bug** forcing the generator engine to
  FP32 while HuBERT/RMVPE stay FP16.

The full spec is
[implementation_plan.md](../../../implementation_plan.md) at the repo root. Unusually for
this project, the implementation is being done by a *different* AI model; the resident
agent's role is reviewing the resulting diffs against the plan's gates (checklist in the
TensorRT row of [.agents/projects/active-backlog.md](../../../.agents/projects/active-backlog.md)).

## What and why

Migrate the Modal worker's inference ([modal_deploy/worker.py](../../../modal_deploy/worker.py))
from PyTorch to three static-shape TensorRT engines on the NVIDIA L4, executed via
onnxruntime-gpu's TensorRT provider:

1. **HuBERT** feature extractor (fairseq — the riskiest export; fallback is the
   community-standard ContentVec `vec-768-layer-12.onnx`, gated on a cosine ≥ 0.999 parity
   check either way).
2. **RVC generator** (`SynthesizerTrnMs768NSFsid`) — RVC's own `models_onnx.py` makes this
   the safest export.
3. **RMVPE pitch estimator** (E2E net only — the `torch.stft` mel frontend and cents
   decoding stay outside the engine).

FAISS index mixing cannot be a TRT engine and stays in NumPy between engine calls, keeping
the 2026-07-03 `faiss.read_index` cache monkeypatch relevant.

**The primary motivation is quality, not latency**: pitch tracking was downgraded from
RMVPE to Parselmouth (`pm`) purely to save ~300ms per block (see
[[part-by-part-audio-investigation]] context). TRT speed makes RMVPE affordable again.
This is the "inference-speed engineering" lever that the 2026-07-03 decision log
explicitly deferred — now activated.

## Key design findings from the planning analysis

- **The naive static shape (22,400 samples) was wrong.** The vendored RVC pipeline
  reflect-pads `t_pad = 16000 × x_pad` per side, and the streaming `BlockAccumulator`'s
  first blocks carry 0–6400 samples of context, not always 6400. The plan pins a fixed
  16,000-sample pad per side (54,400 samples into HuBERT) and zero-fills short blocks, so
  output is exactly 3× input — which also removes the proportional-slice hack in
  `trim_context`.
- **TRT engines are SM89- and TRT-version-specific** — they're built on the L4 by an
  explicit job and cached on the `rvc-models` volume, never at image-build time (image
  builds have no GPU; cold start is already ~75s).
- **TRT does not reduce the dominant latency term.** End-to-end latency floor is the
  1000ms block accumulation, not GPU time. Latency gains require shrinking `BLOCK_MS`,
  which is deliberately deferred (below).

## The playout-buffer decision (partial reversal of 2026-07-03)

The original proposal cut the standing playout buffer ([[adaptive-playout-buffer]]) target
from 3s straight to 0.25s. That conflicts with the 2026-07-03 "continuity over latency"
decision *and* is below the block-arrival granularity: converted audio arrives in ~1s
bursts, so a 0.25s cushion starves every cycle regardless of GPU speed. The user approved
a **phased** reversal on 2026-07-05:

- **Phase 1** (in this plan, last task, after live TRT verification): target 3.0s → 1.25s
  — the floor for `BLOCK_MS=1000` plus jitter headroom.
- **Phase 2** (separate future plan): 0.25s target + smaller `BLOCK_MS`, gated on live TRT
  p95 ≤ 400ms sustained over ~a week. Re-raises the SOLA seam rate, so it re-runs the full
  quality gate at the new block geometry (new canonical shapes → full re-export).

## Invariants preserved

Fail-closed/never-raw is untouched: TRT init failure falls back to the PyTorch *converted*
path only; inference failure still emits silence. Single-tenant `/ws`, `max_containers=1`,
and `region="ap-southeast"` are unchanged. All `modal deploy`/push steps in the plan are
marked [USER-RUN].

## Resolution criteria

Close this page when: TRT is live (`/health` reports `"engine": "trt"`, `"trt_cache": "hot"`),
a real call has confirmed `infer_ms` median ≤ 400ms with RMVPE audio quality at least
matching the `pm` baseline (offline A/B via `convert_file_chunked` first), and the Phase 1
buffer change has survived a live call without gaps.
