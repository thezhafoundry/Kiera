# Desktop readiness fix report

## Scope

Gate the desktop audio bridge's client-visible `ready` message on the converter
readiness handshake.

## Change

- `DesktopAudioBridge` now uses an explicit 150-second readiness timeout, matching
  the route's warm-gate timeout.
- Converters exposing `wait_ready(timeout)` must return `True` before the bridge
  accepts audio or emits `ready`.
- Converters without that optional method remain immediately ready for existing
  test and local fakes.
- A false result, timeout, or exception closes the converter best-effort, reports
  only a generic `converter_unavailable` error, and closes the WebSocket before
  creating its audio receive task.

## Verification

- Red: the new readiness tests failed before the gate existed.
- Green: `.venv/bin/python -m pytest -q backend/test_desktop_audio.py`
  completed with 19 passing tests.
- `git diff --check` completed without findings.
