# Desktop readiness fix report

## Scope

Gate the desktop audio bridge's client-visible `ready` message on the active
converter stream's readiness handshake.

## Change

- `DesktopAudioBridge` now starts the conversion generator before the browser
  handshake and uses an explicit 150-second readiness timeout, matching the
  route's warm-gate timeout.
- `RVCStreamingConverter.wait_stream_ready(timeout)` observes the handshake event
  from its active long-lived WebSocket; the existing short-lived `wait_ready`
  probe remains available to the pipeline warm gate.
- Converters exposing `wait_stream_ready(timeout)` must return `True` before the
  bridge accepts audio or emits `ready`.
- Converters without that optional method remain immediately ready for existing
  test and local fakes.
- A false result, timeout, or exception closes the converter best-effort, reports
  only a generic `converter_unavailable` error, and closes the WebSocket before
  creating its audio receive task. Fatal stream teardown wakes the readiness wait.

## Verification

- Red: the initial readiness tests failed before the gate existed.
- Green: `.venv/bin/python -m pytest -q backend/test_desktop_audio.py`
  completed with 20 passing tests.
- Green: `.venv/bin/python -m pytest -q backend/test_streaming_safety.py`
  completed with 13 passing tests/subtests.
- `git diff --check` completed without findings.
