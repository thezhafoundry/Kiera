# Desktop Voice Changer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a browser-first Windows desktop client that converts the agent microphone through Kiera's existing Modal RVC stream and routes the converted 48 kHz audio into WhatsApp Desktop, Zoom, and Discord through VB-CABLE.

**Architecture:** Add an authenticated desktop WebSocket relay beside the existing LiveKit/Twilio routes. The browser captures device-native audio, emits exact 20 ms 16 kHz PCM frames, consumes converted 48 kHz PCM, and plays it to a selected `CABLE Input` sink. The relay owns the Modal API key and reuses `RVCStreamingConverter`; `backend/pipeline.py` and the Modal worker are unchanged in the first implementation.

**Tech Stack:** FastAPI WebSocket routes, Python asyncio, existing `RVCStreamingConverter`, vanilla HTML/CSS/ES6, Web Audio API AudioWorklet, Node's built-in test runner for pure browser protocol helpers, Chrome/Edge on Windows, VB-CABLE.

## Global Constraints

- Input is raw 16 kHz mono 16-bit PCM in exact 20 ms/640-byte frames.
- Output is raw 48 kHz mono 16-bit PCM, published/played in exact 10 ms/960-byte frames.
- Conversion outages output silence; raw unconverted microphone audio is never a fallback.
- The Modal API key remains server-side and is never placed in a URL or browser bundle.
- One persistent Modal WebSocket is used per desktop session.
- The existing LiveKit/Twilio PSTN path and `backend/pipeline.py` behavior remain unchanged.
- The first release uses the existing configured Modal model and pitch-profile behavior; arbitrary model inventory selection is deferred.
- The first release targets Windows 10/11 with Chrome or Edge and an installed VB-CABLE device.
- Do not add React/Vite or a custom audio driver for this track.
- Do not commit `.env`, voice models, API keys, or captured audio.

---

## File Map

- Create: `backend/desktop_audio.py` — frame constants, ticket store, framing helpers, bounded bridge and silence/failure state.
- Modify: `backend/main.py` — initialize the ticket store and add authenticated desktop session/WebSocket routes.
- Create: `backend/test_desktop_audio.py` — unit and fake-WebSocket tests for protocol, ticket and fail-closed behavior.
- Create: `frontend/desktop/index.html` — desktop setup, status, device and test controls.
- Create: `frontend/desktop/desktop.css` — desktop-only layout using the existing Keira visual language.
- Create: `frontend/desktop/desktop.js` — session lifecycle, device enumeration, worklet graph, UI state and telemetry.
- Create: `frontend/desktop/audio_protocol.js` — pure PCM/resampling/frame helpers shared by the page and Node tests.
- Create: `frontend/desktop/capture-worklet.js` — microphone capture, mono mix and 48 kHz-to-16 kHz stream framing.
- Create: `frontend/desktop/playout-worklet.js` — converted-audio queue, 48 kHz output and silence on underrun.
- Create: `frontend/desktop/audio_protocol.test.mjs` — deterministic Node tests for frame and resampling helpers.
- Modify: `README.md` — Windows/VB-CABLE desktop setup and run instructions.

---

### Task 1: Define and test the desktop audio protocol primitives

**Files:**
- Create: `backend/desktop_audio.py`
- Create: `backend/test_desktop_audio.py`

**Interfaces:**
- `INPUT_SAMPLE_RATE = 16000`, `OUTPUT_SAMPLE_RATE = 48000`.
- `INPUT_FRAME_BYTES = 640`, `OUTPUT_FRAME_BYTES = 960`.
- `DesktopSessionStore.issue(profile: Literal["male", "female"]) -> tuple[str, int]` returns a single-use ticket carrying the selected configured profile and expiry seconds.
- `DesktopSessionStore.consume(ticket: str) -> str | None` atomically consumes an unexpired ticket and returns its profile, or returns `None`.
- `validate_input_frame(frame: bytes) -> None` raises `ValueError` unless `len(frame) == 640`.
- `split_output_frames(buffer: bytearray, chunk: bytes) -> list[bytes]` removes and returns complete 960-byte frames while retaining a partial tail.
- `silence_frame() -> bytes` returns exactly 960 zero bytes.

- [ ] **Step 1: Write failing tests**

```python
def test_input_frame_contract():
    validate_input_frame(bytes(640))
    with pytest.raises(ValueError):
        validate_input_frame(bytes(639))

def test_output_framing_retains_partial_tail():
    pending = bytearray()
    assert split_output_frames(pending, bytes(961)) == [bytes(960)]
    assert pending == bytearray(bytes(1))

def test_session_ticket_carries_profile_and_is_single_use():
    store = DesktopSessionStore(ttl_seconds=1, clock=lambda: 100.0)
    ticket, expires_in = store.issue("male")
    assert expires_in == 1
    assert store.consume(ticket) == "male"
    assert store.consume(ticket) is None

def test_silence_frame_matches_output_contract():
    assert silence_frame() == bytes(960)
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python -m pytest backend/test_desktop_audio.py -q`

Expected: FAIL because `backend.desktop_audio` does not yet expose the constants, store and helpers.

- [ ] **Step 3: Implement the minimal pure helpers**

Use `secrets.token_urlsafe(32)` for tickets, store only a hash of each ticket plus the validated `male`/`female` profile, compare expiry using the injected monotonic clock, and delete a ticket before returning the profile. Use a `bytearray` for output accumulation so no incomplete frame is emitted.

- [ ] **Step 4: Run the focused tests and verify success**

Run: `python -m pytest backend/test_desktop_audio.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the protocol foundation**

```bash
git add backend/desktop_audio.py backend/test_desktop_audio.py
git commit -m "feat: define desktop audio framing contract"
```

### Task 2: Add the authenticated backend desktop session and relay

**Files:**
- Modify: `backend/desktop_audio.py`
- Modify: `backend/main.py`
- Modify: `backend/test_desktop_audio.py`

**Interfaces:**
- `DesktopAudioBridge(converter: VoiceConverter, input_queue_frames: int = 25)`.
- `DesktopAudioBridge.run(websocket: WebSocket) -> None` first requires `{ "type": "config", "sample_rate_in": 16000, "sample_rate_out": 48000, "frame_ms": 20 }`, then consumes binary 640-byte frames and emits complete converted 960-byte frames plus JSON status/error messages.
- `POST /api/desktop/session` accepts `{ "profile": "male" | "female" }`, requires `Authorization: Bearer <KEIRA_CONTROL_TOKEN>`, and returns `{ "ticket": str, "expires_in": int }`.
- `WS /api/desktop/audio` accepts the ticket as `keira-desktop.<ticket>` in `Sec-WebSocket-Protocol`; it never accepts a ticket from a query parameter.

- [ ] **Step 1: Add fake converter and WebSocket tests**

Cover these cases with an in-memory fake converter and a fake WebSocket: a valid ticket reaches `ready`, a malformed frame produces an error and no converter input, converter output is split into 960-byte frames, queue overflow drops oldest input and reports a counter, and converter failure sends `error` followed by silence/close. Include a test asserting an input sentinel is never present in any output message.

- [ ] **Step 2: Run the relay tests and verify failure**

Run: `python -m pytest backend/test_desktop_audio.py -q`

Expected: FAIL because the bridge, routes and lifecycle integration do not exist.

- [ ] **Step 3: Implement the bridge and routes**

In `DesktopAudioBridge`, use a bounded `asyncio.Queue[bytes]` of 25 input frames (500 ms). On overflow, remove the oldest frame, increment `input_drop_count`, and emit status; never enqueue or emit raw fallback audio. Feed one async iterator into `RVCStreamingConverter.convert_stream`, split every returned chunk through `split_output_frames`, and emit JSON `ready`, `stats`, `error`, and `stopped` messages with no audio payloads.

In `main.py`, create `app.state.desktop_sessions = DesktopSessionStore()` during lifespan startup. The POST route accepts `{ "profile": "male" | "female" }` and uses the existing `require_control_token`; it rejects any other profile. The WebSocket route extracts and validates only the `keira-desktop.` subprotocol, consumes the ticket atomically, maps `male` to the existing `RVC_MALE_PITCH_SHIFT` and `female` to zero, creates `RVCStreamingConverter` with the existing `RVC_ENDPOINT_URL`, `RVC_API_KEY`, index-rate, RMS and protect settings, and wraps the bridge in `contextlib.aclosing`/`finally` cleanup. Reject missing configuration with HTTP/WebSocket errors before accepting audio.

- [ ] **Step 4: Run focused and regression tests**

Run: `python -m pytest backend/test_desktop_audio.py backend/test_streaming_safety.py backend/test_control_plane.py -q`

Expected: PASS; existing control-plane tests remain green.

- [ ] **Step 5: Commit the authenticated relay**

```bash
git add backend/desktop_audio.py backend/main.py backend/test_desktop_audio.py
git commit -m "feat: add authenticated desktop audio relay"
```

### Task 3: Implement and test browser PCM/resampling helpers

**Files:**
- Create: `frontend/desktop/audio_protocol.js`
- Create: `frontend/desktop/audio_protocol.test.mjs`

**Interfaces:**
- `mixToMono(channels: Float32Array[]) -> Float32Array`.
- `createDownsampleState() -> DownsampleState` returns caller-owned filter history for one capture stream.
- `downsample48kTo16k(input: Float32Array, state: DownsampleState) -> Float32Array` preserves stream state through the supplied state, applies a fixed 3:1 low-pass polyphase filter with a 7.2 kHz cutoff, and emits the exact output sample count for complete input groups.
- `float32ToPcm16(input: Float32Array) -> Uint8Array` uses little-endian signed 16-bit PCM with clipping to `[-1, 1]`.
- `pcm16ToFloat32(input: ArrayBuffer) -> Float32Array` decodes little-endian signed 16-bit PCM.
- `takeFrames(buffer: Float32Array, frameSamples: number) -> { frames: Float32Array[], remainder: Float32Array }` never returns a partial frame.

- [ ] **Step 1: Write failing Node tests**

Test mono averaging, `0.5` conversion to positive PCM, clipping at `1.0`/`-1.0`, 960-at-48-kHz to 320-at-16-kHz framing, preservation of a partial sample carry, attenuation of a 12 kHz input tone after downsampling, and no partial 320-sample frame emission.

- [ ] **Step 2: Run the Node tests and verify failure**

Run: `node --test frontend/desktop/audio_protocol.test.mjs`

Expected: FAIL because the helper module does not exist.

- [ ] **Step 3: Implement the pure helpers**

Keep them dependency-free ES modules so the browser page and worklets can use the same numerical rules. Generate the fixed FIR coefficients once with a windowed-sinc kernel centered at the current sample and a Hann window; retain the filter history in the caller-owned carry state. Do not use a browser-only API in this file.

- [ ] **Step 4: Run the Node tests and verify success**

Run: `node --test frontend/desktop/audio_protocol.test.mjs`

Expected: PASS.

- [ ] **Step 5: Commit the browser protocol layer**

```bash
git add frontend/desktop/audio_protocol.js frontend/desktop/audio_protocol.test.mjs
git commit -m "feat: add desktop PCM framing helpers"
```

### Task 4: Add AudioWorklet capture, playout and WebSocket client

**Files:**
- Create: `frontend/desktop/capture-worklet.js`
- Create: `frontend/desktop/playout-worklet.js`
- Modify: `frontend/desktop/desktop.js`

**Interfaces:**
- Capture worklet posts `{ type: "frame", pcm: ArrayBuffer }` containing exactly 640 bytes.
- Playout worklet accepts `{ type: "audio", pcm: ArrayBuffer }`, maintains a maximum 5-second queue, and outputs silence when no 48 kHz samples are available.
- `DesktopAudioClient.start({ inputDeviceId, outputDeviceId, ticket }) -> Promise<void>`.
- `DesktopAudioClient.stop() -> Promise<void>`.
- `DesktopAudioClient.onStatus(callback: (status: DesktopStatus) => void) -> void`.
- `DesktopAudioClient.onMeters(callback: (meters: { input: number, output: number, bufferMs: number }) -> void) -> void`.

- [ ] **Step 1: Add browser-level test seams**

Expose the WebSocket factory, `AudioContext` factory and `navigator.mediaDevices` provider through constructor options so Node/browser tests can supply fakes without opening a real microphone.

- [ ] **Step 2: Add capture worklet behavior**

Accumulate device-native samples in a ring buffer, mix channels to mono, apply the anti-aliased 3:1 resampler when the capture context is 48 kHz, and post only complete 320-sample/640-byte PCM frames. Request a 48 kHz `AudioContext`; if the browser reports another device rate, keep the context contract at 48 kHz and report a configuration error rather than silently changing the wire format.

- [ ] **Step 3: Add playout worklet behavior**

Decode incoming PCM into a bounded Float32 queue, drain at 48 kHz, and fill every missing sample with zero. Track queued milliseconds, oldest-drop count and underrun count in periodic messages to the page.

- [ ] **Step 4: Add the client WebSocket and AudioContext graph**

Open `wss://<current-host>/api/desktop/audio` with `keira-desktop.<ticket>` as the subprotocol, send `{ type: "config", sample_rate_in: 16000, sample_rate_out: 48000, frame_ms: 20 }` before binary frames, send binary capture frames, post returned PCM to the playout worklet, and set the `AudioContext` sink to the selected `CABLE Input` device when `setSinkId` is available. On any close/error, disconnect the graph and leave the playout node producing silence.

- [ ] **Step 5: Run protocol tests and a local browser smoke test**

Run: `node --test frontend/desktop/audio_protocol.test.mjs`

Then run the server and verify in Chrome/Edge that the page can enumerate devices, request microphone permission, emit 640-byte frames to a fake relay, and stop without an active microphone track.

- [ ] **Step 6: Commit the worklet/client layer**

```bash
git add frontend/desktop/capture-worklet.js frontend/desktop/playout-worklet.js frontend/desktop/desktop.js
git commit -m "feat: add desktop audio worklets and client"
```

### Task 5: Build the desktop setup and status page

**Files:**
- Create: `frontend/desktop/index.html`
- Create: `frontend/desktop/desktop.css`
- Modify: `frontend/desktop/desktop.js`

**Interfaces:**
- `POST /api/desktop/session` is called with the in-memory control token.
- The page displays the state machine `signed_out`, `starting`, `warming`, `ready`, `converting`, `interrupted`, `stopped`.
- The Start control is disabled unless a selected microphone, `CABLE Input`, valid ticket and backend `ready` state are present.

- [ ] **Step 1: Add markup for setup and telemetry**

Include controls for token, voice profile (`male`/`female` matching existing Kiera behavior), microphone, converted-output device, voice-test, start and stop. Include status, input/output meters, latency, playout buffer, reconnect/drop counters, and a WhatsApp setup instruction block. Do not include a raw-audio monitoring toggle.

- [ ] **Step 2: Add device and safety validation**

Enumerate `audioinput` and `audiooutput` devices after permission. Require an output device whose label contains the VB-CABLE output sink, warn when the selected input label is `CABLE Output`, and refuse to start if `AudioContext.setSinkId` is unavailable or no cable sink is selected.

- [ ] **Step 3: Add the pre-call voice test**

Start a temporary conversion session, send a short microphone recording through the real relay, play the returned converted audio to the normal headphones sink, and show the measured round-trip time. Close the temporary ticket/session after playback.

- [ ] **Step 4: Add WhatsApp-ready start/stop flow**

Before starting, obtain a fresh ticket with `fetch('/api/desktop/session', { method: 'POST', headers: { Authorization: \`Bearer ${token}\`, 'Content-Type': 'application/json' }, body: JSON.stringify({ profile }) })`, connect with that ticket, wait for `ready`, and then enable conversion. On stop, close the client, stop every microphone track, close the AudioContext and clear all meters. Keep the last error visible until the next successful start.

- [ ] **Step 5: Run the frontend smoke test**

Run the server with `uvicorn backend.main:app --reload --port 8000`, open `http://localhost:8000/desktop/`, and verify the complete state transitions with a fake or configured Modal endpoint. Expected: no browser console errors, no raw-audio playback path, and clean Stop/restart behavior.

- [ ] **Step 6: Commit the page**

```bash
git add frontend/desktop/index.html frontend/desktop/desktop.css frontend/desktop/desktop.js
git commit -m "feat: add desktop voice changer setup page"
```

### Task 6: Document operation and run the full verification suite

**Files:**
- Modify: `README.md`
- Modify: `backend/test_desktop_audio.py` if integration gaps are found

- [ ] **Step 1: Document prerequisites and exact device mapping**

Add Windows instructions for installing VB-CABLE, opening `/desktop/`, granting microphone permission, selecting the physical microphone and `CABLE Input`, setting WhatsApp's microphone to `CABLE Output`, and choosing headphones for WhatsApp speakers. State clearly that the recipient receives silence during conversion outages.

- [ ] **Step 2: Run all automated tests**

Run: `python -m pytest backend/test_pipeline.py backend/test_streaming_safety.py backend/test_call_safety.py backend/test_control_plane.py backend/test_desktop_audio.py -q`

Expected: PASS with no changes to existing PSTN safety behavior.

- [ ] **Step 3: Run the browser protocol tests**

Run: `node --test frontend/desktop/audio_protocol.test.mjs`

Expected: PASS.

- [ ] **Step 4: Perform the Windows/VB-CABLE acceptance run**

Record device names, model readiness time, median/P95 mouth-to-ear latency, input/output drops, underruns and duration drift during a ten-minute WhatsApp call. Interrupt the network and unplug/reconnect the microphone; verify silence and clean recovery.

- [ ] **Step 5: Commit documentation and verification evidence**

```bash
git add README.md backend/test_desktop_audio.py
git commit -m "docs: add desktop voice changer setup and verification"
```

---

## Self-review checklist

- [ ] Every approved design requirement maps to at least one task.
- [ ] No task exposes `RVC_API_KEY` to the browser.
- [ ] No task forwards raw audio on an error path.
- [ ] Input/output frame sizes match Kiera's 16 kHz/20 ms and 48 kHz/10 ms contracts.
- [ ] Existing PSTN pipeline files are not modified by the first implementation.
- [ ] Tests cover ticket reuse, malformed frames, output framing, underruns, overflow and raw-leak prevention.
- [ ] The plan contains no unresolved placeholders or undefined neighboring interfaces.
