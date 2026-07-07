---
title: .agents/context/stack-and-rules.md
type: source
sources: [../../../.agents/context/stack-and-rules.md]
updated: 2026-07-07
---

[.agents/context/stack-and-rules.md](../../../.agents/context/stack-and-rules.md) —
tech stack inventory, hard invariants, and file map.

## Key claims
- Stack: FastAPI backend on **Render** (service `Kiera`, `srv-d932m4cvikkc73belt1g`, now
  **Singapore** — colocated with Modal, verified live 2026-07-03); LiveKit Cloud (room + SIP)
  + Twilio (Elastic SIP Trunk + PSTN number); RVC v2 on a serverless **Modal L4** GPU worker
  (was T4 until 2026-07-03) pinned `region="ap-southeast"`, capped `max_containers=1`,
  with an optional **TensorRT** path (`USE_TRT=1`) using 3 static-shape engines
  (HuBERT/generator/RMVPE) built on-gpu and cached to the `rvc-models` volume.
  Noise suppression via WebRTC (degrade to passthrough); vanilla HTML/ES6 frontend; RVC v2
  model trained externally via the vendored `RVC/` WebUI, weights uploaded to a Modal volume,
  never committed.
- Hard invariants (current, post-2026-07-02-rebuild): one-way conversion only
  (agent→lead); **never publish raw/unconverted audio — fail CLOSED** (silence on outage,
  not a raw-audio fallback); fixed sample-rate contract (16kHz in, 48kHz RVC out,
  960-byte/10ms published frames); noise suppressor frame contract (exactly 320 bytes);
  the conversion generator must always be torn down via `contextlib.aclosing`; don't push
  to `main` mid-call (Render auto-redeploys); never commit `.env`/model files.
- File map updated 2026-07-07 with TRT-era `modal_deploy/` files: `modal_defs.py`,
  `trt_pipeline.py`, `export_onnx.py`, `compile_trt.py`, `test_trt_pipeline.py`.
- Region mismatch — **resolved 2026-07-03**, Render confirmed colocated with Modal in
  Singapore. See [[modal-render-region-mismatch]].
