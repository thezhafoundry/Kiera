# TRT Migration — Finish-Line Runbook

Companion to [implementation_plan.md](implementation_plan.md). State as of 2026-07-06:
round-3 review passed the design; what remains is (A) ~15 lines of hardening fixes,
(B) committing three days of uncommitted work, (C) the GPU verification chain (plan
Task 9), (D) the keep-warm cost decision, and (E) the user-run rollout (plan Task 10).

Conventions, same as the plan:
- **[USER-RUN]** = the project owner runs it (or gives explicit go-ahead). Applies to
  `modal deploy` and any push to `main`.
- Every `modal run` spins the L4 briefly (billed) — announce before running.
- **Never push to `main` during a live test call** (Render auto-deploys and kills the
  in-flight bot).

---

## Phase A — Fix the open findings (~30 min, no GPU needed)

Finding numbers refer to the 2026-07-06 round-3 review (also summarized in
`.agents/projects/active-backlog.md`, TensorRT row).

### A1. Finding 1 — stop the crash-loop when fallback artifacts are missing

`modal_deploy/worker.py` `startup()` (~line 122): the base ONNX sessions load
unconditionally. Make them optional so a fresh volume with only TRT engines still boots:

```python
        # Load the base (non-TRT) ONNX sessions if the artifacts exist. They are the
        # FALLBACK path; their absence must not crash a container whose TRT engines
        # are fine. If neither path can load, startup fails loudly below.
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        vec_path = "/root/rvc-models/onnx/vec-768-layer-12.onnx"
        rvc_path = "/root/rvc-models/onnx/mi-test.onnx"

        if os.path.exists(vec_path) and os.path.exists(rvc_path):
            print("Loading ContentVec ONNX...")
            self.vec_session = ort.InferenceSession(vec_path, providers=providers)
            print("Loading RVC Generator ONNX...")
            self.rvc_session = ort.InferenceSession(rvc_path, providers=providers)
            print("ONNX Inference sessions initialized.")
        else:
            print(f"[Warning] Base ONNX artifacts missing ({vec_path}, {rvc_path}) — "
                  "fallback path unavailable; TRT path is required for this container.")
```

Then, at the END of `startup()` (after the TRT branch), enforce fail-closed — at least
one engine must exist or the worker must not report ready:

```python
        if self.trt_pipe is None and self.vec_session is None:
            raise RuntimeError(
                "No conversion engine available: TRT init failed/disabled AND base ONNX "
                "artifacts are missing. Refusing to start (fail-closed, never raw)."
            )
```

Note: `self.ready = True` currently sits BEFORE the TRT branch — move it to after this
final check so `ready` means "an engine actually works". The warm-up pass should also
move after session setup if it isn't already exercising the selected path.

### A2. Finding 2 — provider check on the fallback sessions

Immediately after the two `InferenceSession(...)` calls in A1, add the same guard
`trt_pipeline.py` already has:

```python
            for name, sess in [("vec", self.vec_session), ("rvc", self.rvc_session)]:
                active = sess.get_providers()
                if "CUDAExecutionProvider" not in active:
                    raise RuntimeError(
                        f"CUDAExecutionProvider not active for {name} session (got {active}) — "
                        "CPU inference cannot keep up with real time; failing startup instead "
                        "of serving broken audio."
                    )
```

### A3. Finding 5 — restore the FAISS fail-fast

`modal_deploy/worker.py` (~line 119): replace the silent skip with the historical loud
failure (this trap is documented in subsystem-notes — a missing index silently produces
the "doesn't sound like the trained voice" symptom):

```python
        if not index_files:
            raise RuntimeError("No FAISS index found in /root/rvc-models/logs/mi-test/ — "
                               "refusing to start without target-timbre index.")
        self.index_path = sorted(index_files)[-1]
        ...
```

### A4. Finding 8 — truthful engine label

`modal_deploy/worker.py:84` and the TRT-branch `except` (~line 191): rename
`"pytorch"` → `"onnx-cuda"` (the PyTorch path no longer exists), and fix the comment
that says "fall back to the PyTorch CONVERTED path". `/health` then reports
`"engine": "trt" | "onnx-cuda"`, which is what you actually verify at rollout.

### A5. Finding 9 — revert the smuggled tuning change

`backend/main.py:63`: `RVC_INDEX_RATE` default back to `"0.9"` (the comment above it
documents 0.9 as the deliberate investigation setting). If 0.8 is genuinely wanted,
that's its own one-line commit later, with a sentence of rationale — never bundled
inside a migration where it confounds the A/B.

### A6. Finding 7 — consolidate the duplicate exporter (optional but cheap)

Delete `export_model_to_onnx` from `worker.py` and fold its two jobs into
`modal_deploy/export_onnx.py`:

- Add an `export_fallback()` function there that (1) downloads
  `vec-768-layer-12.onnx` **pinned to a specific HuggingFace revision** (use the
  `/resolve/<commit-sha>/` URL form, not `/resolve/main/`), and (2) exports
  `mi-test.onnx` reusing the SAME loaded checkpoint/config code as `export_generator`
  (dynamic axes are fine for this one — it serves the variable-size fallback path).
- Add `export_fallback.local()` to `export_all`.
- Then remove the now-unneeded `onnxruntime-gpu` and `onnx` lines from
  `modal_deploy/requirements.txt` — `trt_image` pins ORT 1.19.0 via `modal_defs.py`,
  and nothing on the base image imports ORT anymore.

Findings 3, 4, and 6 are handled by Phases B, C, and D below respectively.

### A7. Verify Phase A

```bash
python -m pytest modal_deploy/test_trt_pipeline.py modal_deploy/test_streaming.py -q
python -m backend.test_pipeline
```
Expected: all pass (these fixes touch startup paths, not DSP).

---

## Phase B — Commit, in slices, on a branch

**Why a branch:** Render auto-deploys every push to `main`. The backend changes
(keep-warm loop, 1.25 s buffer) must NOT reach `main` until Phases C–E say so.
Committing locally on a branch makes the work durable without deploying anything.

```bash
git checkout -b trt-migration
```

Then six slices — each is one `git add` of whole files + one commit:

```bash
# Slice 1 — shared Modal definitions (standalone, nothing depends on it when committed)
git add modal_deploy/modal_defs.py
git commit -m "feat(trt): modal_defs.py — single source for volume/image/trt_image, pinned ORT 1.19.0, full CUDA lib paths"

# Slice 2 — vendored TRT-export shims (OWN commit; this is finding 3's fix)
git add RVC/infer/lib/infer_pack/models_onnx.py RVC/infer/lib/infer_pack/attentions_onnx.py
git commit -m "fix(vendored): TRT-export shims in models_onnx/attentions_onnx — replace torch.rand/randn_like (ONNX RandomNormal unsupported by TensorRT) and guard int-vs-tensor length; generator FP16 disabled separately due to TRT Myelin bug"

# Slice 3 — runtime pipeline module + its tests
git add modal_deploy/trt_pipeline.py modal_deploy/test_trt_pipeline.py
git commit -m "feat(trt): TRTVoicePipeline — 3-engine block conversion, provider verification, numpy DSP ports with local tests"

# Slice 4 — export + compile tooling
git add modal_deploy/export_onnx.py modal_deploy/compile_trt.py
git commit -m "feat(trt): ONNX exporters with parity gates + TRT engine-cache priming with fatal <=400ms gate"

# Slice 5 — worker integration
git add modal_deploy/worker.py modal_deploy/requirements.txt modal_deploy/streaming.py
git commit -m "feat(trt): worker on trt_image with USE_TRT, convert_block routing, hardened startup (fail-closed engine check, FAISS fail-fast, provider asserts)"

# Slice 6 — backend changes (buffer Phase 1 + client timeout + keep-warm per Phase D)
git add backend/pipeline.py backend/main.py
git commit -m "feat: playout buffer 1.25s/5s (TRT phase 1), 150s converter connect timeout"

# Docs slice — the already-staged .agents/ + wiki/ + plan/runbook files
git add .agents wiki implementation_plan.md TRT_ROLLOUT_STEPS.md
git commit -m "docs: TRT migration plan, review-round records, runbook"
```

Push the branch (safe — Render only tracks `main`):

```bash
git push -u origin trt-migration
```

After Slice 2 exists, the `git rm -r --cached RVC/` backlog item is unblocked — but
still don't run it casually; those two files must stay tracked (adjust `.gitignore`
with negation entries for them if/when the cleanup happens).

---

## Phase C — Task 9: the GPU verification chain (announce cost before each step)

Run in this exact order; each step gates the next.

### C1. Probe the environment and assets

```bash
modal run modal_deploy/compile_trt.py::probe
```
Expected: `TensorrtExecutionProvider` in providers; asset report printed.
**If `rmvpe.pt` is missing in both locations** [USER-RUN]:
```bash
modal volume put rvc-models <local-path-to-rmvpe.pt> assets/rmvpe/rmvpe.pt
```
then re-run the probe.

### C2. Export the three ONNX models (parity-gated)

```bash
modal run modal_deploy/export_onnx.py
```
Expected: three `parity cosine≥0.999` lines and three "committed to volume" lines.
A green run IS the export test. If `export_hubert` hits a fairseq tracing error the
shims don't cover, stop and report the traceback — don't improvise a new patch mid-run.

### C3. Build the TRT engine caches (latency-gated)

```bash
modal run modal_deploy/compile_trt.py::build_engines
```
Expected: one multi-minute engine build, then 10 warm blocks with
`median <= 400ms` — the job now **raises** if the gate fails. Record the min/median/p95
numbers; they go in the results table.

### C4. Produce the two A/B WAVs

```bash
# Baseline: current ONNX/CUDA path with pm pitch (what production runs today)
modal run modal_deploy/worker.py::main_chunked --pitch 12 --use-trt 0
mv D:\Kiera\test11_chunked.wav D:\Kiera\test11_chunked_baseline.wav

# Candidate: TRT engines with RMVPE
modal run modal_deploy/worker.py::main_chunked --pitch 12 --use-trt 1
mv D:\Kiera\test11_chunked.wav D:\Kiera\test11_chunked_trt.wav
```
Record per-block `convert_block` timings printed for both runs.

### C5. The listen test [USER-RUN — human ears required]

Compare `test11_chunked_baseline.wav` vs `test11_chunked_trt.wav` vs the single-pass
reference `test11.wav`. Checklist:

| Check | What to listen for | Why it's on the list |
|---|---|---|
| Voice identity | TRT ≥ baseline resemblance to the trained voice | The whole point |
| Pitch sanity | No octave jumps / chipmunk segments | RMVPE re-enable |
| Block seams | No clicks/roughness at ~1 s intervals | Fixed 16k-sample pad replaced Config-driven padding |
| **Breath/unvoiced texture** | No buzzy or repeating pattern in breaths and s/f/h sounds | The deterministic noise shim repeats per block — finding 4's specific risk |
| Loudness dynamics | Levels track the input naturally | `rms_mix_rate` works on the TRT path only |

### C6. Record the results

Append the timing table + listen verdicts to `.agents/context/subsystem-notes.md`
(TensorRT section) and commit on the branch. **Gate:** if any listen check fails,
stop here and report — the fix is likely pad size or the noise shim, both of which
require re-export and a fresh C2→C5 pass.

---

## Phase D — Decide the keep-warm loop (before any deploy)

`backend/main.py:99` currently pings `/health` every 90 s unconditionally: the L4 never
scales down. This is a **product/cost decision**, not a code detail:

| Option | Cost | Behavior |
|---|---|---|
| **(a) Delete the loop** (recommended default) | ~$0 idle | Warm the GPU at shift start via existing `POST /api/warmup`; first call after >2 min idle risks the ~75 s cold start (the fail-closed gate holds the call rather than leaking raw audio) |
| **(b) Env-gated loop** | Only while enabled | Keep the loop but guard it: `if os.getenv("RVC_KEEPWARM", "0") != "1": return` as the first line of `_rvc_keepwarm_loop()`. Toggle from Render env at shift start/end, no redeploy |
| (c) Keep as-is, 24/7 | ~730 h/mo × L4 rate ≈ **$550–600/mo** | Zero cold starts, always billed |

Option (b) is 2 lines and preserves the capability — recommended if cold starts during
shifts are a real pain. Whichever is chosen: implement, amend Slice 6 (or add a commit),
and record the decision in `.agents/decisions/log.md`.

---

## Phase E — Task 10: rollout [USER-RUN, in this order]

1. **Deploy the worker** (Modal only — does not touch Render):
   ```bash
   modal deploy modal_deploy/worker.py
   ```
2. **Verify live state** (committed ≠ deployed — always check):
   `GET <worker-url>/health` → expect `"engine": "trt"`, `"trt_cache": "hot"`,
   `"cuda_device": "NVIDIA L4"`. If `trt_cache` is `"cold"`, re-run C3 and re-check.
   If `"engine"` is `"onnx-cuda"`, TRT init failed — read `modal app logs rvc-worker`
   before proceeding; do not roll out on the fallback engine.
3. **Live test call** on the still-deployed OLD backend (worker change alone is
   compatible — the `/ws` protocol is unchanged). Watch `infer_ms` in the stats
   messages / logs; expect ≈ the C3 numbers.
4. **Merge + push the backend** (this deploys Render: buffer 1.25 s + keep-warm
   decision go live):
   ```bash
   git checkout main && git merge trt-migration && git push
   ```
   Not during a live call. Then one more test call end-to-end: no gaps at the 1.25 s
   cushion (if gaps: revert the single constant to 3.0 s and record observed jitter).
5. **Close the loop in the second brain**: decisions-log entry (TRT live, RMVPE
   re-enabled, keep-warm decision), update the backlog TensorRT row, update
   `wiki/pages/issues/tensorrt-migration.md` status, and schedule Phase 2
   (0.25 s + smaller blocks) only after ~a week of live `infer_ms` p95 ≤ 400 ms.

---

## Quick status ledger (tick as you go)

- [ ] A1–A5 hardening fixes (+ A6 optional consolidation)
- [ ] A7 local tests green
- [ ] B: 6 slices + docs committed on `trt-migration`, branch pushed
- [ ] C1 probe green (rmvpe.pt present)
- [ ] C2 three exports, parity ≥ 0.999
- [ ] C3 engine build, warm median ≤ 400 ms: ____ ms
- [ ] C4 two WAVs produced; timings recorded
- [ ] C5 listen test passed (identity / pitch / seams / breath / loudness)
- [ ] D keep-warm decision made + recorded: ____
- [ ] E1–2 worker deployed, /health verified `engine=trt`, cache hot
- [ ] E3 live call at old backend OK, infer_ms ≈ bench
- [ ] E4 backend merged+pushed, live call OK at 1.25 s buffer
- [ ] E5 brain/wiki updated; Phase 2 gate scheduled
