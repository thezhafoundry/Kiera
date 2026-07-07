---
title: LATENCY.md
type: source
sources: [../../../LATENCY.md]
updated: 2026-07-07
---

[LATENCY.md](../../../LATENCY.md) — the canonical latency budget and troubleshooting doc
for the pipeline, last substantively updated 2026-07-07 (stale-data audit).

## Key claims
- Mouth-to-ear latency is now dominated by the **standing playout buffer** (1.25s target /
  5s cap, as of 2026-07-07 TRT phase 1; was ~3s from 2026-07-03 through the phase 1 merge).
  The 2026-07-02 rebuild's one-shot 100ms jitter fill (`_JITTER_TARGET_BYTES`) no longer
  exists in the code.
- GPU inference is now optionally served by 3 static-shape **TensorRT** engines
  (`USE_TRT=1`) with **RMVPE** pitch tracking (was `pm` before TRT). C3 benchmark on a live
  L4 (ap-southeast, 2026-07-06): median 66ms/p95 68ms vs. ≤400ms gate. Legacy non-TRT path
  is unbenchmarked.
- Modal worker moved from **T4 → L4** GPU (2026-07-03).
- Render↔Modal **region mismatch resolved** (2026-07-03): Render confirmed live in Singapore,
  colocated with the `ap-southeast` Modal pin.
- Raw-voice fallback is gone structurally — the fail-closed warm gate blocks the call (503
  outbound / hold-music inbound) rather than degrading to raw audio.
- Cold start measured live at ~75s (historical, T4 measurement); TRT adds a one-time ~22s
  engine build/warmup on a cold volume cache.
- §5 preserves the old VAD-chunked ordered-playout-queue design as historical record.
- No live spectral-tone measurement has been run against the current (buffer + TRT)
  configuration — LATENCY.md's §1 numbers are design estimates, not measured results.
