# Desktop voice changer and BlackHole handoff — 2026-07-23

## Outcome

The RVC converter and the exact local desktop relay path are working when the Modal GPU is
warm. The disabled-button report combined two separate conditions:

1. The page was being exercised in the Codex in-app browser, which did not complete the
   required Web Audio/device setup and exposed only built-in audio devices.
2. The stable Modal edge had a long `ap-southeast` L4 scheduling/cold-start delay, causing
   the desktop bridge's fail-closed 150-second readiness gate to return
   `converter_unavailable`.

Use a current regular Chrome/Edge build for the remaining human microphone and WhatsApp
acceptance. Preserve the fail-closed converter behavior.

## Current checkout and auth truth

- Branch: `main`
- Starting HEAD for this handoff: `61d677c`
- `origin/main` matched `61d677c` when inspected.
- Operator/control-token auth is globally disabled after merge `6e284bd`; see decision
  commit `61d677c`.
- `/api/desktop/session` is unauthenticated, but `/api/desktop/audio` still requires its
  short-lived, single-use `keira-desktop.<ticket>` WebSocket subprotocol.
- `scripts/run_local.py` remains loopback-bound but still sets vestigial
  `KEIRA_LOCAL_LAUNCHER`/`KEIRA_LOCAL_BIND_HOST` markers that the backend no longer reads.

## macOS and BlackHole evidence

- Driver: `/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver`
- CoreAudio device: `BlackHole 2ch`
- Transport: virtual
- Channels: 2 input, 2 output
- Sample rate: 48kHz
- MacBook Air microphone: 48kHz
- MacBook Air speakers: 44.1kHz

The missing BlackHole dropdown entry was not an installation failure. Chrome was opened at
`http://127.0.0.1:8000/desktop/`; the user must grant microphone permission before complete
device labels are available. If Chrome predates the driver installation, quit it fully with
`Cmd+Q` and reopen it.

## Button contracts

- **Test converted voice**: requires a selected physical microphone and desktop session;
  plays converted audio through normal speakers; BlackHole is not required.
- **Start conversion**: additionally requires `AudioContext.setSinkId` and an approved
  virtual output (`BlackHole`, `Loopback`, or VB-CABLE `CABLE Input`).
- **Stop**: enabled only while a desktop relay client exists.

## Fresh live results

Warm direct RVC WebSocket, 2.0-second synthetic stream:

| Metric | Result |
|---|---:|
| Active readiness | 1,970.34ms |
| Output | 174,086 bytes / 1,813.4ms |
| Stats blocks | 6 |
| TensorRT inference median / p95 | 55.64 / 58.08ms |
| Converter wait median / p95 | 1,437.22 / 1,597.90ms |
| Network estimate median / p95 | 1,062.58 / 1,221.63ms |
| Drops | 0 |

Exact local desktop route with a macOS-generated 2,208.9ms spoken sentence:

| Metric | Result |
|---|---:|
| Desktop readiness | 1,856.36ms |
| Output | 173,760 bytes / 181 frames / 1,810.0ms |
| Output geometry | 48kHz mono PCM, 960-byte frames |
| Stats messages | 6 |
| Input drops | 0 |

Ignored local listening artifact:
`/Users/prem/Documents/Kiera/kiera_conversion_test_rerun.wav`.

## Cold-path evidence

- Modal logged the stable function waiting about five minutes for an `ap-southeast` L4.
- Model/ONNX/TRT startup then took about 34 seconds.
- The first direct WebSocket opening handshake timed out; active readiness failed after
  180 seconds.
- A `/health` request returned no bytes inside a 45-second timeout.
- A later cold `/api/desktop/audio` run reached the bridge's 150-second gate and returned
  `{"type":"error","code":"converter_unavailable"}`.
- An explicit health warm-up then took about 94 seconds and returned `ready` on an NVIDIA
  L4; the identical desktop spoken test passed immediately afterward.
- `RVC_KEEPWARM` was not present in the local `.env`, so keep-warm was default-off.
  The server currently logs “RVC keep-warm loop started” even when that coroutine exits
  immediately; do not treat the startup line as proof that the GPU is being kept warm.

## Next-session order

1. Start the local server and explicitly warm the Modal `/health` endpoint.
2. Open `http://127.0.0.1:8000/desktop/` in regular Chrome.
3. Grant microphone permission and confirm both the physical microphone and
   `BlackHole 2ch` appear.
4. Run **Test converted voice** and listen for converted identity/clarity.
5. Select `BlackHole 2ch`, run **Start conversion**, and wait for ready/converting.
6. In WhatsApp Desktop, select BlackHole as the microphone and keep speakers on the
   built-in speakers/headphones.
7. Record readiness, first-audio time, UI errors, input/playout drops, and whether the
   recipient hears only converted audio.

## Open implementation items

- Add explicit desktop warming/progress and decide among longer bounded wait/retry,
  `RVC_KEEPWARM=1` cost, or broader-AP routing.
- Fix direct benchmark invocation:
  `.venv/bin/python scripts/rvc_stream_benchmark.py` currently raises
  `ModuleNotFoundError: backend`; module form works.
- Remove vestigial local-auth launcher markers/wording.
- Log the actual keep-warm enabled/disabled state.
