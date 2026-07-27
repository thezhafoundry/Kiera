# AWS Migration Design — Backend + GPU Worker (Desktop/WhatsApp Path)

Date: 2026-07-27
Status: Approved, pending implementation plan

## Context

Keira currently runs the backend (`backend/main.py`, FastAPI/uvicorn) on Render (Singapore)
and the RVC voice-conversion GPU worker (`modal_deploy/`) on Modal (`ap-southeast`,
serverless L4/TensorRT). The user has set up the AWS CLI locally and wants to consolidate
both onto AWS, in the `ap-south-1` (Mumbai) region, into a single AWS account.

**Primary target usage is the desktop virtual-mic flow** (`/desktop/`, documented in
`.agents/context/subsystem-notes.md`): a browser captures the agent's mic, the backend
relays audio to the RVC worker over a WebSocket, converted audio is routed through a virtual
audio cable (BlackHole/VB-CABLE) into WhatsApp Desktop's microphone input. This is
**replacing** the Twilio/LiveKit PSTN calling path as the primary use case going forward.

**Explicitly out of scope for this migration:**
- Retiring/removing the Twilio/LiveKit code paths (`backend/main.py`'s SIP webhooks,
  `/api/setup`, `backend/pipeline.py`'s `VoiceConversionWorker` LiveKit bot loop). That code
  stays in the repo, untouched, and is tracked as a separate follow-up decommission task in
  `.agents/projects/active-backlog.md` once this migration is verified. Twilio/LiveKit
  region-latency considerations (Singapore media edge pinning, SIP RTP path) do not apply to
  this migration — the desktop flow's only relevant network hop is browser ↔ AWS backend ↔
  AWS GPU worker.
- Any change to the RVC model/pipeline itself (pitch lock, TensorRT engine, streaming DSP in
  `modal_deploy/streaming.py`) — this migration moves *where* the existing worker code runs,
  not what it does.

**Explicit region decision:** ap-south-1 (Mumbai), matching the user's AWS CLI setup, chosen
knowingly even though it doesn't match Render/Modal/Twilio's current Singapore colocation —
acceptable because the PSTN/Twilio/LiveKit latency path this colocation existed for is not in
scope; the desktop flow has no Twilio/LiveKit hop to protect.

## Goals

- Move the backend and the RVC GPU worker onto AWS EC2 in `ap-south-1`, under one AWS account.
- Preserve the existing fail-closed behavior: no converted-audio path may ever fall back to
  publishing raw/unconverted audio; a cold or unavailable GPU worker must block/refuse rather
  than degrade.
- Preserve scale-to-zero economics for the GPU worker — this is an MVP-stage, low/bursty-call
  product; paying for an always-on GPU instance is not acceptable.
- Keep Modal running in parallel, unmodified, until the AWS path is independently verified —
  this is a re-platforming effort, not a rewrite, and should not risk the working system.

## Non-goals

- No SageMaker Async Inference (or other managed "serverless GPU" product). Evaluated and
  rejected: SageMaker Async is fundamentally request/response (S3 in, S3 out, poll/SNS
  notify) and does not fit the current persistent duplex `/ws` streaming protocol (stateful
  per-call SOLA crossfade context, block-by-block audio over one long-lived connection). Would
  force a redesign of the streaming protocol itself, which is out of scope here.
- No multi-region/HA design. Single instance per role, matching current Modal
  (`max_containers=2`, 1-concurrent-session-per-container MVP scope) and current Render
  (single service) scope.
- No changes to `RVC_API_KEY`-style auth semantics beyond moving where the secret is read
  from (Modal secret → EC2 env var / AWS Secrets Manager).

## Architecture

```
Browser (agent, desktop page)
      │ HTTPS/WSS
      ▼
EC2 #1 — Backend (ap-south-1)
  - uvicorn running backend.main:app
  - serves /desktop/ dashboard, issues desktop session tickets
  - DesktopAudioBridge relays 640B/20ms 16kHz frames in, 960B/10ms 48kHz frames out
  - holds RVC_API_KEY, calls EC2 #2 over private VPC networking
      │ private VPC (security-group restricted), WSS to /ws, HTTPS to /health
      ▼
EC2 #2 — GPU worker (ap-south-1, g5.xlarge or g6.xlarge)
  - NORMALLY STOPPED
  - FastAPI app: /health, /ws (same request/response contract as modal_deploy/worker.py today)
  - RVC weights + compiled TensorRT engine cache on an attached EBS gp3 volume
  - self-shutdown after idle timeout (replaces Modal's scaledown_window=120)
```

### Backend (EC2 #1)
- Single EC2 instance (e.g. `t3.medium`), Mumbai region.
- Runs the existing `backend/main.py` app via uvicorn — no code changes required for the
  backend's own hosting; this is a lift-and-shift from Render.
- New responsibility: before treating the GPU worker as available, ensure the EC2 #2 instance
  is running (see "Cold start / warm gate" below) — this is new code, since Render never had
  to manage Modal's compute lifecycle (Modal did that itself).
- Redeploy mechanism: git pull + restart (systemd service or equivalent), replacing Render's
  `autoDeploy: commit`. Decide the exact mechanism in the implementation plan; out of scope to
  pin down here beyond "not a manual SSH copy-paste every time."

### GPU worker (EC2 #2)
- Single EC2 GPU instance (`g5.xlarge`/`g6.xlarge` — final SKU chosen in the implementation
  plan based on L4-equivalent availability/pricing in `ap-south-1`), normally **stopped**.
- Runs the existing `modal_deploy/worker.py` FastAPI app (`/health`, `/ws`), with Modal-specific
  glue removed:
  - `@app.function(...)` decorators, `modal.Volume`, `modal.Image` build chain
    (`modal_deploy/modal_defs.py`) are Modal-only and do not port — replaced by a plain
    Dockerfile (or direct venv install) using the same `modal_deploy/requirements.txt` plus the
    TensorRT/ONNX packages currently layered in `_trt_build_base`.
  - `modal_deploy/streaming.py`, `pitch_lock.py`, `trt_pipeline.py`, `rvc_profiles.py` and the
    vendored `RVC/` tree port unchanged — these have no Modal dependency, per
    `.agents/context/subsystem-notes.md`'s note that `streaming.py` is "pure numpy/stdlib,
    unit-testable standalone."
  - The 1-concurrent-session MVP gate (`_session_active`/`_session_lock`) and
    `max_containers=2`-equivalent scope decision: this design assumes **one GPU instance,
    one concurrent session** (matching one container's limit today) — multi-instance scaling
    is not in scope; revisit only if concurrent-call volume actually requires it.
- **Model/engine storage**: EBS `gp3` volume attached to EC2 #2, mounted at the path the
  worker code expects for `rvc-models` (weights + FAISS index + compiled TensorRT engine
  cache). Chosen over "S3, re-sync on every cold start" specifically because the TensorRT
  engine-build/cache-priming step is the single largest one-time cost in the current system
  (327.7s cold engine build measured 2026-07-07) — an EBS volume attached to a stopped/started
  instance keeps that cache warm across restarts exactly like Modal's volume does, whereas
  S3 re-sync would either repeat that cost or need its own caching logic to avoid it.

### Cold start / warm gate
- Backend's existing fail-closed warm-gate contract (`VoiceConversionWorker.is_ready`,
  `wait_until_ready`, `wait_ready` probe) is preserved as the **behavioral contract** — a
  cold/unready worker must still block/refuse the desktop session, never degrade to raw audio.
- New failure mode this migration introduces: "GPU instance isn't running at all" is a state
  Modal never exposed to this codebase (Modal always presented *some* container, even if
  slow to start). The backend must now:
  1. On first request needing conversion, check EC2 #2 instance state.
  2. If stopped, call `ec2:StartInstances`, then poll `/health` the same way `POST
     /api/warmup` already polls Modal today (existing retry/backoff logic is reusable).
  3. Treat "instance stopped" and "instance running but `/health` not ready yet" as the same
     kind of cold-start wait from the caller's perspective — no new user-facing state needed
     beyond what already exists for Modal cold starts.
- Idle shutdown: worker process tracks time since last active session; after an idle timeout
  (candidate default matching Modal's `scaledown_window=120`, i.e. 2 minutes — to be validated
  against actual desktop-usage call spacing in the implementation plan), it either
  self-invokes `ec2:StopInstances` via instance-role IAM permissions, or a CloudWatch alarm on
  low CPU utilization triggers the stop via Lambda. Exact mechanism decided in the
  implementation plan; both are acceptable, self-shutdown is simpler to reason about and is
  the default assumption.

### Networking / security
- Both instances in the same VPC, private subnets where possible.
- Backend → GPU worker traffic restricted by security group (only the backend's instance/SG
  can reach EC2 #2's `/health`/`/ws` ports) rather than exposed to the public internet the way
  Modal's endpoint necessarily was.
- `RVC_API_KEY` bearer-token check on `/ws`/`/convert` is preserved unchanged as
  defense-in-depth even with network-level restriction — matches the existing
  belt-and-suspenders posture (Twilio signature validation stays even though Twilio also has
  IP-based options, per `CLAUDE.md`'s control-plane rules).
- Secrets (`RVC_API_KEY`, any AWS-side credentials) move from Modal's `rvc-api-key` secret /
  Render's env vars to EC2 instance environment variables or AWS Secrets Manager — exact
  mechanism decided in the implementation plan.

## Rollout / testing plan

1. Stand up both EC2 instances in parallel with Modal/Render still live and untouched.
2. Verify GPU worker in isolation first: cold start (instance stop → start → `/health`
   ready), warm conversion via the existing offline diagnostic tooling
   (`modal_deploy/worker.py`'s `main_chunked`/`convert_file_chunked`, portable since they don't
   depend on Modal decorators), and an EBS stop/start cycle actually preserves the TRT engine
   cache (no rebuild on second start).
3. Point a test backend instance at the AWS GPU worker and re-run
   `scripts/rvc_stream_benchmark.py` against it for a latency comparison against the existing
   Modal baseline numbers in `.agents/decisions/log.md`.
4. Run the full desktop acceptance flow (per `.agents/projects/active-backlog.md`'s pending
   macOS/BlackHole acceptance item) against the AWS-hosted backend + worker.
5. Only after 2-4 pass, cut the desktop flow over to AWS. Decommission timing for
   Render/Modal is a separate decision, made after cutover confidence, not part of this spec.

## Testing

- Reuse existing offline tests where the moved code is unchanged: `modal_deploy/test_streaming.py`,
  `modal_deploy/test_trt_pipeline.py`, `backend/test_pipeline.py`.
- New tests needed (implementation plan to detail): the backend's "ensure GPU instance
  running" helper (mockable boto3 calls), and an idle-shutdown timer if implemented in-process
  rather than via CloudWatch.
- No change to the fail-closed invariant tests — the existing warm-gate/is_ready tests should
  continue to pass unmodified since the *contract* (is_ready, wait_until_ready) is preserved;
  only what backs it changes.

## Open questions carried into the implementation plan

- Exact EC2 instance SKUs for both roles (final choice needs live AWS Mumbai pricing/quota
  check, not assumed here).
- Exact backend redeploy mechanism (systemd + git pull vs. a small CI/CD pipeline).
- Exact idle-shutdown mechanism (in-process self-stop vs. CloudWatch+Lambda).
- Whether AWS Secrets Manager is used for `RVC_API_KEY` or a plain EC2 env var suffices at
  this scale.
- GPU quota approval in `ap-south-1` (G-series instances typically require a service quota
  increase request on a fresh AWS account) — must be confirmed live before relying on this
  design, per this project's standing rule to verify infra state rather than assume it.
