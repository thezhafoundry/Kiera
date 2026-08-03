---
title: Candidate B (400ms accumulation) exported, TRT-compiled, deployed live, then reverted same day
type: issue
status: open
sources: [decisions-log, active-backlog]
updated: 2026-08-02
---

`candidate_b` (`modal_deploy/rvc_profiles.py`, 160ms block / 240ms context / 40ms SOLA /
160ms playout — 400ms total accumulation vs. `baseline`'s 720ms) had existed as a defined
geometry since before this session, but per
[[rvc-baseline-routing-and-duration]]/`active-backlog.md`, its matching ONNX/TRT artifacts
and quality evidence did not — it was explicitly blocked pending Gate R1 completing first.

## What happened 2026-08-02

In pursuit of sub-1000ms latency, `candidate_b` was taken through the full pipeline in one
session, out of the originally planned order:

1. `RVC_STREAM_PROFILE=candidate_b modal run modal_deploy/export_onnx.py` — passed all
   three PyTorch-vs-ONNX parity checks at `cosine=1.000000` (hubert, generator, rmvpe) —
   essentially a perfect numerical match. Artifacts written to an isolated volume path
   (`/root/rvc-models/profiles/candidate_b/`), `baseline`'s own artifacts untouched by
   design (`rvc_profiles.py::_profile_artifact_root`).
2. `RVC_STREAM_PROFILE=candidate_b modal run modal_deploy/compile_trt.py::build_engines` —
   TRT engine build completed.
3. Offline quality check: `modal run modal_deploy/worker.py::main_chunked --pitch 12
   --use-trt 1 --adaptive 1` against `male_test.wav` — user reported "no issue" on
   listening to the output.
4. Deployed live: `RVC_STREAM_PROFILE=candidate_b modal deploy modal_deploy/worker.py`.
5. **Reverted back to `baseline` the same session**, per explicit user request — no
   live-call A/B or listen test was completed on `candidate_b` before the revert.

## What this leaves open

The "geometry exists, artifacts don't" gap from the earlier backlog entry is now closed —
artifacts exist, are TRT-compiled, and passed an offline quality check. **What's still
unconfirmed is live-call quality at 400ms accumulation** — the offline replay tests the
GPU/model layer in isolation (see the recurring "offline replay first" pattern across this
project's incident history, e.g. [[tensorrt-migration]]'s `rand_ini` fix), not real
network jitter, real speech patterns, or the input-side issues found later the same
session (see [[desktop-input-frame-drops]]).

Re-deployable without re-running export/compile:
`RVC_STREAM_PROFILE=candidate_b modal deploy modal_deploy/worker.py`.

Also unresolved: whether skipping the original "benchmark only after Gate R1" sequencing
matters. [[rvc-baseline-routing-and-duration]] is the tracking page for that gate; this
page doesn't attempt to resolve the ordering question, only records that it was skipped.
