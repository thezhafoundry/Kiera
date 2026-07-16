# RVC-First Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a trustworthy RVC latency baseline, then reduce warm p95 mouth-to-ear latency below 500 ms without harming intelligibility, voice identity, continuity, or fail-closed safety.

**Architecture:** RVC remains the only production engine. First make the persistent WebSocket handshake, block timing, and call summaries report the exact deployed profile. Then centralize block/context/SOLA geometry so the baseline and Candidate B cannot silently disagree across streaming, ONNX, and TensorRT modules. Promote a lower-latency profile only after offline, two-call, and staff-PSTN gates pass.

**Tech Stack:** Python 3.11+, FastAPI WebSockets, NumPy DSP, ONNX Runtime/TensorRT on Modal L4, LiveKit, Twilio SIP, pytest.

## Global Constraints

- LLVC training and deployment are paused; do not download a corpus or integrate a real LLVC service.
- Never publish or replay raw representative audio. All failures remain fail-closed.
- RVC input remains 16 kHz mono PCM16 in 20 ms / 640-byte frames; output remains 48 kHz mono PCM16.
- RVC remains the default and only customer-call engine during this plan.
- Do not use or deploy with any provider credential that was pasted into chat until it has been rotated.
- Do not use the `chillandbuild` Modal workspace; the authorized workspace is `thezhafoundry`.
- UptimeRobot may keep Render responsive but must never be described as keeping the Modal GPU warm.
- Change only one latency profile variable set at a time and retain the baseline rollback path.

---

### Task 1: Make RVC profile and latency telemetry truthful

**Files:**
- Modify: `modal_deploy/worker.py`
- Modify: `backend/converters/rvc_stream.py`
- Modify: `backend/pipeline.py`
- Modify: `backend/test_pipeline.py`

**Interfaces:**
- Produces: RVC `ready` payload fields `model_version`, `profile`, `block_ms`, `context_ms`, `sola_ms`, `sample_rate_in`, `sample_rate_out`, and `use_trt`.
- Produces: per-block stats whose `converter_wait_ms` begins at the first input frame of the block rather than after the final frame.

- [ ] **Step 1: Add failing handshake and dynamic-block tests**

Add tests that assert the ready payload configures the client block size and that two 160 ms blocks create two timestamp entries without using a hardcoded `0.32` constant. Also assert the call summary includes `converter_wait_ms`, `network_rtt_ms`, and an estimated playout age.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
.venv/bin/python -m backend.test_pipeline
```

Expected: the new ready-metadata/dynamic-block assertions fail while the existing suite remains otherwise healthy.

- [ ] **Step 3: Extend the server handshake**

Return the effective runtime geometry from `modal_deploy/worker.py`:

```python
await ws.send_json({
    "type": "ready",
    "model_version": MODEL_VERSION,
    "profile": st.PROFILE_NAME,
    "block_ms": st.BLOCK_MS,
    "context_ms": st.CONTEXT_MS,
    "sola_ms": st.SOLA_CROSSFADE_SAMPLES * 1000 // st.SAMPLE_RATE_OUT,
    "sample_rate_in": st.SAMPLE_RATE_IN,
    "sample_rate_out": st.SAMPLE_RATE_OUT,
    "use_trt": os.environ.get("USE_TRT", "0") == "1",
})
```

- [ ] **Step 4: Remove hardcoded client block accounting**

Store `block_ms` from `ready`, calculate `bytes_per_block = sample_rate_in * 2 * block_ms // 1000`, and timestamp the first sent frame of each block. Reset partial accounting on reconnect so timestamps never cross sessions.

- [ ] **Step 5: Verify GREEN and commit**

Run the complete pipeline suite and commit only the four files above.

---

### Task 2: Centralize baseline and Candidate B geometry

**Files:**
- Create: `modal_deploy/rvc_profiles.py`
- Create: `modal_deploy/test_rvc_profiles.py`
- Modify: `modal_deploy/streaming.py`
- Modify: `modal_deploy/trt_pipeline.py`
- Modify: `modal_deploy/export_onnx.py`
- Modify: `modal_deploy/compile_trt.py`
- Modify: `modal_deploy/worker.py`

**Interfaces:**
- Produces: `get_profile(name: str) -> RVCProfile`.
- Defines exactly two initial profiles: `baseline=320/400/80/250` and `candidate_b=160/240/40/160` milliseconds.

- [ ] **Step 1: Add failing profile consistency tests**

Assert both profiles satisfy these contracts:

```python
assert profile.canonical_in == (profile.block_ms + profile.context_ms) * 16
assert profile.sola_samples == profile.sola_ms * 48
assert profile.playout_ms > 0
assert profile.name in {"baseline", "candidate_b"}
```

Also assert an unknown name raises `ValueError` and that baseline values exactly match the current deployed constants.

- [ ] **Step 2: Confirm RED**

Run:

```bash
.venv/bin/python -m pytest modal_deploy/test_rvc_profiles.py -q
```

Expected: import failure because `modal_deploy.rvc_profiles` does not exist.

- [ ] **Step 3: Implement the immutable profile registry**

Use a frozen dataclass and an explicit mapping; do not accept arbitrary numeric environment overrides:

```python
@dataclass(frozen=True)
class RVCProfile:
    name: str
    block_ms: int
    context_ms: int
    sola_ms: int
    playout_ms: int

    @property
    def canonical_in(self) -> int:
        return (self.block_ms + self.context_ms) * 16

    @property
    def sola_samples(self) -> int:
        return self.sola_ms * 48
```

- [ ] **Step 4: Replace duplicated constants**

Read `RVC_STREAM_PROFILE`, defaulting to `baseline`, in the Modal runtime and build scripts. Export/compile/runtime must all print the chosen name and `canonical_in`; abort initialization if the ONNX/TRT static shape does not match the selected profile.

- [ ] **Step 5: Verify both profiles locally and commit**

Run:

```bash
.venv/bin/python -m pytest modal_deploy/test_rvc_profiles.py modal_deploy/test_streaming.py modal_deploy/test_trt_pipeline.py -q
```

Do not deploy Candidate B in this task.

---

### Task 3: Produce the warm baseline report

**Files:**
- Create after measurement: `.agents/session-reports/rvc-baseline-20260716.md`
- Modify after measurement: `LATENCY.md`

**Interfaces:**
- Consumes: rotated provider credentials and verified `thezhafoundry` Modal authentication.
- Produces: one immutable baseline report containing commit SHA, Modal function version, model checksum, profile, cold/warm state, and raw p50/p95/max values.

- [ ] **Step 1: Verify provider identity without printing secrets**

Run `modal profile current`, `modal token info`, and provider health checks that redact tokens. Stop if the workspace is not `thezhafoundry`.

- [ ] **Step 2: Verify deployed parity**

The RVC `ready` payload must report `profile=baseline`, `block_ms=320`, `context_ms=400`, `sola_ms=80`, and `use_trt=true`. Stop if any field differs.

- [ ] **Step 3: Run one warm staff PSTN call**

Use the browser spectral-tone path followed by at least five minutes of normal speech. Save Render/Modal summaries, browser telemetry, and Twilio/LiveKit identifiers without saving raw call audio.

- [ ] **Step 4: Calculate baseline gates**

Report warm p50/p95/max for ingress age, converter wait, network RTT, inference, playout age, estimated mouth-to-ear, drops, and underruns. Mark estimated mouth-to-ear separately from the spectral end-to-end measurement.

- [ ] **Step 5: Reconcile `LATENCY.md`**

Correct the stale 1.25-second statement to the measured deployed value; do not replace estimates with claims of measured PSTN latency.

---

### Task 4: Compile and evaluate Candidate B offline

**Files:**
- Generated outside Git: profile-specific ONNX/TRT artifacts in the Modal model volume
- Create after measurement: `.agents/session-reports/rvc-candidate-b-20260716.md`

**Interfaces:**
- Consumes: `candidate_b=160/240/40/160` and the same authorized evaluation corpus used for baseline.
- Produces: a promote/reject decision; no automatic production switch.

- [ ] **Step 1: Export and compile Candidate B artifacts**

Build with `RVC_STREAM_PROFILE=candidate_b`, retain baseline artifacts under a separate profile path, and fail if active providers are not TensorRT.

- [ ] **Step 2: Run deterministic engine checks**

Require exact 16-to-48 kHz duration preservation, zero raw fallback, zero queue overflow, and warm p95 inference below 100 ms.

- [ ] **Step 3: Run blind baseline-versus-Candidate-B evaluation**

Reject Candidate B if WER worsens by more than two absolute points, speaker similarity falls below 95% of baseline, any duration/pitch seam appears, or median naturalness is below 4/5.

- [ ] **Step 4: Commit only configuration/code changes**

Never commit model files, evaluation WAVs, provider logs containing secrets, or customer recordings.

---

### Task 5: Staff PSTN, concurrency, warmup, and rollout gate

**Files:**
- Modify only if measurement justifies it: `modal_deploy/worker.py`
- Modify only if measurement justifies it: `backend/main.py`
- Modify: `README.md`
- Modify: `.agents/decisions/log.md`
- Modify: `.agents/projects/active-backlog.md`

**Interfaces:**
- Produces: a reversible `RVC_STREAM_PROFILE` deployment choice and a documented baseline rollback.

- [ ] **Step 1: Run one staff-only Candidate B call**

Require p95 mouth-to-ear below 500 ms, no raw leakage, no audible pitch jump, no broken syllables, and staff listening approval.

- [ ] **Step 2: Run two simultaneous 30-minute calls**

Require one active session per container, at most two containers, no `busy` after dialing, resource usage below 70%, no stale replay, no overflow, and no underrun.

- [ ] **Step 3: Measure cold-start policy**

Compare pre-dial warmup with `scaledown_window=120`. Do not enable `min_containers=1` until its operating-hours cost is explicitly approved.

- [ ] **Step 4: Promote or roll back**

Promote Candidate B only if every Task 4 and Task 5 gate passes. Roll back by setting `RVC_STREAM_PROFILE=baseline` and deploying the already-preserved baseline artifacts.

- [ ] **Step 5: Close documentation and second-brain state**

Run:

```bash
make second-brain-close
```

Record measured results, rejected settings, rollback instructions, and remaining zero-shot evaluation work.
