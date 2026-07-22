# Desktop Voice Changer Design

**Date:** 2026-07-22
**Status:** Approved by user

## Goal

Add a browser-first Windows desktop voice-changer client that converts an agent's microphone through Kiera's existing Modal RVC service and exposes the converted 48 kHz audio to WhatsApp Desktop, Zoom, and Discord through VB-CABLE. The existing Kiera LiveKit/Twilio PSTN flow remains unchanged.

## Non-goals for the first version

- No local RVC inference or local model downloads.
- No custom Windows audio driver.
- No Windows installer until the browser prototype passes hardware acceptance tests.
- No conversion of the remote caller's audio.
- No live converted self-monitoring in headphones; a pre-call voice test is sufficient.
- No changes to the Modal model or streaming block geometry unless measurements require them.

## Architecture

```text
Windows microphone (48 kHz)
  -> browser capture AudioWorklet
  -> mono/downsampled 16 kHz, 20 ms PCM frames
  -> authenticated Kiera desktop WebSocket
  -> existing RVCStreamingConverter and Modal /ws worker
  -> converted 48 kHz PCM
  -> browser playout AudioWorklet and bounded buffer
  -> CABLE Input (VB-CABLE)
  -> CABLE Output selected as the WhatsApp microphone
```

The browser must not receive the Modal API key. The Kiera backend owns the authenticated Modal connection. The desktop route is isolated from `backend/pipeline.py`, which continues to own the LiveKit/Twilio path.

## Components and boundaries

### Browser client

Use Kiera's existing vanilla HTML/JavaScript/CSS stack, with a separate page under `frontend/desktop/`. Borrow OpenVoiceChanger's AudioWorklet, device-selection and bounded WebSocket ideas without adding its React/Vite dependency tree.

The browser must:

- capture the selected physical microphone at a device-compatible rate (normally 48 kHz);
- convert to mono and anti-aliased-resample to 16 kHz;
- emit exact 20 ms input frames (320 samples, 640 bytes, signed little-endian PCM);
- maintain a bounded converted-audio playout buffer;
- render converted output at 48 kHz in exact 10 ms frames (480 samples, 960 bytes, signed little-endian PCM);
- route playback to the selected `CABLE Input` device;
- display model/session state, input/output levels, latency and buffer health;
- output silence on an underrun, disconnect, timeout or conversion error.

### Backend desktop relay

Add an isolated desktop audio module and route. It should:

- authenticate a control-plane request and issue a short-lived, single-use desktop session ticket;
- authenticate the WebSocket using the ticket in a subprotocol, never a query string;
- validate input frame size and format before passing data to the converter;
- create one `RVCStreamingConverter` for the session;
- feed it as one long-lived async stream;
- return only converted 48 kHz PCM;
- close the converter with `contextlib.aclosing`/explicit cleanup on every disconnect;
- expose structured status/error messages without exposing secrets or raw audio;
- fail closed to silence rather than forwarding unconverted audio.

The existing `RVCStreamingConverter` remains the Modal protocol adapter. The desktop route must not duplicate its reconnect, heartbeat or bounded buffering logic.

### Modal worker

No first-version changes. It continues to receive Kiera's 16 kHz PCM stream and return 48 kHz converted PCM using the existing 320 ms block, 400 ms context and 80 ms SOLA geometry reported by the readiness handshake.

## Session lifecycle

1. Browser obtains an authenticated short-lived session ticket for the selected configured voice profile/gender.
2. Browser opens the desktop audio WebSocket and sends the ticket plus audio configuration.
3. Backend validates the ticket, creates the converter, and waits for the Modal `ready` handshake.
4. Backend sends `ready` only after the converter is usable.
5. Browser sends 20 ms PCM frames and receives converted output frames/status messages.
6. On stop, timeout, disconnect or fatal conversion error, the backend tears down the converter and the browser drains to silence.
7. Tickets expire quickly and cannot be reused; no persistent browser storage is used for operator credentials.

## Audio and latency contract

The desktop path keeps Kiera's stable transport contract:

| Stage | Contract |
|---|---|
| Browser capture | 48 kHz device-native audio, converted to mono |
| Upload frame | 16 kHz mono signed PCM, 20 ms, 320 samples, 640 bytes |
| Modal conversion | Existing long-lived RVC stream; effective geometry comes from `ready` metadata |
| Browser output | 48 kHz mono signed PCM, 10 ms, 480 samples, 960 bytes |
| Initial playout target | 250 ms bounded buffer, with silence on underrun |

OpenVoiceChanger's 4,096-sample/40 kHz chunks are not used as the network contract. They remain a possible internal DSP/visualizer buffer only. At 40 kHz, 4,096 samples add approximately 102 ms capture batching and do not map cleanly to Kiera's exact 20 ms 16 kHz frames.

Initial performance targets are measured acceptance gates, not claims about the first build: median mouth-to-ear latency below 900 ms, P95 below 1.2 s, no raw-voice leakage, and no audible dropouts in a ten-minute call. Optimize block/buffer geometry only after a Windows-to-Render benchmark establishes the baseline.

## User workflow

1. Open the desktop page in Chrome or Edge on Windows.
2. Enter the Kiera control token in memory only.
3. Select the configured voice profile/gender and wait for `Ready`. The first release uses the same configured Modal model and pitch-profile behavior as the PSTN path; arbitrary model inventory selection is deferred.
4. Select the physical microphone and `CABLE Input` output.
5. Run a short converted voice test through the real Modal path.
6. In WhatsApp Desktop, select `CABLE Output` as microphone and headphones as speakers.
7. Start conversion before answering or placing the call.
8. Stop conversion after the call; all resources are released.

Start is disabled when the model is not ready, VB-CABLE is missing, microphone permission is unavailable, or the conversion test fails. The UI warns if `CABLE Output` is accidentally selected as the physical capture input.

## Testing and acceptance

### Automated

- Session authentication, expiry and one-time-use behavior.
- Exact 20 ms input-frame validation and malformed-frame rejection.
- 48 kHz output framing and browser-facing status messages.
- Converter startup, Modal readiness timeout and cleanup on cancellation.
- Output silence on converter failure, WebSocket loss and playout underrun.
- Bounded buffer overflow policy and oldest-audio drop behavior.
- Regression coverage for existing `backend.test_pipeline`, `backend.test_streaming_safety`, `backend.test_call_safety` and `backend.test_control_plane`.

### Hardware acceptance

- Windows 10/11 with Chrome or Edge and VB-CABLE installed.
- Microphone -> Kiera -> VB-CABLE -> WhatsApp Desktop call.
- Ten-minute call with no clicks, recurring gaps, or steadily increasing delay.
- Network interruption during a call produces silence, never the raw voice.
- Microphone/cable unplug and stop/restart cleanly release browser and backend resources.
- Record measured latency, output level, frame drops, reconnect count and duration drift.

## Reusable repository references

- [OpenVoiceChanger](https://github.com/sioaeko/OpenVoiceChanger): browser AudioWorklet, device selection, local WebSocket framing and UI telemetry patterns.
- [w-okada/voice-changer](https://github.com/w-okada/voice-changer): native audio-device routing, virtual-cable monitoring and later packaging reference.
- [RVC WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI): RVC model/training/runtime compatibility reference.
