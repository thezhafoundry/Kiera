# Task 1 Report: Harden call safety and engine selection

## Status

Implemented and locally verified on `codex/dual-track-hardening` from snapshot `2ff6568`.

## Implementation summary

- Replaced LLVC's output-idle fatal watchdog with a 2-second decision that requires both unacknowledged submitted input and a continuously unhealthy real LLVC conversion session. Pre-call idle, mute/no-pending-input, and healthy speech pauses do not terminate calls.
- Made `LLVCStreamingConverter.wait_ready()` wait on the actual call conversion socket's handshake/health instead of opening a temporary readiness socket.
- Added pre-dial/pre-bridge LLVC-to-RVC replacement: an unready LLVC worker is torn down before the PSTN leg starts, then a ready RVC worker is prepared.
- Made PSTN startup fail closed without a real RVC endpoint, even when the explicit development dummy switch is enabled. `DummyVoiceConverter` is disabled by default and reports `effective_engine=dummy` when explicitly used outside PSTN.
- Persisted and returned `requested_engine`, `effective_engine`, `fallback_reason`, and `model_version` for outbound preparation and inbound acceptance; added model version to worker telemetry.
- Replaced second-based outbound room IDs with a phone prefix plus 96 random bits.
- Moved synchronous Twilio hangup onto `asyncio.to_thread()` and added observed background-task handling, including the LLVC fatal cleanup callback.
- Changed the default `NS_LEVEL` from 3 to validated level 1 while preserving the environment override.

## Files changed

- `backend/converters/llvc_stream.py`
- `backend/main.py`
- `backend/pipeline.py`
- `backend/test_pipeline.py`
- `backend/test_call_safety.py` (new)
- `.superpowers/sdd/task-1-report.md` (this report)

The existing untracked `docs/superpowers/plans/2026-07-16-dual-track-hardening.md` was not modified or staged by this task.

## TDD RED evidence

Each production behavior was preceded by a focused failing test. Representative exact commands and observed failures:

1. Pre-call idle false positive:
   - Command: `.venv/bin/python -m unittest backend.test_call_safety.LLVCOutageDecisionTests.test_pre_call_idle_never_triggers_fatal_outage -v`
   - RED: `AssertionError: Expected mock to not have been awaited. Awaited 1 times.`
2. Actual LLVC session readiness:
   - Command: `.venv/bin/python -m unittest backend.test_call_safety.LLVCRealSessionReadinessTests.test_wait_ready_uses_the_active_conversion_session -v`
   - RED: `AssertionError: 2 != 1` (temporary readiness socket created a second connection).
3. Noise suppression default:
   - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety.ControlPlaneSafetyTests.test_noise_suppression_defaults_to_level_one -v`
   - RED: `AssertionError: 3 != 1`.
4. Missing RVC fail closed:
   - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety.ControlPlaneSafetyTests.test_missing_rvc_configuration_fails_closed_by_default -v`
   - RED: `AssertionError: HTTPException not raised` and the old path spawned Dummy.
5. Dummy engine reporting:
   - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety.ControlPlaneSafetyTests.test_explicit_development_dummy_is_never_reported_as_rvc -v`
   - RED: `AssertionError: 'rvc' != 'dummy'`.
6. Real-session fallback/state:
   - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety.ControlPlaneSafetyTests.test_outbound_falls_back_from_unready_real_llvc_session_to_rvc -v`
   - RED: outbound returned HTTP 503 instead of preparing RVC; after fallback was added the next RED exposed missing `requested_engine` in the response.
7. Collision-resistant room IDs:
   - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety.ControlPlaneSafetyTests.test_outbound_room_ids_are_collision_resistant -v`
   - RED: `_new_outbound_room_name` did not exist.
8. Async Twilio cleanup:
   - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety.ControlPlaneSafetyTests.test_twilio_hangup_runs_off_the_event_loop -v`
   - RED: expected `asyncio.to_thread` once, awaited 0 times.
9. Observed cleanup failures:
   - Commands:
     - `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety.ControlPlaneSafetyTests.test_background_cleanup_task_failures_are_observed -v`
     - `.venv/bin/python -m unittest backend.test_call_safety.LLVCOutageDecisionTests.test_fatal_cleanup_callback_failure_is_observed -v`
   - RED: missing observed-task helper; LLVC cleanup emitted `Task exception was never retrieved`.
10. PSTN dummy rejection even with the dev switch:
    - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety.ControlPlaneSafetyTests.test_pstn_rejects_dummy_even_when_development_dummy_is_enabled -v`
    - RED: `_do_start_bot()` had no `require_real_engine` contract.

## GREEN and verification evidence

- Focused suite:
  - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m unittest backend.test_call_safety -v`
  - Result: `Ran 15 tests ... OK`.
- Syntax compilation:
  - Command: `.venv/bin/python -m py_compile backend/main.py backend/pipeline.py backend/converters/llvc_stream.py backend/test_call_safety.py backend/test_pipeline.py`
  - Result: exit 0.
- Diff formatting:
  - Command: `git diff --check`
  - Result: exit 0, no findings.
- Full pipeline suite (required):
  - Command: `PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m backend.test_pipeline`
  - First sandboxed attempt: blocked by `PermissionError: [Errno 1]` when binding loopback WebSocket test servers.
  - Final approved loopback run: exit 0 with `All automated verification tests completed successfully!`.
- Session close audit:
  - Command: `make second-brain-close`
  - Result: `CHECKS PASSED`; wiki errors 0, credential-pattern matches 0, stale-claim matches 0.

## Self-review

- Verified the LLVC readiness event is set only after the real conversion socket handshake and cleared on disconnect, error, or close.
- Verified LLVC fallback occurs only before outbound dial or inbound bridge; no mid-call engine switching was introduced.
- Verified all PSTN call entry points pass `require_real_engine=True`, including the RVC fallback path.
- Verified the development Dummy path is opt-in, non-PSTN, and records `effective_engine=dummy`.
- Verified fallback cleanup preserves inbound call state while replacing only the worker/session.
- Verified all background tasks introduced/touched in this slice retrieve terminal exceptions.
- Verified no `.env` values were read for tests (`PYTHON_DOTENV_DISABLED=1`) and no Modal/Twilio/LiveKit deployment or live provider call was performed.

## Concerns and verification boundaries

- Local tests emit existing FastAPI `on_event` deprecation warnings; they are unrelated to Task 1 and do not fail verification.
- Real Render, LiveKit, Twilio, RVC, and LLVC behavior remains unverified; this task intentionally performed checkout-only tests and no external deployment.
- The initial full-suite attempt could not bind loopback under the default sandbox; the same exact suite passed after loopback permission was granted.
